
# -*- coding: utf-8 -*-
"""
수익형 v2.7.4

목표:
- v2.6.x 레거시/브릿지/복구 출력 찌꺼기를 기본 실행에서 제거
- 지금 쓰는 실사용 경로만 새 뼈대로 재구성
- 자동매매 실주문은 보호모드로 막고, 후보판/상태판 먼저 검증

주의:
- 이 파일은 v2.6.x의 거대한 함수 덩어리를 그대로 복사하지 않는다.
- 시장자료 -> 중앙허브 -> 후보분류 -> 후보판/상태판까지만 가볍게 운영한다.
- 실거래 주문은 v2.7.4 이후 재연결 전까지 실행하지 않는다.
"""

import os
import sys
import json
import time
import math
import traceback
import threading
import subprocess
import urllib.request
from dataclasses import dataclass, field
from collections import defaultdict, deque
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

try:
    from telegram import Bot, BotCommand
    from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
except Exception as e:  # pragma: no cover
    raise RuntimeError(f"python-telegram-bot import 실패: {e}")

# =========================================================
# 기본 설정
# =========================================================
BOT_VERSION = "수익형 v2.7.4"
BOT_FILE_TAG = "수익형_v2.7.4.py"
TIMEZONE = "Asia/Seoul"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("환경변수 TELEGRAM_TOKEN / CHAT_ID가 필요해요.")

bot = Bot(token=TELEGRAM_TOKEN)

# 실거래 주문은 clean core 1차에서 강제 보호모드
REAL_ORDER_ENABLED = False
MAX_BUY_KRW = int(os.getenv("MAX_BUY_KRW", "10000"))
MAX_POSITIONS = 1

# 가벼운 주기
MARKET_REFRESH_SEC = float(os.getenv("V270_MARKET_REFRESH_SEC", "5"))
STATUS_SNAPSHOT_SAVE_SEC = 60
HISTORY_KEEP_SEC = 45 * 60
MAX_HISTORY_POINTS_PER_TICKER = 720
MAX_RECENT_EVENTS = 160
MAX_CANDIDATES = 80

# API timeout은 짧게. 서버가 작아서 오래 붙잡지 않는다.
HTTP_TIMEOUT_SEC = 3.0
SUBPROCESS_TIMEOUT_FAST = 4.0
SUBPROCESS_TIMEOUT_DEPLOY = 120.0

# =========================================================
# WebSocket / 데이터 허브 1차
# =========================================================
# 공식 Public WebSocket은 ticker/trade/orderbook을 제공한다. v2.7.4는 현재가+체결까지만 붙인다.
WS_HUB_ON = os.getenv("V274_WS_HUB_ON", "1") not in ("0", "false", "False")
WS_URL = os.getenv("V274_WS_URL", "wss://ws-api.bithumb.com/websocket/v1")
WS_LEGACY_URL = os.getenv("V274_WS_LEGACY_URL", "wss://pubwss.bithumb.com/pub/ws")
WS_MAX_CODES = int(os.getenv("V274_WS_MAX_CODES", "80"))
WS_RECV_TIMEOUT_SEC = float(os.getenv("V274_WS_RECV_TIMEOUT_SEC", "1.0"))
WS_RECONNECT_SEC = float(os.getenv("V274_WS_RECONNECT_SEC", "5.0"))
WS_SUBSCRIBE_REFRESH_SEC = float(os.getenv("V274_WS_SUB_REFRESH_SEC", "600"))
WS_NO_MESSAGE_SWITCH_SEC = float(os.getenv("V274_WS_NO_MESSAGE_SWITCH_SEC", "25"))
WS_PRICE_FRESH_SEC = float(os.getenv("V274_WS_PRICE_FRESH_SEC", "8"))
WS_TRADE_FRESH_SEC = float(os.getenv("V274_WS_TRADE_FRESH_SEC", "20"))
WS_CANDLE_FRESH_SEC = float(os.getenv("V274_WS_CANDLE_FRESH_SEC", "12"))
WS_TRADE_ROWS_PER_TICKER = int(os.getenv("V274_WS_TRADE_ROWS", "240"))
WS_CLOSED_CANDLES_PER_TICKER = int(os.getenv("V274_WS_CLOSED_CANDLES", "8"))


# 파일
DEPLOY_TARGET_FILE = os.path.join(BASE_DIR, "DEPLOY_TARGET.txt")
DEPLOYED_TARGET_FILE = os.path.join(BASE_DIR, ".deployed_target")
DEPLOY_STATUS_LOG = os.path.join(BASE_DIR, "deploy_status.log")
UPDATE_TRIGGER_LOG = os.path.join(BASE_DIR, "update_bot_trigger.log")
RUNTIME_SETTINGS_FILE = os.path.join(BASE_DIR, "v270_runtime_settings.json")
STATUS_STATE_FILE = os.path.join(BASE_DIR, "v270_status_state.json")
RUNTIME_ERROR_LOG_FILE = os.path.join(BASE_DIR, "runtime_error_log.txt")

# =========================================================
# 유틸
# =========================================================
def now_ts() -> float:
    return time.time()


def kst_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(TIMEZONE))
    return datetime.now()


def fmt_time(ts: float | None) -> str:
    if not ts:
        return "정보없음"
    try:
        return datetime.fromtimestamp(ts, ZoneInfo(TIMEZONE)).strftime("%m-%d %H:%M:%S") if ZoneInfo else datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
    except Exception:
        return "정보없음"


def age_text(ts: float | None) -> str:
    if not ts:
        return "정보없음"
    sec = max(0, int(now_ts() - ts))
    if sec < 60:
        return f"{sec}초 전"
    if sec < 3600:
        return f"{sec//60}분 전"
    return f"{sec//3600}시간 {sec%3600//60}분 전"


def safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if v in ("", "-", "None"):
                return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def pct(cur: float, old: float) -> float:
    if not cur or not old or old <= 0:
        return 0.0
    return (cur - old) / old * 100.0


def krw(n: float) -> str:
    try:
        n = float(n)
    except Exception:
        return "0원"
    if n >= 100_000_000:
        return f"{n/100_000_000:.2f}억"
    if n >= 10_000:
        return f"{n/10_000:.0f}만"
    return f"{n:.0f}원"


def price_text(p: float) -> str:
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 100:
        return f"{p:,.1f}"
    if p >= 10:
        return f"{p:,.2f}"
    return f"{p:,.4g}"


def atomic_write(path: str, text: str):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return default


def tail_text(path: str, max_lines: int = 80, max_bytes: int = 60_000) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read().decode("utf-8", errors="replace")
        return "\n".join(data.splitlines()[-max_lines:])
    except Exception:
        return ""


def log_error(where: str, err: Exception | str):
    msg = f"[{kst_now().strftime('%Y-%m-%d %H:%M:%S')}] [{where}] {err}\n"
    try:
        with open(RUNTIME_ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    with STATE.lock:
        STATE.errors.appendleft(msg.strip())


def run_cmd(cmd: list[str], timeout: float = SUBPROCESS_TIMEOUT_FAST) -> tuple[bool, str, float]:
    start = now_ts()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode == 0, out.strip(), now_ts() - start
    except subprocess.TimeoutExpired:
        return False, f"timeout {timeout:.0f}s: {' '.join(cmd)}", now_ts() - start
    except Exception as e:
        return False, f"실행실패: {e}", now_ts() - start


# =========================================================
# 중앙 상태
# =========================================================
@dataclass
class MarketRow:
    ticker: str
    price: float
    money24h: float = 0.0
    ch1: float = 0.0
    ch3: float = 0.0
    ch5: float = 0.0
    ch15: float = 0.0
    high30: float = 0.0
    low30: float = 0.0
    from_low30: float = 0.0
    high_gap30: float = 999.0
    hist_span_sec: float = 0.0
    hist_points: int = 0
    ch1_ready: bool = False
    ch3_ready: bool = False
    ch5_ready: bool = False
    range30_pct: float = 0.0
    money1m: float = 0.0
    money3m: float = 0.0
    money5m: float = 0.0
    money1_ready: bool = False
    money3_ready: bool = False
    money5_ready: bool = False
    money_sustain_ok: bool = False
    money_flash_risk: bool = False
    tick_noise: bool = False
    pullback_shape_ok: bool = False
    pullback_drop_pct: float = 0.0
    pullback_rebound_pct: float = 0.0
    low_defense_ok: bool = False
    failed_peak_retest: bool = False
    recent_spike_near_high: bool = False
    box_top_chase: bool = False
    current_milrim: bool = False
    overheat_watch: bool = False
    data_quality: str = "REST_ONLY"
    price_source: str = "REST"
    rest_price: float = 0.0
    ws_price: float = 0.0
    ws_price_age_sec: float = 999999.0
    ws_trade_amount_60: float = 0.0
    ws_trade_count_60: int = 0
    ws_buy_ratio_60: float = 0.0
    own1m_ok: bool = False
    own1m_state: str = "정보없음"
    own1m_body_pct: float = 0.0
    own1m_upper_wick_pct: float = 0.0
    own1m_lower_wick_pct: float = 0.0
    own1m_turnover: float = 0.0
    score: float = 0.0
    updated_ts: float = 0.0


@dataclass
class Candidate:
    ticker: str
    status: str  # BUY_READY / PENDING / WATCH / REJECT
    label: str
    icon: str
    reason: str
    price: float
    score: float
    ch1: float
    ch3: float
    ch5: float
    money24h: float
    from_low30: float
    high_gap30: float
    stop_price: float = 0.0
    entry_note: str = ""
    reject_reason: str = ""
    updated_ts: float = 0.0
    pattern: str = ""
    caution: str = ""
    ch1_ready: bool = True
    ch3_ready: bool = True
    ch5_ready: bool = True
    range30_pct: float = 0.0
    money1m: float = 0.0
    money3m: float = 0.0
    money5m: float = 0.0
    money_sustain_ok: bool = False
    money_flash_risk: bool = False
    tick_noise: bool = False
    pullback_shape_ok: bool = False
    pullback_drop_pct: float = 0.0
    pullback_rebound_pct: float = 0.0
    low_defense_ok: bool = False
    failed_peak_retest: bool = False
    recent_spike_near_high: bool = False
    box_top_chase: bool = False
    current_milrim: bool = False
    overheat_watch: bool = False
    data_quality: str = "REST_ONLY"
    price_source: str = "REST"
    rest_price: float = 0.0
    ws_price: float = 0.0
    ws_price_age_sec: float = 999999.0
    ws_trade_amount_60: float = 0.0
    ws_trade_count_60: int = 0
    ws_buy_ratio_60: float = 0.0
    own1m_ok: bool = False
    own1m_state: str = "정보없음"
    own1m_body_pct: float = 0.0
    own1m_upper_wick_pct: float = 0.0
    own1m_lower_wick_pct: float = 0.0
    own1m_turnover: float = 0.0


@dataclass
class WsPriceRow:
    ticker: str
    price: float
    turnover_hint: float = 0.0
    ts: float = 0.0
    source: str = "ws"


@dataclass
class WsTradeTick:
    ticker: str
    price: float
    volume: float
    amount: float
    side: str = "unknown"
    ts: float = 0.0


@dataclass
class Candle1mRow:
    ticker: str
    minute_ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    turnover: float = 0.0
    trade_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    updated_ts: float = 0.0


@dataclass
class RuntimeState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    start_ts: float = field(default_factory=now_ts)
    boot_count: int = 0
    stage: str = "starting"
    main_loop_ts: float | None = None
    market_ts: float | None = None
    rank_ts: float | None = None
    candidate_ts: float | None = None
    output_ts: float | None = None
    market_rows: dict[str, MarketRow] = field(default_factory=dict)
    rank_rows: list[MarketRow] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    ws_price_hub: dict[str, WsPriceRow] = field(default_factory=dict)
    ws_trade_hub: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=WS_TRADE_ROWS_PER_TICKER)))
    candle_1m_hub: dict[str, Candle1mRow] = field(default_factory=dict)
    candle_1m_closed: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=WS_CLOSED_CANDLES_PER_TICKER)))
    ws_state: dict = field(default_factory=lambda: {
        "price_status": "대기", "trade_status": "대기",
        "price_connected": False, "trade_connected": False,
        "price_msg": 0, "trade_msg": 0,
        "price_raw": 0, "trade_raw": 0,
        "price_empty": 0, "trade_empty": 0,
        "price_last_ts": 0.0, "trade_last_ts": 0.0,
        "price_codes": 0, "trade_codes": 0,
        "price_mode": "v2", "trade_mode": "v2",
        "price_error": "", "trade_error": "",
    })
    price_history: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=MAX_HISTORY_POINTS_PER_TICKER)))
    recent_events: deque = field(default_factory=lambda: deque(maxlen=MAX_RECENT_EVENTS))
    errors: deque = field(default_factory=lambda: deque(maxlen=60))
    cmd_timings: deque = field(default_factory=lambda: deque(maxlen=80))
    last_batch_expected: int = 0
    last_batch_recorded: int = 0
    last_batch_total_sec: float = 0.0
    fetch_ok: int = 0
    fetch_fail: int = 0
    last_fetch_error: str = ""
    auto_enabled: bool = False
    real_order_enabled: bool = REAL_ORDER_ENABLED
    admin_check_count: int = 0
    deploy_check_count: int = 0
    update_bot_count: int = 0
    buy_check_count: int = 0
    legacy_touch_count: int = 0
    shutdown: bool = False


