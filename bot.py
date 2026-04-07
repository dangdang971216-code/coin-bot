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

SCAN_INTERVAL = 20               # 공격형이라 자주 스캔
POSITION_CHECK_INTERVAL = 5
LOOP_SLEEP = 1

TOP_TICKERS = 100
MIN_ORDER_KRW = 5000
FIXED_ENTRY_KRW = 11000

# 완전 자동
AUTO_BUY = True

# 거래 시간대
AUTO_BUY_START_HOUR = 7
AUTO_BUY_END_HOUR = 23

# BTC 필터
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -1.5       # 최근 기준 급락이면 신규 진입 금지
BTC_MA_FILTER = True

# 손익 관리
BASE_STOP_LOSS_PCT = -1.7        # 기본 손절
BASE_TP_PCT = 2.8                # 기본 익절
TRAIL_START_PCT = 2.2            # 이익이 이 정도 나면 트레일링 시작
TRAIL_BACKOFF_PCT = 0.9          # 고점 대비 이 정도 밀리면 익절
BREAKEVEN_TRIGGER_PCT = 1.3      # 이익 보호
BREAKEVEN_BUFFER_PCT = 0.15

# 공격형 전략 파라미터
PREPUMP_MIN_CHANGE_PCT = 0.8
PREPUMP_MAX_CHANGE_PCT = 4.8
PREPUMP_MIN_VOL_RATIO = 1.8
PREPUMP_MIN_RSI = 48
PREPUMP_MAX_RSI = 72

PULLBACK_MIN_TOTAL_PUMP_PCT = 6.0
PULLBACK_REBOUND_MIN_PCT = 0.7
PULLBACK_MIN_VOL_RATIO = 1.15

# 알림 쿨다운
SIGNAL_COOLDOWN_SEC = 600
BTC_REPORT_INTERVAL = 3600
STATUS_REPORT_INTERVAL = 1800

# 동시 보유
MAX_HOLDINGS = 1

# =========================
# 전역 상태
# =========================
active_positions = {}
recent_signal_alerts = {}
last_btc_report_time = 0
last_status_report_time = 0

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
        df = pybithumb.get_ohlcv(ticker, interval=interval)
        return df
    except Exception:
        return None

def get_balance(ticker: str) -> float:
    try:
        bal = bithumb.get_balance(ticker)
        return float(bal[0] or 0)
    except Exception:
        return 0.0

def get_avg_buy_price(ticker: str):
    try:
        bal = bithumb.get_balance(ticker)
        # pybithumb balance tuple 구조에서 avg price는 환경마다 다를 수 있어 fallback 처리
        # 보통 (coin, in_use_coin, total_coin, krw, in_use_krw, total_krw) 형태여서 평균가 직접 안 나옴
        return None
    except Exception:
        return None

# =========================
# 지표
# =========================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_vol_ratio(df, short_n=5, long_n=20):
    try:
        if len(df) < long_n + 1:
            return 0.0
        recent = float(df["volume"].tail(short_n).mean())
        prev = float(df["volume"].tail(long_n + short_n).head(long_n).mean())
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

def ma(df, period):
    try:
        return float(df["close"].rolling(period).mean().iloc[-1])
    except Exception:
        return 0.0

# =========================
# BTC 필터 / 리포트
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
        return False, "BTC가 MA20 아래라 시장 분위기 약함", recent_drop_pct

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
        desc = "시장 약세가 강해서 신규 진입은 조심해야 해."
    elif change_pct <= -0.8:
        state = "⚠️ 약한 하락"
        desc = "장 분위기가 살짝 약해. 강한 코인만 봐야 해."
    elif change_pct >= 2.0:
        state = "🔥 강한 상승"
        desc = "시장 분위기가 좋아서 알트 반응 확률이 높아."
    elif change_pct >= 0.8:
        state = "👍 완만한 상승"
        desc = "무난한 편이라 종목 선별이 중요해."
    else:
        state = "😐 횡보"
        desc = "방향성이 강하지 않아서 종목별 차이가 커."

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
# 전략 1: 급등 직전
# =========================
def analyze_prepump_entry(ticker: str):
    df = get_ohlcv(ticker, "minute3")
    if df is None or len(df) < 30:
        return None

    current_price = get_price(ticker)
    if current_price <= 0:
        return None

    rsi_series = calculate_rsi(df)
    rsi = float(rsi_series.iloc[-1])
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
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if range_pct < 0.8:
        return None

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, strategy="PREPUMP")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, strategy="PREPUMP")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, strategy="PREPUMP")

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

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, strategy="PULLBACK")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, strategy="PULLBACK")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, strategy="PULLBACK")

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
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 단기 MA 상향"
        )
    }

