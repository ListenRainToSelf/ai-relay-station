"""渠道余额查询（方案 10 节）。

按 provider_type 取适配器的余额端点，做缓存与定时刷新；
没有公开余额接口的厂商标记「无余额接口」而不是报错。
口径上与「本机 Key 消费统计」严格分开展示。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import BalanceSnapshot, Channel
from ..settings import SettingsService
from ..timeutil import to_iso, utcnow
from .channels import ChannelService

log = logging.getLogger(__name__)


class BalanceService:
    def __init__(
        self,
        *,
        channels: ChannelService,
        settings: SettingsService,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.channels = channels
        self.settings = settings
        self.session_factory = session_factory

    # ------------------------------------------------------------------ 单渠道
    async def fetch(self, channel: Channel, client: httpx.AsyncClient) -> dict[str, Any]:
        adapter = self.channels.adapter_for(channel)
        call = adapter.build_balance_call(
            balance_url=channel.balance_url or "", json_path=channel.balance_json_path or ""
        )
        if call is None:
            return {
                "supported": False,
                "is_available": False,
                "currency": "",
                "total": 0.0,
                "granted": 0.0,
                "topped_up": 0.0,
                "message": "该渠道未配置余额查询接口",
                "raw": {},
            }
        try:
            response = await client.send(call.to_request())
            body = await response.aread()
        except httpx.TimeoutException:
            return _failure("余额查询超时")
        except httpx.HTTPError as exc:
            return _failure(f"余额查询网络错误：{exc}")
        if response.status_code >= 400:
            text = body.decode("utf-8", "replace")[:300]
            return _failure(f"余额接口返回 HTTP {response.status_code}：{text}")
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return _failure("余额接口返回内容不是 JSON")

        normalizer = getattr(adapter, "normalize_balance", None)
        if normalizer is None:
            return _failure("该渠道类型暂不支持余额解析")
        result = normalizer(payload if isinstance(payload, dict) else {"data": payload}, channel.balance_json_path or "")
        if channel.balance_currency:
            result["currency"] = channel.balance_currency
        result.setdefault("message", "")
        return result

    async def refresh(
        self, session: AsyncSession, client: httpx.AsyncClient, *, channel_id: str | None = None
    ) -> list[dict[str, Any]]:
        if channel_id:
            channels = [await self.channels.get(session, channel_id)]
        else:
            channels = await self.channels.enabled_channels(session)
        results: list[dict[str, Any]] = []
        for channel in channels:
            result = await self.fetch(channel, client)
            snapshot = BalanceSnapshot(
                channel_id=channel.channel_id,
                fetched_at=utcnow(),
                is_available=bool(result.get("is_available")),
                currency=str(result.get("currency") or ""),
                total=float(result.get("total") or 0.0),
                granted=float(result.get("granted") or 0.0),
                topped_up=float(result.get("topped_up") or 0.0),
                supported=bool(result.get("supported", True)),
                message=str(result.get("message") or ""),
                raw=json.dumps(result.get("raw") or {}, ensure_ascii=False)[:8000],
            )
            session.add(snapshot)
            results.append({**result, "channel_id": channel.channel_id, "channel_name": channel.name})
        await session.commit()
        return results

    # ------------------------------------------------------------------ 汇总
    async def overview(self, session: AsyncSession) -> list[dict[str, Any]]:
        channels = (await session.execute(select(Channel))).scalars().all()
        threshold = self.settings.get_float("balance.warn_threshold", 0.0)
        out: list[dict[str, Any]] = []
        for channel in channels:
            snapshot = (
                await session.execute(
                    select(BalanceSnapshot)
                    .where(BalanceSnapshot.channel_id == channel.channel_id)
                    .order_by(BalanceSnapshot.fetched_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            adapter_supported = getattr(
                self.channels.adapter_for(channel), "supports_balance", False
            ) if channel.api_key_enc else False
            has_custom = bool(channel.balance_url)
            total = float(snapshot.total) if snapshot else 0.0
            out.append(
                {
                    "channel_id": channel.channel_id,
                    "channel_name": channel.name,
                    "provider_type": channel.provider_type,
                    "status": channel.status,
                    "supported": bool(snapshot.supported) if snapshot else (adapter_supported or has_custom),
                    "configured": adapter_supported or has_custom,
                    "is_available": bool(snapshot.is_available) if snapshot else True,
                    "currency": snapshot.currency if snapshot else (channel.balance_currency or ""),
                    "total": total,
                    "granted": float(snapshot.granted) if snapshot else 0.0,
                    "topped_up": float(snapshot.topped_up) if snapshot else 0.0,
                    "fetched_at": to_iso(snapshot.fetched_at) if snapshot else None,
                    "message": snapshot.message if snapshot else "尚未查询",
                    "low": bool(threshold > 0 and snapshot and snapshot.supported and total <= threshold),
                    "stale": bool(
                        snapshot is None
                        or (utcnow() - snapshot.fetched_at)
                        > timedelta(minutes=max(1, self.settings.get_int("balance.refresh_minutes", 15)) * 3)
                    ),
                }
            )
        return out

    async def history(self, session: AsyncSession, channel_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = (
            await session.execute(
                select(BalanceSnapshot)
                .where(BalanceSnapshot.channel_id == channel_id)
                .order_by(BalanceSnapshot.fetched_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "fetched_at": to_iso(row.fetched_at),
                "total": row.total,
                "granted": row.granted,
                "topped_up": row.topped_up,
                "currency": row.currency,
                "is_available": row.is_available,
                "supported": row.supported,
                "message": row.message,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ 定时
    async def refresh_loop(self, client: httpx.AsyncClient, stop_event: asyncio.Event) -> None:
        # 启动后先等一会儿，避免和首屏加载抢资源
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=20)
            return
        except asyncio.TimeoutError:
            pass
        while not stop_event.is_set():
            try:
                if self.settings.get_bool("balance.auto_refresh", True):
                    async with self.session_factory() as session:
                        results = await self.refresh(session, client)
                    log.debug("余额自动刷新完成：%d 个渠道", len(results))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("余额自动刷新时代码异常")
            minutes = max(1, self.settings.get_int("balance.refresh_minutes", 15))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=minutes * 60)
                return
            except asyncio.TimeoutError:
                continue


def _failure(message: str) -> dict[str, Any]:
    return {
        "supported": True,
        "is_available": False,
        "currency": "",
        "total": 0.0,
        "granted": 0.0,
        "topped_up": 0.0,
        "message": message,
        "raw": {},
    }


def _dt(value: datetime | None) -> str | None:
    return to_iso(value)
