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

VERSION="risk_worker_v0.2"
INTERVAL_SEC=float(os.getenv("RISK_WORKER_INTERVAL_SEC","10.0")); RETENTION_DAYS=float(os.getenv("RISK_RETENTION_DAYS","2"))
STATUS=BASE_DIR/"clean_risk_status.json"; OUT=BASE_DIR/"clean_risk_filter_cache.json"; ERROR=BASE_DIR/"clean_risk_error.log"; CLOSED=BASE_DIR/"paper_bot_closed.jsonl"; OPEN=BASE_DIR/"paper_bot_open.json"
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def ts_of(r:Dict[str,Any])->float: return fnum(r.get("closed_at"),r.get("sell_ts"),r.get("closed_ts"),r.get("updated_ts"),default=0)
def profit(r:Dict[str,Any])->float: return fnum(r.get("profit_pct"),r.get("return_pct"),r.get("pnl_pct"),r.get("profit_rate"),default=0)
def run_once()->Dict[str,Any]:
    st=now_ts(); cutoff=now_ts()-RETENTION_DAYS*86400
    write_status("running", phase="reading", recent_closed=0, risk_count=0, open_count=0, last_sec=0)
    rows=[r for r in read_jsonl(CLOSED, max_lines=600) if ts_of(r)>=cutoff or ts_of(r)==0]
    by:Dict[str,List[Dict[str,Any]]]={}
    for r in rows:
        t=ticker_norm(r.get("ticker"));
        if t: by.setdefault(t,[]).append(r)
    risks=[]; cooldown={}
    for t,rs in by.items():
        rs=sorted(rs, key=ts_of, reverse=True); last=rs[0] if rs else {}; losses=[r for r in rs[:5] if profit(r)<=-0.45]; hard=[r for r in rs[:5] if profit(r)<=-0.9]
        level="ok"; reasons=[]
        if len(hard)>=1: level="hard_recent"; reasons.append("최근하드손실")
        if len(losses)>=2: level="loss_cluster"; reasons.append("최근반복손실")
        if level!="ok": cooldown[t]={"level":level,"reasons":reasons,"last_profit":profit(last),"last_ts":ts_of(last)}; risks.append({"ticker":t,**cooldown[t]})
    open_obj=load_json(OPEN,{}) or {}; open_ticks=[]
    if isinstance(open_obj,dict): open_ticks=[ticker_norm(v.get("ticker") if isinstance(v,dict) else k) for k,v in open_obj.items()]
    save_json(OUT,{"version":VERSION,"schema":"risk_filter_v1","updated_ts":now_ts(),"updated_text":now_text(),"retention_days":RETENTION_DAYS,"recent_closed":len(rows),"risk_count":len(risks),"open_tickers":[x for x in open_ticks if x],"cooldown":cooldown,"risks":risks[:80]})
    sec=now_ts()-st; write_status("running", recent_closed=len(rows), risk_count=len(risks), open_count=len(open_ticks), last_sec=round(sec,3)); return {"risk":len(risks)}
def main()->None:
    write_status("running", phase="boot", recent_closed=0, risk_count=0, open_count=0, last_sec=0)
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(3.0,INTERVAL_SEC))
if __name__=="__main__": main()
