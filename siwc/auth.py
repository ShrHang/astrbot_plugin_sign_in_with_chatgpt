"""Official public-client OAuth, one-shot loopback callback and OIDC validation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt

from . import AUTHORIZE, ISSUER, RESOURCE, SCOPES, TOKEN
from .errors import SIWCError, http_error


def official_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != "auth.openai.com" or parts.fragment:
        raise SIWCError("identity", "发现文档包含不受信任的授权端点")
    return url


class OAuth:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.discovery = None
        self.jwks = None
        self.keys_at = 0.0
        self.listener = None
        self.handlers: set[asyncio.Task] = set()

    async def discover(self) -> dict:
        if self.discovery is None:
            response = await self.client.get(ISSUER + "/.well-known/openid-configuration")
            if response.is_error:
                raise http_error(response, stage="oidc_discovery")
            data = response.json()
            if data.get("issuer") != ISSUER:
                raise SIWCError("identity", "OIDC issuer 不匹配")
            for key in ("jwks_uri", "authorization_endpoint", "token_endpoint"):
                official_url(data[key])
            if data["authorization_endpoint"] != AUTHORIZE or data["token_endpoint"] != TOKEN:
                raise SIWCError("identity", "授权合同已变化，请重新核对官方文档")
            self.discovery = data
        return self.discovery

    async def prepare_identity(self):
        """Check public identity endpoints before opening the authorization browser."""
        discovery = await self.discover()
        if self.jwks is None or time.monotonic() - self.keys_at > 3600:
            response = await self.client.get(discovery["jwks_uri"])
            if response.is_error:
                raise http_error(response, stage="jwks")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("keys"), list) or not data["keys"]:
                raise SIWCError("identity", "公开 JWKS 未返回可用公钥", stage="jwks")
            self.jwks = data
            self.keys_at = time.monotonic()

    async def validate(self, token: str, client_id: str, nonce: str | None = None) -> dict:
        discovery = await self.discover()
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in {"RS256", "ES256"}:
                raise ValueError
            if self.jwks is None or time.monotonic() - self.keys_at > 3600 or not any(
                key.get("kid") == header.get("kid") for key in self.jwks.get("keys", [])
            ):
                response = await self.client.get(discovery["jwks_uri"])
                if response.is_error:
                    raise http_error(response, stage="jwks")
                self.jwks = response.json()
                self.keys_at = time.monotonic()
            key = next(k for k in self.jwks["keys"] if k.get("kid") == header.get("kid"))
            claims = await asyncio.to_thread(
                jwt.decode, token, jwt.PyJWK.from_dict(key, algorithm=algorithm).key,
                algorithms=[algorithm], audience=client_id, issuer=ISSUER, leeway=5,
                options={"require": ["sub", "exp", "iat"]},
            )
            if not isinstance(claims["sub"], str) or not claims["sub"]:
                raise ValueError
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError
            if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
                raise ValueError
            if claims.get("azp") is not None and claims["azp"] != client_id:
                raise ValueError
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, StopIteration, TypeError):
            raise SIWCError("identity", "ID token 签名或身份声明校验失败") from None

    async def tokens(self, fields: dict) -> dict:
        response = await self.client.post(TOKEN, data={**fields, "resource": RESOURCE})
        if response.is_error:
            stage = "token_refresh" if fields.get("grant_type") == "refresh_token" else "authorization_code_exchange"
            raise http_error(response, oauth=True, stage=stage)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise SIWCError("identity", "授权响应缺少 access token")
        if str(data.get("token_type", "")).lower() != "bearer":
            raise SIWCError("identity", "授权响应的 token_type 无效")
        try:
            lifetime = float(data["expires_in"])
            if not math.isfinite(lifetime) or lifetime <= 0:
                raise ValueError
            for key in ("refresh_token", "id_token", "scope"):
                if key in data and (not isinstance(data[key], str) or not data[key]):
                    raise ValueError
        except (KeyError, ValueError, TypeError):
            raise SIWCError("identity", "授权响应的有效期或 token 字段无效") from None
        # Identity/registration fields are never taken from an unverified token response.
        return {k: v for k, v in data.items() if k in {
            "access_token", "refresh_token", "id_token", "scope", "token_type", "expires_in"}}

    async def sign_in(self, record: dict, open_browser, *, port: int = 0, timeout: float = 180, on_registered=None, on_phase=None) -> dict:
        if on_phase:
            on_phase("preparing")
        await self.prepare_identity()
        state, nonce, verifier = (secrets.token_urlsafe(48) for _ in range(3))
        result = asyncio.get_running_loop().create_future()
        registration = record.get("registration") or {}
        selected = registration.get("client_id") or record.get("pending_client_id")

        async def callback(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            status = "400 Bad Request"
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
                method, target, _ = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ", 2)
                url = urlsplit(target)
                query = parse_qs(url.query, keep_blank_values=True)
                if method != "GET" or url.path != "/auth/callback" or any(len(v) != 1 for v in query.values()):
                    raise ValueError
                if not secrets.compare_digest(query.get("state", [""])[0], state) or result.done():
                    raise ValueError
                # Matching state consumes this attempt, including access_denied and bad client IDs.
                result.set_result({k: v[0] for k, v in query.items()})
                status = "200 OK"
            except (ValueError, UnicodeError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            finally:
                try:
                    message = ("已收到授权回调，账号绑定尚未完成。请返回发起登录的界面查看最终结果，可关闭本页。"
                               if status == "200 OK" else
                               "此授权回调未通过校验。请返回发起登录的界面查看状态。")
                    writer.write((f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\n\r\n" + message).encode())
                    await writer.drain()
                except (OSError, ConnectionError):
                    pass
                writer.close()
                await writer.wait_closed()
                self.handlers.discard(task)

        self.listener = await asyncio.start_server(callback, "127.0.0.1", port, limit=16384)
        actual_port = self.listener.sockets[0].getsockname()[1]
        redirect = f"http://127.0.0.1:{actual_port}/auth/callback"
        fields = {
            "client_id": selected or "dynamic_agent_client", "ext_agent_host_id": record["host_id"],
            "response_type": "code", "redirect_uri": redirect, "scope": SCOPES, "resource": RESOURCE,
            "state": state, "nonce": nonce, "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
        }
        if selected:
            if registration.get("id_token"):
                fields["id_token_hint"] = registration["id_token"]
            if registration.get("email"):
                fields["login_hint"] = registration["email"]
        else:
            fields["agent_name_hint"] = "AstrBot Personal ChatGPT"
        try:
            if on_phase:
                on_phase("waiting_browser")
            await open_browser(AUTHORIZE + "?" + urlencode(fields))
            response = await asyncio.wait_for(result, timeout)
            if on_phase:
                on_phase("validating")
            if response.get("error"):
                raise SIWCError("consent", "授权未完成或已取消")
            client_id = response.get("client_id") or selected
            if not client_id or client_id == "dynamic_agent_client" or (selected and client_id != selected):
                raise SIWCError("identity", "回调 client_id 与注册信息不符")
            if not response.get("code"):
                raise SIWCError("identity", "回调缺少授权码")
            if not selected and on_registered is not None:
                await on_registered(client_id)
            tokens = await self.tokens({"grant_type": "authorization_code", "client_id": client_id,
                                       "code": response["code"], "code_verifier": verifier, "redirect_uri": redirect})
            claims = await self.validate(tokens.get("id_token", ""), client_id, nonce)
            if registration.get("sub") and claims["sub"] != registration["sub"]:
                raise SIWCError("identity", "重新登录的账号与原注册账号不符")
            if not {"resource.invoke", "chatgpt.tokens.use.direct"} <= set(tokens.get("scope", "").split()):
                raise SIWCError("permission", "授权未授予 ChatGPT 订阅推理 scope")
            if not tokens.get("refresh_token"):
                raise SIWCError("identity", "授权响应缺少可续期会话")
            return {**tokens, "client_id": client_id, "sub": claims["sub"], "email": claims.get("email", ""),
                    "expires_at": time.time() + float(tokens["expires_in"])}
        except asyncio.TimeoutError:
            raise SIWCError("timeout", "登录已超时，请重新开始") from None
        finally:
            await self.close()

    async def close(self):
        if self.listener:
            self.listener.close()
            await self.listener.wait_closed()
            self.listener = None
        for task in list(self.handlers):
            task.cancel()
        if self.handlers:
            await asyncio.gather(*list(self.handlers), return_exceptions=True)
        self.handlers.clear()
