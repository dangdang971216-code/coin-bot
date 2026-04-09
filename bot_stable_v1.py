
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

BOT_VERSION = "수익형 v6.5.9-stable1"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY or not API_SECRET:
    raise ValueError("환경변수(API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID) 중 비어 있는 값이 있어요.")

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

TIMEZONE = "Asia/Seoul"
LOG_FILE = "trade_log.json"
POSITIONS_FILE = "active_positions.json"
PENDING_SELLS_FILE = "pending_sells.json"
PENDING_BUYS_FILE = "pending_buy_candidates.json"

LOOP_SLEEP = 1
TOP_TICKERS = 50
UNIVERSE_REFRESH_SEC = 20
MARKET_CACHE_TTL_SEC = 10

MIN_ORDER_KRW = 5000
DUST_KEEP_MIN_KRW = 100
MAX_ENTRY_KRW = 10000
KRW_USE_RATIO = 0.88
ORDER_BUFFER_KRW = 500
MARKET_BUY_SLIPPAGE = 1.05
RETRY_KRW_LEVELS = [1.0, 0.94, 0.88]

AUTO_BUY = True
AUTO_BUY_START_HOUR = 0
AUTO_BUY_END_HOUR = 24
MAX_HOLDINGS = 1
ALLOW_CHASE = False

BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -2.0
BTC_STRONG_BLOCK_PCT = -2.8
BTC_MA_FILTER = True
REGIME_FILTER_ON = True
REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS = True
REGIME_SIDEWAYS_MAX_ABS_PCT = 0.50
REGIME_WEAK_MAX_ABS_PCT = -0.70
REGIME_STRONG_UP_PCT = 1.0

BASE_STOP_LOSS_PCT = -1.7
BASE_TP_PCT = 4.6
TRAIL_START_PCT = 2.8
TRAIL_BACKOFF_PCT = 1.15
BREAKEVEN_TRIGGER_PCT = 1.7
BREAKEVEN_BUFFER_PCT = 0.15
TIME_STOP_MIN_SEC = 480
TIME_STOP_MAX_SEC = 1080
MIN_EXPECTED_EDGE_SCORE = 7.4
MIN_EXPECTED_TP_PCT = 3.2

TRADE_TIER_S_ALLOWED = {"EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT"}
TRADE_TIER_S_EDGE_MIN = 9.8
TRADE_TIER_S_VOL_MIN = 2.8
TRADE_TIER_S_BTC_OK = {"NORMAL", "STRONG_UP", "SIDEWAYS"}
TRADE_TIER_A_EDGE_MIN = 8.4
TRADE_TIER_A_VOL_MIN = 2.0
S_TP_BONUS_PCT = 0.45
S_TIME_STOP_BONUS_SEC = 150
S_BREAKEVEN_TRIGGER_PCT = 2.1
S_BREAKEVEN_BUFFER_PCT = 0.20
S_TRAIL_START_PCT = 3.2
S_TRAIL_BACKOFF_PCT = 1.05
A_TP_BONUS_PCT = 0.20
A_TIME_STOP_BONUS_SEC = 60
A_BREAKEVEN_TRIGGER_PCT = 1.9
A_BREAKEVEN_BUFFER_PCT = 0.18
A_TRAIL_START_PCT = 3.0
A_TRAIL_BACKOFF_PCT = 1.10

EARLY_CFG = {"change_min": 0.80, "change_max": 2.20, "vol_min": 2.80, "rsi_min": 46, "rsi_max": 61, "range_min": 0.90, "jump_min": 0.10}
PREPUMP_CFG = {"change_min": 1.00, "change_max": 4.40, "vol_min": 2.00, "rsi_min": 48, "rsi_max": 70, "range_min": 1.10}
PULLBACK_CFG = {"pump_min": 6.0, "rebound_min": 0.95, "vol_min": 1.6}
PRE_BREAKOUT_CFG = {"change_max": 1.05, "vol_min": 2.50, "rsi_min": 48, "rsi_max": 66, "range_min": 0.90, "gap_max": 0.32, "jump_min": 0.12}
BREAKOUT_CFG = {"change_min": 0.80, "change_max": 3.00, "vol_min": 3.20, "rsi_min": 52, "rsi_max": 72, "range_min": 1.00, "break_min": 0.12, "jump_min": 0.25}
CHASE_CFG = {"change_min": 1.80, "change_max": 3.40, "vol_min": 4.00, "rsi_min": 55, "rsi_max": 72, "range_min": 1.60, "jump_min": 0.35}
TREND_CONT_CFG = {"trend_change_min": 2.40, "trend_change_max": 8.80, "pullback_max": 2.00, "recovery_min": 0.35, "recovery_max": 1.25, "last1_change_max": 0.55, "vol_reaccel_min": 1.60, "rsi_min": 50, "rsi_max": 70, "high_retest_gap_min": 0.18, "high_retest_gap_max": 0.65, "extension_max": 0.08}

LEADER_BASE_MIN_FOR_PRIORITY = 4.8
LEADER_HIGH_MIN = 6.2
LEADER_STRONG_MIN = 7.2
LEADER_REPEAT_SEEN_BONUS = 0.45
LEADER_MAX_REPEAT_BONUS = 1.35
LEADER_SIDEWAYS_EDGE_DISCOUNT = 0.45
LEADER_WEAK_EDGE_DISCOUNT = 0.25

PENDING_BUY_ON = True
PENDING_BUY_MAX_ITEMS = 6
PENDING_BUY_TTL_SEC = 165
PENDING_BUY_MIN_EDGE = 4.2
PENDING_BUY_MIN_SCORE = 3.8
PENDING_BUY_RECHECK_MIN_SEC = 26

WATCH_RENOTICE_SEC = 360
WATCH_VOL_IMPROVE_DELTA = 0.18
WATCH_CHANGE_IMPROVE_DELTA = 0.16
WATCH_SCORE_IMPROVE_DELTA = 0.30
WATCH_RENOTICE_MAX_PER_TICKER = 5

AUTO_PAUSE_ON = True
AUTO_PAUSE_STREAK_COUNT = 3
AUTO_PAUSE_SECONDS = 1200
AUTO_PAUSE_LOOKBACK_SEC = 3600
AUTO_PAUSE_ROLLING_PNL = -2.2

BTC_REPORT_INTERVAL = 3600
STATUS_REPORT_INTERVAL = 1800
last_btc_report_time = 0
last_status_report_time = 0

active_positions = {}
recent_signal_alerts = {}
recent_watch_alerts = {}
recent_watch_snapshots = {}
recent_watch_renotice_counts = {}
pending_sells = {}
pending_buy_candidates = {}
recent_leader_board = {}
paused_until = 0
pause_reason = ""
auto_pause_bypass_until = 0
auto_pause_reset_ignore_before = 0
shared_market_cache = {}
shared_market_cache_time = 0
market_universe = []
market_universe_time = 0

def now_kst():
    return datetime.now(ZoneInfo(TIMEZONE))

def is_auto_time():
    return AUTO_BUY_START_HOUR <= now_kst().hour < AUTO_BUY_END_HOUR

def r(x, n=4):
    try: return round(float(x), n)
    except Exception: return 0.0

def safe_float(v, default=0.0):
    try: return float(v)
    except Exception: return default

def fmt_price(x):
    try: x = float(x)
    except Exception: return "0"
    if x >= 1000: return f"{x:,.0f}"
    if x >= 100: return f"{x:,.1f}"
    if x >= 1: return f"{x:,.2f}"
    if x >= 0.1: return f"{x:,.3f}"
    if x >= 0.01: return f"{x:,.4f}"
    return f"{x:,.6f}"

def fmt_pct(x):
    try: return f"{float(x):.2f}%"
    except Exception: return "0.00%"

def send(msg: str):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip())
    except Exception as e:
        print(f"[텔레그램 오류] {e}")

def send_startup_message():
    send(f"""
✅ 봇 시작 완료

버전: {BOT_VERSION}
자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}
1차 안정본이라 큰 오류 방지 위주로 정리했어.
""")

def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[JSON 저장 오류] {path} / {e}")

trade_logs = load_json_file(LOG_FILE, [])

def save_logs():
    save_json_file(LOG_FILE, trade_logs)

def add_log(item: dict):
    trade_logs.append(item)
    save_logs()

def save_positions():
    save_json_file(POSITIONS_FILE, active_positions)

def load_positions():
    global active_positions
    active_positions = load_json_file(POSITIONS_FILE, {})

def clear_position_file_if_empty():
    if not active_positions and os.path.exists(POSITIONS_FILE):
        try: os.remove(POSITIONS_FILE)
        except Exception: pass

def save_pending_sells():
    save_json_file(PENDING_SELLS_FILE, pending_sells)

def load_pending_sells():
    global pending_sells
    pending_sells = load_json_file(PENDING_SELLS_FILE, {})

def save_pending_buys():
    save_json_file(PENDING_BUYS_FILE, pending_buy_candidates)

def load_pending_buys():
    global pending_buy_candidates
    pending_buy_candidates = load_json_file(PENDING_BUYS_FILE, {})

def get_price(ticker: str) -> float:
    try:
        p = pybithumb.get_current_price(ticker)
        return -1 if p is None else float(p)
    except Exception:
        return -1

def get_ohlcv(ticker: str, interval: str = "minute3"):
    try:
        return pybithumb.get_ohlcv(ticker, interval=interval)
    except Exception:
        return None

def get_orderbook_best_ask(ticker: str) -> float:
    try:
        ob = pybithumb.get_orderbook(ticker)
        if not ob: return -1
        if isinstance(ob, dict) and "asks" in ob and ob["asks"]:
            return float(ob["asks"][0].get("price", -1))
        if isinstance(ob, dict) and "data" in ob and "asks" in ob["data"] and ob["data"]["asks"]:
            return float(ob["data"]["asks"][0].get("price", -1))
        return -1
    except Exception:
        return -1

def get_balance(ticker: str) -> float:
    try:
        bal = bithumb.get_balance(ticker)
        return float(bal[0] or 0)
    except Exception:
        return 0.0

def get_krw_balance():
    try:
        bal = bithumb.get_balance("BTC")
        if not bal or len(bal) < 4: return 0.0
        return float(bal[2] or 0)
    except Exception:
        return 0.0

def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_rsi(df, period=14):
    try: return float(calculate_rsi(df, period).iloc[-1])
    except Exception: return 50.0

def ma(df, period):
    try: return float(df["close"].rolling(period).mean().iloc[-1])
    except Exception: return 0.0

def get_vol_ratio(df, short_n=5, long_n=20):
    try:
        if len(df) < short_n + long_n: return 0.0
        recent = float(df["volume"].tail(short_n).mean())
        prev = float(df["volume"].tail(short_n + long_n).head(long_n).mean())
        return 0.0 if prev <= 0 else recent / prev
    except Exception:
        return 0.0

def get_recent_change_pct(df, n=5):
    try:
        recent = df.tail(n)
        start = float(recent["close"].iloc[0]); end = float(recent["close"].iloc[-1])
        return 0.0 if start <= 0 else ((end - start) / start) * 100
    except Exception:
        return 0.0

def get_range_pct(df, n=10):
    try:
        recent = df.tail(n)
        hi = float(recent["high"].max()); lo = float(recent["low"].min())
        return 0.0 if lo <= 0 else ((hi - lo) / lo) * 100
    except Exception:
        return 0.0

