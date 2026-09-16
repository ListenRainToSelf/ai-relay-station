"""配置中心。

所有可调项集中注册在 `SETTING_SPECS`，持久化到 SQLite 的 `settings` 表，
并在进程内维护一份缓存供热路径读取。方案 11 节要求的「服务 IP / 服务端口」
只是其中两项——这里把网络、网关、限流、监控、余额、日志、计价、界面
全部纳入同一套带类型校验的注册表，避免散落的魔法数字。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import bad_request
from .models import Setting

log = logging.getLogger(__name__)

TYPE_STR = "str"
TYPE_INT = "int"
TYPE_FLOAT = "float"
TYPE_BOOL = "bool"
TYPE_JSON = "json"

GROUP_ORDER = ["network", "gateway", "services", "ratelimit", "monitoring", "balance", "routing", "pricing", "logs", "security", "ui"]

GROUP_LABELS = {
    "network": "网络",
    "gateway": "网关",
    "services": "本地服务托管",
    "ratelimit": "限流",
    "monitoring": "会话监控",
    "balance": "余额",
    "routing": "路由",
    "pricing": "计价",
    "logs": "日志",
    "security": "安全",
    "ui": "外观与界面",
}


@dataclass(frozen=True)
class SettingSpec:
    key: str
    group: str
    label: str
    type: str
    default: Any
    description: str = ""
    requires_restart: bool = False
    choices: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    secret: bool = False
    advanced: bool = False


def _specs() -> dict[str, SettingSpec]:
    s: list[SettingSpec] = [
        # ---------------- 网络 ----------------
        SettingSpec(
            "network.host", "network", "服务 IP", TYPE_STR, "127.0.0.1",
            "监听地址。仅本机使用保持 127.0.0.1；NAS 上供局域网调用改为 0.0.0.0。",
            requires_restart=True,
        ),
        SettingSpec(
            "network.port", "network", "服务端口", TYPE_INT, 8000,
            "网关对外端口，客户端 base_url 用到它。", requires_restart=True,
            minimum=1, maximum=65535,
        ),
        SettingSpec(
            "network.public_base_url", "network", "对外基地址", TYPE_STR, "",
            "仅用于控制台展示与复制，留空则按监听地址自动推断。",
        ),
        SettingSpec(
            "network.trust_proxy", "network", "信任反向代理头", TYPE_BOOL, False,
            "置于 Nginx / 群晖反代之后时开启，用 X-Forwarded-For 识别来源 IP。",
        ),
        SettingSpec(
            "network.cors_allow_origins", "network", "跨域白名单", TYPE_STR, "",
            "逗号分隔的来源列表；留空表示不允许跨域（同源访问控制台不受影响）。",
        ),
        # ---------------- 网关 ----------------
        SettingSpec(
            "gateway.request_timeout", "gateway", "请求总超时(秒)", TYPE_FLOAT, 600.0,
            "单次请求从接收到结束的时间上限。", minimum=5.0, maximum=7200.0,
        ),
        SettingSpec(
            "gateway.connect_timeout", "gateway", "连接上游超时(秒)", TYPE_FLOAT, 15.0,
            "TCP/TLS 建连超时。", minimum=1.0, maximum=300.0,
        ),
        SettingSpec(
            "gateway.first_byte_timeout", "gateway", "首字节超时(秒)", TYPE_FLOAT, 60.0,
            "上游开始返回内容前允许的等待时间，超过即换渠道。", minimum=1.0, maximum=1200.0,
        ),
        SettingSpec(
            "gateway.idle_timeout", "gateway", "流式静默超时(秒)", TYPE_FLOAT, 120.0,
            "两次 chunk 之间的最大间隔，超过判定为僵死连接。", minimum=5.0, maximum=3600.0,
        ),
        SettingSpec(
            "gateway.max_retries", "gateway", "换渠道重试次数", TYPE_INT, 1,
            "可重试类错误（5xx/超时/限流）时切换渠道重试的次数。",
            minimum=0, maximum=5,
        ),
        SettingSpec(
            "gateway.channel_cooldown_seconds", "gateway", "渠道熔断冷却(秒)", TYPE_INT, 60,
            "渠道出错后暂时摘除的时长。", minimum=0, maximum=3600,
        ),
        SettingSpec(
            "gateway.default_max_tokens", "gateway", "默认 max_tokens", TYPE_INT, 4096,
            "Anthropic / Gemini 等要求显式输出上限的协议用它兜底。",
            minimum=1, maximum=200000,
        ),
        SettingSpec(
            "gateway.stream_include_usage", "gateway", "流式回传 usage", TYPE_BOOL, True,
            "在最后一个 SSE chunk 里补上 usage，便于客户端计费。",
        ),
        SettingSpec(
            "gateway.upstream_error_detail", "gateway", "透传上游错误详情", TYPE_BOOL, True,
            "关闭后只返回统一错误码，不外泄上游报文。",
        ),
        # ---------------- 限流 ----------------
        SettingSpec(
            "ratelimit.global_rpm", "ratelimit", "全局 RPM 上限", TYPE_INT, 0,
            "0 表示不限。", minimum=0,
        ),
        SettingSpec(
            "ratelimit.global_tpm", "ratelimit", "全局 TPM 上限", TYPE_INT, 0,
            "每分钟 token 总量上限，0 表示不限。按请求开始时的估算值预检、结束后按真实用量记账。",
            minimum=0,
        ),
        SettingSpec(
            "ratelimit.default_key_rpm", "ratelimit", "新建 Key 默认 RPM", TYPE_INT, 0,
            "创建密钥时的默认每分钟请求上限，0 表示不限。", minimum=0,
        ),
        SettingSpec(
            "ratelimit.default_key_tpm", "ratelimit", "新建 Key 默认 TPM", TYPE_INT, 0,
            "创建密钥时的默认每分钟 token 上限，0 表示不限。", minimum=0,
        ),
        # ---------------- 本地服务托管 ----------------
        SettingSpec(
            "services.supervisor_enabled", "services", "启用本地服务托管", TYPE_BOOL, True,
            "托管本机推理服务（如 start.bat 拉起的 llama.cpp）：探活、掉线自动重启、按需关闭。",
        ),
        SettingSpec(
            "services.check_interval_seconds", "services", "默认探活间隔(秒)", TYPE_INT, 15,
            "渠道未单独指定时使用；探活就是给该渠道的 base_url 发一次轻量 GET。", minimum=5, maximum=3600,
        ),
        SettingSpec(
            "services.autostart_on_boot", "services", "随网关自动启动", TYPE_BOOL, True,
            "网关启动后，把标记了「自动启动」的本地服务拉起来（已在运行则跳过）。",
        ),
        SettingSpec(
            "services.stop_on_shutdown", "services", "网关退出时一并关闭", TYPE_BOOL, False,
            "开启后，网关停止时关闭所有托管的本地服务；关闭则只关那些渠道里单独勾选过的。",
        ),
        # ---------------- 会话监控 ----------------
        SettingSpec(
            "monitoring.stale_seconds", "monitoring", "僵死判定(秒)", TYPE_INT, 300,
            "超过该时长没有任何 chunk 的活跃会话标注为「超时中断」。", minimum=10,
        ),
        SettingSpec(
            "monitoring.push_enabled", "monitoring", "实时推送", TYPE_BOOL, True,
            "关闭后控制台改为按需拉取快照。",
        ),
        SettingSpec(
            "monitoring.push_interval_ms", "monitoring", "推送间隔(毫秒)", TYPE_INT, 1000,
            "活跃会话增量推送的最小间隔，避免高频刷屏。", minimum=200, maximum=10000,
        ),
        SettingSpec(
            "monitoring.recent_limit", "monitoring", "最近请求条数", TYPE_INT, 50,
            "控制台「最近请求」列表长度。", minimum=5, maximum=500,
        ),
        # ---------------- 余额 ----------------
        SettingSpec(
            "balance.auto_refresh", "balance", "自动刷新", TYPE_BOOL, True, "定时拉取各渠道余额。",
        ),
        SettingSpec(
            "balance.refresh_minutes", "balance", "刷新间隔(分钟)", TYPE_INT, 15,
            "自动刷新周期。", minimum=1, maximum=1440,
        ),
        SettingSpec(
            "balance.warn_threshold", "balance", "低余额告警阈值", TYPE_FLOAT, 0.0,
            "0 表示不告警；低于该额度（按余额币种）时控制台高亮。", minimum=0.0,
        ),
        # ---------------- 路由 ----------------
        SettingSpec(
            "routing.sticky_minutes", "routing", "渠道粘滞(分钟)", TYPE_INT, 0,
            "0 表示每次按权重随机；大于 0 时同一本地 Key 在该时间窗内固定用同一渠道。",
            minimum=0, maximum=1440,
        ),
        SettingSpec(
            "routing.wildcard_alias", "routing", "兜底模型别名", TYPE_STR, "",
            "模型未命中别名表时，作为兜底的上游真实模型 id；留空则原样透传。",
        ),
        SettingSpec(
            "routing.max_attempts_channels", "routing", "单次候选渠道上限", TYPE_INT, 4,
            "一次请求最多尝试的渠道个数。", minimum=1, maximum=20,
        ),
        # ---------------- 计价 ----------------
        SettingSpec(
            "pricing.currency", "pricing", "计价币种", TYPE_STR, "USD",
            "pricing 表里的单价币种。", choices=("USD", "CNY"),
        ),
        SettingSpec(
            "pricing.models", "pricing", "模型单价表", TYPE_JSON, {},
            "形如 {\"模型\": {\"prompt\": 每百万输入单价, \"completion\": 每百万输出单价, "
            "\"image\": 每张图片单价, \"character\": 每百万字符单价, \"second\": 每秒音频单价}}；"
            "对话按 token 计，语音合成按字符、语音识别按音频秒数、图片按张数计；未列出的模型按 0 计。",
        ),
        SettingSpec(
            "pricing.default", "pricing", "默认单价", TYPE_JSON, {"prompt": 0.0, "completion": 0.0},
            "未匹配到模型时的兜底单价（每百万 token）。",
        ),
        # ---------------- 日志 ----------------
        SettingSpec(
            "logs.level", "logs", "日志级别", TYPE_STR, "INFO",
            "控制台与文件日志级别。", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        ),
        SettingSpec(
            "logs.retention_days", "logs", "明细保留天数", TYPE_INT, 30,
            "用量明细超期自动清理。", minimum=1, maximum=3650,
        ),
        SettingSpec(
            "logs.access_log", "logs", "记录访问日志", TYPE_BOOL, True,
            "每个请求结束打印一行摘要。",
        ),
        # ---------------- 安全 ----------------
        SettingSpec(
            "security.session_ttl_hours", "security", "会话有效期(小时)", TYPE_INT, 12,
            "控制台登录态时长。", minimum=1, maximum=720,
        ),
        SettingSpec(
            "security.require_admin_token_remote", "security", "远程访问需令牌", TYPE_BOOL, True,
            "非环回地址访问管理面时必须提供管理员令牌。",
        ),
        SettingSpec(
            "security.keep_key_plaintext", "security", "本地密钥可再次查看", TYPE_BOOL, True,
            "开启后本地密钥的明文会加密存库，可在控制台随时再查看/复制（平台式密钥管理）。"
            "关闭后新密钥只存哈希、明文不可取回；已存的密文不受影响。",
        ),
        SettingSpec(
            "security.local_key_prefix", "security", "密钥前缀", TYPE_STR, "sk-relay",
            "生成的本地密钥前缀，便于与上游密钥区分。", requires_restart=False,
        ),
        # ---------------- 界面 ----------------
        SettingSpec(
            "ui.theme", "ui", "主题", TYPE_STR, "dark", "控制台配色。", choices=("dark", "light"),
        ),
        SettingSpec(
            "ui.page_size", "ui", "列表分页条数", TYPE_INT, 20, "", minimum=5, maximum=200,
        ),
        SettingSpec(
            "ui.autostart", "ui", "开机自启", TYPE_BOOL, False,
            "Windows 写入注册表 Run 项；Linux 由部署脚本决定 systemd 是否 enable。",
        ),
    ]
    return {spec.key: spec for spec in s}


SETTING_SPECS: dict[str, SettingSpec] = _specs()

# 需要在热路径上频繁读取、单独缓存以减少加锁的键
HOT_KEYS = frozenset(
    {
        "gateway.max_retries",
        "gateway.channel_cooldown_seconds",
        "gateway.default_max_tokens",
        "gateway.stream_include_usage",
        "gateway.upstream_error_detail",
        "gateway.first_byte_timeout",
        "gateway.idle_timeout",
        "gateway.connect_timeout",
        "gateway.request_timeout",
        "monitoring.stale_seconds",
        "routing.wildcard_alias",
        "routing.sticky_minutes",
        "routing.max_attempts_channels",
        "ratelimit.global_rpm",
        "security.local_key_prefix",
    }
)


def coerce_value(spec: SettingSpec, value: Any) -> Any:
    """把任意输入强制转换为声明的类型，类型不符时抛 BAD_REQUEST。"""
    if value is None:
        return spec.default
    try:
        if spec.type == TYPE_BOOL:
            if isinstance(value, bool):
                out: Any = value
            elif isinstance(value, (int, float)):
                out = bool(value)
            elif isinstance(value, str):
                low = value.strip().lower()
                if low in {"1", "true", "yes", "on", "是", "开"}:
                    out = True
                elif low in {"0", "false", "no", "off", "否", "关"}:
                    out = False
                else:
                    raise ValueError(f"{value!r} 不是布尔值")
            else:
                raise ValueError(f"{value!r} 不是布尔值")
        elif spec.type == TYPE_INT:
            if isinstance(value, bool):
                raise ValueError("布尔值不能作为整数")
            out = int(str(value).strip()) if isinstance(value, str) else int(value)
            if isinstance(value, float) and value != int(value):
                out = int(round(value))
        elif spec.type == TYPE_FLOAT:
            out = float(str(value).strip()) if isinstance(value, str) else float(value)
        elif spec.type == TYPE_JSON:
            if isinstance(value, str):
                text = value.strip()
                out = json.loads(text) if text else spec.default
            elif isinstance(value, (dict, list)):
                out = value
            else:
                raise ValueError(f"{value!r} 不是合法的 JSON 对象")
        else:
            out = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            out = out.strip()
    except (TypeError, ValueError) as exc:
        raise bad_request(f"设置项 {spec.key} 取值非法：{exc}") from None

    if spec.choices is not None and out not in spec.choices:
        raise bad_request(f"设置项 {spec.key} 只接受 {', '.join(spec.choices)}")
    if spec.minimum is not None and isinstance(out, (int, float)) and out < spec.minimum:
        raise bad_request(f"设置项 {spec.key} 不能小于 {spec.minimum}")
    if spec.maximum is not None and isinstance(out, (int, float)) and out > spec.maximum:
        raise bad_request(f"设置项 {spec.key} 不能大于 {spec.maximum}")
    return out


def serialize_value(spec: SettingSpec, value: Any) -> str:
    if spec.type == TYPE_JSON:
        return json.dumps(value, ensure_ascii=False)
    if spec.type == TYPE_BOOL:
        return "true" if value else "false"
    return str(value)


def deserialize_value(spec: SettingSpec, raw: str | None) -> Any:
    if raw is None:
        return spec.default
    try:
        if spec.type == TYPE_JSON:
            return json.loads(raw)
        if spec.type == TYPE_BOOL:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if spec.type == TYPE_INT:
            return int(raw)
        if spec.type == TYPE_FLOAT:
            return float(raw)
        return raw
    except (TypeError, ValueError):
        log.warning("设置项 %s 的库中取值 %r 无法解析，回退默认值", spec.key, raw)
        return spec.default


@dataclass
class SettingChange:
    key: str
    old: Any
    new: Any

    @property
    def requires_restart(self) -> bool:
        spec = SETTING_SPECS.get(self.key)
        return bool(spec and spec.requires_restart)


class SettingsService:
    """带类型校验的设置读写 + 进程内缓存。"""

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {k: spec.default for k, spec in SETTING_SPECS.items()}
        self._listeners: list[Any] = []
        self._overrides: dict[str, Any] = {}
        self._loaded = False

    # ------------------------------------------------------------------ 覆盖
    def set_overrides(self, values: Mapping[str, Any]) -> None:
        """命令行传入的临时覆盖项：优先级高于库中配置，但不写库。"""
        for key, raw in values.items():
            spec = SETTING_SPECS.get(key)
            if spec is None:
                raise bad_request(f"未知设置项：{key}")
            self._overrides[key] = coerce_value(spec, raw)
            self._cache[key] = self._overrides[key]

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._overrides)

    # ------------------------------------------------------------------ 读取
    def get(self, key: str, default: Any = None) -> Any:
        if key in self._cache:
            return self._cache[key]
        if default is not None:
            return default
        spec = SETTING_SPECS.get(key)
        return spec.default if spec else None

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get(key, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def get_str(self, key: str, default: str = "") -> str:
        value = self.get(key, default)
        return default if value is None else str(value)

    def get_json(self, key: str, default: Any = None) -> Any:
        value = self.get(key, default)
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    def snapshot(self) -> dict[str, Any]:
        return dict(self._cache)

    # ------------------------------------------------------------------ 持久化
    async def load(self, session: AsyncSession) -> None:
        rows = (await session.execute(select(Setting))).scalars().all()
        stored = {row.key: row.value for row in rows}
        for key, spec in SETTING_SPECS.items():
            self._cache[key] = deserialize_value(spec, stored.get(key))
        # 保留未知键（老版本遗留），避免升级时静默丢配置
        for key, value in stored.items():
            if key not in SETTING_SPECS:
                self._cache[key] = deserialize_value(
                    SettingSpec(key, "legacy", key, TYPE_STR, ""), value
                )
        # 命令行覆盖项在库配置之后生效（例如 --port 8090）
        self._cache.update(self._overrides)
        self._loaded = True

    async def update(self, session: AsyncSession, values: Mapping[str, Any]) -> list[SettingChange]:
        changes: list[SettingChange] = []
        for key, raw in values.items():
            spec = SETTING_SPECS.get(key)
            if spec is None:
                raise bad_request(f"未知设置项：{key}")
            new_value = coerce_value(spec, raw)
            old_value = self._cache.get(key)
            had_override = key in self._overrides
            row = await session.get(Setting, key)
            encoded = serialize_value(spec, new_value)
            # PUT 的语义是「把这一项持久化为该值」：即便与当前生效值相同也要落库，
            # 否则被命令行 --set 覆盖过的项永远无法在界面上「锁定」下来。
            if row is None:
                session.add(Setting(key=key, value=encoded, type_hint=spec.type, group=spec.group))
            else:
                row.value = encoded
                row.type_hint = spec.type
                row.group = spec.group
            self._cache[key] = new_value
            # 用户在控制台显式改了某项，命令行覆盖就此让位（以库中配置为准）
            self._overrides.pop(key, None)
            if old_value != new_value or had_override:
                changes.append(SettingChange(key=key, old=old_value, new=new_value))
        await session.commit()
        await self._notify(changes)
        return changes

    async def reset(self, session: AsyncSession, keys: Iterable[str] | None = None) -> list[SettingChange]:
        targets = list(keys) if keys else list(SETTING_SPECS.keys())
        defaults = {key: SETTING_SPECS[key].default for key in targets if key in SETTING_SPECS}
        return await self.update(session, defaults)

    # ------------------------------------------------------------------ 变更通知
    def add_listener(self, callback: Any) -> None:
        """注册变更回调（同步或协程均可），用于热重绑定端口、调整日志级别等。"""
        self._listeners.append(callback)

    async def _notify(self, changes: list[SettingChange]) -> None:
        if not changes:
            return
        for callback in list(self._listeners):
            try:
                result = callback(changes)
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # 监听器故障不应影响设置保存
                log.exception("设置变更监听器执行失败")

    # ------------------------------------------------------------------ 元信息
    @staticmethod
    def describe() -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for spec in SETTING_SPECS.values():
            groups.setdefault(spec.group, []).append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "type": spec.type,
                    "description": spec.description,
                    "requires_restart": spec.requires_restart,
                    "choices": list(spec.choices) if spec.choices else None,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "secret": spec.secret,
                    "advanced": spec.advanced,
                    "default": spec.default,
                }
            )
        ordered = [g for g in GROUP_ORDER if g in groups]
        ordered += [g for g in groups if g not in GROUP_ORDER]
        return [
            {"group": g, "label": GROUP_LABELS.get(g, g), "items": groups[g]} for g in ordered
        ]


@dataclass
class PricingTable:
    """把「每百万 token 单价」换算成整数的微美元额度单位。

    额度统一使用 µ$（1e-6 美元）作为内部单位，避免浮点误差累积；
    未配置价格的模型按 0 计，因此不配价也能正常记账。
    """

    currency: str = "USD"
    models: dict[str, dict[str, float]] = field(default_factory=dict)
    default: dict[str, float] = field(
        default_factory=lambda: {"prompt": 0.0, "completion": 0.0, "image": 0.0, "character": 0.0, "second": 0.0}
    )

    @classmethod
    def from_settings(cls, settings: SettingsService) -> "PricingTable":
        models = settings.get_json("pricing.models", {}) or {}
        default = settings.get_json("pricing.default", {}) or {}
        cleaned: dict[str, dict[str, float]] = {}
        if isinstance(models, dict):
            for name, price in models.items():
                if not isinstance(price, dict):
                    continue
                cleaned[str(name)] = {
                    "prompt": _safe_float(price.get("prompt")),
                    "completion": _safe_float(price.get("completion")),
                    # 非对话能力的单价（可选）
                    "image": _safe_float(price.get("image")),
                    "character": _safe_float(price.get("character")),
                    "second": _safe_float(price.get("second")),
                }
        return cls(
            currency=settings.get_str("pricing.currency", "USD"),
            models=cleaned,
            default={
                "prompt": _safe_float(default.get("prompt")),
                "completion": _safe_float(default.get("completion")),
            },
        )

    def unit_prices(self, model: str | None) -> dict[str, float]:
        if model and model in self.models:
            return self.models[model]
        return self.default

    def cost_units(
        self,
        model: str | None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        *,
        unit_kind: str = "",
        units: int = 0,
    ) -> int:
        """按用量算成本（µ$）。

        对话按 token；语音合成按字符、语音识别按音频秒数、图片按张数——
        各家的单价键分别是 prompt/completion、character、second、image。
        """
        price = self.unit_prices(model)
        if unit_kind == "image":
            usd = units * price.get("image", 0.0)
        elif unit_kind == "character":
            usd = (units / 1_000_000.0) * price.get("character", 0.0)
        elif unit_kind == "second":
            usd = units * price.get("second", 0.0)
        else:
            usd = (prompt_tokens / 1_000_000.0) * price.get("prompt", 0.0) + (
                completion_tokens / 1_000_000.0
            ) * price.get("completion", 0.0)
        return int(round(usd * 1_000_000))

    @staticmethod
    def units_to_currency(units: int) -> float:
        return round(units / 1_000_000.0, 6)


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
