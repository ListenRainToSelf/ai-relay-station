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

from sqlalchemy import func, select, text, update
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
        """累加密钥的已用额度与请求数。

        必须用 SQL 层自增（`SET quota_used = quota_used + n`）而不是先读后写：
        同一个 Key 的并发请求各开一个事务，先读后写会互相覆盖（两个请求都读到 0、
        各自写 18，最终只剩 18 而不是 36），账就少了。
        """
        await session.execute(
            update(ApiKey)
            .where(ApiKey.key_id == record.key_id)
            .values(
                quota_used=func.coalesce(ApiKey.quota_used, 0) + int(record.cost_units or 0),
                total_requests=func.coalesce(ApiKey.total_requests, 0) + 1,
                last_used_at=record.ts,
            )
        )

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
    @staticmethod
    def _log_conditions(since: datetime, key_id: str = "", model: str = "") -> list[Any]:
        """用量明细的过滤条件：时间窗 + 可选「某个本地密钥」「某个模型」。"""
        conditions: list[Any] = [UsageLog.ts >= since]
        if key_id:
            conditions.append(UsageLog.key_id == key_id)
        if model:
            conditions.append(UsageLog.model == model)
        return conditions

    @staticmethod
    def _stat_conditions(since: datetime, key_id: str = "", model: str = "") -> list[Any]:
        conditions: list[Any] = [ApiKeyStat.bucket_start >= since]
        if key_id:
            conditions.append(ApiKeyStat.key_id == key_id)
        if model:
            conditions.append(ApiKeyStat.model == model)
        return conditions

    async def options(self, *, hours: float = 24.0) -> dict[str, Any]:
        """控制台筛选下拉的候选项：本机密钥 + 窗口内出现过的模型。"""
        since = utcnow() - timedelta(hours=hours)
        async with self.session_factory() as session:
            keys = (
                await session.execute(select(ApiKey.key_id, ApiKey.name, ApiKey.prefix).order_by(ApiKey.created_at))
            ).all()
            models = (
                await session.execute(
                    select(ApiKeyStat.model)
                    .where(ApiKeyStat.bucket_start >= since)
                    .group_by(ApiKeyStat.model)
                    .order_by(func.sum(ApiKeyStat.total_tokens).desc())
                )
            ).scalars().all()
            if not models:
                models = (
                    await session.execute(
                        select(UsageLog.model)
                        .where(UsageLog.ts >= since)
                        .group_by(UsageLog.model)
                        .order_by(func.count().desc())
                    )
                ).scalars().all()
        return {
            "keys": [
                {"key_id": row.key_id, "name": row.name or row.prefix, "prefix": row.prefix} for row in keys
            ],
            "models": [str(model) for model in models if model],
        }

    async def overview(
        self, *, hours: float = 24.0, key_id: str = "", model: str = ""
    ) -> dict[str, Any]:
        since = utcnow() - timedelta(hours=hours)
        conditions = self._log_conditions(since, key_id, model)
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
            ).where(*conditions)
            row = (await session.execute(stmt)).one()
            requests = int(row[0] or 0)

            async def count_where(*extra: Any) -> int:
                value = (
                    await session.execute(
                        select(func.count()).select_from(UsageLog).where(*conditions, *extra)
                    )
                ).scalar_one()
                return int(value or 0)

            errors = await count_where(UsageLog.status != "ok")
            streamed = await count_where(UsageLog.stream.is_(True))
            first_token_rows = await count_where(UsageLog.first_token_ms > 0)
            speed_rows = await count_where(UsageLog.speed_tok_s > 0)
            key_count = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(UsageLog.key_id))).where(*conditions)
                    )
                ).scalar_one()
                or 0
            )
        return {
            "window_hours": hours,
            "filter": {"key_id": key_id, "model": model},
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

    async def series(
        self, *, hours: float = 24.0, bucket: str = "hour", key_id: str = "", model: str = ""
    ) -> list[dict[str, Any]]:
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
        if model:
            sql += " AND model = :model"
            params["model"] = model
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

    async def series_by_model(
        self, *, hours: float = 24.0, bucket: str = "hour", key_id: str = "", limit: int = 6
    ) -> dict[str, Any]:
        """按模型拆分的多条时间序列，供「按模型对比」折线图使用；

        只取窗口内 token 量最大的前 N 个模型，避免图例被长尾撑爆。
        """
        since = utcnow() - timedelta(hours=hours)
        fmt = "%Y-%m-%dT%H:00:00Z" if bucket == "hour" else "%Y-%m-%dT00:00:00Z"
        sql = """
            SELECT strftime(:fmt, bucket_start) AS bucket,
                   model,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS tokens,
                   SUM(requests) AS requests,
                   SUM(errors) AS errors,
                   SUM(cost_units) AS cost_units
            FROM api_key_stats
            WHERE bucket_start >= :since
        """
        params: dict[str, Any] = {"fmt": fmt, "since": since.isoformat(sep=" ")}
        if key_id:
            sql += " AND key_id = :key_id"
            params["key_id"] = key_id
        sql += " GROUP BY bucket, model ORDER BY bucket ASC"
        async with self.session_factory() as session:
            rows = (await session.execute(text(sql), params)).mappings().all()

        buckets: list[str] = []
        totals: dict[str, int] = {}
        table: dict[str, dict[str, dict[str, Any]]] = {}
        for row in rows:
            name = str(row["model"])
            bucket_name = str(row["bucket"])
            if bucket_name not in buckets:
                buckets.append(bucket_name)
            totals[name] = totals.get(name, 0) + int(row["tokens"] or 0)
            table.setdefault(name, {})[bucket_name] = {
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "tokens": int(row["tokens"] or 0),
                "requests": int(row["requests"] or 0),
                "errors": int(row["errors"] or 0),
                "cost_units": int(row["cost_units"] or 0),
            }
        top = sorted(totals, key=lambda name: totals[name], reverse=True)[: max(1, limit)]
        palette = ["var(--cyan)", "var(--emerald)", "var(--amber)", "var(--violet)", "var(--rose)", "var(--ink-dim)"]
        series: list[dict[str, Any]] = []
        for index, name in enumerate(top):
            points = []
            for bucket_name in buckets:
                cell = table.get(name, {}).get(bucket_name, {})
                points.append(
                    {
                        "bucket": bucket_name,
                        "prompt_tokens": cell.get("prompt_tokens", 0),
                        "completion_tokens": cell.get("completion_tokens", 0),
                        "tokens": cell.get("tokens", 0),
                        "requests": cell.get("requests", 0),
                        "errors": cell.get("errors", 0),
                        "cost_units": cell.get("cost_units", 0),
                    }
                )
            series.append({"model": name, "color": palette[index % len(palette)], "points": points})
        return {"buckets": buckets, "series": series, "totals": totals}

    async def by_model(
        self, *, hours: float = 24.0, limit: int = 50, key_id: str = "", model: str = ""
    ) -> list[dict[str, Any]]:
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
        if model:
            sql += " AND model = :model"
            params["model"] = model
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

    async def by_key(
        self, *, hours: float = 24.0, limit: int = 50, key_id: str = "", model: str = ""
    ) -> list[dict[str, Any]]:
        """按本地密钥聚合；带上 model 过滤就能回答「某个模型是谁在用」。"""
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
        """
        params: dict[str, Any] = {"since": since.isoformat(sep=" "), "limit": limit}
        if key_id:
            sql += " AND s.key_id = :key_id"
            params["key_id"] = key_id
        if model:
            sql += " AND s.model = :model"
            params["model"] = model
        sql += " GROUP BY s.key_id ORDER BY requests DESC LIMIT :limit"
        async with self.session_factory() as session:
            rows = (await session.execute(text(sql), params)).mappings().all()
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

    async def by_channel(
        self, *, hours: float = 24.0, limit: int = 50, key_id: str = "", model: str = ""
    ) -> list[dict[str, Any]]:
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
        """
        params: dict[str, Any] = {"since": since.isoformat(sep=" "), "limit": limit}
        if key_id:
            sql += " AND key_id = :key_id"
            params["key_id"] = key_id
        if model:
            sql += " AND model = :model"
            params["model"] = model
        sql += " GROUP BY channel_id ORDER BY requests DESC LIMIT :limit"
        async with self.session_factory() as session:
            rows = (await session.execute(text(sql), params)).mappings().all()
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
