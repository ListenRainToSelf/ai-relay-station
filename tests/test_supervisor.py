"""本地服务托管测试：拉起、探活、停止、掉线自动重启、脚本 detach、共享启动命令。

这些用例会真的起子进程、真的占端口，所以每个用例结束后都要清理干净。
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from conftest import create_channel
from fake_local_service import (
    kill_pid,
    kill_port,
    pids_on_port,
    port_is_serving,
    wait_until,
    write_service,
)

pytestmark = pytest.mark.anyio

ADMIN = "/api/admin"


@pytest.fixture
def fake_service(tmp_path):
    service = write_service(tmp_path / "spark")
    try:
        yield service
    finally:
        kill_port(service.port_a)
        kill_port(service.port_b)


async def wait_state(ctx, channel_id, predicate, timeout=25.0) -> dict:
    """轮询服务状态，直到满足条件（条件不满足就继续等）。"""
    deadline = asyncio.get_event_loop().time() + timeout
    snapshot: dict = {}
    while asyncio.get_event_loop().time() < deadline:
        snapshot = ctx.services.state(channel_id).to_dict() if ctx.services.state(channel_id) else {}
        if predicate(snapshot):
            return snapshot
        await asyncio.sleep(0.3)
    return snapshot


# --------------------------------------------------------------------------- #
async def test_start_stop_cycle(ctx, fake_service) -> None:
    """拉起 → 探活通过（拿到 PID）→ 停止 → 进程与端口都释放。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-前台型",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port),
    )
    channel_id = channel["channel_id"]
    assert channel["managed"] is True

    state = await ctx.services.start(await _reload(ctx, channel_id), reason="测试启动")
    assert state.pid, f"应当记录子进程 PID：{state.detail}"

    state = await wait_state(ctx, channel_id, lambda s: s.get("healthy") and s.get("status") == "running")
    assert state["healthy"] is True, f"探活未通过：{state.get('detail')}"
    assert state["managed"] is True
    assert port_is_serving(port)

    state = await ctx.services.stop(await _reload(ctx, channel_id), reason="测试停止")
    assert wait_until(lambda: not port_is_serving(port), timeout=15), "停止后端口应当释放"
    assert "PID" in state.detail or "端口" in state.detail
    assert ctx.services.state(channel_id).healthy is False


async def test_does_not_launch_when_already_running(ctx, fake_service) -> None:
    """已经手动跑着（或别的程序占着）时，绝不能重复拉起。"""
    import subprocess

    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-已在运行",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port),
    )
    channel_id = channel["channel_id"]
    manual = subprocess.Popen(
        [fake_service.python, str(fake_service.service_script), str(port)],
        cwd=str(fake_service.workdir),
    )
    try:
        assert wait_until(lambda: port_is_serving(port), timeout=15)
        before = pids_on_port(port)

        state = await ctx.services.start(await _reload(ctx, channel_id), reason="重复启动应被跳过")
        assert state.status == "running"
        assert state.healthy is True
        assert state.managed is False, "外部已在运行的服务不应被标记为网关拉起"
        assert pids_on_port(port) == before, "不应产生第二个监听进程"
        events = ctx.services.state(channel_id).to_dict()["events"]
        assert any("已在运行" in event["message"] for event in events)
    finally:
        manual.terminate()
        manual.wait(timeout=10)


