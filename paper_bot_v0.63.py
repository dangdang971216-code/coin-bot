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
import re
import signal
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import Counter, deque, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

VERSION = "paper_bot_v0.63"
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

    # v0.50: OPEN 포지션 손실 방어. 후보 읽기는 8초 유지, OPEN 감시는 더 촘촘히 본다.
    "open_monitor_interval_sec": 1.5,
    "hard_loss_guard_enabled": True,
    "hard_loss_guard_pct": -0.95,
    "long_loss_guard_enabled": True,
    "long_loss_guard_min_age_sec": 900,
    "long_loss_guard_current_under_pct": -0.45,
    "long_loss_guard_peak_under_pct": 0.60,
    "slow_minutes": 20,
    "slow_peak_under_pct": 0.25,
    # v0.47: 실제 paper 청산모드. 기존 stop/take/slow는 유지하고, 반응 없는 후보만 먼저 정리한다.
    "quick_stop_enabled": True,
    "quick_stop_min_age_sec": 90,
    "quick_stop_max_age_sec": 600,
    "quick_stop_peak_under_pct": 0.20,
    "quick_stop_current_under_pct": -0.35,
    "quick_stop_weak_buy_ratio": 0.50,
    "quick_stop_wide_spread_pct": 0.25,
    "slow_early_enabled": True,
    "slow_early_minutes": 10,
    "slow_early_peak_under_pct": 0.10,
    "slow_early_current_under_pct": 0.05,
    "slow_early_pullback_under": 1.00,
    "slow_early_weak_buy_ratio": 0.50,
    "slow_early_low_money3_krw": 15_000_000,
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
PROGRAM_STARTED_TS = time.time()


def now() -> float:
    return time.time()


KST = timezone(timedelta(hours=9))
TIMEZONE_LABEL = "Asia/Seoul"

def iso_ts(ts: Optional[float] = None) -> str:
    """모든 사용자-facing 시간은 한국시간(KST)으로 표시한다.

    기존 서버가 UTC로 도는 경우 알림 시간이 실제 차트/텔레그램 체감시간보다 9시간 늦게 보였다.
    timestamp 자체는 그대로 보존하고, 문자열 표시만 KST로 통일한다.
    """
    return datetime.fromtimestamp(ts or now(), KST).strftime("%Y-%m-%d %H:%M:%S")


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
    # v0.63: v274 전략층 초기화 중에는 신규 모의매매 OPEN을 생성하지 않는다.
    if control.get("new_open_paused") is not True:
        control["new_open_paused"] = True
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
        "new_open_paused": 0,
    }
    if control.get("new_open_paused", True):
        stats["new_open_paused"] = 1
        return [], stats
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



def exit_mode_from_context(ctx: Dict[str, Any], score: Any = 0.0) -> Dict[str, Any]:
    """v0.46: 실제 청산조건을 바꾸지 않고, 후보별 청산 관찰모드를 기록한다.

    목적:
    - KAIA처럼 강한 후보는 +1.2% 전량청산이 아쉬운지 추적할 재료를 남긴다.
    - 손절/지지부진으로 자주 끝나는 후보는 조기이탈 후보인지 분리할 재료를 남긴다.
    - 이 함수는 장부/청산 판단을 바꾸지 않는다. 기록/알림/분석용이다.
    """
    ctx = ctx if isinstance(ctx, dict) else {}
    score_v = float_any(score, ctx.get("score"), default=0.0)
    money3 = float_any(ctx.get("money_flow_3m") or ctx.get("turnover_3m"), default=0.0)
    pull = float_any(ctx.get("pullback_quality_score"), default=0.0)
    rebreak = float_any(ctx.get("rebreakout_strength"), default=0.0)
    spread = float_any(ctx.get("micro_spread_pct"), default=999.0)
    buy_ratio = float_any(ctx.get("micro_trade_buy_ratio_30"), default=0.0)
    wall_ratio = float_any(ctx.get("micro_bid_ask_wall_ratio"), default=0.0)
    micro_fresh = bool(ctx.get("micro_fresh")) or str(ctx.get("micro_row_status") or "").lower() == "fresh"
    ws_fresh = bool(ctx.get("ws_fresh")) or str(ctx.get("ws_row_status") or "").lower() == "fresh"
    risk_tags = ctx.get("quality_risk_tags") if isinstance(ctx.get("quality_risk_tags"), list) else []
    tags_text = " / ".join(str(x) for x in risk_tags)

    strong_hits = []
    if score_v >= 4.45:
        strong_hits.append("점수충족")
    if money3 >= 20_000_000:
        strong_hits.append("3분돈강함")
    if pull >= 1.80:
        strong_hits.append("눌림양호")
    if micro_fresh and spread <= 0.08:
        strong_hits.append("호가신선스프레드좁음")
    if buy_ratio >= 0.62:
        strong_hits.append("매수체결우세")
    if wall_ratio >= 1.50:
        strong_hits.append("매수벽우세")
    if ws_fresh:
        strong_hits.append("WS신선")

    weak_hits = []
    if (not micro_fresh) or spread >= 0.25:
        weak_hits.append("호가불리")
    if buy_ratio and buy_ratio < 0.45:
        weak_hits.append("매수체결약함")
    if wall_ratio and wall_ratio < 0.75:
        weak_hits.append("매수벽약함")
    if rebreak and rebreak < 1.20:
        weak_hits.append("재돌파힘약함")
    if any("위주" in str(x) or "약함" in str(x) for x in risk_tags):
        weak_hits.append("위험태그")

    if len(strong_hits) >= 5 and len(weak_hits) <= 1:
        mode = "strong_trailing_watch"
        label = "강한후보: 익절 후 더감 추적"
        note = "+1.2% 전량청산이 아쉬울 수 있어 청산 후 더감 여부를 관찰"
    elif len(weak_hits) >= 3:
        mode = "quick_cut_watch"
        label = "약한후보: 빠른손절 관찰"
        note = "초반 반응 없으면 -1.2% 전 조기이탈 후보인지 관찰"
    elif "재돌파힘 약함" in tags_text or rebreak < 1.20:
        mode = "slow_cut_watch"
        label = "지지부진 관찰"
        note = "돈흐름 대비 가격반응이 약하면 지지부진 조기종료 후보"
    else:
        mode = "normal_exit_watch"
        label = "보통후보: 기존청산 관찰"
        note = "기존 익절/손절/시간종료 기준 유지 관찰"
    return {
        "exit_mode": mode,
        "exit_mode_label": label,
        "exit_mode_note": note,
        "exit_mode_strong_hits": strong_hits[:8],
        "exit_mode_weak_hits": weak_hits[:8],
        "exit_mode_metrics": {
            "score": round(score_v, 3),
            "money_flow_3m": money3,
            "pullback_quality_score": pull,
            "rebreakout_strength": rebreak,
            "micro_spread_pct": spread,
            "micro_trade_buy_ratio_30": buy_ratio,
            "micro_bid_ask_wall_ratio": wall_ratio,
            "micro_fresh": micro_fresh,
            "ws_fresh": ws_fresh,
        },
    }


def exit_mode_line(row: Dict[str, Any]) -> str:
    mode = str((row or {}).get("exit_mode_label") or (row or {}).get("exit_mode") or "")
    note = str((row or {}).get("exit_mode_note") or "")
    if not mode:
        return "- 청산관찰: -"
    return f"- 청산관찰: {mode}" + (f" / {note}" if note else "")

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:
    ticker = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol")) or "UNKNOWN"
    detected_price = get_event_price(ev)
    live_price = cycle_live_price(ticker) or detected_price
    entry_price = live_price if live_price > 0 else detected_price
    lane = str(ev.get("lane") or "shadow")
    entry_ctx = entry_context_from_event(ev)
    exit_mode = exit_mode_from_context(entry_ctx, score=float_any(ev.get("score"), ev.get("leader_score"), default=0.0))
    entry_ctx.update(exit_mode)
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
        "exit_mode": exit_mode.get("exit_mode"),
        "exit_mode_label": exit_mode.get("exit_mode_label"),
        "exit_mode_note": exit_mode.get("exit_mode_note"),
        "exit_mode_strong_hits": exit_mode.get("exit_mode_strong_hits"),
        "exit_mode_weak_hits": exit_mode.get("exit_mode_weak_hits"),
        "entry_context": entry_ctx,
        "raw": ev,
    }



def _position_entry_ctx(pos: Dict[str, Any]) -> Dict[str, Any]:
    return (pos or {}).get("entry_context") if isinstance((pos or {}).get("entry_context"), dict) else {}


