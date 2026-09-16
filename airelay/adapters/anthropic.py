"""Anthropic Claude 适配器。

上游协议：`POST /v1/messages`，鉴权用 `x-api-key` + `anthropic-version`，
流式使用具名事件（message_start / content_block_delta / message_delta / message_stop）。
本适配器负责：OpenAI 消息数组 ⇄ Anthropic 内容块数组、具名流事件 → OpenAI SSE chunk。

注意：字段名以 Anthropic 官方 Messages API 为准（system / max_tokens / stop_sequences /
input_schema），落地前仍建议对照最新官方文档逐字段复核（方案 4.2 的 [To be confirmed]）。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..adapters.base import (
    CAP_CHAT,
    CAP_VISION,
    BaseAdapter,
    ChatRequest,
    UpstreamCall,
    Usage,
    join_url,
    openai_chunk,
)
from ..errors import ErrorCode, RelayError
from ..timeutil import utcnow

MESSAGES_SUFFIX = "v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODELS_SUFFIX = "v1/models"

STOP_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "stop",
}


class AnthropicAdapter(BaseAdapter):
    provider_type = "anthropic"
    label = "Anthropic Claude"
    protocol = "anthropic"
    default_base_url = "https://api.anthropic.com"
    supports_balance = False
    requires_max_tokens = True
    has_model_list = True
    # Claude 只做对话（能看图，但没有音频输入/输出，也没有图片生成）
    capabilities = (CAP_CHAT, CAP_VISION)

    # ------------------------------------------------------------------ 请求
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        headers.update(self.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    def build_chat_call(
        self, request: ChatRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        system_text, messages = convert_messages(request.messages)
        body: dict[str, Any] = {
            "model": upstream_model,
            "messages": messages,
            "max_tokens": request.max_tokens(int(defaults.get("max_tokens") or 4096)),
        }
        if system_text:
            body["system"] = system_text
        if request.stream:
            body["stream"] = True

        _copy_scalar(request, body, "temperature", "temperature")
        _copy_scalar(request, body, "top_p", "top_p")
        _copy_scalar(request, body, "metadata", "metadata")
        stop = request.stop_sequences()
        if stop:
            body["stop_sequences"] = stop
        tools = convert_tools(request.raw.get("tools"))
        if tools:
            body["tools"] = tools
        tool_choice = convert_tool_choice(request.raw.get("tool_choice"))
        if tool_choice:
            body["tool_choice"] = tool_choice
        thinking = request.raw.get("thinking") or request.raw.get("reasoning_effort")
        if isinstance(thinking, dict):
            body["thinking"] = thinking

        override = merge_extra(self.extra_body)
        body.update(override)
        return UpstreamCall("POST", join_url(self.base_url, MESSAGES_SUFFIX), self._headers(), body)

    def build_models_call(self) -> UpstreamCall | None:
        return UpstreamCall("GET", join_url(self.base_url, MODELS_SUFFIX), self._headers())

    def normalize_models(self, payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        return [
            {
                "id": str(item.get("id")),
                "owned_by": str(item.get("display_name") or "anthropic"),
                "created": item.get("created_at"),
            }
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]

    # ------------------------------------------------------------------ 响应
    def normalize_response(
        self, payload: dict[str, Any], request: ChatRequest, upstream_model: str
    ) -> dict[str, Any]:
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in payload.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text") or ""))
            elif btype == "thinking":
                reasoning_parts.append(str(block.get("thinking") or ""))
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or "toolu_0",
                        "type": "function",
                        "function": {
                            "name": block.get("name") or "tool",
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts) or None,
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = self.extract_usage(payload)
        result: dict[str, Any] = {
            "id": payload.get("id") or "chatcmpl-anthropic",
            "object": "chat.completion",
            "created": int(utcnow().timestamp()),
            "model": request.model or upstream_model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "logprobs": None,
                    "finish_reason": STOP_REASON_MAP.get(str(payload.get("stop_reason")), "stop"),
                }
            ],
        }
        if usage is not None:
            result["usage"] = usage.to_openai()
        return result

    def extract_usage(self, payload: dict[str, Any]) -> Usage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt = _int(usage.get("input_tokens"))
        completion = _int(usage.get("output_tokens"))
        cache_read = _int(usage.get("cache_read_input_tokens"))
        if cache_read:
            prompt += cache_read
        if not (prompt or completion):
            return None
        return Usage(prompt, completion, prompt + completion, source="upstream")

    # ------------------------------------------------------------------ 流式
    async def iter_stream(
        self, response: httpx.Response, request: ChatRequest, upstream_model: str
    ) -> AsyncIterator[dict[str, Any]]:
        model_name = request.model or upstream_model
        chunk_id = "chatcmpl-anthropic"
        created = int(utcnow().timestamp())
        prompt_tokens = 0
        completion_tokens = 0
        stop_reason: str | None = None
        tool_index = -1
        tool_started: dict[int, int] = {}
        started = False

        def chunk(delta: dict[str, Any] | None = None, finish: str | None = None, usage: dict | None = None):
            return openai_chunk(
                chunk_id=chunk_id,
                model=model_name,
                created=created,
                delta=delta,
                finish_reason=finish,
                usage=usage,
            )

        async for event, data in iter_named_sse(response):
            if event == "message_start":
                message = data.get("message") or {}
                if message.get("id"):
                    chunk_id = str(message["id"])
                usage_in = (message.get("usage") or {})
                prompt_tokens = _int(usage_in.get("input_tokens")) + _int(
                    usage_in.get("cache_read_input_tokens")
                )
                if not started:
                    started = True
                    yield chunk({"role": "assistant", "content": ""})
            elif event == "content_block_start":
                block = data.get("content_block") or {}
                if block.get("type") == "tool_use":
                    tool_index += 1
                    tool_started[_int(data.get("index"))] = tool_index
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": block.get("id") or f"toolu_{tool_index}",
                                    "type": "function",
                                    "function": {
                                        "name": block.get("name") or "tool",
                                        "arguments": "",
                                    },
                                }
                            ]
                        }
                    )
                elif block.get("type") == "thinking" and not started:
                    started = True
                    yield chunk({"role": "assistant", "content": ""})
            elif event == "content_block_delta":
                delta = data.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta" and delta.get("text"):
                    yield chunk({"content": str(delta["text"])})
                elif dtype == "thinking_delta" and delta.get("thinking"):
                    yield chunk({"reasoning_content": str(delta["thinking"])})
                elif dtype == "input_json_delta":
                    idx = tool_started.get(_int(data.get("index")), max(tool_index, 0))
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "function": {"arguments": str(delta.get("partial_json") or "")},
                                }
                            ]
                        }
                    )
            elif event == "message_delta":
                delta = data.get("delta") or {}
                if delta.get("stop_reason"):
                    stop_reason = str(delta["stop_reason"])
                usage_delta = data.get("usage") or {}
                if usage_delta.get("output_tokens") is not None:
                    completion_tokens = _int(usage_delta.get("output_tokens"))
                if usage_delta.get("input_tokens") is not None:
                    prompt_tokens = prompt_tokens or _int(usage_delta.get("input_tokens"))
            elif event == "error":
                message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
                raise RelayError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"上游流式错误：{message or '未知错误'}",
                    status=502,
                    details={"upstream_event": data},
                )

        usage_payload = None
        if prompt_tokens or completion_tokens:
            usage_payload = Usage(prompt_tokens, completion_tokens, prompt_tokens + completion_tokens).to_openai()
        yield chunk({}, finish=STOP_REASON_MAP.get(stop_reason or "end_turn", "stop"), usage=usage_payload)

    # ------------------------------------------------------------------ 余额
    def build_balance_call(self, *, balance_url: str = "", json_path: str = "") -> UpstreamCall | None:
        if not balance_url.strip():
            return None
        return UpstreamCall("GET", balance_url.strip(), self._headers())

    def normalize_balance(self, payload: dict[str, Any], json_path: str = "") -> dict[str, Any]:
        from .openai import _dig, _float

        total = _float(_dig(payload, json_path)) if json_path else 0.0
        return {
            "supported": True,
            "is_available": True,
            "currency": str(payload.get("currency", "")) if isinstance(payload, dict) else "",
            "total": total,
            "granted": 0.0,
            "topped_up": 0.0,
            "raw": payload,
        }


# --------------------------------------------------------------------------- #
# 消息与工具的双向转换
# --------------------------------------------------------------------------- #
def convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system 文本, Anthropic messages)。"""
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        if role in ("system", "developer"):
            text = _content_to_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": _content_to_text(message.get("content")),
                        }
                    ],
                }
            )
            continue

        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = _content_to_text(message.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for position, call in enumerate(message.get("tool_calls") or []):
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or f"toolu_{position}"),
                        "name": str(fn.get("name") or "tool"),
                        "input": _parse_json_object(fn.get("arguments")),
                    }
                )
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            converted.append({"role": "assistant", "content": blocks})
            continue

        blocks = _content_to_blocks(message.get("content"))
        converted.append({"role": "user", "content": blocks or [{"type": "text", "text": ""}]})

    if not converted:
        converted = [{"role": "user", "content": [{"type": "text", "text": ""}]}]
    # Anthropic 要求首条为 user，且相邻同角色消息需要合并
    if converted[0]["role"] != "user":
        converted.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return "\n\n".join(system_parts), _merge_adjacent(converted)


