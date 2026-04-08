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
BOT_VERSION = "수익형 v6.5.2"

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
# 파일 / 시간
# =========================
TIMEZONE = "Asia/Seoul"
LOG_FILE = "trade_log.json"
POSITIONS_FILE = "active_positions.json"
PENDING_SELLS_FILE = "pending_sells.json"
PENDING_BUYS_FILE = "pending_buy_candidates.json"

# =========================
# 실행 주기
# =========================
SCAN_INTERVAL = 8
POSITION_CHECK_INTERVAL = 4
LOOP_SLEEP = 1

# =========================
# 시장 캐시 / 유니버스
# =========================
TOP_TICKERS = 80
ABSOLUTE_TURNOVER_CANDIDATES = 45
SURGE_CANDIDATES = 55
UNIVERSE_REFRESH_SEC = 18
MARKET_CACHE_TTL_SEC = 8

# =========================
# 주문 / 자금
# =========================
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

ALLOW_CHASE = False

# =========================
# BTC / 시장 필터
# =========================
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -2.0
BTC_STRONG_BLOCK_PCT = -2.8
BTC_MA_FILTER = True

REGIME_FILTER_ON = True
REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS = True
REGIME_SIDEWAYS_MAX_ABS_PCT = 0.55
REGIME_WEAK_MAX_ABS_PCT = -0.8
REGIME_STRONG_UP_PCT = 1.0

# =========================
# 리스크 관리
# =========================
BASE_STOP_LOSS_PCT = -1.7
BASE_TP_PCT = 4.8

TRAIL_START_PCT = 2.8
TRAIL_BACKOFF_PCT = 1.15

BREAKEVEN_TRIGGER_PCT = 1.7
BREAKEVEN_BUFFER_PCT = 0.15

TIME_STOP_MIN_SEC = 480
TIME_STOP_MAX_SEC = 1080

MIN_EXPECTED_EDGE_SCORE = 7.8
MIN_EXPECTED_TP_PCT = 3.2

# =========================
# 초반 선점형 (자동매수 허용)
# =========================
EARLY_MIN_CHANGE_PCT = 0.75
EARLY_MAX_CHANGE_PCT = 2.4
EARLY_MIN_VOL_RATIO = 2.4
EARLY_MIN_RSI = 45
EARLY_MAX_RSI = 63
EARLY_MIN_RANGE_PCT = 0.8

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
PRE_BREAKOUT_MAX_CHANGE_PCT = 0.95
PRE_BREAKOUT_MIN_VOL_RATIO = 2.6
PRE_BREAKOUT_MIN_RSI = 48
PRE_BREAKOUT_MAX_RSI = 66
PRE_BREAKOUT_MIN_RANGE_PCT = 0.9
PRE_BREAKOUT_MAX_GAP_PCT = 0.28

# =========================
# 급등 돌파형 (자동매수 허용)
# =========================
BREAKOUT_MIN_CHANGE_PCT = 0.80
BREAKOUT_MAX_CHANGE_PCT = 3.0
BREAKOUT_MIN_VOL_RATIO = 3.2
BREAKOUT_MIN_RSI = 52
BREAKOUT_MAX_RSI = 72
BREAKOUT_MIN_RANGE_PCT = 1.0
BREAKOUT_BREAK_PCT = 0.12

# =========================
# 추격형 (기본 OFF)
# =========================
CHASE_MIN_CHANGE_PCT = 1.8
CHASE_MAX_CHANGE_PCT = 3.4
CHASE_MIN_VOL_RATIO = 4.0
CHASE_MIN_RSI = 55
CHASE_MAX_RSI = 72
CHASE_MIN_RANGE_PCT = 1.6

# =========================
# 늦은 진입 방지
# =========================
LATE_ENTRY_FILTER_ON = True
LATE_ENTRY_MAX_NEAR_HIGH_PCT = 0.22
LATE_ENTRY_MAX_5BAR_CHANGE_PCT = 3.1
LATE_ENTRY_MAX_3BAR_CHANGE_PCT = 2.2
LATE_ENTRY_MAX_BREAKOUT_EXTENSION_PCT = 0.38

# =========================
# 시나리오 실패 빠른 정리
# =========================
SCENARIO_EXIT_ON = True
SCENARIO_MIN_HOLD_SEC = 70
SCENARIO_MAX_HOLD_SEC = 210
SCENARIO_MIN_PROGRESS_PCT = 0.18
SCENARIO_FAIL_DROP_FROM_BREAK_LEVEL_PCT = 0.10
SCENARIO_FAIL_DROP_FROM_ENTRY_PCT = -0.45
SCENARIO_WEAK_VOL_RATIO_THRESHOLD = 1.15

# =========================
# 차트 구조 패턴
# =========================
PATTERN_FILTER_ON = True

USE_HIGHER_LOWS_CORE = True
USE_BOX_COMPRESSION_CORE = True
USE_BIG_BULL_HALF_HOLD_CORE = True

USE_FAKE_BREAKDOWN_RECOVERY_BONUS = True
USE_PULLBACK_RECHECK_BONUS = True

# =========================
# 후보 승격 매수
# =========================
PENDING_BUY_ON = True
PENDING_BUY_MAX_ITEMS = 5
PENDING_BUY_TTL_SEC = 180
PENDING_BUY_MIN_EDGE = 6.6
PENDING_BUY_MIN_SCORE = 6.2
PENDING_BUY_RECHECK_MIN_SEC = 20

PROMOTE_RECOVERY_TO_HIGH_PCT = 99.7
PROMOTE_MIN_VOL_RATIO = 1.2
PROMOTE_MAX_BREAKOUT_EXTENSION_PCT = 0.45

# =========================
# watch 재알림 제어
# =========================
WATCH_RENOTICE_SEC = 600
WATCH_VOL_IMPROVE_DELTA = 0.8
WATCH_CHANGE_IMPROVE_DELTA = 0.7
WATCH_SCORE_IMPROVE_DELTA = 1.2

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
recent_watch_snapshots = {}
pending_sells = {}
pending_buy_candidates = {}

# =========================
# 공유 캐시
# =========================
shared_market_cache = {}
shared_market_cache_time = 0
market_universe = []
market_universe_time = 0

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

def send_startup_message():
    send(
        f"""
✅ 봇 시작 완료

버전: {BOT_VERSION}
자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}
운영시간: {AUTO_BUY_START_HOUR}:00 ~ {AUTO_BUY_END_HOUR}:00
주문 최대금액: {MAX_ENTRY_KRW:,}원

자동매수 허용:
- 초반 선점형
- 쏘기 직전형
- 급등 돌파형
- 추격형({'켜짐' if ALLOW_CHASE else '꺼짐'})

알림만 하는 전략:
- 상승 시작형
- 눌림 반등형

v6.5.2 핵심:
- 쉬는 장 필터 강화
- 늦은 진입 방지
- 시나리오 실패 빠른 정리
- 차트 구조 5개 반영
- 후보알림 → 2차확인 → 자동매수 승격
- 후보 반복 알림 감소
- 좋아졌을 때만 강화 알림
- 보유중/대기중 코인 후보알림 중복 제거
- TIME_STOP 문구 자연화
- 공유 캐시
- 매도 exit 보정

명령어:
- /status
- /summary
- /today
- /summary_strategy
- /today_strategy
- /btc
"""
    )

# =========================
# 파일 I/O
# =========================
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
        try:
            os.remove(POSITIONS_FILE)
        except Exception:
            pass

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

def get_krw_balance():
    try:
        bal = bithumb.get_balance("BTC")
        if not bal or len(bal) < 4:
            return 0.0
        return float(bal[2] or 0)
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

def get_recent_high(df, n=8, exclude_last=True):
    try:
        recent = df.tail(n)
        if exclude_last:
            recent = recent.iloc[:-1]
        if len(recent) <= 0:
            return 0.0
        return float(recent["high"].max())
    except Exception:
        return 0.0

def get_recent_low(df, n=8, exclude_last=False):
    try:
        recent = df.tail(n)
        if exclude_last:
            recent = recent.iloc[:-1]
        if len(recent) <= 0:
            return 0.0
        return float(recent["low"].min())
    except Exception:
        return 0.0