# =========================
# 동적 손절 / 익절 / 시간손절
# =========================
def dynamic_stop_loss_pct(vol_ratio, range_pct, strategy="PREPUMP"):
    base = BASE_STOP_LOSS_PCT

    # 변동성 높고 거래량 높으면 손절 아주 약간 넓힘
    if vol_ratio >= 2.5:
        base -= 0.2
    if range_pct >= 4.0:
        base -= 0.2

    # 눌림 전략은 약간 더 여유
    if strategy == "PULLBACK":
        base -= 0.15

    # 너무 넓어지지 않게 제한
    return max(base, -2.3)

def dynamic_take_profit_pct(vol_ratio, range_pct, strategy="PREPUMP"):
    base = BASE_TP_PCT

    if vol_ratio >= 2.5:
        base += 0.5
    if range_pct >= 4.0:
        base += 0.4

    if strategy == "PULLBACK":
        base += 0.2

    return min(base, 4.5)

def dynamic_time_stop_sec(vol_ratio, range_pct, strategy="PREPUMP"):
    # 공격형이라 기본은 짧게
    # 활발한 코인은 더 짧게 판단
    # 덜 활발하면 조금 더 기다리되 너무 오래는 안 감
    base = 600  # 10분

    if strategy == "PREPUMP":
        base = 480  # 8분
    elif strategy == "PULLBACK":
        base = 720  # 12분

    if vol_ratio >= 2.5:
        base -= 120
    elif vol_ratio < 1.4:
        base += 120

    if range_pct >= 4.0:
        base -= 60
    elif range_pct < 1.2:
        base += 120

    return max(240, min(base, 1200))  # 4분 ~ 20분

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

        return True, {
            "entry": filled_entry,
            "qty": filled_qty,
            "raw": result
        }

    except Exception as e:
        return False, str(e)

def sell_market(ticker, balance):
    try:
        return bithumb.sell_market_order(ticker, balance)
    except Exception as e:
        raise e

# =========================
# 시그널 선택
# =========================
def signal_score(signal):
    score = 0.0

    if signal["strategy"] == "PREPUMP":
        score += 5
        score += min(signal["vol_ratio"], 5.0)
        score += min(signal["change_pct"], 5.0) * 0.8
    else:
        score += 4
        score += min(signal["vol_ratio"], 4.0)
        score += min(signal["rebound_pct"], 3.0)

    return score

def cooldown_ok(ticker):
    last = recent_signal_alerts.get(ticker, 0)
    return (time.time() - last) >= SIGNAL_COOLDOWN_SEC

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

    if not tickers:
        return

    candidates = []

    for ticker in tickers[:TOP_TICKERS]:
        if ticker == "BTC":
            continue

        pre = analyze_prepump_entry(ticker)
        if pre:
            candidates.append(pre)

        pull = analyze_pullback_entry(ticker)
        if pull:
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

            # 고점 갱신
            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price

            # 손절가 / 익절가
            stop_price = entry_price * (1 + pos["stop_loss_pct"] / 100)
            tp_price = entry_price * (1 + pos["take_profit_pct"] / 100)

            # 시간손절
            held_sec = time.time() - pos["entered_at"]
            if held_sec >= pos["time_stop_sec"]:
                # 시간손절 시 기준:
                # 이익이 아주 작거나 마이너스면 바로 정리
                if pnl_pct < 0.8:
                    sell_market(ticker, balance)
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

정해진 시간 안에 힘이 안 붙어서 정리했어.
"""
                    )
                    remove_list.append(ticker)
                    continue

            # 일반 손절
            if current_price <= stop_price:
                sell_market(ticker, balance)
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

            # 본절 보호
            if pnl_pct >= BREAKEVEN_TRIGGER_PCT:
                pos["breakeven_armed"] = True

            if pos["breakeven_armed"]:
                be_price = entry_price * (1 + BREAKEVEN_BUFFER_PCT / 100)
                if current_price <= be_price:
                    sell_market(ticker, balance)
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

            # 트레일링
            if pnl_pct >= TRAIL_START_PCT:
                pos["trailing_armed"] = True

            if pos["trailing_armed"]:
                trail_stop = pos["peak_price"] * (1 - TRAIL_BACKOFF_PCT / 100)
                if current_price <= trail_stop:
                    sell_market(ticker, balance)
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

            # 일반 익절
            if current_price >= tp_price:
                sell_market(ticker, balance)
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

print(f"🚀 완전 자동 공격형 봇 실행 / {TIMEZONE}")

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

    time.sleep(LOOP_SLEEP)