def _exit_rule_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:
    """v0.48: 빠른손절/반등실패/지지부진 조기종료 판단.

    기존 익절/손절/보호청산/시간종료를 지우지 않는다.
    - quick_stop: 아예 초반 반응이 없는 후보
    - bounce_fail: +0.1~0.5% 정도 반응 후 다시 무너지는 후보
    - slow_early_exit: 10분 이상 시간만 쓰는 후보
    """
    ctx = _position_entry_ctx(pos)
    age_sec = max(0.0, age_min * 60.0)
    buy = float_any(ctx.get("micro_trade_buy_ratio_30"), default=0.0)
    spread = float_any(ctx.get("micro_spread_pct"), default=999.0)
    pull = float_any(ctx.get("pullback_quality_score"), default=0.0)
    money3 = float_any(ctx.get("money_flow_3m") or ctx.get("turnover_3m"), default=0.0)
    rebreak = float_any(ctx.get("rebreakout_strength"), default=0.0)
    price_recheck = float_any(ctx.get("price_recheck_pct"), default=0.0)
    sell_pressure = bool(ctx.get("micro_sell_trade_pressure") or ctx.get("micro_ask_wall_pressure"))
    drawdown_from_peak = max(0.0, peak - net)

    # 1) 아예 반응 없이 밀리는 후보: 기존 -1.2% 손절 전에 먼저 정리.
    if bool(control.get("quick_stop_enabled", True)):
        min_age = float_any(control.get("quick_stop_min_age_sec"), default=90.0)
        max_age = float_any(control.get("quick_stop_max_age_sec"), default=600.0)
        peak_under = float_any(control.get("quick_stop_peak_under_pct"), default=0.20)
        current_under = float_any(control.get("quick_stop_current_under_pct"), default=-0.35)
        weak_buy = float_any(control.get("quick_stop_weak_buy_ratio"), default=0.50)
        wide_spread = float_any(control.get("quick_stop_wide_spread_pct"), default=0.25)
        weak_reason = []
        if buy > 0 and buy <= weak_buy:
            weak_reason.append(f"매수비 {buy:.2f}")
        if spread < 900 and spread >= wide_spread:
            weak_reason.append(f"스프레드 {spread:.2f}%")
        if price_recheck < 0:
            weak_reason.append(f"가격재확인 {price_recheck:.2f}%")
        if sell_pressure:
            weak_reason.append("매도압박")
        if min_age <= age_sec <= max_age and peak <= peak_under and net <= current_under and weak_reason:
            return "quick_stop", f"진입 {int(age_sec)}초 / 최고 {peak:+.2f}% / 현재 {net:+.2f}% / " + ", ".join(weak_reason[:3])

    # 2) 조금 반등했다가 실패하는 후보: KAIA처럼 +0.2~0.4% 줬다가 크게 밀리는 유형.
    if bool(control.get("bounce_fail_enabled", True)):
        min_age = float_any(control.get("bounce_fail_min_age_sec"), default=300.0)
        max_age = float_any(control.get("bounce_fail_max_age_sec"), default=900.0)
        peak_min = float_any(control.get("bounce_fail_peak_min_pct"), default=0.12)
        peak_max = float_any(control.get("bounce_fail_peak_max_pct"), default=0.55)
        current_under = float_any(control.get("bounce_fail_current_under_pct"), default=-0.55)
        dd_min = float_any(control.get("bounce_fail_drawdown_from_peak_pct"), default=0.70)
        weak_buy = float_any(control.get("bounce_fail_weak_buy_ratio"), default=0.55)
        wide_spread = float_any(control.get("bounce_fail_wide_spread_pct"), default=0.20)
        fail_reason = []
        if buy > 0 and buy <= weak_buy:
            fail_reason.append(f"매수비 {buy:.2f}")
        if spread < 900 and spread >= wide_spread:
            fail_reason.append(f"스프레드 {spread:.2f}%")
        if price_recheck < 0:
            fail_reason.append(f"가격재확인 {price_recheck:.2f}%")
        if sell_pressure:
            fail_reason.append("매도압박")
        # 외부 약점이 뚜렷하지 않아도, 고점 대비 큰 되밀림 자체는 반등실패 신호로 본다.
        if min_age <= age_sec <= max_age and peak_min <= peak <= peak_max and net <= current_under and drawdown_from_peak >= dd_min:
            extra = (", " + ", ".join(fail_reason[:2])) if fail_reason else ""
            return "bounce_fail", f"진입 {int(age_sec)}초 / 최고 {peak:+.2f}% → 현재 {net:+.2f}% / 되밀림 {drawdown_from_peak:.2f}%{extra}"

    # 3) 시간만 쓰고 안 가는 후보: 기존 20분 지지부진 전 10분 부근에서 정리.
    if bool(control.get("slow_early_enabled", True)):
        min_min = float_any(control.get("slow_early_minutes"), default=10.0)
        peak_under = float_any(control.get("slow_early_peak_under_pct"), default=0.10)
        current_under = float_any(control.get("slow_early_current_under_pct"), default=0.05)
        pull_under = float_any(control.get("slow_early_pullback_under"), default=1.0)
        weak_buy = float_any(control.get("slow_early_weak_buy_ratio"), default=0.50)
        low_money = float_any(control.get("slow_early_low_money3_krw"), default=15_000_000.0)
        weak_reason = []
        if pull > 0 and pull < pull_under:
            weak_reason.append(f"눌림 {pull:.2f}")
        if buy > 0 and buy <= weak_buy:
            weak_reason.append(f"매수비 {buy:.2f}")
        if money3 > 0 and money3 < low_money:
            weak_reason.append(f"3분돈 {money3/10000:.0f}만")
        if rebreak > 0 and rebreak < 1.20:
            weak_reason.append(f"재돌파 {rebreak:.2f}")
        if age_min >= min_min and peak <= peak_under and net <= current_under and weak_reason:
            return "slow_early_exit", f"보유 {age_min:.1f}분 / 최고 {peak:+.2f}% / 현재 {net:+.2f}% / " + ", ".join(weak_reason[:3])

    return None, ""

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
    exit_rule_reason = ""

    early_reason, early_note = _exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if early_reason:
        exit_reason = early_reason
        exit_rule_reason = early_note
    elif net <= float_any(control.get("stop_loss_pct"), default=-1.2):
        exit_reason = "stop_loss"
        exit_rule_reason = f"기존 손절선 도달: 현재 {net:+.2f}%"
    elif peak >= float_any(control.get("protect_trigger_pct"), default=0.9) and net <= float_any(control.get("protect_floor_pct"), default=0.2):
        exit_reason = "protect_stop_after_tp"
        exit_rule_reason = f"익절권 후 보호선: 최고 {peak:+.2f}% / 현재 {net:+.2f}%"
    elif net >= float_any(control.get("take_profit_pct"), default=1.2):
        exit_reason = "take_profit"
        exit_rule_reason = f"기존 익절선 도달: 현재 {net:+.2f}%"
    elif age_min >= float_any(control.get("slow_minutes"), default=20) and peak < float_any(control.get("slow_peak_under_pct"), default=0.25) and net <= 0.10:
        exit_reason = "slow_no_progress"
        exit_rule_reason = f"기존 지지부진: 보유 {age_min:.1f}분 / 최고 {peak:+.2f}% / 현재 {net:+.2f}%"
    elif age_min >= float_any(control.get("time_exit_minutes"), default=120):
        exit_reason = "time_exit"
        exit_rule_reason = f"기존 시간종료: 보유 {age_min:.1f}분"

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
        "exit_mode": pos.get("exit_mode") or ((pos.get("entry_context") or {}).get("exit_mode") if isinstance(pos.get("entry_context"), dict) else ""),
        "exit_mode_label": pos.get("exit_mode_label") or ((pos.get("entry_context") or {}).get("exit_mode_label") if isinstance(pos.get("entry_context"), dict) else ""),
        "exit_mode_note": pos.get("exit_mode_note") or ((pos.get("entry_context") or {}).get("exit_mode_note") if isinstance(pos.get("entry_context"), dict) else ""),
        "exit_mode_strong_hits": pos.get("exit_mode_strong_hits") or ((pos.get("entry_context") or {}).get("exit_mode_strong_hits") if isinstance(pos.get("entry_context"), dict) else []),
        "exit_mode_weak_hits": pos.get("exit_mode_weak_hits") or ((pos.get("entry_context") or {}).get("exit_mode_weak_hits") if isinstance(pos.get("entry_context"), dict) else []),
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
        "exit_rule": exit_reason,
        "exit_rule_label": exit_reason_kr(exit_reason),
        "exit_rule_reason": exit_rule_reason,
        "close_trigger_age_sec": hold_sec,
        "early_peak_pct": round(peak, 4),
        "early_low_pct": round(float_any(pos.get("trough_pct"), default=0.0), 4),
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
    "quick_stop": "빠른손절 종료",
    "bounce_fail": "반등실패 조기손절",
    "slow_early_exit": "지지부진 조기종료",
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
    # v0.46: 과거 row의 opened_at_text가 UTC 문자열로 저장돼 있어도, 숫자 timestamp가 있으면 KST로 다시 표시한다.
    ts = float_any((row or {}).get("opened_at"), default=0.0)
    if ts > 0:
        return iso_ts(ts)
    txt = str((row or {}).get("opened_at_text") or "").strip()
    return txt or "-"


def closed_text_from_row(row: Dict[str, Any]) -> str:
    # v0.46: 과거 row의 closed_at_text가 UTC 문자열로 저장돼 있어도, 숫자 timestamp가 있으면 KST로 다시 표시한다.
    ts = float_any((row or {}).get("closed_at"), default=0.0)
    if ts > 0:
        return iso_ts(ts)
    txt = str((row or {}).get("closed_at_text") or "").strip()
    return txt or "-"


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
        exit_mode_line(pos),
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
        exit_mode_line(closed),
        f"청산규칙: {exit_reason_kr(reason)}" + (f" / {closed.get('exit_rule_reason')}" if closed.get('exit_rule_reason') else ""),
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



def _recent_summary_closed(limit: int = 300) -> List[Dict[str, Any]]:
    rows = rows_since_baseline(read_jsonl(FILES["closed"], max_lines=max(limit, 300)), "closed_at")
    cutoff = now() - 3600.0
    out = [r for r in rows if float_any((r or {}).get("closed_at"), default=0.0) >= cutoff]
    out.sort(key=lambda r: float_any((r or {}).get("closed_at"), default=0.0), reverse=True)
    return out


def _candidate_reason_short(row: Dict[str, Any]) -> str:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else row
    reason = str(row.get("summary_reason") or row.get("final_entry_label") or row.get("trade_ready_label") or row.get("reason") or "-")
    risk = row.get("quality_risk_tags") or ctx.get("quality_risk_tags") or []
    if isinstance(risk, list) and risk:
        reason = (reason + " / " + " / ".join(str(x) for x in risk[:2])).strip(" / ")
    return reason[:90]


def _summary_line_from_closed(row: Dict[str, Any]) -> str:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    return (
        f"- {short_ticker(row.get('ticker'))}: {float_any(row.get('pnl_pct'), default=0.0):+.2f}% / "
        f"최고 {float_any(row.get('peak_pct'), default=0.0):+.2f}% / {exit_reason_kr(str(row.get('exit_reason') or 'unknown'))} / "
        f"보유 {row.get('hold_text') or (str(row.get('age_min') or '-') + '분')} / "
        f"3분돈 {fmt_money_krw_short(ctx.get('money_flow_3m') or row.get('money_flow_3m'))} / "
        f"매수비 {float_any(ctx.get('micro_trade_buy_ratio_30'), default=0.0):.2f}"
    )


def _summary_line_from_candidate(row: Dict[str, Any]) -> str:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else row
    return (
        f"- {short_ticker(row.get('ticker') or row.get('market') or row.get('symbol'))}: "
        f"점수 {float_any(row.get('score'), default=0.0):.2f} / "
        f"3분돈 {fmt_money_krw_short(ctx.get('money_flow_3m') or row.get('money_flow_3m') or row.get('turnover_3m'))} / "
        f"micro {yes_fresh(ctx.get('micro_fresh'))} / WS {yes_fresh(ctx.get('ws_fresh'))} / "
        f"{_candidate_reason_short(row)}"
    )

def format_recheck_summary(rows: List[Dict[str, Any]], control: Dict[str, Any]) -> str:
    """v0.48: 알림을 줄이는 것이 아니라 후보 흐름을 1시간 단위로 분류해서 보여준다."""
    max_rows = int(float_any(control.get("recheck_summary_max_rows"), default=8))
    recent_closed = _recent_summary_closed()
    good = [r for r in recent_closed if float_any(r.get("pnl_pct"), default=0.0) > 0]
    bad = [r for r in recent_closed if float_any(r.get("pnl_pct"), default=0.0) <= 0]
    good.sort(key=lambda r: float_any(r.get("pnl_pct"), default=0.0), reverse=True)
    bad.sort(key=lambda r: float_any(r.get("pnl_pct"), default=0.0))

    # 놓친 후보: OPEN은 안 됐지만 점수/돈흐름이 높은 재확인·관찰 후보.
    missed_watch = []
    for r in rows or []:
        ctx = r.get("entry_context") if isinstance(r.get("entry_context"), dict) else r
        if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready")):
            continue
        score = float_any(r.get("score"), default=0.0)
        money3 = float_any(ctx.get("money_flow_3m") or r.get("money_flow_3m") or r.get("turnover_3m"), default=0.0)
        if score >= 4.45 or money3 >= 20_000_000:
            missed_watch.append(r)
    missed_watch.sort(key=lambda r: (float_any(r.get("score"), default=0.0), float_any((r.get("entry_context") or r).get("money_flow_3m") or r.get("turnover_3m"), default=0.0)), reverse=True)

    # 버린 게 맞았는지 볼 후보: 위험 사유가 뚜렷한 관찰/보류 후보.
    rejected_ok = []
    danger_words = ["스프레드", "매도", "정보 오래", "과폭발", "약함", "VWAP", "가격재확인"]
    for r in rows or []:
        reason = _candidate_reason_short(r)
        if any(w in reason for w in danger_words):
            rejected_ok.append(r)
    rejected_ok = rejected_ok[:max_rows]

    lines = [
        "🕐 후보품질 1시간 요약",
        "- 정식 OPEN/CLOSED 알림은 즉시 유지합니다.",
        "- 재확인/관찰/놓친 후보는 시끄럽게 울리지 않고 1시간 단위로 묶어 봅니다.",
        "",
        f"요약: 종료 {len(recent_closed)}건 / 재확인·관찰 {len(rows or [])}개",
        "",
        "[좋았던 후보]",
    ]
    lines += [_summary_line_from_closed(r) for r in good[:max_rows]] or ["- 없음"]
    lines += ["", "[안 좋았던 후보]"]
    lines += [_summary_line_from_closed(r) for r in bad[:max_rows]] or ["- 없음"]
    lines += ["", "[놓친 후보 후보군]"]
    lines += [_summary_line_from_candidate(r) for r in missed_watch[:max_rows]] or ["- 없음"]
    lines += ["", "[버린 게 맞았는지 확인할 후보]"]
    lines += [_summary_line_from_candidate(r) for r in rejected_ok[:max_rows]] or ["- 없음"]
    lines += ["", "설정: /palerts"]
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
    if not rows and not _recent_summary_closed():
        state["last_recheck_summary_at"] = now()
        save_alert_state(state)
        return
    send_message(format_recheck_summary(rows, control))
    state["last_recheck_summary_at"] = now()
    state["last_recheck_summary_count"] = len(rows)
    save_alert_state(state)


