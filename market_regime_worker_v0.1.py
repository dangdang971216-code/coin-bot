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

VERSION="market_regime_worker_v0.1"
INTERVAL_SEC=float(os.getenv("MARKET_REGIME_INTERVAL_SEC","3.0"))
STATUS=BASE_DIR/"clean_market_regime_status.json"; OUT=BASE_DIR/"clean_market_regime_cache.json"; ERROR=BASE_DIR/"clean_market_regime_error.log"; MARKET=BASE_DIR/"clean_scanner_market_cache.json"; CANDLE=BASE_DIR/"clean_candle_cache.json"
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def run_once()->Dict[str,Any]:
    st=now_ts(); m=load_json(MARKET,{}) or {}; rows=m.get("rows") if isinstance(m,dict) else []
    if not isinstance(rows,list): rows=[]
    tradable=[r for r in rows if not r.get("major_watch")]
    n=len(tradable); up=sum(1 for r in tradable if fnum(r.get("change_24h"))>0); strong=sum(1 for r in tradable if fnum(r.get("change_24h"))>=2.0)
    top_turn=sum(fnum(r.get("turnover_24h")) for r in tradable[:30]); avg_ch=sum(fnum(r.get("change_24h")) for r in tradable[:60])/max(1,min(60,n))
    breadth=up/max(1,n)*100; score=0
    score += 2 if breadth>=55 else (-1 if breadth<42 else 0)
    score += 2 if strong>=35 else (1 if strong>=18 else -1)
    score += 2 if avg_ch>=0.8 else (-1 if avg_ch<-0.5 else 0)
    score += 1 if top_turn>=25_000_000_000 else 0
    mode="강함" if score>=4 else ("보통" if score>=1 else "약함")
    obj={"version":VERSION,"schema":"market_regime_v1","updated_ts":now_ts(),"updated_text":now_text(),"mode":mode,"score":score,"row_count":n,"breadth_up_pct":round(breadth,1),"strong_count":strong,"avg_top60_change":round(avg_ch,3),"top30_turnover":round(top_turn,0)}
    save_json(OUT,obj); sec=now_ts()-st; write_status("running", row_count=n, mode=mode, score=score, last_sec=round(sec,3)); return obj
def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(1.0,INTERVAL_SEC))
if __name__=="__main__": main()
