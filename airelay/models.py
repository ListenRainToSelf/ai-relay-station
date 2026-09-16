"""ORM 模型（对应方案 7 节的数据设计）。

命名与方案里的 ER 图保持一致：`api_keys` / `channels` / `usage_logs` /
`api_key_stats` / `model_map` / `settings`，另外补一张 `balance_snapshots`
承载余额查询的历史快照。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .timeutil import utcnow


class Base(DeclarativeBase):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------- #
class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    type_hint: Mapped[str] = mapped_column(String(16), default="str")
    group: Mapped[str] = mapped_column(String(32), default="general")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# --------------------------------------------------------------------------- #
class ApiKey(Base, TimestampMixin):
    """本地分发密钥层。

    `key_hash` 用于鉴权时的快速比对（加 pepper 的 SHA-256），
    `key_enc` 是明文的对称加密副本——有了它，这把密钥就等同于「平台式密钥管理」：
    创建之后可以随时在控制台再次查看与复制，而不是只有一次机会。
    两个字段都由本地主密钥（数据目录 secrets.json）保护，明文不落库。
    """

    __tablename__ = "api_keys"

    key_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("key"))
    name: Mapped[str] = mapped_column(String(128), default="")
    prefix: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_enc: Mapped[str] = mapped_column(Text, default="")  # 明文密文（Fernet），可为空
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active|disabled
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 额度单位 µ$（1e-6 USD），0 表示不限
    quota_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    # 冗余已用额度，和 api_key_stats 在同一事务里更新，用于 O(1) 额度校验
    quota_used: Mapped[int] = mapped_column(BigInteger, default=0)
    rpm_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0=不限
    tpm_limit: Mapped[int] = mapped_column(Integer, default=0)  # 0=不限
    model_allowed: Mapped[str] = mapped_column(Text, default="[]")  # JSON 数组，glob 模式
    note: Mapped[str] = mapped_column(Text, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_requests: Mapped[int] = mapped_column(BigInteger, default=0)

    def allowed_models(self) -> list[str]:
        import json

        try:
            data = json.loads(self.model_allowed or "[]")
        except ValueError:
            return []
        return [str(x) for x in data] if isinstance(data, list) else []


# --------------------------------------------------------------------------- #
class Channel(Base, TimestampMixin):
    """上游渠道层。api_key_enc 为 Fernet 密文。"""

    __tablename__ = "channels"

    channel_id: Mapped[str] = mapped_column(
        String(48), primary_key=True, default=lambda: new_id("ch")
    )
    name: Mapped[str] = mapped_column(String(128), default="")
    provider_type: Mapped[str] = mapped_column(String(32), index=True)  # openai|anthropic|gemini
    base_url: Mapped[str] = mapped_column(String(512), default="")
    api_key_enc: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)  # 越小越优先
    weight: Mapped[int] = mapped_column(Integer, default=1)  # 同优先级内加权随机
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 该渠道可服务的模型（glob 模式列表）；空数组表示「不限制」
    models: Mapped[str] = mapped_column(Text, default="[]")
    # 余额查询：留空用 provider 内置适配器；填了则用自定义 URL 模板
    balance_url: Mapped[str] = mapped_column(String(512), default="")
    balance_json_path: Mapped[str] = mapped_column(String(128), default="")
    balance_currency: Mapped[str] = mapped_column(String(16), default="")
    extra_headers: Mapped[str] = mapped_column(Text, default="{}")
    extra_body: Mapped[str] = mapped_column(Text, default="{}")
    timeout_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 本地进程托管（可选）：启动命令、停止方式、健康检查与自动重启策略。
    # 本机推理服务（如 start.bat 拉起的 llama.cpp）用这里托管，见 services/supervisor.py
    lifecycle: Mapped[str] = mapped_column(Text, default="{}")
    # 该渠道开放的能力（JSON 数组，如 ["chat","speech"]）；空数组 = 用协议默认。
    # 只能比协议支持的范围更窄，不能凭空多出协议没有的能力。
    capabilities: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str] = mapped_column(Text, default="")
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    def model_patterns(self) -> list[str]:
        import json

        try:
            data = json.loads(self.models or "[]")
        except ValueError:
            return []
        return [str(x) for x in data] if isinstance(data, list) else []

    def header_map(self) -> dict[str, str]:
        import json

        try:
            data = json.loads(self.extra_headers or "{}")
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def body_map(self) -> dict[str, Any]:
        import json

        try:
            data = json.loads(self.extra_body or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def capability_list(self) -> list[str]:
        import json

        try:
            data = json.loads(self.capabilities or "[]")
        except ValueError:
            return []
        return [str(item) for item in data] if isinstance(data, list) else []

    def lifecycle_config(self) -> dict[str, Any]:
        """本地进程托管配置（未配置时返回带默认值的字典）。"""
        from .services.supervisor import merge_lifecycle

        import json

        try:
            data = json.loads(self.lifecycle or "{}")
        except ValueError:
            data = {}
        return merge_lifecycle(data if isinstance(data, dict) else {})


# --------------------------------------------------------------------------- #
class ModelMap(Base, TimestampMixin):
    """模型别名表：对外 alias -> 上游真实模型 + 可选渠道绑定。"""

    __tablename__ = "model_map"
    __table_args__ = (UniqueConstraint("alias", name="uq_model_map_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(128), index=True)
    upstream_model: Mapped[str] = mapped_column(String(160))
    channel_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    provider_type: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# --------------------------------------------------------------------------- #
class UsageLog(Base):
    """用量明细：每次请求一行，含时间戳与速度指标（权威口径）。"""

    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_logs_ts_key", "ts", "key_id"),
        Index("ix_usage_logs_ts_model", "ts", "model"),
    )

    log_id: Mapped[str] = mapped_column(String(48), primary_key=True, default=lambda: new_id("log"))
    request_id: Mapped[str] = mapped_column(String(48), index=True, default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    key_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    key_name: Mapped[str] = mapped_column(String(128), default="")
    key_prefix: Mapped[str] = mapped_column(String(32), default="")
    channel_id: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    channel_name: Mapped[str] = mapped_column(String(128), default="")
    provider_type: Mapped[str] = mapped_column(String(32), default="")
    model: Mapped[str] = mapped_column(String(160), default="", index=True)  # 请求的（别名）模型
    upstream_model: Mapped[str] = mapped_column(String(160), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    first_token_ms: Mapped[float] = mapped_column(Float, default=0.0)
    speed_tok_s: Mapped[float] = mapped_column(Float, default=0.0)
    stream: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="ok", index=True)  # ok|error
    error_code: Mapped[str] = mapped_column(String(48), default="")
    cost_units: Mapped[int] = mapped_column(BigInteger, default=0)
    # 非对话能力的计量：图片张数 / 字符数 / 音频秒数（配合 unit_kind）
    units: Mapped[int] = mapped_column(Integer, default=0)
    unit_kind: Mapped[str] = mapped_column(String(16), default="")
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")


class ApiKeyStat(Base):
    """按 key × 模型 × 分钟桶预聚合，供控制台秒级出图。"""

    __tablename__ = "api_key_stats"
    __table_args__ = (Index("ix_stats_bucket", "bucket_start"),)

    key_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    model: Mapped[str] = mapped_column(String(160), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    completion_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    errors: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_units: Mapped[int] = mapped_column(BigInteger, default=0)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(48), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    currency: Mapped[str] = mapped_column(String(16), default="")
    total: Mapped[float] = mapped_column(Float, default=0.0)
    granted: Mapped[float] = mapped_column(Float, default=0.0)
    topped_up: Mapped[float] = mapped_column(Float, default=0.0)
    supported: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[str] = mapped_column(Text, default="")
