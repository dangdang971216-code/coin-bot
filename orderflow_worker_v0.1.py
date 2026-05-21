#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import json, os, time, math, traceback, urllib.request, urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

BASE_DIR = Path(os.getenv("TRADING_BOT_DIR", "/home/dangdang971216/trading_bot"))

def now_ts() -> float: return time.time()
def now_text(ts: Optional[float]=None) -> str: return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or now_ts()))
def fnum(*vals: Any, default: float=0.0) -> float:
    for v in vals:
        try:
            if v is None or v == "": continue
            return float(str(v).replace(",", ""))
        except Exception:
            continue
    return default

def load_json(path: Path, default: Any=None) -> Any:
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default

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
    except Exception:
        return []

def append_error(path: Path, where: str, exc: BaseException) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[{now_text()}] {where}: {exc.__class__.__name__}: {exc}\n")
            f.write(traceback.format_exc()[-1200:]+"\n")
    except Exception:
        pass

def ticker_norm(x: Any) -> str:
    t = str(x or "").strip().upper().replace("/", "-").replace("_", "-")
    parts=[p for p in t.split("-") if p]
    if len(parts)>=2:
        non=[p for p in parts if p != "KRW"]
        t = non[-1] if non else parts[-1]
    if t.endswith("KRW") and len(t)>3: t=t[:-3]
    return t.strip().upper()

def pct(a: float, b: float) -> float:
    return ((a-b)/b*100.0) if b else 0.0

VERSION="orderflow_worker_v0.1"
INTERVAL_SEC=float(os.getenv("ORDERFLOW_WORKER_INTERVAL_SEC","1.5"))
STATUS=BASE_DIR/"clean_orderflow_status.json"
ERROR=BASE_DIR/"clean_orderflow_error.log"
FEATURE=BASE_DIR/"clean_feature_cache.json"
WS=BASE_DIR/"clean_ws_live_cache.json"
MICRO=BASE_DIR/"clean_bithumb_micro_cache.json"
OUT=BASE_DIR/"clean_orderflow_summary_cache.json"

def write_status(state:str, **extra:Any)->None:
    save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})

def map_rows(obj:Any)->Dict[str,Dict[str,Any]]:
    if isinstance(obj,dict):
        if isinstance(obj.get("rows"),list):
            return {ticker_norm(r.get("ticker") or r.get("symbol") or r.get("market")):r for r in obj.get("rows",[]) if ticker_norm(r.get("ticker") or r.get("symbol") or r.get("market"))}
        if isinstance(obj.get("cache"),dict): obj=obj.get("cache")
        out={}
        for k,v in obj.items():
            t=ticker_norm(k)
            if t and isinstance(v,dict): out[t]=v
        return out
    return {}

def is_fresh(row:Dict[str,Any], ttl:float=12.0)->bool:
    ts=fnum(row.get("updated_ts"), row.get("ts"), row.get("fresh_ts"), row.get("timestamp"), default=0)
    return ts>0 and now_ts()-ts <= ttl

def run_once()->Dict[str,Any]:
    st=now_ts(); ts=now_ts()
    fobj=load_json(FEATURE,{}) or {}; features=fobj.get("rows") if isinstance(fobj,dict) else []
    if not isinstance(features,list): features=[]
    ws=map_rows(load_json(WS,{}) or {})
    micro=map_rows(load_json(MICRO,{}) or {})
    rows=[]; fresh_ws=0; fresh_micro=0
    for r in features:
        t=ticker_norm(r.get("ticker"));
        if not t: continue
        w=ws.get(t,{}) or {}; m=micro.get(t,{}) or {}
        wf=is_fresh(w,15.0); mf=is_fresh(m,18.0)
        fresh_ws += 1 if wf else 0; fresh_micro += 1 if mf else 0
        price=fnum(r.get("current_price"), default=0)
        ws_price=fnum(w.get("price"),w.get("current_price"),w.get("trade_price"), default=0)
        spread=fnum(m.get("spread_pct"),m.get("orderbook_spread_pct"),m.get("micro_spread_pct"), default=0.0)
        buy_ratio=fnum(m.get("buy_ratio"),m.get("trade_buy_ratio_30"),m.get("micro_trade_buy_ratio_30"),m.get("buy_trade_ratio"), default=0.0)
        if buy_ratio>1.5: buy_ratio=buy_ratio/100.0
        bid_wall=fnum(m.get("bid_ask_wall_ratio"),m.get("micro_bid_ask_wall_ratio"),m.get("buy_wall_ratio"), default=0.0)
        sell_pressure=bool(m.get("ask_wall_pressure") or m.get("micro_ask_wall_pressure") or m.get("sell_trade_pressure") or m.get("micro_sell_trade_pressure"))
        if not spread and price and fnum(m.get("ask_price"),default=0)>0 and fnum(m.get("bid_price"),default=0)>0:
            spread=pct(fnum(m.get("ask_price")), fnum(m.get("bid_price")))
        gap=pct(ws_price,price) if price and ws_price else 0.0
        rr={"ticker":t,"orderflow_ts":ts,"ws_fresh":wf,"micro_fresh":mf,"ws_price":ws_price,"ws_gap_pct":round(gap,3),"spread_pct":round(spread,3),"buy_ratio":round(buy_ratio,3),"bid_wall_ratio":round(bid_wall,3),"sell_pressure":sell_pressure,"micro_status":"fresh" if mf else ("stale" if m else "missing"),"ws_status":"fresh" if wf else ("stale" if w else "missing")}
        rows.append(rr)
    save_json(OUT,{"version":VERSION,"schema":"orderflow_summary_v1","updated_ts":ts,"updated_text":now_text(ts),"row_count":len(rows),"fresh_ws":fresh_ws,"fresh_micro":fresh_micro,"rows":rows})
    sec=now_ts()-st; write_status("running", row_count=len(rows), fresh_ws=fresh_ws, fresh_micro=fresh_micro, last_sec=round(sec,3))
    return {"rows":len(rows),"sec":sec}

def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
