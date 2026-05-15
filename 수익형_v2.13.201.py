#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
수익형_v2.13.200.py

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
from collections import Counter, deque, defaultdict
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

BOT_VERSION = "수익형 v2.13.201"
# HTTP 헤더는 latin-1만 안전하다. BOT_VERSION은 한글이라 User-Agent로 쓰면
# UnicodeEncodeError가 나며 bulk 스캔이 시작 즉시 0으로 죽는다.
HTTP_USER_AGENT = "coinbot-v2.13.199-mainline"
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
EXEC_RISK_SYNC_TOP_N = int(os.getenv("CLEAN_EXEC_RISK_SYNC_TOP_N", "8"))
EXEC_RISK_SYNC_MIN_SCORE = float(os.getenv("CLEAN_EXEC_RISK_SYNC_MIN_SCORE", "4.45"))  # v185: TRADE_READY_MIN_SCORE 정의 전 참조 방지
EXEC_RISK_SYNC_WORKERS = int(os.getenv("CLEAN_EXEC_RISK_SYNC_WORKERS", "4"))
# v180: 원인제거. pybithumb WebSocketManager는 multiprocessing 자식 bot.py를 만들고
# systemd stop/restart 때 deactivating timeout을 유발했다.
# 그래서 메인봇 내부에서는 legacy WS를 하드 차단한다.
# 실시간 WS 중심 구조는 다음 단계에서 별도 안전 worker로 재설계한다.
WS_HUB_REQUESTED = str(os.getenv("CLEAN_WS_HUB_ON", "0")).strip().lower() not in {"0", "false", "no", "off"}
WS_HUB_ON = False
WS_HUB_MAX_TICKERS = int(os.getenv("CLEAN_WS_HUB_MAX_TICKERS", "220"))  # v201: WS 대상 80 고착 해제(전략 제한 아님, sidecar 안전상한)
WS_HUB_STALE_SEC = float(os.getenv("CLEAN_WS_HUB_STALE_SEC", "12"))
# v199: 후보를 WS target에서 너무 빨리 빼지 않는다. 신선정보 없음은 차단이 아니라 우선수집 대상이다.
WS_TARGET_KEEP_TTL_SEC = float(os.getenv("CLEAN_WS_TARGET_KEEP_TTL_SEC", "55"))
WS_TARGET_REWRITE_MIN_SEC = float(os.getenv("CLEAN_WS_TARGET_REWRITE_MIN_SEC", "4.0"))
WS_TARGET_URGENT_REWRITE_SEC = float(os.getenv("CLEAN_WS_TARGET_URGENT_REWRITE_SEC", "1.2"))
WS_SIDECAR_STATUS_STALE_SEC = float(os.getenv("CLEAN_WS_SIDECAR_STATUS_STALE_SEC", "20"))
MICRO_TARGET_MAX = int(os.getenv("CLEAN_MICRO_TARGET_MAX", "180"))  # v201: micro 대상 32 고착 해제(수집대상 넓힘, 조건 제한 아님)
MICRO_STALE_SEC = float(os.getenv("CLEAN_MICRO_STALE_SEC", "25"))
MICRO_STATUS_STALE_SEC = float(os.getenv("CLEAN_MICRO_STATUS_STALE_SEC", "35"))
MICRO_SPREAD_HARD_PCT = float(os.getenv("CLEAN_MICRO_SPREAD_HARD_PCT", "0.45"))
MICRO_BUY_RATIO_WEAK = float(os.getenv("CLEAN_MICRO_BUY_RATIO_WEAK", "0.42"))
MICRO_ASK_WALL_RATIO = float(os.getenv("CLEAN_MICRO_ASK_WALL_RATIO", "1.50"))
WS_HUB_RESTART_SEC = float(os.getenv("CLEAN_WS_HUB_RESTART_SEC", "20"))
WS_BLOCK_REASON = "v180: pybithumb WebSocketManager 제거됨(자식 bot.py/stop timeout 원인). REST 안정스캔 유지."
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
# v186: 6차 최종 진입검증 직원. 후보 생성 조건이 아니라 paper OPEN 직전 보류/관찰 분리용이다.
FINAL_ENTRY_WORKER_ON = str(os.getenv("CLEAN_FINAL_ENTRY_WORKER_ON", "1")).strip().lower() not in {"0", "false", "no", "off"}
FINAL_SLOW_SPIKE_MIN = float(os.getenv("CLEAN_FINAL_SLOW_SPIKE_MIN", "2.80"))
FINAL_WEAK_PULLBACK_MAX = float(os.getenv("CLEAN_FINAL_WEAK_PULLBACK_MAX", "1.45"))
FINAL_WEAK_REBREAK_MAX = float(os.getenv("CLEAN_FINAL_WEAK_REBREAK_MAX", "1.20"))
FINAL_PRICE_RECHECK_NEG = float(os.getenv("CLEAN_FINAL_PRICE_RECHECK_NEG", "-0.03"))
FINAL_PRICE_RECHECK_HARD_NEG = float(os.getenv("CLEAN_FINAL_PRICE_RECHECK_HARD_NEG", "-0.10"))
FINAL_WS_GAP_NEG = float(os.getenv("CLEAN_FINAL_WS_GAP_NEG", "-0.03"))
FINAL_VWAP_GAP_NEG = float(os.getenv("CLEAN_FINAL_VWAP_GAP_NEG", "-0.05"))
FINAL_MARKET_UP_WEAK = float(os.getenv("CLEAN_FINAL_MARKET_UP_WEAK", "38.0"))
FINAL_ATR_HIGH_PCT = float(os.getenv("CLEAN_FINAL_ATR_HIGH_PCT", "0.85"))
# v189: 쓰레기 후보 분리팩. 단일조건 차단이 아니라 paper OPEN 직전 관찰/재확인 분리용.
FINAL_PUMP_MF_RATIO_MAX = float(os.getenv("CLEAN_FINAL_PUMP_MF_RATIO_MAX", "1.85"))
FINAL_SPREAD_HARD_PCT = float(os.getenv("CLEAN_FINAL_SPREAD_HARD_PCT", "0.60"))
FINAL_LOW_MONEY_3M = float(os.getenv("CLEAN_FINAL_LOW_MONEY_3M", "7000000"))
FINAL_HIGH_CHASE_GAP_PCT = float(os.getenv("CLEAN_FINAL_HIGH_CHASE_GAP_PCT", "0.55"))
FINAL_FROM_LOW_HOT_PCT = float(os.getenv("CLEAN_FINAL_FROM_LOW_HOT_PCT", "2.50"))
FINAL_UPPER_WICK_PCT = float(os.getenv("CLEAN_FINAL_UPPER_WICK_PCT", "0.45"))
FINAL_CLOSE_POS_WEAK = float(os.getenv("CLEAN_FINAL_CLOSE_POS_WEAK", "0.45"))
FINAL_MARKET_SOLO_SPIKE_MIN = float(os.getenv("CLEAN_FINAL_MARKET_SOLO_SPIKE_MIN", "2.20"))
# v175: 호가 API/단위 이상값을 0이나 과장된 %로 믿지 않기 위한 품질 범위.
SPREAD_ABNORMAL_PCT = float(os.getenv("CLEAN_SPREAD_ABNORMAL_PCT", "5.0"))
PRICE_RECHECK_ABNORMAL_PCT = float(os.getenv("CLEAN_PRICE_RECHECK_ABNORMAL_PCT", "20.0"))
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
    "version_baseline": BASE_DIR / "paper_eval_baseline_current_version.json",
    "ws_cache": BASE_DIR / "clean_ws_live_cache.json",
    "ws_sidecar_status": BASE_DIR / "clean_ws_sidecar_status.json",
    "ws_targets": BASE_DIR / "clean_ws_targets.json",
    "micro_cache": BASE_DIR / "clean_bithumb_micro_cache.json",
    "micro_status": BASE_DIR / "clean_bithumb_micro_status.json",
    "micro_targets": BASE_DIR / "clean_micro_targets.json",
    "paper_flag": BASE_DIR / "external_paper_bot_on.flag",
    "legacy_paper_flag": BASE_DIR / "external_paper_runner_on.flag",
}

STABLE_EXCLUDED = {"USDC", "USDT", "BUSD", "USDP", "DAI", "TUSD", "FDUSD", "USDS", "USD1", "PYUSD", "USDE", "RLUSD"}
# v172: 대형주는 알트 초단타 매매 후보가 아니라 시장 참고용으로 분리한다.
# 삭제/차단이 아니라 major_watch로 남겨서 시장 분위기와 놓친 움직임을 계속 관찰한다.
MAJOR_WATCH_TICKERS = {"BTC", "ETH", "XRP"}

