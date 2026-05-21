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

VERSION = "paper_bot_v0.66"
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
    "max_open_strict": 0,  # v0.72: paper 검증은 고정 개수 제한 없음(0=무제한, 서버 보호는 별도)
    "max_open_shadow": 0,  # shadow는 복기 전용이라 OPEN하지 않는다
    "max_new_per_cycle": 0,  # 0 = 신규 후보를 작은 숫자로 자르지 않음
    "new_open_paused": False,  # v0.64: sweep_vwap_recovery 단일 전략 paper OPEN 재개
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
    "protect_trigger_pct": 0.70,
    "protect_floor_pct": 0.30,
    "stop_loss_pct": -0.55,

    # v0.50: OPEN 포지션 손실 방어. 후보 읽기는 8초 유지, OPEN 감시는 더 촘촘히 본다.
    "open_monitor_interval_sec": 1.5,
    "hard_loss_guard_enabled": True,
    "hard_loss_guard_pct": -0.95,
    "long_loss_guard_enabled": False,
    "long_loss_guard_min_age_sec": 900,
    "long_loss_guard_current_under_pct": -0.45,
    "long_loss_guard_peak_under_pct": 0.60,
    "slow_minutes": 20,
    "slow_peak_under_pct": 0.25,
    # v0.47: 실제 paper 청산모드. 기존 stop/take/slow는 유지하고, 반응 없는 후보만 먼저 정리한다.
    "quick_stop_enabled": False,
    "quick_stop_min_age_sec": 90,
    "quick_stop_max_age_sec": 600,
    "quick_stop_peak_under_pct": 0.20,
    "quick_stop_current_under_pct": -0.35,
    "quick_stop_weak_buy_ratio": 0.50,
    "quick_stop_wide_spread_pct": 0.25,
    "slow_early_enabled": False,
    "slow_early_minutes": 10,
    "slow_early_peak_under_pct": 0.10,
    "slow_early_current_under_pct": 0.05,
    "slow_early_pullback_under": 1.00,
    "slow_early_weak_buy_ratio": 0.50,
    "slow_early_low_money3_krw": 15_000_000,
    "time_exit_minutes": 15,
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
    """v0.72: 단일 JSON 저장 입구.

    이전 방식은 모든 writer가 같은 <file>.tmp를 공유해서, 명령어와 loop가
    동시에 paper_bot_control.json을 저장하면 한쪽 os.replace 뒤 다른 쪽 tmp가
    사라지는 FileNotFoundError가 날 수 있었다. 고유 tmp로 원인을 제거한다.
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tid = threading.get_ident() if 'threading' in globals() else 0
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{tid}.{time.time_ns()}.tmp")
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
    # 구 control에 과거 80/120/160/9999 같은 값이 남아 있으면 0(고정한도 없음)으로 정리한다.
    changed = False
    try:
        mos = int(float(control.get("max_open_strict", 30)))
        if mos < 0 or mos in {80, 120, 160, 9999} or mos > 60:
            control["max_open_strict"] = 0
            changed = True
    except Exception:
        control["max_open_strict"] = 0
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
    # v0.64: sweep_vwap_recovery 단일 전략만 신규 모의매매 OPEN을 허용한다.
    if control.get("new_open_paused") is not False:
        control["new_open_paused"] = False
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
# paper_bot v0.64: sweep_vwap_recovery 단일 전략 검증 본선
# - v0.52~v0.63의 돈흐름/평균회귀/3전략 표시·보정 override를 active path에서 제거한다.
# - 신규 OPEN은 sweep_vwap_recovery trade_ready 후보만 허용한다.
# - 청산은 설계서 고정값만 사용한다: 익절 +1.20 / 보호 +0.70 이후 +0.30 / 손절 -0.55 / 시간 15분.
# - 기존 OPEN/CLOSED 장부 삭제 없음.
# ===============================
try:
    VERSION = "paper_bot_v0.66"
except Exception:
    pass

PAPER_FOCUS_STRATEGY_KEY = "sweep_vwap_recovery"
PAPER_FOCUS_STRATEGY_LABEL = "저점 쓸림 후 VWAP 회복 단타"
PAPER_FOCUS_TAG = "쓸림VWAP"
PAPER_FOCUS_MAIN_MODE = "sweep_vwap_recovery_only"

DEFAULT_CONTROL.update({
    "new_open_paused": False,
    "max_open_strict": 0,
    "max_open_shadow": 0,
    "max_new_per_cycle": 0,
    "open_trade_ready_only": True,
    "open_shadow_positions": False,
    "shadow_review_only": True,
    "take_profit_pct": 1.20,
    "protect_trigger_pct": 0.70,
    "protect_floor_pct": 0.30,
    "stop_loss_pct": -0.55,
    "time_exit_minutes": 15,
    "quick_stop_enabled": False,
    "slow_early_enabled": False,
    "long_loss_guard_enabled": False,
    "notify_on_strict_open": True,
    "notify_on_strict_close": True,
    "notify_open_auto_ready_only": True,
    "notify_open_min_score": 4.45,
    "notify_open_require_final_pass": True,
    "notify_open_require_micro_fresh": True,
    "preopen_micro_recheck": True,
    "notify_close_only_alerted": False,
})

_v064_base_load_control = load_control

def load_control() -> Dict[str, Any]:  # type: ignore[override]
    control = _v064_base_load_control()
    fixed = {
        "new_open_paused": False,
        "max_open_strict": 0,
        "max_open_shadow": 0,
        "max_new_per_cycle": 0,
        "open_trade_ready_only": True,
        "open_shadow_positions": False,
        "shadow_review_only": True,
        "take_profit_pct": 1.20,
        "protect_trigger_pct": 0.70,
        "protect_floor_pct": 0.30,
        "stop_loss_pct": -0.55,
        "time_exit_minutes": 15,
        "quick_stop_enabled": False,
        "slow_early_enabled": False,
        "long_loss_guard_enabled": False,
        "notify_close_only_alerted": False,
    }
    changed = False
    for k, v in fixed.items():
        if control.get(k) != v:
            control[k] = v
            changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
            set_pause_flags(bool(control.get("running")))
        except Exception:
            pass
    return control


def _v064_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    return (row or {}).get("entry_context") if isinstance((row or {}).get("entry_context"), dict) else {}


def _v064_field(row: Dict[str, Any], *keys: str) -> Any:
    ctx = _v064_ctx(row)
    raw = (row or {}).get("raw") if isinstance((row or {}).get("raw"), dict) else {}
    for k in keys:
        if (row or {}).get(k) not in (None, ""):
            return (row or {}).get(k)
        if raw.get(k) not in (None, ""):
            return raw.get(k)
        if ctx.get(k) not in (None, ""):
            return ctx.get(k)
    return ""


def _v064_strategy_key(row: Dict[str, Any]) -> str:
    return str(_v064_field(row, "paper_strategy_key", "strategy_key", "paper_route", "route", "strategy") or "")


def _v052_is_focus_strategy(row: Dict[str, Any]) -> bool:  # compatibility name used by older helpers
    key = _v064_strategy_key(row)
    label = str(_v064_field(row, "paper_strategy_label", "strategy_label", "strategy_display", "strategy") or "")
    return key == PAPER_FOCUS_STRATEGY_KEY or PAPER_FOCUS_STRATEGY_KEY in key or "저점 쓸림" in label or "VWAP 회복" in label


def _v054_text_has_focus(v: Any) -> bool:
    s = str(v or "")
    return PAPER_FOCUS_STRATEGY_KEY in s or "sweep_vwap" in s or "저점 쓸림" in s or "VWAP 회복" in s


_base_strategy_kr_v064 = strategy_kr

def strategy_kr(strategy: Any) -> str:  # type: ignore[override]
    s = str(strategy or "")
    if _v054_text_has_focus(s):
        return PAPER_FOCUS_STRATEGY_LABEL
    return _base_strategy_kr_v064(strategy)


_v064_base_open_position = open_position

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    pos = _v064_base_open_position(pos_id, ev, control)
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
            "single_strategy_mode": True,
            "strategy_test_version": "v288_sweep_vwap_recovery_30_closed_fixed",
            "take_profit_pct_at_open": float_any(control.get("take_profit_pct"), default=1.20),
            "protect_trigger_pct_at_open": float_any(control.get("protect_trigger_pct"), default=0.70),
            "protect_floor_pct_at_open": float_any(control.get("protect_floor_pct"), default=0.30),
            "stop_loss_pct_at_open": float_any(control.get("stop_loss_pct"), default=-0.55),
            "time_exit_minutes_at_open": float_any(control.get("time_exit_minutes"), default=15),
        })
        ctx = pos.get("entry_context") if isinstance(pos.get("entry_context"), dict) else {}
        ctx = dict(ctx)
        ctx.update({
            "paper_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "paper_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
        })
        pos["entry_context"] = ctx
    return pos


_v064_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    updated, closed = _v064_base_update_position(pos, control)
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
            "strategy_test_version": "v288_sweep_vwap_recovery_30_closed_fixed",
        })
        ctx = closed.get("entry_context") if isinstance(closed.get("entry_context"), dict) else {}
        ctx = dict(ctx)
        ctx.update({
            "paper_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "paper_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
            "strategy_key": PAPER_FOCUS_STRATEGY_KEY,
            "strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
        })
        closed["entry_context"] = ctx
    return updated, closed


def _v064_focus_closed_rows(limit: int = 20000) -> List[Dict[str, Any]]:
    rows = read_jsonl(FILES["closed"], max_lines=limit)
    return [r for r in rows if str(r.get("lane") or "strict") == "strict" and _v052_is_focus_strategy(r)]


def _v064_focus_open_positions(open_pos: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    open_pos = open_pos if isinstance(open_pos, dict) else load_open()
    return {k: v for k, v in (open_pos or {}).items() if _v052_is_focus_strategy(v)}


def _v064_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = [float_any(r.get("net_pct"), r.get("pnl_pct"), default=0.0) for r in rows or []]
    wins = sum(1 for v in vals if v > 0)
    total = sum(vals)
    return {"n": len(vals), "wins": wins, "losses": len(vals)-wins, "win_rate": (wins/len(vals)*100 if vals else 0.0), "total": total, "avg": (total/len(vals) if vals else 0.0)}


def _v064_strategy_label(row: Dict[str, Any]) -> str:
    return PAPER_FOCUS_STRATEGY_LABEL if _v052_is_focus_strategy(row) else strategy_kr(_v064_field(row, "strategy", "route", "strategy_key"))


def _v064_open_strategy_counts(open_pos: Dict[str, Dict[str, Any]]) -> str:
    c = Counter(_v064_strategy_label(p) for p in (open_pos or {}).values())
    return " / ".join(f"{k} {v}" for k, v in c.most_common(5)) if c else "-"


def format_open_list(limit: int = 20) -> str:  # type: ignore[override]
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:max(1, min(limit, 10))]
    control = load_control()
    lines = [f"전략별 OPEN: {_v064_open_strategy_counts(open_pos)}"]
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        watch = _v049_open_watch_status(pos, control)
        cls = PAPER_FOCUS_TAG if _v052_is_focus_strategy(pos) else "기존"
        lines.append(
            f"- {short_ticker(pos.get('ticker'))} / {cls} / {_v064_strategy_label(pos)} / "
            f"진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / "
            f"{float_any(pos.get('last_pnl_pct'), default=0.0):+.2f}% / 보유 {hold_txt} / 감시 {watch} / 최고 {fmt_pct(pos.get('peak_pct'))}"
        )
    if len(open_pos) > len(rows):
        lines.append(f"- 나머지 {len(open_pos)-len(rows)}개는 /popen 에서 확인")
    return "\n".join(lines)


def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v064_focus_open_positions(open_pos)
    focus_closed = _v064_focus_closed_rows()
    fs = file_stats()
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    st = _v064_stats(focus_closed)
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        f"- 신규 OPEN: {'중지' if control.get('new_open_paused') else '허용'} / 단, {PAPER_FOCUS_STRATEGY_KEY} trade_ready만",
        f"- OPEN: 현재전략 {len(focus_open)} / 전체 {len(open_pos)} / 한도 {control.get('max_open_strict')}",
        f"- CLOSED: 현재전략 {st['n']}/30 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        f"- 청산고정: 익절 +{float_any(control.get('take_profit_pct'), default=1.2):.2f}% / 보호 +{float_any(control.get('protect_trigger_pct'), default=0.7):.2f}%→+{float_any(control.get('protect_floor_pct'), default=0.3):.2f}% / 손절 {float_any(control.get('stop_loss_pct'), default=-0.55):.2f}% / 시간 {float_any(control.get('time_exit_minutes'), default=15):.0f}분",
        "",
        "[후보]",
        f"- 읽음 {pick.get('paper_file',0)} / 신규중지 {pick.get('new_open_paused',0)} / not_ready {pick.get('not_trade_ready_skip',0)} / micro대기 {pick.get('micro_preopen_wait',0)} / 중복 {pick.get('same_ticker_skip',0)}",
        "",
        "[원칙]",
        "- CLOSED 30건 전 조건 변경 금지 / 오류·장부·정보수집 배관만 수정",
        f"- 오류로그 {fs.get('error', {}).get('size', 0)} bytes / 자세히 /perror",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# v0.65: 같은 코인 반복손실 재진입 차단
# 목적: 전략 조건을 새로 추가하는 게 아니라, 설계서에 있던
# "최근 같은 코인 반복 손실 금지"를 paper OPEN 단일 입구에 실제 연결한다.
# - OPEN 중복 차단은 기존 block_same_ticker_open이 담당한다.
# - 여기서는 CLOSED 직후 같은 ticker가 다시 strict 후보로 들어와 연속 손실나는 경로를 막는다.
# - paper 장부는 읽기만 하고 삭제/수정하지 않는다.
# ─────────────────────────────────────────────────────────────────────────────
PAPER_REPEAT_LOSS_LOOKBACK_SEC = float(os.getenv("PAPER_REPEAT_LOSS_LOOKBACK_SEC", str(6 * 3600)))
PAPER_SINGLE_LOSS_COOLDOWN_SEC = float(os.getenv("PAPER_SINGLE_LOSS_COOLDOWN_SEC", str(60 * 60)))
PAPER_REPEAT_LOSS_COOLDOWN_SEC = float(os.getenv("PAPER_REPEAT_LOSS_COOLDOWN_SEC", str(6 * 3600)))
PAPER_REPEAT_LOSS_THRESHOLD = int(float(os.getenv("PAPER_REPEAT_LOSS_THRESHOLD", "2")))


def _v065_closed_pnl(row: Dict[str, Any]) -> float:
    return float_any((row or {}).get("net_pct"), (row or {}).get("pnl_pct"), default=0.0)


def _v065_closed_time(row: Dict[str, Any]) -> float:
    return float_any((row or {}).get("closed_at"), default=0.0)


def _v065_recent_loss_block_map(now_ts: Optional[float] = None, limit: int = 6000) -> Dict[str, Dict[str, Any]]:
    """최근 CLOSED 기준 같은 코인 재진입 차단표를 만든다.

    차단 기준:
    - 같은 전략에서 직전 CLOSED가 손실이고 60분 이내면 차단.
    - 최근 6시간 내 같은 ticker 손실이 2회 이상이면 6시간 차단.

    이것은 조건 완화/강화가 아니라 paper OPEN 입구의 반복손실 보호장치다.
    """
    now_ts = float(now_ts or now())
    rows = read_jsonl(FILES["closed"], max_lines=limit)
    recent_by_ticker: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if not isinstance(r, dict) or not _v052_is_focus_strategy(r):
            continue
        t = short_ticker(r.get("ticker") or r.get("market") or r.get("symbol"))
        if not t:
            continue
        cts = _v065_closed_time(r)
        if cts <= 0 or now_ts - cts > max(PAPER_REPEAT_LOSS_LOOKBACK_SEC, PAPER_REPEAT_LOSS_COOLDOWN_SEC, PAPER_SINGLE_LOSS_COOLDOWN_SEC) + 300:
            continue
        recent_by_ticker[t].append(r)

    block: Dict[str, Dict[str, Any]] = {}
    for t, arr in recent_by_ticker.items():
        arr.sort(key=_v065_closed_time, reverse=True)
        last = arr[0]
        last_pnl = _v065_closed_pnl(last)
        last_ts = _v065_closed_time(last)
        recent_losses = [r for r in arr if now_ts - _v065_closed_time(r) <= PAPER_REPEAT_LOSS_LOOKBACK_SEC and _v065_closed_pnl(r) < 0]

        cooldown_until = 0.0
        reason = ""
        if len(recent_losses) >= max(1, PAPER_REPEAT_LOSS_THRESHOLD):
            latest_loss_ts = max(_v065_closed_time(r) for r in recent_losses)
            cooldown_until = latest_loss_ts + PAPER_REPEAT_LOSS_COOLDOWN_SEC
            reason = f"최근 {int(PAPER_REPEAT_LOSS_LOOKBACK_SEC//3600)}시간 손실 {len(recent_losses)}회"
        elif last_pnl < 0:
            cooldown_until = last_ts + PAPER_SINGLE_LOSS_COOLDOWN_SEC
            reason = "직전 CLOSED 손실"

        if cooldown_until > now_ts:
            block[t] = {
                "ticker": t,
                "reason": reason,
                "loss_count": len(recent_losses),
                "last_pnl": round(last_pnl, 4),
                "last_closed_at": last_ts,
                "last_closed_at_text": iso_ts(last_ts),
                "cooldown_until": cooldown_until,
                "cooldown_until_text": iso_ts(cooldown_until),
                "remain_min": round((cooldown_until - now_ts) / 60.0, 1),
            }
    return block


_v065_base_pick_candidates = pick_candidates


def pick_candidates(control: Dict[str, Any], open_pos: Dict[str, Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:  # type: ignore[override]
    picked, stats = _v065_base_pick_candidates(control, open_pos)
    block_map = _v065_recent_loss_block_map()
    if not picked or not block_map:
        if isinstance(stats, dict):
            stats.setdefault("recent_loss_cooldown_skip", 0)
            stats.setdefault("recent_loss_cooldown_tickers", [])
        return picked, stats

    kept: List[Tuple[str, Dict[str, Any]]] = []
    skipped: List[str] = []
    skipped_meta: List[Dict[str, Any]] = []
    for eid, ev in picked:
        t = short_ticker((ev or {}).get("ticker") or (ev or {}).get("market") or (ev or {}).get("symbol"))
        meta = block_map.get(t)
        if meta and _v052_is_focus_strategy(ev):
            skipped.append(t)
            skipped_meta.append(meta)
            continue
        kept.append((eid, ev))
    if isinstance(stats, dict):
        stats["recent_loss_cooldown_skip"] = int(stats.get("recent_loss_cooldown_skip", 0)) + len(skipped)
        stats["recent_loss_cooldown_tickers"] = skipped[:12]
        stats["recent_loss_cooldown_meta"] = skipped_meta[:8]
    return kept, stats


_v065_base_summary_text = summary_text


def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v064_focus_open_positions(open_pos)
    focus_closed = _v064_focus_closed_rows()
    fs = file_stats()
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    st = _v064_stats(focus_closed)
    repeat = int(float_any(pick.get("recent_loss_cooldown_skip"), default=0))
    repeat_ticks = pick.get("recent_loss_cooldown_tickers") if isinstance(pick.get("recent_loss_cooldown_tickers"), list) else []
    repeat_line = f" / 반복손실차단 {repeat}" + (f" ({', '.join(map(str, repeat_ticks[:5]))})" if repeat_ticks else "")
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        f"- 신규 OPEN: {'중지' if control.get('new_open_paused') else '허용'} / 단, {PAPER_FOCUS_STRATEGY_KEY} trade_ready만",
        f"- 반복손실 보호: 직전 손실 {int(PAPER_SINGLE_LOSS_COOLDOWN_SEC//60)}분 / {int(PAPER_REPEAT_LOSS_LOOKBACK_SEC//3600)}시간 내 {PAPER_REPEAT_LOSS_THRESHOLD}손실 시 {int(PAPER_REPEAT_LOSS_COOLDOWN_SEC//3600)}시간 차단",
        f"- OPEN: 현재전략 {len(focus_open)} / 전체 {len(open_pos)} / 한도 {control.get('max_open_strict')}",
        f"- CLOSED: 현재전략 {st['n']}/30 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        f"- 청산고정: 익절 +{float_any(control.get('take_profit_pct'), default=1.2):.2f}% / 보호 +{float_any(control.get('protect_trigger_pct'), default=0.7):.2f}%→+{float_any(control.get('protect_floor_pct'), default=0.3):.2f}% / 손절 {float_any(control.get('stop_loss_pct'), default=-0.55):.2f}% / 시간 {float_any(control.get('time_exit_minutes'), default=15):.0f}분",
        "",
        "[후보]",
        f"- 읽음 {pick.get('paper_file',0)} / 신규중지 {pick.get('new_open_paused',0)} / not_ready {pick.get('not_trade_ready_skip',0)} / micro대기 {pick.get('micro_preopen_wait',0)} / 중복 {pick.get('same_ticker_skip',0)}" + repeat_line,
        "",
        "[원칙]",
        "- CLOSED 30건 전 조건 변경 금지 / 오류·장부·정보수집 배관만 수정",
        f"- 오류로그 {fs.get('error', {}).get('size', 0)} bytes / 자세히 /perror",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# v0.66: 반복손실 완전차단 → 재진입 문턱상승 + 강한신호 예외 + 차단후보 기록
# 목적:
# - v0.65의 "직전 손실이면 60분 무조건 차단"은 좋은 2차 타이밍까지 막을 수 있다.
# - 1회 손실 코인은 기본 보류하되, 이전 진입보다 명확히 더 좋은 재진입 근거가 있을 때만 예외 허용한다.
# - 6시간 2회 이상 손실은 강한 차단 유지. 단, 차단 후보는 blocked_watch에 남겨 사후 복기한다.
# - 전략 조건/청산값/장부는 변경하지 않는다.
# ─────────────────────────────────────────────────────────────────────────────
try:
    VERSION = "paper_bot_v0.66"
except Exception:
    pass

FILES.setdefault("blocked_reentry_watch", BASE_DIR / "paper_bot_blocked_reentry_watch.jsonl")
FILES.setdefault("reentry_override_watch", BASE_DIR / "paper_bot_reentry_override_watch.jsonl")

PAPER_REENTRY_LOWER_PRICE_PCT = float(os.getenv("PAPER_REENTRY_LOWER_PRICE_PCT", "0.50"))
PAPER_REENTRY_MONEY_MULT = float(os.getenv("PAPER_REENTRY_MONEY_MULT", "1.25"))
PAPER_REENTRY_MIN_MONEY3_KRW = float(os.getenv("PAPER_REENTRY_MIN_MONEY3_KRW", "20000000"))
PAPER_REENTRY_BUY_RATIO_MIN = float(os.getenv("PAPER_REENTRY_BUY_RATIO_MIN", "0.60"))
PAPER_REENTRY_BUY_RATIO_IMPROVE = float(os.getenv("PAPER_REENTRY_BUY_RATIO_IMPROVE", "0.05"))
PAPER_REENTRY_REBREAK_MIN = float(os.getenv("PAPER_REENTRY_REBREAK_MIN", "0.80"))
PAPER_REENTRY_REBREAK_IMPROVE = float(os.getenv("PAPER_REENTRY_REBREAK_IMPROVE", "0.25"))
PAPER_REENTRY_SPREAD_MAX = float(os.getenv("PAPER_REENTRY_SPREAD_MAX", "0.25"))


def _v066_any_field(row: Dict[str, Any], *keys: str) -> Any:
    """entry/event/closed row에서 같은 의미의 값을 단일 방식으로 읽는다."""
    row = row if isinstance(row, dict) else {}
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    for k in keys:
        for src in (row, ctx, raw):
            if isinstance(src, dict) and src.get(k) not in (None, ""):
                return src.get(k)
    return ""


def _v066_metric(row: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return float_any(_v066_any_field(row, *keys), default=default)


def _v066_entry_price(row: Dict[str, Any]) -> float:
    return float_any(
        _v066_any_field(row, "entry_price", "detected_price", "current_price", "price", "trade_price", "close"),
        default=0.0,
    )


def _v066_money3(row: Dict[str, Any]) -> float:
    return _v066_metric(
        row,
        "money_flow_3m", "turnover_3m", "turnover_3m_krw", "trade_krw_3m", "money3", "money_flow",
        default=0.0,
    )


def _v066_buy_ratio(row: Dict[str, Any]) -> float:
    return _v066_metric(row, "micro_trade_buy_ratio_30", "trade_buy_ratio_30", "buy_ratio", default=0.0)


def _v066_rebreak(row: Dict[str, Any]) -> float:
    return _v066_metric(row, "rebreakout_strength", "rebreakout", "rebreak_strength", default=0.0)


def _v066_spread(row: Dict[str, Any]) -> float:
    return _v066_metric(row, "micro_spread_pct", "orderbook_spread_pct", "spread_pct", default=999.0)


def _v066_recent_loss_gate_map(now_ts: Optional[float] = None, limit: int = 6000) -> Dict[str, Dict[str, Any]]:
    """최근 CLOSED 기준 재진입 문턱상승/강한차단 표를 만든다.

    gate 종류:
    - hard_block: 최근 6시간 내 같은 코인 손실 2회 이상. 예외 없이 OPEN 차단.
    - elevated_gate: 직전 CLOSED가 손실. 강한 재진입 조건을 통과할 때만 예외 허용.
    """
    now_ts = float(now_ts or now())
    rows = read_jsonl(FILES["closed"], max_lines=limit)
    by_ticker: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    horizon = max(PAPER_REPEAT_LOSS_LOOKBACK_SEC, PAPER_REPEAT_LOSS_COOLDOWN_SEC, PAPER_SINGLE_LOSS_COOLDOWN_SEC) + 300
    for r in rows:
        if not isinstance(r, dict) or not _v052_is_focus_strategy(r):
            continue
        t = short_ticker(r.get("ticker") or r.get("market") or r.get("symbol"))
        if not t:
            continue
        cts = _v065_closed_time(r)
        if cts <= 0 or now_ts - cts > horizon:
            continue
        by_ticker[t].append(r)

    gate: Dict[str, Dict[str, Any]] = {}
    for t, arr in by_ticker.items():
        arr.sort(key=_v065_closed_time, reverse=True)
        last = arr[0]
        last_pnl = _v065_closed_pnl(last)
        last_ts = _v065_closed_time(last)
        recent_losses = [r for r in arr if now_ts - _v065_closed_time(r) <= PAPER_REPEAT_LOSS_LOOKBACK_SEC and _v065_closed_pnl(r) < 0]
        if len(recent_losses) >= max(1, PAPER_REPEAT_LOSS_THRESHOLD):
            latest_loss_ts = max(_v065_closed_time(r) for r in recent_losses)
            until = latest_loss_ts + PAPER_REPEAT_LOSS_COOLDOWN_SEC
            if until > now_ts:
                gate[t] = {
                    "mode": "hard_block",
                    "ticker": t,
                    "reason": f"최근 {int(PAPER_REPEAT_LOSS_LOOKBACK_SEC//3600)}시간 손실 {len(recent_losses)}회",
                    "loss_count": len(recent_losses),
                    "last_loss": last,
                    "last_pnl": round(last_pnl, 4),
                    "cooldown_until": until,
                    "cooldown_until_text": iso_ts(until),
                    "remain_min": round((until - now_ts) / 60.0, 1),
                }
            continue
        if last_pnl < 0:
            until = last_ts + PAPER_SINGLE_LOSS_COOLDOWN_SEC
            if until > now_ts:
                gate[t] = {
                    "mode": "elevated_gate",
                    "ticker": t,
                    "reason": "직전 CLOSED 손실: 재진입 문턱상승",
                    "loss_count": len(recent_losses),
                    "last_loss": last,
                    "last_pnl": round(last_pnl, 4),
                    "cooldown_until": until,
                    "cooldown_until_text": iso_ts(until),
                    "remain_min": round((until - now_ts) / 60.0, 1),
                }
    return gate


def _v066_strong_reentry_ok(ev: Dict[str, Any], gate_meta: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """직전 손실 코인의 예외 재진입 허용 여부.

    예외 허용은 느슨한 조건 추가가 아니라, 같은 코인 재진입 시 이전 실패보다
    명확히 더 좋은 자리인지 확인하는 보호 문턱이다.
    """
    last = gate_meta.get("last_loss") if isinstance(gate_meta.get("last_loss"), dict) else {}
    cur_price = _v066_entry_price(ev)
    last_price = _v066_entry_price(last)
    cur_money = _v066_money3(ev)
    last_money = _v066_money3(last)
    cur_buy = _v066_buy_ratio(ev)
    last_buy = _v066_buy_ratio(last)
    cur_rebreak = _v066_rebreak(ev)
    last_rebreak = _v066_rebreak(last)
    cur_spread = _v066_spread(ev)

    checks: List[Tuple[str, bool, str]] = []
    lower_ok = bool(cur_price > 0 and last_price > 0 and cur_price <= last_price * (1.0 - PAPER_REENTRY_LOWER_PRICE_PCT / 100.0))
    checks.append(("더낮은회복", lower_ok, f"현재 {cur_price:g} / 직전 {last_price:g} / 기준 -{PAPER_REENTRY_LOWER_PRICE_PCT:.2f}%"))

    if last_money > 0:
        money_need = max(PAPER_REENTRY_MIN_MONEY3_KRW, last_money * PAPER_REENTRY_MONEY_MULT)
    else:
        money_need = PAPER_REENTRY_MIN_MONEY3_KRW
    money_ok = bool(cur_money >= money_need)
    checks.append(("3분돈흐름강화", money_ok, f"현재 {cur_money/10000:.0f}만 / 기준 {money_need/10000:.0f}만"))

    buy_need = max(PAPER_REENTRY_BUY_RATIO_MIN, (last_buy + PAPER_REENTRY_BUY_RATIO_IMPROVE) if last_buy > 0 else PAPER_REENTRY_BUY_RATIO_MIN)
    buy_ok = bool(cur_buy >= buy_need)
    checks.append(("매수체결강화", buy_ok, f"현재 {cur_buy:.2f} / 기준 {buy_need:.2f}"))

    rebreak_need = max(PAPER_REENTRY_REBREAK_MIN, (last_rebreak + PAPER_REENTRY_REBREAK_IMPROVE) if last_rebreak > 0 else PAPER_REENTRY_REBREAK_MIN)
    rebreak_ok = bool(cur_rebreak >= rebreak_need)
    checks.append(("재돌파강화", rebreak_ok, f"현재 {cur_rebreak:.2f} / 기준 {rebreak_need:.2f}"))

    spread_ok = bool(cur_spread <= PAPER_REENTRY_SPREAD_MAX)
    checks.append(("스프레드양호", spread_ok, f"현재 {cur_spread:.2f}% / 기준 {PAPER_REENTRY_SPREAD_MAX:.2f}%"))

    reasons = [f"{'✅' if ok else '❌'} {name}: {txt}" for name, ok, txt in checks]
    return all(ok for _, ok, _ in checks), reasons


def _v066_watch_row(ev: Dict[str, Any], meta: Dict[str, Any], action: str, reasons: List[str]) -> Dict[str, Any]:
    t = short_ticker((ev or {}).get("ticker") or (ev or {}).get("market") or (ev or {}).get("symbol"))
    return {
        "version": VERSION,
        "ts": now(),
        "time": iso_ts(),
        "action": action,
        "ticker": t,
        "strategy_key": PAPER_FOCUS_STRATEGY_KEY,
        "event_id": event_id(ev, str((ev or {}).get("lane") or "strict")),
        "gate_mode": meta.get("mode"),
        "gate_reason": meta.get("reason"),
        "remain_min": meta.get("remain_min"),
        "last_pnl": meta.get("last_pnl"),
        "current_price": _v066_entry_price(ev),
        "money3": _v066_money3(ev),
        "buy_ratio": _v066_buy_ratio(ev),
        "rebreakout_strength": _v066_rebreak(ev),
        "spread_pct": _v066_spread(ev),
        "reasons": reasons[:8],
        "note": "반복손실 재진입 보호: OPEN 차단/예외허용 후보 사후복기용 기록. 장부 삭제 없음.",
    }


# v0.65의 완전차단 pick_candidates를 우회하고, v0.64 기본 후보선정 뒤 v0.66 gate를 적용한다.
_v066_base_pick_candidates = _v065_base_pick_candidates if "_v065_base_pick_candidates" in globals() else pick_candidates


def pick_candidates(control: Dict[str, Any], open_pos: Dict[str, Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:  # type: ignore[override]
    picked, stats = _v066_base_pick_candidates(control, open_pos)
    gate_map = _v066_recent_loss_gate_map()
    if not picked or not gate_map:
        if isinstance(stats, dict):
            stats.setdefault("repeat_loss_hard_block", 0)
            stats.setdefault("repeat_loss_elevated_block", 0)
            stats.setdefault("repeat_loss_override_allow", 0)
            stats.setdefault("repeat_loss_block_tickers", [])
        return picked, stats

    kept: List[Tuple[str, Dict[str, Any]]] = []
    hard_blocked: List[str] = []
    elevated_blocked: List[str] = []
    override_allowed: List[str] = []
    meta_rows: List[Dict[str, Any]] = []
    for eid, ev in picked:
        t = short_ticker((ev or {}).get("ticker") or (ev or {}).get("market") or (ev or {}).get("symbol"))
        meta = gate_map.get(t)
        if not meta or not _v052_is_focus_strategy(ev):
            kept.append((eid, ev))
            continue
        mode = str(meta.get("mode") or "")
        if mode == "hard_block":
            reasons = [str(meta.get("reason") or "반복손실 강한차단")]
            hard_blocked.append(t)
            meta_rows.append({k: v for k, v in meta.items() if k != "last_loss"})
            append_jsonl(FILES["blocked_reentry_watch"], _v066_watch_row(ev, meta, "hard_block", reasons))
            continue
        ok, reasons = _v066_strong_reentry_ok(ev, meta)
        if ok:
            ev = dict(ev)
            ev["repeat_loss_reentry_override"] = True
            ev["repeat_loss_reentry_reasons"] = reasons[:8]
            override_allowed.append(t)
            append_jsonl(FILES["reentry_override_watch"], _v066_watch_row(ev, meta, "override_allow", reasons))
            kept.append((eid, ev))
        else:
            elevated_blocked.append(t)
            meta_rows.append({k: v for k, v in meta.items() if k != "last_loss"})
            append_jsonl(FILES["blocked_reentry_watch"], _v066_watch_row(ev, meta, "elevated_block", reasons))

    if isinstance(stats, dict):
        stats["repeat_loss_hard_block"] = int(stats.get("repeat_loss_hard_block", 0)) + len(hard_blocked)
        stats["repeat_loss_elevated_block"] = int(stats.get("repeat_loss_elevated_block", 0)) + len(elevated_blocked)
        stats["repeat_loss_override_allow"] = int(stats.get("repeat_loss_override_allow", 0)) + len(override_allowed)
        stats["repeat_loss_block_tickers"] = list(dict.fromkeys(hard_blocked + elevated_blocked))[:12]
        stats["repeat_loss_override_tickers"] = list(dict.fromkeys(override_allowed))[:12]
        stats["repeat_loss_gate_meta"] = meta_rows[:8]
    return kept, stats


_v066_base_summary_text = summary_text


def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v064_focus_open_positions(open_pos)
    focus_closed = _v064_focus_closed_rows()
    fs = file_stats()
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    st = _v064_stats(focus_closed)
    hard = int(float_any(pick.get("repeat_loss_hard_block"), default=0))
    elevated = int(float_any(pick.get("repeat_loss_elevated_block"), default=0))
    override = int(float_any(pick.get("repeat_loss_override_allow"), default=0))
    blocked_ticks = pick.get("repeat_loss_block_tickers") if isinstance(pick.get("repeat_loss_block_tickers"), list) else []
    override_ticks = pick.get("repeat_loss_override_tickers") if isinstance(pick.get("repeat_loss_override_tickers"), list) else []
    repeat_line = f" / 반복손실 문턱 {elevated} / 강한차단 {hard} / 예외허용 {override}"
    if blocked_ticks:
        repeat_line += f" (차단 {', '.join(map(str, blocked_ticks[:5]))})"
    if override_ticks:
        repeat_line += f" (허용 {', '.join(map(str, override_ticks[:5]))})"
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        f"- 신규 OPEN: {'중지' if control.get('new_open_paused') else '허용'} / 단, {PAPER_FOCUS_STRATEGY_KEY} trade_ready만",
        "- 반복손실 보호: 1회 손실=재진입 문턱상승, 강한 재진입 근거만 예외허용 / 6시간 2손실=강한차단",
        f"- OPEN: 현재전략 {len(focus_open)} / 전체 {len(open_pos)} / 한도 {control.get('max_open_strict')}",
        f"- CLOSED: 현재전략 {st['n']}/30 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        f"- 청산고정: 익절 +{float_any(control.get('take_profit_pct'), default=1.2):.2f}% / 보호 +{float_any(control.get('protect_trigger_pct'), default=0.7):.2f}%→+{float_any(control.get('protect_floor_pct'), default=0.3):.2f}% / 손절 {float_any(control.get('stop_loss_pct'), default=-0.55):.2f}% / 시간 {float_any(control.get('time_exit_minutes'), default=15):.0f}분",
        "",
        "[후보]",
        f"- 읽음 {pick.get('paper_file',0)} / 신규중지 {pick.get('new_open_paused',0)} / not_ready {pick.get('not_trade_ready_skip',0)} / micro대기 {pick.get('micro_preopen_wait',0)} / 중복 {pick.get('same_ticker_skip',0)}" + repeat_line,
        "",
        "[원칙]",
        "- CLOSED 30건 전 조건 변경 금지 / 반복손실 보호는 설계서의 기존 보호 연결",
        f"- 오류로그 {fs.get('error', {}).get('size', 0)} bytes / 자세히 /perror",
    ])



# =============================================================================
# v0.68: leader_momentum_continuation paper runner
# - sweep_vwap_recovery는 신규 OPEN 대상에서 제외
# - 새 전략은 +2.0 / -0.60 / 45분 / 보호 +1.0 → +0.45로 검증
# - 반복손실 문턱상승(v0.66)은 유지한다.
# =============================================================================
VERSION = "paper_bot_v0.68"
PAPER_FOCUS_STRATEGY_KEY = "leader_momentum_continuation"
PAPER_FOCUS_STRATEGY_LABEL = "장세 선택형 주도코인 추세 지속 전략"
PAPER_FOCUS_TAG = "주도추세"
PAPER_FOCUS_MAIN_MODE = "leader_momentum_continuation_only"

DEFAULT_CONTROL.update({
    "new_open_paused": False,
    "open_trade_ready_only": True,
    "open_shadow_positions": False,
    "shadow_review_only": True,
    "take_profit_pct": 2.00,
    "protect_trigger_pct": 1.00,
    "protect_floor_pct": 0.45,
    "stop_loss_pct": -0.60,
    "time_exit_minutes": 45,
    "quick_stop_enabled": False,
    "slow_early_enabled": False,
    "long_loss_guard_enabled": False,
    "notify_on_strict_open": True,
    "notify_on_strict_close": True,
    "notify_open_auto_ready_only": True,
    "notify_open_min_score": 4.45,
    "notify_open_require_final_pass": True,
    "notify_open_require_micro_fresh": True,
    "preopen_micro_recheck": True,
    "notify_close_only_alerted": False,
})

_v068_base_load_control = load_control

def load_control() -> Dict[str, Any]:  # type: ignore[override]
    control = _v068_base_load_control()
    fixed = {
        "new_open_paused": False,
        "open_trade_ready_only": True,
        "open_shadow_positions": False,
        "shadow_review_only": True,
        "take_profit_pct": 2.00,
        "protect_trigger_pct": 1.00,
        "protect_floor_pct": 0.45,
        "stop_loss_pct": -0.60,
        "time_exit_minutes": 45,
        "quick_stop_enabled": False,
        "slow_early_enabled": False,
        "long_loss_guard_enabled": False,
        "notify_close_only_alerted": False,
        "active_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
        "active_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
    }
    changed = False
    for k, v in fixed.items():
        if control.get(k) != v:
            control[k] = v
            changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
            set_pause_flags(bool(control.get("running")))
        except Exception:
            pass
    return control

# v0.66의 반복손실 보호는 _v052_is_focus_strategy가 전역 PAPER_FOCUS_*를 보므로 새 전략에 자동 적용된다.
_v068_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v064_focus_open_positions(open_pos)
    focus_closed = _v064_focus_closed_rows()
    fs = file_stats()
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    st = _v064_stats(focus_closed)
    hard = int(float_any(pick.get("repeat_loss_hard_block"), default=0))
    elevated = int(float_any(pick.get("repeat_loss_elevated_block"), default=0))
    override = int(float_any(pick.get("repeat_loss_override_allow"), default=0))
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        f"- 신규 OPEN: {'중지' if control.get('new_open_paused') else '허용'} / 단, {PAPER_FOCUS_STRATEGY_KEY} trade_ready만",
        "- 반복손실 보호: 1회 손실=재진입 문턱상승, 강한 재진입 근거만 예외허용 / 6시간 2손실=강한차단",
        f"- OPEN: 현재전략 {len(focus_open)} / 전체 {len(open_pos)} / 한도 {control.get('max_open_strict')}",
        f"- CLOSED: 현재전략 {st['n']}/30 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        f"- 청산고정: 익절 +{float_any(control.get('take_profit_pct'), default=2.0):.2f}% / 보호 +{float_any(control.get('protect_trigger_pct'), default=1.0):.2f}%→+{float_any(control.get('protect_floor_pct'), default=0.45):.2f}% / 손절 {float_any(control.get('stop_loss_pct'), default=-0.60):.2f}% / 시간 {float_any(control.get('time_exit_minutes'), default=45):.0f}분",
        "",
        "[후보]",
        f"- 읽음 {pick.get('paper_file',0)} / 신규중지 {pick.get('new_open_paused',0)} / not_ready {pick.get('not_trade_ready_skip',0)} / micro대기 {pick.get('micro_preopen_wait',0)} / 중복 {pick.get('same_ticker_skip',0)} / 반복손실 문턱 {elevated} / 강한차단 {hard} / 예외허용 {override}",
        "",
        "[원칙]",
        "- CLOSED 30건 전 조건 변경 금지 / 자동매수 OFF / paper 검증 전용",
        f"- 오류로그 {fs.get('error', {}).get('size', 0)} bytes / 자세히 /perror",
    ])


# =============================================================================
# v0.69: leader_momentum S/A/B/C grade runner
# - S급: 미래 자동매매 후보급이지만 자동매수 OFF, paper OPEN 허용
# - A급: 모의매매 검증 후보, paper OPEN 허용
# - B급: 관찰/복기 후보, 기본 OPEN 안 함
# - C급: 진입 금지
# - 5~10분 초단타가 아니라 1시간 관찰형 청산 구조로 검증
# =============================================================================
VERSION = "paper_bot_v0.69"
PAPER_FOCUS_STRATEGY_KEY = "leader_momentum_continuation"
PAPER_FOCUS_STRATEGY_LABEL = "장세 선택형 주도코인 추세 지속 전략"
PAPER_FOCUS_TAG = "주도추세등급"
PAPER_FOCUS_MAIN_MODE = "leader_momentum_grade_only"

DEFAULT_CONTROL.update({
    "new_open_paused": False,
    "open_trade_ready_only": True,
    "open_shadow_positions": False,
    "shadow_review_only": True,
    "take_profit_pct": 1.20,
    "extended_target_pct": 2.00,
    "protect_trigger_pct": 0.70,
    "protect_floor_pct": 0.35,
    "stop_loss_pct": -0.60,
    "time_exit_minutes": 60,
    "slow_minutes": 60,
    "quick_stop_enabled": False,
    "slow_early_enabled": False,
    "long_loss_guard_enabled": False,
    "notify_on_strict_open": True,
    "notify_on_strict_close": True,
    "notify_open_auto_ready_only": False,
    "notify_open_min_score": 0.0,
    "notify_open_require_final_pass": True,
    "notify_open_require_micro_fresh": True,
    "preopen_micro_recheck": True,
    "notify_close_only_alerted": False,
    "paper_open_grades": ["S", "A"],
})

_v069_base_load_control = load_control

def load_control() -> Dict[str, Any]:  # type: ignore[override]
    control = _v069_base_load_control()
    fixed = {
        "new_open_paused": False,
        "open_trade_ready_only": True,
        "open_shadow_positions": False,
        "shadow_review_only": True,
        "take_profit_pct": 1.20,
        "extended_target_pct": 2.00,
        "protect_trigger_pct": 0.70,
        "protect_floor_pct": 0.35,
        "stop_loss_pct": -0.60,
        "time_exit_minutes": 60,
        "slow_minutes": 60,
        "quick_stop_enabled": False,
        "slow_early_enabled": False,
        "long_loss_guard_enabled": False,
        "notify_close_only_alerted": False,
        "notify_open_auto_ready_only": False,
        "active_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
        "active_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
        "paper_open_grades": ["S", "A"],
    }
    changed = False
    for k, v in fixed.items():
        if control.get(k) != v:
            control[k] = v
            changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
            set_pause_flags(bool(control.get("running")))
        except Exception:
            pass
    return control


def _v069_grade_from_event(ev: Dict[str, Any]) -> str:
    ctx = ev.get("entry_context") if isinstance(ev.get("entry_context"), dict) else {}
    g = str(ev.get("candidate_grade") or ctx.get("candidate_grade") or "").upper().strip()
    if g in {"S", "A", "B", "C"}:
        return g
    label = str(ev.get("candidate_grade_label") or ctx.get("candidate_grade_label") or ev.get("final_entry_label") or "")
    if "S급" in label:
        return "S"
    if "A급" in label:
        return "A"
    if "B급" in label:
        return "B"
    return "C"

_v069_base_pick_candidates = pick_candidates

def pick_candidates(control: Dict[str, Any], open_pos: Dict[str, Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:  # type: ignore[override]
    picked, stats = _v069_base_pick_candidates(control, open_pos)
    allow = set(control.get("paper_open_grades") or ["S", "A"])
    out: List[Tuple[str, Dict[str, Any]]] = []
    stats = dict(stats or {})
    stats.setdefault("grade_s_seen", 0)
    stats.setdefault("grade_a_seen", 0)
    stats.setdefault("grade_b_observe_skip", 0)
    stats.setdefault("grade_c_skip", 0)
    stats.setdefault("grade_missing_skip", 0)
    for pid, ev in picked:
        g = _v069_grade_from_event(ev)
        if g == "S": stats["grade_s_seen"] += 1
        elif g == "A": stats["grade_a_seen"] += 1
        elif g == "B": stats["grade_b_observe_skip"] += 1
        elif g == "C": stats["grade_c_skip"] += 1
        else: stats["grade_missing_skip"] += 1
        if g in allow:
            ev = dict(ev)
            ev["candidate_grade"] = g
            ev["paper_grade_open"] = True
            out.append((pid, ev))
        else:
            continue
    stats["grade_filtered_out"] = max(0, len(picked) - len(out))
    return out, stats

_v069_base_open_position = open_position

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    pos = _v069_base_open_position(pos_id, ev, control)
    ctx = pos.get("entry_context") if isinstance(pos.get("entry_context"), dict) else {}
    g = _v069_grade_from_event(ev)
    pos["candidate_grade"] = g
    pos["candidate_grade_label"] = ev.get("candidate_grade_label") or ctx.get("candidate_grade_label") or ("✅ S급 자동매매 후보급" if g == "S" else "🟡 A급 모의매매 후보" if g == "A" else "❔ B급 관찰" if g == "B" else "❌ C급 금지")
    pos["auto_ready"] = bool(ev.get("auto_ready") or ctx.get("auto_ready") or g == "S")
    pos["take_profit_pct"] = float_any(ev.get("take_profit_pct"), ctx.get("take_profit_pct"), control.get("take_profit_pct"), default=1.20)
    pos["extended_target_pct"] = float_any(ev.get("extended_target_pct"), ctx.get("extended_target_pct"), control.get("extended_target_pct"), default=2.00)
    pos["protect_trigger_pct"] = float_any(ev.get("protect_trigger_pct"), ctx.get("protect_trigger_pct"), control.get("protect_trigger_pct"), default=0.70)
    pos["protect_floor_pct"] = float_any(ev.get("protect_floor_pct"), ctx.get("protect_floor_pct"), control.get("protect_floor_pct"), default=0.35)
    pos["stop_loss_pct"] = float_any(ev.get("stop_loss_pct"), ctx.get("stop_loss_pct"), control.get("stop_loss_pct"), default=-0.60)
    pos["time_exit_min"] = float_any(ev.get("time_exit_min"), ctx.get("time_exit_min"), control.get("time_exit_minutes"), default=60)
    ctx = dict(ctx)
    ctx.update({
        "candidate_grade": pos["candidate_grade"],
        "candidate_grade_label": pos["candidate_grade_label"],
        "auto_ready": pos["auto_ready"],
        "take_profit_pct": pos["take_profit_pct"],
        "extended_target_pct": pos["extended_target_pct"],
        "protect_trigger_pct": pos["protect_trigger_pct"],
        "protect_floor_pct": pos["protect_floor_pct"],
        "stop_loss_pct": pos["stop_loss_pct"],
        "time_exit_min": pos["time_exit_min"],
    })
    pos["entry_context"] = ctx
    return pos

_v069_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    local = dict(control or {})
    local["take_profit_pct"] = float_any(pos.get("take_profit_pct"), default=float_any(control.get("take_profit_pct"), default=1.20))
    local["protect_trigger_pct"] = float_any(pos.get("protect_trigger_pct"), default=float_any(control.get("protect_trigger_pct"), default=0.70))
    local["protect_floor_pct"] = float_any(pos.get("protect_floor_pct"), default=float_any(control.get("protect_floor_pct"), default=0.35))
    local["stop_loss_pct"] = float_any(pos.get("stop_loss_pct"), default=float_any(control.get("stop_loss_pct"), default=-0.60))
    local["time_exit_minutes"] = float_any(pos.get("time_exit_min"), default=float_any(control.get("time_exit_minutes"), default=60))
    updated, closed = _v069_base_update_position(pos, local)
    if closed:
        closed["candidate_grade"] = updated.get("candidate_grade") or pos.get("candidate_grade")
        closed["candidate_grade_label"] = updated.get("candidate_grade_label") or pos.get("candidate_grade_label")
        closed["auto_ready"] = bool(updated.get("auto_ready") or pos.get("auto_ready"))
        closed["take_profit_pct"] = local.get("take_profit_pct")
        closed["extended_target_pct"] = float_any(updated.get("extended_target_pct"), pos.get("extended_target_pct"), local.get("extended_target_pct"), default=2.0)
    return updated, closed

_v069_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v064_focus_open_positions(open_pos)
    focus_closed = _v064_focus_closed_rows()
    fs = file_stats()
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    st = _v064_stats(focus_closed)
    grade_open = {"S": 0, "A": 0, "B": 0, "C": 0}
    for p in focus_open.values():
        g = str((p or {}).get("candidate_grade") or "C")
        if g in grade_open:
            grade_open[g] += 1
    hard = int(float_any(pick.get("repeat_loss_hard_block"), default=0))
    elevated = int(float_any(pick.get("repeat_loss_elevated_block"), default=0))
    override = int(float_any(pick.get("repeat_loss_override_allow"), default=0))
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        "- 등급: S=미래 자동매매 후보급 / A=모의검증 / B=관찰 / C=금지",
        f"- 신규 OPEN: {'중지' if control.get('new_open_paused') else '허용'} / S·A급 trade_ready만",
        "- 자동매수: OFF / S급도 실제 주문 아님",
        f"- OPEN: S {grade_open['S']} / A {grade_open['A']} / 전체전략 {len(focus_open)} / 전체 {len(open_pos)} / 한도 {control.get('max_open_strict')}",
        f"- CLOSED: 현재전략 {st['n']}/30 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        f"- 청산고정: 기본익절 +{float_any(control.get('take_profit_pct'), default=1.2):.2f}% / 강한흐름 관찰 +{float_any(control.get('extended_target_pct'), default=2.0):.2f}% / 보호 +{float_any(control.get('protect_trigger_pct'), default=0.7):.2f}%→+{float_any(control.get('protect_floor_pct'), default=0.35):.2f}% / 손절 {float_any(control.get('stop_loss_pct'), default=-0.60):.2f}% / 시간 {float_any(control.get('time_exit_minutes'), default=60):.0f}분",
        "",
        "[후보]",
        f"- 읽음 {pick.get('paper_file',0)} / S {pick.get('grade_s_seen',0)} / A {pick.get('grade_a_seen',0)} / B관찰스킵 {pick.get('grade_b_observe_skip',0)} / C스킵 {pick.get('grade_c_skip',0)} / 등급필터 {pick.get('grade_filtered_out',0)} / micro대기 {pick.get('micro_preopen_wait',0)} / 중복 {pick.get('same_ticker_skip',0)} / 반복손실 문턱 {elevated} / 강한차단 {hard} / 예외허용 {override}",
        "",
        "[원칙]",
        "- CLOSED 30건 전 조건 변경 금지 / 실제 자동매매는 S급만 후보 / 지금은 paper 검증 전용",
        f"- 오류로그 {fs.get('error', {}).get('size', 0)} bytes / 자세히 /perror",
    ])



# =============================================================================
# v0.70: updated S/A/B/C semantics + B paper validation
# - S = 자동매매 상위 후보, A = 자동매매 일반 후보
# - B = 모의매매 검증 후보, C = 관찰 후보, X/제외 = 차단
# - 현재 자동매수는 OFF. paper OPEN은 S/A/B만 허용한다.
# =============================================================================
VERSION = "paper_bot_v0.70"
PAPER_FOCUS_STRATEGY_KEY = "leader_momentum_continuation"
PAPER_FOCUS_STRATEGY_LABEL = "장세 선택형 주도코인 추세 지속 전략"
PAPER_FOCUS_TAG = "주도추세등급"
PAPER_FOCUS_MAIN_MODE = "leader_momentum_grade_mode"

DEFAULT_CONTROL.update({
    "new_open_paused": False,
    "open_trade_ready_only": True,
    "open_shadow_positions": False,
    "shadow_review_only": True,
    "take_profit_pct": 1.20,
    "extended_target_pct": 2.00,
    "protect_trigger_pct": 0.70,
    "protect_floor_pct": 0.35,
    "stop_loss_pct": -0.60,
    "time_exit_minutes": 60,
    "slow_minutes": 60,
    "notify_open_auto_ready_only": False,
    "notify_open_require_final_pass": True,
    "notify_open_require_micro_fresh": True,
    "preopen_micro_recheck": True,
    "paper_open_grades": ["S", "A", "B"],
})

_v070_base_load_control = load_control

def load_control() -> Dict[str, Any]:  # type: ignore[override]
    control = _v070_base_load_control()
    fixed = {
        "new_open_paused": False,
        "open_trade_ready_only": True,
        "open_shadow_positions": False,
        "shadow_review_only": True,
        "notify_close_only_alerted": False,
        "notify_open_auto_ready_only": False,
        "active_strategy_key": PAPER_FOCUS_STRATEGY_KEY,
        "active_strategy_label": PAPER_FOCUS_STRATEGY_LABEL,
        "paper_open_grades": ["S", "A", "B"],
    }
    changed = False
    for k, v in fixed.items():
        if control.get(k) != v:
            control[k] = v
            changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
            set_pause_flags(bool(control.get("running")))
        except Exception:
            pass
    return control


def _v070_grade_from_event(ev: Dict[str, Any]) -> str:
    ctx = ev.get("entry_context") if isinstance(ev.get("entry_context"), dict) else {}
    g = str(ev.get("candidate_grade") or ctx.get("candidate_grade") or "").upper().strip()
    if g in {"S", "A", "B", "C", "X"}:
        return g
    label = str(ev.get("candidate_grade_label") or ctx.get("candidate_grade_label") or ev.get("final_entry_label") or "")
    if "S급" in label:
        return "S"
    if "A급" in label:
        return "A"
    if "B급" in label:
        return "B"
    if "C급" in label:
        return "C"
    return "X" if "제외" in label or "차단" in label or "금지" in label else "C"

_v070_base_pick_candidates = pick_candidates

def pick_candidates(control: Dict[str, Any], open_pos: Dict[str, Dict[str, Any]]) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:  # type: ignore[override]
    picked, stats = _v070_base_pick_candidates(control, open_pos)
    allow = set(control.get("paper_open_grades") or ["S", "A", "B"])
    out: List[Tuple[str, Dict[str, Any]]] = []
    stats = dict(stats or {})
    for k in ("grade_s_seen", "grade_a_seen", "grade_b_seen", "grade_c_observe_skip", "grade_x_block_skip", "grade_missing_skip"):
        stats.setdefault(k, 0)
    for pid, ev in picked:
        g = _v070_grade_from_event(ev)
        if g == "S": stats["grade_s_seen"] += 1
        elif g == "A": stats["grade_a_seen"] += 1
        elif g == "B": stats["grade_b_seen"] += 1
        elif g == "C": stats["grade_c_observe_skip"] += 1
        elif g == "X": stats["grade_x_block_skip"] += 1
        else: stats["grade_missing_skip"] += 1
        if g in allow:
            ev = dict(ev)
            ev["candidate_grade"] = g
            ev["paper_grade_open"] = True
            out.append((pid, ev))
    stats["grade_filtered_out"] = max(0, len(picked) - len(out))
    return out, stats

_v070_base_open_position = open_position

def open_position(pos_id: str, ev: Dict[str, Any], control: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    pos = _v070_base_open_position(pos_id, ev, control)
    ctx = pos.get("entry_context") if isinstance(pos.get("entry_context"), dict) else {}
    g = _v070_grade_from_event(ev)
    label_map = {
        "S": "✅ S급 자동매매 상위 후보",
        "A": "✅ A급 자동매매 일반 후보",
        "B": "🟡 B급 모의매매 검증 후보",
        "C": "❔ C급 관찰 후보",
        "X": "❌ 제외/차단",
    }
    pos["candidate_grade"] = g
    pos["candidate_grade_label"] = ev.get("candidate_grade_label") or ctx.get("candidate_grade_label") or label_map.get(g, "❔ 관찰")
    pos["leader_market_mode"] = ev.get("leader_market_mode") or ctx.get("leader_market_mode")
    pos["auto_ready"] = bool(ev.get("auto_ready") or ctx.get("auto_ready") or g in {"S", "A"})
    pos["take_profit_pct"] = float_any(ev.get("take_profit_pct"), ctx.get("take_profit_pct"), control.get("take_profit_pct"), default=1.20)
    pos["extended_target_pct"] = float_any(ev.get("extended_target_pct"), ctx.get("extended_target_pct"), control.get("extended_target_pct"), default=2.00)
    pos["protect_trigger_pct"] = float_any(ev.get("protect_trigger_pct"), ctx.get("protect_trigger_pct"), control.get("protect_trigger_pct"), default=0.70)
    pos["protect_floor_pct"] = float_any(ev.get("protect_floor_pct"), ctx.get("protect_floor_pct"), control.get("protect_floor_pct"), default=0.35)
    pos["stop_loss_pct"] = float_any(ev.get("stop_loss_pct"), ctx.get("stop_loss_pct"), control.get("stop_loss_pct"), default=-0.60)
    pos["time_exit_min"] = float_any(ev.get("time_exit_min"), ctx.get("time_exit_min"), control.get("time_exit_minutes"), default=60)
    ctx = dict(ctx)
    ctx.update({
        "candidate_grade": pos["candidate_grade"],
        "candidate_grade_label": pos["candidate_grade_label"],
        "leader_market_mode": pos.get("leader_market_mode"),
        "auto_ready": pos["auto_ready"],
        "take_profit_pct": pos["take_profit_pct"],
        "extended_target_pct": pos["extended_target_pct"],
        "protect_trigger_pct": pos["protect_trigger_pct"],
        "protect_floor_pct": pos["protect_floor_pct"],
        "stop_loss_pct": pos["stop_loss_pct"],
        "time_exit_min": pos["time_exit_min"],
    })
    pos["entry_context"] = ctx
    return pos

_v070_base_update_position = update_position

def update_position(pos: Dict[str, Any], control: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:  # type: ignore[override]
    local = dict(control or {})
    local["take_profit_pct"] = float_any(pos.get("take_profit_pct"), default=float_any(control.get("take_profit_pct"), default=1.20))
    local["protect_trigger_pct"] = float_any(pos.get("protect_trigger_pct"), default=float_any(control.get("protect_trigger_pct"), default=0.70))
    local["protect_floor_pct"] = float_any(pos.get("protect_floor_pct"), default=float_any(control.get("protect_floor_pct"), default=0.35))
    local["stop_loss_pct"] = float_any(pos.get("stop_loss_pct"), default=float_any(control.get("stop_loss_pct"), default=-0.60))
    local["time_exit_minutes"] = float_any(pos.get("time_exit_min"), default=float_any(control.get("time_exit_minutes"), default=60))
    updated, closed = _v070_base_update_position(pos, local)
    if closed:
        closed["candidate_grade"] = updated.get("candidate_grade") or pos.get("candidate_grade")
        closed["candidate_grade_label"] = updated.get("candidate_grade_label") or pos.get("candidate_grade_label")
        closed["leader_market_mode"] = updated.get("leader_market_mode") or pos.get("leader_market_mode")
        closed["auto_ready"] = bool(updated.get("auto_ready") or pos.get("auto_ready"))
        closed["take_profit_pct"] = local.get("take_profit_pct")
        closed["extended_target_pct"] = float_any(updated.get("extended_target_pct"), pos.get("extended_target_pct"), local.get("extended_target_pct"), default=2.0)
    return updated, closed

_v070_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    focus_open = _v064_focus_open_positions(open_pos)
    focus_closed = _v064_focus_closed_rows()
    fs = file_stats()
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    st = _v064_stats(focus_closed)
    grade_open = {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0}
    for p in focus_open.values():
        g = str((p or {}).get("candidate_grade") or "X")
        if g in grade_open:
            grade_open[g] += 1
    hard = int(float_any(pick.get("repeat_loss_hard_block"), default=0))
    elevated = int(float_any(pick.get("repeat_loss_elevated_block"), default=0))
    override = int(float_any(pick.get("repeat_loss_override_allow"), default=0))
    return "\n".join([
        f"🧪 페이퍼봇 {VERSION}",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        "- 등급: S=자동매매 상위 / A=자동매매 일반 / B=모의매매 / C=관찰 / 제외=차단",
        f"- 신규 OPEN: {'중지' if control.get('new_open_paused') else '허용'} / paper OPEN S·A·B / 실제 자동매매 후보 S·A만",
        "- 자동매수: OFF / 실제 주문 아님",
        f"- OPEN: S {grade_open['S']} / A {grade_open['A']} / B {grade_open['B']} / 전체전략 {len(focus_open)} / 전체 {len(open_pos)} / 한도 {control.get('max_open_strict')}",
        f"- CLOSED: 현재전략 {st['n']}/30 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        f"- 기본 청산: 후보별 등급/장세값 우선 / fallback 익절 +{float_any(control.get('take_profit_pct'), default=1.2):.2f}% / 보호 +{float_any(control.get('protect_trigger_pct'), default=0.7):.2f}%→+{float_any(control.get('protect_floor_pct'), default=0.35):.2f}% / 손절 {float_any(control.get('stop_loss_pct'), default=-0.60):.2f}% / 시간 {float_any(control.get('time_exit_minutes'), default=60):.0f}분",
        "",
        "[후보]",
        f"- 읽음 {pick.get('paper_file',0)} / S {pick.get('grade_s_seen',0)} / A {pick.get('grade_a_seen',0)} / B {pick.get('grade_b_seen',0)} / C관찰스킵 {pick.get('grade_c_observe_skip',0)} / 제외스킵 {pick.get('grade_x_block_skip',0)} / 등급필터 {pick.get('grade_filtered_out',0)} / micro대기 {pick.get('micro_preopen_wait',0)} / 중복 {pick.get('same_ticker_skip',0)} / 반복손실 문턱 {elevated} / 강한차단 {hard} / 예외허용 {override}",
        "",
        "[원칙]",
        "- CLOSED 30건 전 조건 변경 금지 / 실제 자동매매는 S·A만 후보 / B는 paper 검증 / C는 관찰",
        f"- 오류로그 {fs.get('error', {}).get('size', 0)} bytes / 자세히 /perror",
    ])



# =============================================================================
# v0.71: grade-level performance view
# - paper OPEN/청산 로직은 v0.70 그대로 유지한다.
# - /pscore와 /pstatus에 S/A/B/C/제외 등급별 CLOSED 성과를 추가한다.
# =============================================================================
VERSION = "paper_bot_v0.72"


def _v071_grade_from_row(row: Dict[str, Any]) -> str:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    g = str(row.get("candidate_grade") or ctx.get("candidate_grade") or "").upper().strip()
    if g in {"S", "A", "B", "C", "X"}:
        return g
    label = str(row.get("candidate_grade_label") or ctx.get("candidate_grade_label") or row.get("final_entry_label") or "")
    if "S급" in label:
        return "S"
    if "A급" in label:
        return "A"
    if "B급" in label:
        return "B"
    if "C급" in label:
        return "C"
    if "제외" in label or "차단" in label or "금지" in label:
        return "X"
    return "U"


def _v071_focus_closed() -> List[Dict[str, Any]]:
    try:
        return _v064_focus_closed_rows()
    except Exception:
        rows = read_jsonl(FILES["closed"], max_lines=30000)
        out = []
        for r in rows:
            ctx = r.get("entry_context") if isinstance(r.get("entry_context"), dict) else {}
            if str(r.get("paper_strategy_key") or r.get("strategy_key") or ctx.get("paper_strategy_key") or ctx.get("strategy_key") or "") == PAPER_FOCUS_STRATEGY_KEY:
                out.append(r)
        return out


def _v071_stats_line(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    st = score_stats(list(rows or []))
    n = int(st.get("n", 0) or 0)
    if n <= 0:
        return f"- {label}: 0전 / 아직 없음"
    vals = [float_any(r.get("net_pnl_pct"), r.get("pnl_pct"), r.get("profit_pct"), default=0.0) for r in rows or []]
    mx = max(vals) if vals else 0.0
    mn = min(vals) if vals else 0.0
    return f"- {label}: {n}전 {int(st['wins'])}승 {int(st['losses'])}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 최대 {mx:+.2f}% / 최소 {mn:+.2f}%"


def _v071_grade_groups(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    groups = {"S": [], "A": [], "B": [], "C": [], "X": [], "U": []}
    for r in rows or []:
        groups.setdefault(_v071_grade_from_row(r), []).append(r)
    return groups


def _v071_grade_score_lines(rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    rows = list(rows if rows is not None else _v071_focus_closed())
    g = _v071_grade_groups(rows)
    auto = g.get("S", []) + g.get("A", [])
    paper = auto + g.get("B", [])
    return [
        "[등급별 CLOSED 성과]",
        _v071_stats_line("✅ S급 자동매매 상위", g.get("S", [])),
        _v071_stats_line("✅ A급 자동매매 일반", g.get("A", [])),
        _v071_stats_line("🟡 B급 모의매매 검증", g.get("B", [])),
        _v071_stats_line("❔ C급 관찰", g.get("C", [])),
        _v071_stats_line("❌ 제외/차단", g.get("X", [])),
        "",
        "[후보군 합산]",
        _v071_stats_line("S/A 자동매매 후보군", auto),
        _v071_stats_line("S/A/B paper 검증 후보군", paper),
        _v071_stats_line("현재전략 전체", rows),
    ]

_v071_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    base = _v071_base_summary_text()
    try:
        rows = _v071_focus_closed()
        return base + "\n\n" + "\n".join(_v071_grade_score_lines(rows))
    except Exception as exc:
        log_error("v071_summary_grade_stats", exc)
        return base


def _v071_pscore_text() -> str:
    rows = _v071_focus_closed()
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in rows)
    lines = [
        "📊 페이퍼봇 등급 성과 /pscore",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        "- v0.71: S/A/B/C/제외 등급별 CLOSED 성과 표시",
        "- S/A=미래 자동매매 후보군, B=paper 검증, C=관찰",
        "",
        *_v071_grade_score_lines(rows),
        "",
        "[종료 사유 TOP]",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(6):
            sub = [r for r in rows if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_v071_stats_line(label_kr(k), sub))
    else:
        lines.append("- 아직 현재전략 CLOSED 없음")
    lines += [
        "",
        "판독",
        "- S/A가 플러스여야 실제 자동매매 후보로 볼 수 있음",
        "- B는 모의검증 성과만 보고 S/A 승격 여부를 나중에 판단",
        "- C는 관찰용이라 OPEN이 없으면 성과가 없어도 정상",
    ]
    return "\n".join(lines)

_v071_base_command_response = command_response

def command_response(text: str) -> str:  # type: ignore[override]
    cmd = (text or "").strip().split()[0].lower() if (text or "").strip().split() else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    if cmd == "/pscore":
        return _v071_pscore_text()
    return _v071_base_command_response(text)


# =============================================================================
# v0.72: count-unfixed paper validation + atomic save fix
# - paper OPEN 고정 8개 한도를 전략 조건처럼 쓰지 않는다. max_open_strict=0은 고정한도 없음.
# - S/A/B 후보를 paper로 검증하되, 실제 자동매매 후보는 여전히 S/A만 본다.
# - paper_bot_control.json 저장은 고유 tmp로 처리해 FileNotFoundError 원인을 제거한다.
# - 장부 OPEN/CLOSED/trade_log는 삭제하거나 초기화하지 않는다.
# =============================================================================
VERSION = "paper_bot_v0.72"
DEFAULT_CONTROL.update({
    "max_open_strict": 0,
    "max_new_per_cycle": 0,
    "paper_open_grades": ["S", "A", "B"],
})

_v072_base_load_control = load_control

def load_control() -> Dict[str, Any]:  # type: ignore[override]
    control = _v072_base_load_control()
    changed = False
    fixed = {
        "max_open_strict": 0,
        "max_new_per_cycle": 0,
        "paper_open_grades": ["S", "A", "B"],
        "open_trade_ready_only": True,
        "open_shadow_positions": False,
        "shadow_review_only": True,
        "new_open_paused": False,
    }
    for k, v in fixed.items():
        if control.get(k) != v:
            control[k] = v
            changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
            set_pause_flags(bool(control.get("running")))
        except Exception as exc:
            log_error("v072_load_control_save", exc)
    return control


def _v072_limit_text(control: Optional[Dict[str, Any]] = None) -> str:
    control = control or load_control()
    try:
        v = int(float_any(control.get("max_open_strict"), default=0))
    except Exception:
        v = 0
    return "고정없음" if v <= 0 else str(v)

_v072_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    text = _v072_base_summary_text()
    try:
        control = load_control()
        text = text.replace(f"한도 {control.get('max_open_strict')}", f"한도 {_v072_limit_text(control)}")
        text = text.replace("paper_bot_v0.71", VERSION)
        text = text.replace("v0.71: S/A/B/C/제외 등급별 CLOSED 성과 표시", "v0.72: 등급별 성과 표시 + paper OPEN 고정개수 해제 + 저장 tmp 오류수술")
        text = text.replace("- 신규 OPEN: 허용 / paper OPEN S·A·B / 실제 자동매매 후보 S·A만", "- 신규 OPEN: 허용 / paper OPEN S·A·B / 고정개수 없음 / 실제 자동매매 후보 S·A만")
    except Exception as exc:
        log_error("v072_summary_text", exc)
    return text

_v072_base_pscore_text = _v071_pscore_text

def _v071_pscore_text() -> str:  # type: ignore[override]
    text = _v072_base_pscore_text()
    text = text.replace("v0.71: S/A/B/C/제외 등급별 CLOSED 성과 표시", "v0.72: S/A/B/C/제외 성과 표시 + paper OPEN 고정개수 해제")
    text = text.replace("- B는 모의검증 성과만 보고 S/A 승격 여부를 나중에 판단", "- B는 모의검증 성과만 보고 S/A 승격 여부를 나중에 판단. 개수 고정 대신 후보 조건으로 자연스럽게 열린다")
    return text


# =============================================================================
# v0.73: /popen grade-visible display
# - 장부/청산/OPEN 판정은 v0.72 그대로 유지한다.
# - /popen 개별 OPEN 줄에 S/A/B/C/제외 등급, 실제 자동매매 후보 여부, 위험태그를 표시한다.
# - /pstatus 안에 포함되는 OPEN 시간표도 같은 표시를 쓴다.
# =============================================================================
VERSION = "paper_bot_v0.73"


def _v073_grade_badge(row: Dict[str, Any]) -> str:
    g = _v071_grade_from_row(row)
    label_map = {
        "S": "✅ S급",
        "A": "✅ A급",
        "B": "🟡 B급",
        "C": "❔ C급",
        "X": "❌ 제외",
        "U": "❔ 등급미기록",
    }
    return label_map.get(g, "❔ 등급미기록")


def _v073_auto_candidate_text(row: Dict[str, Any]) -> str:
    g = _v071_grade_from_row(row)
    if g == "S":
        return "자동매매 상위후보"
    if g == "A":
        return "자동매매 일반후보"
    if g == "B":
        return "paper 검증"
    if g == "C":
        return "관찰"
    if g == "X":
        return "차단"
    return "등급확인"


def _v073_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    if not ctx and isinstance(raw.get("entry_context"), dict):
        ctx = raw.get("entry_context") or {}
    return ctx if isinstance(ctx, dict) else {}


def _v073_short_reasons(row: Dict[str, Any], limit: int = 3) -> str:
    ctx = _v073_ctx(row)
    raw_reasons = (
        row.get("final_entry_reasons")
        or row.get("reasons")
        or row.get("reason_list")
        or ctx.get("final_entry_reasons")
        or ctx.get("reasons")
        or ctx.get("reason_list")
        or []
    )
    if isinstance(raw_reasons, str):
        parts = [x.strip() for x in re.split(r"[,/|]", raw_reasons) if x.strip()]
    elif isinstance(raw_reasons, list):
        parts = [str(x).strip() for x in raw_reasons if str(x).strip()]
    else:
        parts = []
    cleaned = []
    for x in parts:
        x = x.replace("관찰전환:", "").replace("재확인대기:", "").strip()
        if x and x not in cleaned:
            cleaned.append(x)
        if len(cleaned) >= limit:
            break
    return ", ".join(cleaned) if cleaned else "-"


def _v073_risk_text(row: Dict[str, Any], limit: int = 2) -> str:
    ctx = _v073_ctx(row)
    raw_tags = (
        row.get("quality_risk_tags")
        or row.get("risk_tags")
        or ctx.get("quality_risk_tags")
        or ctx.get("risk_tags")
        or ctx.get("micro_flags")
        or []
    )
    if isinstance(raw_tags, str):
        tags = [x.strip() for x in re.split(r"[,/|]", raw_tags) if x.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(x).strip() for x in raw_tags if str(x).strip()]
    else:
        tags = []
    out = []
    for t in tags:
        tt = t.replace("관찰전환:", "").replace("재확인대기:", "").strip()
        if tt and tt not in out:
            out.append(tt)
        if len(out) >= limit:
            break
    return ", ".join(out) if out else "-"


_v073_base_format_open_list = format_open_list

def format_open_list(limit: int = 20) -> str:  # type: ignore[override]
    """v0.73: OPEN 개별 줄에 등급을 직접 표시한다.

    v0.71/v0.72는 /pstatus, /pscore에는 S/A/B가 나오지만 /popen 개별 줄은
    '주도추세등급'만 보여 사용자가 현재 OPEN의 등급을 바로 보기 어려웠다.
    장부와 청산 로직은 건드리지 않고 표시만 단일화한다.
    """
    open_pos = load_open()
    if not open_pos:
        return "OPEN 없음"
    rows = sorted(open_pos.values(), key=lambda x: float_any(x.get("opened_at"), default=0.0), reverse=True)[:max(1, min(limit, 30))]
    control = load_control()
    lines = [f"전략별 OPEN: {_v064_open_strategy_counts(open_pos)}"]
    for pos in rows:
        age = (now() - float_any(pos.get("opened_at"), default=now())) / 60.0
        hold_txt = hold_text_from_seconds(age * 60)
        opened_txt = opened_text_from_row(pos)[5:16] if opened_text_from_row(pos) != "-" else "-"
        watch = _v049_open_watch_status(pos, control)
        badge = _v073_grade_badge(pos)
        auto_txt = _v073_auto_candidate_text(pos)
        strategy_label = _v064_strategy_label(pos)
        ticker = short_ticker(pos.get("ticker"))
        pnl = float_any(pos.get("last_pnl_pct"), default=0.0)
        peak = fmt_pct(pos.get("peak_pct"))
        reasons = _v073_short_reasons(pos, limit=3)
        risk = _v073_risk_text(pos, limit=2)
        lines.append(
            f"- {badge} {ticker} / {auto_txt} / {strategy_label} / "
            f"진입 {opened_txt} {format_price(pos.get('entry_price'))} / 현재 {format_price(pos.get('current_price'))} / "
            f"{pnl:+.2f}% / 보유 {hold_txt} / 감시 {watch} / 최고 {peak}"
        )
        lines.append(f"  · 근거: {reasons} / 주의: {risk}")
    if len(open_pos) > len(rows):
        lines.append(f"- 나머지 {len(open_pos)-len(rows)}개는 /popen 에서 확인")
    return "\n".join(lines)


_v073_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    text = _v073_base_summary_text()
    try:
        text = text.replace("paper_bot_v0.72", VERSION)
        text = text.replace("v0.72: 등급별 성과 표시 + paper OPEN 고정개수 해제 + 저장 tmp 오류수술", "v0.73: 등급별 성과 표시 + /popen 개별 OPEN 등급표시 + paper OPEN 고정개수 해제")
    except Exception as exc:
        log_error("v073_summary_text", exc)
    return text


_v073_base_pscore_text = _v071_pscore_text

def _v071_pscore_text() -> str:  # type: ignore[override]
    text = _v073_base_pscore_text()
    try:
        text = text.replace("v0.72: S/A/B/C/제외 성과 표시 + paper OPEN 고정개수 해제", "v0.73: S/A/B/C/제외 성과 표시 + /popen 개별 OPEN 등급표시")
    except Exception as exc:
        log_error("v073_pscore_text", exc)
    return text


# =============================================================================
# v0.74: paper command compact batch + KST error-log freshness fix
# - 장부/청산/OPEN 판정은 v0.73 그대로 유지한다.
# - 여러 줄 자동 묶음에서 /pstatus /pscore /popen /perror는 compact 요약으로 먼저 보낸다.
# - 계산시간/전송시간을 분리해 페이퍼봇 명령 지연 원인을 바로 볼 수 있게 한다.
# - KST로 기록된 paper_bot_error.log를 UTC로 잘못 해석해 과거 오류가 새 오류처럼 보이던 문제를 고친다.
# =============================================================================
VERSION = "paper_bot_v0.74"

_V074_SCORE_CACHE: Dict[str, Any] = {"ts": 0.0, "rows": None}


def _v074_focus_closed_cached(ttl_sec: float = 20.0) -> List[Dict[str, Any]]:
    nowv = now()
    rows = _V074_SCORE_CACHE.get("rows")
    if isinstance(rows, list) and nowv - float_any(_V074_SCORE_CACHE.get("ts"), default=0.0) <= ttl_sec:
        return rows
    try:
        rows = _v071_focus_closed()
    except Exception as exc:
        log_error("v074_focus_closed_cached", exc)
        rows = []
    _V074_SCORE_CACHE["ts"] = nowv
    _V074_SCORE_CACHE["rows"] = list(rows or [])
    return list(rows or [])


def _v074_score_short_line(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    rows = list(rows or [])
    st = score_stats(rows)
    n = int(st.get("n", 0) or 0)
    if n <= 0:
        return f"- {label}: 0전"
    return f"- {label}: {n}전 {int(st['wins'])}승 {int(st['losses'])}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%"


def _v074_grade_compact_lines(rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    rows = list(rows if rows is not None else _v074_focus_closed_cached())
    g = _v071_grade_groups(rows)
    auto = g.get("S", []) + g.get("A", [])
    paper = auto + g.get("B", [])
    return [
        "[등급별 CLOSED 요약]",
        _v074_score_short_line("✅ S", g.get("S", [])),
        _v074_score_short_line("✅ A", g.get("A", [])),
        _v074_score_short_line("🟡 B", g.get("B", [])),
        _v074_score_short_line("S/A 자동매매 후보군", auto),
        _v074_score_short_line("S/A/B paper 검증", paper),
        _v074_score_short_line("현재전략 전체", rows),
    ]


def _v074_open_grade_counts(open_pos: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, int]:
    open_pos = open_pos if isinstance(open_pos, dict) else load_open()
    counts = {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0, "U": 0}
    for row in (open_pos or {}).values():
        g = _v071_grade_from_row(row)
        counts[g] = counts.get(g, 0) + 1
    return counts


def _v074_pstatus_compact() -> str:
    control = load_control()
    status = load_json(FILES["status"], {})
    open_pos = load_open()
    grade_open = _v074_open_grade_counts(open_pos)
    rows = _v074_focus_closed_cached()
    st = score_stats(rows)
    pick = status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {}
    lines = [
        f"🧪 /pstatus 요약 · 자동묶음 compact ({VERSION})",
        f"- 실행상태: {'ON' if control.get('running') else 'OFF'} / loop {control.get('loop_seconds')}s / OPEN 전체 {len(open_pos)}",
        f"- OPEN: S {grade_open.get('S',0)} / A {grade_open.get('A',0)} / B {grade_open.get('B',0)} / C {grade_open.get('C',0)} / 제외 {grade_open.get('X',0)} / 한도 {_v072_limit_text(control)}",
        f"- 후보읽음: {pick.get('paper_file',0)} / S {pick.get('grade_s_seen',0)} / A {pick.get('grade_a_seen',0)} / B {pick.get('grade_b_seen',0)} / C스킵 {pick.get('grade_c_observe_skip',0)} / 제외스킵 {pick.get('grade_x_block_skip',0)} / micro대기 {pick.get('micro_preopen_wait',0)}",
        f"- CLOSED 현재전략: {int(st.get('n',0))}/30 / 승률 {st.get('win_rate',0.0):.1f}% / 합산 {st.get('total',0.0):+.2f}% / 평균 {st.get('avg',0.0):+.2f}%",
        "- 신규 OPEN: S/A/B / C는 관찰 / 제외는 차단 / 자동매수 OFF",
        *_v074_grade_compact_lines(rows),
        "- 상세 전문은 /pstatus 단독 실행",
    ]
    return "\n".join(lines)


def _v074_pscore_compact() -> str:
    rows = _v074_focus_closed_cached()
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in rows)
    lines = [
        "📊 /pscore 요약 · 자동묶음 compact",
        f"- 현재전략: {PAPER_FOCUS_STRATEGY_LABEL}",
        "- S/A=미래 자동매매 후보군, B=paper 검증, C=관찰",
        *_v074_grade_compact_lines(rows),
        "",
        "[종료 사유 TOP]",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5):
            sub = [r for r in rows if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_v074_score_short_line(label_kr(k), sub))
    else:
        lines.append("- 현재전략 CLOSED 없음")
    lines.append("- 상세 전문은 /pscore 단독 실행")
    return "\n".join(lines)


def _v074_popen_compact() -> str:
    open_pos = load_open()
    lines = ["📂 /popen 요약 · 자동묶음 compact"]
    if not open_pos:
        lines.append("OPEN 없음")
    else:
        lines.append(format_open_list(8))
        if len(open_pos) > 8:
            lines.append(f"- 나머지 {len(open_pos)-8}개는 /popen 단독 실행")
    return "\n".join(lines)


def _v074_parse_error_line_ts_kst(line: str) -> float:
    try:
        m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", str(line or ""))
        if not m:
            return 0.0
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST)
        return dt.timestamp()
    except Exception:
        return 0.0


def perror_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return "🧯 paper_bot_error.log /perror_full\n\n" + tail_file(FILES["error"], 120)[-4500:]
    if not FILES["error"].exists():
        return "🧯 paper_bot_error.log\n\n✅ 오류로그 파일 없음"
    try:
        lines = FILES["error"].read_text(encoding="utf-8", errors="ignore").splitlines()
        recent_marked = [ln for ln in lines if _v074_parse_error_line_ts_kst(ln) >= PROGRAM_STARTED_TS]
        size = FILES["error"].stat().st_size if FILES["error"].exists() else 0
        out = ["🧯 paper_bot_error.log", ""]
        if recent_marked:
            out.append(f"⚠️ 새 실행 이후 오류 흔적 {len(recent_marked)}건")
            out.extend(recent_marked[-8:])
        else:
            out.append("✅ 새 실행 이후 오류 없음")
            if size > 0:
                out.append(f"- 과거 오류 흔적 있음 / 로그크기 {size} bytes")
                out.append("- 자세히: /perror_full")
        return "\n".join(out)
    except Exception as exc:
        return f"🧯 paper_bot_error.log\n\n⚠️ 오류로그 요약 실패: {exc}\n자세히: /perror_full"


_v074_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    text = _v074_base_summary_text()
    try:
        text = text.replace("paper_bot_v0.73", VERSION)
        text = text.replace("v0.73: 등급별 성과 표시 + /popen 개별 OPEN 등급표시 + paper OPEN 고정개수 해제", "v0.74: 페이퍼봇 자동묶음 compact + /popen 등급표시 + 오류시간판정 보정")
    except Exception as exc:
        log_error("v074_summary_text", exc)
    return text


_v074_base_pscore_text = _v071_pscore_text

def _v071_pscore_text() -> str:  # type: ignore[override]
    text = _v074_base_pscore_text()
    try:
        text = text.replace("v0.73: S/A/B/C/제외 성과 표시 + /popen 개별 OPEN 등급표시", "v0.74: S/A/B/C/제외 성과 표시 + /popen 등급표시 + 자동묶음 compact")
    except Exception as exc:
        log_error("v074_pscore_text", exc)
    return text


def _v074_compact_response(line: str) -> str:
    name = _cmd_name(line)
    if name == "pstatus":
        return _v074_pstatus_compact()
    if name == "pscore":
        return _v074_pscore_compact()
    if name == "popen":
        return _v074_popen_compact()
    if name == "perror":
        return perror_text(False)
    if name == "perror_full":
        return "자동묶음에서는 /perror_full 생략. 단독 실행하세요."
    if name == "pbatch":
        return "이미 자동 묶음 처리 중이라 /pbatch는 건너뜀"
    return command_response(line)


def _v074_send_timed(chat_id: str, text: str) -> float:
    t0 = now()
    send_chat(chat_id, text)
    return max(0.0, now() - t0)


def handle_command(chat_id: str, text: str) -> None:  # type: ignore[override]
    lines = _command_lines(text)
    if len(lines) > 1:
        total_start = now()
        timings: List[Dict[str, Any]] = []
        intro = (
            "📦 페이퍼봇 자동 묶음 명령 접수\n"
            "- /pbatch 없이 여러 줄 명령을 감지\n"
            f"- 실행 {len(lines)}개\n"
            "- v0.74: 묶음에서는 compact 요약을 먼저 표시, 상세 전문은 각 명령 단독 실행"
        )
        intro_send = _v074_send_timed(chat_id, intro)
        total_calc = 0.0
        total_send = intro_send
        for idx, line in enumerate(lines, start=1):
            name = _cmd_name(line)
            calc_start = now()
            try:
                body = _v074_compact_response(line)
                ok = "OK"
            except Exception as exc:
                log_error(f"v074_multi:{name}", exc)
                body = f"오류: {exc.__class__.__name__}: {exc}"
                ok = "ERR"
            calc_sec = max(0.0, now() - calc_start)
            send_sec = _v074_send_timed(chat_id, f"[{idx}/{len(lines)}] /{name} (계산 {calc_sec:.2f}s / {ok})\n" + body)
            total_calc += calc_sec
            total_send += send_sec
            timings.append({"name": name, "calc": calc_sec, "send": send_sec, "ok": ok})
        total_sec = max(0.0, now() - total_start)
        rows = [
            "🧾 페이퍼봇 자동 묶음 시간표",
            f"- 접수 전송: {intro_send:.2f}s",
        ]
        for t in timings:
            icon = "✅" if t.get("ok") == "OK" else "❌"
            rows.append(f"- {icon} /{t.get('name')}: 계산 {t.get('calc',0.0):.2f}s / 전송 {t.get('send',0.0):.2f}s / {t.get('ok')}")
        rows += [
            f"- 합계: 계산 {total_calc:.2f}s / 전송 {total_send:.2f}s / 전체 {total_sec:.2f}s",
            "- 상세 전문은 각 명령을 단독으로 실행",
        ]
        _v074_send_timed(chat_id, "\n".join(rows))
        return
    send_chat(chat_id, command_response(text))



# ──────────────────────────────────────────────────────────────────────────────
# paper_bot v0.75: S/A만 실제 paper OPEN, B/C는 shadow_eval 가상복기
# - B/C는 OPEN 장부에 태우지 않지만, 가상 포지션 파일로 60분/손절/익절 성과를 추적한다.
# - 기존 CLOSED / OPEN 장부는 건드리지 않는다.
# ──────────────────────────────────────────────────────────────────────────────
VERSION = "paper_bot_v0.75"
FILES["shadow_eval_open"] = BASE_DIR / "paper_bot_shadow_eval_open.json"
FILES["shadow_eval_closed"] = BASE_DIR / "paper_bot_shadow_eval_closed.jsonl"
SHADOW_EVAL_TTL_SEC = float(os.getenv("PAPER_SHADOW_EVAL_TTL_SEC", "90"))
SHADOW_EVAL_MAX_LATEST = int(os.getenv("PAPER_SHADOW_EVAL_MAX_LATEST", "260"))

_v075_base_load_control = load_control

def load_control() -> Dict[str, Any]:  # type: ignore[override]
    control = _v075_base_load_control()
    fixed = {
        "paper_open_grades": ["S", "A"],
        "max_open_strict": 0,
        "max_open_shadow": 0,
        "open_shadow_positions": False,
        "shadow_review_only": True,
    }
    changed = False
    for k, v in fixed.items():
        if control.get(k) != v:
            control[k] = v
            changed = True
    if changed:
        try:
            save_json(FILES["control"], control)
            set_pause_flags(bool(control.get("running")))
        except Exception:
            pass
    return control


def _v075_shadow_eval_load_open() -> Dict[str, Dict[str, Any]]:
    obj = load_json(FILES["shadow_eval_open"], {})
    return obj if isinstance(obj, dict) else {}


def _v075_shadow_eval_save_open(obj: Dict[str, Dict[str, Any]]) -> None:
    save_json(FILES["shadow_eval_open"], obj or {})


def _v075_shadow_eval_id(ev: Dict[str, Any]) -> str:
    t = normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol"))
    created = int(float_any(ev.get("created_at"), ev.get("source_created_at"), ev.get("updated_ts"), default=0))
    grade = _v070_grade_from_event(ev)
    scan = str(ev.get("scan_id") or ev.get("source_scan_id") or ev.get("candidate_id") or "")[:32]
    return f"shadow_eval:{t}:{grade}:{created}:{scan}"


def _v075_shadow_eval_closed_ids(max_lines: int = 20000) -> set[str]:
    out = set()
    try:
        for r in read_jsonl(FILES["shadow_eval_closed"], max_lines=max_lines):
            sid = str(r.get("shadow_eval_id") or r.get("pos_id") or "")
            if sid:
                out.add(sid)
    except Exception:
        pass
    return out


def _v075_shadow_eval_entry(ev: Dict[str, Any], sid: str, grade: str, control: Dict[str, Any]) -> Dict[str, Any]:
    entry = get_event_price(ev)
    ctx = ev.get("entry_context") if isinstance(ev.get("entry_context"), dict) else {}
    nowv = now()
    pos = {
        "pos_id": sid,
        "shadow_eval_id": sid,
        "shadow_eval": True,
        "lane": "shadow_eval",
        "ticker": normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol")),
        "strategy": ev.get("strategy") or ev.get("strategy_name") or ctx.get("strategy") or "장세 선택형 주도코인 추세 지속 전략",
        "strategy_key": ev.get("strategy_key") or ctx.get("strategy_key") or "leader_momentum_continuation",
        "entry_price": entry,
        "current_price": entry,
        "opened_at": nowv,
        "opened_at_text": iso_ts(nowv),
        "source_created_at": float_any(ev.get("created_at"), ev.get("source_created_at"), default=nowv),
        "candidate_grade": grade,
        "candidate_grade_label": ev.get("candidate_grade_label") or ctx.get("candidate_grade_label") or ("🟡 B급 관찰/복기 후보" if grade == "B" else "❔ C급 관찰 후보"),
        "strategy_bucket_hits": ev.get("strategy_bucket_hits") if isinstance(ev.get("strategy_bucket_hits"), list) else [],
        "strategy_bucket_labels": ev.get("strategy_bucket_labels") if isinstance(ev.get("strategy_bucket_labels"), list) else [],
        "strategy_eval_grades": ev.get("strategy_eval_grades") if isinstance(ev.get("strategy_eval_grades"), dict) else {},
        "strategy_eval_labels": ev.get("strategy_eval_labels") if isinstance(ev.get("strategy_eval_labels"), dict) else {},
        "leader_original_grade": ev.get("leader_original_grade") or ctx.get("leader_original_grade") or grade,
        "leader_original_label": ev.get("leader_original_label") or ctx.get("leader_original_label"),
        "take_profit_pct": float_any(ev.get("take_profit_pct"), ctx.get("take_profit_pct"), control.get("take_profit_pct"), default=1.20),
        "extended_target_pct": float_any(ev.get("extended_target_pct"), ctx.get("extended_target_pct"), control.get("extended_target_pct"), default=2.00),
        "protect_trigger_pct": float_any(ev.get("protect_trigger_pct"), ctx.get("protect_trigger_pct"), control.get("protect_trigger_pct"), default=0.70),
        "protect_floor_pct": float_any(ev.get("protect_floor_pct"), ctx.get("protect_floor_pct"), control.get("protect_floor_pct"), default=0.35),
        "stop_loss_pct": float_any(ev.get("stop_loss_pct"), ctx.get("stop_loss_pct"), control.get("stop_loss_pct"), default=-0.60),
        "time_exit_min": float_any(ev.get("time_exit_min"), ctx.get("time_exit_min"), control.get("time_exit_minutes"), default=60),
        "entry_context": dict(ctx, shadow_eval=True, candidate_grade=grade, candidate_grade_label=ev.get("candidate_grade_label") or ctx.get("candidate_grade_label")),
        "source_event": {k: ev.get(k) for k in ("ticker", "score", "leader_score", "good", "wait", "hard", "created_at", "scan_id", "strategy_bucket_primary", "strategy_bucket_primary_label")},
    }
    return pos


def _v075_shadow_eval_pick(control: Dict[str, Any], open_eval: Dict[str, Dict[str, Any]]) -> Tuple[int, Dict[str, int]]:
    stats = {"seen": 0, "opened": 0, "dup": 0, "bad": 0, "stale": 0, "grade_b": 0, "grade_c": 0}
    closed_ids = _v075_shadow_eval_closed_ids()
    rows = read_jsonl(candidate_input_path("shadow"), max_lines=SHADOW_EVAL_MAX_LATEST)
    if not rows:
        return 0, stats
    filtered, _meta = latest_scan_filter([dict(r, _lane_hint="shadow") for r in rows], control)
    nowv = now()
    for ev in filtered:
        stats["seen"] += 1
        if not isinstance(ev, dict):
            continue
        grade = _v070_grade_from_event(ev)
        if grade not in {"B", "C"}:
            continue
        if grade == "B": stats["grade_b"] += 1
        if grade == "C": stats["grade_c"] += 1
        ts = float_any(ev.get("created_at"), ev.get("source_created_at"), ev.get("updated_ts"), default=0.0)
        if ts > 0 and nowv - ts > SHADOW_EVAL_TTL_SEC:
            stats["stale"] += 1
            continue
        sid = _v075_shadow_eval_id(ev)
        if sid in open_eval or sid in closed_ids:
            stats["dup"] += 1
            continue
        if get_event_price(ev) <= 0 or not normalize_ticker(ev.get("ticker") or ev.get("market") or ev.get("symbol")):
            stats["bad"] += 1
            continue
        open_eval[sid] = _v075_shadow_eval_entry(ev, sid, grade, control)
        stats["opened"] += 1
    return stats["opened"], stats


def _v075_shadow_eval_update(control: Dict[str, Any]) -> Dict[str, Any]:
    open_eval = _v075_shadow_eval_load_open()
    opened, pick_stats = _v075_shadow_eval_pick(control, open_eval)
    closed_items: List[Dict[str, Any]] = []
    for sid, pos in list(open_eval.items()):
        try:
            updated, closed = update_position(pos, control)
            if closed:
                closed = dict(closed)
                closed["shadow_eval"] = True
                closed["shadow_eval_id"] = sid
                closed["lane"] = "shadow_eval"
                closed["candidate_grade"] = pos.get("candidate_grade")
                closed["candidate_grade_label"] = pos.get("candidate_grade_label")
                closed["strategy_eval_grades"] = pos.get("strategy_eval_grades", {})
                closed["leader_original_grade"] = pos.get("leader_original_grade")
                append_jsonl(FILES["shadow_eval_closed"], closed)
                closed_items.append(closed)
                open_eval.pop(sid, None)
            else:
                open_eval[sid] = updated
        except Exception as exc:
            log_error("shadow_eval_update", exc)
    _v075_shadow_eval_save_open(open_eval)
    return {"opened": opened, "open": len(open_eval), "closed": len(closed_items), "pick": pick_stats}


def _v075_shadow_eval_stats_lines() -> List[str]:
    rows = read_jsonl(FILES["shadow_eval_closed"], max_lines=20000)
    open_eval = _v075_shadow_eval_load_open()
    if not rows and not open_eval:
        return ["[B/C 가상복기 성과]", "- 아직 없음"]
    b = [r for r in rows if str(r.get("candidate_grade") or "").upper() == "B"]
    c = [r for r in rows if str(r.get("candidate_grade") or "").upper() == "C"]
    lines = ["[B/C 가상복기 성과 · 실제 OPEN 아님]"]
    lines.append(_v071_stats_line("🟡 B급 가상복기", b))
    lines.append(_v071_stats_line("❔ C급 가상복기", c))
    lines.append(f"- 진행중 가상복기: {len(open_eval)}개 / 실제 paper OPEN 아님")
    return lines


_v075_base_run_cycle = run_cycle

def run_cycle() -> Dict[str, Any]:  # type: ignore[override]
    status = _v075_base_run_cycle()
    try:
        control = load_control()
        shadow_eval = _v075_shadow_eval_update(control)
        status["shadow_eval"] = shadow_eval
        save_json(FILES["status"], status)
    except Exception as exc:
        log_error("v075_shadow_eval_cycle", exc)
    return status


_v075_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    text = _v075_base_summary_text()
    try:
        text = text.replace("신규 OPEN: 허용 / paper OPEN S·A·B / 고정개수 없음 / 실제 자동매매 후보 S·A만", "신규 OPEN: 허용 / 실제 paper OPEN S·A만 / B·C는 가상복기 / 고정개수 없음")
        text = text.replace("- CLOSED 30건 전 조건 변경 금지 / 실제 자동매매는 S·A만 후보 / B는 paper 검증 / C는 관찰", "- 실제 paper OPEN은 S·A만 / B·C는 OPEN 없이 가상복기 성과 추적 / 자동매수 OFF")
        text += "\n\n" + "\n".join(_v075_shadow_eval_stats_lines())
    except Exception as exc:
        log_error("v075_summary_text", exc)
    return text

_v075_base_pscore_text = _v071_pscore_text

def _v071_pscore_text() -> str:  # type: ignore[override]
    text = _v075_base_pscore_text()
    try:
        text = text.replace("v0.74: S/A/B/C/제외 성과 표시 + /popen 등급표시 + 자동묶음 compact", "v0.75: 실제 OPEN S/A만 + B/C shadow_eval 가상성과 + 자동묶음 compact")
        text = text.replace("B=paper 검증", "B=가상복기")
        text += "\n\n" + "\n".join(_v075_shadow_eval_stats_lines())
    except Exception as exc:
        log_error("v075_pscore_text", exc)
    return text

_v075_base_pstatus_compact = _v074_pstatus_compact

def _v074_pstatus_compact() -> str:  # type: ignore[override]
    text = _v075_base_pstatus_compact()
    try:
        st = load_json(FILES.get("status", BASE_DIR / "paper_bot_status.json"), {})
        se = st.get("shadow_eval") if isinstance(st, dict) else {}
        if isinstance(se, dict):
            text += f"\n- B/C 가상복기: 진행 {se.get('open',0)} / 이번 cycle 신규 {se.get('opened',0)} / 청산 {se.get('closed',0)}"
        text = text.replace("S/A/B", "S/A OPEN + B/C 가상복기")
    except Exception as exc:
        log_error("v075_pstatus_compact", exc)
    return text

_v075_base_pscore_compact = _v074_pscore_compact

def _v074_pscore_compact() -> str:  # type: ignore[override]
    text = _v075_base_pscore_compact()
    try:
        rows = read_jsonl(FILES["shadow_eval_closed"], max_lines=20000)
        if rows:
            b = [r for r in rows if str(r.get("candidate_grade") or "").upper() == "B"]
            c = [r for r in rows if str(r.get("candidate_grade") or "").upper() == "C"]
            text += "\n[B/C 가상복기]"
            text += "\n" + _v071_stats_line("🟡 B급", b)
            text += "\n" + _v071_stats_line("❔ C급", c)
    except Exception as exc:
        log_error("v075_pscore_compact", exc)
    return text

try:
    DEFAULT_CONTROL["paper_open_grades"] = ["S", "A"]
    DEFAULT_CONTROL["open_shadow_positions"] = False
    DEFAULT_CONTROL["shadow_review_only"] = True
except Exception:
    pass


# =============================================================================
# v0.76: fast-stop / larger-profit fallback + closed-read light cache
# - 후보 entry_context 청산값을 최우선으로 쓰는 구조는 유지한다.
# - 후보값이 없을 때 fallback을 v310 정책에 맞춘다.
# - 기본 명령이 매번 전체 CLOSED 장부를 훑지 않도록 읽기 범위를 줄인다.
# - 장부 파일은 삭제하지 않는다. full 재계산이 필요하면 별도 full 명령으로 본다.
# =============================================================================
VERSION = "paper_bot_v0.76"
V076_CLOSED_READ_LIMIT = int(os.getenv("PAPERBOT_CLOSED_READ_LIMIT", "2500"))

try:
    DEFAULT_CONTROL.update({
        "take_profit_pct": 1.80,
        "extended_target_pct": 2.80,
        "protect_trigger_pct": 1.00,
        "protect_floor_pct": 0.45,
        "stop_loss_pct": -0.45,
        "time_exit_minutes": 60,
        "paper_open_grades": ["S", "A"],
        "open_shadow_positions": False,
        "shadow_review_only": True,
    })
except Exception:
    pass

_v076_base_v064_focus_closed_rows = _v064_focus_closed_rows

def _v064_focus_closed_rows(limit: int = 20000) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v0.76: 기본 상태/성과 명령은 최근 CLOSED만 읽는다.

    장부 보존과는 별개다. paper_bot_closed.jsonl은 그대로 유지하고,
    명령 계산에서만 서버 부담을 줄인다.
    """
    try:
        lim = max(300, min(int(limit or V076_CLOSED_READ_LIMIT), V076_CLOSED_READ_LIMIT))
    except Exception:
        lim = V076_CLOSED_READ_LIMIT
    return _v076_base_v064_focus_closed_rows(lim)

