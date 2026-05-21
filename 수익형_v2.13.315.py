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

BOT_VERSION = "수익형 v2.13.291"
# HTTP 헤더는 latin-1만 안전하다. BOT_VERSION은 한글이라 User-Agent로 쓰면
# UnicodeEncodeError가 나며 bulk 스캔이 시작 즉시 0으로 죽는다.
HTTP_USER_AGENT = "coinbot-v2.13.265-mainline"
STRATEGY_NAME = "저점 쓸림 후 VWAP 회복 단타"
STRATEGY_KEY = "sweep_vwap_recovery"
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
FILES.setdefault("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json")

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
    "phase_note": "v291: micro 긴급수집/target 연결 + snapshot/공장 저장 경량화. sweep_vwap_recovery 조건값 변경 없음.",
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
    """v285: 단일 JSON 저장 입구.

    이전 save_json은 모든 writer가 같은 <file>.tmp를 공유해서,
    백그라운드 직원과 명령어가 동시에 같은 파일을 저장하면 한쪽 os.replace 뒤
    다른 쪽 tmp가 사라져 FileNotFoundError가 났다.
    여기서는 구 tmp 공유 경로를 삭제하고, pid/thread/time 기반 고유 tmp만 사용한다.
    """
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
        tid = threading.get_ident() if 'threading' in globals() else 0
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{tid}.{time.time_ns()}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log_error(f"save_json:{Path(path).name}", exc)


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







def event_key(row: Dict[str, Any], lane: str, ts: Optional[float] = None) -> str:
    t = str(row.get("ticker") or "UNKNOWN").upper()
    bucket = int((ts or now_ts()) // max(60, EVENT_DEDUP_SEC))
    return f"clean:{lane}:{t}:{bucket}"








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


TELEGRAM_SEND_TIMEOUT_SEC = float(os.getenv("CLEAN_TELEGRAM_SEND_TIMEOUT_SEC", "5.0"))


def _telegram_status_payload(state: str, error: str = "-", **extra: Any) -> Dict[str, Any]:
    payload = {
        "telegram_state": str(state or "unknown"),
        "telegram_error": str(error or "-")[:300],
        "telegram_updated_ts": now_ts(),
        "telegram_updated_text": now_text(),
        "telegram_note": "v305: start_polling 먼저 시작하고 시작알림은 이후 timeout 전송",
    }
    payload.update(extra or {})
    return payload


def mark_telegram_state(state: str, error: str = "-", **extra: Any) -> None:
    """메인봇 Telegram 수신 상태를 status 파일에 남긴다.

    systemd active/hash가 정상이어도 polling이 안 붙으면 운영상 정상 봇이 아니다.
    v305부터는 가드봇이 이 값을 읽어 active지만 무응답인 상태를 구분한다.
    """
    try:
        payload = _telegram_status_payload(state, error, **extra)
        with _state_lock:
            STATE.update(payload)
        save_json(FILES["status"], STATE)
        log(f"telegram_state={payload['telegram_state']} error={payload['telegram_error']}")
    except Exception as exc:
        try:
            log_error("mark_telegram_state", exc)
        except Exception:
            pass


def telegram_ready_check() -> Tuple[bool, str]:
    missing: List[str] = []
    if not TELEGRAM_TOKEN:
        missing.append("TELEGRAM_TOKEN")
    if not CHAT_ID:
        missing.append("CHAT_ID")
    if Updater is None:
        missing.append("python-telegram-bot Updater import")
    if CommandHandler is None:
        missing.append("python-telegram-bot CommandHandler import")
    if missing:
        return False, "missing:" + ",".join(missing)
    return True, "ok"


def _telegram_send_api(text: str, limit: int = 3600, timeout: Optional[float] = None) -> bool:
    """Telegram 전송 단일 입구.

    v305: Bot.send_message가 시작 알림 단계에서 오래 막히면 polling 시작 전 무응답 상태가 된다.
    표준 urllib + 짧은 timeout으로 바꿔 전송 실패가 명령 수신을 막지 않게 한다.
    """
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(text, flush=True)
        return False
    timeout = TELEGRAM_SEND_TIMEOUT_SEC if timeout is None else float(timeout)
    body = str(text or "")
    ok = True
    for i in range(0, len(body), max(1000, int(limit or 3600))):
        chunk = body[i:i+limit]
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": CHAT_ID,
                "text": chunk,
                "disable_web_page_preview": "true",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(200)
        except Exception as exc:
            ok = False
            log_error("telegram_send_api", exc)
            break
    return ok


def send_chunks(text: str, limit: int = 3600) -> None:
    _telegram_send_api(text, limit=limit)


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
        "- 메인봇 대상: 수익형_v2.13.290.py",
        "- 페이퍼봇 대상: paper_bot_v0.64.py",
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





def command_score(update, context) -> None:
    reply(update, score_text())


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
        ("deploy", lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.290.py", "- 페이퍼봇 대상: paper_bot_v0.64.py"])),
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
    if "cannot schedule new futures after interpreter shutdown" in tail or "interpreter shutdown" in tail:
        kinds.append("과거 종료 중 worker 정리")
    if "command_reversion_review" in tail and "NameError" in tail:
        kinds.append("과거 v255 명령등록 오류")
    if "first_number" in tail:
        kinds.append("과거 웹소켓 first_number")
    if "tail_text" in tail:
        kinds.append("과거 errorlog tail_text")
    if "Traceback" in tail and not kinds:
        kinds.append("과거 traceback")
    if "RuntimeError" in tail and not kinds:
        kinds.append("과거 runtime 오류")
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
    if "cannot schedule new futures after interpreter shutdown" in tail or "interpreter shutdown" in tail:
        kinds.append("과거 종료 중 worker 정리")
    if "command_reversion_review" in tail and "NameError" in tail:
        kinds.append("과거 v255 명령등록 오류")
    if "first_number" in tail:
        kinds.append("과거 웹소켓 first_number 누락")
    if "tail_text" in tail:
        kinds.append("과거 errorlog tail_text 누락")
    if "Traceback" in tail and not kinds:
        kinds.append("과거 traceback")
    if "RuntimeError" in tail and not kinds:
        kinds.append("과거 runtime 오류")
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
            "deploy": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.290.py", "- 페이퍼봇 대상: paper_bot_v0.64.py"]),
            "upgradestatus": lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.290.py", "- 페이퍼봇 대상: paper_bot_v0.64.py"]),
            "paper_today": lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", "", new_period_text(), candidate_fresh_text()]),
            "score": score_text,
            "version_score": version_score_text,
            "strategy_watch": lambda: strategy_watch_text(False),
            "watch": lambda: strategy_watch_text(False),
            "strategy_watch_full": lambda: strategy_watch_text(True),
            "reversion_review": lambda: reversion_review_text(False),
            "avg_review": lambda: reversion_review_text(False),
            "reversion_review_full": lambda: reversion_review_text(True),
            "avg_review_full": lambda: reversion_review_text(True),
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






def score_full_text() -> str:
    return score_text()


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
        "strategy_watch": command_strategy_watch,
        "watch": command_strategy_watch,
        "strategy_watch_full": command_strategy_watch_full,
        "reversion_review": command_reversion_review,
        "avg_review": command_reversion_review,
        "reversion_review_full": command_reversion_review_full,
        "avg_review_full": command_reversion_review_full,
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
        menu = ["batch", "health", "check", "core", "external_status", "quality", "score", "version_score", "strategy_watch", "version_compare", "loss_review", "reversion_review", "cpu_status", "paper_handoff", "deploy", "trade", "errorlog", "help"]
        updater.bot.set_my_commands([BotCommand(k, k) for k in menu])
    except Exception:
        pass




# ===============================
# v2.13.209: 캐시 전용 명령어 본선 + 자원/품질 요약 직원
# - 기본 명령어는 무거운 장부/후보/외부캐시 재계산을 하지 않고 요약 캐시만 읽는다.
# - 직접 계산 경로는 백그라운드 요약 직원과 *_full 상세 명령에만 남긴다.
# - 전략 조건/청산/BUY_READY는 변경하지 않는다.
# ===============================
_v209_direct_health_text = globals().get("health_text", lambda *a, **k: "")
_v209_direct_external_status_text = globals().get("external_status_text", lambda *a, **k: "")
_v209_direct_candidate_quality_text = globals().get("candidate_quality_text", lambda *a, **k: "")
_v209_direct_version_score_text = globals().get("version_score_text", lambda *a, **k: "")
_v209_original_update_ws_targets = globals().get("update_ws_targets", lambda *a, **k: None)

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




def _write_resource_status() -> Dict[str, Any]:
    r = server_resource_snapshot()
    save_json(FILES["resource_status"], r)
    return r


def _read_resource_status() -> Dict[str, Any]:
    obj = load_json(FILES.get("resource_status", BASE_DIR / "clean_resource_status.json"), {})
    return obj if isinstance(obj, dict) else {}




def _cache_payload(text: str, name: str) -> Dict[str, Any]:
    return {"version": BOT_VERSION, "name": name, "updated_ts": now_ts(), "updated_text": now_text(), "text": str(text or "")}






















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










# v276: 전략층 철거 후 install_commands가 구 명령 이름을 찾지 못해
# 메인봇이 재시작되던 경로를 제거하고, 재료공장 단일 화면으로 연결한다.
def command_strategy_watch(update, context) -> None:  # type: ignore[override]
    reply(update, strategy_watch_text(False))


def command_strategy_watch_full(update, context) -> None:  # type: ignore[override]
    reply(update, strategy_watch_text(True))


def command_reversion_review(update, context) -> None:  # type: ignore[override]
    reply(update, reversion_review_text(False))


def command_reversion_review_full(update, context) -> None:  # type: ignore[override]
    reply(update, reversion_review_text(True))



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




def external_status_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["external_snapshot"], "/external_status")


def version_score_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES["version_score_summary"], "/version_score")






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
    """v279: material-only worker set. Removed legacy execution_risk worker entirely."""
    global _background_workers_started
    if _background_workers_started:
        return
    _background_workers_started = True
    for i in range(max(1, PRECISION_BACKGROUND_WORKERS)):
        threading.Thread(target=precision_worker_loop, args=(i + 1,), name=f"precision_worker_{i+1}", daemon=True).start()
    threading.Thread(target=command_cache_worker_loop, name="command_cache_worker_material", daemon=True).start()
    websocket_hub_worker_loop()
    log(f"background_workers started precision={PRECISION_BACKGROUND_WORKERS} command_cache=material websocket=external_sidecar_cache requested={WS_HUB_REQUESTED}")


def main() -> None:
    """메인봇 부팅 본선.

    v305 수술 포인트:
    - Telegram이 준비 안 됐는데 systemd active로 숨어 있는 무한 sleep 제거.
    - start_polling을 시작 알림보다 먼저 실행해, 알림 전송 지연이 명령 수신을 막지 않게 함.
    - clean_brain_status.json에 telegram_state를 남겨 /gdeploy가 실제 수신 상태를 볼 수 있게 함.
    - 전략 조건/등급/청산/paper 장부는 변경하지 않음.
    """
    updater = None
    try:
        startup_checks()
        ok, reason = telegram_ready_check()
        if not ok:
            mark_telegram_state("disabled", reason, version=BOT_VERSION)
            print(f"{BOT_VERSION} telegram disabled: {reason}", flush=True)
            raise RuntimeError(f"telegram_not_ready:{reason}")

        mark_telegram_state("initializing", "-", version=BOT_VERSION)
        updater = Updater(token=TELEGRAM_TOKEN, use_context=True)
        install_commands(updater)
        mark_telegram_state("commands_installed", "-", version=BOT_VERSION, command_count=len(STATE.get("compat_commands") or []))

        updater.start_polling(drop_pending_updates=True)
        mark_telegram_state("polling_started", "-", version=BOT_VERSION, command_count=len(STATE.get("compat_commands") or []))
        print(f"{BOT_VERSION} telegram polling_started", flush=True)

        # polling이 먼저 붙은 뒤 시작 알림을 보낸다. 알림 실패는 명령 수신을 막지 않는다.
        sent = _telegram_send_api("\n".join([
            "✅ 봇 시작 완료",
            f"현재 버전: {BOT_VERSION}",
            f"전략: {STRATEGY_NAME}",
            "v305: Telegram polling 선시작 + 시작상태 기록 수술(전략/청산/등급 변경 없음)",
            "확인: /health /score /quality /strategy_watch /errorlog",
        ]), timeout=TELEGRAM_SEND_TIMEOUT_SEC)
        if not sent:
            mark_telegram_state("polling_started_notify_failed", "startup_notice_send_failed", version=BOT_VERSION)

        start_background_workers()
        t = threading.Thread(target=scan_loop, name="clean_scan_loop", daemon=True)
        t.start()
        mark_telegram_state("running", "-", version=BOT_VERSION, scan_thread="started")

        updater.idle()
    except KeyboardInterrupt:
        mark_telegram_state("stopping", "KeyboardInterrupt", version=BOT_VERSION)
        _stop_event.set()
        return
    except Exception as exc:
        mark_telegram_state("error", f"{exc.__class__.__name__}: {exc}", version=BOT_VERSION)
        log_error("main_v305_boot", exc)
        _stop_event.set()
        raise
    finally:
        try:
            _stop_event.set()
        except Exception:
            pass




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


# v2.13.284: precision target selector surgery
# - v213 hot-queue wrapper called a missing base selector and produced list + NoneType errors.
# - The old chained wrapper is removed from the active path.
# - This selector is a single material-factory selector: market bulk rows -> precision target rows.

def _v283_precision_target_score(row: Dict[str, Any]) -> float:
    r = row or {}
    turnover24 = fnum(r.get("turnover_24h") or r.get("acc_trade_value_24h") or r.get("trade_value_24h"), 0.0)
    money3 = fnum(r.get("turnover_3m") or r.get("money_flow_3m"), 0.0)
    move = max(abs(fnum(r.get("change_1"), 0.0)), abs(fnum(r.get("change_3"), 0.0)), abs(fnum(r.get("change_5"), 0.0)))
    ws_bonus = 8.0 if _v274_bool_fresh(r, "ws", 10.0) or bool(r.get("ws_fresh")) else 0.0
    micro_bonus = 8.0 if _v274_bool_fresh(r, "micro", 15.0) else 0.0
    major_bonus = 5.0 if _ticker_from_any(r.get("ticker") or r.get("market")) in {"BTC", "ETH", "XRP"} else 0.0
    return math.log10(max(turnover24, 0.0) + 1.0) * 8.0 + math.log10(max(money3, 0.0) + 1.0) * 4.0 + move * 12.0 + ws_bonus + micro_bonus + major_bonus


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    source_rows = rows if isinstance(rows, list) else []
    by_t: Dict[str, Dict[str, Any]] = {}
    for raw in source_rows:
        if not isinstance(raw, dict):
            continue
        t = _ticker_from_any(raw.get("ticker") or raw.get("market") or raw.get("symbol"))
        if not t:
            continue
        rr = dict(raw)
        rr["ticker"] = t
        rr["precision_target_source"] = "v283_material_selector"
        if t not in by_t or _v283_precision_target_score(rr) > _v283_precision_target_score(by_t[t]):
            by_t[t] = rr
    ordered = sorted(by_t.values(), key=_v283_precision_target_score, reverse=True)
    limit = max(PRECISION_REFRESH_BASE, min(len(ordered), PRECISION_TURNOVER_CORE_RANK))
    out = ordered[:limit]
    with _state_lock:
        STATE["precision_target_note"] = f"v283 material selector {len(out)}/{len(ordered)}"
        STATE["precision_hot_queue_included"] = 0
        STATE["precision_target_source"] = "v283_material_selector"
    return out


def precision_priority(row: Dict[str, Any]) -> float:  # type: ignore[override]
    # refresh_precision sorts ascending. Higher target score must run earlier.
    return -_v283_precision_target_score(row if isinstance(row, dict) else {})


_v213_base_update_ws_targets = globals().get("update_ws_targets", lambda *a, **k: None)
_v213_base_update_micro_targets = globals().get("update_micro_targets", lambda *a, **k: None)

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






_v213_base_snapshot_payload = globals().get("_v212_snapshot_payload", lambda *a, **k: {})



_v213_base_external_status_text = globals().get("_v210_external_status_text", lambda *a, **k: "")



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


_v214_prev_quality_text_builder = globals().get("_v210_quality_text", lambda *a, **k: "")

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






def _v217_write_targets_file(path: Path, payload: Dict[str, Any]) -> None:
    atomic_write(path, payload)








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




V229_EXTERNAL_REFRESH_INTERVALS_SEC = [float(x) for x in os.getenv("CLEAN_V229_EXTERNAL_REFRESH_INTERVALS_SEC", "1.2,2.2,3.2").split(",") if str(x).strip()]




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

_v220_base_update_ws_targets = globals().get("update_ws_targets", lambda *a, **k: None)

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




_v220_base_external_status_text = globals().get("_v210_external_status_text", lambda *a, **k: "")



_v220_base_health_text = globals().get("health_text", lambda *a, **k: "")




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
_v221_base_update_micro_targets = globals().get("update_micro_targets", lambda *a, **k: None)


_v221_base_external_status_text = globals().get("_v210_external_status_text", lambda *a, **k: "")



_v221_base_quality_text = globals().get("candidate_quality_text", lambda *a, **k: "")



_v221_base_health_text = globals().get("health_text", lambda *a, **k: "")




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


_v223_base_quality_text_builder = globals().get("_v216_quality_text", lambda *a, **k: "")



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


_v224_base_health_text = globals().get("health_text", lambda *a, **k: "")



_v224_base_quality_text_builder = globals().get("_v216_quality_text", lambda *a, **k: "")




# ===============================
# v2.13.226: micro target 본선 재연결
# - v225에서 v221 urgent wrapper가 감쌀 본선 writer 이름을 저장하지 않아 scan_once가 중단되던 문제 수정
# - fresh 판정은 v225 single micro cache reader 유지
# - 조건/청산/BUY_READY/자동매수 변경 없음
# ===============================

_v226_base_health_text = globals().get("health_text", lambda *a, **k: "")

_v226_base_quality_text_builder = globals().get("_v216_quality_text", lambda *a, **k: "")

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
_v227_base_update_micro_targets = globals().get("_v221_base_update_micro_targets", globals().get("update_micro_targets", lambda *a, **k: None))



_v227_base_external_status_text = globals().get("_v210_external_status_text", lambda *a, **k: "")



_v227_base_quality_text = globals().get("candidate_quality_text", lambda *a, **k: "")


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


_v228_base_external_status_text = globals().get("_v210_external_status_text", lambda *a, **k: "")

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


_v228_base_health_builder = globals().get("_v210_health_text", lambda *a, **k: "")

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


_v228_base_quality_text_builder = globals().get("_v216_quality_text", lambda *a, **k: "")




# ===============================
# v2.13.230: quality 출력 연결 + snapshot 병목 캐시 + paper 시간표시 연동
# - 조건/청산/BUY_READY/자동매수 변경 없음
# - 구 _v210 quality 캐시 경로가 다시 물리지 않도록 현재 builder를 최종 고정
# - target 계산 중 반복 파일읽기(open/recent candidate)를 짧은 TTL 캐시로 줄인다.
# ===============================

_V230_CACHE_TTL_SEC = float(os.getenv("CLEAN_V230_TARGET_CACHE_TTL_SEC", "1.5"))
_V230_RECENT_PRIORITY_CACHE: Dict[str, Any] = {"ts": 0.0, "limit": 0, "rows": []}
_V230_OPEN_TICKER_CACHE: Dict[str, Any] = {"ts": 0.0, "limit": 0, "rows": []}
_v230_base_recent_candidate_priority_rows = globals().get("recent_candidate_priority_rows", lambda *a, **k: [])
_v230_base_paper_open_tickers = globals().get("_paper_open_tickers", lambda *a, **k: set())


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




def _build_light_command_caches() -> None:  # type: ignore[override]
    """v287: 기본 명령 캐시는 현재 재료공장 단일 경로만 갱신한다.

    v231 observation cache는 구 candidate_snapshot/구 quality renderer를 다시 물 수 있어서
    현재 본선에서 제거한다. /health, /quality, /strategy_watch는 저장된 cache만 읽는다.
    """
    try:
        _write_resource_status()
    except Exception as exc:
        log_error("v287_resource_cache", exc)
    try:
        _v287_write_snapshot_command_caches(reason="command_worker")
    except Exception as exc:
        log_error("v287_light_command_cache", exc)





def _v219_write_command_caches_after_snapshot(build_quality: bool = False) -> None:  # type: ignore[override]
    """v287: post-refresh cache도 현재 재료 snapshot cache 경로로만 연결한다.

    구 v231/v238 observation cache가 health_snapshot을 덮어쓰던 경로를 실제 제거한다.
    """
    try:
        _v287_write_snapshot_command_caches(reason="post_refresh")
    except Exception as exc:
        log_error("v287_post_refresh_cache", exc)



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








# ===============================
# v2.13.237: 캐시 버전 가드 + paper_latest scan_id 최종 보강
# - 이전 버전 command cache가 /version_score, /quality에 물리는 경로를 차단한다.
# - 어떤 공장/refresh 경로가 latest를 쓰더라도 마지막에 scan_id/TTL을 보강한다.
# - 후보조건/청산조건/자동매수/BUY_READY/v343 변경 없음.
# ===============================



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

_v237_original_export_candidates = globals().get("export_candidates", lambda strict, shadow: {})





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


_v240_base_quality_text_from_snapshot = globals().get("_v238_quality_text_from_snapshot", lambda *a, **k: "")

def _v238_quality_text_from_snapshot(snap: Optional[Dict[str, Any]] = None) -> str:  # type: ignore[override]
    txt = _v240_base_quality_text_from_snapshot(snap if isinstance(snap, dict) else _v212_read_candidate_snapshot())
    for old in ("v239", "v238", "v237", "v236", "v235", "v234", "v233", "v231", "v230", "v222"):
        txt = txt.replace(f"{old} 기본은 캐시 전용", f"{BOT_VERSION} 기준: 캐시 전용")
    return txt


_v240_base_quality_payload = globals().get("_v238_quality_payload", lambda *a, **k: {})

def _v238_quality_payload(text: str, *, snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:  # type: ignore[override]
    payload = _v240_base_quality_payload(text, snap=snap)
    payload["quality_reader"] = "v240_cache_worker_only_same_closed_reader"
    payload["quality_direct_rebuild_disabled"] = True
    return payload




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

# ===============================
# v2.13.288: 7전략 실험 종료 / 단일전략 본선 재설계
# - v274~v287의 7전략 재료실험 active path를 제거한다.
# - 새 active path는 sweep_vwap_recovery 단일 전략만 후보 생성/저장/성과 표시한다.
# - 자동매수 ON/BY_READY 생성 없음. paper_bot 검증용 paper 후보만 출력한다.
# - micro/WS 정보가 없으면 즉시 탈락시키지 않고 urgent target으로 보내 재확인 대기한다.
# ===============================

BOT_VERSION = "수익형 v2.13.291"
STRATEGY_NAME = "저점 쓸림 후 VWAP 회복 단타"
STRATEGY_KEY = "sweep_vwap_recovery"
STRATEGY_LABEL = "저점 쓸림 후 VWAP 회복 단타"

V288_MATERIAL_MAX_ROWS = int(os.getenv("CLEAN_V288_MATERIAL_MAX_ROWS", "120"))
V288_STRICT_MAX_ROWS = int(os.getenv("CLEAN_V288_STRICT_MAX_ROWS", "80"))
V288_SHADOW_MAX_ROWS = int(os.getenv("CLEAN_V288_SHADOW_MAX_ROWS", "80"))
V288_TRADE_READY_MAX_ROWS = int(os.getenv("CLEAN_V288_TRADE_READY_MAX_ROWS", "24"))
V288_FROM_LOW_MIN = float(os.getenv("CLEAN_V288_FROM_LOW_MIN", "0.25"))
V288_FROM_LOW_MAX = float(os.getenv("CLEAN_V288_FROM_LOW_MAX", "2.20"))
V288_HIGH_ROOM_MIN = float(os.getenv("CLEAN_V288_HIGH_ROOM_MIN", "0.90"))
V288_VWAP_PASS_GAP = float(os.getenv("CLEAN_V288_VWAP_PASS_GAP", "-0.10"))
V288_VWAP_BLOCK_GAP = float(os.getenv("CLEAN_V288_VWAP_BLOCK_GAP", "-0.30"))
V288_EMA5_PASS_GAP = float(os.getenv("CLEAN_V288_EMA5_PASS_GAP", "-0.10"))
V288_CHANGE1_MIN = float(os.getenv("CLEAN_V288_CHANGE1_MIN", "0.00"))
V288_CHANGE3_MIN = float(os.getenv("CLEAN_V288_CHANGE3_MIN", "-0.10"))
V288_CHANGE3_BLOCK = float(os.getenv("CLEAN_V288_CHANGE3_BLOCK", "-0.25"))
V288_MONEY1_MIN = float(os.getenv("CLEAN_V288_MONEY1_MIN", "5000000"))
V288_MONEY3_MIN = float(os.getenv("CLEAN_V288_MONEY3_MIN", "12000000"))
V288_BIG_MONEY3 = float(os.getenv("CLEAN_V288_BIG_MONEY3", "30000000"))
V288_BIG_MONEY_NO_REACTION_CH3 = float(os.getenv("CLEAN_V288_BIG_MONEY_NO_REACTION_CH3", "0.05"))
V288_SPREAD_PASS = float(os.getenv("CLEAN_V288_SPREAD_PASS", "0.35"))
V288_SPREAD_BLOCK = float(os.getenv("CLEAN_V288_SPREAD_BLOCK", "0.45"))
V288_BUY_RATIO_PASS = float(os.getenv("CLEAN_V288_BUY_RATIO_PASS", "0.50"))
V288_BUY_RATIO_BLOCK = float(os.getenv("CLEAN_V288_BUY_RATIO_BLOCK", "0.45"))
V288_ENTRY_JUMP_BLOCK = float(os.getenv("CLEAN_V288_ENTRY_JUMP_BLOCK", "0.45"))
V288_TAKE_PROFIT_PCT = float(os.getenv("CLEAN_V288_TAKE_PROFIT_PCT", "1.20"))
V288_PROTECT_TRIGGER_PCT = float(os.getenv("CLEAN_V288_PROTECT_TRIGGER_PCT", "0.70"))
V288_PROTECT_FLOOR_PCT = float(os.getenv("CLEAN_V288_PROTECT_FLOOR_PCT", "0.30"))
V288_STOP_LOSS_PCT = float(os.getenv("CLEAN_V288_STOP_LOSS_PCT", "-0.55"))
V288_TIME_EXIT_MIN = float(os.getenv("CLEAN_V288_TIME_EXIT_MIN", "15"))

FILES.setdefault("strategy_material_latest", BASE_DIR / "strategy_material_latest.jsonl")
FILES.setdefault("strategy_material_snapshot", BASE_DIR / "strategy_material_snapshot.json")
FILES.setdefault("strategy_lab_summary", BASE_DIR / "strategy_lab_summary.json")
FILES.setdefault("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json")
FILES.setdefault("score_summary", BASE_DIR / "clean_score_summary.json")
FILES.setdefault("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json")


def _v274_ticker(row: Dict[str, Any]) -> str:
    return _ticker_from_any((row or {}).get("ticker") or (row or {}).get("market") or (row or {}).get("symbol"))


def _v274_bool_fresh(row: Dict[str, Any], prefix: str, max_age: float = 15.0) -> bool:
    if bool((row or {}).get(f"{prefix}_fresh")):
        return True
    age = fnum((row or {}).get(f"{prefix}_age_sec"), 9999)
    return 0 <= age <= max_age


def _v288_as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        raw = value
    elif isinstance(value, (tuple, set)):
        raw = list(value)
    elif value in (None, "", {}, []):
        raw = []
    else:
        raw = [value]
    out: List[str] = []
    for x in raw:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _v274_market_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    total = 0
    up1 = up3 = up5 = money_active = 0
    sum1 = sum3 = sum5 = 0.0
    majors: Dict[str, Dict[str, float]] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = _v274_ticker(r)
        if not t:
            continue
        total += 1
        c1 = fnum(r.get("change_1"), 0)
        c3 = fnum(r.get("change_3"), 0)
        c5 = fnum(r.get("change_5"), 0)
        sum1 += c1; sum3 += c3; sum5 += c5
        if c1 > 0: up1 += 1
        if c3 > 0: up3 += 1
        if c5 > 0: up5 += 1
        if fnum(r.get("turnover_3m") or r.get("money_flow_3m"), 0) >= V288_MONEY3_MIN:
            money_active += 1
        if t in {"BTC", "ETH", "XRP"}:
            majors[t] = {"change_1": c1, "change_3": c3, "change_5": c5}
    def ratio(v: int) -> float:
        return (v / total * 100.0) if total else 0.0
    def avg(v: float) -> float:
        return (v / total) if total else 0.0
    btc3 = fnum(majors.get("BTC", {}).get("change_3"), 0)
    eth3 = fnum(majors.get("ETH", {}).get("change_3"), 0)
    xrp3 = fnum(majors.get("XRP", {}).get("change_3"), 0)
    major_pressure = "하락압력" if min(btc3, eth3, xrp3) <= -0.4 else ("우호" if max(btc3, eth3, xrp3) >= 0.25 and ratio(up3) >= 40 else "중립")
    return {
        "total": total,
        "up1_ratio": ratio(up1),
        "up3_ratio": ratio(up3),
        "up5_ratio": ratio(up5),
        "avg_change_1": avg(sum1),
        "avg_change_3": avg(sum3),
        "avg_change_5": avg(sum5),
        "money_active_ratio": ratio(money_active),
        "majors": majors,
        "major_pressure": major_pressure,
    }


def _v288_pick(row: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        try:
            v = (row or {}).get(k)
            if v not in (None, "", [], {}):
                return v
        except Exception:
            pass
    return default


def _v290_has_value(row: Dict[str, Any], *keys: str) -> bool:
    """전략 재료가 실제로 붙었는지 확인한다.

    v289에서는 결측값 기본값(저점+0, 고점여유 999, VWAP 0)이
    정상 신호처럼 점수를 받아 재확인 후보가 과다 생성됐다.
    기본값으로 덮지 말고 원본 row에 해당 재료가 실제 존재하는지 먼저 본다.
    """
    try:
        for k in keys:
            if not isinstance(row, dict) or k not in row:
                continue
            v = row.get(k)
            if v not in (None, "", [], {}):
                return True
    except Exception:
        pass
    return False


def _v290_stage_line(stage_rows: Any, limit: int = 6) -> Tuple[List[str], List[str]]:
    out: List[str] = []
    slow: List[str] = []
    try:
        for item in list(stage_rows or [])[:limit]:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                name = str(item[0])
                sec = fnum(item[1], 0.0)
                note = str(item[2]) if len(item) >= 3 else ""
            elif isinstance(item, dict):
                name = str(item.get("name") or item.get("stage") or "단계")
                sec = fnum(item.get("sec") or item.get("elapsed") or item.get("time"), 0.0)
                note = str(item.get("note") or "")
            else:
                continue
            label = f"{name} {sec:.1f}s"
            if note:
                label += f"({note[:42]})"
            out.append(label)
            if sec >= 8.0:
                slow.append(f"⚠️ {name} {sec:.1f}s")
        return out, slow
    except Exception:
        return [], []


def _v290_wait_is_recheckable(wait: List[str]) -> bool:
    """재확인은 외부 신선정보/애매한 실전위험만 허용한다.

    돈·위치·VWAP·3분흐름 같은 전략 필수조건 부족은
    재확인이 아니라 관찰/금지로 내려야 한다.
    """
    allowed_prefix = (
        "micro긴급수집대기",
        "WS재확인",
        "스프레드애매",
        "매수체결애매",
        "시장방어약함",
    )
    for w in wait or []:
        s = str(w)
        if not any(s.startswith(x) for x in allowed_prefix):
            return False
    return True


def _v288_norm_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    raw = dict(row if isinstance(row, dict) else {})
    item = dict(raw)
    t = _v274_ticker(item)
    price = fnum(_v288_pick(item, "current_price", "trade_price", "price", default=0), 0)
    has_from_low = _v290_has_value(raw, "from_30m_low_pct", "from_low_pct")
    has_high_room = _v290_has_value(raw, "below_30m_high_pct", "high_gap_pct")
    has_vwap = _v290_has_value(raw, "vwap_gap_pct")
    has_ema5 = _v290_has_value(raw, "ema5_gap_pct")
    has_change1 = _v290_has_value(raw, "change_1", "rate_1m", "change_rate_1m")
    has_change3 = _v290_has_value(raw, "change_3", "rate_3m", "change_rate_3m")
    has_money1 = _v290_has_value(raw, "turnover_1m", "money_flow_1m")
    has_money3 = _v290_has_value(raw, "turnover_3m", "money_flow_3m")
    item.update({
        "ticker": t,
        "current_price": price,
        "entry_price": price,
        "detected_price": price,
        "change_1": fnum(_v288_pick(item, "change_1", "rate_1m", "change_rate_1m", default=0), 0),
        "change_3": fnum(_v288_pick(item, "change_3", "rate_3m", "change_rate_3m", default=0), 0),
        "change_5": fnum(_v288_pick(item, "change_5", "rate_5m", "change_rate_5m", default=0), 0),
        "turnover_1m": fnum(_v288_pick(item, "turnover_1m", "money_flow_1m", default=0), 0),
        "turnover_3m": fnum(_v288_pick(item, "turnover_3m", "money_flow_3m", default=0), 0),
        "turnover_5m": fnum(_v288_pick(item, "turnover_5m", "money_flow_5m", default=0), 0),
        "turnover_24h": fnum(_v288_pick(item, "turnover_24h", "acc_trade_value_24h", "trade_value_24h", default=0), 0),
        "vwap_gap_pct": fnum(_v288_pick(item, "vwap_gap_pct", default=0), 0),
        "ema5_gap_pct": fnum(_v288_pick(item, "ema5_gap_pct", default=0), 0),
        "from_30m_low_pct": fnum(_v288_pick(item, "from_30m_low_pct", "from_low_pct", default=0), 0),
        "below_30m_high_pct": fnum(_v288_pick(item, "below_30m_high_pct", "high_gap_pct", default=999), 999),
        "bb_lower_gap_pct": fnum(_v288_pick(item, "bb_lower_gap_pct", default=0), 0),
        "current_lower_wick_pct": fnum(_v288_pick(item, "current_lower_wick_pct", "lower_wick_pct", default=0), 0),
        "current_upper_wick_pct": fnum(_v288_pick(item, "current_upper_wick_pct", "upper_wick", default=0), 0),
        "low_defense_pct": fnum(_v288_pick(item, "low_defense_pct", default=0), 0),
        "market_context": market if isinstance(market, dict) else {},
        "v290_has_from_low": has_from_low,
        "v290_has_high_room": has_high_room,
        "v290_has_vwap": has_vwap,
        "v290_has_ema5": has_ema5,
        "v290_has_change1": has_change1,
        "v290_has_change3": has_change3,
        "v290_has_money1": has_money1,
        "v290_has_money3": has_money3,
    })
    if t:
        try:
            ws = ws_snapshot(t)
            # 이미 scan row에 fresh 값이 붙어 있으면 missing overlay로 지우지 않는다.
            if str(ws.get("ws_row_status") or "") != "missing" or not bool(item.get("ws_fresh")):
                item.update(ws)
        except Exception as exc:
            log_error("v290_ws_overlay", exc)
        try:
            ms = micro_snapshot(t)
            if str(ms.get("micro_row_status") or "") != "missing" or not bool(item.get("micro_fresh")):
                item.update(ms)
        except Exception as exc:
            log_error("v290_micro_overlay", exc)
    return item


def _v288_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    r = row or {}
    hard: List[str] = []
    wait: List[str] = []
    good: List[str] = []
    score = 0.0

    t = _v274_ticker(r)
    price = fnum(r.get("current_price"), 0)
    from_low = fnum(r.get("from_30m_low_pct"), 0)
    high_room = fnum(r.get("below_30m_high_pct"), 999)
    vwap_gap = fnum(r.get("vwap_gap_pct"), 0)
    ema5_gap = fnum(r.get("ema5_gap_pct"), 0)
    ch1 = fnum(r.get("change_1"), 0)
    ch3 = fnum(r.get("change_3"), 0)
    money1 = fnum(r.get("turnover_1m"), 0)
    money3 = fnum(r.get("turnover_3m"), 0)
    bb_low = fnum(r.get("bb_lower_gap_pct"), 0)
    lower_wick = fnum(r.get("current_lower_wick_pct"), 0)
    low_defense = fnum(r.get("low_defense_pct"), 0)
    spread = fnum(r.get("micro_spread_pct") if r.get("micro_spread_pct") is not None else r.get("orderbook_spread_pct"), 999)
    buy_ratio = fnum(r.get("micro_trade_buy_ratio_30"), 0)
    wall_ratio = fnum(r.get("micro_bid_ask_wall_ratio"), 0)
    ask_pressure = bool(r.get("micro_ask_wall_pressure") or r.get("micro_sell_trade_pressure"))
    micro_fresh = _v274_bool_fresh(r, "micro", MICRO_STALE_SEC if "MICRO_STALE_SEC" in globals() else 25.0)
    ws_fresh = _v274_bool_fresh(r, "ws", WS_HUB_STALE_SEC if "WS_HUB_STALE_SEC" in globals() else 12.0) or bool(r.get("ws_fresh"))
    ws_gap = fnum(r.get("current_price_ws_gap_pct"), 0)

    has_from_low = bool(r.get("v290_has_from_low"))
    has_high_room = bool(r.get("v290_has_high_room")) and high_room < 900
    has_vwap = bool(r.get("v290_has_vwap"))
    has_ema5 = bool(r.get("v290_has_ema5"))
    has_change1 = bool(r.get("v290_has_change1"))
    has_change3 = bool(r.get("v290_has_change3"))
    has_money1 = bool(r.get("v290_has_money1"))
    has_money3 = bool(r.get("v290_has_money3"))

    if not t or price <= 0:
        hard.append("가격없음")
    if t in MAJOR_WATCH_TICKERS:
        hard.append("대형주는 시장참고용")

    if not has_from_low:
        wait.append("저점위치값부족")
    else:
        swept_near = (from_low <= V288_FROM_LOW_MAX and (lower_wick > 0.05 or bb_low <= 0.80 or low_defense >= 0 or from_low <= 0.80))
        if V288_FROM_LOW_MIN <= from_low <= V288_FROM_LOW_MAX and swept_near:
            score += 2.0; good.append("저점쓸림후회복")
        elif from_low < V288_FROM_LOW_MIN:
            wait.append("저점회복확인부족")
        else:
            hard.append(f"저점대비늦음 {from_low:.2f}%")

    if not has_high_room:
        wait.append("고점여유값부족")
    elif high_room >= V288_HIGH_ROOM_MIN:
        score += 1.2; good.append("고점여유")
    else:
        hard.append(f"고점여유부족 {high_room:.2f}%")

    if not has_vwap:
        wait.append("VWAP값부족")
    elif vwap_gap >= V288_VWAP_PASS_GAP:
        score += 1.5; good.append("VWAP회복")
    elif vwap_gap <= V288_VWAP_BLOCK_GAP:
        hard.append(f"VWAP아래 {vwap_gap:+.2f}%")
    else:
        wait.append(f"VWAP재확인 {vwap_gap:+.2f}%")

    if not has_ema5:
        wait.append("EMA5값부족")
    elif ema5_gap >= V288_EMA5_PASS_GAP:
        score += 0.8; good.append("EMA5회복")
    else:
        wait.append(f"EMA5아래 {ema5_gap:+.2f}%")

    if not has_change1:
        wait.append("1분흐름값부족")
    elif ch1 >= V288_CHANGE1_MIN:
        score += 0.8; good.append("1분반응")
    else:
        wait.append(f"1분반응부족 {ch1:+.2f}%")
    if not has_change3:
        wait.append("3분흐름값부족")
    elif ch3 >= V288_CHANGE3_MIN:
        score += 1.0; good.append("3분방어")
    elif ch3 <= V288_CHANGE3_BLOCK:
        hard.append(f"3분흐름무너짐 {ch3:+.2f}%")
    else:
        wait.append(f"3분흐름재확인 {ch3:+.2f}%")

    if not has_money1:
        wait.append("1분거래대금값부족")
    elif money1 >= V288_MONEY1_MIN:
        score += 0.8; good.append("1분돈")
    else:
        wait.append("1분거래대금부족")
    if not has_money3:
        wait.append("3분거래대금값부족")
    elif money3 >= V288_MONEY3_MIN:
        score += 1.0; good.append("3분돈")
    else:
        wait.append("3분거래대금부족")
    if has_money3 and has_change3 and money3 >= V288_BIG_MONEY3 and ch3 < V288_BIG_MONEY_NO_REACTION_CH3:
        hard.append("돈은큰데가격반응없음")

    if not micro_fresh:
        wait.append("micro긴급수집대기")
    else:
        score += 1.0; good.append("micro신선")
        if spread <= V288_SPREAD_PASS:
            score += 0.9; good.append("스프레드통과")
        elif spread >= V288_SPREAD_BLOCK:
            hard.append(f"스프레드넓음 {spread:.2f}%")
        else:
            wait.append(f"스프레드애매 {spread:.2f}%")
        if buy_ratio >= V288_BUY_RATIO_PASS:
            score += 0.9; good.append("매수체결비통과")
        elif buy_ratio <= V288_BUY_RATIO_BLOCK:
            hard.append(f"매수체결약함 {buy_ratio:.2f}")
        else:
            wait.append(f"매수체결애매 {buy_ratio:.2f}")
        if ask_pressure or (0 < wall_ratio < 0.67):
            hard.append("매도벽/매도체결압력")
        else:
            score += 0.5; good.append("호가압력통과")

    if ws_fresh:
        score += 0.4; good.append("WS신선")
    else:
        wait.append("WS재확인")
    if ws_gap >= V288_ENTRY_JUMP_BLOCK:
        hard.append(f"진입전가격튐 {ws_gap:+.2f}%")

    if str((market or {}).get("major_pressure")) == "하락압력" and fnum((market or {}).get("up3_ratio"), 0) < 35:
        wait.append("시장방어약함")

    # v290: 재확인은 실전 신선정보/스프레드/체결비가 애매한 경우만.
    # 전략 필수 재료나 돈/흐름이 부족한 후보는 '관찰'로 내려 재확인 TOP을 오염시키지 않는다.
    if hard:
        action = "block"
    elif wait:
        action = "recheck_wait" if score >= 7.5 and _v290_wait_is_recheckable(wait) else "observe"
    else:
        action = "paper_open" if score >= 8.0 else "observe"

    return {
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "score": round(score, 3),
        "action": action,
        "good": good[:12],
        "wait": wait[:10],
        "hard": hard[:10],
        "from_low_pct": round(from_low, 3),
        "high_room_pct": round(high_room, 3) if high_room < 900 else 999,
        "vwap_gap_pct": round(vwap_gap, 3),
        "ema5_gap_pct": round(ema5_gap, 3),
        "spread_pct": round(spread, 4) if spread < 900 else 999,
        "buy_ratio": round(buy_ratio, 4),
        "micro_fresh": micro_fresh,
        "ws_fresh": ws_fresh,
        "material_missing": [x for x, ok in {
            "저점위치": has_from_low,
            "고점여유": has_high_room,
            "VWAP": has_vwap,
            "EMA5": has_ema5,
            "1분흐름": has_change1,
            "3분흐름": has_change3,
            "1분돈": has_money1,
            "3분돈": has_money3,
        }.items() if not ok],
    }


def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    item = _v288_norm_row(row, market)
    ev = _v288_eval(item, market)
    action = str(ev.get("action") or "observe")
    ready = action == "paper_open"
    recheck = action == "recheck_wait"
    blocked = action == "block"
    reasons = ev.get("good") if ready else (ev.get("wait") if recheck else ev.get("hard"))
    label = "✅ 진입 가능" if ready else ("⚠️ 정보 재확인 중" if recheck else ("❌ 진입 금지" if blocked else "❔ 관찰"))
    nowv = now_ts()
    ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx.update({
        "brain_version": BOT_VERSION,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_rule_version": "v290_design_fixed_30_closed_no_missing_score",
        "take_profit_pct": V288_TAKE_PROFIT_PCT,
        "protect_trigger_pct": V288_PROTECT_TRIGGER_PCT,
        "protect_floor_pct": V288_PROTECT_FLOOR_PCT,
        "stop_loss_pct": V288_STOP_LOSS_PCT,
        "time_exit_min": V288_TIME_EXIT_MIN,
        "v288_eval": ev,
    })
    item.update({
        "schema": "sweep_vwap_recovery_v290",
        "brain_version": BOT_VERSION,
        "strategy": STRATEGY_LABEL,
        "strategy_name": STRATEGY_LABEL,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "paper_route": STRATEGY_KEY,
        "decision": action,
        "event_type": action,
        "candidate_mode": "single_strategy_paper" if ready else ("single_strategy_recheck" if recheck else "single_strategy_review"),
        "score": ev.get("score", 0.0),
        "sweep_vwap_score": ev.get("score", 0.0),
        "sweep_vwap_good": ev.get("good", []),
        "sweep_vwap_wait": ev.get("wait", []),
        "sweep_vwap_blocks": ev.get("hard", []),
        "quality_risk_tags": ev.get("hard", []) + ev.get("wait", []),
        "block_reasons": ev.get("hard", []),
        "hold_reason": ", ".join(reasons or []) or label,
        "paper_bot_open": ready,
        "open_eligible": ready,
        "paper_eligible": ready,
        "eligible_for_paper": ready,
        "trade_ready": ready,
        "review_only": not ready,
        "observe_only": not ready,
        "final_entry_action": "paper_open" if ready else ("recheck_wait" if recheck else "reject" if blocked else "observe"),
        "final_entry_label": label,
        "final_entry_reasons": reasons or [],
        "trade_ready_label": label if ready else "모의진입 보류",
        "trade_ready_reasons": reasons or [],
        "entry_context": ctx,
        "created_at": nowv,
        "source_created_at": nowv,
        "candidate_created_at": nowv,
        "expires_at": nowv + max(30.0, CANDIDATE_TTL_SEC),
        "one_liner": f"{label} / 점수 {fnum(ev.get('score'),0):.1f} / " + ", ".join((reasons or [])[:3]),
    })
    return item


def _v288_rank_key(row: Dict[str, Any]) -> Tuple[float, float, float, float]:
    return (
        fnum((row or {}).get("score"), 0),
        fnum((row or {}).get("turnover_3m"), 0),
        fnum((row or {}).get("turnover_1m"), 0),
        -fnum((row or {}).get("from_30m_low_pct"), 999),
    )


def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    market = _v274_market_context(rows if isinstance(rows, list) else [])
    ready_rows: List[Dict[str, Any]] = []
    recheck_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    rejects: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        t = _v274_ticker(raw)
        if not t or t in STABLE_EXCLUDED:
            continue
        try:
            item = _v288_candidate_row(raw, market)
        except Exception as exc:
            log_error("v288_candidate_row", exc)
            continue
        action = str(item.get("final_entry_action") or "")
        if action == "paper_open":
            ready_rows.append(item)
        elif action == "recheck_wait":
            recheck_rows.append(item)
        else:
            reason = (item.get("block_reasons") or item.get("final_entry_reasons") or ["관찰"])[0]
            rejects[str(reason)] += 1
            if len(examples) < 8:
                examples.append({"ticker": t, "reason": str(reason), "score": fnum(item.get("score"), 0), "line": item.get("one_liner", "")})
            review_rows.append(item)
    ready_rows.sort(key=_v288_rank_key, reverse=True)
    recheck_rows.sort(key=_v288_rank_key, reverse=True)
    review_rows.sort(key=_v288_rank_key, reverse=True)
    strict = [dict(x) for x in (ready_rows[:V288_TRADE_READY_MAX_ROWS] + recheck_rows)[:V288_STRICT_MAX_ROWS]]
    shadow = [dict(x) for x in review_rows[:V288_SHADOW_MAX_ROWS]]
    with _state_lock:
        STATE["v288_ready_rows"] = len(ready_rows)
        STATE["v288_recheck_rows"] = len(recheck_rows)
        STATE["v288_review_rows"] = len(review_rows)
        STATE["strict_decision"] = len(strict)
        STATE["shadow_decision"] = len(shadow)
        STATE["latest_trade_ready"] = len([r for r in strict if bool(r.get("trade_ready"))])
        STATE["trade_ready_written"] = STATE["latest_trade_ready"]
        STATE["phase_note"] = "v291: micro 긴급수집/target 연결 정상화 + 공장 저장 경량화. 조건값 변경 없음."
    return strict, shadow, rejects, examples


def _v288_write_micro_urgent(rows: List[Dict[str, Any]], reason: str = "v290_sweep_vwap_recheck") -> None:
    try:
        targets: List[str] = []
        meta: Dict[str, Any] = {}
        for r in rows or []:
            t = _v274_ticker(r)
            if not t or t in targets or t in STABLE_EXCLUDED:
                continue
            if str((r or {}).get("final_entry_action")) != "recheck_wait":
                continue
            targets.append(t)
            meta[t] = {"source": "v290_sweep_vwap", "score": fnum(r.get("score"), 0), "reason": ",".join(_v288_as_list(r.get("final_entry_reasons"))[:4])}
            if len(targets) >= 40:
                break
        if not targets:
            return
        save_json(FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"), {
            "version": BOT_VERSION,
            "schema": "v290_micro_urgent_targets",
            "updated_ts": now_ts(),
            "updated_text": now_text(),
            "ttl_sec": 35,
            "reason": reason,
            "targets": targets,
            "target_meta": meta,
            "note": "sweep_vwap_recovery 후보 중 micro/WS가 부족한 row를 즉시 버리지 않고 우선 재수집 대상으로 보냄",
        })
    except Exception as exc:
        log_error("v290_micro_urgent_write", exc)


def _v288_row_summary(r: Dict[str, Any]) -> str:
    t = _v274_ticker(r)
    return (
        f"- {t}: {r.get('final_entry_label','-')} / 점수 {fnum(r.get('score'),0):.1f} / "
        f"저점+{fnum(r.get('from_30m_low_pct'),0):.2f}% / 고점여유 {fnum(r.get('below_30m_high_pct'),0):.2f}% / "
        f"VWAP {fnum(r.get('vwap_gap_pct'),0):+.2f}% / 3분 {fnum(r.get('change_3'),0):+.2f}% / "
        f"3분돈 {krw_m(r.get('turnover_3m'))} / 사유 {r.get('hold_reason','-')}"
    )


def _v288_closed_rows(limit: int = 12000) -> List[Dict[str, Any]]:
    try:
        rows = load_closed(limit)
    except Exception:
        rows = []
    out = []
    for r in rows or []:
        ctx = r.get("entry_context") if isinstance(r.get("entry_context"), dict) else {}
        key = str(r.get("paper_strategy_key") or r.get("strategy_key") or r.get("route") or ctx.get("paper_strategy_key") or ctx.get("strategy_key") or ctx.get("route") or "")
        if key == STRATEGY_KEY:
            out.append(r)
    return out


def _v288_perf_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = []
    for r in rows or []:
        vals.append(fnum(r.get("net_pct") if r.get("net_pct") is not None else r.get("pnl_pct"), 0.0))
    n = len(vals); wins = sum(1 for v in vals if v > 0); total = sum(vals)
    return {"n": n, "wins": wins, "losses": n-wins, "wr": (wins/n*100 if n else 0.0), "total": total, "avg": (total/n if n else 0.0), "max": max(vals) if vals else 0.0, "min": min(vals) if vals else 0.0}


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:
    rows = rows if isinstance(rows, list) else _v274_read_material_rows(120)
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    shadow = [r for r in rows if str(r.get("final_entry_action")) not in {"paper_open", "recheck_wait"}]
    closed = _v288_closed_rows()
    st = _v288_perf_stats(closed)
    verdict = "❔ 30건 검증 전" if st["n"] < 30 else ("✅ 통과후보" if st["total"] > 0 and st["wr"] >= 45 else ("⚠️ 보정검토" if st["wr"] >= 40 and st["total"] > -1.0 else "❌ 폐기후보"))
    lines = [
        "📊 단일 전략 성과 /score",
        f"전략: {STRATEGY_LABEL} ({STRATEGY_KEY})",
        f"상태: {verdict} / CLOSED {st['n']}/30 / 조건변경 {'금지' if st['n'] < 30 else '판정가능'}",
        "",
        "[1] 설계 고정값",
        f"- 익절 +{V288_TAKE_PROFIT_PCT:.2f}% / 보호 +{V288_PROTECT_TRIGGER_PCT:.2f}% 이후 +{V288_PROTECT_FLOOR_PCT:.2f}% / 손절 {V288_STOP_LOSS_PCT:.2f}% / 시간 {V288_TIME_EXIT_MIN:.0f}분",
        "- micro/WS 부족: 즉시 탈락이 아니라 긴급수집 → 끝까지 안 붙으면 OPEN 보류",
        "",
        "[2] 현재 후보",
        f"- ✅ 진입 가능 {len(ready)}개 / ⚠️ 재확인 {len(recheck)}개 / ❌ 관찰·금지 {len(shadow)}개",
        *(_v288_row_summary(r) for r in ready[:5]),
        *( ["- 진입 가능 후보 없음"] if not ready else [] ),
        "",
        "[3] 재확인 대기 TOP",
        *(_v288_row_summary(r) for r in recheck[:5]),
        *( ["- 재확인 대기 없음"] if not recheck else [] ),
        "",
        "[4] CLOSED 성과",
        f"- {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['wr']:.1f}% / 순합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 최대 {st['max']:+.2f}% / 최소 {st['min']:+.2f}%",
        "",
        "[5] 30건 후 판정",
        "- 통과: 순수익 플러스, 승률 45% 이상, 무반응/시간청산 과다 아님",
        "- 보정: 본전 근처+공통 손실원인 1~2개일 때만 설계서 단위 묶음수정",
        "- 폐기: 명확한 마이너스, 승률 40% 미만, 손절 먼저가 압도적일 때",
    ]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:
    rows = _v274_read_material_rows(120)
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    blocked = [r for r in rows if str(r.get("final_entry_action")) not in {"paper_open", "recheck_wait"}]
    c = Counter()
    for r in blocked:
        for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
            c[str(x)] += 1
    lines = [
        "🔍 후보품질 /quality",
        f"전략: {STRATEGY_LABEL}",
        f"- 진입 가능 {len(ready)} / 재확인 {len(recheck)} / 관찰·금지 {len(blocked)}",
        "",
        "[진입 가능 TOP]",
        *(_v288_row_summary(r) for r in ready[:8]),
        *( ["- 없음"] if not ready else [] ),
        "",
        "[진입 금지/보류 사유 TOP]",
        *(f"- {k}: {v}" for k, v in c.most_common(8)),
        *( ["- 없음"] if not c else [] ),
    ]
    if full:
        lines += ["", "[재확인 TOP]", *(_v288_row_summary(r) for r in recheck[:20])]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:
    rows = _v274_read_material_rows(120)
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    lines = [
        "👀 전략 감시 /strategy_watch",
        f"전략: {STRATEGY_LABEL}",
        "- 7전략 관찰판은 v288 active path에서 제거됨",
        "- 현재는 단일 설계값으로만 paper 후보를 생성함",
        "",
        "[진입 가능]",
        *(_v288_row_summary(r) for r in ready[:10]),
        *( ["- 없음"] if not ready else [] ),
        "",
        "[정보 재확인]",
        *(_v288_row_summary(r) for r in recheck[:10]),
        *( ["- 없음"] if not recheck else [] ),
    ]
    return "\n".join(lines)


def _v288_health_text() -> str:
    try:
        res = _read_resource_status()
        if not isinstance(res, dict) or not res:
            res = server_resource_snapshot()
    except Exception:
        res = {}
    with _state_lock:
        s = dict(STATE)
    rows = _v274_read_material_rows(120)
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    p = paper_alive_summary(); pst = p.get("status") if isinstance(p.get("status"), dict) else {}
    stage_lines_raw = s.get("stage_times") if isinstance(s.get("stage_times"), list) else []
    simple_stages, slow_note = _v290_stage_line(stage_lines_raw)
    last_sec = fnum(s.get("scan_last_sec"), 0)
    if last_sec >= 20:
        bottleneck = f"❌ 스캔 {last_sec:.1f}초: 병목 확인 필요"
    elif last_sec >= 10:
        bottleneck = f"⚠️ 스캔 {last_sec:.1f}초: 느림"
    elif slow_note:
        bottleneck = " / ".join(slow_note[:3])
    else:
        bottleneck = "✅ 큰 병목 없음"
    disk_pct = fnum(res.get('disk_pct'), 0); disk_free = _fmt_bytes(fnum(res.get('disk_free'),0)); disk_total = _fmt_bytes(fnum(res.get('disk_total'),0))
    mem_pct = fnum(res.get('mem_pct'), 0); mem_avail = _fmt_bytes(fnum(res.get('mem_avail'),0)); mem_total = _fmt_bytes(fnum(res.get('mem_total'),0))
    proc = res.get('cpu_proc_pct') if isinstance(res.get('cpu_proc_pct'), dict) else {}
    rss = res.get('rss') if isinstance(res.get('rss'), dict) else {}
    running_txt = "ON" if p.get("alive") else "OFF"
    try:
        open_count = int(pst.get("open_count") if pst.get("open_count") is not None else len(read_open()))
    except Exception:
        open_count = "?"
    closed_count_v = pst.get("closed_count") or pst.get("closed_total")
    if closed_count_v is None:
        closed_count_v = line_count(FILES.get("paper_closed", BASE_DIR / "paper_bot_closed.jsonl"))
    paper_ver = pst.get("version") or "paper_bot"
    paper_state = p.get("label") or ("정상" if p.get("alive") else "중지")
    paper_age = _age_text(fnum(pst.get("updated_at"), 0)) if fnum(pst.get("updated_at"), 0) > 0 else "상태파일 대기"
    lines = [
        "🧭 건강상태 /health",
        "- CPU·메모리·디스크 남은 용량은 항상 표시합니다.",
        "",
        "[1/6] 메인봇",
        f"✅ {BOT_VERSION} / 전략 {STRATEGY_LABEL} / 최근완료 {last_sec:.1f}초 / 후보 {len(rows)}개 / 진입가능 {len(ready)} / 재확인 {len(recheck)}",
        f"- 오류: {'❌ ' + str(s.get('scan_last_error')) if s.get('scan_last_error') else '✅ 새 실행 중 오류 없음'}",
        f"- 병목: {bottleneck}",
        "",
        "[2/6] 전략흐름",
        "- 허브: 전체시장 수집 → 직원: 정밀/VWAP/micro 보강 → 공장: sweep_vwap_recovery 단일 후보 저장",
        "- 단계: " + (" / ".join(simple_stages) if simple_stages else "아직 단계요약 없음"),
        "",
        "[3/6] 서버자원",
        f"{'✅' if disk_pct < 80 else '⚠️'} 디스크: 사용 {disk_pct:.1f}% / 남음 {disk_free} / 전체 {disk_total}",
        f"{'✅' if mem_pct < 80 else ('⚠️' if mem_pct < 90 else '❌')} 메모리: 사용 {mem_pct:.1f}% / 남음 {mem_avail} / 전체 {mem_total}",
        f"{'✅' if fnum(res.get('cpu_total_pct'),0) < 75 else '⚠️'} CPU: 전체 {fnum(res.get('cpu_total_pct'),0):.1f}% / main {fnum(proc.get('main'),0):.1f}% / paper {fnum(proc.get('paper'),0):.1f}% / WS {fnum(proc.get('ws'),0):.1f}% / micro {fnum(proc.get('micro'),0):.1f}%",
        f"{'✅' if fnum(res.get('load1'),0) < 2.5 else '⚠️'} load: {fnum(res.get('load1'),0):.2f} / {fnum(res.get('load5'),0):.2f} / {fnum(res.get('load15'),0):.2f} / RSS main {_fmt_bytes(fnum(rss.get('main'),0))} / paper {_fmt_bytes(fnum(rss.get('paper'),0))}",
        "",
        "[4/6] 후보",
        f"- ✅ 진입 가능 {len(ready)} / ⚠️ 정보 재확인 {len(recheck)} / ❌ 관찰·금지 {max(0, len(rows)-len(ready)-len(recheck))}",
        "",
        "[5/6] paper_bot",
        f"{p.get('icon','❔')} {paper_ver} / 실행 {running_txt} / {paper_state} / OPEN {open_count} / CLOSED {closed_count_v} / 상태 {paper_age} 전",
        "",
        "[6/6] 고정 원칙",
        "- 자동매수 OFF / paper 검증 전용 / CLOSED 30건 전 조건 변경 금지",
    ]
    return "\n".join(lines)


def _v288_cache_payload(text: str, name: str) -> Dict[str, Any]:
    return {"version": BOT_VERSION, "name": name, "updated_ts": now_ts(), "updated_text": now_text(), "text": str(text or "")}


def _read_cached_text(path: Path, title: str) -> str:  # type: ignore[override]
    obj = load_json(path, {})
    if isinstance(obj, dict) and obj.get("text"):
        cached_ver = str(obj.get("version") or "")
        text = str(obj.get("text") or "")
        if cached_ver and cached_ver != BOT_VERSION:
            return "\n".join([f"⚠️ {title} 새 캐시 갱신중 · 이전 캐시 표시", f"- 현재 실행버전: {BOT_VERSION}", f"- 표시 중인 캐시버전: {cached_ver}", "", text])
        return text
    return f"❔ {title} 캐시 준비중\n- 기준: {BOT_VERSION}\n- 기본 명령어는 직접 계산하지 않고 저장된 캐시만 보여줍니다."


def _v288_write_command_caches(reason: str = "manual") -> None:
    rows = _v274_read_material_rows(120)
    save_json(FILES.get("score_summary", BASE_DIR / "clean_score_summary.json"), _v288_cache_payload(_v288_score_text(rows), "score"))
    save_json(FILES.get("quality_summary", BASE_DIR / "clean_quality_summary.json"), _v288_cache_payload(_v288_quality_text(False), "quality"))
    save_json(FILES.get("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json"), _v288_cache_payload(_v288_strategy_watch_text(False), "strategy_watch"))
    save_json(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), _v288_cache_payload(_v288_health_text(), "health"))


def _v287_write_snapshot_command_caches(payload: Optional[Dict[str, Any]] = None, reason: str = "manual") -> None:  # compatibility for older cache worker hooks
    _v288_write_command_caches(reason=reason)


def _build_quality_cache() -> None:  # type: ignore[override]
    _v288_write_command_caches(reason="quality_cache")


def _build_light_command_caches() -> None:  # type: ignore[override]
    try:
        _write_resource_status()
    except Exception as exc:
        log_error("v288_resource_cache", exc)
    _v288_write_command_caches(reason="command_worker")


def _v219_write_command_caches_after_snapshot(build_quality: bool = False) -> None:  # type: ignore[override]
    _v288_write_command_caches(reason="post_refresh")


def score_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES.get("score_summary", BASE_DIR / "clean_score_summary.json"), "/score")


def health_text() -> str:  # type: ignore[override]
    return _read_cached_text(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), "/health")


def candidate_quality_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v288_quality_text(True)
    return _read_cached_text(FILES.get("quality_summary", BASE_DIR / "clean_quality_summary.json"), "/quality")


def strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    if full:
        return _v288_strategy_watch_text(True)
    return _read_cached_text(FILES.get("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json"), "/strategy_watch")


def _v274_read_material_rows(limit: int = 120) -> List[Dict[str, Any]]:  # type: ignore[override]
    snap = load_json(FILES.get("strategy_material_snapshot", BASE_DIR / "strategy_material_snapshot.json"), {})
    rows = snap.get("rows") if isinstance(snap, dict) and isinstance(snap.get("rows"), list) else []
    return [r for r in rows[:limit] if isinstance(r, dict)]


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Counter) -> None:
    rows = list(strict_rows or []) + list(shadow_rows or [])
    nowv = now_ts()
    payload = {
        "version": BOT_VERSION,
        "schema": "sweep_vwap_recovery_snapshot_v290",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": len([r for r in strict_rows or [] if bool(r.get("trade_ready"))]),
        "recheck_count": len([r for r in strict_rows or [] if str(r.get("final_entry_action")) == "recheck_wait"]),
        "market_context": market,
        "rejects": dict(rejects or {}),
        "rows": rows[:V288_MATERIAL_MAX_ROWS],
        "note": "v290: 7전략 제거 후 sweep_vwap_recovery 단일전략 paper 검증. 결측값 고득점/재확인 과다 분류 방지.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    save_json(FILES["strategy_lab_summary"], {"version": BOT_VERSION, "updated_ts": nowv, "updated_text": now_text(nowv), "strategy_key": STRATEGY_KEY, "strategy_label": STRATEGY_LABEL, "summary": payload})
    write_jsonl_replace(FILES["strategy_material_latest"], rows[:V288_MATERIAL_MAX_ROWS])
    _v288_write_command_caches(reason="strategy_files")


def _v288_refresh_latest_row(row: Dict[str, Any], lane: str, scan_id: str) -> Dict[str, Any]:
    nowv = now_ts()
    rr = dict(row or {})
    ready = bool(rr.get("trade_ready") or rr.get("paper_bot_open") or rr.get("open_eligible"))
    ctx = rr.get("entry_context") if isinstance(rr.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx.update({"brain_version": BOT_VERSION, "scan_id": scan_id, "paper_strategy_key": STRATEGY_KEY, "paper_strategy_label": STRATEGY_LABEL, "trade_ready": ready})
    rr.update({
        "brain_version": BOT_VERSION,
        "schema": "sweep_vwap_recovery_latest_v290",
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "paper_route": STRATEGY_KEY,
        "strategy": STRATEGY_LABEL,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "lane": lane,
        "created_at": nowv,
        "source_created_at": nowv,
        "factory_saved_at": nowv,
        "expires_at": nowv + max(30.0, CANDIDATE_TTL_SEC),
        "scan_id": rr.get("scan_id") or scan_id,
        "snapshot_id": rr.get("snapshot_id") or rr.get("scan_id") or scan_id,
        "candidate_created_at": rr.get("candidate_created_at") or nowv,
        "entry_context": ctx,
    })
    return rr


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_rows = [_v288_refresh_latest_row(r, "strict", scan_id) for r in (strict or [])]
    shadow_rows = [_v288_refresh_latest_row(r, "shadow", scan_id) for r in (shadow or [])]
    _v288_write_micro_urgent(strict_rows, reason="v290_sweep_vwap_recheck")
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        rejects = Counter()
        for r in shadow_rows:
            for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons")):
                rejects[str(x)] += 1
        _v288_write_strategy_files(strict_rows, shadow_rows, market, rejects)
    except Exception as exc:
        log_error("v288_strategy_snapshot_write", exc)
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["factory_mode"] = "v290_sweep_vwap_recovery_factory"
        STATE["write_error"] = write_error
    return {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
        "paper_written": ready_n,
        "shadow_written": len(shadow_rows),
        "trade_ready_written": ready_n,
        "strict_observe_written": max(0, len(strict_rows)-ready_n),
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_strict_observe": max(0, len(strict_rows)-ready_n),
        "latest_final_recheck_wait": recheck_n,
        "latest_final_observe": max(0, len(strict_rows)-ready_n),
        "dup_skip_reason": {},
        "data_quality_note": "v290_sweep_vwap_recovery",
        "dup_skip": 0,
        "write_error": write_error,
        "last_ticker": str((strict_rows or shadow_rows or [{"ticker":"-"}])[-1].get("ticker") or "-"),
        "factory_mode": "v290_sweep_vwap_recovery_factory",
        "archive_deferred": 0,
        "risk_sync_deferred": True,
    }


def apply_final_entry_worker(strict: List[Dict[str, Any]], rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:  # type: ignore[override]
    paper_open = len([r for r in strict or [] if bool((r or {}).get("paper_bot_open") or (r or {}).get("trade_ready") or (r or {}).get("open_eligible"))])
    recheck = len([r for r in strict or [] if str((r or {}).get("final_entry_action")) == "recheck_wait"])
    return {"paper_open": paper_open, "recheck_wait": recheck, "observe": max(0, len(strict or [])-paper_open-recheck)}


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    def key(r: Dict[str, Any]) -> Tuple[float, float, float, float]:
        from_low = fnum(r.get("from_30m_low_pct") or r.get("from_low_pct"), 999)
        high_room = fnum(r.get("below_30m_high_pct") or r.get("high_gap_pct"), 0)
        near_score = max(0.0, 3.0 - abs(from_low - 1.0))
        return (near_score, fnum(r.get("turnover_3m") or r.get("turnover_24h"), 0), high_room, abs(fnum(r.get("change_3"), 0)))
    arr = [r for r in rows or [] if isinstance(r, dict) and _v274_ticker(r) not in STABLE_EXCLUDED]
    arr.sort(key=key, reverse=True)
    limit = max(PRECISION_REFRESH_MIN, min(PRECISION_REFRESH_MAX_SAFETY, PRECISION_REFRESH_BASE + int(len(arr) * 0.04)))
    with _state_lock:
        STATE["precision_target_note"] = f"v290 sweep/vwap selector {min(len(arr), limit)}/{len(arr)}"
        STATE["precision_target_source"] = "v290_sweep_vwap_selector"
    return arr[:limit]


def _v279_target_tickers(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, limit: int = 180) -> List[str]:
    seen: set = set(); out: List[str] = []
    for src in (priority_rows or []):
        _push_unique_ticker(out, seen, (src or {}).get("ticker") or (src or {}).get("market") or (src or {}).get("symbol"))
        if len(out) >= limit:
            return out[:limit]
    ranked = sorted([r for r in (rows or []) if isinstance(r, dict)], key=lambda r: (bool(r.get("trade_ready")), str(r.get("final_entry_action")) == "recheck_wait", fnum(r.get("score"), 0), fnum(r.get("turnover_3m"), 0)), reverse=True)
    for r in ranked:
        _push_unique_ticker(out, seen, r.get("ticker") or r.get("market") or r.get("symbol"))
        if len(out) >= limit:
            break
    return out[:limit]


def _v279_write_target_file(path: Path, tickers: List[str], reason: str) -> None:
    nowv = now_ts()
    payload = {"version": BOT_VERSION, "schema": "v290_sweep_vwap_targets", "updated_ts": nowv, "updated_text": now_text(nowv), "reason": reason, "targets": tickers, "target_count": len(tickers), "target_meta": {t: {"reason": reason, "source": STRATEGY_KEY} for t in tickers}}
    atomic_write(path, payload)


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "v290_sweep_vwap") -> None:  # type: ignore[override]
    tickers = _v279_target_tickers(rows, priority_rows, limit=180)
    with _ws_lock:
        _ws_targets[:] = tickers
    _v279_write_target_file(FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"), tickers, reason)
    with _state_lock:
        STATE["ws_targets"] = len(tickers); STATE["ws_target_file_written"] = 1; STATE["ws_target_file_targets"] = len(tickers); STATE["ws_target_reason"] = reason; STATE["ws_target_write_note"] = "v290 sweep/vwap target writer"


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "v290_sweep_vwap") -> None:  # type: ignore[override]
    tickers = _v279_target_tickers(rows, priority_rows, limit=180)
    with _micro_lock:
        _micro_targets[:] = tickers
    _v279_write_target_file(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), tickers, reason)
    with _state_lock:
        STATE["micro_targets"] = len(tickers); STATE["micro_target_file_written"] = 1; STATE["micro_target_file_targets"] = len(tickers); STATE["micro_target_reason"] = reason; STATE["micro_target_write_note"] = "v290 sweep/vwap target writer"



def _v290_stage_push(stage_times: List[Tuple[str, float, str]], name: str, sec: float, note: str = "") -> None:
    """v290 scan 단계기록 단일 helper.

    v288에서 삭제된 구 helper(_v278_stage_push)를 scan_once가 계속 호출해
    스캔이 허브 단계에서 NameError로 중단됐다. 구 함수명을 되살리지 않고,
    현재 sweep_vwap 본선 안에서 쓰는 단계기록 helper로 단일화한다.
    """
    try:
        stage_times.append((str(name), round(float(sec or 0.0), 3), str(note or "")))
    except Exception:
        stage_times.append((str(name), 0.0, str(note or "")))

def scan_once() -> List[Dict[str, Any]]:  # type: ignore[override]
    started = now_ts()
    ensure_eval_baseline()
    with _state_lock:
        STATE["scan_calls"] = int(STATE.get("scan_calls", 0)) + 1
        STATE["scan_seq"] = int(STATE.get("scan_seq", 0)) + 1
        STATE["scan_id"] = f"scan-{int(started)}-{int(STATE.get('scan_seq', 0))}"
        STATE["scan_started_at"] = started
        STATE["scan_running"] = True
        STATE["scan_display_note"] = "v291 sweep/vwap 스캔 진행중"
        STATE["scan_last_stage"] = "hub_bulk"
        STATE["stage_times"] = []
    stage_times: List[Tuple[str, float, str]] = []
    try:
        st = now_ts()
        rows, source = fetch_all_krw()
        rows = apply_ws_cache_to_rows(rows)
        rows = apply_micro_cache_to_rows(rows)
        _v290_stage_push(stage_times, "1) 허브", now_ts() - st, f"rows {len(rows)} / {source} / WS fresh {STATE.get('ws_fresh',0)} / micro fresh {STATE.get('micro_fresh',0)}")
        with _state_lock:
            STATE["bulk_rows"] = len(rows); STATE["bulk_price"] = sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0); STATE["bulk_money"] = sum(1 for r in rows if fnum(r.get("turnover_24h"), 0) > 0); STATE["scan_last_stage"] = "worker_precision_sweep_vwap"
        if _stop_event.is_set(): return []

        st = now_ts()
        targets = select_precision_targets(rows)
        p_ok, p_fail, p_have = refresh_precision(rows, targets)
        _v290_stage_push(stage_times, "2) 직원: 정밀/VWAP 보강", now_ts() - st, f"targets {len(targets)} / 직접 {p_ok} / cached {p_have} / fail {p_fail}")
        with _state_lock:
            STATE["precision_selected"] = len(targets); STATE["precision_refreshed"] = p_ok; STATE["precision_failed"] = p_fail; STATE["precision_have"] = p_have; STATE["scan_last_stage"] = "worker_merge"
        if _stop_event.is_set(): return []

        st = now_ts()
        rows = merge_precision(rows)
        cov = update_field_coverage(rows)
        _v290_stage_push(stage_times, "3) 직원: 표준값 병합", now_ts() - st, f"precision {cov.get('precision')} / bulk-only {cov.get('bulk_only')}")
        with _state_lock:
            STATE["field_coverage"] = cov; STATE["scan_last_stage"] = "sweep_vwap_build"
        if _stop_event.is_set(): return []

        st = now_ts()
        strict, shadow, rejects, examples = build_candidates(rows)
        ready_n = len([r for r in strict if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
        recheck_n = len([r for r in strict if str(r.get("final_entry_action")) == "recheck_wait"])
        _v290_stage_push(stage_times, "4) 전략: sweep/vwap 판정", now_ts() - st, f"진입 {ready_n} / 재확인 {recheck_n} / shadow {len(shadow)} / reject {sum(rejects.values())}")
        with _state_lock:
            STATE["strict_decision"] = len(strict); STATE["shadow_decision"] = len(shadow); STATE["reject_counts"] = dict(rejects); STATE["reject_examples"] = examples; STATE["latest_trade_ready"] = ready_n; STATE["trade_ready_written"] = ready_n; STATE["scan_last_stage"] = "factory_export"
        if _stop_event.is_set(): return []

        st = now_ts()
        pipe = export_candidates(strict, shadow)
        _v290_stage_push(stage_times, "5) 공장: latest 저장", now_ts() - st, f"latest {pipe.get('paper_latest_written',0)} / OPEN {pipe.get('latest_trade_ready',0)} / 재확인 {pipe.get('latest_final_recheck_wait',0)}")
        if _stop_event.is_set(): return []

        st = now_ts()
        snap_note = v212_publish_final_candidate_state(strict, shadow, rows, pipe)
        _v290_stage_push(stage_times, "6) 공장: snapshot/target", now_ts() - st, snap_note)
        total = now_ts() - started
        with _state_lock:
            STATE["paper_written"] = pipe.get("paper_written", 0); STATE["trade_ready_written"] = pipe.get("trade_ready_written", 0); STATE["strict_observe_written"] = pipe.get("strict_observe_written", 0); STATE["shadow_written"] = len(shadow); STATE["paper_latest_written"] = pipe.get("paper_latest_written", 0); STATE["shadow_latest_written"] = pipe.get("shadow_latest_written", 0); STATE["latest_trade_ready"] = pipe.get("latest_trade_ready", 0); STATE["latest_strict_observe"] = pipe.get("latest_strict_observe", 0); STATE["latest_final_recheck_wait"] = pipe.get("latest_final_recheck_wait", 0); STATE["last_scan_candidates"] = len(strict); STATE["scan_last_sec"] = total; STATE["scan_max_sec"] = max(float(STATE.get("scan_max_sec", 0.0)), total); STATE["scan_completed_at"] = now_ts(); STATE["scan_running"] = False; STATE["scan_last_stage"] = "done"; STATE["scan_display_note"] = "v291 sweep/vwap 완료"; STATE["stage_times"] = [(a, round(float(b), 3), c) for a, b, c in stage_times]; STATE["factory_mode"] = pipe.get("factory_mode")
        return strict
    except Exception as exc:
        log_error("scan_once_v291", exc)
        with _state_lock:
            STATE["scan_running"] = False; STATE["scan_last_stage"] = "error"; STATE["scan_last_error"] = str(exc)[:300]; STATE["scan_completed_at"] = now_ts()
        return []


def reversion_review_text(full: bool = False) -> str:  # type: ignore[override]
    return "\n".join([
        "🧯 전략 복기 /reversion_review",
        f"현재전략: {STRATEGY_LABEL}",
        "- v288~v290에서는 7전략 실험판을 active path에서 제거했습니다.",
        "- CLOSED 30건 전까지 조건 변경 금지, 오류/배관/장부 문제만 수정합니다.",
        "- 판단은 /score, 후보품질은 /quality, 현재 후보는 /strategy_watch에서 봅니다.",
    ])

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v291: micro 긴급수집/target 연결 정상화 + 공장 저장 경량화. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception:
    pass


# ===============================
# v2.13.291: micro urgent/target 배관 수술 + 공장 저장 경량화
# - 조건값/익절/손절/시간청산은 v288 설계 고정값 그대로 유지한다.
# - v290에서 shadow 관찰 후보에 micro긴급수집대기가 있어도 urgent/target으로 못 넘어가던 연결을 복구한다.
# - 결측값/조건부족 후보는 OPEN하지 않되, 정보수집이 필요한 후보는 sidecar target에 올린다.
# - 공장 저장은 rows 중복 저장을 줄이고 command cache는 유지한다.
# ===============================

V291_VERSION_NOTE = "v291: micro urgent/target 연결 + snapshot/공장 저장 경량화"


def _v291_reason_list(row: Dict[str, Any]) -> List[str]:
    vals: List[str] = []
    for key in ("final_entry_reasons", "quality_risk_tags", "sweep_vwap_wait", "sweep_vwap_blocks", "block_reasons"):
        vals.extend(_v288_as_list((row or {}).get(key)))
    ev = (row or {}).get("entry_context") if isinstance((row or {}).get("entry_context"), dict) else {}
    ve = ev.get("v288_eval") if isinstance(ev.get("v288_eval"), dict) else {}
    vals.extend(_v288_as_list(ve.get("wait")))
    vals.extend(_v288_as_list(ve.get("hard")))
    out: List[str] = []
    for v in vals:
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out


def _v291_has_micro_wait(row: Dict[str, Any]) -> bool:
    return any(str(x).startswith("micro긴급수집대기") for x in _v291_reason_list(row))


def _v291_has_ws_wait(row: Dict[str, Any]) -> bool:
    return any(str(x).startswith("WS재확인") for x in _v291_reason_list(row))


def _v291_external_wait_row(row: Dict[str, Any]) -> bool:
    """외부정보 수집 대상인지 판단한다.

    이 함수는 매수조건이 아니다. v290에서는 wait 후보 중 recheck_wait만 urgent 파일에
    쓰였고, 조건부족 observe 후보는 micro target에 올라가지 않아
    /quality의 micro긴급수집대기와 /health target 수가 어긋났다.
    여기서는 OPEN은 막되, 정보가 필요한 후보를 sidecar가 볼 수 있게 target에만 올린다.
    """
    if not isinstance(row, dict):
        return False
    t = _v274_ticker(row)
    if not t or t in STABLE_EXCLUDED:
        return False
    # 대형주는 시장 참고용. 긴급수집 우선순위에서는 제외한다.
    if t in MAJOR_WATCH_TICKERS:
        return False
    action = str(row.get("final_entry_action") or "")
    if action == "paper_open":
        return True
    reasons = _v291_reason_list(row)
    if action == "recheck_wait":
        return True
    if _v291_has_micro_wait(row) or _v291_has_ws_wait(row):
        # 가격 없는 행이나 전략 기본재료가 거의 없는 행은 micro/WS 수집만으로 해결되지 않는다.
        # OPEN 조건은 그대로 두고, sidecar 긴급수집은 어느 정도 재료점수가 있는 후보로 제한한다.
        if "가격없음" in reasons:
            return False
        if fnum(row.get("score"), 0.0) < 5.5:
            return False
        return True
    return False


def _v291_urgent_priority_rows(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], limit: int = 80) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    base = list(strict_rows or []) + list(shadow_rows or [])
    def key(r: Dict[str, Any]) -> Tuple[float, float, float, float]:
        action_rank = 3 if str((r or {}).get("final_entry_action")) == "paper_open" else (2 if str((r or {}).get("final_entry_action")) == "recheck_wait" else (1 if _v291_external_wait_row(r) else 0))
        return (action_rank, fnum((r or {}).get("score"), 0), fnum((r or {}).get("turnover_3m"), 0), fnum((r or {}).get("turnover_24h"), 0))
    for r in sorted([x for x in base if isinstance(x, dict) and _v291_external_wait_row(x)], key=key, reverse=True):
        t = _v274_ticker(r)
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _v291_norm_should_overlay(item: Dict[str, Any], prefix: str) -> bool:
    # scan 초반 apply_ws_cache/apply_micro_cache가 이미 붙인 값을 반복 조회하지 않는다.
    st = str((item or {}).get(f"{prefix}_row_status") or "")
    if st in {"fresh", "stale"} or bool((item or {}).get(f"{prefix}_fresh")):
        return False
    age = fnum((item or {}).get(f"{prefix}_age_sec"), -1)
    if age >= 0:
        return False
    return True


def _v288_norm_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v291: scan row에 이미 붙은 WS/micro overlay를 재조회하지 않는다.

    조건값은 그대로다. v290 전략판정 7~8초 병목의 일부가 후보별 ws_snapshot/micro_snapshot
    반복 overlay였기 때문에, apply_ws_cache_to_rows/apply_micro_cache_to_rows 결과가 없을 때만 보조 조회한다.
    """
    raw = dict(row if isinstance(row, dict) else {})
    item = dict(raw)
    t = _v274_ticker(item)
    price = fnum(_v288_pick(item, "current_price", "trade_price", "price", default=0), 0)
    has_from_low = _v290_has_value(raw, "from_30m_low_pct", "from_low_pct")
    has_high_room = _v290_has_value(raw, "below_30m_high_pct", "high_gap_pct")
    has_vwap = _v290_has_value(raw, "vwap_gap_pct")
    has_ema5 = _v290_has_value(raw, "ema5_gap_pct")
    has_change1 = _v290_has_value(raw, "change_1", "rate_1m", "change_rate_1m")
    has_change3 = _v290_has_value(raw, "change_3", "rate_3m", "change_rate_3m")
    has_money1 = _v290_has_value(raw, "turnover_1m", "money_flow_1m")
    has_money3 = _v290_has_value(raw, "turnover_3m", "money_flow_3m")
    item.update({
        "ticker": t,
        "current_price": price,
        "entry_price": price,
        "detected_price": price,
        "change_1": fnum(_v288_pick(item, "change_1", "rate_1m", "change_rate_1m", default=0), 0),
        "change_3": fnum(_v288_pick(item, "change_3", "rate_3m", "change_rate_3m", default=0), 0),
        "change_5": fnum(_v288_pick(item, "change_5", "rate_5m", "change_rate_5m", default=0), 0),
        "turnover_1m": fnum(_v288_pick(item, "turnover_1m", "money_flow_1m", default=0), 0),
        "turnover_3m": fnum(_v288_pick(item, "turnover_3m", "money_flow_3m", default=0), 0),
        "turnover_5m": fnum(_v288_pick(item, "turnover_5m", "money_flow_5m", default=0), 0),
        "turnover_24h": fnum(_v288_pick(item, "turnover_24h", "acc_trade_value_24h", "trade_value_24h", default=0), 0),
        "vwap_gap_pct": fnum(_v288_pick(item, "vwap_gap_pct", default=0), 0),
        "ema5_gap_pct": fnum(_v288_pick(item, "ema5_gap_pct", default=0), 0),
        "from_30m_low_pct": fnum(_v288_pick(item, "from_30m_low_pct", "from_low_pct", default=0), 0),
        "below_30m_high_pct": fnum(_v288_pick(item, "below_30m_high_pct", "high_gap_pct", default=999), 999),
        "bb_lower_gap_pct": fnum(_v288_pick(item, "bb_lower_gap_pct", default=0), 0),
        "current_lower_wick_pct": fnum(_v288_pick(item, "current_lower_wick_pct", "lower_wick_pct", default=0), 0),
        "current_upper_wick_pct": fnum(_v288_pick(item, "current_upper_wick_pct", "upper_wick", default=0), 0),
        "low_defense_pct": fnum(_v288_pick(item, "low_defense_pct", default=0), 0),
        "market_context": market if isinstance(market, dict) else {},
        "v290_has_from_low": has_from_low,
        "v290_has_high_room": has_high_room,
        "v290_has_vwap": has_vwap,
        "v290_has_ema5": has_ema5,
        "v290_has_change1": has_change1,
        "v290_has_change3": has_change3,
        "v290_has_money1": has_money1,
        "v290_has_money3": has_money3,
    })
    if t:
        if _v291_norm_should_overlay(item, "ws"):
            try:
                ws = ws_snapshot(t)
                if str(ws.get("ws_row_status") or "") != "missing":
                    item.update(ws)
            except Exception as exc:
                log_error("v291_ws_overlay", exc)
        if _v291_norm_should_overlay(item, "micro"):
            try:
                ms = micro_snapshot(t)
                if str(ms.get("micro_row_status") or "") != "missing":
                    item.update(ms)
            except Exception as exc:
                log_error("v291_micro_overlay", exc)
    return item


def _v288_write_micro_urgent(rows: List[Dict[str, Any]], reason: str = "v291_sweep_vwap_external_wait") -> None:  # type: ignore[override]
    try:
        selected = _v291_urgent_priority_rows(list(rows or []), [], limit=40)
        targets: List[str] = []
        meta: Dict[str, Any] = {}
        nowv = now_ts()
        for r in selected:
            t = _v274_ticker(r)
            if not t or t in targets or t in STABLE_EXCLUDED:
                continue
            targets.append(t)
            reasons = _v291_reason_list(r)
            meta[t] = {
                "source": "v291_sweep_vwap",
                "score": fnum(r.get("score"), 0),
                "action": str(r.get("final_entry_action") or ""),
                "reason": ",".join(reasons[:5]),
                "status": str(r.get("micro_row_status") or "missing"),
                "age_sec": fnum(r.get("micro_age_sec"), -1),
            }
        payload = {
            "version": BOT_VERSION,
            "schema": "v291_micro_urgent_targets",
            "updated_ts": nowv,
            "updated_text": now_text(nowv),
            "ttl_sec": 35,
            "reason": reason,
            "targets": targets,
            "target_count": len(targets),
            "target_meta": meta,
            "note": "v291: OPEN 조건은 유지하되, micro/WS 정보가 필요한 관찰 후보도 sidecar 긴급수집 대상으로 보냄",
        }
        save_json(FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"), payload)
        with _state_lock:
            STATE["v291_micro_urgent_targets"] = len(targets)
            STATE["v291_micro_urgent_written"] = nowv
            STATE["v221_micro_urgent_count"] = len(targets)
            STATE["v221_micro_urgent_written"] = nowv
    except Exception as exc:
        log_error("v291_micro_urgent_write", exc)


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Counter) -> None:  # type: ignore[override]
    rows = list(strict_rows or []) + list(shadow_rows or [])
    nowv = now_ts()
    urgent_rows = _v291_urgent_priority_rows(strict_rows or [], shadow_rows or [], limit=80)
    payload = {
        "version": BOT_VERSION,
        "schema": "sweep_vwap_recovery_snapshot_v291",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": len([r for r in strict_rows or [] if bool(r.get("trade_ready"))]),
        "recheck_count": len([r for r in strict_rows or [] if str(r.get("final_entry_action")) == "recheck_wait"]),
        "urgent_watch_count": len(urgent_rows),
        "market_context": market,
        "rejects": dict(rejects or {}),
        "rows": rows[:V288_MATERIAL_MAX_ROWS],
        "note": "v291: 7전략 제거 후 sweep_vwap_recovery 단일전략. urgent/target 연결 보강, 조건값 변경 없음.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    save_json(FILES["strategy_lab_summary"], {
        "version": BOT_VERSION,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": payload["strict_count"],
        "shadow_count": payload["shadow_count"],
        "trade_ready_count": payload["trade_ready_count"],
        "recheck_count": payload["recheck_count"],
        "urgent_watch_count": payload["urgent_watch_count"],
        "rejects": dict(rejects or {}),
        "note": payload["note"],
    })
    write_jsonl_replace(FILES["strategy_material_latest"], rows[:V288_MATERIAL_MAX_ROWS])
    _v288_write_command_caches(reason="strategy_files_v291")


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_rows = [_v288_refresh_latest_row(r, "strict", scan_id) for r in (strict or [])]
    shadow_rows = [_v288_refresh_latest_row(r, "shadow", scan_id) for r in (shadow or [])]
    urgent_priority = _v291_urgent_priority_rows(strict_rows, shadow_rows, limit=80)
    _v288_write_micro_urgent(urgent_priority, reason="v291_sweep_vwap_external_wait")
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        rejects = Counter()
        for r in shadow_rows:
            for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
                rejects[str(x)] += 1
        _v288_write_strategy_files(strict_rows, shadow_rows, market, rejects)
    except Exception as exc:
        log_error("v291_strategy_snapshot_write", exc)
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v291_urgent_watch_rows"] = len(urgent_priority)
        STATE["factory_mode"] = "v291_sweep_vwap_recovery_factory"
        STATE["write_error"] = write_error
    return {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
        "paper_written": ready_n,
        "shadow_written": len(shadow_rows),
        "trade_ready_written": ready_n,
        "strict_observe_written": max(0, len(strict_rows)-ready_n),
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_strict_observe": max(0, len(strict_rows)-ready_n),
        "latest_final_recheck_wait": recheck_n,
        "latest_final_observe": max(0, len(strict_rows)-ready_n),
        "urgent_watch_rows": len(urgent_priority),
        "dup_skip_reason": {},
        "data_quality_note": "v291_sweep_vwap_recovery",
        "dup_skip": 0,
        "write_error": write_error,
        "last_ticker": str((strict_rows or shadow_rows or [{"ticker":"-"}])[-1].get("ticker") or "-"),
        "factory_mode": "v291_sweep_vwap_recovery_factory",
        "archive_deferred": 0,
        "risk_sync_deferred": True,
    }


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v291: shadow의 micro/WS 필요 후보도 target/snapshot 우선순위에 포함한다.

    paper_latest가 0이어도 shadow에 micro긴급수집대기가 있으면 target 0/0으로 보이지 않게 한다.
    OPEN 조건은 바꾸지 않고, sidecar 수집 배관만 복구한다.
    """
    strict_rows = [r for r in (strict or []) if isinstance(r, dict)]
    shadow_rows = [r for r in (shadow or []) if isinstance(r, dict)]
    if not strict_rows and not shadow_rows:
        strict_rows, shadow_rows = _v212_rows_from_latest()
    urgent_rows = _v291_urgent_priority_rows(strict_rows, shadow_rows, limit=80)
    priority_rows = list(strict_rows or [])
    seen = {_v274_ticker(r) for r in priority_rows}
    for r in urgent_rows:
        t = _v274_ticker(r)
        if t and t not in seen:
            priority_rows.append(r)
            seen.add(t)
    update_ws_targets(market_rows or [], priority_rows=priority_rows, reason="factory_final_candidates_v291")
    update_micro_targets(market_rows or [], priority_rows=priority_rows, reason="factory_final_candidates_v291")
    wait_sec = max(0.0, min(float(os.getenv("CLEAN_V232_SNAPSHOT_WAIT_SEC", os.getenv("CLEAN_V219_SNAPSHOT_WAIT_SEC", "0.2"))), 0.8))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    final = _v212_write_candidate_snapshot(priority_rows, shadow_rows, stage="final_after_target_overlay_v291", source="factory_memory_rows_v291", wait_sec=wait_sec)
    scan_id = str(final.get("scan_id") or STATE.get("scan_id") or "") if isinstance(final, dict) else str(STATE.get("scan_id") or "")
    _v219_schedule_external_refresh(scan_id)
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    with _state_lock:
        STATE["v291_snapshot_priority_rows"] = len(priority_rows)
        STATE["v291_urgent_watch_rows"] = len(urgent_rows)
        STATE["v233_snapshot_memory_rows"] = True
        STATE["v233_snapshot_initial_wait_sec"] = wait_sec
    return (
        f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / "
        f"micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / targets WS {STATE.get('ws_target_file_targets',0)} / "
        f"micro {STATE.get('micro_target_file_targets',0)} / urgent {len(urgent_rows)} / wait {wait_sec:.1f}s"
    )

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v291: micro urgent/target 연결 정상화 + 공장 저장 경량화. sweep_vwap 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception:
    pass



# ===============================
# v2.13.292: scan 병목/단계캐시 순서 수술
# - 조건값/익절/손절/시간청산 변경 없음.
# - v291의 후보별 WS/micro 재조회 overlay를 제거한다. scan 초반에 붙은 캐시만 사용하고,
#   부족한 후보는 target/urgent로 넘긴다.
# - /health 캐시를 scan 최종 stage_times 확정 후 저장한다.
# - 전략 파일 저장 단계에서 command cache를 중간 저장하지 않는다.
# ===============================

BOT_VERSION = "수익형 v2.13.292"
V292_VERSION_NOTE = "v292: 후보별 WS/micro 재조회 제거 + /health 단계캐시 최종저장"


def _v291_norm_should_overlay(item: Dict[str, Any], prefix: str) -> bool:  # type: ignore[override]
    """v292: 후보별 외부정보 재조회 제거.

    v291은 scan 초반 apply_ws_cache/apply_micro_cache 이후에도 missing row마다
    ws_snapshot/micro_snapshot을 다시 확인했다. 조건값은 그대로 두고, scan 병목을 줄이기 위해
    판정 중 재조회는 하지 않는다. 정보 부족 후보는 OPEN하지 않고 target/urgent로 보낸다.
    """
    return False


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Counter) -> None:  # type: ignore[override]
    """v292: 전략 snapshot 저장만 담당한다.

    command cache는 scan 최종 stage_times 확정 후 한 번만 저장한다.
    v291처럼 공장 중간에서 /health 캐시를 만들면 단계요약이 비어 보였다.
    """
    rows = list(strict_rows or []) + list(shadow_rows or [])
    nowv = now_ts()
    urgent_rows = _v291_urgent_priority_rows(strict_rows or [], shadow_rows or [], limit=80)
    payload = {
        "version": BOT_VERSION,
        "schema": "sweep_vwap_recovery_snapshot_v292",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": len([r for r in strict_rows or [] if bool(r.get("trade_ready"))]),
        "recheck_count": len([r for r in strict_rows or [] if str(r.get("final_entry_action")) == "recheck_wait"]),
        "urgent_watch_count": len(urgent_rows),
        "market_context": market,
        "rejects": dict(rejects or {}),
        "rows": rows[:V288_MATERIAL_MAX_ROWS],
        "note": "v292: sweep_vwap_recovery 단일전략. 후보별 재조회 제거, command cache는 scan 최종 후 저장. 조건값 변경 없음.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    save_json(FILES["strategy_lab_summary"], {
        "version": BOT_VERSION,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": payload["strict_count"],
        "shadow_count": payload["shadow_count"],
        "trade_ready_count": payload["trade_ready_count"],
        "recheck_count": payload["recheck_count"],
        "urgent_watch_count": payload["urgent_watch_count"],
        "rejects": dict(rejects or {}),
        "note": payload["note"],
    })
    write_jsonl_replace(FILES["strategy_material_latest"], rows[:V288_MATERIAL_MAX_ROWS])


def scan_once() -> List[Dict[str, Any]]:  # type: ignore[override]
    started = now_ts()
    ensure_eval_baseline()
    with _state_lock:
        STATE["scan_calls"] = int(STATE.get("scan_calls", 0)) + 1
        STATE["scan_seq"] = int(STATE.get("scan_seq", 0)) + 1
        STATE["scan_id"] = f"scan-{int(started)}-{int(STATE.get('scan_seq', 0))}"
        STATE["scan_started_at"] = started
        STATE["scan_running"] = True
        STATE["scan_display_note"] = "v292 sweep/vwap 스캔 진행중"
        STATE["scan_last_stage"] = "hub_bulk"
        STATE["scan_last_error"] = ""
        STATE["stage_times"] = []
    stage_times: List[Tuple[str, float, str]] = []
    try:
        st = now_ts()
        rows, source = fetch_all_krw()
        rows = apply_ws_cache_to_rows(rows)
        rows = apply_micro_cache_to_rows(rows)
        _v290_stage_push(stage_times, "1) 허브", now_ts() - st, f"rows {len(rows)} / {source} / WS fresh {STATE.get('ws_fresh',0)} / micro fresh {STATE.get('micro_fresh',0)}")
        with _state_lock:
            STATE["bulk_rows"] = len(rows)
            STATE["bulk_price"] = sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0)
            STATE["bulk_money"] = sum(1 for r in rows if fnum(r.get("turnover_24h"), 0) > 0)
            STATE["scan_last_stage"] = "worker_precision_sweep_vwap"
        if _stop_event.is_set():
            return []

        st = now_ts()
        targets = select_precision_targets(rows)
        p_ok, p_fail, p_have = refresh_precision(rows, targets)
        _v290_stage_push(stage_times, "2) 직원: 정밀/VWAP 보강", now_ts() - st, f"targets {len(targets)} / 직접 {p_ok} / cached {p_have} / fail {p_fail}")
        with _state_lock:
            STATE["precision_selected"] = len(targets)
            STATE["precision_refreshed"] = p_ok
            STATE["precision_failed"] = p_fail
            STATE["precision_have"] = p_have
            STATE["scan_last_stage"] = "worker_merge"
        if _stop_event.is_set():
            return []

        st = now_ts()
        rows = merge_precision(rows)
        cov = update_field_coverage(rows)
        _v290_stage_push(stage_times, "3) 직원: 표준값 병합", now_ts() - st, f"precision {cov.get('precision')} / bulk-only {cov.get('bulk_only')}")
        with _state_lock:
            STATE["field_coverage"] = cov
            STATE["scan_last_stage"] = "sweep_vwap_build"
        if _stop_event.is_set():
            return []

        st = now_ts()
        strict, shadow, rejects, examples = build_candidates(rows)
        ready_n = len([r for r in strict if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
        recheck_n = len([r for r in strict if str(r.get("final_entry_action")) == "recheck_wait"])
        _v290_stage_push(stage_times, "4) 전략: sweep/vwap 판정", now_ts() - st, f"진입 {ready_n} / 재확인 {recheck_n} / shadow {len(shadow)} / reject {sum(rejects.values())}")
        with _state_lock:
            STATE["strict_decision"] = len(strict)
            STATE["shadow_decision"] = len(shadow)
            STATE["reject_counts"] = dict(rejects)
            STATE["reject_examples"] = examples
            STATE["latest_trade_ready"] = ready_n
            STATE["trade_ready_written"] = ready_n
            STATE["scan_last_stage"] = "factory_export"
        if _stop_event.is_set():
            return []

        st = now_ts()
        pipe = export_candidates(strict, shadow)
        _v290_stage_push(stage_times, "5) 공장: latest 저장", now_ts() - st, f"latest {pipe.get('paper_latest_written',0)} / OPEN {pipe.get('latest_trade_ready',0)} / 재확인 {pipe.get('latest_final_recheck_wait',0)} / urgent {pipe.get('urgent_watch_rows',0)}")
        if _stop_event.is_set():
            return []

        st = now_ts()
        snap_note = v212_publish_final_candidate_state(strict, shadow, rows, pipe)
        _v290_stage_push(stage_times, "6) 공장: snapshot/target", now_ts() - st, snap_note)

        # command cache는 최종 stage_times를 먼저 STATE에 올린 뒤 저장한다.
        with _state_lock:
            STATE["stage_times"] = [(a, round(float(b), 3), c) for a, b, c in stage_times]
        st = now_ts()
        try:
            _v288_write_command_caches(reason="scan_final_v292")
        except Exception as exc:
            log_error("v292_final_command_cache", exc)
        _v290_stage_push(stage_times, "7) 캐시: 기본명령", now_ts() - st, "health/score/quality/strategy_watch")

        total = now_ts() - started
        with _state_lock:
            STATE["paper_written"] = pipe.get("paper_written", 0)
            STATE["trade_ready_written"] = pipe.get("trade_ready_written", 0)
            STATE["strict_observe_written"] = pipe.get("strict_observe_written", 0)
            STATE["shadow_written"] = len(shadow)
            STATE["paper_latest_written"] = pipe.get("paper_latest_written", 0)
            STATE["shadow_latest_written"] = pipe.get("shadow_latest_written", 0)
            STATE["latest_trade_ready"] = pipe.get("latest_trade_ready", 0)
            STATE["latest_strict_observe"] = pipe.get("latest_strict_observe", 0)
            STATE["latest_final_recheck_wait"] = pipe.get("latest_final_recheck_wait", 0)
            STATE["last_scan_candidates"] = len(strict)
            STATE["scan_last_sec"] = total
            STATE["scan_max_sec"] = max(float(STATE.get("scan_max_sec", 0.0)), total)
            STATE["scan_completed_at"] = now_ts()
            STATE["last_done_scan_sec"] = total
            STATE["last_done_scan_ts"] = STATE["scan_completed_at"]
            STATE["scan_running"] = False
            STATE["scan_last_stage"] = "done"
            STATE["scan_display_note"] = "v292 sweep/vwap 완료"
            STATE["stage_times"] = [(a, round(float(b), 3), c) for a, b, c in stage_times]
            STATE["factory_mode"] = pipe.get("factory_mode")
        # 캐시에 7단계까지 반영하려고 한 번 더 health만 가볍게 덮는다.
        try:
            save_json(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), _v288_cache_payload(_v288_health_text(), "health"))
        except Exception as exc:
            log_error("v292_final_health_cache", exc)
        return strict
    except Exception as exc:
        log_error("scan_once_v292", exc)
        with _state_lock:
            STATE["scan_running"] = False
            STATE["scan_last_stage"] = "error"
            STATE["scan_last_error"] = str(exc)[:300]
            STATE["scan_completed_at"] = now_ts()
        return []

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v292: 후보별 WS/micro 재조회 제거 + /health 단계캐시 최종저장. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception:
    pass



# ===============================
# v2.13.293: stage cache 고정 + bulk-only 전략판정 스킵 + snapshot 경량화
# - 전략 조건/익절/손절/시간청산 변경 없음.
# - scan 시작 중 stage_times를 비우는 문제를 제거해 /health 단계요약 소실을 막는다.
# - 정밀/VWAP/흐름 재료가 전혀 없는 bulk-only row는 전략 판정을 태우지 않는다.
#   전체시장 bulk 스캔은 유지하되, 통과 불가능한 row의 비싼 후보 row 생성만 생략한다.
# - candidate snapshot/target 단계는 sidecar target 파일과 작은 snapshot만 저장한다.
# ===============================

BOT_VERSION = "수익형 v2.13.293"
V293_VERSION_NOTE = "v293: stage cache 고정 + bulk-only 전략판정 스킵 + snapshot 경량화"


def _v293_has_strategy_material(row: Dict[str, Any]) -> bool:
    """sweep_vwap 진입판정에 필요한 정밀 재료가 하나라도 있는지 본다.

    bulk-only 24h row는 어차피 저점/VWAP/1분·3분 흐름 조건을 통과할 수 없다.
    그런 row를 매번 _v288_candidate_row까지 태우면 scan 병목과 값부족 사유만 늘어난다.
    전체시장 스캔은 유지하고, 전략판정 작업량만 줄인다.
    """
    try:
        if not isinstance(row, dict):
            return False
        keys = (
            "from_30m_low_pct", "from_low_pct", "below_30m_high_pct", "high_gap_pct",
            "vwap_gap_pct", "ema5_gap_pct", "change_1", "rate_1m", "change_rate_1m",
            "change_3", "rate_3m", "change_rate_3m", "turnover_1m", "money_flow_1m",
            "turnover_3m", "money_flow_3m",
        )
        return any(_v290_has_value(row, k) for k in keys)
    except Exception:
        return False


def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    market = _v274_market_context(rows if isinstance(rows, list) else [])
    ready_rows: List[Dict[str, Any]] = []
    recheck_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    rejects: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    skipped_no_material = 0
    evaluated = 0

    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        t = _v274_ticker(raw)
        if not t or t in STABLE_EXCLUDED:
            continue
        if not _v293_has_strategy_material(raw):
            skipped_no_material += 1
            continue
        evaluated += 1
        try:
            item = _v288_candidate_row(raw, market)
        except Exception as exc:
            log_error("v293_candidate_row", exc)
            continue
        action = str(item.get("final_entry_action") or "")
        if action == "paper_open":
            ready_rows.append(item)
        elif action == "recheck_wait":
            recheck_rows.append(item)
        else:
            reason = (item.get("block_reasons") or item.get("final_entry_reasons") or item.get("quality_risk_tags") or ["관찰"])[0]
            rejects[str(reason)] += 1
            if len(examples) < 8:
                examples.append({"ticker": t, "reason": str(reason), "score": fnum(item.get("score"), 0), "line": item.get("one_liner", "")})
            review_rows.append(item)

    if skipped_no_material:
        rejects["전략정밀값없음_bulk스킵"] += skipped_no_material
    ready_rows.sort(key=_v288_rank_key, reverse=True)
    recheck_rows.sort(key=_v288_rank_key, reverse=True)
    review_rows.sort(key=_v288_rank_key, reverse=True)
    strict = [dict(x) for x in (ready_rows[:V288_TRADE_READY_MAX_ROWS] + recheck_rows)[:V288_STRICT_MAX_ROWS]]
    shadow = [dict(x) for x in review_rows[:V288_SHADOW_MAX_ROWS]]
    with _state_lock:
        STATE["v293_evaluated_rows"] = evaluated
        STATE["v293_bulk_skipped_no_material"] = skipped_no_material
        STATE["v288_ready_rows"] = len(ready_rows)
        STATE["v288_recheck_rows"] = len(recheck_rows)
        STATE["v288_review_rows"] = len(review_rows)
        STATE["strict_decision"] = len(strict)
        STATE["shadow_decision"] = len(shadow)
        STATE["latest_trade_ready"] = len([r for r in strict if bool(r.get("trade_ready"))])
        STATE["trade_ready_written"] = STATE["latest_trade_ready"]
        STATE["phase_note"] = "v293: stage cache 고정 + bulk-only 전략판정 스킵 + snapshot 경량화. 조건값 변경 없음."
    return strict, shadow, rejects, examples


_v293_original_stage_line = _v290_stage_line

def _v290_stage_line(stage_rows: Any, limit: int = 6) -> Tuple[List[str], List[str]]:  # type: ignore[override]
    """stage_rows가 scan 시작 중 비어도 마지막 완료 stage를 보여준다."""
    try:
        rows = stage_rows
        if not rows:
            with _state_lock:
                rows = STATE.get("last_done_stage_times") or STATE.get("stage_times") or []
        return _v293_original_stage_line(rows, limit=limit)
    except Exception:
        return [], []


def _v293_write_light_candidate_snapshot(priority_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], stage: str, wait_sec: float = 0.0) -> Dict[str, Any]:
    rows = [r for r in (priority_rows or []) if isinstance(r, dict)]
    shadow = [r for r in (shadow_rows or []) if isinstance(r, dict)]
    nowv = now_ts()
    ext = {
        "total": len(rows),
        "ws_fresh": sum(1 for r in rows if bool(r.get("ws_fresh")) or str(r.get("ws_row_status") or "") == "fresh"),
        "micro_fresh": sum(1 for r in rows if bool(r.get("micro_fresh")) or str(r.get("micro_row_status") or "") == "fresh"),
    }
    data = {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v293_light",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or ""),
        "stage": stage,
        "candidate_count": len(rows),
        "shadow_count": len(shadow),
        "external": ext,
        "rows": rows[:80],
        "shadow_rows": shadow[:40],
        "wait_sec": wait_sec,
        "note": "v293: 무거운 snapshot overlay/external refresh 없이 sidecar target용 작은 snapshot만 저장",
    }
    try:
        save_json(FILES.get("candidate_snapshot", BASE_DIR / "clean_candidate_snapshot.json"), data)
    except Exception as exc:
        log_error("v293_light_candidate_snapshot", exc)
    return data


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v293: target 저장은 유지하고 무거운 snapshot overlay/schedule 경로를 끊는다.

    조건을 완화하지 않는다. OPEN/재확인 후보나 urgent 필요 후보만 priority로 올리고,
    나머지 전체 rows는 sidecar 일반 회전 대상으로만 넘긴다.
    """
    try:
        strict_rows = [r for r in (strict or []) if isinstance(r, dict)]
        shadow_rows = [r for r in (shadow or []) if isinstance(r, dict)]
        urgent_rows = _v291_urgent_priority_rows(strict_rows, shadow_rows, limit=80)
        priority_rows = list(strict_rows or [])
        seen = {_v274_ticker(r) for r in priority_rows}
        for r in urgent_rows:
            t = _v274_ticker(r)
            if t and t not in seen:
                priority_rows.append(r); seen.add(t)
        update_ws_targets(market_rows or [], priority_rows=priority_rows, reason="factory_final_candidates_v293")
        update_micro_targets(market_rows or [], priority_rows=priority_rows, reason="factory_final_candidates_v293")
        final = _v293_write_light_candidate_snapshot(priority_rows, shadow_rows, stage="final_v293_light", wait_sec=0.0)
        ext = final.get("external", {}) if isinstance(final, dict) else {}
        with _state_lock:
            STATE["v293_snapshot_priority_rows"] = len(priority_rows)
            STATE["v293_urgent_watch_rows"] = len(urgent_rows)
            STATE["v233_snapshot_memory_rows"] = True
            STATE["v233_snapshot_initial_wait_sec"] = 0.0
        return (
            f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / "
            f"micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / targets WS {STATE.get('ws_target_file_targets',0)} / "
            f"micro {STATE.get('micro_target_file_targets',0)} / urgent {len(urgent_rows)} / wait 0.0s"
        )
    except Exception as exc:
        log_error("v293_publish_final_candidate_state", exc)
        return f"snapshot 경량화 실패 {exc.__class__.__name__}"


def _v288_write_command_caches(reason: str = "manual") -> None:  # type: ignore[override]
    """v293: command cache 단일 저장.

    scan 진행 중 background worker가 health를 덮어도 마지막 완료 stage를 유지하도록
    _v290_stage_line fallback과 last_done_stage_times를 같이 쓴다.
    """
    try:
        rows = _v274_read_material_rows(120)
        save_json(FILES.get("score_summary", BASE_DIR / "clean_score_summary.json"), _v288_cache_payload(_v288_score_text(rows), "score"))
        save_json(FILES.get("quality_summary", BASE_DIR / "clean_quality_summary.json"), _v288_cache_payload(_v288_quality_text(False), "quality"))
        save_json(FILES.get("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json"), _v288_cache_payload(_v288_strategy_watch_text(False), "strategy_watch"))
        save_json(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), _v288_cache_payload(_v288_health_text(), "health"))
    except Exception as exc:
        log_error("v293_write_command_caches", exc)


def scan_once() -> List[Dict[str, Any]]:  # type: ignore[override]
    started = now_ts()
    ensure_eval_baseline()
    with _state_lock:
        STATE["scan_calls"] = int(STATE.get("scan_calls", 0)) + 1
        STATE["scan_seq"] = int(STATE.get("scan_seq", 0)) + 1
        STATE["scan_id"] = f"scan-{int(started)}-{int(STATE.get('scan_seq', 0))}"
        STATE["scan_started_at"] = started
        STATE["scan_running"] = True
        STATE["scan_display_note"] = "v293 sweep/vwap 스캔 진행중"
        STATE["scan_last_stage"] = "hub_bulk"
        STATE["scan_last_error"] = ""
        # v293: 마지막 완료 stage_times를 지우지 않는다. 진행 중 기록은 별도 키로만 둔다.
        STATE["current_stage_times"] = []
    stage_times: List[Tuple[str, float, str]] = []
    try:
        st = now_ts()
        rows, source = fetch_all_krw()
        rows = apply_ws_cache_to_rows(rows)
        rows = apply_micro_cache_to_rows(rows)
        _v290_stage_push(stage_times, "1) 허브", now_ts() - st, f"rows {len(rows)} / {source} / WS fresh {STATE.get('ws_fresh',0)} / micro fresh {STATE.get('micro_fresh',0)}")
        with _state_lock:
            STATE["current_stage_times"] = list(stage_times)
            STATE["bulk_rows"] = len(rows)
            STATE["bulk_price"] = sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0)
            STATE["bulk_money"] = sum(1 for r in rows if fnum(r.get("turnover_24h"), 0) > 0)
            STATE["scan_last_stage"] = "worker_precision_sweep_vwap"
        if _stop_event.is_set():
            return []

        st = now_ts()
        targets = select_precision_targets(rows)
        p_ok, p_fail, p_have = refresh_precision(rows, targets)
        _v290_stage_push(stage_times, "2) 직원: 정밀/VWAP 보강", now_ts() - st, f"targets {len(targets)} / 직접 {p_ok} / cached {p_have} / fail {p_fail}")
        with _state_lock:
            STATE["current_stage_times"] = list(stage_times)
            STATE["precision_selected"] = len(targets)
            STATE["precision_refreshed"] = p_ok
            STATE["precision_failed"] = p_fail
            STATE["precision_have"] = p_have
            STATE["scan_last_stage"] = "worker_merge"
        if _stop_event.is_set():
            return []

        st = now_ts()
        rows = merge_precision(rows)
        cov = update_field_coverage(rows)
        _v290_stage_push(stage_times, "3) 직원: 표준값 병합", now_ts() - st, f"precision {cov.get('precision')} / bulk-only {cov.get('bulk_only')}")
        with _state_lock:
            STATE["current_stage_times"] = list(stage_times)
            STATE["field_coverage"] = cov
            STATE["scan_last_stage"] = "sweep_vwap_build"
        if _stop_event.is_set():
            return []

        st = now_ts()
        strict, shadow, rejects, examples = build_candidates(rows)
        ready_n = len([r for r in strict if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
        recheck_n = len([r for r in strict if str(r.get("final_entry_action")) == "recheck_wait"])
        _v290_stage_push(stage_times, "4) 전략: sweep/vwap 판정", now_ts() - st, f"진입 {ready_n} / 재확인 {recheck_n} / shadow {len(shadow)} / eval {STATE.get('v293_evaluated_rows',0)} / skip {STATE.get('v293_bulk_skipped_no_material',0)} / reject {sum(rejects.values())}")
        with _state_lock:
            STATE["current_stage_times"] = list(stage_times)
            STATE["strict_decision"] = len(strict)
            STATE["shadow_decision"] = len(shadow)
            STATE["reject_counts"] = dict(rejects)
            STATE["reject_examples"] = examples
            STATE["latest_trade_ready"] = ready_n
            STATE["trade_ready_written"] = ready_n
            STATE["scan_last_stage"] = "factory_export"
        if _stop_event.is_set():
            return []

        st = now_ts()
        pipe = export_candidates(strict, shadow)
        _v290_stage_push(stage_times, "5) 공장: latest 저장", now_ts() - st, f"latest {pipe.get('paper_latest_written',0)} / OPEN {pipe.get('latest_trade_ready',0)} / 재확인 {pipe.get('latest_final_recheck_wait',0)} / urgent {pipe.get('urgent_watch_rows',0)}")
        with _state_lock:
            STATE["current_stage_times"] = list(stage_times)
        if _stop_event.is_set():
            return []

        st = now_ts()
        snap_note = v212_publish_final_candidate_state(strict, shadow, rows, pipe)
        _v290_stage_push(stage_times, "6) 공장: snapshot/target", now_ts() - st, snap_note)

        total = now_ts() - started
        with _state_lock:
            final_stages = [(a, round(float(b), 3), c) for a, b, c in stage_times]
            STATE["paper_written"] = pipe.get("paper_written", 0)
            STATE["trade_ready_written"] = pipe.get("trade_ready_written", 0)
            STATE["strict_observe_written"] = pipe.get("strict_observe_written", 0)
            STATE["shadow_written"] = len(shadow)
            STATE["paper_latest_written"] = pipe.get("paper_latest_written", 0)
            STATE["shadow_latest_written"] = pipe.get("shadow_latest_written", 0)
            STATE["latest_trade_ready"] = pipe.get("latest_trade_ready", 0)
            STATE["latest_strict_observe"] = pipe.get("latest_strict_observe", 0)
            STATE["latest_final_recheck_wait"] = pipe.get("latest_final_recheck_wait", 0)
            STATE["last_scan_candidates"] = len(strict)
            STATE["scan_last_sec"] = total
            STATE["scan_max_sec"] = max(float(STATE.get("scan_max_sec", 0.0)), total)
            STATE["scan_completed_at"] = now_ts()
            STATE["last_done_scan_sec"] = total
            STATE["last_done_scan_ts"] = STATE["scan_completed_at"]
            STATE["scan_running"] = False
            STATE["scan_last_stage"] = "done"
            STATE["scan_display_note"] = "v293 sweep/vwap 완료"
            STATE["stage_times"] = final_stages
            STATE["last_done_stage_times"] = final_stages
            STATE["current_stage_times"] = []
            STATE["factory_mode"] = pipe.get("factory_mode")
        st = now_ts()
        try:
            _v288_write_command_caches(reason="scan_final_v293")
        except Exception as exc:
            log_error("v293_final_command_cache", exc)
        _v290_stage_push(stage_times, "7) 캐시: 기본명령", now_ts() - st, "health/score/quality/strategy_watch")
        # 7단계까지 포함한 health만 한 번 더 갱신
        with _state_lock:
            final_stages = [(a, round(float(b), 3), c) for a, b, c in stage_times]
            STATE["stage_times"] = final_stages
            STATE["last_done_stage_times"] = final_stages
        try:
            save_json(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), _v288_cache_payload(_v288_health_text(), "health"))
        except Exception as exc:
            log_error("v293_final_health_cache", exc)
        return strict
    except Exception as exc:
        log_error("scan_once_v293", exc)
        with _state_lock:
            STATE["scan_running"] = False
            STATE["scan_last_stage"] = "error"
            STATE["scan_last_error"] = str(exc)[:300]
            STATE["scan_completed_at"] = now_ts()
        return []

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v293: stage cache 고정 + bulk-only 전략판정 스킵 + snapshot 경량화. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception:
    pass


# ===============================
# v2.13.294: main guard 위치수술
# - v293 override 블록이 __main__ 실행 뒤에 붙어 실제 서버가 v292로 부팅되던 문제 수정.
# - 전략 조건/익절/손절/시간청산 변경 없음.
# - v293 stage/bulk-skip/snapshot 경량화 블록을 먼저 적용한 뒤 main()을 실행한다.
# ===============================

BOT_VERSION = "수익형 v2.13.294"
V294_VERSION_NOTE = "v294: main guard 위치수술 + v293 override 실제 실행 연결"
try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v294: main guard 위치수술. v293 stage/bulk-skip/snapshot 경량화가 실제 main 실행 전에 적용됨. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception:
    pass


# ===============================
# v2.13.295: 정밀값 schema 수술 + precision retry 재순환 + WS target 진단 보강
# - 조건/익절/손절/시간청산 변경 없음.
# - 정보부족 후보를 표시로만 대기시키는 임시방편이 아니라,
#   왜 값이 비는지의 원천 경로를 정리한다.
# - 정밀값 없는 row에 0/999 기본값을 넣지 않는다.
# - precision_source/precision_ok 기준으로 전략평가 가능 row를 판정한다.
# - 정밀값 부족 but 시장 반응이 있는 row는 다음 scan 정밀대상으로 재순환한다.
# - micro urgent/WS target 파일에는 force_reconnect 진단값을 넣어 sidecar 재구독 확인을 쉽게 한다.
# ===============================

BOT_VERSION = "수익형 v2.13.295"
V295_VERSION_NOTE = "v295: 정밀값 schema 수술 + precision retry 재순환 + WS target 진단 보강"
FILES.setdefault("sweep_precision_retry", BASE_DIR / "clean_sweep_precision_retry.json")

_V295_PRECISION_KEYS = (
    "turnover_1m", "turnover_3m", "turnover_5m",
    "change_1", "change_3", "change_5", "change_15", "change_30",
    "from_30m_low_pct", "from_low_pct", "below_30m_high_pct", "high_gap_pct",
    "vwap_gap_pct", "ema5_gap_pct", "ma5_gap_pct", "ma20_gap_pct",
    "bb_lower_gap_pct", "current_lower_wick_pct", "current_upper_wick_pct",
    "low_defense_pct", "current_close_pos_ratio", "current_candle_change_pct",
    "rsi_14", "mfi_14", "cci_20", "stoch_k", "stoch_d", "atr_1m_pct", "atr_3m_pct",
)


def _v295_ticker(row: Any) -> str:
    try:
        return _v274_ticker(row)
    except Exception:
        return str((row or {}).get("ticker") or "").upper().strip() if isinstance(row, dict) else ""


def _v295_precision_ok(row: Dict[str, Any]) -> bool:
    """전략 정밀값이 실제 candlestick 보강에서 왔는지 확인한다.

    0.0 자체는 실제 값일 수 있으므로 값의 크기만 보지 않는다.
    대신 precision_ok/source/ts를 우선 보고, 고점여유 999 같은 sentinel은 제외한다.
    """
    try:
        if not isinstance(row, dict):
            return False
        src = str(row.get("precision_source") or "")
        ok = bool(row.get("precision_ok")) or src == "candlestick_1m"
        if not ok:
            return False
        ts = fnum(row.get("precision_ts"), 0.0)
        # 오래된 캐시도 너무 엄격히 버리지는 않되, 아예 ts가 없으면 정밀값으로 보지 않는다.
        if ts <= 0 and src != "candlestick_1m":
            return False
        high_room = fnum(row.get("below_30m_high_pct", row.get("high_gap_pct", 999)), 999)
        if high_room >= 900:
            return False
        return True
    except Exception:
        return False


def merge_precision(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v295: 정밀값 없는 row에 전략 기본값 0/999를 넣지 않는다.

    기존 merge_precision은 bulk-only row에도 from_low=0, high_room=999, vwap=0 같은
    sentinel/default를 넣었다. 그 결과 정보 없음과 실제 0값이 섞여 전략평가/품질표가
    흐려졌다. v295는 source를 명확히 남기고, 전략 재료 키는 없는 상태로 유지한다.
    """
    out: List[Dict[str, Any]] = []
    nowv = now_ts()
    with _precision_lock:
        cache = dict(_precision_cache)
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = str(r.get("ticker") or "").upper()
        rr = dict(r)
        prof = cache.get(t)
        if isinstance(prof, dict) and prof.get("precision_ok"):
            rr.update(prof)
            rr["precision_ok"] = True
            rr["precision_age_sec"] = round(nowv - fnum(prof.get("precision_ts"), nowv), 1)
            rr["precision_source"] = "candlestick_1m"
            rr.pop("precision_missing", None)
            rr.pop("precision_missing_reason", None)
        else:
            # 핵심: 전략 재료 default를 넣지 않는다. 이전 row에 섞여 들어왔을 수 있는 sentinel도 제거한다.
            for k in _V295_PRECISION_KEYS:
                if k in rr:
                    rr.pop(k, None)
            rr["precision_ok"] = False
            rr["precision_source"] = "missing"
            rr["precision_missing"] = True
            rr["precision_missing_reason"] = "정밀캔들 미보강: 다음 precision target 순환 필요"
            rr["analysis_status"] = "정밀값없음"
            rr.setdefault("money_status", "24h_bulk_only")
        out.append(rr)
    return out


def update_field_coverage(rows: List[Dict[str, Any]]) -> Dict[str, int]:  # type: ignore[override]
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    precision_rows = [r for r in rows if _v295_precision_ok(r)]
    cov = {
        "rows": len(rows),
        "price": sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0),
        "money": sum(1 for r in rows if fnum(r.get("money_proxy_24h") or r.get("turnover_24h"), 0) > 0),
        "volume": sum(1 for r in precision_rows if fnum(r.get("vol_ratio"), 0) > 0),
        "momentum": sum(1 for r in precision_rows if max(abs(fnum(r.get("change_5"), 0)), abs(fnum(r.get("change_15"), 0)), abs(fnum(r.get("change_30"), 0))) > 0),
        "position": sum(1 for r in precision_rows if fnum(r.get("below_30m_high_pct"), 999) < 900 or _v290_has_value(r, "from_30m_low_pct", "from_low_pct")),
        "rank": sum(1 for r in rows if fint(r.get("turnover_rank"), 999) < 999),
        "fresh": len(rows),
        "precision": len(precision_rows),
        "bulk_only": sum(1 for r in rows if str(r.get("precision_source") or "") in {"missing", "bulk_only", ""}),
        "precision_missing": sum(1 for r in rows if bool(r.get("precision_missing")) or str(r.get("precision_source") or "") == "missing"),
        "real_money": 0,
        "proxy_money": sum(1 for r in rows if fnum(r.get("money_proxy_24h") or r.get("turnover_24h"), 0) > 0),
        "missing_money": sum(1 for r in rows if fnum(r.get("money_proxy_24h") or r.get("turnover_24h"), 0) <= 0),
        "ema12": sum(1 for r in precision_rows if r.get("ema12_gap_pct") not in (None, "")),
        "bb": sum(1 for r in precision_rows if r.get("bb_position") not in (None, "")),
        "mfi": sum(1 for r in precision_rows if r.get("mfi_14") not in (None, "")),
        "stoch": sum(1 for r in precision_rows if r.get("stoch_k") not in (None, "")),
        "adx": sum(1 for r in precision_rows if r.get("adx_14") not in (None, "")),
        "atr": sum(1 for r in precision_rows if fnum(r.get("atr_1m_pct"), 0) > 0),
    }
    return cov


def _v295_read_target_payload_tickers(path: Path, ttl_sec: float = 180.0) -> List[str]:
    try:
        obj = load_json(path, {})
        if not isinstance(obj, dict):
            return []
        ts = fnum(obj.get("updated_ts"), 0.0)
        if ts > 0 and now_ts() - ts > ttl_sec:
            return []
        raw = obj.get("targets") or []
        out: List[str] = []
        seen: set[str] = set()
        for x in raw if isinstance(raw, list) else []:
            t = _ticker_from_any(x)
            if t and t not in STABLE_EXCLUDED and t not in seen:
                seen.add(t); out.append(t)
        return out
    except Exception:
        return []


def _v295_priority_precision_tickers() -> List[str]:
    """다음 scan 정밀보강 우선순위.

    정보부족 후보를 단순히 '대기'로 표시하는 대신, 이전 scan에서 실제로 부족했던
    ticker를 precision target 선정 단계 앞으로 되돌린다.
    """
    out: List[str] = []
    seen: set[str] = set()
    paths = [
        FILES.get("sweep_precision_retry", BASE_DIR / "clean_sweep_precision_retry.json"),
        FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"),
        FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"),
        FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"),
    ]
    for p in paths:
        for t in _v295_read_target_payload_tickers(Path(p), ttl_sec=240.0):
            if t and t not in seen:
                seen.add(t); out.append(t)
            if len(out) >= 120:
                return out
    return out


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    rows = [r for r in (rows or []) if isinstance(r, dict) and _v274_ticker(r) not in STABLE_EXCLUDED]
    by_ticker: Dict[str, Dict[str, Any]] = {_v274_ticker(r): r for r in rows if _v274_ticker(r)}
    priority_tickers = _v295_priority_precision_tickers()
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for t in priority_tickers:
        r = by_ticker.get(t)
        if r is not None and t not in seen:
            selected.append(r); seen.add(t)

    def key(r: Dict[str, Any]) -> Tuple[float, float, float, float, float]:
        # precision 전에는 1/3분 값이 없을 수 있으므로 bulk에서 안정적으로 있는 24h/rank/change 기준으로 뽑는다.
        rank = fint(r.get("turnover_rank"), 9999)
        rank_score = max(0.0, 10000.0 - float(rank))
        turnover24 = fnum(r.get("turnover_24h") or r.get("money_proxy_24h") or r.get("acc_trade_value_24h"), 0.0)
        ch24 = abs(fnum(r.get("change_24h") or r.get("fluctate_rate_24H") or r.get("change_rate"), 0.0))
        ws_bonus = 1.0 if bool(r.get("ws_fresh")) or str(r.get("ws_row_status") or "") == "fresh" else 0.0
        micro_bonus = 1.0 if bool(r.get("micro_fresh")) or str(r.get("micro_row_status") or "") == "fresh" else 0.0
        return (ws_bonus + micro_bonus, rank_score, turnover24, ch24, fnum(r.get("current_price"), 0.0))

    rest = [r for r in rows if _v274_ticker(r) not in seen]
    rest.sort(key=key, reverse=True)
    limit = max(PRECISION_REFRESH_MIN, min(PRECISION_REFRESH_MAX_SAFETY, PRECISION_REFRESH_BASE + int(len(rows) * 0.06) + min(30, len(priority_tickers))))
    for r in rest:
        if len(selected) >= limit:
            break
        t = _v274_ticker(r)
        if t and t not in seen:
            selected.append(r); seen.add(t)
    with _state_lock:
        STATE["precision_target_note"] = f"v295 precision retry {len(selected)}/{len(rows)} / priority {len([t for t in priority_tickers if t in by_ticker])}"
        STATE["precision_target_source"] = "v295_retry_plus_bulk_rank"
        STATE["v295_precision_priority_count"] = len([t for t in priority_tickers if t in by_ticker])
    return selected[:limit]


def _v295_has_strategy_material(row: Dict[str, Any]) -> bool:
    """전략평가는 정밀캔들 보강이 실제로 붙은 row만 태운다."""
    try:
        if not _v295_precision_ok(row):
            return False
        # 핵심 위치/회복/흐름/돈 재료 중 최소 묶음이 있어야 한다.
        core_groups = [
            _v290_has_value(row, "from_30m_low_pct", "from_low_pct"),
            _v290_has_value(row, "below_30m_high_pct", "high_gap_pct"),
            _v290_has_value(row, "vwap_gap_pct") or _v290_has_value(row, "ema5_gap_pct"),
            _v290_has_value(row, "change_1", "rate_1m", "change_rate_1m") or _v290_has_value(row, "change_3", "rate_3m", "change_rate_3m"),
            _v290_has_value(row, "turnover_1m", "money_flow_1m") or _v290_has_value(row, "turnover_3m", "money_flow_3m"),
        ]
        return sum(1 for x in core_groups if x) >= 4
    except Exception:
        return False

# v293 name을 실제 새 판정 함수에 연결한다.
def _v293_has_strategy_material(row: Dict[str, Any]) -> bool:  # type: ignore[override]
    return _v295_has_strategy_material(row)


def _v295_precision_retry_candidate(row: Dict[str, Any]) -> bool:
    """정밀값은 없지만 다음 scan에 정밀보강을 시도할 가치가 있는 bulk row."""
    try:
        if not isinstance(row, dict):
            return False
        t = _v274_ticker(row)
        if not t or t in STABLE_EXCLUDED or t in MAJOR_WATCH_TICKERS:
            return False
        if _v295_precision_ok(row):
            return False
        rank = fint(row.get("turnover_rank"), 9999)
        turnover24 = fnum(row.get("turnover_24h") or row.get("money_proxy_24h"), 0.0)
        ch24 = abs(fnum(row.get("change_24h") or row.get("fluctate_rate_24H") or row.get("change_rate"), 0.0))
        external = bool(row.get("ws_fresh")) or bool(row.get("micro_fresh")) or str(row.get("ws_row_status") or "") == "fresh" or str(row.get("micro_row_status") or "") == "fresh"
        return bool(rank <= 180 or turnover24 >= 800_000_000 or ch24 >= 1.2 or external)
    except Exception:
        return False


def _v295_write_precision_retry(rows: List[Dict[str, Any]], reason: str = "v295_precision_missing_retry") -> None:
    try:
        selected: List[Dict[str, Any]] = []
        seen: set[str] = set()
        def key(r: Dict[str, Any]) -> Tuple[float, float, float, float]:
            rank = fint(r.get("turnover_rank"), 9999)
            return (
                1.0 if bool(r.get("ws_fresh")) or bool(r.get("micro_fresh")) else 0.0,
                max(0.0, 10000.0 - float(rank)),
                fnum(r.get("turnover_24h") or r.get("money_proxy_24h"), 0.0),
                abs(fnum(r.get("change_24h") or r.get("change_rate"), 0.0)),
            )
        for r in sorted([x for x in rows or [] if _v295_precision_retry_candidate(x)], key=key, reverse=True):
            t = _v274_ticker(r)
            if not t or t in seen:
                continue
            seen.add(t)
            selected.append(r)
            if len(selected) >= 80:
                break
        targets = [_v274_ticker(r) for r in selected if _v274_ticker(r)]
        meta = {t: {
            "reason": reason,
            "source": "precision_retry",
            "turnover_rank": fint((next((r for r in selected if _v274_ticker(r)==t), {}) or {}).get("turnover_rank"), 9999),
            "turnover_24h": fnum((next((r for r in selected if _v274_ticker(r)==t), {}) or {}).get("turnover_24h"), 0.0),
        } for t in targets}
        payload = {
            "version": BOT_VERSION,
            "schema": "v295_sweep_precision_retry",
            "updated_ts": now_ts(),
            "updated_text": now_text(),
            "ttl_sec": 240,
            "reason": reason,
            "targets": targets,
            "target_count": len(targets),
            "target_meta": meta,
            "note": "정밀값 없는 bulk row 중 시장 반응이 있는 ticker를 다음 scan 정밀캔들 보강 대상으로 재순환",
        }
        save_json(FILES.get("sweep_precision_retry", BASE_DIR / "clean_sweep_precision_retry.json"), payload)
        with _state_lock:
            STATE["v295_precision_retry_targets"] = len(targets)
            STATE["v295_precision_retry_written"] = payload["updated_ts"]
    except Exception as exc:
        log_error("v295_write_precision_retry", exc)


def build_candidates(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Counter, List[Dict[str, Any]]]:  # type: ignore[override]
    market = _v274_market_context(rows if isinstance(rows, list) else [])
    ready_rows: List[Dict[str, Any]] = []
    recheck_rows: List[Dict[str, Any]] = []
    review_rows: List[Dict[str, Any]] = []
    rejects: Counter = Counter()
    examples: List[Dict[str, Any]] = []
    skipped_no_material = 0
    evaluated = 0
    retry_source_rows: List[Dict[str, Any]] = []

    for raw in rows if isinstance(rows, list) else []:
        if not isinstance(raw, dict):
            continue
        t = _v274_ticker(raw)
        if not t or t in STABLE_EXCLUDED:
            continue
        if not _v295_has_strategy_material(raw):
            skipped_no_material += 1
            if _v295_precision_retry_candidate(raw):
                retry_source_rows.append(raw)
            continue
        evaluated += 1
        try:
            item = _v288_candidate_row(raw, market)
        except Exception as exc:
            log_error("v295_candidate_row", exc)
            continue
        action = str(item.get("final_entry_action") or "")
        if action == "paper_open":
            ready_rows.append(item)
        elif action == "recheck_wait":
            recheck_rows.append(item)
        else:
            reason = (item.get("block_reasons") or item.get("final_entry_reasons") or item.get("quality_risk_tags") or ["관찰"])[0]
            rejects[str(reason)] += 1
            if len(examples) < 8:
                examples.append({"ticker": t, "reason": str(reason), "score": fnum(item.get("score"), 0), "line": item.get("one_liner", "")})
            review_rows.append(item)

    _v295_write_precision_retry(retry_source_rows, reason="v295_precision_missing_retry")
    if skipped_no_material:
        rejects["정밀값없음_다음스캔보강"] += skipped_no_material
    ready_rows.sort(key=_v288_rank_key, reverse=True)
    recheck_rows.sort(key=_v288_rank_key, reverse=True)
    review_rows.sort(key=_v288_rank_key, reverse=True)
    strict = [dict(x) for x in (ready_rows[:V288_TRADE_READY_MAX_ROWS] + recheck_rows)[:V288_STRICT_MAX_ROWS]]
    shadow = [dict(x) for x in review_rows[:V288_SHADOW_MAX_ROWS]]
    with _state_lock:
        STATE["v293_evaluated_rows"] = evaluated
        STATE["v293_bulk_skipped_no_material"] = skipped_no_material
        STATE["v295_precision_retry_source_rows"] = len(retry_source_rows)
        STATE["v295_evaluated_rows"] = evaluated
        STATE["v295_precision_skipped_no_material"] = skipped_no_material
        STATE["v288_ready_rows"] = len(ready_rows)
        STATE["v288_recheck_rows"] = len(recheck_rows)
        STATE["v288_review_rows"] = len(review_rows)
        STATE["strict_decision"] = len(strict)
        STATE["shadow_decision"] = len(shadow)
        STATE["latest_trade_ready"] = len([r for r in strict if bool(r.get("trade_ready"))])
        STATE["trade_ready_written"] = STATE["latest_trade_ready"]
        STATE["phase_note"] = "v295: 정밀값 schema 수술 + precision retry 재순환. 조건값 변경 없음."
    return strict, shadow, rejects, examples


def _v295_target_meta(tickers: List[str], reason: str, priority_rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    row_map = {_v274_ticker(r): r for r in (priority_rows or []) if isinstance(r, dict) and _v274_ticker(r)}
    meta: Dict[str, Any] = {}
    for idx, t in enumerate(tickers):
        r = row_map.get(t, {})
        meta[t] = {
            "reason": reason,
            "source": STRATEGY_KEY,
            "priority": max(1, 200 - idx),
            "auto_candidate": bool(r.get("trade_ready") or r.get("paper_bot_open")),
            "score": fnum(r.get("score"), 0.0) if isinstance(r, dict) else 0.0,
            "action": str(r.get("final_entry_action") or "") if isinstance(r, dict) else "",
        }
    return meta


def _v279_write_target_file(path: Path, tickers: List[str], reason: str, priority_rows: Optional[List[Dict[str, Any]]] = None) -> None:  # type: ignore[override]
    nowv = now_ts()
    payload = {
        "version": BOT_VERSION,
        "schema": "v295_sweep_vwap_targets",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "reason": reason,
        "targets": tickers,
        "target_count": len(tickers),
        "target_meta": _v295_target_meta(tickers, reason, priority_rows=priority_rows),
        "force_reconnect": True,
        "reconnect_seq": int(nowv),
        "reconnect_reason": reason,
        "auto_candidate_count": sum(1 for r in (priority_rows or []) if isinstance(r, dict) and bool(r.get("trade_ready") or r.get("paper_bot_open"))),
        "note": "v295: sidecar target 진단 강화. 조건 변경 아님.",
    }
    atomic_write(path, payload)


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "v295_sweep_vwap") -> None:  # type: ignore[override]
    tickers = _v279_target_tickers(rows, priority_rows, limit=180)
    with _ws_lock:
        _ws_targets[:] = tickers
    _v279_write_target_file(FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"), tickers, reason, priority_rows=priority_rows)
    with _state_lock:
        STATE["ws_targets"] = len(tickers)
        STATE["ws_target_file_written"] = 1
        STATE["ws_target_file_targets"] = len(tickers)
        STATE["ws_target_reason"] = reason
        STATE["ws_target_write_note"] = "v295 force reconnect target writer"


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "v295_sweep_vwap") -> None:  # type: ignore[override]
    tickers = _v279_target_tickers(rows, priority_rows, limit=180)
    with _micro_lock:
        _micro_targets[:] = tickers
    _v279_write_target_file(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), tickers, reason, priority_rows=priority_rows)
    with _state_lock:
        STATE["micro_targets"] = len(tickers)
        STATE["micro_target_file_written"] = 1
        STATE["micro_target_file_targets"] = len(tickers)
        STATE["micro_target_reason"] = reason
        STATE["micro_target_write_note"] = "v295 force reconnect target writer"

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v295: 정밀값 schema 수술 + precision retry 재순환 + WS target 진단 보강. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception:
    pass


# ===============================
# v296 factory storage diet + legacy strategy data purge
# - 조건값/청산값 변경 없음.
# - 폐기된 7전략/구전략 분석자료는 삭제/초기화한다.
# - paper OPEN/CLOSED/trade_log 장부는 절대 삭제하지 않는다.
# - 공장 latest 단계에서 command cache/큰 summary 중복저장을 끊는다.
# ===============================

BOT_VERSION = "수익형 v2.13.296"
V296_VERSION_NOTE = "v296: 공장 저장 경량화 + 폐기 전략 자료 자동삭제/순환정리. 조건값 변경 없음."
FILES.setdefault("legacy_cleanup_status", BASE_DIR / "clean_legacy_strategy_cleanup_status.json")

V296_RETENTION_INTERVAL_SEC = float(os.getenv("CLEAN_V296_RETENTION_INTERVAL_SEC", "21600"))  # 6h
V296_TARGET_REWRITE_MIN_SEC = float(os.getenv("CLEAN_V296_TARGET_REWRITE_MIN_SEC", "18"))

_V296_LEDGER_PROTECTED_NAMES = {
    "paper_bot_open.json", "paper_bot_closed.jsonl", "paper_bot_control.json", "paper_bot_status.json",
    "trade_log.jsonl", "trade_log.csv", "paper_trade_log.jsonl", "paper_trade_log.csv",
}

_V296_ACTIVE_CACHE_NAMES = {
    "strategy_material_snapshot.json",
    "strategy_material_latest.jsonl",
    "strategy_lab_summary.json",
    "clean_score_summary.json",
    "clean_quality_summary.json",
    "clean_strategy_watch_summary.json",
    "clean_health_snapshot.json",
    "clean_candidate_snapshot.json",
    "clean_candidate_snapshot_pending.json",
    "clean_reject_summary.json",
    "clean_sweep_precision_retry.json",
}

_V296_PURGE_EXACT = {
    # 구전략/7전략/복기/후보 관찰 산출물. v296 첫 scan에서 현 전략 기준으로 다시 생성된다.
    "candidate_events.jsonl",
    "clean_runtime_events.jsonl",
    "runtime_events.jsonl",
    "paper_bot_shadow_quarantine.jsonl",
    "paper_bot_legacy_strict_quarantine.jsonl",
    "clean_hot_candidate_queue.json",
    "clean_version_score_summary.json",
    "paper_eval_baseline_current_version.json",
    *_V296_ACTIVE_CACHE_NAMES,
}

_V296_TRUNCATE_EXACT = {
    # 후보 archive는 장부가 아니다. paper_bot은 latest 파일을 우선 소비하므로 폐기 전략 후보 archive는 비운다.
    "paper_candidates.jsonl",
    "shadow_candidates.jsonl",
}

_V296_PURGE_PATTERNS = [
    "candidate_events*.jsonl", "*candidate_review*.jsonl", "clean_candidate_review*.jsonl",
    "review_*.json", "review_*.jsonl", "missed_*.jsonl", "missed_*.json",
    "quality_*.json", "clean_quality_*.old", "clean_version_score_summary*.old",
    "strategy_material_*.old", "strategy_lab_*.old", "*.blocked_perm_*",
]


def _v296_is_protected_ledger(path: Path) -> bool:
    try:
        name = path.name
        low = name.lower()
        if name in _V296_LEDGER_PROTECTED_NAMES:
            return True
        if "paper_bot_closed" in low or "paper_bot_open" in low or "trade_log" in low:
            return True
        return False
    except Exception:
        return True


def _v296_unlink(path: Path, dry_run: bool = False) -> Tuple[bool, int, str]:
    try:
        if _v296_is_protected_ledger(path):
            return True, 0, f"protected:{path.name}"
        if not path.exists() or not path.is_file():
            return True, 0, path.name
        size = int(path.stat().st_size)
        if not dry_run:
            path.unlink(missing_ok=True)
        return True, size, path.name
    except Exception as exc:
        return False, 0, f"{path.name}:{exc.__class__.__name__}"


def _v296_truncate(path: Path, dry_run: bool = False) -> Tuple[bool, int, str]:
    try:
        if _v296_is_protected_ledger(path):
            return True, 0, f"protected:{path.name}"
        if not path.exists() or not path.is_file():
            return True, 0, path.name
        size = int(path.stat().st_size)
        if not dry_run:
            path.write_text("", encoding="utf-8")
        return True, size, path.name
    except Exception as exc:
        return False, 0, f"{path.name}:{exc.__class__.__name__}"


def _v296_cleanup_legacy_strategy_data(force: bool = False, reason: str = "v296_periodic", dry_run: bool = False) -> Dict[str, Any]:
    """폐기 전략 자료 삭제/순환정리.

    삭제 대상은 구전략 후보/분석/캐시/리뷰 산출물이다.
    paper OPEN/CLOSED/trade_log 장부는 삭제하지 않는다.
    """
    try:
        nowv = now_ts()
        last = fnum(STATE.get("v296_legacy_cleanup_ts"), 0.0)
        if (not force) and last > 0 and nowv - last < V296_RETENTION_INTERVAL_SEC:
            return {"skipped": True, "reason": "interval", "last_ts": last}
        deleted = truncated = errors = freed = 0
        samples: List[str] = []
        seen: set[str] = set()

        def sample(s: str) -> None:
            if len(samples) < 14:
                samples.append(s)

        # 1) 폐기 전략 산출물은 즉시 삭제/초기화.
        for name in sorted(_V296_PURGE_EXACT):
            p = BASE_DIR / name
            seen.add(str(p))
            ok, size, msg = _v296_unlink(p, dry_run=dry_run)
            if ok:
                if size > 0:
                    deleted += 1; freed += size; sample(f"삭제 {msg}")
            else:
                errors += 1; sample(msg)
        for name in sorted(_V296_TRUNCATE_EXACT):
            p = BASE_DIR / name
            seen.add(str(p))
            ok, size, msg = _v296_truncate(p, dry_run=dry_run)
            if ok:
                if size > 0:
                    truncated += 1; freed += size; sample(f"비움 {msg}")
            else:
                errors += 1; sample(msg)
        for pat in _V296_PURGE_PATTERNS:
            try:
                for p in BASE_DIR.glob(pat):
                    if str(p) in seen or _v296_is_protected_ledger(p):
                        continue
                    ok, size, msg = _v296_unlink(p, dry_run=dry_run)
                    if ok:
                        if size > 0:
                            deleted += 1; freed += size; sample(f"삭제 {msg}")
                    else:
                        errors += 1; sample(msg)
            except Exception as exc:
                errors += 1; sample(f"glob {pat}:{exc.__class__.__name__}")

        # 2) 일반 순환정리: tmp/오래된 백업/gz만 정리. 장부 보호.
        for pat, max_age_days in [("*.tmp", 0), ("*.swp", 0), ("*~", 0), ("bot.py.backup*", 2), ("*.backup*", 2), ("*.gz", 3), ("*.old", 3)]:
            try:
                for p in BASE_DIR.glob(pat):
                    if not p.is_file() or _v296_is_protected_ledger(p):
                        continue
                    try:
                        age = max(0.0, (nowv - float(p.stat().st_mtime)) / 86400.0)
                    except Exception:
                        age = 999.0
                    if age < max_age_days:
                        continue
                    ok, size, msg = _v296_unlink(p, dry_run=dry_run)
                    if ok:
                        if size > 0:
                            deleted += 1; freed += size; sample(f"순환삭제 {msg}")
                    else:
                        errors += 1; sample(msg)
            except Exception as exc:
                errors += 1; sample(f"retention {pat}:{exc.__class__.__name__}")

        result = {
            "version": BOT_VERSION,
            "schema": "v296_legacy_strategy_cleanup",
            "updated_ts": nowv,
            "updated_text": now_text(nowv),
            "reason": reason,
            "deleted": deleted,
            "truncated": truncated,
            "errors": errors,
            "freed_bytes": freed,
            "freed_mb": round(freed / 1024 / 1024, 3),
            "samples": samples,
            "dry_run": dry_run,
            "protected_note": "paper_bot_open/paper_bot_closed/trade_log 장부는 삭제하지 않음",
        }
        if not dry_run:
            save_json(FILES.get("legacy_cleanup_status", BASE_DIR / "clean_legacy_strategy_cleanup_status.json"), result)
            with _state_lock:
                STATE["v296_legacy_cleanup_ts"] = nowv
                STATE["v296_legacy_cleanup_freed_mb"] = result["freed_mb"]
                STATE["v296_legacy_cleanup_deleted"] = deleted
                STATE["v296_legacy_cleanup_truncated"] = truncated
                STATE["v296_legacy_cleanup_errors"] = errors
        return result
    except Exception as exc:
        log_error("v296_cleanup_legacy_strategy_data", exc)
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def _v296_compact_candidate_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """캐시/스냅샷 저장용 row 다이어트. 판단/장부용 latest row는 원본 유지."""
    if not isinstance(row, dict):
        return {}
    keep = [
        "ticker", "strategy_key", "paper_strategy_key", "strategy_label", "final_entry_action", "final_entry_label",
        "trade_ready", "paper_bot_open", "open_eligible", "score", "hold_reason", "block_reasons", "final_entry_reasons", "quality_risk_tags",
        "current_price", "entry_price", "detected_price", "from_30m_low_pct", "below_30m_high_pct", "vwap_gap_pct", "ema5_gap_pct",
        "change_1", "change_3", "turnover_1m", "turnover_3m", "micro_fresh", "micro_row_status", "micro_age_sec",
        "micro_spread_pct", "micro_trade_buy_ratio_30", "micro_ask_wall_pressure", "micro_sell_trade_pressure",
        "ws_fresh", "ws_row_status", "ws_age_sec", "created_at", "expires_at", "scan_id", "brain_version",
    ]
    out = {k: row.get(k) for k in keep if k in row}
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    if ctx:
        out["entry_context"] = {k: ctx.get(k) for k in [
            "paper_strategy_key", "paper_strategy_label", "trade_ready", "score", "final_entry_action",
            "micro_fresh", "ws_fresh", "micro_spread_pct", "micro_trade_buy_ratio_30", "vwap_gap_pct", "from_30m_low_pct", "below_30m_high_pct",
        ] if k in ctx}
    return out


def _v296_rows_signature(rows: List[Dict[str, Any]]) -> str:
    try:
        arr = []
        for r in rows or []:
            arr.append([
                _v274_ticker(r),
                str((r or {}).get("final_entry_action") or ""),
                round(fnum((r or {}).get("score"), 0), 3),
                bool((r or {}).get("trade_ready") or (r or {}).get("paper_bot_open")),
            ])
        return str(hash(json.dumps(arr, ensure_ascii=False, sort_keys=True)))
    except Exception:
        return str(time.time())


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Counter) -> None:  # type: ignore[override]
    """v296: 공장 latest 단계에서 큰 중복 저장과 command cache 생성을 제거한다."""
    rows = list(strict_rows or []) + list(shadow_rows or [])
    compact_rows = [_v296_compact_candidate_row(r) for r in rows[:V288_MATERIAL_MAX_ROWS]]
    nowv = now_ts()
    ready_n = len([r for r in strict_rows or [] if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows or [] if str(r.get("final_entry_action")) == "recheck_wait"])
    urgent_n = len(_v291_urgent_priority_rows(strict_rows or [], shadow_rows or [], limit=80)) if "_v291_urgent_priority_rows" in globals() else 0
    payload = {
        "version": BOT_VERSION,
        "schema": "sweep_vwap_recovery_snapshot_v296_compact",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": ready_n,
        "recheck_count": recheck_n,
        "urgent_watch_count": urgent_n,
        "market_context": {k: market.get(k) for k in list((market or {}).keys())[:24]} if isinstance(market, dict) else {},
        "rejects": dict((rejects or {}).most_common(24)) if hasattr(rejects, "most_common") else dict(rejects or {}),
        "rows": compact_rows,
        "note": "v296: 폐기 전략 자료 삭제 후 sweep_vwap_recovery 단일전략 compact snapshot. 조건값 변경 없음.",
    }
    sig = _v296_rows_signature(compact_rows)
    last_sig = str(STATE.get("v296_strategy_snapshot_sig") or "")
    # 후보/요약이 같으면 큰 snapshot은 반복저장하지 않는다. command cache는 scan 마지막에만 저장된다.
    if sig != last_sig or nowv - fnum(STATE.get("v296_strategy_snapshot_ts"), 0) > 45:
        save_json(FILES["strategy_material_snapshot"], payload)
        write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
        save_json(FILES["strategy_lab_summary"], {
            "version": BOT_VERSION,
            "updated_ts": nowv,
            "updated_text": now_text(nowv),
            "strategy_key": STRATEGY_KEY,
            "strategy_label": STRATEGY_LABEL,
            "strict_count": payload["strict_count"],
            "shadow_count": payload["shadow_count"],
            "trade_ready_count": ready_n,
            "recheck_count": recheck_n,
            "urgent_watch_count": urgent_n,
            "rejects": payload["rejects"],
            "note": payload["note"],
        })
        with _state_lock:
            STATE["v296_strategy_snapshot_sig"] = sig
            STATE["v296_strategy_snapshot_ts"] = nowv
            STATE["v296_strategy_snapshot_written"] = len(compact_rows)
    else:
        with _state_lock:
            STATE["v296_strategy_snapshot_skipped"] = int(STATE.get("v296_strategy_snapshot_skipped", 0) or 0) + 1


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v296: latest 공장 경량화.

    - paper/shadow latest는 유지한다.
    - 후보 archive append는 하지 않는다.
    - command cache는 scan 마지막 한 번만 생성한다.
    - 폐기 전략 자료 cleanup은 주기적으로만 실행한다.
    """
    _v296_cleanup_legacy_strategy_data(force=False, reason="v296_periodic_from_factory")
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_rows = [_v288_refresh_latest_row(r, "strict", scan_id) for r in (strict or [])]
    shadow_rows = [_v288_refresh_latest_row(r, "shadow", scan_id) for r in (shadow or [])]
    urgent_priority = _v291_urgent_priority_rows(strict_rows, shadow_rows, limit=80) if "_v291_urgent_priority_rows" in globals() else []
    _v288_write_micro_urgent(urgent_priority, reason="v296_sweep_vwap_external_wait")
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        rejects = Counter()
        for r in shadow_rows:
            for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
                rejects[str(x)] += 1
        _v288_write_strategy_files(strict_rows, shadow_rows, market, rejects)
    except Exception as exc:
        log_error("v296_strategy_snapshot_write", exc)
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v296_urgent_watch_rows"] = len(urgent_priority)
        STATE["factory_mode"] = "v296_sweep_vwap_compact_factory"
        STATE["write_error"] = write_error
    return {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
        "paper_written": ready_n,
        "shadow_written": len(shadow_rows),
        "trade_ready_written": ready_n,
        "strict_observe_written": max(0, len(strict_rows)-ready_n),
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_strict_observe": max(0, len(strict_rows)-ready_n),
        "latest_final_recheck_wait": recheck_n,
        "latest_final_observe": max(0, len(strict_rows)-ready_n),
        "urgent_watch_rows": len(urgent_priority),
        "dup_skip_reason": {},
        "data_quality_note": "v296_sweep_vwap_compact_factory",
        "dup_skip": 0,
        "write_error": write_error,
        "last_ticker": str((strict_rows or shadow_rows or [{"ticker":"-"}])[-1].get("ticker") or "-"),
        "factory_mode": "v296_sweep_vwap_compact_factory",
        "archive_deferred": 1,
        "risk_sync_deferred": True,
    }


def _v296_target_hash_payload(tickers: List[str], priority_rows: Optional[List[Dict[str, Any]]]) -> str:
    try:
        meta = []
        row_map = {_v274_ticker(r): r for r in (priority_rows or []) if isinstance(r, dict)}
        for t in tickers or []:
            r = row_map.get(t, {})
            meta.append([t, bool(r.get("trade_ready") or r.get("paper_bot_open")), str(r.get("final_entry_action") or ""), round(fnum(r.get("score"), 0), 2)])
        return str(hash(json.dumps(meta, ensure_ascii=False, sort_keys=True)))
    except Exception:
        return str(time.time())


def _v296_write_target_file_if_changed(path: Path, tickers: List[str], reason: str, priority_rows: Optional[List[Dict[str, Any]]] = None) -> bool:
    nowv = now_ts()
    sig = _v296_target_hash_payload(tickers, priority_rows)
    prev = load_json(path, {})
    prev_sig = str(prev.get("target_sig") or "") if isinstance(prev, dict) else ""
    prev_ts = fnum(prev.get("updated_ts"), 0.0) if isinstance(prev, dict) else 0.0
    if sig == prev_sig and prev_ts > 0 and nowv - prev_ts < V296_TARGET_REWRITE_MIN_SEC:
        return False
    payload = {
        "version": BOT_VERSION,
        "schema": "v296_sweep_vwap_targets_compact",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "reason": reason,
        "targets": tickers,
        "target_count": len(tickers),
        "target_sig": sig,
        "target_meta": _v295_target_meta(tickers, reason, priority_rows=priority_rows) if "_v295_target_meta" in globals() else {t: {"reason": reason, "source": STRATEGY_KEY} for t in tickers},
        "force_reconnect": sig != prev_sig,
        "reconnect_seq": int(nowv) if sig != prev_sig else prev.get("reconnect_seq", int(nowv)) if isinstance(prev, dict) else int(nowv),
        "reconnect_reason": reason if sig != prev_sig else "same_targets_keep_subscription",
        "auto_candidate_count": sum(1 for r in (priority_rows or []) if isinstance(r, dict) and bool(r.get("trade_ready") or r.get("paper_bot_open"))),
        "note": "v296: target 변경 시에만 sidecar 재구독. 조건 변경 아님.",
    }
    atomic_write(path, payload)
    return True


def update_ws_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "v296_sweep_vwap") -> None:  # type: ignore[override]
    tickers = _v279_target_tickers(rows, priority_rows, limit=180)
    with _ws_lock:
        _ws_targets[:] = tickers
    wrote = _v296_write_target_file_if_changed(FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"), tickers, reason, priority_rows=priority_rows)
    with _state_lock:
        STATE["ws_targets"] = len(tickers)
        STATE["ws_target_file_written"] = 1 if wrote else 0
        STATE["ws_target_file_targets"] = len(tickers)
        STATE["ws_target_reason"] = reason
        STATE["ws_target_write_note"] = "v296 changed-write" if wrote else "v296 same-target skip"


def update_micro_targets(rows: List[Dict[str, Any]], priority_rows: Optional[List[Dict[str, Any]]] = None, reason: str = "v296_sweep_vwap") -> None:  # type: ignore[override]
    tickers = _v279_target_tickers(rows, priority_rows, limit=180)
    with _micro_lock:
        _micro_targets[:] = tickers
    wrote = _v296_write_target_file_if_changed(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), tickers, reason, priority_rows=priority_rows)
    with _state_lock:
        STATE["micro_targets"] = len(tickers)
        STATE["micro_target_file_written"] = 1 if wrote else 0
        STATE["micro_target_file_targets"] = len(tickers)
        STATE["micro_target_reason"] = reason
        STATE["micro_target_write_note"] = "v296 changed-write" if wrote else "v296 same-target skip"


_v296_base_scan_once = globals().get("scan_once")

def scan_once() -> List[Dict[str, Any]]:  # type: ignore[override]
    # cleanup은 조건/전략과 무관한 자료 정리다. interval이 아니면 즉시 skip된다.
    _v296_cleanup_legacy_strategy_data(force=False, reason="v296_scan_interval")
    if callable(_v296_base_scan_once):
        return _v296_base_scan_once()
    return []

# 첫 부팅 시 폐기 전략 자료를 실제 정리한다. import 검수 때도 장부 보호 규칙은 유지된다.
try:
    STATE["v296_legacy_cleanup_ts"] = now_ts()  # v297: startup force purge disabled; cleanup is periodic/nonblocking
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v296: 공장 저장 경량화 + 폐기 전략 자료 삭제/순환정리. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
except Exception as exc:
    log_error("v296_init", exc)


# ===============================
# v2.13.297: command responsiveness + factory target/snapshot ultra-light
# - 조건/익절/손절/시간청산 변경 없음.
# - v296의 부팅/스캔 중 cleanup 강제 실행을 끊어 첫 응답 지연을 막는다.
# - target hash는 ticker 목록 기준으로만 비교해 불필요한 재구독/파일쓰기를 줄인다.
# - candidate_snapshot은 compact row로만 저장하고, 동일하면 반복 저장하지 않는다.
# - factory 단계에서 cleanup 호출을 하지 않는다. 구전략 자료 정리는 가드봇 /gcleanup 또는 주기 retention에 맡긴다.
# ===============================

BOT_VERSION = "수익형 v2.13.297"
V297_VERSION_NOTE = "v297: 첫응답 지연 방지 + 공장 snapshot/target 초경량화. 조건값 변경 없음."
V297_SNAPSHOT_TTL_SEC = float(os.getenv("CLEAN_V297_SNAPSHOT_TTL_SEC", "45"))
V297_TARGET_REWRITE_MIN_SEC = float(os.getenv("CLEAN_V297_TARGET_REWRITE_MIN_SEC", "45"))


def _v297_ticker_list_signature(tickers: List[str]) -> str:
    try:
        arr = sorted([_ticker_from_any(t) for t in (tickers or []) if _ticker_from_any(t)])
        return str(hash(json.dumps(arr, ensure_ascii=False, sort_keys=True)))
    except Exception:
        return str(hash(tuple(tickers or [])))


def _v296_target_hash_payload(tickers: List[str], priority_rows: Optional[List[Dict[str, Any]]]) -> str:  # type: ignore[override]
    """v297: target 파일 재작성 여부는 ticker 목록만 본다.

    v296은 score/action까지 signature에 넣어 같은 tickers라도 매 scan 파일을 다시 쓰고
    sidecar 재구독을 유발할 수 있었다. target 파일 목적은 '무엇을 수집할지'라서 ticker 목록만
    바뀌었을 때 재작성한다. 조건/전략판단 변경 아님.
    """
    return _v297_ticker_list_signature(tickers or [])


def _v296_write_target_file_if_changed(path: Path, tickers: List[str], reason: str, priority_rows: Optional[List[Dict[str, Any]]] = None) -> bool:  # type: ignore[override]
    nowv = now_ts()
    tickers = [_ticker_from_any(t) for t in (tickers or []) if _ticker_from_any(t)]
    sig = _v297_ticker_list_signature(tickers)
    prev = load_json(path, {})
    prev_sig = str(prev.get("target_sig") or "") if isinstance(prev, dict) else ""
    prev_ts = fnum(prev.get("updated_ts"), 0.0) if isinstance(prev, dict) else 0.0
    if sig == prev_sig and prev_ts > 0 and nowv - prev_ts < V297_TARGET_REWRITE_MIN_SEC:
        return False
    # meta는 sidecar 수집에는 필수 아님. 너무 큰 meta가 target 저장/재구독 병목을 만들지 않게 핵심만 남긴다.
    row_map = {_v274_ticker(r): r for r in (priority_rows or []) if isinstance(r, dict)}
    meta: Dict[str, Any] = {}
    for t in tickers[:120]:
        r = row_map.get(t) or {}
        meta[t] = {
            "source": STRATEGY_KEY,
            "reason": reason,
            "priority": 150 if bool(r.get("trade_ready") or r.get("paper_bot_open")) else 120,
            "action": str(r.get("final_entry_action") or r.get("hold_reason") or "watch")[:40],
        }
    payload = {
        "version": BOT_VERSION,
        "schema": "v297_sweep_vwap_targets_light",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "reason": reason,
        "targets": tickers,
        "target_count": len(tickers),
        "target_sig": sig,
        "target_meta": meta,
        "force_reconnect": sig != prev_sig,
        "reconnect_seq": int(nowv) if sig != prev_sig else (prev.get("reconnect_seq", int(nowv)) if isinstance(prev, dict) else int(nowv)),
        "reconnect_reason": reason if sig != prev_sig else "same_ticker_targets_keep_subscription",
        "auto_candidate_count": sum(1 for r in (priority_rows or []) if isinstance(r, dict) and bool(r.get("trade_ready") or r.get("paper_bot_open"))),
        "note": "v297: target 저장은 ticker 목록 변경/TTL 때만. 조건 변경 아님.",
    }
    atomic_write(path, payload)
    return True


def _v297_compact_snapshot_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return _v296_compact_candidate_row(row) if '_v296_compact_candidate_row' in globals() else dict(row or {})


def _v293_write_light_candidate_snapshot(priority_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], stage: str, wait_sec: float = 0.0) -> Dict[str, Any]:  # type: ignore[override]
    rows = [_v297_compact_snapshot_row(r) for r in (priority_rows or []) if isinstance(r, dict)]
    shadow = [_v297_compact_snapshot_row(r) for r in (shadow_rows or []) if isinstance(r, dict)]
    nowv = now_ts()
    ext = {
        "total": len(rows),
        "ws_fresh": sum(1 for r in rows if bool(r.get("ws_fresh")) or str(r.get("ws_row_status") or "") == "fresh"),
        "micro_fresh": sum(1 for r in rows if bool(r.get("micro_fresh")) or str(r.get("micro_row_status") or "") == "fresh"),
    }
    sig = _v296_rows_signature(rows) if '_v296_rows_signature' in globals() else str(hash(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)))
    last_sig = str(STATE.get("v297_candidate_snapshot_sig") or "")
    last_ts = fnum(STATE.get("v297_candidate_snapshot_ts"), 0.0)
    data = {
        "version": BOT_VERSION,
        "schema": "candidate_snapshot_v297_compact",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or ""),
        "stage": stage,
        "candidate_count": len(rows),
        "shadow_count": len(shadow),
        "external": ext,
        "rows": rows[:70],
        "shadow_rows": shadow[:20],
        "wait_sec": wait_sec,
        "note": "v297: compact snapshot, same payload skip. 조건값 변경 없음.",
    }
    if sig != last_sig or nowv - last_ts > V297_SNAPSHOT_TTL_SEC:
        try:
            save_json(FILES.get("candidate_snapshot", BASE_DIR / "clean_candidate_snapshot.json"), data)
            with _state_lock:
                STATE["v297_candidate_snapshot_sig"] = sig
                STATE["v297_candidate_snapshot_ts"] = nowv
                STATE["v297_candidate_snapshot_written"] = len(rows)
        except Exception as exc:
            log_error("v297_compact_candidate_snapshot", exc)
    else:
        with _state_lock:
            STATE["v297_candidate_snapshot_skipped"] = int(STATE.get("v297_candidate_snapshot_skipped", 0) or 0) + 1
    return data


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v297: factory latest path without cleanup.

    v296에서 factory마다 cleanup interval check와 큰 snapshot 경로가 섞여 첫 응답/scan 체감이 나빠졌다.
    여기서는 paper/shadow latest, micro urgent, compact summary만 처리한다.
    """
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_rows = [_v288_refresh_latest_row(r, "strict", scan_id) for r in (strict or [])]
    shadow_rows = [_v288_refresh_latest_row(r, "shadow", scan_id) for r in (shadow or [])]
    urgent_priority = _v291_urgent_priority_rows(strict_rows, shadow_rows, limit=80) if "_v291_urgent_priority_rows" in globals() else []
    _v288_write_micro_urgent(urgent_priority, reason="v297_sweep_vwap_external_wait")
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        rejects = Counter()
        for r in shadow_rows:
            for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
                rejects[str(x)] += 1
        _v288_write_strategy_files(strict_rows, shadow_rows, market, rejects)
    except Exception as exc:
        log_error("v297_strategy_summary_write", exc)
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v296_urgent_watch_rows"] = len(urgent_priority)
        STATE["factory_mode"] = "v297_sweep_vwap_light_factory"
        STATE["write_error"] = write_error
    return {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
        "paper_written": ready_n,
        "shadow_written": len(shadow_rows),
        "trade_ready_written": ready_n,
        "strict_observe_written": max(0, len(strict_rows)-ready_n),
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_strict_observe": max(0, len(strict_rows)-ready_n),
        "latest_final_recheck_wait": recheck_n,
        "latest_final_observe": max(0, len(strict_rows)-ready_n),
        "urgent_watch_rows": len(urgent_priority),
        "dup_skip_reason": {},
        "data_quality_note": "v297_sweep_vwap_light_factory",
        "dup_skip": 0,
        "write_error": write_error,
        "last_ticker": str((strict_rows or shadow_rows or [{"ticker":"-"}])[-1].get("ticker") or "-"),
        "factory_mode": "v297_sweep_vwap_light_factory",
        "archive_deferred": 1,
        "risk_sync_deferred": True,
    }


_v297_base_scan_once = globals().get("_v296_base_scan_once") or globals().get("scan_once")

def scan_once() -> List[Dict[str, Any]]:  # type: ignore[override]
    # v297: scan path에서는 cleanup을 실행하지 않는다. 파일 정리는 별도 retention/guard 경로.
    if callable(_v297_base_scan_once):
        return _v297_base_scan_once()
    return []

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v297: 첫응답 지연 방지 + 공장 snapshot/target 초경량화. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
    # v296 cleanup이 scan/factory에서 즉시 돌지 않도록 타임스탬프를 현재로 둔다.
    STATE["v296_legacy_cleanup_ts"] = now_ts()
except Exception as exc:
    log_error("v297_init", exc)


# ===============================
# v2.13.298: adaptive information pipeline without fixed target-count goal
# - 조건/익절/손절/시간청산 변경 없음.
# - "정밀값 몇 개 회복" 같은 고정 개수 목표를 두지 않는다.
# - 전체시장 허브는 넓게 유지하고, 정밀대상은 우선순위 큐로 장세/정보부족/urgent/retry/OPEN에 따라 자연스럽게 늘고 줄게 한다.
# - 전략평가는 실제 정밀값이 붙은 row만 태운다.
# - health/score/quality/strategy_watch는 같은 latest snapshot 기준으로 통일한다.
# - target/snapshot은 가볍게 유지하되 정보 배관을 끊지 않는다.
# ===============================

BOT_VERSION = "수익형 v2.13.298"
V298_VERSION_NOTE = "v298: 고정개수 통제 제거, 정밀/urgent 우선순위 큐 단일화, latest snapshot 기준 통일. 조건값 변경 없음."
V298_TARGET_REWRITE_MIN_SEC = float(os.getenv("CLEAN_V298_TARGET_REWRITE_MIN_SEC", "18"))
V298_SNAPSHOT_TTL_SEC = float(os.getenv("CLEAN_V298_SNAPSHOT_TTL_SEC", "18"))
V298_DIRECT_COLD_BURST_GUARD = int(os.getenv("CLEAN_V298_DIRECT_COLD_BURST_GUARD", "12"))
V298_TARGET_SAFETY_MAX = int(os.getenv("CLEAN_V298_TARGET_SAFETY_MAX", str(PRECISION_REFRESH_MAX_SAFETY)))


def _v298_ticker(row: Any) -> str:
    try:
        return _v274_ticker(row)
    except Exception:
        return _ticker_from_any((row or {}).get("ticker") if isinstance(row, dict) else row)


def _v298_push_unique_rows(out: List[Dict[str, Any]], seen: set, row: Optional[Dict[str, Any]], source: str = "") -> None:
    if not isinstance(row, dict):
        return
    t = _v298_ticker(row)
    if not t or t in STABLE_EXCLUDED or t in seen:
        return
    rr = dict(row)
    if source:
        rr["precision_target_source"] = source
    seen.add(t)
    out.append(rr)


def _v298_priority_precision_tickers() -> List[str]:
    """정밀 우선순위 ticker 목록.

    고정 N개 목표가 아니라, 이전 scan에서 정보가 부족했던 대상과 sidecar target을
    다음 scan 정밀 보강 앞줄로 되돌린다. 숫자는 결과값이며, 서버 보호 상한은
    select_precision_targets에서만 적용한다.
    """
    out: List[str] = []
    seen: set[str] = set()
    paths = [
        FILES.get("sweep_precision_retry", BASE_DIR / "clean_sweep_precision_retry.json"),
        FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"),
        FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"),
        FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"),
    ]
    for p in paths:
        for t in _v295_read_target_payload_tickers(Path(p), ttl_sec=300.0) if "_v295_read_target_payload_tickers" in globals() else []:
            t = _ticker_from_any(t)
            if t and t not in STABLE_EXCLUDED and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _v298_precision_candidate_score(row: Dict[str, Any], priority_set: Optional[set] = None) -> float:
    """정밀대상 우선순위 점수. 전략 조건이 아니라 정보수집 순서다."""
    if not isinstance(row, dict):
        return -1.0
    t = _v298_ticker(row)
    if not t or t in STABLE_EXCLUDED:
        return -1.0
    priority_set = priority_set or set()
    rank = fint(row.get("turnover_rank"), 9999)
    turnover24 = fnum(row.get("turnover_24h") or row.get("money_proxy_24h") or row.get("acc_trade_value_24h"), 0.0)
    ch24 = abs(fnum(row.get("change_24h") or row.get("fluctate_rate_24H") or row.get("change_rate"), 0.0))
    price = fnum(row.get("current_price") or row.get("price"), 0.0)
    external = bool(row.get("ws_fresh")) or bool(row.get("micro_fresh")) or str(row.get("ws_row_status") or "") == "fresh" or str(row.get("micro_row_status") or "") == "fresh"
    open_bonus = 1.0 if t in { _v298_ticker(x) for x in (read_open().values() if 'read_open' in globals() else []) if isinstance(x, dict) } else 0.0
    retry_bonus = 4.0 if t in priority_set else 0.0
    rank_score = max(0.0, 240.0 - float(rank)) / 240.0 if rank < 9999 else 0.0
    money_score = min(3.0, turnover24 / 1_000_000_000.0)
    move_score = min(2.5, ch24 / max(0.1, PRECISION_MOVE_TRIGGER_PCT))
    ext_score = 1.5 if external else 0.0
    return retry_bonus + open_bonus + ext_score + rank_score + money_score + move_score + (0.1 if price > 0 else 0.0)


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v298 정밀대상 선정.

    목표 개수로 자르지 않는다. 전체시장 row 중 실제 정밀 보강 필요성이 있는 애를
    우선순위 큐로 올리고, 서버 보호용 safety max만 둔다. safety max는 전략 제한이 아니다.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict) and _v298_ticker(r) and _v298_ticker(r) not in STABLE_EXCLUDED]
    by_ticker: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        t = _v298_ticker(r)
        if not t:
            continue
        old = by_ticker.get(t)
        if old is None or _v298_precision_candidate_score(r) > _v298_precision_candidate_score(old):
            by_ticker[t] = r
    priority_tickers = _v298_priority_precision_tickers()
    priority_set = set(priority_tickers)
    selected: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for t in priority_tickers:
        _v298_push_unique_rows(selected, seen, by_ticker.get(t), "retry_urgent_target")

    # 나머지는 고정 개수 목표가 아니라 정보수집 필요 점수로 정렬한다.
    candidates = [r for t, r in by_ticker.items() if t not in seen]
    scored = [( _v298_precision_candidate_score(r, priority_set), r) for r in candidates]
    # 너무 의미 없는 bulk row는 큐에 태우지 않는다. 임계값은 개수 제한이 아니라 정보수집 필요성 하한이다.
    meaningful = [(s, r) for s, r in scored if s >= 0.65 or fint(r.get("turnover_rank"), 9999) <= PRECISION_TURNOVER_CORE_RANK or abs(fnum(r.get("change_24h") or r.get("change_rate"), 0.0)) >= PRECISION_MOVE_TRIGGER_PCT]
    meaningful.sort(key=lambda x: x[0], reverse=True)
    for _score, r in meaningful:
        _v298_push_unique_rows(selected, seen, r, "adaptive_priority_queue")
        if len(selected) >= max(1, V298_TARGET_SAFETY_MAX):
            break

    with _state_lock:
        STATE["precision_target_note"] = f"v298 adaptive priority queue selected {len(selected)} / priority {len([t for t in priority_tickers if t in by_ticker])} / source {len(rows)}"
        STATE["precision_target_source"] = "v298_adaptive_priority_queue"
        STATE["v298_precision_priority_count"] = len([t for t in priority_tickers if t in by_ticker])
        STATE["v298_precision_safety_max"] = V298_TARGET_SAFETY_MAX
    return selected


def refresh_precision(rows: List[Dict[str, Any]], targets: Optional[List[Dict[str, Any]]] = None) -> Tuple[int, int, int]:  # type: ignore[override]
    """v298 정밀보강.

    target 수를 맞추려 하지 않고, 큐 등록 + 서버 상황에 맞는 직접 보강만 한다.
    캐시가 차가운 시작 직후에는 약간 더 직접 보강하되, scan이 느려지면 자동으로 줄인다.
    """
    targets = targets if targets is not None else select_precision_targets(rows)
    queued = enqueue_precision_targets(targets)
    nowv = now_ts()
    target_tickers = [_v298_ticker(r) for r in (targets or []) if _v298_ticker(r)]
    with _precision_lock:
        fresh_selected = sum(1 for t in target_tickers if (_precision_cache.get(t) or {}).get("precision_ok") and nowv - fnum((_precision_cache.get(t) or {}).get("precision_ts"), 0) <= PRECISION_TTL_SEC * 3)
        cached_ok_now = sum(1 for v in _precision_cache.values() if v.get("precision_ok") and nowv - fnum(v.get("precision_ts"), 0) <= PRECISION_TTL_SEC * 3)
    stale_targets = []
    for r in targets or []:
        t = _v298_ticker(r)
        if t and is_precision_stale(t, nowv=nowv):
            stale_targets.append(r)
    adaptive_budget = adaptive_precision_budget(rows, targets)
    last_done = fnum(STATE.get("last_done_scan_sec"), 0.0)
    selected_count = max(1, len(target_tickers))
    selected_fresh_ratio = fresh_selected / selected_count
    if last_done >= 14.0:
        direct_guard = PRECISION_SYNC_MAX_PER_SCAN
    elif selected_fresh_ratio < 0.20:
        # cold start: 직접 보강을 조금 넓히되, safety guard 안에서만 움직인다.
        direct_guard = max(PRECISION_SYNC_COLD_MAX_PER_SCAN, min(V298_DIRECT_COLD_BURST_GUARD, max(1, int(len(stale_targets) * 0.20) + 4)))
    else:
        direct_guard = PRECISION_SYNC_MAX_PER_SCAN
    sync_limit = max(0, min(adaptive_budget, direct_guard, len(stale_targets)))
    urgent = []
    for r in sorted(stale_targets, key=precision_priority):
        urgent.append(r)
        if len(urgent) >= sync_limit:
            break
    with _state_lock:
        STATE["precision_need"] = len(stale_targets)
        STATE["precision_budget"] = adaptive_budget
        STATE["precision_sync_limit"] = sync_limit
        STATE["precision_queue_size"] = _precision_queue.qsize()
        STATE["precision_background_note"] = f"v298 큐 {STATE.get('precision_queue_size',0)} / 등록 {queued} / 직접 {len(urgent)} / 대상신선 {fresh_selected}/{len(target_tickers)}"
        STATE["v298_selected_fresh_ratio"] = round(selected_fresh_ratio, 3)
    ok = fail = 0
    if urgent:
        def one(r: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
            t = _v298_ticker(r)
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


def _v298_common_rows(limit: int = 120) -> List[Dict[str, Any]]:
    return _v274_read_material_rows(limit)


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Counter) -> None:  # type: ignore[override]
    """v298: latest snapshot 기준 단일화.

    snapshot은 compact row만 저장하므로 매 scan 저장해도 부담이 작다. health/score/quality/watch가
    같은 파일을 읽게 해 숫자 불일치를 막는다.
    """
    rows = list(strict_rows or []) + list(shadow_rows or [])
    compact_rows = [_v297_compact_snapshot_row(r) if '_v297_compact_snapshot_row' in globals() else _v296_compact_candidate_row(r) for r in rows[:V288_MATERIAL_MAX_ROWS]]
    nowv = now_ts()
    ready_n = len([r for r in strict_rows or [] if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows or [] if str(r.get("final_entry_action")) == "recheck_wait"])
    payload = {
        "version": BOT_VERSION,
        "schema": "sweep_vwap_recovery_snapshot_v298_latest",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or ""),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": ready_n,
        "recheck_count": recheck_n,
        "market_context": {k: market.get(k) for k in list((market or {}).keys())[:24]} if isinstance(market, dict) else {},
        "rejects": dict((rejects or {}).most_common(32)) if hasattr(rejects, "most_common") else dict(rejects or {}),
        "rows": compact_rows,
        "note": "v298: 명령어는 이 latest snapshot 하나를 기준으로 읽음. 조건값 변경 없음.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
    save_json(FILES["strategy_lab_summary"], {
        "version": BOT_VERSION,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": payload["scan_id"],
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": payload["strict_count"],
        "shadow_count": payload["shadow_count"],
        "trade_ready_count": ready_n,
        "recheck_count": recheck_n,
        "rejects": payload["rejects"],
        "note": payload["note"],
    })
    with _state_lock:
        STATE["v298_strategy_snapshot_written"] = len(compact_rows)
        STATE["v298_strategy_snapshot_ts"] = nowv


def _v298_target_rows(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if "_v291_urgent_priority_rows" in globals():
        return _v291_urgent_priority_rows(strict_rows or [], shadow_rows or [], limit=80)
    return [r for r in list(strict_rows or []) + list(shadow_rows or []) if isinstance(r, dict) and ("micro" in ",".join(_v288_as_list(r.get("final_entry_reasons"))) or "WS" in ",".join(_v288_as_list(r.get("final_entry_reasons"))))]


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v298: 가벼운 공장 + 정보 target 연결 유지."""
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_rows = [_v288_refresh_latest_row(r, "strict", scan_id) for r in (strict or [])]
    shadow_rows = [_v288_refresh_latest_row(r, "shadow", scan_id) for r in (shadow or [])]
    urgent_priority = _v298_target_rows(strict_rows, shadow_rows)
    _v288_write_micro_urgent(urgent_priority, reason="v298_sweep_vwap_external_wait")
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        rejects = Counter()
        for r in shadow_rows:
            for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
                rejects[str(x)] += 1
        _v288_write_strategy_files(strict_rows, shadow_rows, market, rejects)
    except Exception as exc:
        log_error("v298_strategy_summary_write", exc)
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v298_urgent_watch_rows"] = len(urgent_priority)
        STATE["factory_mode"] = "v298_sweep_vwap_latest_factory"
        STATE["write_error"] = write_error
    return {
        "paper_attempt": len(strict or []),
        "shadow_attempt": len(shadow or []),
        "paper_written": ready_n,
        "shadow_written": len(shadow_rows),
        "trade_ready_written": ready_n,
        "strict_observe_written": max(0, len(strict_rows)-ready_n),
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_strict_observe": max(0, len(strict_rows)-ready_n),
        "latest_final_recheck_wait": recheck_n,
        "latest_final_observe": max(0, len(strict_rows)-ready_n),
        "urgent_watch_rows": len(urgent_priority),
        "dup_skip_reason": {},
        "data_quality_note": "v298_sweep_vwap_latest_factory",
        "dup_skip": 0,
        "write_error": write_error,
        "last_ticker": str((strict_rows or shadow_rows or [{"ticker":"-"}])[-1].get("ticker") or "-"),
        "factory_mode": "v298_sweep_vwap_latest_factory",
        "archive_deferred": 1,
        "risk_sync_deferred": True,
    }


def _v296_write_target_file_if_changed(path: Path, tickers: List[str], reason: str, priority_rows: Optional[List[Dict[str, Any]]] = None) -> bool:  # type: ignore[override]
    """v298: target 저장은 가볍게 유지하되, 정보 수집 대상이 끊기지 않게 한다."""
    nowv = now_ts()
    tickers = list(dict.fromkeys([_ticker_from_any(t) for t in (tickers or []) if _ticker_from_any(t)]))
    sig = _v297_ticker_list_signature(tickers) if "_v297_ticker_list_signature" in globals() else str(hash(tuple(sorted(tickers))))
    prev = load_json(path, {})
    prev_sig = str(prev.get("target_sig") or "") if isinstance(prev, dict) else ""
    prev_ts = fnum(prev.get("updated_ts"), 0.0) if isinstance(prev, dict) else 0.0
    # 같은 ticker라도 너무 오래 두면 sidecar 재구독/상태 갱신이 안 보일 수 있으므로 TTL 후 재작성한다.
    if sig == prev_sig and prev_ts > 0 and nowv - prev_ts < V298_TARGET_REWRITE_MIN_SEC:
        return False
    row_map = {_v298_ticker(r): r for r in (priority_rows or []) if isinstance(r, dict)}
    meta: Dict[str, Any] = {}
    for t in tickers:
        r = row_map.get(t) or {}
        meta[t] = {
            "source": STRATEGY_KEY,
            "reason": reason,
            "priority": 150 if bool(r.get("trade_ready") or r.get("paper_bot_open")) else 120,
            "action": str(r.get("final_entry_action") or r.get("hold_reason") or "watch")[:40],
        }
    payload = {
        "version": BOT_VERSION,
        "schema": "v298_sweep_vwap_targets_light",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "reason": reason,
        "targets": tickers,
        "target_count": len(tickers),
        "target_sig": sig,
        "target_meta": meta,
        "force_reconnect": sig != prev_sig,
        "reconnect_seq": int(nowv) if sig != prev_sig else (prev.get("reconnect_seq", int(nowv)) if isinstance(prev, dict) else int(nowv)),
        "reconnect_reason": reason if sig != prev_sig else "same_ticker_targets_periodic_refresh",
        "auto_candidate_count": sum(1 for r in (priority_rows or []) if isinstance(r, dict) and bool(r.get("trade_ready") or r.get("paper_bot_open"))),
        "note": "v298: target은 ticker 목록/TTL 기준으로만 가볍게 갱신. 조건 변경 아님.",
    }
    atomic_write(path, payload)
    return True


def _v288_write_command_caches(reason: str = "manual") -> None:  # type: ignore[override]
    """v298: 기본 명령어 캐시는 같은 latest snapshot 기준으로 한 번에 작성한다."""
    try:
        rows = _v298_common_rows(120)
        score = _v288_score_text(rows)
        quality = _v288_quality_text(False)
        watch = _v288_strategy_watch_text(False)
        health = _v288_health_text()
        save_json(FILES.get("score_summary", BASE_DIR / "clean_score_summary.json"), _v288_cache_payload(score, "score"))
        save_json(FILES.get("quality_summary", BASE_DIR / "clean_quality_summary.json"), _v288_cache_payload(quality, "quality"))
        save_json(FILES.get("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json"), _v288_cache_payload(watch, "strategy_watch"))
        save_json(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), _v288_cache_payload(health, "health"))
    except Exception as exc:
        log_error("v298_write_command_caches", exc)


try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = "v298: 고정개수 목표 없이 정보 우선순위 큐/명령 캐시 단일화. 조건값 변경 없음."
    STATE["v288_single_strategy"] = STRATEGY_KEY
    STATE["v296_legacy_cleanup_ts"] = now_ts()
except Exception as exc:
    log_error("v298_init", exc)



# =============================================================================
# v2.13.300: leader momentum continuation single strategy
# 설계서 확정값:
# - 장세 선택형 주도코인 추세 지속 전략
# - +2.0% 익절 / -0.60% 손절 / 45분 시간청산 / 장세필터 강함
# - 정보배관은 v298 adaptive queue를 유지하고, 전략 조건만 새 설계서로 교체한다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.300"
STRATEGY_KEY = "leader_momentum_continuation"
STRATEGY_NAME = "장세 선택형 주도코인 추세 지속 전략"
STRATEGY_LABEL = STRATEGY_NAME
V300_NOTE = "v300: sweep_vwap 폐기 후 leader_momentum_continuation 단일전략 투입. 자동매수 OFF / paper 검증 전용."

# 청산 설계 고정값. 이름은 기존 paper/context 호환을 위해 V288_*를 그대로 재사용한다.
V288_TAKE_PROFIT_PCT = float(os.getenv("CLEAN_LEADER_TAKE_PROFIT_PCT", "2.00"))
V288_PROTECT_TRIGGER_PCT = float(os.getenv("CLEAN_LEADER_PROTECT_TRIGGER_PCT", "1.00"))
V288_PROTECT_FLOOR_PCT = float(os.getenv("CLEAN_LEADER_PROTECT_FLOOR_PCT", "0.45"))
V288_STOP_LOSS_PCT = float(os.getenv("CLEAN_LEADER_STOP_LOSS_PCT", "-0.60"))
V288_TIME_EXIT_MIN = float(os.getenv("CLEAN_LEADER_TIME_EXIT_MIN", "45"))

# 진입 설계값: 고정 개수 목표가 아니라 후보가 갖춘 재료의 통과 기준이다.
LEADER_MAJOR_5M_DROP = float(os.getenv("CLEAN_LEADER_MAJOR_5M_DROP", "-0.50"))
LEADER_MAJOR_15M_DROP = float(os.getenv("CLEAN_LEADER_MAJOR_15M_DROP", "-0.80"))
LEADER_ALT_UP15_RATIO_MIN = float(os.getenv("CLEAN_LEADER_ALT_UP15_RATIO_MIN", "45"))
LEADER_TOP30_UP15_MIN = int(os.getenv("CLEAN_LEADER_TOP30_UP15_MIN", "12"))
LEADER_CHANGE_15_MIN = float(os.getenv("CLEAN_LEADER_CHANGE_15_MIN", "0.80"))
LEADER_CHANGE_30_MIN = float(os.getenv("CLEAN_LEADER_CHANGE_30_MIN", "1.20"))
LEADER_REL_15_MIN = float(os.getenv("CLEAN_LEADER_REL_15_MIN", "0.60"))
LEADER_CHANGE_5_MIN = float(os.getenv("CLEAN_LEADER_CHANGE_5_MIN", "0.20"))
LEADER_CHANGE_3_MIN = float(os.getenv("CLEAN_LEADER_CHANGE_3_MIN", "-0.20"))
LEADER_CHANGE_3_BLOCK = float(os.getenv("CLEAN_LEADER_CHANGE_3_BLOCK", "-0.50"))
LEADER_CH1_CHASE = float(os.getenv("CLEAN_LEADER_CH1_CHASE", "1.50"))
LEADER_CH5_CHASE = float(os.getenv("CLEAN_LEADER_CH5_CHASE", "4.00"))
LEADER_HIGH_ROOM_CHASE = float(os.getenv("CLEAN_LEADER_HIGH_ROOM_CHASE", "0.50"))
LEADER_VWAP_PASS = float(os.getenv("CLEAN_LEADER_VWAP_PASS", "-0.10"))
LEADER_VWAP_BLOCK = float(os.getenv("CLEAN_LEADER_VWAP_BLOCK", "-0.40"))
LEADER_EMA5_PASS = float(os.getenv("CLEAN_LEADER_EMA5_PASS", "-0.10"))
LEADER_MONEY3_MIN = float(os.getenv("CLEAN_LEADER_MONEY3_MIN", "20000000"))
LEADER_MONEY5_MIN = float(os.getenv("CLEAN_LEADER_MONEY5_MIN", "35000000"))
LEADER_BIG_MONEY3 = float(os.getenv("CLEAN_LEADER_BIG_MONEY3", "50000000"))
LEADER_SPREAD_PASS = float(os.getenv("CLEAN_LEADER_SPREAD_PASS", "0.35"))
LEADER_SPREAD_BLOCK = float(os.getenv("CLEAN_LEADER_SPREAD_BLOCK", "0.45"))
LEADER_BUY_RATIO_PASS = float(os.getenv("CLEAN_LEADER_BUY_RATIO_PASS", "0.55"))
LEADER_BUY_RATIO_BLOCK = float(os.getenv("CLEAN_LEADER_BUY_RATIO_BLOCK", "0.48"))
LEADER_WS_GAP_BLOCK = float(os.getenv("CLEAN_LEADER_WS_GAP_BLOCK", "0.40"))
LEADER_OPEN_SCORE_MIN = float(os.getenv("CLEAN_LEADER_OPEN_SCORE_MIN", "10.5"))
LEADER_RECHECK_SCORE_MIN = float(os.getenv("CLEAN_LEADER_RECHECK_SCORE_MIN", "8.8"))

_v300_base_market_context = _v274_market_context

def _v274_market_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v300: 장세 강함 필터를 기존 market_context에 추가한다.

    고정 개수 목표가 아니라 현재 스캔에서 실제 정밀값이 붙은 row들의 상태로 장세를 판단한다.
    """
    ctx = _v300_base_market_context(rows)
    try:
        valid = [r for r in rows or [] if isinstance(r, dict) and _v274_ticker(r)]
        precision = [r for r in valid if bool(r.get("precision_ok")) or str(r.get("precision_source") or "") == "candlestick_1m"]
        base = precision or valid
        total = len(base)
        up15 = sum(1 for r in base if fnum(r.get("change_15"), 0) > 0)
        up30 = sum(1 for r in base if fnum(r.get("change_30"), 0) > 0)
        avg15 = (sum(fnum(r.get("change_15"), 0) for r in base) / total) if total else 0.0
        avg30 = (sum(fnum(r.get("change_30"), 0) for r in base) / total) if total else 0.0
        ranked = sorted(base, key=lambda r: fnum(r.get("turnover_24h") or r.get("acc_trade_value_24h") or r.get("turnover_5m"), 0), reverse=True)
        top30 = ranked[:30]
        top30_up15 = sum(1 for r in top30 if fnum(r.get("change_15"), 0) > 0)
        majors = {t: {} for t in ("BTC", "ETH", "XRP")}
        for r in valid:
            t = _v274_ticker(r)
            if t in majors:
                majors[t] = {"change_5": fnum(r.get("change_5"), 0), "change_15": fnum(r.get("change_15"), 0), "change_30": fnum(r.get("change_30"), 0)}
        major_drop = any(fnum(v.get("change_5"), 0) <= LEADER_MAJOR_5M_DROP or fnum(v.get("change_15"), 0) <= LEADER_MAJOR_15M_DROP for v in majors.values() if v)
        alt_up15_ratio = (up15 / total * 100.0) if total else 0.0
        alt_up30_ratio = (up30 / total * 100.0) if total else 0.0
        strong = bool((not major_drop) and alt_up15_ratio >= LEADER_ALT_UP15_RATIO_MIN and top30_up15 >= LEADER_TOP30_UP15_MIN)
        ctx.update({
            "leader_total_rows": total,
            "leader_precision_rows": len(precision),
            "leader_alt_up15_ratio": round(alt_up15_ratio, 2),
            "leader_alt_up30_ratio": round(alt_up30_ratio, 2),
            "leader_avg_change_15": round(avg15, 4),
            "leader_avg_change_30": round(avg30, 4),
            "leader_top30_up15": top30_up15,
            "leader_major_drop": major_drop,
            "leader_market_strong": strong,
            "leader_market_label": "강함" if strong else "약함/대기",
        })
    except Exception as exc:
        try:
            log_error("v300_market_context", exc)
        except Exception:
            pass
        ctx.update({"leader_market_strong": False, "leader_market_label": "확인불가"})
    return ctx


def _v300_has_value(row: Dict[str, Any], *keys: str) -> bool:
    return _v290_has_value(row, *keys) if "_v290_has_value" in globals() else any((row or {}).get(k) not in (None, "", [], {}) for k in keys)


def _v300_wait_is_recheckable(wait: List[str]) -> bool:
    allowed = ("micro긴급수집대기", "WS재확인", "스프레드애매", "매수체결애매", "시장강함재확인")
    return bool(wait) and all(any(str(w).startswith(x) for x in allowed) for w in wait or [])


def _v300_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    r = row or {}
    hard: List[str] = []
    wait: List[str] = []
    good: List[str] = []
    score = 0.0

    t = _v274_ticker(r)
    price = fnum(r.get("current_price"), 0)
    ch1 = fnum(r.get("change_1"), 0)
    ch3 = fnum(r.get("change_3"), 0)
    ch5 = fnum(r.get("change_5"), 0)
    ch15 = fnum(r.get("change_15"), 0)
    ch30 = fnum(r.get("change_30"), 0)
    money3 = fnum(r.get("turnover_3m") or r.get("money_flow_3m"), 0)
    money5 = fnum(r.get("turnover_5m") or r.get("money_flow_5m"), 0)
    high_room = fnum(r.get("below_30m_high_pct") if r.get("below_30m_high_pct") is not None else r.get("high_gap_pct"), 999)
    vwap_gap = fnum(r.get("vwap_gap_pct"), 0)
    ema5_gap = fnum(r.get("ema5_gap_pct") if r.get("ema5_gap_pct") is not None else r.get("ma5_gap_pct"), 0)
    spread = fnum(r.get("micro_spread_pct") if r.get("micro_spread_pct") is not None else r.get("orderbook_spread_pct"), 999)
    buy_ratio = fnum(r.get("micro_trade_buy_ratio_30"), 0)
    wall_ratio = fnum(r.get("micro_bid_ask_wall_ratio"), 0)
    ask_pressure = bool(r.get("micro_ask_wall_pressure") or r.get("micro_sell_trade_pressure") or r.get("ask_wall_pressure") or r.get("sell_trade_pressure"))
    micro_fresh = _v274_bool_fresh(r, "micro", MICRO_STALE_SEC if "MICRO_STALE_SEC" in globals() else 25.0)
    ws_fresh = _v274_bool_fresh(r, "ws", WS_HUB_STALE_SEC if "WS_HUB_STALE_SEC" in globals() else 12.0) or bool(r.get("ws_fresh"))
    ws_gap = abs(fnum(r.get("current_price_ws_gap_pct"), 0))

    has_ch3 = _v300_has_value(r, "change_3")
    has_ch5 = _v300_has_value(r, "change_5")
    has_ch15 = _v300_has_value(r, "change_15")
    has_ch30 = _v300_has_value(r, "change_30")
    has_money3 = _v300_has_value(r, "turnover_3m", "money_flow_3m")
    has_money5 = _v300_has_value(r, "turnover_5m", "money_flow_5m")
    has_vwap = _v300_has_value(r, "vwap_gap_pct")
    has_ema5 = _v300_has_value(r, "ema5_gap_pct", "ma5_gap_pct")
    has_high_room = _v300_has_value(r, "below_30m_high_pct", "high_gap_pct") and high_room < 900

    market_strong = bool((market or {}).get("leader_market_strong"))
    avg15 = fnum((market or {}).get("leader_avg_change_15"), fnum((market or {}).get("avg_change_5"), 0))
    rel15 = ch15 - avg15

    if not t or price <= 0:
        hard.append("가격없음")
    if t in MAJOR_WATCH_TICKERS:
        hard.append("대형주는 시장참고용")
    if not market_strong:
        hard.append(f"장세강함아님 {str((market or {}).get('leader_market_label') or '-')}")
    else:
        score += 2.0; good.append("장세강함")

    if not has_ch15:
        wait.append("15분흐름값부족")
    elif ch15 >= LEADER_CHANGE_15_MIN:
        score += 1.5; good.append("15분주도")
    else:
        hard.append(f"15분주도부족 {ch15:+.2f}%")

    if not has_ch30:
        wait.append("30분흐름값부족")
    elif ch30 >= LEADER_CHANGE_30_MIN:
        score += 1.2; good.append("30분유지")
    else:
        wait.append(f"30분유지약함 {ch30:+.2f}%")

    if has_ch15:
        if rel15 >= LEADER_REL_15_MIN:
            score += 1.2; good.append("시장대비강함")
        else:
            hard.append(f"상대강도부족 {rel15:+.2f}%")

    if not has_ch5:
        wait.append("5분흐름값부족")
    elif ch5 >= LEADER_CHANGE_5_MIN:
        score += 0.8; good.append("5분흐름유지")
    else:
        hard.append(f"5분흐름부족 {ch5:+.2f}%")

    if not has_ch3:
        wait.append("3분흐름값부족")
    elif ch3 >= LEADER_CHANGE_3_MIN:
        score += 0.6; good.append("3분되밀림방어")
    elif ch3 <= LEADER_CHANGE_3_BLOCK:
        hard.append(f"3분급락전환 {ch3:+.2f}%")
    else:
        wait.append(f"3분흐름재확인 {ch3:+.2f}%")

    if has_ch1 and ch1 >= LEADER_CH1_CHASE and (not has_ch5 or ch5 < LEADER_CHANGE_5_MIN or not has_ch15 or ch15 < LEADER_CHANGE_15_MIN):
        hard.append(f"1분급등추격 {ch1:+.2f}%")
    if has_ch5 and ch5 >= LEADER_CH5_CHASE:
        hard.append(f"5분과급등추격 {ch5:+.2f}%")
    if has_high_room and high_room < LEADER_HIGH_ROOM_CHASE and ch1 >= 0.80:
        hard.append(f"고점바로밑추격 {high_room:.2f}%")

    if not has_vwap:
        wait.append("VWAP값부족")
    elif vwap_gap >= LEADER_VWAP_PASS:
        score += 1.0; good.append("VWAP유지")
    elif vwap_gap <= LEADER_VWAP_BLOCK:
        hard.append(f"VWAP이탈 {vwap_gap:+.2f}%")
    else:
        wait.append(f"VWAP재확인 {vwap_gap:+.2f}%")

    if not has_ema5:
        wait.append("EMA5값부족")
    elif ema5_gap >= LEADER_EMA5_PASS:
        score += 0.8; good.append("EMA5유지")
    else:
        wait.append(f"EMA5재확인 {ema5_gap:+.2f}%")

    if not has_money3:
        wait.append("3분거래대금값부족")
    elif money3 >= LEADER_MONEY3_MIN:
        score += 1.0; good.append("3분돈흐름")
    else:
        hard.append("3분거래대금부족")
    if not has_money5:
        wait.append("5분거래대금값부족")
    elif money5 >= LEADER_MONEY5_MIN:
        score += 1.0; good.append("5분돈흐름")
    else:
        wait.append("5분거래대금부족")
    if has_money3 and has_ch3 and money3 >= LEADER_BIG_MONEY3 and ch3 <= 0:
        hard.append("돈은큰데가격반응없음")

    if not micro_fresh:
        wait.append("micro긴급수집대기")
    else:
        score += 1.0; good.append("micro신선")
        if spread <= LEADER_SPREAD_PASS:
            score += 0.8; good.append("스프레드통과")
        elif spread >= LEADER_SPREAD_BLOCK:
            hard.append(f"스프레드넓음 {spread:.2f}%")
        else:
            wait.append(f"스프레드애매 {spread:.2f}%")
        if buy_ratio >= LEADER_BUY_RATIO_PASS:
            score += 1.0; good.append("매수체결우세")
        elif buy_ratio <= LEADER_BUY_RATIO_BLOCK:
            hard.append(f"매수체결약함 {buy_ratio:.2f}")
        else:
            wait.append(f"매수체결애매 {buy_ratio:.2f}")
        if ask_pressure or (0 < wall_ratio < 0.67):
            hard.append("매도벽/매도체결압력")
        else:
            score += 0.5; good.append("호가압력통과")

    if ws_fresh:
        score += 0.3; good.append("WS신선")
    else:
        wait.append("WS재확인")
    if ws_gap >= LEADER_WS_GAP_BLOCK:
        hard.append(f"REST-WS차이과다 {ws_gap:.2f}%")

    if hard:
        action = "block"
    elif wait:
        action = "recheck_wait" if score >= LEADER_RECHECK_SCORE_MIN and _v300_wait_is_recheckable(wait) else "observe"
    else:
        action = "paper_open" if score >= LEADER_OPEN_SCORE_MIN else "observe"

    return {
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "score": round(score, 3),
        "action": action,
        "good": good[:14],
        "wait": wait[:12],
        "hard": hard[:12],
        "leader_score": round(score, 3),
        "market_strong": market_strong,
        "leader_market_label": str((market or {}).get("leader_market_label") or "-"),
        "leader_rel15_pct": round(rel15, 3),
        "change_15": round(ch15, 3),
        "change_30": round(ch30, 3),
        "change_5": round(ch5, 3),
        "change_3": round(ch3, 3),
        "turnover_3m": money3,
        "turnover_5m": money5,
        "high_room_pct": round(high_room, 3) if high_room < 900 else 999,
        "vwap_gap_pct": round(vwap_gap, 3),
        "ema5_gap_pct": round(ema5_gap, 3),
        "spread_pct": round(spread, 4) if spread < 900 else 999,
        "buy_ratio": round(buy_ratio, 4),
        "micro_fresh": micro_fresh,
        "ws_fresh": ws_fresh,
        "material_missing": [x for x, ok in {
            "15분흐름": has_ch15,
            "30분흐름": has_ch30,
            "5분흐름": has_ch5,
            "3분흐름": has_ch3,
            "3분돈": has_money3,
            "5분돈": has_money5,
            "VWAP": has_vwap,
            "EMA5": has_ema5,
        }.items() if not ok],
    }

_v300_base_candidate_row = _v288_candidate_row

def _v288_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v300_eval(row, market)


def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    item = _v300_base_candidate_row(row, market)
    ev = item.get("entry_context", {}).get("v288_eval") if isinstance(item.get("entry_context"), dict) else None
    if not isinstance(ev, dict):
        ev = _v300_eval(item, market)
    action = str(ev.get("action") or item.get("final_entry_action") or "observe")
    ready = action == "paper_open"
    recheck = action == "recheck_wait"
    item.update({
        "schema": "leader_momentum_continuation_v300",
        "strategy": STRATEGY_LABEL,
        "strategy_name": STRATEGY_LABEL,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "paper_route": STRATEGY_KEY,
        "score": ev.get("score", item.get("score", 0.0)),
        "leader_score": ev.get("leader_score", ev.get("score", 0.0)),
        "leader_good": ev.get("good", []),
        "leader_wait": ev.get("wait", []),
        "leader_blocks": ev.get("hard", []),
        "quality_risk_tags": ev.get("hard", []) + ev.get("wait", []),
        "block_reasons": ev.get("hard", []),
        "paper_bot_open": ready,
        "open_eligible": ready,
        "paper_eligible": ready,
        "eligible_for_paper": ready,
        "trade_ready": ready,
        "review_only": not ready,
        "observe_only": not ready,
        "final_entry_action": "paper_open" if ready else ("recheck_wait" if recheck else "reject" if action == "block" else "observe"),
        "final_entry_label": "✅ 진입 가능" if ready else ("⚠️ 정보 재확인 중" if recheck else "❌ 진입 금지" if action == "block" else "❔ 관찰"),
        "final_entry_reasons": ev.get("good") if ready else (ev.get("wait") if recheck else ev.get("hard") or ev.get("wait") or []),
        "one_liner": f"{'✅ 진입 가능' if ready else ('⚠️ 정보 재확인 중' if recheck else '❌ 진입 금지' if action == 'block' else '❔ 관찰')} / 점수 {fnum(ev.get('score'),0):.1f} / 15분 {fnum(ev.get('change_15'),0):+.2f}% / 30분 {fnum(ev.get('change_30'),0):+.2f}%",
    })
    ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx.update({
        "brain_version": BOT_VERSION,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_rule_version": "v300_leader_momentum_design_fixed_30_closed",
        "take_profit_pct": V288_TAKE_PROFIT_PCT,
        "protect_trigger_pct": V288_PROTECT_TRIGGER_PCT,
        "protect_floor_pct": V288_PROTECT_FLOOR_PCT,
        "stop_loss_pct": V288_STOP_LOSS_PCT,
        "time_exit_min": V288_TIME_EXIT_MIN,
        "leader_eval": ev,
        "v288_eval": ev,
    })
    item["entry_context"] = ctx
    return item


def _v288_rank_key(row: Dict[str, Any]) -> Tuple[float, float, float, float]:  # type: ignore[override]
    return (
        fnum((row or {}).get("leader_score") or (row or {}).get("score"), 0),
        fnum((row or {}).get("change_15"), 0),
        fnum((row or {}).get("turnover_5m"), 0),
        fnum((row or {}).get("turnover_3m"), 0),
    )


def _v288_row_summary(r: Dict[str, Any]) -> str:  # type: ignore[override]
    t = _v274_ticker(r) or "?"
    label = str(r.get("final_entry_label") or ("✅ 진입 가능" if r.get("trade_ready") else "❔ 관찰"))
    reasons = _v288_as_list(r.get("final_entry_reasons") or r.get("quality_risk_tags"))
    return (
        f"- {t}: {label} / 점수 {fnum(r.get('leader_score') or r.get('score'),0):.1f}"
        f" / 15분 {fnum(r.get('change_15'),0):+.2f}% / 30분 {fnum(r.get('change_30'),0):+.2f}%"
        f" / 상대 {fnum(r.get('leader_rel15_pct'),0):+.2f}% / 3분돈 {fnum(r.get('turnover_3m'),0)/10000:.0f}만"
        f" / 사유 {', '.join(reasons[:4]) if reasons else '-'}"
    )


def _v288_score_text(rows: List[Dict[str, Any]]) -> str:  # type: ignore[override]
    rows = rows or []
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    st = _v288_closed_stats()
    lines = [
        "📊 단일 전략 성과 /score",
        f"전략: {STRATEGY_LABEL} ({STRATEGY_KEY})",
        "상태: ❔ 30건 검증 전 / 신규 전략 시작 / 조건변경 금지",
        "",
        "[1] 설계 고정값",
        f"- 익절 +{V288_TAKE_PROFIT_PCT:.2f}% / 보호 +{V288_PROTECT_TRIGGER_PCT:.2f}% 이후 +{V288_PROTECT_FLOOR_PCT:.2f}% / 손절 {V288_STOP_LOSS_PCT:.2f}% / 시간 {V288_TIME_EXIT_MIN:.0f}분",
        "- 장세필터: 강함 / 주도코인 15분·30분 유지력 / micro·WS 신선정보 확인",
        "",
        "[2] 현재 후보",
        f"- ✅ 진입 가능 {len(ready)}개 / ⚠️ 재확인 {len(recheck)}개 / ❌ 관찰·금지 {max(0, len(rows)-len(ready)-len(recheck))}개",
        *( _v288_row_summary(r) for r in ready[:5] ),
        *( ["- 진입 가능 후보 없음"] if not ready else [] ),
        "",
        "[3] 재확인 대기 TOP",
        *( _v288_row_summary(r) for r in recheck[:5] ),
        *( ["- 재확인 대기 없음"] if not recheck else [] ),
        "",
        "[4] CLOSED 성과",
        f"- {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['wr']:.1f}% / 순합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 최대 {st['max']:+.2f}% / 최소 {st['min']:+.2f}%",
        "",
        "[5] 30건 후 판정",
        "- 통과: 순수익 플러스, 승률 35~40% 이상, 평균 손익비 양호",
        "- 보정: 본전 근처+공통 손실원인 1~2개일 때만 설계서 단위 묶음수정",
        "- 폐기: 명확한 마이너스, 승률 30% 미만, +1.0% 이상 후보 거의 없을 때",
    ]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v274_read_material_rows(160)
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    blocked = [r for r in rows if str(r.get("final_entry_action")) not in {"paper_open", "recheck_wait"}]
    c = Counter()
    for r in blocked:
        for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
            c[str(x)] += 1
    lines = [
        "🔍 후보품질 /quality",
        f"전략: {STRATEGY_LABEL}",
        f"- 진입 가능 {len(ready)} / 재확인 {len(recheck)} / 관찰·금지 {len(blocked)}",
        "",
        "[진입 가능 TOP]",
        *(_v288_row_summary(r) for r in ready[:8]),
        *( ["- 없음"] if not ready else [] ),
        "",
        "[진입 금지/보류 사유 TOP]",
        *(f"- {k}: {v}" for k, v in c.most_common(8)),
        *( ["- 없음"] if not c else [] ),
    ]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v274_read_material_rows(160)
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    return "\n".join([
        "👀 전략 감시 /strategy_watch",
        f"전략: {STRATEGY_LABEL}",
        "- 저점/눌림 계열 폐기 후, 장세 강함 + 주도코인 유지력만 검증",
        "- 현재는 leader_momentum_continuation 단일 설계값으로 paper 후보를 생성함",
        "",
        "[진입 가능]",
        *(_v288_row_summary(r) for r in ready[:10]),
        *( ["- 없음"] if not ready else [] ),
        "",
        "[정보 재확인]",
        *(_v288_row_summary(r) for r in recheck[:10]),
        *( ["- 없음"] if not recheck else [] ),
    ])

_v300_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v300_base_health_text()
    text = text.replace("저점 쓸림 후 VWAP 회복 단타", STRATEGY_LABEL)
    text = text.replace("sweep_vwap_recovery", STRATEGY_KEY)
    text = text.replace("공장: sweep_vwap_recovery 단일 후보 저장", "공장: leader_momentum_continuation 단일 후보 저장")
    text = text.replace("CLOSED 30건 전 조건 변경 금지", "CLOSED 30건 전 조건 변경 금지")
    return text

_v300_base_write_strategy_files = _v288_write_strategy_files

def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Counter) -> None:  # type: ignore[override]
    _v300_base_write_strategy_files(strict_rows, shadow_rows, market, rejects)
    try:
        p = load_json(FILES["strategy_material_snapshot"], {})
        if isinstance(p, dict):
            p["schema"] = "leader_momentum_continuation_v300_latest"
            p["strategy_key"] = STRATEGY_KEY
            p["strategy_label"] = STRATEGY_LABEL
            p["note"] = "v300: 장세 선택형 주도코인 추세 지속 전략 latest snapshot. 조건값 30건 전 변경 금지."
            save_json(FILES["strategy_material_snapshot"], p)
    except Exception as exc:
        log_error("v300_strategy_snapshot_meta", exc)

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V300_NOTE
    STATE["v300_single_strategy"] = STRATEGY_KEY
    STATE["strategy_retired"] = False
except Exception as exc:
    log_error("v300_init", exc)


# =============================================================================
# v2.13.301: S/A/B/C grade split + one-hour paper observation
# - 폐기된 sweep_vwap_recovery는 active command/cache/path에서 제거한다.
# - leader_momentum_continuation만 active로 유지한다.
# - S급은 미래 자동매매 후보급, A급은 정식 모의검증, B급은 관찰/복기, C급은 금지로 분리한다.
# - 5~10분 초단타가 아니라 45~60분 관찰형 단타를 기준으로 본다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.301"
STRATEGY_KEY = "leader_momentum_continuation"
STRATEGY_NAME = "장세 선택형 주도코인 추세 지속 전략"
STRATEGY_LABEL = STRATEGY_NAME
V301_NOTE = "v301: S/A/B/C 등급 분리 + 구전략 active 제거 + leader 단일 캐시. 자동매수 OFF / paper 검증 전용."

# v301 청산 설계: 승률 우선 구조. 강한 S급은 +2.0 연장 관찰값을 남기되, paper 기본 익절은 +1.2로 검증한다.
V288_TAKE_PROFIT_PCT = float(os.getenv("CLEAN_LEADER_BASE_TAKE_PROFIT_PCT", "1.20"))
V288_PROTECT_TRIGGER_PCT = float(os.getenv("CLEAN_LEADER_PROTECT_TRIGGER_PCT", "0.70"))
V288_PROTECT_FLOOR_PCT = float(os.getenv("CLEAN_LEADER_PROTECT_FLOOR_PCT", "0.35"))
V288_STOP_LOSS_PCT = float(os.getenv("CLEAN_LEADER_STOP_LOSS_PCT", "-0.60"))
V288_TIME_EXIT_MIN = float(os.getenv("CLEAN_LEADER_TIME_EXIT_MIN", "60"))
LEADER_EXTEND_TARGET_PCT = float(os.getenv("CLEAN_LEADER_EXTEND_TARGET_PCT", "2.00"))

# 등급 기준. 이것도 후보 제한 개수가 아니라 paper/자동매매 가능성 분류 기준이다.
LEADER_S_SCORE_MIN = float(os.getenv("CLEAN_LEADER_S_SCORE_MIN", "12.8"))
LEADER_A_SCORE_MIN = float(os.getenv("CLEAN_LEADER_A_SCORE_MIN", "10.8"))
LEADER_B_SCORE_MIN = float(os.getenv("CLEAN_LEADER_B_SCORE_MIN", "8.8"))
LEADER_S_BUY_RATIO = float(os.getenv("CLEAN_LEADER_S_BUY_RATIO", "0.60"))
LEADER_S_SPREAD_MAX = float(os.getenv("CLEAN_LEADER_S_SPREAD_MAX", "0.25"))
LEADER_S_CHANGE15 = float(os.getenv("CLEAN_LEADER_S_CHANGE15", "1.20"))
LEADER_S_CHANGE30 = float(os.getenv("CLEAN_LEADER_S_CHANGE30", "1.80"))
LEADER_S_MONEY3 = float(os.getenv("CLEAN_LEADER_S_MONEY3", "30000000"))
LEADER_A_OPEN_PAPER = True
LEADER_B_OPEN_PAPER = False  # B는 현재 관찰/복기. 모의 OPEN은 S/A로 제한해 실제 자동매매 기준과 분리한다.


def _v301_strategy_key_from_closed(row: Dict[str, Any]) -> str:
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    for k in ("paper_strategy_key", "strategy_key", "route", "paper_route", "strategy"):
        v = row.get(k)
        if v:
            return str(v)
    for k in ("paper_strategy_key", "strategy_key", "route", "paper_route", "strategy"):
        v = ctx.get(k)
        if v:
            return str(v)
    return ""


def _v288_closed_rows(limit: int = 12000) -> List[Dict[str, Any]]:  # type: ignore[override]
    try:
        rows = load_closed(limit)
    except Exception:
        rows = []
    return [r for r in rows or [] if isinstance(r, dict) and _v301_strategy_key_from_closed(r) == STRATEGY_KEY]


def _v288_closed_stats() -> Dict[str, Any]:  # type: ignore[override]
    if '_v288_perf_stats' in globals():
        return _v288_perf_stats(_v288_closed_rows())
    rows = _v288_closed_rows()
    vals = [fnum(r.get('pnl_pct'), 0) for r in rows]
    wins = sum(1 for v in vals if v > 0)
    losses = sum(1 for v in vals if v <= 0)
    n = len(vals)
    return {"n": n, "wins": wins, "losses": losses, "wr": (wins / n * 100.0 if n else 0.0), "total": sum(vals), "avg": (sum(vals)/n if n else 0.0), "max": (max(vals) if vals else 0.0), "min": (min(vals) if vals else 0.0)}


def _v293_has_strategy_material(row: Dict[str, Any]) -> bool:  # type: ignore[override]
    """leader 전략용 정밀 재료 여부.

    전체시장은 계속 넓게 보되, 15/30분 흐름·VWAP·돈흐름 같은 재료가 하나도 없는
    bulk-only row는 전략판정에 태우지 않는다. 개수 통제가 아니라 정보 schema 분리다.
    """
    try:
        if not isinstance(row, dict):
            return False
        if bool(row.get("precision_ok")) or str(row.get("precision_source") or "") == "candlestick_1m":
            return True
        keys = (
            "change_15", "change_rate_15m", "rate_15m", "change_30", "change_rate_30m", "rate_30m",
            "change_10", "change_rate_10m", "turnover_3m", "money_flow_3m", "turnover_5m", "money_flow_5m",
            "turnover_10m", "money_flow_10m", "vwap_gap_pct", "ema5_gap_pct", "ma5_gap_pct",
        )
        return any(_v290_has_value(row, k) for k in keys)
    except Exception:
        return False


def _v301_wait_is_recheckable(wait: List[str]) -> bool:
    allowed = ("micro긴급수집대기", "WS재확인", "스프레드애매", "매수체결애매", "시장강함재확인")
    return bool(wait) and all(any(str(w).startswith(x) for x in allowed) for w in wait or [])


def _v301_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    """leader_momentum 단일 evaluator.

    v300의 has_ch1 누락/구 sweep 캐시 연결을 끊고 여기서 전체 판정을 끝낸다.
    """
    r = row or {}
    hard: List[str] = []
    wait: List[str] = []
    good: List[str] = []
    score = 0.0

    t = _v274_ticker(r)
    price = fnum(r.get("current_price"), 0)
    ch1 = fnum(r.get("change_1") or r.get("rate_1m") or r.get("change_rate_1m"), 0)
    ch3 = fnum(r.get("change_3") or r.get("rate_3m") or r.get("change_rate_3m"), 0)
    ch5 = fnum(r.get("change_5") or r.get("rate_5m") or r.get("change_rate_5m"), 0)
    ch15 = fnum(r.get("change_15") or r.get("rate_15m") or r.get("change_rate_15m") or r.get("change_10") or r.get("change_rate_10m"), 0)
    ch30 = fnum(r.get("change_30") or r.get("rate_30m") or r.get("change_rate_30m"), 0)
    money3 = fnum(r.get("turnover_3m") or r.get("money_flow_3m"), 0)
    money5 = fnum(r.get("turnover_5m") or r.get("money_flow_5m") or r.get("turnover_10m") or r.get("money_flow_10m"), 0)
    high_room = fnum(r.get("below_30m_high_pct") if r.get("below_30m_high_pct") is not None else r.get("high_gap_pct"), 999)
    vwap_gap = fnum(r.get("vwap_gap_pct"), 0)
    ema5_gap = fnum(r.get("ema5_gap_pct") if r.get("ema5_gap_pct") is not None else r.get("ma5_gap_pct"), 0)
    spread = fnum(r.get("micro_spread_pct") if r.get("micro_spread_pct") is not None else r.get("orderbook_spread_pct"), 999)
    buy_ratio = fnum(r.get("micro_trade_buy_ratio_30") if r.get("micro_trade_buy_ratio_30") is not None else r.get("trade_buy_ratio_30"), 0)
    wall_ratio = fnum(r.get("micro_bid_ask_wall_ratio") if r.get("micro_bid_ask_wall_ratio") is not None else r.get("bid_ask_wall_ratio"), 0)
    ask_pressure = bool(r.get("micro_ask_wall_pressure") or r.get("micro_sell_trade_pressure") or r.get("ask_wall_pressure") or r.get("sell_trade_pressure"))
    micro_fresh = _v274_bool_fresh(r, "micro", MICRO_STALE_SEC if "MICRO_STALE_SEC" in globals() else 25.0)
    ws_fresh = _v274_bool_fresh(r, "ws", WS_HUB_STALE_SEC if "WS_HUB_STALE_SEC" in globals() else 12.0) or bool(r.get("ws_fresh"))
    ws_gap = abs(fnum(r.get("current_price_ws_gap_pct"), 0))

    has_ch1 = _v300_has_value(r, "change_1", "rate_1m", "change_rate_1m")
    has_ch3 = _v300_has_value(r, "change_3", "rate_3m", "change_rate_3m")
    has_ch5 = _v300_has_value(r, "change_5", "rate_5m", "change_rate_5m")
    has_ch15 = _v300_has_value(r, "change_15", "rate_15m", "change_rate_15m", "change_10", "change_rate_10m")
    has_ch30 = _v300_has_value(r, "change_30", "rate_30m", "change_rate_30m")
    has_money3 = _v300_has_value(r, "turnover_3m", "money_flow_3m")
    has_money5 = _v300_has_value(r, "turnover_5m", "money_flow_5m", "turnover_10m", "money_flow_10m")
    has_vwap = _v300_has_value(r, "vwap_gap_pct")
    has_ema5 = _v300_has_value(r, "ema5_gap_pct", "ma5_gap_pct")
    has_high_room = _v300_has_value(r, "below_30m_high_pct", "high_gap_pct") and high_room < 900

    market_strong = bool((market or {}).get("leader_market_strong"))
    avg15 = fnum((market or {}).get("leader_avg_change_15"), fnum((market or {}).get("avg_change_5"), 0))
    rel15 = ch15 - avg15

    if not t or price <= 0:
        hard.append("가격없음")
    if t in MAJOR_WATCH_TICKERS:
        hard.append("대형주는 시장참고용")
    if not market_strong:
        hard.append(f"장세강함아님 {str((market or {}).get('leader_market_label') or '-')}")
    else:
        score += 2.0; good.append("장세강함")

    if not has_ch15:
        wait.append("15분흐름값부족")
    elif ch15 >= LEADER_CHANGE_15_MIN:
        score += 1.5; good.append("15분주도")
    else:
        hard.append(f"15분주도부족 {ch15:+.2f}%")

    if not has_ch30:
        wait.append("30분흐름값부족")
    elif ch30 >= LEADER_CHANGE_30_MIN:
        score += 1.2; good.append("30분유지")
    else:
        wait.append(f"30분유지약함 {ch30:+.2f}%")

    if has_ch15:
        if rel15 >= LEADER_REL_15_MIN:
            score += 1.2; good.append("시장대비강함")
        else:
            hard.append(f"상대강도부족 {rel15:+.2f}%")

    if not has_ch5:
        wait.append("5분흐름값부족")
    elif ch5 >= LEADER_CHANGE_5_MIN:
        score += 0.8; good.append("5분흐름유지")
    else:
        hard.append(f"5분흐름부족 {ch5:+.2f}%")

    if not has_ch3:
        wait.append("3분흐름값부족")
    elif ch3 >= LEADER_CHANGE_3_MIN:
        score += 0.6; good.append("3분되밀림방어")
    elif ch3 <= LEADER_CHANGE_3_BLOCK:
        hard.append(f"3분급락전환 {ch3:+.2f}%")
    else:
        wait.append(f"3분흐름재확인 {ch3:+.2f}%")

    if has_ch1 and ch1 >= LEADER_CH1_CHASE and (not has_ch5 or ch5 < LEADER_CHANGE_5_MIN or not has_ch15 or ch15 < LEADER_CHANGE_15_MIN):
        hard.append(f"1분급등추격 {ch1:+.2f}%")
    if has_ch5 and ch5 >= LEADER_CH5_CHASE:
        hard.append(f"5분과급등추격 {ch5:+.2f}%")
    if has_high_room and high_room < LEADER_HIGH_ROOM_CHASE and ch1 >= 0.80:
        hard.append(f"고점바로밑추격 {high_room:.2f}%")

    if not has_vwap:
        wait.append("VWAP값부족")
    elif vwap_gap >= LEADER_VWAP_PASS:
        score += 1.0; good.append("VWAP유지")
    elif vwap_gap <= LEADER_VWAP_BLOCK:
        hard.append(f"VWAP이탈 {vwap_gap:+.2f}%")
    else:
        wait.append(f"VWAP재확인 {vwap_gap:+.2f}%")

    if not has_ema5:
        wait.append("EMA5값부족")
    elif ema5_gap >= LEADER_EMA5_PASS:
        score += 0.8; good.append("EMA5유지")
    else:
        wait.append(f"EMA5재확인 {ema5_gap:+.2f}%")

    if not has_money3:
        wait.append("3분거래대금값부족")
    elif money3 >= LEADER_MONEY3_MIN:
        score += 1.0; good.append("3분돈흐름")
    else:
        hard.append("3분거래대금부족")
    if not has_money5:
        wait.append("5분거래대금값부족")
    elif money5 >= LEADER_MONEY5_MIN:
        score += 1.0; good.append("5분돈흐름")
    else:
        wait.append("5분거래대금부족")
    if has_money3 and has_ch3 and money3 >= LEADER_BIG_MONEY3 and ch3 <= 0:
        hard.append("돈은큰데가격반응없음")

    if not micro_fresh:
        wait.append("micro긴급수집대기")
    else:
        score += 1.0; good.append("micro신선")
        if spread <= LEADER_SPREAD_PASS:
            score += 0.8; good.append("스프레드통과")
        elif spread >= LEADER_SPREAD_BLOCK:
            hard.append(f"스프레드넓음 {spread:.2f}%")
        else:
            wait.append(f"스프레드애매 {spread:.2f}%")
        if buy_ratio >= LEADER_BUY_RATIO_PASS:
            score += 1.0; good.append("매수체결우세")
        elif buy_ratio <= LEADER_BUY_RATIO_BLOCK:
            hard.append(f"매수체결약함 {buy_ratio:.2f}")
        else:
            wait.append(f"매수체결애매 {buy_ratio:.2f}")
        if ask_pressure or (0 < wall_ratio < 0.67):
            hard.append("매도벽/매도체결압력")
        else:
            score += 0.5; good.append("호가압력통과")

    if ws_fresh:
        score += 0.3; good.append("WS신선")
    else:
        wait.append("WS재확인")
    if ws_gap >= LEADER_WS_GAP_BLOCK:
        hard.append(f"REST-WS차이과다 {ws_gap:.2f}%")

    if hard:
        action = "block"
    elif wait:
        action = "recheck_wait" if score >= LEADER_RECHECK_SCORE_MIN and _v301_wait_is_recheckable(wait) else "observe"
    else:
        action = "paper_open" if score >= LEADER_A_SCORE_MIN else "observe"

    # S/A/B/C 등급 분리. S만 미래 자동매매 후보급, S/A만 현재 paper OPEN, B는 관찰/복기, C는 금지.
    grade = "C"
    grade_label = "❌ C급 진입 금지"
    auto_ready = False
    paper_grade_open = False
    if not hard and not wait and score >= LEADER_S_SCORE_MIN and ch15 >= LEADER_S_CHANGE15 and ch30 >= LEADER_S_CHANGE30 and money3 >= LEADER_S_MONEY3 and micro_fresh and ws_fresh and spread <= LEADER_S_SPREAD_MAX and buy_ratio >= LEADER_S_BUY_RATIO:
        grade = "S"; grade_label = "✅ S급 자동매매 후보급"; auto_ready = True; paper_grade_open = True
    elif not hard and not wait and score >= LEADER_A_SCORE_MIN:
        grade = "A"; grade_label = "🟡 A급 모의매매 후보"; paper_grade_open = True
    elif not hard and score >= LEADER_B_SCORE_MIN:
        grade = "B"; grade_label = "❔ B급 관찰/복기 후보"; paper_grade_open = bool(LEADER_B_OPEN_PAPER and action != "block")
    else:
        grade = "C"; grade_label = "❌ C급 진입 금지"

    if grade in {"S", "A"}:
        action = "paper_open"
    elif action == "paper_open":
        action = "observe"

    return {
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "score": round(score, 3),
        "action": action,
        "good": good[:14],
        "wait": wait[:12],
        "hard": hard[:12],
        "candidate_grade": grade,
        "candidate_grade_label": grade_label,
        "auto_ready": auto_ready,
        "auto_ready_label": "✅ 미래 자동매매 후보급" if auto_ready else "-",
        "paper_grade_open": paper_grade_open,
        "leader_score": round(score, 3),
        "leader_extended_target_pct": LEADER_EXTEND_TARGET_PCT,
        "leader_base_take_profit_pct": V288_TAKE_PROFIT_PCT,
        "market_strong": market_strong,
        "leader_market_label": str((market or {}).get("leader_market_label") or "-"),
        "leader_rel15_pct": round(rel15, 3),
        "change_15": round(ch15, 3),
        "change_30": round(ch30, 3),
        "change_5": round(ch5, 3),
        "change_3": round(ch3, 3),
        "turnover_3m": money3,
        "turnover_5m": money5,
        "high_room_pct": round(high_room, 3) if high_room < 900 else 999,
        "vwap_gap_pct": round(vwap_gap, 3),
        "ema5_gap_pct": round(ema5_gap, 3),
        "spread_pct": round(spread, 4) if spread < 900 else 999,
        "buy_ratio": round(buy_ratio, 4),
        "micro_fresh": micro_fresh,
        "ws_fresh": ws_fresh,
        "material_missing": [x for x, ok in {
            "1분흐름": has_ch1,
            "15분흐름": has_ch15,
            "30분흐름": has_ch30,
            "5분흐름": has_ch5,
            "3분흐름": has_ch3,
            "3분돈": has_money3,
            "5분돈": has_money5,
            "VWAP": has_vwap,
            "EMA5": has_ema5,
        }.items() if not ok],
    }


def _v300_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v301_eval(row, market)


def _v288_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v301_eval(row, market)


def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """구 sweep candidate_row를 호출하지 않는 leader 단일 후보 row 생성."""
    item = dict(row or {})
    ev = _v301_eval(item, market)
    action = str(ev.get("action") or "observe")
    ready = action == "paper_open"
    recheck = action == "recheck_wait"
    reasons = ev.get("good") if ready else (ev.get("wait") if recheck else ev.get("hard") or ev.get("wait") or [])
    item.update({
        "schema": "leader_momentum_continuation_v301_grade",
        "brain_version": BOT_VERSION,
        "version": BOT_VERSION,
        "strategy": STRATEGY_LABEL,
        "strategy_name": STRATEGY_LABEL,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "paper_route": STRATEGY_KEY,
        "score": ev.get("score", 0.0),
        "leader_score": ev.get("leader_score", ev.get("score", 0.0)),
        "leader_good": ev.get("good", []),
        "leader_wait": ev.get("wait", []),
        "leader_blocks": ev.get("hard", []),
        "candidate_grade": ev.get("candidate_grade"),
        "candidate_grade_label": ev.get("candidate_grade_label"),
        "auto_ready": bool(ev.get("auto_ready")),
        "auto_ready_label": ev.get("auto_ready_label"),
        "quality_risk_tags": ev.get("hard", []) + ev.get("wait", []),
        "block_reasons": ev.get("hard", []),
        "paper_bot_open": ready,
        "open_eligible": ready,
        "paper_eligible": ready,
        "eligible_for_paper": ready,
        "trade_ready": ready,
        "review_only": not ready,
        "observe_only": not ready,
        "final_entry_action": action if action in {"paper_open", "recheck_wait"} else ("reject" if action == "block" else "observe"),
        "final_entry_label": ev.get("candidate_grade_label") or ("✅ 진입 가능" if ready else "⚠️ 정보 재확인 중" if recheck else "❌ 진입 금지" if action == "block" else "❔ 관찰"),
        "final_entry_reasons": reasons,
        "one_liner": f"- {_v274_ticker(item) or '?'}: {ev.get('candidate_grade_label')} / 점수 {fnum(ev.get('score'),0):.1f} / 15분 {fnum(ev.get('change_15'),0):+.2f}% / 30분 {fnum(ev.get('change_30'),0):+.2f}% / 3분돈 {fnum(ev.get('turnover_3m'),0)/10000:.0f}만",
        "take_profit_pct": V288_TAKE_PROFIT_PCT,
        "extended_target_pct": LEADER_EXTEND_TARGET_PCT,
        "protect_trigger_pct": V288_PROTECT_TRIGGER_PCT,
        "protect_floor_pct": V288_PROTECT_FLOOR_PCT,
        "stop_loss_pct": V288_STOP_LOSS_PCT,
        "time_exit_min": V288_TIME_EXIT_MIN,
    })
    ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx.update({
        "brain_version": BOT_VERSION,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_rule_version": "v301_leader_momentum_grade_60m",
        "candidate_grade": ev.get("candidate_grade"),
        "candidate_grade_label": ev.get("candidate_grade_label"),
        "auto_ready": bool(ev.get("auto_ready")),
        "take_profit_pct": V288_TAKE_PROFIT_PCT,
        "extended_target_pct": LEADER_EXTEND_TARGET_PCT,
        "protect_trigger_pct": V288_PROTECT_TRIGGER_PCT,
        "protect_floor_pct": V288_PROTECT_FLOOR_PCT,
        "stop_loss_pct": V288_STOP_LOSS_PCT,
        "time_exit_min": V288_TIME_EXIT_MIN,
        "leader_eval": ev,
        "v288_eval": ev,
    })
    item["entry_context"] = ctx
    return item


def _v288_rank_key(row: Dict[str, Any]) -> Tuple[float, float, float, float]:  # type: ignore[override]
    grade_score = {"S": 4.0, "A": 3.0, "B": 2.0, "C": 1.0}.get(str((row or {}).get("candidate_grade") or "C"), 0.0)
    return (
        grade_score,
        fnum((row or {}).get("leader_score") or (row or {}).get("score"), 0),
        fnum((row or {}).get("change_15"), 0),
        fnum((row or {}).get("turnover_5m") or (row or {}).get("turnover_3m"), 0),
    )


def _v301_rows_from_snapshot(limit: int = 180) -> List[Dict[str, Any]]:
    try:
        obj = load_json(FILES.get("strategy_material_snapshot", BASE_DIR / "strategy_material_snapshot.json"), {})
        if isinstance(obj, dict) and str(obj.get("version") or "") == BOT_VERSION and str(obj.get("strategy_key") or "") == STRATEGY_KEY:
            rows = obj.get("rows") or []
            if isinstance(rows, list):
                return [r for r in rows[:limit] if isinstance(r, dict)]
    except Exception:
        pass
    try:
        rows = tail_jsonl(FILES.get("strategy_material_latest", BASE_DIR / "strategy_material_latest.jsonl"), max_lines=limit)
        return [r for r in rows or [] if isinstance(r, dict) and str(r.get("strategy_key") or r.get("paper_strategy_key") or "") == STRATEGY_KEY][:limit]
    except Exception:
        return []


def _v298_common_rows(limit: int = 120) -> List[Dict[str, Any]]:  # type: ignore[override]
    return _v301_rows_from_snapshot(limit)


def _v301_grade_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"S": 0, "A": 0, "B": 0, "C": 0}
    for r in rows or []:
        g = str((r or {}).get("candidate_grade") or "C")
        out[g if g in out else "C"] += 1
    return out


def _v288_row_summary(r: Dict[str, Any]) -> str:  # type: ignore[override]
    t = _v274_ticker(r) or "?"
    label = str(r.get("candidate_grade_label") or r.get("final_entry_label") or "❔ 관찰")
    reasons = _v288_as_list(r.get("final_entry_reasons") or r.get("block_reasons") or r.get("quality_risk_tags"))
    return (
        f"- {t}: {label} / 점수 {fnum(r.get('leader_score') or r.get('score'),0):.1f}"
        f" / 15분 {fnum(r.get('change_15'),0):+.2f}% / 30분 {fnum(r.get('change_30'),0):+.2f}%"
        f" / 상대 {fnum(r.get('leader_rel15_pct'),0):+.2f}% / 3분돈 {fnum(r.get('turnover_3m'),0)/10000:.0f}만"
        f" / 사유 {', '.join(reasons[:4]) if reasons else '-'}"
    )


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows = rows if isinstance(rows, list) else _v301_rows_from_snapshot(180)
    rows = [r for r in rows or [] if isinstance(r, dict) and str(r.get("strategy_key") or r.get("paper_strategy_key") or STRATEGY_KEY) == STRATEGY_KEY]
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade") or "C") == "C"]
    ready = [r for r in rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    st = _v288_closed_stats()
    verdict = "❔ 30건 검증 전" if st["n"] < 30 else ("✅ 통과후보" if st["total"] > 0 and st["wr"] >= 35 else "⚠️ 보정검토" if st["total"] >= -1.0 and st["wr"] >= 30 else "❌ 폐기후보")
    lines = [
        "📊 등급형 전략 성과 /score",
        f"전략: {STRATEGY_LABEL} ({STRATEGY_KEY})",
        f"상태: {verdict} / CLOSED {st['n']}/30 / 조건변경 {'금지' if st['n'] < 30 else '판정가능'}",
        "",
        "[1] 등급 구조",
        f"- ✅ S급 자동매매 후보급 {grade['S']}개 / 🟡 A급 모의검증 {grade['A']}개 / ❔ B급 관찰 {grade['B']}개 / ❌ C급 금지 {grade['C']}개",
        f"- paper OPEN 대상: S/A급만 / 실제 자동매매 후보는 S급만 표시 / 자동매수는 OFF",
        "",
        "[2] 설계 고정값",
        f"- 기본익절 +{V288_TAKE_PROFIT_PCT:.2f}% / 강한흐름 관찰목표 +{LEADER_EXTEND_TARGET_PCT:.2f}% / 보호 +{V288_PROTECT_TRIGGER_PCT:.2f}%→+{V288_PROTECT_FLOOR_PCT:.2f}% / 손절 {V288_STOP_LOSS_PCT:.2f}% / 시간 {V288_TIME_EXIT_MIN:.0f}분",
        "- 장세필터: 강함 / 1시간 관찰형 단타 / 5~10분 초단타 아님",
        "",
        "[3] S급 자동매매 후보급 TOP]",
        *(_v288_row_summary(r) for r in s_rows[:5]),
        *( ["- 없음"] if not s_rows else [] ),
        "",
        "[4] A급 모의매매 후보 TOP]",
        *(_v288_row_summary(r) for r in a_rows[:5]),
        *( ["- 없음"] if not a_rows else [] ),
        "",
        "[5] CLOSED 성과",
        f"- {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['wr']:.1f}% / 순합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 최대 {st['max']:+.2f}% / 최소 {st['min']:+.2f}%",
    ]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(240 if full else 180)
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade") or "C") == "C"]
    c = Counter()
    for r in c_rows:
        for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
            c[str(x)] += 1
    lines = [
        "🔍 등급별 후보품질 /quality",
        f"전략: {STRATEGY_LABEL}",
        f"- S {grade['S']} / A {grade['A']} / B {grade['B']} / C {grade['C']}",
        "",
        "[S급 자동매매 후보급]",
        *(_v288_row_summary(r) for r in s_rows[:5]),
        *( ["- 없음"] if not s_rows else [] ),
        "",
        "[A급 모의매매 후보]",
        *(_v288_row_summary(r) for r in a_rows[:6]),
        *( ["- 없음"] if not a_rows else [] ),
        "",
        "[B급 관찰/복기 후보]",
        *(_v288_row_summary(r) for r in b_rows[:6]),
        *( ["- 없음"] if not b_rows else [] ),
        "",
        "[C급 금지 사유 TOP]",
        *(f"- {k}: {v}" for k, v in c.most_common(8)),
        *( ["- 없음"] if not c else [] ),
    ]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(240 if full else 180)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    return "\n".join([
        "👀 등급별 전략 감시 /strategy_watch",
        f"전략: {STRATEGY_LABEL}",
        "- S급=미래 자동매매 후보급 / A급=모의검증 / B급=관찰 / C급=금지",
        "- 폐기된 저점쓸림/눌림/7전략은 기본 출력에서 제거됨",
        "",
        "[S급]",
        *(_v288_row_summary(r) for r in s_rows[:8]),
        *( ["- 없음"] if not s_rows else [] ),
        "",
        "[A급]",
        *(_v288_row_summary(r) for r in a_rows[:8]),
        *( ["- 없음"] if not a_rows else [] ),
        "",
        "[B급 관찰]",
        *(_v288_row_summary(r) for r in b_rows[:8]),
        *( ["- 없음"] if not b_rows else [] ),
        "",
        "[정보 재확인]",
        *(_v288_row_summary(r) for r in recheck[:6]),
        *( ["- 없음"] if not recheck else [] ),
    ])

_v301_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v301_base_health_text()
    text = text.replace("저점 쓸림 후 VWAP 회복 단타", STRATEGY_LABEL)
    text = text.replace("sweep_vwap_recovery", STRATEGY_KEY)
    text = text.replace("공장: sweep/vwap 판정", "공장: leader 등급판정")
    text = text.replace("전략: sweep/vwap 판정", "전략: leader 등급판정")
    text = text.replace("공장: leader_momentum_continuation 단일 후보 저장", "공장: leader_momentum_continuation 등급 후보 저장")
    return text


def _v301_compact_snapshot_row(r: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "ticker", "market", "current_price", "strategy_key", "strategy_label", "paper_strategy_key", "paper_strategy_label",
        "candidate_grade", "candidate_grade_label", "auto_ready", "auto_ready_label", "score", "leader_score",
        "final_entry_action", "final_entry_label", "final_entry_reasons", "trade_ready", "paper_bot_open", "open_eligible",
        "change_15", "change_30", "change_5", "change_3", "turnover_3m", "turnover_5m", "leader_rel15_pct",
        "vwap_gap_pct", "ema5_gap_pct", "spread_pct", "buy_ratio", "micro_fresh", "ws_fresh", "block_reasons", "quality_risk_tags",
        "entry_context", "take_profit_pct", "extended_target_pct", "protect_trigger_pct", "protect_floor_pct", "stop_loss_pct", "time_exit_min",
        "created_at", "created_at_text", "scan_id", "brain_version", "version", "one_liner",
    ]
    out = {k: r.get(k) for k in keys if k in r}
    out.setdefault("ticker", _v274_ticker(r))
    out.setdefault("strategy_key", STRATEGY_KEY)
    out.setdefault("strategy_label", STRATEGY_LABEL)
    out.setdefault("paper_strategy_key", STRATEGY_KEY)
    out.setdefault("paper_strategy_label", STRATEGY_LABEL)
    out.setdefault("version", BOT_VERSION)
    out.setdefault("brain_version", BOT_VERSION)
    return out


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Any) -> None:  # type: ignore[override]
    nowv = now_ts()
    rows = list(strict_rows or []) + list(shadow_rows or [])
    compact_rows = [_v301_compact_snapshot_row(r) for r in rows[:V288_MATERIAL_MAX_ROWS if 'V288_MATERIAL_MAX_ROWS' in globals() else 160]]
    grade = _v301_grade_counts(compact_rows)
    ready_n = len([r for r in compact_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in compact_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    if hasattr(rejects, "most_common"):
        reject_payload = dict(rejects.most_common(32))
    elif isinstance(rejects, dict):
        reject_payload = dict(list(rejects.items())[:32])
    else:
        reject_payload = {}
    payload = {
        "version": BOT_VERSION,
        "schema": "leader_momentum_continuation_v301_grade_latest",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or ""),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": ready_n,
        "recheck_count": recheck_n,
        "grade_counts": grade,
        "market_context": {k: (market or {}).get(k) for k in list((market or {}).keys())[:40]} if isinstance(market, dict) else {},
        "rejects": reject_payload,
        "rows": compact_rows,
        "note": "v301: S/A/B/C 등급 후보. S=미래 자동매매 후보, A=모의검증, B=관찰, C=금지.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
    save_json(FILES["strategy_lab_summary"], {k: payload[k] for k in ("version", "updated_ts", "updated_text", "scan_id", "strategy_key", "strategy_label", "strict_count", "shadow_count", "trade_ready_count", "recheck_count", "grade_counts", "rejects", "note")})
    with _state_lock:
        STATE["v301_strategy_snapshot_written"] = len(compact_rows)
        STATE["v301_grade_counts"] = grade
        STATE["v301_strategy_snapshot_ts"] = nowv


def _v288_write_command_caches(reason: str = "manual") -> None:  # type: ignore[override]
    try:
        rows = _v301_rows_from_snapshot(180)
        score = _v288_score_text(rows)
        quality = _v288_quality_text(False)
        watch = _v288_strategy_watch_text(False)
        health = _v288_health_text()
        save_json(FILES.get("score_summary", BASE_DIR / "clean_score_summary.json"), _v288_cache_payload(score, "score"))
        save_json(FILES.get("quality_summary", BASE_DIR / "clean_quality_summary.json"), _v288_cache_payload(quality, "quality"))
        save_json(FILES.get("strategy_watch_summary", BASE_DIR / "clean_strategy_watch_summary.json"), _v288_cache_payload(watch, "strategy_watch"))
        save_json(FILES.get("health_snapshot", BASE_DIR / "clean_health_snapshot.json"), _v288_cache_payload(health, "health"))
    except Exception as exc:
        log_error("v301_write_command_caches", exc)

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V301_NOTE
    STATE["v301_active_strategy"] = STRATEGY_KEY
    STATE["candidate_grade_mode"] = "S/A/B/C"
    STATE["retired_strategy_active_output"] = False
except Exception as exc:
    log_error("v301_init", exc)



# =============================================================================
# v2.13.302: market-mode S/A/B/C grading + exclude split
# - S = 자동매매 후보 중 상위권, A = 보통 자동매매 후보
# - B = 모의매매 검증 후보, C = 관찰 후보, X = 제외/차단
# - 장세 강함/보통/약함 모드로 등급과 청산값을 다르게 둔다.
# - 정밀보강은 고정 개수로 자르지 않고, 기존 정보 우선순위 큐를 유지한다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.302"
STRATEGY_KEY = "leader_momentum_continuation"
STRATEGY_NAME = "장세 선택형 주도코인 추세 지속 전략"
STRATEGY_LABEL = STRATEGY_NAME
V302_NOTE = "v302: S/A/B/C 의미 재정의 + 장세 강함/보통/약함 모드. S/A=미래 자동매매 후보, B=paper 검증, C=관찰, X=제외."

# 장세 모드 기준. 정밀대상 개수 제한이 아니라 현재 시장 상태를 등급/청산 목표에 반영한다.
LEADER_NORMAL_ALT_UP15_RATIO_MIN = float(os.getenv("CLEAN_LEADER_NORMAL_ALT_UP15_RATIO_MIN", "32"))
LEADER_NORMAL_TOP30_UP15_MIN = int(os.getenv("CLEAN_LEADER_NORMAL_TOP30_UP15_MIN", "8"))
LEADER_NORMAL_AVG15_MIN = float(os.getenv("CLEAN_LEADER_NORMAL_AVG15_MIN", "-0.15"))
LEADER_WEAK_B_SCORE_MIN = float(os.getenv("CLEAN_LEADER_WEAK_B_SCORE_MIN", "10.2"))
LEADER_NORMAL_A_SCORE_MIN = float(os.getenv("CLEAN_LEADER_NORMAL_A_SCORE_MIN", str(LEADER_A_SCORE_MIN)))
LEADER_NORMAL_B_SCORE_MIN = float(os.getenv("CLEAN_LEADER_NORMAL_B_SCORE_MIN", str(LEADER_B_SCORE_MIN)))

# 등급별 청산 기본값. paper_bot은 후보 entry_context의 값을 우선 사용한다.
LEADER_GRADE_RISK = {
    "S": {"tp": 1.20, "ext": 2.00, "pt": 0.70, "pf": 0.35, "sl": -0.60, "tm": 60},
    "A_STRONG": {"tp": 1.10, "ext": 1.80, "pt": 0.65, "pf": 0.35, "sl": -0.60, "tm": 60},
    "A_NORMAL": {"tp": 0.90, "ext": 1.40, "pt": 0.55, "pf": 0.25, "sl": -0.55, "tm": 45},
    "B_STRONG": {"tp": 0.90, "ext": 1.40, "pt": 0.55, "pf": 0.25, "sl": -0.55, "tm": 45},
    "B_NORMAL": {"tp": 0.80, "ext": 1.20, "pt": 0.50, "pf": 0.25, "sl": -0.55, "tm": 45},
    "B_WEAK": {"tp": 0.60, "ext": 1.00, "pt": 0.35, "pf": 0.15, "sl": -0.50, "tm": 45},
    "C": {"tp": 0.0, "ext": 0.0, "pt": 0.0, "pf": 0.0, "sl": 0.0, "tm": 0},
    "X": {"tp": 0.0, "ext": 0.0, "pt": 0.0, "pf": 0.0, "sl": 0.0, "tm": 0},
}

_v302_base_market_context = _v274_market_context

def _v274_market_context(rows: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v302: 장세를 강함/보통/약함 모드로 분리한다.

    장세가 강하지 않다는 이유만으로 전부 차단하지 않고, 등급과 청산 목표를 달리한다.
    정밀보강 범위는 줄이지 않는다.
    """
    ctx = _v302_base_market_context(rows)
    try:
        alt = fnum(ctx.get("leader_alt_up15_ratio"), 0)
        top = int(fnum(ctx.get("leader_top30_up15"), 0))
        avg15 = fnum(ctx.get("leader_avg_change_15"), 0)
        major_drop = bool(ctx.get("leader_major_drop"))
        strong = bool(ctx.get("leader_market_strong"))
        if strong:
            mode = "강함"
        elif (not major_drop) and alt >= LEADER_NORMAL_ALT_UP15_RATIO_MIN and top >= LEADER_NORMAL_TOP30_UP15_MIN and avg15 >= LEADER_NORMAL_AVG15_MIN:
            mode = "보통"
        else:
            mode = "약함"
        ctx.update({
            "leader_market_mode": mode,
            "leader_market_label": mode,
            "leader_market_strong": mode == "강함",
            "leader_market_normal": mode == "보통",
            "leader_market_weak": mode == "약함",
        })
    except Exception as exc:
        try:
            log_error("v302_market_mode", exc)
        except Exception:
            pass
        ctx.update({"leader_market_mode": "확인불가", "leader_market_label": "확인불가"})
    return ctx


def _v302_grade_risk(grade: str, mode: str) -> Dict[str, float]:
    g = str(grade or "X").upper()
    m = str(mode or "약함")
    if g == "S":
        key = "S"
    elif g == "A":
        key = "A_STRONG" if m == "강함" else "A_NORMAL"
    elif g == "B":
        key = "B_STRONG" if m == "강함" else "B_NORMAL" if m == "보통" else "B_WEAK"
    elif g == "C":
        key = "C"
    else:
        key = "X"
    return dict(LEADER_GRADE_RISK.get(key) or LEADER_GRADE_RISK["X"])


def _v302_regrade(ev: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    ev = dict(ev or {})
    mode = str((market or {}).get("leader_market_mode") or (market or {}).get("leader_market_label") or "약함")
    hard = [str(x) for x in _v288_as_list(ev.get("hard"))]
    wait = [str(x) for x in _v288_as_list(ev.get("wait"))]
    good = [str(x) for x in _v288_as_list(ev.get("good"))]
    # v301의 단순 강함 차단 사유를 제거하고 장세모드로 재해석한다.
    hard = [h for h in hard if not h.startswith("장세강함아님")]
    score = fnum(ev.get("score"), 0)
    if mode == "강함":
        score += 2.0 if "장세강함" not in good else 0.0
        if "장세강함" not in good:
            good.append("장세강함")
    elif mode == "보통":
        score += 0.8
        good.append("장세보통")
    elif mode == "약함":
        wait.append("장세약함_저위험관찰")
    else:
        wait.append("장세확인필요")

    ch15 = fnum(ev.get("change_15"), 0)
    ch30 = fnum(ev.get("change_30"), 0)
    money3 = fnum(ev.get("turnover_3m"), 0)
    spread = fnum(ev.get("spread_pct"), 999)
    buy_ratio = fnum(ev.get("buy_ratio"), 0)
    micro_fresh = bool(ev.get("micro_fresh"))
    ws_fresh = bool(ev.get("ws_fresh"))
    rel15 = fnum(ev.get("leader_rel15_pct"), 0)

    grade = "C"
    grade_label = "❔ C급 관찰 후보"
    auto_ready = False
    paper_open = False
    action = "observe"

    if hard:
        grade = "X"
        grade_label = "❌ 제외/차단"
        action = "block"
    elif wait and _v301_wait_is_recheckable(wait) and score >= LEADER_RECHECK_SCORE_MIN:
        grade = "C"
        grade_label = "⚠️ C급 정보 재확인"
        action = "recheck_wait"
    else:
        if mode == "강함" and (not wait) and score >= LEADER_S_SCORE_MIN and ch15 >= LEADER_S_CHANGE15 and ch30 >= LEADER_S_CHANGE30 and money3 >= LEADER_S_MONEY3 and micro_fresh and ws_fresh and spread <= LEADER_S_SPREAD_MAX and buy_ratio >= LEADER_S_BUY_RATIO:
            grade = "S"
            grade_label = "✅ S급 자동매매 상위 후보"
            auto_ready = True
            paper_open = True
            action = "paper_open"
        elif mode in {"강함", "보통"} and (not wait) and score >= (LEADER_A_SCORE_MIN if mode == "강함" else LEADER_NORMAL_A_SCORE_MIN) and micro_fresh and spread <= LEADER_SPREAD_PASS and buy_ratio >= LEADER_BUY_RATIO_BLOCK:
            grade = "A"
            grade_label = "✅ A급 자동매매 일반 후보"
            auto_ready = True
            paper_open = True
            action = "paper_open"
        elif score >= (LEADER_B_SCORE_MIN if mode != "약함" else LEADER_WEAK_B_SCORE_MIN) and (not hard):
            grade = "B"
            grade_label = "🟡 B급 모의매매 검증 후보"
            paper_open = True
            action = "paper_open"
        else:
            grade = "C"
            grade_label = "❔ C급 관찰 후보"
            action = "observe"

    risk = _v302_grade_risk(grade, mode)
    ev.update({
        "score": round(score, 3),
        "leader_score": round(score, 3),
        "hard": hard[:12],
        "wait": wait[:12],
        "good": good[:14],
        "action": action,
        "candidate_grade": grade,
        "candidate_grade_label": grade_label,
        "auto_ready": auto_ready,
        "auto_ready_label": "✅ 자동매매 후보" if grade in {"S", "A"} else "-",
        "paper_grade_open": paper_open,
        "leader_market_mode": mode,
        "leader_market_label": mode,
        "take_profit_pct": risk["tp"],
        "extended_target_pct": risk["ext"],
        "protect_trigger_pct": risk["pt"],
        "protect_floor_pct": risk["pf"],
        "stop_loss_pct": risk["sl"],
        "time_exit_min": risk["tm"],
        "grade_risk_key": f"{grade}_{mode}",
    })
    return ev


def _v302_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    try:
        base = _v301_eval(row, market)
        return _v302_regrade(base, market)
    except Exception as exc:
        try:
            log_error("v302_eval", exc)
        except Exception:
            pass
        return {"score": 0.0, "action": "block", "candidate_grade": "X", "candidate_grade_label": "❌ 제외/차단", "hard": ["v302_eval_error"], "wait": [], "good": []}


try:
    _v301_eval_base_for_v302 = _v301_eval
except Exception:
    _v301_eval_base_for_v302 = None

def _v301_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    # 기존 v301 본문을 직접 재작성하지 않고, 직전 evaluator를 보정한다.
    return _v302_eval(row, market)


def _v300_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v302_eval(row, market)


def _v288_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v302_eval(row, market)


def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    item = dict(row or {})
    # v301 함수명을 다시 호출하면 override 순환이 생길 수 있으므로 저장된 base가 있으면 직접 사용한다.
    try:
        base_ev = globals().get("_v301_eval_base_for_v302")
        if callable(base_ev):
            ev = _v302_regrade(base_ev(item, market), market)
        else:
            # 안전 fallback: v300/v301 이전 코드가 없으면 현재 v302 평가만 사용
            ev = _v302_eval(item, market)
    except Exception:
        ev = _v302_eval(item, market)
    action = str(ev.get("action") or "observe")
    ready = action == "paper_open"
    recheck = action == "recheck_wait"
    grade = str(ev.get("candidate_grade") or "C")
    reasons = ev.get("good") if ready else (ev.get("wait") if recheck else ev.get("hard") or ev.get("wait") or [])
    item.update({
        "schema": "leader_momentum_continuation_v302_grade_mode",
        "brain_version": BOT_VERSION,
        "version": BOT_VERSION,
        "strategy": STRATEGY_LABEL,
        "strategy_name": STRATEGY_LABEL,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "paper_route": STRATEGY_KEY,
        "score": ev.get("score", 0.0),
        "leader_score": ev.get("leader_score", ev.get("score", 0.0)),
        "leader_good": ev.get("good", []),
        "leader_wait": ev.get("wait", []),
        "leader_blocks": ev.get("hard", []),
        "candidate_grade": grade,
        "candidate_grade_label": ev.get("candidate_grade_label"),
        "leader_market_mode": ev.get("leader_market_mode"),
        "auto_ready": bool(ev.get("auto_ready")),
        "auto_ready_label": ev.get("auto_ready_label"),
        "quality_risk_tags": ev.get("hard", []) + ev.get("wait", []),
        "block_reasons": ev.get("hard", []),
        "paper_bot_open": ready,
        "open_eligible": ready,
        "paper_eligible": ready,
        "eligible_for_paper": ready,
        "trade_ready": ready,
        "review_only": not ready,
        "observe_only": not ready,
        "final_entry_action": action if action in {"paper_open", "recheck_wait", "block"} else "observe",
        "final_entry_label": ev.get("candidate_grade_label") or ("✅ 진입 가능" if ready else "⚠️ 정보 재확인 중" if recheck else "❌ 제외/차단" if grade == "X" else "❔ 관찰"),
        "final_entry_reasons": reasons,
        "one_liner": f"- {_v274_ticker(item) or '?'}: {ev.get('candidate_grade_label')} / 장세 {ev.get('leader_market_mode','-')} / 점수 {fnum(ev.get('score'),0):.1f} / 15분 {fnum(ev.get('change_15'),0):+.2f}% / 30분 {fnum(ev.get('change_30'),0):+.2f}% / 3분돈 {fnum(ev.get('turnover_3m'),0)/10000:.0f}만",
        "take_profit_pct": ev.get("take_profit_pct", 0.0),
        "extended_target_pct": ev.get("extended_target_pct", 0.0),
        "protect_trigger_pct": ev.get("protect_trigger_pct", 0.0),
        "protect_floor_pct": ev.get("protect_floor_pct", 0.0),
        "stop_loss_pct": ev.get("stop_loss_pct", 0.0),
        "time_exit_min": ev.get("time_exit_min", 0.0),
    })
    ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx.update({
        "brain_version": BOT_VERSION,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_rule_version": "v302_leader_momentum_market_mode_grade",
        "candidate_grade": grade,
        "candidate_grade_label": ev.get("candidate_grade_label"),
        "leader_market_mode": ev.get("leader_market_mode"),
        "auto_ready": bool(ev.get("auto_ready")),
        "take_profit_pct": ev.get("take_profit_pct", 0.0),
        "extended_target_pct": ev.get("extended_target_pct", 0.0),
        "protect_trigger_pct": ev.get("protect_trigger_pct", 0.0),
        "protect_floor_pct": ev.get("protect_floor_pct", 0.0),
        "stop_loss_pct": ev.get("stop_loss_pct", 0.0),
        "time_exit_min": ev.get("time_exit_min", 0.0),
        "leader_eval": ev,
        "v302_eval": ev,
    })
    item["entry_context"] = ctx
    return item


def _v288_rank_key(row: Dict[str, Any]) -> Tuple[float, float, float, float]:  # type: ignore[override]
    grade_score = {"S": 5.0, "A": 4.0, "B": 3.0, "C": 2.0, "X": 0.0}.get(str((row or {}).get("candidate_grade") or "X"), 0.0)
    return (
        grade_score,
        fnum((row or {}).get("leader_score") or (row or {}).get("score"), 0),
        fnum((row or {}).get("change_15"), 0),
        fnum((row or {}).get("turnover_5m") or (row or {}).get("turnover_3m"), 0),
    )


def _v301_grade_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:  # type: ignore[override]
    out = {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0}
    for r in rows or []:
        g = str((r or {}).get("candidate_grade") or "X").upper()
        if g not in out:
            g = "X"
        out[g] += 1
    return out


def _v302_grade_section(title: str, rows: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    if not rows:
        return [title, "- 없음"]
    return [title] + [_v288_row_summary(r) for r in rows[:limit]]


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows = rows if rows is not None else _v301_rows_from_snapshot(220)
    rows = [r for r in rows or [] if str(r.get("strategy_key") or r.get("paper_strategy_key") or STRATEGY_KEY) == STRATEGY_KEY]
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    x_rows = [r for r in rows if str(r.get("candidate_grade") or "X") == "X"]
    st = _v288_closed_stats()
    verdict = "❔ 30건 검증 전" if st["n"] < 30 else ("✅ 통과후보" if st["total"] > 0 and st["wr"] >= 35 else "⚠️ 보정검토" if st["total"] >= -1.0 and st["wr"] >= 30 else "❌ 폐기후보")
    mode_counter = Counter(str(r.get("leader_market_mode") or "-") for r in rows)
    mode_txt = ", ".join(f"{k} {v}" for k, v in mode_counter.most_common(3)) if mode_counter else "-"
    lines = [
        "📊 등급형 전략 성과 /score",
        f"전략: {STRATEGY_LABEL} ({STRATEGY_KEY})",
        f"상태: {verdict} / CLOSED {st['n']}/30 / 조건변경 {'금지' if st['n'] < 30 else '판정가능'}",
        "",
        "[1] 등급 구조",
        f"- ✅ S급 자동매매 상위 {grade['S']}개 / ✅ A급 자동매매 일반 {grade['A']}개 / 🟡 B급 모의매매 {grade['B']}개 / ❔ C급 관찰 {grade['C']}개 / ❌ 제외 {grade['X']}개",
        "- 실제 자동매매 후보: S/A만. 현재 자동매수 OFF / paper 검증은 S/A/B만.",
        f"- 장세모드: {mode_txt}",
        "",
        "[2] 장세별 청산 구조",
        "- 강함: S/A 중심, 기본 +1.1~1.2%, 강한흐름 +1.8~2.0%",
        "- 보통: A/B 중심, 기본 +0.8~0.9%, 더 보수적 보호익절",
        "- 약함: B/C 중심, 작은 목표·관찰 위주",
        "",
        *(_v302_grade_section("[3] S급 자동매매 상위 TOP]", s_rows, 5)),
        "",
        *(_v302_grade_section("[4] A급 자동매매 일반 TOP]", a_rows, 5)),
        "",
        *(_v302_grade_section("[5] B급 모의매매 TOP]", b_rows, 5)),
        "",
        "[6] CLOSED 성과",
        f"- {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['wr']:.1f}% / 순합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 최대 {st['max']:+.2f}% / 최소 {st['min']:+.2f}%",
    ]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(260 if full else 200)
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    x_rows = [r for r in rows if str(r.get("candidate_grade") or "X") == "X"]
    c = Counter()
    for r in x_rows:
        for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
            c[str(x)] += 1
    lines = [
        "🔍 등급별 후보품질 /quality",
        f"전략: {STRATEGY_LABEL}",
        f"- S {grade['S']} / A {grade['A']} / B {grade['B']} / C {grade['C']} / 제외 {grade['X']}",
        "",
        *(_v302_grade_section("[S급 자동매매 상위]", s_rows, 5)),
        "",
        *(_v302_grade_section("[A급 자동매매 일반]", a_rows, 6)),
        "",
        *(_v302_grade_section("[B급 모의매매 검증]", b_rows, 8)),
        "",
        *(_v302_grade_section("[C급 관찰/복기]", c_rows, 8)),
        "",
        "[제외/차단 사유 TOP]",
        *(f"- {k}: {v}" for k, v in c.most_common(8)),
        *( ["- 없음"] if not c else [] ),
    ]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(260 if full else 200)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    return "\n".join([
        "👀 등급별 전략 감시 /strategy_watch",
        f"전략: {STRATEGY_LABEL}",
        "- S=자동매매 상위 / A=자동매매 일반 / B=모의매매 / C=관찰 / 제외=차단",
        "- 장세는 강함·보통·약함 모드로 보고, 정보배관은 넓게 유지",
        "",
        *(_v302_grade_section("[S급 자동매매 상위]", s_rows, 8)),
        "",
        *(_v302_grade_section("[A급 자동매매 일반]", a_rows, 8)),
        "",
        *(_v302_grade_section("[B급 모의매매]", b_rows, 10)),
        "",
        *(_v302_grade_section("[C급 관찰]", c_rows, 10)),
        "",
        *(_v302_grade_section("[정보 재확인]", recheck, 6)),
    ])


def _v288_health_text() -> str:  # type: ignore[override]
    text = _v301_base_health_text()
    text = text.replace("저점 쓸림 후 VWAP 회복 단타", STRATEGY_LABEL)
    text = text.replace("sweep_vwap_recovery", STRATEGY_KEY)
    text = text.replace("전략: sweep/vwap 판정", "전략: leader 등급판정")
    text = text.replace("공장: leader_momentum_continuation 등급 후보 저장", "공장: leader_momentum_continuation S/A/B/C 후보 저장")
    text = text.replace("후보 0 / 재확인 0", "후보 0 / 재확인 0")
    return text


def _v301_compact_snapshot_row(r: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    keys = [
        "ticker", "market", "current_price", "strategy_key", "strategy_label", "paper_strategy_key", "paper_strategy_label",
        "candidate_grade", "candidate_grade_label", "leader_market_mode", "auto_ready", "auto_ready_label", "score", "leader_score",
        "final_entry_action", "final_entry_label", "final_entry_reasons", "trade_ready", "paper_bot_open", "open_eligible",
        "change_15", "change_30", "change_5", "change_3", "turnover_3m", "turnover_5m", "leader_rel15_pct",
        "vwap_gap_pct", "ema5_gap_pct", "spread_pct", "buy_ratio", "micro_fresh", "ws_fresh", "block_reasons", "quality_risk_tags",
        "entry_context", "take_profit_pct", "extended_target_pct", "protect_trigger_pct", "protect_floor_pct", "stop_loss_pct", "time_exit_min",
        "created_at", "created_at_text", "scan_id", "brain_version", "version", "one_liner",
    ]
    out = {k: r.get(k) for k in keys if k in r}
    out.setdefault("ticker", _v274_ticker(r))
    out.setdefault("strategy_key", STRATEGY_KEY)
    out.setdefault("strategy_label", STRATEGY_LABEL)
    out.setdefault("paper_strategy_key", STRATEGY_KEY)
    out.setdefault("paper_strategy_label", STRATEGY_LABEL)
    out.setdefault("version", BOT_VERSION)
    out.setdefault("brain_version", BOT_VERSION)
    return out


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Any) -> None:  # type: ignore[override]
    nowv = now_ts()
    rows = list(strict_rows or []) + list(shadow_rows or [])
    compact_rows = [_v301_compact_snapshot_row(r) for r in rows[:V288_MATERIAL_MAX_ROWS if 'V288_MATERIAL_MAX_ROWS' in globals() else 180]]
    grade = _v301_grade_counts(compact_rows)
    ready_n = len([r for r in compact_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in compact_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    if hasattr(rejects, "most_common"):
        reject_payload = dict(rejects.most_common(32))
    elif isinstance(rejects, dict):
        reject_payload = dict(list(rejects.items())[:32])
    else:
        reject_payload = {}
    payload = {
        "version": BOT_VERSION,
        "schema": "leader_momentum_continuation_v302_grade_mode_latest",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or ""),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": ready_n,
        "recheck_count": recheck_n,
        "grade_counts": grade,
        "market_context": {k: (market or {}).get(k) for k in list((market or {}).keys())[:60]} if isinstance(market, dict) else {},
        "rejects": reject_payload,
        "rows": compact_rows,
        "note": "v302: S/A=자동매매 후보, B=paper 검증, C=관찰, X=제외. 장세 강함/보통/약함 모드.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
    save_json(FILES["strategy_lab_summary"], {k: payload[k] for k in ("version", "updated_ts", "updated_text", "scan_id", "strategy_key", "strategy_label", "strict_count", "shadow_count", "trade_ready_count", "recheck_count", "grade_counts", "rejects", "note")})
    with _state_lock:
        STATE["v302_strategy_snapshot_written"] = len(compact_rows)
        STATE["v302_grade_counts"] = grade
        STATE["v302_strategy_snapshot_ts"] = nowv
        STATE["candidate_grade_mode"] = "S/A/B/C/X"

try:
    # v302에서 v301 원 evaluator를 보존한다. 이미 override된 경우를 막기 위한 안전 장치다.
    if "_v301_eval_base_for_v302" not in globals():
        _v301_eval_base_for_v302 = globals().get("_v301_eval")
except Exception:
    pass
try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V302_NOTE
    STATE["v302_active_strategy"] = STRATEGY_KEY
    STATE["candidate_grade_mode"] = "S/A/B/C/X"
    STATE["retired_strategy_active_output"] = False
except Exception as exc:
    log_error("v302_init", exc)


# =============================================================================
# v2.13.303: staged information pipeline + grade redistribution
# - 전체시장 허브는 유지하되, 가벼운 중간 분류 → 정밀보강 → micro/WS → 등급판정으로 단계화한다.
# - 고정 개수 목표가 아니라 정보 필요성/우선순위로 정밀대상을 올리고, 서버 보호상한은 안전장치로만 둔다.
# - 3분거래대금부족/상대강도부족/WS·micro 재확인 같은 약점은 바로 제외하지 않고 C/B로 남긴다.
# - 제외(X)는 진짜 차단 사유만 사용한다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.303"
STRATEGY_KEY = "leader_momentum_continuation"
STRATEGY_NAME = "장세 선택형 주도코인 추세 지속 전략"
STRATEGY_LABEL = STRATEGY_NAME
V303_NOTE = "v303: 450 전체시장 → 중간분류 → 정밀보강 → micro/WS → S/A/B/C/제외 단계형 배관. 제외는 진짜 차단만."

# 고정 개수 목표가 아니라 가벼운 중간 분류 하한값이다. 값은 서버 보호/잡음 제거용이다.
V303_MID_SCORE_MIN = float(os.getenv("CLEAN_V303_MID_SCORE_MIN", "0.85"))
V303_MID_TURNOVER24_MIN = float(os.getenv("CLEAN_V303_MID_TURNOVER24_MIN", "250000000"))
V303_MID_RANK_MAX = int(os.getenv("CLEAN_V303_MID_RANK_MAX", "260"))
V303_MID_MOVE24_MIN = float(os.getenv("CLEAN_V303_MID_MOVE24_MIN", "0.80"))
V303_PRIORITY_TTL_SEC = float(os.getenv("CLEAN_V303_PRIORITY_TTL_SEC", "420"))

# 제외는 진짜 차단만. 나머지 약점은 C/B에서 관찰한다.
V303_TRUE_BLOCK_PREFIXES = (
    "스테이블", "거래정지", "비정상", "가격없음", "price_missing", "반복손실강한차단", "중복OPEN",
    "대형주는 시장참고용", "스프레드넓음", "돈은큰데가격반응없음", "고점바로밑추격", "정보끝까지부족",
)
V303_OBSERVE_PREFIXES = (
    "1분거래대금부족", "3분거래대금부족", "5분흐름부족", "15분주도부족", "상대강도부족",
    "micro긴급수집대기", "WS재확인", "매도벽/매도체결압력", "매수체결약함", "장세약함",
)


def _v303_open_tickers() -> set:
    out = set()
    try:
        if 'read_open' in globals() and callable(read_open):
            opened = read_open()
            vals = opened.values() if isinstance(opened, dict) else opened if isinstance(opened, list) else []
            for p in vals:
                if isinstance(p, dict):
                    t = _v298_ticker(p) if '_v298_ticker' in globals() else _v274_ticker(p)
                    if t:
                        out.add(t)
    except Exception:
        pass
    return out


def _v303_read_priority_tickers() -> List[str]:
    """v303: 정밀 재순환 우선 ticker.

    일반 ws/micro target 파일을 그대로 다음 precision 우선순위에 먹이면 target 폭이 눈덩이처럼 커진다.
    precision_retry와 micro_urgent처럼 '정보가 실제로 부족했던 후보'만 앞줄로 되돌리고,
    OPEN 중 코인은 항상 보호한다.
    """
    out: List[str] = []
    seen: set = set()
    paths = [
        FILES.get("sweep_precision_retry", BASE_DIR / "clean_sweep_precision_retry.json"),
        FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"),
    ]
    for p in paths:
        try:
            tickers = _v295_read_target_payload_tickers(Path(p), ttl_sec=V303_PRIORITY_TTL_SEC) if '_v295_read_target_payload_tickers' in globals() else []
        except Exception:
            tickers = []
        for t in tickers:
            tt = _ticker_from_any(t)
            if tt and tt not in STABLE_EXCLUDED and tt not in seen:
                seen.add(tt); out.append(tt)
    for tt in _v303_open_tickers():
        if tt and tt not in seen:
            seen.add(tt); out.append(tt)
    return out


def _v303_light_stage_score(row: Dict[str, Any], priority_set: Optional[set] = None) -> Tuple[float, List[str]]:
    """가벼운 중간 분류 점수. 전략 점수가 아니라 '정밀보강을 줄 가치' 판단이다."""
    if not isinstance(row, dict):
        return -999.0, ["row_invalid"]
    t = _v298_ticker(row) if '_v298_ticker' in globals() else _v274_ticker(row)
    if not t or t in STABLE_EXCLUDED:
        return -999.0, ["excluded_symbol"]
    priority_set = priority_set or set()
    rank = fint(row.get("turnover_rank"), 9999)
    turnover24 = fnum(row.get("turnover_24h") or row.get("money_proxy_24h") or row.get("acc_trade_value_24h"), 0.0)
    ch24 = abs(fnum(row.get("change_24h") or row.get("fluctate_rate_24H") or row.get("change_rate"), 0.0))
    price = fnum(row.get("current_price") or row.get("price"), 0.0)
    ext = bool(row.get("ws_fresh")) or bool(row.get("micro_fresh")) or str(row.get("ws_row_status") or "") == "fresh" or str(row.get("micro_row_status") or "") == "fresh"
    reasons: List[str] = []
    score = 0.0
    if t in priority_set:
        score += 4.0; reasons.append("retry/urgent/open")
    if rank <= V303_MID_RANK_MAX:
        score += max(0.0, (V303_MID_RANK_MAX + 1 - rank) / max(1.0, V303_MID_RANK_MAX)) * 1.5; reasons.append("rank")
    if turnover24 >= V303_MID_TURNOVER24_MIN:
        score += min(2.5, turnover24 / max(1.0, 1_000_000_000.0)); reasons.append("money24")
    if ch24 >= V303_MID_MOVE24_MIN:
        score += min(2.0, ch24 / max(0.1, PRECISION_MOVE_TRIGGER_PCT if 'PRECISION_MOVE_TRIGGER_PCT' in globals() else 1.0)); reasons.append("move24")
    if ext:
        score += 1.2; reasons.append("external_fresh")
    if price > 0:
        score += 0.1
    return score, reasons


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v303 단계형 정밀대상 선정.

    전체시장은 그대로 받되, 중간 분류를 통과한 대상과 retry/urgent/OPEN을 정밀보강한다.
    개수 목표가 아니라 가벼운 정보 필요성 하한 + 서버 보호상한이다.
    """
    valid = [r for r in (rows or []) if isinstance(r, dict) and (_v298_ticker(r) if '_v298_ticker' in globals() else _v274_ticker(r))]
    by_ticker: Dict[str, Dict[str, Any]] = {}
    priority_tickers = _v303_read_priority_tickers()
    priority_set = set(priority_tickers)
    for r in valid:
        t = _v298_ticker(r) if '_v298_ticker' in globals() else _v274_ticker(r)
        if not t or t in STABLE_EXCLUDED:
            continue
        old = by_ticker.get(t)
        if old is None:
            by_ticker[t] = r
        else:
            ns, _ = _v303_light_stage_score(r, priority_set)
            os, _ = _v303_light_stage_score(old, priority_set)
            if ns > os:
                by_ticker[t] = r

    selected: List[Dict[str, Any]] = []
    seen: set = set()
    mid_rows: List[Tuple[float, Dict[str, Any], List[str]]] = []

    def push(row: Optional[Dict[str, Any]], source: str, score: Optional[float] = None, reasons: Optional[List[str]] = None) -> None:
        if not isinstance(row, dict):
            return
        t = _v298_ticker(row) if '_v298_ticker' in globals() else _v274_ticker(row)
        if not t or t in STABLE_EXCLUDED or t in seen:
            return
        rr = dict(row)
        rr["precision_target_source"] = source
        if score is not None:
            rr["stage_mid_score"] = round(score, 3)
        if reasons:
            rr["stage_mid_reasons"] = reasons[:6]
        seen.add(t)
        selected.append(rr)

    for t in priority_tickers:
        sc, rs = _v303_light_stage_score(by_ticker.get(t) or {}, priority_set)
        push(by_ticker.get(t), "retry_urgent_open", sc, rs)

    for t, r in by_ticker.items():
        if t in seen:
            continue
        sc, rs = _v303_light_stage_score(r, priority_set)
        # 정보 필요성 하한이다. 숫자를 맞추기 위한 제한이 아니다.
        if sc >= V303_MID_SCORE_MIN or fint(r.get("turnover_rank"), 9999) <= PRECISION_TURNOVER_CORE_RANK:
            mid_rows.append((sc, r, rs))
    mid_rows.sort(key=lambda x: x[0], reverse=True)

    safety = max(1, int(globals().get("V298_TARGET_SAFETY_MAX", globals().get("PRECISION_REFRESH_MAX_SAFETY", 220))))
    for sc, r, rs in mid_rows:
        push(r, "stage2_mid_filter", sc, rs)
        if len(selected) >= safety:
            break
    with _state_lock:
        STATE["v303_stage1_total"] = len(valid)
        STATE["v303_stage2_mid_candidates"] = len(mid_rows) + len([t for t in priority_tickers if t in by_ticker])
        STATE["v303_stage3_precision_targets"] = len(selected)
        STATE["precision_target_note"] = f"v303 stage pipeline total {len(valid)} -> mid {STATE['v303_stage2_mid_candidates']} -> precision {len(selected)} / priority {len([t for t in priority_tickers if t in by_ticker])}"
        STATE["precision_target_source"] = "v303_stage_mid_priority_queue"
    return selected


def _v303_is_true_block(reason: str) -> bool:
    s = str(reason or "")
    if not s:
        return False
    # 일부 약점은 관찰/모의검증으로 남긴다.
    if any(s.startswith(p) for p in V303_OBSERVE_PREFIXES):
        return False
    # 스프레드는 원래 진짜 비용이므로 제외. 다만 애매 문구는 재확인/관찰이다.
    if s.startswith("스프레드애매"):
        return False
    return any(s.startswith(p) for p in V303_TRUE_BLOCK_PREFIXES)


def _v303_regrade(ev: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    ev = dict(ev or {})
    mode = str((market or {}).get("leader_market_mode") or (market or {}).get("leader_market_label") or "약함")
    hard0 = [str(x) for x in _v288_as_list(ev.get("hard"))]
    wait0 = [str(x) for x in _v288_as_list(ev.get("wait"))]
    good = [str(x) for x in _v288_as_list(ev.get("good"))]
    # 장세는 차단이 아니라 모드로 재해석한다.
    hard0 = [h for h in hard0 if not h.startswith("장세강함아님")]
    true_blocks = [h for h in hard0 if _v303_is_true_block(h)]
    soft_blocks = [h for h in hard0 if h not in true_blocks]
    wait = list(wait0)
    if mode == "강함":
        if "장세강함" not in good:
            good.append("장세강함")
        score = fnum(ev.get("score"), 0) + 2.0
    elif mode == "보통":
        good.append("장세보통")
        score = fnum(ev.get("score"), 0) + 0.9
    elif mode == "약함":
        score = fnum(ev.get("score"), 0) + 0.2
        wait.append("장세약함_관찰우선")
    else:
        score = fnum(ev.get("score"), 0)
        wait.append("장세확인필요")

    ch15 = fnum(ev.get("change_15"), 0)
    ch30 = fnum(ev.get("change_30"), 0)
    ch5 = fnum(ev.get("change_5"), 0)
    money3 = fnum(ev.get("turnover_3m"), 0)
    spread = fnum(ev.get("spread_pct"), 999)
    buy_ratio = fnum(ev.get("buy_ratio"), 0)
    micro_fresh = bool(ev.get("micro_fresh"))
    ws_fresh = bool(ev.get("ws_fresh"))
    rel15 = fnum(ev.get("leader_rel15_pct"), 0)

    grade = "C"; label = "❔ C급 관찰 후보"; action = "observe"; auto_ready = False; paper_open = False
    # S/A는 보수. B/C는 넓게 살린다. 제외는 true block만.
    if true_blocks:
        grade = "X"; label = "❌ 제외/차단"; action = "block"
    elif wait and _v301_wait_is_recheckable(wait) and score >= LEADER_RECHECK_SCORE_MIN:
        grade = "C"; label = "⚠️ C급 정보 재확인"; action = "recheck_wait"
    elif mode == "강함" and not wait and not soft_blocks and score >= LEADER_S_SCORE_MIN and ch15 >= LEADER_S_CHANGE15 and ch30 >= LEADER_S_CHANGE30 and money3 >= LEADER_S_MONEY3 and micro_fresh and ws_fresh and spread <= LEADER_S_SPREAD_MAX and buy_ratio >= LEADER_S_BUY_RATIO:
        grade = "S"; label = "✅ S급 자동매매 상위 후보"; action = "paper_open"; auto_ready = True; paper_open = True
    elif mode in {"강함", "보통"} and not wait and not soft_blocks and score >= (LEADER_A_SCORE_MIN if mode == "강함" else LEADER_NORMAL_A_SCORE_MIN) and micro_fresh and spread <= LEADER_SPREAD_PASS and buy_ratio >= LEADER_BUY_RATIO_BLOCK:
        grade = "A"; label = "✅ A급 자동매매 일반 후보"; action = "paper_open"; auto_ready = True; paper_open = True
    else:
        # B급은 자동매매가 아니라 paper 검증 후보다. 명확한 차단이 없고 최소한의 추세/돈흐름이 있으면 살린다.
        b_min = LEADER_B_SCORE_MIN if mode in {"강함", "보통"} else LEADER_WEAK_B_SCORE_MIN
        has_basic_trend = (ch15 > 0 or ch30 > 0 or ch5 > 0.2 or rel15 > 0.0)
        has_basic_money = money3 >= max(1_000_000.0, LEADER_MONEY3_MIN * 0.30)
        has_too_bad_micro = (spread >= LEADER_SPREAD_BLOCK) or (buy_ratio > 0 and buy_ratio <= max(0.10, LEADER_BUY_RATIO_BLOCK - 0.08))
        if mode in {"강함", "보통"} and score >= b_min and has_basic_trend and has_basic_money and not has_too_bad_micro:
            grade = "B"; label = "🟡 B급 모의매매 검증 후보"; action = "paper_open"; paper_open = True
        else:
            grade = "C"; label = "❔ C급 관찰 후보"; action = "observe"

    risk = _v302_grade_risk(grade, mode) if '_v302_grade_risk' in globals() else {"tp": 0, "ext": 0, "pt": 0, "pf": 0, "sl": 0, "tm": 0}
    final_hard = true_blocks if grade == "X" else []
    final_wait = list(dict.fromkeys(wait + soft_blocks))[:14]
    ev.update({
        "score": round(score, 3),
        "leader_score": round(score, 3),
        "hard": final_hard,
        "wait": final_wait,
        "good": good[:16],
        "candidate_grade": grade,
        "candidate_grade_label": label,
        "action": action,
        "auto_ready": auto_ready,
        "auto_ready_label": "✅ 자동매매 후보" if grade in {"S", "A"} else "-",
        "paper_grade_open": paper_open,
        "leader_market_mode": mode,
        "leader_market_label": mode,
        "v303_soft_blocks": soft_blocks[:12],
        "v303_true_blocks": true_blocks[:12],
        "take_profit_pct": risk.get("tp", 0.0),
        "extended_target_pct": risk.get("ext", 0.0),
        "protect_trigger_pct": risk.get("pt", 0.0),
        "protect_floor_pct": risk.get("pf", 0.0),
        "stop_loss_pct": risk.get("sl", 0.0),
        "time_exit_min": risk.get("tm", 0.0),
        "grade_risk_key": f"{grade}_{mode}",
    })
    return ev


def _v303_base_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    base = globals().get("_v301_eval_base_for_v302")
    if callable(base):
        return base(row, market)
    # fallback은 새 조건이 아니라 기존 evaluator가 없을 때만 쓰는 안전 경로다.
    try:
        return _v300_eval(row, market)
    except Exception:
        return {"score": 0.0, "hard": ["base_eval_missing"], "wait": [], "good": []}


def _v303_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return _v303_regrade(_v303_base_eval(row, market), market)
    except Exception as exc:
        try:
            log_error("v303_eval", exc)
        except Exception:
            pass
        return {"score": 0.0, "action": "block", "candidate_grade": "X", "candidate_grade_label": "❌ 제외/차단", "hard": ["v303_eval_error"], "wait": [], "good": []}


def _v301_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v303_eval(row, market)

def _v300_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v303_eval(row, market)

def _v288_eval(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    return _v303_eval(row, market)


def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    item = dict(row or {})
    ev = _v303_eval(item, market)
    action = str(ev.get("action") or "observe")
    ready = action == "paper_open"
    recheck = action == "recheck_wait"
    grade = str(ev.get("candidate_grade") or "C")
    reasons = ev.get("good") if ready else (ev.get("wait") if recheck or grade in {"B", "C"} else ev.get("hard") or ev.get("wait") or [])
    item.update({
        "schema": "leader_momentum_continuation_v303_stage_grade",
        "brain_version": BOT_VERSION,
        "version": BOT_VERSION,
        "strategy": STRATEGY_LABEL,
        "strategy_name": STRATEGY_LABEL,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "route": STRATEGY_KEY,
        "paper_route": STRATEGY_KEY,
        "score": ev.get("score", 0.0),
        "leader_score": ev.get("leader_score", ev.get("score", 0.0)),
        "leader_good": ev.get("good", []),
        "leader_wait": ev.get("wait", []),
        "leader_blocks": ev.get("hard", []),
        "candidate_grade": grade,
        "candidate_grade_label": ev.get("candidate_grade_label"),
        "leader_market_mode": ev.get("leader_market_mode"),
        "auto_ready": bool(ev.get("auto_ready")),
        "auto_ready_label": ev.get("auto_ready_label"),
        "quality_risk_tags": (ev.get("hard", []) + ev.get("wait", []))[:16],
        "block_reasons": ev.get("hard", []),
        "observe_reasons": ev.get("wait", []),
        "paper_bot_open": ready,
        "open_eligible": ready,
        "paper_eligible": ready,
        "eligible_for_paper": ready,
        "trade_ready": ready,
        "review_only": not ready,
        "observe_only": not ready,
        "final_entry_action": action if action in {"paper_open", "recheck_wait", "block"} else "observe",
        "final_entry_label": ev.get("candidate_grade_label") or ("✅ 진입 가능" if ready else "⚠️ 정보 재확인 중" if recheck else "❌ 제외/차단" if grade == "X" else "❔ 관찰"),
        "final_entry_reasons": reasons,
        "one_liner": f"- {_v274_ticker(item) or '?'}: {ev.get('candidate_grade_label')} / 장세 {ev.get('leader_market_mode','-')} / 점수 {fnum(ev.get('score'),0):.1f} / 15분 {fnum(ev.get('change_15'),0):+.2f}% / 30분 {fnum(ev.get('change_30'),0):+.2f}% / 3분돈 {fnum(ev.get('turnover_3m'),0)/10000:.0f}만",
        "take_profit_pct": ev.get("take_profit_pct", 0.0),
        "extended_target_pct": ev.get("extended_target_pct", 0.0),
        "protect_trigger_pct": ev.get("protect_trigger_pct", 0.0),
        "protect_floor_pct": ev.get("protect_floor_pct", 0.0),
        "stop_loss_pct": ev.get("stop_loss_pct", 0.0),
        "time_exit_min": ev.get("time_exit_min", 0.0),
    })
    ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
    ctx = dict(ctx)
    ctx.update({
        "brain_version": BOT_VERSION,
        "strategy_key": STRATEGY_KEY,
        "paper_strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "paper_strategy_label": STRATEGY_LABEL,
        "strategy_rule_version": "v303_leader_stage_grade_pipeline",
        "candidate_grade": grade,
        "candidate_grade_label": ev.get("candidate_grade_label"),
        "leader_market_mode": ev.get("leader_market_mode"),
        "auto_ready": bool(ev.get("auto_ready")),
        "take_profit_pct": ev.get("take_profit_pct", 0.0),
        "extended_target_pct": ev.get("extended_target_pct", 0.0),
        "protect_trigger_pct": ev.get("protect_trigger_pct", 0.0),
        "protect_floor_pct": ev.get("protect_floor_pct", 0.0),
        "stop_loss_pct": ev.get("stop_loss_pct", 0.0),
        "time_exit_min": ev.get("time_exit_min", 0.0),
        "leader_eval": ev,
        "v303_eval": ev,
    })
    item["entry_context"] = ctx
    return item


def _v288_rank_key(row: Dict[str, Any]) -> Tuple[float, float, float, float]:  # type: ignore[override]
    grade_score = {"S": 5.0, "A": 4.0, "B": 3.0, "C": 2.0, "X": 0.0}.get(str((row or {}).get("candidate_grade") or "X"), 0.0)
    return (grade_score, fnum((row or {}).get("leader_score") or (row or {}).get("score"), 0), fnum((row or {}).get("change_15"), 0), fnum((row or {}).get("turnover_5m") or (row or {}).get("turnover_3m"), 0))


def _v303_grade_section(title: str, rows: List[Dict[str, Any]], limit: int = 6) -> List[str]:
    if not rows:
        return [title, "- 없음"]
    return [title] + [_v288_row_summary(r) for r in rows[:limit]]


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows = rows if rows is not None else _v301_rows_from_snapshot(240)
    rows = [r for r in rows or [] if isinstance(r, dict) and str(r.get("strategy_key") or r.get("paper_strategy_key") or STRATEGY_KEY) == STRATEGY_KEY]
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    x_rows = [r for r in rows if str(r.get("candidate_grade") or "X") == "X"]
    st = _v288_closed_stats()
    verdict = "❔ 30건 검증 전" if st["n"] < 30 else ("✅ 통과후보" if st["total"] > 0 and st["wr"] >= 35 else "⚠️ 보정검토" if st["total"] >= -1.0 and st["wr"] >= 30 else "❌ 폐기후보")
    mode_counter = Counter(str(r.get("leader_market_mode") or "-") for r in rows)
    mode_txt = ", ".join(f"{k} {v}" for k, v in mode_counter.most_common(3)) if mode_counter else "-"
    stage_txt = f"전체 {STATE.get('v303_stage1_total','-')} → 중간 {STATE.get('v303_stage2_mid_candidates','-')} → 정밀 {STATE.get('v303_stage3_precision_targets','-')}"
    lines = [
        "📊 등급형 전략 성과 /score",
        f"전략: {STRATEGY_LABEL} ({STRATEGY_KEY})",
        f"상태: {verdict} / CLOSED {st['n']}/30 / 조건변경 {'금지' if st['n'] < 30 else '판정가능'}",
        "",
        "[1] 단계형 배관",
        f"- {stage_txt}",
        "- 전체시장은 넓게 보고, 중간분류 통과·retry·urgent·OPEN 중심으로 정밀보강",
        "",
        "[2] 등급 구조",
        f"- ✅ S급 자동매매 상위 {grade['S']}개 / ✅ A급 자동매매 일반 {grade['A']}개 / 🟡 B급 모의매매 {grade['B']}개 / ❔ C급 관찰 {grade['C']}개 / ❌ 제외 {grade['X']}개",
        "- 실제 자동매매 후보: S/A만. paper 검증은 S/A/B. C는 관찰, 제외는 진짜 차단.",
        f"- 장세모드: {mode_txt}",
        "",
        *(_v303_grade_section("[3] S급 자동매매 상위 TOP]", s_rows, 5)),
        "",
        *(_v303_grade_section("[4] A급 자동매매 일반 TOP]", a_rows, 5)),
        "",
        *(_v303_grade_section("[5] B급 모의매매 TOP]", b_rows, 6)),
        "",
        "[6] CLOSED 성과",
        f"- {st['n']}전 {st['wins']}승 {st['losses']}패 / 승률 {st['wr']:.1f}% / 순합산 {st['total']:+.2f}% / 평균 {st['avg']:+.2f}% / 최대 {st['max']:+.2f}% / 최소 {st['min']:+.2f}%",
    ]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(300 if full else 220)
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    x_rows = [r for r in rows if str(r.get("candidate_grade") or "X") == "X"]
    x_counter = Counter()
    c_counter = Counter()
    for r in x_rows:
        for x in _v288_as_list(r.get("block_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
            x_counter[str(x)] += 1
    for r in c_rows:
        for x in _v288_as_list(r.get("observe_reasons") or r.get("final_entry_reasons") or r.get("quality_risk_tags")):
            c_counter[str(x)] += 1
    lines = [
        "🔍 등급별 후보품질 /quality",
        f"전략: {STRATEGY_LABEL}",
        f"- S {grade['S']} / A {grade['A']} / B {grade['B']} / C {grade['C']} / 제외 {grade['X']}",
        "",
        *(_v303_grade_section("[S급 자동매매 상위]", s_rows, 5)),
        "",
        *(_v303_grade_section("[A급 자동매매 일반]", a_rows, 6)),
        "",
        *(_v303_grade_section("[B급 모의매매 검증]", b_rows, 8)),
        "",
        *(_v303_grade_section("[C급 관찰/복기]", c_rows, 10)),
        "",
        "[C급 관찰 사유 TOP]",
        *(f"- {k}: {v}" for k, v in c_counter.most_common(8)),
        *( ["- 없음"] if not c_counter else [] ),
        "",
        "[제외/차단 사유 TOP]",
        *(f"- {k}: {v}" for k, v in x_counter.most_common(8)),
        *( ["- 없음"] if not x_counter else [] ),
    ]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(300 if full else 220)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    recheck = [r for r in rows if str(r.get("final_entry_action")) == "recheck_wait"]
    return "\n".join([
        "👀 등급별 전략 감시 /strategy_watch",
        f"전략: {STRATEGY_LABEL}",
        "- S=자동매매 상위 / A=자동매매 일반 / B=모의매매 / C=관찰 / 제외=진짜 차단",
        "- 병목은 전체시장→중간분류→정밀보강→micro/WS→등급판정 단계로 본다.",
        "",
        *(_v303_grade_section("[S급 자동매매 상위]", s_rows, 8)),
        "",
        *(_v303_grade_section("[A급 자동매매 일반]", a_rows, 8)),
        "",
        *(_v303_grade_section("[B급 모의매매]", b_rows, 10)),
        "",
        *(_v303_grade_section("[C급 관찰]", c_rows, 12)),
        "",
        *(_v303_grade_section("[정보 재확인]", recheck, 8)),
    ])


def _v288_health_text() -> str:  # type: ignore[override]
    text = _v301_base_health_text()
    text = text.replace("저점 쓸림 후 VWAP 회복 단타", STRATEGY_LABEL)
    text = text.replace("sweep_vwap_recovery", STRATEGY_KEY)
    text = text.replace("전략: sweep/vwap 판정", "전략: leader 단계등급판정")
    text = text.replace("전략: leader 등급판정", "전략: leader 단계등급판정")
    text = text.replace("공장: leader_momentum_continuation S/A/B/C 후보 저장", "공장: leader_momentum_continuation 단계형 S/A/B/C 후보 저장")
    text = text.replace("공장: leader_momentum_continuation 단일 후보 저장", "공장: leader_momentum_continuation 단계형 S/A/B/C 후보 저장")
    return text


def _v301_compact_snapshot_row(r: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    keys = [
        "ticker", "market", "current_price", "strategy_key", "strategy_label", "paper_strategy_key", "paper_strategy_label",
        "candidate_grade", "candidate_grade_label", "leader_market_mode", "auto_ready", "auto_ready_label", "score", "leader_score",
        "final_entry_action", "final_entry_label", "final_entry_reasons", "observe_reasons", "trade_ready", "paper_bot_open", "open_eligible",
        "change_15", "change_30", "change_5", "change_3", "turnover_3m", "turnover_5m", "leader_rel15_pct",
        "vwap_gap_pct", "ema5_gap_pct", "spread_pct", "buy_ratio", "micro_fresh", "ws_fresh", "block_reasons", "quality_risk_tags",
        "entry_context", "take_profit_pct", "extended_target_pct", "protect_trigger_pct", "protect_floor_pct", "stop_loss_pct", "time_exit_min",
        "created_at", "created_at_text", "scan_id", "brain_version", "version", "one_liner",
    ]
    out = {k: r.get(k) for k in keys if k in r}
    out.setdefault("ticker", _v274_ticker(r))
    out.setdefault("strategy_key", STRATEGY_KEY)
    out.setdefault("strategy_label", STRATEGY_LABEL)
    out.setdefault("paper_strategy_key", STRATEGY_KEY)
    out.setdefault("paper_strategy_label", STRATEGY_LABEL)
    out.setdefault("version", BOT_VERSION)
    out.setdefault("brain_version", BOT_VERSION)
    return out


def _v288_write_strategy_files(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any], rejects: Any) -> None:  # type: ignore[override]
    nowv = now_ts()
    rows = list(strict_rows or []) + list(shadow_rows or [])
    compact_rows = [_v301_compact_snapshot_row(r) for r in rows[:V288_MATERIAL_MAX_ROWS if 'V288_MATERIAL_MAX_ROWS' in globals() else 220]]
    grade = _v301_grade_counts(compact_rows)
    ready_n = len([r for r in compact_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in compact_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    if hasattr(rejects, "most_common"):
        reject_payload = dict(rejects.most_common(32))
    elif isinstance(rejects, dict):
        reject_payload = dict(list(rejects.items())[:32])
    else:
        reject_payload = {}
    payload = {
        "version": BOT_VERSION,
        "schema": "leader_momentum_continuation_v303_stage_grade_latest",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "scan_id": str(STATE.get("scan_id") or ""),
        "strategy_key": STRATEGY_KEY,
        "strategy_label": STRATEGY_LABEL,
        "strict_count": len(strict_rows or []),
        "shadow_count": len(shadow_rows or []),
        "trade_ready_count": ready_n,
        "recheck_count": recheck_n,
        "grade_counts": grade,
        "stage_counts": {"total": STATE.get("v303_stage1_total"), "mid": STATE.get("v303_stage2_mid_candidates"), "precision_targets": STATE.get("v303_stage3_precision_targets")},
        "market_context": {k: (market or {}).get(k) for k in list((market or {}).keys())[:60]} if isinstance(market, dict) else {},
        "rejects": reject_payload,
        "rows": compact_rows,
        "note": "v303: 단계형 배관 + 등급 재분배. S/A=자동매매 후보, B=paper, C=관찰, X=진짜 차단.",
    }
    save_json(FILES["strategy_material_snapshot"], payload)
    write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
    save_json(FILES["strategy_lab_summary"], {k: payload[k] for k in ("version", "updated_ts", "updated_text", "scan_id", "strategy_key", "strategy_label", "strict_count", "shadow_count", "trade_ready_count", "recheck_count", "grade_counts", "stage_counts", "rejects", "note")})
    with _state_lock:
        STATE["v303_strategy_snapshot_written"] = len(compact_rows)
        STATE["v303_grade_counts"] = grade
        STATE["v303_strategy_snapshot_ts"] = nowv
        STATE["candidate_grade_mode"] = "S/A/B/C/X_v303_stage"

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V303_NOTE
    STATE["v303_active_strategy"] = STRATEGY_KEY
    STATE["candidate_grade_mode"] = "S/A/B/C/X_v303_stage"
    STATE["retired_strategy_active_output"] = False
except Exception as exc:
    try:
        log_error("v303_init", exc)
    except Exception:
        pass



# =============================================================================
# v304: grade-level CLOSED stats + v303 comparison view
# - 전략 조건/등급 기준은 v303 그대로 유지한다.
# - /score에서 S/A/B/C/제외 등급별 CLOSED 성과를 분리 표시한다.
# - paper OPEN 대상과 실제 자동매매 후보군을 성과판에서 분리한다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.306"
V304_NOTE = "v305: v304 등급별 성과판 유지 + 메인봇 Telegram polling 부팅순서/상태기록 수술. 전략 조건 변경 없음."


def _v304_grade_from_row(row: Dict[str, Any]) -> str:
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


def _v304_perf_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = []
    for r in rows or []:
        # paper_bot 계열 장부의 수익률 key가 버전마다 조금씩 달라질 수 있어 후보를 모두 본다.
        vals.append(fnum(r.get("net_pnl_pct") if r.get("net_pnl_pct") is not None else r.get("pnl_pct") if r.get("pnl_pct") is not None else r.get("profit_pct") if r.get("profit_pct") is not None else r.get("return_pct"), 0.0))
    n = len(vals)
    wins = sum(1 for v in vals if v > 0)
    losses = n - wins
    total = sum(vals)
    return {
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": (wins / n * 100.0 if n else 0.0),
        "total": total,
        "avg": (total / n if n else 0.0),
        "max": (max(vals) if vals else 0.0),
        "min": (min(vals) if vals else 0.0),
    }


def _v304_grade_closed_stats() -> Dict[str, Dict[str, Any]]:
    rows = _v288_closed_rows()
    groups: Dict[str, List[Dict[str, Any]]] = {"S": [], "A": [], "B": [], "C": [], "X": [], "U": []}
    for r in rows or []:
        groups.setdefault(_v304_grade_from_row(r), []).append(r)
    out = {g: _v304_perf_stats(groups.get(g, [])) for g in ["S", "A", "B", "C", "X", "U"]}
    out["AUTO"] = _v304_perf_stats((groups.get("S", []) or []) + (groups.get("A", []) or []))
    out["PAPER"] = _v304_perf_stats((groups.get("S", []) or []) + (groups.get("A", []) or []) + (groups.get("B", []) or []))
    out["ALL"] = _v304_perf_stats(rows or [])
    return out


def _v304_stat_line(label: str, st: Dict[str, Any]) -> str:
    n = int(st.get("n", 0) or 0)
    if n <= 0:
        return f"- {label}: 0전 / 아직 없음"
    return f"- {label}: {n}전 {int(st.get('wins',0))}승 {int(st.get('losses',0))}패 / 승률 {fnum(st.get('wr'),0):.1f}% / 순합산 {fnum(st.get('total'),0):+.2f}% / 평균 {fnum(st.get('avg'),0):+.2f}% / 최대 {fnum(st.get('max'),0):+.2f}% / 최소 {fnum(st.get('min'),0):+.2f}%"


def _v304_grade_stats_lines() -> List[str]:
    gs = _v304_grade_closed_stats()
    return [
        "[6] 등급별 CLOSED 성과",
        _v304_stat_line("✅ S급 자동매매 상위", gs["S"]),
        _v304_stat_line("✅ A급 자동매매 일반", gs["A"]),
        _v304_stat_line("🟡 B급 모의매매 검증", gs["B"]),
        _v304_stat_line("❔ C급 관찰", gs["C"]),
        _v304_stat_line("❌ 제외/차단", gs["X"]),
        "",
        "[7] 후보군별 합산",
        _v304_stat_line("S/A 자동매매 후보군", gs["AUTO"]),
        _v304_stat_line("S/A/B paper 검증 후보군", gs["PAPER"]),
        _v304_stat_line("전체 현재전략", gs["ALL"]),
    ]


def _v288_closed_stats() -> Dict[str, Any]:  # type: ignore[override]
    # 전체 전략 성과는 등급별 합산과 같은 source에서 계산한다.
    return _v304_grade_closed_stats()["ALL"]


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows = rows if rows is not None else _v301_rows_from_snapshot(260)
    rows = [r for r in rows or [] if isinstance(r, dict) and str(r.get("strategy_key") or r.get("paper_strategy_key") or STRATEGY_KEY) == STRATEGY_KEY]
    grade = _v301_grade_counts(rows)
    s_rows = [r for r in rows if str(r.get("candidate_grade")) == "S"]
    a_rows = [r for r in rows if str(r.get("candidate_grade")) == "A"]
    b_rows = [r for r in rows if str(r.get("candidate_grade")) == "B"]
    c_rows = [r for r in rows if str(r.get("candidate_grade")) == "C"]
    x_rows = [r for r in rows if str(r.get("candidate_grade") or "X") == "X"]
    st = _v288_closed_stats()
    verdict = "❔ 30건 검증 전" if st["n"] < 30 else ("✅ 통과후보" if st["total"] > 0 and st["wr"] >= 35 else "⚠️ 보정검토" if st["total"] >= -1.0 and st["wr"] >= 30 else "❌ 폐기후보")
    mode_counter = Counter(str(r.get("leader_market_mode") or "-") for r in rows)
    mode_txt = ", ".join(f"{k} {v}" for k, v in mode_counter.most_common(3)) if mode_counter else "-"
    stage_txt = f"전체 {STATE.get('v303_stage1_total','-')} → 중간 {STATE.get('v303_stage2_mid_candidates','-')} → 정밀 {STATE.get('v303_stage3_precision_targets','-')}"
    lines = [
        "📊 등급형 전략 성과 /score",
        f"전략: {STRATEGY_LABEL} ({STRATEGY_KEY})",
        f"상태: {verdict} / CLOSED {int(st.get('n',0))}/30 / 조건변경 {'금지' if int(st.get('n',0)) < 30 else '판정가능'}",
        "",
        "[1] v304 변경점",
        "- v303 단계형 배관/S·A·B·C·제외 구조 유지",
        "- v304 추가: 등급별 CLOSED 승률·순수익 분리 표시",
        "- 조건/청산/등급 기준은 변경 없음",
        "",
        "[2] 단계형 배관",
        f"- {stage_txt}",
        "- 전체시장은 넓게 보고, 중간분류 통과·retry·urgent·OPEN 중심으로 정밀보강",
        "",
        "[3] 현재 후보 등급",
        f"- ✅ S급 자동매매 상위 {grade['S']}개 / ✅ A급 자동매매 일반 {grade['A']}개 / 🟡 B급 모의매매 {grade['B']}개 / ❔ C급 관찰 {grade['C']}개 / ❌ 제외 {grade['X']}개",
        "- 실제 자동매매 후보: S/A만. paper 검증은 S/A/B. C는 관찰, 제외는 진짜 차단.",
        f"- 장세모드: {mode_txt}",
        "",
        *(_v303_grade_section("[4] S/A/B TOP]", s_rows + a_rows + b_rows, 8)),
        "",
        *(_v303_grade_section("[5] C급 관찰 TOP]", c_rows, 8)),
        "",
        *_v304_grade_stats_lines(),
        "",
        "[8] v303과 같이 볼 부분",
        "- v303: 등급 구조/단계형 배관이 정상 분리되는지 확인",
        "- v304: 같은 장부를 S/A/B/C별 승률로 나눠 어느 등급이 살아남는지 확인",
        "- 병목 비교: /health의 단계별 시간, 특히 정밀보강·등급판정·공장 저장",
    ]
    return "\n".join(lines)

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V304_NOTE
    STATE["v304_grade_stats_active"] = True
except Exception as exc:
    try:
        log_error("v304_init", exc)
    except Exception:
        pass


# =============================================================================
# v2.13.306: count-unfixed paper validation + light grade refinement
# - 전략/청산값은 유지한다. S 조건은 건드리지 않는다.
# - paper OPEN 개수 고정으로 전략 검증이 왜곡되지 않게 paper_bot v0.72와 맞춘다.
# - B급은 계속 검증하되, 매도벽/정보 재확인/매수체결 약함이 붙은 약한 B는 OPEN이 아니라 C 관찰로 내린다.
# - 제외(X)는 진짜 차단만 유지한다. C는 놓친 후보 복기용으로 남긴다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.306"
V306_NOTE = "v306: paper OPEN 고정개수 해제 연동 + S 유지, 약한 B는 C 관찰로 경량 보정. 청산/S조건/자동매수 변경 없음."

_v306_base_v303_regrade = _v303_regrade

def _v306_reason_has_any(items: Iterable[Any], patterns: Iterable[str]) -> bool:
    try:
        text = " | ".join(str(x or "") for x in (items or []))
    except Exception:
        text = str(items or "")
    return any(str(p) in text for p in patterns)


def _v303_regrade(ev: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v306 B급 경량 보정.

    S/A 기준과 청산은 유지한다. B는 paper 검증 후보라 계속 살리되,
    최근 결과처럼 매도벽/정보 재확인/매수체결 약함이 붙은 후보가 계속 손절을 만들면
    바로 차단하지 않고 C 관찰로 내려 놓친 후보 복기 대상으로 남긴다.
    """
    out = _v306_base_v303_regrade(ev, market)
    try:
        if str(out.get("candidate_grade") or "").upper() != "B":
            return out
        wait = [str(x) for x in _v288_as_list(out.get("wait"))]
        hard = [str(x) for x in _v288_as_list(out.get("hard"))]
        good = [str(x) for x in _v288_as_list(out.get("good"))]
        reasons = wait + hard
        mode = str(out.get("leader_market_mode") or (market or {}).get("leader_market_mode") or "")
        score = fnum(out.get("score"), 0.0)
        ch15 = fnum(out.get("change_15"), 0.0)
        ch30 = fnum(out.get("change_30"), 0.0)
        money3 = fnum(out.get("turnover_3m"), 0.0)
        spread = fnum(out.get("spread_pct"), 999.0)
        buy_ratio = fnum(out.get("buy_ratio"), 0.0)
        micro_fresh = bool(out.get("micro_fresh"))
        ws_fresh = bool(out.get("ws_fresh"))

        sell_wall = _v306_reason_has_any(reasons, ["매도벽", "매도체결압력"])
        info_wait = _v306_reason_has_any(reasons, ["WS재확인", "micro긴급수집대기", "정보끝까지부족"])
        weak_buy = _v306_reason_has_any(reasons, ["매수체결약함", "매수체결애매"])
        low_money = money3 < max(1_500_000.0, LEADER_MONEY3_MIN * 0.25)
        weak_reaction = (ch15 < max(0.35, LEADER_CHANGE_15_MIN * 0.55) and ch30 < max(0.55, LEADER_CHANGE_30_MIN * 0.45))

        # B라도 S급에 가까운 강한 흐름이면 살린다. 단, S 조건 자체는 변경하지 않는다.
        strong_b_exception = (
            mode == "강함"
            and score >= max(LEADER_A_SCORE_MIN, LEADER_S_SCORE_MIN - 0.8)
            and ch15 >= max(0.90, LEADER_S_CHANGE15 * 0.75)
            and ch30 >= max(1.20, LEADER_S_CHANGE30 * 0.65)
            and money3 >= max(LEADER_MONEY3_MIN * 0.70, 20_000_000.0)
            and micro_fresh
            and ws_fresh
            and spread <= LEADER_S_SPREAD_MAX
            and buy_ratio >= max(0.55, LEADER_S_BUY_RATIO - 0.05)
        )

        demote_reasons: List[str] = []
        if sell_wall and not strong_b_exception:
            demote_reasons.append("v306_B매도벽관찰")
        if info_wait and not (micro_fresh and ws_fresh):
            demote_reasons.append("v306_B정보재확인관찰")
        if weak_buy and buy_ratio < LEADER_BUY_RATIO_PASS:
            demote_reasons.append("v306_B매수체결약함관찰")
        if low_money and weak_reaction:
            demote_reasons.append("v306_B돈흐름반응부족관찰")

        if demote_reasons:
            new_wait = list(dict.fromkeys(demote_reasons + wait))[:14]
            out.update({
                "candidate_grade": "C",
                "candidate_grade_label": "❔ C급 관찰 후보",
                "action": "observe",
                "auto_ready": False,
                "auto_ready_label": "-",
                "paper_grade_open": False,
                "paper_bot_open": False,
                "open_eligible": False,
                "paper_eligible": False,
                "eligible_for_paper": False,
                "trade_ready": False,
                "review_only": True,
                "observe_only": True,
                "final_entry_action": "observe",
                "final_entry_label": "❔ C급 관찰 후보",
                "wait": new_wait,
                "v306_b_demoted_to_c": True,
                "v306_b_demote_reasons": demote_reasons,
            })
    except Exception as exc:
        try:
            log_error("v306_regrade", exc)
        except Exception:
            pass
    return out


_v306_base_score_text = _v288_score_text

def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    text = _v306_base_score_text(rows)
    text = text.replace("[1] v304 변경점", "[1] v306 변경점")
    text = text.replace("- v304 추가: 등급별 CLOSED 승률·순수익 분리 표시", "- v304: 등급별 CLOSED 승률·순수익 분리 표시")
    text = text.replace("- 조건/청산/등급 기준은 변경 없음", "- v306: S 조건·청산값은 유지, 매도벽/정보재확인/매수체결약함 B는 C 관찰로 경량 보정")
    text = text.replace("- 실제 자동매매 후보: S/A만. paper 검증은 S/A/B. C는 관찰, 제외는 진짜 차단.", "- 실제 자동매매 후보: S/A만. paper 검증은 S/A/B지만 개수 고정은 전략 조건으로 쓰지 않음. C는 관찰, 제외는 진짜 차단.")
    text = text.replace("- v304: 같은 장부를 S/A/B/C별 승률로 나눠 어느 등급이 살아남는지 확인", "- v306: paper 개수 고정 해제 후 S/A/B/C별 승률과 B→C 관찰 보정 효과 확인")
    return text

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V306_NOTE
    STATE["v306_b_refine_active"] = True
except Exception as exc:
    try:
        log_error("v306_init", exc)
    except Exception:
        pass


# =============================================================================
# v2.13.307: Telegram multi-command response surgery
# - 전략/등급/청산/후보판정은 v306 그대로 유지한다.
# - 여러 줄 자동 묶음에서 긴 전문을 5개 연속 전송해 1~2분 체감 지연을 만드는 경로를 정리한다.
# - 단독 명령은 기존 상세 출력 유지, 자동 묶음은 compact 요약으로 먼저 응답한다.
# - 명령 전송은 timeout 있는 Bot API 경로로 통일해 reply_text 장기 blocking을 줄인다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.307"
V307_NOTE = "v307: 자동 묶음 compact 응답 + 전송 timeout/시간표 분리. 전략/S·A·B·C/청산/자동매수 변경 없음."
V307_REPLY_TIMEOUT_SEC = float(os.getenv("CLEAN_TELEGRAM_REPLY_TIMEOUT_SEC", "3.5"))
V307_MULTI_CHUNK_LIMIT = int(os.getenv("CLEAN_MULTI_COMPACT_LIMIT", "2800"))


def reply(update, text: str) -> float:  # type: ignore[override]
    """v307 단일 답장 입구.

    python-telegram-bot reply_text가 네트워크 지연 시 자동 묶음 전체를 오래 붙잡던 경로를
    timeout 있는 sendMessage API 입구로 통일한다. 실패해도 handler가 장시간 멈추지 않게 한다.
    """
    st = now_ts()
    try:
        body = str(text or "")
        ok = _telegram_send_api(body, limit=3400, timeout=V307_REPLY_TIMEOUT_SEC)
        if not ok:
            log("v307_reply_send_failed")
    except Exception as exc:
        log_error("v307_reply", exc)
    return max(0.0, now_ts() - st)


def _v307_nonempty_lines(text: str) -> List[str]:
    return [ln.rstrip() for ln in str(text or "").splitlines() if ln.strip()]


def _v307_take_prefixed(lines: List[str], prefixes: Iterable[str], limit: int = 999) -> List[str]:
    out: List[str] = []
    prefs = tuple(str(x) for x in prefixes)
    for ln in lines:
        if ln.startswith(prefs):
            out.append(ln)
            if len(out) >= limit:
                break
    return out


def _v307_compact_score(body: str) -> str:
    lines = _v307_nonempty_lines(body)
    out: List[str] = ["📊 /score 요약 · 자동묶음 compact"]
    # 핵심 헤더
    for ln in lines[:12]:
        if ln.startswith("전략:") or ln.startswith("상태:"):
            out.append(ln)
    # 현재 후보 등급과 운영 원칙
    for ln in lines:
        if ln.startswith("- ✅ S급 자동매매 상위") and "개 /" in ln:
            out.append(ln)
            break
    out += _v307_take_prefixed(lines, ["- 실제 자동매매 후보:", "- 장세모드:"], 3)
    # 등급별 CLOSED / 후보군별 합산
    out.append("[등급별 CLOSED]")
    out += _v307_take_prefixed(lines, ["- ✅ S급 자동매매 상위:", "- ✅ A급 자동매매 일반:", "- 🟡 B급 모의매매 검증:", "- ❔ C급 관찰:", "- ❌ 제외/차단:"], 8)
    out.append("[후보군 합산]")
    out += _v307_take_prefixed(lines, ["- S/A 자동매매 후보군:", "- S/A/B paper 검증 후보군:", "- 전체 현재전략:"], 5)
    # 현재 B TOP은 2개만. 상세는 단독 /score에서 확인.
    b_top = []
    in_top = False
    for ln in lines:
        if ln.startswith("[4] S/A/B TOP"):
            in_top = True
            continue
        if in_top and ln.startswith("[5]"):
            break
        if in_top and ln.startswith("-"):
            b_top.append(ln)
        if len(b_top) >= 2:
            break
    if b_top:
        out.append("[현재 S/A/B TOP 2]")
        out += b_top
    out.append("- 상세 전체표는 /score 단독 실행")
    return "\n".join(dict.fromkeys(out))


def _v307_compact_quality(body: str) -> str:
    lines = _v307_nonempty_lines(body)
    out: List[str] = ["🔍 /quality 요약 · 자동묶음 compact"]
    for ln in lines[:8]:
        if ln.startswith("전략:") or ln.startswith("-"):
            out.append(ln)
    # B 후보 3개, C 후보 5개만 자동묶음에 표시
    for section, cap in (("[B급 모의매매 검증]", 3), ("[C급 관찰/복기]", 5)):
        out.append(section)
        hit = False
        cnt = 0
        for ln in lines:
            if ln == section:
                hit = True
                continue
            if hit and ln.startswith("["):
                break
            if hit and ln.startswith("-"):
                out.append(ln)
                cnt += 1
                if cnt >= cap:
                    break
        if cnt == 0:
            out.append("- 없음")
    # 사유 TOP은 상위 6개
    for section in ("[C급 관찰 사유 TOP]", "[제외/차단 사유 TOP]"):
        out.append(section)
        hit = False
        cnt = 0
        for ln in lines:
            if ln == section:
                hit = True
                continue
            if hit and ln.startswith("["):
                break
            if hit and ln.startswith("-"):
                out.append(ln)
                cnt += 1
                if cnt >= 6:
                    break
        if cnt == 0:
            out.append("- 없음")
    out.append("- 상세 후보 전체는 /quality 단독 실행")
    return "\n".join(out)


def _v307_compact_strategy_watch(body: str) -> str:
    lines = _v307_nonempty_lines(body)
    out: List[str] = ["👀 /strategy_watch 요약 · 자동묶음 compact"]
    for ln in lines[:10]:
        if ln.startswith("전략:") or ln.startswith("-"):
            out.append(ln)
    for section, cap in (("[S급 자동매매 상위]", 2), ("[A급 자동매매 일반]", 2), ("[B급 모의매매]", 3), ("[C급 관찰]", 5), ("[정보 재확인]", 3)):
        out.append(section)
        hit = False
        cnt = 0
        for ln in lines:
            if ln == section:
                hit = True
                continue
            if hit and ln.startswith("["):
                break
            if hit and ln.startswith("-"):
                out.append(ln)
                cnt += 1
                if cnt >= cap:
                    break
        if cnt == 0:
            out.append("- 없음")
    out.append("- 상세 감시는 /strategy_watch 단독 실행")
    return "\n".join(out)


def _v307_compact_for_multi(name: str, body: str) -> str:
    name = str(name or "").lower()
    if name == "score":
        return _v307_compact_score(body)
    if name == "quality":
        return _v307_compact_quality(body)
    if name in {"strategy_watch", "watch"}:
        return _v307_compact_strategy_watch(body)
    # 다른 명령은 너무 길 때만 앞/뒤를 보존한다.
    body = str(body or "")
    if len(body) <= V307_MULTI_CHUNK_LIMIT:
        return body
    head = body[: int(V307_MULTI_CHUNK_LIMIT * 0.72)].rstrip()
    tail = body[-int(V307_MULTI_CHUNK_LIMIT * 0.20):].lstrip()
    return head + "\n...\n- 자동묶음 길이 제한으로 중간 생략. 상세는 해당 명령 단독 실행.\n...\n" + tail


def _v307_process_command_lines(update, context, lines: List[str], source: str = "auto") -> bool:
    lines = [ln for ln in (lines or []) if str(ln).strip().startswith("/")]
    if not lines:
        return False
    total_start = now_ts()
    title = "자동 묶음" if source == "auto" else "/batch"
    ack_send = reply(update, "\n".join([
        f"📦 {title} 명령 접수",
        "- 여러 명령을 compact 요약으로 먼저 표시",
        f"- 실행 {len(lines)}개",
        "- 상세 전문은 각 명령을 단독 실행",
    ]))
    total = len(lines)
    rows: List[Tuple[str, float, float, str]] = []
    for idx, line in enumerate(lines, start=1):
        build_st = now_ts()
        name = _command_name_from_line(line)
        if name == "batch" and source == "auto":
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
                    body = _v307_compact_for_multi(name, body)
                    res = "OK"
                except Exception as exc:
                    log_error(f"multi:{name}", exc)
                    body = f"오류: {exc.__class__.__name__}: {exc}"
                    res = f"ERR {exc.__class__.__name__}"
        build_sec = max(0.0, now_ts() - build_st)
        send_sec = reply(update, f"[{idx}/{total}] /{name} (계산 {build_sec:.2f}s / {res})\n" + body)
        rows.append((name, build_sec, send_sec, res))
    summary = ["🧾 자동 묶음 시간표", f"- 접수 전송: {ack_send:.2f}s"]
    build_total = 0.0
    send_total = ack_send
    for name, build_sec, send_sec, res in rows:
        build_total += build_sec
        send_total += send_sec
        icon = "✅" if res == "OK" else ("❔" if res in {"SKIP", "UNKNOWN"} else "❌")
        summary.append(f"- {icon} /{name}: 계산 {build_sec:.2f}s / 전송 {send_sec:.2f}s / {res}")
    summary.append(f"- 합계: 계산 {build_total:.2f}s / 전송 {send_total:.2f}s / 전체 {now_ts() - total_start:.2f}s")
    summary.append("- 상세 전문은 각 명령을 단독으로 실행")
    reply(update, "\n".join(summary))
    return True


def handle_multi_command_message(update, context) -> bool:  # type: ignore[override]
    text = ""
    try:
        text = getattr(getattr(update, "message", None), "text", "") or ""
    except Exception:
        text = ""
    lines = _extract_command_lines(text)
    if len(lines) <= 1:
        return False
    return _v307_process_command_lines(update, context, lines, source="auto")


def command_batch(update, context) -> None:  # type: ignore[override]
    """v307 /batch도 자동묶음과 같은 compact 경로를 쓴다."""
    lines = ["/health", "/score", "/quality", "/strategy_watch", "/errorlog"]
    _v307_process_command_lines(update, context, lines, source="batch")


try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V307_NOTE
    STATE["v307_multi_compact_active"] = True
except Exception as exc:
    try:
        log_error("v307_init", exc)
    except Exception:
        pass


# =============================================================================
# v2.13.308: first-response latency surgery + strategy S-basket v1
# - 자동묶음 접수 안내 전송이 20초 이상 막히는 경로를 제거한다. 첫 답장은 바로 1번 명령 결과다.
# - 기존 leader_momentum_continuation은 S급만 paper OPEN한다. A/B는 관찰/복기만 남긴다.
# - 첫 추가 전략으로 money_reaccel_s(돈흐름 재가속 S급)를 1차 도입한다.
# - 여러 전략의 S급만 paper OPEN하는 바구니 구조의 1차판이며, 청산/장부/자동매수는 변경하지 않는다.
# =============================================================================
BOT_VERSION = "수익형 v2.13.308"
V308_NOTE = "v308: 자동묶음 접수 전송 생략 + 전략 바구니 S급 전용 1차. leader S 유지, money_reaccel_s 추가. A/B는 관찰."
V308_REPLY_TIMEOUT_SEC = float(os.getenv("CLEAN_TELEGRAM_REPLY_TIMEOUT_SEC", "2.2"))
V308_MULTI_CHUNK_LIMIT = int(os.getenv("CLEAN_MULTI_COMPACT_LIMIT", "2600"))
V308_MONEY_KEY = "money_reaccel_s"
V308_MONEY_LABEL = "돈흐름 재가속 S급"
V308_BASKET_LABEL = "전략 바구니 S급 전용 검증"
V308_MONEY_SCORE_MIN = float(os.getenv("CLEAN_V308_MONEY_SCORE_MIN", "10.2"))
V308_MONEY_3M_MIN = float(os.getenv("CLEAN_V308_MONEY_3M_MIN", "10000000"))
V308_MONEY_5M_MIN = float(os.getenv("CLEAN_V308_MONEY_5M_MIN", "18000000"))
V308_MONEY_CH3_MIN = float(os.getenv("CLEAN_V308_MONEY_CH3_MIN", "0.05"))
V308_MONEY_CH5_MIN = float(os.getenv("CLEAN_V308_MONEY_CH5_MIN", "0.15"))
V308_MONEY_CH15_MIN = float(os.getenv("CLEAN_V308_MONEY_CH15_MIN", "0.55"))
V308_MONEY_CH30_MIN = float(os.getenv("CLEAN_V308_MONEY_CH30_MIN", "0.45"))
V308_MONEY_SPREAD_MAX = float(os.getenv("CLEAN_V308_MONEY_SPREAD_MAX", "0.28"))
V308_MONEY_BUY_RATIO_MIN = float(os.getenv("CLEAN_V308_MONEY_BUY_RATIO_MIN", "0.58"))
V308_MONEY_VWAP_MIN = float(os.getenv("CLEAN_V308_MONEY_VWAP_MIN", "-0.08"))
V308_MONEY_EMA5_MIN = float(os.getenv("CLEAN_V308_MONEY_EMA5_MIN", "-0.08"))

# v307 reply는 이미 Bot API + timeout 경로다. v308은 timeout만 조금 줄인다.
V307_REPLY_TIMEOUT_SEC = V308_REPLY_TIMEOUT_SEC
V307_MULTI_CHUNK_LIMIT = V308_MULTI_CHUNK_LIMIT


def _v308_reason_text(items: Iterable[Any]) -> str:
    try:
        return " | ".join(str(x or "") for x in (items or []))
    except Exception:
        return str(items or "")


def _v308_has_reason(items: Iterable[Any], patterns: Iterable[str]) -> bool:
    text = _v308_reason_text(items)
    return any(str(p) in text for p in patterns or [])


_v308_base_regrade = _v303_regrade


def _v308_money_reaccel_s(out: Dict[str, Any], market: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    """돈흐름 재가속 S급 1차 판정.

    새 전략 전체를 크게 붙이는 게 아니라, 공통 재료 위에서 S급만 뽑는 작은 전략 바구니다.
    A/B/C 등급은 만들지 않고, 조건이 안 맞으면 관찰 사유만 남긴다.
    """
    reasons: List[str] = []
    fails: List[str] = []
    hard = [str(x) for x in _v288_as_list(out.get("hard"))]
    wait = [str(x) for x in _v288_as_list(out.get("wait"))]
    all_reasons = hard + wait
    mode = str(out.get("leader_market_mode") or (market or {}).get("leader_market_mode") or "")
    score = fnum(out.get("score") or out.get("leader_score"), 0.0)
    money3 = fnum(out.get("turnover_3m") or out.get("money_flow_3m"), 0.0)
    money5 = fnum(out.get("turnover_5m") or out.get("money_flow_5m"), 0.0)
    ch3 = fnum(out.get("change_3"), 0.0)
    ch5 = fnum(out.get("change_5"), 0.0)
    ch15 = fnum(out.get("change_15"), 0.0)
    ch30 = fnum(out.get("change_30"), 0.0)
    vwap = fnum(out.get("vwap_gap_pct"), 0.0)
    ema5 = fnum(out.get("ema5_gap_pct"), 0.0)
    spread = fnum(out.get("spread_pct"), 999.0)
    buy_ratio = fnum(out.get("buy_ratio"), 0.0)
    micro_fresh = bool(out.get("micro_fresh"))
    ws_fresh = bool(out.get("ws_fresh"))
    true_bad = _v308_has_reason(hard, ["스프레드넓음", "거래정지", "비정상", "대형주는", "스테이블"])
    info_wait = _v308_has_reason(all_reasons, ["WS재확인", "micro긴급수집대기", "정보끝까지부족"])
    sell_wall = _v308_has_reason(all_reasons, ["매도벽", "매도체결압력"])
    weak_buy = _v308_has_reason(all_reasons, ["매수체결약함"])

    if true_bad:
        fails.append("진짜차단사유")
    if score < V308_MONEY_SCORE_MIN:
        fails.append(f"점수부족 {score:.1f}")
    else:
        reasons.append("점수충족")
    if money3 < V308_MONEY_3M_MIN and money5 < V308_MONEY_5M_MIN:
        fails.append(f"돈흐름부족 3분{money3/10000:.0f}만")
    else:
        reasons.append("돈흐름재가속")
    if ch3 < V308_MONEY_CH3_MIN or ch5 < V308_MONEY_CH5_MIN:
        fails.append(f"단기재가속부족 3분{ch3:+.2f}/5분{ch5:+.2f}")
    else:
        reasons.append("3분/5분재가속")
    if ch15 < V308_MONEY_CH15_MIN or ch30 < V308_MONEY_CH30_MIN:
        fails.append(f"15/30분유지부족 {ch15:+.2f}/{ch30:+.2f}")
    else:
        reasons.append("15/30분유지")
    if vwap < V308_MONEY_VWAP_MIN or ema5 < V308_MONEY_EMA5_MIN:
        fails.append(f"VWAP/EMA유지부족 {vwap:+.2f}/{ema5:+.2f}")
    else:
        reasons.append("VWAP/EMA유지")
    if not micro_fresh or not ws_fresh or info_wait:
        fails.append("외부정보재확인")
    else:
        reasons.append("WS/micro신선")
    if spread > V308_MONEY_SPREAD_MAX:
        fails.append(f"스프레드주의 {spread:.2f}%")
    else:
        reasons.append("스프레드양호")
    if buy_ratio < V308_MONEY_BUY_RATIO_MIN or weak_buy:
        fails.append(f"매수체결부족 {buy_ratio:.2f}")
    else:
        reasons.append("매수체결우세")
    if sell_wall:
        fails.append("매도벽/매도체결압력")
    if mode == "약함":
        fails.append("장세약함")

    ok = not fails
    return ok, reasons[:10], fails[:10]


def _v303_regrade(ev: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v308 전략 바구니 S급 전용 판정.

    이전 v306 평가를 먼저 통과시킨 뒤, paper OPEN은 각 전략의 S급만 허용한다.
    leader A/B는 차단하지 않고 C 관찰로 내리고, money_reaccel_s가 S이면 paper OPEN한다.
    """
    out = _v308_base_regrade(ev, market)
    try:
        leader_grade = str(out.get("candidate_grade") or "C").upper()
        leader_label = str(out.get("candidate_grade_label") or "")
        leader_is_s = leader_grade == "S"
        money_ok, money_good, money_fail = _v308_money_reaccel_s(out, market)
        basket_hits: List[str] = []
        basket_labels: List[str] = []
        if leader_is_s:
            basket_hits.append(STRATEGY_KEY)
            basket_labels.append("주도추세 S")
        if money_ok:
            basket_hits.append(V308_MONEY_KEY)
            basket_labels.append("돈흐름재가속 S")

        original_grade = leader_grade
        original_label = leader_label
        wait = [str(x) for x in _v288_as_list(out.get("wait"))]
        hard = [str(x) for x in _v288_as_list(out.get("hard"))]
        good = [str(x) for x in _v288_as_list(out.get("good"))]

        if basket_hits:
            primary_key = basket_hits[0] if len(basket_hits) == 1 else "multi_strategy_s"
            primary_label = "복수전략 S급" if len(basket_hits) >= 2 else basket_labels[0]
            label = "✅ 복수전략 S급 후보" if len(basket_hits) >= 2 else ("✅ S급 돈흐름 재가속 후보" if basket_hits[0] == V308_MONEY_KEY else "✅ S급 주도추세 후보")
            good2 = list(dict.fromkeys(basket_labels + (money_good if money_ok else []) + good))[:16]
            out.update({
                "candidate_grade": "S",
                "candidate_grade_label": label,
                "action": "paper_open",
                "auto_ready": True,
                "auto_ready_label": "✅ 전략바구니 S급 후보",
                "paper_grade_open": True,
                "paper_bot_open": True,
                "open_eligible": True,
                "paper_eligible": True,
                "eligible_for_paper": True,
                "trade_ready": True,
                "review_only": False,
                "observe_only": False,
                "final_entry_action": "paper_open",
                "final_entry_label": label,
                "good": good2,
                "wait": wait,
                "strategy_bucket_hits": basket_hits,
                "strategy_bucket_labels": basket_labels,
                "strategy_bucket_primary": primary_key,
                "strategy_bucket_primary_label": primary_label,
                "money_reaccel_s": bool(money_ok),
                "money_reaccel_good": money_good,
                "money_reaccel_fail": money_fail,
                "leader_original_grade": original_grade,
                "leader_original_label": original_label,
                "v308_s_basket_open": True,
            })
        else:
            # 진짜 차단은 유지. 나머지 A/B/C는 관찰/복기로만 남긴다.
            if leader_grade != "X":
                demote_reason = f"v308_{leader_grade or 'C'}관찰_only_S만OPEN"
                new_wait = list(dict.fromkeys([demote_reason] + wait + money_fail))[:16]
                out.update({
                    "candidate_grade": "C",
                    "candidate_grade_label": "❔ C급 관찰 후보",
                    "action": "observe",
                    "auto_ready": False,
                    "auto_ready_label": "-",
                    "paper_grade_open": False,
                    "paper_bot_open": False,
                    "open_eligible": False,
                    "paper_eligible": False,
                    "eligible_for_paper": False,
                    "trade_ready": False,
                    "review_only": True,
                    "observe_only": True,
                    "final_entry_action": "observe",
                    "final_entry_label": "❔ C급 관찰 후보",
                    "wait": new_wait,
                    "leader_original_grade": original_grade,
                    "leader_original_label": original_label,
                    "strategy_bucket_hits": [],
                    "strategy_bucket_labels": [],
                    "strategy_bucket_primary": "observe_only",
                    "strategy_bucket_primary_label": "S급없음 관찰",
                    "money_reaccel_s": False,
                    "money_reaccel_fail": money_fail,
                    "v308_s_basket_open": False,
                })
            else:
                out.update({
                    "strategy_bucket_hits": [],
                    "strategy_bucket_labels": [],
                    "strategy_bucket_primary": "blocked",
                    "strategy_bucket_primary_label": "제외/차단",
                    "money_reaccel_s": False,
                    "money_reaccel_fail": money_fail,
                    "leader_original_grade": original_grade,
                    "leader_original_label": original_label,
                    "v308_s_basket_open": False,
                })
    except Exception as exc:
        try:
            log_error("v308_regrade", exc)
        except Exception:
            pass
    return out


_v308_base_candidate_row = _v288_candidate_row


def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    item = _v308_base_candidate_row(row, market)
    try:
        ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
        ev = ctx.get("leader_eval") if isinstance(ctx.get("leader_eval"), dict) else {}
        bucket_hits = ev.get("strategy_bucket_hits") if isinstance(ev.get("strategy_bucket_hits"), list) else []
        bucket_labels = ev.get("strategy_bucket_labels") if isinstance(ev.get("strategy_bucket_labels"), list) else []
        primary = str(ev.get("strategy_bucket_primary") or "observe_only")
        primary_label = str(ev.get("strategy_bucket_primary_label") or "-")
        item.update({
            "schema": "strategy_s_basket_v308",
            "strategy_basket_version": "v308_s_only_money_reaccel_v1",
            "strategy_bucket_hits": bucket_hits,
            "strategy_bucket_labels": bucket_labels,
            "strategy_bucket_primary": primary,
            "strategy_bucket_primary_label": primary_label,
            "money_reaccel_s": bool(ev.get("money_reaccel_s")),
            "money_reaccel_good": ev.get("money_reaccel_good", []),
            "money_reaccel_fail": ev.get("money_reaccel_fail", []),
            "leader_original_grade": ev.get("leader_original_grade"),
            "leader_original_label": ev.get("leader_original_label"),
        })
        # paper route는 일단 기존 leader 키를 유지한다. 전략별 S 태그는 별도 필드로 저장해 장부/표시 충돌을 막는다.
        ctx.update({
            "strategy_basket_version": item["strategy_basket_version"],
            "strategy_bucket_hits": bucket_hits,
            "strategy_bucket_labels": bucket_labels,
            "strategy_bucket_primary": primary,
            "strategy_bucket_primary_label": primary_label,
            "money_reaccel_s": bool(ev.get("money_reaccel_s")),
            "money_reaccel_good": ev.get("money_reaccel_good", []),
            "money_reaccel_fail": ev.get("money_reaccel_fail", []),
            "leader_original_grade": ev.get("leader_original_grade"),
            "leader_original_label": ev.get("leader_original_label"),
            "v308_note": "paper_strategy_key는 기존 leader 유지, 전략별 S 태그는 strategy_bucket_*에 저장",
        })
        item["entry_context"] = ctx
        if item.get("paper_bot_open"):
            label = item.get("candidate_grade_label") or "✅ S급 후보"
            tags = ", ".join(str(x) for x in bucket_labels[:3]) if bucket_labels else primary_label
            item["one_liner"] = f"- {_v274_ticker(item) or '?'}: {label} / {tags} / 점수 {fnum(item.get('score'),0):.1f} / 15분 {fnum(item.get('change_15'),0):+.2f}% / 30분 {fnum(item.get('change_30'),0):+.2f}% / 3분돈 {fnum(item.get('turnover_3m'),0)/10000:.0f}만"
    except Exception as exc:
        try:
            log_error("v308_candidate_row", exc)
        except Exception:
            pass
    return item


_v308_base_compact_snapshot_row = _v301_compact_snapshot_row


def _v301_compact_snapshot_row(r: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    out = _v308_base_compact_snapshot_row(r)
    try:
        for k in [
            "strategy_basket_version", "strategy_bucket_hits", "strategy_bucket_labels", "strategy_bucket_primary", "strategy_bucket_primary_label",
            "money_reaccel_s", "money_reaccel_good", "money_reaccel_fail", "leader_original_grade", "leader_original_label",
        ]:
            if k in r:
                out[k] = r.get(k)
        ctx = r.get("entry_context") if isinstance(r.get("entry_context"), dict) else {}
        if isinstance(ctx, dict):
            out.setdefault("entry_context", ctx)
    except Exception:
        pass
    return out


def _v308_bucket_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"leader_s": 0, "money_reaccel_s": 0, "multi_s": 0, "open_s": 0}
    for r in rows or []:
        hits = r.get("strategy_bucket_hits") if isinstance(r.get("strategy_bucket_hits"), list) else []
        if STRATEGY_KEY in hits:
            out["leader_s"] += 1
        if V308_MONEY_KEY in hits:
            out["money_reaccel_s"] += 1
        if len(hits) >= 2:
            out["multi_s"] += 1
        if bool(r.get("paper_bot_open") or r.get("trade_ready") or r.get("open_eligible")):
            out["open_s"] += 1
    return out


_v308_base_score_text = _v288_score_text


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows0 = rows if rows is not None else _v301_rows_from_snapshot(260)
    rows0 = [r for r in rows0 or [] if isinstance(r, dict)]
    bcnt = _v308_bucket_counts(rows0)
    text = _v308_base_score_text(rows0)
    head = "\n".join([
        "[0] v308 전략 바구니 S급 전용",
        "- paper OPEN은 각 전략의 S급만 허용. leader A/B와 money 조건 미달은 C 관찰로 남김.",
        f"- 현재 S후보: 주도추세 S {bcnt['leader_s']} / 돈흐름재가속 S {bcnt['money_reaccel_s']} / 복수전략 S {bcnt['multi_s']} / OPEN대상 {bcnt['open_s']}",
        "- paper_strategy_key는 기존 leader 키를 유지하고, 세부 전략 태그는 strategy_bucket_*에 저장해 장부 혼선을 줄임.",
        "",
    ])
    text = text.replace("[1] v306 변경점", "[1] v308 변경점")
    text = text.replace("- v306: S 조건·청산값은 유지, 매도벽/정보재확인/매수체결약함 B는 C 관찰로 경량 보정", "- v308: 여러 전략의 S급만 paper OPEN. leader S 유지 + money_reaccel_s 추가. A/B는 관찰")
    text = text.replace("- v306: paper 개수 고정 해제 후 S/A/B/C별 승률과 B→C 관찰 보정 효과 확인", "- v308: 전략 바구니 S급만 OPEN하고, C 관찰 후보에서 놓친 코인 복기")
    return head + text


_v308_base_health_text = _v288_health_text


def _v288_health_text() -> str:  # type: ignore[override]
    text = _v308_base_health_text()
    text = text.replace("전략 장세 선택형 주도코인 추세 지속 전략", "전략 전략바구니 S급 전용(leader+money)")
    text = text.replace("공장: leader_momentum_continuation 단계형 S/A/B/C 후보 저장", "공장: strategy_s_basket S급만 paper 후보 저장")
    text = text.replace("paper 검증 전용 / CLOSED 30건 전 조건 변경 금지", "paper 검증 전용 / v308부터 S급 바구니만 OPEN")
    return text


def _v308_process_command_lines(update, context, lines: List[str], source: str = "auto") -> bool:
    """v308 자동묶음.

    v307에서 접수 안내 전송 자체가 20초 이상 걸려 첫 명령을 늦췄다.
    그래서 접수 메시지는 생략하고 바로 1번 명령 결과부터 보낸다.
    """
    lines = [ln for ln in (lines or []) if str(ln).strip().startswith("/")]
    if not lines:
        return False
    total_start = now_ts()
    total = len(lines)
    rows: List[Tuple[str, float, float, str]] = []
    for idx, line in enumerate(lines, start=1):
        build_st = now_ts()
        name = _command_name_from_line(line)
        if name == "batch" and source == "auto":
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
                    body = _v307_compact_for_multi(name, body)
                    if idx == 1:
                        body = "📦 자동 묶음 compact 실행\n- 접수 메시지 전송은 생략하고 바로 결과부터 표시\n- 상세 전문은 각 명령 단독 실행\n\n" + body
                    res = "OK"
                except Exception as exc:
                    log_error(f"multi:{name}", exc)
                    body = f"오류: {exc.__class__.__name__}: {exc}"
                    res = f"ERR {exc.__class__.__name__}"
        build_sec = max(0.0, now_ts() - build_st)
        send_sec = reply(update, f"[{idx}/{total}] /{name} (계산 {build_sec:.2f}s / {res})\n" + body)
        rows.append((name, build_sec, send_sec, res))
    summary = ["🧾 자동 묶음 시간표", "- 접수 전송: 생략(v308)"]
    build_total = 0.0
    send_total = 0.0
    for name, build_sec, send_sec, res in rows:
        build_total += build_sec
        send_total += send_sec
        icon = "✅" if res == "OK" else ("❔" if res in {"SKIP", "UNKNOWN"} else "❌")
        summary.append(f"- {icon} /{name}: 계산 {build_sec:.2f}s / 전송 {send_sec:.2f}s / {res}")
    summary.append(f"- 합계: 계산 {build_total:.2f}s / 전송 {send_total:.2f}s / 전체 {now_ts() - total_start:.2f}s")
    summary.append("- 첫 응답이 늦으면 접수 전송이 아니라 1번 명령 계산/전송 시간을 확인")
    summary.append("- 상세 전문은 각 명령을 단독으로 실행")
    reply(update, "\n".join(summary))
    return True


def handle_multi_command_message(update, context) -> bool:  # type: ignore[override]
    text = ""
    try:
        text = getattr(getattr(update, "message", None), "text", "") or ""
    except Exception:
        text = ""
    lines = _extract_command_lines(text)
    if len(lines) <= 1:
        return False
    return _v308_process_command_lines(update, context, lines, source="auto")


def command_batch(update, context) -> None:  # type: ignore[override]
    lines = ["/health", "/score", "/quality", "/strategy_watch", "/errorlog"]
    _v308_process_command_lines(update, context, lines, source="batch")

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = V308_BASKET_LABEL
    STATE["phase_note"] = V308_NOTE
    STATE["v308_strategy_s_basket_active"] = True
    STATE["v308_multi_ack_skip"] = True
except Exception as exc:
    try:
        log_error("v308_init", exc)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# v2.13.309: 전략 바구니 보정
# - 실제 paper OPEN은 S/A만 허용한다.
# - B/C는 paper OPEN하지 않지만, 전략별 B/C 가상 복기 성과를 쌓을 수 있게 shadow_eval 태그를 보존한다.
# - v308의 접수 전송 생략/compact 자동묶음 구조는 유지한다.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.309"
V309_NOTE = "v309: 전략 바구니 S/A만 paper OPEN. B/C는 OPEN 없이 전략별 shadow_eval 복기 성과 추적용으로 보존."

_v309_base_regrade = _v308_base_regrade  # v306 경량 보정 직후 원본을 사용한다.


def _v309_shadow_eval_fields(out: Dict[str, Any], *, grade: str, original_grade: str, original_label: str,
                             money_fail: List[str], money_ok: bool = False) -> Dict[str, Any]:
    """B/C 관찰 후보도 나중에 승률·수익률 복기할 수 있도록 공통 shadow_eval 필드를 남긴다."""
    eval_grades = {
        STRATEGY_KEY: original_grade or grade,
        V308_MONEY_KEY: "S" if money_ok else "C",
    }
    eval_labels = {
        STRATEGY_KEY: original_label or f"{original_grade or grade}급 주도추세",
        V308_MONEY_KEY: "돈흐름재가속 S" if money_ok else "돈흐름재가속 관찰",
    }
    return {
        "shadow_eval": True,
        "shadow_eval_enabled": True,
        "shadow_eval_grade": grade,
        "shadow_eval_minutes": 60,
        "strategy_eval_grades": eval_grades,
        "strategy_eval_labels": eval_labels,
        "strategy_eval_primary": STRATEGY_KEY,
        "leader_original_grade": original_grade,
        "leader_original_label": original_label,
        "money_reaccel_s": bool(money_ok),
        "money_reaccel_fail": money_fail,
        "paper_grade_open": False,
        "paper_bot_open": False,
        "open_eligible": False,
        "paper_eligible": False,
        "eligible_for_paper": False,
        "trade_ready": False,
        "review_only": True,
        "observe_only": True,
    }


def _v303_regrade(ev: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v309 전략 바구니 S/A OPEN + B/C shadow_eval.

    - S: 주도추세 S 또는 money_reaccel_s S이면 paper OPEN.
    - A: 주도추세 A는 paper OPEN으로 검증한다.
    - B/C: paper OPEN하지 않고 shadow_eval 복기 대상으로 남긴다.
    - X: 진짜 차단으로 유지한다.
    """
    out = _v309_base_regrade(ev, market)
    try:
        leader_grade = str(out.get("candidate_grade") or "C").upper().strip()
        if leader_grade not in {"S", "A", "B", "C", "X"}:
            leader_grade = "C"
        leader_label = str(out.get("candidate_grade_label") or "")
        money_ok, money_good, money_fail = _v308_money_reaccel_s(out, market)
        wait = [str(x) for x in _v288_as_list(out.get("wait"))]
        good = [str(x) for x in _v288_as_list(out.get("good"))]
        original_grade = leader_grade
        original_label = leader_label

        basket_hits: List[str] = []
        basket_labels: List[str] = []
        if leader_grade == "S":
            basket_hits.append(STRATEGY_KEY)
            basket_labels.append("주도추세 S")
        if money_ok:
            basket_hits.append(V308_MONEY_KEY)
            basket_labels.append("돈흐름재가속 S")

        if basket_hits:
            primary_key = basket_hits[0] if len(basket_hits) == 1 else "multi_strategy_s"
            primary_label = "복수전략 S급" if len(basket_hits) >= 2 else basket_labels[0]
            label = "✅ 복수전략 S급 후보" if len(basket_hits) >= 2 else ("✅ S급 돈흐름 재가속 후보" if basket_hits[0] == V308_MONEY_KEY else "✅ S급 주도추세 후보")
            good2 = list(dict.fromkeys(basket_labels + (money_good if money_ok else []) + good))[:16]
            out.update({
                "candidate_grade": "S",
                "candidate_grade_label": label,
                "action": "paper_open",
                "auto_ready": True,
                "auto_ready_label": "✅ 전략바구니 S급 후보",
                "paper_grade_open": True,
                "paper_bot_open": True,
                "open_eligible": True,
                "paper_eligible": True,
                "eligible_for_paper": True,
                "trade_ready": True,
                "review_only": False,
                "observe_only": False,
                "final_entry_action": "paper_open",
                "final_entry_label": label,
                "good": good2,
                "wait": wait,
                "strategy_bucket_hits": basket_hits,
                "strategy_bucket_labels": basket_labels,
                "strategy_bucket_primary": primary_key,
                "strategy_bucket_primary_label": primary_label,
                "strategy_eval_grades": {STRATEGY_KEY: original_grade, V308_MONEY_KEY: "S" if money_ok else "C"},
                "money_reaccel_s": bool(money_ok),
                "money_reaccel_good": money_good,
                "money_reaccel_fail": money_fail,
                "leader_original_grade": original_grade,
                "leader_original_label": original_label,
                "v309_sa_basket_open": True,
                "shadow_eval": False,
            })
            return out

        if leader_grade == "A":
            # 사용자가 확정한 방향: 실제/모의 OPEN은 S/A만. A는 관찰이 아니라 paper 검증 대상이다.
            good2 = list(dict.fromkeys(["주도추세 A"] + good))[:16]
            out.update({
                "candidate_grade": "A",
                "candidate_grade_label": "✅ A급 자동매매 일반 후보",
                "action": "paper_open",
                "auto_ready": True,
                "auto_ready_label": "✅ 전략바구니 A급 후보",
                "paper_grade_open": True,
                "paper_bot_open": True,
                "open_eligible": True,
                "paper_eligible": True,
                "eligible_for_paper": True,
                "trade_ready": True,
                "review_only": False,
                "observe_only": False,
                "final_entry_action": "paper_open",
                "final_entry_label": "✅ A급 자동매매 일반 후보",
                "good": good2,
                "wait": wait,
                "strategy_bucket_hits": [STRATEGY_KEY],
                "strategy_bucket_labels": ["주도추세 A"],
                "strategy_bucket_primary": "leader_a",
                "strategy_bucket_primary_label": "주도추세 A급",
                "strategy_eval_grades": {STRATEGY_KEY: "A", V308_MONEY_KEY: "C"},
                "money_reaccel_s": False,
                "money_reaccel_fail": money_fail,
                "leader_original_grade": original_grade,
                "leader_original_label": original_label,
                "v309_sa_basket_open": True,
                "shadow_eval": False,
            })
            return out

        if leader_grade in {"B", "C"}:
            label = "🟡 B급 관찰/복기 후보" if leader_grade == "B" else "❔ C급 관찰 후보"
            observe_reason = f"v309_{leader_grade}급_shadow_eval_OPEN없음"
            new_wait = list(dict.fromkeys([observe_reason] + wait + money_fail))[:16]
            out.update({
                "candidate_grade": leader_grade,
                "candidate_grade_label": label,
                "action": "observe",
                "auto_ready": False,
                "auto_ready_label": "-",
                "final_entry_action": "observe",
                "final_entry_label": label,
                "wait": new_wait,
                "strategy_bucket_hits": [],
                "strategy_bucket_labels": [],
                "strategy_bucket_primary": "shadow_eval",
                "strategy_bucket_primary_label": f"{leader_grade}급 가상복기",
                "v309_sa_basket_open": False,
                **_v309_shadow_eval_fields(out, grade=leader_grade, original_grade=original_grade, original_label=original_label, money_fail=money_fail, money_ok=False),
            })
            return out

        # X는 진짜 차단. shadow_eval도 하지 않는다.
        out.update({
            "candidate_grade": "X",
            "candidate_grade_label": out.get("candidate_grade_label") or "❌ 제외/차단",
            "paper_grade_open": False,
            "paper_bot_open": False,
            "trade_ready": False,
            "review_only": True,
            "observe_only": True,
            "shadow_eval": False,
            "strategy_bucket_primary": "blocked",
            "strategy_bucket_primary_label": "제외/차단",
            "v309_sa_basket_open": False,
            "money_reaccel_s": False,
            "money_reaccel_fail": money_fail,
            "leader_original_grade": original_grade,
            "leader_original_label": original_label,
        })
    except Exception as exc:
        try:
            log_error("v309_regrade", exc)
        except Exception:
            pass
    return out


# v309 출력 문구 보정
_v309_base_score_text = _v288_score_text

def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows0 = rows or _v301_current_rows()
    text = _v309_base_score_text(rows0)
    try:
        text = text.replace("[0] v308 전략 바구니 S급 전용", "[0] v309 전략 바구니 S/A OPEN + B/C 가상복기")
        text = text.replace("- v308: 여러 전략의 S급만 paper OPEN. leader S 유지 + money_reaccel_s 추가. A/B는 관찰", "- v309: 실제 paper OPEN은 S/A만. B/C는 OPEN 없이 전략별 shadow_eval 복기 성과를 쌓도록 태그 보존")
        text = text.replace("paper 검증은 S/A/B지만 개수 고정은 전략 조건으로 쓰지 않음", "paper 검증 OPEN은 S/A만. B/C는 가상복기 성과만 추적")
        text = text.replace("전략 바구니 S급만 OPEN", "전략 바구니 S/A만 OPEN")
        text = text.replace("v308: 전략 바구니 S급만 OPEN하고, C 관찰 후보에서 놓친 코인 복기", "v309: S/A는 paper OPEN, B/C는 shadow_eval 가상복기로 승률·수익률 확인")
    except Exception as exc:
        log_error("v309_score_text", exc)
    return text

_v309_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v309_base_health_text()
    try:
        text = text.replace("v308부터 S급 바구니만 OPEN", "v309부터 S/A만 OPEN · B/C는 가상복기")
    except Exception as exc:
        log_error("v309_health_text", exc)
    return text

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = "전략 바구니 S/A 검증"
    STATE["phase_note"] = V309_NOTE
    STATE["v309_strategy_sa_basket_active"] = True
except Exception as exc:
    try:
        log_error("v309_init", exc)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# v2.13.310: strategy basket formalization + fast-stop / larger-profit policy
# - 여러 전략을 이름만 붙이지 않고, 전략별 조건 함수로 분리한다.
# - 공통 안전필터는 모든 전략에 강하게 적용한다.
# - 실제 paper OPEN은 S/A만 허용하고, B/C는 shadow_eval 가상복기로 유지한다.
# - 실제 OPEN되는 S/A에는 빠른 손절(-0.45%) + 큰 익절(S +1.80%, A +1.50%)을 적용한다.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.310"
STRATEGY_NAME = "전략 바구니 S/A 검증"
STRATEGY_LABEL = STRATEGY_NAME
V310_NOTE = "v310: 5전략 바구니 정식화. 공통 안전필터 강하게 적용, 전략별 조건 분리, S/A만 OPEN, B/C 가상복기, S/A 빠른손절+큰익절."

V310_STRATEGIES = {
    "leader_momentum_continuation": "주도추세 지속",
    "money_reaccel_s": "돈흐름 재가속",
    "sweep_vwap_recovery_s": "저점 VWAP 회복",
    "breakout_early_s": "거래대금 돌파 초입",
    "mean_reversion_defense_s": "저점방어 평균회귀",
}

# 실제 OPEN되는 S/A 전용 청산 정책. B/C는 shadow_eval 복기용으로만 이 값을 참고한다.
V310_RISK = {
    "S": {"tp": 1.80, "ext": 2.80, "pt": 1.00, "pf": 0.45, "sl": -0.45, "tm": 60},
    "A": {"tp": 1.50, "ext": 2.30, "pt": 0.90, "pf": 0.40, "sl": -0.45, "tm": 50},
    "B": {"tp": 1.30, "ext": 2.00, "pt": 0.80, "pf": 0.35, "sl": -0.45, "tm": 45},
    "C": {"tp": 0.0, "ext": 0.0, "pt": 0.0, "pf": 0.0, "sl": 0.0, "tm": 0},
    "X": {"tp": 0.0, "ext": 0.0, "pt": 0.0, "pf": 0.0, "sl": 0.0, "tm": 0},
}

V310_COMMON_SPREAD_HARD = float(os.getenv("CLEAN_V310_COMMON_SPREAD_HARD", "0.55"))
V310_COMMON_SPREAD_PASS = float(os.getenv("CLEAN_V310_COMMON_SPREAD_PASS", "0.32"))
V310_COMMON_BUY_HARD = float(os.getenv("CLEAN_V310_COMMON_BUY_HARD", "0.44"))
V310_COMMON_BUY_PASS = float(os.getenv("CLEAN_V310_COMMON_BUY_PASS", "0.56"))
V310_MONEY3_MIN = float(os.getenv("CLEAN_V310_MONEY3_MIN", "6000000"))
V310_MONEY5_MIN = float(os.getenv("CLEAN_V310_MONEY5_MIN", "10000000"))

_v310_base_regrade = _v309_base_regrade


def _v310_reasons(row: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    hard = [str(x) for x in _v288_as_list((row or {}).get("hard"))]
    wait = [str(x) for x in _v288_as_list((row or {}).get("wait"))]
    return hard, wait


def _v310_eval_inputs(out: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    hard, wait = _v310_reasons(out)
    text = " | ".join(hard + wait)
    return {
        "mode": str(out.get("leader_market_mode") or (market or {}).get("leader_market_mode") or (market or {}).get("leader_market_label") or "확인"),
        "score": fnum(out.get("score") or out.get("leader_score"), 0.0),
        "ch3": fnum(out.get("change_3"), 0.0),
        "ch5": fnum(out.get("change_5"), 0.0),
        "ch15": fnum(out.get("change_15"), 0.0),
        "ch30": fnum(out.get("change_30"), 0.0),
        "money3": fnum(out.get("turnover_3m") or out.get("money_flow_3m"), 0.0),
        "money5": fnum(out.get("turnover_5m") or out.get("money_flow_5m"), 0.0),
        "vwap": fnum(out.get("vwap_gap_pct"), 0.0),
        "ema5": fnum(out.get("ema5_gap_pct"), 0.0),
        "spread": fnum(out.get("spread_pct"), 999.0),
        "buy": fnum(out.get("buy_ratio"), 0.0),
        "from_low": fnum(out.get("from_low_pct"), 0.0),
        "high_room": fnum(out.get("high_room_pct"), 999.0),
        "rel15": fnum(out.get("leader_rel15_pct"), 0.0),
        "micro": bool(out.get("micro_fresh")),
        "ws": bool(out.get("ws_fresh")),
        "hard": hard,
        "wait": wait,
        "text": text,
        "sell_wall": any(x in text for x in ["매도벽", "매도체결압력"]),
        "weak_buy": any(x in text for x in ["매수체결약함"]),
        "info_wait": any(x in text for x in ["WS재확인", "micro긴급수집대기", "외부정보재확인", "정보끝까지부족"]),
        "true_block": any(x in text for x in ["스프레드넓음", "거래정지", "비정상", "스테이블", "대형주는", "중복OPEN", "반복손실강한차단"]),
    }


def _v310_common_filter(v: Dict[str, Any]) -> Tuple[bool, List[str], List[str]]:
    hard: List[str] = []
    wait: List[str] = []
    if v["true_block"]:
        hard.append("공통차단사유")
    if not v["ws"]:
        wait.append("WS신선도부족")
    if not v["micro"]:
        wait.append("micro신선도부족")
    if v["spread"] >= V310_COMMON_SPREAD_HARD:
        hard.append(f"스프레드과다 {v['spread']:.2f}%")
    elif v["spread"] > V310_COMMON_SPREAD_PASS:
        wait.append(f"스프레드애매 {v['spread']:.2f}%")
    if v["buy"] <= V310_COMMON_BUY_HARD or v["weak_buy"]:
        wait.append(f"매수체결약함 {v['buy']:.2f}")
    elif v["buy"] < V310_COMMON_BUY_PASS:
        wait.append(f"매수체결애매 {v['buy']:.2f}")
    if v["sell_wall"]:
        wait.append("매도벽/매도체결압력")
    if v["money3"] < V310_MONEY3_MIN and v["money5"] < V310_MONEY5_MIN:
        wait.append("거래대금부족")
    return (not hard), hard, wait


def _v310_grade_from_score(score: float, *, s: float, a: float, b: float, hard: List[str], wait: List[str]) -> str:
    if hard:
        return "X"
    # S/A는 공통 대기 사유가 거의 없어야 실제 OPEN 후보로 인정한다.
    if not wait and score >= s:
        return "S"
    if not wait and score >= a:
        return "A"
    if score >= b:
        return "B"
    return "C"


def _v310_strategy_eval(key: str, out: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    v = _v310_eval_inputs(out, market)
    _, common_hard, common_wait = _v310_common_filter(v)
    good: List[str] = []
    wait: List[str] = list(common_wait)
    hard: List[str] = list(common_hard)
    score = 0.0

    if key == "leader_momentum_continuation":
        if v["mode"] in {"강함", "보통"}: score += 1.0; good.append(f"장세{v['mode']}")
        if v["ch15"] >= 1.0: score += 1.3; good.append("15분주도")
        elif v["ch15"] >= 0.45: score += 0.6; wait.append(f"15분주도약함 {v['ch15']:+.2f}%")
        if v["ch30"] >= 1.2: score += 1.2; good.append("30분유지")
        elif v["ch30"] >= 0.45: score += 0.5; wait.append(f"30분유지약함 {v['ch30']:+.2f}%")
        if v["rel15"] >= 0.15 or v["ch15"] >= 1.2: score += 0.8; good.append("시장대비강함")
        if v["money3"] >= 12_000_000 or v["money5"] >= 22_000_000: score += 0.9; good.append("돈흐름충분")
        else: wait.append("주도추세_돈흐름부족")
        if v["vwap"] >= 0 and v["ema5"] >= 0: score += 0.8; good.append("VWAP/EMA유지")
        else: wait.append(f"VWAP/EMA약함 {v['vwap']:+.2f}/{v['ema5']:+.2f}")
        grade = _v310_grade_from_score(score, s=5.2, a=4.4, b=3.2, hard=hard, wait=wait)
    elif key == "money_reaccel_s":
        if v["money3"] >= 10_000_000 or v["money5"] >= 18_000_000: score += 1.2; good.append("돈흐름재가속")
        else: wait.append("재가속_돈흐름부족")
        if v["ch3"] >= 0.05 and v["ch5"] >= 0.12: score += 1.2; good.append("3분/5분재가속")
        else: wait.append(f"단기재가속부족 {v['ch3']:+.2f}/{v['ch5']:+.2f}")
        if v["ch15"] >= 0.50 and v["ch30"] >= 0.30: score += 0.9; good.append("15/30분유지")
        else: wait.append("15/30분유지약함")
        if v["vwap"] >= -0.05 and v["ema5"] >= -0.05: score += 0.8; good.append("VWAP/EMA방어")
        else: wait.append("VWAP/EMA미확인")
        if v["buy"] >= 0.58: score += 0.8; good.append("매수체결우세")
        grade = _v310_grade_from_score(score, s=4.8, a=4.0, b=3.0, hard=hard, wait=wait)
    elif key == "sweep_vwap_recovery_s":
        if 0.15 <= v["from_low"] <= 3.2: score += 1.0; good.append("저점쓸림후회복권")
        else: wait.append("저점회복위치불명")
        if v["vwap"] >= 0 and v["ema5"] >= -0.03: score += 1.2; good.append("VWAP회복")
        else: wait.append("VWAP회복미확인")
        if v["ch3"] >= 0.03 and v["ch5"] >= 0.08: score += 0.9; good.append("회복흐름")
        else: wait.append("회복흐름약함")
        if v["buy"] >= 0.58: score += 0.8; good.append("매수체결회복")
        if v["ch15"] < -1.2 or v["ch30"] < -2.0: wait.append("하락추세잔존")
        grade = _v310_grade_from_score(score, s=4.5, a=3.8, b=2.8, hard=hard, wait=wait)
    elif key == "breakout_early_s":
        if v["money3"] >= 12_000_000 or v["money5"] >= 24_000_000: score += 1.2; good.append("돌파거래대금")
        else: wait.append("돌파거래대금부족")
        if v["ch15"] >= 0.9 and v["ch30"] >= 0.8: score += 1.1; good.append("박스상단돌파흐름")
        else: wait.append("돌파흐름부족")
        if v["ch3"] >= 0.08 and v["ch5"] >= 0.18: score += 0.9; good.append("초입가속")
        else: wait.append("초입가속부족")
        if v["vwap"] >= 0 and v["ema5"] >= 0: score += 0.8; good.append("VWAP위유지")
        if v["high_room"] < 0.35 and v["high_room"] < 900: wait.append("고점바로밑추격위험")
        grade = _v310_grade_from_score(score, s=4.8, a=4.0, b=3.0, hard=hard, wait=wait)
    elif key == "mean_reversion_defense_s":
        if 0.10 <= v["from_low"] <= 2.2: score += 1.1; good.append("저점방어권")
        else: wait.append("저점방어위치불명")
        if v["ch3"] >= 0.03 and v["ch5"] >= -0.05: score += 0.8; good.append("반등초입")
        else: wait.append("반등초입약함")
        if v["vwap"] >= -0.10 or v["ema5"] >= -0.08: score += 0.8; good.append("VWAP/EMA회복시도")
        else: wait.append("회복시도미확인")
        if v["ch15"] < -1.8 or v["ch30"] < -3.0 or v["mode"] == "약함": wait.append("역추세위험")
        if v["money3"] >= 8_000_000 or v["money5"] >= 15_000_000: score += 0.7; good.append("반등돈흐름")
        else: wait.append("반등돈흐름부족")
        grade = _v310_grade_from_score(score, s=4.4, a=3.8, b=2.8, hard=hard, wait=wait)
    else:
        grade = "C"; wait.append("전략키미정")

    label = f"{V310_STRATEGIES.get(key,key)} {grade}"
    return {"key": key, "label": label, "grade": grade, "score": round(score, 3), "good": good[:8], "wait": wait[:8], "hard": hard[:8]}


def _v310_merge_strategy_evals(out: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    evals = {k: _v310_strategy_eval(k, out, market) for k in V310_STRATEGIES.keys()}
    grades = {k: v.get("grade", "C") for k, v in evals.items()}
    labels = {k: v.get("label", k) for k, v in evals.items()}
    open_hits = [k for k, g in grades.items() if g in {"S", "A"}]
    s_hits = [k for k, g in grades.items() if g == "S"]
    a_hits = [k for k, g in grades.items() if g == "A"]
    b_hits = [k for k, g in grades.items() if g == "B"]
    c_hits = [k for k, g in grades.items() if g == "C"]
    x_hits = [k for k, g in grades.items() if g == "X"]
    if s_hits:
        grade = "S"; label = "✅ 복수전략 S급 후보" if len(s_hits) >= 2 else f"✅ S급 {V310_STRATEGIES.get(s_hits[0], s_hits[0])} 후보"
    elif a_hits:
        grade = "A"; label = "✅ 복수전략 A급 후보" if len(a_hits) >= 2 else f"✅ A급 {V310_STRATEGIES.get(a_hits[0], a_hits[0])} 후보"
    elif b_hits:
        grade = "B"; label = "🟡 B급 가상복기 후보"
    elif len(x_hits) == len(evals):
        grade = "X"; label = "❌ 제외/차단"
    else:
        grade = "C"; label = "❔ C급 관찰 후보"
    risk = dict(V310_RISK.get(grade) or V310_RISK["X"])
    open_ok = grade in {"S", "A"}
    hit_labels = [V310_STRATEGIES.get(k, k) + " " + grades.get(k, "") for k in open_hits]
    primary = s_hits[0] if s_hits else (a_hits[0] if a_hits else (b_hits[0] if b_hits else (c_hits[0] if c_hits else "blocked")))
    all_good: List[str] = []
    all_wait: List[str] = []
    all_hard: List[str] = []
    for k in open_hits or b_hits or c_hits:
        ev = evals.get(k) or {}
        all_good.extend([str(x) for x in ev.get("good", [])])
        all_wait.extend([str(x) for x in ev.get("wait", [])])
        all_hard.extend([str(x) for x in ev.get("hard", [])])
    out.update({
        "candidate_grade": grade,
        "candidate_grade_label": label,
        "strategy_basket_version": "v310_5strategy_sa_faststop_bigprofit",
        "strategy_eval_grades": grades,
        "strategy_eval_labels": labels,
        "strategy_eval_details": evals,
        "strategy_bucket_hits": open_hits,
        "strategy_bucket_labels": hit_labels,
        "strategy_bucket_primary": primary,
        "strategy_bucket_primary_label": V310_STRATEGIES.get(primary, primary),
        "action": "paper_open" if open_ok else ("block" if grade == "X" else "observe"),
        "final_entry_action": "paper_open" if open_ok else ("reject" if grade == "X" else "observe"),
        "final_entry_label": label,
        "auto_ready": open_ok,
        "auto_ready_label": "✅ 전략바구니 S/A 후보" if open_ok else "-",
        "paper_grade_open": open_ok,
        "paper_bot_open": open_ok,
        "open_eligible": open_ok,
        "paper_eligible": open_ok,
        "eligible_for_paper": open_ok,
        "trade_ready": open_ok,
        "review_only": not open_ok,
        "observe_only": not open_ok,
        "shadow_eval": not open_ok and grade in {"B", "C"},
        "shadow_eval_enabled": not open_ok and grade in {"B", "C"},
        "shadow_eval_grade": grade if grade in {"B", "C"} else "",
        "take_profit_pct": risk["tp"],
        "extended_target_pct": risk["ext"],
        "protect_trigger_pct": risk["pt"],
        "protect_floor_pct": risk["pf"],
        "stop_loss_pct": risk["sl"],
        "time_exit_min": risk["tm"],
        "grade_risk_key": f"v310_{grade}_faststop_bigprofit",
        "good": list(dict.fromkeys((hit_labels if open_ok else []) + all_good))[:16],
        "wait": list(dict.fromkeys(([] if open_ok else [f"v310_{grade}급_shadow_eval_OPEN없음"]) + all_wait))[:16],
        "hard": list(dict.fromkeys(all_hard))[:12],
    })
    return out


def _v303_regrade(ev: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    try:
        base = _v310_base_regrade(ev, market)
        return _v310_merge_strategy_evals(base, market)
    except Exception as exc:
        try:
            log_error("v310_regrade", exc)
        except Exception:
            pass
        return {"score": 0.0, "action": "block", "candidate_grade": "X", "candidate_grade_label": "❌ v310 판정오류", "hard": ["v310_regrade_error"], "wait": [], "good": []}


_v310_base_candidate_row = _v288_candidate_row

def _v288_candidate_row(row: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    item = _v310_base_candidate_row(row, market)
    try:
        risk = dict(V310_RISK.get(str(item.get("candidate_grade") or "X").upper()) or V310_RISK["X"])
        if str(item.get("candidate_grade") or "").upper() in {"S", "A"}:
            item.update({
                "take_profit_pct": risk["tp"],
                "extended_target_pct": risk["ext"],
                "protect_trigger_pct": risk["pt"],
                "protect_floor_pct": risk["pf"],
                "stop_loss_pct": risk["sl"],
                "time_exit_min": risk["tm"],
            })
            ctx = item.get("entry_context") if isinstance(item.get("entry_context"), dict) else {}
            ctx = dict(ctx)
            ctx.update({
                "take_profit_pct": risk["tp"],
                "extended_target_pct": risk["ext"],
                "protect_trigger_pct": risk["pt"],
                "protect_floor_pct": risk["pf"],
                "stop_loss_pct": risk["sl"],
                "time_exit_min": risk["tm"],
                "strategy_basket_version": "v310_5strategy_sa_faststop_bigprofit",
                "strategy_eval_grades": item.get("strategy_eval_grades", {}),
                "strategy_eval_labels": item.get("strategy_eval_labels", {}),
                "strategy_eval_details": item.get("strategy_eval_details", {}),
            })
            item["entry_context"] = ctx
    except Exception as exc:
        log_error("v310_candidate_risk", exc)
    return item


def _v310_strategy_counts(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, int]]:
    rows = rows or _v301_current_rows()
    out = {k: {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0} for k in V310_STRATEGIES.keys()}
    for r in rows or []:
        gs = r.get("strategy_eval_grades") if isinstance(r.get("strategy_eval_grades"), dict) else {}
        for k in out:
            g = str(gs.get(k) or "C").upper()
            if g not in out[k]:
                g = "C"
            out[k][g] += 1
    return out

_v310_base_score_text = _v288_score_text

def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows0 = rows or _v301_current_rows()
    text = _v310_base_score_text(rows0)
    try:
        counts = _v310_strategy_counts(rows0)
        lines = [
            "",
            "[0] v310 전략 바구니",
            "- 실제 paper OPEN: 전략별 S/A만",
            "- B/C: OPEN 없이 가상복기",
            "- 청산: S/A 빠른손절 -0.45% / S익절 +1.80%(확장 +2.80) / A익절 +1.50%(확장 +2.30)",
            "[전략별 현재 등급]",
        ]
        for k, label in V310_STRATEGIES.items():
            c = counts.get(k, {})
            lines.append(f"- {label}: S {c.get('S',0)} / A {c.get('A',0)} / B {c.get('B',0)} / C {c.get('C',0)} / 제외 {c.get('X',0)}")
        text = text.replace("📊 /score 요약 · 자동묶음 compact", "📊 /score 요약 · 자동묶음 compact\n" + "\n".join(lines))
        text = text.replace("전략: 장세 선택형 주도코인 추세 지속 전략 (leader_momentum_continuation)", "전략: 전략 바구니 S/A 검증 (5전략)")
        text = text.replace("paper 검증 OPEN은 S/A만. B/C는 가상복기 성과만 추적", "paper OPEN은 전략별 S/A만. B/C는 전략별 가상복기 성과만 추적")
    except Exception as exc:
        log_error("v310_score_text", exc)
    return text

_v310_base_quality_text = _v288_quality_text if '_v288_quality_text' in globals() else None
try:
    _v310_base_health_text = _v288_health_text
    def _v288_health_text() -> str:  # type: ignore[override]
        text = _v310_base_health_text()
        try:
            text = text.replace("전략바구니 S급 전용(leader+money)", "전략 바구니 S/A 검증(5전략)")
            text = text.replace("strategy_s_basket S급만 paper 후보 저장", "strategy_basket S/A만 paper 후보 저장 · B/C 가상복기")
            text = text.replace("v309부터 S/A만 OPEN · B/C는 가상복기", "v310부터 5전략 S/A만 OPEN · 빠른손절/큰익절 · B/C 가상복기")
        except Exception as exc:
            log_error("v310_health_text", exc)
        return text
except Exception:
    pass

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V310_NOTE
    STATE["v310_strategy_basket_active"] = True
    STATE["v310_fast_stop_big_profit"] = True
except Exception as exc:
    try:
        log_error("v310_init", exc)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# v2.13.311: factory bottleneck surgery + visible strategy-basket scoreboard
# - v310에서 5전략 바구니가 들어갔지만 /score·/quality·/strategy_watch가 전체 합산 위주라
#   사용자가 전략별 S/A/B를 확인하기 어려웠다. 기본 출력에 전략별 등급표를 먼저 표시한다.
# - factory latest 저장 병목은 B/C shadow_eval row가 너무 많은 필드를 가진 채 저장되는 경로가 원인이다.
#   기본 paper/shadow latest는 compact row만 저장하고, full 직접계산/전체 상세는 기본 경로에서 끊는다.
# - 전략 조건·청산값·자동매수·paper 장부는 변경하지 않는다.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.311"
V311_NOTE = "v311: v310 전략조건 유지. 기본 출력에 5전략별 S/A/B/C/제외 표기, factory latest/shadow 저장 compact 수술, 병목 세분화."
V311_SHADOW_LATEST_MAX = int(os.getenv("CLEAN_V311_SHADOW_LATEST_MAX", "120"))
V311_STRATEGY_LATEST_MAX = int(os.getenv("CLEAN_V311_STRATEGY_LATEST_MAX", "120"))
V311_URGENT_MAX = int(os.getenv("CLEAN_V311_URGENT_MAX", "45"))


def _v311_strategy_grade_from_row(row: Dict[str, Any], key: str) -> str:
    gs = row.get("strategy_eval_grades") if isinstance(row.get("strategy_eval_grades"), dict) else {}
    g = str(gs.get(key) or "C").upper().strip()
    return g if g in {"S", "A", "B", "C", "X"} else "C"


def _v311_strategy_counts(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, int]]:
    rows = rows if isinstance(rows, list) else _v301_current_rows()
    out = {k: {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0} for k in V310_STRATEGIES.keys()}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        for k in out:
            out[k][_v311_strategy_grade_from_row(r, k)] += 1
    return out


def _v311_overall_counts(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, int]:
    rows = rows if isinstance(rows, list) else _v301_current_rows()
    out = {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0}
    for r in rows or []:
        g = str((r or {}).get("candidate_grade") or "C").upper().strip()
        if g not in out:
            g = "C"
        out[g] += 1
    return out


def _v311_strategy_counts_lines(rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    rows = rows if isinstance(rows, list) else _v301_current_rows()
    counts = _v311_strategy_counts(rows)
    lines = ["[전략별 현재 등급 · v311]", "- 실제 paper OPEN: 각 전략 S/A만. B/C는 OPEN 없이 가상복기. 제외는 차단."]
    for k, label in V310_STRATEGIES.items():
        c = counts.get(k, {})
        lines.append(f"- {label}: S {c.get('S',0)} / A {c.get('A',0)} / B {c.get('B',0)} / C {c.get('C',0)} / 제외 {c.get('X',0)}")
    return lines


def _v311_row_line(r: Dict[str, Any]) -> str:
    t = _v274_ticker(r) or str(r.get("ticker") or "?")
    label = str(r.get("candidate_grade_label") or r.get("final_entry_label") or "-")
    primary = str(r.get("strategy_bucket_primary_label") or r.get("strategy_bucket_primary") or "-")
    score = fnum(r.get("score") or r.get("leader_score"), 0.0)
    ch15 = fnum(r.get("change_15"), 0.0)
    ch30 = fnum(r.get("change_30"), 0.0)
    m3 = fnum(r.get("turnover_3m") or r.get("money_flow_3m"), 0.0)
    reasons = _v288_as_list(r.get("wait") or r.get("hard") or r.get("final_entry_reasons") or [])
    rs = ", ".join(str(x) for x in reasons[:3]) if reasons else "-"
    return f"- {t}: {label} / {primary} / 점수 {score:.1f} / 15분 {ch15:+.2f}% / 30분 {ch30:+.2f}% / 3분돈 {m3/10000:.0f}만 / 사유 {rs}"


def _v311_top_rows(rows: List[Dict[str, Any]], grade_set: set[str], limit: int = 4) -> List[str]:
    picked = []
    for r in sorted(rows or [], key=lambda x: (fnum((x or {}).get("score") or (x or {}).get("leader_score"),0), fnum((x or {}).get("turnover_3m"),0)), reverse=True):
        g = str((r or {}).get("candidate_grade") or "C").upper()
        if g in grade_set:
            picked.append(_v311_row_line(r))
        if len(picked) >= limit:
            break
    return picked or ["- 없음"]


_v311_base_compact_snapshot_row = _v301_compact_snapshot_row

def _v301_compact_snapshot_row(r: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
    """v311 compact row: strategy_eval_details 같은 무거운 내부 구조는 기본 latest 저장에서 제외한다."""
    out = _v311_base_compact_snapshot_row(r)
    try:
        # 무거운 entry_context/strategy_eval_details는 S/A 실제 OPEN 후보에만 유지한다.
        g = str((r or {}).get("candidate_grade") or "C").upper()
        keep_ctx = g in {"S", "A"} and bool((r or {}).get("paper_bot_open") or (r or {}).get("open_eligible"))
        if not keep_ctx:
            out.pop("entry_context", None)
        out.pop("strategy_eval_details", None)
        # 전략별 등급표에 필요한 최소 필드는 유지한다.
        for k in ["strategy_eval_grades", "strategy_eval_labels", "strategy_bucket_hits", "strategy_bucket_labels", "strategy_bucket_primary", "strategy_bucket_primary_label", "shadow_eval", "shadow_eval_grade", "grade_risk_key"]:
            if k in r:
                out[k] = r.get(k)
        # 너무 긴 reason 계열은 앞부분만 남긴다.
        for k in ["good", "wait", "hard", "final_entry_reasons", "quality_risk_tags", "block_reasons"]:
            if k in out:
                out[k] = _v288_as_list(out.get(k))[:6]
        out["version"] = BOT_VERSION
        out["brain_version"] = BOT_VERSION
    except Exception:
        pass
    return out


def _v311_rows_signature(rows: List[Dict[str, Any]]) -> str:
    try:
        return str(hash(json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)))
    except Exception:
        return str(now_ts())


def _v311_write_strategy_files_compact(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any]) -> None:
    nowv = now_ts()
    rows = (strict_rows or []) + (shadow_rows or [])
    # 기본 명령어/상태판은 compact latest만 읽는다. B/C 80개 full payload 저장을 끊는다.
    compact_rows = [_v301_compact_snapshot_row(r) for r in rows[:V311_STRATEGY_LATEST_MAX]]
    grade = _v301_grade_counts(compact_rows) if '_v301_grade_counts' in globals() else _v311_overall_counts(compact_rows)
    ready_n = len([r for r in compact_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    payload = {
        "version": BOT_VERSION,
        "schema": "strategy_basket_snapshot_v311_compact",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy": "전략 바구니 S/A 검증",
        "strategy_keys": list(V310_STRATEGIES.keys()),
        "scan_id": str(STATE.get("scan_id") or ""),
        "grade": grade,
        "strategy_grade_counts": _v311_strategy_counts(compact_rows),
        "paper_open": ready_n,
        "rows": compact_rows,
        "note": "v311: 기본 명령어는 compact snapshot만 사용. full 장부/후보 직접계산은 기본 경로에서 차단.",
    }
    sig = _v311_rows_signature(compact_rows)
    if sig != str(STATE.get("v311_strategy_snapshot_sig") or "") or nowv - fnum(STATE.get("v311_strategy_snapshot_ts"), 0.0) > 20:
        save_json(FILES["strategy_material_snapshot"], payload)
        write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
        with _state_lock:
            STATE["v311_strategy_snapshot_sig"] = sig
            STATE["v311_strategy_snapshot_ts"] = nowv
            STATE["v311_strategy_snapshot_written"] = len(compact_rows)
    else:
        with _state_lock:
            STATE["v311_strategy_snapshot_skipped"] = int(STATE.get("v311_strategy_snapshot_skipped", 0) or 0) + 1


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    """v311 factory export surgery.

    v310에서 B/C shadow_eval row가 full entry_context/strategy_eval_details를 가진 채 latest/material에
    반복 저장되어 공장 latest 저장이 8~12초까지 커졌다. 여기서는 paper/shadow/latest/material 모두
    compact row만 저장한다. S/A 실제 OPEN 후보에 필요한 청산값/entry_context 최소값은 유지한다.
    """
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_rows = [_v301_compact_snapshot_row(_v288_refresh_latest_row(r, "strict", scan_id)) for r in (strict or [])]
    shadow_rows = [_v301_compact_snapshot_row(_v288_refresh_latest_row(r, "shadow", scan_id)) for r in (shadow or [])]
    # shadow latest는 관찰용 compact만 충분하다. 전체 full 저장은 끊는다.
    shadow_rows = shadow_rows[:V311_SHADOW_LATEST_MAX]
    urgent_src = strict_rows + [r for r in shadow_rows if str(r.get("candidate_grade") or "").upper() in {"B", "C"}]
    urgent_priority = _v291_urgent_priority_rows(strict_rows, urgent_src, limit=V311_URGENT_MAX) if "_v291_urgent_priority_rows" in globals() else urgent_src[:V311_URGENT_MAX]
    _v288_write_micro_urgent(urgent_priority, reason="v311_compact_strategy_basket_targets")
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        _v311_write_strategy_files_compact(strict_rows, shadow_rows, market)
    except Exception as exc:
        log_error("v311_strategy_summary_write", exc)
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v311_compact_export"] = True
        if write_error:
            STATE["candidate_write_error"] = write_error
    return {
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_final_recheck_wait": recheck_n,
        "urgent_watch_rows": len(urgent_priority),
        "write_error": write_error,
        "v311_compact_export": True,
    }


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows0 = rows if isinstance(rows, list) else _v301_current_rows()
    rows0 = [r for r in rows0 or [] if isinstance(r, dict)]
    overall = _v311_overall_counts(rows0)
    mode_counter = Counter(str(r.get("leader_market_mode") or "-") for r in rows0)
    mode_txt = ", ".join(f"{k} {v}" for k, v in mode_counter.most_common(3)) if mode_counter else "-"
    closed_lines = []
    try:
        closed_lines = _v304_grade_stats_lines() if '_v304_grade_stats_lines' in globals() else []
    except Exception as exc:
        log_error("v311_closed_stats_line", exc)
    lines = [
        "📊 /score 요약 · v311 전략별 S/A/B 표시",
        "전략: 전략 바구니 S/A 검증 (5전략)",
        "- 실제 paper OPEN: 전략별 S/A만. B/C는 가상복기. 제외는 차단.",
        f"- 현재 합산: S {overall.get('S',0)} / A {overall.get('A',0)} / B {overall.get('B',0)} / C {overall.get('C',0)} / 제외 {overall.get('X',0)} / 장세 {mode_txt}",
        *_v311_strategy_counts_lines(rows0),
        "",
        "[현재 실제 OPEN 후보 S/A TOP]",
        *_v311_top_rows(rows0, {"S", "A"}, 5),
        "",
        "[현재 가상복기 B TOP]",
        *_v311_top_rows(rows0, {"B"}, 3),
    ]
    if closed_lines:
        lines += ["", "[기존 CLOSED 참고 · 과거 B 손실 포함]"] + closed_lines[:12]
    lines += ["", "- 상세 전체표는 /score 단독 실행 또는 /score_full에서 확인"]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(220 if full else 90)
    rows = [r for r in rows or [] if isinstance(r, dict)]
    overall = _v311_overall_counts(rows)
    lines = [
        "🔍 /quality 요약 · v311 전략별 후보품질",
        f"- 합산: S {overall.get('S',0)} / A {overall.get('A',0)} / B {overall.get('B',0)} / C {overall.get('C',0)} / 제외 {overall.get('X',0)}",
        *_v311_strategy_counts_lines(rows),
        "",
        "[S/A 실제 OPEN 후보]",
        *_v311_top_rows(rows, {"S", "A"}, 6),
        "",
        "[B/C 가상복기 TOP]",
        *_v311_top_rows(rows, {"B", "C"}, 6 if full else 4),
        "",
        "- B/C는 OPEN 없이 가상복기. 상세 후보 전체는 /quality_full 단독 실행",
    ]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(220 if full else 90)
    rows = [r for r in rows or [] if isinstance(r, dict)]
    lines = [
        "👀 /strategy_watch 요약 · v311 5전략 감시",
        "- S/A=실제 paper OPEN, B/C=가상복기, X=차단",
        *_v311_strategy_counts_lines(rows),
        "",
        "[전략별 S/A 후보]",
    ]
    for key, label in V310_STRATEGIES.items():
        sub = [r for r in rows if _v311_strategy_grade_from_row(r, key) in {"S", "A"}]
        lines.append(f"[{label} S/A]")
        lines.extend(_v311_top_rows(sub, {"S", "A", "B", "C", "X"}, 3))
    lines += ["", "[가상복기 B/C 대표]", *_v311_top_rows(rows, {"B", "C"}, 5), "- 상세 감시는 /strategy_watch_full 단독 실행"]
    return "\n".join(lines)


_v311_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v311_base_health_text()
    try:
        text = text.replace("전략 바구니 S/A 검증", "전략 바구니 S/A 검증 · v311 compact factory")
        text = text.replace("strategy_basket S/A만 paper 후보 저장 · B/C 가상복기", "strategy_basket compact 저장 · S/A paper · B/C 가상복기")
        if "v311" not in text:
            text += "\n- v311: factory latest/shadow/material compact 저장, 전략별 S/A/B 출력 강화"
    except Exception as exc:
        log_error("v311_health_text", exc)
    return text

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = "전략 바구니 S/A 검증"
    STATE["phase_note"] = V311_NOTE
    STATE["v311_compact_export"] = True
    STATE["v311_strategy_scoreboard"] = True
except Exception as exc:
    try:
        log_error("v311_init", exc)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# v2.13.312: strategy display + external target pipeline surgery
# - v311에서 속도 때문에 compact 저장을 하면서 strategy_eval_grades가 latest/material에 빠져
#   전략별 S/A/B/C 표가 전부 C처럼 보이던 출력 불일치를 수술한다.
# - B/C는 실제 OPEN하지 않지만, S 판단 재료를 채우기 위해 상위 B/C·정보부족 후보는
#   WS/micro target queue에 다시 올린다. 무거운 row 저장은 유지하지 않고 target용 compact만 쓴다.
# - 조건/청산/자동매수는 변경하지 않는다.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.312"
STRATEGY_NAME = "전략 바구니 S/A 검증"
STRATEGY_LABEL = STRATEGY_NAME
V312_NOTE = "v312: 전략별 S/A/B/C 출력 불일치 수정 + B/C 가상복기 상위후보 WS/micro target queue 복구. 조건/청산 변경 없음."
V312_TARGET_MAX = int(os.getenv("CLEAN_V312_TARGET_MAX", "90"))
V312_TARGET_SCORE_MIN = float(os.getenv("CLEAN_V312_TARGET_SCORE_MIN", "5.5"))
V312_TARGET_B_MIN = int(os.getenv("CLEAN_V312_TARGET_B_MIN", "25"))
V312_TARGET_C_MIN = int(os.getenv("CLEAN_V312_TARGET_C_MIN", "25"))


def _v312_compact_snapshot_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """v312 compact row keeps strategy grade fields required by dashboards/target queue.

    v311 accidentally used the old v301 compact row in some write paths, so
    strategy_eval_grades disappeared from latest/material rows. That made the
    aggregate grade say B/C while per-strategy tables showed all C. This is the
    single compact row schema used by default outputs from v312.
    """
    try:
        out = _v311_compact_snapshot_row(r) if '_v311_compact_snapshot_row' in globals() else dict(r or {})
    except Exception:
        out = dict(r or {})
    try:
        for k in [
            "strategy_eval_grades", "strategy_eval_labels", "strategy_eval_details",
            "strategy_bucket_hits", "strategy_bucket_labels", "strategy_bucket_primary",
            "strategy_bucket_primary_label", "strategy_basket_version",
            "shadow_eval", "shadow_eval_enabled", "shadow_eval_grade",
            "paper_grade_open", "paper_bot_open", "open_eligible", "paper_eligible",
            "eligible_for_paper", "trade_ready", "candidate_grade", "candidate_grade_label",
        ]:
            if k in (r or {}):
                val = (r or {}).get(k)
                if k == "strategy_eval_details" and isinstance(val, dict):
                    # Keep only small per-strategy grade/score/reasons. Full nested rows are not stored.
                    slim = {}
                    for sk, ev in val.items():
                        if isinstance(ev, dict):
                            slim[str(sk)] = {
                                "grade": ev.get("grade"),
                                "score": ev.get("score"),
                                "good": _v288_as_list(ev.get("good"))[:4],
                                "wait": _v288_as_list(ev.get("wait"))[:4],
                                "hard": _v288_as_list(ev.get("hard"))[:4],
                            }
                    out[k] = slim
                else:
                    out[k] = val
        # Keep external information fields so B/C reason display and target priority can work.
        for k in [
            "ws_fresh", "ws_row_status", "ws_age_sec", "micro_fresh", "micro_row_status", "micro_age_sec",
            "micro_spread_pct", "micro_trade_buy_ratio_30", "micro_ask_wall_pressure", "micro_sell_trade_pressure",
            "spread_pct", "buy_ratio", "turnover_3m", "turnover_5m", "turnover_24h", "score",
            "change_3", "change_5", "change_15", "change_30", "vwap_gap_pct", "ema5_gap_pct",
        ]:
            if k in (r or {}) and k not in out:
                out[k] = (r or {}).get(k)
        out["version"] = BOT_VERSION
        out["brain_version"] = BOT_VERSION
    except Exception as exc:
        try:
            log_error("v312_compact_row", exc)
        except Exception:
            pass
    return out


def _v312_row_needs_external(row: Dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    t = _v274_ticker(row)
    if not t or t in STABLE_EXCLUDED or t in MAJOR_WATCH_TICKERS:
        return False
    txt = " | ".join([str(x) for x in _v288_as_list(row.get("wait")) + _v288_as_list(row.get("hard")) + _v288_as_list(row.get("quality_risk_tags"))])
    if any(s in txt for s in ["WS신선도부족", "micro신선도부족", "WS재확인", "micro긴급수집대기", "외부정보재확인"]):
        return True
    if not bool(row.get("ws_fresh")) or not bool(row.get("micro_fresh")):
        return True
    return False


def _v312_target_priority_rows(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], limit: int = V312_TARGET_MAX) -> List[Dict[str, Any]]:
    """Build a lightweight WS/micro target queue.

    S/A always stay in targets. High-scoring B/C or information-missing B/C also
    stay in targets for virtual review. This does not open trades; it only keeps
    the information pipeline alive so S/A decisions are not starved.
    """
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    rows = [r for r in list(strict_rows or []) + list(shadow_rows or []) if isinstance(r, dict)]
    def rank(r: Dict[str, Any]) -> Tuple[int, float, float, float]:
        g = str(r.get("candidate_grade") or "C").upper()
        base = {"S": 5, "A": 4, "B": 3, "C": 2, "X": 0}.get(g, 1)
        if _v312_row_needs_external(r):
            base += 1
        return (base, fnum(r.get("score"), 0.0), fnum(r.get("turnover_3m"), 0.0), fnum(r.get("turnover_24h"), 0.0))
    # Ensure minimum B/C review targets when present, but do not exceed limit.
    ordered = sorted(rows, key=rank, reverse=True)
    for r in ordered:
        t = _v274_ticker(r)
        if not t or t in seen or t in STABLE_EXCLUDED:
            continue
        g = str(r.get("candidate_grade") or "C").upper()
        if g in {"S", "A"} or _v312_row_needs_external(r) or (g in {"B", "C"} and fnum(r.get("score"), 0.0) >= V312_TARGET_SCORE_MIN):
            rr = _v312_compact_snapshot_row(r)
            rr["v312_target_reason"] = "S/A" if g in {"S", "A"} else ("B/C정보보강" if _v312_row_needs_external(r) else "B/C상위복기")
            out.append(rr)
            seen.add(t)
            if len(out) >= limit:
                break
    return out


def _v312_write_review_targets(priority_rows: List[Dict[str, Any]], reason: str = "v312_strategy_basket_review_targets") -> None:
    tickers = []
    meta: Dict[str, Any] = {}
    for r in priority_rows or []:
        t = _v274_ticker(r)
        if not t or t in tickers or t in STABLE_EXCLUDED:
            continue
        tickers.append(t)
        meta[t] = {
            "source": "v312_strategy_basket_review",
            "grade": str(r.get("candidate_grade") or ""),
            "score": fnum(r.get("score"), 0.0),
            "reason": str(r.get("v312_target_reason") or ""),
            "ws": str(r.get("ws_row_status") or ""),
            "micro": str(r.get("micro_row_status") or ""),
            "primary": str(r.get("strategy_bucket_primary_label") or ""),
        }
    try:
        payload = {
            "version": BOT_VERSION,
            "schema": "v312_strategy_basket_external_targets",
            "updated_ts": now_ts(),
            "updated_text": now_text(),
            "ttl_sec": 45,
            "reason": reason,
            "targets": tickers,
            "target_count": len(tickers),
            "target_meta": meta,
            "note": "v312: B/C는 OPEN하지 않지만 S/A 판정 재료를 채우기 위해 상위·정보부족 B/C를 WS/micro target queue에 유지",
        }
        # micro urgent file is consumed by the micro sidecar. WS/micro target files are updated by update_* below.
        save_json(FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"), payload)
        _v296_write_target_file_if_changed(FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"), tickers, reason, priority_rows=priority_rows)
        _v296_write_target_file_if_changed(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), tickers, reason, priority_rows=priority_rows)
        with _state_lock:
            STATE["v312_review_targets"] = len(tickers)
            STATE["v312_review_targets_ts"] = now_ts()
            STATE["ws_target_file_targets"] = len(tickers)
            STATE["micro_target_file_targets"] = len(tickers)
            STATE["ws_target_reason"] = reason
            STATE["micro_target_reason"] = reason
    except Exception as exc:
        log_error("v312_write_review_targets", exc)


def _v311_write_strategy_files_compact(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any]) -> None:  # type: ignore[override]
    nowv = now_ts()
    rows = list(strict_rows or []) + list(shadow_rows or [])
    compact_rows = [_v312_compact_snapshot_row(r) for r in rows[:V311_STRATEGY_LATEST_MAX]]
    grade = _v311_overall_counts(compact_rows)
    ready_n = len([r for r in compact_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    payload = {
        "version": BOT_VERSION,
        "schema": "strategy_basket_snapshot_v312_compact_targets",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy": "전략 바구니 S/A 검증",
        "strategy_keys": list(V310_STRATEGIES.keys()),
        "scan_id": str(STATE.get("scan_id") or ""),
        "grade": grade,
        "strategy_grade_counts": _v311_strategy_counts(compact_rows),
        "paper_open": ready_n,
        "review_targets": int(STATE.get("v312_review_targets", 0) or 0),
        "rows": compact_rows,
        "note": "v312: strategy_eval_grades 보존 + B/C 정보보강 target queue 복구. 기본 명령은 compact snapshot만 사용.",
    }
    sig = _v311_rows_signature(compact_rows)
    if sig != str(STATE.get("v312_strategy_snapshot_sig") or "") or nowv - fnum(STATE.get("v312_strategy_snapshot_ts"), 0.0) > 20:
        save_json(FILES["strategy_material_snapshot"], payload)
        write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
        with _state_lock:
            STATE["v312_strategy_snapshot_sig"] = sig
            STATE["v312_strategy_snapshot_ts"] = nowv
            STATE["v312_strategy_snapshot_written"] = len(compact_rows)
    else:
        with _state_lock:
            STATE["v312_strategy_snapshot_skipped"] = int(STATE.get("v312_strategy_snapshot_skipped", 0) or 0) + 1


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_full = [_v288_refresh_latest_row(r, "strict", scan_id) for r in (strict or [])]
    shadow_full = [_v288_refresh_latest_row(r, "shadow", scan_id) for r in (shadow or [])]
    priority_rows = _v312_target_priority_rows(strict_full, shadow_full, limit=V312_TARGET_MAX)
    _v312_write_review_targets(priority_rows, reason="v312_factory_review_targets")
    strict_rows = [_v312_compact_snapshot_row(r) for r in strict_full]
    shadow_rows = [_v312_compact_snapshot_row(r) for r in shadow_full[:V311_SHADOW_LATEST_MAX]]
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        _v311_write_strategy_files_compact(strict_rows, shadow_rows, market)
    except Exception as exc:
        log_error("v312_strategy_summary_write", exc)
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v312_compact_export"] = True
        STATE["v312_review_target_rows"] = len(priority_rows)
        if write_error:
            STATE["candidate_write_error"] = write_error
    return {
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_final_recheck_wait": recheck_n,
        "urgent_watch_rows": len(priority_rows),
        "write_error": write_error,
        "v312_compact_export": True,
    }


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    strict_rows = [r for r in (strict or []) if isinstance(r, dict)]
    shadow_rows = [r for r in (shadow or []) if isinstance(r, dict)]
    if not strict_rows and not shadow_rows:
        strict_rows, shadow_rows = _v212_rows_from_latest()
    priority_rows = _v312_target_priority_rows(strict_rows, shadow_rows, limit=V312_TARGET_MAX)
    try:
        update_ws_targets(market_rows or [], priority_rows=priority_rows, reason="factory_final_candidates_v312")
        update_micro_targets(market_rows or [], priority_rows=priority_rows, reason="factory_final_candidates_v312")
        _v312_write_review_targets(priority_rows, reason="v312_publish_review_targets")
    except Exception as exc:
        log_error("v312_publish_targets", exc)
    wait_sec = max(0.0, min(float(os.getenv("CLEAN_V312_SNAPSHOT_WAIT_SEC", "0.1")), 0.5))
    if wait_sec > 0:
        _stop_event.wait(wait_sec)
    # Snapshot uses priority rows for external coverage and shadow rows for observation, both compact.
    pr_compact = [_v312_compact_snapshot_row(r) for r in priority_rows]
    sh_compact = [_v312_compact_snapshot_row(r) for r in shadow_rows[:V311_SHADOW_LATEST_MAX]]
    final = _v212_write_candidate_snapshot(pr_compact, sh_compact, stage="final_after_target_overlay_v312", source="v312_review_target_rows", wait_sec=wait_sec)
    scan_id = str(final.get("scan_id") or STATE.get("scan_id") or "") if isinstance(final, dict) else str(STATE.get("scan_id") or "")
    try:
        _v219_schedule_external_refresh(scan_id)
    except Exception:
        pass
    ext = final.get("external", {}) if isinstance(final, dict) else {}
    with _state_lock:
        STATE["v312_snapshot_priority_rows"] = len(priority_rows)
        STATE["v312_snapshot_shadow_rows"] = len(sh_compact)
        STATE["v233_snapshot_memory_rows"] = True
        STATE["v233_snapshot_initial_wait_sec"] = wait_sec
    return (
        f"snapshot {final.get('candidate_count',0)} / WS {ext.get('ws_fresh',0)}/{ext.get('total',0)} / "
        f"micro {ext.get('micro_fresh',0)}/{ext.get('total',0)} / targets WS {STATE.get('ws_target_file_targets',0)} / "
        f"micro {STATE.get('micro_target_file_targets',0)} / review_targets {len(priority_rows)} / wait {wait_sec:.1f}s / v312"
    )


# Health and dashboard small wording patch
_v312_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v312_base_health_text()
    try:
        text = text.replace("전략 바구니 S/A 검증 · v311 compact factory", "전략 바구니 S/A 검증 · v312 정보배관복구")
        text = text.replace("strategy_basket compact 저장 · S/A paper · B/C 가상복기", "strategy_basket compact 저장 · S/A paper · B/C 정보보강 target")
        if "v312" not in text:
            text += "\n- v312: 전략별 S/A/B 표기 정상화 + B/C 가상복기 WS/micro target queue 복구"
    except Exception as exc:
        log_error("v312_health_text", exc)
    return text

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = "전략 바구니 S/A 검증"
    STATE["phase_note"] = V312_NOTE
    STATE["v312_external_target_pipeline"] = True
    STATE["v312_strategy_scoreboard_fix"] = True
except Exception as exc:
    try:
        log_error("v312_init", exc)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# v2.13.313: strategy-info preserving pipeline surgery
# - 진단표만 세분화하지 않는다. 후보별/전략별 정보 요구량 자체를 나눠 병목을 줄인다.
# - S/A 가능권은 WS/micro/정밀정보를 우선 채우고, 상위 B/C는 복기용 핵심정보만 채운다.
# - 낮은 B/C는 전략별 등급/사유를 보존하되 무거운 target/snapshot 저장에서 제외한다.
# - 전략 정보는 삭제하지 않는다. strategy_eval_grades/labels는 기본 출력의 단일 원본으로 보존한다.
# - 조건/청산/자동매수/paper 장부는 변경하지 않는다.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.313"
V313_NOTE = "v313: 전략정보 보존형 배관수술. S/A 풀정보, 상위 B/C 제한정보, 낮은 B/C 경량복기. target/snapshot 중복출하 차단. 조건/청산 변경 없음."

V313_TARGET_MAX_TOTAL = int(os.getenv("CLEAN_V313_TARGET_MAX_TOTAL", "34"))
V313_TARGET_MAX_B = int(os.getenv("CLEAN_V313_TARGET_MAX_B", "16"))
V313_TARGET_MAX_C = int(os.getenv("CLEAN_V313_TARGET_MAX_C", "10"))
V313_TARGET_SCORE_B = float(os.getenv("CLEAN_V313_TARGET_SCORE_B", "7.5"))
V313_TARGET_SCORE_C = float(os.getenv("CLEAN_V313_TARGET_SCORE_C", "8.5"))
V313_MATERIAL_MAX_ROWS = int(os.getenv("CLEAN_V313_MATERIAL_MAX_ROWS", "90"))
V313_LOW_BC_REASON_LIMIT = int(os.getenv("CLEAN_V313_LOW_BC_REASON_LIMIT", "4"))


def _v313_market_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        mc = row.get("market_context") if isinstance(row.get("market_context"), dict) else None
        if mc:
            return mc
        return _v274_market_context([row]) if '_v274_market_context' in globals() else {}
    except Exception:
        return {}


def _v313_recompute_strategy_fields(row: Dict[str, Any]) -> Dict[str, Any]:
    """전략별 등급 단일 원본 복구.

    v311/v312 compact 경로 일부에서 strategy_eval_grades가 사라지거나,
    candidate_grade 합산은 B/X인데 전략별 표는 전부 C로 보이는 문제가 있었다.
    여기서는 기존 v310 전략조건을 그대로 사용해 누락된 전략별 등급만 복구한다.
    조건을 새로 조정하지 않는다.
    """
    out = dict(row or {})
    try:
        current = out.get("strategy_eval_grades") if isinstance(out.get("strategy_eval_grades"), dict) else {}
        # 이미 5전략 등급이 모두 있고, 전체가 C인데 후보합산이 B/X가 아닌 경우는 그대로 둔다.
        expected = list(V310_STRATEGIES.keys()) if 'V310_STRATEGIES' in globals() else []
        cg = str(out.get("candidate_grade") or "C").upper().strip()
        all_have = expected and all(k in current for k in expected)
        all_c = all(str(current.get(k) or "C").upper() == "C" for k in expected) if expected else False
        needs = (not all_have) or (cg in {"S", "A", "B", "X"} and all_c)
        if not needs:
            return out
        market = _v313_market_for_row(out)
        evals = {k: _v310_strategy_eval(k, out, market) for k in V310_STRATEGIES.keys()}
        grades = {k: str(v.get("grade") or "C").upper() for k, v in evals.items()}
        labels = {k: str(v.get("label") or k) for k, v in evals.items()}
        out["strategy_eval_grades"] = grades
        out["strategy_eval_labels"] = labels
        # 무거운 details는 기본 row에는 저장하지 않는다. 필요시 full/debug에서 재계산 가능.
        out.pop("strategy_eval_details", None)
        s_hits = [k for k, g in grades.items() if g == "S"]
        a_hits = [k for k, g in grades.items() if g == "A"]
        b_hits = [k for k, g in grades.items() if g == "B"]
        c_hits = [k for k, g in grades.items() if g == "C"]
        hits = s_hits or a_hits or b_hits or c_hits
        if hits:
            out["strategy_bucket_primary"] = hits[0]
            out["strategy_bucket_primary_label"] = V310_STRATEGIES.get(hits[0], hits[0])
        out["v313_strategy_fields_recovered"] = True
    except Exception as exc:
        try:
            log_error("v313_recompute_strategy_fields", exc)
        except Exception:
            pass
    return out


def _v313_grade(row: Dict[str, Any]) -> str:
    g = str((row or {}).get("candidate_grade") or "C").upper().strip()
    return g if g in {"S", "A", "B", "C", "X"} else "C"


def _v313_reason_list(row: Dict[str, Any], limit: int = 6) -> List[str]:
    vals: List[str] = []
    for k in ("wait", "hard", "final_entry_reasons", "observe_reasons", "quality_risk_tags", "block_reasons"):
        vals.extend([str(x) for x in _v288_as_list((row or {}).get(k))])
    return list(dict.fromkeys([x for x in vals if x and x != "-"]))[:limit]


def _v313_needs_external(row: Dict[str, Any]) -> bool:
    text = " | ".join(_v313_reason_list(row, 12))
    if not bool((row or {}).get("ws_fresh")):
        return True
    if not bool((row or {}).get("micro_fresh")):
        return True
    return any(s in text for s in ["WS신선도부족", "micro신선도부족", "외부정보재확인", "micro긴급수집대기", "정보부족", "미확인"])


def _v313_strategy_grade_from_row(row: Dict[str, Any], key: str) -> str:
    rr = _v313_recompute_strategy_fields(row)
    gs = rr.get("strategy_eval_grades") if isinstance(rr.get("strategy_eval_grades"), dict) else {}
    g = str(gs.get(key) or "C").upper().strip()
    return g if g in {"S", "A", "B", "C", "X"} else "C"


def _v311_strategy_grade_from_row(row: Dict[str, Any], key: str) -> str:  # type: ignore[override]
    return _v313_strategy_grade_from_row(row, key)


def _v311_strategy_counts(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, int]]:  # type: ignore[override]
    rows = rows if isinstance(rows, list) else _v301_current_rows()
    out = {k: {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0} for k in V310_STRATEGIES.keys()}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rr = _v313_recompute_strategy_fields(r)
        for k in out:
            out[k][_v313_strategy_grade_from_row(rr, k)] += 1
    return out


def _v311_strategy_counts_lines(rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:  # type: ignore[override]
    rows = rows if isinstance(rows, list) else _v301_current_rows()
    counts = _v311_strategy_counts(rows)
    lines = ["[전략별 현재 등급 · v313 단일원본]", "- 실제 paper OPEN: 각 전략 S/A만. B/C는 OPEN 없이 가상복기. 제외는 차단."]
    for k, label in V310_STRATEGIES.items():
        c = counts.get(k, {})
        lines.append(f"- {label}: S {c.get('S',0)} / A {c.get('A',0)} / B {c.get('B',0)} / C {c.get('C',0)} / 제외 {c.get('X',0)}")
    return lines


def _v313_row_info_tier(row: Dict[str, Any]) -> str:
    g = _v313_grade(row)
    score = fnum((row or {}).get("score") or (row or {}).get("leader_score"), 0.0)
    if g in {"S", "A"}:
        return "P1_SA_FULL"
    if g == "B" and (_v313_needs_external(row) or score >= V313_TARGET_SCORE_B):
        return "P2_B_LIMITED"
    if g == "C" and (_v313_needs_external(row) or score >= V313_TARGET_SCORE_C):
        return "P3_C_LIGHT"
    return "P4_LIGHT_ONLY"


def _v313_compact_snapshot_row(r: Dict[str, Any], *, tier: Optional[str] = None) -> Dict[str, Any]:
    r = _v313_recompute_strategy_fields(r)
    tier = tier or _v313_row_info_tier(r)
    # v312 compact row is still a good base, but we remove heavy detail again by tier.
    try:
        out = _v312_compact_snapshot_row(r) if '_v312_compact_snapshot_row' in globals() else dict(r or {})
    except Exception:
        out = dict(r or {})
    g = _v313_grade(r)
    out["v313_info_tier"] = tier
    out["version"] = BOT_VERSION
    out["brain_version"] = BOT_VERSION
    out["strategy_basket_version"] = "v313_tiered_info_pipeline"
    # Always preserve strategy information. This is analysis material, not garbage.
    for k in ["strategy_eval_grades", "strategy_eval_labels", "strategy_bucket_hits", "strategy_bucket_labels", "strategy_bucket_primary", "strategy_bucket_primary_label", "shadow_eval", "shadow_eval_grade", "grade_risk_key"]:
        if k in r:
            out[k] = r.get(k)
    # Heavy internals are only kept for real S/A OPEN candidates; B/C keeps compact evidence only.
    if not (g in {"S", "A"} and bool(r.get("paper_bot_open") or r.get("open_eligible"))):
        out.pop("entry_context", None)
        out.pop("strategy_eval_details", None)
    # Trim reason fields by tier, but do not delete strategy grades.
    reason_limit = 8 if tier in {"P1_SA_FULL", "P2_B_LIMITED"} else V313_LOW_BC_REASON_LIMIT
    for k in ["good", "wait", "hard", "final_entry_reasons", "quality_risk_tags", "block_reasons", "observe_reasons"]:
        if k in out:
            out[k] = _v288_as_list(out.get(k))[:reason_limit]
    # For low-tier rows, keep only fields needed for dashboards/review.
    if tier == "P4_LIGHT_ONLY":
        keep = {
            "ticker", "symbol", "market", "version", "brain_version", "strategy", "strategy_name", "strategy_label", "strategy_key", "paper_strategy_key",
            "candidate_grade", "candidate_grade_label", "score", "leader_score", "change_3", "change_5", "change_15", "change_30", "turnover_3m", "turnover_5m",
            "vwap_gap_pct", "ema5_gap_pct", "ws_fresh", "micro_fresh", "ws_row_status", "micro_row_status", "spread_pct", "buy_ratio",
            "strategy_eval_grades", "strategy_eval_labels", "strategy_bucket_primary", "strategy_bucket_primary_label", "shadow_eval", "shadow_eval_grade", "v313_info_tier",
            "good", "wait", "hard", "final_entry_reasons", "quality_risk_tags", "block_reasons", "leader_market_mode", "market_context",
        }
        out = {k: v for k, v in out.items() if k in keep}
    return out


def _v313_row_priority(row: Dict[str, Any]) -> Tuple[int, float, float, float]:
    tier = _v313_row_info_tier(row)
    tier_rank = {"P1_SA_FULL": 4, "P2_B_LIMITED": 3, "P3_C_LIGHT": 2, "P4_LIGHT_ONLY": 1}.get(tier, 0)
    return (tier_rank, fnum(row.get("score") or row.get("leader_score"), 0.0), fnum(row.get("turnover_3m"), 0.0), fnum(row.get("change_15"), 0.0))


def _v313_target_priority_rows(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], limit: int = V313_TARGET_MAX_TOTAL) -> List[Dict[str, Any]]:
    """정보 요구량별 target queue.

    전체 후보를 target으로 던지지 않는다. S/A 가능권은 풀정보, 상위 B/C는 제한정보,
    낮은 B/C는 복기 요약만 남기고 target에서 제외한다.
    """
    candidates = [_v313_recompute_strategy_fields(r) for r in list(strict_rows or []) + list(shadow_rows or []) if isinstance(r, dict)]
    buckets = {"P1_SA_FULL": [], "P2_B_LIMITED": [], "P3_C_LIGHT": []}
    for r in candidates:
        tier = _v313_row_info_tier(r)
        if tier in buckets:
            rr = _v313_compact_snapshot_row(r, tier=tier)
            rr["v313_target_reason"] = tier
            buckets[tier].append(rr)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    limits = {"P1_SA_FULL": limit, "P2_B_LIMITED": V313_TARGET_MAX_B, "P3_C_LIGHT": V313_TARGET_MAX_C}
    for tier in ["P1_SA_FULL", "P2_B_LIMITED", "P3_C_LIGHT"]:
        take = 0
        for r in sorted(buckets[tier], key=_v313_row_priority, reverse=True):
            t = _v274_ticker(r)
            if not t or t in seen or t in STABLE_EXCLUDED:
                continue
            out.append(r)
            seen.add(t)
            take += 1
            if len(out) >= limit or take >= limits[tier]:
                break
        if len(out) >= limit:
            break
    return out


def _v313_write_targets_light(priority_rows: List[Dict[str, Any]], reason: str = "v313_tiered_info_targets") -> None:
    tickers: List[str] = []
    meta: Dict[str, Any] = {}
    for r in priority_rows or []:
        t = _v274_ticker(r)
        if not t or t in tickers or t in STABLE_EXCLUDED:
            continue
        tickers.append(t)
        meta[t] = {
            "source": "v313_tiered_info_pipeline",
            "tier": str(r.get("v313_info_tier") or r.get("v313_target_reason") or ""),
            "grade": _v313_grade(r),
            "score": fnum(r.get("score") or r.get("leader_score"), 0.0),
            "primary": str(r.get("strategy_bucket_primary_label") or ""),
        }
    payload = {
        "version": BOT_VERSION,
        "schema": "v313_tiered_external_targets",
        "updated_ts": now_ts(),
        "updated_text": now_text(),
        "ttl_sec": 45,
        "reason": reason,
        "targets": tickers,
        "target_count": len(tickers),
        "target_meta": meta,
        "note": "v313: S/A는 풀정보, 상위 B/C는 제한정보, 낮은 B/C는 target 제외. 전략정보는 삭제하지 않음.",
    }
    try:
        save_json(FILES.get("micro_urgent", BASE_DIR / "clean_micro_urgent_targets.json"), payload)
        save_json(FILES.get("ws_targets", BASE_DIR / "clean_ws_targets.json"), payload)
        save_json(FILES.get("micro_targets", BASE_DIR / "clean_micro_targets.json"), payload)
        with _state_lock:
            STATE["v313_target_rows"] = len(tickers)
            STATE["v313_target_ts"] = now_ts()
            STATE["ws_target_file_targets"] = len(tickers)
            STATE["micro_target_file_targets"] = len(tickers)
            STATE["ws_target_reason"] = reason
            STATE["micro_target_reason"] = reason
    except Exception as exc:
        log_error("v313_write_targets_light", exc)


def _v311_write_strategy_files_compact(strict_rows: List[Dict[str, Any]], shadow_rows: List[Dict[str, Any]], market: Dict[str, Any]) -> None:  # type: ignore[override]
    nowv = now_ts()
    rows_raw = list(strict_rows or []) + list(shadow_rows or [])
    # Preserve strategy information for review, but store only compact/tiered fields.
    rows_sorted = sorted([r for r in rows_raw if isinstance(r, dict)], key=_v313_row_priority, reverse=True)
    compact_rows = [_v313_compact_snapshot_row(r) for r in rows_sorted[:V313_MATERIAL_MAX_ROWS]]
    grade = _v311_overall_counts(compact_rows)
    ready_n = len([r for r in compact_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    payload = {
        "version": BOT_VERSION,
        "schema": "strategy_basket_snapshot_v313_tiered_compact",
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "strategy": "전략 바구니 S/A 검증",
        "strategy_keys": list(V310_STRATEGIES.keys()),
        "scan_id": str(STATE.get("scan_id") or ""),
        "grade": grade,
        "strategy_grade_counts": _v311_strategy_counts(compact_rows),
        "paper_open": ready_n,
        "target_rows": int(STATE.get("v313_target_rows", 0) or 0),
        "rows": compact_rows,
        "note": "v313: 전략별 등급 단일원본 + 후보별 정보요구량 차등 저장. 전략정보는 삭제하지 않는다.",
    }
    sig = _v311_rows_signature(compact_rows)
    if sig != str(STATE.get("v313_strategy_snapshot_sig") or "") or nowv - fnum(STATE.get("v313_strategy_snapshot_ts"), 0.0) > 20:
        save_json(FILES["strategy_material_snapshot"], payload)
        write_jsonl_replace(FILES["strategy_material_latest"], compact_rows)
        with _state_lock:
            STATE["v313_strategy_snapshot_sig"] = sig
            STATE["v313_strategy_snapshot_ts"] = nowv
            STATE["v313_strategy_snapshot_written"] = len(compact_rows)
    else:
        with _state_lock:
            STATE["v313_strategy_snapshot_skipped"] = int(STATE.get("v313_strategy_snapshot_skipped", 0) or 0) + 1


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:  # type: ignore[override]
    ts = now_ts()
    scan_id = str(STATE.get("scan_id") or f"scan-{int(ts)}")
    strict_full = [_v313_recompute_strategy_fields(_v288_refresh_latest_row(r, "strict", scan_id)) for r in (strict or [])]
    shadow_full = [_v313_recompute_strategy_fields(_v288_refresh_latest_row(r, "shadow", scan_id)) for r in (shadow or [])]
    priority_rows = _v313_target_priority_rows(strict_full, shadow_full, limit=V313_TARGET_MAX_TOTAL)
    _v313_write_targets_light(priority_rows, reason="v313_export_tiered_targets")
    strict_rows = [_v313_compact_snapshot_row(r) for r in strict_full]
    # Store enough B/C review data for dashboard; target queue is separately limited.
    shadow_sorted = sorted(shadow_full, key=_v313_row_priority, reverse=True)
    shadow_rows = [_v313_compact_snapshot_row(r) for r in shadow_sorted[:V313_MATERIAL_MAX_ROWS]]
    write_error = ""
    ok, err = write_jsonl_replace(FILES["paper_latest"], strict_rows)
    if not ok:
        write_error = f"paper_latest:{err}"
    ok2, err2 = write_jsonl_replace(FILES["shadow_latest"], shadow_rows)
    if not ok2:
        write_error = (write_error + " / " if write_error else "") + f"shadow_latest:{err2}"
    try:
        market = strict_rows[0].get("market_context") if strict_rows and isinstance(strict_rows[0].get("market_context"), dict) else _v274_market_context(strict_rows + shadow_rows)
        _v311_write_strategy_files_compact(strict_rows, shadow_rows, market)
    except Exception as exc:
        log_error("v313_strategy_summary_write", exc)
    ready_n = len([r for r in strict_rows if bool(r.get("trade_ready") or r.get("paper_bot_open") or r.get("open_eligible"))])
    recheck_n = len([r for r in strict_rows if str(r.get("final_entry_action")) == "recheck_wait"])
    with _state_lock:
        STATE["latest_trade_ready"] = ready_n
        STATE["trade_ready_written"] = ready_n
        STATE["paper_latest_written"] = len(strict_rows)
        STATE["shadow_latest_written"] = len(shadow_rows)
        STATE["latest_final_recheck_wait"] = recheck_n
        STATE["v313_tiered_export"] = True
        STATE["v313_review_target_rows"] = len(priority_rows)
        if write_error:
            STATE["candidate_write_error"] = write_error
    return {
        "paper_latest_written": len(strict_rows),
        "shadow_latest_written": len(shadow_rows),
        "latest_trade_ready": ready_n,
        "latest_final_recheck_wait": recheck_n,
        "urgent_watch_rows": len(priority_rows),
        "write_error": write_error,
        "v313_tiered_export": True,
    }


def v212_publish_final_candidate_state(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]], market_rows: List[Dict[str, Any]], pipe: Dict[str, Any]) -> str:  # type: ignore[override]
    """v313: duplicate heavy snapshot/target publishing is cut.

    export_candidates already writes the tiered target files and compact material snapshot.
    This function must not re-run update_ws_targets/update_micro_targets or full snapshot overlay
    for 70 B/C rows, because that was the v312 bottleneck.
    """
    try:
        if not int(STATE.get("v313_review_target_rows", 0) or 0):
            strict_rows = [r for r in (strict or []) if isinstance(r, dict)]
            shadow_rows = [r for r in (shadow or []) if isinstance(r, dict)]
            priority_rows = _v313_target_priority_rows(strict_rows, shadow_rows, limit=V313_TARGET_MAX_TOTAL)
            _v313_write_targets_light(priority_rows, reason="v313_publish_fallback_targets")
            STATE["v313_review_target_rows"] = len(priority_rows)
    except Exception as exc:
        log_error("v313_publish_light", exc)
    return (
        f"target_queue {STATE.get('v313_review_target_rows',0)} / WS targets {STATE.get('ws_target_file_targets',0)} / "
        f"micro targets {STATE.get('micro_target_file_targets',0)} / duplicate_snapshot skipped / v313 tiered"
    )


def _v288_score_text(rows: Optional[List[Dict[str, Any]]] = None) -> str:  # type: ignore[override]
    rows0 = rows if isinstance(rows, list) else _v301_rows_from_snapshot(180)
    rows0 = [_v313_recompute_strategy_fields(r) for r in rows0 or [] if isinstance(r, dict)]
    overall = _v311_overall_counts(rows0)
    closed_lines = []
    try:
        closed_lines = _v304_grade_stats_lines() if '_v304_grade_stats_lines' in globals() else []
    except Exception as exc:
        log_error("v313_closed_stats_line", exc)
    lines = [
        "📊 /score 요약 · v313 전략별 단일원본",
        "전략: 전략 바구니 S/A 검증 (5전략)",
        "- S/A=실제 paper OPEN, B/C=가상복기, X=차단",
        f"- 합산: S {overall.get('S',0)} / A {overall.get('A',0)} / B {overall.get('B',0)} / C {overall.get('C',0)} / 제외 {overall.get('X',0)}",
        *_v311_strategy_counts_lines(rows0),
        "",
        "[S/A 실제 OPEN 후보 TOP]",
        *_v311_top_rows(rows0, {"S", "A"}, 5),
        "",
        "[B/C 가상복기 TOP]",
        *_v311_top_rows(rows0, {"B", "C"}, 5),
    ]
    if closed_lines:
        lines += ["", "[CLOSED 참고 · 과거 누적 포함]"] + closed_lines[:10]
    lines += ["", "- 상세 전체표는 /score 단독 실행"]
    return "\n".join(lines)


def _v288_quality_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(220 if full else 120)
    rows = [_v313_recompute_strategy_fields(r) for r in rows or [] if isinstance(r, dict)]
    overall = _v311_overall_counts(rows)
    lines = [
        "🔍 /quality 요약 · v313 전략별 후보품질",
        f"- 합산: S {overall.get('S',0)} / A {overall.get('A',0)} / B {overall.get('B',0)} / C {overall.get('C',0)} / 제외 {overall.get('X',0)}",
        *_v311_strategy_counts_lines(rows),
        "",
        "[S/A 실제 OPEN 후보]",
        *_v311_top_rows(rows, {"S", "A"}, 6),
        "",
        "[B/C 가상복기 TOP]",
        *_v311_top_rows(rows, {"B", "C"}, 8 if full else 5),
        "",
        "- B/C는 OPEN 없이 가상복기. 낮은 B/C는 target 제외해 병목을 줄임. 상세는 /quality_full",
    ]
    return "\n".join(lines)


def _v288_strategy_watch_text(full: bool = False) -> str:  # type: ignore[override]
    rows = _v301_rows_from_snapshot(220 if full else 120)
    rows = [_v313_recompute_strategy_fields(r) for r in rows or [] if isinstance(r, dict)]
    lines = [
        "👀 /strategy_watch 요약 · v313 정보요구량 차등배관",
        "- S/A=실제 paper OPEN, B/C=가상복기, X=차단",
        *_v311_strategy_counts_lines(rows),
        "",
        "[전략별 S/A 후보]",
    ]
    for key, label in V310_STRATEGIES.items():
        sub = [r for r in rows if _v313_strategy_grade_from_row(r, key) in {"S", "A"}]
        lines.append(f"[{label} S/A]")
        lines.extend(_v311_top_rows(sub, {"S", "A", "B", "C", "X"}, 3))
    lines += ["", "[가상복기 B/C 대표]", *_v311_top_rows(rows, {"B", "C"}, 5), "- 상세 감시는 /strategy_watch_full"]
    return "\n".join(lines)


_v313_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v313_base_health_text()
    try:
        text = text.replace("전략 바구니 S/A 검증 · v312 정보배관복구", "전략 바구니 S/A 검증 · v313 차등정보배관")
        text = text.replace("strategy_basket compact 저장 · S/A paper · B/C 정보보강 target", "strategy_basket tiered 저장 · S/A 풀정보 · 상위 B/C 제한정보")
        text = text.replace("v312", "v313") if "v312:" in text else text
        if "v313" not in text:
            text += "\n- v313: S/A 풀정보, 상위 B/C 제한정보, 낮은 B/C 경량복기. target/snapshot 중복출하 차단."
    except Exception as exc:
        log_error("v313_health_text", exc)
    return text

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = "전략 바구니 S/A 검증"
    STATE["phase_note"] = V313_NOTE
    STATE["v313_tiered_info_pipeline"] = True
    STATE["v313_strategy_single_source"] = True
except Exception as exc:
    try:
        log_error("v313_init", exc)
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────────────────────
# v2.13.314: Telegram send path surgery + current strategy snapshot single-source fix
# - 반복 timeout을 만드는 명령별 다중 전송 경로를 기본 자동묶음에서 끊는다.
# - 자동묶음은 한 번의 짧은 응답으로만 보낸다. 단독 상세 명령은 최대 2청크까지만 보낸다.
# - telegram 전송 timeout은 봇 로직 오류가 아니므로 /errorlog 새 실행 오류로 올리지 않는다.
# - v313 snapshot payload에 strategy_key가 없어 v312/stale latest로 fallback하던 독자 경로를 끊는다.
# - 전략별 S/A/B/C 표는 strategy_material_snapshot.rows 단일 원본을 우선 읽는다.
# - 전략/조건/청산/paper 장부/WS·micro sidecar는 변경하지 않는다.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.314"
V314_NOTE = "v314: Telegram 자동묶음 단일응답 수술 + stale 캐시 차단 + 전략별 snapshot 단일원본. 조건/청산 변경 없음."
V314_SEND_TIMEOUT_SEC = float(os.getenv("CLEAN_V314_SEND_TIMEOUT_SEC", "1.2"))
V314_MULTI_MAX_CHARS = int(os.getenv("CLEAN_V314_MULTI_MAX_CHARS", "3600"))
V314_SINGLE_CHUNK_LIMIT = int(os.getenv("CLEAN_V314_SINGLE_CHUNK_LIMIT", "2600"))
V314_SINGLE_MAX_CHUNKS = int(os.getenv("CLEAN_V314_SINGLE_MAX_CHUNKS", "2"))


def _v314_is_send_timeout_error(msg: str) -> bool:
    s = str(msg or "")
    return ("telegram_send_api" in s or "v307_reply" in s or "telegram_reply" in s) and ("TimeoutError" in s or "timed out" in s)


_v314_base_log_error = log_error

def log_error(where: str, exc: BaseException) -> None:  # type: ignore[override]
    """v314: Telegram send timeout is transport noise, not bot logic failure.

    반복되는 telegram_send_api TimeoutError가 /errorlog를 계속 오염시켜 실제 코드 오류와 구분이 안 됐다.
    전송 실패는 runtime 상태값으로만 남기고 errorlog/_recent_errors에는 올리지 않는다.
    """
    try:
        msg = f"{where}: {exc.__class__.__name__}: {exc}"
        if _v314_is_send_timeout_error(msg):
            with _state_lock:
                STATE["telegram_send_timeout_count"] = int(STATE.get("telegram_send_timeout_count", 0) or 0) + 1
                STATE["telegram_send_last_timeout_ts"] = now_ts()
                STATE["telegram_send_last_timeout_text"] = msg[:180]
            try:
                append_text(FILES["runtime"], f"[{now_text()}] telegram_send_timeout_suppressed {msg[:220]}")
            except Exception:
                pass
            return
    except Exception:
        pass
    return _v314_base_log_error(where, exc)


_v314_base_recent_error_text = recent_error_text
_v314_base_recent_error_full_text = recent_error_full_text

def _v314_filtered_recent_errors(limit: int = 60) -> List[str]:
    out = []
    for x in list(_recent_errors)[-int(limit):]:
        if _v314_is_send_timeout_error(str(x)):
            continue
        out.append(str(x))
    return out


def recent_error_text(n: int = 60) -> str:  # type: ignore[override]
    recent = _v314_filtered_recent_errors(n)
    if recent:
        return "❌ 새 실행 중 오류 있음\n" + "\n".join(recent[-12:])
    txt = _v314_base_recent_error_text(n)
    if _v314_is_send_timeout_error(txt):
        return "✅ 새 실행 중 오류 없음\n- Telegram 전송 timeout은 v314부터 봇 오류로 표시하지 않음"
    return txt


def recent_error_full_text(n: int = 100) -> str:  # type: ignore[override]
    recent = _v314_filtered_recent_errors(n)
    if recent:
        return "[새 실행 오류]\n" + "\n".join(recent[-40:])
    return _v314_base_recent_error_full_text(n)


def _v314_send_one_no_error(text: str, timeout: float = V314_SEND_TIMEOUT_SEC) -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        try:
            print(text, flush=True)
        except Exception:
            pass
        return False
    body = str(text or "")
    # Telegram limit 보호. 자동묶음은 한 메시지로 끝내기 위해 강제 절단한다.
    if len(body) > 4050:
        body = body[:3920] + "\n…\n- v314: 전송 안정화를 위해 자동으로 줄였습니다. 상세는 단독 명령을 실행하세요."
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": body,
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            resp.read(120)
        return True
    except Exception as exc:
        # errorlog 오염 방지. 전송 실패는 상태에만 남긴다.
        try:
            with _state_lock:
                STATE["telegram_send_failed_count"] = int(STATE.get("telegram_send_failed_count", 0) or 0) + 1
                STATE["telegram_send_last_fail_ts"] = now_ts()
                STATE["telegram_send_last_fail_text"] = f"{exc.__class__.__name__}: {exc}"[:180]
            append_text(FILES["runtime"], f"[{now_text()}] telegram_send_failed_no_errorlog {exc.__class__.__name__}: {exc}")
        except Exception:
            pass
        return False


def reply(update, text: str) -> float:  # type: ignore[override]
    """v314 단일 답장 입구.

    기존 reply는 긴 메시지를 여러 청크로 계속 보내다 50초 이상 block될 수 있었다.
    이제 단독 명령도 최대 2청크까지만 전송한다. 긴 전문은 *_full 단독 명령에서 보되,
    그래도 전송 안정성을 우선한다.
    """
    st = now_ts()
    body = str(text or "")
    try:
        max_len = max(1000, V314_SINGLE_CHUNK_LIMIT * max(1, V314_SINGLE_MAX_CHUNKS))
        if len(body) > max_len:
            body = body[:max_len - 90] + "\n…\n- v314: 긴 출력은 안정성을 위해 줄였습니다. 필요한 상세는 full/단독 명령으로 확인."
        for i in range(0, len(body), max(1000, V314_SINGLE_CHUNK_LIMIT)):
            chunk = body[i:i+V314_SINGLE_CHUNK_LIMIT]
            _v314_send_one_no_error(chunk, timeout=V314_SEND_TIMEOUT_SEC)
            if (i // max(1000, V314_SINGLE_CHUNK_LIMIT) + 1) >= V314_SINGLE_MAX_CHUNKS:
                break
    except Exception as exc:
        try:
            log_error("v314_reply", exc)
        except Exception:
            pass
    return max(0.0, now_ts() - st)


def _v314_hard_compact(name: str, body: str, limit: int) -> str:
    """Command-specific ultra compact section for multi-command single response."""
    text = str(body or "")
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    keep: List[str] = []
    if name == "health":
        prefs = ("✅ 수익형", "⚠️ /health", "❌", "⚠️ 메모리", "⚠️ CPU", "✅ CPU", "❌ 메모리", "- 오류:", "- 병목:", "- 합계:")
        keep = [ln for ln in lines if ln.startswith(prefs) or "최근완료" in ln or "단계:" in ln][:8]
    elif name == "score":
        prefs = ("📊", "전략:", "- ✅ S", "- ✅ A", "- 🟡 B", "- S/A 자동", "- 전체 현재", "- 합산:")
        keep = [ln for ln in lines if ln.startswith(prefs)][:12]
    elif name == "quality":
        prefs = ("🔍", "- 합산:", "- 실제 paper", "- 주도추세", "- 돈흐름", "- 저점", "- 거래대금", "- 평균", "- 상세")
        keep = [ln for ln in lines if ln.startswith(prefs)][:14]
    elif name in {"strategy_watch", "watch"}:
        prefs = ("👀", "- S/A=", "- 실제 paper", "- 주도추세", "- 돈흐름", "- 저점", "- 거래대금", "- 평균", "[S급", "[A급", "- 상세")
        keep = [ln for ln in lines if ln.startswith(prefs)][:14]
    elif name == "errorlog":
        keep = lines[:8]
    else:
        keep = lines[:12]
    if not keep:
        keep = lines[:8]
    out = "\n".join(keep)
    if len(out) > limit:
        out = out[:max(200, limit - 40)] + "\n…"
    return out


def _v314_process_command_lines(update, context, lines: List[str], source: str = "auto") -> bool:
    """v314 multi-command single-response pipeline.

    더 이상 명령마다 Telegram 전송을 하지 않는다. 빌드는 모두 수행하고, 결과는 하나의 짧은 메시지로만 보낸다.
    전송 timeout 반복과 errorlog 오염을 끊기 위한 실제 경로 수술이다.
    """
    lines = [ln for ln in (lines or []) if str(ln).strip().startswith("/")]
    if not lines:
        return False
    total_start = now_ts()
    sections: List[str] = ["📦 자동 묶음 compact 실행 · v314 단일응답", "- 명령별 전송을 끊고 한 번만 보냅니다", "- 상세 전문은 각 명령 단독 실행"]
    rows: List[Tuple[str, float, str]] = []
    per_limit = max(360, int((V314_MULTI_MAX_CHARS - 700) / max(1, len(lines))))
    for idx, line in enumerate(lines, start=1):
        st = now_ts()
        name = _command_name_from_line(line)
        res = "OK"
        if name == "batch" and source == "auto":
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
                    body = _v307_compact_for_multi(name, body) if '_v307_compact_for_multi' in globals() else body
                    body = _v314_hard_compact(name, body, per_limit)
                except Exception as exc:
                    log_error(f"multi:{name}", exc)
                    body = f"오류: {exc.__class__.__name__}: {exc}"
                    res = f"ERR {exc.__class__.__name__}"
        sec = max(0.0, now_ts() - st)
        rows.append((name, sec, res))
        sections.append(f"\n[{idx}/{len(lines)}] /{name} (계산 {sec:.2f}s / {res})\n{body}")
    build_total = sum(x[1] for x in rows)
    sections.append("\n🧾 자동 묶음 시간표")
    for name, sec, res in rows:
        icon = "✅" if res == "OK" else ("❔" if res in {"SKIP", "UNKNOWN"} else "❌")
        sections.append(f"- {icon} /{name}: 계산 {sec:.2f}s / {res}")
    sections.append(f"- 합계: 계산 {build_total:.2f}s / 전체 {now_ts() - total_start:.2f}s / 전송 1회")
    msg = "\n".join(sections)
    if len(msg) > V314_MULTI_MAX_CHARS:
        msg = msg[:V314_MULTI_MAX_CHARS - 120] + "\n…\n- v314: 자동묶음은 안정성을 위해 여기서 줄임. 상세는 단독 명령."
    send_st = now_ts()
    _v314_send_one_no_error(msg, timeout=V314_SEND_TIMEOUT_SEC)
    with _state_lock:
        STATE["v314_last_multi_send_sec"] = max(0.0, now_ts() - send_st)
        STATE["v314_last_multi_build_sec"] = build_total
    return True


def handle_multi_command_message(update, context) -> bool:  # type: ignore[override]
    try:
        text = getattr(getattr(update, "message", None), "text", "") or ""
    except Exception:
        text = ""
    lines = _extract_command_lines(text)
    if len(lines) <= 1:
        return False
    return _v314_process_command_lines(update, context, lines, source="auto")


def command_batch(update, context) -> None:  # type: ignore[override]
    lines = ["/health", "/score", "/quality", "/strategy_watch", "/errorlog"]
    try:
        text = getattr(getattr(update, "message", None), "text", "") or ""
        custom = _extract_command_lines(text)
        if len(custom) > 1:
            lines = custom[1:] if custom[0].split()[0].lower() == "/batch" else custom
    except Exception:
        pass
    _v314_process_command_lines(update, context, lines, source="batch")


_v314_base_rows_from_snapshot = _v301_rows_from_snapshot

def _v301_rows_from_snapshot(limit: int = 180) -> List[Dict[str, Any]]:  # type: ignore[override]
    """v314 current strategy basket row reader.

    v313 snapshot has strategy_keys/schema but no legacy strategy_key, so older reader rejected it and
    fell back to stale v312/latest data. That made 합산표 and 전략별표 read different sources.
    """
    try:
        obj = load_json(FILES.get("strategy_material_snapshot", BASE_DIR / "strategy_material_snapshot.json"), {})
        if isinstance(obj, dict):
            ver = str(obj.get("version") or "")
            schema = str(obj.get("schema") or "")
            rows = obj.get("rows") or []
            has_basket = isinstance(obj.get("strategy_keys"), list) or "strategy_basket" in schema
            if rows and isinstance(rows, list) and (ver == BOT_VERSION or has_basket):
                out = [r for r in rows[:limit] if isinstance(r, dict)]
                return [_v313_recompute_strategy_fields(r) if '_v313_recompute_strategy_fields' in globals() else r for r in out]
    except Exception as exc:
        try:
            log_error("v314_rows_snapshot", exc)
        except Exception:
            pass
    try:
        rows = tail_jsonl(FILES.get("strategy_material_latest", BASE_DIR / "strategy_material_latest.jsonl"), max_lines=limit)
        out = [r for r in rows or [] if isinstance(r, dict)]
        # legacy strategy_key filter 제거: strategy basket rows may keep paper_strategy_key only.
        return [_v313_recompute_strategy_fields(r) if '_v313_recompute_strategy_fields' in globals() else r for r in out[:limit]]
    except Exception:
        return []


def _v314_strategy_counts_from_rows(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, int]]:
    rows = rows if isinstance(rows, list) else _v301_rows_from_snapshot(200)
    out = {k: {"S": 0, "A": 0, "B": 0, "C": 0, "X": 0} for k in V310_STRATEGIES.keys()}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        rr = _v313_recompute_strategy_fields(r) if '_v313_recompute_strategy_fields' in globals() else r
        gs = rr.get("strategy_eval_grades") if isinstance(rr.get("strategy_eval_grades"), dict) else {}
        for k in out:
            g = str(gs.get(k) or "C").upper().strip()
            if g not in out[k]:
                g = "C"
            out[k][g] += 1
    return out


def _v311_strategy_counts(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Dict[str, int]]:  # type: ignore[override]
    return _v314_strategy_counts_from_rows(rows)


def _v311_strategy_counts_lines(rows: Optional[List[Dict[str, Any]]] = None) -> List[str]:  # type: ignore[override]
    rows = rows if isinstance(rows, list) else _v301_rows_from_snapshot(200)
    counts = _v314_strategy_counts_from_rows(rows)
    lines = ["[전략별 현재 등급 · v314 단일 snapshot 원본]", "- 실제 paper OPEN: 각 전략 S/A만. B/C는 OPEN 없이 가상복기. 제외는 차단."]
    for k, label in V310_STRATEGIES.items():
        c = counts.get(k, {})
        lines.append(f"- {label}: S {c.get('S',0)} / A {c.get('A',0)} / B {c.get('B',0)} / C {c.get('C',0)} / 제외 {c.get('X',0)}")
    return lines


_v314_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v314_base_health_text()
    try:
        text = text.replace("v313 차등정보배관", "v314 단일응답·snapshot 단일원본")
        text = text.replace("수익형 v2.13.313", BOT_VERSION)
        if "v314" not in text:
            text += "\n- v314: 자동묶음 단일응답, Telegram timeout errorlog 오염 차단, 전략별 snapshot 단일원본."
    except Exception as exc:
        log_error("v314_health_text", exc)
    return text


try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V314_NOTE
    STATE["v314_single_reply_pipeline"] = True
    STATE["v314_strategy_snapshot_reader"] = True
except Exception as exc:
    try:
        log_error("v314_init", exc)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# v2.13.315: final main-call surgery
# - v314 block was placed after the __main__ call, so runtime still started as v313.
# - Keep v314 Telegram/snapshot fixes, but move the only main() call to the real file end.
# - No strategy/condition/closing/paper-ledger changes.
# ──────────────────────────────────────────────────────────────────────────────
BOT_VERSION = "수익형 v2.13.315"
V315_NOTE = "v315: main() 최종 위치 수술. v314 전송/snapshot 수술이 실제 실행 전에 적용되도록 고정. 전략/조건/청산 변경 없음."

_v315_base_health_text = _v288_health_text

def _v288_health_text() -> str:  # type: ignore[override]
    text = _v315_base_health_text()
    try:
        text = text.replace("수익형 v2.13.314", BOT_VERSION)
        text = text.replace("수익형 v2.13.313", BOT_VERSION)
        text = text.replace("v314 단일응답·snapshot 단일원본", "v315 main-end 고정 + v314 수술 활성화")
        if "v315" not in text:
            text += "\n- v315: main() 최종 위치 고정. v314 전송/snapshot 수술이 실제 실행 전에 적용됨."
    except Exception as exc:
        try:
            log_error("v315_health_text", exc)
        except Exception:
            pass
    return text

try:
    STATE["version"] = BOT_VERSION
    STATE["strategy"] = STRATEGY_NAME
    STATE["phase_note"] = V315_NOTE
    STATE["v315_main_call_fixed"] = True
    STATE["v314_single_reply_pipeline"] = True
    STATE["v314_strategy_snapshot_reader"] = True
except Exception as exc:
    try:
        log_error("v315_init", exc)
    except Exception:
        pass

# Final entry point. Keep this as the only __main__ block and at the real end of file.
if __name__ == "__main__":
    main()