ARCHITECTURE_ROLES = {
    "hub": "허브: 전체시장 가격/거래대금/rank/웹소켓 원자료만 수집",
    "worker1": "1차 직원: 전체시장 표준화·신선도·대형주/알트 분리·정밀대상 선정",
    "worker2": "2차 직원: 눌림 품질·흐름·위치 정밀값 보강",
    "worker3": "3차 직원: 재돌파 확인·거래량만 튄 가짜반등 분리",
    "worker4": "4차 직원: 호가·틱·슬리피지·추격 위험 검사",
    "worker5": "5차 직원: trade_ready / strict관찰 / shadow / major_watch 등급 분류",
    "worker6": "6차 직원: ATR/VWAP/시장장세/WS 기준으로 paper OPEN 직전 최종검증",
    "ws_targeter": "WS 대상직원: 현재 후보·paper OPEN·재확인·보유코인을 sidecar 최신수신 대상으로 올림",
    "micro_worker": "미세구조 직원: 빗썸 호가창·최근 체결방향을 별도 sidecar 캐시로 수집",
    "factory": "공장: latest 후보파일과 entry_context만 저장, 판단/매매 금지",
    "paper_bot": "paper_bot: trade_ready만 OPEN, 장부/알림만 담당",
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
_micro_lock = threading.RLock()
_micro_cache: Dict[str, Dict[str, Any]] = {}
_micro_targets: List[str] = []
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
    "scan_running": False,
    "scan_started_at": 0.0,
    "last_done_scan_sec": 0.0,
    "last_done_scan_ts": 0.0,
    "scan_display_note": "",
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
    "execution_risk_sync_checked": 0,
    "execution_risk_sync_note": "대기",
    "ws_enabled": False,
    "ws_requested": WS_HUB_REQUESTED,
    "ws_state": "격리",
    "ws_targets": 0,
    "ws_cached": 0,
    "ws_fresh": 0,
    "ws_last_age_sec": -1,
    "ws_last_error": WS_BLOCK_REASON,
    "ws_target_file_written": 0,
    "ws_target_file_targets": 0,
    "ws_missing_rows": 0,
    "ws_stale_rows": 0,
    "field_coverage": {},
    "strict_decision": 0,
    "shadow_decision": 0,
    "major_watch_count": 0,
    "major_watch_written": 0,
    "paper_latest_written": 0,
    "shadow_latest_written": 0,
    "latest_trade_ready": 0,
    "latest_strict_observe": 0,
    "latest_final_recheck_wait": 0,
    "latest_final_observe": 0,
    "final_entry_checked": 0,
    "final_entry_open": 0,
    "final_entry_recheck_wait": 0,
    "final_entry_observe": 0,
    "final_entry_note": "대기",
    "market_context": {},
    "dup_skip_reason": {},
    "data_quality_note": "",
    "worker3_rebreakout_ready": 0,
    "worker4_risk_ready": 0,
    "paper_written": 0,
    "shadow_written": 0,
    "dup_skip": 0,
    "write_error": "",
    "reject_counts": {},
    "reject_examples": [],
    "last_ticker": "-",
    "last_rows_sample": [],
    "compat_commands": [],
    "phase_note": "v199: WS 신선도 고정판. target rewrite 과다 방지, 후보 TTL 유지, WS age 기준 통일. 조건/청산/BUY_READY 변경 없음.",
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


def first_number(row: Any, keys: Iterable[str], default: float = 0.0) -> float:
    """dict에서 여러 후보 키를 순서대로 찾아 숫자로 반환한다. 웹소켓 OFF여도 켜질 때를 대비한 허브 공용 helper."""
    try:
        if not isinstance(row, dict):
            return default
        for k in keys or []:
            if k in row and row.get(k) not in (None, ""):
                return fnum(row.get(k), default)
    except Exception:
        pass
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


def atomic_write(path: Path, obj: Any) -> None:
    """원자적 JSON 저장. v190: write_ws_targets가 참조하던 누락 helper 복구."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


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


def paper_bot_baseline_ts() -> float:
    """paper_bot이 실제 장부 판단에 쓰는 기준점(v167)을 우선 사용한다.
    main 기준점과 paper_bot 기준점이 달라 전적이 다르게 보이는 혼란을 줄이기 위함.
    """
    data = load_json(BASE_DIR / "paper_eval_baseline_v167.json", {})
    ts = fnum((data or {}).get("baseline_ts"), 0) if isinstance(data, dict) else 0
    return ts if ts > 0 else baseline_ts()


def paper_bot_baseline_text() -> str:
    data = load_json(BASE_DIR / "paper_eval_baseline_v167.json", {})
    if isinstance(data, dict) and data.get("baseline_text"):
        return str(data.get("baseline_text"))
    return str(ensure_eval_baseline().get("baseline_text", "-"))


def rows_since_paper_bot_baseline(rows: Iterable[Dict[str, Any]], *keys: str) -> List[Dict[str, Any]]:
    bts = paper_bot_baseline_ts()
    return [r for r in rows or [] if row_ts(r, *keys) >= bts]


def active_file_mtime_ts() -> float:
    """현재 bot.py가 배포된 시각을 버전 기준점으로 사용한다."""
    for name in ("bot.py", Path(__file__).name):
        try:
            p = BASE_DIR / str(name)
            if p.exists():
                return float(p.stat().st_mtime)
        except Exception:
            continue
    return now_ts()


def ensure_version_eval_baseline() -> Dict[str, Any]:
    """현재 메인봇 버전 이후 성과만 따로 보기 위한 기준점.
    기록 삭제가 아니라, 버전 효과 비교용 보조 기준이다.
    """
    path = FILES.get("version_baseline", BASE_DIR / "paper_eval_baseline_current_version.json")
    data = load_json(path, {})
    if isinstance(data, dict) and data.get("version") == BOT_VERSION and fnum(data.get("baseline_ts"), 0) > 0:
        return data
    ts = active_file_mtime_ts()
    data = {
        "schema": "paper_eval_baseline_current_version_v189",
        "version": BOT_VERSION,
        "baseline_ts": ts,
        "baseline_text": now_text(ts),
        "closed_lines_at_baseline": line_count(FILES["paper_closed"]),
        "note": "현재 메인봇 버전 이후 paper CLOSED를 별도 집계하기 위한 기준점. 기존 기록 삭제 없음.",
    }
    save_json(path, data)
    return data


def version_baseline_ts() -> float:
    return fnum(ensure_version_eval_baseline().get("baseline_ts"), 0)


def version_baseline_text() -> str:
    return str(ensure_version_eval_baseline().get("baseline_text", "-"))


def rows_since_current_version(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bts = version_baseline_ts()
    out = []
    for r in rows or []:
        # v0.36 이후는 opened_at/brain_version이 정확하고, 이전 기록은 source_created_at/closed_at으로 보조 판단한다.
        ts = row_ts(r, "opened_at", "source_created_at", "closed_at")
        if ts >= bts:
            out.append(r)
    return out


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


def _ticker_from_any(v: Any) -> str:
    t = str(v or "").upper().strip().replace("KRW-", "").replace("_KRW", "").replace("/KRW", "")
    return t.strip("-_/ ")


def _push_unique_ticker(out: List[str], seen: set, ticker: Any) -> None:
    t = _ticker_from_any(ticker)
    if not t or t in STABLE_EXCLUDED or t in seen:
        return
    seen.add(t)
    out.append(t)


def _paper_open_tickers(limit: int = 40) -> List[str]:
    try:
        obj = load_json(FILES.get("paper_open", BASE_DIR / "paper_bot_open.json"), {})
        if not isinstance(obj, dict):
            return []
        rows = list(obj.values()) if all(isinstance(v, dict) for v in obj.values()) else []
        rows.sort(key=lambda r: fnum((r or {}).get("last_update") or (r or {}).get("opened_at"), 0.0), reverse=True)
        out: List[str] = []
        seen: set = set()
        for r in rows:
            _push_unique_ticker(out, seen, (r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        log_error("paper_open_tickers", exc)
        return []


def _ws_reconnect_notice_text(text: Any) -> bool:
    s = str(text or "").lower()
    return "target file changed" in s or "reconnect" in s or "재구독" in s or "대상변경" in s


def _ws_target_payload_path() -> Path:
    return FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json")


def _load_ws_target_payload() -> Dict[str, Any]:
    obj = load_json(_ws_target_payload_path(), {})
    return obj if isinstance(obj, dict) else {}


def _ws_target_meta_from_payload(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    meta = payload.get("target_meta") if isinstance(payload, dict) else {}
    if not isinstance(meta, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for k, v in meta.items():
        t = _ticker_from_any(k)
        if not t or t in STABLE_EXCLUDED:
            continue
        out[t] = dict(v) if isinstance(v, dict) else {}
    return out


def _ws_target_changed_enough(old_targets: List[str], new_targets: List[str]) -> Tuple[bool, int, int]:
    old = [_ticker_from_any(x) for x in old_targets or [] if _ticker_from_any(x)]
    new = [_ticker_from_any(x) for x in new_targets or [] if _ticker_from_any(x)]
    if old == new:
        return False, 0, 0
    old_set, new_set = set(old), set(new)
    added = len(new_set - old_set)
    removed = len(old_set - new_set)
    return True, added, removed

def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:
    """sidecar 실시간 수신 대상을 갱신한다.

    v199: 신선정보 없음은 차단이 아니라 우선수집 대상이다.
    - 현재/직전 진입후보와 OPEN 보유코인을 먼저 올린다.
    - 한 번 올린 후보는 짧은 TTL 동안 유지해서 target 파일이 매 scan마다 흔들리지 않게 한다.
    - target 파일 내용이 실질적으로 같거나 작은 순서 변경뿐이면 rewrite를 생략해 sidecar 재구독 반복을 줄인다.
    """
    try:
        nowv = now_ts()
        path = _ws_target_payload_path()
        old_payload = _load_ws_target_payload()
        old_targets = [_ticker_from_any(x) for x in (old_payload.get("targets") or []) if _ticker_from_any(x)]
        old_meta = _ws_target_meta_from_payload(old_payload)
        old_updated = fnum(old_payload.get("updated_ts"), 0.0)

        ticks: List[str] = []
        seen: set = set()
        meta: Dict[str, Dict[str, Any]] = {}

        def push(ticker: Any, source: str, priority: int = 0) -> None:
            t = _ticker_from_any(ticker)
            if not t or t in STABLE_EXCLUDED or t in seen:
                return
            seen.add(t)
            ticks.append(t)
            prev = old_meta.get(t, {})
            first_seen = fnum(prev.get("first_seen"), nowv) if isinstance(prev, dict) else nowv
            meta[t] = {
                "first_seen": first_seen or nowv,
                "last_seen": nowv,
                "source": source,
                "priority": priority,
            }

        current_priority = list(priority_rows or [])
        previous_priority = recent_candidate_priority_rows(limit=260)
        merged_priority = current_priority + previous_priority

        sorted_pri = sorted(merged_priority, key=lambda r: (
            bool((r or {}).get("paper_bot_open") or (r or {}).get("trade_ready") or (r or {}).get("open_eligible")),
            str((r or {}).get("final_entry_action") or "") in {"paper_open", "trade_ready"},
            str((r or {}).get("final_entry_action") or "") == "recheck_wait",
            fnum((r or {}).get("score"), 0),
            fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
        ), reverse=True)
        urgent_priority: List[str] = []
        for r in sorted_pri:
            t = _ticker_from_any((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if t and t not in urgent_priority:
                urgent_priority.append(t)
            push(t, "priority_candidate", 100)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        for t in _paper_open_tickers(limit=80):
            push(t, "paper_open", 95)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        # v199: 직전 target을 TTL 동안 유지한다. 재구독 반복보다 후보 신선수신 유지가 우선이다.
        kept_old = 0
        for t in old_targets:
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break
            prev = old_meta.get(t, {})
            last_seen = fnum(prev.get("last_seen") or old_updated, old_updated)
            if last_seen > 0 and nowv - last_seen <= WS_TARGET_KEEP_TTL_SEC:
                push(t, "ttl_keep", 50)
                kept_old += 1

        scored = sorted(rows or [], key=lambda r: (
            fnum((r or {}).get("score"), 0),
            fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
            fnum((r or {}).get("turnover_24h"), 0),
        ), reverse=True)
        for r in scored:
            push((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"), "scored_row", 30)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        for t in list(MAJOR_WATCH_TICKERS) + ["BTC", "ETH", "XRP"]:
            push(t, "major_watch", 10)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        ranked = sorted(rows or [], key=lambda r: (fnum(r.get("turnover_rank"), 9999), -abs(fnum(r.get("change_24h"), 0))))
        for r in ranked:
            push((r or {}).get("ticker"), "rank_fill", 5)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        ticks = ticks[:max(10, WS_HUB_MAX_TICKERS)]
        meta = {t: meta.get(t, {"first_seen": nowv, "last_seen": nowv, "source": "unknown", "priority": 0}) for t in ticks}
        changed, added, removed = _ws_target_changed_enough(old_targets, ticks)
        urgent_missing = [t for t in urgent_priority if t and t not in set(old_targets)]
        since_write = nowv - old_updated if old_updated > 0 else 9999.0
        urgent_due = bool(urgent_missing) and since_write >= WS_TARGET_URGENT_REWRITE_SEC
        routine_due = changed and since_write >= WS_TARGET_REWRITE_MIN_SEC
        should_write = not path.exists() or not old_targets or urgent_due or routine_due
        write_note = "write" if should_write else "skip_same_or_debounce"

        payload = {
            "version": BOT_VERSION,
            "updated_ts": nowv if should_write else old_updated,
            "reason": reason,
            "max_tickers": WS_HUB_MAX_TICKERS,
            "targets": ticks,
            "target_meta": meta,
            "priority_count": len(merged_priority),
            "paper_open_count": len(_paper_open_tickers(limit=80)),
            "priority_first": True,
            "ttl_keep_sec": WS_TARGET_KEEP_TTL_SEC,
            "rewrite_min_sec": WS_TARGET_REWRITE_MIN_SEC,
            "changed": changed,
            "added": added,
            "removed": removed,
            "urgent_missing": urgent_missing[:12],
            "kept_old": kept_old,
            "write_note": write_note,
            "note": "v201: wide WS target list with TTL/debounce; missing/stale info is promoted to collection target, not used as a hard block",
        }
        if should_write:
            try:
                atomic_write(path, payload)
            except Exception as exc:
                log_error("write_ws_targets", exc)
        else:
            # 파일을 덮지 않아 sidecar 재구독을 만들지 않는다. 메모리 상태는 최신 후보목록으로 맞춘다.
            payload["updated_ts"] = old_updated
        with _ws_lock:
            _ws_targets[:] = ticks
        with _state_lock:
            STATE["ws_targets"] = len(ticks)
            STATE["ws_target_file_written"] = nowv if should_write else old_updated
            STATE["ws_target_file_targets"] = len(ticks)
            STATE["ws_target_reason"] = reason
            STATE["ws_target_priority_first"] = True
            STATE["ws_target_write_note"] = write_note
            STATE["ws_target_changed"] = changed
            STATE["ws_target_added"] = added
            STATE["ws_target_removed"] = removed
            STATE["ws_target_urgent_missing"] = len(urgent_missing)
            STATE["ws_target_kept_old"] = kept_old
    except Exception as exc:
        log_error("update_ws_targets", exc)


def mark_ws_target_flags(rows: List[Dict[str, Any]]) -> None:
    """v192: clean_ws_targets 갱신 직후 후보 row에 'WS 대상 포함 여부'를 보존한다.

    원인: scan 초반 apply_ws_cache_to_rows()가 붙인 ws_targeted 값은 hub_rank 기준의
    이전 target일 수 있다. 이후 6차 직원이 strict/paper_OPEN 후보를 clean_ws_targets에
    올려도 후보파일에는 예전 ws_targeted=False가 저장되어 /quality에 '대상 0/N'으로
    보였다. 이 함수는 sidecar 수신을 기다리는 차단조건이 아니라, 허브가 이번 scan에서
    실제로 쓴 WS 대상목록을 후보파일에 정확히 남기는 표시/배관 보정이다.
    """
    try:
        with _ws_lock:
            target_set = {_ticker_from_any(x) for x in _ws_targets if _ticker_from_any(x)}
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            t = _ticker_from_any(r.get("ticker") or r.get("market") or r.get("symbol"))
            r["ws_targeted"] = bool(t and t in target_set)
            ctx = r.get("entry_context")
            if isinstance(ctx, dict):
                ctx["ws_targeted"] = r["ws_targeted"]
                ctx["ws_target_reason"] = STATE.get("ws_target_reason", "-")
                ctx["ws_target_file_targets"] = STATE.get("ws_target_file_targets", 0)
            r["ws_target_reason"] = STATE.get("ws_target_reason", "-")
    except Exception as exc:
        log_error("mark_ws_target_flags", exc)

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
    """v181 원인제거 유지: 메인봇은 웹소켓 연결을 직접 열지 않는다.

    웹소켓은 별도 ws_sidecar 프로세스가 clean_ws_live_cache.json에 기록한다.
    메인봇은 그 파일을 빠르게 읽기만 하므로, sidecar가 멈추거나 웹소켓 handshake가
    오래 걸려도 tradingbot systemd stop/restart는 절대 막히지 않는다.
    """
    with _state_lock:
        STATE["ws_state"] = "외부대기"
        STATE["ws_enabled"] = False
        STATE["ws_requested"] = WS_HUB_REQUESTED
        STATE["ws_targets"] = len(_ws_targets)
        STATE["ws_cached"] = 0
        STATE["ws_fresh"] = 0
        STATE["ws_last_age_sec"] = -1
        STATE["ws_worker_mode"] = "external_sidecar_cache"
        STATE["ws_last_error"] = WS_BLOCK_REASON
    log("websocket_hub_worker uses external sidecar cache only; no websocket connection in main bot")
    return


def _normalize_ws_cache_payload(payload: Any) -> Dict[str, Dict[str, Any]]:
    """sidecar/legacy 캐시 파일을 내부 ticker -> row 형태로 정규화한다."""
    if not isinstance(payload, dict):
        return {}
    candidates = payload.get("rows") or payload.get("cache") or payload.get("prices") or payload
    if isinstance(candidates, list):
        out: Dict[str, Dict[str, Any]] = {}
        for row in candidates:
            if isinstance(row, dict):
                t = ws_plain_ticker(row.get("ticker") or row.get("symbol") or row.get("code") or row.get("market"))
                if t:
                    out[t] = dict(row)
        return out
    if not isinstance(candidates, dict):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, row in candidates.items():
        if isinstance(row, dict):
            t = ws_plain_ticker(row.get("ticker") or row.get("symbol") or row.get("code") or key)
            if t:
                rr = dict(row)
                rr.setdefault("ticker", t)
                out[t] = rr
    return out


def refresh_external_ws_cache() -> None:
    """외부 sidecar가 쓴 캐시/상태 파일을 읽어 STATE와 _ws_live_cache에 반영한다."""
    nowv = now_ts()
    status = load_json(FILES.get("ws_sidecar_status", BASE_DIR / "clean_ws_sidecar_status.json"), {})
    cache_payload = load_json(FILES["ws_cache"], {})
    rows = _normalize_ws_cache_payload(cache_payload)
    fresh_rows = 0
    with _ws_lock:
        _ws_live_cache.clear()
        for t, row in rows.items():
            price = first_number(row, ["live_price", "price", "trade_price", "current_price", "closePrice"], 0.0)
            ts = fnum(row.get("ts") or row.get("updated_ts") or row.get("time") or (cache_payload.get("updated_ts") if isinstance(cache_payload, dict) else 0), 0.0)
            if price <= 0 or ts <= 0:
                continue
            rr = dict(row)
            rr["ticker"] = t
            rr["live_price"] = price
            rr["ts"] = ts
            _ws_live_cache[t] = rr
            if nowv - ts <= WS_HUB_STALE_SEC:
                fresh_rows += 1
    status_ts = fnum(status.get("updated_ts"), 0.0) if isinstance(status, dict) else 0.0
    status_age = nowv - status_ts if status_ts > 0 else -1
    state = "외부없음"
    raw_error = str(status.get("last_error") or "-") if isinstance(status, dict) else "-"
    raw_notice = str(status.get("last_notice") or "-") if isinstance(status, dict) else "-"
    if _ws_reconnect_notice_text(raw_error):
        raw_notice = raw_error if raw_notice in {"", "-", "None", "none"} else raw_notice
        raw_error = "-"
    reason = raw_error if raw_error not in {"", "-", "None", "none"} else (raw_notice if raw_notice not in {"", "-", "None", "none"} else WS_BLOCK_REASON)
    if isinstance(status, dict) and status_ts > 0:
        if status_age <= WS_SIDECAR_STATUS_STALE_SEC:
            state = "외부수신" if fresh_rows > 0 else "외부대기"
        else:
            state = "외부오래됨"
    elif rows:
        state = "외부캐시" if fresh_rows > 0 else "외부캐시오래됨"
    with _state_lock:
        STATE["ws_state"] = state
        STATE["ws_requested"] = WS_HUB_REQUESTED
        STATE["ws_worker_mode"] = "external_sidecar_cache"
        STATE["ws_targets"] = int(status.get("targets", len(_ws_targets)) if isinstance(status, dict) else len(_ws_targets))
        STATE["ws_cached"] = len(_ws_live_cache)
        STATE["ws_fresh"] = fresh_rows
        STATE["ws_last_age_sec"] = round(min([nowv - fnum(r.get("ts"), nowv) for r in _ws_live_cache.values()], default=-1), 1) if _ws_live_cache else -1
        STATE["ws_sidecar_age_sec"] = round(status_age, 1) if status_age >= 0 else -1
        STATE["ws_sidecar_pid"] = status.get("pid", "-") if isinstance(status, dict) else "-"
        STATE["ws_sidecar_version"] = status.get("version", "-") if isinstance(status, dict) else "-"
        STATE["ws_raw_total"] = status.get("raw_total", STATE.get("ws_raw_total",0)) if isinstance(status, dict) else STATE.get("ws_raw_total",0)
        STATE["ws_parse_ok"] = status.get("parse_ok", STATE.get("ws_parse_ok",0)) if isinstance(status, dict) else STATE.get("ws_parse_ok",0)
        STATE["ws_price_ok"] = status.get("price_ok", STATE.get("ws_price_ok",0)) if isinstance(status, dict) else STATE.get("ws_price_ok",0)
        STATE["ws_amount_ok"] = status.get("amount_ok", STATE.get("ws_amount_ok",0)) if isinstance(status, dict) else STATE.get("ws_amount_ok",0)
        STATE["ws_match_ok"] = status.get("match_ok", STATE.get("ws_match_ok",0)) if isinstance(status, dict) else STATE.get("ws_match_ok",0)
        STATE["ws_last_format"] = status.get("last_format", STATE.get("ws_last_format","-")) if isinstance(status, dict) else STATE.get("ws_last_format","-")
        STATE["ws_last_error"] = raw_error
        STATE["ws_last_notice"] = raw_notice
        STATE["ws_status_reason"] = reason[:160]


def ws_snapshot(ticker: Any) -> Dict[str, Any]:
    t = _ticker_from_any(ticker)
    nowv = now_ts()
    with _ws_lock:
        target_set = set(_ticker_from_any(x) for x in _ws_targets)
        row = dict(_ws_live_cache.get(t) or {})
    if not row:
        return {
            "live_price_source": "REST",
            "live_price": 0.0,
            "live_age_sec": -1,
            "ws_age_sec": -1,
            "ws_fresh": False,
            "ws_row_status": "missing",
            "ws_targeted": t in target_set,
            "ws_cache_ts": 0.0,
        }
    cache_ts = fnum(row.get("ts"), 0.0)
    age = nowv - cache_ts if cache_ts > 0 else -1
    fresh = 0 <= age <= WS_HUB_STALE_SEC
    status = "fresh" if fresh else ("stale" if age >= 0 else "missing")
    return {
        "live_price_source": "WS_SIDECAR" if fresh else "WS_STALE",
        "live_price": fnum(row.get("live_price"), 0.0),
        "live_age_sec": round(age, 2) if age >= 0 else -1,
        "ws_age_sec": round(age, 2) if age >= 0 else -1,
        "ws_fresh": fresh,
        "ws_row_status": status,
        "ws_targeted": t in target_set,
        "ws_cache_ts": cache_ts,
        "ws_turnover": fnum(row.get("ws_turnover") or row.get("turnover") or row.get("amount"), 0.0),
        "ws_volume": fnum(row.get("ws_volume") or row.get("volume") or row.get("quantity"), 0.0),
    }


def apply_ws_cache_to_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    refresh_external_ws_cache()
    out = []
    fresh = 0
    stale = 0
    missing = 0
    for r in rows or []:
        rr = dict(r)
        ws = ws_snapshot(rr.get("ticker"))
        rr.update(ws)
        status = str(ws.get("ws_row_status") or "missing")
        if status == "fresh" and fnum(ws.get("live_price"), 0) > 0:
            fresh += 1
            rest_price = fnum(rr.get("current_price"), 0)
            live = fnum(ws.get("live_price"), 0)
            rr["ws_price"] = live
            rr["current_price_ws_gap_pct"] = round(((live - rest_price) / rest_price) * 100.0, 3) if rest_price > 0 else 0.0
        elif status == "stale":
            stale += 1
            rr["current_price_ws_gap_pct"] = 0.0
        else:
            missing += 1
            rr["current_price_ws_gap_pct"] = 0.0
        rr["ws_age_sec"] = fnum(rr.get("ws_age_sec", rr.get("live_age_sec", -1)), -1)
        out.append(rr)
    with _state_lock:
        STATE["ws_fresh"] = fresh
        STATE["ws_stale_rows"] = stale
        STATE["ws_missing_rows"] = missing
    return out


def _normalize_micro_cache_payload(payload: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    candidates = payload.get("rows") or payload.get("cache") or payload
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(candidates, dict):
        for key, row in candidates.items():
            if isinstance(row, dict):
                t = _ticker_from_any(row.get("ticker") or row.get("symbol") or key)
                if t:
                    rr = dict(row)
                    rr.setdefault("ticker", t)
                    out[t] = rr
    elif isinstance(candidates, list):
        for row in candidates:
            if isinstance(row, dict):
                t = _ticker_from_any(row.get("ticker") or row.get("symbol"))
                if t:
                    rr = dict(row)
                    rr.setdefault("ticker", t)
                    out[t] = rr
    return out


def refresh_micro_cache() -> None:
    """빗썸 미세구조 sidecar 캐시를 읽는다. 메인봇은 API를 직접 때리지 않는다."""
    nowv = now_ts()
    status = load_json(FILES.get("micro_status", BASE_DIR / "clean_bithumb_micro_status.json"), {})
    payload = load_json(FILES.get("micro_cache", BASE_DIR / "clean_bithumb_micro_cache.json"), {})
    rows = _normalize_micro_cache_payload(payload)
    fresh = 0
    with _micro_lock:
        _micro_cache.clear()
        for t, row in rows.items():
            ts = fnum(row.get("ts") or row.get("updated_ts") or payload.get("updated_ts") if isinstance(payload, dict) else 0, 0.0)
            if ts <= 0:
                continue
            rr = dict(row)
            rr["ticker"] = t
            rr["ts"] = ts
            _micro_cache[t] = rr
            if nowv - ts <= MICRO_STALE_SEC:
                fresh += 1
    sts = fnum(status.get("updated_ts"), 0.0) if isinstance(status, dict) else 0.0
    age = nowv - sts if sts > 0 else -1
    with _state_lock:
        STATE["micro_state"] = str(status.get("state") or ("수집중" if fresh else "대기")) if isinstance(status, dict) else "대기"
        STATE["micro_targets"] = int(status.get("targets", len(_micro_targets)) if isinstance(status, dict) else len(_micro_targets))
        STATE["micro_cached"] = len(_micro_cache)
        STATE["micro_fresh"] = fresh
        STATE["micro_status_age_sec"] = round(age, 1) if age >= 0 else -1
        STATE["micro_last_error"] = str(status.get("last_error") or "-")[:160] if isinstance(status, dict) else "-"
        STATE["micro_orderbook_ok"] = int(status.get("orderbook_ok", 0) if isinstance(status, dict) else 0)
        STATE["micro_trade_ok"] = int(status.get("trade_ok", 0) if isinstance(status, dict) else 0)


def micro_snapshot(ticker: Any) -> Dict[str, Any]:
    t = _ticker_from_any(ticker)
    nowv = now_ts()
    with _micro_lock:
        target_set = set(_ticker_from_any(x) for x in _micro_targets)
        row = dict(_micro_cache.get(t) or {})
    if not row:
        return {"micro_fresh": False, "micro_row_status": "missing", "micro_targeted": t in target_set, "micro_age_sec": -1}
    age = nowv - fnum(row.get("ts"), nowv)
    fresh = age <= MICRO_STALE_SEC
    return {
        "micro_fresh": bool(fresh),
        "micro_row_status": "fresh" if fresh else "stale",
        "micro_targeted": t in target_set,
        "micro_age_sec": round(age, 2),
        "micro_spread_pct": fnum(row.get("micro_spread_pct"), 999.0),
        "micro_bid_wall_5_krw": fnum(row.get("bid_wall_5_krw"), 0.0),
        "micro_ask_wall_5_krw": fnum(row.get("ask_wall_5_krw"), 0.0),
        "micro_bid_ask_wall_ratio": fnum(row.get("bid_ask_wall_ratio"), 0.0),
        "micro_ask_wall_pressure": bool(row.get("ask_wall_pressure")),
        "micro_trade_buy_krw_30": fnum(row.get("trade_buy_krw_30"), 0.0),
        "micro_trade_sell_krw_30": fnum(row.get("trade_sell_krw_30"), 0.0),
        "micro_trade_buy_ratio_30": fnum(row.get("trade_buy_ratio_30"), 0.0),
        "micro_trade_count_30": int(fnum(row.get("trade_count_30"), 0)),
        "micro_sell_trade_pressure": bool(row.get("sell_trade_pressure")),
        "micro_flags": row.get("micro_flags") if isinstance(row.get("micro_flags"), list) else [],
    }


def recent_candidate_priority_rows(limit: int = 80) -> List[Dict[str, Any]]:
    """직전 후보/진입대상을 다음 scan 초반 micro 대상으로 선반영한다.

    v193에서 micro가 후보 확정 뒤 따라붙어 한 사이클 늦는 문제가 있었다.
    최신 후보파일은 이전 scan의 판단 결과라, 다음 scan 초반 실제 호가·체결 대상을
    먼저 올리는 데 안전하게 쓸 수 있다. 기존 장부/조건은 바꾸지 않는다.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        rows = tail_jsonl(FILES.get("paper_latest", BASE_DIR / "paper_candidates_latest.jsonl"), max_lines=limit)
    except Exception:
        rows = []
    def _rank(r: Dict[str, Any]) -> tuple:
        return (
            bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready")),
            str(r.get("final_entry_action") or "") == "recheck_wait",
            fnum(r.get("score"), 0),
            fnum(r.get("money_flow_3m") or r.get("turnover_3m"), 0),
        )
    for r in sorted([x for x in rows if isinstance(x, dict)], key=_rank, reverse=True):
        t = _ticker_from_any(r.get("ticker") or r.get("symbol") or r.get("market"))
        if t and t not in seen:
            seen.add(t)
            out.append(r)
        if len(out) >= limit:
            break
    return out


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:
    """빗썸 호가/체결 직원이 볼 대상.

    v201: 차단보다 빠른 확인이 우선이다.
    - 진입후보/재확인/OPEN/직전후보를 넓게 올린다.
    - MICRO_TARGET_MAX는 전략 제한이 아니라 sidecar target 파일 안전상한이다.
    - micro sidecar v0.5가 넓은 target을 우선순위+회전식으로 수집한다.
    """
    try:
        ticks: List[str] = []
        seen: set = set()
        previous_priority = recent_candidate_priority_rows(limit=260)
        current_priority = list(priority_rows or [])
        # 1순위: 실제 진입 직전/직전 후보. 점수와 3분돈 흐름이 높은 순으로 먼저 확인한다.
        merged_priority = current_priority + previous_priority
        sorted_pri = sorted(merged_priority, key=lambda r: (
            bool((r or {}).get("paper_bot_open") or (r or {}).get("trade_ready") or (r or {}).get("open_eligible")),
            str((r or {}).get("final_entry_action") or "") in {"paper_open", "trade_ready"},
            str((r or {}).get("final_entry_action") or "") == "recheck_wait",
            fnum((r or {}).get("score"), 0),
            fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
        ), reverse=True)
        for r in sorted_pri:
            _push_unique_ticker(ticks, seen, (r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        # 2순위: 현재 OPEN 보유 코인은 청산/위험 확인용으로 계속 추적한다.
        for t in _paper_open_tickers(limit=80):
            _push_unique_ticker(ticks, seen, t)
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        # 3순위: 현재 전체 row에서 점수가 있거나 거래대금이 높은 후보군.
        scored = sorted(rows or [], key=lambda r: (
            fnum((r or {}).get("score"), 0),
            fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
            fnum((r or {}).get("turnover_24h"), 0),
        ), reverse=True)
        for r in scored:
            _push_unique_ticker(ticks, seen, (r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        # 4순위: 대형/기본 코인은 남는 자리에서만 확인한다. 진입 직전 후보보다 앞서면 안 된다.
        for t in list(MAJOR_WATCH_TICKERS) + ["BTC", "ETH", "XRP"]:
            _push_unique_ticker(ticks, seen, t)
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        # 5순위: 남는 자리는 거래대금 상위로 채운다.
        ranked = sorted(rows or [], key=lambda r: fnum(r.get("turnover_24h"), 0), reverse=True)
        for r in ranked:
            _push_unique_ticker(ticks, seen, (r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        ticks = ticks[:max(8, MICRO_TARGET_MAX)]
        payload = {
            "version": BOT_VERSION,
            "updated_ts": now_ts(),
            "reason": reason,
            "max_tickers": MICRO_TARGET_MAX,
            "targets": ticks,
            "priority_count": len(merged_priority),
            "priority_first": True,
            "note": "v201: wide micro target list; sidecar polls priority candidates first and rotates the rest to reduce missing/stale info",
        }
        atomic_write(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), payload)
        with _micro_lock:
            _micro_targets[:] = ticks
        with _state_lock:
            STATE["micro_target_file_targets"] = len(ticks)
            STATE["micro_target_reason"] = reason
            STATE["micro_target_priority_first"] = True
            STATE["micro_target_file_written"] = now_ts()
    except Exception as exc:
        log_error("update_micro_targets", exc)

def apply_micro_cache_to_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    refresh_micro_cache()
    out: List[Dict[str, Any]] = []
    fresh = stale = missing = 0
    for r in rows or []:
        rr = dict(r)
        ms = micro_snapshot(rr.get("ticker"))
        rr.update(ms)
        status = str(ms.get("micro_row_status") or "missing")
        if status == "fresh":
            fresh += 1
        elif status == "stale":
            stale += 1
        else:
            missing += 1
        out.append(rr)
    with _state_lock:
        STATE["micro_fresh_rows"] = fresh
        STATE["micro_stale_rows"] = stale
        STATE["micro_missing_rows"] = missing
    return out


def websocket_status_line() -> str:
    age = STATE.get("ws_last_age_sec", -1)
    age_txt = f"{age}초" if fnum(age, -1) >= 0 else "-"
    err = STATE.get("ws_last_error") or WS_BLOCK_REASON
    state = str(STATE.get("ws_state") or "외부대기")
    if state == "외부수신":
        icon = "✅"
    elif state in {"외부대기", "외부없음", "외부캐시", "외부캐시오래됨", "-"}:
        icon = "❔"
    else:
        icon = "⚠️"
    req = "요청ON" if STATE.get("ws_requested") else "요청OFF"
    return f"{icon} 웹소켓 허브: {state}({req}) / 대상 {STATE.get('ws_targets',0)} / 캐시 {STATE.get('ws_cached',0)} / 신선 {STATE.get('ws_fresh',0)} / 최근 {age_txt} / 이유 {err}"

def websocket_status_text() -> str:
    refresh_external_ws_cache()
    lines = [
        "🛰 웹소켓 상태 /ws_status",
        websocket_status_line(),
        f"- worker: external_sidecar_cache / pid {STATE.get('ws_sidecar_pid','-')} / sidecar {STATE.get('ws_sidecar_version','-')} / status_age {STATE.get('ws_sidecar_age_sec','-')}초",
        f"- 진단: raw {STATE.get('ws_raw_total',0)} / parse {STATE.get('ws_parse_ok',0)} / price {STATE.get('ws_price_ok',0)} / amount {STATE.get('ws_amount_ok',0)} / match {STATE.get('ws_match_ok',0)} / format {STATE.get('ws_last_format','-')}",
        f"- notice: {STATE.get('ws_last_notice','-')} / error: {STATE.get('ws_last_error','-')}",
        f"- 대상갱신: {STATE.get('ws_target_file_targets',0)}개 / reason {STATE.get('ws_target_reason','-')} / note {STATE.get('ws_target_write_note','-')} / keep {STATE.get('ws_target_kept_old',0)} / missing {STATE.get('ws_missing_rows',0)} / stale {STATE.get('ws_stale_rows',0)}",
        "",
        "판독",
        "- 현재 메인봇은 웹소켓을 직접 실행하지 않고 외부 sidecar 캐시만 읽습니다.",
        "- sidecar가 죽어도 REST 전체시장/정밀/위험 본선은 유지됩니다.",
        "- WS 신선값은 우선 가격 확인 재료로만 쓰고, REST 가격을 즉시 덮어쓰지 않습니다.",
    ]
    return "\n".join(lines)

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




def _sma_last(vals: List[float], period: int) -> float:
    try:
        clean = [float(x) for x in vals if x is not None]
        if len(clean) < period or period <= 0:
            return 0.0
        return sum(clean[-period:]) / float(period)
    except Exception:
        return 0.0


def _ema_last(vals: List[float], period: int) -> float:
    try:
        clean = [float(x) for x in vals if x is not None]
        if len(clean) < period or period <= 0:
            return 0.0
        k = 2.0 / (period + 1.0)
        ema = sum(clean[:period]) / float(period)
        for v in clean[period:]:
            ema = (v * k) + (ema * (1.0 - k))
        return ema
    except Exception:
        return 0.0


def _std_last(vals: List[float], period: int) -> float:
    try:
        clean = [float(x) for x in vals if x is not None]
        if len(clean) < period or period <= 0:
            return 0.0
        win = clean[-period:]
        avg = sum(win) / float(period)
        return math.sqrt(sum((x - avg) ** 2 for x in win) / float(period))
    except Exception:
        return 0.0


def _mfi14(highs: List[float], lows: List[float], closes: List[float], vols: List[float]) -> float:
    try:
        n = min(len(highs), len(lows), len(closes), len(vols))
        if n < 15:
            return 0.0
        h, l, c, v = highs[-15:], lows[-15:], closes[-15:], vols[-15:]
        tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(len(c))]
        pos = neg = 0.0
        for i in range(1, len(tp)):
            mf = tp[i] * max(v[i], 0.0)
            if tp[i] > tp[i-1]:
                pos += mf
            elif tp[i] < tp[i-1]:
                neg += mf
        if neg <= 0:
            return 100.0 if pos > 0 else 0.0
        mr = pos / neg
        return round(100.0 - (100.0 / (1.0 + mr)), 2)
    except Exception:
        return 0.0


def _cci20(highs: List[float], lows: List[float], closes: List[float]) -> float:
    try:
        n = min(len(highs), len(lows), len(closes))
        if n < 20:
            return 0.0
        h, l, c = highs[-20:], lows[-20:], closes[-20:]
        tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(20)]
        sma = sum(tp) / 20.0
        md = sum(abs(x - sma) for x in tp) / 20.0
        if md <= 0:
            return 0.0
        return round((tp[-1] - sma) / (0.015 * md), 2)
    except Exception:
        return 0.0


def _stoch_fast(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Tuple[float, float, bool]:
    try:
        n = min(len(highs), len(lows), len(closes))
        if n < period + 3:
            return 0.0, 0.0, False
        ks: List[float] = []
        for end in range(n - 3, n + 1):
            hh = max(highs[max(0, end-period):end])
            ll = min(lows[max(0, end-period):end])
            cc = closes[end-1]
            k = ((cc - ll) / (hh - ll) * 100.0) if hh > ll else 0.0
            ks.append(max(0.0, min(100.0, k)))
        k_now = ks[-1]
        d_now = sum(ks[-3:]) / min(3, len(ks))
        d_prev = sum(ks[-4:-1]) / 3.0 if len(ks) >= 4 else d_now
        cross_up = bool(ks[-2] <= d_prev and k_now > d_now)
        return round(k_now, 2), round(d_now, 2), cross_up
    except Exception:
        return 0.0, 0.0, False


def _adx14(highs: List[float], lows: List[float], closes: List[float]) -> float:
    try:
        n = min(len(highs), len(lows), len(closes))
        if n < 30:
            return 0.0
        trs: List[float] = []
        plus_dm: List[float] = []
        minus_dm: List[float] = []
        for i in range(1, n):
            up = highs[i] - highs[i-1]
            down = lows[i-1] - lows[i]
            plus_dm.append(up if up > down and up > 0 else 0.0)
            minus_dm.append(down if down > up and down > 0 else 0.0)
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            trs.append(tr)
        period = 14
        dxs: List[float] = []
        for j in range(period, len(trs)+1):
            tr_sum = sum(trs[j-period:j])
            if tr_sum <= 0:
                continue
            plus_di = 100.0 * sum(plus_dm[j-period:j]) / tr_sum
            minus_di = 100.0 * sum(minus_dm[j-period:j]) / tr_sum
            den = plus_di + minus_di
            if den > 0:
                dxs.append(100.0 * abs(plus_di - minus_di) / den)
        if not dxs:
            return 0.0
        return round(sum(dxs[-period:]) / min(period, len(dxs)), 2)
    except Exception:
        return 0.0



def _atr_pct(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """1분봉 기준 ATR 변동성 비율. paper OPEN 직전 검증용 분석값이다."""
    try:
        n = min(len(highs), len(lows), len(closes))
        if n < period + 1:
            return 0.0
        trs: List[float] = []
        for i in range(1, n):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            trs.append(tr)
        if len(trs) < period or closes[-1] <= 0:
            return 0.0
        atr = sum(trs[-period:]) / float(period)
        return round((atr / max(closes[-1], 1e-9)) * 100.0, 4)
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
    v1 = sum(vols[-1:]) if vols else 0.0
    v3 = sum(vols[-3:]) if len(vols) >= 3 else sum(vols)
    v5 = sum(vols[-5:]) if len(vols) >= 5 else sum(vols)
    prev20 = vols[-25:-5] if len(vols) >= 25 else vols[:-5]
    prev_avg = (sum(prev20) / len(prev20)) if prev20 else 0.0
    recent_avg = (sum(vols[-5:]) / min(5, len(vols))) if vols else 0.0
    vol_ratio = (recent_avg / prev_avg) if prev_avg > 0 else 0.0
    low30 = min(lows[-31:]) if lows else 0.0
    high30 = max(highs[-31:]) if highs else 0.0
    turnover_1m = v1 * current
    turnover_3m = v3 * current
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

    # v174: 무료 공개전략에서 공통으로 보이는 보조지표를 매매조건이 아닌 분석 재료로 계산한다.
    ema5 = _ema_last(closes, 5)
    ema12 = _ema_last(closes, 12)
    ema21 = _ema_last(closes, 21)
    ema5_gap_pct = round(((current - ema5) / max(ema5, 1e-9)) * 100.0, 3) if ema5 > 0 else 0.0
    ema12_gap_pct = round(((current - ema12) / max(ema12, 1e-9)) * 100.0, 3) if ema12 > 0 else 0.0
    ema21_gap_pct = round(((current - ema21) / max(ema21, 1e-9)) * 100.0, 3) if ema21 > 0 else 0.0
    bb_mid = _sma_last(closes, 20)
    bb_std = _std_last(closes, 20)
    bb_upper = bb_mid + (bb_std * 2.0) if bb_mid > 0 else 0.0
    bb_lower = bb_mid - (bb_std * 2.0) if bb_mid > 0 else 0.0
    bb_width_pct = round(((bb_upper - bb_lower) / max(bb_mid, 1e-9)) * 100.0, 3) if bb_mid > 0 and bb_upper > bb_lower else 0.0
    bb_pos = round((current - bb_lower) / max(bb_upper - bb_lower, 1e-9), 4) if bb_upper > bb_lower else 0.0
    bb_lower_gap_pct = round(((current - bb_lower) / max(bb_lower, 1e-9)) * 100.0, 3) if bb_lower > 0 else 0.0
    bb_middle_gap_pct = round(((current - bb_mid) / max(bb_mid, 1e-9)) * 100.0, 3) if bb_mid > 0 else 0.0
    bb_upper_gap_pct = round(((bb_upper - current) / max(bb_upper, 1e-9)) * 100.0, 3) if bb_upper > 0 else 0.0
    bb_lower_touch = bool(bb_lower > 0 and current <= bb_lower * 1.003)
    mfi_14 = _mfi14(highs, lows, closes, vols)
    cci_20 = _cci20(highs, lows, closes)
    stoch_k, stoch_d, stoch_cross_up = _stoch_fast(highs, lows, closes, 14)
    adx_14 = _adx14(highs, lows, closes)
    avg_prices = []
    try:
        for c in candles:
            op = fnum(c.get("open"), 0); hi = fnum(c.get("high"), 0); lo = fnum(c.get("low"), 0); cl = fnum(c.get("close"), 0)
            if op > 0 and hi > 0 and lo > 0 and cl > 0:
                avg_prices.append((op + hi + lo + cl) / 4.0)
    except Exception:
        avg_prices = []
    avg_price_turn_pct = 0.0
    v_rebound_score = 0.0
    try:
        if len(avg_prices) >= 6:
            avg_price_turn_pct = round(((avg_prices[-1] - avg_prices[-2]) / max(avg_prices[-2], 1e-9)) * 100.0, 3)
            falling = avg_prices[-6] > avg_prices[-5] > avg_prices[-4] > avg_prices[-3] > avg_prices[-2]
            if falling and avg_prices[-1] > avg_prices[-2]:
                v_rebound_score = round(min(3.0, max(0.0, avg_price_turn_pct) * 2.0), 3)
    except Exception:
        avg_price_turn_pct = 0.0
        v_rebound_score = 0.0
    # 위 한 줄의 안전성 때문에 아래에서 실제 rsi/mfi/cci 조건을 보강한다.
    if v_rebound_score > 0 and (mfi_14 < 35 or cci_20 < -80 or _rsi14(closes) < 35):
        v_rebound_score = round(min(3.0, v_rebound_score + 0.5), 3)
    prev30_vol = vols[-35:-5] if len(vols) >= 35 else vols[:-5]
    prev30_avg = (sum(prev30_vol) / len(prev30_vol)) if prev30_vol else 0.0
    latest_vol = vols[-1] if vols else 0.0
    volume_spike_30x = round(latest_vol / prev30_avg, 3) if prev30_avg > 0 else 0.0
    volume_pump_risk = bool(volume_spike_30x >= 20.0)
    # v187 즉시수정: v186에서는 ch3 정의 전에 ATR 비교값을 계산해
    # build_precision 전체가 UnboundLocalError로 실패했고, 정밀/EMA/BB/MFI/ATR이 0으로 고착됐다.
    # ATR 분석값은 정밀직원 본선이 아니라 보조 검증값이므로, 계산 실패가 정밀값 전체를 죽이지 않게 격리한다.
    try:
        atr_1m_pct = _atr_pct(highs, lows, closes, 14)
        atr_3m_pct = round(atr_1m_pct * math.sqrt(3.0), 4) if atr_1m_pct > 0 else 0.0
        move3_for_atr = round(pct_change_from(closes, 3, current), 3)
        current_move_vs_atr = round(abs(move3_for_atr) / max(atr_3m_pct, 1e-9), 3) if atr_3m_pct > 0 else 0.0
    except Exception:
        atr_1m_pct = 0.0
        atr_3m_pct = 0.0
        current_move_vs_atr = 0.0

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

    ch1 = round(pct_change_from(closes, 1, current), 3)
    ch3 = round(pct_change_from(closes, 3, current), 3)
    ch5 = round(pct_change_from(closes, 5, current), 3)
    ch15 = round(pct_change_from(closes, 15, current), 3)
    ch30 = round(pct_change_from(closes, 30, current), 3)
    from_low_pct = round(((current - low30) / low30) * 100.0, 3) if low30 > 0 else 0.0
    below_high_pct = round(((high30 - current) / high30) * 100.0, 3) if high30 > 0 else 999.0
    pullback_depth_pct = below_high_pct if below_high_pct < 900 else 0.0
    low_defense_pct = from_low_pct
    recovery_speed_pct = round(max(ch1, ch3, ch5), 3)
    rebreakout_strength = round(max(0.0, recovery_speed_pct) + max(0.0, min(vol_ratio, 4.0) - 1.0) * 0.45 + (0.25 if close_pos >= 0.55 else 0.0), 3)
    fake_bounce_score = 0.0
    if vol_ratio >= 1.5 and recovery_speed_pct < 0.05:
        fake_bounce_score += 1.0
    if close_pos < 0.45 and upper_wick >= 0.55:
        fake_bounce_score += 0.7
    if from_low_pct > 6.5 and below_high_pct < 1.0:
        fake_bounce_score += 0.5
    pullback_quality_score = round(
        max(0.0, min(2.0, pullback_depth_pct))
        + max(0.0, min(2.0, low_defense_pct)) * 0.30
        + max(0.0, recovery_speed_pct) * 0.50
        + (0.35 if close_pos >= 0.55 else 0.0)
        - fake_bounce_score * 0.40,
        3,
    )
    real_money_label = "real_1m_3m_5m" if turnover_1m > 0 else ("real_5m" if turnover_5m > 0 else "실제0또는미세")

    return {
        "ticker": ticker,
        "precision_ok": True,
        "precision_ts": now_ts(),
        "precision_elapsed": round(now_ts() - started, 3),
        "candles_1m": len(closes),
        "change_1": ch1,
        "change_3": ch3,
        "change_5": ch5,
        "change_15": ch15,
        "change_30": ch30,
        "vol_ratio": round(vol_ratio, 3),
        "turnover_1m": round(turnover_1m, 2),
        "turnover_3m": round(turnover_3m, 2),
        "turnover_5m": round(turnover_5m, 2),
        "money_flow_1m": round(turnover_1m, 2),
        "money_flow_3m": round(turnover_3m, 2),
        "money_flow_5m": round(turnover_5m, 2),
        "money_flow": round(turnover_5m, 2),
        "money_status": real_money_label,
        "real_money_flow_status": real_money_label,
        "from_30m_low_pct": from_low_pct,
        "below_30m_high_pct": below_high_pct,
        "recent_30m_high_price": high30,
        "recent_30m_low_price": low30,
        "pullback_depth_pct": pullback_depth_pct,
        "low_defense_pct": low_defense_pct,
        "recovery_speed_pct": recovery_speed_pct,
        "rebreakout_strength": rebreakout_strength,
        "fake_bounce_score": round(fake_bounce_score, 3),
        "pullback_quality_score": pullback_quality_score,
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
        "ema5_gap_pct": ema5_gap_pct,
        "ema12_gap_pct": ema12_gap_pct,
        "ema21_gap_pct": ema21_gap_pct,
        "bb_position": bb_pos,
        "bb_width_pct": bb_width_pct,
        "bb_lower_gap_pct": bb_lower_gap_pct,
        "bb_middle_gap_pct": bb_middle_gap_pct,
        "bb_upper_gap_pct": bb_upper_gap_pct,
        "bb_lower_touch": bb_lower_touch,
        "mfi_14": mfi_14,
        "cci_20": cci_20,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "stoch_cross_up": stoch_cross_up,
        "adx_14": adx_14,
        "atr_1m_pct": atr_1m_pct,
        "atr_3m_pct": atr_3m_pct,
        "current_move_vs_atr": current_move_vs_atr,
        "avg_price_turn_pct": avg_price_turn_pct,
        "v_rebound_score": v_rebound_score,
        "volume_spike_30x": volume_spike_30x,
        "volume_pump_risk": volume_pump_risk,
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


def sanitize_spread_pct(v: Any) -> Tuple[float, str]:
    """호가 스프레드 값을 신뢰 가능한 범위로 정리한다.
    0~5% 밖은 실전위험 판단값이 아니라 정보오류/호가이상으로 분리한다.
    """
    x = fnum(v, 999.0)
    if x < 0 or x >= 900:
        return 999.0, "정보없음"
    if x > SPREAD_ABNORMAL_PCT:
        return 999.0, f"호가이상값 {x:.2f}%"
    return x, "정상"

def entry_value(item: Dict[str, Any], key: str, default: Any = None, requires_precision: bool = False, requires_risk: bool = False, max_abs: Optional[float] = None) -> Any:
    if requires_precision and str(item.get("precision_source") or "") != "candlestick_1m":
        return None
    if requires_risk and str(item.get("execution_risk_status") or "") != "확인완료":
        return None
    if key not in item or item.get(key) in (None, ""):
        return default
    v = fnum(item.get(key), default if default is not None else 0)
    if max_abs is not None and abs(v) > max_abs:
        return None
    return v


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
                spread, _note = sanitize_spread_pct(((ask - bid) / mid) * 100.0)
                return spread
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
    if abs(price_recheck_pct) > PRICE_RECHECK_ABNORMAL_PCT:
        price_recheck_pct = 0.0
        price_recheck_note = "가격재확인 이상값"
    else:
        price_recheck_note = "정상"
    raw_spread = fetch_orderbook_spread_pct(t, timeout=1.3)
    spread, spread_note = sanitize_spread_pct(raw_spread)
    tick_pct = approximate_tick_pct(live or detected)
    flags = []
    if price_recheck_pct >= 0.50:
        flags.append("알림가 대비 급등")
    if spread_note != "정상" and spread_note != "정보없음":
        flags.append(spread_note)
    elif spread >= 0.25 and spread < 900:
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
        "orderbook_spread_status": spread_note,
        "price_recheck_status": price_recheck_note,
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
        "orderbook_spread_status": "정보없음",
        "price_recheck_status": "정보없음",
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



def quality_risk_tags(item: Dict[str, Any]) -> List[str]:
    """v184 후보품질 관찰 라벨.

    매수조건/차단조건이 아니다. slow_no_progress/stop_loss가 많이 난 재료를
    다음 CLOSED 분석에서 바로 비교하기 위한 태그만 남긴다.
    """
    tags: List[str] = []
    price_recheck = fnum(item.get("price_recheck_pct"), 0.0)
    mf1 = fnum(item.get("money_flow_1m") or item.get("turnover_1m"), 0.0)
    mf3 = fnum(item.get("money_flow_3m") or item.get("turnover_3m"), 0.0)
    spike = fnum(item.get("volume_spike_30x"), 0.0)
    pullback = fnum(item.get("pullback_quality_score"), 0.0)
    rebreak = fnum(item.get("rebreakout_strength"), 0.0)
    spread = fnum(item.get("orderbook_spread_pct"), 999.0)
    if price_recheck < -0.03:
        tags.append("가격재확인 음수")
    if mf1 > 0 and mf3 > 0 and mf3 < mf1 * 2.2:
        tags.append("1분 위주")
    if spike >= 2.8:
        tags.append("거래량 과폭발")
    if pullback > 0 and pullback < 1.45:
        tags.append("눌림품질 약함")
    if rebreak > 0 and rebreak < 1.20:
        tags.append("재돌파힘 약함")
    if 0 <= spread <= SPREAD_ABNORMAL_PCT and spread > TRADE_READY_MAX_SPREAD_PCT:
        tags.append("스프레드 주의")
    return tags[:6]


def sync_execution_risk_for_trade_ready_candidates(items: List[Dict[str, Any]]) -> int:
    """v184: trade_ready 0의 1차 원인이 '실전위험 확인중'으로 몰릴 때,
    점수가 이미 trade_ready권인 strict 후보만 scan 안에서 짧게 우선 확인한다.

    전략 점수/청산/자동매수 조건을 바꾸지 않는다. 늦게 채워지던 호가·가격재확인
    값을 후보파일 작성 전에 채워 paper_bot OPEN 판단이 같은 scan에서 가능하게 하는 배관 보강이다.
    """
    if not items or EXEC_RISK_SYNC_TOP_N <= 0:
        with _state_lock:
            STATE["execution_risk_sync_checked"] = 0
            STATE["execution_risk_sync_note"] = "대상없음"
        return 0
    targets: List[Dict[str, Any]] = []
    for item in sorted(items, key=lambda x: (fnum(x.get("score"), 0), fnum(x.get("money_flow_3m") or x.get("turnover_3m"), 0)), reverse=True):
        if item.get("major_watch"):
            continue
        if fnum(item.get("score"), 0) < EXEC_RISK_SYNC_MIN_SCORE:
            continue
        if str(item.get("execution_risk_status") or "") == "확인완료":
            continue
        targets.append(item)
        if len(targets) >= EXEC_RISK_SYNC_TOP_N:
            break
    if not targets:
        with _state_lock:
            STATE["execution_risk_sync_checked"] = 0
            STATE["execution_risk_sync_note"] = "점수권 대기후보 없음"
        return 0

    done = 0
    def _one(item: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        t = str(item.get("ticker") or "").upper()
        risk = build_execution_risk(item)
        return t, risk

    try:
        with ThreadPoolExecutor(max_workers=max(1, min(EXEC_RISK_SYNC_WORKERS, len(targets)))) as ex:
            futs = [ex.submit(_one, item) for item in targets]
            by_t = {str(item.get("ticker") or "").upper(): item for item in targets}
            for fut in as_completed(futs):
                try:
                    t, risk = fut.result()
                    item = by_t.get(t)
                    if item is not None:
                        item.update(risk)
                        item["execution_risk_sync"] = True
                        item["quality_risk_tags"] = quality_risk_tags(item)
                        with _execution_risk_lock:
                            _execution_risk_cache[t] = dict(risk)
                            _execution_risk_queued.discard(t)
                        done += 1
                except Exception as exc:
                    log_error("execution_risk_sync", exc)
    finally:
        for item in items:
            if not item.get("quality_risk_tags"):
                item["quality_risk_tags"] = quality_risk_tags(item)
        with _state_lock:
            STATE["execution_risk_sync_checked"] = done
            STATE["execution_risk_sync_note"] = f"점수권 우선확인 {done}/{len(targets)}"
            STATE["execution_risk_cached"] = len(_execution_risk_cache)
    return done



def build_market_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """v186 6차 직원용 시장장세 요약. 전체시장 스캔은 유지하고 OPEN 판단 보조값만 만든다."""
    try:
        total = len(rows or [])
        up = sum(1 for r in rows or [] if fnum(r.get("change_5"), fnum(r.get("change_24h"), 0)) > 0)
        down = sum(1 for r in rows or [] if fnum(r.get("change_5"), fnum(r.get("change_24h"), 0)) < 0)
        ranked = sorted(rows or [], key=lambda r: fnum(r.get("turnover_24h"), 0), reverse=True)[:80]
        top_up = sum(1 for r in ranked if fnum(r.get("change_5"), fnum(r.get("change_24h"), 0)) > 0)
        def row_for(t: str) -> Dict[str, Any]:
            for r in rows or []:
                if str(r.get("ticker") or "").upper() == t:
                    return r
            return {}
        btc = row_for("BTC")
        eth = row_for("ETH")
        ctx = {
            "market_total": total,
            "market_up_ratio": round((up / total) * 100.0, 2) if total else 0.0,
            "market_down_ratio": round((down / total) * 100.0, 2) if total else 0.0,
            "top_money_up_ratio": round((top_up / len(ranked)) * 100.0, 2) if ranked else 0.0,
            "btc_change_1": fnum(btc.get("change_1"), 0),
            "btc_change_3": fnum(btc.get("change_3"), 0),
            "btc_change_5": fnum(btc.get("change_5"), fnum(btc.get("change_24h"), 0)),
            "eth_change_1": fnum(eth.get("change_1"), 0),
            "eth_change_3": fnum(eth.get("change_3"), 0),
            "eth_change_5": fnum(eth.get("change_5"), fnum(eth.get("change_24h"), 0)),
        }
        weak = ctx["market_up_ratio"] < FINAL_MARKET_UP_WEAK and ctx["top_money_up_ratio"] < FINAL_MARKET_UP_WEAK
        leaders_down = ctx["btc_change_3"] < -0.05 and ctx["eth_change_3"] < -0.05
        ctx["market_pressure"] = "주의" if (weak or leaders_down) else "보통"
        return ctx
    except Exception as exc:
        log_error("build_market_context", exc)
        return {"market_pressure": "확인중"}


def final_entry_check(item: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    """6차 최종 진입검증 직원.

    후보 생성/전략 점수는 건드리지 않고, paper OPEN 직전에만
    slow형 가짜반응은 관찰, stop형 밀림 후보는 재확인대기로 분리한다.
    """
    if not FINAL_ENTRY_WORKER_ON:
        return {"final_entry_action": "paper_open", "final_entry_label": "최종검증 OFF", "final_entry_reasons": []}
    reasons: List[str] = []
    tags = list(item.get("quality_risk_tags") or quality_risk_tags(item))
    price_recheck = fnum(item.get("price_recheck_pct"), 0.0)
    ws_gap = fnum(item.get("current_price_ws_gap_pct"), 0.0)
    vwap_gap = fnum(item.get("vwap_gap_pct"), 0.0)
    spike = fnum(item.get("volume_spike_30x"), 0.0)
    pullback = fnum(item.get("pullback_quality_score"), 0.0)
    rebreak = fnum(item.get("rebreakout_strength"), 0.0)
    mf1 = fnum(item.get("money_flow_1m") or item.get("turnover_1m"), 0.0)
    mf3 = fnum(item.get("money_flow_3m") or item.get("turnover_3m"), 0.0)
    atr = fnum(item.get("atr_1m_pct"), 0.0)
    spread = fnum(item.get("orderbook_spread_pct"), 999.0)
    tick = fnum(item.get("tick_pct_est"), 999.0)
    micro_fresh = bool(item.get("micro_fresh"))
    micro_spread = fnum(item.get("micro_spread_pct"), 999.0)
    micro_buy_ratio = fnum(item.get("micro_trade_buy_ratio_30"), 0.0)
    micro_ask_pressure = bool(item.get("micro_ask_wall_pressure"))
    micro_sell_pressure = bool(item.get("micro_sell_trade_pressure"))
    score = fnum(item.get("score"), 0.0)
    below_high = fnum(item.get("below_30m_high_pct") or item.get("below_high_pct"), 999.0)
    from_low = fnum(item.get("from_30m_low_pct") or item.get("from_low_pct"), 0.0)
    upper_wick = fnum(item.get("current_upper_wick_pct"), 0.0)
    close_pos = fnum(item.get("current_close_pos_ratio"), 1.0)
    market_pressure = str((market or {}).get("market_pressure") or "보통")
    slow_hits = 0
    if spike >= FINAL_SLOW_SPIKE_MIN:
        slow_hits += 1; reasons.append("거래량 과폭발")
    if pullback > 0 and pullback < FINAL_WEAK_PULLBACK_MAX:
        slow_hits += 1; reasons.append("눌림품질 약함")
    if rebreak > 0 and rebreak < FINAL_WEAK_REBREAK_MAX:
        slow_hits += 1; reasons.append("재돌파힘 약함")
    if mf1 > 0 and mf3 > 0 and mf3 < mf1 * 2.2:
        slow_hits += 1; reasons.append("1분 위주")
    # v189: 순간펌핑/알림낚시형. 1분만 튀고 3분 지속이 약하면 slow 관찰로 보낸다.
    if mf1 > 0 and mf3 > 0 and mf3 < mf1 * FINAL_PUMP_MF_RATIO_MAX and spike >= FINAL_MARKET_SOLO_SPIKE_MIN:
        slow_hits += 1; reasons.append("순간펌핑 지속약함")
    stop_hits = 0
    if price_recheck <= FINAL_PRICE_RECHECK_HARD_NEG:
        stop_hits += 2; reasons.append(f"가격재확인 강한음수 {price_recheck:.2f}%")
    elif price_recheck <= FINAL_PRICE_RECHECK_NEG:
        stop_hits += 1; reasons.append(f"가격재확인 음수 {price_recheck:.2f}%")
    if ws_gap <= FINAL_WS_GAP_NEG:
        stop_hits += 1; reasons.append(f"WS가격차 음수 {ws_gap:.2f}%")
    if vwap_gap <= FINAL_VWAP_GAP_NEG:
        stop_hits += 1; reasons.append(f"VWAP 아래 {vwap_gap:.2f}%")
    if 0 <= spread <= SPREAD_ABNORMAL_PCT and spread > TRADE_READY_MAX_SPREAD_PCT:
        stop_hits += 1; reasons.append(f"스프레드 주의 {spread:.2f}%")
    # v193: 실제 호가/체결 미세구조. fresh일 때만 진입 직전 보조판단에 쓴다.
    if micro_fresh and 0 <= micro_spread < 900 and micro_spread >= MICRO_SPREAD_HARD_PCT:
        stop_hits += 1; reasons.append(f"실호가 스프레드 {micro_spread:.2f}%")
    if micro_fresh and micro_ask_pressure and price_recheck <= 0:
        stop_hits += 1; reasons.append("매도벽 두꺼움")
    if micro_fresh and micro_sell_pressure and micro_buy_ratio > 0:
        stop_hits += 1; reasons.append(f"매도체결 우세 {micro_buy_ratio:.2f}")
    if micro_fresh and micro_buy_ratio > 0 and micro_buy_ratio < MICRO_BUY_RATIO_WEAK and spike >= 2.0:
        slow_hits += 1; reasons.append("체결은 약한데 거래량만 튐")
    # v189: 실제 자동매매 관점에서 스프레드/틱/저유동성 조합은 paper OPEN 가치가 낮다.
    if 0 <= spread <= SPREAD_ABNORMAL_PCT and spread >= FINAL_SPREAD_HARD_PCT and mf3 < FINAL_LOW_MONEY_3M:
        slow_hits += 2; reasons.append(f"스프레드넓고 돈흐름약함 {spread:.2f}%")
    if 0 <= tick < 900 and tick > TRADE_READY_MAX_TICK_PCT and mf3 < FINAL_LOW_MONEY_3M:
        slow_hits += 1; reasons.append(f"틱위험+저유동성 {tick:.2f}%")
    # v189: 고점추격 상태에서 진입가가 밀리면 stop 쪽 위험이 크다.
    if below_high <= FINAL_HIGH_CHASE_GAP_PCT and from_low >= FINAL_FROM_LOW_HOT_PCT and price_recheck < 0:
        stop_hits += 2; reasons.append("고점추격+가격밀림")
    # v189: 현재봉 위꼬리/종가위치 약화는 알림 직후 밀림 후보로 재확인한다.
    if upper_wick >= FINAL_UPPER_WICK_PCT and close_pos <= FINAL_CLOSE_POS_WEAK and price_recheck <= 0:
        stop_hits += 1; reasons.append("현재봉 위꼬리/밀림")
    # v189: 점수는 높지만 3분 지속·눌림·재돌파가 빈약하면 점수만 높은 후보로 분리한다.
    if score >= TRADE_READY_MIN_SCORE and mf3 < FINAL_LOW_MONEY_3M and pullback < FINAL_WEAK_PULLBACK_MAX and rebreak < FINAL_WEAK_REBREAK_MAX:
        slow_hits += 2; reasons.append("점수만높고 구조빈약")
    # v189: 시장 약세에서 개별 과폭발만 튄 후보는 단독펌핑으로 본다.
    if market_pressure == "주의" and spike >= FINAL_MARKET_SOLO_SPIKE_MIN and (mf1 > 0 and (mf3 <= 0 or mf3 < mf1 * 2.4)):
        slow_hits += 2; reasons.append("시장약세 단독펌핑")
    if market_pressure == "주의" and (stop_hits or slow_hits >= 2):
        reasons.append("시장장세 약함")
    if atr >= FINAL_ATR_HIGH_PCT and (stop_hits or price_recheck < 0):
        reasons.append(f"변동성 큼 ATR {atr:.2f}%")
    action = "paper_open"
    label = "최종검증 통과"
    if slow_hits >= 3:
        action = "observe"
        label = "관찰전환: 쓰레기후보 slow/펌핑 위험"
    elif stop_hits >= 2 or (price_recheck <= FINAL_PRICE_RECHECK_HARD_NEG and market_pressure == "주의"):
        action = "recheck_wait"
        label = "재확인대기: stop/밀림 위험"
    if action != "paper_open":
        tags.append(label)
    return {
        "final_entry_action": action,
        "final_entry_label": label,
        "final_entry_reasons": list(dict.fromkeys(reasons))[:8],
        "final_slow_hits": slow_hits,
        "final_stop_hits": stop_hits,
        "market_context": market or {},
        "market_pressure": market_pressure,
        "quality_risk_tags": list(dict.fromkeys(tags))[:10],
    }


def apply_final_entry_worker(strict: List[Dict[str, Any]], rows: List[Dict[str, Any]]) -> Dict[str, int]:
    market = build_market_context(rows)
    counts = Counter()
    for item in strict or []:
        try:
            prof = final_entry_check(item, market)
            item.update(prof)
            counts[str(prof.get("final_entry_action") or "unknown")] += 1
        except Exception as exc:
            log_error("final_entry_worker", exc)
            item["final_entry_action"] = "observe"
            item["final_entry_label"] = "최종검증 오류-관찰"
            item["final_entry_reasons"] = [f"{exc.__class__.__name__}: {str(exc)[:80]}"]
            counts["observe"] += 1
    with _state_lock:
        STATE["market_context"] = market
        STATE["final_entry_checked"] = len(strict or [])
        STATE["final_entry_open"] = int(counts.get("paper_open", 0))
        STATE["final_entry_recheck_wait"] = int(counts.get("recheck_wait", 0))
        STATE["final_entry_observe"] = int(counts.get("observe", 0))
        STATE["final_entry_note"] = f"통과 {counts.get('paper_open',0)} / 재확인 {counts.get('recheck_wait',0)} / 관찰 {counts.get('observe',0)}"
    return dict(counts)

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
            rr.setdefault("ema5_gap_pct", 0.0)
            rr.setdefault("ema12_gap_pct", 0.0)
            rr.setdefault("ema21_gap_pct", 0.0)
            rr.setdefault("bb_position", 0.0)
            rr.setdefault("bb_width_pct", 0.0)
            rr.setdefault("bb_lower_gap_pct", 0.0)
            rr.setdefault("bb_middle_gap_pct", 0.0)
            rr.setdefault("bb_upper_gap_pct", 0.0)
            rr.setdefault("bb_lower_touch", False)
            rr.setdefault("mfi_14", 0.0)
            rr.setdefault("cci_20", 0.0)
            rr.setdefault("stoch_k", 0.0)
            rr.setdefault("stoch_d", 0.0)
            rr.setdefault("stoch_cross_up", False)
            rr.setdefault("adx_14", 0.0)
            rr.setdefault("atr_1m_pct", 0.0)
            rr.setdefault("atr_3m_pct", 0.0)
            rr.setdefault("current_move_vs_atr", 0.0)
            rr.setdefault("avg_price_turn_pct", 0.0)
            rr.setdefault("v_rebound_score", 0.0)
            rr.setdefault("volume_spike_30x", 0.0)
            rr.setdefault("volume_pump_risk", False)
            rr.setdefault("money_status", "정보없음")
            rr.setdefault("precision_missing_reason", "정밀캔들 정보없음/백그라운드 보강중")
            rr.setdefault("analysis_status", "정보없음")
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
        "ema12": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m" and r.get("ema12_gap_pct") not in (None, "")),
        "bb": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m" and r.get("bb_position") not in (None, "")),
        "mfi": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m" and r.get("mfi_14") not in (None, "")),
        "stoch": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m" and r.get("stoch_k") not in (None, "")),
        "adx": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m" and r.get("adx_14") not in (None, "")),
        "atr": sum(1 for r in rows if r.get("precision_source") == "candlestick_1m" and fnum(r.get("atr_1m_pct"), 0) > 0),
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
    ema5_gap = fnum(row.get("ema5_gap_pct"), ma5_gap)
    ema12_gap = fnum(row.get("ema12_gap_pct"), 0)
    ema21_gap = fnum(row.get("ema21_gap_pct"), 0)
    bb_position = fnum(row.get("bb_position"), 0)
    bb_lower_gap = fnum(row.get("bb_lower_gap_pct"), 0)
    bb_middle_gap = fnum(row.get("bb_middle_gap_pct"), 0)
    mfi_14 = fnum(row.get("mfi_14"), 0)
    cci_20 = fnum(row.get("cci_20"), 0)
    stoch_k = fnum(row.get("stoch_k"), 0)
    stoch_d = fnum(row.get("stoch_d"), 0)
    stoch_cross_up = bool(row.get("stoch_cross_up"))
    adx_14 = fnum(row.get("adx_14"), 0)
    avg_price_turn_pct = fnum(row.get("avg_price_turn_pct"), 0)
    v_rebound_score = fnum(row.get("v_rebound_score"), 0)
    volume_spike_30x = fnum(row.get("volume_spike_30x"), 0)
    volume_pump_risk = bool(row.get("volume_pump_risk"))
    money_1m = fnum(row.get("money_flow_1m"), row.get("turnover_1m"))
    money_3m = fnum(row.get("money_flow_3m"), row.get("turnover_3m"))
    pullback_depth = fnum(row.get("pullback_depth_pct"), 0)
    low_defense = fnum(row.get("low_defense_pct"), from_low)
    recovery_speed = fnum(row.get("recovery_speed_pct"), max(ch1, ch3, ch5))
    rebreakout_strength = fnum(row.get("rebreakout_strength"), 0)
    fake_bounce_score = fnum(row.get("fake_bounce_score"), 0)
    pullback_quality_score = fnum(row.get("pullback_quality_score"), 0)
    major_watch = str(row.get("ticker") or "").upper() in MAJOR_WATCH_TICKERS
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
    if bb_lower_gap <= 0.30 and bb_lower_gap != 0:
        aux_notes.append("볼린저하단 근처")
    if stoch_cross_up:
        aux_notes.append("Stoch 상향돌파")
    if adx_14 >= 25:
        aux_notes.append("ADX 힘 확인")
    if mfi_14 and mfi_14 < 35:
        aux_notes.append("MFI 눌림")
    if cci_20 and cci_20 < -80:
        aux_notes.append("CCI 눌림")
    if v_rebound_score > 0:
        aux_notes.append(f"V반등 {v_rebound_score:.2f}")
    if volume_pump_risk:
        aux_notes.append("거래량 과폭발 주의")
    if major_watch:
        aux_notes.append("대형주 참고용")
    if pullback_quality_score > 0:
        aux_notes.append(f"눌림품질 {pullback_quality_score:.2f}")
    if rebreakout_strength > 0:
        aux_notes.append(f"재돌파힘 {rebreakout_strength:.2f}")
    if fake_bounce_score >= 1.0:
        aux_notes.append("가짜반등 주의")

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
        "turnover_1m": money_1m,
        "turnover_3m": money_3m,
        "turnover_5m": money,
        "money_flow_1m": money_1m,
        "money_flow_3m": money_3m,
        "money_flow_5m": money,
        "money_flow": money,
        "turnover_24h": turnover_24h,
        "leader_score": leader,
        "edge_score": edge,
        "from_low_pct": from_low,
        "high_gap_pct": high_gap,
        "pullback_depth_pct": pullback_depth,
        "low_defense_pct": low_defense,
        "recovery_speed_pct": recovery_speed,
        "rebreakout_strength": rebreakout_strength,
        "fake_bounce_score": fake_bounce_score,
        "pullback_quality_score": pullback_quality_score,
        "major_watch": major_watch,
        "rank_best": rank_best,
        "data_age_sec": age,
        "freshness": row.get("freshness", "정상" if precision else "bulk_only"),
        "close_pos": close_pos,
        "upper_wick": upper,
        "rsi_14": rsi,
        "vwap_gap_pct": vwap_gap,
        "ma5_gap_pct": ma5_gap,
        "ema5_gap_pct": ema5_gap,
        "ema12_gap_pct": ema12_gap,
        "ema21_gap_pct": ema21_gap,
        "bb_position": bb_position,
        "bb_lower_gap_pct": bb_lower_gap,
        "bb_middle_gap_pct": bb_middle_gap,
        "mfi_14": mfi_14,
        "cci_20": cci_20,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "stoch_cross_up": stoch_cross_up,
        "adx_14": adx_14,
        "avg_price_turn_pct": avg_price_turn_pct,
        "v_rebound_score": v_rebound_score,
        "volume_spike_30x": volume_spike_30x,
        "volume_pump_risk": volume_pump_risk,
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
            "turnover_1m": prof.get("turnover_1m", 0),
            "turnover_3m": prof.get("turnover_3m", 0),
            "turnover_5m": prof["turnover_5m"],
            "money_flow_1m": prof.get("money_flow_1m", 0),
            "money_flow_3m": prof.get("money_flow_3m", 0),
            "money_flow_5m": prof.get("money_flow_5m", prof["turnover_5m"]),
            "turnover": prof["turnover_5m"],
            "turnover_24h": prof["turnover_24h"],
            "from_30m_low_pct": prof["from_low_pct"],
            "below_30m_high_pct": prof["high_gap_pct"],
            "pullback_depth_pct": prof.get("pullback_depth_pct", 0),
            "low_defense_pct": prof.get("low_defense_pct", 0),
            "recovery_speed_pct": prof.get("recovery_speed_pct", 0),
            "rebreakout_strength": prof.get("rebreakout_strength", 0),
            "fake_bounce_score": prof.get("fake_bounce_score", 0),
            "pullback_quality_score": prof.get("pullback_quality_score", 0),
            "major_watch": bool(prof.get("major_watch")),
            "major_watch_label": "대형주 참고용" if prof.get("major_watch") else "알트 단타 후보",
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
            "ema5_gap_pct": prof.get("ema5_gap_pct", 0),
            "ema12_gap_pct": prof.get("ema12_gap_pct", 0),
            "ema21_gap_pct": prof.get("ema21_gap_pct", 0),
            "bb_position": prof.get("bb_position", 0),
            "bb_lower_gap_pct": prof.get("bb_lower_gap_pct", 0),
            "bb_middle_gap_pct": prof.get("bb_middle_gap_pct", 0),
            "mfi_14": prof.get("mfi_14", 0),
            "cci_20": prof.get("cci_20", 0),
            "stoch_k": prof.get("stoch_k", 0),
            "stoch_d": prof.get("stoch_d", 0),
            "stoch_cross_up": prof.get("stoch_cross_up", False),
            "adx_14": prof.get("adx_14", 0),
            "avg_price_turn_pct": prof.get("avg_price_turn_pct", 0),
            "v_rebound_score": prof.get("v_rebound_score", 0),
            "volume_spike_30x": prof.get("volume_spike_30x", 0),
            "volume_pump_risk": prof.get("volume_pump_risk", False),
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
        if item.get("major_watch"):
            item["auto_ready_level"] = "major_watch"
            item["auto_ready_label"] = "대형주 참고용"
            item["aux_notes"] = list(item.get("aux_notes") or []) + ["대형주: trade_ready 제외/시장 참고"]
        item.update(ws_snapshot(item.get("ticker")))
        item.update(micro_snapshot(item.get("ticker")))
        exec_risk = execution_risk_snapshot(item)
        item.update(exec_risk)
        item["quality_risk_tags"] = quality_risk_tags(item)
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
    if item.get("major_watch"):
        reasons.append("대형주 참고용")
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
    final_action = str(item.get("final_entry_action") or "paper_open")
    final_label = str(item.get("final_entry_label") or "최종검증 통과")
    if final_action in {"observe", "recheck_wait"}:
        reasons.append(final_label)
    ok = not reasons
    label = "자동매매 검증급 OPEN" if ok else (final_label if final_action in {"observe", "recheck_wait"} else "정식 후보 관찰")
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
        "live_price_source": item.get("live_price_source", "-"),
        "live_price": fnum(item.get("live_price"), 0),
        "live_age_sec": fnum(item.get("live_age_sec"), -1),
        "ws_age_sec": fnum(item.get("ws_age_sec", item.get("live_age_sec", -1)), -1),
        "ws_cache_ts": fnum(item.get("ws_cache_ts"), 0),
        "ws_fresh": bool(item.get("ws_fresh")),
        "ws_row_status": item.get("ws_row_status", "missing"),
        "ws_targeted": bool(item.get("ws_targeted")),
        "ws_target_reason": item.get("ws_target_reason", "-"),
        "ws_turnover": fnum(item.get("ws_turnover"), 0),
        "ws_volume": fnum(item.get("ws_volume"), 0),
        "current_price_ws_gap_pct": fnum(item.get("current_price_ws_gap_pct"), 0),
        "micro_fresh": bool(item.get("micro_fresh")),
        "micro_row_status": item.get("micro_row_status", "missing"),
        "micro_targeted": bool(item.get("micro_targeted")),
        "micro_age_sec": fnum(item.get("micro_age_sec"), -1),
        "micro_spread_pct": fnum(item.get("micro_spread_pct"), 999),
        "micro_bid_wall_5_krw": fnum(item.get("micro_bid_wall_5_krw"), 0),
        "micro_ask_wall_5_krw": fnum(item.get("micro_ask_wall_5_krw"), 0),
        "micro_bid_ask_wall_ratio": fnum(item.get("micro_bid_ask_wall_ratio"), 0),
        "micro_trade_buy_ratio_30": fnum(item.get("micro_trade_buy_ratio_30"), 0),
        "micro_trade_buy_krw_30": fnum(item.get("micro_trade_buy_krw_30"), 0),
        "micro_trade_sell_krw_30": fnum(item.get("micro_trade_sell_krw_30"), 0),
        "micro_ask_wall_pressure": bool(item.get("micro_ask_wall_pressure")),
        "micro_sell_trade_pressure": bool(item.get("micro_sell_trade_pressure")),
        "micro_flags": item.get("micro_flags", []),
        "change_1": fnum(item.get("change_1"), 0),
        "change_3": fnum(item.get("change_3"), 0),
        "change_5": fnum(item.get("change_5"), 0),
        "change_15": fnum(item.get("change_15"), 0),
        "change_30": fnum(item.get("change_30"), 0),
        "vol_ratio": fnum(item.get("vol_ratio"), 0),
        "turnover_1m": fnum(item.get("turnover_1m"), 0),
        "turnover_3m": fnum(item.get("turnover_3m"), 0),
        "turnover_5m": fnum(item.get("turnover_5m"), 0),
        "money_flow_1m": fnum(item.get("money_flow_1m"), 0),
        "money_flow_3m": fnum(item.get("money_flow_3m"), 0),
        "money_flow_5m": fnum(item.get("money_flow_5m"), item.get("turnover_5m")),
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
        "pullback_depth_pct": fnum(item.get("pullback_depth_pct"), 0),
        "low_defense_pct": fnum(item.get("low_defense_pct"), 0),
        "recovery_speed_pct": fnum(item.get("recovery_speed_pct"), 0),
        "rebreakout_strength": fnum(item.get("rebreakout_strength"), 0),
        "fake_bounce_score": fnum(item.get("fake_bounce_score"), 0),
        "pullback_quality_score": fnum(item.get("pullback_quality_score"), 0),
        "major_watch": bool(item.get("major_watch")),
        "major_watch_label": item.get("major_watch_label", ""),
        "rank_best": fint(item.get("rank_best"), 999),
        "data_age_sec": fnum(item.get("data_age_sec"), 0),
        "freshness": item.get("freshness", "-"),
        "current_close_pos_ratio": fnum(item.get("current_close_pos_ratio"), 0),
        "current_upper_wick_pct": fnum(item.get("current_upper_wick_pct"), 0),
        "rsi_14": fnum(item.get("rsi_14"), 0),
        "vwap_gap_pct": fnum(item.get("vwap_gap_pct"), 0),
        "ma5_gap_pct": fnum(item.get("ma5_gap_pct"), 0),
        "ema5_gap_pct": fnum(item.get("ema5_gap_pct"), 0),
        "ema12_gap_pct": fnum(item.get("ema12_gap_pct"), 0),
        "ema21_gap_pct": fnum(item.get("ema21_gap_pct"), 0),
        "bb_position": fnum(item.get("bb_position"), 0),
        "bb_lower_gap_pct": fnum(item.get("bb_lower_gap_pct"), 0),
        "bb_middle_gap_pct": fnum(item.get("bb_middle_gap_pct"), 0),
        "mfi_14": fnum(item.get("mfi_14"), 0),
        "cci_20": fnum(item.get("cci_20"), 0),
        "stoch_k": fnum(item.get("stoch_k"), 0),
        "stoch_d": fnum(item.get("stoch_d"), 0),
        "stoch_cross_up": bool(item.get("stoch_cross_up")),
        "adx_14": fnum(item.get("adx_14"), 0),
        "avg_price_turn_pct": fnum(item.get("avg_price_turn_pct"), 0),
        "v_rebound_score": fnum(item.get("v_rebound_score"), 0),
        "volume_spike_30x": fnum(item.get("volume_spike_30x"), 0),
        "volume_pump_risk": bool(item.get("volume_pump_risk")),
        "current_candle_code": item.get("current_candle_code", "UNKNOWN"),
        "current_candle_label": item.get("current_candle_label", "현재봉 확인중"),
        "live_price_recheck": fnum(item.get("live_price_recheck"), 0),
        "price_recheck_pct": fnum(item.get("price_recheck_pct"), 0),
        "orderbook_spread_pct": sanitize_spread_pct(item.get("orderbook_spread_pct"))[0],
        "orderbook_spread_status": item.get("orderbook_spread_status", sanitize_spread_pct(item.get("orderbook_spread_pct"))[1]),
        "price_recheck_status": item.get("price_recheck_status", "정보없음"),
        "tick_pct_est": fnum(item.get("tick_pct_est"), 999),
        "precision_source": item.get("precision_source", "bulk_only"),
        "analysis_status": item.get("analysis_status", "정보없음"),
        "execution_risk_status": item.get("execution_risk_status", "확인중"),
        "execution_risk_flags": item.get("execution_risk_flags", []),
        "quality_risk_tags": item.get("quality_risk_tags", []),
        "execution_risk_sync": bool(item.get("execution_risk_sync")),
        "reason": item.get("one_liner", ""),
        "why": item.get("one_liner", ""),
        "entry_context": {
            "score": fnum(item.get("score"), 0),
            "live_price_source": item.get("live_price_source", "-"),
            "live_price": fnum(item.get("live_price"), 0),
            "live_age_sec": fnum(item.get("live_age_sec"), -1),
            "ws_age_sec": fnum(item.get("ws_age_sec", item.get("live_age_sec", -1)), -1),
            "ws_cache_ts": fnum(item.get("ws_cache_ts"), 0),
            "ws_fresh": bool(item.get("ws_fresh")),
            "ws_row_status": item.get("ws_row_status", "missing"),
            "ws_targeted": bool(item.get("ws_targeted")),
            "ws_target_reason": item.get("ws_target_reason", "-"),
            "ws_turnover": fnum(item.get("ws_turnover"), 0),
            "ws_volume": fnum(item.get("ws_volume"), 0),
            "current_price_ws_gap_pct": fnum(item.get("current_price_ws_gap_pct"), 0),
        "micro_fresh": bool(item.get("micro_fresh")),
        "micro_row_status": item.get("micro_row_status", "missing"),
        "micro_targeted": bool(item.get("micro_targeted")),
        "micro_age_sec": fnum(item.get("micro_age_sec"), -1),
        "micro_spread_pct": fnum(item.get("micro_spread_pct"), 999),
        "micro_bid_wall_5_krw": fnum(item.get("micro_bid_wall_5_krw"), 0),
        "micro_ask_wall_5_krw": fnum(item.get("micro_ask_wall_5_krw"), 0),
        "micro_bid_ask_wall_ratio": fnum(item.get("micro_bid_ask_wall_ratio"), 0),
        "micro_trade_buy_ratio_30": fnum(item.get("micro_trade_buy_ratio_30"), 0),
        "micro_trade_buy_krw_30": fnum(item.get("micro_trade_buy_krw_30"), 0),
        "micro_trade_sell_krw_30": fnum(item.get("micro_trade_sell_krw_30"), 0),
        "micro_ask_wall_pressure": bool(item.get("micro_ask_wall_pressure")),
        "micro_sell_trade_pressure": bool(item.get("micro_sell_trade_pressure")),
        "micro_flags": item.get("micro_flags", []),
            "precision_source": item.get("precision_source", "bulk_only"),
            "analysis_status": item.get("analysis_status", "정보없음"),
            "change_1": entry_value(item, "change_1", requires_precision=True),
            "change_3": entry_value(item, "change_3", requires_precision=True),
            "change_5": entry_value(item, "change_5", requires_precision=True),
            "change_15": entry_value(item, "change_15", requires_precision=True),
            "change_30": entry_value(item, "change_30", requires_precision=True),
            "vol_ratio": entry_value(item, "vol_ratio", requires_precision=True),
            "turnover_1m": entry_value(item, "turnover_1m", requires_precision=True),
            "turnover_3m": entry_value(item, "turnover_3m", requires_precision=True),
            "turnover_5m": entry_value(item, "turnover_5m", requires_precision=True),
            "money_flow_1m": entry_value(item, "money_flow_1m", requires_precision=True),
            "money_flow_3m": entry_value(item, "money_flow_3m", requires_precision=True),
            "money_flow_5m": entry_value(item, "money_flow_5m", requires_precision=True),
            "from_30m_low_pct": entry_value(item, "from_30m_low_pct", requires_precision=True),
            "below_30m_high_pct": entry_value(item, "below_30m_high_pct", requires_precision=True),
            "pullback_depth_pct": entry_value(item, "pullback_depth_pct", requires_precision=True),
            "low_defense_pct": entry_value(item, "low_defense_pct", requires_precision=True),
            "recovery_speed_pct": entry_value(item, "recovery_speed_pct", requires_precision=True),
            "rebreakout_strength": entry_value(item, "rebreakout_strength", requires_precision=True),
            "fake_bounce_score": entry_value(item, "fake_bounce_score", requires_precision=True),
            "pullback_quality_score": entry_value(item, "pullback_quality_score", requires_precision=True),
            "price_recheck_pct": entry_value(item, "price_recheck_pct", requires_risk=True, max_abs=PRICE_RECHECK_ABNORMAL_PCT),
            "price_recheck_status": item.get("price_recheck_status", "정보없음"),
            "orderbook_spread_pct": entry_value(item, "orderbook_spread_pct", requires_risk=True, max_abs=SPREAD_ABNORMAL_PCT),
            "orderbook_spread_status": item.get("orderbook_spread_status", "정보없음"),
            "tick_pct_est": entry_value(item, "tick_pct_est", requires_risk=True, max_abs=5.0),
            "chase_risk": item.get("chase_risk", "-"),
            "execution_risk_status": item.get("execution_risk_status", "확인중"),
            "execution_risk_flags": item.get("execution_risk_flags", []),
            "quality_risk_tags": item.get("quality_risk_tags", []),
            "execution_risk_sync": bool(item.get("execution_risk_sync")),
            "final_entry_action": item.get("final_entry_action", "paper_open"),
            "final_entry_label": item.get("final_entry_label", "최종검증 통과"),
            "final_entry_reasons": item.get("final_entry_reasons", []),
            "final_slow_hits": item.get("final_slow_hits", 0),
            "final_stop_hits": item.get("final_stop_hits", 0),
            "market_pressure": item.get("market_pressure", "-"),
            "market_context": item.get("market_context", {}),
            "atr_1m_pct": entry_value(item, "atr_1m_pct", requires_precision=True),
            "atr_3m_pct": entry_value(item, "atr_3m_pct", requires_precision=True),
            "current_move_vs_atr": entry_value(item, "current_move_vs_atr", requires_precision=True),
            "ema5_gap_pct": entry_value(item, "ema5_gap_pct", requires_precision=True),
            "ema12_gap_pct": entry_value(item, "ema12_gap_pct", requires_precision=True),
            "ema21_gap_pct": entry_value(item, "ema21_gap_pct", requires_precision=True),
            "bb_position": entry_value(item, "bb_position", requires_precision=True),
            "bb_lower_gap_pct": entry_value(item, "bb_lower_gap_pct", requires_precision=True),
            "bb_middle_gap_pct": entry_value(item, "bb_middle_gap_pct", requires_precision=True),
            "mfi_14": entry_value(item, "mfi_14", requires_precision=True),
            "cci_20": entry_value(item, "cci_20", requires_precision=True),
            "stoch_k": entry_value(item, "stoch_k", requires_precision=True),
            "stoch_d": entry_value(item, "stoch_d", requires_precision=True),
            "stoch_cross_up": bool(item.get("stoch_cross_up")) if str(item.get("precision_source") or "") == "candlestick_1m" else None,
            "adx_14": entry_value(item, "adx_14", requires_precision=True),
            "avg_price_turn_pct": entry_value(item, "avg_price_turn_pct", requires_precision=True),
            "v_rebound_score": entry_value(item, "v_rebound_score", requires_precision=True),
            "volume_spike_30x": entry_value(item, "volume_spike_30x", requires_precision=True),
            "volume_pump_risk": bool(item.get("volume_pump_risk")) if str(item.get("precision_source") or "") == "candlestick_1m" else None,
            "major_watch": bool(item.get("major_watch")),
            "major_watch_label": item.get("major_watch_label", ""),
            "slippage_risk": item.get("slippage_risk", "-"),
            "tick_risk": item.get("tick_risk", "-"),
            "auto_ready_level": item.get("auto_ready_level", ""),
            "auto_ready_label": item.get("auto_ready_label", ""),
        },

        "candidate_events_disabled": True,
        "mainline_file": "paper_candidates" if eligible else "shadow_candidates",
        "pipeline_note": "v2.13.199: WS target TTL/debounce + entry_context WS age 기준 통일. 조건/청산/BUY_READY 변경 없음.",
    }


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    # v184: 점수가 이미 trade_ready권인 strict 후보는 후보파일 작성 전에 실전위험을 우선 확인한다.
    sync_execution_risk_for_trade_ready_candidates(strict or [])
    enqueue_execution_risk((strict or []) + (shadow or []))
    result = {
        "paper_attempt": len(strict),
        "shadow_attempt": len(shadow),
        "paper_written": 0,
        "shadow_written": 0,
        "trade_ready_written": 0,
        "strict_observe_written": 0,
        "major_watch_written": 0,
    "paper_latest_written": 0,
    "shadow_latest_written": 0,
    "latest_trade_ready": 0,
    "latest_strict_observe": 0,
    "latest_final_recheck_wait": 0,
    "latest_final_observe": 0,
    "dup_skip_reason": {},
    "data_quality_note": "",
        "dup_skip": 0,
        "write_error": "",
        "last_ticker": "-",
        "factory_mode": "v184_risk_sync_then_batch_append",
    }
    # v166: 압축은 시간 간격을 두고 수행한다. 후보별 파일 open/write 반복 금지.
    compact_candidate_file(FILES["paper"])
    compact_candidate_file(FILES["shadow"])

    dup_reasons: Counter = Counter()
    for lane, items, path in [("strict", strict, FILES["paper"]), ("shadow", shadow, FILES["shadow"] )]:
        rows: List[Dict[str, Any]] = []
        latest_rows: List[Dict[str, Any]] = []
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
            # v175: archive 중복과 latest는 분리한다.
            # archive는 event bucket 중복을 막되, latest는 현재 scan 후보 상태를 그대로 보여줘야 한다.
            latest_rows.append(row)
            key = str(row.get("event_id"))
            last = fnum(_seen_events.get(key), 0)
            if ts - last < EVENT_DEDUP_SEC:
                result["dup_skip"] += 1
                dup_reasons[f"{lane}_event_bucket"] += 1
                continue
            _seen_events[key] = ts
            rows.append(row)
        ok, err = append_jsonl_many(path, rows)
        latest_path = FILES["paper_latest"] if lane == "strict" else FILES["shadow_latest"]
        latest_ok, latest_err = write_jsonl_replace(latest_path, latest_rows)
        if lane == "strict":
            result["paper_latest_written"] = len(latest_rows)
            result["latest_trade_ready"] = sum(1 for r in latest_rows if r.get("paper_bot_open"))
            result["latest_strict_observe"] = sum(1 for r in latest_rows if not r.get("paper_bot_open"))
            result["latest_final_recheck_wait"] = sum(1 for r in latest_rows if r.get("final_entry_action") == "recheck_wait")
            result["latest_final_observe"] = sum(1 for r in latest_rows if r.get("final_entry_action") == "observe")
        else:
            result["shadow_latest_written"] = len(latest_rows)
        if not latest_ok and not err:
            err = "latest:" + latest_err
        if ok:
            if lane == "strict":
                result["paper_written"] += len(rows)
                result["trade_ready_written"] += sum(1 for r in rows if r.get("paper_bot_open"))
                result["strict_observe_written"] += sum(1 for r in rows if not r.get("paper_bot_open"))
                result["major_watch_written"] += sum(1 for r in rows if r.get("major_watch"))
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
    result["dup_skip_reason"] = dict(dup_reasons)
    return result


def scan_once() -> List[Dict[str, Any]]:
    started = now_ts()
    ensure_eval_baseline()
    with _state_lock:
        STATE["scan_calls"] = int(STATE.get("scan_calls", 0)) + 1
        STATE["scan_seq"] = int(STATE.get("scan_seq", 0)) + 1
        STATE["scan_id"] = f"scan-{int(started)}-{int(STATE.get('scan_seq', 0))}"
        STATE["scan_started_at"] = started
        STATE["scan_running"] = True
        STATE["scan_display_note"] = "스캔 진행중"
        STATE["scan_last_stage"] = "hub_bulk"
    stage_times: List[Tuple[str, float, str]] = []
    try:
        st = now_ts()
        rows, source = fetch_all_krw()
        update_ws_targets(rows, reason="hub_rank")
        update_micro_targets(rows, reason="hub_rank")
        rows = apply_ws_cache_to_rows(rows)
        rows = apply_micro_cache_to_rows(rows)
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
        stage_times.append(("3) 2차 직원: 눌림·흐름 정밀값 보강", now_ts() - st, f"직접 {p_ok} / 필요 {STATE.get('precision_need', 0)} / 대기열 {STATE.get('precision_queue_size', 0)} / fail {p_fail} / cached {p_have}"))
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
        stage_times.append(("5) 3~5차 직원: 재돌파·실전위험·등급분류", now_ts() - st, f"strict {len(strict)} / shadow {len(shadow)} / major {sum(1 for r in strict if r.get('major_watch'))} / reject {sum(rejects.values())}"))
        with _state_lock:
            STATE["strict_decision"] = len(strict)
            STATE["shadow_decision"] = len(shadow)
            STATE["reject_counts"] = dict(rejects)
            STATE["reject_examples"] = examples
            STATE["last_rows_sample"] = [{"ticker": r.get("ticker"), "score": r.get("score"), "line": r.get("one_liner", "")[:100]} for r in strict[:5]]
            STATE["scan_last_stage"] = "worker6_final_entry"
        # v199: 최종검증을 기다리지 말고 후보가 잡힌 즉시 WS/micro 대상파일을 먼저 갱신한다.
        # 이번 scan에서 직접 대기하지 않고, sidecar가 다음 poll/reconnect부터 빠르게 따라붙게 한다.
        update_ws_targets(rows, priority_rows=strict, reason="pre_final_candidates")
        update_micro_targets(rows, priority_rows=strict, reason="pre_final_candidates")

        st = now_ts()
        final_counts = apply_final_entry_worker(strict, rows)
        update_ws_targets(rows, priority_rows=strict, reason="strict_final_candidates")
        update_micro_targets(rows, priority_rows=strict, reason="strict_final_candidates")
        mark_ws_target_flags(strict)
        mark_ws_target_flags(shadow)
        stage_times.append(("6) 6차 직원: ATR/VWAP/시장장세 최종진입검증 + WS대상갱신", now_ts() - st, f"통과 {final_counts.get('paper_open',0)} / 재확인 {final_counts.get('recheck_wait',0)} / 관찰 {final_counts.get('observe',0)} / 시장 {STATE.get('market_context',{}).get('market_pressure','-')} / ws대상 {STATE.get('ws_target_file_targets',0)}"))
        with _state_lock:
            STATE["scan_last_stage"] = "factory_export"

        st = now_ts()
        pipe = export_candidates(strict, shadow)
        stage_times.append(("7) 공장: latest 후보파일 + entry_context 저장", now_ts() - st, f"archive strict {pipe['paper_written']}/{pipe['paper_attempt']} / latest strict {pipe.get('paper_latest_written',0)} / paper_OPEN {pipe.get('latest_trade_ready',0)} / 재확인 {pipe.get('latest_final_recheck_wait',0)} / 관찰 {pipe.get('latest_strict_observe',0)} / major {pipe.get('major_watch_written',0)} / shadow latest {pipe.get('shadow_latest_written',0)} / dup {pipe['dup_skip']}"))
        total = now_ts() - started
        with _state_lock:
            STATE["paper_written"] = pipe["paper_written"]
            STATE["trade_ready_written"] = pipe.get("trade_ready_written", 0)
            STATE["strict_observe_written"] = pipe.get("strict_observe_written", 0)
            STATE["major_watch_written"] = pipe.get("major_watch_written", 0)
            STATE["major_watch_count"] = sum(1 for r in strict if r.get("major_watch"))
            STATE["shadow_written"] = pipe["shadow_written"]
            STATE["paper_latest_written"] = pipe.get("paper_latest_written", 0)
            STATE["shadow_latest_written"] = pipe.get("shadow_latest_written", 0)
            STATE["latest_trade_ready"] = pipe.get("latest_trade_ready", 0)
            STATE["latest_strict_observe"] = pipe.get("latest_strict_observe", 0)
            STATE["latest_final_recheck_wait"] = pipe.get("latest_final_recheck_wait", 0)
            STATE["latest_final_observe"] = pipe.get("latest_final_observe", 0)
            STATE["dup_skip"] = pipe["dup_skip"]
            STATE["dup_skip_reason"] = pipe.get("dup_skip_reason", {})
            STATE["write_error"] = pipe.get("write_error", "")
            STATE["last_ticker"] = pipe.get("last_ticker", "-")
            STATE["scan_last_sec"] = round(total, 3)
            STATE["last_done_scan_sec"] = round(total, 3)
            STATE["last_done_scan_ts"] = now_ts()
            STATE["scan_running"] = False
            STATE["scan_display_note"] = "완료"
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
            STATE["scan_running"] = False
            STATE["scan_display_note"] = "오류"
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
    avg = fnum(stat.get("avg"), 0)
    if n <= 0:
        return "❔"
    # 50전 미만은 좋고 나쁨을 확정하지 않는다.
    if n < 50:
        return "⚠️"
    return "✅" if avg > 0 else "❌"

def sample_note(n: int) -> str:
    if n <= 0:
        return ""
    if n < 50:
        return " / 판단보류: 50전 미만"
    return ""

def fmt_stats(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    s = score_stats(rows)
    icon = score_icon(s)
    return f"{icon} {label_kr(label)}: {s['n']}전 {s['wins']}승 {s['losses']}패 / 승률 {s['win_rate']:.1f}% / 합산 {s['total']:+.2f}% / 평균 {s['avg']:+.2f}%{sample_note(s['n'])}"

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



def scan_status_summary(s: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """명령어용 scan 상태 요약.
    scan 중간에 /core를 치면 scan_last_sec가 0으로 보일 수 있어, 진행중/정보없음을 분리 표시한다.
    """
    s = s or STATE
    stage = str(s.get("scan_last_stage") or "-")
    running = bool(s.get("scan_running")) or stage not in {"done", "error", "boot", "-"}
    sec = fnum(s.get("scan_last_sec"), 0)
    last_done = fnum(s.get("last_done_scan_sec"), 0)
    if running:
        age = max(0.0, now_ts() - fnum(s.get("scan_started_at"), now_ts()))
        if last_done > 0:
            return {"icon": "⚠️", "label": f"진행중 {age:.0f}s / 직전완료 {last_done:.2f}s", "sec": last_done, "running": True, "stage": stage}
        return {"icon": "❔", "label": f"진행중 {age:.0f}s / 완료값 정보없음", "sec": 0.0, "running": True, "stage": stage}
    if s.get("scan_last_error"):
        return {"icon": "❌", "label": f"오류 / {sec:.2f}s", "sec": sec, "running": False, "stage": stage}
    icon = "✅" if sec <= 5 else ("⚠️" if sec <= 12 else "❌")
    return {"icon": icon, "label": f"{sec:.2f}s", "sec": sec, "running": False, "stage": stage}


def precision_status_note(s: Optional[Dict[str, Any]] = None) -> str:
    s = s or STATE
    cache = fint(s.get("precision_have"), 0)
    q = fint(s.get("precision_queue_size"), 0)
    selected = fint(s.get("precision_selected"), 0)
    cov = s.get("field_coverage", {}) if isinstance(s.get("field_coverage"), dict) else {}
    bulk_only = cov.get("bulk_only", 0)
    if cache <= 0:
        return f"❔ 정밀: 정보없음 / bulk-only {bulk_only} / 대기 {q} / 원인: 재시작 직후 또는 캔들값 아직 미수집"
    if cache < 80 and q > 0:
        return f"⚠️ 정밀: 캐시 {cache} / bulk-only {bulk_only} / 대기 {q} / 원인: 백그라운드 정밀직원 보강중"
    if selected and cache < max(30, selected * 0.25):
        return f"⚠️ 정밀: 캐시 {cache} / bulk-only {bulk_only} / 대기 {q} / 원인: 정밀 커버리지 낮음"
    return f"✅ 정밀: 캐시 {cache} / bulk-only {bulk_only} / 대기 {q}"


def latest_candidate_counts() -> Dict[str, int]:
    return {"paper_latest": line_count(FILES["paper_latest"]), "shadow_latest": line_count(FILES["shadow_latest"])}

def field_line() -> str:
    cov = STATE.get("field_coverage", {}) if isinstance(STATE.get("field_coverage"), dict) else {}
    return (
        f"- 입력값표준화: rows {cov.get('rows',0)} / 가격 {cov.get('price',0)} / 돈 {cov.get('money',0)} / "
        f"거래량 {cov.get('volume',0)} / 흐름 {cov.get('momentum',0)} / 위치 {cov.get('position',0)} / "
        f"rank {cov.get('rank',0)} / 신선 {cov.get('fresh',0)} / 정밀 {cov.get('precision',0)} / bulk-only {cov.get('bulk_only',0)} / "
        f"돈구분 real {cov.get('real_money',0)} proxy {cov.get('proxy_money',0)} missing {cov.get('missing_money',0)} / "
        f"보조지표 ema {cov.get('ema12',0)} bb {cov.get('bb',0)} mfi {cov.get('mfi',0)} stoch {cov.get('stoch',0)} adx {cov.get('adx',0)} atr {cov.get('atr',0)}"
    )


def candidate_line() -> str:
    err = STATE.get("write_error") or ""
    err_part = f" / 오류 {err[:120]}" if err else ""
    latest = latest_candidate_counts()
    scan = scan_status_summary()
    note = " / 현재 scan 진행중" if scan.get("running") else ""
    return (
        f"- 후보: 정식 {latest.get('paper_latest',0)}개 / 모의매매 진입대상 {STATE.get('latest_trade_ready',0)}개 / "
        f"조금 더 볼 후보 {STATE.get('latest_final_recheck_wait',0)}개 / 진입보류 {STATE.get('latest_strict_observe',0)}개 / 복기 {latest.get('shadow_latest',0)}개"
        f"{note}{err_part}"
    )

def stage_lines() -> List[str]:
    rows = STATE.get("stage_times") if isinstance(STATE.get("stage_times"), list) else []
    out = ["🧩 v2.13.194 scan 단계표"]
    if not rows:
        out.append("- 아직 scan 단계표 없음")
        return out
    for name, sec, note in rows:
        out.append(f"- {name}: {sec:.3f}s / {note}")
    scan = scan_status_summary()
    out.append(f"- 전체: {scan['label']} / stage {STATE.get('scan_last_stage','-')}")
    return out

def reject_lines(limit: int = 5) -> List[str]:
    d = STATE.get("reject_counts") if isinstance(STATE.get("reject_counts"), dict) else {}
    rows = sorted(d.items(), key=lambda x: x[1], reverse=True)[:limit]
    out = ["- 탈락상위: " + (" / ".join([f"{k} {v}" for k, v in rows]) if rows else "-")]
    examples = STATE.get("reject_examples") if isinstance(STATE.get("reject_examples"), list) else []
    for e in examples[:3]:
        out.append(f"  · {e.get('ticker','-')}: {e.get('reason','-')} / 점수 {fnum(e.get('score'),0):.2f}")
    return out



def _read_pid_file(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        return int(str(path.read_text(encoding="utf-8", errors="ignore")).strip() or "0")
    except Exception:
        return 0


def _pid_alive(pid: int) -> bool:
    try:
        if not pid or pid <= 0:
            return False
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _find_paperbot_pids() -> List[int]:
    """실제 실행 중인 paper_bot.py pid를 /proc에서 찾는다.
    pid/status 파일이 엇갈릴 때 운영 찌꺼기에 물리지 않기 위한 보정용이다.
    """
    out: List[int] = []
    proc = Path("/proc")
    try:
        for p in proc.iterdir():
            if not p.name.isdigit():
                continue
            pid = int(p.name)
            cmd = _proc_cmdline(pid)
            low = cmd.lower()
            if "paper_bot.py" in low and "python" in low:
                out.append(pid)
    except Exception:
        pass
    return sorted(set(out))


def _repair_paper_pid_file(pid: int) -> None:
    try:
        if pid and pid > 1:
            (BASE_DIR / "paper_bot.pid").write_text(str(int(pid)), encoding="utf-8")
    except Exception:
        pass


def _age_text(ts: float) -> str:
    try:
        age = max(0.0, now_ts() - float(ts or 0))
        if age < 60:
            return f"{age:.0f}초"
        return f"{age/60:.1f}분"
    except Exception:
        return "-"


def paper_alive_summary() -> Dict[str, Any]:
    """paper_bot 상태를 status/pid/proc 세 갈래로 판정한다.

    v172 원칙:
    - status 파일의 running=True를 그대로 믿지 않는다.
    - pid_file, status self_pid/status_pid, 실제 /proc paper_bot.py 검색 pid를 비교한다.
    - pid 파일이 틀렸고 실제 살아있는 paper_bot.py가 있으면 pid 파일을 보정한다.
    """
    ps = read_paper_status()
    st = ps["status"]
    ctrl = ps["control"]
    pid_file = _read_pid_file(BASE_DIR / "paper_bot.pid")
    status_pid = int(fnum(st.get("self_pid") or st.get("pid") or st.get("process_pid"), 0))
    proc_pids = _find_paperbot_pids()

    file_alive = _pid_alive(pid_file)
    status_alive = _pid_alive(status_pid) if status_pid > 1 else False
    proc_alive = bool(proc_pids)

    if status_alive:
        used_pid = status_pid
    elif file_alive:
        used_pid = pid_file
    elif proc_alive:
        used_pid = proc_pids[-1]
        _repair_paper_pid_file(used_pid)
    else:
        used_pid = status_pid or pid_file or 0

    alive = bool(status_alive or file_alive or proc_alive)
    uts = fnum(st.get("updated_at"), 0)
    age = now_ts() - uts if uts > 0 else -1
    status_running = bool(st.get("running"))
    ctrl_running = bool(ctrl.get("running", status_running))

    mismatch = False
    if alive:
        if pid_file and used_pid and pid_file != used_pid:
            mismatch = True
        if status_pid and used_pid and status_pid != used_pid:
            mismatch = True
    stale = bool(age < 0 or age > 180)
    ghost_status = bool((not alive) and status_running)

    if not alive:
        verdict = "dead"
        icon = "❌"
        label = "pid 죽음" + (" / status 잔상" if ghost_status else "")
    elif mismatch:
        verdict = "mismatch"
        icon = "⚠️"
        label = "pid 보정 필요"
    elif stale:
        verdict = "stale"
        icon = "⚠️"
        label = "상태 갱신 지연"
    else:
        verdict = "ok"
        icon = "✅"
        label = "정상"
    return {
        "status": st,
        "control": ctrl,
        "pid": used_pid,
        "pid_file": pid_file,
        "status_pid": status_pid,
        "proc_pids": proc_pids,
        "alive": alive,
        "file_alive": file_alive,
        "status_alive": status_alive,
        "proc_alive": proc_alive,
        "age": age,
        "stale": stale,
        "mismatch": mismatch,
        "ghost_status": ghost_status,
        "status_running": status_running,
        "ctrl_running": ctrl_running,
        "stop_reason": st.get("stop_reason", "-"),
        "verdict": verdict,
        "icon": icon,
        "label": label,
    }


def guard_summary_line() -> str:
    try:
        status = load_json(BASE_DIR / ".guard_upgrade_status.json", {})
        active = BASE_DIR / "backga_guard_bot.py"
        gv = "-"
        if active.exists():
            txt = active.read_text(encoding="utf-8", errors="ignore")[:6000]
            m = re.search(r'^\s*VERSION\s*=\s*[\"\']([^\"\']+)', txt, flags=re.M)
            if m:
                gv = m.group(1)
        last = status.get("time", "-") if isinstance(status, dict) else "-"
        ok = status.get("ok") if isinstance(status, dict) else None
        icon = "✅" if ok is True else ("⚠️" if ok is False else "❔")
        return f"{icon} 가드봇: {gv} / 최근적용 {last}"
    except Exception:
        return "❔ 가드봇: 상태파일 확인중"


def bot_status_text() -> str:
    p = paper_alive_summary()
    st = p["status"]
    age = p["age"]
    age_txt = _age_text(fnum(st.get('updated_at'), 0))
    if p["verdict"] == "ok":
        p_line = f"✅ 페이퍼봇: {st.get('version','?')} / OPEN {st.get('open_total','?')} / cycle {st.get('elapsed_sec','-')}s"
    elif p["verdict"] == "dead":
        p_line = f"❌ 페이퍼봇: pid 죽음 / status {age_txt} 전 / stop {st.get('stop_reason','-')}"
    elif p["verdict"] == "mismatch":
        p_line = f"⚠️ 페이퍼봇: pid 불일치 / file {p.get('pid_file') or '-'} / status {p.get('status_pid') or '-'}"
    else:
        p_line = f"⚠️ 페이퍼봇: 상태갱신 지연 / {age_txt} 전 / OPEN {st.get('open_total','?')}"
    return "\n".join([
        "🤖 봇 통합상태 /bot_status",
        f"{scan_status_summary()['icon']} 메인봇: {BOT_VERSION} / scan {scan_status_summary()['label']} / 시장 {STATE.get('bulk_rows',0)}",
        p_line,
        guard_summary_line(),
        "- 재시작/업그레이드는 가드봇 담당. 메인봇은 상태만 읽음.",
    ])

def paper_bot_line() -> str:
    p = paper_alive_summary()
    st = p["status"]
    open_pos = read_open()
    closed_total = st.get('closed_total', line_count(FILES['paper_closed']))
    age_part = f"상태 {_age_text(fnum(st.get('updated_at'),0))} 전" if fnum(st.get('updated_at'),0) > 0 else "상태파일 대기"
    if p["verdict"] == "dead":
        return f"- paper_bot: ❌ pid 죽음 / OPEN {len(open_pos)} / CLOSED {closed_total} / {age_part} / stop {st.get('stop_reason','-')}"
    icon = p.get("icon", "❔")
    label = p.get("label", "확인중")
    return (
        f"- paper_bot: {icon} {st.get('version','?')} / {label} / running {p.get('ctrl_running')} / "
        f"OPEN {len(open_pos)} (정식 {st.get('open_strict','?')} / 복기 {st.get('open_shadow','?')}) / "
        f"CLOSED {closed_total} / {age_part}"
    )


def architecture_text() -> str:
    return "- 구조: 허브 → 1차(표준화) → 2차(눌림품질) → 3차(재돌파) → 4차(실전위험) → 5차(등급분류) → 공장 → paper_bot"


def core_text() -> str:
    with _state_lock:
        s = dict(STATE)
    scan = scan_status_summary(s)
    cov = s.get('field_coverage', {}) if isinstance(s.get('field_coverage'), dict) else {}
    return "\n".join([
        "📌 종합 상태판 /core",
        f"{scan['icon']} 메인봇: {BOT_VERSION} / scan {scan['label']} / 단계 {scan.get('stage','-')}",
        f"✅ 시장: 전체 {s.get('bulk_rows',0)} / 가격 {s.get('bulk_price',0)} / 거래대금 {s.get('bulk_money',0)} / 순위 {cov.get('rank', s.get('bulk_rows',0))}",
        precision_status_note(s),
        f"✅ 직원: 2차 눌림 · 3차 재돌파 · 4차 실전위험 · 5차 분류 · 6차 최종진입검증",
        f"🔎 정보: 정밀 {cov.get('precision',0)} / ATR {cov.get('atr',0)} / 위험확인 {s.get('execution_risk_cached',0)} / WS대상 {s.get('ws_target_file_targets',0)} / 호가체결 {s.get('micro_fresh',0)}/{s.get('micro_cached',0)} / 최종검증 {s.get('final_entry_note','-')}",
        websocket_status_line(),
        candidate_line(),
        paper_bot_line(),
        guard_summary_line(),
        "",
        "판독",
        "✅ 전체시장 스캔 유지 / 자동매수 OFF",
        "⚠️ 정밀값이 낮으면 후보품질보다 정밀직원 상태부터 확인",
        "⚠️ 성과는 현재버전 정식 모의매매 기준으로 판단",
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
        f"- RSS {rss}MB / Load {loadavg[0]:.2f}/{loadavg[1]:.2f}/{loadavg[2]:.2f} / scan {scan_status_summary()['label']} / scan최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s",
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
        "- v2.13.187 기준: 6차 최종진입검증 + ATR/VWAP/시장장세 / 정밀복구",
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
        "6) 6차 직원: ATR/VWAP/시장장세로 paper OPEN 최종검증",
        "7) 공장: paper_candidates / shadow_candidates만 출력",
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
        "- 메인봇 대상: 수익형_v2.13.201.py",
        "- 페이퍼봇 대상: paper_bot_v0.39.py",
        "- 본선 후보파일: paper_candidates.jsonl / shadow_candidates.jsonl",
        "- 보조 관찰파일: 없음(candidate_events 미사용)",
        "",
        "v2.13.200 구조",
        "- 본선: 전체시장 bulk → 정밀값 순환보강 → 표준값 → 눌림 재돌파 → paper/shadow 출력",
        "- 기존 tail patch/fallback/내부 paper/BUY_READY/v343 코드 없음",
        "- paper_bot은 전략판단 금지, 받은 후보 장부 처리만 담당",
    ])
    reply(update, text)


def paper_handoff_text() -> str:
    p = paper_alive_summary()
    st = p["status"]
    return "\n".join([
        "🧪 paper handoff /paper_handoff",
        paper_bot_line(),
        f"- 후보 latest: 정식 {line_count(FILES['paper_latest'])} / 복기 {line_count(FILES['shadow_latest'])}",
        f"- 최근 cycle: open +{st.get('opened_this_cycle',0)} / close +{st.get('closed_this_cycle',0)} / {st.get('elapsed_sec','-')}s",
        "- 자세히: /paper_handoff_full",
    ])


def paper_handoff_full_text() -> str:
    return "\n".join(["🧪 paper handoff 상세 /paper_handoff_full", paper_bot_line(), candidate_fresh_text(), bot_status_text()])


def command_paper_handoff(update, context) -> None:
    reply(update, paper_handoff_text())


def command_paper_handoff_full(update, context) -> None:
    reply(update, paper_handoff_full_text())




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
    q = STATE.get('precision_queue_size', 0)
    erq = STATE.get('execution_risk_queue_size', 0)
    cache = STATE.get('precision_have', 0)
    status = "✅" if fnum(STATE.get('scan_last_sec'), 0) <= 5 else "⚠️"
    return "\n".join([
        f"{status} 직원: 정밀대기 {q} / 위험대기 {erq} / 정밀캐시 {cache}",
        f"- 직접갱신 {STATE.get('precision_sync_limit', 0)} / 나머지는 백그라운드 처리",
    ])


def check_text() -> str:
    """가벼운 기본 점검. 큰 파일 카운트/성과 계산은 full로 분리."""
    p = paper_alive_summary()
    p_icon = "✅" if p.get("alive") and fnum(p.get("age"), 9999) <= 120 else ("⚠️" if p.get("alive") else "❌")
    return "\n".join([
        "✅ 빠른 점검 /check",
        f"- 메인봇: {BOT_VERSION} / scan {scan_status_summary()['label']} / stage {STATE.get('scan_last_stage','-')}",
        f"- 시장: {STATE.get('bulk_rows',0)} / 가격 {STATE.get('bulk_price',0)} / 돈 {STATE.get('bulk_money',0)}",
        precision_status_note(),
        f"- 후보: 판단 strict {STATE.get('strict_decision',0)} / latest {STATE.get('paper_latest_written', line_count(FILES['paper_latest']))} / paper_OPEN {STATE.get('latest_trade_ready',0)} / 재확인 {STATE.get('latest_final_recheck_wait',0)} / 관찰 {STATE.get('latest_strict_observe',0)} / 복기 {STATE.get('shadow_latest_written',0)}",
        f"{p_icon} paper_bot: alive {p.get('alive')} / OPEN {p.get('status',{}).get('open_total','?')} / 갱신 {_age_text(fnum(p.get('status',{}).get('updated_at'),0))} 전",
        f"- 오류: scan {STATE.get('scan_last_error','') or '-'} / write {STATE.get('write_error','') or '-'}",
        "- 자세히: /check_full",
    ])


def check_full_text() -> str:
    return "\n".join([
        "✅ 상세 점검 /check_full",
        "[1/6] 봇", bot_status_text(),
        "", "[2/6] 허브", f"- 시장 bulk {STATE.get('bulk_rows',0)} / 가격 {STATE.get('bulk_price',0)} / 돈 {STATE.get('bulk_money',0)} / source {STATE.get('bulk_source','-')}", websocket_status_line(),
        "", "[3/6] 직원", field_line(), worker_status_text(),
        "", "[4/6] 공장", candidate_line(), candidate_fresh_text(),
        "", "[5/6] 신규성과", new_period_text(), "⚠️ 신규 표본 50건 전까지 수익률 과신 금지",
        "", "[6/6] 병목/오류", *stage_lines(), f"- 최근오류: {STATE.get('scan_last_error','') or '-'} / write_error {STATE.get('write_error','') or '-'}",
    ])


def command_check(update, context) -> None:
    reply(update, check_text())


def command_check_full(update, context) -> None:
    reply(update, check_full_text())




def _external_health_snapshot() -> Dict[str, Any]:
    """메인봇방에서 보는 가벼운 외부정보 건강상태. 재시작/업그레이드는 가드봇 담당."""
    try:
        refresh_external_ws_cache()
    except Exception as exc:
        log_error("health_refresh_ws", exc)
    try:
        refresh_micro_cache()
    except Exception as exc:
        log_error("health_refresh_micro", exc)
    latest_strict = tail_jsonl(FILES["paper_latest"], max_lines=1000)
    ws_fresh = ws_stale = ws_missing = 0
    micro_fresh = micro_stale = micro_missing = 0
    for r in latest_strict:
        ws = _ws_status_of(r)
        if ws == "fresh":
            ws_fresh += 1
        elif ws == "stale":
            ws_stale += 1
        else:
            ws_missing += 1
        ms = _micro_status_of(r)
        if ms == "fresh":
            micro_fresh += 1
        elif ms == "stale":
            micro_stale += 1
        else:
            micro_missing += 1
    ws_age = fnum(STATE.get("ws_last_age_sec"), -1)
    ws_state = str(STATE.get("ws_state") or "-")
    micro_state = str(STATE.get("micro_state") or "-")
    ws_worker_ok = ws_state == "외부수신" and 0 <= ws_age <= 20 and int(STATE.get("ws_fresh", 0) or 0) > 0
    micro_worker_ok = micro_state in {"수집중", "정상수집"} and int(STATE.get("micro_fresh", STATE.get("micro_fresh_rows", 0)) or 0) > 0
    candidate_ok = bool(latest_strict) and ws_fresh > 0 and micro_fresh > 0
    return {
        "latest_strict": latest_strict,
        "total": len(latest_strict),
        "ws_fresh": ws_fresh,
        "ws_stale": ws_stale,
        "ws_missing": ws_missing,
        "micro_fresh": micro_fresh,
        "micro_stale": micro_stale,
        "micro_missing": micro_missing,
        "ws_worker_ok": ws_worker_ok,
        "micro_worker_ok": micro_worker_ok,
        "candidate_ok": candidate_ok,
        "ws_age": ws_age,
        "ws_state": ws_state,
        "micro_state": micro_state,
    }


def external_health_warning_lines(snap: Optional[Dict[str, Any]] = None) -> List[str]:
    snap = snap or _external_health_snapshot()
    lines: List[str] = []
    total = int(snap.get("total", 0) or 0)
    if not snap.get("ws_worker_ok"):
        lines.append("⚠️ WS 직원 신선도 부족: 조건 판단 보류")
    if not snap.get("micro_worker_ok"):
        lines.append("⚠️ 호가·체결 직원 신선도 부족: 조건 판단 보류")
    if total > 0 and int(snap.get("ws_fresh", 0) or 0) <= 0:
        lines.append("⚠️ 현재 후보에 WS 신선값 0개: 손절 원인 판단 보류")
    if total > 0 and int(snap.get("micro_fresh", 0) or 0) <= 0:
        lines.append("⚠️ 현재 후보에 호가·체결 신선값 0개: 손절 원인 판단 보류")
    return lines


def health_text() -> str:
    """가벼운 통합 상태판. 상세 로그/재시작/업그레이드는 가드봇에서 본다."""
    snap = _external_health_snapshot()
    scan = scan_status_summary()
    p = paper_alive_summary()
    p_status = p.get("status", {}) if isinstance(p.get("status"), dict) else {}
    err_summary = recent_error_text(20).splitlines()[0] if recent_error_text(20).strip() else "✅ 새 실행 중 오류 없음"
    ws_icon = "✅" if snap.get("ws_worker_ok") else "⚠️"
    micro_icon = "✅" if snap.get("micro_worker_ok") else "⚠️"
    paper_icon = p.get("icon", "❔")
    warnings = external_health_warning_lines(snap)
    if not warnings:
        warnings = ["✅ 외부정보 신선도 확인됨", "✅ 이제부터 후보품질 관찰 가능"]
    else:
        warnings.append("- 재시작/상세로그/업그레이드는 가드봇에서 확인")
    return "\n".join([
        "🧭 건강상태 /health",
        "- 메인봇방용 간단 상태판입니다. 재시작/업그레이드는 가드봇 담당.",
        "",
        "[1/4] 메인봇",
        f"{scan.get('icon','❔')} {BOT_VERSION} / scan {scan.get('label','-')} / 최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / 후보 {snap.get('total',0)}개",
        f"- 오류: {err_summary}",
        "",
        "[2/4] 외부정보",
        f"{ws_icon} WS: {snap.get('ws_state','-')} / 전체신선 {STATE.get('ws_fresh',0)} / 후보신선 {snap.get('ws_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)} / 최근 {snap.get('ws_age','-')}초",
        f"{micro_icon} 호가·체결: {snap.get('micro_state','-')} / 전체신선 {STATE.get('micro_fresh', STATE.get('micro_fresh_rows',0))} / 후보신선 {snap.get('micro_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        "",
        "[3/4] paper_bot",
        f"{paper_icon} {p_status.get('version','?')} / running {p.get('ctrl_running')} / OPEN {p_status.get('open_total','?')} / CLOSED {p_status.get('closed_total','?')} / 상태 {_age_text(fnum(p_status.get('updated_at'),0))} 전",
        "",
        "[4/4] 판독",
        *warnings,
    ])

def score_text() -> str:
    """짧은 성과판. 기본 출력은 판단에 필요한 내용만 보여준다."""
    rows = load_closed()
    new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
    new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    v_strict = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in new_strict)
    lines = [
        "📊 모의매매 성과 /score",
        f"- 전체 기준: paper_bot 신규 기준점 {paper_bot_baseline_text()} / 기존 기록 삭제 없음",
        "",
        "[1/4] 현재버전 성과",
        f"- 기준: {BOT_VERSION} / {version_baseline_text()}",
        fmt_stats("현재버전 정식 모의매매", v_strict),
        "",
        "[2/4] 전체 정식 모의매매",
        fmt_stats("정식 모의매매 전체", new_strict),
        paper_bot_line(),
        "",
        "[3/4] 종료 사유",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5):
            sub = [r for r in new_strict if str(r.get("exit_reason") or "unknown") == k]
            lines.append(fmt_stats(k, sub))
    else:
        lines.append("- 아직 CLOSED 없음")
    lines += [
        "",
        "[4/4] 최근 버전별 성과",
        *version_history_lines(rows, limit=10),
        "",
        "판독",
        "- 50전 미만은 판단보류",
        "- 현재버전 평균이 +로 유지되고 손절/지지부진이 줄어드는지 확인",
    ]
    return "\n".join(lines)

def command_score(update, context) -> None:
    reply(update, score_text())

def command_version_score(update, context) -> None:
    rows = load_closed()
    vrows = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in vrows)
    lines = [
        "📊 현재버전 성과 /version_score",
        f"- 기준: {BOT_VERSION} / {version_baseline_text()} / 기존 기록 삭제 없음",
        fmt_stats("현재버전 정식 모의매매", vrows),
        "",
        "[종료 사유]",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5):
            sub = [r for r in vrows if str(r.get("exit_reason") or "unknown") == k]
            lines.append(fmt_stats(k, sub))
    else:
        lines.append("- 현재버전 CLOSED 부족")
    lines += ["", "[최근 버전별 성과 - 최대 10개]", *version_history_lines(rows, limit=10)]
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



def candidate_quality_text(full: bool = False) -> str:
    """후보품질 전용 분석. 기본은 짧게, full은 자세히.
    기준은 paper_bot 신규 기준점으로 통일해 /pscore와 전적 차이를 줄인다.
    """
    rows = load_closed(limit=30000 if full else 16000)
    new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
    strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in strict)
    latest_strict = tail_jsonl(FILES["paper_latest"], max_lines=1000)
    latest_shadow = tail_jsonl(FILES["shadow_latest"], max_lines=1000)
    trade_ready = [r for r in latest_strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    observe = [r for r in latest_strict if not (r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    win_rows = [r for r in strict if fnum(r.get("pnl_pct"), 0) > 0]
    loss_rows = [r for r in strict if fnum(r.get("pnl_pct"), 0) <= 0]

    def _ctxs(row: Dict[str, Any]) -> List[Dict[str, Any]]:
        arr: List[Dict[str, Any]] = []
        if isinstance(row, dict):
            arr.append(row)
            for k in ["entry_context", "raw", "profile"]:
                v = row.get(k)
                if isinstance(v, dict):
                    arr.append(v)
            raw = row.get("raw")
            if isinstance(raw, dict):
                for k in ["entry_context", "profile"]:
                    v = raw.get(k)
                    if isinstance(v, dict):
                        arr.append(v)
        return arr

    def ctx_get(row: Dict[str, Any], keys: Iterable[str]) -> Any:
        for ctx in _ctxs(row):
            for key in keys:
                if key in ctx and ctx.get(key) not in (None, ""):
                    return ctx.get(key)
        return None

    def avg_ctx(arr: List[Dict[str, Any]], keys: Iterable[str], minv: Optional[float] = None, maxv: Optional[float] = None) -> Tuple[float, int]:
        vals: List[float] = []
        for r in arr:
            v = ctx_get(r, keys)
            if v is None:
                continue
            try:
                x = float(v)
            except Exception:
                continue
            if math.isnan(x) or math.isinf(x):
                continue
            if minv is not None and x < minv:
                continue
            if maxv is not None and x > maxv:
                continue
            vals.append(x)
        return (sum(vals) / len(vals), len(vals)) if vals else (0.0, 0)

    def top_ctx(arr: List[Dict[str, Any]], keys: Iterable[str], limit: int = 3) -> str:
        c: Counter = Counter()
        for r in arr:
            v = ctx_get(r, keys)
            if v not in (None, ""):
                c[str(v)] += 1
        return " / ".join([f"{k} {v}" for k, v in c.most_common(limit)]) if c else "자료없음"

    def fmt_val(v: float, c: int, kind: str) -> str:
        if c <= 0:
            return "정보없음(0)"
        if kind == "pct":
            return f"{v:+.2f}%({c})"
        if kind == "money":
            return f"{v/10000:+,.0f}만({c})"
        if kind == "ratio":
            return f"{v:.2f}배({c})"
        if kind == "score":
            return f"{v:.2f}점({c})"
        return f"{v:+.2f}({c})"

    def metric(label: str, keys: List[str], kind: str = "num", minv: Optional[float] = None, maxv: Optional[float] = None, hint: str = "") -> str:
        w, wc = avg_ctx(win_rows, keys, minv=minv, maxv=maxv)
        l, lc = avg_ctx(loss_rows, keys, minv=minv, maxv=maxv)
        if wc == 0 and lc == 0:
            return f"- {label}: 정보없음 / 원인: 새 CLOSED에 아직 미저장 또는 정밀값 미수집"
        tail = f" → {hint}" if hint else ""
        return f"- {label}: 승리군 {fmt_val(w,wc,kind)} / 손실군 {fmt_val(l,lc,kind)}{tail}"

    def latest_ws_line(arr: List[Dict[str, Any]]) -> str:
        if not arr:
            return "- WS 반영: 현재 strict 후보 없음"
        fresh = 0
        stale = 0
        missing = 0
        targeted = 0
        gaps: List[float] = []
        fresh_ages: List[float] = []
        stale_ages: List[float] = []
        for r in arr:
            src = str(ctx_get(r, ["live_price_source"]) or r.get("live_price_source") or "")
            row_status = str(ctx_get(r, ["ws_row_status"]) or r.get("ws_row_status") or "")
            wf = bool(ctx_get(r, ["ws_fresh"]) or r.get("ws_fresh"))
            tv = ctx_get(r, ["ws_targeted"])
            if tv is None:
                tv = r.get("ws_targeted")
            if str(tv).lower() in ("1", "true", "yes", "y") or tv is True:
                targeted += 1
            a = ctx_get(r, ["live_age_sec"])
            ax = fnum(a, -1) if a is not None else -1
            if wf or src == "WS_SIDECAR" or row_status == "fresh":
                fresh += 1
                if ax >= 0:
                    fresh_ages.append(ax)
                g = ctx_get(r, ["current_price_ws_gap_pct"])
                if g is not None:
                    x = fnum(g, 0)
                    if abs(x) <= PRICE_RECHECK_ABNORMAL_PCT:
                        gaps.append(x)
            elif src == "WS_STALE" or row_status == "stale":
                stale += 1
                if ax >= 0:
                    stale_ages.append(ax)
            else:
                missing += 1
        avg_gap = sum(gaps)/len(gaps) if gaps else 0.0
        avg_age = sum(fresh_ages)/len(fresh_ages) if fresh_ages else -1
        stale_txt = f" / 오래됨 {stale}" if stale else ""
        miss_txt = f" / 없음 {missing}" if missing else ""
        target_txt = f" / 대상 {targeted}/{len(arr)}"
        age_txt = f" / 신선평균 {avg_age:.1f}s" if avg_age >= 0 else ""
        return f"- WS 반영: 신선 {fresh}/{len(arr)}{target_txt}{stale_txt}{miss_txt} / REST-WS 가격차 {avg_gap:+.3f}%({len(gaps)}){age_txt}"

    def latest_micro_line(arr: List[Dict[str, Any]]) -> str:
        if not arr:
            return "- 호가/체결: 현재 정식 후보 없음"
        fresh = stale = missing = targeted = 0
        spreads = []
        buys = []
        ask_pressure = sell_pressure = 0
        for r in arr or []:
            if bool(ctx_get(r, ["micro_targeted"]) or r.get("micro_targeted")):
                targeted += 1
            status = str(ctx_get(r, ["micro_row_status"]) or r.get("micro_row_status") or "missing")
            if bool(ctx_get(r, ["micro_fresh"]) or r.get("micro_fresh")) or status == "fresh":
                fresh += 1
                sp = ctx_get(r, ["micro_spread_pct"])
                br = ctx_get(r, ["micro_trade_buy_ratio_30"])
                if sp is not None and fnum(sp, 999) < 900:
                    spreads.append(fnum(sp, 0))
                if br is not None and fnum(br, 0) > 0:
                    buys.append(fnum(br, 0))
                if bool(ctx_get(r, ["micro_ask_wall_pressure"]) or r.get("micro_ask_wall_pressure")):
                    ask_pressure += 1
                if bool(ctx_get(r, ["micro_sell_trade_pressure"]) or r.get("micro_sell_trade_pressure")):
                    sell_pressure += 1
            elif status == "stale":
                stale += 1
            else:
                missing += 1
        spread_txt = f" / 평균스프레드 {sum(spreads)/len(spreads):.2f}%" if spreads else ""
        buy_txt = f" / 매수체결비율 {sum(buys)/len(buys):.2f}" if buys else ""
        pressure_txt = f" / 매도벽 {ask_pressure} / 매도체결우세 {sell_pressure}" if (ask_pressure or sell_pressure) else ""
        return f"- 호가/체결: 신선 {fresh}/{len(arr)} / 대상 {targeted}/{len(arr)} / 오래됨 {stale} / 없음 {missing}{spread_txt}{buy_txt}{pressure_txt}"

    def trade_ready_block_lines(arr: List[Dict[str, Any]], limit: int = 4) -> List[str]:
        c: Counter = Counter()
        for r in arr:
            reasons = r.get("trade_ready_reasons")
            if isinstance(reasons, str):
                reasons = [reasons]
            if not isinstance(reasons, list):
                reasons = []
            for x in reasons:
                sx = str(x or "").strip()
                if sx:
                    c[sx] += 1
        if not c:
            return ["- 진입 보류 이유: 현재 보류 후보 없음 또는 이유 미저장"]
        return ["- 진입 보류 이유: " + " / ".join(f"{k} {v}" for k, v in c.most_common(limit))]

    def top_candidate_lines(arr: List[Dict[str, Any]], limit: int = 4) -> List[str]:
        out: List[str] = []
        ranked = sorted(arr or [], key=lambda r: (fnum(r.get("score"), 0), fnum(r.get("money_flow_3m") or r.get("turnover_3m"), 0), fnum(r.get("pullback_quality_score"), 0)), reverse=True)[:limit]
        for r in ranked:
            t = str(r.get("ticker") or "-")
            reasons = r.get("trade_ready_reasons") or []
            if isinstance(reasons, str):
                reasons = [reasons]
            why = str(reasons[0]) if reasons else str(r.get("trade_ready_label") or "-")
            out.append(f"- {t}: 점수 {fnum(r.get('score'),0):.2f} / 3분돈 {fnum(r.get('money_flow_3m') or r.get('turnover_3m'),0)/10000:,.0f}만 / 눌림 {fnum(r.get('pullback_quality_score'),0):.2f} / {why[:36]}")
        return out or ["- 현재 예시 후보 없음"]

    def candidate_section_lines(arr: List[Dict[str, Any]]) -> List[str]:
        open_rows = [r for r in arr or [] if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
        recheck_rows = [r for r in arr or [] if r.get("final_entry_action") == "recheck_wait"]
        observe_rows = [r for r in arr or [] if not bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready")) and r.get("final_entry_action") != "recheck_wait"]
        out: List[str] = []
        out.append("[모의매매 진입 후보]")
        out.extend(top_candidate_lines(open_rows, limit=3))
        out.append("[조금 더 볼 후보]")
        out.extend(top_candidate_lines(recheck_rows, limit=2))
        out.append("[진입 보류 후보]")
        out.extend(top_candidate_lines(observe_rows, limit=3))
        return out

    def tag_summary(arr: List[Dict[str, Any]], limit: int = 5) -> str:
        c: Counter = Counter()
        for r in arr or []:
            tags = ctx_get(r, ["quality_risk_tags"])
            if isinstance(tags, str):
                tags = [tags]
            if isinstance(tags, list):
                for x in tags:
                    sx = str(x or "").strip()
                    if sx:
                        c[sx] += 1
        return " / ".join(f"{k} {v}" for k, v in c.most_common(limit)) if c else "자료없음"

    slow_rows = [r for r in strict if str(r.get("exit_reason") or "") == "slow_no_progress"]
    stop_rows = [r for r in strict if str(r.get("exit_reason") or "") == "stop_loss"]
    tp_rows = [r for r in strict if str(r.get("exit_reason") or "") == "take_profit"]

    def reason_profile(label: str, arr: List[Dict[str, Any]]) -> str:
        m3, c3 = avg_ctx(arr, ["money_flow_3m", "turnover_3m"])
        pb, cp = avg_ctx(arr, ["pullback_quality_score"])
        rb, cr = avg_ctx(arr, ["rebreakout_strength"])
        sp, cs = avg_ctx(arr, ["volume_spike_30x"])
        pr, cpr = avg_ctx(arr, ["price_recheck_pct"], minv=-PRICE_RECHECK_ABNORMAL_PCT, maxv=PRICE_RECHECK_ABNORMAL_PCT)
        vw, cvw = avg_ctx(arr, ["vwap_gap_pct"])
        at, cat = avg_ctx(arr, ["atr_1m_pct"])
        return f"- {label}: {len(arr)}건 / 3분돈 {m3/10000:,.0f}만({c3}) / 눌림 {pb:.2f}({cp}) / 재돌파 {rb:.2f}({cr}) / 과폭발 {sp:.2f}({cs}) / 가격재확인 {pr:+.2f}%({cpr}) / VWAP {vw:+.2f}%({cvw}) / ATR {at:.2f}%({cat})"

    key_metrics = [
        ("가격재확인", ["price_recheck_pct"], "pct", -PRICE_RECHECK_ABNORMAL_PCT, PRICE_RECHECK_ABNORMAL_PCT, "승리군은 진입가 확인이 더 좋음"),
        ("3분 돈흐름", ["money_flow_3m", "turnover_3m"], "money", None, None, "순간폭발보다 지속 흐름 확인"),
        ("1분 돈흐름", ["money_flow_1m", "turnover_1m"], "money", None, None, "너무 크면 과열 가능"),
        ("눌림품질", ["pullback_quality_score"], "score", None, None, "좋은 눌림인지 보는 내부점수"),
        ("재돌파힘", ["rebreakout_strength"], "score", None, None, "다시 올라오는 힘"),
        ("거래량과폭발", ["volume_spike_30x"], "score", None, None, "높을수록 추격/설거지 위험"),
    ]
    full_metrics = key_metrics + [
        ("5분흐름", ["change_5", "change_5m"], "pct", None, None, ""),
        ("고점거리", ["below_30m_high_pct", "below_high_pct", "high_gap_pct"], "pct", None, None, ""),
        ("저점대비", ["from_30m_low_pct", "from_low_pct"], "pct", None, None, ""),
        ("거래량배수", ["vol_ratio", "volume_ratio"], "ratio", None, None, "평소 대비 거래량 배수"),
        ("EMA5거리", ["ema5_gap_pct", "ma5_gap_pct"], "pct", None, None, ""),
        ("EMA12거리", ["ema12_gap_pct"], "pct", None, None, ""),
        ("볼린저위치", ["bb_position"], "score", None, None, ""),
        ("볼린저하단거리", ["bb_lower_gap_pct"], "pct", None, None, ""),
        ("MFI", ["mfi_14"], "score", 0, 100, ""),
        ("CCI", ["cci_20"], "score", None, None, ""),
        ("Stoch K", ["stoch_k"], "score", 0, 100, ""),
        ("ADX", ["adx_14"], "score", 0, 100, ""),
        ("ATR", ["atr_1m_pct"], "pct", 0, 10.0, "변동성"),
        ("VWAP거리", ["vwap_gap_pct"], "pct", None, None, "평균거래가격 대비"),
        ("V반등", ["v_rebound_score"], "score", None, None, ""),
        ("스프레드", ["orderbook_spread_pct"], "pct", 0, SPREAD_ABNORMAL_PCT, "실전 진입비용"),
        ("틱위험", ["tick_pct_est"], "pct", 0, 5.0, ""),
        ("실호가스프레드", ["micro_spread_pct"], "pct", 0, SPREAD_ABNORMAL_PCT, "빗썸 실제 호가"),
        ("매수체결비율", ["micro_trade_buy_ratio_30"], "score", 0, 1.0, "최근 체결 방향"),
    ]

    lines = [
        "🔬 후보품질 요약 /quality" + ("_full" if full else ""),
        f"- 기준: paper_bot 신규 기준점 {paper_bot_baseline_text()} / 기존 기록 삭제 없음",
        "",
        "[1/6] 성과",
        fmt_stats("정식 모의매매 전체", strict),
        fmt_stats("현재버전 정식 모의매매", [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()} / 기존 기록 삭제 없음",
        f"- 현재 후보: 정식 {len(latest_strict)}개 / 모의매매 진입대상 {len(trade_ready)}개 / 조금 더 볼 후보 {sum(1 for r in latest_strict if r.get('final_entry_action') == 'recheck_wait')}개 / 진입보류 {len(observe)}개 / 복기 {len(latest_shadow)}개",
        f"- 6차 최종검증: {STATE.get('final_entry_note','-')} / 시장 {STATE.get('market_context',{}).get('market_pressure','-')} / 상승비율 {STATE.get('market_context',{}).get('market_up_ratio','-')}%",
        paper_bot_line(),
        "",
        "[2/6] 현재 후보",
        latest_ws_line(latest_strict),
        latest_micro_line(latest_strict),
        *trade_ready_block_lines(observe, limit=6 if full else 4),
        f"- 위험태그: {tag_summary(latest_strict)}",
        f"- 최종진입검증: {top_ctx(latest_strict, ['final_entry_label'], limit=4)}",
        f"- 실전위험 우선확인: {STATE.get('execution_risk_sync_note','-')}",
        "후보 예시",
        *candidate_section_lines(latest_strict),
        "",
        "[3/6] 종료 사유 TOP",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5 if full else 3):
            sub = [r for r in strict if str(r.get("exit_reason") or "unknown") == k]
            lines.append(fmt_stats(k, sub))
    else:
        lines.append("- 신규 CLOSED 부족")

    lines += ["", "[3-1] 종료 사유별 특징"]
    if slow_rows or stop_rows or tp_rows:
        if slow_rows:
            lines.append(reason_profile("slow_no_progress", slow_rows))
        if stop_rows:
            lines.append(reason_profile("stop_loss", stop_rows))
        if full and tp_rows:
            lines.append(reason_profile("take_profit", tp_rows))
        if full:
            lines.append(f"- 관찰태그 slow: {tag_summary(slow_rows)}")
            lines.append(f"- 관찰태그 stop: {tag_summary(stop_rows)}")
    else:
        lines.append("- 신규 CLOSED 부족")

    lines += ["", "[4/6] 승리/손실 차이"]
    for item in (full_metrics if full else key_metrics):
        lines.append(metric(*item))
    if full:
        lines += [
            metric("WS가격차", ["current_price_ws_gap_pct"], "pct", -PRICE_RECHECK_ABNORMAL_PCT, PRICE_RECHECK_ABNORMAL_PCT, "REST와 실시간 가격 괴리"),
            metric("WS체결금액", ["ws_turnover"], "money", None, None, "sidecar 실시간 체결금액"),
            metric("WS나이", ["live_age_sec"], "score", 0, 120, "낮을수록 최신"),
            f"- 추격위험: 승리군 {top_ctx(win_rows, ['chase_risk'])} / 손실군 {top_ctx(loss_rows, ['chase_risk'])}",
            f"- 실전위험: 승리군 {top_ctx(win_rows, ['execution_risk_status'])} / 손실군 {top_ctx(loss_rows, ['execution_risk_status'])}",
            f"- 실시간소스: 승리군 {top_ctx(win_rows, ['live_price_source'])} / 손실군 {top_ctx(loss_rows, ['live_price_source'])}",
            f"- 대형주분리: 승리군 {top_ctx(win_rows, ['major_watch_label', 'major_watch'])} / 손실군 {top_ctx(loss_rows, ['major_watch_label', 'major_watch'])}",
            f"- 위험라벨: 승리군 slip {top_ctx(win_rows, ['slippage_risk'], 2)} · tick {top_ctx(win_rows, ['tick_risk'], 2)} / 손실군 slip {top_ctx(loss_rows, ['slippage_risk'], 2)} · tick {top_ctx(loss_rows, ['tick_risk'], 2)}",
        ]

    enough = len(strict) >= 100 and avg_ctx(strict, ["pullback_quality_score"])[1] >= 50
    lines += [
        "",
        "[5/6] 탈락/보류 이유",
        *reject_lines(limit=8 if full else 4),
        "",
        "[6/6] 판독",
        "❌ 전체 정식 모의매매는 아직 자동매매 불가" if score_stats(strict).get("avg", 0) < 0 else "✅ 전체 정식 모의매매 평균은 +권",
        "⚠️ 우선 볼 것: 지지부진 종료 / 손절 종료 줄이기",
        "✅ 좋아 보이는 재료: 3분 지속 돈흐름, 눌림품질, 가격재확인",
        "✅ v183부터 WS 신선도/가격차/체결금액을 진입문맥에 저장해 다음 CLOSED부터 비교",
        "❌ 위험해 보이는 재료: 1분만 튐, 거래량 과폭발, 진입 직전 가격 밀림",
        ("✅ 표본이 어느 정도 쌓임: 조건 조정 후보 검토 가능" if enough else "❔ 진입문맥 표본 더 필요: 조건 대수정은 보류"),
    ]
    if full:
        lines += ["", "[추가] 후보파일", candidate_fresh_text(), "", "[추가] 단계표", *stage_lines()]
    return "\n".join(lines)

def command_quality(update, context) -> None:
    reply(update, candidate_quality_text(False))


def command_quality_full(update, context) -> None:
    reply(update, candidate_quality_text(True))

def command_batch(update, context) -> None:
    funcs = [
        ("core", core_text),
        ("ws_status", websocket_status_text),
        ("external_status", external_status_text),
        ("check", check_text),
        ("score", score_text),
        ("paper_today", lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", "", new_period_text(), candidate_fresh_text()])),
        ("flow_check", lambda: "\n".join(["🧭 흐름검사 /flow_check", field_line(), candidate_line()] + stage_lines())),
        ("candidate_reason", lambda: "\n".join(["🔎 후보·전략 원인판 /candidate_reason", field_line(), candidate_line()] + reject_lines())),
        ("cpu_status", lambda: "\n".join(["🧠 CPU/메모리 /cpu_status", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / 최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s / stage {STATE.get('scan_last_stage','-')}"] + stage_lines())),
        ("paper_handoff", paper_handoff_text),
        ("deploy", lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.201.py", "- 페이퍼봇 대상: paper_bot_v0.39.py"])),
    ]
    start = now_ts()
    reply(update, "\n".join(["📦 묶음 명령 접수", "- 출처: /batch", f"- 실행 {len(funcs)}개", "- v2.13.187: v186 ATR 순서오류 수정 + 정밀직원 복구 + 6차 최종검증 유지", "- 기존 기록 삭제 없음 / 후보판단 고정절단 없음 / paper_bot은 최종검증 통과 후보만 OPEN"]))
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
    lines = ["🧾 v2.13.187 batch 요약", f"- 출력무결성: 실행 {len(funcs)}개", "", "⏱ 명령어별 시간표"]
    for n, sec, res in rows:
        lines.append(f"- /{n}: 처리 {sec:.2f}s / 결과 {res}")
    lines += ["", f"- 전체 경과: {now_ts()-start:.2f}s", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / scan최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s", candidate_line(), field_line(), "- 알림: 메인봇 OFF / paper_bot만 모의매매 알림"]
    reply(update, "\n".join(lines))



def tail_text(path: Path, n: int = 80) -> str:
    try:
        if not path.exists():
            return "최근 오류 없음"
        # 큰 파일 통째읽기 방지: 끝부분만 읽는다.
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - 24000))
            chunk = f.read().decode("utf-8", errors="ignore")
        lines = chunk.splitlines()[-int(n):]
        return "\n".join(lines) if lines else "최근 오류 없음"
    except Exception as exc:
        return f"오류로그 읽기 실패: {exc.__class__.__name__}: {exc}"


def recent_error_text(n: int = 60) -> str:
    """기본 /errorlog는 새 오류 요약만 보여준다. 과거 긴 traceback은 /errorlog_full로 분리한다."""
    recent = list(_recent_errors)[-int(n):]
    if recent:
        return "❌ 새 실행 중 오류 있음\n" + "\n".join(recent[-12:])
    tail = tail_text(FILES["error"], int(n))
    kinds = []
    if "first_number" in tail:
        kinds.append("과거 웹소켓 first_number")
    if "tail_text" in tail:
        kinds.append("과거 errorlog tail_text")
    if "Traceback" in tail and not kinds:
        kinds.append("과거 traceback")
    if kinds:
        return "✅ 새 실행 중 오류 없음\n- 과거 오류 흔적: " + ", ".join(kinds) + "\n- 자세히: /errorlog_full"
    return "✅ 새 실행 중 오류 없음" if not tail.strip() or "최근 오류 없음" in tail else tail[-1200:]


def recent_error_full_text(n: int = 100) -> str:
    recent = list(_recent_errors)[-int(n):]
    if recent:
        return "[새 실행 오류]\n" + "\n".join(recent[-40:])
    tail = tail_text(FILES["error"], 160)
    if not tail.strip() or "최근 오류 없음" in tail:
        return "최근 오류 없음"
    kinds = []
    if "first_number" in tail:
        kinds.append("과거 웹소켓 first_number 누락")
    if "tail_text" in tail:
        kinds.append("과거 errorlog tail_text 누락")
    if "Traceback" in tail and not kinds:
        kinds.append("과거 traceback")
    short_lines = []
    for line in tail.splitlines():
        if any(x in line for x in ["Traceback", "NameError", "Error", "Exception", "websocket_hub_worker", "multi:errorlog"]):
            short_lines.append(line[-220:])
    short = "\n".join(short_lines[-24:])
    head = "[과거 오류 요약]\n- " + (" / ".join(kinds) if kinds else "분류 안 됨")
    return head + ("\n\n[최근 오류 줄]\n" + short if short else "")


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
            "core": core_text, "main": core_text, "monitor": core_text, "status": core_text, "health": health_text,
            "check": check_text, "check_full": check_full_text, "bot_status": bot_status_text, "ws_status": websocket_status_text, "external_status": external_status_text, "quality": lambda: candidate_quality_text(False), "quality_full": lambda: candidate_quality_text(True),
            "flow_check": lambda: "\n".join(["🧭 흐름검사 /flow_check", field_line(), candidate_line()] + stage_lines()),
            "candidate_reason": lambda: "\n".join(["🔎 후보·전략 원인판 /candidate_reason", field_line(), candidate_line()] + reject_lines()),
            "cpu_status": lambda: "\n".join(["🧠 CPU/메모리 /cpu_status", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / 최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s / stage {STATE.get('scan_last_stage','-')}"] + stage_lines()),
            "paper_handoff": paper_handoff_text, "paper_handoff_full": paper_handoff_full_text,
            "deploy": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.201.py", "- 페이퍼봇 대상: paper_bot_v0.39.py"]),
            "upgradestatus": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.201.py", "- 페이퍼봇 대상: paper_bot_v0.39.py"]),
            "paper_today": lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", "", new_period_text(), candidate_fresh_text()]),
            "score": score_text,
            "version_score": lambda: "📊 현재버전 성과 /version_score\n" + fmt_stats("현재버전 정식 모의매매", [r for r in rows_since_current_version(load_closed()) if str(r.get("lane")) == "strict"]) + f"\n- 기준: {BOT_VERSION} / {version_baseline_text()} / 기존 기록 삭제 없음",
            "version_compare": version_compare_text,
            "compare_version": version_compare_text,
            "compare": version_compare_text,
            "loss_review": loss_review_text,
            "version_loss": loss_review_text,
            "loss": loss_review_text,
            "trade": lambda: "\n".join(["🔒 거래 상태 /trade", "- 자동매수: OFF", "- BUY_READY: 생성 안 함", "- 실제 주문: 없음"]),
            "prune_check": lambda: "\n".join(["🧹 가지치기 점검 /prune_check", "- candidate_events 없음", "- BUY_READY 구경로 없음", "- 내부 paper 없음", "- shadow는 복기전용"]),
            "errorlog": lambda: "🧯 오류로그 /errorlog\n\n" + recent_error_text(60),
            "errorlog_full": lambda: "🧯 오류로그 상세 /errorlog_full\n\n" + recent_error_full_text(120)[-3200:],
            "help": lambda: "📚 명령어 /help\n- 여러 명령을 줄바꿈으로 보내도 자동 묶음 처리됨\n- /batch /health /check /core /external_status /quality /score /version_score /version_compare /loss_review /cpu_status /deploy /errorlog_full",
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
    total_start = now_ts()
    reply(update, f"📦 자동 묶음 명령 접수\n- /batch 없이 여러 줄 명령 감지\n- 실행 {len(lines)}개")
    total = len(lines)
    rows = []
    for idx, line in enumerate(lines, start=1):
        st = now_ts()
        name = _command_name_from_line(line)
        if name == "batch":
            body = "이미 자동 묶음 처리 중이라 /batch는 건너뜀"
            res = "SKIP"
        else:
            fn = _builder_for_command(name)
            if not fn:
                body = f"알 수 없는 명령: /{name}"
                res = "UNKNOWN"
            else:
                try:
                    if name in {"loss_review", "version_loss", "loss"}:
                        parts = str(line or "").strip().split()
                        body = str(loss_review_text(parts[1] if len(parts) > 1 else None))
                    else:
                        body = str(fn())
                    res = "OK"
                except Exception as exc:
                    log_error(f"multi:{name}", exc)
                    body = f"오류: {exc.__class__.__name__}: {exc}"
                    res = f"ERR {exc.__class__.__name__}"
        sec = now_ts() - st
        rows.append((name, sec, res))
        reply(update, f"[{idx}/{total}] /{name} ({sec:.2f}s / {res})\n" + body)
    summary = ["🧾 자동 묶음 시간표"]
    for name, sec, res in rows:
        icon = "✅" if res == "OK" else ("❔" if res in {"SKIP", "UNKNOWN"} else "❌")
        summary.append(f"- {icon} /{name}: {sec:.2f}s / {res}")
    summary.append(f"- 전체: {now_ts() - total_start:.2f}s")
    reply(update, "\n".join(summary))
    return True



def command_scan_now(update, context) -> None:
    reply(update, "🔁 즉시 스캔 1회 실행")
    scan_once()
    reply(update, "\n".join(["✅ 즉시 스캔 완료", candidate_line(), field_line(), f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s"]))


def command_errorlog(update, context) -> None:
    reply(update, "🧯 오류로그 /errorlog\n\n" + recent_error_text(80))


def command_errorlog_full(update, context) -> None:
    reply(update, "🧯 오류로그 상세 /errorlog_full\n\n" + recent_error_full_text(120)[-3200:])


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
        "- /batch /health /core /external_status /quality /score /version_score",
        "흐름/후보",
        "- /version_compare /loss_review /loss_review 194 /paper_handoff /deploy",
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


# ===============================
# v2.13.194: 기본 출력 가독성 정리 + 최근 3/6/12시간 성과 + 외부정보 통합상태
# - 전략/청산/자동매수/BUY_READY는 변경하지 않는다.
# - 긴 분석은 *_full로 보내고, 기본 명령은 판단용 핵심만 보여준다.
# ===============================
_legacy_score_text_v193 = score_text
_legacy_quality_text_v193 = candidate_quality_text

RECENT_SCORE_HOURS = 12
RECENT_SCORE_WINDOWS = (3, 6, 12)


def _ctxv(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            if key in row:
                return row.get(key)
            ctx = row.get("entry_context")
            if isinstance(ctx, dict) and key in ctx:
                return ctx.get(key)
            raw = row.get("raw")
            if isinstance(raw, dict):
                if key in raw:
                    return raw.get(key)
                ctx = raw.get("entry_context")
                if isinstance(ctx, dict) and key in ctx:
                    return ctx.get(key)
    except Exception:
        pass
    return default


def rows_recent_hours(rows: Iterable[Dict[str, Any]], hours: int = RECENT_SCORE_HOURS) -> List[Dict[str, Any]]:
    cutoff = now_ts() - max(1, int(hours or 12)) * 3600
    return [r for r in rows or [] if closed_ts(r) >= cutoff]


def _reason_summary(rows: List[Dict[str, Any]], limit: int = 3) -> str:
    c = Counter(label_kr(str(r.get("exit_reason") or "unknown")) for r in rows or [])
    return " / ".join(f"{k} {v}" for k, v in c.most_common(limit)) if c else "종료 없음"


def _compact_stat_line(label: str, rows: List[Dict[str, Any]], *, icon_by_sample: bool = True) -> str:
    st = score_stats(rows)
    icon = score_icon(st) if icon_by_sample else ("✅" if st.get("avg", 0) > 0 else ("❌" if st.get("n", 0) else "❔"))
    note = sample_note(st.get("n", 0))
    return f"{icon} {label}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%{note}"


def _short_version_history_lines(rows: Iterable[Dict[str, Any]], limit: int = 5) -> List[str]:
    lines = version_history_lines(rows, limit=limit)
    # 너무 긴 '버전 미기록'은 기본 화면에서 마지막 1개만 남기거나 생략한다.
    short = []
    for line in lines:
        if line.startswith("❌ 버전 미기록"):
            continue
        short.append(line)
    return short[:limit] if short else lines[:min(limit, len(lines))]


def external_status_text() -> str:
    """메인봇 관점의 외부정보 통합상태. 프로세스 상세는 가드봇 /gexternal_state에서 본다."""
    refresh_external_ws_cache()
    refresh_micro_cache()
    latest_strict = tail_jsonl(FILES["paper_latest"], max_lines=1000)
    total = len(latest_strict)

    def _ws_counts(arr: List[Dict[str, Any]]) -> Tuple[int, int, int, int]:
        fresh = targeted = stale = missing = 0
        for r in arr or []:
            status = str(_ctxv(r, "ws_row_status", "") or "")
            if str(_ctxv(r, "ws_targeted", r.get("ws_targeted"))).lower() in {"true", "1", "yes", "y"} or _ctxv(r, "ws_targeted") is True or r.get("ws_targeted") is True:
                targeted += 1
            if bool(_ctxv(r, "ws_fresh")) or status == "fresh" or str(_ctxv(r, "live_price_source", "")) == "WS_SIDECAR":
                fresh += 1
            elif status == "stale" or str(_ctxv(r, "live_price_source", "")) == "WS_STALE":
                stale += 1
            else:
                missing += 1
        return fresh, targeted, stale, missing

    def _micro_counts(arr: List[Dict[str, Any]]) -> Tuple[int, int, int, int]:
        fresh = targeted = stale = missing = 0
        for r in arr or []:
            status = str(_ctxv(r, "micro_row_status", "missing") or "missing")
            if bool(_ctxv(r, "micro_targeted")) or r.get("micro_targeted") is True:
                targeted += 1
            if bool(_ctxv(r, "micro_fresh")) or status == "fresh":
                fresh += 1
            elif status == "stale":
                stale += 1
            else:
                missing += 1
        return fresh, targeted, stale, missing

    wf, wt, ws, wm = _ws_counts(latest_strict)
    mf, mt, ms, mm = _micro_counts(latest_strict)
    ws_ok = str(STATE.get("ws_state") or "") == "외부수신" and fnum(STATE.get("ws_last_age_sec"), 9999) <= 20
    micro_ok = int(STATE.get("micro_fresh", STATE.get("micro_fresh_rows", 0)) or 0) > 0 or mf > 0
    lines = [
        "🛰 외부정보 상태 /external_status",
        "",
        "[1/3] 직원 상태",
        f"{'✅' if ws_ok else '⚠️'} 웹소켓: {STATE.get('ws_state','-')} / 대상 {STATE.get('ws_targets',0)} / 신선 {STATE.get('ws_fresh',0)} / 최근 {STATE.get('ws_last_age_sec','-')}초 / 갱신 {STATE.get('ws_target_write_note','-')}",
        f"{'✅' if micro_ok else '⚠️'} 호가·체결: 대상 {STATE.get('micro_target_file_targets',0)} / 신선 {STATE.get('micro_fresh_rows', STATE.get('micro_fresh',0))} / 상태 {STATE.get('micro_state','캐시확인')}",
        "",
        "[2/3] 현재 후보 반영",
        f"- 정식 후보 {total}개",
        f"- 웹소켓: 신선 {wf} / 대상 {wt} / 오래됨 {ws} / 없음 {wm}",
        f"- 호가·체결: 신선 {mf} / 대상 {mt} / 오래됨 {ms} / 없음 {mm}",
        "",
        "[3/3] 판독",
    ]
    if total <= 0:
        lines.append("❔ 현재 정식 후보 없음")
    else:
        if wf <= 0:
            lines.append("⚠️ 후보에 웹소켓 신선값이 부족함")
        else:
            lines.append("✅ 웹소켓 후보 반영 중")
        if mf <= 0:
            lines.append("⚠️ 호가·체결 신선값이 부족함")
        else:
            lines.append("✅ 호가·체결 후보 반영 중")
    lines.append("- 프로세스 상세는 가드봇 /gexternal_state")
    return "\n".join(lines)


def score_text() -> str:
    rows = load_closed()
    new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
    new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    recent_by_hours = {h: [r for r in rows_recent_hours(new_strict, h) if str(r.get("lane")) == "strict"] for h in RECENT_SCORE_WINDOWS}
    recent_strict = recent_by_hours.get(12, [])
    v_strict = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
    by_reason_recent = Counter(str(r.get("exit_reason") or "unknown") for r in recent_strict)
    lines = [
        "📊 모의매매 성과 /score",
        "- 기본 기준: 최근 3시간 / 6시간 / 12시간 + 현재버전",
        "- 전체 기록 삭제 없음",
        "",
        "[1/4] 최근 성과",
    ]
    for h in RECENT_SCORE_WINDOWS:
        lines.append(_compact_stat_line(f"최근 {h}시간 정식", recent_by_hours.get(h, [])))
    lines += [
        "",
        "[2/4] 현재버전",
        f"- 기준: {BOT_VERSION} / {version_baseline_text()}",
        _compact_stat_line("현재버전 정식", v_strict),
        f"- 주요종료: {_reason_summary(v_strict)}",
        "",
        "[3/4] 최근 12시간 손실 원인",
    ]
    if by_reason_recent:
        for k, _ in by_reason_recent.most_common(4):
            sub = [r for r in recent_strict if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_compact_stat_line(k, sub))
    else:
        lines.append("- 최근 12시간 CLOSED 부족")
    lines += [
        "",
        "[4/4] 최근 버전별 성과",
        *_short_version_history_lines(rows, limit=5),
        "",
        "판독",
        "- 50전 미만은 판단보류",
        "- 3/6/12시간과 현재버전 평균이 +로 유지되는지 확인",
        "- 긴 전체표는 /score_full",
    ]
    return "\n".join(lines)


def score_full_text() -> str:
    return _legacy_score_text_v193()


def command_score_full(update, context) -> None:
    reply(update, score_full_text())


def _candidate_brief_lines(items: List[Dict[str, Any]], limit: int = 2) -> List[str]:
    if not items:
        return ["- 없음"]
    out: List[str] = []
    for r in sorted(items, key=lambda x: fnum(x.get("score"), 0), reverse=True)[:limit]:
        t = str(r.get("ticker") or r.get("symbol") or "?")
        score = fnum(r.get("score"), 0)
        m3 = fnum(r.get("money_flow_3m") or r.get("turnover_3m"), 0)
        pb = fnum(r.get("pullback_quality_score"), 0)
        label = str(_ctxv(r, "final_entry_label", r.get("one_liner") or "-"))
        label = label.replace("관찰전환: 쓰레기후보 slow/펌핑 위험", "진입보류: 펌핑 의심")
        label = label.replace("재확인대기: stop/밀림 위험", "조금 더 보기: 밀림 위험")
        label = label.replace("자동매매 검증급 OPEN", "모의진입")
        out.append(f"- {t}: 점수 {score:.2f} / 3분돈 {m3/10000:,.0f}만 / 눌림 {pb:.2f} / {label}")
    return out


def _simple_ws_line(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "- 웹소켓: 후보 없음"
    fresh = targeted = stale = missing = 0
    for r in items:
        status = str(_ctxv(r, "ws_row_status", "") or "")
        targeted += 1 if (bool(_ctxv(r, "ws_targeted")) or r.get("ws_targeted") is True or str(_ctxv(r, "ws_targeted", "")).lower() in {"true","1","yes"}) else 0
        if bool(_ctxv(r, "ws_fresh")) or status == "fresh" or str(_ctxv(r, "live_price_source", "")) == "WS_SIDECAR":
            fresh += 1
        elif status == "stale" or str(_ctxv(r, "live_price_source", "")) == "WS_STALE":
            stale += 1
        else:
            missing += 1
    return f"- 웹소켓: 신선 {fresh}/{len(items)} / 대상 {targeted}/{len(items)} / 오래됨 {stale} / 없음 {missing}"


def _simple_micro_line(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "- 호가·체결: 후보 없음"
    fresh = targeted = stale = missing = 0
    spreads: List[float] = []
    buys: List[float] = []
    for r in items:
        status = str(_ctxv(r, "micro_row_status", "missing") or "missing")
        targeted += 1 if (bool(_ctxv(r, "micro_targeted")) or r.get("micro_targeted") is True) else 0
        if bool(_ctxv(r, "micro_fresh")) or status == "fresh":
            fresh += 1
            sp = _ctxv(r, "micro_spread_pct")
            br = _ctxv(r, "micro_trade_buy_ratio_30")
            if sp is not None and fnum(sp, 999) < 900:
                spreads.append(fnum(sp, 0))
            if br is not None and fnum(br, -1) >= 0:
                buys.append(fnum(br, 0))
        elif status == "stale":
            stale += 1
        else:
            missing += 1
    sp_txt = f" / 평균스프레드 {sum(spreads)/len(spreads):.2f}%" if spreads else ""
    br_txt = f" / 매수체결비율 {sum(buys)/len(buys):.2f}" if buys else ""
    return f"- 호가·체결: 신선 {fresh}/{len(items)} / 대상 {targeted}/{len(items)} / 오래됨 {stale} / 없음 {missing}{sp_txt}{br_txt}"


def candidate_quality_text(full: bool = False) -> str:
    if full:
        return _legacy_quality_text_v193(True)
    rows = load_closed()
    new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
    new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    recent_by_hours = {h: [r for r in rows_recent_hours(new_strict, h) if str(r.get("lane")) == "strict"] for h in RECENT_SCORE_WINDOWS}
    recent_strict = recent_by_hours.get(12, [])
    v_strict = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
    latest_strict = tail_jsonl(FILES["paper_latest"], max_lines=1000)
    latest_shadow = tail_jsonl(FILES["shadow_latest"], max_lines=1000)
    open_rows = [r for r in latest_strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    recheck_rows = [r for r in latest_strict if r.get("final_entry_action") == "recheck_wait"]
    observe_rows = [r for r in latest_strict if r not in open_rows and r not in recheck_rows]
    reasons = Counter(str(r.get("exit_reason") or "unknown") for r in recent_strict)
    # 기본화면에서는 "최종검증 통과"를 보류 이유로 보이지 않게 분리한다.
    # 통과했지만 모의진입이 아닌 후보는 대부분 외부정보/실전위험 확인 대기다.
    rc = Counter()
    pass_after_check = 0
    for r in observe_rows:
        label = str(_ctxv(r, "final_entry_label", r.get("final_entry_label") or "") or "")
        action = str(_ctxv(r, "final_entry_action", r.get("final_entry_action") or "") or "")
        if "펌핑" in label or "slow" in label:
            rc["펌핑 의심"] += 1
        elif "밀림" in label or "stop" in label:
            rc["밀림 위험"] += 1
        elif "스프레드" in label:
            rc["스프레드 주의"] += 1
        elif "통과" in label or action in {"paper_open", "open"}:
            pass_after_check += 1
        else:
            # 내부명이 남아도 기본화면에서는 짧게만 보여준다.
            v = label or str(_ctxv(r, "trade_block_reason", r.get("reason") or "") or "")
            if v:
                v = v.replace("관찰전환: 쓰레기후보 slow/펌핑 위험", "펌핑 의심")
                v = v.replace("재확인대기: stop/밀림 위험", "밀림 위험")
                v = v.replace("최종검증 통과", "외부정보/실전위험 확인중")
                rc[v[:40]] += 1
    parts = [f"{k} {v}" for k, v in rc.most_common(4)]
    if pass_after_check:
        parts.append(f"외부정보/실전위험 확인중 {pass_after_check}")
    block = ["- 진입 보류 이유: " + (" / ".join(parts[:5]) if parts else "자료없음")]
    health_snap = _external_health_snapshot()
    health_warnings = external_health_warning_lines(health_snap)
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기본 기준: 최근 3시간 / 6시간 / 12시간 + 현재버전",
        "- 전체 기록 삭제 없음",
    ]
    if health_warnings:
        lines += [
            "",
            "[0/5] 먼저 확인",
            *health_warnings,
            "- 외부직원 정지/오래됨 구간의 성과는 조건 판단에서 분리해서 봐야 함",
        ]
    lines += [
        "",
        "[1/5] 성과 요약",
    ]
    for h in RECENT_SCORE_WINDOWS:
        lines.append(_compact_stat_line(f"최근 {h}시간 정식", recent_by_hours.get(h, [])))
    lines += [
        _compact_stat_line("현재버전 정식", v_strict),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(latest_strict)}개 / 🧪 모의진입 {len(open_rows)}개 / ⚠️ 조금 더 보기 {len(recheck_rows)}개 / ❌ 진입보류 {len(observe_rows)}개 / 복기 {len(latest_shadow)}개",
        f"- 최종검증: {STATE.get('final_entry_note','-')} / 시장 {STATE.get('market_context',{}).get('market_pressure','-')} / 상승비율 {STATE.get('market_context',{}).get('market_up_ratio','-')}%",
        _simple_ws_line(latest_strict),
        _simple_micro_line(latest_strict),
        *block[:2],
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
        *_candidate_brief_lines(open_rows, 2),
        "⚠️ 조금 더 보기",
        *_candidate_brief_lines(recheck_rows, 2),
        "❌ 진입보류",
        *_candidate_brief_lines(observe_rows, 2),
        "",
        "[4/5] 최근 12시간 종료 사유",
    ]
    if reasons:
        for k, _ in reasons.most_common(4):
            sub = [r for r in recent_strict if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_compact_stat_line(k, sub))
    else:
        lines.append("- 최근 CLOSED 부족")
    lines += [
        "",
        "[5/5] 판독",
        "❌ 전체 누적은 아직 자동매매 불가" if score_stats(new_strict).get("avg", 0) < 0 else "✅ 전체 누적 평균 +권",
        "⚠️ 우선 볼 것: 손절 / 지지부진 감소",
        "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결",
        "- 긴 승리/손실 차이와 종료사유별 특징은 /quality_full",
    ]
    return "\n".join(lines)



def _version_feature_stats(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "- 표본 없음"
    def avg_field(*keys: str) -> str:
        vals = []
        for r in rows:
            for k in keys:
                v = _ctxv(r, k, r.get(k))
                if v is not None:
                    fv = fnum(v, None)
                    if fv is not None and fv < 900:
                        vals.append(float(fv))
                    break
        return "-" if not vals else f"{sum(vals)/len(vals):.2f}({len(vals)})"
    ws_counts = Counter(_ws_status_of(r) for r in rows)
    micro_counts = Counter(_micro_status_of(r) for r in rows)
    return (
        f"- 종료: {_reason_summary(rows)}\n"
        f"- 3분돈 평균: {avg_field('money_flow_3m','turnover_3m')} / 눌림: {avg_field('pullback_quality_score')} / 과폭발: {avg_field('volume_spike_score')}\n"
        f"- 호가스프레드: {avg_field('micro_spread_pct')} / 매수체결비율: {avg_field('micro_trade_buy_ratio_30')} / WS나이: {avg_field('ws_age_sec','live_age_sec')}\n"
        f"- WS 신선/오래됨/없음: {ws_counts.get('fresh',0)}/{ws_counts.get('stale',0)}/{ws_counts.get('missing',0)} / micro 신선/오래됨/없음: {micro_counts.get('fresh',0)}/{micro_counts.get('stale',0)}/{micro_counts.get('missing',0)}"
    )


def version_compare_text() -> str:
    rows = [r for r in load_closed() if str(r.get("lane")) == "strict"]
    by_ver: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        ver = str(r.get("opened_brain_version") or r.get("brain_version") or r.get("bot_version") or "버전 미기록")
        by_ver[ver].append(r)
    # 최근 버전명 우선. v192/v193/v194/v195가 있으면 앞쪽에 보이게 한다.
    preferred = [BOT_VERSION, "수익형 v2.13.194", "수익형 v2.13.193", "수익형 v2.13.192", "수익형 v2.13.190"]
    keys = []
    for k in preferred:
        if k in by_ver and k not in keys:
            keys.append(k)
    for k in sorted(by_ver.keys(), reverse=True):
        if k not in keys and len(keys) < 6:
            keys.append(k)
    lines = ["📊 버전 비교 /version_compare", "- 최근 버전별 정식 모의매매와 진입 당시 정보 비교", ""]
    for k in keys[:6]:
        arr = by_ver.get(k, [])
        st = score_stats(arr)
        lines.append(f"{score_icon(st)} {k}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%{sample_note(st['n'])}")
        lines.append(_version_feature_stats(arr))
        lines.append("")
    lines.append("판독")
    lines.append("- 50전 미만은 방향만 참고")
    lines.append("- micro 값은 v193 이후부터 주로 의미 있음")
    return "\n".join(lines).strip()


def command_version_compare(update, context) -> None:
    reply(update, version_compare_text())


# ===============================
# v2.13.196: 버전 비교 연결 + 특이값 제외 성과 + 손절 원인 요약
# v2.13.199: WS 신선도 고정판(target TTL/debounce + age 기준 통일)
# - 전략/청산/자동매수/BUY_READY는 변경하지 않는다.
# - full 원자료를 매번 보지 않아도 이전 버전 손절·특이값을 짧게 확인한다.
# ===============================

def _strict_closed_rows() -> List[Dict[str, Any]]:
    return [r for r in load_closed() if str(r.get("lane")) == "strict"]


def _version_label_from_token(token: Optional[str]) -> str:
    raw = str(token or "").strip()
    if not raw:
        return BOT_VERSION
    if raw.startswith("수익형"):
        return raw
    raw = raw.lstrip("vV")
    if raw.startswith("2.13."):
        return "수익형 v" + raw
    if raw.isdigit():
        return "수익형 v2.13." + raw
    if "2.13." in raw:
        return "수익형 v" + raw.split("v")[-1]
    return raw


def _rows_for_version(rows: Iterable[Dict[str, Any]], token: Optional[str]) -> Tuple[str, List[Dict[str, Any]]]:
    label = _version_label_from_token(token)
    out = [r for r in rows or [] if row_brain_version(r) == label]
    if out:
        return label, out
    low = str(token or label).lower().replace("수익형", "").replace(" ", "")
    alt: List[Dict[str, Any]] = []
    found_label = label
    for r in rows or []:
        ver = row_brain_version(r)
        vlow = ver.lower().replace("수익형", "").replace(" ", "")
        if low and low in vlow:
            alt.append(r)
            found_label = ver
    return found_label, alt


def _pnl(row: Dict[str, Any]) -> float:
    return fnum(row.get("pnl_pct"), 0.0)


def _outlier_summary(rows: List[Dict[str, Any]]) -> List[str]:
    arr = list(rows or [])
    st = score_stats(arr)
    if not arr:
        return ["- 특이값: 종료 기록 없음"]
    top = max(arr, key=_pnl)
    low = min(arr, key=_pnl)
    top_p = _pnl(top)
    low_p = _pnl(low)
    total = st["total"]
    top_sym = str(top.get("symbol") or top.get("ticker") or "?")
    low_sym = str(low.get("symbol") or low.get("ticker") or "?")
    n2 = max(0, st["n"] - 1)
    excl_top_total = total - top_p
    excl_low_total = total - low_p
    excl_top_avg = (excl_top_total / n2) if n2 else 0.0
    excl_low_avg = (excl_low_total / n2) if n2 else 0.0
    flag = "⚠️" if st["total"] > 0 and excl_top_total < 0 else ("✅" if st["avg"] > 0 else "❌")
    return [
        f"{flag} 최대수익 제외: 합산 {excl_top_total:+.2f}% / 평균 {excl_top_avg:+.2f}%",
        f"- 최대수익: {top_sym} {top_p:+.2f}% / {label_kr(str(top.get('exit_reason') or 'unknown'))}",
        f"- 최대손실: {low_sym} {low_p:+.2f}% / {label_kr(str(low.get('exit_reason') or 'unknown'))}",
        f"- 최대손실 제외: 합산 {excl_low_total:+.2f}% / 평균 {excl_low_avg:+.2f}%",
    ]


def _micro_status_of(row: Dict[str, Any]) -> str:
    status = str(_ctxv(row, "micro_row_status", "missing") or "missing")
    if bool(_ctxv(row, "micro_fresh")) or status == "fresh":
        return "fresh"
    if status == "stale":
        return "stale"
    return "missing"


def _ws_status_of(row: Dict[str, Any]) -> str:
    status = str(_ctxv(row, "ws_row_status", "") or "")
    source = str(_ctxv(row, "live_price_source", "") or "")
    age = fnum(_ctxv(row, "ws_age_sec", _ctxv(row, "live_age_sec", -1)), -1)
    if bool(_ctxv(row, "ws_fresh")) or status == "fresh" or source == "WS_SIDECAR" or (0 <= age <= WS_HUB_STALE_SEC):
        return "fresh"
    if status == "stale" or source == "WS_STALE" or age >= 0:
        return "stale"
    return "missing"


def _avg_ctx(rows: List[Dict[str, Any]], key: str, *, max_valid: float = 900.0) -> str:
    vals: List[float] = []
    for r in rows or []:
        v = _ctxv(r, key, r.get(key))
        if v is None:
            continue
        fv = fnum(v, None)
        if fv is not None and fv < max_valid:
            vals.append(float(fv))
    return "-" if not vals else f"{sum(vals)/len(vals):.2f}({len(vals)})"


def _loss_review_for_rows(label: str, rows: List[Dict[str, Any]]) -> str:
    arr = list(rows or [])
    stops = [r for r in arr if str(r.get("exit_reason") or "") == "stop_loss"]
    slow = [r for r in arr if str(r.get("exit_reason") or "") == "slow_no_progress"]
    micro_counts = Counter(_micro_status_of(r) for r in stops)
    ws_counts = Counter(_ws_status_of(r) for r in stops)
    bad_buy = sum(1 for r in stops if fnum(_ctxv(r, "micro_trade_buy_ratio_30", -1), -1) >= 0 and fnum(_ctxv(r, "micro_trade_buy_ratio_30", 0), 0) < MICRO_BUY_RATIO_WEAK)
    wide_spread = sum(1 for r in stops if 0 <= fnum(_ctxv(r, "micro_spread_pct", 999), 999) < 900 and fnum(_ctxv(r, "micro_spread_pct", 0), 0) >= MICRO_SPREAD_HARD_PCT)
    sell_pressure = sum(1 for r in stops if bool(_ctxv(r, "micro_sell_trade_pressure")) or bool(_ctxv(r, "micro_ask_wall_pressure")))
    lines = [
        f"🧯 손절 원인 요약 /loss_review {label}",
        fmt_stats(f"{label} 정식", arr),
        *_outlier_summary(arr),
        "",
        "[손절 요약]",
        f"- 손절 {len(stops)}건 / 지지부진 {len(slow)}건",
        f"- 손절 micro: 신선 {micro_counts.get('fresh',0)} / 오래됨 {micro_counts.get('stale',0)} / 없음 {micro_counts.get('missing',0)}",
        f"- 손절 웹소켓: 신선 {ws_counts.get('fresh',0)} / 오래됨 {ws_counts.get('stale',0)} / 없음 {ws_counts.get('missing',0)}",
        f"- 손절 평균스프레드 {_avg_ctx(stops, 'micro_spread_pct')} / 매수체결비율 {_avg_ctx(stops, 'micro_trade_buy_ratio_30', max_valid=2)}",
        f"- 약한매수 {bad_buy} / 넓은스프레드 {wide_spread} / 매도압박 {sell_pressure}",
        "",
        "[대표 손절]",
    ]
    if not stops:
        lines.append("- 손절 없음")
    else:
        for r in sorted(stops, key=_pnl)[:8]:
            sym = str(r.get("symbol") or r.get("ticker") or "?")
            sp = fnum(_ctxv(r, "micro_spread_pct", 999), 999)
            br = fnum(_ctxv(r, "micro_trade_buy_ratio_30", -1), -1)
            sp_txt = "-" if sp >= 900 else f"{sp:.2f}%"
            br_txt = "-" if br < 0 else f"{br:.2f}"
            lines.append(f"- {sym}: {_pnl(r):+.2f}% / micro {_micro_status_of(r)} / WS {_ws_status_of(r)} / 스프레드 {sp_txt} / 매수비율 {br_txt}")
    lines += [
        "",
        "판독",
        "- micro 없음/오래됨 손절이 많으면 배관·대상선반영 문제",
        "- micro 신선인데 손절이 많으면 조건/차단 기준 문제",
    ]
    return "\n".join(lines)


def loss_review_text(token: Optional[str] = None) -> str:
    rows = _strict_closed_rows()
    label, arr = _rows_for_version(rows, token)
    return _loss_review_for_rows(label, arr)


def command_loss_review(update, context) -> None:
    token = None
    try:
        args = list(getattr(context, "args", []) or [])
        token = args[0] if args else None
    except Exception:
        token = None
    reply(update, loss_review_text(token))


def version_compare_text() -> str:
    rows = _strict_closed_rows()
    by_ver: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_ver[row_brain_version(r)].append(r)
    preferred = [BOT_VERSION, "수익형 v2.13.198", "수익형 v2.13.197", "수익형 v2.13.196", "수익형 v2.13.195", "수익형 v2.13.194", "수익형 v2.13.193", "수익형 v2.13.192"]
    keys: List[str] = []
    for k in preferred:
        if k in by_ver and k not in keys:
            keys.append(k)
    for k in sorted(by_ver.keys(), reverse=True):
        if k not in keys and len(keys) < 6 and k != "버전 미기록":
            keys.append(k)
    lines = ["📊 버전 비교 /version_compare", "- 특이값 제외 성과와 손절 원인을 같이 본다", ""]
    for k in keys[:6]:
        arr = by_ver.get(k, [])
        st = score_stats(arr)
        stops = [r for r in arr if str(r.get("exit_reason") or "") == "stop_loss"]
        micro_counts = Counter(_micro_status_of(r) for r in stops)
        lines.append(f"{score_icon(st)} {k}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%{sample_note(st['n'])}")
        for ln in _outlier_summary(arr)[:2]:
            lines.append(ln)
        ws_counts = Counter(_ws_status_of(r) for r in stops)
        lines.append(f"- 손절 {len(stops)}건 / WS 신선 {ws_counts.get('fresh',0)} · 오래됨 {ws_counts.get('stale',0)} · 없음 {ws_counts.get('missing',0)} / micro 신선 {micro_counts.get('fresh',0)} · 오래됨 {micro_counts.get('stale',0)} · 없음 {micro_counts.get('missing',0)}")
        lines.append(_version_feature_stats(arr))
        lines.append("")
    lines += [
        "판독",
        "- 전체 합산이 좋아도 최대수익 제외가 마이너스면 한 방 착시",
        "- 손절이 micro 없음/오래됨에 몰리면 배관 문제부터 본다",
        "- 상세 손절은 /loss_review 또는 /loss_review 194",
    ]
    return "\n".join(lines).strip()

def command_external_status(update, context) -> None:
    reply(update, external_status_text())


def install_commands(updater: Any) -> None:
    dp = updater.dispatcher
    mapping = {
        "batch": command_batch,
        "core": command_core,
        "main": command_core,
        "monitor": command_core,
        "status": command_core,
        "health": lambda u,c: reply(u, health_text()),
        "score": command_score,
        "score_full": command_score_full,
        "version_score": command_version_score,
        "version_compare": command_version_compare,
        "compare_version": command_version_compare,
        "compare": command_version_compare,
        "loss_review": command_loss_review,
        "version_loss": command_loss_review,
        "loss": command_loss_review,
        "check": command_check,
        "check_full": command_check_full,
        "bot_status": lambda u,c: reply(u, bot_status_text()),
        "ws_status": lambda u,c: reply(u, websocket_status_text()),
        "external_status": command_external_status,
        "quality": command_quality,
        "quality_full": command_quality_full,
        "paper_today": command_paper_today,
        "flow_check": command_flow,
        "candidate_reason": command_candidate_reason,
        "cpu_status": command_cpu,
        "paper_handoff": command_paper_handoff,
        "paper_handoff_full": command_paper_handoff_full,
        "deploy": command_deploy,
        "upgradestatus": command_deploy,
        "trade": command_trade,
        "prune_check": command_prune_check,
        "scan_now": command_scan_now,
        "errorlog": command_errorlog,
        "errorlog_full": command_errorlog_full,
        "deep": command_check,
        "upgradebot": command_deploy,
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
        menu = ["batch", "health", "check", "core", "external_status", "quality", "score", "version_score", "version_compare", "loss_review", "cpu_status", "paper_handoff", "deploy", "trade", "errorlog", "help"]
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
    websocket_hub_worker_loop()  # v180: legacy WS는 스레드도 만들지 않고 하드 격리
    log(f"background_workers started precision={PRECISION_BACKGROUND_WORKERS} execution_risk={EXEC_RISK_BACKGROUND_WORKERS} websocket=external_sidecar_cache requested={WS_HUB_REQUESTED}")


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
    send_chunks("\n".join(["✅ 봇 시작 완료", f"현재 버전: {BOT_VERSION}", f"전략: {STRATEGY_NAME}", "v201 WS/micro 넓은 대상수집 보강(조건/청산/BY_READY 변경 없음)", "확인: /health /external_status /version_compare /version_score /quality /errorlog"]))
    updater.start_polling(drop_pending_updates=True)
    updater.idle()
    _stop_event.set()


if __name__ == "__main__":
    main()
