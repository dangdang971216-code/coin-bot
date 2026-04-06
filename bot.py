import os
import time
import uuid
import json
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pybithumb
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Updater, CallbackQueryHandler, CommandHandler, CallbackContext

# =========================
# 환경설정
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

SCAN_INTERVAL = 60
POSITION_CHECK_INTERVAL = 10
LOOP_SLEEP = 1

TOP_TICKERS = 120
BUTTON_EXPIRE = 600
SAME_STATUS_COOLDOWN = 1200
MIN_REALERT_PRICE_CHANGE = 0.30

FIXED_ENTRY_KRW = 11000
MIN_ORDER = 5000

AUTO_BUY = True
AUTO_BUY_24H = True
AUTO_BUY_START_HOUR = 0
AUTO_BUY_END_HOUR = 24

DEBUG_SCAN = True
DEBUG_REJECT_REASON = True

# =========================
# 시장 필터
# =========================
BTC_MARKET_FILTER = True
BTC_CRASH_LOCK_MINUTES = 20
BTC_CRASH_THRESHOLD = -2.0
btc_lock_until = 0

# =========================
# 진입 기준
# =========================
ENTRY_NEAR_RANGE = 0.7
ENTRY_OK_MAX_GAP = 0.8
ENTRY_LATE_MAX_GAP = 1.3

MIN_TP_GAP = 1.3
MIN_VOL_RATIO = 0.28
MIN_STRONG_VOL_RATIO = 0.75
MIN_ENTRY_SCORE = 1
MIN_RESISTANCE_GAP = 0.8
MIN_RECENT_MOVE_PCT = 0.6

# =========================
# 강화 필터
# =========================
MIN_PRICE_RANGE_PCT = 0.9
SIDEWAYS_BLOCK_RANGE_PCT = 0.45
MAX_PUMP_REJECT_PCT = 8.5
MIN_RECOVERY_RATIO = 0.70
RECENT_DROP_BLOCK_PCT = -3.0
MA_SLOPE_LOOKBACK = 3
USE_TREND_CONFIRM = True

# =========================
# 등급 기준
# =========================
S_GRADE_MIN_VOL_RATIO = 1.0
S_GRADE_MIN_MOVE_PCT = 2.2
S_GRADE_MIN_ENTRY_SCORE = 5

# =========================
# 포지션 관리
# =========================
BREAKEVEN_TRIGGER = 1.3
BREAKEVEN_MARGIN = 0.15

USE_TRAILING_TP = True
TRAILING_ARM_PCT = 3.0
TRAILING_GIVEBACK_PCT = 0.8

# =========================
# 상태/등급 상수
# =========================
STATUS_BUY_NOW = "BUY_NOW"
STATUS_SCALP_OK = "SCALP_OK"
STATUS_WATCH = "WATCH"
STATUS_CHASE_BLOCK = "CHASE_BLOCK"

GRADE_S = "S"
GRADE_A = "A"
GRADE_B = "B"

LOG_FILE = "trade_log.json"

active_positions = {}
button_data_store = {}
recent_alerts = {}

# =========================
# 유틸
# =========================
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

def now_kst():
    return datetime.now(ZoneInfo(TIMEZONE))

def is_weekday_auto_time():
    if AUTO_BUY_24H:
        return True
    now = now_kst()
    return now.weekday() < 5 and AUTO_BUY_START_HOUR <= now.hour < AUTO_BUY_END_HOUR

def send(msg, keyboard=None):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip(), reply_markup=keyboard)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

def grade_rank(grade):
    if grade == GRADE_S:
        return 3
    if grade == GRADE_A:
        return 2
    return 1

def status_rank(status):
    if status == STATUS_BUY_NOW:
        return 4
    if status == STATUS_SCALP_OK:
        return 3
    if status == STATUS_WATCH:
        return 2
    return 1

def status_label(status):
    if status == STATUS_BUY_NOW:
        return "🔥 지금 진입 가능"
    if status == STATUS_SCALP_OK:
        return "⚡ 짧게 노려볼 수 있는 자리"
    if status == STATUS_WATCH:
        return "👀 관찰"
    return "⛔ 추격 금지"

def calc_order_qty(entry_price: float):
    try:
        entry_price = float(entry_price)
        if entry_price <= 0:
            return 0.0

        qty = FIXED_ENTRY_KRW / entry_price
        qty = round(qty, 8)

        if qty <= 0:
            return 0.0

        if qty * entry_price < MIN_ORDER:
            qty = round((MIN_ORDER / entry_price) * 1.01, 8)

        return qty
    except Exception:
        return 0.0

def reject(ticker, reason):
    if DEBUG_REJECT_REASON:
        print(f"[{ticker}] 탈락: {reason}")
    return None

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

def add_log(log):
    trade_logs.append(log)
    save_logs()

def get_trade_summary_text():
    closes = [x for x in trade_logs if x.get("type") in ["TP", "STOP", "TRAIL_TP", "BREAKEVEN", "SWITCH_OUT"]]

    if not closes:
        return "📊 아직 종료된 거래가 없어"

    total = len(closes)
    wins = len([x for x in closes if float(x.get("pnl_pct", 0)) >= 0])
    losses = len([x for x in closes if float(x.get("pnl_pct", 0)) < 0 or x.get("type") == "STOP"])
    win_rate = (wins / total) * 100 if total > 0 else 0
    total_pnl = sum(float(x.get("pnl_pct", 0)) for x in closes)
    avg_pnl = total_pnl / total if total > 0 else 0

    return (
        f"📊 거래 요약\n"
        f"총 종료 거래: {total}\n"
        f"익절/본절/플러스 종료: {wins}\n"
        f"손절/마이너스 종료: {losses}\n"
        f"승률: {win_rate:.2f}%\n"
        f"누적 수익률: {total_pnl:.2f}%\n"
        f"평균 수익률: {avg_pnl:.2f}%"
    )

# =========================
# 데이터 조회
# =========================
def get_price(ticker):
    try:
        price = pybithumb.get_current_price(ticker)
        if price is None:
            return -1
        return float(price)
    except Exception:
        return -1

def get_ohlcv(ticker):
    try:
        df = pybithumb.get_ohlcv(ticker)
        if df is None or len(df) < 50:
            return None
        return df
    except Exception:
        return None

def get_balance(ticker):
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

def detect_recent_pump(df):
    recent = df.tail(4)
    if len(recent) < 4:
        return 0.0
    start = float(recent["close"].iloc[0])
    end = float(recent["close"].iloc[-1])
    return ((end - start) / start) * 100 if start > 0 else 0.0

