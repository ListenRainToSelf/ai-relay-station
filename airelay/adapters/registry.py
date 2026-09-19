"""provider_type → 适配器 的注册表。

新增一家上游只需实现一个 BaseAdapter 子类并在此登记，
其余（渠道表单、协议下拉、余额支持标记）自动出现在控制台里。
"""

from __future__ import annotations

from typing import Any

from .anthropic import AnthropicAdapter
from .base import BaseAdapter
from .gemini import GeminiAdapter
from .base import CAPABILITY_LABELS
from .mimo import XiaomiMiMoAdapter
from .openai import DeepSeekAdapter, GenericOpenAIAdapter, OpenAIAdapter, ZhipuAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    OpenAIAdapter.provider_type: OpenAIAdapter,
    DeepSeekAdapter.provider_type: DeepSeekAdapter,
    ZhipuAdapter.provider_type: ZhipuAdapter,
    AnthropicAdapter.provider_type: AnthropicAdapter,
    GeminiAdapter.provider_type: GeminiAdapter,
    XiaomiMiMoAdapter.provider_type: XiaomiMiMoAdapter,
    GenericOpenAIAdapter.provider_type: GenericOpenAIAdapter,
}

# 兼容旧配置 / 用户手填的别名
PROVIDER_ALIASES = {
    "claude": "anthropic",
    "google": "gemini",
    "google-gemini": "gemini",
    "azure": "openai",
    "azure-openai": "openai",
    "openai_compatible": "openai-compatible",
    "openai_compat": "openai-compatible",
    "custom": "openai-compatible",
    "compatible": "openai-compatible",
    "ds": "deepseek",
    "glm": "zhipu",
    "zhipuai": "zhipu",
    "bigmodel": "zhipu",
    "mimo": "xiaomi-mimo",
    "xiaomi": "xiaomi-mimo",
    "xiaomimimo": "xiaomi-mimo",
}


def normalize_provider(provider_type: str | None) -> str:
    key = (provider_type or "").strip().lower()
    key = PROVIDER_ALIASES.get(key, key)
    return key if key in ADAPTERS else "openai-compatible"


def get_adapter_class(provider_type: str | None) -> type[BaseAdapter]:
    return ADAPTERS.get(normalize_provider(provider_type), GenericOpenAIAdapter)


def create_adapter(
    provider_type: str | None,
    *,
    api_key: str,
    base_url: str = "",
    extra_headers: dict[str, str] | None = None,
    extra_body: dict[str, Any] | None = None,
) -> BaseAdapter:
    adapter_class = get_adapter_class(provider_type)
    return adapter_class(
        api_key=api_key,
        base_url=base_url,
        extra_headers=extra_headers,
        extra_body=extra_body,
    )


def provider_metadata() -> list[dict[str, Any]]:
    """供控制台渲染渠道表单 / 模型试算。"""
    base = {
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "anthropic": "https://api.anthropic.com",
        "gemini": "https://generativelanguage.googleapis.com",
        "xiaomi-mimo": "https://api.xiaomimimo.com/v1",
    }
    items: list[dict[str, Any]] = []
    for key, cls in ADAPTERS.items():
        items.append(
            {
                "type": key,
                "label": cls.label,
                "protocol": cls.protocol,
                "default_base_url": base.get(key, cls.default_base_url),
                "supports_balance": cls.supports_balance,
                "requires_max_tokens": cls.requires_max_tokens,
                # 控制台用这两项渲染「能力」勾选与音色提示
                "capabilities": list(cls.capabilities),
                "capability_labels": [CAPABILITY_LABELS.get(c, c) for c in cls.capabilities],
                "voices": list(cls.voices),
            }
        )
    return items


__all__ = [
    "ADAPTERS",
    "BaseAdapter",
    "create_adapter",
    "get_adapter_class",
    "normalize_provider",
    "provider_metadata",
]
