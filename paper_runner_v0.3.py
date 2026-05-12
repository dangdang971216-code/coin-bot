#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_runner_v0.3.py

큰 봇(brain)이 만든 candidate_events.jsonl을 읽어 모의매매만 실행하는 독립 runner.
- 시장 전체 스캔 금지
- 후보 판단/전략 조건 판단 금지
- OPEN 중인 코인 현재가만 확인
- 나중 real_runner로 옮길 때 Broker만 바꾸는 구조

권장 구조:
    Brain bot  -> candidate_events.jsonl
    Paper bot  -> paper_runner_open.json / paper_runner_closed.jsonl / paper_runner_status.json
    Brain bot  -> paper 결과 읽기

실행 예:
    python3 paper_runner_v0.3.py
    python3 paper_runner_v0.3.py --once
    python3 paper_runner_v0.3.py --status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

RUNNER_VERSION = "paper_runner_v0.3"

try:
    import pybithumb  # type: ignore
except Exception:  # pragma: no cover
    pybithumb = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CANDIDATE_EVENTS_FILE = os.getenv("CANDIDATE_EVENTS_FILE", os.path.join(BASE_DIR, "candidate_events.jsonl"))
OPEN_FILE = os.getenv("PAPER_RUNNER_OPEN_FILE", os.path.join(BASE_DIR, "paper_runner_open.json"))
CLOSED_FILE = os.getenv("PAPER_RUNNER_CLOSED_FILE", os.path.join(BASE_DIR, "paper_runner_closed.jsonl"))
STATE_FILE = os.getenv("PAPER_RUNNER_STATE_FILE", os.path.join(BASE_DIR, "paper_runner_state.json"))
STATUS_FILE = os.getenv("PAPER_RUNNER_STATUS_FILE", os.path.join(BASE_DIR, "paper_runner_status.json"))
ERROR_FILE = os.getenv("PAPER_RUNNER_ERROR_FILE", os.path.join(BASE_DIR, "paper_runner_error.log"))
FLAG_FILE = os.getenv("EXTERNAL_PAPER_FLAG_FILE", os.path.join(BASE_DIR, "external_paper_runner_on.flag"))
LOCK_FILE = os.getenv("PAPER_RUNNER_LOCK_FILE", os.path.join(BASE_DIR, "paper_runner.lock"))

POLL_SEC = float(os.getenv("PAPER_RUNNER_POLL_SEC", "3"))
MAX_STRICT_OPEN = int(os.getenv("PAPER_RUNNER_MAX_STRICT_OPEN", "12"))
MAX_SHADOW_OPEN = int(os.getenv("PAPER_RUNNER_MAX_SHADOW_OPEN", "20"))
ENTRY_KRW = float(os.getenv("PAPER_RUNNER_ENTRY_KRW", "10000"))
FEE_PCT = float(os.getenv("PAPER_RUNNER_FEE_PCT", "0.08"))  # 왕복 보수 추정
SLIPPAGE_PCT = float(os.getenv("PAPER_RUNNER_SLIPPAGE_PCT", "0.05"))
STOP_LOSS_PCT = float(os.getenv("PAPER_RUNNER_STOP_LOSS_PCT", "-1.70"))
TAKE_PROFIT_PCT = float(os.getenv("PAPER_RUNNER_TAKE_PROFIT_PCT", "4.60"))
TRAIL_START_PCT = float(os.getenv("PAPER_RUNNER_TRAIL_START_PCT", "2.80"))
TRAIL_BACKOFF_PCT = float(os.getenv("PAPER_RUNNER_TRAIL_BACKOFF_PCT", "1.15"))
BREAKEVEN_TRIGGER_PCT = float(os.getenv("PAPER_RUNNER_BREAKEVEN_TRIGGER_PCT", "1.70"))
BREAKEVEN_BUFFER_PCT = float(os.getenv("PAPER_RUNNER_BREAKEVEN_BUFFER_PCT", "0.15"))
TIME_EXIT_SEC = int(os.getenv("PAPER_RUNNER_TIME_EXIT_SEC", "1800"))
NO_PROGRESS_SEC = int(os.getenv("PAPER_RUNNER_NO_PROGRESS_SEC", "600"))
NO_PROGRESS_BEST_PCT = float(os.getenv("PAPER_RUNNER_NO_PROGRESS_BEST_PCT", "0.15"))
TICKER_COOLDOWN_SEC = int(os.getenv("PAPER_RUNNER_TICKER_COOLDOWN_SEC", "900"))
EVENT_MAX_AGE_SEC = int(os.getenv("PAPER_RUNNER_EVENT_MAX_AGE_SEC", "1200"))
MAX_EVENTS_PER_LOOP = int(os.getenv("PAPER_RUNNER_MAX_EVENTS_PER_LOOP", "300"))
PRICE_ERROR_SLEEP_SEC = float(os.getenv("PAPER_RUNNER_PRICE_ERROR_SLEEP_SEC", "0.15"))

