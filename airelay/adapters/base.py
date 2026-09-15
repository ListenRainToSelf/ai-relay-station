"""协议适配器基类与共享工具。

对外统一暴露 OpenAI 兼容协议，对内把 Chat Completions 的请求/响应
翻译成各家上游的方言。适配器只负责「翻译」，不负责路由、鉴权、计费——
那些在 services 层完成（方案 5 节的单一职责切分）。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

import httpx

from ..errors import ErrorCode, RelayError


# --------------------------------------------------------------------------- #
# URL 拼接
# --------------------------------------------------------------------------- #
_VERSION_SEGMENTS = ("v1beta", "v1")


def join_url(base: str, suffix: str) -> str:
    """拼接 base_url 与路径，自动去重 `/v1`、`/v1beta` 段。

    用户填的 base_url 可能是 `https://api.deepseek.com`、`.../v1`
    或 `.../v1beta`，三种写法都要能正确落到目标端点。
    """
    base = (base or "").strip()
    suffix = (suffix or "").lstrip("/")
    if not base:
        return suffix
    base = base.rstrip("/")
    for seg in _VERSION_SEGMENTS:
        if base.endswith("/" + seg) and (suffix == seg or suffix.startswith(seg + "/")):
            suffix = suffix[len(seg):].lstrip("/")
            break
    return f"{base}/{suffix}" if suffix else base


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# 用量与 token 估算
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    source: str = "estimated"  # upstream | estimated

    def finalize(self) -> "Usage":
        if not self.total_tokens:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self

    def to_openai(self) -> dict[str, int]:
        self.finalize()
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def estimate_tokens(text: str | None) -> int:
    """粗略 token 估算：CJK 字符按 1 token，其余按 4 字符 1 token。

    只在某些上游不返回 usage 时兜底（用于速度指标与报表口径对齐），
    正常情况下优先采用上游返回的真实 usage（方案 6.1）。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if "\u2e80" <= ch <= "\u9fff" or "\uac00" <= ch <= "\ud7af" or "\uff00" <= ch <= "\uffef":
            cjk += 1
        else:
            other += 1
    return cjk + max(0, math.ceil(other / 4))


def estimate_messages_tokens(messages: Iterable[dict[str, Any]]) -> int:
    total = 0
    for message in messages or []:
        total += 4  # 角色与分隔符开销
        total += estimate_tokens(_flatten_content(message.get("content")))
        for call in message.get("tool_calls") or []:
            fn = (call or {}).get("function") or {}
            total += estimate_tokens(fn.get("name")) + estimate_tokens(fn.get("arguments")) + 8
    return total


def _flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") in {"image_url", "image"}:
                    parts.append("[image]")
                elif "text" in item:
                    parts.append(str(item.get("text", "")))
        return " ".join(parts)
    return str(content)


# --------------------------------------------------------------------------- #
# SSE 编码
# --------------------------------------------------------------------------- #
def sse_event(payload: Any, *, event: str | None = None) -> bytes:
    """把 dict 编码成一帧 SSE；`[DONE]` 由调用方用 sse_done()。"""
    if isinstance(payload, (bytes, bytearray)):
        body = bytes(payload)
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    else:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix = f"event: {event}\n".encode("ascii") if event else b""
    return prefix + b"data: " + body + b"\n\n"


def sse_done() -> bytes:
    return b"data: [DONE]\n\n"


CHUNK_TEMPLATE_KEYS = ("id", "object", "created", "model")


