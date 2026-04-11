import os
import time
import json
import math
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

import pybithumb
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, CallbackQueryHandler

# =========================================================
# 버전
# =========================================================
BOT_VERSION = "수익형 v6.5.31"

# =========================================================
# 환경변수
# =========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not TELEGRAM_TOKEN or not CHAT_ID or not API_KEY or not API_SECRET:
    raise ValueError("환경변수(API_KEY, API_SECRET, TELEGRAM_TOKEN, CHAT_ID) 중 비어 있는 값이 있어요.")

bot = Bot(token=TELEGRAM_TOKEN)
bithumb = pybithumb.Bithumb(API_KEY, API_SECRET)

# =========================================================
# 파일 / 시간
# =========================================================
TIMEZONE = "Asia/Seoul"
LOG_FILE = "trade_log.json"
POSITIONS_FILE = "active_positions.json"
PENDING_SELLS_FILE = "pending_sells.json"
PENDING_BUYS_FILE = "pending_buy_candidates.json"
BOT_START_TS = int(time.time())
PRICE_CHECK_REFS = {}
PRICE_CHECK_SCHEDULED = {}
PRICE_CHECK_REF_TTL_SEC = 6 * 3600
PRICE_CHECK_REF_MAX = 300

# =========================================================
# 실행 주기
# =========================================================
SCAN_INTERVAL = 8
POSITION_CHECK_INTERVAL = 4
LOOP_SLEEP = 1

# =========================================================
# 시장 캐시 / 유니버스
# =========================================================
TOP_TICKERS = 80
ABSOLUTE_TURNOVER_CANDIDATES = 45
SURGE_CANDIDATES = 55
UNIVERSE_REFRESH_SEC = 18
MARKET_CACHE_TTL_SEC = 8

# =========================================================
# 주문 / 자금
# =========================================================
MIN_ORDER_KRW = 5000
DUST_KEEP_MIN_KRW = 100
MAX_ENTRY_KRW = 10000
KRW_USE_RATIO = 0.88
ORDER_BUFFER_KRW = 500

MARKET_BUY_SLIPPAGE = 1.05
RETRY_KRW_LEVELS = [1.00, 0.94, 0.88]

AUTO_BUY = False
AUTO_BUY_START_HOUR = 0
AUTO_BUY_END_HOUR = 24
MAX_HOLDINGS = 1
ALLOW_CHASE = False

# =========================================================
# BTC / 시장 필터
# =========================================================
BTC_FILTER_ON = True
BTC_CRASH_BLOCK_PCT = -2.0
BTC_STRONG_BLOCK_PCT = -2.8
BTC_MA_FILTER = True

REGIME_FILTER_ON = True
REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS = True
REGIME_SIDEWAYS_MAX_ABS_PCT = 0.50
REGIME_WEAK_MAX_ABS_PCT = -0.70
REGIME_STRONG_UP_PCT = 1.0

# =========================================================
# 리스크 관리
# =========================================================
BASE_STOP_LOSS_PCT = -1.7
BASE_TP_PCT = 4.6

TRAIL_START_PCT = 2.8
TRAIL_BACKOFF_PCT = 1.15

BREAKEVEN_TRIGGER_PCT = 1.7
BREAKEVEN_BUFFER_PCT = 0.15

TIME_STOP_MIN_SEC = 480
TIME_STOP_MAX_SEC = 1080

# v6.5.3 완화
MIN_EXPECTED_EDGE_SCORE = 6.2
MIN_EXPECTED_TP_PCT = 2.8

# =========================================================
# 거래 등급(S/A/B)
# =========================================================
TRADE_TIER_S_ALLOWED = ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT"]
TRADE_TIER_S_EDGE_MIN = 8.4
TRADE_TIER_S_VOL_MIN = 1.8
TRADE_TIER_S_BTC_OK = {"NORMAL", "STRONG_UP", "SIDEWAYS"}

TRADE_TIER_A_EDGE_MIN = 7.0
TRADE_TIER_A_VOL_MIN = 1.3

S_TP_BONUS_PCT = 0.50
S_TIME_STOP_BONUS_SEC = 180
S_BREAKEVEN_TRIGGER_PCT = 2.0
S_BREAKEVEN_BUFFER_PCT = 0.20
S_TRAIL_START_PCT = 3.0
S_TRAIL_BACKOFF_PCT = 1.00

A_TP_BONUS_PCT = 0.25
A_TIME_STOP_BONUS_SEC = 90
A_BREAKEVEN_TRIGGER_PCT = 1.8
A_BREAKEVEN_BUFFER_PCT = 0.18
A_TRAIL_START_PCT = 2.9
A_TRAIL_BACKOFF_PCT = 1.08

# =========================================================
# 전략 기준
# =========================================================
EARLY_CFG = {
    "change_min": 0.80,
    "change_max": 2.20,
    "vol_min": 2.80,
    "rsi_min": 46,
    "rsi_max": 61,
    "range_min": 0.90,
    "jump_min": 0.10,
}

PREPUMP_CFG = {
    "change_min": 1.00,
    "change_max": 4.40,
    "vol_min": 2.00,
    "rsi_min": 48,
    "rsi_max": 70,
    "range_min": 1.10,
}

PULLBACK_CFG = {
    "pump_min": 6.0,
    "rebound_min": 0.95,
    "vol_min": 1.6,
}

PRE_BREAKOUT_CFG = {
    "change_max": 1.05,
    "vol_min": 2.50,
    "rsi_min": 48,
    "rsi_max": 66,
    "range_min": 0.90,
    "gap_max": 0.32,
    "jump_min": 0.12,
}

BREAKOUT_CFG = {
    "change_min": 0.80,
    "change_max": 3.00,
    "vol_min": 3.20,
    "rsi_min": 52,
    "rsi_max": 72,
    "range_min": 1.00,
    "break_min": 0.12,
    "jump_min": 0.25,
}

CHASE_CFG = {
    "change_min": 1.80,
    "change_max": 3.40,
    "vol_min": 4.00,
    "rsi_min": 55,
    "rsi_max": 72,
    "range_min": 1.60,
    "jump_min": 0.35,
}

TREND_CONT_CFG = {
    "trend_change_min": 2.40,
    "trend_change_max": 8.80,
    "pullback_max": 2.00,
    "recovery_min": 0.35,
    "recovery_max": 1.25,
    "last1_change_max": 0.55,
    "vol_reaccel_min": 1.60,
    "rsi_min": 50,
    "rsi_max": 70,
    "high_retest_gap_min": 0.18,
    "high_retest_gap_max": 0.65,
    "extension_max": 0.08,
}

# =========================================================
# 주도주(LEADER SCORE)
# =========================================================
LEADER_BASE_MIN_FOR_PRIORITY = 4.8
LEADER_HIGH_MIN = 6.2
LEADER_STRONG_MIN = 7.2
LEADER_TOP_TURNOVER_BONUS = 1.2
LEADER_TOP_SURGE_BONUS = 1.0
LEADER_REPEAT_SEEN_BONUS = 0.45
LEADER_MAX_REPEAT_BONUS = 1.35
LEADER_SIDEWAYS_EDGE_DISCOUNT = 0.45
LEADER_WEAK_EDGE_DISCOUNT = 0.25

# PREPUMP 후보 저장용
PREPUMP_PENDING_EDGE_MIN = 5.8
PREPUMP_PENDING_SCORE_MIN = 5.4
PREPUMP_PENDING_VOL_MIN = 2.8

# =========================================================
# 늦은 진입 방지
# =========================================================
LATE_ENTRY_FILTER_ON = True
LATE_ENTRY_MAX_NEAR_HIGH_PCT = 0.22
LATE_ENTRY_MAX_5BAR_CHANGE_PCT = 3.1
LATE_ENTRY_MAX_3BAR_CHANGE_PCT = 2.2
LATE_ENTRY_MAX_BREAKOUT_EXTENSION_PCT = 0.38
FALSE_START_NEAR_HIGH_PCT = 0.35
FALSE_START_UPPER_ZONE_RATIO = 0.86
FALSE_START_MIN_SPAN_PCT = 1.8
FALSE_START_3BAR_CHANGE_PCT = 0.85
FALSE_START_5BAR_CHANGE_PCT = 1.25
PULLBACK_NEAR_HIGH_GAP_MIN_PCT = 0.28
PULLBACK_HOT_GAP_MIN_PCT = 0.55
PULLBACK_EXTREME_GAP_MIN_PCT = 0.85
PULLBACK_VOL_HOT_MIN = 8.0

# =========================================================
# 시나리오 실패 빠른 정리
# =========================================================
SCENARIO_EXIT_ON = True
SCENARIO_MIN_HOLD_SEC = 70
SCENARIO_MAX_HOLD_SEC = 210
SCENARIO_MIN_PROGRESS_PCT = 0.18
SCENARIO_FAIL_DROP_FROM_BREAK_LEVEL_PCT = 0.10
SCENARIO_FAIL_DROP_FROM_ENTRY_PCT = -0.45
SCENARIO_WEAK_VOL_RATIO_THRESHOLD = 1.15

# =========================================================
# v6.5.4 제외 규칙 / 사후검증 / 쉬는 기능
# =========================================================
ENTRY_SHAPE_FILTER_ON = True
OVERHEAT_3BAR_MAX_PCT = 2.2
OVERHEAT_5BAR_MAX_PCT = 3.2
WEAK_CLOSE_BODY_RATIO_MIN = 0.42
WEAK_CLOSE_UPPER_WICK_RATIO = 0.52

POST_ENTRY_RECHECK_ON = True
POST_ENTRY_MIN_CHECK_SEC = 120
POST_ENTRY_MAX_CHECK_SEC = 240
POST_ENTRY_MIN_PROGRESS_PCT = 0.12
POST_ENTRY_MIN_VOL_RATIO = 0.95
POST_ENTRY_FAIL_BELOW_ENTRY_PCT = -0.35

AUTO_PAUSE_ON = True
AUTO_PAUSE_STREAK_COUNT = 3
AUTO_PAUSE_SECONDS = 1200
AUTO_PAUSE_LOOKBACK_SEC = 3600
AUTO_PAUSE_ROLLING_PNL = -1.8

# =========================================================
# 차트 구조 패턴
# =========================================================
PATTERN_FILTER_ON = True
USE_HIGHER_LOWS_CORE = True
USE_BOX_COMPRESSION_CORE = True
USE_BIG_BULL_HALF_HOLD_CORE = True
USE_FAKE_BREAKDOWN_RECOVERY_BONUS = True
USE_PULLBACK_RECHECK_BONUS = True

# =========================================================
# 후보 승격 매수
# =========================================================
PENDING_BUY_ON = True
PENDING_BUY_MAX_ITEMS = 6
PENDING_BUY_TTL_SEC = 165
PENDING_BUY_MIN_EDGE = 4.2
PENDING_BUY_MIN_SCORE = 3.8
PENDING_BUY_RECHECK_MIN_SEC = 26

PROMOTE_RECOVERY_TO_HIGH_PCT = 99.7
PROMOTE_MIN_VOL_RATIO = 1.15
PROMOTE_MAX_BREAKOUT_EXTENSION_PCT = 0.45

# =========================================================
# watch 재알림 제어
# =========================================================
WATCH_RENOTICE_SEC = 1500
WATCH_VOL_IMPROVE_DELTA = 0.28
WATCH_CHANGE_IMPROVE_DELTA = 0.24
WATCH_SCORE_IMPROVE_DELTA = 0.45
WATCH_RENOTICE_MAX_PER_TICKER = 2

# =========================================================
# 리포트 간격
# =========================================================
BTC_REPORT_INTERVAL = 3600
STATUS_REPORT_INTERVAL = 1800

last_btc_report_time = 0
last_status_report_time = 0

# =========================================================
# 상태 저장
# =========================================================
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

# =========================================================
# 공유 캐시
# =========================================================
shared_market_cache = {}
shared_market_cache_time = 0
last_scan_debug_snapshot = []
last_scan_debug_snapshot_time = 0
last_scan_debug_note = ""
SCAN_DEBUG_SNAPSHOT_MAX_AGE_SEC = 180
market_universe = []
market_universe_time = 0

# =========================================================
# 유틸
# =========================================================
def now_kst():
    return datetime.now(ZoneInfo(TIMEZONE))


def is_auto_time():
    return AUTO_BUY_START_HOUR <= now_kst().hour < AUTO_BUY_END_HOUR


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


def send(msg: str, reply_markup=None):
    try:
        bot.send_message(chat_id=CHAT_ID, text=msg.strip(), reply_markup=reply_markup)
    except Exception as e:
        print(f"[텔레그램 오류] {e}")


def format_hms(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), ZoneInfo(TIMEZONE)).strftime("%H:%M:%S")
    except Exception:
        return "--:--:--"


def format_elapsed_text(sec: float) -> str:
    try:
        sec = max(0, int(sec))
    except Exception:
        return "0초"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}시간 {m}분 {s}초"
    if m > 0:
        return f"{m}분 {s}초"
    return f"{s}초"


def get_price_check_note(change_pct: float) -> str:
    try:
        cp = float(change_pct)
    except Exception:
        return "판단: 비교 불가"
    if cp >= 3.0:
        return "판단: 알림 뒤 눈에 띄게 더 감"
    if cp >= 1.0:
        return "판단: 알림 뒤 조금 더 감"
    if cp <= -2.0:
        return "판단: 알림 뒤 꽤 밀림"
    if cp <= -0.5:
        return "판단: 알림 뒤 약하게 밀림"
    return "판단: 크게는 못 가고 애매함"


def cleanup_price_check_refs(now_ts: float = None):
    now_ts = safe_float(now_ts, time.time())
    expired = []
    for ref_id, data in PRICE_CHECK_REFS.items():
        saved_at = safe_float(data.get("alert_ts", 0))
        if saved_at <= 0 or now_ts - saved_at > PRICE_CHECK_REF_TTL_SEC:
            expired.append(ref_id)
    for ref_id in expired:
        PRICE_CHECK_REFS.pop(ref_id, None)
    if len(PRICE_CHECK_REFS) > PRICE_CHECK_REF_MAX:
        ordered = sorted(PRICE_CHECK_REFS.items(), key=lambda x: safe_float(x[1].get("alert_ts", 0)))
        drop_count = len(PRICE_CHECK_REFS) - PRICE_CHECK_REF_MAX
        for ref_id, _ in ordered[:drop_count]:
            PRICE_CHECK_REFS.pop(ref_id, None)

    expired_sched = []
    for key, sched in PRICE_CHECK_SCHEDULED.items():
        if isinstance(sched, dict):
            target_ts = safe_float(sched.get("target_ts", 0), 0)
            ref_id = (sched.get("ref_id") or key.split(":", 1)[0]).strip()
        else:
            target_ts = safe_float(sched, 0)
            ref_id = key.split(":", 1)[0]
        if target_ts <= 0 or now_ts - target_ts > 1800 or ref_id not in PRICE_CHECK_REFS:
            expired_sched.append(key)
    for key in expired_sched:
        PRICE_CHECK_SCHEDULED.pop(key, None)


def make_price_check_ref(item: dict, alert_type: str = "new") -> str:
    now_ts = time.time()
    cleanup_price_check_refs(now_ts)
    ticker = item.get("ticker", "?")
    ref_id = f"{ticker}_{int(now_ts * 1000)}"[-40:]
    PRICE_CHECK_REFS[ref_id] = {
        "ticker": ticker,
        "alert_price": safe_float(item.get("current_price", item.get("price", 0))),
        "alert_ts": now_ts,
        "strategy_label": item.get("strategy_label", ""),
        "alert_type": alert_type,
    }
    return ref_id


def build_price_check_markup(entries):
    if not entries:
        return None
    rows = []
    for idx, (item, alert_type, _reason_text) in enumerate(entries, start=1):
        ref_id = make_price_check_ref(item, alert_type=alert_type)
        ticker = item.get("ticker", "?")
        rows.append([
            InlineKeyboardButton(f"{circled_number(idx)} {ticker} 지금", callback_data=f"pc:{ref_id}:now"),
            InlineKeyboardButton("5분뒤", callback_data=f"pc:{ref_id}:5"),
            InlineKeyboardButton("15분뒤", callback_data=f"pc:{ref_id}:15"),
            InlineKeyboardButton("30분뒤", callback_data=f"pc:{ref_id}:30"),
        ])
    return InlineKeyboardMarkup(rows)


