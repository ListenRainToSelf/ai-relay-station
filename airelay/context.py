"""应用上下文：把配置、数据库、服务与后台任务装配在一起。

`AppContext.startup()` 负责建表、加载设置、预热别名缓存、拉起后台任务；
`shutdown()` 负责优雅收尾（等在途用量落库、关闭连接池）。
网关内核与桌面外壳都只依赖这一个对象（方案 3 节的分层解耦）。

注意：startup / shutdown 成对且可重入——服务 IP 或端口变更时，宿主会
停掉旧的 uvicorn 再起一个新的，新的事件循环需要一套新的引擎与连接池。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from .db import build_engine, build_session_factory, dispose, init_db, sqlite_version
from .logging_setup import setup_logging
from .paths import AppPaths
from .security import SecretBox, Secrets, SessionSigner, load_or_create_secrets
from .services import (
    BalanceService,
    ChannelService,
    KeyService,
    LiveRegistry,
    MaintenanceService,
    ModelMapService,
    LocalServiceSupervisor,
    RateLimiter,
    Router,
    UsageService,
)
from .settings import PricingTable, SettingChange, SettingsService
from .timeutil import to_iso, utcnow
from .version import APP_NAME, __version__

log = logging.getLogger(__name__)

MODE_DESKTOP = "desktop"
MODE_SERVER = "server"


class AppContext:
    def __init__(
        self,
        *,
        paths: AppPaths,
        settings: SettingsService | None = None,
        secrets: Secrets | None = None,
        mode: str = MODE_SERVER,
    ) -> None:
        self.paths = paths
        self.mode = mode
        self.settings = settings or SettingsService()
        self.secrets = secrets or load_or_create_secrets(paths.data_dir)
        self.started_at = utcnow()
        self.stop_event = asyncio.Event()

        self.cipher = SecretBox.from_master_key(self.secrets.master_key.encode("ascii"))
        self.signer = SessionSigner(
            self.secrets.session_secret.encode("utf-8"),
            ttl_seconds=self.settings.get_int("security.session_ttl_hours", 12) * 3600,
        )

        # 与数据库无关的服务可以在构造期就绪
        self.live = LiveRegistry()
        self.ratelimiter = RateLimiter()
        self.router = Router(settings=self.settings)
        self.keys = KeyService(
            pepper=self.secrets.pepper, cipher=self.cipher, settings=self.settings
        )
        self.channels = ChannelService(cipher=self.cipher, settings=self.settings)
        self.mapping = ModelMapService(
            wildcard_fallback_getter=lambda: self.settings.get_str("routing.wildcard_alias", "")
        )

        # 依赖事件循环 / 连接池的资源在 startup 中创建
        self.engine: Any = None
        self.session_factory: Any = None
        self.http: httpx.AsyncClient | None = None
        self.usage: UsageService | None = None
        self.balance: BalanceService | None = None
        self.maintenance: MaintenanceService | None = None
        self.services: LocalServiceSupervisor | None = None

        self._pricing: PricingTable | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._ready = False
        self.pending_restart = False
        self.restart_callback: Callable[[], Awaitable[None]] | None = None
        self.sqlite_version = ""

        self.settings.add_listener(self._on_settings_changed)

    # ------------------------------------------------------------------ 属性
    @property
    def pricing(self) -> PricingTable:
        if self._pricing is None:
            self._pricing = PricingTable.from_settings(self.settings)
        return self._pricing

    @property
    def admin_token(self) -> str:
        return self.secrets.admin_token

    @property
    def host(self) -> str:
        return self.settings.get_str("network.host", "127.0.0.1")

    @property
    def port(self) -> int:
        return self.settings.get_int("network.port", 8000)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def db(self) -> Any:
        """便捷访问：请求用的一次性会话。"""
        if self.session_factory is None:
            raise RuntimeError("应用上下文尚未启动")
        return self.session_factory()

    def base_url(self, host: str | None = None) -> str:
        override = self.settings.get_str("network.public_base_url", "").strip()
        if override:
            return override.rstrip("/")
        target = host or self.host
        display = "127.0.0.1" if target in ("0.0.0.0", "::", "") else target
        return f"http://{display}:{self.port}"

    def openai_base_url(self, host: str | None = None) -> str:
        return f"{self.base_url(host)}/v1"

    def uptime_seconds(self) -> float:
        return (utcnow() - self.started_at).total_seconds()

    def describe(self) -> dict[str, Any]:
        return {
            "app": APP_NAME,
            "version": __version__,
            "mode": self.mode,
            "started_at": to_iso(self.started_at),
            "uptime_seconds": round(self.uptime_seconds(), 1),
            "data_dir": str(self.paths.data_dir),
            "db_path": str(self.paths.db_path),
            "host": self.host,
            "port": self.port,
            "base_url": self.base_url(),
            "openai_base_url": self.openai_base_url(),
            "pending_restart": self.pending_restart,
            "sqlite_version": self.sqlite_version,
            "local_services": [
                {"channel_id": item["channel_id"], "status": item["status"], "managed": item["managed"]}
                for item in (self.services.snapshot() if self.services else [])
            ],
        }

    # ------------------------------------------------------------------ 生命周期
    async def startup(self) -> None:
        if self._ready:
            return
        setup_logging(self.settings.get_str("logs.level", "INFO"), self.paths.log_dir)

        self.engine = build_engine(self.paths.db_path)
        self.session_factory = build_session_factory(self.engine)
        await init_db(self.engine)
        self.sqlite_version = await sqlite_version(self.engine)

        async with self.session_factory() as session:
            await self.settings.load(session)
            setup_logging(self.settings.get_str("logs.level", "INFO"), self.paths.log_dir)
            self.signer.ttl_seconds = self.settings.get_int("security.session_ttl_hours", 12) * 3600
            await self.mapping.refresh(session)

        self.usage = UsageService(session_factory=self.session_factory)
        self.balance = BalanceService(
            channels=self.channels, settings=self.settings, session_factory=self.session_factory
        )
        self.maintenance = MaintenanceService(engine=self.engine, settings=self.settings)
        self.services = LocalServiceSupervisor(
            settings=self.settings,
            session_factory=self.session_factory,
            log_dir=self.paths.log_dir,
            http_getter=lambda: self.http,
        )

        self.http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
            timeout=httpx.Timeout(connect=15.0, read=None, write=30.0, pool=15.0),
            follow_redirects=False,
            headers={"User-Agent": f"airelay/{__version__}"},
        )

        self.stop_event = asyncio.Event()
        self._tasks = [
            asyncio.create_task(
                self.live.broadcast_loop(
                    interval_ms=self.settings.get_int("monitoring.push_interval_ms", 1000),
                    stale_seconds=self.settings.get_int("monitoring.stale_seconds", 300),
                    recent_limit=self.settings.get_int("monitoring.recent_limit", 50),
                    # 兜底回收阈值：静默超过 3 倍流式静默超时、且总时长翻倍于请求超时
                    sweep_idle_seconds=max(
                        self.settings.get_float("gateway.idle_timeout", 120.0) * 3, 180.0
                    ),
                    sweep_max_age_seconds=self.settings.get_float("gateway.request_timeout", 600.0) * 2,
                ),
                name="live-broadcast",
            ),
            asyncio.create_task(
                self.balance.refresh_loop(self.http, self.stop_event), name="balance-refresh"
            ),
            asyncio.create_task(self.maintenance.loop(self.stop_event), name="maintenance"),
            asyncio.create_task(
                self.services.supervise_loop(self.stop_event), name="local-services"
            ),
        ]
        # 本地服务可能要多花几十秒加载模型，放到后台跑，别拖慢网关启动
        asyncio.create_task(self._autostart_local_services(), name="local-services-boot")
        self._ready = True
        log.info(
            "%s v%s 已启动 · 模式=%s · 监听 %s:%s · 数据目录 %s",
            APP_NAME,
            __version__,
            self.mode,
            self.host,
            self.port,
            self.paths.data_dir,
        )

    async def shutdown(self) -> None:
        if not self._ready and self.engine is None:
            return
        self._ready = False
        # 先按配置关闭托管的本地进程（在关连接池之前做，日志才写得进去）
        if self.services is not None:
            try:
                await self.services.stop_all()
            except Exception:
                log.exception("关闭本地服务时出错")
        self.stop_event.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # 给可能还在途中的探活/落库线程一点收尾时间，避免它们撞上「事件循环已关闭」
        await asyncio.sleep(0.05)
        if self.usage is not None:
            await self.usage.drain()
            self.usage = None
        if self.http is not None:
            await self.http.aclose()
            self.http = None
        if self.engine is not None:
            await dispose(self.engine)
            self.engine = None
            self.session_factory = None
        log.info("已停止，数据已安全落盘")

    async def _autostart_local_services(self) -> None:
        if self.services is None:
            return
        # 稍等一下，让 HTTP 服务先对外可用
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=3)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await self.services.autostart()
        except Exception:
            log.exception("自动启动本地服务失败")

    # ------------------------------------------------------------------ 设置联动
    async def _on_settings_changed(self, changes: list[SettingChange]) -> None:
        touched = {change.key for change in changes}
        if touched & {"pricing.models", "pricing.default", "pricing.currency"}:
            self._pricing = None
        if any(change.key.startswith("logs.") for change in changes):
            setup_logging(self.settings.get_str("logs.level", "INFO"), self.paths.log_dir)
        if "security.session_ttl_hours" in touched:
            self.signer.ttl_seconds = self.settings.get_int("security.session_ttl_hours", 12) * 3600
        if any(change.requires_restart for change in changes):
            self.pending_restart = True
            if self.restart_callback is not None:
                log.info("监听参数已变更，触发服务热重绑定")
                try:
                    await self.restart_callback()
                    self.pending_restart = False
                except Exception:
                    log.exception("热重绑定失败，请手动重启服务")

    async def restart_signal(self) -> None:
        """由外层宿主注册的重绑定回调。"""
        if self.restart_callback is not None:
            await self.restart_callback()
            self.pending_restart = False
