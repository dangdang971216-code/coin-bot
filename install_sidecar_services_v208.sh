#!/usr/bin/env bash
set -euo pipefail
BOT_DIR="${BOT_DIR:-/home/dangdang971216/trading_bot}"
BOT_USER="${BOT_USER:-$(stat -c '%U' "$BOT_DIR" 2>/dev/null || echo dangdang971216)}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.10}"
ENV_FILE="$BOT_DIR/guard.env"
HOME_DIR="/home/$BOT_USER"

write_unit() {
  local service="$1"
  local desc="$2"
  local active_file="$3"
  local unit="/etc/systemd/system/${service}.service"
  cat > "$unit" <<UNIT
[Unit]
Description=${desc}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${BOT_USER}
WorkingDirectory=${BOT_DIR}
EnvironmentFile=-${ENV_FILE}
Environment=TRADING_BOT_DIR=${BOT_DIR}
Environment=HOME=${HOME_DIR}
Environment=PYTHONPATH=${HOME_DIR}/.local/lib/python3.10/site-packages:${HOME_DIR}/.local/lib/python3/site-packages
ExecStart=${PYTHON_BIN} ${BOT_DIR}/${active_file}
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
UNIT
  chmod 0644 "$unit"
}

write_unit "tradingbot-ws-sidecar" "TradingBot websocket sidecar" "ws_sidecar.py"
write_unit "tradingbot-micro-sidecar" "TradingBot Bithumb micro sidecar" "bithumb_micro_sidecar.py"
systemctl daemon-reload
systemctl enable tradingbot-ws-sidecar tradingbot-micro-sidecar
# 기존 guard direct-fallback 잔류 프로세스는 서비스 시작 전에 정리
pkill -f "${BOT_DIR}/ws_sidecar.py" 2>/dev/null || true
pkill -f "${BOT_DIR}/bithumb_micro_sidecar.py" 2>/dev/null || true
systemctl restart tradingbot-ws-sidecar tradingbot-micro-sidecar
systemctl status tradingbot-ws-sidecar --no-pager -l || true
systemctl status tradingbot-micro-sidecar --no-pager -l || true
