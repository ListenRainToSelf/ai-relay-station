"""协议面（对外）：OpenAI 兼容入口。

只暴露 `/v1/*`，与 `/api/admin/*` 管理面物理隔离（方案 3.1 的三个面）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..adapters import estimate_messages_tokens
from ..adapters.media import ImageRequest, SpeechRequest, TranscriptionRequest
from ..errors import ErrorCode, RelayError
from ..proxy import ChatProxy, MediaProxy
from ..services.routing import channel_capabilities, channel_serves_request
from ..services.keys import KeyService
from ..version import __version__
from .deps import client_ip_of, require_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["协议面"])

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # 让 Nginx 不缓冲，保证边收边发
}


async def _read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        raise RelayError(ErrorCode.BAD_REQUEST, "请求体为空")
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise RelayError(ErrorCode.BAD_REQUEST, f"请求体不是合法 JSON：{exc}") from None
    if not isinstance(payload, dict):
        raise RelayError(ErrorCode.BAD_REQUEST, "请求体必须是 JSON 对象")
    return payload


async def _authorize(request: Request, model: str, *, estimated_tokens: int = 0) -> tuple[Any, Any]:
    """鉴权 → 模型授权 → 配额 → 限流。对话与媒体端点共用这一套。"""
    ctx = request.app.state.ctx
    key = await require_api_key(request)
    if not model:
        raise RelayError(ErrorCode.BAD_REQUEST, "请求缺少 model 字段", param="model")

    KeyService.ensure_model_allowed(key, model)
    KeyService.ensure_quota(key)

    global_rpm = ctx.settings.get_int("ratelimit.global_rpm", 0)
    global_tpm = ctx.settings.get_int("ratelimit.global_tpm", 0)
    violation = ctx.ratelimiter.check_request(
        key_id=key.key_id, key_rpm=key.rpm_limit, global_rpm=global_rpm
    )
    if violation is not None:
        raise _rate_error(violation)

    token_violation = ctx.ratelimiter.check_tokens(
        key_id=key.key_id, estimated=estimated_tokens,
        key_tpm=key.tpm_limit, global_tpm=global_tpm,
    )
    if token_violation is not None:
        raise _rate_error(token_violation)

    ctx.ratelimiter.charge_request(key.key_id)
    return ctx, key


async def _guard_and_call(
    request: Request, *, transform: Any = None
) -> tuple[Any, Any, bool]:
    """鉴权 → 授权 → 限流 → 打开上游调用。返回 (ctx, prepared, streaming)。"""
    ctx = request.app.state.ctx
    body = await _read_json(request)
    if transform is not None:
        body = transform(body)
    model = str(body.get("model") or "").strip()
    _, key = await _authorize(
        request, model, estimated_tokens=estimate_messages_tokens(body.get("messages") or [])
    )

    proxy = ChatProxy(ctx)
    prepared = await proxy.prepare(
        body=body,
        key=key,
        client_ip=client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False)),
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    return ctx, prepared, prepared.streaming


def _rate_error(violation: dict[str, Any]) -> RelayError:
    metric = violation.get("metric", "rpm").upper()
    scope = "全局" if violation["scope"] == "global" else "该密钥"
    return RelayError(
        ErrorCode.RATE_LIMITED,
        f"触发{scope}{metric}限流：上限 {violation['limit']}，当前窗口已用 {violation['used']}",
        details=violation,
    )


def _ascii_header(value: str, fallback: str = "-") -> str:
    """HTTP 头值只能是 latin-1；渠道名/模型名可能是中文，必要时退回 id。"""
    text = value or ""
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return fallback
    return text


def _call_headers(prepared: Any, ctx: Any) -> dict[str, str]:
    return {
        "X-Request-Id": prepared.request_id,
        "X-Airelay-Version": __version__,
        # 渠道名允许中文（界面里很常见），头里放不下时退回 channel_id
        "X-Airelay-Channel": _ascii_header(prepared.channel.name, prepared.channel.channel_id),
        "X-Airelay-Channel-Id": prepared.channel.channel_id,
        "X-Airelay-Provider": _ascii_header(prepared.channel.provider_type),
        "X-Airelay-Upstream-Model": _ascii_header(prepared.resolved.upstream),
        "X-Airelay-Attempts": str(prepared.attempts),
    }


# --------------------------------------------------------------------------- #
# /v1/chat/completions
# --------------------------------------------------------------------------- #
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    ctx, prepared, streaming = await _guard_and_call(request)
    headers = _call_headers(prepared, ctx)
    if streaming:
        proxy = ChatProxy(ctx)
        try:
            return StreamingResponse(
                proxy.iter_sse(prepared),
                media_type="text/event-stream",
                headers={**SSE_HEADERS, **headers},
            )
        except Exception:
            # 构造响应体失败时上游流还开着、会话还挂在活跃表里，必须收尾
            await proxy.finalize(
                prepared, status="error", error_code=ErrorCode.INTERNAL_ERROR
            )
            raise
    return JSONResponse(prepared.payload, headers=headers)


# --------------------------------------------------------------------------- #
# 语音合成：POST /v1/audio/speech
# --------------------------------------------------------------------------- #
@router.post("/v1/audio/speech")
async def audio_speech(request: Request):
    """按 OpenAI 约定返回音频字节。

    上游实现方式各不相同：OpenAI 兼容的直接打 `/audio/speech`；
    MiMo 这类只在对话里返回音频的，由适配器翻译成对话请求再把音频取出来；
    Gemini 则把裸 PCM 包成 WAV 再返回。
    """
    ctx = request.app.state.ctx
    body = await _read_json(request)
    speech = SpeechRequest.from_body(body)
    if not speech.input:
        raise RelayError(ErrorCode.BAD_REQUEST, "缺少 input 字段（要合成的文本）", param="input")
    _, key = await _authorize(request, speech.model, estimated_tokens=speech.characters // 2)

    proxy = MediaProxy(ctx)
    result = await proxy.execute(
        kind="speech", request=speech, body=body, key=key,
        client_ip=client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False)),
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    headers = _media_headers(result, kind="speech")
    headers["Content-Disposition"] = 'inline; filename="speech.' + _extension_of(result.content_type) + '"'
    return Response(content=result.body, media_type=result.content_type, headers=headers)


# --------------------------------------------------------------------------- #
# 语音识别：POST /v1/audio/transcriptions（multipart/form-data）
# --------------------------------------------------------------------------- #
@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(request: Request):
    """接收 multipart 上传的音频，返回转写文本。"""
    ctx = request.app.state.ctx
    try:
        form = await request.form()
    except Exception as exc:  # 不是 multipart / 缺 python-multipart
        raise RelayError(
            ErrorCode.BAD_REQUEST,
            f"请求体解析失败（需要 multipart/form-data，字段 file 与 model）：{exc}",
        ) from None

    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise RelayError(ErrorCode.BAD_REQUEST, "缺少 file 字段（要转写的音频文件）", param="file")
    content = await upload.read()
    filename = getattr(upload, "filename", "") or "audio.wav"
    content_type = getattr(upload, "content_type", "") or ""

    transcription = TranscriptionRequest(
        model=str(form.get("model") or ""),
        filename=filename,
        content=content,
        content_type=content_type,
        language=str(form.get("language") or ""),
        prompt=str(form.get("prompt") or ""),
        response_format=str(form.get("response_format") or "json"),
        temperature=form.get("temperature"),
    )
    _, key = await _authorize(request, transcription.model)

    proxy = MediaProxy(ctx)
    result = await proxy.execute(
        kind="transcription", request=transcription, body=None, key=key,
        client_ip=client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False)),
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    headers = _media_headers(result, kind="transcription")
    if result.body and result.content_type.startswith("text/"):
        return Response(content=result.body, media_type=result.content_type, headers=headers)
    return JSONResponse(result.payload or {"text": ""}, headers=headers)


# --------------------------------------------------------------------------- #
# 图片生成：POST /v1/images/generations
# --------------------------------------------------------------------------- #
@router.post("/v1/images/generations")
async def image_generations(request: Request):
    ctx = request.app.state.ctx
    body = await _read_json(request)
    image_request = ImageRequest.from_body(body)
    if not image_request.prompt:
        raise RelayError(ErrorCode.BAD_REQUEST, "缺少 prompt 字段", param="prompt")
    _, key = await _authorize(request, image_request.model, estimated_tokens=image_request.n * 64)

    proxy = MediaProxy(ctx)
    result = await proxy.execute(
        kind="images", request=image_request, body=body, key=key,
        client_ip=client_ip_of(request, trust_proxy=ctx.settings.get_bool("network.trust_proxy", False)),
        user_agent=request.headers.get("user-agent", "")[:255],
    )
    return JSONResponse(result.payload or {"data": []}, headers=_media_headers(result, kind="images"))


def _request_id_of(result: Any) -> str:
    return getattr(result, "request_id", "") or ""


def _header_text(value: str) -> str:
    """头值只能是 latin-1；中文（如「音色 冰糖」）改用百分号编码传出，别直接丢掉。"""
    text = value or ""
    try:
        text.encode("latin-1")
        return text
    except UnicodeEncodeError:
        from urllib.parse import quote

        return quote(text, safe="")


def _media_headers(result: Any, *, kind: str) -> dict[str, str]:
    """媒体响应统一带：请求 ID、渠道归属、计量单位、备注。"""
    default_unit = {"speech": "character", "transcription": "second", "images": "image"}.get(kind, "")
    headers = {
        "X-Request-Id": _request_id_of(result),
        "X-Airelay-Version": __version__,
        "X-Airelay-Units": f"{result.units} {result.unit_kind or default_unit}",
        "X-Airelay-Kind": kind,
    }
    channel_id = getattr(result, "channel_id", "")
    if channel_id:
        headers["X-Airelay-Channel-Id"] = channel_id
    channel_name = getattr(result, "channel_name", "")
    if channel_name:
        headers["X-Airelay-Channel"] = _header_text(channel_name)
    provider = getattr(result, "provider_type", "")
    if provider:
        headers["X-Airelay-Provider"] = _header_text(provider)
    if getattr(result, "note", ""):
        headers["X-Airelay-Note"] = _header_text(result.note)
    return headers


def _extension_of(content_type: str) -> str:
    mapping = {
        "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
        "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg",
        "audio/opus": "opus", "audio/flac": "flac", "audio/mp4": "m4a",
        "audio/aac": "aac", "audio/webm": "webm",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), "wav")



# --------------------------------------------------------------------------- #
# /v1/completions（旧版文本补全，映射到 chat 再转回 legacy 形状）
# --------------------------------------------------------------------------- #
def _prompt_to_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(str(item) for item in prompt)
    elif prompt is None:
        prompt = ""
    return [{"role": "user", "content": str(prompt)}]


def _to_legacy_response(payload: dict[str, Any]) -> dict[str, Any]:
    choices: list[dict[str, Any]] = []
    for position, choice in enumerate(payload.get("choices") or []):
        message = (choice or {}).get("message") or {}
        choices.append(
            {
                "text": message.get("content") or "",
                "index": (choice or {}).get("index", position),
                "logprobs": None,
                "finish_reason": (choice or {}).get("finish_reason"),
            }
        )
    result: dict[str, Any] = {
        "id": payload.get("id"),
        "object": "text_completion",
        "created": payload.get("created"),
        "model": payload.get("model"),
        "choices": choices,
    }
    if payload.get("usage"):
        result["usage"] = payload["usage"]
    return result


@router.post("/v1/completions")
async def text_completions(request: Request):
    def to_chat_body(body: dict[str, Any]) -> dict[str, Any]:
        """旧版 /completions 只有 prompt，先补成 chat 形态再走同一条链路。"""
        if body.get("messages"):
            return body
        converted = dict(body)
        converted["messages"] = _prompt_to_messages(body)
        converted.pop("prompt", None)
        return converted

    ctx, prepared, streaming = await _guard_and_call(request, transform=to_chat_body)
    headers = _call_headers(prepared, ctx)
    proxy = ChatProxy(ctx)

    if streaming:
        async def legacy_stream() -> AsyncIterator[bytes]:
            from ..adapters import sse_done, sse_event

            async for chunk in proxy.iter_stream_iter(prepared):
                if isinstance(chunk, dict) and "choices" in chunk:
                    legacy_choices = []
                    for choice in chunk.get("choices") or []:
                        delta = (choice or {}).get("delta") or {}
                        legacy_choices.append(
                            {
                                "text": delta.get("content") or "",
                                "index": (choice or {}).get("index", 0),
                                "logprobs": None,
                                "finish_reason": (choice or {}).get("finish_reason"),
                            }
                        )
                    chunk["choices"] = legacy_choices
                    chunk["object"] = "text_completion"
                yield sse_event(chunk)
            yield sse_done()

        return StreamingResponse(
            legacy_stream(), media_type="text/event-stream", headers={**SSE_HEADERS, **headers}
        )

    return JSONResponse(_to_legacy_response(prepared.payload or {}), headers=headers)


# --------------------------------------------------------------------------- #
# /v1/models
# --------------------------------------------------------------------------- #
@router.get("/v1/models")
async def list_models(request: Request):
    ctx = request.app.state.ctx
    await require_api_key(request)
    created = int(ctx.started_at.timestamp())
    data: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 先算出「哪个模型由哪些渠道服务、这些渠道具备什么能力」，
    # 客户端据此就知道这个模型能不能做语音合成/图片生成。
    caps_by_model: dict[str, set[str]] = {}
    async with ctx.session_factory() as session:
        channels = await ctx.channels.enabled_channels(session)
    declared: list[tuple[str, str]] = []
    for channel in channels:
        caps = channel_capabilities(channel)
        for model in channel.model_patterns():
            if any(char in model for char in "*?["):
                continue
            caps_by_model.setdefault(model, set()).update(caps)
            declared.append((model, channel.provider_type))

    def push(item_id: str, owned_by: str, upstream: str = "") -> None:
        if not item_id or item_id in seen:
            return
        seen.add(item_id)
        entry: dict[str, Any] = {
            "id": item_id,
            "object": "model",
            "created": created,
            "owned_by": owned_by or "airelay",
        }
        if upstream:
            entry["upstream"] = upstream
            entry["capabilities"] = sorted(caps_by_model.get(upstream, set()))
        else:
            entry["capabilities"] = sorted(caps_by_model.get(item_id, set()))
        data.append(entry)

    # 别名优先（这是用户日常要记的名字），后面再补上渠道声明的具体模型，
    # 这样既能看到短名，也能看到真实 id，客户端下拉才完整。
    for alias in ctx.mapping.known_aliases():
        resolved = ctx.mapping.resolve(alias)
        push(alias, resolved.provider, resolved.upstream)
    for model, provider in declared:
        push(model, provider)

    return {"object": "list", "data": data}


@router.get("/v1/models/{model_id:path}")
async def retrieve_model(model_id: str, request: Request):
    ctx = request.app.state.ctx
    await require_api_key(request)
    resolved = ctx.mapping.resolve(model_id)
    async with ctx.session_factory() as session:
        channels = await ctx.channels.enabled_channels(session)
    capabilities: set[str] = set()
    for channel in channels:
        if channel_serves_request(channel, resolved):
            capabilities |= channel_capabilities(channel)
    return {
        "id": resolved.requested,
        "object": "model",
        "created": int(ctx.started_at.timestamp()),
        "owned_by": resolved.provider or "airelay",
        "upstream_model": resolved.upstream,
        "alias": resolved.alias,
        "capabilities": sorted(capabilities),
    }