# strict = 나중 실제매매 후보와 최대한 비슷한 lane
# shadow = 차단/폐기 후보도 모의로 추적해 “막은 게 맞았는지” 확인하는 lane
ALLOW_SHADOW = os.getenv("PAPER_RUNNER_ALLOW_SHADOW", "1").strip().lower() in {"1", "true", "yes", "on"}
ALLOW_BLOCKED_AS_STRICT = os.getenv("PAPER_RUNNER_ALLOW_BLOCKED_AS_STRICT", "0").strip().lower() in {"1", "true", "yes", "on"}
SET_EXTERNAL_FLAG = os.getenv("PAPER_RUNNER_SET_EXTERNAL_FLAG", "1").strip().lower() in {"1", "true", "yes", "on"}

STRICT_DECISIONS = {
    "paper_ready", "paper", "buy_ready", "buyready", "alert_sent", "alert_eligible",
    "eligible", "user_alert", "candidate", "watch_ready",
}
BLOCKED_DECISIONS = {
    "quality_blocked", "blocked", "discard", "reject", "rejected", "grade_c", "grade_c/discard",
}
BAD_WORDS = ("blocked", "discard", "reject", "rejected", "quality_blocked")
GOOD_GRADES = {"A", "S"}


def ts() -> float:
    return time.time()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def atomic_save_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path), suffix=".tmp", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def load_json(path: str, default: Any) -> Any:
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def append_jsonl(path: str, row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def log_error(tag: str, exc: BaseException, extra: Optional[Dict[str, Any]] = None) -> None:
    try:
        body = {
            "ts": ts(),
            "time": now_text(),
            "tag": tag,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "extra": extra or {},
            "traceback": traceback.format_exc(),
        }
        append_jsonl(ERROR_FILE, body)
    except Exception:
        pass


def normalize_ticker(value: Any) -> str:
    return str(value or "").upper().strip().replace("KRW-", "")


def make_event_id(row: Dict[str, Any]) -> str:
    raw = str(row.get("event_id") or row.get("id") or "").strip()
    if raw:
        return raw
    parts = [
        normalize_ticker(row.get("ticker")),
        str(row.get("created_at") or row.get("detected_ts") or row.get("ts") or ""),
        str(row.get("decision") or row.get("event_type") or row.get("quality_category") or ""),
        str(row.get("score") or row.get("edge") or ""),
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8", errors="ignore")).hexdigest()[:16]
    return "evt_" + digest


def row_created_ts(row: Dict[str, Any]) -> float:
    for key in ("created_at", "detected_ts", "ts", "time", "event_ts"):
        v = safe_float(row.get(key), 0.0)
        if v > 0:
            # ms timestamp guard
            if v > 10_000_000_000:
                return v / 1000.0
            return v
    return ts()


def lower_text(*values: Any) -> str:
    return " ".join(str(v or "") for v in values).lower()


def event_decision(row: Dict[str, Any]) -> str:
    for key in ("decision", "event_type", "quality_category", "status", "paper_decision"):
        value = str(row.get(key) or "").strip()
        if value:
            return value.lower()
    return ""


def explicit_bool(row: Dict[str, Any], key: str) -> Optional[bool]:
    if key not in row:
        return None
    v = row.get(key)
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n"}:
        return False
    return None


def detected_price(row: Dict[str, Any]) -> float:
    for key in ("detected_price", "entry_price", "price", "current_price", "last_price"):
        p = safe_float(row.get(key), 0.0)
        if p > 0:
            return p
    return 0.0


def classify_lane(row: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (lane, reason). lane strict/shadow/None."""
    ticker = normalize_ticker(row.get("ticker"))
    if not ticker:
        return None, "no_ticker"
    if detected_price(row) <= 0:
        return None, "no_price"

    explicit_paper = explicit_bool(row, "eligible_for_paper")
    explicit_real = explicit_bool(row, "eligible_for_real")
    decision = event_decision(row)
    grade = str(row.get("alert_grade") or row.get("grade") or "").upper().strip()
    text = lower_text(decision, row.get("reason"), row.get("block_reason"), row.get("quality_category"))
    is_blocked = decision in BLOCKED_DECISIONS or any(w in text for w in BAD_WORDS)

    if explicit_paper is True:
        return "strict", "explicit_eligible_for_paper"
    if explicit_paper is False:
        if ALLOW_SHADOW:
            return "shadow", "explicit_not_paper_shadow"
        return None, "explicit_not_paper"
    if ALLOW_BLOCKED_AS_STRICT and is_blocked:
        return "strict", "blocked_allowed_as_strict"
    if decision in STRICT_DECISIONS and not is_blocked:
        return "strict", f"decision_{decision}"
    if explicit_real is True and not is_blocked:
        return "strict", "eligible_for_real"
    if grade in GOOD_GRADES and not is_blocked:
        return "strict", f"grade_{grade}"
    if ALLOW_SHADOW:
        return "shadow", "shadow_observation"
    return None, "not_eligible"


def read_new_events(path: str, state: Dict[str, Any], max_events: int = MAX_EVENTS_PER_LOOP) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    offset = safe_int(state.get("source_offset"), 0)
    size = os.path.getsize(path)
    if offset < 0 or offset > size:
        offset = 0
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(offset)
        for line in f:
            if len(rows) >= max_events:
                break
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
        state["source_offset"] = f.tell()
    state["source_size"] = size
    return rows


class PriceProvider:
    def get(self, ticker: str) -> Tuple[float, str]:
        ticker = normalize_ticker(ticker)
        if not ticker:
            return 0.0, "no_ticker"
        if pybithumb is None:
            return 0.0, "pybithumb_missing"
        try:
            price = pybithumb.get_current_price(ticker)
            if isinstance(price, dict):
                price = price.get("closing_price") or price.get("price") or price.get("trade_price")
            value = safe_float(price, 0.0)
            if value > 0:
                return value, "pybithumb_current_price"
            return 0.0, "empty_price"
        except Exception as exc:
            log_error("price", exc, {"ticker": ticker})
            time.sleep(PRICE_ERROR_SLEEP_SEC)
            return 0.0, "price_error"


@dataclass
class PaperPosition:
    event_id: str
    ticker: str
    lane: str
    status: str
    entry_ts: float
    entry_price: float
    detected_price: float
    last_price: float
    amount_krw: float
    best_pct: float
    worst_pct: float
    strategy: str
    section: str
    alert_grade: str
    decision: str
    open_reason: str
    source: str
    raw_event: Dict[str, Any]


class PaperBroker:
    def __init__(self, price_provider: PriceProvider) -> None:
        self.price_provider = price_provider

    def buy(self, row: Dict[str, Any], lane: str, open_reason: str) -> Tuple[Optional[Dict[str, Any]], str]:
        ticker = normalize_ticker(row.get("ticker"))
        fallback = detected_price(row)
        live_price, source = self.price_provider.get(ticker)
        if live_price <= 0:
            live_price = fallback
            source = "detected_price_fallback"
        if live_price <= 0:
            return None, "no_entry_price"
        entry = live_price * (1.0 + SLIPPAGE_PCT / 100.0)
        position = PaperPosition(
            event_id=make_event_id(row),
            ticker=ticker,
            lane=lane,
            status="OPEN",
            entry_ts=ts(),
            entry_price=entry,
            detected_price=fallback,
            last_price=live_price,
            amount_krw=ENTRY_KRW,
            best_pct=0.0,
            worst_pct=0.0,
            strategy=str(row.get("strategy") or row.get("route") or ""),
            section=str(row.get("section") or row.get("alert_bucket") or ""),
            alert_grade=str(row.get("alert_grade") or row.get("grade") or ""),
            decision=event_decision(row),
            open_reason=open_reason,
            source=str(row.get("source") or row.get("brain_version") or "brain"),
            raw_event=row,
        )
        result = asdict(position)
        result["entry_price_source"] = source
        result["runner"] = RUNNER_VERSION
        return result, "opened"


def calc_pnl(entry_price: float, current_price: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    gross = (current_price - entry_price) / entry_price * 100.0
    return gross - FEE_PCT


def close_reason(pos: Dict[str, Any], pnl: float, held: float) -> str:
    best = safe_float(pos.get("best_pct"), 0.0)
    if pnl <= STOP_LOSS_PCT:
        return "stop_loss"
    if best >= TRAIL_START_PCT and pnl <= best - TRAIL_BACKOFF_PCT:
        return "protect_stop_after_tp"
    if best >= BREAKEVEN_TRIGGER_PCT and pnl <= BREAKEVEN_BUFFER_PCT:
        return "breakeven_stop"
    if pnl >= TAKE_PROFIT_PCT:
        return "take_profit"
    if held >= NO_PROGRESS_SEC and best < NO_PROGRESS_BEST_PCT and pnl <= 0:
        return "slow_no_progress"
    if held >= TIME_EXIT_SEC:
        return "time_exit"
    return ""


def position_key(ticker: str, lane: str) -> str:
    return f"{lane}:{normalize_ticker(ticker)}"


class PaperRunner:
    def __init__(self) -> None:
        self.price_provider = PriceProvider()
        self.broker = PaperBroker(self.price_provider)
        self.open_positions: Dict[str, Dict[str, Any]] = load_json(OPEN_FILE, {})
        self.state: Dict[str, Any] = load_json(STATE_FILE, {})
        self.stop_requested = False
        self.loop_stats: Dict[str, Any] = {}
        self.last_error = ""
        self._normalize_state()

    def _normalize_state(self) -> None:
        self.state.setdefault("seen_event_ids", [])
        self.state.setdefault("ticker_last_open_ts", {})
        self.state.setdefault("counters", {})
        self.state.setdefault("source_offset", 0)
        self.state.setdefault("started_ts", ts())
        self.seen_ids = set(str(x) for x in self.state.get("seen_event_ids", [])[-20000:])

    def request_stop(self, *_args: Any) -> None:
        self.stop_requested = True

    def count_open_lane(self, lane: str) -> int:
        return sum(1 for p in self.open_positions.values() if str(p.get("lane")) == lane)

    def inc(self, key: str, amount: int = 1) -> None:
        counters = self.state.setdefault("counters", {})
        counters[key] = safe_int(counters.get(key), 0) + amount

    def ticker_cooldown_ok(self, ticker: str, lane: str) -> bool:
        last_map = self.state.setdefault("ticker_last_open_ts", {})
        last = safe_float(last_map.get(position_key(ticker, lane)), 0.0)
        return ts() - last >= TICKER_COOLDOWN_SEC

    def mark_ticker_open(self, ticker: str, lane: str) -> None:
        self.state.setdefault("ticker_last_open_ts", {})[position_key(ticker, lane)] = ts()

    def process_events(self) -> None:
        started = ts()
        events = read_new_events(CANDIDATE_EVENTS_FILE, self.state)
        opened = 0
        skipped: Dict[str, int] = {}
        for row in events:
            eid = make_event_id(row)
            if eid in self.seen_ids:
                skipped["duplicate_event"] = skipped.get("duplicate_event", 0) + 1
                continue
            self.seen_ids.add(eid)
            event_age = ts() - row_created_ts(row)
            if event_age > EVENT_MAX_AGE_SEC:
                skipped["old_event"] = skipped.get("old_event", 0) + 1
                continue
            lane, reason = classify_lane(row)
            if lane is None:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            ticker = normalize_ticker(row.get("ticker"))
            max_open = MAX_STRICT_OPEN if lane == "strict" else MAX_SHADOW_OPEN
            if self.count_open_lane(lane) >= max_open:
                skipped[f"{lane}_max_open"] = skipped.get(f"{lane}_max_open", 0) + 1
                continue
            key = position_key(ticker, lane)
            if key in self.open_positions:
                skipped["already_open"] = skipped.get("already_open", 0) + 1
                continue
            if not self.ticker_cooldown_ok(ticker, lane):
                skipped["cooldown"] = skipped.get("cooldown", 0) + 1
                continue
            position, open_status = self.broker.buy(row, lane, reason)
            if position is None:
                skipped[open_status] = skipped.get(open_status, 0) + 1
                continue
            self.open_positions[key] = position
            self.mark_ticker_open(ticker, lane)
            self.inc(f"opened_{lane}")
            opened += 1
        self.loop_stats["events_read"] = len(events)
        self.loop_stats["events_opened"] = opened
        self.loop_stats["events_skipped"] = skipped
        self.loop_stats["process_events_sec"] = round(ts() - started, 4)

    def update_positions(self) -> None:
        started = ts()
        closed_count = 0
        price_missing = 0
        for key, pos in list(self.open_positions.items()):
            ticker = normalize_ticker(pos.get("ticker"))
            price, price_source = self.price_provider.get(ticker)
            if price <= 0:
                price_missing += 1
                pos["last_price_source"] = price_source
                continue
            pos["last_price"] = price
            pos["last_price_source"] = price_source
            pos["last_update_ts"] = ts()
            pnl = calc_pnl(safe_float(pos.get("entry_price"), 0.0), price)
            pos["current_pct"] = pnl
            pos["best_pct"] = max(safe_float(pos.get("best_pct"), 0.0), pnl)
            pos["worst_pct"] = min(safe_float(pos.get("worst_pct"), 0.0), pnl)
            held = ts() - safe_float(pos.get("entry_ts"), ts())
            reason = close_reason(pos, pnl, held)
            if not reason:
                self.open_positions[key] = pos
                continue
            closed = dict(pos)
            closed.update({
                "status": "CLOSED",
                "close_ts": ts(),
                "close_time": now_text(),
                "close_price": price,
                "result_pct": pnl,
                "pnl_pct": pnl,
                "close_reason": reason,
                "held_sec": int(held),
                "runner": RUNNER_VERSION,
            })
            append_jsonl(CLOSED_FILE, closed)
            self.open_positions.pop(key, None)
            self.inc(f"closed_{reason}")
            self.inc("closed_total")
            closed_count += 1
        self.loop_stats["positions_closed"] = closed_count
        self.loop_stats["price_missing"] = price_missing
        self.loop_stats["update_positions_sec"] = round(ts() - started, 4)

    def persist(self) -> None:
        self.state["seen_event_ids"] = list(self.seen_ids)[-20000:]
        self.state["last_loop_ts"] = ts()
        self.state["last_loop_time"] = now_text()
        self.state["runner_version"] = RUNNER_VERSION
        atomic_save_json(OPEN_FILE, self.open_positions)
        atomic_save_json(STATE_FILE, self.state)
        status = self.build_status()
        atomic_save_json(STATUS_FILE, status)

    def build_status(self) -> Dict[str, Any]:
        strict_open = self.count_open_lane("strict")
        shadow_open = self.count_open_lane("shadow")
        return {
            "runner_version": RUNNER_VERSION,
            "time": now_text(),
            "ts": ts(),
            "source_file": CANDIDATE_EVENTS_FILE,
            "open_file": OPEN_FILE,
            "closed_file": CLOSED_FILE,
            "status_file": STATUS_FILE,
            "flag_file": FLAG_FILE,
            "external_flag_on": os.path.exists(FLAG_FILE),
            "open_total": len(self.open_positions),
            "open_strict": strict_open,
            "open_shadow": shadow_open,
            "closed_total": safe_int(self.state.get("counters", {}).get("closed_total"), 0),
            "counters": self.state.get("counters", {}),
            "last_loop": self.loop_stats,
            "last_error": self.last_error,
            "settings": {
                "max_strict_open": MAX_STRICT_OPEN,
                "max_shadow_open": MAX_SHADOW_OPEN,
                "entry_krw": ENTRY_KRW,
                "allow_shadow": ALLOW_SHADOW,
                "allow_blocked_as_strict": ALLOW_BLOCKED_AS_STRICT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "time_exit_sec": TIME_EXIT_SEC,
                "no_progress_sec": NO_PROGRESS_SEC,
            },
        }

    def set_flag(self, on: bool) -> None:
        if not SET_EXTERNAL_FLAG:
            return
        if on:
            with open(FLAG_FILE, "w", encoding="utf-8") as f:
                f.write(f"{RUNNER_VERSION} on {now_text()}\n")
        else:
            try:
                os.remove(FLAG_FILE)
            except FileNotFoundError:
                pass

    def loop_once(self) -> None:
        loop_started = ts()
        self.loop_stats = {}
        self.update_positions()
        self.process_events()
        self.loop_stats["loop_sec"] = round(ts() - loop_started, 4)
        self.persist()

    def run(self, once: bool = False) -> None:
        self.set_flag(True)
        print(f"{RUNNER_VERSION} start / source={CANDIDATE_EVENTS_FILE}", flush=True)
        try:
            while not self.stop_requested:
                try:
                    self.loop_once()
                    status = self.build_status()
                    print(
                        f"{RUNNER_VERSION} open={status['open_total']} "
                        f"strict={status['open_strict']} shadow={status['open_shadow']} "
                        f"read={self.loop_stats.get('events_read', 0)} opened={self.loop_stats.get('events_opened', 0)} "
                        f"closed={self.loop_stats.get('positions_closed', 0)} sec={self.loop_stats.get('loop_sec', 0)}",
                        flush=True,
                    )
                except Exception as exc:
                    self.last_error = f"{exc.__class__.__name__}: {exc}"
                    log_error("loop", exc)
                    self.persist()
                if once:
                    break
                time.sleep(POLL_SEC)
        finally:
            self.persist()
            self.set_flag(False)
            print(f"{RUNNER_VERSION} stop", flush=True)


class RunnerLock:
    def __init__(self, path: str) -> None:
        self.path = path
        self.acquired = False

    def __enter__(self) -> "RunnerLock":
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    old = f.read().strip()
                # If old process is still alive, refuse duplicate run.
                old_pid = safe_int(old.split()[0], 0) if old else 0
                if old_pid > 0:
                    try:
                        os.kill(old_pid, 0)
                        raise RuntimeError(f"paper_runner already running pid={old_pid}")
                    except ProcessLookupError:
                        pass
            except RuntimeError:
                raise
            except Exception:
                pass
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {now_text()}\n")
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self.acquired:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass


def print_status() -> None:
    status = load_json(STATUS_FILE, {})
    if not status:
        print(json.dumps({"runner_version": RUNNER_VERSION, "status": "no_status_file", "status_file": STATUS_FILE}, ensure_ascii=False, indent=2))
        return
    print(json.dumps(status, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone paper runner for coinbot candidate events")
    parser.add_argument("--once", action="store_true", help="run one loop and exit")
    parser.add_argument("--status", action="store_true", help="print status json and exit")
    parser.add_argument("--no-lock", action="store_true", help="skip lock file check")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    runner = PaperRunner()
    signal.signal(signal.SIGTERM, runner.request_stop)
    signal.signal(signal.SIGINT, runner.request_stop)

    if args.no_lock:
        runner.run(once=args.once)
    else:
        with RunnerLock(LOCK_FILE):
            runner.run(once=args.once)


if __name__ == "__main__":
    main()
