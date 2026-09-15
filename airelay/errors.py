"""统一错误码与异常。

协议面（/v1/*）抛出的错误会被渲染成 OpenAI 兼容的错误体，便于使用方客户端识别；
管理面（/api/admin/*）复用同一套 HTTP 状态与 code，只是文案面向本机操作者。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    # 协议面：鉴权与授权
    INVALID_API_KEY = "INVALID_API_KEY"
    KEY_EXPIRED = "KEY_EXPIRED"
    MODEL_NOT_ALLOWED = "MODEL_NOT_ALLOWED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    RATE_LIMITED = "RATE_LIMITED"
    # 协议面：上游与路由
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    NO_CHANNEL_AVAILABLE = "NO_CHANNEL_AVAILABLE"
    # 通用
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNAUTHORIZED = "UNAUTHORIZED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"


# 对外错误码 -> 默认 HTTP 状态
DEFAULT_STATUS: dict[str, int] = {
    ErrorCode.INVALID_API_KEY: 401,
    ErrorCode.KEY_EXPIRED: 403,
    ErrorCode.MODEL_NOT_ALLOWED: 403,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.UPSTREAM_ERROR: 502,
    ErrorCode.UPSTREAM_TIMEOUT: 504,
    ErrorCode.NO_CHANNEL_AVAILABLE: 503,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.CLIENT_DISCONNECTED: 499,
}

# 路由层判定为「可换渠道重试」的错误码白名单。
# 鉴权 / 参数类错误绝不重试，避免放大上游限流（见方案 13 节）。
RETRYABLE_CODES = frozenset(
    {
        ErrorCode.UPSTREAM_ERROR,
        ErrorCode.UPSTREAM_TIMEOUT,
        ErrorCode.NO_CHANNEL_AVAILABLE,
    }
)

# 上游返回这些 HTTP 状态时，视为「可换渠道重试」
RETRYABLE_UPSTREAM_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524})


class RelayError(Exception):
    """网关内部统一异常。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        param: str | None = None,
        retryable: bool | None = None,
        details: Any = None,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status if status is not None else DEFAULT_STATUS.get(code, 500)
        self.param = param
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable
        self.details = details
        # 便于把错误响应和 usage_logs 里的明细对上号（出错请求也会留痕）
        self.request_id = request_id

    def to_openai_body(self) -> dict[str, Any]:
        err: dict[str, Any] = {
            "message": self.message,
            "type": _openai_error_type(self.status),
            "code": self.code,
            "param": self.param,
        }
        if self.details is not None:
            err["details"] = self.details
        return {"error": err}

    def to_admin_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.details is not None:
            body["details"] = self.details
        return body

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<RelayError {self.status} {self.code}: {self.message}>"


def _openai_error_type(status: int) -> str:
    if status == 401:
        return "invalid_request_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if status >= 500:
        return "upstream_error"
    return "invalid_request_error"


def bad_request(message: str, *, param: str | None = None) -> RelayError:
    return RelayError(ErrorCode.BAD_REQUEST, message, param=param)


def not_found(message: str) -> RelayError:
    return RelayError(ErrorCode.NOT_FOUND, message)


def unauthorized(message: str = "无效的管理凭据") -> RelayError:
    return RelayError(ErrorCode.UNAUTHORIZED, message)


def conflict(message: str) -> RelayError:
    return RelayError(ErrorCode.CONFLICT, message)