def detect_recent_move_pct(df, lookback=5):
    try:
        recent = df.tail(lookback)
        high_v = float(recent["high"].max())
        low_v = float(recent["low"].min())
        if low_v <= 0:
            return 0.0
        return ((high_v - low_v) / low_v) * 100
    except Exception:
        return 0.0

def recent_drop_too_strong(df):
    try:
        recent = df.tail(3)
        start = float(recent["close"].iloc[0])
        end = float(recent["close"].iloc[-1])
        if start <= 0:
            return False
        pct = ((end - start) / start) * 100
        return pct <= RECENT_DROP_BLOCK_PCT
    except Exception:
        return False

def is_sideways_too_flat(df):
    try:
        recent = df.tail(8)
        hi = float(recent["high"].max())
        lo = float(recent["low"].min())
        if lo <= 0:
            return True
        range_pct = ((hi - lo) / lo) * 100
        return range_pct < SIDEWAYS_BLOCK_RANGE_PCT
    except Exception:
        return True

def is_ma_slope_positive(df):
    try:
        ma5 = df["close"].rolling(5).mean()
        if len(ma5.dropna()) < MA_SLOPE_LOOKBACK + 1:
            return False
        recent = ma5.dropna().tail(MA_SLOPE_LOOKBACK + 1)
        return float(recent.iloc[-1]) >= float(recent.iloc[0])
    except Exception:
        return False

# =========================
# 더 꼼꼼한 보조 계산
# =========================
def count_near_lows(values, tolerance_pct=0.6):
    try:
        vals = sorted([float(v) for v in values if float(v) > 0])
        if not vals:
            return 0, 0.0
        base = vals[0]
        count = 0
        picked = []
        for v in vals:
            diff_pct = ((v - base) / base) * 100 if base > 0 else 999
            if diff_pct <= tolerance_pct:
                count += 1
                picked.append(v)
        level = sum(picked) / len(picked) if picked else base
        return count, level
    except Exception:
        return 0, 0.0

def calculate_support_resistance(df):
    """
    단순 최저점/최고점 대신:
    - support: 최근 20봉 저점들 중 여러 번 닿은 하단 구간 우선
    - resistance: 최근 20봉 고점들 중 여러 번 닿은 상단 구간 우선
    """
    try:
        recent20 = df.tail(20).copy()

        lows = list(recent20["low"].tail(12))
        highs = list(recent20["high"].tail(12))

        low_count, low_level = count_near_lows(lows, tolerance_pct=0.7)

        # support 후보
        recent_low_min = float(min(lows))
        recent_close_floor = float(recent20["close"].tail(8).min())
        if low_count >= 2 and low_level > 0:
            support = low_level
        else:
            support = (recent_low_min * 0.7) + (recent_close_floor * 0.3)

        # resistance 후보
        sorted_highs = sorted([float(x) for x in highs if float(x) > 0], reverse=True)
        if len(sorted_highs) >= 3:
            resistance = sum(sorted_highs[:3]) / 3
        elif len(sorted_highs) >= 1:
            resistance = sorted_highs[0]
        else:
            resistance = float(recent20["high"].max())

        return float(support), float(resistance)
    except Exception:
        recent20 = df.tail(20)
        return float(recent20["low"].min()), float(recent20["high"].max())

def detect_bad_flow(df):
    """
    단순히 종가 3개 하락만 보지 않고
    - 종가 하락
    - 고가도 낮아짐
    - 거래량도 살아나지 않음
    을 같이 봄
    """
    try:
        recent = df.tail(3)
        closes = list(recent["close"])
        highs = list(recent["high"])
        vols = list(recent["volume"])

        close_down = closes[2] < closes[1] < closes[0]
        high_down = highs[2] <= highs[1] <= highs[0]
        vol_weak = vols[2] <= max(vols[0], vols[1]) * 0.95

        return close_down and high_down and vol_weak
    except Exception:
        return False

def detect_rebound(df):
    """
    더 꼼꼼한 반등 판단:
    - 최근 저점 찍고
    - 마지막 종가가 직전 종가보다 위
    - 마지막 종가가 최근 3봉 평균보다 위
    - 마지막 고가가 직전 고가 이상
    - 마지막 저가가 직전 저가보다 크게 무너지지 않음
    """
    try:
        recent = df.tail(5)
        if len(recent) < 5:
            return False

        c = list(recent["close"])
        h = list(recent["high"])
        l = list(recent["low"])

        local_bounce = c[-2] <= min(c[-4], c[-3]) and c[-1] > c[-2]
        close_recover = c[-1] > (sum(c[-3:]) / 3)
        high_recover = h[-1] >= h[-2]
        low_hold = l[-1] >= l[-2] * 0.997

        return local_bounce and close_recover and high_recover and low_hold
    except Exception:
        return False

def is_uptrend_confirmed(df):
    """
    단순 종가 3개 상승이 아니라
    - 종가 우상향
    - 최근 3봉 중 2봉 이상 양봉
    - 마지막 종가가 ma5 위
    - 마지막 고가가 직전 2봉 고가 중 하나 이상 돌파
    """
    try:
        recent = df.tail(3)
        c = list(recent["close"])
        o = list(recent["open"])
        h = list(recent["high"])

        close_up = c[2] > c[1] >= c[0]
        bullish_count = sum(1 for i in range(3) if c[i] >= o[i])
        ma5 = float(df["close"].rolling(5).mean().iloc[-1])
        above_ma5 = c[-1] > ma5
        high_break = h[-1] >= max(h[0], h[1])

        return close_up and bullish_count >= 2 and above_ma5 and high_break
    except Exception:
        return False

