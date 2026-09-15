"""本地进程托管：把本机推理服务（如 start.bat 拉起的 llama.cpp）纳入网关托管。

解决三件事：
  1. **没起来就拉起来**——网关启动后或运行期间发现某个渠道的后端不通，执行启动命令；
  2. **意外退出自动重启**——周期性健康检查（HTTP 探活），连续失败后按退避策略重启；
  3. **需要时关掉**——手动停止、或网关退出时一并关闭（可配置）。

几个刻意的设计取舍：
  * **健康检查是唯一事实来源**，不看进程是否存活。因为 `start.bat` 这类脚本常常
    拉起后台进程后自己就退出了（甚至 detached），"子进程还在"并不能说明服务可用。
  * **启动前先探活**：已经有人在跑这个端口就绝不重复拉起，避免端口冲突/双份显存占用。
  * **同一条启动命令只会执行一次**：多个渠道可以指向同一个脚本（一个脚本拉起多个端口
    的常见形态），按命令去重成同一个"托管单元"。
  * **停止只动自己该动的**：优先杀我们拉起的进程树；否则只杀监听该渠道端口的那个进程
    （或执行用户配置的停止命令），绝不按进程名乱杀。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from ..adapters.base import join_url
from ..models import Channel
from ..timeutil import to_iso, utcnow

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

# 状态取值
STATUS_DISABLED = "disabled"      # 未配置托管
STATUS_UNKNOWN = "unknown"        # 还没检查过
STATUS_RUNNING = "running"        # 探活通过
STATUS_DEGRADED = "degraded"      # 探活失败但还没到重启阈值
STATUS_STARTING = "starting"      # 刚拉起，等待宽限期
STATUS_RESTARTING = "restarting"
STATUS_STOPPED = "stopped"        # 明确停止 / 未运行且不自动重启
STATUS_FAILED = "failed"          # 超过重启次数上限

MAX_EVENTS = 40


def merge_lifecycle(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把用户配置与默认值合并，并做类型清洗。"""
    data = dict(raw or {})
    command = str(data.get("command") or "").strip()
    merged: dict[str, Any] = {
        "enabled": bool(data.get("enabled", bool(command))),
        "command": command,
        "args": _as_str_list(data.get("args")),
        "workdir": str(data.get("workdir") or "").strip(),
        "env": {str(k): str(v) for k, v in (data.get("env") or {}).items()} if isinstance(data.get("env"), dict) else {},
        "stop_command": str(data.get("stop_command") or "").strip(),
        "stop_strategy": str(data.get("stop_strategy") or "auto"),
        "health_path": str(data.get("health_path") or "/v1/models").strip() or "/v1/models",
        "startup_grace_seconds": _as_int(data.get("startup_grace_seconds"), 40, 0, 3600),
        "auto_start": bool(data.get("auto_start", True)),
        "auto_restart": bool(data.get("auto_restart", True)),
        "stop_on_shutdown": bool(data.get("stop_on_shutdown", False)),
        "check_interval_seconds": _as_int(data.get("check_interval_seconds"), 15, 5, 3600),
        "failure_threshold": _as_int(data.get("failure_threshold"), 3, 1, 60),
        "max_restarts": _as_int(data.get("max_restarts"), 0, 0, 1000),
        "restart_backoff_seconds": _as_int(data.get("restart_backoff_seconds"), 30, 5, 3600),
    }
    if merged["stop_strategy"] not in {"auto", "process", "port", "command"}:
        merged["stop_strategy"] = "auto"
    return merged


def unit_key_of(config: dict[str, Any]) -> str:
    """同一条启动命令 = 同一个托管单元（一个脚本拉起多个端口时只执行一次）。"""
    material = json.dumps(
        [config.get("command"), config.get("args"), config.get("workdir")],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:10]


def port_of_url(url: str) -> int | None:
    match = re.match(r"^[a-zA-Z]+://(?:[^/@]*@)?([^/:?#]+)(?::(\d+))?", (url or "").strip())
    if not match:
        return None
    if match.group(2):
        return int(match.group(2))
    return 443 if url.strip().lower().startswith("https") else 80


