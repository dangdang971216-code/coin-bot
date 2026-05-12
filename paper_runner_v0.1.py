#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_runner_v0.1.py
- 큰 봇(brain)이 내보내는 candidate_events.jsonl을 읽어서 모의매매만 실행하는 작은 runner.
- 시장 전체 스캔 금지. 후보 파일 + OPEN 중인 코인 가격만 본다.
- 나중 real_runner로 갈 때는 PaperBroker 부분만 RealBroker로 교체하는 구조를 목표로 한다.
"""
import os
import json
import time
import math
import traceback
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional

try:
    import pybithumb
except Exception:
    pybithumb = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CANDIDATE_EVENTS_FILE = os.getenv("CANDIDATE_EVENTS_FILE", os.path.join(BASE_DIR, "candidate_events.jsonl"))
OPEN_FILE = os.getenv("PAPER_RUNNER_OPEN_FILE", os.path.join(BASE_DIR, "paper_runner_open.json"))
CLOSED_FILE = os.getenv("PAPER_RUNNER_CLOSED_FILE", os.path.join(BASE_DIR, "paper_runner_closed.jsonl"))
SEEN_FILE = os.getenv("PAPER_RUNNER_SEEN_FILE", os.path.join(BASE_DIR, "paper_runner_seen.json"))
FLAG_FILE = os.getenv("EXTERNAL_PAPER_FLAG_FILE", os.path.join(BASE_DIR, "external_paper_runner_on.flag"))

POLL_SEC = float(os.getenv("PAPER_RUNNER_POLL_SEC", "3"))
MAX_OPEN = int(os.getenv("PAPER_RUNNER_MAX_OPEN", "12"))
ENTRY_KRW = float(os.getenv("PAPER_RUNNER_ENTRY_KRW", "10000"))
FEE_PCT = float(os.getenv("PAPER_RUNNER_FEE_PCT", "0.08"))  # 왕복 보수 추정, 기본 0.08%
SLIPPAGE_PCT = float(os.getenv("PAPER_RUNNER_SLIPPAGE_PCT", "0.05"))
STOP_LOSS_PCT = float(os.getenv("PAPER_RUNNER_STOP_LOSS_PCT", "-1.70"))
TAKE_PROFIT_PCT = float(os.getenv("PAPER_RUNNER_TAKE_PROFIT_PCT", "4.60"))
TIME_EXIT_SEC = int(os.getenv("PAPER_RUNNER_TIME_EXIT_SEC", "1800"))
NO_PROGRESS_SEC = int(os.getenv("PAPER_RUNNER_NO_PROGRESS_SEC", "600"))
NO_PROGRESS_BEST_PCT = float(os.getenv("PAPER_RUNNER_NO_PROGRESS_BEST_PCT", "0.15"))
COOLDOWN_SEC = int(os.getenv("PAPER_RUNNER_TICKER_COOLDOWN_SEC", "900"))
# 기본은 brain 본선 후보 전체를 paper로 태워 데이터 확보. 원하면 B/A/S로 조일 수 있음.
MIN_GRADE = os.getenv("PAPER_RUNNER_MIN_GRADE", "ANY").upper().strip()
ALLOW_QUALITY_BLOCKED = os.getenv("PAPER_RUNNER_ALLOW_QUALITY_BLOCKED", "1").strip().lower() in {"1", "true", "yes", "on"}

GRADE_ORDER = {"C": 0, "B": 1, "A": 2, "S": 3}


def now() -> float:
    return time.time()


def load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def append_jsonl(path: str, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_new_events(path: str, seen_ids: set, max_lines: int = 5000) -> List[dict]:
    if not os.path.exists(path):
        return []
    rows: List[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-max_lines:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            eid = str(row.get("event_id") or "")
            if not eid or eid in seen_ids:
                continue
            rows.append(row)
    except Exception:
        return []
    return rows


def grade_ok(row: dict) -> bool:
    if MIN_GRADE in {"", "ANY", "ALL"}:
        return True
    g = str(row.get("alert_grade") or "").upper().strip() or "C"
    return GRADE_ORDER.get(g, 0) >= GRADE_ORDER.get(MIN_GRADE, 0)


def event_ok(row: dict, open_positions: Dict[str, dict], seen: dict) -> bool:
    ticker = str(row.get("ticker") or "").upper().strip()
    if not ticker:
        return False
    if ticker in open_positions:
        return False
    last_ts = float(seen.get("ticker_last_open_ts", {}).get(ticker, 0) or 0)
    if now() - last_ts < COOLDOWN_SEC:
        return False
    if not ALLOW_QUALITY_BLOCKED and str(row.get("event_type")) == "quality_blocked":
        return False
    if not grade_ok(row):
        return False
    price = float(row.get("detected_price") or 0)
    if price <= 0:
        return False
    return True


def get_price(ticker: str, fallback: float = 0.0) -> float:
    ticker = ticker.upper().strip()
    if not ticker:
        return fallback
    if pybithumb is None:
        return fallback
    try:
        p = pybithumb.get_current_price(ticker)
        if isinstance(p, dict):
            p = p.get("closing_price") or p.get("price") or p.get("trade_price")
        p = float(p or 0)
        return p if p > 0 else fallback
    except Exception:
        return fallback


def calc_pnl(entry_price: float, current_price: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    gross = (current_price - entry_price) / entry_price * 100.0
    return gross - FEE_PCT


def open_position(row: dict) -> dict:
    ticker = str(row.get("ticker") or "").upper().strip()
    detected = float(row.get("detected_price") or 0)
    live = get_price(ticker, detected)
    entry = live * (1.0 + SLIPPAGE_PCT / 100.0) if live > 0 else detected
    ts = now()
    return {
        "event_id": row.get("event_id"),
        "ticker": ticker,
        "status": "OPEN",
        "entry_ts": ts,
        "entry_price": entry,
        "detected_price": detected,
        "last_price": live,
        "amount_krw": ENTRY_KRW,
        "best_pct": 0.0,
        "worst_pct": 0.0,
        "strategy": row.get("strategy"),
        "section": row.get("section"),
        "alert_grade": row.get("alert_grade"),
        "quality_category": row.get("quality_category"),
        "source": row.get("source", "brain"),
        "reason": row.get("reason", ""),
        "raw_event": row,
    }


def maybe_close(pos: dict) -> Optional[dict]:
    ticker = str(pos.get("ticker") or "").upper().strip()
    fallback = float(pos.get("last_price") or pos.get("entry_price") or 0)
    price = get_price(ticker, fallback)
    if price <= 0:
        return None
    pos["last_price"] = price
    pnl = calc_pnl(float(pos.get("entry_price") or 0), price)
    pos["best_pct"] = max(float(pos.get("best_pct") or 0), pnl)
    pos["worst_pct"] = min(float(pos.get("worst_pct") or 0), pnl)
    held = now() - float(pos.get("entry_ts") or now())
    reason = ""
    if pnl <= STOP_LOSS_PCT:
        reason = "stop_loss"
    elif pnl >= TAKE_PROFIT_PCT:
        reason = "take_profit"
    elif held >= NO_PROGRESS_SEC and float(pos.get("best_pct") or 0) < NO_PROGRESS_BEST_PCT and pnl <= 0:
        reason = "slow_no_progress"
    elif held >= TIME_EXIT_SEC:
        reason = "time_exit"
    if not reason:
        return None
    closed = dict(pos)
    closed.update({
        "status": "CLOSED",
        "close_ts": now(),
        "close_price": price,
        "result_pct": pnl,
        "close_reason": reason,
        "held_sec": int(held),
        "runner": "paper_runner_v0.1",
    })
    return closed


def set_external_flag(on: bool) -> None:
    if on:
        with open(FLAG_FILE, "w", encoding="utf-8") as f:
            f.write("on\n")
    else:
        try:
            os.remove(FLAG_FILE)
        except FileNotFoundError:
            pass


def main() -> None:
    print("paper_runner_v0.1 start")
    set_external_flag(True)
    open_positions: Dict[str, dict] = load_json(OPEN_FILE, {})
    seen = load_json(SEEN_FILE, {"event_ids": [], "ticker_last_open_ts": {}})
    seen_ids = set(seen.get("event_ids", []))
    try:
        while True:
            # 1) close/update open positions
            changed = False
            for ticker, pos in list(open_positions.items()):
                closed = maybe_close(pos)
                if closed:
                    append_jsonl(CLOSED_FILE, closed)
                    open_positions.pop(ticker, None)
                    changed = True
                else:
                    open_positions[ticker] = pos
            # 2) open new candidates
            events = read_new_events(CANDIDATE_EVENTS_FILE, seen_ids)
            for row in events:
                eid = str(row.get("event_id") or "")
                if eid:
                    seen_ids.add(eid)
                if len(open_positions) >= MAX_OPEN:
                    continue
                if not event_ok(row, open_positions, seen):
                    continue
                pos = open_position(row)
                if pos.get("ticker"):
                    open_positions[pos["ticker"]] = pos
                    seen.setdefault("ticker_last_open_ts", {})[pos["ticker"]] = now()
                    changed = True
            # 3) persist
            seen["event_ids"] = list(seen_ids)[-10000:]
            save_json(SEEN_FILE, seen)
            save_json(OPEN_FILE, open_positions)
            if changed:
                print(f"open={len(open_positions)} seen={len(seen_ids)}")
            time.sleep(POLL_SEC)
    except KeyboardInterrupt:
        print("paper_runner stop")
    except Exception:
        traceback.print_exc()
    finally:
        # 안전상 runner가 꺼지면 큰 봇 내부 paper를 다시 유지하도록 flag 제거
        set_external_flag(False)


if __name__ == "__main__":
    main()
