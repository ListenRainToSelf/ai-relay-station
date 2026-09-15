"""OpenAI 协议适配器（含 DeepSeek 等兼容上游）。

OpenAI / DeepSeek / 多数聚合中转都遵循 `/chat/completions`，因此主体是透传，
只做三件事：替换模型 id、按需开启含 usage 的流式、叠加渠道级 extra_body。
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import ErrorCode, RelayError
from .base import (
    BaseAdapter,
    ChatRequest,
    OpenAIPassthroughMixin,
    UpstreamCall,
    Usage,
    bearer,
    join_url,
    merge_extra_body,
    strip_internal_fields,
)

CHAT_SUFFIX = "v1/chat/completions"
MODELS_SUFFIX = "v1/models"


def _api_root(base_url: str) -> str:
    """去掉版本段，得到厂商 API 根地址（余额等非 /v1 端点挂在根上）。"""
    base = (base_url or "").rstrip("/")
    for seg in ("/v1beta", "/v1", "/beta", "/api"):
        if base.endswith(seg):
            return base[: -len(seg)]
    return base


class OpenAIAdapter(OpenAIPassthroughMixin, BaseAdapter):
    provider_type = "openai"
    label = "OpenAI 兼容"
    protocol = "openai"
    default_base_url = "https://api.openai.com/v1"
    supports_balance = False
    has_model_list = True

    def build_chat_call(
        self, request: ChatRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        body = strip_internal_fields(dict(request.raw))
        body["model"] = upstream_model
        if request.stream:
            body["stream"] = True
            # 只在客户端明确要 usage 时下发 stream_options：部分兼容上游不认这个字段
            if request.wants_usage():
                body["stream_options"] = {"include_usage": True}
        body = merge_extra_body(body, self.extra_body)
        return UpstreamCall(
            "POST",
            join_url(self.base_url, CHAT_SUFFIX),
            self._headers(bearer(self.api_key)),
            body,
        )

    def build_models_call(self) -> UpstreamCall | None:
        return UpstreamCall(
            "GET",
            join_url(self.base_url, MODELS_SUFFIX),
            self._headers(bearer(self.api_key)),
        )

    def normalize_models(self, payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return []
        models: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict) and item.get("id"):
                models.append(
                    {
                        "id": str(item["id"]),
                        "owned_by": str(item.get("owned_by") or self.provider_type),
                        "created": item.get("created"),
                    }
                )
            elif isinstance(item, str):
                models.append({"id": item, "owned_by": self.provider_type, "created": None})
        return models

    def normalize_response(
        self, payload: dict[str, Any], request: ChatRequest, upstream_model: str
    ) -> dict[str, Any]:
        result = dict(payload)
        result.setdefault("object", "chat.completion")
        # 回显客户端请求的模型名，隐藏上游真实 id
        result["model"] = request.model or upstream_model
        if not result.get("id"):
            result["id"] = "chatcmpl-" + str(abs(hash(json.dumps(payload, sort_keys=True))) % 10**13)
        usage = self.extract_usage(payload)
        if usage is not None:
            # 保留上游 usage 的附加字段（缓存命中、reasoning tokens 等），只补齐标准三项
            merged = dict(payload.get("usage") or {})
            merged.update(usage.to_openai())
            result["usage"] = merged
        return result

    def extract_usage(self, payload: dict[str, Any]) -> Usage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
        completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
        total = _int(usage.get("total_tokens")) or (prompt + completion)
        if not (prompt or completion or total):
            return None
        return Usage(prompt, completion, total, source="upstream")


class DeepSeekAdapter(OpenAIAdapter):
    """DeepSeek：OpenAI 兼容协议 + 官方余额查询接口。"""

    provider_type = "deepseek"
    label = "DeepSeek"
    default_base_url = "https://api.deepseek.com/v1"
    supports_balance = True

    def build_balance_call(self, *, balance_url: str = "", json_path: str = "") -> UpstreamCall | None:
        url = balance_url.strip() or join_url(_api_root(self.base_url), "user/balance")
        return UpstreamCall("GET", url, self._headers(bearer(self.api_key)))

    def normalize_balance(self, payload: dict[str, Any], json_path: str = "") -> dict[str, Any]:
        """把 DeepSeek 的 balance_infos[] 映射成本地统一余额结构。"""
        infos = payload.get("balance_infos")
        entry: dict[str, Any] = {}
        if isinstance(infos, list) and infos:
            # 优先取 CNY，其次第一条
            entry = next(
                (x for x in infos if isinstance(x, dict) and str(x.get("currency", "")).upper() == "CNY"),
                infos[0] if isinstance(infos[0], dict) else {},
            )
        total = _float(entry.get("total_balance"))
        granted = _float(entry.get("granted_balance"))
        topped = _float(entry.get("topped_up_balance"))
        if not entry and json_path:
            total = _float(_dig(payload, json_path))
        return {
            "supported": True,
            "is_available": bool(payload.get("is_available", True)),
            "currency": str(entry.get("currency") or "CNY"),
            "total": total,
            "granted": granted,
            "topped_up": topped,
            "raw": payload,
        }


def _dig(data: Any, path: str) -> Any:
    """按 `a.b.0.c` 形式取值，供自定义余额路径使用。"""
    current = data
    for part in str(path).split("."):
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class GenericOpenAIAdapter(OpenAIAdapter):
    """任意 OpenAI 兼容上游：base_url 必填，可配置自定义余额 URL。"""

    provider_type = "openai-compatible"
    label = "OpenAI 兼容（自定义）"
    default_base_url = ""
    supports_balance = True

    def build_balance_call(self, *, balance_url: str = "", json_path: str = "") -> UpstreamCall | None:
        if not balance_url.strip():
            return None
        return UpstreamCall("GET", balance_url.strip(), self._headers(bearer(self.api_key)))

    def normalize_balance(self, payload: dict[str, Any], json_path: str = "") -> dict[str, Any]:
        total = _float(_dig(payload, json_path)) if json_path else 0.0
        if not json_path:
            for key in ("total_balance", "balance", "total", "available"):
                if isinstance(payload, dict) and key in payload:
                    total = _float(payload[key])
                    break
        return {
            "supported": True,
            "is_available": True,
            "currency": str(payload.get("currency", "")) if isinstance(payload, dict) else "",
            "total": total,
            "granted": 0.0,
            "topped_up": 0.0,
            "raw": payload,
        }