def _merge_adjacent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1]["content"] = list(merged[-1]["content"]) + list(message["content"])
        else:
            merged.append({"role": message["role"], "content": list(message["content"])})
    return merged


def _content_to_text(content: Any) -> str:
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
                if item.get("type") == "text" or "text" in item:
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _content_to_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]

    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text" or ("text" in item and itype is None):
            blocks.append({"type": "text", "text": str(item.get("text") or "")})
        elif itype == "image_url":
            block = _image_block(item.get("image_url"))
            if block:
                blocks.append(block)
        elif itype == "image":
            block = _image_block(item)
            if block:
                blocks.append(block)
    return blocks


def _image_block(source: Any) -> dict[str, Any] | None:
    url = ""
    if isinstance(source, str):
        url = source
    elif isinstance(source, dict):
        url = str(source.get("url") or "")
        if source.get("type") == "base64" and source.get("data"):
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": str(source.get("media_type") or "image/png"),
                    "data": str(source["data"]),
                },
            }
    if not url:
        return None
    if url.startswith("data:"):
        media_type, _, data = url.partition(";base64,")
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type[5:] or "image/png",
                "data": data,
            },
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def convert_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        entry: dict[str, Any] = {"name": str(fn["name"])}
        if fn.get("description"):
            entry["description"] = str(fn["description"])
        schema = fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}}
        entry["input_schema"] = schema
        converted.append(entry)
    return converted


