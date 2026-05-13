#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_bot_v0.17.py

새 페이퍼봇 신축판.
- 전략 판단 없음.
- 재스캔 없음.
- candidate_events 읽기/소비/fallback 없음.
- paper_candidates.jsonl / shadow_candidates.jsonl만 소비한다.
- 기존 OPEN/CLOSED 장부는 유지한다.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "paper_bot_v0.17"
BASE_DIR = Path(__file__).resolve().parent

TOKEN = os.getenv("PAPER_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", "")).strip()
ALLOWED_CHAT_ID = os.getenv("PAPER_BOT_ALLOWED_CHAT_ID", os.getenv("CHAT_ID", "")).strip()
NOTIFY_CHAT_ID = os.getenv("PAPER_BOT_NOTIFY_CHAT_ID", ALLOWED_CHAT_ID).strip()

FILES = {
    "paper_candidates": BASE_DIR / "paper_candidates.jsonl",
    "shadow_candidates": BASE_DIR / "shadow_candidates.jsonl",
    "open": BASE_DIR / "paper_bot_open.json",
    "closed": BASE_DIR / "paper_bot_closed.jsonl",
    "status": BASE_DIR / "paper_bot_status.json",
    "control": BASE_DIR / "paper_bot_control.json",
    "error": BASE_DIR / "paper_bot_error.log",
    "log": BASE_DIR / "paper_bot.log",
    "pid": BASE_DIR / "paper_bot.pid",
    "flag": BASE_DIR / "external_paper_bot_on.flag",
    "legacy_flag": BASE_DIR / "external_paper_runner_on.flag",
}

DEFAULT_CONTROL = {
    "running": False,
    "loop_seconds": 8,
    "max_open_strict": 80,
    "max_open_shadow": 120,
    "max_new_per_cycle": 24,
    "notify_on_strict_open": True,
    "notify_on_strict_close": True,
    "notify_on_shadow": False,
    "fee_pct_roundtrip": 0.10,
    "take_profit_pct": 1.20,
    "protect_trigger_pct": 0.90,
    "protect_floor_pct": 0.20,
    "stop_loss_pct": -1.20,
    "slow_minutes": 20,
    "slow_peak_under_pct": 0.25,
    "time_exit_minutes": 120,
    "block_same_ticker_open": True,
}

# 구 control 파일에 남아 있을 수 있는 candidate_events 옵션은 v0.17에서 강제 무시한다.
REMOVED_CONTROL_KEYS = {"allow_shadow_from_candidate_events"}

_stop_event = threading.Event()
_state_lock = threading.RLock()
_update_offset = 0
_bad_markets: set[str] = set()
_symbols_cache: Dict[str, Any] = {"ts": 0.0, "symbols": set()}
_recent_errors = deque(maxlen=20)


def now() -> float:
    return time.time()


def iso_ts(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or now()).strftime("%Y-%m-%d %H:%M:%S")


def float_any(*vals: Any, default: float = 0.0) -> float:
    for v in vals:
        if v is None:
            continue
        try:
            if isinstance(v, str):
                v = v.replace(",", "").replace("%", "").strip()
            x = float(v)
            if x == x:
                return x
        except Exception:
            continue
    return default


