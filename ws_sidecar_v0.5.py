#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bithumb websocket sidecar for the clean coinbot line.

This process is intentionally separate from tradingbot.service.
It writes clean_ws_live_cache.json and clean_ws_sidecar_status.json.
The main bot only reads these files, so websocket hangs cannot block systemd
stop/restart for the main bot.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(os.getenv("TRADING_BOT_DIR", "/home/dangdang971216/trading_bot"))
CACHE_FILE = BASE_DIR / "clean_ws_live_cache.json"
STATUS_FILE = BASE_DIR / "clean_ws_sidecar_status.json"
LOG_FILE = BASE_DIR / "clean_ws_sidecar.log"
TARGET_FILE = BASE_DIR / "clean_ws_targets.json"

VERSION = "ws_sidecar_v0.5_notice_error_split_target_debounce_2026-05-15"
WS_URL = os.getenv("CLEAN_WS_HUB_URL", "wss://pubwss.bithumb.com/pub/ws").strip()
MAX_TICKERS = int(os.getenv("CLEAN_WS_HUB_MAX_TICKERS", "80"))
STALE_SEC = float(os.getenv("CLEAN_WS_HUB_STALE_SEC", "12"))
OPEN_TIMEOUT = float(os.getenv("CLEAN_WS_HUB_OPEN_TIMEOUT_SEC", "4.0"))
READ_TIMEOUT = float(os.getenv("CLEAN_WS_HUB_READ_TIMEOUT_SEC", "3.0"))
IDLE_RECONNECT_SEC = float(os.getenv("CLEAN_WS_HUB_IDLE_RECONNECT_SEC", "12.0"))
RESTART_WAIT_SEC = float(os.getenv("CLEAN_WS_HUB_RESTART_SEC", "8.0"))
TARGET_RELOAD_SEC = float(os.getenv("CLEAN_WS_TARGET_RELOAD_SEC", "1.2"))
TARGET_RECONNECT_WAIT_SEC = float(os.getenv("CLEAN_WS_TARGET_RECONNECT_WAIT_SEC", "0.2"))
STALE_ROW_DROP_SEC = float(os.getenv("CLEAN_WS_STALE_ROW_DROP_SEC", "60.0"))
TICK_TYPES = [x.strip() for x in os.getenv("CLEAN_WS_HUB_TICK_TYPES", "24H").split(",") if x.strip()]
MAJORS = ["BTC", "ETH", "XRP"]
STABLE_EXCLUDED = {"USDC", "USDT", "BUSD", "USDP", "DAI", "TUSD", "FDUSD", "USDS", "USD1", "PYUSD", "USDE", "RLUSD"}

STOP = False
ROWS: Dict[str, Dict[str, Any]] = {}
STATS: Dict[str, Any] = {
    "version": VERSION,
    "pid": os.getpid(),
    "state": "init",
    "targets": 0,
    "cached": 0,
    "fresh": 0,
    "raw_total": 0,
    "parse_ok": 0,
    "price_ok": 0,
    "amount_ok": 0,
    "match_ok": 0,
    "parse_empty": 0,
    "connect_ok": 0,
    "connect_fail": 0,
    "recv_timeout": 0,
    "last_error": "-",
    "last_notice": "-",
    "last_format": "-",
    "updated_ts": time.time(),
    "target_source": "init",
    "target_file_ts": 0.0,
    "target_reload_count": 0,
    "target_hash": "-",
    "stale_dropped": 0,
}


def now() -> float:
    return time.time()


def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def atomic_write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _is_reconnect_notice(text: Any) -> bool:
    s = str(text or "").lower()
    return "target file changed" in s or "reconnect" in s or "재구독" in s or "대상변경" in s


def target_hash(symbols: List[str]) -> str:
    try:
        return str(hash(tuple(sorted(symbols))))
    except Exception:
        return "?"


def prune_rows(target_set: set[str] | None = None) -> int:
    """오래된 WS row를 fresh처럼 들고 있지 않게 정리한다."""
    ts = now()
    target_set = target_set or set()
    dropped = 0
    for t in list(ROWS.keys()):
        row = ROWS.get(t) or {}
        age = ts - float(row.get("ts", 0) or 0)
        if age > STALE_ROW_DROP_SEC or (target_set and t not in target_set and age > STALE_SEC):
            ROWS.pop(t, None)
            dropped += 1
    if dropped:
        STATS["stale_dropped"] = int(STATS.get("stale_dropped", 0)) + dropped
    return dropped