# =========================
# 차트 구조 패턴
# =========================
def detect_higher_lows(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12:
            return False, {}

        part1 = recent.iloc[0:4]
        part2 = recent.iloc[4:8]
        part3 = recent.iloc[8:12]

        low1 = float(part1["low"].min())
        low2 = float(part2["low"].min())
        low3 = float(part3["low"].min())

        ok = low1 < low2 < low3
        return ok, {"low1": low1, "low2": low2, "low3": low3}
    except Exception:
        return False, {}

def detect_box_top_compression(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12:
            return False, {}

        highs = list(recent["high"].tail(6))
        lows = list(recent["low"].tail(6))
        current_price = float(recent["close"].iloc[-1])

        hi_max = max(highs)
        hi_min = min(highs)
        low_early = min(lows[:3])
        low_late = min(lows[3:])
        high_band_pct = ((hi_max - hi_min) / hi_min) * 100 if hi_min > 0 else 999
        dist_from_top_pct = ((hi_max - current_price) / current_price) * 100 if current_price > 0 else 999

        ok = (
            high_band_pct <= 0.45
            and low_late >= low_early
            and 0 <= dist_from_top_pct <= 0.35
        )

        return ok, {
            "hi_max": hi_max,
            "hi_min": hi_min,
            "high_band_pct": high_band_pct,
            "dist_from_top_pct": dist_from_top_pct,
            "low_early": low_early,
            "low_late": low_late,
        }
    except Exception:
        return False, {}

def detect_big_bull_half_hold(df, bars=10):
    try:
        recent = df.tail(bars)
        if len(recent) < 8:
            return False, {}

        best_idx = None
        best_body = 0.0
        for idx, row in recent.iloc[:-1].iterrows():
            o = float(row["open"])
            c = float(row["close"])
            if c <= o:
                continue
            body = c - o
            if body > best_body:
                best_body = body
                best_idx = idx

        if best_idx is None or best_body <= 0:
            return False, {}

        bull = recent.loc[best_idx]
        o = float(bull["open"])
        c = float(bull["close"])
        half_level = o + (c - o) * 0.5

        after = recent.loc[best_idx:]
        current_price = float(recent["close"].iloc[-1])
        low_after = float(after["low"].min())

        ok = current_price >= half_level and low_after >= half_level * 0.998

        return ok, {
            "half_level": half_level,
            "bull_open": o,
            "bull_close": c,
            "current_price": current_price,
            "low_after": low_after,
        }
    except Exception:
        return False, {}

def detect_fake_breakdown_recovery(df, bars=10):
    try:
        recent = df.tail(bars)
        if len(recent) < 8:
            return False, {}

        support_zone = float(recent.iloc[:-2]["low"].tail(5).min())
        prev = recent.iloc[-2]
        last = recent.iloc[-1]

        prev_low = float(prev["low"])
        last_close = float(last["close"])

        broke = prev_low < support_zone * 0.998
        recovered = last_close >= support_zone * 1.001
        ok = broke and recovered

        return ok, {
            "support_zone": support_zone,
            "prev_low": prev_low,
            "last_close": last_close,
        }
    except Exception:
        return False, {}

def detect_pullback_recheck(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12:
            return False, {}

        part1 = recent.iloc[0:4]
        part2 = recent.iloc[4:8]
        part3 = recent.iloc[8:12]

        rise1 = ((float(part1["close"].iloc[-1]) - float(part1["close"].iloc[0])) / float(part1["close"].iloc[0])) * 100
        pull2 = ((float(part2["close"].iloc[-1]) - float(part2["close"].iloc[0])) / float(part2["close"].iloc[0])) * 100
        recover3 = ((float(part3["close"].iloc[-1]) - float(part3["close"].iloc[0])) / float(part3["close"].iloc[0])) * 100

        high1 = float(part1["high"].max())
        close3 = float(part3["close"].iloc[-1])

        ok = (
            rise1 >= 0.8
            and pull2 <= -0.2
            and recover3 >= 0.35
            and close3 >= high1 * 0.995
        )

        return ok, {
            "rise1": rise1,
            "pull2": pull2,
            "recover3": recover3,
            "high1": high1,
            "close3": close3,
        }
    except Exception:
        return False, {}

def analyze_chart_patterns(df):
    patterns = {}
    tags = []
    bonus_score = 0.0

    hl_ok, hl_meta = detect_higher_lows(df)
    bc_ok, bc_meta = detect_box_top_compression(df)
    bh_ok, bh_meta = detect_big_bull_half_hold(df)
    fb_ok, fb_meta = detect_fake_breakdown_recovery(df)
    pr_ok, pr_meta = detect_pullback_recheck(df)

    patterns["higher_lows"] = hl_ok
    patterns["box_compression"] = bc_ok
    patterns["big_bull_half_hold"] = bh_ok
    patterns["fake_breakdown_recovery"] = fb_ok
    patterns["pullback_recheck"] = pr_ok

    if hl_ok:
        tags.append("저점높임")
    if bc_ok:
        tags.append("상단압축")
    if bh_ok:
        tags.append("양봉절반유지")

    if fb_ok and USE_FAKE_BREAKDOWN_RECOVERY_BONUS:
        tags.append("가짜하락회복")
        bonus_score += 0.45

    if pr_ok and USE_PULLBACK_RECHECK_BONUS:
        tags.append("눌림재확인")
        bonus_score += 0.55

    return {
        "patterns": patterns,
        "tags": tags,
        "bonus_score": bonus_score,
        "meta": {
            "higher_lows": hl_meta,
            "box_compression": bc_meta,
            "big_bull_half_hold": bh_meta,
            "fake_breakdown_recovery": fb_meta,
            "pullback_recheck": pr_meta,
        }
    }

# =========================
# BTC / 시장 상태
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

def get_market_regime():
    df, label = get_btc_df()
    if df is None:
        return {
            "name": "UNKNOWN",
            "label": label or "unknown",
            "btc_change_pct": 0.0,
            "allow_auto_buy": True,
            "allow_breakout": True,
            "message": "시장 상태 판단 실패"
        }

    price = get_price("BTC")
    if price <= 0:
        return {
            "name": "UNKNOWN",
            "label": label or "unknown",
            "btc_change_pct": 0.0,
            "allow_auto_buy": True,
            "allow_breakout": True,
            "message": "시장 상태 판단 실패"
        }

    change_pct = get_recent_change_pct(df, 4)
    ma5v = ma(df, 5)
    ma20v = ma(df, 20)

    if change_pct <= BTC_STRONG_BLOCK_PCT:
        return {
            "name": "BLOCK",
            "label": label,
            "btc_change_pct": change_pct,
            "allow_auto_buy": False,
            "allow_breakout": False,
            "message": "BTC 급락 구간이라 자동매수 쉬는 장"
        }

    if change_pct <= REGIME_WEAK_MAX_ABS_PCT or (ma20v > 0 and price < ma20v * 0.992):
        return {
            "name": "WEAK",
            "label": label,
            "btc_change_pct": change_pct,
            "allow_auto_buy": True,
            "allow_breakout": False,
            "message": "약한 장이라 EARLY / PRE_BREAKOUT 위주"
        }

    if abs(change_pct) <= REGIME_SIDEWAYS_MAX_ABS_PCT:
        return {
            "name": "SIDEWAYS",
            "label": label,
            "btc_change_pct": change_pct,
            "allow_auto_buy": True,
            "allow_breakout": not REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS,
            "message": "횡보 장이라 돌파형은 조심"
        }

    if change_pct >= REGIME_STRONG_UP_PCT and price >= ma5v and price >= ma20v:
        return {
            "name": "STRONG_UP",
            "label": label,
            "btc_change_pct": change_pct,
            "allow_auto_buy": True,
            "allow_breakout": True,
            "message": "강한 상승 장"
        }

    return {
        "name": "NORMAL",
        "label": label,
        "btc_change_pct": change_pct,
        "allow_auto_buy": True,
        "allow_breakout": True,
        "message": "무난한 장"
    }

def analyze_btc_flow():
    regime = get_market_regime()
    df, label = get_btc_df()
    if df is None:
        return "📊 BTC 리포트\nBTC 데이터를 불러오지 못했어."

    price = get_price("BTC")
    if price <= 0:
        return "📊 BTC 리포트\nBTC 현재가 조회 실패."

    change_pct = get_recent_change_pct(df, 4)
    ma5v = ma(df, 5)
    ma20v = ma(df, 20)

    if regime["name"] == "BLOCK":
        state = "🚨 강한 하락"
    elif regime["name"] == "WEAK":
        state = "⚠️ 약한 장"
    elif regime["name"] == "SIDEWAYS":
        state = "😐 횡보"
    elif regime["name"] == "STRONG_UP":
        state = "🔥 강한 상승"
    else:
        state = "👍 보통"

    loc = "단기 이동평균 위" if price >= ma5v and price >= ma20v else "단기 이동평균 아래/혼조"

    return f"""
📊 BTC 리포트 ({label} 기준)

💰 현재가: {fmt_price(price)}
📉 최근 변동률: {change_pct:.2f}%
📌 상태: {state}
📎 현재 위치: {loc}

한줄 해석:
{regime['message']}
"""

# =========================
# 점수 / 리스크
# =========================
def dynamic_stop_loss_pct(vol_ratio, range_pct, strategy):
    stop = BASE_STOP_LOSS_PCT

    if strategy in ["PRE_BREAKOUT", "BREAKOUT", "CHASE"]:
        stop -= 0.05
    elif strategy == "EARLY":
        stop += 0.10

    if vol_ratio >= 4.0:
        stop -= 0.05
    if range_pct >= 4.0:
        stop -= 0.05

    return max(stop, -2.2)

def dynamic_take_profit_pct(vol_ratio, range_pct, strategy):
    tp = BASE_TP_PCT

    if strategy == "EARLY":
        tp += 0.5
    elif strategy == "PRE_BREAKOUT":
        tp += 0.7
    elif strategy == "BREAKOUT":
        tp += 0.7
    elif strategy == "CHASE":
        tp += 0.6

    if vol_ratio >= 4.0:
        tp += 0.3
    if range_pct >= 3.5:
        tp += 0.2

    return min(tp, 6.8)

def dynamic_time_stop_sec(vol_ratio, range_pct, strategy):
    if strategy == "EARLY":
        base = 900
    elif strategy == "PRE_BREAKOUT":
        base = 900
    elif strategy == "BREAKOUT":
        base = 780
    elif strategy == "CHASE":
        base = 660
    else:
        base = 900

    if vol_ratio >= 4.0:
        base -= 60
    elif vol_ratio < 2.0:
        base += 60

    return max(TIME_STOP_MIN_SEC, min(base, TIME_STOP_MAX_SEC))

def score_heat_penalty(rsi):
    if rsi >= 74:
        return 1.3
    if rsi >= 70:
        return 0.9
    if rsi >= 66:
        return 0.5
    return 0.0

def score_entry_bonus(strategy):
    if strategy == "EARLY":
        return 1.15
    if strategy == "PRE_BREAKOUT":
        return 1.05
    if strategy == "BREAKOUT":
        return 0.45
    if strategy == "CHASE":
        return -0.95
    return 0.0

def expected_edge_score(strategy, base_signal_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct):
    risk = abs(float(stop_loss_pct))
    reward = float(take_profit_pct)
    if risk <= 0:
        return -999

    rr = reward / risk

    score = float(base_signal_score)
    score += min(rr, 3.8) * 0.95
    score += min(vol_ratio, 4.5) * 0.32
    score += min(range_pct, 3.5) * 0.18
    score += score_entry_bonus(strategy)
    score -= score_heat_penalty(rsi)

    if strategy in ["EARLY", "PRE_BREAKOUT"] and change_pct <= 1.6:
        score += 0.45
    if strategy == "BREAKOUT" and change_pct >= 2.6:
        score -= 0.55
    if strategy == "CHASE" and change_pct >= 2.8:
        score -= 0.8

    return round(score, 2)

# =========================
# 시장 유니버스 / 공유 캐시
# =========================
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
        if ticker == "BTC":
            continue

        try:
            df3 = get_ohlcv(ticker, "minute3")
            if df3 is None or len(df3) < 30:
                continue

            price = safe_float(df3["close"].iloc[-1], 0)
            if price <= 0:
                continue

            vol_ratio = get_vol_ratio(df3, 3, 15)
            short_change = get_recent_change_pct(df3, 3)
            range_pct = get_range_pct(df3, 8)

            turnover_recent = float(df3["volume"].tail(5).sum()) * price
            turnover_score = math.log10(max(turnover_recent, 1))
            surge_score = min(vol_ratio, 7.0) * 1.8 + min(max(short_change, 0), 4.5) * 1.4 + min(range_pct, 5.0) * 0.7

            rows.append({
                "ticker": ticker,
                "df_3m": df3,
                "last_price": price,
                "turnover_recent": turnover_recent,
                "vol_ratio_3m": vol_ratio,
                "short_change_3m": short_change,
                "range_pct_3m": range_pct,
                "turnover_score": turnover_score,
                "surge_score": surge_score,
            })
        except Exception:
            continue

    if not rows:
        return market_universe

    abs_sorted = sorted(rows, key=lambda x: x["turnover_recent"], reverse=True)[:ABSOLUTE_TURNOVER_CANDIDATES]
    surge_sorted = sorted(rows, key=lambda x: x["surge_score"], reverse=True)[:SURGE_CANDIDATES]

    merged = {}
    for item in abs_sorted + surge_sorted:
        merged[item["ticker"]] = item

    selected = list(merged.values())
    selected.sort(key=lambda x: (x["surge_score"] * 1.15) + (x["turnover_score"] * 0.35), reverse=True)
    selected = selected[:TOP_TICKERS]

    market_universe = selected
    market_universe_time = now_ts
    return market_universe

def build_shared_market_cache(force=False):
    global shared_market_cache, shared_market_cache_time

    now_ts = time.time()
    if (not force) and shared_market_cache and (now_ts - shared_market_cache_time < MARKET_CACHE_TTL_SEC):
        return shared_market_cache

    universe = get_market_universe(force=force)
    if not universe:
        return shared_market_cache

    cache = {}

    for row in universe:
        ticker = row["ticker"]
        try:
            df1 = get_ohlcv(ticker, "minute1")
            if df1 is None or len(df1) < 35:
                continue

            price = safe_float(df1["close"].iloc[-1], 0)
            if price <= 0:
                price = row["last_price"]
            if price <= 0:
                continue

            cache[ticker] = {
                "price": price,
                "df_1m": df1,
                "df_3m": row["df_3m"],
                "turnover": row["turnover_recent"],
                "surge_score": row["surge_score"],
                "vol_ratio_3m": row["vol_ratio_3m"],
                "short_change_3m": row["short_change_3m"],
                "range_pct_3m": row["range_pct_3m"],
            }
        except Exception:
            continue

    if cache:
        shared_market_cache = cache
        shared_market_cache_time = now_ts

    return shared_market_cache

# =========================
# 공용 필터
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

def strong_early_entry_filter(df, change_pct, vol_ratio, rsi):
    if vol_ratio < EARLY_MIN_VOL_RATIO:
        return False
    if change_pct < EARLY_MIN_CHANGE_PCT or change_pct > EARLY_MAX_CHANGE_PCT:
        return False
    if rsi < EARLY_MIN_RSI or rsi > EARLY_MAX_RSI:
        return False
    if upper_wick_too_large(df):
        return False
    if candle_body_ratio(df) < 0.50:
        return False
    if not short_trend_up(df):
        return False
    return True

def late_entry_block(ticker, strategy, df, current_price):
    if not LATE_ENTRY_FILTER_ON:
        return False, ""

    if df is None or len(df) < 10 or current_price <= 0:
        return True, "가격 데이터 부족"

    recent_high = get_recent_high(df, 8, exclude_last=True)
    if recent_high <= 0:
        return False, ""

    dist_to_high_pct = ((recent_high - current_price) / current_price) * 100
    change_5 = get_recent_change_pct(df, 5)
    change_3 = get_recent_change_pct(df, 3)

    if strategy in ["BREAKOUT", "CHASE"] and 0 <= dist_to_high_pct <= LATE_ENTRY_MAX_NEAR_HIGH_PCT:
        if change_3 >= 1.0:
            return True, f"너무 고점 바로 밑이라 늦은 진입 ({dist_to_high_pct:.2f}%)"

    if change_5 > LATE_ENTRY_MAX_5BAR_CHANGE_PCT:
        return True, f"최근 5봉 상승이 너무 커서 늦은 진입 ({change_5:.2f}%)"

    if change_3 > LATE_ENTRY_MAX_3BAR_CHANGE_PCT and strategy in ["BREAKOUT", "CHASE"]:
        return True, f"최근 3봉 상승이 너무 커서 늦은 진입 ({change_3:.2f}%)"

    if strategy == "BREAKOUT":
        extension_pct = ((current_price - recent_high) / recent_high) * 100
        if extension_pct > LATE_ENTRY_MAX_BREAKOUT_EXTENSION_PCT:
            return True, f"돌파 후 이미 너무 위에서 체결될 자리 ({extension_pct:.2f}%)"

    return False, ""

def passes_core_pattern_filter(strategy, pattern_info):
    if not PATTERN_FILTER_ON:
        return True

    p = pattern_info["patterns"]
    hl = p.get("higher_lows", False)
    bc = p.get("box_compression", False)
    bh = p.get("big_bull_half_hold", False)

    if strategy == "EARLY":
        need = (hl if USE_HIGHER_LOWS_CORE else False) or (bh if USE_BIG_BULL_HALF_HOLD_CORE else False)
        return need

    if strategy == "PRE_BREAKOUT":
        left = bc if USE_BOX_COMPRESSION_CORE else True
        right = hl or bh
        return left and right

    if strategy == "BREAKOUT":
        left = (bc if USE_BOX_COMPRESSION_CORE else False) or (bh if USE_BIG_BULL_HALF_HOLD_CORE else False)
        return left

    if strategy == "CHASE":
        return (bc or bh)

    return True

def pattern_bonus_score(pattern_info):
    bonus = 0.0
    p = pattern_info["patterns"]

    if p.get("higher_lows", False):
        bonus += 0.55
    if p.get("box_compression", False):
        bonus += 0.60
    if p.get("big_bull_half_hold", False):
        bonus += 0.65

    bonus += pattern_info.get("bonus_score", 0.0)
    return bonus

def pattern_reason_suffix(pattern_info):
    tags = pattern_info.get("tags", [])
    if not tags:
        return ""
    return "\n- 차트 구조: " + ", ".join(tags)

# =========================
# 전략
# =========================
def analyze_early_entry(ticker: str, data: dict):
    df = data["df_1m"]
    current_price = data["price"]

    if df is None or len(df) < 30 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("EARLY", pattern_info):
        return None

    change_pct = get_recent_change_pct(df, 5)
    vol_ratio = get_vol_ratio(df, 3, 15)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    ma5v = ma(df, 5)
    ma10v = ma(df, 10)
    range_pct = get_range_pct(df, 8)

    if not strong_early_entry_filter(df, change_pct, vol_ratio, rsi):
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if range_pct < EARLY_MIN_RANGE_PCT:
        return None

    block, _ = late_entry_block(ticker, "EARLY", df, current_price)
    if block:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_close = float(last["close"])
    prev_close = float(prev["close"])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if last_jump_pct < 0.10:
        return None

    base_score = 5.8 + min(vol_ratio, 4.2) * 0.9 + min(change_pct, 2.4) * 0.8 + min(data.get("surge_score", 0), 8) * 0.18
    base_score += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "EARLY")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "EARLY")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "EARLY")
    edge_score = expected_edge_score("EARLY", base_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

    return {
        "ticker": ticker,
        "strategy": "EARLY",
        "strategy_label": "초반 선점형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "signal_score": r(base_score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "invalid_break_level": 0.0,
        "pattern_tags": pattern_info["tags"],
        "reference_high": get_recent_high(df, 8, exclude_last=True),
        "reference_low": get_recent_low(df, 8, exclude_last=False),
        "reason": (
            f"- 이제 막 오르기 시작했어\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}\n"
            f"- 초입 흐름이 살아있어"
            f"{pattern_reason_suffix(pattern_info)}"
        )
    }

def analyze_prepump_entry(ticker: str, data: dict):
    df = data["df_3m"]
    current_price = data["price"]

    if df is None or len(df) < 30 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)

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

    score = 4.9 + min(vol_ratio, 4.0) * 0.8 + min(data.get("surge_score", 0), 8) * 0.12
    score += pattern_info.get("bonus_score", 0)

    return {
        "ticker": ticker,
        "strategy": "PREPUMP",
        "strategy_label": "상승 시작형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "signal_score": r(score, 2),
        "edge_score": r(score, 2),
        "pattern_tags": pattern_info["tags"],
    }