def notify_strict_events(opened: List[Dict[str, Any]], closed: List[Dict[str, Any]], control: Dict[str, Any]) -> None:
    # v0.48: 정식 OPEN/CLOSED 즉시 알림은 유지한다.
    # 재확인/관찰/놓친 후보는 알림을 없애지 않고 1시간 후보품질 요약으로 묶는다.
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
        f"- 알림: 정식 OPEN/CLOSED 즉시 ON / 재확인·관찰은 1시간 요약",
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



def _paper_error_line_ts(line: str) -> float:
    try:
        m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", str(line or ""))
        if not m:
            return 0.0
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return 0.0


def perror_text(full: bool = False) -> str:
    if full:
        return "🧯 paper_bot_error.log /perror_full\n\n" + tail_file(FILES["error"], 120)[-4500:]
    if not FILES["error"].exists():
        return "🧯 paper_bot_error.log\n\n✅ 오류로그 파일 없음"
    try:
        lines = FILES["error"].read_text(encoding="utf-8", errors="ignore").splitlines()
        recent_marked = [ln for ln in lines if _paper_error_line_ts(ln) >= PROGRAM_STARTED_TS]
        size = FILES["error"].stat().st_size if FILES["error"].exists() else 0
        out = ["🧯 paper_bot_error.log", ""]
        if recent_marked:
            out.append(f"⚠️ 새 실행 이후 오류 흔적 {len(recent_marked)}건")
            out.extend(recent_marked[-8:])
        else:
            out.append("✅ 새 실행 이후 오류 없음")
            out.append(f"- 과거 오류 흔적 있음 / 로그크기 {size} bytes")
            out.append("- 자세히: /perror_full")
        return "\n".join(out)
    except Exception as exc:
        return f"🧯 paper_bot_error.log\n\n⚠️ 오류로그 요약 실패: {exc}\n자세히: /perror_full"

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
            "명령어: /pbatch /pstatus /pcheck /ponce /pstart /pstop /prestart /plog /perror /perror_full /pscore /pversion_score /popen /pfiles /pcontrol /palerts",
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
        return perror_text(False)
    if cmd == "/perror_full":
        return perror_text(True)
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
        return "\n".join(lines)

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
            f"- 후보품질 시간요약: {c.get('notify_recheck_summary')} / {int(float_any(c.get('recheck_summary_interval_sec'), default=3600))}초",
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



# ===============================
# paper_bot v0.49: latest scan_id/TTL fallback + OPEN 청산감시 표시
# - main v236의 scan_id/TTL 보강을 소비한다.
# - 구 latest row가 top-level scan_id를 빼먹어도 entry_context/raw에서 복구한다.
# - OPEN별 왜 아직 안 닫혔는지 감시 상태를 /pstatus, /popen에 표시한다.
# ===============================