def write_status(state: str | None = None, error: str | None = None, notice: str | None = None) -> None:
    if state is not None:
        STATS["state"] = state
    if error is not None:
        if _is_reconnect_notice(error):
            STATS["last_notice"] = str(error)[:240]
            STATS["last_error"] = "-"
        else:
            STATS["last_error"] = str(error)[:240]
    if notice is not None:
        STATS["last_notice"] = str(notice)[:240]
        if _is_reconnect_notice(notice) and _is_reconnect_notice(STATS.get("last_error", "")):
            STATS["last_error"] = "-"
    ts = now()
    fresh = sum(1 for r in ROWS.values() if ts - float(r.get("ts", 0) or 0) <= STALE_SEC)
    STATS.update({
        "pid": os.getpid(),
        "version": VERSION,
        "updated_ts": ts,
        "cached": len(ROWS),
        "fresh": fresh,
    })
    try:
        atomic_write(STATUS_FILE, STATS)
    except Exception:
        pass


def write_cache() -> None:
    try:
        prune_rows()
        atomic_write(CACHE_FILE, {
            "version": VERSION,
            "updated_ts": now(),
            "rows": ROWS,
            "stats": STATS,
        })
    except Exception as exc:
        log(f"cache write error: {exc}")


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, str):
            v = v.replace(",", "").strip()
            if not v:
                return default
        return float(v)
    except Exception:
        return default


