#!/usr/bin/env bash
set -euo pipefail

BOT_DIR="${BOT_DIR:-/home/dangdang971216/trading_bot}"
SERVICE_NAME="${SERVICE_NAME:-tradingbot-guard}"
cd "$BOT_DIR"

if [ ! -f guard.env ]; then
  echo "guard.env 파일이 없습니다. 먼저 아래 형태로 만들어주세요:"
  echo "GUARD_BOT_TOKEN=토큰"
  echo "GUARD_CHAT_ID=챗아이디"
  echo "MAIN_SERVICE=tradingbot"
  echo "BOT_DIR=/home/dangdang971216/trading_bot"
  echo "GIT_BRANCH=main"
  exit 1
fi

if [ ! -f backga_guard_bot.py ]; then
  echo "backga_guard_bot.py 파일이 없습니다."
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
if [ -f "backga_guard_bot.py" ]; then
  cp -f backga_guard_bot.py "backga_guard_bot.py.install_backup_${TS}" || true
fi

chmod 600 guard.env
chmod +x backga_guard_bot.py
python3 -m py_compile backga_guard_bot.py

if [ -f tradingbot-guard.service ]; then
  sudo cp tradingbot-guard.service "/etc/systemd/system/${SERVICE_NAME}.service"
fi

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"
sleep 2
sudo systemctl status "${SERVICE_NAME}" --no-pager -l

echo ""
echo "설치 완료. 텔레그램 가드봇에서 /guard, /gdeploy, /gupgrade 를 확인하세요."
