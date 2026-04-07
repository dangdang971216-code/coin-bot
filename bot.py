import os
import time
import json
import math
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pybithumb
from telegram import Bot
from telegram.ext import Updater, CommandHandler, CallbackContext

# =========================
# 버전
# =========================
BOT_VERSION = "공격형 선별매수 v5.1"

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
# 파일
# =========================
TIMEZONE = "Asia/Seoul"
LOG_FILE = "trade_log.json"
POSITIONS_FILE = "active_positions.json"

# =========================
# 기본 설정
# =========================
SCAN_INTERVAL = 8
POSITION_CHECK_INTERVAL = 4
LOOP_SLEEP = 1

TOP_TICKERS = 150
MIN_ORDER_KRW = 5000
DUST_KEEP_MIN_KRW = 100

MAX_ENTRY_KRW = 10000
KRW_USE_RATIO = 0.88
ORDER_BUFFER_KRW = 500

MARKET_BUY_SLIPPAGE = 1.05
RETRY_KRW_LEVELS = [1.00, 0.94, 0.88]

AUTO_BUY = True
AUTO_BUY_START_HOUR = 0
AUTO_BUY_END_HOUR = 24
MAX_HOLDINGS = 1

# =========================
# BTC 필터
# =========================
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -2.0
BTC_STRONG_BLOCK_PCT = -2.8
BTC_MA_FILTER = True

# =========================
# 리스크 관리
# =========================
BASE_STOP_LOSS_PCT = -1.75
BASE_TP_PCT = 4.8

TRAIL_START_PCT = 2.8
TRAIL_BACKOFF_PCT = 1.15

BREAKEVEN_TRIGGER_PCT = 1.7
BREAKEVEN_BUFFER_PCT = 0.15

TIME_STOP_MIN_SEC = 480
TIME_STOP_MAX_SEC = 1080

MIN_EXPECTED_EDGE_SCORE = 7.2
MIN_EXPECTED_TP_PCT = 3.5

# =========================
# 초기 상승형 (알림용)
# =========================
EARLY_MIN_CHANGE_PCT = 1.0
EARLY_MAX_CHANGE_PCT = 2.8
EARLY_MIN_VOL_RATIO = 2.4
EARLY_MIN_RSI = 44
EARLY_MAX_RSI = 65
EARLY_MIN_RANGE_PCT = 0.9

# =========================
# 상승 시작형 (알림용)
# =========================
PREPUMP_MIN_CHANGE_PCT = 1.0
PREPUMP_MAX_CHANGE_PCT = 4.2
PREPUMP_MIN_VOL_RATIO = 2.0
PREPUMP_MIN_RSI = 48
PREPUMP_MAX_RSI = 70
PREPUMP_MIN_RANGE_PCT = 1.1

# =========================
# 눌림 반등형 (알림용)
# =========================
PULLBACK_MIN_TOTAL_PUMP_PCT = 6.0
PULLBACK_REBOUND_MIN_PCT = 0.95
PULLBACK_MIN_VOL_RATIO = 1.6

# =========================
# 쏘기 직전형 (자동매수 허용)
# =========================
PRE_BREAKOUT_MAX_CHANGE_PCT = 1.05
PRE_BREAKOUT_MIN_VOL_RATIO = 2.6
PRE_BREAKOUT_MIN_RSI = 48
PRE_BREAKOUT_MAX_RSI = 66
PRE_BREAKOUT_MIN_RANGE_PCT = 0.9
PRE_BREAKOUT_MAX_GAP_PCT = 0.32

# =========================
# 급등 돌파형 (자동매수 허용)
# =========================
BREAKOUT_MIN_CHANGE_PCT = 0.80
BREAKOUT_MAX_CHANGE_PCT = 3.8
BREAKOUT_MIN_VOL_RATIO = 3.0
BREAKOUT_MIN_RSI = 52
BREAKOUT_MAX_RSI = 74
BREAKOUT_MIN_RANGE_PCT = 1.0
BREAKOUT_BREAK_PCT = 0.10

# =========================
# 추격형 (자동매수 허용, 강화)
# =========================
CHASE_MIN_CHANGE_PCT = 1.8
CHASE_MAX_CHANGE_PCT = 3.8
CHASE_MIN_VOL_RATIO = 4.0
CHASE_MIN_RSI = 55
CHASE_MAX_RSI = 74
CHASE_MIN_RANGE_PCT = 1.6

# =========================
# 리포트 간격
# =========================
BTC_REPORT_INTERVAL = 3600
STATUS_REPORT_INTERVAL = 1800
WATCH_REPORT_INTERVAL = 300

last_btc_report_time = 0
last_status_report_time = 0
last_watch_report_time = 0

# =========================
# 상태 저장
# =========================
active_positions = {}
recent_signal_alerts = {}
recent_watch_alerts = {}

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

def safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

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
주문 최대금액: {MAX_ENTRY_KRW:,}원

자동매수 허용:
- 쏘기 직전형
- 급등 돌파형
- 추격형(강한 경우만)

알림만 하고 자동매수 안 함:
- 초기 상승형
- 상승 시작형
- 눌림 반등형

포지션 복구: 켜짐
매도 확인: 켜짐
오늘 요약: /today

