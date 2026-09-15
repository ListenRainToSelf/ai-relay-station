"""命令行入口。

    python -m airelay                     # 自动判断：桌面（Windows）或无头服务
    python -m airelay --mode server       # 无头常驻（Linux NAS / 容器 / systemd）
    python -m airelay --host 0.0.0.0      # 允许局域网调用
    python -m airelay --port 8090
    python -m airelay --data-dir /volume1/airelay
    python -m airelay --print-token       # 查看管理员令牌
    python -m airelay --doctor            # 环境自检
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
from pathlib import Path

from .context import MODE_DESKTOP, MODE_SERVER, AppContext
from .db import build_engine, build_session_factory, dispose, init_db
from .desktop import run_desktop, tray_available
from .desktop.window import find_chromium
from .host import run_blocking
from .logging_setup import setup_logging
from .paths import AppPaths
from .security import load_or_create_secrets
from .settings import SETTING_SPECS, SettingsService
from .version import APP_NAME, __version__

log = logging.getLogger("airelay.cli")

ENV_HOST = "AIRELAY_HOST"
ENV_PORT = "AIRELAY_PORT"
ENV_ADMIN_TOKEN = "AIRELAY_ADMIN_TOKEN"
ENV_MODE = "AIRELAY_MODE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airelay",
        description=f"{APP_NAME} —— 本机/局域网自建的 AI API 聚合中转网关",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--mode",
        choices=("auto", MODE_DESKTOP, MODE_SERVER),
        default=os.environ.get(ENV_MODE) or "auto",
        help="运行形态：auto 自动判断（Windows 走桌面，其它走无头服务）",
    )
    parser.add_argument("--host", default=os.environ.get(ENV_HOST) or "", help="监听地址，如 0.0.0.0")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get(ENV_PORT) or 0) or None, help="监听端口，默认 8000"
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="数据目录（数据库/日志/机密文件），默认见 README",
    )
    parser.add_argument("--log-level", default="", choices=("", "DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="临时覆盖设置项（不写库），可重复，如 --set gateway.max_retries=2",
    )
    parser.add_argument("--no-window", action="store_true", help="桌面模式下不自动打开控制台窗口")
    parser.add_argument("--no-tray", action="store_true", help="桌面模式下不启用托盘（前台常驻）")
    parser.add_argument("--print-token", action="store_true", help="打印管理员令牌后退出")
    parser.add_argument("--rotate-token", action="store_true", help="轮换管理员令牌后启动")
    parser.add_argument("--print-config", action="store_true", help="打印设置项清单（JSON）后退出")
    parser.add_argument("--doctor", action="store_true", help="做一次环境自检后退出")
    parser.add_argument("--version", action="store_true", help="打印版本号后退出")
    return parser


def _parse_overrides(items: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--set 参数格式应为 KEY=VALUE，收到：{item}")
        key, _, value = item.partition("=")
        key = key.strip()
        if key not in SETTING_SPECS:
            raise SystemExit(f"未知设置项：{key}")
        values[key] = value.strip()
    return values


def preload_settings(paths: AppPaths, settings: SettingsService) -> None:
    """在绑定监听之前把库里的设置读进内存。

    监听地址与端口本身也是设置项，而 uvicorn 的 Config 要在应用生命周期开始之前
    就拿到 host/port——所以必须先同步地把设置读出来，否则「在控制台改完端口，
    下次不带参数启动」会悄悄退回默认端口。
    """
    async def load() -> None:
        engine = build_engine(paths.db_path)
        try:
            # 首次启动时库还不存在，先确保表结构就绪
            await init_db(engine)
            factory = build_session_factory(engine)
            async with factory() as session:
                await settings.load(session)
        finally:
            await dispose(engine)

    asyncio.run(load())


def resolve_mode(requested: str) -> str:
    """auto 模式：有图形能力就走桌面（托盘或独立窗口），否则无头服务。"""
    if requested in (MODE_DESKTOP, MODE_SERVER):
        return requested
    if sys.platform in ("win32", "darwin"):
        # Windows/macOS 上即便装不了托盘，只要还能开浏览器窗口就仍按桌面跑
        return MODE_DESKTOP if (tray_available() or find_chromium()) else MODE_SERVER
    return MODE_DESKTOP if tray_available() else MODE_SERVER


def print_banner(ctx: AppContext, *, first_run: bool) -> None:
    console = f"{ctx.base_url()}/admin"
    lines = [
        "",
        "  " + "─" * 62,
        f"  {APP_NAME}  v{__version__}   ·   {ctx.mode} 模式",
        "  " + "─" * 62,
        f"  控制台        {console}",
        f"  OpenAI 基地址 {ctx.openai_base_url()}",
        f"  数据目录      {ctx.paths.data_dir}",
        "  " + "─" * 62,
        f"  管理员令牌    {ctx.admin_token}",
        "",
        "  在别的机器上打开控制台时，用上面的令牌登录即可。",
        "  首次使用：控制台 → 渠道 → 新建渠道（填上游 base_url 与 API Key），",
        "            再到「密钥」页创建一个本地密钥，就能给客户端用了。",
        "  " + "─" * 62,
        "",
    ]
    if first_run:
        lines.insert(1, "  （首次启动，已生成数据目录与机密文件）")
    text = "\n".join(lines)
    if getattr(sys, "stdout", None) is not None:
        print(text, flush=True)
    else:
        # pythonw / 静默托盘：没有控制台可打印，把同样内容写进日志，方便回看
        for line in lines:
            if line.strip():
                log.info(line.strip())


def run_doctor(paths: AppPaths, settings: SettingsService) -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # 数据目录可写
    try:
        paths.data_dir.mkdir(parents=True, exist_ok=True)
        probe = paths.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("数据目录可写", True, str(paths.data_dir))
    except OSError as exc:
        check("数据目录可写", False, str(exc))

    # 端口占用
    host = settings.get_str("network.host", "127.0.0.1")
    port = settings.get_int("network.port", 8000)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
        check(f"监听端口 {host}:{port} 可用", True)
    except OSError as exc:
        check(f"监听端口 {host}:{port} 可用", False, str(exc))

    # 可选依赖
    try:
        import httpx

        check("httpx 可用", True, httpx.__version__)
    except Exception as exc:  # pragma: no cover
        check("httpx 可用", False, str(exc))
    try:
        import aiosqlite

        check("aiosqlite 可用", True, getattr(aiosqlite, "__version__", ""))
    except Exception as exc:  # pragma: no cover
        check("aiosqlite 可用", False, str(exc))

    desktop_ok = tray_available()
    check("图形托盘可用", desktop_ok, "Windows/macOS 或带 DISPLAY 的 Linux")
    try:
        from .desktop.window import find_chromium, pywebview_available

        check("内嵌窗口组件", pywebview_available() or bool(find_chromium()), "pywebview 或 Chromium 系浏览器")
    except Exception as exc:  # pragma: no cover
        check("内嵌窗口组件", False, str(exc))

    width = max(len(name) for name, _, _ in checks)
    print(f"\n{APP_NAME} 环境自检\n" + "-" * (width + 30))
    failed = 0
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        if not ok:
            failed += 1
        suffix = f"  {detail}" if detail else ""
        print(f"  {mark} {name.ljust(width)}{suffix}")
    print("-" * (width + 30))
    print(f"  通过 {len(checks) - failed}/{len(checks)}\n")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if args.print_config:
        print(json.dumps(SettingsService.describe(), ensure_ascii=False, indent=2))
        return 0

    paths = AppPaths.build(args.data_dir or None)
    secrets = load_or_create_secrets(paths.data_dir, rotate_admin_token=args.rotate_token)
    if args.print_token:
        print(secrets.admin_token)
        return 0

    settings = SettingsService()
    # 先把库里的设置读出来（监听地址就藏在其中），再叠加命令行覆盖
    preload_settings(paths, settings)
    overrides: dict[str, object] = {}
    if args.host:
        overrides["network.host"] = args.host
    if args.port:
        overrides["network.port"] = args.port
    if args.log_level:
        overrides["logs.level"] = args.log_level
    overrides.update(_parse_overrides(args.set))
    if overrides:
        settings.set_overrides(overrides)

    if args.doctor:
        return run_doctor(paths, settings)

    mode = resolve_mode(args.mode)
    ctx = AppContext(paths=paths, settings=settings, secrets=secrets, mode=mode)
    setup_logging(settings.get_str("logs.level", "INFO"), paths.log_dir)

    # 环境变量里的管理员令牌优先级最高（容器/服务化部署常用）
    env_token = os.environ.get(ENV_ADMIN_TOKEN)
    if env_token:
        from .security import Secrets

        ctx.secrets = Secrets(
            pepper=secrets.pepper,
            master_key=secrets.master_key,
            admin_token=env_token,
            session_secret=secrets.session_secret,
        )

    first_run = not (paths.db_path.exists())
    print_banner(ctx, first_run=first_run)

    if mode == MODE_DESKTOP:
        return run_desktop(ctx, open_window=not args.no_window, use_tray=not args.no_tray)
    return run_blocking(ctx, host=args.host or None, port=args.port or None)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