def build_price_check_lines(ref: dict, current_price: float, now_ts: float, target_min: int = 0):
    ticker = ref.get("ticker", "?")
    alert_price = safe_float(ref.get("alert_price", 0))
    alert_ts = safe_float(ref.get("alert_ts", 0))
    elapsed_sec = max(0.0, now_ts - alert_ts)
    change_pct = ((current_price - alert_price) / alert_price * 100.0) if alert_price > 0 else 0.0

    if target_min <= 0:
        title = f"💰 {ticker} 현재가 비교"
    else:
        title = f"💰 {ticker} {target_min}분 확인"

    lines = [
        title,
        "",
        f"- 알림가: {fmt_price(alert_price)}",
        f"- 알림 시각: {format_hms(alert_ts)}",
        f"- 현재가: {fmt_price(current_price)}",
        f"- 확인 시각: {format_hms(now_ts)}",
        f"- 지난 시간: {format_elapsed_text(elapsed_sec)}",
        f"- 변화: {change_pct:+.2f}%",
        "",
        get_price_check_note(change_pct),
    ]
    return lines


def send_price_check_result(chat_id, ref: dict, target_min: int = 0, now_ts: float = None):
    ticker = ref.get("ticker", "?")
    now_ts = safe_float(now_ts, time.time())
    current_price = get_price(ticker)
    if current_price <= 0:
        try:
            bot.send_message(chat_id=chat_id, text=f"⚠️ {ticker} 현재가를 불러오지 못해서 비교를 못 했어.")
        except Exception as e:
            print(f"[텔레그램 지연 비교 오류] {e}")
        return False
    try:
        bot.send_message(chat_id=chat_id, text="\n".join(build_price_check_lines(ref, current_price, now_ts, target_min=target_min)))
        return True
    except Exception as e:
        print(f"[텔레그램 지연 비교 오류] {e}")
        return False


def process_scheduled_price_checks(now_ts: float = None):
    now_ts = safe_float(now_ts, time.time())
    due_keys = []
    for key, sched in list(PRICE_CHECK_SCHEDULED.items()):
        if not isinstance(sched, dict):
            continue
        target_ts = safe_float(sched.get("target_ts", 0), 0)
        if target_ts > 0 and now_ts >= target_ts:
            due_keys.append(key)

    for key in due_keys:
        sched = PRICE_CHECK_SCHEDULED.pop(key, None)
        if not isinstance(sched, dict):
            continue
        ref_id = (sched.get("ref_id") or "").strip()
        ref = PRICE_CHECK_REFS.get(ref_id)
        if not ref:
            continue
        chat_id = sched.get("chat_id", CHAT_ID)
        target_min = int(safe_float(sched.get("target_min", 0), 0))
        send_price_check_result(chat_id, ref, target_min=target_min, now_ts=now_ts)


def send_watch_alert_bundle(header: str, entries):
    if not entries:
        return
    body = []
    for idx, (item, alert_type, reason_text) in enumerate(entries, start=1):
        body.append(format_watch_alert_line(item, order=idx, alert_type=alert_type, reason_text=reason_text))
    markup = build_price_check_markup(entries)
    send(header + "\n\n" + "\n\n".join(body), reply_markup=markup)


STRATEGY_GUIDE = {
    "초반 선점형": "막 움직이기 전 초반 자리",
    "상승 시작형": "막 오르기 시작한 초반 자리",
    "눌림 반등형": "오른 뒤 눌렸다가 다시 반등 시도",
    "추세 지속형": "이미 오른 흐름이 이어지는 자리",
    "쏘기 직전형": "강하게 움직이기 직전처럼 보이는 자리",
    "급등 돌파형": "막 저항을 뚫고 강하게 치는 자리",
    "추격형": "이미 오른 흐름을 따라붙는 자리",
    "주도주 후보형": "시장 주목도가 높아 먼저 보는 후보",
}

TERM_GUIDE = [
    ("등급 S/A/B", "S가 가장 강하고 B는 후보는 맞지만 상대적으로 약한 편"),
    ("시장주목도", "지금 시장에서 거래량과 움직임이 눈에 띄는 정도"),
    ("빠른 후보", "지금 바로 한 번 볼 만한 후보"),
    ("이전 후보 다시 확인", "전에 봤던 후보를 시간이 지나 다시 확인하라는 뜻"),
    ("자동 쉬기", "연속 손실이나 흐름 악화 때문에 자동매수를 잠깐 쉬는 상태"),
]


def strategy_help_text(strategy_label: str) -> str:
    return STRATEGY_GUIDE.get(strategy_label, "전략 설명 준비중")


def build_info_text() -> str:
    lines = [
        "📘 용어 / 전략 설명",
        "",
        "[전략 설명]",
    ]
    for name, desc in STRATEGY_GUIDE.items():
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append("[알림에서 자주 보는 말]")
    for name, desc in TERM_GUIDE:
        lines.append(f"- {name}: {desc}")
    lines.append("")
    lines.append("[해석 팁]")
    lines.append("- 상승%가 너무 큰 후보는 이미 많이 오른 자리일 수 있어")
    lines.append("- 거래량이 평소보다 크게 붙은 후보를 우선적으로 보면 좋아")
    lines.append("- 자동매수는 꺼둔 상태에서 알림 품질부터 보는 게 안전해")
    return "\n".join(lines)


def format_strategy_with_help(strategy_label: str) -> str:
    return f"{strategy_label} ({strategy_help_text(strategy_label)})"


def circled_number(n: int) -> str:
    nums = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
    if 1 <= n <= len(nums):
        return nums[n - 1]
    return f"{n}."


def get_watch_item_icon(item: dict, alert_type: str = "new") -> str:
    if alert_type == "upgrade":
        return "⬆️"
    if alert_type == "renotice":
        return "🔁"
    strategy_label = item.get("strategy_label", "")
    change_pct = safe_float(item.get("change_pct", 0))
    if change_pct >= 12.0:
        return "⚠️"
    if strategy_label in ["상승 시작형", "초반 선점형"]:
        return "🌱"
    if strategy_label in ["추세 지속형", "급등 돌파형"]:
        return "📈"
    return "👀"


def get_watch_caution_text(item: dict) -> str:
    change_pct = safe_float(item.get("change_pct", 0))
    vol_ratio = safe_float(item.get("vol_ratio", 0))
    strategy_label = item.get("strategy_label", "")
    if change_pct >= 18.0:
        return "이미 많이 오른 자리라 실매수는 특히 조심"
    if change_pct >= 12.0:
        return "상승폭이 큰 편이라 늦은 자리일 수 있어"
    if strategy_label == "눌림 반등형" and change_pct >= 7.0:
        return "눌림 반등형인데 이미 많이 올라 보수적으로 보는 게 좋아"
    if change_pct >= 10.0 and vol_ratio < 2.4:
        return "상승폭 대비 거래량이 아주 강한 편은 아니야"
    return ""


def get_watch_judgement_text(item: dict) -> str:
    strategy_label = item.get("strategy_label", "")
    change_pct = safe_float(item.get("change_pct", 0))
    vol_ratio = safe_float(item.get("vol_ratio", 0))
    caution = get_watch_caution_text(item)
    if caution:
        return "⚠️ 늦은 자리 주의"
    if strategy_label in ["상승 시작형", "초반 선점형"] and change_pct <= 2.6 and vol_ratio >= 2.0:
        return "✅ 초반형 후보"
    if strategy_label == "눌림 반등형":
        return "👀 관찰 후보"
    if strategy_label in ["추세 지속형", "급등 돌파형"]:
        return "📈 힘 있는 후보"
    return "👀 한 번 볼 후보"


def format_watch_alert_line(item: dict, order: int = 1, alert_type: str = "new", reason_text: str = "") -> str:
    tags = item.get("pattern_tags", [])
    tag_text = ", ".join(tags[:3]) if tags else ""
    icon = get_watch_item_icon(item, alert_type=alert_type)
    caution = get_watch_caution_text(item)
    judgement = get_watch_judgement_text(item)
    lines = [
        f"{circled_number(order)} {icon} {item['ticker']}",
        f"- 한눈판단: {judgement}",
        f"- 현재가 {fmt_price(item['current_price'])} / 상승 {float(item.get('change_pct', 0)):.2f}% / 거래량 {float(item.get('vol_ratio', 0)):.2f}배",
        f"- 전략: {format_strategy_with_help(item['strategy_label'])}",
        f"- 등급 {item.get('trade_tier','B')} / 시장주목도 {safe_float(item.get('leader_score',0)):.2f}",
    ]
    if tag_text:
        lines.append(f"- 구조: {tag_text}")
    if caution:
        lines.append(f"- 주의: {caution}")
    if reason_text:
        lines.append(f"- 이유: {reason_text}")
    return "\n".join(lines)


def send_startup_message():
    lines = [
        "✅ 봇 시작 완료",
        f"현재 버전: {BOT_VERSION}",
        "",
        f"자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}",
        f"운영시간: {AUTO_BUY_START_HOUR}:00 ~ {AUTO_BUY_END_HOUR}:00",
        f"주문 최대금액: {MAX_ENTRY_KRW:,}원",
        "",
        "정리 포인트",
        "- 고점 근처 가짜 초반형 후보 더 보수화",
        "- 초저가 급등 왜곡 후보 더 보수화",
        "- 후보별 현재가 / 경과시간 버튼 추가",
        "- 자세한 설명은 /info",
    ]
    send("\n".join(lines))


# =========================================================
# 파일 I/O
# =========================================================
def load_json_file(path, default):
    lines = [
        "✅ 봇 시작 완료",
        "",
        f"자동매수: {'켜짐' if AUTO_BUY else '꺼짐'}",
        f"운영시간: {AUTO_BUY_START_HOUR}:00 ~ {AUTO_BUY_END_HOUR}:00",
        f"주문 최대금액: {MAX_ENTRY_KRW:,}원",
        "",
        "정리 포인트",
        "- 초저가 급등 왜곡 후보 더 보수화",
        "- 고점 근처 가짜 초반형 후보 더 보수화",
        "- 초반형 후보 위주로 정리",
        "- 자동 쉬기 / 수동 해제 / 상태 표시 유지",
        "- 자세한 설명은 /info",
    ]
    send("\n".join(lines))


# =========================================================
# 파일 I/O
# =========================================================
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


def save_config():
    return False


def save_settings():
    return False


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


# =========================================================
# 거래소 데이터
# =========================================================
def get_price(ticker: str) -> float:
    try:
        p = pybithumb.get_current_price(ticker)
        return -1 if p is None else float(p)
    except Exception:
        return -1


def get_orderbook_best_ask(ticker: str) -> float:
    try:
        ob = pybithumb.get_orderbook(ticker)
        if not ob:
            return -1
        if isinstance(ob, dict) and "asks" in ob and ob["asks"]:
            ask0 = ob["asks"][0]
            return float(ask0.get("price", -1))
        if isinstance(ob, dict) and "data" in ob and "asks" in ob["data"] and ob["data"]["asks"]:
            ask0 = ob["data"]["asks"][0]
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


# =========================================================
# 지표
# =========================================================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def get_rsi(df, period=14):
    try:
        if df is None or len(df) < period + 1:
            return 50.0
        series = calculate_rsi(df, period)
        value = safe_float(series.iloc[-1], 50.0)
        return 50.0 if math.isnan(value) else value
    except Exception:
        return 50.0


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
        return len(closes) == 3 and closes[-1] >= closes[-2] >= closes[-3]
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
        return (high - body_top) / (high - low) >= 0.48
    except Exception:
        return False


def candle_body_ratio(df):
    try:
        last = df.iloc[-1]
        o, c, h, l = map(float, [last["open"], last["close"], last["high"], last["low"]])
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
        return 0.0 if len(recent) <= 0 else float(recent["high"].max())
    except Exception:
        return 0.0


def get_recent_low(df, n=8, exclude_last=False):
    try:
        recent = df.tail(n)
        if exclude_last:
            recent = recent.iloc[:-1]
        return 0.0 if len(recent) <= 0 else float(recent["low"].min())
    except Exception:
        return 0.0


def recent_unique_close_count(df, n=15):
    try:
        recent = df.tail(n)
        if len(recent) <= 0:
            return 0
        return int(recent["close"].round(10).nunique())
    except Exception:
        return 0


def is_tiny_tick_distortion(df, current_price, lookback=15):
    try:
        if df is None or len(df) < lookback or current_price <= 0:
            return False
        if current_price > 1.0:
            return False
        recent = df.tail(lookback)
        lo = float(recent["low"].min())
        hi = float(recent["high"].max())
        if lo <= 0 or hi <= lo:
            return False
        swing_pct = ((hi - lo) / lo) * 100
        uniq = recent_unique_close_count(df, lookback)
        return uniq <= 4 and swing_pct >= 12.0
    except Exception:
        return False


def is_retest_after_failed_peak(df, lookback=15):
    try:
        if df is None or len(df) < lookback:
            return False
        recent = df.tail(lookback).reset_index(drop=True)
        if len(recent) < 8:
            return False

        peak_zone = recent.iloc[:-3]
        if len(peak_zone) < 5:
            return False
        peak_idx = int(peak_zone["high"].idxmax())
        if peak_idx >= len(recent) - 3:
            return False

        peak_high = float(recent["high"].iloc[peak_idx])
        last_close = float(recent["close"].iloc[-1])
        if peak_high <= 0 or last_close <= 0:
            return False

        after_peak = recent.iloc[peak_idx + 1:]
        if len(after_peak) < 3:
            return False
        pullback_low = float(after_peak["low"].min())
        if pullback_low <= 0 or pullback_low >= peak_high:
            return False

        retest_gap_pct = ((peak_high - last_close) / peak_high) * 100
        pullback_pct = ((peak_high - pullback_low) / peak_high) * 100
        recovery_pct = ((last_close - pullback_low) / pullback_low) * 100

        return pullback_pct >= 2.2 and 0 <= retest_gap_pct <= 1.2 and recovery_pct >= 1.2
    except Exception:
        return False


def is_recent_spike_near_high(df, current_price, lookback=12):
    try:
        if df is None or len(df) < lookback or current_price <= 0:
            return False
        recent = df.tail(lookback)
        lo = float(recent["low"].min())
        hi = float(recent["high"].max())
        if lo <= 0 or hi <= lo:
            return False
        span_pct = ((hi - lo) / lo) * 100
        dist_to_high_pct = ((hi - current_price) / current_price) * 100 if current_price > 0 else 999.0
        last3_high = float(recent["high"].tail(3).max())
        recent_peak_in_last = last3_high >= hi * 0.998
        last3_change = get_recent_change_pct(df, 3)
        last5_change = get_recent_change_pct(df, 5)
        return recent_peak_in_last and 0 <= dist_to_high_pct <= 0.22 and span_pct >= 1.8 and (last3_change >= 0.95 or last5_change >= 1.35)
    except Exception:
        return False


def is_false_start_near_high(df, current_price, lookback=14):
    try:
        if df is None or len(df) < lookback or current_price <= 0:
            return False
        recent = df.tail(lookback)
        lo = float(recent["low"].min())
        hi = float(recent["high"].max())
        if lo <= 0 or hi <= lo:
            return False
        span_pct = ((hi - lo) / lo) * 100
        if span_pct < FALSE_START_MIN_SPAN_PCT:
            return False
        dist_to_high_pct = ((hi - current_price) / current_price) * 100 if current_price > 0 else 999.0
        if not (0 <= dist_to_high_pct <= FALSE_START_NEAR_HIGH_PCT):
            return False
        zone_ratio = (current_price - lo) / (hi - lo) if hi > lo else 0.0
        if zone_ratio < FALSE_START_UPPER_ZONE_RATIO:
            return False
        recent_peak_in_last = float(recent["high"].tail(3).max()) >= hi * 0.998
        if not recent_peak_in_last:
            return False
        last3_change = get_recent_change_pct(df, 3)
        last5_change = get_recent_change_pct(df, 5)
        return last3_change >= FALSE_START_3BAR_CHANGE_PCT or last5_change >= FALSE_START_5BAR_CHANGE_PCT
    except Exception:
        return False


def is_box_top_range_chase(df, current_price, lookback=18):
    try:
        if df is None or len(df) < lookback or current_price <= 0:
            return False
        recent = df.tail(lookback)
        lo = float(recent["low"].min())
        hi = float(recent["high"].max())
        if lo <= 0 or hi <= lo:
            return False
        span_pct = ((hi - lo) / lo) * 100
        dist_to_high_pct = ((hi - current_price) / current_price) * 100 if current_price > 0 else 999.0
        closes = [float(x) for x in recent["close"]]
        flips = 0
        for i in range(2, len(closes)):
            d1 = closes[i - 1] - closes[i - 2]
            d2 = closes[i] - closes[i - 1]
            if d1 == 0 or d2 == 0:
                continue
            if d1 * d2 < 0:
                flips += 1
        recent_peak_in_last = float(recent["high"].tail(4).max()) >= hi * 0.999
        zone_ratio = (current_price - lo) / (hi - lo) if hi > lo else 0.0
        return recent_peak_in_last and zone_ratio >= 0.84 and 0 <= dist_to_high_pct <= 0.32 and 1.0 <= span_pct <= 5.5 and flips >= 5
    except Exception:
        return False