def short_trend_up(df):
    try:
        closes = list(df.tail(3)["close"])
        return len(closes) == 3 and closes[-1] >= closes[-2] >= closes[-3]
    except Exception:
        return False

def candle_body_ratio(df):
    try:
        last = df.iloc[-1]
        o, c, h, l = map(float, [last["open"], last["close"], last["high"], last["low"]])
        return 0.0 if h <= l else abs(c - o) / (h - l)
    except Exception:
        return 0.0

def upper_wick_too_large(df):
    try:
        last = df.iloc[-1]
        body_top = max(float(last["open"]), float(last["close"]))
        high = float(last["high"]); low = float(last["low"])
        return False if high <= low else (high - body_top) / (high - low) >= 0.48
    except Exception:
        return False

def get_upper_wick_ratio(df):
    try:
        last = df.iloc[-1]
        body_top = max(float(last["open"]), float(last["close"]))
        high = float(last["high"]); low = float(last["low"])
        return 0.0 if high <= low else (high - body_top) / (high - low)
    except Exception:
        return 0.0

def get_recent_high(df, n=8, exclude_last=True):
    try:
        recent = df.tail(n)
        if exclude_last: recent = recent.iloc[:-1]
        return 0.0 if len(recent) <= 0 else float(recent["high"].max())
    except Exception:
        return 0.0

def get_recent_low(df, n=8, exclude_last=False):
    try:
        recent = df.tail(n)
        if exclude_last: recent = recent.iloc[:-1]
        return 0.0 if len(recent) <= 0 else float(recent["low"].min())
    except Exception:
        return 0.0

