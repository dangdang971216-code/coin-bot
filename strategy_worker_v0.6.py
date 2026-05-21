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

VERSION="strategy_worker_v0.6"
INTERVAL_SEC=float(os.getenv("STRATEGY_WORKER_INTERVAL_SEC","2.0"))
STATUS=BASE_DIR/"clean_strategy_worker_status.json"; ERROR=BASE_DIR/"clean_strategy_worker_error.log"
FEATURE=BASE_DIR/"clean_feature_cache.json"; ORDERFLOW=BASE_DIR/"clean_orderflow_summary_cache.json"; REGIME=BASE_DIR/"clean_market_regime_cache.json"; RISK=BASE_DIR/"clean_risk_filter_cache.json"
PAPER=BASE_DIR/"paper_candidates.jsonl"; PAPER_LATEST=BASE_DIR/"paper_candidates_latest.jsonl"; SUMMARY=BASE_DIR/"clean_strategy_s_summary.json"; REJECT=BASE_DIR/"clean_strategy_s_reject_summary.json"; HANDOFF=BASE_DIR/"clean_strategy_paper_handoff_status.json"; WS_TARGETS=BASE_DIR/"clean_ws_targets.json"; MICRO_TARGETS=BASE_DIR/"clean_micro_targets.json"; MICRO_URGENT=BASE_DIR/"clean_micro_urgent_targets.json"; PRIORITY_REQ=BASE_DIR/"clean_strategy_priority_requests.json"; TARGET_ROUTER_QUEUE=BASE_DIR/"clean_target_router_queue.json"
CANDIDATE_TTL_SEC=float(os.getenv("STRATEGY_PAPER_CANDIDATE_TTL_SEC","120"))
STRATEGIES={"money_reaccel_s":"돈흐름 재가속 S","leader_momentum_s":"주도추세 지속 S","breakout_early_s":"돌파 초입 S","sweep_vwap_recovery_s":"저점 VWAP 회복 S","surge_rebreak_s":"급등 후 재돌파 S"}
def write_status(state:str, **extra:Any)->None: save_json(STATUS,{"version":VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),**extra})

def _event_created_ts(ev: Dict[str, Any]) -> float:
    return fnum(ev.get("created_at"), ev.get("candidate_created_at"), ev.get("source_created_at"), ev.get("detected_ts"), ev.get("event_ts"), ev.get("ts"), ev.get("timestamp"), default=0.0)

def _normalize_paper_event(ev: Dict[str, Any], ttl_sec: float = CANDIDATE_TTL_SEC) -> Dict[str, Any]:
    row = dict(ev or {})
    ts = _event_created_ts(row) or now_ts()
    ticker = ticker_norm(row.get("ticker") or row.get("market") or row.get("symbol"))
    skey = str(row.get("strategy_key") or row.get("strategy_bucket_primary") or "strategy_s")
    scan_id = row.get("scan_id") or row.get("source_scan_id") or f"strategy_s_{int(ts // 60)}"
    eid = row.get("event_id") or row.get("id") or row.get("event_key") or f"{ticker}:{skey}:{int(ts // 60)}"
    row.update({
        "ticker": ticker,
        "market": f"KRW-{ticker}" if ticker else row.get("market"),
        "symbol": f"{ticker}_KRW" if ticker else row.get("symbol"),
        "created_at": ts,
        "candidate_created_at": ts,
        "source_created_at": ts,
        "detected_ts": ts,
        "event_ts": ts,
        "created_at_text": row.get("created_at_text") or now_text(ts),
        "detected_at_text": row.get("detected_at_text") or now_text(ts),
        "updated_ts": now_ts(),
        "expires_at": row.get("expires_at") or (ts + ttl_sec),
        "event_id": eid,
        "event_key": eid,
        "scan_id": scan_id,
        "lane": "strict",
        "paper_bot_open": True,
        "open_eligible": True,
        "trade_ready": True,
        "candidate_grade": "S",
        "grade": "S",
    })
    ctx = row.get("entry_context") if isinstance(row.get("entry_context"), dict) else {}
    # paper_bot의 pre-open check가 top-level 또는 entry_context를 모두 볼 수 있게 복제한다.
    for k in ("ws_fresh", "micro_fresh", "spread_pct", "buy_ratio", "sell_pressure", "orderflow_score"):
        if k in ctx and row.get(k) in (None, ""):
            row[k] = ctx.get(k)
    return row

