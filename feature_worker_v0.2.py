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

VERSION="feature_worker_v0.2"
INTERVAL_SEC=float(os.getenv("FEATURE_WORKER_INTERVAL_SEC","2.0"))
STATUS=BASE_DIR/"clean_feature_status.json"; OUT=BASE_DIR/"clean_feature_cache.json"; ERROR=BASE_DIR/"clean_feature_error.log"
SCANNER=BASE_DIR/"clean_scanner_candidate_pool.json"; CANDLE=BASE_DIR/"clean_candle_cache.json"; REGIME=BASE_DIR/"clean_market_regime_cache.json"
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def run_once()->Dict[str,Any]:
    st=now_ts(); sobj=load_json(SCANNER,{}) or {}; srows=sobj.get("rows") if isinstance(sobj,dict) else []
    if not isinstance(srows,list): srows=[]
    crows=map_rows(load_json(CANDLE,{}) or {}); reg=load_json(REGIME,{}) or {}; rows=[]; ts=now_ts()
    for r in srows:
        if not isinstance(r,dict): continue
        t=ticker_norm(r.get("ticker")); price=fnum(r.get("current_price"),default=0)
        if not t or price<=0: continue
        c=crows.get(t,{})
        ch1=fnum(c.get("m1_candle_change_1"), default=fnum(r.get("change_24h"))/1440.0)
        ch3=fnum(c.get("m3_candle_change_3"), default=fnum(r.get("change_24h"))/480.0)
        ch5=fnum(c.get("m5_candle_change_5"), default=fnum(r.get("change_24h"))/288.0)
        ch15=fnum(c.get("m5_candle_change_3"), default=fnum(r.get("change_24h"))/96.0)
        ch30=fnum(c.get("m30_candle_change_3"), default=fnum(r.get("change_24h"))/48.0)
        vwap_gap=fnum(c.get("m5_recent_low_rebound_pct"), default=fnum(r.get("day_pos_pct"))-50.0)/10.0
        ema5_gap=ch3*0.45+ch5*0.25
        day_pos=fnum(r.get("day_pos_pct"),default=50)
        rel= fnum(r.get("change_24h"))-fnum(reg.get("avg_top60_change"), default=0)
        rr={**r,"feature_ts":ts,"change_1m":round(ch1,3),"change_3m":round(ch3,3),"change_5m":round(ch5,3),"change_15m":round(ch15,3),"change_30m":round(ch30,3),"vwap_gap_pct":round(vwap_gap,3),"ema5_gap_pct":round(ema5_gap,3),"relative_strength_pct":round(rel,3),"day_pos_pct":day_pos,"market_mode":reg.get("mode","확인중"),"candle_ok":bool(c),"breakout_hint":bool(c.get("m5_breakout_hint") or c.get("m3_breakout_hint")),"sweep_reclaim_hint":bool(c.get("m5_sweep_reclaim_hint") or c.get("m3_sweep_reclaim_hint")),"volume_expanding":bool(c.get("m5_volume_expanding") or c.get("m3_volume_expanding")),"long_upper_wick":bool(c.get("m5_long_upper_wick") or c.get("m3_long_upper_wick")),"recent_high_gap_pct":fnum(c.get("m5_recent_high_gap_pct"),default=0),"recent_low_rebound_pct":fnum(c.get("m5_recent_low_rebound_pct"),default=0)}
        rows.append(rr)
    save_json(OUT,{"version":VERSION,"schema":"feature_cache_v2_candle_regime","updated_ts":ts,"updated_text":now_text(ts),"row_count":len(rows),"scanner_age":round(age_sec(SCANNER),1),"candle_age":round(age_sec(CANDLE),1),"regime_age":round(age_sec(REGIME),1),"rows":rows})
    sec=now_ts()-st; write_status("running", row_count=len(rows), last_sec=round(sec,3), scanner_age=round(age_sec(SCANNER),1), candle_age=round(age_sec(CANDLE),1)); return {"rows":len(rows)}
def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
