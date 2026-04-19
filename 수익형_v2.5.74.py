# 재생성본: 대화 중 전달되었지만 만료/미보존된 v2.5.55 링크를 대체하기 위해 다시 저장한 파일입니다.
import os
import time
import json
import math
import traceback
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
import pybithumb
from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackContext, CallbackQueryHandler
# =========================================================
# 버전
# =========================================================
BOT_VERSION = "수익형 v2.5.74"
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
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
def data_file(name: str) -> str:
    return os.path.join(BASE_DIR, name)
# =========================================================
# 파일 / 시간
# =========================================================
TIMEZONE = "Asia/Seoul"
LOG_FILE = data_file("trade_log.json")
POSITIONS_FILE = data_file("active_positions.json")
PENDING_SELLS_FILE = data_file("pending_sells.json")
PENDING_BUYS_FILE = data_file("pending_buy_candidates.json")
RUNTIME_SETTINGS_FILE = data_file("runtime_settings.json")
RECENT_CANDIDATES_FILE = data_file("recent_candidate_alerts.json")
LEGACY_RECENT_CANDIDATES_FILE = "recent_candidate_alerts.json"
PRICE_CHECK_REFS_FILE = data_file("price_check_refs.json")
PRICE_CHECK_SCHEDULED_FILE = data_file("price_check_scheduled.json")
BOT_START_TS = int(time.time())
PRICE_CHECK_REFS = {}
PRICE_CHECK_SCHEDULED = {}
PRICE_CHECK_REF_TTL_SEC = 6 * 3600
PRICE_CHECK_REF_MAX = 300
PRICE_TRACK_UPDATE_SEC = 3
PRICE_TRACK_WINDOW_SEC = 35 * 60
RECENT_CANDIDATE_ALERTS = []
RECENT_CANDIDATE_MAX = 20
RECENT_CANDIDATE_TTL_SEC = 6 * 3600
MISSED_REVIEW_QUEUE_FILE = data_file("missed_review_queue.json")
MISSED_REVIEW_LOG_FILE = data_file("missed_review_logs.json")
MISSED_REVIEW_META_FILE = data_file("missed_review_meta.json")
MISSED_REVIEW_ON = True
MISSED_REVIEW_DELAY_SEC = 35 * 60
MISSED_REVIEW_REGISTER_COOLDOWN_SEC = 20 * 60
MISSED_REVIEW_MAX_QUEUE = 240
MISSED_REVIEW_MAX_LOGS = 200
MISSED_REVIEW_MARKET_LIMIT = 36
MISSED_REVIEW_SIGNAL_LIMIT = 22
MISSED_REVIEW_MIN_BEST_UP_PCT = 1.90
MISSED_REVIEW_MIN_END_UP_PCT = 0.80
MISSED_REVIEW_MIN_FAST_UP_PCT = 2.50
MISSED_REVIEW_MIN_BASE_HIGH_GAP_PCT = 0.85
MISSED_REVIEW_NEAR_HIGH_BLOCK_PCT = 0.35
MISSED_REVIEW_MAX_BASE_FROM_LOW_PCT = 2.70
MISSED_REVIEW_MAX_BASE_CHART_RISE_PCT = 4.10
MISSED_REVIEW_MIN_BASE_VOL_RATIO = 1.20
MISSED_REVIEW_MIN_BASE_LEADER_SCORE = 0.25
MISSED_REVIEW_AUTO_ALERT_ON = False
MISSED_REVIEW_AUTO_ALERT_MIN_PCT = 2.60
MISSED_REVIEW_AUTO_ALERT_COOLDOWN_SEC = 600
MISSED_REVIEW_HOURLY_ALERT_ON = True
MISSED_REVIEW_HOURLY_MAX_ITEMS = 5
USER_ALERT_FILTER_ON = True
USER_ALERT_ALLOW_SECTIONS = {"early", "live", "restart"}
USER_ALERT_HIDE_SECTIONS = {"late", "watch"}
USER_ALERT_MIN_RESTART_EDGE = 5.25
USER_ALERT_MIN_RESTART_VOL = 1.45
USER_ALERT_MIN_RESTART_LEADER = 0.90
USER_ALERT_MIN_RESTART_HIGH_GAP_PCT = 0.85
USER_ALERT_MAX_RESTART_FROM_LOW_PCT = 2.15
USER_ALERT_MAX_RESTART_CHART_RISE_PCT = 3.20
USER_ALERT_PREWATCH_STRICT_ON = True
USER_ALERT_MIN_LIVE_EDGE = 5.95
USER_ALERT_MIN_LIVE_VOL = 4.50
USER_ALERT_MIN_LIVE_LEADER = 1.40
USER_ALERT_MIN_LIVE_HIGH_GAP_PCT = 0.95
USER_ALERT_MAX_LIVE_FROM_LOW_PCT = 2.05
USER_ALERT_MAX_LIVE_CHART_RISE_PCT = 3.25
USER_ALERT_LIVE_MIN_30M_PCT = 0.05
USER_ALERT_LIVE_MIN_5M_PCT = 0.70
USER_ALERT_LIVE_B_FAIL_COUNT_MIN = 2
USER_ALERT_ALLOW_HIDDEN_PROMOTION = True
USER_ALERT_PROMOTE_MIN_EDGE = 5.55
USER_ALERT_PROMOTE_MIN_VOL = 1.85
USER_ALERT_PROMOTE_MIN_LEADER = 0.70
USER_ALERT_PROMOTE_MIN_5M_PCT = 0.22
USER_ALERT_PROMOTE_MIN_30M_PCT = 0.05
USER_ALERT_PROMOTE_MIN_HIGH_GAP_PCT = 0.80
USER_ALERT_PROMOTE_MAX_FROM_LOW_PCT = 2.65
USER_ALERT_PROMOTE_MAX_CHART_RISE_PCT = 3.85
USER_ALERT_RESCUE_ON = True
USER_ALERT_RESCUE_SHORTLIST_RANK_MAX = 10
USER_ALERT_RESCUE_MIN_EDGE = 4.85
USER_ALERT_RESCUE_MIN_VOL = 1.15
USER_ALERT_RESCUE_MIN_LEADER = 0.55
USER_ALERT_RESCUE_MIN_HIGH_GAP_PCT = 0.45
USER_ALERT_RESCUE_MAX_FROM_LOW_PCT = 3.10
USER_ALERT_RESCUE_MIN_LIVE_5M_PCT = 0.12
USER_ALERT_RESCUE_MIN_RESTART_MOTION_PCT = 0.05
USER_ALERT_RESCUE_GOOD_MARKET_DAY_PCT = 0.60
USER_ALERT_RESCUE_GOOD_MARKET_MID_PCT = 0.10
USER_ALERT_LIVE_B_HARD_BLOCK_NEAR_HIGH_PCT = 0.95
USER_ALERT_LIVE_B_HARD_BLOCK_FROM_LOW_PCT = 1.85
USER_ALERT_LIVE_NEG30_BLOCK_FROM_LOW_PCT = 2.25
USER_ALERT_RESCUE_MIN_HOT_SCORE = 1
USER_ALERT_RESCUE_STRONG_HOT_SCORE = 4
USER_ALERT_RESCUE_MIN_SHORTLIST_RANK_STRONG = 4
MISSED_REVIEW_MIN_TRUE_END_UP_PCT = 0.90
MISSED_REVIEW_MAX_FALSE_SPIKE_FADE_PCT = 2.20
MISSED_REVIEW_MAX_FALSE_SPIKE_END_DOWN_PCT = 0.20
MISSED_REVIEW_MIN_KEEP_AFTER_SPIKE_END_PCT = 1.20
MARKET_PULSE_STATE_FILE = data_file("market_pulse_state.json")
MARKET_PULSE_TOP_N = 20
MARKET_PULSE_RANK_SEEN_CUTOFF = 20
ALERT_CHART_CONTEXT_ON = True
ALERT_CHART_CONTEXT_WINDOW_1M = 18
ALERT_PULLBACK_LIVE_BLOCK_PCT = 1.35
ALERT_PULLBACK_RESTART_BLOCK_PCT = 1.85
ALERT_WOBBLE_UPPER_WICK_PCT = 0.70
ALERT_HTF_INTERVAL = "minute240"
ALERT_HTF_LOOKBACK = 36
ALERT_HTF_STRONG_NEAR_HIGH_PCT = 1.40
SCAN_HEARTBEAT_WARN_SEC = 120
SCAN_HEARTBEAT_HARD_RESTART_SEC = 1500
SCAN_CACHE_WARN_SEC = 90
SCAN_CACHE_HARD_RESET_SEC = 420
SCAN_WATCHDOG_CHECK_SEC = 15
SCAN_WATCHDOG_STARTUP_GRACE_SEC = 600
SCAN_WATCHDOG_RESET_COOLDOWN_SEC = 180
TICKER_LIST_REFRESH_SEC = 300
UNIVERSE_REFRESH_BATCH = 8
UNIVERSE_FORCE_FULL_BATCH = 14
UNIVERSE_MIN_PARTIAL_ROWS = 8
UNIVERSE_BUILD_MAX_SEC = 3
CACHE_REFRESH_BATCH = 6
CACHE_FORCE_FULL_BATCH = 10
CACHE_MIN_PARTIAL_ROWS = 6
CACHE_BUILD_MAX_SEC = 3
CACHE_ROW_FRESH_KEEP_SEC = 900
# =========================================================
# 실행 주기
# =========================================================
SCAN_INTERVAL = 6
POSITION_CHECK_INTERVAL = 4
LOOP_SLEEP = 1
POSITION_SAVE_MIN_INTERVAL_SEC = 12
# =========================================================
# 시장 캐시 / 유니버스
# =========================================================
TOP_TICKERS = 120
ABSOLUTE_TURNOVER_CANDIDATES = 60
SURGE_CANDIDATES = 80
UNIVERSE_REFRESH_SEC = 18
MARKET_CACHE_TTL_SEC = 8
MARKET_REFRESH_INTERVAL_SEC = 12
MARKET_REFRESH_EMPTY_RETRY_SEC = 4
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
BTC_DAY_BLOCK_PCT = -3.5
BTC_DAY_WEAK_PCT = -2.0
BTC_MID_WEAK_PCT = -1.20
BTC_SHORT_WEAK_PCT = -0.90
REGIME_RECOVERY_ENTRY_BLOCK_IN_WEAK = True
REGIME_RECOVERY_ENTRY_SIDEWAYS_EXTRA_STRENGTH = -1
REGIME_RECOVERY_ONLY_MIN_VOL_WEAK = 2.35
REGIME_RECOVERY_ONLY_MIN_LEADER_WEAK = 1.30
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
TRADE_TIER_A_EDGE_MIN = 6.8
TRADE_TIER_A_VOL_MIN = 1.25
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
    "change_min": 0.60,
    "change_max": 1.90,
    "vol_min": 2.35,
    "rsi_min": 46,
    "rsi_max": 61,
    "range_min": 0.80,
    "jump_min": 0.08,
    "high_gap_min": 0.35,
    "high_gap_max": 2.30,
    "rebound_max": 3.00,
}
PREPUMP_CFG = {
    "change_min": 1.00,
    "change_max": 4.40,
    "vol_min": 2.25,
    "rsi_min": 48,
    "rsi_max": 70,
    "range_min": 1.25,
}
PULLBACK_CFG = {
    "pump_min": 6.0,
    "rebound_min": 0.95,
    "vol_min": 1.6,
}
PRE_BREAKOUT_CFG = {
    "change_max": 0.95,
    "vol_min": 2.15,
    "rsi_min": 48,
    "rsi_max": 66,
    "range_min": 0.80,
    "gap_min": 0.18,
    "gap_max": 0.65,
    "jump_min": 0.08,
    "rebound_max": 3.20,
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
PENDING_BUY_MIN_EDGE = 3.8
PENDING_BUY_MIN_SCORE = 3.8
PENDING_BUY_RECHECK_MIN_SEC = 26
PROMOTE_RECOVERY_TO_HIGH_PCT = 99.7
PROMOTE_MIN_VOL_RATIO = 1.15
PROMOTE_MAX_BREAKOUT_EXTENSION_PCT = 0.45
# =========================================================
# watch 재알림 제어
# =========================================================
WATCH_RENOTICE_SEC = 3600
WATCH_VOL_IMPROVE_DELTA = 0.32
WATCH_CHANGE_IMPROVE_DELTA = 0.28
WATCH_SCORE_IMPROVE_DELTA = 0.50
WATCH_RENOTICE_MAX_PER_TICKER = 1
WATCH_STRONG_REALERT_MIN_SEC = 2400
WATCH_WEAK_REALERT_MIN_SEC = 3000
WATCH_BLOCK_IF_PENDING_30MIN_CHECK = True
WATCH_REPEAT_WINDOW_SEC = 3 * 3600
WATCH_REPEAT_MAX_PER_TICKER = 1
WATCH_REPEAT_REQUIRE_STRONGER_SEC = 3600
WATCH_STRONGER_SCORE_DELTA = 0.65
WATCH_STRONGER_VOL_DELTA = 0.45
WATCH_STRONGER_LEADER_DELTA = 0.75
WATCH_STRONGER_CHANGE_DELTA = 0.55
WATCH_STRONGER_HIGH_GAP_DELTA = 0.30

# =========================================================
# 차트 위치 필터
# =========================================================
CHART_POS_LOOKBACK_1M = 30
CHART_POS_LATE_NEAR_HIGH_PCT = 0.22
CHART_POS_HIGH_ZONE_RATIO = 0.82
CHART_POS_VERY_HIGH_ZONE_RATIO = 0.92
CHART_POS_EXTENDED_NEAR_HIGH_PCT = 1.20
CHART_POS_LIVE_MAX_RISE_PCT = 3.40
CHART_POS_RESTART_MAX_RISE_PCT = 2.90
ALERT_PRICE_RECHECK_ON = True
ALERT_PRICE_RECHECK_ENTRY_MAX_UP_PCT = 0.50
ALERT_PRICE_RECHECK_LIVE_MAX_UP_PCT = 0.30
ALERT_PRICE_RECHECK_RESTART_MAX_UP_PCT = 0.24
ALERT_PRICE_RECHECK_SKIP_UP_PCT = 1.10
ALERT_PRICE_RECHECK_LIVE_NEAR_HIGH_MAX_UP_PCT = 0.18
ALERT_PRICE_RECHECK_RESTART_NEAR_HIGH_MAX_UP_PCT = 0.12
WATCH_EXCLUDED_TICKERS = {"USDC", "USDT", "BUSD", "USDP", "DAI", "TUSD", "FDUSD", "USDS", "USD1", "PYUSD", "USDE", "RLUSD"}
WATCH_MIN_EFFECTIVE_30M_MOVE_PCT = 0.06
WATCH_MIN_RECOVERY_FROM_LOW_PCT = 0.05
WATCH_MIN_RANGE_PCT = 0.08
WATCH_HIGH_VOL_MIN = 3.20
WATCH_HIGH_VOL_MIN_MOVE_PCT = 0.06
RECOVERY_ENTRY_MIN_30M_PCT = 0.00
RECOVERY_ENTRY_MIN_FROM_LOW_PCT = 0.08
RECOVERY_ENTRY_MIN_VOL_RATIO = 1.15
RECOVERY_ENTRY_MIN_LEADER_SCORE = 0.30
RECOVERY_ENTRY_MIN_HIGH_GAP_PCT = 0.35
RECOVERY_ENTRY_MIN_5M_PCT = -0.25
FAST_ENTRY_MIN_5M_PCT = 0.03
FAST_ENTRY_MIN_30M_PCT = 0.10
FAST_ENTRY_MIN_FROM_LOW_PCT = 0.12
FAST_ENTRY_MIN_VOL_RATIO = 1.15
FAST_ENTRY_MIN_LEADER_SCORE = 0.25
FAST_ENTRY_MIN_HIGH_GAP_PCT = 0.30
LIVE_FORCE_ALERT_COOLDOWN_SEC = 420
LIVE_FORCE_PER_TICKER_COOLDOWN_SEC = 900
LIVE_FORCE_MAX_ENTRY_ITEMS = 3
LIVE_FORCE_MAX_RECOVERY_ITEMS = 3
LIVE_FORCE_ENTRY_MIN_5M_PCT = 0.70
LIVE_FORCE_ENTRY_MIN_VOL_RATIO = 3.00
LIVE_FORCE_ENTRY_MIN_LEADER_SCORE = 1.00
LIVE_FORCE_RECOVERY_MIN_5M_PCT = 0.08
LIVE_FORCE_RECOVERY_MIN_VOL_RATIO = 1.45
LIVE_FORCE_RECOVERY_MIN_LEADER_SCORE = 0.60
LIVE_DISPLAY_MIN_5M_PCT = 0.55
LIVE_DISPLAY_MIN_VOL_RATIO = 3.20
LIVE_DISPLAY_MIN_LEADER_SCORE = 1.15
RESTART_DISPLAY_MIN_5M_PCT = 0.40
RESTART_DISPLAY_MIN_VOL_RATIO = 3.80
RESTART_DISPLAY_MIN_LEADER_SCORE = 1.30
RESTART_DISPLAY_MIN_HIGH_GAP_PCT = 1.45
RESTART_DISPLAY_MIN_COMBINED_MOTION_PCT = 1.20
RESTART_DISPLAY_MAX_FROM_LOW_PCT = 1.35
RESTART_DISPLAY_MAX_RISE_PCT = 1.75
LIVE_DISPLAY_OVERHEAT_30M_PCT = 4.20
LIVE_DISPLAY_OVERHEAT_5M_PCT = 1.80
LIVE_DISPLAY_MIN_30M_PCT = 0.15
LIVE_DISPLAY_MIN_HIGH_GAP_PCT = 1.10
LIVE_DISPLAY_WEAK_30M_MAX_FROM_LOW_PCT = 1.70
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
recent_fast_alert_signatures = {}
recent_watch_alerts = {}
recent_watch_snapshots = {}
recent_watch_renotice_counts = {}
recent_watch_history = {}
pending_sells = {}
pending_buy_candidates = {}
recent_leader_board = {}
last_live_force_alert_time = 0
live_force_alert_history = {}
paused_until = 0
pause_reason = ""
auto_pause_bypass_until = 0
auto_pause_bypass_reason = ""
auto_pause_reset_ignore_before = 0
missed_review_queue = {}
missed_review_logs = []
missed_review_meta = {}
last_missed_review_alert_ts = 0
# =========================================================
# 공유 캐시
# =========================================================
shared_market_cache = {}
shared_market_cache_time = 0
last_scan_debug_snapshot = []
last_scan_debug_snapshot_time = 0
last_scan_debug_note = ""
last_main_loop_heartbeat_ts = 0
last_main_loop_stage = "booting"
last_main_loop_error = ""
last_shared_cache_success_ts = 0
last_watchdog_cache_reset_ts = 0
scan_runtime_reset_reason = ""
scan_runtime_reset_count = 0
SCAN_DEBUG_SNAPSHOT_MAX_AGE_SEC = 180
market_universe = []
market_universe_time = 0
market_all_tickers = []
market_all_tickers_time = 0
market_universe_cursor = 0
shared_cache_cursor = 0
shared_cache_row_times = {}
last_universe_build_stats = {}
last_cache_build_stats = {}
market_refresh_lock = threading.Lock()
market_refresh_in_progress = False
market_refresh_started_ts = 0.0
last_market_refresh_request_ts = 0.0
last_market_refresh_finish_ts = 0.0
last_market_refresh_reason = ""
last_market_refresh_result = "대기"
market_pulse_state = {}
position_state_dirty = False
last_position_save_time = 0
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
def send(msg: str, reply_markup=None, reply_to_message_id=None):
    try:
        return bot.send_message(
            chat_id=CHAT_ID,
            text=msg.strip(),
            reply_markup=reply_markup,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception as e:
        print(f"[텔레그램 오류] {e}")
        return None
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
def format_tier_badge(tier: str) -> str:
    t = str(tier or "B").upper().strip()
    return {
        "S": "🔥 S",
        "A": "⭐ A",
        "B": "👀 B",
        "C": "⛔ C",
    }.get(t, f"👀 {t}")
def get_reference_high_gap_pct(item: dict) -> float:
    current_price = safe_float(item.get("current_price", item.get("price", 0)), 0)
    reference_high = safe_float(item.get("reference_high", 0), 0)
    if current_price <= 0 or reference_high <= 0:
        return 999.0
    return ((reference_high - current_price) / reference_high) * 100.0

def get_reference_low_rise_pct(item: dict) -> float:
    current_price = safe_float(item.get("current_price", item.get("price", 0)), 0)
    reference_low = safe_float(item.get("reference_low", 0), 0)
    if current_price <= 0 or reference_low <= 0:
        return 0.0
    return ((current_price - reference_low) / reference_low) * 100.0

def compute_chart_position_profile(df, current_price: float, lookback: int = CHART_POS_LOOKBACK_1M) -> dict:
    profile = {
        "chart_low": 0.0,
        "chart_high": 0.0,
        "chart_rise_pct": 0.0,
        "chart_gap_pct": 999.0,
        "chart_zone_ratio": -1.0,
        "chart_swing_pct": 0.0,
    }
    try:
        if df is None or len(df) < 8 or current_price <= 0:
            return profile
        recent = df.tail(min(len(df), max(int(lookback), 8)))
        low_v = safe_float(recent["low"].min(), 0)
        high_v = safe_float(recent["high"].max(), 0)
        if low_v <= 0 or high_v <= low_v:
            return profile
        rise_pct = ((current_price - low_v) / low_v) * 100.0
        gap_pct = ((high_v - current_price) / current_price) * 100.0 if current_price > 0 else 999.0
        swing_pct = ((high_v - low_v) / low_v) * 100.0
        zone_ratio = (current_price - low_v) / (high_v - low_v) if high_v > low_v else -1.0
        profile.update({
            "chart_low": low_v,
            "chart_high": high_v,
            "chart_rise_pct": round(rise_pct, 2),
            "chart_gap_pct": round(gap_pct, 2),
            "chart_zone_ratio": round(zone_ratio, 4),
            "chart_swing_pct": round(swing_pct, 2),
        })
    except Exception:
        return profile
    return profile

def apply_chart_position_profile(item: dict, df, lookback: int = CHART_POS_LOOKBACK_1M) -> dict:
    if not isinstance(item, dict):
        return item
    profile = compute_chart_position_profile(df, safe_float(item.get("current_price", item.get("price", 0)), 0), lookback=lookback)
    item.update(profile)
    return item

def get_item_chart_profile(item: dict) -> dict:
    snap = ensure_pre_alert_snapshot(item)
    chart_rise = safe_float(item.get("chart_rise_pct", 0), 0)
    chart_gap = safe_float(item.get("chart_gap_pct", 999), 999)
    chart_zone = safe_float(item.get("chart_zone_ratio", -1), -1)
    chart_swing = safe_float(item.get("chart_swing_pct", 0), 0)
    if chart_rise <= 0:
        chart_rise = max(get_reference_low_rise_pct(item), safe_float(snap.get("from_30m_low_pct", 0), 0))
    if chart_gap >= 900:
        chart_gap = min(get_reference_high_gap_pct(item), safe_float(snap.get("below_30m_high_pct", 999), 999))
    return {
        "chart_rise_pct": chart_rise,
        "chart_gap_pct": chart_gap,
        "chart_zone_ratio": chart_zone,
        "chart_swing_pct": chart_swing,
    }

def is_chart_position_extended(item: dict) -> bool:
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    rise_pct = safe_float(profile.get("chart_rise_pct", 0), 0)
    gap_pct = safe_float(profile.get("chart_gap_pct", 999), 999)
    zone_ratio = safe_float(profile.get("chart_zone_ratio", -1), -1)
    swing_pct = safe_float(profile.get("chart_swing_pct", 0), 0)
    if 0 <= gap_pct <= CHART_POS_LATE_NEAR_HIGH_PCT:
        return True
    if zone_ratio >= CHART_POS_VERY_HIGH_ZONE_RATIO and rise_pct >= 1.8:
        return True
    if zone_ratio >= CHART_POS_HIGH_ZONE_RATIO and rise_pct >= max(2.2, swing_pct * 0.55) and gap_pct <= CHART_POS_EXTENDED_NEAR_HIGH_PCT:
        return True
    if rise_pct >= 5.5 and gap_pct <= 2.0:
        return True
    return False

def is_chart_position_good_for_live(item: dict) -> bool:
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    rise_pct = safe_float(profile.get("chart_rise_pct", 0), 0)
    gap_pct = safe_float(profile.get("chart_gap_pct", 999), 999)
    zone_ratio = safe_float(profile.get("chart_zone_ratio", -1), -1)
    swing_pct = safe_float(profile.get("chart_swing_pct", 0), 0)
    ctx = ensure_alert_chart_context(item)
    snap = ensure_pre_alert_snapshot(item)
    if bool(ctx.get("is_fading_after_spike", False)) and safe_float(snap.get("chg_30m", 0), 0) < 0.40:
        return False
    if str(ctx.get("chart_state_code", "")) == "HIGH_WOBBLE" and safe_float(snap.get("chg_30m", 0), 0) < 0.60:
        return False
    if 0 <= gap_pct <= 0.55:
        return False
    if rise_pct >= CHART_POS_LIVE_MAX_RISE_PCT and gap_pct <= 2.20:
        return False
    if rise_pct >= max(3.00, swing_pct * 0.58) and gap_pct <= 1.60:
        return False
    if zone_ratio >= CHART_POS_HIGH_ZONE_RATIO and rise_pct >= 1.80 and gap_pct <= CHART_POS_EXTENDED_NEAR_HIGH_PCT:
        return False
    if zone_ratio >= CHART_POS_VERY_HIGH_ZONE_RATIO and gap_pct <= 1.60:
        return False
    return True

def is_chart_position_good_for_restart(item: dict) -> bool:
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    rise_pct = safe_float(profile.get("chart_rise_pct", 0), 0)
    gap_pct = safe_float(profile.get("chart_gap_pct", 999), 999)
    zone_ratio = safe_float(profile.get("chart_zone_ratio", -1), -1)
    ctx = ensure_alert_chart_context(item)
    if bool(ctx.get("is_fading_after_spike", False)) and safe_float(ctx.get("recent_pullback_pct", 0), 0) >= ALERT_PULLBACK_RESTART_BLOCK_PCT:
        return False
    if 0 <= gap_pct <= 0.55:
        return False
    if rise_pct >= CHART_POS_RESTART_MAX_RISE_PCT and gap_pct <= 1.4:
        return False
    if rise_pct >= 3.00 and gap_pct <= 1.80:
        return False
    if zone_ratio >= 0.85 and rise_pct >= 2.0 and gap_pct <= 1.2:
        return False
    if zone_ratio >= CHART_POS_HIGH_ZONE_RATIO and rise_pct >= 1.8 and gap_pct <= 0.95:
        return False
    return True

def get_restart_quality_status(item: dict) -> tuple[bool, str]:
    if not is_chart_position_good_for_restart(item):
        return False, "재출발 자리로 보기엔 이미 위쪽"
    snap = ensure_pre_alert_snapshot(item)
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    high_gap_pct = get_effective_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    rise_pct = safe_float(profile.get("chart_rise_pct", 0), 0)
    zone_ratio = safe_float(profile.get("chart_zone_ratio", -1), -1)
    tags = set(item.get("pattern_tags", []))
    higher_low_hint = bool(snap.get("higher_low_hint", False))

    if high_gap_pct < 1.15:
        return False, "최근 고점이 너무 가까워서 재출발 자리로 애매"
    if high_gap_pct < 1.50 and max(chg_30, from_low) >= 0.95:
        return False, "조금 반등했어도 이미 많이 올라와서 재출발 자리로 늦음"
    if chg_5 <= 0.20 and chg_30 <= 0.40:
        return False, "재출발 힘이 아직 거의 안 보임"
    if vol_ratio < 3.20 and max(chg_5, chg_30, from_low) < 1.20:
        return False, "거래량과 반등 힘이 약해서 더 지켜봐야 함"
    if from_low >= RESTART_DISPLAY_MAX_FROM_LOW_PCT and high_gap_pct < 1.70:
        return False, "저점에서 이미 꽤 올라와서 재출발 자리로는 늦음"
    if rise_pct >= RESTART_DISPLAY_MAX_RISE_PCT and high_gap_pct < 2.30:
        return False, "차트상 이미 많이 올라온 뒤라 재출발로 보기 어려움"
    if zone_ratio >= CHART_POS_HIGH_ZONE_RATIO and high_gap_pct < 1.50:
        return False, "상단 구역에 가까워서 재출발 여유가 부족"
    if chg_5 < 0.00 and chg_30 < 1.60 and vol_ratio < 3.80:
        return False, "방금 흐름이 다시 꺾여서 재출발 후보로는 약함"
    if vol_ratio < 2.20:
        return False, "거래량이 너무 약해서 재출발 후보로 올리기엔 부족"
    if leader_score < 1.10 and max(chg_5, chg_30, from_low) < 1.40:
        return False, "주도 힘이 약해서 재출발 후보로 보기엔 부족"

    strength = 0
    if chg_5 >= RESTART_DISPLAY_MIN_5M_PCT:
        strength += 1
    if chg_30 >= 0.28:
        strength += 1
    if 0.22 <= from_low <= RESTART_DISPLAY_MAX_FROM_LOW_PCT:
        strength += 1
    if vol_ratio >= RESTART_DISPLAY_MIN_VOL_RATIO:
        strength += 1
    if leader_score >= RESTART_DISPLAY_MIN_LEADER_SCORE:
        strength += 1
    if high_gap_pct >= RESTART_DISPLAY_MIN_HIGH_GAP_PCT:
        strength += 1
    if max(chg_30, from_low) >= RESTART_DISPLAY_MIN_COMBINED_MOTION_PCT:
        strength += 1
    if higher_low_hint or tags.intersection({"저점높임", "가짜하락회복", "하락복구", "눌림재확인", "저가회복"}):
        strength += 1

    if strength < 7:
        return False, "재출발 힘이 아직 약해서 더 확인이 필요"
    return True, ""

def is_restart_candidate_strong(item: dict) -> bool:
    ok, _reason = get_restart_quality_status(item)
    return ok
def compute_pre_alert_snapshot_from_df(df, current_price: float = 0.0) -> dict:
    snap = {
        "chg_5m": 0.0,
        "chg_10m": 0.0,
        "chg_20m": 0.0,
        "chg_30m": 0.0,
        "from_30m_low_pct": 0.0,
        "below_30m_high_pct": 0.0,
        "vol_5v20_ratio": 0.0,
        "had_recent_spike": False,
        "higher_low_hint": False,
    }
    try:
        if df is None or len(df) < 12:
            return snap
        close = df["close"].astype(float).dropna()
        volume = df["volume"].astype(float).fillna(0)
        if close.empty:
            return snap
        price = current_price if current_price > 0 else float(close.iloc[-1])
        def back_change(n: int) -> float:
            if len(close) <= n:
                return 0.0
            base = float(close.iloc[-n - 1])
            if base <= 0:
                return 0.0
            return ((price - base) / base) * 100.0
        snap["chg_5m"] = round(back_change(5), 2)
        snap["chg_10m"] = round(back_change(10), 2)
        snap["chg_20m"] = round(back_change(20), 2)
        snap["chg_30m"] = round(back_change(30), 2)
        tail_30 = close.tail(min(len(close), 31))
        if not tail_30.empty:
            low30 = float(tail_30.min())
            high30 = float(tail_30.max())
            if low30 > 0:
                snap["from_30m_low_pct"] = round(((price - low30) / low30) * 100.0, 2)
            if high30 > 0:
                snap["below_30m_high_pct"] = round(((high30 - price) / high30) * 100.0, 2)
        vol_short = float(volume.tail(min(len(volume), 5)).mean()) if len(volume) >= 1 else 0.0
        vol_long_series = volume.tail(min(len(volume), 25))
        if len(vol_long_series) > 5:
            vol_long = float(vol_long_series.head(len(vol_long_series) - 5).mean())
        else:
            vol_long = float(volume.tail(min(len(volume), 20)).mean()) if len(volume) >= 1 else 0.0
        if vol_long > 0:
            snap["vol_5v20_ratio"] = round(vol_short / vol_long, 2)
        recent_spike_ref = max(
            abs(snap["chg_5m"]),
            abs(snap["chg_10m"]),
            abs(snap["chg_20m"]),
            abs(snap["chg_30m"]),
            snap["from_30m_low_pct"],
        )
        snap["had_recent_spike"] = recent_spike_ref >= 4.0
        lows = list(close.tail(min(len(close), 6)))
        if len(lows) >= 4:
            snap["higher_low_hint"] = lows[-1] >= min(lows[-4:-1])
    except Exception:
        return snap
    return snap

def compute_pre_alert_snapshot(ticker: str, current_price: float = 0.0) -> dict:
    try:
        df = get_ohlcv(ticker, "minute1")
        return compute_pre_alert_snapshot_from_df(df, current_price=current_price)
    except Exception:
        return compute_pre_alert_snapshot_from_df(None, current_price=current_price)
def ensure_pre_alert_snapshot(item: dict) -> dict:
    snap = item.get("pre_alert_snapshot")
    if isinstance(snap, dict):
        return snap
    ticker = item.get("ticker", "")
    current_price = safe_float(item.get("current_price", item.get("price", 0)), 0)
    snap = compute_pre_alert_snapshot(ticker, current_price=current_price)
    item["pre_alert_snapshot"] = snap
    return snap

def _last_upper_wick_pct(df) -> float:
    try:
        if df is None or len(df) < 1:
            return 0.0
        row = df.iloc[-1]
        op = safe_float(row.get("open", 0), 0)
        cl = safe_float(row.get("close", 0), 0)
        hi = safe_float(row.get("high", 0), 0)
        base = max(op, cl, 1e-9)
        if hi <= base:
            return 0.0
        return ((hi - base) / base) * 100.0
    except Exception:
        return 0.0

def compute_alert_chart_context_from_df(df1, df4h=None, current_price: float = 0.0) -> dict:
    ctx = {
        "chart_state": "흐름 확인",
        "chart_state_code": "CHECK",
        "chart_warning": "",
        "recent_pullback_pct": 0.0,
        "recent_bars_since_high": 999,
        "recent_upper_wick_pct": 0.0,
        "is_fading_after_spike": False,
        "htf_trend": "상위흐름 확인중",
        "htf_trend_code": "UNKNOWN",
        "htf_gap_pct": 999.0,
        "htf_rise_pct": 0.0,
    }
    try:
        price = safe_float(current_price, 0)
        if df1 is not None and len(df1) >= 12:
            close_price = safe_float(df1["close"].iloc[-1], 0)
            if price <= 0:
                price = close_price
            recent = df1.tail(min(len(df1), max(12, int(ALERT_CHART_CONTEXT_WINDOW_1M))))
            recent_high = safe_float(recent["high"].max(), 0)
            recent_low = safe_float(recent["low"].min(), 0)
            vals = list(recent["high"].astype(float).values)
            idx_high = vals.index(max(vals)) if vals else 0
            bars_since_high = max(len(vals) - idx_high - 1, 0)
            pullback_pct = ((recent_high - price) / recent_high) * 100.0 if recent_high > 0 and price > 0 else 0.0
            rise_from_low = ((price - recent_low) / recent_low) * 100.0 if recent_low > 0 and price > 0 else 0.0
            snap = compute_pre_alert_snapshot_from_df(df1, current_price=price)
            chg5 = safe_float(snap.get("chg_5m", 0), 0)
            chg30 = safe_float(snap.get("chg_30m", 0), 0)
            below_high = safe_float(snap.get("below_30m_high_pct", 999), 999)
            upper_wick_pct = _last_upper_wick_pct(df1)
            state = "흐름 확인"
            state_code = "CHECK"
            warning = ""
            fading = False
            if recent_high > 0 and pullback_pct >= ALERT_PULLBACK_LIVE_BLOCK_PCT and bars_since_high <= 8 and chg30 < 0.35:
                state = "고점 찍고 밀림"
                state_code = "FADE_AFTER_SPIKE"
                warning = f"방금 고점 찍고 {pullback_pct:.2f}% 밀린 상태"
                fading = True
            elif upper_wick_pct >= ALERT_WOBBLE_UPPER_WICK_PCT and below_high <= 2.40 and max(chg5, chg30) > 0:
                state = "위에서 흔들림"
                state_code = "HIGH_WOBBLE"
                warning = "위꼬리/흔들림이 보여서 추격 주의"
            elif chg5 >= 0.45 and chg30 >= 0.30 and rise_from_low <= 2.40 and below_high >= 1.20:
                state = "막 시작"
                state_code = "STARTING"
            elif chg5 >= 0.10 and chg30 < 0.20 and rise_from_low <= 1.80 and below_high >= 1.40:
                state = "눌림 뒤 재출발"
                state_code = "RESTARTING"
            elif rise_from_low >= 3.00 and below_high <= 1.20:
                state = "이미 한 번 쏜 자리"
                state_code = "EXTENDED"
                warning = "이미 한 번 강하게 움직인 뒤라 늦을 수 있음"
            elif chg5 > 0 and chg30 >= 0:
                state = "들썩이는 중"
                state_code = "WAKING_UP"
            ctx.update({
                "chart_state": state,
                "chart_state_code": state_code,
                "chart_warning": warning,
                "recent_pullback_pct": round(pullback_pct, 2),
                "recent_bars_since_high": int(bars_since_high),
                "recent_upper_wick_pct": round(upper_wick_pct, 2),
                "is_fading_after_spike": bool(fading),
            })
        if df4h is not None and len(df4h) >= 12:
            price4 = price if price > 0 else safe_float(df4h["close"].iloc[-1], 0)
            ma7 = ma(df4h, 7)
            ma30 = ma(df4h, 30)
            recent = df4h.tail(min(len(df4h), max(12, int(ALERT_HTF_LOOKBACK))))
            htf_high = safe_float(recent["high"].max(), 0)
            htf_low = safe_float(recent["low"].min(), 0)
            htf_gap = ((htf_high - price4) / htf_high) * 100.0 if htf_high > 0 and price4 > 0 else 999.0
            htf_rise = ((price4 - htf_low) / htf_low) * 100.0 if htf_low > 0 and price4 > 0 else 0.0
            trend = "상위흐름 확인중"
            trend_code = "UNKNOWN"
            if price4 >= ma7 > 0 and ma7 >= max(ma30, 0):
                trend = "4시간 상승"
                trend_code = "UP"
            elif price4 >= max(ma30, 0) and ma7 >= max(ma30 * 0.985, 0):
                trend = "4시간 회복"
                trend_code = "RECOVER"
            elif price4 < ma7 and ma7 > max(ma30, 0):
                trend = "4시간 고점 눌림"
                trend_code = "PULLBACK"
            elif price4 < ma7 and ma7 < max(ma30, 0):
                trend = "4시간 약함"
                trend_code = "WEAK"
            if htf_gap <= ALERT_HTF_STRONG_NEAR_HIGH_PCT and htf_rise >= 6.0 and trend_code in {"UP", "RECOVER"}:
                trend = "4시간 위쪽"
                trend_code = "NEAR_HIGH"
            ctx.update({
                "htf_trend": trend,
                "htf_trend_code": trend_code,
                "htf_gap_pct": round(htf_gap, 2),
                "htf_rise_pct": round(htf_rise, 2),
            })
    except Exception:
        return ctx
    return ctx

def compute_alert_chart_context(ticker: str, current_price: float = 0.0, df1=None, df4h=None) -> dict:
    try:
        df1 = df1 if df1 is not None else get_ohlcv(ticker, "minute1")
        df4h = df4h if df4h is not None else get_ohlcv(ticker, ALERT_HTF_INTERVAL)
        return compute_alert_chart_context_from_df(df1, df4h=df4h, current_price=current_price)
    except Exception:
        return compute_alert_chart_context_from_df(None, df4h=None, current_price=current_price)

def ensure_alert_chart_context(item: dict) -> dict:
    ctx = item.get("alert_chart_context")
    if isinstance(ctx, dict):
        return ctx
    ticker = str(item.get("ticker", "") or "").upper().strip()
    current_price = safe_float(item.get("current_price", item.get("price", 0)), 0)
    ctx = compute_alert_chart_context(ticker, current_price=current_price)
    item["alert_chart_context"] = ctx
    return ctx

def format_chart_context_line(item: dict) -> str:
    ctx = ensure_alert_chart_context(item)
    state = str(ctx.get("chart_state", "흐름 확인") or "흐름 확인")
    htf = str(ctx.get("htf_trend", "상위흐름 확인중") or "상위흐름 확인중")
    pullback = safe_float(ctx.get("recent_pullback_pct", 0), 0)
    warning = str(ctx.get("chart_warning", "") or "").strip()
    parts = [f"- 차트: {state}", f"상위흐름 {htf}"]
    if pullback > 0.05:
        parts.append(f"직전고점 뒤 {pullback:.2f}% 밀림")
    line = " / ".join(parts)
    if warning:
        line += f" / 주의: {warning}"
    return line
def format_recent_high_gap_text(gap_pct: float) -> str:
    gap_pct = safe_float(gap_pct, 0)
    if gap_pct >= 0:
        return f"최근고점까지 {gap_pct:.2f}% 남음"
    return f"최근고점 돌파 {abs(gap_pct):.2f}%"
def format_pre_alert_snapshot_line(item: dict) -> str:
    snap = ensure_pre_alert_snapshot(item)
    return (
        f"- 직전흐름: 5분 {snap.get('chg_5m', 0):+.2f}% / "
        f"30분 {snap.get('chg_30m', 0):+.2f}% / "
        f"30분저점대비 {snap.get('from_30m_low_pct', 0):+.2f}% / "
        f"{format_recent_high_gap_text(snap.get('below_30m_high_pct', 0))}"
    )
def init_price_track(ref: dict):
    ref.setdefault("track", {})
    track = ref["track"]
    alert_price = safe_float(ref.get("alert_price", 0))
    alert_ts = safe_float(ref.get("alert_ts", 0))
    track.setdefault("max_price", alert_price)
    track.setdefault("min_price", alert_price)
    track.setdefault("max_up_pct", 0.0)
    track.setdefault("max_down_pct", 0.0)
    track.setdefault("max_ts", alert_ts)
    track.setdefault("min_ts", alert_ts)
    track.setdefault("last_price", alert_price)
    track.setdefault("last_ts", alert_ts)
    track.setdefault("last_update_ts", 0.0)
    track.setdefault("samples", [])
    samples = track.get("samples")
    if not isinstance(samples, list):
        samples = []
        track["samples"] = samples
    if alert_price > 0 and alert_ts > 0 and not samples:
        samples.append({"ts": alert_ts, "price": alert_price})
def trim_price_track_samples(track: dict, keep_after_ts: float):
    samples = track.get("samples")
    if not isinstance(samples, list):
        track["samples"] = []
        return
    kept = []
    for row in samples:
        if not isinstance(row, dict):
            continue
        ts = safe_float(row.get("ts", 0), 0)
        price = safe_float(row.get("price", 0), 0)
        if ts <= 0 or price <= 0:
            continue
        if ts >= keep_after_ts:
            kept.append({"ts": ts, "price": price})
    track["samples"] = kept[-220:]
def update_price_track(ref: dict, price: float, now_ts: float):
    alert_price = safe_float(ref.get("alert_price", 0))
    if alert_price <= 0 or price <= 0:
        return
    init_price_track(ref)
    track = ref["track"]
    if price >= safe_float(track.get("max_price", 0), 0):
        track["max_price"] = price
        track["max_ts"] = now_ts
    if safe_float(track.get("min_price", 0), 0) <= 0 or price <= safe_float(track.get("min_price", 0), 0):
        track["min_price"] = price
        track["min_ts"] = now_ts
    track["last_price"] = price
    track["last_ts"] = now_ts
    track["max_up_pct"] = max(safe_float(track.get("max_up_pct", 0)), ((price - alert_price) / alert_price) * 100.0)
    track["max_down_pct"] = min(safe_float(track.get("max_down_pct", 0)), ((price - alert_price) / alert_price) * 100.0)
    track["last_update_ts"] = now_ts
    samples = track.get("samples")
    if not isinstance(samples, list):
        samples = []
        track["samples"] = samples
    last_sample_ts = safe_float(samples[-1].get("ts", 0), 0) if samples else 0
    if not samples or now_ts - last_sample_ts >= 1.0:
        samples.append({"ts": now_ts, "price": price})
    trim_price_track_samples(track, now_ts - PRICE_TRACK_WINDOW_SEC)
def get_price_track_summary(ref: dict, end_ts: float = None, fallback_price: float = 0.0):
    alert_price = safe_float(ref.get("alert_price", 0), 0)
    alert_ts = safe_float(ref.get("alert_ts", 0), 0)
    track = ref.get("track", {}) if isinstance(ref.get("track"), dict) else {}
    if end_ts is None or end_ts <= 0:
        end_ts = safe_float(track.get("last_ts", alert_ts), alert_ts)
    rows = []
    samples = track.get("samples") if isinstance(track.get("samples"), list) else []
    for row in samples:
        if not isinstance(row, dict):
            continue
        ts = safe_float(row.get("ts", 0), 0)
        price = safe_float(row.get("price", 0), 0)
        if ts < alert_ts or ts > end_ts or price <= 0:
            continue
        rows.append((ts, price))
    if not rows and alert_price > 0 and alert_ts > 0:
        rows.append((alert_ts, alert_price))
    if fallback_price > 0 and (not rows or safe_float(rows[-1][0], 0) < end_ts):
        rows.append((end_ts, fallback_price))
    if not rows:
        base_price = fallback_price if fallback_price > 0 else alert_price
        return {
            "end_ts": end_ts,
            "end_price": base_price,
            "max_price": base_price,
            "min_price": base_price,
            "max_up_pct": 0.0,
            "max_down_pct": 0.0,
            "max_ts": alert_ts,
            "min_ts": alert_ts,
        }
    max_ts, max_price = max(rows, key=lambda x: x[1])
    min_ts, min_price = min(rows, key=lambda x: x[1])
    end_price = rows[-1][1]
    max_up_pct = ((max_price - alert_price) / alert_price) * 100.0 if alert_price > 0 else 0.0
    max_down_pct = ((min_price - alert_price) / alert_price) * 100.0 if alert_price > 0 else 0.0
    sample_count = len(rows)
    coverage_sec = max(0.0, safe_float(rows[-1][0], alert_ts) - safe_float(rows[0][0], alert_ts)) if rows else 0.0
    return {
        "end_ts": end_ts,
        "end_price": end_price,
        "max_price": max_price,
        "min_price": min_price,
        "max_up_pct": max_up_pct,
        "max_down_pct": max_down_pct,
        "max_ts": max_ts,
        "min_ts": min_ts,
        "sample_count": sample_count,
        "coverage_sec": coverage_sec,
    }
def get_track_quality_text(summary: dict) -> str:
    sample_count = int(safe_float(summary.get("sample_count", 0), 0))
    coverage_sec = safe_float(summary.get("coverage_sec", 0), 0)
    if sample_count >= 4 and coverage_sec >= 60:
        status = "정상"
    else:
        status = "부족"
    return f"- 추적기록: {status} / 수집샘플 {sample_count}개"
def update_active_price_check_refs(now_ts: float = None):
    now_ts = safe_float(now_ts, time.time())
    active = []
    for ref_id, ref in PRICE_CHECK_REFS.items():
        alert_ts = safe_float(ref.get("alert_ts", 0))
        if alert_ts <= 0 or now_ts - alert_ts > PRICE_TRACK_WINDOW_SEC:
            continue
        active.append((ref_id, ref))
    active.sort(key=lambda x: safe_float(x[1].get("alert_ts", 0)), reverse=True)
    active = active[:20]
    changed = False
    for ref_id, ref in active:
        init_price_track(ref)
        last_update_ts = safe_float(ref.get("track", {}).get("last_update_ts", 0), 0)
        if now_ts - last_update_ts < PRICE_TRACK_UPDATE_SEC:
            continue
        ticker = ref.get("ticker", "")
        price = get_price(ticker)
        if price <= 0:
            continue
        update_price_track(ref, price, now_ts)
        changed = True
    if changed:
        persist_price_check_state()
def get_price_check_note(ref: dict, current_price: float) -> str:
    alert_price = safe_float(ref.get("alert_price", 0))
    if alert_price <= 0 or current_price <= 0:
        return "판단: 비교 불가"
    end_pct = ((current_price - alert_price) / alert_price) * 100.0
    track = ref.get("track", {}) if isinstance(ref.get("track"), dict) else {}
    max_up_pct = safe_float(track.get("max_up_pct", end_pct), end_pct)
    max_down_pct = safe_float(track.get("max_down_pct", min(0.0, end_pct)), min(0.0, end_pct))
    if max_up_pct >= 2.0 and end_pct <= 0.2:
        return "판단: 한 번 튀었지만 유지 실패. 계속 들고 가면 별로"
    if max_up_pct >= 1.5 and end_pct > 0.2:
        return "판단: 짧게는 먹었겠지만 지금 다시 사긴 별로"
    if end_pct >= 3.0:
        return "판단: 알림 뒤 확실히 더 갔음"
    if end_pct >= 1.0:
        return "판단: 더 가긴 했지만 큰 수익 자리까진 아님"
    if max_down_pct <= -2.0 or end_pct <= -2.0:
        return "판단: 버릴 후보. 알림 뒤 바로 무너짐"
    if end_pct <= -0.5:
        return "판단: 지금 사기 별로. 알림 뒤 밀림"
    return "판단: 지금 사기 별로. 크게 못 갔음"
def format_offset_text(base_ts: float, event_ts: float) -> str:
    base_ts = safe_float(base_ts, 0)
    event_ts = safe_float(event_ts, 0)
    if base_ts <= 0 or event_ts <= 0:
        return "--"
    diff = max(0, int(event_ts - base_ts))
    if diff < 60:
        return f"{diff}초 뒤"
    m, s = divmod(diff, 60)
    return f"{m}분 {s}초 뒤"
def cleanup_price_check_refs(now_ts: float = None):
    now_ts = safe_float(now_ts, time.time())
    changed = False
    expired = []
    for ref_id, data in PRICE_CHECK_REFS.items():
        saved_at = safe_float(data.get("alert_ts", 0))
        if saved_at <= 0 or now_ts - saved_at > PRICE_CHECK_REF_TTL_SEC:
            expired.append(ref_id)
    for ref_id in expired:
        PRICE_CHECK_REFS.pop(ref_id, None)
        changed = True
    if len(PRICE_CHECK_REFS) > PRICE_CHECK_REF_MAX:
        ordered = sorted(PRICE_CHECK_REFS.items(), key=lambda x: safe_float(x[1].get("alert_ts", 0)))
        drop_count = len(PRICE_CHECK_REFS) - PRICE_CHECK_REF_MAX
        for ref_id, _ in ordered[:drop_count]:
            PRICE_CHECK_REFS.pop(ref_id, None)
            changed = True
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
        changed = True
    if changed:
        persist_price_check_state()
def has_pending_30m_check_for_ticker(ticker: str, now_ts: float = None) -> bool:
    now_ts = safe_float(now_ts, time.time())
    if not ticker:
        return False
    for sched in PRICE_CHECK_SCHEDULED.values():
        if not isinstance(sched, dict):
            continue
        if int(safe_float(sched.get("target_min", 0), 0)) < 30:
            continue
        target_ts = safe_float(sched.get("target_ts", 0), 0)
        if target_ts <= now_ts:
            continue
        ref_id = str(sched.get("ref_id", "") or "").strip()
        ref = PRICE_CHECK_REFS.get(ref_id, {}) if ref_id else {}
        if str(ref.get("ticker", "") or "").upper() == str(ticker).upper():
            return True
    return False
def make_price_check_ref(item: dict, alert_type: str = "new", source_label: str = "", judgement_text: str = "") -> str:
    now_ts = time.time()
    cleanup_price_check_refs(now_ts)
    ticker = item.get("ticker", "?")
    alert_price = safe_float(item.get("current_price", item.get("price", 0)))
    ref_id = f"{ticker}_{int(now_ts * 1000)}"[-40:]
    pre_alert_snapshot = ensure_pre_alert_snapshot(item)
    PRICE_CHECK_REFS[ref_id] = {
        "ticker": ticker,
        "alert_price": alert_price,
        "alert_ts": now_ts,
        "strategy_label": item.get("strategy_label", ""),
        "alert_type": alert_type,
        "source_label": source_label or item.get("strategy_label", ""),
        "judgement_text": judgement_text or get_watch_judgement_text(item),
        "pre_alert_snapshot": pre_alert_snapshot,
        "root_message_id": None,
    }
    init_price_track(PRICE_CHECK_REFS[ref_id])
    update_price_track(PRICE_CHECK_REFS[ref_id], alert_price, now_ts)
    persist_price_check_state()
    return ref_id
def unpack_recent_entry(entry):
    if len(entry) >= 4:
        item, alert_type, reason_text, header = entry[0], entry[1], entry[2], entry[3]
    else:
        item, alert_type, reason_text = entry[0], entry[1], entry[2]
        header = ""
    return item, alert_type, reason_text, header
def get_bithumb_chart_url(ticker: str, market: str = "KRW") -> str:
    ticker = str(ticker or "").upper().strip()
    market = str(market or "KRW").upper().strip()
    return f"https://m.bithumb.com/react/trade/chart/{ticker}-{market}"
def build_price_check_markup(prepared_entries, section_key_override: str = ""):
    if not prepared_entries:
        return None
    rows = []
    for idx, prepared in enumerate(prepared_entries, start=1):
        item = prepared["item"]
        ref_id = prepared["ref_id"]
        ticker = item.get("ticker", "?")
        judgement = get_watch_judgement_text(item)
        caution = get_watch_caution_text(item)
        section_key = section_key_override or get_watch_display_group(item, judgement, caution)
        rows.append([
            InlineKeyboardButton(f"{bold_number_badge(idx)} {ticker} 빗썸 열기", url=get_bithumb_chart_url(ticker)),
        ])
        if section_key in {"entry", "recovery", "early", "restart", "live"}:
            rows.append([
                InlineKeyboardButton(f"{circled_number(idx)} 지금", callback_data=f"pc:{ref_id}:now"),
                InlineKeyboardButton("15분뒤", callback_data=f"pc:{ref_id}:15"),
                InlineKeyboardButton("30분뒤", callback_data=f"pc:{ref_id}:30"),
            ])
        else:
            rows.append([
                InlineKeyboardButton("30분뒤", callback_data=f"pc:{ref_id}:30"),
            ])
    return InlineKeyboardMarkup(rows)
def build_price_check_lines(ref: dict, current_price: float, now_ts: float, target_min: int = 0, summary: dict = None, target_ts: float = None):
    ticker = ref.get("ticker", "?")
    alert_price = safe_float(ref.get("alert_price", 0))
    alert_ts = safe_float(ref.get("alert_ts", 0))
    source_label = (ref.get("source_label") or "").strip()
    strategy_label = (ref.get("strategy_label") or "").strip()
    judgement_text = (ref.get("judgement_text") or "").strip()
    snap = ref.get("pre_alert_snapshot", {}) if isinstance(ref.get("pre_alert_snapshot"), dict) else {}
    if target_min > 0:
        target_ts = safe_float(target_ts, alert_ts + target_min * 60)
        effective_ts = target_ts
    else:
        effective_ts = now_ts
    if summary is None:
        summary = get_price_track_summary(ref, effective_ts, fallback_price=current_price)
    end_price = safe_float(summary.get("end_price", current_price), current_price)
    change_pct = ((end_price - alert_price) / alert_price * 100.0) if alert_price > 0 else 0.0
    elapsed_sec = max(0.0, effective_ts - alert_ts)
    if target_min <= 0:
        title = f"💰 {bold_ticker_text(ticker)} 지금 확인"
    else:
        title = f"⏱ {bold_ticker_text(ticker)} {target_min}분 확인"
    lines = [title, ""]
    if source_label:
        lines.append(f"- 알림종류: {source_label}")
    if strategy_label:
        lines.append(f"- 전략: {strategy_label}")
    if judgement_text:
        lines.append(f"- 한눈판단: {judgement_text}")
    if snap:
        lines.append(
            f"- 직전흐름: 5분 {safe_float(snap.get('chg_5m', 0)):+.2f}% / "
            f"30분 {safe_float(snap.get('chg_30m', 0)):+.2f}% / "
            f"30분저점대비 {safe_float(snap.get('from_30m_low_pct', 0)):+.2f}% / "
            f"{format_recent_high_gap_text(safe_float(snap.get('below_30m_high_pct', 0), 0))}"
        )
    lines.append("")
    lines.extend([
        f"- 알림가: {fmt_price(alert_price)}",
        f"- 현재가: {fmt_price(end_price)}",
        f"- 변화: {change_pct:+.2f}%",
    ])
    max_price = safe_float(summary.get("max_price", 0), 0)
    min_price = safe_float(summary.get("min_price", 0), 0)
    if max_price > 0:
        lines.append(f"- 중간 최고가: {fmt_price(max_price)} ({safe_float(summary.get('max_up_pct', 0), 0):+.2f}%) / {format_offset_text(alert_ts, safe_float(summary.get('max_ts', 0), alert_ts))}")
    if min_price > 0:
        lines.append(f"- 중간 최저가: {fmt_price(min_price)} ({safe_float(summary.get('max_down_pct', 0), 0):+.2f}%) / {format_offset_text(alert_ts, safe_float(summary.get('min_ts', 0), alert_ts))}")
    lines.append(get_track_quality_text(summary))
    lines.extend([
        "",
        f"- 알림시각: {format_hms(alert_ts)}",
        f"- 확인시각: {format_hms(effective_ts)}",
        f"- 지난시간: {format_elapsed_text(elapsed_sec)}",
        "",
        get_price_check_note(ref, end_price),
    ])
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
        update_price_track(ref, current_price, now_ts)
        if target_min > 0:
            target_ts = safe_float(ref.get("alert_ts", 0)) + target_min * 60
            summary = get_price_track_summary(ref, target_ts, fallback_price=0.0)
            if safe_float(summary.get("end_price", 0), 0) <= 0:
                summary = get_price_track_summary(ref, target_ts, fallback_price=current_price)
            msg = "\n".join(build_price_check_lines(ref, current_price, now_ts, target_min=target_min, summary=summary, target_ts=target_ts))
        else:
            summary = get_price_track_summary(ref, now_ts, fallback_price=current_price)
            msg = "\n".join(build_price_check_lines(ref, current_price, now_ts, target_min=target_min, summary=summary, target_ts=now_ts))
        bot.send_message(
            chat_id=chat_id,
            text=msg,
            reply_to_message_id=ref.get("root_message_id"),
        )
        persist_price_check_state()
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
        persist_price_check_state()
        if not isinstance(sched, dict):
            continue
        ref_id = (sched.get("ref_id") or "").strip()
        ref = PRICE_CHECK_REFS.get(ref_id)
        if not ref:
            continue
        chat_id = sched.get("chat_id", CHAT_ID)
        target_min = int(safe_float(sched.get("target_min", 0), 0))
        send_price_check_result(chat_id, ref, target_min=target_min, now_ts=now_ts)
def cleanup_recent_candidate_alerts(now_ts: float = None):
    now_ts = safe_float(now_ts, time.time())
    fresh = []
    for row in RECENT_CANDIDATE_ALERTS:
        if not isinstance(row, dict):
            continue
        saved_at = safe_float(row.get("saved_at", 0))
        if saved_at <= 0:
            continue
        if now_ts - saved_at > RECENT_CANDIDATE_TTL_SEC:
            continue
        fresh.append(row)
    del RECENT_CANDIDATE_ALERTS[:]
    RECENT_CANDIDATE_ALERTS.extend(fresh[-RECENT_CANDIDATE_MAX:])
    persist_recent_candidate_alerts()
def save_recent_candidate_alerts(entries, header: str = ""):
    if not entries:
        return
    now_ts = time.time()
    cleanup_recent_candidate_alerts(now_ts)
    for item, alert_type, reason_text in entries:
        try:
            RECENT_CANDIDATE_ALERTS.append({
                "saved_at": now_ts,
                "header": header or "",
                "item": dict(item),
                "alert_type": alert_type or "new",
                "reason_text": reason_text or "",
            })
        except Exception:
            continue
    cleanup_recent_candidate_alerts(now_ts)
def get_recent_candidate_entries(limit: int = 5):
    cleanup_recent_candidate_alerts()
    rows = RECENT_CANDIDATE_ALERTS[-max(1, int(limit)): ]
    rows = list(reversed(rows))
    entries = []
    for row in rows:
        item = row.get("item")
        if not isinstance(item, dict):
            continue
        entries.append((item, row.get("alert_type", "new"), row.get("reason_text", ""), row.get("header", "")))
    return entries
def prepare_watch_entries(entries):
    prepared = []
    for entry in entries:
        item, alert_type, reason_text, header = unpack_recent_entry(entry)
        ensure_pre_alert_snapshot(item)
        ref_id = make_price_check_ref(
            item,
            alert_type=alert_type,
            source_label=header or item.get("strategy_label", ""),
            judgement_text=get_watch_judgement_text(item),
        )
        prepared.append({
            "entry": entry,
            "item": item,
            "alert_type": alert_type,
            "reason_text": reason_text,
            "header": header,
            "ref_id": ref_id,
        })
    return prepared
def recent5_command(update, context: CallbackContext):
    entries = get_recent_candidate_entries(limit=5)
    if not entries:
        send("🗂 최근 후보 5개\n\n아직 조금 더 볼 후보가 없어.")
        return
    prepared = prepare_watch_entries(entries)
    body = []
    for idx, prepared_entry in enumerate(prepared, start=1):
        item = prepared_entry["item"]
        alert_type = prepared_entry["alert_type"]
        reason_text = prepared_entry["reason_text"]
        body.append(format_watch_alert_line(item, order=idx, alert_type=alert_type, reason_text=reason_text))
    markup = build_price_check_markup(prepared)
    sent = send("🗂 최근 후보 5개\n\n최근에 잡힌 후보를 다시 보여줄게.\n\n" + "\n\n".join(body), reply_markup=markup)
    if sent and getattr(sent, "message_id", None):
        for prepared_entry in prepared:
            PRICE_CHECK_REFS[prepared_entry["ref_id"]]["root_message_id"] = sent.message_id
        persist_price_check_state()
def send_watch_alert_bundle(header: str, entries, section_key: str = ""):
    if not entries:
        return
    prepared = prepare_watch_entries(entries)
    save_recent_candidate_alerts(entries, header=header)
    body = []
    for idx, prepared_entry in enumerate(prepared, start=1):
        item = prepared_entry["item"]
        alert_type = prepared_entry["alert_type"]
        reason_text = prepared_entry["reason_text"]
        body.append(
            format_watch_alert_line(
                item,
                order=idx,
                alert_type=alert_type,
                reason_text=reason_text,
                section_override=section_key,
            )
        )
    markup = build_price_check_markup(prepared, section_key_override=section_key)
    sent = send(header + "\n\n" + "\n\n".join(body), reply_markup=markup)
    if sent and getattr(sent, "message_id", None):
        for prepared_entry in prepared:
            PRICE_CHECK_REFS[prepared_entry["ref_id"]]["root_message_id"] = sent.message_id
        persist_price_check_state()
STRATEGY_GUIDE = {
    "초반 선점형": "막 출발할 가능성이 있는 초입 자리",
    "상승 시작형": "막 오르려는 듯 보이지만 힘 확인이 더 필요한 자리",
    "눌림 반등형": "오른 뒤 눌렸다가 다시 사는지 보는 자리",
    "추세 지속형": "이미 오른 흐름이 이어지는 자리라 추격 조심이 필요함",
    "쏘기 직전형": "곧 강하게 움직일 수 있지만 실패하면 바로 식는 자리",
    "급등 돌파형": "저항을 뚫는 자리지만 늦으면 물릴 수 있음",
    "추격형": "이미 오른 뒤 따라붙는 자리라 보통은 별로",
    "주도주 후보형": "시장 관심은 붙었지만 지금 사도 되는지는 별개",
    "실시간 감시형": "시장 관심은 붙지만 아직 사기엔 이른 후보",
    "실시간 강세형": "순위나 추천에서 막 힘이 붙는 자리",
    "실시간 주도형": "주목도와 거래량이 붙어 실시간으로 보는 자리",
    "예비 후보 감시": "아직 사면 안 되고 먼저 지켜봐야 하는 후보",
    "신저가 반등 감시": "바닥 확인 중인 후보. 지금 매수보다 반등 확인이 먼저",
}
TERM_GUIDE = [
    ("등급 S/A/B", "S가 가장 강하고 B는 약한 편이라 그냥 사면 안 될 수 있음"),
    ("시장주목도", "지금 시장에서 돈과 관심이 얼마나 붙는지 보여주는 점수"),
    ("초반 후보", "아직 크게 가기 전, 막 살아나는 초입 후보"),
    ("재출발 후보", "한 번 움직인 뒤 눌리고 다시 가려는 후보"),
    ("실시간 강세 후보", "순위나 추천에서 막 힘 붙기 시작한 후보"),
    ("늦어서 별로인 후보", "이미 많이 가서 지금 추격하면 별로인 후보"),
    ("감시만 할 후보", "아직 사지 말고 흐름만 보면 되는 후보"),
    ("자동 쉬기", "연속 손실이나 장 악화 때문에 자동매수를 잠깐 멈춘 상태"),
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
    lines.append("- 상승%가 너무 큰 후보는 이미 늦은 자리일 수 있어")
    lines.append("- 거래량만 세다고 바로 사면 안 되고, 가격 유지까지 같이 봐야 해")
    lines.append("- 자동매수는 꺼둔 상태에서 알림 품질부터 보는 게 안전해")
    return "\n".join(lines)
def format_strategy_with_help(strategy_label: str) -> str:
    return f"{strategy_label} ({strategy_help_text(strategy_label)})"
def circled_number(n: int) -> str:
    nums = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
    if 1 <= n <= len(nums):
        return nums[n - 1]
    return f"{n}."

def bold_number_badge(n: int) -> str:
    return f"【{circled_number(n)}】"

def bold_ticker_text(ticker: str) -> str:
    t = str(ticker or "?").upper().strip() or "?"
    return f"《{t}》"

def highlight_line(order: int, ticker: str, icon: str = "") -> str:
    prefix = f"{bold_number_badge(order)} "
    if icon:
        prefix += f"{icon} "
    return f"{prefix}{bold_ticker_text(ticker)}"
def get_watch_item_icon(item: dict, alert_type: str = "new") -> str:
    if alert_type == "upgrade":
        return "⬆️"
    if alert_type == "renotice":
        return "🔁"
    strategy_label = item.get("strategy_label", "")
    change_pct = safe_float(item.get("change_pct", 0))
    if strategy_label == "예비 후보 감시":
        return "🧭"
    if strategy_label == "신저가 반등 감시":
        return "🪫"
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
    high_gap_pct = get_reference_high_gap_pct(item)
    snap = ensure_pre_alert_snapshot(item)
    if strategy_label == "예비 후보 감시":
        return "지금 사면 안 됨. 먼저 흐름만 확인"
    if strategy_label == "신저가 반등 감시":
        return "지금 매수 금지. 저가 다시 깨지면 바로 버릴 후보"
    if is_chart_position_extended(item):
        return "이미 위쪽 자리라 지금 추격은 별로"
    if 0 <= high_gap_pct <= 0.70:
        return "지금 들어가면 늦음"
    if change_pct >= 18.0:
        return "이미 많이 올랐음. 지금 추격 금지"
    if change_pct >= 12.0:
        return "이미 꽤 오른 자리라 지금 사기 별로"
    if strategy_label == "눌림 반등형" and change_pct >= 7.0:
        return "이미 한 번 오른 뒤 재시도라 지금 사기 별로"
    if change_pct >= 10.0 and vol_ratio < 2.4:
        return "올라간 것 같아도 힘이 약함"
    if safe_float(snap.get("from_30m_low_pct", 0)) >= 4.5 and safe_float(snap.get("below_30m_high_pct", 0)) <= 1.0:
        return "이미 한 번 튄 자리라 추격 금지"
    return ""
def get_watch_judgement_text(item: dict) -> str:
    strategy_label = item.get("strategy_label", "")
    change_pct = safe_float(item.get("change_pct", 0))
    vol_ratio = safe_float(item.get("vol_ratio", 0))
    caution = get_watch_caution_text(item)
    snap = ensure_pre_alert_snapshot(item)
    if strategy_label == "예비 후보 감시":
        return "👀 지금 사면 안 됨"
    if strategy_label == "신저가 반등 감시":
        return "👀 지금은 구경만"
    if caution:
        return "⛔ 지금 사기 별로"
    if strategy_label in ["상승 시작형", "초반 선점형"]:
        if change_pct <= 2.2 and vol_ratio >= 2.4 and safe_float(snap.get("from_30m_low_pct", 0)) <= 3.8:
            return "✅ 지금만 잠깐 볼 만함"
        return "👀 초반처럼 보여도 아직 힘 부족"
    if strategy_label == "눌림 반등형":
        return "👀 다시 사는지 확인부터"
    if strategy_label in ["추세 지속형", "급등 돌파형"]:
        return "⚠️ 힘은 있는데 지금 추격은 별로"
    return "👀 지금은 구경만"
def get_watch_one_line_text(item: dict, caution: str, reason_text: str) -> str:
    for candidate in [caution, reason_text]:
        candidate = (candidate or "").strip()
        if candidate:
            return candidate
    strategy_label = item.get("strategy_label", "")
    if strategy_label == "예비 후보 감시":
        return "지금은 흐름만 확인"
    if strategy_label == "신저가 반등 감시":
        return "반등 확인 전이라 지금 매수는 금지"
    return "지금은 흐름만 확인"
def should_use_compact_watch_alert(item: dict, judgement: str, caution: str) -> bool:
    return get_watch_alert_detail_level(item, judgement, caution) == "compact"
def get_watch_alert_detail_level(item: dict, judgement: str = "", caution: str = "") -> str:
    strategy_label = item.get("strategy_label", "")
    strategy = item.get("strategy", "")
    trade_tier = str(item.get("trade_tier", "B") or "B").upper().strip()
    judgement = judgement or get_watch_judgement_text(item)
    caution = caution or get_watch_caution_text(item)
    if strategy_label in ["예비 후보 감시", "신저가 반등 감시"]:
        return "compact"
    weak_keywords = ["구경만", "힘 부족", "사면 안 됨", "추격은 별로", "지금 사기 별로", "다시 확인부터"]
    if caution or any(word in judgement for word in weak_keywords):
        return "compact"
    if trade_tier in ["S", "A"] or strategy in ["EARLY", "PRE_BREAKOUT", "TREND_CONT", "BREAKOUT"]:
        return "strong"
    return "normal"
def get_watch_display_conclusion(item: dict, judgement: str = "", caution: str = "", section_override: str = "") -> str:
    strategy_label = item.get("strategy_label", "")
    strategy = str(item.get("strategy", "") or "").strip()
    judgement = judgement or get_watch_judgement_text(item)
    caution = caution or get_watch_caution_text(item)
    if caution:
        if "늦음" in caution or "추격" in caution or "사기 별로" in caution:
            conclusion = "지금 사기 별로"
        elif "힘이 약" in caution:
            conclusion = "힘이 약해서 더 확인 필요"
        else:
            conclusion = caution
    elif strategy_label == "신저가 반등 감시":
        conclusion = "다시 사는지 확인 먼저"
    elif strategy_label == "예비 후보 감시":
        conclusion = "지금은 구경만"
    elif "볼 만함" in judgement:
        conclusion = "지금 볼만함"
    elif strategy_label == "눌림 반등형":
        conclusion = "다시 사는지 확인 먼저"
    elif strategy_label in ["추세 지속형", "급등 돌파형"]:
        conclusion = "힘은 있는데 추격 금지"
    elif strategy_label in ["상승 시작형", "초반 선점형"]:
        conclusion = "조금 더 확인 필요"
    else:
        conclusion = "지금은 구경만"
    floor = get_conclusion_section_floor(conclusion)
    target = str(section_override or "").strip()
    if target == "entry" and floor in {"watch", "recovery", "late"}:
        if strategy in {"EARLY", "PRE_BREAKOUT", "PREPUMP", "LEADER_WATCH"}:
            return "지금 볼만함"
        return "지금은 흐름만 확인"
    if target == "recovery" and floor in {"watch", "late"}:
        return "다시 사는지 확인 먼저"
    if target == "watch" and floor == "late":
        return "지금은 구경만"
    return conclusion
def is_late_conclusion_text(text: str) -> bool:
    text = str(text or "")
    late_keywords = ["늦", "추격", "사기 별로", "매수 금지"]
    return any(word in text for word in late_keywords)
def get_conclusion_section_floor(conclusion: str) -> str:
    conclusion = str(conclusion or "").strip()
    if not conclusion:
        return ""
    if "매수 금지" in conclusion:
        return "watch"
    if any(word in conclusion for word in ["지금 사기 별로", "늦", "추격 금지"]):
        return "late"
    if any(word in conclusion for word in ["다시 사는지 확인", "조금 더 확인", "힘이 약해서 더 확인"]):
        return "recovery"
    if "구경만" in conclusion:
        return "watch"
    if any(word in conclusion for word in ["지금 볼만함", "지금만 잠깐 볼 만함", "지금 진입 가능"]):
        return "entry"
    return ""
def is_watch_excluded_ticker(ticker: str) -> bool:
    return str(ticker or "").upper().strip() in WATCH_EXCLUDED_TICKERS
def has_meaningful_watch_motion(item: dict) -> bool:
    ticker = item.get("ticker", "")
    if is_watch_excluded_ticker(ticker):
        return False
    snap = ensure_pre_alert_snapshot(item)
    chg_5 = abs(safe_float(snap.get("chg_5m", 0), 0))
    chg_30 = abs(safe_float(snap.get("chg_30m", 0), 0))
    from_low = abs(safe_float(snap.get("from_30m_low_pct", 0), 0))
    change_now = abs(safe_float(item.get("change_pct", 0), 0))
    range_pct = abs(safe_float(item.get("range_pct", 0), 0))
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    strong_motion = max(chg_30, from_low, chg_5, change_now)
    overall_motion = max(strong_motion, range_pct)
    if vol_ratio >= WATCH_HIGH_VOL_MIN:
        return strong_motion >= WATCH_HIGH_VOL_MIN_MOVE_PCT or range_pct >= WATCH_MIN_RANGE_PCT
    if vol_ratio >= 1.70 and (chg_5 >= 0.00 or chg_30 >= 0.05 or from_low >= 0.05):
        return True
    if leader_score >= 0.90 and vol_ratio >= 1.15 and (chg_5 >= -0.02 or chg_30 >= 0.05 or from_low >= 0.05):
        return True
    if overall_motion < WATCH_MIN_EFFECTIVE_30M_MOVE_PCT:
        return False
    if from_low < WATCH_MIN_RECOVERY_FROM_LOW_PCT and chg_30 < WATCH_MIN_EFFECTIVE_30M_MOVE_PCT and change_now < WATCH_HIGH_VOL_MIN_MOVE_PCT:
        return False
    return True
def is_fast_start_entry_candidate(item: dict, judgement: str = "", caution: str = "", regime: dict = None) -> bool:
    if not has_meaningful_watch_motion(item):
        return False
    if is_watch_late_candidate(item, judgement, caution):
        return False
    if caution:
        return False
    regime = regime or get_market_regime()
    regime_name = str((regime or {}).get("name", "NORMAL") or "NORMAL")
    if regime_name == "BLOCK":
        return False
    strategy = str(item.get("strategy", "") or "").strip()
    if strategy not in {"LEADER_WATCH", "PREPUMP", "PULLBACK"}:
        return False
    if not is_chart_position_good_for_live(item):
        return False
    snap = ensure_pre_alert_snapshot(item)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    change_pct = safe_float(item.get("change_pct", 0), 0)
    tags = set(item.get("pattern_tags", []))
    if high_gap_pct < max(FAST_ENTRY_MIN_HIGH_GAP_PCT, 0.85) or high_gap_pct > 2.40:
        return False
    if from_low > 3.20:
        return False
    if chg_5 < 0.05:
        return False
    if change_pct > 4.20 and high_gap_pct < 1.25:
        return False
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
        if chg_5 < 0.10 or high_gap_pct < 0.75:
            return False
    if strategy == "LEADER_WATCH" and chg_30 < 0.10 and from_low < 0.25:
        return False
    strength = 0
    if chg_5 >= max(FAST_ENTRY_MIN_5M_PCT, 0.10):
        strength += 1
    if chg_30 >= max(FAST_ENTRY_MIN_30M_PCT, 0.15):
        strength += 1
    if from_low >= max(FAST_ENTRY_MIN_FROM_LOW_PCT, 0.25):
        strength += 1
    if vol_ratio >= max(FAST_ENTRY_MIN_VOL_RATIO, 1.35):
        strength += 1
    if leader_score >= max(FAST_ENTRY_MIN_LEADER_SCORE, 1.10):
        strength += 1
    if tags.intersection({"저점높임", "상단압축", "양봉절반유지", "가짜하락회복", "눌림재확인", "저가회복"}):
        strength += 1
    if vol_ratio >= 2.60 and max(chg_5, chg_30, from_low) >= 0.55:
        strength += 1
    threshold = 4
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
        threshold = 4
    elif strategy == "LEADER_WATCH":
        threshold = 3
    if regime_name == "SIDEWAYS":
        if chg_5 >= 0.20 and vol_ratio >= 1.65 and high_gap_pct >= 0.80:
            threshold -= 1
    elif regime_name == "WEAK":
        threshold += 1
    if strategy == "LEADER_WATCH" and vol_ratio >= 1.90 and leader_score >= 1.20 and max(chg_5, chg_30, from_low) >= 0.30:
        threshold -= 1
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"} and vol_ratio >= 1.90 and max(chg_5, from_low, chg_30) >= 0.35 and high_gap_pct >= 0.90:
        threshold -= 1
    if max(chg_5, chg_30, from_low) >= 0.70 and vol_ratio >= 1.55 and high_gap_pct >= 0.85:
        threshold -= 1
    threshold = max(2, threshold)
    return strength >= threshold
def should_promote_recovery_candidate_to_entry(item: dict, judgement: str = "", caution: str = "", regime: dict = None) -> bool:
    if not has_meaningful_watch_motion(item):
        return False
    if is_watch_late_candidate(item, judgement, caution):
        return False
    if caution:
        return False
    regime = regime or get_market_regime()
    regime_name = str((regime or {}).get("name", "NORMAL") or "NORMAL")
    if regime_name == "BLOCK":
        return False
    strategy = str(item.get("strategy", "") or "").strip()
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
        return False
    restart_ok, _restart_reason = get_restart_quality_status(item)
    if not restart_ok:
        return False
    snap = ensure_pre_alert_snapshot(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    change_pct = safe_float(item.get("change_pct", 0), 0)
    tags = set(item.get("pattern_tags", []))
    if high_gap_pct < max(RECOVERY_ENTRY_MIN_HIGH_GAP_PCT, 0.85):
        return False
    if high_gap_pct <= 0.55:
        return False
    if chg_5 < 0.10:
        return False
    if from_low > 2.20 and high_gap_pct < 1.20:
        return False
    if regime_name == "WEAK" and REGIME_RECOVERY_ENTRY_BLOCK_IN_WEAK and not should_keep_recovery_candidate_in_weak_regime(item, regime=regime):
        return False
    strength = 0
    if chg_30 >= max(RECOVERY_ENTRY_MIN_30M_PCT, 0.10):
        strength += 1
    if from_low >= max(RECOVERY_ENTRY_MIN_FROM_LOW_PCT, 0.25):
        strength += 1
    if vol_ratio >= max(RECOVERY_ENTRY_MIN_VOL_RATIO, 1.35):
        strength += 1
    if leader_score >= max(RECOVERY_ENTRY_MIN_LEADER_SCORE, 1.00):
        strength += 1
    if chg_5 >= max(RECOVERY_ENTRY_MIN_5M_PCT, 0.10) or change_pct >= 0.45:
        strength += 1
    if tags.intersection({"저점높임", "가짜하락회복", "하락복구", "눌림재확인", "저가회복"}):
        strength += 1
    if vol_ratio >= 3.20 and max(from_low, chg_30, change_pct) >= 0.45:
        strength += 1
    if leader_score >= 2.10 and high_gap_pct >= 1.00:
        strength += 1
    threshold = 3
    if strategy == "LEADER_WATCH":
        chart_ctx = ensure_alert_chart_context(item)
        if str(chart_ctx.get("chart_state_code", "")) in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}:
            return "restart" if restart_ok and high_gap_pct >= 1.15 else "watch"
        threshold = 2
    elif strategy in ["PREWATCH", "NEWLOW_RECOVER"]:
        threshold = 4
    if strategy == "LEADER_WATCH" and leader_score >= 1.35 and vol_ratio >= 1.45 and max(from_low, chg_30, change_pct) >= 0.25:
        threshold -= 1
    if strategy in ["PREWATCH", "NEWLOW_RECOVER"] and vol_ratio >= 2.00 and high_gap_pct >= 0.90 and chg_5 >= 0.15 and from_low >= 0.40:
        threshold -= 1
    if chg_5 >= 0.15 and vol_ratio >= 1.45 and high_gap_pct >= 0.75 and max(from_low, chg_30, change_pct) >= 0.25:
        threshold -= 1
    if regime_name == "SIDEWAYS":
        threshold += max(REGIME_RECOVERY_ENTRY_SIDEWAYS_EXTRA_STRENGTH, 0)
        if chg_5 >= 0.12 and vol_ratio >= 1.55 and high_gap_pct >= 0.80:
            threshold -= 1
    if regime_name == "WEAK":
        threshold += 1
    threshold = max(2, threshold)
    return strength >= threshold
def should_keep_recovery_candidate_in_weak_regime(item: dict, regime: dict = None) -> bool:
    regime = regime or get_market_regime()
    if str((regime or {}).get("name", "NORMAL") or "NORMAL") != "WEAK":
        return True
    snap = ensure_pre_alert_snapshot(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    return (
        vol_ratio >= REGIME_RECOVERY_ONLY_MIN_VOL_WEAK
        and leader_score >= REGIME_RECOVERY_ONLY_MIN_LEADER_WEAK
        and from_low >= 0.65
        and chg_5 >= 0.00
    )
def is_watch_late_candidate(item: dict, judgement: str = "", caution: str = "") -> bool:
    judgement = judgement or get_watch_judgement_text(item)
    caution = caution or get_watch_caution_text(item)
    conclusion = get_watch_display_conclusion(item, judgement, caution)
    strategy = str(item.get("strategy", "") or "").strip()
    high_gap_pct = get_reference_high_gap_pct(item)
    snap = ensure_pre_alert_snapshot(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    if strategy in ["PREWATCH", "NEWLOW_RECOVER"]:
        return False
    if is_late_conclusion_text(caution) or is_late_conclusion_text(conclusion):
        return True
    if strategy in ["TREND_CONT", "BREAKOUT", "CHASE"] and 0 <= high_gap_pct <= 1.50:
        return True
    if from_low >= 2.20 and 0 <= high_gap_pct <= 1.60:
        return True
    return False
def is_low_zone_recovery_candidate(item: dict, judgement: str = "", caution: str = "") -> bool:
    if not has_meaningful_watch_motion(item):
        return False
    strategy = str(item.get("strategy", "") or "").strip()
    if is_watch_late_candidate(item, judgement, caution):
        return False
    if not is_restart_candidate_strong(item):
        return False
    snap = ensure_pre_alert_snapshot(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    tags = set(item.get("pattern_tags", []))
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    if high_gap_pct <= 0.55:
        return False
    if max(chg_5, chg_30, from_low) < 0.35 and vol_ratio < 2.30:
        return False
    if from_low > 2.60 and high_gap_pct < 1.30:
        return False
    if strategy in ["PREWATCH", "NEWLOW_RECOVER"]:
        return (
            high_gap_pct >= 1.00
            and (
                (chg_5 >= 0.18 and vol_ratio >= 1.85 and max(chg_30, from_low) >= 0.45)
                or (chg_30 >= 0.45 and from_low >= 0.50 and vol_ratio >= 1.70)
                or (leader_score >= 1.45 and vol_ratio >= 2.10 and from_low >= 0.45 and chg_5 >= 0.15)
            )
        )
    if strategy == "LEADER_WATCH":
        return (
            from_low >= 0.25
            and high_gap_pct >= 0.80
            and (
                (chg_30 >= 0.28 and vol_ratio >= 1.45)
                or (chg_5 >= 0.12 and vol_ratio >= 1.35 and from_low >= 0.25)
                or (leader_score >= 1.10 and vol_ratio >= 1.30 and from_low >= 0.25)
            )
        )
    if strategy in ["PREPUMP", "PULLBACK"]:
        return high_gap_pct >= 0.95 and vol_ratio >= 1.60 and (chg_5 >= 0.15 or chg_30 >= 0.35 or from_low >= 0.50)
    if tags.intersection({"저점높임", "가짜하락회복", "하락복구", "눌림재확인"}) and from_low <= 2.10 and high_gap_pct >= 1.10 and max(chg_5, chg_30) >= 0.10:
        return True
    if leader_score >= 2.00 and vol_ratio >= 2.30 and from_low <= 1.95 and high_gap_pct >= 1.20 and max(chg_5, chg_30, from_low) >= 0.35:
        return True
    return False

def get_watch_section_key(item: dict, judgement: str = "", caution: str = "", regime: dict = None) -> str:
    judgement = judgement or get_watch_judgement_text(item)
    caution = caution or get_watch_caution_text(item)
    conclusion = get_watch_display_conclusion(item, judgement, caution)
    regime = regime or get_market_regime()
    regime_name = str((regime or {}).get("name", "NORMAL") or "NORMAL")
    strategy = str(item.get("strategy", "") or "").strip()
    trade_tier = get_watch_tier_text(item)
    snap = ensure_pre_alert_snapshot(item)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    if high_gap_pct <= 0.20:
        section = "late"
    elif is_chart_position_extended(item):
        section = "late" if high_gap_pct <= 1.40 else "recovery"
    elif is_watch_late_candidate(item, judgement, caution):
        section = "late"
    elif is_fast_start_entry_candidate(item, judgement, caution, regime=regime):
        section = "entry"
    elif is_low_zone_recovery_candidate(item, judgement, caution):
        section = "entry" if should_promote_recovery_candidate_to_entry(item, judgement, caution, regime=regime) else "recovery"
    else:
        section = "recovery" if strategy in {"LEADER_WATCH", "PREWATCH", "NEWLOW_RECOVER", "PREPUMP", "PULLBACK"} else "entry"
    if regime_name == "BLOCK":
        section = "late" if section == "late" else "watch"
    elif regime_name == "WEAK":
        if section == "entry":
            if is_low_zone_recovery_candidate(item, judgement, caution):
                section = "recovery" if should_keep_recovery_candidate_in_weak_regime(item, regime=regime) else "watch"
            else:
                section = "late" if strategy in ["TREND_CONT", "BREAKOUT", "CHASE"] else "watch"
        elif section == "recovery" and not should_keep_recovery_candidate_in_weak_regime(item, regime=regime):
            section = "watch"
    elif regime_name == "SIDEWAYS" and section == "entry" and strategy in ["TREND_CONT", "BREAKOUT", "CHASE"]:
        section = "late"
    floor = get_conclusion_section_floor(conclusion)
    if floor == "late":
        section = "late"
    elif floor == "watch" and section in {"entry", "recovery"}:
        section = "watch"
    elif floor == "recovery" and section == "entry":
        section = "recovery"
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"} and section == "entry":
        if not is_fast_start_entry_candidate(item, judgement, caution, regime=regime):
            section = "recovery"
    if strategy == "LEADER_WATCH" and section == "watch" and has_meaningful_watch_motion(item):
        section = "recovery"
    if section == "entry":
        allowed_entry_conclusions = {"지금 볼만함", "지금만 잠깐 볼 만함", "지금 진입 가능"}
        entry_like = is_fast_start_entry_candidate(item, judgement, caution, regime=regime)
        if floor not in {"entry", ""} and not entry_like:
            section = "recovery" if is_low_zone_recovery_candidate(item, judgement, caution) else "watch"
        elif conclusion not in allowed_entry_conclusions and not entry_like:
            section = "recovery" if is_low_zone_recovery_candidate(item, judgement, caution) else "watch"
    if section == "entry":
        weak_entry_text = (
            "사면 안 됨" in conclusion
            or "흐름만 확인" in conclusion
            or "다시 사는지 확인" in conclusion
        )
        if weak_entry_text:
            section = "recovery" if is_low_zone_recovery_candidate(item, judgement, caution) else "watch"
    if section == "entry" and trade_tier == "B":
        recovery_like = is_low_zone_recovery_candidate(item, judgement, caution)
        if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
            section = "recovery" if recovery_like else "watch"
        elif high_gap_pct <= 0.60:
            section = "late"
        elif chg_5 < 0.05:
            section = "recovery" if recovery_like else "watch"
        elif chg_30 <= 0.05 and from_low <= 0.15:
            section = "recovery" if recovery_like else "watch"
        elif leader_score < 0.80 and vol_ratio < 1.35:
            section = "recovery" if recovery_like else "watch"
        elif from_low >= 1.50 and high_gap_pct < 1.10:
            section = "recovery"
        elif vol_ratio < 1.05 and not recovery_like:
            section = "watch"
        elif strategy in {"PREWATCH", "NEWLOW_RECOVER"} and (chg_5 < 0.10 or high_gap_pct < 0.75):
            section = "recovery"
        elif strategy == "LEADER_WATCH" and (vol_ratio < 1.20 or high_gap_pct < 0.60):
            section = "recovery" if recovery_like else "watch"
    if section == "entry" and strategy == "LEADER_WATCH":
        if chg_5 < 0.10 and chg_30 < 0.20:
            section = "recovery" if is_low_zone_recovery_candidate(item, judgement, caution) else "watch"
    return section
def normalize_alert_section_key(section_key: str) -> str:
    key = str(section_key or "").strip()
    return {
        "early": "entry",
        "live": "entry",
        "restart": "recovery",
    }.get(key, key)
def get_watch_bucket_label(section_key: str) -> str:
    key = str(section_key or "").strip()
    return {
        "early": "초반형",
        "restart": "재출발형",
        "live": "실시간 강세형",
        "late": "늦음",
        "watch": "감시형",
        "entry": "초반형",
        "recovery": "재출발형",
    }.get(key, "감시형")
def get_watch_display_group(item: dict, judgement: str = "", caution: str = "", section_override: str = "", regime: dict = None) -> str:
    regime = regime or get_market_regime()
    judgement = judgement or get_watch_judgement_text(item)
    caution = caution or get_watch_caution_text(item)
    raw_key = normalize_alert_section_key(section_override or get_watch_section_key(item, judgement, caution, regime=regime))
    if raw_key in {"late", "watch"}:
        return raw_key
    strategy = str(item.get("strategy", "") or "").strip()
    snap = ensure_pre_alert_snapshot(item)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    change_pct = safe_float(item.get("change_pct", 0), 0)
    chart_profile = get_item_chart_profile(item)
    rise_pct = safe_float(chart_profile.get("chart_rise_pct", 0), 0)
    restart_ok, _restart_reason = get_restart_quality_status(item)
    if strategy in {"PULLBACK", "PREWATCH", "NEWLOW_RECOVER"}:
        if not restart_ok:
            return "watch"
        if high_gap_pct < 0.95:
            return "watch"
        if rise_pct >= 3.00 and high_gap_pct < 2.00:
            return "watch"
        if chg_5 < -0.10 and max(chg_30, from_low) < 0.45:
            return "watch"
        return "restart" if restart_ok else "watch"
    if strategy == "PREPUMP":
        if high_gap_pct >= 1.10 and from_low <= 1.60 and chg_5 >= 0.05 and not is_chart_position_extended(item):
            return "early"
        return "restart" if restart_ok else "watch"
    if strategy == "LEADER_WATCH":
        if high_gap_pct < 0.55 or not is_chart_position_good_for_live(item):
            return "late"
        if max(chg_30, from_low) >= LIVE_DISPLAY_OVERHEAT_30M_PCT and high_gap_pct < 2.10:
            return "late"
        if chg_5 >= LIVE_DISPLAY_OVERHEAT_5M_PCT and max(chg_30, from_low) >= 3.20 and high_gap_pct < 2.30:
            return "late"
        if chg_5 <= 0.05 and max(chg_30, from_low) < 0.35:
            return "watch"
        if chg_5 < LIVE_DISPLAY_MIN_5M_PCT:
            return "restart" if restart_ok and max(chg_30, from_low) >= 0.35 and high_gap_pct >= 1.05 else "watch"
        if vol_ratio < LIVE_DISPLAY_MIN_VOL_RATIO:
            return "restart" if restart_ok and max(chg_30, from_low) >= 0.40 and high_gap_pct >= 1.05 else "watch"
        if leader_score < LIVE_DISPLAY_MIN_LEADER_SCORE and vol_ratio < (LIVE_DISPLAY_MIN_VOL_RATIO + 1.20):
            return "watch"
        if high_gap_pct < LIVE_DISPLAY_MIN_HIGH_GAP_PCT:
            return "restart" if restart_ok else "watch"
        if chg_30 <= LIVE_DISPLAY_MIN_30M_PCT and from_low >= LIVE_DISPLAY_WEAK_30M_MAX_FROM_LOW_PCT and high_gap_pct <= 1.20:
            return "restart" if restart_ok else "watch"
        live_strength = 0
        if chg_5 >= LIVE_DISPLAY_MIN_5M_PCT:
            live_strength += 1
        if vol_ratio >= LIVE_DISPLAY_MIN_VOL_RATIO:
            live_strength += 1
        if leader_score >= max(1.20, LIVE_DISPLAY_MIN_LEADER_SCORE):
            live_strength += 1
        if chg_30 >= LIVE_DISPLAY_MIN_30M_PCT:
            live_strength += 1
        if max(chg_30, from_low) >= 0.55:
            live_strength += 1
        if change_pct >= 0.75:
            live_strength += 1
        if high_gap_pct >= LIVE_DISPLAY_MIN_HIGH_GAP_PCT:
            live_strength += 1
        if rise_pct >= 3.30 and high_gap_pct < 2.40:
            live_strength -= 2
        if max(chg_30, from_low) >= LIVE_DISPLAY_OVERHEAT_30M_PCT and high_gap_pct < 2.40:
            live_strength -= 2
        if chg_5 >= LIVE_DISPLAY_OVERHEAT_5M_PCT and max(chg_30, from_low) >= 3.20 and high_gap_pct < 2.60:
            live_strength -= 2
        if live_strength >= 5 and chg_5 >= LIVE_DISPLAY_MIN_5M_PCT and vol_ratio >= LIVE_DISPLAY_MIN_VOL_RATIO and high_gap_pct >= LIVE_DISPLAY_MIN_HIGH_GAP_PCT:
            return "live"
        return "restart" if restart_ok else "watch"
    if strategy in {"TREND_CONT", "BREAKOUT", "CHASE"}:
        chart_ctx = ensure_alert_chart_context(item)
        if str(chart_ctx.get("chart_state_code", "")) in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE"} and chg_30 < 0.60:
            return "restart" if restart_ok else "watch"
        if high_gap_pct < 0.55 or not is_chart_position_good_for_live(item):
            return "late"
        if max(chg_30, from_low) >= LIVE_DISPLAY_OVERHEAT_30M_PCT and high_gap_pct < 2.10:
            return "late"
        if chg_5 >= LIVE_DISPLAY_OVERHEAT_5M_PCT and max(chg_30, from_low) >= 3.20 and high_gap_pct < 2.30:
            return "late"
        if (
            chg_5 >= max(0.40, LIVE_DISPLAY_MIN_5M_PCT - 0.05)
            and max(chg_30, from_low) >= 0.45
            and vol_ratio >= 1.80
            and high_gap_pct >= 0.85
            and leader_score >= 0.85
        ):
            return "live"
        return "restart" if restart_ok else "watch"
    if strategy in {"EARLY", "PRE_BREAKOUT"}:
        return "early" if is_chart_position_good_for_live(item) else ("restart" if restart_ok else "watch")
    if raw_key == "recovery":
        return "restart" if restart_ok else "watch"
    return "early"
def get_watch_section_header(section_key: str, regime: dict = None) -> str:
    base = {
        "early": "🌱 바로 볼 초반형",
        "restart": "🔁 조금 더 볼 재출발형",
        "live": "⚡ 바로 볼 실시간 강세형",
        "late": "⛔ 지금 사면 늦은 후보",
        "watch": "👀 조금 더 볼 후보",
        "entry": "🌱 바로 볼 초반형",
        "recovery": "🔁 조금 더 볼 재출발형",
    }.get(section_key, "👀 조금 더 볼 후보")
    regime = regime or {}
    regime_name = str(regime.get("name", "NORMAL") or "NORMAL")
    normalized = normalize_alert_section_key(section_key)
    note = ""
    if regime_name == "BLOCK":
        note = "- 장 상태: BTC가 약해서 오늘은 감시 위주"
    elif regime_name == "WEAK" and normalized in {"entry", "recovery", "watch"}:
        note = "- 장 상태: 약한 장이라 초반/재출발 후보도 더 보수적으로 봄"
    elif regime_name == "SIDEWAYS" and section_key in {"early", "live", "late", "entry"}:
        note = "- 장 상태: 횡보 장이라 늦은 추격은 더 막는 중"
    elif regime_name == "STRONG_UP" and section_key in {"early", "restart", "live", "entry", "recovery"}:
        note = "- 장 상태: 장이 좋아서 초반/재출발 후보가 더 잘 올라오는 구간"
    if note:
        return base + "\n" + note
    return base
def classify_user_alert_grade(item: dict, display_key: str = "", regime: dict = None) -> str:
    section = str(display_key or get_watch_display_group(item, regime=regime)).strip()
    snap = ensure_pre_alert_snapshot(item)
    ctx = ensure_alert_chart_context(item)
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    hot_score = get_user_alert_hot_score(item)
    high_gap_pct = get_effective_high_gap_pct(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    chart_rise = safe_float(profile.get("chart_rise_pct", from_low), from_low)
    edge = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader = safe_float(item.get("leader_score", 0), 0)
    trade_tier = str(item.get("trade_tier", "B") or "B").upper().strip()
    state_code = str(ctx.get("chart_state_code", "") or "")
    htf_code = str(ctx.get("htf_trend_code", "") or "")

    if section in {"late", "watch"}:
        return "C"
    if section == "live" and state_code in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"} and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return "C"
    if section == "restart" and chg_5 < 0 and chg_30 < 0 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return "C"

    if section in {"live", "early"}:
        if (
            chg_5 >= 0.95 and chg_30 >= 0.65 and vol_ratio >= 6.0 and leader >= 1.60
            and high_gap_pct >= 1.20 and chart_rise <= 3.20 and state_code not in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}
        ):
            return "S"
        if (
            chg_5 >= 0.28 and vol_ratio >= 1.55 and leader >= 0.85 and high_gap_pct >= 0.70
            and chart_rise <= 4.10 and state_code not in {"FADE_AFTER_SPIKE", "EXTENDED"}
            and (chg_30 >= -0.15 or hot_score >= USER_ALERT_RESCUE_STRONG_HOT_SCORE)
        ):
            if trade_tier in {"S", "A"} or hot_score >= USER_ALERT_RESCUE_MIN_HOT_SCORE or edge >= 5.10 or htf_code in {"UP", "RECOVER", "NEAR_HIGH"}:
                return "A"
        if vol_ratio >= 1.15 and leader >= 0.55 and high_gap_pct >= 0.55 and state_code not in {"FADE_AFTER_SPIKE"}:
            return "B"
        return "C"

    if section == "restart":
        if is_restart_candidate_strong(item) and high_gap_pct >= 1.00 and vol_ratio >= 1.70 and leader >= 0.95 and max(chg_5, chg_30, from_low) >= 0.18 and state_code not in {"FADE_AFTER_SPIKE", "EXTENDED"}:
            if trade_tier in {"S", "A"} or hot_score >= USER_ALERT_RESCUE_MIN_HOT_SCORE or edge >= 5.10 or htf_code in {"UP", "RECOVER"}:
                return "A"
        if is_restart_candidate_strong(item) and high_gap_pct >= 0.85 and vol_ratio >= 1.15 and leader >= 0.70 and max(chg_5, chg_30, from_low) >= 0.05:
            return "B"
        return "C"

    return trade_tier if trade_tier in {"S", "A"} else "B"

def get_watch_tier_text(item: dict) -> str:
    grade = str(item.get("user_alert_grade", "") or "").upper().strip()
    if grade in {"S", "A", "B", "C"}:
        return grade
    return classify_user_alert_grade(item)
def format_alert_price_line(item: dict) -> str:
    return f"- 현재가: {fmt_price(item.get('current_price', 0))} / 상승 {safe_float(item.get('change_pct', 0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio', 0)):.2f}배"
def format_alert_flow_line(item: dict, short: bool = False) -> str:
    snap = ensure_pre_alert_snapshot(item)
    chg_5 = safe_float(snap.get('chg_5m', 0))
    chg_30 = safe_float(snap.get('chg_30m', 0))
    from_low = safe_float(snap.get('from_30m_low_pct', 0))
    high_gap = safe_float(snap.get('below_30m_high_pct', 0), 0)
    if short:
        return f"- 흐름(최근 30분 기준): 5분 {chg_5:+.2f}% / 30분 {chg_30:+.2f}% / 저점대비 {from_low:+.2f}% / {format_recent_high_gap_text(high_gap)}"
    return f"- 흐름(최근 30분 기준): 5분 {chg_5:+.2f}% / 30분 {chg_30:+.2f}% / 저점대비 {from_low:+.2f}% / {format_recent_high_gap_text(high_gap)}"
def get_alert_price_recheck_limit(section_key: str = "") -> float:
    key = str(section_key or "").strip()
    if key == "live":
        return ALERT_PRICE_RECHECK_LIVE_MAX_UP_PCT
    if key in {"restart", "recovery"}:
        return ALERT_PRICE_RECHECK_RESTART_MAX_UP_PCT
    return ALERT_PRICE_RECHECK_ENTRY_MAX_UP_PCT

def refresh_watch_item_before_alert(item: dict, section_key: str = "", regime: dict = None):
    if not isinstance(item, dict):
        return item, False, "후보 데이터 오류"
    if not ALERT_PRICE_RECHECK_ON:
        return item, True, ""
    ticker = str(item.get("ticker", "") or "").upper().strip()
    base_price = safe_float(item.get("current_price", item.get("price", 0)), 0)
    if not ticker or base_price <= 0:
        return item, True, ""
    refreshed = dict(item)
    live_price = get_price(ticker)
    if live_price <= 0:
        return refreshed, True, ""
    drift_pct = ((live_price - base_price) / base_price) * 100.0 if base_price > 0 else 0.0
    refreshed["current_price"] = live_price
    refreshed["price"] = live_price
    refreshed["price_before_alert"] = base_price
    refreshed["price_drift_pct"] = round(drift_pct, 2)
    df = get_ohlcv(ticker, "minute1")
    df4h = get_ohlcv(ticker, ALERT_HTF_INTERVAL)
    if df is not None and len(df) >= 8:
        refreshed.update(compute_chart_position_profile(df, live_price, lookback=CHART_POS_LOOKBACK_1M))
        refreshed["pre_alert_snapshot"] = compute_pre_alert_snapshot_from_df(df, current_price=live_price)
    else:
        refreshed["pre_alert_snapshot"] = compute_pre_alert_snapshot(ticker, current_price=live_price)
    refreshed["alert_chart_context"] = compute_alert_chart_context_from_df(df, df4h=df4h, current_price=live_price)
    high_gap_pct = get_reference_high_gap_pct(refreshed)
    snap = ensure_pre_alert_snapshot(refreshed)
    if drift_pct >= ALERT_PRICE_RECHECK_SKIP_UP_PCT:
        return refreshed, False, f"알림 직전 가격이 너무 빨리 올라가서 제외 (+{drift_pct:.2f}%)"
    limit = get_alert_price_recheck_limit(section_key)
    if drift_pct > limit and section_key in {"early", "entry", "live", "restart", "recovery"}:
        return refreshed, False, f"알림 직전 가격이 이미 벌어져서 제외 (+{drift_pct:.2f}%)"
    if section_key == "live" and drift_pct > ALERT_PRICE_RECHECK_LIVE_NEAR_HIGH_MAX_UP_PCT and 0 <= high_gap_pct <= 1.15:
        return refreshed, False, f"실시간 강세인데 알림 직전 가격이 더 올라 늦어져서 제외 (+{drift_pct:.2f}%)"
    if section_key in {"restart", "recovery"} and drift_pct > ALERT_PRICE_RECHECK_RESTART_NEAR_HIGH_MAX_UP_PCT and 0 <= high_gap_pct <= 1.15:
        return refreshed, False, f"재출발 후보인데 알림 직전 가격이 벌어져서 제외 (+{drift_pct:.2f}%)"
    if section_key in {"restart", "recovery"} and not is_restart_candidate_strong(refreshed):
        _ok, restart_reason = get_restart_quality_status(refreshed)
        return refreshed, False, restart_reason or "재출발 힘이 약해져서 제외"
    if section_key == "live" and 0 <= high_gap_pct <= 0.60:
        return refreshed, False, "실시간 강세로 보기엔 이미 고점이 너무 가까워서 제외"
    if section_key == "live" and safe_float(snap.get("chg_5m", 0), 0) < 0.18 and safe_float(refreshed.get("vol_ratio", 0), 0) < 3.20:
        return refreshed, False, "실시간 강세로 보기엔 방금 힘이 약해져서 제외"
    if section_key in {"restart", "recovery"} and safe_float(snap.get("chg_5m", 0), 0) < 0.30 and max(safe_float(snap.get("chg_30m", 0), 0), safe_float(snap.get("from_30m_low_pct", 0), 0)) < 0.95:
        return refreshed, False, "재출발 후보로 보기엔 방금 반등 힘이 약해서 제외"
    regime = regime or get_market_regime()
    refreshed_group = get_watch_display_group(refreshed, regime=regime)
    if section_key == "live" and refreshed_group != "live":
        return refreshed, False, "실시간 강세 초입으로 보기 어려워져 제외"
    if section_key in {"early", "entry"} and refreshed_group != "early":
        return refreshed, False, "초반 선점형으로 보기 어려워져 제외"
    if section_key in {"restart", "recovery"} and refreshed_group != "restart":
        return refreshed, False, "재출발 후보로 보기 어려워져 제외"
    return refreshed, True, ""
def get_alert_no_chase_pct(item: dict) -> float:
    strategy = str(item.get("strategy", "") or "").strip().upper()
    tier = str(item.get("trade_tier", "B") or "B").upper().strip()
    high_gap_pct = get_reference_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    base_map = {
        "EARLY": 0.60,
        "PREPUMP": 0.55,
        "PULLBACK": 0.45,
        "PRE_BREAKOUT": 0.40,
        "TREND_CONT": 0.35,
        "BREAKOUT": 0.28,
        "CHASE": 0.18,
        "LEADER_WATCH": 0.35,
        "PREWATCH": 0.35,
        "NEWLOW_RECOVER": 0.30,
    }
    pct = base_map.get(strategy, 0.40)
    if tier == "S":
        pct += 0.08
    elif tier == "B":
        pct -= 0.05
    if 0 <= high_gap_pct <= 0.60:
        pct -= 0.08
    elif high_gap_pct >= 1.40:
        pct += 0.05
    if vol_ratio >= 4.5:
        pct += 0.03
    return round(max(0.12, min(pct, 0.95)), 2)
def build_normal_alert_action_lines(item: dict):
    current_price = safe_float(item.get("current_price", 0), 0)
    if current_price <= 0:
        return []
    no_chase_pct = get_alert_no_chase_pct(item)
    no_chase_price = current_price * (1 + no_chase_pct / 100.0)
    return [f"- 추격 금지선: {fmt_price(no_chase_price)} (+{no_chase_pct:.2f}%)"]
def should_show_trade_plan_in_alert(item: dict) -> bool:
    judgement = get_watch_judgement_text(item)
    caution = get_watch_caution_text(item)
    return get_watch_alert_detail_level(item, judgement, caution) == "strong"
def build_alert_trade_plan_lines(item: dict):
    if not should_show_trade_plan_in_alert(item):
        return []
    current_price = safe_float(item.get("current_price", 0), 0)
    stop_loss_pct = safe_float(item.get("stop_loss_pct", 0), 0)
    take_profit_pct = safe_float(item.get("take_profit_pct", 0), 0)
    if current_price <= 0:
        return []
    stop_price = current_price * (1 + stop_loss_pct / 100.0) if stop_loss_pct else 0.0
    target_price = current_price * (1 + take_profit_pct / 100.0) if take_profit_pct else 0.0
    no_chase_pct = get_alert_no_chase_pct(item)
    no_chase_price = current_price * (1 + no_chase_pct / 100.0)
    lines = [f"- 진입가(참고): {fmt_price(current_price)}"]
    if stop_price > 0:
        lines.append(f"- 손절가: {fmt_price(stop_price)} ({fmt_pct(stop_loss_pct)})")
    if target_price > 0:
        lines.append(f"- 1차 목표가: {fmt_price(target_price)} ({fmt_pct(take_profit_pct)})")
    lines.append(f"- 추격 금지선: {fmt_price(no_chase_price)} (+{no_chase_pct:.2f}%)")
    return lines
def format_watch_alert_line(item: dict, order: int = 1, alert_type: str = "new", reason_text: str = "", section_override: str = "") -> str:
    tags = item.get("pattern_tags", [])
    tag_text = ", ".join(tags[:3]) if tags else ""
    icon = get_watch_item_icon(item, alert_type=alert_type)
    caution = get_watch_caution_text(item)
    judgement = get_watch_judgement_text(item)
    detail_level = get_watch_alert_detail_level(item, judgement, caution)
    bucket_key = section_override or get_watch_display_group(item, judgement, caution)
    normalized_key = normalize_alert_section_key(bucket_key)
    item["user_alert_grade"] = classify_user_alert_grade(item, display_key=normalized_key)
    conclusion = get_watch_display_conclusion(item, judgement, caution, section_override=normalized_key)
    one_line = get_watch_one_line_text(item, caution, reason_text)
    bucket_label = get_watch_bucket_label(bucket_key)
    strategy = str(item.get("strategy", "") or "").strip()
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
        if bucket_key in {"early", "live"} and conclusion in {"지금 매수 금지", "지금은 구경만"}:
            conclusion = "지금 볼만함"
        elif bucket_key == "restart" and conclusion in {"지금 매수 금지", "지금은 구경만"}:
            conclusion = "다시 사는지 확인 먼저"
    if bucket_key in {"restart", "late", "watch"}:
        summary = one_line
        if bucket_key == "restart" and not summary:
            summary = "한 번 움직인 뒤 다시 가는지 먼저 확인"
        elif bucket_key == "late" and not summary:
            summary = "이미 올라온 자리라 지금 추격은 금지"
        elif bucket_key == "watch" and not summary:
            summary = "지금은 흐름만 확인"
        lines = [
            highlight_line(order, item.get('ticker', '?'), icon),
            f"- 유형: {bucket_label}",
            f"- 결론: {conclusion}",
            format_alert_price_line(item),
            format_alert_flow_line(item, short=False),
            format_chart_context_line(item),
            f"- 요약: {summary}",
        ]
        if alert_type in ["upgrade", "renotice"] and reason_text:
            reason_norm = reason_text.replace(" ", "").strip()
            summary_norm = summary.replace(" ", "").strip()
            if reason_norm and reason_norm != summary_norm:
                lines.append(f"- 변화: {reason_text}")
        return "\n".join(lines)
    lines = [
        highlight_line(order, item.get('ticker', '?'), icon),
        f"- 유형: {bucket_label}",
        f"- 결론: {conclusion}",
        format_alert_price_line(item),
        f"- 전략: {item['strategy_label']} / 등급 {get_watch_tier_text(item)} / 주도도 {safe_float(item.get('leader_score',0)):.2f}",
        format_alert_flow_line(item, short=(detail_level == 'normal')),
        format_chart_context_line(item),
    ]
    if detail_level == "strong":
        lines.extend(build_alert_trade_plan_lines(item))
        if tag_text:
            lines.append(f"- 구조: {tag_text}")
    else:
        lines.extend(build_normal_alert_action_lines(item))
    if alert_type in ["upgrade", "renotice"] and reason_text:
        reason_norm = reason_text.replace(" ", "").strip()
        one_line_norm = one_line.replace(" ", "").strip() if one_line else ""
        if reason_norm and reason_norm != one_line_norm:
            lines.append(f"- 변화: {reason_text}")
    elif detail_level == "strong" and caution:
        lines.append(f"- 주의: {caution}")
    elif detail_level == "normal" and reason_text:
        lines.append(f"- 참고: {reason_text}")
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
        "이번 핵심 변경",
        "- /missed_reset 실제 명령 함수 누락을 복구해서 시작 오류 가능성을 막음",
        "- 스캔 heartbeat / watchdog 기준을 완화하고 긴 스캔 중에도 heartbeat를 계속 찍게 보강",
        "- 오래 걸리는 유니버스/캐시 생성 중 watchdog 오작동으로 재시작되던 문제를 줄이도록 조정",
        "- 1차 빠른 스캔은 더 넓게 보되, 무거운 계산은 상위 후보에만 하도록 조정",
        "- 같은 코인 반복 알림과 30분 확인 추적은 기존 정리 방향 유지",
        "- 숫자와 차트가 덜 어긋나게 알림 직전 재계산 / 차트상태 / 4시간 흐름 표시는 유지",
        "",
        "자세한 설명은 /info",
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
def persist_price_check_state():
    try:
        save_json_file(PRICE_CHECK_REFS_FILE, PRICE_CHECK_REFS)
        save_json_file(PRICE_CHECK_SCHEDULED_FILE, PRICE_CHECK_SCHEDULED)
    except Exception as e:
        print(f"[가격 확인 상태 저장 오류] {e}")
def load_price_check_state():
    global PRICE_CHECK_REFS, PRICE_CHECK_SCHEDULED
    refs = load_json_file(PRICE_CHECK_REFS_FILE, {})
    sched = load_json_file(PRICE_CHECK_SCHEDULED_FILE, {})
    PRICE_CHECK_REFS = refs if isinstance(refs, dict) else {}
    PRICE_CHECK_SCHEDULED = sched if isinstance(sched, dict) else {}
    cleanup_price_check_refs(time.time())
def persist_recent_candidate_alerts():
    try:
        save_json_file(RECENT_CANDIDATES_FILE, RECENT_CANDIDATE_ALERTS)
    except Exception as e:
        print(f"[최근 후보 저장 오류] {e}")
def load_recent_candidate_alerts():
    rows = load_json_file(RECENT_CANDIDATES_FILE, [])
    if isinstance(rows, list) and rows:
        return rows
    # 예전 버전에서 상대경로로 저장된 파일이 있으면 한 번 가져와서 절대경로 파일로 옮겨준다.
    legacy_rows = load_json_file(LEGACY_RECENT_CANDIDATES_FILE, [])
    if isinstance(legacy_rows, list) and legacy_rows:
        try:
            save_json_file(RECENT_CANDIDATES_FILE, legacy_rows)
        except Exception:
            pass
        return legacy_rows
    return rows if isinstance(rows, list) else []

def persist_market_pulse_state():
    try:
        save_json_file(MARKET_PULSE_STATE_FILE, market_pulse_state if isinstance(market_pulse_state, dict) else {})
    except Exception as e:
        print(f"[시장 순위 저장 오류] {e}")

def load_market_pulse_state():
    global market_pulse_state
    data = load_json_file(MARKET_PULSE_STATE_FILE, {})
    market_pulse_state = data if isinstance(data, dict) else {}
def persist_missed_review_state():
    try:
        save_json_file(MISSED_REVIEW_QUEUE_FILE, missed_review_queue)
        save_json_file(MISSED_REVIEW_LOG_FILE, missed_review_logs)
        save_json_file(MISSED_REVIEW_META_FILE, missed_review_meta if isinstance(missed_review_meta, dict) else {})
    except Exception as e:
        print(f"[놓친 코인 상태 저장 오류] {e}")

def trim_missed_review_logs():
    global missed_review_logs
    rows = missed_review_logs if isinstance(missed_review_logs, list) else []
    kept = []
    for row in rows:
        if isinstance(row, dict) and row.get("ticker"):
            kept.append(row)
    missed_review_logs = kept[-MISSED_REVIEW_MAX_LOGS:]

def cleanup_missed_review_queue(now_ts: float = None):
    global missed_review_queue
    now_ts = safe_float(now_ts, time.time())
    fresh = {}
    if not isinstance(missed_review_queue, dict):
        missed_review_queue = {}
        return
    for ticker, row in missed_review_queue.items():
        if not isinstance(row, dict):
            continue
        saved_at = safe_float(row.get("saved_at", 0), 0)
        review_ts = safe_float(row.get("review_ts", 0), 0)
        if saved_at <= 0:
            continue
        if now_ts - saved_at > max(MISSED_REVIEW_DELAY_SEC * 3, 4 * 3600):
            continue
        if review_ts <= 0:
            row["review_ts"] = saved_at + MISSED_REVIEW_DELAY_SEC
        fresh[str(ticker).upper().strip()] = row
    if len(fresh) > MISSED_REVIEW_MAX_QUEUE:
        ordered = sorted(fresh.items(), key=lambda kv: safe_float(kv[1].get("saved_at", 0), 0), reverse=True)
        fresh = dict(ordered[:MISSED_REVIEW_MAX_QUEUE])
    missed_review_queue = fresh

def load_missed_review_state():
    global missed_review_queue, missed_review_logs, missed_review_meta
    queue = load_json_file(MISSED_REVIEW_QUEUE_FILE, {})
    logs = load_json_file(MISSED_REVIEW_LOG_FILE, [])
    meta = load_json_file(MISSED_REVIEW_META_FILE, {})
    missed_review_queue = queue if isinstance(queue, dict) else {}
    missed_review_logs = logs if isinstance(logs, list) else []
    missed_review_meta = meta if isinstance(meta, dict) else {}
    cleanup_missed_review_queue(time.time())
    trim_missed_review_logs()

def clear_missed_review_state():
    global missed_review_queue, missed_review_logs, missed_review_meta, last_missed_review_alert_ts
    missed_review_queue = {}
    missed_review_logs = []
    missed_review_meta = {}
    last_missed_review_alert_ts = 0
    persist_missed_review_state()

def save_missed_review_log(row: dict):
    if not isinstance(row, dict) or not row.get("ticker"):
        return
    missed_review_logs.append(row)
    trim_missed_review_logs()
    persist_missed_review_state()

def get_hour_start_ts(ts: float = None) -> float:
    ts = safe_float(ts, time.time())
    dt = datetime.fromtimestamp(ts, ZoneInfo(TIMEZONE))
    hour_dt = datetime(dt.year, dt.month, dt.day, dt.hour, 0, 0, tzinfo=ZoneInfo(TIMEZONE))
    return hour_dt.timestamp()

def format_hour_window_text(start_ts: float, end_ts: float) -> str:
    try:
        start_dt = datetime.fromtimestamp(float(start_ts), ZoneInfo(TIMEZONE))
        end_dt = datetime.fromtimestamp(float(end_ts), ZoneInfo(TIMEZONE))
        if start_dt.date() == end_dt.date():
            return f"{start_dt.strftime('%m-%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"
        return f"{start_dt.strftime('%m-%d %H:%M')} ~ {end_dt.strftime('%m-%d %H:%M')}"
    except Exception:
        return "시간 구간 확인 실패"

def process_missed_review_hourly_report(now_ts: float = None):
    global missed_review_meta
    if not MISSED_REVIEW_HOURLY_ALERT_ON:
        return
    now_ts = safe_float(now_ts, time.time())
    current_hour_start = get_hour_start_ts(now_ts)
    if not isinstance(missed_review_meta, dict):
        missed_review_meta = {}
    last_cutoff_ts = safe_float(missed_review_meta.get("last_hourly_cutoff_ts", 0), 0)
    if last_cutoff_ts <= 0:
        missed_review_meta["last_hourly_cutoff_ts"] = current_hour_start
        persist_missed_review_state()
        return
    if current_hour_start <= last_cutoff_ts:
        return
    rows = []
    for row in missed_review_logs:
        if not isinstance(row, dict):
            continue
        logged_at = safe_float(row.get("logged_at", 0), 0)
        if last_cutoff_ts <= logged_at < current_hour_start:
            rows.append(row)
    rows.sort(key=lambda x: safe_float(x.get("logged_at", 0), 0), reverse=True)
    if not rows:
        missed_review_meta["last_hourly_cutoff_ts"] = current_hour_start
        persist_missed_review_state()
        return
    summary = summarize_missed_review_logs(rows)
    lines = [
        "🧪 놓친 좋은 코인 자동 요약",
        f"- 구간: {format_hour_window_text(last_cutoff_ts, current_hour_start)}",
        f"- 탐색 놓침 {summary.get('탐색 놓침', 0)}건 / 승격 놓침 {summary.get('승격 놓침', 0)}건 / 분류 놓침 {summary.get('분류 놓침', 0)}건",
        "",
        f"[최근 {min(len(rows), MISSED_REVIEW_HOURLY_MAX_ITEMS)}건]",
    ]
    for idx, row in enumerate(rows[:MISSED_REVIEW_HOURLY_MAX_ITEMS], start=1):
        lines.append("")
        lines.append(format_missed_review_log_block(row, idx=idx))
    remain = len(rows) - MISSED_REVIEW_HOURLY_MAX_ITEMS
    if remain > 0:
        lines.extend(["", f"- 그외 {remain}건은 /missed 에서 더 볼 수 있어"])
    sent = send("\n".join(lines))
    if sent:
        missed_review_meta["last_hourly_cutoff_ts"] = current_hour_start
        persist_missed_review_state()

def has_recent_same_missed_review(row: dict, lookback_sec: int = 3 * 3600) -> bool:
    ticker = str(row.get("ticker", "") or "").upper().strip()
    miss_type = str(row.get("miss_type", "") or "")
    saved_at = safe_float(row.get("logged_at", time.time()), time.time())
    for prev in reversed(missed_review_logs[-30:]):
        if not isinstance(prev, dict):
            continue
        if str(prev.get("ticker", "") or "").upper().strip() != ticker:
            continue
        if str(prev.get("miss_type", "") or "") != miss_type:
            continue
        prev_ts = safe_float(prev.get("logged_at", 0), 0)
        if prev_ts > 0 and saved_at - prev_ts <= lookback_sec:
            return True
    return False

def save_runtime_settings():
    data = {
        "auto_buy": bool(AUTO_BUY),
    }
    save_json_file(RUNTIME_SETTINGS_FILE, data)
    return True
def load_runtime_settings():
    global AUTO_BUY
    data = load_json_file(RUNTIME_SETTINGS_FILE, {})
    if not isinstance(data, dict):
        AUTO_BUY = False
        return False
    AUTO_BUY = bool(data.get("auto_buy", AUTO_BUY))
    return True

def force_autobuy_off_on_startup():
    global AUTO_BUY
    changed = bool(AUTO_BUY)
    AUTO_BUY = False
    save_runtime_settings()
    return changed
def save_config():
    return save_runtime_settings()
def save_settings():
    return save_runtime_settings()
RECENT_CANDIDATE_ALERTS = load_recent_candidate_alerts()
trade_logs = load_json_file(LOG_FILE, [])
CLOSE_TYPES = {"TP", "TRAIL_TP", "BREAKEVEN", "STOP", "TIME_STOP", "SCENARIO_FAIL", "EARLY_FAIL"}
def save_logs():
    save_json_file(LOG_FILE, trade_logs)
def add_log(item: dict):
    trade_logs.append(item)
    save_logs()
def mark_position_state_dirty():
    global position_state_dirty
    position_state_dirty = True
def flush_position_state(force=False):
    global position_state_dirty, last_position_save_time
    now_ts = time.time()
    if (not force) and (not position_state_dirty):
        return False
    if (not force) and (now_ts - last_position_save_time < POSITION_SAVE_MIN_INTERVAL_SEC):
        return False
    save_json_file(POSITIONS_FILE, active_positions)
    position_state_dirty = False
    last_position_save_time = now_ts
    return True
def save_positions(force=True):
    if force:
        return flush_position_state(force=True)
    mark_position_state_dirty()
    return False
def load_positions():
    global active_positions, position_state_dirty, last_position_save_time
    active_positions = load_json_file(POSITIONS_FILE, {})
    position_state_dirty = False
    last_position_save_time = time.time()
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
def direction_flip_count(df, n=12):
    try:
        recent = df.tail(n)
        closes = [float(x) for x in recent["close"]]
        flips = 0
        for i in range(2, len(closes)):
            d1 = closes[i - 1] - closes[i - 2]
            d2 = closes[i] - closes[i - 1]
            if abs(d1) < 1e-12 or abs(d2) < 1e-12:
                continue
            if d1 * d2 < 0:
                flips += 1
        return flips
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
def get_btc_multi_tf_snapshot():
    snapshot = {}
    configs = [
        ("short", "minute10", 6),
        ("mid", "minute60", 6),
        ("day", "day", 3),
    ]
    for name, interval, bars in configs:
        try:
            df = get_ohlcv("BTC", interval)
            if df is None or len(df) < max(20, bars + 1):
                continue
            price = safe_float(df["close"].iloc[-1], 0)
            if price <= 0:
                continue
            change_pct = get_recent_change_pct(df, bars)
            ma20v = ma(df, 20)
            snapshot[name] = {
                "interval": interval,
                "price": price,
                "change_pct": change_pct,
                "ma20": ma20v,
                "above_ma20": (ma20v > 0 and price >= ma20v),
            }
        except Exception:
            continue
    return snapshot
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
    snapshot = get_btc_multi_tf_snapshot()
    if not snapshot:
        return {
            "name": "UNKNOWN",
            "label": "unknown",
            "btc_change_pct": 0.0,
            "allow_auto_buy": True,
            "allow_breakout": True,
            "watch_entry_allowed": True,
            "watch_recovery_allowed": True,
            "message": "시장 상태 판단 실패",
            "watch_message": "시장 상태를 아직 못 읽었어",
            "short_change_pct": 0.0,
            "mid_change_pct": 0.0,
            "day_change_pct": 0.0,
        }
    short = snapshot.get("short", {})
    mid = snapshot.get("mid", {})
    day = snapshot.get("day", {})
    short_change = safe_float(short.get("change_pct", 0), 0)
    mid_change = safe_float(mid.get("change_pct", short_change), short_change)
    day_change = safe_float(day.get("change_pct", mid_change), mid_change)
    weak_count = 0
    strong_count = 0
    if short:
        if short_change <= BTC_SHORT_WEAK_PCT:
            weak_count += 1
        if short_change >= 0.80 and bool(short.get("above_ma20", False)):
            strong_count += 1
    if mid:
        if mid_change <= BTC_MID_WEAK_PCT:
            weak_count += 1
        if not bool(mid.get("above_ma20", False)):
            weak_count += 1
        if mid_change >= REGIME_STRONG_UP_PCT and bool(mid.get("above_ma20", False)):
            strong_count += 1
    if day:
        if day_change <= BTC_DAY_WEAK_PCT:
            weak_count += 1
        if not bool(day.get("above_ma20", False)):
            weak_count += 1
        if day_change >= 1.20 and bool(day.get("above_ma20", False)):
            strong_count += 1
    if day_change <= BTC_DAY_BLOCK_PCT or mid_change <= BTC_STRONG_BLOCK_PCT:
        return {
            "name": "BLOCK",
            "label": "multi-tf",
            "btc_change_pct": mid_change,
            "allow_auto_buy": False,
            "allow_breakout": False,
            "watch_entry_allowed": False,
            "watch_recovery_allowed": False,
            "message": "BTC가 여러 구간에서 약해서 신규 진입을 쉬는 장",
            "watch_message": "장 약함 · 감시만 하고 진입은 쉬기",
            "short_change_pct": short_change,
            "mid_change_pct": mid_change,
            "day_change_pct": day_change,
        }
    if weak_count >= 3 or (mid_change <= REGIME_WEAK_MAX_ABS_PCT and day_change <= 0):
        return {
            "name": "WEAK",
            "label": "multi-tf",
            "btc_change_pct": mid_change,
            "allow_auto_buy": True,
            "allow_breakout": False,
            "watch_entry_allowed": False,
            "watch_recovery_allowed": True,
            "message": "약한 장이라 저점권 후보도 보수적으로 봐야 해",
            "watch_message": "약한 장 · 회복 후보까지만 보고 진입 승격은 더 보수적으로",
            "short_change_pct": short_change,
            "mid_change_pct": mid_change,
            "day_change_pct": day_change,
        }
    if abs(short_change) <= 0.45 and abs(mid_change) <= 0.85:
        return {
            "name": "SIDEWAYS",
            "label": "multi-tf",
            "btc_change_pct": mid_change,
            "allow_auto_buy": True,
            "allow_breakout": not REGIME_BLOCK_BREAKOUT_ON_SIDEWAYS,
            "watch_entry_allowed": True,
            "watch_recovery_allowed": True,
            "message": "횡보 장이라 급한 추격은 조심해야 해",
            "watch_message": "횡보 장 · 늦은 추격은 막고 저점권 회복만 더 엄격하게 보기",
            "short_change_pct": short_change,
            "mid_change_pct": mid_change,
            "day_change_pct": day_change,
        }
    if strong_count >= 3:
        return {
            "name": "STRONG_UP",
            "label": "multi-tf",
            "btc_change_pct": mid_change,
            "allow_auto_buy": True,
            "allow_breakout": True,
            "watch_entry_allowed": True,
            "watch_recovery_allowed": True,
            "message": "여러 시간대가 같이 좋아서 강한 상승 장으로 보는 중",
            "watch_message": "장 좋음 · 회복 후보가 진입 후보로 올라가기 쉬운 구간",
            "short_change_pct": short_change,
            "mid_change_pct": mid_change,
            "day_change_pct": day_change,
        }
    return {
        "name": "NORMAL",
        "label": "multi-tf",
        "btc_change_pct": mid_change,
        "allow_auto_buy": True,
        "allow_breakout": True,
        "watch_entry_allowed": True,
        "watch_recovery_allowed": True,
        "message": "무난한 장이라 기본 기준대로 보면 돼",
        "watch_message": "보통 장 · 기준대로 후보를 보면 돼",
        "short_change_pct": short_change,
        "mid_change_pct": mid_change,
        "day_change_pct": day_change,
    }
def analyze_btc_flow():
    regime = get_market_regime()
    name = regime.get("name", "UNKNOWN")
    if name == "BLOCK":
        state = "🚨 강한 하락"
    elif name == "WEAK":
        state = "⚠️ 약한 장"
    elif name == "SIDEWAYS":
        state = "😐 횡보"
    elif name == "STRONG_UP":
        state = "🔥 강한 상승"
    else:
        state = "👍 보통"
    short_change = safe_float(regime.get("short_change_pct", 0), 0)
    mid_change = safe_float(regime.get("mid_change_pct", 0), 0)
    day_change = safe_float(regime.get("day_change_pct", 0), 0)
    return f"""
📊 BTC 리포트 (다중 시간대 기준)
📌 상태: {state}
- 단기: {short_change:+.2f}%
- 중기: {mid_change:+.2f}%
- 일봉: {day_change:+.2f}%
한줄 해석:
{regime.get('message', '시장 상태 확인 중')}
후보 해석:
{regime.get('watch_message', '기본 기준대로 보면 돼')}
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
    current_price = safe_float(signal.get("current_price", 0))
    reference_high = safe_float(signal.get("reference_high", 0))
    near_high_gap_pct = 999.0
    if current_price > 0 and reference_high > 0:
        near_high_gap_pct = ((reference_high - current_price) / reference_high) * 100.0
    high_near_cap_b = (
        strategy in ["EARLY", "PREPUMP", "PULLBACK", "TREND_CONT"]
        and 0 <= near_high_gap_pct <= 0.70
    )
    if high_near_cap_b:
        return "B"
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
        if leader_score >= LEADER_BASE_MIN_FOR_PRIORITY or strategy in ["TREND_CONT", "PRE_BREAKOUT", "EARLY"]:
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
def mark_main_loop_heartbeat(stage: str = "", error: str = ""):
    global last_main_loop_heartbeat_ts, last_main_loop_stage, last_main_loop_error
    last_main_loop_heartbeat_ts = time.time()
    if stage:
        last_main_loop_stage = str(stage)
    if error:
        last_main_loop_error = str(error)

def get_scan_runtime_health(now_ts: float = None) -> dict:
    now_ts = safe_float(now_ts, time.time())
    loop_age = int(now_ts - last_main_loop_heartbeat_ts) if last_main_loop_heartbeat_ts else None
    cache_age = int(now_ts - shared_market_cache_time) if shared_market_cache_time else None
    snapshot_age = int(now_ts - last_scan_debug_snapshot_time) if last_scan_debug_snapshot_time else None
    universe_age = int(now_ts - market_universe_time) if market_universe_time else None
    cache_stale = cache_age is None or cache_age >= SCAN_CACHE_WARN_SEC
    snapshot_stale = snapshot_age is None or snapshot_age >= SCAN_CACHE_WARN_SEC
    loop_warn = loop_age is None or loop_age >= SCAN_HEARTBEAT_WARN_SEC
    loop_hard_stale = loop_age is None or loop_age >= SCAN_HEARTBEAT_HARD_RESTART_SEC
    cache_hard_stale = cache_age is not None and cache_age >= SCAN_CACHE_HARD_RESET_SEC
    if cache_age is None and snapshot_age is not None and snapshot_age >= SCAN_CACHE_HARD_RESET_SEC:
        cache_hard_stale = True
    return {
        "loop_age": loop_age,
        "cache_age": cache_age,
        "snapshot_age": snapshot_age,
        "universe_age": universe_age,
        "cache_stale": cache_stale,
        "snapshot_stale": snapshot_stale,
        "loop_warn": loop_warn,
        "loop_hard_stale": loop_hard_stale,
        "cache_hard_stale": cache_hard_stale,
        "stage": last_main_loop_stage,
        "error": last_main_loop_error,
        "reset_reason": scan_runtime_reset_reason,
        "reset_count": scan_runtime_reset_count,
        "cache_rows": len(shared_market_cache) if isinstance(shared_market_cache, dict) else 0,
        "universe_rows": len(market_universe) if isinstance(market_universe, list) else 0,
        "universe_stats": dict(last_universe_build_stats) if isinstance(last_universe_build_stats, dict) else {},
        "cache_stats": dict(last_cache_build_stats) if isinstance(last_cache_build_stats, dict) else {},
    }

def build_scan_health_warning_text(now_ts: float = None) -> str:
    info = get_scan_runtime_health(now_ts)
    lines = []
    if info["loop_warn"]:
        age = info["loop_age"]
        if age is None:
            lines.append("⚠️ 실시간 스캔 루프 시작 기록이 아직 없음")
        else:
            lines.append(f"⚠️ 실시간 스캔이 {age}초째 새로 안 돈 것 같아")
    if info["cache_stale"] or info["snapshot_stale"]:
        cache_txt = f"캐시 {info['cache_age']}초 전" if info["cache_age"] is not None else "캐시 없음"
        snap_txt = f"스냅샷 {info['snapshot_age']}초 전" if info["snapshot_age"] is not None else "스냅샷 없음"
        lines.append(f"⚠️ 후보 데이터가 오래됐어 ({cache_txt} / {snap_txt})")
    c_stats = info.get("cache_stats") or {}
    u_stats = info.get("universe_stats") or {}
    if info["loop_warn"] or info["cache_stale"]:
        if c_stats:
            lines.append(f"ℹ️ 최근 캐시 갱신: 시도 {int(safe_float(c_stats.get('tried',0),0))} / 성공 {int(safe_float(c_stats.get('success',0),0))} / 유지 {int(safe_float(c_stats.get('kept',0),0))}")
        if u_stats:
            lines.append(f"ℹ️ 최근 유니버스 갱신: 시도 {int(safe_float(u_stats.get('tried',0),0))} / 성공 {int(safe_float(u_stats.get('success',0),0))} / 현재 {int(safe_float(u_stats.get('selected',0),0))}")
        if info.get("stage"):
            lines.append(f"ℹ️ 최근 스캔 단계: {info['stage']}")
    refresh_info = get_market_refresh_status(now_ts)
    if refresh_info.get("in_progress"):
        lines.append(f"🔄 시장 갱신은 백그라운드에서 도는 중 ({refresh_info.get('age_sec', 0)}초 / 사유: {refresh_info.get('reason','loop')})")
    if info.get("reset_reason"):
        lines.append(f"🛠 최근 자동 복구 사유: {info['reset_reason']}")
    return "\n".join(lines).strip()

def reset_scan_runtime_state(reason: str = ""):
    global shared_market_cache, shared_market_cache_time, market_universe, market_universe_time
    global last_scan_debug_snapshot, last_scan_debug_snapshot_time, last_scan_debug_note
    global scan_runtime_reset_reason, scan_runtime_reset_count
    global shared_cache_cursor, market_universe_cursor, shared_cache_row_times
    global market_refresh_in_progress, market_refresh_started_ts, last_market_refresh_result
    shared_market_cache = {}
    shared_market_cache_time = 0
    market_universe = []
    market_universe_time = 0
    shared_cache_cursor = 0
    market_universe_cursor = 0
    shared_cache_row_times = {}
    last_scan_debug_snapshot = []
    last_scan_debug_snapshot_time = 0
    last_scan_debug_note = ""
    market_refresh_in_progress = False
    market_refresh_started_ts = 0.0
    last_market_refresh_result = "초기화"
    scan_runtime_reset_reason = str(reason or "스캔 런타임 초기화")
    scan_runtime_reset_count += 1
    print(f"[scan_runtime_reset] {scan_runtime_reset_reason}", flush=True)

def get_market_refresh_status(now_ts: float = None) -> dict:
    now_ts = safe_float(now_ts, time.time())
    age_sec = int(now_ts - market_refresh_started_ts) if market_refresh_in_progress and market_refresh_started_ts else 0
    last_finish_age_sec = int(now_ts - last_market_refresh_finish_ts) if last_market_refresh_finish_ts else None
    return {
        "in_progress": bool(market_refresh_in_progress),
        "age_sec": age_sec,
        "started_ts": safe_float(market_refresh_started_ts, 0),
        "requested_ts": safe_float(last_market_refresh_request_ts, 0),
        "finished_ts": safe_float(last_market_refresh_finish_ts, 0),
        "last_finish_age_sec": last_finish_age_sec,
        "reason": str(last_market_refresh_reason or ""),
        "result": str(last_market_refresh_result or "대기"),
    }

def _market_refresh_worker(force: bool = False, reason: str = "loop"):
    global market_refresh_in_progress, market_refresh_started_ts
    global last_market_refresh_finish_ts, last_market_refresh_result
    try:
        mark_main_loop_heartbeat(stage=f"market_refresh_{reason}")
    except Exception:
        pass
    try:
        universe_before = len(market_universe) if isinstance(market_universe, list) else 0
        cache_before = len(shared_market_cache) if isinstance(shared_market_cache, dict) else 0
        get_market_universe(force=force)
        cache = build_shared_market_cache(force=force)
        universe_after = len(market_universe) if isinstance(market_universe, list) else 0
        cache_after = len(cache) if isinstance(cache, dict) else 0
        last_market_refresh_result = f"완료 / 유니버스 {universe_before}->{universe_after} / 캐시 {cache_before}->{cache_after}"
    except Exception as e:
        last_market_refresh_result = f"실패 / {e}"
        print(f"[market_refresh 오류] {e}", flush=True)
    finally:
        last_market_refresh_finish_ts = time.time()
        market_refresh_in_progress = False
        market_refresh_started_ts = 0.0

def request_market_refresh(force: bool = False, reason: str = "loop") -> bool:
    global market_refresh_in_progress, market_refresh_started_ts
    global last_market_refresh_request_ts, last_market_refresh_reason, last_market_refresh_result
    with market_refresh_lock:
        if market_refresh_in_progress:
            if force and reason:
                last_market_refresh_reason = str(reason)
            return False
        market_refresh_in_progress = True
        market_refresh_started_ts = time.time()
        last_market_refresh_request_ts = market_refresh_started_ts
        last_market_refresh_reason = str(reason or "loop")
        last_market_refresh_result = "실행 중"
    threading.Thread(target=_market_refresh_worker, kwargs={"force": force, "reason": reason}, daemon=True).start()
    return True

def scan_watchdog_loop():
    global last_watchdog_cache_reset_ts
    while True:
        time.sleep(SCAN_WATCHDOG_CHECK_SEC)
        try:
            now_ts = time.time()
            if now_ts - BOT_START_TS < SCAN_WATCHDOG_STARTUP_GRACE_SEC:
                continue
            info = get_scan_runtime_health(now_ts)
            if info["loop_hard_stale"]:
                age = info["loop_age"]
                stage = str(info.get("stage", "?") or "?")
                if stage.startswith(("fetch_tickers", "build_universe", "build_cache")) and safe_float(age, 0) < (SCAN_HEARTBEAT_HARD_RESTART_SEC * 2):
                    continue
                print(f"[watchdog] 메인 루프 멈춤 감지 / {age}초 / stage={stage}", flush=True)
                os._exit(1)
            if info["cache_hard_stale"] and (now_ts - last_watchdog_cache_reset_ts >= SCAN_WATCHDOG_RESET_COOLDOWN_SEC):
                reason = f"시장 캐시 {info['cache_age']}초 정체 / stage={info.get('stage','?')}"
                reset_scan_runtime_state(reason)
                last_watchdog_cache_reset_ts = now_ts
        except Exception as e:
            print(f"[watchdog 오류] {e}", flush=True)

def update_scan_debug_snapshot(snapshot=None, note=""):
    global last_scan_debug_snapshot, last_scan_debug_snapshot_time, last_scan_debug_note
    try:
        if isinstance(snapshot, dict) and snapshot:
            last_scan_debug_snapshot = snapshot
            last_scan_debug_snapshot_time = time.time()
        elif isinstance(snapshot, list) and snapshot:
            last_scan_debug_snapshot = snapshot
            last_scan_debug_snapshot_time = time.time()
        last_scan_debug_note = note or last_scan_debug_note or ""
    except Exception as e:
        print(f"[scan_debug snapshot 오류] {e}")
def get_cached_market_tickers(force=False):
    global market_all_tickers, market_all_tickers_time
    now_ts = time.time()
    if (not force) and market_all_tickers and (now_ts - market_all_tickers_time < TICKER_LIST_REFRESH_SEC):
        return list(market_all_tickers)
    try:
        mark_main_loop_heartbeat(stage="fetch_tickers")
    except Exception:
        pass
    try:
        tickers = pybithumb.get_tickers()
        clean = []
        for raw in tickers or []:
            ticker = str(raw or "").upper().strip()
            if not ticker or ticker == "BTC":
                continue
            clean.append(ticker)
        if clean:
            market_all_tickers = clean
            market_all_tickers_time = now_ts
    except Exception as e:
        print(f"[티커 목록 조회 오류] {e}", flush=True)
    return list(market_all_tickers)

def pick_refresh_batch(items, cursor, batch_size):
    rows = list(items or [])
    if not rows:
        return [], 0
    size = max(1, min(int(batch_size), len(rows)))
    start = int(cursor or 0) % len(rows)
    picked = []
    idx2 = start
    while len(picked) < size:
        picked.append(rows[idx2])
        idx2 = (idx2 + 1) % len(rows)
        if idx2 == start:
            break
    return picked, idx2

def rank_universe_rows(rows):
    rows = [dict(x) for x in (rows or []) if isinstance(x, dict) and x.get("ticker")]
    if not rows:
        return []
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
    return selected[:TOP_TICKERS]

def get_market_universe(force=False):
    global market_universe, market_universe_time, market_universe_cursor, last_universe_build_stats
    now_ts = time.time()
    if (not force) and market_universe and (now_ts - market_universe_time < UNIVERSE_REFRESH_SEC):
        return market_universe
    tickers = get_cached_market_tickers(force=force)
    if not tickers:
        last_universe_build_stats = {
            "tried": 0,
            "success": 0,
            "selected": len(market_universe),
            "kept": len(market_universe),
            "duration_sec": 0.0,
            "note": "티커 목록 없음",
        }
        return market_universe
    tickers_set = set(tickers)
    base_map = {row.get("ticker"): dict(row) for row in market_universe if isinstance(row, dict) and row.get("ticker") in tickers_set}
    batch_size = UNIVERSE_FORCE_FULL_BATCH if (force or not base_map) else UNIVERSE_REFRESH_BATCH
    batch, next_cursor = pick_refresh_batch(tickers, market_universe_cursor, batch_size)
    started = time.time()
    refreshed_count = 0
    fail_count = 0
    tried_count = 0
    for idx3, ticker in enumerate(batch, start=1):
        tried_count += 1
        if idx3 % 8 == 0:
            try:
                mark_main_loop_heartbeat(stage=f"build_universe_{idx3}")
            except Exception:
                pass
        try:
            df3 = get_ohlcv(ticker, "minute3")
            if df3 is None or len(df3) < 30:
                fail_count += 1
                continue
            price = safe_float(df3["close"].iloc[-1], 0)
            if price <= 0:
                fail_count += 1
                continue
            vol_ratio = get_vol_ratio(df3, 3, 15)
            short_change = get_recent_change_pct(df3, 3)
            range_pct = get_range_pct(df3, 8)
            turnover_recent = float(df3["volume"].tail(5).sum()) * price
            turnover_score = math.log10(max(turnover_recent, 1))
            surge_score = min(vol_ratio, 7.0) * 1.8 + min(max(short_change, 0), 4.5) * 1.4 + min(range_pct, 5.0) * 0.7
            base_map[ticker] = {
                "ticker": ticker,
                "df_3m": df3,
                "last_price": price,
                "turnover_recent": turnover_recent,
                "turnover_score": turnover_score,
                "surge_score": surge_score,
            }
            refreshed_count += 1
        except Exception as e:
            fail_count += 1
            if fail_count <= 3:
                print(f"[유니버스 갱신 오류] {ticker}: {e}", flush=True)
        if (time.time() - started) >= UNIVERSE_BUILD_MAX_SEC and refreshed_count >= UNIVERSE_MIN_PARTIAL_ROWS:
            break
    if not base_map:
        last_universe_build_stats = {
            "tried": tried_count,
            "success": refreshed_count,
            "selected": len(market_universe),
            "kept": len(market_universe),
            "duration_sec": round(time.time() - started, 2),
            "note": "유니버스 후보 비었음",
        }
        return market_universe
    ranked = rank_universe_rows(base_map.values())
    if ranked:
        market_universe = ranked
        market_universe_time = time.time()
        market_universe_cursor = next_cursor
    last_universe_build_stats = {
        "tried": tried_count,
        "success": refreshed_count,
        "selected": len(market_universe),
        "kept": max(len(base_map) - refreshed_count, 0),
        "duration_sec": round(time.time() - started, 2),
        "note": "부분 갱신" if refreshed_count and refreshed_count < tried_count else ("유지" if refreshed_count == 0 else "정상"),
    }
    return market_universe

def build_shared_market_cache(force=False):
    global shared_market_cache, shared_market_cache_time, last_shared_cache_success_ts
    global shared_cache_cursor, shared_cache_row_times, last_cache_build_stats
    now_ts = time.time()
    cache_age = (now_ts - shared_market_cache_time) if shared_market_cache_time else None
    if (not force) and cache_age is not None and cache_age >= SCAN_CACHE_HARD_RESET_SEC:
        force = True
    if (not force) and shared_market_cache and (now_ts - shared_market_cache_time < MARKET_CACHE_TTL_SEC):
        return shared_market_cache
    universe = get_market_universe(force=force)
    if not universe:
        if cache_age is not None and cache_age >= SCAN_CACHE_HARD_RESET_SEC:
            reset_scan_runtime_state(f"시장 유니버스 갱신 실패가 {int(cache_age)}초 넘게 이어짐")
        last_cache_build_stats = {
            "tried": 0,
            "success": 0,
            "kept": len(shared_market_cache) if isinstance(shared_market_cache, dict) else 0,
            "duration_sec": 0.0,
            "note": "유니버스 없음",
        }
        return shared_market_cache
    universe_tickers = {row.get("ticker") for row in universe if isinstance(row, dict) and row.get("ticker")}
    base_cache = {ticker: dict(row) for ticker, row in shared_market_cache.items() if ticker in universe_tickers} if isinstance(shared_market_cache, dict) else {}
    row_times = {ticker: safe_float(shared_cache_row_times.get(ticker, shared_market_cache_time), shared_market_cache_time) for ticker in base_cache.keys()}
    batch_size = CACHE_FORCE_FULL_BATCH if (force or not base_cache) else CACHE_REFRESH_BATCH
    batch_rows, next_cursor = pick_refresh_batch(universe, shared_cache_cursor, batch_size)
    started = time.time()
    refreshed_count = 0
    fail_count = 0
    tried_count = 0
    for idx4, row in enumerate(batch_rows, start=1):
        tried_count += 1
        if idx4 % 8 == 0:
            try:
                mark_main_loop_heartbeat(stage=f"build_cache_{idx4}")
            except Exception:
                pass
        ticker = row.get("ticker", "")
        if not ticker:
            continue
        try:
            df1 = get_ohlcv(ticker, "minute1")
            if df1 is None or len(df1) < 35:
                fail_count += 1
                continue
            price = safe_float(df1["close"].iloc[-1], 0)
            if price <= 0:
                price = safe_float(row.get("last_price", 0), 0)
            if price <= 0:
                fail_count += 1
                continue
            change_1 = get_recent_change_pct(df1, 2)
            change_3 = get_recent_change_pct(df1, 3)
            change_5 = get_recent_change_pct(df1, 5)
            vol_ratio_1m = get_vol_ratio(df1, 3, 15)
            range_pct_1m = get_range_pct(df1, 10)
            rsi_1m = get_rsi(df1, 14)
            base_cache[ticker] = {
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
                "cache_updated_at": time.time(),
            }
            row_times[ticker] = time.time()
            refreshed_count += 1
        except Exception as e:
            fail_count += 1
            if fail_count <= 3:
                print(f"[시장 캐시 갱신 오류] {ticker}: {e}", flush=True)
        if (time.time() - started) >= CACHE_BUILD_MAX_SEC and refreshed_count >= CACHE_MIN_PARTIAL_ROWS:
            break
    if base_cache:
        keep = {}
        keep_times = {}
        for ticker, row in base_cache.items():
            updated_at = safe_float(row_times.get(ticker, shared_market_cache_time), shared_market_cache_time)
            if refreshed_count > 0 and updated_at > 0 and (now_ts - updated_at) > CACHE_ROW_FRESH_KEEP_SEC:
                continue
            keep[ticker] = row
            keep_times[ticker] = updated_at
        if keep:
            base_cache = keep
            row_times = keep_times
    if refreshed_count > 0 and base_cache:
        shared_market_cache = base_cache
        shared_cache_row_times = row_times
        shared_market_cache_time = time.time()
        last_shared_cache_success_ts = shared_market_cache_time
        shared_cache_cursor = next_cursor
        update_recent_leader_board(shared_market_cache)
        update_market_pulse_state(shared_market_cache)
        try:
            update_scan_debug_snapshot(shared_market_cache, note=f"시장 캐시 부분 갱신 {refreshed_count}/{tried_count}")
        except Exception:
            pass
    elif not shared_market_cache and base_cache:
        shared_market_cache = base_cache
        shared_cache_row_times = row_times
    elif cache_age is not None and cache_age >= SCAN_CACHE_HARD_RESET_SEC:
        reset_scan_runtime_state(f"1분봉 시장 캐시가 {int(cache_age)}초 넘게 새로 안 만들어짐")
    last_cache_build_stats = {
        "tried": tried_count,
        "success": refreshed_count,
        "kept": len(shared_market_cache) if isinstance(shared_market_cache, dict) else 0,
        "duration_sec": round(time.time() - started, 2),
        "note": "부분 갱신" if refreshed_count and refreshed_count < tried_count else ("유지" if refreshed_count == 0 else "정상"),
    }
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
def reset_auto_pause_state(bypass_sec=900, reason="manual"):
    global paused_until, pause_reason, auto_pause_bypass_until, auto_pause_bypass_reason, auto_pause_reset_ignore_before
    now_ts = int(time.time())
    paused_until = 0
    pause_reason = ""
    auto_pause_bypass_until = now_ts + max(int(bypass_sec), 0)
    auto_pause_bypass_reason = str(reason or "manual")
    auto_pause_reset_ignore_before = now_ts
def should_pause_auto_buy_now():
    clear_auto_pause_if_needed()
    if auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time())
        if auto_pause_bypass_reason == "restart":
            return False, f"재시작으로 쉬기 초기화됨 / 자동 쉬기 재판정까지 {max(remain, 0)}초"
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
    reference_high = get_recent_high(df, 12, exclude_last=True)
    if reference_high <= 0:
        return None
    high_gap_pct = ((reference_high - current_price) / current_price) * 100.0
    reference_low = get_recent_low(df, 12, exclude_last=False)
    rebound_from_low_pct = ((current_price - reference_low) / reference_low) * 100.0 if reference_low > 0 else 0.0
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
    if high_gap_pct < EARLY_CFG["high_gap_min"] or high_gap_pct > EARLY_CFG["high_gap_max"]:
        return None
    if rebound_from_low_pct > EARLY_CFG["rebound_max"]:
        return None
    if rebound_from_low_pct > 1.85 and high_gap_pct < 1.05:
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
    if last2_change_pct > 1.10:
        return None
    if upper_wick_ratio > 0.30:
        return None
    if change_pct > 1.60 and vol_ratio < 2.80:
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
    reference_high = get_recent_high(df, 10, exclude_last=True)
    reference_low = get_recent_low(df, 10, exclude_last=False)
    high_gap_pct = ((reference_high - current_price) / current_price) * 100.0 if reference_high > 0 and current_price > 0 else 999.0
    rebound_from_low_pct = ((current_price - reference_low) / reference_low) * 100.0 if reference_low > 0 else 0.0
    if high_gap_pct < 0.55:
        return None
    if rebound_from_low_pct > 3.60:
        return None
    if rebound_from_low_pct > 2.40 and high_gap_pct < 1.10:
        return None
    recent_hot_high = get_recent_high(df, 6, exclude_last=False)
    recent_hot_gap_pct = ((recent_hot_high - current_price) / current_price) * 100.0 if recent_hot_high > 0 and current_price > 0 else 0.0
    if recent_hot_gap_pct >= 2.60 and vol_ratio < 6.20 and change_pct < 2.80:
        return None
    if change_pct > 2.40 and vol_ratio < 3.20:
        return None
    score_v = 5.1 + min(vol_ratio, 4.0) * 0.8 + min(data.get("surge_score", 0), 8) * 0.14 + pattern_info.get("bonus_score", 0)
    if vol_ratio >= 3.60 and "저점높임" in pattern_info.get("tags", []):
        score_v += 0.28
    if vol_ratio >= 4.40 and high_gap_pct >= 0.95:
        score_v += 0.22
    if rebound_from_low_pct <= 2.20 and high_gap_pct >= 0.85:
        score_v += 0.18
    if vol_ratio < 2.70 and high_gap_pct < 0.90:
        score_v -= 0.40
    if recent_hot_gap_pct >= 2.80 and vol_ratio < 5.80:
        score_v -= 0.35
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
    recent_low = get_recent_low(df, 12, exclude_last=False)
    rebound_from_low_pct = ((current_price - recent_low) / recent_low) * 100.0 if recent_low > 0 else 0.0
    if gap_to_break < PRE_BREAKOUT_CFG["gap_min"] or gap_to_break > PRE_BREAKOUT_CFG["gap_max"]:
        return None
    if rebound_from_low_pct > PRE_BREAKOUT_CFG["rebound_max"]:
        return None
    if rebound_from_low_pct > 2.20 and gap_to_break < 0.40:
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
    cache = shared_cache if isinstance(shared_cache, dict) and shared_cache else (shared_market_cache if isinstance(shared_market_cache, dict) and shared_market_cache else None)
    if not cache:
        request_market_refresh(force=False, reason="auto_trade_empty_cache")
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
def build_watch_snapshot(item: dict, main_watch: bool = False):
    now_ts = time.time()
    return {
        "strategy": item.get("strategy", ""),
        "strategy_label": item.get("strategy_label", ""),
        "trade_tier": str(item.get("trade_tier", "B") or "B").upper().strip(),
        "user_alert_grade": classify_user_alert_grade(item),
        "change_pct": safe_float(item.get("change_pct", item.get("change_5", 0))),
        "change_5": safe_float(item.get("change_5", item.get("change_pct", 0))),
        "vol_ratio": safe_float(item.get("vol_ratio", item.get("vol_ratio_1m", 0))),
        "signal_score": safe_float(item.get("signal_score", item.get("edge_score", 0))),
        "edge_score": safe_float(item.get("edge_score", item.get("signal_score", 0))),
        "leader_score": safe_float(item.get("leader_score", 0)),
        "high_gap_pct": get_reference_high_gap_pct(item),
        "is_main_watch": bool(main_watch),
        "pattern_tags": list(item.get("pattern_tags", [])),
        "price": safe_float(item.get("current_price", item.get("price", 0))),
        "turnover": safe_float(item.get("turnover", 0)),
        "rsi": safe_float(item.get("rsi", item.get("rsi_1m", 50))),
        "saved_at": safe_float(item.get("saved_at", now_ts)) or now_ts,
        "judgement": get_watch_judgement_text(item),
        "caution": get_watch_caution_text(item),
        "conclusion": get_watch_display_conclusion(item),
        "section_key": get_watch_display_group(item),
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
def get_watch_quality_block_reason(item: dict, alert_type: str = "new"):
    strategy = str(item.get("strategy", "") or "")
    tier = str(item.get("trade_tier", "B") or "B").upper().strip()
    edge = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    change_pct = safe_float(item.get("change_pct", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    tags = set(item.get("pattern_tags", []))
    recovery_like = bool(tags & {"저점높임", "눌림재확인", "가짜하락회복"})
    snap = ensure_pre_alert_snapshot(item)
    if strategy == "EARLY" and tier == "B":
        recent_spike = bool(safe_float(snap.get("had_recent_spike", 0), 0))
        from_30m_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
        below_30m_high = safe_float(snap.get("below_30m_high_pct", 0), 99)
        vol_5v20_ratio = safe_float(snap.get("vol_5v20_ratio", 0), 0)
        higher_low_hint = bool(snap.get("higher_low_hint", False))
        if high_gap_pct < 0.80:
            return "바로 위 저항이 가까워서 초반 선점형 B에서 제외"
        if recent_spike and below_30m_high < 1.20:
            return "이미 한 번 튄 뒤라 초반 선점형 B에서 제외"
        if from_30m_low > 3.20 and below_30m_high < 1.15:
            return "저점에서 이미 많이 올라와서 초반 선점형 B에서 제외"
        if vol_5v20_ratio < 1.05 and leader_score < 1.55 and vol_ratio < 2.70:
            return "거래량이 이어지지 않아 초반 선점형 B에서 제외"
        if not recovery_like and not higher_low_hint and leader_score < 1.45 and vol_ratio < 3.10:
            return "초반처럼 보여도 힘이 약해서 버릴 후보"
        if leader_score < 1.20 and change_pct < 0.80 and vol_ratio < 2.60:
            return "초입 힘이 약해서 지금 볼 가치가 낮음"
        if high_gap_pct < 0.95 and vol_ratio < 3.30 and leader_score < 1.65:
            return "잠깐 튀고 끝날 가능성이 커서 제외"
    if strategy == "PREPUMP" and tier == "B":
        recent_spike = bool(safe_float(snap.get("had_recent_spike", 0), 0))
        from_30m_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
        below_30m_high = safe_float(snap.get("below_30m_high_pct", 0), 99)
        vol_5v20_ratio = safe_float(snap.get("vol_5v20_ratio", 0), 0)
        higher_low_hint = bool(snap.get("higher_low_hint", False))
        if high_gap_pct < 0.70:
            return "바로 위 저항이 가까워서 상승 시작형 B에서 제외"
        if leader_score < 0.85:
            return "큰 흐름 준비가 너무 약해서 상승 시작형 B에서 제외"
        if recent_spike and below_30m_high < 1.10:
            return "이미 한 번 튄 뒤라 지금 초반형으로 보기 어려움"
        if from_30m_low > 3.90 and below_30m_high < 1.35:
            return "저점에서 이미 많이 올라와서 지금 사기 늦음"
        if not recovery_like and not higher_low_hint and leader_score < 1.35 and vol_ratio < 4.80:
            return "반등 모양은 있는데 힘이 약해서 버릴 후보"
        if vol_5v20_ratio < 1.05 and leader_score < 1.55 and vol_ratio < 5.10:
            return "거래량이 이어지지 않아 상승 시작형 B에서 제외"
        if leader_score < 1.45 and change_pct < 1.25 and vol_ratio < 4.60:
            return "초반 힘이 부족해서 지금 볼 가치가 낮음"
        if high_gap_pct < 0.95 and vol_ratio < 6.20 and leader_score < 1.75:
            return "잠깐 튀고 끝날 가능성이 커서 제외"
    if alert_type in ["upgrade", "renotice"]:
        if strategy in ["EARLY", "PREPUMP", "TREND_CONT", "BREAKOUT", "LEADER_WATCH"] and high_gap_pct < 0.75:
            return "재알림 시점이 최근 고점에 너무 가까워 제외"
        if strategy in ["EARLY", "PREPUMP"] and change_pct > 2.60 and high_gap_pct < 1.00:
            return "재알림 시점이 이미 꽤 오른 자리라 제외"
        if strategy == "EARLY" and tier == "B" and not recovery_like and leader_score < 2.00 and vol_ratio < 3.80:
            return "재알림치고 힘이 약한 초반 선점형 B라 제외"
        if strategy == "PREPUMP" and not recovery_like and leader_score < 2.00 and vol_ratio < 6.40:
            return "재알림치고 힘이 약한 상승 시작형 B라 제외"
    return ""
def cleanup_recent_watch_history(now_ts: float = None):
    now_ts = safe_float(now_ts, time.time())
    for ticker in list(recent_watch_history.keys()):
        rows = recent_watch_history.get(ticker, [])
        if not isinstance(rows, list):
            recent_watch_history.pop(ticker, None)
            continue
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts = safe_float(row.get("ts", 0), 0)
            if ts <= 0:
                continue
            if now_ts - ts <= WATCH_REPEAT_WINDOW_SEC:
                kept.append(row)
        if kept:
            recent_watch_history[ticker] = kept[-8:]
        else:
            recent_watch_history.pop(ticker, None)
def get_recent_watch_alert_count(ticker: str, now_ts: float = None) -> int:
    cleanup_recent_watch_history(now_ts)
    rows = recent_watch_history.get(ticker, [])
    return len(rows) if isinstance(rows, list) else 0
def is_watch_realert_strong_enough(prev_snap: dict, item: dict, is_main_watch: bool = False) -> bool:
    if not isinstance(prev_snap, dict):
        return True
    prev_strategy = str(prev_snap.get("strategy", "") or "")
    now_strategy = str(item.get("strategy", "") or "")
    prev_tier = str(prev_snap.get("trade_tier", "B") or "B").upper().strip()
    now_tier = str(item.get("trade_tier", "B") or "B").upper().strip()
    prev_main = bool(prev_snap.get("is_main_watch", False))
    if get_strategy_rank(now_strategy) > get_strategy_rank(prev_strategy):
        return True
    if prev_tier == "B" and now_tier in {"A", "S"}:
        return True
    if (not prev_main) and is_main_watch:
        return True
    score_delta = safe_float(item.get("signal_score", 0), 0) - safe_float(prev_snap.get("signal_score", 0), 0)
    vol_delta = safe_float(item.get("vol_ratio", 0), 0) - safe_float(prev_snap.get("vol_ratio", 0), 0)
    leader_delta = safe_float(item.get("leader_score", 0), 0) - safe_float(prev_snap.get("leader_score", 0), 0)
    change_delta = safe_float(item.get("change_pct", 0), 0) - safe_float(prev_snap.get("change_pct", 0), 0)
    high_gap_delta = get_reference_high_gap_pct(item) - safe_float(prev_snap.get("high_gap_pct", 0), 0)
    if score_delta >= WATCH_STRONGER_SCORE_DELTA:
        return True
    if leader_delta >= WATCH_STRONGER_LEADER_DELTA:
        return True
    if vol_delta >= WATCH_STRONGER_VOL_DELTA and change_delta >= WATCH_STRONGER_CHANGE_DELTA:
        return True
    if vol_delta >= WATCH_STRONGER_VOL_DELTA and high_gap_delta >= WATCH_STRONGER_HIGH_GAP_DELTA:
        return True
    return False
def should_send_watch_alert(ticker: str, item: dict, now_ts: float):
    if is_ticker_blocked_for_watch_alert(ticker):
        return False, "", ""
    if not has_meaningful_watch_motion(item):
        return False, "", ""
    if WATCH_BLOCK_IF_PENDING_30MIN_CHECK and has_pending_30m_check_for_ticker(ticker, now_ts):
        return False, "", ""
    is_main_watch = should_send_main_watch_alert(item)
    repeat_count = get_recent_watch_alert_count(ticker, now_ts)
    prev_time = recent_watch_alerts.get(ticker, 0)
    prev_snap = recent_watch_snapshots.get(ticker)
    if not prev_snap or prev_time <= 0:
        block_reason = get_watch_quality_block_reason(item, alert_type="new")
        if block_reason:
            return False, "", ""
        return True, "new", ""
    judgement = get_watch_judgement_text(item)
    caution = get_watch_caution_text(item)
    compact_mode = should_use_compact_watch_alert(item, judgement, caution)
    min_realert_sec = WATCH_WEAK_REALERT_MIN_SEC if compact_mode else WATCH_STRONG_REALERT_MIN_SEC
    if now_ts - prev_time < min_realert_sec:
        return False, "", ""
    if repeat_count >= WATCH_REPEAT_MAX_PER_TICKER and not is_watch_realert_strong_enough(prev_snap, item, is_main_watch=is_main_watch):
        return False, "", ""
    reasons = compare_watch_improvement(prev_snap, item)
    if reasons:
        block_reason = get_watch_quality_block_reason(item, alert_type="upgrade")
        if block_reason:
            return False, "", ""
        if now_ts - prev_time < WATCH_REPEAT_REQUIRE_STRONGER_SEC:
            if not is_watch_realert_strong_enough(prev_snap, item, is_main_watch=is_main_watch):
                return False, "", ""
        return True, "upgrade", " / ".join(reasons)
    renotice_count = int(recent_watch_renotice_counts.get(ticker, 0))
    if now_ts - prev_time >= WATCH_RENOTICE_SEC and renotice_count < WATCH_RENOTICE_MAX_PER_TICKER:
        if compact_mode and repeat_count >= 1:
            return False, "", ""
        if should_allow_time_renotice(item):
            block_reason = get_watch_quality_block_reason(item, alert_type="renotice")
            if block_reason:
                return False, "", ""
            if repeat_count >= 1 and not is_watch_realert_strong_enough(prev_snap, item, is_main_watch=is_main_watch):
                return False, "", ""
            return True, "renotice", "시간이 지나도 흐름이 유지돼 다시 확인"
    return False, "", ""
def save_watch_snapshot(ticker: str, item: dict, now_ts: float, alert_type: str = "new", main_watch: bool = False):
    cleanup_recent_watch_history(now_ts)
    recent_watch_alerts[ticker] = now_ts
    snap = build_watch_snapshot(item, main_watch=main_watch)
    snap["saved_at"] = now_ts
    recent_watch_snapshots[ticker] = snap
    history = recent_watch_history.get(ticker, [])
    if not isinstance(history, list):
        history = []
    history.append({
        "ts": now_ts,
        "alert_type": alert_type,
        "main_watch": bool(main_watch),
    })
    recent_watch_history[ticker] = history[-8:]
    if alert_type in ["new", "upgrade"]:
        recent_watch_renotice_counts[ticker] = 0
    elif alert_type == "renotice":
        recent_watch_renotice_counts[ticker] = int(recent_watch_renotice_counts.get(ticker, 0)) + 1
def build_market_pulse_state(cache: dict, top_n: int = MARKET_PULSE_TOP_N):
    state = {"saved_at": time.time(), "top_n": int(top_n), "lists": {}}
    if not isinstance(cache, dict):
        return state
    metric_defs = {
        "change_1": lambda d: safe_float(d.get("change_1", 0), 0),
        "change_3": lambda d: safe_float(d.get("change_3", 0), 0),
        "change_5": lambda d: safe_float(d.get("change_5", 0), 0),
        "vol_ratio": lambda d: safe_float(d.get("vol_ratio", d.get("vol_ratio_1m", 0)), 0),
        "turnover": lambda d: safe_float(d.get("turnover", 0), 0),
        "leader_score": lambda d: safe_float(d.get("leader_score", 0), 0),
    }
    for name, getter in metric_defs.items():
        rows = []
        for ticker, data in cache.items():
            try:
                value = getter(data)
            except Exception:
                value = 0.0
            if value <= 0:
                continue
            rows.append((str(ticker or "").upper().strip(), round(value, 4)))
        rows.sort(key=lambda x: x[1], reverse=True)
        state["lists"][name] = [{"ticker": ticker, "value": value, "rank": idx} for idx, (ticker, value) in enumerate(rows[:max(1, int(top_n))], start=1)]
    return state

def update_market_pulse_state(cache: dict):
    global market_pulse_state
    market_pulse_state = build_market_pulse_state(cache, top_n=MARKET_PULSE_TOP_N)
    persist_market_pulse_state()
    return market_pulse_state

def get_market_pulse_rank_map(snapshot: dict = None) -> dict:
    snapshot = snapshot if isinstance(snapshot, dict) else market_pulse_state
    rank_map = {}
    lists = snapshot.get("lists", {}) if isinstance(snapshot, dict) else {}
    if not isinstance(lists, dict):
        return rank_map
    for name, rows in lists.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker", "") or "").upper().strip()
            rank = int(safe_float(row.get("rank", 0), 0))
            value = safe_float(row.get("value", 0), 0)
            if not ticker or rank <= 0:
                continue
            rank_map.setdefault(ticker, {})[name] = {"rank": rank, "value": value}
    return rank_map

def get_market_pulse_rank_info(ticker: str, snapshot: dict = None) -> dict:
    ticker = str(ticker or "").upper().strip()
    info = {
        "change_1_rank": 0,
        "change_3_rank": 0,
        "change_5_rank": 0,
        "vol_ratio_rank": 0,
        "turnover_rank_pulse": 0,
        "leader_rank": 0,
        "seen_hot_rank": False,
    }
    if not ticker:
        return info
    row = get_market_pulse_rank_map(snapshot).get(ticker, {})
    info["change_1_rank"] = int(safe_float((row.get("change_1") or {}).get("rank", 0), 0))
    info["change_3_rank"] = int(safe_float((row.get("change_3") or {}).get("rank", 0), 0))
    info["change_5_rank"] = int(safe_float((row.get("change_5") or {}).get("rank", 0), 0))
    info["vol_ratio_rank"] = int(safe_float((row.get("vol_ratio") or {}).get("rank", 0), 0))
    info["turnover_rank_pulse"] = int(safe_float((row.get("turnover") or {}).get("rank", 0), 0))
    info["leader_rank"] = int(safe_float((row.get("leader_score") or {}).get("rank", 0), 0))
    ranks = [v for k, v in info.items() if k.endswith("_rank") and isinstance(v, int) and v > 0]
    info["seen_hot_rank"] = any(v <= MARKET_PULSE_RANK_SEEN_CUTOFF for v in ranks)
    return info

def build_market_rank_watch_candidates(cache: dict, limit_each: int = MARKET_PULSE_TOP_N):
    candidates = []
    seen = set()
    if not isinstance(cache, dict):
        return candidates
    subset_cache = get_watchlist_subset_cache(cache, limit=min(max(int(limit_each) * 2, 8), SCAN_WATCHLIST_MARKET_CACHE_LIMIT))
    snapshot = build_market_pulse_state(subset_cache or cache, top_n=limit_each)
    rank_map = get_market_pulse_rank_map(snapshot)
    for ticker, _row in rank_map.items():
        data = cache.get(ticker)
        if not isinstance(data, dict):
            continue
        item = build_missed_review_probe_item(ticker, data)
        if not item:
            continue
        info = get_market_pulse_rank_info(ticker, snapshot=snapshot)
        if not info.get("seen_hot_rank", False):
            continue
        key = str(ticker or "").upper().strip()
        if key in seen:
            continue
        seen.add(key)
        item.update(info)
        candidates.append(item)
    candidates.sort(key=lambda x: (
        min([r for r in [int(x.get("change_1_rank", 0) or 0), int(x.get("change_3_rank", 0) or 0), int(x.get("change_5_rank", 0) or 0), int(x.get("vol_ratio_rank", 0) or 0), int(x.get("turnover_rank_pulse", 0) or 0), int(x.get("leader_rank", 0) or 0)] if r > 0] or [999]),
        -safe_float(x.get("leader_score", 0), 0),
        -safe_float(x.get("vol_ratio", 0), 0),
        -safe_float(x.get("change_pct", 0), 0),
    ))
    return candidates[:max(1, int(limit_each))]

def build_missed_review_probe_item(ticker: str, data: dict):
    try:
        df1 = data.get("df_1m")
        price = safe_float(data.get("price", 0), 0)
        if df1 is None or len(df1) < 12 or price <= 0:
            return None
        item = {
            "ticker": str(ticker or "").upper().strip(),
            "current_price": price,
            "price": price,
            "vol_ratio": safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0)), 0),
            "leader_score": safe_float(data.get("leader_score", 0), 0),
            "change_1": safe_float(data.get("change_1", 0), 0),
            "change_3": safe_float(data.get("change_3", 0), 0),
            "change_5": safe_float(data.get("change_5", 0), 0),
            "change_pct": safe_float(data.get("change_5", 0), 0),
            "turnover": safe_float(data.get("turnover", 0), 0),
            "turnover_rank": int(safe_float(data.get("turnover_rank", 0), 0)),
            "surge_rank": int(safe_float(data.get("surge_rank", 0), 0)),
            "rsi": safe_float(data.get("rsi", data.get("rsi_1m", 50)), 50),
            "strategy": "",
            "strategy_label": "시장후보",
            "trade_tier": "B",
            "signal_score": 0.0,
            "edge_score": 0.0,
        }
        item.update(compute_chart_position_profile(df1, price, lookback=CHART_POS_LOOKBACK_1M))
        item["pre_alert_snapshot"] = compute_pre_alert_snapshot_from_df(df1, current_price=price)
        item["alert_chart_context"] = compute_alert_chart_context_from_df(df1, df4h=None, current_price=price)
        item["reference_high"] = get_recent_high(df1, 8, exclude_last=True)
        item["reference_low"] = get_recent_low(df1, 8, exclude_last=False)
        return item
    except Exception:
        return None

def build_missed_review_market_candidates(cache: dict, limit: int = MISSED_REVIEW_MARKET_LIMIT):
    items = []
    if not isinstance(cache, dict):
        return items
    ranked_rows = get_watchlist_ranked_cache_items(cache, limit=min(max(int(limit) * 2, 8), SCAN_WATCHLIST_MARKET_CACHE_LIMIT))
    for ticker, data, _score in ranked_rows:
        try:
            if is_watch_excluded_ticker(ticker):
                continue
            item = build_missed_review_probe_item(ticker, data)
            if not item:
                continue
            snap = ensure_pre_alert_snapshot(item)
            change_5 = safe_float(snap.get("chg_5m", item.get("change_pct", 0)), 0)
            change_30 = safe_float(snap.get("chg_30m", 0), 0)
            from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
            vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
            leader = safe_float(item.get("leader_score", 0), 0)
            if max(change_5, change_30, from_low) < 0.10 and vol_ratio < 1.35 and leader < 0.35:
                continue
            score = (
                max(change_5, 0) * 6.0
                + max(change_30, 0) * 2.8
                + max(from_low, 0) * 1.2
                + max(vol_ratio - 1.0, 0) * 3.0
                + leader * 4.0
            )
            items.append((score, item))
        except Exception:
            continue
    items.sort(key=lambda x: x[0], reverse=True)
    return [item for _score, item in items[:max(1, int(limit))]]

def build_missed_review_row_from_item(item: dict, now_ts: float = None) -> dict | None:
    if not isinstance(item, dict):
        return None
    ticker = str(item.get("ticker", "") or "").upper().strip()
    price = safe_float(item.get("current_price", item.get("price", 0)), 0)
    if not ticker or price <= 0:
        return None
    now_ts = safe_float(now_ts, time.time())
    snap = ensure_pre_alert_snapshot(item)
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    rank_info = get_market_pulse_rank_info(ticker)
    turnover_rank = int(safe_float(item.get("turnover_rank", 0), 0))
    surge_rank = int(safe_float(item.get("surge_rank", 0), 0))
    if turnover_rank > 0:
        rank_info["turnover_rank"] = turnover_rank
    else:
        rank_info["turnover_rank"] = 0
    if surge_rank > 0:
        rank_info["surge_rank"] = surge_rank
    else:
        rank_info["surge_rank"] = 0
    rank_info["seen_hot_rank"] = bool(rank_info.get("seen_hot_rank", False) or (turnover_rank and turnover_rank <= MARKET_PULSE_RANK_SEEN_CUTOFF) or (surge_rank and surge_rank <= MARKET_PULSE_RANK_SEEN_CUTOFF))
    return {
        "ticker": ticker,
        "saved_at": now_ts,
        "review_ts": now_ts + MISSED_REVIEW_DELAY_SEC,
        "base_price": price,
        "strategy": str(item.get("strategy", "") or ""),
        "strategy_label": str(item.get("strategy_label", "") or ""),
        "trade_tier": str(item.get("trade_tier", "B") or "B").upper().strip(),
        "user_alert_grade": classify_user_alert_grade(item),
        "signal_score": safe_float(item.get("signal_score", item.get("edge_score", 0)), 0),
        "edge_score": safe_float(item.get("edge_score", item.get("signal_score", 0)), 0),
        "vol_ratio": safe_float(item.get("vol_ratio", item.get("vol_ratio_1m", 0)), 0),
        "leader_score": safe_float(item.get("leader_score", 0), 0),
        "change_pct": safe_float(item.get("change_pct", 0), 0),
        "turnover": safe_float(item.get("turnover", 0), 0),
        "chg_5m": safe_float(snap.get("chg_5m", item.get("change_pct", 0)), 0),
        "chg_30m": safe_float(snap.get("chg_30m", 0), 0),
        "from_30m_low_pct": safe_float(snap.get("from_30m_low_pct", 0), 0),
        "below_30m_high_pct": safe_float(snap.get("below_30m_high_pct", get_reference_high_gap_pct(item)), 999),
        "vol_5v20_ratio": safe_float(snap.get("vol_5v20_ratio", 0), 0),
        "higher_low_hint": bool(snap.get("higher_low_hint", False)),
        "chart_rise_pct": safe_float(profile.get("chart_rise_pct", 0), 0),
        "chart_gap_pct": safe_float(profile.get("chart_gap_pct", 999), 999),
        "chart_zone_ratio": safe_float(profile.get("chart_zone_ratio", -1), -1),
        "high_gap_pct": get_reference_high_gap_pct(item),
        "change_1_rank": int(rank_info.get("change_1_rank", 0) or 0),
        "change_3_rank": int(rank_info.get("change_3_rank", 0) or 0),
        "change_5_rank": int(rank_info.get("change_5_rank", 0) or 0),
        "vol_ratio_rank": int(rank_info.get("vol_ratio_rank", 0) or 0),
        "turnover_rank_pulse": int(rank_info.get("turnover_rank_pulse", 0) or 0),
        "leader_rank": int(rank_info.get("leader_rank", 0) or 0),
        "turnover_rank": int(rank_info.get("turnover_rank", 0) or 0),
        "surge_rank": int(rank_info.get("surge_rank", 0) or 0),
        "seen_hot_rank": bool(rank_info.get("seen_hot_rank", False)),
        "seen_market": False,
        "seen_shortlist": False,
        "seen_signal": False,
        "alert_sent": False,
        "alert_section": "",
        "display_key": str(get_watch_display_group(item) or "").strip(),
        "quality_block_reason": "",
    }

def merge_missed_review_row(base: dict, extra: dict):
    if not isinstance(base, dict):
        return extra
    if not isinstance(extra, dict):
        return base
    for key in ["seen_market", "seen_shortlist", "seen_signal", "alert_sent", "higher_low_hint", "seen_hot_rank"]:
        base[key] = bool(base.get(key, False) or extra.get(key, False))
    if extra.get("alert_section"):
        base["alert_section"] = extra.get("alert_section")
    if extra.get("display_key"):
        base["display_key"] = extra.get("display_key")
    if extra.get("quality_block_reason") and not base.get("quality_block_reason"):
        base["quality_block_reason"] = extra.get("quality_block_reason")
    for key in [
        "strategy", "strategy_label", "trade_tier", "signal_score", "edge_score", "vol_ratio", "leader_score",
        "change_pct", "turnover", "chg_5m", "chg_30m", "from_30m_low_pct", "below_30m_high_pct",
        "vol_5v20_ratio", "chart_rise_pct", "chart_gap_pct", "chart_zone_ratio", "high_gap_pct",
        "change_1_rank", "change_3_rank", "change_5_rank", "vol_ratio_rank", "turnover_rank_pulse", "leader_rank", "turnover_rank", "surge_rank",
    ]:
        if (not base.get(key)) and extra.get(key) not in [None, ""]:
            base[key] = extra.get(key)
    base_saved = safe_float(base.get("saved_at", 0), 0)
    extra_saved = safe_float(extra.get("saved_at", 0), 0)
    if extra_saved > 0 and (base_saved <= 0 or extra_saved < base_saved):
        base["saved_at"] = extra_saved
        base["review_ts"] = extra.get("review_ts", extra_saved + MISSED_REVIEW_DELAY_SEC)
        if safe_float(extra.get("base_price", 0), 0) > 0:
            base["base_price"] = extra.get("base_price")
    return base

def register_missed_review_candidate(item: dict, now_ts: float = None, seen_market: bool = False, seen_shortlist: bool = False, seen_signal: bool = False, alert_sent: bool = False, alert_section: str = "", quality_block_reason: str = "", persist: bool = True):
    global missed_review_queue
    if not MISSED_REVIEW_ON:
        return
    row = build_missed_review_row_from_item(item, now_ts=now_ts)
    if not row:
        return
    ticker = row["ticker"]
    row["seen_market"] = bool(seen_market)
    row["seen_shortlist"] = bool(seen_shortlist)
    row["seen_signal"] = bool(seen_signal)
    row["alert_sent"] = bool(alert_sent)
    row["alert_section"] = str(alert_section or "").strip()
    row["display_key"] = str(alert_section or row.get("display_key", "")).strip() or str(row.get("display_key", "")).strip()
    row["quality_block_reason"] = str(quality_block_reason or "").strip()
    existing = missed_review_queue.get(ticker)
    changed = False
    if isinstance(existing, dict):
        existing_saved = safe_float(existing.get("saved_at", 0), 0)
        row_saved = safe_float(row.get("saved_at", 0), 0)
        if existing_saved > 0 and row_saved - existing_saved <= MISSED_REVIEW_REGISTER_COOLDOWN_SEC:
            missed_review_queue[ticker] = merge_missed_review_row(existing, row)
            changed = True
        else:
            missed_review_queue[ticker] = row
            changed = True
    else:
        missed_review_queue[ticker] = row
        changed = True
    if changed:
        cleanup_missed_review_queue(now_ts)
        if persist:
            persist_missed_review_state()

def register_missed_review_market_candidates(cache: dict, now_ts: float = None, limit: int = MISSED_REVIEW_MARKET_LIMIT):
    if not MISSED_REVIEW_ON or int(limit) <= 0:
        return 0
    now_ts = safe_float(now_ts, time.time())
    picked = []
    picked.extend(build_missed_review_market_candidates(cache, limit=limit))
    rank_limit = min(int(limit), max(4, MARKET_PULSE_TOP_N // 2))
    picked.extend(build_market_rank_watch_candidates(cache, limit_each=rank_limit))
    seen = set()
    changed = 0
    for item in picked:
        ticker = str(item.get("ticker", "") or "").upper().strip()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        before = dict(missed_review_queue.get(ticker, {})) if isinstance(missed_review_queue.get(ticker), dict) else None
        register_missed_review_candidate(item, now_ts=now_ts, seen_market=True, persist=False)
        after = missed_review_queue.get(ticker)
        if before != after:
            changed += 1
    if changed > 0:
        cleanup_missed_review_queue(now_ts)
        persist_missed_review_state()
    return changed

def initial_spot_good_enough_for_missed_review(row: dict):
    high_gap_pct = safe_float(row.get("high_gap_pct", row.get("below_30m_high_pct", 999)), 999)
    from_low = safe_float(row.get("from_30m_low_pct", 0), 0)
    chart_rise = safe_float(row.get("chart_rise_pct", from_low), from_low)
    chg_5 = safe_float(row.get("chg_5m", 0), 0)
    chg_30 = safe_float(row.get("chg_30m", 0), 0)
    vol_ratio = safe_float(row.get("vol_ratio", 0), 0)
    leader = safe_float(row.get("leader_score", 0), 0)
    if high_gap_pct < MISSED_REVIEW_NEAR_HIGH_BLOCK_PCT:
        return False, "시작 자리 자체가 최근 고점에 너무 가까웠음"
    if from_low > MISSED_REVIEW_MAX_BASE_FROM_LOW_PCT and high_gap_pct < 1.20:
        return False, "시작할 때 이미 저점에서 꽤 올라온 자리였음"
    if chart_rise > MISSED_REVIEW_MAX_BASE_CHART_RISE_PCT and high_gap_pct < 1.80:
        return False, "시작할 때 차트상 이미 위쪽 자리였음"
    if max(chg_5, chg_30, from_low) < 0.10 and vol_ratio < MISSED_REVIEW_MIN_BASE_VOL_RATIO and leader < MISSED_REVIEW_MIN_BASE_LEADER_SCORE:
        return False, "시작할 때는 시장 힘이 아직 거의 없었음"
    return True, ""

def classify_missed_review_result_quality(best_up_pct: float, end_up_pct: float, worst_down_pct: float) -> str:
    best_up_pct = safe_float(best_up_pct, 0)
    end_up_pct = safe_float(end_up_pct, 0)
    worst_down_pct = safe_float(worst_down_pct, 0)
    peak_fade = best_up_pct - end_up_pct
    qualifies_move = (
        best_up_pct >= MISSED_REVIEW_MIN_FAST_UP_PCT
        or (best_up_pct >= MISSED_REVIEW_MIN_BEST_UP_PCT and end_up_pct >= MISSED_REVIEW_MIN_END_UP_PCT)
    )
    if not qualifies_move:
        return "weak"
    if end_up_pct <= MISSED_REVIEW_MAX_FALSE_SPIKE_END_DOWN_PCT and peak_fade >= MISSED_REVIEW_MAX_FALSE_SPIKE_FADE_PCT:
        return "spike"
    if best_up_pct >= MISSED_REVIEW_MIN_FAST_UP_PCT and end_up_pct < MISSED_REVIEW_MIN_TRUE_END_UP_PCT and peak_fade >= MISSED_REVIEW_MAX_FALSE_SPIKE_FADE_PCT:
        return "spike"
    if end_up_pct >= MISSED_REVIEW_MIN_KEEP_AFTER_SPIKE_END_PCT:
        return "good"
    if best_up_pct >= MISSED_REVIEW_MIN_BEST_UP_PCT and end_up_pct >= MISSED_REVIEW_MIN_END_UP_PCT and worst_down_pct > -1.60:
        return "good"
    return "weak"

def get_missed_review_window_stats(ticker: str, base_price: float):
    stats = {
        "best_up_pct": 0.0,
        "end_up_pct": 0.0,
        "worst_down_pct": 0.0,
        "best_price": base_price,
        "end_price": base_price,
    }
    if base_price <= 0:
        return stats
    try:
        df = get_ohlcv(ticker, "minute1")
        if df is None or len(df) < 8:
            current_price = get_price(ticker)
            if current_price > 0:
                stats["end_price"] = current_price
                stats["end_up_pct"] = ((current_price - base_price) / base_price) * 100.0
            return stats
        recent = df.tail(min(len(df), 35))
        high_price = safe_float(recent["high"].max(), base_price)
        low_price = safe_float(recent["low"].min(), base_price)
        end_price = safe_float(recent["close"].iloc[-1], base_price)
        stats["best_price"] = high_price
        stats["end_price"] = end_price
        stats["best_up_pct"] = ((high_price - base_price) / base_price) * 100.0 if base_price > 0 else 0.0
        stats["end_up_pct"] = ((end_price - base_price) / base_price) * 100.0 if base_price > 0 else 0.0
        stats["worst_down_pct"] = ((low_price - base_price) / base_price) * 100.0 if base_price > 0 else 0.0
    except Exception:
        return stats
    return stats

def classify_missed_review_type(row: dict) -> str:
    if bool(row.get("alert_sent", False)) and str(row.get("alert_section", "") or "") in {"late", "watch"}:
        return "분류 놓침"
    if bool(row.get("seen_signal", False)) or bool(row.get("seen_shortlist", False)):
        return "승격 놓침"
    return "탐색 놓침"

def build_missed_review_reason_text(row: dict, miss_type: str) -> str:
    alert_section = str(row.get("alert_section", "") or "")
    block_reason = str(row.get("quality_block_reason", "") or "").strip()
    if miss_type == "분류 놓침":
        if alert_section == "late":
            return "좋은 자리였는데 늦은 후보로 내려버림"
        if alert_section == "watch":
            return "좋은 자리였는데 감시만 할 후보로만 둠"
        return "좋은 자리였는데 보수적으로 분류함"
    if miss_type == "승격 놓침":
        if block_reason:
            return f"상위 후보까진 봤는데 여기서 못 올림: {block_reason}"
        return "상위 후보까진 봤는데 카드 알림으로 못 올림"
    if bool(row.get("seen_hot_rank", False)):
        return "급등/거래량 순위엔 올라왔는데 본선 후보 단계까지 못 잡음"
    return "시장 흐름은 있었는데 본선 후보 단계까지 못 잡음"

def maybe_send_missed_review_alert(row: dict):
    global last_missed_review_alert_ts
    if not MISSED_REVIEW_AUTO_ALERT_ON:
        return
    best_up_pct = safe_float(row.get("best_up_pct", 0), 0)
    if best_up_pct < MISSED_REVIEW_AUTO_ALERT_MIN_PCT:
        return
    now_ts = time.time()
    if now_ts - last_missed_review_alert_ts < MISSED_REVIEW_AUTO_ALERT_COOLDOWN_SEC:
        return
    ticker = row.get("ticker", "?")
    miss_type = row.get("miss_type", "탐색 놓침")
    lines = [
        f"🧪 놓친 좋은 코인 감지: {ticker}",
        f"- 분류: {miss_type}",
        f"- 순위: {format_rank_line_for_missed_review(row)}",
        f"- 시작흐름: 5분 {safe_float(row.get('chg_5m', 0), 0):+.2f}% / 30분 {safe_float(row.get('chg_30m', 0), 0):+.2f}% / 저점대비 {safe_float(row.get('from_30m_low_pct', 0), 0):+.2f}% / 최근고점까지 {format_missed_review_high_gap_text(row)}",
        f"- 결과: 30분 안 최고 {safe_float(row.get('best_up_pct', 0), 0):+.2f}% / 끝 {safe_float(row.get('end_up_pct', 0), 0):+.2f}%",
        f"- 이유: {row.get('reason_text', '')}",
    ]
    send("\n".join(lines))
    last_missed_review_alert_ts = now_ts

def process_missed_review_queue(now_ts: float = None):
    global missed_review_queue
    if not MISSED_REVIEW_ON:
        return
    now_ts = safe_float(now_ts, time.time())
    cleanup_missed_review_queue(now_ts)
    due = []
    for ticker, row in list(missed_review_queue.items()):
        if safe_float(row.get("review_ts", 0), 0) <= now_ts:
            due.append((ticker, row))
    changed = False
    for ticker, row in due:
        base_price = safe_float(row.get("base_price", 0), 0)
        ok_spot, _spot_reason = initial_spot_good_enough_for_missed_review(row)
        stats = get_missed_review_window_stats(ticker, base_price)
        best_up_pct = safe_float(stats.get("best_up_pct", 0), 0)
        end_up_pct = safe_float(stats.get("end_up_pct", 0), 0)
        row["best_up_pct"] = round(best_up_pct, 2)
        row["end_up_pct"] = round(end_up_pct, 2)
        row["worst_down_pct"] = round(safe_float(stats.get("worst_down_pct", 0), 0), 2)
        row["logged_at"] = now_ts
        result_quality = classify_missed_review_result_quality(best_up_pct, end_up_pct, safe_float(row.get("worst_down_pct", 0), 0))
        row["result_quality"] = result_quality
        was_caught = bool(row.get("alert_sent", False)) and str(row.get("alert_section", "") or "") in {"early", "live", "restart"}
        if ok_spot and result_quality == "good" and not was_caught:
            miss_type = classify_missed_review_type(row)
            row["miss_type"] = miss_type
            row["reason_text"] = build_missed_review_reason_text(row, miss_type)
            if not has_recent_same_missed_review(row):
                save_missed_review_log(dict(row))
                maybe_send_missed_review_alert(row)
        missed_review_queue.pop(ticker, None)
        changed = True
    if changed:
        persist_missed_review_state()

def format_rank_line_for_missed_review(row: dict) -> str:
    parts = []
    if int(safe_float(row.get("change_1_rank", 0), 0)) > 0:
        parts.append(f"1분급등 {int(safe_float(row.get('change_1_rank',0),0))}위")
    if int(safe_float(row.get("change_5_rank", 0), 0)) > 0:
        parts.append(f"5분급등 {int(safe_float(row.get('change_5_rank',0),0))}위")
    if int(safe_float(row.get("vol_ratio_rank", 0), 0)) > 0:
        parts.append(f"거래량 {int(safe_float(row.get('vol_ratio_rank',0),0))}위")
    if int(safe_float(row.get("turnover_rank", 0), 0)) > 0:
        parts.append(f"거래대금 {int(safe_float(row.get('turnover_rank',0),0))}위")
    elif int(safe_float(row.get("turnover_rank_pulse", 0), 0)) > 0:
        parts.append(f"거래대금 {int(safe_float(row.get('turnover_rank_pulse',0),0))}위")
    if int(safe_float(row.get("surge_rank", 0), 0)) > 0:
        parts.append(f"시장주목 {int(safe_float(row.get('surge_rank',0),0))}위")
    return " / ".join(parts) if parts else "상위 순위권 기록 없음"

def format_missed_review_high_gap_text(row: dict) -> str:
    vals = [
        safe_float(row.get("high_gap_pct", 999), 999),
        safe_float(row.get("below_30m_high_pct", 999), 999),
        safe_float(row.get("chart_gap_pct", 999), 999),
    ]
    usable = [v for v in vals if 0 <= v < 900]
    if not usable:
        return "기준 없음"
    return f"{min(usable):.2f}%"

def format_missed_review_log_block(row: dict, idx: int = 0) -> str:
    prefix = f"{idx}. " if idx else "- "
    ticker = row.get("ticker", "?")
    miss_type = row.get("miss_type", "놓침")
    return "\n".join([
        f"{prefix}{ticker} / {miss_type}",
        f"- 순위: {format_rank_line_for_missed_review(row)}",
        f"- 시작흐름: 5분 {safe_float(row.get('chg_5m', 0), 0):+.2f}% / 30분 {safe_float(row.get('chg_30m', 0), 0):+.2f}% / 저점대비 {safe_float(row.get('from_30m_low_pct', 0), 0):+.2f}% / 최근고점까지 {format_missed_review_high_gap_text(row)}",
        f"- 결과: 30분 안 최고 {safe_float(row.get('best_up_pct', 0), 0):+.2f}% / 끝 {safe_float(row.get('end_up_pct', 0), 0):+.2f}%",
        f"- 해석: {row.get('reason_text', '')}",
    ])

def append_missed_review_parts(parts: list, limit: int = 2):
    rows = [x for x in missed_review_logs if isinstance(x, dict)][-max(1, int(limit)):]
    if not rows:
        return
    rows = list(reversed(rows))
    lines = ["🧪 최근 놓친 좋은 코인"]
    for idx, row in enumerate(rows, start=1):
        lines.append("")
        lines.append(format_missed_review_log_block(row, idx=idx))
    parts.append("\n".join(lines))

def summarize_missed_review_logs(rows):
    bucket = {"탐색 놓침": 0, "승격 놓침": 0, "분류 놓침": 0}
    for row in rows:
        if not isinstance(row, dict):
            continue
        miss_type = str(row.get("miss_type", "") or "")
        bucket[miss_type] = bucket.get(miss_type, 0) + 1
    return bucket

def missed_command(update, context: CallbackContext):
    process_missed_review_queue(time.time())
    rows = [x for x in missed_review_logs if isinstance(x, dict)]
    if not rows:
        send("🧪 아직 자동으로 잡힌 놓친 좋은 코인은 없어")
        return
    recent = rows[-5:]
    summary = summarize_missed_review_logs(rows[-20:])
    lines = ["🧪 놓친 좋은 코인 보기", "", "[최근 20건 요약]", f"- 탐색 놓침 {summary.get('탐색 놓침', 0)}건", f"- 승격 놓침 {summary.get('승격 놓침', 0)}건", f"- 분류 놓침 {summary.get('분류 놓침', 0)}건", "", "[최근 5건]"]
    for idx, row in enumerate(reversed(recent), start=1):
        lines.append("")
        lines.append(format_missed_review_log_block(row, idx=idx))
    send("\n".join(lines))

def missed_reset_command(update, context: CallbackContext):
    clear_missed_review_state()
    send(
        "🧹 놓친 코인 기록 초기화 완료\n\n"
        "- missed_review_queue 초기화\n"
        "- missed_review_logs 초기화\n"
        "- missed_review_meta 초기화\n"
        "- 시간별 missed 요약 기준도 새로 시작\n\n"
        "이제 새 버전 기준 표본만 다시 쌓여."
    )

def signal_score(signal):
    return float(signal.get("edge_score", signal.get("signal_score", 0)))
def watch_overheat_block(df, current_price, change_3, change_5, vol_ratio):
    if df is None or len(df) < 12 or current_price <= 0:
        return False, ""
    recent_high = get_recent_high(df, 8, exclude_last=True)
    dist_to_high_pct = 999.0
    if recent_high > 0:
        dist_to_high_pct = ((recent_high - current_price) / current_price) * 100
    recent_hot_high = get_recent_high(df, 6, exclude_last=False)
    hot_spike_gap_pct = ((recent_hot_high - current_price) / current_price) * 100 if recent_hot_high > 0 else 0.0
    if is_tiny_tick_distortion(df, current_price):
        return True, "초저가 호가 움직임만으로 상승률이 과장돼 보여"
    if is_recent_spike_near_high(df, current_price):
        return True, "이미 한 번 급하게 오른 뒤 고점 근처라 초반 자리로 보기 어려워"
    if is_box_top_range_chase(df, current_price):
        return True, "박스권 상단/고점 근처라 오르기 전에 뜬 후보로 보기 어려워"
    if is_retest_after_failed_peak(df):
        return True, "최근 고점 찍고 밀린 뒤 재반등이라 초반 자리 아님"
    if hot_spike_gap_pct >= 3.50 and change_5 < 3.0 and vol_ratio < 6.0:
        return True, f"최근 급하게 튄 뒤 다시 식는 흐름이라 초반형으로 보기 어려워 ({hot_spike_gap_pct:.2f}%)"
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
            if is_watch_excluded_ticker(ticker):
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
            chart_profile = compute_chart_position_profile(df, current_price, lookback=CHART_POS_LOOKBACK_1M)
            if chart_profile.get("chart_gap_pct", 999) <= 0.45:
                continue
            if chart_profile.get("chart_zone_ratio", -1) >= CHART_POS_HIGH_ZONE_RATIO and chart_profile.get("chart_rise_pct", 0) >= 2.2 and chart_profile.get("chart_gap_pct", 999) <= CHART_POS_EXTENDED_NEAR_HIGH_PCT:
                continue
            if chart_profile.get("chart_rise_pct", 0) >= 3.10 and chart_profile.get("chart_gap_pct", 999) <= 2.10 and change_5 >= 1.20:
                continue
            if max(change_5, change_3, 0) >= 4.20 and chart_profile.get("chart_gap_pct", 999) <= 2.10:
                continue
            if change_5 >= 1.80 and max(change_3, 0) >= 1.00 and chart_profile.get("chart_gap_pct", 999) <= 2.40:
                continue
            if change_5 <= 0.18 and (change_3 <= 0.10 or vol_ratio < 2.20):
                continue
            label = "실시간 감시형"
            if change_5 >= 0.60 and change_3 >= 0.18 and vol_ratio >= 4.00 and leader_score >= 1.00 and chart_profile.get("chart_gap_pct", 999) >= 0.95 and chart_profile.get("chart_rise_pct", 0) <= 3.20:
                label = "실시간 강세형"
            elif top_rank_ok and change_5 >= 0.35 and vol_ratio >= 2.40:
                label = "실시간 주도형"
            candidate = make_signal(
                ticker=ticker,
                strategy="LEADER_WATCH",
                strategy_label=label,
                current_price=current_price,
                vol_ratio=max(vol_ratio, 0.85),
                change_pct=max(change_1, change_3, change_5),
                rsi=rsi,
                range_pct=max(range_pct, 0.08),
                signal_score=max(4.6, activity_score + 4.2),
                edge_score=max(4.4, activity_score + 3.8),
                leader_score=leader_score,
                stop_loss_pct=-1.4,
                take_profit_pct=3.6,
                time_stop_sec=600,
                pattern_tags=[],
                reason=f"테스트 완화 후보 / 5분상승 {change_5:.2f}% / 거래량 {vol_ratio:.2f}배 / 주도 {leader_score:.2f} / 순위 T{turnover_rank}/S{surge_rank}",
            )
            candidate.update(chart_profile)
            if not has_meaningful_watch_motion(candidate):
                continue
            quick.append(candidate)
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
PREWATCH_ALERT_COOLDOWN_SEC = 18 * 60
NEWLOW_ALERT_COOLDOWN_SEC = 20 * 60
PREWATCH_ALERT_MAX = 1
NEWLOW_ALERT_MAX = 1
AUX_WATCH_ALERT_MAX = 1
SCAN_WATCHLIST_TOP_LIMIT = 10
SCAN_WATCHLIST_HEAVY_REFRESH_TOP = 3
SCAN_WATCHLIST_MAX_SEC = 24
SCAN_WATCHLIST_AUX_MAX_SEC = 6
SCAN_WATCHLIST_HEARTBEAT_EVERY = 1
SCAN_WATCHLIST_PREWATCH_LIMIT = 1
SCAN_WATCHLIST_NEWLOW_LIMIT = 1
SCAN_WATCHLIST_CACHE_LIMIT = 18
SCAN_WATCHLIST_SIGNAL_CACHE_LIMIT = 16
SCAN_WATCHLIST_MARKET_CACHE_LIMIT = 24
SCAN_WATCHLIST_PREPARE_MAX_SEC = 5
SCAN_WATCHLIST_PREPARE_MISSED_LIMIT = 18
SCAN_WATCHLIST_FALLBACK_LIMIT = 5
SCAN_WATCHLIST_COLLECT_HEARTBEAT_EVERY = 3
SCAN_WATCHLIST_MARKET_REGISTER_INTERVAL_SEC = 45
recent_prewatch_alerts = {}
recent_newlow_alerts = {}
last_watchlist_market_register_ts = 0.0
def build_prewatch_candidates(cache, regime, exclude_tickers=None):
    preview = []
    exclude_tickers = set(exclude_tickers or [])
    regime_name = (regime or {}).get("name", "NORMAL")
    for ticker, data in cache.items():
        try:
            if ticker in exclude_tickers:
                continue
            df = data.get("df_1m")
            if df is None or len(df) < 20:
                continue
            current_price = safe_float(data.get("price", 0))
            if current_price <= 0:
                continue
            if is_watch_excluded_ticker(ticker):
                continue
            if is_tiny_tick_distortion(df, current_price):
                continue
            change_1 = safe_float(data.get("change_1", 0))
            change_3 = safe_float(data.get("change_3", 0))
            change_5 = safe_float(data.get("change_5", 0))
            vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0))) or get_vol_ratio(df, 3, 15)
            rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50))) or get_rsi(df, 14)
            range_pct = safe_float(data.get("range_pct", data.get("range_pct_1m", 0))) or get_range_pct(df, 10)
            leader_score = safe_float(data.get("leader_score", 0))
            turnover_rank = int(safe_float(data.get("turnover_rank", 999), 999))
            surge_rank = int(safe_float(data.get("surge_rank", 999), 999))
            reference_high = get_recent_high(df, 12, exclude_last=True)
            if reference_high <= 0:
                continue
            high_gap_pct = ((reference_high - current_price) / current_price) * 100.0 if current_price > 0 else 999.0
            recent_low = get_recent_low(df, 12, exclude_last=False)
            rebound_from_low_pct = ((current_price - recent_low) / recent_low) * 100.0 if recent_low > 0 else 0.0
            hl = has_higher_lows(df)
            box = detect_box_top_compression(df)
            half_hold = big_bull_half_hold(df)
            flip_count = direction_flip_count(df, 12)
            momentum_hint = max(change_1, change_3 * 0.85, change_5 * 0.65)
            if change_5 >= 2.0 or change_3 >= 1.4:
                continue
            if change_1 <= -0.40 and change_3 <= -0.10:
                continue
            if max(change_1, change_3, change_5) < 0.08:
                continue
            if momentum_hint <= 0.08:
                continue
            if not (1.75 <= vol_ratio <= 5.8):
                continue
            if not (47 <= rsi <= 66):
                continue
            if not (0.55 <= high_gap_pct <= 2.6):
                continue
            if range_pct >= 3.0:
                continue
            if regime_name == "WEAK" and vol_ratio < 2.40:
                continue
            if leader_score < 0.85 and turnover_rank > 40 and surge_rank > 40:
                continue
            if is_recent_spike_near_high(df, current_price) or is_box_top_range_chase(df, current_price) or is_retest_after_failed_peak(df) or is_false_start_near_high(df, current_price):
                continue
            if rebound_from_low_pct >= 1.55 and high_gap_pct <= 0.95:
                continue
            if flip_count >= 6 and not box and change_5 < 0.55:
                continue
            if flip_count >= 7 and range_pct >= 1.35:
                continue
            if not (hl or box or half_hold):
                continue
            if leader_score < 0.95 and vol_ratio < 2.45:
                continue
            tags = []
            if hl:
                tags.append("저점높임")
            if box:
                tags.append("상단압축")
            if half_hold:
                tags.append("양봉절반유지")
            score = 4.2 + max(vol_ratio - 1.0, 0) * 0.55 + max(change_3, 0) * 0.28 + max(leader_score, 0) * 0.35
            if score < 5.05:
                continue
            candidate = make_signal(
                ticker=ticker,
                strategy="PREWATCH",
                strategy_label="예비 후보 감시",
                current_price=current_price,
                vol_ratio=vol_ratio,
                change_pct=max(change_1, change_3, change_5),
                rsi=rsi,
                range_pct=range_pct,
                signal_score=max(4.0, score),
                edge_score=max(3.9, score),
                leader_score=leader_score,
                stop_loss_pct=-1.2,
                take_profit_pct=3.0,
                time_stop_sec=900,
                pattern_tags=tags,
                reference_high=reference_high,
                reference_low=recent_low,
                reason=f"예비 감시 / 고점까지 {high_gap_pct:.2f}% 여유 / 저가에서 {rebound_from_low_pct:.2f}% / 거래량 {vol_ratio:.2f}배",
            )
            apply_chart_position_profile(candidate, df, lookback=CHART_POS_LOOKBACK_1M)
            if not has_meaningful_watch_motion(candidate):
                continue
            preview.append(candidate)
        except Exception:
            continue
    preview.sort(key=lambda x: (safe_float(x.get("edge_score", 0)) + safe_float(x.get("leader_score", 0)) * 0.8), reverse=True)
    return preview[:4]
def build_newlow_recovery_candidates(cache, regime, exclude_tickers=None):
    recovered = []
    exclude_tickers = set(exclude_tickers or [])
    regime_name = (regime or {}).get("name", "NORMAL")
    for ticker, data in cache.items():
        try:
            if ticker in exclude_tickers:
                continue
            df = data.get("df_1m")
            if df is None or len(df) < 24:
                continue
            current_price = safe_float(data.get("price", 0))
            if current_price <= 0 or current_price <= 1.0:
                continue
            if is_tiny_tick_distortion(df, current_price):
                continue
            recent = df.tail(14)
            recent_low = float(recent["low"].min())
            if recent_low <= 0:
                continue
            recent_high = float(recent["high"].max())
            low_gap_pct = ((current_price - recent_low) / recent_low) * 100.0
            pullback_from_peak_pct = ((recent_high - current_price) / recent_high) * 100.0 if recent_high > 0 else 0.0
            change_1 = safe_float(data.get("change_1", 0))
            change_3 = safe_float(data.get("change_3", 0))
            change_5 = safe_float(data.get("change_5", 0))
            vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0))) or get_vol_ratio(df, 3, 15)
            rsi = safe_float(data.get("rsi", data.get("rsi_1m", 50))) or get_rsi(df, 14)
            range_pct = safe_float(data.get("range_pct", data.get("range_pct_1m", 0))) or get_range_pct(df, 10)
            leader_score = safe_float(data.get("leader_score", 0))
            reference_high = get_recent_high(df, 12, exclude_last=True)
            high_gap_pct = ((reference_high - current_price) / current_price) * 100.0 if reference_high > 0 and current_price > 0 else 999.0
            last = recent.iloc[-1]
            prev = recent.iloc[-2]
            bullish_reclaim = float(last["close"]) >= float(last["open"]) and float(last["close"]) >= float(prev["close"]) * 0.998
            fake_recovery = fake_breakdown_recovery(df)
            lower_low_recent = float(prev["low"]) <= recent_low * 1.004 or float(last["low"]) <= recent_low * 1.004
            flip_count = direction_flip_count(df, 12)
            if regime_name == "WEAK" and vol_ratio < 2.10:
                continue
            if change_5 <= -3.2 or change_5 >= 2.8:
                continue
            if change_1 < 0.03 and change_3 < 0.10:
                continue
            if vol_ratio < 1.95:
                continue
            if leader_score < 0.85 and vol_ratio < 2.60:
                continue
            if not (0.20 <= low_gap_pct <= 1.35):
                continue
            if high_gap_pct <= 0.70:
                continue
            if range_pct >= 2.8:
                continue
            if pullback_from_peak_pct >= 0.75 and change_1 <= 0.12:
                continue
            if flip_count >= 7 and range_pct >= 1.4:
                continue
            if not (fake_recovery or (bullish_reclaim and lower_low_recent and change_1 >= 0.03 and change_3 >= 0.08)):
                continue
            if is_recent_spike_near_high(df, current_price) or is_box_top_range_chase(df, current_price) or is_false_start_near_high(df, current_price):
                continue
            tags = ["저가회복"]
            if fake_recovery:
                tags.append("가짜하락회복")
            if has_higher_lows(df):
                tags.append("저점높임")
            score = 4.4 + max(vol_ratio - 1.0, 0) * 0.50 + max(change_1, 0) * 0.25 + max(leader_score, 0) * 0.25
            if score < 5.00:
                continue
            candidate = make_signal(
                ticker=ticker,
                strategy="NEWLOW_RECOVER",
                strategy_label="신저가 반등 감시",
                current_price=current_price,
                vol_ratio=vol_ratio,
                change_pct=max(change_1, change_3, change_5),
                rsi=rsi,
                range_pct=range_pct,
                signal_score=max(4.2, score),
                edge_score=max(4.1, score),
                leader_score=leader_score,
                stop_loss_pct=-1.0,
                take_profit_pct=3.2,
                time_stop_sec=720,
                pattern_tags=tags,
                reference_high=reference_high,
                reference_low=recent_low,
                reason=f"최근 저가권 회복 / 저가에서 {low_gap_pct:.2f}% 회복 / 되밀림 {pullback_from_peak_pct:.2f}% / 거래량 {vol_ratio:.2f}배",
            )
            apply_chart_position_profile(candidate, df, lookback=CHART_POS_LOOKBACK_1M)
            if not has_meaningful_watch_motion(candidate):
                continue
            recovered.append(candidate)
        except Exception:
            continue
    recovered.sort(key=lambda x: (safe_float(x.get("edge_score", 0)) + safe_float(x.get("vol_ratio", 0)) * 0.2), reverse=True)
    return recovered[:4]
def should_send_simple_aux_alert(kind_dict: dict, ticker: str, now_ts: float, cooldown_sec: int):
    prev = safe_float(kind_dict.get(ticker, 0), 0)
    if now_ts - prev < cooldown_sec:
        return False
    kind_dict[ticker] = now_ts
    return True
def is_good_market_for_user_rescue(regime: dict = None) -> bool:
    regime = regime or get_market_regime()
    name = str((regime or {}).get("name", "NORMAL") or "NORMAL")
    if name == "STRONG_UP":
        return True
    day_change = safe_float((regime or {}).get("day_change_pct", 0), 0)
    mid_change = safe_float((regime or {}).get("mid_change_pct", 0), 0)
    return day_change >= USER_ALERT_RESCUE_GOOD_MARKET_DAY_PCT and mid_change >= USER_ALERT_RESCUE_GOOD_MARKET_MID_PCT


def get_effective_high_gap_pct(item: dict) -> float:
    snap = ensure_pre_alert_snapshot(item)
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    vals = [
        get_reference_high_gap_pct(item),
        safe_float(snap.get("below_30m_high_pct", 999), 999),
        safe_float(profile.get("chart_gap_pct", 999), 999),
    ]
    usable = [v for v in vals if 0 <= v < 900]
    if not usable:
        return 999.0
    return min(usable)

def get_user_alert_hot_score(item: dict) -> int:
    info = get_market_pulse_rank_info(item.get("ticker", ""))
    change_1_rank = int(safe_float(item.get("change_1_rank", info.get("change_1_rank", 0)), 0))
    change_5_rank = int(safe_float(item.get("change_5_rank", info.get("change_5_rank", 0)), 0))
    vol_ratio_rank = int(safe_float(item.get("vol_ratio_rank", info.get("vol_ratio_rank", 0)), 0))
    turnover_rank = int(safe_float(item.get("turnover_rank_pulse", info.get("turnover_rank_pulse", 0)), 0))
    if turnover_rank <= 0:
        turnover_rank = int(safe_float(item.get("turnover_rank", 0), 0))
    surge_rank = int(safe_float(item.get("surge_rank", 0), 0))
    leader_rank = int(safe_float(item.get("leader_rank", info.get("leader_rank", 0)), 0))
    shortlist_rank = int(safe_float(item.get("shortlist_rank", 999), 999))
    score = 0
    if shortlist_rank <= USER_ALERT_RESCUE_MIN_SHORTLIST_RANK_STRONG:
        score += 2
    elif shortlist_rank <= USER_ALERT_RESCUE_SHORTLIST_RANK_MAX:
        score += 1
    if 0 < change_1_rank <= 10:
        score += 1
    if 0 < change_5_rank <= 12:
        score += 1
    if 0 < vol_ratio_rank <= 12:
        score += 1
    if 0 < turnover_rank <= 20:
        score += 1
    if 0 < surge_rank <= 20:
        score += 1
    if 0 < leader_rank <= 20:
        score += 1
    return score

def get_shortlist_user_rescue_display_key(item: dict, display_key: str = "", regime: dict = None) -> str:
    if not USER_ALERT_RESCUE_ON:
        return ""
    regime = regime or (item.get("market_regime") if isinstance(item.get("market_regime"), dict) else None) or get_market_regime()
    if not is_good_market_for_user_rescue(regime=regime):
        return ""
    shortlist_rank = int(safe_float(item.get("shortlist_rank", 999), 999))
    if shortlist_rank > USER_ALERT_RESCUE_SHORTLIST_RANK_MAX:
        return ""
    snap = ensure_pre_alert_snapshot(item)
    high_gap_pct = get_effective_high_gap_pct(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    edge = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader = safe_float(item.get("leader_score", 0), 0)
    chart_rise = safe_float(get_item_chart_profile(item).get("chart_rise_pct", 0), 0)
    hot_score = get_user_alert_hot_score(item)
    if hot_score < USER_ALERT_RESCUE_MIN_HOT_SCORE:
        return ""
    if high_gap_pct < 0.50:
        return ""
    if from_low > 3.25 and high_gap_pct < 1.20:
        return ""
    if chart_rise > 4.80 and high_gap_pct < 1.60:
        return ""
    if edge < USER_ALERT_RESCUE_MIN_EDGE and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return ""
    if vol_ratio < USER_ALERT_RESCUE_MIN_VOL and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return ""
    if leader < USER_ALERT_RESCUE_MIN_LEADER and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return ""
    if chg_5 >= 0.38 and high_gap_pct >= 0.80 and (chg_30 >= 0.00 or vol_ratio >= 1.80 or leader >= 1.00 or hot_score >= USER_ALERT_RESCUE_STRONG_HOT_SCORE):
        return "live"
    if max(chg_5, chg_30, from_low) >= 0.05 and high_gap_pct >= 0.55:
        return "restart"
    return ""

def is_user_alert_rescue_candidate(item: dict, section: str = "", regime: dict = None) -> bool:
    if not USER_ALERT_RESCUE_ON:
        return False
    regime = regime or (item.get("market_regime") if isinstance(item.get("market_regime"), dict) else None) or get_market_regime()
    section = str(section or get_watch_display_group(item, regime=regime)).strip()
    if section not in {"live", "restart", "early"}:
        return False
    shortlist_rank = int(safe_float(item.get("shortlist_rank", 999), 999))
    if shortlist_rank > USER_ALERT_RESCUE_SHORTLIST_RANK_MAX:
        return False
    if get_shortlist_user_rescue_display_key(item, display_key=section, regime=regime):
        return True
    snap = ensure_pre_alert_snapshot(item)
    high_gap_pct = get_effective_high_gap_pct(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    edge = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader = safe_float(item.get("leader_score", 0), 0)
    chart_rise = safe_float(get_item_chart_profile(item).get("chart_rise_pct", 0), 0)
    trade_tier = get_watch_tier_text(item)
    hot_score = get_user_alert_hot_score(item)
    chart_ctx = ensure_alert_chart_context(item)

    if not is_good_market_for_user_rescue(regime=regime):
        return False
    if str(chart_ctx.get("chart_state_code", "")) in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE"} and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    if high_gap_pct < USER_ALERT_RESCUE_MIN_HIGH_GAP_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    if from_low > USER_ALERT_RESCUE_MAX_FROM_LOW_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    if chart_rise > 4.40 and high_gap_pct < 1.60:
        return False
    if edge < USER_ALERT_RESCUE_MIN_EDGE and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    if vol_ratio < USER_ALERT_RESCUE_MIN_VOL and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    if leader < USER_ALERT_RESCUE_MIN_LEADER and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False

    if section == "live":
        if chg_5 < USER_ALERT_RESCUE_MIN_LIVE_5M_PCT and chg_30 < 0.10 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
            return False
        if trade_tier == "B" and high_gap_pct < 0.70 and from_low > 2.35 and chg_5 < 0.30 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
            return False
        return True

    if section == "restart":
        if max(chg_5, chg_30, from_low) < USER_ALERT_RESCUE_MIN_RESTART_MOTION_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
            return False
        return True

    # early rescue는 너무 약한 흐름은 막고, 상위 shortlist + 차트 여유가 있으면 통과
    if high_gap_pct < 0.55 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    if max(chg_5, chg_30, from_low) < 0.05 and vol_ratio < 1.05 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE:
        return False
    return True

def get_user_alert_block_reason(item: dict, display_key: str = "") -> str:
    if not USER_ALERT_FILTER_ON:
        return ""
    section = str(display_key or get_watch_display_group(item)).strip()
    regime = item.get("market_regime") if isinstance(item.get("market_regime"), dict) else None
    if section in USER_ALERT_HIDE_SECTIONS:
        if is_user_alert_rescue_candidate(item, section="restart", regime=regime):
            return ""
        return "이미 늦었거나 감시만 할 후보라 사용자 알림에서 제외"
    if section not in USER_ALERT_ALLOW_SECTIONS:
        return "지금 직접 볼 가치가 낮아서 사용자 알림에서 제외"
    snap = ensure_pre_alert_snapshot(item)
    high_gap_pct = get_effective_high_gap_pct(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    chart_rise = safe_float(get_item_chart_profile(item).get("chart_rise_pct", 0), 0)
    edge = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader = safe_float(item.get("leader_score", 0), 0)
    trade_tier = get_watch_tier_text(item)
    strategy = str(item.get("strategy", "") or "").strip()
    hot_score = get_user_alert_hot_score(item)
    chart_ctx = ensure_alert_chart_context(item)
    user_grade = str(item.get("user_alert_grade", "") or "").upper().strip()
    if user_grade not in {"S", "A", "B", "C"}:
        user_grade = classify_user_alert_grade(item, display_key=section, regime=regime)
        item["user_alert_grade"] = user_grade
    rescue_ok = is_user_alert_rescue_candidate(item, section=section, regime=regime)
    if section == "live":
        if high_gap_pct < USER_ALERT_MIN_LIVE_HIGH_GAP_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
            return "실시간 강세로 보기엔 바로 위 고점이 너무 가까움"
        if from_low > USER_ALERT_MAX_LIVE_FROM_LOW_PCT and high_gap_pct < 1.70 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
            return "실시간 강세 후보인데 저점에서 이미 꽤 올라와서 제외"
        if chart_rise > USER_ALERT_MAX_LIVE_CHART_RISE_PCT and high_gap_pct < 2.00 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
            return "실시간 강세 후보인데 차트상 이미 위쪽 자리라 제외"
        if chg_5 < USER_ALERT_LIVE_MIN_5M_PCT and chg_30 < USER_ALERT_LIVE_MIN_30M_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
            return "실시간 강세라기엔 방금 붙는 힘이 약함"
        if chg_30 <= USER_ALERT_LIVE_MIN_30M_PCT and from_low >= 1.30 and high_gap_pct < 1.45 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
            return "실시간 강세 후보인데 이미 조금 오른 뒤라 사용자 알림에선 제외"
        if trade_tier == "B":
            if high_gap_pct <= USER_ALERT_LIVE_B_HARD_BLOCK_NEAR_HIGH_PCT and from_low >= USER_ALERT_LIVE_B_HARD_BLOCK_FROM_LOW_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
                return "실시간 강세 B인데 이미 조금 오른 뒤라 지금 볼 가치가 낮음"
            if chg_30 < 0 and from_low >= USER_ALERT_LIVE_NEG30_BLOCK_FROM_LOW_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
                return "실시간 강세 B인데 30분 흐름이 꺾여서 사용자 알림에선 제외"
            fail_count = 0
            if edge < USER_ALERT_MIN_LIVE_EDGE:
                fail_count += 1
            if vol_ratio < USER_ALERT_MIN_LIVE_VOL:
                fail_count += 1
            if leader < USER_ALERT_MIN_LIVE_LEADER:
                fail_count += 1
            if fail_count >= (USER_ALERT_LIVE_B_FAIL_COUNT_MIN + (1 if hot_score <= USER_ALERT_RESCUE_MIN_HOT_SCORE else 0)) and not rescue_ok:
                return "실시간 강세 B지만 아직 해볼만한 수준까진 아님"
        if user_grade in {"B", "C"} and not rescue_ok:
            return "실시간 강세지만 차트 기준으론 아직 해볼만한 단계까진 아님"
    if section != "restart":
        return ""
    if str(chart_ctx.get("chart_state_code", "")) == "FADE_AFTER_SPIKE" and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
        return "재출발이라기보다 고점 뒤 밀리는 자리라 사용자 알림에서 제외"
    if high_gap_pct < USER_ALERT_MIN_RESTART_HIGH_GAP_PCT and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
        return "재출발로 보기엔 바로 위 고점이 너무 가까움"
    if from_low > USER_ALERT_MAX_RESTART_FROM_LOW_PCT and high_gap_pct < 1.25 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
        return "재출발 후보인데 저점에서 이미 꽤 올라와서 제외"
    if chart_rise > USER_ALERT_MAX_RESTART_CHART_RISE_PCT and high_gap_pct < 1.80 and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
        return "재출발 후보인데 차트상 이미 위쪽 자리라 제외"
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"} and USER_ALERT_PREWATCH_STRICT_ON:
        if edge < max(USER_ALERT_MIN_RESTART_EDGE, 5.65) or vol_ratio < max(USER_ALERT_MIN_RESTART_VOL, 1.70) or leader < max(USER_ALERT_MIN_RESTART_LEADER, 1.10):
            if not rescue_ok:
                return "감시용 재출발 후보라 지금 직접 볼 가치가 아직 낮음"
    if trade_tier == "B" and edge < USER_ALERT_MIN_RESTART_EDGE and vol_ratio < USER_ALERT_MIN_RESTART_VOL and leader < USER_ALERT_MIN_RESTART_LEADER and hot_score < USER_ALERT_RESCUE_STRONG_HOT_SCORE and not rescue_ok:
        return "재출발 후보지만 아직 해볼만한 수준까진 아님"
    if user_grade in {"B", "C"} and not rescue_ok:
        return "재출발 감시용까진 되지만 아직 직접 볼 단계까진 아님"
    return ""

def get_user_visible_display_key(item: dict, display_key: str = "", regime: dict = None) -> str:
    key = str(display_key or get_watch_display_group(item, regime=regime)).strip()
    rescue_key = get_shortlist_user_rescue_display_key(item, display_key=key, regime=regime)
    item["user_alert_grade"] = classify_user_alert_grade(item, display_key=key, regime=regime)
    if rescue_key:
        return rescue_key
    if not USER_ALERT_ALLOW_HIDDEN_PROMOTION:
        return key
    if key not in {"late", "watch"}:
        return key
    snap = ensure_pre_alert_snapshot(item)
    high_gap_pct = get_reference_high_gap_pct(item)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    profile = get_item_chart_profile(item)
    chart_ctx = ensure_alert_chart_context(item)
    chart_rise = safe_float(profile.get("chart_rise_pct", from_low), from_low)
    edge = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader = safe_float(item.get("leader_score", 0), 0)
    if str(chart_ctx.get("chart_state_code", "")) in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}:
        return key
    if high_gap_pct < USER_ALERT_PROMOTE_MIN_HIGH_GAP_PCT:
        return key
    if from_low > USER_ALERT_PROMOTE_MAX_FROM_LOW_PCT:
        return key
    if chart_rise > USER_ALERT_PROMOTE_MAX_CHART_RISE_PCT and high_gap_pct < 1.90:
        return key
    if edge < USER_ALERT_PROMOTE_MIN_EDGE:
        return key
    if vol_ratio < USER_ALERT_PROMOTE_MIN_VOL:
        return key
    if leader < USER_ALERT_PROMOTE_MIN_LEADER:
        return key
    strength = 0
    if chg_5 >= USER_ALERT_PROMOTE_MIN_5M_PCT:
        strength += 1
    if chg_30 >= USER_ALERT_PROMOTE_MIN_30M_PCT:
        strength += 1
    if from_low >= 0.80:
        strength += 1
    if vol_ratio >= max(USER_ALERT_PROMOTE_MIN_VOL, 3.20):
        strength += 1
    if leader >= max(USER_ALERT_PROMOTE_MIN_LEADER, 1.30):
        strength += 1
    if strength < 4:
        return key
    if chg_5 >= 0.65 and vol_ratio >= 3.60 and high_gap_pct >= 1.20 and leader >= 1.10 and chg_30 >= 0.15:
        return "live"
    return "restart"

def should_send_main_watch_alert(item: dict) -> bool:
    judgement = get_watch_judgement_text(item)
    caution = get_watch_caution_text(item)
    return get_watch_section_key(item, judgement, caution) == "entry"
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
            reference_high = get_recent_high(df, 10, exclude_last=True)
            high_gap_pct = ((reference_high - current_price) / current_price) * 100.0 if reference_high > 0 and current_price > 0 else 999.0
            reference_low = get_recent_low(df, 10, exclude_last=False)
            chart_profile = compute_chart_position_profile(df, current_price, lookback=CHART_POS_LOOKBACK_1M)
            if chart_profile.get("chart_gap_pct", 999) <= 0.35:
                continue
            if chart_profile.get("chart_zone_ratio", -1) >= CHART_POS_HIGH_ZONE_RATIO and chart_profile.get("chart_rise_pct", 0) >= 2.2 and chart_profile.get("chart_gap_pct", 999) <= CHART_POS_EXTENDED_NEAR_HIGH_PCT and change_5 < 1.40:
                continue
            if chart_profile.get("chart_rise_pct", 0) >= 3.20 and chart_profile.get("chart_gap_pct", 999) <= 2.10 and change_5 >= 1.10:
                continue
            if max(change_5, change_3, 0) >= 4.20 and chart_profile.get("chart_gap_pct", 999) <= 2.10:
                continue
            if change_5 >= 1.80 and max(change_3, 0) >= 1.00 and chart_profile.get("chart_gap_pct", 999) <= 2.40:
                continue
            if change_5 <= 0.15 and change_3 <= 0.10 and vol_ratio < 2.80:
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
            label = "실시간 감시형"
            if change_3 >= 0.22 and change_5 >= 0.60 and vol_ratio >= 4.00 and high_gap_pct >= 0.95 and leader_score >= 1.00 and chart_profile.get("chart_rise_pct", 0) <= 3.20:
                label = "실시간 강세형"
            elif change_5 >= 0.40 and vol_ratio >= 2.40 and high_gap_pct >= 1.00:
                label = "상승 시작형"
            elif vol_ratio >= 3.00 and change_3 >= 0.25 and high_gap_pct >= 0.90:
                label = "실시간 주도형"
            elif top_rank_ok and (change_5 >= 0.18 or vol_ratio >= 1.20):
                label = "실시간 감시형"
            candidate = make_signal(
                ticker=ticker,
                strategy="LEADER_WATCH",
                strategy_label=label,
                current_price=current_price,
                vol_ratio=vol_ratio,
                change_pct=max(change_1, change_3, change_5),
                rsi=rsi,
                range_pct=range_pct,
                signal_score=max(
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
            )
            candidate.update(chart_profile)
            if not has_meaningful_watch_motion(candidate):
                continue
            fallback.append(candidate)
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
def build_watch_fallback_candidates(cache, regime=None):
    combined = []
    combined.extend(build_leader_watch_candidates(cache, regime))
    combined.extend(build_leader_watch_candidates(cache, regime, relaxed=True))
    combined.extend(build_quick_watch_candidates(cache, regime))
    if not combined:
        return []
    combined = dedupe_best_signal_per_ticker(combined, key_name="edge_score")
    combined.sort(
        key=lambda x: (
            safe_float(x.get("edge_score", 0))
            + safe_float(x.get("leader_score", 0)) * 1.0
            + safe_float(x.get("change_pct", 0)) * 0.25
            + safe_float(x.get("vol_ratio", 0)) * 0.20
        ),
        reverse=True,
    )
    return combined[:18]
def is_live_force_candidate(item: dict, regime: dict = None) -> tuple[bool, str, float]:
    if not isinstance(item, dict):
        return False, "", 0.0
    ticker = str(item.get("ticker", "") or "").upper().strip()
    if not ticker or is_watch_excluded_ticker(ticker) or is_ticker_blocked_for_watch_alert(ticker):
        return False, "", 0.0
    if WATCH_BLOCK_IF_PENDING_30MIN_CHECK and has_pending_30m_check_for_ticker(ticker, time.time()):
        return False, "", 0.0
    regime = regime or get_market_regime()
    regime_name = str((regime or {}).get("name", "NORMAL") or "NORMAL")
    if regime_name == "BLOCK":
        return False, "", 0.0
    judgement = get_watch_judgement_text(item)
    caution = get_watch_caution_text(item)
    if is_watch_late_candidate(item, judgement, caution):
        return False, "", 0.0
    snap = ensure_pre_alert_snapshot(item)
    chg_5 = safe_float(snap.get("chg_5m", 0), 0)
    chg_30 = safe_float(snap.get("chg_30m", 0), 0)
    from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
    high_gap_pct = get_reference_high_gap_pct(item)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    edge_score = safe_float(item.get("edge_score", item.get("signal_score", 0)), 0)
    trade_tier = get_watch_tier_text(item)
    strategy = str(item.get("strategy", "") or "").strip()
    chart_profile = get_item_chart_profile(item)
    rise_pct = safe_float(chart_profile.get("chart_rise_pct", 0), 0)
    if high_gap_pct < 0.55 or high_gap_pct > 3.20:
        return False, "", 0.0
    if high_gap_pct <= 0.20:
        return False, "", 0.0
    if rise_pct >= 3.30 and high_gap_pct < 2.30:
        return False, "", 0.0
    if max(chg_30, from_low) >= LIVE_DISPLAY_OVERHEAT_30M_PCT and high_gap_pct < 2.10:
        return False, "", 0.0
    if chg_5 >= LIVE_DISPLAY_OVERHEAT_5M_PCT and max(chg_30, from_low) >= 3.20 and high_gap_pct < 2.30:
        return False, "", 0.0
    if not is_chart_position_good_for_live(item):
        return False, "", 0.0
    live_strength = 0
    if chg_5 >= LIVE_FORCE_ENTRY_MIN_5M_PCT:
        live_strength += 1
    if chg_5 <= 0.05 and chg_30 <= 0.05:
        live_strength -= 2
    if vol_ratio >= LIVE_FORCE_ENTRY_MIN_VOL_RATIO:
        live_strength += 1
    if leader_score >= LIVE_FORCE_ENTRY_MIN_LEADER_SCORE:
        live_strength += 1
    if max(chg_30, from_low) >= 0.45:
        live_strength += 1
    if edge_score >= 5.40:
        live_strength += 1
    if trade_tier in {"S", "A"}:
        live_strength += 1
    if regime_name == "STRONG_UP":
        live_strength += 1
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
        live_strength = -99
    if live_strength >= 4 and chg_5 >= LIVE_FORCE_ENTRY_MIN_5M_PCT and vol_ratio >= LIVE_FORCE_ENTRY_MIN_VOL_RATIO and high_gap_pct >= 0.95:
        priority = edge_score + leader_score * 1.2 + vol_ratio * 0.45 + max(chg_5, 0) * 1.1
        return True, "live", priority
    restart_strength = 0
    if chg_5 >= LIVE_FORCE_RECOVERY_MIN_5M_PCT:
        restart_strength += 1
    if vol_ratio >= LIVE_FORCE_RECOVERY_MIN_VOL_RATIO:
        restart_strength += 1
    if leader_score >= LIVE_FORCE_RECOVERY_MIN_LEADER_SCORE:
        restart_strength += 1
    if max(chg_30, from_low) >= 0.15:
        restart_strength += 1
    if edge_score >= 4.80:
        restart_strength += 1
    if regime_name == "SIDEWAYS" and chg_5 >= 0.20 and vol_ratio >= 1.60:
        restart_strength += 1
    if strategy in {"PREWATCH", "NEWLOW_RECOVER"}:
        if high_gap_pct < 0.85 or chg_5 < 0.12 or vol_ratio < 1.65 or max(chg_30, from_low) < 0.30:
            return False, "", 0.0
    if restart_strength >= 3:
        priority = edge_score + leader_score * 0.9 + vol_ratio * 0.30 + max(chg_5, 0) * 0.8
        return True, "restart", priority
    return False, "", 0.0
def build_live_force_alert_candidates(cache, regime, source_candidates=None, exclude_tickers=None):
    exclude_tickers = {str(x or "").upper().strip() for x in (exclude_tickers or set())}
    combined = []
    if source_candidates:
        combined.extend(source_candidates)
    combined.extend(build_quick_watch_candidates(cache, regime))
    combined.extend(build_leader_watch_candidates(cache, regime, relaxed=True))
    combined.extend(build_prewatch_candidates(cache, regime, exclude_tickers=exclude_tickers))
    combined.extend(build_newlow_recovery_candidates(cache, regime, exclude_tickers=exclude_tickers))
    if not combined:
        return []
    combined = dedupe_best_signal_per_ticker(combined, key_name="edge_score")
    picked = []
    for item in combined:
        ticker = str(item.get("ticker", "") or "").upper().strip()
        if not ticker or ticker in exclude_tickers:
            continue
        ok, forced_section, priority = is_live_force_candidate(item, regime=regime)
        if not ok:
            continue
        picked.append((forced_section, priority, item))
    picked.sort(key=lambda x: x[1], reverse=True)
    return picked
def maybe_send_live_force_alerts(cache, regime, source_candidates, sent_tickers, now_ts):
    global last_live_force_alert_time, live_force_alert_history
    if now_ts - safe_float(last_live_force_alert_time, 0) < LIVE_FORCE_ALERT_COOLDOWN_SEC:
        return False
    picked = build_live_force_alert_candidates(cache, regime, source_candidates=source_candidates, exclude_tickers=sent_tickers)
    if not picked:
        return False
    live_entries = []
    restart_entries = []
    for forced_section, priority, item in picked:
        ticker = str(item.get("ticker", "") or "").upper().strip()
        prev_ts = safe_float(live_force_alert_history.get(ticker, 0), 0)
        if now_ts - prev_ts < LIVE_FORCE_PER_TICKER_COOLDOWN_SEC:
            continue
        item, refresh_ok, _refresh_reason = refresh_watch_item_before_alert(item, section_key=forced_section, regime=regime)
        if not refresh_ok:
            continue
        reason_text = "실시간으로 힘이 붙어서 실제 알림으로 먼저 올림" if forced_section == "live" else "재출발 힘이 붙어서 실제 알림으로 먼저 올림"
        row = (item, "new", reason_text)
        if forced_section == "live" and len(live_entries) < LIVE_FORCE_MAX_ENTRY_ITEMS:
            live_entries.append(row)
        elif forced_section == "restart" and len(restart_entries) < LIVE_FORCE_MAX_RECOVERY_ITEMS:
            restart_entries.append(row)
        if len(live_entries) >= LIVE_FORCE_MAX_ENTRY_ITEMS and len(restart_entries) >= LIVE_FORCE_MAX_RECOVERY_ITEMS:
            break
    if not live_entries and not restart_entries:
        return False
    if live_entries:
        send_watch_alert_bundle(get_watch_section_header("live", regime=regime), live_entries, section_key="live")
    if restart_entries:
        send_watch_alert_bundle(get_watch_section_header("restart", regime=regime), restart_entries, section_key="restart")
    for row in live_entries + restart_entries:
        item = row[0]
        ticker = str(item.get("ticker", "") or "").upper().strip()
        live_force_alert_history[ticker] = now_ts
        save_watch_snapshot(ticker, item, now_ts, alert_type="new", main_watch=(row in live_entries))
    last_live_force_alert_time = now_ts
    return True

def get_watchlist_ranked_cache_items(cache: dict, limit: int = 0):
    rows = []
    if not isinstance(cache, dict):
        return rows
    for ticker, data in cache.items():
        if not isinstance(data, dict):
            continue
        leader = safe_float(data.get("leader_score", 0), 0)
        vol_ratio = safe_float(data.get("vol_ratio", data.get("vol_ratio_1m", 0)), 0)
        change_5 = safe_float(data.get("change_5", 0), 0)
        change_3 = safe_float(data.get("change_3", 0), 0)
        turnover_rank = int(safe_float(data.get("turnover_rank", 999), 999))
        surge_rank = int(safe_float(data.get("surge_rank", 999), 999))
        rank_bonus = 0.0
        if turnover_rank <= 20:
            rank_bonus += 0.55
        elif turnover_rank <= 40:
            rank_bonus += 0.25
        if surge_rank <= 20:
            rank_bonus += 0.55
        elif surge_rank <= 40:
            rank_bonus += 0.25
        score = leader * 1.35 + max(vol_ratio - 0.9, 0) * 1.15 + max(change_5, 0) * 0.95 + max(change_3, 0) * 0.45 + rank_bonus
        rows.append((ticker, data, score))
    rows.sort(key=lambda x: x[2], reverse=True)
    if int(limit) > 0:
        rows = rows[:int(limit)]
    return rows

def get_watchlist_subset_cache(cache: dict, limit: int = SCAN_WATCHLIST_CACHE_LIMIT):
    subset = {}
    for ticker, data, _score in get_watchlist_ranked_cache_items(cache, limit=limit):
        subset[ticker] = data
    return subset

def collect_watch_signals_from_cache(cache, regime=None, started_at: float = None, time_limit_sec: float = SCAN_WATCHLIST_PREPARE_MAX_SEC):
    results = []
    analyzers = [analyze_early_entry, analyze_pre_breakout_entry, analyze_trend_cont_entry, analyze_breakout_entry, analyze_prepump_entry, analyze_pullback_entry]
    ranked_rows = get_watchlist_ranked_cache_items(cache, limit=SCAN_WATCHLIST_SIGNAL_CACHE_LIMIT)
    base_ts = safe_float(started_at, time.time())
    for idx, (ticker, data, _score) in enumerate(ranked_rows, start=1):
        if idx == 1 or idx % SCAN_WATCHLIST_COLLECT_HEARTBEAT_EVERY == 0:
            try:
                mark_main_loop_heartbeat(stage=f"scan_watchlist_prepare_collect_{idx}")
            except Exception:
                pass
        if time.time() - base_ts >= max(float(time_limit_sec), 6.0):
            print(f"[watchlist_prepare] collect_signals time budget 초과 / idx={idx}", flush=True)
            break
        for analyzer in analyzers:
            try:
                signal = analyzer(ticker, data)
                if not signal:
                    continue
                signal = apply_trade_tier_adjustments(signal, regime=regime)
                if regime and not watch_strategy_allowed_in_regime(signal["strategy"], regime):
                    continue
                results.append(signal)
            except Exception:
                continue
    return results

def build_watch_fallback_candidates_lite(cache, regime=None, started_at: float = None, time_limit_sec: float = 8.0):
    base_ts = safe_float(started_at, time.time())
    subset = get_watchlist_subset_cache(cache, limit=SCAN_WATCHLIST_SIGNAL_CACHE_LIMIT)
    combined = []
    try:
        mark_main_loop_heartbeat(stage="scan_watchlist_prepare_fallback_leader")
    except Exception:
        pass
    combined.extend(build_leader_watch_candidates(subset, regime))
    if time.time() - base_ts >= time_limit_sec:
        return dedupe_best_signal_per_ticker(combined, key_name="edge_score")[:SCAN_WATCHLIST_FALLBACK_LIMIT] if combined else []
    try:
        mark_main_loop_heartbeat(stage="scan_watchlist_prepare_fallback_relaxed")
    except Exception:
        pass
    combined.extend(build_leader_watch_candidates(subset, regime, relaxed=True))
    if len(combined) < max(4, SCAN_WATCHLIST_FALLBACK_LIMIT // 2) and (time.time() - base_ts) < time_limit_sec:
        try:
            mark_main_loop_heartbeat(stage="scan_watchlist_prepare_fallback_quick")
        except Exception:
            pass
        combined.extend(build_quick_watch_candidates(subset, regime))
    if not combined:
        return []
    combined = dedupe_best_signal_per_ticker(combined, key_name="edge_score")
    combined.sort(
        key=lambda x: (
            safe_float(x.get("edge_score", 0))
            + safe_float(x.get("leader_score", 0)) * 1.0
            + safe_float(x.get("change_pct", 0)) * 0.25
            + safe_float(x.get("vol_ratio", 0)) * 0.20
        ),
        reverse=True,
    )
    return combined[:SCAN_WATCHLIST_FALLBACK_LIMIT]

def scan_watchlist_time_exceeded(start_ts: float, limit_sec: float = SCAN_WATCHLIST_MAX_SEC) -> bool:
    return (time.time() - safe_float(start_ts, time.time())) >= float(limit_sec)

def should_do_heavy_watch_refresh(item: dict, order_idx: int, display_key: str = "") -> bool:
    key = str(display_key or "").strip()
    if key not in {"early", "restart", "live"}:
        return False
    if order_idx <= SCAN_WATCHLIST_HEAVY_REFRESH_TOP:
        return True
    trade_tier = str(item.get("trade_tier", "B") or "B").upper().strip()
    if trade_tier in {"S", "A"}:
        return True
    priority = signal_priority_value(item)
    return priority >= 8.2

def scan_watchlist(shared_cache=None):
    global last_watchlist_market_register_ts
    started_at = time.time()
    mark_main_loop_heartbeat(stage="scan_watchlist_prepare")
    cache = shared_cache if isinstance(shared_cache, dict) and shared_cache else (shared_market_cache if isinstance(shared_market_cache, dict) and shared_market_cache else None)
    if not cache:
        request_market_refresh(force=False, reason="scan_watchlist_empty_cache")
        return
    regime = get_market_regime()
    ranked_cache = get_watchlist_subset_cache(cache, limit=SCAN_WATCHLIST_CACHE_LIMIT)
    if ranked_cache:
        cache = ranked_cache
    prepare_deadline = started_at + SCAN_WATCHLIST_PREPARE_MAX_SEC
    now_prepare_ts = time.time()
    should_register_market = (now_prepare_ts - safe_float(last_watchlist_market_register_ts, 0)) >= SCAN_WATCHLIST_MARKET_REGISTER_INTERVAL_SEC
    try:
        if should_register_market:
            mark_main_loop_heartbeat(stage="scan_watchlist_prepare_market")
            market_cache = get_watchlist_subset_cache(cache, limit=SCAN_WATCHLIST_MARKET_CACHE_LIMIT) or cache
            register_missed_review_market_candidates(market_cache, now_ts=now_prepare_ts, limit=SCAN_WATCHLIST_PREPARE_MISSED_LIMIT)
            last_watchlist_market_register_ts = time.time()
        else:
            mark_main_loop_heartbeat(stage="scan_watchlist_prepare_market_skip")
    except Exception as e:
        print(f"[놓친 코인 시장후보 등록 오류] {e}")
    if time.time() >= prepare_deadline:
        print("[watchlist_prepare] market candidate 등록 뒤 time budget 초과", flush=True)
    try:
        mark_main_loop_heartbeat(stage="scan_watchlist_prepare_collect")
    except Exception:
        pass
    results = collect_watch_signals_from_cache(cache, regime=regime, started_at=started_at, time_limit_sec=SCAN_WATCHLIST_PREPARE_MAX_SEC)
    if (not results) and time.time() < prepare_deadline:
        try:
            mark_main_loop_heartbeat(stage="scan_watchlist_prepare_fallback")
        except Exception:
            pass
        fallback_budget = max(4.0, prepare_deadline - time.time())
        fallback_results = build_watch_fallback_candidates_lite(cache, regime, started_at=time.time(), time_limit_sec=fallback_budget)
        if fallback_results:
            results.extend(fallback_results)
    if results:
        try:
            mark_main_loop_heartbeat(stage="scan_watchlist_prepare_pending")
        except Exception:
            pass
        update_pending_buy_candidates_from_results(results, regime)
    unique_results = dedupe_best_signal_per_ticker(results, key_name="signal_score") if results else []
    unique_results.sort(key=lambda x: signal_priority_value(x), reverse=True)
    top = unique_results[:SCAN_WATCHLIST_TOP_LIMIT]
    try:
        mark_main_loop_heartbeat(stage="scan_watchlist_prepare_shortlist")
    except Exception:
        pass
    for idx, item in enumerate(top, start=1):
        try:
            item["shortlist_rank"] = idx
            item["market_regime"] = dict(regime) if isinstance(regime, dict) else {}
            cache_row = cache.get(item.get("ticker", ""), {}) if isinstance(cache, dict) else {}
            if isinstance(cache_row, dict):
                item["turnover_rank"] = int(safe_float(cache_row.get("turnover_rank", item.get("turnover_rank", 999)), 999))
                item["surge_rank"] = int(safe_float(cache_row.get("surge_rank", item.get("surge_rank", 999)), 999))
            rank_info = get_market_pulse_rank_info(item.get("ticker", ""))
            item["change_1_rank"] = int(safe_float(rank_info.get("change_1_rank", item.get("change_1_rank", 0)), 0))
            item["change_3_rank"] = int(safe_float(rank_info.get("change_3_rank", item.get("change_3_rank", 0)), 0))
            item["change_5_rank"] = int(safe_float(rank_info.get("change_5_rank", item.get("change_5_rank", 0)), 0))
            item["vol_ratio_rank"] = int(safe_float(rank_info.get("vol_ratio_rank", item.get("vol_ratio_rank", 0)), 0))
            item["turnover_rank_pulse"] = int(safe_float(rank_info.get("turnover_rank_pulse", item.get("turnover_rank_pulse", 0)), 0))
            item["leader_rank"] = int(safe_float(rank_info.get("leader_rank", item.get("leader_rank", 0)), 0))
        except Exception:
            continue
    try:
        mark_main_loop_heartbeat(stage="scan_watchlist_prepare_signal")
    except Exception:
        pass
    try:
        review_now_ts = time.time()
        for item in top[:MISSED_REVIEW_SIGNAL_LIMIT]:
            display_key = get_watch_display_group(item, regime=regime)
            block_reason = get_watch_quality_block_reason(item, alert_type="new")
            register_missed_review_candidate(
                item,
                now_ts=review_now_ts,
                seen_market=True,
                seen_shortlist=True,
                seen_signal=True,
                alert_sent=False,
                alert_section=(display_key if display_key in {"late", "watch"} else ""),
                quality_block_reason=block_reason,
                persist=False,
            )
        if top:
            cleanup_missed_review_queue(review_now_ts)
            persist_missed_review_state()
    except Exception as e:
        print(f"[놓친 코인 신호후보 등록 오류] {e}")
    section_entries = {
        "early": [],
        "restart": [],
        "live": [],
        "late": [],
        "watch": [],
    }
    now_ts = time.time()
    skipped_top = []
    for idx, item in enumerate(top, start=1):
        if idx == 1 or idx % SCAN_WATCHLIST_HEARTBEAT_EVERY == 0:
            try:
                mark_main_loop_heartbeat(stage=f"scan_watchlist_top_{idx}")
            except Exception:
                pass
        if scan_watchlist_time_exceeded(started_at):
            print(f"[watchlist] top loop time budget 초과 / idx={idx}", flush=True)
            break
        ticker = item["ticker"]
        base_judgement = get_watch_judgement_text(item)
        base_caution = get_watch_caution_text(item)
        raw_display_key = get_watch_display_group(item, base_judgement, base_caution, regime=regime)
        display_key = get_user_visible_display_key(item, raw_display_key, regime=regime)
        send_ok, alert_type, reason_text = should_send_watch_alert(ticker, item, now_ts)
        if not send_ok:
            skipped_top.append(item)
            continue
        user_block_reason = get_user_alert_block_reason(item, display_key)
        if user_block_reason:
            try:
                register_missed_review_candidate(
                    item,
                    now_ts=now_ts,
                    seen_market=True,
                    seen_shortlist=True,
                    seen_signal=True,
                    alert_sent=False,
                    alert_section=display_key,
                    quality_block_reason=f"사용자 알림 제외: {user_block_reason}",
                )
            except Exception as e:
                print(f"[놓친 코인 사용자알림 제외 등록 오류] {e}")
            continue
        if should_do_heavy_watch_refresh(item, idx, display_key):
            item, refresh_ok, _refresh_reason = refresh_watch_item_before_alert(item, section_key=display_key, regime=regime)
            if not refresh_ok:
                continue
            judgement = get_watch_judgement_text(item)
            caution = get_watch_caution_text(item)
            raw_display_key = get_watch_display_group(item, judgement, caution, regime=regime)
            display_key = get_user_visible_display_key(item, raw_display_key, regime=regime)
            user_block_reason = get_user_alert_block_reason(item, display_key)
            if user_block_reason:
                try:
                    register_missed_review_candidate(
                        item,
                        now_ts=now_ts,
                        seen_market=True,
                        seen_shortlist=True,
                        seen_signal=True,
                        alert_sent=False,
                        alert_section=display_key,
                        quality_block_reason=f"사용자 알림 제외: {user_block_reason}",
                    )
                except Exception as e:
                    print(f"[놓친 코인 사용자알림 제외 등록 오류] {e}")
                continue
        section_entries[display_key].append((item, alert_type, reason_text))
        save_watch_snapshot(ticker, item, now_ts, alert_type=alert_type, main_watch=(display_key in {"early", "live"}))
        try:
            register_missed_review_candidate(
                item,
                now_ts=now_ts,
                seen_market=True,
                seen_shortlist=True,
                seen_signal=True,
                alert_sent=True,
                alert_section=display_key,
                quality_block_reason=reason_text,
            )
        except Exception as e:
            print(f"[놓친 코인 알림후보 등록 오류] {e}")
    sent_tickers = {entry[0]["ticker"] for rows in section_entries.values() for entry in rows}
    early_entries = section_entries["early"]
    restart_entries = section_entries["restart"]
    live_entries = section_entries["live"]
    immediate_early_entries = early_entries[:2]
    immediate_live_entries = live_entries[:1]
    if immediate_early_entries:
        for row in immediate_early_entries:
            send_watch_alert_bundle(get_watch_section_header("early", regime=regime), [row], section_key="early")
        section_entries["early"] = early_entries[len(immediate_early_entries):]
        early_entries = section_entries["early"]
    if immediate_live_entries:
        for row in immediate_live_entries:
            send_watch_alert_bundle(get_watch_section_header("live", regime=regime), [row], section_key="live")
        section_entries["live"] = live_entries[len(immediate_live_entries):]
        live_entries = section_entries["live"]

    aux_started = time.time()
    if not scan_watchlist_time_exceeded(started_at):
        prewatch_rows = build_prewatch_candidates(cache, regime, exclude_tickers=sent_tickers)[:SCAN_WATCHLIST_PREWATCH_LIMIT]
        for idx, item in enumerate(prewatch_rows, start=1):
            if idx == 1 or idx % SCAN_WATCHLIST_HEARTBEAT_EVERY == 0:
                try:
                    mark_main_loop_heartbeat(stage=f"scan_watchlist_prewatch_{idx}")
                except Exception:
                    pass
            if scan_watchlist_time_exceeded(started_at) or (time.time() - aux_started) >= SCAN_WATCHLIST_AUX_MAX_SEC:
                print(f"[watchlist] prewatch time budget 초과 / idx={idx}", flush=True)
                break
            ticker = item["ticker"]
            if not should_send_simple_aux_alert(recent_prewatch_alerts, ticker, now_ts, PREWATCH_ALERT_COOLDOWN_SEC):
                continue
            user_block_reason = get_user_alert_block_reason(item, "restart")
            if user_block_reason:
                try:
                    register_missed_review_candidate(item, now_ts=now_ts, seen_market=True, seen_shortlist=True, seen_signal=True, alert_sent=False, alert_section="restart", quality_block_reason=f"사용자 알림 제외: {user_block_reason}")
                except Exception as e:
                    print(f"[놓친 코인 재출발 사용자제외 등록 오류] {e}")
                continue
            item, refresh_ok, _refresh_reason = refresh_watch_item_before_alert(item, section_key="restart", regime=regime)
            if not refresh_ok:
                continue
            reason_text = "한 번 눌린 뒤 다시 붙는지 볼 재출발 후보"
            section_entries["restart"].append((item, "new", reason_text))
            save_watch_snapshot(ticker, item, now_ts, alert_type="new", main_watch=False)
            try:
                register_missed_review_candidate(item, now_ts=now_ts, seen_market=True, seen_shortlist=True, seen_signal=True, alert_sent=True, alert_section="restart", quality_block_reason=reason_text)
            except Exception as e:
                print(f"[놓친 코인 재출발 등록 오류] {e}")
            sent_tickers.add(ticker)

    aux_started = time.time()
    if not scan_watchlist_time_exceeded(started_at):
        newlow_rows = build_newlow_recovery_candidates(cache, regime, exclude_tickers=sent_tickers)[:SCAN_WATCHLIST_NEWLOW_LIMIT]
        for idx, item in enumerate(newlow_rows, start=1):
            if idx == 1 or idx % SCAN_WATCHLIST_HEARTBEAT_EVERY == 0:
                try:
                    mark_main_loop_heartbeat(stage=f"scan_watchlist_newlow_{idx}")
                except Exception:
                    pass
            if scan_watchlist_time_exceeded(started_at) or (time.time() - aux_started) >= SCAN_WATCHLIST_AUX_MAX_SEC:
                print(f"[watchlist] newlow time budget 초과 / idx={idx}", flush=True)
                break
            ticker = item["ticker"]
            if not should_send_simple_aux_alert(recent_newlow_alerts, ticker, now_ts, NEWLOW_ALERT_COOLDOWN_SEC):
                continue
            user_block_reason = get_user_alert_block_reason(item, "restart")
            if user_block_reason:
                try:
                    register_missed_review_candidate(item, now_ts=now_ts, seen_market=True, seen_shortlist=True, seen_signal=True, alert_sent=False, alert_section="restart", quality_block_reason=f"사용자 알림 제외: {user_block_reason}")
                except Exception as e:
                    print(f"[놓친 코인 재출발 사용자제외 등록 오류] {e}")
                continue
            item, refresh_ok, _refresh_reason = refresh_watch_item_before_alert(item, section_key="restart", regime=regime)
            if not refresh_ok:
                continue
            reason_text = "바닥권에서 반등 뒤 다시 가는지 볼 재출발 후보"
            section_entries["restart"].append((item, "new", reason_text))
            save_watch_snapshot(ticker, item, now_ts, alert_type="new", main_watch=False)
            try:
                register_missed_review_candidate(item, now_ts=now_ts, seen_market=True, seen_shortlist=True, seen_signal=True, alert_sent=True, alert_section="restart", quality_block_reason=reason_text)
            except Exception as e:
                print(f"[놓친 코인 재출발 등록 오류] {e}")
            sent_tickers.add(ticker)

    early_entries = section_entries["early"]
    restart_entries = section_entries["restart"]
    live_entries = section_entries["live"]
    if len(early_entries) + len(restart_entries) + len(live_entries) < 2 and skipped_top and not scan_watchlist_time_exceeded(started_at):
        promoted_pool = []
        for idx, item in enumerate(skipped_top, start=1):
            if idx == 1 or idx % SCAN_WATCHLIST_HEARTBEAT_EVERY == 0:
                try:
                    mark_main_loop_heartbeat(stage=f"scan_watchlist_promote_{idx}")
                except Exception:
                    pass
            if scan_watchlist_time_exceeded(started_at):
                print(f"[watchlist] promote loop time budget 초과 / idx={idx}", flush=True)
                break
            ticker = item["ticker"]
            if ticker in sent_tickers:
                continue
            if is_ticker_blocked_for_watch_alert(ticker):
                continue
            if WATCH_BLOCK_IF_PENDING_30MIN_CHECK and has_pending_30m_check_for_ticker(ticker, now_ts):
                continue
            if not has_meaningful_watch_motion(item):
                continue
            judgement = get_watch_judgement_text(item)
            caution = get_watch_caution_text(item)
            raw_display_key = get_watch_display_group(item, judgement, caution, regime=regime)
            display_key = get_user_visible_display_key(item, raw_display_key, regime=regime)
            if display_key in {"late", "watch"}:
                continue
            snap = ensure_pre_alert_snapshot(item)
            chg_5 = safe_float(snap.get("chg_5m", 0), 0)
            chg_30 = safe_float(snap.get("chg_30m", 0), 0)
            from_low = safe_float(snap.get("from_30m_low_pct", 0), 0)
            high_gap_pct = get_reference_high_gap_pct(item)
            vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
            priority = signal_priority_value(item)
            motion = max(chg_5, chg_30, from_low)
            if get_user_alert_block_reason(item, display_key):
                continue
            if display_key == "live":
                if vol_ratio < 1.30 or high_gap_pct < 0.50 or motion < 0.18:
                    continue
                promoted_pool.append(("live", priority + 0.35, item, "new", "실시간으로 힘이 붙어서 바로 볼 후보"))
            elif display_key == "early":
                if high_gap_pct < 0.65 or vol_ratio < 1.10 or motion < 0.08:
                    continue
                promoted_pool.append(("early", priority + 0.20, item, "new", "초반 자리라 먼저 볼 후보"))
            elif display_key == "restart":
                if high_gap_pct < 0.70 or vol_ratio < 1.05 or motion < 0.08:
                    continue
                promoted_pool.append(("restart", priority, item, "new", "재출발 힘이 꽤 보여 먼저 볼 후보"))
        promoted_pool.sort(key=lambda x: x[1], reverse=True)
        add_limits = {
            "early": 1 if not early_entries else 0,
            "live": 1 if not live_entries else 0,
            "restart": 1 if len(restart_entries) < 2 else 0,
        }
        for display_key, _prio, item, alert_type, reason_text in promoted_pool:
            if add_limits.get(display_key, 0) <= 0:
                continue
            add_limits[display_key] -= 1
            section_entries[display_key].append((item, alert_type, reason_text))
            save_watch_snapshot(item["ticker"], item, now_ts, alert_type=alert_type, main_watch=(display_key in {"early", "live"}))
            sent_tickers.add(item["ticker"])
    sent_any_alert = bool(immediate_early_entries or immediate_live_entries)
    early_entries = section_entries["early"]
    restart_entries = section_entries["restart"]
    live_entries = section_entries["live"]
    if early_entries:
        send_watch_alert_bundle(get_watch_section_header("early", regime=regime), early_entries[:2], section_key="early")
        sent_any_alert = True
    if live_entries:
        send_watch_alert_bundle(get_watch_section_header("live", regime=regime), live_entries[:1], section_key="live")
        sent_any_alert = True
    if restart_entries:
        limit = 1 if (early_entries or live_entries) else 2
        send_watch_alert_bundle(get_watch_section_header("restart", regime=regime), restart_entries[:limit], section_key="restart")
        sent_any_alert = True
    if not sent_any_alert and not scan_watchlist_time_exceeded(started_at, limit_sec=SCAN_WATCHLIST_MAX_SEC + 8):
        source_candidates = unique_results if unique_results else results
        maybe_send_live_force_alerts(cache, regime, source_candidates, sent_tickers, now_ts)
    try:
        mark_main_loop_heartbeat(stage="scan_watchlist_done")
    except Exception:
        pass

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
                "leader_score": safe_float(last_buy.get("leader_score", 0)),
                "trade_tier": last_buy.get("trade_tier", "B"),
                "managed_by_bot": True,
                "invalid_break_level": safe_float(last_buy.get("invalid_break_level", 0)),
                "entry_strategy_snapshot": strategy,
                "pattern_tags": last_buy.get("pattern_tags", []),
            }
            recovered += 1
        except Exception as e:
            print(f"[포지션 복구 오류] {ticker}: {e}")
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
    positions_changed = False
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
                positions_changed = True
                clear_position_file_if_empty()
                continue
            entry_price = float(pos["entry_price"])
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
            changed_stats = update_position_run_stats(pos, pnl_pct)
            if current_price > pos["peak_price"]:
                pos["peak_price"] = current_price
                changed_stats = True
            if changed_stats:
                positions_changed = True
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
                positions_changed = True
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
                positions_changed = True
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
        except Exception as e:
            print(f"[포지션 모니터 오류] {ticker} / {e}")
    if positions_changed:
        mark_position_state_dirty()
    flush_position_state(force=False)
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
    append_missed_review_parts(lines, limit=2)
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
    append_missed_review_parts(parts, limit=2)
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
            query.answer(f"{target_min}분 구간 기준으로 바로 보여줄게" if ok else "현재가를 못 불러왔어", show_alert=not ok)
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
    persist_price_check_state()
    try:
        query.answer(f"{target_min}분 뒤 자동 확인 예약했어")
    except Exception:
        pass
    lines = [
        f"⏰ {ticker} {target_min}분 뒤 자동 확인 예약",
        f"- 예정 {format_hms(target_ts)} / 남은 {format_elapsed_text(max(0.0, target_ts - now_ts))}",
    ]
    try:
        bot.send_message(chat_id=query.message.chat_id, text="\n".join(lines), reply_to_message_id=ref.get("root_message_id"))
    except Exception as e:
        print(f"[텔레그램 예약 메시지 오류] {e}")
def register_bot_commands():
    try:
        bot.set_my_commands([
            BotCommand("status", "지금 상태"),
            BotCommand("summary", "최근 요약"),
            BotCommand("today", "오늘 결과"),
            BotCommand("summary_strategy", "전략별 요약"),
            BotCommand("today_strategy", "오늘 전략 결과"),
            BotCommand("btc", "BTC 흐름"),
            BotCommand("autobuy_on", "자동매수 켜기"),
            BotCommand("autobuy_off", "자동매수 끄기"),
            BotCommand("reset_pause", "자동 쉬기 해제"),
            BotCommand("scan_debug", "후보 자세히"),
            BotCommand("info", "설명 보기"),
            BotCommand("recent5", "최근 후보"),
            BotCommand("missed", "놓친 코인"),
            BotCommand("missed_reset", "놓친 기록 초기화"),
        ])
    except Exception as e:
        print(f"[명령어 등록 오류] {e}")
def reset_pause_command(update, context: CallbackContext):
    reset_auto_pause_state(bypass_sec=900, reason="manual")
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
def get_scan_debug_candidate_section(change_5: float, vol_ratio: float, leader_score: float, rsi: float) -> str:
    change_5 = safe_float(change_5, 0)
    vol_ratio = safe_float(vol_ratio, 0)
    leader_score = safe_float(leader_score, 0)
    rsi = safe_float(rsi, 50)
    if change_5 >= 1.90 and vol_ratio < 6.00:
        return "weak"
    if rsi >= 84:
        return "weak"
    if change_5 >= 0.25 and vol_ratio >= 1.55 and leader_score >= 0.45:
        return "strong"
    if (change_5 >= 0.05 and vol_ratio >= 1.15 and leader_score >= 0.25) or (vol_ratio >= 2.20 and leader_score >= 0.20):
        return "watch"
    return "weak"
def build_scan_debug_reasons(change_5: float, vol_ratio: float, leader_score: float, rsi: float, section: str = ""):
    change_5 = safe_float(change_5, 0)
    vol_ratio = safe_float(vol_ratio, 0)
    leader_score = safe_float(leader_score, 0)
    rsi = safe_float(rsi, 50)
    section = section or get_scan_debug_candidate_section(change_5, vol_ratio, leader_score, rsi)
    if section == "strong":
        return ["초입 가능 후보"]
    if section == "watch":
        return ["초입/회복 확인 후보"]
    reasons = []
    if change_5 <= -0.05:
        reasons.append("5분 힘이 아직 없음")
    elif change_5 < 0.05:
        reasons.append("5분 상승이 아직 약함")
    if vol_ratio < 1.15:
        reasons.append("거래량이 아직 약함")
    if leader_score < 0.25:
        reasons.append("시장주목도가 아직 약함")
    if rsi >= 84 or (change_5 >= 1.90 and vol_ratio < 6.00):
        reasons.append("너무 빠르게 오른 편")
    if not reasons:
        reasons.append("지금은 구경만")
    return reasons
def normalize_scan_debug_score(base_score: float, change_5: float, vol_ratio: float, leader_score: float, rsi: float, turnover: float = 0.0) -> float:
    score = min(max(safe_float(base_score, 0), 0), 38.0)
    change_5 = safe_float(change_5, 0)
    vol_ratio = safe_float(vol_ratio, 0)
    leader_score = safe_float(leader_score, 0)
    rsi = safe_float(rsi, 50)
    turnover = safe_float(turnover, 0)
    score += min(max(change_5, 0), 1.80) * 4.6
    score += min(max(vol_ratio - 1.0, 0), 5.0) * 1.3
    score += min(leader_score, 4.0) * 1.5
    score += min(turnover / 1000000000.0, 2.2)
    if change_5 <= 0:
        score = min(score, 14.0)
        score -= 8.0
    elif change_5 < 0.18:
        score = min(score, 20.0)
        score -= 4.5
    elif change_5 < 0.35:
        score = min(score, 28.0)
        score -= 1.5
    if vol_ratio >= 10.0 and change_5 <= 0.25:
        score = min(score, 16.0)
    if rsi >= 82:
        score = min(score, 18.0)
        score -= 2.0
    return round(max(score, 0.0), 2)
def finalize_scan_debug_item(raw_item: dict) -> dict:
    item = dict(raw_item or {})
    change_5 = safe_float(item.get("change_5", 0), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    rsi = safe_float(item.get("rsi", 50), 50)
    turnover = safe_float(item.get("turnover", 0), 0)
    base_score = safe_float(item.get("lite_score", 0), 0)
    section = get_scan_debug_candidate_section(change_5, vol_ratio, leader_score, rsi)
    item["section"] = section
    item["section_rank"] = {"strong": 2, "watch": 1, "weak": 0}.get(section, 0)
    item["lite_score"] = normalize_scan_debug_score(base_score, change_5, vol_ratio, leader_score, rsi, turnover)
    item["reasons"] = build_scan_debug_reasons(change_5, vol_ratio, leader_score, rsi, section=section)
    return item
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
            items.append(finalize_scan_debug_item({
                "ticker": ticker,
                "price": safe_float(snap.get("price", 0)),
                "change_5": safe_float(snap.get("change_5", snap.get("change_pct", 0))),
                "vol_ratio": safe_float(snap.get("vol_ratio", 0)),
                "leader_score": safe_float(snap.get("leader_score", 0)),
                "turnover": safe_float(snap.get("turnover", 0)),
                "lite_score": safe_float(snap.get("edge_score", snap.get("signal_score", 0))),
                "rsi": safe_float(snap.get("rsi", 50)),
                "saved_at": saved_at,
            }))
        items.sort(key=lambda x: (safe_float(x.get("section_rank", 0)), safe_float(x.get("lite_score", 0)), safe_float(x.get("saved_at", 0))), reverse=True)
        return items[:limit]
    except Exception:
        return []
def get_scan_debug_candidates():
    try:
        now_ts = time.time()
        cache_age = int(now_ts - shared_market_cache_time) if shared_market_cache_time else None
        snapshot_age = int(now_ts - last_scan_debug_snapshot_time) if last_scan_debug_snapshot_time else None
        def build_items_from_cache(cache_dict, limit=8):
            items = []
            if not isinstance(cache_dict, dict):
                return items
            for ticker, data in cache_dict.items():
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
                    raw_score = (
                        max(change_5, 0) * 6.5
                        + max(change_3, 0) * 3.2
                        + max(change_1, 0) * 1.6
                        + max(vol_ratio - 1.0, 0) * 3.0
                        + min(turnover / 1000000000.0, 3.0)
                        + leader * 4.0
                    )
                    items.append(finalize_scan_debug_item({
                        "ticker": ticker,
                        "price": price,
                        "change_5": change_5,
                        "vol_ratio": vol_ratio,
                        "leader_score": leader,
                        "turnover": turnover,
                        "lite_score": raw_score,
                        "rsi": rsi,
                    }))
                except Exception:
                    continue
            items.sort(
                key=lambda x: (
                    safe_float(x.get("section_rank", 0)),
                    safe_float(x.get("lite_score", 0)),
                    safe_float(x.get("change_5", 0)),
                    safe_float(x.get("turnover", 0)),
                ),
                reverse=True,
            )
            return items[:limit]
        # 1순위: 최근 shared cache 직접 사용 (가장 빠름)
        debug_cache_max_age = 900
        if shared_market_cache and cache_age is not None and cache_age <= debug_cache_max_age:
            items = build_items_from_cache(shared_market_cache, limit=8)
            if items:
                note = f"최근 {cache_age}초 안에 모은 시장 데이터 기준"
                if cache_age > 180:
                    note += " (조금 지난 데이터)"
                return items, note
        # 2순위: 최근 scan_debug 스냅샷 fallback
        if isinstance(last_scan_debug_snapshot, dict) and last_scan_debug_snapshot and snapshot_age is not None and snapshot_age <= 1800:
            items = build_items_from_cache(last_scan_debug_snapshot, limit=8)
            if items:
                note = f"최근 {snapshot_age}초 안에 저장된 시장 스냅샷 기준"
                if snapshot_age > 300:
                    note += " (조금 지난 데이터)"
                return items, note
        # 3순위: 최근 후보 스냅샷
        recent_items = [x for x in get_recent_watch_snapshot_items(limit=12) if str(x.get("section", "")) != "weak"]
        if recent_items:
            return recent_items[:8], "방금 잡힌 후보 기준"
        # 4순위: 지금은 기다릴 후보(pending) fallback
        if pending_buy_candidates:
            items = []
            sorted_candidates = sorted(
                pending_buy_candidates.values(),
                key=lambda x: (safe_float(x.get("leader_score", 0)) * 0.8) + safe_float(x.get("edge_score", 0)),
                reverse=True,
            )[:8]
            for row in sorted_candidates:
                ticker = str(row.get("ticker", "?") or "?")
                if is_watch_excluded_ticker(ticker):
                    continue
                price = safe_float(row.get("first_price", 0))
                score = safe_float(row.get("edge_score", 0))
                leader = safe_float(row.get("leader_score", 0))
                strategy_label = str(row.get("strategy_label", "?") or "?")
                seen_count = int(safe_float(row.get("seen_count", 1), 1))
                item = finalize_scan_debug_item({
                    "ticker": ticker,
                    "price": price,
                    "change_5": 0.0,
                    "vol_ratio": 0.0,
                    "leader_score": leader,
                    "turnover": 0.0,
                    "lite_score": score,
                    "rsi": 50.0,
                    "extra_reasons": ["지금은 기다릴 후보 기준", f"{strategy_label}", f"반복 포착 {seen_count}회"],
                })
                if str(item.get("section", "")) == "weak" and score < 6.0:
                    continue
                items.append(item)
            if items:
                items.sort(key=lambda x: (safe_float(x.get("section_rank", 0)), safe_float(x.get("lite_score", 0)), safe_float(x.get("leader_score", 0))), reverse=True)
                return items[:8], "최근 실시간 후보가 비어 있어 지금은 기다릴 후보 기준으로 보여줌"
        # 5순위: 최근 알림 후보 fallback
        recent_entries = get_recent_candidate_entries(limit=12)
        if recent_entries:
            items = []
            for entry in recent_entries:
                item, alert_type, reason_text, header = unpack_recent_entry(entry)
                if not isinstance(item, dict):
                    continue
                if is_watch_excluded_ticker(item.get("ticker", "")):
                    continue
                snap = ensure_pre_alert_snapshot(item)
                price = safe_float(item.get("current_price", item.get("price", 0)))
                score = safe_float(item.get("edge_score", item.get("signal_score", 0)))
                leader = safe_float(item.get("leader_score", 0))
                vol_ratio = safe_float(item.get("vol_ratio", 0))
                change_5 = safe_float(snap.get("chg_5m", item.get("change_pct", 0)))
                source_label = (header or item.get("strategy_label", "") or "최근 후보").strip()
                debug_item = finalize_scan_debug_item({
                    "ticker": str(item.get("ticker", "?") or "?"),
                    "price": price,
                    "change_5": change_5,
                    "vol_ratio": vol_ratio,
                    "leader_score": leader,
                    "turnover": 0.0,
                    "lite_score": score + leader * 0.6,
                    "rsi": safe_float(item.get("rsi", 50)),
                })
                if str(debug_item.get("section", "")) == "weak" and change_5 <= 0.15:
                    continue
                items.append(debug_item)
            items.sort(key=lambda x: (safe_float(x.get("section_rank", 0)), safe_float(x.get("lite_score", 0))), reverse=True)
            if items:
                return items[:8], "최근 시장 후보가 비어 있어 최근 알림 후보 기준으로 보여줌"
        if cache_age is None:
            return [], "최근 시장 데이터가 아직 없음. 지금은 기다릴 후보나 최근 후보가 생기면 여기에도 같이 보여줄게"
        return [], f"최근 {cache_age}초 안에는 지금 볼 후보가 아직 없음"
    except Exception as e:
        return None, f"후보 상세보기 조회 에러: {e}"
def split_scan_debug_results(results):
    strong_items = [x for x in results if str(x.get("section", "")) == "strong"][:3]
    if strong_items:
        watch_items = [x for x in results if str(x.get("section", "")) == "watch"][:2]
    else:
        watch_items = [x for x in results if str(x.get("section", "")) == "watch"][:3]
    weak_items = [x for x in results if str(x.get("section", "")) == "weak"][:2]
    return strong_items, watch_items, weak_items
def get_scan_debug_shortlist(limit=3):
    results, note = get_scan_debug_candidates()
    if results is None:
        return [], note
    strong_items, watch_items, weak_items = split_scan_debug_results(results)
    shortlist = (strong_items + watch_items)[:max(1, int(limit))]
    if not shortlist:
        shortlist = weak_items[:max(1, int(limit))]
    return shortlist, note
def build_scan_debug_text():
    results, note = get_scan_debug_candidates()
    if results is None:
        return f"⚠️ 후보 상세보기 에러\n{note}"
    lines = ["🔎 지금 볼 후보 보기"]
    health_warning = build_scan_health_warning_text()
    if health_warning:
        lines.append(health_warning)
    if note:
        lines.append(f"안내: {note}")
    if not results:
        lines.append("")
        lines.append("지금 볼 후보 없음")
        return "\n".join(lines)
    strong_items, watch_items, weak_items = split_scan_debug_results(results)
    def append_group(title: str, items):
        if not items:
            return
        lines.append("")
        lines.append(title)
        for i, item in enumerate(items, 1):
            price_text = f"{safe_float(item.get('price', 0)):,.4f}".rstrip("0").rstrip(".")
            lines.append("")
            lines.append(f"{i}. {item.get('ticker','?')} / 현재가 {price_text}")
            lines.append(
                f"   5분 상승 {safe_float(item.get('change_5',0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 시장주목도 {safe_float(item.get('leader_score',0)):.2f}"
            )
            lines.append(f"   참고 점수 {safe_float(item.get('lite_score',0)):.2f}")
            if item.get("one_liner"):
                lines.append(f"   한줄평: {item.get('one_liner')}")
            if safe_float(item.get("from_low", 0), 0) or safe_float(item.get("below_high", 999), 999) < 900:
                lines.append(f"   30분 저점대비 {safe_float(item.get('from_low',0),0):+.2f}% / {format_fast_scan_high_gap_text(safe_float(item.get('below_high',999),999))}")
            lines.append(f"   잡힌 이유: {' / '.join(item.get('reasons', [])[:3])}")
    append_group("🌱 초반/실시간 강세 후보", strong_items)
    append_group("🔁 재출발 확인 후보", watch_items)
    append_group("⛔ 늦었거나 아직 약한 후보", weak_items[:3])
    return "\n".join(lines)
def scan_debug_command(update, context: CallbackContext):
    try:
        send("🔎 지금 볼 후보 불러오는 중...")
        send(build_scan_debug_text())
    except Exception as e:
        send(f"⚠️ 후보 상세보기 에러: {e}")
def append_debug_shortlist_parts(parts: list):
    try:
        shortlist, note = get_scan_debug_shortlist(limit=3)
        header = "🔎 지금 확인할 상위 후보"
        if note:
            header += f" ({note})"
        if not shortlist:
            parts.append(header + "\n\n후보 없음")
            return
        lines = [header]
        for item in shortlist:
            label = "초반/실시간" if str(item.get("section", "")) == "strong" else ("재출발" if str(item.get("section", "")) == "watch" else "늦음/약함")
            lines.append(
                f"• {item.get('ticker','?')} / {label} / 5분 {safe_float(item.get('change_5',0)):.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 주목도 {safe_float(item.get('leader_score',0)):.2f}"
            )
            if item.get("one_liner"):
                lines.append(f"  - {item.get('one_liner')}")
        parts.append("\n".join(lines))
    except Exception as e:
        parts.append(f"⚠️ 상위 스캔 후보 표시 에러: {e}")
def status_command(update, context: CallbackContext):
    parts = []
    parts.append("🟢 자동매수 : 켜짐" if AUTO_BUY else "🔴 자동매수 : 꺼짐")
    health_warning = build_scan_health_warning_text()
    if health_warning:
        parts.append(health_warning)
    paused, pause_msg = should_pause_auto_buy_now()
    if paused:
        parts.append(f"⏸ 지금은 잠깐 쉬는 중: {pause_msg}")
    elif auto_pause_bypass_until and time.time() < auto_pause_bypass_until:
        remain = int(auto_pause_bypass_until - time.time())
        if auto_pause_bypass_reason == "restart":
            parts.append(f"🔄 재시작으로 쉬기 초기화됨 / {max(remain, 0)}초 뒤 다시 자동 쉬기 판단")
        else:
            parts.append(f"🛠 자동 쉬기 수동 해제 적용 중 / {max(remain, 0)}초 남음")
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
        sorted_candidates = sorted(
            pending_buy_candidates.values(),
            key=lambda x: (safe_float(x.get("leader_score", 0)) * 0.8) + safe_float(x.get("edge_score", 0)),
            reverse=True,
        )[:3]
        for item in sorted_candidates:
            c_lines.append(
                f"• {item['ticker']} / {item.get('strategy_label','?')} / 점수 {safe_float(item.get('edge_score', 0)):.2f} / 주도주 {safe_float(item.get('leader_score', 0)):.2f}"
            )
        parts.append("🕒 지금은 기다릴 후보\n" + "\n".join(c_lines))
    append_debug_shortlist_parts(parts)
    if pending_sells:
        p = ["⚠️ 매도 확인 대기중"]
        for ticker in pending_sells.keys():
            p.append(f"• {ticker}")
        parts.append("\n".join(p))
    send("\n\n".join(parts))

# =========================================================
# v2.5.56 빠른 스캔 재구성
# =========================================================
API_CALL_TIMEOUT_SEC = 3.2
MARKET_REFRESH_STUCK_RESET_SEC = 18
FAST_SCAN_RESULT_MAX = 12
FAST_SCAN_INPUT_LIMIT = 32
FAST_SCAN_ALERT_LIMIT = 3
FAST_SCAN_ALERT_COOLDOWN_SEC = 1200
FAST_SCAN_ALERT_REPEAT_PRICE_MOVE_PCT = 1.10
FAST_SCAN_ALERT_REPEAT_SCORE_JUMP = 4.0
FAST_SCAN_ALERT_REPEAT_VOL_JUMP = 2.5
FAST_SCAN_USER_ALERT_MIN_TURNOVER_15M = 120000000
FAST_SCAN_USER_ALERT_MIN_TURNOVER_3M = 18000000
FAST_SCAN_USER_ALERT_MIN_TURNOVER_5M = 32000000
FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_15M = 220000000
FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_3M = 32000000
FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_5M = 55000000
FAST_SCAN_WATCH_MIN_TURNOVER_15M = 45000000
FAST_SCAN_WATCH_MIN_TURNOVER_3M = 8000000
FAST_SCAN_WATCH_MIN_TURNOVER_5M = 14000000
UNIVERSE_REFRESH_BATCH = 4
CACHE_REFRESH_BATCH = 4
MARKET_REFRESH_INTERVAL_SEC = 6
MARKET_REFRESH_EMPTY_RETRY_SEC = 3
SCAN_INTERVAL = 5
FAST_SCAN_STRONG_CHANGE5 = 0.45
FAST_SCAN_STRONG_VOL = 1.35
FAST_SCAN_STRONG_LEADER = 0.90
FAST_SCAN_WATCH_CHANGE5 = 0.08
FAST_SCAN_WATCH_VOL = 1.05
FAST_SCAN_WATCH_LEADER = 0.40
FAST_SCAN_ALERT_MIN_HIGH_GAP = 0.55
FAST_SCAN_ALERT_MIN_FROM_LOW = 0.12
FAST_SCAN_ALERT_MAX_FROM_LOW = 3.20
FAST_SCAN_ALERT_MIN_CHANGE3 = 0.08
FAST_SCAN_ALERT_MIN_COMBINED = 0.35
FAST_SCAN_SURGE_CHANGE5 = 0.85
FAST_SCAN_SURGE_VOL = 2.40
FAST_SCAN_SURGE_LEADER = 1.10
FAST_SCAN_STAGE1_POOL_LIMIT = 44
FAST_SCAN_STAGE1_TURNOVER_TOP = 26
FAST_SCAN_STAGE1_SURGE_TOP = 26
FAST_SCAN_STAGE1_LEADER_TOP = 22
FAST_SCAN_STAGE1_CHANGE5_TOP = 22
FAST_SCAN_STAGE1_SCORE_TOP = 18

FAST_SCAN_RESCUE_LIMIT = 3
FAST_SCAN_RESCUE_SCORE_MIN = 14.4
FAST_SCAN_RESCUE_MIN_TURNOVER_5M = 22000000

def _fast_user_alert_drought_sec(now_ts: float = None) -> int:
    now_ts = safe_float(now_ts, time.time())
    latest_ts = 0.0
    try:
        for value in recent_signal_alerts.values():
            latest_ts = max(latest_ts, safe_float(value, 0))
    except Exception:
        pass
    try:
        for value in recent_fast_alert_signatures.values():
            if isinstance(value, dict):
                latest_ts = max(latest_ts, safe_float(value.get("ts", 0), 0))
    except Exception:
        pass
    if latest_ts <= 0:
        return 999999
    return max(0, int(now_ts - latest_ts))

def _fast_abs_turnover_ok(item: dict, level: str = "watch") -> bool:
    level = str(level or "watch").strip().lower()
    turnover_15m = safe_float(item.get("turnover", 0), 0)
    turnover_3m = safe_float(item.get("turnover_3m", 0), 0)
    turnover_5m = safe_float(item.get("turnover_5m", 0), 0)
    turnover_rank = int(safe_float(item.get("turnover_rank", 999), 999))
    if level in {"s", "strong"}:
        return (
            turnover_15m >= FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_15M
            or (turnover_3m >= FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_3M and turnover_5m >= FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_5M)
            or (0 < turnover_rank <= 18 and turnover_3m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_3M)
        )
    if level in {"a", "user"}:
        return (
            turnover_15m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_15M
            or (turnover_3m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_3M and turnover_5m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_5M)
            or (0 < turnover_rank <= 25 and turnover_3m >= FAST_SCAN_WATCH_MIN_TURNOVER_3M and turnover_5m >= FAST_SCAN_WATCH_MIN_TURNOVER_5M)
        )
    if level == "rescue":
        return (
            turnover_5m >= FAST_SCAN_RESCUE_MIN_TURNOVER_5M
            or turnover_15m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_15M * 0.70
            or (0 < turnover_rank <= 25 and turnover_5m >= FAST_SCAN_WATCH_MIN_TURNOVER_5M)
        )
    return fast_scan_turnover_gate(item, level="watch")

def _fast_chart_bad(item: dict) -> bool:
    chart_ctx = item.get("alert_chart_context", {}) if isinstance(item.get("alert_chart_context"), dict) else {}
    state_code = str(chart_ctx.get("chart_state_code", "") or "")
    if state_code in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}:
        return True
    chart_gap = safe_float(item.get("chart_gap_pct", item.get("below_high", 999)), 999)
    chart_rise = safe_float(item.get("chart_rise_pct", item.get("from_low", 0)), 0)
    zone_ratio = safe_float(item.get("chart_zone_ratio", -1), -1)
    below_high = safe_float(item.get("below_high", 999), 999)
    from_low = safe_float(item.get("from_low", 0), 0)
    if 0 <= chart_gap <= 0.45:
        return True
    if chart_rise >= 4.2 and chart_gap < 1.6:
        return True
    if zone_ratio >= 0.88 and chart_gap < 1.2:
        return True
    if below_high <= 0.55 and from_low >= 1.20:
        return True
    if from_low >= 3.60 and below_high < 1.20:
        return True
    return False

def _fast_rescue_has_weak_reason(item: dict) -> bool:
    reasons = list(item.get("reasons", [])) if isinstance(item.get("reasons"), list) else []
    one_liner = str(item.get("one_liner", "") or "")
    text = " / ".join([str(x) for x in reasons[:5] if str(x).strip()] + [one_liner])
    weak_words = [
        "지금은 구경만",
        "지금 사기엔 별로",
        "이미 많이 올라와서 늦음",
        "고점이 너무 가까움",
        "차트 모양이 아직 별로",
        "재출발 힘이 아직 약함",
        "실시간 강세 초입으로 보기엔 아직 애매함",
        "초반 선점형으로 보기엔 아직 너무 약함",
    ]
    return any(word in text for word in weak_words)

def _fast_rescue_score(item: dict) -> float:
    lite_score = safe_float(item.get("lite_score", 0), 0)
    change_5 = safe_float(item.get("change_5", 0), 0)
    change_3 = safe_float(item.get("change_3", 0), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader_score = safe_float(item.get("leader_score", 0), 0)
    below_high = safe_float(item.get("below_high", 999), 999)
    from_low = safe_float(item.get("from_low", 0), 0)
    bonus = max(min(below_high, 3.2), 0) * 0.9
    penalty = max(from_low - 2.4, 0) * 1.4
    return round(
        lite_score
        + max(change_5, 0) * 2.8
        + max(change_3, 0) * 1.6
        + max(vol_ratio - 1.0, 0) * 2.2
        + leader_score * 1.7
        + bonus
        - penalty,
        2,
    )

def _build_fast_scan_item(ticker: str, data: dict):
    if not ticker or not isinstance(data, dict):
        return None
    item = dict(data)
    price = safe_float(item.get("price", item.get("current_price", 0)), 0)
    df1 = item.get("df_1m")
    if df1 is None or len(df1) < 12:
        df1 = get_ohlcv(ticker, "minute1")
    if df1 is None or len(df1) < 12:
        return None
    if price <= 0:
        price = safe_float(df1["close"].iloc[-1], 0)
    if price <= 0:
        return None

    snap = compute_pre_alert_snapshot_from_df(df1, current_price=price)
    turnover = compute_fast_scan_turnover_metrics(df1, price=price)
    item.update({
        "ticker": str(ticker).upper().strip(),
        "price": price,
        "current_price": price,
        "change_3": safe_float(item.get("change_3", get_recent_change_pct(df1, 3)), 0),
        "change_5": safe_float(item.get("change_5", get_recent_change_pct(df1, 5)), 0),
        "change_30": safe_float(snap.get("chg_30m", 0), 0),
        "vol_ratio": safe_float(item.get("vol_ratio", item.get("vol_ratio_1m", get_vol_ratio(df1, 3, 15))), 0),
        "leader_score": safe_float(item.get("leader_score", 0), 0),
        "rsi": safe_float(item.get("rsi", item.get("rsi_1m", get_rsi(df1, 14))), 0),
        "from_low": safe_float(snap.get("from_30m_low_pct", 0), 0),
        "below_high": safe_float(snap.get("below_30m_high_pct", 999), 999),
        "turnover_1m": safe_float(turnover.get("turnover_1m", 0), 0),
        "turnover_3m": safe_float(turnover.get("turnover_3m", 0), 0),
        "turnover_5m": safe_float(turnover.get("turnover_5m", 0), 0),
        "pre_alert_snapshot": snap,
    })
    section, primary_reason, reasons = classify_fast_scan_alert_section(
        item.get("change_5", 0),
        item.get("change_3", 0),
        item.get("vol_ratio", 0),
        item.get("leader_score", 0),
        item.get("from_low", 0),
        item.get("below_high", 999),
        item.get("rsi", 0),
    )
    item["section"] = section
    item["primary_reason"] = primary_reason
    item["reasons"] = list(reasons or [])
    item = refine_fast_scan_item_with_chart(item, df1)

    lite_score = (
        max(safe_float(item.get("change_5", 0), 0), 0) * 5.2
        + max(safe_float(item.get("change_3", 0), 0), 0) * 2.6
        + max(safe_float(item.get("vol_ratio", 0), 0) - 1.0, 0) * 4.0
        + safe_float(item.get("leader_score", 0), 0) * 3.0
        + max(safe_float(item.get("from_low", 0), 0), 0) * 1.5
        + max(min(safe_float(item.get("below_high", 999), 999), 3.0), 0) * 1.1
        + (1.2 if fast_scan_turnover_gate(item, level="watch") else 0.0)
        + (2.0 if str(item.get("section", "") or "") == "strong" else 0.8 if str(item.get("section", "") or "") == "watch" else 0.0)
    )
    if _fast_chart_bad(item):
        lite_score -= 3.5
    item["lite_score"] = round(lite_score, 2)

    grade, bucket = classify_fast_scan_grade_bucket(
        str(item.get("section", "") or ""),
        str(item.get("primary_reason", "") or ""),
        safe_float(item.get("change_5", 0), 0),
        safe_float(item.get("change_3", 0), 0),
        safe_float(item.get("vol_ratio", 0), 0),
        safe_float(item.get("leader_score", 0), 0),
        safe_float(item.get("from_low", 0), 0),
        safe_float(item.get("below_high", 999), 999),
        safe_float(item.get("rsi", 0), 0),
        safe_float(item.get("lite_score", 0), 0),
        item,
    )
    item["alert_grade"] = grade
    item["alert_bucket"] = bucket
    item["label_text"] = get_fast_scan_label_text(str(item.get("primary_reason", "") or ""))
    item["one_liner"] = build_fast_scan_one_liner(
        str(item.get("primary_reason", "") or ""),
        safe_float(item.get("from_low", 0), 0),
        safe_float(item.get("below_high", 999), 999),
        grade=grade,
        bucket=bucket,
    )
    item["bucket_rank"] = 3 if bucket == "user_alert" else 2 if bucket == "internal_watch" else 1
    item["section_rank"] = 3 if str(item.get("section", "")) == "strong" else 2 if str(item.get("section", "")) == "watch" else 1
    return item

def _friendly_fast_stage_text(stage: str) -> str:
    key = str(stage or "").strip()
    mapping = {
        "scan_watchlist_fast": "빠른 후보 고르는 중",
        "scan_watchlist_done": "빠른 후보 정리 완료",
        "scan_watchlist_empty": "시장 데이터 기다리는 중",
        "scan_watchlist_empty_done": "시장 데이터 없음 처리 완료",
        "fetch_tickers": "거래 코인 목록 새로 받는 중",
        "build_universe": "시장 유니버스 다시 만드는 중",
        "build_cache": "시장 캐시 다시 만드는 중",
    }
    if key in mapping:
        return mapping[key]
    return key.replace("_", " ")

def build_fast_scan_alert_markup(ticker: str, ref_id: str = ""):
    rows = [[InlineKeyboardButton(f"{str(ticker or '').upper()} 빗썸 열기", url=get_bithumb_chart_url(ticker))]]
    if ref_id:
        rows.append([
            InlineKeyboardButton("지금", callback_data=f"pc:{ref_id}:now"),
            InlineKeyboardButton("15분뒤", callback_data=f"pc:{ref_id}:15"),
            InlineKeyboardButton("30분뒤", callback_data=f"pc:{ref_id}:30"),
        ])
    return InlineKeyboardMarkup(rows)

def schedule_fast_scan_auto_check(ref_id: str, chat_id: str, target_min: int = 30, now_ts: float = None):
    if not ref_id:
        return
    now_ts = safe_float(now_ts, time.time())
    key = f"{ref_id}:{int(target_min)}"
    PRICE_CHECK_SCHEDULED[key] = {
        "ref_id": ref_id,
        "chat_id": chat_id,
        "target_min": int(target_min),
        "target_ts": now_ts + int(target_min) * 60,
    }
    persist_price_check_state()

latest_fast_scan_items = []
latest_fast_scan_result_ts = 0.0
latest_fast_scan_note = ""
latest_fast_scan_stage = ""
latest_fast_scan_cache_age = None
fast_scan_state_lock = threading.Lock()

def _call_with_timeout(func, *args, timeout_sec: float = API_CALL_TIMEOUT_SEC, default=None, **kwargs):
    holder = {"done": False, "value": default, "error": None}
    def runner():
        try:
            holder["value"] = func(*args, **kwargs)
        except Exception as e:
            holder["error"] = e
        finally:
            holder["done"] = True
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(max(float(timeout_sec), 0.1))
    if t.is_alive():
        return default
    if holder["error"] is not None:
        raise holder["error"]
    return holder["value"]

def get_price(ticker: str) -> float:
    try:
        p = _call_with_timeout(pybithumb.get_current_price, ticker, default=None)
        return -1 if p is None else float(p)
    except Exception:
        return -1

def get_orderbook_best_ask(ticker: str) -> float:
    try:
        ob = _call_with_timeout(pybithumb.get_orderbook, ticker, default=None)
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
        return _call_with_timeout(pybithumb.get_ohlcv, ticker, interval=interval, default=None)
    except Exception:
        return None

def get_balance(ticker: str) -> float:
    try:
        bal = _call_with_timeout(bithumb.get_balance, ticker, default=None)
        return float(bal[0] or 0) if bal else 0.0
    except Exception:
        return 0.0

def get_krw_balance():
    try:
        bal = _call_with_timeout(bithumb.get_balance, "BTC", default=None)
        if not bal or len(bal) < 4:
            return 0.0
        return float(bal[2] or 0)
    except Exception:
        return 0.0

def get_cached_market_tickers(force=False):
    global market_all_tickers, market_all_tickers_time
    now_ts = time.time()
    if (not force) and market_all_tickers and (now_ts - market_all_tickers_time < TICKER_LIST_REFRESH_SEC):
        return list(market_all_tickers)
    try:
        mark_main_loop_heartbeat(stage="fetch_tickers")
    except Exception:
        pass
    try:
        tickers = _call_with_timeout(pybithumb.get_tickers, default=None)
        clean = []
        for raw in tickers or []:
            ticker = str(raw or "").upper().strip()
            if not ticker or ticker == "BTC":
                continue
            clean.append(ticker)
        if clean:
            market_all_tickers = clean
            market_all_tickers_time = now_ts
    except Exception as e:
        print(f"[티커 목록 조회 오류] {e}", flush=True)
    return list(market_all_tickers)

def request_market_refresh(force: bool = False, reason: str = "loop") -> bool:
    global market_refresh_in_progress, market_refresh_started_ts
    global last_market_refresh_request_ts, last_market_refresh_reason, last_market_refresh_result
    now_ts = time.time()
    with market_refresh_lock:
        if market_refresh_in_progress:
            age = now_ts - market_refresh_started_ts if market_refresh_started_ts else 0.0
            if age < MARKET_REFRESH_STUCK_RESET_SEC:
                if force and reason:
                    last_market_refresh_reason = str(reason)
                return False
            print(f"[market_refresh] stuck reset / {age:.1f}초 / reason={last_market_refresh_reason}", flush=True)
            market_refresh_in_progress = False
            market_refresh_started_ts = 0.0
            last_market_refresh_result = f"강제 재시도 / {int(age)}초 정체"
        market_refresh_in_progress = True
        market_refresh_started_ts = now_ts
        last_market_refresh_request_ts = now_ts
        last_market_refresh_reason = str(reason or "loop")
        last_market_refresh_result = "실행 중"
    threading.Thread(target=_market_refresh_worker, kwargs={"force": force, "reason": reason}, daemon=True).start()
    return True

def _save_fast_scan_state(items, note: str = "", stage: str = "", cache_age=None):
    global latest_fast_scan_items, latest_fast_scan_result_ts, latest_fast_scan_note, latest_fast_scan_stage, latest_fast_scan_cache_age
    with fast_scan_state_lock:
        latest_fast_scan_items = list(items or [])[:FAST_SCAN_RESULT_MAX]
        latest_fast_scan_result_ts = time.time()
        latest_fast_scan_note = str(note or "")
        latest_fast_scan_stage = str(stage or "")
        latest_fast_scan_cache_age = cache_age
    try:
        update_scan_debug_snapshot([dict(x) for x in latest_fast_scan_items], note=latest_fast_scan_note)
    except Exception:
        pass


def get_fast_scan_state(now_ts: float = None) -> dict:
    now_ts = safe_float(now_ts, time.time())
    with fast_scan_state_lock:
        age_sec = int(now_ts - latest_fast_scan_result_ts) if latest_fast_scan_result_ts else None
        return {
            "items": list(latest_fast_scan_items),
            "age_sec": age_sec,
            "note": str(latest_fast_scan_note or ""),
            "stage": str(latest_fast_scan_stage or ""),
            "cache_age": latest_fast_scan_cache_age,
        }


def format_fast_scan_high_gap_text(gap_pct: float) -> str:
    gap_pct = safe_float(gap_pct, 999)
    if gap_pct >= 900:
        return "고점 거리 확인 안 됨"
    if gap_pct >= 0:
        return f"최근고점까지 {gap_pct:.2f}% 남음"
    return f"최근고점 {abs(gap_pct):.2f}% 돌파"

def get_fast_scan_bucket_text(bucket: str) -> str:
    key = str(bucket or "").strip()
    return {
        "user_alert": "사용자 알림",
        "internal_watch": "내부 감시",
        "discard": "버릴 후보",
    }.get(key, "내부 감시")

def get_fast_scan_label_text(primary_reason: str) -> str:
    text = str(primary_reason or "").strip()
    if text == "실시간 급등 초입":
        return "실시간 강세 초입형"
    if text == "초입 가능 후보":
        return "초반 선점형"
    if text == "재출발 확인 후보":
        return "재출발 확인형"
    if text == "초입/회복 확인 후보":
        return "초반/회복 확인형"
    return "구경만 후보"

def build_fast_scan_one_liner(primary_reason: str, from_low: float, below_high: float, grade: str = "", bucket: str = "") -> str:
    label = get_fast_scan_label_text(primary_reason)
    gap_text = format_fast_scan_high_gap_text(below_high)
    if str(bucket or "") == "discard":
        return f"지금 사기엔 별로. 30분 저점대비 {from_low:+.2f}% / {gap_text}"
    if str(primary_reason or "") == "실시간 급등 초입":
        return f"{label}. 거래량 붙는 초입이라 빠르게 볼 자리 / {gap_text}"
    if str(primary_reason or "") == "초입 가능 후보":
        return f"{label}. 힘 붙기 시작하는지 볼 자리 / 30분 저점대비 {from_low:+.2f}% / {gap_text}"
    if str(primary_reason or "") == "재출발 확인 후보":
        return f"{label}. 다시 사는지 확인할 자리 / 30분 저점대비 {from_low:+.2f}% / {gap_text}"
    if str(primary_reason or "") == "초입/회복 확인 후보":
        return f"{label}. 아직 한 끗 부족해서 더 확인 필요 / 30분 저점대비 {from_low:+.2f}% / {gap_text}"
    return f"지금은 구경만 / 30분 저점대비 {from_low:+.2f}% / {gap_text}"


def compute_fast_scan_turnover_metrics(df1, price: float = 0.0) -> dict:
    price = safe_float(price, 0)
    if df1 is None or len(df1) < 1:
        return {"turnover_1m": 0.0, "turnover_3m": 0.0, "turnover_5m": 0.0}
    try:
        closes = df1["close"].astype(float)
        vols = df1["volume"].astype(float)
        values = closes * vols
        if price > 0 and len(values) >= 1:
            values = values.copy()
            values.iloc[-1] = float(vols.iloc[-1]) * price
        return {
            "turnover_1m": float(values.tail(1).sum()),
            "turnover_3m": float(values.tail(3).sum()),
            "turnover_5m": float(values.tail(5).sum()),
        }
    except Exception:
        return {"turnover_1m": 0.0, "turnover_3m": 0.0, "turnover_5m": 0.0}


def fast_scan_turnover_gate(item: dict, level: str = "watch") -> bool:
    level = str(level or "watch").strip().lower()
    turnover_15m = safe_float(item.get("turnover", 0), 0)
    turnover_3m = safe_float(item.get("turnover_3m", 0), 0)
    turnover_5m = safe_float(item.get("turnover_5m", 0), 0)
    turnover_rank = int(safe_float(item.get("turnover_rank", 999), 999))
    if level == "strong":
        if turnover_15m >= FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_15M:
            return True
        if turnover_3m >= FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_3M and turnover_5m >= FAST_SCAN_STRONG_ALERT_MIN_TURNOVER_5M:
            return True
        return turnover_rank > 0 and turnover_rank <= 18 and turnover_3m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_3M
    if level == "user":
        if turnover_15m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_15M:
            return True
        if turnover_3m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_3M and turnover_5m >= FAST_SCAN_USER_ALERT_MIN_TURNOVER_5M:
            return True
        return turnover_rank > 0 and turnover_rank <= 25 and turnover_3m >= FAST_SCAN_WATCH_MIN_TURNOVER_3M and turnover_5m >= FAST_SCAN_WATCH_MIN_TURNOVER_5M
    if turnover_15m >= FAST_SCAN_WATCH_MIN_TURNOVER_15M:
        return True
    if turnover_3m >= FAST_SCAN_WATCH_MIN_TURNOVER_3M or turnover_5m >= FAST_SCAN_WATCH_MIN_TURNOVER_5M:
        return True
    return turnover_rank > 0 and turnover_rank <= 35 and turnover_5m >= max(5000000, FAST_SCAN_WATCH_MIN_TURNOVER_3M * 0.8)


def get_fast_scan_early_quality(item: dict) -> str:
    primary = str(item.get("primary_reason", "") or "")
    change_5 = safe_float(item.get("change_5", 0), 0)
    change_3 = safe_float(item.get("change_3", 0), 0)
    vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
    leader = safe_float(item.get("leader_score", 0), 0)
    from_low = safe_float(item.get("from_low", 0), 0)
    below_high = safe_float(item.get("below_high", 999), 999)
    chart_gap = safe_float(item.get("chart_gap_pct", below_high), below_high)
    chart_rise = safe_float(item.get("chart_rise_pct", from_low), from_low)
    zone_ratio = safe_float(item.get("chart_zone_ratio", -1), -1)
    ctx = item.get("alert_chart_context", {}) if isinstance(item.get("alert_chart_context"), dict) else {}
    state_code = str(ctx.get("chart_state_code", "") or "")
    turnover_ok_user = fast_scan_turnover_gate(item, level="user")
    turnover_ok_strong = fast_scan_turnover_gate(item, level="strong")

    if primary == "실시간 급등 초입":
        if (
            change_5 >= 0.95 and change_3 >= 0.28 and vol_ratio >= 4.0 and leader >= 1.35
            and from_low <= 2.20 and below_high >= 1.00 and chart_gap >= 1.00
            and chart_rise <= 2.70 and state_code not in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}
            and turnover_ok_strong
        ):
            return "strong"
        if (
            change_5 >= 0.70 and vol_ratio >= 2.40 and leader >= 0.95 and below_high >= 0.85
            and chart_gap >= 0.85 and chart_rise <= 3.20 and state_code not in {"FADE_AFTER_SPIKE", "EXTENDED"}
            and turnover_ok_user
        ):
            return "user"
        return "watch"

    if primary == "초입 가능 후보":
        if (
            change_5 >= 0.55 and change_3 >= 0.10 and vol_ratio >= 2.20 and leader >= 0.95
            and from_low <= 1.90 and below_high >= 1.15 and chart_gap >= 1.15
            and chart_rise <= 2.50 and zone_ratio <= 0.72
            and state_code not in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}
            and turnover_ok_user
        ):
            return "user"
        if (
            change_5 >= 0.35 and vol_ratio >= 1.45 and leader >= 0.70 and below_high >= 0.90
            and chart_gap >= 0.90 and chart_rise <= 3.30 and state_code not in {"FADE_AFTER_SPIKE", "EXTENDED"}
            and fast_scan_turnover_gate(item, level="watch")
        ):
            return "watch"
        return "discard"

    if primary == "재출발 확인 후보":
        if (
            change_5 >= 0.85 and vol_ratio >= 3.00 and leader >= 1.05 and from_low >= 1.00
            and below_high >= 2.05 and chart_gap >= 1.60 and chart_rise <= 2.90
            and state_code not in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}
            and turnover_ok_user
        ):
            return "user"
        if (
            change_5 >= 0.25 and vol_ratio >= 1.25 and leader >= 0.55 and from_low >= 0.35
            and below_high >= 0.90 and fast_scan_turnover_gate(item, level="watch")
        ):
            return "watch"
        return "discard"

    return "watch" if fast_scan_turnover_gate(item, level="watch") else "discard"


def refine_fast_scan_item_with_chart(item: dict, df1):
    try:
        if not isinstance(item, dict) or df1 is None or len(df1) < 12:
            return item
        price = safe_float(item.get("price", 0), 0)
        apply_chart_position_profile(item, df1, lookback=CHART_POS_LOOKBACK_1M)
        item["alert_chart_context"] = compute_alert_chart_context_from_df(df1, df4h=None, current_price=price)
        ctx = item.get("alert_chart_context", {}) if isinstance(item.get("alert_chart_context"), dict) else {}
        state_code = str(ctx.get("chart_state_code", "") or "")
        chart_gap = safe_float(item.get("chart_gap_pct", item.get("below_high", 999)), 999)
        chart_rise = safe_float(item.get("chart_rise_pct", item.get("from_low", 0)), 0)
        zone_ratio = safe_float(item.get("chart_zone_ratio", -1), -1)
        vol_ratio = safe_float(item.get("vol_ratio", 0), 0)
        leader_score = safe_float(item.get("leader_score", 0), 0)
        change_5 = safe_float(item.get("change_5", 0), 0)
        from_low = safe_float(item.get("from_low", 0), 0)
        below_high = safe_float(item.get("below_high", 999), 999)
        section = str(item.get("section", "") or "")
        primary = str(item.get("primary_reason", "") or "")
        reasons = list(item.get("reasons", [])) if isinstance(item.get("reasons"), list) else []
        def add_reason(text: str):
            if text and text not in reasons:
                reasons.append(text)
        if state_code in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"}:
            add_reason("차트 모양이 아직 별로")
            if section == "strong":
                section = "watch"
                if primary == "실시간 급등 초입":
                    primary = "초입/회복 확인 후보"
        if chart_gap <= 1.05 and chart_rise >= 0.95:
            add_reason("고점이 가까워서 늦을 수 있음")
            if section == "strong":
                section = "watch"
        if zone_ratio >= 0.82 and chart_gap <= 1.45 and chart_rise >= 0.90:
            add_reason("위쪽 구간이라 더 확인 필요")
            if section == "strong":
                section = "watch"
        if not fast_scan_turnover_gate(item, level="watch"):
            add_reason("실제 거래대금이 아직 작음")
            if section == "strong":
                section = "watch"
        early_quality = get_fast_scan_early_quality(item)
        if primary == "실시간 급등 초입":
            if early_quality == "watch":
                add_reason("실시간 강세 초입으로 보기엔 힘이 덜 붙음")
                section = "watch"
            elif early_quality == "discard":
                add_reason("실시간 강세 초입으로 보기엔 아직 애매함")
                section = "weak"
                primary = "지금은 구경만"
        elif primary == "초입 가능 후보":
            if early_quality == "watch":
                add_reason("막 힘 붙는 자리로 보기엔 아직 애매함")
                section = "watch"
            elif early_quality == "discard":
                add_reason("초반 선점형으로 보기엔 아직 너무 약함")
                section = "weak"
                primary = "지금은 구경만"
        if primary == "재출발 확인 후보":
            strong_restart = (
                change_5 >= 0.85 and vol_ratio >= 3.00 and leader_score >= 1.05 and from_low >= 1.00 and below_high >= 2.05 and chart_gap >= 1.60 and state_code not in {"FADE_AFTER_SPIKE", "HIGH_WOBBLE", "EXTENDED"} and fast_scan_turnover_gate(item, level="user")
            )
            if not strong_restart:
                add_reason("재출발 힘이 아직 약함")
                section = "watch"
        item["section"] = section
        item["reasons"] = reasons[:5]
        item["primary_reason"] = primary
    except Exception:
        return item
    return item

def classify_fast_scan_alert_section(change_5: float, change_3: float, vol_ratio: float, leader_score: float, from_low: float, below_high: float, rsi: float) -> tuple[str, str, list]:
    reasons = []
    primary = "지금은 구경만"
    section = "weak"
    strong_surge = (
        change_5 >= FAST_SCAN_SURGE_CHANGE5
        and vol_ratio >= FAST_SCAN_SURGE_VOL
        and leader_score >= FAST_SCAN_SURGE_LEADER
        and below_high >= 0.90
        and from_low <= 2.60
        and rsi < 82
    )
    strong_early = (
        change_5 >= FAST_SCAN_STRONG_CHANGE5
        and change_3 >= FAST_SCAN_ALERT_MIN_CHANGE3
        and vol_ratio >= FAST_SCAN_STRONG_VOL
        and leader_score >= FAST_SCAN_STRONG_LEADER
        and below_high >= FAST_SCAN_ALERT_MIN_HIGH_GAP
        and FAST_SCAN_ALERT_MIN_FROM_LOW <= from_low <= FAST_SCAN_ALERT_MAX_FROM_LOW
        and (change_5 + max(change_3, 0)) >= FAST_SCAN_ALERT_MIN_COMBINED
        and rsi < 84
    )
    recovery_watch = (
        from_low >= 0.18
        and change_5 >= 0.12
        and vol_ratio >= 1.20
        and leader_score >= 0.45
        and below_high >= 0.75
        and from_low <= 2.80
    )
    if strong_surge:
        section = "strong"
        primary = "실시간 급등 초입"
    elif strong_early:
        section = "strong"
        primary = "초입 가능 후보"
    elif recovery_watch:
        section = "watch"
        primary = "재출발 확인 후보"
    elif from_low >= 0.10 and vol_ratio >= FAST_SCAN_WATCH_VOL and leader_score >= FAST_SCAN_WATCH_LEADER:
        section = "watch"
        primary = "초입/회복 확인 후보"

    if below_high <= 0.35:
        reasons.append("고점이 너무 가까움")
        if section == "strong":
            section = "watch"
            primary = "초입/회복 확인 후보"
    if from_low > 3.60 and below_high < 1.20:
        reasons.append("이미 많이 올라와서 늦음")
        section = "weak"
        primary = "지금은 구경만"
    if change_5 < FAST_SCAN_WATCH_CHANGE5:
        reasons.append("5분 상승이 아직 약함")
        if section == "strong":
            section = "watch"
    if vol_ratio < 1.15:
        reasons.append("거래량이 아직 약함")
        if section == "strong":
            section = "watch"
    if leader_score < 0.30 and vol_ratio < 1.30:
        reasons.append("시장주목도가 아직 약함")
        if section != "weak":
            section = "watch" if change_5 >= 0.12 else "weak"
    if rsi >= 84 or (change_5 >= 1.90 and vol_ratio < 6.00):
        reasons.append("너무 빠르게 오른 편")
        if section == "strong":
            section = "watch"

    ordered = [primary] + [r for r in reasons if r != primary]
    return section, primary, ordered[:4]

def classify_fast_scan_grade_bucket(section: str, primary_reason: str, change_5: float, change_3: float, vol_ratio: float, leader_score: float, from_low: float, below_high: float, rsi: float, lite_score: float, item: dict | None = None) -> tuple[str, str]:
    section = str(section or '').strip()
    primary_reason = str(primary_reason or '').strip()
    item = item or {}
    chart_bad = _fast_chart_bad(item)
    turnover_s_ok = _fast_abs_turnover_ok(item, 's')
    turnover_a_ok = _fast_abs_turnover_ok(item, 'a')
    turnover_watch_ok = _fast_abs_turnover_ok(item, 'watch')
    turnover_rescue_ok = _fast_abs_turnover_ok(item, 'rescue')
    drought_sec = _fast_user_alert_drought_sec()

    if section == 'strong':
        direct_live = primary_reason in {'실시간 급등 초입', '초입 가능 후보'}
        direct_early = primary_reason == '초반 선점형'
        s_change5 = 0.92
        s_change3 = 0.18
        s_vol = 2.45
        s_leader = 1.05
        s_from_low_min = 0.10
        s_from_low_max = 2.05
        s_below_high = 1.05

        a_change5 = 0.34
        a_change3 = -0.02
        a_vol = 1.35
        a_leader = 0.62
        a_from_low_min = 0.04
        a_from_low_max = 2.35
        a_below_high = 0.82
        a_lite = 9.3

        # 알림이 너무 마르면 직접 잡힌 실시간/초반형만 한 단계 더 살린다.
        if drought_sec >= 7200 and (direct_live or direct_early):
            a_change5 = 0.22
            a_change3 = -0.06
            a_vol = 1.18
            a_leader = 0.45
            a_from_low_min = 0.02
            a_from_low_max = 2.45
            a_below_high = 0.72
            a_lite = 8.6

        if (
            primary_reason == '실시간 급등 초입'
            and change_5 >= s_change5 and change_3 >= s_change3
            and vol_ratio >= s_vol and leader_score >= s_leader
            and s_from_low_min <= from_low <= s_from_low_max and below_high >= s_below_high
            and rsi < 80 and turnover_s_ok and not chart_bad
        ):
            return 'S', 'user_alert'
        if (
            primary_reason in {'실시간 급등 초입', '초입 가능 후보', '초반 선점형'}
            and change_5 >= a_change5 and change_3 >= a_change3
            and vol_ratio >= a_vol and leader_score >= a_leader
            and a_from_low_min <= from_low <= a_from_low_max and below_high >= a_below_high
            and lite_score >= a_lite and rsi < 82 and turnover_a_ok and not chart_bad
        ):
            return 'A', 'user_alert'
        if turnover_watch_ok and change_5 >= 0.10 and vol_ratio >= 0.98 and below_high >= 0.62:
            return 'B', 'internal_watch'
        return 'C', 'discard'

    if section == 'watch':
        restart_like = primary_reason == '재출발 확인 후보'
        weak_restart = _fast_rescue_has_weak_reason(item)
        rescue_like = (
            primary_reason in {'재출발 확인 후보', '초입/회복 확인 후보', '초입 가능 후보'}
            and change_5 >= 0.66 and vol_ratio >= 2.20 and leader_score >= 0.78
            and 0.28 <= from_low <= 2.10 and below_high >= 1.45
            and lite_score >= max(FAST_SCAN_RESCUE_SCORE_MIN - 0.6, 13.8) and rsi < 78
            and turnover_rescue_ok and not chart_bad and not weak_restart
            and safe_float(item.get('change_30', 0), 0) >= -0.55
        )
        if rescue_like:
            return 'A', 'user_alert'
        if restart_like:
            if (
                change_5 >= 0.72 and vol_ratio >= 2.55 and leader_score >= 0.95
                and from_low >= 0.55 and below_high >= 2.10 and lite_score >= 14.8
                and turnover_a_ok and not chart_bad and not weak_restart
                and safe_float(item.get('change_30', 0), 0) >= -0.45
            ):
                return 'A', 'user_alert'
            if turnover_watch_ok and change_5 >= 0.12 and from_low >= 0.18 and below_high >= 0.75:
                return 'B', 'internal_watch'
            return 'C', 'discard'
        if turnover_watch_ok and change_5 >= 0.10 and vol_ratio >= 1.02 and leader_score >= 0.30 and below_high >= 0.55:
            return 'B', 'internal_watch'
        return 'C', 'discard'

    return 'C', 'discard'


def _rescue_internal_watch_items(items):
    if not items:
        return []
    now_ts = time.time()
    drought_sec = _fast_user_alert_drought_sec(now_ts)
    promoted = 0
    user_count = sum(1 for x in items if str(x.get('alert_bucket', '')) == 'user_alert' and str(x.get('alert_grade', '')) in {'S', 'A'})
    if user_count >= FAST_SCAN_ALERT_LIMIT:
        return list(items)
    internal_sorted = sorted(
        [x for x in items if str(x.get('alert_bucket', '')) == 'internal_watch'],
        key=_fast_rescue_score,
        reverse=True,
    )
    upgrades = {}
    dynamic_limit = FAST_SCAN_RESCUE_LIMIT + (1 if drought_sec >= 7200 else 0)
    for item in internal_sorted:
        if promoted >= dynamic_limit or user_count + promoted >= FAST_SCAN_ALERT_LIMIT:
            break
        ticker = str(item.get('ticker', '') or '')
        if not ticker:
            continue
        if _fast_rescue_score(item) < FAST_SCAN_RESCUE_SCORE_MIN:
            continue
        if not _fast_abs_turnover_ok(item, 'rescue') or _fast_chart_bad(item) or _fast_rescue_has_weak_reason(item):
            continue
        regime = item.get('market_regime') if isinstance(item.get('market_regime'), dict) else get_market_regime()
        good_market = is_good_market_for_user_rescue(regime)
        change_5 = safe_float(item.get('change_5', 0), 0)
        change_30 = safe_float(item.get('change_30', 0), 0)
        change_3 = safe_float(item.get('change_3', 0), 0)
        vol_ratio = safe_float(item.get('vol_ratio', 0), 0)
        leader = safe_float(item.get('leader_score', 0), 0)
        below_high = safe_float(item.get('below_high', 999), 999)
        from_low = safe_float(item.get('from_low', 0), 0)
        turnover_5m = safe_float(item.get('turnover_5m', 0), 0)
        primary_reason = str(item.get('primary_reason', '') or '')
        rescue_ok = (
            change_5 >= 0.50
            and vol_ratio >= 2.00
            and leader >= 0.72
            and below_high >= 1.25
            and 0.12 <= from_low <= 2.10
            and turnover_5m >= FAST_SCAN_RESCUE_MIN_TURNOVER_5M
            and change_30 >= -0.55
        )
        if primary_reason == '실시간 급등 초입':
            rescue_ok = rescue_ok and change_5 >= 0.76 and change_3 >= 0.00 and vol_ratio >= 2.20 and leader >= 0.82 and below_high >= 1.45 and turnover_5m >= 22000000 and change_30 >= -0.35
            if drought_sec >= 7200:
                rescue_ok = rescue_ok and change_5 >= 0.62 and vol_ratio >= 1.90 and below_high >= 1.20 and turnover_5m >= 19000000
            if not good_market:
                rescue_ok = rescue_ok and turnover_5m >= 36000000 and change_5 >= 1.05 and below_high >= 2.10 and change_30 >= -0.05
        elif primary_reason == '초반 선점형':
            rescue_ok = rescue_ok and change_5 >= 0.62 and change_3 >= -0.02 and vol_ratio >= 1.90 and leader >= 0.70 and below_high >= 1.30 and turnover_5m >= 19000000 and from_low <= 1.95
            if drought_sec >= 7200:
                rescue_ok = rescue_ok and change_5 >= 0.52 and vol_ratio >= 1.70 and below_high >= 1.10 and turnover_5m >= 17000000
        elif primary_reason == '재출발 확인 후보':
            # 재출발형은 계속 보수적으로 유지
            rescue_ok = rescue_ok and change_5 >= 0.92 and vol_ratio >= 3.00 and leader >= 1.02 and from_low >= 0.70 and below_high >= 2.55 and turnover_5m >= 30000000 and change_30 >= -0.15
        else:
            rescue_ok = rescue_ok and change_5 >= 0.74 and vol_ratio >= 2.55 and below_high >= 1.90 and turnover_5m >= 28000000
        if not rescue_ok:
            continue
        new_item = dict(item)
        new_item['alert_bucket'] = 'user_alert'
        new_item['alert_grade'] = 'A'
        new_item['bucket_rank'] = 2
        reasons = list(new_item.get('reasons', [])) if isinstance(new_item.get('reasons'), list) else []
        reasons = [r for r in reasons if '힘이 아직 약함' not in str(r) and '다시 사는지 확인할 자리' not in str(r)]
        if '좋은 흐름이라 rescue 승격' not in reasons:
            reasons.insert(0, '좋은 흐름이라 rescue 승격')
        if primary_reason == '재출발 확인 후보':
            if '유지력 확인이 더 필요한 자리' not in reasons:
                reasons.append('재출발은 다시 밀릴 수 있어 유지력도 같이 확인')
        else:
            reasons = [r for r in reasons if '유지력 확인이 더 필요한 자리' not in str(r)]
        new_item['reasons'] = reasons[:5]
        one_liner = str(new_item.get('one_liner', '') or '지금은 흐름만 확인')
        if primary_reason == '재출발 확인 후보':
            one_liner = one_liner.replace('재출발 힘이 아직 약함', '재출발 힘이 붙는지 재확인')
            new_item['one_liner'] = one_liner + ' / 감시 상위라 다시 올렸지만 재출발은 끝까지 확인 필요'
        else:
            new_item['one_liner'] = one_liner
        upgrades[ticker] = new_item
        promoted += 1
    if not upgrades:
        return list(items)
    merged = [upgrades.get(str(x.get('ticker', '') or ''), x) for x in items]
    merged.sort(
        key=lambda x: (
            safe_float(x.get('bucket_rank', 0)),
            3 if str(x.get('alert_grade', '')) == 'S' else 2 if str(x.get('alert_grade', '')) == 'A' else 1 if str(x.get('alert_grade', '')) == 'B' else 0,
            safe_float(x.get('section_rank', 0)),
            _fast_rescue_score(x),
        ),
        reverse=True,
    )
    return merged


def get_fast_scan_stage1_rows(cache: dict, limit: int = FAST_SCAN_INPUT_LIMIT):
    if not isinstance(cache, dict) or not cache:
        return []
    ranked_rows = get_watchlist_ranked_cache_items(cache, limit=max(int(limit), FAST_SCAN_STAGE1_POOL_LIMIT))
    picked = {}

    def add_rows(rows):
        for ticker, data, score in rows:
            key = str(ticker or '').upper().strip()
            if not key or key in picked:
                continue
            picked[key] = (ticker, data, score)
            if len(picked) >= max(int(limit), FAST_SCAN_STAGE1_POOL_LIMIT):
                break

    add_rows(ranked_rows[:FAST_SCAN_STAGE1_SCORE_TOP])

    turnover_rows = sorted(
        ranked_rows,
        key=lambda x: (
            safe_float(x[1].get('turnover_5m', x[1].get('turnover', 0)), 0),
            -int(safe_float(x[1].get('turnover_rank', 999), 999)),
        ),
        reverse=True,
    )
    surge_rows = sorted(
        ranked_rows,
        key=lambda x: (
            safe_float(x[1].get('change_5', 0), 0),
            safe_float(x[1].get('vol_ratio', x[1].get('vol_ratio_1m', 0)), 0),
            safe_float(x[1].get('leader_score', 0), 0),
        ),
        reverse=True,
    )
    leader_rows = sorted(
        ranked_rows,
        key=lambda x: (
            safe_float(x[1].get('leader_score', 0), 0),
            safe_float(x[1].get('vol_ratio', x[1].get('vol_ratio_1m', 0)), 0),
            safe_float(x[1].get('change_5', 0), 0),
        ),
        reverse=True,
    )
    change_rows = sorted(
        ranked_rows,
        key=lambda x: (
            safe_float(x[1].get('change_3', 0), 0) + safe_float(x[1].get('change_5', 0), 0),
            safe_float(x[1].get('turnover_5m', x[1].get('turnover', 0)), 0),
            safe_float(x[1].get('leader_score', 0), 0),
        ),
        reverse=True,
    )

    add_rows(turnover_rows[:FAST_SCAN_STAGE1_TURNOVER_TOP])
    add_rows(surge_rows[:FAST_SCAN_STAGE1_SURGE_TOP])
    add_rows(leader_rows[:FAST_SCAN_STAGE1_LEADER_TOP])
    add_rows(change_rows[:FAST_SCAN_STAGE1_CHANGE5_TOP])

    rows = list(picked.values())
    rows.sort(
        key=lambda x: (
            safe_float(x[1].get('turnover_5m', x[1].get('turnover', 0)), 0) * 0.00000001
            + safe_float(x[1].get('leader_score', 0), 0) * 1.4
            + max(safe_float(x[1].get('vol_ratio', x[1].get('vol_ratio_1m', 0)), 0) - 0.9, 0) * 1.1
            + max(safe_float(x[1].get('change_5', 0), 0), 0) * 0.9
            + max(safe_float(x[1].get('change_3', 0), 0), 0) * 0.45
        ),
        reverse=True,
    )
    return rows[:max(1, int(limit))]


def build_fast_scan_candidates_from_cache(cache: dict, limit: int = FAST_SCAN_RESULT_MAX):
    stage1_rows = get_fast_scan_stage1_rows(cache, limit=max(int(limit) * 2, FAST_SCAN_INPUT_LIMIT))
    items = []
    for ticker, data, _score in stage1_rows:
        item = _build_fast_scan_item(ticker, data)
        if not item:
            continue
        items.append(item)
    items = _rescue_internal_watch_items(items)
    items.sort(
        key=lambda x: (
            safe_float(x.get('bucket_rank', 0)),
            3 if str(x.get('alert_grade', '')) == 'S' else 2 if str(x.get('alert_grade', '')) == 'A' else 1 if str(x.get('alert_grade', '')) == 'B' else 0,
            safe_float(x.get('section_rank', 0)),
            _fast_rescue_score(x),
            safe_float(x.get('change_5', 0)),
        ),
        reverse=True,
    )
    return items[:max(1, int(limit))]


def build_scan_health_warning_text(now_ts: float = None) -> str:
    now_ts = safe_float(now_ts, time.time())
    info = get_scan_runtime_health(now_ts)
    fast = get_fast_scan_state(now_ts)
    lines = []
    fast_age = fast.get('age_sec')
    if fast_age is None:
        lines.append('⚠️ 아직 새 후보 기록이 없어')
    elif fast_age >= SCAN_HEARTBEAT_WARN_SEC:
        lines.append(f'⚠️ 후보 계산이 {fast_age}초째 안 바뀐 것 같아')
    cache_age = fast.get('cache_age')
    if cache_age is not None and cache_age >= SCAN_CACHE_WARN_SEC:
        lines.append(f'ℹ️ 지금 후보는 {cache_age}초 전 시장 데이터 기준이야')
    stage = fast.get('stage') or info.get('stage')
    if stage:
        lines.append(f'ℹ️ 최근 단계: {_friendly_fast_stage_text(stage)}')
    refresh_info = get_market_refresh_status(now_ts)
    if refresh_info.get('in_progress'):
        lines.append(f"🔄 시장 데이터 다시 받는 중 ({refresh_info.get('age_sec', 0)}초)")
    if info.get('reset_reason'):
        lines.append(f"🛠 최근 자동 복구: {info['reset_reason']}")
    return '\n'.join(lines).strip()


def get_scan_debug_candidates():
    now_ts = time.time()
    fast = get_fast_scan_state(now_ts)
    items = list(fast.get('items') or [])
    if items:
        cache_age = fast.get('cache_age')
        note = f'최근 {cache_age}초 안 시장 데이터 기준' if cache_age is not None else '방금 고른 후보 기준'
        return items, note
    cache = shared_market_cache if isinstance(shared_market_cache, dict) else {}
    if cache:
        cache_age = int(now_ts - shared_market_cache_time) if shared_market_cache_time else None
        items = build_fast_scan_candidates_from_cache(cache, limit=FAST_SCAN_RESULT_MAX)
        _save_fast_scan_state(items, note='빠른 후보 스캔 기준', stage='scan_watchlist_fast', cache_age=cache_age)
        note = f'최근 {cache_age}초 안 시장 데이터 기준' if cache_age is not None else '최근 시장 데이터 기준'
        return items, note
    return [], '아직 볼 만한 후보가 안 잡혔어'


def build_scan_debug_text():
    results, note = get_scan_debug_candidates()
    lines = ['🔎 지금 볼 후보 보기']
    health_warning = build_scan_health_warning_text()
    if health_warning:
        lines.append(health_warning)
    if note:
        lines.append(f'안내: {note}')
    if not results:
        lines.append('')
        lines.append('지금 볼 후보 없음')
        return '\n'.join(lines)
    user_items, internal_items, discard_items = split_scan_debug_results(results)
    def append_group(title: str, items, icon: str):
        if not items:
            return
        lines.append('')
        lines.append(title)
        for i, item in enumerate(items, 1):
            price_text = f"{safe_float(item.get('price', 0)):,.4f}".rstrip('0').rstrip('.')
            lines.append('')
            lines.append(highlight_line(i, item.get('ticker','?'), icon))
            lines.append(f"- 현재가: {price_text} / 등급 {item.get('alert_grade','B')} / {get_fast_scan_bucket_text(item.get('alert_bucket',''))}")
            lines.append(f"- 유형: {item.get('label_text','후보')} / 5분 {safe_float(item.get('change_5',0)):+.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 주목도 {safe_float(item.get('leader_score',0)):.2f}")
            lines.append(f"- 최근 5분 거래대금: {fmt_price(item.get('turnover_5m', 0))}")
            lines.append(f"- 30분 흐름: 저점대비 {safe_float(item.get('from_low',0),0):+.2f}% / {format_fast_scan_high_gap_text(safe_float(item.get('below_high',999),999))}")
            if item.get('one_liner'):
                lines.append(f"- 한줄평: {item.get('one_liner')}")
            lines.append(f"- 판단: {' / '.join(item.get('reasons', [])[:3])}")
    append_group('🌱 바로 볼 후보', user_items, '🔥')
    append_group('👀 조금 더 볼 후보', internal_items, '👀')
    hidden_count = len(discard_items)
    if hidden_count:
        lines.append('')
        lines.append(f'🙈 내부에서만 넘긴 후보 {hidden_count}개 (평소엔 숨기고 놓쳤을 때만 보면 됨)')
    return '\n'.join(lines)


def append_debug_shortlist_parts(parts: list):
    try:
        results, note = get_scan_debug_candidates()
        header = '🔎 지금 확인할 상위 후보'
        if note:
            header += f' ({note})'
        if not results:
            parts.append(header + '\n\n지금은 올릴 만한 후보 없음')
            return
        user_items, internal_items, discard_items = split_scan_debug_results(results)
        shortlist = (user_items + internal_items)[:3]
        lines = [header]
        if not shortlist:
            hidden_count = len(discard_items)
            lines.append('• 지금은 올릴 만한 후보 없음')
            if hidden_count:
                lines.append(f'  - 내부에서 넘긴 후보 {hidden_count}개 (평소엔 숨김)')
            parts.append('\n'.join(lines))
            return
        for idx, item in enumerate(shortlist, 1):
            bucket = str(item.get('alert_bucket', '') or '')
            label = '바로 볼 후보' if bucket == 'user_alert' else '조금 더 볼 후보'
            lines.append('')
            lines.append(highlight_line(idx, item.get('ticker','?'), '🔥' if bucket == 'user_alert' else '👀'))
            lines.append(f"- 상태: {label} / 5분 {safe_float(item.get('change_5',0)):+.2f}% / 거래량 {safe_float(item.get('vol_ratio',0)):.2f}배 / 최근 5분 거래대금 {fmt_price(item.get('turnover_5m', 0))}")
            if item.get('one_liner'):
                lines.append(f"- 한줄: {item.get('one_liner')}")
        hidden_count = len(discard_items)
        if hidden_count:
            lines.append('')
            lines.append(f'🙈 내부에서 넘긴 후보 {hidden_count}개 (평소엔 숨기고 놓쳤을 때만 이유 확인)')
        parts.append('\n'.join(lines))
    except Exception as e:
        parts.append(f'⚠️ 상위 후보 표시 에러: {e}')


def _build_fast_alert_signature(item: dict) -> str:
    ticker = str(item.get('ticker', '') or '')
    grade = str(item.get('alert_grade', '') or '')
    bucket = str(item.get('alert_bucket', '') or '')
    label = str(item.get('label_text', '') or '')
    reasons = list(item.get('reasons', [])) if isinstance(item.get('reasons'), list) else []
    key_reasons = tuple(sorted(str(x).strip() for x in reasons[:2] if str(x).strip()))
    turnover_bin = int(safe_float(item.get('turnover_5m', 0), 0) // 10000000)
    vol_bin = int(safe_float(item.get('vol_ratio', 0), 0) // 1.5)
    score_bin = int(safe_float(item.get('lite_score', 0), 0) // 5)
    return f"{ticker}|{grade}|{bucket}|{label}|{turnover_bin}|{vol_bin}|{score_bin}|{'/'.join(key_reasons)}"


def _fast_alert_has_meaningful_upgrade(prev: dict, item: dict) -> bool:
    if not isinstance(prev, dict) or not prev:
        return True
    grade_rank = {'C': 0, 'B': 1, 'A': 2, 'S': 3}
    prev_grade = str(prev.get('grade', '') or '')
    now_grade = str(item.get('alert_grade', '') or '')
    if grade_rank.get(now_grade, 0) > grade_rank.get(prev_grade, 0):
        return True
    prev_label = str(prev.get('label_text', '') or '')
    now_label = str(item.get('label_text', '') or '')
    if prev_label != now_label and grade_rank.get(now_grade, 0) >= grade_rank.get(prev_grade, 0):
        return True
    prev_price = safe_float(prev.get('price', 0), 0)
    now_price = safe_float(item.get('price', 0), 0)
    if prev_price > 0 and abs((now_price - prev_price) / prev_price) * 100.0 >= FAST_SCAN_ALERT_REPEAT_PRICE_MOVE_PCT:
        return True
    prev_turn = safe_float(prev.get('turnover_5m', 0), 0)
    now_turn = safe_float(item.get('turnover_5m', 0), 0)
    if prev_turn > 0 and (now_turn / prev_turn) >= 1.8 and safe_float(item.get('lite_score',0),0) >= safe_float(prev.get('lite_score',0),0) + 2.0:
        return True
    prev_score = safe_float(prev.get('lite_score', 0), 0)
    now_score = safe_float(item.get('lite_score', 0), 0)
    if (now_score - prev_score) >= FAST_SCAN_ALERT_REPEAT_SCORE_JUMP:
        return True
    prev_vol = safe_float(prev.get('vol_ratio', 0), 0)
    now_vol = safe_float(item.get('vol_ratio', 0), 0)
    if (now_vol - prev_vol) >= FAST_SCAN_ALERT_REPEAT_VOL_JUMP and now_turn > prev_turn * 1.25:
        return True
    return False


def _send_fast_scan_alerts(items, now_ts: float):
    sent = 0
    for item in items:
        if sent >= FAST_SCAN_ALERT_LIMIT:
            break
        if str(item.get('alert_bucket', '')) != 'user_alert':
            continue
        if str(item.get('alert_grade', '')) not in {'S', 'A'}:
            continue
        ticker = str(item.get('ticker', '') or '')
        if not ticker or ticker in active_positions or is_watch_excluded_ticker(ticker):
            continue
        type_key = str(item.get('primary_reason', '') or '')
        key = f'{ticker}:{type_key}'
        sig = _build_fast_alert_signature(item)
        prev = recent_fast_alert_signatures.get(key, {})
        last_ts = safe_float(prev.get('ts', 0), 0) if isinstance(prev, dict) else 0
        last_sig = str(prev.get('sig', '')) if isinstance(prev, dict) else ''
        if (now_ts - last_ts) < FAST_SCAN_ALERT_COOLDOWN_SEC:
            if sig == last_sig and not _fast_alert_has_meaningful_upgrade(prev, item):
                continue
            if not _fast_alert_has_meaningful_upgrade(prev, item):
                continue
        recent_signal_key = f'{ticker}:{type_key}'
        last = safe_float(recent_signal_alerts.get(recent_signal_key, 0), 0)
        if (now_ts - last) < FAST_SCAN_ALERT_COOLDOWN_SEC and not _fast_alert_has_meaningful_upgrade(prev, item):
            continue
        recent_signal_alerts[recent_signal_key] = now_ts
        recent_fast_alert_signatures[key] = {
            'ts': now_ts,
            'sig': sig,
            'grade': str(item.get('alert_grade', '') or ''),
            'price': safe_float(item.get('price', 0), 0),
            'lite_score': safe_float(item.get('lite_score', 0), 0),
            'vol_ratio': safe_float(item.get('vol_ratio', 0), 0),
            'turnover_5m': safe_float(item.get('turnover_5m', 0), 0),
            'label_text': str(item.get('label_text', '') or ''),
        }
        grade = str(item.get('alert_grade','A') or 'A')
        label_text = str(item.get('label_text','후보') or '후보')
        one_liner = str(item.get('one_liner','') or '지금은 흐름만 확인')
        prepared_item = dict(item)
        prepared_item.setdefault('ticker', ticker)
        prepared_item.setdefault('current_price', safe_float(item.get('price', 0), 0))
        prepared_item.setdefault('strategy_label', label_text)
        prepared_item.setdefault('below_30m_high_pct', safe_float(item.get('below_high', 999), 999))
        prepared_item.setdefault('chg_5m', safe_float(item.get('change_5', 0), 0))
        prepared_item.setdefault('chg_30m', 0.0)
        prepared_item.setdefault('from_30m_low_pct', safe_float(item.get('from_low', 0), 0))
        prepared_item.setdefault('judgement_text', one_liner)
        ref_id = make_price_check_ref(prepared_item, alert_type='fast_scan', source_label='빠른 후보 알림', judgement_text=one_liner)
        msg = ''.join([
            f"🌱 빠른 후보 알림\n"
            f"━━━━━━━━━━\n"
            f"{bold_number_badge(1)} {bold_ticker_text(ticker)}\n\n"
            f"- 유형/등급: {label_text} / {grade}\n"
            f"- 현재가: {fmt_price(item.get('price', 0))}\n"
            f"- 5분 상승: {safe_float(item.get('change_5',0)):.2f}%\n"
            f"- 거래량: {safe_float(item.get('vol_ratio',0)):.2f}배\n"
            f"- 시장주목도: {safe_float(item.get('leader_score',0)):.2f}\n"
            f"- 최근 5분 거래대금: {fmt_price(item.get('turnover_5m', 0))}\n"
            f"- 30분 저점대비: {safe_float(item.get('from_low',0),0):+.2f}%\n"
            f"- 최근고점 거리: {format_fast_scan_high_gap_text(safe_float(item.get('below_high',999),999))}\n"
            f"- 한줄평: {one_liner}\n"
            f"- 판단: {' / '.join(item.get('reasons', [])[:3])}"
        ])
        markup = build_fast_scan_alert_markup(ticker, ref_id=ref_id)
        sent_msg = send(msg, reply_markup=markup)
        if sent_msg and getattr(sent_msg, 'message_id', None):
            PRICE_CHECK_REFS[ref_id]['root_message_id'] = sent_msg.message_id
            schedule_fast_scan_auto_check(ref_id, CHAT_ID, target_min=30, now_ts=now_ts)
            persist_price_check_state()
        sent += 1


def _register_fast_scan_items_for_missed_review(items, now_ts: float):
    if not items:
        return
    changed = False
    for item in list(items)[:max(FAST_SCAN_RESULT_MAX, 10)]:
        try:
            bucket = str(item.get('alert_bucket', '') or '')
            block_reason = ''
            if bucket != 'user_alert':
                one_liner = str(item.get('one_liner', '') or '').strip()
                reasons = list(item.get('reasons', [])) if isinstance(item.get('reasons'), list) else []
                block_reason = one_liner or ' / '.join([str(x).strip() for x in reasons[:3] if str(x).strip()])
            key = str(item.get('ticker', '') or '').upper()
            before = dict(missed_review_queue.get(key, {})) if isinstance(missed_review_queue.get(key), dict) else None
            register_missed_review_candidate(
                item,
                now_ts=now_ts,
                seen_market=True,
                seen_shortlist=True,
                seen_signal=True,
                alert_sent=(bucket == 'user_alert' and str(item.get('alert_grade', '')) in {'S', 'A'}),
                alert_section='live' if bucket == 'user_alert' else str(item.get('section', 'watch') or 'watch'),
                quality_block_reason=block_reason,
                persist=False,
            )
            after = missed_review_queue.get(key, {})
            if before != after:
                changed = True
        except Exception:
            continue
    if changed:
        cleanup_missed_review_queue(now_ts)
        persist_missed_review_state()


def scan_watchlist(shared_cache=None):
    global last_watchlist_market_register_ts
    started_at = time.time()
    mark_main_loop_heartbeat(stage='scan_watchlist_fast')
    cache = shared_cache if isinstance(shared_cache, dict) and shared_cache else (shared_market_cache if isinstance(shared_market_cache, dict) and shared_market_cache else None)
    if not cache:
        request_market_refresh(force=True, reason='empty_cache')
        _save_fast_scan_state([], note='최근 시장 데이터가 아직 없음', stage='scan_watchlist_empty', cache_age=None)
        mark_main_loop_heartbeat(stage='scan_watchlist_empty_done')
        return
    cache_age = int(started_at - shared_market_cache_time) if shared_market_cache_time else None
    now_ts = time.time()
    if (now_ts - safe_float(last_watchlist_market_register_ts, 0)) >= SCAN_WATCHLIST_MARKET_REGISTER_INTERVAL_SEC:
        try:
            market_cache = get_watchlist_subset_cache(cache, limit=SCAN_WATCHLIST_MARKET_CACHE_LIMIT) or cache
            register_missed_review_market_candidates(market_cache, now_ts=now_ts, limit=SCAN_WATCHLIST_PREPARE_MISSED_LIMIT)
            last_watchlist_market_register_ts = now_ts
        except Exception as e:
            print(f'[fast_scan_missed_market 오류] {e}', flush=True)
    items = build_fast_scan_candidates_from_cache(cache, limit=FAST_SCAN_RESULT_MAX)
    try:
        _register_fast_scan_items_for_missed_review(items, now_ts)
    except Exception as e:
        print(f'[fast_scan_missed_shortlist 오류] {e}', flush=True)
    _save_fast_scan_state(items, note='빠른 후보 스캔 기준', stage='scan_watchlist_fast', cache_age=cache_age)
    try:
        _send_fast_scan_alerts(items, started_at)
    except Exception as e:
        print(f'[fast_scan_alert 오류] {e}', flush=True)
    mark_main_loop_heartbeat(stage='scan_watchlist_done')

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
dispatcher.add_handler(CommandHandler("recent5", recent5_command))
dispatcher.add_handler(CommandHandler("recent_candidates", recent5_command))
dispatcher.add_handler(CommandHandler("missed", missed_command))
dispatcher.add_handler(CommandHandler("missed_reset", missed_reset_command))
dispatcher.add_handler(CallbackQueryHandler(price_check_callback, pattern=r"^pc:"))
load_runtime_settings()
force_autobuy_off_on_startup()
load_positions()
load_pending_sells()
load_pending_buys()
load_price_check_state()
load_market_pulse_state()
load_missed_review_state()
recover_positions_from_exchange()
cleanup_pending_buy_candidates()
save_positions()
save_pending_sells()
save_pending_buys()
persist_price_check_state()
persist_market_pulse_state()
persist_missed_review_state()
reset_auto_pause_state(bypass_sec=900, reason="restart")
mark_main_loop_heartbeat(stage="startup_ready")
threading.Thread(target=scan_watchdog_loop, daemon=True).start()
request_market_refresh(force=True, reason="startup")
updater.start_polling(drop_pending_updates=True)
send_startup_message()
print(f"🚀 {BOT_VERSION} 실행 / {TIMEZONE}")
last_scan_cycle_time = 0
last_position_cycle_time = 0
last_market_refresh_time = 0
while True:
    now_ts = time.time()
    try:
        mark_main_loop_heartbeat(stage="loop_idle")
        update_active_price_check_refs(now_ts)
        process_scheduled_price_checks(now_ts)
        need_scan_cycle = (now_ts - last_scan_cycle_time) >= SCAN_INTERVAL
        need_position_cycle = (now_ts - last_position_cycle_time) >= POSITION_CHECK_INTERVAL
        cache_empty = not bool(shared_market_cache)
        market_refresh_gap = MARKET_REFRESH_EMPTY_RETRY_SEC if cache_empty else MARKET_REFRESH_INTERVAL_SEC
        need_market_refresh = (need_scan_cycle or need_position_cycle or cache_empty) and ((now_ts - last_market_refresh_time) >= market_refresh_gap)
        shared_cache = shared_market_cache if isinstance(shared_market_cache, dict) else None
        if need_market_refresh:
            requested = request_market_refresh(force=cache_empty, reason=("empty_cache" if cache_empty else "loop_refresh"))
            if requested:
                try:
                    update_scan_debug_snapshot(shared_cache or {}, note="시장 갱신은 백그라운드에서 진행 / 후보 스캔은 기존 캐시로 계속 진행")
                except Exception:
                    pass
            last_market_refresh_time = now_ts
        if need_scan_cycle:
            if shared_cache is None and isinstance(shared_market_cache, dict) and shared_market_cache:
                shared_cache = shared_market_cache
            mark_main_loop_heartbeat(stage="scan_watchlist")
            try:
                scan_watchlist(shared_cache=shared_cache)
            except Exception as e:
                print(f"[loop] scan_watchlist error: {e}", flush=True)
                last_main_loop_error = str(e)
            mark_main_loop_heartbeat(stage="missed_review")
            try:
                process_missed_review_queue(now_ts)
            except Exception as e:
                print(f"[loop] missed review error: {e}", flush=True)
                last_main_loop_error = str(e)
            try:
                process_missed_review_hourly_report(now_ts)
            except Exception as e:
                print(f"[loop] missed review hourly error: {e}", flush=True)
                last_main_loop_error = str(e)
            mark_main_loop_heartbeat(stage="pending_promote")
            try:
                process_pending_buy_promotions(shared_cache=shared_cache)
            except Exception as e:
                print(f"[loop] pending promotion error: {e}", flush=True)
                last_main_loop_error = str(e)
            mark_main_loop_heartbeat(stage="auto_trade")
            try:
                scan_and_auto_trade(shared_cache=shared_cache)
            except Exception as e:
                print(f"[loop] auto trade error: {e}", flush=True)
                last_main_loop_error = str(e)
            last_scan_cycle_time = now_ts
            mark_main_loop_heartbeat(stage="scan_cycle_done")
        if need_position_cycle:
            mark_main_loop_heartbeat(stage="position_check")
            check_pending_sells()
            cleanup_pending_buy_candidates()
            monitor_positions()
            last_position_cycle_time = now_ts
        if now_ts - last_btc_report_time >= BTC_REPORT_INTERVAL:
            mark_main_loop_heartbeat(stage="btc_report")
            try:
                send(analyze_btc_flow())
            except Exception as e:
                print(f"[BTC 리포트 오류] {e}")
                last_main_loop_error = str(e)
            last_btc_report_time = now_ts
        if now_ts - last_status_report_time >= STATUS_REPORT_INTERVAL:
            mark_main_loop_heartbeat(stage="status_report")
            try:
                if active_positions or pending_sells or pending_buy_candidates:
                    status_command(None, None)
            except Exception as e:
                print(f"[상태 리포트 오류] {e}")
                last_main_loop_error = str(e)
            last_status_report_time = now_ts
        flush_position_state(force=False)
        mark_main_loop_heartbeat(stage="loop_sleep")
    except Exception as e:
        print(f"[메인 루프 오류] {e}")
        traceback.print_exc()
        last_main_loop_error = str(e)
        mark_main_loop_heartbeat(stage="loop_exception", error=str(e))
    time.sleep(LOOP_SLEEP)