# =========================================================
# 차트 구조 패턴
# =========================================================
def detect_higher_lows(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12:
            return False
        p1, p2, p3 = recent.iloc[0:4], recent.iloc[4:8], recent.iloc[8:12]
        return float(p1["low"].min()) < float(p2["low"].min()) < float(p3["low"].min())
    except Exception:
        return False


def detect_box_top_compression(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12:
            return False
        highs = list(recent["high"].tail(6))
        lows = list(recent["low"].tail(6))
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
        if len(recent) < 8:
            return False
        best_idx, best_body = None, 0.0
        for idx, row in recent.iloc[:-1].iterrows():
            o, c = float(row["open"]), float(row["close"])
            if c <= o:
                continue
            body = c - o
            if body > best_body:
                best_body, best_idx = body, idx
        if best_idx is None:
            return False
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
        if len(recent) < 8:
            return False
        support_zone = float(recent.iloc[:-2]["low"].tail(5).min())
        prev, last = recent.iloc[-2], recent.iloc[-1]
        return float(prev["low"]) < support_zone * 0.998 and float(last["close"]) >= support_zone * 1.001
    except Exception:
        return False


def detect_pullback_recheck(df, bars=12):
    try:
        recent = df.tail(bars)
        if len(recent) < 12:
            return False
        p1, p2, p3 = recent.iloc[0:4], recent.iloc[4:8], recent.iloc[8:12]
        rise1 = ((float(p1["close"].iloc[-1]) - float(p1["close"].iloc[0])) / float(p1["close"].iloc[0])) * 100
        pull2 = ((float(p2["close"].iloc[-1]) - float(p2["close"].iloc[0])) / float(p2["close"].iloc[0])) * 100
        recover3 = ((float(p3["close"].iloc[-1]) - float(p3["close"].iloc[0])) / float(p3["close"].iloc[0])) * 100
        high1 = float(p1["high"].max())
        close3 = float(p3["close"].iloc[-1])
        return rise1 >= 0.8 and pull2 <= -0.2 and recover3 >= 0.35 and close3 >= high1 * 0.995
    except Exception:
        return False


def analyze_chart_patterns(df):
    tags = []
    bonus = 0.0
    hl = detect_higher_lows(df)
    bc = detect_box_top_compression(df)
    bh = detect_big_bull_half_hold(df)
    fb = detect_fake_breakdown_recovery(df)
    pr = detect_pullback_recheck(df)

    if hl:
        tags.append("저점높임")
    if bc:
        tags.append("상단압축")
    if bh:
        tags.append("양봉절반유지")
    if fb and USE_FAKE_BREAKDOWN_RECOVERY_BONUS:
        tags.append("가짜하락회복")
        bonus += 0.45
    if pr and USE_PULLBACK_RECHECK_BONUS:
        tags.append("눌림재확인")
        bonus += 0.55

    return {
        "patterns": {
            "higher_lows": hl,
            "box_compression": bc,
            "big_bull_half_hold": bh,
            "fake_breakdown_recovery": fb,
            "pullback_recheck": pr,
        },
        "tags": tags,
        "bonus_score": bonus,
    }


def has_higher_lows(df):
    return detect_higher_lows(df)


def big_bull_half_hold(df):
    return detect_big_bull_half_hold(df)


def fake_breakdown_recovery(df):
    return detect_fake_breakdown_recovery(df)


def passes_core_pattern_filter(strategy, pattern_info):
    if not PATTERN_FILTER_ON:
        return True
    p = pattern_info["patterns"]
    hl = p.get("higher_lows", False)
    bc = p.get("box_compression", False)
    bh = p.get("big_bull_half_hold", False)

    if strategy == "EARLY":
        return (hl if USE_HIGHER_LOWS_CORE else False) or (bh if USE_BIG_BULL_HALF_HOLD_CORE else False)
    if strategy == "PRE_BREAKOUT":
        left = bc if USE_BOX_COMPRESSION_CORE else True
        return left and (hl or bh)
    if strategy == "BREAKOUT":
        return (bc if USE_BOX_COMPRESSION_CORE else False) or (bh if USE_BIG_BULL_HALF_HOLD_CORE else False)
    if strategy == "CHASE":
        return bc or bh
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
    return "" if not tags else "\n- 차트 구조: " + ", ".join(tags)


# =========================================================
# BTC / 시장 상태
# =========================================================
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
        return {"name": "UNKNOWN", "label": label or "unknown", "btc_change_pct": 0.0, "allow_auto_buy": True, "allow_breakout": True, "message": "시장 상태 판단 실패"}

    price = get_price("BTC")
    if price <= 0:
        return {"name": "UNKNOWN", "label": label or "unknown", "btc_change_pct": 0.0, "allow_auto_buy": True, "allow_breakout": True, "message": "시장 상태 판단 실패"}

    change_pct = get_recent_change_pct(df, 4)
    ma5v, ma20v = ma(df, 5), ma(df, 20)

    if change_pct <= BTC_STRONG_BLOCK_PCT:
        return {"name": "BLOCK", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": False, "allow_breakout": False, "message": "BTC 급락 구간이라 자동매수 쉬는 장"}
    if change_pct <= REGIME_WEAK_MAX_ABS_PCT or (ma20v > 0 and price < ma20v * 0.992):
        return {"name": "WEAK", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": False, "message": "약한 장이라 EARLY / PRE_BREAKOUT 위주"}
    if abs(change_pct) <= REGIME_SIDEWAYS_MAX_ABS_PCT:
        return {"name": "SIDEWAYS", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": not REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS, "message": "횡보 장이라 돌파형은 조심"}
    if change_pct >= REGIME_STRONG_UP_PCT and price >= ma5v and price >= ma20v:
        return {"name": "STRONG_UP", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": True, "message": "강한 상승 장"}
    return {"name": "NORMAL", "label": label, "btc_change_pct": change_pct, "allow_auto_buy": True, "allow_breakout": True, "message": "무난한 장"}


def analyze_btc_flow():
    regime = get_market_regime()
    df, label = get_btc_df()
    if df is None:
        return "📊 BTC 리포트\nBTC 데이터를 불러오지 못했어."
    price = get_price("BTC")
    if price <= 0:
        return "📊 BTC 리포트\nBTC 현재가 조회 실패."
    change_pct = get_recent_change_pct(df, 4)
    ma5v, ma20v = ma(df, 5), ma(df, 20)

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


# =========================================================
# 점수 / 리스크
# =========================================================
def dynamic_stop_loss_pct(vol_ratio, range_pct, strategy):
    stop = BASE_STOP_LOSS_PCT
    if strategy in ["PRE_BREAKOUT", "BREAKOUT", "CHASE"]:
        stop -= 0.05
    elif strategy == "TREND_CONT":
        stop -= 0.02
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
        tp += 0.8
    elif strategy == "TREND_CONT":
        tp += 0.9
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
        base = 930
    elif strategy == "TREND_CONT":
        base = 840
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
        return 1.45
    if strategy == "PRE_BREAKOUT":
        return 1.30
    if strategy == "TREND_CONT":
        return 1.05
    if strategy == "BREAKOUT":
        return 0.45
    if strategy == "CHASE":
        return -0.95
    if strategy == "PREPUMP":
        return 0.35
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

    if strategy in ["EARLY", "PRE_BREAKOUT"] and change_pct <= 1.8:
        score += 0.45
    if strategy == "TREND_CONT" and 0.3 <= change_pct <= 2.0:
        score += 0.35
    if strategy == "BREAKOUT" and change_pct >= 2.6:
        score -= 0.55
    if strategy == "CHASE" and change_pct >= 2.8:
        score -= 0.8
    return round(score, 2)


def classify_trade_tier(signal, regime=None):
    strategy = signal.get("strategy", "")
    edge = safe_float(signal.get("edge_score", 0))
    vol = safe_float(signal.get("vol_ratio", 0))
    rsi = safe_float(signal.get("rsi", 0))
    tags = set(signal.get("pattern_tags", []))
    regime_name = (regime or {}).get("name", "NORMAL")
    leader_score = safe_float(signal.get("leader_score", 0))

    if (
        strategy in TRADE_TIER_S_ALLOWED
        and edge >= TRADE_TIER_S_EDGE_MIN
        and vol >= TRADE_TIER_S_VOL_MIN
        and rsi <= 66
        and regime_name in TRADE_TIER_S_BTC_OK
        and (tags.intersection({"저점높임", "양봉절반유지", "눌림재확인"}) or strategy == "TREND_CONT")
        and leader_score >= LEADER_HIGH_MIN
    ):
        return "S"
    if edge >= TRADE_TIER_A_EDGE_MIN and vol >= TRADE_TIER_A_VOL_MIN:
        if leader_score >= LEADER_BASE_MIN_FOR_PRIORITY or strategy in ["TREND_CONT", "PRE_BREAKOUT"]:
            return "A"
    return "B"


def apply_trade_tier_adjustments(signal, regime=None):
    tier = classify_trade_tier(signal, regime=regime)
    signal["trade_tier"] = tier

    tp = safe_float(signal.get("take_profit_pct", BASE_TP_PCT))
    ts = int(safe_float(signal.get("time_stop_sec", TIME_STOP_MAX_SEC)))

    if tier == "S":
        signal["take_profit_pct"] = round(min(tp + S_TP_BONUS_PCT, 7.2), 2)
        signal["time_stop_sec"] = int(min(ts + S_TIME_STOP_BONUS_SEC, TIME_STOP_MAX_SEC + 240))
        signal["breakeven_trigger_pct"] = S_BREAKEVEN_TRIGGER_PCT
        signal["breakeven_buffer_pct"] = S_BREAKEVEN_BUFFER_PCT
        signal["trail_start_pct"] = S_TRAIL_START_PCT
        signal["trail_backoff_pct"] = S_TRAIL_BACKOFF_PCT
    elif tier == "A":
        signal["take_profit_pct"] = round(min(tp + A_TP_BONUS_PCT, 6.9), 2)
        signal["time_stop_sec"] = int(min(ts + A_TIME_STOP_BONUS_SEC, TIME_STOP_MAX_SEC + 120))
        signal["breakeven_trigger_pct"] = A_BREAKEVEN_TRIGGER_PCT
        signal["breakeven_buffer_pct"] = A_BREAKEVEN_BUFFER_PCT
        signal["trail_start_pct"] = A_TRAIL_START_PCT
        signal["trail_backoff_pct"] = A_TRAIL_BACKOFF_PCT
    else:
        signal["breakeven_trigger_pct"] = BREAKEVEN_TRIGGER_PCT
        signal["breakeven_buffer_pct"] = BREAKEVEN_BUFFER_PCT
        signal["trail_start_pct"] = TRAIL_START_PCT
        signal["trail_backoff_pct"] = TRAIL_BACKOFF_PCT
    return signal


def signal_priority_value(signal):
    tier_bonus = {"S": 1.15, "A": 0.35, "B": 0.0}.get(signal.get("trade_tier", "B"), 0.0)
    leader_bonus = min(safe_float(signal.get("leader_score", 0)), 8.0) * 0.22
    return float(signal.get("edge_score", signal.get("signal_score", 0))) + tier_bonus + leader_bonus


def update_recent_leader_board(cache):
    global recent_leader_board
    board = {}
    ranked = sorted(cache.items(), key=lambda kv: safe_float(kv[1].get("leader_score", 0)), reverse=True)[:15]
    now_ts = time.time()
    for rank, (ticker, data) in enumerate(ranked, start=1):
        board[ticker] = {
            "rank": rank,
            "leader_score": safe_float(data.get('leader_score', 0)),
            "time": now_ts,
        }
    recent_leader_board = board


def leader_reason_suffix(signal):
    leader_score = safe_float(signal.get("leader_score", 0))
    if leader_score <= 0:
        return ""
    return f"\n- 주도주 점수 {leader_score:.2f}"


def get_pending_seen_count(ticker: str):
    if ticker in pending_buy_candidates:
        return int(pending_buy_candidates[ticker].get("seen_count", 0))
    return 0

# FULL FILE CONTINUES BELOW EXACTLY AS NEEDED FOR THE BOT.
# To keep this tool call manageable, the complete file is saved directly.



def update_scan_debug_snapshot(snapshot=None, note=""):
    global last_scan_debug_snapshot, last_scan_debug_snapshot_time, last_scan_debug_note
    try:
        last_scan_debug_snapshot = snapshot or []
        last_scan_debug_snapshot_time = time.time()
        last_scan_debug_note = note or ""
    except Exception as e:
        print(f"[scan_debug snapshot 오류] {e}")



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
                "turnover_score": turnover_score,
                "surge_score": surge_score,
            })
        except Exception:
            continue

    if not rows:
        return market_universe

    turnover_rank = {item["ticker"]: i + 1 for i, item in enumerate(sorted(rows, key=lambda x: x["turnover_recent"], reverse=True))}
    surge_rank = {item["ticker"]: i + 1 for i, item in enumerate(sorted(rows, key=lambda x: x["surge_score"], reverse=True))}

    for item in rows:
        t_rank = turnover_rank.get(item["ticker"], 999)
        s_rank = surge_rank.get(item["ticker"], 999)
        leader_score = 0.0
        if t_rank <= 5:
            leader_score += LEADER_TOP_TURNOVER_BONUS
        elif t_rank <= 15:
            leader_score += 0.8
        elif t_rank <= 30:
            leader_score += 0.45

        if s_rank <= 5:
            leader_score += LEADER_TOP_SURGE_BONUS
        elif s_rank <= 15:
            leader_score += 0.7
        elif s_rank <= 30:
            leader_score += 0.35

        leader_score += min(item["surge_score"], 8.0) * 0.18
        item["leader_score"] = round(leader_score, 2)
        item["turnover_rank"] = t_rank
        item["surge_rank"] = s_rank

    abs_sorted = sorted(rows, key=lambda x: x["turnover_recent"], reverse=True)[:ABSOLUTE_TURNOVER_CANDIDATES]
    surge_sorted = sorted(rows, key=lambda x: x["surge_score"], reverse=True)[:SURGE_CANDIDATES]
    merged = {}
    for item in abs_sorted + surge_sorted:
        merged[item["ticker"]] = item

    selected = list(merged.values())
    selected.sort(key=lambda x: (x["leader_score"] * 1.20) + (x["surge_score"] * 0.95) + (x["turnover_score"] * 0.35), reverse=True)
    market_universe = selected[:TOP_TICKERS]
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

            change_1 = get_recent_change_pct(df1, 2)
            change_3 = get_recent_change_pct(df1, 3)
            change_5 = get_recent_change_pct(df1, 5)
            vol_ratio_1m = get_vol_ratio(df1, 3, 15)
            range_pct_1m = get_range_pct(df1, 10)
            rsi_1m = get_rsi(df1, 14)

            cache[ticker] = {
                "price": price,
                "df_1m": df1,
                "df_3m": row["df_3m"],
                "turnover": row["turnover_recent"],
                "surge_score": row["surge_score"],
                "leader_score": row.get("leader_score", 0),
                "turnover_rank": row.get("turnover_rank", 999),
                "surge_rank": row.get("surge_rank", 999),
                "change_1": change_1,
                "change_3": change_3,
                "change_5": change_5,
                "vol_ratio_1m": vol_ratio_1m,
                "range_pct_1m": range_pct_1m,
                "rsi_1m": rsi_1m,
                "vol_ratio": vol_ratio_1m,
                "range_pct": range_pct_1m,
                "rsi": rsi_1m,
            }
        except Exception:
            continue

    if cache:
        shared_market_cache = cache
        shared_market_cache_time = now_ts
        update_recent_leader_board(cache)
    return shared_market_cache


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



def get_upper_wick_ratio(df):
    try:
        last = df.iloc[-1]
        body_top = max(float(last["open"]), float(last["close"]))
        high = float(last["high"])
        low = float(last["low"])
        if high <= low:
            return 0.0
        return (high - body_top) / (high - low)
    except Exception:
        return 0.0


def is_recent_overheated(df):
    try:
        change3 = get_recent_change_pct(df, 3)
        change5 = get_recent_change_pct(df, 5)
        return change3 >= OVERHEAT_3BAR_MAX_PCT or change5 >= OVERHEAT_5BAR_MAX_PCT
    except Exception:
        return False


def is_weak_close_candle(df):
    try:
        last = df.iloc[-1]
        o = float(last["open"])
        c = float(last["close"])
        h = float(last["high"])
        if c < o:
            return True
        body_ratio = candle_body_ratio(df)
        upper_wick_ratio = get_upper_wick_ratio(df)
        return body_ratio < WEAK_CLOSE_BODY_RATIO_MIN and upper_wick_ratio >= WEAK_CLOSE_UPPER_WICK_RATIO
    except Exception:
        return False


def entry_shape_block(strategy, df):
    if not ENTRY_SHAPE_FILTER_ON:
        return False, ""
    if df is None or len(df) < 8:
        return False, ""
    if strategy in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "PREPUMP"]:
        if is_recent_overheated(df):
            return True, "최근 봉이 너무 과열"
        if is_weak_close_candle(df):
            return True, "봉 마감 힘이 약해"
    return False, ""


def get_recent_closed_logs_window(sec=AUTO_PAUSE_LOOKBACK_SEC):
    now_ts = int(time.time())
    return [
        x for x in trade_logs
        if x.get("type") in CLOSE_TYPES
        and int(x.get("time", 0)) >= int(auto_pause_reset_ignore_before or 0)
        and now_ts - int(x.get("time", 0)) <= sec
    ]


def get_recent_loss_streak():
    streak = 0
    ignore_before = int(auto_pause_reset_ignore_before or 0)
    for x in reversed(trade_logs):
        if x.get("type") not in CLOSE_TYPES:
            continue
        if int(x.get("time", 0)) < ignore_before:
            break
        if float(x.get("pnl_pct", 0)) <= 0:
            streak += 1
        else:
            break
        if streak >= AUTO_PAUSE_STREAK_COUNT:
            break
    return streak


def activate_auto_pause(reason):
    global paused_until, pause_reason
    paused_until = int(time.time()) + AUTO_PAUSE_SECONDS
    pause_reason = reason


def clear_auto_pause_if_needed():
    global paused_until, pause_reason
    if paused_until and time.time() >= paused_until:
        paused_until = 0
        pause_reason = ""


def reset_auto_pause_state(bypass_sec=900):
    global paused_until, pause_reason, auto_pause_bypass_until, auto_pause_reset_ignore_before
    now_ts = int(time.time())
    paused_until = 0
    pause_reason = ""
    auto_pause_bypass_until = now_ts + max(int(bypass_sec), 0)
    auto_pause_reset_ignore_before = now_ts


def should_pause_auto_buy_now():
    clear_auto_pause_if_needed()

    if auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time())
        return False, f"수동 해제 적용 중 / 자동 쉬기 재판정까지 {max(remain, 0)}초"

    if not AUTO_PAUSE_ON:
        return False, ""

    if paused_until and time.time() < paused_until:
        remain = int(paused_until - time.time())
        return True, f"{max(remain, 0)}초 남음 / {pause_reason}"

    streak = get_recent_loss_streak()
    if streak >= AUTO_PAUSE_STREAK_COUNT:
        reason = f"최근 연속 손실 {streak}회"
        activate_auto_pause(reason)
        return True, f"{AUTO_PAUSE_SECONDS}초 쉬기 / {reason}"

    recent_logs = get_recent_closed_logs_window(AUTO_PAUSE_LOOKBACK_SEC)
    if len(recent_logs) >= 3:
        rolling_pnl = sum(float(x.get("pnl_pct", 0)) for x in recent_logs)
        if rolling_pnl <= AUTO_PAUSE_ROLLING_PNL:
            reason = f"최근 1시간 누적 {rolling_pnl:.2f}%"
            activate_auto_pause(reason)
            return True, f"{AUTO_PAUSE_SECONDS}초 쉬기 / {reason}"

    return False, ""

def late_entry_block(strategy, df, current_price):
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

    if strategy in ["BREAKOUT", "CHASE"] and 0 <= dist_to_high_pct <= LATE_ENTRY_MAX_NEAR_HIGH_PCT and change_3 >= 1.0:
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


def base_signal(ticker, strategy, strategy_label, current_price, vol_ratio, change_pct, rsi=None, range_pct=None,
                signal_score=0, edge_score=0, leader_score=0, stop_loss_pct=0, take_profit_pct=0, time_stop_sec=0,
                invalid_break_level=0.0, pattern_tags=None, reference_high=0.0, reference_low=0.0, reason=""):
    return {
        "ticker": ticker,
        "strategy": strategy,
        "strategy_label": strategy_label,
        "strategy_help": strategy_help_text(strategy_label),
        "current_price": current_price,
        "vol_ratio": r(vol_ratio, 2),
        "change_pct": r(change_pct, 2),
        "rsi": r(rsi, 2) if rsi is not None else 0.0,
        "range_pct": r(range_pct, 2) if range_pct is not None else 0.0,
        "signal_score": r(signal_score, 2),
        "edge_score": r(edge_score, 2),
        "leader_score": r(leader_score, 2),
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "time_stop_sec": time_stop_sec,
        "invalid_break_level": invalid_break_level,
        "pattern_tags": pattern_tags or [],
        "reference_high": reference_high,
        "reference_low": reference_low,
        "reason": reason,
        "trade_tier": "B",
    }


def make_signal(**kwargs):
    return base_signal(**kwargs)


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
    ma5v, ma10v = ma(df, 5), ma(df, 10)
    range_pct = get_range_pct(df, 8)

    if vol_ratio < EARLY_CFG["vol_min"]:
        return None
    if change_pct < EARLY_CFG["change_min"] or change_pct > EARLY_CFG["change_max"]:
        return None
    if rsi < EARLY_CFG["rsi_min"] or rsi > EARLY_CFG["rsi_max"]:
        return None
    if range_pct < EARLY_CFG["range_min"]:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None
    if candle_body_ratio(df) < 0.48:
        return None
    if not short_trend_up(df):
        return None

    shape_block, _ = entry_shape_block("EARLY", df)
    if shape_block:
        return None

    block, _ = late_entry_block("EARLY", df, current_price)
    if block:
        return None
    if is_retest_after_failed_peak(df):
        return None
    if is_recent_spike_near_high(df, current_price):
        return None
    if is_false_start_near_high(df, current_price):
        return None
    if is_box_top_range_chase(df, current_price):
        return None

    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    prev2_close = float(df["close"].iloc[-3]) if len(df) >= 3 else prev_close
    if prev_close <= 0 or prev2_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    last2_change_pct = ((last_close - prev2_close) / prev2_close) * 100
    upper_wick_ratio = get_upper_wick_ratio(df)
    if last_jump_pct < EARLY_CFG["jump_min"]:
        return None
    if last_jump_pct > 0.78:
        return None
    if last2_change_pct > 1.30:
        return None
    if upper_wick_ratio > 0.36:
        return None

    base_score_v = 5.8 + min(vol_ratio, 4.2) * 0.86 + min(change_pct, 2.2) * 0.75 + min(data.get("surge_score", 0), 8) * 0.16
    base_score_v += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "EARLY")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "EARLY")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "EARLY")
    edge_score_v = expected_edge_score("EARLY", base_score_v, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

    return base_signal(
        ticker=ticker,
        strategy="EARLY",
        strategy_label="초반 선점형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=change_pct,
        rsi=rsi,
        range_pct=range_pct,
        signal_score=base_score_v,
        edge_score=edge_score_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        time_stop_sec=time_stop_sec,
        invalid_break_level=0.0,
        pattern_tags=pattern_info["tags"],
        reference_high=get_recent_high(df, 8, exclude_last=True),
        reference_low=get_recent_low(df, 8, exclude_last=False),
        reason=(
            f"- 이제 막 오르기 시작했어\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}\n"
            f"- 마지막 1봉 상승 {last_jump_pct:.2f}%\n"
            f"- 최근 2봉 상승 {last2_change_pct:.2f}%\n"
            f"- 초입 흐름이 살아있어"
            f"{pattern_reason_suffix(pattern_info)}\n- 주도주 점수 {safe_float(data.get('leader_score', 0)):.2f}"
        ),
    )


def analyze_prepump_entry(ticker: str, data: dict):
    df = data["df_3m"]
    current_price = data["price"]
    if df is None or len(df) < 30 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    vol_ratio = get_vol_ratio(df, 5, 20)
    change_pct = get_recent_change_pct(df, 5)
    ma5v, ma10v = ma(df, 5), ma(df, 10)
    range_pct = get_range_pct(df, 10)

    if change_pct < PREPUMP_CFG["change_min"] or change_pct > PREPUMP_CFG["change_max"]:
        return None
    if vol_ratio < PREPUMP_CFG["vol_min"]:
        return None
    if rsi < PREPUMP_CFG["rsi_min"] or rsi > PREPUMP_CFG["rsi_max"]:
        return None
    if range_pct < PREPUMP_CFG["range_min"]:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None

    shape_block, _ = entry_shape_block("PREPUMP", df)
    if shape_block:
        return None
    if is_recent_spike_near_high(df, current_price, lookback=10):
        return None
    if is_false_start_near_high(df, current_price, lookback=12):
        return None
    if is_box_top_range_chase(df, current_price, lookback=12):
        return None
    if is_retest_after_failed_peak(df, lookback=12):
        return None

    score_v = 5.1 + min(vol_ratio, 4.0) * 0.8 + min(data.get("surge_score", 0), 8) * 0.14 + pattern_info.get("bonus_score", 0)
    edge_v = expected_edge_score("PREPUMP", score_v, BASE_STOP_LOSS_PCT, BASE_TP_PCT, rsi, vol_ratio, change_pct, range_pct)

    return base_signal(
        ticker=ticker,
        strategy="PREPUMP",
        strategy_label="상승 시작형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=change_pct,
        rsi=rsi,
        range_pct=range_pct,
        signal_score=score_v,
        edge_score=edge_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        pattern_tags=pattern_info["tags"],
        reference_high=get_recent_high(df, 8, exclude_last=True),
        reference_low=get_recent_low(df, 8, exclude_last=False),
        reason="",
    )


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
    if total_pump_pct < PULLBACK_CFG["pump_min"]:
        return None

    last3 = df.tail(3)
    c1, c2, c3 = map(float, [last3["close"].iloc[0], last3["close"].iloc[1], last3["close"].iloc[2]])
    if not (c2 < c1 and c3 > c2):
        return None

    rebound_pct = ((c3 - c2) / c2) * 100 if c2 > 0 else 0.0
    if rebound_pct < PULLBACK_CFG["rebound_min"]:
        return None

    vol_ratio = get_vol_ratio(df, 3, 10)
    if vol_ratio < PULLBACK_CFG["vol_min"]:
        return None
    if is_tiny_tick_distortion(df, current_price):
        return None
    if is_retest_after_failed_peak(df, lookback=12):
        return None
    if is_recent_spike_near_high(df, current_price, lookback=12):
        return None
    if is_box_top_range_chase(df, current_price, lookback=12):
        return None
    if current_price < 1.0 and total_pump_pct >= 8.0:
        return None
    if total_pump_pct >= 18.0 and vol_ratio < 3.5:
        return None

    retest_gap_pct = ((recent_high - current_price) / recent_high) * 100 if recent_high > 0 else 999.0
    if total_pump_pct >= 6.0 and retest_gap_pct < PULLBACK_NEAR_HIGH_GAP_MIN_PCT:
        return None
    if total_pump_pct >= 10.0 and retest_gap_pct < PULLBACK_HOT_GAP_MIN_PCT:
        return None
    if total_pump_pct >= 14.0 and retest_gap_pct < PULLBACK_EXTREME_GAP_MIN_PCT:
        return None
    if vol_ratio >= PULLBACK_VOL_HOT_MIN and retest_gap_pct < PULLBACK_HOT_GAP_MIN_PCT:
        return None

    score_v = 4.8 + vol_ratio * 0.8 + pattern_info.get("bonus_score", 0.0)

    return base_signal(
        ticker=ticker,
        strategy="PULLBACK",
        strategy_label="눌림 반등형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=rebound_pct,
        signal_score=score_v,
        edge_score=score_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        pattern_tags=pattern_info["tags"],
        reason="",
    )



def analyze_trend_cont_entry(ticker: str, data: dict):
    df = data["df_1m"]
    current_price = data["price"]
    if df is None or len(df) < 40 or current_price <= 0:
        return None

    pattern_info = analyze_chart_patterns(df)
    rsi = safe_float(calculate_rsi(df).iloc[-1])
    vol_ratio = get_vol_ratio(df, 3, 15)
    ma5v, ma10v = ma(df, 5), ma(df, 10)
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None

    recent20 = df.tail(20)
    rise_low = float(recent20["low"].min())
    rise_high = float(recent20["high"].max())
    if rise_low <= 0 or rise_high <= rise_low:
        return None

    trend_change_pct = ((rise_high - rise_low) / rise_low) * 100
    if trend_change_pct < TREND_CONT_CFG["trend_change_min"] or trend_change_pct > TREND_CONT_CFG["trend_change_max"]:
        return None

    recent9 = df.tail(9)
    prior8 = recent9.iloc[:-1] if len(recent9) >= 2 else recent9
    recent_high = float(prior8["high"].max())
    recent_low = float(prior8["low"].min())
    if recent_high <= 0 or recent_low <= 0:
        return None

    pullback_pct = ((recent_high - current_price) / recent_high) * 100
    if pullback_pct < 0 or pullback_pct > TREND_CONT_CFG["pullback_max"]:
        return None

    recovery_pct = get_recent_change_pct(df, 3)
    if recovery_pct < TREND_CONT_CFG["recovery_min"] or recovery_pct > TREND_CONT_CFG["recovery_max"]:
        return None

    last1_change_pct = get_recent_change_pct(df, 1)
    if last1_change_pct > TREND_CONT_CFG["last1_change_max"]:
        return None

    retest_gap_pct = ((recent_high - current_price) / current_price) * 100
    if retest_gap_pct < TREND_CONT_CFG["high_retest_gap_min"] or retest_gap_pct > TREND_CONT_CFG["high_retest_gap_max"]:
        return None

    extension_pct = ((current_price - recent_high) / recent_high) * 100
    if extension_pct > TREND_CONT_CFG["extension_max"]:
        return None

    if rsi < TREND_CONT_CFG["rsi_min"] or rsi > TREND_CONT_CFG["rsi_max"]:
        return None
    if vol_ratio < TREND_CONT_CFG["vol_reaccel_min"]:
        return None
    if upper_wick_too_large(df):
        return None

    shape_block, _ = entry_shape_block("TREND_CONT", df)
    if shape_block:
        return None

    low_last5 = float(df["low"].tail(5).min())
    low_prev5 = float(df["low"].tail(10).head(5).min()) if len(df) >= 10 else low_last5
    if low_last5 < low_prev5 * 0.992:
        return None

    base_score_v = 6.0 + min(vol_ratio, 4.2) * 0.75 + min(trend_change_pct, 7.0) * 0.28 + min(recovery_pct, 1.2) * 0.8
    base_score_v += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, get_range_pct(df, 10), "TREND_CONT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, get_range_pct(df, 10), "TREND_CONT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, get_range_pct(df, 10), "TREND_CONT")
    edge_score_v = expected_edge_score("TREND_CONT", base_score_v, stop_loss_pct, take_profit_pct, rsi, vol_ratio, recovery_pct, get_range_pct(df, 10))

    return base_signal(
        ticker=ticker,
        strategy="TREND_CONT",
        strategy_label="추세 지속형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=recovery_pct,
        rsi=rsi,
        range_pct=get_range_pct(df, 10),
        signal_score=base_score_v,
        edge_score=edge_score_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        time_stop_sec=time_stop_sec,
        invalid_break_level=recent_low,
        pattern_tags=pattern_info["tags"],
        reference_high=recent_high,
        reference_low=recent_low,
        reason=(
            f"- 오늘 강한 흐름이 이어지는 코인이야\n"
            f"- 잠깐 눌렸다가 다시 위로 가려는 모습이 보여\n"
            f"- 거래량 재가속 {vol_ratio:.2f}배\n"
            f"- 직전 고점까지 남은 거리 {retest_gap_pct:.2f}%\n"
            f"- 마지막 1봉 상승 {last1_change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}\n- 주도주 점수 {safe_float(data.get('leader_score', 0)):.2f}"
        ),
    )



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
    ma5v, ma10v = ma(df, 5), ma(df, 10)
    range_pct = get_range_pct(df, 8)
    body_ratio = candle_body_ratio(df)

    if change_pct < 0 or change_pct > PRE_BREAKOUT_CFG["change_max"]:
        return None
    if vol_ratio < PRE_BREAKOUT_CFG["vol_min"]:
        return None
    if rsi < PRE_BREAKOUT_CFG["rsi_min"] or rsi > PRE_BREAKOUT_CFG["rsi_max"]:
        return None
    if range_pct < PRE_BREAKOUT_CFG["range_min"]:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None
    if body_ratio < 0.40:
        return None

    shape_block, _ = entry_shape_block("PRE_BREAKOUT", df)
    if shape_block:
        return None

    block, _ = late_entry_block("PRE_BREAKOUT", df, current_price)
    if block:
        return None

    recent_high = get_recent_high(df, 8, exclude_last=True)
    if recent_high <= 0:
        return None
    gap_to_break = ((recent_high - current_price) / current_price) * 100
    if gap_to_break < 0 or gap_to_break > PRE_BREAKOUT_CFG["gap_max"]:
        return None

    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if last_jump_pct < PRE_BREAKOUT_CFG["jump_min"]:
        return None

    base_score_v = 6.1 + min(vol_ratio, 4.5) * 0.85 + (PRE_BREAKOUT_CFG["gap_max"] - gap_to_break) * 3.4 + min(data.get("surge_score", 0), 8) * 0.16
    base_score_v += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "PRE_BREAKOUT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "PRE_BREAKOUT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "PRE_BREAKOUT")
    edge_score_v = expected_edge_score("PRE_BREAKOUT", base_score_v, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

    return base_signal(
        ticker=ticker,
        strategy="PRE_BREAKOUT",
        strategy_label="쏘기 직전형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=change_pct,
        rsi=rsi,
        range_pct=range_pct,
        signal_score=base_score_v,
        edge_score=edge_score_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        time_stop_sec=time_stop_sec,
        invalid_break_level=recent_high,
        pattern_tags=pattern_info["tags"],
        reference_high=recent_high,
        reference_low=get_recent_low(df, 8, exclude_last=False),
        reason=(
            f"- 거의 돌파 직전이야\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- 직전 고점까지 남은 거리 {gap_to_break:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}\n- 주도주 점수 {safe_float(data.get('leader_score', 0)):.2f}"
        ),
    )


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
    ma5v, ma10v = ma(df, 5), ma(df, 10)
    range_pct = get_range_pct(df, 8)
    body_ratio = candle_body_ratio(df)

    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100

    if change_pct < BREAKOUT_CFG["change_min"] or change_pct > BREAKOUT_CFG["change_max"]:
        return None
    if vol_ratio < BREAKOUT_CFG["vol_min"]:
        return None
    if rsi < BREAKOUT_CFG["rsi_min"] or rsi > BREAKOUT_CFG["rsi_max"]:
        return None
    if range_pct < BREAKOUT_CFG["range_min"]:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if not strong_momentum_filter(df, change_pct, vol_ratio):
        return None
    if body_ratio < 0.5:
        return None
    if last_jump_pct < BREAKOUT_CFG["jump_min"]:
        return None

    recent_high = get_recent_high(df, 10, exclude_last=True)
    if recent_high <= 0:
        return None
    break_pct = ((current_price - recent_high) / recent_high) * 100
    if break_pct < BREAKOUT_CFG["break_min"]:
        return None

    shape_block, _ = entry_shape_block("BREAKOUT", df)
    if shape_block:
        return None

    block, _ = late_entry_block("BREAKOUT", df, current_price)
    if block:
        return None

    base_score_v = 6.0 + min(vol_ratio, 5.0) * 0.80 + min(change_pct, 3.4) * 0.65 + min(break_pct, 0.7) * 1.25 + min(data.get("surge_score", 0), 8) * 0.12
    base_score_v += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "BREAKOUT")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "BREAKOUT")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "BREAKOUT")
    edge_score_v = expected_edge_score("BREAKOUT", base_score_v, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

    return base_signal(
        ticker=ticker,
        strategy="BREAKOUT",
        strategy_label="급등 돌파형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=change_pct,
        rsi=rsi,
        range_pct=range_pct,
        signal_score=base_score_v,
        edge_score=edge_score_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        time_stop_sec=time_stop_sec,
        invalid_break_level=recent_high,
        pattern_tags=pattern_info["tags"],
        reference_high=recent_high,
        reference_low=get_recent_low(df, 8, exclude_last=False),
        reason=(
            f"- 힘 있게 고점 돌파 중이야\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- 돌파폭 {break_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}\n- 주도주 점수 {safe_float(data.get('leader_score', 0)):.2f}"
        ),
    )


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
    ma5v, ma10v = ma(df, 5), ma(df, 10)
    range_pct = get_range_pct(df, 8)
    body_ratio = candle_body_ratio(df)

    if change_pct < CHASE_CFG["change_min"] or change_pct > CHASE_CFG["change_max"]:
        return None
    if vol_ratio < CHASE_CFG["vol_min"]:
        return None
    if rsi < CHASE_CFG["rsi_min"] or rsi > CHASE_CFG["rsi_max"]:
        return None
    if range_pct < CHASE_CFG["range_min"]:
        return None
    if ma5v <= 0 or ma10v <= 0 or ma5v < ma10v:
        return None
    if upper_wick_too_large(df):
        return None
    if body_ratio < 0.55:
        return None

    block, _ = late_entry_block("CHASE", df, current_price)
    if block:
        return None

    last_close = float(df["close"].iloc[-1])
    prev_close = float(df["close"].iloc[-2])
    if prev_close <= 0:
        return None
    last_jump_pct = ((last_close - prev_close) / prev_close) * 100
    if last_jump_pct < CHASE_CFG["jump_min"]:
        return None

    base_score_v = 5.0 + min(vol_ratio, 6.0) * 0.65 + min(change_pct, 3.4) * 0.52 + min(data.get("surge_score", 0), 8) * 0.10
    base_score_v += pattern_bonus_score(pattern_info)

    stop_loss_pct = dynamic_stop_loss_pct(vol_ratio, range_pct, "CHASE")
    take_profit_pct = dynamic_take_profit_pct(vol_ratio, range_pct, "CHASE")
    time_stop_sec = dynamic_time_stop_sec(vol_ratio, range_pct, "CHASE")
    edge_score_v = expected_edge_score("CHASE", base_score_v, stop_loss_pct, take_profit_pct, rsi, vol_ratio, change_pct, range_pct)

    return base_signal(
        ticker=ticker,
        strategy="CHASE",
        strategy_label="추격형",
        current_price=current_price,
        vol_ratio=vol_ratio,
        change_pct=change_pct,
        rsi=rsi,
        range_pct=range_pct,
        signal_score=base_score_v,
        edge_score=edge_score_v,
        leader_score=safe_float(data.get('leader_score', 0)) + min(get_pending_seen_count(ticker) * LEADER_REPEAT_SEEN_BONUS, LEADER_MAX_REPEAT_BONUS),
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        time_stop_sec=time_stop_sec,
        invalid_break_level=0.0,
        pattern_tags=pattern_info["tags"],
        reference_high=get_recent_high(df, 8, exclude_last=True),
        reference_low=get_recent_low(df, 8, exclude_last=False),
        reason=(
            f"- 이미 오르는 중인데 힘이 아주 강해\n"
            f"- 거래량 {vol_ratio:.2f}배\n"
            f"- 최근 상승 {change_pct:.2f}%\n"
            f"- RSI {rsi:.2f}"
            f"{pattern_reason_suffix(pattern_info)}\n- 주도주 점수 {safe_float(data.get('leader_score', 0)):.2f}"
        ),
    )


def get_safe_use_krw(multiplier: float = 1.0):
    krw = get_krw_balance()
    use_krw = min(krw * KRW_USE_RATIO, MAX_ENTRY_KRW)
    use_krw = (use_krw - ORDER_BUFFER_KRW) * multiplier
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
    qty = math.floor((use_krw / safe_price) * 100000000) / 100000000
    return qty, use_krw, safe_price


def build_position(signal, filled_entry, filled_qty, used_krw):
    regime = get_market_regime()
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
        "trade_tier": signal.get("trade_tier", "B"),
        "breakeven_trigger_pct": signal.get("breakeven_trigger_pct", BREAKEVEN_TRIGGER_PCT),
        "breakeven_buffer_pct": signal.get("breakeven_buffer_pct", BREAKEVEN_BUFFER_PCT),
        "trail_start_pct": signal.get("trail_start_pct", TRAIL_START_PCT),
        "trail_backoff_pct": signal.get("trail_backoff_pct", TRAIL_BACKOFF_PCT),
        "managed_by_bot": True,
        "invalid_break_level": safe_float(signal.get("invalid_break_level", 0)),
        "entry_strategy_snapshot": signal["strategy"],
        "pattern_tags": signal.get("pattern_tags", []),
        "entry_regime": regime.get("name", "UNKNOWN"),
        "from_pending_buy": bool(signal.get("from_pending_buy", False)),
        "max_profit_pct": 0.0,
        "max_drawdown_pct": 0.0,
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
        qty, _, _ = calc_order_qty(ticker, entry_price, multiplier)
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
                    last_error = msg
                    if status == "5600":
                        continue
                    return False, f"매수 실패 / 응답: {result}"

            ok_fill, after_qty = confirm_buy_filled(ticker, before_qty, retries=5, sleep_sec=1.0)
            after_krw = get_krw_balance()

            filled_qty = round(max(after_qty - before_qty, 0), 8)
            krw_used_real = max(before_krw - after_krw, 0)
            if (not ok_fill) or filled_qty <= 0:
                last_error = f"체결 확인이 안 됐어 / 응답: {result}"
                continue

            filled_entry = (krw_used_real / filled_qty) if krw_used_real > 0 else entry_price
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
                "trade_tier": signal.get("trade_tier", "B"),
                "reason": signal["reason"],
                "managed_by_bot": True,
                "invalid_break_level": signal.get("invalid_break_level", 0),
                "pattern_tags": signal.get("pattern_tags", []),
                "entry_regime": get_market_regime().get("name", "UNKNOWN"),
                "from_pending_buy": bool(signal.get("from_pending_buy", False)),
            })

            return True, {"entry": filled_entry, "qty": filled_qty, "used_krw": krw_used_real}
        except Exception as e:
            last_error = str(e)

    return False, f"매수는 시도했지만 주문 금액이 빡빡해서 실패했어 / 마지막 사유: {last_error}"



