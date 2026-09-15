"""本地分发密钥服务：创建 / 校验 / 额度 / 模型授权。

对应方案 3.2 的「本地 Key 层」与 7.1 的 api_keys 表。
明文密钥只在创建时返回一次，库中仅存 prefix 与加 pepper 的哈希。
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..errors import ErrorCode, RelayError, bad_request, conflict, not_found
from ..models import ApiKey, UsageLog
from ..security import generate_local_key, hash_local_key, mask_key, parse_key_prefix, verify_local_key
from ..timeutil import parse_iso, utcnow

log = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"


@dataclass
class QuotaState:
    limit: int
    used: int
    remaining: int
    percent: float
    exceeded: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "percent": round(self.percent, 2),
            "exceeded": self.exceeded,
        }


class KeyService:
    def __init__(
        self,
        *,
        pepper: str,
        cipher: SecretBox | None = None,
        settings: Any = None,
    ) -> None:
        self.pepper = pepper
        # 有 cipher 才能把明文加密存库并在之后取回（平台式密钥管理）
        self.cipher = cipher
        self.settings = settings

    # ------------------------------------------------------------------ 明文存取
    @property
    def keep_plaintext(self) -> bool:
        if self.cipher is None:
            return False
        if self.settings is None:
            return True
        return self.settings.get_bool("security.keep_key_plaintext", True)

    def store_plaintext(self, plaintext: str) -> str:
        """按当前设置决定是否保存明文密文；返回要落库的值。"""
        if not self.keep_plaintext:
            return ""
        assert self.cipher is not None
        return self.cipher.encrypt(plaintext)

    def reveal(self, record: ApiKey) -> str:
        """取回明文；老数据（未存密文）或主密钥更换过则返回空串。"""
        if not record.key_enc or self.cipher is None:
            return ""
        return self.cipher.try_decrypt(record.key_enc)

    async def get_secret(self, session: AsyncSession, key_id: str) -> tuple[ApiKey, str]:
        record = await self.get(session, key_id)
        return record, self.reveal(record)

    # ------------------------------------------------------------------ 校验
    async def authenticate(self, session: AsyncSession, raw_key: str) -> ApiKey:
        """校验 Bearer 密钥，失败抛 INVALID_API_KEY / KEY_EXPIRED。"""
        token = (raw_key or "").strip()
        if not token:
            raise RelayError(ErrorCode.INVALID_API_KEY, "缺少 API Key：请在 Authorization 头里提供 Bearer 密钥")
        prefix = parse_key_prefix(token)
        if not prefix:
            raise RelayError(ErrorCode.INVALID_API_KEY, "API Key 格式不正确")
        result = await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        record = result.scalar_one_or_none()
        if record is None or not verify_local_key(token, record.key_hash, self.pepper):
            raise RelayError(ErrorCode.INVALID_API_KEY, "API Key 无效")
        if record.status != STATUS_ACTIVE:
            raise RelayError(ErrorCode.INVALID_API_KEY, "该 API Key 已被禁用")
        if record.expires_at is not None and record.expires_at <= utcnow():
            raise RelayError(
                ErrorCode.KEY_EXPIRED,
                f"该 API Key 已于 {record.expires_at.isoformat()} 过期",
            )
        return record

    # ------------------------------------------------------------------ 授权
    @staticmethod
    def model_allowed(key: ApiKey, model: str) -> bool:
        patterns = key.allowed_models()
        if not patterns:
            return True
        return any(fnmatch.fnmatchcase(model, pattern) for pattern in patterns)

    @staticmethod
    def ensure_model_allowed(key: ApiKey, model: str) -> None:
        if not KeyService.model_allowed(key, model):
            allowed = ", ".join(key.allowed_models()) or "（未配置）"
            raise RelayError(
                ErrorCode.MODEL_NOT_ALLOWED,
                f"该 API Key 无权使用模型 {model}；允许的模型：{allowed}",
                param="model",
            )

    @staticmethod
    def quota_state(key: ApiKey) -> QuotaState:
        limit = int(key.quota_limit or 0)
        used = int(key.quota_used or 0)
        if limit <= 0:
            return QuotaState(0, used, 0, 0.0, False)
        remaining = max(0, limit - used)
        percent = min(999.0, used / limit * 100.0)
        return QuotaState(limit, used, remaining, percent, used >= limit)

    @classmethod
    def ensure_quota(cls, key: ApiKey) -> None:
        state = cls.quota_state(key)
        if state.exceeded:
            raise RelayError(
                ErrorCode.QUOTA_EXCEEDED,
                "该 API Key 的额度已用尽，请在控制台调整配额",
            )

    # ------------------------------------------------------------------ 查询
    async def list_keys(
        self, session: AsyncSession, *, search: str = "", status: str = "", limit: int = 200, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        stmt = select(ApiKey)
        count_stmt = select(func.count()).select_from(ApiKey)
        if status:
            stmt = stmt.where(ApiKey.status == status)
            count_stmt = count_stmt.where(ApiKey.status == status)
        if search:
            like = f"%{search}%"
            condition = ApiKey.name.like(like) | ApiKey.prefix.like(like) | ApiKey.note.like(like)
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        stmt = stmt.order_by(ApiKey.created_at.desc()).limit(limit).offset(offset)
        records = (await session.execute(stmt)).scalars().all()
        total = int((await session.execute(count_stmt)).scalar_one())
        return [self.to_public(record) for record in records], total

    async def get(self, session: AsyncSession, key_id: str) -> ApiKey:
        record = await session.get(ApiKey, key_id)
        if record is None:
            raise not_found(f"未找到密钥 {key_id}")
        return record

    async def get_by_prefix(self, session: AsyncSession, prefix: str) -> ApiKey | None:
        result = await session.execute(select(ApiKey).where(ApiKey.prefix == prefix))
        return result.scalar_one_or_none()

    async def usage_summary(self, session: AsyncSession, key_id: str) -> dict[str, Any]:
        """按模型聚合该 key 的用量（来自明细表，供详情页核对）。"""
        stmt = (
            select(
                UsageLog.model,
                func.count().label("requests"),
                func.coalesce(func.sum(UsageLog.total_tokens), 0).label("tokens"),
                func.coalesce(func.sum(UsageLog.cost_units), 0).label("cost_units"),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("completion_tokens"),
                func.max(UsageLog.ts).label("last_ts"),
            )
            .where(UsageLog.key_id == key_id)
            .group_by(UsageLog.model)
            .order_by(func.count().desc())
        )
        rows = (await session.execute(stmt)).all()
        return {
            "by_model": [
                {
                    "model": row.model,
                    "requests": int(row.requests),
                    "tokens": int(row.tokens),
                    "completion_tokens": int(row.completion_tokens),
                    "cost_units": int(row.cost_units),
                    "last_ts": row.last_ts.isoformat() + "Z" if row.last_ts else None,
                }
                for row in rows
            ]
        }

    # ------------------------------------------------------------------ 写入
    async def create(self, session: AsyncSession, payload: dict[str, Any], *, defaults: dict[str, Any]) -> tuple[ApiKey, str]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise bad_request("密钥名称不能为空", param="name")

        plaintext = ""
        record: ApiKey | None = None
        for _ in range(6):
            plaintext, prefix, key_hash = generate_local_key(self.pepper)
            existing = await self.get_by_prefix(session, prefix)
            if existing is None:
                record = ApiKey(prefix=prefix, key_hash=key_hash, name=name)
                break
        if record is None:  # pragma: no cover - 概率极低
            raise conflict("生成密钥前缀时发生碰撞，请重试")

        record.name = name
        record.note = str(payload.get("note") or "")
        record.status = str(payload.get("status") or STATUS_ACTIVE)
        if record.status not in (STATUS_ACTIVE, STATUS_DISABLED):
            raise bad_request("状态只能是 active 或 disabled", param="status")

        expires_at = payload.get("expires_at")
        if expires_at:
            parsed = parse_iso(str(expires_at)) if not isinstance(expires_at, datetime) else expires_at
            if parsed is None:
                raise bad_request("过期时间格式不正确，应为 ISO8601 字符串", param="expires_at")
            record.expires_at = parsed
        elif payload.get("expires_in_days"):
            try:
                from datetime import timedelta

                days = float(payload["expires_in_days"])
            except (TypeError, ValueError):
                raise bad_request("expires_in_days 必须是数字", param="expires_in_days") from None
            if days > 0:
                from datetime import timedelta

                record.expires_at = utcnow() + timedelta(days=days)

        record.quota_limit = _as_int(payload.get("quota_limit"), 0)
        if record.quota_limit < 0:
            raise bad_request("配额不能为负数", param="quota_limit")

        models = payload.get("model_allowed")
        if models is None:
            models = payload.get("allowed_models") or []
        if isinstance(models, str):
            models = [item.strip() for item in models.replace("\n", ",").split(",") if item.strip()]
        if not isinstance(models, list):
            raise bad_request("model_allowed 应为字符串数组", param="model_allowed")
        record.model_allowed = json.dumps([str(item) for item in models], ensure_ascii=False)

        record.rpm_limit = _as_int(payload.get("rpm_limit"), defaults.get("rpm", 0))
        record.tpm_limit = _as_int(payload.get("tpm_limit"), defaults.get("tpm", 0))
        # 明文加密存库，之后可随时在控制台再查看/复制
        record.key_enc = self.store_plaintext(plaintext)
        session.add(record)
        await session.commit()
        await session.refresh(record)
        log.info("已创建密钥 %s（%s）", record.name, mask_key(record.prefix))
        return record, plaintext

    async def update(self, session: AsyncSession, key_id: str, payload: dict[str, Any]) -> ApiKey:
        record = await self.get(session, key_id)
        if "name" in payload and payload["name"] is not None:
            name = str(payload["name"]).strip()
            if not name:
                raise bad_request("密钥名称不能为空", param="name")
            record.name = name
        if "note" in payload and payload["note"] is not None:
            record.note = str(payload["note"])
        if "status" in payload and payload["status"] is not None:
            status = str(payload["status"])
            if status not in (STATUS_ACTIVE, STATUS_DISABLED):
                raise bad_request("状态只能是 active 或 disabled", param="status")
            record.status = status
        if "expires_at" in payload:
            raw = payload["expires_at"]
            if raw in (None, "", 0):
                record.expires_at = None
            else:
                parsed = parse_iso(str(raw)) if not isinstance(raw, datetime) else raw
                if parsed is None:
                    raise bad_request("过期时间格式不正确", param="expires_at")
                record.expires_at = parsed
        if "quota_limit" in payload and payload["quota_limit"] is not None:
            limit = _as_int(payload["quota_limit"], record.quota_limit)
            if limit < 0:
                raise bad_request("配额不能为负数", param="quota_limit")
            record.quota_limit = limit
        if "reset_used" in payload and payload["reset_used"]:
            record.quota_used = 0
        if "model_allowed" in payload and payload["model_allowed"] is not None:
            models = payload["model_allowed"]
            if isinstance(models, str):
                models = [item.strip() for item in models.replace("\n", ",").split(",") if item.strip()]
            if not isinstance(models, list):
                raise bad_request("model_allowed 应为字符串数组", param="model_allowed")
            record.model_allowed = json.dumps([str(item) for item in models], ensure_ascii=False)
        for field_name, column in (("rpm_limit", "rpm_limit"), ("tpm_limit", "tpm_limit")):
            if field_name in payload and payload[field_name] is not None:
                setattr(record, column, max(0, _as_int(payload[field_name], 0)))
        await session.commit()
        await session.refresh(record)
        return record

    async def delete(self, session: AsyncSession, key_id: str) -> None:
        record = await self.get(session, key_id)
        await session.delete(record)
        await session.commit()
        log.info("已删除密钥 %s", mask_key(record.prefix))

    async def rotate(self, session: AsyncSession, key_id: str) -> tuple[ApiKey, str]:
        """重新生成密钥值：换 prefix 与哈希，旧明文立即失效。

        配额用量、限速、模型授权、有效期这些「配置」保持不变——换的只是那把秘密字符串，
        所以控制台里原本配好的一切不用重配。明文同样只在这一次返回。
        """
        record = await self.get(session, key_id)
        plaintext = ""
        new_prefix = record.prefix
        new_hash = record.key_hash
        for _ in range(6):
            candidate, prefix, key_hash = generate_local_key(self.pepper)
            if prefix == record.prefix:
                continue
            if await self.get_by_prefix(session, prefix) is None:
                plaintext, new_prefix, new_hash = candidate, prefix, key_hash
                break
        if not plaintext:  # pragma: no cover - 概率极低
            raise conflict("生成新密钥前缀时发生碰撞，请重试")
        old_prefix = record.prefix
        record.prefix = new_prefix
        record.key_hash = new_hash
        record.key_enc = self.store_plaintext(plaintext)
        await session.commit()
        await session.refresh(record)
        log.warning("密钥 %s 已重新生成（%s → %s）", record.name, mask_key(old_prefix), mask_key(new_prefix))
        return record, plaintext

    async def set_status(self, session: AsyncSession, key_id: str, status: str) -> ApiKey:
        return await self.update(session, key_id, {"status": status})

    # ------------------------------------------------------------------ 序列化
    @staticmethod
    def to_public(record: ApiKey) -> dict[str, Any]:
        state = KeyService.quota_state(record)
        return {
            "key_id": record.key_id,
            "name": record.name,
            "prefix": record.prefix,
            "masked": mask_key(record.prefix),
            # 明文密文是否在库（控制台据此决定要不要显示「显示明文」）
            "has_secret": bool(record.key_enc),
            "status": record.status,
            "created_at": _iso(record.created_at),
            "updated_at": _iso(record.updated_at),
            "expires_at": _iso(record.expires_at),
            "expired": bool(record.expires_at and record.expires_at <= utcnow()),
            "quota_limit": record.quota_limit,
            "quota_used": record.quota_used,
            "quota": state.to_dict(),
            "rpm_limit": record.rpm_limit,
            "tpm_limit": record.tpm_limit,
            "model_allowed": record.allowed_models(),
            "note": record.note,
            "last_used_at": _iso(record.last_used_at),
            "total_requests": record.total_requests,
        }


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def expand_model_patterns(patterns: Sequence[str]) -> list[str]:
    return [str(p) for p in patterns]