def first_number(row: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    for k in keys:
        if isinstance(row, dict) and k in row:
            v = fnum(row.get(k), default)
            if v != default or str(row.get(k, "")).strip() not in {"", "0", "0.0"}:
                return v
    return default


def to_ticker(symbol: Any) -> str:
    s = str(symbol or "").upper().strip().replace('"', '').replace("'", "").replace(" ", "")
    if not s:
        return ""
    if s.startswith("KRW-"):
        s = s.split("-", 1)[1]
    elif s.startswith("KRW_"):
        s = s.split("_", 1)[1]
    elif s.endswith("_KRW"):
        s = s[:-4]
    elif s.endswith("/KRW"):
        s = s[:-4]
    elif "/" in s:
        parts = [x for x in s.split("/") if x]
        if len(parts) == 2:
            s = parts[0] if parts[1] == "KRW" else parts[-1]
    if s.startswith("KRW") and len(s) > 3 and s[3] not in {"-", "_", "/"}:
        s = s[3:]
    return s.strip("-_/")


def ws_symbol(ticker: str) -> str:
    t = to_ticker(ticker)
    return f"{t}_KRW" if t and t not in STABLE_EXCLUDED else ""


def symbol_format(symbol: Any) -> str:
    s = str(symbol or "").upper().strip()
    if s.startswith("KRW-"):
        return "KRW-BTC"
    if s.endswith("_KRW"):
        return "BTC_KRW"
    if s.endswith("/KRW"):
        return "BTC/KRW"
    return "plain" if s else "unknown"


def payload_rows(data: Any) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", errors="ignore")
        if isinstance(data, str):
            data = json.loads(data)
    except Exception:
        return []
    if isinstance(data, list):
        out: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for x in data:
            out.extend(payload_rows(x))
        return out
    if not isinstance(data, dict):
        return []
    wrapper = data
    content = data.get("content") if isinstance(data.get("content"), (dict, list)) else None
    if isinstance(content, list):
        return [(r, wrapper) for r in content if isinstance(r, dict)]
    if isinstance(content, dict):
        for key in ("list", "data", "ticks", "items"):
            if isinstance(content.get(key), list):
                return [(r, wrapper) for r in content.get(key) if isinstance(r, dict)]
        return [(content, wrapper)]
    for key in ("data", "list", "ticks", "items"):
        val = data.get(key)
        if isinstance(val, list):
            return [(r, wrapper) for r in val if isinstance(r, dict)]
        if isinstance(val, dict):
            return [(val, wrapper)]
    return [(data, wrapper)]


def parse_ws(data: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row, wrapper in payload_rows(data):
        typ = str(wrapper.get("type") or row.get("type") or row.get("ty") or "ticker").lower()
        if typ and typ not in {"ticker", "transaction", "trade", "ticker_lite"}:
            continue
        code = None
        for source in (row, wrapper):
            for k in ("symbol", "code", "market", "cd", "ticker", "symbolTicker", "s", "pair"):
                if isinstance(source, dict) and source.get(k):
                    code = source.get(k)
                    break
            if code:
                break
        ticker = to_ticker(code)
        price = first_number(row, ["closePrice", "close_price", "trade_price", "price", "closing_price", "current_price", "tp", "close", "c", "last", "contPrice"])
        if price <= 0:
            price = first_number(wrapper, ["closePrice", "close_price", "trade_price", "price", "tp", "close", "c", "last"])
        turnover = first_number(row, ["value", "trade_value", "tradeValue", "acc_trade_price", "acc_trade_price_24h", "turnover", "amount", "contAmt", "total", "volumePower"])
        volume = first_number(row, ["quantity", "qty", "volume", "trade_volume", "units", "contQty", "acc_trade_volume", "acc_trade_volume_24h"])
        if not ticker or price <= 0:
            continue
        out.append({
            "ticker": ticker,
            "symbol": str(code or ""),
            "live_price": price,
            "ws_turnover": turnover,
            "ws_volume": volume,
            "ts": now(),
            "symbol_format": symbol_format(code),
            "raw_keys": ",".join(list(row.keys())[:8]),
            "change_rate": first_number(row, ["chgRate", "change_rate", "signed_change_rate", "rate"], 0.0),
            "volume_power": first_number(row, ["volumePower", "volume_power"], 0.0),
        })
    return out


def target_file_mtime() -> float:
    try:
        return TARGET_FILE.stat().st_mtime if TARGET_FILE.exists() else 0.0
    except Exception:
        return 0.0


def load_targets_from_file() -> List[str]:
    try:
        if TARGET_FILE.exists():
            obj = json.loads(TARGET_FILE.read_text(encoding="utf-8", errors="ignore"))
            raw = obj.get("targets") if isinstance(obj, dict) else obj
            if isinstance(obj, dict):
                STATS["target_file_ts"] = float(obj.get("updated_ts") or target_file_mtime() or 0.0)
                STATS["target_source"] = str(obj.get("reason") or "clean_ws_targets.json")[:80]
            if isinstance(raw, list):
                return [to_ticker(x) for x in raw if to_ticker(x)]
    except Exception as exc:
        STATS["last_error"] = f"target file {exc.__class__.__name__}: {str(exc)[:120]}"
        log(STATS["last_error"])
    return []


def fetch_targets_from_rest() -> List[str]:
    url = "https://api.bithumb.com/public/ticker/ALL_KRW"
    req = urllib.request.Request(url, headers={"User-Agent": "coinbot-ws-sidecar/0.1"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    data = payload.get("data") if isinstance(payload, dict) else {}
    rows = []
    if isinstance(data, dict):
        for k, v in data.items():
            t = to_ticker(k)
            if not t or t in STABLE_EXCLUDED:
                continue
            value = 0.0
            if isinstance(v, dict):
                value = first_number(v, ["acc_trade_value_24H", "acc_trade_value_24h", "acc_trade_value", "trade_value", "amount", "value"])
            rows.append((value, t))
    rows.sort(reverse=True)
    ticks = []
    seen = set()
    for t in MAJORS + [t for _, t in rows]:
        if t and t not in seen:
            seen.add(t)
            ticks.append(t)
        if len(ticks) >= MAX_TICKERS:
            break
    return ticks


def get_targets() -> List[str]:
    targets = load_targets_from_file()
    if not targets:
        try:
            targets = fetch_targets_from_rest()
            STATS["target_source"] = "REST_TOP"
        except Exception as exc:
            STATS["last_error"] = f"target fetch {exc.__class__.__name__}: {str(exc)[:120]}"
            log(STATS["last_error"])
            targets = MAJORS[:]
            STATS["target_source"] = "MAJORS_FALLBACK"
    targets = [t for t in targets if t and t not in STABLE_EXCLUDED]
    return list(dict.fromkeys(targets))[:MAX_TICKERS]


def update_rows(parsed: List[Dict[str, Any]], target_set: set[str]) -> int:
    applied = 0
    prune_rows(target_set)
    for row in parsed:
        t = to_ticker(row.get("ticker"))
        if not t:
            continue
        row["ticker"] = t
        ROWS[t] = row
        applied += 1
    STATS["parse_ok"] = int(STATS.get("parse_ok", 0)) + len(parsed)
    STATS["price_ok"] = int(STATS.get("price_ok", 0)) + sum(1 for r in parsed if fnum(r.get("live_price")) > 0)
    STATS["amount_ok"] = int(STATS.get("amount_ok", 0)) + sum(1 for r in parsed if fnum(r.get("ws_turnover")) > 0 or fnum(r.get("ws_volume")) > 0)
    STATS["match_ok"] = int(STATS.get("match_ok", 0)) + sum(1 for r in parsed if to_ticker(r.get("ticker")) in target_set)
    if parsed:
        STATS["last_format"] = str(parsed[-1].get("symbol_format") or "-")
    else:
        STATS["parse_empty"] = int(STATS.get("parse_empty", 0)) + 1
    return applied


def handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
    global STOP
    STOP = True
    write_status("종료요청", f"signal {signum}")


async def run_session(symbols: List[str], target_set: set[str]) -> str:
    import websockets  # imported inside sidecar only
    sub = {"type": "ticker", "symbols": symbols, "tickTypes": TICK_TYPES or ["24H"]}
    write_status("연결중", "-")
    async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=5, open_timeout=OPEN_TIMEOUT, close_timeout=1, max_queue=64) as ws:
        await asyncio.wait_for(ws.send(json.dumps(sub)), timeout=2.0)
        STATS["connect_ok"] = int(STATS.get("connect_ok", 0)) + 1
        STATS["targets"] = len(symbols)
        STATS["target_hash"] = target_hash(symbols)
        write_status("연결", "-")
        log(f"subscribed {len(symbols)} targets")
        last_data = now()
        last_write = 0.0
        last_target_check = now()
        last_target_mtime = target_file_mtime()
        while not STOP:
            if now() - last_target_check >= TARGET_RELOAD_SEC:
                last_target_check = now()
                cur_mtime = target_file_mtime()
                if cur_mtime and cur_mtime != last_target_mtime:
                    new_targets = get_targets()
                    new_symbols = [ws_symbol(t) for t in new_targets if ws_symbol(t)]
                    if set(new_symbols) != set(symbols):
                        STATS["target_reload_count"] = int(STATS.get("target_reload_count", 0)) + 1
                        write_status("재구독중", "-", "target file changed; fast reconnect")
                        log(f"target changed {len(symbols)} -> {len(new_symbols)}; fast reconnect")
                        return "target_changed"
                    last_target_mtime = cur_mtime
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=READ_TIMEOUT)
            except asyncio.TimeoutError:
                STATS["recv_timeout"] = int(STATS.get("recv_timeout", 0)) + 1
                write_status("수신대기", "-", "recv timeout")
                if now() - last_data >= IDLE_RECONNECT_SEC:
                    return "idle_reconnect"
                continue
            STATS["raw_total"] = int(STATS.get("raw_total", 0)) + 1
            parsed = parse_ws(raw)
            applied = update_rows(parsed, target_set)
            if applied:
                last_data = now()
                write_status("수신중", "-")
            if now() - last_write >= 1.0:
                write_cache()
                write_status()
                last_write = now()

    return "stopped"


async def main_loop() -> None:
    while not STOP:
        targets = get_targets()
        symbols = [ws_symbol(t) for t in targets if ws_symbol(t)]
        target_set = set(targets)
        STATS["targets"] = len(symbols)
        write_status("대상준비", "-")
        if not symbols:
            await asyncio.sleep(2)
            continue
        reason = "normal"
        try:
            reason = await run_session(symbols, target_set)
        except Exception as exc:
            STATS["connect_fail"] = int(STATS.get("connect_fail", 0)) + 1
            err = f"{exc.__class__.__name__}: {str(exc)[:160]}"
            write_status("재연결대기", err)
            log("websocket error: " + err)
            try:
                log(traceback.format_exc())
            except Exception:
                pass
            await asyncio.sleep(min(max(1.0, RESTART_WAIT_SEC), 20.0))
            continue
        if reason == "target_changed":
            await asyncio.sleep(max(0.05, min(TARGET_RECONNECT_WAIT_SEC, 1.0)))
        else:
            await asyncio.sleep(min(max(1.0, RESTART_WAIT_SEC), 20.0))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    log(f"{VERSION} started pid={os.getpid()}")
    write_status("시작", "-")
    try:
        asyncio.run(main_loop())
    finally:
        write_cache()
        write_status("종료", "-", "normal stop")
        log(f"{VERSION} stopped")