_v076_base_pstatus_compact = _v074_pstatus_compact

def _v074_pstatus_compact() -> str:  # type: ignore[override]
    text = _v076_base_pstatus_compact()
    try:
        text = text.replace("paper_bot_v0.75", VERSION)
        text += f"\n- v0.76: 기본 CLOSED 계산 최근 {V076_CLOSED_READ_LIMIT}줄 기준 / 전체 장부는 보존"
        text += "\n- fallback 청산: 익절 +1.80% / 확장 +2.80% / 보호 +1.00%→+0.45% / 손절 -0.45% / 시간 60분"
    except Exception as exc:
        log_error("v076_pstatus_compact", exc)
    return text

_v076_base_pscore_compact = _v074_pscore_compact

def _v074_pscore_compact() -> str:  # type: ignore[override]
    text = _v076_base_pscore_compact()
    try:
        text = text.replace("/pscore 요약 · 자동묶음 compact", "/pscore 요약 · 자동묶음 compact (최근 CLOSED 경량계산)")
        text += f"\n- v0.76: 기본 성과는 최근 {V076_CLOSED_READ_LIMIT}줄 읽기. 오래된 전체 장부는 삭제하지 않음"
    except Exception as exc:
        log_error("v076_pscore_compact", exc)
    return text

_v076_base_summary_text = summary_text

