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

VERSION="candle_worker_v0.2"
INTERVAL_SEC=float(os.getenv("CANDLE_WORKER_INTERVAL_SEC","4.0")); BATCH=int(os.getenv("CANDLE_WORKER_BATCH","4")); HTTP_TIMEOUT=float(os.getenv("CANDLE_WORKER_TIMEOUT","1.8"))
STATUS=BASE_DIR/"clean_candle_status.json"; OUT=BASE_DIR/"clean_candle_cache.json"; ERROR=BASE_DIR/"clean_candle_error.log"
POOL=BASE_DIR/"clean_scanner_candidate_pool.json"; STRAT_TARGET=BASE_DIR/"clean_ws_targets.json"
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def http_json(url:str)->Any:
    req=urllib.request.Request(url,headers={"Accept":"application/json","User-Agent":"coinbot-candle-worker/0.1"})
    with urllib.request.urlopen(req,timeout=HTTP_TIMEOUT) as r: return json.loads(r.read().decode("utf-8",errors="ignore"))
def pick_targets()->List[str]:
    targets=[]
    o=load_json(STRAT_TARGET,{}) or {}; raw=o.get("targets") if isinstance(o,dict) else []
    if isinstance(raw,list): targets += [ticker_norm(x) for x in raw]
    p=load_json(POOL,{}) or {}; rows=p.get("rows") if isinstance(p,dict) else []
    if isinstance(rows,list): targets += [ticker_norm(r.get("ticker")) for r in rows[:140] if isinstance(r,dict)]
    return [t for t in dict.fromkeys(targets) if t][:160]
def parse_candles(payload:Any)->List[List[float]]:
    data=payload.get("data") if isinstance(payload,dict) else payload
    if not isinstance(data,list): return []
    out=[]
    for x in data[-40:]:
        try:
            # Bithumb 구 candle: [time, open, close, high, low, volume]
            if isinstance(x,(list,tuple)) and len(x)>=6:
                vals=[fnum(x[1]),fnum(x[2]),fnum(x[3]),fnum(x[4]),fnum(x[5])]
                if vals[0] and vals[1]: out.append(vals)
        except Exception: pass
    return out
def candle_features(c:List[List[float]])->Dict[str,Any]:
    if len(c)<3: return {}
    op,cl,hi,lo,vol = c[-1]
    prev= c[-2][1]
    ch1=pct(cl, prev); ch3=pct(cl, c[-3][1]) if len(c)>=3 else ch1; ch5=pct(cl, c[-5][1]) if len(c)>=5 else ch3
    rng=max(hi-lo, 1e-12); body=abs(cl-op)/rng*100; upper=(hi-max(op,cl))/rng*100; lower=(min(op,cl)-lo)/rng*100
    vols=[x[4] for x in c[-8:]]; vavg=sum(vols[:-1])/max(1,len(vols)-1); vr=vol/vavg if vavg else 0
    recent_high=max(x[2] for x in c[-10:]); recent_low=min(x[3] for x in c[-10:])
    return {"candle_change_1":round(ch1,3),"candle_change_3":round(ch3,3),"candle_change_5":round(ch5,3),"candle_body_pct":round(body,1),"upper_wick_pct":round(upper,1),"lower_wick_pct":round(lower,1),"volume_ratio":round(vr,2),"recent_high_gap_pct":round(pct(cl,recent_high),3),"recent_low_rebound_pct":round(pct(cl,recent_low),3),"close_near_high": upper<25 and cl>=op,"long_upper_wick": upper>45,"volume_expanding": vr>=1.25,"breakout_hint": cl>=recent_high*0.998 and vr>=1.15,"sweep_reclaim_hint": lower>35 and cl>op and vr>=1.0}
def fetch_one(ticker:str)->Dict[str,Any]:
    base=f"https://api.bithumb.com/public/candlestick/{ticker}_KRW"
    feats={"ticker":ticker,"candle_ts":now_ts()}
    for interval,key in [("1m","m1"),("3m","m3"),("5m","m5"),("30m","m30")]:
        try:
            c=parse_candles(http_json(base+"/"+interval)); cf=candle_features(c)
            for k,v in cf.items(): feats[f"{key}_{k}"]=v
            feats[f"{key}_ok"]=bool(cf)
        except Exception as exc:
            feats[f"{key}_ok"]=False; feats[f"{key}_err"]=str(exc)[:60]
    return feats
def run_once()->Dict[str,Any]:
    st=now_ts(); existing=load_json(OUT,{}) or {}; cache=map_rows(existing)
    targets=pick_targets(); idx=int(fnum(existing.get("cursor") if isinstance(existing,dict) else 0, default=0)) if isinstance(existing,dict) else 0
    batch=targets[idx:idx+BATCH] or targets[:BATCH]; new=[]
    # ready 판정용: 캔들 API 수집 전에 먼저 현재 캐시 상태를 running으로 기록한다.
    write_status("running", phase="fetching", row_count=len(cache), target_count=len(targets), batch=len(batch), last_sec=0)
    for t in batch:
        row=fetch_one(t); cache[t]=row; new.append(t)
    # 20분 이상 오래된 candle cache 제거
    cutoff=now_ts()-1200
    rows=[v for v in cache.values() if fnum(v.get("candle_ts"),default=0)>=cutoff]
    rows.sort(key=lambda r:fnum(r.get("candle_ts")), reverse=True)
    next_idx=(idx+BATCH)%max(1,len(targets))
    save_json(OUT,{"version":VERSION,"schema":"candle_cache_v1","updated_ts":now_ts(),"updated_text":now_text(),"row_count":len(rows),"target_count":len(targets),"cursor":next_idx,"updated_batch":new,"rows":rows})
    sec=now_ts()-st; write_status("running", row_count=len(rows), target_count=len(targets), batch=len(new), last_sec=round(sec,3))
    return {"rows":len(rows),"sec":sec}
def main()->None:
    # systemd/guard가 initializing에서 timeout으로 오판하지 않도록 즉시 running 상태를 남긴다.
    existing=load_json(OUT,{}) or {}; cache=map_rows(existing)
    write_status("running", phase="boot", row_count=len(cache), target_count=0, batch=0, last_sec=0)
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(1.0,INTERVAL_SEC))
if __name__=="__main__": main()