def analyze_pullback_entry(ticker: str, data: dict):
    df = data["df_3m"]
    current_price = data["price"]

    if df is None or len(df) < 35 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)

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

    score = 4.8 + vol_ratio * 0.8 + pattern_info.get("bonus_score", 0.0)

    return {
        "ticker": ticker,
        "strategy": "PULLBACK",
        "strategy_label": "눌림 반등형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(rebound_pct, 2),
        "signal_score": r(score, 2),
        "edge_score": r(score, 2),
        "pattern_tags": pattern_info["tags"],
    }

def analyze_pre_breakout_entry(ticker: str, data: dict):
    df = data["df_1m"]
    current_price = data["price"]

    if df is None or len(df) < 35 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("PRE_BREAKOUT", pattern_info):
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

    block, _ = late_entry_block(ticker, "PRE_BREAKOUT", df, current_price)
    if block:
        return None

    recent_high = get_recent_high(df, 8, exclude_last=True)
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

    base_score = 6.0 + min(vol_ratio, 4.5) * 0.85 + (PRE_BREAKOUT_MAX_GAP_PCT - gap_to_break) * 3.4 + min(data.get("surge_score", 0), 8) * 0.16
    base_score += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "PRE_BREAKOUT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "PRE_BREAKOUT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "PRE_BREAKOUT")
    edge_score = expected_edge_score("PRE_BREAKOUT", base_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

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
        "signal_score": r(base_score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "invalid_break_level": recent_high,
        "pattern_tags": pattern_info["tags"],
        "reference_high": recent_high,
        "reference_low": get_recent_low(df, 8, exclude_last=False),
        "reason": (
            f"- 거의 돌파 직전이야\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- 직전 고점까지 남은 거리 {gap_to_break:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}"
        )
    }

def analyze_breakout_entry(ticker: str, data: dict):
    df = data["df_1m"]
    current_price = data["price"]

    if df is None or len(df) < 40 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("BREAKOUT", pattern_info):
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

    recent_high = get_recent_high(df, 10, exclude_last=True)
    if recent_high <= 0:
        return None

    break_pct = ((current_price - recent_high) / recent_high) * 100
    if break_pct < BREAKOUT_BREAK_PCT:
        return None

    block, _ = late_entry_block(ticker, "BREAKOUT", df, current_price)
    if block:
        return None

    base_score = 6.0 + min(vol_ratio, 5.0) * 0.80 + min(change_pct, 3.4) * 0.65 + min(break_pct, 0.7) * 1.25 + min(data.get("surge_score", 0), 8) * 0.12
    base_score += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "BREAKOUT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "BREAKOUT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "BREAKOUT")
    edge_score = expected_edge_score("BREAKOUT", base_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

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
        "signal_score": r(base_score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "invalid_break_level": recent_high,
        "pattern_tags": pattern_info["tags"],
        "reference_high": recent_high,
        "reference_low": get_recent_low(df, 8, exclude_last=False),
        "reason": (
            f"- 힘 있게 고점 돌파 중이야\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- 돌파폭 {break_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}"
        )
    }

def analyze_chase_entry(ticker: str, data: dict):
    if not ALLOW_CHASE:
        return None

    df = data["df_1m"]
    current_price = data["price"]

    if df is None or len(df) < 40 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)
    if not passes_core_pattern_filter("CHASE", pattern_info):
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

    block, _ = late_entry_block(ticker, "CHASE", df, current_price)
    if block:
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

    base_score = 5.0 + min(vol_ratio, 6.0) * 0.65 + min(change_pct, 3.4) * 0.52 + min(data.get("surge_score", 0), 8) * 0.10
    base_score += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "CHASE")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "CHASE")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "CHASE")
    edge_score = expected_edge_score("CHASE", base_score, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

    return {
        "ticker": ticker,
        "strategy": "CHASE",
        "strategy_label": "추격형",
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2),
        "range_pct": r(range_pct, 2),
        "signal_score": r(base_score, 2),
        "edge_score": r(edge_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "invalid_break_level": 0.0,
        "pattern_tags": pattern_info["tags"],
        "reference_high": get_recent_high(df, 8, exclude_last=True),
        "reference_low": get_recent_low(df, 8, exclude_last=False),
        "reason": (
            f"- 이미 오르는 중인데 힘이 아주 강해\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}"
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

def build_position(signal, filled_entry, filled_qty, used_krw):
    return {
        "ticker": signal["ticker"],
        "strategy": signal["strategy"],
        "strategy_label": signal["strategy_label"],
        "entry_price": filled_entry,
        "qty": filled_qty,
        "used_krw": used_krw,
        "stop_loss_pct": signal["stop_loss_pct"],
        "take_profit_pct": signal["take_profit_pct"],
        "time_stop_sec": signal["time_stop_sec"],
        "entered_at": time.time(),
        "peak_price": filled_entry,
        "breakeven_armed": False,
        "trailing_armed": False,
        "reason": signal["reason"],
        "edge_score": signal.get("edge_score", 0),
        "managed_by_bot": True,
        "invalid_break_level": safe_float(signal.get("invalid_break_level", 0)),
        "entry_strategy_snapshot": signal["strategy"],
        "pattern_tags": signal.get("pattern_tags", []),
    }

def buy_market(signal):
    ticker = signal["ticker"]
    entry_price = get_price(ticker)
    if entry_price <= 0:
        return False, "현재가를 불러오지 못했어"

    before_qty = get_balance(ticker)
    before_krw = get_krw_balance()
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

            after_qty = get_balance(ticker)
            after_krw = get_krw_balance()

            filled_qty = round(after_qty - before_qty, 8)
            krw_used_real = max(before_krw - after_krw, 0)

            if filled_qty <= 0:
                last_error = f"체결 확인이 안 됐어 / 응답: {result}"
                continue

            if krw_used_real > 0:
                filled_entry = krw_used_real / filled_qty
            else:
                filled_entry = entry_price

            active_positions[ticker] = build_position(signal, filled_entry, filled_qty, krw_used_real)
            save_positions()

            add_log({
                "time": int(time.time()),
                "type": "BUY",
                "ticker": ticker,
                "strategy": signal["strategy"],
                "strategy_label": signal["strategy_label"],
                "entry": filled_entry,
                "qty": filled_qty,
                "used_krw": krw_used_real,
                "stop_loss_pct": signal["stop_loss_pct"],
                "take_profit_pct": signal["take_profit_pct"],
                "time_stop_sec": signal["time_stop_sec"],
                "edge_score": signal.get("edge_score"),
                "reason": signal["reason"],
                "managed_by_bot": True,
                "invalid_break_level": signal.get("invalid_break_level", 0),
                "pattern_tags": signal.get("pattern_tags", []),
            })

            return True, {
                "entry": filled_entry,
                "qty": filled_qty,
                "used_krw": krw_used_real,
            }

        except Exception as e:
            last_error = str(e)

    return False, f"매수는 시도했지만 주문 금액이 빡빡해서 실패했어 / 마지막 사유: {last_error}"

# =========================
# 매도 / pending sell
# =========================
def confirm_sell_filled(ticker: str, before_balance: float, retries: int = 6, sleep_sec: float = 1.0):
    for _ in range(retries):
        time.sleep(sleep_sec)
        after_balance = get_balance(ticker)
        if after_balance < before_balance * 0.2:
            return True, after_balance
    return False, get_balance(ticker)

def make_pending_sell_record(ticker, reason_type, pos, current_price, pnl_pct, held_sec):
    pending_sells[ticker] = {
        "time": int(time.time()),
        "reason_type": reason_type,
        "ticker": ticker,
        "strategy": pos["strategy"],
        "strategy_label": pos["strategy_label"],
        "entry": float(pos["entry_price"]),
        "exit_guess": float(current_price),
        "pnl_pct_guess": round(float(pnl_pct), 2),
        "held_sec": int(held_sec),
        "edge_score": pos.get("edge_score", 0),
    }
    save_pending_sells()

def finalize_pending_sell_if_closed(ticker):
    if ticker not in pending_sells:
        return False

    bal = get_balance(ticker)
    if bal > 0.0000001:
        return False

    data = pending_sells[ticker]
    add_log({
        "time": int(time.time()),
        "type": data["reason_type"],
        "ticker": data["ticker"],
        "strategy": data["strategy"],
        "strategy_label": data["strategy_label"],
        "entry": data["entry"],
        "exit": data["exit_guess"],
        "pnl_pct": data["pnl_pct_guess"],
        "held_sec": data["held_sec"],
        "edge_score": data["edge_score"],
        "note": "pending sell 보정 로그",
    })

    active_positions.pop(ticker, None)
    pending_sells.pop(ticker, None)
    save_positions()
    save_pending_sells()
    clear_position_file_if_empty()
    return True

def check_pending_sells():
    for ticker in list(pending_sells.keys()):
        try:
            if finalize_pending_sell_if_closed(ticker):
                send(f"🧾 매도 확인 지연 보정 완료\n{ticker} 종료 로그를 나중에 확인해서 반영했어.")
        except Exception as e:
            print(f"[pending sell 확인 오류] {ticker} / {e}")

# =========================
# 후보 승격 매수
# =========================
def cleanup_pending_buy_candidates():
    changed = False
    now_ts = time.time()

    for ticker in list(pending_buy_candidates.keys()):
        item = pending_buy_candidates[ticker]

        if ticker in active_positions:
            pending_buy_candidates.pop(ticker, None)
            changed = True
            continue

        created_at = safe_float(item.get("created_at", 0))
        if created_at <= 0 or now_ts - created_at > PENDING_BUY_TTL_SEC:
            pending_buy_candidates.pop(ticker, None)
            changed = True
            continue

    if len(pending_buy_candidates) > PENDING_BUY_MAX_ITEMS:
        sorted_items = sorted(
            pending_buy_candidates.items(),
            key=lambda kv: safe_float(kv[1].get("edge_score", 0)),
            reverse=True
        )
        keep = dict(sorted_items[:PENDING_BUY_MAX_ITEMS])
        pending_buy_candidates.clear()
        pending_buy_candidates.update(keep)
        changed = True

    if changed:
        save_pending_buys()

def candidate_eligible_for_store(signal, regime):
    if not PENDING_BUY_ON:
        return False

    if signal["strategy"] not in ["EARLY", "PRE_BREAKOUT", "BREAKOUT", "CHASE"]:
        return False
    if signal["strategy"] == "CHASE" and not ALLOW_CHASE:
        return False
    if not strategy_allowed_in_regime(signal["strategy"], regime):
        return False

    edge = safe_float(signal.get("edge_score", 0))
    score = safe_float(signal.get("signal_score", 0))
    if edge < PENDING_BUY_MIN_EDGE and score < PENDING_BUY_MIN_SCORE:
        return False

    return True

def add_or_refresh_pending_buy_candidate(signal, regime):
    if not candidate_eligible_for_store(signal, regime):
        return

    ticker = signal["ticker"]
    now_ts = time.time()

    data = {
        "ticker": ticker,
        "strategy": signal["strategy"],
        "strategy_label": signal["strategy_label"],
        "created_at": now_ts,
        "last_seen_at": now_ts,
        "first_price": safe_float(signal.get("current_price", 0)),
        "edge_score": safe_float(signal.get("edge_score", 0)),
        "signal_score": safe_float(signal.get("signal_score", 0)),
        "reference_high": safe_float(signal.get("reference_high", 0)),
        "reference_low": safe_float(signal.get("reference_low", 0)),
        "pattern_tags": signal.get("pattern_tags", []),
    }

    if ticker in pending_buy_candidates:
        old = pending_buy_candidates[ticker]
        data["created_at"] = safe_float(old.get("created_at", now_ts))
        if safe_float(data["edge_score"]) < safe_float(old.get("edge_score", 0)):
            data["edge_score"] = safe_float(old.get("edge_score", 0))
        if not data["reference_high"]:
            data["reference_high"] = safe_float(old.get("reference_high", 0))
        if not data["reference_low"]:
            data["reference_low"] = safe_float(old.get("reference_low", 0))

    pending_buy_candidates[ticker] = data
    cleanup_pending_buy_candidates()
    save_pending_buys()

def update_pending_buy_candidates_from_results(results, regime):
    for signal in results:
        if should_auto_buy_signal(signal, regime=regime):
            continue
        add_or_refresh_pending_buy_candidate(signal, regime)

def candidate_promote_ok(candidate, current_signal, regime):
    ticker = candidate["ticker"]

    if ticker in active_positions:
        return False, "이미 보유중"
    if len(active_positions) >= MAX_HOLDINGS:
        return False, "보유 제한"
    if not cooldown_ok(ticker):
        return False, "쿨다운"
    if not strategy_allowed_in_regime(current_signal["strategy"], regime):
        return False, "장 필터"
    if not should_auto_buy_signal(current_signal, regime=regime):
        return False, "자동매수 기준 미달"

    current_price = safe_float(current_signal.get("current_price", 0))
    reference_high = safe_float(candidate.get("reference_high", 0))
    reference_low = safe_float(candidate.get("reference_low", 0))

    if current_price <= 0:
        return False, "가격 오류"

    if reference_high > 0:
        if current_price < reference_high * (PROMOTE_RECOVERY_TO_HIGH_PCT / 100):
            return False, "고점 회복 부족"

        extension_pct = ((current_price - reference_high) / reference_high) * 100
        if extension_pct > PROMOTE_MAX_BREAKOUT_EXTENSION_PCT:
            return False, "재확인 후 너무 위"

    if reference_low > 0 and current_price < reference_low * 0.998:
        return False, "후보 저점 이탈"

    ticker_cache = shared_market_cache.get(ticker)
    if not ticker_cache:
        return False, "캐시 없음"

    df = ticker_cache.get("df_1m")
    if df is None or len(df) < 12:
        return False, "1분봉 부족"

    vol_ratio_now = get_vol_ratio(df, 2, 8)
    if vol_ratio_now < PROMOTE_MIN_VOL_RATIO:
        return False, "거래량 재확인 부족"

    block, reason = late_entry_block(ticker, current_signal["strategy"], df, current_price)
    if block:
        return False, reason

    return True, "승격 가능"

def process_pending_buy_promotions(shared_cache=None):
    if not PENDING_BUY_ON:
        return
    if not AUTO_BUY or not is_auto_time():
        return
    if len(active_positions) >= MAX_HOLDINGS:
        return

    cleanup_pending_buy_candidates()
    if not pending_buy_candidates:
        return

    regime = get_market_regime()
    if REGIME_FILTER_ON and not regime["allow_auto_buy"]:
        return

    cache = shared_cache or build_shared_market_cache()
    if not cache:
        return

    candidates_sorted = sorted(
        pending_buy_candidates.values(),
        key=lambda x: safe_float(x.get("edge_score", 0)),
        reverse=True
    )

    for item in candidates_sorted:
        ticker = item["ticker"]
        created_at = safe_float(item.get("created_at", 0))
        if time.time() - created_at < PENDING_BUY_RECHECK_MIN_SEC:
            continue

        if ticker not in cache:
            continue

        data = cache[ticker]

        current_signal = None
        for analyzer in [analyze_early_entry, analyze_pre_breakout_entry, analyze_breakout_entry, analyze_chase_entry]:
            try:
                sig = analyzer(ticker, data)
                if sig and sig["strategy"] == item.get("strategy"):
                    current_signal = sig
                    break
            except Exception:
                continue

        if not current_signal:
            continue

        ok, _ = candidate_promote_ok(item, current_signal, regime)
        if not ok:
            continue

        success, result = buy_market(current_signal)
        if success:
            recent_signal_alerts[ticker] = time.time()
            pending_buy_candidates.pop(ticker, None)
            save_pending_buys()

            tag_text = ""
            if current_signal.get("pattern_tags"):
                tag_text = "\n📐 차트 구조: " + ", ".join(current_signal["pattern_tags"])

            send(
                f"""
🚀 후보 승격 후 자동매수 완료

📊 {ticker}
방식: {current_signal['strategy_label']}

💰 매수가(보정): {fmt_price(result['entry'])}
📦 수량: {result['qty']:.6f}
💵 사용한 금액(대략): {fmt_price(result['used_krw'])}

🛑 손절 기준: {fmt_pct(current_signal['stop_loss_pct'])}
🎯 목표 수익: {fmt_pct(current_signal['take_profit_pct'])}
⏱ 오래 안 가면 정리: {int(current_signal['time_stop_sec'] / 60)}분
📈 수익 기대 점수: {current_signal['edge_score']:.2f}{tag_text}

처음엔 후보였는데,
2차 확인 후 다시 강해져서 들어갔어.
"""
            )
            return
        else:
            send(
                f"""
❌ 후보 승격 자동매수 실패

📊 {ticker}
방식: {current_signal['strategy_label']}

사유:
{result}
"""
            )
            return

# =========================
# 포지션 복구 / 매도
# =========================
def sell_market_confirmed(ticker: str, reason_type: str, pos: dict, current_price: float, pnl_pct: float, held_sec: int = 0):
    try:
        before_balance = get_balance(ticker)
        before_krw = get_krw_balance()

        if before_balance <= 0:
            return False, "잔고가 없어서 매도할 수 없었어"

        result = bithumb.sell_market_order(ticker, before_balance)
        ok, after_balance = confirm_sell_filled(ticker, before_balance)

        after_krw = get_krw_balance()

        if not ok:
            make_pending_sell_record(ticker, reason_type, pos, current_price, pnl_pct, held_sec)
            return False, f"매도 요청은 갔지만 체결 확인이 늦어졌어 / 응답: {result}"

        sold_qty = max(before_balance - after_balance, 0)
        krw_gained = max(after_krw - before_krw, 0)

        if sold_qty > 0 and krw_gained > 0:
            exit_price = krw_gained / sold_qty
            pnl_pct_real = ((exit_price - float(pos["entry_price"])) / float(pos["entry_price"])) * 100
        else:
            exit_price = current_price
            pnl_pct_real = pnl_pct

        add_log({
            "time": int(time.time()),
            "type": reason_type,
            "ticker": ticker,
            "strategy": pos["strategy"],
            "strategy_label": pos["strategy_label"],
            "entry": float(pos["entry_price"]),
            "exit": exit_price,
            "pnl_pct": round(pnl_pct_real, 2),
            "held_sec": int(held_sec),
            "edge_score": pos.get("edge_score", 0),
        })

        active_positions.pop(ticker, None)
        pending_sells.pop(ticker, None)
        save_positions()
        save_pending_sells()
        clear_position_file_if_empty()

        return True, "매도 완료"
    except Exception as e:
        return False, str(e)

def find_last_open_buy_log(ticker):
    last_buy = None
    last_close_time = 0

    for log in trade_logs:
        if log.get("ticker") != ticker:
            continue
        if log.get("type") == "BUY" and log.get("managed_by_bot", False):
            if (last_buy is None) or int(log.get("time", 0)) > int(last_buy.get("time", 0)):
                last_buy = log
        elif log.get("type") in ["TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP", "SCENARIO_FAIL"]:
            last_close_time = max(last_close_time, int(log.get("time", 0)))

    if last_buy and int(last_buy.get("time", 0)) > last_close_time:
        return last_buy
    return None

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
            last_buy = find_last_open_buy_log(ticker)
            if not last_buy:
                continue

            qty = get_balance(ticker)
            price = get_price(ticker)

            if qty <= 0 or price <= 0:
                continue
            if qty * price < DUST_KEEP_MIN_KRW:
                continue

            entry_price = float(last_buy.get("entry", price))
            strategy = last_buy.get("strategy", "RECOVER")
            strategy_label = last_buy.get("strategy_label", "복구 포지션")
            stop_loss_pct = float(last_buy.get("stop_loss_pct", BASE_STOP_LOSS_PCT))
            take_profit_pct = float(last_buy.get("take_profit_pct", BASE_TP_PCT))
            time_stop_sec = int(last_buy.get("time_stop_sec", 900))
            edge_score = float(last_buy.get("edge_score", 0))
            reason = last_buy.get("reason", "이전 BUY 로그 기반 복구")
            entered_at = int(last_buy.get("time", int(time.time())))
            used_krw = float(last_buy.get("used_krw", qty * entry_price))

            active_positions[ticker] = {
                "ticker": ticker,
                "strategy": strategy,
                "strategy_label": strategy_label,
                "entry_price": entry_price,
                "qty": qty,
                "used_krw": used_krw,
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "time_stop_sec": time_stop_sec,
                "entered_at": entered_at,
                "peak_price": max(entry_price, price),
                "breakeven_armed": False,
                "trailing_armed": False,
                "reason": reason,
                "edge_score": edge_score,
                "managed_by_bot": True,
                "invalid_break_level": safe_float(last_buy.get("invalid_break_level", 0)),
                "entry_strategy_snapshot": strategy,
                "pattern_tags": last_buy.get("pattern_tags", []),
            }
            recovered += 1
        except Exception:
            continue

    if recovered > 0:
        save_positions()
        send(f"♻️ 포지션 복구 완료\n최근 BUY 로그 기준으로 복구된 코인 수: {recovered}")

# =========================
# 시그널 평가 / watch 알림 판단
# =========================
def signal_score(signal):
    return float(signal.get("edge_score", signal.get("signal_score", 0)))

def strategy_allowed_in_regime(strategy, regime):
    if not REGIME_FILTER_ON:
        return True

    if regime["name"] == "BLOCK":
        return False

    if strategy == "BREAKOUT" and not regime.get("allow_breakout", True):
        return False

    if strategy == "CHASE" and regime["name"] in ["SIDEWAYS", "WEAK"]:
        return False

    return True

def should_auto_buy_signal(signal, regime=None):
    strategy = signal["strategy"]
    edge = float(signal.get("edge_score", 0))
    tp = float(signal.get("take_profit_pct", 0))

    if strategy not in ["EARLY", "PRE_BREAKOUT", "BREAKOUT", "CHASE"]:
        return False
    if strategy == "CHASE" and not ALLOW_CHASE:
        return False
    if tp < MIN_EXPECTED_TP_PCT:
        return False
    if edge < MIN_EXPECTED_EDGE_SCORE:
        return False
    if regime and not strategy_allowed_in_regime(strategy, regime):
        return False
    return True

def cooldown_ok(ticker):
    last = recent_signal_alerts.get(ticker, 0)
    return (time.time() - last) >= 300

def is_ticker_blocked_for_watch_alert(ticker: str) -> bool:
    return (
        ticker in active_positions
        or ticker in pending_buy_candidates
        or ticker in pending_sells
    )

def get_strategy_rank(strategy: str) -> int:
    rank_map = {
        "PREPUMP": 1,
        "PULLBACK": 2,
        "EARLY": 3,
        "PRE_BREAKOUT": 4,
        "BREAKOUT": 5,
        "CHASE": 6,
    }
    return rank_map.get(strategy, 0)

def build_watch_snapshot(item: dict):
    return {
        "strategy": item.get("strategy", ""),
        "strategy_label": item.get("strategy_label", ""),
        "change_pct": safe_float(item.get("change_pct", 0)),
        "vol_ratio": safe_float(item.get("vol_ratio", 0)),
        "signal_score": safe_float(item.get("signal_score", 0)),
        "pattern_tags": list(item.get("pattern_tags", [])),
        "price": safe_float(item.get("current_price", 0)),
    }

def compare_watch_improvement(prev_snap: dict, item: dict):
    reasons = []

    prev_vol = safe_float(prev_snap.get("vol_ratio", 0))
    new_vol = safe_float(item.get("vol_ratio", 0))
    if new_vol - prev_vol >= WATCH_VOL_IMPROVE_DELTA:
        reasons.append(f"거래량 증가 ({prev_vol:.2f}배 → {new_vol:.2f}배)")

    prev_change = safe_float(prev_snap.get("change_pct", 0))
    new_change = safe_float(item.get("change_pct", 0))
    if new_change - prev_change >= WATCH_CHANGE_IMPROVE_DELTA:
        reasons.append(f"상승률 강화 ({prev_change:.2f}% → {new_change:.2f}%)")

    prev_score = safe_float(prev_snap.get("signal_score", 0))
    new_score = safe_float(item.get("signal_score", 0))
    if new_score - prev_score >= WATCH_SCORE_IMPROVE_DELTA:
        reasons.append(f"점수 상승 ({prev_score:.2f} → {new_score:.2f})")

    prev_strategy = prev_snap.get("strategy", "")
    new_strategy = item.get("strategy", "")
    if get_strategy_rank(new_strategy) > get_strategy_rank(prev_strategy):
        reasons.append(
            f"전략 승격 ({prev_snap.get('strategy_label', prev_strategy)} → {item.get('strategy_label', new_strategy)})"
        )

    prev_tags = set(prev_snap.get("pattern_tags", []))
    new_tags = set(item.get("pattern_tags", []))
    added_tags = [x for x in item.get("pattern_tags", []) if x in (new_tags - prev_tags)]
    if added_tags:
        reasons.append("차트 구조 추가 (" + ", ".join(added_tags) + ")")

    return reasons

def should_send_watch_alert(ticker: str, item: dict, now_ts: float):
    if is_ticker_blocked_for_watch_alert(ticker):
        return False, "", ""

    prev_time = recent_watch_alerts.get(ticker, 0)
    prev_snap = recent_watch_snapshots.get(ticker)

    if not prev_snap or prev_time <= 0:
        return True, "new", ""

    reasons = compare_watch_improvement(prev_snap, item)
    if reasons:
        return True, "upgrade", " / ".join(reasons)

    if now_ts - prev_time >= WATCH_RENOTICE_SEC:
        return True, "renotice", "시간이 지나 다시 확인 알림"

    return False, "", ""

def save_watch_snapshot(ticker: str, item: dict, now_ts: float):
    recent_watch_alerts[ticker] = now_ts
    recent_watch_snapshots[ticker] = build_watch_snapshot(item)

# =========================
# 후보 추출
# =========================
def collect_signals_from_cache(cache, auto_only=False, regime=None):
    results = []

    auto_analyzers = [
        analyze_early_entry,
        analyze_pre_breakout_entry,
        analyze_breakout_entry,
        analyze_chase_entry,
    ]

    watch_analyzers = [
        analyze_early_entry,
        analyze_pre_breakout_entry,
        analyze_breakout_entry,
        analyze_prepump_entry,
        analyze_pullback_entry,
    ]

    analyzers = auto_analyzers if auto_only else watch_analyzers

    for ticker, data in cache.items():
        for analyzer in analyzers:
            try:
                signal = analyzer(ticker, data)
                if not signal:
                    continue

                if regime and not strategy_allowed_in_regime(signal["strategy"], regime):
                    continue

                if auto_only and not should_auto_buy_signal(signal, regime=regime):
                    continue

                results.append(signal)
            except Exception:
                continue

    return results

def dedupe_best_signal_per_ticker(results, key_name="signal_score"):
    best_per_ticker = {}
    for signal in results:
        t = signal["ticker"]
        s = float(signal.get(key_name, 0))
        if t not in best_per_ticker or s > float(best_per_ticker[t].get(key_name, 0)):
            best_per_ticker[t] = signal
    return list(best_per_ticker.values())

# =========================
# 후보 알림
# =========================
def scan_watchlist(shared_cache=None):
    cache = shared_cache or build_shared_market_cache()
    if not cache:
        return

    regime = get_market_regime()
    results = collect_signals_from_cache(cache, auto_only=False, regime=regime)
    if not results:
        return

    update_pending_buy_candidates_from_results(results, regime)

    unique_results = dedupe_best_signal_per_ticker(results, key_name="signal_score")
    unique_results.sort(key=lambda x: float(x.get("signal_score", 0)), reverse=True)
    top = unique_results[:5]

    new_lines = []
    upgrade_lines = []
    renotice_lines = []

    now_ts = time.time()

    for item in top:
        ticker = item["ticker"]

        send_ok, alert_type, reason_text = should_send_watch_alert(ticker, item, now_ts)
        if not send_ok:
            continue

        tag_text = ""
        tags = item.get("pattern_tags", [])
        if tags:
            tag_text = f" / 구조 {','.join(tags[:3])}"

        base_line = (
            f"• {ticker} / {fmt_price(item['current_price'])} / 상승 {float(item.get('change_pct', 0)):.2f}% "
            f"/ 거래량 {float(item.get('vol_ratio', 0)):.2f}배 / 방식 {item['strategy_label']}{tag_text}"
        )

        if alert_type == "upgrade":
            upgrade_lines.append(base_line + f"\n  ↳ 강화 이유: {reason_text}")
        elif alert_type == "renotice":
            renotice_lines.append(base_line + f"\n  ↳ 재확인: {reason_text}")
        else:
            new_lines.append(base_line)

        save_watch_snapshot(ticker, item, now_ts)

    if new_lines:
        send("👀 빠른 후보 알림\n\n" + "\n\n".join(new_lines))

    if upgrade_lines:
        send("🔁 후보 강화 알림\n\n" + "\n\n".join(upgrade_lines))

    if renotice_lines:
        send("🕒 후보 재확인 알림\n\n" + "\n\n".join(renotice_lines))

# =========================
# 자동 진입
# =========================
def scan_and_auto_trade(shared_cache=None):
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

    regime = get_market_regime()
    if REGIME_FILTER_ON and not regime["allow_auto_buy"]:
        print(f"[쉬는 장] {regime['message']}")
        return

    cache = shared_cache or build_shared_market_cache()
    if not cache:
        return

    candidates = collect_signals_from_cache(cache, auto_only=True, regime=regime)
    if not candidates:
        return

    candidates = dedupe_best_signal_per_ticker(candidates, key_name="edge_score")
    candidates.sort(key=signal_score, reverse=True)
    best = candidates[0]
    ticker = best["ticker"]

    if ticker in pending_buy_candidates:
        return

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

    tag_text = ""
    if best.get("pattern_tags"):
        tag_text = "\n📐 차트 구조: " + ", ".join(best["pattern_tags"])

    send(
        f"""
🔥 자동매수 완료

📊 {ticker}
방식: {best['strategy_label']}

💰 매수가(보정): {fmt_price(result['entry'])}
📦 수량: {result['qty']:.6f}
💵 사용한 금액(대략): {fmt_price(result['used_krw'])}

🛑 손절 기준: {fmt_pct(best['stop_loss_pct'])}
🎯 목표 수익: {fmt_pct(best['take_profit_pct'])}
⏱ 오래 안 가면 정리: {int(best['time_stop_sec'] / 60)}분
📈 수익 기대 점수: {best['edge_score']:.2f}{tag_text}

매수 이유
{best['reason']}
"""
    )

# =========================
# 시나리오 실패 빠른 정리
# =========================
def should_scenario_fail_exit(ticker, pos, current_price):
    if not SCENARIO_EXIT_ON:
        return False, ""

    held_sec = int(time.time() - int(pos["entered_at"]))
    if held_sec < SCENARIO_MIN_HOLD_SEC or held_sec > SCENARIO_MAX_HOLD_SEC:
        return False, ""

    entry_price = float(pos["entry_price"])
    if entry_price <= 0 or current_price <= 0:
        return False, ""

    pnl_pct = ((current_price - entry_price) / entry_price) * 100
    if pnl_pct >= SCENARIO_MIN_PROGRESS_PCT:
        return False, ""

    ticker_cache = shared_market_cache.get(ticker)
    if not ticker_cache:
        return False, ""

    df = ticker_cache.get("df_1m")
    if df is None or len(df) < 20:
        return False, ""

    vol_ratio_now = get_vol_ratio(df, 2, 8)
    invalid_break_level = safe_float(pos.get("invalid_break_level", 0.0))
    strategy = pos.get("strategy", "")

    if strategy in ["BREAKOUT", "PRE_BREAKOUT"] and invalid_break_level > 0:
        if current_price <= invalid_break_level * (1 - SCENARIO_FAIL_DROP_FROM_BREAK_LEVEL_PCT / 100):
            if vol_ratio_now < max(SCENARIO_WEAK_VOL_RATIO_THRESHOLD, 1.0):
                return True, "돌파 기준선 아래로 다시 밀렸고 거래량도 약해"

    if strategy == "EARLY":
        if pnl_pct <= SCENARIO_FAIL_DROP_FROM_ENTRY_PCT and vol_ratio_now < SCENARIO_WEAK_VOL_RATIO_THRESHOLD:
            return True, "초반 흐름을 기대했는데 바로 힘이 약해졌어"

    if strategy == "CHASE":
        if pnl_pct <= -0.35:
            return True, "추격 진입 후 바로 탄력이 약했어"

    return False, ""

def build_time_stop_comment(pnl_pct: float) -> str:
    if pnl_pct >= 0.2:
        return "짧게 수익이 났지만 크게 뻗는 힘은 약해서 정리했어."
    if pnl_pct > -0.15:
        return "빠르게 안 뻗어서 거의 본전 근처에서 정리했어."
    return "힘이 약해서 정리했어."

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
                if ticker in pending_sells:
                    continue
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

            if market_value < MIN_ORDER_KRW:
                continue

            fail_exit, fail_reason = should_scenario_fail_exit(ticker, pos, current_price)
            if fail_exit:
                ok, msg = sell_market_confirmed(ticker, "SCENARIO_FAIL", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(
                        f"""
⚠️ 예상한 흐름이 깨져서 빠른 정리

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
⏰ 보유시간: {int(held_sec/60)}분

사유:
{fail_reason}
"""
                    )
                else:
                    send(
                        f"""
⚠️ 빠른 정리 시도했지만 확인 지연

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                    )
                continue

            if held_sec >= pos["time_stop_sec"]:
                if pnl_pct < 0.8:
                    ok, msg = sell_market_confirmed(ticker, "TIME_STOP", pos, current_price, pnl_pct, held_sec)
                    if ok:
                        comment = build_time_stop_comment(pnl_pct)
                        send(
                            f"""
⏱ 오래 안 가서 정리

📊 {ticker}
방식: {pos['strategy_label']}

💰 현재가: {fmt_price(current_price)}
📈 수익률: {fmt_pct(pnl_pct)}
⏰ 보유시간: {int(held_sec/60)}분

{comment}
"""
                        )
                    else:
                        send(
                            f"""
⚠️ 정리 시도했지만 확인 지연

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                        )
                    continue

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
⚠️ 손절 시도했지만 확인 지연

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                    )
                continue

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
⚠️ 본절 정리 시도했지만 확인 지연

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                        )
                    continue

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
⚠️ 익절 시도했지만 확인 지연

📊 {ticker}
방식: {pos['strategy_label']}

사유:
{msg}
"""
                        )
                    continue

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
⚠️ 익절 시도했지만 확인 지연

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
# 통계
# =========================
CLOSE_TYPES = {"TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP", "SCENARIO_FAIL"}

def get_closed_logs_all():
    return [x for x in trade_logs if x.get("type") in CLOSE_TYPES]

def get_closed_logs_today():
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    result = []
    for x in trade_logs:
        if x.get("type") not in CLOSE_TYPES:
            continue
        ts = int(x.get("time", 0))
        d = datetime.fromtimestamp(ts, tz).date()
        if d == today:
            result.append(x)
    return result

def summarize_logs(logs):
    if not logs:
        return None
    total = len(logs)
    wins = len([x for x in logs if float(x.get("pnl_pct", 0)) > 0])
    losses = total - wins
    total_pnl = sum(float(x.get("pnl_pct", 0)) for x in logs)
    avg_pnl = total_pnl / total if total > 0 else 0.0
    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total) * 100 if total > 0 else 0,
        "total_pnl": total_pnl,
        "avg_pnl": avg_pnl,
    }

def summarize_by_strategy(logs):
    bucket = {}
    for x in logs:
        strategy = x.get("strategy", "UNKNOWN")
        bucket.setdefault(strategy, []).append(x)

    lines = []
    for strategy, items in sorted(bucket.items(), key=lambda kv: len(kv[1]), reverse=True):
        info = summarize_logs(items)
        lines.append(
            f"{strategy}\n- 거래: {info['total']}\n- 승률: {info['win_rate']:.2f}%\n- 평균: {info['avg_pnl']:.2f}%\n- 누적: {info['total_pnl']:.2f}%"
        )
    return lines

# =========================
# 명령어
# =========================
def summary_command(update, context: CallbackContext):
    closes = get_closed_logs_all()
    info = summarize_logs(closes)
    if not info:
        send("📊 아직 종료된 거래가 없어")
        return

    send(
        f"""
📊 거래 요약

총 종료 거래: {info['total']}
익절/플러스 종료: {info['wins']}
손절/마이너스 종료: {info['losses']}
승률: {info['win_rate']:.2f}%
누적 수익률: {info['total_pnl']:.2f}%
평균 수익률: {info['avg_pnl']:.2f}%
"""
    )

def today_command(update, context: CallbackContext):
    today_closes = get_closed_logs_today()
    info = summarize_logs(today_closes)
    if not info:
        send("📊 오늘 종료된 거래가 아직 없어")
        return

    lines = []
    for x in today_closes[-10:]:
        lines.append(f"• {x.get('ticker','?')} / {x.get('type')} / {float(x.get('pnl_pct',0)):.2f}%")

    send(
        f"""
📊 오늘 거래 요약

총 종료 거래: {info['total']}
익절/플러스 종료: {info['wins']}
손절/마이너스 종료: {info['losses']}
승률: {info['win_rate']:.2f}%
누적 수익률: {info['total_pnl']:.2f}%
평균 수익률: {info['avg_pnl']:.2f}%

최근 종료 거래
""" + "\n".join(lines)
    )

def summary_strategy_command(update, context: CallbackContext):
    closes = get_closed_logs_all()
    if not closes:
        send("📊 아직 종료된 거래가 없어")
        return
    lines = summarize_by_strategy(closes)
    send("📊 전략별 전체 결과\n\n" + "\n\n".join(lines))

def today_strategy_command(update, context: CallbackContext):
    closes = get_closed_logs_today()
    if not closes:
        send("📊 오늘 종료된 거래가 아직 없어")
        return
    lines = summarize_by_strategy(closes)
    send("📊 전략별 오늘 결과\n\n" + "\n\n".join(lines))

def btc_command(update, context: CallbackContext):
    send(analyze_btc_flow())

def status_command(update, context: CallbackContext):
    parts = []

    if active_positions:
        lines = []
        dust_lines = []

        for ticker, pos in active_positions.items():
            price = get_price(ticker)
            entry = float(pos["entry_price"])
            qty = float(pos.get("qty", 0))
            pnl = ((price - entry) / entry) * 100 if entry > 0 and price > 0 else 0
            held_min = int((time.time() - int(pos["entered_at"])) / 60)
            value = qty * price if price > 0 else 0
            tag_text = ""
            if pos.get("pattern_tags"):
                tag_text = " / 구조 " + ",".join(pos["pattern_tags"][:3])

            line = (
                f"• {ticker} / 방식 {pos['strategy_label']} / 진입 {fmt_price(entry)} / 현재 {fmt_price(price)} "
                f"/ 수익률 {fmt_pct(pnl)} / 평가금액 {fmt_price(value)} / 보유 {held_min}분 / 기대점수 {pos.get('edge_score', 0):.2f}{tag_text}"
            )

            if value < MIN_ORDER_KRW:
                dust_lines.append(line + " / 상태 소액포지션 대기중")
            else:
                lines.append(line)

        if lines:
            parts.append("📌 현재 포지션\n\n" + "\n".join(lines))
        if dust_lines:
            parts.append("🪙 소액 포지션(최소 주문금액 미만)\n\n" + "\n".join(dust_lines))
    else:
        parts.append("📭 현재 보유 포지션 없음")

    if pending_buy_candidates:
        c_lines = []
        sorted_candidates = sorted(
            pending_buy_candidates.values(),
            key=lambda x: safe_float(x.get("edge_score", 0)),
            reverse=True
        )[:5]
        for item in sorted_candidates:
            age = int((time.time() - safe_float(item.get("created_at", 0))) / 1)
            c_lines.append(
                f"• {item['ticker']} / 방식 {item.get('strategy_label','?')} / 후보대기 {age}초 / 기대점수 {safe_float(item.get('edge_score', 0)):.2f}"
            )
        parts.append("🕒 2차 확인 후보\n\n" + "\n".join(c_lines))

    if pending_sells:
        p = ["⚠️ 매도 확인 대기중"]
        for ticker in pending_sells.keys():
            p.append(f"• {ticker}")
        parts.append("\n".join(p))

    send("\n\n".join(parts))

# =========================
# 텔레그램 실행
# =========================
updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler("summary", summary_command))
dispatcher.add_handler(CommandHandler("today", today_command))
dispatcher.add_handler(CommandHandler("summary_strategy", summary_strategy_command))
dispatcher.add_handler(CommandHandler("today_strategy", today_strategy_command))
dispatcher.add_handler(CommandHandler("btc", btc_command))
dispatcher.add_handler(CommandHandler("status", status_command))

updater.start_polling(drop_pending_updates=True)

# =========================
# 시작 시 복구
# =========================
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

# =========================
# 메인 루프
# =========================
last_scan_time = 0
last_position_check = 0

while True:
    now_ts = time.time()

    if now_ts - last_scan_time >= SCAN_INTERVAL:
        try:
            shared_cache = build_shared_market_cache(force=False)
            scan_watchlist(shared_cache=shared_cache)
            process_pending_buy_promotions(shared_cache=shared_cache)
            scan_and_auto_trade(shared_cache=shared_cache)
        except Exception as e:
            print(f"[자동진입 스캔 오류] {e}")
            traceback.print_exc()
        last_scan_time = now_ts

    if now_ts - last_position_check >= POSITION_CHECK_INTERVAL:
        try:
            check_pending_sells()
            cleanup_pending_buy_candidates()
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
            if active_positions or pending_sells or pending_buy_candidates:
                status_command(None, None)
        except Exception as e:
            print(f"[상태 리포트 오류] {e}")
        last_status_report_time = now_ts

    time.sleep(LOOP_SLEEP)