강한 코인만 자동으로 보게 시작할게.
"""
    )

# =========================
# 로그
# =========================
def load_logs():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

trade_logs = load_logs()

def save_logs():
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(trade_logs, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[로그 저장 오류] {e}")

def add_log(item: dict):
    trade_logs.append(item)
    save_logs()

# =========================
# 포지션 파일
# =========================
def save_positions():
    try:
        with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(active_positions, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[포지션 저장 오류] {e}")

def load_positions():
    global active_positions
    if not os.path.exists(POSITIONS_FILE):
        active_positions = {}
        return

    try:
        with open(POSITIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        active_positions = data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[포지션 불러오기 오류] {e}")
        active_positions = {}

def clear_position_file_if_empty():
    if not active_positions:
        try:
            if os.path.exists(POSITIONS_FILE):
                os.remove(POSITIONS_FILE)
        except Exception:
            pass

# =========================
# 거래소 데이터
# =========================
def get_price(ticker: str) -> float:
    try:
        p = pybithumb.get_current_price(ticker)
        if p is None:
            return -1
        return float(p)
    except Exception:
        return -1

def get_orderbook_best_ask(ticker: str) -> float:
    try:
        ob = pybithumb.get_orderbook(ticker)
        if not ob:
            return -1

        if isinstance(ob, dict) and "asks" in ob and ob["asks"]:
            ask0 = ob["asks"][0]
            if isinstance(ask0, dict):
                return float(ask0.get("price", -1))

        if isinstance(ob, dict) and "data" in ob and "asks" in ob["data"] and ob["data"]["asks"]:
            ask0 = ob["data"]["asks"][0]
            if isinstance(ask0, dict):
                return float(ask0.get("price", -1))

        return -1
    except Exception as e:
        print("[ORDERBOOK ERROR]", ticker, e)
        return -1

def get_ohlcv(ticker: str, interval: str = "minute3"):
    try:
        return pybithumb.get_ohlcv(ticker, interval=interval)
    except Exception:
        return None

def get_balance(ticker: str) -> float:
    try:
        bal = bithumb.get_balance(ticker)
        return float(bal[0] or 0)
    except Exception:
        return 0.0

def get_krw_balance():
    try:
        bal = bithumb.get_balance("BTC")
        if not bal or len(bal) < 4:
            return 0.0
        return float(bal[2] or 0)
    except Exception as e:
        print("[KRW BALANCE ERROR]", e)
        return 0.0

# =========================
# 지표
# =========================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def ma(df, period):
    try:
        return float(df["close"].rolling(period).mean().iloc[-1])
    except Exception:
        return 0.0

def get_vol_ratio(df, short_n=5, long_n=20):
    try:
        if len(df) < short_n + long_n:
            return 0.0
        recent = float(df["volume"].tail(short_n).mean())
        prev = float(df["volume"].tail(short_n + long_n).head(long_n).mean())
        if prev <= 0:
            return 0.0
        return recent / prev
    except Exception:
        return 0.0

def get_recent_change_pct(df, n=5):
    try:
        recent = df.tail(n)
        start = float(recent["close"].iloc[0])
        end = float(recent["close"].iloc[-1])
        if start <= 0:
            return 0.0
        return ((end - start) / start) * 100
    except Exception:
        return 0.0

def get_range_pct(df, n=10):
    try:
        recent = df.tail(n)
        hi = float(recent["high"].max())
        lo = float(recent["low"].min())
        if lo <= 0:
            return 0.0
        return ((hi - lo) / lo) * 100
    except Exception:
        return 0.0

def short_trend_up(df):
    try:
        closes = list(df.tail(3)["close"])
        if len(closes) < 3:
            return False
        return closes[-1] >= closes[-2] and closes[-2] >= closes[-3]
    except Exception:
        return False

def upper_wick_too_large(df):
    try:
        last = df.iloc[-1]
        body_top = max(float(last["open"]), float(last["close"]))
        high = float(last["high"])
        low = float(last["low"])
        if high <= low:
            return False
        upper_wick_ratio = (high - body_top) / (high - low)
        return upper_wick_ratio >= 0.48
    except Exception:
        return False

def candle_body_ratio(df):
    try:
        last = df.iloc[-1]
        o = float(last["open"])
        c = float(last["close"])
        h = float(last["high"])
        l = float(last["low"])
        if h <= l:
            return 0.0
        return abs(c - o) / (h - l)
    except Exception:
        return 0.0

# =========================
# BTC 필터
# =========================
def get_btc_df():
    for interval in ["minute60", "minute30", "minute10"]:
        df = get_ohlcv("BTC", interval)
        if df is not None and len(df) >= 20:
            return df, interval
    df = get_ohlcv("BTC", "day")
    if df is not None and len(df) >= 20:
        return df, "day"
    return None, None

def get_btc_market_state():
    df, _ = get_btc_df()
    if df is None:
        return True, "BTC 조회 실패지만 진행", 0.0

    price = get_price("BTC")
    if price <= 0:
        return True, "BTC 현재가 조회 실패지만 진행", 0.0

    recent_drop_pct = get_recent_change_pct(df, 4)
    ma20 = ma(df, 20)

    if recent_drop_pct <= BTC_STRONG_BLOCK_PCT:
        return False, f"BTC가 크게 빠지는 중이야 ({recent_drop_pct:.2f}%)", recent_drop_pct
    if recent_drop_pct <= BTC_CRASH_BLOCK_PCT:
        return False, f"BTC가 약해서 신규 진입을 쉬고 있어 ({recent_drop_pct:.2f}%)", recent_drop_pct
    if BTC_MA_FILTER and ma20 > 0 and price < ma20 * 0.992:
        return False, "BTC가 약한 자리라 신규 진입을 쉬고 있어", recent_drop_pct

    return True, "시장 분위기 무난", recent_drop_pct

def analyze_btc_flow():
    df, label = get_btc_df()
    if df is None:
        return "📊 BTC 리포트\nBTC 데이터를 불러오지 못했어."

    price = get_price("BTC")
    if price <= 0:
        return "📊 BTC 리포트\nBTC 현재가 조회 실패."

    change_pct = get_recent_change_pct(df, 4)
    ma5v = ma(df, 5)
    ma20v = ma(df, 20)

    if change_pct <= -2.0:
        state = "🚨 강한 하락"
        desc = "지금은 신규 진입이 꽤 위험한 장이야."
    elif change_pct <= -0.8:
        state = "⚠️ 약한 하락"
        desc = "강한 코인만 살아남는 장이야."
    elif change_pct >= 2.0:
        state = "🔥 강한 상승"
        desc = "시장 분위기가 좋아서 수익 기대가 괜찮아."
    elif change_pct >= 0.8:
        state = "👍 완만한 상승"
        desc = "무난한 장이라 종목만 잘 고르면 기회가 있어."
    else:
        state = "😐 횡보"
        desc = "시장 방향은 애매해서 종목 힘이 중요해."

    loc = "단기 이동평균 위" if price >= ma5v and price >= ma20v else "단기 이동평균 아래/혼조"

    return f"""
