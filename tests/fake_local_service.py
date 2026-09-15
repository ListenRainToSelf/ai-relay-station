"""测试用的「本地推理服务」仿真。

提供两样东西：
  * `write_service(tmp_path, ports)` —— 生成一个极简的 OpenAI 兼容服务脚本，
    以及一个「拉起它然后立刻退出」的启动脚本（模拟 start.bat / start-server.bat）；
  * `kill_port(port)` —— 清理监听端口的进程，保证测试不残留后台进程。

刻意做成「启动脚本会立刻退出」的形态，因为这才是真实的本地推理场景：
脚本 detach 出服务进程后自己结束，所以托管逻辑必须以探活为事实来源。
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SERVICE_SOURCE = '''"""测试用假推理服务：只暴露 /v1/models 与 /health，用于探活。"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/v1/models") or self.path == "/health":
            body = json.dumps(
                {"object": "list", "data": [{"id": "fake-local", "object": "model"}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''

# 会立刻退出的启动脚本：拉两个后台服务后马上结束（start.bat 的典型形态）
BAT_LAUNCHER = """@echo off
cd /d "%~dp0"
start "" /b "{python}" service.py {port_a}
start "" /b "{python}" service.py {port_b}
exit /b 0
"""

SH_LAUNCHER = """#!/bin/sh
cd "$(dirname "$0")"
nohup "{python}" service.py {port_a} >/dev/null 2>&1 &
nohup "{python}" service.py {port_b} >/dev/null 2>&1 &
exit 0
"""


@dataclass
class FakeService:
    workdir: Path
    service_script: Path
    launcher: Path
    port_a: int
    port_b: int
    python: str

    def direct_command(self, port: int) -> str:
        """前台常驻型命令（对应 start.bat → python serve.py 那种形态）。"""
        return f'"{self.python}" "{self.service_script}" {port}'

    def launcher_command(self) -> str:
        return str(self.launcher)

    def base_url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}/v1"

    def lifecycle(self, port: int, **overrides) -> dict:
        config = {
            "enabled": True,
            "command": self.direct_command(port),
            "workdir": str(self.workdir),
            "health_path": "/v1/models",
            "startup_grace_seconds": 0,
            "check_interval_seconds": 5,
            "failure_threshold": 1,
            "restart_backoff_seconds": 5,
            "auto_start": False,
            "auto_restart": True,
            "stop_on_shutdown": False,
            "stop_strategy": "auto",
        }
        config.update(overrides)
        return config


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def write_service(base: Path) -> FakeService:
    base.mkdir(parents=True, exist_ok=True)
    service_script = base / "service.py"
    service_script.write_text(SERVICE_SOURCE, encoding="utf-8")
    port_a, port_b = free_port(), free_port()
    python = sys.executable
    if sys.platform == "win32":
        launcher = base / "start.bat"
        launcher.write_text(
            BAT_LAUNCHER.format(python=python, port_a=port_a, port_b=port_b), encoding="utf-8"
        )
    else:
        launcher = base / "start.sh"
        launcher.write_text(
            SH_LAUNCHER.format(python=python, port_a=port_a, port_b=port_b), encoding="utf-8"
        )
        launcher.chmod(0o755)
    return FakeService(base, service_script, launcher, port_a, port_b, python)


def wait_until(predicate, timeout: float = 20.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def port_is_serving(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def kill_port(port: int) -> None:
    """清理：杀掉监听该端口的进程（测试收尾用，避免残留后台进程）。"""
    try:
        if sys.platform == "win32":
            completed = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20)
            for raw in completed.stdout.decode("utf-8", "replace").splitlines():
                parts = raw.split()
                if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
                    subprocess.run(["taskkill", "/PID", parts[4], "/T", "/F"], capture_output=True, timeout=20)
        else:
            for command in (["lsof", "-ti", f"tcp:{port}"], ["fuser", "-k", f"{port}/tcp"]):
                try:
                    completed = subprocess.run(command, capture_output=True, timeout=20)
                except FileNotFoundError:
                    continue
                if completed.returncode == 0:
                    break
    except Exception:  # noqa: BLE001
        pass


def pids_on_port(port: int) -> list[int]:
    found: list[int] = []
    try:
        completed = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20)
        for raw in completed.stdout.decode("utf-8", "replace").splitlines():
            parts = raw.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(f":{port}"):
                if parts[4].isdigit():
                    found.append(int(parts[4]))
    except Exception:  # noqa: BLE001
        pass
    return found


def kill_pid(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        pass