def detect_higher_lows(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12: return False
        p1, p2, p3 = recent.iloc[0:4], recent.iloc[4:8], recent.iloc[8:12]
        return float(p1["low"].min()) < float(p2["low"].min()) < float(p3["low"].min())
    except Exception:
        return False

def detect_box_top_compression(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12: return False
        highs = list(recent["high"].tail(6)); lows = list(recent["low"].tail(6))
        current_price = float(recent["close"].iloc[-1])
        hi_max, hi_min = max(highs), min(highs)
        low_early, low_late = min(lows[:3]), min(lows[3:])
        high_band_pct = ((hi_max - hi_min) / hi_min) * 100 if hi_min > 0 else 999
        dist_from_top_pct = ((hi_max - current_price) / current_price) * 100 if current_price > 0 else 999
        return high_band_pct <= 0.45 and low_late >= low_early and 0 <= dist_from_top_pct <= 0.35
    except Exception:
        return False

def detect_big_bull_half_hold(df, bars=10):
    try:
        recent = df.tail(bars)
        if len(recent) < 8: return False
        best_idx, best_body = None, 0.0
        for idx, row in recent.iloc[:-1].iterrows():
            o, c = float(row["open"]), float(row["close"])
            if c <= o: continue
            body = c - o
            if body > best_body: best_body, best_idx = body, idx
        if best_idx is None: return False
        bull = recent.loc[best_idx]
        o, c = float(bull["open"]), float(bull["close"])
        half_level = o + (c - o) * 0.5
        after = recent.loc[best_idx:]
        return float(recent["close"].iloc[-1]) >= half_level and float(after["low"].min()) >= half_level * 0.998
    except Exception:
        return False

def detect_fake_breakdown_recovery(df, bars=10):
    try:
        recent = df.tail(bars)
        if len(recent) < 8: return False
        support_zone = float(recent.iloc[:-2]["low"].tail(5).min())
        prev, last = recent.iloc[-2], recent.iloc[-1]
        return float(prev["low"]) < support_zone * 0.998 and float(last["close"]) >= support_zone * 1.001
    except Exception:
        return False

def analyze_chart_patterns(df):
    tags = []; bonus = 0.0
    hl = detect_higher_lows(df)
    bc = detect_box_top_compression(df)
    bh = detect_big_bull_half_hold(df)
    fb = detect_fake_breakdown_recovery(df)
    if hl: tags.append("저점높임")
    if bc: tags.append("상단압축")
    if bh: tags.append("양봉절반유지")
    if fb: tags.append("가짜하락회복"); bonus += 0.45
    return {"patterns": {"higher_lows": hl, "box_compression": bc, "big_bull_half_hold": bh, "fake_breakdown_recovery": fb}, "tags": tags, "bonus_score": bonus}

def has_higher_lows(df): return detect_higher_lows(df)
def big_bull_half_hold(df): return detect_big_bull_half_hold(df)
def fake_breakdown_recovery(df): return detect_fake_breakdown_recovery(df)

def passes_core_pattern_filter(strategy, pattern_info):
    p = pattern_info["patterns"]
    hl = p.get("higher_lows", False); bc = p.get("box_compression", False); bh = p.get("big_bull_half_hold", False)
    if strategy == "EARLY": return hl or bh
    if strategy == "PRE_BREAKOUT": return bc and (hl or bh)
    if strategy == "BREAKOUT": return bc or bh
    if strategy == "CHASE": return bc or bh
    return True

def pattern_bonus_score(pattern_info):
    bonus = pattern_info.get("bonus_score", 0.0)
    p = pattern_info["patterns"]
    if p.get("higher_lows", False): bonus += 0.55
    if p.get("box_compression", False): bonus += 0.60
    if p.get("big_bull_half_hold", False): bonus += 0.65
    return bonus

def pattern_reason_suffix(pattern_info):
    tags = pattern_info.get("tags", [])
    return "" if not tags else "\n- 차트 구조: " + ", ".join(tags)

def get_btc_df():
    for interval in ["minute60", "minute30", "minute10"]:
        df = get_ohlcv("BTC", interval)
        if df is not None and len(df) >= 20: return df, interval
    df = get_ohlcv("BTC", "day")
    if df is not None and len(df) >= 20: return df, "day"
    return None, None

def get_btc_market_state():
    df, _ = get_btc_df()
    if df is None: return True, "BTC 조회 실패지만 진행", 0.0
    price = get_price("BTC")
    if price <= 0: return True, "BTC 현재가 조회 실패지만 진행", 0.0
    recent_drop_pct = get_recent_change_pct(df, 4); ma20 = ma(df, 20)
    if recent_drop_pct <= BTC_STRONG_BLOCK_PCT: return False, f"BTC가 크게 빠지는 중이야 ({recent_drop_pct:.2f}%)", recent_drop_pct
    if recent_drop_pct <= BTC_CRASH_BLOCK_PCT: return False, f"BTC가 약해서 신규 진입을 쉬고 있어 ({recent_drop_pct:.2f}%)", recent_drop_pct
    if BTC_MA_FILTER and ma20 > 0 and price < ma20 * 0.992: return False, "BTC가 약한 자리라 신규 진입을 쉬고 있어", recent_drop_pct
    return True, "시장 분위기 무난", recent_drop_pct

def get_market_regime():
    df, label = get_btc_df()
    if df is None: return {"name": "UNKNOWN", "label": label or "unknown", "allow_auto_buy": True, "allow_breakout": True, "message": "시장 상태 판단 실패"}
    price = get_price("BTC")
    if price <= 0: return {"name": "UNKNOWN", "label": label or "unknown", "allow_auto_buy": True, "allow_breakout": True, "message": "시장 상태 판단 실패"}
    change_pct = get_recent_change_pct(df, 4); ma5v, ma20v = ma(df, 5), ma(df, 20)
    if change_pct <= BTC_STRONG_BLOCK_PCT: return {"name": "BLOCK", "label": label, "allow_auto_buy": False, "allow_breakout": False, "message": "BTC 급락 구간"}
    if change_pct <= REGIME_WEAK_MAX_ABS_PCT or (ma20v > 0 and price < ma20v * 0.992): return {"name": "WEAK", "label": label, "allow_auto_buy": True, "allow_breakout": False, "message": "약한 장"}
    if abs(change_pct) <= REGIME_SIDEWAYS_MAX_ABS_PCT: return {"name": "SIDEWAYS", "label": label, "allow_auto_buy": True, "allow_breakout": not REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS, "message": "횡보 장"}
    if change_pct >= REGIME_STRONG_UP_PCT and price >= ma5v and price >= ma20v: return {"name": "STRONG_UP", "label": label, "allow_auto_buy": True, "allow_breakout": True, "message": "강한 상승 장"}
    return {"name": "NORMAL", "label": label, "allow_auto_buy": True, "allow_breakout": True, "message": "무난한 장"}

def analyze_btc_flow():
    regime = get_market_regime()
    df, label = get_btc_df()
    if df is None: return "📊 BTC 리포트\nBTC 데이터를 불러오지 못했어."
    price = get_price("BTC")
    if price <= 0: return "📊 BTC 리포트\nBTC 현재가 조회 실패."
    change_pct = get_recent_change_pct(df, 4)
    return f"📊 BTC 리포트 ({label} 기준)\n\n💰 현재가: {fmt_price(price)}\n📉 최근 변동률: {change_pct:.2f}%\n📌 상태: {regime['message']}"

def dynamic_stop_loss_pct(vol_ratio, range_pct, strategy):
    stop = BASE_STOP_LOSS_PCT
    if strategy in ["PRE_BREAKOUT", "BREAKOUT", "CHASE"]: stop -= 0.05
    elif strategy == "TREND_CONT": stop -= 0.02
    elif strategy == "EARLY": stop += 0.10
    if vol_ratio >= 4.0: stop -= 0.05
    if range_pct >= 4.0: stop -= 0.05
    return max(stop, -2.2)

def dynamic_take_profit_pct(vol_ratio, range_pct, strategy):
    tp = BASE_TP_PCT
    if strategy == "EARLY": tp += 0.5
    elif strategy == "PRE_BREAKOUT": tp += 0.8
    elif strategy == "TREND_CONT": tp += 0.9
    elif strategy == "BREAKOUT": tp += 0.7
    elif strategy == "CHASE": tp += 0.6
    if vol_ratio >= 4.0: tp += 0.3
    if range_pct >= 3.5: tp += 0.2
    return min(tp, 6.8)

def dynamic_time_stop_sec(vol_ratio, range_pct, strategy):
    base = {"EARLY": 900, "PRE_BREAKOUT": 930, "TREND_CONT": 840, "BREAKOUT": 780, "CHASE": 660}.get(strategy, 900)
    if vol_ratio >= 4.0: base -= 60
    elif vol_ratio < 2.0: base += 60
    return max(TIME_STOP_MIN_SEC, min(base, TIME_STOP_MAX_SEC))

def score_heat_penalty(rsi):
    if rsi >= 74: return 1.3
    if rsi >= 70: return 0.9
    if rsi >= 66: return 0.5
    return 0.0

def score_entry_bonus(strategy):
    return {"EARLY": 1.45, "PRE_BREAKOUT": 1.30, "TREND_CONT": 1.05, "BREAKOUT": 0.45, "CHASE": -0.95, "PREPUMP": 0.35}.get(strategy, 0.0)

def expected_edge_score(strategy, base_signal_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct):
    risk = abs(float(stop_loss_pct)); reward = float(take_profit_pct)
    if risk <= 0: return -999
    rr = reward / risk
    score = float(base_signal_score) + min(rr, 3.8) * 0.95 + min(vol_ratio, 4.5) * 0.32 + min(range_pct, 3.5) * 0.18 + score_entry_bonus(strategy) - score_heat_penalty(rsi)
    if strategy in ["EARLY", "PRE_BREAKOUT"] and change_pct <= 1.8: score += 0.45
    if strategy == "TREND_CONT" and 0.3 <= change_pct <= 2.0: score += 0.35
    if strategy == "BREAKOUT" and change_pct >= 2.6: score -= 0.55
    if strategy == "CHASE" and change_pct >= 2.8: score -= 0.8
    return round(score, 2)

def classify_trade_tier(signal, regime=None):
    strategy = signal.get("strategy", ""); edge = safe_float(signal.get("edge_score", 0)); vol = safe_float(signal.get("vol_ratio", 0)); rsi = safe_float(signal.get("rsi", 0)); tags = set(signal.get("pattern_tags", [])); regime_name = (regime or {}).get("name", "NORMAL"); leader_score = safe_float(signal.get("leader_score", 0))
    if strategy in TRADE_TIER_S_ALLOWED and edge >= TRADE_TIER_S_EDGE_MIN and vol >= TRADE_TIER_S_VOL_MIN and rsi <= 66 and regime_name in TRADE_TIER_S_BTC_OK and (tags.intersection({"저점높임", "양봉절반유지"}) or strategy == "TREND_CONT") and leader_score >= LEADER_HIGH_MIN: return "S"
    if edge >= TRADE_TIER_A_EDGE_MIN and vol >= TRADE_TIER_A_VOL_MIN and (leader_score >= LEADER_BASE_MIN_FOR_PRIORITY or strategy in ["TREND_CONT", "PRE_BREAKOUT"]): return "A"
    return "B"

def apply_trade_tier_adjustments(signal, regime=None):
    tier = classify_trade_tier(signal, regime=regime); signal["trade_tier"] = tier
    tp = safe_float(signal.get("take_profit_pct", BASE_TP_PCT)); ts = int(safe_float(signal.get("time_stop_sec", TIME_STOP_MAX_SEC)))
    if tier == "S":
        signal["take_profit_pct"] = round(min(tp + S_TP_BONUS_PCT, 7.2), 2); signal["time_stop_sec"] = int(min(ts + S_TIME_STOP_BONUS_SEC, TIME_STOP_MAX_SEC + 240)); signal["breakeven_trigger_pct"] = S_BREAKEVEN_TRIGGER_PCT; signal["breakeven_buffer_pct"] = S_BREAKEVEN_BUFFER_PCT; signal["trail_start_pct"] = S_TRAIL_START_PCT; signal["trail_backoff_pct"] = S_TRAIL_BACKOFF_PCT
    elif tier == "A":
        signal["take_profit_pct"] = round(min(tp + A_TP_BONUS_PCT, 6.9), 2); signal["time_stop_sec"] = int(min(ts + A_TIME_STOP_BONUS_SEC, TIME_STOP_MAX_SEC + 120)); signal["breakeven_trigger_pct"] = A_BREAKEVEN_TRIGGER_PCT; signal["breakeven_buffer_pct"] = A_BREAKEVEN_BUFFER_PCT; signal["trail_start_pct"] = A_TRAIL_START_PCT; signal["trail_backoff_pct"] = A_TRAIL_BACKOFF_PCT
    else:
        signal["breakeven_trigger_pct"] = BREAKEVEN_TRIGGER_PCT; signal["breakeven_buffer_pct"] = BREAKEVEN_BUFFER_PCT; signal["trail_start_pct"] = TRAIL_START_PCT; signal["trail_backoff_pct"] = TRAIL_BACKOFF_PCT
    return signal

def signal_priority_value(signal):
    tier_bonus = {"S": 1.15, "A": 0.35, "B": 0.0}.get(signal.get("trade_tier", "B"), 0.0)
    leader_bonus = min(safe_float(signal.get("leader_score", 0)), 8.0) * 0.22
    return float(signal.get("edge_score", signal.get("signal_score", 0))) + tier_bonus + leader_bonus

def base_signal(**kwargs):
    return {"ticker": kwargs["ticker"], "strategy": kwargs["strategy"], "strategy_label": kwargs["strategy_label"], "current_price": kwargs["current_price"], "vol_ratio": r(kwargs.get("vol_ratio", 0), 2), "change_pct": r(kwargs.get("change_pct", 0), 2), "rsi": r(kwargs.get("rsi", 0), 2), "range_pct": r(kwargs.get("range_pct", 0), 2), "signal_score": r(kwargs.get("signal_score", 0), 2), "edge_score": r(kwargs.get("edge_score", 0), 2), "leader_score": r(kwargs.get("leader_score", 0), 2), "stop_loss_pct": kwargs.get("stop_loss_pct", 0), "take_profit_pct": kwargs.get("take_profit_pct", 0), "time_stop_sec": kwargs.get("time_stop_sec", 0), "invalid_break_level": kwargs.get("invalid_break_level", 0.0), "pattern_tags": kwargs.get("pattern_tags", []), "reference_high": kwargs.get("reference_high", 0.0), "reference_low": kwargs.get("reference_low", 0.0), "reason": kwargs.get("reason", ""), "trade_tier": "B"}

def make_signal(**kwargs):
    if "signal_score" not in kwargs and "score" in kwargs:
        kwargs["signal_score"] = kwargs["score"]
    if "edge_score" not in kwargs and "signal_score" in kwargs:
        kwargs["edge_score"] = kwargs["signal_score"]
    return base_signal(**kwargs)

def update_recent_leader_board(cache):
    global recent_leader_board
    board = {}
    ranked = sorted(cache.items(), key=lambda kv: safe_float(kv[1].get("leader_score", 0)), reverse=True)[:15]
    now_ts = time.time()
    for rank, (ticker, data) in enumerate(ranked, start=1):
        board[ticker] = {"rank": rank, "leader_score": safe_float(data.get("leader_score", 0)), "time": now_ts}
    recent_leader_board = board

def get_pending_seen_count(ticker: str):
    return int(pending_buy_candidates.get(ticker, {}).get("seen_count", 0))

def update_scan_debug_snapshot(shared_cache):
    return

def get_market_universe(force=False):
    global market_universe, market_universe_time
    now_ts = time.time()
    if (not force) and market_universe and (now_ts - market_universe_time < UNIVERSE_REFRESH_SEC):
        return market_universe
    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print(f"[티커 조회 오류] {e}")
        return market_universe
    rows = []
    for ticker in tickers:
        if ticker == "BTC": continue
        try:
            df3 = get_ohlcv(ticker, "minute3")
            if df3 is None or len(df3) < 30: continue
            price = safe_float(df3["close"].iloc[-1], 0)
            if price <= 0: continue
            vol_ratio = get_vol_ratio(df3, 3, 15)
            short_change = get_recent_change_pct(df3, 3)
            range_pct = get_range_pct(df3, 8)
            turnover_recent = float(df3["volume"].tail(5).sum()) * price
            surge_score = min(vol_ratio, 7.0) * 1.8 + min(max(short_change, 0), 4.5) * 1.4 + min(range_pct, 5.0) * 0.7
            rows.append({"ticker": ticker, "df_3m": df3, "last_price": price, "turnover_recent": turnover_recent, "surge_score": surge_score})
        except Exception:
            continue
    if not rows: return market_universe
    rows.sort(key=lambda x: (x["turnover_recent"], x["surge_score"]), reverse=True)
    selected = rows[:TOP_TICKERS]
    for idx, item in enumerate(selected, start=1):
        item["turnover_rank"] = idx
        item["surge_rank"] = idx
        item["leader_score"] = round(min(item["surge_score"], 8.0) * 0.18 + (1.0 if idx <= 5 else 0.5 if idx <= 15 else 0.2), 2)
    market_universe = selected
    market_universe_time = now_ts
    return market_universe

def build_shared_market_cache(force=False):
    global shared_market_cache, shared_market_cache_time
    now_ts = time.time()
    if (not force) and shared_market_cache and (now_ts - shared_market_cache_time < MARKET_CACHE_TTL_SEC):
        return shared_market_cache
    universe = get_market_universe(force=force)
    if not universe: return shared_market_cache
    cache = {}
    for row in universe:
        ticker = row["ticker"]
        try:
            df1 = get_ohlcv(ticker, "minute1")
            if df1 is None or len(df1) < 35: continue
            price = safe_float(df1["close"].iloc[-1], 0) or row["last_price"]
            if price <= 0: continue
            vol_ratio_1m = get_vol_ratio(df1, 3, 15)
            cache[ticker] = {"price": price, "df_1m": df1, "df_3m": row["df_3m"], "turnover": row["turnover_recent"], "surge_score": row["surge_score"], "leader_score": row.get("leader_score", 0), "turnover_rank": row.get("turnover_rank", 999), "surge_rank": row.get("surge_rank", 999), "change_1": get_recent_change_pct(df1, 2), "change_3": get_recent_change_pct(df1, 3), "change_5": get_recent_change_pct(df1, 5), "vol_ratio": vol_ratio_1m, "vol_ratio_1m": vol_ratio_1m, "range_pct_1m": get_range_pct(df1, 10), "rsi": get_rsi(df1), "rsi_1m": get_rsi(df1)}
        except Exception:
            continue
    if cache:
        shared_market_cache = cache
        shared_market_cache_time = now_ts
        update_recent_leader_board(cache)
    return shared_market_cache

def entry_shape_block(strategy, df):
    if df is None or len(df) < 8: return False, ""
    if strategy in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "PREPUMP"]:
        if get_recent_change_pct(df, 3) >= 2.2 or get_recent_change_pct(df, 5) >= 3.2: return True, "최근 봉이 너무 과열"
        if candle_body_ratio(df) < 0.42 and get_upper_wick_ratio(df) >= 0.52: return True, "봉 마감 힘이 약해"
    return False, ""

LOSS_STREAK_TYPES = {"STOP", "EARLY_FAIL", "SCENARIO_FAIL"}
CLOSE_TYPES = {"TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP", "SCENARIO_FAIL", "EARLY_FAIL"}

def get_recent_closed_logs_window(sec=AUTO_PAUSE_LOOKBACK_SEC):
    now_ts = int(time.time())
    return [x for x in trade_logs if x.get("type") in CLOSE_TYPES and int(x.get("time", 0)) >= int(auto_pause_reset_ignore_before or 0) and now_ts - int(x.get("time", 0)) <= sec]

def get_recent_loss_streak():
    streak = 0; ignore_before = int(auto_pause_reset_ignore_before or 0)
    for x in reversed(trade_logs):
        if x.get("type") not in CLOSE_TYPES: continue
        if int(x.get("time", 0)) < ignore_before: break
        if x.get("type") in LOSS_STREAK_TYPES and float(x.get("pnl_pct", 0)) < 0: streak += 1
        else: break
        if streak >= AUTO_PAUSE_STREAK_COUNT: break
    return streak

def activate_auto_pause(reason):
    global paused_until, pause_reason
    paused_until = int(time.time()) + AUTO_PAUSE_SECONDS; pause_reason = reason

def clear_auto_pause_if_needed():
    global paused_until, pause_reason
    if paused_until and time.time() >= paused_until:
        paused_until = 0; pause_reason = ""

def reset_auto_pause_state(bypass_sec=900):
    global paused_until, pause_reason, auto_pause_bypass_until, auto_pause_reset_ignore_before
    now_ts = int(time.time())
    paused_until = 0; pause_reason = ""; auto_pause_bypass_until = now_ts + max(int(bypass_sec), 0); auto_pause_reset_ignore_before = now_ts

def should_pause_auto_buy_now():
    clear_auto_pause_if_needed()
    if auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time()); return False, f"수동 해제 적용 중 / 자동 쉬기 재판정까지 {max(remain, 0)}초"
    if not AUTO_PAUSE_ON: return False, ""
    if paused_until and time.time() < paused_until:
        remain = int(paused_until - time.time()); return True, f"{max(remain, 0)}초 남음 / {pause_reason}"
    streak = get_recent_loss_streak()
    if streak >= AUTO_PAUSE_STREAK_COUNT:
        reason = f"최근 연속 손실 {streak}회"; activate_auto_pause(reason); return True, f"{AUTO_PAUSE_SECONDS}초 쉬기 / {reason}"
    recent_logs = get_recent_closed_logs_window(AUTO_PAUSE_LOOKBACK_SEC)
    if len(recent_logs) >= 3:
        rolling_pnl = sum(float(x.get("pnl_pct", 0)) for x in recent_logs)
        if rolling_pnl <= AUTO_PAUSE_ROLLING_PNL:
            reason = f"최근 1시간 누적 {rolling_pnl:.2f}%"; activate_auto_pause(reason); return True, f"{AUTO_PAUSE_SECONDS}초 쉬기 / {reason}"
    return False, ""

