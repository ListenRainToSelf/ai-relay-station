"""用量计量与统计（方案 6.1 / 7.2 节）。

每次请求落一行 usage_logs 明细（权威口径，含时间戳与速度指标），
同时按 key × model × 分钟桶 upsert 到 api_key_stats，供控制台秒级出图。
明细与聚合相互印证，避免报表口径漂移。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import ApiKey, ApiKeyStat, UsageLog
from ..timeutil import bucket_start, to_iso, utcnow

log = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    request_id: str
    key_id: str = ""
    key_name: str = ""
    key_prefix: str = ""
    channel_id: str = ""
    channel_name: str = ""
    provider_type: str = ""
    model: str = ""
    upstream_model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: float = 0.0
    first_token_ms: float = 0.0
    speed_tok_s: float = 0.0
    stream: bool = False
    attempts: int = 1
    status: str = "ok"
    error_code: str = ""
    cost_units: int = 0
    client_ip: str = ""
    user_agent: str = ""
    ts: datetime = field(default_factory=utcnow)

    def normalized(self) -> "UsageRecord":
        if not self.total_tokens:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self


class UsageService:
    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
        self._pending: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ 写入
    async def record(self, record: UsageRecord) -> None:
        """写明细 + 预聚合 + 更新密钥冗余计数（单事务）。"""
        record = record.normalized()
        async with self.session_factory() as session:
            session.add(
                UsageLog(
                    request_id=record.request_id,
                    ts=record.ts,
                    key_id=record.key_id or None,
                    key_name=record.key_name,
                    key_prefix=record.key_prefix,
                    channel_id=record.channel_id or None,
                    channel_name=record.channel_name,
                    provider_type=record.provider_type,
                    model=record.model,
                    upstream_model=record.upstream_model,
                    prompt_tokens=record.prompt_tokens,
                    completion_tokens=record.completion_tokens,
                    total_tokens=record.total_tokens,
                    latency_ms=record.latency_ms,
                    first_token_ms=record.first_token_ms,
                    speed_tok_s=record.speed_tok_s,
                    stream=record.stream,
                    attempts=record.attempts,
                    status=record.status,
                    error_code=record.error_code,
                    cost_units=record.cost_units,
                    client_ip=record.client_ip,
                    user_agent=record.user_agent[:255],
                )
            )
            if record.key_id:
                await self._upsert_stats(session, record)
                await self._bump_key(session, record)
            await session.commit()

    async def _upsert_stats(self, session: AsyncSession, record: UsageRecord) -> None:
        bucket = bucket_start(record.ts, 1)
        model = record.model or record.upstream_model or "(unknown)"
        statement = sqlite_insert(ApiKeyStat).values(
            key_id=record.key_id,
            model=model,
            bucket_start=bucket,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            total_tokens=record.total_tokens,
            requests=1,
            errors=1 if record.status != "ok" else 0,
            cost_units=record.cost_units,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[ApiKeyStat.key_id, ApiKeyStat.model, ApiKeyStat.bucket_start],
            set_={
                "prompt_tokens": ApiKeyStat.prompt_tokens + record.prompt_tokens,
                "completion_tokens": ApiKeyStat.completion_tokens + record.completion_tokens,
                "total_tokens": ApiKeyStat.total_tokens + record.total_tokens,
                "requests": ApiKeyStat.requests + 1,
                "errors": ApiKeyStat.errors + (1 if record.status != "ok" else 0),
                "cost_units": ApiKeyStat.cost_units + record.cost_units,
            },
        )
        await session.execute(statement)

    async def _bump_key(self, session: AsyncSession, record: UsageRecord) -> None:
        key = await session.get(ApiKey, record.key_id)
        if key is None:
            return
        key.quota_used = int(key.quota_used or 0) + int(record.cost_units or 0)
        key.total_requests = int(key.total_requests or 0) + 1
        key.last_used_at = record.ts

    def record_soon(self, record: UsageRecord) -> None:
        """异步落库：不阻塞响应（方案 12 节「用量打点走异步 task」）。"""
        task = asyncio.create_task(self._record_safe(record))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _record_safe(self, record: UsageRecord) -> None:
        for attempt in range(3):
            try:
                await self.record(record)
                return
            except Exception:
                if attempt >= 2:
                    log.exception("用量落库失败（已重试 3 次）：request_id=%s", record.request_id)
                    return
                await asyncio.sleep(0.2 * (attempt + 1))

    async def drain(self, timeout: float = 5.0) -> None:
        """等待在途落库任务结束（关机 / 测试收尾用）。"""
        if not self._pending:
            return
        pending = list(self._pending)
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("仍有 %d 个用量落库任务未完成", len(self._pending))

    # ------------------------------------------------------------------ 查询
    async def overview(self, *, hours: float = 24.0) -> dict[str, Any]:
        since = utcnow() - timedelta(hours=hours)
        async with self.session_factory() as session:
            stmt = select(
                func.count().label("requests"),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.cost_units), 0),
                func.coalesce(func.sum(UsageLog.latency_ms), 0.0),
                func.coalesce(func.sum(UsageLog.first_token_ms), 0.0),
                func.coalesce(func.sum(UsageLog.speed_tok_s), 0.0),
            ).where(UsageLog.ts >= since)
            row = (await session.execute(stmt)).one()
            requests = int(row[0] or 0)
            errors = int(
                (
                    await session.execute(
                        select(func.count()).select_from(UsageLog).where(
                            UsageLog.ts >= since, UsageLog.status != "ok"
                        )
                    )
                ).scalar_one()
                or 0
            )
            streamed = int(
                (
                    await session.execute(
                        select(func.count()).select_from(UsageLog).where(
                            UsageLog.ts >= since, UsageLog.stream.is_(True)
                        )
                    )
                ).scalar_one()
                or 0
            )
            first_token_rows = int(
                (
                    await session.execute(
                        select(func.count()).select_from(UsageLog).where(
                            UsageLog.ts >= since, UsageLog.first_token_ms > 0
                        )
                    )
                ).scalar_one()
                or 0
            )
            speed_rows = int(
                (
                    await session.execute(
                        select(func.count()).select_from(UsageLog).where(
                            UsageLog.ts >= since, UsageLog.speed_tok_s > 0
                        )
                    )
                ).scalar_one()
                or 0
            )
            key_count = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(UsageLog.key_id))).where(UsageLog.ts >= since)
                    )
                ).scalar_one()
                or 0
            )
        return {
            "window_hours": hours,
            "requests": requests,
            "ok": requests - errors,
            "errors": errors,
            "error_rate": round(errors / requests * 100, 2) if requests else 0.0,
            "streamed": streamed,
            "prompt_tokens": int(row[1] or 0),
            "completion_tokens": int(row[2] or 0),
            "total_tokens": int(row[3] or 0),
            "cost_units": int(row[4] or 0),
            "avg_latency_ms": round(float(row[5] or 0) / requests, 1) if requests else 0.0,
            "avg_first_token_ms": round(float(row[6] or 0) / first_token_rows, 1) if first_token_rows else 0.0,
            "avg_speed_tok_s": round(float(row[7] or 0) / speed_rows, 2) if speed_rows else 0.0,
            "active_keys": key_count,
        }

    async def series(self, *, hours: float = 24.0, bucket: str = "hour", key_id: str = "") -> list[dict[str, Any]]:
        """时间序列：直接读预聚合桶（方案 7.2 的分模型时间戳设计）。"""
        since = utcnow() - timedelta(hours=hours)
        fmt = "%Y-%m-%dT%H:00:00Z" if bucket == "hour" else "%Y-%m-%dT00:00:00Z"
        sql = """
            SELECT strftime(:fmt, bucket_start) AS bucket,
                   SUM(requests) AS requests,
                   SUM(errors) AS errors,
                   SUM(total_tokens) AS tokens,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cost_units) AS cost_units
            FROM api_key_stats
            WHERE bucket_start >= :since
        """
        params: dict[str, Any] = {"fmt": fmt, "since": since.isoformat(sep=" ")}
        if key_id:
            sql += " AND key_id = :key_id"
            params["key_id"] = key_id
        sql += " GROUP BY bucket ORDER BY bucket ASC"
        async with self.session_factory() as session:
            rows = (await session.execute(text(sql), params)).mappings().all()
        return [
            {
                "bucket": row["bucket"],
                "requests": int(row["requests"] or 0),
                "errors": int(row["errors"] or 0),
                "tokens": int(row["tokens"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "cost_units": int(row["cost_units"] or 0),
            }
            for row in rows
        ]

    async def by_model(self, *, hours: float = 24.0, limit: int = 50, key_id: str = "") -> list[dict[str, Any]]:
        since = utcnow() - timedelta(hours=hours)
        sql = """
            SELECT model,
                   SUM(requests) AS requests,
                   SUM(errors) AS errors,
                   SUM(total_tokens) AS tokens,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(cost_units) AS cost_units
            FROM api_key_stats WHERE bucket_start >= :since
        """
        params: dict[str, Any] = {"since": since.isoformat(sep=" "), "limit": limit}
        if key_id:
            sql += " AND key_id = :key_id"
            params["key_id"] = key_id
        sql += " GROUP BY model ORDER BY requests DESC LIMIT :limit"
        async with self.session_factory() as session:
            rows = (await session.execute(text(sql), params)).mappings().all()
        return [
            {
                "model": row["model"],
                "requests": int(row["requests"] or 0),
                "errors": int(row["errors"] or 0),
                "tokens": int(row["tokens"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "cost_units": int(row["cost_units"] or 0),
            }
            for row in rows
        ]

    async def by_key(self, *, hours: float = 24.0, limit: int = 50) -> list[dict[str, Any]]:
        since = utcnow() - timedelta(hours=hours)
        sql = """
            SELECT s.key_id AS key_id,
                   COALESCE(k.name, '') AS name,
                   COALESCE(k.prefix, '') AS prefix,
                   SUM(s.requests) AS requests,
                   SUM(s.errors) AS errors,
                   SUM(s.total_tokens) AS tokens,
                   SUM(s.cost_units) AS cost_units
            FROM api_key_stats s
            LEFT JOIN api_keys k ON k.key_id = s.key_id
            WHERE s.bucket_start >= :since
            GROUP BY s.key_id ORDER BY requests DESC LIMIT :limit
        """
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    text(sql), {"since": since.isoformat(sep=" "), "limit": limit}
                )
            ).mappings().all()
        return [
            {
                "key_id": row["key_id"],
                "name": row["name"],
                "prefix": row["prefix"],
                "requests": int(row["requests"] or 0),
                "errors": int(row["errors"] or 0),
                "tokens": int(row["tokens"] or 0),
                "cost_units": int(row["cost_units"] or 0),
            }
            for row in rows
        ]

    async def by_channel(self, *, hours: float = 24.0, limit: int = 50) -> list[dict[str, Any]]:
        since = utcnow() - timedelta(hours=hours)
        sql = """
            SELECT channel_id,
                   COALESCE(MAX(channel_name), '') AS channel_name,
                   COALESCE(MAX(provider_type), '') AS provider_type,
                   COUNT(*) AS requests,
                   SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(cost_units), 0) AS cost_units,
                   COALESCE(AVG(latency_ms), 0) AS avg_latency_ms,
                   COALESCE(AVG(speed_tok_s), 0) AS avg_speed_tok_s
            FROM usage_logs WHERE ts >= :since
            GROUP BY channel_id ORDER BY requests DESC LIMIT :limit
        """
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    text(sql), {"since": since.isoformat(sep=" "), "limit": limit}
                )
            ).mappings().all()
        return [
            {
                "channel_id": row["channel_id"],
                "channel_name": row["channel_name"],
                "provider_type": row["provider_type"],
                "requests": int(row["requests"] or 0),
                "errors": int(row["errors"] or 0),
                "tokens": int(row["tokens"] or 0),
                "cost_units": int(row["cost_units"] or 0),
                "avg_latency_ms": round(float(row["avg_latency_ms"] or 0), 1),
                "avg_speed_tok_s": round(float(row["avg_speed_tok_s"] or 0), 2),
            }
            for row in rows
        ]

    async def recent(self, *, limit: int = 50, key_id: str = "", status: str = "", model: str = "") -> list[dict[str, Any]]:
        stmt = select(UsageLog).order_by(UsageLog.ts.desc()).limit(limit)
        if key_id:
            stmt = stmt.where(UsageLog.key_id == key_id)
        if status:
            stmt = stmt.where(UsageLog.status == status)
        if model:
            stmt = stmt.where(UsageLog.model == model)
        async with self.session_factory() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [self.log_to_public(row) for row in rows]

    async def totals_for_key(self, key_id: str) -> dict[str, Any]:
        async with self.session_factory() as session:
            stmt = select(
                func.count(),
                func.coalesce(func.sum(UsageLog.total_tokens), 0),
                func.coalesce(func.sum(UsageLog.cost_units), 0),
            ).where(UsageLog.key_id == key_id)
            row = (await session.execute(stmt)).one()
        return {"requests": int(row[0] or 0), "tokens": int(row[1] or 0), "cost_units": int(row[2] or 0)}

    @staticmethod
    def log_to_public(row: UsageLog) -> dict[str, Any]:
        return {
            "log_id": row.log_id,
            "request_id": row.request_id,
            "ts": to_iso(row.ts),
            "key_id": row.key_id,
            "key_name": row.key_name,
            "key_prefix": row.key_prefix,
            "channel_id": row.channel_id,
            "channel_name": row.channel_name,
            "provider_type": row.provider_type,
            "model": row.model,
            "upstream_model": row.upstream_model,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "latency_ms": round(row.latency_ms or 0, 1),
            "first_token_ms": round(row.first_token_ms or 0, 1),
            "speed_tok_s": round(row.speed_tok_s or 0, 2),
            "stream": row.stream,
            "attempts": row.attempts,
            "status": row.status,
            "error_code": row.error_code,
            "cost_units": row.cost_units,
            "client_ip": row.client_ip,
        }
