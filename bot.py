import os
import time
import json
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pybithumb
from telegram import Bot
from telegram.ext import Updater, CommandHandler, CallbackContext

# =========================
# 버전
# =========================
BOT_VERSION = "수익률 우선 풀공격형 v2.0"

# =========================
# 환경변수
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY or not API_SECRET:
    raise ValueError("환경변수(API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID) 중 비어 있는 값이 있어요.")

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

# =========================
# 기본 설정
# =========================
TIMEZONE = "Asia/Seoul"
LOG_FILE = "trade_log.json"

SCAN_INTERVAL = 15
POSITION_CHECK_INTERVAL = 5
LOOP_SLEEP = 1

TOP_TICKERS = 100
MIN_ORDER_KRW = 5000

# 고정 진입금 대신 '최대 사용 가능 금액' 개념으로 사용
MAX_ENTRY_KRW = 11000
KRW_USE_RATIO = 0.95  # 잔고의 95%만 사용
AUTO_BUY = True
AUTO_BUY_START_HOUR = 7
AUTO_BUY_END_HOUR = 23
MAX_HOLDINGS = 1

# =========================
# BTC 필터
# =========================
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -1.5
BTC_STRONG_BLOCK_PCT = -2.2
BTC_MA_FILTER = True

# =========================
# 수익률 중심 리스크 관리
# =========================
BASE_STOP_LOSS_PCT = -1.8
BASE_TP_PCT = 3.4
TRAIL_START_PCT = 2.4
TRAIL_BACKOFF_PCT = 0.95
BREAKEVEN_TRIGGER_PCT = 1.4
BREAKEVEN_BUFFER_PCT = 0.20

# 시간손절
TIME_STOP_MIN_SEC = 300
TIME_STOP_MAX_SEC = 1500

# 기대값 필터
MIN_EXPECTED_EDGE_SCORE = 5.8
MIN_EXPECTED_TP_PCT = 2.2

# =========================
# EARLY (초기)
# =========================
EARLY_MIN_CHANGE_PCT = 0.25
EARLY_MAX_CHANGE_PCT = 2.3
EARLY_MIN_VOL_RATIO = 1.20
EARLY_MIN_RSI = 42
EARLY_MAX_RSI = 66
EARLY_MIN_RANGE_PCT = 0.50
EARLY_AUTO_BUY_MIN_SCORE = 5

# =========================
# PREPUMP (급등 직전)
# =========================
PREPUMP_MIN_CHANGE_PCT = 0.55
PREPUMP_MAX_CHANGE_PCT = 4.8
PREPUMP_MIN_VOL_RATIO = 1.45
PREPUMP_MIN_RSI = 45
PREPUMP_MAX_RSI = 72
PREPUMP_MIN_RANGE_PCT = 0.85

# =========================
# PULLBACK (급등 후 눌림)
# =========================
PULLBACK_MIN_TOTAL_PUMP_PCT = 5.0
PULLBACK_REBOUND_MIN_PCT = 0.55
PULLBACK_MIN_VOL_RATIO = 1.05

# =========================
# 리포트
# =========================
BTC_REPORT_INTERVAL = 3600
STATUS_REPORT_INTERVAL = 1800
EARLY_REPORT_INTERVAL = 300

last_btc_report_time = 0
last_status_report_time = 0
last_early_report_time = 0

# =========================
# 상태 저장
# =========================
active_positions = {}
recent_signal_alerts = {}
recent_early_alerts = {}

# =========================
# 유틸
# =========================
def now_kst():
    return datetime.now(ZoneInfo(TIMEZONE))

def is_auto_time():
    now = now_kst()
    return AUTO_BUY_START_HOUR <= now.hour < AUTO_BUY_END_HOUR

def r(x, n=4):
    try:
        return round(float(x), n)
    except Exception:
        return 0.0

def fmt_price(x):
    try:
        x = float(x)
    except Exception:
        return "0"

    if x >= 1000:
        return f"{x:,.0f}"
    elif x >= 100:
        return f"{x:,.1f}"
    elif x >= 1:
        return f"{x:,.2f}"
    elif x >= 0.1:
        return f"{x:,.3f}"
    elif x >= 0.01:
        return f"{x:,.4f}"
    else:
        return f"{x:,.6f}"

def fmt_pct(x):
    try:
        return f"{float(x):.2f}%"
    except Exception:
        return "0.00%"

def send(msg: str):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip())
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

# =========================
# 시작 알림
# =========================
def send_startup_message():
    send(
        f"""
✅ 봇 시작 완료

버전: {BOT_VERSION}
자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}
운영시간: {AUTO_BUY_START_HOUR}:00 ~ {AUTO_BUY_END_HOUR}:00
운영 방식: 수익률 우선 풀공격형

이 버전으로 시장 감시를 시작할게.
"""
    )

# =========================
# 로그
# =========================
def load_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