def confirm_buy_filled(ticker: str, before_balance: float, retries: int = 5, sleep_sec: float = 1.0):
    best_after = before_balance
    for _ in range(retries):
        time.sleep(sleep_sec)
        after_balance = get_balance(ticker)
        if after_balance > best_after:
            best_after = after_balance
        if after_balance > before_balance * 1.0001:
            return True, best_after
    return best_after > before_balance, best_after


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
        sorted_items = sorted(pending_buy_candidates.items(), key=lambda kv: (safe_float(kv[1].get("leader_score", 0)) * 0.8) + safe_float(kv[1].get("edge_score", 0)), reverse=True)
        keep = dict(sorted_items[:PENDING_BUY_MAX_ITEMS])
        pending_buy_candidates.clear()
        pending_buy_candidates.update(keep)
        changed = True

    if changed:
        save_pending_buys()


def strategy_allowed_in_regime(strategy, regime):
    if not REGIME_FILTER_ON:
        return True
    if regime["name"] == "BLOCK":
        return False
    if strategy == "BREAKOUT" and not regime.get("allow_breakout", True):
        return False
    if strategy == "TREND_CONT" and regime["name"] == "WEAK":
        return False
    if strategy == "CHASE" and regime["name"] in ["SIDEWAYS", "WEAK"]:
        return False
    return True


