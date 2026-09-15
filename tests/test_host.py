"""宿主行为测试：端口变更热重绑定、启动失败提示、上下文可重入。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import free_port
from airelay.host import AppHost

pytestmark = pytest.mark.anyio


async def poll_http(url: str, *, timeout: float = 25.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    async with httpx.AsyncClient() as probe:
        while loop.time() < deadline:
            try:
                response = await probe.get(url, timeout=1.0)
                if response.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.15)
    return False


async def test_context_startup_shutdown_is_reentrant(app_context) -> None:
    await app_context.startup()
    assert app_context.ready is True
    assert app_context.engine is not None
    await app_context.shutdown()
    assert app_context.ready is False
    assert app_context.engine is None
    # 再启动一次应当完全可用（热重绑定会走到这条路径）
    await app_context.startup()
    assert app_context.ready is True
    async with app_context.session_factory() as session:
        assert await app_context.mapping.count(session) == 0
    await app_context.shutdown()


async def test_port_change_rebinds_without_process_restart(app_context) -> None:
    port_a = free_port()
    port_b = free_port()
    host = AppHost(app_context, host="127.0.0.1", port=port_a)
    assert host.start(wait=True), host.bind_error
    base_a = f"http://127.0.0.1:{port_a}"
    assert await poll_http(f"{base_a}/healthz")

    headers = {"X-Admin-Token": app_context.admin_token}
    async with httpx.AsyncClient(timeout=20.0) as http:
        response = await http.put(
            f"{base_a}/api/admin/settings",
            json={"values": {"network.port": port_b}},
            headers=headers,
        )
        assert response.status_code == 200
        # 宿主在进程内接管了重绑定，因此请求返回时已经不需要再提示「重启生效」
        assert response.json()["pending_restart"] is False

    # 宿主应当在新端口上重新提供服务，而进程没有退出
    base_b = f"http://127.0.0.1:{port_b}"
    assert await poll_http(f"{base_b}/healthz"), "新端口应当已开始监听"
    assert host.running is True
    assert host.port == port_b

    async with httpx.AsyncClient(timeout=20.0) as http:
        info = (await http.get(f"{base_b}/api/admin/system", headers=headers)).json()
        assert info["port"] == port_b
        assert info["pending_restart"] is False
    host.stop()


async def test_apphost_reports_bind_error_for_invalid_host(app_context) -> None:
    # 用不可绑定的地址触发启动失败分支，验证错误信息会被记录下来
    host = AppHost(app_context, host="203.0.113.250", port=free_port())
    started = host.start(wait=True, timeout=8)
    assert started is False
    assert host.bind_error
    host.stop()


def test_resolve_mode_prefers_server_when_no_gui(monkeypatch) -> None:
    """NAS / 无图形环境：auto 必须退化成无头服务，而不是硬起桌面。"""
    import airelay.__main__ as cli

    monkeypatch.setattr(cli, "tray_available", lambda: False)
    monkeypatch.setattr(cli, "find_chromium", lambda: None)
    assert cli.resolve_mode("auto") == cli.MODE_SERVER

    monkeypatch.setattr(cli, "find_chromium", lambda: "/usr/bin/chromium")
    assert cli.resolve_mode("auto") == cli.MODE_DESKTOP

    # 显式指定永远优先
    assert cli.resolve_mode("server") == cli.MODE_SERVER
    assert cli.resolve_mode("desktop") == cli.MODE_DESKTOP


async def test_settings_are_preloaded_before_binding(app_context) -> None:
    """监听地址/端口也是设置项：不带参数启动时必须先读库，否则会退回默认端口。"""
    import anyio

    from airelay.__main__ import preload_settings
    from airelay.settings import SettingsService

    await app_context.startup()
    async with app_context.session_factory() as session:
        await app_context.settings.update(session, {"network.port": 9999, "network.host": "0.0.0.0"})
    await app_context.shutdown()

    fresh = SettingsService()
    # 未加载前只有默认值
    assert fresh.get_int("network.port", 8000) == 8000
    # preload 是同步入口（main 里没有运行中的事件循环），测试里放到线程里跑
    await anyio.to_thread.run_sync(preload_settings, app_context.paths, fresh)
    assert fresh.get_int("network.port", 8000) == 9999
    assert fresh.get_str("network.host", "127.0.0.1") == "0.0.0.0"