def _v049_ctx(ev: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance((ev or {}).get("entry_context"), dict):
        return (ev or {}).get("entry_context") or {}
    if isinstance((ev or {}).get("profile"), dict):
        return (ev or {}).get("profile") or {}
    return {}


def _v049_raw(ev: Dict[str, Any]) -> Dict[str, Any]:
    return (ev or {}).get("raw") if isinstance((ev or {}).get("raw"), dict) else {}


def _v049_first(ev: Dict[str, Any], *keys: str, default: Any = "") -> Any:
    ctx = _v049_ctx(ev)
    raw = _v049_raw(ev)
    for k in keys:
        for src in (ev, ctx, raw):
            if isinstance(src, dict):
                v = src.get(k)
                if v not in (None, "", "-"):
                    return v
    return default


def _v049_scan_id(ev: Dict[str, Any]) -> str:
    return str(_v049_first(ev, "scan_id", "snapshot_id", default="") or "")


def _v049_ts(ev: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = _v049_first(ev, k, default=0.0)
        x = float_any(v, default=0.0)
        if x > 0:
            return x
    return 0.0


def candidate_is_fresh(ev: Dict[str, Any], control: Dict[str, Any]) -> bool:  # type: ignore[override]
    nowv = now()
    exp = _v049_ts(ev, "expires_at", "source_expires_at")
    created = _v049_ts(ev, "created_at", "candidate_created_at", "source_created_at", "detected_ts", "ts", "timestamp")
    ttl = float_any(control.get("candidate_ttl_sec"), default=120.0)
    if exp > 0:
        return exp >= nowv
    if created > 0:
        return (nowv - created) <= ttl
    return False


def candidate_fresh_stats(path: Path, control: Optional[Dict[str, Any]] = None) -> Dict[str, int]:  # type: ignore[override]
    control = control or load_control()
    rows = read_jsonl(path, max_lines=int(float_any(control.get("candidate_read_max_lines"), default=2500)))
    fresh = sum(1 for r in rows if candidate_is_fresh(r, control))
    with_scan = sum(1 for r in rows if _v049_scan_id(r))
    return {"read_window": len(rows), "fresh": fresh, "expired": max(0, len(rows) - fresh), "with_scan_id": with_scan}


def latest_scan_filter(events: List[Dict[str, Any]], control: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:  # type: ignore[override]
    meta = {"enabled": bool(control.get("consume_latest_scan_only", True)), "latest_scan_id": "", "latest_ts": 0.0, "before": len(events), "after": len(events), "scan_id_recovered": 0}
    if not meta["enabled"] or not events:
        return events, meta
    normalized: List[Dict[str, Any]] = []
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        row = dict(ev)
        sid = _v049_scan_id(row)
        if sid and not row.get("scan_id"):
            row["scan_id"] = sid
            meta["scan_id_recovered"] = int(meta.get("scan_id_recovered", 0)) + 1
        ts = _v049_ts(row, "created_at", "candidate_created_at", "source_created_at", "detected_ts", "ts", "timestamp")
        if ts > 0 and not float_any(row.get("created_at"), default=0.0):
            row["created_at"] = ts
        normalized.append(row)
    scan_rows = [e for e in normalized if _v049_scan_id(e)]
    if scan_rows:
        def key(e: Dict[str, Any]):
            return (_v049_ts(e, "created_at", "candidate_created_at", "source_created_at"), _v049_scan_id(e))
        latest = max(scan_rows, key=key)
        sid = _v049_scan_id(latest)
        out = [e for e in normalized if _v049_scan_id(e) == sid]
        meta.update({"latest_scan_id": sid, "latest_ts": _v049_ts(latest, "created_at", "candidate_created_at", "source_created_at"), "after": len(out)})
        return out, meta
    latest_ts = max((_v049_ts(e, "created_at", "candidate_created_at", "source_created_at") for e in normalized), default=0.0)
    if latest_ts <= 0:
        meta.update({"after": len(normalized)})
        return normalized, meta
    win = max(1.0, float_any(control.get("latest_scan_window_sec"), default=8.0))
    out = [e for e in normalized if _v049_ts(e, "created_at", "candidate_created_at", "source_created_at") >= latest_ts - win]
    meta.update({"latest_ts": latest_ts, "after": len(out)})
    return out, meta


_v048_open_position = open_position

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    pos = _v048_open_position(pos_id, ev, control)
    sid = _v049_scan_id(ev)
    snapshot_id = str(_v049_first(ev, "snapshot_id", default=sid) or sid)
    cand_ts = _v049_ts(ev, "candidate_created_at", "created_at", "source_created_at")
    if sid:
        pos["scan_id"] = sid
    if snapshot_id:
        pos["snapshot_id"] = snapshot_id
    if cand_ts > 0:
        pos["candidate_created_at"] = cand_ts
    ctx = pos.get("entry_context") if isinstance(pos.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    if sid:
        ctx["scan_id"] = sid
    if snapshot_id:
        ctx["snapshot_id"] = snapshot_id
    if cand_ts > 0:
        ctx["candidate_created_at"] = cand_ts
    pos["entry_context"] = ctx
    return pos


def _v049_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:
    control = control or load_control()
    opened = float_any((pos or {}).get("opened_at"), default=now())
    age_sec = max(0.0, now() - opened)
    age_min = age_sec / 60.0
    net = float_any((pos or {}).get("last_pnl_pct"), default=0.0)
    peak = float_any((pos or {}).get("peak_pct"), default=0.0)
    reason, note = _exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if reason:
        return f"{exit_reason_kr(reason)} 조건감시 / {note}"
    if age_sec < float_any(control.get("quick_stop_min_age_sec"), default=180):
        return f"빠른손절 대기 {int(age_sec)}초 / 최고 {peak:+.2f}%"
    if peak > float_any(control.get("quick_stop_peak_under_pct"), default=0.10) and age_sec <= float_any(control.get("bounce_fail_max_age_sec"), default=900):
        dd = max(0.0, peak - net)
        return f"반등실패 감시 / 최고 {peak:+.2f}% 현재 {net:+.2f}% 되밀림 {dd:.2f}%"
    if age_min < float_any(control.get("slow_early_minutes"), default=10):
        return f"지지부진 감시 대기 {age_min:.1f}분 / 최고 {peak:+.2f}%"
    if age_min < float_any(control.get("time_exit_minutes"), default=120):
        return f"time_exit 대기 / 최고 {peak:+.2f}% 현재 {net:+.2f}%"
    return f"시간종료 감시 / 보유 {age_min:.1f}분"


def format_open_list(limit: int = 20) -> str:  # type: ignore[override]
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    control = load_control()
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:limit]
    lines = []
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        tag = "신규" if float_any(pos.get("opened_at"), default=0.0) >= baseline_ts() else "기존"
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        scan = str(pos.get("scan_id") or ((pos.get("entry_context") or {}).get("scan_id") if isinstance(pos.get("entry_context"), dict) else "") or "-")
        watch = _v049_open_watch_status(pos, control)
        lines.append(f"- {short_ticker(pos.get('ticker'))} / {tag} / {pos.get('lane','-')} / 진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / {float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / 보유 {hold_txt} / scan {scan} / 감시 {watch}")
    return "\n".join(lines)


# ===============================
# paper_bot v0.50: OPEN 전용 빠른 청산감시 + 손실 하드가드
# - 후보 읽기 cycle은 기존 8초를 유지한다.
# - OPEN 포지션은 worker wait 중 1~2초 단위로 따로 감시한다.
# - -2%까지 끌리는 것을 막기 위해 -1% 안팎 하드가드를 둔다.
# - quick_stop/bounce_fail/hard_loss_guard 내부키를 한글 출력으로 정리한다.
# ===============================

EXIT_REASON_KR.update({
    "quick_stop": "빠른손절 종료",
    "bounce_fail": "반등실패 조기손절",
    "slow_early_exit": "지지부진 조기종료",
    "hard_loss_guard": "손실하드가드 종료",
    "long_loss_guard": "장기손실 정리",
})

def exit_reason_kr(reason: Any) -> str:  # type: ignore[override]
    return EXIT_REASON_KR.get(str(reason or ""), str(reason or "-"))


def _v050_weak_reasons_from_ctx(ctx: Dict[str, Any]) -> List[str]:
    buy = float_any(ctx.get("micro_trade_buy_ratio_30"), default=0.0)
    spread = float_any(ctx.get("micro_spread_pct"), default=999.0)
    pull = float_any(ctx.get("pullback_quality_score"), default=0.0)
    rebreak = float_any(ctx.get("rebreakout_strength"), default=0.0)
    price_recheck = float_any(ctx.get("price_recheck_pct"), default=0.0)
    bid_wall = float_any(ctx.get("micro_bid_ask_wall_ratio"), default=0.0)
    tags = " / ".join(str(x) for x in (ctx.get("quality_risk_tags") or [])) if isinstance(ctx.get("quality_risk_tags"), list) else str(ctx.get("quality_risk_tags") or "")
    reasons: List[str] = []
    if buy > 0 and buy <= 0.55:
        reasons.append(f"매수비 {buy:.2f}")
    if spread < 900 and spread >= 0.20:
        reasons.append(f"스프레드 {spread:.2f}%")
    if price_recheck < 0:
        reasons.append(f"가격재확인 {price_recheck:.2f}%")
    if rebreak > 0 and rebreak < 1.20:
        reasons.append(f"재돌파 {rebreak:.2f}")
    if pull > 0 and pull < 1.00:
        reasons.append(f"눌림 {pull:.2f}")
    if bid_wall > 0 and bid_wall < 0.80:
        reasons.append(f"매수벽 {bid_wall:.2f}")
    if bool(ctx.get("micro_sell_trade_pressure") or ctx.get("micro_ask_wall_pressure")):
        reasons.append("매도압박")
    if "재돌파힘 약함" in tags:
        reasons.append("재돌파힘 약함")
    if "눌림품질 약함" in tags:
        reasons.append("눌림품질 약함")
    if "매도벽" in tags:
        reasons.append("매도벽 주의")
    return list(dict.fromkeys(reasons))


def _exit_rule_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:  # type: ignore[override]
    ctx = _position_entry_ctx(pos)
    age_sec = max(0.0, age_min * 60.0)
    drawdown = max(0.0, peak - net)
    weak = _v050_weak_reasons_from_ctx(ctx)

    # 0) 최우선 장부 보호: 어떤 후보든 -1% 안팎을 넘으면 더 끌지 않는다.
    if bool(control.get("hard_loss_guard_enabled", True)):
        hard_pct = float_any(control.get("hard_loss_guard_pct"), default=-0.95)
        if net <= hard_pct:
            return "hard_loss_guard", f"현재 {net:+.2f}% / 하드가드 {hard_pct:+.2f}% / 최고 {peak:+.2f}%"

    # 1) 초반 약반응/무반응 손절: +0.2% 이하만 주고 밀리면 기존 손절 전 정리.
    if bool(control.get("quick_stop_enabled", True)):
        min_age = float_any(control.get("quick_stop_min_age_sec"), default=90.0)
        max_age = float_any(control.get("quick_stop_max_age_sec"), default=600.0)
        peak_under = float_any(control.get("quick_stop_peak_under_pct"), default=0.20)
        current_under = float_any(control.get("quick_stop_current_under_pct"), default=-0.35)
        if min_age <= age_sec <= max_age and peak <= peak_under and net <= current_under:
            # 외부 약점이 뚜렷하거나 5분 이상 반응이 없으면 자른다.
            if weak or age_sec >= 300:
                note = ", ".join(weak[:3]) if weak else "초반반응 없음"
                return "quick_stop", f"진입 {int(age_sec)}초 / 최고 {peak:+.2f}% / 현재 {net:+.2f}% / {note}"

    # 2) 조금 갔다가 되밀리는 반등실패형.
    if bool(control.get("bounce_fail_enabled", True)):
        min_age = float_any(control.get("bounce_fail_min_age_sec"), default=180.0)
        max_age = float_any(control.get("bounce_fail_max_age_sec"), default=1800.0)
        peak_min = float_any(control.get("bounce_fail_peak_min_pct"), default=0.10)
        peak_max = float_any(control.get("bounce_fail_peak_max_pct"), default=0.70)
        current_under = float_any(control.get("bounce_fail_current_under_pct"), default=-0.45)
        dd_min = float_any(control.get("bounce_fail_drawdown_from_peak_pct"), default=0.60)
        if min_age <= age_sec <= max_age and peak_min <= peak <= peak_max and net <= current_under and drawdown >= dd_min:
            extra = (" / " + ", ".join(weak[:2])) if weak else ""
            return "bounce_fail", f"진입 {int(age_sec)}초 / 최고 {peak:+.2f}% → 현재 {net:+.2f}% / 되밀림 {drawdown:.2f}%{extra}"

    # 3) 오래 들고 있는 손실 포지션 정리: HOOK 같은 장기 손실 방지.
    if bool(control.get("long_loss_guard_enabled", True)):
        min_age = float_any(control.get("long_loss_guard_min_age_sec"), default=900.0)
        current_under = float_any(control.get("long_loss_guard_current_under_pct"), default=-0.45)
        peak_under = float_any(control.get("long_loss_guard_peak_under_pct"), default=0.60)
        if age_sec >= min_age and net <= current_under and peak < peak_under:
            return "long_loss_guard", f"보유 {age_min:.1f}분 / 최고 {peak:+.2f}% / 현재 {net:+.2f}%"

    # 4) 시간만 쓰고 안 가는 후보.
    if bool(control.get("slow_early_enabled", True)):
        min_min = float_any(control.get("slow_early_minutes"), default=10.0)
        peak_under = float_any(control.get("slow_early_peak_under_pct"), default=0.10)
        current_under = float_any(control.get("slow_early_current_under_pct"), default=0.05)
        if age_min >= min_min and peak <= peak_under and net <= current_under:
            note = ", ".join(weak[:3]) if weak else "최고수익 약함"
            return "slow_early_exit", f"보유 {age_min:.1f}분 / 최고 {peak:+.2f}% / 현재 {net:+.2f}% / {note}"
    return None, ""


_v050_original_run_cycle = run_cycle

def monitor_open_positions(control: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """OPEN 포지션만 빠르게 감시한다. 후보파일은 읽지 않는다."""
    global _CYCLE_PRICE_MAP
    started = now()
    control = control or load_control()
    with _state_lock:
        open_pos = load_open()
        if not open_pos:
            return {"checked": 0, "closed": 0, "elapsed_sec": round(now() - started, 3)}
        # OPEN은 보통 소수라 가격맵 1회 조회만으로 빠르게 감시한다.
        _CYCLE_PRICE_MAP = fetch_all_bithumb_prices()
        closed_items: List[Dict[str, Any]] = []
        checked = 0
        for pos_id, pos in list(open_pos.items()):
            try:
                checked += 1
                updated, closed = update_position(pos, control)
                if closed:
                    closed_items.append(closed)
                    open_pos.pop(pos_id, None)
                    append_jsonl(FILES["closed"], closed)
                else:
                    open_pos[pos_id] = updated
            except Exception as exc:
                log_error("monitor_open_position", exc)
        save_open(open_pos)
        if closed_items:
            try:
                notify_strict_events([], closed_items, control)
            except Exception as exc:
                log_error("monitor_notify_close", exc)
        # status에 빠른 감시 흔적만 가볍게 보강한다.
        st = load_json(FILES["status"], {})
        if not isinstance(st, dict):
            st = {}
        st.update({
            "version": VERSION,
            "updated_at": now(),
            "updated_at_text": iso_ts(),
            "fast_monitor_checked": checked,
            "fast_monitor_closed": len(closed_items),
            "fast_monitor_elapsed_sec": round(now() - started, 3),
            "fast_monitor_note": "v0.50: OPEN 전용 1~2초 청산감시",
            "open_total": len(open_pos),
            "closed_total": closed_count(),
        })
        save_json(FILES["status"], st)
        if closed_items:
            log(f"fast_monitor closed={len(closed_items)} checked={checked} elapsed={round(now()-started,3)}s")
        return {"checked": checked, "closed": len(closed_items), "elapsed_sec": round(now() - started, 3)}


def worker_loop() -> None:  # type: ignore[override]
    log("worker_loop v0.50 started")
    next_full = 0.0
    while not _stop_event.is_set():
        try:
            control = load_control()
            heartbeat_log("worker")
            if control.get("running"):
                nowv = now()
                if nowv >= next_full:
                    _v050_original_run_cycle()
                    next_full = now() + float_any(control.get("loop_seconds"), default=8.0)
                else:
                    monitor_open_positions(control)
            interval = max(0.8, min(3.0, float_any(control.get("open_monitor_interval_sec"), default=1.5)))
            _stop_event.wait(interval)
        except Exception as exc:
            log_error("worker_loop_v050", exc)
            _stop_event.wait(2)
    log("worker_loop v0.50 stopped")


def _v050_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:
    control = control or load_control()
    opened = float_any((pos or {}).get("opened_at"), default=now())
    age_sec = max(0.0, now() - opened)
    age_min = age_sec / 60.0
    net = float_any((pos or {}).get("last_pnl_pct"), default=0.0)
    peak = float_any((pos or {}).get("peak_pct"), default=0.0)
    reason, note = _exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if reason:
        return f"{exit_reason_kr(reason)} 조건감시 / {note}"
    hard_pct = float_any(control.get("hard_loss_guard_pct"), default=-0.95)
    if net <= hard_pct + 0.25:
        return f"손실하드가드 근접 / 현재 {net:+.2f}% / 기준 {hard_pct:+.2f}% / 최고 {peak:+.2f}%"
    if age_sec < float_any(control.get("quick_stop_min_age_sec"), default=90):
        return f"빠른손절 대기 {int(age_sec)}초 / 최고 {peak:+.2f}%"
    if age_min < float_any(control.get("slow_early_minutes"), default=10):
        return f"반응감시 / 최고 {peak:+.2f}% 현재 {net:+.2f}%"
    if age_min < float_any(control.get("time_exit_minutes"), default=120):
        return f"time_exit 대기 / 최고 {peak:+.2f}% 현재 {net:+.2f}%"
    return f"시간종료 감시 / 보유 {age_min:.1f}분"


def _v049_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    return _v050_open_watch_status(pos, control)


# /palerts 출력 문구 보강
_v050_original_alert_policy_text = alert_policy_text if 'alert_policy_text' in globals() else None


# ===============================
# paper_bot v0.52: 돈흐름 단일검증 표시/장부화면 기준 정리
# - 전략 판단/청산조건은 변경하지 않는다.
# - 메인봇이 넘긴 strategy_key/route/strategy_label을 OPEN/CLOSED에 보존한다.
# - /pstatus, /popen 기본화면은 돈흐름 단일검증 중심으로 보여주고, 과거 장부는 보관됨으로만 표시한다.
# ===============================

PAPER_FOCUS_STRATEGY_KEY = "money_reaccel_main"
PAPER_FOCUS_STRATEGY_LABEL = "돈흐름 재가속 단일검증"


def _v052_get_raw(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = (row or {}).get("raw")
    return raw if isinstance(raw, dict) else {}


def _v052_get_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    ctx = (row or {}).get("entry_context")
    return ctx if isinstance(ctx, dict) else {}


def _v052_get_field(row: Dict[str, Any], *keys: str) -> Any:
    raw = _v052_get_raw(row)
    ctx = _v052_get_ctx(row)
    for k in keys:
        if (row or {}).get(k) not in (None, ""):
            return (row or {}).get(k)
        if raw.get(k) not in (None, ""):
            return raw.get(k)
        if ctx.get(k) not in (None, ""):
            return ctx.get(k)
    return ""


def _v052_is_focus_strategy(row: Dict[str, Any]) -> bool:
    val = str(_v052_get_field(row, "strategy_key", "paper_strategy_key", "route", "paper_route", "strategy") or "")
    lab = str(_v052_get_field(row, "strategy_label", "paper_strategy_label", "strategy_display") or "")
    return val == PAPER_FOCUS_STRATEGY_KEY or PAPER_FOCUS_STRATEGY_KEY in val or "돈흐름" in lab or "돈흐름" in val


def _v052_strategy_label(row: Dict[str, Any]) -> str:
    if _v052_is_focus_strategy(row):
        return PAPER_FOCUS_STRATEGY_LABEL
    raw = str(_v052_get_field(row, "strategy_label", "paper_strategy_label", "strategy_display", "strategy", "route") or "")
    return strategy_kr(raw) if raw else "-"


def _v052_route_label(row: Dict[str, Any]) -> str:
    route = str(_v052_get_field(row, "paper_route", "route", "paper_strategy_key", "strategy_key") or "")
    return route or "-"


_v052_base_strategy_kr = strategy_kr

def strategy_kr(strategy: Any) -> str:  # type: ignore[override]
    s = str(strategy or "")
    if s == PAPER_FOCUS_STRATEGY_KEY or "money_reaccel" in s or "돈흐름" in s:
        return PAPER_FOCUS_STRATEGY_LABEL
    return _v052_base_strategy_kr(strategy)


_v052_base_open_position = open_position

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    pos = _v052_base_open_position(pos_id, ev, control)
    if _v052_is_focus_strategy(ev):
        pos["strategy"] = PAPER_FOCUS_STRATEGY_LABEL
        pos["strategy_key"] = PAPER_FOCUS_STRATEGY_KEY
        pos["strategy_label"] = PAPER_FOCUS_STRATEGY_LABEL
        pos["paper_strategy_key"] = PAPER_FOCUS_STRATEGY_KEY
        pos["paper_strategy_label"] = PAPER_FOCUS_STRATEGY_LABEL
        pos["route"] = PAPER_FOCUS_STRATEGY_KEY
        pos["paper_route"] = PAPER_FOCUS_STRATEGY_KEY
        pos["main_mode"] = "money_reaccel_only"
        pos["money_reaccel_score"] = float_any(ev.get("money_reaccel_score"), default=0.0)
        pos["money_reaccel_reasons"] = ev.get("money_reaccel_reasons") or []
        pos["single_strategy_mode"] = True
    return pos


_v052_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v052_base_update_position(pos, control)
    if closed is not None and _v052_is_focus_strategy(pos):
        closed.update({
            "strategy": PAPER_FOCUS_STRATEGY_LABEL,
            "strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "paper_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "paper_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "route": PAPER_FOCUS_STRATEGY_KEY,
            "paper_route": PAPER_FOCUS_STRATEGY_KEY,
            "main_mode": "money_reaccel_only",
            "money_reaccel_score": pos.get("money_reaccel_score") or _v052_get_raw(pos).get("money_reaccel_score"),
            "money_reaccel_reasons": pos.get("money_reaccel_reasons") or _v052_get_raw(pos).get("money_reaccel_reasons") or [],
        })
    return updated, closed


def _v052_focus_open_positions(open_pos: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    open_pos = open_pos if isinstance(open_pos, dict) else load_open()
    return {k: v for k, v in open_pos.items() if _v052_is_focus_strategy(v)}


def _v052_focus_closed_rows(limit: int = 20000) -> List[Dict[str, Any]]:
    rows = read_jsonl(FILES["closed"], max_lines=limit)
    return [r for r in rows if str(r.get("lane") or "strict") == "strict" and _v052_is_focus_strategy(r)]


def _v052_focus_candidate_counts(control: Optional[Dict[str, Any]] = None) -> Tuple[int, int]:
    control = control or load_control()
    strict_path = candidate_input_path("strict")
    shadow_path = candidate_input_path("shadow")
    strict_rows = [r for r in read_jsonl(strict_path, max_lines=int(float_any(control.get("candidate_read_max_lines"), default=6000))) if _v052_is_focus_strategy(r)]
    shadow_rows = [r for r in read_jsonl(shadow_path, max_lines=int(float_any(control.get("candidate_read_max_lines"), default=6000))) if _v052_is_focus_strategy(r)]
    return len(strict_rows), len(shadow_rows)


def format_open_list(limit: int = 20) -> str:  # type: ignore[override]
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    control = load_control()
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:limit]
    lines = []
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        focus = _v052_is_focus_strategy(pos)
        tag = "돈흐름" if focus else ("기존" if float_any(pos.get("opened_at"), default=0.0) < baseline_ts() else "신규")
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        scan = str(pos.get("scan_id") or ((_v052_get_ctx(pos)).get("scan_id") if isinstance(_v052_get_ctx(pos), dict) else "") or "-")
        watch = _v049_open_watch_status(pos, control)
        strat = _v052_strategy_label(pos)
        route = _v052_route_label(pos)
        lines.append(
            f"- {short_ticker(pos.get('ticker'))} / {tag} / {pos.get('lane','-')} / {strat} / route {route} / "
            f"진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / "
            f"{float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / 보유 {hold_txt} / scan {scan} / 감시 {watch}"
        )
    return "\n".join(lines)


def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v052_focus_open_positions(open_pos)
    focus_closed = _v052_focus_closed_rows()
    fs = file_stats()
    err_size = fs.get("error", {}).get("size", 0)
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    focus_strict_n, focus_shadow_n = _v052_focus_candidate_counts(control)
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재 기준: {PAPER_FOCUS_STRATEGY_LABEL}만 기본 화면에 표시",
        f"- OPEN: 돈흐름 {len(focus_open)} / 전체 {len(open_pos)}",
        "- OPEN 시간표:" if open_pos else "- OPEN 시간표: 없음",
        *(format_open_list(5).splitlines() if open_pos else []),
        f"- CLOSED: 돈흐름 단일검증 {len(focus_closed)}건 / 이전 장부 {closed_count()}건 보관됨",
        "- 기존 기록 삭제 없음: 과거 눌림/구전략 장부는 보관, 현재 판단에서는 제외",
        f"- 현재 latest 파일: 돈흐름 정식 {focus_strict_n}개 / 돈흐름 복기 {focus_shadow_n}개",
        "- 처리: trade_ready만 신규 OPEN / shadow 복기전용 / 기존 장부 보관",
        f"- 이번 cycle: open +{status.get('opened_this_cycle',0)} / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"- 직전 cycle 후보: 읽음 {pick.get('paper_file',0)} / OPEN통과 {status.get('opened_this_cycle',0)} / micro재확인대기 {pick.get('micro_preopen_wait',0)} / 관찰제외 {pick.get('strict_observe_skip',0)} / 복기 {pick.get('shadow_review_only_skip',0)} / 중복 {pick.get('same_ticker_skip',0)}",
        f"- 최신 scan 소비: before {pick.get('latest_scan_before',0)} → after {pick.get('latest_scan_after',0)} / scan_id {pick.get('latest_scan_id','-') or '-'}",
        f"- 알림: 정식 OPEN/CLOSED 즉시 ON / 재확인·관찰은 1시간 요약",
        f"- 업데이트: {status.get('updated_at_text','-')}",
        f"- flag: {'ON' if FILES['flag'].exists() else 'OFF'} / 오류로그 {err_size} bytes / bad_market {len(_bad_markets)}",
    ])


def version_score_text() -> str:  # type: ignore[override]
    focus_closed = _v052_focus_closed_rows()
    return "\n".join([
        f"📊 현재전략 모의매매 /pversion_score",
        f"- 기준: {PAPER_FOCUS_STRATEGY_LABEL}",
        fmt_stats("돈흐름 단일검증 정식", focus_closed),
        "- 이전 장부는 보관하지만 현재 판단에서는 제외",
    ])



# ===============================
# paper_bot v0.53: 돈흐름 꺼짐 보호청산
# - 돈흐름 재가속으로 들어갔으면, 진입 후 돈/체결/호가가 꺼질 때도 먼저 빠진다.
# - 기존 빠른손절/하드가드/익절/보호청산/시간종료는 유지한다.
# - 조건은 과민하게 두지 않고, +반응 후 고점대비 되밀림 + live micro 매도전환이 같이 보일 때만 작동한다.
# ===============================

DEFAULT_CONTROL.update({
    "flow_fade_guard_enabled": True,
    "flow_fade_min_age_sec": 120,
    "flow_fade_max_age_sec": 1800,
    "flow_fade_peak_min_pct": 0.35,
    "flow_fade_drawdown_from_peak_pct": 0.35,
    "flow_fade_current_under_pct": 0.35,
    "flow_fade_weak_buy_ratio": 0.40,
    "flow_fade_wide_spread_pct": 0.35,
    "flow_fade_wall_ratio_under": 0.70,
    "flow_fade_volume_drop_ratio": 0.45,
    "flow_fade_min_signals": 2,
})

FILES.setdefault("micro_cache", BASE_DIR / "clean_bithumb_micro_cache.json")
_V053_MICRO_CACHE: Dict[str, Any] = {"mtime": 0.0, "ts": 0.0, "rows": {}}
_V053_MICRO_CACHE_TTL_SEC = float(os.getenv("PAPER_BOT_V053_MICRO_CACHE_TTL_SEC", "1.2"))

EXIT_REASON_KR.update({
    "flow_fade_guard": "돈흐름 꺼짐 보호청산",
})


def _v053_micro_rows() -> Dict[str, Dict[str, Any]]:
    nowv = now()
    path = FILES.get("micro_cache", BASE_DIR / "clean_bithumb_micro_cache.json")
    try:
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except Exception:
        mtime = 0.0
    cached = _V053_MICRO_CACHE.get("rows") if isinstance(_V053_MICRO_CACHE.get("rows"), dict) else {}
    if cached and _V053_MICRO_CACHE.get("mtime") == mtime and nowv - float_any(_V053_MICRO_CACHE.get("ts"), default=0.0) <= _V053_MICRO_CACHE_TTL_SEC:
        return cached  # type: ignore[return-value]
    payload = load_json(path, {})
    raw = payload.get("rows") if isinstance(payload, dict) and isinstance(payload.get("rows"), dict) else payload
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            sym = normalize_ticker(k)
            if sym and isinstance(v, dict):
                row = dict(v)
                row.setdefault("ticker", sym)
                out[sym] = row
    _V053_MICRO_CACHE.update({"mtime": mtime, "ts": nowv, "rows": out})
    return out


def _v053_live_micro_row(ticker: Any) -> Dict[str, Any]:
    sym = normalize_ticker(ticker)
    if not sym:
        return {}
    return dict(_v053_micro_rows().get(sym) or {})


def _v053_live_micro_fresh(row: Dict[str, Any], max_age: float = 8.0) -> bool:
    if not row:
        return False
    ts = float_any(row.get("updated_ts"), row.get("ts"), default=0.0)
    return ts > 0 and now() - ts <= max_age


def _v053_entry_micro_total(ctx: Dict[str, Any]) -> float:
    return float_any(ctx.get("micro_trade_buy_krw_30"), default=0.0) + float_any(ctx.get("micro_trade_sell_krw_30"), default=0.0)


def _v053_live_micro_total(row: Dict[str, Any]) -> float:
    return float_any(row.get("trade_buy_krw_30"), row.get("micro_trade_buy_krw_30"), default=0.0) + float_any(row.get("trade_sell_krw_30"), row.get("micro_trade_sell_krw_30"), default=0.0)


def _v053_flow_fade_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:
    if not bool(control.get("flow_fade_guard_enabled", True)):
        return None, ""
    if not _v052_is_focus_strategy(pos):
        return None, ""
    age_sec = max(0.0, age_min * 60.0)
    min_age = float_any(control.get("flow_fade_min_age_sec"), default=120.0)
    max_age = float_any(control.get("flow_fade_max_age_sec"), default=1800.0)
    peak_min = float_any(control.get("flow_fade_peak_min_pct"), default=0.35)
    dd_min = float_any(control.get("flow_fade_drawdown_from_peak_pct"), default=0.35)
    current_under = float_any(control.get("flow_fade_current_under_pct"), default=0.35)
    if not (min_age <= age_sec <= max_age):
        return None, ""
    drawdown = max(0.0, peak - net)
    # 한 번은 반응했고, 그 후 고점에서 의미 있게 꺾인 경우만 본다.
    if peak < peak_min or drawdown < dd_min or net > current_under:
        return None, ""

    ctx = _position_entry_ctx(pos)
    live = _v053_live_micro_row(pos.get("ticker"))
    if not _v053_live_micro_fresh(live):
        return None, ""

    buy_ratio = float_any(live.get("trade_buy_ratio_30"), live.get("micro_trade_buy_ratio_30"), default=0.0)
    spread = float_any(live.get("micro_spread_pct"), default=999.0)
    wall_ratio = float_any(live.get("bid_ask_wall_ratio"), live.get("micro_bid_ask_wall_ratio"), default=0.0)
    sell_pressure = bool(live.get("sell_trade_pressure") or live.get("micro_sell_trade_pressure"))
    ask_wall_pressure = bool(live.get("ask_wall_pressure") or live.get("micro_ask_wall_pressure"))
    entry_total = _v053_entry_micro_total(ctx)
    live_total = _v053_live_micro_total(live)

    weak_buy = float_any(control.get("flow_fade_weak_buy_ratio"), default=0.40)
    wide_spread = float_any(control.get("flow_fade_wide_spread_pct"), default=0.35)
    wall_under = float_any(control.get("flow_fade_wall_ratio_under"), default=0.70)
    drop_ratio = float_any(control.get("flow_fade_volume_drop_ratio"), default=0.45)
    min_signals = int(float_any(control.get("flow_fade_min_signals"), default=2))

    reasons: List[str] = []
    if buy_ratio > 0 and buy_ratio <= weak_buy:
        reasons.append(f"매수비 약화 {buy_ratio:.2f}")
    if sell_pressure:
        reasons.append("매도체결 우세")
    if ask_wall_pressure:
        reasons.append("매도벽 압박")
    if wall_ratio > 0 and wall_ratio <= wall_under:
        reasons.append(f"매수벽 약화 {wall_ratio:.2f}")
    if spread < 900 and spread >= wide_spread:
        reasons.append(f"스프레드 확대 {spread:.2f}%")
    if entry_total > 0 and live_total > 0 and live_total <= entry_total * drop_ratio:
        reasons.append(f"30초 체결대금 감소 {live_total/10000:.0f}만")

    # 아주 강한 매도 전환이면 신호 1개라도 허용하되, 기본은 2개 이상 동시 확인.
    strong_sell = sell_pressure and buy_ratio > 0 and buy_ratio <= weak_buy and drawdown >= dd_min
    if len(reasons) >= min_signals or strong_sell:
        return "flow_fade_guard", f"최고 {peak:+.2f}% → 현재 {net:+.2f}% / 되밀림 {drawdown:.2f}% / " + ", ".join(reasons[:4])
    return None, ""


_v053_base_exit_rule_decision = _exit_rule_decision

def _exit_rule_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:  # type: ignore[override]
    # 하드가드/기본 손실 방어는 base가 먼저 처리한다. 다만 돈흐름 꺼짐은 빠른손절/반등실패보다 앞서 본다.
    # base 내부 첫 단계가 hard_loss_guard라, 이미 -1% 근처까지 간 후보는 기존 하드가드가 우선한다.
    base_reason, base_note = _v053_base_exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if base_reason == "hard_loss_guard":
        return base_reason, base_note
    flow_reason, flow_note = _v053_flow_fade_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if flow_reason:
        return flow_reason, flow_note
    return base_reason, base_note


_v053_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v053_base_update_position(pos, control)
    if closed is not None and str(closed.get("exit_reason") or "") == "flow_fade_guard":
        live = _v053_live_micro_row(closed.get("ticker") or updated.get("ticker") or pos.get("ticker"))
        closed.update({
            "flow_fade_guard": True,
            "flow_fade_live_buy_ratio": float_any(live.get("trade_buy_ratio_30"), live.get("micro_trade_buy_ratio_30"), default=0.0),
            "flow_fade_live_spread_pct": float_any(live.get("micro_spread_pct"), default=0.0),
            "flow_fade_live_wall_ratio": float_any(live.get("bid_ask_wall_ratio"), live.get("micro_bid_ask_wall_ratio"), default=0.0),
            "flow_fade_live_total_krw_30": _v053_live_micro_total(live),
        })
    return updated, closed


_v053_base_open_watch_status = _v050_open_watch_status

def _v050_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    control = control or load_control()
    opened = float_any((pos or {}).get("opened_at"), default=now())
    age_sec = max(0.0, now() - opened)
    age_min = age_sec / 60.0
    net = float_any((pos or {}).get("last_pnl_pct"), default=0.0)
    peak = float_any((pos or {}).get("peak_pct"), default=0.0)
    reason, note = _exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if reason:
        return f"{exit_reason_kr(reason)} 조건감시 / {note}"
    if _v052_is_focus_strategy(pos):
        live = _v053_live_micro_row(pos.get("ticker"))
        if _v053_live_micro_fresh(live):
            buy = float_any(live.get("trade_buy_ratio_30"), live.get("micro_trade_buy_ratio_30"), default=0.0)
            spread = float_any(live.get("micro_spread_pct"), default=999.0)
            return f"돈흐름 감시 / 최고 {peak:+.2f}% 현재 {net:+.2f}% / 매수비 {buy:.2f} 스프레드 {spread:.2f}%"
    return _v053_base_open_watch_status(pos, control)


def _v049_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    return _v050_open_watch_status(pos, control)



# ===============================
# paper_bot v0.54: 평균가 회귀 반등 단타 단일검증 표시 전환
# - 메인봇 v250의 새 focus strategy(vwap_reversion_main)를 현재전략으로 본다.
# - 돈흐름 전용 flow_fade_guard는 새 평균회귀 전략에는 적용하지 않는다.
# - TP분할 없음. 기존 전량익절/전량손절/기본 보호청산 구조 유지.
# - 과거 돈흐름/구전략 장부는 보관하되 현재 판단에서는 제외.
# ===============================

PAPER_FOCUS_STRATEGY_KEY = "vwap_reversion_main"
PAPER_FOCUS_STRATEGY_LABEL = "평균가 회귀 반등 단타"
PAPER_FOCUS_TAG = "평균회귀"
PAPER_FOCUS_MAIN_MODE = "vwap_reversion_only"


def _v054_text_has_focus(v: Any) -> bool:
    s = str(v or "")
    return (
        PAPER_FOCUS_STRATEGY_KEY in s
        or "vwap_reversion" in s
        or "평균가 회귀" in s
        or "평균회귀" in s
        or "VWAP 회귀" in s
    )


def _v052_is_focus_strategy(row: Dict[str, Any]) -> bool:  # type: ignore[override]
    val = str(_v052_get_field(row, "strategy_key", "paper_strategy_key", "route", "paper_route", "strategy") or "")
    lab = str(_v052_get_field(row, "strategy_label", "paper_strategy_label", "strategy_display") or "")
    mode = str(_v052_get_field(row, "main_mode", "paper_main_mode") or "")
    return _v054_text_has_focus(val) or _v054_text_has_focus(lab) or _v054_text_has_focus(mode)


_v054_base_strategy_kr = strategy_kr

def strategy_kr(strategy: Any) -> str:  # type: ignore[override]
    s = str(strategy or "")
    if _v054_text_has_focus(s):
        return PAPER_FOCUS_STRATEGY_LABEL
    # 돈흐름은 과거 장부 표시로만 남긴다.
    if "money_reaccel" in s or "돈흐름" in s:
        return "돈흐름 재가속(보관)"
    return _v054_base_strategy_kr(strategy)


_v054_base_open_position = open_position

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    pos = _v054_base_open_position(pos_id, ev, control)
    if _v052_is_focus_strategy(ev):
        pos.update({
            "strategy": PAPER_FOCUS_STRATEGY_LABEL,
            "strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "paper_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "paper_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "route": PAPER_FOCUS_STRATEGY_KEY,
            "paper_route": PAPER_FOCUS_STRATEGY_KEY,
            "main_mode": PAPER_FOCUS_MAIN_MODE,
            "paper_main_mode": PAPER_FOCUS_MAIN_MODE,
            "vwap_reversion_score": float_any(ev.get("vwap_reversion_score"), default=0.0),
            "vwap_reversion_reasons": ev.get("vwap_reversion_reasons") or [],
            "single_strategy_mode": True,
        })
    return pos


_v054_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v054_base_update_position(pos, control)
    if closed is not None and _v052_is_focus_strategy(pos):
        closed.update({
            "strategy": PAPER_FOCUS_STRATEGY_LABEL,
            "strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "paper_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "paper_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "route": PAPER_FOCUS_STRATEGY_KEY,
            "paper_route": PAPER_FOCUS_STRATEGY_KEY,
            "main_mode": PAPER_FOCUS_MAIN_MODE,
            "paper_main_mode": PAPER_FOCUS_MAIN_MODE,
            "vwap_reversion_score": pos.get("vwap_reversion_score") or _v052_get_raw(pos).get("vwap_reversion_score"),
            "vwap_reversion_reasons": pos.get("vwap_reversion_reasons") or _v052_get_raw(pos).get("vwap_reversion_reasons") or [],
        })
    return updated, closed


# 돈흐름 전용 보호청산은 v250 평균회귀 단일검증에서는 끈다. 기존 빠른손절/하드가드/익절/시간종료는 base에 남아 있다.
def _v053_flow_fade_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:  # type: ignore[override]
    return None, ""


def format_open_list(limit: int = 20) -> str:  # type: ignore[override]
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    control = load_control()
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:limit]
    lines = []
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        focus = _v052_is_focus_strategy(pos)
        tag = PAPER_FOCUS_TAG if focus else ("기존" if float_any(pos.get("opened_at"), default=0.0) < baseline_ts() else "신규")
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        scan = str(pos.get("scan_id") or ((_v052_get_ctx(pos)).get("scan_id") if isinstance(_v052_get_ctx(pos), dict) else "") or "-")
        watch = _v049_open_watch_status(pos, control)
        strat = _v052_strategy_label(pos)
        route = _v052_route_label(pos)
        lines.append(
            f"- {short_ticker(pos.get('ticker'))} / {tag} / {pos.get('lane','-')} / {strat} / route {route} / "
            f"진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / "
            f"{float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / 보유 {hold_txt} / scan {scan} / 감시 {watch}"
        )
    return "\n".join(lines)


def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v052_focus_open_positions(open_pos)
    focus_closed = _v052_focus_closed_rows()
    fs = file_stats()
    err_size = fs.get("error", {}).get("size", 0)
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    focus_strict_n, focus_shadow_n = _v052_focus_candidate_counts(control)
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재 기준: {PAPER_FOCUS_STRATEGY_LABEL}만 기본 화면에 표시",
        f"- OPEN: {PAPER_FOCUS_TAG} {len(focus_open)} / 전체 {len(open_pos)}",
        "- OPEN 시간표:" if open_pos else "- OPEN 시간표: 없음",
        *(format_open_list(5).splitlines() if open_pos else []),
        f"- CLOSED: {PAPER_FOCUS_TAG} 단일검증 {len(focus_closed)}건 / 이전 장부 {closed_count()}건 보관됨",
        "- 기존 기록 삭제 없음: 과거 돈흐름/눌림/구전략 장부는 보관, 현재 판단에서는 제외",
        f"- 현재 latest 파일: {PAPER_FOCUS_TAG} 정식 {focus_strict_n}개 / {PAPER_FOCUS_TAG} 복기 {focus_shadow_n}개",
        "- 처리: trade_ready만 신규 OPEN / shadow 복기전용 / 기존 장부 보관 / TP분할 없음",
        f"- 이번 cycle: open +{status.get('opened_this_cycle',0)} / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"- 직전 cycle 후보: 읽음 {pick.get('paper_file',0)} / OPEN통과 {status.get('opened_this_cycle',0)} / micro재확인대기 {pick.get('micro_preopen_wait',0)} / 관찰제외 {pick.get('strict_observe_skip',0)} / 복기 {pick.get('shadow_review_only_skip',0)} / 중복 {pick.get('same_ticker_skip',0)}",
        f"- 최신 scan 소비: before {pick.get('latest_scan_before',0)} → after {pick.get('latest_scan_after',0)} / scan_id {pick.get('latest_scan_id','-') or '-'}",
        f"- 알림: 정식 OPEN/CLOSED 즉시 ON / 재확인·관찰은 1시간 요약",
        f"- 업데이트: {status.get('updated_at_text','-')}",
        f"- flag: {'ON' if FILES['flag'].exists() else 'OFF'} / 오류로그 {err_size} bytes / bad_market {len(_bad_markets)}",
    ])


