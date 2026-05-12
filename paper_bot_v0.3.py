#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_bot_v0.3.py
- 텔레그램 조종 + 모의매매 실행을 한 파일에서 처리하는 '페이퍼봇'.
- 메인봇이 만든 후보파일을 읽고, 가짜 OPEN/CLOSED 결과를 저장한다.
- 실제 주문 기능 없음. 실매매와 연결 금지.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from threading import Thread, Event, Lock
from typing import Any, Dict, List, Optional, Tuple

VERSION = "paper_bot_v0.3"
BASE_DIR = Path(__file__).resolve().parent

FILES = {
    "candidate_events": BASE_DIR / "candidate_events.jsonl",
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
    "max_open_strict": 8,
    "max_open_shadow": 20,
    "max_new_per_cycle": 8,
    "allow_shadow_from_candidate_events": True,
    # 알림은 정식 모의매매급만 보낸다. 탈락 후보 복기는 기록만 쌓고 텔레그램 알림은 보내지 않는다.
    "notify_on_strict_open": True,
    "notify_on_strict_close": True,
    "notify_on_shadow": False,
    "fee_pct_roundtrip": 0.10,  # 왕복 수수료 가정치(%). 실제 체결 아님.
    "take_profit_pct": 1.20,
    "protect_trigger_pct": 0.90,
    "protect_floor_pct": 0.20,
    "stop_loss_pct": -1.20,
    "slow_minutes": 20,
    "slow_peak_under_pct": 0.25,
    "time_exit_minutes": 120,
}

_stop_event = Event()
_state_lock = Lock()
_update_offset = 0
_BAD_MARKETS: set[str] = set()
_BITHUMB_MARKETS_CACHE: dict[str, Any] = {'ts': 0.0, 'symbols': set()}
ERROR_LOG_ROTATE_MAX_BYTES = 500_000
ERROR_LOG_KEEP_LINES = 260



def now() -> float:
    return time.time()


