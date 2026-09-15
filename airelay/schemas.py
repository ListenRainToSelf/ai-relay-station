"""管理面请求/响应模型（pydantic）。

协议面刻意不做严格建模：为了忠实透传各家上游参数，那边直接用原始 dict。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Lenient(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
class LoginPayload(Strict):
    token: str = Field(..., description="管理员令牌，见数据目录 secrets.json 或启动日志")


class KeyCreate(Strict):
    name: str = Field(..., min_length=1, max_length=128)
    note: str = ""
    status: str = "active"
    quota_limit: int = Field(0, ge=0, description="额度，单位 µ$（1e-6 美元），0 表示不限")
    rpm_limit: int = Field(0, ge=0)
    tpm_limit: int = Field(0, ge=0)
    model_allowed: list[str] = Field(default_factory=list, description="允许的模型（支持 glob），空表示全部")
    expires_at: str | None = Field(None, description="ISO8601 过期时间")
    expires_in_days: float | None = Field(None, description="按天数设置过期，优先于 expires_at")


class KeyUpdate(Lenient):
    name: str | None = None
    note: str | None = None
    status: str | None = None
    quota_limit: int | None = Field(None, ge=0)
    rpm_limit: int | None = Field(None, ge=0)
    tpm_limit: int | None = Field(None, ge=0)
    model_allowed: list[str] | None = None
    expires_at: str | None = None
    reset_used: bool | None = None


class LifecycleConfig(Lenient):
    """本地进程托管配置（可选）。命令会以当前用户身份在本机执行，只填自己信任的脚本。"""

    enabled: bool = False
    command: str = ""
    args: list[str] | str = Field(default_factory=list)
    workdir: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    stop_command: str = ""
    stop_strategy: str = "auto"
    health_path: str = "/v1/models"
    startup_grace_seconds: int = 40
    auto_start: bool = True
    auto_restart: bool = True
    stop_on_shutdown: bool = False
    check_interval_seconds: int = 15
    failure_threshold: int = 3
    max_restarts: int = 0
    restart_backoff_seconds: int = 30


class ChannelCreate(Lenient):
    name: str = ""
    provider_type: str = "openai"
    base_url: str = ""
    api_key: str = Field(..., min_length=1)
    priority: int = 0
    weight: int = Field(1, ge=0)
    status: str = "active"
    models: list[str] = Field(default_factory=list)
    balance_url: str = ""
    balance_json_path: str = ""
    balance_currency: str = ""
    extra_headers: dict[str, Any] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = None
    lifecycle: LifecycleConfig | None = None
    note: str = ""


class ChannelUpdate(Lenient):
    name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    priority: int | None = None
    weight: int | None = Field(None, ge=0)
    status: str | None = None
    models: list[str] | None = None
    balance_url: str | None = None
    balance_json_path: str | None = None
    balance_currency: str | None = None
    extra_headers: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    lifecycle: LifecycleConfig | None = None
    note: str | None = None


class ProbePayload(Lenient):
    model: str = ""
    max_tokens: int = Field(128, ge=8, le=4096, description="探针的最大输出 token（推理模型建议 ≥128）")


class ModelMapCreate(Lenient):
    alias: str = Field(..., min_length=1)
    upstream_model: str = Field(..., min_length=1)
    channel_id: str | None = None
    provider_type: str = ""
    note: str = ""
    enabled: bool = True


class ModelMapUpdate(Lenient):
    alias: str | None = None
    upstream_model: str | None = None
    channel_id: str | None = None
    provider_type: str | None = None
    note: str | None = None
    enabled: bool | None = None


class ModelMapImport(Strict):
    entries: list[ModelMapCreate]
    replace: bool = False


class SettingsUpdate(Strict):
    values: dict[str, Any]


class SettingsReset(Strict):
    keys: list[str] | None = None


class BalanceRefresh(Strict):
    channel_id: str | None = None


__all__ = [
    "BalanceRefresh",
    "ChannelCreate",
    "ChannelUpdate",
    "KeyCreate",
    "KeyUpdate",
    "LifecycleConfig",
    "LoginPayload",
    "ModelMapCreate",
    "ModelMapImport",
    "ModelMapUpdate",
    "ProbePayload",
    "SettingsReset",
    "SettingsUpdate",
]
