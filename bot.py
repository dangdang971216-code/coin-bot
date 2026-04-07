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
FIXED_ENTRY_KRW = 11000

AUTO_BUY = True
AUTO_BUY_START_HOUR = 7
AUTO_BUY_END_HOUR = 23

MAX_HOLDINGS = 1

# =========================
# BTC 필터
# =========================
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -1.5
BTC_MA_FILTER = True

# =========================
# 기본 손익 관리
# =========================
BASE_STOP_LOSS_PCT = -1.7
BASE_TP_PCT = 2.8
TRAIL_START_PCT = 2.2
TRAIL_BACKOFF_PCT = 0.9
BREAKEVEN_TRIGGER_PCT = 1.3
BREAKEVEN_BUFFER_PCT = 0.15

# =========================
# EARLY (초조기 감지)
# =========================
EARLY_MIN_CHANGE_PCT = 0.2
EARLY_MAX_CHANGE_PCT = 2.2
EARLY_MIN_VOL_RATIO = 1.15
EARLY_MIN_RSI = 42
EARLY_MAX_RSI = 66
EARLY_MIN_RANGE_PCT = 0.45

# =========================
# PREPUMP (급등 직전)
# =========================
PREPUMP_MIN_CHANGE_PCT = 0.6
PREPUMP_MAX_CHANGE_PCT = 4.8
PREPUMP_MIN_VOL_RATIO = 1.5
PREPUMP_MIN_RSI = 46
PREPUMP_MAX_RSI = 72
PREPUMP_MIN_RANGE_PCT = 0.8

# =========================
# PULLBACK (급등 후 눌림)
# =========================
PULLBACK_MIN_TOTAL_PUMP_PCT = 5.0
PULLBACK_REBOUND_MIN_PCT = 0.5
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
# 로그
# =========================
def load_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
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
# 데이터
# =========================
def get_price(ticker: str) -> float:
    try:
        p = pybithumb.get_current_price(ticker)
        if p is None:
            return -1
        return float(p)
    except Exception:
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

def rebound_now(df):
    try:
        closes = list(df.tail(4)["close"])
        if len(closes) < 4:
            return False
        return closes[-2] < closes[-3] and closes[-1] > closes[-2]
    except Exception:
        return False

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
    df, label = get_btc_df()
    if df is None:
        return True, "BTC 조회 실패지만 진행", 0.0

    price = get_price("BTC")
    if price <= 0:
        return True, "BTC 현재가 조회 실패지만 진행", 0.0

    recent_drop_pct = get_recent_change_pct(df, 4)
    ma20 = ma(df, 20)

    if recent_drop_pct <= BTC_CRASH_BLOCK_PCT:
        return False, f"BTC 급락 차단 ({recent_drop_pct:.2f}%)", recent_drop_pct

    if BTC_MA_FILTER and ma20 > 0 and price < ma20 * 0.992:
        return False, "BTC가 MA20 아래라 약세", recent_drop_pct

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
        desc = "공격형도 신규 진입은 많이 조심해야 해."
    elif change_pct <= -0.8:
        state = "⚠️ 약한 하락"
        desc = "약한 장이라 강한 코인만 살아남아."
    elif change_pct >= 2.0:
        state = "🔥 강한 상승"
        desc = "시장 분위기 좋음."
    elif change_pct >= 0.8:
        state = "👍 완만한 상승"
        desc = "무난한 장. 종목 선별 중요."
    else:
        state = "😐 횡보"
        desc = "시장 자체 힘은 애매함."

    loc = "단기 MA 위" if price >= ma5v and price >= ma20v else "단기 MA 아래/혼조"

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
# 동적 리스크 관리
# =========================
def dynamic_stop_loss_pct(vol_ratio, range_pct, strategy):
    stop = BASE_STOP_LOSS_PCT

    if strategy == "EARLY":
        stop -= 0.15
    elif strategy == "PULLBACK":
        stop -= 0.10

    if vol_ratio >= 2.3:
        stop -= 0.15
    if range_pct >= 3.5:
        stop -= 0.15

    return max(stop, -2.3)

def dynamic_take_profit_pct(vol_ratio, range_pct, strategy):
    tp = BASE_TP_PCT

    if strategy == "EARLY":
        tp += 0.4
    elif strategy == "PREPUMP":
        tp += 0.2
    elif strategy == "PULLBACK":
        tp += 0.15

    if vol_ratio >= 2.3:
        tp += 0.35
    if range_pct >= 3.5:
        tp += 0.25

    return min(tp, 4.8)