def summary_text() -> str:  # type: ignore[override]
    text = _v076_base_summary_text()
    try:
        text = text.replace("paper_bot_v0.75", VERSION)
        text = text.replace("fallback 익절 +1.20%", "fallback 익절 +1.80%")
        text = text.replace("보호 +0.70%→+0.35%", "보호 +1.00%→+0.45%")
        text = text.replace("손절 -0.60%", "손절 -0.45%")
        text += f"\n- v0.76 경량화: 기본 성과계산은 최근 {V076_CLOSED_READ_LIMIT}줄 기준, 장부는 보존"
    except Exception as exc:
        log_error("v076_summary_text", exc)
    return text


# =============================================================================
# paper_bot v0.77: default command cache surgery
# - /pscore, /pstatus 기본 명령이 CLOSED 6000줄을 직접 읽던 경로를 끊는다.
# - 기본 명령은 paper_bot_score_cache.json만 읽는다.
# - 직접계산은 /pscore_full, /pstatus_full에서만 허용한다.
# - 장부 OPEN/CLOSED/trade_log는 삭제하지 않는다.
# =============================================================================
VERSION = "paper_bot_v0.77"
FILES["score_cache"] = BASE_DIR / "paper_bot_score_cache.json"
V077_CACHE_TTL_SEC = float(os.getenv("PAPERBOT_SCORE_CACHE_TTL_SEC", "90"))
V077_CACHE_TAIL_LINES = int(os.getenv("PAPERBOT_SCORE_CACHE_TAIL_LINES", "450"))
V077_CACHE_TAIL_BYTES = int(os.getenv("PAPERBOT_SCORE_CACHE_TAIL_BYTES", "1200000"))


