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

# v2.4 파일을 실제 실행 파일명으로 정규화
if [ -f backga_guard_bot_v2_4.py ]; then
  cp -f backga_guard_bot_v2_4.py backga_guard_bot.py
elif [ -f backga_guard_bot-3.py ]; then
  cp -f backga_guard_bot-3.py backga_guard_bot.py
elif [ -f backga_guard_bot-2.py ]; then
  cp -f backga_guard_bot-2.py backga_guard_bot.py
elif [ -f backga_guard_bot-1.py ]; then
  cp -f backga_guard_bot-1.py backga_guard_bot.py
fi

if [ ! -f backga_guard_bot.py ]; then
  echo "backga_guard_bot.py 파일이 없습니다. backga_guard_bot_v2_4.py 또는 backga_guard_bot.py를 올려주세요."
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
cp -f backga_guard_bot.py "backga_guard_bot.py.install_backup_${TS}" || true
[ -f tradingbot-guard.service ] && cp -f tradingbot-guard.service "tradingbot-guard.service.install_backup_${TS}" || true

chmod 600 guard.env
chmod +x backga_guard_bot.py
python3 -m py_compile backga_guard_bot.py

if [ -f tradingbot-guard_v2_4.service ]; then
  cp -f tradingbot-guard_v2_4.service tradingbot-guard.service
fi

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
