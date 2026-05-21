#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""수익형_v2.13.319.py

Clean main bot renewal phase.
- 메인봇은 무거운 전체시장 스캔/정밀/전략판정을 직접 하지 않는다.
- scanner/feature/orderflow/strategy/review worker가 만든 캐시만 읽는다.
- 실제 paper OPEN은 strategy worker의 S급 후보만 허용한다.
- A/B/C 실시간 후보와 무거운 가상복기는 메인봇 경로에서 제거한다.
"""
from __future__ import annotations
import json, os, sys, time, subprocess, signal, traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from collections import Counter

try:
    from telegram import Bot, BotCommand
    from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
except Exception:
    Bot = None; BotCommand = None; Updater = None; CommandHandler = None; MessageHandler = None; Filters = None

BOT_VERSION="수익형 v2.13.319"
BASE_DIR=Path(__file__).resolve().parent
TELEGRAM_TOKEN=os.getenv("TELEGRAM_TOKEN","").strip()
CHAT_ID=os.getenv("CHAT_ID","").strip()
WORKERS={
    "scanner": {"file":"scanner_worker_v0.2.py", "status":"clean_scanner_status.json"},
    "feature": {"file":"feature_worker_v0.1.py", "status":"clean_feature_status.json"},
    "orderflow": {"file":"orderflow_worker_v0.1.py", "status":"clean_orderflow_status.json"},
    "strategy": {"file":"strategy_worker_v0.1.py", "status":"clean_strategy_worker_status.json"},
    "review": {"file":"review_worker_v0.1.py", "status":"clean_review_worker_status.json"},
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
    "strategy_reject":BASE_DIR/"clean_strategy_s_reject_summary.json",
    "review":BASE_DIR/"clean_review_summary.json",
    "paper_status":BASE_DIR/"paper_bot_status.json",
    "paper_open":BASE_DIR/"paper_bot_open.json",
    "paper_score":BASE_DIR/"paper_bot_score_cache.json",
    "paper_latest":BASE_DIR/"paper_candidates_latest.jsonl",
    "ws_status":BASE_DIR/"clean_ws_sidecar_status.json",
    "micro_status":BASE_DIR/"clean_bithumb_micro_status.json",
}
PROCS:Dict[str,subprocess.Popen]={}
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
    for name,spec in WORKERS.items():
        script=BASE_DIR/spec["file"]
        if not script.exists(): continue
        st=worker_status(name)
        # status fresh and pid alive이면 건드리지 않는다.
        if st["alive"] and st["age"]<30: continue
        p=PROCS.get(name)
        if p and p.poll() is None: continue
        try:
            env=os.environ.copy(); env["TRADING_BOT_DIR"]=str(BASE_DIR)
            out=(BASE_DIR/f"{name}_worker.stdout.log").open("a",encoding="utf-8")
            err=(BASE_DIR/f"{name}_worker.stderr.log").open("a",encoding="utf-8")
            PROCS[name]=subprocess.Popen([sys.executable,str(script)], cwd=str(BASE_DIR), env=env, stdout=out, stderr=err, start_new_session=True)
            log_runtime(f"worker_start {name} pid={PROCS[name].pid} file={script.name}")
        except Exception as exc: log_error(f"worker_start:{name}",exc)

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
    ensure_workers()
    workers={k:worker_status(k) for k in WORKERS}
    save_json(FILES["brain_status"],{"version":BOT_VERSION,"state":state,"pid":os.getpid(),"updated_ts":now_ts(),"updated_text":now_text(),"uptime_sec":round(now_ts()-START_TS,1),"workers":workers,"resource":resource_snapshot()})

def line_worker(st:Dict[str,Any])->str:
    ok = st.get('alive') and st.get('age',999)<30 and str(st.get('state')) in {'running','initializing'}
    icon='✅' if ok else ('⚠️' if st.get('alive') else '❌')
    return f"- {icon} {st['name']}: {st.get('version','-')} / {st.get('state')} / age {st.get('age')}s / rows {st.get('row_count')} / sec {st.get('last_sec')}"

def health_text()->str:
    update_brain_status()
    workers=[worker_status(k) for k in WORKERS]
    res=resource_snapshot(); paper=load_json(FILES['paper_status'],{}) or {}; ws=load_json(FILES['ws_status'],{}) or {}; micro=load_json(FILES['micro_status'],{}) or {}
    strat=load_json(FILES['strategy_summary'],{}) or {}; reject=load_json(FILES['strategy_reject'],{}) or {}
    lines=["🧭 건강상태 /health", f"✅ 메인봇 {BOT_VERSION} / clean worker hub / uptime {int(now_ts()-START_TS)}s", "", "[1] worker 상태"]
    lines += [line_worker(w) for w in workers]
    lines += ["", "[2] 외부직원"]
    lines.append(f"- WS: {ws.get('state', ws.get('status','-'))} / age {age(FILES['ws_status']):.1f}s / cache {ws.get('cache', ws.get('cache_count','-'))} / fresh {ws.get('fresh','-')}")
    lines.append(f"- micro: {micro.get('state', micro.get('status','-'))} / age {age(FILES['micro_status']):.1f}s / targets {micro.get('targets','-')} / fresh {micro.get('fresh','-')}")
    lines += ["", "[3] paper_bot"]
    lines.append(f"- {paper.get('version','-')} / {paper.get('state', paper.get('running','-'))} / OPEN {paper.get('open_count', paper.get('open_total','-'))} / CLOSED {paper.get('closed_count','-')} / age {age(FILES['paper_status']):.1f}s")
    lines += ["", "[4] 전략 S 후보"]
    lines.append(f"- S 후보 {strat.get('s_count',0)} / paper_latest {strat.get('paper_latest_count',0)} / 전략별 {strat.get('strategies',{})}")
    lines.append(f"- S 탈락 평가 {reject.get('evaluated','-')} / 탈락 {reject.get('reject_count','-')}")
    lines += ["", "[5] 자원"]
    lines.append(f"- 디스크 사용 {res.get('disk_used_pct','-')}% / 남음 {res.get('disk_free_gb','-')}GB")
    lines.append(f"- 메모리 사용 {res.get('mem_used_pct','-')}% / 남음 {res.get('mem_free_mb','-')}MB / load {res.get('load','-')}")
    lines += ["", "[원칙] 메인봇은 스캔/전략판정 직접 실행 안 함. 명령어는 worker cache만 읽음. 실제 paper OPEN은 전략별 S만."]
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
    if strat:
        lines += ["", "[현재 S 후보 캐시]"]
        lines.append(f"- S 후보 {strat.get('s_count',0)} / 최신전달 {strat.get('paper_latest_count',0)}")
        for k,v in (strat.get('strategies') or {}).items(): lines.append(f"- {k}: {v}")
    return "\n".join(lines)

def quality_text()->str:
    reject=load_json(FILES['strategy_reject'],{}) or {}; strat=load_json(FILES['strategy_summary'],{}) or {}
    lines=["🔍 후보품질 /quality · S 후보와 탈락사유", f"- 실제 OPEN: 전략별 S만. A/B/C 실시간 후보표 제거.", f"- S 후보 {strat.get('s_count',0)} / 평가 {reject.get('evaluated','-')} / 탈락 {reject.get('reject_count','-')}"]
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
    lines=["👀 전략 감시 /strategy_watch", "- 전략별 S만 감시. A/B/C는 기본화면에서 제외."]
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

COMMANDS={'health':health_text,'core':health_text,'score':score_text,'quality':quality_text,'strategy_watch':strategy_watch_text,'errorlog':errorlog_text,'popen_short':open_text}

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
    out=[]; st=time.time()
    for raw in cmds[:8]:
        name=str(raw).strip().lstrip('/')
        fn=COMMANDS.get(name)
        if not fn: continue
        t=time.time()
        try: body=fn()
        except Exception as exc: body=f"❌ /{name} 오류 {exc.__class__.__name__}"
        first='\n'.join(body.splitlines()[:12])
        out.append(f"[{len(out)+1}] /{name} ({time.time()-t:.2f}s)\n{first}")
    out.append(f"\n합계 {time.time()-st:.2f}s / clean main cache-only")
    send(update,'\n\n'.join(out))

def text_router(update, context):
    try:
        txt=update.message.text or ''
        lines=[x.strip().lstrip('/') for x in txt.splitlines() if x.strip().startswith('/')]
        if len(lines)>=2:
            context.args=lines[:8]; return batch_handler(update, context)
    except Exception as exc: log_error('text_router',exc)

def set_commands(updater):
    try:
        updater.bot.set_my_commands([BotCommand('health','worker/main/paper 상태'),BotCommand('score','전략별 S급 성과'),BotCommand('quality','S 후보와 탈락사유'),BotCommand('strategy_watch','전략별 S 감시'),BotCommand('errorlog','오류로그'),BotCommand('batch','묶음 확인')])
    except Exception as exc: log_error('set_commands',exc)

def main():
    log_runtime(f"{BOT_VERSION} starting")
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
    update_brain_status('running')
    log_runtime(f"{BOT_VERSION} telegram polling_started")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__=='__main__': main()
