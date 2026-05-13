#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
수익형_v2.13.153.py

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
    from telegram import Bot, BotCommand
    from telegram.ext import Updater, CommandHandler
except Exception:  # py_compile 단계에서는 실제 import 성공이 필수는 아님
    Bot = None
    BotCommand = None
    Updater = None
    CommandHandler = None

BOT_VERSION = "수익형 v2.13.153"
STRATEGY_NAME = "거래대금 눌림 재돌파"
STRATEGY_KEY = "money_pullback_rebreakout_clean"
BASE_DIR = Path(__file__).resolve().parent
TIMEZONE_LABEL = "Asia/Seoul"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# 스캔을 줄이지 않는다: 매 cycle ALL_KRW 전체를 가져와 451개 안팎의 가격/24h 거래대금을 본다.
SCAN_INTERVAL_SEC = float(os.getenv("CLEAN_SCAN_INTERVAL_SEC", "5"))
ALL_MARKET_TIMEOUT_SEC = float(os.getenv("CLEAN_ALL_MARKET_TIMEOUT_SEC", "3.0"))
CANDLE_TIMEOUT_SEC = float(os.getenv("CLEAN_CANDLE_TIMEOUT_SEC", "2.2"))
PRECISION_TTL_SEC = float(os.getenv("CLEAN_PRECISION_TTL_SEC", "45"))
PRECISION_REFRESH_PER_SCAN = int(os.getenv("CLEAN_PRECISION_REFRESH_PER_SCAN", "28"))
PRECISION_WORKERS = int(os.getenv("CLEAN_PRECISION_WORKERS", "6"))
PRECISION_TOP_TURNOVER = int(os.getenv("CLEAN_PRECISION_TOP_TURNOVER", "140"))
PRECISION_TOP_MOVE = int(os.getenv("CLEAN_PRECISION_TOP_MOVE", "40"))
STRICT_LIMIT = int(os.getenv("CLEAN_STRICT_LIMIT", "8"))
SHADOW_LIMIT = int(os.getenv("CLEAN_SHADOW_LIMIT", "32"))
# v150/v139 단일전략 흐름 기준값. 조건을 새로 조이는 목적이 아니라, v150에서 실제 쓰던 점수 흐름을 clean 파일에 옮긴다.
MIN_STRICT_SCORE = float(os.getenv("CLEAN_MIN_STRICT_SCORE", "2.15"))
FRESH_OK_SEC = float(os.getenv("CLEAN_FRESH_OK_SEC", "15"))
FRESH_WEAK_SEC = float(os.getenv("CLEAN_FRESH_WEAK_SEC", "35"))
STALE_BLOCK_SEC = float(os.getenv("CLEAN_STALE_BLOCK_SEC", "75"))
EVENT_DEDUP_SEC = int(os.getenv("CLEAN_EVENT_DEDUP_SEC", "150"))
CANDIDATE_FILE_KEEP_LINES = int(os.getenv("CLEAN_CANDIDATE_FILE_KEEP_LINES", "12000"))

FILES = {
    "paper": BASE_DIR / "paper_candidates.jsonl",
    "shadow": BASE_DIR / "shadow_candidates.jsonl",
    "status": BASE_DIR / "clean_brain_status.json",
    "error": BASE_DIR / "clean_brain_error.log",
    "runtime": BASE_DIR / "clean_brain_runtime.log",
    "cache": BASE_DIR / "clean_market_cache.json",
    "reject": BASE_DIR / "clean_reject_summary.json",
    "paper_open": BASE_DIR / "paper_bot_open.json",
    "paper_closed": BASE_DIR / "paper_bot_closed.jsonl",
    "paper_status": BASE_DIR / "paper_bot_status.json",
    "paper_control": BASE_DIR / "paper_bot_control.json",
    "paper_flag": BASE_DIR / "external_paper_bot_on.flag",
    "legacy_paper_flag": BASE_DIR / "external_paper_runner_on.flag",
}