📊 BTC 리포트 ({label} 기준)

💰 현재가: {fmt_price(price)}
📉 최근 변동률: {change_pct:.2f}%
📌 상태: {state}
📎 현재 위치: {loc}

한줄 해석:
{desc}
"""

# =========================
# 점수/리스크
# =========================
def dynamic_stop_loss_pct(vol_ratio, range_pct, strategy):
    stop = BASE_STOP_LOSS_PCT

    if strategy in ["PRE_BREAKOUT", "BREAKOUT", "CHASE"]:
        stop -= 0.05

    if vol_ratio >= 4.0:
        stop -= 0.05
    if range_pct >= 4.0:
        stop -= 0.05

    return max(stop, -2.2)

def dynamic_take_profit_pct(vol_ratio, range_pct, strategy):
    tp = BASE_TP_PCT

    if strategy == "PRE_BREAKOUT":
        tp += 0.6
    elif strategy == "BREAKOUT":
        tp += 0.8
    elif strategy == "CHASE":
        tp += 0.9

    if vol_ratio >= 4.0:
        tp += 0.3

    return min(tp, 6.6)

def dynamic_time_stop_sec(vol_ratio, range_pct, strategy):
    if strategy == "PRE_BREAKOUT":
        base = 900
    elif strategy == "BREAKOUT":
        base = 840
    elif strategy == "CHASE":
        base = 720
    else:
        base = 900

    if vol_ratio >= 4.0:
        base -= 60
    elif vol_ratio < 2.0:
        base += 60

    return max(TIME_STOP_MIN_SEC, min(base, TIME_STOP_MAX_SEC))

def expected_edge_score(stop_loss_pct, take_profit_pct, base_score):
    risk = abs(float(stop_loss_pct))
    reward = float(take_profit_pct)
    if risk <= 0:
        return -999
    rr = reward / risk
    return base_score + (rr * 1.4)

# =========================
# 공용 강한 진입 필터
# =========================
def strong_momentum_filter(df, change_pct, vol_ratio):
    if upper_wick_too_large(df):
        return False
    if candle_body_ratio(df) < 0.46:
        return False
    if vol_ratio < 2.5:
        return False
    if change_pct > 4.2:
        return False
    return True

# =========================
# 전략 1: 초기 상승형 (알림용)
# =========================
def analyze_early_entry(ticker: str):
    df = get_ohlcv(ticker, "minute3")
    if df is None or len(df) < 30:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    change_pct = get_recent_change_pct(df, 5)
    vol_ratio = get_vol_ratio(df, 4, 18)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    range_pct = get_range_pct(df, 10)
    trend_up = short_trend_up(df)

    if change_pct < EARLY_MIN_CHANGE_PCT or change_pct > EARLY_MAX_CHANGE_PCT:
        return None
    if vol_ratio < EARLY_MIN_VOL_RATIO:
        return None
    if rsi < EARLY_MIN_RSI or rsi > EARLY_MAX_RSI:
        return None
    if range_pct < EARLY_MIN_RANGE_PCT:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None
    if not trend_up:
        return None

    easy_score = 0
    if ma5v >= ma10v:
        easy_score += 1
    if trend_up:
        easy_score += 1
    if vol_ratio >= 2.6:
        easy_score += 1
    if change_pct >= 1.2:
        easy_score += 1
    if 47 <= rsi <= 61:
        easy_score += 1

    return {
        "ticker": ticker,
        "strategy": "EARLY",
        "strategy_label": "초기 상승형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "signal_score": easy_score,
    }

# =========================
# 전략 2: 상승 시작형 (알림용)
# =========================
def analyze_prepump_entry(ticker: str):
    df = get_ohlcv(ticker, "minute3")
    if df is None or len(df) < 30:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    rsi = safe_float(calculate_rsi(df).iloc[-1])
    vol_ratio = get_vol_ratio(df, 5, 20)
    change_pct = get_recent_change_pct(df, 5)
    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    range_pct = get_range_pct(df, 10)

    if change_pct < PREPUMP_MIN_CHANGE_PCT or change_pct > PREPUMP_MAX_CHANGE_PCT:
        return None
    if vol_ratio < PREPUMP_MIN_VOL_RATIO:
        return None
    if rsi < PREPUMP_MIN_RSI or rsi > PREPUMP_MAX_RSI:
        return None
    if range_pct < PREPUMP_MIN_RANGE_PCT:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None

    score = 5.0 + min(vol_ratio, 4.0)

    return {
        "ticker": ticker,
        "strategy": "PREPUMP",
        "strategy_label": "상승 시작형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "signal_score": r(score, 2),
    }

# =========================
# 전략 3: 눌림 반등형 (알림용)
# =========================
def analyze_pullback_entry(ticker: str):
    df = get_ohlcv(ticker, "minute3")
    if df is None or len(df) < 35:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    recent15 = df.tail(15)
    recent_low = float(recent15["low"].min())
    recent_high = float(recent15["high"].max())
    if recent_low <= 0:
        return None

    total_pump_pct = ((recent_high - recent_low) / recent_low) * 100
    if total_pump_pct < PULLBACK_MIN_TOTAL_PUMP_PCT:
        return None

    last3 = df.tail(3)
    c1 = float(last3["close"].iloc[0])
    c2 = float(last3["close"].iloc[1])
    c3 = float(last3["close"].iloc[2])

    if not (c2 < c1 and c3 > c2):
        return None

    rebound_pct = ((c3 - c2) / c2) * 100 if c2 > 0 else 0.0
    if rebound_pct < PULLBACK_REBOUND_MIN_PCT:
        return None

    vol_ratio = get_vol_ratio(df, 3, 10)
    if vol_ratio < PULLBACK_MIN_VOL_RATIO:
        return None

    return {
        "ticker": ticker,
        "strategy": "PULLBACK",
        "strategy_label": "눌림 반등형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(rebound_pct, 2),
        "signal_score": r(4.8 + vol_ratio, 2),
    }

# =========================
# 전략 4: 쏘기 직전형 (자동매수)
# =========================
def analyze_pre_breakout_entry(ticker: str):
    df = get_ohlcv(ticker, "minute1")
    if df is None or len(df) < 35:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    change_pct = get_recent_change_pct(df, 5)
    vol_ratio = get_vol_ratio(df, 3, 15)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    range_pct = get_range_pct(df, 8)
    body_ratio = candle_body_ratio(df)

    if change_pct < 0 or change_pct > PRE_BREAKOUT_MAX_CHANGE_PCT:
        return None
    if vol_ratio < PRE_BREAKOUT_MIN_VOL_RATIO:
        return None
    if rsi < PRE_BREAKOUT_MIN_RSI or rsi > PRE_BREAKOUT_MAX_RSI:
        return None
    if range_pct < PRE_BREAKOUT_MIN_RANGE_PCT:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None
    if body_ratio < 0.42:
        return None

    recent_high = float(df.tail(8)["high"].iloc[:-1].max())
    if recent_high <= 0:
        return None

    gap_to_break = ((recent_high - current_price) / current_price) * 100
    if gap_to_break < 0 or gap_to_break > PRE_BREAKOUT_MAX_GAP_PCT:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if last_jump_pct < 0.15:
        return None

    score = 6.8 + min(vol_ratio, 4.5) + (PRE_BREAKOUT_MAX_GAP_PCT - gap_to_break) * 4.0
    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "PRE_BREAKOUT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "PRE_BREAKOUT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "PRE_BREAKOUT")
    edge_score = expected_edge_score(stop_loss_pct, take_profit_pct, score)

    return {
        "ticker": ticker,
        "strategy": "PRE_BREAKOUT",
        "strategy_label": "쏘기 직전형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "gap_to_break": r(gap_to_break, 2),
        "signal_score": r(score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "reason": (
            f"- 거의 돌파 직전이야\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- 직전 고점까지 남은 거리 {gap_to_break:.2f}%\n"
            f"- RSI {rsi:.2f}"
        )
    }

# =========================
# 전략 5: 급등 돌파형 (자동매수)
# =========================
def analyze_breakout_entry(ticker: str):
    df = get_ohlcv(ticker, "minute1")
    if df is None or len(df) < 40:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    change_pct = get_recent_change_pct(df, 5)
    vol_ratio = get_vol_ratio(df, 3, 15)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    range_pct = get_range_pct(df, 8)
    body_ratio = candle_body_ratio(df)

    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100

    if change_pct < BREAKOUT_MIN_CHANGE_PCT or change_pct > BREAKOUT_MAX_CHANGE_PCT:
        return None
    if vol_ratio < BREAKOUT_MIN_VOL_RATIO:
        return None
    if rsi < BREAKOUT_MIN_RSI or rsi > BREAKOUT_MAX_RSI:
        return None
    if range_pct < BREAKOUT_MIN_RANGE_PCT:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if not strong_momentum_filter(df, change_pct, vol_ratio):
        return None
    if body_ratio < 0.5:
        return None
    if last_jump_pct < 0.25:
        return None

    recent_high = float(df.tail(10)["high"].iloc[:-1].max())
    if recent_high <= 0:
        return None

    break_pct = ((current_price - recent_high) / recent_high) * 100
    if break_pct < BREAKOUT_BREAK_PCT:
        return None

    score = 7.0 + min(vol_ratio, 5.0) + min(change_pct, 4.0) * 0.9 + min(break_pct, 1.0) * 2.0
    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "BREAKOUT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "BREAKOUT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "BREAKOUT")
    edge_score = expected_edge_score(stop_loss_pct, take_profit_pct, score)

    return {
        "ticker": ticker,
        "strategy": "BREAKOUT",
        "strategy_label": "급등 돌파형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "break_pct": r(break_pct, 2),
        "signal_score": r(score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "reason": (
            f"- 힘 있게 고점 돌파 중이야\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- 돌파폭 {break_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
        )
    }

# =========================
# 전략 6: 추격형 (자동매수, 강화)
# =========================
def analyze_chase_entry(ticker: str):
    df = get_ohlcv(ticker, "minute1")
    if df is None or len(df) < 40:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    change_pct = get_recent_change_pct(df, 5)
    vol_ratio = get_vol_ratio(df, 3, 15)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    range_pct = get_range_pct(df, 8)
    body_ratio = candle_body_ratio(df)

    if change_pct < CHASE_MIN_CHANGE_PCT or change_pct > CHASE_MAX_CHANGE_PCT:
        return None
    if vol_ratio < CHASE_MIN_VOL_RATIO:
        return None
    if rsi < CHASE_MIN_RSI or rsi > CHASE_MAX_RSI:
        return None
    if range_pct < CHASE_MIN_RANGE_PCT:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None
    if body_ratio < 0.55:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if last_jump_pct < 0.35:
        return None

    score = 7.2 + min(vol_ratio, 6.0) + min(change_pct, 3.8) * 0.9
    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "CHASE")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "CHASE")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "CHASE")
    edge_score = expected_edge_score(stop_loss_pct, take_profit_pct, score)

    return {
        "ticker": ticker,
        "strategy": "CHASE",
        "strategy_label": "추격형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "signal_score": r(score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "reason": (
            f"- 이미 오르는 중인데 힘이 아주 강해\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
        )
    }

# =========================
# 주문
# =========================
def get_safe_use_krw(multiplier: float = 1.0):
    krw = get_krw_balance()
    use_krw = min(krw * KRW_USE_RATIO, MAX_ENTRY_KRW)
    use_krw = use_krw - ORDER_BUFFER_KRW
    use_krw = use_krw * multiplier
    return max(use_krw, 0)

def calc_order_qty(ticker: str, entry_price: float, multiplier: float = 1.0):
    if entry_price <= 0:
        return 0.0, 0.0, 0.0

    best_ask = get_orderbook_best_ask(ticker)
    if best_ask <= 0:
        best_ask = entry_price

    safe_price = max(entry_price, best_ask) * MARKET_BUY_SLIPPAGE
    use_krw = get_safe_use_krw(multiplier)

    if use_krw < MIN_ORDER_KRW:
        return 0.0, use_krw, safe_price

    qty = use_krw / safe_price
    qty = math.floor(qty * 100000000) / 100000000
    return qty, use_krw, safe_price

def build_position(signal, filled_entry, filled_qty):
    return {
        "ticker": signal["ticker"],
        "strategy": signal["strategy"],
        "strategy_label": signal["strategy_label"],
        "entry_price": filled_entry,
        "qty": filled_qty,
        "stop_loss_pct": signal["stop_loss_pct"],
        "take_profit_pct": signal["take_profit_pct"],
        "time_stop_sec": signal["time_stop_sec"],
        "entered_at": time.time(),
        "peak_price": filled_entry,
        "breakeven_armed": False,
        "trailing_armed": False,
        "reason": signal["reason"],
        "edge_score": signal.get("edge_score", 0),
    }

def buy_market(signal):
    ticker = signal["ticker"]
    entry_price = get_price(ticker)
    if entry_price <= 0:
        return False, "현재가를 불러오지 못했어"

    before_balance = get_balance(ticker)
    last_error = ""

    for multiplier in RETRY_KRW_LEVELS:
        qty, use_krw, _ = calc_order_qty(ticker, entry_price, multiplier)

        if qty <= 0:
            krw = get_krw_balance()
            return False, f"원화 잔고가 부족해서 매수하지 않았어 (사용 가능 원화: {fmt_price(krw)})"

        try:
            result = bithumb.buy_market_order(ticker, qty)
            time.sleep(1.5)

            if isinstance(result, dict):
                status = str(result.get("status", ""))
                if status and status != "0000":
                    msg = result.get("message", "알 수 없는 오류")
                    last_error = f"{msg}"
                    if status == "5600":
                        continue
                    return False, f"매수 실패 / 응답: {result}"

            after_balance = get_balance(ticker)
            filled_qty = round(after_balance - before_balance, 8)

            if filled_qty <= 0:
                last_error = f"체결 확인이 안 됐어 / 응답: {result}"
                continue

            filled_entry = get_price(ticker)
            if filled_entry <= 0:
                filled_entry = entry_price

            active_positions[ticker] = build_position(signal, filled_entry, filled_qty)
            save_positions()

            add_log({
                "time": int(time.time()),
                "type": "BUY",
                "ticker": ticker,
                "strategy": signal["strategy"],
                "strategy_label": signal["strategy_label"],
                "entry": filled_entry,
                "qty": filled_qty,
                "used_krw": use_krw,
                "stop_loss_pct": signal["stop_loss_pct"],
                "take_profit_pct": signal["take_profit_pct"],
                "time_stop_sec": signal["time_stop_sec"],
                "edge_score": signal.get("edge_score"),
                "reason": signal["reason"],
            })

            return True, {
                "entry": filled_entry,
                "qty": filled_qty,
                "used_krw": use_krw,
            }

        except Exception as e:
            last_error = str(e)

    return False, f"매수는 시도했지만 주문 금액이 빡빡해서 실패했어 / 마지막 사유: {last_error}"

# =========================
# 매도 확인
# =========================
def confirm_sell_filled(ticker: str, before_balance: float, retries: int = 6, sleep_sec: float = 1.0):
    for _ in range(retries):
        time.sleep(sleep_sec)
        after_balance = get_balance(ticker)
        if after_balance < before_balance * 0.2:
            return True, after_balance
    return False, get_balance(ticker)

def sell_market_confirmed(ticker: str, reason_type: str, pos: dict, current_price: float, pnl_pct: float, held_sec: int = 0):
    try:
        before_balance = get_balance(ticker)
        if before_balance <= 0:
            return False, "잔고가 없어서 매도할 수 없었어"

        result = bithumb.sell_market_order(ticker, before_balance)
        ok, _ = confirm_sell_filled(ticker, before_balance)

        if not ok:
            return False, f"매도 요청은 갔지만 체결 확인이 안 됐어 / 응답: {result}"

        add_log({
            "time": int(time.time()),
            "type": reason_type,
            "ticker": ticker,
            "strategy": pos["strategy"],
            "strategy_label": pos["strategy_label"],
            "entry": float(pos["entry_price"]),
            "exit": current_price,
            "pnl_pct": round(pnl_pct, 2),
            "held_sec": int(held_sec),
            "edge_score": pos.get("edge_score", 0),
        })

        active_positions.pop(ticker, None)
        save_positions()
        clear_position_file_if_empty()

        return True, "매도 완료"
    except Exception as e:
        return False, str(e)

# =========================
# 포지션 복구
# =========================
def recover_positions_from_exchange():
    global active_positions

    if active_positions:
        return

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print("[복구 티커 조회 오류]", e)
        return

    recovered = 0

    for ticker in tickers:
        try:
            qty = get_balance(ticker)
            price = get_price(ticker)
            if qty <= 0 or price <= 0:
                continue

            if qty * price < DUST_KEEP_MIN_KRW:
                continue

            last_buy = None
            last_close_time = 0

            for log in trade_logs:
                if log.get("ticker") != ticker:
                    continue
                if log.get("type") == "BUY":
                    if (last_buy is None) or int(log.get("time", 0)) > int(last_buy.get("time", 0)):
                        last_buy = log
                elif log.get("type") in ["TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP"]:
                    last_close_time = max(last_close_time, int(log.get("time", 0)))

            if last_buy and int(last_buy.get("time", 0)) > last_close_time:
                entry_price = float(last_buy.get("entry", price))
                strategy = last_buy.get("strategy", "RECOVER")
                strategy_label = last_buy.get("strategy_label", "복구 포지션")
                stop_loss_pct = float(last_buy.get("stop_loss_pct", BASE_STOP_LOSS_PCT))
                take_profit_pct = float(last_buy.get("take_profit_pct", BASE_TP_PCT))
                time_stop_sec = int(last_buy.get("time_stop_sec", 900))
                edge_score = float(last_buy.get("edge_score", 0))
                reason = last_buy.get("reason", "이전 로그 기반 복구")
                entered_at = int(last_buy.get("time", int(time.time())))
            else:
                entry_price = price
                strategy = "RECOVER"
                strategy_label = "복구 포지션"
                stop_loss_pct = BASE_STOP_LOSS_PCT
                take_profit_pct = BASE_TP_PCT
                time_stop_sec = 900
                edge_score = 0
                reason = "현재 잔고 기반 복구"
                entered_at = int(time.time())

            active_positions[ticker] = {
                "ticker": ticker,
                "strategy": strategy,
                "strategy_label": strategy_label,
                "entry_price": entry_price,
                "qty": qty,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "time_stop_sec": time_stop_sec,
                "entered_at": entered_at,
                "peak_price": max(entry_price, price),
                "breakeven_armed": False,
                "trailing_armed": False,
                "reason": reason,
                "edge_score": edge_score,
            }
            recovered += 1
        except Exception:
            continue

    if recovered > 0:
        save_positions()
        send(f"♻️ 포지션 복구 완료\n복구된 코인 수: {recovered}")

# =========================
# 시그널 평가
# =========================
def signal_score(signal):
    return float(signal.get("edge_score", 0))

def should_auto_buy_signal(signal):
    strategy = signal["strategy"]
    edge = float(signal.get("edge_score", 0))
    tp = float(signal.get("take_profit_pct", 0))

    if strategy not in ["PRE_BREAKOUT", "BREAKOUT", "CHASE"]:
        return False
    if tp < MIN_EXPECTED_TP_PCT:
        return False
    if edge < MIN_EXPECTED_EDGE_SCORE:
        return False
    return True

def cooldown_ok(ticker):
    last = recent_signal_alerts.get(ticker, 0)
    return (time.time() - last) >= 300

# =========================
# 후보 알림
# =========================
def scan_watchlist():
    global recent_watch_alerts

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[후보 감지 오류] {e}")
        return

    results = []

    for ticker in tickers[:TOP_TICKERS]:
        if ticker == "BTC":
            continue

        for analyzer in [
            analyze_pre_breakout_entry,
            analyze_breakout_entry,
            analyze_early_entry,
            analyze_prepump_entry,
            analyze_pullback_entry,
        ]:
            signal = analyzer(ticker)
            if signal:
                results.append(signal)

    if not results:
        return

    results.sort(key=lambda x: float(x.get("signal_score", 0)), reverse=True)
    top = results[:5]

    lines = []
    now_ts = time.time()

    for item in top:
        ticker = item["ticker"]
        prev = recent_watch_alerts.get(ticker, 0)
        if now_ts - prev < WATCH_REPORT_INTERVAL:
            continue
        recent_watch_alerts[ticker] = now_ts

        lines.append(
            f"• {ticker} / {fmt_price(item['current_price'])} / 상승 {float(item.get('change_pct', 0)):.2f}% / 거래량 {float(item.get('vol_ratio', 0)):.2f}배 / 방식 {item['strategy_label']}"
        )

    if not lines:
        return

    send("👀 빠른 후보 알림\n\n" + "\n\n".join(lines))

# =========================
# 자동 진입
# =========================
def scan_and_auto_trade():
    if not AUTO_BUY:
        return
    if not is_auto_time():
        return
    if len(active_positions) >= MAX_HOLDINGS:
        return

    market_ok, market_msg, _ = get_btc_market_state()
    if BTC_FILTER_ON and not market_ok:
        print(f"[시장 차단] {market_msg}")
        return

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[티커 조회 오류] {e}")
        return

    candidates = []

    analyzers = [
        analyze_pre_breakout_entry,
        analyze_breakout_entry,
        analyze_chase_entry,
    ]

    for ticker in tickers[:TOP_TICKERS]:
        if ticker == "BTC":
            continue

        for analyzer in analyzers:
            signal = analyzer(ticker)
            if signal and should_auto_buy_signal(signal):
                candidates.append(signal)

    if not candidates:
        return

    candidates.sort(key=signal_score, reverse=True)
    best = candidates[0]
    ticker = best["ticker"]

    if not cooldown_ok(ticker):
        return

    recent_signal_alerts[ticker] = time.time()

    success, result = buy_market(best)
    if not success:
        send(
            f"""
