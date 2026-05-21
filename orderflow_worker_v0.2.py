#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import json, os, time, math, traceback, urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional
BASE_DIR = Path(os.getenv("TRADING_BOT_DIR", "/home/dangdang971216/trading_bot"))
def now_ts() -> float: return time.time()
def now_text(ts: Optional[float]=None) -> str: return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or now_ts()))
def fnum(*vals: Any, default: float=0.0) -> float:
    for v in vals:
        try:
            if v is None or v == "": continue
            return float(str(v).replace(",", "").replace("%", ""))
        except Exception: continue
    return default
def load_json(path: Path, default: Any=None) -> Any:
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception: return default
def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    os.replace(tmp, path)
def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)+"\n")
def write_jsonl_atomic(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":"), default=str)+"\n")
    os.replace(tmp, path)
def read_jsonl(path: Path, max_lines: int=1000) -> List[Dict[str, Any]]:
    if not path.exists(): return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if max_lines and len(lines) > max_lines: lines = lines[-max_lines:]
        out=[]
        for ln in lines:
            try:
                if ln.strip():
                    o=json.loads(ln)
                    if isinstance(o,dict): out.append(o)
            except Exception: pass
        return out
    except Exception: return []
def append_error(path: Path, where: str, exc: BaseException) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{now_text()}] {where}: {exc.__class__.__name__}: {exc}\n")
            f.write(traceback.format_exc()[-1500:]+"\n")
    except Exception: pass
def ticker_norm(x: Any) -> str:
    t = str(x or "").strip().upper().replace("/", "-").replace("_", "-")
    parts=[p for p in t.split("-") if p]
    if len(parts)>=2:
        non=[p for p in parts if p != "KRW"]
        t = non[-1] if non else parts[-1]
    if t.endswith("KRW") and len(t)>3: t=t[:-3]
    return t.strip().upper()
def pct(a: float, b: float) -> float: return ((a-b)/b*100.0) if b else 0.0
def age_sec(path: Path) -> float:
    try: return max(0.0, now_ts()-path.stat().st_mtime)
    except Exception: return 999999.0
def map_rows(obj: Any) -> Dict[str, Dict[str, Any]]:
    if isinstance(obj, dict) and isinstance(obj.get("rows"), list): obj = obj.get("rows")
    elif isinstance(obj, dict) and isinstance(obj.get("rows"), dict): obj = obj.get("rows")
    elif isinstance(obj, dict) and isinstance(obj.get("cache"), dict): obj = obj.get("cache")
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(obj, list):
        for r in obj:
            if isinstance(r, dict):
                t=ticker_norm(r.get("ticker") or r.get("symbol") or r.get("market"))
                if t: out[t]=r
    elif isinstance(obj, dict):
        for k,v in obj.items():
            t=ticker_norm(k)
            if t and isinstance(v, dict):
                vv=dict(v); vv.setdefault("ticker", t); out[t]=vv
    return out

VERSION="orderflow_worker_v0.2"
INTERVAL_SEC=float(os.getenv("ORDERFLOW_WORKER_INTERVAL_SEC","2.0"))
STATUS=BASE_DIR/"clean_orderflow_status.json"; OUT=BASE_DIR/"clean_orderflow_summary_cache.json"; ERROR=BASE_DIR/"clean_orderflow_error.log"
FEATURE=BASE_DIR/"clean_feature_cache.json"; WS=BASE_DIR/"clean_ws_live_cache.json"; MICRO=BASE_DIR/"clean_bithumb_micro_cache.json"
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def is_fresh(row:Dict[str,Any], ttl:float)->bool:
    ts=fnum(row.get("updated_ts"), row.get("ts"), row.get("fresh_ts"), row.get("timestamp"), row.get("time"), default=0)
    return ts>0 and now_ts()-ts <= ttl
def run_once()->Dict[str,Any]:
    st=now_ts(); fobj=load_json(FEATURE,{}) or {}; features=fobj.get("rows") if isinstance(fobj,dict) else []
    if not isinstance(features,list): features=[]
    ws=map_rows(load_json(WS,{}) or {}); micro=map_rows(load_json(MICRO,{}) or {})
    rows=[]; fresh_ws=0; fresh_micro=0; ts=now_ts()
    for r in features:
        t=ticker_norm(r.get("ticker"));
        if not t: continue
        w=ws.get(t,{}) or {}; m=micro.get(t,{}) or {}
        wf=is_fresh(w,20.0); mf=is_fresh(m,25.0)
        fresh_ws += 1 if wf else 0; fresh_micro += 1 if mf else 0
        price=fnum(r.get("current_price"), default=0); ws_price=fnum(w.get("price"),w.get("current_price"),w.get("trade_price"),w.get("close"), default=0)
        spread=fnum(m.get("spread_pct"),m.get("orderbook_spread_pct"),m.get("micro_spread_pct"), default=0.0)
        ask=fnum(m.get("ask_price"),m.get("best_ask"), default=0); bid=fnum(m.get("bid_price"),m.get("best_bid"), default=0)
        if not spread and ask and bid: spread=pct(ask,bid)
        buy_ratio=fnum(m.get("buy_ratio"),m.get("trade_buy_ratio_30"),m.get("micro_trade_buy_ratio_30"),m.get("buy_trade_ratio"),m.get("buy_ratio_30s"), default=0.0)
        if buy_ratio>1.5: buy_ratio=buy_ratio/100.0
        bid_wall=fnum(m.get("bid_ask_wall_ratio"),m.get("micro_bid_ask_wall_ratio"),m.get("buy_wall_ratio"), default=0.0)
        sell_pressure=bool(m.get("ask_wall_pressure") or m.get("micro_ask_wall_pressure") or m.get("sell_trade_pressure") or m.get("micro_sell_trade_pressure") or m.get("ask_pressure"))
        gap=pct(ws_price,price) if price and ws_price else 0.0
        strength_score=0
        strength_score += 2 if wf else -1; strength_score += 2 if mf else -1
        strength_score += 1 if 0<spread<=0.22 else (-1 if spread>0.35 else 0)
        strength_score += 1 if buy_ratio>=0.62 else (-1 if buy_ratio and buy_ratio<0.5 else 0)
        strength_score += -1 if sell_pressure else 1
        rr={"ticker":t,"orderflow_ts":ts,"ws_fresh":wf,"micro_fresh":mf,"ws_price":ws_price,"ws_gap_pct":round(gap,3),"spread_pct":round(spread,3),"buy_ratio":round(buy_ratio,3),"bid_wall_ratio":round(bid_wall,3),"sell_pressure":sell_pressure,"orderflow_score":strength_score,"micro_status":"fresh" if mf else ("stale" if m else "missing"),"ws_status":"fresh" if wf else ("stale" if w else "missing")}
        rows.append(rr)
    save_json(OUT,{"version":VERSION,"schema":"orderflow_summary_v2","updated_ts":ts,"updated_text":now_text(ts),"row_count":len(rows),"fresh_ws":fresh_ws,"fresh_micro":fresh_micro,"ws_cache_count":len(ws),"micro_cache_count":len(micro),"rows":rows})
    sec=now_ts()-st; write_status("running", row_count=len(rows), fresh_ws=fresh_ws, fresh_micro=fresh_micro, ws_cache_count=len(ws), micro_cache_count=len(micro), last_sec=round(sec,3))
    return {"rows":len(rows)}
def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
