#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_bot_v0.22.py

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

VERSION = "paper_bot_v0.27"
BASE_DIR = Path(__file__).resolve().parent

TOKEN = os.getenv("PAPER_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", "")).strip()
# v0.23: 가드봇이 직접 실행할 때 PAPER_BOT_ALLOWED_CHAT_ID가 빠지는 경우가 있어
# GUARD_CHAT_ID까지 안전 fallback으로 사용한다.
ALLOWED_CHAT_ID = (
    os.getenv("PAPER_BOT_ALLOWED_CHAT_ID", "").strip()
    or os.getenv("PAPER_BOT_CHAT_ID", "").strip()
    or os.getenv("CHAT_ID", "").strip()
    or os.getenv("GUARD_CHAT_ID", "").strip()
)
NOTIFY_CHAT_ID = (
    os.getenv("PAPER_BOT_NOTIFY_CHAT_ID", "").strip()
    or ALLOWED_CHAT_ID
)

FILES = {
    "paper_candidates": BASE_DIR / "paper_candidates.jsonl",
    "shadow_candidates": BASE_DIR / "shadow_candidates.jsonl",
    "paper_latest": BASE_DIR / "paper_candidates_latest.jsonl",
    "shadow_latest": BASE_DIR / "shadow_candidates_latest.jsonl",
    "shadow_quarantine": BASE_DIR / "paper_bot_shadow_quarantine.jsonl",
    "legacy_strict_quarantine": BASE_DIR / "paper_bot_legacy_strict_quarantine.jsonl",
    "open": BASE_DIR / "paper_bot_open.json",
    "closed": BASE_DIR / "paper_bot_closed.jsonl",
    "status": BASE_DIR / "paper_bot_status.json",
    "control": BASE_DIR / "paper_bot_control.json",
    "baseline": BASE_DIR / "paper_eval_baseline_v167.json",
    "error": BASE_DIR / "paper_bot_error.log",
    "log": BASE_DIR / "paper_bot.log",
    "pid": BASE_DIR / "paper_bot.pid",
    "flag": BASE_DIR / "external_paper_bot_on.flag",
    "legacy_flag": BASE_DIR / "external_paper_runner_on.flag",
}

DEFAULT_CONTROL = {
    "running": False,
    "loop_seconds": 8,
    "max_open_strict": 30,  # v0.27: 후보를 자르는 값이 아니라 실제 자동매매식 보유 슬롯 보호값
    "max_open_shadow": 0,  # shadow는 복기 전용이라 OPEN하지 않는다
    "max_new_per_cycle": 0,  # 0 = 신규 후보를 작은 숫자로 자르지 않음
    "open_trade_ready_only": True,
    "track_strict_observe": True,
    "notify_on_strict_open": True,
    "notify_on_strict_close": True,
    "notify_on_shadow": False,
    "open_shadow_positions": False,  # v0.27 기본값: shadow는 OPEN하지 않고 복기 전용
    "shadow_review_only": True,
    "quarantine_shadow_open": True,
    "quarantine_legacy_strict_open": True,
    "consume_latest_candidate_files": True,
    "consume_latest_scan_only": True,
    "latest_scan_window_sec": 8,
    "fee_pct_roundtrip": 0.10,
    "take_profit_pct": 1.20,
    "protect_trigger_pct": 0.90,
    "protect_floor_pct": 0.20,
    "stop_loss_pct": -1.20,
    "slow_minutes": 20,
    "slow_peak_under_pct": 0.25,
    "time_exit_minutes": 120,
    "block_same_ticker_open": True,
    "candidate_ttl_sec": 120,
    "candidate_read_max_lines": 800,
}