def analyze_volume_quality(df):
    """
    거래량을 더 꼼꼼히 판단
    - 마지막 1봉 거래량
    - 최근 3봉 평균 거래량
    - 상승봉 쪽 거래량이 붙는지
    """
    try:
        recent20 = df.tail(20)
        recent10 = df.tail(10)
        recent3 = df.tail(3)

        last_vol = float(recent20["volume"].iloc[-1])
        avg20 = float(recent20["volume"].mean())
        avg10 = float(recent10["volume"].mean())
        avg3 = float(recent3["volume"].mean())

        if avg20 <= 0 or avg10 <= 0:
            return {
                "vol_ratio": 0.0,
                "volume_recovery_ok": False,
                "volume_recovery_ratio": 0.0,
                "volume_trend_ok": False,
                "up_candle_vol_ok": False
            }

        vol_ratio = last_vol / avg20
        volume_recovery_ratio = avg3 / avg10 if avg10 > 0 else 0.0
        volume_recovery_ok = volume_recovery_ratio >= MIN_RECOVERY_RATIO

        vols = list(recent3["volume"])
        opens = list(recent3["open"])
        closes = list(recent3["close"])

        up_vols = [vols[i] for i in range(3) if closes[i] >= opens[i]]
        down_vols = [vols[i] for i in range(3) if closes[i] < opens[i]]

        up_candle_vol_ok = False
        if up_vols:
            if down_vols:
                up_candle_vol_ok = (sum(up_vols) / len(up_vols)) >= (sum(down_vols) / len(down_vols)) * 0.95
            else:
                up_candle_vol_ok = True

        volume_trend_ok = vols[-1] >= vols[-2] * 0.85

        return {
            "vol_ratio": vol_ratio,
            "volume_recovery_ok": volume_recovery_ok,
            "volume_recovery_ratio": volume_recovery_ratio,
            "volume_trend_ok": volume_trend_ok,
            "up_candle_vol_ok": up_candle_vol_ok
        }
    except Exception:
        return {
            "vol_ratio": 0.0,
            "volume_recovery_ok": False,
            "volume_recovery_ratio": 0.0,
            "volume_trend_ok": False,
            "up_candle_vol_ok": False
        }

def calculate_tp(signal_entry, resistance, grade_hint, current_price):
    """
    고정 비율 + 저항 고려
    """
    try:
        if signal_entry <= 0 or resistance <= 0 or current_price <= 0:
            return signal_entry * 1.02, "짧은 목표"

        resistance_cap_loose = resistance * 0.996
        resistance_cap_mid = resistance * 0.994
        resistance_cap_tight = resistance * 0.992

        if grade_hint == "strong":
            base_tp = signal_entry * 1.07
            tp = min(base_tp, resistance_cap_loose)
            tp_type = "강한 목표"
        elif grade_hint == "mid":
            base_tp = signal_entry * 1.04
            tp = min(base_tp, resistance_cap_mid)
            tp_type = "보통 목표"
        else:
            base_tp = signal_entry * 1.02
            tp = min(base_tp, resistance_cap_tight)
            tp_type = "짧은 목표"

        if tp <= current_price:
            tp = max(current_price * 1.01, signal_entry * 1.015)

        return tp, tp_type
    except Exception:
        return signal_entry * 1.02, "짧은 목표"

# =========================
# 시장 필터
# =========================
def get_btc_market_state():
    global btc_lock_until

    if time.time() < btc_lock_until:
        remain = int((btc_lock_until - time.time()) / 60)
        return False, f"BTC 급락 잠금 중 ({remain}분 남음)"

    df = get_ohlcv("BTC")
    if df is None:
        return True, "BTC 조회 실패지만 진행"

    price = get_price("BTC")
    if price <= 0:
        return True, "BTC 현재가 조회 실패지만 진행"

    ma20 = float(df["close"].rolling(20).mean().iloc[-1])

    recent = df.tail(4)
    start = float(recent["close"].iloc[0])
    end = float(recent["close"].iloc[-1])
    drop_pct = ((end - start) / start) * 100 if start > 0 else 0

    if drop_pct <= BTC_CRASH_THRESHOLD:
        btc_lock_until = time.time() + BTC_CRASH_LOCK_MINUTES * 60
        return False, f"BTC 급락 감지 ({drop_pct:.2f}%), {BTC_CRASH_LOCK_MINUTES}분 잠금"

    if price < ma20 * 0.992:
        return False, "BTC가 최근 평균 아래라 분위기가 약함"

    return True, "시장 분위기 무난"

# =========================
# 알림 관련
# =========================
def cleanup_buttons():
    now_ts = time.time()
    delete_keys = []
    for k, v in button_data_store.items():
        if now_ts - v["created_at"] > BUTTON_EXPIRE:
            delete_keys.append(k)
    for k in delete_keys:
        button_data_store.pop(k, None)

def price_change_from_last_alert_pct(current_price, last_price):
    try:
        current_price = float(current_price)
        last_price = float(last_price)
        if last_price <= 0:
            return 999.0
        return abs((current_price - last_price) / last_price) * 100
    except Exception:
        return 999.0

def should_send_alert(signal, now_ts):
    ticker = signal["ticker"]

    if ticker not in recent_alerts:
        return True

    last_info = recent_alerts[ticker]
    last_status = last_info.get("status")
    last_time = last_info.get("time", 0)
    last_entry_score = last_info.get("entry_score", -999)
    last_entry_near = last_info.get("entry_near", False)
    last_grade = last_info.get("grade", GRADE_B)
    last_price = last_info.get("price", 0)

    price_delta = price_change_from_last_alert_pct(signal["current_price"], last_price)

    if signal["status"] != last_status:
        return True
    if signal["entry_near"] and not last_entry_near:
        return True
    if signal["entry_score"] > last_entry_score:
        return True
    if grade_rank(signal["grade"]) > grade_rank(last_grade):
        return True

    if price_delta < MIN_REALERT_PRICE_CHANGE:
        return False

    if now_ts - last_time >= SAME_STATUS_COOLDOWN:
        return True

    return False