❌ 자동매수 실패

📊 {ticker}
방식: {best['strategy_label']}

사유:
{result}
"""
        )
        return

    send(
        f"""
🔥 자동매수 완료

📊 {ticker}
방식: {best['strategy_label']}

💰 매수가: {fmt_price(result['entry'])}
📦 수량: {result['qty']:.6f}
💵 사용한 금액(대략): {fmt_price(result['used_krw'])}

🛑 손절 기준: {fmt_pct(best['stop_loss_pct'])}
🎯 목표 수익: {fmt_pct(best['take_profit_pct'])}
⏱ 오래 안 가면 정리: {int(best['time_stop_sec'] / 60)}분
📈 수익 기대 점수: {best['edge_score']:.2f}

매수 이유
{best['reason']}
"""
    )

# =========================
# 포지션 관리
# =========================
def monitor_positions():
    for ticker, pos in list(active_positions.items()):
        try:
            current_price = get_price(ticker)
            if current_price <= 0:
                continue

            balance = get_balance(ticker)
            market_value = balance * current_price

            if balance <= 0:
                active_positions.pop(ticker, None)
                save_positions()
                clear_position_file_if_empty()
                continue

            entry_price = float(pos["entry_price"])
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0

            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price
                save_positions()

            stop_price = entry_price * (1 + pos["stop_loss_pct"] / 100)
            tp_price = entry_price * (1 + pos["take_profit_pct"] / 100)
            held_sec = int(time.time() - int(pos["entered_at"]))

            # 5천원 미만은 유지
            if market_value < MIN_ORDER_KRW:
                continue

            # 시간정리
            if held_sec >= pos["time_stop_sec"]:
                if pnl_pct < 0.8:
                    ok, msg = sell_market_confirmed(ticker, "TIME_STOP", pos, current_price, pnl_pct, held_sec)
                    if ok:
                        send(
                            f"""