def version_score_text() -> str:  # type: ignore[override]
    focus_closed = _v052_focus_closed_rows()
    return "\n".join([
        f"📊 현재전략 모의매매 /pversion_score",
        f"- 기준: {PAPER_FOCUS_STRATEGY_LABEL}",
        fmt_stats("평균회귀 단일검증 정식", focus_closed),
        "- 이전 장부는 보관하지만 현재 판단에서는 제외",
        "- TP분할 없음: 소액 실전 전제라 전량청산 기준",
    ])



# ===============================
# paper_bot v0.55: 평균회귀 감시 문구/청산표시 찌꺼기 정리
# - v0.54에서 평균회귀 본선으로 바뀌었지만 OPEN 감시 문구에 "돈흐름 감시"가 남았다.
# - 실제 청산조건은 바꾸지 않고, 평균회귀 포지션의 표시/상태를 VWAP 회귀 기준으로 분리한다.
# - TP분할 없음, 장부 삭제 없음, 자동매수 관련 변경 없음.
# ===============================


def _v055_vwap_watch_note(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:
    control = control or load_control()
    opened = float_any((pos or {}).get("opened_at"), default=now())
    age_sec = max(0.0, now() - opened)
    age_min = age_sec / 60.0
    net = float_any((pos or {}).get("last_pnl_pct"), default=0.0)
    peak = float_any((pos or {}).get("peak_pct"), default=0.0)
    reason, note = _exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if reason:
        return f"{exit_reason_kr(reason)} 조건감시 / {note}"
    live = _v053_live_micro_row(pos.get("ticker"))
    buy = 0.0
    spread = 999.0
    if _v053_live_micro_fresh(live):
        buy = float_any(live.get("trade_buy_ratio_30"), live.get("micro_trade_buy_ratio_30"), default=0.0)
        spread = float_any(live.get("micro_spread_pct"), default=999.0)
    dd = max(0.0, peak - net)
    if peak >= 0.60 and net <= 0.10 and dd >= 0.45:
        base = f"평균회귀 되밀림 관찰 / 최고 {peak:+.2f}% 현재 {net:+.2f}% 되밀림 {dd:.2f}%"
    elif peak >= 0.60:
        base = f"평균회귀 보호감시 / 최고 {peak:+.2f}% 현재 {net:+.2f}%"
    else:
        base = f"평균회귀 감시 / 최고 {peak:+.2f}% 현재 {net:+.2f}%"
    if spread < 900:
        return f"{base} / 매수비 {buy:.2f} 스프레드 {spread:.2f}%"
    return base


_v055_base_open_watch_status = _v050_open_watch_status

def _v050_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    if _v052_is_focus_strategy(pos):
        return _v055_vwap_watch_note(pos, control)
    return _v055_base_open_watch_status(pos, control)


def _v049_open_watch_status(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    return _v050_open_watch_status(pos, control)





# ===============================
# paper_bot v0.56: 평균회귀 수익권 되밀림 보호청산
# - TP분할 없음: 소액 실전 전제의 전량청산 유지.
# - +0.6~0.7% 이상 갔다가 힘이 꺼질 때 +0.30% 안팎 수익은 지키는 구조.
# - RESOLV처럼 고점 근처를 유지하거나 매수세가 살아 있는 후보는 강제익절하지 않는다.
# - 진입조건/자동매수/장부 구조는 변경하지 않는다.
# ===============================

DEFAULT_CONTROL.update({
    "vwap_reversion_protect_enabled": True,
    "vwap_reversion_protect_min_age_sec": 120,
    "vwap_reversion_protect_peak_min_pct": 0.60,
    "vwap_reversion_protect_floor_pct": 0.30,
    "vwap_reversion_protect_drawdown_pct": 0.30,
    "vwap_reversion_protect_strong_buy_ratio": 0.58,
    "vwap_reversion_protect_wide_spread_pct": 0.35,
})

EXIT_REASON_KR.update({
    "vwap_reversion_protect": "평균회귀 되밀림 보호청산",
})


def _v056_is_vwap_focus(pos: Dict[str, Any]) -> bool:
    try:
        return _v052_is_focus_strategy(pos)
    except Exception:
        return _v054_text_has_focus(pos.get("strategy_key")) or _v054_text_has_focus(pos.get("route")) or _v054_text_has_focus(pos.get("strategy"))


def _v056_live_micro_view(pos: Dict[str, Any]) -> Tuple[bool, float, float]:
    live = _v053_live_micro_row((pos or {}).get("ticker"))
    if not _v053_live_micro_fresh(live):
        return False, 0.0, 999.0
    buy = float_any(live.get("trade_buy_ratio_30"), live.get("micro_trade_buy_ratio_30"), default=0.0)
    spread = float_any(live.get("micro_spread_pct"), default=999.0)
    return True, buy, spread


def _v056_vwap_reversion_protect_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:
    if not bool(control.get("vwap_reversion_protect_enabled", True)):
        return None, ""
    if not _v056_is_vwap_focus(pos):
        return None, ""
    age_sec = max(0.0, age_min * 60.0)
    min_age = float_any(control.get("vwap_reversion_protect_min_age_sec"), default=120.0)
    peak_min = float_any(control.get("vwap_reversion_protect_peak_min_pct"), default=0.60)
    floor = float_any(control.get("vwap_reversion_protect_floor_pct"), default=0.30)
    dd_min = float_any(control.get("vwap_reversion_protect_drawdown_pct"), default=0.30)
    if age_sec < min_age or peak < peak_min:
        return None, ""
    dd = max(0.0, peak - net)
    if dd < dd_min or net > floor:
        return None, ""

    fresh, buy, spread = _v056_live_micro_view(pos)
    strong_buy = float_any(control.get("vwap_reversion_protect_strong_buy_ratio"), default=0.58)
    wide_spread = float_any(control.get("vwap_reversion_protect_wide_spread_pct"), default=0.35)
    # 고점 근처가 아니고 이미 +0.30% 보호선까지 밀렸다면 보호청산한다.
    # 단, micro가 아주 강하고 스프레드가 정상인 경우만 한 번 더 버틸 여지를 둔다.
    if fresh and buy >= strong_buy and spread < wide_spread and net >= floor - 0.05:
        return None, ""
    extras = []
    if fresh:
        extras.append(f"매수비 {buy:.2f}")
        if spread < 900:
            extras.append(f"스프레드 {spread:.2f}%")
    return "vwap_reversion_protect", f"최고 {peak:+.2f}% → 현재 {net:+.2f}% / 되밀림 {dd:.2f}% / 보호선 +{floor:.2f}%" + ((" / " + ", ".join(extras[:2])) if extras else "")


_v056_base_exit_rule_decision = _exit_rule_decision

def _exit_rule_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:  # type: ignore[override]
    base_reason, base_note = _v056_base_exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)
    # 하드가드는 최우선 유지. 이미 익절/손절이 확정된 경우도 그대로 둔다.
    if base_reason in {"hard_loss_guard", "stop_loss", "take_profit"}:
        return base_reason, base_note
    protect_reason, protect_note = _v056_vwap_reversion_protect_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if protect_reason:
        return protect_reason, protect_note
    return base_reason, base_note


_v056_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v056_base_update_position(pos, control)
    if closed is not None and str(closed.get("exit_reason") or "") == "vwap_reversion_protect":
        live = _v053_live_micro_row(closed.get("ticker") or updated.get("ticker") or pos.get("ticker"))
        closed.update({
            "vwap_reversion_protect": True,
            "vwap_protect_live_buy_ratio": float_any(live.get("trade_buy_ratio_30"), live.get("micro_trade_buy_ratio_30"), default=0.0),
            "vwap_protect_live_spread_pct": float_any(live.get("micro_spread_pct"), default=0.0),
        })
    return updated, closed


_v056_base_vwap_watch_note = _v055_vwap_watch_note

def _v055_vwap_watch_note(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    control = control or load_control()
    opened = float_any((pos or {}).get("opened_at"), default=now())
    age_sec = max(0.0, now() - opened)
    age_min = age_sec / 60.0
    net = float_any((pos or {}).get("last_pnl_pct"), default=0.0)
    peak = float_any((pos or {}).get("peak_pct"), default=0.0)
    protect_reason, protect_note = _v056_vwap_reversion_protect_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if protect_reason:
        return f"{exit_reason_kr(protect_reason)} 조건감시 / {protect_note}"
    return _v056_base_vwap_watch_note(pos, control)


# ===============================
# paper_bot v0.57: 평균회귀 무반응 빠른 실패청산
# - 메인 v258에서 정식 OPEN 전 돌아섬 확인을 추가했다.
# - 그래도 들어간 뒤 2~3분 동안 최고수익이 거의 없고 -0.45~-0.55%로 밀리는 후보는
#   하드가드(-1%대)까지 끌지 않고 평균회귀 실패로 정리한다.
# - RESOLV류 큰 수익 후보는 최고수익이 생기면 이 규칙 대상이 아니며, v0.56 되밀림 보호청산은 유지한다.
# ===============================

DEFAULT_CONTROL.update({
    "vwap_reversion_no_reaction_enabled": True,
    "vwap_reversion_no_reaction_min_age_sec": 120,
    "vwap_reversion_no_reaction_max_age_sec": 720,
    "vwap_reversion_no_reaction_peak_under_pct": 0.10,
    "vwap_reversion_no_reaction_current_under_pct": -0.45,
    "vwap_reversion_no_reaction_late_age_sec": 180,
    "vwap_reversion_no_reaction_late_peak_under_pct": 0.20,
    "vwap_reversion_no_reaction_late_current_under_pct": -0.55,
})

EXIT_REASON_KR.update({
    "vwap_reversion_no_reaction": "평균회귀 무반응 빠른정리",
})


def _v057_vwap_no_reaction_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:
    if not bool(control.get("vwap_reversion_no_reaction_enabled", True)):
        return None, ""
    if not _v056_is_vwap_focus(pos):
        return None, ""
    age_sec = max(0.0, age_min * 60.0)
    min_age = float_any(control.get("vwap_reversion_no_reaction_min_age_sec"), default=120.0)
    max_age = float_any(control.get("vwap_reversion_no_reaction_max_age_sec"), default=720.0)
    peak_under = float_any(control.get("vwap_reversion_no_reaction_peak_under_pct"), default=0.10)
    cur_under = float_any(control.get("vwap_reversion_no_reaction_current_under_pct"), default=-0.45)
    late_age = float_any(control.get("vwap_reversion_no_reaction_late_age_sec"), default=180.0)
    late_peak = float_any(control.get("vwap_reversion_no_reaction_late_peak_under_pct"), default=0.20)
    late_cur = float_any(control.get("vwap_reversion_no_reaction_late_current_under_pct"), default=-0.55)
    if min_age <= age_sec <= max_age and peak <= peak_under and net <= cur_under:
        return "vwap_reversion_no_reaction", f"진입 {int(age_sec)}초 / 최고 {peak:+.2f}% / 현재 {net:+.2f}% / 평균회귀 반응없음"
    if late_age <= age_sec <= max_age and peak <= late_peak and net <= late_cur:
        return "vwap_reversion_no_reaction", f"진입 {int(age_sec)}초 / 최고 {peak:+.2f}% / 현재 {net:+.2f}% / 반응 약함 지속"
    return None, ""


_v057_base_exit_rule_decision = _exit_rule_decision

def _exit_rule_decision(pos: Dict[str, Any], control: Dict[str, Any], *, age_min: float, net: float, peak: float) -> Tuple[Optional[str], str]:  # type: ignore[override]
    # 하드가드보다 먼저 평균회귀 무반응 실패를 잡는다. net 조건이 음수권이라 익절/큰수익 후보에는 영향 없다.
    nr_reason, nr_note = _v057_vwap_no_reaction_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if nr_reason:
        return nr_reason, nr_note
    return _v057_base_exit_rule_decision(pos, control, age_min=age_min, net=net, peak=peak)


_v057_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v057_base_update_position(pos, control)
    if closed is not None and str(closed.get("exit_reason") or "") == "vwap_reversion_no_reaction":
        closed["vwap_reversion_no_reaction"] = True
    return updated, closed


_v057_base_vwap_watch_note = _v055_vwap_watch_note

def _v055_vwap_watch_note(pos: Dict[str, Any], control: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    control = control or load_control()
    opened = float_any((pos or {}).get("opened_at"), default=now())
    age_min = max(0.0, now() - opened) / 60.0
    net = float_any((pos or {}).get("last_pnl_pct"), default=0.0)
    peak = float_any((pos or {}).get("peak_pct"), default=0.0)
    nr_reason, nr_note = _v057_vwap_no_reaction_decision(pos, control, age_min=age_min, net=net, peak=peak)
    if nr_reason:
        return f"{exit_reason_kr(nr_reason)} 조건감시 / {nr_note}"
    return _v057_base_vwap_watch_note(pos, control)

# ===============================
# paper_bot v0.58: 3전략 실험진입 표시/알림/성과 보강
# - /pscore chat_id NameError 수정은 본문 command_response에서 return 방식으로 교체했다.
# - 3전략 실험진입은 모의매매 OPEN은 유지하되 즉시 OPEN 알림은 시간요약으로 돌린다.
# - OPEN이 많아져도 /pstatus는 전략별 요약 + 최근 일부만 보여준다.
# - CLOSED에는 paper_strategy_key/label을 보존해 메인봇 /score 전략별 성과가 잡히게 한다.
# ===============================

V058_EXPERIMENT_KEYS = {"low_rebound_vwap", "low_rebound_reaccel", "breakout_retest"}
V058_EXPERIMENT_LABELS = {
    "low_rebound_vwap": "저점방어+평균회귀",
    "low_rebound_reaccel": "저점방어+재가속",
    "breakout_retest": "돌파/재돌파 초입",
}

def _v058_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    return (row or {}).get("entry_context") if isinstance((row or {}).get("entry_context"), dict) else {}

def _v058_strategy_key(row: Dict[str, Any]) -> str:
    ctx = _v058_ctx(row)
    return str((row or {}).get("paper_strategy_key") or (row or {}).get("strategy_key") or (row or {}).get("route") or ctx.get("paper_strategy_key") or ctx.get("strategy_key") or ctx.get("route") or "")

def _v058_strategy_label(row: Dict[str, Any]) -> str:
    key = _v058_strategy_key(row)
    ctx = _v058_ctx(row)
    return str((row or {}).get("paper_strategy_label") or (row or {}).get("strategy_label") or (row or {}).get("strategy") or ctx.get("paper_strategy_label") or V058_EXPERIMENT_LABELS.get(key) or strategy_kr((row or {}).get("strategy")) or key or "-")

def _v058_is_experiment(row: Dict[str, Any]) -> bool:
    ctx = _v058_ctx(row)
    key = _v058_strategy_key(row)
    return bool(key in V058_EXPERIMENT_KEYS or (row or {}).get("experiment_paper") or ctx.get("experiment_paper") or str((row or {}).get("strategy_test_version") or ctx.get("strategy_test_version") or "").startswith("v26"))

_v058_prev_should_send_open_alert = should_send_open_alert

def should_send_open_alert(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[bool, str]:  # type: ignore[override]
    if _v058_is_experiment(pos):
        return False, f"3전략 실험진입 즉시알림 생략({_v058_strategy_label(pos)})"
    return _v058_prev_should_send_open_alert(pos, control)

_v058_prev_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v058_prev_update_position(pos, control)
    if closed is not None and _v058_is_experiment(pos):
        key = _v058_strategy_key(pos)
        label = _v058_strategy_label(pos)
        ctx = _v058_ctx(pos)
        closed.update({
            "strategy": label,
            "strategy_key": key,
            "strategy_label": label,
            "paper_strategy_key": key,
            "paper_strategy_label": label,
            "route": key,
            "paper_route": key,
            "strategy_test_version": str(pos.get("strategy_test_version") or ctx.get("strategy_test_version") or "v269_three_strategy_experiment_display"),
            "experiment_paper": True,
            "paper_experiment": True,
            "paper_entry_class": "3전략 실험진입",
        })
        cctx = closed.get("entry_context") if isinstance(closed.get("entry_context"), dict) else {}
        cctx = dict(cctx)
        cctx.update({
            "paper_strategy_key": key,
            "paper_strategy_label": label,
            "strategy_key": key,
            "strategy_label": label,
            "experiment_paper": True,
            "paper_experiment": True,
            "paper_entry_class": "3전략 실험진입",
        })
        closed["entry_context"] = cctx
    return updated, closed

def _v058_open_strategy_counts(open_pos: Dict[str, Dict[str, Any]]) -> str:
    c = Counter(_v058_strategy_label(p) for p in (open_pos or {}).values())
    if not c:
        return "-"
    return " / ".join(f"{k} {v}" for k, v in c.most_common(6))

def _v058_stats_for(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = [float_any(r.get("pnl_pct"), default=0.0) for r in rows or []]
    wins = sum(1 for v in vals if v > 0)
    total = sum(vals)
    maxv = max(vals) if vals else 0.0
    max_ex = total - maxv if vals else 0.0
    return {"n": len(vals), "wins": wins, "win_rate": (wins/len(vals)*100.0 if vals else 0.0), "total": total, "max": maxv, "max_ex": max_ex}

def _v058_perf_line(label: str, rows: List[Dict[str, Any]], width: int = 14) -> str:
    st = _v058_stats_for(rows)
    if st["n"] <= 0:
        return f"{label:<{width}} ❔   0전  승률   -    합산      -   최대      -   최대제외      -"
    icon = "✅" if st["total"] > 0 and st["max_ex"] >= 0 else ("⚠️" if st["total"] > 0 else "❌")
    return f"{label:<{width}} {icon} {st['n']:3d}전  승률 {st['win_rate']:5.1f}%  합산 {st['total']:+8.2f}%  최대 {st['max']:+7.2f}%  최대제외 {st['max_ex']:+8.2f}%"

def _v058_strategy_perf_text(rows: List[Dict[str, Any]]) -> List[str]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows or []:
        if _v058_is_experiment(r):
            groups[_v058_strategy_label(r)].append(r)
    lines = ["[전략별 3전략 성과]"]
    if not groups:
        return lines + ["- 3전략 실험진입 CLOSED 아직 없음"]
    order = ["저점방어+평균회귀", "저점방어+재가속", "돌파/재돌파 초입"]
    for name in order:
        lines.append(_v058_perf_line(name, groups.get(name, []), width=14))
    return lines

_v058_prev_format_open_list = format_open_list

def format_open_list(limit: int = 20) -> str:  # type: ignore[override]
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:max(1, min(limit, 6))]
    lines = [f"전략별 OPEN: {_v058_open_strategy_counts(open_pos)}"]
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        watch = _v049_open_watch_status(pos, load_control())
        cls = "실험" if _v058_is_experiment(pos) else (PAPER_FOCUS_TAG if _v052_is_focus_strategy(pos) else "기존")
        lines.append(
            f"- {short_ticker(pos.get('ticker'))} / {cls} / {_v058_strategy_label(pos)} / "
            f"진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / "
            f"{float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / {hold_txt} / 감시 {watch} / 최고 {fmt_pct(pos.get('peak_pct'))}"
        )
    if len(open_pos) > len(rows):
        lines.append(f"- 나머지 {len(open_pos)-len(rows)}개는 /popen 에서 확인")
    return "\n".join(lines)

def _v061_open_version_counts(open_pos: Dict[str, Dict[str, Any]]) -> str:
    c = Counter(str((p or {}).get("brain_version") or (p or {}).get("bot_version") or (p or {}).get("version") or "이전/미상") for p in (open_pos or {}).values())
    return " / ".join(f"{k} {v}" for k, v in c.most_common(5)) if c else "-"


def _v061_recent_open_rows(open_pos: Dict[str, Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    rows = list((open_pos or {}).values())
    rows.sort(key=lambda x: float_any((x or {}).get("opened_at"), default=0.0), reverse=True)
    return rows[:max(1, min(limit, 6))]


def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    fs = file_stats()
    err_size = fs.get("error", {}).get("size", 0)
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    closed_rows = read_jsonl(FILES["closed"], max_lines=12000)
    recent_closed = [r for r in closed_rows if now() - float_any(r.get("closed_at"), default=0.0) <= 6*3600]

    rows = _v061_recent_open_rows(open_pos, 5)
    lines = [
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- OPEN: 전체 {len(open_pos)} / 전략별 {_v058_open_strategy_counts(open_pos)}",
        f"- OPEN 버전: {_v061_open_version_counts(open_pos)}",
        "- 신규 모의매매 OPEN: 중지(v274 전략층 초기화) / 기존 OPEN만 감시·청산",
        "",
        "[1] 최근 OPEN",
    ]
    if not rows:
        lines.append("- OPEN 없음")
    else:
        for pos in rows:
            age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
            hold_txt = hold_text_from_seconds(age * 60)
            opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
            watch = _v049_open_watch_status(pos, load_control())
            cls = "실험" if _v058_is_experiment(pos) else (PAPER_FOCUS_TAG if _v052_is_focus_strategy(pos) else "기존")
            lines.append(
                f"- {short_ticker(pos.get('ticker'))} / {cls} / {_v058_strategy_label(pos)} / "
                f"진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / "
                f"{float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / {hold_txt} / 감시 {watch} / 최고 {fmt_pct(pos.get('peak_pct'))}"
            )
        if len(open_pos) > len(rows):
            lines.append(f"- 나머지 {len(open_pos)-len(rows)}개는 /popen 또는 상세 명령에서 확인")
    lines += [
        "",
        *_v058_strategy_perf_text(recent_closed),
        "",
        f"[cycle] 신규OPEN 중지 / close +{status.get('closed_this_cycle',0)} / {status.get('elapsed_sec','-')}s",
        f"[후보] 읽음 {pick.get('paper_file',0)} / 신규OPEN중지 {pick.get('new_open_paused',0)} / 복기 {pick.get('shadow_review_only_skip',0)} / 중복 {pick.get('same_ticker_skip',0)}",
        f"[알림] 기존 OPEN/CLOSED만 / 신규 OPEN 알림 없음",
        f"[상태] 업데이트 {status.get('updated_at_text','-')} / 오류로그 {err_size} bytes / 자세히 /perror",
    ]
    return "\n".join(lines)

# ===============================
# paper_bot v0.63: 신규 모의매매 OPEN 중지 / 기존 OPEN 관리
# - v274 메인봇 재료공장 전환에 맞춰 신규 OPEN을 생성하지 않는다.
# - /pscore, /pstatus 요약은 유지한다.
# - 실제 청산조건/장부 삭제 없음.
# ===============================
try:
    VERSION = "paper_bot_v0.63"
except Exception:
    pass


# v0.63: v274 재료수집 전환. 신규 후보를 소비해 OPEN하지 않는다.
# - 기존 OPEN 여러 개는 유지 감시하되 신규 OPEN은 중지한다.
# - 실제 청산조건/장부 삭제 없음.
try:
    VERSION = "paper_bot_v0.63"
except Exception:
    pass

if __name__ == "__main__":
    main()