def late_entry_block(strategy, df, current_price):
    if df is None or len(df) < 10 or current_price <= 0: return True, "가격 데이터 부족"
    recent_high = get_recent_high(df, 8, exclude_last=True)
    if recent_high <= 0: return False, ""
    dist_to_high_pct = ((recent_high - current_price) / current_price) * 100
    change_5 = get_recent_change_pct(df, 5); change_3 = get_recent_change_pct(df, 3)
    if strategy in ["BREAKOUT", "CHASE"] and 0 <= dist_to_high_pct <= 0.22 and change_3 >= 1.0: return True, f"너무 고점 바로 밑이라 늦은 진입 ({dist_to_high_pct:.2f}%)"
    if change_5 > 3.1: return True, f"최근 5봉 상승이 너무 커서 늦은 진입 ({change_5:.2f}%)"
    if change_3 > 2.2 and strategy in ["BREAKOUT", "CHASE"]: return True, f"최근 3봉 상승이 너무 커서 늦은 진입 ({change_3:.2f}%)"
    if strategy == "BREAKOUT":
        extension_pct = ((current_price - recent_high) / recent_high) * 100
        if extension_pct > 0.38: return True, f"돌파 후 이미 너무 위 ({extension_pct:.2f}%)"
    return False, ""

def build_signal_common(ticker, strategy, label, current_price, df, vol_ratio, change_pct, rsi, range_pct, base_score, invalid_break_level=0.0, reference_high=0.0, reference_low=0.0, reason=""):
    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, strategy)
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, strategy)
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, strategy)
    edge_score = expected_edge_score(strategy, base_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)
    return base_signal(ticker=ticker, strategy=strategy, strategy_label=label, current_price=current_price, vol_ratio=vol_ratio, change_pct=change_pct, rsi=rsi, range_pct=range_pct, signal_score=base_score, edge_score=edge_score, leader_score=min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS), stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct, time_stop_sec=time_stop_sec, invalid_break_level=invalid_break_level, pattern_tags=analyze_chart_patterns(df)["tags"], reference_high=reference_high, reference_low=reference_low, reason=reason)

def analyze_early_entry(ticker, data):
    df = data["df_1m"]; current_price = data["price"]
    if df is None or len(df) < 30 or current_price <= 0: return None
    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("EARLY", pattern_info): return None
    change_pct = get_recent_change_pct(df, 5); vol_ratio = get_vol_ratio(df, 3, 15); rsi = get_rsi(df); ma5v, ma10v = ma(df, 5), ma(df, 10); range_pct = get_range_pct(df, 8)
    if vol_ratio < EARLY_CFG["vol_min"] or change_pct < EARLY_CFG["change_min"] or change_pct > EARLY_CFG["change_max"] or rsi < EARLY_CFG["rsi_min"] or rsi > EARLY_CFG["rsi_max"] or range_pct < EARLY_CFG["range_min"] or ma5v <= 0 or ma10v <= 0 or ma5v < ma10v or upper_wick_too_large(df) or candle_body_ratio(df) < 0.48 or not short_trend_up(df): return None
    shape_block, _ = entry_shape_block("EARLY", df)
    if shape_block: return None
    block, _ = late_entry_block("EARLY", df, current_price)
    if block: return None
    last_close = float(df["close"].iloc[-1]); prev_close = float(df["close"].iloc[-2]); prev2_close = float(df["close"].iloc[-3]) if len(df) >= 3 else prev_close
    if prev_close <= 0 or prev2_close <= 0: return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100; last2_change_pct = ((last_close - prev2_close) / prev2_close) * 100
    if last_jump_pct < EARLY_CFG["jump_min"] or last_jump_pct > 0.78 or last2_change_pct > 1.30 or get_upper_wick_ratio(df) > 0.36: return None
    base_score = 5.8 + min(vol_ratio, 4.2) * 0.86 + min(change_pct, 2.2) * 0.75 + min(data.get("surge_score", 0), 8) * 0.16 + pattern_bonus_score(pattern_info)
    sig = build_signal_common(ticker, "EARLY", "초반 선점형", current_price, df, vol_ratio, change_pct, rsi, range_pct, base_score, 0.0, get_recent_high(df, 8, True), get_recent_low(df, 8, False), f"- 이제 막 오르기 시작했어{pattern_reason_suffix(pattern_info)}")
    sig["leader_score"] += safe_float(data.get("leader_score", 0))
    return sig

def analyze_pre_breakout_entry(ticker, data):
    df = data["df_1m"]; current_price = data["price"]
    if df is None or len(df) < 35 or current_price <= 0: return None
    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("PRE_BREAKOUT", pattern_info): return None
    change_pct = get_recent_change_pct(df, 5); vol_ratio = get_vol_ratio(df, 3, 15); rsi = get_rsi(df); ma5v, ma10v = ma(df, 5), ma(df, 10); range_pct = get_range_pct(df, 8); body_ratio = candle_body_ratio(df)
    if change_pct < 0 or change_pct > PRE_BREAKOUT_CFG["change_max"] or vol_ratio < PRE_BREAKOUT_CFG["vol_min"] or rsi < PRE_BREAKOUT_CFG["rsi_min"] or rsi > PRE_BREAKOUT_CFG["rsi_max"] or range_pct < PRE_BREAKOUT_CFG["range_min"] or ma5v <= 0 or ma10v <= 0 or ma5v < ma10v or upper_wick_too_large(df) or body_ratio < 0.40: return None
    shape_block, _ = entry_shape_block("PRE_BREAKOUT", df)
    if shape_block: return None
    block, _ = late_entry_block("PRE_BREAKOUT", df, current_price)
    if block: return None
    recent_high = get_recent_high(df, 8, True)
    if recent_high <= 0: return None
    gap_to_break = ((recent_high - current_price) / current_price) * 100
    if gap_to_break < 0 or gap_to_break > PRE_BREAKOUT_CFG["gap_max"]: return None
    last_close = float(df["close"].iloc[-1]); prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0: return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if last_jump_pct < PRE_BREAKOUT_CFG["jump_min"]: return None
    base_score = 6.1 + min(vol_ratio, 4.5) * 0.85 + (PRE_BREAKOUT_CFG["gap_max"] - gap_to_break) * 3.4 + min(data.get("surge_score", 0), 8) * 0.16 + pattern_bonus_score(pattern_info)
    sig = build_signal_common(ticker, "PRE_BREAKOUT", "쏘기 직전형", current_price, df, vol_ratio, change_pct, rsi, range_pct, base_score, recent_high, recent_high, get_recent_low(df, 8, False), f"- 거의 돌파 직전이야{pattern_reason_suffix(pattern_info)}")
    sig["leader_score"] += safe_float(data.get("leader_score", 0))
    return sig

def analyze_breakout_entry(ticker, data):
    df = data["df_1m"]; current_price = data["price"]
    if df is None or len(df) < 40 or current_price <= 0: return None
    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("BREAKOUT", pattern_info): return None
    change_pct = get_recent_change_pct(df, 5); vol_ratio = get_vol_ratio(df, 3, 15); rsi = get_rsi(df); ma5v, ma10v = ma(df, 5), ma(df, 10); range_pct = get_range_pct(df, 8); body_ratio = candle_body_ratio(df)
    last_close = float(df["close"].iloc[-1]); prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0: return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if change_pct < BREAKOUT_CFG["change_min"] or change_pct > BREAKOUT_CFG["change_max"] or vol_ratio < BREAKOUT_CFG["vol_min"] or rsi < BREAKOUT_CFG["rsi_min"] or rsi > BREAKOUT_CFG["rsi_max"] or range_pct < BREAKOUT_CFG["range_min"] or ma5v <= 0 or ma10v <= 0 or ma5v < ma10v or body_ratio < 0.5 or last_jump_pct < BREAKOUT_CFG["jump_min"] or upper_wick_too_large(df): return None
    recent_high = get_recent_high(df, 10, True)
    if recent_high <= 0: return None
    break_pct = ((current_price - recent_high) / recent_high) * 100
    if break_pct < BREAKOUT_CFG["break_min"]: return None
    block, _ = late_entry_block("BREAKOUT", df, current_price)
    if block: return None
    base_score = 6.0 + min(vol_ratio, 5.0) * 0.80 + min(change_pct, 3.4) * 0.65 + min(break_pct, 0.7) * 1.25 + min(data.get("surge_score", 0), 8) * 0.12 + pattern_bonus_score(pattern_info)
    sig = build_signal_common(ticker, "BREAKOUT", "급등 돌파형", current_price, df, vol_ratio, change_pct, rsi, range_pct, base_score, recent_high, recent_high, get_recent_low(df, 8, False), f"- 힘 있게 고점 돌파 중이야{pattern_reason_suffix(pattern_info)}")
    sig["leader_score"] += safe_float(data.get("leader_score", 0))
    return sig

def analyze_trend_cont_entry(ticker, data):
    df = data["df_1m"]; current_price = data["price"]
    if df is None or len(df) < 40 or current_price <= 0: return None
    pattern_info = analyze_chart_patterns(df)
    rsi = get_rsi(df); vol_ratio = get_vol_ratio(df, 3, 15); ma5v, ma10v = ma(df, 5), ma(df, 10)
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v: return None
    recent20 = df.tail(20); rise_low = float(recent20["low"].min()); rise_high = float(recent20["high"].max())
    if rise_low <= 0 or rise_high <= rise_low: return None
    trend_change_pct = ((rise_high - rise_low) / rise_low) * 100
    if trend_change_pct < TREND_CONT_CFG["trend_change_min"] or trend_change_pct > TREND_CONT_CFG["trend_change_max"]: return None
    recent9 = df.tail(9); prior8 = recent9.iloc[:-1] if len(recent9) >= 2 else recent9
    recent_high = float(prior8["high"].max()); recent_low = float(prior8["low"].min())
    if recent_high <= 0 or recent_low <= 0: return None
    pullback_pct = ((recent_high - current_price) / recent_high) * 100
    if pullback_pct < 0 or pullback_pct > TREND_CONT_CFG["pullback_max"]: return None
    recovery_pct = get_recent_change_pct(df, 3)
    if recovery_pct < TREND_CONT_CFG["recovery_min"] or recovery_pct > TREND_CONT_CFG["recovery_max"]: return None
    if get_recent_change_pct(df, 1) > TREND_CONT_CFG["last1_change_max"]: return None
    retest_gap_pct = ((recent_high - current_price) / current_price) * 100
    if retest_gap_pct < TREND_CONT_CFG["high_retest_gap_min"] or retest_gap_pct > TREND_CONT_CFG["high_retest_gap_max"]: return None
    extension_pct = ((current_price - recent_high) / recent_high) * 100
    if extension_pct > TREND_CONT_CFG["extension_max"] or rsi < TREND_CONT_CFG["rsi_min"] or rsi > TREND_CONT_CFG["rsi_max"] or vol_ratio < TREND_CONT_CFG["vol_reaccel_min"] or upper_wick_too_large(df): return None
    range_pct = get_range_pct(df, 10)
    base_score = 6.0 + min(vol_ratio, 4.2) * 0.75 + min(trend_change_pct, 7.0) * 0.28 + min(recovery_pct, 1.2) * 0.8 + pattern_bonus_score(pattern_info)
    sig = build_signal_common(ticker, "TREND_CONT", "추세 지속형", current_price, df, vol_ratio, recovery_pct, rsi, range_pct, base_score, recent_low, recent_high, recent_low, f"- 강한 흐름 뒤 눌림 자리야{pattern_reason_suffix(pattern_info)}")
    sig["leader_score"] += safe_float(data.get("leader_score", 0))
    return sig

