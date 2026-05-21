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

VERSION="strategy_worker_v0.1"
INTERVAL_SEC=float(os.getenv("STRATEGY_WORKER_INTERVAL_SEC","2.0"))
STATUS=BASE_DIR/"clean_strategy_worker_status.json"
ERROR=BASE_DIR/"clean_strategy_worker_error.log"
FEATURE=BASE_DIR/"clean_feature_cache.json"
ORDERFLOW=BASE_DIR/"clean_orderflow_summary_cache.json"
PAPER=BASE_DIR/"paper_candidates.jsonl"
PAPER_LATEST=BASE_DIR/"paper_candidates_latest.jsonl"
SHADOW=BASE_DIR/"shadow_candidates.jsonl"
SHADOW_LATEST=BASE_DIR/"shadow_candidates_latest.jsonl"
SUMMARY=BASE_DIR/"clean_strategy_s_summary.json"
REJECT=BASE_DIR/"clean_strategy_s_reject_summary.json"
WS_TARGETS=BASE_DIR/"clean_ws_targets.json"
MICRO_TARGETS=BASE_DIR/"clean_micro_targets.json"
MICRO_URGENT=BASE_DIR/"clean_micro_urgent_targets.json"
STRATEGIES={
 "money_reaccel_s":"돈흐름 재가속 S",
 "leader_momentum_s":"주도추세 지속 S",
 "breakout_early_s":"거래대금 돌파 초입 S",
 "sweep_vwap_recovery_s":"저점 VWAP 회복 S",
 "surge_rebreak_s":"급등 후 재돌파 S",
}

def write_status(state:str, **extra:Any)->None:
    save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})

def map_by_ticker(rows:List[Dict[str,Any]])->Dict[str,Dict[str,Any]]:
    return {ticker_norm(r.get("ticker")):r for r in rows if ticker_norm(r.get("ticker"))}

def hard_common(row:Dict[str,Any], of:Dict[str,Any])->List[str]:
    bad=[]
    if row.get("major_watch"): bad.append("대형주는 시장참고용")
    if not of.get("ws_fresh"): bad.append("WS신선도부족")
    if not of.get("micro_fresh"): bad.append("micro신선도부족")
    if fnum(of.get("spread_pct"),default=9)>0.30: bad.append(f"스프레드넓음 {fnum(of.get('spread_pct')):.2f}%")
    if fnum(of.get("buy_ratio"),default=0)<0.58: bad.append(f"매수체결약함 {fnum(of.get('buy_ratio')):.2f}")
    if of.get("sell_pressure"): bad.append("매도벽/매도체결압력")
    if fnum(row.get("turnover_24h"),default=0)<350_000_000: bad.append("24h거래대금부족")
    if fnum(row.get("vwap_gap_pct"),default=-9)<-0.20 and fnum(row.get("ema5_gap_pct"),default=-9)<-0.20: bad.append("VWAP/EMA약함")
    if fnum(row.get("below_30m_high_pct"),default=0)>0 and fnum(row.get("below_30m_high_pct"))<0.20 and fnum(row.get("change_15m"),default=0)>1.2:
        bad.append("고점바로밑추격위험")
    return bad

def eval_money(row,of)->Tuple[bool,List[str],List[str]]:
    why=[]; no=[]
    if fnum(row.get("change_3m"))>0.15 and fnum(row.get("change_5m"))>-0.10: why.append("3분/5분재가속")
    else: no.append("재가속부족")
    if fnum(row.get("vwap_gap_pct"))>=-0.05 or fnum(row.get("ema5_gap_pct"))>=-0.05: why.append("VWAP/EMA방어")
    else: no.append("VWAP/EMA미흡")
    if fnum(of.get("buy_ratio"))>=0.64: why.append("매수체결우세")
    else: no.append("매수체결부족")
    if fnum(row.get("money_flow_3m_proxy"))>2_000_000 or fnum(row.get("turnover_24h"))>1_200_000_000: why.append("돈흐름충분")
    else: no.append("돈흐름부족")
    return len(no)==0, why, no

