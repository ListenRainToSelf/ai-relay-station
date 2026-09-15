"""模型别名映射（方案 9 节）。

对外别名 alias → 上游真实模型 upstream_model，可选绑定渠道与协议。
支持精确匹配与通配（`claude-*`、`*` 兜底），未命中则原样透传。
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import bad_request, not_found
from ..models import ModelMap
from ..timeutil import to_iso

log = logging.getLogger(__name__)


@dataclass
class ResolvedModel:
    requested: str
    upstream: str
    channel_id: str | None = None
    provider: str = ""
    alias: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "upstream": self.upstream,
            "channel_id": self.channel_id,
            "provider": self.provider,
            "alias": self.alias,
            "mapped": bool(self.alias),
        }


class ModelMapService:
    """带内存缓存的别名表，热路径上零查询。"""

    def __init__(self, *, wildcard_fallback_getter=None) -> None:
        self._exact: dict[str, tuple[str, str | None, str]] = {}
        self._patterns: list[tuple[str, str, str | None, str]] = []
        self._wildcard_fallback_getter = wildcard_fallback_getter or (lambda: "")

    # ------------------------------------------------------------------ 缓存
    async def refresh(self, session: AsyncSession) -> None:
        rows = (await session.execute(select(ModelMap).where(ModelMap.enabled.is_(True)))).scalars().all()
        exact: dict[str, tuple[str, str | None, str]] = {}
        patterns: list[tuple[str, str, str | None, str]] = []
        for row in rows:
            alias = (row.alias or "").strip()
            if not alias:
                continue
            entry = (row.upstream_model, row.channel_id or None, row.provider_type or "")
            if _is_pattern(alias):
                patterns.append((alias, *entry))
            else:
                exact[alias] = entry
        # 长的模式优先，避免 `*` 抢占 `claude-*`
        patterns.sort(key=lambda item: len(item[0]), reverse=True)
        self._exact = exact
        self._patterns = patterns
        log.debug("模型别名缓存已刷新：%d 条精确 / %d 条通配", len(exact), len(patterns))

    @property
    def size(self) -> int:
        return len(self._exact) + len(self._patterns)

    # ------------------------------------------------------------------ 解析
    def resolve(self, requested: str) -> ResolvedModel:
        model = (requested or "").strip()
        entry = self._exact.get(model)
        if entry is not None:
            return ResolvedModel(model, entry[0], entry[1], entry[2], alias=model)
        for pattern, upstream, channel_id, provider in self._patterns:
            if fnmatch.fnmatchcase(model, pattern):
                return ResolvedModel(model, upstream, channel_id, provider, alias=pattern)
        fallback = self._wildcard_fallback_getter() or ""
        upstream = fallback.strip() or model
        return ResolvedModel(model, upstream, None, "", alias=None)

    def known_aliases(self) -> list[str]:
        return sorted(self._exact.keys()) + [p[0] for p in self._patterns]

    # ------------------------------------------------------------------ CRUD
    async def list_maps(self, session: AsyncSession) -> list[dict[str, Any]]:
        rows = (await session.execute(select(ModelMap).order_by(ModelMap.alias.asc()))).scalars().all()
        return [self.to_public(row) for row in rows]

    async def get(self, session: AsyncSession, map_id: int) -> ModelMap:
        record = await session.get(ModelMap, map_id)
        if record is None:
            raise not_found(f"未找到模型映射 {map_id}")
        return record

    async def create(self, session: AsyncSession, payload: dict[str, Any]) -> ModelMap:
        alias = str(payload.get("alias") or "").strip()
        upstream = str(payload.get("upstream_model") or "").strip()
        if not alias:
            raise bad_request("别名不能为空", param="alias")
        if not upstream:
            raise bad_request("上游模型 id 不能为空", param="upstream_model")
        existing = (
            await session.execute(select(ModelMap).where(ModelMap.alias == alias))
        ).scalar_one_or_none()
        if existing is not None:
            raise bad_request(f"别名 {alias} 已存在", param="alias")
        record = ModelMap(
            alias=alias,
            upstream_model=upstream,
            channel_id=(str(payload["channel_id"]) if payload.get("channel_id") else None),
            provider_type=str(payload.get("provider_type") or ""),
            note=str(payload.get("note") or ""),
            enabled=bool(payload.get("enabled", True)),
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        await self.refresh(session)
        return record

    async def update(self, session: AsyncSession, map_id: int, payload: dict[str, Any]) -> ModelMap:
        record = await self.get(session, map_id)
        if payload.get("alias") is not None:
            alias = str(payload["alias"]).strip()
            if not alias:
                raise bad_request("别名不能为空", param="alias")
            clash = (
                await session.execute(
                    select(ModelMap).where(ModelMap.alias == alias, ModelMap.id != map_id)
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise bad_request(f"别名 {alias} 已存在", param="alias")
            record.alias = alias
        if payload.get("upstream_model") is not None:
            upstream = str(payload["upstream_model"]).strip()
            if not upstream:
                raise bad_request("上游模型 id 不能为空", param="upstream_model")
            record.upstream_model = upstream
        if "channel_id" in payload:
            record.channel_id = str(payload["channel_id"]) if payload["channel_id"] else None
        if payload.get("provider_type") is not None:
            record.provider_type = str(payload["provider_type"])
        if payload.get("note") is not None:
            record.note = str(payload["note"])
        if payload.get("enabled") is not None:
            record.enabled = bool(payload["enabled"])
        await session.commit()
        await session.refresh(record)
        await self.refresh(session)
        return record

    async def delete(self, session: AsyncSession, map_id: int) -> None:
        record = await self.get(session, map_id)
        await session.delete(record)
        await session.commit()
        await self.refresh(session)

    async def bulk_import(self, session: AsyncSession, entries: list[dict[str, Any]]) -> dict[str, int]:
        created = 0
        updated = 0
        for entry in entries:
            alias = str(entry.get("alias") or "").strip()
            upstream = str(entry.get("upstream_model") or "").strip()
            if not alias or not upstream:
                continue
            existing = (
                await session.execute(select(ModelMap).where(ModelMap.alias == alias))
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    ModelMap(
                        alias=alias,
                        upstream_model=upstream,
                        channel_id=(str(entry["channel_id"]) if entry.get("channel_id") else None),
                        provider_type=str(entry.get("provider_type") or ""),
                        note=str(entry.get("note") or ""),
                    )
                )
                created += 1
            else:
                existing.upstream_model = upstream
                if entry.get("channel_id") is not None:
                    existing.channel_id = str(entry["channel_id"]) or None
                if entry.get("provider_type") is not None:
                    existing.provider_type = str(entry["provider_type"])
                updated += 1
        await session.commit()
        await self.refresh(session)
        return {"created": created, "updated": updated}

    async def count(self, session: AsyncSession) -> int:
        return int((await session.execute(select(func.count()).select_from(ModelMap))).scalar_one())

    @staticmethod
    def to_public(record: ModelMap) -> dict[str, Any]:
        return {
            "id": record.id,
            "alias": record.alias,
            "upstream_model": record.upstream_model,
            "channel_id": record.channel_id,
            "provider_type": record.provider_type,
            "note": record.note,
            "enabled": record.enabled,
            "wildcard": _is_pattern(record.alias),
            "created_at": to_iso(record.created_at),
            "updated_at": to_iso(record.updated_at),
        }


def _is_pattern(alias: str) -> bool:
    return any(char in alias for char in "*?[")
