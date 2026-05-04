
# -*- coding: utf-8 -*-
"""
수익형 v2.7.1 clean_core_pullback

목표:
- v2.6.x 레거시/브릿지/복구 출력 찌꺼기를 기본 실행에서 제거
- 지금 쓰는 실사용 경로만 새 뼈대로 재구성
- 자동매매 실주문은 보호모드로 막고, 후보판/상태판 먼저 검증

주의:
- 이 파일은 v2.6.x의 거대한 함수 덩어리를 그대로 복사하지 않는다.
- 시장자료 -> 중앙허브 -> 후보분류 -> 후보판/상태판까지만 가볍게 운영한다.
- 실거래 주문은 v2.7.2 이후 재연결 전까지 실행하지 않는다.
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
BOT_VERSION = "수익형 v2.7.1 clean_core_pullback"
BOT_FILE_TAG = "수익형_v2.7.1_clean_core_pullback.py"
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


def compute_row(ticker: str, price: float, money: float, ts: float, hist: deque) -> MarketRow:
    hist_span = max(0.0, ts - hist[0][0]) if hist else 0.0
    hist_points = len(hist)
    old1 = nearest_old_price(hist, 60)
    old3 = nearest_old_price(hist, 180)
    old5 = nearest_old_price(hist, 300)
    old15 = nearest_old_price(hist, 900)
    h30, l30 = high_low_in_window(hist, 1800)
    ch1 = pct(price, old1)
    ch3 = pct(price, old3)
    ch5 = pct(price, old5)
    ch15 = pct(price, old15)
    from_low = pct(price, l30) if l30 else 0.0
    high_gap = max(0.0, pct(h30, price)) if h30 and price else 999.0
    money_score = 0.0
    if money >= 50_000_000_000:
        money_score = 1.2
    elif money >= 10_000_000_000:
        money_score = 0.9
    elif money >= 3_000_000_000:
        money_score = 0.6
    elif money >= 500_000_000:
        money_score = 0.3

    # v2.7.1 점수: 단순 급등보다 눌림 재확인/저점방어/돈 유지에 가중.
    raw = (ch1 * 1.7) + (ch3 * 1.8) + (ch5 * 0.9) + money_score
    if 0.15 <= from_low <= 1.45:
        raw += 0.35  # 저점에서 너무 멀지 않게 회복한 자리
    if ch1 >= 0 and ch3 >= 0.12:
        raw += 0.25  # 1분이 밀리지 않고 3분 회복
    if high_gap < 0.12 and from_low > 1.65:
        raw -= 1.15  # 최근 고점 바로 근처에서 이미 많이 뜬 후보는 추격 위험
    if ch1 < -0.15 and ch3 > 0.35:
        raw -= 0.45  # 3분은 강하지만 현재 1분이 밀리는 후보
    if hist_span < 75:
        raw -= 0.8  # 부팅 직후/자료 부족 후보 초록 방지

    return MarketRow(
        ticker=ticker, price=price, money24h=money, ch1=ch1, ch3=ch3, ch5=ch5, ch15=ch15,
        high30=h30, low30=l30, from_low30=from_low, high_gap30=high_gap,
        hist_span_sec=hist_span, hist_points=hist_points, score=raw, updated_ts=ts
    )


def make_candidate(row: MarketRow, status: str, label: str, icon: str, reason: str, *, pattern: str = "", caution: str = "", entry_note: str = "", reject_reason: str = "") -> Candidate:
    stop = row.low30 * 0.995 if row.low30 else row.price * 0.985
    return Candidate(
        ticker=row.ticker, status=status, label=label, icon=icon, reason=reason,
        price=row.price, score=row.score, ch1=row.ch1, ch3=row.ch3, ch5=row.ch5,
        money24h=row.money24h, from_low30=row.from_low30, high_gap30=row.high_gap30,
        stop_price=stop, entry_note=entry_note, reject_reason=reject_reason,
        updated_ts=row.updated_ts, pattern=pattern, caution=caution,
    )


def classify_candidate(row: MarketRow) -> Candidate:
    """
    v2.7.1 눌림 라인 복구판.
    핵심: 후보 개수 제한이 아니라 코인 상태로만 🟢/🟡/⚪/🔴 분류한다.
    - 복기용/legacy/paper C등급은 애초에 입력으로 쓰지 않는다.
    - 단순 1분 급등보다 저점방어 + 1분/3분 회복 + 돈 유지가 중요하다.
    """
    stale = now_ts() - row.updated_ts > 45
    data_1m_ok = row.hist_span_sec >= 55
    data_3m_ok = row.hist_span_sec >= 150
    data_5m_ok = row.hist_span_sec >= 260
    money_ok = row.money24h >= 500_000_000
    money_strong = row.money24h >= 3_000_000_000

    if stale:
        return make_candidate(row, "WATCH", "자료확인", "⚪", "자료가 오래되어 감시만", pattern="DATA_STALE", entry_note="자료 갱신 후 재확인", reject_reason="자료 오래됨")

    if not data_1m_ok:
        return make_candidate(row, "WATCH", "자료쌓는중", "⚪", "부팅 직후라 1분 기준값 부족", pattern="WARMUP", entry_note="1분 이상 자료가 쌓인 뒤 재평가", reject_reason="자료 부족")

    # 3분/5분 자료가 아직 부족하면 초록 금지. 단, 움직임과 돈은 감시/대기로 남긴다.
    if not data_3m_ok:
        if money_ok and row.ch1 >= 0.20:
            return make_candidate(row, "PENDING", "초기재확인", "🟡", "1분 움직임은 있으나 3분 자료가 아직 부족", pattern="EARLY_RECHECK", caution="초기자료", entry_note="3분 자료가 쌓이면 재평가", reject_reason="3분 기준 부족")
        if row.ch1 > 0.05:
            return make_candidate(row, "WATCH", "초기감시", "⚪", "움직임은 있으나 자료가 아직 부족", pattern="EARLY_WATCH", caution="초기자료", entry_note="자료 누적 후 재평가", reject_reason="자료 부족")

    # 위험/대기 분리
    one_min_pullback = row.ch1 < -0.12 and row.ch3 >= 0.25
    too_extended = row.from_low30 >= 2.0 and row.high_gap30 < 0.20
    high_break_hold = row.high_gap30 < 0.12 and row.ch1 >= 0.22 and row.ch3 >= 0.30 and row.from_low30 <= 1.65
    pullback_reconfirm = (
        money_ok
        and row.ch1 >= 0.03
        and row.ch3 >= 0.12
        and row.score >= 0.85
        and 0.08 <= row.from_low30 <= 1.65
        and not too_extended
    )
    restart_early = (
        money_strong
        and row.ch1 >= 0.00
        and row.ch3 >= 0.18
        and row.ch5 >= 0.18
        and row.from_low30 <= 1.35
        and not too_extended
    )
    real_strength_early = (
        money_ok
        and row.ch1 >= 0.28
        and row.ch3 >= 0.28
        and row.from_low30 <= 1.55
        and not too_extended
    )

    if pullback_reconfirm or restart_early or real_strength_early or high_break_hold:
        pattern = "PULLBACK_RECONFIRM" if pullback_reconfirm else "RESTART_EARLY" if restart_early else "REAL_STRENGTH_EARLY" if real_strength_early else "BREAK_HOLD"
        caution = "고점 돌파 유지 확인" if row.high_gap30 < 0.12 else ""
        reason = "눌림 재확인 통과" if pattern == "PULLBACK_RECONFIRM" else "재출발/초입 강세" if pattern in ("RESTART_EARLY", "REAL_STRENGTH_EARLY") else "고점 재돌파 후 유지 확인"
        return make_candidate(row, "BUY_READY", "진입가능 검토", "🟢", reason, pattern=pattern, caution=caution, entry_note="실거래 전 호가/슬리피지 재검사 필요")

    # 노랑: 후보는 맞지만 아직 재확인 전. 좋은 후보를 숫자로 자르지 않고 상태로만 보류한다.
    if money_ok and (row.ch3 >= 0.10 or row.ch5 >= 0.12 or row.from_low30 <= 0.85 or one_min_pullback):
        reasons = []
        if one_min_pullback:
            reasons.append("3분은 강하지만 1분이 밀림")
        if too_extended:
            reasons.append("저점대비 많이 떠서 추격 확인 필요")
        if row.high_gap30 < 0.12:
            reasons.append("고점 바로 근처라 유지 확인 필요")
        if row.ch1 < 0.03:
            reasons.append("1분 회복 부족")
        if not data_5m_ok:
            reasons.append("5분 기준 아직 부족")
        reason = " / ".join(reasons) if reasons else "돈은 있으나 재확인 전"
        return make_candidate(row, "PENDING", "눌림대기", "🟡", reason, pattern="PULLBACK_WAIT", caution="재확인 필요", entry_note="1분 회복·저점방어·고점추격 여부 확인", reject_reason="아직 PENDING")

    # 감시: 움직임은 있지만 매수 근거 부족, 돈 부족, 반짝/초저유동성 등
    if row.score >= 0.15 or row.ch1 > 0.05 or row.ch3 > 0.08:
        reason = "움직임은 있으나 매수후보 부족"
        if not money_ok:
            reason = "움직임은 있으나 돈/유동성 부족"
        elif too_extended:
            reason = "이미 많이 떠서 추격 위험"
        return make_candidate(row, "WATCH", "감시만", "⚪", reason, pattern="WATCH", entry_note="강해지면 재평가", reject_reason="매수 근거 부족")

    return make_candidate(row, "REJECT", "버림", "🔴", "점수/움직임 부족", pattern="REJECT", reject_reason="점수 낮음")


def rebuild_hubs(raw: dict[str, dict]):
    ts = now_ts()
    rows: dict[str, MarketRow] = {}
    with STATE.lock:
        for ticker, item in raw.items():
            price = float(item["price"])
            money = float(item.get("money24h") or 0.0)
            hist = STATE.price_history[ticker]
            hist.append((ts, price, money))
            # 오래된 포인트 제거
            cutoff = ts - HISTORY_KEEP_SEC
            while hist and hist[0][0] < cutoff:
                hist.popleft()
            rows[ticker] = compute_row(ticker, price, money, ts, hist)

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
        f"🏭 전체 상태판 /system_check v2.7.1\n"
        f"- 한줄판정: {judgement} / 교통정리 {traffic}\n"
        f"- 자료흐름: 거래가능 추정 454 → 현재허브 {s['market_count']} → 순위 {s['rank_count']} → 후보 {s['candidate_count']}\n"
        f"- 후보판: 🟢 {s['buy_ready']} / 🟡 {s['pending']} / ⚪ {s['watch']}\n\n"
        f"1) 허브\n"
        f"- {'✅' if market_ok else '⚠️'} 시장허브: {age_text(s['market_ts'])} / {s['market_count']}개\n"
        f"- {'✅' if rank_ok else '⚠️'} 순위허브: {age_text(s['rank_ts'])} / {s['rank_count']}개\n"
        f"- {'✅' if candidate_ok else '⚠️'} 후보허브: {age_text(s['candidate_ts'])} / {s['candidate_count']}개\n"
        f"- ✅ 출력: 요청 시 조립 / 무거운 캐시 worker 없음\n\n"
        f"2) 직원\n"
        f"- 시장자료 직원: fetch OK {s['fetch_ok']} / FAIL {s['fetch_fail']}\n"
        f"- 후보분류 직원: 최신 후보 {s['candidate_count']}개\n"
        f"- 실거래 직원: 보호모드 / 실제 주문 {'허용' if s['real_order_enabled'] else '차단'}\n"
        f"- legacy 직원: 기본 실행 없음\n\n"
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
        f"🩺 오류/병목 진단 /error_doctor v2.7.1\n\n"
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
        f"- v2.7.1: 눌림 재확인 라인을 clean core 방식으로 복구\n\n"
        f"3) 최근 오류\n" + ("\n".join(f"- {e}" for e in s['errors'][:4]) if s['errors'] else "- 없음") + "\n"
    )


def render_speed_check() -> str:
    s = snapshot()
    market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok = status_flags(s)
    timings = s["cmd_timings"][:8]
    lines = [
        "⏱ 속도 체크 /speed_check v2.7.1",
        "",
        "1) worker",
        f"- 시장자료: {'✅' if market_ok else '⚠️'} {age_text(s['market_ts'])} / {s['market_count']}개",
        f"- 순위자료: {'✅' if rank_ok else '⚠️'} {age_text(s['rank_ts'])} / {s['rank_count']}개",
        f"- 후보분류: {'✅' if candidate_ok else '⚠️'} {age_text(s['candidate_ts'])} / {s['candidate_count']}개",
        "- 출력캐시: 무거운 상시 worker 없음 / 요청 시 조립",
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
        "🎯 매수 후보판 /buy_check v2.7.1",
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
            lines.append(f"- 흐름 1분 {c.ch1:+.2f}% / 3분 {c.ch3:+.2f}% / 5분 {c.ch5:+.2f}%")
            lines.append(f"- 돈 {krw(c.money24h)} / 저점대비 {c.from_low30:+.2f}% / 고점거리 {c.high_gap30:.2f}%")
            lines.append(f"- 유형: {c.pattern or c.status}")
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
    lines.append("- 🟢: 눌림 재확인/재출발/초입 강세가 상태 기준을 통과한 후보. 개수 제한 없음")
    lines.append("- 🟡: 좋은 후보일 수 있지만 1분 회복·고점유지·자료누적 재확인 전")
    lines.append("- ⚪: 움직임은 있으나 지금 매수 후보로는 약함")
    return "\n".join(lines)


def render_auto_status() -> str:
    s = snapshot()
    market_age, rank_age, cmd_age, market_ok, rank_ok, candidate_ok, output_ok = status_flags(s)
    safe = market_ok and rank_ok and candidate_ok
    return (
        f"🤖 자동매매 상태 /auto_status v2.7.1\n"
        f"- 자동매매 스위치: {'켜짐' if s['auto_enabled'] else '꺼짐'}\n"
        f"- 실거래 주문: {'허용' if s['real_order_enabled'] else '보호모드 차단'}\n"
        f"- 허용전략: 눌림 재확인 라인만 / 1회 {MAX_BUY_KRW:,}원 / 동시보유 {MAX_POSITIONS}개\n"
        f"- 자료안전: {'✅ 정상' if safe else '⚠️ 확인필요'} / 시장 {age_text(s['market_ts'])} / 순위 {age_text(s['rank_ts'])}\n"
        f"- 후보: 🟢 {s['buy_ready']} / 🟡 {s['pending']} / ⚪ {s['watch']}\n\n"
        f"중요\n"
        f"- v2.7.1은 clean core 눌림라인 검증판이라 실제 주문은 막아둠.\n"
        f"- 후보 품질 확인 후 후보판 검증 후 실거래 직원 재연결 예정.\n"
    )


def render_real_flow() -> str:
    s = snapshot()
    with STATE.lock:
        cands = list(STATE.candidates)[:8]
        events = list(STATE.recent_events)[:8]
    lines = [
        "🤖 실거래 후보 흐름 /real_flow v2.7.1",
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
        f"📊 실거래 성과 /real_stats v2.7.1\n"
        f"- v2.7.1 clean core는 실거래 주문 보호모드야.\n"
        f"- 기존 v2.6 실거래 로그를 여기서 섞어 계산하지 않음.\n"
        f"- 실거래 직원은 v2.7.2에서 체결확정/보유보호와 함께 재연결 예정.\n"
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
        f"📦 배포 상태 /deploy_status v2.7.1\n"
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
        f"🖥 서버 점검 /admin_check v2.7.1\n"
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
        f"🧹 레거시 격리 확인 /legacy_check v2.7.1\n"
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
        f"🚀 /update_bot 배포 시작 v2.7.1\n"
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
              "🟢 자동매매 스위치 ON\n- 단, v2.7.1 clean core는 실거래 주문 보호모드라 실제 주문은 아직 차단돼.\n- 후보 흐름 검증 후 후보판 검증 후 실거래 직원 재연결 예정.")


def auto_off_handler(update, context: CallbackContext):
    with STATE.lock:
        STATE.auto_enabled = False
    save_runtime_settings()
    send_text(update.effective_chat.id, "⛔ 자동매매 스위치 OFF\n- 신규 실거래 주문 없음. v2.7.1은 원래 보호모드야.")


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

    for cmd in ["ping", "system_check", "error_doctor", "speed_check", "buy_check", "signal", "auto_status", "real_flow", "real_stats", "deploy_status", "admin_check", "server_check", "legacy_check"]:
        dp.add_handler(CommandHandler(cmd.lstrip("/"), simple_command_handler))

    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), multiline_message_handler))

    set_commands(updater)
    # 명령 수신을 먼저 열고, 시장 worker는 별도 스레드에서 시작
    updater.start_polling(drop_pending_updates=True)
    send_startup_message()

    threading.Thread(target=market_worker, daemon=True, name="market_worker_v270").start()
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