def watch_strategy_allowed_in_regime(strategy, regime):
    if not REGIME_FILTER_ON:
        return True
    if regime["name"] == "BLOCK":
        return False
    if strategy == "CHASE":
        return False
    if regime["name"] == "WEAK" and strategy == "BREAKOUT":
        return False
    return True


def should_auto_buy_signal(signal, regime=None):
    strategy = signal["strategy"]
    edge = float(signal.get("edge_score", 0))
    tp = float(signal.get("take_profit_pct", 0))
    if strategy not in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "CHASE"]:
        return False
    if strategy == "CHASE" and not ALLOW_CHASE:
        return False
    if tp < MIN_EXPECTED_TP_PCT:
        return False
    required_edge = MIN_EXPECTED_EDGE_SCORE
    tier = signal.get("trade_tier", "B")
    leader_score = safe_float(signal.get("leader_score", 0))
    if regime:
        if not strategy_allowed_in_regime(strategy, regime):
            return False
        if regime.get("name") == "SIDEWAYS":
            required_edge += 0.9
            if strategy == "EARLY":
                required_edge += 0.7
            if tier == "B":
                required_edge += 0.4
            if leader_score >= LEADER_HIGH_MIN:
                required_edge -= LEADER_SIDEWAYS_EDGE_DISCOUNT
        elif regime.get("name") == "WEAK":
            required_edge += 1.1
            if strategy in ["EARLY", "TREND_CONT"]:
                required_edge += 0.5
            if tier != "S":
                required_edge += 0.3
            if leader_score >= LEADER_STRONG_MIN:
                required_edge -= LEADER_WEAK_EDGE_DISCOUNT
        elif regime.get("name") == "NORMAL":
            if strategy == "EARLY":
                required_edge += 0.2
    if strategy == "EARLY" and tier == "B" and regime and regime.get("name") in ["SIDEWAYS", "WEAK"] and leader_score < LEADER_HIGH_MIN:
        return False
    if edge < required_edge:
        return False
    return True


def candidate_eligible_for_store(signal, regime):
    if not PENDING_BUY_ON:
        return False
    strategy = signal["strategy"]

    if strategy in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "CHASE"]:
        if strategy == "CHASE" and not ALLOW_CHASE:
            return False
        if not strategy_allowed_in_regime(strategy, regime):
            return False
        edge = safe_float(signal.get("edge_score", 0))
        score = safe_float(signal.get("signal_score", 0))
        return not (edge < PENDING_BUY_MIN_EDGE and score < PENDING_BUY_MIN_SCORE)

    if strategy == "PREPUMP":
        edge = safe_float(signal.get("edge_score", 0))
        score = safe_float(signal.get("signal_score", 0))
        vol = safe_float(signal.get("vol_ratio", 0))
        if edge < PREPUMP_PENDING_EDGE_MIN and score < PREPUMP_PENDING_SCORE_MIN:
            return False
        if vol < PREPUMP_PENDING_VOL_MIN:
            return False
        return True

    if strategy == "LEADER_WATCH":
        leader = safe_float(signal.get("leader_score", 0))
        score = safe_float(signal.get("signal_score", 0))
        vol = safe_float(signal.get("vol_ratio", 0))
        return leader >= 2.4 and (score >= 4.6 or vol >= 1.1)

    return False


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
        "leader_score": safe_float(signal.get("leader_score", 0)),
        "reference_high": safe_float(signal.get("reference_high", 0)),
        "reference_low": safe_float(signal.get("reference_low", 0)),
        "pattern_tags": signal.get("pattern_tags", []),
        "seen_count": 1,
    }

    if ticker in pending_buy_candidates:
        old = pending_buy_candidates[ticker]
        data["created_at"] = safe_float(old.get("created_at", now_ts))
        if safe_float(data["edge_score"]) < safe_float(old.get("edge_score", 0)):
            data["edge_score"] = safe_float(old.get("edge_score", 0))
        if safe_float(data["leader_score"]) < safe_float(old.get("leader_score", 0)):
            data["leader_score"] = safe_float(old.get("leader_score", 0))
        data["seen_count"] = int(old.get("seen_count", 0)) + 1
        if not data["reference_high"]:
            data["reference_high"] = safe_float(old.get("reference_high", 0))
        if not data["reference_low"]:
            data["reference_low"] = safe_float(old.get("reference_low", 0))

    pending_buy_candidates[ticker] = data
    cleanup_pending_buy_candidates()
    save_pending_buys()


def update_pending_buy_candidates_from_results(results, regime):
    for signal in results:
        if signal.get("strategy") == "LEADER_WATCH":
            add_or_refresh_pending_buy_candidate(signal, regime)
            continue
        if not should_auto_buy_signal(signal, regime=regime):
            continue
        add_or_refresh_pending_buy_candidate(signal, regime)


def cooldown_ok(ticker):
    last = recent_signal_alerts.get(ticker, 0)
    return (time.time() - last) >= 300


def candidate_promote_ok(candidate, current_signal, regime):
    ticker = candidate["ticker"]
    if ticker in active_positions:
        return False, "이미 보유중"
    if len(active_positions) >= MAX_HOLDINGS:
        return False, "보유 제한"
    if not cooldown_ok(ticker):
        return False, "쿨다운"

    strategy = current_signal["strategy"]
    if strategy not in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT", "CHASE"]:
        return False, "승격 전략 아님"
    if not strategy_allowed_in_regime(strategy, regime):
        return False, "장 필터"
    if not should_auto_buy_signal(current_signal, regime=regime):
        return False, "자동매수 기준 미달"

    current_price = safe_float(current_signal.get("current_price", 0))
    reference_high = safe_float(candidate.get("reference_high", 0))
    reference_low = safe_float(candidate.get("reference_low", 0))
    if current_price <= 0:
        return False, "가격 오류"

    leader_score = max(safe_float(candidate.get("leader_score", 0)), safe_float(current_signal.get("leader_score", 0)))
    if reference_high > 0:
        recovery_pct = PROMOTE_RECOVERY_TO_HIGH_PCT if leader_score < LEADER_HIGH_MIN else PROMOTE_RECOVERY_TO_HIGH_PCT - 0.15
        if current_price < reference_high * (recovery_pct / 100):
            return False, "고점 회복 부족"
        extension_pct = ((current_price - reference_high) / reference_high) * 100
        extension_limit = PROMOTE_MAX_BREAKOUT_EXTENSION_PCT if leader_score < LEADER_HIGH_MIN else PROMOTE_MAX_BREAKOUT_EXTENSION_PCT + 0.08
        if extension_pct > extension_limit:
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
    required_vol = PROMOTE_MIN_VOL_RATIO
    if current_signal["strategy"] == "EARLY":
        required_vol += 0.15
    elif current_signal["strategy"] == "TREND_CONT":
        required_vol += 0.05
    if vol_ratio_now < required_vol:
        return False, "거래량 재확인 부족"

    if current_signal["strategy"] == "TREND_CONT":
        ref_high_now = safe_float(current_signal.get("reference_high", 0))
        if ref_high_now > 0:
            retest_gap_now = ((ref_high_now - current_price) / current_price) * 100
            if retest_gap_now < TREND_CONT_CFG["high_retest_gap_min"]:
                return False, "고점에 너무 붙음"
    if current_signal["strategy"] == "EARLY":
        last1 = get_recent_change_pct(df, 1)
        last2 = get_recent_change_pct(df, 2)
        if last1 > 0.78 or last2 > 1.30:
            return False, "초반형이 너무 급함"

    block, reason = late_entry_block(current_signal["strategy"], df, current_price)
    if block:
        return False, reason
    return True, "승격 가능"


