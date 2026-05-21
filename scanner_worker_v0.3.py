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

VERSION="scanner_worker_v0.3"
INTERVAL_SEC=float(os.getenv("SCANNER_WORKER_INTERVAL_SEC","2.0"))
HTTP_TIMEOUT_SEC=float(os.getenv("SCANNER_WORKER_HTTP_TIMEOUT_SEC","3.0"))
STATUS=BASE_DIR/"clean_scanner_status.json"; MARKET=BASE_DIR/"clean_scanner_market_cache.json"; POOL=BASE_DIR/"clean_scanner_candidate_pool.json"; ERROR=BASE_DIR/"clean_scanner_error.log"
STABLE={"USDT","USDC","DAI","BUSD","FDUSD","TUSD","USDS","USD1","PYUSD","USDE","RLUSD"}; MAJOR={"BTC","ETH","XRP"}
def http_json(url:str)->Any:
    req=urllib.request.Request(url,headers={"Accept":"application/json","User-Agent":"coinbot-scanner-worker/0.3"})
    with urllib.request.urlopen(req,timeout=HTTP_TIMEOUT_SEC) as r:
        return json.loads(r.read().decode("utf-8",errors="ignore"))
def parse_rows(payload:Any)->List[Dict[str,Any]]:
    data=payload.get("data") if isinstance(payload,dict) else payload
    if not isinstance(data,dict): return []
    ts=now_ts(); rows=[]
    for k,row in data.items():
        t=ticker_norm(k)
        if not t or t in {"DATE","STATUS"} or t in STABLE: continue
        if not isinstance(row,dict): continue
        price=fnum(row.get("closing_price"),row.get("trade_price"),row.get("close"),row.get("prev_closing_price"),default=0)
        if price<=0: continue
        prev=fnum(row.get("prev_closing_price"), row.get("opening_price"), default=price)
        turnover=fnum(row.get("acc_trade_value_24H"),row.get("acc_trade_value_24h"),row.get("acc_trade_value"),row.get("acc_trade_price_24h"),default=0)
        vol=fnum(row.get("units_traded_24H"),row.get("units_traded_24h"),row.get("units_traded"),row.get("volume"),default=0)
        chg=fnum(row.get("fluctate_rate_24H"),row.get("fluctate_rate_24h"),row.get("fluctate_rate"),row.get("change_rate"),default=pct(price,prev))
        high=fnum(row.get("max_price"), row.get("high_price"), default=price)
        low=fnum(row.get("min_price"), row.get("low_price"), default=price)
        rows.append({"ticker":t,"market":f"{t}_KRW","current_price":price,"price":price,"prev_price":prev,"turnover_24h":turnover,"volume_24h":vol,"change_24h":chg,"day_high":high,"day_low":low,"day_pos_pct":round((price-low)/(high-low)*100,2) if high>low else 50.0,"major_watch":t in MAJOR,"fresh_ts":ts,"source":"scanner_ALL_KRW"})
    rows.sort(key=lambda r:(fnum(r.get("turnover_24h")), abs(fnum(r.get("change_24h")))), reverse=True)
    for i,r in enumerate(rows,1): r["turnover_rank"]=i
    return rows
def build_pool(rows:List[Dict[str,Any]])->List[Dict[str,Any]]:
    out=[]
    for r in rows:
        t24=fnum(r.get("turnover_24h")); ch=abs(fnum(r.get("change_24h"))); pos=fnum(r.get("day_pos_pct"),default=50)
        score=min(10,t24/700_000_000.0)+min(8,ch)+ (1.0 if 35<=pos<=85 else 0.0)
        if r.get("major_watch"): score*=0.25
        rr=dict(r); rr["scanner_priority_score"]=round(score,3); out.append(rr)
    out.sort(key=lambda r:(fnum(r.get("scanner_priority_score")), fnum(r.get("turnover_24h"))), reverse=True)
    return out[:650]
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def run_once()->Dict[str,Any]:
    st=now_ts(); rows=parse_rows(http_json("https://api.bithumb.com/public/ticker/ALL_KRW"))
    if not rows: raise RuntimeError("scanner empty rows")
    pool=build_pool(rows); ts=now_ts()
    save_json(MARKET,{"version":VERSION,"schema":"scanner_market_cache_v3","updated_ts":ts,"updated_text":now_text(ts),"row_count":len(rows),"rows":rows})
    save_json(POOL,{"version":VERSION,"schema":"scanner_candidate_pool_v3","updated_ts":ts,"updated_text":now_text(ts),"row_count":len(pool),"rows":pool})
    sec=now_ts()-st; write_status("running", row_count=len(rows), pool_count=len(pool), last_sec=round(sec,3), source="ALL_KRW")
    return {"rows":len(rows),"pool":len(pool),"sec":sec}
def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