STABLE_EXCLUDED = {"USDC", "USDT", "BUSD", "USDP", "DAI", "TUSD", "FDUSD", "USDS", "USD1", "PYUSD", "USDE", "RLUSD"}

_state_lock = threading.RLock()
_stop_event = threading.Event()
_precision_lock = threading.RLock()
_precision_cache: Dict[str, Dict[str, Any]] = {}
_precision_cursor = 0
_seen_events: Dict[str, float] = {}
_recent_strict = deque(maxlen=12)
_recent_shadow = deque(maxlen=12)
_recent_errors = deque(maxlen=20)

STATE: Dict[str, Any] = {
    "version": BOT_VERSION,
    "strategy": STRATEGY_NAME,
    "started_at": time.time(),
    "scan_calls": 0,
    "scan_last_sec": 0.0,
    "scan_max_sec": 0.0,
    "scan_last_ts": 0.0,
    "scan_last_stage": "boot",
    "scan_last_error": "",
    "bulk_rows": 0,
    "bulk_price": 0,
    "bulk_money": 0,
    "precision_have": 0,
    "precision_refreshed": 0,
    "precision_failed": 0,
    "precision_selected": 0,
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
    "phase_note": "새 본선 rebuild: v150 functional flow preserved without old fallback/tail/candidate_events",
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
    try:
        if not path.exists() or keep_lines <= 0:
            return
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


def http_json(url: str, timeout: float = 3.0) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": BOT_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def fetch_all_krw() -> Tuple[List[Dict[str, Any]], str]:
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    data = http_json(url, timeout=ALL_MARKET_TIMEOUT_SEC)
    if not isinstance(data, dict) or str(data.get("status")) != "0000":
        raise RuntimeError(f"ALL_KRW status={data.get('status') if isinstance(data, dict) else '?'}")
    rows = []
    raw = data.get("data") or {}
    for sym, r in raw.items():
        if not isinstance(r, dict):
            continue
        t = str(sym).upper().strip()
        if t == "DATE" or t in STABLE_EXCLUDED:
            continue
        price = fnum(r.get("closing_price") or r.get("trade_price") or r.get("close"), 0)
        value24 = fnum(r.get("acc_trade_value_24H") or r.get("acc_trade_value") or r.get("trade_value"), 0)
        volume24 = fnum(r.get("units_traded_24H") or r.get("units_traded"), 0)
        chg24 = fnum(r.get("fluctate_rate_24H") or r.get("fluctate_rate"), 0)
        if price <= 0:
            continue
        rows.append({
            "ticker": t,
            "current_price": price,
            "price": price,
            "turnover_24h": value24,
            "money_proxy_24h": value24,
            "volume_24h": volume24,
            "change_24h": chg24,
            "money_source": "proxy_24h",
            "source": "ALL_KRW_bulk",
            "fresh_ts": now_ts(),
        })
    rows.sort(key=lambda x: fnum(x.get("turnover_24h"), 0), reverse=True)
    for i, r in enumerate(rows, start=1):
        r["turnover_rank"] = i
    return rows, "ALL_KRW"


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
        "rsi_14": _rsi14(closes),
        "vwap_gap_pct": round(vwap_gap, 3),
        "ma5_gap_pct": round(ma5_gap, 3),
        "ma20_gap_pct": round(ma20_gap, 3),
    }


