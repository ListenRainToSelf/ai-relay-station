"""多模态与「非对话」能力的公共定义。

网关原来只有对话（chat）一种能力，现在还要覆盖：
  * 语音合成 TTS      → `POST /v1/audio/speech`
  * 语音识别 ASR      → `POST /v1/audio/transcriptions`
  * 图片生成          → `POST /v1/images/generations`
  * 多模态输入输出    → 对话里带图片/音频，或对话返回图片/音频

这里定义**能力标识**与**媒体请求/结果**的数据结构，适配器只负责把统一结构
翻译成各家方言。能力标识同时用于路由：一个渠道只服务它声明具备的能力，
避免把 TTS 请求发给只会对话的渠道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import (
    ALL_CAPABILITIES,
    normalize_capabilities,
    CAPABILITY_LABELS,
    CAP_AUDIO_IN,
    CAP_AUDIO_OUT,
    CAP_CHAT,
    CAP_IMAGES,
    CAP_SPEECH,
    CAP_TRANSCRIPTION,
    CAP_VISION,
    UNIT_CHARACTER,
    UNIT_IMAGE,
    UNIT_LABELS,
    UNIT_NONE,
    UNIT_SECOND,
    Usage,
)

ENDPOINT_CAPABILITY: dict[str, str] = {
    "chat": CAP_CHAT,
    "speech": CAP_SPEECH,
    "transcription": CAP_TRANSCRIPTION,
    "images": CAP_IMAGES,
}


# --------------------------------------------------------------------------- #
# 媒体请求
# --------------------------------------------------------------------------- #
@dataclass
class SpeechRequest:
    model: str
    input: str
    voice: str = ""
    response_format: str = "wav"
    speed: float | None = None
    instructions: str = ""          # 有些厂商用「音色描述」
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> "SpeechRequest":
        return cls(
            model=str(body.get("model") or ""),
            input=str(body.get("input") or ""),
            voice=str(body.get("voice") or ""),
            response_format=str(body.get("response_format") or "wav").lower(),
            speed=body.get("speed"),
            instructions=str(body.get("instructions") or ""),
            raw=dict(body),
        )

    @property
    def characters(self) -> int:
        return len(self.input)


@dataclass
class TranscriptionRequest:
    model: str
    filename: str
    content: bytes
    content_type: str = ""
    language: str = ""
    prompt: str = ""
    response_format: str = "json"
    temperature: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def audio_seconds(self) -> float:
        """能解析出时长就返回秒数（用于按秒计费），否则 0。"""
        return probe_audio_seconds(self.content, self.filename, self.content_type)


@dataclass
class ImageRequest:
    model: str
    prompt: str
    n: int = 1
    size: str = ""
    quality: str = ""
    style: str = ""
    response_format: str = "b64_json"
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> "ImageRequest":
        try:
            count = int(body.get("n") or 1)
        except (TypeError, ValueError):
            count = 1
        return cls(
            model=str(body.get("model") or ""),
            prompt=str(body.get("prompt") or ""),
            n=max(1, min(count, 10)),
            size=str(body.get("size") or ""),
            quality=str(body.get("quality") or ""),
            style=str(body.get("style") or ""),
            response_format=str(body.get("response_format") or "b64_json").lower(),
            raw=dict(body),
        )


@dataclass
class MediaResult:
    """适配器归一化后的结果（网关直接据此回包）。"""

    kind: str                                   # speech | transcription | images
    payload: dict[str, Any] | None = None       # JSON 响应
    body: bytes = b""                           # 二进制响应（语音合成）
    content_type: str = "application/json"
    usage: Usage = field(default_factory=Usage)
    units: int = 0
    unit_kind: str = UNIT_NONE
    model_echo: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    note: str = ""
    # 网关补上的追踪信息，用于响应头
    request_id: str = ""
    channel_id: str = ""
    channel_name: str = ""
    provider_type: str = ""


# --------------------------------------------------------------------------- #
def probe_audio_seconds(content: bytes, filename: str = "", content_type: str = "") -> float:
    """尽量算出音频时长：WAV 直接读头，MP3 用帧头估算，其它返回 0。"""
    if not content:
        return 0.0
    lowered = (filename or "").lower()
    if content[:4] == b"RIFF" and content[8:12] == b"WAVE":
        try:
            import io
            import wave

            with wave.open(io.BytesIO(content)) as handle:
                rate = handle.getframerate() or 1
                return round(handle.getnframes() / float(rate), 3)
        except Exception:  # noqa: BLE001
            return 0.0
    if content[:3] == b"ID3" or content[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        try:
            return round(_estimate_mp3_seconds(content), 3)
        except Exception:  # noqa: BLE001
            return 0.0
    if "wav" in lowered or "wav" in (content_type or ""):
        return 0.0
    return 0.0


def _estimate_mp3_seconds(content: bytes) -> float:
    """按 128kbps 粗估 MP3 时长（够用来计费口径，不追求精确）。"""
    start = 0
    if content[:3] == b"ID3" and len(content) > 10:
        size = (content[6] << 21) | (content[7] << 14) | (content[8] << 7) | content[9]
        start = 10 + size
    audio_bytes = max(0, len(content) - start)
    return audio_bytes * 8 / 128_000.0


def billing_seconds(seconds: float) -> int:
    """计费秒数：不足一秒按一秒算（音频计费的通行口径）。"""
    try:
        value = float(seconds or 0)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return int(value) if value == int(value) else int(value) + 1


def pcm_to_wav(pcm: bytes, *, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    """把裸 PCM 包成 WAV。

    Gemini 的 TTS 返回 `audio/L16;codec=pcm;rate=24000` 的裸 PCM，而 OpenAI 的
    `/v1/audio/speech` 约定回音频文件——所以这里补一个 WAV 头，客户端才放得出来。
    """
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def parse_pcm_mime(mime_type: str) -> tuple[int, int]:
    """从 `audio/L16;codec=pcm;rate=24000` 里取出 (采样率, 位宽字节数)。"""
    import re as _re

    rate = 24000
    width = 2
    for part in (mime_type or "").split(";"):
        item = part.strip().lower()
        if item.startswith("rate="):
            digits = _re.sub(r"\D", "", item)
            if digits:
                rate = int(digits)
        elif item.startswith("audio/l"):
            digits = _re.sub(r"\D", "", item)
            if digits:
                width = max(1, int(digits) // 8)
    return rate, width


def wav_header_info(content: bytes) -> dict[str, Any]:
    """读 WAV 头，给控制台展示采样率/时长用。"""
    if content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        return {}
    try:
        import io
        import wave

        with wave.open(io.BytesIO(content)) as handle:
            return {
                "channels": handle.getnchannels(),
                "sample_width": handle.getsampwidth(),
                "frame_rate": handle.getframerate(),
                "frames": handle.getnframes(),
                "seconds": round(handle.getnframes() / float(handle.getframerate() or 1), 3),
            }
    except Exception:  # noqa: BLE001
        return {}


__all__ = [
    "ALL_CAPABILITIES",
    "CAPABILITY_LABELS",
    "CAP_AUDIO_IN",
    "CAP_AUDIO_OUT",
    "CAP_CHAT",
    "CAP_IMAGES",
    "CAP_SPEECH",
    "CAP_TRANSCRIPTION",
    "CAP_VISION",
    "ENDPOINT_CAPABILITY",
    "ImageRequest",
    "MediaResult",
    "SpeechRequest",
    "TranscriptionRequest",
    "UNIT_CHARACTER",
    "UNIT_IMAGE",
    "UNIT_LABELS",
    "UNIT_NONE",
    "UNIT_SECOND",
    "normalize_capabilities",
    "probe_audio_seconds",
    "billing_seconds",
    "pcm_to_wav",
    "parse_pcm_mime",
    "wav_header_info",
]