# =========================
# 분석
# =========================
def analyze_coin(ticker):
    df = get_ohlcv(ticker)
    if df is None:
        return reject(ticker, "OHLCV 부족")

    current_price = get_price(ticker)
    if current_price <= 0:
        return reject(ticker, "현재가 조회 실패")

    recent20 = df.tail(20)

    support, resistance = calculate_support_resistance(df)

    ma5 = float(df["close"].rolling(5).mean().iloc[-1])
    ma10 = float(df["close"].rolling(10).mean().iloc[-1])
    rsi = float(calculate_rsi(df).iloc[-1])

    pump = detect_recent_pump(df)
    rebound = detect_rebound(df)
    recent_move_pct = detect_recent_move_pct(df, 5)
    uptrend_confirmed = is_uptrend_confirmed(df)
    recent_drop_block = recent_drop_too_strong(df)
    sideways_flat = is_sideways_too_flat(df)
    ma_slope_positive = is_ma_slope_positive(df)
    bad_flow = detect_bad_flow(df)

    vol_info = analyze_volume_quality(df)
    vol_ratio = float(vol_info["vol_ratio"])
    volume_recovery_ok = bool(vol_info["volume_recovery_ok"])
    volume_recovery_ratio = float(vol_info["volume_recovery_ratio"])
    volume_trend_ok = bool(vol_info["volume_trend_ok"])
    up_candle_vol_ok = bool(vol_info["up_candle_vol_ok"])

    if recent_move_pct < MIN_RECENT_MOVE_PCT:
        return reject(ticker, f"최근 움직임 너무 작음 ({recent_move_pct:.2f}%)")

    if recent_move_pct < MIN_PRICE_RANGE_PCT:
        return reject(ticker, f"변동폭 부족 ({recent_move_pct:.2f}%)")

    if sideways_flat:
        return reject(ticker, "너무 조용한 횡보장")

    if pump >= MAX_PUMP_REJECT_PCT:
        return reject(ticker, f"이미 너무 급등함 ({pump:.2f}%)")

    if support <= 0 or resistance <= 0 or resistance <= support:
        return reject(ticker, "지지/저항 계산 이상")

    signal_entry = support * 1.008
    signal_stop = min(signal_entry * 0.972, support * 0.978)

    score = 0
    entry_score = 0
    reasons = []
    warnings = []

    # 지지선 근처 여부
    if support * 0.992 <= current_price <= support * 1.030:
        score += 2
        entry_score += 2
        reasons.append("- 지지 구간 근처")
    elif support * 0.985 <= current_price <= support * 1.050:
        score += 1
        entry_score += 1
        reasons.append("- 지지 구간에서 아주 멀진 않음")
    else:
        warnings.append("- 지지 구간과 거리가 있음")

    # 이동평균
    if ma5 > ma10:
        score += 1
        entry_score += 1
        reasons.append("- 최근 흐름이 살아있음")
    else:
        warnings.append("- 최근 흐름이 아직 약함")

    if ma_slope_positive:
        entry_score += 1
        reasons.append("- 평균 흐름이 꺾이지 않음")
    else:
        warnings.append("- 평균 흐름 기울기가 약함")

    # RSI
    if 30 <= rsi <= 68:
        score += 1
        reasons.append(f"- RSI 무난 ({rsi:.2f})")
    elif rsi < 30:
        warnings.append(f"- 너무 약한 흐름일 수 있음 ({rsi:.2f})")
    else:
        warnings.append(f"- 이미 오른 구간일 수 있음 ({rsi:.2f})")

    # 거래량
    if vol_ratio >= 1.0:
        score += 1
        entry_score += 2
        reasons.append(f"- 거래량 강함 ({vol_ratio:.2f}배)")
    elif vol_ratio >= MIN_VOL_RATIO:
        score += 1
        entry_score += 1
        reasons.append(f"- 거래량 무난 ({vol_ratio:.2f}배)")
    else:
        return reject(ticker, f"거래량 부족 ({vol_ratio:.2f}배)")

    if volume_recovery_ok:
        entry_score += 1
        reasons.append(f"- 최근 거래량 회복 ({volume_recovery_ratio:.2f}배)")
    else:
        warnings.append(f"- 거래량 회복 약함 ({volume_recovery_ratio:.2f}배)")

    if volume_trend_ok:
        entry_score += 1
        reasons.append("- 마지막 거래량 흐름 유지")
    else:
        warnings.append("- 마지막 거래량이 약해짐")

    if up_candle_vol_ok:
        entry_score += 1
        reasons.append("- 오르는 봉에 거래량이 붙음")
    else:
        warnings.append("- 오르는 봉 거래량 힘이 약함")

    # 반등 / 상승 확인
    if rebound:
        entry_score += 2
        reasons.append("- 눌림 후 반등 확인")
    else:
        entry_score -= 1
        warnings.append("- 반등 힘이 아직 약함")

    if uptrend_confirmed:
        entry_score += 2
        reasons.append("- 상승 흐름 확인")
    else:
        entry_score -= 1
        warnings.append("- 상승 확인이 약함")

    # 나쁜 흐름
    if bad_flow:
        entry_score -= 2
        warnings.append("- 최근 흐름이 아래로 밀리는 편")

    # 이미 오른 코인 감점
    if pump >= 4.0:
        entry_score -= 2
        warnings.append(f"- 최근 이미 많이 오른 편 ({pump:.2f}%)")
    elif pump >= 2.5:
        entry_score -= 1
        warnings.append(f"- 최근 살짝 오른 편 ({pump:.2f}%)")
    else:
        reasons.append(f"- 과한 급등 추격 자리는 아님 ({pump:.2f}%)")

    # 급락 직후 감점
    if recent_drop_block:
        entry_score -= 3
        warnings.append("- 최근 급하게 빠져서 위험함")

    # 목표가 계산용 등급 힌트
    if (
        vol_ratio >= 1.0
        and rebound
        and volume_recovery_ok
        and uptrend_confirmed
        and up_candle_vol_ok
        and entry_score >= 5
        and ma5 > ma10
        and ma_slope_positive
    ):
        tp_grade_hint = "strong"
    elif (
        vol_ratio >= 0.55
        and rebound
        and uptrend_confirmed
        and entry_score >= 2
    ):
        tp_grade_hint = "mid"
    else:
        tp_grade_hint = "short"

    signal_tp, tp_type = calculate_tp(signal_entry, resistance, tp_grade_hint, current_price)

    entry_gap_pct = ((current_price - signal_entry) / signal_entry) * 100 if signal_entry > 0 else 999
    tp_gap_pct = ((signal_tp - current_price) / current_price) * 100 if current_price > 0 else -999
    resistance_gap_pct = ((resistance - current_price) / current_price) * 100 if current_price > 0 else -999
    entry_near = abs(entry_gap_pct) <= ENTRY_NEAR_RANGE

    if resistance_gap_pct < MIN_RESISTANCE_GAP:
        return reject(ticker, f"위 저항이 너무 가까움 ({resistance_gap_pct:.2f}%)")
    elif resistance_gap_pct < 1.1:
        entry_score -= 1
        warnings.append(f"- 위쪽 공간이 넉넉하진 않음 ({resistance_gap_pct:.2f}%)")
    else:
        reasons.append(f"- 위쪽 공간 있음 ({resistance_gap_pct:.2f}%)")

    if tp_type == "짧은 목표" and tp_gap_pct < 1.0:
        return reject(ticker, f"짧은 목표 여유 부족 ({tp_gap_pct:.2f}%)")
    elif tp_type == "보통 목표" and tp_gap_pct < 1.5:
        return reject(ticker, f"보통 목표 여유 부족 ({tp_gap_pct:.2f}%)")
    elif tp_type == "강한 목표" and tp_gap_pct < 2.3:
        return reject(ticker, f"강한 목표 여유 부족 ({tp_gap_pct:.2f}%)")

    if current_price >= signal_tp:
        return reject(ticker, "이미 목표가 근처임")

    qty = calc_order_qty(signal_entry)
    if qty <= 0:
        return reject(ticker, "주문 수량 계산 실패")

    risk = signal_entry - signal_stop
    reward = signal_tp - signal_entry
    rr = reward / risk if risk > 0 else 0
    if rr < 0.35:
        return reject(ticker, f"손익비 부족 ({rr:.2f})")

    if entry_score < -1:
        return reject(ticker, f"진입 점수 너무 낮음 ({entry_score})")

    # 등급 판정
    if (
        vol_ratio >= S_GRADE_MIN_VOL_RATIO
        and recent_move_pct >= S_GRADE_MIN_MOVE_PCT
        and rebound
        and volume_recovery_ok
        and uptrend_confirmed
        and up_candle_vol_ok
        and entry_score >= S_GRADE_MIN_ENTRY_SCORE
        and ma5 > ma10
        and ma_slope_positive
        and tp_type == "강한 목표"
    ):
        grade = GRADE_S
    elif tp_type in ["보통 목표", "강한 목표"] and entry_score >= 2:
        grade = GRADE_A
    else:
        grade = GRADE_B

    # 상태 판정
    if entry_gap_pct > ENTRY_LATE_MAX_GAP:
        status = STATUS_CHASE_BLOCK
    elif entry_gap_pct > ENTRY_OK_MAX_GAP:
        status = STATUS_WATCH
    else:
        if recent_drop_block:
            status = STATUS_WATCH
        elif USE_TREND_CONFIRM and not uptrend_confirmed:
            status = STATUS_WATCH
        elif not ma_slope_positive:
            status = STATUS_WATCH
        else:
            if (
                score >= 2
                and entry_score >= 2
                and vol_ratio >= MIN_STRONG_VOL_RATIO
                and tp_gap_pct >= 1.5
            ):
                status = STATUS_BUY_NOW
            elif score >= 1 and entry_score >= 0:
                status = STATUS_SCALP_OK
            else:
                status = STATUS_WATCH

    return {
        "ticker": ticker,
        "grade": grade,
        "status": status,

        "current_price": r(current_price),
        "support": r(support, 8),
        "resistance": r(resistance, 8),

        "signal_entry": r(signal_entry, 8),
        "signal_stop": r(signal_stop, 8),
        "signal_tp": r(signal_tp, 8),

        "tp_type": tp_type,
        "qty": qty,

        "score": score,
        "entry_score": entry_score,

        "entry_gap_pct": r(entry_gap_pct, 2),
        "tp_gap_pct": r(tp_gap_pct, 2),
        "resistance_gap_pct": r(resistance_gap_pct, 2),

        "entry_near": entry_near,
        "vol_ratio": r(vol_ratio, 4),
        "recent_move_pct": r(recent_move_pct, 2),

        "reason": "\n".join(reasons) if reasons else "- 없음",
        "warning": "\n".join(warnings) if warnings else "- 특별한 경고 없음",
    }

