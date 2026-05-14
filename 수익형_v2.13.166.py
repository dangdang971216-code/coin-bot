#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
수익형_v2.13.166.py

새 본선 신축판.
- 기존 5만 줄 tail patch / fallback / 내부 paper / candidate_events 소비를 가져오지 않는다.
- 메인봇 역할은 스캔·전략판단·paper/shadow 후보파일 출력만 담당한다.
- paper_bot이 실행·장부·알림을 담당한다.

고정 본선:
시장 전체 bulk 스캔(ALL_KRW) -> 표준값 생성 -> 거래대금 눌림 재돌파 판단 ->
paper_candidates.jsonl / shadow_candidates.jsonl 출력 -> 상태판.

절대 금지:
자동매수 ON, BUY_READY 강제 생성, v343 복구, 내부 paper 실행, candidate_events 소비.
"""
from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
import traceback
import urllib.parse
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import pybithumb  # v157: REST bulk가 막힐 때 전체현재가 fallback용. API KEY 불필요.
except Exception:
    pybithumb = None

try:
    from telegram import Bot, BotCommand
    from telegram.ext import Updater, CommandHandler
except Exception:  # py_compile 단계에서는 실제 import 성공이 필수는 아님
    Bot = None
    BotCommand = None
    Updater = None
    CommandHandler = None

BOT_VERSION = "수익형 v2.13.166"
# HTTP 헤더는 latin-1만 안전하다. BOT_VERSION은 한글이라 User-Agent로 쓰면
# UnicodeEncodeError가 나며 bulk 스캔이 시작 즉시 0으로 죽는다.
HTTP_USER_AGENT = "coinbot-v2.13.166-mainline"
STRATEGY_NAME = "거래대금 눌림 재돌파"
STRATEGY_KEY = "money_pullback_rebreakout_clean"
BASE_DIR = Path(__file__).resolve().parent
TIMEZONE_LABEL = "Asia/Seoul"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# 스캔을 줄이지 않는다: 매 cycle ALL_KRW 전체를 가져와 451개 안팎의 가격/24h 거래대금을 본다.
SCAN_INTERVAL_SEC = float(os.getenv("CLEAN_SCAN_INTERVAL_SEC", "7"))
ALL_MARKET_TIMEOUT_SEC = float(os.getenv("CLEAN_ALL_MARKET_TIMEOUT_SEC", "3.0"))
CANDLE_TIMEOUT_SEC = float(os.getenv("CLEAN_CANDLE_TIMEOUT_SEC", "2.2"))
PRECISION_TTL_SEC = float(os.getenv("CLEAN_PRECISION_TTL_SEC", "45"))
# v157: 스캔/판단을 작은 고정 갯수로 자르지 않는다.
# 아래 값들은 전략 조건이 아니라 API 폭주를 막는 안전장치이며, 장이 활발하면 자동으로 더 넓게 본다.
PRECISION_REFRESH_MIN = int(os.getenv("CLEAN_PRECISION_REFRESH_MIN", "28"))
PRECISION_REFRESH_BASE = int(os.getenv("CLEAN_PRECISION_REFRESH_BASE", "36"))
PRECISION_REFRESH_MAX_SAFETY = int(os.getenv("CLEAN_PRECISION_REFRESH_MAX_SAFETY", "96"))
PRECISION_WORKERS = int(os.getenv("CLEAN_PRECISION_WORKERS", "6"))
PRECISION_TURNOVER_CORE_RANK = int(os.getenv("CLEAN_PRECISION_TURNOVER_CORE_RANK", "160"))
PRECISION_MOVE_TRIGGER_PCT = float(os.getenv("CLEAN_PRECISION_MOVE_TRIGGER_PCT", "1.20"))
PRECISION_POSITIVE_MOVE_PCT = float(os.getenv("CLEAN_PRECISION_POSITIVE_MOVE_PCT", "0.60"))
PRECISION_ROTATE_BASE = int(os.getenv("CLEAN_PRECISION_ROTATE_BASE", "70"))
# v162: 무거운 정밀 계산은 메인 스캔을 붙잡지 않게 분리한다.
# 아래 값은 후보를 자르는 조건이 아니라, 메인 스캔이 직접 기다릴 최대 작업량/시간을 제한하는 서버 보호장치다.
PRECISION_SYNC_MAX_PER_SCAN = int(os.getenv("CLEAN_PRECISION_SYNC_MAX_PER_SCAN", "4"))
PRECISION_SYNC_COLD_MAX_PER_SCAN = int(os.getenv("CLEAN_PRECISION_SYNC_COLD_MAX_PER_SCAN", "8"))
PRECISION_QUEUE_MAX = int(os.getenv("CLEAN_PRECISION_QUEUE_MAX", "900"))
PRECISION_BACKGROUND_WORKERS = int(os.getenv("CLEAN_PRECISION_BACKGROUND_WORKERS", "4"))
EXEC_RISK_TTL_SEC = float(os.getenv("CLEAN_EXEC_RISK_TTL_SEC", "45"))
EXEC_RISK_BACKGROUND_WORKERS = int(os.getenv("CLEAN_EXEC_RISK_BACKGROUND_WORKERS", "2"))
EXEC_RISK_QUEUE_MAX = int(os.getenv("CLEAN_EXEC_RISK_QUEUE_MAX", "500"))
# v162: 웹소켓은 허브 보조직원이다. 전략판단/후보파일 쓰기는 하지 않고, 실시간 가격/체결 캐시만 갱신한다.
WS_HUB_ON = str(os.getenv("CLEAN_WS_HUB_ON", "0")).strip().lower() not in {"0", "false", "no", "off"}
WS_HUB_MAX_TICKERS = int(os.getenv("CLEAN_WS_HUB_MAX_TICKERS", "80"))
WS_HUB_STALE_SEC = float(os.getenv("CLEAN_WS_HUB_STALE_SEC", "12"))
WS_HUB_RESTART_SEC = float(os.getenv("CLEAN_WS_HUB_RESTART_SEC", "20"))
# 후보판단 결과는 strict/shadow 상위 N개로 자르지 않는다.
# paper_bot 소비/실제 자동매매 위험관리는 다음 단계에서 별도 담당한다.
CANDIDATE_DISPLAY_LIMIT = int(os.getenv("CLEAN_CANDIDATE_DISPLAY_LIMIT", "8"))
# v150/v139 단일전략 흐름 기준값. 조건을 새로 조이지 않고 대수술 전 v150 수치를 그대로 옮긴다.
MIN_STRICT_SCORE = float(os.getenv("CLEAN_MIN_STRICT_SCORE", "1.85"))
# v166: 후보 분석은 전부 유지하되, 실제 paper OPEN은 자동매매 검증급(trade_ready)만 연다.
# 이 값들은 전략 통과 조건이 아니라 paper OPEN 선별용 실전 검증 기준이다.
TRADE_READY_MIN_SCORE = float(os.getenv("CLEAN_TRADE_READY_MIN_SCORE", "4.45"))
TRADE_READY_MAX_PRICE_RECHECK_PCT = float(os.getenv("CLEAN_TRADE_READY_MAX_PRICE_RECHECK_PCT", "0.50"))
TRADE_READY_MAX_SPREAD_PCT = float(os.getenv("CLEAN_TRADE_READY_MAX_SPREAD_PCT", "0.25"))
TRADE_READY_MAX_TICK_PCT = float(os.getenv("CLEAN_TRADE_READY_MAX_TICK_PCT", "0.35"))
FRESH_OK_SEC = float(os.getenv("CLEAN_FRESH_OK_SEC", "15"))
FRESH_WEAK_SEC = float(os.getenv("CLEAN_FRESH_WEAK_SEC", "35"))
STALE_BLOCK_SEC = float(os.getenv("CLEAN_STALE_BLOCK_SEC", "75"))
EVENT_DEDUP_SEC = int(os.getenv("CLEAN_EVENT_DEDUP_SEC", "150"))
CANDIDATE_FILE_KEEP_LINES = int(os.getenv("CLEAN_CANDIDATE_FILE_KEEP_LINES", "12000"))
CANDIDATE_COMPACT_INTERVAL_SEC = int(os.getenv("CLEAN_CANDIDATE_COMPACT_INTERVAL_SEC", "300"))
CANDIDATE_READ_MAX_LINES = int(os.getenv("CLEAN_CANDIDATE_READ_MAX_LINES", "500"))
CANDIDATE_TTL_SEC = float(os.getenv("CLEAN_CANDIDATE_TTL_SEC", "120"))

FILES = {
    "paper": BASE_DIR / "paper_candidates.jsonl",
    "shadow": BASE_DIR / "shadow_candidates.jsonl",
    "paper_latest": BASE_DIR / "paper_candidates_latest.jsonl",
    "shadow_latest": BASE_DIR / "shadow_candidates_latest.jsonl",
    "status": BASE_DIR / "clean_brain_status.json",
    "error": BASE_DIR / "clean_brain_error.log",
    "runtime": BASE_DIR / "clean_brain_runtime.log",
    "cache": BASE_DIR / "clean_market_cache.json",
    "reject": BASE_DIR / "clean_reject_summary.json",
    "paper_open": BASE_DIR / "paper_bot_open.json",
    "paper_closed": BASE_DIR / "paper_bot_closed.jsonl",
    "paper_status": BASE_DIR / "paper_bot_status.json",
    "paper_control": BASE_DIR / "paper_bot_control.json",
    "baseline": BASE_DIR / "paper_eval_baseline_v166.json",
    "ws_cache": BASE_DIR / "clean_ws_live_cache.json",
    "paper_flag": BASE_DIR / "external_paper_bot_on.flag",
    "legacy_paper_flag": BASE_DIR / "external_paper_runner_on.flag",
}

STABLE_EXCLUDED = {"USDC", "USDT", "BUSD", "USDP", "DAI", "TUSD", "FDUSD", "USDS", "USD1", "PYUSD", "USDE", "RLUSD"}

ARCHITECTURE_ROLES = {
    "hub": "허브: ALL_KRW 전체 원자료/가격/24h 거래대금/rank/캐시 수집 + 웹소켓 보조직원 실시간 가격 캐시",
    "worker1": "1차 직원: 451개 안팎 전체 bulk 표준화, 기본 제외, 신선도, 정밀대상 선정",
    "worker2": "2차 직원: 상위·급변·순환 정밀값 보강, v150 수치 기준 눌림 재돌파 판단, 차단 이유 산출",
    "factory": "공장: paper_candidates.jsonl / shadow_candidates.jsonl만 안전 저장, candidate_events·BUY_READY·내부paper 없음",
    "paper_bot": "paper_bot: 후보파일 소비, OPEN/CLOSED 장부, 알림 담당. 전략판단 없음",
}

_state_lock = threading.RLock()
_stop_event = threading.Event()
_precision_lock = threading.RLock()
_precision_cache: Dict[str, Dict[str, Any]] = {}
_precision_cursor = 0
_precision_queue: "queue.PriorityQueue[Tuple[float, str, Dict[str, Any]]]" = queue.PriorityQueue()
_precision_queued: set[str] = set()
_execution_risk_lock = threading.RLock()
_execution_risk_cache: Dict[str, Dict[str, Any]] = {}
_execution_risk_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_execution_risk_queued: set[str] = set()
_background_workers_started = False
_ws_lock = threading.RLock()
_ws_live_cache: Dict[str, Dict[str, Any]] = {}
_ws_targets: List[str] = []
_ws_last_error = ""
_ws_last_msg_ts = 0.0
_ws_worker_started = False
_seen_events: Dict[str, float] = {}
_last_compact_ts: Dict[str, float] = {}
_recent_strict = deque(maxlen=12)
_recent_shadow = deque(maxlen=12)
_recent_errors = deque(maxlen=20)

STATE: Dict[str, Any] = {
    "version": BOT_VERSION,
    "strategy": STRATEGY_NAME,
    "started_at": time.time(),
    "scan_calls": 0,
    "scan_seq": 0,
    "scan_id": "",
    "scan_last_sec": 0.0,
    "scan_max_sec": 0.0,
    "scan_last_ts": 0.0,
    "scan_last_stage": "boot",
    "scan_last_error": "",
    "bulk_rows": 0,
    "bulk_price": 0,
    "bulk_money": 0,
    "bulk_source": "-",
    "bulk_fetch_error": "",
    "precision_have": 0,
    "precision_refreshed": 0,
    "precision_failed": 0,
    "precision_selected": 0,
    "precision_need": 0,
    "precision_budget": 0,
    "precision_target_note": "",
    "precision_queue_size": 0,
    "precision_sync_limit": 0,
    "precision_background_note": "대기",
    "execution_risk_queue_size": 0,
    "execution_risk_cached": 0,
    "ws_enabled": WS_HUB_ON,
    "ws_state": "대기",
    "ws_targets": 0,
    "ws_cached": 0,
    "ws_fresh": 0,
    "ws_last_age_sec": -1,
    "ws_last_error": "",
    "field_coverage": {},
    "strict_decision": 0,
    "shadow_decision": 0,
    "paper_written": 0,
    "shadow_written": 0,
    "dup_skip": 0,
    "write_error": "",
    "reject_counts": {},
    "reject_examples": [],
    "last_ticker": "-",
    "last_rows_sample": [],
    "compat_commands": [],
    "phase_note": "v163: 비상 안정화. 웹소켓은 코드 유지/기본 OFF, 메인 스캔 직접 정밀계산은 최소화하고 백그라운드 직원 캐시를 조립한다.",
}


def now_ts() -> float:
    return time.time()


def now_text(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts or now_ts()).strftime("%Y-%m-%d %H:%M:%S")


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, str):
            v = v.replace(",", "").replace("%", "").strip()
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def fint(v: Any, default: int = 0) -> int:
    try:
        return int(fnum(v, default))
    except Exception:
        return default


def fmt_price(x: Any) -> str:
    v = fnum(x, 0.0)
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 100:
        return f"{v:,.1f}"
    if v >= 1:
        return f"{v:,.3f}"
    if v >= 0.01:
        return f"{v:,.5f}"
    return f"{v:,.8f}"


def fmt_pct(x: Any) -> str:
    return f"{fnum(x, 0.0):+.2f}%"


def krw_m(v: Any) -> str:
    return f"{fnum(v, 0.0)/1_000_000:.1f}백만"


def append_text(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(str(text).rstrip("\n") + "\n")
    except Exception:
        pass


def log(msg: str) -> None:
    append_text(FILES["runtime"], f"[{now_text()}] {msg}")


def log_error(where: str, exc: BaseException) -> None:
    msg = f"{where}: {exc.__class__.__name__}: {exc}"
    _recent_errors.append(msg[:300])
    try:
        append_text(FILES["error"], f"[{now_text()}] {msg}")
        append_text(FILES["error"], traceback.format_exc())
    except Exception:
        pass


def load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log_error(f"save_json:{path.name}", exc)


def tail_jsonl(path: Path, max_lines: int = 3000) -> List[Dict[str, Any]]:
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
    except Exception:
        return []


def line_count(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return -1




def ensure_eval_baseline() -> Dict[str, Any]:
    """기존 OPEN/CLOSED를 삭제하지 않고, 이 파일 이후 성과만 보기 위한 기준점."""
    data = load_json(FILES["baseline"], {})
    if isinstance(data, dict) and fnum(data.get("baseline_ts"), 0) > 0:
        return data
    data = {
        "schema": "paper_eval_baseline_v166",
        "created_by": BOT_VERSION,
        "baseline_ts": now_ts(),
        "baseline_text": now_text(),
        "closed_lines_at_baseline": line_count(FILES["paper_closed"]),
        "paper_lines_at_baseline": line_count(FILES["paper"]),
        "shadow_lines_at_baseline": line_count(FILES["shadow"]),
        "note": "기존 기록은 삭제하지 않고, 이 시각 이후 신규 OPEN/CLOSED만 별도 집계한다.",
    }
    save_json(FILES["baseline"], data)
    return data


def baseline_ts() -> float:
    return fnum(ensure_eval_baseline().get("baseline_ts"), 0)


def row_ts(row: Dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = fnum((row or {}).get(k), 0)
        if v > 0:
            return v
    return 0.0


def rows_since_baseline(rows: Iterable[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    bts = baseline_ts()
    return [r for r in rows or [] if row_ts(r, *keys) >= bts]


def open_split_since_baseline() -> Tuple[int, int, Dict[str, int], Dict[str, int]]:
    bts = baseline_ts()
    obj = read_open() if 'read_open' in globals() else {}
    new_counts = {"strict": 0, "shadow": 0}
    old_counts = {"strict": 0, "shadow": 0}
    for p in (obj or {}).values():
        lane = str((p or {}).get("lane") or "shadow")
        target = new_counts if fnum((p or {}).get("opened_at"), 0) >= bts else old_counts
        target[lane] = target.get(lane, 0) + 1
    return sum(new_counts.values()), sum(old_counts.values()), new_counts, old_counts


def candidate_file_fresh_stats(path: Path) -> Dict[str, int]:
    nowv = now_ts()
    rows = tail_jsonl(path, max_lines=CANDIDATE_READ_MAX_LINES)
    fresh = expired = no_ts = 0
    for r in rows:
        exp = fnum(r.get("expires_at"), 0)
        created = fnum(r.get("created_at"), 0)
        if exp > 0:
            if exp >= nowv:
                fresh += 1
            else:
                expired += 1
        elif created > 0:
            if nowv - created <= CANDIDATE_TTL_SEC:
                fresh += 1
            else:
                expired += 1
        else:
            no_ts += 1
    return {"read_window": len(rows), "fresh": fresh, "expired": expired, "no_ts": no_ts}

def ensure_candidate_file(path: Path) -> Tuple[bool, str]:
    """권한이 꼬인 후보파일은 본선 파일이므로 공격적으로 새 파일로 교체한다.
    기존 파일은 가능하면 .blocked_perm_시각 으로 보존한다. OPEN/CLOSED/trade_log는 건드리지 않는다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write("")
            return True, "ok"
        except PermissionError:
            pass
        except Exception as exc:
            return False, f"{exc.__class__.__name__}: {exc}"
        # append가 막히면 chmod -> 교체 순서로 시도한다.
        try:
            os.chmod(path, 0o664)
            with path.open("a", encoding="utf-8") as f:
                f.write("")
            return True, "chmod_fixed"
        except Exception:
            pass
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(path.name + f".blocked_perm_{stamp}")
        try:
            os.replace(path, backup)
        except Exception:
            # rename도 안 되면 directory에 새 tmp를 만들고 replace를 시도한다.
            try:
                path.unlink(missing_ok=True)
            except Exception:
                return False, "permission_denied_and_cannot_replace"
        path.write_text("", encoding="utf-8")
        os.chmod(path, 0o664)
        return True, f"recreated_old={backup.name}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def compact_candidate_file(path: Path, keep_lines: int = CANDIDATE_FILE_KEEP_LINES) -> None:
    """후보파일 압축은 무거운 작업이라 매 scan마다 하지 않는다.
    v166: 파일 보존은 유지하되, 공장 단계가 5초 이상 막히지 않도록 시간 간격을 둔다.
    """
    try:
        if not path.exists() or keep_lines <= 0:
            return
        ts = now_ts()
        key = str(path)
        if ts - float(_last_compact_ts.get(key, 0.0)) < CANDIDATE_COMPACT_INTERVAL_SEC:
            return
        _last_compact_ts[key] = ts
        cnt = line_count(path)
        if cnt <= keep_lines + 1000:
            return
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-keep_lines:]
        tmp = path.with_suffix(path.suffix + ".compact_tmp")
        tmp.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log_error(f"compact:{path.name}", exc)


