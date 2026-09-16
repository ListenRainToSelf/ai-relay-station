"""小米 MiMo 适配器。

MiMo 对外是 OpenAI 兼容的对话接口，但它的音频模型**不在** OpenAI 那些音频端点上
（`/v1/audio/speech`、`/v1/audio/transcriptions` 都是 404）。实测得到的真实用法：

* **语音合成（TTS）**：走 `/v1/chat/completions`，把要合成的文本放在 **assistant** 消息里，
  返回值在 `choices[0].message.audio.data`（base64 WAV，24kHz 单声道 16bit）。
  音色只能用它给的这批：mimo_default / 冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean。
  `mimo-v2.5-tts-voicedesign` 需要在 user 消息里写音色描述；
  `mimo-v2.5-tts-voiceclone` 需要传参考音频。
* **语音识别（ASR）**：也走 `/v1/chat/completions`，把音频放进 OpenAI 风格的
  `input_audio` 内容块，转写文本在 `message.content` 里。约束：**只能有这一个块**，
  不能带 text 内容块、也不能带多个音频块（上游提示词由它自己注入）；`language` 可作为顶层字段传。

所以这个适配器的作用就是把标准的 OpenAI 音频端点「翻译」成上面这套 chat 形态，
让客户端不用关心上游的实现差异。
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ..errors import ErrorCode, RelayError
from .base import (
    CAP_AUDIO_IN,
    CAP_AUDIO_OUT,
    CAP_CHAT,
    CAP_SPEECH,
    CAP_TRANSCRIPTION,
    CAP_VISION,
    UNIT_CHARACTER,
    UNIT_SECOND,
    ChatRequest,
    UpstreamCall,
    Usage,
    audio_format_of,
    bearer,
    join_url,
    strip_internal_fields,
)
from .media import MediaResult, SpeechRequest, TranscriptionRequest, billing_seconds
from .openai import OpenAIAdapter

CHAT_SUFFIX = "v1/chat/completions"

# 实测可用的音色（上游在参数错误时会把清单原样告诉你）
MIMO_VOICES: tuple[str, ...] = (
    "mimo_default", "冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean",
)

# 常见 OpenAI 音色名 → MiMo 音色（让 OpenAI 客户端也能直接跑）
MIMO_VOICE_ALIASES = {
    "alloy": "mimo_default",
    "echo": "Milo",
    "fable": "Dean",
    "onyx": "白桦",
    "nova": "冰糖",
    "shimmer": "茉莉",
    "coral": "苏打",
    "sage": "Mia",
    "ash": "白桦",
    "ballad": "茉莉",
    "verse": "Dean",
    "marin": "Chloe",
    "cedar": "Milo",
}

# 音色设计/克隆模型需要额外的输入，单独标注出来
VOICE_DESIGN_MODELS = ("mimo-v2.5-tts-voicedesign",)
VOICE_CLONE_MODELS = ("mimo-v2.5-tts-voiceclone",)


class XiaomiMiMoAdapter(OpenAIAdapter):
    provider_type = "xiaomi-mimo"
    label = "小米 MiMo"
    default_base_url = "https://api.xiaomimimo.com/v1"
    capabilities = (CAP_CHAT, CAP_VISION, CAP_AUDIO_IN, CAP_AUDIO_OUT, CAP_SPEECH, CAP_TRANSCRIPTION)
    has_model_list = True
    voices = MIMO_VOICES
    voice_aliases = MIMO_VOICE_ALIASES

    # ------------------------------------------------------------------ TTS
    def build_speech_call(
        self, request: SpeechRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        voice = self.translate_voice(request.voice)
        text = (request.input or "").strip()
        if not text:
            raise RelayError(ErrorCode.BAD_REQUEST, "语音合成的文本不能为空", param="input")

        messages: list[dict[str, Any]] = []
        if upstream_model in VOICE_DESIGN_MODELS:
            # 音色设计：user 消息给音色描述，assistant 消息放要合成的文本
            messages.append({"role": "user", "content": request.instructions or "自然、清晰的中文女声"})
        elif upstream_model in VOICE_CLONE_MODELS:
            raise RelayError(
                ErrorCode.BAD_REQUEST,
                "音色克隆需要参考音频，当前端点没有音频输入字段；"
                "请改用 channel 的 extra_body 传参考音频，或换用普通 TTS 模型",
                param="model",
            )
        messages.append({"role": "assistant", "content": text})

        body: dict[str, Any] = {"model": upstream_model, "messages": messages, "audio": {"voice": voice}}
        fmt = (request.response_format or "wav").lower()
        if fmt:
            body["audio"]["format"] = fmt
        if request.speed is not None:
            body["speed"] = request.speed
        # 语音合成一次给完，不走上游流式（客户端拿到的是完整音频文件）
        body.update({k: v for k, v in (self.extra_body or {}).items() if not k.startswith("_")})
        return UpstreamCall(
            "POST", join_url(self.base_url, CHAT_SUFFIX), self._headers(bearer(self.api_key)), body
        )

    def normalize_speech(
        self, response_bytes: bytes, content_type: str, request: SpeechRequest
    ) -> MediaResult:
        try:
            payload = json.loads(response_bytes.decode("utf-8", "replace"))
        except ValueError:
            raise RelayError(
                ErrorCode.UPSTREAM_ERROR, "语音合成返回的不是 JSON", details={"preview": response_bytes[:200].decode("utf-8", "replace")}
            ) from None
        message = ((payload.get("choices") or [{}])[0].get("message") or {})
        audio = message.get("audio") or {}
        encoded = str(audio.get("data") or "")
        if not encoded:
            raise RelayError(
                ErrorCode.UPSTREAM_ERROR,
                "上游没有返回音频数据（该模型可能不是语音合成模型）",
                details={"model": payload.get("model") or request.model},
            )
        try:
            raw = base64.b64decode(encoded)
        except Exception as exc:  # noqa: BLE001
            raise RelayError(ErrorCode.UPSTREAM_ERROR, f"音频 base64 解码失败：{exc}") from None
        container, guessed = self._audio_container_of(raw, str(audio.get("format") or ""))
        usage = self.extract_usage(payload) or Usage()
        return MediaResult(
            kind="speech",
            body=raw,
            content_type=guessed,
            usage=usage,
            units=request.characters,
            unit_kind=UNIT_CHARACTER,
            model_echo=str(payload.get("model") or request.model),
            note=f"音色 {self.translate_voice(request.voice)} · 上游返回 {container}",
        )

    @staticmethod
    def _audio_container_of(raw: bytes, declared: str) -> tuple[str, str]:
        from .base import detect_audio_container

        container, guessed = detect_audio_container(raw)
        if declared in ("mp3", "wav") and container != declared:
            # 上游说 mp3 但字节头是 WAV（或反之）：以字节为准，另附提示
            return container, guessed
        return container, guessed

    # ------------------------------------------------------------------ ASR
    def build_transcription_call(
        self, request: TranscriptionRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        if not request.content:
            raise RelayError(ErrorCode.BAD_REQUEST, "上传的音频内容为空", param="file")
        fmt = audio_format_of(request.filename, request.content_type)
        encoded = base64.b64encode(request.content).decode("ascii")
        # 注意：MiMo 的 ASR **不接受** text 内容块（上游会回 "must not include text parts"），
        # 也不接受多个音频块；提示词由上游自己注入。所以这里只放一个 input_audio。
        body: dict[str, Any] = {
            "model": upstream_model,
            "messages": [
                {"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": encoded, "format": fmt}},
                ]}
            ],
        }
        if request.language:
            body["language"] = request.language
        body.update({k: v for k, v in (self.extra_body or {}).items() if not k.startswith("_")})
        return UpstreamCall(
            "POST", join_url(self.base_url, CHAT_SUFFIX), self._headers(bearer(self.api_key)), body
        )

    def normalize_transcription(
        self, payload: dict[str, Any], request: TranscriptionRequest
    ) -> MediaResult:
        message = ((payload.get("choices") or [{}])[0].get("message") or {})
        text = str(message.get("content") or "").strip()
        seconds = request.audio_seconds
        usage = self.extract_usage(payload) or Usage()
        if (request.response_format or "json") in ("text", "srt", "vtt"):
            return MediaResult(
                kind="transcription", body=text.encode("utf-8"),
                content_type="text/plain; charset=utf-8", usage=usage,
                units=billing_seconds(seconds), unit_kind=UNIT_SECOND, model_echo=request.model,
            )
        return MediaResult(
            kind="transcription",
            payload={"text": text},
            usage=usage,
            units=billing_seconds(seconds),
            unit_kind=UNIT_SECOND,
            model_echo=str(payload.get("model") or request.model),
            note=f"{fmt_seconds(seconds)}音频".replace("None", ""),
        )

    # ------------------------------------------------------------------ 对话
    def build_chat_call(
        self, request: ChatRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        """对话基本透传；只把 `input_audio` 的格式补全（MiMo 需要显式 format）。"""
        body = strip_internal_fields(dict(request.raw))
        body["model"] = upstream_model
        for message in body.get("messages") or []:
            content = (message or {}).get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "input_audio":
                    audio = part.setdefault("input_audio", {})
                    if isinstance(audio, dict) and not audio.get("format"):
                        audio["format"] = "wav"
        if request.stream:
            body["stream"] = True
            if request.wants_usage():
                body["stream_options"] = {"include_usage": True}
        body.update({k: v for k, v in (self.extra_body or {}).items() if not k.startswith("_")})
        return UpstreamCall(
            "POST", join_url(self.base_url, CHAT_SUFFIX), self._headers(bearer(self.api_key)), body
        )


def fmt_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s " if seconds else ""
