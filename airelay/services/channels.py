"""渠道服务：上游渠道的增删改查、适配器实例化、连通性自检。

对应方案 3.2 的「上游渠道层」与 7.1 的 channels 表。
上游 API Key 一律加密落库，对外序列化只回显掩码。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..adapters import BaseAdapter, ChatRequest, create_adapter, normalize_provider
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
        adapter = self.adapter_for(channel)
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