# 구 control 파일에 남아 있을 수 있는 candidate_events 옵션은 v0.21에서 강제 무시한다.
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
    # v0.27 고정: candidate_events 소비 옵션은 저장돼 있어도 다시 만들지 않는다.
    for k in REMOVED_CONTROL_KEYS:
        control.pop(k, None)

    # v0.27: 후보 분석은 제한하지 않되, 실제 paper OPEN은 자동매매 검증 슬롯처럼 보호한다.
    # 구 control에 0/80/120/160/9999 같은 값이 남아 있으면 안전 기본값으로 정리한다.
    changed = False
    try:
        mos = int(float(control.get("max_open_strict", 30)))
        if mos <= 0 or mos in {80, 120, 160, 9999} or mos > 60:
            control["max_open_strict"] = 30
            changed = True
    except Exception:
        control["max_open_strict"] = 30
        changed = True
    try:
        if int(float(control.get("max_open_shadow", 0))) != 0:
            control["max_open_shadow"] = 0
            changed = True
    except Exception:
        control["max_open_shadow"] = 0
        changed = True
    try:
        mn = int(float(control.get("max_new_per_cycle", 0)))
        # v0.27: 9999 같은 구값은 “사실상 제한 없음”으로 보이지만 설정판을 헷갈리게 하므로 0으로 정리한다.
        if mn in {24, 80, 120, 160, 9999} or mn < 0:
            control["max_new_per_cycle"] = 0
            changed = True
    except Exception:
        control["max_new_per_cycle"] = 0
        changed = True
    if control.get("consume_latest_scan_only") is not True:
        control["consume_latest_scan_only"] = True
        changed = True
    if control.get("consume_latest_candidate_files") is not True:
        control["consume_latest_candidate_files"] = True
        changed = True
    if control.get("quarantine_legacy_strict_open") is not True:
        control["quarantine_legacy_strict_open"] = True
        changed = True
    if control.get("open_trade_ready_only") is not True:
        control["open_trade_ready_only"] = True
        changed = True
    if control.get("track_strict_observe") is not True:
        control["track_strict_observe"] = True
        changed = True
    try:
        # v0.27: paper_bot은 *_latest 후보파일을 우선 읽는다. 큰 archive 파일을 매 cycle 읽지 않는다.
        if int(float(control.get("candidate_read_max_lines", 0))) > 1200 or int(float(control.get("candidate_read_max_lines", 0))) <= 0:
            control["candidate_read_max_lines"] = 800
            changed = True
    except Exception:
        control["candidate_read_max_lines"] = 800
        changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
        except Exception:
            pass
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




def ensure_eval_baseline() -> Dict[str, Any]:
    data = load_json(FILES["baseline"], {})
    if isinstance(data, dict) and float_any(data.get("baseline_ts"), default=0.0) > 0:
        return data
    data = {
        "schema": "paper_eval_baseline_v157",
        "created_by": VERSION,
        "baseline_ts": now(),
        "baseline_text": iso_ts(),
        "closed_lines_at_baseline": line_count(FILES["closed"]),
        "paper_lines_at_baseline": line_count(FILES["paper_candidates"]),
        "shadow_lines_at_baseline": line_count(FILES["shadow_candidates"]),
        "note": "기존 OPEN/CLOSED는 삭제하지 않고, 기준점 이후 신규만 분리 표시한다.",
    }
    save_json(FILES["baseline"], data)
    return data


def baseline_ts() -> float:
    return float_any(ensure_eval_baseline().get("baseline_ts"), default=0.0)