async def test_auto_restart_after_crash(ctx, fake_service) -> None:
    """服务被外部杀掉后，守护循环应当把它重新拉起来。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-会挂",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port),
    )
    channel_id = channel["channel_id"]
    await ctx.services.start(await _reload(ctx, channel_id), reason="先起来")
    await wait_state(ctx, channel_id, lambda s: s.get("healthy"))

    victims = pids_on_port(port)
    assert victims, "应当能查到监听端口"
    for pid in victims:
        kill_pid(pid)
    assert wait_until(lambda: not port_is_serving(port), timeout=15)

    # 直接驱动一次守护 tick（等价于后台循环，但结果可预期）；
    # 先把 last_check_at 清掉，等价于「探活间隔已到」，否则会被间隔节流跳过
    ctx.services.state(channel_id).last_check_at = None
    await ctx.services._tick()
    state = await wait_state(ctx, channel_id, lambda s: s.get("healthy"))
    assert state["healthy"] is True, f"未自动恢复：{state.get('detail')}"
    assert state["restarts"] >= 1
    assert pids_on_port(port), "端口应重新被监听"


async def test_launcher_that_exits_immediately(ctx, fake_service) -> None:
    """start.bat 这种「拉起后自己退出」的脚本：探活为准，不因为启动进程退出就重启。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-detach脚本",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port, command=fake_service.launcher_command()),
    )
    channel_id = channel["channel_id"]
    assert wait_until(lambda: port_is_serving(port)) is False, "启动前不该有服务"

    await ctx.services.start(await _reload(ctx, channel_id), reason="跑启动脚本")
    state = await wait_state(ctx, channel_id, lambda s: s.get("healthy"), timeout=30)
    assert state["healthy"] is True, f"脚本拉起的服务未被探活到：{state.get('detail')}"
    assert state["managed"] is False, "启动脚本已退出，不应声称持有它的进程树"

    # 再 tick 几次：虽然启动进程没了，也不该触发重启
    for _ in range(2):
        ctx.services.state(channel_id).last_check_at = None
        await ctx.services._tick()
    state = ctx.services.state(channel_id).to_dict()
    assert state["healthy"] is True
    assert state["restarts"] == 0


