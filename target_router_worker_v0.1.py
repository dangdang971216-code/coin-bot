#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""target_router_worker_v0.1

정보보강 배차 직원.
- 후보 개수를 고정으로 자르지 않는다.
- strategy worker가 만든 정보요청을 우선순위로 정렬한다.
- WS/micro target 파일을 같은 우선순위 큐로 갱신한다.
- 서버 보호는 소비 worker/sidecar의 rate limit로 처리하고, 여기서는 후보를 버리지 않는다.
"""
from __future__ import annotations
import json, os, time, traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(os.getenv("TRADING_BOT_DIR", "/home/dangdang971216/trading_bot"))
VERSION = "target_router_worker_v0.1"
INTERVAL_SEC = float(os.getenv("TARGET_ROUTER_INTERVAL_SEC", "1.5"))
STATUS = BASE_DIR / "clean_target_router_status.json"
ERROR = BASE_DIR / "clean_target_router_error.log"
STRATEGY_REQ = BASE_DIR / "clean_strategy_priority_requests.json"
STRATEGY_SUMMARY = BASE_DIR / "clean_strategy_s_summary.json"
FEATURE = BASE_DIR / "clean_feature_cache.json"
ORDERFLOW = BASE_DIR / "clean_orderflow_summary_cache.json"
PAPER_OPEN = BASE_DIR / "paper_bot_open.json"
QUEUE = BASE_DIR / "clean_target_router_queue.json"
WS_TARGETS = BASE_DIR / "clean_ws_targets.json"
MICRO_TARGETS = BASE_DIR / "clean_micro_targets.json"
MICRO_URGENT = BASE_DIR / "clean_micro_urgent_targets.json"


def now_ts() -> float:
    return time.time()

def now_text(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or now_ts()))

def fnum(*vals: Any, default: float = 0.0) -> float:
    for v in vals:
        try:
            if v is None or v == "":
                continue
            return float(str(v).replace(",", "").replace("%", ""))
        except Exception:
            continue
    return default

def ticker_norm(x: Any) -> str:
    t = str(x or "").strip().upper().replace("/", "-").replace("_", "-")
    parts = [p for p in t.split("-") if p]
    if len(parts) >= 2:
        non = [p for p in parts if p != "KRW"]
        t = non[-1] if non else parts[-1]
    if t.endswith("KRW") and len(t) > 3:
        t = t[:-3]
    return t.strip().upper()

def load_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default

def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, path)

def append_error(where: str, exc: BaseException) -> None:
    try:
        ERROR.parent.mkdir(parents=True, exist_ok=True)
        with ERROR.open("a", encoding="utf-8") as f:
            f.write(f"[{now_text()}] {where}: {exc.__class__.__name__}: {exc}\n")
            f.write(traceback.format_exc()[-1600:] + "\n")
    except Exception:
        pass

def write_status(state: str, **extra: Any) -> None:
    save_json(STATUS, {"version": VERSION, "state": state, "pid": os.getpid(), "updated_ts": now_ts(), "updated_text": now_text(), **extra})

def map_rows(obj: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
        obj = obj.get("rows")
    elif isinstance(obj, dict) and isinstance(obj.get("rows"), dict):
        obj = obj.get("rows")
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(obj, list):
        for r in obj:
            if isinstance(r, dict):
                t = ticker_norm(r.get("ticker") or r.get("symbol") or r.get("market"))
                if t:
                    out[t] = r
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                t = ticker_norm(k)
                vv = dict(v); vv.setdefault("ticker", t)
                out[t] = vv
    return out

def _open_tickers() -> List[str]:
    obj = load_json(PAPER_OPEN, {}) or {}
    out: List[str] = []
    if isinstance(obj, dict):
        values = obj.values() if not isinstance(obj.get("positions"), list) else obj.get("positions", [])
        for r in values:
            if isinstance(r, dict):
                t = ticker_norm(r.get("ticker") or r.get("market") or r.get("symbol"))
                if t:
                    out.append(t)
    return out

def _request_rows() -> List[Dict[str, Any]]:
    obj = load_json(STRATEGY_REQ, {}) or {}
    rows = obj.get("requests") if isinstance(obj, dict) else []
    return rows if isinstance(rows, list) else []

def _base_feature_priority(features: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    # 전체시장을 버리지 않고 낮은 우선순위 순환 대상으로 둔다.
    out = []
    for t, r in features.items():
        score = 10.0
        score += min(20.0, max(0.0, fnum(r.get("change_3m"), default=0.0) * 4))
        score += min(20.0, max(0.0, fnum(r.get("change_15m"), default=0.0) * 2))
        score += min(15.0, max(0.0, fnum(r.get("turnover_24h"), default=0.0) / 500_000_000))
        if r.get("volume_expanding"):
            score += 8
        if r.get("breakout_hint") or r.get("sweep_reclaim_hint"):
            score += 8
        out.append({"ticker": t, "priority": round(score, 3), "bucket": "normal_rotation", "need": [], "source": "feature_rotation"})
    return out

def build_queue() -> Dict[str, Any]:
    ts = now_ts()
    features = map_rows(load_json(FEATURE, {}) or {})
    orderflow = map_rows(load_json(ORDERFLOW, {}) or {})
    reqs = _request_rows()
    merged: Dict[str, Dict[str, Any]] = {}

    def add(row: Dict[str, Any], bonus: float = 0.0) -> None:
        t = ticker_norm(row.get("ticker") or row.get("market") or row.get("symbol"))
        if not t:
            return
        cur = dict(row)
        cur["ticker"] = t
        cur["priority"] = fnum(cur.get("priority"), default=0.0) + bonus
        of = orderflow.get(t, {})
        cur["ws_fresh"] = bool(of.get("ws_fresh"))
        cur["micro_fresh"] = bool(of.get("micro_fresh"))
        cur["fresh_ready"] = bool(cur["ws_fresh"] and cur["micro_fresh"])
        cur["updated_ts"] = ts
        old = merged.get(t)
        if old is None or fnum(cur.get("priority"), default=0) > fnum(old.get("priority"), default=0):
            merged[t] = cur

    # 1순위: 이미 OPEN인 코인은 감시 우선.
    for t in _open_tickers():
        add({"ticker": t, "priority": 130, "bucket": "open_position", "need": ["ws", "micro"], "source": "paper_open"})

    # 2순위: strategy가 요청한 S/S근접/정보부족 후보. 개수 제한 없이 전부 넣고 우선순위로만 정렬.
    for r in reqs:
        bucket = str(r.get("bucket") or "strategy_request")
        base = fnum(r.get("priority"), default=50.0)
        if bucket in {"s_candidate", "paper_handoff"}:
            base += 40
        elif bucket in {"near_s_info_wait", "s_near_info"}:
            base += 25
        elif bucket in {"info_wait_high", "high_score_info_wait"}:
            base += 10
        add({**r, "priority": base, "source": r.get("source") or "strategy"})

    # 3순위: 전체시장 순환. 낮은 우선순위로 전부 유지한다.
    for r in _base_feature_priority(features):
        add(r)

    rows = sorted(merged.values(), key=lambda x: (fnum(x.get("priority"), default=0.0), bool(x.get("fresh_ready"))), reverse=True)
    buckets: Dict[str, int] = {}
    fresh_recovered = 0
    need_ws = 0
    need_micro = 0
    for r in rows:
        b = str(r.get("bucket") or "unknown")
        buckets[b] = buckets.get(b, 0) + 1
        if r.get("fresh_ready"):
            fresh_recovered += 1
        need = r.get("need") if isinstance(r.get("need"), list) else []
        if "ws" in need and not r.get("ws_fresh"):
            need_ws += 1
        if "micro" in need and not r.get("micro_fresh"):
            need_micro += 1

    targets = [r.get("ticker") for r in rows if r.get("ticker")]
    payload = {
        "version": VERSION,
        "updated_ts": ts,
        "updated_text": now_text(ts),
        "target_count": len(targets),
        "targets": targets,
        "rows": rows[:500],
        "buckets": buckets,
        "fresh_recovered": fresh_recovered,
        "need_ws": need_ws,
        "need_micro": need_micro,
        "near_s_count": buckets.get("near_s_info_wait", 0) + buckets.get("s_near_info", 0),
        "note": "고정 개수 제한이 아니라 우선순위 큐. 소비 측 rate limit은 별도 서버 보호장치.",
    }
    save_json(QUEUE, payload)
    # sidecar 호환을 위해 targets list 중심 포맷 유지. rows도 같이 넣어 추후 우선순위 참고 가능하게 한다.
    target_payload = {k: payload[k] for k in ["version", "updated_ts", "updated_text", "target_count", "targets", "buckets", "note"]}
    target_payload["rows"] = rows[:300]
    save_json(WS_TARGETS, target_payload)
    save_json(MICRO_TARGETS, target_payload)
    save_json(MICRO_URGENT, target_payload)
    write_status("running", target_count=len(targets), request_count=len(reqs), near_s_count=payload["near_s_count"], fresh_recovered=fresh_recovered, need_ws=need_ws, need_micro=need_micro, last_sec=0)
    return payload

def run_once() -> Dict[str, Any]:
    st = now_ts()
    p = build_queue()
    sec = now_ts() - st
    write_status("running", target_count=p.get("target_count", 0), request_count=len(_request_rows()), near_s_count=p.get("near_s_count", 0), fresh_recovered=p.get("fresh_recovered", 0), need_ws=p.get("need_ws", 0), need_micro=p.get("need_micro", 0), last_sec=round(sec, 3))
    return p

def main() -> None:
    write_status("running", phase="boot", target_count=0, last_sec=0)
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            write_status("stopping")
            raise
        except Exception as exc:
            append_error("loop", exc)
            write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5, INTERVAL_SEC))

if __name__ == "__main__":
    main()