def row_ts(row: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = float_any((row or {}).get(k), default=0.0)
        if v > 0:
            return v
    return 0.0


def rows_since_baseline(rows: Iterable[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    bts = baseline_ts()
    return [r for r in rows or [] if row_ts(r, *keys) >= bts]


def open_split_since_baseline(open_pos: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[int, int, Dict[str, int], Dict[str, int]]:
    open_pos = open_pos if isinstance(open_pos, dict) else load_open()
    bts = baseline_ts()
    new_counts = {"strict": 0, "shadow": 0}
    old_counts = {"strict": 0, "shadow": 0}
    for p in (open_pos or {}).values():
        lane = str((p or {}).get("lane") or "shadow")
        target = new_counts if float_any((p or {}).get("opened_at"), default=0.0) >= bts else old_counts
        target[lane] = target.get(lane, 0) + 1
    return sum(new_counts.values()), sum(old_counts.values()), new_counts, old_counts


def candidate_is_fresh(ev: Dict[str, Any], control: Dict[str, Any]) -> bool:
    nowv = now()
    exp = float_any((ev or {}).get("expires_at"), default=0.0)
    created = float_any((ev or {}).get("created_at"), default=0.0)
    ttl = float_any(control.get("candidate_ttl_sec"), default=120.0)
    if exp > 0:
        return exp >= nowv
    if created > 0:
        return (nowv - created) <= ttl
    return False


def candidate_fresh_stats(path: Path, control: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    control = control or load_control()
    rows = read_jsonl(path, max_lines=int(float_any(control.get("candidate_read_max_lines"), default=2500)))
    fresh = sum(1 for r in rows if candidate_is_fresh(r, control))
    return {"read_window": len(rows), "fresh": fresh, "expired": max(0, len(rows) - fresh)}

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


def candidate_input_path(lane: str) -> Path:
    """paper_bot은 작은 latest 파일을 우선 소비한다. 없으면 기존 archive를 fallback으로만 읽는다."""
    try:
        if lane == "strict" and load_control().get("consume_latest_candidate_files", True) and FILES.get("paper_latest", Path("")) .exists():
            return FILES["paper_latest"]
        if lane == "shadow" and load_control().get("consume_latest_candidate_files", True) and FILES.get("shadow_latest", Path("")) .exists():
            return FILES["shadow_latest"]
    except Exception:
        pass
    return FILES["paper_candidates"] if lane == "strict" else FILES["shadow_candidates"]


def quarantine_shadow_open_positions(open_pos: Dict[str, Dict[str, Any]], control: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """기존 shadow OPEN을 삭제하지 않고 격리 파일로 보존한 뒤 active OPEN에서 제외한다.
    shadow는 복기 전용이므로 더 이상 paper_bot cycle을 200~300개씩 붙잡지 않게 한다.
    """
    if not control.get("quarantine_shadow_open", True) or control.get("open_shadow_positions", False):
        return open_pos, 0
    kept: Dict[str, Dict[str, Any]] = {}
    moved = 0
    ts = now()
    for pid, pos in (open_pos or {}).items():
        lane = str((pos or {}).get("lane") or "shadow")
        if lane == "shadow":
            row = dict(pos or {})
            row["quarantined_at"] = ts
            row["quarantined_at_text"] = iso_ts(ts)
            row["quarantine_reason"] = "v0.27_shadow_review_only"
            append_jsonl(FILES["shadow_quarantine"], row)
            moved += 1
        else:
            kept[pid] = pos
    return kept, moved




def quarantine_legacy_strict_open_positions(open_pos: Dict[str, Dict[str, Any]], control: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """v0.27: trade_ready 분리 전 열렸던 기존 strict OPEN은 삭제하지 않고 격리한다.
    목적은 새 기준 이후 trade_ready만 깨끗하게 모의매매하기 위한 active OPEN 정리다.
    """
    if not control.get("quarantine_legacy_strict_open", True):
        return open_pos, 0
    kept: Dict[str, Dict[str, Any]] = {}
    moved = 0
    ts = now()
    bts = baseline_ts()
    for pid, pos in (open_pos or {}).items():
        lane = str((pos or {}).get("lane") or "shadow")
        opened_at = float_any((pos or {}).get("opened_at"), default=0.0)
        is_trade_ready = bool((pos or {}).get("trade_ready") or (pos or {}).get("paper_bot_open") or (pos or {}).get("open_eligible"))
        # v0.26 이전에 strict로 열린 기존분이 active 슬롯을 막는 문제를 방지한다.
        if lane == "strict" and (opened_at < bts or not is_trade_ready):
            row = dict(pos or {})
            row["quarantined_at"] = ts
            row["quarantined_at_text"] = iso_ts(ts)
            row["quarantine_reason"] = "v0.27_legacy_strict_before_trade_ready_split"
            append_jsonl(FILES["legacy_strict_quarantine"], row)
            moved += 1
        else:
            kept[pid] = pos
    return kept, moved

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




def fetch_all_bithumb_prices(timeout: float = 4.0) -> Dict[str, float]:
    """한 cycle에서 OPEN 수백 개를 종목별 REST로 때리지 않기 위한 전체 현재가 캐시.
    실패하면 빈 dict를 반환하고 기존 개별 조회 fallback을 사용한다.
    """
    try:
        url = "https://api.bithumb.com/public/ticker/ALL_KRW"
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        rows = data.get("data") if isinstance(data, dict) else {}
        out: Dict[str, float] = {}
        if isinstance(rows, dict):
            for k, v in rows.items():
                if str(k).upper() == "DATE" or not isinstance(v, dict):
                    continue
                sym = normalize_ticker(k)
                px = float_any(v.get("closing_price"), v.get("trade_price"), v.get("close"), default=0.0)
                if sym and px > 0:
                    out[sym] = px
        return out
    except Exception as exc:
        log(f"bulk_price_fetch_failed: {exc.__class__.__name__}")
        return {}

_CYCLE_PRICE_MAP: Dict[str, float] = {}

def cycle_live_price(ticker: Any) -> Optional[float]:
    sym = normalize_ticker(ticker)
    if not sym:
        return None
    px = _CYCLE_PRICE_MAP.get(sym)
    if px and px > 0:
        return px
    return fetch_bithumb_price(sym)

def latest_scan_filter(events: List[Dict[str, Any]], control: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """고정 갯수 제한이 아니라 최신 scan 묶음만 소비한다.
    후보파일에 과거 TTL 유효 후보가 많이 남아 있어도 paper_bot이 한꺼번에 몰아먹지 않게 한다.
    scan_id가 있으면 최신 scan_id만, 없으면 최신 created_at 근처만 사용한다.
    """
    meta = {"enabled": bool(control.get("consume_latest_scan_only", True)), "latest_scan_id": "", "latest_ts": 0.0, "before": len(events), "after": len(events)}
    if not meta["enabled"] or not events:
        return events, meta
    # scan_id가 있는 새 후보는 scan_id 기준이 가장 안전하다.
    scan_rows = [e for e in events if e.get("scan_id")]
    if scan_rows:
        def key(e: Dict[str, Any]):
            return (float_any(e.get("created_at"), default=0.0), str(e.get("scan_id") or ""))
        latest = max(scan_rows, key=key)
        sid = str(latest.get("scan_id") or "")
        out = [e for e in events if str(e.get("scan_id") or "") == sid]
        meta.update({"latest_scan_id": sid, "latest_ts": float_any(latest.get("created_at"), default=0.0), "after": len(out)})
        return out, meta
    # 구 후보는 created_at 최신 묶음만 허용한다.
    latest_ts = max(float_any(e.get("created_at"), default=0.0) for e in events)
    win = max(1.0, float_any(control.get("latest_scan_window_sec"), default=8.0))
    out = [e for e in events if float_any(e.get("created_at"), default=0.0) >= latest_ts - win]
    meta.update({"latest_ts": latest_ts, "after": len(out)})
    return out, meta

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
        "stale_skip": 0,
        "strict_observe_skip": 0,
        "not_trade_ready_skip": 0,
    }
    lane_counts = count_by_lane(open_pos)
    max_strict = int(float_any(control.get("max_open_strict"), default=0))
    max_shadow = int(float_any(control.get("max_open_shadow"), default=0))
    max_new = int(float_any(control.get("max_new_per_cycle"), default=0))
    # 0 이하는 고정 갯수 제한 없음. paper_bot은 전략 판단을 하지 않고, TTL 유효 후보를 장부에 태운다.
    strict_cap_on = max_strict > 0
    shadow_cap_on = max_shadow > 0
    new_cap_on = max_new > 0

    def try_pick(ev: Dict[str, Any], lane: str) -> None:
        if new_cap_on and len(picked) >= max_new:
            return
        if strict_cap_on and lane == "strict" and lane_counts.get("strict", 0) >= max_strict:
            stats["limit_skip"] += 1
            return
        if shadow_cap_on and lane == "shadow" and lane_counts.get("shadow", 0) >= max_shadow:
            stats["limit_skip"] += 1
            return
        if not candidate_is_fresh(ev, control):
            stats["stale_skip"] += 1
            return
        if lane == "strict" and control.get("open_trade_ready_only", True):
            can_open = bool(ev.get("paper_bot_open") or ev.get("open_eligible") or ev.get("trade_ready"))
            if not can_open:
                stats["strict_observe_skip"] += 1
                stats["not_trade_ready_skip"] += 1
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

    read_max = int(float_any(control.get("candidate_read_max_lines"), default=2500))
    paper_events = read_jsonl(candidate_input_path("strict"), max_lines=read_max)
    shadow_events = read_jsonl(candidate_input_path("shadow"), max_lines=read_max)
    open_shadow = bool(control.get("open_shadow_positions", False))
    combined: List[Tuple[str, Dict[str, Any]]] = [("strict", ev) for ev in paper_events]
    if open_shadow:
        combined += [("shadow", ev) for ev in shadow_events]
    else:
        # v0.27: shadow는 복기 전용. OPEN 장부에 태우지 않는다.
        stats["shadow_file"] = len(shadow_events)
        stats["shadow_review_only_skip"] = len(shadow_events)
    filtered_events, meta = latest_scan_filter([dict(ev, _lane_hint=lane) for lane, ev in combined], control)
    stats["latest_scan_before"] = int(meta.get("before", len(combined)))
    stats["latest_scan_after"] = int(meta.get("after", len(filtered_events)))
    stats["latest_scan_id"] = str(meta.get("latest_scan_id") or "")

    # 최신 scan 묶음 안에서는 strict만 OPEN한다. shadow는 파일에는 남기되 복기 전용이다.
    for ev in filtered_events:
        if new_cap_on and len(picked) >= max_new:
            break
        lane = str(ev.pop("_lane_hint", ev.get("lane") or "shadow"))
        if lane == "shadow" and not open_shadow:
            stats["shadow_review_only_skip"] = stats.get("shadow_review_only_skip", 0) + 1
            continue
        if lane == "strict":
            stats["paper_file"] += 1
        else:
            stats["shadow_file"] += 1
        try_pick(ev, lane)

    # candidate_events는 v0.21에서 완전 제거. 읽지도 세지도 않는다.
    stats["events_file"] = 0
    return picked, stats


def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:
    ticker = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol")) or "UNKNOWN"
    detected_price = get_event_price(ev)
    live_price = cycle_live_price(ticker) or detected_price
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
        "trade_ready": bool(ev.get("trade_ready") or ev.get("paper_bot_open") or ev.get("open_eligible")),
        "trade_ready_label": ev.get("trade_ready_label") or "",
        "trade_ready_reasons": ev.get("trade_ready_reasons") or [],
        "score": float_any(ev.get("score"), ev.get("leader_score"), default=0.0),
        "edge": float_any(ev.get("edge"), ev.get("edge_score"), default=0.0),
        "reason": ev.get("reason") or ev.get("why") or ev.get("block_reason") or "",
        "source_created_at": float_any(ev.get("created_at"), default=0.0),
        "source_expires_at": float_any(ev.get("expires_at"), default=0.0),
        "baseline_tag": "new" if now() >= baseline_ts() else "old",
        "raw": ev,
    }


def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    ticker = pos.get("ticker") or ""
    entry = float_any(pos.get("entry_price"), default=0.0)
    if entry <= 0:
        return pos, None
    price = cycle_live_price(ticker) or float_any(pos.get("current_price"), pos.get("entry_price"), default=entry)
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
        "scan_id": (pos.get("raw") or {}).get("scan_id") if isinstance(pos.get("raw"), dict) else "",
        "auto_ready_level": (pos.get("raw") or {}).get("auto_ready_level") if isinstance(pos.get("raw"), dict) else "",
        "auto_ready_label": (pos.get("raw") or {}).get("auto_ready_label") if isinstance(pos.get("raw"), dict) else "",
        "liquidity_grade": (pos.get("raw") or {}).get("liquidity_grade") if isinstance(pos.get("raw"), dict) else "",
        "slippage_risk": (pos.get("raw") or {}).get("slippage_risk") if isinstance(pos.get("raw"), dict) else "",
        "tick_risk": (pos.get("raw") or {}).get("tick_risk") if isinstance(pos.get("raw"), dict) else "",
        "chase_risk": (pos.get("raw") or {}).get("chase_risk") if isinstance(pos.get("raw"), dict) else "",
        "source_created_at": pos.get("source_created_at"),
        "source_expires_at": pos.get("source_expires_at"),
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
    for name in ["paper_candidates", "shadow_candidates", "paper_latest", "shadow_latest", "shadow_quarantine", "legacy_strict_quarantine", "open", "closed", "status", "error", "log"]:
        p = FILES[name]
        info = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}
        if p.suffix == ".jsonl" and p.exists():
            info["lines"] = line_count(p)
        out[name] = info
    return out


def run_cycle() -> Dict[str, Any]:
    global _CYCLE_PRICE_MAP
    started = now()
    control = load_control()
    # v0.23: 종목별 현재가 호출 수백 개를 막고, 한 cycle에 ALL_KRW 1회로 OPEN 갱신한다.
    _CYCLE_PRICE_MAP = fetch_all_bithumb_prices()
    with _state_lock:
        open_pos = load_open()
        open_pos, quarantined_shadow = quarantine_shadow_open_positions(open_pos, control)
        open_pos, quarantined_legacy_strict = quarantine_legacy_strict_open_positions(open_pos, control)
        if quarantined_shadow or quarantined_legacy_strict:
            save_open(open_pos)
            if quarantined_shadow:
                log(f"shadow_quarantine moved={quarantined_shadow}")
            if quarantined_legacy_strict:
                log(f"legacy_strict_quarantine moved={quarantined_legacy_strict}")
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
        new_open_total, old_open_total, new_open_counts, old_open_counts = open_split_since_baseline(open_pos)
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
            "open_new_total": new_open_total,
            "open_old_total": old_open_total,
            "open_new_counts": new_open_counts,
            "open_old_counts": old_open_counts,
            "closed_total": closed_count(),
            "closed_new_total": len(rows_since_baseline(read_jsonl(FILES["closed"], max_lines=20000), "closed_at")),
            "quarantined_shadow_this_cycle": int(locals().get("quarantined_shadow", 0)),
            "pick_stats": pick_stats,
            "counts_before": counts_before,
            "elapsed_sec": round(now() - started, 3),
            "files": file_stats(),
            "flag_exists": FILES["flag"].exists(),
            "candidate_events_consumed": 0,
            "candidate_events_note": "v0.21: candidate_events 읽기/소비 없음",
        }
        save_json(FILES["status"], status)
        notify_strict_events(opened_items, closed_items, control)
        log(f"cycle opened={opened} closed={len(closed_items)} open={len(open_pos)} elapsed={status['elapsed_sec']}s")
        return status



def heartbeat_log(where: str = "loop") -> None:
    global _LAST_HEARTBEAT_LOG_TS
    try:
        ts = now()
        if ts - float_any(_LAST_HEARTBEAT_LOG_TS, default=0.0) < HEARTBEAT_LOG_SEC:
            return
        _LAST_HEARTBEAT_LOG_TS = ts
        control = load_control()
        open_pos = load_open()
        log(f"heartbeat {where} running={bool(control.get('running'))} open={len(open_pos)} closed={closed_count()}")
    except Exception:
        pass


def write_startup_status() -> None:
    try:
        control = load_control()
        open_pos = load_open()
        counts = count_by_lane(open_pos)
        save_json(FILES["status"], {
            "version": VERSION,
            "updated_at": now(),
            "updated_at_text": iso_ts(),
            "running": bool(control.get("running")),
            "loop_seconds": control.get("loop_seconds"),
            "open_total": len(open_pos),
            "open_strict": counts.get("strict", 0),
            "open_shadow": counts.get("shadow", 0),
            "closed_total": closed_count(),
            "startup_status": True,
        })
    except Exception as exc:
        log_error("write_startup_status", exc)


def worker_loop() -> None:
    log("worker_loop started")
    while not _stop_event.is_set():
        try:
            control = load_control()
            heartbeat_log("worker")
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




def closed_ts(row: Dict[str, Any]) -> float:
    ts = float_any(row.get("closed_at"), default=0.0)
    if ts > 0:
        return ts
    text = str(row.get("closed_at_text") or "")
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def bucket_date(row: Dict[str, Any]) -> str:
    ts = closed_ts(row)
    return datetime.fromtimestamp(ts).strftime("%m-%d") if ts > 0 else "날짜모름"


def bucket_hour3(row: Dict[str, Any]) -> str:
    ts = closed_ts(row)
    if ts <= 0:
        return "시간모름"
    dt = datetime.fromtimestamp(ts)
    start = (dt.hour // 3) * 3
    end = (start + 3) % 24
    return f"{start:02d}-{end:02d}시"


def bucket_score(row: Dict[str, Any]) -> str:
    score = float_any(row.get("score"), default=0.0)
    if score >= 4.0:
        return "점수 4.0+"
    if score >= 3.0:
        return "점수 3.0~4.0"
    if score >= 2.0:
        return "점수 2.0~3.0"
    if score > 0:
        return "점수 0~2.0"
    return "점수없음"


def bucket_auto_ready(row: Dict[str, Any]) -> str:
    val = str(row.get("auto_ready_level") or row.get("auto_ready_label") or "").strip()
    return val or bucket_score(row)


def group_table(title: str, rows: Iterable[Dict[str, Any]], key_fn, limit: int = 8) -> str:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        groups.setdefault(str(key_fn(r) or "기타"), []).append(r)
    lines = [title]
    if not groups:
        lines.append("- 아직 데이터 없음")
        return "\n".join(lines)
    for key, arr in sorted(groups.items(), key=lambda x: (x[0] if ("시" in x[0] or "-" in x[0]) else f"{999999-len(x[1]):06d}_{x[0]}"))[:limit]:
        st = score_stats(arr)
        icon = "⚠️" if 0 < st["n"] < 20 else ("✅" if st["avg"] > 0 else "❌")
        note = " / 표본적음" if 0 < st["n"] < 20 else ""
        lines.append(f"{icon} {key}: {st['n']}전 / 승률 {st['win_rate']:.1f}% / 평균 {st['avg']:+.2f}% / 합산 {st['total']:+.2f}%{note}")
    return "\n".join(lines)


def easy_judge_text() -> str:
    return "\n".join([
        "판독",
        "✅ 평균이 계속 +인 시간대/등급은 살릴 후보",
        "⚠️ 20건 미만은 참고만",
        "❌ 반복 손실 구간은 조건 조정 후보",
        "❔ 지금은 새 전략 추가보다 어디가 좋은지 나눠 보는 단계",
    ])

def summary_text() -> str:
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    counts = count_by_lane(open_pos)
    new_open_total, old_open_total, new_open_counts, old_open_counts = open_split_since_baseline(open_pos)
    fs = file_stats()
    err_size = fs.get("error", {}).get("size", 0)
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    p_fresh = candidate_fresh_stats(candidate_input_path("strict"), control)
    s_fresh = candidate_fresh_stats(candidate_input_path("shadow"), control)
    new_closed = rows_since_baseline(read_jsonl(FILES["closed"], max_lines=20000), "closed_at")
    base = ensure_eval_baseline()
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- OPEN: 전체 {len(open_pos)} / 정식 {counts.get('strict',0)} / 기존복기 {counts.get('shadow',0)}",
        f"  · 신규 {new_open_total}(정식 {new_open_counts.get('strict',0)} / 복기 {new_open_counts.get('shadow',0)}) / 기존 {old_open_total}(정식 {old_open_counts.get('strict',0)} / 복기 {old_open_counts.get('shadow',0)})",
        f"- CLOSED: 전체 {closed_count()}건 / 신규기준 이후 {len(new_closed)}건",
        f"- 기준점: {base.get('baseline_text','-')} / 기존 기록 삭제 없음",
        f"- 소비파일: 정식 latest {fs.get('paper_latest',{}).get('lines',0)} lines(TTL유효 {p_fresh.get('fresh',0)}) / 복기 latest {fs.get('shadow_latest',{}).get('lines',0)} lines(TTL유효 {s_fresh.get('fresh',0)}) / shadow 격리 {fs.get('shadow_quarantine',{}).get('lines',0)} / 기존strict 격리 {fs.get('legacy_strict_quarantine',{}).get('lines',0)}",
        f"- 처리: trade_ready만 신규 OPEN / shadow 복기전용 / 기존 shadow·기존 strict 격리 보존",
        f"- 이번 cycle: open +{status.get('opened_this_cycle',0)} / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"- pick: paper {pick.get('paper_file',0)} / trade_ready아님 {pick.get('strict_observe_skip',0)} / shadow복기 {pick.get('shadow_review_only_skip',0)} / dup {pick.get('dup_skip',0)} / stale {pick.get('stale_skip',0)} / same_ticker {pick.get('same_ticker_skip',0)} / bad {pick.get('bad_skip',0)}",
        f"- 최신 scan 소비: before {pick.get('latest_scan_before',0)} → after {pick.get('latest_scan_after',0)} / scan_id {pick.get('latest_scan_id','-') or '-'}",
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
        tag = "신규" if float_any(pos.get("opened_at"), default=0.0) >= baseline_ts() else "기존"
        lines.append(f"- {short_ticker(pos.get('ticker'))} / {tag} / {pos.get('lane','-')} / 진입 {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / {float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / {age:.1f}분")
    return "\n".join(lines)


def files_text() -> str:
    fs = file_stats()
    c = load_control()
    pf = candidate_fresh_stats(candidate_input_path("strict"), c)
    sf = candidate_fresh_stats(candidate_input_path("shadow"), c)
    return "\n".join([
        "📁 페이퍼봇 파일 /pfiles",
        f"- paper_latest: {fs.get('paper_latest',{}).get('lines',0)} lines / TTL유효 {pf.get('fresh',0)} / archive {fs.get('paper_candidates',{}).get('lines',0)} lines",
        f"- shadow_latest: {fs.get('shadow_latest',{}).get('lines',0)} lines / TTL유효 {sf.get('fresh',0)} / archive {fs.get('shadow_candidates',{}).get('lines',0)} lines",
        f"- shadow_quarantine: {fs.get('shadow_quarantine',{}).get('lines',0)} lines",
        f"- legacy_strict_quarantine: {fs.get('legacy_strict_quarantine',{}).get('lines',0)} lines",
        f"- open: {fs.get('open',{}).get('size',0)} bytes",
        f"- closed: {fs.get('closed',{}).get('lines',0)} lines / {fs.get('closed',{}).get('size',0)} bytes",
        "- candidate_events: 소비 안 함 / 읽지 않음",
    ])


def control_text() -> str:
    c = load_control()
    keys = ["running", "loop_seconds", "max_open_strict", "max_open_shadow", "max_new_per_cycle", "open_trade_ready_only", "track_strict_observe", "candidate_ttl_sec", "candidate_read_max_lines", "consume_latest_scan_only", "consume_latest_candidate_files", "quarantine_shadow_open", "quarantine_legacy_strict_open", "latest_scan_window_sec", "fee_pct_roundtrip", "take_profit_pct", "protect_trigger_pct", "protect_floor_pct", "stop_loss_pct", "slow_minutes", "time_exit_minutes", "block_same_ticker_open"]
    lines = ["⚙️ 페이퍼봇 설정 /pcontrol"]
    for k in keys:
        lines.append(f"- {k}: {c.get(k)}")
    lines.append("- allow_shadow_from_candidate_events: 제거됨/무시됨")
    lines.append("- 분석 원칙: 후보는 줄이지 않고, paper OPEN은 trade_ready만")
    lines.append("- max_open_strict: 후보 제한이 아니라 실제 자동매매식 보유 슬롯 보호값")
    lines.append("- 과거 후보 몰아먹기 방지: consume_latest_scan_only=True이면 최신 scan 묶음만 소비")
    return "\n".join(lines)


def _command_lines(text: str) -> List[str]:
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip().startswith("/")]


def _cmd_name(line: str) -> str:
    first = line.strip().split()[0].lower() if line.strip().split() else ""
    if first.startswith("/"):
        first = first[1:]
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


def command_response(text: str) -> str:
    cmd = (text or "").strip().split()[0].lower() if (text or "").strip().split() else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    if cmd in {"/phelp", "/start"}:
        return "\n".join([
            f"🧪 {VERSION}",
            "명령어: /pbatch /pstatus /pcheck /ponce /pstart /pstop /prestart /plog /perror /pscore /popen /pfiles /pcontrol",
            "여러 명령어를 줄바꿈으로 보내도 자동 묶음 처리",
            "소비파일: paper_candidates_latest 우선 / shadow는 복기 전용",
            "candidate_events는 읽지 않음",
        ])
    if cmd == "/pstatus":
        return summary_text()
    if cmd == "/pcheck":
        return "✅ 페이퍼봇 점검 /pcheck\n\n" + summary_text() + "\n\n" + files_text()
    if cmd == "/pbatch":
        rows = read_jsonl(FILES["closed"], max_lines=8000)
        new_rows = rows_since_baseline(rows, "closed_at")
        strict = [r for r in rows if str(r.get("lane")) == "strict"]
        shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
        return "\n".join([
            "📦 페이퍼봇 묶음 /pbatch",
            "", "[1/5] 상태", summary_text(),
            "", "[2/5] 신규 성과", fmt_stats("신규 strict", [r for r in new_rows if str(r.get("lane")) == "strict"]), fmt_stats("신규 전체 참고", new_rows),
            "", "[3/5] 누적 성과", fmt_stats("전체", rows), fmt_stats("정식 strict", strict), fmt_stats("복기 shadow 참고", shadow),
            "", "[4/5] 파일/TTL", files_text(),
            "", "[5/5] 구조", "- strict만 OPEN", "- shadow는 복기전용/기존 shadow OPEN은 격리 보존", "- candidate_events 소비 0 / fallback 없음",
        ])
    if cmd == "/ponce":
        run_cycle()
        return "✅ 1회 cycle 완료\n" + summary_text()
    if cmd == "/pstart":
        save_control({"running": True})
        return "✅ paper_bot 실행 ON\n" + summary_text()
    if cmd == "/pstop":
        save_control({"running": False})
        return "⏸ paper_bot 실행 OFF\n" + summary_text()
    if cmd == "/prestart":
        save_control({"running": False}); time.sleep(0.5); save_control({"running": True})
        return "🔁 paper_bot 논리 재시작 완료\n" + summary_text()
    if cmd == "/plog":
        return "🧾 paper_bot.log\n\n" + tail_file(FILES["log"], 60)[-3500:]
    if cmd == "/perror":
        return "🧯 paper_bot_error.log\n\n" + tail_file(FILES["error"], 80)[-3500:]
    if cmd == "/pscore":
        rows = read_jsonl(FILES["closed"], max_lines=8000)
        new_rows = rows_since_baseline(rows, "closed_at")
        strict = [r for r in rows if str(r.get("lane")) == "strict"]
        shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
        new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
        by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in new_rows)
        lines = ["📊 페이퍼봇 성과 /pscore", "", "[1/7] 신규 요약", fmt_stats("신규 strict", new_strict), fmt_stats("신규 전체 참고", new_rows), "⚠️ 자동매매 판단은 strict 중심. shadow는 복기 참고", "", "[2/7] 시간대별 3시간 묶음", group_table("- 00/03/06/09/12/15/18/21시 구간", new_strict, bucket_hour3, limit=8), "", "[3/7] 등급별", group_table("- strict 중심", new_rows, lambda r: str(r.get("lane") or "unknown"), limit=6), "", "[4/7] 자동매매 준비등급/점수대", group_table("- auto_ready 또는 점수대", new_strict, bucket_auto_ready, limit=8), "", "[5/7] 날짜별", group_table("- 날짜별 신규 strict CLOSED", new_strict, bucket_date, limit=10), "", "[6/7] 청산사유"]
        if by_reason:
            for k, v in by_reason.most_common(10):
                sub = [r for r in new_rows if str(r.get("exit_reason") or "unknown") == k]
                lines.append(fmt_stats(k, sub))
        else:
            lines.append("- 아직 신규 CLOSED 없음")
        lines += ["", "[7/7] 누적 참고", fmt_stats("전체", rows), fmt_stats("정식 strict", strict), fmt_stats("복기 shadow 참고", shadow), "", easy_judge_text()]
        return "\n".join(lines)
    if cmd == "/popen":
        return "📂 페이퍼봇 OPEN /popen\n\n" + format_open_list(limit=30)
    if cmd == "/pfiles":
        return files_text()
    if cmd == "/pcontrol":
        return control_text()
    return "알 수 없는 명령. /phelp"


def handle_command(chat_id: str, text: str) -> None:
    lines = _command_lines(text)
    if len(lines) > 1:
        send_chat(chat_id, f"📦 자동 묶음 명령 접수\n- /pbatch 없이 여러 줄 명령을 감지\n- 실행 {len(lines)}개")
        total = len(lines)
        for idx, line in enumerate(lines, start=1):
            name = _cmd_name(line)
            if name == "pbatch":
                body = "이미 자동 묶음 처리 중이라 /pbatch는 건너뜀"
            else:
                try:
                    body = command_response(line)
                except Exception as exc:
                    log_error(f"multi:{name}", exc)
                    body = f"오류: {exc.__class__.__name__}: {exc}"
            send_chat(chat_id, f"[{idx}/{total}] /{name}\n" + body)
        return
    send_chat(chat_id, command_response(text))


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
    ensure_eval_baseline()
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot", action="store_true", help="run telegram + worker")
    parser.add_argument("--once", action="store_true", help="run one paper cycle")
    args = parser.parse_args()
    write_pid()
    write_startup_status()
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
    log(f"{VERSION} started pid={os.getpid()} token={'yes' if TOKEN else 'no'} chat={'yes' if ALLOWED_CHAT_ID else 'no'} env_chat_keys PAPER={bool(os.getenv('PAPER_BOT_ALLOWED_CHAT_ID'))} CHAT={bool(os.getenv('CHAT_ID'))} GUARD={bool(os.getenv('GUARD_CHAT_ID'))}")
    while not _stop_event.is_set():
        time.sleep(1)
    log(f"{VERSION} stopped")


if __name__ == "__main__":
    main()