def iso_ts(ts: Optional[float] = None) -> str:
    ts = ts or now()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def append_line(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")


def log(msg: str) -> None:
    append_line(FILES["log"], f"[{iso_ts()}] {msg}")


def rotate_error_log_if_needed() -> None:
    """오류로그가 너무 커지면 최근 줄만 남긴다. 오래된 내용은 .bak로 백업."""
    try:
        path = FILES["error"]
        if not path.exists() or path.stat().st_size <= ERROR_LOG_ROTATE_MAX_BYTES:
            return
        bak = path.with_name(path.name + f".bak_{time.strftime('%Y%m%d_%H%M%S')}")
        try:
            path.replace(bak)
            lines = bak.read_text(encoding="utf-8", errors="ignore").splitlines()[-ERROR_LOG_KEEP_LINES:]
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        except Exception:
            # 백업 실패 시에도 봇은 멈추지 않게 둔다.
            pass
    except Exception:
        pass


def log_error(where: str, exc: BaseException) -> None:
    try:
        rotate_error_log_if_needed()
        append_line(FILES["error"], f"[{iso_ts()}] {where}: {exc}")
        append_line(FILES["error"], traceback.format_exc())
    except Exception:
        pass


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log_error(f"load_json:{path.name}", exc)
        return default


def save_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_control() -> Dict[str, Any]:
    control = DEFAULT_CONTROL.copy()
    saved = load_json(FILES["control"], {})
    if isinstance(saved, dict):
        control.update(saved)
    return control


def save_control(updates: Dict[str, Any]) -> Dict[str, Any]:
    control = load_control()
    control.update(updates)
    save_json(FILES["control"], control)
    return control


def set_pause_flags(on: bool) -> None:
    # v131부터 primary는 external_paper_bot_on.flag. legacy flag도 같이 맞춰 예전 메인봇과 충돌을 줄인다.
    for key in ["flag", "legacy_flag"]:
        path = FILES.get(key)
        if not isinstance(path, Path):
            continue
        try:
            if on:
                path.write_text(f"paper_bot_on {iso_ts()}\n", encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except Exception as exc:
            log_error(f"set_pause_flags:{key}", exc)


def read_jsonl(path: Path, max_lines: int = 5000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        for line in lines[-max_lines:]:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
    except Exception as exc:
        log_error(f"read_jsonl:{path.name}", exc)
    return out


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    append_line(path, json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def normalize_ticker(ticker: Any) -> Optional[str]:
    if not ticker:
        return None
    t = str(ticker).strip().upper()
    if not t:
        return None
    t = t.replace("/", "-")
    if t.startswith("KRW-"):
        return t
    if t.endswith("KRW") and "-" not in t:
        t = t[:-3]
    if "-" in t:
        parts = t.split("-")
        if len(parts) == 2 and parts[0] != "KRW":
            return f"KRW-{parts[-1]}"
        return t
    return f"KRW-{t}"


def short_ticker(ticker: str) -> str:
    return ticker.replace("KRW-", "")


def bithumb_symbol(ticker: Any) -> Optional[str]:
    """메인봇 후보 ticker를 빗썸용 심볼(BTC, ETH...)로 통일한다."""
    t = normalize_ticker(ticker)
    if not t:
        return None
    return short_ticker(t).replace("_KRW", "").replace("-KRW", "").strip().upper() or None


def float_any(*vals: Any, default: float = 0.0) -> float:
    for v in vals:
        if v is None:
            continue
        try:
            if isinstance(v, str):
                v = v.replace(",", "").replace("%", "").strip()
            f = float(v)
            if f == f:
                return f
        except Exception:
            continue
    return default


def get_event_price(event: Dict[str, Any]) -> float:
    return float_any(
        event.get("entry_price"),
        event.get("detected_price"),
        event.get("current_price"),
        event.get("price"),
        event.get("close"),
        event.get("trade_price"),
        default=0.0,
    )


def fetch_bithumb_symbols(timeout: float = 3.0) -> set[str]:
    """빗썸 KRW 마켓 심볼 목록을 캐시한다. 실패하면 빈 set을 반환하고 기존 후보 처리는 계속한다."""
    try:
        now_ts = now()
        cached = _BITHUMB_MARKETS_CACHE.get("symbols")
        if cached and now_ts - float_any(_BITHUMB_MARKETS_CACHE.get("ts"), default=0.0) < 1800:
            return set(cached)
        url = "https://api.bithumb.com/public/ticker/ALL_KRW"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("data") if isinstance(data, dict) else {}
        symbols = {str(k).upper() for k in rows.keys() if isinstance(rows.get(k), dict)} if isinstance(rows, dict) else set()
        symbols.discard("DATE")
        if symbols:
            _BITHUMB_MARKETS_CACHE["ts"] = now_ts
            _BITHUMB_MARKETS_CACHE["symbols"] = symbols
        return symbols
    except Exception as exc:
        # 마켓목록 실패는 큰 오류로 누적하지 않고 로그에만 남긴다.
        log(f"bithumb symbol list fetch failed: {exc.__class__.__name__}")
        return set()


def fetch_bithumb_price(ticker: str, timeout: float = 2.5) -> Optional[float]:
    """빗썸 현재가 조회. 없는 종목/404는 bad_market_skip으로 조용히 넘긴다."""
    sym = bithumb_symbol(ticker)
    if not sym:
        return None
    if sym in _BAD_MARKETS:
        return None
    symbols = fetch_bithumb_symbols(timeout=1.8)
    if symbols and sym not in symbols:
        _BAD_MARKETS.add(sym)
        log(f"bad_market_skip {sym}: not in Bithumb KRW market")
        return None
    urls = [
        f"https://api.bithumb.com/public/ticker/{urllib.parse.quote(sym)}_KRW",
        f"https://api.bithumb.com/public/ticker/{urllib.parse.quote(sym)}/KRW",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": VERSION})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict) and str(data.get("status")) == "0000":
                row = data.get("data") or {}
                price = float_any(row.get("closing_price"), row.get("trade_price"), row.get("close"), default=0.0)
                return price if price > 0 else None
            if isinstance(data, dict) and str(data.get("status")) not in {"0000", "None", ""}:
                _BAD_MARKETS.add(sym)
                log(f"bad_market_skip {sym}: bithumb status {data.get('status')}")
                return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                _BAD_MARKETS.add(sym)
                log(f"bad_market_skip {sym}: HTTP 404")
                return None
            log_error("fetch_bithumb_price_http", exc)
            return None
        except Exception as exc:
            log_error("fetch_bithumb_price", exc)
            return None
    return None

def event_id(event: Dict[str, Any], lane: str) -> str:
    raw = event.get("event_id") or event.get("id") or event.get("event_key")
    ticker = normalize_ticker(event.get("ticker") or event.get("market") or event.get("symbol") or "") or "UNKNOWN"
    ts = event.get("created_at") or event.get("detected_ts") or event.get("ts") or event.get("time") or event.get("timestamp") or "0"
    decision = event.get("decision") or event.get("quality_category") or event.get("alert_bucket") or ""
    if raw:
        return f"{lane}:{raw}"
    return f"{lane}:{ticker}:{ts}:{decision}"


def load_open() -> Dict[str, Dict[str, Any]]:
    obj = load_json(FILES["open"], {})
    return obj if isinstance(obj, dict) else {}


def save_open(open_pos: Dict[str, Dict[str, Any]]) -> None:
    save_json(FILES["open"], open_pos)


def closed_count() -> int:
    if not FILES["closed"].exists():
        return 0
    try:
        return sum(1 for _ in FILES["closed"].open("r", encoding="utf-8", errors="ignore"))
    except Exception:
        return 0


def load_closed_ids(limit: int = 10000) -> set:
    ids = set()
    for obj in read_jsonl(FILES["closed"], max_lines=limit):
        eid = obj.get("event_id") or obj.get("pos_id")
        if eid:
            ids.add(str(eid))
    return ids


def count_by_lane(open_pos: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    counts = {"strict": 0, "shadow": 0}
    for p in open_pos.values():
        lane = str(p.get("lane") or "shadow")
        counts[lane] = counts.get(lane, 0) + 1
    return counts


def pick_candidates(control: Dict[str, Any], open_pos: Dict[str, Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:
    closed_ids = load_closed_ids()
    existing_ids = set(open_pos.keys()) | closed_ids
    picked: List[Tuple[str, Dict[str, Any]]] = []
    stats = {
        "paper_file": 0,
        "shadow_file": 0,
        "events_file": 0,
        "dup_skip": 0,
        "bad_skip": 0,
    }

    lane_counts = count_by_lane(open_pos)
    max_strict = int(control.get("max_open_strict", 8))
    max_shadow = int(control.get("max_open_shadow", 20))
    max_new = int(control.get("max_new_per_cycle", 8))

    # 1) 정식 paper 후보
    for ev in read_jsonl(FILES["paper_candidates"], max_lines=2000):
        if len(picked) >= max_new:
            break
        stats["paper_file"] += 1
        lane = "strict"
        eid = event_id(ev, lane)
        if eid in existing_ids:
            stats["dup_skip"] += 1
            continue
        if lane_counts.get("strict", 0) >= max_strict:
            break
        t = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol"))
        price = get_event_price(ev)
        if not t or price <= 0:
            stats["bad_skip"] += 1
            continue
        ev["lane"] = lane
        picked.append((eid, ev))
        existing_ids.add(eid)
        lane_counts["strict"] = lane_counts.get("strict", 0) + 1

    # 2) 탈락 복기용 shadow 후보
    for ev in read_jsonl(FILES["shadow_candidates"], max_lines=2000):
        if len(picked) >= max_new:
            break
        stats["shadow_file"] += 1
        lane = "shadow"
        eid = event_id(ev, lane)
        if eid in existing_ids:
            stats["dup_skip"] += 1
            continue
        if lane_counts.get("shadow", 0) >= max_shadow:
            break
        t = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol"))
        price = get_event_price(ev)
        if not t or price <= 0:
            stats["bad_skip"] += 1
            continue
        ev["lane"] = lane
        picked.append((eid, ev))
        existing_ids.add(eid)
        lane_counts["shadow"] = lane_counts.get("shadow", 0) + 1

    # 3) shadow 파일이 비었을 때만, 전체 이벤트에서 복기 후보를 일부 가져옴
    if control.get("allow_shadow_from_candidate_events", True):
        for ev in read_jsonl(FILES["candidate_events"], max_lines=2000):
            if len(picked) >= max_new:
                break
            stats["events_file"] += 1
            decision = str(ev.get("decision") or ev.get("quality_category") or "").lower()
            eligible = bool(ev.get("eligible_for_paper") or ev.get("paper_eligible"))
            lane = "strict" if eligible else "shadow"
            if lane == "strict" and lane_counts.get("strict", 0) >= max_strict:
                continue
            if lane == "shadow" and lane_counts.get("shadow", 0) >= max_shadow:
                continue
            # 너무 무의미한 기록은 제외. 그래도 밤새 복기용 데이터는 쌓이게 한다.
            if lane == "shadow" and not any(x in decision for x in ["blocked", "discard", "flow", "money", "data", "quality"]):
                # decision이 빈 경우에도 점수/가격이 있으면 허용
                if float_any(ev.get("score"), ev.get("edge"), default=0.0) <= 0:
                    continue
            eid = event_id(ev, lane)
            if eid in existing_ids:
                stats["dup_skip"] += 1
                continue
            t = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol"))
            price = get_event_price(ev)
            if not t or price <= 0:
                stats["bad_skip"] += 1
                continue
            ev["lane"] = lane
            picked.append((eid, ev))
            existing_ids.add(eid)
            lane_counts[lane] = lane_counts.get(lane, 0) + 1

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


def run_cycle() -> Dict[str, Any]:
    started = now()
    control = load_control()
    with _state_lock:
        open_pos = load_open()
        counts_before = count_by_lane(open_pos)
        picked, pick_stats = pick_candidates(control, open_pos)
        opened = 0
        opened_items: List[Dict[str, Any]] = []
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
        }
        save_json(FILES["status"], status)
        notify_strict_events(opened_items, closed_items, control)
        log(f"cycle opened={opened} closed={len(closed_items)} open={len(open_pos)} elapsed={status['elapsed_sec']}s")
        return status


def file_stats() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in ["candidate_events", "paper_candidates", "shadow_candidates", "open", "closed", "status", "error", "log"]:
        p = FILES[name]
        info = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}
        if p.suffix == ".jsonl" and p.exists():
            try:
                info["lines"] = sum(1 for _ in p.open("r", encoding="utf-8", errors="ignore"))
            except Exception:
                info["lines"] = -1
        out[name] = info
    return out


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


def summary_text() -> str:
    status = load_json(FILES["status"], {})
    control = load_control()
    open_pos = load_open()
    counts = count_by_lane(open_pos)
    fs = file_stats()
    lines_candidate = fs.get("candidate_events", {}).get("lines", 0)
    lines_paper = fs.get("paper_candidates", {}).get("lines", 0)
    lines_shadow = fs.get("shadow_candidates", {}).get("lines", 0)
    err_size = fs.get("error", {}).get("size", 0)
    return (
        f"🧪 페이퍼봇 {VERSION}\n"
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s\n"
        f"- OPEN: 전체 {len(open_pos)} / 정식 {counts.get('strict',0)} / 복기 {counts.get('shadow',0)}\n"
        f"- CLOSED: {closed_count()}건\n"
        f"- 후보파일: 전체 {lines_candidate} / 정식 {lines_paper} / 복기 {lines_shadow}\n"
        f"- 알림: 정식 모의매매급만 ON / 복기용 알림 OFF\n"
        f"- 최근 cycle: open +{status.get('opened_this_cycle',0)} / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s\n"
        f"- 업데이트: {status.get('updated_at_text','-')}\n"
        f"- flag: {'ON' if FILES['flag'].exists() else 'OFF'} / 오류로그 {err_size} bytes / bad_market {len(_BAD_MARKETS)}"
    )


def tail_file(path: Path, n: int = 20) -> str:
    if not path.exists():
        return f"파일 없음: {path.name}"
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-n:]
        if not lines:
            return f"빈 파일: {path.name}"
        return "\n".join(lines)
    except Exception as exc:
        return f"읽기 실패 {path.name}: {exc}"


def tg_api(token: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw)


def send_message(token: str, chat_id: Any, text: str) -> None:
    # Telegram 제한 방지: 너무 길면 자름
    max_len = 3900
    chunks = [text[i:i+max_len] for i in range(0, len(text), max_len)] or [""]
    for ch in chunks:
        tg_api(token, "sendMessage", {"chat_id": chat_id, "text": ch})


def get_notify_chat_id() -> str:
    # 자동 알림은 별도 지정값 우선. 없으면 허용 채팅방 값을 사용한다.
    return (
        os.environ.get("PAPER_BOT_NOTIFY_CHAT_ID", "").strip()
        or os.environ.get("PAPER_BOT_ALLOWED_CHAT_ID", "").strip()
    )


def _fmt_alert_price(v: Any) -> str:
    try:
        x = float_any(v, default=0.0)
        if x >= 1000:
            return f"{x:,.0f}"
        if x >= 1:
            return f"{x:,.4f}"
        return f"{x:,.8f}"
    except Exception:
        return "0"


def _format_strict_open_alert(pos: Dict[str, Any]) -> str:
    return (
        "✅ 정식 모의매매 진입\n"
        f"- 코인: {short_ticker(str(pos.get('ticker') or ''))}\n"
        f"- 진입가: {_fmt_alert_price(pos.get('entry_price'))}\n"
        f"- 전략/경로: {pos.get('strategy') or 'unknown'}\n"
        f"- 점수: {float_any(pos.get('score'), default=0.0):.2f} / edge {float_any(pos.get('edge'), default=0.0):.2f}\n"
        "- 구분: 정식 모의매매급\n"
        "- 참고: 실제 주문 아님"
    )


def _format_strict_close_alert(row: Dict[str, Any]) -> str:
    pnl = float_any(row.get('pnl_pct'), default=0.0)
    icon = "✅" if pnl >= 0 else "❌"
    return (
        f"{icon} 정식 모의매매 종료\n"
        f"- 코인: {short_ticker(str(row.get('ticker') or ''))}\n"
        f"- 수익률: {pnl:+.2f}% / 최고 {float_any(row.get('peak_pct'), default=0.0):+.2f}%\n"
        f"- 종료이유: {row.get('exit_reason') or '-'}\n"
        f"- 보유시간: {float_any(row.get('age_min'), default=0.0):.1f}분\n"
        "- 구분: 정식 모의매매급"
    )


def notify_strict_events(opened_items: List[Dict[str, Any]], closed_items: List[Dict[str, Any]], control: Dict[str, Any]) -> None:
    # 복기용 shadow는 알림 금지. 기록/상태에는 남긴다.
    token = os.environ.get("PAPER_BOT_TOKEN", "").strip()
    chat_id = get_notify_chat_id()
    if not token or not chat_id:
        return
    try:
        if bool(control.get("notify_on_strict_open", True)):
            for pos in opened_items:
                if str(pos.get("lane") or "") == "strict":
                    send_message(token, chat_id, _format_strict_open_alert(pos))
        if bool(control.get("notify_on_strict_close", True)):
            for row in closed_items:
                if str(row.get("lane") or "") == "strict":
                    send_message(token, chat_id, _format_strict_close_alert(row))
    except Exception as exc:
        log_error("notify_strict_events", exc)


def build_pbatch_text() -> str:
    """페이퍼봇 전체 점검: 상태/파일/최근로그/오류를 한 번에 보여준다."""
    fs = file_stats()
    status = load_json(FILES["status"], {})
    control = load_control()
    open_pos = load_open()
    counts = count_by_lane(open_pos)
    err_tail = tail_file(FILES["error"], 12)
    log_tail = tail_file(FILES["log"], 10)
    if err_tail.startswith("파일 없음") or err_tail.startswith("빈 파일"):
        err_tail = "오류 없음"
    lines = [
        f"📦 페이퍼봇 묶음점검 /pbatch",
        "",
        f"✅ 버전: {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재 모의보유: 전체 {len(open_pos)} / 정식 {counts.get('strict',0)} / 탈락복기 {counts.get('shadow',0)}",
        f"- 오늘/누적 종료결과: {closed_count()}건",
        "- 알림정책: 정식 모의매매급만 알림 / 탈락 후보 복기는 무알림 기록",
        f"- 최근 cycle: 진입 +{status.get('opened_this_cycle',0)} / 종료 +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"- 업데이트: {status.get('updated_at_text','-')}",
        f"- 내부 paper pause flag: {'ON' if FILES['flag'].exists() else 'OFF'}",
        f"- 가격조회: 빗썸 기준 / 없는 종목 skip {len(_BAD_MARKETS)}개",
        "",
        "📁 후보/결과 파일",
        f"- 전체 후보 기록: {fs.get('candidate_events',{}).get('lines',0)} lines",
        f"- 정식 모의매매: {fs.get('paper_candidates',{}).get('lines',0)} lines",
        f"- 탈락 후보 복기: {fs.get('shadow_candidates',{}).get('lines',0)} lines",
        f"- 현재 모의보유 파일: {'있음' if fs.get('open',{}).get('exists') else '없음'} / {fs.get('open',{}).get('size',0)} bytes",
        f"- 종료결과 파일: {fs.get('closed',{}).get('lines',0)} lines / {fs.get('closed',{}).get('size',0)} bytes",
        "",
        "⚠️ 오류",
        err_tail,
        "",
        "🧾 최근 로그",
        log_tail,
        "",
        "판독",
        "- 정식 후보가 없어도 탈락 후보 복기는 계속 모의매매로 쌓는다.",
        "- 복기용은 알림 없이 기록만 쌓고, 알림은 정식 모의매매급만 보낸다.",
        "- 이 결과를 보고 나중에 v340 조건을 만질지 판단한다.",
    ]
    return "\n".join(lines)


def set_paper_bot_menu(token: str) -> None:
    """텔레그램 메뉴탭 등록. 실패해도 실행에는 영향 없게 둔다."""
    try:
        commands = [
            {"command": "phelp", "description": "페이퍼봇 도움말"},
            {"command": "pbatch", "description": "전체 점검"},
            {"command": "batch", "description": "전체 점검"},
            {"command": "pstatus", "description": "현재 상태"},
            {"command": "ponce", "description": "1회 테스트"},
            {"command": "pstart", "description": "밤새 실행 시작"},
            {"command": "pstop", "description": "중지"},
            {"command": "prestart", "description": "재시작"},
            {"command": "plog", "description": "최근 로그"},
            {"command": "perror", "description": "오류 확인"},
        ]
        tg_api(token, "setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)})
        log("telegram command menu registered")
    except Exception as exc:
        log_error("set_paper_bot_menu", exc)

def notify_startup(token: str) -> None:
    chat_id = get_notify_chat_id()
    if not token or not chat_id:
        return
    try:
        send_message(token, chat_id, "\n".join([
            f"🧪 페이퍼봇 시작/재시작 알림",
            f"- 버전: {VERSION}",
            f"- 실행상태: {'ON' if load_control().get('running') else 'OFF'}",
            f"- OPEN: {len(load_open())} / CLOSED {closed_count()}",
            "- 가격조회: 빗썸 기준",
            "- 알림정책: 정식 모의매매급만 알림"
        ]))
    except Exception as exc:
        log_error("notify_startup", exc)


def handle_command(text: str) -> str:
    cmd = (text or "").strip().split()[0].lower()
    if cmd in ["/phelp", "/help", "/start"]:
        return (
            "🧪 페이퍼봇 명령어\n"
            "/pstatus - 상태\n"
            "/pbatch 또는 /batch - 전체 점검\n"
            "/ponce - 한 번만 모의매매 실행\n"
            "/pstart - 밤새 실행 시작\n"
            "/pstop - 실행 중지\n"
            "/prestart - 재시작\n"
            "/pfiles - 후보/결과 파일 확인\n"
            "/plog - 최근 로그\n"
            "/perror - 오류 로그\n"
            "/pflag_on - 메인봇 내부 paper pause flag 켜기\n"
            "/pflag_off - pause flag 끄기\n"
        )
    if cmd == "/pstatus":
        return summary_text()
    if cmd in ["/pbatch", "/batch"]:
        return build_pbatch_text()
    if cmd == "/ponce":
        status = run_cycle()
        return "✅ 1회 실행 완료\n" + summary_text()
    if cmd == "/pstart":
        save_control({"running": True})
        set_pause_flags(True)
        return "✅ 페이퍼봇 실행 ON\n메인봇 내부 paper pause flag도 켰어.\n" + summary_text()
    if cmd == "/pstop":
        save_control({"running": False})
        return "⏸ 페이퍼봇 실행 OFF\n" + summary_text()
    if cmd == "/prestart":
        save_control({"running": False})
        time.sleep(0.5)
        save_control({"running": True})
        set_pause_flags(True)
        return "🔁 페이퍼봇 재시작 완료\n" + summary_text()
    if cmd == "/pfiles":
        fs = file_stats()
        lines = ["📁 페이퍼봇 파일"]
        for k, v in fs.items():
            line = f"- {k}: {'있음' if v.get('exists') else '없음'} / {v.get('size',0)} bytes"
            if "lines" in v:
                line += f" / {v.get('lines')} lines"
            lines.append(line)
        return "\n".join(lines)
    if cmd == "/plog":
        return "🧾 최근 로그\n" + tail_file(FILES["log"], 30)
    if cmd == "/perror":
        body = tail_file(FILES["error"], 30)
        if body.startswith("파일 없음") or body.startswith("빈 파일"):
            body = "오류 없음"
        return "⚠️ 오류 로그\n" + body
    if cmd == "/pflag_on":
        set_pause_flags(True)
        return "✅ paper_bot pause flag 생성 완료\n메인봇 내부 paper pause 신호야."
    if cmd == "/pflag_off":
        try:
            set_pause_flags(False)
        except Exception:
            pass
        return "✅ paper_bot pause flag 제거 완료\n메인봇 내부 paper 유지/재개 신호야."
    return "모르는 명령어야. /phelp 를 보내줘."


def telegram_loop(token: str) -> None:
    global _update_offset
    allowed = os.environ.get("PAPER_BOT_ALLOWED_CHAT_ID", "").strip()
    log("telegram_loop started")
    send_ok_once = False
    while not _stop_event.is_set():
        try:
            params: Dict[str, Any] = {"timeout": 20}
            if _update_offset:
                params["offset"] = _update_offset
            res = tg_api(token, "getUpdates", params)
            if not res.get("ok"):
                time.sleep(2)
                continue
            for upd in res.get("result", []):
                _update_offset = int(upd.get("update_id", 0)) + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                if not chat_id or not text:
                    continue
                if allowed and str(chat_id) != allowed:
                    send_message(token, chat_id, "허용된 채팅방이 아니야.")
                    continue
                reply = handle_command(text)
                send_message(token, chat_id, reply)
            if not send_ok_once:
                send_ok_once = True
                log("telegram polling ok")
        except Exception as exc:
            log_error("telegram_loop", exc)
            time.sleep(3)
    log("telegram_loop stopped")


def write_pid() -> None:
    FILES["pid"].write_text(str(os.getpid()), encoding="utf-8")


def install_signal_handlers() -> None:
    def _handler(signum, frame):  # type: ignore[no-untyped-def]
        log(f"signal {signum} received")
        _stop_event.set()
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram-controlled paper trading bot")
    parser.add_argument("--once", action="store_true", help="run one paper cycle and exit")
    parser.add_argument("--status", action="store_true", help="print status and exit")
    parser.add_argument("--bot", action="store_true", help="start Telegram bot polling")
    args = parser.parse_args()

    if args.once:
        print(json.dumps(run_cycle(), ensure_ascii=False, indent=2))
        return 0
    if args.status:
        print(summary_text())
        return 0

    token = os.environ.get("PAPER_BOT_TOKEN", "").strip()
    if not token:
        print("PAPER_BOT_TOKEN 환경변수가 없어. BotFather 토큰을 export 한 뒤 실행해.")
        print("예: export PAPER_BOT_TOKEN='123:ABC'")
        return 2

    write_pid()
    install_signal_handlers()
    set_paper_bot_menu(token)
    set_pause_flags(True)
    notify_startup(token)
    worker = Thread(target=worker_loop, name="paper-worker", daemon=True)
    worker.start()
    telegram_loop(token)
    _stop_event.set()
    worker.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