def analyze_prepump_entry(ticker, data):
    df = data["df_3m"]; current_price = data["price"]
    if df is None or len(df) < 30 or current_price <= 0: return None
    rsi = get_rsi(df); vol_ratio = get_vol_ratio(df, 5, 20); change_pct = get_recent_change_pct(df, 5); range_pct = get_range_pct(df, 10)
    if change_pct < PREPUMP_CFG["change_min"] or change_pct > PREPUMP_CFG["change_max"] or vol_ratio < PREPUMP_CFG["vol_min"] or rsi < PREPUMP_CFG["rsi_min"] or rsi > PREPUMP_CFG["rsi_max"] or range_pct < PREPUMP_CFG["range_min"]: return None
    score = 5.1 + min(vol_ratio, 4.0) * 0.8 + min(data.get("surge_score", 0), 8) * 0.14
    sig = base_signal(ticker=ticker, strategy="PREPUMP", strategy_label="상승 시작형", current_price=current_price, vol_ratio=vol_ratio, change_pct=change_pct, rsi=rsi, range_pct=range_pct, signal_score=score, edge_score=score, leader_score=safe_float(data.get("leader_score", 0)))
    return sig

def analyze_pullback_entry(ticker, data):
    df = data["df_3m"]; current_price = data["price"]
    if df is None or len(df) < 35 or current_price <= 0: return None
    recent15 = df.tail(15); recent_low = float(recent15["low"].min()); recent_high = float(recent15["high"].max())
    if recent_low <= 0: return None
    total_pump_pct = ((recent_high - recent_low) / recent_low) * 100
    if total_pump_pct < PULLBACK_CFG["pump_min"]: return None
    last3 = df.tail(3); c1, c2, c3 = map(float, [last3["close"].iloc[0], last3["close"].iloc[1], last3["close"].iloc[2]])
    if not (c2 < c1 and c3 > c2): return None
    rebound_pct = ((c3 - c2) / c2) * 100 if c2 > 0 else 0.0
    vol_ratio = get_vol_ratio(df, 3, 10)
    if rebound_pct < PULLBACK_CFG["rebound_min"] or vol_ratio < PULLBACK_CFG["vol_min"]: return None
    score = 4.8 + vol_ratio * 0.8
    return base_signal(ticker=ticker, strategy="PULLBACK", strategy_label="눌림 반등형", current_price=current_price, vol_ratio=vol_ratio, change_pct=rebound_pct, signal_score=score, edge_score=score, leader_score=safe_float(data.get("leader_score", 0)))

def should_auto_buy_signal(signal, regime=None):
    strategy = signal["strategy"]; edge = float(signal.get("edge_score", 0)); tp = float(signal.get("take_profit_pct", 0))
    if strategy not in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "CHASE"]: return False
    if strategy == "CHASE" and not ALLOW_CHASE: return False
    if tp < MIN_EXPECTED_TP_PCT: return False
    required_edge = MIN_EXPECTED_EDGE_SCORE; tier = signal.get("trade_tier", "B"); leader_score = safe_float(signal.get("leader_score", 0))
    if regime:
        if not strategy_allowed_in_regime(strategy, regime): return False
        if regime.get("name") == "SIDEWAYS":
            required_edge += 0.9
            if strategy == "EARLY": required_edge += 0.7
            if tier == "B": required_edge += 0.4
            if leader_score >= LEADER_HIGH_MIN: required_edge -= LEADER_SIDEWAYS_EDGE_DISCOUNT
        elif regime.get("name") == "WEAK":
            required_edge += 1.1
            if strategy in ["EARLY", "TREND_CONT"]: required_edge += 0.5
            if tier != "S": required_edge += 0.3
            if leader_score >= LEADER_STRONG_MIN: required_edge -= LEADER_WEAK_EDGE_DISCOUNT
    if edge < required_edge: return False
    return True

def strategy_allowed_in_regime(strategy, regime):
    if regime["name"] == "BLOCK": return False
    if strategy == "BREAKOUT" and not regime.get("allow_breakout", True): return False
    if strategy == "TREND_CONT" and regime["name"] == "WEAK": return False
    if strategy == "CHASE" and regime["name"] in ["SIDEWAYS", "WEAK"]: return False
    return True

def watch_strategy_allowed_in_regime(strategy, regime):
    if regime["name"] == "BLOCK": return False
    if strategy == "CHASE": return False
    if regime["name"] == "WEAK" and strategy == "BREAKOUT": return False
    return True

def candidate_eligible_for_store(signal, regime):
    strategy = signal["strategy"]
    if strategy in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "CHASE"]:
        if strategy == "CHASE" and not ALLOW_CHASE: return False
        if not strategy_allowed_in_regime(strategy, regime): return False
        edge = safe_float(signal.get("edge_score", 0)); score = safe_float(signal.get("signal_score", 0))
        return not (edge < PENDING_BUY_MIN_EDGE and score < PENDING_BUY_MIN_SCORE)
    if strategy == "PREPUMP":
        edge = safe_float(signal.get("edge_score", 0)); score = safe_float(signal.get("signal_score", 0)); vol = safe_float(signal.get("vol_ratio", 0))
        return not (edge < 5.8 and score < 5.4) and vol >= 2.8
    return False

def add_or_refresh_pending_buy_candidate(signal, regime):
    if not candidate_eligible_for_store(signal, regime): return
    ticker = signal["ticker"]; now_ts = time.time()
    data = {"ticker": ticker, "strategy": signal["strategy"], "strategy_label": signal["strategy_label"], "created_at": now_ts, "last_seen_at": now_ts, "first_price": safe_float(signal.get("current_price", 0)), "edge_score": safe_float(signal.get("edge_score", 0)), "signal_score": safe_float(signal.get("signal_score", 0)), "leader_score": safe_float(signal.get("leader_score", 0)), "reference_high": safe_float(signal.get("reference_high", 0)), "reference_low": safe_float(signal.get("reference_low", 0)), "pattern_tags": signal.get("pattern_tags", []), "seen_count": 1}
    if ticker in pending_buy_candidates:
        old = pending_buy_candidates[ticker]
        data["created_at"] = safe_float(old.get("created_at", now_ts))
        data["edge_score"] = max(data["edge_score"], safe_float(old.get("edge_score", 0)))
        data["leader_score"] = max(data["leader_score"], safe_float(old.get("leader_score", 0)))
        data["seen_count"] = int(old.get("seen_count", 0)) + 1
    pending_buy_candidates[ticker] = data
    cleanup_pending_buy_candidates(); save_pending_buys()

def update_pending_buy_candidates_from_results(results, regime):
    for signal in results:
        if not should_auto_buy_signal(signal, regime=regime):
            add_or_refresh_pending_buy_candidate(signal, regime)

def cooldown_ok(ticker):
    return (time.time() - recent_signal_alerts.get(ticker, 0)) >= 300

def get_safe_use_krw(multiplier=1.0):
    krw = get_krw_balance()
    use_krw = min(krw * KRW_USE_RATIO, MAX_ENTRY_KRW)
    use_krw = (use_krw - ORDER_BUFFER_KRW) * multiplier
    return max(use_krw, 0)

def calc_order_qty(ticker, entry_price, multiplier=1.0):
    if entry_price <= 0: return 0.0, 0.0, 0.0
    best_ask = get_orderbook_best_ask(ticker)
    if best_ask <= 0: best_ask = entry_price
    safe_price = max(entry_price, best_ask) * MARKET_BUY_SLIPPAGE
    use_krw = get_safe_use_krw(multiplier)
    if use_krw < MIN_ORDER_KRW: return 0.0, use_krw, safe_price
    qty = math.floor((use_krw / safe_price) * 100000000) / 100000000
    return qty, use_krw, safe_price

def build_position(signal, filled_entry, filled_qty, used_krw):
    regime = get_market_regime()
    return {"ticker": signal["ticker"], "strategy": signal["strategy"], "strategy_label": signal["strategy_label"], "entry_price": filled_entry, "qty": filled_qty, "used_krw": used_krw, "stop_loss_pct": signal["stop_loss_pct"], "take_profit_pct": signal["take_profit_pct"], "time_stop_sec": signal["time_stop_sec"], "entered_at": time.time(), "peak_price": filled_entry, "breakeven_armed": False, "trailing_armed": False, "reason": signal["reason"], "edge_score": signal.get("edge_score", 0), "trade_tier": signal.get("trade_tier", "B"), "leader_score": signal.get("leader_score", 0), "breakeven_trigger_pct": signal.get("breakeven_trigger_pct", BREAKEVEN_TRIGGER_PCT), "breakeven_buffer_pct": signal.get("breakeven_buffer_pct", BREAKEVEN_BUFFER_PCT), "trail_start_pct": signal.get("trail_start_pct", TRAIL_START_PCT), "trail_backoff_pct": signal.get("trail_backoff_pct", TRAIL_BACKOFF_PCT), "managed_by_bot": True, "invalid_break_level": safe_float(signal.get("invalid_break_level", 0)), "entry_strategy_snapshot": signal["strategy"], "pattern_tags": signal.get("pattern_tags", []), "entry_regime": regime.get("name", "UNKNOWN"), "from_pending_buy": bool(signal.get("from_pending_buy", False)), "max_profit_pct": 0.0, "max_drawdown_pct": 0.0}

