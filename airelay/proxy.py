"""上游调用编排：路由选渠道 → 协议适配 → 故障切换 → 流式归一化 → 用量计量。

这是网关内核里唯一同时接触「路由 / 适配器 / 计量 / 会话监控」的地方，
四者的耦合都收敛在 `ChatProxy` 内部，对外只暴露 prepare / iter_sse / finalize。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .adapters import (
    ChatRequest,
    Usage,
    estimate_messages_tokens,
    estimate_tokens,
    openai_chunk,
    sse_done,
    sse_event,
)
from .context import AppContext
from .errors import ErrorCode, RelayError
from .models import ApiKey, Channel
from .security import new_request_id
from .adapters.base import preferred_capabilities, required_capabilities
from .adapters.media import ImageRequest, MediaResult, SpeechRequest, TranscriptionRequest
from .services.live import LiveSession
from .services.mapping import ResolvedModel
from .timeutil import utcnow

log = logging.getLogger(__name__)

_RETRYABLE_UPSTREAM_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}
# 上游这些状态说明「请求本身有问题」，换渠道也没用，直接透传不重试
_NON_RETRYABLE_UPSTREAM_STATUS = {400, 401, 403, 404, 405, 413, 422}


@dataclass
class PreparedCall:
    """一次已建立的上游调用（流式时 response 仍处于打开状态）。"""

    request_id: str
    streaming: bool
    request: ChatRequest
    key: ApiKey
    resolved: ResolvedModel
    channel: Channel
    adapter: Any
    attempts: int
    started_monotonic: float
    live: LiveSession
    client_ip: str = ""
    user_agent: str = ""
    upstream: httpx.Response | None = None
    payload: dict[str, Any] | None = None
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    _metered: bool = False

    @property
    def latency_ms(self) -> float:
        return (time.perf_counter() - self.started_monotonic) * 1000.0

    @property
    def prompt_estimate(self) -> int:
        return estimate_messages_tokens(self.request.messages)


class ChatProxy:
    def __init__(self, context: AppContext) -> None:
        self.ctx = context

    # ------------------------------------------------------------------ 准备
    async def prepare(
        self,
        *,
        body: dict[str, Any],
        key: ApiKey,
        client_ip: str = "",
        user_agent: str = "",
    ) -> PreparedCall:
        ctx = self.ctx
        settings = ctx.settings
        if ctx.http is None:
            raise RelayError(ErrorCode.INTERNAL_ERROR, "网关尚未完成初始化", status=503)

        request = ChatRequest.from_body(body)
        if not request.model:
            raise RelayError(ErrorCode.BAD_REQUEST, "请求缺少 model 字段", param="model")
        if not request.messages:
            raise RelayError(ErrorCode.BAD_REQUEST, "请求缺少 messages 字段", param="messages")

        resolved = ctx.mapping.resolve(request.model)
        request_id = new_request_id()
        started = time.perf_counter()

        live = ctx.live.start(
            LiveSession(
                request_id=request_id,
                key_id=key.key_id,
                key_name=key.name,
                key_prefix=key.prefix,
                model=request.model,
                upstream_model=resolved.upstream,
                client_ip=client_ip,
                stream=request.stream,
            )
        )

        # 对话只要 chat 这一个硬门槛；带图片/音频只是「偏好」——
        # 没有 vision 渠道时也不能把请求挡死（兼容上游可能自己会处理图片块）
        required = required_capabilities("chat", body)
        preferred = preferred_capabilities("chat", body)
        async with ctx.session_factory() as session:
            channels = await ctx.channels.enabled_channels(session)
            plan = await ctx.router.plan(
                session,
                resolved=resolved,
                channels=channels,
                key_id=key.key_id,
                required_capabilities=required,
                preferred_capabilities=preferred,
            )
        if plan.empty:
            self.finalize_sync(live, status="error", error_code=ErrorCode.NO_CHANNEL_AVAILABLE)
            await self._record_error_usage(
                request_id=request_id,
                key=key,
                resolved=resolved,
                request=request,
                code=ErrorCode.NO_CHANNEL_AVAILABLE,
                attempts=0,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                client_ip=client_ip,
                user_agent=user_agent,
            )
            raise RelayError(ErrorCode.NO_CHANNEL_AVAILABLE, plan.reason or "没有可用渠道",
                             request_id=request_id)

        max_retries = max(0, settings.get_int("gateway.max_retries", 1))
        defaults = {"max_tokens": settings.get_int("gateway.default_max_tokens", 4096)}
        last_error: RelayError | None = None
        attempt = 0

        for channel in plan.candidates:
            attempt += 1
            try:
                adapter = ctx.channels.adapter_for(channel)
            except RelayError as exc:
                last_error = exc
                ctx.router.mark_failure(channel.channel_id, exc.message, retryable=False)
                continue

            try:
                call = adapter.build_chat_call(request, resolved.upstream, defaults=defaults)
            except Exception as exc:  # 适配器翻译失败属于配置问题，不重试
                last_error = RelayError(
                    ErrorCode.UPSTREAM_ERROR, f"构建上游请求失败：{exc}", retryable=False
                )
                ctx.router.mark_failure(channel.channel_id, str(exc), retryable=False)
                continue

            timeout = self._timeout_for(channel, streaming=request.stream)
            try:
                upstream = await ctx.http.send(call.to_request(timeout=timeout), stream=request.stream)
            except httpx.TimeoutException as exc:
                last_error = RelayError(
                    ErrorCode.UPSTREAM_TIMEOUT, f"连接上游 {channel.name} 超时：{exc}"
                )
                ctx.router.mark_failure(channel.channel_id, str(exc))
                await self._mark_channel_failure(channel.channel_id, str(exc))
                if not self._can_retry(attempt, max_retries, len(plan.candidates)):
                    break
                continue
            except httpx.HTTPError as exc:
                last_error = RelayError(ErrorCode.UPSTREAM_ERROR, f"访问上游 {channel.name} 失败：{exc}")
                ctx.router.mark_failure(channel.channel_id, str(exc))
                await self._mark_channel_failure(channel.channel_id, str(exc))
                if not self._can_retry(attempt, max_retries, len(plan.candidates)):
                    break
                continue

            if upstream.status_code >= 400:
                raw = await upstream.aread()
                await upstream.aclose()
                error = adapter.translate_error(upstream.status_code, raw)
                if upstream.status_code in _NON_RETRYABLE_UPSTREAM_STATUS:
                    error.retryable = False
                last_error = error
                ctx.router.mark_failure(channel.channel_id, error.message, retryable=error.retryable)
                await self._mark_channel_failure(channel.channel_id, error.message)
                if not error.retryable or not self._can_retry(attempt, max_retries, len(plan.candidates)):
                    break
                log.info(
                    "渠道 %s 返回 %s，换渠道重试（第 %s 次）", channel.name, upstream.status_code, attempt
                )
                continue

            # 成功：登记渠道健康度与会话归属
            ctx.router.mark_success(channel.channel_id, latency_ms=(time.perf_counter() - started) * 1000.0)
            ctx.router.remember_used(key.key_id, channel.channel_id)
            live.attempts = attempt
            ctx.live.set_channel(
                request_id,
                channel_id=channel.channel_id,
                channel_name=channel.name,
                provider_type=channel.provider_type,
                upstream_model=resolved.upstream,
            )
            asyncio.create_task(self._mark_channel_success(channel.channel_id))

            prepared = PreparedCall(
                request_id=request_id,
                streaming=request.stream,
                request=request,
                key=key,
                resolved=resolved,
                channel=channel,
                adapter=adapter,
                attempts=attempt,
                started_monotonic=started,
                live=live,
                client_ip=client_ip,
                user_agent=user_agent,
                upstream=upstream,
            )

            if request.stream:
                prepared.usage = Usage(prompt_tokens=estimate_messages_tokens(request.messages))
                ctx.live.update_usage(request_id, prompt_tokens=prepared.usage.prompt_tokens)
                return prepared

            raw = await upstream.aread()
            await upstream.aclose()
            prepared.upstream = None
            upstream_payload = self._decode_payload(raw, adapter)
            # 关键一步：把各家方言翻译回 OpenAI 兼容结构
            prepared.payload = adapter.normalize_response(
                upstream_payload, request, resolved.upstream
            )
            prepared.usage = adapter.extract_usage(upstream_payload) or self._estimate_usage(
                request, prepared.payload
            )
            prepared.finish_reason = _finish_reason_of(prepared.payload)
            ctx.live.update_usage(
                request_id,
                prompt_tokens=prepared.usage.prompt_tokens,
                completion_tokens=prepared.usage.completion_tokens,
                total_tokens=prepared.usage.total_tokens,
                source=prepared.usage.source,
            )
            await self.finalize(prepared, status="ok", finish_reason=prepared.finish_reason)
            return prepared

        error = last_error or RelayError(ErrorCode.NO_CHANNEL_AVAILABLE, "所有候选渠道均不可用")
        error.request_id = request_id
        self.finalize_sync(live, status="error", error_code=error.code)
        await self._record_error_usage(
            request_id=request_id,
            key=key,
            resolved=resolved,
            request=request,
            code=error.code,
            attempts=attempt,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            client_ip=client_ip,
            user_agent=user_agent,
            channel=plan.candidates[0] if plan.candidates else None,
        )
        raise error

    # ------------------------------------------------------------------ 流式
    async def iter_sse(self, prepared: PreparedCall) -> AsyncIterator[bytes]:
        """把上游流翻译成 OpenAI SSE 逐块转发（边收边发，不缓冲整包）。"""
        async for chunk in self.iter_chunks(prepared):
            if isinstance(chunk, dict):
                yield sse_event(chunk)
        yield sse_done()

    async def iter_stream_iter(self, prepared: PreparedCall) -> AsyncIterator[dict[str, Any]]:
        """与 iter_sse 同源，但吐出 dict chunk（供 /v1/completions 等再加工）。"""
        async for chunk in self.iter_chunks(prepared):
            if isinstance(chunk, dict):
                yield chunk

    async def iter_chunks(self, prepared: PreparedCall) -> AsyncIterator[dict[str, Any]]:
        """流式转发的核心：归一化 chunk + 实时更新会话 + 结束时计量。"""
        ctx = self.ctx
        settings = ctx.settings
        usage = prepared.usage
        finish_reason = ""
        status = "ok"
        error_code = ""
        saw_usage = False
        collected: list[str] = []
        idle_timeout = settings.get_float("gateway.idle_timeout", 120.0)
        total_budget = settings.get_float("gateway.request_timeout", 600.0)
        include_usage = settings.get_bool("gateway.stream_include_usage", True) or prepared.request.wants_usage()
        upstream = prepared.upstream
        assert upstream is not None

        try:
            async for chunk in prepared.adapter.iter_stream(
                upstream, prepared.request, prepared.resolved.upstream
            ):
                if not isinstance(chunk, dict):
                    continue
                if "model" in chunk:
                    chunk["model"] = prepared.resolved.requested
                chunk_usage = _usage_of_chunk(chunk)
                if chunk_usage is not None:
                    usage = chunk_usage
                    saw_usage = True
                reason = _finish_reason_of_chunk(chunk)
                if reason:
                    finish_reason = reason
                text = _delta_text(chunk)
                if text:
                    collected.append(text)
                ctx.live.note_activity(prepared.request_id, chars=len(text), chunks=1)
                ctx.live.update_usage(
                    prepared.request_id,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=None if saw_usage else estimate_tokens("".join(collected)),
                    source=usage.source,
                )
                yield chunk
                if time.perf_counter() - prepared.started_monotonic > total_budget:
                    raise RelayError(
                        ErrorCode.UPSTREAM_TIMEOUT,
                        f"流式响应超过请求总超时 {total_budget:.0f}s，已中断",
                    )

            if not saw_usage:
                usage = Usage(
                    prompt_tokens=usage.prompt_tokens or prepared.prompt_estimate,
                    completion_tokens=estimate_tokens("".join(collected)),
                    source="estimated",
                ).finalize()
            if include_usage and not saw_usage:
                usage_chunk = openai_chunk(
                    chunk_id=f"chatcmpl-{prepared.request_id[-12:]}",
                    model=prepared.resolved.requested,
                    created=int(utcnow().timestamp()),
                    delta={},
                    finish_reason=None,
                    usage=usage.to_openai(),
                )
                usage_chunk["choices"] = []
                yield usage_chunk
        except asyncio.CancelledError:
            status = "error"
            error_code = ErrorCode.CLIENT_DISCONNECTED
            raise
        except GeneratorExit:
            status = "error"
            error_code = ErrorCode.CLIENT_DISCONNECTED
            raise
        except RelayError as exc:
            status = "error"
            error_code = exc.code
            ctx.router.mark_failure(prepared.channel.channel_id, exc.message, retryable=exc.retryable)
            yield exc.to_openai_body()
        except httpx.TimeoutException:
            status = "error"
            error_code = ErrorCode.UPSTREAM_TIMEOUT
            message = f"上游静默超过 {idle_timeout:.0f}s，已中断"
            ctx.router.mark_failure(prepared.channel.channel_id, message)
            await self._mark_channel_failure(prepared.channel.channel_id, message)
            yield RelayError(ErrorCode.UPSTREAM_TIMEOUT, message).to_openai_body()
        except httpx.HTTPError as exc:
            status = "error"
            error_code = ErrorCode.UPSTREAM_ERROR
            message = f"上游连接中断：{exc}"
            ctx.router.mark_failure(prepared.channel.channel_id, message)
            yield RelayError(ErrorCode.UPSTREAM_ERROR, message).to_openai_body()
        except Exception as exc:  # pragma: no cover - 兜底，避免流悬挂
            status = "error"
            error_code = ErrorCode.INTERNAL_ERROR
            log.exception("流式转发出现未预期错误 request_id=%s", prepared.request_id)
            yield RelayError(ErrorCode.INTERNAL_ERROR, f"网关内部错误：{exc}").to_openai_body()
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            if status == "ok" and not saw_usage:
                usage = Usage(
                    prompt_tokens=usage.prompt_tokens or prepared.prompt_estimate,
                    completion_tokens=estimate_tokens("".join(collected)),
                    source="estimated",
                ).finalize()
            ctx.live.update_usage(
                prepared.request_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                source=usage.source,
            )
            try:
                await self.finalize(
                    prepared,
                    status=status,
                    error_code=error_code,
                    finish_reason=finish_reason or ("stop" if status == "ok" else ""),
                    usage=usage,
                )
            except Exception:  # 收尾失败不能影响已发出的流
                log.exception("流式收尾计量失败 request_id=%s", prepared.request_id)

    # ------------------------------------------------------------------ 计量
    async def finalize(
        self,
        prepared: PreparedCall,
        *,
        status: str = "ok",
        error_code: str = "",
        finish_reason: str = "",
        usage: Usage | None = None,
    ) -> None:
        if prepared._metered:
            return
        prepared._metered = True
        usage = usage or prepared.usage
        ctx = self.ctx
        # 计价优先用客户端请求的模型名（别名），其次回落到上游真实 id
        price_model = prepared.resolved.requested
        if price_model not in ctx.pricing.models and prepared.resolved.upstream in ctx.pricing.models:
            price_model = prepared.resolved.upstream
        cost_units = ctx.pricing.cost_units(
            price_model, usage.prompt_tokens, usage.completion_tokens
        )
        session = ctx.live.get(prepared.request_id)
        first_token_ms = 0.0
        speed = 0.0
        if session is not None:
            first_token_ms = session.first_token_ms
            # 输出速度只对「边收边发」的流式有意义。非流式是整包返回，用总耗时算出来的
            # 是吞吐而不是输出速度，记 0 让报表口径干净（界面显示 —）。
            speed = session.speed_tok_s if session.first_token_at is not None else 0.0
        latency_ms = prepared.latency_ms
        prepared.usage = usage.finalize()
        # 限流按真实用量补记（预检用的是估算值）
        ctx.ratelimiter.charge_tokens(prepared.key.key_id, prepared.usage.total_tokens)
        ctx.live.finish(
            prepared.request_id,
            status=status,
            error_code=error_code,
            finish_reason=finish_reason,
            cost_units=cost_units,
            retention=ctx.settings.get_int("monitoring.recent_limit", 50),
        )
        from .services.usage import UsageRecord

        ctx.usage.record_soon(
            UsageRecord(
                request_id=prepared.request_id,
                key_id=prepared.key.key_id,
                key_name=prepared.key.name,
                key_prefix=prepared.key.prefix,
                channel_id=prepared.channel.channel_id,
                channel_name=prepared.channel.name,
                provider_type=prepared.channel.provider_type,
                model=prepared.resolved.requested,
                upstream_model=prepared.resolved.upstream,
                prompt_tokens=prepared.usage.prompt_tokens,
                completion_tokens=prepared.usage.completion_tokens,
                total_tokens=prepared.usage.total_tokens,
                latency_ms=latency_ms,
                first_token_ms=first_token_ms,
                speed_tok_s=speed,
                stream=prepared.streaming,
                attempts=prepared.attempts,
                status=status,
                error_code=error_code,
                cost_units=cost_units,
                client_ip=prepared.client_ip,
                user_agent=prepared.user_agent,
            )
        )
        if ctx.settings.get_bool("logs.access_log", True):
            log.log(
                logging.WARNING if status != "ok" else logging.INFO,
                "%s %s | %s → %s | %s tok | %.1fms | %.1f tok/s | %s",
                prepared.resolved.requested,
                ("stream" if prepared.streaming else "sync"),
                prepared.key.name or prepared.key.prefix,
                prepared.channel.name,
                prepared.usage.total_tokens,
                latency_ms,
                speed,
                status if status == "ok" else f"{status}({error_code})",
            )

    def finalize_sync(self, live: LiveSession, *, status: str, error_code: str) -> None:
        """在没有 PreparedCall 的早退路径上直接收尾会话。"""
        self.ctx.live.finish(live.request_id, status=status, error_code=error_code)

    async def _record_error_usage(
        self,
        *,
        request_id: str,
        key: ApiKey,
        resolved: ResolvedModel,
        request: ChatRequest,
        code: str,
        attempts: int,
        latency_ms: float,
        client_ip: str,
        user_agent: str,
        channel: Channel | None = None,
    ) -> None:
        """失败请求也留痕，便于控制台看出错误率。"""
        from .services.usage import UsageRecord

        prompt_tokens = estimate_messages_tokens(request.messages)
        self.ctx.usage.record_soon(
            UsageRecord(
                request_id=request_id,
                key_id=key.key_id,
                key_name=key.name,
                key_prefix=key.prefix,
                channel_id=channel.channel_id if channel else "",
                channel_name=channel.name if channel else "",
                provider_type=channel.provider_type if channel else "",
                model=request.model,
                upstream_model=resolved.upstream,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                total_tokens=prompt_tokens,
                latency_ms=latency_ms,
                stream=request.stream,
                attempts=attempts,
                status="error",
                error_code=code,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )

    # ------------------------------------------------------------------ 工具
    def _timeout_for(self, channel: Channel, *, streaming: bool) -> dict[str, float]:
        settings = self.ctx.settings
        connect = settings.get_float("gateway.connect_timeout", 15.0)
        if channel.timeout_seconds:
            connect = min(connect, float(channel.timeout_seconds))
        read = (
            settings.get_float("gateway.idle_timeout", 120.0)
            if streaming
            else settings.get_float("gateway.request_timeout", 600.0)
        )
        first_byte = settings.get_float("gateway.first_byte_timeout", 60.0)
        if streaming:
            read = min(read, first_byte + read)
        return {"connect": connect, "read": read, "write": connect, "pool": connect}

    @staticmethod
    def _can_retry(attempt: int, max_retries: int, candidates: int) -> bool:
        return (attempt - 1) < max_retries and attempt < candidates

    async def _mark_channel_failure(self, channel_id: str, message: str) -> None:
        try:
            async with self.ctx.session_factory() as session:
                await self.ctx.channels.mark_failure(session, channel_id, message)
        except Exception:
            log.debug("记录渠道失败状态时出错", exc_info=True)

    async def _mark_channel_success(self, channel_id: str) -> None:
        try:
            async with self.ctx.session_factory() as session:
                await self.ctx.channels.mark_success(session, channel_id)
        except Exception:
            log.debug("记录渠道成功状态时出错", exc_info=True)

    @staticmethod
    def _decode_payload(raw: bytes, adapter: Any) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise RelayError(
                ErrorCode.UPSTREAM_ERROR,
                "上游返回内容不是合法 JSON",
                details={"preview": raw[:300].decode("utf-8", "replace")},
            ) from None
        if not isinstance(payload, dict):
            raise RelayError(ErrorCode.UPSTREAM_ERROR, "上游返回结构不是 JSON 对象")
        if isinstance(payload.get("error"), (dict, str)):
            message = payload["error"] if isinstance(payload["error"], str) else payload["error"].get("message")
            raise RelayError(ErrorCode.UPSTREAM_ERROR, f"上游返回错误：{message}")
        return payload

    def _estimate_usage(self, request: ChatRequest, payload: dict[str, Any]) -> Usage:
        prompt = estimate_messages_tokens(request.messages)
        completion = 0
        for choice in payload.get("choices") or []:
            message = (choice or {}).get("message") or {}
            completion += estimate_tokens(message.get("content"))
            for call in message.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                completion += estimate_tokens(fn.get("arguments")) + 6
        return Usage(prompt, completion, prompt + completion, source="estimated")


# --------------------------------------------------------------------------- #
def _delta_text(chunk: dict[str, Any]) -> str:
    parts: list[str] = []
    for choice in chunk.get("choices") or []:
        delta = (choice or {}).get("delta") or {}
        for key in ("content", "reasoning_content"):
            value = delta.get(key)
            if isinstance(value, str):
                parts.append(value)
    return "".join(parts)


def _finish_reason_of_chunk(chunk: dict[str, Any]) -> str:
    for choice in chunk.get("choices") or []:
        reason = (choice or {}).get("finish_reason")
        if reason:
            return str(reason)
    return ""


def _finish_reason_of(payload: dict[str, Any]) -> str:
    for choice in payload.get("choices") or []:
        reason = (choice or {}).get("finish_reason")
        if reason:
            return str(reason)
    return ""


def _usage_of_chunk(chunk: dict[str, Any]) -> Usage | None:
    usage = chunk.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _int(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _int(usage.get("completion_tokens") or usage.get("output_tokens"))
    total = _int(usage.get("total_tokens")) or (prompt + completion)
    if not (prompt or completion or total):
        return None
    return Usage(prompt, completion, total, source="upstream")


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# =========================================================================== #
# 媒体请求：语音合成 / 语音识别 / 图片生成
# =========================================================================== #
_MEDIA_BUILDERS = {
    "speech": ("build_speech_call", "normalize_speech"),
    "transcription": ("build_transcription_call", "normalize_transcription"),
    "images": ("build_image_call", "normalize_images"),
}


@dataclass
class PreparedMedia:
    kind: str
    request_id: str
    key: ApiKey
    request: Any
    resolved: ResolvedModel
    channel: Channel
    adapter: Any
    attempts: int
    started_monotonic: float
    live: LiveSession
    client_ip: str = ""
    user_agent: str = ""
    result: MediaResult | None = None

    @property
    def latency_ms(self) -> float:
        return (time.perf_counter() - self.started_monotonic) * 1000.0


class MediaProxy:
    """非对话能力的编排器：同样是「按能力选渠道 + 失败换渠道 + 落库计量」。"""

    KINDS = tuple(_MEDIA_BUILDERS)

    def __init__(self, context: AppContext) -> None:
        self.ctx = context

    async def execute(
        self,
        *,
        kind: str,
        request: SpeechRequest | TranscriptionRequest | ImageRequest,
        body: dict[str, Any] | None,
        key: ApiKey,
        client_ip: str = "",
        user_agent: str = "",
    ) -> MediaResult:
        ctx = self.ctx
        settings = ctx.settings
        if kind not in _MEDIA_BUILDERS:
            raise RelayError(ErrorCode.BAD_REQUEST, f"不支持的媒体类型：{kind}")
        if ctx.http is None:
            raise RelayError(ErrorCode.INTERNAL_ERROR, "网关尚未完成初始化", status=503)

        build_name, normalize_name = _MEDIA_BUILDERS[kind]
        resolved = ctx.mapping.resolve(request.model)
        request_id = new_request_id()
        started = time.perf_counter()
        live = ctx.live.start(
            LiveSession(
                request_id=request_id,
                key_id=key.key_id,
                key_name=key.name,
                key_prefix=key.prefix,
                model=request.model,
                upstream_model=resolved.upstream,
                client_ip=client_ip,
                stream=False,
            )
        )

        required = required_capabilities(kind, body)
        async with ctx.session_factory() as session:
            channels = await ctx.channels.enabled_channels(session)
            plan = await ctx.router.plan(
                session, resolved=resolved, channels=channels, key_id=key.key_id,
                required_capabilities=required,
            )
        if plan.empty:
            ctx.live.finish(request_id, status="error", error_code=ErrorCode.NO_CHANNEL_AVAILABLE)
            await self._record_failure(
                kind=kind, request_id=request_id, key=key, resolved=resolved,
                request=request, code=ErrorCode.NO_CHANNEL_AVAILABLE, attempts=0,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                client_ip=client_ip, user_agent=user_agent,
            )
            raise RelayError(ErrorCode.NO_CHANNEL_AVAILABLE, plan.reason or "没有可用渠道")

        max_retries = max(0, settings.get_int("gateway.max_retries", 1))
        defaults = {"max_tokens": settings.get_int("gateway.default_max_tokens", 4096)}
        last_error: RelayError | None = None
        attempt = 0

        for channel in plan.candidates:
            attempt += 1
            try:
                adapter = ctx.channels.adapter_for(channel)
                call = getattr(adapter, build_name)(request, resolved.upstream, defaults=defaults)
            except RelayError as exc:
                last_error = exc
                if not exc.retryable:
                    break
                ctx.router.mark_failure(channel.channel_id, exc.message, retryable=False)
                continue

            timeout = self._timeout_for(channel)
            try:
                upstream = await ctx.http.send(call.to_request(timeout=timeout))
            except httpx.TimeoutException as exc:
                last_error = RelayError(ErrorCode.UPSTREAM_TIMEOUT, f"访问上游 {channel.name} 超时：{exc}")
                ctx.router.mark_failure(channel.channel_id, str(exc))
                await self._mark_failure_db(channel.channel_id, str(exc))
                if not self._can_retry(attempt, max_retries, len(plan.candidates)): break
                continue
            except httpx.HTTPError as exc:
                last_error = RelayError(ErrorCode.UPSTREAM_ERROR, f"访问上游 {channel.name} 失败：{exc}")
                ctx.router.mark_failure(channel.channel_id, str(exc))
                await self._mark_failure_db(channel.channel_id, str(exc))
                if not self._can_retry(attempt, max_retries, len(plan.candidates)): break
                continue

            raw = await upstream.aread()
            content_type = upstream.headers.get("content-type", "")
            if upstream.status_code >= 400:
                error = adapter.translate_error(upstream.status_code, raw)
                if upstream.status_code in {400, 401, 403, 404, 405, 413, 422}:
                    error.retryable = False
                last_error = error
                ctx.router.mark_failure(channel.channel_id, error.message, retryable=error.retryable)
                await self._mark_failure_db(channel.channel_id, error.message)
                if not error.retryable or not self._can_retry(attempt, max_retries, len(plan.candidates)):
                    break
                log.info("渠道 %s 返回 %s，换渠道重试", channel.name, upstream.status_code)
                continue

            ctx.router.mark_success(channel.channel_id, latency_ms=(time.perf_counter() - started) * 1000.0)
            ctx.router.remember_used(key.key_id, channel.channel_id)
            asyncio.create_task(self._mark_success_db(channel.channel_id))
            ctx.live.set_channel(
                request_id, channel_id=channel.channel_id, channel_name=channel.name,
                provider_type=channel.provider_type, upstream_model=resolved.upstream,
            )

            prepared = PreparedMedia(
                kind=kind, request_id=request_id, key=key, request=request, resolved=resolved,
                channel=channel, adapter=adapter, attempts=attempt, started_monotonic=started,
                live=live, client_ip=client_ip, user_agent=user_agent,
            )
            if kind == "speech":
                result = getattr(adapter, normalize_name)(raw, content_type, request)
            else:
                payload = ChatProxy._decode_payload(raw, adapter)
                result = getattr(adapter, normalize_name)(payload, request)
            result.request_id = request_id
            result.channel_id = channel.channel_id
            result.channel_name = channel.name
            result.provider_type = channel.provider_type
            prepared.result = result
            live.attempts = attempt
            ctx.live.set_units(request_id, units=result.units, unit_kind=result.unit_kind)
            await self._finalize(prepared, result)
            return result

        error = last_error or RelayError(ErrorCode.NO_CHANNEL_AVAILABLE, "所有候选渠道均不可用")
        error.request_id = request_id
        ctx.live.finish(request_id, status="error", error_code=error.code)
        await self._record_failure(
            kind=kind, request_id=request_id, key=key, resolved=resolved, request=request,
            code=error.code, attempts=attempt,
            latency_ms=(time.perf_counter() - started) * 1000.0,
            client_ip=client_ip, user_agent=user_agent,
            channel=plan.candidates[0] if plan.candidates else None,
        )
        raise error

    async def _finalize(self, prepared: PreparedMedia, result: MediaResult) -> None:
        ctx = self.ctx
        pricing = ctx.pricing
        price_model = prepared.resolved.requested
        if price_model not in pricing.models and prepared.resolved.upstream in pricing.models:
            price_model = prepared.resolved.upstream
        cost_units = pricing.cost_units(
            price_model,
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
            unit_kind=result.unit_kind,
            units=result.units,
        )
        ctx.live.finish(
            prepared.request_id, status="ok", finish_reason="stop", cost_units=cost_units,
            retention=ctx.settings.get_int("monitoring.recent_limit", 50),
        )
        ctx.live.update_usage(
            prepared.request_id,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            total_tokens=result.usage.total_tokens,
            source=result.usage.source,
        )
        from .services.usage import UsageRecord

        ctx.usage.record_soon(
            UsageRecord(
                request_id=prepared.request_id,
                key_id=prepared.key.key_id, key_name=prepared.key.name, key_prefix=prepared.key.prefix,
                channel_id=prepared.channel.channel_id, channel_name=prepared.channel.name,
                provider_type=prepared.channel.provider_type,
                model=prepared.resolved.requested, upstream_model=prepared.resolved.upstream,
                prompt_tokens=result.usage.prompt_tokens, completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
                latency_ms=prepared.latency_ms, stream=False, attempts=prepared.attempts,
                status="ok", cost_units=cost_units, units=result.units, unit_kind=result.unit_kind,
                client_ip=prepared.client_ip, user_agent=prepared.user_agent,
            )
        )
        if ctx.settings.get_bool("logs.access_log", True):
            log.info(
                "%s %s | %s → %s | %s %s | %.1fms | ok",
                prepared.resolved.requested, prepared.kind, prepared.key.name or prepared.key.prefix,
                prepared.channel.name, result.units,
                result.unit_kind or "tok", prepared.latency_ms,
            )

    async def _record_failure(
        self, *, kind: str, request_id: str, key: ApiKey, resolved: ResolvedModel, request: Any,
        code: str, attempts: int, latency_ms: float, client_ip: str, user_agent: str,
        channel: Channel | None = None,
    ) -> None:
        from .services.usage import UsageRecord

        self.ctx.usage.record_soon(
            UsageRecord(
                request_id=request_id, key_id=key.key_id, key_name=key.name, key_prefix=key.prefix,
                channel_id=channel.channel_id if channel else "",
                channel_name=channel.name if channel else "",
                provider_type=channel.provider_type if channel else "",
                model=request.model, upstream_model=resolved.upstream,
                latency_ms=latency_ms, stream=False, attempts=attempts,
                status="error", error_code=code, client_ip=client_ip, user_agent=user_agent,
            )
        )

    def _timeout_for(self, channel: Channel) -> dict[str, float]:
        settings = self.ctx.settings
        connect = settings.get_float("gateway.connect_timeout", 15.0)
        if channel.timeout_seconds:
            connect = min(connect, float(channel.timeout_seconds))
        # 图片生成/语音合成比对话慢得多，给足读超时（默认沿用请求总超时）
        read = settings.get_float("gateway.request_timeout", 600.0)
        return {"connect": connect, "read": read, "write": connect, "pool": connect}

    @staticmethod
    def _can_retry(attempt: int, max_retries: int, candidates: int) -> bool:
        return (attempt - 1) < max_retries and attempt < candidates

    async def _mark_failure_db(self, channel_id: str, message: str) -> None:
        try:
            async with self.ctx.session_factory() as session:
                await self.ctx.channels.mark_failure(session, channel_id, message)
        except Exception:
            log.debug("记录渠道失败状态时出错", exc_info=True)

    async def _mark_success_db(self, channel_id: str) -> None:
        try:
            async with self.ctx.session_factory() as session:
                await self.ctx.channels.mark_success(session, channel_id)
        except Exception:
            log.debug("记录渠道成功状态时出错", exc_info=True)