⏱ 오래 안 가서 정리

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
⏰ 보유시간: {int(held_sec/60)}분

힘이 약해서 정리했어.
"""
                        )
                    else:
                        send(
                            f"""
⚠️ 정리 시도했지만 확인 실패

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                        )
                    continue

            # 손절
            if current_price <= stop_price:
                ok, msg = sell_market_confirmed(ticker, "STOP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(
                        f"""
🚨 손절

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📉 손실률: {fmt_pct(pnl_pct)}
"""
                    )
                else:
                    send(
                        f"""
⚠️ 손절 시도했지만 확인 실패

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                    )
                continue

            # 본절
            if pnl_pct >= BREAKEVEN_TRIGGER_PCT:
                if not pos.get("breakeven_armed", False):
                    pos["breakeven_armed"] = True
                    save_positions()

            if pos.get("breakeven_armed", False):
                be_price = entry_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
                if held_sec >= 240 and current_price <= be_price:
                    ok, msg = sell_market_confirmed(ticker, "BREAKEVEN", pos, current_price, pnl_pct, held_sec)
                    if ok:
                        send(
                            f"""
🛡️ 손실 없이 정리

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📈 결과: {fmt_pct(pnl_pct)}
"""
                        )
                    else:
                        send(
                            f"""
⚠️ 본절 정리 시도했지만 확인 실패

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                        )
                    continue

            # 트레일링
            if pnl_pct >= TRAIL_START_PCT:
                if not pos.get("trailing_armed", False):
                    pos["trailing_armed"] = True
                    save_positions()

            if pos.get("trailing_armed", False):
                trail_stop = pos["peak_price"] * (1 - TRAIL_BACKOFF_PCT / 100)
                if current_price <= trail_stop:
                    ok, msg = sell_market_confirmed(ticker, "TRAIL_TP", pos, current_price, pnl_pct, held_sec)
                    if ok:
                        send(
                            f"""
📈 오른 뒤 조금 내려와서 익절

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}

다시 꺾이기 전에 정리했어.
"""
                        )
                    else:
                        send(
                            f"""
⚠️ 익절 시도했지만 확인 실패

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                        )
                    continue

            # 목표익절
            if current_price >= tp_price:
                ok, msg = sell_market_confirmed(ticker, "TP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(
                        f"""
🎉 익절 완료

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
"""
                    )
                else:
                    send(
                        f"""
⚠️ 익절 시도했지만 확인 실패

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                    )
                continue

        except Exception as e:
            print(f"[포지션 감시 오류] {ticker} / {e}")
            traceback.print_exc()

