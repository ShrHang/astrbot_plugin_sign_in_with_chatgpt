"""Single credential owner and generation-aware operation/request lifecycle."""
from __future__ import annotations
import asyncio
import copy
import time
from functools import wraps
from pathlib import Path
import httpx
from . import DISPLAY_NAME, RESOURCE
from .auth import OAuth, official_url
from .credentials import CredentialStore
from .errors import SIWCError, http_error, public_error
from .usage import UsageStore


async def disk_call(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    return await finish_atomic(task)


async def finish_atomic(task):
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def tracked(operation):
    @wraps(operation)
    async def run(self, *args, **kwargs):
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            return await operation(self, *args, **kwargs)
        finally:
            self.tasks.discard(task)
    return run


class Service:
    def __init__(self, directory: Path, config=None, *, client=None):
        config = config or {}
        self.store = CredentialStore(directory)
        self.client = client or httpx.AsyncClient(proxy=config.get("proxy") or None, follow_redirects=False,
            trust_env=False, timeout=httpx.Timeout(float(config.get("read_timeout", 90)), connect=float(config.get("connect_timeout", 15))))
        self.oauth = OAuth(self.client)
        self.total_timeout = max(5, float(config.get("total_timeout", 180)))
        self.semaphore = asyncio.Semaphore(max(1, int(config.get("max_concurrency", 2))))
        self.refresh_lock, self.commit_lock = asyncio.Lock(), asyncio.Lock()
        self.generation = 0
        self.record = {}
        self.state = "unbound"
        self.health = None
        self.refreshing = self.closed = self.logging_out = False
        self._auth_lease = None
        self.tasks = set()
        self.requests = {}
        self.model_cache = None
        self.usage = UsageStore(self.store.directory)

    async def start(self):
        try:
            self.record = await disk_call(self.store.open)
            await disk_call(self.usage.open)
            state = self.record.get("account_state")
            self.state = state if state in ("login_required", "configuration_error") else (
                "ready" if (self.record.get("registration") or {}).get("access_token") else "unbound")
        except BaseException:
            await self.client.aclose()
            await disk_call(self.store.close)
            raise

    @property
    def authorizing(self):
        return self._auth_lease is not None

    def admission(self):
        if self.closed:
            raise SIWCError("closed", "插件已停止")
        if self.logging_out:
            raise SIWCError("busy", "正在解绑，请稍后重试")
        if self.authorizing:
            raise SIWCError("busy", "正在授权，请完成或取消后重试")
        if self.state == "configuration_error":
            raise SIWCError("configuration_error", "客户端配置异常；请检查注册信息，不要循环登录")

    def reserve_login(self):
        self.admission()
        self._auth_lease = object()
        return self._auth_lease

    def track_request(self, task):
        self.admission()
        self.tasks.add(task)
        self.requests[task] = self.generation

    def untrack_request(self, task):
        self.tasks.discard(task)
        self.requests.pop(task, None)

    @staticmethod
    async def _cancel(tasks):
        tasks = [task for task in tasks if task is not asyncio.current_task()]
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_requests(self, *, before=None):
        await self._cancel([task for task, generation in self.requests.items() if before is None or generation < before])

    def note_health(self, error, generation):
        if not self.closed and generation == self.generation:
            self.health = public_error(error) if error else None

    def status(self):
        registration = self.record.get("registration") or {}
        return {"state": self.state, "registered": bool(registration.get("client_id")),
            "registration_pending": bool(self.record.get("pending_client_id")),
            "connected": bool(registration.get("access_token")) and self.state == "ready",
            "email": registration.get("email", ""),
            "welcome_pending": bool(self.record.get("welcome_pending")) and self.state == "ready",
            "expires_at": registration.get("expires_at"), "authorizing": self.authorizing,
            "refreshing": self.refreshing, "logging_out": self.logging_out, "health": copy.deepcopy(self.health)}

    def usage_snapshot(self):
        return self.usage.snapshot()

    def record_usage(self, generation, **request):
        # Statistics describe this installation's already-dispatched requests.
        # Logout, reauthorization and shutdown advance the account generation
        # before cancelling producers; their cancellation still belongs here.
        # UsageStore.close runs after those producers finish and rejects later
        # records. Account health and response delivery remain generation-gated.
        try:
            self.usage.record(**request)
        except Exception:
            # Local dashboard storage must never change inference delivery.
            self.usage.storage_error = True

    @tracked
    async def acknowledge_welcome(self):
        self.admission()
        generation = self.generation
        async with self.commit_lock:
            if self.closed or generation != self.generation:
                raise SIWCError("cancelled", "账号状态已变化")
            if self.record.get("welcome_pending"):
                await self._save({**self.record, "plan_notice_acknowledged": True, "welcome_pending": False})

    async def _save(self, record):
        async def commit():
            await asyncio.to_thread(self.store.save, record)
            self.record = record
        task = asyncio.create_task(commit())
        await finish_atomic(task)

    @tracked
    async def login(self, open_browser, *, _lease=None, **kwargs):
        lease = _lease if _lease is not None else self.reserve_login()
        if self.closed or lease is not self._auth_lease:
            raise SIWCError("cancelled", "授权尝试已失效")
        generation, old_state = self.generation, self.state
        async def retain_client(client_id):
            async with self.commit_lock:
                if generation != self.generation or self.closed:
                    raise SIWCError("cancelled", "账号状态已变化，注册结果已丢弃")
                await self._save({**self.record, "pending_client_id": client_id})
        try:
            async with asyncio.timeout(float(kwargs.get("timeout", 180))), self.refresh_lock:
                if self.logging_out or self.closed or lease is not self._auth_lease:
                    raise SIWCError("cancelled", "授权尝试已失效")
                if self.state == "configuration_error":
                    raise SIWCError("configuration_error", "客户端配置异常，请检查注册信息")
                generation, old_state = self.generation, self.state
                registration = await self.oauth.sign_in(copy.deepcopy(self.record), open_browser,
                    on_registered=retain_client, **kwargs)
                async with self.commit_lock:
                    if self.closed or generation != self.generation:
                        raise SIWCError("cancelled", "账号状态已变化，登录结果已丢弃")
                    record = {**self.record, "registration": registration, "pending_client_id": None, "account_state": "ready",
                              "welcome_pending": not bool(self.record.get("plan_notice_acknowledged"))}
                    try:
                        await self._save(record)
                    except asyncio.CancelledError:
                        if self.record is not record:
                            raise
                    self.generation += 1
                    self.model_cache = None
                    self.state, self.health = "ready", None
                await self.cancel_requests(before=self.generation)
        except TimeoutError:
            raise SIWCError("timeout", "授权尝试已超时，请重新开始") from None
        except httpx.HTTPError:
            raise SIWCError("network", "授权网络请求失败；原绑定保留，请检查代理与连接") from None
        except BaseException:
            if generation == self.generation:
                self.state = old_state
            raise
        finally:
            if self._auth_lease is lease:
                self._auth_lease = None

    async def _terminal_refresh(self, error, generation):
        async with self.commit_lock:
            if generation != self.generation or self.closed:
                return
            registration = self.record.get("registration") or {}
            if error.category == "login_required":
                registration = {k: registration[k] for k in ("client_id", "sub", "email") if k in registration}
            self.generation += 1
            self.state, self.model_cache = error.category, None
            self.record = {**self.record, "registration": registration, "account_state": self.state}
            await self._save(self.record)
            self.health = public_error(error)
        await self.cancel_requests(before=self.generation)

    async def access_token(self):
        self.admission()
        async with self.refresh_lock:
            self.admission()
            registration = self.record.get("registration") or {}
            if not registration.get("access_token") or self.state == "login_required":
                raise SIWCError("login_required", "尚无有效绑定，请在插件的 account 管理页授权")
            if registration.get("expires_at", 0) > time.time() + 60:
                return registration["access_token"]
            generation = self.generation
            self.refreshing = True
            try:
                async with asyncio.timeout(self.total_timeout):
                    tokens = await self.oauth.tokens({"grant_type": "refresh_token", "client_id": registration["client_id"],
                        "refresh_token": registration["refresh_token"]})
                    if tokens.get("id_token"):
                        claims = await self.oauth.validate(tokens["id_token"], registration["client_id"])
                        if claims["sub"] != registration["sub"]:
                            raise SIWCError("identity", "刷新后的账号身份不符")
                scopes = tokens.get("scope", registration.get("scope", ""))
                if not {"resource.invoke", "chatgpt.tokens.use.direct"} <= set(scopes.split()):
                    raise SIWCError("permission", "刷新结果不包含订阅推理权限")
                updated = {**registration, **tokens, "scope": scopes, "expires_at": time.time() + float(tokens["expires_in"])}
                async with self.commit_lock:
                    if self.closed or generation != self.generation:
                        raise SIWCError("cancelled", "账号状态已变化，刷新结果已丢弃")
                    await self._save({**self.record, "registration": updated})
                return updated["access_token"]
            except SIWCError as error:
                if error.category in {"login_required", "configuration_error"}:
                    await self._terminal_refresh(error, generation)
                else:
                    self.note_health(error, generation)
                raise
            except (httpx.HTTPError, TimeoutError) as exc:
                error = SIWCError("timeout" if isinstance(exc, TimeoutError) else "network", "刷新连接失败或超时；凭据保留", stage="token_refresh")
                self.note_health(error, generation)
                raise error from None
            finally:
                self.refreshing = False

    @tracked
    async def logout(self, *, on_local_clear=None):
        self.logging_out = True
        try:
            async with self.commit_lock:
                registration = self.record.get("registration") or {}
                renewal_pending = self.refresh_lock.locked()
                self.generation += 1
                self.model_cache = None
                retained = {k: registration[k] for k in ("client_id", "sub", "email") if k in registration}
                self.record = {**self.record, "registration": retained or None, "account_state": "unbound", "welcome_pending": False}
                self.state = "unbound"
                await self._save(self.record)
            if on_local_clear:
                on_local_clear()
            await self._cancel(list(self.tasks))
            if not registration.get("refresh_token"):
                return True
            try:
                async with asyncio.timeout(self.total_timeout):
                    discovery = await self.oauth.discover()
                    response = await self.client.post(official_url(discovery["revocation_endpoint"]), data={
                        "token": registration["refresh_token"], "token_type_hint": "refresh_token", "client_id": registration["client_id"]})
                    return response.status_code == 200 and not renewal_pending
            except (httpx.HTTPError, SIWCError, KeyError, ValueError, TimeoutError):
                return False
        finally:
            self.logging_out = False

    async def models(self, *, force=False):
        return [item["slug"] for item in await self.model_catalog(force=force)]

    @tracked
    async def model_catalog(self, *, force=False):
        now = time.monotonic()
        if not force and self.model_cache and self.model_cache[0] > now and self.model_cache[1] == self.generation:
            return copy.deepcopy(self.model_cache[2])
        self.admission()
        generation = self.generation
        task = asyncio.current_task()
        self.requests[task] = generation
        try:
            async with asyncio.timeout(self.total_timeout), self.semaphore:
                token = await self.access_token()
                response = await self.client.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
                if response.is_error:
                    raise http_error(response, stage="model_catalog")
                models = []
                seen = set()
                for item in response.json().get("models", []):
                    if not isinstance(item, dict) or item.get("visibility") != "list" or not isinstance(item.get("slug"), str):
                        continue
                    slug = item["slug"]
                    if not slug or slug in seen:
                        continue
                    display_name = item.get("display_name")
                    models.append({"slug": slug, "display_name": display_name if isinstance(display_name, str) and display_name.strip() else slug})
                    seen.add(slug)
                if not models:
                    raise SIWCError("protocol", "模型目录未返回可显示的模型 slug")
                if generation != self.generation:
                    raise SIWCError("cancelled", "获取目录时账号状态已变化")
                self.model_cache = (now + 300, generation, models)
                return copy.deepcopy(models)
        except (httpx.HTTPError, TimeoutError) as exc:
            error = SIWCError("timeout" if isinstance(exc, TimeoutError) else "network", "模型目录连接失败或超时", stage="model_catalog")
            self.note_health(error, generation)
            raise error from None
        except SIWCError as error:
            self.note_health(error, generation)
            raise
        finally:
            self.requests.pop(task, None)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        await self._cancel(list(self.tasks))
        await finish_atomic(asyncio.create_task(self.usage.close()))
        self._auth_lease = None
        await self.oauth.close()
        await self.client.aclose()
        await disk_call(self.store.close)


_active = None


def bind(service):
    global _active
    _active = service


def unbind(service):
    global _active
    if _active is service:
        _active = None


def active():
    if _active is None or _active.closed:
        raise SIWCError("unavailable", f"{DISPLAY_NAME} 插件未启用")
    return _active