def eval_leader(row,of)->Tuple[bool,List[str],List[str]]:
    why=[]; no=[]
    if fnum(row.get("change_15m"))>=0.8: why.append("15분주도")
    else: no.append("15분주도부족")
    if fnum(row.get("change_30m"))>=0.6: why.append("30분유지")
    else: no.append("30분유지부족")
    if fnum(row.get("vwap_gap_pct"))>=0 and fnum(row.get("ema5_gap_pct"))>=-0.05: why.append("VWAP/EMA유지")
    else: no.append("추세위치부족")
    if fnum(of.get("buy_ratio"))>=0.60: why.append("매수체결양호")
    else: no.append("체결부족")
    return len(no)==0, why, no

def eval_breakout(row,of)->Tuple[bool,List[str],List[str]]:
    why=[]; no=[]
    if fnum(row.get("change_5m"))>=0.35 and fnum(row.get("change_15m"))>=0.5: why.append("돌파초입흐름")
    else: no.append("돌파흐름부족")
    if 0.25 <= fnum(row.get("below_30m_high_pct"),default=9) <= 2.5: why.append("고점권접근")
    else: no.append("돌파위치아님")
    if fnum(of.get("buy_ratio"))>=0.63 and not of.get("sell_pressure"): why.append("매도벽부담낮음")
    else: no.append("벽/체결부담")
    return len(no)==0, why, no

def eval_sweep(row,of)->Tuple[bool,List[str],List[str]]:
    why=[]; no=[]
    if fnum(row.get("from_30m_low_pct"))>=0.45 and fnum(row.get("from_30m_low_pct"))<=4.0: why.append("저점방어후회복")
    else: no.append("저점회복위치불명")
    if fnum(row.get("vwap_gap_pct"))>=0 or fnum(row.get("ema5_gap_pct"))>=0: why.append("VWAP/EMA회복")
    else: no.append("회복미확인")
    if fnum(of.get("buy_ratio"))>=0.64: why.append("체결회복")
    else: no.append("체결회복부족")
    return len(no)==0, why, no

def eval_surge(row,of)->Tuple[bool,List[str],List[str]]:
    why=[]; no=[]
    if fnum(row.get("change_15m"))>=1.0 and fnum(row.get("change_5m"))>=0.05: why.append("1차상승후유지")
    else: no.append("재돌파흐름부족")
    if fnum(row.get("below_30m_high_pct"),default=9)>=0.35: why.append("끝물추격완화")
    else: no.append("고점추격위험")
    if fnum(of.get("buy_ratio"))>=0.66: why.append("체결재상승")
    else: no.append("체결재상승부족")
    return len(no)==0, why, no

def build_event(row:Dict[str,Any], of:Dict[str,Any], skey:str, reasons:List[str])->Dict[str,Any]:
    t=ticker_norm(row.get("ticker")); ts=now_ts(); price=fnum(row.get("current_price"),row.get("price"),default=0)
    eid=f"{VERSION}:{skey}:{t}:{int(ts)}"
    label=STRATEGIES.get(skey,skey)
    return {"event_id":eid,"ticker":t,"market":f"{t}_KRW","created_at":ts,"created_at_text":now_text(ts),"detected_ts":ts,
        "strategy_key":skey,"strategy_name":label,"strategy_bucket_primary":skey,"strategy_bucket_primary_label":label,
        "strategy_bucket_labels":[label],"candidate_grade":"S","candidate_grade_label":"✅ S급 자동매매 상위","grade":"S",
        "paper_bot_open":True,"open_eligible":True,"trade_ready":True,"lane":"strict",
        "current_price":price,"entry_price":price,"detected_price":price,"score":round(12.0+len(reasons)*0.8,2),
        "take_profit_pct":1.80,"extended_take_profit_pct":2.80,"protect_trigger_pct":1.00,"protect_floor_pct":0.45,"stop_loss_pct":-0.45,"time_exit_minutes":60,
        "reasons":reasons[:6],"quality_risk_tags":[],"entry_context":{**{k:row.get(k) for k in ["change_3m","change_5m","change_15m","change_30m","vwap_gap_pct","ema5_gap_pct","turnover_24h"]}, **of, "strategy_reasons":reasons[:6], "brain_version":VERSION}}