STATE = RuntimeState()


def record_event(msg: str):
    with STATE.lock:
        STATE.recent_events.appendleft(f"{kst_now().strftime('%H:%M:%S')} {msg}")


def record_timing(cmd: str, sec: float, ok: bool = True):
    with STATE.lock:
        STATE.cmd_timings.appendleft({"cmd": cmd, "sec": sec, "ok": ok, "ts": now_ts()})


# =========================================================
# 설정 저장
# =========================================================
def load_runtime_settings():
    txt = read_text(RUNTIME_SETTINGS_FILE, "{}")
    try:
        data = json.loads(txt or "{}")
    except Exception:
        data = {}
    with STATE.lock:
        STATE.auto_enabled = bool(data.get("auto_enabled", False))


def save_runtime_settings():
    with STATE.lock:
        data = {"auto_enabled": STATE.auto_enabled, "updated_ts": now_ts(), "version": BOT_VERSION}
    try:
        atomic_write(RUNTIME_SETTINGS_FILE, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log_error("save_runtime_settings", e)



# =========================================================
# WebSocket / 데이터 신뢰도 허브 1차
# =========================================================
def ticker_to_ws_code(ticker: str) -> str:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return ""
    if ticker.startswith("KRW-"):
        return ticker
    if ticker.endswith("_KRW"):
        ticker = ticker.split("_", 1)[0]
    return f"KRW-{ticker}"


def ticker_to_legacy_symbol(ticker: str) -> str:
    ticker = str(ticker or "").upper().strip()
    if ticker.startswith("KRW-"):
        ticker = ticker.split("-", 1)[1]
    if ticker.endswith("_KRW"):
        return ticker
    return f"{ticker}_KRW" if ticker else ""


def ws_code_to_ticker(code: str) -> str:
    code = str(code or "").upper().strip()
    if not code:
        return ""
    if code.startswith("KRW-"):
        return code.split("-", 1)[1]
    if code.endswith("_KRW"):
        return code.split("_", 1)[0]
    return code


def _ws_state_set(**kwargs):
    try:
        with STATE.lock:
            for k, v in kwargs.items():
                STATE.ws_state[k] = v
    except Exception:
        pass


def ws_candidate_tickers(limit: int = WS_MAX_CODES) -> list[str]:
    # 허브 1차: 현재 순위/후보 중심으로만 구독해서 작게 시작한다.
    with STATE.lock:
        cand = [c.ticker for c in STATE.candidates if c.status in ("BUY_READY", "PENDING", "WATCH")]
        rank = [r.ticker for r in STATE.rank_rows]
        market_top = sorted(STATE.market_rows.values(), key=lambda r: r.money24h, reverse=True)
        market = [r.ticker for r in market_top[:limit]]
    out = []
    seen = set()
    for t in cand + rank + market + ["BTC", "ETH", "XRP", "SOL", "PEPE"]:
        t = str(t or "").upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def parse_ws_ticker_payload(raw_msg):
    try:
        if isinstance(raw_msg, (bytes, bytearray)):
            raw_msg = raw_msg.decode("utf-8", errors="ignore")
        msg = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
    except Exception:
        return []
    if isinstance(msg, list):
        out = []
        for one in msg:
            out.extend(parse_ws_ticker_payload(one))
        return out
    if not isinstance(msg, dict):
        return []
    content = msg.get("content") if isinstance(msg.get("content"), dict) else None
    if content:
        typ = str(msg.get("type", "") or "").lower()
        if typ and typ != "ticker":
            return []
        code = content.get("symbol", content.get("code", content.get("cd", "")))
        price = safe_float(content.get("closePrice", content.get("close_price", content.get("trade_price", content.get("tp", 0)))), 0)
        if not code or price <= 0:
            return []
        turnover = safe_float(content.get("value", content.get("acc_trade_price_24h", content.get("accTradeValue", 0))), 0)
        return [{"ticker": ws_code_to_ticker(code), "price": price, "turnover": turnover, "source": "legacy"}]
    typ = str(msg.get("type", msg.get("ty", "")) or "").lower()
    if typ and typ != "ticker":
        return []
    code = msg.get("code", msg.get("market", msg.get("cd", "")))
    price = safe_float(msg.get("trade_price", msg.get("tp", 0)), 0)
    if not code or price <= 0:
        return []
    turnover = safe_float(msg.get("acc_trade_price_24h", msg.get("atp24h", msg.get("acc_trade_price", msg.get("atp", 0)))), 0)
    return [{"ticker": ws_code_to_ticker(code), "price": price, "turnover": turnover, "source": "v2"}]


def normalize_trade_side(v) -> str:
    raw = str(v or "").upper().strip()
    if raw in {"BID", "BUY", "B", "1", "매수"}:
        return "buy"
    if raw in {"ASK", "SELL", "S", "2", "매도"}:
        return "sell"
    return "unknown"


def parse_ws_trade_payload(raw_msg):
    try:
        if isinstance(raw_msg, (bytes, bytearray)):
            raw_msg = raw_msg.decode("utf-8", errors="ignore")
        msg = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
    except Exception:
        return []
    if isinstance(msg, list):
        out = []
        for one in msg:
            out.extend(parse_ws_trade_payload(one))
        return out
    if not isinstance(msg, dict):
        return []

    content = msg.get("content")
    if isinstance(content, dict):
        typ = str(msg.get("type", "") or "").lower().strip()
        if typ and typ not in {"transaction", "trade"}:
            return []
        rows = content.get("list") if isinstance(content.get("list"), list) else [content]
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = row.get("symbol", row.get("code", row.get("cd", "")))
            price = safe_float(row.get("contPrice", row.get("trade_price", row.get("price", row.get("tp", 0)))), 0)
            qty = safe_float(row.get("contQty", row.get("trade_volume", row.get("volume", row.get("tv", 0)))), 0)
            amount = safe_float(row.get("contAmt", row.get("amount", row.get("trade_price", 0))), 0)
            if amount <= 0 and price > 0 and qty > 0:
                amount = price * qty
            side = normalize_trade_side(row.get("buySellGb", row.get("ask_bid", row.get("side", ""))))
            if code and price > 0:
                out.append({"ticker": ws_code_to_ticker(code), "price": price, "volume": qty, "amount": amount, "side": side, "source": "legacy"})
        return out

    typ = str(msg.get("type", msg.get("ty", "")) or "").lower()
    if typ and typ != "trade":
        return []
    code = msg.get("code", msg.get("market", msg.get("cd", "")))
    price = safe_float(msg.get("trade_price", msg.get("tp", 0)), 0)
    qty = safe_float(msg.get("trade_volume", msg.get("tv", msg.get("volume", 0))), 0)
    amount = safe_float(msg.get("trade_price", 0), 0) * qty if price > 0 and qty > 0 else 0.0
    amount = safe_float(msg.get("trade_amount", msg.get("amount", amount)), amount)
    side = normalize_trade_side(msg.get("ask_bid", msg.get("ab", msg.get("side", ""))))
    if code and price > 0:
        return [{"ticker": ws_code_to_ticker(code), "price": price, "volume": qty, "amount": amount, "side": side, "source": "v2"}]
    return []


def update_own_1m_candle_locked(ticker: str, price: float, volume: float = 0.0, amount: float = 0.0, side: str = "unknown", ts: float | None = None):
    ts = ts or now_ts()
    if not ticker or price <= 0:
        return
    minute_ts = int(ts // 60) * 60
    cur = STATE.candle_1m_hub.get(ticker)
    if not isinstance(cur, Candle1mRow) or cur.minute_ts != minute_ts:
        if isinstance(cur, Candle1mRow):
            STATE.candle_1m_closed[ticker].append(cur)
        cur = Candle1mRow(ticker=ticker, minute_ts=minute_ts, open=price, high=price, low=price, close=price, updated_ts=ts)
        STATE.candle_1m_hub[ticker] = cur
    cur.high = max(cur.high, price)
    cur.low = min(cur.low, price)
    cur.close = price
    cur.volume += max(0.0, float(volume or 0.0))
    cur.turnover += max(0.0, float(amount or 0.0))
    if amount or volume:
        cur.trade_count += 1
        if side == "buy":
            cur.buy_count += 1
        elif side == "sell":
            cur.sell_count += 1
    cur.updated_ts = ts


def apply_ws_price_update(ticker: str, price: float, turnover_hint: float = 0.0, source: str = "ws"):
    ticker = str(ticker or "").upper().strip()
    price = safe_float(price, 0)
    if not ticker or price <= 0:
        return
    ts = now_ts()
    with STATE.lock:
        STATE.ws_price_hub[ticker] = WsPriceRow(ticker=ticker, price=price, turnover_hint=safe_float(turnover_hint, 0), ts=ts, source=source)
        update_own_1m_candle_locked(ticker, price, ts=ts)
        st = STATE.ws_state
        st["price_msg"] = int(st.get("price_msg", 0)) + 1
        st["price_last_ts"] = ts
        st["price_status"] = "수신중"


def apply_ws_trade_update(ticker: str, price: float, volume: float = 0.0, amount: float = 0.0, side: str = "unknown", source: str = "ws"):
    ticker = str(ticker or "").upper().strip()
    price = safe_float(price, 0)
    if not ticker or price <= 0:
        return
    ts = now_ts()
    amount = safe_float(amount, 0)
    volume = safe_float(volume, 0)
    if amount <= 0 and volume > 0:
        amount = price * volume
    side = normalize_trade_side(side)
    with STATE.lock:
        STATE.ws_trade_hub[ticker].append(WsTradeTick(ticker=ticker, price=price, volume=volume, amount=amount, side=side, ts=ts))
        STATE.ws_price_hub[ticker] = WsPriceRow(ticker=ticker, price=price, turnover_hint=0.0, ts=ts, source=source)
        update_own_1m_candle_locked(ticker, price, volume=volume, amount=amount, side=side, ts=ts)
        st = STATE.ws_state
        st["trade_msg"] = int(st.get("trade_msg", 0)) + 1
        st["trade_last_ts"] = ts
        st["trade_status"] = "수신중"


def get_trade_summary_locked(ticker: str, window_sec: int = 60) -> dict:
    ts = now_ts()
    rows = list(STATE.ws_trade_hub.get(ticker, []))
    recent = [r for r in rows if ts - r.ts <= window_sec]
    amount = sum(max(0.0, r.amount) for r in recent)
    count = len(recent)
    buy = len([r for r in recent if r.side == "buy"])
    sell = len([r for r in recent if r.side == "sell"])
    buy_ratio = (buy / (buy + sell) * 100.0) if (buy + sell) > 0 else 0.0
    return {"amount": amount, "count": count, "buy": buy, "sell": sell, "buy_ratio": buy_ratio}


def candle_state_from_row(c: Candle1mRow | None) -> dict:
    if not isinstance(c, Candle1mRow) or c.open <= 0 or c.high <= 0 or c.low <= 0:
        return {"ok": False, "state": "정보없음", "body_pct": 0.0, "upper_pct": 0.0, "lower_pct": 0.0, "turnover": 0.0, "age": 999999.0}
    age = now_ts() - c.updated_ts if c.updated_ts else 999999.0
    body_pct = (c.close - c.open) / c.open * 100.0
    upper = max(0.0, c.high - max(c.open, c.close)) / c.open * 100.0
    lower = max(0.0, min(c.open, c.close) - c.low) / c.open * 100.0
    if body_pct >= 0.10 and upper <= max(0.35, abs(body_pct) * 1.5):
        state = "회복"
    elif body_pct <= -0.10:
        state = "밀림"
    elif upper >= 0.35 and upper > lower * 1.5:
        state = "윗꼬리"
    else:
        state = "보합"
    return {"ok": age <= WS_CANDLE_FRESH_SEC, "state": state, "body_pct": body_pct, "upper_pct": upper, "lower_pct": lower, "turnover": c.turnover, "age": age}


def get_realtime_quality_snapshot(ticker: str, rest_price: float) -> dict:
    ticker = str(ticker or "").upper().strip()
    rest_price = safe_float(rest_price, 0)
    with STATE.lock:
        ws = STATE.ws_price_hub.get(ticker)
        c = STATE.candle_1m_hub.get(ticker)
        trade = get_trade_summary_locked(ticker, 60)
    ts = now_ts()
    ws_price = ws.price if isinstance(ws, WsPriceRow) else 0.0
    ws_age = ts - ws.ts if isinstance(ws, WsPriceRow) and ws.ts else 999999.0
    ws_fresh = ws_price > 0 and ws_age <= WS_PRICE_FRESH_SEC
    candle = candle_state_from_row(c)
    trade_fresh = trade["count"] > 0 and trade["amount"] > 0
    price = ws_price if ws_fresh else rest_price
    source = "WebSocket" if ws_fresh else "REST"
    rest_diff = abs(pct(ws_price, rest_price)) if ws_fresh and rest_price > 0 else 0.0
    if ws_fresh and candle["ok"] and trade_fresh:
        quality = "WS_OK"
    elif ws_fresh and candle["ok"]:
        quality = "WS_PRICE_OK"
    elif ws_fresh:
        quality = "WS_PRICE_ONLY"
    else:
        quality = "REST_ONLY"
    if ws_fresh and rest_diff >= 1.0:
        quality = "PRICE_MISMATCH"
    return {
        "price": price,
        "price_source": source,
        "rest_price": rest_price,
        "ws_price": ws_price,
        "ws_price_age_sec": ws_age,
        "data_quality": quality,
        "ws_trade_amount_60": trade["amount"],
        "ws_trade_count_60": trade["count"],
        "ws_buy_ratio_60": trade["buy_ratio"],
        "own1m_ok": bool(candle["ok"]),
        "own1m_state": candle["state"],
        "own1m_body_pct": candle["body_pct"],
        "own1m_upper_wick_pct": candle["upper_pct"],
        "own1m_lower_wick_pct": candle["lower_pct"],
        "own1m_turnover": candle["turnover"],
        "rest_diff_pct": rest_diff,
    }


def websocket_worker(kind: str):
    if not WS_HUB_ON:
        _ws_state_set(**{f"{kind}_status": "꺼짐"})
        return
    mode_index = 0
    modes = ["v2", "legacy"]
    while not STATE.shutdown:
        ws = None
        mode = modes[mode_index % len(modes)]
        try:
            tickers = ws_candidate_tickers(WS_MAX_CODES)
            if not tickers:
                _ws_state_set(**{f"{kind}_status": "구독대기", f"{kind}_connected": False, f"{kind}_mode": mode})
                time.sleep(3)
                continue
            try:
                import websocket  # websocket-client
            except Exception as e:
                _ws_state_set(**{f"{kind}_status": "라이브러리없음", f"{kind}_connected": False, f"{kind}_error": str(e)[:120]})
                time.sleep(30)
                continue
            url = WS_LEGACY_URL if mode == "legacy" else WS_URL
            _ws_state_set(**{f"{kind}_status": "연결중", f"{kind}_connected": False, f"{kind}_codes": len(tickers), f"{kind}_mode": mode, f"{kind}_error": ""})
            ws = websocket.create_connection(url, timeout=5, enable_multithread=True)
            ws.settimeout(WS_RECV_TIMEOUT_SEC)
            if mode == "legacy":
                symbols = [ticker_to_legacy_symbol(t) for t in tickers]
                req = {"type": "ticker" if kind == "price" else "transaction", "symbols": symbols}
            else:
                codes = [ticker_to_ws_code(t) for t in tickers]
                req = [{"ticket": f"coinbot-{kind}-{int(time.time())}"}, {"type": "ticker" if kind == "price" else "trade", "codes": codes}, {"format": "DEFAULT"}]
            ws.send(json.dumps(req))
            opened = now_ts()
            _ws_state_set(**{f"{kind}_status": "수신중", f"{kind}_connected": True, f"{kind}_codes": len(tickers), f"{kind}_mode": mode})
            got = False
            while not STATE.shutdown:
                if now_ts() - opened >= WS_SUBSCRIBE_REFRESH_SEC:
                    break
                if not got and now_ts() - opened >= WS_NO_MESSAGE_SWITCH_SEC:
                    _ws_state_set(**{f"{kind}_status": "무수신전환", f"{kind}_connected": False})
                    break
                try:
                    raw = ws.recv()
                except Exception as e:
                    if "timeout" in e.__class__.__name__.lower():
                        continue
                    raise
                if not raw:
                    continue
                with STATE.lock:
                    STATE.ws_state[f"{kind}_raw"] = int(STATE.ws_state.get(f"{kind}_raw", 0)) + 1
                parsed = parse_ws_ticker_payload(raw) if kind == "price" else parse_ws_trade_payload(raw)
                if not parsed:
                    with STATE.lock:
                        STATE.ws_state[f"{kind}_empty"] = int(STATE.ws_state.get(f"{kind}_empty", 0)) + 1
                    continue
                got = True
                for item in parsed:
                    if kind == "price":
                        apply_ws_price_update(item.get("ticker"), item.get("price"), item.get("turnover"), item.get("source", mode))
                    else:
                        apply_ws_trade_update(item.get("ticker"), item.get("price"), item.get("volume"), item.get("amount"), item.get("side"), item.get("source", mode))
        except Exception as e:
            _ws_state_set(**{f"{kind}_status": "재연결대기", f"{kind}_connected": False, f"{kind}_error": f"{e.__class__.__name__}: {str(e)[:90]}"})
            log_error(f"ws_{kind}_worker", e)
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
            _ws_state_set(**{f"{kind}_connected": False})
        mode_index += 1
        time.sleep(WS_RECONNECT_SEC)


def ws_hub_snapshot() -> dict:
    now = now_ts()
    with STATE.lock:
        st = dict(STATE.ws_state)
        price_live = len([r for r in STATE.ws_price_hub.values() if isinstance(r, WsPriceRow) and now - r.ts <= WS_PRICE_FRESH_SEC])
        price_total = len(STATE.ws_price_hub)
        trade_live = len([t for t, rows in STATE.ws_trade_hub.items() if rows and now - rows[-1].ts <= WS_TRADE_FRESH_SEC])
        trade_total = len(STATE.ws_trade_hub)
        candle_live = len([c for c in STATE.candle_1m_hub.values() if isinstance(c, Candle1mRow) and now - c.updated_ts <= WS_CANDLE_FRESH_SEC])
        candle_total = len(STATE.candle_1m_hub)
    st.update({"price_live": price_live, "price_total": price_total, "trade_live": trade_live, "trade_total": trade_total, "candle_live": candle_live, "candle_total": candle_total})
    return st


def render_data_check() -> str:
    st = ws_hub_snapshot()
    price_age = age_text(st.get("price_last_ts")) if st.get("price_last_ts") else "수신 전"
    trade_age = age_text(st.get("trade_last_ts")) if st.get("trade_last_ts") else "수신 전"
    with STATE.lock:
        sample = []
        for c in STATE.candidates[:8]:
            q = get_realtime_quality_snapshot(c.ticker, c.rest_price or c.price)
            sample.append((c.ticker, q))
    lines = [
        "🧱 데이터 허브 /data_check v2.7.4",
        "- 목적: 차트 비교 전에 봇이 보는 가격·체결·자체1분봉이 믿을 만한지 확인",
        "",
        "1) WebSocket 허브",
        f"- 현재가WS: {st.get('price_status','대기')} / 연결 {st.get('price_connected',False)} / 모드 {st.get('price_mode','-')} / 구독 {st.get('price_codes',0)} / 활성 {st.get('price_live',0)}/{st.get('price_total',0)} / 최근 {price_age} / 수신 {st.get('price_msg',0)}건 / raw {st.get('price_raw',0)} / 미파싱 {st.get('price_empty',0)}",
        f"- 체결WS: {st.get('trade_status','대기')} / 연결 {st.get('trade_connected',False)} / 모드 {st.get('trade_mode','-')} / 구독 {st.get('trade_codes',0)} / 활성 {st.get('trade_live',0)}/{st.get('trade_total',0)} / 최근 {trade_age} / 체결 {st.get('trade_msg',0)}건 / raw {st.get('trade_raw',0)} / 미파싱 {st.get('trade_empty',0)}",
        f"- 자체1분봉: 활성 {st.get('candle_live',0)}/{st.get('candle_total',0)}",
    ]
    if st.get("price_error"):
        lines.append(f"- 현재가WS 오류: {st.get('price_error')}")
    if st.get("trade_error"):
        lines.append(f"- 체결WS 오류: {st.get('trade_error')}")
    lines += ["", "2) 후보 샘플 데이터 신뢰도"]
    if not sample:
        lines.append("- 후보 없음")
    for t, q in sample:
        lines.append(f"- {t}: {q['data_quality']} / 소스 {q['price_source']} / WS가격 {price_text(q['ws_price']) if q['ws_price'] else '-'} / WS나이 {int(q['ws_price_age_sec']) if q['ws_price_age_sec'] < 9999 else '없음'}초 / 체결60초 {q['ws_trade_count_60']}건·{krw(q['ws_trade_amount_60'])} / 자체봉 {q['own1m_state']}")
    lines += ["", "3) 원칙", "- WS_OK/WS_PRICE_OK 전에는 🟢 진입가능을 보수적으로 대기 처리", "- REST_ONLY 후보는 차트 비교용 참고값이지 실거래 판단 근거로 쓰지 않음"]
    return "\n".join(lines)

# =========================================================
# 시장자료 수집: Bithumb public ticker ALL_KRW 한 번만 사용
# =========================================================
def fetch_bithumb_all_ticker() -> dict[str, dict]:
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    req = urllib.request.Request(url, headers={"User-Agent": "tradingbot-v270-clean-core"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if str(data.get("status")) != "0000":
        raise RuntimeError(f"bithumb status={data.get('status')}")
    payload = data.get("data") or {}
    rows = {}
    for ticker, item in payload.items():
        if ticker == "date" or not isinstance(item, dict):
            continue
        price = safe_float(item.get("closing_price") or item.get("prev_closing_price"))
        if price <= 0:
            continue
        money = safe_float(item.get("acc_trade_value_24H") or item.get("acc_trade_value"))
        rows[ticker.upper()] = {"price": price, "money24h": money}
    return rows


def nearest_old_price(hist: deque, target_age_sec: int, tolerance_sec: int | None = None) -> float:
    """
    목표 시간보다 과거인 샘플만 사용한다.
    v2.7.0처럼 현재값에 가까운 샘플을 억지로 재사용하면 1분/3분/5분이 같은 값으로 보이는 문제가 생긴다.
    자료가 아직 충분히 쌓이지 않았으면 0을 반환해서 후보를 무리하게 초록으로 올리지 않는다.
    """
    if not hist:
        return 0.0
    target = now_ts() - target_age_sec
    best = None
    best_ts = None
    for ts, price, _money in reversed(hist):
        if ts <= target and price > 0:
            best = price
            best_ts = ts
            break
    if best is None:
        return 0.0
    if tolerance_sec is None:
        tolerance_sec = max(45, int(target_age_sec * 0.45))
    if abs(best_ts - target) > tolerance_sec:
        return 0.0
    return float(best)


def high_low_in_window(hist: deque, window_sec: int) -> tuple[float, float]:
    if not hist:
        return 0.0, 0.0
    cutoff = now_ts() - window_sec
    vals = [p for ts, p, _m in hist if ts >= cutoff and p > 0]
    if not vals:
        vals = [hist[-1][1]]
    return max(vals), min(vals)


def window_samples(hist: deque, window_sec: int):
    cutoff = now_ts() - window_sec
    return [(ts, p, m) for ts, p, m in hist if ts >= cutoff and p > 0]


def nearest_old_sample(hist: deque, target_age_sec: int, tolerance_sec: int | None = None):
    if not hist:
        return None
    target = now_ts() - target_age_sec
    best = None
    best_ts = None
    for ts, price, money in reversed(hist):
        if ts <= target and price > 0:
            best = (float(price), float(money or 0.0))
            best_ts = ts
            break
    if best is None:
        return None
    if tolerance_sec is None:
        tolerance_sec = max(45, int(target_age_sec * 0.45))
    if abs(best_ts - target) > tolerance_sec:
        return None
    return best


def money_delta_from(hist: deque, current_money: float, age_sec: int) -> tuple[float, bool]:
    sample = nearest_old_sample(hist, age_sec)
    if not sample:
        return 0.0, False
    _old_price, old_money = sample
    delta = float(current_money or 0.0) - float(old_money or 0.0)
    if delta < 0:
        # 24H 누적값이 거래소 갱신/리셋으로 줄어들 수 있으므로, 이 경우는 정보없음으로 둔다.
        return 0.0, False
    return delta, True


def unique_price_count(hist: deque, window_sec: int) -> int:
    vals = [p for _ts, p, _m in window_samples(hist, window_sec)]
    if not vals:
        return 0
    rounded = []
    for p in vals:
        # 초저가 코인은 너무 세밀한 소수점 때문에 같은 틱도 다르게 보일 수 있어 적당히 반올림한다.
        if p < 0.01:
            rounded.append(round(p, 8))
        elif p < 1:
            rounded.append(round(p, 6))
        elif p < 100:
            rounded.append(round(p, 4))
        else:
            rounded.append(round(p, 2))
    return len(set(rounded))


def detect_tick_noise(price: float, hist: deque, range30_pct: float) -> bool:
    # PEPE류 초저가 코인은 한두 틱 움직임이 퍼센트로 크게 보인다.
    # clean core에서는 이런 후보를 바로 초록으로 올리지 않는다.
    if price <= 0:
        return True
    if price < 0.01:
        return True
    if price < 0.1:
        uniq = unique_price_count(hist, 900)
        return uniq <= 5 and range30_pct >= 1.2
    if price < 1.0:
        uniq = unique_price_count(hist, 900)
        return uniq <= 4 and range30_pct >= 5.0
    return False


def analyze_shape_from_history(hist: deque, current_price: float) -> dict:
    """
    v2.6 눌림 핵심을 캔들 df 없이 최대한 가볍게 흉내낸다.
    현재 clean core는 5초 단위 현재가 샘플만 있으므로,
    정밀 캔들 판단이 아니라 '상승→눌림→재반등' 여부를 가볍게 본다.
    """
    out = {
        "pullback_shape_ok": False,
        "pullback_drop_pct": 0.0,
        "pullback_rebound_pct": 0.0,
        "low_defense_ok": False,
        "failed_peak_retest": False,
        "recent_spike_near_high": False,
        "box_top_chase": False,
        "current_milrim": False,
    }
    samples = window_samples(hist, 1800)
    if len(samples) < 24 or current_price <= 0:
        return out
    prices = [p for _ts, p, _m in samples]
    n = len(prices)
    # 최근 3개 샘플은 현재 고점 갱신 때문에 pullback low 탐색을 왜곡할 수 있어 제외한다.
    search_prices = prices[:-3] if n > 8 else prices
    if len(search_prices) < 12:
        return out

    # 1) 과거 고점 이후 눌림 저점, 이후 재반등 여부
    peak_idx = max(range(len(search_prices)), key=lambda i: search_prices[i])
    if peak_idx < len(search_prices) - 4:
        after = search_prices[peak_idx + 1:]
        if after:
            low_rel = min(range(len(after)), key=lambda i: after[i])
            low_idx = peak_idx + 1 + low_rel
            peak = search_prices[peak_idx]
            low = search_prices[low_idx]
            if peak > 0 and low > 0 and peak > low:
                drop_pct = (peak - low) / peak * 100.0
                rebound_pct = (current_price - low) / low * 100.0
                out["pullback_drop_pct"] = drop_pct
                out["pullback_rebound_pct"] = rebound_pct
                # 0.35~3.0% 정도의 눌림 뒤 0.20% 이상 회복을 눌림 구조로 본다.
                if 0.35 <= drop_pct <= 3.5 and rebound_pct >= 0.20 and low_idx <= n - 3:
                    out["pullback_shape_ok"] = True
                # 고점 찍고 충분히 밀렸다가 고점 근처 재도전 중이면, 바로 초록이 아니라 재돌파 확인대기.
                retest_gap = (peak - current_price) / peak * 100.0 if peak > 0 else 999.0
                if drop_pct >= 1.25 and 0 <= retest_gap <= 0.55 and rebound_pct >= 0.80:
                    out["failed_peak_retest"] = True

    # 2) 최근 1분 안에서 고점 찍고 현재가가 밀리는지
    last60 = window_samples(hist, 60)
    if len(last60) >= 4:
        vals60 = [p for _ts, p, _m in last60]
        hi60 = max(vals60)
        if hi60 > 0:
            drop_from_60_high = (hi60 - current_price) / hi60 * 100.0
            if drop_from_60_high >= 0.22:
                out["current_milrim"] = True

    # 3) 저점 방어: 최근 저점이 이전 저점보다 크게 무너지지 않고 현재가가 저점 위로 회복
    last6m = window_samples(hist, 360)
    if len(last6m) >= 18:
        vals = [p for _ts, p, _m in last6m]
        half = max(3, len(vals)//2)
        prev_low = min(vals[:half])
        recent_low = min(vals[half:])
        if prev_low > 0 and recent_low > 0:
            not_breaking = recent_low >= prev_low * 0.994
            recovered = current_price >= recent_low * 1.002
            out["low_defense_ok"] = bool(not_breaking and recovered)

    # 4) 최근 급등 후 고점 근처/박스 상단 추격 위험
    last12m = window_samples(hist, 720)
    if len(last12m) >= 20:
        vals = [p for _ts, p, _m in last12m]
        lo, hi = min(vals), max(vals)
        if lo > 0 and hi > lo:
            span_pct = (hi - lo) / lo * 100.0
            dist_high = (hi - current_price) / hi * 100.0
            last_quarter_high = max(vals[int(len(vals)*0.75):])
            recent_peak = last_quarter_high >= hi * 0.998
            if recent_peak and 0 <= dist_high <= 0.22 and span_pct >= 1.8:
                out["recent_spike_near_high"] = True
            # 박스권 상단에서 잦은 방향 전환 후 고점 근처면 추격 위험
            flips = 0
            for i in range(2, len(vals)):
                d1 = vals[i-1] - vals[i-2]
                d2 = vals[i] - vals[i-1]
                if d1 and d2 and d1*d2 < 0:
                    flips += 1
            zone_ratio = (current_price - lo) / (hi - lo)
            if zone_ratio >= 0.84 and 0 <= dist_high <= 0.32 and 1.0 <= span_pct <= 5.5 and flips >= 5:
                out["box_top_chase"] = True
    return out


def compute_row(ticker: str, price: float, money: float, ts: float, hist: deque, data_quality: dict | None = None) -> MarketRow:
    dq = data_quality or {}
    hist_span = max(0.0, ts - hist[0][0]) if hist else 0.0
    hist_points = len(hist)
    old1 = nearest_old_price(hist, 60)
    old3 = nearest_old_price(hist, 180)
    old5 = nearest_old_price(hist, 300)
    old15 = nearest_old_price(hist, 900)
    h30, l30 = high_low_in_window(hist, 1800)
    ch1_ready = old1 > 0
    ch3_ready = old3 > 0
    ch5_ready = old5 > 0
    ch1 = pct(price, old1) if ch1_ready else 0.0
    ch3 = pct(price, old3) if ch3_ready else 0.0
    ch5 = pct(price, old5) if ch5_ready else 0.0
    ch15 = pct(price, old15) if old15 > 0 else 0.0
    from_low = pct(price, l30) if l30 else 0.0
    high_gap = max(0.0, pct(h30, price)) if h30 and price else 999.0
    range30_pct = pct(h30, l30) if h30 and l30 else 0.0

    money1, money1_ready = money_delta_from(hist, money, 60)
    money3, money3_ready = money_delta_from(hist, money, 180)
    money5, money5_ready = money_delta_from(hist, money, 300)

    # 24H 대금은 최소 유동성, 최근 대금 delta는 돈 유지/반짝 분리에 사용한다.
    money_sustain_ok = False
    money_flash_risk = False
    if money3_ready:
        if money3 >= 20_000_000:
            money_sustain_ok = True
        # 1분에만 튀고 3~5분 누적이 거의 없으면 반짝 위험.
        if money1_ready and money1 >= 8_000_000 and money3 < max(12_000_000, money1 * 1.18):
            money_flash_risk = True
    else:
        # delta가 아직 없으면 24H 대금이 충분한 경우만 중립적으로 본다.
        money_sustain_ok = money >= 3_000_000_000

    # WebSocket 체결 허브가 있으면 24H 대금보다 단기 체결 흐름을 우선 보조로 쓴다.
    ws_amt60 = safe_float(dq.get("ws_trade_amount_60", 0), 0)
    ws_cnt60 = int(safe_float(dq.get("ws_trade_count_60", 0), 0))
    if ws_amt60 >= 8_000_000 and ws_cnt60 >= 3:
        money_sustain_ok = True
    if 0 < ws_amt60 < 1_000_000 and money1_ready and money1 >= 10_000_000:
        money_flash_risk = True

    tick_noise = detect_tick_noise(price, hist, range30_pct)
    shape = analyze_shape_from_history(hist, price)
    overheat_watch = False
    if from_low >= 5.5 and high_gap < 1.2:
        overheat_watch = True
    if range30_pct >= 8.0 and high_gap < 0.65:
        overheat_watch = True

    money_score = 0.0
    if money >= 50_000_000_000:
        money_score = 1.2
    elif money >= 10_000_000_000:
        money_score = 0.9
    elif money >= 3_000_000_000:
        money_score = 0.6
    elif money >= 500_000_000:
        money_score = 0.3

    # v2.7.4 점수: 후보 발견용 점수다. 실제 초록/노랑은 classify에서 다시 가른다.
    raw = (ch1 * 1.55) + (ch3 * 1.65) + (ch5 * 0.75) + money_score
    if shape["pullback_shape_ok"]:
        raw += 0.9
    if shape["low_defense_ok"]:
        raw += 0.45
    if money_sustain_ok:
        raw += 0.35
    if 0.15 <= from_low <= 1.65:
        raw += 0.25
    if ch1 >= 0 and ch3 >= 0.12:
        raw += 0.20
    if shape["current_milrim"]:
        raw -= 0.55
    if tick_noise:
        raw -= 1.2
    if overheat_watch:
        raw -= 1.1
    if shape["failed_peak_retest"]:
        raw -= 0.7
    if shape["box_top_chase"]:
        raw -= 0.7
    if money_flash_risk:
        raw -= 0.35
    if hist_span < 75:
        raw -= 0.8
    if str(dq.get("data_quality", "REST_ONLY")) in ("REST_ONLY", "PRICE_MISMATCH"):
        raw -= 0.45

    return MarketRow(
        ticker=ticker, price=price, money24h=money, ch1=ch1, ch3=ch3, ch5=ch5, ch15=ch15,
        high30=h30, low30=l30, from_low30=from_low, high_gap30=high_gap,
        hist_span_sec=hist_span, hist_points=hist_points, ch1_ready=ch1_ready, ch3_ready=ch3_ready, ch5_ready=ch5_ready,
        range30_pct=range30_pct,
        money1m=money1, money3m=money3, money5m=money5,
        money1_ready=money1_ready, money3_ready=money3_ready, money5_ready=money5_ready,
        money_sustain_ok=money_sustain_ok, money_flash_risk=money_flash_risk,
        tick_noise=tick_noise,
        pullback_shape_ok=shape["pullback_shape_ok"], pullback_drop_pct=shape["pullback_drop_pct"], pullback_rebound_pct=shape["pullback_rebound_pct"],
        low_defense_ok=shape["low_defense_ok"], failed_peak_retest=shape["failed_peak_retest"],
        recent_spike_near_high=shape["recent_spike_near_high"], box_top_chase=shape["box_top_chase"],
        current_milrim=shape["current_milrim"], overheat_watch=overheat_watch,
        data_quality=str(dq.get("data_quality", "REST_ONLY")),
        price_source=str(dq.get("price_source", "REST")),
        rest_price=safe_float(dq.get("rest_price", 0), 0),
        ws_price=safe_float(dq.get("ws_price", 0), 0),
        ws_price_age_sec=safe_float(dq.get("ws_price_age_sec", 999999), 999999),
        ws_trade_amount_60=safe_float(dq.get("ws_trade_amount_60", 0), 0),
        ws_trade_count_60=int(safe_float(dq.get("ws_trade_count_60", 0), 0)),
        ws_buy_ratio_60=safe_float(dq.get("ws_buy_ratio_60", 0), 0),
        own1m_ok=bool(dq.get("own1m_ok", False)),
        own1m_state=str(dq.get("own1m_state", "정보없음")),
        own1m_body_pct=safe_float(dq.get("own1m_body_pct", 0), 0),
        own1m_upper_wick_pct=safe_float(dq.get("own1m_upper_wick_pct", 0), 0),
        own1m_lower_wick_pct=safe_float(dq.get("own1m_lower_wick_pct", 0), 0),
        own1m_turnover=safe_float(dq.get("own1m_turnover", 0), 0),
        score=raw, updated_ts=ts
    )

def make_candidate(row: MarketRow, status: str, label: str, icon: str, reason: str, *, pattern: str = "", caution: str = "", entry_note: str = "", reject_reason: str = "") -> Candidate:
    stop = row.low30 * 0.995 if row.low30 else row.price * 0.985
    return Candidate(
        ticker=row.ticker, status=status, label=label, icon=icon, reason=reason,
        price=row.price, score=row.score, ch1=row.ch1, ch3=row.ch3, ch5=row.ch5,
        money24h=row.money24h, from_low30=row.from_low30, high_gap30=row.high_gap30,
        stop_price=stop, entry_note=entry_note, reject_reason=reject_reason,
        updated_ts=row.updated_ts, pattern=pattern, caution=caution,
        ch1_ready=row.ch1_ready, ch3_ready=row.ch3_ready, ch5_ready=row.ch5_ready, range30_pct=row.range30_pct,
        money1m=row.money1m, money3m=row.money3m, money5m=row.money5m,
        money_sustain_ok=row.money_sustain_ok, money_flash_risk=row.money_flash_risk, tick_noise=row.tick_noise,
        pullback_shape_ok=row.pullback_shape_ok, pullback_drop_pct=row.pullback_drop_pct, pullback_rebound_pct=row.pullback_rebound_pct,
        low_defense_ok=row.low_defense_ok, failed_peak_retest=row.failed_peak_retest, recent_spike_near_high=row.recent_spike_near_high,
        box_top_chase=row.box_top_chase, current_milrim=row.current_milrim, overheat_watch=row.overheat_watch,
        data_quality=row.data_quality, price_source=row.price_source, rest_price=row.rest_price, ws_price=row.ws_price,
        ws_price_age_sec=row.ws_price_age_sec, ws_trade_amount_60=row.ws_trade_amount_60,
        ws_trade_count_60=row.ws_trade_count_60, ws_buy_ratio_60=row.ws_buy_ratio_60,
        own1m_ok=row.own1m_ok, own1m_state=row.own1m_state, own1m_body_pct=row.own1m_body_pct,
        own1m_upper_wick_pct=row.own1m_upper_wick_pct, own1m_lower_wick_pct=row.own1m_lower_wick_pct,
        own1m_turnover=row.own1m_turnover,
    )


def classify_candidate(row: MarketRow) -> Candidate:
    """
    v2.7.4 눌림/상승초입 판정 복구 묶음.
    목표는 여전히 딱 2개다.
    1) 눌림대기/눌림 재확인
    2) 상승시작 초입

    이번 버전은 v2.6 코드 덩어리를 복붙하지 않고, 예전 눌림 승률라인의 핵심 문턱을 clean core 자료로 재구성한다.
    - 진짜 눌림 구조 확인
    - 초저가/틱노이즈 차단
    - 고점 찍고 밀린 후보 재돌파 대기 처리
    - 과열 급등 후보 분리
    - 돈 유지/반짝 거래량 분리
    - 1분 현재봉 밀림/회복 판단
    - 저점 방어 강도 보강
    """
    stale = now_ts() - row.updated_ts > 45
    data_1m_ok = row.ch1_ready and row.hist_span_sec >= 55
    data_3m_ok = row.ch3_ready and row.hist_span_sec >= 150
    data_5m_ok = row.ch5_ready and row.hist_span_sec >= 260

    money_watch = row.money24h >= 100_000_000       # 1억: 감시 가능
    money_ok = row.money24h >= 500_000_000          # 5억: 본선 가능
    money_strong = row.money24h >= 3_000_000_000    # 30억: 강한 돈

    high_touch = row.high_gap30 < 0.12
    one_min_recovered = data_1m_ok and row.ch1 >= 0.04
    three_min_alive = data_3m_ok and row.ch3 >= 0.12
    five_min_alive = data_5m_ok and row.ch5 >= 0.10
    one_min_milrim = row.current_milrim or (data_1m_ok and row.ch1 < -0.10 and data_3m_ok and row.ch3 >= 0.20)

    hot_near_high = False
    if row.range30_pct >= 6.0 and row.high_gap30 < 0.28:
        hot_near_high = True
    if row.range30_pct >= 10.0 and row.high_gap30 < 0.55:
        hot_near_high = True
    if row.range30_pct >= 14.0 and row.high_gap30 < 0.85:
        hot_near_high = True

    too_extended = row.from_low30 >= 3.2 or (row.from_low30 >= 2.2 and high_touch)
    late_chase = hot_near_high or too_extended or row.overheat_watch
    data_green_ok = row.data_quality in ("WS_OK", "WS_PRICE_OK")

    if stale:
        return make_candidate(row, "WATCH", "자료확인", "⚪", "자료가 오래되어 감시만", pattern="DATA_STALE", entry_note="자료 갱신 후 재확인", reject_reason="자료 오래됨")

    if not data_1m_ok:
        return make_candidate(row, "WATCH", "자료쌓는중", "⚪", "부팅 직후라 1분 기준값 부족", pattern="WARMUP", entry_note="1분 이상 자료가 쌓인 뒤 재평가", reject_reason="자료 부족")

    if row.tick_noise:
        return make_candidate(row, "WATCH", "틱노이즈 감시", "⚪", "초저가/틱노이즈라 퍼센트 왜곡 가능", pattern="TICK_NOISE_WATCH", caution="초저가 틱노이즈", entry_note="구조가 선명해지면 재평가", reject_reason="초저가 틱노이즈")

    if not data_3m_ok:
        if money_ok and row.ch1 >= 0.20 and not row.money_flash_risk:
            return make_candidate(row, "PENDING", "초입대기", "🟡", "1분 움직임은 있으나 3분 자료가 아직 부족", pattern="START_EARLY_WAIT", caution="초기자료", entry_note="3분 자료가 쌓이면 재평가", reject_reason="3분 기준 부족")
        if row.ch1 > 0.05:
            return make_candidate(row, "WATCH", "초기감시", "⚪", "움직임은 있으나 자료가 아직 부족", pattern="EARLY_WATCH", caution="초기자료", entry_note="자료 누적 후 재평가", reject_reason="자료 부족")

    if row.overheat_watch:
        return make_candidate(row, "WATCH", "과열감시", "⚪", "급등 후 고점 근처라 추격위험", pattern="OVERHEAT_WATCH", caution="과열/추격주의", entry_note="확실한 눌림 후 재확인 전까지 대기", reject_reason="과열 추격주의")

    if row.failed_peak_retest:
        return make_candidate(row, "PENDING", "재돌파 확인대기", "🟡", "고점 찍고 밀린 뒤 재도전 중이라 유지 확인 필요", pattern="REBREAK_WAIT", caution="고점 재돌파 확인", entry_note="재돌파 후 1분봉 유지 확인", reject_reason="재돌파 확인 전")

    if row.recent_spike_near_high or row.box_top_chase:
        return make_candidate(row, "WATCH", "추격주의", "⚪", "최근 고점/박스 상단 추격 위험", pattern="CHASE_RISK_WATCH", caution="고점 추격주의", entry_note="눌림 구조가 생기면 재평가", reject_reason="추격위험")

    if row.money_flash_risk:
        return make_candidate(row, "WATCH", "반짝돈 감시", "⚪", "순간 거래만 붙고 돈 유지가 약함", pattern="FLASH_MONEY_WATCH", caution="반짝 거래량", entry_note="3분 이상 돈 유지 확인", reject_reason="돈 유지 부족")

    # 1) 눌림 재확인: '상승→눌림→저점방어→재반등' 구조를 우선한다.
    pullback_reconfirm = (
        money_ok
        and row.money_sustain_ok
        and row.pullback_shape_ok
        and row.low_defense_ok
        and one_min_recovered
        and three_min_alive
        and row.score >= 0.80
        and 0.18 <= row.from_low30 <= 2.85
        and not late_chase
        and not one_min_milrim
    )

    # 2) 상승시작 초입: 아직 과하게 뜨지 않은 상태에서 1분/3분과 돈이 같이 붙는 구간.
    # 단순 돌파형은 여기 안에서만 허용하되, 돈이 얇거나 반짝이면 초록 금지.
    start_early = (
        money_ok
        and row.money_sustain_ok
        and data_3m_ok
        and 0.16 <= max(row.ch1, row.ch3) <= 1.25
        and row.ch1 >= 0.02
        and row.ch3 >= 0.15
        and row.from_low30 <= 1.65
        and row.range30_pct <= 3.8
        and not late_chase
        and not one_min_milrim
    )
    start_break_hold = (
        money_strong
        and row.money_sustain_ok
        and data_3m_ok
        and high_touch
        and row.ch1 >= 0.18
        and row.ch3 >= 0.22
        and row.from_low30 <= 1.45
        and row.range30_pct <= 2.8
        and not hot_near_high
        and not one_min_milrim
    )

    if pullback_reconfirm:
        caution = []
        if high_touch:
            caution.append("고점 돌파 유지 확인")
        if not data_5m_ok:
            caution.append("5분 정보없음")
        if row.pullback_drop_pct:
            caution.append(f"눌림폭 {row.pullback_drop_pct:.2f}%")
        if not data_green_ok:
            caution.append("데이터 신뢰도 확인")
            return make_candidate(row, "PENDING", "데이터확인대기", "🟡", f"눌림 구조는 보이나 {row.data_quality} 상태라 WebSocket/자체봉 확인 필요", pattern="DATA_CONFIRM_WAIT", caution=" / ".join(caution), entry_note="WS 현재가·체결·자체1분봉 확인 후 초록 가능", reject_reason="데이터 확인 전")
        return make_candidate(row, "BUY_READY", "진입가능 검토", "🟢", "눌림 재확인 통과", pattern="PULLBACK_RECONFIRM", caution=" / ".join(caution), entry_note="실거래 전 호가/슬리피지 재검사 필요")

    if start_early or start_break_hold:
        caution = []
        if high_touch:
            caution.append("고점 돌파 유지 확인")
        if not data_5m_ok:
            caution.append("5분 정보없음")
        reason = "상승시작 초입 통과" if start_early else "상승시작 초입-돌파유지 통과"
        pattern = "START_EARLY" if start_early else "START_EARLY_BREAK_HOLD"
        if not data_green_ok:
            caution.append("데이터 신뢰도 확인")
            return make_candidate(row, "PENDING", "데이터확인대기", "🟡", f"초입 구조는 보이나 {row.data_quality} 상태라 WebSocket/자체봉 확인 필요", pattern="DATA_CONFIRM_WAIT", caution=" / ".join(caution), entry_note="WS 현재가·체결·자체1분봉 확인 후 초록 가능", reject_reason="데이터 확인 전")
        return make_candidate(row, "BUY_READY", "진입가능 검토", "🟢", reason, pattern=pattern, caution=" / ".join(caution), entry_note="실거래 전 호가/슬리피지 재검사 필요")

    # 노랑: 목표 후보일 가능성은 있으나 확인 전.
    pending_reasons = []
    candidate_like = money_ok and (three_min_alive or five_min_alive or row.ch1 >= 0.18 or one_min_milrim or row.pullback_shape_ok or row.low_defense_ok)
    if candidate_like:
        if row.pullback_shape_ok and not row.low_defense_ok:
            pending_reasons.append("눌림은 보이나 저점방어 확인 부족")
        if row.low_defense_ok and not one_min_recovered:
            pending_reasons.append("저점은 버티지만 1분 회복 부족")
        if one_min_milrim:
            pending_reasons.append("3분은 강하지만 1분이 밀림")
        if late_chase:
            pending_reasons.append("많이 오른 뒤 고점 근처라 추격 확인 필요")
        if high_touch:
            pending_reasons.append("고점 바로 근처라 유지 확인 필요")
        if not one_min_recovered:
            pending_reasons.append("1분 회복 부족")
        if not row.money_sustain_ok:
            pending_reasons.append("돈 유지 확인 필요")
        if not data_5m_ok:
            pending_reasons.append("5분 정보없음")
        elif not five_min_alive and row.ch3 >= 0.25:
            pending_reasons.append("5분 유지 확인 필요")
        if not pending_reasons:
            pending_reasons.append("돈은 있으나 재확인 전")
        if row.pullback_shape_ok or row.low_defense_ok or row.from_low30 >= 0.15:
            pattern = "PULLBACK_WAIT"
            label = "눌림대기"
        else:
            pattern = "START_EARLY_WAIT"
            label = "초입대기"
        return make_candidate(row, "PENDING", label, "🟡", " / ".join(dict.fromkeys(pending_reasons)), pattern=pattern, caution="재확인 필요", entry_note="1분 회복·저점방어·고점추격 여부 확인", reject_reason="아직 PENDING")

    # 감시: 움직임은 있으나 두 목표에 아직 안 맞음.
    if row.score >= 0.15 or row.ch1 > 0.05 or row.ch3 > 0.08:
        reason = "움직임은 있으나 두 목표와 아직 안 맞음"
        pattern = "WATCH"
        if not money_watch:
            reason = "움직임은 있으나 돈/유동성 부족"
        elif late_chase:
            reason = "이미 많이 떠서 추격 위험"
            pattern = "CHASE_RISK_WATCH"
        elif not row.money_sustain_ok:
            reason = "움직임은 있으나 돈 유지 확인 부족"
            pattern = "MONEY_WAIT_WATCH"
        return make_candidate(row, "WATCH", "감시만", "⚪", reason, pattern=pattern, entry_note="강해지면 재평가", reject_reason="매수 근거 부족")

    return make_candidate(row, "REJECT", "버림", "🔴", "점수/움직임 부족", pattern="REJECT", reject_reason="점수 낮음")

def rebuild_hubs(raw: dict[str, dict]):
    ts = now_ts()
    rows: dict[str, MarketRow] = {}
    with STATE.lock:
        for ticker, item in raw.items():
            rest_price = float(item["price"])
            money = float(item.get("money24h") or 0.0)
            dq = get_realtime_quality_snapshot(ticker, rest_price)
            price = float(dq.get("price") or rest_price)
            hist = STATE.price_history[ticker]
            hist.append((ts, price, money))
            # 오래된 포인트 제거
            cutoff = ts - HISTORY_KEEP_SEC
            while hist and hist[0][0] < cutoff:
                hist.popleft()
            rows[ticker] = compute_row(ticker, price, money, ts, hist, dq)

        ranked = sorted(rows.values(), key=lambda r: (r.score, r.ch1, r.ch3, r.money24h), reverse=True)
        candidates_all = [classify_candidate(r) for r in ranked[:200]]
        # 상태 기준 분류만 한다. 초록 개수 제한으로 강제 강등하지 않는다.
        candidates = [c for c in candidates_all if c.status in ("BUY_READY", "PENDING", "WATCH")][:MAX_CANDIDATES]
        rejected = [c for c in candidates_all if c.status == "REJECT"][:40]

        STATE.market_rows = rows
        STATE.rank_rows = ranked[:80]
        STATE.candidates = candidates
        STATE.rejected = rejected
        STATE.market_ts = ts
        STATE.rank_ts = ts
        STATE.candidate_ts = ts
        STATE.stage = "market_ready"


def market_worker():
    with STATE.lock:
        STATE.stage = "market_worker_start"
    while not STATE.shutdown:
        start = now_ts()
        try:
            raw = fetch_bithumb_all_ticker()
            if raw:
                rebuild_hubs(raw)
                with STATE.lock:
                    STATE.fetch_ok += 1
                    STATE.last_fetch_error = ""
                record_event(f"시장자료 갱신 {len(raw)}개")
            else:
                raise RuntimeError("empty ticker payload")
        except Exception as e:
            with STATE.lock:
                STATE.fetch_fail += 1
                STATE.last_fetch_error = str(e)[:220]
                STATE.stage = "market_fetch_error"
            log_error("market_worker", e)
        with STATE.lock:
            STATE.main_loop_ts = now_ts()
        elapsed = now_ts() - start
        time.sleep(max(1.0, MARKET_REFRESH_SEC - elapsed))


def light_state_save_worker():
    while not STATE.shutdown:
        time.sleep(STATUS_SNAPSHOT_SAVE_SEC)
        try:
            with STATE.lock:
                data = {
                    "version": BOT_VERSION,
                    "ts": now_ts(),
                    "stage": STATE.stage,
                    "market_count": len(STATE.market_rows),
                    "rank_count": len(STATE.rank_rows),
                    "candidate_count": len(STATE.candidates),
                    "auto_enabled": STATE.auto_enabled,
                    "fetch_ok": STATE.fetch_ok,
                    "fetch_fail": STATE.fetch_fail,
                }
            atomic_write(STATUS_STATE_FILE, json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            log_error("state_save", e)


# =========================================================
# 렌더러
# =========================================================
def snapshot():
    with STATE.lock:
        return {
            "stage": STATE.stage,
            "main_loop_ts": STATE.main_loop_ts,
            "market_ts": STATE.market_ts,
            "rank_ts": STATE.rank_ts,
            "candidate_ts": STATE.candidate_ts,
            "output_ts": STATE.output_ts,
            "market_count": len(STATE.market_rows),
            "rank_count": len(STATE.rank_rows),
            "candidate_count": len(STATE.candidates),
            "buy_ready": len([c for c in STATE.candidates if c.status == "BUY_READY"]),
            "pending": len([c for c in STATE.candidates if c.status == "PENDING"]),
            "watch": len([c for c in STATE.candidates if c.status == "WATCH"]),
            "fetch_ok": STATE.fetch_ok,
            "fetch_fail": STATE.fetch_fail,
            "last_fetch_error": STATE.last_fetch_error,
            "auto_enabled": STATE.auto_enabled,
            "real_order_enabled": STATE.real_order_enabled,
            "cmd_timings": list(STATE.cmd_timings)[:12],
            "last_batch_expected": STATE.last_batch_expected,
            "last_batch_recorded": STATE.last_batch_recorded,
            "last_batch_total_sec": STATE.last_batch_total_sec,
            "recent_events": list(STATE.recent_events)[:8],
            "errors": list(STATE.errors)[:5],
            "admin_check_count": STATE.admin_check_count,
            "deploy_check_count": STATE.deploy_check_count,
            "update_bot_count": STATE.update_bot_count,
            "buy_check_count": STATE.buy_check_count,
        }


def status_flags(s):
    market_age = now_ts() - s["market_ts"] if s["market_ts"] else 999999
    rank_age = now_ts() - s["rank_ts"] if s["rank_ts"] else 999999
    cmd_age = now_ts() - s["output_ts"] if s["output_ts"] else 999999
    market_ok = market_age <= 35 and s["market_count"] >= 50
    rank_ok = rank_age <= 90 and s["rank_count"] >= 20
    candidate_ok = s["candidate_count"] > 0
    output_ok = cmd_age <= 120 or s["output_ts"] is None  # 요청형이라 없어도 치명 아님
    return market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok


def render_ping() -> str:
    s = snapshot()
    return (
        f"🏓 봇 살아있음\n"
        f"- 버전: {BOT_VERSION}\n"
        f"- 파일: {BOT_FILE_TAG}\n"
        f"- 단계: {s['stage']}\n"
        f"- 메인루프: {age_text(s['main_loop_ts'])}\n"
        f"- 시장자료: {age_text(s['market_ts'])} / {s['market_count']}개\n"
        f"- 후보: 진입검토 {s['buy_ready']} / 대기 {s['pending']} / 감시 {s['watch']}\n"
    )


def render_system_check() -> str:
    s = snapshot()
    market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok = status_flags(s)
    judgement = "정상" if market_ok and rank_ok and candidate_ok else "확인필요"
    traffic = "FRONTLINE" if market_ok and rank_ok else "DATA_RECOVERY"
    return (
        f"🏭 전체 상태판 /system_check v2.7.4\n"
        f"- 한줄판정: {judgement} / 교통정리 {traffic}\n"
        f"- 자료흐름: 거래가능 추정 454 → 현재허브 {s['market_count']} → 순위 {s['rank_count']} → 후보 {s['candidate_count']}\n"
        f"- 후보판: 🟢 {s['buy_ready']} / 🟡 {s['pending']} / ⚪ {s['watch']}\n\n"
        f"1) 허브\n"
        f"- {'✅' if market_ok else '⚠️'} 시장허브: {age_text(s['market_ts'])} / {s['market_count']}개\n"
        f"- {'✅' if rank_ok else '⚠️'} 순위허브: {age_text(s['rank_ts'])} / {s['rank_count']}개\n"
        f"- {'✅' if candidate_ok else '⚠️'} 후보허브: {age_text(s['candidate_ts'])} / {s['candidate_count']}개\n"
        f"- ✅ 출력: 요청 시 조립 / 무거운 캐시 worker 없음\n"
        f"- 🧱 데이터허브: /data_check에서 WebSocket·자체1분봉 확인\n\n"
        f"2) 직원\n"
        f"- 시장자료 직원: fetch OK {s['fetch_ok']} / FAIL {s['fetch_fail']}\n"
        f"- 후보분류 직원: 최신 후보 {s['candidate_count']}개\n"
        f"- 실거래 직원: 보호모드 / 실제 주문 {'허용' if s['real_order_enabled'] else '차단'}\n"
        f"- legacy 직원: 기본 실행 없음\n"
        f"- WebSocket 직원: 현재가/체결 허브 1차 운영\n\n"
        f"3) 문제\n"
        f"- 시장자료: {'정상' if market_ok else f'확인필요 age {int(market_age)}s'}\n"
        f"- 순위자료: {'정상' if rank_ok else f'확인필요 age {int(rank_age)}s'}\n"
        f"- batch 계측: 기록 {s['last_batch_recorded']} / 기대 {s['last_batch_expected']} / 합계 {s['last_batch_total_sec']:.2f}s\n"
        f"- 최근 오류: {s['errors'][0] if s['errors'] else '없음'}\n"
    )


def render_error_doctor() -> str:
    s = snapshot()
    market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok = status_flags(s)
    root = []
    if not market_ok:
        root.append("시장자료 부족/오래됨")
    if not rank_ok:
        root.append("순위자료 부족/오래됨")
    if not candidate_ok:
        root.append("후보분류 결과 없음")
    if not root:
        root.append("현재 큰 병목 없음")
    return (
        f"🩺 오류/병목 진단 /error_doctor v2.7.4\n\n"
        f"1) 현재 판정\n"
        f"- {'✅' if market_ok and rank_ok else '⚠️'} {' / '.join(root)}\n"
        f"- 시장 {s['market_count']}개 / {age_text(s['market_ts'])}\n"
        f"- 순위 {s['rank_count']}개 / {age_text(s['rank_ts'])}\n"
        f"- 후보 {s['candidate_count']}개 / {age_text(s['candidate_ts'])}\n\n"
        f"2) clean core 원칙\n"
        f"- v328/v329/v330/v340 브릿지 기본 실행 없음\n"
        f"- 복기용/C등급 기록을 🟢 후보로 승격하지 않음\n"
        f"- review_trace 직접 재조회 없음\n"
        f"- 출력은 요청 시 경량 조립\n"
        f"- v2.7.4: 눌림 재확인 라인을 clean core 방식으로 복구\n\n"
        f"3) 최근 오류\n" + ("\n".join(f"- {e}" for e in s['errors'][:4]) if s['errors'] else "- 없음") + "\n"
    )


def render_speed_check() -> str:
    s = snapshot()
    market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok = status_flags(s)
    timings = s["cmd_timings"][:8]
    lines = [
        "⏱ 속도 체크 /speed_check v2.7.4",
        "",
        "1) worker",
        f"- 시장자료: {'✅' if market_ok else '⚠️'} {age_text(s['market_ts'])} / {s['market_count']}개",
        f"- 순위자료: {'✅' if rank_ok else '⚠️'} {age_text(s['rank_ts'])} / {s['rank_count']}개",
        f"- 후보분류: {'✅' if candidate_ok else '⚠️'} {age_text(s['candidate_ts'])} / {s['candidate_count']}개",
        "- 출력캐시: 무거운 상시 worker 없음 / 요청 시 조립",
        f"- 데이터허브: /data_check에서 WebSocket 현재가·체결·자체1분봉 확인",
        "",
        "2) batch/명령 계측",
        f"- 최근 batch: 기록 {s['last_batch_recorded']} / 기대 {s['last_batch_expected']} / 합계 {s['last_batch_total_sec']:.2f}s",
    ]
    if timings:
        for t in timings[:6]:
            mark = "🚨" if t["sec"] >= 8 else "⚠️" if t["sec"] >= 3 else ""
            lines.append(f"  · {t['cmd']}: {t['sec']:.2f}s {mark}".rstrip())
    else:
        lines.append("  · 기록 없음")
    lines += [
        "",
        "3) CPU 줄인 지점",
        "- review_trace_detail 직접 재조회 제외",
        "- full/review_dump 기본 생성 제외",
        "- legacy 브릿지 기본 실행 제외",
    ]
    return "\n".join(lines)


def pct_text(value: float, ready: bool = True) -> str:
    if not ready:
        return "정보없음"
    return f"{value:+.2f}%"


def pattern_kor(pattern: str) -> str:
    table = {
        "PULLBACK_RECONFIRM": "눌림 재확인",
        "PULLBACK_WAIT": "눌림대기",
        "START_EARLY": "상승시작 초입",
        "START_EARLY_BREAK_HOLD": "상승시작 초입(돌파유지)",
        "START_EARLY_WAIT": "초입대기",
        "REBREAK_WAIT": "재돌파 확인대기",
        "OVERHEAT_WATCH": "과열감시",
        "TICK_NOISE_WATCH": "틱노이즈 감시",
        "FLASH_MONEY_WATCH": "반짝돈 감시",
        "CHASE_RISK_WATCH": "추격주의 감시",
        "MONEY_WAIT_WATCH": "돈유지 감시",
        "EARLY_WATCH": "초기감시",
        "WARMUP": "자료쌓는중",
        "DATA_CONFIRM_WAIT": "데이터확인대기",
        "WATCH": "감시",
        "REJECT": "버림",
    }
    return table.get(str(pattern or ""), str(pattern or "-"))


def render_buy_check() -> str:
    with STATE.lock:
        STATE.buy_check_count += 1
        cands = list(STATE.candidates)
        market_count = len(STATE.market_rows)
        rank_count = len(STATE.rank_rows)
        cand_ts = STATE.candidate_ts
    ready = [c for c in cands if c.status == "BUY_READY"]
    pending = [c for c in cands if c.status == "PENDING"]
    watch = [c for c in cands if c.status == "WATCH"]
    lines = [
        "🎯 매수 후보판 /buy_check v2.7.4",
        "- 자동매매 OFF여도 후보 품질을 보기 위한 화면. 실제 매수 명령 아님.",
        f"- 자료: 현재허브 {market_count} → 순위 {rank_count} → 후보 {len(cands)} / 후보갱신 {age_text(cand_ts)}",
        "- 원칙: 복기용/C등급/순위권 단순포착은 🟢 진입가능으로 올리지 않음",
        "",
    ]
    def add_group(title, items, limit):
        lines.append(title)
        if not items:
            lines.append("- 없음")
            return
        for idx, c in enumerate(items[:limit], 1):
            lines.append(f"{idx}. {c.icon} {c.label} / {c.ticker}")
            lines.append(f"- 현재가 {price_text(c.price)} / 점수 {c.score:.2f}")
            lines.append(f"- 흐름 1분 {pct_text(c.ch1, c.ch1_ready)} / 3분 {pct_text(c.ch3, c.ch3_ready)} / 5분 {pct_text(c.ch5, c.ch5_ready)}")
            money_bits = []
            if c.money1m > 0:
                money_bits.append(f"1분 {krw(c.money1m)}")
            if c.money3m > 0:
                money_bits.append(f"3분 {krw(c.money3m)}")
            money_short = " / 단기돈 " + ", ".join(money_bits) if money_bits else ""
            lines.append(f"- 돈 {krw(c.money24h)}{money_short} / 저점대비 {c.from_low30:+.2f}% / 고점거리 {c.high_gap30:.2f}% / 30분폭 {c.range30_pct:.2f}%")
            ws_age_txt = f"{int(c.ws_price_age_sec)}초" if c.ws_price_age_sec < 9999 else "없음"
            ws_trade_txt = f"{c.ws_trade_count_60}건·{krw(c.ws_trade_amount_60)}" if c.ws_trade_count_60 or c.ws_trade_amount_60 else "없음"
            lines.append(f"- 데이터: {c.data_quality} / 가격소스 {c.price_source} / WS나이 {ws_age_txt} / 체결60초 {ws_trade_txt} / 자체1분봉 {c.own1m_state}({c.own1m_body_pct:+.2f}%)")
            lines.append(f"- 유형: {pattern_kor(c.pattern or c.status)}")
            lines.append(f"- 판단: {c.reason}")
            if c.caution:
                lines.append(f"- 주의: {c.caution}")
            if c.stop_price:
                lines.append(f"- 손절 참고: {price_text(c.stop_price)} 부근 이탈")
            if c.entry_note:
                lines.append(f"- 메모: {c.entry_note}")
            if c.reject_reason:
                lines.append(f"- 미진입/주의: {c.reject_reason}")
            lines.append("")
    add_group("1) 🟢 진입가능 검토", ready, 5)
    add_group("2) 🟡 눌림/재확인 대기", pending, 6)
    add_group("3) ⚪ 감시만", watch, 5)
    lines.append("판단 기준")
    lines.append("- 🟢: 눌림 재확인 또는 상승시작 초입이 상태 기준을 통과한 후보. 개수 제한 없음")
    lines.append("- 🟡: 두 목표 중 하나일 가능성은 있지만 1분 회복·고점유지·자료누적 재확인 전")
    lines.append("- ⚪: 움직임은 있으나 지금은 눌림/상승초입 근거가 약함")
    lines.append("- 참고: 5분 기준값이 없으면 0.00%가 아니라 정보없음으로 표시")
    lines.append("- v2.7.4: WebSocket 현재가·체결·자체1분봉 허브를 먼저 지어 데이터 신뢰도를 표시")
    lines.append("- WS_OK/WS_PRICE_OK 전에는 초록을 보수적으로 대기 처리")
    return "\n".join(lines)


def render_auto_status() -> str:
    s = snapshot()
    market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok = status_flags(s)
    safe = market_ok and rank_ok and candidate_ok
    return (
        f"🤖 자동매매 상태 /auto_status v2.7.4\n"
        f"- 자동매매 스위치: {'켜짐' if s['auto_enabled'] else '꺼짐'}\n"
        f"- 실거래 주문: {'허용' if s['real_order_enabled'] else '보호모드 차단'}\n"
        f"- 허용전략: 눌림 재확인 라인만 / 1회 {MAX_BUY_KRW:,}원 / 동시보유 {MAX_POSITIONS}개\n"
        f"- 자료안전: {'✅ 정상' if safe else '⚠️ 확인필요'} / 시장 {age_text(s['market_ts'])} / 순위 {age_text(s['rank_ts'])}\n"
        f"- 후보: 🟢 {s['buy_ready']} / 🟡 {s['pending']} / ⚪ {s['watch']}\n\n"
        f"중요\n"
        f"- v2.7.4는 clean core 눌림/상승초입 검증판이라 실제 주문은 막아둠.\n"
        f"- 후보 품질 확인 후 후보판 검증 후 실거래 직원 재연결 예정.\n"
    )


def render_real_flow() -> str:
    s = snapshot()
    with STATE.lock:
        cands = list(STATE.candidates)[:8]
        events = list(STATE.recent_events)[:8]
    lines = [
        "🤖 실거래 후보 흐름 /real_flow v2.7.4",
        "",
        "1) 현재 상태",
        f"- 자동매매 스위치: {'켜짐' if s['auto_enabled'] else '꺼짐'} / 실거래 주문: {'허용' if s['real_order_enabled'] else '보호모드 차단'}",
        f"- 후보: 🟢 {s['buy_ready']} / 🟡 {s['pending']} / ⚪ {s['watch']}",
        "",
        "2) 최근 후보",
    ]
    if cands:
        for idx, c in enumerate(cands, 1):
            lines.append(f"{idx}. {c.icon} {c.ticker} / {c.label} / ch1 {c.ch1:+.2f}% / ch3 {c.ch3:+.2f}% / 점수 {c.score:.2f}")
    else:
        lines.append("- 아직 후보 없음")
    lines += ["", "3) 최근 이벤트"]
    lines.extend([f"- {e}" for e in events] if events else ["- 없음"])
    return "\n".join(lines)


def render_real_stats() -> str:
    return (
        f"📊 실거래 성과 /real_stats v2.7.4\n"
        f"- v2.7.4 clean core는 실거래 주문 보호모드야.\n"
        f"- 기존 v2.6 실거래 로그를 여기서 섞어 계산하지 않음.\n"
        f"- 실거래 직원은 v2.7.4에서 체결확정/보유보호와 함께 재연결 예정.\n"
    )


def render_deploy_status() -> str:
    with STATE.lock:
        STATE.deploy_check_count += 1
    target = read_text(DEPLOY_TARGET_FILE, "").strip() or "DEPLOY_TARGET.txt 없음"
    deployed = read_text(DEPLOYED_TARGET_FILE, "").strip() or "배포기록 없음"
    log_tail = tail_text(DEPLOY_STATUS_LOG, 35)
    trig_tail = tail_text(UPDATE_TRIGGER_LOG, 25)
    ok = target == deployed and target not in ("", "DEPLOY_TARGET.txt 없음")
    return (
        f"📦 배포 상태 /deploy_status v2.7.4\n"
        f"- 방식: 직접 파일조회 / sudo 없음\n"
        f"- DEPLOY_TARGET: {target}\n"
        f"- deployed_target: {deployed}\n"
        f"- 적용판정: {'✅ 대상과 적용파일 일치' if ok else '⚠️ 확인필요'}\n\n"
        f"[deploy_status.log]\n{log_tail or '로그 없음'}\n\n"
        f"[update_bot_trigger.log]\n{trig_tail or '로그 없음'}\n"
    )


def render_admin_check() -> str:
    with STATE.lock:
        STATE.admin_check_count += 1
    # 외부 스크립트 금지. 빠른 명령만 timeout으로 제한.
    ok_mem, mem, _ = run_cmd(["free", "-h"], timeout=2)
    ok_df, df, _ = run_cmd(["df", "-h", "/"], timeout=2)
    ok_sw, sw, _ = run_cmd(["swapon", "--show"], timeout=2)
    ok_active, active, _ = run_cmd(["systemctl", "is-active", "tradingbot"], timeout=2)
    ok_ps, ps, _ = run_cmd(["bash", "-lc", "ps -eo pid,%cpu,%mem,etime,cmd --sort=-%cpu | head -12"], timeout=2)
    return (
        f"🖥 서버 점검 /admin_check v2.7.4\n"
        f"- 방식: 초경량 내장 점검 / 긴 systemctl status·journalctl 호출 없음\n"
        f"- 봇 실행유저: {os.getenv('USER','?')} / euid {os.geteuid()}\n\n"
        f"[메모리]\n{mem if ok_mem else mem}\n"
        f"[디스크]\n{df if ok_df else df}\n"
        f"[Swap]\n{sw if sw else 'swap 없음'}\n"
        f"[봇 active]\n{active}\n"
        f"[CPU 상위]\n{ps}\n"
    )


def render_legacy_check() -> str:
    return (
        f"🧹 레거시 격리 확인 /legacy_check v2.7.4\n"
        f"- v274/v275/v313/v328/v329/v330/v340 브릿지 기본 실행: OFF\n"
        f"- review_trace_detail 직접 재조회: OFF\n"
        f"- full/review_dump 자동 조립: OFF\n"
        f"- 복기용/C등급 → 🟢 후보 승격: 차단\n"
        f"- 급등복구 본선 선점: 차단\n\n"
        f"판정: clean core는 기존 파일의 함수 덩어리를 통째로 복사하지 않고, 필요한 역할만 다시 구성한 상태야.\n"
    )


RENDERERS = {
    "/ping": render_ping,
    "/system_check": render_system_check,
    "/error_doctor": render_error_doctor,
    "/speed_check": render_speed_check,
    "/buy_check": render_buy_check,
    "/signal": render_buy_check,
    "/data_check": render_data_check,
    "/auto_status": render_auto_status,
    "/real_flow": render_real_flow,
    "/real_stats": render_real_stats,
    "/deploy_status": render_deploy_status,
    "/admin_check": render_admin_check,
    "/server_check": render_admin_check,
    "/legacy_check": render_legacy_check,
}

# =========================================================
# 텔레그램 출력
# =========================================================
def split_text(text: str, limit: int = 3600):
    if len(text) <= limit:
        return [text]
    parts = []
    cur = []
    cur_len = 0
    for line in text.splitlines():
        add = len(line) + 1
        if cur and cur_len + add > limit:
            parts.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(line)
        cur_len += add
    if cur:
        parts.append("\n".join(cur))
    return parts


def send_text(chat_id, text: str):
    for part in split_text(text):
        bot.send_message(chat_id=chat_id, text=part)


def command_to_key(text: str) -> str:
    if not text:
        return ""
    first = text.strip().split()[0]
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


def render_command(key: str) -> tuple[str, bool, float]:
    start = now_ts()
    try:
        if key in RENDERERS:
            text = RENDERERS[key]()
            sec = now_ts() - start
            record_timing(key, sec, True)
            return text, True, sec
        return f"알 수 없는 명령어: {key}", False, now_ts() - start
    except Exception as e:
        sec = now_ts() - start
        log_error(f"render {key}", traceback.format_exc())
        record_timing(key, sec, False)
        return f"⛔ {key} 실행 실패\n- {e}\n- 자세한 내용은 /error_doctor", False, sec


def simple_command_handler(update, context: CallbackContext):
    key = command_to_key(update.message.text)
    text, ok, sec = render_command(key)
    send_text(update.effective_chat.id, text)


def batch_handler(update, context: CallbackContext):
    raw = update.message.text or ""
    lines = [x.strip() for x in raw.splitlines() if x.strip().startswith("/")]
    # 첫 줄 /batch 제거
    commands = []
    for line in lines:
        key = command_to_key(line)
        if key == "/batch":
            continue
        commands.append(key)
    if not commands:
        commands = ["/ping", "/system_check", "/buy_check", "/speed_check"]
    # 중복 과다 방지
    commands = commands[:12]
    start_total = now_ts()
    parts = [
        f"📦 묶음 명령 접수",
        f"- 실행 {len(commands)}개",
        f"- v2.7 clean core: 각 명령은 같은 renderer/계측 경로를 통과",
    ]
    timings = []
    for idx, key in enumerate(commands, 1):
        text, ok, sec = render_command(key)
        timings.append((key, sec, ok))
        parts.append(f"\n▶️ 묶음 {idx}/{len(commands)} {key}\n{text}")
    total = now_ts() - start_total
    with STATE.lock:
        STATE.last_batch_expected = len(commands)
        STATE.last_batch_recorded = len(timings)
        STATE.last_batch_total_sec = total
        STATE.output_ts = now_ts()
    parts.append("\n⏱ 묶음 응답시간")
    parts.append(f"- 실행 기록: {len(timings)}개 / 합계 {total:.2f}s")
    for key, sec, ok in timings:
        mark = "🚨" if sec >= 8 else "⚠️" if sec >= 3 else ""
        parts.append(f"  · {key}: 처리 {sec:.2f}s {mark}".rstrip())
    send_text(update.effective_chat.id, "\n".join(parts))


def multiline_message_handler(update, context: CallbackContext):
    raw = update.message.text or ""
    lines = [x.strip() for x in raw.splitlines() if x.strip().startswith("/")]
    if len(lines) >= 2:
        return batch_handler(update, context)
    # 일반 텍스트는 무시


# =========================================================
# 업데이트/자동매매 명령
# =========================================================
def update_bot_handler(update, context: CallbackContext):
    with STATE.lock:
        STATE.update_bot_count += 1
    target = read_text(DEPLOY_TARGET_FILE, "").strip() or "로컬 DEPLOY_TARGET 없음"
    msg = (
        f"🚀 /update_bot 배포 시작 v2.7.4\n"
        f"- 로컬 대상: {target}\n"
        f"- 서버 스크립트: /usr/local/bin/tradingbot_auto_deploy.sh\n"
        f"- GitHub fetch 후 DEPLOY_TARGET 기준으로 적용\n"
        f"- 배포 중 봇이 재시작되면 이 메시지 뒤에 잠깐 응답이 끊길 수 있음\n"
        f"- 확인: 20~60초 뒤 /deploy_status, /ping\n"
    )
    send_text(update.effective_chat.id, msg)

    def runner():
        try:
            with open(UPDATE_TRIGGER_LOG, "a", encoding="utf-8") as f:
                f.write(f"===== update_bot trigger =====\n{datetime.utcnow()} UTC\nuser={os.getenv('USER','?')} euid={os.geteuid()} target={target}\n")
            ok, out, sec = run_cmd(["sudo", "-n", "/usr/local/bin/tradingbot_auto_deploy.sh"], timeout=SUBPROCESS_TIMEOUT_DEPLOY)
            with open(UPDATE_TRIGGER_LOG, "a", encoding="utf-8") as f:
                f.write(out[-4000:] + f"\nresult ok={ok} sec={sec:.2f}\n")
        except Exception as e:
            log_error("update_bot_runner", e)
    threading.Thread(target=runner, daemon=True).start()


def auto_on_handler(update, context: CallbackContext):
    with STATE.lock:
        STATE.auto_enabled = True
    save_runtime_settings()
    send_text(update.effective_chat.id,
              "🟢 자동매매 스위치 ON\n- 단, v2.7.4 clean core는 실거래 주문 보호모드라 실제 주문은 아직 차단돼.\n- 후보 흐름 검증 후 후보판 검증 후 실거래 직원 재연결 예정.")


def auto_off_handler(update, context: CallbackContext):
    with STATE.lock:
        STATE.auto_enabled = False
    save_runtime_settings()
    send_text(update.effective_chat.id, "⛔ 자동매매 스위치 OFF\n- 신규 실거래 주문 없음. v2.7.4은 원래 보호모드야.")


# =========================================================
# 시작/명령어 등록
# =========================================================
def set_commands(updater: Updater):
    commands = [
        BotCommand("ping", "봇 생존 확인"),
        BotCommand("system_check", "전체 상태판"),
        BotCommand("error_doctor", "오류/병목 진단"),
        BotCommand("speed_check", "속도 체크"),
        BotCommand("buy_check", "매수 후보판"),
        BotCommand("data_check", "데이터 허브 확인"),
        BotCommand("auto_status", "자동매매 상태"),
        BotCommand("real_flow", "후보 흐름"),
        BotCommand("legacy_check", "레거시 격리 확인"),
        BotCommand("deploy_status", "배포 상태"),
        BotCommand("update_bot", "GitHub 파일 즉시 반영"),
        BotCommand("admin_check", "서버 점검"),
        BotCommand("batch", "여러 명령 묶음 실행"),
        BotCommand("auto_on", "자동매매 스위치 ON 보호모드"),
        BotCommand("auto_off", "자동매매 스위치 OFF"),
    ]
    try:
        updater.bot.set_my_commands(commands)
    except Exception as e:
        log_error("set_commands", e)


def send_startup_message():
    txt = (
        f"✅ 봇 시작 완료\n"
        f"현재 버전: {BOT_VERSION}\n\n"
        f"이번 핵심 변경\n"
        f"- v2.6.x 레거시/브릿지/긴 복구출력 기본 실행 제거\n"
        f"- 시장자료→중앙허브→후보분류→후보판 clean core 재구성\n"
        f"- /buy_check는 복기용/C등급을 🟢 후보로 올리지 않음\n"
        f"- 실거래 주문은 보호모드. 후보판/상태판 검증 후 재연결\n\n"
        f"확인: /batch\n/ping\n/system_check\n/buy_check\n/speed_check\n/legacy_check"
    )
    try:
        send_text(CHAT_ID, txt)
    except Exception as e:
        log_error("startup_message", e)


def main():
    load_runtime_settings()
    with STATE.lock:
        STATE.stage = "booting"
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    # 특수 명령
    dp.add_handler(CommandHandler("update_bot", update_bot_handler))
    dp.add_handler(CommandHandler("auto_on", auto_on_handler))
    dp.add_handler(CommandHandler("auto_off", auto_off_handler))
    dp.add_handler(CommandHandler("batch", batch_handler))

    for cmd in ["ping", "system_check", "error_doctor", "speed_check", "buy_check", "signal", "data_check", "auto_status", "real_flow", "real_stats", "deploy_status", "admin_check", "server_check", "legacy_check"]:
        dp.add_handler(CommandHandler(cmd.lstrip("/"), simple_command_handler))

    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), multiline_message_handler))

    set_commands(updater)
    # 명령 수신을 먼저 열고, 시장 worker는 별도 스레드에서 시작
    updater.start_polling(drop_pending_updates=True)
    send_startup_message()

    threading.Thread(target=market_worker, daemon=True, name="market_worker_v270").start()
    threading.Thread(target=websocket_worker, args=("price",), daemon=True, name="ws_price_hub_v274").start()
    threading.Thread(target=websocket_worker, args=("trade",), daemon=True, name="ws_trade_hub_v274").start()
    threading.Thread(target=light_state_save_worker, daemon=True, name="state_save_worker_v270").start()

    with STATE.lock:
        STATE.stage = "running"
        STATE.main_loop_ts = now_ts()
    try:
        updater.idle()
    finally:
        with STATE.lock:
            STATE.shutdown = True


if __name__ == "__main__":
    main()
