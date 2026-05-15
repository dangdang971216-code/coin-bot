#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bithumb microstructure sidecar for coinbot.

Separate process. It polls Bithumb public orderbook and recent transaction
history for the tickers selected by the main bot. The main bot only reads the
cache files, so REST/API stalls here cannot block tradingbot.service.
"""
from __future__ import annotations

import json
import os
import signal
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(os.getenv("TRADING_BOT_DIR", "/home/dangdang971216/trading_bot"))
TARGET_FILE = BASE_DIR / "clean_micro_targets.json"
WS_TARGET_FILE = BASE_DIR / "clean_ws_targets.json"
CACHE_FILE = BASE_DIR / "clean_bithumb_micro_cache.json"
STATUS_FILE = BASE_DIR / "clean_bithumb_micro_status.json"
LOG_FILE = BASE_DIR / "clean_bithumb_micro.log"
PID_FILE = BASE_DIR / "clean_bithumb_micro.pid"

VERSION = "bithumb_micro_sidecar_v0.4_fast_priority_poll_2026-05-15"
MAX_TICKERS = int(os.getenv("CLEAN_MICRO_MAX_TICKERS", "32"))
POLL_SEC = float(os.getenv("CLEAN_MICRO_POLL_SEC", "3.0"))
REQUEST_TIMEOUT = float(os.getenv("CLEAN_MICRO_REQUEST_TIMEOUT_SEC", "2.0"))
STALE_SEC = float(os.getenv("CLEAN_MICRO_STALE_SEC", "20"))
TARGET_RELOAD_SEC = float(os.getenv("CLEAN_MICRO_TARGET_RELOAD_SEC", "1.5"))
MAX_WORKERS = int(os.getenv("CLEAN_MICRO_WORKERS", "6"))
USER_AGENT = "coinbot-bithumb-micro-sidecar/0.4"
STABLE_EXCLUDED = {"USDC", "USDT", "BUSD", "USDP", "DAI", "TUSD", "FDUSD", "USDS", "USD1", "PYUSD", "USDE", "RLUSD"}
MAJORS = ["BTC", "ETH", "XRP"]

STOP = False
ROTATE = 0
ROWS: Dict[str, Dict[str, Any]] = {}
STATS: Dict[str, Any] = {
    "version": VERSION,
    "pid": os.getpid(),
    "state": "init",
    "targets": 0,
    "cached": 0,
    "fresh": 0,
    "orderbook_ok": 0,
    "orderbook_fail": 0,
    "trade_ok": 0,
    "trade_fail": 0,
    "poll_count": 0,
    "last_error": "-",
    "updated_ts": time.time(),
    "target_source": "init",
    "target_file_ts": 0.0,
    "target_reload_count": 0,
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
    return s.strip("-_/ ")


def http_json(url: str, timeout: float = REQUEST_TIMEOUT) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def load_targets_from(path: Path) -> Tuple[List[str], str, float]:
    try:
        if not path.exists():
            return [], "missing", 0.0
        obj = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        raw = obj.get("targets") if isinstance(obj, dict) else obj
        reason = str(obj.get("reason") or path.name) if isinstance(obj, dict) else path.name
        ts = float(obj.get("updated_ts") or path.stat().st_mtime) if isinstance(obj, dict) else path.stat().st_mtime
        out: List[str] = []
        seen = set()
        if isinstance(raw, list):
            for x in raw:
                t = to_ticker(x)
                if t and t not in STABLE_EXCLUDED and t not in seen:
                    seen.add(t)
                    out.append(t)
        return out, reason, ts
    except Exception as exc:
        STATS["last_error"] = f"target {path.name} {exc.__class__.__name__}: {str(exc)[:120]}"
        log(STATS["last_error"])
        return [], "error", 0.0


def load_targets() -> List[str]:
    targets, reason, ts = load_targets_from(TARGET_FILE)
    if not targets:
        targets, reason, ts = load_targets_from(WS_TARGET_FILE)
        if targets:
            reason = "ws_targets_fallback:" + reason
    if not targets:
        targets = MAJORS[:]
        reason = "majors_fallback"
        ts = now()
    final: List[str] = []
    seen = set()
    for t in targets:
        t = to_ticker(t)
        if t and t not in STABLE_EXCLUDED and t not in seen:
            final.append(t)
            seen.add(t)
        if len(final) >= MAX_TICKERS:
            break
    STATS["target_source"] = reason[:120]
    STATS["target_file_ts"] = ts
    STATS["targets"] = len(final)
    return final


def parse_orderbook(ticker: str) -> Dict[str, Any]:
    urls = [
        f"https://api.bithumb.com/public/orderbook/{urllib.parse.quote(ticker)}_KRW?count=5",
        f"https://api.bithumb.com/public/orderbook/{urllib.parse.quote(ticker)}/KRW?count=5",
    ]
    last_err = ""
    for url in urls:
        try:
            data = http_json(url)
            if not isinstance(data, dict) or str(data.get("status")) != "0000":
                last_err = f"status={data.get('status') if isinstance(data, dict) else '?'}"
                continue
            book = data.get("data") or {}
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                last_err = "empty book"
                continue
            bid1 = fnum((bids[0] or {}).get("price"), 0)
            ask1 = fnum((asks[0] or {}).get("price"), 0)
            mid = (bid1 + ask1) / 2 if bid1 > 0 and ask1 > 0 else 0.0
            spread = ((ask1 - bid1) / mid) * 100 if mid > 0 and ask1 >= bid1 else 999.0
            bid_wall = 0.0
            ask_wall = 0.0
            for r in bids[:5]:
                p = fnum((r or {}).get("price"), 0)
                q = fnum((r or {}).get("quantity") or (r or {}).get("qty"), 0)
                bid_wall += p * q
            for r in asks[:5]:
                p = fnum((r or {}).get("price"), 0)
                q = fnum((r or {}).get("quantity") or (r or {}).get("qty"), 0)
                ask_wall += p * q
            ratio = bid_wall / ask_wall if ask_wall > 0 else (9.99 if bid_wall > 0 else 0.0)
            return {
                "orderbook_ok": True,
                "best_bid": bid1,
                "best_ask": ask1,
                "micro_mid_price": mid,
                "micro_spread_pct": round(spread, 4) if spread < 900 else 999.0,
                "bid_wall_5_krw": round(bid_wall, 2),
                "ask_wall_5_krw": round(ask_wall, 2),
                "bid_ask_wall_ratio": round(ratio, 4),
                "ask_wall_pressure": bool(ask_wall > bid_wall * 1.5 and ask_wall > 3_000_000),
            }
        except Exception as exc:
            last_err = f"{exc.__class__.__name__}: {str(exc)[:100]}"
    return {"orderbook_ok": False, "orderbook_error": last_err or "unknown"}


def _trade_side(row: Dict[str, Any]) -> str:
    raw = str(row.get("type") or row.get("buySellGb") or row.get("ask_bid") or row.get("side") or "").lower()
    # Bithumb transaction_history has used bid/ask labels in multiple wrappers.
    if "bid" in raw or "buy" in raw or raw in {"1", "b"}:
        return "buy"
    if "ask" in raw or "sell" in raw or raw in {"2", "s"}:
        return "sell"
    return "unknown"


def parse_trades(ticker: str) -> Dict[str, Any]:
    urls = [
        f"https://api.bithumb.com/public/transaction_history/{urllib.parse.quote(ticker)}_KRW?count=30",
        f"https://api.bithumb.com/public/transaction_history/{urllib.parse.quote(ticker)}/KRW?count=30",
    ]
    last_err = ""
    for url in urls:
        try:
            data = http_json(url)
            if not isinstance(data, dict) or str(data.get("status")) != "0000":
                last_err = f"status={data.get('status') if isinstance(data, dict) else '?'}"
                continue
            arr = data.get("data") or []
            if isinstance(arr, dict):
                arr = arr.get("list") or arr.get("data") or []
            if not isinstance(arr, list):
                last_err = "bad data"
                continue
            buy = sell = unk = 0.0
            cnt = 0
            last_side = "unknown"
            for row in arr[:30]:
                if not isinstance(row, dict):
                    continue
                price = fnum(row.get("price") or row.get("contPrice") or row.get("trade_price"), 0)
                qty = fnum(row.get("units_traded") or row.get("quantity") or row.get("contQty") or row.get("qty"), 0)
                total = fnum(row.get("total") or row.get("contAmt") or row.get("value"), price * qty if price > 0 and qty > 0 else 0)
                side = _trade_side(row)
                if side == "buy":
                    buy += total
                elif side == "sell":
                    sell += total
                else:
                    unk += total
                if side != "unknown":
                    last_side = side
                cnt += 1
            denom = buy + sell
            buy_ratio = buy / denom if denom > 0 else 0.0
            return {
                "trade_ok": True,
                "trade_buy_krw_30": round(buy, 2),
                "trade_sell_krw_30": round(sell, 2),
                "trade_unknown_krw_30": round(unk, 2),
                "trade_buy_ratio_30": round(buy_ratio, 4),
                "trade_count_30": cnt,
                "last_trade_side": last_side,
                "sell_trade_pressure": bool(denom > 0 and buy_ratio < 0.42 and sell > buy * 1.25),
            }
        except Exception as exc:
            last_err = f"{exc.__class__.__name__}: {str(exc)[:100]}"
    return {"trade_ok": False, "trade_error": last_err or "unknown"}


def poll_one(ticker: str) -> Dict[str, Any]:
    t = to_ticker(ticker)
    ts = now()
    row: Dict[str, Any] = {"ticker": t, "ts": ts, "updated_ts": ts}
    ob = parse_orderbook(t)
    tr = parse_trades(t)
    row.update(ob)
    row.update(tr)
    flags: List[str] = []
    if not ob.get("orderbook_ok"):
        flags.append("호가정보없음")
    if not tr.get("trade_ok"):
        flags.append("체결정보없음")
    if fnum(row.get("micro_spread_pct"), 999) >= 0.45 and fnum(row.get("micro_spread_pct"), 999) < 900:
        flags.append("스프레드주의")
    if bool(row.get("ask_wall_pressure")):
        flags.append("매도벽두꺼움")
    if bool(row.get("sell_trade_pressure")):
        flags.append("매도체결우세")
    if fnum(row.get("trade_buy_ratio_30"), 0) >= 0.58:
        flags.append("매수체결우세")
    if not flags:
        flags.append("미세구조보통")
    row["micro_flags"] = flags[:6]
    if ob.get("orderbook_ok"):
        STATS["orderbook_ok"] = int(STATS.get("orderbook_ok", 0)) + 1
    else:
        STATS["orderbook_fail"] = int(STATS.get("orderbook_fail", 0)) + 1
    if tr.get("trade_ok"):
        STATS["trade_ok"] = int(STATS.get("trade_ok", 0)) + 1
    else:
        STATS["trade_fail"] = int(STATS.get("trade_fail", 0)) + 1
    return row


def write_status(state: str | None = None, error: str | None = None) -> None:
    if state is not None:
        STATS["state"] = state
    if error is not None:
        STATS["last_error"] = error[:240]
    ts = now()
    fresh = sum(1 for r in ROWS.values() if ts - fnum(r.get("ts"), 0) <= STALE_SEC)
    STATS.update({"pid": os.getpid(), "updated_ts": ts, "cached": len(ROWS), "fresh": fresh})
    try:
        atomic_write(STATUS_FILE, STATS)
    except Exception:
        pass


def write_cache() -> None:
    try:
        atomic_write(CACHE_FILE, {"version": VERSION, "updated_ts": now(), "rows": ROWS, "stats": STATS})
    except Exception as exc:
        log(f"cache write error: {exc}")


def handle_signal(signum, frame) -> None:  # type: ignore[no-untyped-def]
    global STOP
    STOP = True
    write_status("종료요청", f"signal {signum}")


def poll_loop() -> None:
    global ROTATE
    targets = load_targets()
    last_target_load = 0.0
    write_status("시작", "-")
    log(f"{VERSION} started pid={os.getpid()}")
    while not STOP:
        try:
            if now() - last_target_load >= TARGET_RELOAD_SEC:
                targets = load_targets()
                last_target_load = now()
                STATS["target_reload_count"] = int(STATS.get("target_reload_count", 0)) + 1
            if not targets:
                write_status("대상없음", "targets empty")
                time.sleep(POLL_SEC)
                continue
            # rotate but prioritize current target list order.
            work = targets[:MAX_TICKERS]
            if len(work) > 10:
                # process all targets but keep parallelism controlled.
                batch = work
            else:
                batch = work
            write_status("수집중", "-")
            with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(batch)))) as ex:
                futs = {ex.submit(poll_one, t): t for t in batch}
                for fut in as_completed(futs):
                    try:
                        row = fut.result()
                        t = to_ticker(row.get("ticker"))
                        if t:
                            ROWS[t] = row
                    except Exception as exc:
                        STATS["last_error"] = f"poll {futs.get(fut)} {exc.__class__.__name__}: {str(exc)[:100]}"
                        log(STATS["last_error"])
            STATS["poll_count"] = int(STATS.get("poll_count", 0)) + 1
            write_cache()
            write_status("수집중", "-")
        except Exception as exc:
            err = f"{exc.__class__.__name__}: {str(exc)[:160]}"
            write_status("재시도대기", err)
            log("loop error: " + err)
            try:
                log(traceback.format_exc())
            except Exception:
                pass
        time.sleep(max(1.0, POLL_SEC))
    write_cache()
    write_status("종료", "-")
    log(f"{VERSION} stopped")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    poll_loop()