def buy_market(signal):
    ticker = signal["ticker"]; entry_price = get_price(ticker)
    if entry_price <= 0: return False, "현재가를 불러오지 못했어"
    before_qty = get_balance(ticker); before_krw = get_krw_balance(); last_error = ""
    for multiplier in RETRY_KRW_LEVELS:
        qty, _, _ = calc_order_qty(ticker, entry_price, multiplier)
        if qty <= 0:
            return False, f"원화 잔고가 부족해서 매수하지 않았어 (사용 가능 원화: {fmt_price(get_krw_balance())})"
        try:
            result = bithumb.buy_market_order(ticker, qty)
            time.sleep(1.5)
            if isinstance(result, dict):
                status = str(result.get("status", ""))
                if status and status != "0000":
                    last_error = result.get("message", "알 수 없는 오류")
                    if status == "5600": continue
                    return False, f"매수 실패 / 응답: {result}"
            after_qty = get_balance(ticker); after_krw = get_krw_balance()
            filled_qty = round(after_qty - before_qty, 8); krw_used_real = max(before_krw - after_krw, 0)
            if filled_qty <= 0:
                last_error = f"체결 확인이 안 됐어 / 응답: {result}"
                continue
            filled_entry = (krw_used_real / filled_qty) if krw_used_real > 0 else entry_price
            active_positions[ticker] = build_position(signal, filled_entry, filled_qty, krw_used_real)
            save_positions()
            add_log({"time": int(time.time()), "type": "BUY", "ticker": ticker, "strategy": signal["strategy"], "strategy_label": signal["strategy_label"], "entry": filled_entry, "qty": filled_qty, "used_krw": krw_used_real, "stop_loss_pct": signal["stop_loss_pct"], "take_profit_pct": signal["take_profit_pct"], "time_stop_sec": signal["time_stop_sec"], "edge_score": signal.get("edge_score"), "trade_tier": signal.get("trade_tier", "B"), "reason": signal["reason"], "managed_by_bot": True, "invalid_break_level": signal.get("invalid_break_level", 0), "pattern_tags": signal.get("pattern_tags", []), "entry_regime": get_market_regime().get("name", "UNKNOWN"), "from_pending_buy": bool(signal.get("from_pending_buy", False))})
            return True, {"entry": filled_entry, "qty": filled_qty, "used_krw": krw_used_real}
        except Exception as e:
            last_error = str(e)
    return False, f"매수는 시도했지만 주문 금액이 빡빡해서 실패했어 / 마지막 사유: {last_error}"

def candidate_promote_ok(candidate, current_signal, regime):
    ticker = candidate["ticker"]
    if ticker in active_positions or len(active_positions) >= MAX_HOLDINGS or not cooldown_ok(ticker): return False, "보유/쿨다운"
    if not strategy_allowed_in_regime(current_signal["strategy"], regime): return False, "장 필터"
    if not should_auto_buy_signal(current_signal, regime=regime): return False, "자동매수 기준 미달"
    return True, "승격 가능"

def process_pending_buy_promotions(shared_cache=None):
    if not PENDING_BUY_ON or not AUTO_BUY or not is_auto_time() or len(active_positions) >= MAX_HOLDINGS: return
    paused, _ = should_pause_auto_buy_now()
    if paused: return
    cleanup_pending_buy_candidates()
    if not pending_buy_candidates: return
    regime = get_market_regime()
    if not regime["allow_auto_buy"]: return
    cache = shared_cache or build_shared_market_cache()
    if not cache: return
    for item in sorted(pending_buy_candidates.values(), key=lambda x: (safe_float(x.get("leader_score", 0)) * 0.8) + safe_float(x.get("edge_score", 0)), reverse=True):
        ticker = item["ticker"]
        if time.time() - safe_float(item.get("created_at", 0)) < PENDING_BUY_RECHECK_MIN_SEC: continue
        if ticker not in cache: continue
        current_signal = None
        for analyzer in [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry]:
            sig = analyzer(ticker, cache[ticker])
            if sig:
                current_signal = apply_trade_tier_adjustments(sig, regime=regime); break
        if not current_signal: continue
        ok, _ = candidate_promote_ok(item, current_signal, regime)
        if not ok: continue
        current_signal["from_pending_buy"] = True
        success, result = buy_market(current_signal)
        if success:
            recent_signal_alerts[ticker] = time.time()
            pending_buy_candidates.pop(ticker, None); save_pending_buys()
            send(f"🚀 다시 보던 후보 자동매수 완료\n\n📊 {ticker}\n💰 매수가(보정): {fmt_price(result['entry'])}")
            return

def build_watch_snapshot(item):
    now_ts = time.time()
    return {"strategy": item.get("strategy", ""), "strategy_label": item.get("strategy_label", ""), "change_pct": safe_float(item.get("change_pct", item.get("change_5", 0))), "change_5": safe_float(item.get("change_5", item.get("change_pct", 0))), "vol_ratio": safe_float(item.get("vol_ratio", item.get("vol_ratio_1m", 0))), "signal_score": safe_float(item.get("signal_score", item.get("edge_score", 0))), "edge_score": safe_float(item.get("edge_score", item.get("signal_score", 0))), "leader_score": safe_float(item.get("leader_score", 0)), "pattern_tags": list(item.get("pattern_tags", [])), "price": safe_float(item.get("current_price", item.get("price", 0))), "turnover": safe_float(item.get("turnover", 0)), "rsi": safe_float(item.get("rsi", item.get("rsi_1m", 50))), "saved_at": safe_float(item.get("saved_at", now_ts)) or now_ts}

def compare_watch_improvement(prev_snap, item):
    reasons = []
    if safe_float(item.get("vol_ratio", 0)) - safe_float(prev_snap.get("vol_ratio", 0)) >= WATCH_VOL_IMPROVE_DELTA: reasons.append("거래량 증가")
    if safe_float(item.get("change_pct", 0)) - safe_float(prev_snap.get("change_pct", 0)) >= WATCH_CHANGE_IMPROVE_DELTA: reasons.append("상승률 강화")
    if safe_float(item.get("signal_score", 0)) - safe_float(prev_snap.get("signal_score", 0)) >= WATCH_SCORE_IMPROVE_DELTA: reasons.append("점수 상승")
    return reasons

def should_send_watch_alert(ticker, item, now_ts):
    if ticker in active_positions or ticker in pending_sells: return False, "", ""
    prev_time = recent_watch_alerts.get(ticker, 0); prev_snap = recent_watch_snapshots.get(ticker)
    if not prev_snap or prev_time <= 0: return True, "new", ""
    reasons = compare_watch_improvement(prev_snap, item)
    if reasons: return True, "upgrade", " / ".join(reasons)
    renotice_count = int(recent_watch_renotice_counts.get(ticker, 0))
    if now_ts - prev_time >= WATCH_RENOTICE_SEC and renotice_count < WATCH_RENOTICE_MAX_PER_TICKER: return True, "renotice", "시간이 지나 다시 확인"
    return False, "", ""

def save_watch_snapshot(ticker, item, now_ts, alert_type="new"):
    recent_watch_alerts[ticker] = now_ts
    snap = build_watch_snapshot(item); snap["saved_at"] = now_ts
    recent_watch_snapshots[ticker] = snap
    if alert_type in ["new", "upgrade"]: recent_watch_renotice_counts[ticker] = 0
    elif alert_type == "renotice": recent_watch_renotice_counts[ticker] = int(recent_watch_renotice_counts.get(ticker, 0)) + 1

def collect_signals_from_cache(cache, auto_only=False, regime=None):
    results = []
    analyzers = [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry] if auto_only else [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry, analyze_prepump_entry, analyze_pullback_entry]
    for ticker, data in cache.items():
        for analyzer in analyzers:
            try:
                signal = analyzer(ticker, data)
                if not signal: continue
                signal = apply_trade_tier_adjustments(signal, regime=regime)
                if regime:
                    if auto_only and not strategy_allowed_in_regime(signal["strategy"], regime): continue
                    if (not auto_only) and not watch_strategy_allowed_in_regime(signal["strategy"], regime): continue
                if auto_only and not should_auto_buy_signal(signal, regime=regime): continue
                results.append(signal)
            except Exception:
                continue
    return results

def dedupe_best_signal_per_ticker(results, key_name="signal_score"):
    best = {}
    for signal in results:
        t = signal["ticker"]; s = float(signal.get(key_name, 0))
        if t not in best or s > float(best[t].get(key_name, 0)): best[t] = signal
    return list(best.values())

def build_leader_watch_candidates(cache, regime):
    fallback = []
    for ticker, data in cache.items():
        try:
            df = data.get("df_1m")
            if df is None or len(df) < 20: continue
            current_price = safe_float(data.get("price", 0))
            if current_price <= 0: continue
            leader_score = safe_float(data.get("leader_score", 0)); vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0))) or get_vol_ratio(df, 3, 15); change_1 = safe_float(data.get("change_1", 0)); change_3 = safe_float(data.get("change_3", 0)); change_5 = safe_float(data.get("change_5", 0)); range_pct = safe_float(data.get("range_pct_1m", 0)) or get_range_pct(df, 10); rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50))) or get_rsi(df, 14)
            if leader_score < 0.5: continue
            if vol_ratio < 0.95 and change_5 < 0.22: continue
            label = "관심 후보" if change_5 < 1.0 else "강한 관심 후보"
            fallback.append(make_signal(ticker=ticker, strategy="LEADER_WATCH", strategy_label=label, current_price=current_price, vol_ratio=vol_ratio, change_pct=max(change_1, change_3, change_5), rsi=rsi, range_pct=range_pct, score=max(5.6, leader_score + max(change_5, 0) * 0.9 + min(vol_ratio, 4.0) * 0.65), edge_score=max(4.2, leader_score + min(max(change_5, 0), 3.0) * 0.75 + min(vol_ratio, 4.0) * 0.55), leader_score=leader_score, stop_loss_pct=-1.4, take_profit_pct=4.2, time_stop_sec=720, pattern_tags=[]))
        except Exception:
            continue
    fallback.sort(key=lambda x: (safe_float(x.get("leader_score", 0)) * 1.2) + safe_float(x.get("edge_score", 0)), reverse=True)
    return fallback[:18]

def scan_watchlist(shared_cache=None):
    cache = shared_cache or build_shared_market_cache()
    if not cache: return
    regime = get_market_regime()
    results = collect_signals_from_cache(cache, auto_only=False, regime=regime)
    results.extend(build_leader_watch_candidates(cache, regime))
    if not results: return
    real_results = [x for x in results if x.get("strategy") != "LEADER_WATCH"]
    if real_results: update_pending_buy_candidates_from_results(real_results, regime)
    unique_results = dedupe_best_signal_per_ticker(results, key_name="signal_score")
    unique_results.sort(key=lambda x: signal_priority_value(x), reverse=True)
    top = unique_results[:18]
    new_lines, upgrade_lines, renotice_lines = [], [], []
    now_ts = time.time()
    for item in top:
        ticker = item["ticker"]
        send_ok, alert_type, reason_text = should_send_watch_alert(ticker, item, now_ts)
        if not send_ok: continue
        base_line = f"• {ticker} / {fmt_price(item['current_price'])} / 상승 {float(item.get('change_pct', 0)):.2f}% / 거래량 {float(item.get('vol_ratio', 0)):.2f}배 / 방식 {item['strategy_label']} / 등급 {item.get('trade_tier','B')} / 주도주 {safe_float(item.get('leader_score',0)):.2f}"
        if alert_type == "upgrade": upgrade_lines.append(base_line + f"\n  ↳ 더 좋아진 이유: {reason_text}")
        elif alert_type == "renotice": renotice_lines.append(base_line + f"\n  ↳ 다시 확인: {reason_text}")
        else: new_lines.append(base_line)
        save_watch_snapshot(ticker, item, now_ts, alert_type)
    if new_lines: send("👀 관심 후보 알림\n\n" + "\n\n".join(new_lines))
    if upgrade_lines: send("🔁 관심 후보 강화 알림\n\n" + "\n\n".join(upgrade_lines))
    if renotice_lines: send("🕒 관심 후보 다시 확인\n\n" + "\n\n".join(renotice_lines))