# =========================
# 명령어
# =========================
def summary_command(update, context: CallbackContext):
    closes = [x for x in trade_logs if x.get("type") in ["TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP"]]
    if not closes:
        send("📊 아직 종료된 거래가 없어")
        return

    total = len(closes)
    wins = len([x for x in closes if float(x.get("pnl_pct", 0)) > 0])
    losses = len([x for x in closes if float(x.get("pnl_pct", 0)) <= 0])
    total_pnl = sum(float(x.get("pnl_pct", 0)) for x in closes)
    avg_pnl = total_pnl / total if total > 0 else 0

    send(
        f"""
📊 거래 요약

총 종료 거래: {total}
익절/플러스 종료: {wins}
손절/마이너스 종료: {losses}
승률: {(wins / total) * 100:.2f}%
누적 수익률: {total_pnl:.2f}%
평균 수익률: {avg_pnl:.2f}%
"""
    )

def today_command(update, context: CallbackContext):
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    closed_types = {"TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP"}

    today_closes = []
    for x in trade_logs:
        if x.get("type") not in closed_types:
            continue
        ts = int(x.get("time", 0))
        d = datetime.fromtimestamp(ts, tz).date()
        if d == today:
            today_closes.append(x)

    if not today_closes:
        send("📊 오늘 종료된 거래가 아직 없어")
        return

    total = len(today_closes)
    wins = len([x for x in today_closes if float(x.get("pnl_pct", 0)) > 0])
    losses = total - wins
    total_pnl = sum(float(x.get("pnl_pct", 0)) for x in today_closes)
    avg_pnl = total_pnl / total if total > 0 else 0.0

    lines = []
    for x in today_closes[-10:]:
        lines.append(
            f"• {x.get('ticker','?')} / {x.get('type')} / {float(x.get('pnl_pct',0)):.2f}%"
        )

    send(
        f"""
📊 오늘 거래 요약

총 종료 거래: {total}
익절/플러스 종료: {wins}
손절/마이너스 종료: {losses}
승률: {(wins / total) * 100:.2f}%
누적 수익률: {total_pnl:.2f}%
평균 수익률: {avg_pnl:.2f}%

최근 종료 거래
""" + "\n".join(lines)
    )