def openai_chunk(
    *,
    chunk_id: str,
    model: str,
    created: int,
    delta: dict[str, Any] | None = None,
    index: int = 0,
    finish_reason: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    choice: dict[str, Any] = {
        "index": index,
        "delta": delta or {},
        "logprobs": None,
        "finish_reason": finish_reason,
    }
    chunk: dict[str, Any] = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [choice],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


# --------------------------------------------------------------------------- #
# 请求包装
# --------------------------------------------------------------------------- #
@dataclass
class ChatRequest:
    """OpenAI 形态的请求（内部统一表示）。"""

    model: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    stream: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> "ChatRequest":
        return cls(
            model=str(body.get("model") or ""),
            messages=list(body.get("messages") or []),
            stream=bool(body.get("stream")),
            raw=body,
        )

    def option(self, name: str, default: Any = None) -> Any:
        value = self.raw.get(name, default)
        return default if value is None else value

    def max_tokens(self, fallback: int) -> int:
        for key in ("max_tokens", "max_completion_tokens"):
            value = self.raw.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return fallback

    def stop_sequences(self) -> list[str]:
        stop = self.raw.get("stop")
        if isinstance(stop, str):
            return [stop]
        if isinstance(stop, list):
            return [str(x) for x in stop if isinstance(x, (str, int, float))]
        return []

    def wants_usage(self) -> bool:
        opts = self.raw.get("stream_options") or {}
        return bool(isinstance(opts, dict) and opts.get("include_usage"))


@dataclass
class UpstreamCall:
    """适配器吐出的「待发送请求」。"""

    method: str
    url: str
    headers: dict[str, str]
    json_body: dict[str, Any] | None = None
    params: dict[str, str] | None = None

    def to_request(self, timeout: dict[str, float] | None = None) -> httpx.Request:
        request = httpx.Request(
            self.method,
            self.url,
            headers=self.headers,
            json=self.json_body,
            params=self.params,
        )
        if timeout:
            request.extensions["timeout"] = dict(timeout)
        return request


# --------------------------------------------------------------------------- #
# 适配器基类
# --------------------------------------------------------------------------- #
class BaseAdapter:
    provider_type: str = "base"
    label: str = "基础"
    protocol: str = "openai"
    default_base_url: str = ""
    supports_balance: bool = False
    # 该协议是否要求显式 max_tokens
    requires_max_tokens: bool = False

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url or self.default_base_url
        self.extra_headers = extra_headers or {}
        self.extra_body = extra_body or {}

    # ---------------------------------------------------------------- 请求
    def build_chat_call(self, request: ChatRequest, upstream_model: str, *, defaults: dict[str, Any]) -> UpstreamCall:
        raise NotImplementedError

    def build_models_call(self) -> UpstreamCall | None:
        return None

    def normalize_models(self, payload: Any) -> list[dict[str, Any]]:
        return []

    # ---------------------------------------------------------------- 响应
    def normalize_response(self, payload: dict[str, Any], request: ChatRequest, upstream_model: str) -> dict[str, Any]:
        raise NotImplementedError

    def extract_usage(self, payload: dict[str, Any]) -> Usage | None:
        return None

    async def iter_stream(
        self, response: httpx.Response, request: ChatRequest, upstream_model: str
    ) -> AsyncIterator[dict[str, Any]]:
        """把上游流式响应翻译成 OpenAI 形态的增量 chunk（dict）。"""
        raise NotImplementedError
        yield  # pragma: no cover

    # ---------------------------------------------------------------- 余额
    def build_balance_call(self, *, balance_url: str = "", json_path: str = "") -> UpstreamCall | None:
        return None

    # ---------------------------------------------------------------- 错误
    def translate_error(self, status: int, body: bytes | None, text: str = "") -> RelayError:
        message = text
        if body:
            try:
                data = json.loads(body.decode("utf-8", "replace"))
                message = extract_error_message(data) or message
            except (ValueError, UnicodeDecodeError):
                message = body.decode("utf-8", "replace")[:500] or message
        if not message:
            message = f"上游返回 HTTP {status}"
        code = ErrorCode.UPSTREAM_ERROR
        relay_status = 502
        if status == 429:
            code = ErrorCode.RATE_LIMITED
            relay_status = 429
        elif status in (401, 403):
            code = ErrorCode.UPSTREAM_ERROR
            relay_status = 502
        elif status in (400, 404, 422):
            code = ErrorCode.UPSTREAM_ERROR
            relay_status = 502
        elif status in (408, 504):
            code = ErrorCode.UPSTREAM_TIMEOUT
            relay_status = 504
        elif status >= 500:
            relay_status = 502
        return RelayError(code, message, status=relay_status, details={"upstream_status": status})

    # ---------------------------------------------------------------- 工具
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        headers.update(self.extra_headers)
        if extra:
            headers.update(extra)
        return headers


def extract_error_message(data: Any) -> str:
    """从各家五花八门的错误体里挖出可读文案。"""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for key in ("message", "msg", "detail", "type"):
            if error.get(key):
                return str(error[key])
    for key in ("message", "msg", "error_description", "detail"):
        if data.get(key):
            return str(data[key])
    if isinstance(data.get("errors"), list) and data["errors"]:
        first = data["errors"][0]
        if isinstance(first, dict):
            return str(first.get("message") or first.get("msg") or first)
        return str(first)
    return ""


def merge_extra_body(body: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """渠道级 extra_body 浅合并到请求体（渠道配置可覆盖/补充字段）。"""
    if not extra:
        return body
    merged = dict(body)
    merged.update(extra)
    return merged


def strip_internal_fields(body: dict[str, Any]) -> dict[str, Any]:
    """剔除网关内部字段与流式控制字段，避免污染上游请求。"""
    return {
        key: value
        for key, value in body.items()
        if not key.startswith("_") and key not in {"stream_options"}
    }


class OpenAIPassthroughMixin:
    """OpenAI 兼容协议（OpenAI / DeepSeek / 各类兼容中转）。"""

    async def iter_stream(
        self, response: httpx.Response, request: ChatRequest, upstream_model: str
    ) -> AsyncIterator[dict[str, Any]]:
        async for line in _iter_sse_data(response):
            if line == "[DONE]":
                return
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict):
                yield payload


async def _iter_sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """逐行读取 SSE 的 data: 载荷（忽略注释与 event 行）。"""
    async for raw_line in response.aiter_lines():
        if raw_line is None:
            continue
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[5:].strip()
        elif line.startswith("{"):
            # 少数上游不带 data: 前缀，直接吐 JSON 行
            yield line