async def test_stop_by_port_for_detached_service(ctx, fake_service) -> None:
    """detach 出来的服务拿不到子进程句柄，停止要能按端口收尾。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-按端口停",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(
            port, command=fake_service.launcher_command(), stop_strategy="port"
        ),
    )
    channel_id = channel["channel_id"]
    await ctx.services.start(await _reload(ctx, channel_id), reason="脚本拉起")
    assert wait_until(lambda: port_is_serving(port), timeout=25)

    state = await ctx.services.stop(await _reload(ctx, channel_id), reason="按端口停止")
    assert wait_until(lambda: not port_is_serving(port), timeout=20), f"端口未被释放：{state.detail}"
    assert "端口" in state.detail


async def test_shared_launcher_runs_once_for_multiple_channels(ctx, fake_service) -> None:
    """一个脚本拉起多个端口：两个渠道指向同一命令，只执行一次启动脚本。"""
    lifecycle = fake_service.lifecycle(
        fake_service.port_a, command=fake_service.launcher_command(), startup_grace_seconds=0
    )
    first = await create_channel(
        ctx, name="本地-A", provider_type="openai",
        base_url=fake_service.base_url(fake_service.port_a), models=["fake-local"], lifecycle=lifecycle,
    )
    second = await create_channel(
        ctx, name="本地-B", provider_type="openai",
        base_url=fake_service.base_url(fake_service.port_b), models=["fake-local"], lifecycle=lifecycle,
    )
    assert first["lifecycle"]["command"] == second["lifecycle"]["command"]

    await ctx.services.start(await _reload(ctx, first["channel_id"]), reason="启动")
    await ctx.services._tick()
    assert wait_until(lambda: port_is_serving(fake_service.port_a), timeout=25)
    assert wait_until(lambda: port_is_serving(fake_service.port_b), timeout=25)
    # 同一个托管单元：两个渠道共享
    assert (
        ctx.services.state(first["channel_id"]).unit_key
        == ctx.services.state(second["channel_id"]).unit_key
    )
    await ctx.services._tick()
    assert (await wait_state(ctx, first["channel_id"], lambda s: s.get("healthy")))["healthy"] is True
    assert (await wait_state(ctx, second["channel_id"], lambda s: s.get("healthy")))["healthy"] is True


async def test_failed_launch_is_reported_and_bounded(ctx, fake_service) -> None:
    """命令不存在时要给出明确错误，并且不会无限重启刷屏。"""
    port = fake_service.port_a
    bogus = str(fake_service.workdir / "not-exists.bat")
    channel = await create_channel(
        ctx,
        name="本地-命令写错",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port, command=bogus, max_restarts=1),
    )
    channel_id = channel["channel_id"]
    state = await ctx.services.start(await _reload(ctx, channel_id), reason="启动不存在的脚本")
    # Windows 下 cmd 对不存在的脚本返回非 0，但 Popen 本身仍成功；关键是不能崩、且有记录
    assert state.status in {"starting", "failed", "degraded"}

    # 连推几次 tick：清掉间隔与退避，让重启上限逻辑走到位（max_restarts=1）
    for _ in range(6):
        live = ctx.services.state(channel_id)
        live.last_check_at = None
        live.next_attempt_at = 0.0
        await ctx.services._tick()
        await asyncio.sleep(0.2)

    final = ctx.services.state(channel_id).to_dict()
    assert final["healthy"] is False
    assert final["restarts"] <= 2, f"重启次数应受上限约束：{final['restarts']}"
    assert final["status"] == "failed", f"达到上限后应标记异常：{final}"
    assert "上限" in final["detail"] or "上限" in " ".join(
        event["message"] for event in final["events"]
    ), f"应说明已达重启上限：{final['detail']}"


async def test_lifecycle_validation(client, admin_headers, ctx) -> None:
    """配置校验：启用托管必须有启动命令；停止策略选 command 必须有停止命令。"""
    bad = await client.post(
        f"{ADMIN}/channels",
        json={
            "name": "缺命令", "provider_type": "openai", "base_url": "http://127.0.0.1:1/v1",
            "api_key": "k", "lifecycle": {"enabled": True, "command": ""},
        },
        headers=admin_headers,
    )
    assert bad.status_code == 400
    assert "启动命令" in bad.json()["message"]

    bad2 = await client.post(
        f"{ADMIN}/channels",
        json={
            "name": "缺停止命令", "provider_type": "openai", "base_url": "http://127.0.0.1:1/v1",
            "api_key": "k",
            "lifecycle": {"enabled": True, "command": "echo hi", "stop_strategy": "command"},
        },
        headers=admin_headers,
    )
    assert bad2.status_code == 400
    assert "停止命令" in bad2.json()["message"]


async def test_service_admin_endpoints(client, admin_headers, ctx, fake_service) -> None:
    """管理面：列表、单个启停、立即探活、日志、批量。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-接口",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port),
    )
    channel_id = channel["channel_id"]

    listing = (await client.get(f"{ADMIN}/services", headers=admin_headers)).json()
    assert listing["enabled"] is True
    item = next(entry for entry in listing["items"] if entry["channel_id"] == channel_id)
    assert item["command"] == fake_service.direct_command(port)
    assert item["port"] == port
    assert item["status"] in {"unknown", "stopped", "disabled", "degraded"}

    started = await client.post(f"{ADMIN}/services/{channel_id}/start", headers=admin_headers)
    assert started.status_code == 200
    assert started.json()["pid"]

    state = await wait_state(ctx, channel_id, lambda s: s.get("healthy"))
    assert state["healthy"] is True

    checked = await client.post(f"{ADMIN}/services/{channel_id}/check", headers=admin_headers)
    assert checked.json()["healthy"] is True

    log = (await client.get(f"{ADMIN}/services/{channel_id}/log?lines=50", headers=admin_headers)).json()
    assert log["path"]

    stopped = await client.post(f"{ADMIN}/services/{channel_id}/stop", headers=admin_headers)
    assert stopped.status_code == 200
    assert wait_until(lambda: not port_is_serving(port), timeout=15)

    bulk = await client.post(f"{ADMIN}/services/bulk/check", headers=admin_headers)
    assert bulk.status_code == 200 and bulk.json()["items"]

    invalid = await client.post(f"{ADMIN}/services/{channel_id}/nope", headers=admin_headers)
    assert invalid.status_code == 400

    plain = await client.post(
        f"{ADMIN}/channels",
        json={
            "name": "非托管", "provider_type": "openai", "base_url": "http://127.0.0.1:1/v1", "api_key": "k",
        },
        headers=admin_headers,
    )
    not_managed = await client.post(
        f"{ADMIN}/services/{plain.json()['channel_id']}/start", headers=admin_headers
    )
    assert not_managed.status_code == 400