def run_once()->Dict[str,Any]:
    st=now_ts(); ts=now_ts()
    fobj=load_json(FEATURE,{}) or {}; rows=fobj.get("rows") if isinstance(fobj,dict) else []
    if not isinstance(rows,list): rows=[]
    ofobj=load_json(ORDERFLOW,{}) or {}; ofrows=ofobj.get("rows") if isinstance(ofobj,dict) else []
    ofmap=map_by_ticker(ofrows if isinstance(ofrows,list) else [])
    evals=[("money_reaccel_s",eval_money),("leader_momentum_s",eval_leader),("breakout_early_s",eval_breakout),("sweep_vwap_recovery_s",eval_sweep),("surge_rebreak_s",eval_surge)]
    events=[]; rejects=[]; target=[]
    for row in rows:
        t=ticker_norm(row.get("ticker"));
        if not t: continue
        of=ofmap.get(t,{})
        common_bad=hard_common(row,of)
        local_reasons=[]; s_hits=[]
        for skey,fn in evals:
            ok,why,no=fn(row,of)
            if ok and not common_bad:
                s_hits.append((skey,why))
            else:
                local_reasons += common_bad[:2] + [f"{STRATEGIES.get(skey,skey)}:{x}" for x in no[:2]]
        if s_hits:
            # 복수전략 S면 첫 전략으로 OPEN하고 labels에 모두 기록.
            skey,why=s_hits[0]
            ev=build_event(row,of,skey,why)
            if len(s_hits)>1:
                ev["multi_strategy_s"]=[k for k,_ in s_hits]
                ev["strategy_bucket_labels"]=[STRATEGIES.get(k,k) for k,_ in s_hits]
            events.append(ev); target.append(t)
        else:
            rejects.append({"ticker":t,"score":fnum(row.get("scanner_priority_score")),"reasons":local_reasons[:6],"price":row.get("current_price"),"updated_ts":ts})
            # 정보가 부족해서 S 판정이 막힌 상위 후보만 target에 올린다.
            if any("신선도부족" in x or "체결" in x for x in local_reasons[:4]) and len(target)<60:
                target.append(t)
    events.sort(key=lambda e:fnum(e.get("score")), reverse=True)
    # 실제 OPEN 후보는 S만. 너무 많으면 조건이 약한 것으로 보되 서버 보호상 최신 파일은 상위 30까지만 전달.
    latest=events[:30]
    write_jsonl_atomic(PAPER_LATEST, latest)
    for ev in latest: append_jsonl(PAPER, ev)
    write_jsonl_atomic(SHADOW_LATEST, [])
    save_json(SUMMARY,{"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"s_count":len(events),"paper_latest_count":len(latest),"strategies":{k:sum(1 for e in events if e.get("strategy_key")==k or k in e.get("multi_strategy_s",[])) for k in STRATEGIES},"events":latest[:10]})
    # S 탈락 사유는 2일만 필요한 보조요약. 무거운 B/C 원문 저장 대신 요약만.
    ctr:Dict[str,int]={}
    for r in rejects:
        for reason in r.get("reasons",[])[:4]: ctr[reason]=ctr.get(reason,0)+1
    top=sorted(ctr.items(), key=lambda x:x[1], reverse=True)[:20]
    save_json(REJECT,{"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"evaluated":len(rows),"s_count":len(events),"reject_count":len(rejects),"reason_top":[{"reason":k,"count":v} for k,v in top],"sample":rejects[:25]})
    target=list(dict.fromkeys(target))[:80]
    target_payload={"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"targets":target,"target_count":len(target),"reason":"S 후보와 S 탈락 정보부족 상위 후보만 target"}
    save_json(WS_TARGETS,target_payload); save_json(MICRO_TARGETS,target_payload); save_json(MICRO_URGENT,target_payload)
    sec=now_ts()-st; write_status("running", evaluated=len(rows), s_count=len(events), target_count=len(target), last_sec=round(sec,3))
    return {"eval":len(rows),"s":len(events),"sec":sec}

def main()->None:
    write_status("initializing")
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
