"""本地 AI 中转站 —— 托盘常驻式大模型 API 聚合中转网关。

对外暴露 OpenAI 兼容协议，对内把多家上游（OpenAI / Anthropic / Gemini）
归一化为统一渠道，并提供平台式密钥、用量统计、实时会话、余额查询等能力。
支持 Windows 桌面（托盘 + WebView）与 Linux NAS 无头两种部署形态。
"""

from .version import __version__

__all__ = ["__version__"]