def select_precision_targets(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    top_turnover = sorted(rows, key=lambda x: fnum(x.get("turnover_24h"), 0), reverse=True)[:PRECISION_TOP_TURNOVER]
    top_move = sorted(rows, key=lambda x: abs(fnum(x.get("change_24h"), 0)), reverse=True)[:PRECISION_TOP_MOVE]
    by_t: Dict[str, Dict[str, Any]] = {}
    for r in top_turnover + top_move:
        by_t[str(r.get("ticker"))] = r
    all_rows = rows[:]
    global _precision_cursor
    if all_rows:
        # 전체 종목을 순환으로 계속 보강한다. bulk 스캔은 전체, 정밀값은 순환 갱신.
        rotate_n = min(40, len(all_rows))
        start = _precision_cursor % len(all_rows)
        rot = [all_rows[(start + i) % len(all_rows)] for i in range(rotate_n)]
        _precision_cursor = (_precision_cursor + rotate_n) % len(all_rows)
        for r in rot:
            by_t[str(r.get("ticker"))] = r
    targets = list(by_t.values())
    targets.sort(key=lambda x: (fnum(x.get("turnover_24h"), 0), abs(fnum(x.get("change_24h"), 0))), reverse=True)
    return targets


def refresh_precision(rows: List[Dict[str, Any]]) -> Tuple[int, int, int]:
    targets = select_precision_targets(rows)
    nowv = now_ts()
    need: List[Dict[str, Any]] = []
    with _precision_lock:
        for r in targets:
            t = str(r.get("ticker") or "").upper()
            cached = _precision_cache.get(t) or {}
            age = nowv - fnum(cached.get("precision_ts"), 0)
            if not cached or age > PRECISION_TTL_SEC:
                need.append(r)
            if len(need) >= PRECISION_REFRESH_PER_SCAN:
                break
    ok = fail = 0
    if not need:
        with _precision_lock:
            return 0, 0, len(_precision_cache)
    def one(r: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Optional[str]]:
        t = str(r.get("ticker") or "").upper()
        try:
            return t, build_precision(t, fnum(r.get("current_price"), 0)), None
        except Exception as exc:
            return t, {"ticker": t, "precision_ok": False, "precision_error": f"{exc.__class__.__name__}: {str(exc)[:120]}", "precision_ts": now_ts()}, str(exc)
    with ThreadPoolExecutor(max_workers=max(1, PRECISION_WORKERS)) as ex:
        futs = [ex.submit(one, r) for r in need]
        for fut in as_completed(futs, timeout=max(CANDLE_TIMEOUT_SEC * len(futs) / max(PRECISION_WORKERS, 1) + 3, 5)):
            try:
                t, prof, err = fut.result()
                with _precision_lock:
                    _precision_cache[t] = prof
                if prof.get("precision_ok"):
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
    with _precision_lock:
        have = len([1 for v in _precision_cache.values() if v.get("precision_ok") and nowv - fnum(v.get("precision_ts"), 0) <= PRECISION_TTL_SEC * 3])
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


def score_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """v150에서 실제 쓰던 v139 단일전략 점수 흐름을 새 본선에 이식.
    새 전략을 만든 것이 아니라, money/volume/rank + short momentum + position + candle 보조값 흐름을 유지한다.
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
    precision = row.get("precision_source") == "candlestick_1m"

    reasons: List[str] = []
    blocks: List[str] = []
    score = 0.0
    if price <= 0:
        blocks.append("가격 없음")
    if precision and age > STALE_BLOCK_SEC:
        blocks.append(f"정보 오래됨 {age:.0f}s")
    elif precision and age > FRESH_WEAK_SEC:
        score -= 0.35
        reasons.append(f"정보 약간 오래됨 {age:.0f}s")

    money_ok = False
    if money >= 20_000_000:
        score += 1.25; money_ok = True; reasons.append("거래대금 강함")
    elif money >= 5_000_000:
        score += 0.85; money_ok = True; reasons.append("거래대금 확인")
    elif money > 0:
        score += 0.35; reasons.append("거래대금 약함")
    if vol >= 1.60:
        score += 1.05; money_ok = True; reasons.append("거래량 증가 강함")
    elif vol >= 1.15:
        score += 0.70; money_ok = True; reasons.append("거래량 증가")
    elif vol >= 1.00:
        score += 0.25; reasons.append("거래량 보통")
    if rank_best <= 5:
        score += 0.85; reasons.append(f"시장상위 rank {rank_best}")
    elif rank_best <= 20:
        score += 0.45; reasons.append(f"시장상위 rank {rank_best}")
    elif turnover_24h >= 1_000_000_000:
        score += 0.20; reasons.append("24h 유동성 확인")

    short_momo = max(ch1, ch3, ch5)
    move_ok = False
    if short_momo >= 0.55:
        score += 1.05; move_ok = True; reasons.append("단기 재상승")
    elif short_momo >= 0.18 and max(ch15, ch30) >= 0.05:
        score += 0.75; move_ok = True; reasons.append("단기 양수 전환")
    elif short_momo >= 0.05 and max(ch15, ch30) >= 0.25:
        score += 0.45; move_ok = True; reasons.append("큰 흐름 양수")
    if max(ch15, ch30) >= 0.35:
        score += 0.35; reasons.append("큰 흐름 양수")

    position_ok = True
    if 0 <= high_gap <= 0.12 and from_low >= 0.50:
        position_ok = False; blocks.append("고점 바로 붙은 추격")
    elif 0 <= high_gap <= 0.35:
        score += 0.20; reasons.append(f"고점거리 {high_gap:.2f}%")
    elif 0.35 < high_gap <= 2.8:
        score += 0.55; reasons.append(f"고점거리 {high_gap:.2f}%")
    elif high_gap < 900:
        reasons.append(f"고점거리 {high_gap:.2f}%")
    if from_low >= 5.2:
        position_ok = False; blocks.append("저점대비 과열")
    elif 0.10 <= from_low <= 3.8:
        score += 0.35; reasons.append(f"저점대비 {from_low:.2f}%")

    # 보조값은 있으면 반영, 없다고 바로 탈락시키지 않는다. 이게 v150 흐름에 가깝다.
    if close_pos >= 0.58:
        score += 0.25; reasons.append("현재봉 종가위치 양호")
    if upper >= 0.75 and close_pos and close_pos < 0.50:
        score -= 0.45; blocks.append("윗꼬리 밀림")
    if 0 < rsi <= 78:
        score += 0.15
    elif rsi > 82:
        score -= 0.25; blocks.append("RSI 과열")
    if abs(vwap_gap) <= 0.65 and vwap_gap != 0:
        score += 0.15; reasons.append("VWAP 근처")
    if ma5_gap >= -0.20:
        score += 0.10

    evidence_count = int(money > 0 or vol >= 1.05) + int(move_ok) + int(rank_best <= 20) + int(position_ok and (high_gap < 900 or from_low > 0))
    if evidence_count < 2:
        blocks.append("근거 부족")
    if not money_ok and rank_best > 20:
        blocks.append("돈흐름 입력/강도 부족")
    if not move_ok:
        blocks.append("재상승 신호 부족")
    ok = bool(price > 0 and evidence_count >= 2 and move_ok and position_ok and score >= MIN_STRICT_SCORE and not any(str(x).startswith("정보 오래됨") for x in blocks))
    if not ok and not blocks:
        blocks.append("점수/구조 부족")
    return {
        "ok": ok,
        "score": round(score, 2),
        "reasons": reasons[:10],
        "blocks": blocks[:10],
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
        "money_ok": money_ok,
        "money_status": row.get("money_status", "정보없음"),
        "move_ok": move_ok,
        "position_ok": position_ok,
        "evidence_count": evidence_count,
        "precision": precision,
    }


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
            "one_liner": " / ".join(prof["reasons"][:5]) if prof["ok"] else "차단: " + (" / ".join(prof["blocks"][:5]) if prof["blocks"] else "조건 부족"),
            "profile": prof,
        })
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
    return strict[:STRICT_LIMIT], shadow[:SHADOW_LIMIT], rejects, examples


def event_key(row: Dict[str, Any], lane: str, ts: Optional[float] = None) -> str:
    t = str(row.get("ticker") or "UNKNOWN").upper()
    bucket = int((ts or now_ts()) // max(60, EVENT_DEDUP_SEC))
    return f"clean:{lane}:{t}:{bucket}"


def consume_row(item: Dict[str, Any], lane: str, ts: Optional[float] = None) -> Dict[str, Any]:
    ts = ts or now_ts()
    eligible = lane == "strict"
    return {
        "schema": "candidate_consume_v153",
        "source": "brain_v153",
        "brain_version": BOT_VERSION,
        "created_at": ts,
        "created_at_text": now_text(ts),
        "event_id": event_key(item, lane, ts),
        "ticker": item.get("ticker"),
        "market": f"KRW-{item.get('ticker')}",
        "lane": lane,
        "eligible_for_paper": eligible,
        "paper_eligible": eligible,
        "decision": "paper_ready" if eligible else "shadow_review",
        "event_type": "paper_ready" if eligible else "single_strategy_shadow",
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
        "reason": item.get("one_liner", ""),
        "why": item.get("one_liner", ""),
        "candidate_events_disabled": True,
        "mainline_file": "paper_candidates" if eligible else "shadow_candidates",
        "pipeline_note": "v2.13.153: candidate_events 없음. paper/shadow만 paper_bot 소비파일.",
    }


def export_candidates(strict: List[Dict[str, Any]], shadow: List[Dict[str, Any]]) -> Dict[str, Any]:
    ts = now_ts()
    result = {
        "paper_attempt": len(strict),
        "shadow_attempt": len(shadow),
        "paper_written": 0,
        "shadow_written": 0,
        "dup_skip": 0,
        "write_error": "",
        "last_ticker": "-",
    }
    compact_candidate_file(FILES["paper"])
    compact_candidate_file(FILES["shadow"])
    for lane, items, path in [("strict", strict, FILES["paper"]), ("shadow", shadow, FILES["shadow"] )]:
        for item in items:
            row = consume_row(item, lane, ts)
            key = str(row.get("event_id"))
            last = fnum(_seen_events.get(key), 0)
            if ts - last < EVENT_DEDUP_SEC:
                result["dup_skip"] += 1
                continue
            _seen_events[key] = ts
            ok, err = append_jsonl(path, row)
            if ok:
                if lane == "strict":
                    result["paper_written"] += 1
                    _recent_strict.append(row)
                else:
                    result["shadow_written"] += 1
                    _recent_shadow.append(row)
                result["last_ticker"] = str(row.get("ticker") or "-")
            else:
                result["write_error"] = err
    return result


def scan_once() -> List[Dict[str, Any]]:
    started = now_ts()
    with _state_lock:
        STATE["scan_calls"] = int(STATE.get("scan_calls", 0)) + 1
        STATE["scan_last_stage"] = "bulk_fetch"
    stage_times: List[Tuple[str, float, str]] = []
    try:
        st = now_ts()
        rows, source = fetch_all_krw()
        stage_times.append(("1) 전체시장 bulk", now_ts() - st, f"rows {len(rows)} / {source}"))
        with _state_lock:
            STATE["bulk_rows"] = len(rows)
            STATE["bulk_price"] = sum(1 for r in rows if fnum(r.get("current_price"), 0) > 0)
            STATE["bulk_money"] = sum(1 for r in rows if fnum(r.get("turnover_24h"), 0) > 0)
            STATE["scan_last_stage"] = "precision_refresh"
        st = now_ts()
        p_ok, p_fail, p_have = refresh_precision(rows)
        stage_times.append(("2) 정밀값 갱신", now_ts() - st, f"refresh {p_ok} / fail {p_fail} / cached {p_have}"))
        with _state_lock:
            STATE["precision_refreshed"] = p_ok
            STATE["precision_failed"] = p_fail
            STATE["precision_have"] = p_have
            STATE["scan_last_stage"] = "standardize"
        st = now_ts()
        rows = merge_precision(rows)
        cov = update_field_coverage(rows)
        stage_times.append(("3) 표준값 생성", now_ts() - st, f"precision {cov.get('precision')} / bulk-only {cov.get('bulk_only')}"))
        with _state_lock:
            STATE["field_coverage"] = cov
            STATE["scan_last_stage"] = "strategy"
        st = now_ts()
        strict, shadow, rejects, examples = build_candidates(rows)
        stage_times.append(("4) 단일전략 판단", now_ts() - st, f"strict {len(strict)} / shadow {len(shadow)} / reject {sum(rejects.values())}"))
        with _state_lock:
            STATE["strict_decision"] = len(strict)
            STATE["shadow_decision"] = len(shadow)
            STATE["reject_counts"] = dict(rejects)
            STATE["reject_examples"] = examples
            STATE["last_rows_sample"] = [{"ticker": r.get("ticker"), "score": r.get("score"), "line": r.get("one_liner", "")[:100]} for r in strict[:5]]
            STATE["scan_last_stage"] = "file_export"
        st = now_ts()
        pipe = export_candidates(strict, shadow)
        stage_times.append(("5) paper/shadow 출력", now_ts() - st, f"paper {pipe['paper_written']}/{pipe['paper_attempt']} / shadow {pipe['shadow_written']}/{pipe['shadow_attempt']} / dup {pipe['dup_skip']}"))
        total = now_ts() - started
        with _state_lock:
            STATE["paper_written"] = pipe["paper_written"]
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
        # cache 파일은 짧게만 저장한다. 후보 장부가 아니다.
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


def fmt_stats(label: str, rows: Iterable[Dict[str, Any]]) -> str:
    s = score_stats(rows)
    icon = "✅" if s["avg"] > 0 else "❌"
    return f"{icon} {label}: {s['n']}전 {s['wins']}승 {s['losses']}패 / 승률 {s['win_rate']:.1f}% / 합산 {s['total']:+.2f}% / 평균 {s['avg']:+.2f}%"


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
        f"→ 정식paper {STATE.get('paper_written',0)} / 복기paper {STATE.get('shadow_written',0)} / "
        f"중복skip {STATE.get('dup_skip',0)} / candidate_events 없음 / 최근 {STATE.get('last_ticker','-')}{err_part}"
    )


def stage_lines() -> List[str]:
    rows = STATE.get("stage_times") if isinstance(STATE.get("stage_times"), list) else []
    out = ["🧩 v2.13.153 scan 단계표"]
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


def core_text() -> str:
    with _state_lock:
        s = dict(STATE)
    return "\n".join([
        "📌 종합 상태판 /core",
        "",
        "✅ 구조: 메인봇은 눈+뇌, paper_bot은 손+장부",
        f"- 버전: {BOT_VERSION}",
        f"- 전략: {STRATEGY_NAME} / 새 본선",
        f"- scan rows {s.get('strict_decision',0)} / 시장 bulk {s.get('bulk_rows',0)} / 가격 {s.get('bulk_price',0)} / 돈 {s.get('bulk_money',0)} / 정밀캐시 {s.get('precision_have',0)} / stage {s.get('scan_last_stage','-')}",
        field_line(),
        candidate_line(),
        "- candidate_events: 없음. 관찰/소비 fallback 모두 제거.",
        "- 실행분리: 메인봇=스캔·전략·후보파일 / paper_bot=모의매매 장부 / 내부paper 없음",
        paper_bot_line(),
        "- 알림: 메인봇 OFF / 모의 OPEN·CLOSED 알림은 paper_bot 전담",
        "",
        "판독",
        "- 전체 451개 안팎 시장 bulk 스캔은 유지한다.",
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
        "- v2.13.153 기준: 새 본선 / paper·shadow 소비파일만 출력",
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
        "2) 직원: 상위 유동성+급변+순환 정밀값 보강",
        "3) 직원: 표준값 생성",
        "4) 직원: 거래대금 눌림 재돌파 후보판단",
        "5) 공장: paper_candidates / shadow_candidates만 출력",
        "6) paper_bot: paper/shadow만 읽고 OPEN/CLOSED 저장",
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
        "- 메인봇 대상: 수익형_v2.13.153.py",
        "- 페이퍼봇 대상: paper_bot_v0.17_v153.py",
        "- 본선 후보파일: paper_candidates.jsonl / shadow_candidates.jsonl",
        "- 보조 관찰파일: 없음(candidate_events 미사용)",
        "",
        "v2.13.153 구조",
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
        "- 기대 paper_bot: paper_bot_v0.17_v153.py / paper_bot_v0.17_v153",
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

def command_score(update, context) -> None:
    rows = load_closed()
    strict = [r for r in rows if str(r.get("lane")) == "strict"]
    shadow = [r for r in rows if str(r.get("lane")) == "shadow"]
    strat = [r for r in rows if STRATEGY_NAME in str(r.get("strategy", "")) or STRATEGY_KEY in str(r.get("strategy", "")) or STRATEGY_KEY in str(r.get("route", ""))]
    deduped = dedupe_closed_rows(rows, bucket_min=10)
    by_reason = Counter(str(r.get("exit_reason") or "unknown") for r in rows)
    lines = [
        "📊 paper 성과판 /score",
        "",
        "✅ 기준: paper_bot_closed.jsonl 중심 / 종료된 포지션만 집계",
        "- 새 본선도 기존 장부를 삭제하지 않고 같은 CLOSED 파일을 읽는다.",
        fmt_stats("누적 전체", rows),
        "",
        "🎯 정식/복기 비교",
        fmt_stats("정식 후보(strict)", strict),
        fmt_stats("복기 후보(shadow)", shadow),
        "",
        "📊 전략별 성과",
        fmt_stats(STRATEGY_NAME, strat),
        "",
        "🧹 중복 보정 참고",
        fmt_stats("중복보정", deduped),
        f"- 제외된 중복 추정: {max(0, len(rows)-len(deduped))}건",
        "",
        "🚪 청산사유 건수",
    ]
    for k, v in by_reason.most_common(8):
        lines.append(f"- {k}: {v}건")
    lines += ["", paper_bot_line(), "", "판독", "- 기존 내부 paper 결과와 섞어서 보지 않는다.", "- 자동매매 판단은 새 본선 이후 신규 CLOSED가 충분히 쌓인 뒤에만 본다."]
    reply(update, "\n".join(lines))


def command_paper_today(update, context) -> None:
    rows = load_closed()
    today = datetime.now().strftime("%Y-%m-%d")
    today_rows = [r for r in rows if str(r.get("closed_at_text", "")).startswith(today)]
    lines = [
        "📌 오늘 paper·모의매매 /paper_today",
        "",
        paper_bot_line(),
        fmt_stats("오늘 종료", today_rows),
        "",
        f"- 후보파일 lines: 정식 {line_count(FILES['paper'])} / 복기 {line_count(FILES['shadow'])} / candidate_events 없음",
        "- 실행분리: 메인봇=스캔·전략·후보파일 / paper_bot=모의매매 장부",
        "",
        "판독",
        "- 오늘 성과가 0이면 아직 v2.13.153 신규 청산이 쌓이지 않은 것",
        "- 자동매매 판단은 신규 CLOSED가 충분히 쌓인 뒤에만 본다",
    ]
    reply(update, "\n".join(lines))


def command_batch(update, context) -> None:
    funcs = [
        ("core", core_text),
        ("score", lambda: "\n".join(["📊 paper 성과판 /score", fmt_stats("누적 전체", load_closed()), paper_bot_line()])),
        ("paper_today", lambda: "\n".join(["📌 오늘 paper·모의매매 /paper_today", paper_bot_line(), f"- 후보파일 lines: 정식 {line_count(FILES['paper'])} / 복기 {line_count(FILES['shadow'])} / candidate_events 없음"])),
        ("flow_check", lambda: "\n".join(["🧭 흐름검사 /flow_check", field_line(), candidate_line()] + stage_lines())),
        ("candidate_reason", lambda: "\n".join(["🔎 후보·전략 원인판 /candidate_reason", field_line(), candidate_line()] + reject_lines())),
        ("cpu_status", lambda: "\n".join(["🧠 CPU/메모리 /cpu_status", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / 최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s / stage {STATE.get('scan_last_stage','-')}"] + stage_lines())),
        ("paper_handoff", lambda: "\n".join(["🧪 paper handoff /paper_handoff", paper_bot_line(), f"- 파일: paper {line_count(FILES['paper'])} / shadow {line_count(FILES['shadow'])} / candidate_events 없음"])),
        ("deploy", lambda: "\n".join(["📦 배포 상태 /deploy", f"- 메인봇 실행버전: {BOT_VERSION}", "- 메인봇 대상: 수익형_v2.13.153.py", "- 페이퍼봇 대상: paper_bot_v0.17_v153.py"])),
    ]
    start = now_ts()
    reply(update, "\n".join(["📦 묶음 명령 접수", "- 출처: /batch", f"- 실행 {len(funcs)}개", "- v2.13.153: 기능흐름 일괄이식 새 본선", "- 메인봇=스캔·전략·paper/shadow 후보파일 / paper_bot=실행·장부·알림"]))
    rows = []
    for name, fn in funcs:
        st = now_ts()
        try:
            reply(update, fn())
            rows.append((name, now_ts() - st, "OK"))
        except Exception as exc:
            rows.append((name, now_ts() - st, f"ERR {exc.__class__.__name__}"))
            log_error(f"batch:{name}", exc)
    lines = ["🧾 v2.13.153 batch 요약", f"- 출력무결성: 실행 {len(funcs)}개", "", "⏱ 명령어별 시간표"]
    for n, sec, res in rows:
        lines.append(f"- /{n}: 처리 {sec:.2f}s / 결과 {res}")
    lines += ["", f"- 전체 경과: {now_ts()-start:.2f}s", f"- scan최근 {fnum(STATE.get('scan_last_sec'),0):.2f}s / scan최대 {fnum(STATE.get('scan_max_sec'),0):.2f}s", candidate_line(), field_line(), "- 알림: 메인봇 OFF / paper_bot만 모의매매 알림"]
    reply(update, "\n".join(lines))


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
    for name, fn in mapping.items():
        dp.add_handler(CommandHandler(name, fn))
    try:
        menu = ["batch", "core", "score", "paper_today", "flow_check", "candidate_reason", "cpu_status", "paper_handoff", "deploy", "trade", "errorlog", "help"]
        updater.bot.set_my_commands([BotCommand(k, k) for k in menu])
    except Exception:
        pass


def startup_checks() -> None:
    for p in [FILES["paper"], FILES["shadow"]]:
        ok, note = ensure_candidate_file(p)
        log(f"candidate_file {p.name}: {ok} {note}")
    # candidate_events는 일부러 만들지도 읽지도 않는다.
    save_json(FILES["status"], STATE)


def main() -> None:
    startup_checks()
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
    send_chunks("\n".join(["✅ 봇 시작 완료", f"현재 버전: {BOT_VERSION}", f"전략: {STRATEGY_NAME}", "구버전 tail/fallback/candidate_events/내부 paper 없는 새 본선", "확인: /batch /core /cpu_status /paper_handoff /deploy"]))
    updater.start_polling(drop_pending_updates=True)
    updater.idle()
    _stop_event.set()


if __name__ == "__main__":
    main()
