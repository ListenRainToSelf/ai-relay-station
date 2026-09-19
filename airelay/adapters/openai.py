"""OpenAI 协议适配器（含 DeepSeek 等兼容上游）。

OpenAI / DeepSeek / 多数聚合中转都遵循 `/chat/completions`，因此主体是透传，
只做三件事：替换模型 id、按需开启含 usage 的流式、叠加渠道级 extra_body。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..errors import ErrorCode, RelayError
from .media import ImageRequest, MediaResult, SpeechRequest, TranscriptionRequest, billing_seconds
from .base import (
    CAP_AUDIO_IN,
    CAP_AUDIO_OUT,
    CAP_CHAT,
    CAP_IMAGES,
    CAP_SPEECH,
    CAP_TRANSCRIPTION,
    CAP_VISION,
    UNIT_CHARACTER,
    UNIT_IMAGE,
    UNIT_SECOND,
    BaseAdapter,
    audio_format_of,
    build_multipart,
    detect_audio_container,
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
    # OpenAI 兼容协议把这些端点都定义了；具体上游不一定实现，能力路由会兜住
    capabilities = (
        CAP_CHAT, CAP_VISION, CAP_AUDIO_IN, CAP_AUDIO_OUT,
        CAP_SPEECH, CAP_TRANSCRIPTION, CAP_IMAGES,
    )

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


    # ---------------------------------------------------------------- 语音合成
    def build_speech_call(
        self, request: SpeechRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        body: dict[str, Any] = {
            "model": upstream_model,
            "input": request.input,
            "voice": request.voice or "alloy",
            "response_format": request.response_format or "wav",
        }
        if request.speed is not None:
            body["speed"] = request.speed
        body.update({k: v for k, v in (self.extra_body or {}).items() if not k.startswith("_")})
        return UpstreamCall(
            "POST", join_url(self.base_url, "v1/audio/speech"), self._headers(bearer(self.api_key)), body
        )

    def normalize_speech(
        self, response_bytes: bytes, content_type: str, request: SpeechRequest
    ) -> MediaResult:
        container, guessed = detect_audio_container(response_bytes)
        return MediaResult(
            kind="speech",
            body=response_bytes,
            content_type=content_type if content_type and content_type != "application/json" else guessed,
            units=request.characters,
            unit_kind=UNIT_CHARACTER,
            model_echo=request.model,
            note=f"上游返回 {container}",
        )

    # ---------------------------------------------------------------- 语音识别
    def build_transcription_call(
        self, request: TranscriptionRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        fields = {"model": upstream_model}
        if request.language:
            fields["language"] = request.language
        if request.prompt:
            fields["prompt"] = request.prompt
        if request.response_format:
            fields["response_format"] = request.response_format
        if request.temperature is not None:
            fields["temperature"] = str(request.temperature)
        content, ctype = build_multipart(
            fields,
            [("file", request.filename or "audio.wav", request.content, request.content_type or "audio/wav")],
        )
        headers = self._headers(bearer(self.api_key))
        headers["Content-Type"] = ctype
        return UpstreamCall(
            "POST", join_url(self.base_url, "v1/audio/transcriptions"), headers, content=content
        )

    def normalize_transcription(
        self, payload: dict[str, Any], request: TranscriptionRequest
    ) -> MediaResult:
        text = str((payload or {}).get("text") or "")
        seconds = request.audio_seconds
        wants_text = (request.response_format or "json") in ("text", "srt", "vtt")
        if wants_text:
            return MediaResult(
                kind="transcription", body=text.encode("utf-8"), content_type="text/plain; charset=utf-8",
                usage=self.extract_usage(payload) or Usage(), units=billing_seconds(seconds),
                unit_kind=UNIT_SECOND, model_echo=request.model,
            )
        extra = {k: v for k, v in (payload or {}).items() if k != "text"}
        usage = self.extract_usage(payload)
        return MediaResult(
            kind="transcription",
            payload={"text": text, **extra},
            usage=usage or Usage(),
            units=billing_seconds(seconds),
            unit_kind=UNIT_SECOND,
            model_echo=request.model,
        )

    # ---------------------------------------------------------------- 图片生成
    def build_image_call(
        self, request: ImageRequest, upstream_model: str, *, defaults: dict[str, Any]
    ) -> UpstreamCall:
        body: dict[str, Any] = {
            "model": upstream_model,
            "prompt": request.prompt,
            "n": request.n,
        }
        for field_name in ("size", "quality", "style", "response_format"):
            value = getattr(request, field_name, "")
            if value:
                body[field_name] = value
        body.update({k: v for k, v in (self.extra_body or {}).items() if not k.startswith("_")})
        return UpstreamCall(
            "POST", join_url(self.base_url, "v1/images/generations"), self._headers(bearer(self.api_key)), body
        )

    def normalize_images(self, payload: dict[str, Any], request: ImageRequest) -> MediaResult:
        items = (payload or {}).get("data")
        images: list[dict[str, Any]] = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {}
            if item.get("b64_json"):
                entry["b64_json"] = item["b64_json"]
            if item.get("url"):
                entry["url"] = item["url"]
            if item.get("revised_prompt"):
                entry["revised_prompt"] = item["revised_prompt"]
            if entry:
                images.append(entry)
        usage = self.extract_usage(payload) if isinstance(payload, dict) else None
        return MediaResult(
            kind="images",
            payload={"created": (payload or {}).get("created") or int(time.time()), "data": images},
            usage=usage or Usage(),
            units=len(images) or request.n,
            unit_kind=UNIT_IMAGE,
            model_echo=request.model,
        )


class DeepSeekAdapter(OpenAIAdapter):
    """DeepSeek：OpenAI 兼容协议 + 官方余额查询接口。"""

    provider_type = "deepseek"
    label = "DeepSeek"
    default_base_url = "https://api.deepseek.com/v1"
    supports_balance = True
    # DeepSeek 只提供文本对话（其模型列表里没有音频/图片模型）
    capabilities = (CAP_CHAT,)

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


class ZhipuAdapter(OpenAIAdapter):
    """智谱 GLM（BigModel 开放平台）。

    协议是 OpenAI 兼容，唯一特别的是端点前缀 /api/paas/v4——版本段是 v4
    而不是 v1，所以默认 base_url 必须给全（join_url 已能识别任意版本段，
    用户手填 `.../api/paas/v4` 甚至整条端点 URL 也都落到同一处）。

    能力按实测端点声明：
    - chat        glm-4-flash / glm-4.5 / glm-5 系列
    - vision      仅 glm-4v-* 系列接受图片；glm-4.5 等纯文本模型会由上游报错
    - images      cogview-*（/images/generations）
    - speech      /audio/speech，模型要用 cogtts（glm-4-voice 不是 TTS 模型）
    - transcription /audio/transcriptions
    音色不预置白名单（voices 为空），因为智谱的音色名随账号与模型变化，
    写死会误拦；上游不认识时会把 1214「音色不存在」原样透传回来。
    """

    provider_type = "zhipu"
    label = "智谱 GLM"
    default_base_url = "https://open.bigmodel.cn/api/paas/v4"
    capabilities = (CAP_CHAT, CAP_VISION, CAP_IMAGES, CAP_SPEECH, CAP_TRANSCRIPTION)


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
