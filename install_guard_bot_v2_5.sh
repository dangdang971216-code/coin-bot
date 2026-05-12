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
  echo "PAPER_SERVICE=tradingbot-paper   # 없으면 pid 방식으로 재시작 시도"
  echo "BOT_DIR=/home/dangdang971216/trading_bot"
  echo "GIT_BRANCH=main"
  exit 1
fi

# v2.5 파일을 실제 실행 파일명으로 정규화
if [ -f backga_guard_bot_v2_5.py ]; then
  cp -f backga_guard_bot_v2_5.py backga_guard_bot.py
elif [ -f backga_guard_bot_v2.5.py ]; then
  cp -f backga_guard_bot_v2.5.py backga_guard_bot.py
elif [ -f backga_guard_bot_v2_4.py ]; then
  echo "주의: v2.5 파일이 없어 v2.4를 사용합니다. v2.5 파일을 먼저 올려주세요."
  cp -f backga_guard_bot_v2_4.py backga_guard_bot.py
fi

if [ ! -f backga_guard_bot.py ]; then
  echo "backga_guard_bot.py 파일이 없습니다. backga_guard_bot_v2_5.py 또는 backga_guard_bot.py를 올려주세요."
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
cp -f backga_guard_bot.py "backga_guard_bot.py.install_backup_${TS}" || true
[ -f tradingbot-guard.service ] && cp -f tradingbot-guard.service "tradingbot-guard.service.install_backup_${TS}" || true

chmod 600 guard.env
chmod +x backga_guard_bot.py
python3 -m py_compile backga_guard_bot.py

if [ -f tradingbot-guard_v2_5.service ]; then
  cp -f tradingbot-guard_v2_5.service tradingbot-guard.service
elif [ -f tradingbot-guard_v2_4.service ]; then
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
echo "v2.5부터 /gupgrade는 main + paper_bot 동시 확인이고, /gguard_upgrade는 가드봇 자체 업그레이드입니다."
