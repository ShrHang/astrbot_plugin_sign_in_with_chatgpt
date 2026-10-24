"""AstrBot 4.29.1 authenticated management boundary and short-lived UI state."""
from __future__ import annotations
import asyncio
import copy
import json
import secrets
import time
import webbrowser

import jwt
from starlette.responses import JSONResponse

from . import DISPLAY_NAME, PLUGIN_NAME
from .errors import SIWCError, public_error

ACTIVE_PHASES = {"preparing", "waiting_browser", "validating"}
ROUTES = {"status": "GET", "auth/start": "POST", "auth/cancel": "POST", "auth/logout": "POST",
          "models": "GET", "models/refresh": "POST"}


class Management:
    def __init__(self, service, *, browser=None, login_timeout=180):
        self.service = service
        self.browser = browser or self.open_browser
        self.login_timeout = login_timeout
        self.lock = asyncio.Lock()
        self.attempt = None
        self.logout_result = None
        self.auth_task = self.logout_task = None
        self.auth_lease = None
        self.stopping = False
        self.routes = []

    @staticmethod
    async def open_browser(url):
        if not await asyncio.to_thread(webbrowser.open, url):
            raise SIWCError("browser", "系统浏览器未能打开；请检查默认浏览器设置后重试")

    def snapshot(self):
        return {"account": self.service.status(), "attempt": copy.deepcopy(self.attempt),
                "logout": copy.deepcopy(self.logout_result)}

    def available(self):
        if self.stopping or self.service.closed:
            raise SIWCError("closed", "管理服务已停止，请刷新 AstrBot 页面")

    async def start_auth(self):
        async with self.lock:
            self.available()
            if self.auth_task and not self.auth_task.done():
                return self.snapshot()
            self.auth_lease = self.service.reserve_login()
            self.logout_result = None
            now = time.time()
            self.attempt = {"attempt_id": secrets.token_urlsafe(18), "started_at": now,
                            "deadline": now + self.login_timeout, "phase": "preparing", "error": None}
            self.auth_task = asyncio.create_task(self._authorize(self.attempt, self.auth_lease), name="siwc:authorization")
            return self.snapshot()

    async def _authorize(self, attempt, lease):
        generation = self.service.generation
        def phase(value):
            if self.attempt is attempt and attempt["phase"] in ACTIVE_PHASES:
                attempt["phase"] = value
        try:
            await self.service.login(self.browser, _lease=lease, on_phase=phase, timeout=self.login_timeout)
            attempt["phase"] = "succeeded"
        except asyncio.CancelledError:
            committed = self.service.generation > generation and self.service.state == "ready"
            attempt["phase"] = "succeeded" if committed else "cancelled"
        except Exception as error:
            attempt["phase"] = "expired" if isinstance(error, SIWCError) and error.category == "timeout" else "failed"
            attempt["error"] = public_error(error)
        finally:
            if self.service._auth_lease is lease:
                self.service._auth_lease = None

    async def cancel_auth(self, attempt_id):
        async with self.lock:
            self.available()
            if not self.attempt or attempt_id != self.attempt["attempt_id"]:
                raise SIWCError("request", "授权尝试已变化，请刷新状态")
            if self.auth_task and not self.auth_task.done():
                await self.service._cancel([self.auth_task])
            if self.service._auth_lease is self.auth_lease:
                self.service._auth_lease = None
            if self.attempt["phase"] in ACTIVE_PHASES:
                self.attempt["phase"] = "cancelled"
            return self.snapshot()

    async def start_logout(self):
        async with self.lock:
            self.available()
            if self.logout_task and not self.logout_task.done():
                return self.snapshot()
            self.service.logging_out = True
            self.service.generation += 1  # Invalidate in-flight snapshots before returning.
            self.logout_result = {"phase": "clearing", "local_cleared": False, "remote_confirmed": None, "error": None}
            self.logout_task = asyncio.create_task(self._logout(self.logout_result), name="siwc:logout")
            return self.snapshot()

    async def _logout(self, result):
        try:
            await self.service._cancel([self.auth_task] if self.auth_task else [])
            if self.service._auth_lease is self.auth_lease:
                self.service._auth_lease = None
            if self.attempt and self.attempt["phase"] in ACTIVE_PHASES:
                self.attempt["phase"] = "cancelled"
            def local_cleared():
                result["local_cleared"] = True
                result["phase"] = "revoking"
            result["remote_confirmed"] = await self.service.logout(on_local_clear=local_cleared)
            result["local_cleared"] = True
            result["phase"] = "succeeded"
        except asyncio.CancelledError:
            result["phase"] = "cancelled"
        except Exception as error:
            result["phase"], result["error"] = "failed", public_error(error)
        finally:
            self.service.logging_out = False

    async def dispatch(self, action, payload):
        self.available()
        required = {"auth/cancel": {"attempt_id"}}
        if not isinstance(payload, dict) or set(payload) != required.get(action, set()):
            raise SIWCError("request", "管理接口参数无效")
        if any(not isinstance(value, str) or not value or len(value) > 200 for value in payload.values()):
            raise SIWCError("request", "管理接口参数无效")
        if action == "status":
            return self.snapshot()
        if action == "auth/start":
            return await self.start_auth()
        if action == "auth/cancel":
            return await self.cancel_auth(payload["attempt_id"])
        if action == "auth/logout":
            return await self.start_logout()
        if action in {"models", "models/refresh"}:
            return {"models": await self.service.models(force=action.endswith("refresh"))}
        raise SIWCError("request", "未知管理操作")

    @staticmethod
    def authorize_request(request):
        # Host authenticates extension routes. Recheck its JWT to reject asset
        # tokens even on host paths that only check the signed username claim.
        raw = request._request
        from astrbot.dashboard.services.auth_service import DASHBOARD_JWT_COOKIE_NAME
        bearer = request.headers.get("authorization", "")
        token = bearer[7:].strip() if bearer.startswith("Bearer ") else request.cookies.get(DASHBOARD_JWT_COOKIE_NAME, "")
        try:
            claims = jwt.decode(token, raw.app.state.jwt_secret, algorithms=["HS256"], options={"require": ["exp", "username"]})
        except (jwt.PyJWTError, AttributeError, TypeError):
            raise SIWCError("unauthorized", "管理认证失效，请回到 AstrBot 登录") from None
        if claims.get("token_type") is not None or not request.username or claims.get("username") != request.username:
            raise SIWCError("unauthorized", "管理认证失效，请回到 AstrBot 登录")
        origin = request.headers.get("origin")
        expected = f"{raw.url.scheme}://{raw.url.netloc}"
        if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
            raise SIWCError("forbidden", "拒绝跨站管理请求")
        if request.method == "POST" and not bearer.startswith("Bearer ") and origin != expected:
            raise SIWCError("forbidden", "Cookie 管理操作需要同源 Origin")
        if list(request.query.keys()):
            raise SIWCError("request", "管理接口不接受 URL 参数")

    def register(self, context):
        from astrbot.api.web import request
        for action, method in ROUTES.items():
            async def handler(_action=action):
                status = 200
                try:
                    self.authorize_request(request)
                    body = await request.body()
                    if len(body) > 4096:
                        raise SIWCError("request", "管理请求过大")
                    payload = json.loads(body) if body else {}
                    data = {"ok": True, "result": await self.dispatch(_action, payload)}
                except SIWCError as error:
                    status = {"unauthorized": 401, "forbidden": 403}.get(error.category, 200)
                    data = {"ok": False, "error": public_error(error)}
                except (ValueError, TypeError):
                    data = {"ok": False, "error": public_error(SIWCError("request", "无效 JSON 请求"))}
                except Exception as error:
                    data = {"ok": False, "error": public_error(error)}
                return JSONResponse({"status": "ok", "data": data}, status_code=status,
                                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            route = f"/{PLUGIN_NAME}/{action}"
            context.register_web_api(route, handler, [method], f"{DISPLAY_NAME} account management")
            self.routes.append((route, handler, [method]))

    def unregister(self, context):
        owned = self.routes
        context.registered_web_apis[:] = [entry for entry in context.registered_web_apis
            if not any(entry[0] == path and entry[1] is handler and entry[2] == methods for path, handler, methods in owned)]
        self.routes = []

    async def stop(self):
        self.stopping = True
        await self.service._cancel([task for task in (self.auth_task, self.logout_task) if task])
        if self.service._auth_lease is self.auth_lease:
            self.service._auth_lease = None