def health_url(base_url: str, path: str) -> str:
    """拼健康检查地址。

    渠道的 base_url 通常已经带版本段（OpenAI 兼容类就是 `.../v1`），而默认检查路径
    是 `/v1/models`——直接相加会变成 `.../v1/v1/models`。所以复用适配器里同一套
    去重规则，`/v1`、`/v1beta` 只保留一次。
    """
    return join_url(base_url, (path or "").lstrip("/"))


@dataclass
class Unit:
    """一个被托管的进程单元（可能被多个渠道共享）。"""

    key: str
    command: str
    args: list[str]
    workdir: str
    env: dict[str, str]
    log_path: Path
    process: subprocess.Popen | None = None
    restarts: int = 0
    started_at: datetime | None = None
    last_error: str = ""
    channels: set[str] = field(default_factory=set)
    ports: set[int] = field(default_factory=set)
    external: bool = False  # 探活通过但不是我们拉起的（例如用户手动跑着）

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        """我们持有的子进程 PID（脚本 detach 后启动进程已退出时为空）。"""
        return self.process.pid if self.alive and self.process else None


@dataclass
class ChannelState:
    channel_id: str
    channel_name: str
    unit_key: str = ""
    status: str = STATUS_DISABLED
    healthy: bool = False
    detail: str = ""
    pid: int | None = None
    managed: bool = False
    restarts: int = 0
    consecutive_failures: int = 0
    last_check_at: datetime | None = None
    last_healthy_at: datetime | None = None
    started_at: datetime | None = None
    next_attempt_at: float = 0.0
    latency_ms: float = 0.0
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))

    def note(self, message: str, level: str = "info") -> None:
        self.events.appendleft({"at": to_iso(utcnow()), "level": level, "message": message})
        if level == "error":
            log.warning("[本地服务] %s：%s", self.channel_name, message)
        else:
            log.info("[本地服务] %s：%s", self.channel_name, message)

    def to_dict(self, *, restarts: int | None = None) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "unit_key": self.unit_key,
            "status": self.status,
            "healthy": self.healthy,
            "detail": self.detail,
            "pid": self.pid,
            "managed": self.managed,
            "restarts": self.restarts if restarts is None else restarts,
            "consecutive_failures": self.consecutive_failures,
            "last_check_at": to_iso(self.last_check_at),
            "last_healthy_at": to_iso(self.last_healthy_at),
            "started_at": to_iso(self.started_at),
            "uptime_seconds": (
                (utcnow() - self.started_at).total_seconds() if self.started_at else 0
            ),
            "latency_ms": round(self.latency_ms, 1),
            "retry_in_seconds": max(0, round(self.next_attempt_at - time.monotonic(), 1)),
            "events": list(self.events)[:12],
        }