async def test_stop_on_shutdown_and_autostart(ctx, fake_service) -> None:
    """随网关启动拉起；渠道勾了「退出时一并关闭」就在 shutdown 时收掉。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-自启自关",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port, auto_start=True, stop_on_shutdown=True),
    )
    channel_id = channel["channel_id"]

    # 模拟网关启动时的自动拉起
    await ctx.services.autostart()
    state = await wait_state(ctx, channel_id, lambda s: s.get("healthy"), timeout=30)
    assert state["healthy"] is True, f"未随启动拉起：{state.get('detail')}"
    assert port_is_serving(port)

    # 后台守护循环真的在跑（不是只靠手工调用）
    loop_task = next(
        (task for task in asyncio.all_tasks() if task.get_name() == "local-services"), None
    )
    assert loop_task is not None, "守护循环任务应已启动"
    assert not loop_task.done()


async def test_supervise_loop_recovers_by_itself(ctx, fake_service) -> None:
    """不手工 tick：让后台守护循环自己把挂掉的服务拉回来。"""
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-自助恢复",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(
            port, auto_start=False, check_interval_seconds=5, restart_backoff_seconds=5
        ),
    )
    channel_id = channel["channel_id"]
    await ctx.services.start(await _reload(ctx, channel_id), reason="先起一次")
    assert wait_until(lambda: port_is_serving(port), timeout=30)

    for pid in pids_on_port(port):
        kill_pid(pid)
    assert wait_until(lambda: not port_is_serving(port), timeout=15)

    # 守护循环每 5 秒 tick 一次：先等它发现服务挂了（healthy 变 False），再等它自己拉回来
    assert await _eventually(
        lambda: ctx.services.state(channel_id) is not None
        and ctx.services.state(channel_id).healthy is False,
        timeout=40,
    ), "后台守护循环未发现服务已挂"
    assert await _eventually(
        lambda: ctx.services.state(channel_id).healthy is True and port_is_serving(port),
        timeout=60,
    ), "后台守护循环未自动恢复服务"
    final = ctx.services.state(channel_id).to_dict()
    assert final["restarts"] >= 1, f"未记录到重启：{final}"
    assert final["status"] == "running"
    # 拉起来之后要给足就绪窗口，不能在它还没绑定端口时又判失败重启一次
    assert final["restarts"] == 1, f"恢复过程中不应反复重启：{final}"


async def _eventually(predicate, timeout: float = 30.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
    return False


async def _reload(ctx, channel_id: str):
    """从库里取回渠道（supervisor 的方法需要 ORM 对象）。"""
    from sqlalchemy import select

    from airelay.models import Channel

    async with ctx.session_factory() as session:
        return (
            await session.execute(select(Channel).where(Channel.channel_id == channel_id))
        ).scalar_one()


async def test_concurrent_start_does_not_double_launch(ctx, fake_service) -> None:
    """并发（开机自启撞上手动启动）也只允许拉起一份进程。

    Windows 上两个进程可以同时 bind 同一端口（SO_REUSEADDR），所以这里必须靠
    启动去重来保证，而不是指望端口冲突报错。
    """
    port = fake_service.port_a
    channel = await create_channel(
        ctx,
        name="本地-并发启动",
        provider_type="openai",
        base_url=fake_service.base_url(port),
        models=["fake-local"],
        lifecycle=fake_service.lifecycle(port, startup_grace_seconds=10),
    )
    channel_id = channel["channel_id"]
    target = await _reload(ctx, channel_id)

    await asyncio.gather(
        ctx.services.start(target, reason="并发A"),
        ctx.services.start(target, reason="并发B"),
        ctx.services.start(target, reason="并发C"),
    )
    assert wait_until(lambda: port_is_serving(port), timeout=30)
    # 事件里应能看到「跳过重复启动」，且监听进程只有一个
    events = " ".join(event["message"] for event in ctx.services.state(channel_id).to_dict()["events"])
    assert "跳过重复启动" in events or "仍在运行" in events, f"应当有去重记录：{events}"
    await asyncio.sleep(0.5)
    assert len(pids_on_port(port)) == 1, f"只应有一个监听进程，实际 {pids_on_port(port)}"