def scan_and_auto_trade(shared_cache=None):
    if not AUTO_BUY or not is_auto_time() or len(active_positions) >= MAX_HOLDINGS: return
    market_ok, market_msg, _ = get_btc_market_state()
    if BTC_FILTER_ON and not market_ok:
        print(f"[시장 차단] {market_msg}"); return
    regime = get_market_regime()
    if REGIME_FILTER_ON and not regime["allow_auto_buy"]:
        print(f"[쉬는 장] {regime['message']}"); return
    paused, pause_msg = should_pause_auto_buy_now()
    if paused:
        print(f"[자동 쉬기] {pause_msg}"); return
    cache = shared_cache or build_shared_market_cache()
    if not cache: return
    candidates = collect_signals_from_cache(cache, auto_only=True, regime=regime)
    if not candidates: return
    candidates = dedupe_best_signal_per_ticker(candidates, key_name="edge_score")
    candidates.sort(key=signal_priority_value, reverse=True)
    best = candidates[0]; ticker = best["ticker"]
    if ticker in pending_buy_candidates or not cooldown_ok(ticker): return
    recent_signal_alerts[ticker] = time.time()
    success, result = buy_market(best)
    if not success:
        send(f"\n❌ 자동매수 실패\n\n📊 {ticker}\n방식: {best['strategy_label']}\n\n사유:\n{result}\n")
        return
    send(f"\n🔥 자동매수 완료\n\n📊 {ticker}\n방식: {best['strategy_label']} / 등급 {best.get('trade_tier','B')} / 주도주 {safe_float(best.get('leader_score',0)):.2f}\n\n💰 매수가(보정): {fmt_price(result['entry'])}\n📦 수량: {result['qty']:.6f}\n")

def get_scan_debug_candidates():
    try:
        now_ts = time.time(); cache = shared_market_cache; cache_age = int(now_ts - shared_market_cache_time) if shared_market_cache_time else 999999
        if (not cache) or cache_age > 180:
            cache = build_shared_market_cache(force=True)
            now_ts = time.time(); cache_age = int(now_ts - shared_market_cache_time) if shared_market_cache_time else 999999
        if cache and cache_age <= 180:
            items = []
            for ticker, data in cache.items():
                price = safe_float(data.get("price", 0))
                if price <= 0: continue
                change_1 = safe_float(data.get("change_1", 0)); change_3 = safe_float(data.get("change_3", 0)); change_5 = safe_float(data.get("change_5", 0)); vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0))); leader = safe_float(data.get("leader_score", 0)); turnover = safe_float(data.get("turnover", 0)); rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50)))
                lite_score = max(change_5, 0) * 2.4 + max(change_3, 0) * 1.2 + max(change_1, 0) * 0.6 + max(vol_ratio - 1.0, 0) * 4.0 + min(turnover / 1000000000.0, 8.0) + leader * 1.6
                if rsi >= 82: lite_score -= 1.5
                reasons = []
                if change_5 < 0.20: reasons.append("5분상승약함")
                if vol_ratio < 1.00: reasons.append("거래량약함")
                if leader < 0.50: reasons.append("주도약함")
                if not reasons: reasons.append("후보가능권")
                items.append({"ticker": ticker, "price": price, "change_5": change_5, "vol_ratio": vol_ratio, "leader_score": leader, "turnover": turnover, "lite_score": lite_score, "rsi": rsi, "reasons": reasons})
            items.sort(key=lambda x: (safe_float(x.get("lite_score", 0)), safe_float(x.get("turnover", 0)), safe_float(x.get("change_5", 0))), reverse=True)
            return items[:8], f"최근 {cache_age}초 shared cache"
        return [], f"최근 캐시 없음({cache_age}초)"
    except Exception as e:
        return None, f"디버그 조회 에러: {e}"

def build_scan_debug_text():
    results, note = get_scan_debug_candidates()
    if results is None: return f"⚠️ 스캔 디버그 에러\n{note}"
    lines = ["🔎 스캔 디버그"]
    if note: lines.append(f"사유: {note}")
    if not results: return "\n".join(lines + ["", "후보 없음"])
    for i, item in enumerate(results, 1):
        price_text = f"{safe_float(item.get('price', 0)):,.4f}".rstrip("0").rstrip(".")
        lines += ["", f"{i}. {item.get('ticker','?')} / 현재가 {price_text}", f"   5분상승 {safe_float(item.get('change_5',0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 주도 {safe_float(item.get('leader_score',0)):.2f}", f"   경량점수 {safe_float(item.get('lite_score',0)):.2f}", f"   체크: {' / '.join(item.get('reasons', [])[:3])}"]
    return "\n".join(lines)

def scan_debug_command(update, context: CallbackContext):
    send("🔎 scan_debug 실행중...")
    send(build_scan_debug_text())

def append_debug_shortlist_parts(parts):
    try:
        results, note = get_scan_debug_candidates()
        header = "🔎 상위 관심 후보" + (f" ({note})" if note else "")
        if results is None:
            parts.append(f"⚠️ 상위 스캔 후보 에러: {note}")
            return
        if not results:
            parts.append(header + "\n\n후보 없음")
            return
        lines = [header]
        for item in results[:3]:
            lines.append(f"• {item.get('ticker','?')} / 5분 {safe_float(item.get('change_5',0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 주도 {safe_float(item.get('leader_score',0)):.2f}")
        parts.append("\n".join(lines))
    except Exception as e:
        parts.append(f"⚠️ 상위 스캔 후보 표시 에러: {e}")

def summary_command(update, context: CallbackContext):
    closes = [x for x in trade_logs if x.get("type") in CLOSE_TYPES]
    if not closes:
        send("📊 아직 종료된 거래가 없어"); return
    total = len(closes); wins = len([x for x in closes if float(x.get("pnl_pct", 0)) > 0]); losses = total - wins; total_pnl = sum(float(x.get("pnl_pct", 0)) for x in closes); avg_pnl = total_pnl / total if total > 0 else 0.0
    send(f"\n📊 거래 요약\n\n총 종료 거래: {total}\n익절/플러스 종료: {wins}\n손절/마이너스 종료: {losses}\n승률: {(wins / total) * 100:.2f}%\n누적 수익률: {total_pnl:.2f}%\n평균 수익률: {avg_pnl:.2f}%\n")

def today_command(update, context: CallbackContext):
    tz = ZoneInfo(TIMEZONE); today = datetime.now(tz).date(); closes = []
    for x in trade_logs:
        if x.get("type") not in CLOSE_TYPES: continue
        if datetime.fromtimestamp(int(x.get("time", 0)), tz).date() == today: closes.append(x)
    if not closes:
        send("📊 오늘 종료된 거래가 아직 없어"); return
    total = len(closes); wins = len([x for x in closes if float(x.get("pnl_pct", 0)) > 0]); losses = total - wins; total_pnl = sum(float(x.get("pnl_pct", 0)) for x in closes); avg_pnl = total_pnl / total if total > 0 else 0.0
    lines = [f"• {x.get('ticker','?')} / {x.get('type')} / {float(x.get('pnl_pct',0)):.2f}%" for x in closes[-10:]]
    send(f"\n📊 오늘 거래 요약\n\n총 종료 거래: {total}\n익절/플러스 종료: {wins}\n손절/마이너스 종료: {losses}\n승률: {(wins / total) * 100:.2f}%\n누적 수익률: {total_pnl:.2f}%\n평균 수익률: {avg_pnl:.2f}%\n\n최근 종료 거래\n" + "\n".join(lines))

def summary_strategy_command(update, context: CallbackContext):
    closes = [x for x in trade_logs if x.get("type") in CLOSE_TYPES]
    if not closes:
        send("📊 아직 종료된 거래가 없어"); return
    bucket = {}
    for x in closes: bucket.setdefault(x.get("strategy", "UNKNOWN"), []).append(x)
    lines = []
    for strategy, items in sorted(bucket.items(), key=lambda kv: len(kv[1]), reverse=True):
        total = len(items); wins = len([x for x in items if float(x.get("pnl_pct", 0)) > 0]); total_pnl = sum(float(x.get("pnl_pct", 0)) for x in items); avg_pnl = total_pnl / total if total > 0 else 0.0
        lines.append(f"{strategy}\n- 거래: {total}\n- 승률: {(wins / total) * 100:.2f}%\n- 평균: {avg_pnl:.2f}%\n- 누적: {total_pnl:.2f}%")
    parts = ["📊 전략별 전체 결과\n\n" + "\n\n".join(lines)]
    append_debug_shortlist_parts(parts)
    send("\n\n".join(parts))

def today_strategy_command(update, context: CallbackContext):
    send("📊 오늘 전략 결과는 1차 안정본에서 간단 요약만 유지했어.")

def btc_command(update, context: CallbackContext):
    send(analyze_btc_flow())

def reset_pause_command(update, context: CallbackContext):
    reset_auto_pause_state(bypass_sec=900)
    send("🛠 자동 쉬기 수동 해제\n\n앞으로 15분 동안은 이전 손실 기록 때문에 바로 다시 쉬지 않게 해둘게.")

def status_command(update, context: CallbackContext):
    parts = []
    paused, pause_msg = should_pause_auto_buy_now()
    if paused: parts.append("⏸ 자동매수 쉬는 중\n\n" + pause_msg)
    elif auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time()); parts.append("🛠 자동 쉬기 수동 해제 적용 중\n\n" + f"{max(remain, 0)}초 남음")
    if active_positions:
        lines = []
        for ticker, pos in active_positions.items():
            price = get_price(ticker); entry = float(pos["entry_price"]); qty = float(pos.get("qty", 0)); pnl = ((price - entry) / entry) * 100 if entry > 0 and price > 0 else 0; held_min = int((time.time() - int(pos["entered_at"])) / 60); value = qty * price if price > 0 else 0
            lines.append(f"• {ticker} / 방식 {pos['strategy_label']} / 진입 {fmt_price(entry)} / 현재 {fmt_price(price)} / 수익률 {fmt_pct(pnl)} / 평가금액 {fmt_price(value)} / 보유 {held_min}분")
        parts.append("📌 현재 포지션\n\n" + "\n".join(lines))
    else:
        parts.append("📭 현재 보유 포지션 없음")
    if pending_buy_candidates:
        c_lines = []
        sorted_candidates = sorted(pending_buy_candidates.values(), key=lambda x: (safe_float(x.get("leader_score", 0)) * 0.8) + safe_float(x.get("edge_score", 0)), reverse=True)[:6]
        for item in sorted_candidates:
            age = int(time.time() - safe_float(item.get("created_at", 0)))
            c_lines.append(f"• {item['ticker']} / 방식 {item.get('strategy_label','?')} / 다시 본 시간 {age}초 / 기대점수 {safe_float(item.get('edge_score', 0)):.2f} / 주도주 {safe_float(item.get('leader_score', 0)):.2f}")
        parts.append("🕒 다시 볼 후보\n\n" + "\n".join(c_lines))
    if pending_sells:
        parts.append("⚠️ 매도 확인 대기중\n\n" + "\n".join([f"• {ticker}" for ticker in pending_sells.keys()]))
    append_debug_shortlist_parts(parts)
    send("\n\n".join(parts))

def find_last_open_buy_log(ticker):
    last_buy = None; last_close_time = 0
    for log in trade_logs:
        if log.get("ticker") != ticker: continue
        if log.get("type") == "BUY" and log.get("managed_by_bot", False):
            if (last_buy is None) or int(log.get("time", 0)) > int(last_buy.get("time", 0)): last_buy = log
        elif log.get("type") in CLOSE_TYPES:
            last_close_time = max(last_close_time, int(log.get("time", 0)))
    if last_buy and int(last_buy.get("time", 0)) > last_close_time: return last_buy
    return None