def _paper_event_fresh(ev: Dict[str, Any], nowv: Optional[float] = None) -> bool:
    nowv = nowv or now_ts()
    exp = fnum(ev.get("expires_at"), default=0.0)
    ts = _event_created_ts(ev)
    if exp > 0:
        return exp >= nowv
    return ts > 0 and (nowv - ts) <= CANDIDATE_TTL_SEC

def _paper_dedupe_key(ev: Dict[str, Any]) -> str:
    t = ticker_norm(ev.get("ticker") or ev.get("market") or ev.get("symbol"))
    sk = str(ev.get("strategy_key") or ev.get("strategy_bucket_primary") or "strategy_s")
    return f"{t}:{sk}"

def persist_paper_handoff(events: List[Dict[str, Any]], evaluated: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """S 후보 handoff를 휘발성 latest 한 번으로 날리지 않는다.
    - 새 S 후보에는 created_at/detected_ts/expires_at/event_id를 고정한다.
    - 새 루프에서 S가 0이어도 TTL 내 기존 S 후보는 latest에 유지한다.
    - archive paper_candidates.jsonl에는 신규 event_id만 append한다.
    """
    nowv = now_ts()
    new_rows = [_normalize_paper_event(e) for e in (events or [])]
    old_latest = [_normalize_paper_event(e) for e in read_jsonl(PAPER_LATEST, max_lines=120)]
    old_archive = [_normalize_paper_event(e) for e in read_jsonl(PAPER, max_lines=500)]
    merged: Dict[str, Dict[str, Any]] = {}
    # 오래된 후보가 새 후보를 덮지 않게 old 먼저, new 나중에 넣는다.
    for ev in old_archive + old_latest + new_rows:
        if not _paper_event_fresh(ev, nowv):
            continue
        key = _paper_dedupe_key(ev)
        if not key:
            continue
        prev = merged.get(key)
        if (prev is None) or fnum(ev.get("score"), default=0.0) >= fnum(prev.get("score"), default=0.0) or _event_created_ts(ev) >= _event_created_ts(prev):
            merged[key] = ev
    latest = sorted(merged.values(), key=lambda e: (fnum(e.get("score"), default=0.0), _event_created_ts(e)), reverse=True)[:40]
    write_jsonl_atomic(PAPER_LATEST, latest)
    recent_ids = {str(r.get("event_id") or r.get("event_key") or "") for r in read_jsonl(PAPER, max_lines=800)}
    appended = 0
    for ev in new_rows:
        eid = str(ev.get("event_id") or ev.get("event_key") or "")
        if eid and eid not in recent_ids:
            append_jsonl(PAPER, ev)
            recent_ids.add(eid)
            appended += 1
    save_json(HANDOFF, {
        "version": VERSION,
        "updated_ts": nowv,
        "updated_text": now_text(nowv),
        "evaluated": evaluated,
        "new_s_count": len(new_rows),
        "paper_latest_count": len(latest),
        "archive_appended": appended,
        "latest_tickers": [e.get("ticker") for e in latest[:20]],
        "ttl_sec": CANDIDATE_TTL_SEC,
        "note": "v0.6: 정보요청은 priority queue로 target_router에 넘김. handoff TTL 유지.",
    })
    return latest, appended

def risk_bad(t:str)->List[str]:
    r=load_json(RISK,{}) or {}; cd=(r.get("cooldown") or {}) if isinstance(r,dict) else {}
    out=[]
    if t in cd: out.append("최근반복손실/하드손실")
    if t in set(r.get("open_tickers",[]) if isinstance(r,dict) else []): out.append("이미OPEN중")
    return out
def common_bad(row:Dict[str,Any], of:Dict[str,Any])->Tuple[List[str], List[str]]:
    """S 최종판정 공통 차단과 정보보강 대기를 분리한다.
    - WS/micro가 없다고 곧바로 전략 실패로 보지 않는다.
    - 단, 최종 S 확정에는 fresh 정보가 필요하므로 info_wait로 target 요청한다.
    - micro가 stale/missing이면 spread/buy_ratio/sell_pressure를 0값으로 오판하지 않는다.
    """
    hard=[]; info=[]; t=ticker_norm(row.get("ticker")); hard+=risk_bad(t)
    if row.get("major_watch"): hard.append("대형주는시장참고")
    ws_fresh=bool(of.get("ws_fresh")); micro_fresh=bool(of.get("micro_fresh"))
    if not ws_fresh: info.append("WS정보보강대기")
    if not micro_fresh: info.append("micro정보보강대기")
    if micro_fresh:
        spread=fnum(of.get("spread_pct"),default=0)
        buy=fnum(of.get("buy_ratio"),default=0)
        if spread>0.28: hard.append(f"스프레드넓음 {spread:.2f}%")
        if buy and buy<0.58: hard.append(f"매수체결약함 {buy:.2f}")
        if of.get("sell_pressure"): hard.append("매도벽/매도체결압력")
    if fnum(row.get("turnover_24h"))<250_000_000: hard.append("거래대금부족")
    if row.get("long_upper_wick"): hard.append("윗꼬리위험")
    if fnum(row.get("day_pos_pct"),default=50)>94 and fnum(row.get("change_5m"))<0.2: hard.append("고점끝물위험")
    return hard[:8], info[:4]
def eval_money(r,o):
    no=[]
    if fnum(r.get("change_3m"))<0.25 or fnum(r.get("change_5m"))<0.35: no.append("3/5분재가속부족")
    if fnum(r.get("vwap_gap_pct"))<-0.05 or fnum(r.get("ema5_gap_pct"))<-0.08: no.append("VWAP/EMA유지부족")
    if not r.get("volume_expanding"): no.append("캔들거래량확장부족")
    # micro가 fresh일 때만 매수체결비를 전략 실패로 본다. fresh가 없으면 정보보강대기로 분리한다.
    if o.get("micro_fresh") and fnum(o.get("buy_ratio"))<0.65: no.append("매수체결우세부족")
    return (not no, ["돈흐름재가속","3/5분재가속","VWAP/EMA유지","체결우세"], no)
def eval_leader(r,o):
    no=[]
    if fnum(r.get("change_15m"))<0.65 or fnum(r.get("change_30m"))<0.65: no.append("15/30분주도부족")
    if fnum(r.get("relative_strength_pct"))<0.3: no.append("시장대비강도부족")
    if fnum(r.get("vwap_gap_pct"))<0 or fnum(r.get("ema5_gap_pct"))<0: no.append("VWAP/EMA위치부족")
    if r.get("market_mode") == "약함": no.append("장세약함")
    return (not no, ["15/30분주도","시장대비강함","VWAP/EMA위"], no)
def eval_breakout(r,o):
    no=[]
    if not r.get("breakout_hint"): no.append("돌파캔들미확인")
    if fnum(r.get("recent_high_gap_pct"))<-0.15: no.append("돌파후유지부족")
    if fnum(r.get("change_15m"))>5.0 and not r.get("volume_expanding"): no.append("끝물추격위험")
    return (not no, ["돌파초입","거래량확장","고점돌파유지"], no)
def eval_sweep(r,o):
    no=[]
    if not r.get("sweep_reclaim_hint"): no.append("저점쓸림회복미확인")
    if fnum(r.get("vwap_gap_pct"))<-0.05: no.append("VWAP회복부족")
    if fnum(r.get("recent_low_rebound_pct"))<0.4: no.append("저점방어반등부족")
    return (not no, ["저점이탈실패","VWAP회복","매수회복"], no)
def eval_surge(r,o):
    no=[]
    if fnum(r.get("change_15m"))<1.2: no.append("1차상승부족")
    if fnum(r.get("change_3m"))<0.15: no.append("재돌파시도부족")
    if fnum(r.get("recent_high_gap_pct"))<-0.25: no.append("고점재접근부족")
    if r.get("long_upper_wick"): no.append("윗꼬리반락위험")
    return (not no, ["급등후재돌파","얕은눌림","체결재상승"], no)
def build_event(row, of, skey, reasons):
    ts = now_ts()
    t = ticker_norm(row.get("ticker"))
    price = fnum(row.get("current_price"), row.get("price"), default=0)
    label = STRATEGIES.get(skey, skey)
    of_ctx = {k: of.get(k) for k in ["spread_pct", "buy_ratio", "sell_pressure", "ws_fresh", "micro_fresh", "orderflow_score", "ws_age_sec", "micro_age_sec"]}
    base_ctx = {k: row.get(k) for k in ["change_3m", "change_5m", "change_15m", "change_30m", "vwap_gap_pct", "ema5_gap_pct", "relative_strength_pct", "turnover_24h", "market_mode", "volume_expanding", "breakout_hint", "sweep_reclaim_hint"]}
    eid = f"{t}:{skey}:{int(ts // 60)}"
    ev = {
        "event_id": eid,
        "event_key": eid,
        "created_at": ts,
        "candidate_created_at": ts,
        "source_created_at": ts,
        "detected_ts": ts,
        "event_ts": ts,
        "updated_ts": ts,
        "expires_at": ts + CANDIDATE_TTL_SEC,
        "created_at_text": now_text(ts),
        "detected_at_text": now_text(ts),
        "scan_id": f"strategy_s_{int(ts // 60)}",
        "ticker": t,
        "market": f"KRW-{t}",
        "symbol": f"{t}_KRW",
        "strategy_key": skey,
        "strategy": label,
        "strategy_name": label,
        "paper_strategy_key": skey,
        "paper_strategy_label": label,
        "strategy_bucket_primary": skey,
        "strategy_bucket_primary_label": label,
        "candidate_grade": "S",
        "grade": "S",
        "candidate_grade_label": "✅ S급 자동매매 상위",
        "paper_bot_open": True,
        "open_eligible": True,
        "trade_ready": True,
        "lane": "strict",
        "current_price": price,
        "entry_price": price,
        "detected_price": price,
        "score": round(13.0 + len(reasons) * 0.9 + fnum(of.get("orderflow_score")) * 0.15, 2),
        "take_profit_pct": 1.80,
        "extended_take_profit_pct": 2.80,
        "protect_trigger_pct": 1.00,
        "protect_floor_pct": 0.45,
        "stop_loss_pct": -0.45,
        "time_exit_minutes": 60,
        "reasons": reasons[:6],
        "quality_risk_tags": [],
        "entry_context": {**base_ctx, **of_ctx, "candidate_created_at": ts, "event_id": eid},
        "strategy_reasons": reasons[:6],
        "brain_version": VERSION,
        # paper_bot이 top-level freshness도 볼 수 있게 복제한다.
        "ws_fresh": bool(of.get("ws_fresh")),
        "micro_fresh": bool(of.get("micro_fresh")),
        "spread_pct": of.get("spread_pct"),
        "buy_ratio": of.get("buy_ratio"),
        "sell_pressure": of.get("sell_pressure"),
        "orderflow_score": of.get("orderflow_score"),
    }
    return ev
def _priority_score(row: Dict[str, Any], core_hits: List[Tuple[str, List[str]]], info_bad: List[str], hard_bad: List[str]) -> float:
    score = 0.0
    score += fnum(row.get("scanner_priority_score"), default=0.0)
    score += min(30.0, max(0.0, fnum(row.get("change_3m"), default=0.0) * 8))
    score += min(30.0, max(0.0, fnum(row.get("change_5m"), default=0.0) * 6))
    score += min(25.0, max(0.0, fnum(row.get("change_15m"), default=0.0) * 3))
    score += min(20.0, max(0.0, fnum(row.get("turnover_24h"), default=0.0) / 400_000_000))
    score += 25.0 * len(core_hits)
    if info_bad:
        score += 15.0
    if hard_bad:
        score -= 35.0
    return round(score, 3)


def _make_priority_request(t: str, row: Dict[str, Any], bucket: str, priority: float, need: List[str], core_hits: List[Tuple[str, List[str]]], reasons: List[str]) -> Dict[str, Any]:
    return {
        "ticker": t,
        "bucket": bucket,
        "priority": round(priority, 3),
        "need": need,
        "core_strategies": [k for k, _ in core_hits],
        "core_strategy_labels": [STRATEGIES.get(k, k) for k, _ in core_hits],
        "reasons": reasons[:8],
        "score": fnum(row.get("scanner_priority_score"), default=0.0),
        "change_3m": fnum(row.get("change_3m"), default=0.0),
        "change_5m": fnum(row.get("change_5m"), default=0.0),
        "change_15m": fnum(row.get("change_15m"), default=0.0),
        "turnover_24h": fnum(row.get("turnover_24h"), default=0.0),
        "updated_ts": now_ts(),
        "source": "strategy_worker",
    }


def run_once()->Dict[str,Any]:
    st=now_ts(); ts=now_ts(); write_status("running", phase="evaluating", evaluated=0, s_count=0, target_count=0, last_sec=0)
    fobj=load_json(FEATURE,{}) or {}; rows=fobj.get("rows") if isinstance(fobj,dict) else []
    if not isinstance(rows,list): rows=[]
    ofmap=map_rows(load_json(ORDERFLOW,{}) or {})
    evals=[("money_reaccel_s",eval_money),("leader_momentum_s",eval_leader),("breakout_early_s",eval_breakout),("sweep_vwap_recovery_s",eval_sweep),("surge_rebreak_s",eval_surge)]
    events=[]; rejects=[]; priority_requests=[]; strat_counts={k:0 for k in STRATEGIES}; info_pending=0; near_s_info_pending=0; hard_blocked=0
    priority_buckets={}
    for row in rows:
        t=ticker_norm(row.get("ticker"));
        if not t: continue
        of=ofmap.get(t,{}) or {}
        hard_bad, info_bad = common_bad(row,of)
        s_hits=[]; reasons=[]; core_hits=[]; core_fail=[]
        for skey,fn in evals:
            ok,why,no=fn(row,of)
            if ok:
                core_hits.append((skey,why))
                if not hard_bad and not info_bad:
                    s_hits.append((skey,why)); strat_counts[skey]+=1
            else:
                core_fail += [f"{STRATEGIES.get(skey,skey)}:{x}" for x in no[:2]]
        need=[]
        if "WS정보보강대기" in info_bad: need.append("ws")
        if "micro정보보강대기" in info_bad: need.append("micro")
        pscore=_priority_score(row, core_hits, info_bad, hard_bad)
        if info_bad:
            info_pending += 1
        if s_hits:
            ev=build_event(row,of,s_hits[0][0],s_hits[0][1])
            if len(s_hits)>1:
                ev["multi_strategy_s"]=[k for k,_ in s_hits]; ev["strategy_bucket_labels"]=[STRATEGIES.get(k,k) for k,_ in s_hits]
            events.append(ev)
            priority_requests.append(_make_priority_request(t,row,"s_candidate",130+pscore, ["ws","micro"], s_hits, ["S후보유지"]))
            priority_buckets["s_candidate"]=priority_buckets.get("s_candidate",0)+1
        elif core_hits and info_bad and not hard_bad:
            near_s_info_pending += 1
            reasons += [f"정보보강대기:{x}" for x in info_bad[:3]] + [f"S근접:{STRATEGIES.get(k,k)}" for k,_ in core_hits[:2]]
            priority_requests.append(_make_priority_request(t,row,"near_s_info_wait",110+pscore, need, core_hits, reasons))
            priority_buckets["near_s_info_wait"]=priority_buckets.get("near_s_info_wait",0)+1
        elif core_hits and hard_bad:
            hard_blocked += 1
            reasons += hard_bad[:4] + [f"S근접차단:{STRATEGIES.get(k,k)}" for k,_ in core_hits[:2]]
            # hard block은 정보 target 최우선에 태우지 않는다. 단 복기용 낮은 우선순위는 남긴다.
            if info_bad:
                priority_requests.append(_make_priority_request(t,row,"hard_blocked_info_wait",35+pscore, need, core_hits, reasons))
                priority_buckets["hard_blocked_info_wait"]=priority_buckets.get("hard_blocked_info_wait",0)+1
        else:
            reasons += ([f"정보보강대기:{x}" for x in info_bad[:2]] if info_bad else []) + hard_bad[:2] + core_fail[:4]
            if info_bad:
                bucket = "info_wait_high" if pscore >= 45 else "normal_info_wait"
                priority_requests.append(_make_priority_request(t,row,bucket, (65 if bucket=="info_wait_high" else 25)+pscore, need, core_hits, reasons))
                priority_buckets[bucket]=priority_buckets.get(bucket,0)+1
        if not s_hits:
            rejects.append({"ticker":t,"score":fnum(row.get("scanner_priority_score")),"priority":pscore,"reasons":reasons[:8],"price":row.get("current_price"),"updated_ts":ts,"info_pending":bool(info_bad),"near_s_info_pending":bool(core_hits and info_bad and not hard_bad)})
    events.sort(key=lambda e:fnum(e.get("score")), reverse=True)
    latest, appended = persist_paper_handoff(events, evaluated=len(rows))
    ctr={}
    for r in rejects:
        for reason in r.get("reasons",[])[:5]: ctr[reason]=ctr.get(reason,0)+1
    top=sorted(ctr.items(), key=lambda x:x[1], reverse=True)[:20]
    # 고정 개수로 자르지 않는다. target_router가 우선순위만 정하고 소비 측 rate limit로 서버를 보호한다.
    priority_requests = sorted(priority_requests, key=lambda x: fnum(x.get("priority"), default=0), reverse=True)
    target = list(dict.fromkeys([r.get("ticker") for r in priority_requests if r.get("ticker")]))
    save_json(PRIORITY_REQ,{"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"request_count":len(priority_requests),"targets":target,"requests":priority_requests,"buckets":priority_buckets,"note":"v0.6: 고정 개수 제한 없이 정보보강 우선순위만 세분화. target_router가 WS/micro target을 배차."})
    tq=load_json(TARGET_ROUTER_QUEUE,{}) or {}
    save_json(SUMMARY,{"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"s_count":len(events),"paper_latest_count":len(latest),"archive_appended":appended,"strategies":strat_counts,"events":latest[:12],"info_pending_count":info_pending,"near_s_info_pending":near_s_info_pending,"hard_blocked_near_s":hard_blocked,"priority_request_count":len(priority_requests),"priority_buckets":priority_buckets,"target_count":len(target),"router_target_count":tq.get("target_count","-"),"router_fresh_recovered":tq.get("fresh_recovered","-"),"router_need_ws":tq.get("need_ws","-"),"router_need_micro":tq.get("need_micro","-")})
    save_json(REJECT,{"version":VERSION,"updated_ts":ts,"updated_text":now_text(ts),"evaluated":len(rows),"s_count":len(events),"reject_count":len(rejects),"info_pending_count":info_pending,"near_s_info_pending":near_s_info_pending,"hard_blocked_near_s":hard_blocked,"priority_request_count":len(priority_requests),"priority_buckets":priority_buckets,"target_count":len(target),"router_target_count":tq.get("target_count","-"),"router_fresh_recovered":tq.get("fresh_recovered","-"),"reason_top":[{"reason":k,"count":v} for k,v in top],"sample":rejects[:30]})
    # v0.6부터 clean_ws_targets/clean_micro_targets는 target_router_worker가 쓴다. 여기서 덮어쓰지 않는다.
    sec=now_ts()-st; write_status("running", evaluated=len(rows), s_count=len(events), paper_latest_count=len(latest), info_pending_count=info_pending, near_s_info_pending=near_s_info_pending, priority_request_count=len(priority_requests), target_count=len(target), router_target_count=tq.get("target_count","-"), last_sec=round(sec,3)); return {"eval":len(rows),"s":len(events),"paper_latest":len(latest),"priority_requests":len(priority_requests)}
def main()->None:
    write_status("running", phase="boot", evaluated=0, s_count=0, target_count=0, last_sec=0)
    while True:
        try: run_once()
        except KeyboardInterrupt: write_status("stopping"); raise
        except Exception as exc: append_error(ERROR,"loop",exc); write_status("error", error=f"{exc.__class__.__name__}: {exc}")
        time.sleep(max(0.5,INTERVAL_SEC))
if __name__=="__main__": main()