def convert_tool_choice(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        return {"type": {"auto": "auto", "required": "any", "any": "any"}.get(choice, "auto")}
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            name = (choice.get("function") or {}).get("name")
            return {"type": "tool", "name": str(name)} if name else {"type": "auto"}
        if choice.get("type") in {"auto", "any", "tool"}:
            return choice
    return None


def merge_extra(extra: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in (extra or {}).items() if not k.startswith("_")}


def _copy_scalar(request: ChatRequest, body: dict[str, Any], source: str, target: str) -> None:
    value = request.raw.get(source)
    if value is not None:
        body[target] = value


def _parse_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except ValueError:
        return {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def iter_named_sse(response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """解析具名 SSE：返回 (event, data)。

    Anthropic 每帧形如：
      event: content_block_delta
      data: {"type":"content_block_delta",...}
    """
    event = ""
    buffer: list[str] = []

    async for raw_line in response.aiter_lines():
        if raw_line is None:
            continue
        line = raw_line.rstrip("\r")
        if not line:
            if buffer:
                data = "\n".join(buffer)
                buffer = []
                payload = _try_json(data)
                if payload is not None:
                    yield event or str(payload.get("type") or "message"), payload
                event = ""
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            buffer.append(line[5:].strip())
        elif line.startswith("{"):
            buffer.append(line)

    if buffer:
        payload = _try_json("\n".join(buffer))
        if payload is not None:
            yield event or str(payload.get("type") or "message"), payload


def _try_json(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