def process_pending_buy_promotions(shared_cache=None):
    if not PENDING_BUY_ON or not AUTO_BUY or not is_auto_time() or len(active_positions) >= MAX_HOLDINGS:
        return

    paused, _ = should_pause_auto_buy_now()
    if paused:
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

    candidates_sorted = sorted(pending_buy_candidates.values(), key=lambda x: (safe_float(x.get("leader_score", 0)) * 0.8) + safe_float(x.get("edge_score", 0)), reverse=True)

    for item in candidates_sorted:
        ticker = item["ticker"]
        created_at = safe_float(item.get("created_at", 0))
        if time.time() - created_at < PENDING_BUY_RECHECK_MIN_SEC:
            continue
        if ticker not in cache:
            continue

        data = cache[ticker]
        current_signal = None
        for analyzer in [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry, analyze_chase_entry]:
            try:
                sig = analyzer(ticker, data)
                if sig:
                    current_signal = sig
                    break
            except Exception:
                continue

        if not current_signal:
            continue

        ok, _ = candidate_promote_ok(item, current_signal, regime)
        if not ok:
            continue

        current_signal["from_pending_buy"] = True
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
방식: {current_signal['strategy_label']} / 주도주 {safe_float(current_signal.get('leader_score',0)):.2f} / 등급 {current_signal.get('trade_tier','B')}

💰 매수가(보정): {fmt_price(result['entry'])}
📦 수량: {result['qty']:.6f}
💵 사용한 금액(대략): {fmt_price(result['used_krw'])}

🛑 손절 기준: {fmt_pct(current_signal['stop_loss_pct'])}
🎯 목표 수익: {fmt_pct(current_signal['take_profit_pct'])}
⏱ 오래 안 가면 정리: {int(current_signal['time_stop_sec'] / 60)}분
📈 수익 기대 점수: {current_signal['edge_score']:.2f}{tag_text}

처음엔 후보였는데,
2차 확인 후 다시 강해져서 들어갔어.
주도주 점수도 높게 유지된 상태였어.
"""
            )
            return


def is_ticker_blocked_for_watch_alert(ticker: str) -> bool:
    # 후보 저장 상태여도 후보 알림은 막지 않음
    return ticker in active_positions or ticker in pending_sells


def get_strategy_rank(strategy: str) -> int:
    rank_map = {
        "PREPUMP": 1,
        "PULLBACK": 2,
        "EARLY": 3,
        "PRE_BREAKOUT": 4,
        "TREND_CONT": 5,
        "BREAKOUT": 6,
        "CHASE": 7,
    }
    return rank_map.get(strategy, 0)


def build_watch_snapshot(item: dict):
    now_ts = time.time()
    return {
        "strategy": item.get("strategy", ""),
        "strategy_label": item.get("strategy_label", ""),
        "change_pct": safe_float(item.get("change_pct", item.get("change_5", 0))),
        "change_5": safe_float(item.get("change_5", item.get("change_pct", 0))),
        "vol_ratio": safe_float(item.get("vol_ratio", item.get("vol_ratio_1m", 0))),
        "signal_score": safe_float(item.get("signal_score", item.get("edge_score", 0))),
        "edge_score": safe_float(item.get("edge_score", item.get("signal_score", 0))),
        "leader_score": safe_float(item.get("leader_score", 0)),
        "pattern_tags": list(item.get("pattern_tags", [])),
        "price": safe_float(item.get("current_price", item.get("price", 0))),
        "turnover": safe_float(item.get("turnover", 0)),
        "rsi": safe_float(item.get("rsi", item.get("rsi_1m", 50))),
        "saved_at": safe_float(item.get("saved_at", now_ts)) or now_ts,
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
    prev_leader = safe_float(prev_snap.get("leader_score", 0))
    new_leader = safe_float(item.get("leader_score", 0))
    if new_leader - prev_leader >= 0.9:
        reasons.append(f"주도주 점수 상승 ({prev_leader:.2f} → {new_leader:.2f})")

    prev_strategy = prev_snap.get("strategy", "")
    new_strategy = item.get("strategy", "")
    if get_strategy_rank(new_strategy) > get_strategy_rank(prev_strategy):
        reasons.append(f"전략 승격 ({prev_snap.get('strategy_label', prev_strategy)} → {item.get('strategy_label', new_strategy)})")

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

    renotice_count = int(recent_watch_renotice_counts.get(ticker, 0))
    if now_ts - prev_time >= WATCH_RENOTICE_SEC and renotice_count < WATCH_RENOTICE_MAX_PER_TICKER:
        if should_allow_time_renotice(item):
            return True, "renotice", "시간이 지나도 흐름이 유지돼 다시 확인"

    return False, "", ""


def save_watch_snapshot(ticker: str, item: dict, now_ts: float, alert_type: str = "new"):
    recent_watch_alerts[ticker] = now_ts
    snap = build_watch_snapshot(item)
    snap["saved_at"] = now_ts
    recent_watch_snapshots[ticker] = snap
    if alert_type in ["new", "upgrade"]:
        recent_watch_renotice_counts[ticker] = 0
    elif alert_type == "renotice":
        recent_watch_renotice_counts[ticker] = int(recent_watch_renotice_counts.get(ticker, 0)) + 1


def signal_score(signal):
    return float(signal.get("edge_score", signal.get("signal_score", 0)))


def watch_overheat_block(df, current_price, change_3, change_5, vol_ratio):
    if df is None or len(df) < 12 or current_price <= 0:
        return False, ""

    recent_high = get_recent_high(df, 8, exclude_last=True)
    dist_to_high_pct = 999.0
    if recent_high > 0:
        dist_to_high_pct = ((recent_high - current_price) / current_price) * 100

    if is_tiny_tick_distortion(df, current_price):
        return True, "초저가 호가 움직임만으로 상승률이 과장돼 보여"
    if is_recent_spike_near_high(df, current_price):
        return True, "이미 한 번 급하게 오른 뒤 고점 근처라 초반 자리로 보기 어려워"
    if is_box_top_range_chase(df, current_price):
        return True, "박스권 상단/고점 근처라 오르기 전에 뜬 후보로 보기 어려워"
    if is_retest_after_failed_peak(df):
        return True, "최근 고점 찍고 밀린 뒤 재반등이라 초반 자리로 보기 애매해"
    if change_5 >= 12.0:
        return True, f"이미 너무 많이 오른 자리 ({change_5:.2f}%)"
    if change_3 >= 6.0:
        return True, f"최근 3봉 상승이 커서 늦은 자리일 수 있어 ({change_3:.2f}%)"
    if change_5 >= 7.5 and vol_ratio < 3.0:
        return True, f"상승폭 대비 거래량이 아주 강하지 않아 늦은 자리일 수 있어 ({change_5:.2f}% / {vol_ratio:.2f}배)"
    if 0 <= dist_to_high_pct <= 0.18 and change_3 >= 2.1:
        return True, f"고점 바로 밑 추격 자리일 수 있어 ({dist_to_high_pct:.2f}%)"

    return False, ""


def should_allow_time_renotice(item):
    edge = safe_float(item.get("edge_score", 0))
    change_pct = safe_float(item.get("change_pct", 0))
    vol_ratio = safe_float(item.get("vol_ratio", 0))
    leader_score = safe_float(item.get("leader_score", 0))
    strategy = item.get("strategy", "")

    if get_watch_caution_text(item):
        return False

    if strategy == "LEADER_WATCH":
        if change_pct > 5.5:
            return False
        if edge < 6.4 and vol_ratio < 1.75 and leader_score < 2.35:
            return False
        return True

    if change_pct > 7.0:
        return False
    return edge >= 6.4 or vol_ratio >= 1.9 or leader_score >= 2.2


def collect_signals_from_cache(cache, auto_only=False, regime=None):
    results = []
    auto_analyzers = [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry, analyze_chase_entry]
    watch_analyzers = [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry, analyze_prepump_entry, analyze_pullback_entry]
    analyzers = auto_analyzers if auto_only else watch_analyzers

    for ticker, data in cache.items():
        for analyzer in analyzers:
            try:
                signal = analyzer(ticker, data)
                if not signal:
                    continue
                signal = apply_trade_tier_adjustments(signal, regime=regime)
                if regime:
                    if auto_only:
                        if not strategy_allowed_in_regime(signal["strategy"], regime):
                            continue
                    else:
                        if not watch_strategy_allowed_in_regime(signal["strategy"], regime):
                            continue
                if auto_only and not should_auto_buy_signal(signal, regime=regime):
                    continue
                results.append(signal)
            except Exception:
                continue

    if (not auto_only) and not results:
        try:
            relaxed = build_leader_watch_candidates(cache, regime, relaxed=True)
            if relaxed:
                results.extend(relaxed[:10])
            else:
                quick = build_quick_watch_candidates(cache, regime)
                if quick:
                    results.extend(quick[:8])
        except Exception:
            pass

    return results


def dedupe_best_signal_per_ticker(results, key_name="signal_score"):
    best_per_ticker = {}
    for signal in results:
        t = signal["ticker"]
        s = float(signal.get(key_name, 0))
        if t not in best_per_ticker or s > float(best_per_ticker[t].get(key_name, 0)):
            best_per_ticker[t] = signal
    return list(best_per_ticker.values())



def build_quick_watch_candidates(cache, regime=None):
    quick = []
    regime_name = (regime or {}).get("name", "NORMAL")
    for ticker, data in cache.items():
        try:
            df = data.get("df_1m")
            current_price = safe_float(data.get("price", 0))
            if current_price <= 0:
                continue
            change_1 = safe_float(data.get("change_1", 0))
            change_3 = safe_float(data.get("change_3", 0))
            change_5 = safe_float(data.get("change_5", 0))
            vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0)))
            leader_score = safe_float(data.get("leader_score", 0))
            range_pct = safe_float(data.get("range_pct", data.get("range_pct_1m", 0)))
            rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50)))
            turnover_rank = int(safe_float(data.get("turnover_rank", 999), 999))
            surge_rank = int(safe_float(data.get("surge_rank", 999), 999))

            top_rank_ok = turnover_rank <= 55 or surge_rank <= 55
            activity_score = (
                max(change_5, 0) * 1.45
                + max(change_3, 0) * 0.90
                + max(change_1, 0) * 0.45
                + max(vol_ratio - 0.9, 0) * 2.0
                + max(range_pct - 0.08, 0) * 0.45
                + leader_score * 0.90
            )

            if regime_name == "BLOCK":
                continue
            if change_1 <= -1.2 and change_3 <= -1.4 and change_5 <= -1.8:
                continue
            if rsi >= 90 and change_3 < 1.0:
                continue
            if not top_rank_ok and activity_score < 0.85:
                continue
            if vol_ratio < 1.00 and change_5 < 0.20:
                continue
            if regime_name == "WEAK" and change_5 > 4.2 and vol_ratio < 1.25:
                continue
            if df is not None and len(df) >= 12:
                blocked, block_reason = watch_overheat_block(df, current_price, change_3, change_5, vol_ratio)
                if blocked:
                    continue

            label = "테스트 관찰형"
            if change_5 >= 0.6 and vol_ratio >= 1.25:
                label = "테스트 상승형"
            elif top_rank_ok:
                label = "테스트 주도주형"

            quick.append(make_signal(
                ticker=ticker,
                strategy="LEADER_WATCH",
                strategy_label=label,
                current_price=current_price,
                vol_ratio=max(vol_ratio, 0.85),
                change_pct=max(change_1, change_3, change_5),
                rsi=rsi,
                range_pct=max(range_pct, 0.08),
                score=max(4.6, activity_score + 4.2),
                edge_score=max(4.4, activity_score + 3.8),
                leader_score=leader_score,
                stop_loss_pct=-1.4,
                take_profit_pct=3.6,
                time_stop_sec=600,
                pattern_tags=[],
                reason=f"테스트 완화 후보 / 5분상승 {change_5:.2f}% / 거래량 {vol_ratio:.2f}배 / 주도 {leader_score:.2f} / 순위 T{turnover_rank}/S{surge_rank}",
            ))
        except Exception:
            continue

    quick.sort(
        key=lambda x: (
            safe_float(x.get("edge_score", 0))
            + safe_float(x.get("leader_score", 0)) * 0.9
            + safe_float(x.get("change_pct", 0)) * 0.25
        ),
        reverse=True,
    )
    return quick[:12]


def build_leader_watch_candidates(cache, regime, relaxed=False):
    fallback = []
    regime_name = (regime or {}).get("name", "NORMAL")
    for ticker, data in cache.items():
        try:
            df = data.get("df_1m")
            if df is None or len(df) < 20:
                continue

            current_price = safe_float(data.get("price", 0))
            if current_price <= 0:
                continue

            leader_score = safe_float(data.get("leader_score", 0))
            vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0))) or get_vol_ratio(df, 3, 15)
            change_1 = safe_float(data.get("change_1", 0))
            change_3 = safe_float(data.get("change_3", 0))
            change_5 = safe_float(data.get("change_5", 0))
            range_pct = safe_float(data.get("range_pct", data.get("range_pct_1m", 0))) or get_range_pct(df, 10)
            rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50))) or get_rsi(df, 14)
            turnover_rank = int(safe_float(data.get("turnover_rank", 999), 999))
            surge_rank = int(safe_float(data.get("surge_rank", 999), 999))

            activity_score = (
                max(change_5, 0) * 1.15
                + max(change_3, 0) * 0.70
                + max(change_1, 0) * 0.35
                + max(vol_ratio - 0.9, 0) * 1.8
                + max(range_pct - 0.10, 0) * 0.55
            )
            top_rank_ok = turnover_rank <= (55 if relaxed else 40) or surge_rank <= (55 if relaxed else 40)

            min_leader = 0.00 if relaxed else 0.20
            if leader_score < min_leader and not top_rank_ok and activity_score < (0.30 if relaxed else 0.50):
                continue
            if change_1 <= -1.10 and change_3 <= -1.30 and change_5 <= -1.60:
                continue
            if rsi >= 88 and change_3 < 1.10:
                continue
            if upper_wick_too_large(df) and change_3 < (0.15 if relaxed else 0.45) and vol_ratio < (0.95 if relaxed else 1.20):
                continue
            if regime_name == "WEAK" and change_5 > 4.0 and vol_ratio < 1.25:
                continue
            if regime_name == "SIDEWAYS" and change_3 > 3.0 and vol_ratio < 1.15:
                continue
            blocked, block_reason = watch_overheat_block(df, current_price, change_3, change_5, vol_ratio)
            if blocked and not (relaxed and top_rank_ok and vol_ratio >= 2.2 and change_5 <= 9.0):
                continue
            if is_retest_after_failed_peak(df) and change_5 >= 1.0 and not relaxed:
                continue

            pattern_tags = []
            if has_higher_lows(df):
                pattern_tags.append("저점높임")
            if big_bull_half_hold(df):
                pattern_tags.append("양봉절반유지")
            if fake_breakdown_recovery(df):
                pattern_tags.append("하락복구")

            leader_edge = max(
                4.0 if relaxed else 4.4,
                leader_score
                + min(max(change_5, 0), 3.2) * 0.78
                + min(vol_ratio, 4.2) * 0.58
                + (0.45 if top_rank_ok else 0.0)
            )

            label = "주도주 후보형"
            if change_5 >= 1.0 and vol_ratio >= 1.6:
                label = "상승 시작형"
            elif vol_ratio >= 2.0 and change_3 >= 0.55:
                label = "주도주 가속형"
            elif top_rank_ok and (change_5 >= 0.25 or vol_ratio >= 1.15):
                label = "주도주 관찰형"

            fallback.append(make_signal(
                ticker=ticker,
                strategy="LEADER_WATCH",
                strategy_label=label,
                current_price=current_price,
                vol_ratio=vol_ratio,
                change_pct=max(change_1, change_3, change_5),
                rsi=rsi,
                range_pct=range_pct,
                score=max(
                    4.8 if relaxed else 5.2,
                    leader_score
                    + max(change_5, 0) * 0.85
                    + min(vol_ratio, 4.0) * 0.62
                    + (0.35 if top_rank_ok else 0.0)
                ),
                edge_score=leader_edge,
                leader_score=leader_score,
                stop_loss_pct=-1.4,
                take_profit_pct=4.2,
                time_stop_sec=720,
                pattern_tags=pattern_tags,
                reason=f"시장주목도 {leader_score:.2f} / 5분상승 {change_5:.2f}% / 거래량 {vol_ratio:.2f}배 / 순위 T{turnover_rank}/S{surge_rank}",
            ))
        except Exception:
            continue

    fallback.sort(
        key=lambda x: (
            safe_float(x.get("leader_score", 0)) * 1.15
            + safe_float(x.get("edge_score", 0))
            + safe_float(x.get("change_pct", 0)) * 0.2
        ),
        reverse=True,
    )
    return fallback[:18]


def scan_watchlist(shared_cache=None):
    cache = shared_cache or build_shared_market_cache()
    if not cache:
        return

    regime = get_market_regime()
    results = collect_signals_from_cache(cache, auto_only=False, regime=regime)
    fallback_results = build_leader_watch_candidates(cache, regime)
    if not fallback_results:
        fallback_results = build_leader_watch_candidates(cache, regime, relaxed=True)
    if not fallback_results:
        fallback_results = build_quick_watch_candidates(cache, regime)
    if fallback_results:
        results.extend(fallback_results)
    if not results:
        return

    real_results = [x for x in results if x.get("strategy") != "LEADER_WATCH"]
    if results:
        update_pending_buy_candidates_from_results(results, regime)
    unique_results = dedupe_best_signal_per_ticker(results, key_name="signal_score")
    unique_results.sort(key=lambda x: signal_priority_value(x), reverse=True)
    top = unique_results[:18]

    new_entries, upgrade_entries, renotice_entries = [], [], []
    now_ts = time.time()

    for item in top:
        ticker = item["ticker"]
        send_ok, alert_type, reason_text = should_send_watch_alert(ticker, item, now_ts)
        if not send_ok:
            continue

        if alert_type == "upgrade":
            upgrade_entries.append((item, alert_type, reason_text))
        elif alert_type == "renotice":
            renotice_entries.append((item, alert_type, reason_text))
        else:
            new_entries.append((item, alert_type, reason_text))

        save_watch_snapshot(ticker, item, now_ts, alert_type=alert_type)

    if new_entries:
        send_watch_alert_bundle("👀 지금 볼 후보", new_entries)
    if upgrade_entries:
        send_watch_alert_bundle("🔁 다시 볼 만해진 후보", upgrade_entries)
    if renotice_entries:
        send_watch_alert_bundle("🔁 이전 후보 다시 확인", renotice_entries)


def scan_and_auto_trade(shared_cache=None):
    if not AUTO_BUY or not is_auto_time() or len(active_positions) >= MAX_HOLDINGS:
        return

    market_ok, market_msg, _ = get_btc_market_state()
    if BTC_FILTER_ON and not market_ok:
        print(f"[시장 차단] {market_msg}")
        return

    regime = get_market_regime()
    if REGIME_FILTER_ON and not regime["allow_auto_buy"]:
        print(f"[쉬는 장] {regime['message']}")
        return

    paused, pause_msg = should_pause_auto_buy_now()
    if paused:
        print(f"[자동 쉬기] {pause_msg}")
        return

    cache = shared_cache or build_shared_market_cache()
    if not cache:
        return

    candidates = collect_signals_from_cache(cache, auto_only=True, regime=regime)
    if not candidates:
        return

    candidates = dedupe_best_signal_per_ticker(candidates, key_name="edge_score")
    candidates.sort(key=signal_priority_value, reverse=True)
    best = candidates[0]
    ticker = best["ticker"]

    if ticker in pending_buy_candidates:
        return
    if not cooldown_ok(ticker):
        return

    recent_signal_alerts[ticker] = time.time()
    success, result = buy_market(best)
    if not success:
        send(f"\n❌ 자동매수 실패\n\n📊 {ticker}\n방식: {best['strategy_label']}\n\n사유:\n{result}\n")
        return

    tag_text = ""
    if best.get("pattern_tags"):
        tag_text = "\n📐 차트 구조: " + ", ".join(best["pattern_tags"])

    send(
        f"""
