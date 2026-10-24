"""Errors deliberately exclude server bodies, URLs, credentials and chat content."""

import re

STAGES = {"oidc_discovery", "jwks", "authorization_code_exchange", "token_refresh", "model_catalog", "inference"}
KNOWN_CODES = {
    "invalid_grant", "invalid_client", "invalid_request", "invalid_scope", "unauthorized_client", "access_denied",
    "unsupported_grant_type", "server_error", "temporarily_unavailable", "unsupported_country_region_territory",
    "subscription_sharing_user_not_eligible", "subscription_sharing_usage_limit_exceeded",
    "subscription_sharing_usage_unavailable", "subscription_sharing_unsupported_capability",
    "subscription_sharing_route_not_supported", "subscription_sharing_invalid_user",
    "chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context",
    "subscription_sharing_user_unavailable", "invalid_refresh_token", "token_expired",
    "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused",
}
TERMINAL_REFRESH_CODES = {"invalid_grant", "invalid_refresh_token", "token_expired",
                          "refresh_token_expired", "refresh_token_invalidated", "refresh_token_reused"}


def code_category(code, *, stage=None):
    """Shared HTTP/SSE recovery policy; never use arbitrary server messages."""
    if code == "invalid_client":
        return "configuration_error", "客户端配置异常；请检查注册配置，不要重复登录或自动注册"
    if stage == "token_refresh" and code in TERMINAL_REFRESH_CODES:
        return "login_required", "续期凭据已失效；请在插件管理页重新授权"
    if code in {"subscription_sharing_usage_limit_exceeded", "insufficient_quota", "usage_limit_reached", "rate_limit_exceeded"}:
        return "limited", "请求或订阅用量受限；保留授权，请在 ChatGPT 设置中查看用量"
    if code in {"subscription_sharing_usage_unavailable", "subscription_sharing_user_unavailable", "server_error", "temporarily_unavailable"}:
        return "temporary", "服务或用量检查暂不可用；授权已保留，请稍后显式重试"
    if code in {"subscription_sharing_route_not_supported", "subscription_sharing_unsupported_capability"}:
        return "request", "当前路由或能力不受支持；请检查请求配置，不要重复相同请求"
    if code in {"subscription_sharing_user_not_eligible", "chatpass_v2_scope_not_authorized", "chatpass_v2_invalid_authorization_context"}:
        return "permission", "账号、工作区或授权策略不允许此操作；请检查权限，不要循环登录"
    if code == "subscription_sharing_invalid_user":
        return "permission", "订阅身份上下文未通过校验；保留授权并检查诊断信息"
    if code in {"invalid_api_key", "invalid_token", "token_expired"}:
        return "login_required", "当前授权被拒绝；请检查绑定状态"
    if code == "invalid_grant":
        return "login_required", "本次授权码交换失败；请重新发起授权，原绑定保持不变"
    return None


class SIWCError(RuntimeError):
    def __init__(self, category: str, message: str, status: int | None = None, *, stage=None, diagnostics=None):
        self.category = category
        self.user_message = message
        self.status = status
        self.stage = stage if stage in STAGES else None
        self.diagnostics = diagnostics or {}
        # All diagnostic values are selected or validated below, never raw server text.
        details = ([f"stage={self.stage}"] if self.stage else [])
        details.extend(f"{key}={value}" for key, value in self.diagnostics.items())
        suffix = " [" + ", ".join(details) + "]" if details else ""
        super().__init__(f"SIWC {category}: {message}" + (f" (HTTP {status})" if status else "") + suffix)


def http_error(response, *, oauth: bool = False, stage=None) -> SIWCError:
    status = response.status_code
    code = ""
    param = ""
    response_format = "empty" if not response.content else "other"
    try:
        data = response.json()
        response_format = "json_object" if isinstance(data, dict) else "json_other"
        error = data.get("error", {})
        code = error if isinstance(error, str) else error.get("code", "")
        param = error.get("param", "") if isinstance(error, dict) else ""
    except (ValueError, AttributeError, TypeError):
        pass
    if response_format == "other" and "text/html" in response.headers.get("content-type", "").lower():
        response_format = "html"
    code = code if isinstance(code, str) else ""
    param = param if isinstance(param, str) else ""
    diagnostics = {"response_format": response_format}
    if code:
        diagnostics["error_code"] = code if code in KNOWN_CODES else "unrecognized"
    for header in ("x-request-id", "openai-request-id"):
        request_id = response.headers.get(header, "")
        if re.fullmatch(r"req_[A-Za-z0-9_-]{1,128}|[a-fA-F0-9]{16,64}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", request_id):
            diagnostics["request_id"] = request_id
            break
    if response.headers.get("cf-mitigated") == "challenge":
        diagnostics["edge_challenge"] = "true"

    def result(category, message):
        return SIWCError(category, message, status, stage=stage, diagnostics=diagnostics)

    policy = code_category(code, stage=stage)
    if policy:
        return result(*policy)
    if status == 401:
        return result("login_required", "访问令牌被拒绝，请检查授权或重新登录")
    if status == 403:
        if response_format == "html":
            return result("http_forbidden", "HTTP 请求被拒绝且返回 HTML，不能据此判定账号权限；请检查该阶段的网络路径")
        return result("permission", "服务端拒绝了此 HTTP 请求；请结合失败阶段及错误代码定位原因")
    if status == 429:
        return result("limited", "请求或订阅额度受限，请稍后重试；授权已保留")
    if status >= 500:
        return result("temporary", "官方服务暂不可用")
    allowed = {"model", "input", "tools", "tool_choice", "reasoning", "store", "stream"}
    field = f"，字段 {param}" if param in allowed else ""
    return result("request", "官方服务拒绝请求" + field)


def sse_error(event):
    response = event.get("response")
    error = response.get("error") if isinstance(response, dict) else None
    error = error or event.get("error") or event
    if not isinstance(error, dict):
        error = {}
    for _ in range(3):
        if isinstance(error.get("code"), str) or not isinstance(error.get("error"), dict):
            break
        error = error["error"]
    code = error.get("code")
    code = code if isinstance(code, str) else ""
    policy = code_category(code, stage="inference")
    diagnostics = {"response_format": "sse"}
    if code:
        diagnostics["error_code"] = code if code in KNOWN_CODES else "unrecognized"
    return SIWCError(*(policy or ("incomplete", "推理失败或未完整完成；本次输出不能视为成功")),
                     stage="inference", diagnostics=diagnostics)


def public_error(error):
    """Only structured, sanitized information crosses the management boundary."""
    if not isinstance(error, SIWCError):
        error = SIWCError("internal", "操作失败；请检查运行环境后重试")
    return {"category": error.category, "stage": error.stage, "message": error.user_message,
            "http_status": error.status, "diagnostics": dict(error.diagnostics)}
