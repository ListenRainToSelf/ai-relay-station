"""pytest 夹具：临时数据目录、假上游、ASGI 客户端与真实端口服务。

异步用例统一走 anyio 插件（环境里没有 pytest-asyncio）。
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn

from airelay.context import AppContext
from airelay.host import AppHost
from airelay.paths import AppPaths
from airelay.security import load_or_create_secrets
from airelay.settings import SettingsService

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_upstream import create_app as create_mock_app  # noqa: E402
from mock_upstream import reset_state  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_http(url: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code < 500:
                return True
        except httpx.HTTPError:
            time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
@pytest.fixture
def mock_upstream() -> Iterator[str]:
    """在独立线程里起假上游，返回其 base_url。"""
    reset_state()
    port = free_port()
    config = uvicorn.Config(create_mock_app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mock-upstream", daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    if not wait_for_http(f"{base_url}/v1/models"):
        raise RuntimeError("假上游启动失败")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture
def mock_state():
    import mock_upstream

    return mock_upstream


# --------------------------------------------------------------------------- #
@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def app_context(data_dir: Path, mock_upstream: str) -> Iterator[AppContext]:
    """已启动的应用上下文（引擎/连接池建在当前事件循环里）。"""
    paths = AppPaths.build(data_dir)
    settings = SettingsService()
    settings.set_overrides(
        {
            "gateway.max_retries": 1,
            "gateway.channel_cooldown_seconds": 30,
            "monitoring.push_interval_ms": 500,
            "logs.access_log": False,
        }
    )
    ctx = AppContext(
        paths=paths,
        settings=settings,
        secrets=load_or_create_secrets(paths.data_dir),
        mode="server",
    )
    yield ctx


@pytest.fixture
async def ctx(app_context: AppContext) -> Any:
    await app_context.startup()
    try:
        yield app_context
    finally:
        await app_context.shutdown()


@pytest.fixture
async def client(ctx: AppContext) -> Any:
    from airelay.main import create_app

    app = create_app(ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as http_client:
        yield http_client


@pytest.fixture
async def remote_client(ctx: AppContext) -> Any:
    """来源不是环回地址的客户端：用来验证管理面的令牌要求。"""
    from airelay.main import create_app

    app = create_app(ctx)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.7", 45678))
    async with httpx.AsyncClient(transport=transport, base_url="http://relay.test") as http_client:
        yield http_client


@pytest.fixture
def admin_headers(ctx: AppContext) -> dict[str, str]:
    """管理面直接用 X-Admin-Token，避免在每个用例里处理 Cookie。"""
    return {"X-Admin-Token": ctx.admin_token}


# --------------------------------------------------------------------------- #
@pytest.fixture
def live_server(app_context: AppContext) -> Iterator[str]:
    """真实端口服务（用于 WebSocket 与真 HTTP 联调）。"""
    host = AppHost(app_context, host="127.0.0.1", port=free_port())
    app_context.restart_callback = None
    if not host.start(wait=True):
        raise RuntimeError(f"网关启动失败：{host.bind_error}")
    base_url = f"http://127.0.0.1:{host.port}"
    if not wait_for_http(f"{base_url}/healthz"):
        host.stop()
        raise RuntimeError("网关未在预期时间内就绪")
    try:
        yield base_url
    finally:
        host.stop()


# --------------------------------------------------------------------------- #
async def create_channel(
    ctx: AppContext,
    *,
    name: str,
    provider_type: str,
    base_url: str,
    api_key: str = "sk-upstream-test",
    priority: int = 0,
    weight: int = 1,
    models: list[str] | None = None,
    status: str = "active",
    **extra: Any,
) -> dict[str, Any]:
    async with ctx.session_factory() as session:
        record = await ctx.channels.create(
            session,
            {
                "name": name,
                "provider_type": provider_type,
                "base_url": base_url,
                "api_key": api_key,
                "priority": priority,
                "weight": weight,
                "models": models or [],
                "status": status,
                **extra,
            },
        )
        return ctx.channels.to_public(record)


async def create_key(
    ctx: AppContext,
    *,
    name: str = "test-key",
    quota_limit: int = 0,
    model_allowed: list[str] | None = None,
    rpm_limit: int = 0,
    tpm_limit: int = 0,
    expires_in_days: float | None = None,
    status: str = "active",
) -> tuple[dict[str, Any], str]:
    async with ctx.session_factory() as session:
        record, plaintext = await ctx.keys.create(
            session,
            {
                "name": name,
                "quota_limit": quota_limit,
                "model_allowed": model_allowed or [],
                "rpm_limit": rpm_limit,
                "tpm_limit": tpm_limit,
                "expires_in_days": expires_in_days,
                "status": status,
            },
            defaults={"rpm": 0, "tpm": 0},
        )
        return ctx.keys.to_public(record), plaintext


def auth_header(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}