def btc_command(update, context: CallbackContext):
    send(analyze_btc_flow())

def status_command(update, context: CallbackContext):
    if not active_positions:
        send("📭 현재 보유 포지션 없음")
        return

    lines = []
    for ticker, pos in active_positions.items():
        price = get_price(ticker)
        entry = float(pos["entry_price"])
        qty = float(pos.get("qty", 0))
        pnl = ((price - entry) / entry) * 100 if entry > 0 and price > 0 else 0
        held_min = int((time.time() - int(pos["entered_at"])) / 60)
        value = qty * price if price > 0 else 0
        lines.append(
            f"• {ticker} / 방식 {pos['strategy_label']} / 진입 {fmt_price(entry)} / 현재 {fmt_price(price)} / 수익률 {fmt_pct(pnl)} / 평가금액 {fmt_price(value)} / 보유 {held_min}분 / 기대점수 {pos.get('edge_score', 0):.2f}"
        )

    send("📌 현재 포지션\n\n" + "\n".join(lines))

# =========================
# 텔레그램 실행
# =========================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler("summary", summary_command))
dispatcher.add_handler(CommandHandler("today", today_command))
dispatcher.add_handler(CommandHandler("btc", btc_command))
dispatcher.add_handler(CommandHandler("status", status_command))