# =========================
# 포지션/주문
# =========================
def build_position_from_signal(signal, filled_entry, filled_qty):
    return {
        "ticker": signal["ticker"],
        "grade": signal["grade"],
        "status": signal["status"],
        "tp_type": signal["tp_type"],

        "signal_entry": signal["signal_entry"],
        "signal_stop": signal["signal_stop"],
        "signal_tp": signal["signal_tp"],

        "filled_entry": filled_entry,
        "qty": filled_qty,
        "peak_price": filled_entry,

        "trailing_armed": False,
        "trailing_stop_price": 0.0,
        "breakeven_armed": False,
    }

def wait_for_balance_change(ticker, before_balance, retry=5, delay=1):
    for _ in range(retry):
        time.sleep(delay)
        after_balance = get_balance(ticker)
        diff = round(after_balance - before_balance, 8)
        if diff > 0:
            return True, after_balance, diff
    return False, get_balance(ticker), 0.0

def wait_for_balance_reduce(ticker, before_balance, retry=5, delay=1):
    for _ in range(retry):
        time.sleep(delay)
        after_balance = get_balance(ticker)
        price_now = max(get_price(ticker), 0)
        if after_balance < before_balance * 0.2 or after_balance * price_now < MIN_ORDER:
            return True, after_balance
    return False, get_balance(ticker)

def buy_coin(signal, mode="AUTO"):
    ticker = signal["ticker"]

    try:
        before_balance = get_balance(ticker)
        before_price = get_price(ticker)

        result = bithumb.buy_market_order(ticker, signal["qty"])

        ok, after_balance, filled_qty = wait_for_balance_change(ticker, before_balance, retry=5, delay=1)
        if not ok or filled_qty <= 0:
            return False, f"매수 응답은 왔지만 실제 체결 수량 확인 실패 / 응답: {result}"

        after_price = get_price(ticker)
        if before_price > 0 and after_price > 0:
            filled_entry = round((before_price + after_price) / 2, 8)
        elif after_price > 0:
            filled_entry = after_price
        else:
            filled_entry = signal["current_price"]

        active_positions[ticker] = build_position_from_signal(signal, filled_entry, filled_qty)

        add_log({
            "time": int(time.time()),
            "type": "BUY",
            "ticker": ticker,
            "entry": filled_entry,
            "qty": filled_qty,
            "mode": mode,
            "grade": signal["grade"],
            "status": signal["status"],
            "tp_type": signal["tp_type"],
            "entry_score": signal["entry_score"],
            "score": signal["score"],
            "vol_ratio": signal["vol_ratio"],
            "recent_move_pct": signal["recent_move_pct"],
            "entry_gap_pct": signal["entry_gap_pct"],
            "tp_gap_pct": signal["tp_gap_pct"],
            "reason": signal["reason"],
            "warning": signal["warning"],
        })

        return True, {
            "qty": filled_qty,
            "filled_entry": filled_entry,
            "raw_result": result
        }

    except Exception as e:
        return False, str(e)

def sell_coin_confirmed(ticker, qty):
    try:
        before_balance = get_balance(ticker)
        result = bithumb.sell_market_order(ticker, qty)
        ok, after_balance = wait_for_balance_reduce(ticker, before_balance, retry=5, delay=1)
        return ok, result, before_balance, after_balance
    except Exception as e:
        return False, str(e), 0.0, 0.0