def _v077_read_jsonl_tail_fast(path: Path, max_lines: int = 500, max_bytes: int = 1_200_000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as f:
            f.seek(start)
            data = f.read()
        text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        if start > 0 and lines:
            lines = lines[1:]  # drop partial line
        out = []
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
        return out
    except Exception as exc:
        log_error("v077_read_tail_fast", exc)
        return []


def _v077_focus_rows_fast() -> List[Dict[str, Any]]:
    rows = _v077_read_jsonl_tail_fast(FILES["closed"], V077_CACHE_TAIL_LINES, V077_CACHE_TAIL_BYTES)
    # 현재전략/전략바구니 관련만. 과거 장부는 보존하지만 기본 지표에서는 최근·현재 관련만 본다.
    focus = []
    for r in rows:
        sk = str(r.get("strategy_key") or r.get("paper_strategy_key") or "")
        st = str(r.get("strategy") or r.get("strategy_name") or "")
        if ("leader_momentum" in sk or "money_reaccel" in sk or "strategy_basket" in sk or "장세 선택형" in st or "전략 바구니" in st):
            focus.append(r)
    return focus


def _v077_group_by_grade(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out = {"S": [], "A": [], "B": [], "C": [], "X": []}
    for r in rows or []:
        g = _v071_grade_from_row(r) if '_v071_grade_from_row' in globals() else str(r.get("candidate_grade") or "C").upper()
        if g not in out:
            g = "C"
        out[g].append(r)
    return out


def _v077_short_stat(label: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    s = score_stats(rows)
    return {"label": label, "n": int(s.get("n",0)), "wins": int(s.get("wins",0)), "losses": int(s.get("losses",0)), "win_rate": round(float_any(s.get("win_rate"), default=0.0), 1), "total": round(float_any(s.get("total"), default=0.0), 2), "avg": round(float_any(s.get("avg"), default=0.0), 2)}


def _v077_reason_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    c = Counter(str(r.get("exit_reason") or "unknown") for r in rows or [])
    out = []
    for k, _ in c.most_common(6):
        sub = [r for r in rows if str(r.get("exit_reason") or "unknown") == k]
        d = _v077_short_stat(label_kr(k), sub)
        d["key"] = k
        out.append(d)
    return out


def _v077_strategy_stats(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        keys = []
        evals = r.get("strategy_eval_grades") if isinstance(r.get("strategy_eval_grades"), dict) else {}
        if evals:
            keys = [str(k) for k in evals.keys()]
        else:
            k = str(r.get("strategy_bucket_primary") or r.get("strategy_key") or r.get("paper_strategy_key") or "unknown")
            keys = [k]
        for k in keys:
            buckets.setdefault(k, []).append(r)
    return [_v077_short_stat(k, v) for k, v in sorted(buckets.items())[:12]]


def _v077_build_score_cache(reason: str = "cycle") -> Dict[str, Any]:
    rows = _v077_focus_rows_fast()
    groups = _v077_group_by_grade(rows)
    auto = groups.get("S", []) + groups.get("A", [])
    paper = auto + groups.get("B", [])
    open_pos = load_open()
    status = load_json(FILES.get("status", BASE_DIR / "paper_bot_status.json"), {})
    shadow_rows = _v077_read_jsonl_tail_fast(FILES.get("shadow_eval_closed", BASE_DIR / "paper_bot_shadow_eval_closed.jsonl"), 300, 600000)
    cache = {
        "schema": "paper_score_cache_v077",
        "version": VERSION,
        "updated_at": now(),
        "updated_at_text": iso_ts(),
        "reason": reason,
        "source_tail_lines": V077_CACHE_TAIL_LINES,
        "closed_file_size": FILES["closed"].stat().st_size if FILES["closed"].exists() else 0,
        "closed_total": closed_count(),
        "open_total": len(open_pos),
        "open_grade_counts": _v074_open_grade_counts(open_pos) if '_v074_open_grade_counts' in globals() else {},
        "loop_seconds": load_control().get("loop_seconds"),
        "running": bool(load_control().get("running")),
        "pick_stats": status.get("pick_stats") if isinstance(status.get("pick_stats"), dict) else {},
        "grade_stats": {
            "S": _v077_short_stat("✅ S", groups.get("S", [])),
            "A": _v077_short_stat("✅ A", groups.get("A", [])),
            "B": _v077_short_stat("🟡 B", groups.get("B", [])),
            "C": _v077_short_stat("❔ C", groups.get("C", [])),
            "X": _v077_short_stat("❌ 제외", groups.get("X", [])),
            "AUTO": _v077_short_stat("S/A 자동매매 후보군", auto),
            "PAPER": _v077_short_stat("S/A OPEN + B/C 가상복기", paper),
            "ALL": _v077_short_stat("현재전략 최근", rows),
        },
        "reason_stats": _v077_reason_stats(rows),
        "strategy_stats": _v077_strategy_stats(rows),
        "shadow_eval": {
            "closed_rows": len(shadow_rows),
            "B": _v077_short_stat("🟡 B 가상복기", [r for r in shadow_rows if str(r.get("candidate_grade") or "").upper() == "B"]),
            "C": _v077_short_stat("❔ C 가상복기", [r for r in shadow_rows if str(r.get("candidate_grade") or "").upper() == "C"]),
        },
        "note": "v0.77: 기본 명령은 이 캐시만 읽음. 전체/직접계산은 /pscore_full 또는 /pstatus_full.",
    }
    save_json(FILES["score_cache"], cache)
    return cache


def _v077_score_cache(allow_stale: bool = True) -> Dict[str, Any]:
    c = load_json(FILES["score_cache"], {})
    if not isinstance(c, dict) or not c:
        return {}
    return c


def _v077_stat_line_from_cache(d: Dict[str, Any]) -> str:
    if not d:
        return "- 없음"
    return f"- {d.get('label','-')}: {d.get('n',0)}전 {d.get('wins',0)}승 {d.get('losses',0)}패 / 승률 {d.get('win_rate',0.0):.1f}% / 합산 {float_any(d.get('total'),default=0.0):+.2f}% / 평균 {float_any(d.get('avg'),default=0.0):+.2f}%"


def _v077_cache_age_text(c: Dict[str, Any]) -> str:
    age = max(0.0, now() - float_any(c.get("updated_at"), default=0.0)) if c else -1
    if age < 0:
        return "캐시 없음"
    flag = "✅" if age <= V077_CACHE_TTL_SEC else "⚠️"
    return f"{flag} 캐시 {age:.0f}초 전"


def _v077_pstatus_cached_text(compact: bool = True) -> str:
    c = _v077_score_cache()
    if not c:
        return "🧪 /pstatus 요약 · 캐시 준비중\n- 다음 loop에서 paper_score_cache.json 생성 후 표시됩니다.\n- 무거운 직접계산은 /pstatus_full 단독 실행"
    og = c.get("open_grade_counts") if isinstance(c.get("open_grade_counts"), dict) else {}
    gs = c.get("grade_stats") if isinstance(c.get("grade_stats"), dict) else {}
    pick = c.get("pick_stats") if isinstance(c.get("pick_stats"), dict) else {}
    lines = [
        f"🧪 /pstatus 요약 · 캐시전용 ({VERSION})",
        f"- 상태: {'ON' if c.get('running') else 'OFF'} / loop {c.get('loop_seconds','-')}s / OPEN {c.get('open_total',0)} / {_v077_cache_age_text(c)}",
        f"- OPEN: S {og.get('S',0)} / A {og.get('A',0)} / B {og.get('B',0)} / C {og.get('C',0)} / 제외 {og.get('X',0)} / 한도 고정없음",
        f"- 후보읽음: {pick.get('paper_file',0)} / S {pick.get('grade_s_seen',0)} / A {pick.get('grade_a_seen',0)} / B {pick.get('grade_b_seen',0)} / C스킵 {pick.get('grade_c_observe_skip',0)} / 제외스킵 {pick.get('grade_x_block_skip',0)}",
        "[최근 성과 캐시]",
        _v077_stat_line_from_cache(gs.get("S", {})),
        _v077_stat_line_from_cache(gs.get("A", {})),
        _v077_stat_line_from_cache(gs.get("B", {})),
        _v077_stat_line_from_cache(gs.get("AUTO", {})),
        _v077_stat_line_from_cache(gs.get("ALL", {})),
        "- 실제 paper OPEN은 S/A만. B/C는 OPEN 없이 가상복기. 자동매수 OFF",
        "- 장부는 보존. 기본명령은 캐시만 읽음. 전체 직접계산은 /pstatus_full",
    ]
    return "\n".join(lines)


def _v077_pscore_cached_text(compact: bool = True) -> str:
    c = _v077_score_cache()
    if not c:
        return "📊 /pscore 요약 · 캐시 준비중\n- 다음 loop에서 paper_score_cache.json 생성 후 표시됩니다.\n- 무거운 직접계산은 /pscore_full 단독 실행"
    gs = c.get("grade_stats") if isinstance(c.get("grade_stats"), dict) else {}
    lines = [
        f"📊 /pscore 요약 · 캐시전용 ({VERSION})",
        f"- 기준: 최근 {c.get('source_tail_lines','-')}줄 tail 캐시 / 전체 CLOSED {c.get('closed_total','-')}개 보존 / {_v077_cache_age_text(c)}",
        "[등급별 최근 성과]",
        _v077_stat_line_from_cache(gs.get("S", {})),
        _v077_stat_line_from_cache(gs.get("A", {})),
        _v077_stat_line_from_cache(gs.get("B", {})),
        _v077_stat_line_from_cache(gs.get("AUTO", {})),
        _v077_stat_line_from_cache(gs.get("PAPER", {})),
        _v077_stat_line_from_cache(gs.get("ALL", {})),
        "[종료 사유 TOP]",
    ]
    for d in c.get("reason_stats", [])[:6] if isinstance(c.get("reason_stats"), list) else []:
        lines.append(_v077_stat_line_from_cache(d))
    ss = c.get("shadow_eval") if isinstance(c.get("shadow_eval"), dict) else {}
    if ss:
        lines += ["[B/C 가상복기 캐시]", _v077_stat_line_from_cache(ss.get("B", {})), _v077_stat_line_from_cache(ss.get("C", {}))]
    lines += ["- 기본 /pscore는 직접계산 안 함. 전체 재계산은 /pscore_full 단독 실행"]
    return "\n".join(lines)


_v077_base_run_cycle = run_cycle

def run_cycle() -> Dict[str, Any]:  # type: ignore[override]
    st = _v077_base_run_cycle()
    try:
        cache = _v077_build_score_cache("run_cycle")
        if isinstance(st, dict):
            st["score_cache_version"] = VERSION
            st["score_cache_updated_at"] = cache.get("updated_at")
            st["score_cache_age_sec"] = 0
            save_json(FILES["status"], st)
    except Exception as exc:
        log_error("v077_score_cache_update", exc)
    return st


def summary_text() -> str:  # type: ignore[override]
    return _v077_pstatus_cached_text(compact=False)


def _v071_pscore_text() -> str:  # type: ignore[override]
    return _v077_pscore_cached_text(compact=False)


def _v074_pstatus_compact() -> str:  # type: ignore[override]
    return _v077_pstatus_cached_text(compact=True)


def _v074_pscore_compact() -> str:  # type: ignore[override]
    return _v077_pscore_cached_text(compact=True)


_v077_base_command_response = command_response

def command_response(text: str) -> str:  # type: ignore[override]
    cmd = (text or "").strip().split()[0].lower() if (text or "").strip().split() else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    if cmd == "/pstatus":
        return _v077_pstatus_cached_text(False)
    if cmd == "/pscore":
        return _v077_pscore_cached_text(False)
    if cmd == "/pstatus_full":
        return _v076_summary_text() if '_v076_summary_text' in globals() else _v077_base_command_response('/pstatus')
    if cmd == "/pscore_full":
        # full은 기존 직접계산 경로를 명시적으로 사용한다. 기본 명령에서는 절대 호출하지 않는다.
        try:
            return _v075_base_pscore_text() if '_v075_base_pscore_text' in globals() else _v077_base_command_response('/pscore')
        except Exception as exc:
            return f"📊 /pscore_full\n직접계산 실패: {exc.__class__.__name__}: {exc}"
    return _v077_base_command_response(text)

try:
    DEFAULT_CONTROL.update({"paper_open_grades": ["S", "A"], "open_shadow_positions": False, "shadow_review_only": True})
except Exception:
    pass
try:
    # 시작 직후 캐시가 없으면 가볍게 한 번 만든다. 실패해도 기본 명령은 직접계산으로 돌아가지 않는다.
    if not FILES["score_cache"].exists():
        _v077_build_score_cache("startup")
except Exception as exc:
    try:
        log_error("v077_startup_cache", exc)
    except Exception:
        pass

if __name__ == "__main__":
    main()
