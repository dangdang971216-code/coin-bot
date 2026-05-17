#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_bot_v0.43.py

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

VERSION = "paper_bot_v0.45"
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
    "alert_state": BASE_DIR / "paper_bot_alert_state.json",
    "micro_urgent": BASE_DIR / "clean_micro_urgent_targets.json",
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
    "notify_real_trades_only": True,
    "notify_open_auto_ready_only": True,
    "notify_open_min_score": 4.45,
    "notify_open_require_final_pass": True,
    "notify_open_require_micro_fresh": True,
    "preopen_micro_recheck": True,
    "preopen_micro_urgent_ttl_sec": 35,
    "preopen_micro_recheck_max_targets": 40,
    "notify_open_require_ws_fresh": False,
    "notify_close_only_alerted": True,
    "notify_recheck_summary": True,
    "recheck_summary_interval_sec": 3600,
    "recheck_summary_max_rows": 8,
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
_STOP_REASON = "running"
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
            row["quarantine_reason"] = "v0.28_shadow_review_only"
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
            row["quarantine_reason"] = "v0.28_legacy_strict_before_trade_ready_split"
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




def _fetch_all_bithumb_prices_direct(timeout: float = 4.0) -> Dict[str, float]:
    """Bithumb ALL_KRW direct fetch. v0.45 cached wrapper below uses this only when TTL expired."""
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


def fetch_all_bithumb_prices(timeout: float = 4.0) -> Dict[str, float]:
    """v0.45: cycle마다 ALL_KRW를 무조건 치던 경로를 TTL 캐시로 정리한다.

    장부/청산조건은 그대로 유지한다. 네트워크가 느린 순간에는 직전 가격맵을 쓰고,
    그래도 없을 때만 빈 dict를 반환해 기존 개별 조회 fallback을 탄다.
    """
    nowv = now()
    try:
        cached = _PRICE_MAP_CACHE.get("rows") if isinstance(_PRICE_MAP_CACHE.get("rows"), dict) else {}
        if cached and nowv - float_any(_PRICE_MAP_CACHE.get("ts"), default=0.0) <= CYCLE_PRICE_CACHE_TTL_SEC:
            return dict(cached)
        rows = _fetch_all_bithumb_prices_direct(timeout=timeout)
        if rows:
            _PRICE_MAP_CACHE.update({"ts": nowv, "rows": dict(rows), "source": "fresh"})
            return rows
        if cached:
            _PRICE_MAP_CACHE["source"] = "stale_after_fetch_fail"
            return dict(cached)
        return {}
    except Exception as exc:
        log_error("fetch_all_prices_cached", exc)
        cached = _PRICE_MAP_CACHE.get("rows") if isinstance(_PRICE_MAP_CACHE.get("rows"), dict) else {}
        return dict(cached) if cached else {}

_CYCLE_PRICE_MAP: Dict[str, float] = {}

_PRICE_MAP_CACHE: Dict[str, Any] = {"ts": 0.0, "rows": {}}
CYCLE_PRICE_CACHE_TTL_SEC = float(os.getenv("PAPER_BOT_PRICE_CACHE_TTL_SEC", "12"))
_CLOSED_NEW_TOTAL_CACHE: Dict[str, Any] = {"ts": 0.0, "count": None, "file_size": -1, "mtime": 0.0}
CLOSED_NEW_TOTAL_CACHE_TTL_SEC = float(os.getenv("PAPER_BOT_CLOSED_NEW_CACHE_TTL_SEC", "60"))
_FILE_STATS_CACHE: Dict[str, Any] = {"ts": 0.0, "stats": None}
FILE_STATS_CACHE_TTL_SEC = float(os.getenv("PAPER_BOT_FILE_STATS_CACHE_TTL_SEC", "45"))


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


def _ctx_from_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance((ev or {}).get("entry_context"), dict):
        return (ev or {}).get("entry_context") or {}
    if isinstance((ev or {}).get("profile"), dict):
        return (ev or {}).get("profile") or {}
    return ev if isinstance(ev, dict) else {}


def _event_micro_is_fresh(ev: Dict[str, Any]) -> bool:
    ctx = _ctx_from_event(ev)
    if bool((ev or {}).get("micro_fresh")) or bool(ctx.get("micro_fresh")):
        return True
    st = str((ev or {}).get("micro_row_status") or ctx.get("micro_row_status") or "").lower()
    return st == "fresh"