def recover_positions_from_exchange():
    global active_positions
    if active_positions: return
    try:
        tickers = pybithumb.get_tickers()
    except Exception as e:
        print("[복구 티커 조회 오류]", e); return
    recovered = 0
    for ticker in tickers:
        try:
            last_buy = find_last_open_buy_log(ticker)
            if not last_buy: continue
            qty = get_balance(ticker); price = get_price(ticker)
            if qty <= 0 or price <= 0 or qty * price < DUST_KEEP_MIN_KRW: continue
            entry_price = float(last_buy.get("entry", price))
            active_positions[ticker] = {"ticker": ticker, "strategy": last_buy.get("strategy", "RECOVER"), "strategy_label": last_buy.get("strategy_label", "복구 포지션"), "entry_price": entry_price, "qty": qty, "used_krw": float(last_buy.get("used_krw", qty * entry_price)), "stop_loss_pct": float(last_buy.get("stop_loss_pct", BASE_STOP_LOSS_PCT)), "take_profit_pct": float(last_buy.get("take_profit_pct", BASE_TP_PCT)), "time_stop_sec": int(last_buy.get("time_stop_sec", 900)), "entered_at": int(last_buy.get("time", int(time.time()))), "peak_price": max(entry_price, price), "breakeven_armed": False, "trailing_armed": False, "reason": last_buy.get("reason", "이전 BUY 로그 기반 복구"), "edge_score": float(last_buy.get("edge_score", 0)), "leader_score": 0.0, "trade_tier": "B", "managed_by_bot": True, "invalid_break_level": safe_float(last_buy.get("invalid_break_level", 0)), "entry_strategy_snapshot": last_buy.get("strategy", "RECOVER"), "pattern_tags": last_buy.get("pattern_tags", []), "entry_regime": last_buy.get("entry_regime", "UNKNOWN"), "from_pending_buy": bool(last_buy.get("from_pending_buy", False)), "max_profit_pct": 0.0, "max_drawdown_pct": 0.0}
            recovered += 1
        except Exception:
            continue
    if recovered > 0:
        save_positions(); send(f"♻️ 포지션 복구 완료\n최근 BUY 로그 기준으로 복구된 코인 수: {recovered}")

def update_position_run_stats(pos, pnl_pct):
    changed = False
    if pnl_pct > safe_float(pos.get("max_profit_pct", 0)): pos["max_profit_pct"] = round(float(pnl_pct), 2); changed = True
    if pnl_pct < safe_float(pos.get("max_drawdown_pct", 0)): pos["max_drawdown_pct"] = round(float(pnl_pct), 2); changed = True
    return changed

def post_entry_strength_fail(ticker, pos, current_price):
    held_sec = int(time.time() - int(pos["entered_at"]))
    if held_sec < 120 or held_sec > 240: return False, ""
    entry_price = float(pos["entry_price"])
    if entry_price <= 0: return False, ""
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    ticker_cache = shared_market_cache.get(ticker)
    if not ticker_cache: return False, ""
    df = ticker_cache.get("df_1m")
    if df is None or len(df) < 12: return False, ""
    vol_ratio_now = get_vol_ratio(df, 2, 8)
    strategy = pos.get("strategy", "")
    if strategy in ["EARLY", "PRE_BREAKOUT"] and pnl_pct < 0.12:
        if vol_ratio_now < 0.95 or pnl_pct <= -0.35: return True, "진입 후 힘이 약해졌어"
    if strategy == "TREND_CONT" and (vol_ratio_now < 0.95 and pnl_pct < 0.12): return True, "추세 지속 기대였는데 힘이 약해졌어"
    return False, ""

def should_scenario_fail_exit(ticker, pos, current_price):
    held_sec = int(time.time() - int(pos["entered_at"]))
    if held_sec < 70 or held_sec > 210: return False, ""
    entry_price = float(pos["entry_price"])
    if entry_price <= 0 or current_price <= 0: return False, ""
    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    if pnl_pct >= 0.18: return False, ""
    ticker_cache = shared_market_cache.get(ticker)
    if not ticker_cache: return False, ""
    df = ticker_cache.get("df_1m")
    if df is None or len(df) < 20: return False, ""
    vol_ratio_now = get_vol_ratio(df, 2, 8)
    invalid_break_level = safe_float(pos.get("invalid_break_level", 0.0))
    strategy = pos.get("strategy", "")
    if strategy in ["BREAKOUT", "PRE_BREAKOUT"] and invalid_break_level > 0 and current_price <= invalid_break_level * (1 - 0.10 / 100) and vol_ratio_now < 1.15: return True, "돌파 기준선 아래로 다시 밀렸고 거래량도 약해"
    if strategy == "EARLY" and pnl_pct <= -0.45 and vol_ratio_now < 1.15: return True, "초반 흐름이 약해졌어"
    if strategy == "TREND_CONT" and pnl_pct <= -0.32 and vol_ratio_now < 1.0: return True, "추세 지속 기대였는데 재가속이 약해"
    return False, ""

def build_time_stop_comment(pnl_pct):
    if pnl_pct >= 0.2: return "짧게 수익이 났지만 크게 뻗는 힘은 약해서 정리했어."
    if pnl_pct > -0.15: return "빠르게 안 뻗어서 거의 본전 근처에서 정리했어."
    return "힘이 약해서 정리했어."

def monitor_positions():
    for ticker, pos in list(active_positions.items()):
        try:
            current_price = get_price(ticker)
            if current_price <= 0: continue
            balance = get_balance(ticker); market_value = balance * current_price
            if balance <= 0:
                if ticker in pending_sells: continue
                active_positions.pop(ticker, None); save_positions(); clear_position_file_if_empty(); continue
            entry_price = float(pos["entry_price"]); pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
            changed_stats = update_position_run_stats(pos, pnl_pct)
            if current_price > pos["peak_price"]: pos["peak_price"] = current_price; changed_stats = True
            if changed_stats: save_positions()
            stop_price = entry_price * (1 + pos["stop_loss_pct"] / 100); tp_price = entry_price * (1 + pos["take_profit_pct"] / 100); held_sec = int(time.time() - int(pos["entered_at"]))
            if market_value < MIN_ORDER_KRW: continue
            early_fail, early_fail_reason = post_entry_strength_fail(ticker, pos, current_price)
            if early_fail:
                ok, msg = sell_market_confirmed(ticker, "EARLY_FAIL", pos, current_price, pnl_pct, held_sec)
                send(f"\n⚠️ 진입 직후 힘이 약해서 빠른 정리\n\n📊 {ticker}\n사유: {early_fail_reason if ok else msg}\n")
                continue
            fail_exit, fail_reason = should_scenario_fail_exit(ticker, pos, current_price)
            if fail_exit:
                ok, msg = sell_market_confirmed(ticker, "SCENARIO_FAIL", pos, current_price, pnl_pct, held_sec)
                send(f"\n⚠️ 예상한 흐름이 깨져서 빠른 정리\n\n📊 {ticker}\n사유: {fail_reason if ok else msg}\n")
                continue
            if held_sec >= pos["time_stop_sec"] and pnl_pct < 0.8:
                ok, msg = sell_market_confirmed(ticker, "TIME_STOP", pos, current_price, pnl_pct, held_sec)
                send(f"\n⏱ 오래 안 가서 정리\n\n📊 {ticker}\n{build_time_stop_comment(pnl_pct) if ok else msg}\n")
                continue
            if current_price <= stop_price:
                ok, msg = sell_market_confirmed(ticker, "STOP", pos, current_price, pnl_pct, held_sec)
                send(f"\n🚨 손절\n\n📊 {ticker}\n사유: {'손절 완료' if ok else msg}\n")
                continue
            if pnl_pct >= float(pos.get("breakeven_trigger_pct", BREAKEVEN_TRIGGER_PCT)) and not pos.get("breakeven_armed", False):
                pos["breakeven_armed"] = True; save_positions()
            if pos.get("breakeven_armed", False):
                be_price = entry_price * (1 + float(pos.get("breakeven_buffer_pct", BREAKEVEN_BUFFER_PCT)) / 100)
                if held_sec >= 240 and current_price <= be_price:
                    ok, msg = sell_market_confirmed(ticker, "BREAKEVEN", pos, current_price, pnl_pct, held_sec)
                    send(f"\n🛡️ 손실 없이 정리\n\n📊 {ticker}\n사유: {'본절 정리' if ok else msg}\n")
                    continue
            if pnl_pct >= float(pos.get("trail_start_pct", TRAIL_START_PCT)) and not pos.get("trailing_armed", False):
                pos["trailing_armed"] = True; save_positions()
            if pos.get("trailing_armed", False):
                trail_stop = pos["peak_price"] * (1 - float(pos.get("trail_backoff_pct", TRAIL_BACKOFF_PCT)) / 100)
                if current_price <= trail_stop:
                    ok, msg = sell_market_confirmed(ticker, "TRAIL_TP", pos, current_price, pnl_pct, held_sec)
                    send(f"\n📈 오른 뒤 조금 내려와서 익절\n\n📊 {ticker}\n사유: {'익절 완료' if ok else msg}\n")
                    continue
            if current_price >= tp_price:
                ok, msg = sell_market_confirmed(ticker, "TP", pos, current_price, pnl_pct, held_sec)
                send(f"\n🎉 익절 완료\n\n📊 {ticker}\n사유: {'익절 완료' if ok else msg}\n")
                continue
        except Exception as e:
            print(f"[포지션 감시 오류] {ticker} / {e}")
            traceback.print_exc()

updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler("summary", summary_command))
dispatcher.add_handler(CommandHandler("today", today_command))
dispatcher.add_handler(CommandHandler("summary_strategy", summary_strategy_command))
dispatcher.add_handler(CommandHandler("today_strategy", today_strategy_command))
dispatcher.add_handler(CommandHandler("btc", btc_command))
dispatcher.add_handler(CommandHandler("reset_pause", reset_pause_command))
dispatcher.add_handler(CommandHandler("status", status_command))
dispatcher.add_handler(CommandHandler("scan_debug", scan_debug_command))
updater.start_polling(drop_pending_updates=True)

load_positions()
load_pending_sells()
load_pending_buys()
recover_positions_from_exchange()
cleanup_pending_buy_candidates()
save_positions()
save_pending_sells()
save_pending_buys()
send_startup_message()

print(f"🚀 {BOT_VERSION} 실행 / {TIMEZONE}")

while True:
    now_ts = time.time()
    try:
        shared_cache = build_shared_market_cache(force=False)
        scan_watchlist(shared_cache=shared_cache)
        process_pending_buy_promotions(shared_cache=shared_cache)
        scan_and_auto_trade(shared_cache=shared_cache)
        check_pending_sells()
        cleanup_pending_buy_candidates()
        monitor_positions()
        if now_ts - last_btc_report_time >= BTC_REPORT_INTERVAL:
            try: send(analyze_btc_flow())
            except Exception as e: print(f"[BTC 리포트 오류] {e}")
            last_btc_report_time = now_ts
        if now_ts - last_status_report_time >= STATUS_REPORT_INTERVAL:
            try:
                if active_positions or pending_sells or pending_buy_candidates: status_command(None, None)
            except Exception as e:
                print(f"[상태 리포트 오류] {e}")
            last_status_report_time = now_ts
    except Exception as e:
        print(f"[메인 루프 오류] {e}")
        traceback.print_exc()
    time.sleep(LOOP_SLEEP)