def dynamic_time_stop_sec(vol_ratio, range_pct, strategy):
    if strategy == "EARLY":
        base = 900
    elif strategy == "PREPUMP":
        base = 600
    else:
        base = 720

    if vol_ratio >= 2.5:
        base -= 120
    elif vol_ratio < 1.3:
        base += 120

    if range_pct >= 4.0:
        base -= 60
    elif range_pct < 1.0:
        base += 120

    return max(240, min(base, 1500))

# =========================
# 전략 0: 초조기 감지
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
    rsi = float(calculate_rsi(df).iloc[-1])
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
    if ma5v <= 0 or ma10v <= 0:
        return None
    if ma5v < ma10v * 0.997:
        return None

    early_score = 0
    if ma5v >= ma10v:
        early_score += 1
    if trend_up:
        early_score += 1
    if vol_ratio >= 1.4:
        early_score += 1
    if change_pct >= 0.5:
        early_score += 1
    if 46 <= rsi <= 62:
        early_score += 1

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "EARLY")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "EARLY")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "EARLY")

    return {
        "ticker": ticker,
        "strategy": "EARLY",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "early_score": early_score,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "reason": (
            f"- 초조기 조짐\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}\n"
            f"- 조기점수 {early_score}"
        )
    }

# =========================
# 전략 1: 급등 직전
# =========================
def analyze_prepump_entry(ticker: str):
    df = get_ohlcv(ticker, "minute3")
    if df is None or len(df) < 30:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    rsi = float(calculate_rsi(df).iloc[-1])
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

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "PREPUMP")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "PREPUMP")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "PREPUMP")

    return {
        "ticker": ticker,
        "strategy": "PREPUMP",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "reason": (
            f"- 급등 직전 조짐\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}\n"
            f"- 단기 MA 상향"
        )
    }

