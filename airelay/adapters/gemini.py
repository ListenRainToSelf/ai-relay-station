"""Google Gemini 适配器。

上游协议是自有的 `generateContent` 结构：内容放在 `contents[].parts[]`，
系统提示走 `systemInstruction`，参数走 `generationConfig`，鉴权用 `x-goog-api-key`。
流式用 `:streamGenerateContent?alt=sse`，返回的仍是自家 chunk 结构。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator
from urllib.parse import quote

import httpx

from .base import BaseAdapter, ChatRequest, UpstreamCall, Usage, join_url, openai_chunk
from ..errors import ErrorCode, RelayError
from ..timeutil import utcnow

MODELS_SUFFIX = "v1beta/models"

FINISH_REASON_MAP = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
    "SPII": "content_filter",
    "MALFORMED_FUNCTION_CALL": "tool_calls",
    "OTHER": "stop",
}


class GeminiAdapter(BaseAdapter):
    provider_type = "gemini"
    label = "Google Gemini"
    protocol = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com"
    supports_balance = False
    requires_max_tokens = True
    has_model_list = True

    # ------------------------------------------------------------------ 请求
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-goog-api-key": self.api_key,
        }
        headers.update(self.extra_headers)
        if extra:
            headers.update(extra)
        return headers

    def _endpoint(self, upstream_model: str, method: str) -> str:
        model = upstream_model
        if model.startswith("models/"):
            model = model[len("models/"):]
        suffix = f"{MODELS_SUFFIX}/{quote(model, safe='')}:{method}"
        return join_url(self.base_url, suffix)

    def build_chat_call(
        self, request: ChatRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        system_text, contents = convert_contents(request.messages)
        body: dict[str, Any] = {"contents": contents}
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}

        config: dict[str, Any] = {
            "maxOutputTokens": request.max_tokens(int(defaults.get("max_tokens") or 4096))
        }
        _copy_scalar(request, config, "temperature", "temperature")
        _copy_scalar(request, config, "top_p", "topP")
        _copy_scalar(request, config, "top_k", "topK")
        if request.raw.get("presence_penalty") is not None:
            config["presencePenalty"] = request.raw.get("presence_penalty")
        if request.raw.get("frequency_penalty") is not None:
            config["frequencyPenalty"] = request.raw.get("frequency_penalty")
        stop = request.stop_sequences()
        if stop:
            config["stopSequences"] = stop[:5]
        response_format = request.raw.get("response_format")
        if isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}:
            config["responseMimeType"] = "application/json"
            schema = (response_format.get("json_schema") or {}).get("schema")
            if isinstance(schema, dict):
                config["responseSchema"] = schema
        seed = request.raw.get("seed")
        if seed is not None:
            config["seed"] = seed
        body["generationConfig"] = config

        declarations = convert_tools(request.raw.get("tools"))
        if declarations:
            body["tools"] = [{"functionDeclarations": declarations}]
            tool_config = convert_tool_choice(request.raw.get("tool_choice"))
            if tool_config:
                body["toolConfig"] = tool_config

        body.update({k: v for k, v in (self.extra_body or {}).items() if not k.startswith("_")})
        method = "streamGenerateContent" if request.stream else "generateContent"
        params = {"alt": "sse"} if request.stream else None
        return UpstreamCall("POST", self._endpoint(upstream_model, method), self._headers(), body, params)

    def build_models_call(self) -> UpstreamCall | None:
        return UpstreamCall("GET", join_url(self.base_url, MODELS_SUFFIX), self._headers())

    def normalize_models(self, payload: Any) -> list[dict[str, Any]]:
        items = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        models: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            if name.startswith("models/"):
                name = name[len("models/"):]
            models.append(
                {
                    "id": name,
                    "owned_by": str(item.get("displayName") or "google"),
                    "created": None,
                }
            )
        return models

    # ------------------------------------------------------------------ 响应
    def normalize_response(
        self, payload: dict[str, Any], request: ChatRequest, upstream_model: str
    ) -> dict[str, Any]:
        candidate = _first_candidate(payload)
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for part in (candidate.get("content") or {}).get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("text"):
                if part.get("thought"):
                    reasoning_parts.append(str(part["text"]))
                else:
                    text_parts.append(str(part["text"]))
            call = part.get("functionCall")
            if isinstance(call, dict):
                tool_calls.append(
                    {
                        "id": f"call_{call.get('name', 'tool')}_{len(tool_calls)}",
                        "type": "function",
                        "function": {
                            "name": str(call.get("name") or "tool"),
                            "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        },
                    }
                )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls

        finish = FINISH_REASON_MAP.get(str(candidate.get("finishReason")), "stop")
        if tool_calls and finish == "stop":
            finish = "tool_calls"

        result: dict[str, Any] = {
            "id": str(payload.get("responseId") or "chatcmpl-gemini"),
            "object": "chat.completion",
            "created": int(utcnow().timestamp()),
            "model": request.model or upstream_model,
            "choices": [
                {"index": 0, "message": message, "logprobs": None, "finish_reason": finish}
            ],
        }
        usage = self.extract_usage(payload)
        if usage is not None:
            result["usage"] = usage.to_openai()
        return result

    def extract_usage(self, payload: dict[str, Any]) -> Usage | None:
        meta = payload.get("usageMetadata")
        if not isinstance(meta, dict):
            return None
        prompt = _int(meta.get("promptTokenCount"))
        completion = _int(meta.get("candidatesTokenCount")) + _int(meta.get("thoughtsTokenCount"))
        total = _int(meta.get("totalTokenCount")) or (prompt + completion)
        if not (prompt or completion or total):
            return None
        return Usage(prompt, completion, total, source="upstream")

    # ------------------------------------------------------------------ 流式
    async def iter_stream(
        self, response: httpx.Response, request: ChatRequest, upstream_model: str
    ) -> AsyncIterator[dict[str, Any]]:
        model_name = request.model or upstream_model
        chunk_id = "chatcmpl-gemini"
        created = int(utcnow().timestamp())
        tool_index = -1
        finish_reason = "stop"
        usage_payload: dict[str, int] | None = None
        sent_role = False

        def chunk(delta: dict[str, Any] | None = None, finish: str | None = None, usage: dict | None = None):
            return openai_chunk(
                chunk_id=chunk_id,
                model=model_name,
                created=created,
                delta=delta,
                finish_reason=finish,
                usage=usage,
            )

        async for line in _iter_sse(response):
            payload = _try_json(line)
            if payload is None:
                continue
            if payload.get("responseId"):
                chunk_id = str(payload["responseId"])
            if isinstance(payload.get("error"), dict):
                raise RelayError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"上游流式错误：{payload['error'].get('message') or '未知错误'}",
                    status=502,
                    details={"upstream_event": payload},
                )
            usage = self.extract_usage(payload)
            if usage is not None:
                usage_payload = usage.to_openai()
            candidate = _first_candidate(payload)
            if not candidate:
                continue
            if not sent_role:
                sent_role = True
                yield chunk({"role": "assistant", "content": ""})
            for part in (candidate.get("content") or {}).get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("text"):
                    key = "reasoning_content" if part.get("thought") else "content"
                    yield chunk({key: str(part["text"])})
                call = part.get("functionCall")
                if isinstance(call, dict):
                    tool_index += 1
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "id": f"call_{call.get('name', 'tool')}_{tool_index}",
                                    "type": "function",
                                    "function": {
                                        "name": str(call.get("name") or "tool"),
                                        "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                                    },
                                }
                            ]
                        }
                    )
            if candidate.get("finishReason"):
                finish_reason = FINISH_REASON_MAP.get(str(candidate["finishReason"]), "stop")
                if tool_index >= 0 and finish_reason == "stop":
                    finish_reason = "tool_calls"

        yield chunk({}, finish=finish_reason, usage=usage_payload)

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
            "currency": "USD",
            "total": total,
            "granted": 0.0,
            "topped_up": 0.0,
            "raw": payload,
        }


# --------------------------------------------------------------------------- #
def convert_contents(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI messages → (system 文本, Gemini contents)。"""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}

    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        if role in ("system", "developer"):
            text = _text_of(message.get("content"))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            name = str(message.get("name") or tool_names.get(str(message.get("tool_call_id")), "function"))
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name,
                                "response": {"content": _text_of(message.get("content"))},
                            }
                        }
                    ],
                }
            )
            continue

        if role == "assistant":
            parts: list[dict[str, Any]] = []
            text = _text_of(message.get("content"))
            if text:
                parts.append({"text": text})
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                name = str(fn.get("name") or "tool")
                tool_names[str(call.get("id") or name)] = name
                parts.append(
                    {
                        "functionCall": {
                            "name": name,
                            "args": _parse_json_object(fn.get("arguments")),
                        }
                    }
                )
            if not parts:
                parts = [{"text": ""}]
            contents.append({"role": "model", "parts": parts})
            continue

        parts = _content_to_parts(message.get("content"))
        contents.append({"role": "user", "parts": parts or [{"text": ""}]})

    if not contents:
        contents = [{"role": "user", "parts": [{"text": ""}]}]
    return "\n\n".join(system_parts), contents