def can_buy_signal_now(signal):
    price = get_price(signal["ticker"])
    if price <= 0:
        return False, "현재가를 불러오지 못했어"

    if signal["status"] not in [STATUS_BUY_NOW, STATUS_SCALP_OK]:
        return False, "지금 진입 가능한 상태가 아니야"

    if price < signal["support"] * 0.995:
        return False, "지지 구간 아래로 밀려서 위험해"

    if price >= signal["signal_tp"]:
        return False, "이미 목표가 근처라 너무 늦었어"

    if price > signal["signal_entry"] * (1 + ENTRY_OK_MAX_GAP / 100):
        return False, "추천 진입가보다 너무 올라서 늦은 자리야"

    if price < signal["signal_entry"] * 0.972:
        return False, "추천받은 자리보다 너무 많이 내려왔어"

    tp_gap_pct = ((signal["signal_tp"] - price) / price) * 100 if price > 0 else 0
    if tp_gap_pct < MIN_TP_GAP:
        return False, "목표가가 너무 가까워"

    stop_gap_pct = ((signal["signal_entry"] - signal["signal_stop"]) / signal["signal_entry"]) * 100 if signal["signal_entry"] > 0 else 0
    if stop_gap_pct > 6.0:
        return False, "손절폭이 너무 커"

    return True, "OK"

def try_auto_buy(signal):
    ticker = signal["ticker"]

    if ticker in active_positions:
        return False, f"{ticker}는 이미 들고 있어"
    if active_positions:
        return False, "이미 다른 코인 보유 중이야"
    if not AUTO_BUY:
        return False, "자동매수가 꺼져 있어"
    if not is_weekday_auto_time():
        return False, "자동매수 시간대가 아니야"

    market_ok, market_msg = get_btc_market_state()
    if BTC_MARKET_FILTER and not market_ok:
        return False, f"시장 필터 차단: {market_msg}"

    if signal["status"] != STATUS_BUY_NOW:
        return False, "지금 바로 들어갈 상태는 아니야"
    if not signal["entry_near"]:
        return False, "진입가 근처가 아니야"

    # 자동매수는 A/S만
    if signal["grade"] == GRADE_B:
        return False, "B급은 자동매수 안 해"

    if signal["entry_score"] < 3:
        return False, "진입 점수가 아직 부족해"

    if signal["tp_gap_pct"] < 1.8:
        return False, "목표가 여유가 너무 적어"

    if signal["vol_ratio"] < 0.7:
        return False, "거래량이 자동매수하기엔 약해"

    ok, msg = can_buy_signal_now(signal)
    if not ok:
        return False, msg

    success, result = buy_coin(signal, mode="AUTO")
    if not success:
        return False, result

    send(
        f"""
✅ 자동매수 완료

📊 {ticker}

💰 실제 매수가: {fmt_price(result['filled_entry'])}
📦 체결수량: {result['qty']:.6f}

🎯 목표가: {fmt_price(signal['signal_tp'])}
🛑 손절가: {fmt_price(signal['signal_stop'])}

🏅 등급: {signal['grade']} / {signal['tp_type']}
"""
    )
    return True, "자동매수 완료"