def _write_micro_urgent_targets(rows: List[Dict[str, Any]], reason: str = "paper_preopen_micro_stale", control: Optional[Dict[str, Any]] = None) -> None:
    try:
        limit = int(float_any((control or {}).get("preopen_micro_recheck_max_targets"), default=40))
        ttl = float_any((control or {}).get("preopen_micro_urgent_ttl_sec"), default=35.0)
        tickers: List[str] = []
        meta: Dict[str, Any] = {}
        for r in rows or []:
            t = normalize_ticker((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if not t or t in tickers:
                continue
            tickers.append(t)
            meta[t] = {
                "source": "paper_preopen",
                "score": float_any((r or {}).get("score"), default=0.0),
                "reason": reason,
                "trade_ready": bool((r or {}).get("paper_bot_open") or (r or {}).get("open_eligible") or (r or {}).get("trade_ready")),
            }
            if len(tickers) >= max(1, limit):
                break
        if not tickers:
            return
        save_json(FILES["micro_urgent"], {
            "version": VERSION,
            "source": "paper_bot_v0.45",
            "reason": reason,
            "updated_ts": now(),
            "updated_at": iso_ts(),
            "ttl_sec": ttl,
            "targets": tickers,
            "target_meta": meta,
            "note": "paper OPEN 직전 micro stale 후보를 바로 장부에 태우지 않고 micro sidecar 최우선 재확인 대상으로 보냄",
        })
    except Exception as exc:
        log_error("write_micro_urgent_targets", exc)


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
        "micro_preopen_wait": 0,
    }
    lane_counts = count_by_lane(open_pos)
    micro_wait_rows: List[Dict[str, Any]] = []
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
        if lane == "strict" and control.get("preopen_micro_recheck", True) and bool(ev.get("paper_bot_open") or ev.get("open_eligible") or ev.get("trade_ready")):
            if not _event_micro_is_fresh(ev):
                stats["micro_preopen_wait"] += 1
                micro_wait_rows.append(dict(ev))
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
    if micro_wait_rows:
        _write_micro_urgent_targets(micro_wait_rows, reason="paper_preopen_micro_stale", control=control)
    return picked, stats


ENTRY_CONTEXT_KEYS = [
    "score", "edge", "edge_score",
    "live_price_source", "live_price", "live_age_sec", "ws_fresh", "ws_row_status", "ws_age_sec", "ws_targeted", "ws_cache_ts", "ws_turnover", "ws_volume", "current_price_ws_gap_pct",
    "micro_fresh", "micro_row_status", "micro_age_sec", "micro_targeted", "micro_spread_pct", "micro_bid_ask_wall_ratio",
    "micro_trade_buy_ratio_30", "micro_trade_buy_krw_30", "micro_trade_sell_krw_30",
    "micro_ask_wall_pressure", "micro_sell_trade_pressure", "micro_flags",
    "change_1", "change_3", "change_5", "change_15", "change_30",
    "vol_ratio", "turnover_1m", "turnover_3m", "turnover_5m", "turnover_24h", "money_flow",
    "money_flow_1m", "money_flow_3m", "money_flow_5m", "real_money_flow_status",
    "from_30m_low_pct", "below_30m_high_pct", "pullback_depth_pct", "low_defense_pct",
    "recovery_speed_pct", "rebreakout_strength", "fake_bounce_score", "pullback_quality_score",
    "major_watch", "major_watch_label", "rank_best", "current_close_pos_ratio", "current_upper_wick_pct",
    "rsi_14", "vwap_gap_pct", "ma5_gap_pct", "ema5_gap_pct", "ema12_gap_pct", "ema21_gap_pct",
    "bb_position", "bb_lower_gap_pct", "bb_middle_gap_pct", "mfi_14", "cci_20", "stoch_k", "stoch_d",
    "stoch_cross_up", "adx_14", "atr_1m_pct", "atr_3m_pct", "current_move_vs_atr", "avg_price_turn_pct", "v_rebound_score", "volume_spike_30x", "volume_pump_risk",
    "brain_version", "created_at", "created_at_text", "final_entry_action", "final_entry_label", "final_entry_reasons", "final_slow_hits", "final_stop_hits", "market_pressure", "market_context", "quality_risk_tags",
    "liquidity_grade", "slippage_risk", "tick_risk",
    "tick_pct_est", "chase_risk", "price_recheck_pct", "orderbook_spread_pct", "execution_risk_status",
    "execution_risk_flags", "auto_ready_level", "auto_ready_label", "trade_ready_label", "trade_ready_reasons",
    "data_age_sec", "freshness", "current_candle_code", "current_candle_label",
    "external_refreshed_by", "snapshot_overlay_ts", "snapshot_refresh_attempt", "snapshot_refresh_attempt_total",
    "micro_urgent_requested", "micro_urgent_source", "micro_urgent_status_at_request",
    "micro_urgent_age_sec", "micro_urgent_age_sec_at_request", "micro_urgent_updated_ts",
    "candidate_grade_label", "hold_reason",
]


def _external_status_from_ctx(ctx: Dict[str, Any], kind: str) -> str:
    if not isinstance(ctx, dict):
        return "missing"
    if kind == "micro":
        if bool(ctx.get("micro_fresh")) or str(ctx.get("micro_row_status") or "").lower() == "fresh":
            return "fresh"
        st = str(ctx.get("micro_row_status") or "").lower()
        return "stale" if st in {"stale", "old", "오래됨"} else "missing"
    if kind == "ws":
        if bool(ctx.get("ws_fresh")) or str(ctx.get("ws_row_status") or "").lower() == "fresh":
            return "fresh"
        st = str(ctx.get("ws_row_status") or "").lower()
        return "stale" if st in {"stale", "old", "오래됨"} else "missing"
    return "missing"


def _external_closed_fields_from_ctx(ctx: Dict[str, Any]) -> Dict[str, Any]:
    ctx = ctx if isinstance(ctx, dict) else {}
    micro_status = _external_status_from_ctx(ctx, "micro")
    ws_status = _external_status_from_ctx(ctx, "ws")
    urgent_used = bool(ctx.get("micro_urgent_requested") or ctx.get("urgent_recheck_used"))
    urgent_result = "fresh" if urgent_used and micro_status == "fresh" else ("not_fresh" if urgent_used else "not_requested")
    return {
        "micro_entry_status": micro_status,
        "ws_entry_status": ws_status,
        "urgent_recheck_used": urgent_used,
        "urgent_recheck_result": urgent_result,
        "micro_urgent_requested": bool(ctx.get("micro_urgent_requested")),
        "micro_urgent_source": ctx.get("micro_urgent_source"),
        "micro_urgent_status_at_request": ctx.get("micro_urgent_status_at_request"),
        "micro_urgent_age_sec_at_request": ctx.get("micro_urgent_age_sec_at_request"),
        "ws_row_status": ctx.get("ws_row_status"),
        "ws_age_sec": ctx.get("ws_age_sec"),
        "ws_targeted": ctx.get("ws_targeted"),
        "current_price_ws_gap_pct": ctx.get("current_price_ws_gap_pct"),
        "micro_targeted": ctx.get("micro_targeted"),
        "micro_age_sec": ctx.get("micro_age_sec"),
        "entry_external_context_version": "paper_bot_v0.43",
    }


def entry_context_from_event(ev: Dict[str, Any]) -> Dict[str, Any]:
    """진입 당시 이미 알 수 있던 재료만 저장한다.
    청산사유 같은 사후 결과와 분리해서 후보품질 분석에 쓴다.
    """
    ctx: Dict[str, Any] = {
        "captured_at": now(),
        "captured_at_text": iso_ts(),
        "source": "candidate_event_at_open",
    }
    raw_profile = ev.get("profile") if isinstance(ev.get("profile"), dict) else {}
    for key in ENTRY_CONTEXT_KEYS:
        val = ev.get(key)
        if val is None and raw_profile:
            val = raw_profile.get(key)
        if val is not None:
            ctx[key] = val
    # 품질 분석에서 자주 쓰는 alias도 같이 저장한다.
    if "change_5" in ctx:
        ctx["change_5m"] = ctx.get("change_5")
    if "below_30m_high_pct" in ctx:
        ctx["below_high_pct"] = ctx.get("below_30m_high_pct")
    if "from_30m_low_pct" in ctx:
        ctx["from_low_pct"] = ctx.get("from_30m_low_pct")
    if "turnover_1m" in ctx and "money_flow_1m" not in ctx:
        ctx["money_flow_1m"] = ctx.get("turnover_1m")
    if "turnover_3m" in ctx and "money_flow_3m" not in ctx:
        ctx["money_flow_3m"] = ctx.get("turnover_3m")
    if "turnover_5m" in ctx and "money_flow_5m" not in ctx:
        ctx["money_flow_5m"] = ctx.get("turnover_5m")
    ctx.update(_external_closed_fields_from_ctx(ctx))
    ctx["captured_external_note"] = "v0.44: main snapshot/paper_latest에 반영된 micro/WS fresh 값과 진입/매도/보유시간을 OPEN/CLOSED 문맥으로 고정"
    return ctx

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
        "opened_brain_version": ev.get("brain_version") or (ev.get("profile") or {}).get("brain_version") if isinstance(ev.get("profile"), dict) else ev.get("brain_version"),
        "opened_paper_version": VERSION,
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
        "final_entry_action": ev.get("final_entry_action") or "paper_open",
        "final_entry_label": ev.get("final_entry_label") or ev.get("trade_ready_label") or "",
        "final_entry_reasons": ev.get("final_entry_reasons") or [],
        "scan_id": ev.get("scan_id") or "",
        "brain_version": ev.get("brain_version") or (ev.get("profile") or {}).get("brain_version") if isinstance(ev.get("profile"), dict) else ev.get("brain_version"),
        "source_created_at_text": ev.get("created_at_text") or "",
        "open_source": "trade_ready" if bool(ev.get("trade_ready") or ev.get("paper_bot_open") or ev.get("open_eligible")) else "unknown",
        "score": float_any(ev.get("score"), ev.get("leader_score"), default=0.0),
        "edge": float_any(ev.get("edge"), ev.get("edge_score"), default=0.0),
        "reason": ev.get("reason") or ev.get("why") or ev.get("block_reason") or "",
        "source_created_at": float_any(ev.get("created_at"), default=0.0),
        "source_expires_at": float_any(ev.get("expires_at"), default=0.0),
        "baseline_tag": "new" if now() >= baseline_ts() else "old",
        "entry_context": entry_context_from_event(ev),
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

    closed_at_ts = now()
    opened_at_ts = float_any(pos.get("opened_at"), default=closed_at_ts)
    hold_sec = hold_seconds_from_rows(opened_at_ts, closed_at_ts)
    closed = {
        "closed_at": closed_at_ts,
        "closed_at_text": iso_ts(closed_at_ts),
        "pos_id": pos.get("pos_id"),
        "event_id": pos.get("event_id"),
        "ticker": ticker,
        "lane": pos.get("lane"),
        "opened_at": pos.get("opened_at"),
        "opened_at_text": pos.get("opened_at_text"),
        "opened_brain_version": pos.get("opened_brain_version") or pos.get("brain_version"),
        "opened_paper_version": pos.get("opened_paper_version") or VERSION,
        "brain_version": pos.get("brain_version") or (pos.get("raw") or {}).get("brain_version") if isinstance(pos.get("raw"), dict) else pos.get("brain_version"),
        "source_created_at_text": pos.get("source_created_at_text"),
        "open_source": pos.get("open_source"),
        "final_entry_action": pos.get("final_entry_action"),
        "final_entry_label": pos.get("final_entry_label"),
        "final_entry_reasons": pos.get("final_entry_reasons"),
        "quality_risk_tags": (pos.get("entry_context") or {}).get("quality_risk_tags") if isinstance(pos.get("entry_context"), dict) else [],
        "entry_price": entry,
        "exit_price": price,
        "pnl_pct": round(net, 4),
        "peak_pct": round(peak, 4),
        "trough_pct": round(float_any(pos.get("trough_pct"), default=0.0), 4),
        "age_min": round(age_min, 2),
        "hold_sec": hold_sec,
        "hold_text": hold_text_from_seconds(hold_sec),
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
        "entry_context": pos.get("entry_context") if isinstance(pos.get("entry_context"), dict) else {},
        "change_5": (pos.get("entry_context") or {}).get("change_5") if isinstance(pos.get("entry_context"), dict) else None,
        "change_5m": (pos.get("entry_context") or {}).get("change_5m") if isinstance(pos.get("entry_context"), dict) else None,
        "below_30m_high_pct": (pos.get("entry_context") or {}).get("below_30m_high_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "below_high_pct": (pos.get("entry_context") or {}).get("below_high_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "from_30m_low_pct": (pos.get("entry_context") or {}).get("from_30m_low_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "from_low_pct": (pos.get("entry_context") or {}).get("from_low_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "vol_ratio": (pos.get("entry_context") or {}).get("vol_ratio") if isinstance(pos.get("entry_context"), dict) else None,
        "money_flow_1m": (pos.get("entry_context") or {}).get("money_flow_1m") if isinstance(pos.get("entry_context"), dict) else None,
        "money_flow_3m": (pos.get("entry_context") or {}).get("money_flow_3m") if isinstance(pos.get("entry_context"), dict) else None,
        "money_flow_5m": (pos.get("entry_context") or {}).get("money_flow_5m") if isinstance(pos.get("entry_context"), dict) else None,
        "pullback_quality_score": (pos.get("entry_context") or {}).get("pullback_quality_score") if isinstance(pos.get("entry_context"), dict) else None,
        "rebreakout_strength": (pos.get("entry_context") or {}).get("rebreakout_strength") if isinstance(pos.get("entry_context"), dict) else None,
        "fake_bounce_score": (pos.get("entry_context") or {}).get("fake_bounce_score") if isinstance(pos.get("entry_context"), dict) else None,
        "ema5_gap_pct": (pos.get("entry_context") or {}).get("ema5_gap_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "ema12_gap_pct": (pos.get("entry_context") or {}).get("ema12_gap_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "ema21_gap_pct": (pos.get("entry_context") or {}).get("ema21_gap_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "bb_position": (pos.get("entry_context") or {}).get("bb_position") if isinstance(pos.get("entry_context"), dict) else None,
        "bb_lower_gap_pct": (pos.get("entry_context") or {}).get("bb_lower_gap_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "bb_middle_gap_pct": (pos.get("entry_context") or {}).get("bb_middle_gap_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "mfi_14": (pos.get("entry_context") or {}).get("mfi_14") if isinstance(pos.get("entry_context"), dict) else None,
        "cci_20": (pos.get("entry_context") or {}).get("cci_20") if isinstance(pos.get("entry_context"), dict) else None,
        "stoch_k": (pos.get("entry_context") or {}).get("stoch_k") if isinstance(pos.get("entry_context"), dict) else None,
        "stoch_d": (pos.get("entry_context") or {}).get("stoch_d") if isinstance(pos.get("entry_context"), dict) else None,
        "stoch_cross_up": (pos.get("entry_context") or {}).get("stoch_cross_up") if isinstance(pos.get("entry_context"), dict) else None,
        "adx_14": (pos.get("entry_context") or {}).get("adx_14") if isinstance(pos.get("entry_context"), dict) else None,
        "avg_price_turn_pct": (pos.get("entry_context") or {}).get("avg_price_turn_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "v_rebound_score": (pos.get("entry_context") or {}).get("v_rebound_score") if isinstance(pos.get("entry_context"), dict) else None,
        "volume_spike_30x": (pos.get("entry_context") or {}).get("volume_spike_30x") if isinstance(pos.get("entry_context"), dict) else None,
        "volume_pump_risk": (pos.get("entry_context") or {}).get("volume_pump_risk") if isinstance(pos.get("entry_context"), dict) else None,
        "major_watch": (pos.get("entry_context") or {}).get("major_watch") if isinstance(pos.get("entry_context"), dict) else None,
        "major_watch_label": (pos.get("entry_context") or {}).get("major_watch_label") if isinstance(pos.get("entry_context"), dict) else None,
        "price_recheck_pct": (pos.get("entry_context") or {}).get("price_recheck_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "orderbook_spread_pct": (pos.get("entry_context") or {}).get("orderbook_spread_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "tick_pct_est": (pos.get("entry_context") or {}).get("tick_pct_est") if isinstance(pos.get("entry_context"), dict) else None,
        "execution_risk_status": (pos.get("entry_context") or {}).get("execution_risk_status") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_fresh": (pos.get("entry_context") or {}).get("micro_fresh") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_row_status": (pos.get("entry_context") or {}).get("micro_row_status") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_spread_pct": (pos.get("entry_context") or {}).get("micro_spread_pct") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_bid_ask_wall_ratio": (pos.get("entry_context") or {}).get("micro_bid_ask_wall_ratio") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_trade_buy_ratio_30": (pos.get("entry_context") or {}).get("micro_trade_buy_ratio_30") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_ask_wall_pressure": (pos.get("entry_context") or {}).get("micro_ask_wall_pressure") if isinstance(pos.get("entry_context"), dict) else None,
        "micro_sell_trade_pressure": (pos.get("entry_context") or {}).get("micro_sell_trade_pressure") if isinstance(pos.get("entry_context"), dict) else None,
        "source_created_at": pos.get("source_created_at"),
        "source_expires_at": pos.get("source_expires_at"),
    }
    try:
        closed.update(_external_closed_fields_from_ctx(closed.get("entry_context") if isinstance(closed.get("entry_context"), dict) else {}))
    except Exception as exc:
        log_error("closed_external_context", exc)
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


def fmt_num(v: Any, digits: int = 2, default: str = "-") -> str:
    try:
        if v is None:
            return default
        x = float(v)
        return f"{x:.{digits}f}"
    except Exception:
        return default


def fmt_money_krw_short(v: Any) -> str:
    x = float_any(v, default=0.0)
    if abs(x) >= 100_000_000:
        return f"{x/100_000_000:.1f}억"
    if abs(x) >= 10_000:
        return f"{x/10_000:.0f}만"
    if x:
        return f"{x:.0f}원"
    return "-"


def yes_fresh(v: Any) -> str:
    if v is True or str(v).lower() in {"true", "1", "fresh", "ok", "yes"}:
        return "✅ 신선"
    if v is False or str(v).lower() in {"false", "0", "stale", "old"}:
        return "⚠️ 오래됨"
    return "❔ 없음"


EXIT_REASON_KR = {
    "take_profit": "익절 종료",
    "stop_loss": "손절 종료",
    "slow_no_progress": "지지부진 종료",
    "time_exit": "시간 종료",
    "protect_stop_after_tp": "익절 후 보호청산",
}


def exit_reason_kr(reason: Any) -> str:
    return EXIT_REASON_KR.get(str(reason or ""), str(reason or "-"))


def strategy_kr(strategy: Any) -> str:
    s = str(strategy or "")
    if "거래대금" in s or "pull" in s.lower() or "rebreak" in s.lower():
        return "거래대금 눌림 재돌파"
    return s or "-"


def tag_text_kr(tags: Any, limit: int = 3) -> str:
    if not isinstance(tags, list):
        raw = [str(tags)] if tags else []
    else:
        raw = [str(x) for x in tags if str(x).strip()]
    mapped = []
    for t in raw[:limit]:
        low = t.lower()
        if "slow" in low or "펌핑" in t:
            mapped.append("펌핑/지지부진 위험")
        elif "stop" in low or "밀림" in t:
            mapped.append("진입 직전 밀림 위험")
        elif "spread" in low or "스프레드" in t:
            mapped.append("스프레드 주의")
        elif "weak" in low or "약함" in t:
            mapped.append(t.replace("관찰전환:", "").replace("재확인대기:", "").strip())
        else:
            mapped.append(t.replace("관찰전환:", "").replace("재확인대기:", "").strip())
    return " / ".join(mapped) if mapped else "특이사항 없음"


def micro_summary_from_ctx(ctx: Dict[str, Any]) -> str:
    if not isinstance(ctx, dict):
        return "- 호가·체결: 정보없음"
    spread = ctx.get("micro_spread_pct")
    buy_ratio = ctx.get("micro_trade_buy_ratio_30")
    wall_ratio = ctx.get("micro_bid_ask_wall_ratio")
    fresh = ctx.get("micro_fresh")
    row_status = ctx.get("micro_row_status") or "-"
    flags = []
    if ctx.get("micro_ask_wall_pressure"):
        flags.append("매도벽 주의")
    if ctx.get("micro_sell_trade_pressure"):
        flags.append("매도체결 우세")
    if not flags:
        flags.append("특이압박 없음")
    return (
        f"- 호가·체결: {yes_fresh(fresh)} / 상태 {row_status}\n"
        f"  · 스프레드 {fmt_num(spread, 2)}% / 매수체결비율 {fmt_num(buy_ratio, 2)} / 매수벽비율 {fmt_num(wall_ratio, 2)}\n"
        f"  · {' / '.join(flags[:3])}"
    )


def ws_summary_from_ctx(ctx: Dict[str, Any]) -> str:
    if not isinstance(ctx, dict):
        return "- 웹소켓: 정보없음"
    return (
        f"- 웹소켓: {yes_fresh(ctx.get('ws_fresh'))}"
        f" / REST-WS 차이 {fmt_num(ctx.get('current_price_ws_gap_pct'), 3)}%"
    )

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




def hold_text_from_seconds(sec: Any) -> str:
    s = max(0, int(float_any(sec, default=0.0)))
    h, rem = divmod(s, 3600)
    m, ss = divmod(rem, 60)
    if h > 0:
        return f"{h}시간 {m}분 {ss}초"
    if m > 0:
        return f"{m}분 {ss}초"
    return f"{ss}초"


def opened_text_from_row(row: Dict[str, Any]) -> str:
    txt = str((row or {}).get("opened_at_text") or "").strip()
    if txt:
        return txt
    ts = float_any((row or {}).get("opened_at"), default=0.0)
    return iso_ts(ts) if ts > 0 else "-"


def closed_text_from_row(row: Dict[str, Any]) -> str:
    txt = str((row or {}).get("closed_at_text") or "").strip()
    if txt:
        return txt
    ts = float_any((row or {}).get("closed_at"), default=0.0)
    return iso_ts(ts) if ts > 0 else "-"


def hold_seconds_from_rows(opened_at: Any, closed_at: Any = None) -> int:
    start = float_any(opened_at, default=0.0)
    end = float_any(closed_at, default=now())
    if start <= 0 or end <= 0 or end < start:
        return 0
    return int(end - start)


def market_links_text(ticker: Any) -> str:
    t = short_ticker(ticker)
    if not t or t == "-":
        return ""
    bithumb = f"https://www.bithumb.com/trade/order/{t}_KRW"
    tv = f"https://www.tradingview.com/chart/?symbol=BITHUMB%3A{t}KRW"
    return "🔗 바로보기\n- 빗썸: " + bithumb + "\n- 차트: " + tv


def format_open_alert(pos: Dict[str, Any]) -> str:
    """실제 paper_bot OPEN 장부에 저장된 건만 보내는 알림.
    후보/관찰/복기 알림이 아니라 실제 모의매매 진입 기록이다.
    """
    ctx = pos.get("entry_context") if isinstance(pos.get("entry_context"), dict) else {}
    tags = ctx.get("quality_risk_tags") or []
    final_label = pos.get("final_entry_label") or ctx.get("final_entry_label") or pos.get("trade_ready_label") or "최종검증 통과"
    reasons = pos.get("final_entry_reasons") or ctx.get("final_entry_reasons") or []
    if isinstance(reasons, list):
        reason_txt = " / ".join(str(x).replace("관찰전환:", "").replace("재확인대기:", "").strip() for x in reasons[:3] if str(x).strip()) or final_label
    else:
        reason_txt = str(reasons or final_label)
    lines = [
        "📥 모의매매 진입",
        "",
        f"코인: {short_ticker(pos.get('ticker'))}",
        f"진입가: {format_price(pos.get('entry_price'))}",
        f"진입시간: {opened_text_from_row(pos)}",
        f"전략: {strategy_kr(pos.get('strategy'))}",
        f"판정: {str(final_label).replace('OPEN', '진입').replace('trade_ready', '최종검증 통과')}",
        "",
        "📌 진입 근거",
        f"- 점수: {float_any(pos.get('score'), default=0.0):.2f}",
        f"- 3분 돈흐름: {fmt_money_krw_short(ctx.get('money_flow_3m'))}",
        f"- 눌림품질: {fmt_num(ctx.get('pullback_quality_score'), 2)}",
        f"- 재돌파힘: {fmt_num(ctx.get('rebreakout_strength'), 2)}",
        f"- 위험태그: {tag_text_kr(tags)}",
        "",
        "🛰 외부정보",
        ws_summary_from_ctx(ctx),
        micro_summary_from_ctx(ctx),
        "",
        market_links_text(pos.get('ticker')),
        "",
        "참고: 실제 주문 아님 / paper_bot 모의매매 장부 OPEN 기록",
    ]
    return "\n".join(lines)


def format_close_alert(closed: Dict[str, Any]) -> str:
    """실제 paper_bot CLOSED 장부에 저장된 건만 보내는 알림."""
    ctx = closed.get("entry_context") if isinstance(closed.get("entry_context"), dict) else {}
    reason = str(closed.get("exit_reason") or "")
    pnl = float_any(closed.get("pnl_pct"), default=0.0)
    if reason == "take_profit" or pnl > 0:
        icon = "✅"
    elif reason == "stop_loss" or pnl < -0.5:
        icon = "❌"
    else:
        icon = "⚠️"
    lines = [
        f"{icon} 모의매매 종료: {exit_reason_kr(reason)}",
        "",
        f"코인: {short_ticker(closed.get('ticker'))}",
        f"수익률: {fmt_pct(closed.get('pnl_pct'))}",
        f"진입 → 청산: {format_price(closed.get('entry_price'))} → {format_price(closed.get('exit_price'))}",
        f"최고/최저: {fmt_pct(closed.get('peak_pct'))} / {fmt_pct(closed.get('trough_pct'))}",
        f"진입시간: {opened_text_from_row(closed)}",
        f"매도시간: {closed_text_from_row(closed)}",
        f"보유시간: {closed.get('hold_text') or hold_text_from_seconds(closed.get('hold_sec') or float_any(closed.get('age_min'), default=0.0) * 60)}",
        "",
        "📌 진입 당시 근거",
        f"- 전략: {strategy_kr(closed.get('strategy'))}",
        f"- 점수: {float_any(closed.get('score'), default=0.0):.2f}",
        f"- 3분 돈흐름: {fmt_money_krw_short(ctx.get('money_flow_3m'))}",
        f"- 눌림품질: {fmt_num(ctx.get('pullback_quality_score'), 2)}",
        f"- 위험태그: {tag_text_kr(closed.get('quality_risk_tags') or ctx.get('quality_risk_tags'))}",
        "",
        "🛰 진입 당시 외부정보",
        ws_summary_from_ctx(ctx),
        micro_summary_from_ctx(ctx),
        "",
        market_links_text(closed.get('ticker')),
    ]
    return "\n".join(x for x in lines if x is not None)



def load_alert_state() -> Dict[str, Any]:
    data = load_json(FILES.get("alert_state", BASE_DIR / "paper_bot_alert_state.json"), {})
    return data if isinstance(data, dict) else {}


def save_alert_state(data: Dict[str, Any]) -> None:
    if not isinstance(data, dict):
        data = {}
    data["updated_at"] = now()
    data["updated_at_text"] = iso_ts()
    save_json(FILES.get("alert_state", BASE_DIR / "paper_bot_alert_state.json"), data)


def _alert_key(row: Dict[str, Any]) -> str:
    return str((row or {}).get("pos_id") or (row or {}).get("event_id") or "")


def _alerted_open_ids(state: Optional[Dict[str, Any]] = None) -> set[str]:
    state = state if isinstance(state, dict) else load_alert_state()
    vals = state.get("open_alerted_ids") if isinstance(state.get("open_alerted_ids"), list) else []
    return {str(x) for x in vals if str(x)}


def mark_open_alerted(row: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = state if isinstance(state, dict) else load_alert_state()
    ids = list(_alerted_open_ids(state))
    key = _alert_key(row)
    if key and key not in ids:
        ids.append(key)
    state["open_alerted_ids"] = ids[-5000:]
    state["last_open_alert_at"] = now()
    state["last_open_alert_ticker"] = short_ticker((row or {}).get("ticker"))
    save_alert_state(state)
    return state


def was_open_alerted(row: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> bool:
    key = _alert_key(row)
    return bool(key and key in _alerted_open_ids(state))


def should_send_open_alert(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[bool, str]:
    """v0.40: 알림은 '실제 자동매매에 넣을 만한 paper OPEN'만 보낸다.
    장부 OPEN 자체는 유지하지만, 애매한 후보/관찰성 OPEN 알림은 시간 요약으로 보낸다.
    """
    if str((pos or {}).get("lane")) != "strict":
        return False, "strict 아님"
    if not bool((pos or {}).get("trade_ready")):
        return False, "trade_ready 아님"
    score = float_any((pos or {}).get("score"), default=0.0)
    min_score = float_any(control.get("notify_open_min_score"), default=4.45)
    if score < min_score:
        return False, f"점수 {score:.2f} < 알림 {min_score:.2f}"
    action = str((pos or {}).get("final_entry_action") or "")
    label = str((pos or {}).get("final_entry_label") or "")
    if control.get("notify_open_require_final_pass", True):
        if action not in {"paper_open", "trade_ready", "open", ""} and "통과" not in label:
            return False, f"최종검증 통과 아님({action or label})"
    ctx = (pos or {}).get("entry_context") if isinstance((pos or {}).get("entry_context"), dict) else {}
    if control.get("notify_open_require_micro_fresh", True) and not bool(ctx.get("micro_fresh")):
        return False, "micro fresh 아님"
    if control.get("notify_open_require_ws_fresh", False) and not bool(ctx.get("ws_fresh")):
        return False, "WS fresh 아님"
    risk_text = " / ".join(str(x) for x in ((pos or {}).get("quality_risk_tags") or ctx.get("quality_risk_tags") or []))
    if any(bad in risk_text for bad in ["펌핑", "밀림", "추격", "실전위험"]):
        return False, f"위험태그 {risk_text[:80]}"
    return True, "자동매매급 알림"


def _ambiguous_candidates(control: Dict[str, Any]) -> List[Dict[str, Any]]:
    read_max = int(float_any(control.get("candidate_read_max_lines"), default=800))
    rows = read_jsonl(candidate_input_path("strict"), max_lines=read_max)
    rows, _ = latest_scan_filter(rows, control)
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        can_open = bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))
        action = str(r.get("final_entry_action") or "")
        if can_open:
            # 알림대상에서 탈락한 OPEN급은 요약에 함께 표시한다.
            dummy = {"lane":"strict", "trade_ready": True, "score": r.get("score"), "final_entry_action": action, "final_entry_label": r.get("final_entry_label"), "entry_context": r.get("entry_context") if isinstance(r.get("entry_context"), dict) else r}
            ok, why = should_send_open_alert(dummy, control)
            if ok:
                continue
            rr = dict(r); rr["summary_reason"] = "OPEN 알림 제외: " + why; out.append(rr); continue
        if action in {"recheck_wait", "observe"} or not can_open:
            rr = dict(r); rr.setdefault("summary_reason", r.get("final_entry_label") or r.get("trade_ready_label") or "애매/관찰 후보"); out.append(rr)
    out.sort(key=lambda x: (bool(x.get("paper_bot_open") or x.get("trade_ready") or x.get("open_eligible")), float_any(x.get("score"), default=0.0), float_any(x.get("money_flow_3m") or x.get("turnover_3m"), default=0.0)), reverse=True)
    return out


def format_recheck_summary(rows: List[Dict[str, Any]], control: Dict[str, Any]) -> str:
    max_rows = int(float_any(control.get("recheck_summary_max_rows"), default=8))
    reason_counter = Counter(str(r.get("summary_reason") or r.get("final_entry_label") or r.get("trade_ready_label") or "-") for r in rows)
    lines = [
        "🧭 애매한 후보 시간요약",
        "- 실시간 알림은 자동매매급 OPEN만 보냅니다.",
        "- 아래는 알림 대신 묶어서 보는 재확인/관찰 후보입니다.",
        "",
        f"전체: {len(rows)}개 / 표시 {min(len(rows), max_rows)}개",
    ]
    if reason_counter:
        lines.append("이유 TOP: " + " / ".join(f"{k} {v}" for k, v in reason_counter.most_common(4)))
    lines += ["", "후보 예시"]
    for r in rows[:max_rows]:
        ctx = r.get("entry_context") if isinstance(r.get("entry_context"), dict) else r
        lines.append(
            f"- {short_ticker(r.get('ticker') or r.get('market') or r.get('symbol'))}: "
            f"점수 {float_any(r.get('score'), default=0.0):.2f} / 3분돈 {fmt_money_krw_short(ctx.get('money_flow_3m') or r.get('money_flow_3m') or r.get('turnover_3m'))} / "
            f"micro {yes_fresh(ctx.get('micro_fresh'))} / WS {yes_fresh(ctx.get('ws_fresh'))} / "
            f"{str(r.get('summary_reason') or r.get('final_entry_label') or '-')[:80]}"
        )
    lines.append("")
    lines.append("설정: /palerts")
    return "\n".join(lines)


def maybe_send_recheck_summary(control: Dict[str, Any]) -> None:
    if not control.get("notify_recheck_summary", True):
        return
    state = load_alert_state()
    interval = max(300.0, float_any(control.get("recheck_summary_interval_sec"), default=3600.0))
    last = float_any(state.get("last_recheck_summary_at"), default=0.0)
    if now() - last < interval:
        return
    rows = _ambiguous_candidates(control)
    if not rows:
        state["last_recheck_summary_at"] = now()
        save_alert_state(state)
        return
    send_message(format_recheck_summary(rows, control))
    state["last_recheck_summary_at"] = now()
    state["last_recheck_summary_count"] = len(rows)
    save_alert_state(state)


def notify_strict_events(opened: List[Dict[str, Any]], closed: List[Dict[str, Any]], control: Dict[str, Any]) -> None:
    # v0.40: 알림은 자동매매급 OPEN 중심. 애매한 후보는 시간 요약으로 묶는다.
    # 장부 OPEN/CLOSED 저장은 그대로 유지하고, 알림만 줄인다.
    try:
        state = load_alert_state()
        if control.get("notify_on_strict_open", True):
            for p in opened:
                if str(p.get("lane")) != "strict":
                    continue
                ok, why = should_send_open_alert(p, control)
                if ok:
                    send_message(format_open_alert(p))
                    state = mark_open_alerted(p, state)
                else:
                    log(f"open_alert_skip {short_ticker(p.get('ticker'))}: {why}")
        if control.get("notify_on_strict_close", True):
            for c in closed:
                if str(c.get("lane")) != "strict":
                    continue
                if control.get("notify_close_only_alerted", True) and not was_open_alerted(c, state):
                    log(f"close_alert_skip_not_alerted {short_ticker(c.get('ticker'))}")
                    continue
                send_message(format_close_alert(c))
        maybe_send_recheck_summary(control)
    except Exception as exc:
        log_error("notify_strict_events", exc)


def _file_stats_direct() -> Dict[str, Any]:
    out = {}
    for name in ["paper_candidates", "shadow_candidates", "paper_latest", "shadow_latest", "shadow_quarantine", "legacy_strict_quarantine", "open", "closed", "status", "error", "log"]:
        p = FILES[name]
        info = {"exists": p.exists(), "size": p.stat().st_size if p.exists() else 0}
        if p.suffix == ".jsonl" and p.exists():
            info["lines"] = line_count(p)
        out[name] = info
    return out


def file_stats(*, force: bool = False) -> Dict[str, Any]:
    """v0.45: status 작성 때마다 jsonl 라인수를 전부 세는 경로를 TTL 캐시로 정리한다."""
    try:
        nowv = now()
        cached = _FILE_STATS_CACHE.get("stats")
        if (not force) and isinstance(cached, dict) and nowv - float_any(_FILE_STATS_CACHE.get("ts"), default=0.0) <= FILE_STATS_CACHE_TTL_SEC:
            return cached
        stats = _file_stats_direct()
        _FILE_STATS_CACHE.update({"ts": nowv, "stats": stats})
        return stats
    except Exception as exc:
        log_error("file_stats_cached", exc)
        cached = _FILE_STATS_CACHE.get("stats")
        return cached if isinstance(cached, dict) else {}


def closed_new_total_cached() -> int:
    """v0.45: CLOSED 20000줄 tail을 매 cycle 읽지 않는다."""
    try:
        p = FILES["closed"]
        size = p.stat().st_size if p.exists() else 0
        mtime = p.stat().st_mtime if p.exists() else 0.0
        nowv = now()
        if (_CLOSED_NEW_TOTAL_CACHE.get("count") is not None
                and int(_CLOSED_NEW_TOTAL_CACHE.get("file_size", -1) or -1) == int(size)
                and abs(float_any(_CLOSED_NEW_TOTAL_CACHE.get("mtime"), default=0.0) - mtime) < 0.001
                and nowv - float_any(_CLOSED_NEW_TOTAL_CACHE.get("ts"), default=0.0) <= CLOSED_NEW_TOTAL_CACHE_TTL_SEC):
            return int(_CLOSED_NEW_TOTAL_CACHE.get("count") or 0)
        cnt = len(rows_since_baseline(read_jsonl(FILES["closed"], max_lines=20000), "closed_at"))
        _CLOSED_NEW_TOTAL_CACHE.update({"ts": nowv, "count": cnt, "file_size": size, "mtime": mtime})
        return cnt
    except Exception as exc:
        log_error("closed_new_total_cached", exc)
        return int(_CLOSED_NEW_TOTAL_CACHE.get("count") or 0)


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
        write_pid()
        status = {
            "version": VERSION,
            "pid": os.getpid(),
            "self_pid": os.getpid(),
            "process_pid": os.getpid(),
            "process_alive": True,
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
            "closed_new_total": closed_new_total_cached(),
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
            "pid": os.getpid(),
            "self_pid": os.getpid(),
            "process_pid": os.getpid(),
            "process_alive": True,
            "updated_at": now(),
            "updated_at_text": iso_ts(),
            "running": bool(control.get("running")),
            "loop_seconds": control.get("loop_seconds"),
            "open_total": len(open_pos),
            "open_strict": counts.get("strict", 0),
            "open_shadow": counts.get("shadow", 0),
            "closed_total": closed_count(),
            "startup_status": True,
            "stop_reason": "running",
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


EXIT_REASON_KR = {
    "take_profit": "익절 종료",
    "stop_loss": "손절 종료",
    "slow_no_progress": "지지부진 종료",
    "time_exit": "시간 종료",
    "protect_stop_after_tp": "익절 후 보호청산",
    "unknown": "사유 미확인",
}

def label_kr(label: str) -> str:
    s = str(label or "-")
    return EXIT_REASON_KR.get(s, s)

def score_icon(stat: Dict[str, Any]) -> str:
    n = int(stat.get("n", 0) or 0)
    if n <= 0:
        return "❔"
    if n < 50:
        return "⚠️"
    return "✅" if float_any(stat.get("avg"), default=0.0) > 0 else "❌"

def sample_note(n: int) -> str:
    if 0 < n < 50:
        return " / 판단보류: 50전 미만"
    return ""

def fmt_stats(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    s = score_stats(rows)
    return f"{score_icon(s)} {label_kr(label)}: {s['n']}전 {s['wins']}승 {s['losses']}패 / 승률 {s['win_rate']:.1f}% / 합산 {s['total']:+.2f}% / 평균 {s['avg']:+.2f}%{sample_note(s['n'])}"




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
        "⚠️ 50전 미만은 판단보류",
        "❌ 반복 손실 구간은 조건 조정 후보",
        "❔ 지금은 새 전략 추가보다 어디가 좋은지 나눠 보는 단계",
    ])


def current_brain_version_from_files() -> str:
    """paper CLOSED에 기록된 최신 brain_version을 참고 표시한다."""
    rows = read_jsonl(FILES["closed"], max_lines=5000)
    for r in reversed(rows):
        v = r.get("opened_brain_version") or r.get("brain_version")
        if v:
            return str(v)
    return "unknown"


def row_brain_version(row: Dict[str, Any]) -> str:
    for key in ("opened_brain_version", "brain_version"):
        v = row.get(key)
        if v:
            return str(v)
    raw = row.get("raw")
    if isinstance(raw, dict):
        for key in ("opened_brain_version", "brain_version"):
            v = raw.get(key)
            if v:
                return str(v)
    return "버전 미기록"

def version_history_lines(rows: Iterable[Dict[str, Any]], limit: int = 10) -> List[str]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        if str(r.get("lane")) != "strict":
            continue
        groups.setdefault(row_brain_version(r), []).append(r)
    if not groups:
        return ["- 아직 버전별 CLOSED 없음"]
    items = []
    for ver, arr in groups.items():
        latest = max((closed_ts(x) for x in arr), default=0.0)
        items.append((latest, ver, arr))
    out: List[str] = []
    for _, ver, arr in sorted(items, reverse=True)[:limit]:
        st = score_stats(arr)
        reasons = Counter(label_kr(str(r.get("exit_reason") or "unknown")) for r in arr)
        reason_txt = " / ".join(f"{k} {v}" for k, v in reasons.most_common(3)) if reasons else "-"
        out.append(f"{score_icon(st)} {ver}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%{sample_note(st['n'])} / 주요종료 {reason_txt}")
    return out

def version_score_text() -> str:
    rows = read_jsonl(FILES["closed"], max_lines=30000)
    version = current_brain_version_from_files()
    if version == "unknown":
        target = []
    else:
        target = [r for r in rows if str(r.get("opened_brain_version") or r.get("brain_version") or "") == version and str(r.get("lane")) == "strict"]
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in target)
    lines = [
        "📊 현재버전 모의매매 /pversion_score",
        f"- 기준 brain_version: {version}",
        fmt_stats("현재버전 정식 모의매매", target),
        "",
        "[종료 사유]",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5):
            sub = [r for r in target if str(r.get("exit_reason") or "unknown") == k]
            lines.append(fmt_stats(k, sub))
    else:
        lines.append("- 현재버전 CLOSED 부족 또는 구기록에 brain_version 없음")
    lines += ["", "[최근 버전별 성과 - 최대 10개]", *version_history_lines(rows, limit=10)]
    return "\n".join(lines)

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
    new_closed_count = closed_new_total_cached()
    base = ensure_eval_baseline()
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- OPEN: 전체 {len(open_pos)} / 정식 {counts.get('strict',0)} / 기존복기 {counts.get('shadow',0)}",
        f"  · 신규 {new_open_total}(정식 {new_open_counts.get('strict',0)} / 복기 {new_open_counts.get('shadow',0)}) / 기존 {old_open_total}(정식 {old_open_counts.get('strict',0)} / 복기 {old_open_counts.get('shadow',0)})",
        "- OPEN 시간표:" if open_pos else "- OPEN 시간표: 없음",
        *(format_open_list(5).splitlines() if open_pos else []),
        f"- CLOSED: 전체 {closed_count()}건 / 신규기준 이후 {new_closed_count}건",
        f"- 기준점: {base.get('baseline_text','-')} / 기존 기록 삭제 없음",
        f"- 현재 latest 파일: 정식 {fs.get('paper_latest',{}).get('lines',0)}개(TTL {p_fresh.get('fresh',0)}) / 복기 {fs.get('shadow_latest',{}).get('lines',0)}개(TTL {s_fresh.get('fresh',0)})",
        f"- 격리보존: shadow {fs.get('shadow_quarantine',{}).get('lines',0)} / 기존strict {fs.get('legacy_strict_quarantine',{}).get('lines',0)}",
        f"- 처리: trade_ready만 신규 OPEN / shadow 복기전용 / 기존 shadow·기존 strict 격리 보존",
        f"- 이번 cycle: open +{status.get('opened_this_cycle',0)} / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"- 직전 cycle 후보: 읽음 {pick.get('paper_file',0)} / OPEN통과 {status.get('opened_this_cycle',0)} / micro재확인대기 {pick.get('micro_preopen_wait',0)} / 관찰제외 {pick.get('strict_observe_skip',0)} / 복기 {pick.get('shadow_review_only_skip',0)} / 중복 {pick.get('same_ticker_skip',0)}",
        f"- 최신 scan 소비: before {pick.get('latest_scan_before',0)} → after {pick.get('latest_scan_after',0)} / scan_id {pick.get('latest_scan_id','-') or '-'}",
        f"- 알림: 실제 장부 OPEN/CLOSED만 ON / 후보·관찰·복기 알림 OFF",
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
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        lines.append(f"- {short_ticker(pos.get('ticker'))} / {tag} / {pos.get('lane','-')} / 진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / {float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / 보유 {hold_txt}")
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
    keys = ["running", "loop_seconds", "max_open_strict", "max_open_shadow", "max_new_per_cycle", "open_trade_ready_only", "track_strict_observe", "candidate_ttl_sec", "candidate_read_max_lines", "consume_latest_scan_only", "consume_latest_candidate_files", "quarantine_shadow_open", "quarantine_legacy_strict_open", "latest_scan_window_sec", "fee_pct_roundtrip", "take_profit_pct", "protect_trigger_pct", "protect_floor_pct", "stop_loss_pct", "slow_minutes", "time_exit_minutes", "block_same_ticker_open", "preopen_micro_recheck", "preopen_micro_urgent_ttl_sec", "notify_open_auto_ready_only", "notify_open_min_score", "notify_open_require_micro_fresh", "notify_open_require_ws_fresh", "notify_close_only_alerted", "notify_recheck_summary", "recheck_summary_interval_sec"]
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
            "명령어: /pbatch /pstatus /pcheck /ponce /pstart /pstop /prestart /plog /perror /pscore /pversion_score /popen /pfiles /pcontrol /palerts",
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
        save_control({"running": False, "last_stop_reason": "user_pstop"})
        log("control running=False reason=user_pstop")
        return "⏸ paper_bot 실행 OFF\n" + summary_text()
    if cmd == "/prestart":
        save_control({"running": False, "last_stop_reason": "user_prestart"}); time.sleep(0.5); save_control({"running": True})
        return "🔁 paper_bot 논리 재시작 완료\n" + summary_text()
    if cmd == "/plog":
        return "🧾 paper_bot.log\n\n" + tail_file(FILES["log"], 60)[-3500:]
    if cmd == "/perror":
        return "🧯 paper_bot_error.log\n\n" + tail_file(FILES["error"], 80)[-3500:]
    if cmd == "/pversion_score":
        return version_score_text()
    if cmd == "/popen":
        return "📂 OPEN 시간표 /popen\n" + format_open_list(30)
    if cmd == "/pscore":
        rows = read_jsonl(FILES["closed"], max_lines=30000)
        new_rows = rows_since_baseline(rows, "closed_at")
        new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
        strict = [r for r in rows if str(r.get("lane")) == "strict"]
        shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
        by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in new_strict)
        lines = [
            "📊 페이퍼봇 성과 /pscore",
            "- 기준: paper_bot 신규 기준점 / 기존 기록 삭제 없음",
            "",
            "[1/5] 전적",
            fmt_stats("정식 모의매매 전체", new_strict),
            fmt_stats("누적 정식 참고", strict),
            "",
            "[2/5] 종료 사유",
        ]
        if by_reason:
            for k, _ in by_reason.most_common(5):
                sub = [r for r in new_strict if str(r.get("exit_reason") or "unknown") == k]
                lines.append(fmt_stats(k, sub))
        else:
            lines.append("- 아직 신규 CLOSED 없음")
        lines += [
            "",
            "[3/5] 시간대",
            group_table("- 3시간 묶음", new_strict, bucket_hour3, limit=5),
            "",
            "[4/5] 등급",
            group_table("- 준비등급/점수대", new_strict, bucket_auto_ready, limit=5),
            "",
            "[5/5] 누적 참고",
            fmt_stats("전체", rows),
            fmt_stats("복기 shadow 참고", shadow),
            "",
            "판독",
            "❌ 평균이 계속 -면 자동매매 보류",
            "✅ 평균이 +인 시간대/등급만 살릴 후보",
            "⚠️ 50건 미만은 참고만",
        ]
        send_message(chat_id, "\n".join(lines))
        return

    if cmd == "/palerts":
        c = load_control()
        st = load_alert_state()
        rows = _ambiguous_candidates(c)
        return "\n".join([
            "🔔 페이퍼봇 알림정책 /palerts",
            "- 실시간 OPEN 알림: 자동매매급 후보만",
            f"- OPEN 알림 최소점수: {c.get('notify_open_min_score')}",
            f"- micro fresh 요구: {c.get('notify_open_require_micro_fresh')}",
        f"- 정식 OPEN 직전 micro 재확인: {c.get('preopen_micro_recheck')} / stale이면 urgent 대기",
            f"- WS fresh 요구: {c.get('notify_open_require_ws_fresh')}",
            f"- CLOSE 알림: OPEN 알림 보낸 포지션만 {c.get('notify_close_only_alerted')}",
            f"- 애매후보 시간요약: {c.get('notify_recheck_summary')} / {int(float_any(c.get('recheck_summary_interval_sec'), default=3600))}초",
            f"- 현재 요약대상: {len(rows)}개",
            f"- 마지막 요약: {iso_ts(float_any(st.get('last_recheck_summary_at'), default=0.0)) if float_any(st.get('last_recheck_summary_at'), default=0.0)>0 else '-'}",
            "- 장부 OPEN/CLOSED 자체는 기존대로 유지, 알림만 줄임",
        ])
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



def write_shutdown_status(reason: str = "unknown") -> None:
    try:
        control = load_control()
        open_pos = load_open()
        counts = count_by_lane(open_pos)
        save_json(FILES["status"], {
            "version": VERSION,
            "pid": os.getpid(),
            "self_pid": os.getpid(),
            "process_pid": os.getpid(),
            "process_alive": True,
            "updated_at": now(),
            "updated_at_text": iso_ts(),
            "pid": os.getpid(),
            "self_pid": os.getpid(),
            "process_pid": os.getpid(),
            "running": False,
            "process_alive": False,
            "stop_reason": reason,
            "loop_seconds": control.get("loop_seconds"),
            "open_total": len(open_pos),
            "open_strict": counts.get("strict", 0),
            "open_shadow": counts.get("shadow", 0),
            "closed_total": closed_count(),
        })
        log(f"shutdown_status reason={reason} open={len(open_pos)} closed={closed_count()}")
    except Exception as exc:
        log_error("write_shutdown_status", exc)

def handle_signal(signum, frame) -> None:
    global _STOP_REASON
    _STOP_REASON = f"signal_{signum}"
    log(f"stop requested: {_STOP_REASON}")
    write_shutdown_status(_STOP_REASON)
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
    try:
        while not _stop_event.is_set():
            time.sleep(1)
    finally:
        reason = _STOP_REASON if _STOP_REASON != "running" else "main_loop_exit"
        write_shutdown_status(reason)
        log(f"{VERSION} stopped reason={reason}")


if __name__ == "__main__":
    main()
