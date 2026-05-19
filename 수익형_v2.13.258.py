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
import re
import shutil
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

BOT_VERSION = "수익형 v2.13.258"
# HTTP 헤더는 latin-1만 안전하다. BOT_VERSION은 한글이라 User-Agent로 쓰면
# UnicodeEncodeError가 나며 bulk 스캔이 시작 즉시 0으로 죽는다.
HTTP_USER_AGENT = "coinbot-v2.13.242-mainline"
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
COMMAND_CACHE_SEC = float(os.getenv("CLEAN_COMMAND_CACHE_SEC", "5"))
QUALITY_CACHE_SEC = float(os.getenv("CLEAN_QUALITY_CACHE_SEC", "45"))
VERSION_SCORE_CACHE_SEC = float(os.getenv("CLEAN_VERSION_SCORE_CACHE_SEC", "25"))
RESOURCE_CACHE_SEC = float(os.getenv("CLEAN_RESOURCE_CACHE_SEC", "5"))
WS_CANDIDATE_URGENT_MISSING_RATIO = float(os.getenv("CLEAN_WS_CANDIDATE_URGENT_MISSING_RATIO", "0.35"))

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
    "resource_status": BASE_DIR / "clean_resource_status.json",
    "health_snapshot": BASE_DIR / "clean_health_snapshot.json",
    "external_snapshot": BASE_DIR / "clean_external_snapshot.json",
    "quality_summary": BASE_DIR / "clean_quality_summary.json",
    "version_score_summary": BASE_DIR / "clean_version_score_summary.json",
    "candidate_snapshot": BASE_DIR / "clean_candidate_snapshot.json",
    "candidate_snapshot_pending": BASE_DIR / "clean_candidate_snapshot_pending.json",
    "hot_queue": BASE_DIR / "clean_hot_candidate_queue.json",
    "retention_status": BASE_DIR / "clean_retention_status.json",
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
    "phase_note": "v230: quality 출력 연결/후보문구/외부정보별 성과/target 캐시 병목 정리. 조건/청산/BUY_READY 변경 없음.",
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
    """빠른 JSONL tail reader.

    v206: 기존 read_text().splitlines()는 CLOSED/후보 파일이 커질수록 기본 명령어를 느리게 했다.
    파일 끝쪽만 읽고 부족할 때만 점진적으로 뒤로 넓혀 읽는다.
    """
    if not path.exists():
        return []
    try:
        max_lines = max(1, int(max_lines or 3000))
        size = path.stat().st_size
        # 평균 300~600 bytes/line 정도를 가정하되, 부족하면 아래에서 확장한다.
        block = min(size, max(64 * 1024, max_lines * 700))
        data = b""
        with path.open("rb") as f:
            while True:
                start = max(0, size - block)
                f.seek(start)
                data = f.read(size - start)
                if start <= 0 or data.count(b"\n") >= max_lines + 2:
                    break
                block = min(size, block * 2)
        text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()[-max_lines:]
        out: List[Dict[str, Any]] = []
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



# v2.13.225: micro fresh 단일 판정 본선
V225_MICRO_CACHE_TTL_SEC = float(os.getenv("V225_MICRO_CACHE_TTL_SEC", "0.35"))
_V225_MICRO_SOURCE_CACHE: Dict[str, Any] = {"ts": 0.0, "rows": {}, "status": {}, "targets": set(), "status_age": -1.0}


def _v225_micro_row_ts(row: Dict[str, Any], payload_ts: float = 0.0) -> float:
    if not isinstance(row, dict):
        return 0.0
    for key in ("ts", "updated_ts", "micro_ts", "cache_ts", "last_update_ts", "last_updated_ts"):
        val = fnum(row.get(key), 0.0)
        if val > 0:
            return val
    return fnum(payload_ts, 0.0)


def _v225_read_target_tickers(path: Path) -> List[str]:
    try:
        obj = load_json(path, {})
        raw = obj.get("targets") if isinstance(obj, dict) else obj
        out: List[str] = []
        seen: set[str] = set()
        if isinstance(raw, list):
            for x in raw:
                t = _ticker_from_any(x)
                if t and t not in STABLE_EXCLUDED and t not in seen:
                    seen.add(t)
                    out.append(t)
        return out
    except Exception:
        return []


def _v225_load_micro_source(*, force: bool = False) -> Dict[str, Any]:
    nowv = now_ts()
    if (not force) and fnum(_V225_MICRO_SOURCE_CACHE.get("ts"), 0.0) > 0:
        if nowv - fnum(_V225_MICRO_SOURCE_CACHE.get("ts"), 0.0) <= V225_MICRO_CACHE_TTL_SEC:
            return _V225_MICRO_SOURCE_CACHE
    status = load_json(FILES.get("micro_status", BASE_DIR / "clean_bithumb_micro_status.json"), {})
    payload = load_json(FILES.get("micro_cache", BASE_DIR / "clean_bithumb_micro_cache.json"), {})
    payload_ts = fnum(payload.get("updated_ts"), 0.0) if isinstance(payload, dict) else 0.0
    raw_rows = _normalize_micro_cache_payload(payload)
    rows: Dict[str, Dict[str, Any]] = {}
    fresh = stale = 0
    nowv = now_ts()
    for t, row in raw_rows.items():
        if not t or not isinstance(row, dict):
            continue
        ts = _v225_micro_row_ts(row, payload_ts)
        if ts <= 0:
            continue
        rr = dict(row)
        rr["ticker"] = t
        rr["ts"] = ts
        rr["updated_ts"] = ts
        age = nowv - ts
        rr["_main_age_sec"] = round(age, 3)
        rr["_main_row_status"] = "fresh" if 0 <= age <= MICRO_STALE_SEC else "stale"
        rows[t] = rr
        if 0 <= age <= MICRO_STALE_SEC:
            fresh += 1
        else:
            stale += 1
    targets = _v225_read_target_tickers(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"))
    target_set = set(_ticker_from_any(x) for x in targets if _ticker_from_any(x))
    status_ts = fnum(status.get("updated_ts"), 0.0) if isinstance(status, dict) else 0.0
    status_age = nowv - status_ts if status_ts > 0 else -1.0
    guard_fresh = int(fnum(status.get("fresh"), fresh)) if isinstance(status, dict) else fresh
    _V225_MICRO_SOURCE_CACHE.update({
        "ts": nowv,
        "rows": rows,
        "status": status if isinstance(status, dict) else {},
        "targets": target_set,
        "target_list": targets,
        "payload_ts": payload_ts,
        "status_age": round(status_age, 2) if status_age >= 0 else -1,
        "fresh": fresh,
        "stale": stale,
        "cached": len(rows),
        "guard_fresh": guard_fresh,
    })
    return _V225_MICRO_SOURCE_CACHE


def _v225_micro_snapshot_from_source(ticker: Any, src: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    t = _ticker_from_any(ticker)
    src = src or _v225_load_micro_source()
    rows = src.get("rows") if isinstance(src.get("rows"), dict) else {}
    target_set = src.get("targets") if isinstance(src.get("targets"), set) else set()
    row = dict(rows.get(t) or {})
    if not t:
        return {"micro_fresh": False, "micro_row_status": "missing", "micro_targeted": False, "micro_age_sec": -1}
    if not row:
        return {"micro_fresh": False, "micro_row_status": "missing", "micro_targeted": t in target_set, "micro_age_sec": -1}
    age = fnum(row.get("_main_age_sec"), now_ts() - _v225_micro_row_ts(row, fnum(src.get("payload_ts"), 0.0)))
    fresh = 0 <= age <= MICRO_STALE_SEC
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

def refresh_micro_cache() -> None:
    """v225: 메인봇 micro fresh 기준 단일화.

    sidecar status와 cache는 정상인데 메인봇 snapshot만 fresh=0으로 보이는 문제를 막기 위해
    refresh_micro_cache, micro_snapshot, snapshot overlay가 같은 cache reader를 사용한다.
    """
    try:
        src = _v225_load_micro_source(force=True)
        rows = src.get("rows") if isinstance(src.get("rows"), dict) else {}
        status = src.get("status") if isinstance(src.get("status"), dict) else {}
        targets = list(src.get("target_list") or [])
        with _micro_lock:
            _micro_cache.clear()
            for t, row in rows.items():
                if isinstance(row, dict):
                    _micro_cache[t] = dict(row)
            _micro_targets[:] = targets
        with _state_lock:
            STATE["micro_state"] = str(status.get("state") or ("수집중" if int(src.get("fresh", 0) or 0) > 0 else "대기"))
            STATE["micro_targets"] = int(status.get("targets", len(targets)) if isinstance(status, dict) else len(targets))
            STATE["micro_cached"] = int(src.get("cached", len(rows)) or 0)
            STATE["micro_fresh"] = int(src.get("fresh", 0) or 0)
            STATE["micro_stale"] = int(src.get("stale", 0) or 0)
            STATE["micro_guard_fresh"] = int(src.get("guard_fresh", STATE.get("micro_fresh", 0)) or 0)
            STATE["micro_status_age_sec"] = src.get("status_age", -1)
            STATE["micro_last_error"] = str(status.get("last_error") or "-")[:160]
            STATE["micro_orderbook_ok"] = int(status.get("orderbook_ok", 0) or 0)
            STATE["micro_trade_ok"] = int(status.get("trade_ok", 0) or 0)
            STATE["micro_fresh_source"] = "v225_cache_rows_ts"
            gf = int(src.get("guard_fresh", 0) or 0)
            mf = int(src.get("fresh", 0) or 0)
            STATE["micro_fresh_mismatch"] = bool(gf >= 10 and mf <= 0)
    except Exception as exc:
        log_error("v225_refresh_micro_cache", exc)


def micro_snapshot(ticker: Any) -> Dict[str, Any]:
    return _v225_micro_snapshot_from_source(ticker, _v225_load_micro_source())


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
            "note": "v208: wide target list; sidecar polls priority/open/recent candidates first, then rotates the rest. freshness display uses latest+cache overlay",
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
        xrp = row_for("XRP")
        def avg_change(key: str, arr: List[Dict[str, Any]]) -> float:
            vals = [fnum((r or {}).get(key), fnum((r or {}).get("change_24h"), 0)) for r in (arr or [])]
            vals = [v for v in vals if v == v]
            return round(sum(vals) / len(vals), 4) if vals else 0.0
        ctx = {
            "market_total": total,
            "market_up_ratio": round((up / total) * 100.0, 2) if total else 0.0,
            "market_down_ratio": round((down / total) * 100.0, 2) if total else 0.0,
            "top_money_up_ratio": round((top_up / len(ranked)) * 100.0, 2) if ranked else 0.0,
            "market_avg_1": avg_change("change_1", rows or []),
            "market_avg_3": avg_change("change_3", rows or []),
            "market_avg_5": avg_change("change_5", rows or []),
            "top_money_avg_1": avg_change("change_1", ranked),
            "top_money_avg_3": avg_change("change_3", ranked),
            "top_money_avg_5": avg_change("change_5", ranked),
            "btc_change_1": fnum(btc.get("change_1"), 0),
            "btc_change_3": fnum(btc.get("change_3"), 0),
            "btc_change_5": fnum(btc.get("change_5"), fnum(btc.get("change_24h"), 0)),
            "eth_change_1": fnum(eth.get("change_1"), 0),
            "eth_change_3": fnum(eth.get("change_3"), 0),
            "eth_change_5": fnum(eth.get("change_5"), fnum(eth.get("change_24h"), 0)),
            "xrp_change_1": fnum(xrp.get("change_1"), 0),
            "xrp_change_3": fnum(xrp.get("change_3"), 0),
            "xrp_change_5": fnum(xrp.get("change_5"), fnum(xrp.get("change_24h"), 0)),
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
        "snapshot_id": scan_id,
        "candidate_created_at": ts,
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
            "scan_id": scan_id,
            "snapshot_id": scan_id,
            "candidate_created_at": ts,
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
    """v214 fast factory.

    핵심 수술:
    - scan critical path에서 archive append/compact/동기 실전위험확인/긴 중복검사를 제거한다.
    - paper_bot이 소비하는 *_latest 파일을 먼저 빠르게 저장한다.
    - latest TTL은 저장 직전 now 기준으로 다시 찍어 공장 지연으로 즉시 만료되는 일을 막는다.
    - archive/candidate_events/full context는 기본 scan 본선에 연결하지 않는다.
    """
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    result = {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
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
        "data_quality_note": "v215_latest_first_archive_deferred",
        "dup_skip": 0,
        "write_error": "",
        "last_ticker": "-",
        "factory_mode": "v215_latest_first_no_archive_on_scan",
        "archive_deferred": 0,
        "risk_sync_deferred": True,
    }
    # 동기 실전위험 확인은 공장 병목의 주범이 될 수 있어 scan critical path에서 제거한다.
    # 후보는 background execution_risk_worker가 계속 채운다.
    try:
        enqueue_execution_risk((strict or []) + (shadow or []))
    except Exception as exc:
        log_error("v215_enqueue_execution_risk", exc)

    # 저장 직전 현재 sidecar cache를 1회만 overlay한다. shadow는 복기/관찰용이라 refresh 반복 금지.
    try:
        strict = _overlay_current_external_for_items(list(strict or []), refresh=True)
        shadow = _overlay_current_external_for_items(list(shadow or []), refresh=False)
        mark_ws_target_flags(strict)
        mark_ws_target_flags(shadow)
        with _state_lock:
            STATE["factory_external_overlay"] = "v215_latest_before_export_once"
            STATE["factory_external_overlay_ts"] = now_ts()
    except Exception as exc:
        log_error("v215_factory_external_overlay", exc)

    def _refresh_latest_ttl(row: Dict[str, Any], lane: str) -> Dict[str, Any]:
        # consume_row가 만든 row를 최신 파일용으로 다시 살아있게 만든다.
        nowv = now_ts()
        rr = dict(row or {})
        rr["created_at"] = nowv
        rr["source_created_at"] = nowv
        rr["factory_saved_at"] = nowv
        rr["expires_at"] = nowv + max(30.0, CANDIDATE_TTL_SEC)
        rr["factory_mode"] = "v215_latest_first"
        rr["archive_deferred"] = True
        rr["candidate_ttl_refreshed"] = True
        rr["lane"] = lane
        # v234: paper latest에서 scan_id가 '-'로 떨어지지 않게 factory 저장 직전 보강한다.
        rr["scan_id"] = rr.get("scan_id") or scan_id or str(STATE.get("scan_id") or f"scan-{int(nowv)}")
        rr["snapshot_id"] = rr.get("snapshot_id") or rr.get("scan_id")
        rr["candidate_created_at"] = rr.get("candidate_created_at") or rr.get("created_at") or nowv
        ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
        ctx = dict(ctx)
        ctx["factory_saved_at"] = nowv
        ctx["candidate_ttl_refreshed"] = True
        ctx["factory_mode"] = "v215_latest_first"
        ctx["scan_id"] = rr.get("scan_id")
        ctx["snapshot_id"] = rr.get("snapshot_id")
        ctx["candidate_created_at"] = rr.get("candidate_created_at")
        rr["entry_context"] = ctx
        return rr

    dup_reasons: Counter = Counter()
    for lane, items in (("strict", strict), ("shadow", shadow)):
        latest_rows: List[Dict[str, Any]] = []
        for item in items or []:
            row = consume_row(item, lane, ts, scan_id=scan_id)
            if lane == "shadow":
                row["review_only"] = True
                row["open_eligible"] = False
                row["paper_bot_open"] = False
            else:
                can_open = bool(row.get("trade_ready"))
                row["review_only"] = not can_open
                row["open_eligible"] = can_open
                row["paper_bot_open"] = can_open
            latest_rows.append(_refresh_latest_ttl(row, lane))
        latest_path = FILES["paper_latest"] if lane == "strict" else FILES["shadow_latest"]
        latest_ok, latest_err = write_jsonl_replace(latest_path, latest_rows)
        if not latest_ok:
            result["write_error"] = f"latest:{latest_err}"
        if lane == "strict":
            result["paper_latest_written"] = len(latest_rows)
            result["latest_trade_ready"] = sum(1 for r in latest_rows if r.get("paper_bot_open"))
            result["latest_strict_observe"] = sum(1 for r in latest_rows if not r.get("paper_bot_open"))
            result["latest_final_recheck_wait"] = sum(1 for r in latest_rows if r.get("final_entry_action") == "recheck_wait")
            result["latest_final_observe"] = sum(1 for r in latest_rows if r.get("final_entry_action") == "observe")
            result["paper_written"] = 0
            result["trade_ready_written"] = result["latest_trade_ready"]
            result["strict_observe_written"] = result["latest_strict_observe"]
            result["major_watch_written"] = sum(1 for r in latest_rows if r.get("major_watch"))
            for row in latest_rows[-12:]:
                _recent_strict.append(row)
        else:
            result["shadow_latest_written"] = len(latest_rows)
            result["shadow_written"] = 0
            for row in latest_rows[-12:]:
                _recent_shadow.append(row)
        if latest_rows:
            result["last_ticker"] = str(latest_rows[-1].get("ticker") or "-")
        result["archive_deferred"] += len(latest_rows)
    result["dup_skip_reason"] = dict(dup_reasons)
    with _state_lock:
        STATE["factory_archive_deferred"] = result.get("archive_deferred", 0)
        STATE["factory_mode"] = result.get("factory_mode")
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
        rows = apply_ws_cache_to_rows(rows)
        rows = apply_micro_cache_to_rows(rows)
        stage_times.append(("1) 허브: 전체시장 bulk 수집 + 웹소켓 보조", now_ts() - st, f"rows {len(rows)} / {source} / ws {STATE.get('ws_state','-')} fresh {STATE.get('ws_fresh',0)}"))
        with _state_lock:
            STATE["bulk_rows"] = len(rows)
            STATE["bulk_price"] = sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0)
            STATE["bulk_money"] = sum(1 for r in rows if fnum(r.get("turnover_24h"), 0) > 0)
            STATE["scan_last_stage"] = "worker1_select"

        if _stop_event.is_set():
            with _state_lock:
                STATE["scan_display_note"] = "종료요청으로 스캔 중단"
                STATE["scan_last_stage"] = "stop_requested_before_precision"
            return []

        st = now_ts()
        targets = select_precision_targets(rows)
        stage_times.append(("2) 1차 직원: 표준값/신선도/정밀대상 선정", now_ts() - st, f"bulk {len(rows)} / targets {len(targets)} / {STATE.get('precision_target_note', '-')}"))
        with _state_lock:
            STATE["precision_selected"] = len(targets)
            STATE["scan_last_stage"] = "worker2_precision"

        if _stop_event.is_set():
            with _state_lock:
                STATE["scan_display_note"] = "종료요청으로 스캔 중단"
                STATE["scan_last_stage"] = "stop_requested_before_refresh_precision"
            return []

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

        st = now_ts()
        final_counts = apply_final_entry_worker(strict, rows)
        mark_ws_target_flags(strict)
        mark_ws_target_flags(shadow)
        stage_times.append(("6) 6차 직원: ATR/VWAP/시장장세 최종진입검증", now_ts() - st, f"통과 {final_counts.get('paper_open',0)} / 재확인 {final_counts.get('recheck_wait',0)} / 관찰 {final_counts.get('observe',0)} / 시장 {STATE.get('market_context',{}).get('market_pressure','-')}"))
        with _state_lock:
            STATE["scan_last_stage"] = "factory_export"

        if _stop_event.is_set():
            with _state_lock:
                STATE["scan_display_note"] = "종료요청으로 스캔 중단"
                STATE["scan_last_stage"] = "stop_requested_before_factory"
            return []

        st = now_ts()
        pipe = export_candidates(strict, shadow)
        stage_times.append(("7) 공장: latest 먼저 저장(archive 지연)", now_ts() - st, f"latest strict {pipe.get('paper_latest_written',0)} / paper_OPEN {pipe.get('latest_trade_ready',0)} / 재확인 {pipe.get('latest_final_recheck_wait',0)} / 관찰 {pipe.get('latest_strict_observe',0)} / shadow latest {pipe.get('shadow_latest_written',0)} / archive_deferred {pipe.get('archive_deferred',0)} / mode {pipe.get('factory_mode','-')}"))

        if _stop_event.is_set():
            with _state_lock:
                STATE["scan_display_note"] = "종료요청으로 스캔 중단"
                STATE["scan_last_stage"] = "stop_requested_before_snapshot"
            return []

        st = now_ts()
        snap_note = v212_publish_final_candidate_state(strict, shadow, rows, pipe)
        stage_times.append(("8) 공장: 후보 snapshot 단일 원천 + WS/micro target 최종쓰기", now_ts() - st, snap_note))
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


# v202: 기본 명령어가 같은 CLOSED 파일을 여러 번 읽어 느려지던 문제를 줄인다.
# 파일 mtime/size가 같으면 메모리 캐시를 재사용하고, 바뀐 경우에만 다시 읽는다.
_CLOSED_ROWS_CACHE: Dict[str, Any] = {"mtime": 0.0, "size": -1, "limit": 0, "rows": [], "cached_at": 0.0}
_CLOSED_CACHE_TTL_SEC = 12.0


def _cached_tail_jsonl(path: Path, max_lines: int = 20000) -> List[Dict[str, Any]]:
    try:
        st = path.stat() if path.exists() else None
        mtime = float(st.st_mtime) if st else 0.0
        size = int(st.st_size) if st else 0
        cache = _CLOSED_ROWS_CACHE
        nowv = now_ts()
        same_path = cache.get("path") == str(path)
        enough_limit = int(cache.get("limit", 0)) >= int(max_lines or 0)
        # v206: paper_bot이 CLOSED를 자주 append하면 mtime/size가 계속 바뀌어 기본세트마다
        # CLOSED 전체를 다시 읽었다. 짧은 TTL 안에서는 기존 캐시를 우선 사용하고, 상세 명령에서만 다시 갱신한다.
        if same_path and enough_limit and cache.get("rows") and nowv - fnum(cache.get("cached_at"), 0) <= _CLOSED_CACHE_TTL_SEC:
            rows = list(cache.get("rows") or [])
            return rows[-max_lines:] if max_lines and len(rows) > max_lines else rows
        if same_path and enough_limit and float(cache.get("mtime", 0.0)) == mtime and int(cache.get("size", -1)) == size:
            rows = list(cache.get("rows") or [])
            return rows[-max_lines:] if max_lines and len(rows) > max_lines else rows
        rows = tail_jsonl(path, max_lines=max_lines)
        cache.update({"path": str(path), "mtime": mtime, "size": size, "limit": int(max_lines), "rows": rows, "cached_at": nowv})
        return rows
    except Exception as exc:
        log_error("cached_tail_jsonl", exc)
        return tail_jsonl(path, max_lines=max_lines)


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
    "quick_stop": "빠른손절 종료",
    "bounce_fail": "반등실패 조기손절",
    "slow_early_exit": "지지부진 조기종료",
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
    return _cached_tail_jsonl(FILES["paper_closed"], max_lines=limit)


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
        "- v2.13.208 기준: 6차 최종진입검증 유지 + micro 표시 기준 통일",
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
        "- 메인봇 대상: 수익형_v2.13.219.py",
        "- 페이퍼봇 대상: paper_bot_v0.40.py",
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




def _paper_status_light() -> Dict[str, Any]:
    status = load_json(FILES.get("paper_status", BASE_DIR / "paper_bot_status.json"), {})
    control = load_json(FILES.get("paper_control", BASE_DIR / "paper_bot_control.json"), {})
    if not isinstance(status, dict): status = {}
    if not isinstance(control, dict): control = {}
    age = now_ts() - fnum(status.get("updated_at"), 0) if fnum(status.get("updated_at"), 0) > 0 else -1
    icon = "✅" if 0 <= age <= 90 else ("⚠️" if age >= 0 else "❔")
    return {"status": status, "control": control, "age": age, "icon": icon, "running": bool(control.get("running", status.get("running")))}


def _fmt_bytes(n: Any) -> str:
    try:
        n = float(n)
    except Exception:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f}{units[i]}" if i >= 2 else f"{n:.0f}{units[i]}"


def _read_meminfo() -> Dict[str, float]:
    out: Dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            parts = v.strip().split()
            if parts:
                out[k] = float(parts[0]) * 1024.0
    except Exception:
        pass
    return out


def _self_rss_bytes() -> float:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) * 1024.0
    except Exception:
        pass
    return 0.0


def server_resource_snapshot() -> Dict[str, Any]:
    try:
        du = shutil.disk_usage(str(BASE_DIR))
        disk_total, disk_used, disk_free = float(du.total), float(du.used), float(du.free)
        disk_pct = (disk_used / disk_total * 100.0) if disk_total > 0 else 0.0
    except Exception:
        disk_total = disk_used = disk_free = disk_pct = 0.0
    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0.0)
    mem_avail = mem.get("MemAvailable", 0.0)
    mem_used = max(0.0, mem_total - mem_avail) if mem_total else 0.0
    mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    return {
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "disk_pct": disk_pct,
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_avail": mem_avail,
        "mem_pct": mem_pct,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "rss": _self_rss_bytes(),
    }


def resource_health_lines() -> List[str]:
    r = server_resource_snapshot()
    disk_icon = "✅" if r["disk_pct"] < 85 else ("⚠️" if r["disk_pct"] < 95 else "❌")
    mem_icon = "✅" if r["mem_pct"] < 80 else ("⚠️" if r["mem_pct"] < 92 else "❌")
    load_icon = "✅" if float(r.get("load1", 0.0)) < 1.5 else ("⚠️" if float(r.get("load1", 0.0)) < 3.0 else "❌")
    return [
        f"{disk_icon} 디스크: 사용 {r['disk_pct']:.1f}% / 남음 {_fmt_bytes(r['disk_free'])} / 전체 {_fmt_bytes(r['disk_total'])}",
        f"{mem_icon} 메모리: 사용 {r['mem_pct']:.1f}% / 남음 {_fmt_bytes(r['mem_avail'])} / 전체 {_fmt_bytes(r['mem_total'])}",
        f"{load_icon} CPU/load: {r['load1']:.2f} / {r['load5']:.2f} / {r['load15']:.2f} / main RSS {_fmt_bytes(r['rss'])}",
    ]

def _latest_strict_overlay(limit: int = 240) -> List[Dict[str, Any]]:
    # v204: 기본 상태판은 latest 파일 전체를 무겁게 읽지 않고 최근 후보 묶음만 본다.
    return _overlay_current_external_for_items(tail_jsonl(FILES["paper_latest"], max_lines=limit), refresh=True)

def _external_health_snapshot() -> Dict[str, Any]:
    latest_strict = _latest_strict_overlay(240)
    ws_fresh = ws_stale = ws_missing = 0
    micro_fresh = micro_stale = micro_missing = 0
    for r in latest_strict:
        ws = _ws_status_of(r)
        if ws == "fresh": ws_fresh += 1
        elif ws == "stale": ws_stale += 1
        else: ws_missing += 1
        ms = _micro_status_of(r)
        if ms == "fresh": micro_fresh += 1
        elif ms == "stale": micro_stale += 1
        else: micro_missing += 1
    ws_age = fnum(STATE.get("ws_last_age_sec"), -1)
    ws_state = str(STATE.get("ws_state") or "-")
    micro_state = str(STATE.get("micro_state") or "-")
    ws_worker_ok = ws_state == "외부수신" and 0 <= ws_age <= 20 and int(STATE.get("ws_fresh", 0) or 0) > 0
    micro_worker_ok = micro_state in {"수집중", "정상수집"} and int(STATE.get("micro_fresh", STATE.get("micro_fresh_rows", 0)) or 0) > 0
    return {"latest_strict": latest_strict, "total": len(latest_strict), "ws_fresh": ws_fresh, "ws_stale": ws_stale, "ws_missing": ws_missing, "micro_fresh": micro_fresh, "micro_stale": micro_stale, "micro_missing": micro_missing, "ws_worker_ok": ws_worker_ok, "micro_worker_ok": micro_worker_ok, "candidate_ok": bool(latest_strict) and ws_fresh > 0 and micro_fresh > 0, "ws_age": ws_age, "ws_state": ws_state, "micro_state": micro_state}

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
    snap = _external_health_snapshot()
    scan = scan_status_summary()
    p = _paper_status_light()
    p_status = p.get("status", {}) if isinstance(p.get("status"), dict) else {}
    err_summary = "✅ 새 실행 중 오류 없음" if not _recent_errors else "❌ 새 실행 중 오류 있음"
    ws_icon = "✅" if snap.get("ws_worker_ok") else "⚠️"
    micro_icon = "✅" if snap.get("micro_worker_ok") else "⚠️"
    warnings = external_health_warning_lines(snap)
    if not warnings: warnings = ["✅ 외부정보 신선도 확인됨", "✅ 이제부터 후보품질 관찰 가능"]
    else: warnings.append("- 재시작/상세로그/업그레이드는 가드봇에서 확인")
    return "\n".join([
        "🧭 건강상태 /health",
        "- 메인봇방용 간단 상태판입니다. 재시작/업그레이드는 가드봇 담당.",
        "",
        "[1/5] 메인봇",
        f"{scan.get('icon','❔')} {BOT_VERSION} / scan {scan.get('label','-')} / 단계 {scan.get('stage','-')} / 후보 {snap.get('total',0)}개",
        f"- 오류: {err_summary}",
        "",
        "[2/5] 서버자원",
        *resource_health_lines(),
        "",
        "[3/5] 외부정보",
        f"{ws_icon} WS: {snap.get('ws_state','-')} / 전체신선 {STATE.get('ws_fresh',0)} / 후보신선 {snap.get('ws_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)} / 최근 {snap.get('ws_age','-')}초",
        f"{micro_icon} 호가·체결: {snap.get('micro_state','-')} / 전체신선 {STATE.get('micro_fresh', STATE.get('micro_fresh_rows',0))} / 후보신선 {snap.get('micro_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        "",
        "[4/5] paper_bot",
        f"{p.get('icon','❔')} {p_status.get('version','?')} / running {p.get('running')} / OPEN {p_status.get('open_total','?')} / CLOSED {p_status.get('closed_total','?')} / 상태 {_age_text(fnum(p_status.get('updated_at'),0))} 전",
        "",
        "[5/5] 판독",
        *warnings
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
    latest_strict = _overlay_current_external_for_items(tail_jsonl(FILES["paper_latest"], max_lines=1000))
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
        ("deploy", lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.219.py", "- 페이퍼봇 대상: paper_bot_v0.40.py"])),
    ]
    start = now_ts()
    reply(update, "\n".join(["📦 묶음 명령 접수", "- 출처: /batch", f"- 실행 {len(funcs)}개", "- v2.13.208: micro 최신값 표시 기준 통일 + 구버전 배포문구 정리(조건/청산/BUY_READY 변경 없음)", "- 기존 기록 삭제 없음 / 후보판단 고정절단 없음 / paper_bot은 최종검증 통과 후보만 OPEN"]))
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
    lines = ["🧾 v2.13.208 batch 요약", f"- 출력무결성: 실행 {len(funcs)}개", "", "⏱ 명령어별 시간표"]
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
            "deploy": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.219.py", "- 페이퍼봇 대상: paper_bot_v0.40.py"]),
            "upgradestatus": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.219.py", "- 페이퍼봇 대상: paper_bot_v0.40.py"]),
            "paper_today": lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", "", new_period_text(), candidate_fresh_text()]),
            "score": score_text,
            "version_score": version_score_text,
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




def _apply_relative_strength_context(item: Dict[str, Any]) -> Dict[str, Any]:
    """v204: 시장장세를 차단조건이 아니라 상대강도 기록값으로만 붙인다.

    목적은 장이 안 좋아도 시장보다 강한 코인을 찾기 위한 참고값이다.
    이 함수는 paper_open/observe/recheck를 바꾸지 않는다.
    """
    try:
        if not isinstance(item, dict):
            return item
        market = STATE.get("market_context") if isinstance(STATE.get("market_context"), dict) else {}
        c1 = fnum(item.get("change_1"), 0.0)
        c3 = fnum(item.get("change_3"), 0.0)
        c5 = fnum(item.get("change_5"), fnum(item.get("change_24h"), 0.0))
        m1 = fnum(market.get("market_avg_1"), 0.0)
        m3 = fnum(market.get("market_avg_3"), 0.0)
        m5 = fnum(market.get("market_avg_5"), 0.0)
        t3 = fnum(market.get("top_money_avg_3"), m3)
        t5 = fnum(market.get("top_money_avg_5"), m5)
        rs1 = round(c1 - m1, 4)
        rs3 = round(c3 - m3, 4)
        rs5 = round(c5 - m5, 4)
        top_rs3 = round(c3 - t3, 4)
        top_rs5 = round(c5 - t5, 4)
        # 시장이 약한데도 후보가 시장/상위거래대금 평균보다 버티면 독립강세로 본다.
        pressure = str(market.get("market_pressure") or "확인중")
        up_ratio = fnum(market.get("market_up_ratio"), 0.0)
        score = round(rs3 * 0.45 + rs5 * 0.45 + top_rs3 * 0.10, 4)
        if score >= 0.70 and (pressure == "주의" or up_ratio < FINAL_MARKET_UP_WEAK):
            label = "장약해도강함"
        elif score >= 0.35:
            label = "시장대비강함"
        elif score <= -0.35:
            label = "시장대비약함"
        else:
            label = "시장비슷"
        ctx = {
            "market_pressure": pressure,
            "market_up_ratio": round(up_ratio, 2),
            "market_avg_3": m3,
            "market_avg_5": m5,
            "top_money_avg_3": t3,
            "top_money_avg_5": t5,
            "relative_strength_1m": rs1,
            "relative_strength_3m": rs3,
            "relative_strength_5m": rs5,
            "relative_strength_vs_top_3m": top_rs3,
            "relative_strength_vs_top_5m": top_rs5,
            "relative_strength_score": score,
            "relative_strength_label": label,
            "btc_change_3": fnum(market.get("btc_change_3"), 0.0),
            "eth_change_3": fnum(market.get("eth_change_3"), 0.0),
            "xrp_change_3": fnum(market.get("xrp_change_3"), 0.0),
        }
        item.update(ctx)
        ectx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
        ectx = dict(ectx)
        ectx.update(ctx)
        ectx["relative_strength_note"] = "v204_record_only_not_filter"
        item["entry_context"] = ectx
    except Exception as exc:
        log_error("relative_strength_context", exc)
    return item


def _relative_strength_line(items: List[Dict[str, Any]]) -> str:
    if not items:
        return "- 상대강도: 후보 없음"
    strong = weak = neutral = 0
    vals: List[float] = []
    labels = Counter()
    for r in items or []:
        label = str(_ctxv(r, "relative_strength_label", r.get("relative_strength_label") or ""))
        score = fnum(_ctxv(r, "relative_strength_score", r.get("relative_strength_score")), 0.0)
        vals.append(score)
        labels[label or "확인중"] += 1
        if label in {"장약해도강함", "시장대비강함"}:
            strong += 1
        elif label == "시장대비약함":
            weak += 1
        else:
            neutral += 1
    avg = sum(vals) / len(vals) if vals else 0.0
    top = " / ".join(f"{k} {v}" for k, v in labels.most_common(3)) if labels else "-"
    m = STATE.get("market_context") if isinstance(STATE.get("market_context"), dict) else {}
    return f"- 상대강도: 강함 {strong} / 보통 {neutral} / 약함 {weak} / 평균 {avg:+.2f} / 시장 {m.get('market_pressure','-')}·상승 {m.get('market_up_ratio','-')}% / {top}"

def _overlay_current_external_for_items(items: List[Dict[str, Any]], *, refresh: bool = True) -> List[Dict[str, Any]]:
    """v202: 현재 후보 상태판은 후보파일에 저장된 오래된 WS/micro snapshot만 믿지 않는다.

    sidecar 캐시가 더 최신이면 표시 직전에 overlay해서 /health, /external_status, /quality가
    실제 현재 수집 상태를 보여주게 한다. 진입 당시 CLOSED 분석은 기존 entry_context를 유지한다.
    """
    if refresh:
        try:
            refresh_external_ws_cache()
        except Exception as exc:
            log_error("overlay_refresh_ws", exc)
        try:
            refresh_micro_cache()
        except Exception as exc:
            log_error("overlay_refresh_micro", exc)
    out: List[Dict[str, Any]] = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        rr = dict(r)
        t = rr.get("ticker") or rr.get("market") or rr.get("symbol")
        overlay_keys: Dict[str, Any] = {}
        try:
            ws = ws_snapshot(t)
            # 현재 상태판/공장 저장은 최신 캐시 기준을 우선한다. cache missing이면 targeted 표시 정도만 반영한다.
            rr.update(ws)
            overlay_keys.update(ws)
            if fnum(ws.get("live_price"), 0) > 0:
                rest_price = fnum(rr.get("current_price") or rr.get("entry_price") or rr.get("detected_price"), 0)
                live = fnum(ws.get("live_price"), 0)
                rr["ws_price"] = live
                rr["current_price_ws_gap_pct"] = round(((live - rest_price) / rest_price) * 100.0, 3) if rest_price > 0 else 0.0
                overlay_keys["ws_price"] = live
                overlay_keys["current_price_ws_gap_pct"] = rr["current_price_ws_gap_pct"]
            rr["ws_age_sec"] = fnum(ws.get("live_age_sec"), -1)
            overlay_keys["ws_age_sec"] = rr["ws_age_sec"]
        except Exception as exc:
            log_error("overlay_ws_item", exc)
        try:
            ms = micro_snapshot(t)
            rr.update(ms)
            overlay_keys.update(ms)
        except Exception as exc:
            log_error("overlay_micro_item", exc)
        # v203: 후보 row에 이미 entry_context가 있는 경우도 저장 직전 최신 snapshot과 맞춘다.
        ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
        if isinstance(ctx, dict):
            ctx = dict(ctx)
            for k, v in overlay_keys.items():
                ctx[k] = v
            ctx["external_overlay_at"] = now_ts()
            ctx["external_overlay_note"] = "v204_latest_cache_before_display_or_export"
            rr["entry_context"] = ctx
        rr = _apply_relative_strength_context(rr)
        out.append(rr)
    return out


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
    """메인봇 관점의 외부정보 통합상태.
    v205: /health와 같은 _external_health_snapshot() 단일 경로를 사용해 micro fresh 판정이
    명령어마다 다르게 보이는 문제를 줄인다.
    """
    snap = _external_health_snapshot()
    total = int(snap.get("total", 0) or 0)
    ws_ok = bool(snap.get("ws_worker_ok"))
    micro_ok = bool(snap.get("micro_worker_ok"))
    lines = [
        "🛰 외부정보 상태 /external_status",
        "",
        "[1/3] 직원 상태",
        f"{'✅' if ws_ok else '⚠️'} 웹소켓: {STATE.get('ws_state','-')} / 대상 {STATE.get('ws_targets',0)} / 신선 {STATE.get('ws_fresh',0)} / 최근 {STATE.get('ws_last_age_sec','-')}초 / 갱신 {STATE.get('ws_target_write_note','-')}",
        f"{'✅' if micro_ok else '⚠️'} 호가·체결: 대상 {STATE.get('micro_target_file_targets',0)} / 신선 {STATE.get('micro_fresh', STATE.get('micro_fresh_rows',0))} / 상태 {STATE.get('micro_state','캐시확인')}",
        "",
        "[2/3] 현재 후보 반영",
        f"- 정식 후보 {total}개",
        f"- 웹소켓: 신선 {snap.get('ws_fresh',0)} / 대상 {total} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {snap.get('micro_fresh',0)} / 대상 {total} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        "- 기준: latest 후보 + 현재 clean_bithumb_micro_cache overlay",
        "",
        "[3/3] 판독",
    ]
    if total <= 0:
        lines.append("❔ 현재 정식 후보 없음")
    else:
        lines.append("✅ 웹소켓 후보 반영 중" if int(snap.get('ws_fresh',0) or 0) > 0 else "⚠️ 후보에 웹소켓 신선값이 부족함")
        lines.append("✅ 호가·체결 후보 반영 중" if int(snap.get('micro_fresh',0) or 0) > 0 else "⚠️ 호가·체결 신선값이 부족함")
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
        rs_label = str(_ctxv(r, "relative_strength_label", r.get("relative_strength_label") or ""))
        rs_txt = f" / 상대 {rs_label}" if rs_label else ""
        out.append(f"- {t}: 점수 {score:.2f} / 3분돈 {m3/10000:,.0f}만 / 눌림 {pb:.2f}{rs_txt} / {label}")
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
        status = _micro_status_of(r)
        targeted += 1 if (bool(_ctxv(r, "micro_targeted")) or r.get("micro_targeted") is True) else 0
        if status == "fresh":
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
    rows = load_closed(limit=3500)
    new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
    new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    recent3 = rows_recent_hours(new_strict, 3)
    recent12 = rows_recent_hours(new_strict, 12)
    v_strict = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
    latest_strict = _latest_strict_overlay(240)
    latest_shadow = tail_jsonl(FILES["shadow_latest"], max_lines=240)
    open_rows = [r for r in latest_strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    recheck_rows = [r for r in latest_strict if r.get("final_entry_action") == "recheck_wait"]
    observe_rows = [r for r in latest_strict if r not in open_rows and r not in recheck_rows]
    reasons = Counter(str(r.get("exit_reason") or "unknown") for r in recent12)
    rc = Counter(); pass_after_check = 0
    for r in observe_rows:
        label = str(_ctxv(r, "final_entry_label", r.get("final_entry_label") or "") or "")
        action = str(_ctxv(r, "final_entry_action", r.get("final_entry_action") or "") or "")
        if "펌핑" in label or "slow" in label: rc["펌핑 의심"] += 1
        elif "밀림" in label or "stop" in label: rc["밀림 위험"] += 1
        elif "스프레드" in label: rc["스프레드 주의"] += 1
        elif "통과" in label or action in {"paper_open", "open"}: pass_after_check += 1
        else: rc[(label or "외부정보/실전위험 확인중")[:40]] += 1
    parts = [f"{k} {v}" for k, v in rc.most_common(4)]
    if pass_after_check: parts.append(f"외부정보/실전위험 확인중 {pass_after_check}")
    lines = ["🔬 후보품질 요약 /quality", "- 기본은 가볍게: 현재후보 + 현재버전 + 최근 3시간/12시간 핵심만 표시", "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full", "", "[1/5] 성과 요약", _compact_stat_line("최근 3시간 정식", recent3), _compact_stat_line("최근 12시간 정식", recent12), _compact_stat_line("현재버전 정식", v_strict), f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}", "", "[2/5] 현재 후보", f"- 정식 {len(latest_strict)}개 / 🧪 모의진입 {len(open_rows)}개 / ⚠️ 조금 더 보기 {len(recheck_rows)}개 / ❌ 진입보류 {len(observe_rows)}개 / 복기 {len(latest_shadow)}개", f"- 최종검증: {STATE.get('final_entry_note','-')} / 시장 {STATE.get('market_context',{}).get('market_pressure','-')} / 상승비율 {STATE.get('market_context',{}).get('market_up_ratio','-')}%", _simple_ws_line(latest_strict), _simple_micro_line(latest_strict), _relative_strength_line(latest_strict), "- 진입 보류 이유: " + (" / ".join(parts[:5]) if parts else "자료없음"), "", "[3/5] 후보 예시", "🧪 모의진입", *_candidate_brief_lines(open_rows, 2), "⚠️ 조금 더 보기", *_candidate_brief_lines(recheck_rows, 2), "❌ 진입보류", *_candidate_brief_lines(observe_rows, 2), "", "[4/5] 최근 12시간 종료 사유"]
    if reasons:
        for k, _ in reasons.most_common(4):
            sub = [r for r in recent12 if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_compact_stat_line(k, sub))
    else: lines.append("- 최근 CLOSED 부족")
    lines += ["", "[5/5] 판독", "❌ 전체 누적은 아직 자동매매 불가" if score_stats(new_strict).get("avg", 0) < 0 else "✅ 전체 누적 평균 +권", "⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결"]
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
    """v225: rows already overlaid by the single micro snapshot reader."""
    status = str(_ctxv(row, "micro_row_status", "missing") or "missing")
    age = fnum(_ctxv(row, "micro_age_sec", -1), -1)
    if status == "fresh" or bool(_ctxv(row, "micro_fresh")) or (0 <= age <= MICRO_STALE_SEC):
        return "fresh"
    if status == "stale" or age >= 0:
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



# v205: /version_compare는 기본세트에 남기되, CLOSED 원자료를 매번 무겁게 다시 훑지 않도록
# mtime/size 기반 메모리 캐시와 단일 요약 경로만 사용한다.
_VERSION_COMPARE_SUMMARY_CACHE: Dict[str, Any] = {"mtime": 0.0, "size": -1, "lines": []}


def _fast_ctx(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    try:
        if key in row and row.get(key) not in (None, ""):
            return row.get(key)
        ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
        if key in ctx and ctx.get(key) not in (None, ""):
            return ctx.get(key)
    except Exception:
        pass
    return default


def _fast_micro_status(row: Dict[str, Any]) -> str:
    status = str(_fast_ctx(row, "micro_row_status", "missing") or "missing")
    if bool(_fast_ctx(row, "micro_fresh", False)) or status == "fresh":
        return "fresh"
    if status == "stale":
        return "stale"
    return "missing"


def _fast_ws_status(row: Dict[str, Any]) -> str:
    status = str(_fast_ctx(row, "ws_row_status", "") or "")
    source = str(_fast_ctx(row, "live_price_source", "") or "")
    age = fnum(_fast_ctx(row, "ws_age_sec", _fast_ctx(row, "live_age_sec", -1)), -1)
    if bool(_fast_ctx(row, "ws_fresh", False)) or status == "fresh" or source == "WS_SIDECAR" or (0 <= age <= WS_HUB_STALE_SEC):
        return "fresh"
    if status == "stale" or source == "WS_STALE" or age >= 0:
        return "stale"
    return "missing"


def _fast_version_label(row: Dict[str, Any]) -> str:
    return str(row.get("opened_brain_version") or row.get("brain_version") or row.get("bot_version") or "버전 미기록")


def _build_version_compare_lines_fast(limit_rows: int = 9000) -> List[str]:
    rows = [r for r in load_closed(limit=limit_rows) if str(r.get("lane")) == "strict"]
    # 버전별 배열을 만들지 않고 필요한 요약값만 누적한다.
    stats: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        ver = _fast_version_label(r)
        d = stats.setdefault(ver, {
            "n": 0, "wins": 0, "total": 0.0, "top": None, "top_pnl": -9999.0,
            "stop": 0, "ws": Counter(), "micro": Counter(), "reasons": Counter(), "latest": 0.0,
        })
        pnl = fnum(r.get("pnl_pct"), 0.0)
        d["n"] += 1
        d["wins"] += 1 if pnl > 0 else 0
        d["total"] += pnl
        if pnl > d["top_pnl"]:
            d["top_pnl"] = pnl
            d["top"] = r
        reason = str(r.get("exit_reason") or "unknown")
        d["reasons"][label_kr(reason)] += 1
        if reason == "stop_loss":
            d["stop"] += 1
            d["ws"][_fast_ws_status(r)] += 1
            d["micro"][_fast_micro_status(r)] += 1
        d["latest"] = max(float(d.get("latest", 0.0) or 0.0), row_ts(r, "closed_at", "opened_at", "source_created_at"))

    preferred = [BOT_VERSION, "수익형 v2.13.204", "수익형 v2.13.203", "수익형 v2.13.202", "수익형 v2.13.201", "수익형 v2.13.200", "수익형 v2.13.198", "수익형 v2.13.197"]
    keys: List[str] = []
    for k in preferred:
        if k in stats and k not in keys:
            keys.append(k)
    for k, _ in sorted(stats.items(), key=lambda kv: (float(kv[1].get("latest", 0.0) or 0.0), kv[0]), reverse=True):
        if k not in keys and k != "버전 미기록" and len(keys) < 6:
            keys.append(k)

    lines = ["📊 버전 비교 /version_compare", "- 경량화 요약. 상세 손절은 /loss_review <버전>", ""]
    for k in keys[:6]:
        d = stats.get(k) or {}
        n = int(d.get("n", 0) or 0)
        wins = int(d.get("wins", 0) or 0)
        total = float(d.get("total", 0.0) or 0.0)
        avg = total / n if n else 0.0
        st = {"n": n, "wins": wins, "losses": n-wins, "win_rate": wins/n*100 if n else 0.0, "total": total, "avg": avg}
        top_pnl = float(d.get("top_pnl", 0.0) or 0.0) if n else 0.0
        excl_total = total - top_pnl if n else 0.0
        excl_avg = excl_total / max(1, n-1) if n > 1 else 0.0
        excl_icon = "⚠️" if total > 0 and excl_total < 0 else ("✅" if avg > 0 else "❌")
        ws = d.get("ws") if isinstance(d.get("ws"), Counter) else Counter()
        micro = d.get("micro") if isinstance(d.get("micro"), Counter) else Counter()
        reasons = d.get("reasons") if isinstance(d.get("reasons"), Counter) else Counter()
        reason_txt = " / ".join(f"{rk} {rv}" for rk, rv in reasons.most_common(3)) if reasons else "종료 없음"
        lines.append(f"{score_icon(st)} {k}: {n}전 {wins}승 {n-wins}패 / 합산 {total:+.2f}% / 평균 {avg:+.2f}%{sample_note(n)}")
        lines.append(f"{excl_icon} 최대수익 제외: 합산 {excl_total:+.2f}% / 평균 {excl_avg:+.2f}%")
        lines.append(f"- 손절 {int(d.get('stop',0) or 0)}건 / WS {ws.get('fresh',0)}/{ws.get('stale',0)}/{ws.get('missing',0)} / micro {micro.get('fresh',0)}/{micro.get('stale',0)}/{micro.get('missing',0)}")
        lines.append(f"- 종료: {reason_txt}")
        lines.append("")
    lines += ["판독", "- 50전 미만은 판단보류", "- 최대수익 제외가 마이너스면 한 방 착시", "- 자세한 원인은 /loss_review <버전>"]
    return lines


def version_compare_text() -> str:
    try:
        path = FILES["paper_closed"]
        st = path.stat() if path.exists() else None
        mtime = float(st.st_mtime) if st else 0.0
        size = int(st.st_size) if st else 0
        cache = _VERSION_COMPARE_SUMMARY_CACHE
        if cache.get("mtime") == mtime and cache.get("size") == size and cache.get("lines"):
            lines = list(cache.get("lines") or [])
        else:
            lines = _build_version_compare_lines_fast()
            cache.update({"mtime": mtime, "size": size, "lines": lines, "cached_at": now_ts()})
        return "\n".join(lines).strip()
    except Exception as exc:
        log_error("version_compare_fast", exc)
        # 비상 fallback도 compact만 유지한다.
        rows = _strict_closed_rows()
        return "📊 버전 비교 /version_compare\n- 요약 생성 실패, 최근 성과만 표시\n\n" + "\n".join(_short_version_history_lines(rows, limit=6))


_CURRENT_VERSION_SCORE_CACHE: Dict[str, Any] = {"mtime": 0.0, "size": -1, "lines": "", "cached_at": 0.0}


def _current_version_rows_fast(limit: int = 2500) -> List[Dict[str, Any]]:
    return [r for r in rows_since_current_version(load_closed(limit=limit)) if str(r.get("lane")) == "strict"]


def version_score_text() -> str:
    try:
        path = FILES["paper_closed"]
        st = path.stat() if path.exists() else None
        mtime = float(st.st_mtime) if st else 0.0
        size = int(st.st_size) if st else 0
        cache = _CURRENT_VERSION_SCORE_CACHE
        nowv = now_ts()
        if cache.get("mtime") == mtime and cache.get("size") == size and cache.get("lines"):
            return str(cache.get("lines"))
        if cache.get("lines") and nowv - fnum(cache.get("cached_at"), 0) <= 10.0:
            return str(cache.get("lines"))
        rows = _current_version_rows_fast(limit=2500)
        text = "\n".join([
            "📊 현재버전 성과 /version_score",
            _compact_stat_line("현재버전 정식 모의매매", rows),
            f"- 기준: {BOT_VERSION} / {version_baseline_text()} / 기존 기록 삭제 없음",
            f"- 주요종료: {_reason_summary(rows)}",
            "- 참고: 상세 손절은 /loss_review 206",
        ])
        cache.update({"mtime": mtime, "size": size, "lines": text, "cached_at": nowv})
        return text
    except Exception as exc:
        log_error("version_score_fast", exc)
        rows = _current_version_rows_fast(limit=1500)
        return "\n".join(["📊 현재버전 성과 /version_score", _compact_stat_line("현재버전 정식 모의매매", rows), f"- 기준: {BOT_VERSION} / {version_baseline_text()} / 기존 기록 삭제 없음"])

def command_version_score(update, context) -> None:
    reply(update, version_score_text())


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
        "reversion_review": command_reversion_review,
        "avg_review": command_reversion_review,
        "reversion_review_full": command_reversion_review_full,
        "avg_review_full": command_reversion_review_full,
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
        menu = ["batch", "health", "check", "core", "external_status", "quality", "score", "version_score", "version_compare", "loss_review", "reversion_review", "cpu_status", "paper_handoff", "deploy", "trade", "errorlog", "help"]
        updater.bot.set_my_commands([BotCommand(k, k) for k in menu])
    except Exception:
        pass




# ===============================
# v2.13.209: 캐시 전용 명령어 본선 + 자원/품질 요약 직원
# - 기본 명령어는 무거운 장부/후보/외부캐시 재계산을 하지 않고 요약 캐시만 읽는다.
# - 직접 계산 경로는 백그라운드 요약 직원과 *_full 상세 명령에만 남긴다.
# - 전략 조건/청산/BUY_READY는 변경하지 않는다.
# ===============================
_v209_direct_health_text = health_text
_v209_direct_external_status_text = external_status_text
_v209_direct_candidate_quality_text = candidate_quality_text
_v209_direct_version_score_text = version_score_text
_v209_original_update_ws_targets = update_ws_targets

_CPU_SAMPLE: Dict[str, Any] = {"ts": 0.0, "system": None, "proc": {}}
_COMMAND_CACHE_STATE: Dict[str, Any] = {"last_light": 0.0, "last_quality": 0.0, "last_version": 0.0}

def _read_proc_stat_total() -> Tuple[float, float]:
    """return (total_jiffies, idle_jiffies)."""
    try:
        parts = Path("/proc/stat").read_text(encoding="utf-8", errors="ignore").splitlines()[0].split()[1:]
        vals = [float(x) for x in parts[:10]]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)
        return total, idle
    except Exception:
        return 0.0, 0.0


def _read_pid_cpu_jiffies(pid: Any) -> float:
    try:
        pid = int(pid)
        if pid <= 0:
            return 0.0
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="ignore")
        # comm 필드에 공백/괄호가 있을 수 있어 마지막 ')' 뒤부터 파싱한다.
        rest = raw.rsplit(")", 1)[1].strip().split()
        # rest[11]=utime, rest[12]=stime because fields start from state(3)
        return float(rest[11]) + float(rest[12])
    except Exception:
        return 0.0


def _pid_file_int(name: str) -> int:
    try:
        p = BASE_DIR / name
        if not p.exists():
            return 0
        return int(str(p.read_text(encoding="utf-8", errors="ignore").strip() or "0"))
    except Exception:
        return 0


def _resource_pid_map() -> Dict[str, int]:
    return {
        "main": os.getpid(),
        "paper": _pid_file_int("paper_bot.pid"),
        "ws": _pid_file_int("clean_ws_sidecar.pid"),
        "micro": _pid_file_int("clean_bithumb_micro.pid"),
    }


def _proc_rss_bytes_for_pid(pid: Any) -> float:
    try:
        pid = int(pid)
        if pid <= 0:
            return 0.0
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) * 1024.0
    except Exception:
        pass
    return 0.0


def server_resource_snapshot() -> Dict[str, Any]:  # type: ignore[override]
    """v209 자원 직원용 직접 측정 함수. 명령어는 이 함수가 아니라 clean_resource_status.json을 읽는다."""
    try:
        du = shutil.disk_usage(str(BASE_DIR))
        disk_total, disk_used, disk_free = float(du.total), float(du.used), float(du.free)
        disk_pct = (disk_used / disk_total * 100.0) if disk_total > 0 else 0.0
    except Exception:
        disk_total = disk_used = disk_free = disk_pct = 0.0
    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0.0)
    mem_avail = mem.get("MemAvailable", 0.0)
    mem_used = max(0.0, mem_total - mem_avail) if mem_total else 0.0
    mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0

    nowv = now_ts()
    total, idle = _read_proc_stat_total()
    pids = _resource_pid_map()
    proc_now = {k: _read_pid_cpu_jiffies(v) for k, v in pids.items()}
    rss = {k: _proc_rss_bytes_for_pid(v) for k, v in pids.items()}
    cpu_total_pct = 0.0
    proc_pct = {k: 0.0 for k in pids}
    try:
        prev_total, prev_idle = _CPU_SAMPLE.get("system") or (0.0, 0.0)
        dt = total - float(prev_total or 0.0)
        didle = idle - float(prev_idle or 0.0)
        if dt > 0:
            cpu_total_pct = max(0.0, min(100.0, (1.0 - (didle / dt)) * 100.0))
            ncpu = max(1, os.cpu_count() or 1)
            prev_proc = _CPU_SAMPLE.get("proc") if isinstance(_CPU_SAMPLE.get("proc"), dict) else {}
            for k, cur in proc_now.items():
                old = float(prev_proc.get(k, cur) or cur)
                # ps/top식으로 한 코어 100 기준. 멀티스레드면 100 초과 가능.
                proc_pct[k] = max(0.0, ((float(cur) - old) / dt) * ncpu * 100.0)
    except Exception:
        pass
    _CPU_SAMPLE.update({"ts": nowv, "system": (total, idle), "proc": proc_now})
    return {
        "version": BOT_VERSION,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "disk_pct": disk_pct,
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_avail": mem_avail,
        "mem_pct": mem_pct,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cpu_total_pct": cpu_total_pct,
        "cpu_proc_pct": proc_pct,
        "pids": pids,
        "rss": rss,
        "note": "v209 resource monitor cache; commands read cache only",
    }


def _write_resource_status() -> Dict[str, Any]:
    r = server_resource_snapshot()
    save_json(FILES["resource_status"], r)
    return r


def _read_resource_status() -> Dict[str, Any]:
    obj = load_json(FILES.get("resource_status", BASE_DIR / "clean_resource_status.json"), {})
    return obj if isinstance(obj, dict) else {}


def resource_health_lines() -> List[str]:  # type: ignore[override]
    """v209: 명령어용 자원 표시는 측정하지 않고 캐시만 읽는다."""
    r = _read_resource_status()
    if not r:
        return ["❔ 자원캐시: 준비중 / resource 직원이 곧 저장"]
    age = now_ts() - fnum(r.get("updated_ts"), 0.0)
    disk_pct = fnum(r.get("disk_pct"), 0.0)
    mem_pct = fnum(r.get("mem_pct"), 0.0)
    cpu_pct = fnum(r.get("cpu_total_pct"), 0.0)
    load1 = fnum(r.get("load1"), 0.0)
    load5 = fnum(r.get("load5"), 0.0)
    load15 = fnum(r.get("load15"), 0.0)
    proc = r.get("cpu_proc_pct") if isinstance(r.get("cpu_proc_pct"), dict) else {}
    rss = r.get("rss") if isinstance(r.get("rss"), dict) else {}
    disk_icon = "✅" if disk_pct < 85 else ("⚠️" if disk_pct < 95 else "❌")
    mem_icon = "✅" if mem_pct < 80 else ("⚠️" if mem_pct < 92 else "❌")
    cpu_icon = "✅" if cpu_pct < 70 and load1 < 1.5 else ("⚠️" if cpu_pct < 90 and load1 < 3.0 else "❌")
    return [
        f"{disk_icon} 디스크: 사용 {disk_pct:.1f}% / 남음 {_fmt_bytes(r.get('disk_free',0))} / 전체 {_fmt_bytes(r.get('disk_total',0))}",
        f"{mem_icon} 메모리: 사용 {mem_pct:.1f}% / 남음 {_fmt_bytes(r.get('mem_avail',0))} / 전체 {_fmt_bytes(r.get('mem_total',0))}",
        f"{cpu_icon} CPU: 전체 {cpu_pct:.1f}% / main {fnum(proc.get('main'),0):.1f}% / paper {fnum(proc.get('paper'),0):.1f}% / WS {fnum(proc.get('ws'),0):.1f}% / micro {fnum(proc.get('micro'),0):.1f}%",
        f"- load: {load1:.2f} / {load5:.2f} / {load15:.2f} / RSS main {_fmt_bytes(rss.get('main',0))} / paper {_fmt_bytes(rss.get('paper',0))} / WS {_fmt_bytes(rss.get('ws',0))} / micro {_fmt_bytes(rss.get('micro',0))} / 캐시 {age:.0f}초 전",
    ]


def _cache_payload(text: str, name: str) -> Dict[str, Any]:
    return {"version": BOT_VERSION, "name": name, "updated_ts": now_ts(), "updated_text": now_text(), "text": str(text or "")}


def _read_cached_text(path: Path, title: str) -> str:
    obj = load_json(path, {})
    if isinstance(obj, dict) and obj.get("text"):
        age = now_ts() - fnum(obj.get("updated_ts"), 0.0)
        text = str(obj.get("text") or "")
        # 캐시 나이는 핵심 상태판만 하단에 붙인다. 긴 품질 출력에는 이미 기준시각이 있다.
        if title in {"/health", "/external_status", "/version_score"}:
            return text + f"\n\n- 캐시: {age:.0f}초 전 / 명령어는 재계산 없이 캐시만 읽음"
        return text
    return "\n".join([
        f"❔ {title} 캐시 준비중",
        "- v209부터 기본 명령어는 직접 재계산하지 않고 요약 캐시만 읽습니다.",
        "- 스캔/요약 직원이 곧 캐시를 저장합니다.",
    ])


def _build_light_command_caches() -> None:
    """가벼운 상태판 캐시. 후보/latest와 외부 캐시 overlay는 여기서만 수행한다."""
    try:
        _write_resource_status()
    except Exception as exc:
        log_error("v209_resource_cache", exc)
    try:
        save_json(FILES["external_snapshot"], _cache_payload(_v209_direct_external_status_text(), "external_status"))
    except Exception as exc:
        log_error("v209_external_cache", exc)
    try:
        save_json(FILES["health_snapshot"], _cache_payload(_v209_direct_health_text(), "health"))
    except Exception as exc:
        log_error("v209_health_cache", exc)


def _build_version_score_cache() -> None:
    try:
        save_json(FILES["version_score_summary"], _cache_payload(_v209_direct_version_score_text(), "version_score"))
    except Exception as exc:
        log_error("v209_version_score_cache", exc)


def _build_quality_cache() -> None:
    try:
        txt = _v209_direct_candidate_quality_text(False)
        txt = txt.replace("- 기본은 가볍게: 현재후보 + 현재버전 + 최근 3시간/12시간 핵심만 표시", "- v209 기본은 캐시 전용: 계산은 요약 직원이 수행, 명령어는 캐시만 표시")
        save_json(FILES["quality_summary"], _cache_payload(txt, "quality"))
    except Exception as exc:
        log_error("v209_quality_cache", exc)


def command_cache_worker_loop() -> None:
    log("v209 command_cache_worker started")
    # CPU%는 이전 샘플과 비교해야 하므로 시작 직후 한 번 찍고 다음 주기부터 의미가 있다.
    try:
        _write_resource_status()
    except Exception:
        pass
    while not _stop_event.is_set():
        nowv = now_ts()
        try:
            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_light"), 0.0) >= max(2.0, COMMAND_CACHE_SEC):
                _build_light_command_caches()
                _COMMAND_CACHE_STATE["last_light"] = nowv
            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_version"), 0.0) >= max(10.0, VERSION_SCORE_CACHE_SEC):
                _build_version_score_cache()
                _COMMAND_CACHE_STATE["last_version"] = nowv
            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_quality"), 0.0) >= max(30.0, QUALITY_CACHE_SEC):
                _build_quality_cache()
                _COMMAND_CACHE_STATE["last_quality"] = nowv
        except Exception as exc:
            log_error("command_cache_worker_loop", exc)
        _stop_event.wait(2.0)
    log("v209 command_cache_worker stopped")


def health_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["health_snapshot"], "/health")


def external_status_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["external_snapshot"], "/external_status")


def version_score_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["version_score_summary"], "/version_score")


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    return _read_cached_text(FILES["quality_summary"], "/quality")


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    """v209 WS 대상 본선.
    후보/모의진입/재확인 후보를 앞쪽에 고정하고, stale/missing이 많으면 urgent rewrite한다.
    WS fresh 부족은 차단조건이 아니라 수집 우선순위 문제로만 다룬다.
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
        missing_or_stale = 0
        checked_priority = 0

        def push(ticker: Any, source: str, priority: int = 0) -> None:
            t = _ticker_from_any(ticker)
            if not t or t in STABLE_EXCLUDED or t in seen:
                return
            seen.add(t)
            ticks.append(t)
            prev = old_meta.get(t, {})
            first_seen = fnum(prev.get("first_seen"), nowv) if isinstance(prev, dict) else nowv
            meta[t] = {"first_seen": first_seen or nowv, "last_seen": nowv, "source": source, "priority": priority}

        current_priority = list(priority_rows or [])
        previous_priority = recent_candidate_priority_rows(limit=320)

        def pri_key(r: Dict[str, Any]) -> tuple:
            return (
                bool((r or {}).get("paper_bot_open") or (r or {}).get("trade_ready") or (r or {}).get("open_eligible")),
                str((r or {}).get("final_entry_action") or "") in {"paper_open", "trade_ready", "open"},
                str((r or {}).get("final_entry_action") or "") == "recheck_wait",
                fnum((r or {}).get("score"), 0),
                fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
            )

        # 1순위: 이번 scan의 모의진입/통과/재확인 후보. 여기가 신선해야 후보품질 판단이 가능하다.
        urgent_rows = sorted([r for r in current_priority if isinstance(r, dict)], key=pri_key, reverse=True)
        # 2순위: 직전 scan 후보. websocket은 재구독 뒤 tick이 와야 하므로 한두 scan 유지가 필요하다.
        carry_rows = sorted([r for r in previous_priority if isinstance(r, dict)], key=pri_key, reverse=True)
        for source_name, arr, pr in (("current_candidate", urgent_rows, 120), ("recent_candidate", carry_rows, 100)):
            for r in arr:
                t = _ticker_from_any((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
                if t:
                    try:
                        ws = ws_snapshot(t)
                        st = str(ws.get("ws_row_status") or "missing")
                        age = fnum(ws.get("ws_age_sec", ws.get("live_age_sec", -1)), -1)
                        if st != "fresh" and not (0 <= age <= WS_HUB_STALE_SEC):
                            missing_or_stale += 1
                        checked_priority += 1
                    except Exception:
                        pass
                push(t, source_name, pr)
                if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                    break
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        # 3순위: 보유중 OPEN. 청산/보호 판단용으로 유지.
        for t in _paper_open_tickers(limit=90):
            push(t, "paper_open", 95)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        # 4순위: 직전 target TTL 유지. 재구독 반복보다 신선 수신 유지가 우선이다.
        kept_old = 0
        for t in old_targets:
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break
            prev = old_meta.get(t, {})
            last_seen = fnum(prev.get("last_seen") or old_updated, old_updated)
            if last_seen > 0 and nowv - last_seen <= WS_TARGET_KEEP_TTL_SEC:
                push(t, "ttl_keep", 50)
                kept_old += 1

        # 5순위: 점수/돈흐름/거래대금 상위로 남은 자리를 채운다.
        scored = sorted(rows or [], key=lambda r: (
            fnum((r or {}).get("score"), 0),
            fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
            fnum((r or {}).get("turnover_24h"), 0),
        ), reverse=True)
        for r in scored:
            push((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"), "scored_row", 30)
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break

        # 6순위: 대형주는 시장 참고용. 남는 자리에서만 넣는다.
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
        urgent_missing = [t for t in ticks[:min(80, len(ticks))] if t and t not in set(old_targets)]
        since_write = nowv - old_updated if old_updated > 0 else 9999.0
        miss_ratio = (missing_or_stale / checked_priority) if checked_priority else 0.0
        urgent_due = bool(urgent_missing) and since_write >= max(0.2, min(WS_TARGET_URGENT_REWRITE_SEC, 0.8))
        freshness_due = checked_priority >= 10 and miss_ratio >= WS_CANDIDATE_URGENT_MISSING_RATIO and since_write >= max(0.5, min(WS_TARGET_REWRITE_MIN_SEC, 1.5))
        routine_due = changed and since_write >= WS_TARGET_REWRITE_MIN_SEC
        should_write = not path.exists() or not old_targets or urgent_due or freshness_due or routine_due
        write_note = "write" if should_write else "skip_same_or_debounce"
        payload = {
            "version": BOT_VERSION,
            "updated_ts": nowv if should_write else old_updated,
            "reason": reason,
            "max_tickers": WS_HUB_MAX_TICKERS,
            "targets": ticks,
            "target_meta": meta,
            "priority_count": len(current_priority) + len(previous_priority),
            "paper_open_count": len(_paper_open_tickers(limit=90)),
            "priority_first": True,
            "ttl_keep_sec": WS_TARGET_KEEP_TTL_SEC,
            "rewrite_min_sec": WS_TARGET_REWRITE_MIN_SEC,
            "changed": changed,
            "added": added,
            "removed": removed,
            "urgent_missing": urgent_missing[:12],
            "candidate_missing_or_stale": missing_or_stale,
            "candidate_checked": checked_priority,
            "candidate_missing_ratio": round(miss_ratio, 3),
            "freshness_due": freshness_due,
            "kept_old": kept_old,
            "write_note": write_note,
            "note": "v209: current/recheck/open candidates first; WS stale/missing triggers urgent target rewrite; not a hard entry block",
        }
        if should_write:
            atomic_write(path, payload)
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
            STATE["ws_target_candidate_missing_ratio"] = round(miss_ratio, 3)
            STATE["ws_target_freshness_due"] = freshness_due
            STATE["ws_target_kept_old"] = kept_old
    except Exception as exc:
        log_error("update_ws_targets_v209", exc)


def external_health_warning_lines(snap: Optional[Dict[str, Any]] = None) -> List[str]:  # type: ignore[override]
    snap = snap or _external_health_snapshot()
    lines: List[str] = []
    total = int(snap.get("total", 0) or 0)
    if not snap.get("ws_worker_ok"):
        lines.append("⚠️ WS 직원 수신 확인 필요: 가드봇 /gws_state 확인")
    if not snap.get("micro_worker_ok"):
        lines.append("⚠️ 호가·체결 직원 수집 확인 필요: 가드봇 /gmicro_state 확인")
    if total > 0 and int(snap.get("ws_fresh", 0) or 0) <= 0:
        lines.append("⚠️ 현재 후보 WS fresh 0개: 차단조건 아님, WS 우선수집 대상 갱신 중")
    elif total > 0 and int(snap.get("ws_fresh", 0) or 0) < max(3, int(total * 0.35)):
        lines.append("⚠️ WS 후보 fresh 부족: 차단조건 아님, 후보/재확인 종목을 우선수집 중")
    if total > 0 and int(snap.get("micro_fresh", 0) or 0) <= 0:
        lines.append("⚠️ 현재 후보 호가·체결 fresh 0개: micro 대상/수집 상태 확인")
    return lines

def start_background_workers() -> None:
    global _background_workers_started
    if _background_workers_started:
        return
    _background_workers_started = True
    for i in range(max(1, PRECISION_BACKGROUND_WORKERS)):
        threading.Thread(target=precision_worker_loop, args=(i + 1,), name=f"precision_worker_{i+1}", daemon=True).start()
    for i in range(max(1, EXEC_RISK_BACKGROUND_WORKERS)):
        threading.Thread(target=execution_risk_worker_loop, args=(i + 1,), name=f"execution_risk_worker_{i+1}", daemon=True).start()
    threading.Thread(target=command_cache_worker_loop, name="command_cache_worker_v210", daemon=True).start()
    websocket_hub_worker_loop()  # v180: legacy WS는 스레드도 만들지 않고 하드 격리
    log(f"background_workers started precision={PRECISION_BACKGROUND_WORKERS} execution_risk={EXEC_RISK_BACKGROUND_WORKERS} command_cache=v212 websocket=external_sidecar_cache requested={WS_HUB_REQUESTED}")


def startup_checks() -> None:
    ensure_eval_baseline()
    for p in [FILES["paper"], FILES["shadow"]]:
        ok, note = ensure_candidate_file(p)
        log(f"candidate_file {p.name}: {ok} {note}")
    # candidate_events는 일부러 만들지도 읽지도 않는다.
    save_json(FILES["status"], STATE)
    try:
        _build_light_command_caches()
    except Exception as exc:
        log_error("startup_v209_cache", exc)



# ===============================
# v2.13.213: 캐시 생성 저부하 수술 + 스캔 병목 복구
# - 기본 명령어는 v209와 동일하게 캐시만 읽는다.
# - v209에서 무거운 direct quality/version 계산이 백그라운드로 이동하며 scan을 잡아먹던 경로를 끊는다.
# - 캐시 생성 직원은 scan 중 무거운 요약을 하지 않고, 오래된 캐시는 오래됨/대기중으로만 표시한다.
# - 전략 조건/청산/BUY_READY는 변경하지 않는다.
# ===============================
V210_VERSION_NOTE = "v210_cache_worker_low_load_scan_protect_2026-05-16"
COMMAND_CACHE_SEC = max(float(os.getenv("CLEAN_COMMAND_CACHE_SEC", "8")), 8.0)
QUALITY_CACHE_SEC = max(float(os.getenv("CLEAN_QUALITY_CACHE_SEC", "90")), 90.0)
VERSION_SCORE_CACHE_SEC = max(float(os.getenv("CLEAN_VERSION_SCORE_CACHE_SEC", "90")), 90.0)
V210_SCAN_BUSY_LIGHT_SKIP_SEC = float(os.getenv("CLEAN_V210_SCAN_BUSY_LIGHT_SKIP_SEC", "18"))
V210_CPU_SAMPLE_MIN_SEC = float(os.getenv("CLEAN_V210_CPU_SAMPLE_MIN_SEC", "4.0"))
V210_QUALITY_CLOSED_TAIL = int(os.getenv("CLEAN_V210_QUALITY_CLOSED_TAIL", "1800"))
V210_VERSION_CLOSED_TAIL = int(os.getenv("CLEAN_V210_VERSION_CLOSED_TAIL", "1000"))

_V210_CPU_SAMPLE: Dict[str, Any] = {"ts": 0.0, "system": None, "proc": {}, "last_good": {}}
_V210_CACHE_LOCK = threading.RLock()
_V210_CACHE_BUSY: Dict[str, bool] = {"light": False, "quality": False, "version": False}


def _v210_scan_running() -> bool:
    try:
        if bool(STATE.get("scan_running")):
            return True
        stage = str(STATE.get("scan_last_stage") or "")
        if stage not in {"", "done", "error", "boot", "-"}:
            started = fnum(STATE.get("scan_started_at"), 0.0)
            if started > 0 and now_ts() - started < 300:
                return True
    except Exception:
        pass
    return False


def _v210_rows_last_hours(rows: Iterable[Dict[str, Any]], hours: int) -> List[Dict[str, Any]]:
    cutoff = now_ts() - max(1, int(hours)) * 3600
    return [r for r in rows or [] if closed_ts(r) >= cutoff]


def _v210_strict_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows or []:
        lane = str((r or {}).get("lane") or (r or {}).get("source_lane") or "strict")
        if lane == "strict":
            out.append(r)
    return out


def _v210_current_version_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        return rows_since_current_version(list(rows or []))
    except Exception:
        return [r for r in rows or [] if BOT_VERSION in str(row_brain_version(r))]


def _v210_reason_lines(rows: List[Dict[str, Any]], limit: int = 4) -> List[str]:
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        groups.setdefault(str(r.get("exit_reason") or "unknown"), []).append(r)
    items = []
    for reason, arr in groups.items():
        st = score_stats(arr)
        items.append((len(arr), reason, st))
    if not items:
        return ["- 종료 없음"]
    out = []
    for _, reason, st in sorted(items, reverse=True)[:limit]:
        icon = score_icon(st)
        out.append(f"{icon} {label_kr(reason)}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%{sample_note(st['n'])}")
    return out


def _v210_candidate_rows() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    strict = tail_jsonl(FILES.get("paper_latest", BASE_DIR / "paper_candidates_latest.jsonl"), max_lines=260)
    shadow = tail_jsonl(FILES.get("shadow_latest", BASE_DIR / "shadow_candidates_latest.jsonl"), max_lines=260)
    return strict, shadow


def _v210_candidate_counts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {"total": len(rows), "trade_ready": 0, "recheck": 0, "observe": 0, "paper_open": 0, "final_pass": 0, "relative": Counter(), "hold": Counter()}
    for r in rows or []:
        action = str(r.get("final_entry_action") or r.get("final_entry_label") or "")
        if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready")) or action in {"paper_open", "trade_ready", "open"}:
            out["trade_ready"] += 1
        if action == "recheck_wait":
            out["recheck"] += 1
        if action == "observe":
            out["observe"] += 1
        if action in {"paper_open", "trade_ready", "open"} or str(r.get("final_entry_label") or "").find("통과") >= 0:
            out["final_pass"] += 1
        rel = str(r.get("relative_strength_label") or r.get("relative_label") or "-")
        out["relative"][rel] += 1
        reason = str(r.get("final_entry_label") or r.get("hold_reason") or r.get("quality_label") or "-")
        if not (bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))):
            out["hold"][reason] += 1
    return out


def _v210_external_snapshot_dict() -> Dict[str, Any]:
    """현재 latest 후보와 현재 WS/micro 캐시만 한 번 읽어 fresh 통계를 만든다."""
    strict, _shadow = _v210_candidate_rows()
    try:
        refresh_external_ws_cache()
    except Exception as exc:
        log_error("v210_refresh_ws_for_snapshot", exc)
    try:
        refresh_micro_cache()
    except Exception as exc:
        log_error("v210_refresh_micro_for_snapshot", exc)
    ws_fresh = ws_stale = ws_missing = 0
    micro_fresh = micro_stale = micro_missing = 0
    ws_targeted = micro_targeted = 0
    examples = []
    for r in strict:
        t = r.get("ticker") or r.get("market") or r.get("symbol")
        ws = ws_snapshot(t)
        ms = micro_snapshot(t)
        if bool(ws.get("ws_targeted")):
            ws_targeted += 1
        if bool(ms.get("micro_targeted")):
            micro_targeted += 1
        wst = str(ws.get("ws_row_status") or "missing")
        mst = str(ms.get("micro_row_status") or "missing")
        if wst == "fresh":
            ws_fresh += 1
        elif wst == "stale":
            ws_stale += 1
        else:
            ws_missing += 1
        if mst == "fresh":
            micro_fresh += 1
        elif mst == "stale":
            micro_stale += 1
        else:
            micro_missing += 1
        if len(examples) < 4 and (wst != "fresh" or mst != "fresh"):
            examples.append(f"{_ticker_from_any(t)}: WS {wst} / micro {mst}")
    return {
        "updated_ts": now_ts(),
        "total": len(strict),
        "ws_fresh": ws_fresh,
        "ws_stale": ws_stale,
        "ws_missing": ws_missing,
        "ws_targeted": ws_targeted,
        "micro_fresh": micro_fresh,
        "micro_stale": micro_stale,
        "micro_missing": micro_missing,
        "micro_targeted": micro_targeted,
        "ws_worker_state": STATE.get("ws_state", "-"),
        "ws_worker_targets": STATE.get("ws_targets", 0),
        "ws_worker_fresh": STATE.get("ws_fresh", 0),
        "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
        "micro_worker_state": STATE.get("micro_state", "-"),
        "micro_worker_targets": STATE.get("micro_targets", 0),
        "micro_worker_fresh": STATE.get("micro_fresh", 0),
        "examples": examples,
    }


def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap or _v210_external_snapshot_dict()
    total = int(snap.get("total", 0) or 0)
    lines = [
        "🛰 외부정보 상태 /external_status",
        "",
        "[1/3] 직원 상태",
        f"✅ 웹소켓: {snap.get('ws_worker_state','-')} / 대상 {snap.get('ws_worker_targets',0)} / 신선 {snap.get('ws_worker_fresh',0)} / 최근 {snap.get('ws_last_age_sec','-')}초",
        f"✅ 호가·체결: {snap.get('micro_worker_state','-')} / 대상 {snap.get('micro_worker_targets',0)} / 신선 {snap.get('micro_worker_fresh',0)}",
        "",
        "[2/3] 현재 후보 반영",
        f"- 정식 후보 {total}개",
        f"- 웹소켓: 신선 {snap.get('ws_fresh',0)} / 대상 {snap.get('ws_targeted',0)} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {snap.get('micro_fresh',0)} / 대상 {snap.get('micro_targeted',0)} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        "- 기준: latest 후보 + 현재 WS/micro cache 1회 overlay",
        "",
        "[3/3] 판독",
    ]
    if total <= 0:
        lines.append("❔ 현재 latest 후보가 적거나 준비중")
    else:
        lines.append("✅ 웹소켓 후보 반영 중" if int(snap.get('ws_fresh',0)) > 0 else "⚠️ 웹소켓 후보 fresh 부족: 차단조건 아님, 우선수집 중")
        lines.append("✅ 호가·체결 후보 반영 중" if int(snap.get('micro_fresh',0)) > 0 else "⚠️ 호가·체결 후보 fresh 부족: micro target 우선순위 확인")
    ex = snap.get("examples") if isinstance(snap.get("examples"), list) else []
    if ex:
        lines += ["", "예시", *[f"- {x}" for x in ex[:4]]]
    return "\n".join(lines)


def _v210_health_text() -> str:
    snap = _v210_external_snapshot_dict()
    scan = scan_status_summary()
    paper = read_paper_status().get("status", {})
    warn = external_health_warning_lines({
        "total": snap.get("total", 0),
        "ws_worker_ok": True,
        "micro_worker_ok": True,
        "ws_fresh": snap.get("ws_fresh", 0),
        "micro_fresh": snap.get("micro_fresh", 0),
    })
    lines = [
        "🧭 건강상태 /health",
        "- 메인봇방용 간단 상태판입니다. 재시작/업그레이드는 가드봇 담당.",
        "",
        "[1/5] 메인봇",
        f"{scan.get('icon','❔')} {BOT_VERSION} / scan {scan.get('label','-')} / 단계 {scan.get('stage','-')} / 후보 {snap.get('total',0)}개",
        f"- 오류: {'✅ 새 실행 중 오류 없음' if not STATE.get('scan_last_error') else '❌ ' + str(STATE.get('scan_last_error'))[:160]}",
        "",
        "[2/5] 서버자원",
        *resource_health_lines(),
        "",
        "[3/5] 외부정보",
        f"✅ WS: {snap.get('ws_worker_state','-')} / 전체신선 {snap.get('ws_worker_fresh',0)} / 후보신선 {snap.get('ws_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)} / 최근 {snap.get('ws_last_age_sec','-')}초",
        f"✅ 호가·체결: {snap.get('micro_worker_state','-')} / 전체신선 {snap.get('micro_worker_fresh',0)} / 후보신선 {snap.get('micro_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        "",
        "[4/5] paper_bot",
        f"✅ {paper.get('version','paper_bot')} / running {paper.get('running','?')} / OPEN {paper.get('open_total', paper.get('open_count','?'))} / CLOSED {paper.get('closed_total','?')} / 상태 {int(now_ts()-fnum(paper.get('updated_ts'), now_ts())) if fnum(paper.get('updated_ts'),0)>0 else '?'}초 전",
        "",
        "[5/5] 판독",
    ]
    if warn:
        lines += warn
    else:
        lines.append("✅ 외부정보 신선도 확인됨")
    return "\n".join(lines)


def _v210_version_score_text() -> str:
    rows = _v210_strict_rows(load_closed(V210_VERSION_CLOSED_TAIL))
    cur = _v210_current_version_rows(rows)
    reasons = _reason_summary(cur, limit=3)
    return "\n".join([
        "📊 현재버전 성과 /version_score",
        _compact_stat_line("현재버전 정식 모의매매", cur),
        f"- 기준: {BOT_VERSION} / {version_baseline_text()} / 기존 기록 삭제 없음",
        f"- 주요종료: {reasons}",
        "- 참고: 상세 손절은 /loss_review 206",
    ])


def _v210_quality_text() -> str:
    strict, shadow = _v210_candidate_rows()
    counts = _v210_candidate_counts(strict)
    ext = _v210_external_snapshot_dict()
    closed = _v210_strict_rows(load_closed(V210_QUALITY_CLOSED_TAIL))
    rows3 = _v210_rows_last_hours(closed, 3)
    rows12 = _v210_rows_last_hours(closed, 12)
    cur = _v210_current_version_rows(closed)
    examples_open = [r for r in strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    examples_hold = [r for r in strict if not bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    def ex_line(r: Dict[str, Any]) -> str:
        return f"- {_ticker_from_any(r.get('ticker') or r.get('market') or r.get('symbol'))}: 점수 {fnum(r.get('score'),0):.2f} / 3분돈 {krw_m(r.get('money_flow_3m') or r.get('turnover_3m'))} / 눌림 {fnum(r.get('pullback_quality_score'),0):.2f} / 상대 {r.get('relative_strength_label','-')} / {r.get('final_entry_label') or r.get('quality_label') or '-'}"
    hold = counts.get("hold") if isinstance(counts.get("hold"), Counter) else Counter()
    hold_txt = " / ".join(f"{k} {v}" for k, v in hold.most_common(3)) if hold else "-"
    rel = counts.get("relative") if isinstance(counts.get("relative"), Counter) else Counter()
    rel_txt = " / ".join(f"{k} {v}" for k, v in rel.most_common(4)) if rel else "-"
    lines = [
        "🔬 후보품질 요약 /quality",
        "- v214 기본은 캐시 전용: 명령어는 캐시만 읽고, 요약직원은 scan 중 무거운 계산을 건너뜁니다.",
        "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full",
        "",
        "[1/5] 성과 요약",
        _compact_stat_line("최근 3시간 정식", rows3),
        _compact_stat_line("최근 12시간 정식", rows12),
        _compact_stat_line("현재버전 정식", cur),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(strict)}개 / 🧪 모의진입 {counts.get('trade_ready',0)}개 / ⚠️ 재확인 {counts.get('recheck',0)}개 / ❌ 진입보류 {max(0, len(strict)-int(counts.get('trade_ready',0)))}개 / 복기 {len(shadow)}개",
        f"- 최종검증: 통과 {counts.get('final_pass',0)} / 재확인 {counts.get('recheck',0)} / 관찰 {counts.get('observe',0)}",
        f"- 웹소켓: 신선 {ext.get('ws_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('ws_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('ws_stale',0)} / 없음 {ext.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {ext.get('micro_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('micro_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('micro_stale',0)} / 없음 {ext.get('micro_missing',0)}",
        f"- 상대강도: {rel_txt}",
        f"- 후보성격: 🧪 모의진입 가능 / ⚠️ 실전위험 재확인 / ❌ 진입보류를 분리 표시",
        f"- 진입 보류 사유: {hold_txt}",
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
    ]
    lines += [ex_line(r) for r in examples_open] or ["- 없음"]
    lines += ["❌ 진입보류"]
    lines += [ex_line(r) for r in examples_hold] or ["- 없음"]
    lines += ["", "[4/5] 최근 12시간 종료 사유", *_v210_reason_lines(rows12, limit=4)]
    lines += ["", "[5/5] 판독"]
    st12 = score_stats(rows12)
    if st12.get("avg", 0) < 0:
        lines.append("❌ 전체 누적은 아직 자동매매 불가")
    else:
        lines.append("⚠️ 표본 확인 필요")
    lines += ["⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결"]
    return "\n".join(lines)


def server_resource_snapshot() -> Dict[str, Any]:  # type: ignore[override]
    """v210: CPU% 안정화. 첫 샘플/짧은 샘플/비정상 튐값은 이전 정상값을 유지한다."""
    try:
        du = shutil.disk_usage(str(BASE_DIR))
        disk_total, disk_used, disk_free = float(du.total), float(du.used), float(du.free)
        disk_pct = (disk_used / disk_total * 100.0) if disk_total > 0 else 0.0
    except Exception:
        disk_total = disk_used = disk_free = disk_pct = 0.0
    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0.0)
    mem_avail = mem.get("MemAvailable", 0.0)
    mem_used = max(0.0, mem_total - mem_avail) if mem_total else 0.0
    mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    nowv = now_ts()
    total, idle = _read_proc_stat_total()
    pids = _resource_pid_map()
    proc_now = {k: _read_pid_cpu_jiffies(v) for k, v in pids.items()}
    rss = {k: _proc_rss_bytes_for_pid(v) for k, v in pids.items()}
    last_good = _V210_CPU_SAMPLE.get("last_good") if isinstance(_V210_CPU_SAMPLE.get("last_good"), dict) else {}
    cpu_total_pct = fnum(last_good.get("cpu_total_pct"), 0.0)
    proc_pct = dict(last_good.get("cpu_proc_pct") or {k: 0.0 for k in pids}) if isinstance(last_good.get("cpu_proc_pct"), dict) else {k: 0.0 for k in pids}
    sample_note = "측정중"
    try:
        prev_ts = fnum(_V210_CPU_SAMPLE.get("ts"), 0.0)
        prev_total, prev_idle = _V210_CPU_SAMPLE.get("system") or (0.0, 0.0)
        elapsed = nowv - prev_ts if prev_ts > 0 else 0.0
        dt_total = total - float(prev_total or 0.0)
        dt_idle = idle - float(prev_idle or 0.0)
        hz = float(os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK")) or 100)
        if elapsed >= V210_CPU_SAMPLE_MIN_SEC and dt_total > 0:
            raw_total = max(0.0, min(100.0, (1.0 - (dt_idle / dt_total)) * 100.0))
            prev_proc = _V210_CPU_SAMPLE.get("proc") if isinstance(_V210_CPU_SAMPLE.get("proc"), dict) else {}
            raw_proc: Dict[str, float] = {}
            max_proc = max(100.0, float(os.cpu_count() or 1) * 100.0)
            valid = True
            for k, cur in proc_now.items():
                old = float(prev_proc.get(k, cur) or cur)
                val = ((float(cur) - old) / max(0.001, elapsed * hz)) * 100.0
                if val < -1 or val > max_proc * 1.5:
                    valid = False
                raw_proc[k] = max(0.0, min(max_proc, val))
            if valid:
                cpu_total_pct = raw_total
                proc_pct = raw_proc
                sample_note = f"측정간격 {elapsed:.1f}s"
                _V210_CPU_SAMPLE["last_good"] = {"cpu_total_pct": cpu_total_pct, "cpu_proc_pct": proc_pct, "sample_note": sample_note}
    except Exception:
        pass
    _V210_CPU_SAMPLE.update({"ts": nowv, "system": (total, idle), "proc": proc_now})
    return {
        "version": BOT_VERSION,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "disk_pct": disk_pct,
        "mem_total": mem_total,
        "mem_used": mem_used,
        "mem_avail": mem_avail,
        "mem_pct": mem_pct,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "cpu_total_pct": cpu_total_pct,
        "cpu_proc_pct": proc_pct,
        "cpu_sample_note": sample_note,
        "pids": pids,
        "rss": rss,
        "note": "v212 resource monitor cache; commands read cache only",
    }


def resource_health_lines() -> List[str]:  # type: ignore[override]
    r = _read_resource_status()
    if not r:
        return ["❔ 자원캐시: 준비중 / resource 직원이 곧 저장"]
    age = now_ts() - fnum(r.get("updated_ts"), 0.0)
    disk_pct = fnum(r.get("disk_pct"), 0.0)
    mem_pct = fnum(r.get("mem_pct"), 0.0)
    cpu_pct = fnum(r.get("cpu_total_pct"), 0.0)
    load1 = fnum(r.get("load1"), 0.0)
    load5 = fnum(r.get("load5"), 0.0)
    load15 = fnum(r.get("load15"), 0.0)
    proc = r.get("cpu_proc_pct") if isinstance(r.get("cpu_proc_pct"), dict) else {}
    rss = r.get("rss") if isinstance(r.get("rss"), dict) else {}
    note = str(r.get("cpu_sample_note") or "측정중")
    disk_icon = "✅" if disk_pct < 85 else ("⚠️" if disk_pct < 95 else "❌")
    mem_icon = "✅" if mem_pct < 80 else ("⚠️" if mem_pct < 92 else "❌")
    cpu_icon = "✅" if cpu_pct < 70 and load1 < 1.5 else ("⚠️" if cpu_pct < 90 and load1 < 3.0 else "❌")
    return [
        f"{disk_icon} 디스크: 사용 {disk_pct:.1f}% / 남음 {_fmt_bytes(r.get('disk_free',0))} / 전체 {_fmt_bytes(r.get('disk_total',0))}",
        f"{mem_icon} 메모리: 사용 {mem_pct:.1f}% / 남음 {_fmt_bytes(r.get('mem_avail',0))} / 전체 {_fmt_bytes(r.get('mem_total',0))}",
        f"{cpu_icon} CPU: 전체 {cpu_pct:.1f}% / main {fnum(proc.get('main'),0):.1f}% / paper {fnum(proc.get('paper'),0):.1f}% / WS {fnum(proc.get('ws'),0):.1f}% / micro {fnum(proc.get('micro'),0):.1f}%",
        f"- load: {load1:.2f} / {load5:.2f} / {load15:.2f} / RSS main {_fmt_bytes(rss.get('main',0))} / paper {_fmt_bytes(rss.get('paper',0))} / WS {_fmt_bytes(rss.get('ws',0))} / micro {_fmt_bytes(rss.get('micro',0))} / {note} / 캐시 {age:.0f}초 전",
    ]


def _build_light_command_caches() -> None:  # type: ignore[override]
    """v210: scan 중에는 resource만 갱신하고, 후보/외부 overlay는 scan이 쉬는 구간에만 수행."""
    try:
        _write_resource_status()
    except Exception as exc:
        log_error("v210_resource_cache", exc)
    if _v210_scan_running():
        return
    try:
        ext_snap = _v210_external_snapshot_dict()
        save_json(FILES["external_snapshot"], _cache_payload(_v210_external_status_text(ext_snap), "external_status"))
        save_json(FILES["health_snapshot"], _cache_payload(_v210_health_text(), "health"))
    except Exception as exc:
        log_error("v210_light_cache", exc)


def _build_version_score_cache() -> None:  # type: ignore[override]
    try:
        save_json(FILES["version_score_summary"], _cache_payload(_v210_version_score_text(), "version_score"))
    except Exception as exc:
        log_error("v210_version_score_cache", exc)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        save_json(FILES["quality_summary"], _cache_payload(_v210_quality_text(), "quality"))
    except Exception as exc:
        log_error("v210_quality_cache", exc)


def command_cache_worker_loop() -> None:  # type: ignore[override]
    log("v212 command_cache_worker started")
    try:
        _write_resource_status()
    except Exception:
        pass
    while not _stop_event.is_set():
        nowv = now_ts()
        try:
            # 자원 캐시는 scan 중에도 가볍게 갱신한다.
            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_resource"), 0.0) >= 5.0:
                _write_resource_status()
                _COMMAND_CACHE_STATE["last_resource"] = nowv

            # scan 중에는 health/external/quality/version 요약을 새로 만들지 않는다.
            # 명령어는 기존 캐시를 보여주거나 오래됨/준비중으로 표시한다.
            if _v210_scan_running():
                _stop_event.wait(2.0)
                continue

            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_light"), 0.0) >= COMMAND_CACHE_SEC:
                if not _V210_CACHE_BUSY.get("light"):
                    _V210_CACHE_BUSY["light"] = True
                    try:
                        _build_light_command_caches()
                        _COMMAND_CACHE_STATE["last_light"] = nowv
                    finally:
                        _V210_CACHE_BUSY["light"] = False
            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_version"), 0.0) >= VERSION_SCORE_CACHE_SEC:
                if not _V210_CACHE_BUSY.get("version"):
                    _V210_CACHE_BUSY["version"] = True
                    try:
                        _build_version_score_cache()
                        _COMMAND_CACHE_STATE["last_version"] = nowv
                    finally:
                        _V210_CACHE_BUSY["version"] = False
            if nowv - fnum(_COMMAND_CACHE_STATE.get("last_quality"), 0.0) >= QUALITY_CACHE_SEC:
                if not _V210_CACHE_BUSY.get("quality"):
                    _V210_CACHE_BUSY["quality"] = True
                    try:
                        _build_quality_cache()
                        _COMMAND_CACHE_STATE["last_quality"] = nowv
                    finally:
                        _V210_CACHE_BUSY["quality"] = False
        except Exception as exc:
            log_error("v210_command_cache_worker_loop", exc)
        _stop_event.wait(2.0)
    log("v212 command_cache_worker stopped")


def health_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["health_snapshot"], "/health")


def external_status_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["external_snapshot"], "/external_status")


def version_score_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["version_score_summary"], "/version_score")


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    return _read_cached_text(FILES["quality_summary"], "/quality")


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    """v210: 현재 후보/직전 후보를 micro target 최상단에 고정한다. 조건/차단 변경 없음."""
    try:
        ticks: List[str] = []
        seen: set = set()
        current_priority = list(priority_rows or [])
        previous_priority = recent_candidate_priority_rows(limit=260)
        def pri_key(r: Dict[str, Any]) -> tuple:
            return (
                bool((r or {}).get("paper_bot_open") or (r or {}).get("trade_ready") or (r or {}).get("open_eligible")),
                str((r or {}).get("final_entry_action") or "") in {"paper_open", "trade_ready", "open"},
                str((r or {}).get("final_entry_action") or "") == "recheck_wait",
                fnum((r or {}).get("score"), 0),
                fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
            )
        for arr, label in ((sorted(current_priority, key=pri_key, reverse=True), "current_candidate"), (sorted(previous_priority, key=pri_key, reverse=True), "recent_candidate")):
            for r in arr:
                _push_unique_ticker(ticks, seen, (r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
                if len(ticks) >= max(8, MICRO_TARGET_MAX):
                    break
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        for t in _paper_open_tickers(limit=80):
            _push_unique_ticker(ticks, seen, t)
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        scored = sorted(rows or [], key=lambda r: (
            fnum((r or {}).get("score"), 0),
            fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
            fnum((r or {}).get("turnover_24h"), 0),
        ), reverse=True)
        for r in scored:
            _push_unique_ticker(ticks, seen, (r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        for t in list(MAJOR_WATCH_TICKERS) + ["BTC", "ETH", "XRP"]:
            _push_unique_ticker(ticks, seen, t)
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
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
            "priority_count": len(current_priority) + len(previous_priority),
            "priority_first": True,
            "note": "v210: current/recent/open candidates pinned first; sidecar polls priority first then rotates rest",
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
        log_error("update_micro_targets_v210", exc)


def startup_checks() -> None:  # type: ignore[override]
    ensure_eval_baseline()
    for p in [FILES["paper"], FILES["shadow"]]:
        ok, note = ensure_candidate_file(p)
        log(f"candidate_file {p.name}: {ok} {note}")
    save_json(FILES["status"], STATE)
    try:
        _write_resource_status()
    except Exception as exc:
        log_error("startup_v210_resource_cache", exc)
    # 첫 health/external은 scan이 한 번 돈 뒤 가볍게 채운다. 여기서 old direct heavy path는 실행하지 않는다.


def start_background_workers() -> None:  # type: ignore[override]
    global _background_workers_started
    if _background_workers_started:
        return
    _background_workers_started = True
    for i in range(max(1, PRECISION_BACKGROUND_WORKERS)):
        threading.Thread(target=precision_worker_loop, args=(i + 1,), name=f"precision_worker_{i+1}", daemon=True).start()
    for i in range(max(1, EXEC_RISK_BACKGROUND_WORKERS)):
        threading.Thread(target=execution_risk_worker_loop, args=(i + 1,), name=f"execution_risk_worker_{i+1}", daemon=True).start()
    threading.Thread(target=command_cache_worker_loop, name="command_cache_worker_v210", daemon=True).start()
    websocket_hub_worker_loop()
    log(f"background_workers started precision={PRECISION_BACKGROUND_WORKERS} execution_risk={EXEC_RISK_BACKGROUND_WORKERS} command_cache=v210_low_load websocket=external_sidecar_cache requested={WS_HUB_REQUESTED}")

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
    send_chunks("\n".join(["✅ 봇 시작 완료", f"현재 버전: {BOT_VERSION}", f"전략: {STRATEGY_NAME}", "v242 strategy-watch display cleanup(정식 paper OPEN/청산/BUY_READY 변경 없음)", "확인: /health /external_status /version_compare /version_score /quality /errorlog"]))
    updater.start_polling(drop_pending_updates=True)
    updater.idle()
    _stop_event.set()




# ===============================
# v2.13.213: 후보/target/snapshot 단일 본선 수술
# - v211 뒤붙임식 보정은 폐기한다.
# - WS/micro target 파일은 scan 중간 단계가 아니라 factory export 이후 한 번만 쓴다.
# - health/external/quality는 같은 clean_candidate_snapshot.json을 읽는다.
# - 캐시가 없거나 오래돼도 기본 명령어는 old direct 계산으로 돌아가지 않는다.
# ===============================
V212_SNAPSHOT_WAIT_SEC = float(os.getenv("CLEAN_V212_SNAPSHOT_WAIT_SEC", "1.2"))
V212_SNAPSHOT_READ_MAX = int(os.getenv("CLEAN_V212_SNAPSHOT_READ_MAX", "500"))
V212_SNAPSHOT_MAX_ROWS = int(os.getenv("CLEAN_V212_SNAPSHOT_MAX_ROWS", "260"))


def _v212_rows_from_latest() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    strict = tail_jsonl(FILES.get("paper_latest", BASE_DIR / "paper_candidates_latest.jsonl"), max_lines=V212_SNAPSHOT_READ_MAX)
    shadow = tail_jsonl(FILES.get("shadow_latest", BASE_DIR / "shadow_candidates_latest.jsonl"), max_lines=V212_SNAPSHOT_READ_MAX)
    return [r for r in strict if isinstance(r, dict)], [r for r in shadow if isinstance(r, dict)]


def _v212_ticker(row: Dict[str, Any]) -> str:
    return _ticker_from_any((row or {}).get("ticker") or (row or {}).get("market") or (row or {}).get("symbol"))


def _v212_priority_key(row: Dict[str, Any]) -> tuple:
    return (
        bool((row or {}).get("paper_bot_open") or (row or {}).get("open_eligible") or (row or {}).get("trade_ready")),
        str((row or {}).get("final_entry_action") or "") in {"paper_open", "trade_ready", "open"},
        str((row or {}).get("final_entry_action") or "") == "recheck_wait",
        fnum((row or {}).get("score"), 0.0),
        fnum((row or {}).get("money_flow_3m") or (row or {}).get("turnover_3m"), 0.0),
        fnum((row or {}).get("turnover_24h"), 0.0),
    )


def _v212_order_candidates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for r in sorted([x for x in rows or [] if isinstance(x, dict)], key=_v212_priority_key, reverse=True):
        t = _v212_ticker(r)
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(r)
        if len(out) >= V212_SNAPSHOT_MAX_ROWS:
            break
    return out


def _v212_overlay_row(row: Dict[str, Any]) -> Dict[str, Any]:
    rr = dict(row or {})
    t = _v212_ticker(rr)
    ws = ws_snapshot(t)
    ms = micro_snapshot(t)
    rr.update(ws)
    rr.update(ms)
    rr["ticker"] = t or rr.get("ticker")
    rr["snapshot_overlay_ts"] = now_ts()
    return rr


def _v212_count_external(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ws_fresh = ws_stale = ws_missing = 0
    micro_fresh = micro_stale = micro_missing = 0
    ws_targeted = micro_targeted = 0
    examples: List[str] = []
    for r in rows or []:
        wst = str((r or {}).get("ws_row_status") or "missing")
        mst = str((r or {}).get("micro_row_status") or "missing")
        if bool((r or {}).get("ws_targeted")):
            ws_targeted += 1
        if bool((r or {}).get("micro_targeted")):
            micro_targeted += 1
        if wst == "fresh":
            ws_fresh += 1
        elif wst == "stale":
            ws_stale += 1
        else:
            ws_missing += 1
        if mst == "fresh":
            micro_fresh += 1
        elif mst == "stale":
            micro_stale += 1
        else:
            micro_missing += 1
        if len(examples) < 4 and (wst != "fresh" or mst != "fresh"):
            examples.append(f"{_v212_ticker(r)}: WS {wst} / micro {mst}")
    return {
        "total": len(rows or []),
        "ws_fresh": ws_fresh,
        "ws_stale": ws_stale,
        "ws_missing": ws_missing,
        "ws_targeted": ws_targeted,
        "micro_fresh": micro_fresh,
        "micro_stale": micro_stale,
        "micro_missing": micro_missing,
        "micro_targeted": micro_targeted,
        "examples": examples,
    }


def _v212_snapshot_payload(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:
    try:
        refresh_external_ws_cache()
    except Exception as exc:
        log_error("v212_refresh_ws_for_snapshot", exc)
    try:
        refresh_micro_cache()
    except Exception as exc:
        log_error("v212_refresh_micro_for_snapshot", exc)
    ordered = _v212_order_candidates(strict_rows)
    rows = [_v212_overlay_row(r) for r in ordered]
    ext = _v212_count_external(rows)
    nowv = now_ts()
    return {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v212",
        "stage": stage,
        "source": source,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or "-"),
        "scan_seq": int(fnum(STATE.get("scan_seq"), 0)),
        "candidate_count": len(rows),
        "shadow_count": len(shadow_rows or []),
        "rows": rows,
        "external": {
            **ext,
            "ws_worker_state": STATE.get("ws_state", "-"),
            "ws_worker_targets": STATE.get("ws_targets", 0),
            "ws_worker_fresh": STATE.get("ws_fresh", 0),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
            "micro_worker_state": STATE.get("micro_state", "-"),
            "micro_worker_targets": STATE.get("micro_targets", 0),
            "micro_worker_fresh": STATE.get("micro_fresh", 0),
        },
        "target": {
            "writer": "factory_export_final_only",
            "reason": "factory_final_candidates",
            "ws_target_count": STATE.get("ws_target_file_targets", 0),
            "micro_target_count": STATE.get("micro_target_file_targets", 0),
            "ws_target_reason": STATE.get("ws_target_reason", "-"),
            "micro_target_reason": STATE.get("micro_target_reason", "-"),
            "wait_sec": wait_sec,
        },
        "note": "v212 single source snapshot; commands read this snapshot, not separate candidate tails",
    }


def _v212_write_candidate_snapshot(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:
    payload = _v212_snapshot_payload(strict_rows, shadow_rows, stage=stage, source=source, wait_sec=wait_sec)
    save_json(FILES["candidate_snapshot"], payload)
    with _state_lock:
        STATE["candidate_snapshot_ts"] = payload.get("updated_ts", now_ts())
        STATE["candidate_snapshot_count"] = payload.get("candidate_count", 0)
        STATE["candidate_snapshot_source"] = source
        STATE["candidate_snapshot_stage"] = stage
        STATE["candidate_snapshot_external"] = payload.get("external", {})
    return payload


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:
    """factory export 이후에만 WS/micro target과 후보 snapshot을 쓴다.

    scan 중간의 hub/pre/strict target write를 제거했으므로, 이 함수가 현재 후보 상태의
    단일 출하 지점이다. target 갱신 뒤 짧게 기다렸다가 최신 WS/micro cache를 다시 입힌
    clean_candidate_snapshot.json을 저장한다.
    """
    strict_rows, shadow_rows = _v212_rows_from_latest()
    if not strict_rows:
        strict_rows = [r for r in strict or [] if isinstance(r, dict)]
    if not shadow_rows:
        shadow_rows = [r for r in shadow or [] if isinstance(r, dict)]
    pending = _v212_snapshot_payload(strict_rows, shadow_rows, stage="pending_before_target", source="factory_latest_rows", wait_sec=0.0)
    save_json(FILES["candidate_snapshot_pending"], pending)
    # target writer는 여기 한 곳만 사용한다. market_rows는 남는 자리를 거래대금/랭크로 채우기 위한 재료다.
    update_ws_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    update_micro_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    wait_sec = max(0.0, min(V212_SNAPSHOT_WAIT_SEC, 3.0))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    final = _v212_write_candidate_snapshot(strict_rows, shadow_rows, stage="final_after_target_overlay", source="factory_latest_rows", wait_sec=wait_sec)
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    return f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / wait {wait_sec:.1f}s / target_writer factory_final_candidates"


def _v212_read_candidate_snapshot() -> Dict[str, Any]:
    obj = load_json(FILES.get("candidate_snapshot", BASE_DIR / "clean_candidate_snapshot.json"), {})
    return obj if isinstance(obj, dict) else {}


def _v212_snapshot_rows() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    snap = _v212_read_candidate_snapshot()
    rows = snap.get("rows") if isinstance(snap.get("rows"), list) else []
    strict = [r for r in rows if isinstance(r, dict)]
    # shadow 원자료는 snapshot에는 count만 두고, 필요 시 latest 파일에서만 제한적으로 읽는다.
    shadow = tail_jsonl(FILES.get("shadow_latest", BASE_DIR / "shadow_candidates_latest.jsonl"), max_lines=V212_SNAPSHOT_READ_MAX)
    return strict, [r for r in shadow if isinstance(r, dict)], snap


def _v210_external_snapshot_dict() -> Dict[str, Any]:  # type: ignore[override]
    """v212: external/status/quality가 같은 clean_candidate_snapshot.json을 보게 한다."""
    snap = _v212_read_candidate_snapshot()
    ext = snap.get("external") if isinstance(snap.get("external"), dict) else {}
    if not snap or not ext:
        return {
            "updated_ts": now_ts(),
            "total": 0,
            "ws_fresh": 0, "ws_stale": 0, "ws_missing": 0, "ws_targeted": 0,
            "micro_fresh": 0, "micro_stale": 0, "micro_missing": 0, "micro_targeted": 0,
            "ws_worker_state": STATE.get("ws_state", "-"), "ws_worker_targets": STATE.get("ws_targets", 0), "ws_worker_fresh": STATE.get("ws_fresh", 0), "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
            "micro_worker_state": STATE.get("micro_state", "-"), "micro_worker_targets": STATE.get("micro_targets", 0), "micro_worker_fresh": STATE.get("micro_fresh", 0),
            "examples": [], "snapshot_missing": True,
        }
    out = dict(ext)
    out.setdefault("updated_ts", snap.get("updated_ts", now_ts()))
    out.setdefault("total", snap.get("candidate_count", len(snap.get("rows") or [])))
    out["snapshot_age_sec"] = round(now_ts() - fnum(snap.get("updated_ts"), now_ts()), 1)
    out["snapshot_scan_id"] = snap.get("scan_id", "-")
    out["snapshot_stage"] = snap.get("stage", "-")
    out["target"] = snap.get("target", {}) if isinstance(snap.get("target"), dict) else {}
    return out


def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    snap = snap or _v210_external_snapshot_dict()
    total = int(snap.get("total", 0) or 0)
    age = snap.get("snapshot_age_sec", "-")
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    lines = [
        "🛰 외부정보 상태 /external_status",
        "",
        "[1/3] 직원 상태",
        f"✅ 웹소켓: {snap.get('ws_worker_state','-')} / 대상 {snap.get('ws_worker_targets',0)} / 신선 {snap.get('ws_worker_fresh',0)} / 최근 {snap.get('ws_last_age_sec','-')}초",
        f"✅ 호가·체결: {snap.get('micro_worker_state','-')} / 대상 {snap.get('micro_worker_targets',0)} / 신선 {snap.get('micro_worker_fresh',0)}",
        "",
        "[2/3] 현재 후보 반영",
        f"- 정식 후보 {total}개 / snapshot {snap.get('snapshot_scan_id','-')} / {age}초 전",
        f"- 웹소켓: 신선 {snap.get('ws_fresh',0)} / 대상 {snap.get('ws_targeted',0)} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {snap.get('micro_fresh',0)} / 대상 {snap.get('micro_targeted',0)} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        f"- 기준: clean_candidate_snapshot.json / writer {target.get('writer','-')} / reason {target.get('reason','-')}",
        "",
        "[3/3] 판독",
    ]
    if total <= 0:
        lines.append("❔ 후보 snapshot 준비중 / old 직접계산 fallback 없음")
    else:
        lines.append("✅ 웹소켓 후보 반영 중" if int(snap.get('ws_fresh',0)) > 0 else "⚠️ 웹소켓 후보 fresh 부족: 차단조건 아님, 우선수집 중")
        lines.append("✅ 호가·체결 후보 반영 중" if int(snap.get('micro_fresh',0)) > 0 else "⚠️ 호가·체결 후보 fresh 부족: target/write 시점 확인")
    ex = snap.get("examples") if isinstance(snap.get("examples"), list) else []
    if ex:
        lines += ["", "예시", *[f"- {x}" for x in ex[:4]]]
    return "\n".join(lines)


def _v210_health_text() -> str:  # type: ignore[override]
    snap = _v210_external_snapshot_dict()
    scan = scan_status_summary()
    paper = read_paper_status().get("status", {})
    warn = external_health_warning_lines({
        "total": snap.get("total", 0),
        "ws_worker_ok": True,
        "micro_worker_ok": True,
        "ws_fresh": snap.get("ws_fresh", 0),
        "micro_fresh": snap.get("micro_fresh", 0),
    })
    lines = [
        "🧭 건강상태 /health",
        "- 메인봇방용 간단 상태판입니다. 재시작/업그레이드는 가드봇 담당.",
        "",
        "[1/5] 메인봇",
        f"{scan.get('icon','❔')} {BOT_VERSION} / scan {scan.get('label','-')} / 단계 {scan.get('stage','-')} / 후보 {snap.get('total',0)}개 / snapshot {snap.get('snapshot_age_sec','-')}초 전",
        f"- 오류: {'✅ 새 실행 중 오류 없음' if not STATE.get('scan_last_error') else '❌ ' + str(STATE.get('scan_last_error'))[:160]}",
        "",
        "[2/5] 서버자원",
        *resource_health_lines(),
        "",
        "[3/5] 외부정보",
        f"✅ WS: {snap.get('ws_worker_state','-')} / 전체신선 {snap.get('ws_worker_fresh',0)} / 후보신선 {snap.get('ws_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('ws_stale',0)} / 없음 {snap.get('ws_missing',0)} / 최근 {snap.get('ws_last_age_sec','-')}초",
        f"✅ 호가·체결: {snap.get('micro_worker_state','-')} / 전체신선 {snap.get('micro_worker_fresh',0)} / 후보신선 {snap.get('micro_fresh',0)}/{snap.get('total',0)} / 오래됨 {snap.get('micro_stale',0)} / 없음 {snap.get('micro_missing',0)}",
        "",
        "[4/5] paper_bot",
        f"✅ {paper.get('version','paper_bot')} / running {paper.get('running','?')} / OPEN {paper.get('open_total', paper.get('open_count','?'))} / CLOSED {paper.get('closed_total','?')} / 상태 {int(now_ts()-fnum(paper.get('updated_ts'), now_ts())) if fnum(paper.get('updated_ts'),0)>0 else '?'}초 전",
        "",
        "[5/5] 판독",
    ]
    if warn:
        lines += warn
    else:
        lines.append("✅ 외부정보 신선도 확인됨")
    return "\n".join(lines)


def _v210_quality_text() -> str:  # type: ignore[override]
    strict, shadow, snap_payload = _v212_snapshot_rows()
    counts = _v210_candidate_counts(strict)
    ext = _v210_external_snapshot_dict()
    closed = _v210_strict_rows(load_closed(V210_QUALITY_CLOSED_TAIL))
    rows3 = _v210_rows_last_hours(closed, 3)
    rows12 = _v210_rows_last_hours(closed, 12)
    cur = _v210_current_version_rows(closed)
    examples_open = [r for r in strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    examples_hold = [r for r in strict if not bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    def ex_line(r: Dict[str, Any]) -> str:
        return f"- {_ticker_from_any(r.get('ticker') or r.get('market') or r.get('symbol'))}: 점수 {fnum(r.get('score'),0):.2f} / 3분돈 {krw_m(r.get('money_flow_3m') or r.get('turnover_3m'))} / 눌림 {fnum(r.get('pullback_quality_score'),0):.2f} / 상대 {r.get('relative_strength_label','-')} / {r.get('final_entry_label') or r.get('quality_label') or '-'}"
    hold = counts.get("hold") if isinstance(counts.get("hold"), Counter) else Counter()
    hold_txt = " / ".join(f"{k} {v}" for k, v in hold.most_common(3)) if hold else "-"
    rel = counts.get("relative") if isinstance(counts.get("relative"), Counter) else Counter()
    rel_txt = " / ".join(f"{k} {v}" for k, v in rel.most_common(4)) if rel else "-"
    age = ext.get("snapshot_age_sec", "-")
    lines = [
        "🔬 후보품질 요약 /quality",
        "- v214 기본은 캐시 전용: 현재 후보는 clean_candidate_snapshot.json 단일 원천만 표시",
        "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full",
        "",
        "[1/5] 성과 요약",
        _compact_stat_line("최근 3시간 정식", rows3),
        _compact_stat_line("최근 12시간 정식", rows12),
        _compact_stat_line("현재버전 정식", cur),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(strict)}개 / 🧪 모의진입 {counts.get('trade_ready',0)}개 / ⚠️ 재확인 {counts.get('recheck',0)}개 / ❌ 진입보류 {max(0, len(strict)-int(counts.get('trade_ready',0)))}개 / 복기 {len(shadow)}개 / snapshot {age}초 전",
        f"- 최종검증: 통과 {counts.get('final_pass',0)} / 재확인 {counts.get('recheck',0)} / 관찰 {counts.get('observe',0)}",
        f"- 웹소켓: 신선 {ext.get('ws_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('ws_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('ws_stale',0)} / 없음 {ext.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {ext.get('micro_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('micro_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('micro_stale',0)} / 없음 {ext.get('micro_missing',0)}",
        f"- 상대강도: {rel_txt}",
        f"- 진입 보류 이유: {hold_txt}",
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
    ]
    lines += [ex_line(r) for r in examples_open] or ["- 없음"]
    lines += ["❌ 진입보류"]
    lines += [ex_line(r) for r in examples_hold] or ["- 없음"]
    lines += ["", "[4/5] 최근 12시간 종료 사유", *_v210_reason_lines(rows12, limit=4)]
    lines += ["", "[5/5] 판독"]
    st12 = score_stats(rows12)
    if st12.get("avg", 0) < 0:
        lines.append("❌ 전체 누적은 아직 자동매매 불가")
    else:
        lines.append("⚠️ 표본 확인 필요")
    lines += ["⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결"]
    return "\n".join(lines)



# ===============================
# v2.13.231: hot_queue 병목 수술
# - 1차선정 안에서 외부 캐시 refresh/file read/file write를 하지 않는다.
# - hot_queue는 매수조건이 아니라 정밀/target 우선순위 재료만 담당한다.
# - 현재 scan에서 이미 읽은 WS/micro 메모리 캐시를 재사용하고, 파일 저장은 target 출하 시점으로 제한한다.
# - 조건/청산/BUY_READY/자동매수 변경 없음
# ===============================
V213_HOT_QUEUE_MAX = int(os.getenv("CLEAN_V213_HOT_QUEUE_MAX", "90"))
V213_HOT_PRECISION_LIMIT = int(os.getenv("CLEAN_V213_HOT_PRECISION_LIMIT", "45"))
V213_HOT_TARGET_LIMIT = int(os.getenv("CLEAN_V213_HOT_TARGET_LIMIT", "90"))
V213_HOT_TTL_SEC = float(os.getenv("CLEAN_V213_HOT_TTL_SEC", "45"))
V213_HOT_MIN_MICRO_BUY_KRW = float(os.getenv("CLEAN_V213_HOT_MIN_MICRO_BUY_KRW", "1200000"))
V213_HOT_MIN_WS_TURNOVER = float(os.getenv("CLEAN_V213_HOT_MIN_WS_TURNOVER", "1500000"))
V231_HOT_BUILD_TTL_SEC = float(os.getenv("CLEAN_V231_HOT_BUILD_TTL_SEC", "1.8"))
V231_HOT_WRITE_TTL_SEC = float(os.getenv("CLEAN_V231_HOT_WRITE_TTL_SEC", "4.0"))
_V231_HOT_CACHE: Dict[str, Any] = {"ts": 0.0, "rows": [], "last_write_ts": 0.0, "mode": "init"}


def _v213_row_ticker(row: Dict[str, Any]) -> str:
    return _ticker_from_any((row or {}).get("ticker") or (row or {}).get("market") or (row or {}).get("symbol"))


def _v213_hot_score_from_micro(row: Dict[str, Any], nowv: float) -> float:
    ts = fnum((row or {}).get("ts") or (row or {}).get("updated_ts"), 0.0)
    age = nowv - ts if ts > 0 else 9999.0
    if age > max(MICRO_STALE_SEC, V213_HOT_TTL_SEC):
        return 0.0
    buy = fnum((row or {}).get("trade_buy_krw_30"), 0.0)
    ratio = fnum((row or {}).get("trade_buy_ratio_30"), 0.0)
    spread = fnum((row or {}).get("micro_spread_pct"), 999.0)
    wall = fnum((row or {}).get("bid_ask_wall_ratio"), 0.0)
    if buy < V213_HOT_MIN_MICRO_BUY_KRW and ratio < 0.56:
        return 0.0
    score = min(buy / 1_000_000.0, 40.0) + max(0.0, ratio - 0.50) * 70.0 + min(max(wall, 0.0), 3.0) * 2.0
    if spread <= 0.25:
        score += 5.0
    elif spread >= 0.60:
        score -= 8.0
    return max(0.0, score)


def _v213_hot_score_from_ws(row: Dict[str, Any], nowv: float) -> float:
    ts = fnum((row or {}).get("ts") or (row or {}).get("updated_ts"), 0.0)
    age = nowv - ts if ts > 0 else 9999.0
    if age > max(WS_HUB_STALE_SEC, V213_HOT_TTL_SEC):
        return 0.0
    turnover = fnum((row or {}).get("ws_turnover") or (row or {}).get("turnover") or (row or {}).get("amount") or (row or {}).get("accTradeValue"), 0.0)
    vol = fnum((row or {}).get("ws_volume") or (row or {}).get("volume") or (row or {}).get("quantity"), 0.0)
    change = abs(fnum((row or {}).get("change_rate") or (row or {}).get("signed_change_rate"), 0.0))
    if turnover < V213_HOT_MIN_WS_TURNOVER and change < 0.20:
        return 0.0
    return min(turnover / 1_000_000.0, 30.0) + min(vol / 1000.0, 10.0) + min(change * 8.0, 20.0)


def _v213_current_snapshot_tickers(limit: int = 80) -> List[str]:
    """v231: 1차선정에서 호출하지 않는다. target 진입 직전 보조용으로만 파일을 읽는다."""
    try:
        snap = load_json(FILES.get("candidate_snapshot", BASE_DIR / "clean_candidate_snapshot.json"), {})
        rows = snap.get("rows") if isinstance(snap, dict) and isinstance(snap.get("rows"), list) else []
        out: List[str] = []
        seen: set = set()
        for r in rows:
            t = _v213_row_ticker(r)
            if t and t not in seen:
                seen.add(t); out.append(t)
            if len(out) >= limit:
                break
        return out
    except Exception:
        return []


def _v231_add_hot_row(merged: Dict[str, Dict[str, Any]], ticker: Any, source: str, score: float, reason: str, nowv: float) -> None:
    t = _ticker_from_any(ticker)
    if not t or t in STABLE_EXCLUDED or score <= 0:
        return
    row = merged.setdefault(t, {"ticker": t, "hot_score": 0.0, "sources": [], "reasons": []})
    row["hot_score"] = max(float(row.get("hot_score") or 0.0), float(score))
    if source not in row["sources"]:
        row["sources"].append(source)
    if reason and reason not in row["reasons"]:
        row["reasons"].append(reason)
    row["updated_ts"] = nowv


def _v213_hot_queue_rows(refresh: bool = False, *, include_file_sources: bool = False, write_file: bool = False, base_rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """v231 single hot_queue builder.

    중요: refresh=True는 더 이상 WS/micro 파일을 강제로 다시 읽지 않는다.
    scan 1차선정에서는 메모리 캐시만 사용한다. 파일 읽기/저장은 factory target 단계에서만 제한적으로 수행한다.
    """
    nowv = now_ts()
    try:
        if (not include_file_sources) and (not write_file) and fnum(_V231_HOT_CACHE.get("ts"), 0.0) > 0:
            if nowv - fnum(_V231_HOT_CACHE.get("ts"), 0.0) <= V231_HOT_BUILD_TTL_SEC:
                return list(_V231_HOT_CACHE.get("rows") or [])
    except Exception:
        pass

    merged: Dict[str, Dict[str, Any]] = {}
    by_base = {_v213_row_ticker(r): r for r in (base_rows or []) if isinstance(r, dict) and _v213_row_ticker(r)}

    # 이미 허브 단계에서 읽힌 메모리 cache만 사용한다. 여기서 refresh_external_ws_cache/refresh_micro_cache 호출 금지.
    try:
        with _micro_lock:
            micro_items = list((_micro_cache or {}).items())
        for t, row in micro_items:
            score = _v213_hot_score_from_micro(row, nowv)
            if score > 0:
                _v231_add_hot_row(merged, t, "micro", score + 10.0, f"micro hot {score:.1f}", nowv)
    except Exception as exc:
        log_error("v231_micro_hot", exc)

    try:
        with _ws_lock:
            ws_items = list((_ws_live_cache or {}).items())
        for t, row in ws_items:
            score = _v213_hot_score_from_ws(row, nowv)
            if score > 0:
                _v231_add_hot_row(merged, t, "ws", score + 6.0, f"WS hot {score:.1f}", nowv)
    except Exception as exc:
        log_error("v231_ws_hot", exc)

    if include_file_sources:
        try:
            for idx, t in enumerate(_v213_current_snapshot_tickers(limit=V213_HOT_TARGET_LIMIT)):
                _v231_add_hot_row(merged, t, "current_candidate", max(1.0, 20.0 - idx * 0.05), "현재 후보 유지", nowv)
        except Exception as exc:
            log_error("v231_current_candidate_hot", exc)
        try:
            for idx, r in enumerate(recent_candidate_priority_rows(limit=V213_HOT_TARGET_LIMIT)):
                _v231_add_hot_row(merged, _v213_row_ticker(r), "recent_candidate", max(1.0, 18.0 - idx * 0.04), "직전 후보 유지", nowv)
        except Exception as exc:
            log_error("v231_recent_candidate_hot", exc)
        try:
            for t in _paper_open_tickers(limit=60):
                _v231_add_hot_row(merged, t, "paper_open", 22.0, "paper OPEN 추적", nowv)
        except Exception as exc:
            log_error("v231_paper_open_hot", exc)

    rows = sorted(merged.values(), key=lambda r: fnum(r.get("hot_score"), 0), reverse=True)[:V213_HOT_QUEUE_MAX]
    _V231_HOT_CACHE.update({"ts": nowv, "rows": rows, "mode": "file_sources" if include_file_sources else "memory_only"})
    with _state_lock:
        STATE["hot_queue_count"] = len(rows)
        STATE["hot_queue_top"] = [r.get("ticker") for r in rows[:8]]
        STATE["hot_queue_mode"] = _V231_HOT_CACHE.get("mode")
        STATE["hot_queue_scan_safe"] = not include_file_sources

    if write_file:
        last_write = fnum(_V231_HOT_CACHE.get("last_write_ts"), 0.0)
        if nowv - last_write >= V231_HOT_WRITE_TTL_SEC:
            payload = {
                "schema": "hot_candidate_queue_v231",
                "version": BOT_VERSION,
                "updated_ts": nowv,
                "updated_at": now_text(nowv),
                "ttl_sec": V213_HOT_TTL_SEC,
                "rows": rows,
                "count": len(rows),
                "mode": _V231_HOT_CACHE.get("mode"),
                "note": "v231: scan 1차선정에서는 외부 refresh/file read/write 금지. target priority only; not a buy condition",
            }
            try:
                save_json(FILES.get("hot_queue", BASE_DIR / "clean_hot_candidate_queue.json"), payload)
                _V231_HOT_CACHE["last_write_ts"] = nowv
            except Exception as exc:
                log_error("v231_save_hot_queue", exc)
    return rows


def _v213_hot_rows_as_market(rows: List[Dict[str, Any]], base_rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    by_t = {_v213_row_ticker(r): r for r in (base_rows or []) if _v213_row_ticker(r)}
    out: List[Dict[str, Any]] = []
    for h in (rows or [])[:limit]:
        t = _v213_row_ticker(h)
        if not t:
            continue
        base = dict(by_t.get(t) or {"ticker": t})
        base["hot_queue"] = True
        base["hot_score"] = fnum(h.get("hot_score"), 0)
        base["hot_sources"] = h.get("sources") or []
        base["hot_reasons"] = h.get("reasons") or []
        out.append(base)
    return out


_v213_base_select_precision_targets = select_precision_targets

def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    base_targets = _v213_base_select_precision_targets(rows)
    # v231: 1차선정에서는 파일/외부 refresh 없이 이미 메모리에 있는 hot cache만 사용한다.
    hot = _v213_hot_queue_rows(refresh=False, include_file_sources=False, write_file=False, base_rows=rows or [])
    hot_market = _v213_hot_rows_as_market(hot, rows or [], V213_HOT_PRECISION_LIMIT)
    by_t: Dict[str, Dict[str, Any]] = {}
    for r in hot_market + base_targets:
        t = _v213_row_ticker(r)
        if t and t not in by_t:
            by_t[t] = r
    out = list(by_t.values())
    try:
        hot_set = {_v213_row_ticker(r) for r in hot_market if _v213_row_ticker(r)}
        with _state_lock:
            STATE["precision_hot_queue_included"] = sum(1 for r in out if _v213_row_ticker(r) in hot_set)
            note = str(STATE.get("precision_target_note") or "")
            STATE["precision_target_note"] = (note + f" / hot_queue {STATE.get('precision_hot_queue_included',0)}(memory)").strip()
    except Exception:
        pass
    return out


_v213_base_precision_priority = precision_priority

def precision_priority(row: Dict[str, Any]) -> float:  # type: ignore[override]
    base = _v213_base_precision_priority(row)
    if bool((row or {}).get("hot_queue")):
        return base - 80000.0 - min(fnum((row or {}).get("hot_score"), 0), 100.0) * 100.0
    return base


_v213_base_update_ws_targets = update_ws_targets
_v213_base_update_micro_targets = update_micro_targets

def _v213_merge_priority(priority_rows: Optional[List[Dict[str, Any]]], base_rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    # target 단계에서는 파일 보조를 허용하지만 TTL 캐시/제한 저장을 쓴다. scan 1차선정과 분리된 경로다.
    hot = _v213_hot_queue_rows(refresh=False, include_file_sources=True, write_file=True, base_rows=base_rows or [])
    hot_market = _v213_hot_rows_as_market(hot, base_rows or [], V213_HOT_TARGET_LIMIT)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for r in list(priority_rows or []) + hot_market:
        t = _v213_row_ticker(r)
        if t and t not in seen:
            seen.add(t); out.append(r)
    return out


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    merged = _v213_merge_priority(priority_rows, rows)
    _v213_base_update_ws_targets(rows, priority_rows=merged, reason=reason)
    try:
        with _state_lock:
            STATE["ws_target_hot_queue"] = max(0, len(merged) - len(priority_rows or []))
    except Exception:
        pass


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    merged = _v213_merge_priority(priority_rows, rows)
    _v213_base_update_micro_targets(rows, priority_rows=merged, reason=reason)
    try:
        with _state_lock:
            STATE["micro_target_hot_queue"] = max(0, len(merged) - len(priority_rows or []))
    except Exception:
        pass


_v213_base_snapshot_payload = _v212_snapshot_payload

def _v212_snapshot_payload(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:  # type: ignore[override]
    payload = _v213_base_snapshot_payload(strict_rows, shadow_rows, stage=stage, source=source, wait_sec=wait_sec)
    try:
        rows = list(_V231_HOT_CACHE.get("rows") or [])
        payload["hot_queue"] = {
            "count": len(rows),
            "top": [str((r or {}).get("ticker")) for r in rows[:8] if (r or {}).get("ticker")],
            "target_ws_added": STATE.get("ws_target_hot_queue", 0),
            "target_micro_added": STATE.get("micro_target_hot_queue", 0),
            "precision_included": STATE.get("precision_hot_queue_included", 0),
            "mode": STATE.get("hot_queue_mode", "memory_only"),
        }
    except Exception:
        payload["hot_queue"] = {"count": 0}
    return payload


_v213_base_external_status_text = _v210_external_status_text

def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v213_base_external_status_text(snap)
    try:
        rows = list(_V231_HOT_CACHE.get("rows") or [])
        if not rows:
            hot = load_json(FILES.get("hot_queue", BASE_DIR / "clean_hot_candidate_queue.json"), {})
            rows = (hot or {}).get("rows") if isinstance(hot, dict) else []
        if isinstance(rows, list):
            top = ", ".join(str((r or {}).get("ticker")) for r in rows[:8] if (r or {}).get("ticker")) or "-"
            txt += f"\n\n[hot queue]\n- WS/micro 빠른감시 후보 {len(rows)}개 / top {top}\n- 용도: 정밀대상·target 우선순위, 매수조건 아님 / mode {STATE.get('hot_queue_mode','-')}"
    except Exception:
        pass
    return txt


# ===============================
# v2.13.214: factory bottleneck surgery + recent outlier summary
# ===============================
# - scan critical path에서는 latest만 저장한다.
# - archive/dup/full context는 기본 scan에서 지연한다.
# - 최근 3시간 성과는 최대수익 1건과 최대수익 제외 합산을 같이 보여 한 방 착시를 확인한다.


def _v214_outlier_brief(rows: List[Dict[str, Any]], label: str = "최근 3시간") -> str:
    arr = list(rows or [])
    if not arr:
        return f"- {label} 최대수익 제외: 기록 없음"
    st = score_stats(arr)
    top = max(arr, key=_pnl)
    top_p = _pnl(top)
    top_sym = str(top.get("symbol") or top.get("ticker") or "?")
    top_reason = label_kr(str(top.get("exit_reason") or "unknown"))
    n2 = max(0, int(st.get("n", 0)) - 1)
    excl_total = fnum(st.get("total"), 0.0) - top_p
    excl_avg = (excl_total / n2) if n2 else 0.0
    icon = "⚠️" if fnum(st.get("total"), 0.0) > 0 and excl_total < 0 else ("✅" if excl_total >= 0 else "❌")
    return f"{icon} {label} 최대수익 제외: 합산 {excl_total:+.2f}% / 평균 {excl_avg:+.2f}% / 최대 {top_sym} {top_p:+.2f}%({top_reason})"


_v214_prev_quality_text_builder = _v210_quality_text

def _v210_quality_text() -> str:  # type: ignore[override]
    strict, shadow, snap_payload = _v212_snapshot_rows()
    counts = _v210_candidate_counts(strict)
    ext = _v210_external_snapshot_dict()
    closed = _v210_strict_rows(load_closed(V210_QUALITY_CLOSED_TAIL))
    rows3 = _v210_rows_last_hours(closed, 3)
    rows12 = _v210_rows_last_hours(closed, 12)
    cur = _v210_current_version_rows(closed)
    examples_open = [r for r in strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    examples_hold = [r for r in strict if not bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    def ex_line(r: Dict[str, Any]) -> str:
        return f"- {_ticker_from_any(r.get('ticker') or r.get('market') or r.get('symbol'))}: 점수 {fnum(r.get('score'),0):.2f} / 3분돈 {krw_m(r.get('money_flow_3m') or r.get('turnover_3m'))} / 눌림 {fnum(r.get('pullback_quality_score'),0):.2f} / 상대 {r.get('relative_strength_label','-')} / {r.get('final_entry_label') or r.get('quality_label') or '-'}"
    hold = counts.get("hold") if isinstance(counts.get("hold"), Counter) else Counter()
    hold_txt = " / ".join(f"{k} {v}" for k, v in hold.most_common(3)) if hold else "-"
    rel = counts.get("relative") if isinstance(counts.get("relative"), Counter) else Counter()
    rel_txt = " / ".join(f"{k} {v}" for k, v in rel.most_common(4)) if rel else "-"
    age = ext.get("snapshot_age_sec", "-")
    lines = [
        "🔬 후보품질 요약 /quality",
        "- v214 기본은 캐시 전용: 현재 후보는 clean_candidate_snapshot.json 단일 원천만 표시",
        "- 최근 3시간은 버전 섞임 가능. 최대수익 제외 줄로 한 방 착시를 같이 봅니다.",
        "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full",
        "",
        "[1/5] 성과 요약",
        _compact_stat_line("최근 3시간 전체 정식(버전 섞임)", rows3),
        _v214_outlier_brief(rows3, "최근 3시간"),
        _compact_stat_line("최근 12시간 전체 정식(버전 섞임)", rows12),
        _compact_stat_line("현재버전 정식", cur),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(strict)}개 / 🧪 모의진입 {counts.get('trade_ready',0)}개 / ⚠️ 재확인 {counts.get('recheck',0)}개 / ❌ 진입보류 {max(0, len(strict)-int(counts.get('trade_ready',0)))}개 / 복기 {len(shadow)}개 / snapshot {age}초 전",
        f"- 최종검증: 통과 {counts.get('final_pass',0)} / 재확인 {counts.get('recheck',0)} / 관찰 {counts.get('observe',0)}",
        f"- 웹소켓: 신선 {ext.get('ws_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('ws_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('ws_stale',0)} / 없음 {ext.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {ext.get('micro_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('micro_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('micro_stale',0)} / 없음 {ext.get('micro_missing',0)}",
        f"- 상대강도: {rel_txt}",
        f"- 진입 보류 이유: {hold_txt}",
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
    ]
    lines += [ex_line(r) for r in examples_open] or ["- 없음"]
    lines += ["❌ 진입보류"]
    lines += [ex_line(r) for r in examples_hold] or ["- 없음"]
    lines += ["", "[4/5] 최근 12시간 종료 사유", *_v210_reason_lines(rows12, limit=4)]
    lines += ["", "[5/5] 판독"]
    st12 = score_stats(rows12)
    if st12.get("avg", 0) < 0:
        lines.append("❌ 전체 누적은 아직 자동매매 불가")
    else:
        lines.append("⚠️ 표본 확인 필요")
    lines += ["⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결"]
    return "\n".join(lines)


def stage_lines() -> List[str]:  # type: ignore[override]
    lines = ["🧩 scan 단계표"]
    arr = STATE.get("stage_times") if isinstance(STATE.get("stage_times"), list) else []
    if not arr:
        return lines + ["- 아직 단계 기록 없음"]
    for item in arr[-10:]:
        try:
            name, sec, note = item
            warn = " ❌" if fnum(sec, 0) >= 20 else (" ⚠️" if fnum(sec, 0) >= 8 else "")
            lines.append(f"- {name}: {fnum(sec,0):.3f}s{warn} / {note}")
        except Exception:
            lines.append(f"- {item}")
    lines.append("- v214: 공장은 latest 먼저 저장, archive/dup/full context는 scan 본선에서 지연")
    return lines


# ===============================
# v2.13.216: 수익확장/조기이탈 분석 표시 보강
# ===============================
# - 진입조건/청산조건은 변경하지 않는다.
# - 최근 3시간 최대수익 1건/TOP3 제외를 함께 보여 한 방 착시를 확인한다.
# - 익절 후보가 더 갈 여지가 있었는지, 손절/지지부진 후보가 초반 반응 없이 끌렸는지
#   CLOSED 진입문맥에 저장된 값만 사용해 가볍게 표시한다.


def _v216_closed_symbol(row: Dict[str, Any]) -> str:
    return str((row or {}).get("symbol") or (row or {}).get("ticker") or (row or {}).get("market") or "?")


def _v216_reason(row: Dict[str, Any]) -> str:
    return str((row or {}).get("exit_reason") or (row or {}).get("close_reason") or (row or {}).get("reason") or "unknown")


def _v216_max_profit_pct(row: Dict[str, Any]) -> float:
    # paper_bot 버전별 키가 다를 수 있어 가능한 키를 넓게 읽는다.
    for k in (
        "max_profit_pct", "best_profit_pct", "highest_profit_pct", "peak_profit_pct",
        "max_unrealized_profit_pct", "highest_unrealized_pct", "high_profit_pct",
    ):
        v = (row or {}).get(k)
        if v is not None:
            return fnum(v, 0.0)
    # highest_price/open_price가 있으면 대략 계산
    hi = fnum((row or {}).get("highest_price") or (row or {}).get("high_price"), 0.0)
    ent = fnum((row or {}).get("entry_price") or (row or {}).get("open_price") or (row or {}).get("buy_price"), 0.0)
    if hi > 0 and ent > 0:
        return (hi / ent - 1.0) * 100.0
    return 0.0


def _v216_hold_min(row: Dict[str, Any]) -> float:
    for k in ("hold_min", "holding_min", "hold_minutes", "duration_min"):
        v = (row or {}).get(k)
        if v is not None:
            return fnum(v, 0.0)
    st = fnum((row or {}).get("entry_ts") or (row or {}).get("open_ts"), 0.0)
    et = fnum((row or {}).get("exit_ts") or (row or {}).get("close_ts"), 0.0)
    if st > 0 and et > st:
        return (et - st) / 60.0
    return 0.0


def _v216_avg(vals: List[float]) -> float:
    vals = [float(v) for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def _v216_outlier_brief(rows: List[Dict[str, Any]], label: str = "최근 3시간") -> List[str]:
    arr = list(rows or [])
    if not arr:
        return [f"- {label} 최대수익 제외: 기록 없음"]
    st = score_stats(arr)
    sorted_rows = sorted(arr, key=_pnl, reverse=True)
    top = sorted_rows[0]
    top_p = _pnl(top)
    top_sym = _v216_closed_symbol(top)
    top_reason = label_kr(_v216_reason(top))
    total = fnum(st.get("total"), 0.0)
    n = int(st.get("n", len(arr)) or len(arr))
    n1 = max(0, n - 1)
    excl1 = total - top_p
    avg1 = excl1 / n1 if n1 else 0.0
    icon1 = "⚠️" if total > 0 and excl1 < 0 else ("✅" if excl1 >= 0 else "❌")
    top3 = sorted_rows[:3]
    top3_sum = sum(_pnl(r) for r in top3)
    n3 = max(0, n - len(top3))
    excl3 = total - top3_sum
    avg3 = excl3 / n3 if n3 else 0.0
    icon3 = "⚠️" if total > 0 and excl3 < 0 else ("✅" if excl3 >= 0 else "❌")
    top3_txt = ", ".join(f"{_v216_closed_symbol(r)} {_pnl(r):+.2f}%" for r in top3)
    return [
        f"{icon1} {label} 최대수익 제외: 합산 {excl1:+.2f}% / 평균 {avg1:+.2f}% / 최대 {top_sym} {top_p:+.2f}%({top_reason})",
        f"{icon3} {label} TOP3 수익 제외: 합산 {excl3:+.2f}% / 평균 {avg3:+.2f}% / TOP3 {top3_txt}",
    ]


def _v216_position_manage_lines(rows: List[Dict[str, Any]], label: str = "최근 3시간") -> List[str]:
    arr = list(rows or [])
    if not arr:
        return [f"- {label} 관리분석: 기록 없음"]
    take = [r for r in arr if _v216_reason(r) in {"take_profit", "tp", "profit", "익절 종료"}]
    stop = [r for r in arr if _v216_reason(r) in {"stop_loss", "손절 종료"}]
    slow = [r for r in arr if _v216_reason(r) in {"slow_no_progress", "time_no_progress", "지지부진 종료"}]
    protect = [r for r in arr if _v216_reason(r) in {"protect_stop_after_tp", "익절 후 보호청산"}]

    def max_avg(sub):
        vals = [_v216_max_profit_pct(r) for r in sub]
        return _v216_avg(vals)
    def pnl_avg(sub):
        vals = [_pnl(r) for r in sub]
        return _v216_avg(vals)
    def no_react(sub, threshold=0.10):
        return sum(1 for r in sub if _v216_max_profit_pct(r) <= threshold)
    def hold_avg(sub):
        vals = [_v216_hold_min(r) for r in sub]
        return _v216_avg(vals)

    lines = [f"[1-1] {label} 수익확장/조기이탈 참고"]
    lines.append(
        f"- 익절: {len(take)}건 / 평균수익 {pnl_avg(take):+.2f}% / 진입후 최고평균 {max_avg(take):+.2f}%"
        + (" → 최고평균이 익절보다 크면 더 먹을 여지 확인" if take else "")
    )
    lines.append(
        f"- 지지부진: {len(slow)}건 / 평균 {pnl_avg(slow):+.2f}% / 최고수익 0.10% 이하 {no_react(slow)}건 / 평균보유 {hold_avg(slow):.1f}분"
        + (" → 초반반응 없으면 조기이탈 후보" if slow else "")
    )
    lines.append(
        f"- 손절: {len(stop)}건 / 평균 {pnl_avg(stop):+.2f}% / 손절 전 최고평균 {max_avg(stop):+.2f}% / 최고수익 0.10% 이하 {no_react(stop)}건"
        + (" → 한 번도 못 튄 손절은 진입품질/초반반응 확인" if stop else "")
    )
    if protect:
        lines.append(f"- 익절 후 보호청산: {len(protect)}건 / 평균 {pnl_avg(protect):+.2f}% / 최고평균 {max_avg(protect):+.2f}% → 보호청산이 수익을 깎는지 확인")
    return lines


def _v216_quality_text() -> str:
    strict, shadow, snap_payload = _v212_snapshot_rows()
    counts = _v210_candidate_counts(strict)
    ext = _v210_external_snapshot_dict()
    closed = _v210_strict_rows(load_closed(V210_QUALITY_CLOSED_TAIL))
    rows3 = _v210_rows_last_hours(closed, 3)
    rows12 = _v210_rows_last_hours(closed, 12)
    cur = _v210_current_version_rows(closed)
    examples_open = [r for r in strict if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]
    examples_hold = [r for r in strict if not bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))][:2]

    def ex_line(r: Dict[str, Any]) -> str:
        return f"- {_ticker_from_any(r.get('ticker') or r.get('market') or r.get('symbol'))}: 점수 {fnum(r.get('score'),0):.2f} / 3분돈 {krw_m(r.get('money_flow_3m') or r.get('turnover_3m'))} / 눌림 {fnum(r.get('pullback_quality_score'),0):.2f} / 상대 {r.get('relative_strength_label','-')} / {r.get('final_entry_label') or r.get('quality_label') or '-'}"

    hold = counts.get("hold") if isinstance(counts.get("hold"), Counter) else Counter()
    hold_txt = " / ".join(f"{k} {v}" for k, v in hold.most_common(3)) if hold else "-"
    rel = counts.get("relative") if isinstance(counts.get("relative"), Counter) else Counter()
    rel_txt = " / ".join(f"{k} {v}" for k, v in rel.most_common(4)) if rel else "-"
    age = ext.get("snapshot_age_sec", "-")
    lines = [
        "🔬 후보품질 요약 /quality",
        "- v231 기본은 캐시 전용: 현재 후보는 clean_candidate_snapshot.json 단일 원천만 표시",
        "- 최근 3시간은 버전 섞임 가능. 최대수익/TOP3 제외로 한 방 착시를 같이 봅니다.",
        "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full",
        "",
        "[1/5] 성과 요약",
        _compact_stat_line("최근 3시간 전체 정식(버전 섞임)", rows3),
    ]
    lines += _v216_outlier_brief(rows3, "최근 3시간")
    lines.append(_compact_stat_line("최근 12시간 전체 정식(버전 섞임)", rows12))
    lines.append(_compact_stat_line("현재버전 정식", cur))
    lines.append(f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}")
    lines += _v229_external_performance_lines(rows3, cur)
    lines += [""] + _v216_position_manage_lines(rows3, "최근 3시간")
    lines += [
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(strict)}개 / 🧪 모의진입 {counts.get('trade_ready',0)}개 / ⚠️ 재확인 {counts.get('recheck',0)}개 / ❌ 진입보류 {max(0, len(strict)-int(counts.get('trade_ready',0)))}개 / 복기 {len(shadow)}개 / snapshot {age}초 전",
        f"- 최종검증: 통과 {counts.get('final_pass',0)} / 재확인 {counts.get('recheck',0)} / 관찰 {counts.get('observe',0)}",
        f"- 웹소켓: 신선 {ext.get('ws_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('ws_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('ws_stale',0)} / 없음 {ext.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {ext.get('micro_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('micro_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('micro_stale',0)} / 없음 {ext.get('micro_missing',0)}",
        f"- 상대강도: {rel_txt}",
        f"- 진입 보류 이유: {hold_txt}",
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
    ]
    lines += [ex_line(r) for r in examples_open] or ["- 없음"]
    lines += ["❌ 진입보류"]
    lines += [ex_line(r) for r in examples_hold] or ["- 없음"]
    lines += ["", "[4/5] 최근 12시간 종료 사유", *_v210_reason_lines(rows12, limit=4)]
    lines += ["", "[5/5] 판독"]
    st12 = score_stats(rows12)
    lines.append("❌ 전체 누적은 아직 자동매매 불가" if st12.get("avg", 0) < 0 else "⚠️ 표본 확인 필요")
    lines += ["⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결", "✅ 다음 판단: micro fresh/stale 성과 차이와 지지부진 초반반응 0% 반복 여부"]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        save_json(FILES["quality_summary"], _cache_payload(_v216_quality_text(), "quality"))
    except Exception as exc:
        log_error("v216_quality_cache", exc)



# ===============================
# v2.13.217: scan 병목 수술 + 분석 필드 스키마 보정
# - 조건/청산/BUY_READY 변경 없음
# - build_candidates 전체 row 외부정보/위험태그 중복 계산 제거
# - WS/micro target은 현재 후보 블록을 hot/recent보다 앞에 고정
# - snapshot pending 저장 제거 및 target/snapshot 단계 경량화
# - paper_bot v0.40 CLOSED 필드 peak_pct / age_min 읽기 보정
# ===============================


def _v217_row_ticker(row: Dict[str, Any]) -> str:
    return _ticker_from_any((row or {}).get("ticker") or (row or {}).get("market") or (row or {}).get("symbol"))


def _v217_priority_key(r: Dict[str, Any]) -> tuple:
    return (
        bool((r or {}).get("paper_bot_open") or (r or {}).get("trade_ready") or (r or {}).get("open_eligible")),
        str((r or {}).get("final_entry_action") or "") in {"paper_open", "trade_ready", "open"},
        str((r or {}).get("final_entry_action") or "") == "recheck_wait",
        fnum((r or {}).get("score"), 0),
        fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0),
        fnum((r or {}).get("turnover_24h"), 0),
    )


def _v217_push_row_ticker(ticks: List[str], seen: set, row: Dict[str, Any]) -> None:
    _push_unique_ticker(ticks, seen, _v217_row_ticker(row))


def _v217_hot_rows_for_target(base_rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    try:
        hot = _v213_hot_queue_rows(refresh=False)
        return _v213_hot_rows_as_market(hot, base_rows or [], V213_HOT_TARGET_LIMIT)
    except Exception:
        return []


def _v217_enrich_candidate_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """strict/shadow 후보에만 외부정보·실전위험·위험태그를 붙인다.

    v216까지는 reject 포함 전체 row에 이 계산을 붙여 scan 3~5차 직원이 무거웠다.
    v217은 후보로 살아남은 row에만 붙인다. 조건값 자체는 바꾸지 않는다.
    """
    try:
        item.update(ws_snapshot(item.get("ticker")))
    except Exception as exc:
        log_error("v217_enrich_ws", exc)
    try:
        item.update(micro_snapshot(item.get("ticker")))
    except Exception as exc:
        log_error("v217_enrich_micro", exc)
    try:
        exec_risk = execution_risk_snapshot(item)
        item.update(exec_risk)
        flags = list(item.get("execution_risk_flags") or [])
        if exec_risk.get("execution_risk_status") == "확인중":
            item["aux_notes"] = list(item.get("aux_notes") or []) + ["실전위험 확인중"]
        elif any("급등" in str(x) or "스프레드" in str(x) or "틱위험" in str(x) for x in flags):
            item["auto_ready_level"] = "paper_ready_risk_check" if bool(item.get("_base_ok")) else item.get("auto_ready_level", "watch_review")
            item["auto_ready_label"] = "실전위험 재확인 필요"
            item["aux_notes"] = list(item.get("aux_notes") or []) + flags[:3]
    except Exception as exc:
        log_error("v217_enrich_exec_risk", exc)
    try:
        item["quality_risk_tags"] = quality_risk_tags(item)
    except Exception as exc:
        log_error("v217_enrich_quality_tags", exc)
        item.setdefault("quality_risk_tags", [])
    item.pop("_base_ok", None)
    return item


def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    """v217: 3~5차 직원 병목 수술.

    전체 453개 row에는 score_row와 최소 분류만 수행한다.
    WS/micro snapshot, execution_risk_snapshot, quality_risk_tags는 strict/shadow로 살아남은 후보에만 붙인다.
    전략 조건/임계값/후보판정 기준은 바꾸지 않는다.
    """
    strict: List[Dict[str, Any]] = []
    shadow: List[Dict[str, Any]] = []
    rejects: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    scored_total = 0
    enriched_total = 0
    for row in rows:
        t = str((row or {}).get("ticker") or "").upper()
        if not t:
            continue
        try:
            prof = score_row(row)
        except Exception as exc:
            log_error("v217_score_row", exc)
            continue
        scored_total += 1
        item = dict(row)
        item.update({
            "ticker": t,
            "strategy": STRATEGY_NAME,
            "strategy_key": STRATEGY_KEY,
            "route": STRATEGY_KEY,
            "score": prof.get("score", 0),
            "edge_score": prof.get("score", 0),
            "current_price": prof.get("price", 0),
            "entry_price": prof.get("price", 0),
            "detected_price": prof.get("price", 0),
            "change_1": prof.get("change_1", 0),
            "change_3": prof.get("change_3", 0),
            "change_5": prof.get("change_5", 0),
            "change_15": prof.get("change_15", 0),
            "change_30": prof.get("change_30", 0),
            "vol_ratio": prof.get("vol_ratio", 0),
            "turnover_1m": prof.get("turnover_1m", 0),
            "turnover_3m": prof.get("turnover_3m", 0),
            "turnover_5m": prof.get("turnover_5m", 0),
            "money_flow_1m": prof.get("money_flow_1m", 0),
            "money_flow_3m": prof.get("money_flow_3m", 0),
            "money_flow_5m": prof.get("money_flow_5m", prof.get("turnover_5m", 0)),
            "turnover": prof.get("turnover_5m", 0),
            "turnover_24h": prof.get("turnover_24h", 0),
            "from_30m_low_pct": prof.get("from_low_pct", 0),
            "below_30m_high_pct": prof.get("high_gap_pct", 0),
            "pullback_depth_pct": prof.get("pullback_depth_pct", 0),
            "low_defense_pct": prof.get("low_defense_pct", 0),
            "recovery_speed_pct": prof.get("recovery_speed_pct", 0),
            "rebreakout_strength": prof.get("rebreakout_strength", 0),
            "fake_bounce_score": prof.get("fake_bounce_score", 0),
            "pullback_quality_score": prof.get("pullback_quality_score", 0),
            "major_watch": bool(prof.get("major_watch")),
            "major_watch_label": "대형주 참고용" if prof.get("major_watch") else "알트 단타 후보",
            "rank_best": prof.get("rank_best", 999),
            "money_flow": prof.get("money_flow", 0),
            "leader_score": prof.get("leader_score", 0),
            "data_age_sec": prof.get("data_age_sec", 999),
            "freshness": prof.get("freshness", "-"),
            "current_close_pos_ratio": prof.get("close_pos", 0),
            "current_upper_wick_pct": prof.get("upper_wick", 0),
            "rsi_14": prof.get("rsi_14", 0),
            "vwap_gap_pct": prof.get("vwap_gap_pct", 0),
            "ma5_gap_pct": prof.get("ma5_gap_pct", 0),
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
            "money_status": prof.get("money_status", "-"),
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
            "one_liner": " / ".join((prof.get("reasons") or [])[:5]) if prof.get("ok") else "차단: " + (" / ".join((prof.get("blocks") or [])[:5]) if prof.get("blocks") else "조건 부족"),
            "profile": prof,
            "_base_ok": bool(prof.get("ok")),
        })
        if item.get("major_watch"):
            item["auto_ready_level"] = "major_watch"
            item["auto_ready_label"] = "대형주 참고용"
            item["aux_notes"] = list(item.get("aux_notes") or []) + ["대형주: trade_ready 제외/시장 참고"]
        if prof.get("ok"):
            strict.append(item)
        else:
            reason = (prof.get("blocks") or ["조건 부족"])[0]
            rejects[reason] += 1
            if len(examples) < 8:
                examples.append({"ticker": t, "reason": reason, "score": prof.get("score", 0), "line": item["one_liner"][:160]})
            if fnum(prof.get("price"), 0) > 0 and (fnum(prof.get("score"), 0) >= 0.75 or fnum(prof.get("turnover_5m"), 0) >= 1_000_000 or fnum(prof.get("vol_ratio"), 0) >= 1.0 or fnum(prof.get("rank_best"), 999) <= 50):
                shadow.append(item)
    strict.sort(key=lambda x: (fnum(x.get("score"), 0), fnum(x.get("turnover_5m"), 0), fnum(x.get("change_5"), 0)), reverse=True)
    shadow.sort(key=lambda x: (fnum(x.get("score"), 0), fnum(x.get("turnover_5m"), 0), -fnum(x.get("rank_best"), 999)), reverse=True)
    # shadow는 복기/관찰용이므로 너무 많은 외부정보 보강을 scan 본선에서 하지 않는다.
    shadow_enrich_limit = int(os.getenv("CLEAN_V217_SHADOW_ENRICH_LIMIT", "80"))
    enrich_rows = strict + shadow[:max(0, shadow_enrich_limit)]
    for item in enrich_rows:
        _v217_enrich_candidate_item(item)
        enriched_total += 1
    try:
        with _state_lock:
            STATE["v217_build_scored_rows"] = scored_total
            STATE["v217_build_enriched_rows"] = enriched_total
            STATE["v217_build_enrich_note"] = f"전체 {scored_total}개 중 strict {len(strict)} + shadow {min(len(shadow), shadow_enrich_limit)}만 외부/위험태그 보강"
    except Exception:
        pass
    return strict, shadow, rejects, examples


def _v217_write_targets_file(path: Path, payload: Dict[str, Any]) -> None:
    atomic_write(path, payload)


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    """v217: 현재 후보 블록을 hot/recent보다 앞에 고정한다."""
    try:
        ticks: List[str] = []
        seen: set = set()
        current = [r for r in (priority_rows or []) if isinstance(r, dict)]
        current_sorted = sorted(current, key=_v217_priority_key, reverse=True)
        previous = sorted(recent_candidate_priority_rows(limit=160), key=_v217_priority_key, reverse=True)
        hot_rows = sorted(_v217_hot_rows_for_target(rows), key=_v217_priority_key, reverse=True)
        blocks = [
            ("current_candidate", current_sorted),
            ("paper_open", [{"ticker": t} for t in _paper_open_tickers(limit=80)]),
            ("recent_candidate", previous),
            ("hot_queue", hot_rows),
            ("scored_fill", sorted(rows or [], key=lambda r: (fnum((r or {}).get("score"), 0), fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0), fnum((r or {}).get("turnover_24h"), 0)), reverse=True)),
            ("major_watch", [{"ticker": t} for t in list(MAJOR_WATCH_TICKERS) + ["BTC", "ETH", "XRP"]]),
            ("turnover_fill", sorted(rows or [], key=lambda r: fnum((r or {}).get("turnover_24h"), 0), reverse=True)),
        ]
        block_counts: Dict[str, int] = {}
        for name, arr in blocks:
            before = len(ticks)
            for r in arr:
                _v217_push_row_ticker(ticks, seen, r)
                if len(ticks) >= max(8, MICRO_TARGET_MAX):
                    break
            block_counts[name] = len(ticks) - before
            if len(ticks) >= max(8, MICRO_TARGET_MAX):
                break
        ticks = ticks[:max(8, MICRO_TARGET_MAX)]
        payload = {
            "version": BOT_VERSION,
            "updated_ts": now_ts(),
            "reason": reason,
            "max_tickers": MICRO_TARGET_MAX,
            "targets": ticks,
            "priority_count": len(current),
            "priority_first": True,
            "block_counts": block_counts,
            "current_candidate_pinned": True,
            "note": "v217: current candidates pinned before paper_open/recent/hot/scored fill; hot queue cannot outrank current candidates",
        }
        _v217_write_targets_file(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), payload)
        with _micro_lock:
            _micro_targets[:] = ticks
        with _state_lock:
            STATE["micro_target_file_targets"] = len(ticks)
            STATE["micro_target_reason"] = reason
            STATE["micro_target_priority_first"] = True
            STATE["micro_target_file_written"] = now_ts()
            STATE["micro_target_block_counts"] = block_counts
    except Exception as exc:
        log_error("update_micro_targets_v217", exc)


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    """v217: WS도 현재 후보 블록을 hot/recent보다 앞에 고정한다.

    v209의 stale/missing urgent rewrite 아이디어는 유지하되, hot queue가 현재 후보를 앞지르지 못하게 한다.
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
        current = [r for r in (priority_rows or []) if isinstance(r, dict)]
        previous = sorted(recent_candidate_priority_rows(limit=200), key=_v217_priority_key, reverse=True)
        hot_rows = sorted(_v217_hot_rows_for_target(rows), key=_v217_priority_key, reverse=True)

        def push_row(row: Dict[str, Any], source: str, priority: int) -> None:
            t = _v217_row_ticker(row)
            if not t or t in seen or t in STABLE_EXCLUDED:
                return
            seen.add(t); ticks.append(t)
            prev = old_meta.get(t, {}) if isinstance(old_meta, dict) else {}
            first_seen = fnum(prev.get("first_seen"), nowv) if isinstance(prev, dict) else nowv
            meta[t] = {"first_seen": first_seen or nowv, "last_seen": nowv, "source": source, "priority": priority}

        block_counts: Dict[str, int] = {}
        blocks = [
            ("current_candidate", sorted(current, key=_v217_priority_key, reverse=True), 130),
            ("paper_open", [{"ticker": t} for t in _paper_open_tickers(limit=90)], 120),
            ("recent_candidate", previous, 100),
            ("hot_queue", hot_rows, 80),
            ("ttl_keep", [{"ticker": t} for t in old_targets], 50),
            ("scored_fill", sorted(rows or [], key=lambda r: (fnum((r or {}).get("score"), 0), fnum((r or {}).get("money_flow_3m") or (r or {}).get("turnover_3m"), 0), fnum((r or {}).get("turnover_24h"), 0)), reverse=True), 30),
            ("major_watch", [{"ticker": t} for t in list(MAJOR_WATCH_TICKERS) + ["BTC", "ETH", "XRP"]], 10),
        ]
        for name, arr, pr in blocks:
            before = len(ticks)
            for r in arr:
                if name == "ttl_keep":
                    t = _v217_row_ticker(r)
                    prev = old_meta.get(t, {}) if isinstance(old_meta, dict) else {}
                    last_seen = fnum(prev.get("last_seen") or old_updated, old_updated) if isinstance(prev, dict) else old_updated
                    if not (last_seen > 0 and nowv - last_seen <= WS_TARGET_KEEP_TTL_SEC):
                        continue
                push_row(r, name, pr)
                if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                    break
            block_counts[name] = len(ticks) - before
            if len(ticks) >= max(10, WS_HUB_MAX_TICKERS):
                break
        ticks = ticks[:max(10, WS_HUB_MAX_TICKERS)]
        meta = {t: meta.get(t, {"first_seen": nowv, "last_seen": nowv, "source": "unknown", "priority": 0}) for t in ticks}
        changed, added, removed = _ws_target_changed_enough(old_targets, ticks)
        current_missing_or_stale = 0
        for r in current[:80]:
            try:
                ws = ws_snapshot(_v217_row_ticker(r))
                st = str(ws.get("ws_row_status") or "missing")
                age = fnum(ws.get("ws_age_sec", ws.get("live_age_sec", -1)), -1)
                if st != "fresh" and not (0 <= age <= WS_HUB_STALE_SEC):
                    current_missing_or_stale += 1
            except Exception:
                pass
        since_write = nowv - old_updated if old_updated > 0 else 9999.0
        urgent_due = bool(set(ticks[:min(80, len(ticks))]) - set(old_targets)) and since_write >= 0.3
        freshness_due = len(current) >= 5 and current_missing_or_stale / max(1, len(current[:80])) >= WS_CANDIDATE_URGENT_MISSING_RATIO and since_write >= 0.8
        routine_due = changed and since_write >= WS_TARGET_REWRITE_MIN_SEC
        should_write = not path.exists() or not old_targets or urgent_due or freshness_due or routine_due
        write_note = "write" if should_write else "skip_same_or_debounce"
        payload = {
            "version": BOT_VERSION,
            "updated_ts": nowv if should_write else old_updated,
            "reason": reason,
            "max_tickers": WS_HUB_MAX_TICKERS,
            "targets": ticks,
            "target_meta": meta,
            "priority_count": len(current),
            "block_counts": block_counts,
            "current_candidate_pinned": True,
            "paper_open_count": len(_paper_open_tickers(limit=90)),
            "priority_first": True,
            "changed": changed,
            "added": added,
            "removed": removed,
            "candidate_missing_or_stale": current_missing_or_stale,
            "candidate_checked": len(current[:80]),
            "freshness_due": freshness_due,
            "write_note": write_note,
            "note": "v217: current candidates pinned before paper_open/recent/hot; WS fresh is a signal, not a hard entry block",
        }
        if should_write:
            atomic_write(path, payload)
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
            STATE["ws_target_candidate_missing_ratio"] = round(current_missing_or_stale / max(1, len(current[:80])), 3) if current else 0.0
            STATE["ws_target_freshness_due"] = freshness_due
            STATE["ws_target_block_counts"] = block_counts
    except Exception as exc:
        log_error("update_ws_targets_v217", exc)


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v217: pending snapshot 저장을 제거하고 factory 이후 최종 target/snapshot 1회만 수행한다."""
    strict_rows, shadow_rows = _v212_rows_from_latest()
    if not strict_rows:
        strict_rows = [r for r in strict or [] if isinstance(r, dict)]
    if not shadow_rows:
        shadow_rows = [r for r in shadow or [] if isinstance(r, dict)]
    update_ws_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    update_micro_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    wait_sec = max(0.0, min(float(os.getenv("CLEAN_V217_SNAPSHOT_WAIT_SEC", str(V212_SNAPSHOT_WAIT_SEC))), 2.0))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    final = _v212_write_candidate_snapshot(strict_rows, shadow_rows, stage="final_after_target_overlay", source="factory_latest_rows_v217_no_pending", wait_sec=wait_sec)
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    with _state_lock:
        STATE["v217_snapshot_no_pending"] = True
    return f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / wait {wait_sec:.1f}s / target_writer factory_final_candidates / no_pending"


def _v216_max_profit_pct(row: Dict[str, Any]) -> float:  # type: ignore[override]
    # paper_bot v0.40 CLOSED 저장키: peak_pct
    for k in (
        "peak_pct", "max_profit_pct", "best_profit_pct", "highest_profit_pct", "peak_profit_pct",
        "max_unrealized_profit_pct", "highest_unrealized_pct", "high_profit_pct",
    ):
        v = (row or {}).get(k)
        if v is not None:
            return fnum(v, 0.0)
    raw = (row or {}).get("raw") if isinstance((row or {}).get("raw"), dict) else {}
    for k in ("peak_pct", "max_profit_pct", "best_profit_pct", "highest_profit_pct", "peak_profit_pct"):
        v = raw.get(k)
        if v is not None:
            return fnum(v, 0.0)
    hi = fnum((row or {}).get("highest_price") or (row or {}).get("high_price"), 0.0)
    ent = fnum((row or {}).get("entry_price") or (row or {}).get("open_price") or (row or {}).get("buy_price"), 0.0)
    if hi > 0 and ent > 0:
        return (hi / ent - 1.0) * 100.0
    return 0.0


def _v216_hold_min(row: Dict[str, Any]) -> float:  # type: ignore[override]
    # paper_bot v0.40 CLOSED 저장키: age_min
    for k in ("age_min", "hold_min", "holding_min", "hold_minutes", "duration_min"):
        v = (row or {}).get(k)
        if v is not None:
            return fnum(v, 0.0)
    raw = (row or {}).get("raw") if isinstance((row or {}).get("raw"), dict) else {}
    for k in ("age_min", "hold_min", "holding_min", "hold_minutes", "duration_min"):
        v = raw.get(k)
        if v is not None:
            return fnum(v, 0.0)
    st = fnum((row or {}).get("entry_ts") or (row or {}).get("open_ts"), 0.0)
    et = fnum((row or {}).get("exit_ts") or (row or {}).get("close_ts"), 0.0)
    if st > 0 and et > st:
        return (et - st) / 60.0
    return 0.0




# ===============================
# v2.13.229: micro fresh 관찰 반영 공용 helper
# ===============================
# 목적:
# - micro sidecar가 만든 fresh 값을 후보 snapshot/paper 최신파일/성과분석까지 같은 기준으로 흘린다.
# - urgent 파일은 수집 요청/표시 보조이며, fresh 판정 원천은 clean_bithumb_micro_cache.json 한 곳만 사용한다.
# - 조건/청산/BUY_READY/자동매수 변경 없음.

def _v229_micro_urgent_payload() -> Dict[str, Any]:
    try:
        path = globals().get("MICRO_URGENT_TARGET_FILE", BASE_DIR / "clean_micro_urgent_targets.json")
        obj = load_json(path, {})
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _v229_apply_urgent_marker(row: Dict[str, Any], maps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """현재 row가 micro urgent 수집요청 대상이었는지 표시만 보강한다.

    이 값은 매수/차단 조건이 아니라 snapshot과 paper 성과분석용 진입 당시 문맥이다.
    """
    rr = dict(row or {})
    try:
        t = _ticker_from_any(rr.get("ticker") or rr.get("market") or rr.get("symbol"))
        if not t:
            return rr
        payload = (maps or {}).get("micro_urgent_payload") if isinstance(maps, dict) else None
        if not isinstance(payload, dict):
            payload = _v229_micro_urgent_payload()
        targets = [_ticker_from_any(x) for x in (payload.get("targets") or []) if _ticker_from_any(x)] if isinstance(payload, dict) else []
        meta = payload.get("target_meta") if isinstance(payload.get("target_meta"), dict) else {}
        m = meta.get(t) if isinstance(meta, dict) else None
        rr["micro_urgent_requested"] = bool(t in set(targets))
        rr["micro_urgent_updated_ts"] = fnum(payload.get("updated_ts"), 0.0) if isinstance(payload, dict) else 0.0
        rr["micro_urgent_age_sec"] = round(max(0.0, now_ts() - fnum(payload.get("updated_ts"), now_ts())), 2) if isinstance(payload, dict) and fnum(payload.get("updated_ts"), 0.0) > 0 else -1
        if isinstance(m, dict):
            rr["micro_urgent_source"] = str(m.get("source") or "-")[:80]
            rr["micro_urgent_status_at_request"] = str(m.get("status") or "missing")
            rr["micro_urgent_age_sec_at_request"] = m.get("age_sec", -1)
            rr["micro_urgent_label"] = str(m.get("label") or "")[:100]
        else:
            rr.setdefault("micro_urgent_source", "-")
            rr.setdefault("micro_urgent_status_at_request", "-")
        ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
        if ctx:
            for k in ("micro_urgent_requested", "micro_urgent_updated_ts", "micro_urgent_age_sec", "micro_urgent_source", "micro_urgent_status_at_request", "micro_urgent_age_sec_at_request", "micro_urgent_label"):
                ctx[k] = rr.get(k)
            rr["entry_context"] = ctx
    except Exception as exc:
        log_error("v229_apply_urgent_marker", exc)
    return rr


def _v229_entry_bucket_from_ctx(row: Dict[str, Any], kind: str) -> str:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else row
    if kind == "micro":
        if bool(ctx.get("micro_fresh")) or str(ctx.get("micro_row_status") or "").lower() == "fresh":
            return "fresh"
        st = str(ctx.get("micro_row_status") or "").lower()
        if st in {"stale", "old", "오래됨"}:
            return "stale"
        return "missing"
    if kind == "ws":
        if bool(ctx.get("ws_fresh")) or str(ctx.get("ws_row_status") or "").lower() == "fresh":
            return "fresh"
        st = str(ctx.get("ws_row_status") or "").lower()
        if st in {"stale", "old", "오래됨"}:
            return "stale"
        return "missing"
    if kind == "urgent":
        if bool(ctx.get("micro_urgent_requested") or ctx.get("urgent_recheck_used")):
            if bool(ctx.get("micro_fresh")) or str(ctx.get("micro_row_status") or "").lower() == "fresh":
                return "urgent_fresh"
            return "urgent_not_fresh"
        return "not_urgent"
    return "unknown"


def _v229_compact_perf_line(label: str, rows: List[Dict[str, Any]]) -> str:
    st = score_stats(rows)
    icon = "❔" if st.get("n", 0) <= 0 else ("⚠️" if st.get("n", 0) < 20 else ("✅" if fnum(st.get("avg"), 0.0) > 0 else "❌"))
    note = " / 표본적음" if 0 < int(st.get("n", 0) or 0) < 20 else ""
    return f"{icon} {label}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%{note}"


def _v229_external_performance_lines(rows3: List[Dict[str, Any]], cur: List[Dict[str, Any]]) -> List[str]:
    """CLOSED 진입문맥에 저장된 외부정보 상태별 성과를 짧게 표시한다.

    새 판단조건이 아니라, micro/WS 반영 지연과 후보품질 문제를 분리하기 위한 관찰용이다.
    """
    base = list(cur or []) or list(rows3 or [])
    if not base:
        return ["", "[1-2] 외부정보별 성과", "- 아직 CLOSED 표본 없음"]
    micro_fresh = [r for r in base if _v229_entry_bucket_from_ctx(r, "micro") == "fresh"]
    micro_stale = [r for r in base if _v229_entry_bucket_from_ctx(r, "micro") in {"stale", "missing"}]
    ws_fresh = [r for r in base if _v229_entry_bucket_from_ctx(r, "ws") == "fresh"]
    ws_not = [r for r in base if _v229_entry_bucket_from_ctx(r, "ws") in {"stale", "missing"}]
    urgent_fresh = [r for r in base if _v229_entry_bucket_from_ctx(r, "urgent") == "urgent_fresh"]
    urgent_not = [r for r in base if _v229_entry_bucket_from_ctx(r, "urgent") == "urgent_not_fresh"]
    label = "현재버전" if cur else "최근 3시간"
    return [
        "",
        f"[1-2] 외부정보별 성과({label}, 조건판단 아님)",
        _v229_compact_perf_line("micro fresh 진입", micro_fresh),
        _v229_compact_perf_line("micro stale/missing 진입", micro_stale),
        _v229_compact_perf_line("WS fresh 진입", ws_fresh),
        _v229_compact_perf_line("WS stale/missing 진입", ws_not),
        _v229_compact_perf_line("urgent 확인 후 fresh 진입", urgent_fresh),
        _v229_compact_perf_line("urgent 요청 후 미신선 진입", urgent_not),
    ]

# ===============================
# v2.13.218: 스캔 본선 재배치 1차
# - 스캔은 전체시장 발견 본선 유지
# - WS/micro는 현재 후보 검증 캐시로만 가볍게 overlay
# - hot queue는 보조 레이더이며 현재 후보보다 앞서지 않음
# - 조건/청산/BUY_READY 변경 없음
# ===============================

# shadow 복기 후보에 외부/위험태그를 과하게 붙이면 3~5차 직원이 다시 무거워진다.
# 환경값이 없을 때만 기본 40개로 낮춘다. strict 후보는 전부 보강한다.
os.environ.setdefault("CLEAN_V217_SHADOW_ENRICH_LIMIT", "40")
os.environ.setdefault("CLEAN_V218_SNAPSHOT_WAIT_SEC", "0.6")

_V218_EXT_CACHE: Dict[str, Any] = {"ts": 0.0, "ws": {}, "micro": {}, "ws_targets": set(), "micro_targets": set()}


def _v218_refresh_external_maps(*, force: bool = False, ttl: float = 0.8) -> Dict[str, Any]:
    """v225: WS/micro maps reader. micro part is same single source as health/external/quality."""
    nowv = now_ts()
    try:
        if (not force) and fnum(_V218_EXT_CACHE.get("ts"), 0.0) > 0 and nowv - fnum(_V218_EXT_CACHE.get("ts"), 0.0) <= ttl:
            return _V218_EXT_CACHE
    except Exception:
        pass
    try:
        refresh_external_ws_cache()
    except Exception as exc:
        log_error("v225_refresh_ws_maps", exc)
    try:
        refresh_micro_cache()
    except Exception as exc:
        log_error("v225_refresh_micro_maps", exc)
    with _ws_lock:
        ws_cache = {str(k).upper(): dict(v) for k, v in (_ws_live_cache or {}).items() if k and isinstance(v, dict)}
        ws_targets = set(_ticker_from_any(x) for x in (_ws_targets or []) if _ticker_from_any(x))
    micro_src = _v225_load_micro_source()
    micro_cache = micro_src.get("rows") if isinstance(micro_src.get("rows"), dict) else {}
    micro_targets = micro_src.get("targets") if isinstance(micro_src.get("targets"), set) else set()
    micro_urgent_payload = _v229_micro_urgent_payload()
    _V218_EXT_CACHE.update({
        "ts": nowv,
        "ws": ws_cache,
        "micro": micro_cache,
        "ws_targets": ws_targets,
        "micro_targets": micro_targets,
        "micro_source": micro_src,
        "micro_urgent_payload": micro_urgent_payload,
    })
    return _V218_EXT_CACHE


def _v218_ext_maps(*, force: bool = False, ttl: float = 0.8) -> Dict[str, Any]:
    return _v218_refresh_external_maps(force=force, ttl=ttl)

def _v218_ws_snapshot_from_maps(ticker: Any, maps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    t = _ticker_from_any(ticker)
    maps = maps or _v218_refresh_external_maps()
    nowv = now_ts()
    row = dict((maps.get("ws") or {}).get(t) or {})
    target_set = maps.get("ws_targets") or set()
    if not row:
        return {"live_price_source": "REST", "live_price": 0.0, "live_age_sec": -1, "ws_age_sec": -1, "ws_fresh": False, "ws_row_status": "missing", "ws_targeted": t in target_set, "ws_cache_ts": 0.0}
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


def _v218_micro_snapshot_from_maps(ticker: Any, maps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    maps = maps or _v218_refresh_external_maps()
    src = maps.get("micro_source") if isinstance(maps.get("micro_source"), dict) else None
    if src is None:
        src = _v225_load_micro_source()
    return _v225_micro_snapshot_from_source(ticker, src)


def _v217_enrich_candidate_item(item: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v218: 후보 보강은 현재 캐시맵 1회 재사용으로 처리한다.

    거절 후보 전체가 아니라 strict + shadow 제한분에만 호출되는 v217 구조를 유지한다.
    여기서는 외부정보/상대강도/실전위험/위험태그를 붙이되 조건값 자체는 바꾸지 않는다.
    """
    maps = _v218_refresh_external_maps()
    try:
        item.update(_v218_ws_snapshot_from_maps(item.get("ticker"), maps))
    except Exception as exc:
        log_error("v218_enrich_ws", exc)
    try:
        item.update(_v218_micro_snapshot_from_maps(item.get("ticker"), maps))
    except Exception as exc:
        log_error("v218_enrich_micro", exc)
    try:
        item = _apply_relative_strength_context(item)
    except Exception as exc:
        log_error("v218_enrich_relative", exc)
    try:
        exec_risk = execution_risk_snapshot(item)
        item.update(exec_risk)
        flags = list(item.get("execution_risk_flags") or [])
        if exec_risk.get("execution_risk_status") == "확인중":
            item["aux_notes"] = list(item.get("aux_notes") or []) + ["실전위험 확인중"]
        elif any("급등" in str(x) or "스프레드" in str(x) or "틱위험" in str(x) for x in flags):
            item["auto_ready_level"] = "paper_ready_risk_check" if bool(item.get("_base_ok")) else item.get("auto_ready_level", "watch_review")
            item["auto_ready_label"] = "실전위험 재확인 필요"
            item["aux_notes"] = list(item.get("aux_notes") or []) + flags[:3]
    except Exception as exc:
        log_error("v218_enrich_exec_risk", exc)
    try:
        item["quality_risk_tags"] = quality_risk_tags(item)
    except Exception as exc:
        log_error("v218_enrich_quality_tags", exc)
        item.setdefault("quality_risk_tags", [])
    item.pop("_base_ok", None)
    return item


def _v212_overlay_row(row: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v218: snapshot overlay는 WS/micro 캐시를 row마다 새로 읽지 않는다."""
    rr = dict(row or {})
    t = _v212_ticker(rr)
    maps = _v218_refresh_external_maps()
    ws = _v218_ws_snapshot_from_maps(t, maps)
    ms = _v218_micro_snapshot_from_maps(t, maps)
    rr.update(ws)
    rr.update(ms)
    rr["ticker"] = t or rr.get("ticker")
    rr["snapshot_overlay_ts"] = now_ts()
    try:
        rest_price = fnum(rr.get("current_price") or rr.get("entry_price") or rr.get("detected_price"), 0.0)
        live = fnum(ws.get("live_price"), 0.0)
        if rest_price > 0 and live > 0:
            rr["ws_price"] = live
            rr["current_price_ws_gap_pct"] = round(((live - rest_price) / rest_price) * 100.0, 3)
    except Exception:
        pass
    try:
        rr = _apply_relative_strength_context(rr)
    except Exception as exc:
        log_error("v218_snapshot_relative", exc)
    try:
        rr = _v229_apply_urgent_marker(rr, maps)
    except Exception as exc:
        log_error("v229_snapshot_urgent_marker", exc)
    return rr


def _v212_snapshot_payload(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:  # type: ignore[override]
    maps = _v218_refresh_external_maps(force=True)
    ordered = _v212_order_candidates(strict_rows)
    rows = [_v212_overlay_row(r) for r in ordered]
    ext = _v212_count_external(rows)
    nowv = now_ts()
    return {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v218",
        "stage": stage,
        "source": source,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or "-"),
        "scan_seq": int(fnum(STATE.get("scan_seq"), 0)),
        "candidate_count": len(rows),
        "shadow_count": len(shadow_rows or []),
        "rows": rows,
        "external": {
            **ext,
            "ws_worker_state": STATE.get("ws_state", "-"),
            "ws_worker_targets": STATE.get("ws_targets", 0),
            "ws_worker_fresh": STATE.get("ws_fresh", 0),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
            "micro_worker_state": STATE.get("micro_state", "-"),
            "micro_worker_targets": STATE.get("micro_targets", 0),
            "micro_worker_fresh": STATE.get("micro_fresh", 0),
            "overlay_mode": "v218_one_cache_read",
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2),
        },
        "target": {
            "writer": "factory_export_final_only",
            "reason": "factory_final_candidates",
            "ws_target_count": STATE.get("ws_target_file_targets", 0),
            "micro_target_count": STATE.get("micro_target_file_targets", 0),
            "ws_target_reason": STATE.get("ws_target_reason", "-"),
            "micro_target_reason": STATE.get("micro_target_reason", "-"),
            "wait_sec": wait_sec,
            "priority_rule": "scan_candidates_first_then_paper_open_recent_hot",
        },
        "note": "v218 scan-first mainline; WS/micro validate current scan candidates; commands read this snapshot only",
    }


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v218: 스캔 후보 확정 뒤 target 1회 + 짧은 검증 대기 + 단일 snapshot 저장.

    스캔은 전체시장 발견 본선이다. WS/micro는 현재 후보 검증 재료이며 hot queue는 보조 레이더다.
    """
    strict_rows, shadow_rows = _v212_rows_from_latest()
    if not strict_rows:
        strict_rows = [r for r in strict or [] if isinstance(r, dict)]
    if not shadow_rows:
        shadow_rows = [r for r in shadow or [] if isinstance(r, dict)]
    # target writer는 factory 이후 이 한 곳만 사용한다.
    update_ws_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    update_micro_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    wait_sec = max(0.0, min(float(os.getenv("CLEAN_V218_SNAPSHOT_WAIT_SEC", "0.6")), 1.5))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    final = _v212_write_candidate_snapshot(strict_rows, shadow_rows, stage="final_after_target_overlay", source="factory_latest_rows_v218_scan_first", wait_sec=wait_sec)
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    with _state_lock:
        STATE["v218_scan_first_mainline"] = True
        STATE["v218_snapshot_overlay_mode"] = "one_cache_read"
        STATE["v218_shadow_enrich_limit"] = int(os.getenv("CLEAN_V217_SHADOW_ENRICH_LIMIT", "40"))
    return f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / wait {wait_sec:.1f}s / target_writer factory_final_candidates / v218 scan-first"


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    txt = _read_cached_text(FILES["quality_summary"], "/quality")
    return txt.replace("v216 기본은 캐시 전용", "v222 기본은 캐시 전용").replace("v214 기본은 캐시 전용", "v222 기본은 캐시 전용")



# ===============================
# v2.13.219: scan은 기다리지 않고, scan 후 외부정보만 재overlay
# ===============================
# 목적:
# - v218에서 scan 21초대까지 줄인 구조는 유지한다.
# - WS/micro fresh를 높이려고 scan 본선을 다시 기다리게 하지 않는다.
# - factory 출하 직후 target을 던지고, 별도 저부하 refresh가 같은 snapshot에 WS/micro만 다시 입힌다.

V219_EXTERNAL_REFRESH_DELAY_SEC = float(os.getenv("CLEAN_V219_EXTERNAL_REFRESH_DELAY_SEC", "3.0"))
V219_EXTERNAL_REFRESH_MIN_GAP_SEC = float(os.getenv("CLEAN_V219_EXTERNAL_REFRESH_MIN_GAP_SEC", "2.0"))
_V219_REFRESH_LOCK = threading.Lock()
_V219_REFRESH_LAST_START_TS = 0.0
_V219_REFRESH_LAST_SCAN_ID = ""


def _v219_status_label_reason(row: Dict[str, Any]) -> str:
    """snapshot 표시용 보류/판정 이유를 기존 후보 row에서만 뽑는다.

    조건을 새로 계산하지 않는다. 기존 필드의 표시 누락만 보강한다.
    """
    r = row or {}
    candidates = [
        r.get("final_entry_label"),
        r.get("auto_ready_label"),
        r.get("quality_label"),
        r.get("block_reason"),
        r.get("reject_reason"),
        r.get("hold_reason"),
    ]
    for x in candidates:
        s = str(x or "").strip()
        if s and s not in {"-", "None", "null"}:
            return s[:80]
    for key in ("final_entry_reasons", "block_reasons", "execution_risk_flags", "quality_risk_tags", "aux_notes"):
        vals = r.get(key)
        if isinstance(vals, list) and vals:
            ss = [str(v).strip() for v in vals if str(v).strip()]
            if ss:
                return " / ".join(ss[:3])[:100]
    prof = r.get("profile") if isinstance(r.get("profile"), dict) else {}
    vals = prof.get("blocks") or prof.get("reasons") or []
    if isinstance(vals, list) and vals:
        ss = [str(v).strip() for v in vals if str(v).strip()]
        if ss:
            return " / ".join(ss[:3])[:100]
    return "-"


def _v219_overlay_row_with_maps(row: Dict[str, Any], maps: Dict[str, Any]) -> Dict[str, Any]:
    rr = dict(row or {})
    t = _v212_ticker(rr)
    try:
        rr.update(_v218_ws_snapshot_from_maps(t, maps))
    except Exception as exc:
        log_error("v219_overlay_ws", exc)
    try:
        rr.update(_v218_micro_snapshot_from_maps(t, maps))
    except Exception as exc:
        log_error("v219_overlay_micro", exc)
    rr["ticker"] = t or rr.get("ticker")
    rr["snapshot_overlay_ts"] = now_ts()
    rr["external_refreshed_by"] = "v229_post_scan_multi_refresh"
    # 표시 필드 누락 보강. 조건/판정은 바꾸지 않는다.
    is_open_grade = bool(rr.get("paper_bot_open") or rr.get("trade_ready") or rr.get("open_eligible"))
    label_reason = _v219_status_label_reason(rr)
    if not rr.get("final_entry_label"):
        rr["final_entry_label"] = label_reason
    # v229: 후보 성격(모의진입 가능)과 진입보류 사유를 분리한다.
    # 모의진입 가능 후보에 "모의매매 검증 후보" 같은 문구를 hold_reason으로 박지 않는다.
    rr["candidate_grade_label"] = "🧪 모의진입 가능" if is_open_grade else ("⚠️ 실전위험 재확인" if "재확인" in label_reason or "실전위험" in label_reason else "❌ 진입보류")
    if not rr.get("hold_reason") and not is_open_grade:
        rr["hold_reason"] = label_reason
    elif is_open_grade and str(rr.get("hold_reason") or "").strip() in {"", "-", "모의매매 검증 후보"}:
        rr["hold_reason"] = "-"
    try:
        rest_price = fnum(rr.get("current_price") or rr.get("entry_price") or rr.get("detected_price"), 0.0)
        live = fnum(rr.get("live_price"), 0.0)
        if rest_price > 0 and live > 0:
            rr["ws_price"] = live
            rr["current_price_ws_gap_pct"] = round(((live - rest_price) / rest_price) * 100.0, 3)
    except Exception:
        pass
    try:
        rr = _apply_relative_strength_context(rr)
    except Exception as exc:
        log_error("v219_overlay_relative", exc)
    try:
        rr = _v229_apply_urgent_marker(rr, maps)
    except Exception as exc:
        log_error("v229_refresh_urgent_marker", exc)
    return rr


def _v219_write_command_caches_after_snapshot(build_quality: bool = False) -> None:
    """refresh된 snapshot을 명령어 캐시에 반영한다.

    v230: v229에서 매 refresh마다 구 _v210_quality_text()를 저장해 /quality가
    외부정보별 성과/후보문구 정리를 잃고, refresh thread도 불필요하게 무거워졌다.
    light 캐시는 유지하되 quality는 마지막 refresh 또는 필요 시에만 현재 builder(_v216_quality_text)를 쓴다.
    """
    try:
        ext_snap = _v210_external_snapshot_dict()
        save_json(FILES["external_snapshot"], _cache_payload(_v210_external_status_text(ext_snap), "external_status"))
        save_json(FILES["health_snapshot"], _cache_payload(_v210_health_text(), "health"))
    except Exception as exc:
        log_error("v230_refresh_light_cache", exc)
    if build_quality:
        try:
            save_json(FILES["quality_summary"], _cache_payload(_v216_quality_text(), "quality"))
        except Exception as exc:
            log_error("v230_refresh_quality_cache", exc)


V229_EXTERNAL_REFRESH_INTERVALS_SEC = [float(x) for x in os.getenv("CLEAN_V229_EXTERNAL_REFRESH_INTERVALS_SEC", "1.2,2.2,3.2").split(",") if str(x).strip()]


def _v229_refresh_candidate_snapshot_once(expected_scan_id: str = "", *, attempt: int = 1, total_attempts: int = 1, waited_sec: float = 0.0) -> Tuple[bool, Dict[str, Any]]:
    snap = _v212_read_candidate_snapshot()
    if not isinstance(snap, dict) or not snap.get("rows"):
        return False, {}
    scan_id = str(snap.get("scan_id") or "")
    if expected_scan_id and scan_id and scan_id != expected_scan_id:
        return False, {}
    rows0 = [r for r in (snap.get("rows") or []) if isinstance(r, dict)]
    maps = _v218_refresh_external_maps(force=True, ttl=0.0)
    rows = [_v219_overlay_row_with_maps(r, maps) for r in rows0]
    ext = _v212_count_external(rows)
    nowv = now_ts()
    old_ext = snap.get("external") if isinstance(snap.get("external"), dict) else {}
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    refreshed = dict(snap)
    refreshed.update({
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v229",
        "stage": "external_refreshed_after_scan",
        "source": "v229_post_scan_multi_external_refresh",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "candidate_count": len(rows),
        "rows": rows,
        "external": {
            **old_ext,
            **ext,
            "ws_worker_state": STATE.get("ws_state", old_ext.get("ws_worker_state", "-")),
            "ws_worker_targets": STATE.get("ws_targets", old_ext.get("ws_worker_targets", 0)),
            "ws_worker_fresh": STATE.get("ws_fresh", old_ext.get("ws_worker_fresh", 0)),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", old_ext.get("ws_last_age_sec", -1)),
            "micro_worker_state": STATE.get("micro_state", old_ext.get("micro_worker_state", "-")),
            "micro_worker_targets": STATE.get("micro_targets", old_ext.get("micro_worker_targets", 0)),
            "micro_worker_fresh": STATE.get("micro_fresh", old_ext.get("micro_worker_fresh", 0)),
            "overlay_mode": "v229_post_scan_multi_external_refresh",
            "refreshed_ts": nowv,
            "refresh_attempt": attempt,
            "refresh_attempt_total": total_attempts,
            "refresh_waited_sec": round(waited_sec, 2),
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2),
        },
        "target": {
            **target,
            "post_scan_refresh": True,
            "post_scan_refresh_attempt": attempt,
            "post_scan_refresh_total": total_attempts,
            "post_scan_refresh_waited_sec": round(waited_sec, 2),
            "priority_rule": "scan_candidates_first; v229 re-overlays same candidates after micro urgent fast-lane without blocking scan",
        },
        "note": "v229: scan exits fast; WS/micro are re-overlaid several times after scan, and refreshed strict rows are written back to paper_latest for paper performance context",
    })
    save_json(FILES["candidate_snapshot"], refreshed)
    # paper_bot은 *_latest 파일을 소비하므로, snapshot만 고치면 성과 장부에는 stale 값이 남는다.
    # archive에는 중복 append하지 않고 latest만 교체해 진입 당시 context가 최신 micro 기준을 보게 한다.
    try:
        write_jsonl_replace(FILES["paper_latest"], rows)
    except Exception as exc:
        log_error("v229_refresh_write_paper_latest", exc)
    with _state_lock:
        STATE["candidate_snapshot_ts"] = nowv
        STATE["candidate_snapshot_count"] = len(rows)
        STATE["candidate_snapshot_source"] = "v229_post_scan_multi_external_refresh"
        STATE["candidate_snapshot_stage"] = "external_refreshed_after_scan"
        STATE["candidate_snapshot_external"] = refreshed.get("external", {})
        STATE["v219_last_refresh_scan_id"] = scan_id
        STATE["v219_last_refresh_ts"] = nowv
        STATE["v229_last_refresh_attempt"] = attempt
        STATE["v229_latest_rewrite_rows"] = len(rows)
        STATE["v229_micro_fresh_after_refresh"] = ext.get("micro_fresh", 0)
        STATE["v229_micro_wait_after_refresh"] = int(ext.get("micro_stale", 0) or 0) + int(ext.get("micro_missing", 0) or 0)
    build_quality = bool(attempt >= total_attempts or (int(ext.get("micro_stale", 0) or 0) + int(ext.get("micro_missing", 0) or 0) <= 0))
    _v219_write_command_caches_after_snapshot(build_quality=build_quality)
    return True, ext


def _v219_refresh_candidate_snapshot(expected_scan_id: str = "") -> None:
    """v229: micro urgent fast-lane 결과가 snapshot/paper_latest에 들어갈 때까지 짧게 재overlay한다.

    scan 본선을 기다리게 하지 않고, 같은 후보 묶음만 다시 입힌다. 후보 조건/청산조건은 바꾸지 않는다.
    """
    try:
        intervals = [max(0.4, min(float(x), 8.0)) for x in (V229_EXTERNAL_REFRESH_INTERVALS_SEC or [1.2, 2.2, 3.2])]
        waited = 0.0
        total = len(intervals)
        for idx, delay in enumerate(intervals, start=1):
            if _stop_event.wait(delay):
                return
            waited += delay
            ok, ext = _v229_refresh_candidate_snapshot_once(expected_scan_id, attempt=idx, total_attempts=total, waited_sec=waited)
            if not ok:
                return
            # 모든 현재 후보 micro가 fresh면 남은 반복은 생략한다.
            if int(ext.get("total", 0) or 0) > 0 and int(ext.get("micro_stale", 0) or 0) + int(ext.get("micro_missing", 0) or 0) <= 0:
                break
    except Exception as exc:
        log_error("v229_refresh_candidate_snapshot", exc)


def _v219_schedule_external_refresh(scan_id: str = "") -> None:
    global _V219_REFRESH_LAST_START_TS, _V219_REFRESH_LAST_SCAN_ID
    try:
        nowv = now_ts()
        with _V219_REFRESH_LOCK:
            # 같은 scan_id는 한 번만, 너무 잦은 refresh thread도 금지.
            if scan_id and _V219_REFRESH_LAST_SCAN_ID == scan_id and nowv - _V219_REFRESH_LAST_START_TS < 20:
                return
            if nowv - _V219_REFRESH_LAST_START_TS < V219_EXTERNAL_REFRESH_MIN_GAP_SEC:
                return
            _V219_REFRESH_LAST_START_TS = nowv
            _V219_REFRESH_LAST_SCAN_ID = scan_id or ""
        th = threading.Thread(target=_v219_refresh_candidate_snapshot, args=(scan_id,), name="v219_external_refresh", daemon=True)
        th.start()
    except Exception as exc:
        log_error("v219_schedule_external_refresh", exc)


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v219: 스캔은 기다리지 않고 빠르게 snapshot을 출하한다.

    WS/micro 최신화는 scan 뒤 별도 refresh thread가 같은 clean_candidate_snapshot.json에 다시 입힌다.
    """
    strict_rows, shadow_rows = _v212_rows_from_latest()
    if not strict_rows:
        strict_rows = [r for r in strict or [] if isinstance(r, dict)]
    if not shadow_rows:
        shadow_rows = [r for r in shadow or [] if isinstance(r, dict)]
    update_ws_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    update_micro_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    # scan 본선 대기는 짧게 유지. 최신 외부정보 보강은 refresh worker에서 처리한다.
    wait_sec = max(0.0, min(float(os.getenv("CLEAN_V219_SNAPSHOT_WAIT_SEC", "0.2")), 0.8))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    final = _v212_write_candidate_snapshot(strict_rows, shadow_rows, stage="final_after_target_overlay", source="factory_latest_rows_v219_fast_scan", wait_sec=wait_sec)
    scan_id = str(final.get("scan_id") or STATE.get("scan_id") or "") if isinstance(final, dict) else str(STATE.get("scan_id") or "")
    _v219_schedule_external_refresh(scan_id)
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    with _state_lock:
        STATE["v219_scan_fast_external_refresh"] = True
        STATE["v219_snapshot_initial_wait_sec"] = wait_sec
        STATE["v218_scan_first_mainline"] = True
    return f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / wait {wait_sec:.1f}s / target_writer factory_final_candidates / v229 multi-refresh scheduled"


def resource_health_lines() -> List[str]:  # type: ignore[override]
    """v221: cached resource keys are disk_pct/mem_pct. CPU and load are separate."""
    r = load_json(FILES["resource_status"], {})
    if not isinstance(r, dict) or not r:
        return ["❔ 서버자원 캐시 준비중"]
    age = max(0.0, now_ts() - fnum(r.get("updated_ts"), now_ts()))
    disk_pct = fnum(r.get("disk_pct", r.get("disk_used_pct", 0.0)), 0.0)
    mem_pct = fnum(r.get("mem_pct", r.get("mem_used_pct", 0.0)), 0.0)
    cpu_pct = fnum(r.get("cpu_total_pct"), 0.0)
    load = r.get("load") if isinstance(r.get("load"), list) else []
    load1 = fnum(r.get("load1", load[0] if len(load) > 0 else 0.0), 0.0)
    load5 = fnum(r.get("load5", load[1] if len(load) > 1 else 0.0), 0.0)
    load15 = fnum(r.get("load15", load[2] if len(load) > 2 else 0.0), 0.0)
    proc = r.get("cpu_proc_pct") if isinstance(r.get("cpu_proc_pct"), dict) else {}
    rss = r.get("rss") if isinstance(r.get("rss"), dict) else {}
    note = str(r.get("cpu_sample_note") or "측정중")
    if note == "측정중" and cpu_pct > 0:
        note = "측정완료"
    disk_icon = "✅" if disk_pct < 85 else ("⚠️" if disk_pct < 95 else "❌")
    mem_icon = "✅" if mem_pct < 80 else ("⚠️" if mem_pct < 92 else "❌")
    cpu_icon = "✅" if cpu_pct < 70 else ("⚠️" if cpu_pct < 90 else "❌")
    load_icon = "✅" if load1 < 2.0 else ("⚠️" if load1 < 5.0 else "❌")
    return [
        f"{disk_icon} 디스크: 사용 {disk_pct:.1f}% / 남음 {_fmt_bytes(r.get('disk_free',0))} / 전체 {_fmt_bytes(r.get('disk_total',0))}",
        f"{mem_icon} 메모리: 사용 {mem_pct:.1f}% / 남음 {_fmt_bytes(r.get('mem_avail',0))} / 전체 {_fmt_bytes(r.get('mem_total',0))}",
        f"{cpu_icon} CPU: 전체 {cpu_pct:.1f}% / main {fnum(proc.get('main'),0):.1f}% / paper {fnum(proc.get('paper'),0):.1f}% / WS {fnum(proc.get('ws'),0):.1f}% / micro {fnum(proc.get('micro'),0):.1f}%",
        f"{load_icon} load: {load1:.2f} / {load5:.2f} / {load15:.2f} / RSS main {_fmt_bytes(rss.get('main',0))} / paper {_fmt_bytes(rss.get('paper',0))} / WS {_fmt_bytes(rss.get('ws',0))} / micro {_fmt_bytes(rss.get('micro',0))} / {note} / 캐시 {age:.0f}초 전",
    ]


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    txt = _read_cached_text(FILES["quality_summary"], "/quality")
    return txt.replace("v222 기본은 캐시 전용", "v222 기본은 캐시 전용").replace("v216 기본은 캐시 전용", "v222 기본은 캐시 전용").replace("v214 기본은 캐시 전용", "v222 기본은 캐시 전용")



# ===============================
# v2.13.220: auto-trade candidate WS force reconnect request
# ===============================
# 목적:
# - 모든 후보를 억지로 최신화하지 않는다.
# - 자동매매 검증급/모의진입 가능 후보만 WS stale/missing이면 sidecar에 force_reconnect를 요청한다.
# - 스캔은 계속 빠르게 끝내고, WS sidecar v0.7이 같은 target set이어도 reconnect_seq 변경을 보고 재구독한다.

V220_AUTO_WS_FORCE_MIN_INTERVAL_SEC = float(os.getenv("V220_AUTO_WS_FORCE_MIN_INTERVAL_SEC", "5.0"))
V220_AUTO_WS_FORCE_MAX_ROWS = int(os.getenv("V220_AUTO_WS_FORCE_MAX_ROWS", "20"))
V220_AUTO_WS_SCORE_FLOOR = float(os.getenv("V220_AUTO_WS_SCORE_FLOOR", "4.45"))

_v220_base_update_ws_targets = update_ws_targets

def _v220_is_auto_trade_candidate(row: Dict[str, Any]) -> bool:
    try:
        if not isinstance(row, dict):
            return False
        action = str(row.get("final_entry_action") or row.get("action") or "").lower()
        label = str(row.get("final_entry_label") or row.get("auto_ready_label") or row.get("quality_label") or row.get("hold_reason") or "")
        if bool(row.get("paper_bot_open") or row.get("trade_ready") or row.get("open_eligible")):
            return True
        if action in {"paper_open", "trade_ready", "open", "buy_ready"}:
            return True
        if "자동매매" in label or "모의매매 검증" in label or "검증급" in label:
            return True
        if fnum(row.get("score"), 0.0) >= V220_AUTO_WS_SCORE_FLOOR and action not in {"reject", "block"}:
            # 점수만으로 매수조건을 바꾸는 것이 아니라, WS 최신화 요청 대상으로만 본다.
            return True
    except Exception:
        return False
    return False


def _v220_ws_status_for_ticker(ticker: str) -> str:
    try:
        row = ws_snapshot(ticker)
        st = str(row.get("ws_row_status") or row.get("live_status") or "").lower()
        age = fnum(row.get("ws_age_sec", row.get("live_age_sec", -1)), -1)
        if st == "fresh" or (0 <= age <= WS_HUB_STALE_SEC):
            return "fresh"
        if st in {"stale", "old", "오래됨"} or age >= 0:
            return "stale"
    except Exception:
        pass
    return "missing"


def _v220_mark_auto_ws_payload(path: Path, priority_rows: Optional[List[Dict[str, Any]]], reason: str) -> None:
    try:
        rows = [r for r in (priority_rows or []) if _v220_is_auto_trade_candidate(r)]
        if not rows:
            return
        rows = rows[:max(1, V220_AUTO_WS_FORCE_MAX_ROWS)]
        tickers: List[str] = []
        for r in rows:
            t = _ticker_from_any((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if t and t not in tickers:
                tickers.append(t)
        if not tickers:
            return
        status = {t: _v220_ws_status_for_ticker(t) for t in tickers}
        fresh = sum(1 for v in status.values() if v == "fresh")
        stale = sum(1 for v in status.values() if v == "stale")
        missing = sum(1 for v in status.values() if v == "missing")
        need_force = (stale + missing) > 0
        if not need_force:
            with _state_lock:
                STATE["v220_auto_ws_count"] = len(tickers)
                STATE["v220_auto_ws_fresh"] = fresh
                STATE["v220_auto_ws_stale"] = stale
                STATE["v220_auto_ws_missing"] = missing
                STATE["v220_auto_ws_force"] = False
            return
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            return
        nowv = now_ts()
        last_force = fnum(payload.get("last_force_reconnect_ts") or STATE.get("v220_last_ws_force_ts"), 0.0)
        force_due = nowv - last_force >= V220_AUTO_WS_FORCE_MIN_INTERVAL_SEC
        meta = payload.get("target_meta")
        if not isinstance(meta, dict):
            meta = {}
        for t in tickers:
            m = meta.get(t)
            if not isinstance(m, dict):
                m = {}
            m["auto_candidate"] = True
            m["auto_ws_status"] = status.get(t, "missing")
            m["source"] = m.get("source") or "current_candidate"
            m["priority"] = max(int(fnum(m.get("priority"), 0)), 150)
            m["last_seen"] = nowv
            meta[t] = m
        payload["target_meta"] = meta
        payload["auto_candidate_count"] = len(tickers)
        payload["auto_candidate_tickers"] = tickers[:20]
        payload["auto_candidate_ws_fresh"] = fresh
        payload["auto_candidate_ws_stale"] = stale
        payload["auto_candidate_ws_missing"] = missing
        payload["auto_candidate_ws_checked_ts"] = nowv
        payload["auto_candidate_ws_checked_at"] = now_text(nowv)
        payload["auto_candidate_ws_note"] = "v220: auto-trade-grade candidates must be refreshed first; this is not a buy-condition change"
        if force_due:
            seq = int(fnum(payload.get("reconnect_seq"), 0)) + 1
            payload["force_reconnect"] = True
            payload["reconnect_seq"] = seq
            payload["reconnect_reason"] = "auto_candidate_ws_stale_or_missing"
            payload["last_force_reconnect_ts"] = nowv
            payload["updated_ts"] = nowv
            payload["reason"] = reason or payload.get("reason") or "factory_final_candidates"
            payload["write_note"] = "force_reconnect_auto_candidate"
            atomic_write(path, payload)
        else:
            # reconnect throttle 중이어도 진단 메타는 저장한다. updated_ts는 건드리지 않는다.
            payload["force_reconnect"] = False
            payload["write_note"] = "auto_candidate_force_throttled"
            atomic_write(path, payload)
        with _state_lock:
            STATE["v220_auto_ws_count"] = len(tickers)
            STATE["v220_auto_ws_fresh"] = fresh
            STATE["v220_auto_ws_stale"] = stale
            STATE["v220_auto_ws_missing"] = missing
            STATE["v220_auto_ws_force"] = bool(force_due)
            STATE["v220_last_ws_force_ts"] = nowv if force_due else last_force
            STATE["v220_auto_ws_tickers"] = tickers[:8]
    except Exception as exc:
        log_error("v220_mark_auto_ws_payload", exc)


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    _v220_base_update_ws_targets(rows, priority_rows=priority_rows, reason=reason)
    _v220_mark_auto_ws_payload(_ws_target_payload_path(), priority_rows, reason)


_v220_base_external_status_text = _v210_external_status_text

def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v220_base_external_status_text(snap)
    try:
        p = _load_ws_target_payload()
        ac = int(fnum(p.get("auto_candidate_count"), 0)) if isinstance(p, dict) else 0
        if ac:
            fresh = int(fnum(p.get("auto_candidate_ws_fresh"), 0)); stale = int(fnum(p.get("auto_candidate_ws_stale"), 0)); missing = int(fnum(p.get("auto_candidate_ws_missing"), 0))
            force = "강제재구독 요청" if bool(p.get("force_reconnect")) else "대기/정상"
            reason = str(p.get("reconnect_reason") or "-")
            txt += f"\n\n[자동매매급 WS 최신화]\n- 대상 {ac}개 / fresh {fresh} / stale {stale} / missing {missing} / 상태 {force}\n- reason {reason} / seq {p.get('reconnect_seq','-')}"
    except Exception:
        pass
    return txt


_v220_base_health_text = health_text

def health_text() -> str:  # type: ignore[override]
    return _v220_base_health_text().replace("수익형 v2.13.219", "수익형 v2.13.220")



# ===============================
# v2.13.221: micro fresh 수술
# - tail override가 main() 뒤에서 죽지 않도록 main()은 파일 맨 아래에서 1회만 호출한다.
# - 자동매매급/보류 후보 품질확인용 micro urgent 대상을 별도 파일에 출하한다.
# - paper_bot이 OPEN 직전 stale 후보를 urgent 대기열로 올릴 수 있게 같은 파일을 쓴다.
# - 조건/청산/BUY_READY/자동매수 변경 없음.
# ===============================
MICRO_URGENT_TARGET_FILE = BASE_DIR / "clean_micro_urgent_targets.json"
V221_MICRO_URGENT_TTL_SEC = float(os.getenv("V221_MICRO_URGENT_TTL_SEC", "35"))
V221_MICRO_URGENT_MAX = int(os.getenv("V221_MICRO_URGENT_MAX", "80"))


# v229 cleanup:
# v221의 옛 urgent wrapper는 v225/v226에서 NameError 원인이었고, v227 확장 urgent writer가 단일 본선이다.
# 여기서는 v227이 감쌀 실제 micro target 본선만 명시적으로 고정하고, 옛 writer/update wrapper는 제거한다.
_v221_base_update_micro_targets = update_micro_targets


_v221_base_external_status_text = _v210_external_status_text

def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v221_base_external_status_text(snap)
    try:
        p = load_json(MICRO_URGENT_TARGET_FILE, {})
        if isinstance(p, dict) and p.get("targets"):
            meta = p.get("target_meta") if isinstance(p.get("target_meta"), dict) else {}
            total = len(p.get("targets") or [])
            fresh = sum(1 for t in (p.get("targets") or []) if (meta.get(t) or {}).get("status") == "fresh")
            stale = sum(1 for t in (p.get("targets") or []) if (meta.get(t) or {}).get("status") == "stale")
            missing = sum(1 for t in (p.get("targets") or []) if (meta.get(t) or {}).get("status") == "missing")
            age = max(0.0, now_ts() - fnum(p.get("updated_ts"), now_ts()))
            tops = ", ".join(str(x) for x in (p.get("targets") or [])[:8])
            txt += f"\n\n[호가·체결 우선확인]\n- 대상 {total}개 / fresh {fresh} / stale {stale} / missing {missing} / {age:.0f}초 전\n- 우선: {tops}\n- 용도: 자동매매급·보류후보 품질확인용 최신화, 매수조건 아님"
    except Exception:
        pass
    return txt


_v221_base_quality_text = candidate_quality_text

def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    txt = _v221_base_quality_text(full)
    if full:
        return txt
    return txt.replace("v222 기본은 캐시 전용", "v222 기본은 캐시 전용").replace("v222 기본은 캐시 전용", "v222 기본은 캐시 전용")


_v221_base_health_text = health_text

def health_text() -> str:  # type: ignore[override]
    return _v221_base_health_text().replace("수익형 v2.13.221", "수익형 v2.13.223").replace("수익형 v2.13.220", "수익형 v2.13.223").replace("수익형 v2.13.219", "수익형 v2.13.223")



# ===============================
# v2.13.222: micro urgent 끊긴 경로 복구
# - v221의 _v218_ext_maps NameError를 현재 v218 캐시맵 함수로 재연결
# - 조건/청산/BUY_READY/자동매수 변경 없음
# - paper/micro/ws/guard 파일 변경 없음
# ===============================


# ===============================
# v2.13.223: 최근 5개 버전별 성과 표시 보강
# - 조건/청산/BUY_READY/자동매수 변경 없음
# - /quality 성과 요약과 /version_score 캐시에 최근 종료가 있는 5개 버전 성과를 표시한다.
# - 전수/승률/합산수익/평균/최대수익 제외 합산을 함께 보여 한 방 착시를 줄인다.
# ===============================


def _v223_version_key(row: Dict[str, Any]) -> str:
    try:
        return str(row_brain_version(row) or _fast_version_label(row) or "버전 미기록")
    except Exception:
        return str((row or {}).get("opened_brain_version") or (row or {}).get("brain_version") or (row or {}).get("bot_version") or "버전 미기록")


def _v223_top_excluded_stat(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    arr = list(rows or [])
    if not arr:
        return {"excl_total": 0.0, "excl_avg": 0.0, "top_sym": "-", "top_pnl": 0.0, "top_reason": "-"}
    st = score_stats(arr)
    top = max(arr, key=_pnl)
    top_p = _pnl(top)
    n = int(st.get("n", len(arr)) or len(arr))
    excl_total = fnum(st.get("total"), 0.0) - top_p
    excl_avg = excl_total / max(1, n - 1) if n > 1 else 0.0
    return {
        "excl_total": excl_total,
        "excl_avg": excl_avg,
        "top_sym": str(top.get("symbol") or top.get("ticker") or top.get("market") or "?"),
        "top_pnl": top_p,
        "top_reason": label_kr(str(top.get("exit_reason") or top.get("close_reason") or "unknown")),
    }


def _v223_recent_version_groups(limit_rows: int = 12000, max_versions: int = 5) -> List[Tuple[str, List[Dict[str, Any]]]]:
    rows = [r for r in load_closed(limit=limit_rows) if str(r.get("lane")) == "strict"]
    by_ver: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    latest_ts: Dict[str, float] = {}
    for r in rows:
        ver = _v223_version_key(r)
        if not ver or ver == "버전 미기록":
            continue
        by_ver[ver].append(r)
        latest_ts[ver] = max(latest_ts.get(ver, 0.0), row_ts(r, "closed_at", "exit_at", "opened_at", "source_created_at"))
    keys = sorted(by_ver.keys(), key=lambda k: (latest_ts.get(k, 0.0), k), reverse=True)
    return [(k, by_ver[k]) for k in keys[:max_versions]]


def _v223_version_score_icon(st: Dict[str, Any], excl_total: float) -> str:
    n = int(st.get("n", 0) or 0)
    total = fnum(st.get("total"), 0.0)
    if n <= 0:
        return "❔"
    if n < 50:
        return "⚠️"
    if total > 0 and excl_total >= 0:
        return "✅"
    if total > 0 and excl_total < 0:
        return "⚠️"
    return "❌"


def _v223_recent_version_score_lines(max_versions: int = 5) -> List[str]:
    groups = _v223_recent_version_groups(max_versions=max_versions)
    lines = ["[1-2] 최근 5개 버전별 성과", "- 기준: 정식 CLOSED / 최신 종료 시각 기준 / 최대수익 제외로 한 방 착시 확인"]
    if not groups:
        return lines + ["- 버전별 종료 기록 없음"]
    for ver, arr in groups:
        st = score_stats(arr)
        extra = _v223_top_excluded_stat(arr)
        n = int(st.get("n", 0) or 0)
        wins = int(st.get("wins", 0) or 0)
        losses = int(st.get("losses", max(0, n - wins)) or 0)
        icon = _v223_version_score_icon(st, fnum(extra.get("excl_total"), 0.0))
        sample = " / 판단보류" if n < 50 else ""
        lines.append(
            f"{icon} {ver}: {n}전 {wins}승 {losses}패 / 승률 {fnum(st.get('win_rate'),0):.1f}% / "
            f"합산 {fnum(st.get('total'),0):+.2f}% / 평균 {fnum(st.get('avg'),0):+.2f}% / "
            f"최대제외 {fnum(extra.get('excl_total'),0):+.2f}%{sample}"
        )
        lines.append(
            f"  · 최대 {extra.get('top_sym','?')} {fnum(extra.get('top_pnl'),0):+.2f}%({extra.get('top_reason','-')}) / "
            f"최대제외 평균 {fnum(extra.get('excl_avg'),0):+.2f}%"
        )
    return lines


_v223_base_quality_text_builder = _v216_quality_text

def _v216_quality_text() -> str:  # type: ignore[override]
    txt = _v223_base_quality_text_builder()
    try:
        block = "\n".join(_v223_recent_version_score_lines(5))
        marker = "\n[2/5] 현재 후보"
        if marker in txt:
            return txt.replace(marker, "\n" + block + "\n" + marker, 1)
        return txt + "\n\n" + block
    except Exception as exc:
        log_error("v223_quality_version_score_lines", exc)
        return txt


def _v223_version_score_text() -> str:
    try:
        base = _v210_version_score_text()
    except Exception as exc:
        log_error("v223_base_version_score", exc)
        base = _read_cached_text(FILES["version_score_summary"], "/version_score")
    try:
        return base + "\n\n" + "\n".join(_v223_recent_version_score_lines(5))
    except Exception as exc:
        log_error("v223_version_score_lines", exc)
        return base


def _build_version_score_cache() -> None:  # type: ignore[override]
    try:
        save_json(FILES["version_score_summary"], _cache_payload(_v223_version_score_text(), "version_score"))
    except Exception as exc:
        log_error("v223_version_score_cache", exc)



# ===============================
# v2.13.224: micro urgent NameError 재발 방지 + 자원표시 안정화
# - v221_write_micro_urgent가 없는 MICRO_FRESH_SEC 상수에 물리지 않게 현재 micro fresh 기준(MICRO_STALE_SEC)으로 단일화
# - CPU 첫 샘플/측정중 상태를 0.0% 정상처럼 보이지 않게 표시
# - CPU와 load 판정은 계속 분리한다.
# - 조건/청산/BUY_READY/자동매수 변경 없음
# ===============================

# 구 tail/후속 함수가 MICRO_FRESH_SEC 이름을 참조해도 현재 기준으로 귀결되게 고정한다.
# 실제 fresh 판정 기준은 기존 코드에서 쓰던 MICRO_STALE_SEC 하나만 사용한다.
MICRO_FRESH_SEC = MICRO_STALE_SEC


def resource_health_lines() -> List[str]:  # type: ignore[override]
    """v224: disk/mem은 실제 pct 키, CPU는 측정중/샘플완료를 구분해 표시한다."""
    r = load_json(FILES["resource_status"], {})
    if not isinstance(r, dict) or not r:
        return ["❔ 서버자원 캐시 준비중"]
    age = max(0.0, now_ts() - fnum(r.get("updated_ts"), now_ts()))
    disk_pct = fnum(r.get("disk_pct", r.get("disk_used_pct", 0.0)), 0.0)
    mem_pct = fnum(r.get("mem_pct", r.get("mem_used_pct", 0.0)), 0.0)
    cpu_pct = fnum(r.get("cpu_total_pct"), 0.0)
    load = r.get("load") if isinstance(r.get("load"), list) else []
    load1 = fnum(r.get("load1", load[0] if len(load) > 0 else 0.0), 0.0)
    load5 = fnum(r.get("load5", load[1] if len(load) > 1 else 0.0), 0.0)
    load15 = fnum(r.get("load15", load[2] if len(load) > 2 else 0.0), 0.0)
    proc = r.get("cpu_proc_pct") if isinstance(r.get("cpu_proc_pct"), dict) else {}
    rss = r.get("rss") if isinstance(r.get("rss"), dict) else {}
    note = str(r.get("cpu_sample_note") or "측정중")
    cpu_ready = note.startswith("측정간격") or note.startswith("측정완료") or bool(r.get("cpu_sample_ready"))
    # 첫 샘플은 0.0%로 찍히기 쉬우므로 정상처럼 보이지 않게 분리한다.
    proc_sum = sum(fnum(v, 0.0) for v in proc.values()) if isinstance(proc, dict) else 0.0
    cpu_waiting = (not cpu_ready) and cpu_pct <= 0.01 and proc_sum <= 0.01

    disk_icon = "✅" if disk_pct < 85 else ("⚠️" if disk_pct < 95 else "❌")
    mem_icon = "✅" if mem_pct < 80 else ("⚠️" if mem_pct < 92 else "❌")
    load_icon = "✅" if load1 < 2.0 else ("⚠️" if load1 < 5.0 else "❌")
    if cpu_waiting:
        cpu_icon = "❔"
        cpu_line = (
            f"{cpu_icon} CPU: 측정중 / main {fnum(proc.get('main'),0):.1f}% / paper {fnum(proc.get('paper'),0):.1f}% / "
            f"WS {fnum(proc.get('ws'),0):.1f}% / micro {fnum(proc.get('micro'),0):.1f}%"
        )
    else:
        if note == "측정중" and cpu_pct > 0:
            note = "측정완료"
        cpu_icon = "✅" if cpu_pct < 70 else ("⚠️" if cpu_pct < 90 else "❌")
        cpu_line = (
            f"{cpu_icon} CPU: 전체 {cpu_pct:.1f}% / main {fnum(proc.get('main'),0):.1f}% / paper {fnum(proc.get('paper'),0):.1f}% / "
            f"WS {fnum(proc.get('ws'),0):.1f}% / micro {fnum(proc.get('micro'),0):.1f}%"
        )
    return [
        f"{disk_icon} 디스크: 사용 {disk_pct:.1f}% / 남음 {_fmt_bytes(r.get('disk_free',0))} / 전체 {_fmt_bytes(r.get('disk_total',0))}",
        f"{mem_icon} 메모리: 사용 {mem_pct:.1f}% / 남음 {_fmt_bytes(r.get('mem_avail',0))} / 전체 {_fmt_bytes(r.get('mem_total',0))}",
        cpu_line,
        f"{load_icon} load: {load1:.2f} / {load5:.2f} / {load15:.2f} / RSS main {_fmt_bytes(rss.get('main',0))} / paper {_fmt_bytes(rss.get('paper',0))} / WS {_fmt_bytes(rss.get('ws',0))} / micro {_fmt_bytes(rss.get('micro',0))} / {note} / 캐시 {age:.0f}초 전",
    ]


_v224_base_health_text = health_text

def health_text() -> str:  # type: ignore[override]
    return (_v224_base_health_text()
            .replace("수익형 v2.13.223", "수익형 v2.13.224")
            .replace("수익형 v2.13.222", "수익형 v2.13.224")
            .replace("수익형 v2.13.221", "수익형 v2.13.224"))


_v224_base_quality_text_builder = _v216_quality_text

def _v216_quality_text() -> str:  # type: ignore[override]
    txt = _v224_base_quality_text_builder()
    return (txt.replace("v223 기본은 캐시 전용", "v226 기본은 캐시 전용")
               .replace("v222 기본은 캐시 전용", "v226 기본은 캐시 전용")
               .replace("v221 기본은 캐시 전용", "v226 기본은 캐시 전용")
               .replace("v219 기본은 캐시 전용", "v226 기본은 캐시 전용"))



# ===============================
# v2.13.226: micro target 본선 재연결
# - v225에서 v221 urgent wrapper가 감쌀 본선 writer 이름을 저장하지 않아 scan_once가 중단되던 문제 수정
# - fresh 판정은 v225 single micro cache reader 유지
# - 조건/청산/BUY_READY/자동매수 변경 없음
# ===============================

_v226_base_health_text = health_text
def health_text() -> str:  # type: ignore[override]
    return (_v226_base_health_text()
            .replace("수익형 v2.13.225", "수익형 v2.13.231")
            .replace("수익형 v2.13.224", "수익형 v2.13.231")
            .replace("수익형 v2.13.226", "수익형 v2.13.231")
            .replace("v225 micro fresh 단일판정", "v230 micro fresh snapshot/paper 반영"))

_v226_base_quality_text_builder = _v216_quality_text
def _v216_quality_text() -> str:  # type: ignore[override]
    txt = _v226_base_quality_text_builder()
    return (txt.replace("v225 기본은 캐시 전용", "v226 기본은 캐시 전용")
               .replace("v224 기본은 캐시 전용", "v226 기본은 캐시 전용")
               .replace("v223 기본은 캐시 전용", "v226 기본은 캐시 전용")
               .replace("v222 기본은 캐시 전용", "v226 기본은 캐시 전용"))

# ===============================
# v2.13.227: micro urgent 범위 확대 + urgent 제외 후보 표시
# - 조건/청산/BUY_READY/자동매수 변경 없음.
# - 현재 후보/모의매매 검증 후보/실전위험 재확인 후보를 micro urgent 요청에 더 넓게 포함한다.
# - urgent는 요청용, fresh 판정은 clean_bithumb_micro_cache.json 단일 기준을 유지한다.
# - micro sidecar v0.10은 urgent target을 회전 fast-lane으로 처리한다.
# ===============================
V227_MICRO_URGENT_MAX = int(os.getenv("V227_MICRO_URGENT_MAX", "48"))
V227_MICRO_URGENT_MIN_SCORE = float(os.getenv("V227_MICRO_URGENT_MIN_SCORE", "3.50"))


def _v227_row_label_text(row: Dict[str, Any]) -> str:
    try:
        vals = [
            row.get("final_entry_label"), row.get("auto_ready_label"), row.get("quality_label"),
            row.get("hold_reason"), row.get("reject_reason"), row.get("block_reason"),
            row.get("final_entry_action"), row.get("auto_ready_level"),
        ]
        for key in ("final_entry_reasons", "block_reasons", "execution_risk_flags", "quality_risk_tags", "aux_notes"):
            v = row.get(key)
            if isinstance(v, list):
                vals.extend(v[:6])
            elif v:
                vals.append(v)
        prof = row.get("profile") if isinstance(row.get("profile"), dict) else {}
        for key in ("blocks", "reasons"):
            v = prof.get(key)
            if isinstance(v, list):
                vals.extend(v[:6])
        return " / ".join(str(x) for x in vals if x not in (None, ""))[:400]
    except Exception:
        return ""


def _v227_row_is_quality_watch(row: Dict[str, Any]) -> bool:
    """품질분석에 필요한 후보를 urgent 요청 대상으로 넓게 잡는다.

    매수조건이 아니라 micro 수집 우선순위다.
    """
    try:
        if not isinstance(row, dict):
            return False
        if bool(row.get("major_watch")):
            # 대형 참고용은 남는 target으로 충분하다.
            return False
        if bool(row.get("paper_bot_open") or row.get("trade_ready") or row.get("open_eligible")):
            return True
        label = _v227_row_label_text(row)
        if any(k in label for k in ("모의매매 검증", "실전위험", "재확인", "보류", "검증", "관찰", "paper_ready", "trade_ready")):
            return True
        action = str(row.get("final_entry_action") or row.get("action") or "").lower()
        if action in {"recheck_wait", "observe", "paper_open", "trade_ready"}:
            return True
        # 현재 정식 후보 중 점수권은 보류/복기 품질 확인을 위해 우선확인에 포함한다.
        if fnum(row.get("score"), 0.0) >= V227_MICRO_URGENT_MIN_SCORE:
            return True
        return False
    except Exception:
        return False


def _v227_candidate_rows_for_urgent(priority_rows: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # 1) 현재 factory가 넘긴 strict rows
    for r in priority_rows or []:
        if isinstance(r, dict):
            rows.append(r)
    # 2) 직전 snapshot rows: scan 직후/캐시 시점 차이 보강
    try:
        strict, _shadow, _snap = _v212_snapshot_rows()
        for r in strict:
            if isinstance(r, dict):
                rows.append(r)
    except Exception:
        pass
    # 3) latest 후보파일: paper가 소비하는 원천과 맞춘다.
    try:
        for r in tail_jsonl(FILES.get("paper_latest", BASE_DIR / "paper_candidates_latest.jsonl"), max_lines=140):
            if isinstance(r, dict):
                rows.append(r)
    except Exception:
        pass
    seen = set(); out: List[Dict[str, Any]] = []
    def rank(r: Dict[str, Any]) -> tuple:
        label = _v227_row_label_text(r)
        return (
            bool(r.get("paper_bot_open") or r.get("trade_ready") or r.get("open_eligible")),
            "모의매매 검증" in label,
            "실전위험" in label or "재확인" in label,
            fnum(r.get("score"), 0.0),
            fnum(r.get("money_flow_3m") or r.get("turnover_3m"), 0.0),
        )
    for r in sorted([x for x in rows if _v227_row_is_quality_watch(x)], key=rank, reverse=True):
        t = _ticker_from_any(r.get("ticker") or r.get("market") or r.get("symbol"))
        if not t or t in seen or t in STABLE_EXCLUDED:
            continue
        seen.add(t); out.append(r)
        if len(out) >= max(8, V227_MICRO_URGENT_MAX):
            break
    return out


def _v227_write_micro_urgent_from_candidates(priority_rows: Optional[List[Dict[str, Any]]], reason: str = "factory_final_candidates") -> None:
    """v227: urgent 대상 확대. fresh 판정은 실제 micro cache 기준만 사용."""
    try:
        rows = _v227_candidate_rows_for_urgent(priority_rows)
        tickers: List[str] = []
        meta: Dict[str, Any] = {}
        nowv = now_ts()
        src = _v225_load_micro_source(force=True)
        for r in rows:
            t = _ticker_from_any(r.get("ticker") or r.get("market") or r.get("symbol"))
            if not t or t in tickers or t in STABLE_EXCLUDED:
                continue
            tickers.append(t)
            ms = _v225_micro_snapshot_from_source(t, src)
            label = _v227_row_label_text(r)
            if bool(r.get("paper_bot_open") or r.get("trade_ready") or r.get("open_eligible")):
                source = "paper_open_candidate"
            elif "실전위험" in label or "재확인" in label:
                source = "risk_recheck_candidate"
            elif "모의매매 검증" in label:
                source = "paper_verify_candidate"
            else:
                source = "quality_watch_candidate"
            meta[t] = {
                "source": source,
                "score": fnum(r.get("score"), 0.0),
                "status": str(ms.get("micro_row_status") or "missing"),
                "age_sec": ms.get("micro_age_sec"),
                "trade_ready": bool(r.get("paper_bot_open") or r.get("trade_ready") or r.get("open_eligible")),
                "label": label[:100],
            }
        if not tickers:
            return
        # 현재 snapshot 후보 중 urgent에 못 들어간 후보도 표시용으로 계산한다.
        current_rows: List[Dict[str, Any]] = []
        try:
            current_rows, _shadow, _snap = _v212_snapshot_rows()
        except Exception:
            current_rows = []
        current_tickers = []
        for r in current_rows:
            t = _ticker_from_any((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if t and t not in current_tickers and t not in STABLE_EXCLUDED:
                current_tickers.append(t)
        urgent_set = set(tickers)
        excluded = [t for t in current_tickers if t not in urgent_set]
        ex_fresh = ex_stale = ex_missing = 0
        for t in excluded:
            st = str(_v225_micro_snapshot_from_source(t, src).get("micro_row_status") or "missing")
            if st == "fresh": ex_fresh += 1
            elif st == "stale": ex_stale += 1
            else: ex_missing += 1
        payload = {
            "version": BOT_VERSION,
            "schema": "micro_urgent_targets_v227",
            "source": "main_bot_v227",
            "reason": reason,
            "updated_ts": nowv,
            "updated_at": now_text(nowv),
            "ttl_sec": V221_MICRO_URGENT_TTL_SEC,
            "targets": tickers,
            "target_meta": meta,
            "current_candidate_count": len(current_tickers),
            "current_urgent_count": sum(1 for t in current_tickers if t in urgent_set),
            "current_excluded_count": len(excluded),
            "current_excluded_fresh": ex_fresh,
            "current_excluded_stale": ex_stale,
            "current_excluded_missing": ex_missing,
            "current_excluded_sample": excluded[:12],
            "note": "v227: widened urgent for paper-verify/recheck/quality-watch candidates; urgent is request-only; fresh is decided from clean_bithumb_micro_cache.json",
        }
        atomic_write(MICRO_URGENT_TARGET_FILE, payload)
        fresh = sum(1 for t in tickers if meta.get(t, {}).get("status") == "fresh")
        stale = sum(1 for t in tickers if meta.get(t, {}).get("status") == "stale")
        missing = sum(1 for t in tickers if meta.get(t, {}).get("status") == "missing")
        with _state_lock:
            STATE["v221_micro_urgent_count"] = len(tickers)
            STATE["v221_micro_urgent_fresh"] = fresh
            STATE["v221_micro_urgent_stale"] = stale
            STATE["v221_micro_urgent_missing"] = missing
            STATE["v221_micro_urgent_written"] = nowv
            STATE["v227_micro_urgent_current_count"] = len(current_tickers)
            STATE["v227_micro_urgent_excluded_count"] = len(excluded)
    except Exception as exc:
        log_error("v227_write_micro_urgent", exc)


# v227: 기존 v217 current-first target writer를 본선으로 두고, 확장 urgent 파일을 그 뒤에 쓴다.
_v227_base_update_micro_targets = globals().get("_v221_base_update_micro_targets", update_micro_targets)

def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "hub_rank") -> None:  # type: ignore[override]
    _v227_base_update_micro_targets(rows, priority_rows=priority_rows, reason=reason)
    _v227_write_micro_urgent_from_candidates(priority_rows, reason=reason)


_v227_base_external_status_text = _v210_external_status_text

def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v227_base_external_status_text(snap)
    # 이전 v221의 짧은 [호가·체결 우선확인] 블록이 있으면 제거하고 확장판을 붙인다.
    marker = "\n\n[호가·체결 우선확인]\n"
    if marker in txt:
        txt = txt.split(marker, 1)[0]
    try:
        p = load_json(MICRO_URGENT_TARGET_FILE, {})
        if isinstance(p, dict) and p.get("targets"):
            meta = p.get("target_meta") if isinstance(p.get("target_meta"), dict) else {}
            targets = list(p.get("targets") or [])
            total = len(targets)
            fresh = sum(1 for t in targets if (meta.get(t) or {}).get("status") == "fresh")
            stale = sum(1 for t in targets if (meta.get(t) or {}).get("status") == "stale")
            missing = sum(1 for t in targets if (meta.get(t) or {}).get("status") == "missing")
            age = max(0.0, now_ts() - fnum(p.get("updated_ts"), now_ts()))
            tops = ", ".join(str(x) for x in targets[:10])
            cur_cnt = int(fnum(p.get("current_candidate_count"), 0))
            cur_urg = int(fnum(p.get("current_urgent_count"), 0))
            ex_cnt = int(fnum(p.get("current_excluded_count"), 0))
            ex_f = int(fnum(p.get("current_excluded_fresh"), 0))
            ex_s = int(fnum(p.get("current_excluded_stale"), 0))
            ex_m = int(fnum(p.get("current_excluded_missing"), 0))
            ex_sample = ", ".join(str(x) for x in (p.get("current_excluded_sample") or [])[:8]) or "-"
            txt += (
                f"\n\n[호가·체결 우선확인]\n"
                f"- urgent 대상 {total}개 / fresh {fresh} / stale {stale} / missing {missing} / {age:.0f}초 전\n"
                f"- 현재후보 포함 {cur_urg}/{cur_cnt} / urgent 제외 {ex_cnt}개(fresh {ex_f} / stale {ex_s} / missing {ex_m})\n"
                f"- 우선: {tops}\n"
                f"- 제외예시: {ex_sample}\n"
                f"- 용도: 자동매매급·보류후보 품질확인용 최신화, 매수조건 아님"
            )
    except Exception:
        pass
    return txt


_v227_base_quality_text = candidate_quality_text

def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    txt = _v227_base_quality_text(full)
    if full:
        return txt
    return txt.replace("v222 기본은 캐시 전용", "v231 기본은 캐시 전용").replace("v227 기본은 캐시 전용", "v231 기본은 캐시 전용")

# ===============================
# v2.13.228: 실행순서 수술 + WS 진단 + scan 단계요약
# - main() 호출을 파일 맨 아래 1회로 고정해 v227 tail override가 실제 실행 전에 정의되게 한다.
# - WS fresh 부족은 조건으로 쓰지 않고, target/cache/tick 진단으로 원인을 분리한다.
# - scan 30초대 원인을 보기 위해 기본 /health 캐시에 짧은 단계요약을 붙인다.
# - 조건/청산/BUY_READY/자동매수 변경 없음.
# ===============================


def _v228_short_stage_name(name: Any) -> str:
    s = str(name or "-")
    for old, new in [
        ("1) 허브: 전체시장 bulk 수집 + 웹소켓 보조", "허브"),
        ("2) 1차 직원: 표준값/신선도/정밀대상 선정", "1차선정"),
        ("3) 2차 직원: 눌림·흐름 정밀값 보강", "정밀"),
        ("4) 1차 직원: 표준값 병합/커버리지 확인", "병합"),
        ("5) 3~5차 직원: 재돌파·실전위험·등급분류", "후보분류"),
        ("6) 6차 직원: ATR/VWAP/시장장세 최종진입검증", "최종검증"),
        ("7) 공장: latest 먼저 저장(archive 지연)", "공장"),
        ("8) 공장: 후보 snapshot 단일 원천 + WS/micro target 최종쓰기", "snapshot"),
    ]:
        s = s.replace(old, new)
    return s[:20]


def _v228_scan_stage_summary_line(limit: int = 4) -> str:
    try:
        arr = STATE.get("stage_times") if isinstance(STATE.get("stage_times"), list) else []
        if not arr:
            return "- 단계요약: 아직 없음"
        parts = []
        for item in arr[:8]:
            try:
                name, sec, _note = item
            except Exception:
                continue
            parts.append((_v228_short_stage_name(name), fnum(sec, 0.0)))
        if not parts:
            return "- 단계요약: 아직 없음"
        total = sum(sec for _n, sec in parts)
        slow = sorted(parts, key=lambda x: x[1], reverse=True)[:max(1, limit)]
        slow_txt = " / ".join(f"{n} {sec:.1f}s" for n, sec in slow)
        return f"- 단계요약: {slow_txt} / 합계 {total:.1f}s"
    except Exception as exc:
        log_error("v228_stage_summary", exc)
        return "- 단계요약: 확인 실패"


def _v228_ws_current_tickers() -> List[str]:
    tickers: List[str] = []
    try:
        strict, _shadow, _snap = _v212_snapshot_rows()
        for r in strict or []:
            t = _ticker_from_any((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
            if t and t not in tickers and t not in STABLE_EXCLUDED:
                tickers.append(t)
    except Exception:
        pass
    return tickers


def _v228_ws_target_cache_diag() -> Dict[str, Any]:
    """현재 후보가 WS target/cache/fresh 중 어디서 끊기는지 보기 위한 진단. 조건 판단에는 쓰지 않는다."""
    out: Dict[str, Any] = {
        "current": 0, "target_in": 0, "target_out": 0, "cache_row": 0,
        "fresh": 0, "stale": 0, "missing": 0, "seq": "-", "reconnect_age": "-",
        "reason": "-", "sample_target_out": [], "sample_no_cache": [], "sample_stale": [],
    }
    try:
        tickers = _v228_ws_current_tickers()
        p = _load_ws_target_payload()
        targets = [_ticker_from_any(x) for x in (p.get("targets") or []) if _ticker_from_any(x)] if isinstance(p, dict) else []
        target_set = set(targets)
        out["current"] = len(tickers)
        out["seq"] = (p.get("reconnect_seq", "-") if isinstance(p, dict) else "-")
        out["reason"] = str((p.get("reconnect_reason") or p.get("reason") or "-") if isinstance(p, dict) else "-")
        lf = fnum((p.get("last_force_reconnect_ts") if isinstance(p, dict) else 0), 0.0)
        out["reconnect_age"] = round(max(0.0, now_ts() - lf), 1) if lf > 0 else "-"
        for t in tickers:
            in_target = t in target_set
            if in_target:
                out["target_in"] += 1
            else:
                out["target_out"] += 1
                if len(out["sample_target_out"]) < 5:
                    out["sample_target_out"].append(t)
            try:
                ws = ws_snapshot(t)
                st = str(ws.get("ws_row_status") or "missing")
                age = fnum(ws.get("ws_age_sec", ws.get("live_age_sec", -1)), -1)
                if st != "missing":
                    out["cache_row"] += 1
                if st == "fresh":
                    out["fresh"] += 1
                elif st == "stale":
                    out["stale"] += 1
                    if len(out["sample_stale"]) < 5:
                        out["sample_stale"].append(f"{t} {age:.1f}s")
                else:
                    out["missing"] += 1
                    if in_target and len(out["sample_no_cache"]) < 5:
                        out["sample_no_cache"].append(t)
            except Exception:
                out["missing"] += 1
        return out
    except Exception as exc:
        log_error("v228_ws_diag", exc)
        return out


_v228_base_external_status_text = _v210_external_status_text

def _v210_external_status_text(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v228_base_external_status_text(snap)
    try:
        d = _v228_ws_target_cache_diag()
        if int(d.get("current", 0) or 0) > 0:
            samples = []
            if d.get("sample_target_out"):
                samples.append("target밖 " + ", ".join(d.get("sample_target_out") or []))
            if d.get("sample_no_cache"):
                samples.append("cache없음 " + ", ".join(d.get("sample_no_cache") or []))
            if d.get("sample_stale"):
                samples.append("stale " + ", ".join(d.get("sample_stale") or []))
            sample_txt = " / ".join(samples) if samples else "-"
            txt += (
                f"\n\n[WS target/cache 진단]\n"
                f"- 현재후보 {d.get('current',0)}개 / target포함 {d.get('target_in',0)} / target밖 {d.get('target_out',0)} / cache있음 {d.get('cache_row',0)}\n"
                f"- fresh {d.get('fresh',0)} / stale {d.get('stale',0)} / missing {d.get('missing',0)}\n"
                f"- reconnect seq {d.get('seq','-')} / age {d.get('reconnect_age','-')}초 / reason {d.get('reason','-')}\n"
                f"- 예시: {sample_txt}\n"
                f"- 용도: WS가 target 문제인지, tick 미수신/cache 문제인지 구분. 매수조건 아님"
            )
    except Exception:
        pass
    return txt


_v228_base_health_builder = _v210_health_text

def _v210_health_text() -> str:  # type: ignore[override]
    txt = _v228_base_health_builder()
    try:
        # [1/5] 메인봇 블록 바로 뒤에 짧은 단계요약을 넣는다.
        line = _v228_scan_stage_summary_line()
        if "[2/5] 서버자원" in txt:
            txt = txt.replace("\n\n[2/5] 서버자원", "\n" + line + "\n\n[2/5] 서버자원", 1)
        else:
            txt += "\n" + line
    except Exception:
        pass
    return (txt.replace("수익형 v2.13.228", "수익형 v2.13.231")
               .replace("수익형 v2.13.227", "수익형 v2.13.231")
               .replace("수익형 v2.13.226", "수익형 v2.13.231"))


_v228_base_quality_text_builder = _v216_quality_text

def _v216_quality_text() -> str:  # type: ignore[override]
    txt = _v228_base_quality_text_builder()
    return (txt.replace("v228 기본은 캐시 전용", "v231 기본은 캐시 전용")
               .replace("v227 기본은 캐시 전용", "v231 기본은 캐시 전용")
               .replace("v226 기본은 캐시 전용", "v231 기본은 캐시 전용")
               .replace("v222 기본은 캐시 전용", "v231 기본은 캐시 전용"))



# ===============================
# v2.13.230: quality 출력 연결 + snapshot 병목 캐시 + paper 시간표시 연동
# - 조건/청산/BUY_READY/자동매수 변경 없음
# - 구 _v210 quality 캐시 경로가 다시 물리지 않도록 현재 builder를 최종 고정
# - target 계산 중 반복 파일읽기(open/recent candidate)를 짧은 TTL 캐시로 줄인다.
# ===============================

_V230_CACHE_TTL_SEC = float(os.getenv("CLEAN_V230_TARGET_CACHE_TTL_SEC", "1.5"))
_V230_RECENT_PRIORITY_CACHE: Dict[str, Any] = {"ts": 0.0, "limit": 0, "rows": []}
_V230_OPEN_TICKER_CACHE: Dict[str, Any] = {"ts": 0.0, "limit": 0, "rows": []}
_v230_base_recent_candidate_priority_rows = recent_candidate_priority_rows
_v230_base_paper_open_tickers = _paper_open_tickers


def recent_candidate_priority_rows(limit: int = 80) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v230: 한 scan/snapshot 안에서 latest 후보파일을 반복 tail 하지 않는다."""
    try:
        nowv = now_ts()
        if (_V230_RECENT_PRIORITY_CACHE.get("rows") is not None
                and int(_V230_RECENT_PRIORITY_CACHE.get("limit", 0) or 0) >= int(limit)
                and nowv - fnum(_V230_RECENT_PRIORITY_CACHE.get("ts"), 0.0) <= _V230_CACHE_TTL_SEC):
            return list(_V230_RECENT_PRIORITY_CACHE.get("rows") or [])[:limit]
        rows = _v230_base_recent_candidate_priority_rows(limit)
        _V230_RECENT_PRIORITY_CACHE.update({"ts": nowv, "limit": limit, "rows": list(rows or [])})
        return rows
    except Exception as exc:
        log_error("v230_recent_candidate_priority_rows", exc)
        return _v230_base_recent_candidate_priority_rows(limit)


def _paper_open_tickers(limit: int = 40) -> List[str]:  # type: ignore[override]
    """v230: target writer가 paper_open 장부를 여러 번 읽는 병목을 줄인다."""
    try:
        nowv = now_ts()
        if (_V230_OPEN_TICKER_CACHE.get("rows") is not None
                and int(_V230_OPEN_TICKER_CACHE.get("limit", 0) or 0) >= int(limit)
                and nowv - fnum(_V230_OPEN_TICKER_CACHE.get("ts"), 0.0) <= _V230_CACHE_TTL_SEC):
            return list(_V230_OPEN_TICKER_CACHE.get("rows") or [])[:limit]
        rows = _v230_base_paper_open_tickers(limit)
        _V230_OPEN_TICKER_CACHE.update({"ts": nowv, "limit": limit, "rows": list(rows or [])})
        return rows
    except Exception as exc:
        log_error("v230_paper_open_tickers", exc)
        return _v230_base_paper_open_tickers(limit)


def _v230_str_clean(v: Any) -> str:
    return str(v or "").strip()


def _v230_is_trade_ready(row: Dict[str, Any]) -> bool:
    action = str((row or {}).get("final_entry_action") or "")
    return bool((row or {}).get("paper_bot_open") or (row or {}).get("open_eligible") or (row or {}).get("trade_ready") or action in {"paper_open", "trade_ready", "open"})


def _v230_candidate_grade(row: Dict[str, Any]) -> str:
    r = row or {}
    action = str(r.get("final_entry_action") or "")
    label = str(r.get("final_entry_label") or r.get("candidate_grade_label") or r.get("quality_label") or "")
    if _v230_is_trade_ready(r):
        return "🧪 모의진입 가능"
    if action == "recheck_wait" or "재확인" in label or "실전위험" in label:
        return "⚠️ 실전위험 재확인"
    return "❌ 진입보류"


def _v230_hold_reason(row: Dict[str, Any]) -> str:
    """후보 성격 라벨을 보류 사유로 세지 않는다."""
    r = row or {}
    banned = {"", "-", "None", "null", "모의매매 검증 후보", "실전위험 재확인 필요", "최종검증 통과", "trade_ready", "paper_open", "OPEN"}
    for key in ("block_reasons", "final_entry_reasons", "execution_risk_flags", "quality_risk_tags", "aux_notes"):
        vals = r.get(key)
        if isinstance(vals, list):
            ss = [str(v).replace("관찰전환:", "").replace("재확인대기:", "").strip() for v in vals if str(v).strip()]
            ss = [s for s in ss if s not in banned and "모의매매 검증" not in s]
            if ss:
                return " / ".join(ss[:3])[:100]
    for key in ("hold_reason", "block_reason", "reject_reason"):
        s = _v230_str_clean(r.get(key))
        if s and s not in banned and "모의매매 검증" not in s:
            return s[:100]
    label = _v230_str_clean(r.get("final_entry_label") or r.get("quality_label"))
    if label and label not in banned and "모의매매 검증" not in label:
        if "재확인" in label or "실전위험" in label:
            return "실전위험 재확인 필요"
        return label[:100]
    return "보류 사유 미저장"


def _v230_candidate_counts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    out = {"total": len(rows or []), "trade_ready": 0, "recheck": 0, "observe": 0, "paper_open": 0, "final_pass": 0, "relative": Counter(), "hold": Counter(), "grade": Counter()}
    for r in rows or []:
        grade = _v230_candidate_grade(r)
        out["grade"][grade] += 1
        if _v230_is_trade_ready(r):
            out["trade_ready"] += 1
            out["final_pass"] += 1
        elif grade.startswith("⚠️"):
            out["recheck"] += 1
        else:
            out["observe"] += 1
        rel = str((r or {}).get("relative_strength_label") or (r or {}).get("relative_label") or "-")
        out["relative"][rel] += 1
        if not _v230_is_trade_ready(r):
            out["hold"][_v230_hold_reason(r)] += 1
    return out


def _v210_candidate_counts(rows: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    return _v230_candidate_counts(rows)


def _v230_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    merged = dict(ctx)
    for k, v in (row or {}).items():
        if k not in merged or merged.get(k) in (None, "", "-"):
            merged[k] = v
    return merged


def _v229_entry_bucket_from_ctx(row: Dict[str, Any], kind: str) -> str:  # type: ignore[override]
    ctx = _v230_ctx(row or {})
    if kind == "micro":
        st = str(ctx.get("micro_entry_status") or ctx.get("micro_row_status") or "").lower()
        if bool(ctx.get("micro_fresh")) or st == "fresh":
            return "fresh"
        if st in {"stale", "old", "오래됨"}:
            return "stale"
        return "missing"
    if kind == "ws":
        st = str(ctx.get("ws_entry_status") or ctx.get("ws_row_status") or "").lower()
        if bool(ctx.get("ws_fresh")) or st == "fresh":
            return "fresh"
        if st in {"stale", "old", "오래됨"}:
            return "stale"
        return "missing"
    if kind == "urgent":
        used = bool(ctx.get("urgent_recheck_used") or ctx.get("micro_urgent_requested"))
        if used:
            return "urgent_fresh" if _v229_entry_bucket_from_ctx(row, "micro") == "fresh" else "urgent_not_fresh"
        return "not_urgent"
    return "unknown"


def _v230_example_line(r: Dict[str, Any]) -> str:
    t = _ticker_from_any((r or {}).get("ticker") or (r or {}).get("market") or (r or {}).get("symbol"))
    grade = _v230_candidate_grade(r)
    reason = _v230_hold_reason(r) if not _v230_is_trade_ready(r) else str((r or {}).get("final_entry_label") or (r or {}).get("trade_ready_label") or "최종검증 통과")
    return f"- {t}: {grade} / 점수 {fnum((r or {}).get('score'),0):.2f} / 3분돈 {krw_m((r or {}).get('money_flow_3m') or (r or {}).get('turnover_3m'))} / 눌림 {fnum((r or {}).get('pullback_quality_score'),0):.2f} / 상대 {(r or {}).get('relative_strength_label','-')} / 사유 {reason[:36]}"


def _v216_quality_text() -> str:  # type: ignore[override]
    strict, shadow, snap_payload = _v212_snapshot_rows()
    counts = _v230_candidate_counts(strict)
    ext = _v210_external_snapshot_dict()
    closed = _v210_strict_rows(load_closed(V210_QUALITY_CLOSED_TAIL))
    rows3 = _v210_rows_last_hours(closed, 3)
    rows12 = _v210_rows_last_hours(closed, 12)
    cur = _v210_current_version_rows(closed)
    open_rows = [r for r in strict if _v230_is_trade_ready(r)][:2]
    recheck_rows = [r for r in strict if (not _v230_is_trade_ready(r)) and _v230_candidate_grade(r).startswith("⚠️")][:2]
    hold_rows = [r for r in strict if (not _v230_is_trade_ready(r)) and not _v230_candidate_grade(r).startswith("⚠️")][:2]
    hold = counts.get("hold") if isinstance(counts.get("hold"), Counter) else Counter()
    hold_txt = " / ".join(f"{k} {v}" for k, v in hold.most_common(4)) if hold else "-"
    rel = counts.get("relative") if isinstance(counts.get("relative"), Counter) else Counter()
    rel_txt = " / ".join(f"{k} {v}" for k, v in rel.most_common(4)) if rel else "-"
    grade = counts.get("grade") if isinstance(counts.get("grade"), Counter) else Counter()
    grade_txt = " / ".join(f"{k} {v}" for k, v in grade.most_common(4)) if grade else "-"
    age = ext.get("snapshot_age_sec", "-")
    lines = [
        "🔬 후보품질 요약 /quality",
        "- v231 기본은 캐시 전용: 현재 후보는 clean_candidate_snapshot.json 단일 원천만 표시",
        "- 최근 3시간은 버전 섞임 가능. 최대수익 제외 줄로 한 방 착시를 같이 봅니다.",
        "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full",
        "",
        "[1/5] 성과 요약",
        _compact_stat_line("최근 3시간 전체 정식(버전 섞임)", rows3),
    ]
    lines += _v216_outlier_brief(rows3, "최근 3시간")
    lines.append(_compact_stat_line("최근 12시간 전체 정식(버전 섞임)", rows12))
    lines.append(_compact_stat_line("현재버전 정식", cur))
    lines.append(f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}")
    lines += _v229_external_performance_lines(rows3, cur)
    lines += [
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(strict)}개 / 🧪 모의진입 {counts.get('trade_ready',0)}개 / ⚠️ 재확인 {counts.get('recheck',0)}개 / ❌ 진입보류 {counts.get('observe',0)}개 / 복기 {len(shadow)}개 / snapshot {age}초 전",
        f"- 최종검증: 통과 {counts.get('final_pass',0)} / 재확인 {counts.get('recheck',0)} / 관찰 {counts.get('observe',0)}",
        f"- 웹소켓: 신선 {ext.get('ws_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('ws_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('ws_stale',0)} / 없음 {ext.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {ext.get('micro_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('micro_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('micro_stale',0)} / 없음 {ext.get('micro_missing',0)}",
        f"- 후보성격: {grade_txt}",
        f"- 상대강도: {rel_txt}",
        f"- 진입 보류 사유: {hold_txt}",
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
    ]
    lines += [_v230_example_line(r) for r in open_rows] or ["- 없음"]
    lines += ["⚠️ 재확인"]
    lines += [_v230_example_line(r) for r in recheck_rows] or ["- 없음"]
    lines += ["❌ 진입보류"]
    lines += [_v230_example_line(r) for r in hold_rows] or ["- 없음"]
    lines += ["", "[4/5] 최근 12시간 종료 사유", *_v210_reason_lines(rows12, limit=4)]
    lines += ["", "[5/5] 판독"]
    st12 = score_stats(rows12)
    lines.append("❌ 전체 누적은 아직 자동매매 불가" if st12.get("avg", 0) < 0 else "⚠️ 표본 확인 필요")
    lines += ["⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결", "✅ 다음 판단: micro fresh/stale 성과 차이와 지지부진 초반반응 0% 반복 여부"]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        save_json(FILES["quality_summary"], _cache_payload(_v216_quality_text(), "quality"))
    except Exception as exc:
        log_error("v230_quality_cache", exc)


def _v219_write_command_caches_after_snapshot(build_quality: bool = False) -> None:  # type: ignore[override]
    """v230 final: post-refresh에서는 light cache만 매번, quality는 마지막/성공 때만 현재 builder로 저장."""
    try:
        ext_snap = _v210_external_snapshot_dict()
        save_json(FILES["external_snapshot"], _cache_payload(_v210_external_status_text(ext_snap), "external_status"))
        save_json(FILES["health_snapshot"], _cache_payload(_v210_health_text(), "health"))
    except Exception as exc:
        log_error("v230_refresh_light_cache_final", exc)
    if build_quality:
        _build_quality_cache()


def health_text() -> str:  # type: ignore[override]
    return _v210_health_text()



# ===============================
# v2.13.231: observation cache 통일 + post-refresh CPU 겹침 완화
# - /health, /external_status, /quality가 같은 candidate_snapshot 기준을 보게 한다.
# - post-refresh 중간 단계에서 light cache만 따로 갱신해 숫자가 갈리는 경로를 끊는다.
# - quality/성과 계산은 같은 snapshot_id 기준으로 묶어 저장한다.
# ===============================
_V231_OBS_CACHE_LOCK = threading.RLock()
_V231_LAST_OBS_CACHE: Dict[str, Any] = {"scan_id": "", "snapshot_ts": 0.0, "updated_ts": 0.0}
_V231_LAST_QUALITY_SCAN_ID = ""


def _v231_snapshot_identity() -> Tuple[str, float]:
    try:
        snap = _v212_read_candidate_snapshot()
        return str(snap.get("scan_id") or ""), fnum(snap.get("updated_ts"), 0.0)
    except Exception:
        return "", 0.0


def _v231_cache_payload(text: str, name: str, *, snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    return {
        "version": BOT_VERSION,
        "name": name,
        "updated_ts": now_ts(),
        "updated_text": now_text(),
        "snapshot_scan_id": str((snap or {}).get("scan_id") or ""),
        "snapshot_updated_ts": fnum((snap or {}).get("updated_ts"), 0.0) if isinstance(snap, dict) else 0.0,
        "snapshot_stage": str((snap or {}).get("stage") or "") if isinstance(snap, dict) else "",
        "text": str(text or ""),
    }


def _v231_build_observation_caches(*, build_quality: bool = True, reason: str = "manual") -> None:
    """같은 snapshot에서 external/health/quality를 한 번에 만든다."""
    global _V231_LAST_QUALITY_SCAN_ID
    with _V231_OBS_CACHE_LOCK:
        snap = _v212_read_candidate_snapshot()
        ext_snap = _v210_external_snapshot_dict()
        try:
            save_json(FILES["external_snapshot"], _v231_cache_payload(_v210_external_status_text(ext_snap), "external_status", snap=snap))
            save_json(FILES["health_snapshot"], _v231_cache_payload(_v210_health_text(), "health", snap=snap))
        except Exception as exc:
            log_error("v231_observation_light_cache", exc)
        if build_quality:
            try:
                save_json(FILES["quality_summary"], _v231_cache_payload(_v216_quality_text(), "quality", snap=snap))
                _V231_LAST_QUALITY_SCAN_ID = str((snap or {}).get("scan_id") or "")
            except Exception as exc:
                log_error("v231_observation_quality_cache", exc)
        _V231_LAST_OBS_CACHE.update({
            "scan_id": str((snap or {}).get("scan_id") or ""),
            "snapshot_ts": fnum((snap or {}).get("updated_ts"), 0.0) if isinstance(snap, dict) else 0.0,
            "updated_ts": now_ts(),
            "reason": reason,
            "quality": bool(build_quality),
        })
        with _state_lock:
            STATE["v231_observation_cache_scan_id"] = _V231_LAST_OBS_CACHE.get("scan_id", "")
            STATE["v231_observation_cache_reason"] = reason
            STATE["v231_observation_quality_built"] = bool(build_quality)


def _build_light_command_caches() -> None:  # type: ignore[override]
    """v231: light/quality 숫자 불일치를 줄이기 위해 snapshot 변경 시 quality도 같이 갱신한다."""
    try:
        _write_resource_status()
    except Exception as exc:
        log_error("v231_resource_cache", exc)
    try:
        scan_id, snap_ts = _v231_snapshot_identity()
        need_quality = bool(scan_id and scan_id != _V231_LAST_QUALITY_SCAN_ID)
        _v231_build_observation_caches(build_quality=need_quality, reason="command_worker_light_same_snapshot")
    except Exception as exc:
        log_error("v231_light_cache", exc)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        _v231_build_observation_caches(build_quality=True, reason="command_worker_quality")
    except Exception as exc:
        log_error("v231_quality_cache", exc)


def _v219_write_command_caches_after_snapshot(build_quality: bool = False) -> None:  # type: ignore[override]
    """v231: post-refresh 중간 단계에서 external만 갱신해 quality와 숫자가 갈리는 경로를 제거한다."""
    try:
        if build_quality:
            _v231_build_observation_caches(build_quality=True, reason="post_refresh_final_same_snapshot")
        else:
            # 중간 refresh는 candidate_snapshot/paper_latest만 갱신한다. 명령어 cache는 마지막 refresh에서 한 번만 통일 저장.
            with _state_lock:
                STATE["v231_post_refresh_mid_cache_skip"] = True
    except Exception as exc:
        log_error("v231_refresh_cache", exc)



# ===============================
# v2.13.233: 청산모드 기준 관찰 + 시간표시 보강 연계
# - 조건/청산/BUY_READY/자동매수 변경 없음.
# - /external_status와 /quality는 같은 snapshot row에서 external 숫자를 계산한다.
# - 공장/snapshot overlay는 WS/micro map 1회 재사용으로 줄인다.
# - 후보품질은 손절/지지부진 2개 축을 기본 /quality에 짧게 표시한다.
# ===============================

def _v233_external_from_snapshot(snap: Optional[Dict[str, Any]] = None, rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    rows = rows if isinstance(rows, list) else [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    ext0 = (snap or {}).get("external") if isinstance((snap or {}).get("external"), dict) else {}
    ext = _v212_count_external(rows)
    out = dict(ext0)
    out.update(ext)
    out.setdefault("updated_ts", (snap or {}).get("updated_ts", now_ts()))
    out["snapshot_age_sec"] = round(now_ts() - fnum((snap or {}).get("updated_ts"), now_ts()), 1)
    out["snapshot_scan_id"] = (snap or {}).get("scan_id", "-")
    out["snapshot_stage"] = (snap or {}).get("stage", "-")
    out["target"] = (snap or {}).get("target", {}) if isinstance((snap or {}).get("target"), dict) else {}
    out["count_source"] = "v233_same_snapshot_rows"
    return out


def _v210_external_snapshot_dict() -> Dict[str, Any]:  # type: ignore[override]
    """v233: external_status와 quality가 같은 snapshot row 기준으로 fresh 숫자를 계산한다."""
    snap = _v212_read_candidate_snapshot()
    if not isinstance(snap, dict) or not snap:
        return {
            "updated_ts": now_ts(), "total": 0,
            "ws_fresh": 0, "ws_stale": 0, "ws_missing": 0, "ws_targeted": 0,
            "micro_fresh": 0, "micro_stale": 0, "micro_missing": 0, "micro_targeted": 0,
            "examples": [], "snapshot_missing": True, "count_source": "v233_missing_snapshot",
        }
    rows = [r for r in (snap.get("rows") or []) if isinstance(r, dict)]
    return _v233_external_from_snapshot(snap, rows)


def _v233_overlay_row_with_maps(row: Dict[str, Any], maps: Dict[str, Any]) -> Dict[str, Any]:
    rr = dict(row or {})
    t = _v212_ticker(rr)
    try:
        ws = _v218_ws_snapshot_from_maps(t, maps)
        rr.update(ws)
        rest_price = fnum(rr.get("current_price") or rr.get("entry_price") or rr.get("detected_price"), 0.0)
        live = fnum(ws.get("live_price"), 0.0)
        if rest_price > 0 and live > 0:
            rr["ws_price"] = live
            rr["current_price_ws_gap_pct"] = round(((live - rest_price) / rest_price) * 100.0, 3)
    except Exception as exc:
        log_error("v233_overlay_ws", exc)
    try:
        rr.update(_v218_micro_snapshot_from_maps(t, maps))
    except Exception as exc:
        log_error("v233_overlay_micro", exc)
    rr["ticker"] = t or rr.get("ticker")
    rr["snapshot_overlay_ts"] = now_ts()
    rr["external_refreshed_by"] = "v233_one_map_overlay"
    try:
        rr = _apply_relative_strength_context(rr)
    except Exception as exc:
        log_error("v233_overlay_relative", exc)
    try:
        rr = _v229_apply_urgent_marker(rr, maps)
    except Exception as exc:
        log_error("v233_overlay_urgent_marker", exc)
    return rr


def _overlay_current_external_for_items(items: List[Dict[str, Any]], *, refresh: bool = True) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v233: 공장/표시 overlay는 WS/micro 파일을 row마다 보지 않고 map 1회만 쓴다."""
    try:
        maps = _v218_refresh_external_maps(force=bool(refresh), ttl=0.8)
    except Exception as exc:
        log_error("v233_overlay_maps", exc)
        maps = _v218_refresh_external_maps(force=False, ttl=2.0)
    out: List[Dict[str, Any]] = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        rr = _v233_overlay_row_with_maps(r, maps)
        # entry_context도 최신 외부정보 표시값과 맞춘다. 조건/청산 판단은 바꾸지 않는다.
        ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
        ctx = dict(ctx)
        for k in ("ws_row_status", "ws_age_sec", "ws_targeted", "ws_cache_ts", "current_price_ws_gap_pct", "micro_fresh", "micro_row_status", "micro_age_sec", "micro_targeted", "micro_spread_pct", "micro_trade_buy_ratio_30"):
            if k in rr:
                ctx[k] = rr.get(k)
        ctx["external_overlay_at"] = now_ts()
        ctx["external_overlay_note"] = "v233_one_map_overlay_before_display_or_export"
        rr["entry_context"] = ctx
        out.append(rr)
    return out


def _v212_snapshot_payload(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:  # type: ignore[override]
    maps = _v218_refresh_external_maps(force=True, ttl=0.0)
    ordered = _v212_order_candidates(strict_rows)
    rows = [_v233_overlay_row_with_maps(r, maps) for r in ordered]
    ext = _v233_external_from_snapshot({"updated_ts": now_ts(), "rows": rows, "scan_id": str(STATE.get("scan_id") or "-")}, rows)
    nowv = now_ts()
    return {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v233",
        "stage": stage,
        "source": source,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or "-"),
        "scan_seq": int(fnum(STATE.get("scan_seq"), 0)),
        "candidate_count": len(rows),
        "shadow_count": len(shadow_rows or []),
        "rows": rows,
        "external": {
            **ext,
            "ws_worker_state": STATE.get("ws_state", "-"),
            "ws_worker_targets": STATE.get("ws_targets", 0),
            "ws_worker_fresh": STATE.get("ws_fresh", 0),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
            "micro_worker_state": STATE.get("micro_state", "-"),
            "micro_worker_targets": STATE.get("micro_targets", 0),
            "micro_worker_fresh": STATE.get("micro_fresh", 0),
            "overlay_mode": "v233_one_map_overlay",
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2),
        },
        "target": {
            "writer": "factory_export_final_only",
            "reason": "factory_final_candidates",
            "ws_target_count": STATE.get("ws_target_file_targets", 0),
            "micro_target_count": STATE.get("micro_target_file_targets", 0),
            "ws_target_reason": STATE.get("ws_target_reason", "-"),
            "micro_target_reason": STATE.get("micro_target_reason", "-"),
            "wait_sec": wait_sec,
            "priority_rule": "scan_candidates_first_then_paper_open_recent_hot",
        },
        "note": "v233: snapshot overlay uses one WS/micro map; external counts are recalculated from the same snapshot rows",
    }


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v233: snapshot은 방금 만든 후보 메모리 rows를 우선 사용한다. latest 파일 재읽기 경로를 줄인다."""
    strict_rows = [r for r in (strict or []) if isinstance(r, dict)]
    shadow_rows = [r for r in (shadow or []) if isinstance(r, dict)]
    # 방금 scan 후보가 비어 있을 때만 latest 파일을 보조로 읽는다.
    if not strict_rows:
        strict_rows, shadow_rows = _v212_rows_from_latest()
    update_ws_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    update_micro_targets(market_rows or [], priority_rows=strict_rows, reason="factory_final_candidates")
    wait_sec = max(0.0, min(float(os.getenv("CLEAN_V232_SNAPSHOT_WAIT_SEC", os.getenv("CLEAN_V219_SNAPSHOT_WAIT_SEC", "0.2"))), 0.8))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    final = _v212_write_candidate_snapshot(strict_rows, shadow_rows, stage="final_after_target_overlay", source="factory_memory_rows_v233", wait_sec=wait_sec)
    scan_id = str(final.get("scan_id") or STATE.get("scan_id") or "") if isinstance(final, dict) else str(STATE.get("scan_id") or "")
    _v219_schedule_external_refresh(scan_id)
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    with _state_lock:
        STATE["v233_snapshot_memory_rows"] = True
        STATE["v233_snapshot_initial_wait_sec"] = wait_sec
    return f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / wait {wait_sec:.1f}s / target_writer factory_final_candidates / v233 one-map snapshot"


def _v233_reason_group(row: Dict[str, Any]) -> str:
    r = str((row or {}).get("exit_reason") or (row or {}).get("reason") or "").lower()
    if "stop" in r or "손절" in r:
        return "stop"
    if "slow" in r or "no_progress" in r or "지지" in r or "부진" in r:
        return "slow"
    return r or "unknown"


def _v233_entry_value(row: Dict[str, Any], key: str, default: Any = None) -> Any:
    if key in (row or {}) and (row or {}).get(key) is not None:
        return (row or {}).get(key)
    ctx = (row or {}).get("entry_context") if isinstance((row or {}).get("entry_context"), dict) else {}
    return ctx.get(key, default)


def _v233_avg(rows: List[Dict[str, Any]], key: str) -> float:
    vals = [fnum(_v233_entry_value(r, key), 0.0) for r in rows or [] if _v233_entry_value(r, key) not in (None, "")]
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def _v233_problem_sample(rows: List[Dict[str, Any]], limit: int = 2) -> List[str]:
    if not rows:
        return ["  · 예시 없음"]
    out: List[str] = []
    ordered = sorted(rows, key=lambda r: fnum(r.get("pnl_pct"), 0.0))[:limit]
    for r in ordered:
        t = _ticker_from_any(r.get("ticker") or r.get("market") or r.get("symbol")) or "-"
        pnl = fnum(r.get("pnl_pct"), 0.0)
        peak = fnum(r.get("peak_pct"), 0.0)
        hold = str(r.get("hold_text") or (f"{fnum(r.get('age_min'),0):.1f}분" if fnum(r.get("age_min"),0)>0 else "-"))
        score = fnum(_v233_entry_value(r, "score", r.get("score")), 0.0)
        money3 = _v233_entry_value(r, "money_flow_3m", r.get("money_flow_3m"))
        pull = fnum(_v233_entry_value(r, "pullback_quality_score", r.get("pullback_quality_score")), 0.0)
        micro = str(_v233_entry_value(r, "micro_row_status", r.get("micro_entry_status") or r.get("micro_row_status") or "-"))
        ws = str(_v233_entry_value(r, "ws_row_status", r.get("ws_entry_status") or r.get("ws_row_status") or "-"))
        spread = fnum(_v233_entry_value(r, "micro_spread_pct", r.get("micro_spread_pct")), 0.0)
        buy = fnum(_v233_entry_value(r, "micro_trade_buy_ratio_30", r.get("micro_trade_buy_ratio_30")), 0.0)
        out.append(f"  · {t}: {pnl:+.2f}% / 최고 {peak:+.2f}% / 보유 {hold} / 점수 {score:.2f} / 3분돈 {krw_m(money3)} / 눌림 {pull:.2f} / micro {micro} 스프레드 {spread:.2f}% 매수비 {buy:.2f} / WS {ws}")
    return out


def _v233_problem_block(cur_rows: List[Dict[str, Any]]) -> List[str]:
    stop = [r for r in cur_rows or [] if _v233_reason_group(r) == "stop"]
    slow = [r for r in cur_rows or [] if _v233_reason_group(r) == "slow"]
    out = ["", "[1-3] 후보품질 2대 문제(현재버전, 조건변경 아님)"]
    for label, arr in (("손절", stop), ("지지부진", slow)):
        st = score_stats(arr)
        peak_low = sum(1 for r in arr if fnum(r.get("peak_pct"), 0.0) <= 0.10)
        avg_peak = _v233_avg(arr, "peak_pct")
        avg_money = _v233_avg(arr, "money_flow_3m")
        avg_pull = _v233_avg(arr, "pullback_quality_score")
        avg_spread = _v233_avg(arr, "micro_spread_pct")
        out.append(f"⚠️ {label}: {st.get('n',0)}전 / 합산 {st.get('total',0):+.2f}% / 평균 {st.get('avg',0):+.2f}% / 최고수익≤0.10% {peak_low}건 / 평균최고 {avg_peak:+.2f}%")
        out.append(f"  · 진입평균: 3분돈 {krw_m(avg_money)} / 눌림 {avg_pull:.2f} / micro스프레드 {avg_spread:.2f}%")
        out.extend(_v233_problem_sample(arr, 2))
    out.append("- 용도: 후보품질 원인 확인용. 이 블록은 조건/청산을 바꾸지 않습니다.")
    return out




def _v233_entry_bool(row: Dict[str, Any], *keys: str) -> bool:
    for k in keys:
        v = _v233_entry_value(row, k, None)
        if isinstance(v, bool):
            return v
        if str(v).lower() in {"true", "fresh", "1", "yes", "ok"}:
            return True
    return False


def _v233_exit_mode_infer(row: Dict[str, Any]) -> Dict[str, Any]:
    """v235: CLOSED를 더먹기/빠른손절/반등실패/지지부진 후보로 분류한다."""
    reason = _v233_exit_reason_bucket(row)
    raw_reason = str((row or {}).get("exit_reason") or "")
    pnl = fnum(row.get("pnl_pct"), 0)
    peak = fnum(row.get("peak_pct"), 0)
    score = fnum(_v233_entry_value(row, "score", row.get("score")), 0)
    money3 = fnum(_v233_entry_value(row, "money_flow_3m", row.get("money_flow_3m")), 0)
    pull = fnum(_v233_entry_value(row, "pullback_quality_score", row.get("pullback_quality_score")), 0)
    spread = fnum(_v233_entry_value(row, "micro_spread_pct", row.get("micro_spread_pct")), 999)
    buy = fnum(_v233_entry_value(row, "micro_trade_buy_ratio_30", row.get("micro_trade_buy_ratio_30")), 0)
    wall = fnum(_v233_entry_value(row, "micro_bid_ask_wall_ratio", row.get("micro_bid_ask_wall_ratio")), 0)
    micro_fresh = bool(_v233_entry_value(row, "micro_fresh", row.get("micro_fresh")))
    ws_fresh = bool(_v233_entry_value(row, "ws_fresh", row.get("ws_fresh"))) or str(_v233_entry_value(row, "ws_row_status", row.get("ws_entry_status"))).lower() == "fresh"
    age = fnum(row.get("age_min"), 0.0)
    drawdown = max(0.0, peak - pnl)
    strong_hits = []
    if score >= 4.45: strong_hits.append("점수")
    if money3 >= 20_000_000: strong_hits.append("3분돈")
    if pull >= 1.8: strong_hits.append("눌림")
    if micro_fresh and spread <= 0.08: strong_hits.append("호가")
    if buy >= 0.62: strong_hits.append("매수비")
    if wall >= 1.5: strong_hits.append("매수벽")
    if ws_fresh: strong_hits.append("WS")
    weak_hits = []
    if peak <= 0.10: weak_hits.append("초반최고≤0.10")
    if 0.10 < peak <= 0.55 and drawdown >= 0.70: weak_hits.append("반등실패")
    if spread >= 0.25 and spread < 900: weak_hits.append("스프레드")
    if buy and buy < 0.45: weak_hits.append("매수비약")
    if reason == "stop": weak_hits.append("손절")
    if reason == "slow": weak_hits.append("지지부진")
    if raw_reason == "bounce_fail":
        mode = "bounce_fail_candidate"; label = "반등실패 조기손절"
    elif reason in {"take_profit", "protect_stop_after_tp"} and len(strong_hits) >= 5:
        mode = "eat_more_watch"; label = "더먹기 관찰"
    elif reason == "stop" and peak <= 0.10:
        mode = "quick_cut_candidate"; label = "빠른손절 후보"
    elif reason == "stop" and 0.10 < peak <= 0.55 and drawdown >= 0.70:
        mode = "bounce_fail_candidate"; label = "반등실패 조기손절"
    elif reason == "slow" and peak <= 0.10:
        mode = "slow_cut_candidate"; label = "지지부진 조기종료 후보"
    elif reason == "stop":
        mode = "stop_review"; label = "손절 원인검토"
    elif reason == "slow":
        mode = "slow_review"; label = "지지부진 원인검토"
    else:
        mode = "normal_review"; label = "기존청산 유지관찰"
    return {"mode": mode, "label": label, "strong_hits": strong_hits, "weak_hits": weak_hits, "peak": peak, "pnl": pnl, "age": age, "score": score, "money3": money3, "pull": pull, "spread": spread, "buy": buy, "wall": wall, "micro_fresh": micro_fresh, "ws_fresh": ws_fresh, "drawdown": drawdown}

def _v233_exit_sample(rows: List[Dict[str, Any]], limit: int = 2) -> List[str]:
    if not rows:
        return ["  · 예시 없음"]
    out: List[str] = []
    for r in rows[:limit]:
        m = _v233_exit_mode_infer(r)
        t = _ticker_from_any(r.get("ticker") or r.get("market") or r.get("symbol")) or "-"
        hold = str(r.get("hold_text") or (f"{fnum(r.get('age_min'),0):.1f}분" if fnum(r.get("age_min"),0)>0 else "-"))
        out.append(f"  · {t}: {m['label']} / 손익 {m['pnl']:+.2f}% / 최고 {m['peak']:+.2f}% / 보유 {hold} / 점수 {m['score']:.2f} / 3분돈 {krw_m(m['money3'])} / 눌림 {m['pull']:.2f} / 스프레드 {m['spread']:.2f}% / 매수비 {m['buy']:.2f} / 근거 {','.join(m['strong_hits'] or m['weak_hits'] or ['-'])}")
    return out


def _v233_exit_mode_block(cur_rows: List[Dict[str, Any]]) -> List[str]:
    rows = [r for r in cur_rows or [] if isinstance(r, dict)]
    inferred = [(r, _v233_exit_mode_infer(r)) for r in rows]
    eat = [r for r, m in inferred if m["mode"] == "eat_more_watch"]
    quick = [r for r, m in inferred if m["mode"] == "quick_cut_candidate"]
    bounce = [r for r, m in inferred if m["mode"] == "bounce_fail_candidate"]
    slow = [r for r, m in inferred if m["mode"] == "slow_cut_candidate"]
    out = ["", "[1-4] 청산모드 기준 후보(현재버전, 실제청산 변경 아님)"]
    out.append("- 기준: 진입 당시 강함 + 진입 후 반응. 실제 자동매수는 변경하지 않고 paper 청산모드 결과를 관찰합니다.")
    for label, arr, desc in (
        ("더먹기 관찰", eat, "+1.2% 이후 더 갔을 가능성 확인"),
        ("빠른손절 후보", quick, "초반 최고수익 없이 손절로 밀린 후보"),
        ("반등실패 조기손절", bounce, "+0.1~0.5% 반응 후 고점 대비 크게 되밀린 후보"),
        ("지지부진 조기종료 후보", slow, "최고수익 0.10% 이하로 시간만 쓴 후보"),
    ):
        st = score_stats(arr)
        avg_peak = round(sum(fnum(x.get("peak_pct"),0) for x in arr)/len(arr), 3) if arr else 0.0
        out.append(f"⚠️ {label}: {st.get('n',0)}건 / 합산 {st.get('total',0):+.2f}% / 평균 {st.get('avg',0):+.2f}% / 평균최고 {avg_peak:+.2f}% / {desc}")
        out.extend(_v233_exit_sample(arr, 2))
    out.append("- 다음 판단: 더먹기 후보가 반복되면 일부익절+추적, 빠른손절/반등실패/지지부진 후보가 반복되면 조기이탈 기준을 미세조정")
    return out

def _v233_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    strict = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    shadow = tail_jsonl(FILES.get("shadow_latest", BASE_DIR / "shadow_candidates_latest.jsonl"), max_lines=V212_SNAPSHOT_READ_MAX)
    shadow = [r for r in shadow if isinstance(r, dict)]
    counts = _v230_candidate_counts(strict)
    ext = _v233_external_from_snapshot(snap, strict)
    closed = _v210_strict_rows(load_closed(V210_QUALITY_CLOSED_TAIL))
    rows3 = _v210_rows_last_hours(closed, 3)
    rows12 = _v210_rows_last_hours(closed, 12)
    cur = _v210_current_version_rows(closed)
    open_rows = [r for r in strict if _v230_is_trade_ready(r)][:2]
    recheck_rows = [r for r in strict if (not _v230_is_trade_ready(r)) and _v230_candidate_grade(r).startswith("⚠️")][:2]
    hold_rows = [r for r in strict if (not _v230_is_trade_ready(r)) and not _v230_candidate_grade(r).startswith("⚠️")][:2]
    hold = counts.get("hold") if isinstance(counts.get("hold"), Counter) else Counter()
    hold_txt = " / ".join(f"{k} {v}" for k, v in hold.most_common(4)) if hold else "-"
    rel = counts.get("relative") if isinstance(counts.get("relative"), Counter) else Counter()
    rel_txt = " / ".join(f"{k} {v}" for k, v in rel.most_common(4)) if rel else "-"
    grade = counts.get("grade") if isinstance(counts.get("grade"), Counter) else Counter()
    grade_txt = " / ".join(f"{k} {v}" for k, v in grade.most_common(4)) if grade else "-"
    age = ext.get("snapshot_age_sec", "-")
    lines = [
        "🔬 후보품질 요약 /quality",
        "- v236 기본은 캐시 전용: 현재 후보는 clean_candidate_snapshot.json 단일 원천만 표시",
        "- 최근 3시간은 버전 섞임 가능. 최대수익 제외 줄로 한 방 착시를 같이 봅니다.",
        "- 긴 3/6/12 상세와 원자료성 비교는 /quality_full",
        "",
        "[1/5] 성과 요약",
        _compact_stat_line("최근 3시간 전체 정식(버전 섞임)", rows3),
    ]
    lines += _v216_outlier_brief(rows3, "최근 3시간")
    lines.append(_compact_stat_line("최근 12시간 전체 정식(버전 섞임)", rows12))
    lines.append(_compact_stat_line("현재버전 정식", cur))
    lines.append(f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}")
    lines += _v229_external_performance_lines(rows3, cur)
    lines += _v233_problem_block(cur)
    lines += _v233_exit_mode_block(cur)
    lines += [
        "",
        "[2/5] 현재 후보",
        f"- 정식 {len(strict)}개 / 🧪 모의진입 {counts.get('trade_ready',0)}개 / ⚠️ 재확인 {counts.get('recheck',0)}개 / ❌ 진입보류 {counts.get('observe',0)}개 / 복기 {len(shadow)}개 / snapshot {age}초 전",
        f"- 최종검증: 통과 {counts.get('final_pass',0)} / 재확인 {counts.get('recheck',0)} / 관찰 {counts.get('observe',0)}",
        f"- 웹소켓: 신선 {ext.get('ws_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('ws_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('ws_stale',0)} / 없음 {ext.get('ws_missing',0)}",
        f"- 호가·체결: 신선 {ext.get('micro_fresh',0)}/{ext.get('total',0)} / 대상 {ext.get('micro_targeted',0)}/{ext.get('total',0)} / 오래됨 {ext.get('micro_stale',0)} / 없음 {ext.get('micro_missing',0)}",
        f"- 후보성격: {grade_txt}",
        f"- 상대강도: {rel_txt}",
        f"- 진입 보류 사유: {hold_txt}",
        "",
        "[3/5] 후보 예시",
        "🧪 모의진입",
    ]
    lines += [_v230_example_line(r) for r in open_rows] or ["- 없음"]
    lines += ["⚠️ 재확인"]
    lines += [_v230_example_line(r) for r in recheck_rows] or ["- 없음"]
    lines += ["❌ 진입보류"]
    lines += [_v230_example_line(r) for r in hold_rows] or ["- 없음"]
    lines += ["", "[4/5] 최근 12시간 종료 사유", *_v210_reason_lines(rows12, limit=4)]
    lines += ["", "[5/5] 판독"]
    st12 = score_stats(rows12)
    lines.append("❌ 전체 누적은 아직 자동매매 불가" if st12.get("avg", 0) < 0 else "⚠️ 표본 확인 필요")
    lines += ["⚠️ 우선 볼 것: 손절 / 지지부진 감소", "✅ 확인할 재료: 3분 지속 돈흐름, 눌림품질, 실제 호가·체결", "✅ 다음 판단: 위 [1-3]에서 손절/지지부진의 공통 진입특징을 확인"]
    return "\n".join(lines)


def _v216_quality_text() -> str:  # type: ignore[override]
    return _v233_quality_text_from_snapshot(_v212_read_candidate_snapshot())


def _v231_build_observation_caches(*, build_quality: bool = True, reason: str = "manual") -> None:  # type: ignore[override]
    """v233: external/health/quality를 같은 snapshot payload 하나로 만든다."""
    global _V231_LAST_QUALITY_SCAN_ID
    with _V231_OBS_CACHE_LOCK:
        snap = _v212_read_candidate_snapshot()
        strict_rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
        ext_snap = _v233_external_from_snapshot(snap, strict_rows)
        try:
            save_json(FILES["external_snapshot"], _v231_cache_payload(_v210_external_status_text(ext_snap), "external_status", snap=snap))
            save_json(FILES["health_snapshot"], _v231_cache_payload(_v210_health_text(), "health", snap=snap))
        except Exception as exc:
            log_error("v233_observation_light_cache", exc)
        if build_quality:
            try:
                save_json(FILES["quality_summary"], _v231_cache_payload(_v233_quality_text_from_snapshot(snap), "quality", snap=snap))
                _V231_LAST_QUALITY_SCAN_ID = str((snap or {}).get("scan_id") or "")
            except Exception as exc:
                log_error("v233_observation_quality_cache", exc)
        _V231_LAST_OBS_CACHE.update({
            "scan_id": str((snap or {}).get("scan_id") or ""),
            "snapshot_ts": fnum((snap or {}).get("updated_ts"), 0.0) if isinstance(snap, dict) else 0.0,
            "updated_ts": now_ts(),
            "reason": reason,
            "quality": bool(build_quality),
        })
        with _state_lock:
            STATE["v233_observation_cache_scan_id"] = _V231_LAST_OBS_CACHE.get("scan_id", "")
            STATE["v233_observation_cache_reason"] = reason
            STATE["v233_observation_quality_built"] = bool(build_quality)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    txt = _read_cached_text(FILES["quality_summary"], "/quality")
    return (txt.replace("v234 기본은 캐시 전용", "v235 기본은 캐시 전용")
               .replace("v233 기본은 캐시 전용", "v235 기본은 캐시 전용")
               .replace("v231 기본은 캐시 전용", "v235 기본은 캐시 전용")
               .replace("v230 기본은 캐시 전용", "v235 기본은 캐시 전용")
               .replace("v222 기본은 캐시 전용", "v235 기본은 캐시 전용"))



# ===============================
# v2.13.235: 반등실패 조기손절 + 후보품질 시간요약 결과 반영
# - paper_bot_v0.48의 quick_stop / bounce_fail / slow_early_exit 종료사유를 메인 성과판에서 한글 표시한다.
# - paper latest scan_id '-' 방지를 위해 후보 latest 저장 직전 scan_id/snapshot_id를 top-level과 entry_context에 고정한다.
# - 진입조건/실제 자동매수/BUY_READY/v343 변경 없음.
# ===============================


# ===============================
# v2.13.236: paper_latest scan_id/TTL 단일화 수술
# - v229 post-refresh가 candidate_snapshot rows를 paper_latest에 덮어쓸 때 scan_id/TTL을 지우던 구경로를 차단한다.
# - snapshot rows도 같은 helper로 scan_id/snapshot_id/candidate_created_at을 보강한다.
# - 후보조건/청산조건/자동매수/BUY_READY/v343 변경 없음.
# ===============================

def _v236_latest_meta_value(row: Dict[str, Any], key: str, default: Any = "") -> Any:
    if not isinstance(row, dict):
        return default
    v = row.get(key)
    if v not in (None, "", "-"):
        return v
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    v = ctx.get(key)
    if v not in (None, "", "-"):
        return v
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    v = raw.get(key)
    if v not in (None, "", "-"):
        return v
    return default


def _v236_fix_candidate_meta(row: Dict[str, Any], *, scan_id: str = "", lane: str = "strict", refresh_ttl: bool = True, source: str = "v236") -> Dict[str, Any]:
    """paper_bot이 scan_id '-' / TTL 0으로 읽지 않게 latest row 메타를 한 곳에서 보강한다."""
    nowv = now_ts()
    rr = dict(row or {})
    sid = str(scan_id or _v236_latest_meta_value(rr, "scan_id", "") or STATE.get("scan_id") or f"scan-{int(nowv)}")
    if sid in {"", "-"}:
        sid = f"scan-{int(nowv)}"
    snapshot_id = str(_v236_latest_meta_value(rr, "snapshot_id", sid) or sid)
    created = fnum(_v236_latest_meta_value(rr, "candidate_created_at", 0), 0.0)
    if created <= 0:
        created = fnum(_v236_latest_meta_value(rr, "created_at", 0), 0.0)
    if created <= 0:
        created = fnum(_v236_latest_meta_value(rr, "source_created_at", 0), 0.0)
    if created <= 0 or refresh_ttl:
        created = nowv
    rr["scan_id"] = sid
    rr["snapshot_id"] = snapshot_id
    rr["candidate_created_at"] = created
    rr["created_at"] = created
    rr["source_created_at"] = created
    if refresh_ttl:
        rr["factory_saved_at"] = nowv
        rr["expires_at"] = nowv + max(30.0, CANDIDATE_TTL_SEC)
        rr["candidate_ttl_refreshed"] = True
    else:
        exp = fnum(_v236_latest_meta_value(rr, "expires_at", 0), 0.0)
        if exp <= 0:
            rr["expires_at"] = created + max(30.0, CANDIDATE_TTL_SEC)
    rr["lane"] = rr.get("lane") or lane
    rr["latest_meta_source"] = source
    ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx["scan_id"] = sid
    ctx["snapshot_id"] = snapshot_id
    ctx["candidate_created_at"] = created
    ctx["created_at"] = created
    ctx["expires_at"] = rr.get("expires_at")
    ctx["latest_meta_source"] = source
    rr["entry_context"] = ctx
    return rr


def _v212_snapshot_payload(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:  # type: ignore[override]
    """v236: snapshot rows에도 scan_id/snapshot_id를 보강하되, paper 최신 TTL은 post-refresh에서만 새로 찍는다."""
    maps = _v218_refresh_external_maps(force=True, ttl=0.0)
    scan_id = str(STATE.get("scan_id") or f"scan-{int(now_ts())}")
    ordered = _v212_order_candidates(strict_rows)
    rows = [_v236_fix_candidate_meta(_v233_overlay_row_with_maps(r, maps), scan_id=scan_id, lane="strict", refresh_ttl=False, source="v236_snapshot") for r in ordered]
    ext = _v233_external_from_snapshot({"updated_ts": now_ts(), "rows": rows, "scan_id": scan_id}, rows)
    nowv = now_ts()
    return {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v236",
        "stage": stage,
        "source": source,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": scan_id,
        "scan_seq": int(fnum(STATE.get("scan_seq"), 0)),
        "candidate_count": len(rows),
        "shadow_count": len(shadow_rows or []),
        "rows": rows,
        "external": {
            **ext,
            "ws_worker_state": STATE.get("ws_state", "-"),
            "ws_worker_targets": STATE.get("ws_targets", 0),
            "ws_worker_fresh": STATE.get("ws_fresh", 0),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
            "micro_worker_state": STATE.get("micro_state", "-"),
            "micro_worker_targets": STATE.get("micro_targets", 0),
            "micro_worker_fresh": STATE.get("micro_fresh", 0),
            "overlay_mode": "v236_one_map_overlay_meta_fixed",
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2),
        },
        "target": {
            "writer": "factory_export_final_only",
            "reason": "factory_final_candidates",
            "ws_target_count": STATE.get("ws_target_file_targets", 0),
            "micro_target_count": STATE.get("micro_target_file_targets", 0),
            "ws_target_reason": STATE.get("ws_target_reason", "-"),
            "micro_target_reason": STATE.get("micro_target_reason", "-"),
            "wait_sec": wait_sec,
            "priority_rule": "scan_candidates_first_then_paper_open_recent_hot",
        },
        "note": "v236: snapshot and paper_latest share scan_id/snapshot_id metadata; post-refresh cannot write TTL-less rows",
    }


def _v229_refresh_candidate_snapshot_once(expected_scan_id: str = "", *, attempt: int = 1, total_attempts: int = 1, waited_sec: float = 0.0) -> Tuple[bool, Dict[str, Any]]:  # type: ignore[override]
    """v236: 구 v229 post-refresh가 scan_id/TTL 없는 rows로 paper_latest를 덮는 경로를 수술한다."""
    snap = _v212_read_candidate_snapshot()
    if not isinstance(snap, dict) or not snap.get("rows"):
        return False, {}
    scan_id = str(snap.get("scan_id") or expected_scan_id or STATE.get("scan_id") or f"scan-{int(now_ts())}")
    if expected_scan_id and scan_id and scan_id != expected_scan_id:
        return False, {}
    rows0 = [r for r in (snap.get("rows") or []) if isinstance(r, dict)]
    maps = _v218_refresh_external_maps(force=True, ttl=0.0)
    nowv = now_ts()
    rows = [_v236_fix_candidate_meta(_v219_overlay_row_with_maps(r, maps), scan_id=scan_id, lane="strict", refresh_ttl=True, source="v236_post_refresh") for r in rows0]
    ext = _v212_count_external(rows)
    old_ext = snap.get("external") if isinstance(snap.get("external"), dict) else {}
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    refreshed = dict(snap)
    refreshed.update({
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v236",
        "stage": "external_refreshed_after_scan",
        "source": "v236_post_scan_meta_fixed_refresh",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": scan_id,
        "candidate_count": len(rows),
        "rows": rows,
        "external": {
            **old_ext,
            **ext,
            "ws_worker_state": STATE.get("ws_state", old_ext.get("ws_worker_state", "-")),
            "ws_worker_targets": STATE.get("ws_targets", old_ext.get("ws_worker_targets", 0)),
            "ws_worker_fresh": STATE.get("ws_fresh", old_ext.get("ws_worker_fresh", 0)),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", old_ext.get("ws_last_age_sec", -1)),
            "micro_worker_state": STATE.get("micro_state", old_ext.get("micro_worker_state", "-")),
            "micro_worker_targets": STATE.get("micro_targets", old_ext.get("micro_worker_targets", 0)),
            "micro_worker_fresh": STATE.get("micro_fresh", old_ext.get("micro_worker_fresh", 0)),
            "overlay_mode": "v236_post_scan_meta_fixed_refresh",
            "refreshed_ts": nowv,
            "refresh_attempt": attempt,
            "refresh_attempt_total": total_attempts,
            "refresh_waited_sec": round(waited_sec, 2),
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2),
        },
        "target": {
            **target,
            "post_scan_refresh": True,
            "post_scan_refresh_attempt": attempt,
            "post_scan_refresh_total": total_attempts,
            "post_scan_refresh_waited_sec": round(waited_sec, 2),
            "priority_rule": "v236: post-refresh rewrites paper_latest only with scan_id/TTL fixed rows",
        },
        "note": "v236: post-refresh keeps paper_latest fresh and traceable; no scan_id '-' / TTL 0 rows",
    })
    save_json(FILES["candidate_snapshot"], refreshed)
    try:
        write_jsonl_replace(FILES["paper_latest"], rows)
    except Exception as exc:
        log_error("v236_refresh_write_paper_latest", exc)
    with _state_lock:
        STATE["candidate_snapshot_ts"] = nowv
        STATE["candidate_snapshot_count"] = len(rows)
        STATE["candidate_snapshot_source"] = "v236_post_scan_meta_fixed_refresh"
        STATE["candidate_snapshot_stage"] = "external_refreshed_after_scan"
        STATE["candidate_snapshot_external"] = refreshed.get("external", {})
        STATE["v219_last_refresh_scan_id"] = scan_id
        STATE["v219_last_refresh_ts"] = nowv
        STATE["v236_last_refresh_attempt"] = attempt
        STATE["v236_latest_rewrite_rows"] = len(rows)
        STATE["v236_latest_ttl_fixed"] = True
        STATE["v229_micro_fresh_after_refresh"] = ext.get("micro_fresh", 0)
        STATE["v229_micro_wait_after_refresh"] = int(ext.get("micro_stale", 0) or 0) + int(ext.get("micro_missing", 0) or 0)
    build_quality = bool(attempt >= total_attempts or (int(ext.get("micro_stale", 0) or 0) + int(ext.get("micro_missing", 0) or 0) <= 0))
    _v219_write_command_caches_after_snapshot(build_quality=build_quality)
    return True, ext


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    txt = _read_cached_text(FILES["quality_summary"], "/quality")
    return (txt.replace("v235 기본은 캐시 전용", "v236 기본은 캐시 전용")
               .replace("v234 기본은 캐시 전용", "v236 기본은 캐시 전용")
               .replace("v233 기본은 캐시 전용", "v236 기본은 캐시 전용")
               .replace("v231 기본은 캐시 전용", "v236 기본은 캐시 전용")
               .replace("v230 기본은 캐시 전용", "v236 기본은 캐시 전용")
               .replace("v222 기본은 캐시 전용", "v236 기본은 캐시 전용"))


# ===============================
# v2.13.237: 캐시 버전 가드 + paper_latest scan_id 최종 보강
# - 이전 버전 command cache가 /version_score, /quality에 물리는 경로를 차단한다.
# - 어떤 공장/refresh 경로가 latest를 쓰더라도 마지막에 scan_id/TTL을 보강한다.
# - 후보조건/청산조건/자동매수/BUY_READY/v343 변경 없음.
# ===============================

_v237_original_read_cached_text = _read_cached_text

def _read_cached_text(path: Path, title: str) -> str:  # type: ignore[override]
    obj = load_json(path, {})
    if isinstance(obj, dict) and obj.get("text"):
        cached_ver = str(obj.get("version") or "")
        if cached_ver and cached_ver != BOT_VERSION:
            return "\n".join([
                f"❔ {title} 캐시 갱신중",
                f"- 현재 실행버전: {BOT_VERSION}",
                f"- 기존 캐시버전: {cached_ver}",
                "- 이전 버전 요약은 표시하지 않습니다. 요약 직원이 곧 새 캐시를 저장합니다.",
            ])
    return _v237_original_read_cached_text(path, title)


def _v237_rewrite_latest_file_with_meta(path: Path, *, lane: str, scan_id: str = "") -> int:
    """latest 파일에 scan_id/TTL이 없는 row가 섞이면 같은 본선 helper로 정리한다."""
    try:
        rows = tail_jsonl(path, max_lines=2000) if path.exists() else []
        if not rows:
            return 0
        sid = str(scan_id or STATE.get("scan_id") or "")
        fixed = [_v236_fix_candidate_meta(r, scan_id=sid, lane=lane, refresh_ttl=True, source="v237_latest_final_rewrite") for r in rows if isinstance(r, dict)]
        write_jsonl_replace(path, fixed)
        return len(fixed)
    except Exception as exc:
        log_error(f"v237_rewrite_latest_meta:{getattr(path,'name',path)}", exc)
        return 0

_v237_original_export_candidates = export_candidates

def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v237: 기존 공장 본선을 쓰되, 마지막 latest 파일 메타를 한 번 더 고정한다."""
    res = _v237_original_export_candidates(strict, shadow)
    try:
        sid = str(STATE.get("scan_id") or "")
        n1 = _v237_rewrite_latest_file_with_meta(FILES["paper_latest"], lane="strict", scan_id=sid)
        n2 = _v237_rewrite_latest_file_with_meta(FILES["shadow_latest"], lane="shadow", scan_id=sid)
        with _state_lock:
            STATE["v237_latest_scan_id_fixed"] = sid or "-"
            STATE["v237_paper_latest_fixed_rows"] = n1
            STATE["v237_shadow_latest_fixed_rows"] = n2
    except Exception as exc:
        log_error("v237_export_latest_final_rewrite", exc)
    return res


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    txt = _read_cached_text(FILES["quality_summary"], "/quality")
    return (txt.replace("v236 기본은 캐시 전용", "v237 기본은 캐시 전용")
               .replace("v235 기본은 캐시 전용", "v237 기본은 캐시 전용")
               .replace("v234 기본은 캐시 전용", "v237 기본은 캐시 전용")
               .replace("v233 기본은 캐시 전용", "v237 기본은 캐시 전용")
               .replace("v231 기본은 캐시 전용", "v237 기본은 캐시 전용")
               .replace("v230 기본은 캐시 전용", "v237 기본은 캐시 전용")
               .replace("v222 기본은 캐시 전용", "v237 기본은 캐시 전용"))


# ===============================
# v2.13.238: quality/current-version CLOSED reader 단일화 + cache closed-file guard
# - /version_score는 CLOSED 장부를 읽는데 /quality 현재버전 블록이 stale cache/구 reader 때문에 0전으로 보이던 경로를 차단한다.
# - /quality [1-3], [1-4]는 /version_score와 같은 rows_since_current_version(load_closed()) 계열을 사용한다.
# - 종료명 quick_stop/bounce_fail/long_loss_guard/hard_loss_guard/slow_early_exit 한글 집계를 보강한다.
# - 후보조건/청산조건/자동매수/BUY_READY/v343 변경 없음.
# ===============================

EXIT_REASON_KR.update({
    "quick_stop": "빠른손절 종료",
    "bounce_fail": "반등실패 조기손절",
    "hard_loss_guard": "손실하드가드 종료",
    "long_loss_guard": "장기손실 정리",
    "slow_early_exit": "지지부진 조기종료",
})


def _v233_exit_reason_bucket(row: Dict[str, Any]) -> str:
    """v238: CLOSED exit_reason을 분석용 큰 분류로 단일화한다."""
    raw = str((row or {}).get("exit_reason") or (row or {}).get("close_reason") or (row or {}).get("reason") or "unknown").strip()
    low = raw.lower()
    if low in {"take_profit", "protect_stop_after_tp"} or "익절" in raw:
        return low if low in {"take_profit", "protect_stop_after_tp"} else "take_profit"
    if low in {"quick_stop", "bounce_fail", "hard_loss_guard", "long_loss_guard", "stop_loss"}:
        return "stop"
    if low in {"slow_early_exit", "slow_no_progress", "time_exit"}:
        return "slow"
    if "quick" in low or "stop" in low or "손절" in raw or "하드가드" in raw or "장기손실" in raw or "반등실패" in raw:
        return "stop"
    if "slow" in low or "지지" in raw or "부진" in raw or "시간" in raw:
        return "slow"
    return low or "unknown"


def _v238_closed_sig() -> Dict[str, Any]:
    p = FILES.get("paper_closed", BASE_DIR / "paper_bot_closed.jsonl")
    try:
        st = p.stat() if p.exists() else None
        return {"paper_closed_mtime": float(st.st_mtime) if st else 0.0, "paper_closed_size": int(st.st_size) if st else 0}
    except Exception:
        return {"paper_closed_mtime": 0.0, "paper_closed_size": 0}


def _v238_current_version_rows(limit: int = 5000) -> List[Dict[str, Any]]:
    try:
        return [r for r in rows_since_current_version(load_closed(limit=limit)) if str((r or {}).get("lane") or "strict") == "strict"]
    except Exception as exc:
        log_error("v238_current_version_rows", exc)
        try:
            return _v210_current_version_rows(_v210_strict_rows(load_closed(limit)))
        except Exception:
            return []


def _v238_quality_payload(text: str, *, snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = _v231_cache_payload(text, "quality", snap=snap if isinstance(snap, dict) else _v212_read_candidate_snapshot())
    payload.update(_v238_closed_sig())
    payload["quality_reader"] = "v238_same_closed_reader_as_version_score"
    payload["current_version_closed_n"] = len(_v238_current_version_rows())
    return payload


def _v238_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    """v238: 기존 v233 품질판을 유지하되 현재버전 성과/문제/청산모드 블록만 CLOSED 최신 reader로 강제 동기화한다."""
    txt = _v233_quality_text_from_snapshot(snap if isinstance(snap, dict) else _v212_read_candidate_snapshot())
    cur = _v238_current_version_rows()
    # 성과 요약의 현재버전 한 줄 교체
    txt = re.sub(
        r"[❔⚠️✅❌] 현재버전 정식: .*?(?=\n- 현재버전 기준:)",
        _compact_stat_line("현재버전 정식", cur),
        txt,
        count=1,
    )
    # [1-3]과 [1-4] 블록 교체. 후보/외부정보 블록은 기존 snapshot 기준 유지.
    problem = "\n".join(_v233_problem_block(cur))
    exitmode = "\n".join(_v233_exit_mode_block(cur))
    txt = re.sub(r"\n\[1-3\] 후보품질 2대 문제.*?(?=\n\[2/5\] 현재 후보)", "\n" + problem + exitmode + "\n", txt, flags=re.S, count=1)
    # 버전 문구 통일
    for old in ("v237", "v236", "v235", "v234", "v233", "v231", "v230", "v222"):
        txt = txt.replace(f"{old} 기본은 캐시 전용", "v238 기본은 캐시 전용")
    return txt


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        snap = _v212_read_candidate_snapshot()
        save_json(FILES["quality_summary"], _v238_quality_payload(_v238_quality_text_from_snapshot(snap), snap=snap))
    except Exception as exc:
        log_error("v238_quality_cache", exc)


def _v231_build_observation_caches(*, build_quality: bool = True, reason: str = "manual") -> None:  # type: ignore[override]
    """v238: health/external은 기존처럼 snapshot 기준, quality는 CLOSED mtime까지 포함해 저장한다."""
    global _V231_LAST_QUALITY_SCAN_ID
    with _V231_OBS_CACHE_LOCK:
        snap = _v212_read_candidate_snapshot()
        strict_rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
        ext_snap = _v233_external_from_snapshot(snap, strict_rows)
        try:
            save_json(FILES["external_snapshot"], _v231_cache_payload(_v210_external_status_text(ext_snap), "external_status", snap=snap))
            save_json(FILES["health_snapshot"], _v231_cache_payload(_v210_health_text(), "health", snap=snap))
        except Exception as exc:
            log_error("v238_observation_light_cache", exc)
        if build_quality:
            try:
                save_json(FILES["quality_summary"], _v238_quality_payload(_v238_quality_text_from_snapshot(snap), snap=snap))
                _V231_LAST_QUALITY_SCAN_ID = str((snap or {}).get("scan_id") or "")
            except Exception as exc:
                log_error("v238_observation_quality_cache", exc)
        _V231_LAST_OBS_CACHE.update({
            "scan_id": str((snap or {}).get("scan_id") or ""),
            "snapshot_ts": fnum((snap or {}).get("updated_ts"), 0.0) if isinstance(snap, dict) else 0.0,
            "updated_ts": now_ts(),
            "reason": reason,
            "quality": bool(build_quality),
            **_v238_closed_sig(),
        })
        with _state_lock:
            STATE["v238_observation_cache_scan_id"] = _V231_LAST_OBS_CACHE.get("scan_id", "")
            STATE["v238_observation_cache_reason"] = reason
            STATE["v238_observation_quality_built"] = bool(build_quality)


def _v238_quality_cache_valid(obj: Dict[str, Any]) -> bool:
    if not isinstance(obj, dict) or not obj.get("text"):
        return False
    if str(obj.get("version") or "") != BOT_VERSION:
        return False
    sig = _v238_closed_sig()
    # CLOSED 파일이 바뀌면 /quality 현재버전 블록도 즉시 새로 만들어야 한다.
    if fnum(obj.get("paper_closed_mtime"), -1) != fnum(sig.get("paper_closed_mtime"), -2):
        return False
    if int(obj.get("paper_closed_size", -1)) != int(sig.get("paper_closed_size", -2)):
        return False
    return True


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    obj = load_json(FILES["quality_summary"], {})
    if not _v238_quality_cache_valid(obj):
        try:
            _build_quality_cache()
            obj = load_json(FILES["quality_summary"], {})
        except Exception as exc:
            log_error("v238_quality_on_demand_rebuild", exc)
    if isinstance(obj, dict) and obj.get("text"):
        txt = str(obj.get("text") or "")
    else:
        txt = _v238_quality_text_from_snapshot(_v212_read_candidate_snapshot())
    for old in ("v237", "v236", "v235", "v234", "v233", "v231", "v230", "v222"):
        txt = txt.replace(f"{old} 기본은 캐시 전용", "v238 기본은 캐시 전용")
    return txt



# ===============================
# v2.13.240: /quality 캐시전용 고정 + 공장/snapshot 경량화
# - /quality가 캐시 버전 불일치 때 직접 무거운 재계산을 타던 v238 on-demand 경로를 차단한다.
# - 공장 latest 저장 후 파일을 다시 읽어 재작성하던 v237 final rewrite를 현재 본선에서 우회한다.
# - snapshot/post-refresh의 외부맵 강제 refresh를 TTL 기반 1회 재사용으로 바꿔 factory/snapshot 병목을 줄인다.
# - 후보조건/청산조건/자동매수/BUY_READY/v343 변경 없음.
# ===============================


def _v240_cache_wait_text(title: str, obj: Any, why: str = "") -> str:
    cached_ver = "-"
    cached_at = "-"
    if isinstance(obj, dict):
        cached_ver = str(obj.get("version") or "-")
        cached_at = str(obj.get("updated_text") or "-")
    parts = [
        f"❔ {title} 캐시 갱신중",
        f"- 현재 실행버전: {BOT_VERSION}",
        f"- 기존 캐시버전: {cached_ver}",
        f"- 기존 캐시시각: {cached_at}",
        "- /quality는 직접 재계산하지 않고 캐시만 읽습니다.",
        "- 백그라운드 요약 직원이 새 캐시를 저장하면 자동으로 바뀝니다.",
    ]
    if why:
        parts.append(f"- 사유: {why}")
    return "\n".join(parts)


def _v240_quality_cache_invalid_reason(obj: Any) -> str:
    if not isinstance(obj, dict) or not obj.get("text"):
        return "캐시 없음"
    if str(obj.get("version") or "") != BOT_VERSION:
        return f"버전 불일치 {obj.get('version') or '-'}"
    sig = _v238_closed_sig()
    if fnum(obj.get("paper_closed_mtime"), -1) != fnum(sig.get("paper_closed_mtime"), -2):
        return "CLOSED 장부 변경"
    if int(obj.get("paper_closed_size", -1)) != int(sig.get("paper_closed_size", -2)):
        return "CLOSED 크기 변경"
    return ""


_v240_base_quality_text_from_snapshot = _v238_quality_text_from_snapshot

def _v238_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v240_base_quality_text_from_snapshot(snap if isinstance(snap, dict) else _v212_read_candidate_snapshot())
    for old in ("v239", "v238", "v237", "v236", "v235", "v234", "v233", "v231", "v230", "v222"):
        txt = txt.replace(f"{old} 기본은 캐시 전용", f"{BOT_VERSION} 기준: 캐시 전용")
    return txt


_v240_base_quality_payload = _v238_quality_payload

def _v238_quality_payload(text: str, *, snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # type: ignore[override]
    payload = _v240_base_quality_payload(text, snap=snap)
    payload["quality_reader"] = "v240_cache_worker_only_same_closed_reader"
    payload["quality_direct_rebuild_disabled"] = True
    return payload


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    """v240: /quality는 절대 명령어 안에서 직접 재계산하지 않는다."""
    if full:
        return _v209_direct_candidate_quality_text(True)
    obj = load_json(FILES["quality_summary"], {})
    why = _v240_quality_cache_invalid_reason(obj)
    if why:
        # 다음 worker tick에서 바로 만들 수 있도록 시간표만 당긴다. 여기서 직접 생성하지 않는다.
        try:
            _COMMAND_CACHE_STATE["last_quality"] = 0.0
            with _state_lock:
                STATE["v240_quality_cache_wait_reason"] = why
                STATE["v240_quality_direct_rebuild_blocked"] = True
        except Exception:
            pass
        return _v240_cache_wait_text("/quality", obj, why)
    txt = str((obj or {}).get("text") or "")
    for old in ("v239", "v238", "v237", "v236", "v235", "v234", "v233", "v231", "v230", "v222"):
        txt = txt.replace(f"{old} 기본은 캐시 전용", f"{BOT_VERSION} 기준: 캐시 전용")
    return txt


# 공장/snapshot 경량화: 같은 외부맵을 짧은 TTL 안에서 재사용한다.
def _v240_maps(refresh: bool = True) -> Dict[str, Any]:
    try:
        # force=True로 WS/micro 파일을 반복 갱신하던 경로를 줄인다.
        return _v218_refresh_external_maps(force=False, ttl=1.5 if refresh else 3.0)
    except Exception as exc:
        log_error("v240_maps", exc)
        return _V218_EXT_CACHE if isinstance(_V218_EXT_CACHE, dict) else {}


def _overlay_current_external_for_items(items: List[Dict[str, Any]], *, refresh: bool = True) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v240: 공장/표시 overlay는 외부 cache map을 TTL 기반 1회만 재사용한다."""
    maps = _v240_maps(refresh=refresh)
    out: List[Dict[str, Any]] = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        rr = _v233_overlay_row_with_maps(r, maps)
        ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
        ctx = dict(ctx)
        for k in ("ws_row_status", "ws_age_sec", "ws_targeted", "ws_cache_ts", "current_price_ws_gap_pct", "micro_fresh", "micro_row_status", "micro_age_sec", "micro_targeted", "micro_spread_pct", "micro_trade_buy_ratio_30"):
            if k in rr:
                ctx[k] = rr.get(k)
        ctx["external_overlay_at"] = now_ts()
        ctx["external_overlay_note"] = "v240_one_map_ttl_overlay_before_display_or_export"
        rr["entry_context"] = ctx
        out.append(rr)
    try:
        with _state_lock:
            STATE["v240_overlay_rows"] = len(out)
            STATE["v240_overlay_map_age_sec"] = round(now_ts() - fnum(maps.get("ts"), now_ts()), 2) if isinstance(maps, dict) else -1
    except Exception:
        pass
    return out


def _v212_snapshot_payload(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], *, stage: str, source: str, wait_sec: float = 0.0) -> Dict[str, Any]:  # type: ignore[override]
    """v240: snapshot도 강제 refresh 대신 TTL map을 재사용한다."""
    maps = _v240_maps(refresh=True)
    scan_id = str(STATE.get("scan_id") or f"scan-{int(now_ts())}")
    ordered = _v212_order_candidates(strict_rows)
    rows = [_v236_fix_candidate_meta(_v233_overlay_row_with_maps(r, maps), scan_id=scan_id, lane="strict", refresh_ttl=False, source="v240_snapshot") for r in ordered]
    ext = _v233_external_from_snapshot({"updated_ts": now_ts(), "rows": rows, "scan_id": scan_id}, rows)
    nowv = now_ts()
    return {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v240",
        "stage": stage,
        "source": source,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": scan_id,
        "scan_seq": int(fnum(STATE.get("scan_seq"), 0)),
        "candidate_count": len(rows),
        "shadow_count": len(shadow_rows or []),
        "rows": rows,
        "external": {
            **ext,
            "ws_worker_state": STATE.get("ws_state", "-"),
            "ws_worker_targets": STATE.get("ws_targets", 0),
            "ws_worker_fresh": STATE.get("ws_fresh", 0),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", -1),
            "micro_worker_state": STATE.get("micro_state", "-"),
            "micro_worker_targets": STATE.get("micro_targets", 0),
            "micro_worker_fresh": STATE.get("micro_fresh", 0),
            "overlay_mode": "v240_one_map_ttl_overlay_meta_fixed",
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2) if isinstance(maps, dict) else -1,
        },
        "target": {
            "writer": "factory_export_final_only",
            "reason": "factory_final_candidates",
            "ws_target_count": STATE.get("ws_target_file_targets", 0),
            "micro_target_count": STATE.get("micro_target_file_targets", 0),
            "ws_target_reason": STATE.get("ws_target_reason", "-"),
            "micro_target_reason": STATE.get("micro_target_reason", "-"),
            "wait_sec": wait_sec,
            "priority_rule": "v240: scan candidates first; snapshot uses cached external maps",
        },
        "note": "v240: snapshot/factory use TTL external map; /quality is cache-worker only",
    }


# v237의 latest 파일 재읽기/재쓰기 final rewrite는 scan 공장 병목을 만들 수 있어 현재 본선에서 우회한다.
def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v240: base factory 저장 결과를 그대로 쓰고, latest 재읽기 rewrite를 하지 않는다."""
    base = globals().get("_v237_original_export_candidates")
    if callable(base):
        res = base(strict, shadow)
    else:
        # 비상시에는 직전 export를 쓰되, 이 경로는 정상 배포에서는 타면 안 된다.
        res = _v237_original_export_candidates(strict, shadow)  # type: ignore[name-defined]
    try:
        with _state_lock:
            STATE["v240_export_no_final_reread"] = True
            STATE["v240_export_mode"] = "base_factory_no_v237_latest_reread"
    except Exception:
        pass
    return res


def _v229_refresh_candidate_snapshot_once(expected_scan_id: str = "", *, attempt: int = 1, total_attempts: int = 1, waited_sec: float = 0.0) -> Tuple[bool, Dict[str, Any]]:  # type: ignore[override]
    """v240: post-refresh도 TTL map을 재사용하고 /quality를 직접 만들지 않는다."""
    snap = _v212_read_candidate_snapshot()
    if not isinstance(snap, dict) or not snap.get("rows"):
        return False, {}
    scan_id = str(snap.get("scan_id") or expected_scan_id or STATE.get("scan_id") or f"scan-{int(now_ts())}")
    if expected_scan_id and scan_id and scan_id != expected_scan_id:
        return False, {}
    rows0 = [r for r in (snap.get("rows") or []) if isinstance(r, dict)]
    maps = _v240_maps(refresh=True)
    nowv = now_ts()
    rows = [_v236_fix_candidate_meta(_v219_overlay_row_with_maps(r, maps), scan_id=scan_id, lane="strict", refresh_ttl=True, source="v240_post_refresh") for r in rows0]
    ext = _v212_count_external(rows)
    old_ext = snap.get("external") if isinstance(snap.get("external"), dict) else {}
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    refreshed = dict(snap)
    refreshed.update({
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v240",
        "stage": "external_refreshed_after_scan",
        "source": "v240_post_scan_meta_fixed_refresh",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": scan_id,
        "candidate_count": len(rows),
        "rows": rows,
        "external": {
            **old_ext,
            **ext,
            "ws_worker_state": STATE.get("ws_state", old_ext.get("ws_worker_state", "-")),
            "ws_worker_targets": STATE.get("ws_targets", old_ext.get("ws_worker_targets", 0)),
            "ws_worker_fresh": STATE.get("ws_fresh", old_ext.get("ws_worker_fresh", 0)),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", old_ext.get("ws_last_age_sec", -1)),
            "micro_worker_state": STATE.get("micro_state", old_ext.get("micro_worker_state", "-")),
            "micro_worker_targets": STATE.get("micro_targets", old_ext.get("micro_worker_targets", 0)),
            "micro_worker_fresh": STATE.get("micro_fresh", old_ext.get("micro_worker_fresh", 0)),
            "overlay_mode": "v240_post_scan_ttl_map_refresh",
            "refreshed_ts": nowv,
            "refresh_attempt": attempt,
            "refresh_attempt_total": total_attempts,
            "refresh_waited_sec": round(waited_sec, 2),
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2) if isinstance(maps, dict) else -1,
        },
        "target": {
            **target,
            "post_scan_refresh": True,
            "post_scan_refresh_attempt": attempt,
            "post_scan_refresh_total": total_attempts,
            "post_scan_refresh_waited_sec": round(waited_sec, 2),
            "priority_rule": "v240: post-refresh rewrites paper_latest with metadata but does not build quality cache",
        },
        "note": "v240: post-refresh keeps latest traceable and leaves /quality to background cache worker",
    })
    save_json(FILES["candidate_snapshot"], refreshed)
    try:
        write_jsonl_replace(FILES["paper_latest"], rows)
    except Exception as exc:
        log_error("v240_refresh_write_paper_latest", exc)
    with _state_lock:
        STATE["candidate_snapshot_ts"] = nowv
        STATE["candidate_snapshot_count"] = len(rows)
        STATE["candidate_snapshot_source"] = "v240_post_scan_meta_fixed_refresh"
        STATE["candidate_snapshot_stage"] = "external_refreshed_after_scan"
        STATE["candidate_snapshot_external"] = refreshed.get("external", {})
        STATE["v240_last_refresh_attempt"] = attempt
        STATE["v240_latest_rewrite_rows"] = len(rows)
        STATE["v240_quality_build_skipped_in_post_refresh"] = True
    _v219_write_command_caches_after_snapshot(build_quality=False)
    return True, ext


# ===============================
# v2.13.241: 전략검증/복기 전용 관찰판
# - 새 전략을 정식 paper OPEN에 연결하지 않는다.
# - 기존 눌림 재돌파 본선과 paper_bot 장부/청산조건은 그대로 둔다.
# - scan 때 이미 만들어진 표준값으로만 전략별 후보를 작게 기록한다.
# - 3시간 동안 최고/최저/5분/10분/20분/3시간 성과를 추적해 /strategy_watch에서 본다.
# ===============================

FILES.setdefault("strategy_watch_events", BASE_DIR / "strategy_watch_events.jsonl")
FILES.setdefault("strategy_watch_cache", BASE_DIR / "strategy_watch_cache.json")

STRATEGY_WATCH_ON = str(os.getenv("CLEAN_STRATEGY_WATCH_ON", "1")).strip().lower() not in {"0", "false", "no", "off"}
STRATEGY_WATCH_TOP_PER_KIND = int(os.getenv("CLEAN_STRATEGY_WATCH_TOP_PER_KIND", "4"))
STRATEGY_WATCH_MAX_ACTIVE = int(os.getenv("CLEAN_STRATEGY_WATCH_MAX_ACTIVE", "160"))
STRATEGY_WATCH_HOURS = float(os.getenv("CLEAN_STRATEGY_WATCH_HOURS", "3"))
STRATEGY_WATCH_TTL_SEC = max(600.0, STRATEGY_WATCH_HOURS * 3600.0)
STRATEGY_WATCH_REOPEN_GAP_SEC = float(os.getenv("CLEAN_STRATEGY_WATCH_REOPEN_GAP_SEC", "1800"))
STRATEGY_WATCH_EVENT_KEEP_LINES = int(os.getenv("CLEAN_STRATEGY_WATCH_EVENT_KEEP_LINES", "20000"))

STRATEGY_WATCH_LABELS = {
    "pullback_rebreakout": "눌림 재돌파",
    "low_rebound_early": "저점반등 초입",
    "surge_start": "급등초입",
    "money_reaccel": "돈흐름 재가속",
}


def _v241_watch_ticker(row: Dict[str, Any]) -> str:
    return _ticker_from_any((row or {}).get("ticker") or (row or {}).get("market") or (row or {}).get("symbol"))


def _v241_watch_price(row: Dict[str, Any]) -> float:
    return fnum((row or {}).get("current_price") or (row or {}).get("entry_price") or (row or {}).get("detected_price") or (row or {}).get("price"), 0.0)


def _v241_ret(now_price: float, entry_price: float) -> float:
    return round(((now_price / entry_price) - 1.0) * 100.0, 3) if now_price > 0 and entry_price > 0 else 0.0


def _v241_watch_load() -> Dict[str, Any]:
    obj = load_json(FILES["strategy_watch_cache"], {})
    if not isinstance(obj, dict):
        obj = {}
    obj.setdefault("version", BOT_VERSION)
    obj.setdefault("schema", "strategy_watch_v241")
    obj.setdefault("active", {})
    obj.setdefault("recent_done", [])
    obj.setdefault("last_pairs", {})
    return obj


def _v241_watch_save(obj: Dict[str, Any]) -> None:
    obj["version"] = BOT_VERSION
    obj["schema"] = "strategy_watch_v241"
    obj["updated_ts"] = now_ts()
    obj["updated_text"] = now_text()
    obj["ttl_sec"] = STRATEGY_WATCH_TTL_SEC
    save_json(FILES["strategy_watch_cache"], obj)


def _v241_watch_event(kind: str, row: Dict[str, Any], watch_score: float, reasons: List[str], scan_id: str) -> Dict[str, Any]:
    ts = now_ts()
    t = _v241_watch_ticker(row)
    price = _v241_watch_price(row)
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    return {
        "event_id": f"watch:{kind}:{t}:{int(ts)}",
        "version": BOT_VERSION,
        "created_at": ts,
        "created_at_text": now_text(ts),
        "scan_id": scan_id or str(STATE.get("scan_id") or f"scan-{int(ts)}"),
        "watch_kind": kind,
        "watch_label": STRATEGY_WATCH_LABELS.get(kind, kind),
        "ticker": t,
        "entry_price": price,
        "last_price": price,
        "watch_score": round(watch_score, 3),
        "watch_reasons": reasons[:8],
        "source": "v241_strategy_watch_observe_only",
        "review_only": True,
        "paper_bot_open": False,
        "open_eligible": False,
        "auto_trade_ready": False,
        "buy_ready_created": False,
        "strategy_watch_only": True,
        "score": fnum(row.get("score"), 0),
        "base_strategy": row.get("strategy_key") or row.get("route") or STRATEGY_KEY,
        "change_1": fnum(row.get("change_1"), 0),
        "change_3": fnum(row.get("change_3"), 0),
        "change_5": fnum(row.get("change_5"), 0),
        "change_15": fnum(row.get("change_15"), 0),
        "change_30": fnum(row.get("change_30"), 0),
        "money_flow_1m": fnum(row.get("money_flow_1m") or row.get("turnover_1m"), 0),
        "money_flow_3m": fnum(row.get("money_flow_3m") or row.get("turnover_3m"), 0),
        "money_flow_5m": fnum(row.get("money_flow_5m") or row.get("turnover_5m"), 0),
        "turnover_24h": fnum(row.get("turnover_24h"), 0),
        "rank_best": fint(row.get("rank_best"), 9999),
        "from_30m_low_pct": fnum(row.get("from_30m_low_pct"), 0),
        "below_30m_high_pct": fnum(row.get("below_30m_high_pct"), 999),
        "pullback_quality_score": fnum(row.get("pullback_quality_score"), 0),
        "rebreakout_strength": fnum(row.get("rebreakout_strength"), 0),
        "v_rebound_score": fnum(row.get("v_rebound_score"), 0),
        "vol_ratio": fnum(row.get("vol_ratio"), 0),
        "rsi_14": fnum(row.get("rsi_14"), 0),
        "current_close_pos_ratio": fnum(row.get("current_close_pos_ratio"), 0),
        "current_upper_wick_pct": fnum(row.get("current_upper_wick_pct"), 0),
        "micro_fresh": bool(row.get("micro_fresh") or ctx.get("micro_fresh")),
        "micro_row_status": row.get("micro_row_status") or ctx.get("micro_row_status") or "-",
        "micro_trade_buy_ratio_30": fnum(row.get("micro_trade_buy_ratio_30") or ctx.get("micro_trade_buy_ratio_30"), 0),
        "micro_spread_pct": fnum(row.get("micro_spread_pct") or ctx.get("micro_spread_pct"), 999),
        "ws_fresh": bool(row.get("ws_fresh") or ctx.get("ws_fresh")),
        "ws_row_status": row.get("ws_row_status") or ctx.get("ws_row_status") or "-",
        "max_return_pct": 0.0,
        "min_return_pct": 0.0,
        "current_return_pct": 0.0,
        "hit_plus_1_2": False,
        "hit_minus_0_7": False,
        "hit_minus_1_0": False,
        "first_hit": "none",
        "done": False,
    }


# v252 prune: old multi-strategy watch scorers/selector removed.
# Current mainline uses VWAP reversion only; strategy_watch selector is redefined below for vwap_reversion.
def _v241_update_existing_watch(cache: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    active = cache.get("active") if isinstance(cache.get("active"), dict) else {}
    if not active:
        return
    nowv = now_ts()
    price_map = {_v241_watch_ticker(r): _v241_watch_price(r) for r in rows or [] if _v241_watch_ticker(r) and _v241_watch_price(r) > 0}
    done = cache.get("recent_done") if isinstance(cache.get("recent_done"), list) else []
    for eid, ev in list(active.items()):
        if not isinstance(ev, dict):
            active.pop(eid, None); continue
        t = _v241_watch_ticker(ev)
        entry = fnum(ev.get("entry_price"), 0)
        px = price_map.get(t) or fnum(ev.get("last_price"), 0)
        created = fnum(ev.get("created_at"), nowv)
        age = max(0.0, nowv - created)
        if px > 0 and entry > 0:
            cur = _v241_ret(px, entry)
            ev["last_price"] = px
            ev["current_return_pct"] = cur
            ev["max_return_pct"] = max(fnum(ev.get("max_return_pct"), cur), cur)
            ev["min_return_pct"] = min(fnum(ev.get("min_return_pct"), cur), cur)
            if cur >= 1.2 and not ev.get("hit_plus_1_2"):
                ev["hit_plus_1_2"] = True
                ev["hit_plus_1_2_at"] = nowv
                if ev.get("first_hit") == "none":
                    ev["first_hit"] = "+1.2"
            if cur <= -0.7 and not ev.get("hit_minus_0_7"):
                ev["hit_minus_0_7"] = True
                ev["hit_minus_0_7_at"] = nowv
                if ev.get("first_hit") == "none":
                    ev["first_hit"] = "-0.7"
            if cur <= -1.0 and not ev.get("hit_minus_1_0"):
                ev["hit_minus_1_0"] = True
                ev["hit_minus_1_0_at"] = nowv
            for sec, key in ((300, "return_5m_pct"), (600, "return_10m_pct"), (1200, "return_20m_pct"), (10800, "return_3h_pct")):
                if age >= sec and key not in ev:
                    ev[key] = cur
                    ev[key.replace("return", "max_return")] = fnum(ev.get("max_return_pct"), cur)
                    ev[key.replace("return", "min_return")] = fnum(ev.get("min_return_pct"), cur)
        ev["age_sec"] = round(age, 1)
        ev["last_update_ts"] = nowv
        if age >= STRATEGY_WATCH_TTL_SEC:
            ev["done"] = True
            ev["done_at"] = nowv
            ev["done_at_text"] = now_text(nowv)
            done.append(ev)
            active.pop(eid, None)
    # 최근 완료분은 너무 길게 들고 있지 않는다. 장부가 아니라 전략검증 캐시다.
    cutoff = nowv - max(STRATEGY_WATCH_TTL_SEC * 2, 6 * 3600)
    done = [x for x in done if isinstance(x, dict) and fnum(x.get("created_at"), 0) >= cutoff][-500:]
    cache["active"] = active
    cache["recent_done"] = done


def _v241_add_new_watch(cache: Dict[str, Any], events: List[Dict[str, Any]]) -> int:
    active = cache.get("active") if isinstance(cache.get("active"), dict) else {}
    last_pairs = cache.get("last_pairs") if isinstance(cache.get("last_pairs"), dict) else {}
    nowv = now_ts()
    added = 0
    active_pairs = {f"{ev.get('watch_kind')}:{_v241_watch_ticker(ev)}" for ev in active.values() if isinstance(ev, dict)}
    for ev in events or []:
        if len(active) >= STRATEGY_WATCH_MAX_ACTIVE:
            break
        t = _v241_watch_ticker(ev)
        kind = str(ev.get("watch_kind") or "")
        pair = f"{kind}:{t}"
        if not t or not kind:
            continue
        if pair in active_pairs:
            continue
        if nowv - fnum(last_pairs.get(pair), 0) < STRATEGY_WATCH_REOPEN_GAP_SEC:
            continue
        active[str(ev.get("event_id"))] = ev
        active_pairs.add(pair)
        last_pairs[pair] = nowv
        added += 1
        try:
            append_jsonl(FILES["strategy_watch_events"], {**ev, "event_type": "strategy_watch_start"})
        except Exception as exc:
            log_error("v241_strategy_watch_append", exc)
    cache["active"] = active
    cache["last_pairs"] = {k: v for k, v in last_pairs.items() if nowv - fnum(v, 0) <= STRATEGY_WATCH_TTL_SEC * 2}
    return added


def _v241_strategy_watch_update(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> None:
    if not STRATEGY_WATCH_ON:
        return
    try:
        cache = _v241_watch_load()
        _v241_update_existing_watch(cache, rows)
        events = _v241_select_watch_candidates(rows, strict, shadow)
        added = _v241_add_new_watch(cache, events)
        _v241_watch_save(cache)
        try:
            compact_candidate_file(FILES["strategy_watch_events"], keep_lines=STRATEGY_WATCH_EVENT_KEEP_LINES)
        except Exception:
            pass
        with _state_lock:
            STATE["strategy_watch_active"] = len(cache.get("active") or {})
            STATE["strategy_watch_added"] = added
            STATE["strategy_watch_selected"] = len(events)
            STATE["strategy_watch_note"] = "v242 display cleanup; paper OPEN 연결 없음"
    except Exception as exc:
        log_error("v241_strategy_watch_update", exc)


def _v241_watch_rows(window_hours: float = 3.0) -> List[Dict[str, Any]]:
    cache = _v241_watch_load()
    nowv = now_ts()
    cutoff = nowv - max(0.5, window_hours) * 3600.0
    rows: List[Dict[str, Any]] = []
    for ev in (cache.get("active") or {}).values():
        if isinstance(ev, dict) and fnum(ev.get("created_at"), 0) >= cutoff:
            rows.append(ev)
    for ev in cache.get("recent_done") or []:
        if isinstance(ev, dict) and fnum(ev.get("created_at"), 0) >= cutoff:
            rows.append(ev)
    rows.sort(key=lambda r: fnum(r.get("created_at"), 0), reverse=True)
    return rows


def _v241_avg(vals: List[float]) -> float:
    vals = [float(x) for x in vals if x is not None]
    return sum(vals) / len(vals) if vals else 0.0


STRATEGY_WATCH_DECISION_MIN = int(os.getenv("CLEAN_STRATEGY_WATCH_DECISION_MIN", "20"))


def _v242_pct(v: Any) -> str:
    return f"{fnum(v, 0.0):+.2f}%"


def _v242_is_numberish(v: Any) -> bool:
    try:
        if v in (None, "", "-"):
            return False
        float(str(v).replace("%", "").replace(",", ""))
        return True
    except Exception:
        return False


def _v242_fmt_ret(v: Any) -> str:
    return _v242_pct(v) if _v242_is_numberish(v) else "대기"


def _v242_age_text(row: Dict[str, Any]) -> str:
    age = fnum((row or {}).get("age_sec"), max(0.0, now_ts() - fnum((row or {}).get("created_at"), now_ts())))
    if age < 60:
        return f"{age:.0f}초"
    if age < 3600:
        return f"{age/60:.0f}분"
    return f"{age/3600:.1f}시간"


def _v242_external_status(row: Dict[str, Any]) -> str:
    micro_fresh = bool((row or {}).get("micro_fresh")) or str((row or {}).get("micro_row_status") or "").lower() == "fresh"
    ws_fresh = bool((row or {}).get("ws_fresh")) or str((row or {}).get("ws_row_status") or "").lower() == "fresh"
    m_icon = "✅" if micro_fresh else "❔"
    w_icon = "✅" if ws_fresh else "❔"
    buy = fnum((row or {}).get("micro_trade_buy_ratio_30"), 0.0)
    spread = fnum((row or {}).get("micro_spread_pct"), 999.0)
    buy_txt = f"매수 {buy:.2f}" if buy > 0 else "매수 -"
    spread_txt = f"스프레드 {spread:.2f}%" if 0 <= spread < 900 else "스프레드 -"
    return f"micro {m_icon} / WS {w_icon} / {buy_txt} / {spread_txt}"


def _v242_strategy_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows or [])
    if n <= 0:
        return {"n": 0, "hit": 0, "positive_now": 0, "minus_first": 0, "avg_cur": 0.0, "avg_max": 0.0, "avg_min": 0.0, "micro_fresh": 0, "ws_fresh": 0}
    hit = sum(1 for r in rows if bool(r.get("hit_plus_1_2")) or fnum(r.get("max_return_pct"), 0) >= 1.2)
    positive_now = sum(1 for r in rows if fnum(r.get("current_return_pct"), 0) > 0)
    minus_first = sum(1 for r in rows if str(r.get("first_hit") or "") == "-0.7")
    micro_fresh = sum(1 for r in rows if bool(r.get("micro_fresh")) or str(r.get("micro_row_status") or "").lower() == "fresh")
    ws_fresh = sum(1 for r in rows if bool(r.get("ws_fresh")) or str(r.get("ws_row_status") or "").lower() == "fresh")
    return {
        "n": n,
        "hit": hit,
        "positive_now": positive_now,
        "minus_first": minus_first,
        "avg_cur": _v241_avg([fnum(r.get("current_return_pct"), 0) for r in rows]),
        "avg_max": _v241_avg([fnum(r.get("max_return_pct"), 0) for r in rows]),
        "avg_min": _v241_avg([fnum(r.get("min_return_pct"), 0) for r in rows]),
        "micro_fresh": micro_fresh,
        "ws_fresh": ws_fresh,
    }


def _v241_strategy_stat_line(label: str, rows: List[Dict[str, Any]], rank: Optional[int] = None) -> str:
    st = _v242_strategy_stats(rows)
    n = int(st.get("n", 0) or 0)
    prefix = f"{rank}위 " if rank else ""
    if n <= 0:
        return f"- {prefix}{label}: 기록 없음"
    mature = n >= STRATEGY_WATCH_DECISION_MIN
    hit = int(st.get("hit", 0) or 0)
    minus_first = int(st.get("minus_first", 0) or 0)
    if mature and hit / max(n, 1) >= 0.30 and fnum(st.get("avg_max"), 0) >= 0.8:
        icon = "✅"
    elif mature and minus_first / max(n, 1) >= 0.40:
        icon = "❌"
    else:
        icon = "❔"
    sample = "표본충분" if mature else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
    return (
        f"{icon} {prefix}{label}: 후보 {n} / +1.2 {hit}({hit/n*100:.0f}%) / "
        f"현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/n*100:.0f}%) / "
        f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / "
        f"micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / "
        f"-0.7먼저 {minus_first} / {sample}"
    )

def _v242_group_watch_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        t = _v241_watch_ticker(r)
        if not t:
            continue
        g = groups.setdefault(t, {"ticker": t, "rows": [], "labels": [], "kinds": set()})
        g["rows"].append(r)
        kind = str(r.get("watch_kind") or "")
        if kind and kind not in g["kinds"]:
            g["kinds"].add(kind)
            g["labels"].append(str(r.get("watch_label") or STRATEGY_WATCH_LABELS.get(kind, kind)))
    out: List[Dict[str, Any]] = []
    for g in groups.values():
        rs = g.get("rows") or []
        best = max(rs, key=lambda r: (fnum(r.get("max_return_pct"), 0), fnum(r.get("current_return_pct"), 0))) if rs else {}
        first = min(rs, key=lambda r: fnum(r.get("created_at"), now_ts())) if rs else best
        g["best"] = best
        g["first"] = first
        g["max_return_pct"] = max([fnum(r.get("max_return_pct"), 0) for r in rs] or [0])
        g["min_return_pct"] = min([fnum(r.get("min_return_pct"), 0) for r in rs] or [0])
        g["current_return_pct"] = fnum(best.get("current_return_pct"), 0)
        g["age_sec"] = max([fnum(r.get("age_sec"), now_ts() - fnum(r.get("created_at"), now_ts())) for r in rs] or [0])
        out.append(g)
    out.sort(key=lambda g: (fnum(g.get("max_return_pct"), 0), fnum(g.get("current_return_pct"), 0), len(g.get("labels") or [])), reverse=True)
    return out


def _v242_watch_progress_line(rows: List[Dict[str, Any]]) -> str:
    ages = [fnum(r.get("age_sec"), max(0.0, now_ts() - fnum(r.get("created_at"), now_ts()))) for r in rows or []]
    lt5 = sum(1 for a in ages if a < 300)
    ge5 = sum(1 for a in ages if a >= 300)
    ge10 = sum(1 for a in ages if a >= 600)
    ge20 = sum(1 for a in ages if a >= 1200)
    return f"- 평가진행: 5분미만 {lt5} / 5분이상 {ge5} / 10분이상 {ge10} / 20분이상 {ge20}"


def _v241_watch_candidate_lines(rows: List[Dict[str, Any]], limit: int = 8) -> List[str]:
    grouped = _v242_group_watch_rows(rows)
    if not grouped:
        return ["- 기록 없음"]
    out: List[str] = []
    for g in grouped[:limit]:
        best = g.get("best") or {}
        labels = "·".join((g.get("labels") or [])[:4])
        age_txt = _v242_age_text(best)
        eval_txt = "5분평가 대기" if fnum(g.get("age_sec"), 0) < 300 else f"5m {_v242_fmt_ret(best.get('return_5m_pct'))} / 10m {_v242_fmt_ret(best.get('return_10m_pct'))} / 20m {_v242_fmt_ret(best.get('return_20m_pct'))}"
        multi = "여러전략 동시포착" if len(g.get("labels") or []) >= 2 else "단일전략 포착"
        out.append(f"- {g.get('ticker','-')} / 현재 {_v242_pct(g.get('current_return_pct'))} / 최고 {_v242_pct(g.get('max_return_pct'))} / 최저 {_v242_pct(g.get('min_return_pct'))} / {age_txt} / {eval_txt}")
        out.append(f"  · 전략: {labels or '-'} / {multi}")
        out.append(f"  · 외부: {_v242_external_status(best)}")
    return out


def _v242_watch_full_lines(rows: List[Dict[str, Any]], limit: int = 30) -> List[str]:
    if not rows:
        return ["- 기록 없음"]
    ranked = sorted(rows, key=lambda r: (fnum(r.get("max_return_pct"), 0), fnum(r.get("current_return_pct"), 0)), reverse=True)[:limit]
    out: List[str] = []
    for r in ranked:
        out.append(
            f"- {_v241_watch_ticker(r)} / {r.get('watch_label','-')} / {_v242_age_text(r)} / "
            f"현재 {_v242_pct(r.get('current_return_pct'))} / 최고 {_v242_pct(r.get('max_return_pct'))} / 최저 {_v242_pct(r.get('min_return_pct'))} / "
            f"5m {_v242_fmt_ret(r.get('return_5m_pct'))} / 10m {_v242_fmt_ret(r.get('return_10m_pct'))} / 20m {_v242_fmt_ret(r.get('return_20m_pct'))} / {_v242_external_status(r)}"
        )
    return out


def _v242_strategy_ranked(by_kind: Dict[str, List[Dict[str, Any]]]) -> List[Tuple[str, List[Dict[str, Any]], Dict[str, Any]]]:
    ranked: List[Tuple[str, List[Dict[str, Any]], Dict[str, Any]]] = []
    for kind in ("pullback_rebreakout", "low_rebound_early", "surge_start", "money_reaccel"):
        rs = by_kind.get(kind, [])
        ranked.append((kind, rs, _v242_strategy_stats(rs)))
    ranked.sort(key=lambda kv: (fnum(kv[2].get("avg_max"), 0), int(kv[2].get("hit", 0) or 0), int(kv[2].get("n", 0) or 0)), reverse=True)
    return ranked

def strategy_watch_text(full: bool = False) -> str:
    rows = _v241_watch_rows(3.0)
    by_kind: Dict[str, List[Dict[str, Any]]] = {k: [] for k in STRATEGY_WATCH_LABELS}
    for r in rows:
        by_kind.setdefault(str(r.get("watch_kind") or "unknown"), []).append(r)
    cache = _v241_watch_load()
    active_n = len(cache.get("active") or {})
    done_n = len(cache.get("recent_done") or [])
    ranked = _v242_strategy_ranked(by_kind)
    grouped = _v242_group_watch_rows(rows)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결 없음: 관찰·복기 전용입니다.",
        f"- 기준: 최근 3시간 / 관찰이벤트 {len(rows)}개 / 중복묶음 {len(grouped)}개 / active {active_n}개 / 완료보관 {done_n}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/4] 전략별 3시간 임시순위",
    ]
    for idx, (kind, rs, _st) in enumerate(ranked, start=1):
        lines.append(_v241_strategy_stat_line(STRATEGY_WATCH_LABELS.get(kind, kind), rs, idx))
    lines += [
        "",
        "[2/4] 눈에 띄는 후보",
        *_v241_watch_candidate_lines(rows, 8 if not full else 14),
        "",
        "[3/4] 복기 기준",
        "- +1.2% 도달했는데 정식 paper가 안 샀으면: 놓친 후보 후보군",
        "- -0.7%가 먼저 찍히면: 버린 게 맞았던 후보군",
        "- 5m/10m/20m가 계속 양수면: 다음 본선 승격 후보",
        "",
        "[4/4] 판독",
    ]
    if len(rows) < STRATEGY_WATCH_DECISION_MIN:
        best = ranked[0] if ranked else ("", [], {})
        lines.append(f"❔ 표본 부족: {len(rows)}/{STRATEGY_WATCH_DECISION_MIN}. 지금 1위는 임시값입니다.")
        if best[0]:
            lines.append(f"- 임시 1위: {STRATEGY_WATCH_LABELS.get(best[0], best[0])} / 평균최고 {fnum(best[2].get('avg_max'),0):+.2f}%")
    else:
        best = ranked[0] if ranked else ("", [], {})
        lines.append(f"✅ 우선 관찰: {STRATEGY_WATCH_LABELS.get(best[0], best[0])} / 평균최고 {fnum(best[2].get('avg_max'),0):+.2f}%")
        lines.append("- 단, 정식 승격은 5m/10m/20m와 -0.7먼저 비율까지 같이 본 뒤 판단")
    lines.append("- 긴 원자료는 /strategy_watch_full")
    if full:
        lines += ["", "[FULL] 전략별 개별 이벤트", *_v242_watch_full_lines(rows, 40)]
    return "\n".join(lines)

_v241_base_build_candidates = build_candidates

def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    strict, shadow, rejects, examples = _v241_base_build_candidates(rows)
    _v241_strategy_watch_update(rows, strict, shadow)
    return strict, shadow, rejects, examples


_v241_base_score_text = score_text


def _v242_strategy_score_summary_lines() -> List[str]:
    rows = _v241_watch_rows(3.0)
    if not rows:
        return ["- 아직 관찰 표본 없음 / 자세히: /strategy_watch"]
    by_kind: Dict[str, List[Dict[str, Any]]] = {k: [] for k in STRATEGY_WATCH_LABELS}
    for r in rows:
        by_kind.setdefault(str(r.get("watch_kind") or "unknown"), []).append(r)
    ranked = _v242_strategy_ranked(by_kind)
    best = ranked[0] if ranked else ("", [], {})
    hit = sum(1 for r in rows if bool(r.get("hit_plus_1_2")) or fnum(r.get("max_return_pct"), 0) >= 1.2)
    grouped_n = len(_v242_group_watch_rows(rows))
    sample = "결론금지" if len(rows) < STRATEGY_WATCH_DECISION_MIN else "판단가능"
    return [
        f"- 최근 3시간 관찰이벤트 {len(rows)}개 / 후보묶음 {grouped_n}개 / +1.2도달 {hit}개 / {sample}",
        f"- 임시 1위: {STRATEGY_WATCH_LABELS.get(best[0], best[0]) if best[0] else '-'} / 평균최고 {fnum(best[2].get('avg_max'),0):+.2f}% / 자세히: /strategy_watch",
    ]


def score_text() -> str:  # type: ignore[override]
    """v242: /score는 최근 시간대 성과판만 담당한다. 버전별 비교는 /version_score로 분리."""
    try:
        rows = load_closed()
        new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
        new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
        recent_by_hours = {h: [r for r in rows_recent_hours(new_strict, h) if str(r.get("lane")) == "strict"] for h in RECENT_SCORE_WINDOWS}
        recent12 = recent_by_hours.get(12, [])
        v_strict = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
        by_reason_recent = Counter(str(r.get("exit_reason") or "unknown") for r in recent12)
        lines = [
            "📊 모의매매 성과 /score",
            "- 역할: 최근 3/6/12시간 흐름 확인용. 버전 비교는 /version_score",
            "- 전체 기록 삭제 없음",
            "",
            "[1/3] 최근 성과",
        ]
        for h in RECENT_SCORE_WINDOWS:
            lines.append(_compact_stat_line(f"최근 {h}시간 정식", recent_by_hours.get(h, [])))
        lines += [
            "",
            "[2/3] 최근 12시간 종료 사유",
        ]
        if by_reason_recent:
            for k, _ in by_reason_recent.most_common(5):
                sub = [r for r in recent12 if str(r.get("exit_reason") or "unknown") == k]
                lines.append(_compact_stat_line(k, sub))
        else:
            lines.append("- 최근 12시간 CLOSED 부족")
        lines += [
            "",
            "[3/3] 전략검증 3시간",
            *_v242_strategy_score_summary_lines(),
            "",
            "판독",
            "- /score는 지금 장 흐름만 본다.",
            f"- 현재버전 성과: {_compact_stat_line('현재버전 정식', v_strict)}",
            "- 버전별 비교와 최대수익 제외는 /version_score",
            "- 긴 전체표는 /score_full",
        ]
        return "\n".join(lines)
    except Exception as exc:
        log_error("v242_score_text", exc)
        return _v241_base_score_text()

def command_strategy_watch(update, context) -> None:
    reply(update, strategy_watch_text(False))


def command_strategy_watch_full(update, context) -> None:
    reply(update, strategy_watch_text(True))


_v241_base_install_commands = install_commands

def install_commands(updater: Any) -> None:  # type: ignore[override]
    _v241_base_install_commands(updater)
    try:
        dp = updater.dispatcher
        def _wrap(fn):
            def _inner(update, context):
                if handle_multi_command_message(update, context):
                    return
                return fn(update, context)
            return _inner
        dp.add_handler(CommandHandler("strategy_watch", _wrap(command_strategy_watch)))
        dp.add_handler(CommandHandler("strategy_watch_full", _wrap(command_strategy_watch_full)))
        dp.add_handler(CommandHandler("watch", _wrap(command_strategy_watch)))
        with _state_lock:
            cmds = set(STATE.get("compat_commands") or [])
            cmds.update({"strategy_watch", "strategy_watch_full", "watch"})
            STATE["compat_commands"] = sorted(cmds)
    except Exception as exc:
        log_error("v241_install_strategy_watch", exc)


_v241_base_builder_for_command = _builder_for_command

def _builder_for_command(name: str):  # type: ignore[override]
    if name in {"strategy_watch", "watch"}:
        return lambda: strategy_watch_text(False)
    if name == "strategy_watch_full":
        return lambda: strategy_watch_text(True)
    # v257: automatic multi-command uses _builder_for_command(), not only CommandHandler.
    # Keep /reversion_review in the same single source path so line-batch messages no longer return UNKNOWN.
    if name in {"reversion_review", "avg_review"}:
        return lambda: reversion_review_text(False)
    if name in {"reversion_review_full", "avg_review_full"}:
        return lambda: reversion_review_text(True)
    return _v241_base_builder_for_command(name)


# ===============================
# v2.13.243: 전략검증 병목 완화 + /score 캐시화 + 복기 판독 보강
# - 전략검증(update/compact/summary)을 scan마다 전부 돌리지 않고 간격을 둬 병목을 줄인다.
# - /score는 직접 CLOSED 장부를 매번 재계산하지 않고 캐시만 읽는다. stale이면 갱신중으로 안내한다.
# - /strategy_watch는 요약 캐시를 우선 읽고, 표시는 놓친 후보/위험후보가 더 잘 보이게 정리한다.
# - 전략 조건/청산조건/자동매수/BUY_READY/v343/paper 장부는 변경하지 않는다.
# ===============================

FILES.setdefault("strategy_watch_summary", BASE_DIR / "strategy_watch_summary.json")
FILES.setdefault("score_summary", BASE_DIR / "clean_score_summary.json")

STRATEGY_WATCH_UPDATE_GAP_SEC = float(os.getenv("CLEAN_STRATEGY_WATCH_UPDATE_GAP_SEC", "30"))
STRATEGY_WATCH_COMPACT_GAP_SEC = float(os.getenv("CLEAN_STRATEGY_WATCH_COMPACT_GAP_SEC", "900"))
STRATEGY_WATCH_SUMMARY_GAP_SEC = float(os.getenv("CLEAN_STRATEGY_WATCH_SUMMARY_GAP_SEC", "30"))
SCORE_CACHE_MIN_GAP_SEC = float(os.getenv("CLEAN_SCORE_CACHE_MIN_GAP_SEC", "60"))

_V243_SW_RUNTIME: Dict[str, Any] = {"last_update_ts": 0.0, "last_compact_ts": 0.0, "last_summary_ts": 0.0}
_V243_SCORE_LOCK = threading.Lock()
_V243_SCORE_RUNTIME: Dict[str, Any] = {"running": False, "last_try_ts": 0.0, "last_done_ts": 0.0}


def _v243_file_sig(path: Any) -> Dict[str, Any]:
    p = Path(path)
    try:
        st = p.stat() if p.exists() else None
        return {"mtime": float(st.st_mtime) if st else 0.0, "size": int(st.st_size) if st else 0}
    except Exception:
        return {"mtime": 0.0, "size": 0}


def _v243_watch_sig() -> Dict[str, Any]:
    sig = _v243_file_sig(FILES.get("strategy_watch_cache", BASE_DIR / "strategy_watch_cache.json"))
    return {"watch_cache_mtime": sig.get("mtime", 0.0), "watch_cache_size": sig.get("size", 0)}


def _v243_strategy_watch_wait_text(reason: str = "") -> str:
    parts = [
        "🧪 전략검증 /strategy_watch",
        "- 관찰·복기 전용입니다.",
        "❔ 전략검증 요약 캐시 갱신중",
        f"- 현재 실행버전: {BOT_VERSION}",
        "- /strategy_watch는 직접 무거운 재계산 대신 요약 캐시를 우선 읽습니다.",
    ]
    if reason:
        parts.append(f"- 사유: {reason}")
    return "\n".join(parts)


def _v243_score_wait_text(obj: Any, reason: str = "") -> str:
    cached_at = str(obj.get("updated_text") or "-") if isinstance(obj, dict) else "-"
    parts = [
        "📊 모의매매 성과 /score",
        "- 역할: 최근 3/6/12시간 흐름 확인용. 버전 비교는 /version_score",
        "❔ /score 캐시 갱신중",
        f"- 현재 실행버전: {BOT_VERSION}",
        f"- 기존 캐시시각: {cached_at}",
        "- /score는 직접 장부 재계산 대신 캐시만 읽습니다.",
        "- 백그라운드 요약 직원이 새 캐시를 저장하면 자동으로 바뀝니다.",
    ]
    if reason:
        parts.append(f"- 사유: {reason}")
    return "\n".join(parts)


def _v243_repr_strategy_label(group: Dict[str, Any]) -> str:
    rows = list((group or {}).get("rows") or [])
    if not rows:
        return "-"
    best = max(rows, key=lambda r: (fnum(r.get("watch_score"), 0), fnum(r.get("max_return_pct"), 0), fnum(r.get("current_return_pct"), 0)))
    kind = str(best.get("watch_kind") or "")
    return str(best.get("watch_label") or STRATEGY_WATCH_LABELS.get(kind, kind) or "-")


def _v243_watch_judgement(group: Dict[str, Any]) -> str:
    mx = fnum(group.get("max_return_pct"), 0)
    mn = fnum(group.get("min_return_pct"), 0)
    cur = fnum(group.get("current_return_pct"), 0)
    rows = list((group or {}).get("rows") or [])
    hit_plus = any(bool(r.get("hit_plus_1_2")) or fnum(r.get("max_return_pct"), 0) >= 1.2 for r in rows)
    minus_first = any(str(r.get("first_hit") or "") == "-0.7" for r in rows)
    if hit_plus and not minus_first and cur >= 0:
        return "✅ 놓친 좋은 후보"
    if minus_first and mx >= 1.2:
        return "⚠️ 크게 흔들린 후보"
    if minus_first or mn <= -0.7:
        return "❌ 먼저 밀린 후보"
    if mx > 0 and cur > 0:
        return "❔ 관찰중 후보"
    return "❔ 표본 관찰중"


def _v243_watch_candidate_lines(rows: List[Dict[str, Any]], limit: int = 8) -> List[str]:
    grouped = _v242_group_watch_rows(rows)
    if not grouped:
        return ["- 기록 없음"]
    out: List[str] = []
    for g in grouped[:limit]:
        best = g.get("best") or {}
        labels = "·".join((g.get("labels") or [])[:4])
        age_txt = _v242_age_text(best)
        eval_txt = "5분평가 대기" if fnum(g.get("age_sec"), 0) < 300 else f"5m {_v242_fmt_ret(best.get('return_5m_pct'))} / 10m {_v242_fmt_ret(best.get('return_10m_pct'))} / 20m {_v242_fmt_ret(best.get('return_20m_pct'))}"
        rep = _v243_repr_strategy_label(g)
        out.append(f"- {g.get('ticker','-')} / 현재 {_v242_pct(g.get('current_return_pct'))} / 최고 {_v242_pct(g.get('max_return_pct'))} / 최저 {_v242_pct(g.get('min_return_pct'))} / {age_txt} / {eval_txt}")
        out.append(f"  · 대표전략: {rep} / 동시포착: {labels or '-'}")
        out.append(f"  · 판정: {_v243_watch_judgement(g)}")
        out.append(f"  · 외부: {_v242_external_status(best)}")
    return out


def _v243_strategy_ranked(by_kind: Dict[str, List[Dict[str, Any]]]) -> List[Tuple[str, List[Dict[str, Any]], Dict[str, Any]]]:
    ranked: List[Tuple[str, List[Dict[str, Any]], Dict[str, Any], float]] = []
    for kind in ("money_reaccel", "low_rebound_early", "surge_start", "pullback_rebreakout"):
        rs = by_kind.get(kind, [])
        st = _v242_strategy_stats(rs)
        n = max(1, int(st.get("n", 0) or 0))
        hit_rate = int(st.get("hit", 0) or 0) / n
        minus_rate = int(st.get("minus_first", 0) or 0) / n
        score = round(hit_rate * 2.0 + fnum(st.get("avg_max"), 0) * 0.25 - minus_rate * 1.2 + fnum(st.get("avg_cur"), 0) * 0.15, 4)
        ranked.append((kind, rs, st, score))
    ranked.sort(key=lambda kv: (kv[3], fnum(kv[2].get("avg_max"), 0), int(kv[2].get("hit", 0) or 0)), reverse=True)
    return [(k, rs, st) for k, rs, st, _ in ranked]


def _v243_strategy_score_summary_lines_from_rows(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return ["- 아직 관찰 표본 없음 / 자세히: /strategy_watch"]
    by_kind: Dict[str, List[Dict[str, Any]]] = {k: [] for k in STRATEGY_WATCH_LABELS}
    for r in rows:
        by_kind.setdefault(str(r.get("watch_kind") or "unknown"), []).append(r)
    ranked = _v243_strategy_ranked(by_kind)
    best = ranked[0] if ranked else ("", [], {})
    hit = sum(1 for r in rows if bool(r.get("hit_plus_1_2")) or fnum(r.get("max_return_pct"), 0) >= 1.2)
    grouped_n = len(_v242_group_watch_rows(rows))
    sample = "결론금지" if len(rows) < STRATEGY_WATCH_DECISION_MIN else "판단가능"
    return [
        f"- 최근 3시간 관찰이벤트 {len(rows)}개 / 후보묶음 {grouped_n}개 / +1.2도달 {hit}개 / {sample}",
        f"- 임시 1위: {STRATEGY_WATCH_LABELS.get(best[0], best[0]) if best[0] else '-'} / 평균최고 {fnum(best[2].get('avg_max'),0):+.2f}% / 자세히: /strategy_watch",
    ]


def _v243_build_strategy_watch_summary_cache(cache_obj: Optional[Dict[str, Any]] = None) -> None:
    cache = cache_obj if isinstance(cache_obj, dict) else _v241_watch_load()
    nowv = now_ts()
    cutoff = nowv - max(0.5, STRATEGY_WATCH_HOURS) * 3600.0
    rows: List[Dict[str, Any]] = []
    for ev in (cache.get("active") or {}).values():
        if isinstance(ev, dict) and fnum(ev.get("created_at"), 0) >= cutoff:
            rows.append(ev)
    for ev in cache.get("recent_done") or []:
        if isinstance(ev, dict) and fnum(ev.get("created_at"), 0) >= cutoff:
            rows.append(ev)
    rows.sort(key=lambda r: fnum(r.get("created_at"), 0), reverse=True)
    by_kind: Dict[str, List[Dict[str, Any]]] = {k: [] for k in STRATEGY_WATCH_LABELS}
    for r in rows:
        by_kind.setdefault(str(r.get("watch_kind") or "unknown"), []).append(r)
    active_n = len(cache.get("active") or {})
    done_n = len(cache.get("recent_done") or [])
    ranked = _v243_strategy_ranked(by_kind)
    grouped = _v242_group_watch_rows(rows)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결 없음: 관찰·복기 전용입니다.",
        f"- 기준: 최근 3시간 / 관찰이벤트 {len(rows)}개 / 중복묶음 {len(grouped)}개 / active {active_n}개 / 완료보관 {done_n}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/4] 전략별 3시간 임시순위",
    ]
    for idx, (kind, rs, _st) in enumerate(ranked, start=1):
        lines.append(_v241_strategy_stat_line(STRATEGY_WATCH_LABELS.get(kind, kind), rs, idx))
    lines += [
        "",
        "[2/4] 눈에 띄는 후보",
        *_v243_watch_candidate_lines(rows, 8),
        "",
        "[3/4] 복기 기준",
        "- +1.2% 먼저 도달 + -0.7 먼저 아님: 놓친 좋은 후보",
        "- -0.7%가 먼저 찍히면: 버린 게 맞았던 후보 또는 흔들림 큰 후보",
        "- 5m/10m/20m가 계속 양수면: 다음 본선 승격 후보",
        "",
        "[4/4] 판독",
    ]
    if len(rows) < STRATEGY_WATCH_DECISION_MIN:
        best = ranked[0] if ranked else ("", [], {})
        lines.append(f"❔ 표본 부족: {len(rows)}/{STRATEGY_WATCH_DECISION_MIN}. 지금 1위는 임시값입니다.")
        if best[0]:
            lines.append(f"- 임시 1위: {STRATEGY_WATCH_LABELS.get(best[0], best[0])} / 평균최고 {fnum(best[2].get('avg_max'),0):+.2f}%")
    else:
        best = ranked[0] if ranked else ("", [], {})
        lines.append(f"✅ 우선 관찰: {STRATEGY_WATCH_LABELS.get(best[0], best[0])} / 평균최고 {fnum(best[2].get('avg_max'),0):+.2f}%")
        lines.append("- 단, 정식 승격은 +1.2먼저 / -0.7먼저 / 5m·10m·20m를 같이 본 뒤 판단")
    lines.append("- 긴 원자료는 /strategy_watch_full")
    basic_text = "\n".join(lines)
    full_text = basic_text + "\n\n[FULL] 전략별 개별 이벤트\n" + "\n".join(_v242_watch_full_lines(rows, 40))
    sig = _v243_watch_sig()
    payload = {
        "version": BOT_VERSION,
        "name": "strategy_watch",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "text": basic_text,
        "full_text": full_text,
        "summary_lines": _v243_strategy_score_summary_lines_from_rows(rows),
        **sig,
    }
    save_json(FILES["strategy_watch_summary"], payload)
    _V243_SW_RUNTIME["last_summary_ts"] = nowv


def _v243_strategy_watch_summary_valid(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    if str(obj.get("version") or "") != BOT_VERSION:
        return False
    sig = _v243_watch_sig()
    return fnum(obj.get("watch_cache_mtime"), -1) == fnum(sig.get("watch_cache_mtime"), -2) and int(obj.get("watch_cache_size", -1)) == int(sig.get("watch_cache_size", -2))


def _v243_read_strategy_watch_summary(full: bool = False) -> str:
    obj = load_json(FILES["strategy_watch_summary"], {})
    if not _v243_strategy_watch_summary_valid(obj):
        try:
            _v243_build_strategy_watch_summary_cache()
            obj = load_json(FILES["strategy_watch_summary"], {})
        except Exception as exc:
            log_error("v243_strategy_watch_summary_build", exc)
    if isinstance(obj, dict) and _v243_strategy_watch_summary_valid(obj):
        txt = str(obj.get("full_text") if full else obj.get("text") or "")
        if txt:
            return txt
    return _v243_strategy_watch_wait_text("캐시 없음 또는 갱신중")


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    return _v243_read_strategy_watch_summary(full)


def _v241_strategy_watch_update(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> None:  # type: ignore[override]
    if not STRATEGY_WATCH_ON:
        return
    nowv = now_ts()
    try:
        if nowv - fnum(_V243_SW_RUNTIME.get("last_update_ts"), 0.0) < STRATEGY_WATCH_UPDATE_GAP_SEC:
            if nowv - fnum(_V243_SW_RUNTIME.get("last_summary_ts"), 0.0) >= STRATEGY_WATCH_SUMMARY_GAP_SEC:
                try:
                    _v243_build_strategy_watch_summary_cache()
                except Exception:
                    pass
            with _state_lock:
                STATE["strategy_watch_note"] = f"v243 throttle {STRATEGY_WATCH_UPDATE_GAP_SEC:.0f}s; scan 병목 완화"
            return
        cache = _v241_watch_load()
        _v241_update_existing_watch(cache, rows)
        events = _v241_select_watch_candidates(rows, strict, shadow)
        added = _v241_add_new_watch(cache, events)
        _v241_watch_save(cache)
        _V243_SW_RUNTIME["last_update_ts"] = nowv
        if nowv - fnum(_V243_SW_RUNTIME.get("last_compact_ts"), 0.0) >= STRATEGY_WATCH_COMPACT_GAP_SEC:
            try:
                compact_candidate_file(FILES["strategy_watch_events"], keep_lines=STRATEGY_WATCH_EVENT_KEEP_LINES)
            except Exception:
                pass
            _V243_SW_RUNTIME["last_compact_ts"] = nowv
        try:
            _v243_build_strategy_watch_summary_cache(cache)
        except Exception as exc:
            log_error("v243_strategy_watch_summary_build_scan", exc)
        with _state_lock:
            STATE["strategy_watch_active"] = len(cache.get("active") or {})
            STATE["strategy_watch_added"] = added
            STATE["strategy_watch_selected"] = len(events)
            STATE["strategy_watch_note"] = "v243 throttle+cache; paper OPEN 연결 없음"
    except Exception as exc:
        log_error("v243_strategy_watch_update", exc)


def _v243_strategy_score_summary_lines_cached() -> List[str]:
    obj = load_json(FILES["strategy_watch_summary"], {})
    if isinstance(obj, dict) and _v243_strategy_watch_summary_valid(obj):
        lines = obj.get("summary_lines")
        if isinstance(lines, list) and lines:
            return [str(x) for x in lines]
    return ["- 전략검증 요약 캐시 갱신중 / 자세히: /strategy_watch"]


def _v243_score_dep_sig() -> Dict[str, Any]:
    closed_sig = _v238_closed_sig()
    watch_sig = _v243_file_sig(FILES.get("strategy_watch_summary", BASE_DIR / "strategy_watch_summary.json"))
    return {
        **closed_sig,
        "strategy_watch_summary_mtime": watch_sig.get("mtime", 0.0),
        "strategy_watch_summary_size": watch_sig.get("size", 0),
    }


def _v243_score_cache_valid(obj: Any) -> Tuple[bool, str]:
    if not isinstance(obj, dict) or not obj.get("text"):
        return False, "캐시 없음"
    if str(obj.get("version") or "") != BOT_VERSION:
        return False, f"버전 불일치 {obj.get('version') or '-'}"
    sig = _v243_score_dep_sig()
    if fnum(obj.get("paper_closed_mtime"), -1) != fnum(sig.get("paper_closed_mtime"), -2):
        return False, "CLOSED 장부 변경"
    if int(obj.get("paper_closed_size", -1)) != int(sig.get("paper_closed_size", -2)):
        return False, "CLOSED 크기 변경"
    if fnum(obj.get("strategy_watch_summary_mtime"), -1) != fnum(sig.get("strategy_watch_summary_mtime"), -2):
        return False, "전략검증 요약 변경"
    if int(obj.get("strategy_watch_summary_size", -1)) != int(sig.get("strategy_watch_summary_size", -2)):
        return False, "전략검증 요약 크기 변경"
    return True, ""


def _v243_build_score_text_direct() -> str:
    rows = load_closed(limit=16000)
    new_rows = rows_since_paper_bot_baseline(rows, "closed_at")
    new_strict = [r for r in new_rows if str(r.get("lane")) == "strict"]
    recent_by_hours = {h: [r for r in rows_recent_hours(new_strict, h) if str(r.get("lane")) == "strict"] for h in RECENT_SCORE_WINDOWS}
    recent12 = recent_by_hours.get(12, [])
    v_strict = [r for r in rows_since_current_version(rows) if str(r.get("lane")) == "strict"]
    by_reason_recent = Counter(str(r.get("exit_reason") or "unknown") for r in recent12)
    lines = [
        "📊 모의매매 성과 /score",
        "- 역할: 최근 3/6/12시간 흐름 확인용. 버전 비교는 /version_score",
        "- 전체 기록 삭제 없음",
        "",
        "[1/3] 최근 성과",
    ]
    for h in RECENT_SCORE_WINDOWS:
        lines.append(_compact_stat_line(f"최근 {h}시간 정식", recent_by_hours.get(h, [])))
    lines += ["", "[2/3] 최근 12시간 종료 사유"]
    if by_reason_recent:
        for k, _ in by_reason_recent.most_common(5):
            sub = [r for r in recent12 if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_compact_stat_line(k, sub))
    else:
        lines.append("- 최근 12시간 CLOSED 부족")
    lines += [
        "",
        "[3/3] 전략검증 3시간",
        *_v243_strategy_score_summary_lines_cached(),
        "",
        "판독",
        "- /score는 지금 장 흐름만 본다.",
        f"- 현재버전 성과: {_compact_stat_line('현재버전 정식', v_strict)}",
        "- 버전별 비교와 최대수익 제외는 /version_score",
        "- 긴 전체표는 /score_full",
    ]
    return "\n".join(lines)


def _v243_build_score_cache() -> None:
    txt = _v243_build_score_text_direct()
    payload = _cache_payload(txt, "score")
    payload.update(_v243_score_dep_sig())
    save_json(FILES["score_summary"], payload)
    _V243_SCORE_RUNTIME["last_done_ts"] = now_ts()


def _v243_score_refresh_worker() -> None:
    with _V243_SCORE_LOCK:
        if _V243_SCORE_RUNTIME.get("running"):
            return
        _V243_SCORE_RUNTIME["running"] = True
    try:
        _v243_build_score_cache()
    except Exception as exc:
        log_error("v243_build_score_cache", exc)
    finally:
        _V243_SCORE_RUNTIME["running"] = False


def _v243_kick_score_cache_refresh(reason: str = "") -> bool:
    nowv = now_ts()
    if _V243_SCORE_RUNTIME.get("running"):
        return False
    if nowv - fnum(_V243_SCORE_RUNTIME.get("last_try_ts"), 0.0) < SCORE_CACHE_MIN_GAP_SEC:
        return False
    _V243_SCORE_RUNTIME["last_try_ts"] = nowv
    try:
        th = threading.Thread(target=_v243_score_refresh_worker, name="score-cache-refresh", daemon=True)
        th.start()
        return True
    except Exception as exc:
        log_error("v243_kick_score_cache_refresh", exc)
        return False


def score_text() -> str:  # type: ignore[override]
    obj = load_json(FILES["score_summary"], {})
    ok, why = _v243_score_cache_valid(obj)
    if ok and isinstance(obj, dict) and obj.get("text"):
        return str(obj.get("text") or "")
    _v243_kick_score_cache_refresh(why)
    if isinstance(obj, dict) and obj.get("text"):
        return str(obj.get("text") or "") + "\n\n" + _v243_score_wait_text(obj, why)
    return _v243_score_wait_text(obj, why)



# ===============================
# v2.13.244: 정식 모의매매 본선 전환(돈흐름 재가속 단일) + 표시 기준선 리셋
# - 기존 눌림 재돌파 strict 본선은 정식 paper OPEN 경로에서 제외하고 shadow/복기 관찰로 내린다.
# - 정식 paper 후보는 돈흐름 재가속 조건을 통과한 후보만 남긴다.
# - 과거 3/6/12시간 기록은 물리삭제하지 않고 v244 이후 기준선으로 표시를 리셋한다.
# - paper OPEN/CLOSED/trade_log 원장은 삭제하지 않는다.
# ===============================

MONEY_REACCEL_MAIN_KEY = "money_reaccel_main"
MONEY_REACCEL_MAIN_NAME = "돈흐름 재가속 단일검증"
MONEY_REACCEL_STRICT_THRESHOLD = float(os.getenv("CLEAN_MONEY_REACCEL_STRICT_THRESHOLD", "1.85"))
MONEY_REACCEL_STRICT_MAX = int(os.getenv("CLEAN_MONEY_REACCEL_STRICT_MAX", "40"))
MONEY_REACCEL_SHADOW_KEEP_OLD_STRICT = str(os.getenv("CLEAN_MONEY_REACCEL_KEEP_OLD_STRICT_SHADOW", "1")).lower() not in {"0", "false", "no", "off"}


def _v244_is_money_reaccel_item(item: Dict[str, Any]) -> bool:
    return str((item or {}).get("strategy_key") or (item or {}).get("route") or "") == MONEY_REACCEL_MAIN_KEY


def _v244_prepare_money_reaccel_item(item: Dict[str, Any], money_score: float, reasons: List[str]) -> Dict[str, Any]:
    out = dict(item or {})
    out["strategy"] = MONEY_REACCEL_MAIN_NAME
    out["strategy_key"] = MONEY_REACCEL_MAIN_KEY
    out["route"] = MONEY_REACCEL_MAIN_KEY
    out["base_strategy"] = STRATEGY_KEY
    out["money_reaccel_score"] = round(fnum(money_score), 3)
    out["money_reaccel_reasons"] = list(reasons or [])[:8]
    out["one_liner"] = "돈흐름 재가속: " + (" / ".join(list(reasons or [])[:5]) if reasons else "조건 통과")
    out["mainline_note"] = "v244 정식 모의매매는 돈흐름 재가속 단일검증만 사용"
    out["pullback_main_disabled"] = True
    out["review_only"] = False
    return out


def _v244_prepare_old_main_shadow(item: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(item or {})
    out["strategy"] = str(out.get("strategy") or STRATEGY_NAME)
    out["strategy_key"] = str(out.get("strategy_key") or STRATEGY_KEY)
    out["route"] = str(out.get("route") or STRATEGY_KEY)
    out["review_only"] = True
    out["paper_bot_open"] = False
    out["open_eligible"] = False
    out["trade_ready"] = False
    out["observe_only"] = True
    out["mainline_disabled_reason"] = "v244에서 기존 눌림 본선은 정식 모의매매에서 제외"
    notes = list(out.get("aux_notes") or [])
    notes.append("기존 눌림 본선은 v244부터 복기/관찰 전용")
    out["aux_notes"] = notes[-8:]
    return out


_v244_base_build_candidates = build_candidates

def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    base_strict, base_shadow, rejects, examples = _v244_base_build_candidates(rows)
    pool: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(base_strict or []) + list(base_shadow or []):
        t = _v241_watch_ticker(item)
        if not t or t in seen:
            continue
        seen.add(t)
        pool.append(item)

    money_items: List[Tuple[float, Dict[str, Any], List[str]]] = []
    for item in pool:
        s, reasons = _v241_score_money_reaccel(item)
        if s >= MONEY_REACCEL_STRICT_THRESHOLD:
            money_items.append((s, item, reasons))

    money_items.sort(
        key=lambda x: (
            x[0],
            fnum(x[1].get("score"), 0),
            fnum(x[1].get("money_flow_3m") or x[1].get("turnover_3m"), 0),
            fnum(x[1].get("change_3"), 0),
        ),
        reverse=True,
    )
    strict = [_v244_prepare_money_reaccel_item(item, s, reasons) for s, item, reasons in money_items[:max(1, MONEY_REACCEL_STRICT_MAX)]]

    strict_tickers = {_v241_watch_ticker(x) for x in strict}
    shadow: List[Dict[str, Any]] = []
    shadow_seen: set[str] = set()
    if MONEY_REACCEL_SHADOW_KEEP_OLD_STRICT:
        for item in list(base_strict or []) + list(base_shadow or []):
            t = _v241_watch_ticker(item)
            if not t or t in strict_tickers or t in shadow_seen:
                continue
            shadow.append(_v244_prepare_old_main_shadow(item))
            shadow_seen.add(t)
    else:
        for item in base_shadow or []:
            t = _v241_watch_ticker(item)
            if not t or t in strict_tickers or t in shadow_seen:
                continue
            shadow.append(_v244_prepare_old_main_shadow(item))
            shadow_seen.add(t)

    with _state_lock:
        STATE["v244_main_strategy"] = MONEY_REACCEL_MAIN_NAME
        STATE["v244_money_reaccel_strict"] = len(strict)
        STATE["v244_old_pullback_shadow"] = len(shadow)
        STATE["v244_note"] = "정식 모의매매는 돈흐름 재가속 단일검증만 사용"
    return strict, shadow, rejects, examples


_v244_base_decide_trade_ready = decide_trade_ready

def decide_trade_ready(item: Dict[str, Any]) -> Tuple[bool, str, List[str]]:  # type: ignore[override]
    if not _v244_is_money_reaccel_item(item):
        return False, "복기 전용", ["v244: 정식 모의매매는 돈흐름 재가속 단일검증만 허용"]
    ok, label, reasons = _v244_base_decide_trade_ready(item)
    if ok:
        return True, "돈흐름 재가속 정식 모의진입", list(item.get("money_reaccel_reasons") or [])[:5]
    return False, label, reasons


_v244_base_consume_row = consume_row

def consume_row(item: Dict[str, Any], lane: str, ts: Optional[float] = None, scan_id: str = "") -> Dict[str, Any]:  # type: ignore[override]
    row = _v244_base_consume_row(item, lane, ts, scan_id)
    if _v244_is_money_reaccel_item(item):
        row.update({
            "strategy": MONEY_REACCEL_MAIN_NAME,
            "strategy_key": MONEY_REACCEL_MAIN_KEY,
            "route": MONEY_REACCEL_MAIN_KEY,
            "watch_kind": "money_reaccel",
            "watch_label": "돈흐름 재가속",
            "money_reaccel_score": fnum(item.get("money_reaccel_score"), 0),
            "money_reaccel_reasons": list(item.get("money_reaccel_reasons") or [])[:8],
            "mainline_note": "v244 정식 모의매매 단일전략",
        })
    else:
        row.update({
            "review_only": True,
            "paper_bot_open": False,
            "open_eligible": False,
            "trade_ready": False,
            "decision": "shadow_review" if lane != "strict" else "strict_observe",
            "event_type": "single_strategy_shadow" if lane != "strict" else "strict_observe",
            "mainline_disabled_reason": "v244 정식 모의매매에서 제외",
        })
    return row


# v244부터 strategy_watch 캐시는 현재 버전 기록만 기본 표시한다. 구 기록은 파일에 남아도 화면/요약에서 제외.
_v244_base_watch_load = _v241_watch_load

def _v241_watch_load() -> Dict[str, Any]:  # type: ignore[override]
    obj = _v244_base_watch_load()
    try:
        active = obj.get("active") if isinstance(obj.get("active"), dict) else {}
        obj["active"] = {k: v for k, v in active.items() if isinstance(v, dict) and str(v.get("version") or "") == BOT_VERSION}
        done = obj.get("recent_done") if isinstance(obj.get("recent_done"), list) else []
        obj["recent_done"] = [v for v in done if isinstance(v, dict) and str(v.get("version") or "") == BOT_VERSION]
    except Exception as exc:
        log_error("v244_watch_load_filter", exc)
    return obj


def _v244_rows_current_version_only(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in rows_since_current_version(list(rows or [])) if str((r or {}).get("lane") or "strict") == "strict"]


def _v244_build_score_text_direct() -> str:
    rows = load_closed(limit=12000)
    current = _v244_rows_current_version_only(rows)
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in current)
    lines = [
        "📊 모의매매 성과 /score",
        "- 기준: v244 이후 정식 모의매매만 표시합니다. 과거 3/6/12시간 기록은 화면 기준에서 제외했습니다.",
        "- 현재 정식 전략: 돈흐름 재가속 단일검증",
        "- 전체 장부 삭제 없음: 과거 기록은 보존, 현재 판단에서는 제외",
        "",
        "[1/3] v244 이후 성과",
        _compact_stat_line("현재 전략 정식", current),
        f"- 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/3] 종료 사유",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5):
            sub = [r for r in current if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_compact_stat_line(k, sub))
    else:
        lines.append("- v244 이후 CLOSED 없음")
    lines += [
        "",
        "[3/3] 전략검증 참고",
        *_v243_strategy_score_summary_lines_cached(),
        "",
        "판독",
        "- 이제 /score는 돈흐름 재가속 단일검증 결과만 본다.",
        "- 버전별 과거 비교는 /version_score 참고",
        "- 과거 12시간 손실표는 기본 화면에서 제외",
    ]
    return "\n".join(lines)

# /score 캐시 생성 함수가 새 기준을 쓰도록 교체
_v243_build_score_text_direct = _v244_build_score_text_direct  # type: ignore[assignment]


def _v244_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    open_rows = [r for r in rows if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    recheck_rows = [r for r in rows if r.get("final_entry_action") == "recheck_wait" and r not in open_rows]
    observe_rows = [r for r in rows if r not in open_rows and r not in recheck_rows]
    closed = _v244_rows_current_version_only(load_closed(limit=12000))
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: v244 이후 돈흐름 재가속 단일검증만 현재 판단에 사용합니다.",
        "- 과거 3/12시간 기록은 화면 기준에서 제외했습니다. 원장 삭제는 하지 않습니다.",
        "- 긴 원자료성 비교는 /quality_full",
        "",
        "[1/4] v244 이후 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/4] 현재 후보",
        f"- 정식 {len(rows)}개 / 🧪 모의진입 {len(open_rows)}개 / ⚠️ 재확인 {len(recheck_rows)}개 / ❌ 관찰/복기 {len(observe_rows)}개 / snapshot {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())):.1f}초 전",
        f"- 본선: 돈흐름 재가속 단일검증 / 기존 눌림 재돌파는 정식 모의매매에서 제외",
        _simple_ws_line(rows),
        _simple_micro_line(rows),
        "",
        "[3/4] 후보 예시",
        "🧪 모의진입",
        *_candidate_brief_lines(open_rows, 3),
        "⚠️ 재확인",
        *_candidate_brief_lines(recheck_rows, 2),
        "❌ 관찰/복기",
        *_candidate_brief_lines(observe_rows, 2),
        "",
        "[4/4] 판독",
        "✅ 지금부터는 돈흐름 재가속만 정식 모의매매 성과로 판단",
        "⚠️ 표본 50전 전까지 자동매매 판단 금지",
        "✅ 볼 것: +1.2 먼저 도달, -0.7 먼저 비율, 5m/10m/20m 유지력, micro/WS 신선도",
    ]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        snap = _v212_read_candidate_snapshot()
        save_json(FILES["quality_summary"], _v238_quality_payload(_v244_quality_text_from_snapshot(snap), snap=snap))
    except Exception as exc:
        log_error("v244_quality_cache", exc)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    obj = load_json(FILES["quality_summary"], {})
    why = _v240_quality_cache_invalid_reason(obj)
    if why:
        try:
            _build_quality_cache()
            obj = load_json(FILES["quality_summary"], {})
        except Exception as exc:
            log_error("v244_quality_rebuild", exc)
    if isinstance(obj, dict) and obj.get("text") and str(obj.get("version") or "") == BOT_VERSION:
        return str(obj.get("text") or "")
    return _v244_quality_text_from_snapshot(_v212_read_candidate_snapshot())



# ===============================
# v2.13.245: v244 단일검증 표시/캐시 잔상 제거
# - /score가 버전 불일치 과거 캐시 본문을 같이 보여주던 문제를 제거한다.
# - /quality가 백그라운드 관찰캐시 경로에서 과거 3/12시간 혼합표로 다시 덮이는 문제를 차단한다.
# - /strategy_watch 기본 화면은 돈흐름 재가속 단일검증만 보여준다. 다른 전략은 FULL/복기 자료로만 남긴다.
# - paper 원장 물리삭제 없음. 화면/캐시 기준만 현재 버전으로 자른다.
# ===============================

MONEY_REACCEL_DISPLAY_ONLY = True


def _v245_current_closed_rows(limit: int = 6000) -> List[Dict[str, Any]]:
    try:
        return [r for r in rows_since_current_version(load_closed(limit=limit)) if str((r or {}).get("lane") or "strict") == "strict"]
    except Exception as exc:
        log_error("v245_current_closed_rows", exc)
        return []


def _v245_money_watch_rows(window_hours: float = 3.0) -> List[Dict[str, Any]]:
    rows = _v241_watch_rows(window_hours)
    return [r for r in rows if str((r or {}).get("watch_kind") or "") == "money_reaccel"]


def _v245_select_money_watch_candidates(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scan_id = str(STATE.get("scan_id") or f"scan-{int(now_ts())}")
    arr: List[Tuple[float, Dict[str, Any], List[str]]] = []
    seen: set[str] = set()
    # 전체 표준 row를 보되 돈흐름 재가속 후보만 관찰한다.
    for row in list(rows or []) + list(strict or []) + list(shadow or []):
        t = _v241_watch_ticker(row)
        if not t or t in seen:
            continue
        s, reasons = _v241_score_money_reaccel(row)
        if s >= MONEY_REACCEL_STRICT_THRESHOLD:
            arr.append((s, row, reasons))
            seen.add(t)
    arr.sort(key=lambda x: (x[0], fnum(x[1].get("money_flow_3m") or x[1].get("turnover_3m"), 0), fnum(x[1].get("change_3"), 0)), reverse=True)
    return [_v241_watch_event("money_reaccel", row, s, reasons, scan_id) for s, row, reasons in arr[:max(1, STRATEGY_WATCH_TOP_PER_KIND * 3)]]


def _v241_strategy_watch_update(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> None:  # type: ignore[override]
    """v245: strategy_watch 기록도 돈흐름 재가속만 남긴다. 기존 다전략 복기는 FULL/파일에만 남길 수 있다."""
    if not STRATEGY_WATCH_ON:
        return
    nowv = now_ts()
    try:
        if nowv - fnum(_V243_SW_RUNTIME.get("last_update_ts"), 0.0) < STRATEGY_WATCH_UPDATE_GAP_SEC:
            with _state_lock:
                STATE["strategy_watch_note"] = "v245 money_reaccel_only throttle"
            return
        cache = _v241_watch_load()
        _v241_update_existing_watch(cache, rows)
        events = _v245_select_money_watch_candidates(rows, strict, shadow)
        added = _v241_add_new_watch(cache, events)
        _v241_watch_save(cache)
        _V243_SW_RUNTIME["last_update_ts"] = nowv
        if nowv - fnum(_V243_SW_RUNTIME.get("last_compact_ts"), 0.0) >= STRATEGY_WATCH_COMPACT_GAP_SEC:
            try:
                compact_candidate_file(FILES["strategy_watch_events"], keep_lines=STRATEGY_WATCH_EVENT_KEEP_LINES)
            except Exception:
                pass
            _V243_SW_RUNTIME["last_compact_ts"] = nowv
        try:
            _v243_build_strategy_watch_summary_cache(cache)
        except Exception:
            pass
        with _state_lock:
            STATE["strategy_watch_active"] = len(cache.get("active") or {})
            STATE["strategy_watch_added"] = added
            STATE["strategy_watch_selected"] = len(events)
            STATE["strategy_watch_note"] = "v245 돈흐름 재가속만 관찰/복기"
    except Exception as exc:
        log_error("v245_strategy_watch_update", exc)


def _v245_strategy_score_summary_lines() -> List[str]:
    rows = _v245_money_watch_rows(3.0)
    if not rows:
        return ["- 돈흐름 재가속 관찰 표본 없음 / 자세히: /strategy_watch"]
    hit = sum(1 for r in rows if bool(r.get("hit_plus_1_2")) or fnum(r.get("max_return_pct"), 0) >= 1.2)
    minus_first = sum(1 for r in rows if str(r.get("first_hit") or "") == "-0.7")
    grouped_n = len(_v242_group_watch_rows(rows))
    avg_max = _v241_avg([fnum(r.get("max_return_pct"), 0) for r in rows])
    avg_cur = _v241_avg([fnum(r.get("current_return_pct"), 0) for r in rows])
    sample = "결론금지" if len(rows) < STRATEGY_WATCH_DECISION_MIN else "판단가능"
    return [
        f"- 돈흐름 관찰이벤트 {len(rows)}개 / 후보묶음 {grouped_n}개 / +1.2도달 {hit}개 / -0.7먼저 {minus_first}개 / {sample}",
        f"- 평균현재 {avg_cur:+.2f}% / 평균최고 {avg_max:+.2f}% / 자세히: /strategy_watch",
    ]


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v245_money_watch_rows(3.0)
    grouped = _v242_group_watch_rows(rows)
    st = _v242_strategy_stats(rows)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결: 돈흐름 재가속 단일검증만 사용합니다.",
        "- 기존 눌림/급등/저점반등은 기본화면에서 제외했습니다. 필요하면 /strategy_watch_full",
        f"- 기준: 최근 3시간 / 돈흐름 이벤트 {len(rows)}개 / 후보묶음 {len(grouped)}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/4] 돈흐름 재가속 3시간",
    ]
    if rows:
        n = int(st.get("n", 0) or 0)
        hit = int(st.get("hit", 0) or 0)
        minus_first = int(st.get("minus_first", 0) or 0)
        icon = "✅" if n >= 20 and hit >= minus_first else ("⚠️" if n >= 20 else "❔")
        sample = "표본충분" if n >= STRATEGY_WATCH_DECISION_MIN else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
        lines.append(
            f"{icon} 돈흐름 재가속: 후보 {n} / +1.2 {hit}({hit/max(n,1)*100:.0f}%) / "
            f"현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/max(n,1)*100:.0f}%) / "
            f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / "
            f"micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / -0.7먼저 {minus_first} / {sample}"
        )
    else:
        lines.append("- 아직 기록 없음")
    lines += [
        "",
        "[2/4] 눈에 띄는 돈흐름 후보",
        *_v243_watch_candidate_lines(rows, 8 if not full else 20),
        "",
        "[3/4] 복기 기준",
        "- +1.2% 먼저 도달 + -0.7 먼저 아님: 놓친 좋은 돈흐름 후보",
        "- -0.7%가 먼저 찍히면: 버린 게 맞았던 후보 또는 흔들림 큰 후보",
        "- 5m/10m/20m가 계속 양수면: 다음 세부 조건 강화 후보",
        "",
        "[4/4] 판독",
    ]
    if len(rows) < STRATEGY_WATCH_DECISION_MIN:
        lines.append(f"❔ 표본 부족: {len(rows)}/{STRATEGY_WATCH_DECISION_MIN}. 지금은 돈흐름 단일검증을 쌓는 단계입니다.")
    else:
        lines.append("✅ 돈흐름 단일검증 표본 확인중")
        lines.append("- 정식 승격/폐기는 +1.2먼저 / -0.7먼저 / 5m·10m·20m를 같이 본 뒤 판단")
    lines.append("- 긴 원자료는 /strategy_watch_full")
    if full:
        # full은 현재 버전의 모든 전략 이벤트를 참고용으로 보여주되, 기본 판단에는 쓰지 않는다.
        all_rows = _v241_watch_rows(3.0)
        lines += ["", "[FULL] 현재버전 전체 전략검증 참고", *_v242_watch_full_lines(all_rows, 50)]
    return "\n".join(lines)


def _v245_build_score_text_direct() -> str:
    current = _v245_current_closed_rows()
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in current)
    lines = [
        "📊 모의매매 성과 /score",
        "- 기준: 현재버전 돈흐름 재가속 단일검증만 표시합니다.",
        "- 과거 3/6/12시간 기록은 기본 화면에서 제외했습니다.",
        "- 전체 장부 삭제 없음: 과거 기록은 보존, 현재 판단에서는 제외",
        "",
        "[1/3] 현재버전 성과",
        _compact_stat_line("돈흐름 재가속 정식", current),
        f"- 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/3] 종료 사유",
    ]
    if by_reason:
        for k, _ in by_reason.most_common(5):
            sub = [r for r in current if str(r.get("exit_reason") or "unknown") == k]
            lines.append(_compact_stat_line(k, sub))
    else:
        lines.append("- 현재버전 CLOSED 없음")
    lines += [
        "",
        "[3/3] 돈흐름 관찰 참고",
        *_v245_strategy_score_summary_lines(),
        "",
        "판독",
        "- /score는 현재버전 돈흐름 재가속 단일검증 결과만 본다.",
        "- 버전별 과거 비교는 /version_score 참고",
        "- 과거 12시간 손실표는 기본 화면에서 제외",
    ]
    return "\n".join(lines)


def _v245_build_score_cache() -> None:
    txt = _v245_build_score_text_direct()
    payload = _cache_payload(txt, "score")
    payload.update(_v243_score_dep_sig())
    save_json(FILES["score_summary"], payload)


def score_text() -> str:  # type: ignore[override]
    # v245: 버전 불일치 캐시 본문은 절대 보여주지 않는다. 바로 현재 기준으로 다시 만든다.
    try:
        _v245_build_score_cache()
        obj = load_json(FILES["score_summary"], {})
        return str(obj.get("text") or _v245_build_score_text_direct())
    except Exception as exc:
        log_error("v245_score_text", exc)
        return _v245_build_score_text_direct()


def _v245_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    money_rows = [r for r in rows if str(r.get("strategy_key") or r.get("route") or "") == MONEY_REACCEL_MAIN_KEY]
    open_rows = [r for r in money_rows if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    recheck_rows = [r for r in rows if r not in money_rows and r.get("final_entry_action") == "recheck_wait"]
    observe_rows = [r for r in rows if r not in money_rows and r not in recheck_rows]
    closed = _v245_current_closed_rows()
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 현재버전 돈흐름 재가속 단일검증만 현재 판단에 사용합니다.",
        "- 과거 3/12시간 기록은 기본 화면에서 제외했습니다. 원장 삭제는 하지 않습니다.",
        "- 긴 원자료성 비교는 /quality_full",
        "",
        "[1/4] 현재버전 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/4] 현재 후보",
        f"- 전체 후보 {len(rows)}개 / 돈흐름 정식후보 {len(money_rows)}개 / 🧪 모의진입 {len(open_rows)}개 / ⚠️ 재확인 {len(recheck_rows)}개 / ❌ 관찰/복기 {len(observe_rows)}개 / snapshot {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())):.1f}초 전",
        "- 본선: 돈흐름 재가속 단일검증 / 기존 눌림 재돌파는 정식 모의매매에서 제외",
        _simple_ws_line(rows),
        _simple_micro_line(rows),
        "",
        "[3/4] 후보 예시",
        "🧪 돈흐름 모의진입",
        *_candidate_brief_lines(open_rows, 3),
        "⚠️ 재확인",
        *_candidate_brief_lines(recheck_rows, 2),
        "❌ 관찰/복기",
        *_candidate_brief_lines(observe_rows, 2),
        "",
        "[4/4] 판독",
        "✅ 지금부터는 돈흐름 재가속만 정식 모의매매 성과로 판단",
        "⚠️ 표본 50전 전까지 자동매매 판단 금지",
        "✅ 볼 것: +1.2 먼저 도달, -0.7 먼저 비율, 5m/10m/20m 유지력, micro/WS 신선도",
    ]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        snap = _v212_read_candidate_snapshot()
        save_json(FILES["quality_summary"], _v238_quality_payload(_v245_quality_text_from_snapshot(snap), snap=snap))
    except Exception as exc:
        log_error("v245_quality_cache", exc)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    try:
        txt = _v245_quality_text_from_snapshot(_v212_read_candidate_snapshot())
        save_json(FILES["quality_summary"], _v238_quality_payload(txt, snap=_v212_read_candidate_snapshot()))
        return txt
    except Exception as exc:
        log_error("v245_quality_text", exc)
        return _v245_quality_text_from_snapshot(_v212_read_candidate_snapshot())


def _v231_build_observation_caches(*, build_quality: bool = True, reason: str = "manual") -> None:  # type: ignore[override]
    """v245: 백그라운드 관찰캐시가 구 quality 텍스트로 덮어쓰지 못하게 현재 기준으로 저장한다."""
    global _V231_LAST_QUALITY_SCAN_ID
    with _V231_OBS_CACHE_LOCK:
        snap = _v212_read_candidate_snapshot()
        strict_rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
        ext_snap = _v233_external_from_snapshot(snap, strict_rows)
        try:
            save_json(FILES["external_snapshot"], _v231_cache_payload(_v210_external_status_text(ext_snap), "external_status", snap=snap))
            save_json(FILES["health_snapshot"], _v231_cache_payload(_v210_health_text(), "health", snap=snap))
        except Exception as exc:
            log_error("v245_observation_light_cache", exc)
        if build_quality:
            try:
                save_json(FILES["quality_summary"], _v238_quality_payload(_v245_quality_text_from_snapshot(snap), snap=snap))
                _V231_LAST_QUALITY_SCAN_ID = str((snap or {}).get("scan_id") or "")
            except Exception as exc:
                log_error("v245_observation_quality_cache", exc)
        _V231_LAST_OBS_CACHE.update({
            "scan_id": str((snap or {}).get("scan_id") or ""),
            "snapshot_ts": fnum((snap or {}).get("updated_ts"), 0.0) if isinstance(snap, dict) else 0.0,
            "updated_ts": now_ts(),
            "reason": reason,
            "quality": bool(build_quality),
            **_v238_closed_sig(),
        })
        with _state_lock:
            STATE["v245_observation_cache_scan_id"] = _V231_LAST_OBS_CACHE.get("scan_id", "")
            STATE["v245_observation_cache_reason"] = reason
            STATE["v245_observation_quality_built"] = bool(build_quality)



# ===============================
# v2.13.246: 돈흐름 단일검증 후보 도장 강화 + quality/watch 문구 정리
# - paper_bot이 전략/route를 확실히 표시할 수 있게 후보 row에 표시용 필드를 추가한다.
# - /quality의 "정식후보 10개 / 모의진입 0개"처럼 헷갈리는 문구를 paper 전달 후보 중심으로 정리한다.
# - /strategy_watch에는 관찰 이벤트와 paper 전달 후보 수를 분리해서 표시한다.
# - 전략조건/청산조건/자동매수/BUY_READY/v343 변경 없음.
# ===============================

MONEY_REACCEL_MAIN_MODE = "money_reaccel_only"


def _v246_money_reaccel_stamp(out: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(out or {})
    out["strategy"] = MONEY_REACCEL_MAIN_NAME
    out["strategy_key"] = MONEY_REACCEL_MAIN_KEY
    out["strategy_label"] = MONEY_REACCEL_MAIN_NAME
    out["strategy_display"] = MONEY_REACCEL_MAIN_NAME
    out["paper_strategy_label"] = MONEY_REACCEL_MAIN_NAME
    out["paper_strategy_key"] = MONEY_REACCEL_MAIN_KEY
    out["route"] = MONEY_REACCEL_MAIN_KEY
    out["paper_route"] = MONEY_REACCEL_MAIN_KEY
    out["main_mode"] = MONEY_REACCEL_MAIN_MODE
    out["paper_main_mode"] = MONEY_REACCEL_MAIN_MODE
    out["single_strategy_mode"] = True
    out["pullback_main_disabled"] = True
    out["mainline_note"] = "v246 정식 모의매매는 돈흐름 재가속 단일검증만 사용"
    return out


_v246_base_prepare_money_reaccel_item = _v244_prepare_money_reaccel_item

def _v244_prepare_money_reaccel_item(item: Dict[str, Any], money_score: float, reasons: List[str]) -> Dict[str, Any]:  # type: ignore[override]
    out = _v246_base_prepare_money_reaccel_item(item, money_score, reasons)
    out = _v246_money_reaccel_stamp(out)
    out["money_reaccel_score"] = round(fnum(money_score), 3)
    out["money_reaccel_reasons"] = list(reasons or [])[:8]
    out["one_liner"] = "돈흐름 재가속: " + (" / ".join(list(reasons or [])[:5]) if reasons else "조건 통과")
    return out


_v246_base_consume_row = consume_row

def consume_row(item: Dict[str, Any], lane: str, ts: Optional[float] = None, scan_id: str = "") -> Dict[str, Any]:  # type: ignore[override]
    row = _v246_base_consume_row(item, lane, ts, scan_id)
    if _v244_is_money_reaccel_item(item) or _v244_is_money_reaccel_item(row):
        row = _v246_money_reaccel_stamp(row)
        row["watch_kind"] = "money_reaccel"
        row["watch_label"] = "돈흐름 재가속"
        row["money_reaccel_score"] = fnum(item.get("money_reaccel_score", row.get("money_reaccel_score")), 0)
        row["money_reaccel_reasons"] = list(item.get("money_reaccel_reasons") or row.get("money_reaccel_reasons") or [])[:8]
    return row


def _v246_current_snapshot_rows() -> List[Dict[str, Any]]:
    snap = _v212_read_candidate_snapshot()
    return [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]


def _v246_money_snapshot_rows() -> List[Dict[str, Any]]:
    rows = _v246_current_snapshot_rows()
    return [r for r in rows if str(r.get("strategy_key") or r.get("route") or r.get("paper_route") or "") == MONEY_REACCEL_MAIN_KEY]


def _v246_strategy_watch_text_from_rows(full: bool = False) -> str:
    rows = _v245_money_watch_rows(3.0)
    grouped = _v242_group_watch_rows(rows)
    st = _v242_strategy_stats(rows)
    money_snapshot = _v246_money_snapshot_rows()
    paper_ready = [r for r in money_snapshot if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결: 돈흐름 재가속 단일검증만 사용합니다.",
        "- 기존 눌림/급등/저점반등은 기본화면에서 제외했습니다. 필요하면 /strategy_watch_full",
        f"- 기준: 최근 3시간 / 돈흐름 관찰이벤트 {len(rows)}개 / 후보묶음 {len(grouped)}개",
        f"- paper 전달 후보: {len(money_snapshot)}개 / trade_ready 표시: {len(paper_ready)}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/4] 돈흐름 재가속 3시간",
    ]
    if rows:
        n = int(st.get("n", 0) or 0)
        hit = int(st.get("hit", 0) or 0)
        minus_first = int(st.get("minus_first", 0) or 0)
        icon = "✅" if n >= 20 and hit >= minus_first else ("⚠️" if n >= 20 else "❔")
        sample = "표본충분" if n >= STRATEGY_WATCH_DECISION_MIN else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
        lines.append(
            f"{icon} 돈흐름 재가속: 후보 {n} / +1.2 {hit}({hit/max(n,1)*100:.0f}%) / "
            f"현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/max(n,1)*100:.0f}%) / "
            f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / "
            f"micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / -0.7먼저 {minus_first} / {sample}"
        )
    else:
        lines.append("- 아직 기록 없음")
    lines += [
        "",
        "[2/4] 눈에 띄는 돈흐름 후보",
        *_v243_watch_candidate_lines(rows, 8 if not full else 20),
        "",
        "[3/4] 복기 기준",
        "- +1.2% 먼저 도달 + -0.7 먼저 아님: 놓친 좋은 돈흐름 후보",
        "- -0.7%가 먼저 찍히면: 버린 게 맞았던 후보 또는 흔들림 큰 후보",
        "- 5m/10m/20m가 계속 양수면: 다음 세부 조건 강화 후보",
        "",
        "[4/4] 판독",
    ]
    if len(rows) < STRATEGY_WATCH_DECISION_MIN:
        lines.append(f"❔ 표본 부족: {len(rows)}/{STRATEGY_WATCH_DECISION_MIN}. 지금은 돈흐름 단일검증을 쌓는 단계입니다.")
    else:
        lines.append("✅ 돈흐름 단일검증 표본 확인중")
        lines.append("- 정식 승격/폐기는 +1.2먼저 / -0.7먼저 / 5m·10m·20m를 같이 본 뒤 판단")
    lines.append("- 긴 원자료는 /strategy_watch_full")
    if full:
        all_rows = _v241_watch_rows(3.0)
        lines += ["", "[FULL] 현재버전 전체 전략검증 참고", *_v242_watch_full_lines(all_rows, 50)]
    return "\n".join(lines)


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    return _v246_strategy_watch_text_from_rows(full)


def _v246_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    money_rows = [r for r in rows if str(r.get("strategy_key") or r.get("route") or r.get("paper_route") or "") == MONEY_REACCEL_MAIN_KEY]
    paper_ready = [r for r in money_rows if bool(r.get("paper_bot_open") or r.get("open_eligible") or r.get("trade_ready"))]
    recheck_rows = [r for r in rows if r not in money_rows and r.get("final_entry_action") == "recheck_wait"]
    observe_rows = [r for r in rows if r not in money_rows and r not in recheck_rows]
    closed = _v245_current_closed_rows()
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 현재버전 돈흐름 재가속 단일검증만 현재 판단에 사용합니다.",
        "- 과거 3/12시간 기록은 기본 화면에서 제외했습니다. 원장 삭제는 하지 않습니다.",
        "- 긴 원자료성 비교는 /quality_full",
        "",
        "[1/4] 현재버전 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/4] 현재 후보",
        f"- 전체 후보 {len(rows)}개 / 돈흐름 paper 전달 후보 {len(money_rows)}개 / trade_ready 표시 {len(paper_ready)}개 / snapshot {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())):.1f}초 전",
        f"- 참고 분리: 재확인 {len(recheck_rows)}개 / 관찰·복기 {len(observe_rows)}개",
        "- 본선: 돈흐름 재가속 단일검증 / 기존 눌림 재돌파는 정식 모의매매에서 제외",
        _simple_ws_line(rows),
        _simple_micro_line(rows),
        "",
        "[3/4] 후보 예시",
        "🧪 돈흐름 paper 전달 후보",
        *_candidate_brief_lines(money_rows, 5),
        "⚠️ 재확인",
        *_candidate_brief_lines(recheck_rows, 2),
        "❌ 관찰/복기",
        *_candidate_brief_lines(observe_rows, 2),
        "",
        "[4/4] 판독",
        "✅ 지금부터는 돈흐름 재가속만 정식 모의매매 성과로 판단",
        "⚠️ 표본 50전 전까지 자동매매 판단 금지",
        "✅ 볼 것: +1.2 먼저 도달, -0.7 먼저 비율, 5m/10m/20m 유지력, micro/WS 신선도",
    ]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        snap = _v212_read_candidate_snapshot()
        save_json(FILES["quality_summary"], _v238_quality_payload(_v246_quality_text_from_snapshot(snap), snap=snap))
    except Exception as exc:
        log_error("v246_quality_cache", exc)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    try:
        snap = _v212_read_candidate_snapshot()
        txt = _v246_quality_text_from_snapshot(snap)
        save_json(FILES["quality_summary"], _v238_quality_payload(txt, snap=snap))
        return txt
    except Exception as exc:
        log_error("v246_quality_text", exc)
        return _v246_quality_text_from_snapshot(_v212_read_candidate_snapshot())



# ===============================
# v2.13.247: 돈흐름 후보 탈락이유/놓친후보 복기 강화
# - 후보를 임시로 넓혀서 모의매매 수를 늘리지 않는다.
# - 현재 돈흐름 후보가 왜 trade_ready까지 못 갔는지 /quality에 숫자로 표시한다.
# - trade_ready 탈락 후 +1.2% 도달한 후보를 /strategy_watch에서 별도로 보여준다.
# - 자동매매 때도 가져갈 수 있는 조건만 나중에 조정하기 위한 관찰/복기 전용 수술이다.
# - 진입조건/청산조건/자동매수/BUY_READY/v343/paper 장부 삭제 없음.
# ===============================


def _v247_bool_ready(row: Dict[str, Any]) -> bool:
    return bool((row or {}).get("paper_bot_open") or (row or {}).get("open_eligible") or (row or {}).get("trade_ready"))


def _v247_ticker(row: Dict[str, Any]) -> str:
    return str((row or {}).get("ticker") or (row or {}).get("symbol") or "-").upper()


def _v247_text_blob(row: Dict[str, Any]) -> str:
    parts: List[str] = []
    for k in (
        "trade_ready_label", "final_entry_label", "one_liner", "decision", "event_type",
        "hold_reason", "reject_reason", "mainline_disabled_reason", "micro_row_status", "ws_row_status",
    ):
        v = (row or {}).get(k)
        if v not in (None, ""):
            parts.append(str(v))
    for k in ("trade_ready_reasons", "aux_notes", "money_reaccel_reasons", "watch_reasons"):
        v = (row or {}).get(k)
        if isinstance(v, list):
            parts.extend(str(x) for x in v if x not in (None, ""))
        elif v not in (None, ""):
            parts.append(str(v))
    return " / ".join(parts)


def _v247_reject_bucket(row: Dict[str, Any]) -> str:
    if _v247_bool_ready(row):
        return "통과"
    blob = _v247_text_blob(row)
    low = blob.lower()
    spread = fnum(_ctxv(row, "micro_spread_pct", row.get("micro_spread_pct")), 0)
    buy_ratio = fnum(_ctxv(row, "micro_trade_buy_ratio_30", row.get("micro_trade_buy_ratio_30")), 0)
    ws_gap = fnum(_ctxv(row, "current_price_ws_gap_pct", row.get("current_price_ws_gap_pct")), 0)
    upper = fnum(row.get("current_upper_wick_pct"), 0)
    below_high = fnum(row.get("below_30m_high_pct"), 0)
    if "정보 오래" in blob or "stale" in low or "미신선" in blob or "fresh" in low and "not" in low:
        return "정보 신선도 부족"
    if "펌핑" in blob or "과폭발" in blob or "과열" in blob or "고점" in blob or upper >= 1.2 or below_high < 0.35:
        return "과열/추격 위험"
    if "밀림" in blob or "재확인" in blob or "강한음수" in blob or ws_gap <= -0.3:
        return "밀림/재확인 대기"
    if "매도벽" in blob or "매도체결" in blob or "매수세" in blob or (0 < buy_ratio < 0.35):
        return "매수세 약함/매도 우세"
    if "스프레드" in blob or spread >= 0.45:
        return "스프레드/호가 부담"
    if "점수" in blob or "구조" in blob or "부족" in blob:
        return "점수/구조 부족"
    if str((row or {}).get("final_entry_action") or "") == "recheck_wait":
        return "재확인 대기"
    return "기타/표시부족"


def _v247_reject_reason_detail(row: Dict[str, Any]) -> str:
    if _v247_bool_ready(row):
        return "통과"
    reasons = row.get("trade_ready_reasons")
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])[:60]
    label = str(_ctxv(row, "final_entry_label", row.get("one_liner") or row.get("trade_ready_label") or "-"))
    if label and label != "-":
        return label[:60]
    bucket = _v247_reject_bucket(row)
    spread = fnum(_ctxv(row, "micro_spread_pct", row.get("micro_spread_pct")), 0)
    buy_ratio = fnum(_ctxv(row, "micro_trade_buy_ratio_30", row.get("micro_trade_buy_ratio_30")), 0)
    extras = []
    if spread:
        extras.append(f"스프레드 {spread:.2f}%")
    if buy_ratio:
        extras.append(f"매수비 {buy_ratio:.2f}")
    return bucket + ((" / " + " / ".join(extras[:2])) if extras else "")


def _v247_money_reject_summary(rows: List[Dict[str, Any]]) -> Tuple[Counter, Dict[str, List[Dict[str, Any]]]]:
    counter: Counter = Counter()
    examples: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows or []:
        if _v247_bool_ready(r):
            continue
        b = _v247_reject_bucket(r)
        counter[b] += 1
        examples.setdefault(b, [])
        if len(examples[b]) < 3:
            examples[b].append(r)
    return counter, examples


def _v247_reject_summary_lines(rows: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    counter, examples = _v247_money_reject_summary(rows)
    if not counter:
        return ["- trade_ready 탈락 후보 없음"]
    out: List[str] = []
    for bucket, n in counter.most_common(limit):
        exs = examples.get(bucket, [])
        ex_txt = ", ".join(_v247_ticker(x) for x in exs[:3]) if exs else "-"
        out.append(f"- {bucket}: {n}개 / 예시 {ex_txt}")
    return out


def _v247_money_row_map(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    base = rows if rows is not None else _v246_money_snapshot_rows()
    return {_v247_ticker(r): r for r in base or [] if _v247_ticker(r) not in {"", "-"}}


def _v247_watch_original_ready(row: Dict[str, Any]) -> bool:
    return bool((row or {}).get("source_trade_ready") or (row or {}).get("source_paper_bot_open") or (row or {}).get("source_open_eligible"))


_v247_base_watch_event = _v241_watch_event

def _v241_watch_event(kind: str, row: Dict[str, Any], watch_score: float, reasons: List[str], scan_id: str) -> Dict[str, Any]:  # type: ignore[override]
    ev = _v247_base_watch_event(kind, row, watch_score, reasons, scan_id)
    try:
        ev["source_trade_ready"] = bool((row or {}).get("trade_ready"))
        ev["source_paper_bot_open"] = bool((row or {}).get("paper_bot_open"))
        ev["source_open_eligible"] = bool((row or {}).get("open_eligible"))
        ev["source_final_entry_action"] = str((row or {}).get("final_entry_action") or "")
        ev["source_trade_ready_label"] = str((row or {}).get("trade_ready_label") or "")
        ev["source_trade_ready_reasons"] = list((row or {}).get("trade_ready_reasons") or [])[:8] if isinstance((row or {}).get("trade_ready_reasons"), list) else []
        ev["source_reject_bucket"] = _v247_reject_bucket(row)
        ev["source_reject_detail"] = _v247_reject_reason_detail(row)
        ev["source_strategy_key"] = str((row or {}).get("strategy_key") or (row or {}).get("route") or "")
    except Exception as exc:
        log_error("v247_watch_event_stamp", exc)
    return ev


def _v247_missed_money_watch_rows(rows: Optional[List[Dict[str, Any]]] = None, money_rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    watch_rows = rows if rows is not None else _v245_money_watch_rows(3.0)
    by_ticker = _v247_money_row_map(money_rows)
    out: List[Dict[str, Any]] = []
    for r in watch_rows or []:
        if str((r or {}).get("watch_kind") or "") != "money_reaccel":
            continue
        hit = bool((r or {}).get("hit_plus_1_2")) or fnum((r or {}).get("max_return_pct"), 0) >= 1.2
        minus_first = str((r or {}).get("first_hit") or "") == "-0.7"
        if not hit or minus_first:
            continue
        t = _v247_ticker(r)
        snap_row = by_ticker.get(t)
        source_ready = _v247_watch_original_ready(r)
        if snap_row is not None:
            source_ready = _v247_bool_ready(snap_row)
        if source_ready:
            continue
        rr = dict(r)
        if snap_row is not None:
            rr["source_reject_bucket"] = _v247_reject_bucket(snap_row)
            rr["source_reject_detail"] = _v247_reject_reason_detail(snap_row)
        else:
            rr.setdefault("source_reject_bucket", str(r.get("source_reject_bucket") or "현재 snapshot 밖"))
            rr.setdefault("source_reject_detail", str(r.get("source_reject_detail") or "과거 관찰 후보"))
        out.append(rr)
    out.sort(key=lambda x: (fnum(x.get("max_return_pct"), 0), fnum(x.get("current_return_pct"), 0)), reverse=True)
    return out


def _v247_missed_lines(rows: Optional[List[Dict[str, Any]]] = None, money_rows: Optional[List[Dict[str, Any]]] = None, limit: int = 5) -> List[str]:
    missed = _v247_missed_money_watch_rows(rows, money_rows)
    if not missed:
        return ["- 아직 없음"]
    out: List[str] = []
    for r in missed[:limit]:
        out.append(
            f"- {_v247_ticker(r)} / 최고 {_v242_pct(r.get('max_return_pct'))} / 현재 {_v242_pct(r.get('current_return_pct'))} / "
            f"5m {_v242_fmt_ret(r.get('return_5m_pct'))} / 10m {_v242_fmt_ret(r.get('return_10m_pct'))} / 탈락 {r.get('source_reject_bucket','-')}"
        )
        detail = str(r.get("source_reject_detail") or "-")
        out.append(f"  · 왜 빠졌나: {detail}")
    return out


def _v247_money_gate_summary_lines(money_rows: List[Dict[str, Any]]) -> List[str]:
    total = len(money_rows or [])
    ready = sum(1 for r in money_rows or [] if _v247_bool_ready(r))
    lines = [f"- 돈흐름 후보 {total}개 → trade_ready {ready}개 / 탈락 {max(0, total-ready)}개"]
    lines.extend(_v247_reject_summary_lines(money_rows, 6))
    return lines


def _v247_strategy_watch_text_from_rows(full: bool = False) -> str:
    rows = _v245_money_watch_rows(3.0)
    grouped = _v242_group_watch_rows(rows)
    st = _v242_strategy_stats(rows)
    money_snapshot = _v246_money_snapshot_rows()
    paper_ready = [r for r in money_snapshot if _v247_bool_ready(r)]
    missed = _v247_missed_money_watch_rows(rows, money_snapshot)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결: 돈흐름 재가속 단일검증만 사용합니다.",
        "- 이번 기준: 후보를 임시로 넓히지 않고, trade_ready 탈락 이유와 놓친 후보를 복기합니다.",
        f"- 기준: 최근 3시간 / 돈흐름 관찰이벤트 {len(rows)}개 / 후보묶음 {len(grouped)}개",
        f"- paper 전달 후보: {len(money_snapshot)}개 / trade_ready 표시: {len(paper_ready)}개 / 놓친 후보 {len(missed)}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/5] 돈흐름 재가속 3시간",
    ]
    if rows:
        n = int(st.get("n", 0) or 0)
        hit = int(st.get("hit", 0) or 0)
        minus_first = int(st.get("minus_first", 0) or 0)
        icon = "✅" if n >= 20 and hit >= minus_first else ("⚠️" if n >= 20 else "❔")
        sample = "표본충분" if n >= STRATEGY_WATCH_DECISION_MIN else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
        lines.append(
            f"{icon} 돈흐름 재가속: 후보 {n} / +1.2 {hit}({hit/max(n,1)*100:.0f}%) / "
            f"현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/max(n,1)*100:.0f}%) / "
            f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / "
            f"micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / -0.7먼저 {minus_first} / {sample}"
        )
    else:
        lines.append("- 아직 기록 없음")
    lines += [
        "",
        "[2/5] trade_ready 못 간 이유",
        *_v247_money_gate_summary_lines(money_snapshot),
        "",
        "[3/5] 탈락했는데 오른 돈흐름 후보",
        *_v247_missed_lines(rows, money_snapshot, 5),
        "",
        "[4/5] 눈에 띄는 돈흐름 후보",
        *_v243_watch_candidate_lines(rows, 6 if not full else 20),
        "",
        "[5/5] 판독",
        "- 후보수를 임시로 넓히는 게 아니라, 탈락 사유 중 실제로 좋은 후보를 죽인 조건만 찾습니다.",
        "- +1.2 먼저 도달했고 -0.7 먼저가 아니었던 탈락 후보가 핵심 복기 대상입니다.",
        "- 긴 원자료는 /strategy_watch_full",
    ]
    if full:
        all_rows = _v241_watch_rows(3.0)
        lines += ["", "[FULL] 현재버전 전체 전략검증 참고", *_v242_watch_full_lines(all_rows, 50)]
    return "\n".join(lines)


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    return _v247_strategy_watch_text_from_rows(full)


def _v247_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    money_rows = [r for r in rows if str(r.get("strategy_key") or r.get("route") or r.get("paper_route") or "") == MONEY_REACCEL_MAIN_KEY]
    paper_ready = [r for r in money_rows if _v247_bool_ready(r)]
    closed = _v245_current_closed_rows()
    watch_rows = _v245_money_watch_rows(3.0)
    missed = _v247_missed_money_watch_rows(watch_rows, money_rows)
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 현재버전 돈흐름 재가속 단일검증만 현재 판단에 사용합니다.",
        "- 후보를 임시로 넓히지 않고, trade_ready 탈락 이유와 놓친 후보를 먼저 봅니다.",
        "- 과거 3/12시간 기록은 기본 화면에서 제외했습니다. 원장 삭제는 하지 않습니다.",
        "",
        "[1/5] 현재버전 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        f"- 현재버전 기준: {BOT_VERSION} / {version_baseline_text()}",
        "",
        "[2/5] 현재 돈흐름 후보 흐름",
        f"- 전체 후보 {len(rows)}개 / 돈흐름 후보 {len(money_rows)}개 / trade_ready {len(paper_ready)}개 / 탈락 {max(0, len(money_rows)-len(paper_ready))}개 / snapshot {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())):.1f}초 전",
        _simple_ws_line(money_rows),
        _simple_micro_line(money_rows),
        "",
        "[3/5] trade_ready 못 간 이유",
        *_v247_money_gate_summary_lines(money_rows),
        "",
        "[4/5] 탈락했는데 오른 후보",
        *_v247_missed_lines(watch_rows, money_rows, 5),
        "",
        "[5/5] 후보 예시 / 판독",
        "🧪 trade_ready 통과",
        *_candidate_brief_lines(paper_ready, 4),
        "❔ 탈락 예시",
        *_candidate_brief_lines([r for r in money_rows if not _v247_bool_ready(r)], 4),
        "",
        "판독",
        "- 후보수를 억지로 늘리지 말고, 좋은 후보를 죽인 탈락 조건만 찾습니다.",
        "- 반복해서 오른 탈락 사유가 나오면 그 조건만 자동매매에도 가져갈 수 있게 미세조정합니다.",
        "- 표본 50전 전까지 자동매매 판단 금지",
    ]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        snap = _v212_read_candidate_snapshot()
        save_json(FILES["quality_summary"], _v238_quality_payload(_v247_quality_text_from_snapshot(snap), snap=snap))
    except Exception as exc:
        log_error("v247_quality_cache", exc)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    try:
        snap = _v212_read_candidate_snapshot()
        txt = _v247_quality_text_from_snapshot(snap)
        save_json(FILES["quality_summary"], _v238_quality_payload(txt, snap=snap))
        return txt
    except Exception as exc:
        log_error("v247_quality_text", exc)
        return _v247_quality_text_from_snapshot(_v212_read_candidate_snapshot())



# ===============================
# v2.13.248: 돈흐름 기준 통일 + 탈락원인 세분화
# - /score와 /quality를 현재버전 기준이 아니라 paper_bot /pversion_score와 같은 "돈흐름 단일검증 전략 누적" 기준으로 맞춘다.
# - trade_ready 탈락 사유 중 정보 신선도 부족을 WS/micro/가격/urgent 관점으로 쪼개 표시한다.
# - "최종검증 통과"와 "paper trade_ready 탈락"을 분리해서 보여준다.
# - 탈락했는데 오른 후보를 탈락사유별 누적으로 보여준다.
# - 후보 임시확대/진입조건 완화/청산조건 변경/장부 삭제 없음.
# ===============================


def _v248_text_has_money_reaccel(v: Any) -> bool:
    s = str(v or "")
    return ("돈흐름" in s) or ("money_reaccel" in s)


def _v248_is_money_closed(row: Dict[str, Any]) -> bool:
    if str((row or {}).get("lane") or "strict") != "strict":
        return False
    fields = [
        row.get("strategy"), row.get("route"), row.get("paper_route"), row.get("strategy_key"),
        row.get("paper_strategy_label"), row.get("strategy_label"), row.get("final_entry_label"),
    ]
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    raw = row.get("raw") if isinstance(row.get("raw"), dict) else {}
    fields += [ctx.get("strategy"), ctx.get("route"), ctx.get("paper_route"), ctx.get("strategy_key"), ctx.get("paper_strategy_label")]
    fields += [raw.get("strategy"), raw.get("route"), raw.get("paper_route"), raw.get("strategy_key"), raw.get("paper_strategy_label")]
    return any(_v248_text_has_money_reaccel(x) for x in fields)


def _v248_money_strategy_closed_rows(limit: int = 20000) -> List[Dict[str, Any]]:
    try:
        rows = load_closed(limit=limit)
        # paper_bot의 "현재전략"과 맞추기 위해 버전 기준선이 아니라 전략/route 기준으로 본다.
        out = [r for r in rows if _v248_is_money_closed(r)]
        out.sort(key=lambda r: row_ts(r, "closed_at", "opened_at"), reverse=True)
        return out
    except Exception as exc:
        log_error("v248_money_strategy_closed_rows", exc)
        return []


def _v248_exit_reason_lines(rows: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    by_reason = Counter(str((r or {}).get("exit_reason") or "unknown") for r in rows or [])
    if not by_reason:
        return ["- 돈흐름 CLOSED 없음"]
    out: List[str] = []
    for k, _n in by_reason.most_common(limit):
        sub = [r for r in rows if str((r or {}).get("exit_reason") or "unknown") == k]
        out.append(_compact_stat_line(str(k), sub))
    return out


def _v248_bool_field(row: Dict[str, Any], *keys: str) -> Optional[bool]:
    for k in keys:
        v = _ctxv(row, k, (row or {}).get(k))
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and v in (0, 1):
            return bool(v)
        s = str(v or "").strip().lower()
        if s in ("true", "fresh", "ok", "yes", "1", "신선"):
            return True
        if s in ("false", "stale", "missing", "none", "no", "0", "오래됨", "없음"):
            return False
    return None


def _v248_fnum_field(row: Dict[str, Any], *keys: str, default: float = 0.0) -> float:
    for k in keys:
        v = _ctxv(row, k, (row or {}).get(k))
        if v not in (None, ""):
            return fnum(v, default)
    return default


def _v248_fresh_detail(row: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    ws_fresh = _v248_bool_field(row, "ws_fresh", "ws_is_fresh", "ws_target_fresh")
    micro_fresh = _v248_bool_field(row, "micro_fresh", "micro_is_fresh", "micro_target_fresh")
    ws_age = _v248_fnum_field(row, "ws_age_sec", "ws_age", "quote_age_sec", default=-1)
    micro_age = _v248_fnum_field(row, "micro_age_sec", "micro_age", "orderbook_age_sec", "trade_age_sec", default=-1)
    price_age = _v248_fnum_field(row, "price_age_sec", "last_price_age_sec", "current_price_age_sec", default=-1)
    if ws_fresh is False:
        out.append("WS 오래됨/없음")
    elif ws_age >= 45:
        out.append(f"WS {ws_age:.0f}s")
    if micro_fresh is False:
        out.append("micro 오래됨/없음")
    elif micro_age >= 45:
        out.append(f"micro {micro_age:.0f}s")
    if price_age >= 45:
        out.append(f"가격 {price_age:.0f}s")
    blob = _v247_text_blob(row)
    if "urgent" in blob.lower() or "긴급" in blob:
        if "미신선" in blob or "오래" in blob or "stale" in blob.lower():
            out.append("urgent 후 미갱신")
    if not out:
        # 정보 신선도 bucket인데 구체값이 없으면 표시부족으로 남겨둔다.
        if _v247_reject_bucket(row) == "정보 신선도 부족":
            out.append("신선도 표시부족")
    return out


def _v248_fresh_breakdown_lines(rows: List[Dict[str, Any]]) -> List[str]:
    stale_rows = [r for r in rows or [] if (not _v247_bool_ready(r)) and _v247_reject_bucket(r) == "정보 신선도 부족"]
    if not stale_rows:
        return ["- 정보 신선도 부족 후보 없음"]
    c: Counter = Counter()
    examples: Dict[str, List[str]] = {}
    for r in stale_rows:
        details = _v248_fresh_detail(r) or ["신선도 표시부족"]
        for d in details:
            c[d] += 1
            examples.setdefault(d, [])
            if len(examples[d]) < 3:
                examples[d].append(_v247_ticker(r))
    out = [f"- 정보 신선도 부족 {len(stale_rows)}개 세분화"]
    for k, n in c.most_common(6):
        out.append(f"  · {k}: {n}개 / 예시 {', '.join(examples.get(k, [])[:3]) or '-'}")
    return out


def _v248_trade_stage_line(row: Dict[str, Any]) -> str:
    t = _v247_ticker(row)
    final_label = str(_ctxv(row, "final_entry_label", row.get("final_entry_label") or row.get("one_liner") or "-"))
    action = str((row or {}).get("final_entry_action") or "")
    ready = _v247_bool_ready(row)
    bucket = "통과" if ready else _v247_reject_bucket(row)
    detail = "통과" if ready else _v247_reject_reason_detail(row)
    stage = []
    stage.append("전략후보 통과")
    if final_label and final_label != "-":
        stage.append(f"최종검증 {final_label}")
    elif action:
        stage.append(f"최종검증 {action}")
    stage.append("paper trade_ready 통과" if ready else f"paper trade_ready 탈락({bucket})")
    return f"- {t}: " + " / ".join(stage[:3]) + f" / 사유 {detail}"


def _v248_stage_example_lines(rows: List[Dict[str, Any]], limit: int = 5) -> List[str]:
    if not rows:
        return ["- 예시 없음"]
    # 점수 높고 탈락한 후보를 우선 보여준다.
    arr = sorted(rows, key=lambda r: (fnum(r.get("score"), 0), fnum(r.get("money_reaccel_score"), 0), fnum(r.get("money_3m"), 0)), reverse=True)
    return [_v248_trade_stage_line(r) for r in arr[:limit]]


def _v248_missed_bucket_lines(rows: Optional[List[Dict[str, Any]]] = None, money_rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    missed = _v247_missed_money_watch_rows(rows, money_rows)
    if not missed:
        return ["- 놓친 후보 누적 없음"]
    c: Counter = Counter(str(r.get("source_reject_bucket") or "기타") for r in missed)
    out = [f"- 놓친 후보 {len(missed)}개 / 탈락사유별"]
    for k, n in c.most_common(5):
        ex = [str(r.get("ticker") or r.get("symbol") or "-").upper() for r in missed if str(r.get("source_reject_bucket") or "기타") == k][:3]
        out.append(f"  · {k}: {n}개 / 예시 {', '.join(ex)}")
    return out


def _v248_strategy_watch_text_from_rows(full: bool = False) -> str:
    rows = _v245_money_watch_rows(3.0)
    grouped = _v242_group_watch_rows(rows)
    st = _v242_strategy_stats(rows)
    money_snapshot = _v246_money_snapshot_rows()
    paper_ready = [r for r in money_snapshot if _v247_bool_ready(r)]
    missed = _v247_missed_money_watch_rows(rows, money_snapshot)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결: 돈흐름 재가속 단일검증만 사용합니다.",
        "- 이번 기준: 후보를 임시로 넓히지 않고, trade_ready 탈락 이유와 놓친 후보를 복기합니다.",
        f"- 기준: 최근 3시간 / 돈흐름 관찰이벤트 {len(rows)}개 / 후보묶음 {len(grouped)}개",
        f"- paper 전달 후보: {len(money_snapshot)}개 / trade_ready 표시: {len(paper_ready)}개 / 놓친 후보 {len(missed)}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/6] 돈흐름 재가속 3시간",
    ]
    if rows:
        n = int(st.get("n", 0) or 0)
        hit = int(st.get("hit", 0) or 0)
        minus_first = int(st.get("minus_first", 0) or 0)
        icon = "✅" if n >= 20 and hit >= minus_first else ("⚠️" if n >= 20 else "❔")
        sample = "표본충분" if n >= STRATEGY_WATCH_DECISION_MIN else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
        lines.append(
            f"{icon} 돈흐름 재가속: 후보 {n} / +1.2 {hit}({hit/max(n,1)*100:.0f}%) / "
            f"현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/max(n,1)*100:.0f}%) / "
            f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / "
            f"micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / -0.7먼저 {minus_first} / {sample}"
        )
    else:
        lines.append("- 아직 기록 없음")
    lines += [
        "",
        "[2/6] trade_ready 못 간 이유",
        *_v247_money_gate_summary_lines(money_snapshot),
        "",
        "[3/6] 신선도 부족 세분화",
        *_v248_fresh_breakdown_lines(money_snapshot),
        "",
        "[4/6] 탈락했는데 오른 돈흐름 후보",
        *_v247_missed_lines(rows, money_snapshot, 5),
        *_v248_missed_bucket_lines(rows, money_snapshot),
        "",
        "[5/6] 단계별 예시",
        *_v248_stage_example_lines(money_snapshot, 5),
        "",
        "[6/6] 눈에 띄는 돈흐름 후보",
        *_v243_watch_candidate_lines(rows, 6 if not full else 20),
        "",
        "판독",
        "- 최종검증 통과와 paper trade_ready 통과는 다릅니다. 이제 둘을 분리해 봅니다.",
        "- 정보 신선도 부족이 반복되면 조건 완화가 아니라 저장 직전 overlay/urgent 배관부터 봅니다.",
        "- +1.2 먼저 도달했고 -0.7 먼저가 아니었던 탈락 후보가 핵심 복기 대상입니다.",
    ]
    if full:
        all_rows = _v241_watch_rows(3.0)
        lines += ["", "[FULL] 현재버전 전체 전략검증 참고", *_v242_watch_full_lines(all_rows, 50)]
    return "\n".join(lines)


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    return _v248_strategy_watch_text_from_rows(full)


def _v248_score_text_direct() -> str:
    closed = _v248_money_strategy_closed_rows()
    watch_rows = _v245_money_watch_rows(3.0)
    missed = _v247_missed_money_watch_rows(watch_rows, _v246_money_snapshot_rows())
    lines = [
        "📊 모의매매 성과 /score",
        "- 기준: 돈흐름 재가속 단일검증 전략 누적만 표시합니다.",
        "- 페이퍼봇 /pversion_score와 같은 전략 기준으로 맞췄습니다.",
        "- 과거 눌림/구전략 장부는 보관하지만 현재 판단에서는 제외합니다.",
        "",
        "[1/4] 돈흐름 전략 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        "",
        "[2/4] 종료 사유",
        *_v248_exit_reason_lines(closed, 6),
        "",
        "[3/4] 돈흐름 관찰 참고",
        *_v245_strategy_score_summary_lines(),
        "",
        "[4/4] 놓친 후보 요약",
        f"- 탈락 후 +1.2 먼저 도달: {len(missed)}개",
        *_v248_missed_bucket_lines(watch_rows, _v246_money_snapshot_rows()),
        "",
        "판독",
        "- /score는 버전 기준 0전이 아니라 돈흐름 전략 누적을 본다.",
        "- 버전별 과거 비교는 /version_score 참고",
        "- 후보수는 임시로 넓히지 않고, 좋은 후보를 죽인 탈락조건만 찾는다.",
    ]
    return "\n".join(lines)


def _v250_score_text_direct_for_cache() -> str:
    """v251: /score handler에서는 호출 금지. 백그라운드 score cache 생성 전용."""
    try:
        return _v248_score_text_direct()
    except Exception as exc:
        log_error("v248_score_text", exc)
        return _v245_build_score_text_direct()


def _v248_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    rows = [r for r in ((snap or {}).get("rows") or []) if isinstance(r, dict)]
    money_rows = [r for r in rows if str(r.get("strategy_key") or r.get("route") or r.get("paper_route") or "") == MONEY_REACCEL_MAIN_KEY]
    paper_ready = [r for r in money_rows if _v247_bool_ready(r)]
    closed = _v248_money_strategy_closed_rows()
    watch_rows = _v245_money_watch_rows(3.0)
    missed = _v247_missed_money_watch_rows(watch_rows, money_rows)
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 돈흐름 재가속 단일검증 전략 누적만 현재 판단에 사용합니다.",
        "- 후보를 임시로 넓히지 않고, trade_ready 탈락 이유와 놓친 후보를 먼저 봅니다.",
        "- 과거 눌림/구전략 장부는 보관하지만 현재 판단에서는 제외합니다.",
        "",
        "[1/6] 돈흐름 전략 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        "",
        "[2/6] 현재 돈흐름 후보 흐름",
        f"- 전체 후보 {len(rows)}개 / 돈흐름 후보 {len(money_rows)}개 / trade_ready {len(paper_ready)}개 / 탈락 {max(0, len(money_rows)-len(paper_ready))}개 / snapshot {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())):.1f}초 전",
        _simple_ws_line(money_rows),
        _simple_micro_line(money_rows),
        "",
        "[3/6] trade_ready 못 간 이유",
        *_v247_money_gate_summary_lines(money_rows),
        "",
        "[4/6] 정보 신선도 부족 세분화",
        *_v248_fresh_breakdown_lines(money_rows),
        "",
        "[5/6] 탈락했는데 오른 후보",
        *_v247_missed_lines(watch_rows, money_rows, 5),
        *_v248_missed_bucket_lines(watch_rows, money_rows),
        "",
        "[6/6] 단계별 예시 / 판독",
        "🧪 trade_ready 통과",
        *_v248_stage_example_lines(paper_ready, 4),
        "❔ trade_ready 탈락",
        *_v248_stage_example_lines([r for r in money_rows if not _v247_bool_ready(r)], 5),
        "",
        "판독",
        "- 최종검증 통과와 paper trade_ready 통과는 별도입니다.",
        "- 정보 신선도 부족이 반복되면 조건 완화가 아니라 저장 직전 overlay/urgent 배관부터 봅니다.",
        "- 반복해서 오른 탈락 사유가 나오면 그 조건만 자동매매에도 가져갈 수 있게 미세조정합니다.",
        "- 표본 50전 전까지 자동매매 판단 금지",
    ]
    return "\n".join(lines)


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        snap = _v212_read_candidate_snapshot()
        save_json(FILES["quality_summary"], _v238_quality_payload(_v248_quality_text_from_snapshot(snap), snap=snap))
    except Exception as exc:
        log_error("v248_quality_cache", exc)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    try:
        snap = _v212_read_candidate_snapshot()
        txt = _v248_quality_text_from_snapshot(snap)
        save_json(FILES["quality_summary"], _v238_quality_payload(txt, snap=snap))
        return txt
    except Exception as exc:
        log_error("v248_quality_text", exc)
        return _v248_quality_text_from_snapshot(_v212_read_candidate_snapshot())



# ===============================
# v2.13.249: 돈흐름 paper_latest 기준 단일화 + 저장 전 micro/WS target 선갱신 수술
# - trade_ready/탈락이유 분석 원천을 clean_candidate_snapshot이 아니라 paper_candidates_latest로 통일한다.
# - paper_latest 저장 전에 돈흐름 후보를 WS/micro target에 먼저 올리고, 저장 직전 force overlay 후 consume_row/trade_ready를 판정한다.
# - post-scan snapshot refresh가 paper_latest를 다시 덮어쓰던 경로를 차단한다.
# - 조건 완화, 후보 B급 확장, 청산조건 변경, 자동매수/BUY_READY 변경 없음.
# ===============================

V249_PRE_EXPORT_TARGET_WAIT_SEC = float(os.getenv("CLEAN_V249_PRE_EXPORT_TARGET_WAIT_SEC", "0.35"))
V249_PAPER_LATEST_READ_MAX = int(os.getenv("CLEAN_V249_PAPER_LATEST_READ_MAX", "400"))


def _v249_is_money_row(row: Dict[str, Any]) -> bool:
    return str((row or {}).get("strategy_key") or (row or {}).get("route") or (row or {}).get("paper_route") or "") == MONEY_REACCEL_MAIN_KEY


def _v249_read_paper_latest_rows(limit: int = V249_PAPER_LATEST_READ_MAX) -> List[Dict[str, Any]]:
    try:
        rows = tail_jsonl(FILES.get("paper_latest", BASE_DIR / "paper_candidates_latest.jsonl"), max_lines=limit)
        return [r for r in rows if isinstance(r, dict)]
    except Exception as exc:
        log_error("v249_read_paper_latest", exc)
        return []


def _v249_money_paper_latest_rows(limit: int = V249_PAPER_LATEST_READ_MAX) -> List[Dict[str, Any]]:
    return [r for r in _v249_read_paper_latest_rows(limit) if _v249_is_money_row(r)]


def _v246_current_snapshot_rows() -> List[Dict[str, Any]]:  # type: ignore[override]
    # v249: trade_ready/탈락이유 판단은 paper_bot이 실제 읽는 latest 파일을 원천으로 쓴다.
    return _v249_read_paper_latest_rows()


def _v246_money_snapshot_rows() -> List[Dict[str, Any]]:  # type: ignore[override]
    return _v249_money_paper_latest_rows()


def _v249_stamp_external_context(rr: Dict[str, Any], note: str) -> Dict[str, Any]:
    ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    for k in (
        "ws_row_status", "ws_age_sec", "ws_targeted", "ws_cache_ts", "current_price_ws_gap_pct",
        "micro_fresh", "micro_row_status", "micro_age_sec", "micro_targeted", "micro_spread_pct",
        "micro_trade_buy_ratio_30", "micro_bid_ask_wall_ratio", "micro_ask_wall_pressure", "micro_sell_trade_pressure",
    ):
        if k in rr:
            ctx[k] = rr.get(k)
    ctx["external_overlay_at"] = now_ts()
    ctx["external_overlay_note"] = note
    rr["entry_context"] = ctx
    return rr


def _v249_force_external_overlay(items: List[Dict[str, Any]], *, note: str = "v249_force_overlay_before_paper_latest") -> List[Dict[str, Any]]:
    try:
        maps = _v218_refresh_external_maps(force=True, ttl=0.0)
    except Exception as exc:
        log_error("v249_force_external_maps", exc)
        maps = _v218_refresh_external_maps(force=False, ttl=0.1)
    out: List[Dict[str, Any]] = []
    for r in items or []:
        if not isinstance(r, dict):
            continue
        try:
            rr = _v233_overlay_row_with_maps(r, maps)
            rr["external_refreshed_by"] = note
            rr = _v249_stamp_external_context(rr, note)
            out.append(rr)
        except Exception as exc:
            log_error("v249_force_overlay_row", exc)
            out.append(dict(r))
    try:
        with _state_lock:
            STATE["v249_force_overlay_rows"] = len(out)
            STATE["v249_force_overlay_map_age_sec"] = round(now_ts() - fnum(maps.get("ts"), now_ts()), 3) if isinstance(maps, dict) else -1
    except Exception:
        pass
    return out


def _v249_prime_external_targets(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> None:
    priority = [r for r in (strict or []) if isinstance(r, dict)]
    # shadow는 복기용이라 target 우선순위에는 보조로만 둔다. strict 돈흐름 후보가 맨 앞에 와야 한다.
    support = [r for r in (shadow or []) if isinstance(r, dict)][:80]
    try:
        update_ws_targets(priority + support, priority_rows=priority, reason="v249_pre_paper_latest_money_candidates")
    except Exception as exc:
        log_error("v249_pre_update_ws_targets", exc)
    try:
        update_micro_targets(priority + support, priority_rows=priority, reason="v249_pre_paper_latest_money_candidates")
    except Exception as exc:
        log_error("v249_pre_update_micro_targets", exc)
    wait_sec = max(0.0, min(V249_PRE_EXPORT_TARGET_WAIT_SEC, 1.5))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    try:
        # target 파일을 올린 뒤 sidecar cache를 강제로 다시 읽어 factory 판정 직전 stale map 재사용을 끊는다.
        refresh_micro_cache()
        refresh_external_ws_cache()
    except Exception as exc:
        log_error("v249_pre_refresh_external_cache", exc)
    try:
        with _state_lock:
            STATE["v249_pre_export_target_wait_sec"] = wait_sec
            STATE["v249_pre_export_priority"] = len(priority)
            STATE["v249_pre_export_support"] = len(support)
    except Exception:
        pass


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v249: paper_latest 저장 전 target 선갱신 + force overlay + latest 단일 원천.

    v215 fast factory를 기반으로 하되, 기존 문제였던
    'paper_latest 저장 → 나중에 target 갱신' 순서를 'target 선갱신 → 최신 overlay → paper_latest 저장'으로 바꾼다.
    """
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    result = {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
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
        "data_quality_note": "v249_pre_target_force_overlay_latest_single_source",
        "dup_skip": 0,
        "write_error": "",
        "last_ticker": "-",
        "factory_mode": "v249_pre_target_force_overlay_no_post_rewrite",
        "archive_deferred": 0,
        "risk_sync_deferred": True,
    }
    try:
        enqueue_execution_risk((strict or []) + (shadow or []))
    except Exception as exc:
        log_error("v249_enqueue_execution_risk", exc)

    try:
        _v249_prime_external_targets(list(strict or []), list(shadow or []))
        strict = _v249_force_external_overlay(list(strict or []), note="v249_force_overlay_before_paper_latest_strict")
        shadow = _v249_force_external_overlay(list(shadow or []), note="v249_force_overlay_before_paper_latest_shadow")
        mark_ws_target_flags(strict)
        mark_ws_target_flags(shadow)
        with _state_lock:
            STATE["factory_external_overlay"] = "v249_pre_target_force_overlay_before_export"
            STATE["factory_external_overlay_ts"] = now_ts()
    except Exception as exc:
        log_error("v249_factory_external_overlay", exc)

    def _refresh_latest_ttl(row: Dict[str, Any], lane: str) -> Dict[str, Any]:
        nowv = now_ts()
        rr = dict(row or {})
        rr["created_at"] = nowv
        rr["source_created_at"] = nowv
        rr["factory_saved_at"] = nowv
        rr["expires_at"] = nowv + max(30.0, CANDIDATE_TTL_SEC)
        rr["factory_mode"] = "v249_pre_target_force_overlay"
        rr["archive_deferred"] = True
        rr["candidate_ttl_refreshed"] = True
        rr["lane"] = lane
        rr["scan_id"] = rr.get("scan_id") or scan_id or str(STATE.get("scan_id") or f"scan-{int(nowv)}")
        rr["snapshot_id"] = rr.get("snapshot_id") or rr.get("scan_id")
        rr["candidate_created_at"] = rr.get("candidate_created_at") or rr.get("created_at") or nowv
        ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
        ctx = dict(ctx)
        ctx["factory_saved_at"] = nowv
        ctx["candidate_ttl_refreshed"] = True
        ctx["factory_mode"] = "v249_pre_target_force_overlay"
        ctx["scan_id"] = rr.get("scan_id")
        ctx["snapshot_id"] = rr.get("snapshot_id")
        ctx["candidate_created_at"] = rr.get("candidate_created_at")
        ctx["paper_latest_source"] = "v249_latest_single_source"
        rr["entry_context"] = ctx
        return rr

    dup_reasons: Counter = Counter()
    for lane, items in (("strict", strict), ("shadow", shadow)):
        latest_rows: List[Dict[str, Any]] = []
        for item in items or []:
            row = consume_row(item, lane, ts, scan_id=scan_id)
            if lane == "shadow":
                row["review_only"] = True
                row["open_eligible"] = False
                row["paper_bot_open"] = False
            else:
                can_open = bool(row.get("trade_ready"))
                row["review_only"] = not can_open
                row["open_eligible"] = can_open
                row["paper_bot_open"] = can_open
            latest_rows.append(_refresh_latest_ttl(row, lane))
        latest_path = FILES["paper_latest"] if lane == "strict" else FILES["shadow_latest"]
        latest_ok, latest_err = write_jsonl_replace(latest_path, latest_rows)
        if not latest_ok:
            result["write_error"] = f"latest:{latest_err}"
        if lane == "strict":
            result["paper_latest_written"] = len(latest_rows)
            result["latest_trade_ready"] = sum(1 for r in latest_rows if r.get("paper_bot_open"))
            result["latest_strict_observe"] = sum(1 for r in latest_rows if not r.get("paper_bot_open"))
            result["latest_final_recheck_wait"] = sum(1 for r in latest_rows if r.get("final_entry_action") == "recheck_wait")
            result["latest_final_observe"] = sum(1 for r in latest_rows if r.get("final_entry_action") == "observe")
            result["paper_written"] = 0
            result["trade_ready_written"] = result["latest_trade_ready"]
            result["strict_observe_written"] = result["latest_strict_observe"]
            result["major_watch_written"] = sum(1 for r in latest_rows if r.get("major_watch"))
            for row in latest_rows[-12:]:
                _recent_strict.append(row)
        else:
            result["shadow_latest_written"] = len(latest_rows)
            result["shadow_written"] = 0
            for row in latest_rows[-12:]:
                _recent_shadow.append(row)
        if latest_rows:
            result["last_ticker"] = str(latest_rows[-1].get("ticker") or "-")
        result["archive_deferred"] += len(latest_rows)
    result["dup_skip_reason"] = dict(dup_reasons)
    with _state_lock:
        STATE["factory_archive_deferred"] = result.get("archive_deferred", 0)
        STATE["factory_mode"] = result.get("factory_mode")
        STATE["v249_latest_single_source"] = True
    return result


def _v229_refresh_candidate_snapshot_once(expected_scan_id: str = "", *, attempt: int = 1, total_attempts: int = 1, waited_sec: float = 0.0) -> Tuple[bool, Dict[str, Any]]:  # type: ignore[override]
    """v249: post-scan external refresh는 snapshot만 갱신하고 paper_latest를 덮어쓰지 않는다."""
    snap = _v212_read_candidate_snapshot()
    if not isinstance(snap, dict) or not snap.get("rows"):
        return False, {}
    scan_id = str(snap.get("scan_id") or expected_scan_id or STATE.get("scan_id") or f"scan-{int(now_ts())}")
    if expected_scan_id and scan_id and scan_id != expected_scan_id:
        return False, {}
    rows0 = [r for r in (snap.get("rows") or []) if isinstance(r, dict)]
    maps = _v218_refresh_external_maps(force=True, ttl=0.0)
    nowv = now_ts()
    rows = [_v236_fix_candidate_meta(_v233_overlay_row_with_maps(r, maps), scan_id=scan_id, lane="strict", refresh_ttl=True, source="v249_post_refresh_snapshot_only") for r in rows0]
    ext = _v212_count_external(rows)
    old_ext = snap.get("external") if isinstance(snap.get("external"), dict) else {}
    target = snap.get("target") if isinstance(snap.get("target"), dict) else {}
    refreshed = dict(snap)
    refreshed.update({
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v249",
        "stage": "external_refreshed_after_scan",
        "source": "v249_post_scan_snapshot_only_no_paper_latest_rewrite",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": scan_id,
        "candidate_count": len(rows),
        "rows": rows,
        "external": {
            **old_ext,
            **ext,
            "ws_worker_state": STATE.get("ws_state", old_ext.get("ws_worker_state", "-")),
            "ws_worker_targets": STATE.get("ws_targets", old_ext.get("ws_worker_targets", 0)),
            "ws_worker_fresh": STATE.get("ws_fresh", old_ext.get("ws_worker_fresh", 0)),
            "ws_last_age_sec": STATE.get("ws_last_age_sec", old_ext.get("ws_last_age_sec", -1)),
            "micro_worker_state": STATE.get("micro_state", old_ext.get("micro_worker_state", "-")),
            "micro_worker_targets": STATE.get("micro_targets", old_ext.get("micro_worker_targets", 0)),
            "micro_worker_fresh": STATE.get("micro_fresh", old_ext.get("micro_worker_fresh", 0)),
            "overlay_mode": "v249_post_scan_force_map_snapshot_only",
            "refreshed_ts": nowv,
            "refresh_attempt": attempt,
            "refresh_attempt_total": total_attempts,
            "refresh_waited_sec": round(waited_sec, 2),
            "cache_map_age_sec": round(now_ts() - fnum(maps.get("ts"), now_ts()), 2) if isinstance(maps, dict) else -1,
        },
        "target": {
            **target,
            "post_scan_refresh": True,
            "post_scan_refresh_attempt": attempt,
            "post_scan_refresh_total": total_attempts,
            "post_scan_refresh_waited_sec": round(waited_sec, 2),
            "paper_latest_rewrite": False,
            "priority_rule": "v249: post-refresh snapshot only; paper_latest remains factory single source",
        },
        "note": "v249: post-refresh does not rewrite paper_candidates_latest",
    })
    save_json(FILES["candidate_snapshot"], refreshed)
    with _state_lock:
        STATE["candidate_snapshot_ts"] = nowv
        STATE["candidate_snapshot_count"] = len(rows)
        STATE["candidate_snapshot_source"] = "v249_post_scan_snapshot_only"
        STATE["candidate_snapshot_stage"] = "external_refreshed_after_scan"
        STATE["candidate_snapshot_external"] = refreshed.get("external", {})
        STATE["v249_post_refresh_no_paper_latest_rewrite"] = True
    return True, ext


def _v248_strategy_watch_text_from_rows(full: bool = False) -> str:  # type: ignore[override]
    rows = _v245_money_watch_rows(3.0)
    grouped = _v242_group_watch_rows(rows)
    st = _v242_strategy_stats(rows)
    money_snapshot = _v249_money_paper_latest_rows()
    paper_ready = [r for r in money_snapshot if _v247_bool_ready(r)]
    missed = _v247_missed_money_watch_rows(rows, money_snapshot)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결: 돈흐름 재가속 단일검증만 사용합니다.",
        "- v249 기준: trade_ready/탈락이유는 paper_candidates_latest 실제 소비파일 기준입니다.",
        f"- 기준: 최근 3시간 / 돈흐름 관찰이벤트 {len(rows)}개 / 후보묶음 {len(grouped)}개",
        f"- paper_latest 돈흐름 후보: {len(money_snapshot)}개 / trade_ready: {len(paper_ready)}개 / 놓친 후보 {len(missed)}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/6] 돈흐름 재가속 3시간",
    ]
    if rows:
        n = int(st.get("n", 0) or 0)
        hit = int(st.get("hit", 0) or 0)
        minus_first = int(st.get("minus_first", 0) or 0)
        icon = "✅" if n >= 20 and hit >= minus_first else ("⚠️" if n >= 20 else "❔")
        sample = "표본충분" if n >= STRATEGY_WATCH_DECISION_MIN else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
        lines.append(
            f"{icon} 돈흐름 재가속: 후보 {n} / +1.2 {hit}({hit/max(n,1)*100:.0f}%) / "
            f"현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/max(n,1)*100:.0f}%) / "
            f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / "
            f"micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / -0.7먼저 {minus_first} / {sample}"
        )
    else:
        lines.append("- 아직 기록 없음")
    lines += [
        "",
        "[2/6] paper_latest trade_ready 못 간 이유",
        *_v247_money_gate_summary_lines(money_snapshot),
        "",
        "[3/6] 신선도 부족 세분화",
        *_v248_fresh_breakdown_lines(money_snapshot),
        "",
        "[4/6] 탈락했는데 오른 돈흐름 후보",
        *_v247_missed_lines(rows, money_snapshot, 5),
        *_v248_missed_bucket_lines(rows, money_snapshot),
        "",
        "[5/6] 단계별 예시",
        *_v248_stage_example_lines(money_snapshot, 5),
        "",
        "[6/6] 눈에 띄는 돈흐름 후보",
        *_v243_watch_candidate_lines(rows, 6 if not full else 20),
        "",
        "판독",
        "- v249부터 snapshot 추정이 아니라 paper_latest 실제 후보 기준으로 봅니다.",
        "- 정보 신선도 부족이 반복되면 조건 완화가 아니라 저장 직전 target/overlay 배관을 봅니다.",
        "- +1.2 먼저 도달했고 -0.7 먼저가 아니었던 탈락 후보가 핵심 복기 대상입니다.",
    ]
    if full:
        all_rows = _v241_watch_rows(3.0)
        lines += ["", "[FULL] 현재버전 전체 전략검증 참고", *_v242_watch_full_lines(all_rows, 50)]
    return "\n".join(lines)


def _v248_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    # v249: 현재 paper 판단 분석은 snapshot이 아니라 paper_latest 실제 소비파일 기준.
    money_rows = _v249_money_paper_latest_rows()
    paper_ready = [r for r in money_rows if _v247_bool_ready(r)]
    closed = _v248_money_strategy_closed_rows()
    watch_rows = _v245_money_watch_rows(3.0)
    missed = _v247_missed_money_watch_rows(watch_rows, money_rows)
    snap = snap if isinstance(snap, dict) else _v212_read_candidate_snapshot()
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 돈흐름 재가속 단일검증 전략 누적만 현재 판단에 사용합니다.",
        "- v249 기준: trade_ready/탈락이유는 paper_candidates_latest 실제 소비파일 기준입니다.",
        "- clean_candidate_snapshot은 상태 참고용이며, paper OPEN 판단 원천은 아닙니다.",
        "",
        "[1/6] 돈흐름 전략 성과",
        _compact_stat_line("돈흐름 재가속 정식", closed),
        "",
        "[2/6] paper_latest 돈흐름 후보 흐름",
        f"- 돈흐름 후보 {len(money_rows)}개 / trade_ready {len(paper_ready)}개 / 탈락 {max(0, len(money_rows)-len(paper_ready))}개 / latest 기준",
        _simple_ws_line(money_rows),
        _simple_micro_line(money_rows),
        "",
        "[3/6] paper_latest trade_ready 못 간 이유",
        *_v247_money_gate_summary_lines(money_rows),
        "",
        "[4/6] 정보 신선도 부족 세분화",
        *_v248_fresh_breakdown_lines(money_rows),
        "",
        "[5/6] 탈락했는데 오른 후보",
        *_v247_missed_lines(watch_rows, money_rows, 5),
        *_v248_missed_bucket_lines(watch_rows, money_rows),
        "",
        "[6/6] 단계별 예시 / 판독",
        "🧪 trade_ready 통과",
        *_v248_stage_example_lines(paper_ready, 4),
        "❔ trade_ready 탈락",
        *_v248_stage_example_lines([r for r in money_rows if not _v247_bool_ready(r)], 5),
        "",
        "판독",
        "- 최종검증 통과와 paper trade_ready 통과는 별도입니다.",
        "- paper_latest 저장 전에 target 선갱신 + force overlay를 적용했습니다.",
        "- 정보 신선도 부족이 남으면 조건 완화보다 sidecar 수집 지연/target 반영을 먼저 봅니다.",
        "- 표본 50전 전까지 자동매매 판단 금지",
    ]
    try:
        snap_age = max(0.0, now_ts() - fnum((snap or {}).get("updated_ts"), now_ts())) if isinstance(snap, dict) else -1
        lines.insert(4, f"- 참고 snapshot: {len((snap or {}).get('rows') or []) if isinstance(snap, dict) else 0}개 / {snap_age:.1f}초 전")
    except Exception:
        pass
    return "\n".join(lines)


# ===============================
# v2.13.250: 평균가 회귀 반등 단타 단일검증 전환
# - 돈흐름 재가속 본선은 중단하고 관찰/복기 전용으로 내린다.
# - 정식 paper OPEN은 평균가/VWAP 아래 이탈 후 저점 방어 + 반등 전환 + micro 회복 후보만 사용한다.
# - 처음 수치는 일반적인 VWAP 회귀 단타 기준으로 둔다: VWAP -0.3~-1.5%, 3분돈 700만+, spread 0.5% 이하, 매수비 0.52+.
# - TP 분할 없음. paper_bot은 기존 전량익절/전량손절 구조를 유지한다.
# - 자동매수/BUY_READY/v343/paper 장부 삭제 없음.
# ===============================

VWAP_REVERSION_MAIN_KEY = "vwap_reversion_main"
VWAP_REVERSION_MAIN_NAME = "평균가 회귀 반등 단타"
VWAP_REVERSION_WATCH_KIND = "vwap_reversion"
VWAP_REVERSION_MAIN_MODE = "vwap_reversion_only"

VWAP_REV_MIN_GAP = float(os.getenv("CLEAN_VWAP_REV_MIN_GAP", "-1.50"))
VWAP_REV_MAX_GAP = float(os.getenv("CLEAN_VWAP_REV_MAX_GAP", "-0.30"))
VWAP_REV_MIN_3M_KRW = float(os.getenv("CLEAN_VWAP_REV_MIN_3M_KRW", "7000000"))
VWAP_REV_MAX_SPREAD = float(os.getenv("CLEAN_VWAP_REV_MAX_SPREAD", "0.50"))
VWAP_REV_MIN_BUY_RATIO = float(os.getenv("CLEAN_VWAP_REV_MIN_BUY_RATIO", "0.52"))
VWAP_REV_MAX_FROM_LOW = float(os.getenv("CLEAN_VWAP_REV_MAX_FROM_LOW", "5.00"))
VWAP_REV_MIN_FROM_LOW = float(os.getenv("CLEAN_VWAP_REV_MIN_FROM_LOW", "0.08"))
VWAP_REV_MIN_SCORE = float(os.getenv("CLEAN_VWAP_REV_MIN_SCORE", "2.60"))
VWAP_REV_STRICT_MAX = int(os.getenv("CLEAN_VWAP_REV_STRICT_MAX", "40"))

try:
    STRATEGY_WATCH_LABELS[VWAP_REVERSION_WATCH_KIND] = "평균가 회귀 반등"
except Exception:
    pass


def _v250_text_has_vwap(v: Any) -> bool:
    s = str(v or "")
    return (
        VWAP_REVERSION_MAIN_KEY in s
        or "vwap_reversion" in s
        or "평균가 회귀" in s
        or "평균회귀" in s
        or "VWAP 회귀" in s
    )


def _v250_is_vwap_row(row: Dict[str, Any]) -> bool:
    fields = [
        (row or {}).get("strategy"), (row or {}).get("route"), (row or {}).get("paper_route"),
        (row or {}).get("strategy_key"), (row or {}).get("paper_strategy_key"),
        (row or {}).get("strategy_label"), (row or {}).get("paper_strategy_label"),
        (row or {}).get("strategy_display"),
    ]
    ctx = (row or {}).get("entry_context") if isinstance((row or {}).get("entry_context"), dict) else {}
    raw = (row or {}).get("raw") if isinstance((row or {}).get("raw"), dict) else {}
    for k in ("strategy", "route", "paper_route", "strategy_key", "paper_strategy_key", "strategy_label", "paper_strategy_label"):
        fields.append(ctx.get(k))
        fields.append(raw.get(k))
    return any(_v250_text_has_vwap(x) for x in fields)


def _v250_is_focus_item(item: Dict[str, Any]) -> bool:
    return _v250_is_vwap_row(item)


def _v250_is_micro_fresh(row: Dict[str, Any], max_age: float = 8.0) -> bool:
    status = str(_ctxv(row, "micro_row_status", row.get("micro_row_status") or "")).lower()
    if bool(_ctxv(row, "micro_fresh", row.get("micro_fresh"))):
        return True
    age = fnum(_ctxv(row, "micro_age_sec", row.get("micro_age_sec")), 999)
    return ("fresh" in status or "신선" in status) and age <= max_age


def _v250_micro_buy_ratio(row: Dict[str, Any]) -> float:
    return fnum(_ctxv(row, "micro_trade_buy_ratio_30", row.get("micro_trade_buy_ratio_30")), 0)


def _v250_micro_spread(row: Dict[str, Any]) -> float:
    return fnum(_ctxv(row, "micro_spread_pct", row.get("micro_spread_pct") or row.get("orderbook_spread_pct")), 999)


def _v250_vwap_gap(row: Dict[str, Any]) -> float:
    return fnum(row.get("vwap_gap_pct"), fnum(row.get("vwap_gap"), 0))


def _v250_money3(row: Dict[str, Any]) -> float:
    return fnum(row.get("money_flow_3m") or row.get("turnover_3m"), 0)


def _v250_score_vwap_reversion(row: Dict[str, Any]) -> Tuple[float, List[str], List[str]]:
    """일반적인 VWAP 회귀 단타 조건을 점수화한다.

    필수축:
    - VWAP 아래 -0.3~-1.5% 구간
    - 3분돈 700만 이상
    - 저점 방어/반등 전환 신호
    - 과열/추격/펌핑 위험 제외
    - 실제 OPEN은 decide_trade_ready에서 micro fresh/spread/buy ratio까지 최종 확인
    """
    reasons: List[str] = []
    blocks: List[str] = []
    score = 0.0
    price = fnum(row.get("current_price") or row.get("price") or row.get("entry_price"), 0)
    if price <= 0:
        return 0.0, reasons, ["가격 없음"]

    vgap = _v250_vwap_gap(row)
    m3 = _v250_money3(row)
    ch1 = fnum(row.get("change_1"), 0)
    ch3 = fnum(row.get("change_3"), 0)
    from_low = fnum(row.get("from_30m_low_pct") or row.get("from_low_pct"), 0)
    below_high = fnum(row.get("below_30m_high_pct") or row.get("below_high_pct"), 999)
    avg_turn = fnum(row.get("avg_price_turn_pct"), 0)
    lower_wick = fnum(row.get("current_lower_wick_pct"), 0)
    recovery = fnum(row.get("recovery_speed_pct"), 0)
    bb_pos = fnum(row.get("bb_position"), 0)
    stoch_cross = bool(row.get("stoch_cross_up"))
    volume_pump = bool(row.get("volume_pump_risk")) or fnum(row.get("volume_spike_30x"), 0) >= 25

    if not (VWAP_REV_MIN_GAP <= vgap <= VWAP_REV_MAX_GAP):
        blocks.append(f"VWAP 이격 {vgap:+.2f}% 범위밖({VWAP_REV_MIN_GAP:+.1f}~{VWAP_REV_MAX_GAP:+.1f})")
    else:
        score += 1.35
        reasons.append(f"VWAP 아래 회귀권 {vgap:+.2f}%")
        # -0.6~-1.1% 부근을 가장 무난한 회귀권으로 가산
        if -1.10 <= vgap <= -0.45:
            score += 0.35
            reasons.append("평균 회귀 먹을폭 적정")

    if m3 < VWAP_REV_MIN_3M_KRW:
        blocks.append(f"3분돈 {krw_m(m3)} < {krw_m(VWAP_REV_MIN_3M_KRW)}")
    else:
        score += min(1.1, m3 / 25_000_000)
        reasons.append(f"3분 거래대금 {krw_m(m3)}")

    # 저점 방어: 너무 바닥 바로 이탈 중도 아니고, 너무 이미 튄 것도 아닌 구간
    if from_low < VWAP_REV_MIN_FROM_LOW:
        blocks.append("저점 방어 미확인")
    elif from_low > VWAP_REV_MAX_FROM_LOW:
        blocks.append(f"저점대비 과열 {from_low:.2f}%")
    else:
        score += 0.55
        reasons.append(f"저점 방어 {from_low:.2f}%")

    bounce_signals: List[str] = []
    if ch1 >= 0.02:
        score += 0.35; bounce_signals.append(f"1분 반등 {ch1:+.2f}%")
    if avg_turn > 0:
        score += 0.30; bounce_signals.append(f"평균가 턴 {avg_turn:+.2f}%")
    if recovery >= 0.05:
        score += 0.25; bounce_signals.append(f"회복속도 {recovery:+.2f}%")
    if lower_wick >= 0.12:
        score += 0.20; bounce_signals.append(f"아래꼬리 {lower_wick:.2f}%")
    if stoch_cross:
        score += 0.15; bounce_signals.append("단기반등 교차")
    if not bounce_signals:
        blocks.append("반등 전환 신호 부족")
    else:
        reasons.extend(bounce_signals[:3])

    # 너무 고점 바로 아래/펌핑은 평균회귀가 아니라 추격으로 본다.
    if below_high < 0.20:
        blocks.append(f"고점 바로 붙음 {below_high:.2f}%")
    if volume_pump:
        blocks.append("거래량 과폭발/펌핑 위험")
    if bb_pos >= 0.92 and vgap > -0.45:
        blocks.append("밴드 상단 추격권")

    # 과도한 단기 하락 지속은 칼날로 본다.
    if ch3 <= -2.5 and ch1 < 0:
        blocks.append(f"하락 지속 {ch3:+.2f}%")

    return round(score, 3), reasons[:8], blocks[:8]


def _v250_stamp_item(out: Dict[str, Any], score: float, reasons: List[str], blocks: Optional[List[str]] = None) -> Dict[str, Any]:
    out = dict(out or {})
    out["strategy"] = VWAP_REVERSION_MAIN_NAME
    out["strategy_key"] = VWAP_REVERSION_MAIN_KEY
    out["strategy_label"] = VWAP_REVERSION_MAIN_NAME
    out["strategy_display"] = VWAP_REVERSION_MAIN_NAME
    out["paper_strategy_label"] = VWAP_REVERSION_MAIN_NAME
    out["paper_strategy_key"] = VWAP_REVERSION_MAIN_KEY
    out["route"] = VWAP_REVERSION_MAIN_KEY
    out["paper_route"] = VWAP_REVERSION_MAIN_KEY
    out["main_mode"] = VWAP_REVERSION_MAIN_MODE
    out["paper_main_mode"] = VWAP_REVERSION_MAIN_MODE
    out["single_strategy_mode"] = True
    out["watch_kind"] = VWAP_REVERSION_WATCH_KIND
    out["watch_label"] = "평균가 회귀 반등"
    out["vwap_reversion_score"] = round(fnum(score), 3)
    out["vwap_reversion_reasons"] = list(reasons or [])[:8]
    out["vwap_reversion_blocks"] = list(blocks or [])[:8]
    # score는 화면/페이퍼봇 최소점수와 호환되게 4점대 스케일로 둔다.
    out["score"] = max(fnum(out.get("score"), 0), round(4.0 + min(1.6, fnum(score) * 0.33), 3))
    out["edge_score"] = out["score"]
    out["one_liner"] = "평균가 회귀 반등: " + (" / ".join(list(reasons or [])[:5]) if reasons else "조건 통과")
    out["mainline_note"] = "v250 정식 모의매매는 평균가 회귀 반등 단타 단일검증만 사용"
    out["money_reaccel_main_disabled"] = True
    out["pullback_main_disabled"] = True
    out["review_only"] = False
    return out


def _v250_prepare_old_shadow(item: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(item or {})
    out["review_only"] = True
    out["paper_bot_open"] = False
    out["open_eligible"] = False
    out["trade_ready"] = False
    out["observe_only"] = True
    out["mainline_disabled_reason"] = "v250에서 돈흐름/눌림 본선은 정식 모의매매에서 제외"
    notes = list(out.get("aux_notes") or [])
    notes.append("v250부터 평균가 회귀 반등 단타 비교용 복기/관찰")
    out["aux_notes"] = notes[-8:]
    return out


_v250_base_build_candidates = _v244_base_build_candidates if ' _v244_base_build_candidates' in globals() else None
# 위 문자열 검사는 의미가 없으므로 안전하게 globals에서 다시 잡는다.
_v250_base_build_candidates = globals().get("_v244_base_build_candidates", globals().get("build_candidates"))


def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    # v244 이전 원본 후보생성기를 사용해 기존 feature/profile을 최대한 재사용한다.
    base_fn = globals().get("_v244_base_build_candidates")
    if not callable(base_fn):
        base_fn = _v250_base_build_candidates
    base_strict, base_shadow, rejects, examples = base_fn(rows)  # type: ignore[misc]
    pool: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(base_strict or []) + list(base_shadow or []):
        t = _v241_watch_ticker(item)
        if not t or t in seen:
            continue
        seen.add(t)
        pool.append(item)

    scored: List[Tuple[float, Dict[str, Any], List[str], List[str]]] = []
    rejected_examples: List[Dict[str, Any]] = []
    for item in pool:
        s, reasons, blocks = _v250_score_vwap_reversion(item)
        # strict 파일에는 평균회귀 조건의 형태를 통과한 후보만 담는다.
        if s >= VWAP_REV_MIN_SCORE and not blocks:
            scored.append((s, item, reasons, blocks))
        elif len(rejected_examples) < 8 and (s >= 2.8 or _v250_money3(item) >= VWAP_REV_MIN_3M_KRW):
            rejected_examples.append({"ticker": _v241_watch_ticker(item), "reason": blocks[0] if blocks else "점수 부족", "score": round(s, 2), "line": " / ".join((reasons or blocks)[:4])})
    scored.sort(key=lambda x: (x[0], _v250_money3(x[1]), -abs(_v250_vwap_gap(x[1])), fnum(x[1].get("change_1"), 0)), reverse=True)
    strict = [_v250_stamp_item(item, s, reasons, blocks) for s, item, reasons, blocks in scored[:max(1, VWAP_REV_STRICT_MAX)]]
    strict_tickers = {_v241_watch_ticker(x) for x in strict}
    shadow: List[Dict[str, Any]] = []
    shadow_seen: set[str] = set()
    for item in list(base_strict or []) + list(base_shadow or []):
        t = _v241_watch_ticker(item)
        if not t or t in strict_tickers or t in shadow_seen:
            continue
        shadow.append(_v250_prepare_old_shadow(item))
        shadow_seen.add(t)
    if rejected_examples:
        examples = list(rejected_examples)[:8]
    with _state_lock:
        STATE["v250_main_strategy"] = VWAP_REVERSION_MAIN_NAME
        STATE["v250_vwap_strict"] = len(strict)
        STATE["v250_old_shadow"] = len(shadow)
        STATE["v250_note"] = "정식 모의매매는 평균가 회귀 반등 단타 단일검증만 사용"
    return strict, shadow, rejects, examples


_v250_base_decide_trade_ready = decide_trade_ready

def decide_trade_ready(item: Dict[str, Any]) -> Tuple[bool, str, List[str]]:  # type: ignore[override]
    if not _v250_is_focus_item(item):
        return False, "복기 전용", ["v250: 정식 모의매매는 평균가 회귀 반등 단타만 허용"]
    reasons: List[str] = []
    s, score_reasons, blocks = _v250_score_vwap_reversion(item)
    if s < VWAP_REV_MIN_SCORE:
        reasons.append(f"평균회귀 점수 {s:.2f} < {VWAP_REV_MIN_SCORE:.2f}")
    reasons.extend(blocks)
    if not _v250_is_micro_fresh(item):
        reasons.append("micro 정보 신선도 부족")
    spread = _v250_micro_spread(item)
    buy = _v250_micro_buy_ratio(item)
    if spread < 900 and spread > VWAP_REV_MAX_SPREAD:
        reasons.append(f"스프레드 {spread:.2f}% > {VWAP_REV_MAX_SPREAD:.2f}%")
    if buy > 0 and buy < VWAP_REV_MIN_BUY_RATIO:
        reasons.append(f"매수비 {buy:.2f} < {VWAP_REV_MIN_BUY_RATIO:.2f}")
    elif buy <= 0:
        reasons.append("매수비 없음")
    # 기존 최종검증의 과열/펌핑/추격류 경고는 유지하되, VWAP 아래 자체는 차단으로 보지 않는다.
    blob = _v247_text_blob(item)
    if "거래량 과폭발" in blob or "펌핑" in blob or "고점 바로" in blob or "저점대비 과열" in blob:
        reasons.append("과열/추격 위험")
    if str(item.get("chase_risk") or "") == "높음":
        reasons.append("추격위험 높음")
    ok = not reasons
    if ok:
        return True, "평균가 회귀 반등 정식 모의진입", list(score_reasons or [])[:5]
    return False, "평균회귀 후보 관찰", reasons[:8]


# v251: 평균회귀 후보가 v244/v246 돈흐름 단일 경로에서 다시 observe로 눌리는 문제를 제거한다.
# 실제 row 생성은 단일전략 전환 전 원본 consume_row를 직접 사용한다.
_v250_base_consume_row = globals().get("_v244_base_consume_row", consume_row)

def consume_row(item: Dict[str, Any], lane: str, ts: Optional[float] = None, scan_id: str = "") -> Dict[str, Any]:  # type: ignore[override]
    row = _v250_base_consume_row(item, lane, ts, scan_id)
    if _v250_is_focus_item(item) or _v250_is_focus_item(row):
        row = _v250_stamp_item(row, fnum(item.get("vwap_reversion_score", row.get("vwap_reversion_score")), 0), list(item.get("vwap_reversion_reasons") or row.get("vwap_reversion_reasons") or []), list(item.get("vwap_reversion_blocks") or row.get("vwap_reversion_blocks") or []))
        row["watch_kind"] = VWAP_REVERSION_WATCH_KIND
        row["watch_label"] = "평균가 회귀 반등"
    else:
        row.update({
            "review_only": True,
            "paper_bot_open": False,
            "open_eligible": False,
            "trade_ready": False,
            "decision": "shadow_review" if lane != "strict" else "strict_observe",
            "event_type": "single_strategy_shadow" if lane != "strict" else "strict_observe",
            "mainline_disabled_reason": "v250 정식 모의매매에서 제외",
        })
    return row


# strategy_watch도 v250 전략만 새로 쌓는다.
_v250_base_watch_select = globals().get("_v241_select_watch_candidates")

def _v241_select_watch_candidates(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    scan_id = str(STATE.get("scan_id") or f"scan-{int(now_ts())}")
    pool: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(strict or []) + list(shadow or []):
        t = _v241_watch_ticker(item)
        if not t or t in seen:
            continue
        seen.add(t)
        pool.append(item)
    events: List[Tuple[float, Dict[str, Any]]] = []
    for item in pool:
        s, reasons, blocks = _v250_score_vwap_reversion(item)
        if s >= max(2.8, VWAP_REV_MIN_SCORE - 0.9):
            ev = _v241_watch_event(VWAP_REVERSION_WATCH_KIND, _v250_stamp_item(item, s, reasons, blocks), s, reasons if not blocks else reasons + ["관찰: " + blocks[0]], scan_id)
            ev["source_reject_bucket"] = _v247_reject_bucket(item)
            ev["source_reject_detail"] = _v247_reject_reason_detail(item)
            events.append((s, ev))
    events.sort(key=lambda x: (x[0], fnum(x[1].get("money_flow_3m"), 0), fnum(x[1].get("current_return_pct"), 0)), reverse=True)
    return [ev for _s, ev in events[:STRATEGY_WATCH_MAX_PER_SCAN]]


def _v250_watch_rows(window_hours: float = 3.0) -> List[Dict[str, Any]]:
    cutoff = now_ts() - max(0.1, window_hours) * 3600.0
    obj = _v241_watch_load()
    rows: List[Dict[str, Any]] = []
    for ev in (obj.get("active") or {}).values():
        if isinstance(ev, dict) and str(ev.get("watch_kind") or "") == VWAP_REVERSION_WATCH_KIND and fnum(ev.get("created_at"), 0) >= cutoff:
            rows.append(ev)
    for ev in obj.get("recent_done") or []:
        if isinstance(ev, dict) and str(ev.get("watch_kind") or "") == VWAP_REVERSION_WATCH_KIND and fnum(ev.get("created_at"), 0) >= cutoff:
            rows.append(ev)
    rows.sort(key=lambda r: fnum(r.get("created_at"), 0), reverse=True)
    return rows


def _v250_paper_latest_rows(limit: int = V249_PAPER_LATEST_READ_MAX) -> List[Dict[str, Any]]:
    return [r for r in _v249_read_paper_latest_rows(limit) if _v250_is_vwap_row(r)]


def _v250_is_closed(row: Dict[str, Any]) -> bool:
    return str((row or {}).get("lane") or "strict") == "strict" and _v250_is_vwap_row(row)


def _v250_closed_rows(limit: int = 20000) -> List[Dict[str, Any]]:
    try:
        rows = load_closed(limit=limit)
        out = [r for r in rows if _v250_is_closed(r)]
        out.sort(key=lambda r: row_ts(r, "closed_at", "opened_at"), reverse=True)
        return out
    except Exception as exc:
        log_error("v250_closed_rows", exc)
        return []


def _v250_focus_reject_summary_lines(rows: List[Dict[str, Any]]) -> List[str]:
    return _v247_money_gate_summary_lines(rows)


def _v250_missed_rows(watch_rows: Optional[List[Dict[str, Any]]] = None, focus_rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    watch_rows = watch_rows if watch_rows is not None else _v250_watch_rows(3.0)
    by_ticker = {_v247_ticker(r): r for r in (focus_rows if focus_rows is not None else _v250_paper_latest_rows())}
    out: List[Dict[str, Any]] = []
    for r in watch_rows or []:
        hit = bool((r or {}).get("hit_plus_1_2")) or fnum((r or {}).get("max_return_pct"), 0) >= 1.2
        minus_first = str((r or {}).get("first_hit") or "") == "-0.7"
        if not hit or minus_first:
            continue
        t = _v247_ticker(r)
        src = by_ticker.get(t)
        ready = _v247_bool_ready(src) if src is not None else _v247_watch_original_ready(r)
        if ready:
            continue
        rr = dict(r)
        if src is not None:
            rr["source_reject_bucket"] = _v247_reject_bucket(src)
            rr["source_reject_detail"] = _v247_reject_reason_detail(src)
        out.append(rr)
    out.sort(key=lambda x: (fnum(x.get("max_return_pct"), 0), fnum(x.get("current_return_pct"), 0)), reverse=True)
    return out


def _v250_missed_lines(rows: Optional[List[Dict[str, Any]]] = None, focus_rows: Optional[List[Dict[str, Any]]] = None, limit: int = 5) -> List[str]:
    missed = _v250_missed_rows(rows, focus_rows)
    if not missed:
        return ["- 아직 없음"]
    out: List[str] = []
    for r in missed[:limit]:
        out.append(f"- {_v247_ticker(r)} / 최고 {_v242_pct(r.get('max_return_pct'))} / 현재 {_v242_pct(r.get('current_return_pct'))} / 5m {_v242_fmt_ret(r.get('return_5m_pct'))} / 탈락 {r.get('source_reject_bucket','-')}")
        out.append(f"  · 왜 빠졌나: {r.get('source_reject_detail','-')}")
    return out


def _v250_strategy_score_summary_lines() -> List[str]:
    rows = _v250_watch_rows(3.0)
    if not rows:
        return ["- 평균회귀 관찰이벤트 없음 / 자세히: /strategy_watch"]
    st = _v242_strategy_stats(rows)
    n = int(st.get("n", 0) or 0)
    hit = int(st.get("hit", 0) or 0)
    minus_first = int(st.get("minus_first", 0) or 0)
    sample = "판단가능" if n >= STRATEGY_WATCH_DECISION_MIN else "결론금지"
    return [
        f"- 평균회귀 관찰이벤트 {n}개 / +1.2도달 {hit}개 / -0.7먼저 {minus_first}개 / {sample}",
        f"- 평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / 자세히: /strategy_watch",
    ]


def _v250_score_text_direct_removed_from_handler() -> str:
    """v251: 구 /score 직접계산 본문. 기본 handler 연결 삭제, 캐시 생성용도 사용 안 함."""
    return _v251_wait_text("📊 모의매매 성과 /score", "/score", "구 직접계산 경로 제거됨")


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v250_watch_rows(3.0)
    grouped = _v242_group_watch_rows(rows)
    st = _v242_strategy_stats(rows)
    focus_rows = _v250_paper_latest_rows()
    paper_ready = [r for r in focus_rows if _v247_bool_ready(r)]
    missed = _v250_missed_rows(rows, focus_rows)
    lines = [
        "🧪 전략검증 /strategy_watch",
        "- 정식 paper OPEN 연결: 평균가 회귀 반등 단타 단일검증만 사용합니다.",
        "- 평균 조건: VWAP -0.3~-1.5% / 3분돈 700만+ / micro fresh / spread≤0.5% / 매수비≥0.52",
        "- 기존 돈흐름/눌림은 기본화면에서 제외했습니다. 필요하면 /strategy_watch_full",
        f"- 기준: 최근 3시간 / 평균회귀 관찰이벤트 {len(rows)}개 / 후보묶음 {len(grouped)}개",
        f"- paper_latest 평균회귀 후보: {len(focus_rows)}개 / trade_ready: {len(paper_ready)}개 / 놓친 후보 {len(missed)}개",
        _v242_watch_progress_line(rows),
        "",
        "[1/6] 평균가 회귀 반등 3시간",
    ]
    if rows:
        n = int(st.get("n", 0) or 0)
        hit = int(st.get("hit", 0) or 0)
        minus_first = int(st.get("minus_first", 0) or 0)
        icon = "✅" if n >= 20 and hit >= minus_first else ("⚠️" if n >= 20 else "❔")
        sample = "표본충분" if n >= STRATEGY_WATCH_DECISION_MIN else f"표본부족 {n}/{STRATEGY_WATCH_DECISION_MIN}"
        lines.append(
            f"{icon} 평균회귀: 후보 {n} / +1.2 {hit}({hit/max(n,1)*100:.0f}%) / 현재양수 {int(st.get('positive_now',0))}({int(st.get('positive_now',0))/max(n,1)*100:.0f}%) / "
            f"평균현재 {fnum(st.get('avg_cur'),0):+.2f}% / 평균최고 {fnum(st.get('avg_max'),0):+.2f}% / micro신선 {int(st.get('micro_fresh',0))}/{n} / WS신선 {int(st.get('ws_fresh',0))}/{n} / -0.7먼저 {minus_first} / {sample}"
        )
    else:
        lines.append("- 아직 기록 없음")
    lines += [
        "",
        "[2/6] paper_latest trade_ready 못 간 이유",
        *_v250_focus_reject_summary_lines(focus_rows),
        "",
        "[3/6] 신선도 부족 세분화",
        *_v248_fresh_breakdown_lines(focus_rows),
        "",
        "[4/6] 탈락했는데 오른 평균회귀 후보",
        *_v250_missed_lines(rows, focus_rows, 5),
        "",
        "[5/6] 단계별 예시",
        *_v248_stage_example_lines(focus_rows, 5),
        "",
        "[6/6] 눈에 띄는 평균회귀 후보",
        *_v243_watch_candidate_lines(rows, 6 if not full else 20),
        "",
        "판독",
        "- 지금은 평균적인 VWAP 회귀 조건으로 단일검증을 시작한 단계입니다.",
        "- 조건을 맞춘 후보가 적으면 먼저 trade_ready 탈락 이유를 봅니다.",
        "- +1.2 먼저 도달했고 -0.7 먼저가 아니었던 탈락 후보가 핵심 복기 대상입니다.",
    ]
    if full:
        all_rows = _v241_watch_rows(3.0)
        lines += ["", "[FULL] 현재버전 전체 전략검증 참고", *_v242_watch_full_lines(all_rows, 50)]
    return "\n".join(lines)


def _v250_quality_text() -> str:
    focus_rows = _v250_paper_latest_rows()
    paper_ready = [r for r in focus_rows if _v247_bool_ready(r)]
    closed = _v250_closed_rows()
    watch_rows = _v250_watch_rows(3.0)
    missed = _v250_missed_rows(watch_rows, focus_rows)
    snap = _v212_read_candidate_snapshot()
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 평균가 회귀 반등 단타 단일검증 전략 누적만 현재 판단에 사용합니다.",
        "- 평균 조건으로 시작: VWAP -0.3~-1.5%, 3분돈 700만+, spread≤0.5%, 매수비≥0.52.",
        "- 과거 돈흐름/눌림/구전략 장부는 보관하지만 현재 판단에서는 제외합니다.",
        f"- 참고 snapshot: {len((snap or {}).get('rows') or []) if isinstance(snap, dict) else 0}개 / {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())) if isinstance(snap, dict) else -1:.1f}초 전",
        "",
        "[1/6] 평균회귀 전략 성과",
        _compact_stat_line("평균가 회귀 반등 정식", closed),
        "",
        "[2/6] paper_latest 평균회귀 후보 흐름",
        f"- 평균회귀 후보 {len(focus_rows)}개 / trade_ready {len(paper_ready)}개 / 탈락 {max(0, len(focus_rows)-len(paper_ready))}개 / latest 기준",
        _simple_ws_line(focus_rows),
        _simple_micro_line(focus_rows),
        "",
        "[3/6] paper_latest trade_ready 못 간 이유",
        *_v250_focus_reject_summary_lines(focus_rows),
        "",
        "[4/6] 정보 신선도 부족 세분화",
        *_v248_fresh_breakdown_lines(focus_rows),
        "",
        "[5/6] 탈락했는데 오른 후보",
        *_v250_missed_lines(watch_rows, focus_rows, 5),
        *_v248_missed_bucket_lines(watch_rows, focus_rows),
        "",
        "[6/6] 단계별 예시 / 판독",
        "🧪 trade_ready 통과",
        *_v248_stage_example_lines(paper_ready, 4),
        "❔ trade_ready 탈락",
        *_v248_stage_example_lines([r for r in focus_rows if not _v247_bool_ready(r)], 5),
        "",
        "판독",
        "- 평균회귀는 VWAP 아래에서 버티고 되돌아오는 구간만 먹는 전략입니다.",
        "- TP분할은 나중에 시드가 커졌을 때 검토하고, 지금은 전량청산으로 봅니다.",
        "- 표본 50전 전까지 자동매매 판단 금지",
    ]
    return "\n".join(lines)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    try:
        txt = _v250_quality_text()
        save_json(FILES["quality_summary"], _v238_quality_payload(txt, snap=_v212_read_candidate_snapshot()))
        return txt
    except Exception as exc:
        log_error("v250_quality_text", exc)
        return _v248_quality_text_from_snapshot(_v212_read_candidate_snapshot())


def _build_quality_cache() -> None:  # type: ignore[override]
    try:
        save_json(FILES["quality_summary"], _v238_quality_payload(_v250_quality_text(), snap=_v212_read_candidate_snapshot()))
    except Exception as exc:
        log_error("v250_quality_cache", exc)



# ===============================
# v2.13.251: 평균회귀 배관 오류 수정 + 기본 명령어 캐시 전용화
# - /score, /quality 기본 handler에서 구 직접계산 경로를 제거한다. 캐시 없으면 갱신중만 표시한다.
# - 평균회귀 후보가 v244/v246 돈흐름 단일경로의 non-money observe 처리에 다시 눌리던 consume_row 원인을 우회가 아니라 원본 row 생성기로 재연결한다.
# - 평균회귀 trade_ready/탈락 문구를 평균회귀 기준으로 정리한다.
# - 전략 수치, 청산조건, 자동매수/BUY_READY, 장부는 변경하지 않는다.
# ===============================

SCORE_CACHE_STRATEGY_KEY = VWAP_REVERSION_MAIN_KEY
SCORE_CACHE_STRATEGY_LABEL = VWAP_REVERSION_MAIN_NAME
QUALITY_CACHE_STRATEGY_KEY = VWAP_REVERSION_MAIN_KEY

_V251_SCORE_BUILD_LOCK = threading.Lock()
_V251_QUALITY_BUILD_LOCK = threading.Lock()


def _v251_file_sig(path: Any) -> Dict[str, Any]:
    p = Path(path)
    try:
        st = p.stat() if p.exists() else None
        return {"mtime": float(st.st_mtime) if st else 0.0, "size": int(st.st_size) if st else 0}
    except Exception:
        return {"mtime": 0.0, "size": 0}


def _v251_score_dep_sig() -> Dict[str, Any]:
    closed = _v251_file_sig(FILES.get("paper_closed", BASE_DIR / "paper_closed.jsonl"))
    latest = _v251_file_sig(FILES.get("paper_latest", BASE_DIR / "paper_candidates_latest.jsonl"))
    watch = _v251_file_sig(FILES.get("strategy_watch_cache", BASE_DIR / "strategy_watch_cache.json"))
    return {
        "paper_closed_mtime": closed.get("mtime", 0.0),
        "paper_closed_size": closed.get("size", 0),
        "paper_latest_mtime": latest.get("mtime", 0.0),
        "paper_latest_size": latest.get("size", 0),
        "strategy_watch_mtime": watch.get("mtime", 0.0),
        "strategy_watch_size": watch.get("size", 0),
    }


def _v251_cache_valid(obj: Any, *, name: str, deps: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    if not isinstance(obj, dict) or not str(obj.get("text") or ""):
        return False, "캐시 없음"
    if str(obj.get("version") or "") != BOT_VERSION:
        return False, f"버전 불일치 {obj.get('version') or '-'}"
    if str(obj.get("strategy_key") or "") != SCORE_CACHE_STRATEGY_KEY:
        return False, f"전략 불일치 {obj.get('strategy_key') or '-'}"
    if str(obj.get("name") or "") != name:
        return False, f"캐시 종류 불일치 {obj.get('name') or '-'}"
    deps = deps or _v251_score_dep_sig()
    for k, v in deps.items():
        if k not in obj:
            return False, f"의존성 누락 {k}"
        ov = obj.get(k)
        if isinstance(v, float):
            if abs(fnum(ov, -999999) - fnum(v, -888888)) > 1e-6:
                return False, f"{k} 변경"
        else:
            if int(ov or 0) != int(v or 0):
                return False, f"{k} 변경"
    return True, ""


def _v251_wait_text(title: str, name: str, reason: str = "") -> str:
    lines = [
        title,
        f"❔ {name} 캐시 갱신중",
        f"- 기준: {BOT_VERSION} / {SCORE_CACHE_STRATEGY_LABEL}",
        "- 기본 명령어에서는 구 직접계산 경로를 타지 않습니다.",
        "- 백그라운드 요약 직원이 새 캐시를 저장하면 자동으로 바뀝니다.",
    ]
    if reason:
        lines.append(f"- 사유: {reason}")
    return "\n".join(lines)


def _v251_focus_gate_summary_lines(rows: List[Dict[str, Any]]) -> List[str]:
    if not rows:
        return ["- 평균회귀 후보 없음"]
    c, ex = _v247_money_reject_summary(rows)
    total = len(rows)
    ready = sum(1 for r in rows if _v247_bool_ready(r))
    lines = [f"- 평균회귀 후보 {total}개 → trade_ready {ready}개 / 탈락 {max(0, total-ready)}개"]
    if not c:
        lines.append("- 탈락 없음")
        return lines
    for k, n in c.most_common(6):
        examples = ", ".join(_v247_ticker(r) for r in ex.get(k, [])[:3]) or "-"
        lines.append(f"- {k}: {n}개 / 예시 {examples}")
    return lines


def _v250_focus_reject_summary_lines(rows: List[Dict[str, Any]]) -> List[str]:  # type: ignore[override]
    return _v251_focus_gate_summary_lines(rows)


def _v251_score_cache_payload(text: str, name: str) -> Dict[str, Any]:
    payload = _cache_payload(text, name)
    payload.update(_v251_score_dep_sig())
    payload["strategy_key"] = SCORE_CACHE_STRATEGY_KEY
    payload["strategy_label"] = SCORE_CACHE_STRATEGY_LABEL
    payload["cache_mode"] = "v251_cache_only_basic_command"
    return payload


def _v251_build_score_text_for_cache() -> str:
    # 백그라운드 전용. handler에서 직접 호출하지 않는다.
    closed = _v250_closed_rows(limit=7000)
    watch_rows = _v250_watch_rows(3.0)
    focus_rows = _v250_paper_latest_rows()
    missed = _v250_missed_rows(watch_rows, focus_rows)
    lines = [
        "📊 모의매매 성과 /score",
        "- 기준: 평균가 회귀 반등 단타 단일검증 전략 누적만 표시합니다.",
        "- 과거 돈흐름/눌림/구전략 장부는 보관하지만 현재 판단에서는 제외합니다.",
        "- TP분할 없음: 소액 실전 전제라 전량익절/전량손절 기준으로 봅니다.",
        "- v251: 이 화면은 캐시 전용입니다. 구 직접계산 경로는 기본 handler에서 제거했습니다.",
        "",
        "[1/4] 평균회귀 전략 성과",
        _compact_stat_line("평균가 회귀 반등 정식", closed),
        "",
        "[2/4] 종료 사유",
        *_v248_exit_reason_lines(closed, 6),
        "",
        "[3/4] 평균회귀 관찰 참고",
        *_v250_strategy_score_summary_lines(),
        "",
        "[4/4] 놓친 후보 요약",
        f"- 탈락 후 +1.2 먼저 도달: {len(missed)}개",
        *_v248_missed_bucket_lines(watch_rows, focus_rows),
        "",
        "판독",
        "- /score는 평균가 회귀 반등 단타 전략 누적을 본다.",
        "- 표본 50전 전까지 자동매매 판단 금지",
        "- 후보수는 임시로 넓히지 않고, 좋은 후보를 죽인 탈락조건만 찾는다.",
    ]
    return "\n".join(lines)


def _v251_build_score_cache(reason: str = "background") -> None:
    with _V251_SCORE_BUILD_LOCK:
        try:
            txt = _v251_build_score_text_for_cache()
            payload = _v251_score_cache_payload(txt, "score")
            payload["build_reason"] = reason
            save_json(FILES["score_summary"], payload)
            with _state_lock:
                STATE["v251_score_cache_updated_ts"] = now_ts()
                STATE["v251_score_cache_reason"] = reason
        except Exception as exc:
            log_error("v251_build_score_cache", exc)


def _v251_quality_cache_payload(text: str) -> Dict[str, Any]:
    payload = _v251_score_cache_payload(text, "quality")
    return payload


def _v251_build_quality_cache(reason: str = "background") -> None:
    with _V251_QUALITY_BUILD_LOCK:
        try:
            txt = _v250_quality_text()
            payload = _v251_quality_cache_payload(txt)
            payload["build_reason"] = reason
            save_json(FILES["quality_summary"], payload)
            with _state_lock:
                STATE["v251_quality_cache_updated_ts"] = now_ts()
                STATE["v251_quality_cache_reason"] = reason
        except Exception as exc:
            log_error("v251_build_quality_cache", exc)


_v251_prev_build_light_command_caches = _build_light_command_caches

def _build_light_command_caches() -> None:  # type: ignore[override]
    try:
        _v251_prev_build_light_command_caches()
    except Exception as exc:
        log_error("v251_prev_light_cache", exc)
    _v251_build_score_cache("light_command_worker")
    _v251_build_quality_cache("light_command_worker")


def _build_quality_cache() -> None:  # type: ignore[override]
    _v251_build_quality_cache("quality_worker")


def score_text() -> str:  # type: ignore[override]
    obj = load_json(FILES["score_summary"], {})
    ok, why = _v251_cache_valid(obj, name="score")
    if ok:
        return str(obj.get("text") or "")
    return _v251_wait_text("📊 모의매매 성과 /score", "/score", why)


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v209_direct_candidate_quality_text(True)
    obj = load_json(FILES["quality_summary"], {})
    ok, why = _v251_cache_valid(obj, name="quality")
    if ok:
        return str(obj.get("text") or "")
    return _v251_wait_text("🔬 후보품질 요약 /quality", "/quality", why)



# ===============================
# v2.13.252: 평균회귀 전용 공장 수술 + 구전략 찌꺼기 삭제
# - v251 병목 원인: 평균회귀 본선인데도 v244 이전 구 후보생성기를 먼저 돌린 뒤 평균회귀로 재필터링했다.
# - v252는 기본 스캔 공장을 평균회귀 전용으로 교체한다. 구 눌림/돈흐름/급등/저점반등 후보생성은 기본 스캔에서 호출하지 않는다.
# - /score, /quality는 v251 캐시 전용 유지. 캐시 없으면 갱신중만 표시한다.
# - 평균회귀 관찰 이벤트는 paper_latest뿐 아니라 이번 스캔 후보/근접탈락도 기록한다.
# - 전략 수치, 청산조건, 자동매수/BUY_READY, paper 장부는 변경하지 않는다.
# ===============================

VWAP_REV_SHADOW_MAX = int(os.getenv("CLEAN_VWAP_REV_SHADOW_MAX", "16"))
VWAP_REV_SCAN_POOL_LIMIT = int(os.getenv("CLEAN_VWAP_REV_SCAN_POOL_LIMIT", "260"))


def _v252_row_sort_key(row: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (
        fnum(row.get("score"), 0),
        _v250_money3(row),
        fnum(row.get("turnover_24h"), 0),
        -abs(_v250_vwap_gap(row)),
    )


def _v252_candidate_pool(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """평균회귀 전용 1차 풀. 구전략 build_candidates를 호출하지 않는다."""
    seen: set[str] = set()
    pool: List[Dict[str, Any]] = []
    for row in sorted([r for r in (rows or []) if isinstance(r, dict)], key=_v252_row_sort_key, reverse=True):
        t = _v241_watch_ticker(row)
        if not t or t in seen:
            continue
        seen.add(t)
        # 거래대금/VWAP 둘 다 완전히 동떨어진 종목은 여기서 바로 제외해 공장 CPU를 줄인다.
        m3 = _v250_money3(row)
        vgap = _v250_vwap_gap(row)
        if m3 < max(1_500_000, VWAP_REV_MIN_3M_KRW * 0.35):
            continue
        if not (VWAP_REV_MIN_GAP - 0.8 <= vgap <= VWAP_REV_MAX_GAP + 0.45):
            continue
        pool.append(row)
        if len(pool) >= VWAP_REV_SCAN_POOL_LIMIT:
            break
    return pool


def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    """v252 평균회귀 전용 공장.

    이전: 구 눌림/돈흐름 후보생성기 실행 → strict/shadow 생성 → 평균회귀 재필터링.
    현재: 표준 row → 평균회귀 조건 직접 점수화 → strict/near-miss만 생성.
    """
    pool = _v252_candidate_pool(rows)
    scored: List[Tuple[float, Dict[str, Any], List[str], List[str]]] = []
    near: List[Tuple[float, Dict[str, Any], List[str], List[str]]] = []
    rejects: Counter = Counter()
    examples: List[Dict[str, Any]] = []

    for item in pool:
        s, reasons, blocks = _v250_score_vwap_reversion(item)
        if s >= VWAP_REV_MIN_SCORE and not blocks:
            scored.append((s, item, reasons, blocks))
        else:
            bucket = blocks[0] if blocks else f"평균회귀 점수 {s:.2f} < {VWAP_REV_MIN_SCORE:.2f}"
            rejects.update([bucket])
            if s >= max(1.9, VWAP_REV_MIN_SCORE - 1.0) or _v250_money3(item) >= VWAP_REV_MIN_3M_KRW:
                near.append((s, item, reasons, blocks or [bucket]))
                if len(examples) < 8:
                    examples.append({
                        "ticker": _v241_watch_ticker(item),
                        "reason": bucket,
                        "score": round(s, 2),
                        "line": " / ".join((reasons or blocks or [bucket])[:4]),
                    })

    scored.sort(key=lambda x: (x[0], _v250_money3(x[1]), -abs(_v250_vwap_gap(x[1])), fnum(x[1].get("change_1"), 0)), reverse=True)
    near.sort(key=lambda x: (x[0], _v250_money3(x[1]), -abs(_v250_vwap_gap(x[1]))), reverse=True)

    strict = [_v250_stamp_item(item, s, reasons, blocks) for s, item, reasons, blocks in scored[:max(1, VWAP_REV_STRICT_MAX)]]
    strict_tickers = {_v241_watch_ticker(x) for x in strict}
    shadow: List[Dict[str, Any]] = []
    for s, item, reasons, blocks in near:
        t = _v241_watch_ticker(item)
        if not t or t in strict_tickers:
            continue
        out = _v250_stamp_item(item, s, reasons, blocks)
        out["review_only"] = True
        out["paper_bot_open"] = False
        out["open_eligible"] = False
        out["trade_ready"] = False
        out["observe_only"] = True
        out["final_entry_action"] = "observe"
        out["final_entry_label"] = "평균회귀 근접탈락 관찰"
        out["quality_label"] = "평균회귀 근접탈락"
        out["mainline_disabled_reason"] = "v252 평균회귀 근접탈락 복기/관찰"
        shadow.append(out)
        if len(shadow) >= VWAP_REV_SHADOW_MAX:
            break

    with _state_lock:
        STATE["v252_main_strategy"] = VWAP_REVERSION_MAIN_NAME
        STATE["v252_pool"] = len(pool)
        STATE["v252_vwap_strict"] = len(strict)
        STATE["v252_vwap_shadow"] = len(shadow)
        STATE["v252_pruned_old_factory"] = True
        STATE["v252_note"] = "구 전략 후보생성기 미호출: 평균회귀 전용 공장"
    return strict, shadow, rejects, examples


# strategy_watch: 이번 스캔 평균회귀 strict/near-miss를 직접 기록한다.
def _v241_select_watch_candidates(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    scan_id = str(STATE.get("scan_id") or f"scan-{int(now_ts())}")
    pool: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(strict or []) + list(shadow or []):
        t = _v241_watch_ticker(item)
        if not t or t in seen:
            continue
        seen.add(t)
        pool.append(item)
    # paper_latest가 아직 바뀌기 전이어도 현재 rows에서 후보권을 추적한다.
    if len(pool) < STRATEGY_WATCH_TOP_PER_KIND:
        for item in _v252_candidate_pool(rows):
            t = _v241_watch_ticker(item)
            if not t or t in seen:
                continue
            s, reasons, blocks = _v250_score_vwap_reversion(item)
            if s >= max(1.9, VWAP_REV_MIN_SCORE - 1.0):
                pool.append(_v250_stamp_item(item, s, reasons, blocks))
                seen.add(t)
            if len(pool) >= max(STRATEGY_WATCH_TOP_PER_KIND * 3, 12):
                break

    events: List[Tuple[float, Dict[str, Any]]] = []
    for item in pool:
        s, reasons, blocks = _v250_score_vwap_reversion(item)
        if s < max(1.8, VWAP_REV_MIN_SCORE - 1.1):
            continue
        ev = _v241_watch_event(
            VWAP_REVERSION_WATCH_KIND,
            _v250_stamp_item(item, s, reasons, blocks),
            s,
            reasons if not blocks else reasons + ["관찰: " + str(blocks[0])],
            scan_id,
        )
        ev["source_reject_bucket"] = _v247_reject_bucket(item)
        ev["source_reject_detail"] = _v247_reject_reason_detail(item)
        ev["v252_factory"] = "vwap_only"
        events.append((s, ev))
    events.sort(key=lambda x: (x[0], fnum(x[1].get("money_flow_3m"), 0), fnum(x[1].get("current_return_pct"), 0)), reverse=True)
    return [ev for _s, ev in events[:STRATEGY_WATCH_MAX_PER_SCAN]]


# v252: 기본 캐시 작업에서 구 health/quality/light cache 전체를 다시 돌리지 않는다.
def _build_light_command_caches() -> None:  # type: ignore[override]
    _v251_build_score_cache("v252_light_worker_cache_only")
    _v251_build_quality_cache("v252_light_worker_cache_only")


# v252: version_score도 stale이면 기다리되, 구 직접계산으로 돌아가지 않게 유지한다.
def version_score_text() -> str:  # type: ignore[override]
    obj = load_json(FILES["version_score_summary"], {})
    if isinstance(obj, dict) and str(obj.get("version") or "") == BOT_VERSION and str(obj.get("text") or ""):
        return str(obj.get("text") or "")
    return "\n".join([
        "📊 현재버전 성과 /version_score",
        "❔ /version_score 캐시 갱신중",
        f"- 현재 실행버전: {BOT_VERSION}",
        f"- 기존 캐시버전: {obj.get('version','-') if isinstance(obj, dict) else '-'}",
        "- 기본 명령어에서는 구 직접계산 경로를 타지 않습니다.",
        "- 요약 직원이 새 캐시를 저장하면 자동으로 바뀝니다.",
    ])


def _v252_prune_map_text() -> str:
    return "\n".join([
        "# 코인봇 v2.13.252 가지치기 지도",
        "",
        "## 목적",
        "- 평균가 회귀 반등 단타 본선만 기본 스캔에서 사용한다.",
        "- 구 눌림/돈흐름/급등/저점반등 후보생성기는 기본 스캔에서 호출하지 않는다.",
        "- /score, /quality, /version_score는 기본 명령어에서 직접계산으로 돌아가지 않는다.",
        "",
        "## 삭제/제거",
        "- v241 다전략 watch scoring 함수 묶음 삭제: low_rebound_early / surge_start / money_reaccel / pullback selector.",
        "- v252 build_candidates는 구 v244 이전 후보생성기를 호출하지 않고, 표준 row에서 평균회귀 후보만 직접 생성.",
        "- v251 light cache에서 이전 light cache 전체 호출 제거. score/quality 캐시만 생성.",
        "",
        "## 유지",
        "- paper 장부 OPEN/CLOSED/trade_log 삭제 없음.",
        "- 자동매수 ON 없음, BUY_READY 강제 없음.",
        "- 평균회귀 조건 수치 변경 없음: VWAP -0.3~-1.5%, 3분돈 700만+, spread≤0.5%, 매수비≥0.52.",
        "- 페이퍼봇 v0.54 유지.",
        "",
        "## 다음 확인",
        "- /health 공장 시간이 13초대에서 내려가는지.",
        "- /strategy_watch 평균회귀 관찰이벤트가 0이 아니라 쌓이는지.",
        "- paper OPEN/CLOSED가 평균회귀 route(vwap_reversion_main)로 찍히는지.",
    ])


# ===============================
# v2.13.253: 평균회귀 관찰이벤트 연결 + paper_latest/strategy_watch 기준 정렬
# - v252에서 평균회귀 전용 공장은 정상화됐지만, v245 money_reaccel 전용 strategy_watch_update가 남아 있어
#   평균회귀 후보가 실제 OPEN/CLOSED 되어도 관찰이벤트가 0개로 남았다.
# - 기본 스캔의 관찰기록 update를 평균회귀 전용 selector로 다시 연결한다.
# - paper_latest에 이미 찍힌 평균회귀 후보도 watch cache에 보강해, 5m/10m/20m 복기가 끊기지 않게 한다.
# - 진입조건/청산조건/자동매수/BUY_READY/paper 장부 삭제 없음.
# ===============================


def _v253_strategy_watch_paper_latest_events(scan_id: str, existing: Optional[set] = None) -> List[Dict[str, Any]]:
    existing = existing or set()
    events: List[Tuple[float, Dict[str, Any]]] = []
    try:
        for item in _v250_paper_latest_rows(limit=80):
            t = _v241_watch_ticker(item)
            if not t or t in existing:
                continue
            s = fnum(item.get("vwap_reversion_score") or item.get("score"), 0.0)
            reasons = list(item.get("vwap_reversion_reasons") or [])
            blocks = list(item.get("vwap_reversion_blocks") or [])
            if not reasons and str(item.get("one_liner") or ""):
                reasons = [str(item.get("one_liner"))[:80]]
            if s <= 0:
                s, reasons2, blocks2 = _v250_score_vwap_reversion(item)
                reasons = reasons or reasons2
                blocks = blocks or blocks2
            # paper_latest에 온 평균회귀 후보는 실제 paper 소비대상이라 점수 낮음만으로 버리지 않는다.
            ev = _v241_watch_event(
                VWAP_REVERSION_WATCH_KIND,
                _v250_stamp_item(item, s, reasons, blocks),
                s,
                reasons if not blocks else reasons + ["관찰: " + str(blocks[0])],
                scan_id,
            )
            ev["source_reject_bucket"] = _v247_reject_bucket(item)
            ev["source_reject_detail"] = _v247_reject_reason_detail(item)
            ev["v253_source"] = "paper_latest"
            events.append((s, ev))
    except Exception as exc:
        log_error("v253_paper_latest_watch_events", exc)
    events.sort(key=lambda x: (x[0], fnum(x[1].get("money_flow_3m"), 0), fnum(x[1].get("current_return_pct"), 0)), reverse=True)
    return [ev for _s, ev in events[:max(8, STRATEGY_WATCH_MAX_PER_SCAN)]]


def _v253_select_watch_candidates(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """평균회귀 전용 관찰 이벤트 selector.

    v252의 _v241_select_watch_candidates는 맞았지만, update 함수가 v245 money selector를 직접 호출해
    실제로는 쓰이지 않았다. v253 update가 이 selector를 직접 호출한다.
    """
    scan_id = str(STATE.get("scan_id") or f"scan-{int(now_ts())}")
    pool: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(strict or []) + list(shadow or []):
        t = _v241_watch_ticker(item)
        if not t or t in seen:
            continue
        seen.add(t)
        pool.append(item)
    if len(pool) < STRATEGY_WATCH_TOP_PER_KIND:
        for item in _v252_candidate_pool(rows):
            t = _v241_watch_ticker(item)
            if not t or t in seen:
                continue
            s, reasons, blocks = _v250_score_vwap_reversion(item)
            if s >= max(1.9, VWAP_REV_MIN_SCORE - 1.0):
                stamped = _v250_stamp_item(item, s, reasons, blocks)
                stamped.setdefault("review_only", True)
                pool.append(stamped)
                seen.add(t)
            if len(pool) >= max(STRATEGY_WATCH_TOP_PER_KIND * 3, 12):
                break

    events: List[Tuple[float, Dict[str, Any]]] = []
    for item in pool:
        t = _v241_watch_ticker(item)
        if not t:
            continue
        s = fnum(item.get("vwap_reversion_score"), 0.0)
        reasons = list(item.get("vwap_reversion_reasons") or [])
        blocks = list(item.get("vwap_reversion_blocks") or [])
        if s <= 0:
            s, reasons2, blocks2 = _v250_score_vwap_reversion(item)
            reasons = reasons or reasons2
            blocks = blocks or blocks2
        if s < max(1.8, VWAP_REV_MIN_SCORE - 1.1):
            continue
        ev = _v241_watch_event(
            VWAP_REVERSION_WATCH_KIND,
            _v250_stamp_item(item, s, reasons, blocks),
            s,
            reasons if not blocks else reasons + ["관찰: " + str(blocks[0])],
            scan_id,
        )
        ev["source_reject_bucket"] = _v247_reject_bucket(item)
        ev["source_reject_detail"] = _v247_reject_reason_detail(item)
        ev["v253_source"] = "scan_factory"
        events.append((s, ev))

    # paper_latest 보강: update 타이밍/스캔 throttle 때문에 strict/shadow 이벤트가 빠져도 실제 소비 후보는 추적한다.
    paper_events = _v253_strategy_watch_paper_latest_events(scan_id, {_v241_watch_ticker(ev) for _s, ev in events})
    for ev in paper_events:
        events.append((fnum(ev.get("score"), 0.0), ev))

    events.sort(key=lambda x: (x[0], fnum(x[1].get("money_flow_3m"), 0), fnum(x[1].get("current_return_pct"), 0)), reverse=True)
    return [ev for _s, ev in events[:max(STRATEGY_WATCH_MAX_PER_SCAN, 12)]]


def _v241_strategy_watch_update(rows: List[Dict[str, Any]], strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> None:  # type: ignore[override]
    """v253: v245 돈흐름 전용 update를 평균회귀 전용 update로 교체."""
    if not STRATEGY_WATCH_ON:
        return
    nowv = now_ts()
    try:
        # 너무 자주 파일을 만지지는 않되, v252처럼 0개 고착되지 않도록 요약은 갱신한다.
        if nowv - fnum(_V243_SW_RUNTIME.get("last_update_ts"), 0.0) < STRATEGY_WATCH_UPDATE_GAP_SEC:
            with _state_lock:
                STATE["strategy_watch_note"] = "v253 평균회귀 관찰 throttle"
            try:
                if nowv - fnum(_V243_SW_RUNTIME.get("last_summary_ts"), 0.0) >= STRATEGY_WATCH_SUMMARY_GAP_SEC:
                    _v243_build_strategy_watch_summary_cache()
            except Exception:
                pass
            return
        cache = _v241_watch_load()
        _v241_update_existing_watch(cache, rows)
        events = _v253_select_watch_candidates(rows, strict, shadow)
        added = _v241_add_new_watch(cache, events)
        _v241_watch_save(cache)
        _V243_SW_RUNTIME["last_update_ts"] = nowv
        if nowv - fnum(_V243_SW_RUNTIME.get("last_compact_ts"), 0.0) >= STRATEGY_WATCH_COMPACT_GAP_SEC:
            try:
                compact_candidate_file(FILES["strategy_watch_events"], keep_lines=STRATEGY_WATCH_EVENT_KEEP_LINES)
            except Exception:
                pass
            _V243_SW_RUNTIME["last_compact_ts"] = nowv
        try:
            # v250 strategy_watch_text는 직접 watch cache를 읽으므로, 요약 캐시도 같이 최신화한다.
            _v243_build_strategy_watch_summary_cache(cache)
        except Exception as exc:
            log_error("v253_strategy_watch_summary_build", exc)
        with _state_lock:
            STATE["strategy_watch_active"] = len(cache.get("active") or {})
            STATE["strategy_watch_added"] = added
            STATE["strategy_watch_selected"] = len(events)
            STATE["strategy_watch_note"] = "v253 평균회귀 관찰이벤트 연결"
    except Exception as exc:
        log_error("v253_strategy_watch_update", exc)


def _v253_prune_map_text() -> str:
    return "\n".join([
        "# 코인봇 v2.13.253 가지치기/연결 지도",
        "",
        "## 이번 수정",
        "- v245 돈흐름 전용 strategy_watch_update가 남아 평균회귀 관찰이벤트가 0개로 고착되던 경로를 교체.",
        "- 평균회귀 strict/shadow 및 paper_latest 후보를 strategy_watch_cache에 기록해 5m/10m/20m 복기를 살림.",
        "- 기본 스캔의 평균회귀 전용 공장(v252)은 유지.",
        "",
        "## 삭제/차단 유지",
        "- 기본 스캔에서 구 눌림/돈흐름/급등/저점반등 후보공장 미호출 유지.",
        "- /score, /quality, /version_score 기본 직접계산 fallback 차단 유지.",
        "",
        "## 건드리지 않음",
        "- 평균회귀 조건 수치 변경 없음.",
        "- 자동매수 ON 없음, BUY_READY 강제 없음.",
        "- paper OPEN/CLOSED/trade_log 삭제 없음.",
        "- TP분할 없음.",
        "",
        "## 다음 확인",
        "- /strategy_watch 평균회귀 관찰이벤트가 0개에서 증가하는지.",
        "- /score의 평균회귀 관찰 참고가 이벤트 수를 표시하는지.",
        "- /health 공장 시간이 3~5초대 근처로 유지되는지.",
    ])


# ===============================
# v2.13.254/v2.13.256: 평균회귀 과거 CLOSED 원인분석 연결
# - 조건을 바로 바꾸기 전에, 지금 이후 기록뿐 아니라 이전 평균회귀 CLOSED까지 원인분석에 포함한다.
# - 기본 명령어는 v251 캐시 전용 유지. 원인분석은 백그라운드 score/quality 캐시 생성 때만 계산한다.
# - 전략 조건/청산조건/자동매수/BUY_READY/paper 장부는 변경하지 않는다.
# ===============================

VWAP_REVIEW_MIN_SAMPLE = 20


def _v254_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    ctx = (row or {}).get("entry_context")
    return ctx if isinstance(ctx, dict) else {}


def _v254_get(row: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    ctx = _v254_ctx(row)
    raw = (row or {}).get("raw") if isinstance((row or {}).get("raw"), dict) else {}
    for k in keys:
        if k in (row or {}) and (row or {}).get(k) not in (None, ""):
            return (row or {}).get(k)
        if k in ctx and ctx.get(k) not in (None, ""):
            return ctx.get(k)
        if k in raw and raw.get(k) not in (None, ""):
            return raw.get(k)
    return default


def _v254_pnl(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "pnl_pct", "net_pct", "return_pct", default=0), 0)


def _v254_peak(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "peak_pct", "highest_return_pct", "max_return_pct", "early_peak_pct", default=0), 0)


def _v254_trough(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "trough_pct", "min_return_pct", default=0), 0)


def _v254_exit_key(row: Dict[str, Any]) -> str:
    return str(_v254_get(row, "exit_rule", "exit_reason", "exit_mode", default="-") or "-")


def _v254_exit_label(row: Dict[str, Any]) -> str:
    lab = str(_v254_get(row, "exit_rule_label", "exit_reason_label", default="") or "")
    if lab:
        return lab
    key = _v254_exit_key(row)
    try:
        return str(_exit_reason_kr(key))  # type: ignore[name-defined]
    except Exception:
        try:
            return str(exit_reason_kr(key))
        except Exception:
            return key


def _v254_hold_min(row: Dict[str, Any]) -> float:
    sec = fnum(_v254_get(row, "hold_sec", default=None), -1)
    if sec >= 0:
        return sec / 60.0
    return fnum(_v254_get(row, "age_min", "hold_min", default=0), 0)


def _v254_vwap_gap(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "vwap_gap_pct", "vwap_gap", "avg_price_gap_pct", default=0), 0)


def _v254_money3(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "money_flow_3m", "turnover_3m", default=0), 0)


def _v254_buy_ratio(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "micro_trade_buy_ratio_30", "buy_ratio", "trade_buy_ratio", default=0), 0)


def _v254_spread(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "micro_spread_pct", "orderbook_spread_pct", "spread_pct", default=999), 999)


def _v254_from_low(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "from_30m_low_pct", "from_low_pct", default=0), 0)


def _v254_below_high(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "below_30m_high_pct", "below_high_pct", default=999), 999)


def _v254_change1(row: Dict[str, Any]) -> float:
    return fnum(_v254_get(row, "change_1", "change_1m", default=0), 0)


def _v254_avg(rows: List[Dict[str, Any]], fn, default: float = 0.0) -> float:
    vals = []
    for r in rows or []:
        try:
            v = float(fn(r))
            if v == v and abs(v) < 1e18:
                vals.append(v)
        except Exception:
            pass
    return sum(vals) / len(vals) if vals else default


def _v254_stat(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    rows = list(rows or [])
    n = len(rows)
    wins = [r for r in rows if _v254_pnl(r) > 0]
    total = sum(_v254_pnl(r) for r in rows)
    return {
        "n": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": (len(wins) / n * 100.0) if n else 0.0,
        "total": total,
        "avg": (total / n) if n else 0.0,
        "avg_peak": _v254_avg(rows, _v254_peak),
        "avg_pnl": (total / n) if n else 0.0,
    }


def _v254_closed_all_focus(limit: int = 30000) -> List[Dict[str, Any]]:
    return _v250_closed_rows(limit=limit)


def _v254_loss_rows(rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    rows = list(rows if rows is not None else _v254_closed_all_focus())
    return [r for r in rows if _v254_pnl(r) <= 0]


def _v254_cause_tags(row: Dict[str, Any]) -> List[str]:
    tags: List[str] = []
    pnl = _v254_pnl(row)
    peak = _v254_peak(row)
    exit_key = _v254_exit_key(row)
    spread = _v254_spread(row)
    buy = _v254_buy_ratio(row)
    gap = _v254_vwap_gap(row)
    m3 = _v254_money3(row)
    from_low = _v254_from_low(row)
    below_high = _v254_below_high(row)
    ch1 = _v254_change1(row)
    hold = _v254_hold_min(row)

    if "hard" in exit_key or pnl <= -0.95:
        tags.append("하드가드급 큰손실")
    if peak <= 0.15 and pnl <= 0:
        tags.append("진입 후 반응없음")
    if peak >= 0.60 and pnl <= 0.30:
        tags.append("수익권 후 되밀림")
    if spread > 0.50 and spread < 900:
        tags.append("스프레드 넓음")
    if buy > 0 and buy < 0.52:
        tags.append("매수비 약함")
    if gap < -1.20:
        tags.append("VWAP 이격 깊음")
    elif gap > -0.30:
        tags.append("VWAP 이격 얕음")
    if m3 > 0 and m3 < VWAP_REV_MIN_3M_KRW:
        tags.append("3분돈 부족")
    if from_low > 2.4:
        tags.append("저점대비 이미 반등")
    if below_high < 0.35:
        tags.append("고점근접 추격")
    if ch1 < -0.05:
        tags.append("1분 흐름 음수")
    if hold >= 12 and peak <= 0.40:
        tags.append("장시간 무반응")
    if not tags:
        # 종료규칙을 마지막 보조 태그로 남겨 과거 row도 빈칸 없이 묶는다.
        lab = _v254_exit_label(row)
        tags.append(f"종료:{lab}")
    return list(dict.fromkeys(tags))[:6]


def _v254_by_exit_lines(rows: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    rows = list(rows or [])
    if not rows:
        return ["- 평균회귀 CLOSED 없음"]
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        buckets.setdefault(_v254_exit_label(r), []).append(r)
    items = sorted(buckets.items(), key=lambda kv: (len(kv[1]), abs(sum(_v254_pnl(x) for x in kv[1]))), reverse=True)[:limit]
    out: List[str] = []
    for label, arr in items:
        st = _v254_stat(arr)
        icon = "✅" if st["avg"] > 0 else ("❌" if st["n"] >= 10 and st["avg"] < 0 else "⚠️")
        out.append(f"{icon} {label}: {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 평균최고 {st['avg_peak']:+.2f}%")
    return out


def _v254_cause_bucket_lines(rows: List[Dict[str, Any]], limit: int = 8) -> List[str]:
    loss = _v254_loss_rows(rows)
    if not loss:
        return ["- 손실 CLOSED 없음"]
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for r in loss:
        for tag in _v254_cause_tags(r):
            buckets.setdefault(tag, []).append(r)
    items = sorted(buckets.items(), key=lambda kv: (len(kv[1]), -_v254_stat(kv[1])["avg"]), reverse=True)[:limit]
    out: List[str] = []
    for tag, arr in items:
        st = _v254_stat(arr)
        examples = ", ".join(str(_v247_ticker(x)) for x in arr[:3]) or "-"
        out.append(f"- {tag}: {st['n']}건 / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 평균최고 {st['avg_peak']:+.2f}% / 예시 {examples}")
    return out


def _v254_entry_feature_lines(rows: List[Dict[str, Any]]) -> List[str]:
    rows = list(rows or [])
    if not rows:
        return ["- 비교할 CLOSED 없음"]
    wins = [r for r in rows if _v254_pnl(r) > 0]
    losses = [r for r in rows if _v254_pnl(r) <= 0]
    def part(label: str, arr: List[Dict[str, Any]]) -> str:
        return (
            f"- {label}: {len(arr)}건 / "
            f"VWAP이격 {_v254_avg(arr, _v254_vwap_gap):+.2f}% / "
            f"3분돈 {krw_m(_v254_avg(arr, _v254_money3))} / "
            f"매수비 {_v254_avg(arr, _v254_buy_ratio):.2f} / "
            f"스프레드 {_v254_avg(arr, _v254_spread):.2f}% / "
            f"저점대비 {_v254_avg(arr, _v254_from_low):.2f}% / "
            f"평균최고 {_v254_avg(arr, _v254_peak):+.2f}%"
        )
    return [part("승리군", wins), part("손실군", losses)]


def _v254_hard_loss_detail_lines(rows: List[Dict[str, Any]], limit: int = 5) -> List[str]:
    hard = [r for r in rows or [] if ("hard" in _v254_exit_key(r) or _v254_pnl(r) <= -0.95)]
    hard.sort(key=lambda r: _v254_pnl(r))
    if not hard:
        return ["- 하드가드급 손실 없음"]
    out = []
    for r in hard[:limit]:
        out.append(
            f"- {_v247_ticker(r)}: 손익 {_v242_pct(_v254_pnl(r))} / 최고 {_v242_pct(_v254_peak(r))} / "
            f"VWAP {_v254_vwap_gap(r):+.2f}% / 매수비 {_v254_buy_ratio(r):.2f} / 스프레드 {_v254_spread(r):.2f}% / "
            f"보유 {_v254_hold_min(r):.1f}분 / {_v254_exit_label(r)}"
        )
    return out


def _v254_historical_review_lines(rows: Optional[List[Dict[str, Any]]] = None, *, compact: bool = False) -> List[str]:
    rows = list(rows if rows is not None else _v254_closed_all_focus())
    st = _v254_stat(rows)
    lines: List[str] = [
        "[원인분석] 평균회귀 과거 CLOSED 포함",
        f"- 대상: 평균회귀 CLOSED {st['n']}건 / {st['wins']}승 {st['losses']}패 / 승률 {st['win_rate']:.1f}% / 합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}%",
        "- 목적: 지금 이후 기록만 보지 않고, 기존 평균회귀 장부까지 포함해 조건 수정 전 원인을 먼저 봅니다.",
        "",
        "종료사유별",
        *_v254_by_exit_lines(rows, 6 if compact else 10),
        "",
        "손실 원인 태그",
        *_v254_cause_bucket_lines(rows, 6 if compact else 10),
        "",
        "승리군 vs 손실군 진입값",
        *_v254_entry_feature_lines(rows),
    ]
    if not compact:
        lines += ["", "하드가드급 손실 예시", *_v254_hard_loss_detail_lines(rows, 8)]
    lines += [
        "",
        "판독",
        "- 이 블록은 조건을 바로 바꾸기 위한 게 아니라, 어떤 손실군이 반복되는지 확인하는 지도입니다.",
        "- 반복 원인이 3~5건 이상 누적된 항목만 다음 수정 후보로 봅니다.",
    ]
    return lines


_v254_prev_score_builder = _v251_build_score_text_for_cache

def _v251_build_score_text_for_cache() -> str:  # type: ignore[override]
    closed = _v250_closed_rows(limit=12000)
    watch_rows = _v250_watch_rows(3.0)
    focus_rows = _v250_paper_latest_rows()
    missed = _v250_missed_rows(watch_rows, focus_rows)
    lines = [
        "📊 모의매매 성과 /score",
        "- 기준: 평균가 회귀 반등 단타 단일검증 전략 누적만 표시합니다.",
        "- 과거 돈흐름/눌림/구전략 장부는 보관하지만 현재 판단에서는 제외합니다.",
        "- TP분할 없음: 소액 실전 전제라 전량익절/전량손절 기준으로 봅니다.",
        "- v255: 과거 평균회귀 CLOSED까지 원인분석에 포함합니다. 기본 명령어는 캐시 전용입니다.",
        "",
        "[1/5] 평균회귀 전략 성과",
        _compact_stat_line("평균가 회귀 반등 정식", closed),
        "",
        "[2/5] 종료 사유",
        *_v248_exit_reason_lines(closed, 6),
        "",
        "[3/5] 과거 포함 손실 원인",
        *_v254_historical_review_lines(closed, compact=True),
        "",
        "[4/5] 평균회귀 관찰 참고",
        *_v250_strategy_score_summary_lines(),
        "",
        "[5/5] 놓친 후보 요약",
        f"- 탈락 후 +1.2 먼저 도달: {len(missed)}개",
        *_v248_missed_bucket_lines(watch_rows, focus_rows),
        "",
        "판독",
        "- /score는 평균가 회귀 반등 단타 전략 누적을 본다.",
        "- 조건을 바로 만지지 말고, 위 손실 원인 태그가 반복되는지 먼저 본다.",
        "- 후보수는 임시로 넓히지 않고, 좋은 후보를 죽인 탈락조건만 찾는다.",
    ]
    return "\n".join(lines)


_v254_prev_quality_text = _v250_quality_text

def _v250_quality_text() -> str:  # type: ignore[override]
    focus_rows = _v250_paper_latest_rows()
    paper_ready = [r for r in focus_rows if _v247_bool_ready(r)]
    closed = _v250_closed_rows(limit=12000)
    watch_rows = _v250_watch_rows(3.0)
    missed = _v250_missed_rows(watch_rows, focus_rows)
    snap = _v212_read_candidate_snapshot()
    lines = [
        "🔬 후보품질 요약 /quality",
        "- 기준: 평균가 회귀 반등 단타 단일검증 전략 누적만 현재 판단에 사용합니다.",
        "- v255: 이전 평균회귀 CLOSED까지 원인분석에 포함합니다. 조건 변경 전 확인용입니다.",
        "- 과거 돈흐름/눌림/구전략 장부는 보관하지만 현재 판단에서는 제외합니다.",
        f"- 참고 snapshot: {len((snap or {}).get('rows') or []) if isinstance(snap, dict) else 0}개 / {max(0.0, now_ts() - fnum((snap or {}).get('updated_ts'), now_ts())) if isinstance(snap, dict) else -1:.1f}초 전",
        "",
        "[1/7] 평균회귀 전략 성과",
        _compact_stat_line("평균가 회귀 반등 정식", closed),
        "",
        "[2/7] 과거 포함 손실 원인분석",
        *_v254_historical_review_lines(closed, compact=True),
        "",
        "[3/7] 하드가드급 손실 예시",
        *_v254_hard_loss_detail_lines(closed, 5),
        "",
        "[4/7] paper_latest 평균회귀 후보 흐름",
        f"- 평균회귀 후보 {len(focus_rows)}개 / trade_ready {len(paper_ready)}개 / 탈락 {max(0, len(focus_rows)-len(paper_ready))}개 / latest 기준",
        _simple_ws_line(focus_rows),
        _simple_micro_line(focus_rows),
        "",
        "[5/7] paper_latest trade_ready 못 간 이유",
        *_v250_focus_reject_summary_lines(focus_rows),
        "",
        "[6/7] 정보 신선도/놓친 후보",
        *_v248_fresh_breakdown_lines(focus_rows),
        "탈락했는데 오른 후보",
        *_v250_missed_lines(watch_rows, focus_rows, 5),
        *_v248_missed_bucket_lines(watch_rows, focus_rows),
        "",
        "[7/7] 단계별 예시 / 판독",
        "🧪 trade_ready 통과",
        *_v248_stage_example_lines(paper_ready, 4),
        "❔ trade_ready 탈락",
        *_v248_stage_example_lines([r for r in focus_rows if not _v247_bool_ready(r)], 5),
        "",
        "판독",
        "- 평균회귀는 VWAP 아래에서 버티고 되돌아오는 구간만 먹는 전략입니다.",
        "- 바로 조건을 만지지 말고, 과거 포함 손실 태그와 하드가드 예시를 먼저 봅니다.",
        "- 표본이 충분해도 최대수익 몇 개가 버티는 구조인지 같이 확인합니다.",
    ]
    return "\n".join(lines)


def reversion_review_text(full: bool = False) -> str:
    rows = _v250_closed_rows(limit=30000)
    lines = ["🧯 평균회귀 원인분석 /reversion_review"]
    lines += _v254_historical_review_lines(rows, compact=not full)
    return "\n".join(lines)


def command_reversion_review(update, context) -> None:
    reply(update, reversion_review_text(False))


def command_reversion_review_full(update, context) -> None:
    reply(update, reversion_review_text(True))


# v255: /reversion_review 계열은 install_commands() 본선 mapping에 직접 등록한다.


# v256: 위 reversion_review 관련 정의는 if __name__ == '__main__' 전에 위치해야 한다.
# v257: 자동 묶음 명령은 CommandHandler가 아니라 _builder_for_command()를 타므로 해당 경로에도 등록한다.


# ===============================
# v2.13.258: 평균회귀 진입확인 분리 + reversion_review 캐시화
# - 과거 원인분석 결과: 승리군/손실군의 VWAP·매수비·스프레드는 비슷했지만,
#   손실군은 "진입 후 반응없음"과 "1분 흐름 음수"가 반복됐다.
# - 따라서 VWAP/매수비/스프레드 수치는 건드리지 않고,
#   "후보 발견"과 "정식 OPEN"을 분리한다.
# - 1분 흐름 음수/돌아섬 미확인 후보는 paper_latest에는 남기되 trade_ready에서 제외한다.
# - /reversion_review 기본 명령은 직접계산 대신 캐시만 읽고, full에서만 상세 직접계산한다.
# ===============================

VWAP_REV_CONFIRM_MIN_CHANGE_1 = float(os.getenv("CLEAN_VWAP_REV_CONFIRM_MIN_CHANGE_1", "0.00"))
VWAP_REV_CONFIRM_MIN_RECOVERY = float(os.getenv("CLEAN_VWAP_REV_CONFIRM_MIN_RECOVERY", "0.05"))
VWAP_REV_CONFIRM_MIN_AVG_TURN = float(os.getenv("CLEAN_VWAP_REV_CONFIRM_MIN_AVG_TURN", "0.00"))


def _v258_turn_confirm_reasons(item: Dict[str, Any]) -> List[str]:
    """평균회귀 정식 OPEN 전 '돌아섬' 확인.

    조건을 새로 넓히거나 좁히는 목적이 아니라, 평균회귀 후보 중
    아직 하락 중인 후보를 관찰로 빼기 위한 trade_ready 전용 게이트다.
    """
    if not _v250_is_focus_item(item):
        return []
    reasons: List[str] = []
    ch1 = fnum(item.get("change_1"), 0.0)
    avg_turn = fnum(item.get("avg_price_turn_pct"), 0.0)
    recovery = fnum(item.get("recovery_speed_pct"), 0.0)
    lower_wick = fnum(item.get("current_lower_wick_pct"), 0.0)
    if ch1 < VWAP_REV_CONFIRM_MIN_CHANGE_1:
        reasons.append(f"1분 흐름 음수 {ch1:+.2f}%")
    turned = (
        ch1 >= max(0.02, VWAP_REV_CONFIRM_MIN_CHANGE_1)
        or avg_turn > VWAP_REV_CONFIRM_MIN_AVG_TURN
        or recovery >= VWAP_REV_CONFIRM_MIN_RECOVERY
    )
    # 아래꼬리만 있는 경우는 '바닥 가능성'이지 회귀 시작 확정이 아니므로 단독 통과시키지 않는다.
    if not turned:
        reasons.append(f"돌아섬 확인 부족: 1분 {ch1:+.2f}% / 평균가턴 {avg_turn:+.2f}% / 회복 {recovery:+.2f}% / 아래꼬리 {lower_wick:.2f}%")
    return list(dict.fromkeys(reasons))[:4]


_v258_base_decide_trade_ready = decide_trade_ready

def decide_trade_ready(item: Dict[str, Any]) -> Tuple[bool, str, List[str]]:  # type: ignore[override]
    ok, label, reasons = _v258_base_decide_trade_ready(item)
    if not _v250_is_focus_item(item):
        return ok, label, reasons
    turn_reasons = _v258_turn_confirm_reasons(item)
    if turn_reasons:
        merged = list(reasons or []) + turn_reasons
        return False, "평균회귀 후보 관찰: 돌아섬 확인 대기", list(dict.fromkeys(str(x) for x in merged))[:8]
    return ok, label, reasons


# /reversion_review 기본 명령 캐시화. 상세 직접계산은 /reversion_review_full에만 남긴다.
FILES["reversion_review_summary"] = BASE_DIR / "clean_reversion_review_summary.json"


def _v258_reversion_review_text_for_cache() -> str:
    rows = _v250_closed_rows(limit=30000)
    lines = [
        "🧯 평균회귀 원인분석 /reversion_review",
        "- v258: 기본 명령은 캐시 전용입니다. 긴 직접계산은 /reversion_review_full",
    ]
    lines += _v254_historical_review_lines(rows, compact=True)
    lines += [
        "",
        "다음 판단",
        "- 1분 흐름 음수/진입 후 반응없음이 줄어드는지 확인",
        "- 하드가드급 손실이 빠른 실패청산 쪽으로 줄어드는지 확인",
        "- 조건 수치 조정은 이 결과를 본 뒤 진행",
    ]
    return "\n".join(lines)


def _v258_build_reversion_review_cache(reason: str = "background") -> None:
    try:
        txt = _v258_reversion_review_text_for_cache()
        payload = _v251_score_cache_payload(txt, "reversion_review")
        payload["build_reason"] = reason
        save_json(FILES["reversion_review_summary"], payload)
        with _state_lock:
            STATE["v258_reversion_review_cache_ts"] = now_ts()
            STATE["v258_reversion_review_cache_reason"] = reason
    except Exception as exc:
        log_error("v258_build_reversion_review_cache", exc)


_v258_prev_build_light_command_caches = _build_light_command_caches

def _build_light_command_caches() -> None:  # type: ignore[override]
    try:
        _v258_prev_build_light_command_caches()
    except Exception as exc:
        log_error("v258_prev_light_cache", exc)
    _v258_build_reversion_review_cache("light_command_worker")


_v258_direct_reversion_review_text = reversion_review_text

def reversion_review_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v258_direct_reversion_review_text(True)
    obj = load_json(FILES["reversion_review_summary"], {})
    ok, why = _v251_cache_valid(obj, name="reversion_review")
    if ok:
        return str(obj.get("text") or "")
    return _v251_wait_text("🧯 평균회귀 원인분석 /reversion_review", "/reversion_review", why)


def _v258_map_text() -> str:
    return "\n".join([
        "# 코인봇 v2.13.258 수정 지도",
        "",
        "## 원인",
        "- 평균회귀 승리군/손실군의 VWAP·매수비·스프레드 평균값은 큰 차이가 없었다.",
        "- 손실은 진입 후 반응없음, 1분 흐름 음수, 하드가드급 큰손실에서 반복됐다.",
        "",
        "## 수정",
        "- 후보 발견과 정식 OPEN을 분리한다.",
        "- 1분 흐름 음수/돌아섬 미확인 후보는 paper_latest에는 남기되 trade_ready 제외한다.",
        "- /reversion_review 기본 명령은 캐시 전용으로 돌린다.",
        "",
        "## 미변경",
        "- VWAP -0.3~-1.5, 3분돈 700만, spread 0.5, 매수비 0.52 수치 변경 없음.",
        "- 자동매수 ON 없음, BUY_READY 강제 없음, 장부 삭제 없음.",
    ])


if __name__ == "__main__":
    main()