def append_jsonl(path: Path, obj: Dict[str, Any]) -> Tuple[bool, str]:
    ok, note = ensure_candidate_file(path)
    if not ok:
        return False, note
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
        return True, note
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def append_jsonl_many(path: Path, rows: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """여러 후보를 한 번에 append한다.
    v166: 공장 단계 병목 원인이던 후보별 ensure/open/write 반복을 제거한다.
    """
    if not rows:
        return True, "empty"
    ok, note = ensure_candidate_file(path)
    if not ok:
        return False, note
    try:
        body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for row in rows)
        with path.open("a", encoding="utf-8") as f:
            f.write(body)
        return True, note
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def write_jsonl_replace(path: Path, rows: List[Dict[str, Any]]) -> Tuple[bool, str]:
    """최신 scan 묶음 소비용 파일을 작게 유지한다.
    기록 archive(paper_candidates.jsonl)는 보존하되, paper_bot은 *_latest 파일을 우선 소비한다.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str) + "\n" for row in (rows or []))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
        return True, "ok"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def http_json(url: str, timeout: float = 3.0) -> Any:
    """공용 HTTP JSON 호출.

    v153 장애 원인: 한글 BOT_VERSION을 User-Agent에 넣어 urllib가 HTTP 헤더 인코딩에서
    UnicodeEncodeError를 냈고, 그래서 ALL_KRW bulk가 네트워크 요청 전 0으로 죽었다.
    v157도 User-Agent는 ASCII 고정값만 쓴다.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": HTTP_USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _ticker_from_raw_key(raw_key: Any) -> str:
    t = str(raw_key or "").upper().strip()
    if not t or t in {"DATE", "STATUS"}:
        return ""
    t = t.replace("/", "-").replace("_", "-")
    parts = [x for x in t.split("-") if x]
    if len(parts) >= 2:
        non_krw = [x for x in parts if x != "KRW"]
        t = non_krw[-1] if non_krw else parts[-1]
    if t.endswith("KRW") and len(t) > 3:
        t = t[:-3]
    return t.strip().upper()


def _price_from_bulk_row(row: Any) -> float:
    if isinstance(row, dict):
        for k in ("closing_price", "trade_price", "close", "current_price", "price", "prev_closing_price"):
            v = fnum(row.get(k), 0)
            if v > 0:
                return v
    return fnum(row, 0)


def _turnover_from_bulk_row(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    for k in ("acc_trade_value_24H", "acc_trade_value_24h", "acc_trade_value", "acc_trade_price_24h", "acc_trade_price_24H", "trade_value", "volume_krw_24h", "turnover_24h"):
        v = fnum(row.get(k), 0)
        if v > 0:
            return v
    return 0.0


def _volume_from_bulk_row(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    for k in ("units_traded_24H", "units_traded_24h", "units_traded", "volume"):
        v = fnum(row.get(k), 0)
        if v > 0:
            return v
    return 0.0


def _change_from_bulk_row(row: Any) -> float:
    if not isinstance(row, dict):
        return 0.0
    for k in ("fluctate_rate_24H", "fluctate_rate_24h", "fluctate_rate", "change_rate", "signed_change_rate"):
        v = fnum(row.get(k), 0)
        if v != 0:
            return v
    return 0.0


def _rows_from_bulk_payload(payload: Any, source: str) -> List[Dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(data, dict):
        return []
    rows: List[Dict[str, Any]] = []
    ts = now_ts()
    for raw_key, raw_row in data.items():
        t = _ticker_from_raw_key(raw_key)
        if not t or t in STABLE_EXCLUDED:
            continue
        price = _price_from_bulk_row(raw_row)
        if price <= 0:
            continue
        value24 = _turnover_from_bulk_row(raw_row)
        volume24 = _volume_from_bulk_row(raw_row)
        chg24 = _change_from_bulk_row(raw_row)
        rows.append({
            "ticker": t,
            "current_price": price,
            "price": price,
            "turnover_24h": value24,
            "money_proxy_24h": value24,
            "volume_24h": volume24,
            "change_24h": chg24,
            "money_source": "proxy_24h" if value24 > 0 else "missing",
            "source": source,
            "fresh_ts": ts,
        })
    rows.sort(key=lambda x: (fnum(x.get("turnover_24h"), 0), abs(fnum(x.get("change_24h"), 0))), reverse=True)
    for i, r in enumerate(rows, start=1):
        r["turnover_rank"] = i
    return rows




def ws_symbol(ticker: Any) -> str:
    t = str(ticker or "").upper().strip().replace("KRW-", "").replace("_KRW", "").replace("/KRW", "")
    return f"{t}_KRW" if t else ""


def ws_plain_ticker(symbol: Any) -> str:
    s = str(symbol or "").upper().strip().replace("KRW-", "").replace("/KRW", "").replace("_KRW", "")
    return s


def update_ws_targets(rows: List[Dict[str, Any]]) -> None:
    """허브 보조직원이 볼 실시간 대상만 갱신한다. 후보판단을 자르는 제한이 아니다."""
    try:
        ranked = sorted(rows or [], key=lambda r: (fnum(r.get("turnover_rank"), 9999), -abs(fnum(r.get("change_24h"), 0))))
        ticks = []
        for r in ranked:
            t = str(r.get("ticker") or "").upper().strip()
            if not t or t in STABLE_EXCLUDED:
                continue
            ticks.append(t)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break
        with _ws_lock:
            _ws_targets[:] = ticks
            STATE["ws_targets"] = len(ticks)
    except Exception as exc:
        log_error("update_ws_targets", exc)


def parse_ws_price_payload(data: Any) -> Tuple[str, float, Dict[str, Any]]:
    """pybithumb WebSocketManager/빗썸 ticker 응답을 느슨하게 파싱한다."""
    if not isinstance(data, dict):
        return "", 0.0, {}
    row = data.get("content") if isinstance(data.get("content"), dict) else data
    ticker = ws_plain_ticker(row.get("symbol") or row.get("code") or row.get("market") or row.get("ticker") or row.get("symbolTicker") or "")
    price = first_number(row, ["closePrice", "close_price", "trade_price", "price", "closing_price", "current_price"])
    extra = {
        "raw_type": data.get("type") or row.get("type") or "ticker",
        "change_rate": first_number(row, ["chgRate", "change_rate", "signed_change_rate"], 0.0),
        "volume_power": first_number(row, ["volumePower", "volume_power"], 0.0),
    }
    return ticker, price, extra


def websocket_hub_worker_loop() -> None:
    """웹소켓 허브 보조직원. 전략판단/파일쓰기 없이 실시간 가격 캐시만 갱신한다."""
    global _ws_last_error, _ws_last_msg_ts
    if not WS_HUB_ON:
        with _state_lock:
            STATE["ws_state"] = "OFF"
        return
    if pybithumb is None or not hasattr(pybithumb, "WebSocketManager"):
        with _state_lock:
            STATE["ws_state"] = "불가"
            STATE["ws_last_error"] = "pybithumb.WebSocketManager 없음"
        return
    log("websocket_hub_worker started")
    last_target_key = ""
    wm = None
    while not _stop_event.is_set():
        try:
            with _ws_lock:
                targets = list(_ws_targets)[:max(1, WS_HUB_MAX_TICKERS)]
            if not targets:
                with _state_lock:
                    STATE["ws_state"] = "대상대기"
                _stop_event.wait(2.0)
                continue
            symbols = [ws_symbol(t) for t in targets if ws_symbol(t)]
            key = ",".join(symbols)
            if key != last_target_key:
                try:
                    if wm is not None and hasattr(wm, "terminate"):
                        wm.terminate()
                except Exception:
                    pass
                wm = pybithumb.WebSocketManager("ticker", symbols)
                last_target_key = key
                with _state_lock:
                    STATE["ws_state"] = "연결"
                    STATE["ws_targets"] = len(symbols)
                log(f"websocket_hub subscribed {len(symbols)}")
            data = wm.get()
            ticker, price, extra = parse_ws_price_payload(data)
            if ticker and price > 0:
                ts = now_ts()
                with _ws_lock:
                    _ws_live_cache[ticker] = {"ticker": ticker, "live_price": price, "ts": ts, **extra}
                    _ws_last_msg_ts = ts
                    STATE["ws_cached"] = len(_ws_live_cache)
                    STATE["ws_fresh"] = sum(1 for v in _ws_live_cache.values() if ts - fnum(v.get("ts"), 0) <= WS_HUB_STALE_SEC)
                    STATE["ws_last_age_sec"] = 0
                    STATE["ws_state"] = "수신중"
                    STATE["ws_last_error"] = ""
        except Exception as exc:
            _ws_last_error = f"{exc.__class__.__name__}: {str(exc)[:120]}"
            with _state_lock:
                age = now_ts() - _ws_last_msg_ts if _ws_last_msg_ts > 0 else -1
                STATE["ws_state"] = "재연결대기"
                STATE["ws_last_error"] = _ws_last_error
                STATE["ws_last_age_sec"] = round(age, 1) if age >= 0 else -1
            log_error("websocket_hub_worker", exc)
            try:
                if wm is not None and hasattr(wm, "terminate"):
                    wm.terminate()
            except Exception:
                pass
            wm = None
            last_target_key = ""
            _stop_event.wait(WS_HUB_RESTART_SEC)
    try:
        if wm is not None and hasattr(wm, "terminate"):
            wm.terminate()
    except Exception:
        pass
    log("websocket_hub_worker stopped")


def ws_snapshot(ticker: Any) -> Dict[str, Any]:
    t = str(ticker or "").upper().strip()
    nowv = now_ts()
    with _ws_lock:
        row = dict(_ws_live_cache.get(t) or {})
    if not row:
        return {"live_price_source": "REST", "live_price": 0.0, "live_age_sec": -1, "ws_fresh": False}
    age = nowv - fnum(row.get("ts"), nowv)
    return {
        "live_price_source": "WS" if age <= WS_HUB_STALE_SEC else "WS_STALE",
        "live_price": fnum(row.get("live_price"), 0.0),
        "live_age_sec": round(age, 2),
        "ws_fresh": age <= WS_HUB_STALE_SEC,
    }


def apply_ws_cache_to_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    fresh = 0
    for r in rows or []:
        rr = dict(r)
        ws = ws_snapshot(rr.get("ticker"))
        rr.update(ws)
        if ws.get("ws_fresh") and fnum(ws.get("live_price"), 0) > 0:
            fresh += 1
        out.append(rr)
    with _state_lock:
        STATE["ws_fresh"] = fresh
        if _ws_last_msg_ts > 0:
            STATE["ws_last_age_sec"] = round(now_ts() - _ws_last_msg_ts, 1)
    return out


def websocket_status_line() -> str:
    age = STATE.get("ws_last_age_sec", -1)
    age_txt = f"{age}초" if fnum(age, -1) >= 0 else "-"
    err = STATE.get("ws_last_error") or "-"
    state = str(STATE.get("ws_state") or "-")
    if state == "수신중":
        icon = "✅"
    elif state in {"OFF", "대기", "대상대기", "연결", "재연결대기", "-"}:
        icon = "❔"
    else:
        icon = "⚠️"
    return f"{icon} 웹소켓 허브: {STATE.get('ws_state','-')} / 대상 {STATE.get('ws_targets',0)} / 캐시 {STATE.get('ws_cached',0)} / 신선 {STATE.get('ws_fresh',0)} / 최근 {age_txt} / 오류 {err}"

def _fetch_all_krw_rest() -> List[Dict[str, Any]]:
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    data = http_json(url, timeout=ALL_MARKET_TIMEOUT_SEC)
    if not isinstance(data, dict) or str(data.get("status")) != "0000":
        raise RuntimeError(f"ALL_KRW status={data.get('status') if isinstance(data, dict) else '?'}")
    return _rows_from_bulk_payload(data, "ALL_KRW_rest")


def _fetch_all_krw_pybithumb() -> List[Dict[str, Any]]:
    if pybithumb is None or not callable(getattr(pybithumb, "get_current_price", None)):
        return []
    payload = pybithumb.get_current_price("ALL")
    return _rows_from_bulk_payload(payload, "pybithumb_ALL")


def _fetch_all_krw_from_saved_cache() -> List[Dict[str, Any]]:
    # 실시간 API가 순간 실패할 때만 짧게 쓰는 안전망. 후보 조작용이 아니라 scan 0 고착 방지용이다.
    try:
        cache = load_json(FILES["cache"], {})
        rows = cache.get("rows") if isinstance(cache, dict) else []
        if not isinstance(rows, list):
            return []
        out = []
        ts = now_ts()
        for r in rows:
            if not isinstance(r, dict):
                continue
            t = _ticker_from_raw_key(r.get("ticker"))
            price = fnum(r.get("current_price") or r.get("price"), 0)
            if not t or t in STABLE_EXCLUDED or price <= 0:
                continue
            row = dict(r)
            row.update({"ticker": t, "current_price": price, "price": price, "source": "saved_cache_stale", "fresh_ts": ts})
            out.append(row)
        return out
    except Exception:
        return []


def fetch_all_krw() -> Tuple[List[Dict[str, Any]], str]:
    errors: List[str] = []
    for label, fn in (("ALL_KRW_rest", _fetch_all_krw_rest), ("pybithumb_ALL", _fetch_all_krw_pybithumb), ("saved_cache_stale", _fetch_all_krw_from_saved_cache)):
        try:
            rows = fn()
            if rows:
                with _state_lock:
                    STATE["bulk_source"] = label
                    STATE["bulk_fetch_error"] = " / ".join(errors[-2:])
                return rows, label
            errors.append(f"{label}:empty")
        except Exception as exc:
            errors.append(f"{label}:{exc.__class__.__name__}:{str(exc)[:80]}")
    with _state_lock:
        STATE["bulk_source"] = "failed"
        STATE["bulk_fetch_error"] = " / ".join(errors[-4:])
    raise RuntimeError("bulk_fetch_failed | " + " / ".join(errors[-4:]))


def fetch_candles_public(ticker: str, interval: str = "1m", count: int = 80) -> Optional[List[Dict[str, float]]]:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return None
    # Bithumb public candlestick: data item is commonly [timestamp, open, close, high, low, volume].
    # high/low는 방어적으로 max/min 보정한다.
    urls = [
        f"https://api.bithumb.com/public/candlestick/{urllib.parse.quote(ticker)}_KRW/{interval}",
        f"https://api.bithumb.com/public/candlestick/{urllib.parse.quote(ticker)}/KRW/{interval}",
    ]
    last_exc = None
    for url in urls:
        try:
            data = http_json(url, timeout=CANDLE_TIMEOUT_SEC)
            if not isinstance(data, dict) or str(data.get("status")) != "0000":
                continue
            arr = data.get("data") or []
            out = []
            for item in arr[-max(count, 10):]:
                if not isinstance(item, (list, tuple)) or len(item) < 6:
                    continue
                op = fnum(item[1], 0)
                close = fnum(item[2], 0)
                a = fnum(item[3], 0)
                b = fnum(item[4], 0)
                high = max(op, close, a, b)
                low = min(x for x in [op, close, a, b] if x > 0) if any(x > 0 for x in [op, close, a, b]) else 0
                vol = fnum(item[5], 0)
                if close > 0:
                    out.append({"open": op, "close": close, "high": high, "low": low, "volume": vol})
            return out if len(out) >= 8 else None
        except Exception as exc:
            last_exc = exc
            continue
    if last_exc:
        raise last_exc
    return None


def pct_change_from(closes: List[float], back: int, current: float) -> float:
    try:
        if len(closes) <= back or current <= 0:
            return 0.0
        base = float(closes[-back-1])
        if base <= 0:
            return 0.0
        return ((current - base) / base) * 100.0
    except Exception:
        return 0.0


def _rsi14(closes: List[float]) -> float:
    try:
        if len(closes) < 15:
            return 0.0
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))][-14:]
        gains = sum(x for x in deltas if x > 0) / 14.0
        losses = abs(sum(x for x in deltas if x < 0)) / 14.0
        if losses <= 0:
            return 100.0
        rs = gains / losses
        return round(100.0 - (100.0 / (1.0 + rs)), 2)
    except Exception:
        return 0.0