class LocalServiceSupervisor:
    def __init__(
        self,
        *,
        settings: Any,
        session_factory: Any,
        log_dir: Path,
        http_getter: Callable[[], httpx.AsyncClient | None],
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.log_dir = Path(log_dir) / "services"
        self._http = http_getter
        self._states: dict[str, ChannelState] = {}
        self._units: dict[str, Unit] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ 查询
    def state(self, channel_id: str) -> ChannelState | None:
        return self._states.get(channel_id)

    def snapshot(self) -> list[dict[str, Any]]:
        return [
            state.to_dict(
                restarts=(self._units[state.unit_key].restarts if state.unit_key in self._units else state.restarts)
            )
            for state in sorted(self._states.values(), key=lambda s: s.channel_name)
        ]

    def tail_log(self, channel_id: str, lines: int = 200) -> dict[str, Any]:
        state = self._states.get(channel_id)
        if state is None or not state.unit_key:
            return {"path": "", "content": "", "lines": 0}
        unit = self._units.get(state.unit_key)
        path = unit.log_path if unit else self.log_dir / f"{state.unit_key}.log"
        if not path.is_file():
            return {"path": str(path), "content": "", "lines": 0}
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {"path": str(path), "content": f"（日志读取失败：{exc}）", "lines": 0}
        chunk = text.splitlines()[-max(1, min(lines, 5000)):]
        return {"path": str(path), "content": "\n".join(chunk), "lines": len(chunk)}

    # ------------------------------------------------------------------ 健康检查
    async def check(self, channel: Channel) -> tuple[bool, str, float]:
        """探活：任何 HTTP 响应都算「服务在跑」（401 也说明端口后面有东西）。"""
        config = channel.lifecycle_config()
        url = health_url(channel.base_url, config["health_path"])
        if not url:
            return False, "渠道没配 base_url，无法探活", 0.0
        client = self._http()
        if client is None:
            return False, "网关尚未完成初始化", 0.0
        started = time.perf_counter()
        try:
            response = await client.request(
                "GET", url, timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0)
            )
            latency = (time.perf_counter() - started) * 1000
            detail = f"HTTP {response.status_code}"
            if response.status_code >= 500:
                # 起了但内部报错，按「不健康」处理，但保留细节
                return False, detail + "（服务端错误）", latency
            if response.status_code in (401, 403):
                # 本机服务常要求 API Key（如 llama-server --api-key）。探活只判断「端口后面有没有东西」，
                # 不带密钥，所以 401 恰恰说明服务在正常应答。
                detail += "（在跑，需鉴权）"
            return True, detail, latency
        except httpx.TimeoutException:
            return False, "探活超时", (time.perf_counter() - started) * 1000
        except httpx.HTTPError as exc:
            return False, f"连接失败：{type(exc).__name__}", (time.perf_counter() - started) * 1000

    # ------------------------------------------------------------------ 启停
    async def start(self, channel: Channel, *, reason: str = "手动启动") -> ChannelState:
        config = channel.lifecycle_config()
        state = self._state_for(channel, config)
        if not config["command"]:
            state.detail = "没有配置启动命令"
            state.status = STATUS_DISABLED
            return state
        # 串行化：并发的两个 start（开机自启与手动启动同时发生）不能各拉一份进程
        async with self._lock:
            return await self._start_locked(channel, config, state, reason=reason)

    async def _start_locked(
        self, channel: Channel, config: dict[str, Any], state: ChannelState, *, reason: str
    ) -> ChannelState:
        healthy, detail, latency = await self.check(channel)
        if healthy and state.unit_key not in self._units:
            # 已经有人在跑这个端口：只登记，不重复拉起（避免端口冲突与双份显存）
            await self._register_external(channel, config, detail, latency)
            state.note(f"检测到服务已在运行（{detail}），不重复启动")
            return state

        unit = self._units.get(state.unit_key)
        if unit is not None and unit.alive:
            state.note("启动被跳过：该启动命令的进程仍在运行")
            return state
        return await self._launch(channel, config, reason=reason)

    async def stop(self, channel: Channel, *, reason: str = "手动停止") -> ChannelState:
        async with self._lock:
            return await self._stop_locked(channel, reason=reason)

    async def _stop_locked(self, channel: Channel, *, reason: str) -> ChannelState:
        config = channel.lifecycle_config()
        state = self._state_for(channel, config)
        state.status = STATUS_STOPPED
        state.healthy = False
        state.next_attempt_at = 0.0
        state.consecutive_failures = 0
        unit = self._units.get(state.unit_key)
        results = await self._stop_unit(unit, config, [channel])
        state.detail = "；".join(results) or "没有需要停止的进程"
        state.pid = None
        state.managed = False
        state.started_at = None
        if unit is not None:
            unit.process = None
            unit.started_at = None
        state.note(f"{reason}：{state.detail}")
        return state

    async def restart(self, channel: Channel, *, reason: str = "手动重启") -> ChannelState:
        async with self._lock:
            return await self._restart_locked(channel, reason=reason)

    async def _restart_locked(self, channel: Channel, *, reason: str) -> ChannelState:
        config = channel.lifecycle_config()
        state = self._state_for(channel, config)
        unit = self._units.get(state.unit_key)
        await self._stop_unit(unit, config, [channel])
        if unit is not None:
            unit.process = None
            unit.started_at = None
        state.note(f"{reason}：已停止旧进程，准备重新拉起")
        return await self._launch(channel, config, reason=reason)

    async def check_now(self, channel: Channel) -> ChannelState:
        config = channel.lifecycle_config()
        state = self._state_for(channel, config)
        healthy, detail, latency = await self.check(channel)
        self._apply_health(state, healthy, detail, latency)
        return state

    # ------------------------------------------------------------------ 内部
    def _state_for(self, channel: Channel, config: dict[str, Any] | None = None) -> ChannelState:
        state = self._states.get(channel.channel_id)
        if state is None:
            state = ChannelState(channel_id=channel.channel_id, channel_name=channel.name or channel.channel_id)
            self._states[channel.channel_id] = state
        config = config if config is not None else channel.lifecycle_config()
        state.channel_name = channel.name or channel.channel_id
        state.unit_key = unit_key_of(config) if config.get("command") else ""
        state.status = state.status if config.get("enabled") else STATUS_DISABLED
        return state

    def _apply_health(self, state: ChannelState, healthy: bool, detail: str, latency: float) -> None:
        # 重启次数的事实来源是托管单元（命令级），这里镜像到渠道状态方便前端直接读
        unit = self._units.get(state.unit_key)
        if unit is not None:
            state.restarts = unit.restarts
        state.healthy = healthy
        state.detail = detail
        state.latency_ms = latency
        state.last_check_at = utcnow()
        if healthy:
            if state.consecutive_failures:
                state.note(f"探活恢复正常（{detail}）")
            state.consecutive_failures = 0
            state.last_healthy_at = state.last_check_at
            # 探活通过就是「运行中」，不管之前标记成 starting 还是 degraded；
            # pid/managed 只表示「我们是否持有它的活进程」——脚本 detach 后自己不占进程也很正常。
            state.status = STATUS_RUNNING
            unit = self._units.get(state.unit_key)
            if unit is not None and unit.alive:
                state.pid = unit.pid
                state.managed = True
                state.started_at = state.started_at or unit.started_at
            else:
                state.pid = None
                state.managed = False
        else:
            state.consecutive_failures += 1
            if state.status not in (STATUS_STARTING, STATUS_RESTARTING):
                state.status = STATUS_DEGRADED

    async def _register_external(self, channel: Channel, config: dict[str, Any], detail: str, latency: float) -> None:
        key = unit_key_of(config)
        unit = self._units.get(key)
        if unit is None:
            unit = Unit(
                key=key,
                command=config["command"],
                args=config["args"],
                workdir=config["workdir"],
                env=config["env"],
                log_path=self._log_path_for(key, config),
                external=True,
            )
            self._units[key] = unit
        unit.channels.add(channel.channel_id)
        port = port_of_url(channel.base_url)
        if port:
            unit.ports.add(port)
        state = self._state_for(channel, config)
        state.status = STATUS_RUNNING
        state.healthy = True
        state.detail = detail
        state.latency_ms = latency
        state.last_check_at = utcnow()
        state.last_healthy_at = state.last_check_at
        state.managed = False
        state.consecutive_failures = 0

    async def _launch(self, channel: Channel, config: dict[str, Any], *, reason: str) -> ChannelState:
        key = unit_key_of(config)
        unit = self._units.get(key)
        if unit is None:
            unit = Unit(
                key=key,
                command=config["command"],
                args=config["args"],
                workdir=config["workdir"],
                env=config["env"],
                log_path=self._log_path_for(key, config),
            )
            self._units[key] = unit
        # 刚拉起过（还在宽限期内）就不要再执行一次启动命令：脚本可能还在加载、端口还没监听，
        # 重复执行会起出第二份进程（Windows 上甚至能同时绑上同一端口）。
        if unit.started_at is not None:
            elapsed = (utcnow() - unit.started_at).total_seconds()
            if elapsed < max(config["startup_grace_seconds"], 10):
                state = self._state_for(channel, config)
                state.detail = f"刚启动 {elapsed:.0f}s，仍在等待就绪，跳过重复启动"
                state.note(state.detail)
                return state
        unit.channels.add(channel.channel_id)
        port = port_of_url(channel.base_url)
        if port:
            unit.ports.add(port)

        state = self._state_for(channel, config)
        state.status = STATUS_STARTING
        state.started_at = utcnow()
        state.pid = None
        # 刚拉起的进程要有一段「就绪窗口」：模型加载可能几十秒，端口也还没监听。
        # 窗口内即便探活失败也不重启，并且把失败计数清零——否则会把「正在启动」
        # 误判成「反复失败」，出现刚拉起来又立刻重启的抖动。
        state.consecutive_failures = 0
        ready_window = max(
            config["startup_grace_seconds"],
            config["restart_backoff_seconds"],
        ) + config["check_interval_seconds"]
        state.next_attempt_at = time.monotonic() + ready_window

        try:
            process = await asyncio.to_thread(self._spawn, unit, config)
        except Exception as exc:  # 命令写错、脚本不存在等
            state.status = STATUS_FAILED
            state.detail = f"启动失败：{exc}"
            unit.last_error = str(exc)
            state.note(f"{reason} 失败：{exc}", "error")
            return state

        unit.process = process
        unit.started_at = state.started_at
        unit.external = False
        state.pid = process.pid
        state.managed = True
        state.detail = f"已拉起 PID {process.pid}，等待就绪（宽限 {config['startup_grace_seconds']}s）"
        state.note(f"{reason}：执行 {config['command']}{' ' + ' '.join(config['args']) if config['args'] else ''}")
        return state

    def _spawn(self, unit: Unit, config: dict[str, Any]) -> subprocess.Popen:
        """同步启动子进程（在线程里跑，避免阻塞事件循环）。"""
        command = config["command"]
        args = list(config["args"])
        workdir = config["workdir"] or str(Path(command).expanduser().parent if os.path.isabs(command) else Path.cwd())
        if not Path(workdir).is_dir():
            workdir = str(Path.cwd())

        # .bat/.cmd/.sh 这类要交给 shell 执行；路径带空格时补引号
        program = command
        if " " in program and not program.startswith('"'):
            program = f'"{program}"'
        full = " ".join([program, *[shlex.quote(a) if not IS_WINDOWS else a for a in args]]).strip()

        environment = dict(os.environ)
        environment.update({k: v for k, v in (config.get("env") or {}).items()})
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")

        unit.log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(unit.log_path, "ab", buffering=0)
        log_file.write(
            f"\n===== {utcnow().isoformat()}Z 启动：{full}（cwd={workdir}）=====\n".encode("utf-8")
        )
        kwargs: dict[str, Any] = {
            "cwd": workdir,
            "env": environment,
            "shell": True,
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
        }
        if IS_WINDOWS:
            # 不弹控制台窗口；独立进程组便于整组结束
            kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return subprocess.Popen(full, **kwargs)
        finally:
            log_file.close()

    def _log_path_for(self, key: str, config: dict[str, Any]) -> Path:
        stem = Path(str(config.get("command") or "service")).name
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "-", stem).strip("-") or "service"
        return self.log_dir / f"{safe}-{key}.log"

    async def _stop_unit(self, unit: Unit | None, config: dict[str, Any], channels: list[Channel]) -> list[str]:
        """按策略停止：自管进程树 → 用户停止命令 → 端口占用者。"""
        results: list[str] = []
        strategy = config.get("stop_strategy", "auto")
        if unit is not None and unit.alive and strategy in ("auto", "process"):
            pid = unit.process.pid if unit.process else unit.pid
            if pid:
                if await self._kill_tree(pid):
                    results.append(f"已终止托管进程树 PID {pid}")
                else:
                    results.append(f"终止 PID {pid} 失败")

        if strategy in ("auto", "command") and config.get("stop_command"):
            ok, message = await self._run_stop_command(config)
            results.append(message)
            if ok and strategy == "command":
                return results

        if strategy in ("auto", "port"):
            ports = set()
            if unit is not None:
                ports |= unit.ports
            for channel in channels:
                port = port_of_url(channel.base_url)
                if port:
                    ports.add(port)
            for port in sorted(ports):
                killed = await self._kill_listener(port)
                results.extend(killed)
        return results

    async def _run_stop_command(self, config: dict[str, Any]) -> tuple[bool, str]:
        command = config["stop_command"]
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                shell=True,
                cwd=config.get("workdir") or None,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=60,
            )
            text = (completed.stdout or completed.stderr or b"").decode("utf-8", "replace").strip()
            if completed.returncode == 0:
                return True, f"已执行停止命令（{text.splitlines()[-1][:80] if text else '无输出'}）"
            return False, f"停止命令返回 {completed.returncode}"
        except subprocess.TimeoutExpired:
            return False, "停止命令超时"
        except Exception as exc:  # noqa: BLE001
            return False, f"停止命令执行失败：{exc}"

    async def _kill_tree(self, pid: int) -> bool:
        def run() -> bool:
            try:
                if IS_WINDOWS:
                    completed = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=30,
                    )
                    return completed.returncode == 0
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.25)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                return True
            except ProcessLookupError:
                return True  # 已经没了
            except Exception as exc:  # noqa: BLE001
                log.debug("终止进程 %s 失败：%s", pid, exc)
                return False

        return await asyncio.to_thread(run)

    async def _kill_listener(self, port: int) -> list[str]:
        """杀掉监听该端口的进程（脚本 detach 后我们拿不到子进程句柄时的兜底）。"""
        def run() -> list[str]:
            results: list[str] = []
            pids = _listener_pids(port)
            if not pids:
                results.append(f"端口 {port} 上没有监听进程")
                return results
            for pid, name in pids:
                if pid == os.getpid():
                    continue
                try:
                    if IS_WINDOWS:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
                    else:
                        os.kill(pid, signal.SIGTERM)
                    results.append(f"已终止端口 {port} 的监听进程 {name}({pid})")
                except Exception as exc:  # noqa: BLE001
                    results.append(f"终止 {name}({pid}) 失败：{exc}")
            return results

        return await asyncio.to_thread(run)

    # ------------------------------------------------------------------ 批量与循环
    async def managed_channels(self) -> list[Channel]:
        from sqlalchemy import select

        async with self.session_factory() as session:
            rows = (await session.execute(select(Channel))).scalars().all()
        return [channel for channel in rows if channel.lifecycle_config()["enabled"]]

    async def autostart(self) -> None:
        """网关启动后：把标记了「自动启动」的本地服务拉起来（先探活，已在跑就不动）。"""
        if not self.settings.get_bool("services.supervisor_enabled", True):
            return
        if not self.settings.get_bool("services.autostart_on_boot", True):
            log.info("本地服务：随网关自动启动已在设置里关闭")
            return
        for channel in await self.managed_channels():
            config = channel.lifecycle_config()
            if not config.get("auto_start"):
                continue
            try:
                healthy, detail, latency = await self.check(channel)
            except Exception:  # noqa: BLE001
                healthy, detail, latency = False, "探活异常", 0.0
            if healthy:
                await self._register_external(channel, config, detail, latency)
                self._state_for(channel, config).note(f"已在运行（{detail}），无需启动")
                continue
            # 走加锁的 start()，与手动启停串行
            await self.start(channel, reason="随网关自动启动")

    async def stop_all(self, *, reason: str = "随网关关闭") -> None:
        if not self.settings.get_bool("services.supervisor_enabled", True):
            return
        stop_everything = self.settings.get_bool("services.stop_on_shutdown", False)
        for channel in await self.managed_channels():
            config = channel.lifecycle_config()
            if not (stop_everything or config.get("stop_on_shutdown")):
                continue
            try:
                await self.stop(channel, reason=reason)
            except Exception:  # noqa: BLE001
                log.exception("关闭本地服务失败：%s", channel.name)

    async def bulk(self, action: str) -> list[dict[str, Any]]:
        results = []
        for channel in await self.managed_channels():
            try:
                if action == "start":
                    state = await self.start(channel, reason="批量启动")
                elif action == "stop":
                    state = await self.stop(channel, reason="批量停止")
                elif action == "restart":
                    state = await self.restart(channel, reason="批量重启")
                else:
                    state = await self.check_now(channel)
                results.append(state.to_dict())
            except Exception as exc:  # noqa: BLE001
                results.append({"channel_id": channel.channel_id, "channel_name": channel.name,
                                "status": STATUS_FAILED, "detail": str(exc)})
        return results

    async def supervise_loop(self, stop_event: asyncio.Event) -> None:
        """周期性探活 + 按策略重启。任何异常都不能让这个循环死掉。"""
        tick = 5.0
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick)
                return
            except asyncio.TimeoutError:
                pass
            try:
                if self.settings.get_bool("services.supervisor_enabled", True):
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 守护循环必须存活
                log.exception("本地服务守护循环异常")

    async def _tick(self) -> None:
        now = time.monotonic()
        for channel in await self.managed_channels():
            config = channel.lifecycle_config()
            state = self._state_for(channel, config)
            interval = config["check_interval_seconds"] or self.settings.get_int(
                "services.check_interval_seconds", 15
            )
            if state.last_check_at is not None:
                elapsed = (utcnow() - state.last_check_at).total_seconds()
                if elapsed < interval:
                    continue
            healthy, detail, latency = await self.check(channel)
            self._apply_health(state, healthy, detail, latency)
            if healthy:
                continue

            # ---- 不健康：走重启决策 ----
            if not config.get("auto_restart"):
                state.status = STATUS_STOPPED
                continue
            unit = self._units.get(state.unit_key)
            if state.status == STATUS_STARTING and now < state.next_attempt_at:
                # 刚拉起，还在宽限期内，给它时间加载模型
                continue
            if state.consecutive_failures < config["failure_threshold"]:
                state.status = STATUS_DEGRADED
                continue
            if unit is not None and unit.last_error and state.status == STATUS_FAILED:
                continue
            if config["max_restarts"] and (unit.restarts if unit else 0) >= config["max_restarts"]:
                state.status = STATUS_FAILED
                state.detail = f"已达重启上限（{config['max_restarts']} 次），停止自动重启"
                continue
            if now < state.next_attempt_at:
                continue

            backoff = min(
                config["restart_backoff_seconds"] * max(1, unit.restarts if unit else 1),
                config["restart_backoff_seconds"] * 10,
            )
            state.next_attempt_at = now + backoff
            state.status = STATUS_RESTARTING
            state.note(
                f"连续 {state.consecutive_failures} 次探活失败（{detail}），准备重启"
                f"（{backoff}s 退避）",
                "error",
            )
            if unit is not None:
                unit.restarts += 1
                state.restarts = unit.restarts
            async with self._lock:
                await self._stop_unit(unit, config, [channel])
                if unit is not None:
                    unit.process = None
                await self._launch(channel, config, reason="掉线自动重启")

    # ------------------------------------------------------------------ 同步观察
    def forget(self, channel_id: str) -> None:
        self._states.pop(channel_id, None)


