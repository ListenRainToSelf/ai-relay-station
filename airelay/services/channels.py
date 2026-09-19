"""渠道服务：上游渠道的增删改查、适配器实例化、连通性自检。

对应方案 3.2 的「上游渠道层」与 7.1 的 channels 表。
上游 API Key 一律加密落库，对外序列化只回显掩码。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import BaseAdapter, ChatRequest, create_adapter, normalize_provider
from .routing import channel_capabilities
from ..errors import ErrorCode, RelayError, bad_request, not_found
from ..models import Channel
from ..security import SecretBox
from ..timeutil import to_iso, utcnow
from ..settings import SettingsService

log = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return secret[:2] + "***"
    return f"{secret[:6]}...{secret[-4:]}"


class ChannelService:
    def __init__(self, *, cipher: SecretBox, settings: SettingsService) -> None:
        self.cipher = cipher
        self.settings = settings
        # 上游模型列表缓存：channel_id -> (取回时刻, items)。
        # 「填模型名」的下拉要看到所有模型，就得认识每个渠道的上游列表；逐个实时拉一遍
        # 又慢又容易被上游限流，所以拉到的结果留在内存里复用（进程重启即失效）。
        self._models_cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
        # 每个渠道最近一次拉取失败的说明（成功的渠道会被清掉），给下拉里做个标记
        self._models_errors: dict[str, str] = {}

    # ------------------------------------------------------------------ 查询
    async def list_channels(
        self, session: AsyncSession, *, search: str = "", status: str = "", limit: int = 200, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        stmt = select(Channel)
        count_stmt = select(func.count()).select_from(Channel)
        if status:
            stmt = stmt.where(Channel.status == status)
            count_stmt = count_stmt.where(Channel.status == status)
        if search:
            like = f"%{search}%"
            condition = (
                Channel.name.like(like)
                | Channel.provider_type.like(like)
                | Channel.base_url.like(like)
            )
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = stmt.order_by(Channel.priority.asc(), Channel.created_at.desc()).limit(limit).offset(offset)
        records = (await session.execute(stmt)).scalars().all()
        total = int((await session.execute(count_stmt)).scalar_one())
        return [self.to_public(record) for record in records], total

    async def get(self, session: AsyncSession, channel_id: str) -> Channel:
        record = await session.get(Channel, channel_id)
        if record is None:
            raise not_found(f"未找到渠道 {channel_id}")
        return record

    async def enabled_channels(self, session: AsyncSession) -> list[Channel]:
        result = await session.execute(
            select(Channel).where(Channel.status == STATUS_ACTIVE).order_by(Channel.priority.asc())
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ 写入
    async def create(self, session: AsyncSession, payload: dict[str, Any]) -> Channel:
        provider = normalize_provider(payload.get("provider_type"))
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise bad_request("上游 API Key 不能为空", param="api_key")
        base_url = str(payload.get("base_url") or "").strip()
        if not base_url and provider == "openai-compatible":
            raise bad_request("OpenAI 兼容渠道必须填写 base_url", param="base_url")

        record = Channel(
            name=str(payload.get("name") or provider).strip(),
            provider_type=provider,
            base_url=base_url,
            api_key_enc=self.cipher.encrypt(api_key),
        )
        self._apply_editable(record, payload)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        log.info("已创建渠道 %s（%s）", record.name, record.provider_type)
        return record

    async def update(self, session: AsyncSession, channel_id: str, payload: dict[str, Any]) -> Channel:
        record = await self.get(session, channel_id)
        if payload.get("provider_type"):
            record.provider_type = normalize_provider(payload["provider_type"])
        if payload.get("name") is not None:
            record.name = str(payload["name"]).strip() or record.name
        if payload.get("base_url") is not None:
            record.base_url = str(payload["base_url"]).strip()
        api_key = payload.get("api_key")
        if api_key:
            record.api_key_enc = self.cipher.encrypt(str(api_key).strip())
        self._apply_editable(record, payload)
        await session.commit()
        await session.refresh(record)
        return record

    async def delete(self, session: AsyncSession, channel_id: str) -> None:
        record = await self.get(session, channel_id)
        await session.delete(record)
        await session.commit()
        log.info("已删除渠道 %s", record.name)

    async def mark_success(self, session: AsyncSession, channel_id: str) -> None:
        record = await session.get(Channel, channel_id)
        if record is None:
            return
        record.last_ok_at = utcnow()
        record.consecutive_failures = 0
        await session.commit()

    async def mark_failure(self, session: AsyncSession, channel_id: str, message: str) -> None:
        record = await session.get(Channel, channel_id)
        if record is None:
            return
        record.last_error = message[:500]
        record.last_error_at = utcnow()
        record.consecutive_failures = int(record.consecutive_failures or 0) + 1
        await session.commit()

    def _apply_editable(self, record: Channel, payload: dict[str, Any]) -> None:
        if payload.get("status") is not None:
            status = str(payload["status"])
            if status not in (STATUS_ACTIVE, STATUS_DISABLED):
                raise bad_request("状态只能是 active 或 disabled", param="status")
            record.status = status
        for src, column in (("priority", "priority"), ("weight", "weight")):
            if payload.get(src) is not None:
                try:
                    value = int(payload[src])
                except (TypeError, ValueError):
                    raise bad_request(f"{src} 必须是整数", param=src) from None
                if column == "weight" and value < 0:
                    raise bad_request("权重不能为负数", param="weight")
                setattr(record, column, value)
        if payload.get("note") is not None:
            record.note = str(payload["note"])
        if payload.get("models") is not None:
            record.models = _json_list(payload["models"], "models")
        if payload.get("balance_url") is not None:
            record.balance_url = str(payload["balance_url"]).strip()
        if payload.get("balance_json_path") is not None:
            record.balance_json_path = str(payload["balance_json_path"]).strip()
        if payload.get("balance_currency") is not None:
            record.balance_currency = str(payload["balance_currency"]).strip()
        if payload.get("extra_headers") is not None:
            record.extra_headers = _json_object(payload["extra_headers"], "extra_headers")
        if payload.get("extra_body") is not None:
            record.extra_body = _json_object(payload["extra_body"], "extra_body")
        if payload.get("capabilities") is not None:
            from ..adapters.base import normalize_capabilities
            from ..adapters.registry import get_adapter_class

            provider = normalize_provider(record.provider_type)
            supported = tuple(get_adapter_class(provider).capabilities)
            wanted = normalize_capabilities(payload["capabilities"], fallback=supported)
            unknown = [cap for cap in wanted if cap not in supported]
            if unknown:
                raise bad_request(
                    "该协议不支持这些能力：" + "、".join(unknown)
                    + "；它支持：" + "、".join(supported),
                    param="capabilities",
                )
            record.capabilities = json.dumps(wanted, ensure_ascii=False)
        if payload.get("lifecycle") is not None:
            from .supervisor import merge_lifecycle

            raw = payload["lifecycle"]
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw or "{}")
                except ValueError:
                    raise bad_request("本地进程配置不是合法 JSON", param="lifecycle") from None
            if not isinstance(raw, dict):
                raise bad_request("本地进程配置应为 JSON 对象", param="lifecycle")
            config = merge_lifecycle(raw)
            if config["enabled"] and not config["command"]:
                raise bad_request("启用本地服务托管时必须填写启动命令", param="lifecycle.command")
            if config["stop_strategy"] == "command" and not config["stop_command"]:
                raise bad_request("停止策略选择「执行停止命令」时必须填写停止命令", param="lifecycle.stop_command")
            record.lifecycle = json.dumps(config, ensure_ascii=False)
        if payload.get("timeout_seconds") is not None:
            raw = payload["timeout_seconds"]
            if raw in ("", 0):
                record.timeout_seconds = None
            else:
                try:
                    record.timeout_seconds = max(1.0, float(raw))
                except (TypeError, ValueError):
                    raise bad_request("超时必须是数字", param="timeout_seconds") from None

    # ------------------------------------------------------------------ 适配器
    def adapter_for(self, channel: Channel) -> BaseAdapter:
        api_key = self.cipher.try_decrypt(channel.api_key_enc or "")
        if not api_key and channel.api_key_enc:
            raise RelayError(
                ErrorCode.UPSTREAM_ERROR,
                f"渠道「{channel.name}」的上游密钥无法解密，请重新填写其 API Key",
                status=500,
                retryable=False,
            )
        return create_adapter(
            channel.provider_type,
            api_key=api_key,
            base_url=channel.base_url,
            extra_headers=channel.header_map(),
            extra_body=channel.body_map(),
        )

    # ------------------------------------------------------------------ 序列化
    @staticmethod
    def to_public(record: Channel, *, reveal_key: bool = False) -> dict[str, Any]:
        plain_hint = ""
        return {
            "channel_id": record.channel_id,
            "name": record.name,
            "provider_type": record.provider_type,
            "base_url": record.base_url,
            "api_key_hint": plain_hint if reveal_key else "••••••",
            "has_api_key": bool(record.api_key_enc),
            "priority": record.priority,
            "weight": record.weight,
            "status": record.status,
            "models": record.model_patterns(),
            "lifecycle": record.lifecycle_config(),
            "managed": record.lifecycle_config()["enabled"],
            # 配置原始值 + 生效值（生效值 = 协议能力 ∩ 配置）
            "capabilities": record.capability_list(),
            "effective_capabilities": sorted(
                channel_capabilities(record)
            ),
            "balance_url": record.balance_url,
            "balance_json_path": record.balance_json_path,
            "balance_currency": record.balance_currency,
            "extra_headers": record.header_map(),
            "extra_body": record.body_map(),
            "timeout_seconds": record.timeout_seconds,
            "note": record.note,
            "created_at": to_iso(record.created_at),
            "updated_at": to_iso(record.updated_at),
            "last_ok_at": to_iso(record.last_ok_at),
            "last_error": record.last_error,
            "last_error_at": to_iso(record.last_error_at),
            "consecutive_failures": record.consecutive_failures,
        }

    def masked_key_of(self, record: Channel) -> str:
        return mask_secret(self.cipher.try_decrypt(record.api_key_enc or ""))

    # ------------------------------------------------------------------ 自检
    async def probe(
        self,
        channel: Channel,
        *,
        model: str,
        client: httpx.AsyncClient,
        max_tokens: int = 128,
    ) -> dict[str, Any]:
        """对渠道做一次最小化调用，返回连通性、延迟与回包摘要。

        max_tokens 默认给 128：新一代推理模型会把预算先花在 reasoning 上，
        给太小会得到「连接正常但正文为空」的误导性结果。
        """
        adapter = self.adapter_for(channel)
        if not model:
            model = (channel.model_patterns() or ["gpt-4o-mini"])[0]
            if "*" in model:
                model = model.replace("*", "mini")
        request = ChatRequest.from_body(
            {
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": max(8, int(max_tokens)),
                "stream": False,
            }
        )
        call = adapter.build_chat_call(
            request, model, defaults={"max_tokens": self.settings.get_int("gateway.default_max_tokens", 4096)}
        )
        started = time.perf_counter()
        try:
            response = await client.send(call.to_request())
        except httpx.TimeoutException as exc:
            return {
                "ok": False,
                "code": ErrorCode.UPSTREAM_TIMEOUT,
                "message": f"请求超时：{exc}",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        except httpx.HTTPError as exc:
            return {
                "ok": False,
                "code": ErrorCode.UPSTREAM_ERROR,
                "message": f"网络错误：{exc}",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        latency = round((time.perf_counter() - started) * 1000, 1)
        body = await response.aread()
        if response.status_code >= 400:
            error = adapter.translate_error(response.status_code, body)
            return {
                "ok": False,
                "code": error.code,
                "http_status": response.status_code,
                "message": error.message,
                "latency_ms": latency,
            }
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return {"ok": True, "message": "上游返回非 JSON，但状态码正常", "latency_ms": latency}
        normalized = adapter.normalize_response(payload, request, model)
        choice = (normalized.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or "").strip()
        return {
            "ok": True,
            "message": "连通正常" + ("" if content else "（仅返回思考内容，可调大 max_tokens）"),
            "latency_ms": latency,
            "model": model,
            "reply": (content or reasoning)[:160],
            "reasoning_only": bool(reasoning and not content),
            "finish_reason": choice.get("finish_reason"),
            "usage": normalized.get("usage"),
        }

    async def fetch_models(self, channel: Channel, *, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """拉某个渠道的上游模型列表（手动触发，永远实时取），顺便更新缓存。"""
        items = await self._list_models(self.adapter_for(channel), client)
        self._models_cache[channel.channel_id] = (utcnow(), items)
        return items

    async def fetch_models_preview(
        self,
        *,
        provider_type: str,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """按「还没落库的表单值」拉一次上游模型列表。

        新建渠道时渠道 id 还不存在，拿不到记录，但用户恰恰在这个时刻最需要候选
        （白名单就是照着上游真实 id 填的），所以这里按表单里的协议/地址/密钥现建一个适配器。
        """
        adapter = create_adapter(
            normalize_provider(provider_type),
            api_key=api_key,
            base_url=base_url,
            extra_headers=extra_headers or {},
            extra_body=extra_body or {},
        )
        return await self._list_models(adapter, client)

    async def _list_models(self, adapter: BaseAdapter, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        call = adapter.build_models_call()
        if call is None:
            return []
        response = await client.send(call.to_request())
        body = await response.aread()
        if response.status_code >= 400:
            error = adapter.translate_error(response.status_code, body)
            raise RelayError(error.code, error.message, status=error.status, retryable=False)
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return []
        return adapter.normalize_models(payload)

    async def models_catalog(
        self, session: AsyncSession, *, client: httpx.AsyncClient | None, refresh: bool = False
    ) -> list[dict[str, Any]]:
        """每个渠道一条：渠道白名单 + 上游真实模型列表（带缓存）。

        refresh=False 时只读缓存（进表单时调用，必须秒回）；refresh=True 时并发去拉。
        单个渠道失败只记在自己的 upstream_error 里，不影响别的渠道——上游限流、离线、
        协议不支持 /models 都很常见，不该让整张目录空掉。
        """
        channels = list((await session.execute(select(Channel).order_by(Channel.priority, Channel.name))).scalars().all())
        ttl = max(60, self.settings.get_int("services.catalog_ttl_seconds", 600))
        now = utcnow()

        def is_stale(channel: Channel) -> bool:
            cached = self._models_cache.get(channel.channel_id)
            if cached is None:
                return True
            return (now - cached[0]).total_seconds() > ttl

        if refresh and client is not None:
            pending = [c for c in channels if c.status == STATUS_ACTIVE]

            async def pull(channel: Channel) -> None:
                try:
                    items = await self._list_models(self.adapter_for(channel), client)
                except Exception as error:  # noqa: BLE001 —— 单渠道失败不拖垮目录
                    log.info("模型目录：渠道「%s」拉取上游列表失败：%s", channel.name, error)
                    self._models_errors[channel.channel_id] = str(error)
                    # 失败也要记时刻，否则每次进表单都会重试一遍慢上游
                    self._models_cache.setdefault(channel.channel_id, (now, []))
                    self._models_cache[channel.channel_id] = (now, self._models_cache[channel.channel_id][1])
                    return
                self._models_cache[channel.channel_id] = (now, items)
                self._models_errors.pop(channel.channel_id, None)

            if pending:
                await asyncio.gather(*(pull(channel) for channel in pending))

        entries: list[dict[str, Any]] = []
        for channel in channels:
            cached = self._models_cache.get(channel.channel_id)
            upstream = list(cached[1]) if cached else []
            entries.append({
                "channel_id": channel.channel_id,
                "name": channel.name,
                "provider_type": channel.provider_type,
                "status": channel.status,
                "models": channel.model_patterns(),
                "upstream": upstream,
                # 下拉候选只认 id；owned_by 这类字段留给单渠道的模型列表接口
                "upstream_ids": [str(item.get("id") or "") for item in upstream if item.get("id")],
                "upstream_at": to_iso(cached[0]) if cached else "",
                "upstream_stale": is_stale(channel),
                "upstream_error": self._models_errors.get(channel.channel_id, ""),
            })
        return entries


def _json_list(value: Any, field: str) -> str:
    if isinstance(value, str):
        items = [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        raise bad_request(f"{field} 应为字符串数组", param=field)
    return json.dumps(items, ensure_ascii=False)


def _json_object(value: Any, field: str) -> str:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "{}"
        try:
            parsed = json.loads(text)
        except ValueError:
            raise bad_request(f"{field} 不是合法 JSON", param=field) from None
    elif isinstance(value, dict):
        parsed = value
    else:
        raise bad_request(f"{field} 应为 JSON 对象", param=field)
    if not isinstance(parsed, dict):
        raise bad_request(f"{field} 应为 JSON 对象", param=field)
    return json.dumps(parsed, ensure_ascii=False)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None