def _content_to_parts(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        return [{"text": str(content)}]
    parts: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            parts.append({"text": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "text" or ("text" in item and itype is None):
            parts.append({"text": str(item.get("text") or "")})
        elif itype in {"image_url", "image"}:
            inline = _inline_image(item.get("image_url") or item)
            if inline:
                parts.append(inline)
    return parts


def _inline_image(source: Any) -> dict[str, Any] | None:
    url = source if isinstance(source, str) else str((source or {}).get("url") or "")
    if not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(";base64,")
        return {"inlineData": {"mimeType": header[5:] or "image/png", "data": data}}
    return {"fileData": {"fileUri": url}}


def convert_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []
    declarations: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        entry: dict[str, Any] = {"name": str(fn["name"])}
        if fn.get("description"):
            entry["description"] = str(fn["description"])
        params = fn.get("parameters") or fn.get("input_schema")
        if isinstance(params, dict) and params.get("properties"):
            entry["parameters"] = params
        declarations.append(entry)
    return declarations


def convert_tool_choice(choice: Any) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, str):
        if choice in {"auto", "none"}:
            return {"functionCallingConfig": {"mode": "AUTO" if choice == "auto" else "NONE"}}
        if choice in {"required", "any"}:
            return {"functionCallingConfig": {"mode": "ANY"}}
        return None
    if isinstance(choice, dict) and choice.get("type") == "function":
        name = (choice.get("function") or {}).get("name")
        config = {"mode": "ANY"}
        if name:
            config["allowedFunctionNames"] = [str(name)]
        return {"functionCallingConfig": config}
    return None


def _first_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return {}


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("text") is not None:
                    parts.append(str(item["text"]))
                elif item.get("type") in {"image_url", "image"}:
                    parts.append("[image]")
        return "\n".join(p for p in parts if p)
    return str(content)


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


def _copy_scalar(request: ChatRequest, target: dict[str, Any], source: str, dest: str) -> None:
    value = request.raw.get(source)
    if value is not None:
        target[dest] = value


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def _iter_sse(response: httpx.Response) -> AsyncIterator[str]:
    async for raw_line in response.aiter_lines():
        if raw_line is None:
            continue
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            yield line[5:].strip()
        elif line.startswith("{"):
            yield line


def _try_json(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None