# --------------------------------------------------------------------------- #
def _listener_pids(port: int) -> list[tuple[int, str]]:
    """找出监听指定端口的进程（Windows 用 netstat，POSIX 依次尝试 lsof/fuser/ss）。"""
    results: list[tuple[int, str]] = []
    try:
        if IS_WINDOWS:
            completed = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20)
            for raw in completed.stdout.decode("utf-8", "replace").splitlines():
                parts = raw.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                    if parts[1].endswith(f":{port}"):
                        try:
                            results.append((int(parts[4]), "pid"))
                        except ValueError:
                            continue
        else:
            for command in (
                ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                ["fuser", "-n", "tcp", str(port)],
            ):
                try:
                    completed = subprocess.run(command, capture_output=True, timeout=20)
                except FileNotFoundError:
                    continue
                if completed.returncode == 0:
                    for token in completed.stdout.decode().split():
                        digits = re.sub(r"\D", "", token)
                        if digits:
                            results.append((int(digits), "pid"))
                    if results:
                        break
    except Exception as exc:  # noqa: BLE001
        log.debug("查询端口 %s 的监听进程失败：%s", port, exc)
    # 去重并补上进程名，便于日志里看清楚杀了谁
    unique: dict[int, str] = {}
    for pid, name in results:
        unique.setdefault(pid, name)
    return [(pid, _process_name(pid)) for pid in unique]


def _process_name(pid: int) -> str:
    try:
        if IS_WINDOWS:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, timeout=15,
            )
            text = completed.stdout.decode("utf-8", "replace").strip()
            if text and "," in text:
                return text.split(",")[0].strip('"')
        else:
            return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001
        pass
    return "process"


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part for part in shlex.split(value)] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]