updater.start_polling(drop_pending_updates=True)

# =========================
# 시작 시 복구
# =========================
load_positions()
recover_positions_from_exchange()
save_positions()
send_startup_message()

print(f"🚀 {BOT_VERSION} 실행 / {TIMEZONE}")

# =========================
# 메인 루프
# =========================
last_scan_time = 0
last_position_check = 0

while True:
    now_ts = time.time()

    if now_ts - last_scan_time >= SCAN_INTERVAL:
        try:
            scan_and_auto_trade()
        except Exception as e:
            print(f"[자동진입 스캔 오류] {e}")
            traceback.print_exc()
        last_scan_time = now_ts

    if now_ts - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor_positions()
        except Exception as e:
            print(f"[포지션 감시 오류] {e}")
            traceback.print_exc()
        last_position_check = now_ts

    if now_ts - last_btc_report_time >= BTC_REPORT_INTERVAL:
        try:
            send(analyze_btc_flow())
        except Exception as e:
            print(f"[BTC 리포트 오류] {e}")
        last_btc_report_time = now_ts

    if now_ts - last_status_report_time >= STATUS_REPORT_INTERVAL:
        try:
            if active_positions:
                status_command(None, None)
        except Exception as e:
            print(f"[상태 리포트 오류] {e}")
        last_status_report_time = now_ts

    if now_ts - last_watch_report_time >= WATCH_REPORT_INTERVAL:
        try:
            scan_watchlist()
        except Exception as e:
            print(f"[후보 리포트 오류] {e}")
        last_watch_report_time = now_ts

    time.sleep(LOOP_SLEEP)