def append_line(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(str(text).rstrip("\n") + "\n")
    except Exception:
        pass


def log(msg: str) -> None:
    append_line(FILES["log"], f"[{iso_ts()}] {msg}")


def log_error(where: str, exc: BaseException) -> None:
    msg = f"{where}: {exc.__class__.__name__}: {exc}"
    _recent_errors.append(msg[:300])
    try:
        append_line(FILES["error"], f"[{iso_ts()}] {msg}")
        append_line(FILES["error"], traceback.format_exc())
    except Exception:
        pass


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:
        log_error(f"load_json:{path.name}", exc)
        return default


def save_json(path: Path, obj: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log_error(f"save_json:{path.name}", exc)


def load_control() -> Dict[str, Any]:
    control = DEFAULT_CONTROL.copy()
    saved = load_json(FILES["control"], {})
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in REMOVED_CONTROL_KEYS:
                continue
            if k in DEFAULT_CONTROL:
                control[k] = v
    # v0.17 고정: candidate_events 소비 옵션은 저장돼 있어도 다시 만들지 않는다.
    for k in REMOVED_CONTROL_KEYS:
        control.pop(k, None)
    return control


def save_control(updates: Dict[str, Any]) -> Dict[str, Any]:
    control = load_control()
    for k, v in updates.items():
        if k in REMOVED_CONTROL_KEYS:
            continue
        if k in DEFAULT_CONTROL:
            control[k] = v
    save_json(FILES["control"], control)
    set_pause_flags(bool(control.get("running")))
    return control


def set_pause_flags(on: bool) -> None:
    for key in ["flag", "legacy_flag"]:
        p = FILES[key]
        try:
            if on:
                p.write_text(f"paper_bot_on {iso_ts()}\n", encoding="utf-8")
            else:
                p.unlink(missing_ok=True)
        except Exception as exc:
            log_error(f"set_flag:{key}", exc)


def read_jsonl(path: Path, max_lines: int = 5000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out
    except Exception as exc:
        log_error(f"read_jsonl:{path.name}", exc)
        return []


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    append_line(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str))


def line_count(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return -1


def normalize_ticker(ticker: Any) -> Optional[str]:
    if not ticker:
        return None
    t = str(ticker).strip().upper().replace("/", "-").replace("_", "-")
    parts = [x for x in t.split("-") if x]
    if len(parts) >= 2:
        non_krw = [x for x in parts if x != "KRW"]
        return (non_krw[-1] if non_krw else parts[-1]).strip().upper() or None
    if t.endswith("KRW") and len(t) > 3:
        t = t[:-3]
    return t.strip().upper() or None


def short_ticker(ticker: Any) -> str:
    return normalize_ticker(ticker) or str(ticker or "").strip().upper()


def get_event_price(event: Dict[str, Any]) -> float:
    return float_any(event.get("entry_price"), event.get("detected_price"), event.get("current_price"), event.get("price"), event.get("close"), event.get("trade_price"), default=0.0)


def event_id(event: Dict[str, Any], lane: str) -> str:
    raw = event.get("event_id") or event.get("id") or event.get("event_key")
    t = normalize_ticker(event.get("ticker") or event.get("market") or event.get("symbol") or "") or "UNKNOWN"
    ts = event.get("created_at") or event.get("detected_ts") or event.get("ts") or event.get("time") or event.get("timestamp") or "0"
    decision = event.get("decision") or event.get("quality_category") or event.get("alert_bucket") or ""
    if raw:
        return f"{lane}:{raw}"
    return f"{lane}:{t}:{ts}:{decision}"


def load_open() -> Dict[str, Dict[str, Any]]:
    obj = load_json(FILES["open"], {})
    return obj if isinstance(obj, dict) else {}


def save_open(open_pos: Dict[str, Dict[str, Any]]) -> None:
    save_json(FILES["open"], open_pos)


def load_closed_ids(limit: int = 15000) -> set[str]:
    ids = set()
    for obj in read_jsonl(FILES["closed"], max_lines=limit):
        eid = obj.get("event_id") or obj.get("pos_id")
        if eid:
            ids.add(str(eid))
    return ids


def closed_count() -> int:
    return line_count(FILES["closed"])


def count_by_lane(open_pos: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    counts = {"strict": 0, "shadow": 0}
    for p in open_pos.values():
        lane = str(p.get("lane") or "shadow")
        counts[lane] = counts.get(lane, 0) + 1
    return counts


def open_tickers(open_pos: Dict[str, Dict[str, Any]]) -> set[str]:
    return {short_ticker(p.get("ticker")) for p in open_pos.values() if short_ticker(p.get("ticker"))}


def fetch_symbols(timeout: float = 2.0) -> set[str]:
    try:
        now_ts = now()
        cached = _symbols_cache.get("symbols")
        if cached and now_ts - float_any(_symbols_cache.get("ts"), default=0.0) < 1800:
            return set(cached)
        req = urllib.request.Request("https://api.bithumb.com/public/ticker/ALL_KRW", headers={"Accept":"application/json", "User-Agent":VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        rows = data.get("data") if isinstance(data, dict) else {}
        symbols = {str(k).upper() for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else set()
        symbols.discard("DATE")
        if symbols:
            _symbols_cache["ts"] = now_ts
            _symbols_cache["symbols"] = symbols
        return symbols
    except Exception:
        return set()


def fetch_bithumb_price(ticker: Any, timeout: float = 2.5) -> Optional[float]:
    sym = normalize_ticker(ticker)
    if not sym:
        return None
    if sym in _bad_markets:
        return None
    symbols = fetch_symbols(timeout=1.8)
    if symbols and sym not in symbols:
        _bad_markets.add(sym)
        return None
    urls = [
        f"https://api.bithumb.com/public/ticker/{urllib.parse.quote(sym)}_KRW",
        f"https://api.bithumb.com/public/ticker/{urllib.parse.quote(sym)}/KRW",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"Accept":"application/json", "User-Agent":VERSION})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            if isinstance(data, dict) and str(data.get("status")) == "0000":
                row = data.get("data") or {}
                price = float_any(row.get("closing_price"), row.get("trade_price"), row.get("close"), default=0.0)
                return price if price > 0 else None
            if isinstance(data, dict) and str(data.get("status")) not in {"0000", "None", ""}:
                _bad_markets.add(sym)
                return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                _bad_markets.add(sym)
                return None
            log_error("fetch_price_http", exc)
            return None
        except Exception as exc:
            log_error("fetch_price", exc)
            return None
    return None


def pick_candidates(control: Dict[str, Any], open_pos: Dict[str, Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:
    existing_ids = set(open_pos.keys()) | load_closed_ids()
    blocked_tickers = open_tickers(open_pos) if control.get("block_same_ticker_open", True) else set()
    picked: List[Tuple[str, Dict[str, Any]]] = []
    stats = {
        "paper_file": 0,
        "shadow_file": 0,
        "events_file": 0,
        "dup_skip": 0,
        "same_ticker_skip": 0,
        "bad_skip": 0,
        "limit_skip": 0,
    }
    lane_counts = count_by_lane(open_pos)
    max_strict = int(float_any(control.get("max_open_strict"), default=80))
    max_shadow = int(float_any(control.get("max_open_shadow"), default=120))
    max_new = int(float_any(control.get("max_new_per_cycle"), default=24))

    def try_pick(ev: Dict[str, Any], lane: str) -> None:
        if len(picked) >= max_new:
            return
        if lane == "strict" and lane_counts.get("strict", 0) >= max_strict:
            stats["limit_skip"] += 1
            return
        if lane == "shadow" and lane_counts.get("shadow", 0) >= max_shadow:
            stats["limit_skip"] += 1
            return
        eid = event_id(ev, lane)
        if eid in existing_ids:
            stats["dup_skip"] += 1
            return
        t = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol"))
        price = get_event_price(ev)
        if not t or price <= 0:
            stats["bad_skip"] += 1
            return
        if control.get("block_same_ticker_open", True) and t in blocked_tickers:
            stats["same_ticker_skip"] += 1
            return
        ev = dict(ev)
        ev["lane"] = lane
        picked.append((eid, ev))
        existing_ids.add(eid)
        blocked_tickers.add(t)
        lane_counts[lane] = lane_counts.get(lane, 0) + 1

    # 1) 정식 후보만 paper_candidates에서 읽는다.
    for ev in read_jsonl(FILES["paper_candidates"], max_lines=2500):
        if len(picked) >= max_new:
            break
        stats["paper_file"] += 1
        try_pick(ev, "strict")

    # 2) 복기 후보만 shadow_candidates에서 읽는다.
    for ev in read_jsonl(FILES["shadow_candidates"], max_lines=2500):
        if len(picked) >= max_new:
            break
        stats["shadow_file"] += 1
        try_pick(ev, "shadow")

    # candidate_events는 v0.17에서 완전 제거. 읽지도 세지도 않는다.
    stats["events_file"] = 0
    return picked, stats


def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:
    ticker = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol")) or "UNKNOWN"
    detected_price = get_event_price(ev)
    live_price = fetch_bithumb_price(ticker) or detected_price
    entry_price = live_price if live_price > 0 else detected_price
    lane = str(ev.get("lane") or "shadow")
    return {
        "pos_id": pos_id,
        "event_id": pos_id,
        "ticker": ticker,
        "lane": lane,
        "opened_at": now(),
        "opened_at_text": iso_ts(),
        "entry_price": entry_price,
        "detected_price": detected_price,
        "current_price": entry_price,
        "peak_pct": 0.0,
        "trough_pct": 0.0,
        "last_update": now(),
        "strategy": ev.get("strategy") or ev.get("route") or ev.get("section") or "unknown",
        "decision": ev.get("decision") or ev.get("quality_category") or "",
        "score": float_any(ev.get("score"), ev.get("leader_score"), default=0.0),
        "edge": float_any(ev.get("edge"), ev.get("edge_score"), default=0.0),
        "reason": ev.get("reason") or ev.get("why") or ev.get("block_reason") or "",
        "raw": ev,
    }


def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    ticker = pos.get("ticker") or ""
    entry = float_any(pos.get("entry_price"), default=0.0)
    if entry <= 0:
        return pos, None
    price = fetch_bithumb_price(ticker) or float_any(pos.get("current_price"), pos.get("entry_price"), default=entry)
    pos["current_price"] = price
    gross = ((price / entry) - 1.0) * 100.0
    net = gross - float_any(control.get("fee_pct_roundtrip"), default=0.10)
    pos["last_pnl_pct"] = net
    pos["peak_pct"] = max(float_any(pos.get("peak_pct"), default=0.0), net)
    pos["trough_pct"] = min(float_any(pos.get("trough_pct"), default=0.0), net)
    pos["last_update"] = now()

    age_min = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
    peak = float_any(pos.get("peak_pct"), default=0.0)
    exit_reason: Optional[str] = None

    if net <= float_any(control.get("stop_loss_pct"), default=-1.2):
        exit_reason = "stop_loss"
    elif peak >= float_any(control.get("protect_trigger_pct"), default=0.9) and net <= float_any(control.get("protect_floor_pct"), default=0.2):
        exit_reason = "protect_stop_after_tp"
    elif net >= float_any(control.get("take_profit_pct"), default=1.2):
        exit_reason = "take_profit"
    elif age_min >= float_any(control.get("slow_minutes"), default=20) and peak < float_any(control.get("slow_peak_under_pct"), default=0.25) and net <= 0.10:
        exit_reason = "slow_no_progress"
    elif age_min >= float_any(control.get("time_exit_minutes"), default=120):
        exit_reason = "time_exit"

    if not exit_reason:
        return pos, None

    closed = {
        "closed_at": now(),
        "closed_at_text": iso_ts(),
        "pos_id": pos.get("pos_id"),
        "event_id": pos.get("event_id"),
        "ticker": ticker,
        "lane": pos.get("lane"),
        "entry_price": entry,
        "exit_price": price,
        "pnl_pct": round(net, 4),
        "peak_pct": round(peak, 4),
        "trough_pct": round(float_any(pos.get("trough_pct"), default=0.0), 4),
        "age_min": round(age_min, 2),
        "exit_reason": exit_reason,
        "strategy": pos.get("strategy"),
        "decision": pos.get("decision"),
        "score": pos.get("score"),
        "edge": pos.get("edge"),
    }
    return pos, closed


def format_price(v: Any) -> str:
    x = float_any(v, default=0.0)
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.4f}"
    return f"{x:,.8f}"


def fmt_pct(v: Any) -> str:
    return f"{float_any(v, default=0.0):+.2f}%"


def send_message(text: str) -> None:
    if not TOKEN or not NOTIFY_CHAT_ID:
        return
    chunks = [str(text)[i:i+3900] for i in range(0, len(str(text)), 3900)] or [""]
    for ch in chunks:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": NOTIFY_CHAT_ID, "text": ch}).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except Exception as exc:
            log_error("send_message", exc)


def format_open_alert(pos: Dict[str, Any]) -> str:
    return "\n".join([
        "✅ 정식 모의매매 진입",
        f"- 코인: {short_ticker(pos.get('ticker'))}",
        f"- 진입가: {format_price(pos.get('entry_price'))}",
        f"- 전략/경로: {pos.get('strategy') or 'unknown'}",
        f"- 점수: {float_any(pos.get('score'), default=0.0):.2f} / edge {float_any(pos.get('edge'), default=0.0):.2f}",
        "- 구분: 정식 모의매매급",
        "- 참고: 실제 주문 아님 / paper_bot 장부 기록",
    ])


def format_close_alert(closed: Dict[str, Any]) -> str:
    return "\n".join([
        "📘 정식 모의매매 종료",
        f"- 코인: {short_ticker(closed.get('ticker'))}",
        f"- 수익률: {fmt_pct(closed.get('pnl_pct'))}",
        f"- 진입/청산: {format_price(closed.get('entry_price'))} → {format_price(closed.get('exit_price'))}",
        f"- 최고/최저: {fmt_pct(closed.get('peak_pct'))} / {fmt_pct(closed.get('trough_pct'))}",
        f"- 사유: {closed.get('exit_reason')}",
        f"- 보유: {float_any(closed.get('age_min'), default=0.0):.1f}분",
    ])


def notify_strict_events(opened: List[Dict[str, Any]], closed: List[Dict[str, Any]], control: Dict[str, Any]) -> None:
    # 알림 실패가 worker/장부 저장을 흔들지 않게 완전 격리.
    try:
        if control.get("notify_on_strict_open", True):
            for p in opened:
                if str(p.get("lane")) == "strict":
                    send_message(format_open_alert(p))
        if control.get("notify_on_strict_close", True):
            for c in closed:
                if str(c.get("lane")) == "strict":
                    send_message(format_close_alert(c))
    except Exception as exc:
        log_error("notify_strict_events", exc)


def file_stats() -> Dict[str, Any]:
    out = {}
    for name in ["paper_candidates", "shadow_candidates", "open", "closed", "status", "error", "log"]:
        p = FILES[name]
        info = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}
        if p.suffix == ".jsonl" and p.exists():
            info["lines"] = line_count(p)
        out[name] = info
    return out


def run_cycle() -> Dict[str, Any]:
    started = now()
    control = load_control()
    with _state_lock:
        open_pos = load_open()
        counts_before = count_by_lane(open_pos)
        picked, pick_stats = pick_candidates(control, open_pos)
        opened_items: List[Dict[str, Any]] = []
        opened = 0
        for pos_id, ev in picked:
            try:
                if pos_id not in open_pos:
                    new_pos = open_position(pos_id, ev, control)
                    open_pos[pos_id] = new_pos
                    opened_items.append(new_pos)
                    opened += 1
            except Exception as exc:
                log_error("open_position", exc)

        closed_items: List[Dict[str, Any]] = []
        for pos_id, pos in list(open_pos.items()):
            try:
                updated, closed = update_position(pos, control)
                if closed:
                    closed_items.append(closed)
                    open_pos.pop(pos_id, None)
                    append_jsonl(FILES["closed"], closed)
                else:
                    open_pos[pos_id] = updated
            except Exception as exc:
                log_error("update_position", exc)

        save_open(open_pos)
        counts_after = count_by_lane(open_pos)
        status = {
            "version": VERSION,
            "updated_at": now(),
            "updated_at_text": iso_ts(),
            "running": bool(control.get("running")),
            "loop_seconds": control.get("loop_seconds"),
            "opened_this_cycle": opened,
            "closed_this_cycle": len(closed_items),
            "open_total": len(open_pos),
            "open_strict": counts_after.get("strict", 0),
            "open_shadow": counts_after.get("shadow", 0),
            "closed_total": closed_count(),
            "pick_stats": pick_stats,
            "counts_before": counts_before,
            "elapsed_sec": round(now() - started, 3),
            "files": file_stats(),
            "flag_exists": FILES["flag"].exists(),
            "candidate_events_consumed": 0,
            "candidate_events_note": "v0.17: candidate_events 읽기/소비 없음",
        }
        save_json(FILES["status"], status)
        notify_strict_events(opened_items, closed_items, control)
        log(f"cycle opened={opened} closed={len(closed_items)} open={len(open_pos)} elapsed={status['elapsed_sec']}s")
        return status


def worker_loop() -> None:
    log("worker_loop started")
    while not _stop_event.is_set():
        try:
            control = load_control()
            if control.get("running"):
                run_cycle()
            _stop_event.wait(float_any(control.get("loop_seconds"), default=8.0))
        except Exception as exc:
            log_error("worker_loop", exc)
            _stop_event.wait(3)
    log("worker_loop stopped")


def score_stats(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    arr = list(rows or [])
    n = len(arr)
    wins = sum(1 for r in arr if float_any(r.get("pnl_pct"), default=0.0) > 0)
    total = sum(float_any(r.get("pnl_pct"), default=0.0) for r in arr)
    return {"n": n, "wins": wins, "losses": n-wins, "win_rate": wins/n*100 if n else 0.0, "total": total, "avg": total/n if n else 0.0}


def fmt_stats(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    s = score_stats(rows)
    icon = "✅" if s["avg"] > 0 else "❌"
    return f"{icon} {label}: {s['n']}전 {s['wins']}승 {s['losses']}패 / 승률 {s['win_rate']:.1f}% / 합산 {s['total']:+.2f}% / 평균 {s['avg']:+.2f}%"


def summary_text() -> str:
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    counts = count_by_lane(open_pos)
    fs = file_stats()
    err_size = fs.get("error", {}).get("size", 0)
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- OPEN: 전체 {len(open_pos)} / 정식 {counts.get('strict',0)} / 복기 {counts.get('shadow',0)}",
        f"- CLOSED: {closed_count()}건",
        f"- 소비파일: 정식 {fs.get('paper_candidates',{}).get('lines',0)} / 복기 {fs.get('shadow_candidates',{}).get('lines',0)} / candidate_events 소비 0",
        f"- 이번 cycle: open +{status.get('opened_this_cycle',0)} / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"- pick: paper {pick.get('paper_file',0)} / shadow {pick.get('shadow_file',0)} / dup {pick.get('dup_skip',0)} / same_ticker {pick.get('same_ticker_skip',0)} / bad {pick.get('bad_skip',0)}",
        f"- 알림: 정식 모의매매급만 ON / 복기용 알림 OFF",
        f"- 업데이트: {status.get('updated_at_text','-')}",
        f"- flag: {'ON' if FILES['flag'].exists() else 'OFF'} / 오류로그 {err_size} bytes / bad_market {len(_bad_markets)}",
    ])


def tail_file(path: Path, n: int = 40) -> str:
    if not path.exists():
        return f"파일 없음: {path.name}"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
        return "\n".join(lines) if lines else f"빈 파일: {path.name}"
    except Exception as exc:
        return f"읽기 실패 {path.name}: {exc}"


def tg_api(method: str, params: Dict[str, Any], timeout: int = 25) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def send_chat(chat_id: Any, text: str) -> None:
    if not TOKEN:
        print(text)
        return
    body = str(text or "")
    chunks = [body[i:i+3900] for i in range(0, len(body), 3900)] or [""]
    for ch in chunks:
        try:
            tg_api("sendMessage", {"chat_id": chat_id, "text": ch, "disable_web_page_preview": "true"}, timeout=20)
        except Exception as exc:
            log_error("send_chat", exc)




def format_open_list(limit: int = 20) -> str:
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:limit]
    lines = []
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        lines.append(f"- {short_ticker(pos.get('ticker'))} / {pos.get('lane','-')} / 진입 {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / {float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / {age:.1f}분")
    return "\n".join(lines)


def files_text() -> str:
    fs = file_stats()
    return "\n".join([
        "📁 페이퍼봇 파일 /pfiles",
        f"- paper_candidates: {fs.get('paper_candidates',{}).get('lines',0)} lines / {fs.get('paper_candidates',{}).get('size',0)} bytes",
        f"- shadow_candidates: {fs.get('shadow_candidates',{}).get('lines',0)} lines / {fs.get('shadow_candidates',{}).get('size',0)} bytes",
        f"- open: {fs.get('open',{}).get('size',0)} bytes",
        f"- closed: {fs.get('closed',{}).get('lines',0)} lines / {fs.get('closed',{}).get('size',0)} bytes",
        "- candidate_events: 소비 안 함 / 읽지 않음",
    ])


def control_text() -> str:
    c = load_control()
    keys = ["running", "loop_seconds", "max_open_strict", "max_open_shadow", "max_new_per_cycle", "fee_pct_roundtrip", "take_profit_pct", "protect_trigger_pct", "protect_floor_pct", "stop_loss_pct", "slow_minutes", "time_exit_minutes", "block_same_ticker_open"]
    lines = ["⚙️ 페이퍼봇 설정 /pcontrol"]
    for k in keys:
        lines.append(f"- {k}: {c.get(k)}")
    lines.append("- allow_shadow_from_candidate_events: 제거됨/무시됨")
    return "\n".join(lines)

def handle_command(chat_id: str, text: str) -> None:
    cmd = (text or "").strip().split()[0].lower()
    if cmd in {"/phelp", "/start"}:
        send_chat(chat_id, "\n".join([
            f"🧪 {VERSION}",
            "명령어: /pbatch /pstatus /ponce /pstart /pstop /prestart /plog /perror /pscore /popen /pfiles /pcontrol",
            "소비파일: paper_candidates.jsonl / shadow_candidates.jsonl만 사용",
            "candidate_events는 읽지 않음",
        ]))
    elif cmd == "/pstatus":
        send_chat(chat_id, summary_text())
    elif cmd == "/pbatch":
        rows = read_jsonl(FILES["closed"], max_lines=20000)
        strict = [r for r in rows if str(r.get("lane")) == "strict"]
        shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
        send_chat(chat_id, "\n".join([
            "📦 페이퍼봇 묶음 /pbatch",
            summary_text(),
            "",
            "📊 성과",
            fmt_stats("전체", rows),
            fmt_stats("정식 strict", strict),
            fmt_stats("복기 shadow", shadow),
            "",
            "구조",
            "- paper_candidates / shadow_candidates만 소비",
            "- candidate_events 소비 0 / fallback 없음",
            "- 전략판단 없음 / 재스캔 없음 / 실제 주문 없음",
        ]))
    elif cmd == "/ponce":
        st = run_cycle()
        send_chat(chat_id, "✅ 1회 cycle 완료\n" + summary_text())
    elif cmd == "/pstart":
        save_control({"running": True})
        send_chat(chat_id, "✅ paper_bot 실행 ON\n" + summary_text())
    elif cmd == "/pstop":
        save_control({"running": False})
        send_chat(chat_id, "⏸ paper_bot 실행 OFF\n" + summary_text())
    elif cmd == "/prestart":
        save_control({"running": False})
        time.sleep(0.5)
        save_control({"running": True})
        send_chat(chat_id, "🔁 paper_bot 논리 재시작 완료\n" + summary_text())
    elif cmd == "/plog":
        send_chat(chat_id, "🧾 paper_bot.log\n\n" + tail_file(FILES["log"], 60)[-3500:])
    elif cmd == "/perror":
        send_chat(chat_id, "🧯 paper_bot_error.log\n\n" + tail_file(FILES["error"], 80)[-3500:])
    elif cmd == "/pscore":
        rows = read_jsonl(FILES["closed"], max_lines=20000)
        strict = [r for r in rows if str(r.get("lane")) == "strict"]
        shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
        by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in rows)
        lines = ["📊 페이퍼봇 성과 /pscore", fmt_stats("전체", rows), fmt_stats("정식 strict", strict), fmt_stats("복기 shadow", shadow), "", "청산사유"]
        for k, v in by_reason.most_common(10):
            lines.append(f"- {k}: {v}건")
        send_chat(chat_id, "\n".join(lines))
    elif cmd == "/popen":
        send_chat(chat_id, "📂 페이퍼봇 OPEN /popen\n\n" + format_open_list(limit=30))
    elif cmd == "/pfiles":
        send_chat(chat_id, files_text())
    elif cmd == "/pcontrol":
        send_chat(chat_id, control_text())
    else:
        send_chat(chat_id, "알 수 없는 명령. /phelp")


def telegram_loop() -> None:
    global _update_offset
    log("telegram_loop started")
    if not TOKEN or not ALLOWED_CHAT_ID:
        log("telegram disabled: token/chat_id missing")
        return
    while not _stop_event.is_set():
        try:
            res = tg_api("getUpdates", {"timeout": 20, "offset": _update_offset + 1}, timeout=30)
            for upd in res.get("result", []):
                _update_offset = max(_update_offset, int(upd.get("update_id", 0)))
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                if ALLOWED_CHAT_ID and chat_id != str(ALLOWED_CHAT_ID):
                    continue
                text = msg.get("text") or ""
                if text.startswith("/"):
                    handle_command(chat_id, text)
        except Exception as exc:
            log_error("telegram_loop", exc)
            _stop_event.wait(3)
    log("telegram_loop stopped")


def write_pid() -> None:
    try:
        FILES["pid"].write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def handle_signal(signum, frame) -> None:
    _stop_event.set()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", action="store_true", help="run telegram + worker")
    parser.add_argument("--once", action="store_true", help="run one paper cycle")
    args = parser.parse_args()
    write_pid()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    # 구 control에서 candidate_events 키 제거한 상태로 한 번 저장해둔다.
    save_control({})
    if args.once:
        print(json.dumps(run_cycle(), ensure_ascii=False, indent=2, default=str))
        return
    t_worker = threading.Thread(target=worker_loop, name="paper_worker", daemon=True)
    t_worker.start()
    t_tg = threading.Thread(target=telegram_loop, name="paper_telegram", daemon=True)
    t_tg.start()
    log(f"{VERSION} started")
    while not _stop_event.is_set():
        time.sleep(1)
    log(f"{VERSION} stopped")


if __name__ == "__main__":
    main()
