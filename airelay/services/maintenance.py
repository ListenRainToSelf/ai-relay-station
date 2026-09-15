"""后台维护任务：明细保留期清理 + SQLite 检查点。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..db import prune_usage_logs
from ..settings import SettingsService

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600


class MaintenanceService:
    def __init__(self, *, engine: AsyncEngine, settings: SettingsService) -> None:
        self.engine = engine
        self.settings = settings
        self.last_run: dict[str, Any] = {}

    async def run_once(self) -> dict[str, Any]:
        days = self.settings.get_int("logs.retention_days", 30)
        removed = await prune_usage_logs(self.engine, days)
        # WAL 定期截断，避免 -wal 文件无限增长（NAS 上尤其重要）
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        except Exception:
            log.debug("WAL checkpoint 失败", exc_info=True)
        self.last_run = {"removed_logs": removed, "retention_days": days}
        if removed:
            log.info("已清理 %d 条超期用量明细（保留 %d 天）", removed, days)
        return self.last_run

    async def loop(self, stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
            return
        except asyncio.TimeoutError:
            pass
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("维护任务执行失败")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=CHECK_INTERVAL_SECONDS)
                return
            except asyncio.TimeoutError:
                continue
