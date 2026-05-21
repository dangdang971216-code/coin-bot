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

VERSION="review_worker_v0.2"
INTERVAL_SEC=float(os.getenv("REVIEW_WORKER_INTERVAL_SEC","30")); RETENTION_DAYS=float(os.getenv("REVIEW_RETENTION_DAYS","2"))
STATUS=BASE_DIR/"clean_review_worker_status.json"; ERROR=BASE_DIR/"clean_review_worker_error.log"; CLOSED=BASE_DIR/"paper_bot_closed.jsonl"; REJECT=BASE_DIR/"clean_strategy_s_reject_summary.json"; SUMMARY=BASE_DIR/"clean_review_summary.json"
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})
def profit(row:Dict[str,Any])->float: return fnum(row.get("profit_pct"),row.get("return_pct"),row.get("pnl_pct"),row.get("profit_rate"),default=0.0)
def run_once()->Dict[str,Any]:
    st=now_ts(); write_status("running", phase="refreshing", last_sec=0)
    # 시작 준비 timeout 방지: 큰 장부 전체를 읽지 않고 최근 tail만 본다. 핵심 장부는 삭제하지 않음.
    rows=read_jsonl(CLOSED, max_lines=900)
    big_loss=sorted([r for r in rows if profit(r)<=-0.8], key=profit)[:10]
    good_win=sorted([r for r in rows if profit(r)>=1.0], key=profit, reverse=True)[:10]
    reject=load_json(REJECT,{}) or {}
    save_json(SUMMARY,{"version":VERSION,"updated_ts":now_ts(),"updated_text":now_text(),"retention_days":RETENTION_DAYS,"basis":"recent_tail_900_keep_core_ledger","closed_recent":len(rows),"big_loss":[{k:r.get(k) for k in ["ticker","strategy_name","strategy_key","profit_pct","return_pct","close_reason","sell_reason","opened_at_text","closed_at_text"]} for r in big_loss],"good_win":[{k:r.get(k) for k in ["ticker","strategy_name","strategy_key","profit_pct","return_pct","close_reason","sell_reason","opened_at_text","closed_at_text"]} for r in good_win],"s_reject_reason_top":reject.get("reason_top",[])[:12] if isinstance(reject,dict) else []})
    sec=now_ts()-st; write_status("running", closed_recent=len(rows), big_loss=len(big_loss), good_win=len(good_win), last_sec=round(sec,3)); return {"rows":len(rows)}
def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(10.0,INTERVAL_SEC))
if __name__=="__main__": main()