def build_precision(ticker: str, price: float) -> Dict[str, Any]:
    """v150/v139 흐름의 정밀 재료를 clean 파일에서 새로 계산한다.
    - 새 파일이지만 스캔을 줄이지 않는다.
    - 전체 bulk는 매번 유지하고, OHLCV 정밀값은 상위+급변+순환으로 보강한다.
    - 현재봉 위치, 윗꼬리, RSI, VWAP, MA gap처럼 v150 판단에 쓰던 보조값도 같이 만든다.
    """
    started = now_ts()
    candles = fetch_candles_public(ticker, "1m", count=90) or []
    closes = [fnum(c.get("close"), 0) for c in candles if fnum(c.get("close"), 0) > 0]
    highs = [fnum(c.get("high"), 0) for c in candles if fnum(c.get("high"), 0) > 0]
    lows = [fnum(c.get("low"), 0) for c in candles if fnum(c.get("low"), 0) > 0]
    vols = [fnum(c.get("volume"), 0) for c in candles]
    current = price if price > 0 else (closes[-1] if closes else 0.0)
    if len(closes) < 8 or current <= 0:
        return {"ticker": ticker, "precision_ok": False, "precision_error": "not_enough_candles", "precision_ts": now_ts()}
    v5 = sum(vols[-5:]) if len(vols) >= 5 else sum(vols)
    prev20 = vols[-25:-5] if len(vols) >= 25 else vols[:-5]
    prev_avg = (sum(prev20) / len(prev20)) if prev20 else 0.0
    recent_avg = (sum(vols[-5:]) / min(5, len(vols))) if vols else 0.0
    vol_ratio = (recent_avg / prev_avg) if prev_avg > 0 else 0.0
    low30 = min(lows[-31:]) if lows else 0.0
    high30 = max(highs[-31:]) if highs else 0.0
    turnover_5m = v5 * current

    close_pos = 0.0
    upper_wick = 0.0
    lower_wick = 0.0
    candle_change = 0.0
    try:
        last = candles[-1]
        op = fnum(last.get("open"), 0)
        cl = max(current, fnum(last.get("close"), 0))
        hi = max(fnum(last.get("high"), 0), cl, op)
        lo = fnum(last.get("low"), 0)
        if op > 0 and hi > lo and lo > 0:
            rng = hi - lo
            close_pos = (cl - lo) / rng if rng > 0 else 0.0
            upper_wick = ((hi - max(op, cl)) / max(op, cl)) * 100.0 if hi > max(op, cl) else 0.0
            lower_wick = ((min(op, cl) - lo) / min(op, cl)) * 100.0 if lo < min(op, cl) else 0.0
            candle_change = ((cl - op) / op) * 100.0
    except Exception:
        pass

    ma5_gap = 0.0
    ma20_gap = 0.0
    vwap_gap = 0.0
    try:
        if len(closes) >= 5:
            ma5 = sum(closes[-5:]) / 5.0
            ma5_gap = ((current - ma5) / max(ma5, 1e-9)) * 100.0
        if len(closes) >= 20:
            ma20 = sum(closes[-20:]) / 20.0
            ma20_gap = ((current - ma20) / max(ma20, 1e-9)) * 100.0
            v20 = vols[-20:]
            c20 = closes[-20:]
            if sum(v20) > 0:
                vwap = sum(c * v for c, v in zip(c20, v20)) / sum(v20)
                vwap_gap = ((current - vwap) / max(vwap, 1e-9)) * 100.0
    except Exception:
        pass

    current_code = "CHECK"
    current_label = "현재봉 확인중"
    if upper_wick >= 0.85 and close_pos < 0.42:
        current_code = "UPPER_WICK_DUMP_HARD"
        current_label = "위꼬리 강하게 밀림"
    elif upper_wick >= 0.55 and close_pos < 0.55:
        current_code = "UPPER_WICK_DUMP_SOFT"
        current_label = "위꼬리 밀림 주의"
    elif lower_wick >= 0.18 and close_pos >= 0.58:
        current_code = "LOWER_RECLAIM"
        current_label = "아랫꼬리 회복"
    elif candle_change >= 0.08 and close_pos >= 0.58:
        current_code = "LIVE_UP"
        current_label = "현재봉 살아남"

    return {
        "ticker": ticker,
        "precision_ok": True,
        "precision_ts": now_ts(),
        "precision_elapsed": round(now_ts() - started, 3),
        "candles_1m": len(closes),
        "change_1": round(pct_change_from(closes, 1, current), 3),
        "change_3": round(pct_change_from(closes, 3, current), 3),
        "change_5": round(pct_change_from(closes, 5, current), 3),
        "change_15": round(pct_change_from(closes, 15, current), 3),
        "change_30": round(pct_change_from(closes, 30, current), 3),
        "vol_ratio": round(vol_ratio, 3),
        "turnover_5m": round(turnover_5m, 2),
        "money_flow": round(turnover_5m, 2),
        "money_status": "확인됨" if turnover_5m > 0 else "실제0또는미세",
        "from_30m_low_pct": round(((current - low30) / low30) * 100.0, 3) if low30 > 0 else 0.0,
        "below_30m_high_pct": round(((high30 - current) / high30) * 100.0, 3) if high30 > 0 else 999.0,
        "recent_30m_high_price": high30,
        "recent_30m_low_price": low30,
        "current_close_pos_ratio": round(close_pos, 4),
        "current_upper_wick_pct": round(upper_wick, 3),
        "current_lower_wick_pct": round(lower_wick, 3),
        "current_candle_change_pct": round(candle_change, 3),
        "current_candle_code": current_code,
        "current_candle_label": current_label,
        "rsi_14": _rsi14(closes),
        "vwap_gap_pct": round(vwap_gap, 3),
        "ma5_gap_pct": round(ma5_gap, 3),
        "ma20_gap_pct": round(ma20_gap, 3),
    }


def market_heat_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows or [])
    hot_move = sum(1 for r in rows if abs(fnum(r.get("change_24h"), 0)) >= PRECISION_MOVE_TRIGGER_PCT)
    positive_move = sum(1 for r in rows if fnum(r.get("change_24h"), 0) >= PRECISION_POSITIVE_MOVE_PCT)
    top_core = sum(1 for r in rows if 0 < fint(r.get("rank_best"), 9999) <= PRECISION_TURNOVER_CORE_RANK)
    return {
        "total": total,
        "hot_move": hot_move,
        "positive_move": positive_move,
        "top_core": top_core,
    }


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """1차 직원: 정밀대상 선정.

    v157 원칙:
    - 전체시장 bulk는 항상 전부 본다.
    - 정밀대상은 작은 top N으로 고정하지 않는다.
    - 상위 유동성, 급변, 양수 흐름, 직전 후보, 전체 순환을 합쳐 장이 좋으면 대상이 자연히 늘어난다.
    """
    if not rows:
        return []
    n = len(rows)
    turnover_core = max(PRECISION_TURNOVER_CORE_RANK, int(n * 0.35))
    positive_rank_cut = max(turnover_core, int(n * 0.60))
    by_t: Dict[str, Dict[str, Any]] = {}
    reasons = Counter()

    for r in rows:
        t = str(r.get("ticker") or "").upper()
        if not t:
            continue
        rank = fint(r.get("rank_best"), 9999)
        move = fnum(r.get("change_24h"), 0)
        add_reason = ""
        if 0 < rank <= turnover_core:
            add_reason = "상위유동성"
        elif abs(move) >= PRECISION_MOVE_TRIGGER_PCT:
            add_reason = "급변"
        elif move >= PRECISION_POSITIVE_MOVE_PCT and 0 < rank <= positive_rank_cut:
            add_reason = "양수흐름"
        if add_reason:
            by_t[t] = r
            reasons[add_reason] += 1

    global _precision_cursor
    all_rows = rows[:]
    if all_rows:
        heat = market_heat_stats(rows)
        rotate_n = min(len(all_rows), max(PRECISION_ROTATE_BASE, int(len(all_rows) * 0.18), heat.get("hot_move", 0)))
        start = _precision_cursor % len(all_rows)
        rot = [all_rows[(start + i) % len(all_rows)] for i in range(rotate_n)]
        _precision_cursor = (_precision_cursor + rotate_n) % len(all_rows)
        for r in rot:
            t = str(r.get("ticker") or "").upper()
            if t and t not in by_t:
                by_t[t] = r
                reasons["순환"] += 1

    targets = list(by_t.values())
    targets.sort(key=lambda x: (
        fint(x.get("rank_best"), 9999) <= turnover_core,
        abs(fnum(x.get("change_24h"), 0)),
        fnum(x.get("turnover_24h"), 0),
    ), reverse=True)
    with _state_lock:
        STATE["precision_target_note"] = " / ".join(f"{k} {v}" for k, v in reasons.most_common())
    return targets