# =========================
# 스캔
# =========================
def scan():
    cleanup_buttons()
    now_ts = time.time()

    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[티커 조회 오류] {e}")
        return

    if not tickers:
        return

    market_ok, market_msg = get_btc_market_state()

    current_holding = None
    current_ticker = None
    if active_positions:
        current_ticker, current_holding = list(active_positions.items())[0]

    signals = []
    checked = 0

    for t in tickers[:TOP_TICKERS]:
        if t == "BTC":
            continue
        checked += 1
        signal = analyze_coin(t)
        if signal and signal["status"] in [STATUS_BUY_NOW, STATUS_SCALP_OK]:
            signals.append(signal)

    if DEBUG_SCAN:
        print(f"[SCAN] 검사 수: {checked} / 통과 수: {len(signals)} / 보유중: {list(active_positions.keys())}")

    if not signals:
        print("조건 맞는 진입 가능 코인 없음")
        return

    signals.sort(
        key=lambda x: (
            status_rank(x["status"]),
            1 if x["entry_near"] else 0,
            grade_rank(x["grade"]),
            x["entry_score"],
            x["tp_gap_pct"],
            x["recent_move_pct"],
            x["score"]
        ),
        reverse=True
    )

    signal = signals[0]
    prev = recent_alerts.get(signal["ticker"], {})

    if DEBUG_SCAN:
        print(
            f"[SCAN TOP] {signal['ticker']} / {signal['status']} / grade={signal['grade']} "
            f"/ entry_score={signal['entry_score']} / tp_gap={signal['tp_gap_pct']}%"
        )

    # 보유 중 정책
    if current_holding:
        holding_grade = current_holding.get("grade", GRADE_B)
        new_grade = signal.get("grade", GRADE_B)

        if grade_rank(holding_grade) >= grade_rank(GRADE_S):
            print("[SCAN] S급 보유 중이라 다른 신호 무시")
            return

        if (
            grade_rank(holding_grade) == grade_rank(GRADE_A)
            and grade_rank(new_grade) == grade_rank(GRADE_S)
            and signal["status"] == STATUS_BUY_NOW
        ):
            if should_send_alert(signal, now_ts):
                bid = str(uuid.uuid4())[:8]
                button_data_store[bid] = {
                    "signal": signal,
                    "created_at": now_ts,
                    "switch_from": current_ticker
                }

                keyboard = [[InlineKeyboardButton("🔁 A 팔고 S로 갈아타기", callback_data=f"SWITCH|{bid}")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                recent_alerts[signal["ticker"]] = {
                    "time": now_ts,
                    "status": signal["status"],
                    "entry_score": signal["entry_score"],
                    "entry_near": signal["entry_near"],
                    "tp_type": signal["tp_type"],
                    "grade": signal["grade"],
                    "price": signal["current_price"]
                }

                msg = f"""
🚨 S급 우선 기회

현재 보유: {current_ticker} ({holding_grade})
새 후보: {signal['ticker']} ({new_grade})

💰 현재가: {fmt_price(signal['current_price'])}
🎯 추천 진입: {fmt_price(signal['signal_entry'])}
🛑 손절: {fmt_price(signal['signal_stop'])}
🚀 목표가: {fmt_price(signal['signal_tp'])}

🏅 등급: {signal['grade']} / {signal['tp_type']}
📈 진입 위치: {fmt_pct(signal['entry_gap_pct'])}
📈 목표 여유: {fmt_pct(signal['tp_gap_pct'])}

👍 진입 근거
{signal['reason']}

⚠️ 주의
{signal['warning']}
"""
                send(msg, reply_markup)
            return

        return

    # 자동매수
    if (
        signal["entry_near"]
        and signal["status"] == STATUS_BUY_NOW
        and AUTO_BUY
        and is_weekday_auto_time()
        and (not BTC_MARKET_FILTER or market_ok)
    ):
        success, msg = try_auto_buy(signal)
        if success:
            recent_alerts[signal["ticker"]] = {
                "time": now_ts,
                "status": "AUTO_BOUGHT",
                "entry_score": signal["entry_score"],
                "entry_near": True,
                "tp_type": signal["tp_type"],
                "grade": signal["grade"],
                "price": signal["current_price"]
            }
            return
        else:
            print(f"[자동매수 실패 또는 미실행] {signal['ticker']} / {msg}")

    if not should_send_alert(signal, now_ts):
        print(f"[알림 스킵] {signal['ticker']} / 같은 상태 반복")
        return

    change_reason = ""
    if prev:
        if signal["entry_score"] > prev.get("entry_score", 0):
            change_reason += "\n📈 진입 점수 상승"
        if signal["entry_near"] and not prev.get("entry_near", False):
            change_reason += "\n🎯 진입가 근접"
        if signal["tp_type"] != prev.get("tp_type", ""):
            change_reason += f"\n🚀 목표 유형 변화: {signal['tp_type']}"
        if grade_rank(signal["grade"]) > grade_rank(prev.get("grade", GRADE_B)):
            change_reason += f"\n🏅 등급 상승: {signal['grade']}"
        price_delta = price_change_from_last_alert_pct(signal["current_price"], prev.get("price", 0))
        if price_delta >= MIN_REALERT_PRICE_CHANGE:
            change_reason += f"\n📊 가격 변화: {price_delta:.2f}%"

    recent_alerts[signal["ticker"]] = {
        "time": now_ts,
        "status": signal["status"],
        "entry_score": signal["entry_score"],
        "entry_near": signal["entry_near"],
        "tp_type": signal["tp_type"],
        "grade": signal["grade"],
        "price": signal["current_price"]
    }

    reply_markup = None
    if signal["status"] == STATUS_BUY_NOW:
        bid = str(uuid.uuid4())[:8]
        button_data_store[bid] = {"signal": signal, "created_at": now_ts}
        keyboard = [[InlineKeyboardButton("🔥 매수", callback_data=f"BUY|{bid}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

    if signal["grade"] == GRADE_S and signal["entry_near"] and signal["status"] == STATUS_BUY_NOW:
        status_text = "🚨 S급 기회 (진입가 근접)"
    elif signal["entry_near"] and signal["status"] == STATUS_BUY_NOW:
        status_text = "🚨 지금 진입 타이밍"
    else:
        status_text = status_label(signal["status"])

    extra_text = ""
    if signal["status"] == STATUS_BUY_NOW:
        if AUTO_BUY:
            if BTC_MARKET_FILTER and not market_ok:
                extra_text = f"\n자동매수: 시장 필터로 보류 ({market_msg})"
            elif signal["entry_near"]:
                extra_text = "\n자동매수: 진입가 근처라 실행 후보"
            else:
                extra_text = "\n자동매수: 현재는 살짝 거리 있음"

    msg = f"""
📊 {signal['ticker']}

{status_text}{extra_text}{change_reason}

━━━━━━━━━━━━━━━
💰 현재가: {fmt_price(signal['current_price'])}
🎯 추천 진입: {fmt_price(signal['signal_entry'])}
🛑 손절: {fmt_price(signal['signal_stop'])}
🚀 목표가: {fmt_price(signal['signal_tp'])}
━━━━━━━━━━━━━━━

📌 상태: {status_label(signal['status'])}
🏅 등급: {signal['grade']} / {signal['tp_type']}

📈 진입 위치: {fmt_pct(signal['entry_gap_pct'])}
📈 목표 여유: {fmt_pct(signal['tp_gap_pct'])}

━━━━━━━━━━━━━━━
👍 진입 근거
{signal['reason']}

⚠️ 주의
{signal['warning']}
"""
    send(msg, reply_markup)

# =========================
# 버튼 처리
# =========================
def handle(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()

    try:
        action, bid = query.data.split("|")
    except Exception:
        send("버튼 정보가 이상해")
        return

    item = button_data_store.get(bid)
    if not item:
        send("만료된 신호")
        return

    signal = item["signal"]
    ticker = signal["ticker"]

    if action == "BUY":
        if ticker in active_positions:
            send(f"{ticker}는 이미 들고 있어")
            return
        if active_positions:
            send("이미 다른 코인 보유 중이야")
            return

        ok, msg = can_buy_signal_now(signal)
        if not ok:
            send(f"🛡️ 매수 취소: {msg}")
            return

        market_ok, market_msg = get_btc_market_state()
        if BTC_MARKET_FILTER and not market_ok:
            send(f"🛡️ 매수 취소: 시장 필터 차단 ({market_msg})")
            return

        success, result = buy_coin(signal, mode="MANUAL")
        if not success:
            send(f"❌ 매수 실패\n{result}")
            return

        send(
            f"""
✅ 매수 완료

📊 {ticker}

💰 실제 매수가: {fmt_price(result['filled_entry'])}
📦 수량: {result['qty']:.6f}

🎯 목표가: {fmt_price(signal['signal_tp'])}
🛑 손절가: {fmt_price(signal['signal_stop'])}

🏅 등급: {signal['grade']} / {signal['tp_type']}
"""
        )
        return

    elif action == "SWITCH":
        switch_from = item.get("switch_from")

        if not switch_from or switch_from not in active_positions:
            send("갈아탈 기존 포지션을 찾지 못했어")
            return

        current_position = active_positions[switch_from]
        balance = get_balance(switch_from)

        if balance <= 0:
            active_positions.pop(switch_from, None)
            send("기존 포지션 잔고가 없어서 갈아타기 취소")
            return

        market_ok, market_msg = get_btc_market_state()
        if BTC_MARKET_FILTER and not market_ok:
            send(f"🛡️ 갈아타기 취소: 시장 필터 차단 ({market_msg})")
            return

        ok, msg = can_buy_signal_now(signal)
        if not ok:
            send(f"🛡️ 새 코인 진입 취소: {msg}")
            return

        try:
            current_entry = float(current_position.get("filled_entry", current_position["signal_entry"]))
            current_price = get_price(switch_from)
            current_pnl = ((current_price - current_entry) / current_entry * 100) if current_entry > 0 else 0

            sell_ok, sell_result, _, _ = sell_coin_confirmed(switch_from, balance)
            if not sell_ok:
                send(f"❌ 기존 코인 매도 확인 실패\n{sell_result}")
                return

            add_log({
                "time": int(time.time()),
                "type": "SWITCH_OUT",
                "ticker": switch_from,
                "entry": current_entry,
                "exit": current_price,
                "pnl_pct": round(current_pnl, 2)
            })

            active_positions.pop(switch_from, None)

            success, result = buy_coin(signal, mode="SWITCH")
            if not success:
                send(f"❌ 갈아타기 중 새 코인 체결 확인 실패\n{result}")
                return

            add_log({
                "time": int(time.time()),
                "type": "SWITCH_IN",
                "ticker": ticker,
                "entry": result["filled_entry"],
                "qty": result["qty"]
            })

            send(
                f"""
🔁 갈아타기 완료

{switch_from} → {ticker}

💰 실제 매수가: {fmt_price(result['filled_entry'])}
📦 수량: {result['qty']:.6f}

🎯 목표가: {fmt_price(signal['signal_tp'])}
🛑 손절가: {fmt_price(signal['signal_stop'])}

🏅 새 등급: {signal['grade']} / {signal['tp_type']}
"""
            )
        except Exception as e:
            send(f"❌ 갈아타기 실패\n{e}")
        return

    else:
        send("알 수 없는 버튼이야")

# =========================
# 요약 명령
# =========================
def summary_command(update: Update, context: CallbackContext):
    send(get_trade_summary_text())

# =========================
# 포지션 감시
# =========================
def monitor():
    remove = []

    for ticker, position in list(active_positions.items()):
        try:
            price = get_price(ticker)
            if price <= 0:
                continue

            balance = get_balance(ticker)
            if balance <= 0 or balance * price < MIN_ORDER:
                remove.append(ticker)
                continue

            filled_entry = float(position.get("filled_entry", position["signal_entry"]))
            pnl = ((price - filled_entry) / filled_entry) * 100 if filled_entry > 0 else 0

            if price > position.get("peak_price", filled_entry):
                position["peak_price"] = price

            # 손절
            if price <= position["signal_stop"]:
                sell_ok, sell_result, _, _ = sell_coin_confirmed(ticker, balance)
                if sell_ok:
                    add_log({
                        "time": int(time.time()),
                        "type": "STOP",
                        "ticker": ticker,
                        "entry": filled_entry,
                        "exit": price,
                        "pnl_pct": round(pnl, 2)
                    })
                    send(
                        f"""
🚨 손절

📊 {ticker}

💰 현재가: {fmt_price(price)}
📉 손실률: {fmt_pct(pnl)}
"""
                    )
                    remove.append(ticker)
                else:
                    print(f"[매도 확인 실패-손절] {ticker} / {sell_result}")
                continue

            # 본절 보호
            if pnl >= BREAKEVEN_TRIGGER:
                position["breakeven_armed"] = True

            if position.get("breakeven_armed", False):
                breakeven_price = filled_entry * (1 + BREAKEVEN_MARGIN / 100)
                if price <= breakeven_price:
                    sell_ok, sell_result, _, _ = sell_coin_confirmed(ticker, balance)
                    if sell_ok:
                        add_log({
                            "time": int(time.time()),
                            "type": "BREAKEVEN",
                            "ticker": ticker,
                            "entry": filled_entry,
                            "exit": price,
                            "pnl_pct": round(pnl, 2)
                        })
                        send(
                            f"""
🛡️ 본절 처리

📊 {ticker}

💰 현재가: {fmt_price(price)}
📊 결과: 거의 손익 없음
"""
                        )
                        remove.append(ticker)
                    else:
                        print(f"[매도 확인 실패-본절] {ticker} / {sell_result}")
                    continue

            # 트레일링
            if USE_TRAILING_TP and position.get("grade") == GRADE_S and position.get("tp_type") == "강한 목표":
                if pnl >= TRAILING_ARM_PCT:
                    position["trailing_armed"] = True

                if position.get("trailing_armed", False):
                    position["trailing_stop_price"] = position["peak_price"] * (1 - TRAILING_GIVEBACK_PCT / 100)

                    if price <= position.get("trailing_stop_price", 0):
                        sell_ok, sell_result, _, _ = sell_coin_confirmed(ticker, balance)
                        if sell_ok:
                            add_log({
                                "time": int(time.time()),
                                "type": "TRAIL_TP",
                                "ticker": ticker,
                                "entry": filled_entry,
                                "exit": price,
                                "pnl_pct": round(pnl, 2)
                            })
                            send(
                                f"""
📈 트레일링 익절

📊 {ticker}

💰 현재가: {fmt_price(price)}
📈 수익률: {fmt_pct(pnl)}

(고점 찍고 내려와서 자동 익절)
"""
                            )
                            remove.append(ticker)
                        else:
                            print(f"[매도 확인 실패-트레일링] {ticker} / {sell_result}")
                        continue

            # 일반 익절
            elif price >= position["signal_tp"]:
                sell_ok, sell_result, _, _ = sell_coin_confirmed(ticker, balance)
                if sell_ok:
                    add_log({
                        "time": int(time.time()),
                        "type": "TP",
                        "ticker": ticker,
                        "entry": filled_entry,
                        "exit": price,
                        "pnl_pct": round(pnl, 2)
                    })
                    send(
                        f"""
🎉 익절 완료

📊 {ticker}

💰 현재가: {fmt_price(price)}
📈 수익률: {fmt_pct(pnl)}

🏅 등급: {position['grade']}
"""
                    )
                    remove.append(ticker)
                else:
                    print(f"[매도 확인 실패-익절] {ticker} / {sell_result}")
                continue

        except Exception as e:
            print(f"[감시 오류] {ticker} / {e}")
            traceback.print_exc()

    for t in remove:
        active_positions.pop(t, None)

# =========================
# 실행
# =========================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CallbackQueryHandler(handle))
dispatcher.add_handler(CommandHandler("summary", summary_command))

updater.start_polling(drop_pending_updates=True)

print(f"🚀 완성형 수정 버전 실행 / 기준시간대: {TIMEZONE}")
print(f"🚀 자동매수 24시간 모드: {AUTO_BUY_24H}")
print(f"🚀 스캔 주기: {SCAN_INTERVAL}초 / 포지션 감시: {POSITION_CHECK_INTERVAL}초")

last_scan_time = 0
last_position_check = 0

while True:
    now_ts = time.time()

    if now_ts - last_scan_time >= SCAN_INTERVAL:
        try:
            scan()
        except Exception as e:
            print(f"[스캔 오류] {e}")
            traceback.print_exc()
        last_scan_time = now_ts

    if now_ts - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            monitor()
        except Exception as e:
            print(f"[감시 오류] {e}")
            traceback.print_exc()
        last_position_check = now_ts

    time.sleep(LOOP_SLEEP)
