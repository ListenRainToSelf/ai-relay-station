#!/usr/bin/env sh
# ============================================================================
#  本地 AI 中转站 · Linux / macOS 启动脚本
#
#  用法：
#    ./start.sh                  # 有图形界面就走托盘，否则无头服务
#    ./start.sh --mode server    # 强制无头（NAS / 服务器）
#    ./start.sh --host 0.0.0.0 --port 8000
#
#  Linux 上想用托盘需要图形会话（DISPLAY/WAYLAND_DISPLAY）与 pystray 依赖，
#  NAS 场景通常直接走 server 模式，或用 scripts/install-linux.sh 装成 systemd 服务。
# ============================================================================
set -e
cd "$(dirname "$0")"

PY=""
if [ -x ".venv/bin/python" ]; then
  PY="$(pwd)/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
fi

if [ -z "$PY" ]; then
  echo "  [错误] 没有找到 python3，请先安装 Python 3.10+" >&2
  exit 1
fi

if ! "$PY" -c "import fastapi, uvicorn, httpx, sqlalchemy, cryptography, pydantic, websockets" >/dev/null 2>&1; then
  echo "  [提示] 依赖不完整，正在安装 requirements.txt ..."
  "$PY" -m pip install -r requirements.txt
fi

echo "  正在启动…（数据目录默认为 ~/.local/share/airelay，日志在 logs/airelay.log）"
exec "$PY" -m airelay "$@"