def adaptive_precision_budget(rows: List[Dict[str, Any]], targets: List[Dict[str, Any]]) -> int:
    """2차 직원 정밀보강량.

    작은 고정 숫자로 자르는 대신 장세와 대상 수에 따라 늘린다.
    이 값은 전략 조건이 아니라 API/CPU 보호용 안전장치다.
    """
    heat = market_heat_stats(rows)
    hot = int(heat.get("hot_move", 0))
    positive = int(heat.get("positive_move", 0))
    target_count = len(targets or [])
    heat_bonus = min(36, hot // 2 + positive // 5)
    target_bonus = min(24, max(0, target_count - PRECISION_TURNOVER_CORE_RANK) // 8)
    budget = PRECISION_REFRESH_BASE + heat_bonus + target_bonus
    budget = max(PRECISION_REFRESH_MIN, budget)
    budget = min(PRECISION_REFRESH_MAX_SAFETY, budget, target_count if target_count > 0 else budget)
    return int(max(0, budget))

def precision_priority(row: Dict[str, Any]) -> float:
    """작은 top N 제한이 아니라, 작업 순서를 정하는 우선순위다."""
    rank = fint(row.get("rank_best", row.get("turnover_rank", 9999)), 9999)
    move = abs(fnum(row.get("change_24h"), 0))
    turnover = fnum(row.get("turnover_24h"), 0)
    rank_score = max(0.0, 1000.0 - min(rank, 1000))
    return -(rank_score * 3.0 + move * 80.0 + min(turnover / 10_000_000, 200.0))


def is_precision_stale(ticker: str, nowv: Optional[float] = None, ttl: Optional[float] = None) -> bool:
    nowv = nowv or now_ts()
    ttl = ttl or PRECISION_TTL_SEC
    with _precision_lock:
        cached = _precision_cache.get(str(ticker or "").upper()) or {}
    if not cached or not cached.get("precision_ok"):
        return True
    return (nowv - fnum(cached.get("precision_ts"), 0)) > ttl


def store_precision_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
    t = str(row.get("ticker") or "").upper()
    try:
        prof = build_precision(t, fnum(row.get("current_price"), 0))
    except Exception as exc:
        prof = {"ticker": t, "precision_ok": False, "precision_error": f"{exc.__class__.__name__}: {str(exc)[:120]}", "precision_ts": now_ts()}
    with _precision_lock:
        _precision_cache[t] = prof
        _precision_queued.discard(t)
    return prof


def enqueue_precision_targets(targets: List[Dict[str, Any]]) -> int:
    """2차 직원용 백그라운드 작업 등록. 메인 스캔은 이 작업을 전부 기다리지 않는다."""
    added = 0
    nowv = now_ts()
    for row in targets or []:
        t = str(row.get("ticker") or "").upper()
        if not t:
            continue
        if not is_precision_stale(t, nowv=nowv):
            continue
        with _precision_lock:
            if t in _precision_queued:
                continue
            if _precision_queue.qsize() >= PRECISION_QUEUE_MAX:
                break
            _precision_queued.add(t)
        try:
            _precision_queue.put_nowait((precision_priority(row), t, dict(row)))
            added += 1
        except Exception:
            with _precision_lock:
                _precision_queued.discard(t)
    with _state_lock:
        STATE["precision_queue_size"] = _precision_queue.qsize()
        STATE["precision_background_note"] = f"대기열 {STATE.get('precision_queue_size', 0)} / 이번등록 {added}"
    return added


def precision_worker_loop(worker_no: int) -> None:
    log(f"precision_worker_{worker_no} started")
    while not _stop_event.is_set():
        try:
            _prio, ticker, row = _precision_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            store_precision_for_row(row)
        except Exception as exc:
            log_error(f"precision_worker:{ticker}", exc)
        finally:
            try:
                _precision_queue.task_done()
            except Exception:
                pass
            with _state_lock:
                STATE["precision_queue_size"] = _precision_queue.qsize()


def fetch_single_price_public(ticker: str, timeout: float = 1.4) -> float:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return 0.0
    for url in [
        f"https://api.bithumb.com/public/ticker/{urllib.parse.quote(ticker)}_KRW",
        f"https://api.bithumb.com/public/ticker/{urllib.parse.quote(ticker)}/KRW",
    ]:
        try:
            data = http_json(url, timeout=timeout)
            if isinstance(data, dict) and str(data.get("status")) == "0000":
                row = data.get("data") or {}
                return fnum(row.get("closing_price") or row.get("trade_price") or row.get("close"), 0)
        except Exception:
            continue
    return 0.0


def fetch_orderbook_spread_pct(ticker: str, timeout: float = 1.4) -> float:
    ticker = str(ticker or "").upper().strip()
    if not ticker:
        return 999.0
    for url in [
        f"https://api.bithumb.com/public/orderbook/{urllib.parse.quote(ticker)}_KRW?count=5",
        f"https://api.bithumb.com/public/orderbook/{urllib.parse.quote(ticker)}/KRW?count=5",
    ]:
        try:
            data = http_json(url, timeout=timeout)
            if not isinstance(data, dict) or str(data.get("status")) != "0000":
                continue
            book = data.get("data") or {}
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            bid = fnum((bids[0] or {}).get("price") if bids else 0, 0)
            ask = fnum((asks[0] or {}).get("price") if asks else 0, 0)
            mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0
            if mid > 0 and ask >= bid:
                return ((ask - bid) / mid) * 100.0
        except Exception:
            continue
    return 999.0


def approximate_tick_pct(price: float) -> float:
    """빗썸 호가단위는 변할 수 있으므로 보수적 근사만 쓴다. 자동매매 확정 조건이 아니라 위험 표시다."""
    p = fnum(price, 0)
    if p <= 0:
        return 999.0
    if p < 1:
        tick = 0.0001
    elif p < 10:
        tick = 0.001
    elif p < 100:
        tick = 0.01
    elif p < 1000:
        tick = 0.1
    else:
        tick = 1.0
    return (tick / p) * 100.0


def build_execution_risk(row: Dict[str, Any]) -> Dict[str, Any]:
    t = str(row.get("ticker") or "").upper()
    detected = fnum(row.get("entry_price") or row.get("current_price") or row.get("price"), 0)
    live = fetch_single_price_public(t, timeout=1.3) or detected
    price_recheck_pct = ((live - detected) / detected) * 100.0 if detected > 0 and live > 0 else 0.0
    spread = fetch_orderbook_spread_pct(t, timeout=1.3)
    tick_pct = approximate_tick_pct(live or detected)
    flags = []
    if price_recheck_pct >= 0.50:
        flags.append("알림가 대비 급등")
    if spread >= 0.25 and spread < 900:
        flags.append("호가스프레드 넓음")
    if tick_pct >= 0.35 and tick_pct < 900:
        flags.append("틱위험 큼")
    if not flags:
        flags.append("실전위험 보통")
    return {
        "ticker": t,
        "execution_risk_ts": now_ts(),
        "live_price_recheck": live,
        "price_recheck_pct": round(price_recheck_pct, 3),
        "orderbook_spread_pct": round(spread, 3) if spread < 900 else 999.0,
        "tick_pct_est": round(tick_pct, 4) if tick_pct < 900 else 999.0,
        "execution_risk_flags": flags,
        "execution_risk_status": "확인완료" if live > 0 else "확인중",
    }


def execution_risk_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    t = str(row.get("ticker") or "").upper()
    nowv = now_ts()
    with _execution_risk_lock:
        cached = dict(_execution_risk_cache.get(t) or {})
    if cached and nowv - fnum(cached.get("execution_risk_ts"), 0) <= EXEC_RISK_TTL_SEC:
        return cached
    price = fnum(row.get("current_price") or row.get("price"), 0)
    tick_pct = approximate_tick_pct(price)
    return {
        "ticker": t,
        "execution_risk_ts": 0.0,
        "live_price_recheck": price,
        "price_recheck_pct": 0.0,
        "orderbook_spread_pct": 999.0,
        "tick_pct_est": round(tick_pct, 4) if tick_pct < 900 else 999.0,
        "execution_risk_flags": ["실전위험 확인중"],
        "execution_risk_status": "확인중",
    }


def enqueue_execution_risk(items: List[Dict[str, Any]]) -> int:
    added = 0
    for item in items or []:
        t = str(item.get("ticker") or "").upper()
        if not t:
            continue
        with _execution_risk_lock:
            cached = _execution_risk_cache.get(t) or {}
            if cached and now_ts() - fnum(cached.get("execution_risk_ts"), 0) <= EXEC_RISK_TTL_SEC:
                continue
            if t in _execution_risk_queued:
                continue
            if _execution_risk_queue.qsize() >= EXEC_RISK_QUEUE_MAX:
                break
            _execution_risk_queued.add(t)
        try:
            _execution_risk_queue.put_nowait(dict(item))
            added += 1
        except Exception:
            with _execution_risk_lock:
                _execution_risk_queued.discard(t)
    with _state_lock:
        STATE["execution_risk_queue_size"] = _execution_risk_queue.qsize()
        STATE["execution_risk_cached"] = len(_execution_risk_cache)
    return added


def execution_risk_worker_loop(worker_no: int) -> None:
    log(f"execution_risk_worker_{worker_no} started")
    while not _stop_event.is_set():
        try:
            item = _execution_risk_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        t = str(item.get("ticker") or "").upper()
        try:
            risk = build_execution_risk(item)
            with _execution_risk_lock:
                _execution_risk_cache[t] = risk
                _execution_risk_queued.discard(t)
        except Exception as exc:
            with _execution_risk_lock:
                _execution_risk_queued.discard(t)
            log_error(f"execution_risk_worker:{t}", exc)
        finally:
            try:
                _execution_risk_queue.task_done()
            except Exception:
                pass
            with _state_lock:
                STATE["execution_risk_queue_size"] = _execution_risk_queue.qsize()
                STATE["execution_risk_cached"] = len(_execution_risk_cache)


def refresh_precision(rows: List[Dict[str, Any]], targets: Optional[List[Dict[str, Any]]] = None) -> Tuple[int, int, int]:
    """2차 직원 정밀보강.

    v161 원칙:
    - 정밀대상 전체는 백그라운드 직원 큐에 넣는다.
    - 메인 스캔은 아주 급한 일부만 직접 갱신하고 나머지는 이전 캐시를 사용한다.
    - 따라서 기능을 줄이지 않고도 scan 루프가 모든 OHLCV/호가 계산을 기다리지 않는다.
    """
    targets = targets if targets is not None else select_precision_targets(rows)
    adaptive_budget = adaptive_precision_budget(rows, targets)
    queued = enqueue_precision_targets(targets)
    nowv = now_ts()
    with _precision_lock:
        cached_ok_now = sum(1 for v in _precision_cache.values() if v.get("precision_ok") and nowv - fnum(v.get("precision_ts"), 0) <= PRECISION_TTL_SEC * 3)
    sync_limit = PRECISION_SYNC_COLD_MAX_PER_SCAN if cached_ok_now < 60 else PRECISION_SYNC_MAX_PER_SCAN
    sync_limit = max(0, min(sync_limit, adaptive_budget, len(targets or [])))
    urgent: List[Dict[str, Any]] = []
    for r in sorted(targets or [], key=precision_priority):
        t = str(r.get("ticker") or "").upper()
        if not t:
            continue
        if is_precision_stale(t, nowv=nowv):
            urgent.append(r)
        if len(urgent) >= sync_limit:
            break
    with _state_lock:
        STATE["precision_need"] = sum(1 for r in targets or [] if is_precision_stale(str(r.get("ticker") or "").upper(), nowv=nowv))
        STATE["precision_budget"] = adaptive_budget
        STATE["precision_sync_limit"] = sync_limit
        STATE["precision_queue_size"] = _precision_queue.qsize()
        STATE["precision_background_note"] = f"백그라운드 대기 {STATE.get('precision_queue_size', 0)} / 이번등록 {queued} / 직접갱신 {len(urgent)}"
    ok = fail = 0
    if urgent:
        def one(r: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            t = str(r.get("ticker") or "").upper()
            return t, store_precision_for_row(r)
        with ThreadPoolExecutor(max_workers=max(1, PRECISION_WORKERS)) as ex:
            futs = [ex.submit(one, r) for r in urgent]
            for fut in as_completed(futs):
                try:
                    _t, prof = fut.result()
                    if prof.get("precision_ok"):
                        ok += 1
                    else:
                        fail += 1
                except Exception:
                    fail += 1
    with _precision_lock:
        have = len([1 for v in _precision_cache.values() if v.get("precision_ok") and now_ts() - fnum(v.get("precision_ts"), 0) <= PRECISION_TTL_SEC * 3])
    return ok, fail, have


def merge_precision(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    nowv = now_ts()
    with _precision_lock:
        cache = dict(_precision_cache)
    for r in rows:
        t = str(r.get("ticker") or "").upper()
        rr = dict(r)
        prof = cache.get(t)
        if isinstance(prof, dict) and prof.get("precision_ok"):
            rr.update(prof)
            rr["precision_age_sec"] = round(nowv - fnum(prof.get("precision_ts"), nowv), 1)
            rr["precision_source"] = "candlestick_1m"
        else:
            rr.setdefault("turnover_5m", 0.0)
            rr.setdefault("change_5", 0.0)
            rr.setdefault("change_15", 0.0)
            rr.setdefault("change_30", 0.0)
            rr.setdefault("vol_ratio", 0.0)
            rr.setdefault("from_30m_low_pct", 0.0)
            rr.setdefault("below_30m_high_pct", 999.0)
            rr.setdefault("current_close_pos_ratio", 0.0)
            rr.setdefault("current_upper_wick_pct", 0.0)
            rr.setdefault("current_lower_wick_pct", 0.0)
            rr.setdefault("current_candle_change_pct", 0.0)
            rr.setdefault("current_candle_code", "UNKNOWN")
            rr.setdefault("current_candle_label", "현재봉 정보없음")
            rr.setdefault("rsi_14", 0.0)
            rr.setdefault("vwap_gap_pct", 0.0)
            rr.setdefault("ma5_gap_pct", 0.0)
            rr.setdefault("ma20_gap_pct", 0.0)
            rr.setdefault("money_status", "정보없음")
            rr["precision_source"] = "bulk_only"
        out.append(rr)
    return out


def update_field_coverage(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    cov = {
        "rows": len(rows),
        "price": sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0),
        "money": sum(1 for r in rows if fnum(r.get("money_proxy_24h"), 0) > 0),
        "volume": sum(1 for r in rows if fnum(r.get("vol_ratio"), 0) > 0),
        "momentum": sum(1 for r in rows if max(abs(fnum(r.get("change_5"), 0)), abs(fnum(r.get("change_15"), 0)), abs(fnum(r.get("change_30"), 0))) > 0),
        "position": sum(1 for r in rows if fnum(r.get("below_30m_high_pct"), 999) < 900 or fnum(r.get("from_30m_low_pct"), 0) > 0),
        "rank": sum(1 for r in rows if fint(r.get("turnover_rank"), 999) < 999),
        "fresh": len(rows),
        "precision": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m"),
        "bulk_only": sum(1 for r in rows if r.get("precision_source") == "bulk_only"),
        "real_money": 0,
        "proxy_money": sum(1 for r in rows if fnum(r.get("money_proxy_24h"), 0) > 0),
        "missing_money": sum(1 for r in rows if fnum(r.get("money_proxy_24h"), 0) <= 0),
    }
    return cov


def classify_liquidity(turnover_24h: float, turnover_5m: float, price: float) -> Dict[str, Any]:
    """자동매매 준비용 위험 등급. 전략 통과 조건을 바꾸지 않는 보조 필드다."""
    if turnover_24h >= 5_000_000_000 or turnover_5m >= 50_000_000:
        grade = "A"
    elif turnover_24h >= 1_000_000_000 or turnover_5m >= 20_000_000:
        grade = "B"
    elif turnover_24h >= 200_000_000 or turnover_5m >= 5_000_000:
        grade = "C"
    else:
        grade = "D"
    if grade in {"A", "B"}:
        slip = "낮음"
    elif grade == "C":
        slip = "주의"
    else:
        slip = "높음"
    tick_pct = approximate_tick_pct(price)
    if price <= 0:
        tick = "정보없음"
    elif tick_pct >= 0.35:
        tick = "높음"
    elif tick_pct >= 0.12:
        tick = "주의"
    else:
        tick = "보통"
    return {"liquidity_grade": grade, "slippage_risk": slip, "tick_risk": tick, "tick_pct_est": round(tick_pct, 4) if tick_pct < 900 else 999.0}


def build_auto_ready_profile(prof: Dict[str, Any]) -> Dict[str, Any]:
    """real_bot 전환용 후보 상태값. BUY_READY 경로가 아니라 후보파일 schema 필드만 만든다."""
    blocks = list(prof.get("blocks") or [])
    if prof.get("ok") and prof.get("money_ok") and prof.get("move_ok") and prof.get("position_ok"):
        level = "paper_ready"
        label = "모의매매 검증 후보"
    elif prof.get("price", 0) > 0 and prof.get("score", 0) >= 0.75:
        level = "watch_review"
        label = "복기 관찰 후보"
    else:
        level = "blocked"
        label = "자동매매 차단 후보"
    if any("고점 바로" in str(x) or "저점대비 과열" in str(x) for x in blocks):
        chase = "높음"
    elif prof.get("high_gap_pct", 999) < 0.8 and prof.get("from_low_pct", 0) > 2.0:
        chase = "주의"
    else:
        chase = "보통"
    return {
        "auto_ready_level": level,
        "auto_ready_label": label,
        "auto_trade_ready": False,  # 실제 자동매수 신호가 아니다. real_bot 준비용 참고값만 저장한다.
        "chase_risk": chase,
        "block_reason": " / ".join(blocks[:6]) if blocks else "",
        "block_reasons": blocks[:8],
    }


def score_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """v150에서 실제 쓰던 v138/v139 단일전략 점수 흐름을 새 본선에 이식.
    조건 수치를 새로 조정하지 않고, 아래 핵심 문턱은 v150 기준을 그대로 따른다.
    - turnover 20M/5M, vol 1.80/1.15, rank 20/40, leader 0.45
    - ch5 0.35 or ch3 0.22 or ch1 0.12, short_momo 0.05, broad_momo 0.25/0.05
    - high_gap 0.20~4.80, high_gap<0.20 차단, from_low<=4.80, from_low<=6.50+high_gap>=0.80
    - strict score >= 1.85
    보조 위험필드는 자동매매 준비용 schema일 뿐, 전략 통과 수치를 바꾸지 않는다.
    """
    price = fnum(row.get("current_price"), 0)
    ch1 = fnum(row.get("change_1"), 0)
    ch3 = fnum(row.get("change_3"), 0)
    ch5 = fnum(row.get("change_5"), 0)
    ch15 = fnum(row.get("change_15"), 0)
    ch30 = fnum(row.get("change_30"), 0)
    vol = fnum(row.get("vol_ratio"), 0)
    money = fnum(row.get("turnover_5m"), row.get("money_flow"))
    if money <= 0:
        money = fnum(row.get("money_flow"), 0)
    turnover_24h = fnum(row.get("turnover_24h"), 0)
    leader = fnum(row.get("leader_score"), 0)
    edge = fnum(row.get("edge_score"), 0)
    from_low = fnum(row.get("from_30m_low_pct"), 0)
    high_gap = fnum(row.get("below_30m_high_pct"), 999)
    rank_best = fint(row.get("rank_best", row.get("turnover_rank", 999)), 999)
    age = fnum(row.get("precision_age_sec"), 9999 if row.get("precision_source") != "candlestick_1m" else 0)
    close_pos = fnum(row.get("current_close_pos_ratio"), 0)
    upper = fnum(row.get("current_upper_wick_pct"), 0)
    rsi = fnum(row.get("rsi_14"), 0)
    vwap_gap = fnum(row.get("vwap_gap_pct"), 0)
    ma5_gap = fnum(row.get("ma5_gap_pct"), 0)
    current_candle_code = str(row.get("current_candle_code") or "UNKNOWN")
    current_candle_label = str(row.get("current_candle_label") or "현재봉 확인중")
    precision = row.get("precision_source") == "candlestick_1m"

    reasons: List[str] = []
    blocks: List[str] = []
    score = 0.0
    if price <= 0:
        blocks.append("가격 없음")
    if precision and age > STALE_BLOCK_SEC:
        blocks.append(f"정보 오래됨 {age:.0f}s")
    elif precision and age > FRESH_WEAK_SEC:
        # v150 원조건에 직접 없던 신선도 감점은 차단이 아니라 참고 수준으로만 둔다.
        reasons.append(f"정보 약간 오래됨 {age:.0f}s")

    # 1) 돈이 붙었는지: v150/v138 문턱 그대로
    money_ok = False
    if money >= 20_000_000:
        score += 1.25; money_ok = True; reasons.append("거래대금 강함")
    elif money >= 5_000_000:
        score += 0.85; money_ok = True; reasons.append("거래대금 확인")
    if vol >= 1.80:
        score += 1.00; money_ok = True; reasons.append("거래량 증가 강함")
    elif vol >= 1.15:
        score += 0.65; money_ok = True; reasons.append("거래량 증가")
    if rank_best <= 20:
        score += 0.75; money_ok = True; reasons.append(f"시장상위 rank {rank_best}")
    elif rank_best <= 40:
        score += 0.40; money_ok = True; reasons.append(f"rank {rank_best}")
    if leader >= 0.45:
        score += 0.45; money_ok = True; reasons.append("주도점수 확인")
    if not money_ok:
        blocks.append("돈흐름 입력/강도 부족")

    # 2) 다시 살아나는지: v150/v138 문턱 그대로
    short_momo = max(ch1, ch3, ch5)
    broad_momo = max(ch5, ch15, ch30)
    move_ok = False
    if ch5 >= 0.35 or ch3 >= 0.22 or ch1 >= 0.12:
        score += 1.00; move_ok = True; reasons.append("단기 재상승")
    elif short_momo >= 0.05:
        score += 0.55; move_ok = True; reasons.append("단기 양수 전환")
    if broad_momo >= 0.25:
        score += 0.55; move_ok = True; reasons.append("큰 흐름 양수")
    elif broad_momo >= 0.05:
        score += 0.25; move_ok = True; reasons.append("큰 흐름 약양수")
    if not move_ok:
        blocks.append("재상승 신호 부족")

    # 3) 위치: v150/v138 문턱 그대로
    position_ok = True
    if high_gap < 999:
        if 0.20 <= high_gap <= 4.80:
            score += 0.60; reasons.append(f"고점거리 {high_gap:.2f}%")
        elif high_gap < 0.20:
            position_ok = False; blocks.append("고점 바로 붙은 추격")
        elif high_gap <= 7.0:
            score += 0.15; reasons.append("고점거리 넓음")
    else:
        reasons.append("고점거리값 없음-약하게 통과")
    if from_low > 0:
        if from_low <= 4.80:
            score += 0.45; reasons.append(f"저점대비 {from_low:.2f}%")
        elif from_low <= 6.50 and high_gap >= 0.80:
            score += 0.10; reasons.append("저점대비 진행 있지만 여유 있음")
        else:
            position_ok = False; blocks.append("저점대비 과열")
    else:
        reasons.append("저점대비값 없음-약하게 통과")

    # 4) 눌림 후 재돌파 근사: v150/v138 문턱 그대로
    pullback_proxy = False
    if high_gap < 999 and high_gap >= 0.20 and short_momo >= 0.05:
        pullback_proxy = True
    if from_low > 0 and 0.15 <= from_low <= 4.8 and short_momo >= 0.05:
        pullback_proxy = True
    if not pullback_proxy and (money_ok and move_ok and high_gap >= 999):
        pullback_proxy = True
        reasons.append("위치값 부족으로 돈+재상승 우선 실험")

    if edge >= 3.0:
        score += 0.25; reasons.append("기존 점수 보조")

    # 보조값은 자동매매 준비용 참고로만 저장한다. v150 조건 수치를 바꾸는 강제 차단으로 쓰지 않는다.
    aux_notes: List[str] = []
    if current_candle_code and current_candle_code != "UNKNOWN":
        aux_notes.append(current_candle_label)
    if close_pos >= 0.58:
        aux_notes.append("현재봉 종가위치 양호")
    if upper >= 0.75 and close_pos and close_pos < 0.50:
        aux_notes.append("윗꼬리 밀림 주의")
    if rsi > 82:
        aux_notes.append("RSI 과열 주의")
    if abs(vwap_gap) <= 0.65 and vwap_gap != 0:
        aux_notes.append("VWAP 근처")
    if ma5_gap >= -0.20:
        aux_notes.append("MA5 방어")

    ok = bool(price > 0 and money_ok and move_ok and position_ok and pullback_proxy and score >= MIN_STRICT_SCORE)
    if not ok and not blocks:
        blocks.append("점수/구조 부족")
    risk = classify_liquidity(turnover_24h, money, price)
    prof = {
        "ok": ok,
        "score": round(score, 2),
        "reasons": reasons[:8],
        "aux_notes": aux_notes[:8],
        "blocks": blocks[:8],
        "price": price,
        "change_1": ch1,
        "change_3": ch3,
        "change_5": ch5,
        "change_15": ch15,
        "change_30": ch30,
        "vol_ratio": vol,
        "turnover_5m": money,
        "money_flow": money,
        "turnover_24h": turnover_24h,
        "leader_score": leader,
        "edge_score": edge,
        "from_low_pct": from_low,
        "high_gap_pct": high_gap,
        "rank_best": rank_best,
        "data_age_sec": age,
        "freshness": row.get("freshness", "정상" if precision else "bulk_only"),
        "close_pos": close_pos,
        "upper_wick": upper,
        "rsi_14": rsi,
        "vwap_gap_pct": vwap_gap,
        "ma5_gap_pct": ma5_gap,
        "current_candle_code": current_candle_code,
        "current_candle_label": current_candle_label,
        "money_ok": money_ok,
        "money_status": row.get("money_status", "정보없음"),
        "move_ok": move_ok,
        "position_ok": position_ok,
        "pullback_proxy": pullback_proxy,
        "precision": precision,
    }
    prof.update(risk)
    prof.update(build_auto_ready_profile(prof))
    return prof

def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:
    strict: List[Dict[str, Any]] = []
    shadow: List[Dict[str, Any]] = []
    rejects: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    for row in rows:
        t = str(row.get("ticker") or "").upper()
        if not t:
            continue
        prof = score_row(row)
        item = dict(row)
        item.update({
            "ticker": t,
            "strategy": STRATEGY_NAME,
            "strategy_key": STRATEGY_KEY,
            "route": STRATEGY_KEY,
            "score": prof["score"],
            "edge_score": prof["score"],
            "current_price": prof["price"],
            "entry_price": prof["price"],
            "detected_price": prof["price"],
            "change_1": prof["change_1"],
            "change_3": prof["change_3"],
            "change_5": prof["change_5"],
            "change_15": prof["change_15"],
            "change_30": prof["change_30"],
            "vol_ratio": prof["vol_ratio"],
            "turnover_5m": prof["turnover_5m"],
            "turnover": prof["turnover_5m"],
            "turnover_24h": prof["turnover_24h"],
            "from_30m_low_pct": prof["from_low_pct"],
            "below_30m_high_pct": prof["high_gap_pct"],
            "rank_best": prof["rank_best"],
            "money_flow": prof["money_flow"],
            "leader_score": prof["leader_score"],
            "data_age_sec": prof["data_age_sec"],
            "freshness": prof["freshness"],
            "current_close_pos_ratio": prof["close_pos"],
            "current_upper_wick_pct": prof["upper_wick"],
            "rsi_14": prof["rsi_14"],
            "vwap_gap_pct": prof["vwap_gap_pct"],
            "ma5_gap_pct": prof["ma5_gap_pct"],
            "money_status": prof["money_status"],
            "liquidity_grade": prof.get("liquidity_grade", "-"),
            "slippage_risk": prof.get("slippage_risk", "-"),
            "tick_risk": prof.get("tick_risk", "-"),
            "chase_risk": prof.get("chase_risk", "-"),
            "auto_ready_level": prof.get("auto_ready_level", "blocked"),
            "auto_ready_label": prof.get("auto_ready_label", ""),
            "auto_trade_ready": False,
            "block_reason": prof.get("block_reason", ""),
            "block_reasons": prof.get("block_reasons", []),
            "aux_notes": prof.get("aux_notes", []),
            "one_liner": " / ".join(prof["reasons"][:5]) if prof["ok"] else "차단: " + (" / ".join(prof["blocks"][:5]) if prof["blocks"] else "조건 부족"),
            "profile": prof,
        })
        item.update(ws_snapshot(item.get("ticker")))
        exec_risk = execution_risk_snapshot(item)
        item.update(exec_risk)
        flags = list(item.get("execution_risk_flags") or [])
        if exec_risk.get("execution_risk_status") == "확인중":
            item["aux_notes"] = list(item.get("aux_notes") or []) + ["실전위험 확인중"]
        elif any("급등" in str(x) or "스프레드" in str(x) or "틱위험" in str(x) for x in flags):
            item["auto_ready_level"] = "paper_ready_risk_check" if prof["ok"] else item.get("auto_ready_level", "watch_review")
            item["auto_ready_label"] = "실전위험 재확인 필요"
            item["aux_notes"] = list(item.get("aux_notes") or []) + flags[:3]
        if prof["ok"]:
            strict.append(item)
        else:
            reason = prof["blocks"][0] if prof["blocks"] else "조건 부족"
            rejects[reason] += 1
            if len(examples) < 8:
                examples.append({"ticker": t, "reason": reason, "score": prof["score"], "line": item["one_liner"][:160]})
            if prof["price"] > 0 and (prof["score"] >= 0.75 or prof["turnover_5m"] >= 1_000_000 or prof["vol_ratio"] >= 1.0 or prof["rank_best"] <= 50):
                shadow.append(item)
    strict.sort(key=lambda x: (fnum(x.get("score"), 0), fnum(x.get("turnover_5m"), 0), fnum(x.get("change_5"), 0)), reverse=True)
    shadow.sort(key=lambda x: (fnum(x.get("score"), 0), fnum(x.get("turnover_5m"), 0), -fnum(x.get("rank_best"), 999)), reverse=True)
    return strict, shadow, rejects, examples



def decide_trade_ready(item: Dict[str, Any]) -> Tuple[bool, str, List[str]]:
    """strict 후보 중 실제 paper OPEN할 자동매매 검증급만 고른다.

    중요:
    - 전략 조건을 새로 조이는 함수가 아니다.
    - strict_all 후보는 전부 paper_candidates 파일에 남긴다.
    - 여기서는 paper_bot이 OPEN 장부에 태울지 여부만 정한다.
    """
    reasons: List[str] = []
    score = fnum(item.get("score"), 0)
    if score < TRADE_READY_MIN_SCORE:
        reasons.append(f"점수 {score:.2f} < trade_ready {TRADE_READY_MIN_SCORE:.2f}")
    if str(item.get("execution_risk_status") or "") != "확인완료":
        reasons.append("실전위험 확인중")
    price_recheck = fnum(item.get("price_recheck_pct"), 0)
    spread = fnum(item.get("orderbook_spread_pct"), 999)
    tick = fnum(item.get("tick_pct_est"), 999)
    if price_recheck > TRADE_READY_MAX_PRICE_RECHECK_PCT:
        reasons.append(f"알림가 대비 급등 {price_recheck:.2f}%")
    if spread < 900 and spread > TRADE_READY_MAX_SPREAD_PCT:
        reasons.append(f"스프레드 {spread:.2f}%")
    if tick < 900 and tick > TRADE_READY_MAX_TICK_PCT:
        reasons.append(f"틱위험 {tick:.2f}%")
    if str(item.get("chase_risk") or "") == "높음":
        reasons.append("추격위험 높음")
    flags = [str(x) for x in (item.get("execution_risk_flags") or [])]
    for bad in ["알림가 대비 급등", "호가스프레드 넓음", "틱위험 큼"]:
        if any(bad in f for f in flags):
            reasons.append(bad)
    ok = not reasons
    label = "자동매매 검증급 OPEN" if ok else "정식 후보 관찰"
    return ok, label, reasons[:8]

def event_key(row: Dict[str, Any], lane: str, ts: Optional[float] = None) -> str:
    t = str(row.get("ticker") or "UNKNOWN").upper()
    bucket = int((ts or now_ts()) // max(60, EVENT_DEDUP_SEC))
    return f"clean:{lane}:{t}:{bucket}"


def consume_row(item: Dict[str, Any], lane: str, ts: Optional[float] = None, scan_id: str = "") -> Dict[str, Any]:
    ts = ts or now_ts()
    trade_ready, trade_ready_label, trade_ready_reasons = decide_trade_ready(item) if lane == "strict" else (False, "복기 전용", ["shadow는 OPEN하지 않음"])
    eligible = bool(lane == "strict" and trade_ready)
    scan_id = scan_id or str(STATE.get("scan_id") or f"scan-{int(ts)}")
    return {
        "schema": "candidate_consume_v161",
        "source": "brain_v161",
        "scan_id": scan_id,
        "scan_seq": int(STATE.get("scan_seq", 0) or 0),
        "brain_version": BOT_VERSION,
        "created_at": ts,
        "created_at_text": now_text(ts),
        "expires_at": ts + 90,
        "expires_at_text": now_text(ts + 90),
        "event_id": event_key(item, lane, ts),
        "ticker": item.get("ticker"),
        "market": f"KRW-{item.get('ticker')}",
        "lane": lane,
        "eligible_for_paper": eligible,
        "paper_eligible": eligible,
        "decision": "trade_ready" if eligible else ("strict_observe" if lane == "strict" else "shadow_review"),
        "event_type": "trade_ready" if eligible else ("strict_observe" if lane == "strict" else "single_strategy_shadow"),
        "strict_all": bool(lane == "strict"),
        "trade_ready": bool(eligible),
        "trade_ready_label": trade_ready_label,
        "trade_ready_reasons": trade_ready_reasons,
        "observe_only": bool(lane == "strict" and not eligible),
        "strategy": STRATEGY_NAME,
        "strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "score": fnum(item.get("score"), 0),
        "edge_score": fnum(item.get("edge_score"), 0),
        "entry_price": fnum(item.get("entry_price"), item.get("current_price")),
        "detected_price": fnum(item.get("detected_price"), item.get("current_price")),
        "current_price": fnum(item.get("current_price"), 0),
        "price": fnum(item.get("current_price"), 0),
        "change_1": fnum(item.get("change_1"), 0),
        "change_3": fnum(item.get("change_3"), 0),
        "change_5": fnum(item.get("change_5"), 0),
        "change_15": fnum(item.get("change_15"), 0),
        "change_30": fnum(item.get("change_30"), 0),
        "vol_ratio": fnum(item.get("vol_ratio"), 0),
        "turnover_5m": fnum(item.get("turnover_5m"), 0),
        "money_flow": fnum(item.get("money_flow"), item.get("turnover_5m")),
        "turnover_24h": fnum(item.get("turnover_24h"), 0),
        "money_status": item.get("money_status", "정보없음"),
        "money_source": item.get("money_source", "proxy_24h"),
        "liquidity_grade": item.get("liquidity_grade", "-"),
        "slippage_risk": item.get("slippage_risk", "-"),
        "tick_risk": item.get("tick_risk", "-"),
        "chase_risk": item.get("chase_risk", "-"),
        "auto_ready_level": item.get("auto_ready_level", "blocked"),
        "auto_ready_label": item.get("auto_ready_label", ""),
        "auto_trade_ready": False,
        "block_reason": item.get("block_reason", ""),
        "block_reasons": item.get("block_reasons", []),
        "aux_notes": item.get("aux_notes", []),
        "leader_score": fnum(item.get("leader_score"), 0),
        "from_30m_low_pct": fnum(item.get("from_30m_low_pct"), 0),
        "below_30m_high_pct": fnum(item.get("below_30m_high_pct"), 999),
        "rank_best": fint(item.get("rank_best"), 999),
        "data_age_sec": fnum(item.get("data_age_sec"), 0),
        "freshness": item.get("freshness", "-"),
        "current_close_pos_ratio": fnum(item.get("current_close_pos_ratio"), 0),
        "current_upper_wick_pct": fnum(item.get("current_upper_wick_pct"), 0),
        "rsi_14": fnum(item.get("rsi_14"), 0),
        "vwap_gap_pct": fnum(item.get("vwap_gap_pct"), 0),
        "ma5_gap_pct": fnum(item.get("ma5_gap_pct"), 0),
        "current_candle_code": item.get("current_candle_code", "UNKNOWN"),
        "current_candle_label": item.get("current_candle_label", "현재봉 확인중"),
        "live_price_recheck": fnum(item.get("live_price_recheck"), 0),
        "price_recheck_pct": fnum(item.get("price_recheck_pct"), 0),
        "orderbook_spread_pct": fnum(item.get("orderbook_spread_pct"), 999),
        "tick_pct_est": fnum(item.get("tick_pct_est"), 999),
        "execution_risk_status": item.get("execution_risk_status", "확인중"),
        "execution_risk_flags": item.get("execution_risk_flags", []),
        "reason": item.get("one_liner", ""),
        "why": item.get("one_liner", ""),
        "candidate_events_disabled": True,
        "mainline_file": "paper_candidates" if eligible else "shadow_candidates",
        "pipeline_note": "v2.13.166: 후보분석은 strict_all 전부 기록, paper OPEN은 trade_ready만. shadow는 복기전용.",
    }


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    enqueue_execution_risk((strict or []) + (shadow or []))
    result = {
        "paper_attempt": len(strict),
        "shadow_attempt": len(shadow),
        "paper_written": 0,
        "shadow_written": 0,
        "trade_ready_written": 0,
        "strict_observe_written": 0,
        "dup_skip": 0,
        "write_error": "",
        "last_ticker": "-",
        "factory_mode": "v166_batch_append_shadow_review",
    }
    # v166: 압축은 시간 간격을 두고 수행한다. 후보별 파일 open/write 반복 금지.
    compact_candidate_file(FILES["paper"])
    compact_candidate_file(FILES["shadow"])

    for lane, items, path in [("strict", strict, FILES["paper"]), ("shadow", shadow, FILES["shadow"] )]:
        rows: List[Dict[str, Any]] = []
        for item in items:
            row = consume_row(item, lane, ts, scan_id=scan_id)
            if lane == "shadow":
                # shadow는 복기 전용이다. paper_bot 기본값에서는 OPEN하지 않는다.
                row["review_only"] = True
                row["open_eligible"] = False
                row["paper_bot_open"] = False
            else:
                # v166: strict 후보는 전부 기록하지만, paper OPEN은 trade_ready만 허용한다.
                can_open = bool(row.get("trade_ready"))
                row["review_only"] = not can_open
                row["open_eligible"] = can_open
                row["paper_bot_open"] = can_open
            key = str(row.get("event_id"))
            last = fnum(_seen_events.get(key), 0)
            if ts - last < EVENT_DEDUP_SEC:
                result["dup_skip"] += 1
                continue
            _seen_events[key] = ts
            rows.append(row)
        ok, err = append_jsonl_many(path, rows)
        latest_path = FILES["paper_latest"] if lane == "strict" else FILES["shadow_latest"]
        latest_ok, latest_err = write_jsonl_replace(latest_path, rows)
        if not latest_ok and not err:
            err = "latest:" + latest_err
        if ok:
            if lane == "strict":
                result["paper_written"] += len(rows)
                result["trade_ready_written"] += sum(1 for r in rows if r.get("paper_bot_open"))
                result["strict_observe_written"] += sum(1 for r in rows if not r.get("paper_bot_open"))
                for row in rows[-12:]:
                    _recent_strict.append(row)
            else:
                result["shadow_written"] += len(rows)
                for row in rows[-12:]:
                    _recent_shadow.append(row)
            if rows:
                result["last_ticker"] = str(rows[-1].get("ticker") or "-")
        else:
            result["write_error"] = err
    return result


def scan_once() -> List[Dict[str, Any]]:
    started = now_ts()
    ensure_eval_baseline()
    with _state_lock:
        STATE["scan_calls"] = int(STATE.get("scan_calls", 0)) + 1
        STATE["scan_seq"] = int(STATE.get("scan_seq", 0)) + 1
        STATE["scan_id"] = f"scan-{int(started)}-{int(STATE.get('scan_seq', 0))}"
        STATE["scan_last_stage"] = "hub_bulk"
    stage_times: List[Tuple[str, float, str]] = []
    try:
        st = now_ts()
        rows, source = fetch_all_krw()
        update_ws_targets(rows)
        rows = apply_ws_cache_to_rows(rows)
        stage_times.append(("1) 허브: 전체시장 bulk 수집 + 웹소켓 보조", now_ts() - st, f"rows {len(rows)} / {source} / ws {STATE.get('ws_state','-')} fresh {STATE.get('ws_fresh',0)}"))
        with _state_lock:
            STATE["bulk_rows"] = len(rows)
            STATE["bulk_price"] = sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0)
            STATE["bulk_money"] = sum(1 for r in rows if fnum(r.get("turnover_24h"), 0) > 0)
            STATE["scan_last_stage"] = "worker1_select"

        st = now_ts()
        targets = select_precision_targets(rows)
        stage_times.append(("2) 1차 직원: 표준값/신선도/정밀대상 선정", now_ts() - st, f"bulk {len(rows)} / targets {len(targets)} / {STATE.get('precision_target_note', '-')}"))
        with _state_lock:
            STATE["precision_selected"] = len(targets)
            STATE["scan_last_stage"] = "worker2_precision"

        st = now_ts()
        p_ok, p_fail, p_have = refresh_precision(rows, targets)
        stage_times.append(("3) 2차 직원들: 정밀값 비동기 보강", now_ts() - st, f"직접 {p_ok} / 필요 {STATE.get('precision_need', 0)} / 대기열 {STATE.get('precision_queue_size', 0)} / 실전위험대기 {STATE.get('execution_risk_queue_size', 0)} / fail {p_fail} / cached {p_have}"))
        with _state_lock:
            STATE["precision_refreshed"] = p_ok
            STATE["precision_failed"] = p_fail
            STATE["precision_have"] = p_have
            STATE["scan_last_stage"] = "worker1_standardize"

        st = now_ts()
        rows = merge_precision(rows)
        cov = update_field_coverage(rows)
        stage_times.append(("4) 1차 직원: 표준값 병합/커버리지 확인", now_ts() - st, f"precision {cov.get('precision')} / bulk-only {cov.get('bulk_only')}"))
        with _state_lock:
            STATE["field_coverage"] = cov
            STATE["scan_last_stage"] = "worker2_strategy"

        st = now_ts()
        strict, shadow, rejects, examples = build_candidates(rows)
        stage_times.append(("5) 2차 직원: v150 수치 전략판단/차단이유", now_ts() - st, f"strict {len(strict)} / shadow {len(shadow)} / reject {sum(rejects.values())}"))
        with _state_lock:
            STATE["strict_decision"] = len(strict)
            STATE["shadow_decision"] = len(shadow)
            STATE["reject_counts"] = dict(rejects)
            STATE["reject_examples"] = examples
            STATE["last_rows_sample"] = [{"ticker": r.get("ticker"), "score": r.get("score"), "line": r.get("one_liner", "")[:100]} for r in strict[:5]]
            STATE["scan_last_stage"] = "factory_export"

        st = now_ts()
        pipe = export_candidates(strict, shadow)
        stage_times.append(("6) 공장: 후보분석 저장 + trade_ready만 OPEN 허용", now_ts() - st, f"strict_all {pipe['paper_written']}/{pipe['paper_attempt']} / trade_ready {pipe.get('trade_ready_written',0)} / strict관찰 {pipe.get('strict_observe_written',0)} / shadow {pipe['shadow_written']}/{pipe['shadow_attempt']} / dup {pipe['dup_skip']}"))
        total = now_ts() - started
        with _state_lock:
            STATE["paper_written"] = pipe["paper_written"]
            STATE["trade_ready_written"] = pipe.get("trade_ready_written", 0)
            STATE["strict_observe_written"] = pipe.get("strict_observe_written", 0)
            STATE["shadow_written"] = pipe["shadow_written"]
            STATE["dup_skip"] = pipe["dup_skip"]
            STATE["write_error"] = pipe.get("write_error", "")
            STATE["last_ticker"] = pipe.get("last_ticker", "-")
            STATE["scan_last_sec"] = round(total, 3)
            STATE["scan_max_sec"] = max(fnum(STATE.get("scan_max_sec"), 0), round(total, 3))
            STATE["scan_last_ts"] = now_ts()
            STATE["scan_last_stage"] = "done"
            STATE["scan_last_error"] = ""
            STATE["stage_times"] = [(a, round(b, 3), c) for a, b, c in stage_times]
        save_json(FILES["status"], STATE)
        save_json(FILES["cache"], {"updated_at": now_text(), "rows": rows[:80], "coverage": cov})
        save_json(FILES["reject"], {"updated_at": now_text(), "reject_counts": dict(rejects), "examples": examples})
        log(f"scan ok rows={len(rows)} strict={len(strict)} shadow={len(shadow)} sec={total:.2f}")
        return strict
    except Exception as exc:
        total = now_ts() - started
        with _state_lock:
            STATE["scan_last_sec"] = round(total, 3)
            STATE["scan_last_error"] = f"{exc.__class__.__name__}: {str(exc)[:180]}"
            STATE["scan_last_stage"] = "error"
            STATE["scan_last_ts"] = now_ts()
        log_error("scan_once", exc)
        save_json(FILES["status"], STATE)
        return []


def scan_loop() -> None:
    log("scan_loop started")
    while not _stop_event.is_set():
        scan_once()
        _stop_event.wait(SCAN_INTERVAL_SEC)
    log("scan_loop stopped")


def read_paper_status() -> Dict[str, Any]:
    st = load_json(FILES["paper_status"], {})
    ctrl = load_json(FILES["paper_control"], {})
    return {"status": st if isinstance(st, dict) else {}, "control": ctrl if isinstance(ctrl, dict) else {}}


def read_open() -> Dict[str, Any]:
    obj = load_json(FILES["paper_open"], {})
    return obj if isinstance(obj, dict) else {}


def score_stats(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    arr = list(rows or [])
    n = len(arr)
    wins = sum(1 for r in arr if fnum(r.get("pnl_pct"), 0) > 0)
    total = sum(fnum(r.get("pnl_pct"), 0) for r in arr)
    return {"n": n, "wins": wins, "losses": n - wins, "win_rate": (wins / n * 100.0 if n else 0.0), "total": total, "avg": (total / n if n else 0.0)}


def score_icon(stat: Dict[str, Any]) -> str:
    n = int(stat.get("n", 0) or 0)
    avg = fnum(stat.get("avg"), 0)
    if n <= 0:
        return "❔"
    # 표본이 적으면 수익/손실보다 '주의'가 먼저다. 자동매매 판단 금지.
    if n < 50:
        return "⚠️"
    return "✅" if avg > 0 else "❌"


def fmt_stats(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    s = score_stats(rows)
    icon = score_icon(s)
    note = " / 표본부족" if 0 < s["n"] < 50 else ""
    return f"{icon} {label}: {s['n']}전 {s['wins']}승 {s['losses']}패 / 승률 {s['win_rate']:.1f}% / 합산 {s['total']:+.2f}% / 평균 {s['avg']:+.2f}%{note}"




def closed_ts(row: Dict[str, Any]) -> float:
    ts = fnum(row.get("closed_at"), 0)
    if ts > 0:
        return ts
    text = str(row.get("closed_at_text") or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt).timestamp()
        except Exception:
            pass
    return 0.0


def bucket_date(row: Dict[str, Any]) -> str:
    ts = closed_ts(row)
    if ts <= 0:
        return "날짜모름"
    return datetime.fromtimestamp(ts).strftime("%m-%d")


def bucket_hour3(row: Dict[str, Any]) -> str:
    ts = closed_ts(row)
    if ts <= 0:
        return "시간모름"
    dt = datetime.fromtimestamp(ts)
    start = (dt.hour // 3) * 3
    end = (start + 3) % 24
    return f"{start:02d}-{end:02d}시"


def bucket_score(row: Dict[str, Any]) -> str:
    score = fnum(row.get("score"), 0)
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
    # v0.21 이후 closed에는 auto_ready_level이 직접 저장된다.
    # 기존 closed에는 없으므로 raw/decision/score로 최대한 안전하게 참고 표시만 한다.
    val = str(row.get("auto_ready_level") or row.get("auto_ready_label") or "").strip()
    if val:
        return val
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    val = str(raw.get("auto_ready_level") or raw.get("auto_ready_label") or "").strip()
    if val:
        return val
    return bucket_score(row)


def rows_by_key(rows: Iterable[Dict[str, Any]], key_fn, limit: int = 8) -> List[Tuple[str, List[Dict[str, Any]]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        k = str(key_fn(r) or "기타")
        groups.setdefault(k, []).append(r)
    def sort_key(item):
        k, arr = item
        # 날짜/시간은 이름순, 나머지는 표본 많은 순
        if "시" in k or "-" in k:
            return (0, k)
        return (-len(arr), k)
    return sorted(groups.items(), key=sort_key)[:limit]


def group_table(title: str, rows: Iterable[Dict[str, Any]], key_fn, limit: int = 8, min_note: bool = True) -> str:
    rows = list(rows or [])
    lines = [title]
    if not rows:
        lines.append("- 아직 데이터 없음")
        return "\n".join(lines)
    for key, arr in rows_by_key(rows, key_fn, limit=limit):
        st = score_stats(arr)
        icon = "❔" if st["n"] <= 0 else ("⚠️" if st["n"] < 20 and min_note else ("✅" if st["avg"] > 0 else "❌"))
        note = " / 표본적음" if min_note and 0 < st["n"] < 20 else ""
        lines.append(f"{icon} {key}: {st['n']}전 / 승률 {st['win_rate']:.1f}% / 평균 {st['avg']:+.2f}% / 합산 {st['total']:+.2f}%{note}")
    return "\n".join(lines)


def simple_judge_text() -> str:
    return "\n".join([
        "판독",
        "✅ 좋은 쪽: 신규 strict, 특정 시간대, 특정 등급에서 평균이 계속 +면 자동매매 후보로 살림",
        "⚠️ 애매한 쪽: 표본 20건 미만은 참고만",
        "❌ 나쁜 쪽: 반복 손실 시간대/등급은 조건을 조이거나 차단 후보로 보냄",
        "❔ 확인중: 지금은 전략을 새로 만들기보다 어디가 좋은지 나눠 보는 단계",
    ])

def load_closed(limit: int = 20000) -> List[Dict[str, Any]]:
    return tail_jsonl(FILES["paper_closed"], max_lines=limit)


def send_chunks(text: str, limit: int = 3600) -> None:
    if not TELEGRAM_TOKEN or not CHAT_ID or Bot is None:
        print(text)
        return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        body = str(text or "")
        for i in range(0, len(body), limit):
            bot.send_message(chat_id=CHAT_ID, text=body[i:i+limit], disable_web_page_preview=True)
    except Exception as exc:
        log_error("telegram_send", exc)


def reply(update, text: str) -> None:
    try:
        if update and getattr(update, "message", None):
            body = str(text or "")
            for i in range(0, len(body), 3600):
                update.message.reply_text(body[i:i+3600], disable_web_page_preview=True)
            return
    except Exception as exc:
        log_error("telegram_reply", exc)
    send_chunks(text)


def field_line() -> str:
    cov = STATE.get("field_coverage", {}) if isinstance(STATE.get("field_coverage"), dict) else {}
    return (
        f"- 입력값표준화: rows {cov.get('rows',0)} / 가격 {cov.get('price',0)} / 돈 {cov.get('money',0)} / "
        f"거래량 {cov.get('volume',0)} / 흐름 {cov.get('momentum',0)} / 위치 {cov.get('position',0)} / "
        f"rank {cov.get('rank',0)} / 신선 {cov.get('fresh',0)} / 정밀 {cov.get('precision',0)} / bulk-only {cov.get('bulk_only',0)} / "
        f"돈구분 real {cov.get('real_money',0)} proxy {cov.get('proxy_money',0)} missing {cov.get('missing_money',0)}"
    )


def candidate_line() -> str:
    err = STATE.get("write_error") or ""
    err_part = f" / 오류 {err[:120]}" if err else ""
    return (
        f"- 후보파일: 판단 strict {STATE.get('strict_decision',0)} / shadow {STATE.get('shadow_decision',0)} "
        f"→ trade_ready {STATE.get('trade_ready_written', STATE.get('paper_written',0))} / strict관찰 {STATE.get('strict_observe_written',0)} / 복기 {STATE.get('shadow_written',0)} / "
        f"중복skip {STATE.get('dup_skip',0)} / candidate_events 없음 / 최근 {STATE.get('last_ticker','-')}{err_part}"
    )


def stage_lines() -> List[str]:
    rows = STATE.get("stage_times") if isinstance(STATE.get("stage_times"), list) else []
    out = ["🧩 v2.13.166 scan 단계표"]
    if not rows:
        out.append("- 아직 scan 단계표 없음")
        return out
    for name, sec, note in rows:
        out.append(f"- {name}: {sec:.3f}s / {note}")
    out.append(f"- 전체: {fnum(STATE.get('scan_last_sec'),0):.3f}s / stage {STATE.get('scan_last_stage','-')}")
    return out


def reject_lines(limit: int = 5) -> List[str]:
    d = STATE.get("reject_counts") if isinstance(STATE.get("reject_counts"), dict) else {}
    rows = sorted(d.items(), key=lambda x: x[1], reverse=True)[:limit]
    out = ["- 탈락상위: " + (" / ".join([f"{k} {v}" for k, v in rows]) if rows else "-")]
    examples = STATE.get("reject_examples") if isinstance(STATE.get("reject_examples"), list) else []
    for e in examples[:3]:
        out.append(f"  · {e.get('ticker','-')}: {e.get('reason','-')} / 점수 {fnum(e.get('score'),0):.2f}")
    return out


def paper_bot_line() -> str:
    ps = read_paper_status()
    st = ps["status"]
    ctrl = ps["control"]
    open_pos = read_open()
    age = now_ts() - fnum(st.get("updated_at"), 0) if fnum(st.get("updated_at"), 0) > 0 else -1
    return (
        f"- paper_bot: {st.get('version','?')} / running {ctrl.get('running', st.get('running','?'))} / "
        f"OPEN {len(open_pos)} (정식 {st.get('open_strict','?')} / 복기 {st.get('open_shadow','?')}) / "
        f"CLOSED {st.get('closed_total', line_count(FILES['paper_closed']))} / 상태 {age:.0f}초 전" if age >= 0 else
        f"- paper_bot: 상태파일 없음 또는 대기 / OPEN {len(open_pos)} / CLOSED {line_count(FILES['paper_closed'])}"
    )


def architecture_text() -> str:
    return "\n".join([
        "🧱 허브/직원/공장 배치",
        f"- {ARCHITECTURE_ROLES['hub']}",
        f"- {ARCHITECTURE_ROLES['worker1']}",
        f"- {ARCHITECTURE_ROLES['worker2']}",
        f"- {ARCHITECTURE_ROLES['factory']}",
        f"- {ARCHITECTURE_ROLES['paper_bot']}",
    ])


def core_text() -> str:
    with _state_lock:
        s = dict(STATE)
    return "\n".join([
        "📌 종합 상태판 /core",
        "",
        "✅ 구조: 메인봇은 눈+뇌, paper_bot은 손+장부",
        architecture_text(),
        f"- 버전: {BOT_VERSION}",
        f"- 전략: {STRATEGY_NAME} / 새 본선",
        f"- scan rows {s.get('strict_decision',0)} / 시장 bulk {s.get('bulk_rows',0)} / 가격 {s.get('bulk_price',0)} / 돈 {s.get('bulk_money',0)} / 정밀캐시 {s.get('precision_have',0)} / stage {s.get('scan_last_stage','-')}",
        websocket_status_line(),
        f"- 최신 scan_id: {s.get('scan_id','-')} / paper_bot은 최신 scan 묶음 중 strict만 OPEN, shadow는 복기전용",
        f"- scan 오류: {s.get('scan_last_error','') or '-'} / bulk_source {s.get('bulk_source','-')}",
        field_line(),
        worker_status_text(),
        candidate_line(),
        f"- 정밀보강: 대상 {s.get('precision_selected',0)} / 필요 {s.get('precision_need',0)} / 적응형 {s.get('precision_budget',0)} / 고정 22개 절단 없음",
        "- candidate_events: 없음. 관찰/소비 fallback 모두 제거.",
        "- 실행분리: 메인봇=스캔·전략·후보파일 / paper_bot=모의매매 장부 / 내부paper 없음",
        paper_bot_line(),
        "- 알림: 메인봇 OFF / 모의 OPEN·CLOSED 알림은 paper_bot 전담",
        "",
        "판독",
        "- 전체 451개 안팎 시장 bulk 스캔은 유지한다.",
        "- 웹소켓은 허브 보조직원으로 실시간 가격 캐시만 담당한다.",
        "- 정밀값은 상위 유동성+급변+순환 종목을 계속 보강한다.",
        "- 자동매수/BUY_READY/v343/내부 paper 경로는 이 파일에 없다.",
    ])


def command_core(update, context) -> None:
    reply(update, core_text())


def command_cpu(update, context) -> None:
    loadavg = os.getloadavg() if hasattr(os, "getloadavg") else (0,0,0)
    rss = 0
    try:
        import resource
        rss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
    except Exception:
        pass
    lines = [
        "🧠 CPU/메모리 /cpu_status",
        "",
        f"- RSS {rss}MB / Load {loadavg[0]:.2f}/{loadavg[1]:.2f}/{loadavg[2]:.2f} / scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / scan최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s",
        f"- scan 호출 {STATE.get('scan_calls',0)} / stage {STATE.get('scan_last_stage','-')} / 오류 {STATE.get('scan_last_error','') or '-'}",
        f"- bulk_source {STATE.get('bulk_source','-')} / bulk_fetch_error {STATE.get('bulk_fetch_error','') or '-'}",
        websocket_status_line(),
        field_line(),
        candidate_line(),
        "",
    ] + stage_lines()
    reply(update, "\n".join(lines))


def command_candidate_reason(update, context) -> None:
    lines = [
        "🔎 후보·전략 원인판 /candidate_reason",
        "",
        f"전략: {STRATEGY_NAME}",
        "- v2.13.166 기준: 새 본선 / 점검체계 정리 / 신규성과 분리",
        "- 메인봇은 후보파일만 출력하고 알림은 보내지 않음",
        "- candidate_events는 코드에 없음",
        "",
        field_line(),
        candidate_line(),
        "",
        "최근 strict 예시",
    ]
    sample = STATE.get("last_rows_sample") if isinstance(STATE.get("last_rows_sample"), list) else []
    if sample:
        for e in sample[:5]:
            lines.append(f"- {e.get('ticker','-')}: 점수 {fnum(e.get('score'),0):.2f} / {e.get('line','')}")
    else:
        lines.append("- 아직 없음")
    lines += ["", "탈락/부족 사유"] + reject_lines()
    reply(update, "\n".join(lines))


def command_flow(update, context) -> None:
    lines = [
        "🧭 흐름검사 /flow_check",
        "",
        "현재 고정 본선",
        "1) 허브: ALL_KRW 전체 시장 bulk 스캔",
        "2) 1차 직원: 전체 bulk 표준화/기본 제외/정밀대상 선정",
        "3) 2차 직원들: 흐름/자리/현재봉/실전위험을 나눠 비동기 보강",
        "4) 1차 직원: 표준값 병합/신선도/커버리지 확인",
        "5) 2차 직원: 거래대금 눌림 재돌파 후보판단/차단 이유",
        "6) 공장: paper_candidates / shadow_candidates만 출력",
        "7) paper_bot: paper/shadow만 읽고 OPEN/CLOSED 저장",
        "",
        field_line(),
        candidate_line(),
        "",
    ] + stage_lines() + ["", "삭제/격리한 흐름", "- candidate_events 전체 제거", "- 내부 paper/pending/auto 제거", "- v340/v343/다중전략 dispatch 제거", "- 구버전 fallback 제거"]
    reply(update, "\n".join(lines))


def command_deploy(update, context) -> None:
    text = "\n".join([
        "📦 배포 상태 /deploy",
        "",
        f"- 메인봇 실행버전: {BOT_VERSION}",
        "- 메인봇 대상: 수익형_v2.13.166.py",
        "- 페이퍼봇 대상: paper_bot_v0.26.py",
        "- 본선 후보파일: paper_candidates.jsonl / shadow_candidates.jsonl",
        "- 보조 관찰파일: 없음(candidate_events 미사용)",
        "",
        "v2.13.166 구조",
        "- 본선: 전체시장 bulk → 정밀값 순환보강 → 표준값 → 눌림 재돌파 → paper/shadow 출력",
        "- 기존 tail patch/fallback/내부 paper/BUY_READY/v343 코드 없음",
        "- paper_bot은 전략판단 금지, 받은 후보 장부 처리만 담당",
    ])
    reply(update, text)


def command_paper_handoff(update, context) -> None:
    lines = [
        "🧪 paper handoff /paper_handoff",
        "",
        "- 실행분리: 메인봇=스캔·전략·후보파일 / paper_bot=모의매매 장부",
        paper_bot_line(),
        f"- 파일: paper_candidates {line_count(FILES['paper'])} / shadow_candidates {line_count(FILES['shadow'])} / candidate_events 없음",
        "- 기대 paper_bot: paper_bot_v0.26.py / paper_bot_v0.21",
        "",
        "역할",
        "- 메인봇: 전략 판단까지 완료해서 후보파일에 저장",
        "- paper_bot: 전략 판단 금지, 받은 후보로 모의매매 장부만 처리",
    ]
    reply(update, "\n".join(lines))




def closed_bucket_key(row: Dict[str, Any], bucket_min: int = 10) -> Tuple[str, str, str, int]:
    t = str(row.get("ticker") or "").upper()
    lane = str(row.get("lane") or "")
    strat = str(row.get("strategy") or row.get("route") or "")
    ts = fnum(row.get("closed_at"), 0)
    if ts <= 0:
        # closed_at_text만 있는 경우는 같은 묶음에 과하게 섞이지 않게 텍스트 일부를 쓴다.
        txt = str(row.get("closed_at_text") or "")[:15]
        return (t, lane, strat, hash(txt) % 10_000_000)
    return (t, lane, strat, int(ts // max(60, bucket_min * 60)))


def dedupe_closed_rows(rows: Iterable[Dict[str, Any]], bucket_min: int = 10) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        k = closed_bucket_key(r, bucket_min=bucket_min)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out



def new_period_text() -> str:
    b = ensure_eval_baseline()
    rows = load_closed()
    new_rows = rows_since_baseline(rows, "closed_at")
    n_open, old_open, n_counts, o_counts = open_split_since_baseline()
    return "\n".join([
        "🆕 신규 기준점",
        f"- 기준: {b.get('baseline_text','-')} / 기존 기록 삭제 없음",
        f"- 신규 CLOSED: {len(new_rows)}건 / 기존 CLOSED 기준선 {b.get('closed_lines_at_baseline','-')} lines",
        f"- OPEN: 신규 {n_open}개(정식 {n_counts.get('strict',0)} / 복기 {n_counts.get('shadow',0)}) / 기존 {old_open}개(정식 {o_counts.get('strict',0)} / 복기 {o_counts.get('shadow',0)})",
    ])


def candidate_fresh_text() -> str:
    p = candidate_file_fresh_stats(FILES["paper_latest"] if FILES["paper_latest"].exists() else FILES["paper"])
    sh = candidate_file_fresh_stats(FILES["shadow_latest"] if FILES["shadow_latest"].exists() else FILES["shadow"])
    return "\n".join([
        "📁 후보파일 최신성",
        f"- 정식 latest: {line_count(FILES['paper_latest'])} lines / TTL유효 {p['fresh']} / 만료 {p['expired']} / archive {line_count(FILES['paper'])} lines",
        f"- 복기 latest: {line_count(FILES['shadow_latest'])} lines / TTL유효 {sh['fresh']} / 만료 {sh['expired']} / archive {line_count(FILES['shadow'])} lines",
        "- 기존 후보 archive는 보존. paper_bot은 작은 *_latest 파일을 우선 소비",
    ])


def worker_status_text() -> str:
    return "\n".join([
        "🧑‍🏭 직원 대기열",
        f"- 2차 정밀직원: 대기 {STATE.get('precision_queue_size', 0)} / 캐시 {STATE.get('precision_have', 0)} / 직접갱신한도 {STATE.get('precision_sync_limit', 0)}",
        f"- 실전위험직원: 대기 {STATE.get('execution_risk_queue_size', 0)} / 캐시 {STATE.get('execution_risk_cached', 0)}",
        "- 뜻: 기능은 빼지 않고, 무거운 계산을 메인 스캔 밖 직원들이 나눠 처리",
    ])


def check_text() -> str:
    return "\n".join([
        "✅ 통합 점검 /check",
        "",
        "[1/6] 봇 상태",
        f"- 메인봇: {BOT_VERSION} / stage {STATE.get('scan_last_stage','-')} / scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s",
        f"- paper_bot: {paper_bot_line()}",
        "",
        "[2/6] 허브",
        f"- 시장 bulk {STATE.get('bulk_rows',0)} / 가격 {STATE.get('bulk_price',0)} / 돈 {STATE.get('bulk_money',0)} / source {STATE.get('bulk_source','-')}",
        websocket_status_line(),
        "",
        "[3/6] 직원 상태",
        field_line(),
        worker_status_text(),
        "",
        "[4/6] 공장",
        candidate_line(),
        candidate_fresh_text(),
        "",
        "[5/6] 신규성과",
        new_period_text(),
        "⚠️ 신규 표본 50건 전까지 수익률 과신 금지",
        "",
        "[6/6] 최근 병목/오류",
    ] + stage_lines() + [f"- 최근오류: {STATE.get('scan_last_error','') or '-'} / write_error {STATE.get('write_error','') or '-'}"])


def command_check(update, context) -> None:
    reply(update, check_text())


def command_score(update, context) -> None:
    rows = load_closed()
    new_rows = rows_since_baseline(rows, "closed_at")
    strict = [r for r in rows if str(r.get("lane")) == "strict"]
    shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
    new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    new_shadow = [r for r in new_rows if str(r.get("lane")) == "shadow"]
    strat = [r for r in rows if STRATEGY_NAME in str(r.get("strategy", "")) or STRATEGY_KEY in str(r.get("strategy", "")) or STRATEGY_KEY in str(r.get("route", ""))]
    new_strat = [r for r in new_rows if STRATEGY_NAME in str(r.get("strategy", "")) or STRATEGY_KEY in str(r.get("strategy", "")) or STRATEGY_KEY in str(r.get("route", ""))]
    deduped = dedupe_closed_rows(rows, bucket_min=10)
    new_deduped = dedupe_closed_rows(new_rows, bucket_min=10)
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in new_rows)
    lines = [
        "📊 paper 성과판 /score",
        "",
        "[1/8] 한눈에 보기",
        new_period_text(),
        fmt_stats("신규 정식 strict", new_strict),
        fmt_stats("신규 전체 참고", new_rows),
        fmt_stats("신규 복기 shadow 참고", new_shadow),
        fmt_stats(f"신규 {STRATEGY_NAME}", new_strat),
        "⚠️ 자동매매 판단은 신규 정식 strict 중심. shadow는 복기 참고",
        "",
        "[2/8] 시간대별 3시간 묶음",
        group_table("- 00/03/06/09/12/15/18/21시 구간", new_rows, bucket_hour3, limit=8),
        "",
        "[3/8] 등급별",
        group_table("- strict / shadow", new_rows, lambda r: str(r.get("lane") or "unknown"), limit=6),
        "",
        "[4/8] 자동매매 준비등급/점수대",
        group_table("- auto_ready 또는 점수대", new_rows, bucket_auto_ready, limit=8),
        "",
        "[5/8] 날짜별",
        group_table("- 날짜별 신규 CLOSED", new_rows, bucket_date, limit=10),
        "",
        "[6/8] 청산사유",
    ]
    if by_reason:
        for k, v in by_reason.most_common(10):
            sub = [r for r in new_rows if str(r.get("exit_reason") or "unknown") == k]
            lines.append(fmt_stats(k, sub))
    else:
        lines.append("- 아직 신규 CLOSED 없음")
    lines += [
        "",
        "[7/8] 누적 참고",
        fmt_stats("누적 전체", rows),
        fmt_stats("누적 정식 strict", strict),
        fmt_stats("누적 복기 shadow", shadow),
        fmt_stats(f"누적 {STRATEGY_NAME}", strat),
        fmt_stats("신규 중복보정", new_deduped),
        fmt_stats("누적 중복보정", deduped),
        f"- 누적 제외된 중복 추정: {max(0, len(rows)-len(deduped))}건 / 신규 {max(0, len(new_rows)-len(new_deduped))}건",
        "",
        "[8/8] 다음 판단 기준",
        simple_judge_text(),
        "",
        paper_bot_line(),
    ]
    reply(update, "\n".join(lines))

def command_paper_today(update, context) -> None:
    rows = load_closed()
    today = datetime.now().strftime("%Y-%m-%d")
    today_rows = [r for r in rows if str(r.get("closed_at_text", "")).startswith(today)]
    new_rows = rows_since_baseline(rows, "closed_at")
    lines = [
        "📌 오늘 paper·모의매매 /paper_today",
        "",
        "[1/4] paper_bot",
        paper_bot_line(),
        "",
        "[2/4] 오늘 종료",
        fmt_stats("오늘 종료", today_rows),
        "",
        "[3/4] 신규 기준점 이후",
        new_period_text(),
        fmt_stats("신규 종료", new_rows),
        "",
        "[4/4] 후보파일",
        candidate_fresh_text(),
        "",
        "판독",
        "- 오늘 성과와 v157 이후 신규 성과를 분리해서 본다",
        "- 기존 기록 삭제 없이 기준점 이후 결과만 자동매매 판단에 사용",
    ]
    reply(update, "\n".join(lines))


def command_batch(update, context) -> None:
    funcs = [
        ("core", core_text),
        ("check", check_text),
        ("score", lambda: "\n".join(["📊 paper 성과판 /score", "", "[요약]", fmt_stats("신규 strict", [r for r in rows_since_baseline(load_closed(), "closed_at") if str(r.get('lane')) == 'strict']), fmt_stats("신규 전체 참고", rows_since_baseline(load_closed(), "closed_at")), fmt_stats("신규 shadow 참고", [r for r in rows_since_baseline(load_closed(), "closed_at") if str(r.get('lane')) == 'shadow']), "⚠️ 자동매매 판단은 strict 중심. shadow는 복기 참고", fmt_stats("누적 전체", load_closed()), paper_bot_line()])),
        ("paper_today", lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", "", new_period_text(), candidate_fresh_text()])),
        ("flow_check", lambda: "\n".join(["🧭 흐름검사 /flow_check", field_line(), candidate_line()] + stage_lines())),
        ("candidate_reason", lambda: "\n".join(["🔎 후보·전략 원인판 /candidate_reason", field_line(), candidate_line()] + reject_lines())),
        ("cpu_status", lambda: "\n".join(["🧠 CPU/메모리 /cpu_status", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / 최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s / stage {STATE.get('scan_last_stage','-')}"] + stage_lines())),
        ("paper_handoff", lambda: "\n".join(["🧪 paper handoff /paper_handoff", paper_bot_line(), candidate_fresh_text()])),
        ("deploy", lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.166.py", "- 페이퍼봇 대상: paper_bot_v0.26.py"])),
    ]
    start = now_ts()
    reply(update, "\n".join(["📦 묶음 명령 접수", "- 출처: /batch", f"- 실행 {len(funcs)}개", "- v2.13.166: 후보분석 전부 기록 + trade_ready만 paper OPEN", "- 기존 기록 삭제 없음 / 후보판단 고정절단 없음 / paper_bot은 trade_ready만 OPEN"]))
    rows = []
    total = len(funcs)
    for idx, (name, fn) in enumerate(funcs, start=1):
        st = now_ts()
        try:
            body = fn()
            reply(update, f"[{idx}/{total}] /{name}\n" + body)
            rows.append((name, now_ts() - st, "OK"))
        except Exception as exc:
            rows.append((name, now_ts() - st, f"ERR {exc.__class__.__name__}"))
            log_error(f"batch:{name}", exc)
    lines = ["🧾 v2.13.166 batch 요약", f"- 출력무결성: 실행 {len(funcs)}개", "", "⏱ 명령어별 시간표"]
    for n, sec, res in rows:
        lines.append(f"- /{n}: 처리 {sec:.2f}s / 결과 {res}")
    lines += ["", f"- 전체 경과: {now_ts()-start:.2f}s", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / scan최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s", candidate_line(), field_line(), "- 알림: 메인봇 OFF / paper_bot만 모의매매 알림"]
    reply(update, "\n".join(lines))


TEXT_COMMAND_BUILDERS: Dict[str, Any] = {}


def _command_name_from_line(line: str) -> str:
    first = (line or "").strip().split()[0].lower() if (line or "").strip().split() else ""
    if first.startswith("/"):
        first = first[1:]
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


def _extract_command_lines(text: str) -> List[str]:
    out = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if ln.startswith("/"):
            out.append(ln)
    return out


def _builder_for_command(name: str):
    if not TEXT_COMMAND_BUILDERS:
        TEXT_COMMAND_BUILDERS.update({
            "core": core_text, "main": core_text, "monitor": core_text, "status": core_text,
            "check": check_text,
            "flow_check": lambda: "\n".join(["🧭 흐름검사 /flow_check", field_line(), candidate_line()] + stage_lines()),
            "candidate_reason": lambda: "\n".join(["🔎 후보·전략 원인판 /candidate_reason", field_line(), candidate_line()] + reject_lines()),
            "cpu_status": lambda: "\n".join(["🧠 CPU/메모리 /cpu_status", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / 최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s / stage {STATE.get('scan_last_stage','-')}"] + stage_lines()),
            "paper_handoff": lambda: "\n".join(["🧪 paper handoff /paper_handoff", paper_bot_line(), candidate_fresh_text()]),
            "deploy": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.166.py", "- 페이퍼봇 대상: paper_bot_v0.26.py"]),
            "upgradestatus": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.166.py", "- 페이퍼봇 대상: paper_bot_v0.26.py"]),
            "paper_today": lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", "", new_period_text(), candidate_fresh_text()]),
            "score": lambda: "\n".join(["📊 paper 성과판 /score", "", "[요약]", fmt_stats("신규 strict", [r for r in rows_since_baseline(load_closed(), "closed_at") if str(r.get('lane')) == 'strict']), fmt_stats("신규 전체 참고", rows_since_baseline(load_closed(), "closed_at")), fmt_stats("신규 shadow 참고", [r for r in rows_since_baseline(load_closed(), "closed_at") if str(r.get('lane')) == 'shadow']), "⚠️ 자동매매 판단은 strict 중심. shadow는 복기 참고", fmt_stats("누적 전체", load_closed()), paper_bot_line()]),
            "trade": lambda: "\n".join(["🔒 거래 상태 /trade", "- 자동매수: OFF", "- BUY_READY: 생성 안 함", "- 실제 주문: 없음"]),
            "prune_check": lambda: "\n".join(["🧹 가지치기 점검 /prune_check", "- candidate_events 없음", "- BUY_READY 구경로 없음", "- 내부 paper 없음", "- shadow는 복기전용"]),
            "errorlog": lambda: "🧯 오류로그 /errorlog\n\n" + tail_text(FILES["error"], 80),
            "help": lambda: "📚 명령어 /help\n- 여러 명령을 줄바꿈으로 보내도 자동 묶음 처리됨\n- /batch /check /core /score /cpu_status /paper_handoff /deploy",
        })
    return TEXT_COMMAND_BUILDERS.get(name)


def handle_multi_command_message(update, context) -> bool:
    text = ""
    try:
        text = getattr(getattr(update, "message", None), "text", "") or ""
    except Exception:
        text = ""
    lines = _extract_command_lines(text)
    if len(lines) <= 1:
        return False
    reply(update, f"📦 자동 묶음 명령 접수\n- /batch 없이 여러 줄 명령을 감지\n- 실행 {len(lines)}개")
    total = len(lines)
    for idx, line in enumerate(lines, start=1):
        name = _command_name_from_line(line)
        if name == "batch":
            body = "이미 자동 묶음 처리 중이라 /batch는 건너뜀"
        else:
            fn = _builder_for_command(name)
            if not fn:
                body = f"알 수 없는 명령: /{name}"
            else:
                try:
                    body = str(fn())
                except Exception as exc:
                    log_error(f"multi:{name}", exc)
                    body = f"오류: {exc.__class__.__name__}: {exc}"
        reply(update, f"[{idx}/{total}] /{name}\n" + body)
    return True



def command_scan_now(update, context) -> None:
    reply(update, "🔁 즉시 스캔 1회 실행")
    scan_once()
    reply(update, "\n".join(["✅ 즉시 스캔 완료", candidate_line(), field_line(), f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s"]))


def command_errorlog(update, context) -> None:
    text = "\n".join(list(_recent_errors)[-10:]) or "최근 오류 없음"
    try:
        tail = FILES["error"].read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
        text = "\n".join(tail) or text
    except Exception:
        pass
    reply(update, "🧯 최근 오류 /errorlog\n\n" + text[-3200:])




def command_trade(update, context) -> None:
    reply(update, "\n".join([
        "🛡 매매 상태 /trade",
        "",
        "- 자동매수: OFF 고정",
        "- BUY_READY 강제 생성: 없음",
        "- 메인봇 내부 paper/auto/pending 실행: 없음",
        "- 실제 주문: 없음",
        "- 현재 구조: 메인봇은 후보파일만 만들고, paper_bot이 모의매매 장부만 처리",
        "",
        paper_bot_line(),
    ]))


def command_help(update, context) -> None:
    reply(update, "\n".join([
        f"📚 명령어 /help - {BOT_VERSION}",
        "",
        "기본 확인",
        "- /batch /core /score /paper_today /cpu_status",
        "흐름/후보",
        "- /flow_check /candidate_reason /paper_handoff /deploy",
        "안전/오류",
        "- /trade /errorlog /prune_check /scan_now",
        "호환 별칭",
        "- /main=/core, /monitor=/core, /status=/core, /upgradestatus=/deploy",
        "",
        "구조",
        "- 전체시장 bulk 스캔 유지",
        "- v150 단일전략 점수 흐름 유지",
        "- candidate_events 생성/소비 없음",
    ]))


def command_prune_check(update, context) -> None:
    reply(update, "\n".join([
        "🧹 가지치기 점검 /prune_check",
        "",
        "✅ 새 본선 실행 파일에는 아래 경로를 넣지 않음",
        "- candidate_events 생성/소비/fallback",
        "- 내부 paper/pending/auto",
        "- BUY_READY 강제 생성",
        "- v343 실행경로",
        "- 구버전 tail patch 재정의",
        "",
        "✅ 남긴 기능",
        "- 전체시장 bulk 스캔",
        "- 상위+급변+순환 정밀값 보강",
        "- v150 단일전략 점수 흐름",
        "- paper/shadow 소비파일 출력",
        "- 상태/성과/CPU/배포/흐름 명령어",
    ]))

def install_commands(updater: Any) -> None:
    dp = updater.dispatcher
    mapping = {
        "batch": command_batch,
        "core": command_core,
        "main": command_core,
        "monitor": command_core,
        "status": command_core,
        "score": command_score,
        "check": command_check,
        "paper_today": command_paper_today,
        "flow_check": command_flow,
        "candidate_reason": command_candidate_reason,
        "cpu_status": command_cpu,
        "paper_handoff": command_paper_handoff,
        "deploy": command_deploy,
        "upgradestatus": command_deploy,
        "trade": command_trade,
        "prune_check": command_prune_check,
        "scan_now": command_scan_now,
        "errorlog": command_errorlog,
        "help": command_help,
    }
    with _state_lock:
        STATE["compat_commands"] = sorted(mapping.keys())
    def _wrap(fn):
        def _inner(update, context):
            if handle_multi_command_message(update, context):
                return
            return fn(update, context)
        return _inner
    for name, fn in mapping.items():
        dp.add_handler(CommandHandler(name, _wrap(fn)))
    try:
        menu = ["batch", "check", "core", "score", "paper_today", "flow_check", "candidate_reason", "cpu_status", "paper_handoff", "deploy", "trade", "errorlog", "help"]
        updater.bot.set_my_commands([BotCommand(k, k) for k in menu])
    except Exception:
        pass


def start_background_workers() -> None:
    global _background_workers_started
    if _background_workers_started:
        return
    _background_workers_started = True
    for i in range(max(1, PRECISION_BACKGROUND_WORKERS)):
        threading.Thread(target=precision_worker_loop, args=(i + 1,), name=f"precision_worker_{i+1}", daemon=True).start()
    for i in range(max(1, EXEC_RISK_BACKGROUND_WORKERS)):
        threading.Thread(target=execution_risk_worker_loop, args=(i + 1,), name=f"execution_risk_worker_{i+1}", daemon=True).start()
    threading.Thread(target=websocket_hub_worker_loop, name="websocket_hub_worker", daemon=True).start()
    log(f"background_workers started precision={PRECISION_BACKGROUND_WORKERS} execution_risk={EXEC_RISK_BACKGROUND_WORKERS} websocket={WS_HUB_ON}")


def startup_checks() -> None:
    ensure_eval_baseline()
    for p in [FILES["paper"], FILES["shadow"]]:
        ok, note = ensure_candidate_file(p)
        log(f"candidate_file {p.name}: {ok} {note}")
    # candidate_events는 일부러 만들지도 읽지도 않는다.
    save_json(FILES["status"], STATE)


def main() -> None:
    startup_checks()
    start_background_workers()
    t = threading.Thread(target=scan_loop, name="clean_scan_loop", daemon=True)
    t.start()
    if not TELEGRAM_TOKEN or not CHAT_ID or Updater is None:
        print(f"{BOT_VERSION} running without telegram. TELEGRAM_TOKEN/CHAT_ID missing or telegram lib unavailable.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            _stop_event.set()
            return
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
    install_commands(updater)
    send_chunks("\n".join(["✅ 봇 시작 완료", f"현재 버전: {BOT_VERSION}", f"전략: {STRATEGY_NAME}", "v150 수치 이식 + 직원 분리 + latest 후보파일 + shadow 격리 / 구버전 tail 없는 새 본선", "확인: /batch /core /cpu_status /paper_handoff /deploy"]))
    updater.start_polling(drop_pending_updates=True)
    updater.idle()
    _stop_event.set()


if __name__ == "__main__":
    main()
