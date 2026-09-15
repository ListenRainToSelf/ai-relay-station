#!/usr/bin/env bash
# 本地 AI 中转站 · Linux / NAS 一键安装脚本
#
#   sudo ./scripts/install-linux.sh                        # 默认装到 /opt/airelay，数据在 /var/lib/airelay
#   sudo ./scripts/install-linux.sh --port 8090 --host 0.0.0.0
#   sudo ./scripts/install-linux.sh --no-systemd           # 只装依赖，不起 systemd（NAS 无 systemd 时用）
#
# 装完会打印控制台地址与管理员令牌。卸载：sudo ./scripts/install-linux.sh --uninstall

set -euo pipefail

APP_DIR="/opt/airelay"
DATA_DIR="/var/lib/airelay"
SERVICE_NAME="airelay"
RUN_USER="airelay"
HOST="0.0.0.0"
PORT="8000"
USE_SYSTEMD=1
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-dir) APP_DIR="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-systemd) USE_SYSTEMD=0; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 1 ;;
  esac
done

log()  { printf '\033[38;5;39m[安装]\033[0m %s\n' "$*"; }
warn() { printf '\033[38;5;214m[注意]\033[0m %s\n' "$*"; }
die()  { printf '\033[38;5;203m[失败]\033[0m %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" == "0" ]] || die "请用 root（或 sudo）运行"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------- 卸载
if [[ "$UNINSTALL" == "1" ]]; then
  log "停止并移除服务"
  systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload 2>/dev/null || true
  warn "代码目录 ${APP_DIR} 与数据目录 ${DATA_DIR} 已保留（内含密钥与统计，请自行确认后删除）"
  exit 0
fi

# ---------------------------------------------------------------- 依赖
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    if [[ "$(printf '%s\n3.11\n' "$version" | sort -V | head -1)" == "3.11" ]]; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  fi
done
[[ -n "$PYTHON_BIN" ]] || die "需要 Python 3.11+，请先安装（Debian/Ubuntu: apt install python3 python3-venv；群晖/威联通请用 Container Manager 走 Docker 方式）"
log "使用解释器：$PYTHON_BIN（$("$PYTHON_BIN" --version)）"

# ---------------------------------------------------------------- 用户与目录
if ! id -u "$RUN_USER" >/dev/null 2>&1; then
  log "创建运行用户 $RUN_USER"
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
    || adduser --system --home "$DATA_DIR" --no-create-home --shell /sbin/nologin "$RUN_USER"
fi

log "同步代码到 $APP_DIR"
mkdir -p "$APP_DIR"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete \
    --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'devdata*' --exclude 'testdata*' --exclude '.venv' \
    "$SRC_DIR/" "$APP_DIR/"
else
  cp -r "$SRC_DIR/." "$APP_DIR/"
  rm -rf "$APP_DIR/.git" "$APP_DIR/.venv" "$APP_DIR/devdata" "$APP_DIR/testdata"
fi

log "创建数据目录 $DATA_DIR"
mkdir -p "$DATA_DIR"
chown -R "$RUN_USER:$RUN_USER" "$DATA_DIR" "$APP_DIR"
chmod 750 "$DATA_DIR"

# ---------------------------------------------------------------- 虚拟环境
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  log "创建虚拟环境"
  "$PYTHON_BIN" -m venv "$APP_DIR/venv"
fi
log "安装依赖"
"$APP_DIR/venv/bin/python" -m pip install --upgrade pip --quiet
"$APP_DIR/venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt" --quiet
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR/venv"

# ---------------------------------------------------------------- 自检
log "运行环境自检"
sudo -u "$RUN_USER" AIRELAY_DATA_DIR="$DATA_DIR" \
  "$APP_DIR/venv/bin/python" -m airelay --data-dir "$DATA_DIR" --doctor || warn "自检有未通过项，请查看上面的输出"

# ---------------------------------------------------------------- systemd
if [[ "$USE_SYSTEMD" == "1" ]] && command -v systemctl >/dev/null 2>&1; then
  log "写入 systemd 单元"
  sed -e "s#^User=.*#User=${RUN_USER}#" \
      -e "s#^Group=.*#Group=${RUN_USER}#" \
      -e "s#^WorkingDirectory=.*#WorkingDirectory=${APP_DIR}#" \
      -e "s#^ExecStart=.*#ExecStart=${APP_DIR}/venv/bin/python -m airelay --mode server#" \
      -e "s#^Environment=AIRELAY_HOST=.*#Environment=AIRELAY_HOST=${HOST}#" \
      -e "s#^Environment=AIRELAY_PORT=.*#Environment=AIRELAY_PORT=${PORT}#" \
      -e "s#^Environment=AIRELAY_DATA_DIR=.*#Environment=AIRELAY_DATA_DIR=${DATA_DIR}#" \
      -e "s#^ReadWritePaths=.*#ReadWritePaths=${DATA_DIR}#" \
      "$APP_DIR/scripts/airelay.service" > "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}.service"
  sleep 2
  systemctl --no-pager --lines=12 status "${SERVICE_NAME}.service" || true
else
  warn "未启用 systemd。可手动后台启动："
  echo "    sudo -u ${RUN_USER} AIRELAY_DATA_DIR=${DATA_DIR} ${APP_DIR}/venv/bin/python -m airelay --mode server --host ${HOST} --port ${PORT} &"
fi

# ---------------------------------------------------------------- 收尾提示
TOKEN="$(sudo -u "$RUN_USER" AIRELAY_DATA_DIR="$DATA_DIR" "$APP_DIR/venv/bin/python" -m airelay --data-dir "$DATA_DIR" --print-token 2>/dev/null || true)"
IP_GUESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

────────────────────────────────────────────────────────────
 安装完成
────────────────────────────────────────────────────────────
 控制台         http://${IP_GUESS:-<NAS地址>}:${PORT}/admin
 OpenAI 基地址  http://${IP_GUESS:-<NAS地址>}:${PORT}/v1
 管理员令牌     ${TOKEN:-<见 /var/lib/airelay/secrets.json>}
 数据目录       ${DATA_DIR}
 代码目录       ${APP_DIR}
 日志           journalctl -u ${SERVICE_NAME} -f
────────────────────────────────────────────────────────────
 第一次用：打开控制台 → 渠道 → 新建渠道（填上游 base_url 与 API Key）
           → 密钥 → 创建密钥，把明文填进客户端即可。
 端口/监听地址也可以在控制台「设置 → 网络」里改，进程内会热重绑定。
────────────────────────────────────────────────────────────
EOF
