#!/usr/bin/env bash
set -euo pipefail

BOT_DIR="/home/dangdang971216/trading_bot"
SERVICE_NAME="tradingbot-paper"
UNIT_SRC="$BOT_DIR/tradingbot-paper.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"
PAPER_PID="$BOT_DIR/paper_bot.pid"

cd "$BOT_DIR"

echo "===== check files ====="
ls -lh paper_bot.py guard.env tradingbot-paper.service

echo "===== stop old direct paper_bot only ====="
if [[ -f "$PAPER_PID" ]]; then
  OLD_PID="$(cat "$PAPER_PID" 2>/dev/null || true)"
  if [[ -n "${OLD_PID:-}" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "stop pid file paper_bot pid=$OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
fi
pkill -f "${BOT_DIR}/paper_bot.py --bot" 2>/dev/null || true
sleep 1

echo "===== install systemd unit ====="
sudo cp "$UNIT_SRC" "$UNIT_DST"
sudo chmod 644 "$UNIT_DST"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"
sleep 4

echo "===== service status ====="
systemctl is-active "$SERVICE_NAME" || true
systemctl status "$SERVICE_NAME" --no-pager -l | tail -80 || true

echo "===== paper status file ====="
cat paper_bot_status.json 2>/dev/null | head -80 || true

echo "===== next telegram checks ====="
echo "/gpaper_service"
echo "/gpaper_state"
echo "/gpaperlog 120"
echo "/pstatus"
echo "/perror"