🔥 자동매수 완료

📊 {ticker}
방식: {best['strategy_label']} / 등급 {best.get('trade_tier','B')} / 주도주 {safe_float(best.get('leader_score',0)):.2f}

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
            "max_profit_pct": pos.get("max_profit_pct", 0),
            "max_drawdown_pct": pos.get("max_drawdown_pct", 0),
            "entry_regime": pos.get("entry_regime", "UNKNOWN"),
            "from_pending_buy": pos.get("from_pending_buy", False),
            "pattern_tags": pos.get("pattern_tags", []),
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
            if qty <= 0 or price <= 0 or qty * price < DUST_KEEP_MIN_KRW:
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
                "leader_score": leader_score,
                "trade_tier": trade_tier,
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



def update_position_run_stats(pos, pnl_pct):
    changed = False
    if pnl_pct > safe_float(pos.get("max_profit_pct", 0)):
        pos["max_profit_pct"] = round(float(pnl_pct), 2)
        changed = True
    if pnl_pct < safe_float(pos.get("max_drawdown_pct", 0)):
        pos["max_drawdown_pct"] = round(float(pnl_pct), 2)
        changed = True
    return changed


def post_entry_strength_fail(ticker, pos, current_price):
    if not POST_ENTRY_RECHECK_ON:
        return False, ""
    held_sec = int(time.time() - int(pos["entered_at"]))
    if held_sec < POST_ENTRY_MIN_CHECK_SEC or held_sec > POST_ENTRY_MAX_CHECK_SEC:
        return False, ""

    entry_price = float(pos["entry_price"])
    if entry_price <= 0:
        return False, ""
    pnl_pct = ((current_price - entry_price) / entry_price) * 100

    ticker_cache = shared_market_cache.get(ticker)
    if not ticker_cache:
        return False, ""
    df = ticker_cache.get("df_1m")
    if df is None or len(df) < 12:
        return False, ""

    vol_ratio_now = get_vol_ratio(df, 2, 8)
    invalid_break_level = safe_float(pos.get("invalid_break_level", 0))
    strategy = pos.get("strategy", "")

    if strategy in ["EARLY", "PRE_BREAKOUT"] and pnl_pct < POST_ENTRY_MIN_PROGRESS_PCT:
        if vol_ratio_now < POST_ENTRY_MIN_VOL_RATIO:
            return True, "초반 기대보다 거래량이 빨리 약해졌어"
        if pnl_pct <= POST_ENTRY_FAIL_BELOW_ENTRY_PCT:
            return True, "진입 후 바로 힘이 약해서 초반 시나리오가 깨졌어"

    if strategy == "BREAKOUT" and invalid_break_level > 0:
        if current_price < invalid_break_level and pnl_pct < POST_ENTRY_MIN_PROGRESS_PCT:
            return True, "돌파 후 기준선 위에 못 안착했어"

    if strategy == "TREND_CONT":
        if vol_ratio_now < POST_ENTRY_MIN_VOL_RATIO and pnl_pct < POST_ENTRY_MIN_PROGRESS_PCT:
            return True, "추세 지속 기대였는데 반등 거래량이 다시 약해졌어"
        if pnl_pct <= POST_ENTRY_FAIL_BELOW_ENTRY_PCT:
            return True, "추세 지속 자리였는데 진입 후 바로 힘이 약했어"

    return False, ""

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

    if strategy == "CHASE" and pnl_pct <= -0.35:
        return True, "추격 진입 후 바로 탄력이 약했어"

    if strategy == "PRE_BREAKOUT" and pnl_pct <= -0.28 and vol_ratio_now < 1.0:
        return True, "직전형 기대였는데 눌린 뒤 힘이 약했어"

    if strategy == "TREND_CONT" and pnl_pct <= -0.32 and vol_ratio_now < 1.0:
        return True, "추세 지속 기대였는데 눌림 뒤 재가속이 약했어"

    return False, ""