# =========================
# 전략 2: 급등 후 눌림 재돌파
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

    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None

    range_pct = get_range_pct(df, 12)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "PULLBACK")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "PULLBACK")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "PULLBACK")

    return {
        "ticker": ticker,
        "strategy": "PULLBACK",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "pump_pct": r(total_pump_pct, 2),
        "rebound_pct": r(rebound_pct, 2),
        "range_pct": r(range_pct, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "reason": (
            f"- 급등 후 눌림 반등\n"
            f"- 최근 급등 {total_pump_pct:.2f}%\n"
            f"- 반등 {rebound_pct:.2f}%\n"
            f"- 거래량 {vol_ratio:.2f}배"
        )
    }

# =========================
# 주문
# =========================
def calc_order_qty(entry_price: float):
    if entry_price <= 0:
        return 0.0
    qty = FIXED_ENTRY_KRW / entry_price
    qty = round(qty, 8)
    if qty * entry_price < MIN_ORDER_KRW:
        qty = round((MIN_ORDER_KRW / entry_price) * 1.01, 8)
    return qty

def build_position(signal, filled_entry, filled_qty):
    return {
        "ticker": signal["ticker"],
        "strategy": signal["strategy"],
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
    }

def buy_market(signal):
    ticker = signal["ticker"]
    entry_price = get_price(ticker)
    if entry_price <= 0:
        return False, "현재가 조회 실패"

    qty = calc_order_qty(entry_price)
    if qty <= 0:
        return False, "주문 수량 계산 실패"

    before_balance = get_balance(ticker)

    try:
        result = bithumb.buy_market_order(ticker, qty)
        time.sleep(1.5)

        after_balance = get_balance(ticker)
        filled_qty = round(after_balance - before_balance, 8)

        if filled_qty <= 0:
            return False, f"체결 수량 확인 실패 / 응답: {result}"

        filled_entry = get_price(ticker)
        if filled_entry <= 0:
            filled_entry = entry_price

        active_positions[ticker] = build_position(signal, filled_entry, filled_qty)

        add_log({
            "time": int(time.time()),
            "type": "BUY",
            "ticker": ticker,
            "strategy": signal["strategy"],
            "entry": filled_entry,
            "qty": filled_qty,
            "stop_loss_pct": signal["stop_loss_pct"],
            "take_profit_pct": signal["take_profit_pct"],
            "time_stop_sec": signal["time_stop_sec"],
            "reason": signal["reason"],
        })

        return True, {"entry": filled_entry, "qty": filled_qty}
    except Exception as e:
        return False, str(e)

# =========================
# 점수
# =========================
def signal_score(signal):
    s = 0.0
    strategy = signal["strategy"]

    if strategy == "EARLY":
        s += 3.0
        s += signal.get("early_score", 0) * 0.9
        s += min(signal.get("vol_ratio", 0), 4.0) * 0.8
        s += min(signal.get("change_pct", 0), 3.0) * 0.5

    elif strategy == "PREPUMP":
        s += 5.0
        s += min(signal.get("vol_ratio", 0), 5.0)
        s += min(signal.get("change_pct", 0), 5.0) * 0.8

    elif strategy == "PULLBACK":
        s += 4.3
        s += min(signal.get("vol_ratio", 0), 4.0)
        s += min(signal.get("rebound_pct", 0), 3.0) * 0.8

    return s

def should_auto_buy_signal(signal):
    strategy = signal["strategy"]

    if strategy == "EARLY":
        # EARLY는 더 엄격하게
        if signal.get("early_score", 0) < 4:
            return False
        if signal.get("vol_ratio", 0) < 1.35:
            return False
        if signal.get("change_pct", 0) < 0.35:
            return False
        return True

    if strategy == "PREPUMP":
        return True

    if strategy == "PULLBACK":
        return True

    return False

def cooldown_ok(ticker):
    last = recent_signal_alerts.get(ticker, 0)
    return (time.time() - last) >= 600

# =========================
# 초조기 알림 전용
# =========================
def scan_early_watchlist():
    global recent_early_alerts

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[EARLY 감지 오류] {e}")
        return

    results = []
    for ticker in tickers[:TOP_TICKERS]:
        if ticker == "BTC":
            continue
        signal = analyze_early_entry(ticker)
        if signal:
            results.append(signal)

    if not results:
        return

    results.sort(key=signal_score, reverse=True)
    top = results[:5]

    lines = []
    now_ts = time.time()

    for item in top:
        ticker = item["ticker"]
        prev = recent_early_alerts.get(ticker, 0)
        if now_ts - prev < EARLY_REPORT_INTERVAL:
            continue
        recent_early_alerts[ticker] = now_ts

        lines.append(
            f"• {ticker} / {fmt_price(item['current_price'])} / 상승 {item['change_pct']:.2f}% / 거래량 {item['vol_ratio']:.2f}배 / RSI {item['rsi']:.2f} / 조기점수 {item['early_score']}"
        )

    if not lines:
        return

    send(
        "👀 초조기 후보 감지\n\n"
        + "\n".join(lines)
        + "\n\n아직 완전 진입 전일 수 있지만 오르기 직전 조짐 후보야."
    )

# =========================
# 자동 진입 스캔
# =========================
def scan_and_auto_trade():
    if not AUTO_BUY:
        return
    if not is_auto_time():
        return
    if len(active_positions) >= MAX_HOLDINGS:
        return

    market_ok, market_msg, btc_drop = get_btc_market_state()
    if BTC_FILTER_ON and not market_ok:
        print(f"[시장 차단] {market_msg}")
        return

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[티커 조회 오류] {e}")
        return

    candidates = []

    for ticker in tickers[:TOP_TICKERS]:
        if ticker == "BTC":
            continue

        early = analyze_early_entry(ticker)
        if early and should_auto_buy_signal(early):
            candidates.append(early)

        pre = analyze_prepump_entry(ticker)
        if pre and should_auto_buy_signal(pre):
            candidates.append(pre)

        pull = analyze_pullback_entry(ticker)
        if pull and should_auto_buy_signal(pull):
            candidates.append(pull)

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
전략: {best['strategy']}
사유: {result}
"""
        )
        return

    send(
        f"""
✅ 자동매수 완료

📊 {ticker}
전략: {best['strategy']}

💰 매수가: {fmt_price(result['entry'])}
📦 수량: {result['qty']:.6f}

🛑 손절: {fmt_pct(best['stop_loss_pct'])}
🎯 익절: {fmt_pct(best['take_profit_pct'])}
⏱ 시간손절: {int(best['time_stop_sec'] / 60)}분

진입 근거
{best['reason']}
"""
    )

# =========================
# 포지션 관리
# =========================
def monitor_positions():
    remove_list = []

    for ticker, pos in list(active_positions.items()):
        try:
            current_price = get_price(ticker)
            if current_price <= 0:
                continue

            balance = get_balance(ticker)
            if balance <= 0 or balance * current_price < MIN_ORDER_KRW:
                remove_list.append(ticker)
                continue

            entry_price = float(pos["entry_price"])
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0

            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price

            stop_price = entry_price * (1 + pos["stop_loss_pct"] / 100)
            tp_price = entry_price * (1 + pos["take_profit_pct"] / 100)
            held_sec = time.time() - pos["entered_at"]

            if held_sec >= pos["time_stop_sec"]:
                if pnl_pct < 0.8:
                    bithumb.sell_market_order(ticker, balance)
                    add_log({
                        "time": int(time.time()),
                        "type": "TIME_STOP",
                        "ticker": ticker,
                        "strategy": pos["strategy"],
                        "entry": entry_price,
                        "exit": current_price,
                        "pnl_pct": round(pnl_pct, 2),
                        "held_sec": int(held_sec),
                    })
                    send(
                        f"""
⏱ 시간손절

📊 {ticker}
전략: {pos['strategy']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
⏰ 보유시간: {int(held_sec/60)}분

시간 안에 힘이 안 붙어서 정리했어.
"""
                    )
                    remove_list.append(ticker)
                    continue

            if current_price <= stop_price:
                bithumb.sell_market_order(ticker, balance)
                add_log({
                    "time": int(time.time()),
                    "type": "STOP",
                    "ticker": ticker,
                    "strategy": pos["strategy"],
                    "entry": entry_price,
                    "exit": current_price,
                    "pnl_pct": round(pnl_pct, 2),
                })
                send(
                    f"""
🚨 손절

📊 {ticker}
전략: {pos['strategy']}

💰 현재가: {fmt_price(current_price)}
📉 손실률: {fmt_pct(pnl_pct)}
"""
                )
                remove_list.append(ticker)
                continue

            if pnl_pct >= BREAKEVEN_TRIGGER_PCT:
                pos["breakeven_armed"] = True

            if pos["breakeven_armed"]:
                be_price = entry_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
                if current_price <= be_price:
                    bithumb.sell_market_order(ticker, balance)
                    add_log({
                        "time": int(time.time()),
                        "type": "BREAKEVEN",
                        "ticker": ticker,
                        "strategy": pos["strategy"],
                        "entry": entry_price,
                        "exit": current_price,
                        "pnl_pct": round(pnl_pct, 2),
                    })
                    send(
                        f"""
🛡️ 본절 처리

📊 {ticker}
전략: {pos['strategy']}

💰 현재가: {fmt_price(current_price)}
📈 결과: {fmt_pct(pnl_pct)}
"""
                    )
                    remove_list.append(ticker)
                    continue

            if pnl_pct >= TRAIL_START_PCT:
                pos["trailing_armed"] = True

            if pos["trailing_armed"]:
                trail_stop = pos["peak_price"] * (1 - TRAIL_BACKOFF_PCT / 100)
                if current_price <= trail_stop:
                    bithumb.sell_market_order(ticker, balance)
                    add_log({
                        "time": int(time.time()),
                        "type": "TRAIL_TP",
                        "ticker": ticker,
                        "strategy": pos["strategy"],
                        "entry": entry_price,
                        "exit": current_price,
                        "pnl_pct": round(pnl_pct, 2),
                    })
                    send(
                        f"""
📈 트레일링 익절

📊 {ticker}
전략: {pos['strategy']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
"""
                    )
                    remove_list.append(ticker)
                    continue

            if current_price >= tp_price:
                bithumb.sell_market_order(ticker, balance)
                add_log({
                    "time": int(time.time()),
                    "type": "TP",
                    "ticker": ticker,
                    "strategy": pos["strategy"],
                    "entry": entry_price,
                    "exit": current_price,
                    "pnl_pct": round(pnl_pct, 2),
                })
                send(
                    f"""
🎉 익절 완료

📊 {ticker}
전략: {pos['strategy']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
"""
                )
                remove_list.append(ticker)
                continue

        except Exception as e:
            print(f"[포지션 감시 오류] {ticker} / {e}")
            traceback.print_exc()

    for ticker in remove_list:
        active_positions.pop(ticker, None)

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

def btc_command(update, context: CallbackContext):
    send(analyze_btc_flow())

def status_command(update, context: CallbackContext):
    if not active_positions:
        send("📭 현재 보유 포지션 없음")
        return

    lines = []
    for ticker, pos in active_positions.items():
        price = get_price(ticker)
        entry = pos["entry_price"]
        pnl = ((price - entry) / entry) * 100 if entry > 0 and price > 0 else 0
        held_min = int((time.time() - pos["entered_at"]) / 60)
        lines.append(
            f"• {ticker} / 전략 {pos['strategy']} / 진입 {fmt_price(entry)} / 현재 {fmt_price(price)} / 수익률 {fmt_pct(pnl)} / 보유 {held_min}분"
        )

    send("📌 현재 포지션\n\n" + "\n".join(lines))

# =========================
# 실행
# =========================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler("summary", summary_command))
dispatcher.add_handler(CommandHandler("btc", btc_command))
dispatcher.add_handler(CommandHandler("status", status_command))

updater.start_polling(drop_pending_updates=True)

print(f"🚀 초조기 포함 완전 자동 공격형 봇 실행 / {TIMEZONE}")

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

    if now_ts - last_early_report_time >= EARLY_REPORT_INTERVAL:
        try:
            scan_early_watchlist()
        except Exception as e:
            print(f"[초조기 후보 리포트 오류] {e}")
        last_early_report_time = now_ts

    time.sleep(LOOP_SLEEP)
