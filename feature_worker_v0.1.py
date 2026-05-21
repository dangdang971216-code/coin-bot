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

VERSION="feature_worker_v0.1"
INTERVAL_SEC=float(os.getenv("FEATURE_WORKER_INTERVAL_SEC","2.5"))
STATUS=BASE_DIR/"clean_feature_status.json"
ERROR=BASE_DIR/"clean_feature_error.log"
SCANNER_POOL=BASE_DIR/"clean_scanner_candidate_pool.json"
FEATURE=BASE_DIR/"clean_feature_cache.json"
HISTORY=BASE_DIR/"clean_feature_history.json"
MAX_HISTORY=int(os.getenv("FEATURE_WORKER_MAX_HISTORY","900"))

def write_status(state:str, **extra:Any)->None:
    save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})

def find_past(hist:List[Tuple[float,float]], sec:float)->float:
    if not hist: return 0.0
    target=now_ts()-sec
    best=hist[0][1]
    for ts,price in hist:
        if ts <= target: best=price
        else: break
    return best or hist[0][1]

def ema_proxy(current:float, past:float, weight:float=0.35)->float:
    return current*weight + past*(1-weight) if past>0 else current

def run_once()->Dict[str,Any]:
    st=now_ts()
    obj=load_json(SCANNER_POOL,{}) or {}
    rows=obj.get("rows") if isinstance(obj,dict) else []
    if not isinstance(rows,list): rows=[]
    hist_obj=load_json(HISTORY,{}) or {}
    hist=hist_obj.get("history") if isinstance(hist_obj,dict) else {}
    if not isinstance(hist,dict): hist={}
    ts=now_ts(); out=[]
    for r in rows:
        t=ticker_norm(r.get("ticker")); price=fnum(r.get("current_price"),r.get("price"),default=0)
        if not t or price<=0: continue
        h=hist.get(t) if isinstance(hist.get(t),list) else []
        h=[x for x in h if isinstance(x,list) or isinstance(x,tuple)]
        h=[(fnum(x[0]), fnum(x[1])) for x in h if len(x)>=2 and fnum(x[1])>0]
        h.append((ts, price)); h=h[-MAX_HISTORY:]
        hist[t]=h
        p3=find_past(h,180); p5=find_past(h,300); p15=find_past(h,900); p30=find_past(h,1800)
        ch3=pct(price,p3); ch5=pct(price,p5); ch15=pct(price,p15); ch30=pct(price,p30)
        hi30=max([p for _,p in h[-240:]], default=price); lo30=min([p for _,p in h[-240:]], default=price)
        ema5=ema_proxy(price,p5,0.45); ema15=ema_proxy(price,p15,0.28)
        vwap_proxy=(price*0.55 + p5*0.25 + p15*0.20) if p5 and p15 else price
        turnover=fnum(r.get("turnover_24h"),default=0)
        scanner_score=fnum(r.get("scanner_priority_score"),default=0)
        rr=dict(r)
        rr.update({
            "feature_ts":ts,"feature_age_sec":0,
            "change_3m":round(ch3,3),"change_5m":round(ch5,3),"change_15m":round(ch15,3),"change_30m":round(ch30,3),
            "ema5_gap_pct":round(pct(price,ema5),3),"ema15_gap_pct":round(pct(price,ema15),3),"vwap_gap_pct":round(pct(price,vwap_proxy),3),
            "below_30m_high_pct":round(pct(hi30,price),3),"from_30m_low_pct":round(pct(price,lo30),3),
            "history_points":len(h),"turnover_24h":turnover,"scanner_priority_score":scanner_score,
            "money_flow_3m_proxy":round(turnover*max(0.05,abs(ch3))/100.0,2),
            "money_flow_5m_proxy":round(turnover*max(0.05,abs(ch5))/100.0,2),
        })
        out.append(rr)
    save_json(HISTORY,{"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"history":hist})
    save_json(FEATURE,{"version":VERSION,"schema":"feature_cache_v1","updated_ts":ts,"updated_text":now_text(ts),"row_count":len(out),"rows":out})
    sec=now_ts()-st; write_status("running", row_count=len(out), scanner_age=round(ts-fnum(obj.get("updated_ts"),default=ts),1), last_sec=round(sec,3))
    return {"rows":len(out),"sec":sec}

def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
