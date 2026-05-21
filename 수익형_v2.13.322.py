#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""수익형_v2.13.321.py

Clean main bot renewal phase v321.
- 메인봇은 무거운 전체시장 스캔/정밀/전략판정을 직접 하지 않는다.
- scanner/feature/orderflow/strategy/review worker가 만든 캐시만 읽는다.
- 실제 paper OPEN은 strategy worker의 S급 후보만 허용한다.
- A/B/C 실시간 후보와 무거운 가상복기는 메인봇 경로에서 제거한다.
"""
from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
import threading
from collections import Counter

try:
    from telegram import Bot, BotCommand
    from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
except Exception:
    Bot = None; BotCommand = None; Updater = None; CommandHandler = None; MessageHandler = None; Filters = None

BOT_VERSION="수익형 v2.13.322"
BASE_DIR=Path(__file__).resolve().parent
TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID=os.getenv("CHAT_ID","").strip()
WORKERS={
    "scanner": {"file":"scanner_worker_v0.3.py", "status":"clean_scanner_status.json"},
    "candle": {"file":"candle_worker_v0.2.py", "status":"clean_candle_status.json"},
    "market": {"file":"market_regime_worker_v0.1.py", "status":"clean_market_regime_status.json"},
    "feature": {"file":"feature_worker_v0.2.py", "status":"clean_feature_status.json"},
    "orderflow": {"file":"orderflow_worker_v0.2.py", "status":"clean_orderflow_status.json"},
    "risk": {"file":"risk_worker_v0.2.py", "status":"clean_risk_status.json"},
    "strategy": {"file":"strategy_worker_v0.4.py", "status":"clean_strategy_worker_status.json"},
    "review": {"file":"review_worker_v0.2.py", "status":"clean_review_worker_status.json"},
}
FILES={
    "brain_status":BASE_DIR/"clean_brain_status.json",
    "brain_runtime":BASE_DIR/"clean_brain_runtime.log",
    "brain_error":BASE_DIR/"clean_brain_error.log",
    "resource":BASE_DIR/"clean_resource_status.json",
    "scanner_market":BASE_DIR/"clean_scanner_market_cache.json",
    "feature":BASE_DIR/"clean_feature_cache.json",
    "orderflow":BASE_DIR/"clean_orderflow_summary_cache.json",
    "strategy_summary":BASE_DIR/"clean_strategy_s_summary.json",
    "candle":BASE_DIR/"clean_candle_cache.json",
    "market_regime":BASE_DIR/"clean_market_regime_cache.json",
    "risk":BASE_DIR/"clean_risk_filter_cache.json",
    "strategy_reject":BASE_DIR/"clean_strategy_s_reject_summary.json",
    "review":BASE_DIR/"clean_review_summary.json",
    "paper_status":BASE_DIR/"paper_bot_status.json",
    "paper_open":BASE_DIR/"paper_bot_open.json",
    "paper_score":BASE_DIR/"paper_bot_score_cache.json",
    "paper_latest":BASE_DIR/"paper_candidates_latest.jsonl",
    "paper_archive":BASE_DIR/"paper_candidates.jsonl",
    "paper_handoff":BASE_DIR/"clean_strategy_paper_handoff_status.json",
    "paperbot_handoff":BASE_DIR/"paper_bot_candidate_handoff_status.json",
    "ws_status":BASE_DIR/"clean_ws_sidecar_status.json",
    "micro_status":BASE_DIR/"clean_bithumb_micro_status.json",
}
START_TS=time.time()

def now_ts()->float: return time.time()
def now_text(ts:Optional[float]=None)->str: return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts or now_ts()))
def fnum(v:Any, default:float=0.0)->float:
    try:
        if v is None or v=="": return default
        return float(str(v).replace(",",""))
    except Exception: return default

def load_json(path:Path, default:Any=None)->Any:
    try:
        if not path.exists(): return default
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception: return default

def save_json(path:Path,obj:Any)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj,ensure_ascii=False,separators=(",",":"),default=str),encoding="utf-8")
    os.replace(tmp,path)

def read_jsonl(path:Path,max_lines:int=1000)->List[Dict[str,Any]]:
    if not path.exists(): return []
    try:
        lines=path.read_text(encoding="utf-8",errors="ignore").splitlines()
        if len(lines)>max_lines: lines=lines[-max_lines:]
        out=[]
        for ln in lines:
            try:
                if ln.strip():
                    o=json.loads(ln)
                    if isinstance(o,dict): out.append(o)
            except Exception: pass
        return out
    except Exception: return []

def line_count(path: Path, max_bytes: int = 2_000_000) -> int:
    try:
        if not path.exists(): return 0
        if path.stat().st_size > max_bytes:
            # 큰 archive는 기본 명령에서 전체를 세지 않는다. tail이 아니라 대략 표시만 한다.
            return -1
        return len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
    except Exception: return 0

def log_runtime(msg:str)->None:
    try:
        with FILES["brain_runtime"].open("a",encoding="utf-8") as f: f.write(f"[{now_text()}] {msg}\n")
    except Exception: pass

def log_error(where:str,exc:BaseException)->None:
    try:
        with FILES["brain_error"].open("a",encoding="utf-8") as f:
            f.write(f"[{now_text()}] {where}: {exc.__class__.__name__}: {exc}\n{traceback.format_exc()[-1500:]}\n")
    except Exception: pass

def age(path:Path)->float:
    try: return max(0.0, now_ts()-path.stat().st_mtime)
    except Exception: return 999999.0

def pid_alive(pid:int)->bool:
    if pid<=0: return False
    try: os.kill(pid,0); return True
    except Exception: return False

def worker_status(name:str)->Dict[str,Any]:
    spec=WORKERS[name]; path=BASE_DIR/spec["status"]
    obj=load_json(path,{}) or {}; pid=int(fnum(obj.get("pid"),default=0)); alive=pid_alive(pid)
    a=age(path)
    state=str(obj.get("state") or "missing") if obj else "missing"
    return {"name":name,"state":state,"pid":pid,"alive":alive,"age":round(a,1),"version":obj.get("version","-"),"last_sec":obj.get("last_sec","-"),"row_count":obj.get("row_count",obj.get("evaluated",obj.get("closed_recent","-"))),"raw":obj}

def ensure_workers()->None:
    # v321: worker 실행/재시작은 가드봇 systemd 전담. 메인봇은 절대 직접 worker를 켜지 않는다.
    return

def resource_snapshot()->Dict[str,Any]:
    try:
        import shutil
        total, used, free = shutil.disk_usage(BASE_DIR)
        mem_total=mem_free=0
        try:
            for ln in Path('/proc/meminfo').read_text().splitlines():
                if ln.startswith('MemTotal:'): mem_total=int(ln.split()[1])*1024
                if ln.startswith('MemAvailable:'): mem_free=int(ln.split()[1])*1024
        except Exception: pass
        load=os.getloadavg() if hasattr(os,'getloadavg') else (0,0,0)
        return {"disk_free_gb":round(free/1024**3,2),"disk_used_pct":round(used/total*100,1),"mem_free_mb":round(mem_free/1024**2,1) if mem_free else 0,"mem_used_pct":round((1-mem_free/mem_total)*100,1) if mem_total and mem_free else 0,"load":[round(x,2) for x in load]}
    except Exception as exc:
        log_error("resource_snapshot",exc); return {}

def update_brain_status(state:str="running")->None:
    """메인봇 heartbeat/status만 저장한다.
    v321부터 worker 실행/재시작은 가드봇 systemd 전담이다.
    """
    workers={k:worker_status(k) for k in WORKERS}
    save_json(FILES["brain_status"],{
        "version":BOT_VERSION,
        "state":state,
        "telegram_state":state,
        "pid":os.getpid(),
        "updated_ts":now_ts(),
        "updated_text":now_text(),
        "uptime_sec":round(now_ts()-START_TS,1),
        "workers":workers,
        "resource":resource_snapshot(),
        "note":"v321 main is cache-only; worker lifecycle is guard/systemd only",
    })

def worker_ok(st:Dict[str,Any])->str:
    state=str(st.get('state') or '').lower()
    a=fnum(st.get('age'), default=999999)
    if state in {'error','failed','stopped','missing','dead'}:
        return '❌'
    if state in {'running','ready','ok','initializing'}:
        return '✅' if a < 45 else '⚠️'
    return '❔'

def line_worker(st:Dict[str,Any], detail:bool=False)->str:
    icon=worker_ok(st)
    base=f"- {icon} {st['name']}: {st.get('version','-')} / {st.get('state')} / age {st.get('age')}s"
    if detail:
        base += f" / rows {st.get('row_count')} / sec {st.get('last_sec')}"
    return base

def health_text()->str:
    # v321: health는 worker/cache 상태만 빠르게 읽는다. worker 시작/재시작/직접계산 금지.
    update_brain_status('running')
    workers=[worker_status(k) for k in WORKERS]
    res=resource_snapshot()
    paper=load_json(FILES['paper_status'],{}) or {}
    ws=load_json(FILES['ws_status'],{}) or {}
    micro=load_json(FILES['micro_status'],{}) or {}
    strat=load_json(FILES['strategy_summary'],{}) or {}
    reject=load_json(FILES['strategy_reject'],{}) or {}
    good=sum(1 for w in workers if worker_ok(w)=='✅')
    warn=sum(1 for w in workers if worker_ok(w)=='⚠️')
    bad=sum(1 for w in workers if worker_ok(w)=='❌')
    lines=[
        "🧭 건강상태 /health",
        f"✅ 메인봇 {BOT_VERSION} / clean worker hub v322 / uptime {int(now_ts()-START_TS)}s",
        f"- worker: ✅ {good} / ⚠️ {warn} / ❌ {bad}",
        f"- paper: {paper.get('version','-')} / OPEN {paper.get('open_count', paper.get('open_total','-'))} / age {age(FILES['paper_status']):.1f}s",
        f"- WS: age {age(FILES['ws_status']):.1f}s / cache {ws.get('cache', ws.get('cache_count','-'))} / fresh {ws.get('fresh','-')}",
        f"- micro: age {age(FILES['micro_status']):.1f}s / targets {micro.get('targets','-')} / fresh {micro.get('fresh','-')}",
        f"- S 후보 {strat.get('s_count',0)} / 평가 {reject.get('evaluated','-')} / 탈락 {reject.get('reject_count','-')}",
        f"- 디스크 {res.get('disk_used_pct','-')}% / 남음 {res.get('disk_free_gb','-')}GB / 메모리 {res.get('mem_used_pct','-')}% / load {res.get('load','-')}",
        "",
        "[worker 요약]",
    ]
    lines += [line_worker(w, detail=False) for w in workers]
    lines += ["", "[원칙] 메인봇은 worker cache만 읽음. worker 실행/재시작은 가드봇 systemd 전담."]
    return "\n".join(lines)

def workers_text()->str:
    update_brain_status('running')
    workers=[worker_status(k) for k in WORKERS]
    lines=["🧩 worker 상세 /workers", "- 메인봇은 상태파일만 읽고 worker를 직접 실행하지 않음."]
    lines += [line_worker(w, detail=True) for w in workers]
    return "\n".join(lines)

def score_text()->str:
    cache=load_json(FILES['paper_score'],{}) or {}
    lines=["📊 전략별 스코어 /score", "- 목적: 전략별 S급 실제 paper 성과만 본다. worker 상태는 /health."]
    if cache:
        lines.append(f"- 캐시 age {age(FILES['paper_score']):.1f}s / 기준 {cache.get('basis', cache.get('note','recent'))}")
        for k in ['grade_stats','summary','scores']:
            if isinstance(cache.get(k),dict):
                for name,val in cache[k].items():
                    if isinstance(val,dict):
                        lines.append(f"- {name}: {val.get('count',val.get('n','-'))}전 / 승률 {val.get('win_rate',val.get('win_rate_pct','-'))} / 합산 {val.get('sum',val.get('total_pct',val.get('profit_sum','-')))}")
                break
    strat=load_json(FILES['strategy_summary'],{}) or {}
    hand=load_json(FILES['paper_handoff'],{}) or {}
    pbh=load_json(FILES['paperbot_handoff'],{}) or {}
    if strat:
        lines += ["", "[현재 S 후보 / handoff]"]
        lines.append(f"- S 후보 {strat.get('s_count',0)} / strategy latest {strat.get('paper_latest_count',0)} / archive append {strat.get('archive_appended','-')}")
        lines.append(f"- strategy handoff latest {hand.get('paper_latest_count','-')} / 새S {hand.get('new_s_count','-')} / TTL {hand.get('ttl_sec','-')}s")
        lines.append(f"- paper_bot 병합 fresh {pbh.get('merged_fresh','-')} / latest {pbh.get('latest_lines','-')} / archive창 {pbh.get('archive_window','-')}")
        for k,v in (strat.get('strategies') or {}).items(): lines.append(f"- {k}: {v}")
    return "\n".join(lines)

def quality_text()->str:
    reject=load_json(FILES['strategy_reject'],{}) or {}; strat=load_json(FILES['strategy_summary'],{}) or {}
    hand=load_json(FILES['paper_handoff'],{}) or {}
    pbh=load_json(FILES['paperbot_handoff'],{}) or {}
    latest_lines=line_count(FILES['paper_latest'])
    archive_lines=line_count(FILES['paper_archive'])
    lines=["🔍 후보품질 /quality · S 후보와 탈락사유", f"- 실제 OPEN: 전략별 S만. A/B/C 실시간 후보표 제거.", f"- S 후보 {strat.get('s_count',0)} / 평가 {reject.get('evaluated','-')} / 탈락 {reject.get('reject_count','-')}", f"- paper 전달: latest파일 {latest_lines}줄 / archive {'대형' if archive_lines<0 else str(archive_lines)+'줄'} / strategy handoff {hand.get('paper_latest_count','-')} / paper_bot 병합 {pbh.get('merged_fresh','-')}"]
    lines += ["", "[S 후보 TOP]"]
    evs=strat.get('events') if isinstance(strat.get('events'),list) else []
    if evs:
        for e in evs[:8]: lines.append(f"- ✅ {e.get('ticker')} / {e.get('strategy_bucket_primary_label', e.get('strategy_name'))} / 점수 {e.get('score')} / 근거 {', '.join(e.get('reasons',[])[:4])}")
    else: lines.append("- 없음")
    lines += ["", "[S 탈락 사유 TOP]"]
    tops=reject.get('reason_top') if isinstance(reject.get('reason_top'),list) else []
    lines += [f"- {x.get('reason')}: {x.get('count')}" for x in tops[:12]] or ["- 아직 없음"]
    return "\n".join(lines)

def strategy_watch_text()->str:
    strat=load_json(FILES['strategy_summary'],{}) or {}
    hand=load_json(FILES['paper_handoff'],{}) or {}
    lines=["👀 전략 감시 /strategy_watch", "- 전략별 S만 감시. A/B/C는 기본화면에서 제외.", f"- handoff: latest {hand.get('paper_latest_count','-')} / 새S {hand.get('new_s_count','-')} / append {hand.get('archive_appended','-')}"]
    for k,v in (strat.get('strategies') or {}).items(): lines.append(f"- {k}: S {v}")
    evs=strat.get('events') if isinstance(strat.get('events'),list) else []
    lines += ["", "[OPEN 전달 후보]"]
    if evs:
        for e in evs[:10]: lines.append(f"- ✅ {e.get('ticker')} / {e.get('strategy_bucket_primary_label')} / {e.get('score')} / {', '.join(e.get('reasons',[])[:3])}")
    else: lines.append("- 없음")
    return "\n".join(lines)

def errorlog_text()->str:
    path=FILES['brain_error']
    if not path.exists(): return "🧯 오류로그 /errorlog\n\n✅ 새 실행 중 오류 없음"
    txt=path.read_text(encoding='utf-8',errors='ignore')[-2000:]
    recent=[ln for ln in txt.splitlines() if 'v2.13.319' in ln or '[' in ln]
    return "🧯 오류로그 /errorlog\n\n" + ("✅ 새 실행 중 오류 없음\n- 과거 오류 파일 있음 / 자세히: /errorlog_full" if not recent else "⚠️ 최근 오류 흔적\n"+"\n".join(recent[-10:]))

def open_text()->str:
    obj=load_json(FILES['paper_open'],{}) or {}
    lines=["📂 OPEN 요약 /popen_short"]
    if not obj: return "\n".join(lines+["- OPEN 없음"])
    for _,p in list(obj.items())[:12]: lines.append(f"- {p.get('ticker')} / {p.get('strategy_name',p.get('strategy_key','-'))} / {p.get('profit_pct',p.get('current_profit_pct','-'))}%")
    return "\n".join(lines)

COMMANDS={'health':health_text,'core':health_text,'workers':workers_text,'score':score_text,'quality':quality_text,'strategy_watch':strategy_watch_text,'errorlog':errorlog_text,'popen_short':open_text}

def send(update, text:str)->None:
    try:
        # Telegram hard limit 보호
        if len(text)>3900: text=text[:3850]+"\n…(잘림: full 명령에서 확인)"
        update.message.reply_text(text)
    except Exception as exc: log_error('telegram_send',exc)

def make_handler(name:str):
    def h(update, context):
        try:
            txt = getattr(update.message, 'text', '') or ''
            lines = [x.strip().lstrip('/') for x in txt.splitlines() if x.strip().startswith('/')]
            if len(lines) >= 2:
                context.args = lines[:8]
                return batch_handler(update, context)
            send(update, COMMANDS[name]())
        except Exception as exc:
            log_error(f'cmd_{name}',exc); send(update, f"❌ /{name} 오류: {exc.__class__.__name__}: {exc}")
    return h

def batch_handler(update, context):
    cmds=context.args or ['health','score','quality','strategy_watch','errorlog']
    cmds=[str(x).strip().lstrip('/') for x in cmds[:8] if str(x).strip()]
    st=time.time(); sent=0; timings=[]
    try:
        send(update, f"📦 자동 묶음 명령 접수\n- 실행 {len(cmds)}개\n- v322: 명령별 전송 유지 + handoff 상태 표시")
    except Exception:
        pass
    total=len(cmds)
    for raw in cmds:
        name=raw.strip().lstrip('/')
        fn=COMMANDS.get(name)
        if not fn:
            continue
        t=time.time()
        try:
            body=fn()
            ok="OK"
        except Exception as exc:
            body=f"❌ /{name} 오류 {exc.__class__.__name__}: {exc}"
            ok="ERR"
            log_error(f'batch_{name}', exc)
        sec=time.time()-t
        sent += 1
        timings.append(f"- {'✅' if ok=='OK' else '❌'} /{name}: {sec:.2f}s / {ok}")
        send(update, f"[{sent}/{total}] /{name} ({sec:.2f}s / {ok})\n{body}")
    send(update, "🧾 자동 묶음 시간표 · v322 명령별 전송\n"+"\n".join(timings)+f"\n- 합계 {time.time()-st:.2f}s")

def text_router(update, context):
    try:
        txt=update.message.text or ''
        lines=[x.strip().lstrip('/') for x in txt.splitlines() if x.strip().startswith('/')]
        if len(lines)>=2:
            context.args=lines[:8]; return batch_handler(update, context)
    except Exception as exc: log_error('text_router',exc)

def set_commands(updater):
    try:
        updater.bot.set_my_commands([BotCommand('health','메인/worker/paper 빠른상태'),BotCommand('workers','worker 상세 상태'),BotCommand('score','전략별 S급 성과'),BotCommand('quality','S 후보와 탈락사유'),BotCommand('strategy_watch','전략별 S 감시'),BotCommand('errorlog','오류로그'),BotCommand('batch','묶음 확인')])
    except Exception as exc: log_error('set_commands',exc)

def startup_notify(updater):
    try:
        if not CHAT_ID:
            return
        text=(
            f"✅ 메인봇 시작 완료\n"
            f"- 버전: {BOT_VERSION}\n"
            f"- 구조: clean worker hub v322\n"
            f"- 명령: /health /score /quality /strategy_watch /errorlog"
        )
        updater.bot.send_message(chat_id=CHAT_ID, text=text)
    except Exception as exc:
        log_error('startup_notify', exc)

def heartbeat_loop():
    while True:
        try:
            update_brain_status('running')
        except Exception as exc:
            log_error('heartbeat', exc)
        time.sleep(10)

def main():
    log_runtime(f"telegram_state=initializing version={BOT_VERSION}")
    update_brain_status('initializing')
    if not TELEGRAM_TOKEN:
        print('TELEGRAM_TOKEN missing; worker hub status only')
        while True:
            update_brain_status('running_no_telegram'); time.sleep(5)
    updater=Updater(token=TELEGRAM_TOKEN, use_context=True)
    dp=updater.dispatcher
    for name in COMMANDS: dp.add_handler(CommandHandler(name, make_handler(name)))
    dp.add_handler(CommandHandler('batch', batch_handler))
    if MessageHandler is not None and Filters is not None: dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_router))
    set_commands(updater)
    log_runtime(f"telegram_state=commands_installed version={BOT_VERSION}")
    update_brain_status('running')
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    updater.start_polling(drop_pending_updates=True)
    startup_notify(updater)
    log_runtime(f"{BOT_VERSION} telegram polling_started")
    log_runtime("telegram_state=running error=-")
    updater.idle()

if __name__=='__main__': main()