def build_time_stop_comment(pnl_pct: float) -> str:
    if pnl_pct >= 0.2:
        return "짧게 수익이 났지만 크게 뻗는 힘은 약해서 정리했어."
    if pnl_pct > -0.15:
        return "빠르게 안 뻗어서 거의 본전 근처에서 정리했어."
    return "힘이 약해서 정리했어."


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

            changed_stats = update_position_run_stats(pos, pnl_pct)
            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price
                changed_stats = True
            if changed_stats:
                save_positions()

            stop_price = entry_price * (1 + pos["stop_loss_pct"] / 100)
            tp_price = entry_price * (1 + pos["take_profit_pct"] / 100)
            held_sec = int(time.time() - int(pos["entered_at"]))

            if market_value < MIN_ORDER_KRW:
                continue

            early_fail, early_fail_reason = post_entry_strength_fail(ticker, pos, current_price)
            if early_fail:
                ok, msg = sell_market_confirmed(ticker, "EARLY_FAIL", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"\n⚠️ 진입 직후 힘이 약해서 빠른 정리\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📈 수익률: {fmt_pct(pnl_pct)}\n⏰ 보유시간: {int(held_sec/60)}분\n\n사유:\n{early_fail_reason}\n")
                else:
                    send(f"\n⚠️ 빠른 정리 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                continue

            fail_exit, fail_reason = should_scenario_fail_exit(ticker, pos, current_price)
            if fail_exit:
                ok, msg = sell_market_confirmed(ticker, "SCENARIO_FAIL", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"\n⚠️ 예상한 흐름이 깨져서 빠른 정리\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📈 수익률: {fmt_pct(pnl_pct)}\n⏰ 보유시간: {int(held_sec/60)}분\n\n사유:\n{fail_reason}\n")
                else:
                    send(f"\n⚠️ 빠른 정리 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                continue

            if held_sec >= pos["time_stop_sec"] and pnl_pct < 0.8:
                ok, msg = sell_market_confirmed(ticker, "TIME_STOP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"\n⏱ 오래 안 가서 정리\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📈 수익률: {fmt_pct(pnl_pct)}\n⏰ 보유시간: {int(held_sec/60)}분\n\n{build_time_stop_comment(pnl_pct)}\n")
                else:
                    send(f"\n⚠️ 정리 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                continue

            if current_price <= stop_price:
                ok, msg = sell_market_confirmed(ticker, "STOP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"\n🚨 손절\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📉 손실률: {fmt_pct(pnl_pct)}\n")
                else:
                    send(f"\n⚠️ 손절 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                continue

            be_trigger_pct = float(pos.get("breakeven_trigger_pct", BREAKEVEN_TRIGGER_PCT))
            be_buffer_pct = float(pos.get("breakeven_buffer_pct", BREAKEVEN_BUFFER_PCT))
            trail_start_pct = float(pos.get("trail_start_pct", TRAIL_START_PCT))
            trail_backoff_pct = float(pos.get("trail_backoff_pct", TRAIL_BACKOFF_PCT))

            if pnl_pct >= be_trigger_pct and not pos.get("breakeven_armed", False):
                pos["breakeven_armed"] = True
                save_positions()

            if pos.get("breakeven_armed", False):
                be_price = entry_price * (1 + be_buffer_pct / 100)
                if held_sec >= 240 and current_price <= be_price:
                    ok, msg = sell_market_confirmed(ticker, "BREAKEVEN", pos, current_price, pnl_pct, held_sec)
                    if ok:
                        send(f"\n🛡️ 손실 없이 정리\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📈 결과: {fmt_pct(pnl_pct)}\n")
                    else:
                        send(f"\n⚠️ 본절 정리 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                    continue

            if pnl_pct >= trail_start_pct and not pos.get("trailing_armed", False):
                pos["trailing_armed"] = True
                save_positions()

            if pos.get("trailing_armed", False):
                trail_stop = pos["peak_price"] * (1 - trail_backoff_pct / 100)
                if current_price <= trail_stop:
                    ok, msg = sell_market_confirmed(ticker, "TRAIL_TP", pos, current_price, pnl_pct, held_sec)
                    if ok:
                        send(f"\n📈 오른 뒤 조금 내려와서 익절\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📈 수익률: {fmt_pct(pnl_pct)}\n\n다시 꺾이기 전에 정리했어.\n")
                    else:
                        send(f"\n⚠️ 익절 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                    continue

            if current_price >= tp_price:
                ok, msg = sell_market_confirmed(ticker, "TP", pos, current_price, pnl_pct, held_sec)
                if ok:
                    send(f"\n🎉 익절 완료\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n💰 현재가: {fmt_price(current_price)}\n📈 수익률: {fmt_pct(pnl_pct)}\n")
                else:
                    send(f"\n⚠️ 익절 시도했지만 확인 지연\n\n📊 {ticker}\n방식: {pos['strategy_label']}\n\n사유:\n{msg}\n")
                continue
        except Exception as e:
            print(f"[포지션 감시 오류] {ticker} / {e}")
            traceback.print_exc()


CLOSE_TYPES = {"TP", "STOP", "TRAIL_TP", "BREAKEVEN", "TIME_STOP", "SCENARIO_FAIL", "EARLY_FAIL"}


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


def get_closed_logs_since(ts_cutoff: int):
    return [x for x in trade_logs if x.get("type") in CLOSE_TYPES and int(x.get("time", 0)) >= int(ts_cutoff)]


def get_recent_closed_logs(limit=10):
    closes = get_closed_logs_all()
    return closes[-limit:] if closes else []


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

def close_type_label(x):
    tp = x.get("type", "UNKNOWN")
    mapping = {
        "TP": "익절",
        "TRAIL_TP": "트레일 익절",
        "BREAKEVEN": "본절",
        "STOP": "손절",
        "TIME_STOP": "시간정리",
        "SCENARIO_FAIL": "시나리오 실패",
        "EARLY_FAIL": "초반 실패",
    }
    return mapping.get(tp, tp)


def format_close_log_line(x):
    ticker = x.get("ticker", "?")
    type_label = close_type_label(x)
    pnl = float(x.get("pnl_pct", 0))
    return f"- {ticker} / {type_label} / {pnl:+.2f}%"


def format_summary_block(title: str, logs):
    info = summarize_logs(logs)
    if not info:
        return [f"[{title}]", "- 아직 종료 거래 없음"]
    lines = [
        f"[{title}]",
        f"- 종료 {info['total']}건",
        f"- 익절/플러스 {info['wins']}건 / 손절/마이너스 {info['losses']}건",
        f"- 승률 {info['win_rate']:.2f}%",
        f"- 누적 {info['total_pnl']:.2f}% / 평균 {info['avg_pnl']:.2f}%",
    ]
    return lines



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


def summary_command(update, context: CallbackContext):
    closes_all = get_closed_logs_all()
    if not closes_all:
        send("📊 아직 종료된 거래가 없어")
        return

    closes_today = get_closed_logs_today()
    closes_recent = get_recent_closed_logs(10)
    closes_since_boot = get_closed_logs_since(BOT_START_TS)

    lines = ["📊 거래 요약"]
    lines += [""] + format_summary_block("전체 누적", closes_all)
    lines += [""] + format_summary_block("오늘 요약", closes_today)
    lines += [""] + format_summary_block("최근 10건", closes_recent)
    lines += [""] + format_summary_block("이번 실행 후", closes_since_boot)

    recent_lines = [format_close_log_line(x) for x in closes_all[-5:]]
    if recent_lines:
        lines += ["", "[최근 종료 5건]"] + recent_lines

    send("\n".join(lines))


def today_command(update, context: CallbackContext):
    today_closes = get_closed_logs_today()
    info = summarize_logs(today_closes)
    if not info:
        send("📊 오늘 종료된 거래가 아직 없어")
        return
    lines = [f"• {x.get('ticker','?')} / {x.get('type')} / {float(x.get('pnl_pct',0)):.2f}%" for x in today_closes[-10:]]
    send(f"\n📊 오늘 거래 요약\n\n총 종료 거래: {info['total']}\n익절/플러스 종료: {info['wins']}\n손절/마이너스 종료: {info['losses']}\n승률: {info['win_rate']:.2f}%\n누적 수익률: {info['total_pnl']:.2f}%\n평균 수익률: {info['avg_pnl']:.2f}%\n\n최근 종료 거래\n" + "\n".join(lines))



def summarize_exit_reasons(logs):
    bucket = {}
    for x in logs:
        bucket[x.get("type", "UNKNOWN")] = bucket.get(x.get("type", "UNKNOWN"), 0) + 1
    lines = []
    for k, v in sorted(bucket.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"{k}: {v}")
    return lines


def summarize_by_regime(logs):
    bucket = {}
    for x in logs:
        regime = x.get("entry_regime", "UNKNOWN")
        bucket.setdefault(regime, []).append(x)
    lines = []
    for regime, items in sorted(bucket.items(), key=lambda kv: len(kv[1]), reverse=True):
        info = summarize_logs(items)
        lines.append(f"{regime} / 거래 {info['total']} / 승률 {info['win_rate']:.2f}% / 평균 {info['avg_pnl']:.2f}%")
    return lines


def summarize_by_pattern(logs):
    bucket = {}
    for x in logs:
        tags = x.get("pattern_tags", [])
        if not tags:
            bucket.setdefault("패턴없음", []).append(x)
            continue
        for tag in tags[:3]:
            bucket.setdefault(tag, []).append(x)
    lines = []
    for tag, items in sorted(bucket.items(), key=lambda kv: len(kv[1]), reverse=True)[:8]:
        info = summarize_logs(items)
        lines.append(f"{tag} / 거래 {info['total']} / 승률 {info['win_rate']:.2f}% / 평균 {info['avg_pnl']:.2f}%")
    return lines

def summary_strategy_command(update, context: CallbackContext):
    closes = get_closed_logs_all()
    if not closes:
        send("📊 아직 종료된 거래가 없어")
        return

    parts = ["📊 전략별 전체 결과\n\n" + "\n\n".join(summarize_by_strategy(closes))]

    exit_lines = summarize_exit_reasons(closes)
    if exit_lines:
        parts.append("🧾 종료 이유\n\n" + "\n".join(exit_lines))

    regime_lines = summarize_by_regime(closes)
    if regime_lines:
        parts.append("🌤 장세별 결과\n\n" + "\n".join(regime_lines))

    pattern_lines = summarize_by_pattern(closes)
    if pattern_lines:
        parts.append("📐 패턴별 결과\n\n" + "\n".join(pattern_lines))

    append_debug_shortlist_parts(parts)
    send("\n\n".join(parts))


def today_strategy_command(update, context: CallbackContext):
    closes = get_closed_logs_today()
    if not closes:
        send("📊 오늘 종료된 거래가 아직 없어")
        return
    send("📊 전략별 오늘 결과\n\n" + "\n\n".join(summarize_by_strategy(closes)))


def btc_command(update, context: CallbackContext):
    send(analyze_btc_flow())



def set_auto_buy_value(enabled: bool):
    global AUTO_BUY
    AUTO_BUY = bool(enabled)
    try:
        save_config()
    except Exception:
        pass
    try:
        save_settings()
    except Exception:
        pass

def set_auto_buy(enabled: bool):
    return set_auto_buy_value(enabled)


def autobuy_on_command(update, context: CallbackContext):
    set_auto_buy_value(True)
    send("🟢 자동매수 켜짐")


def autobuy_off_command(update, context: CallbackContext):
    set_auto_buy_value(False)
    send("⛔ 자동매수 꺼짐")


def price_check_callback(update, context: CallbackContext):
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if not data.startswith("pc:"):
        return
    parts = data.split(":")
    ref_id = parts[1].strip() if len(parts) >= 2 else ""
    check_mode = parts[2].strip() if len(parts) >= 3 else "now"
    ref = PRICE_CHECK_REFS.get(ref_id)
    if not ref:
        try:
            query.answer("이전 알림 정보가 없어서 비교를 못 했어.", show_alert=True)
        except Exception:
            pass
        return

    ticker = ref.get("ticker", "?")
    alert_ts = safe_float(ref.get("alert_ts", 0))
    now_ts = time.time()
    elapsed_sec = max(0.0, now_ts - alert_ts)

    target_min_map = {"now": 0, "5": 5, "15": 15, "30": 30}
    target_min = target_min_map.get(check_mode, 0)

    if target_min <= 0:
        ok = send_price_check_result(query.message.chat_id, ref, target_min=0, now_ts=now_ts)
        try:
            query.answer("현재가 비교 확인했어" if ok else "현재가를 못 불러왔어", show_alert=not ok)
        except Exception:
            pass
        return

    target_ts = alert_ts + target_min * 60
    sched_key = f"{ref_id}:{target_min}:{query.message.chat_id}"

    if elapsed_sec >= target_min * 60:
        ok = send_price_check_result(query.message.chat_id, ref, target_min=target_min, now_ts=now_ts)
        try:
            query.answer(f"{target_min}분 시점이 이미 지나서 지금 바로 보여줄게" if ok else "현재가를 못 불러왔어", show_alert=not ok)
        except Exception:
            pass
        return

    if sched_key in PRICE_CHECK_SCHEDULED:
        try:
            query.answer(f"{target_min}분 뒤 자동 확인은 이미 예약돼 있어", show_alert=False)
        except Exception:
            pass
        return

    PRICE_CHECK_SCHEDULED[sched_key] = {
        "ref_id": ref_id,
        "chat_id": query.message.chat_id,
        "target_min": target_min,
        "target_ts": target_ts,
        "created_at": now_ts,
    }
    try:
        query.answer(f"{target_min}분 뒤 자동 확인 예약했어")
    except Exception:
        pass

    lines = [
        f"⏰ {ticker} {target_min}분 뒤 자동 확인 예약",
        "",
        f"- 알림 시각: {format_hms(alert_ts)}",
        f"- 예정 시각: {format_hms(target_ts)}",
        f"- 남은 시간: {format_elapsed_text(max(0.0, target_ts - now_ts))}",
    ]
    try:
        bot.send_message(chat_id=query.message.chat_id, text="\n".join(lines))
    except Exception as e:
        print(f"[텔레그램 예약 메시지 오류] {e}")


def register_bot_commands():
    try:
        bot.set_my_commands([
            BotCommand("status", "현재 상태 보기"),
            BotCommand("summary", "전체 요약 보기"),
            BotCommand("today", "오늘 거래 보기"),
            BotCommand("summary_strategy", "전략별 요약"),
            BotCommand("today_strategy", "오늘 전략별 보기"),
            BotCommand("btc", "BTC 상태 보기"),
            BotCommand("autobuy_on", "자동매수 켜기"),
            BotCommand("autobuy_off", "자동매수 끄기"),
            BotCommand("reset_pause", "자동 쉬기 해제"),
            BotCommand("scan_debug", "후보 상세보기"),
            BotCommand("info", "용어/전략 설명 보기"),
        ])
    except Exception as e:
        print(f"[명령어 등록 오류] {e}")


def reset_pause_command(update, context: CallbackContext):
    reset_auto_pause_state(bypass_sec=900)
    send(
        f"""
🛠 자동 쉬기 수동 해제

자동매수 쉬기 상태를 풀었어.
연속 손실 기록도 지금 시점 기준으로 초기화했어.

앞으로 15분 동안은 새 버전 테스트용으로
이전 손실 기록 때문에 바로 다시 쉬지 않게 해둘게.

15분 뒤부터는 새로 쌓이는 기록 기준으로만
원래 쉬기 로직이 다시 작동해.
"""
    )






def info_command(update, context: CallbackContext):
    send(build_info_text())



def get_recent_watch_snapshot_items(limit=8):
    try:
        now_ts = time.time()
        items = []
        for ticker, snap in recent_watch_snapshots.items():
            if not isinstance(snap, dict):
                continue
            saved_at = safe_float(snap.get("saved_at", now_ts))
            age = int(now_ts - saved_at)
            if age > 1800:
                continue
            items.append({
                "ticker": ticker,
                "price": safe_float(snap.get("price", 0)),
                "change_5": safe_float(snap.get("change_5", snap.get("change_pct", 0))),
                "vol_ratio": safe_float(snap.get("vol_ratio", 0)),
                "leader_score": safe_float(snap.get("leader_score", 0)),
                "turnover": safe_float(snap.get("turnover", 0)),
                "lite_score": safe_float(snap.get("edge_score", snap.get("signal_score", 0))) + safe_float(snap.get("leader_score", 0)) * 1.2,
                "rsi": safe_float(snap.get("rsi", 50)),
                "reasons": ["방금 잡힌 후보 기준"],
                "saved_at": saved_at,
            })
        items.sort(key=lambda x: (safe_float(x.get("lite_score", 0)), safe_float(x.get("saved_at", 0))), reverse=True)
        return items[:limit]
    except Exception:
        return []

def get_scan_debug_candidates():
    try:
        now_ts = time.time()
        cache_age = int(now_ts - shared_market_cache_time) if shared_market_cache_time else 999999

        # scan_debug는 현재 캐시/스냅샷만 빠르게 보여주고, 여기서 강제 스캔은 하지 않음
        # 강제 rebuild를 하면 모바일에서 응답이 묶여 보일 수 있어서 피함

        # 1순위: 최근 shared cache 직접 사용 (가장 빠름)
        if shared_market_cache and cache_age <= 180:
            items = []
            for ticker, data in shared_market_cache.items():
                try:
                    price = safe_float(data.get("price", 0))
                    if price <= 0:
                        continue

                    change_1 = safe_float(data.get("change_1", 0))
                    change_3 = safe_float(data.get("change_3", 0))
                    change_5 = safe_float(data.get("change_5", 0))
                    vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0)))
                    leader = safe_float(data.get("leader_score", 0))
                    turnover = safe_float(data.get("turnover", 0))
                    rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50)))

                    lite_score = (
                        max(change_5, 0) * 2.4
                        + max(change_3, 0) * 1.2
                        + max(change_1, 0) * 0.6
                        + max(vol_ratio - 1.0, 0) * 4.0
                        + min(turnover / 1000000000.0, 8.0)
                        + leader * 1.6
                    )
                    if rsi >= 82:
                        lite_score -= 1.5

                    reasons = []
                    if change_5 < 0.20:
                        reasons.append("5분 상승이 아직 약함")
                    if vol_ratio < 1.00:
                        reasons.append("거래량이 아직 약함")
                    if leader < 0.50:
                        reasons.append("시장주목도가 아직 약함")
                    if rsi >= 82:
                        reasons.append("너무 빠르게 오른 편")
                    if not reasons:
                        reasons.append("후보로 볼 수 있음")

                    items.append({
                        "ticker": ticker,
                        "price": price,
                        "change_5": change_5,
                        "vol_ratio": vol_ratio,
                        "leader_score": leader,
                        "turnover": turnover,
                        "lite_score": lite_score,
                        "rsi": rsi,
                        "reasons": reasons,
                    })
                except Exception:
                    continue

            items.sort(
                key=lambda x: (
                    safe_float(x.get("lite_score", 0)),
                    safe_float(x.get("turnover", 0)),
                    safe_float(x.get("change_5", 0)),
                ),
                reverse=True,
            )
            note = f"최근 {cache_age}초 안에 모은 시장 데이터 기준"
            if cache_age > 180:
                note += " (조금 지난 데이터)"
            return items[:8], note

        # 2순위: 최근 후보 스냅샷
        recent_items = get_recent_watch_snapshot_items(limit=8)
        if recent_items:
            return recent_items, "방금 잡힌 후보 기준"

        return [], f"최근 데이터가 아직 없음 ({cache_age}초)"
    except Exception as e:
        return None, f"후보 상세보기 조회 에러: {e}"

def build_scan_debug_text():
    results, note = get_scan_debug_candidates()
    if results is None:
        return f"⚠️ 후보 상세보기 에러\n{note}"

    lines = ["🔎 후보 확인용 상세보기"]
    if note:
        lines.append(f"안내: {note}")

    if not results:
        lines.append("")
        lines.append("후보 없음")
        return "\n".join(lines)

    for i, item in enumerate(results, 1):
        price_text = f"{safe_float(item.get('price', 0)):,.4f}".rstrip("0").rstrip(".")
        lines.append("")
        lines.append(f"{i}. {item.get('ticker','?')} / 현재가 {price_text}")
        lines.append(
            f"   5분 상승 {safe_float(item.get('change_5',0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 시장주목도 {safe_float(item.get('leader_score',0)):.2f}"
        )
        lines.append(f"   후보 점수 {safe_float(item.get('lite_score',0)):.2f}")
        lines.append(f"   잡힌 이유: {' / '.join(item.get('reasons', [])[:3])}")
    return "\n".join(lines)

def scan_debug_command(update, context: CallbackContext):
    try:
        send("🔎 후보 상세보기 불러오는 중...")
        send(build_scan_debug_text())
    except Exception as e:
        send(f"⚠️ 후보 상세보기 에러: {e}")

def append_debug_shortlist_parts(parts: list):
    try:
        results, note = get_scan_debug_candidates()
        if results is None:
            parts.append(f"⚠️ 상위 후보 확인 에러: {note}")
            return

        header = "🔎 상위 스캔 후보"
        if note:
            header += f" ({note})"

        if not results:
            parts.append(header + "\n\n후보 없음")
            return

        lines = [header]
        for item in results[:3]:
            lines.append(
                f"• {item.get('ticker','?')} / 5분 {safe_float(item.get('change_5',0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 시장주목도 {safe_float(item.get('leader_score',0)):.2f}"
            )
        parts.append("\n".join(lines))
    except Exception as e:
        parts.append(f"⚠️ 상위 스캔 후보 표시 에러: {e}")

def status_command(update, context: CallbackContext):



    parts = []

    if AUTO_BUY:
        parts.append("🟢 자동매수 : 켜짐")
    else:
        parts.append("🔴 자동매수 : 꺼짐")

    paused, pause_msg = should_pause_auto_buy_now()
    if not AUTO_BUY and paused:
        parts.append("")
        parts.append(f"(참고) 자동 쉬기 상태: {pause_msg}")
    elif AUTO_BUY and paused:
        parts.append("")
        parts.append("⏸ 자동매수 쉬는 중")
        parts.append(pause_msg)
    elif auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time())
        parts.append("")
        parts.append("🛠 자동 쉬기 수동 해제 적용 중")
        parts.append(f"{max(remain, 0)}초 남음")

    if active_positions:
        lines, dust_lines = [], []
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
                f"/ 수익률 {fmt_pct(pnl)} / 평가금액 {fmt_price(value)} / 보유 {held_min}분 / 기대점수 {pos.get('edge_score', 0):.2f} / 등급 {pos.get('trade_tier','B')} / 주도주 {safe_float(pos.get('leader_score',0)):.2f}{tag_text}"
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
        sorted_candidates = sorted(pending_buy_candidates.values(), key=lambda x: (safe_float(x.get("leader_score", 0)) * 0.8) + safe_float(x.get("edge_score", 0)), reverse=True)[:6]
        for item in sorted_candidates:
            age = int(time.time() - safe_float(item.get("created_at", 0)))
            c_lines.append(f"• {item['ticker']} / 방식 {item.get('strategy_label','?')} / 후보대기 {age}초 / 기대점수 {safe_float(item.get('edge_score', 0)):.2f} / 주도주 {safe_float(item.get('leader_score', 0)):.2f}")
        parts.append("🕒 2차 확인 후보\n\n" + "\n".join(c_lines))

    if pending_sells:
        p = ["⚠️ 매도 확인 대기중"]
        for ticker in pending_sells.keys():
            p.append(f"• {ticker}")
        parts.append("\n".join(p))

    send("\n\n".join(parts))


register_bot_commands()

updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
dispatcher = updater.dispatcher
dispatcher.add_handler(CommandHandler("summary", summary_command))
dispatcher.add_handler(CommandHandler("today", today_command))
dispatcher.add_handler(CommandHandler("summary_strategy", summary_strategy_command))
dispatcher.add_handler(CommandHandler("today_strategy", today_strategy_command))
dispatcher.add_handler(CommandHandler("btc", btc_command))
dispatcher.add_handler(CommandHandler("autobuy_on", autobuy_on_command))
dispatcher.add_handler(CommandHandler("autobuy_off", autobuy_off_command))
dispatcher.add_handler(CommandHandler("reset_pause", reset_pause_command))
dispatcher.add_handler(CommandHandler("status", status_command))
dispatcher.add_handler(CommandHandler("scan_debug", scan_debug_command))
dispatcher.add_handler(CommandHandler("info", info_command))
dispatcher.add_handler(CallbackQueryHandler(price_check_callback, pattern=r"^pc:"))
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
        try:
            update_scan_debug_snapshot(shared_cache)
        except Exception:
            pass
        try:
            scan_watchlist(shared_cache=shared_cache)
        except Exception as e:
            print(f"[loop] scan_watchlist error: {e}", flush=True)
        process_pending_buy_promotions(shared_cache=shared_cache)
        scan_and_auto_trade(shared_cache=shared_cache)

        check_pending_sells()
        cleanup_pending_buy_candidates()
        monitor_positions()
        process_scheduled_price_checks(now_ts)

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

    except Exception as e:
        print(f"[메인 루프 오류] {e}")
        traceback.print_exc()

    time.sleep(LOOP_SLEEP)

