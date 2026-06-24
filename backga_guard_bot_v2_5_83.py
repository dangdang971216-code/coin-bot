#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backga_guard_bot_v2.py
- 메인 코인봇(tradingbot)이 죽어도 따로 살아있는 관리 전용 텔레그램 봇
- 표준 라이브러리만 사용
- GUARD_CHAT_ID에서 온 명령만 처리

핵심 개선:
- DEPLOY_TARGET 없이 GitHub 최신 수익형_v*.py 자동 선택
- fast validate: py_compile + BOT_VERSION + command string 중심 검사
- 필수 명령어 문자열 검사
- bot.py 백업/교체/hash 검증/재시작/실패 시 자동 롤백
- 가드봇 메뉴 등록
"""

import ast
import builtins
import gzip
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ENV_PATH = Path(__file__).with_name("guard.env")
VERSION = "guard_v2.5.83_simple_post_restart_continue_v1000_2026-06-24"
UPGRADE_STATUS_FILE = ".guard_upgrade_status.json"
GUARD_OFFSET_FILE = ".guard_update_offset.json"
GUARD_START_NOTICE_FILE = ".guard_start_notice.json"
GUARD_POST_UPGRADE_FILE = ".guard_post_upgrade_request.json"
GUARD_RELEASE_RESULT_FILE = "guard_release_last_result_v1000.json"
GUARD_RELEASE_LOCK_FILE = ".guard_release_once_v1000.lock"
GUARD_RELEASE_HISTORY_FILE = ".guard_release_history_v1000.json"
GUARD_DEEP_AUDIT_STATE_FILE = ".guard_deep_audit_state.json"
GUARD_DEEP_AUDIT_JSON = "clean_guard_deep_audit.json"
GUARD_DEEP_AUDIT_TEXT = "clean_guard_deep_audit.txt"


def load_env(path: Path = ENV_PATH) -> dict:
    data = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    allowed = {
        "MAIN_SERVICE", "BOT_DIR", "PYTHON_BIN", "GUARD_POLL_SEC",
        "GIT_BRANCH", "GUARD_AUTO_ROLLBACK", "GUARD_RESTART_WAIT_SEC",
        "GUARD_BOT_TOKEN", "GUARD_CHAT_ID",
        "PAPER_SERVICE", "PAPER_ACTIVE_FILE", "PAPER_PID_FILE", "PAPER_BOT_TOKEN", "PAPER_PYTHON_BIN", "TELEGRAM_TOKEN",
        "GUARD_SERVICE", "GUARD_ACTIVE_FILE",
        "WS_ACTIVE_FILE", "WS_PID_FILE", "WS_LOG_FILE", "WS_STATUS_FILE", "WS_CACHE_FILE",
        "MICRO_ACTIVE_FILE", "MICRO_PID_FILE", "MICRO_LOG_FILE", "MICRO_STATUS_FILE", "MICRO_CACHE_FILE", "MICRO_PYTHON_BIN", "WS_SERVICE", "MICRO_SERVICE", "WORKER_PYTHON_BIN"
    }
    data.update({k: v for k, v in os.environ.items() if k.startswith("GUARD_") or k in allowed})
    return data


ENV = load_env()
TOKEN = ENV.get("GUARD_BOT_TOKEN", "")
ALLOWED_CHAT_ID = str(ENV.get("GUARD_CHAT_ID", "")).strip()
MAIN_SERVICE = ENV.get("MAIN_SERVICE", "tradingbot")
BOT_DIR = Path(ENV.get("BOT_DIR", "/home/dangdang971216/trading_bot"))
PYTHON_BIN = ENV.get("PYTHON_BIN", "python3")
POLL_SEC = int(ENV.get("GUARD_POLL_SEC", "2") or "2")
GIT_BRANCH = ENV.get("GIT_BRANCH", "main")
AUTO_ROLLBACK = str(ENV.get("GUARD_AUTO_ROLLBACK", "1")).strip().lower() not in {"0", "false", "no", "off"}
RESTART_WAIT_SEC = int(ENV.get("GUARD_RESTART_WAIT_SEC", "18") or "18")
STATIC_ALIAS_CHECK_ENABLED = str(ENV.get("GUARD_STATIC_ALIAS_CHECK", "0")).strip().lower() in {"1", "true", "yes", "on"}
PAPER_SERVICE = ENV.get("PAPER_SERVICE", "tradingbot-paper")
PAPER_ACTIVE_FILE = ENV.get("PAPER_ACTIVE_FILE", "paper_bot.py")
PAPER_PID_FILE = ENV.get("PAPER_PID_FILE", "paper_bot.pid")
PAPER_PROCESS_LOG = ENV.get("PAPER_PROCESS_LOG", "paper_bot_process.log")
PAPER_PROCESS_ERR = ENV.get("PAPER_PROCESS_ERR", "paper_bot_process.err")
# paper runtime은 systemd 단일선만 허용한다.
# 최종 목표는 tradingbot-paper.service 관리이며, 이 값은 서비스 미설치 때만 사용된다.
PAPER_PYTHON_BIN = ENV.get("PAPER_PYTHON_BIN", ENV.get("PYTHON_BIN", "/usr/bin/python3")).strip() or "/usr/bin/python3"
GUARD_SERVICE = ENV.get("GUARD_SERVICE", "tradingbot-guard")
GUARD_ACTIVE_FILE = ENV.get("GUARD_ACTIVE_FILE", "backga_guard_bot.py")
DEPLOY_TARGETS_FILE = "DEPLOY_TARGETS.json"
DEPLOYED_PAPER_FILE = ".deployed_paper_target"
DEPLOYED_GUARD_FILE = ".deployed_guard_target"
DEPLOYED_WS_FILE = ".deployed_ws_sidecar_target"
DEPLOYED_MICRO_FILE = ".deployed_micro_sidecar_target"
WS_ACTIVE_FILE = ENV.get("WS_ACTIVE_FILE", "ws_sidecar.py")
WS_PID_FILE = ENV.get("WS_PID_FILE", "clean_ws_sidecar.pid")
WS_LOG_FILE = ENV.get("WS_LOG_FILE", "clean_ws_sidecar.out")
WS_STATUS_FILE = ENV.get("WS_STATUS_FILE", "clean_ws_sidecar_status.json")
WS_CACHE_FILE = ENV.get("WS_CACHE_FILE", "clean_ws_live_cache.json")
# v2.5.18: ws_sidecar는 실제 실행 중인 python 경로를 우선 감지한다.
# guard.env의 WS_PYTHON_BIN은 후보값일 뿐이며, import 실패 시 /proc pid exe, python3.10 등으로 자동 재검사한다.
WS_PYTHON_BIN = ENV.get("WS_PYTHON_BIN", "/usr/bin/python3").strip() or "/usr/bin/python3"
MICRO_ACTIVE_FILE = ENV.get("MICRO_ACTIVE_FILE", "bithumb_micro_sidecar.py")
MICRO_PID_FILE = ENV.get("MICRO_PID_FILE", "clean_bithumb_micro.pid")
MICRO_LOG_FILE = ENV.get("MICRO_LOG_FILE", "clean_bithumb_micro.log")
MICRO_STATUS_FILE = ENV.get("MICRO_STATUS_FILE", "clean_bithumb_micro_status.json")
MICRO_CACHE_FILE = ENV.get("MICRO_CACHE_FILE", "clean_bithumb_micro_cache.json")
MICRO_DESIRED_FILE = ENV.get("MICRO_DESIRED_FILE", ".micro_sidecar_enabled")
MICRO_PYTHON_BIN = ENV.get("MICRO_PYTHON_BIN", ENV.get("PYTHON_BIN", "/usr/bin/python3.10")).strip() or "/usr/bin/python3.10"
WS_SERVICE = ENV.get("WS_SERVICE", "tradingbot-ws-sidecar")
MICRO_SERVICE = ENV.get("MICRO_SERVICE", "tradingbot-micro-sidecar")

# v2.5.39: v319 clean worker hub 정식 관리 대상.
# 메인봇이 무거운 스캔을 직접 하지 않도록 scanner/candle/market/feature/orderflow/risk/strategy/review를
# 가드봇이 독립 systemd 서비스로 설치·업그레이드·재시작·상태확인한다.
WORKER_PYTHON_BIN = ENV.get("WORKER_PYTHON_BIN", ENV.get("PYTHON_BIN", "/usr/bin/python3")).strip() or "/usr/bin/python3"
if not WORKER_PYTHON_BIN.startswith("/"):
    WORKER_PYTHON_BIN = shutil.which(WORKER_PYTHON_BIN) or "/usr/bin/python3"
WORKER_SPECS = {
    "scanner": {"label": "스캐너", "prefix": "scanner_worker", "active": "scanner_worker.py", "service": "tradingbot-scanner-worker", "status": "clean_scanner_status.json", "log": "clean_scanner_worker_service.log"},
    "candle": {"label": "캔들", "prefix": "candle_worker", "active": "candle_worker.py", "service": "tradingbot-candle-worker", "status": "clean_candle_status.json", "log": "clean_candle_worker_service.log", "fresh_limit": 120, "ready_timeout": 45},
    "market": {"label": "장세", "prefix": "market_regime_worker", "active": "market_regime_worker.py", "service": "tradingbot-market-regime-worker", "status": "clean_market_regime_status.json", "log": "clean_market_regime_worker_service.log"},
    "feature": {"label": "특징", "prefix": "feature_worker", "active": "feature_worker.py", "service": "tradingbot-feature-worker", "status": "clean_feature_status.json", "log": "clean_feature_worker_service.log"},
    "orderflow": {"label": "호가요약", "prefix": "orderflow_worker", "active": "orderflow_worker.py", "service": "tradingbot-orderflow-worker", "status": "clean_orderflow_status.json", "log": "clean_orderflow_worker_service.log"},
    "risk": {"label": "위험", "prefix": "risk_worker", "active": "risk_worker.py", "service": "tradingbot-risk-worker", "status": "clean_risk_status.json", "log": "clean_risk_worker_service.log", "fresh_limit": 180, "ready_timeout": 30},
    "strategy": {"label": "전략판정", "prefix": "strategy_worker", "active": "strategy_worker.py", "service": "tradingbot-strategy-worker", "status": "clean_strategy_worker_status.json", "log": "clean_strategy_worker_service.log", "ready_timeout": 30},
    "target_router": {"label": "정보배차", "prefix": "target_router_worker", "active": "target_router_worker.py", "service": "tradingbot-target-router-worker", "status": "clean_target_router_status.json", "log": "clean_target_router_worker_service.log", "ready_timeout": 20},
    "review": {"label": "복기", "prefix": "review_worker", "active": "review_worker.py", "service": "tradingbot-review-worker", "status": "clean_review_worker_status.json", "log": "clean_review_worker_service.log", "fresh_limit": 240, "ready_timeout": 90},
}

if not TOKEN:
    print("GUARD_BOT_TOKEN missing. Put it in guard.env", file=sys.stderr)
    sys.exit(2)
if not ALLOWED_CHAT_ID:
    print("GUARD_CHAT_ID missing. Put it in guard.env", file=sys.stderr)
    sys.exit(2)


@dataclass(order=True, frozen=True)
class BotVersion:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> Optional["BotVersion"]:
        m = re.search(r"v(\d+)[._](\d+)[._](\d+)", text or "")
        if not m:
            return None
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    def __str__(self) -> str:
        return f"v{self.major}.{self.minor}.{self.patch}"


@dataclass(order=True, frozen=True)
class PaperVersion:
    major: int
    minor: int

    @classmethod
    def parse(cls, text: str) -> Optional["PaperVersion"]:
        m = re.search(r"paper_bot_v(\d+)\.(\d+)", text or "")
        if not m:
            return None
        return cls(int(m.group(1)), int(m.group(2)))

    def __str__(self) -> str:
        return f"paper_bot_v{self.major}.{self.minor}"


def paper_version_from_filename(path: Path) -> Optional[PaperVersion]:
    return PaperVersion.parse(path.name)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def perf_now() -> float:
    return time.perf_counter()


def fmt_sec(sec: float) -> str:
    try:
        return f"{sec:.1f}s"
    except Exception:
        return "?s"


def systemctl_value(prop: str, service: str = None) -> str:
    service = service or MAIN_SERVICE
    rc, out = run(["systemctl", "show", "-p", prop, "--value", service], timeout=8)
    return (out or "").strip() if rc == 0 else ""


def service_start_since() -> Optional[str]:
    # journalctl --since accepts systemd timestamp strings. This reduces old-log version false positives.
    val = systemctl_value("ExecMainStartTimestamp") or systemctl_value("ActiveEnterTimestamp")
    return val or None


def autodeploy_status() -> dict:
    names = ["tradingbot-autodeploy.timer", "tradingbot-autodeploy.service"]
    rows = []
    problem = False
    for name in names:
        rc_a, active = run(["systemctl", "is-active", name], timeout=5)
        rc_e, enabled = run(["systemctl", "is-enabled", name], timeout=5)
        active = (active or "unknown").strip()
        enabled = (enabled or "unknown").strip()
        rows.append(f"{name}: active={active}, enabled={enabled}")
        if active in {"active", "activating"} or enabled in {"enabled", "static"}:
            problem = True
    rc, timers = run(["systemctl", "list-timers", "--all"], timeout=8)
    if "tradingbot-autodeploy" in (timers or ""):
        problem = True
        rows.append("list-timers: tradingbot-autodeploy 표시됨")
    return {"problem": problem, "text": "\n".join(rows)}


def run(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 20):
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        return 124, f"TIMEOUT after {timeout}s\n{out}".strip()
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def api(method: str, params: dict, timeout: int = 20):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def send(chat_id, text: str) -> bool:
    text = str(text or "(empty)")
    chunks = []
    while len(text) > 3600:
        chunks.append(text[:3600]); text = text[3600:]
    chunks.append(text)
    all_ok = True
    for chunk in chunks:
        delivered = False
        for attempt in range(3):
            try:
                resp = api("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}, timeout=12)
                delivered = bool(isinstance(resp, dict) and resp.get("ok", True))
                if delivered: break
            except Exception as e:
                print(f"send failed attempt={attempt+1}: {e}", file=sys.stderr, flush=True)
            time.sleep(1.0 + attempt)
        all_ok = all_ok and delivered
    return all_ok


def tail(text: str, n: int = 3500) -> str:
    text = text or ""
    return text if len(text) <= n else "...\n" + text[-n:]


def read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def read_file_tail(path: Path, max_bytes: int = 120_000) -> str:
    """큰 로그파일을 통째로 읽지 않고 끝부분만 읽는다.
    v2.5.5: /gpaperlog 무반응 방지용.
    """
    try:
        if not path.exists():
            return ""
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(max(0, size - max_bytes))
            raw = f.read(max_bytes)
        return raw.decode("utf-8", errors="ignore").strip()
    except Exception as e:
        return f"read_tail_failed {path.name}: {type(e).__name__}: {e}"


def write_json(path: Path, data: dict):
    """Process-safe atomic JSON writer.

    Guard self-restarts can briefly overlap old/new processes. A fixed .tmp name lets
    those processes steal each other's temporary file, so every write uses a unique
    pid+time_ns temporary and fsync before replace.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except Exception:
            pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}




def _fmt_bytes(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.1f}{units[i]}" if i >= 2 else f"{n:.0f}{units[i]}"


def _read_meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8", errors="ignore").splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            parts = v.strip().split()
            if parts:
                out[k] = float(parts[0]) * 1024.0
    except Exception:
        pass
    return out


def _proc_rss_bytes(pid) -> float:
    try:
        pid = int(pid)
        if pid <= 0:
            return 0.0
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                return float(line.split()[1]) * 1024.0
    except Exception:
        pass
    return 0.0


def server_resource_snapshot() -> dict:
    try:
        du = shutil.disk_usage(str(BOT_DIR))
        disk_total, disk_used, disk_free = float(du.total), float(du.used), float(du.free)
        disk_pct = (disk_used / disk_total * 100.0) if disk_total > 0 else 0.0
    except Exception:
        disk_total = disk_used = disk_free = disk_pct = 0.0
    mem = _read_meminfo()
    mem_total = mem.get("MemTotal", 0.0)
    mem_avail = mem.get("MemAvailable", 0.0)
    mem_used = max(0.0, mem_total - mem_avail) if mem_total else 0.0
    mem_pct = (mem_used / mem_total * 100.0) if mem_total else 0.0
    try:
        load1, load5, load15 = os.getloadavg()
    except Exception:
        load1 = load5 = load15 = 0.0
    pids = {"main": _service_main_pid(MAIN_SERVICE), "paper": _service_main_pid(PAPER_SERVICE), "guard": _service_main_pid(GUARD_SERVICE)}
    rss = {k: _proc_rss_bytes(v) for k, v in pids.items()}
    return {"disk_total": disk_total, "disk_used": disk_used, "disk_free": disk_free, "disk_pct": disk_pct, "mem_total": mem_total, "mem_used": mem_used, "mem_avail": mem_avail, "mem_pct": mem_pct, "load1": load1, "load5": load5, "load15": load15, "pids": pids, "rss": rss}


def resource_status_lines(prefix: str = "") -> list[str]:
    r = server_resource_snapshot()
    disk_icon = "✅" if r["disk_pct"] < 85 else ("⚠️" if r["disk_pct"] < 95 else "❌")
    mem_icon = "✅" if r["mem_pct"] < 80 else ("⚠️" if r["mem_pct"] < 92 else "❌")
    load_icon = "✅" if float(r.get("load1", 0.0)) < 1.5 else ("⚠️" if float(r.get("load1", 0.0)) < 3.0 else "❌")
    rss = r.get("rss", {}) or {}
    return [
        f"{disk_icon} 디스크: 사용 {r['disk_pct']:.1f}% / 남음 {_fmt_bytes(r['disk_free'])} / 전체 {_fmt_bytes(r['disk_total'])}",
        f"{mem_icon} 메모리: 사용 {r['mem_pct']:.1f}% / 남음 {_fmt_bytes(r['mem_avail'])} / 전체 {_fmt_bytes(r['mem_total'])}",
        f"{load_icon} CPU/load: {r['load1']:.2f} / {r['load5']:.2f} / {r['load15']:.2f}",
        f"- RSS: main {_fmt_bytes(rss.get('main',0))} / paper {_fmt_bytes(rss.get('paper',0))} / guard {_fmt_bytes(rss.get('guard',0))}",
    ]


def _safe_unlink(path: Path) -> tuple[bool, int, str]:
    try:
        size = path.stat().st_size if path.exists() else 0
        path.unlink(missing_ok=True)
        return True, size, path.name
    except Exception as e:
        return False, 0, f"{path.name}: {type(e).__name__}"


def _safe_truncate(path: Path, min_size: int = 0) -> tuple[bool, int, str]:
    try:
        if not path.exists() or not path.is_file():
            return True, 0, path.name
        size = path.stat().st_size
        if size < min_size:
            return True, 0, path.name
        with path.open("w", encoding="utf-8", errors="ignore"):
            pass
        return True, size, path.name
    except Exception as e:
        return False, 0, f"{path.name}: {type(e).__name__}"




RETIRED_LIVE_CACHE_FILES_V942 = (
    'clean_candidate_reason_compact_cache.json',
    'clean_recheck_candidates_state.json',
    'clean_recheck_candidates_cache.json',
    'clean_s1_lifecycle_state.json',
    'clean_s1_lifecycle_trace.json',
)

def cleanup_retired_live_cache_files_v942(dry_run: bool = False) -> dict:
    """Delete only the five noncore files whose active strategy/candle paths were removed in v942."""
    result = {'checked':0,'deleted':0,'freed':0,'errors':0,'samples':[]}
    for name in RETIRED_LIVE_CACHE_FILES_V942:
        path = BOT_DIR / name
        if not path.exists() or not path.is_file():
            continue
        result['checked'] += 1
        try:
            size = path.stat().st_size
            if dry_run:
                result['deleted'] += 1; result['freed'] += size
                result['samples'].append(f'retired live 삭제예정 {name} {_fmt_bytes(size)}')
            else:
                ok, got, msg = _safe_unlink(path)
                if ok:
                    result['deleted'] += 1; result['freed'] += got
                    result['samples'].append(f'retired live 삭제 {msg} {_fmt_bytes(got)}')
                else:
                    result['errors'] += 1; result['samples'].append(msg)
        except Exception as exc:
            result['errors'] += 1; result['samples'].append(f'{name}: {type(exc).__name__}')
    return result


LEGACY_NONCORE_CACHE_SOURCES = (
    'clean_candidate_reason_compact_cache.json',
    'clean_recheck_candidates_state.json',
    'clean_recheck_candidates_cache.json',
    'clean_s1_lifecycle_state.json',
    'clean_s1_lifecycle_trace.json',
    'paper_bot_hourly_event_trace.jsonl',
)


def cleanup_redundant_legacy_cache_archives(dry_run: bool = False) -> dict:
    """Remove repeated snapshots created by retired one-time cache migrations.

    These files are cache/state/history snapshots, not OPEN/CLOSED/trade/score ledgers.
    Across both legacy roots, keep at most one newest compressed copy per source.
    Stale uncompressed/tmp leftovers are removed only when the current live source exists.
    """
    nowv = time.time()
    result = {'checked':0, 'deleted':0, 'freed':0, 'errors':0, 'samples':[], 'kept':{}}
    roots = [BOT_DIR / 'legacy_v839_archive', BOT_DIR / 'legacy_v840_archive']
    grouped = {name: [] for name in LEGACY_NONCORE_CACHE_SOURCES}
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob('*'):
            if not path.is_file():
                continue
            name = path.name
            source = next((x for x in LEGACY_NONCORE_CACHE_SOURCES if name.startswith(x + '.')), None)
            if source is None:
                continue
            result['checked'] += 1
            grouped[source].append(path)
    for source, files in grouped.items():
        if not files:
            continue
        files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
        compressed = [p for p in files if p.name.endswith(('.gz','.gzip')) and not p.name.endswith('.tmp')]
        keep = compressed[:1] if compressed else files[:1]
        result['kept'][source] = [str(p.relative_to(BOT_DIR)) for p in keep]
        live_exists = (BOT_DIR / source).exists()
        for path in files:
            if path in keep:
                continue
            try:
                age = max(0.0, nowv - path.stat().st_mtime)
                is_tmp = path.name.endswith(('.tmp','.part'))
                is_uncompressed = '.legacy' in path.name and not path.name.endswith(('.gz','.gzip'))
                if (is_tmp or is_uncompressed) and (age < 600.0 or not live_exists):
                    continue
                size = path.stat().st_size
                if dry_run:
                    result['samples'].append(f'중복 legacy 삭제예정 {path.name} {_fmt_bytes(size)}')
                    result['deleted'] += 1; result['freed'] += size
                    continue
                ok, got, label = _safe_unlink(path)
                if ok:
                    result['deleted'] += 1; result['freed'] += got
                    result['samples'].append(f'중복 legacy 삭제 {label} {_fmt_bytes(got)}')
                else:
                    result['errors'] += 1; result['samples'].append(label)
            except Exception as exc:
                result['errors'] += 1
                result['samples'].append(f'{path.name}: {type(exc).__name__}')
    return result


def run_retention_cleanup(dry_run: bool = False) -> dict:
    """v2.5.34 순환정리 본선.
    - 내부 로그/이벤트성 파일은 오늘이 지나면 gzip 압축한다.
    - gzip 파일은 3일 초과 시 삭제한다.
    - 후보품질/복기/품질요약 계열은 당분간 7일 보관 기준으로 정리한다.
    - paper OPEN/CLOSED/trade_log 장부는 삭제하지 않는다.
    """
    now_s = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    deleted = truncated = errors = compressed = 0
    freed = 0
    samples = []

    retired_live_cleanup = cleanup_retired_live_cache_files_v942(dry_run=dry_run)
    deleted += int(retired_live_cleanup.get('deleted') or 0)
    errors += int(retired_live_cleanup.get('errors') or 0)
    freed += int(retired_live_cleanup.get('freed') or 0)
    samples.extend(list(retired_live_cleanup.get('samples') or [])[:12])

    legacy_cleanup = cleanup_redundant_legacy_cache_archives(dry_run=dry_run)
    deleted += int(legacy_cleanup.get('deleted') or 0)
    errors += int(legacy_cleanup.get('errors') or 0)
    freed += int(legacy_cleanup.get('freed') or 0)
    samples.extend(list(legacy_cleanup.get('samples') or [])[:12])

    def age_days(path: Path) -> float:
        try:
            return max(0.0, (now_s - path.stat().st_mtime) / 86400.0)
        except Exception:
            return 0.0

    def mday(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d")
        except Exception:
            return today

    protected_names = {
        "paper_bot_open.json", "paper_bot_closed.jsonl", "paper_bot_status.json", "paper_bot_control.json",
        "paper_candidates.jsonl", "paper_candidates_latest.jsonl", "shadow_candidates.jsonl", "shadow_candidates_latest.jsonl",
    }

    def protected(path: Path) -> bool:
        name = path.name
        if name in protected_names:
            return True
        low = name.lower()
        # trade_log/open/closed 원장은 직접 삭제 금지. 별도 장기 archive 정책에서만 다룬다.
        if "trade_log" in low or "paper_bot_open" in low or "paper_bot_closed" in low:
            return True
        if any(token in low for token in (
            "paper_score", "score_ledger", "change_verify", "issue_ledger",
            "good_s2_observation_ledger", "paper_execution_cost_ledger",
            "closed_result", "decision_state_v926",
        )):
            return True
        return False

    def note_sample(text: str) -> None:
        if len(samples) < 12:
            samples.append(text)

    def gzip_file(path: Path) -> tuple[bool, int, str]:
        try:
            if not path.exists() or not path.is_file() or protected(path) or path.suffix == ".gz":
                return True, 0, path.name
            old_size = path.stat().st_size
            if old_size <= 0:
                return True, 0, path.name
            gz = path.with_name(path.name + f".{mday(path).replace('-', '')}.gz")
            if dry_run:
                return True, 0, f"압축예정 {path.name}"
            with path.open("rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
                shutil.copyfileobj(src, dst)
            path.unlink(missing_ok=True)
            new_size = gz.stat().st_size if gz.exists() else 0
            return True, max(0, old_size - new_size), gz.name
        except Exception as e:
            return False, 0, f"{path.name}: {type(e).__name__}"

    # 1) 즉시 삭제 가능한 임시파일
    for pat in ["*.tmp", "*.swp", "*~"]:
        for path in BOT_DIR.glob(pat):
            if not path.is_file() or protected(path):
                continue
            size = path.stat().st_size if path.exists() else 0
            ok, size, name = (True, size, path.name) if dry_run else _safe_unlink(path)
            if ok:
                deleted += 1; freed += size; note_sample(f"삭제 {name}")
            else:
                errors += 1; note_sample(name)

    # 2) 오래된 backup은 2일 초과 시 삭제
    for pat in ["bot.py.backup*", "*.backup_guard_apply_*", "ws_sidecar.py.backup*", "bithumb_micro_sidecar.py.backup*", "paper_bot.py.backup*", "backga_guard_bot*.backup*"]:
        for path in BOT_DIR.glob(pat):
            if not path.is_file() or protected(path) or age_days(path) <= 2:
                continue
            size = path.stat().st_size if path.exists() else 0
            ok, size, name = (True, size, path.name) if dry_run else _safe_unlink(path)
            if ok:
                deleted += 1; freed += size; note_sample(f"삭제 {name}")
            else:
                errors += 1; note_sample(name)

    # 3) 내부 로그/이벤트성 파일은 날짜가 지나면 비우지 말고 압축한다.
    internal_names = [
        "clean_ws_sidecar.out", "clean_ws_sidecar.log", "ws_sidecar.log",
        "clean_bithumb_micro.log", "clean_brain_runtime.log", "clean_brain_error.log",
        "clean_runtime_events.jsonl", "runtime_events.jsonl",
    ]
    for name in internal_names:
        path = BOT_DIR / name
        if not path.exists() or not path.is_file() or protected(path):
            continue
        if mday(path) == today:
            continue
        ok, saved, gzname = gzip_file(path)
        if ok:
            if saved > 0:
                compressed += 1; freed += saved; note_sample(f"압축 {gzname}")
        else:
            errors += 1; note_sample(gzname)

    # 4) candidate_events는 너무 커지면 최근 2만 줄만 유지. 7일 지난 이벤트성 파일은 압축/삭제 대상.
    ce = BOT_DIR / "candidate_events.jsonl"
    try:
        if ce.exists() and ce.stat().st_size > 100 * 1024 * 1024 and not protected(ce):
            old_size = ce.stat().st_size
            if not dry_run:
                lines = read_file_tail(ce, max_bytes=8_000_000).splitlines()[-20000:]
                tmp = ce.with_suffix(ce.suffix + ".keep")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(tmp, ce)
            new_size = ce.stat().st_size if ce.exists() else 0
            truncated += 1; freed += max(0, old_size - new_size); note_sample("축소 candidate_events.jsonl")
    except Exception as e:
        errors += 1; note_sample(f"candidate_events: {type(e).__name__}")

    # 5) 후보품질/복기/요약 계열은 7일 보관. active snapshot은 덮어쓰기 파일이면 유지하되 dated/old 파일은 정리.
    quality_patterns = [
        "candidate_events*.jsonl", "clean_candidate_review*.jsonl", "candidate_review*.jsonl",
        "clean_quality*.json", "quality_*.json", "clean_external_snapshot*.json", "clean_version_score_summary*.json",
        "clean_reject_summary*.json", "review_*.json", "missed_*.jsonl",
    ]
    keep_active = {"clean_quality_summary.json", "clean_external_snapshot.json", "clean_version_score_summary.json", "clean_health_snapshot.json", "clean_resource_status.json", "clean_reject_summary.json"}
    for pat in quality_patterns:
        for path in BOT_DIR.glob(pat):
            if not path.is_file() or protected(path) or path.name in keep_active:
                continue
            if age_days(path) <= 7:
                continue
            size = path.stat().st_size if path.exists() else 0
            ok, size, name = (True, size, path.name) if dry_run else _safe_unlink(path)
            if ok:
                deleted += 1; freed += size; note_sample(f"7일보관 삭제 {name}")
            else:
                errors += 1; note_sample(name)

    # 6) 3일 지난 gzip/old/log rotate 파일 삭제
    for pat in ["*.gz", "*.old", "*.log.*", "*.out.*"]:
        for path in BOT_DIR.glob(pat):
            if not path.is_file() or protected(path) or age_days(path) <= 3:
                continue
            size = path.stat().st_size if path.exists() else 0
            ok, size, name = (True, size, path.name) if dry_run else _safe_unlink(path)
            if ok:
                deleted += 1; freed += size; note_sample(f"압축3일 삭제 {name}")
            else:
                errors += 1; note_sample(name)

    # 6-1) v928: archive/legacy archive는 재귀 순환한다.
    # 기존 cleanup은 BOT_DIR 최상단만 봐 5월 debug archive가 계속 남았다.
    archive_roots = [BOT_DIR / 'archive', BOT_DIR / 'legacy_v839_archive', BOT_DIR / 'legacy_v840_archive']
    for root in archive_roots:
        if not root.exists() or not root.is_dir():
            continue
        try:
            for path in root.rglob('*'):
                if not path.is_file() or protected(path):
                    continue
                low = path.name.lower()
                if not (low.endswith(('.gz','.old')) or '.log.' in low or '.out.' in low or 'guard_removed' in low or 'legacy' in low):
                    continue
                if age_days(path) <= 7:
                    continue
                size = path.stat().st_size if path.exists() else 0
                ok, got, name = (True, size, path.name) if dry_run else _safe_unlink(path)
                if ok:
                    deleted += 1; freed += got; note_sample(f"archive7일 삭제 {name}")
                else:
                    errors += 1; note_sample(name)
        except Exception as e:
            errors += 1; note_sample(f"archive_recursive: {type(e).__name__}")

    # 7) v427: 가드봇 전담 활성파일 다이어트. OPEN 장부는 절대 대상에 넣지 않는다.
    def trim_jsonl_tail(path: Path, keep_lines: int, label: str, min_lines: int = 0) -> None:
        nonlocal truncated, errors, freed
        try:
            if not path.exists() or not path.is_file():
                return
            low = path.name.lower()
            if "open" in low and "closed" not in low:
                note_sample(f"OPEN보호 {path.name}")
                return
            data = path.read_bytes()
            lines = [ln for ln in data.splitlines() if ln.strip()]
            keep_lines = max(20, int(keep_lines or 300))
            min_lines = max(keep_lines + 1, int(min_lines or keep_lines + 1))
            if len(lines) < min_lines or len(lines) <= keep_lines:
                return
            old_size = path.stat().st_size
            if dry_run:
                note_sample(f"활성축소예정 {label} {len(lines)}→{keep_lines}")
                return
            arch_dir = BOT_DIR / "archive"
            arch_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            gz = arch_dir / f"{path.stem}_guard_removed_v427_{stamp}.jsonl.gz"
            with gzip.open(gz, "wb", compresslevel=6) as dst:
                dst.write(b"\n".join(lines[:-keep_lines]) + b"\n")
            tmp = path.with_suffix(path.suffix + ".guardtmp")
            tmp.write_bytes(b"\n".join(lines[-keep_lines:]) + b"\n")
            os.replace(tmp, path)
            new_size = path.stat().st_size if path.exists() else 0
            truncated += 1
            freed += max(0, old_size - new_size)
            note_sample(f"활성축소 {label} {len(lines)}→{keep_lines}")
        except Exception as e:
            errors += 1
            note_sample(f"{label}: {type(e).__name__}")

    def trim_text_tail(path: Path, keep_bytes: int, label: str) -> None:
        nonlocal truncated, errors, freed
        try:
            if not path.exists() or not path.is_file():
                return
            size = path.stat().st_size
            keep_bytes = max(80_000, int(keep_bytes or 300_000))
            if size <= keep_bytes:
                return
            if dry_run:
                note_sample(f"로그축소예정 {label} {_fmt_bytes(size)}→{_fmt_bytes(keep_bytes)}")
                return
            data = path.read_bytes()
            arch_dir = BOT_DIR / "archive"
            arch_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            gz = arch_dir / f"{path.stem}_guard_removed_v427_{stamp}.log.gz"
            with gzip.open(gz, "wb", compresslevel=6) as dst:
                dst.write(data[:-keep_bytes])
            tmp = path.with_suffix(path.suffix + ".guardtmp")
            tmp.write_bytes(data[-keep_bytes:])
            os.replace(tmp, path)
            truncated += 1
            freed += max(0, size - path.stat().st_size)
            note_sample(f"로그축소 {label}")
        except Exception as e:
            errors += 1
            note_sample(f"{label}: {type(e).__name__}")

    # v2.5.68: CLOSED/trade_log are live core ledgers.  Do not read-tail-replace
    # them while paper_bot may append; that race can lose the newest CLOSED row.
    # They remain protected and are never deleted/truncated by guard cleanup.
    for path, keep, min_lines, label in [
        (BOT_DIR / "paper_candidates.jsonl", 500, 650, "paper candidates"),
        (BOT_DIR / "paper_candidates_latest.jsonl", 300, 420, "paper latest"),
        (BOT_DIR / "paper_candidates_handoff_merged.jsonl", 500, 650, "paper merged handoff"),
        (BOT_DIR / "candidate_events.jsonl", 700, 900, "candidate events"),
        (BOT_DIR / "paper_bot_candidate_handoff_trace.jsonl", 300, 420, "paper trace jsonl"),
    ]:
        trim_jsonl_tail(path, keep, label, min_lines)
    trim_text_tail(BOT_DIR / "clean_brain_runtime.log", 300_000, "brain runtime")
    trim_text_tail(BOT_DIR / "clean_brain_error.log", 160_000, "brain error")
    trim_text_tail(BOT_DIR / "paper_bot.log", 300_000, "paper log")
    trim_text_tail(BOT_DIR / "paper_bot_error.log", 160_000, "paper error")

    # 8) 서버 작업폴더 update zip은 최신 3개만 유지한다. GitHub history는 건드리지 않는다.
    try:
        zips = sorted(BOT_DIR.glob("coinbot_update_v*.zip"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        for i, path in enumerate(zips):
            if i < 3:
                continue
            size = path.stat().st_size if path.exists() else 0
            if dry_run:
                note_sample(f"zip삭제예정 {path.name}")
                continue
            ok, size, name = _safe_unlink(path)
            if ok:
                deleted += 1; freed += size; note_sample(f"zip삭제 {name}")
            else:
                errors += 1; note_sample(name)
    except Exception as e:
        errors += 1; note_sample(f"zip_cleanup: {type(e).__name__}")

    # 9) v928: 버전 보관 코드는 컴포넌트별 최신 3개를 남기고 7일 지난 것만 삭제한다.
    version_prefixes = (
        'coinbot_main_', 'strategy_worker_', 'paper_bot_', 'review_worker_', 'candle_worker_',
        'scanner_worker_', 'feature_worker_', 'orderflow_worker_', 'target_router_worker_',
        'risk_worker_', 'market_regime_worker_', 'backga_guard_bot_',
    )
    try:
        for prefix in version_prefixes:
            files = [x for x in BOT_DIR.glob(prefix + 'v*.py') if x.is_file()]
            files.sort(key=lambda x: x.stat().st_mtime if x.exists() else 0, reverse=True)
            for path in files[3:]:
                if age_days(path) <= 7:
                    continue
                size = path.stat().st_size if path.exists() else 0
                ok, got, name = (True, size, path.name) if dry_run else _safe_unlink(path)
                if ok:
                    deleted += 1; freed += got; note_sample(f"구코드7일 삭제 {name}")
                else:
                    errors += 1; note_sample(name)
    except Exception as e:
        errors += 1; note_sample(f"old_code_cleanup: {type(e).__name__}")

    try:
        write_json(BOT_DIR / "clean_retention_state.json", {
            "ok": errors == 0, "time": now(), "ts": time.time(), "version": VERSION,
            "deleted": deleted, "truncated": truncated, "compressed": compressed, "errors": errors,
            "freed": freed, "samples": samples[:12],
            "policy": {"owner": "guard", "open_deleted": False, "closed_live_truncate": False, "trade_log_live_truncate": False, "update_zip_keep": 3},
        })
    except Exception:
        pass

    return {"deleted": deleted, "truncated": truncated, "compressed": compressed, "errors": errors, "freed": freed, "samples": samples, "dry_run": dry_run, "legacy_cleanup": legacy_cleanup, "retired_live_cleanup_v942": retired_live_cleanup}

def cleanup_text(dry_run: bool = False) -> str:
    before = server_resource_snapshot()
    res = run_retention_cleanup(dry_run=dry_run)
    after = server_resource_snapshot()
    lines = ["🧹 디스크 순환정리 /gcleanup", "- 내부 로그/이벤트: 날짜 지나면 gzip 압축, gzip은 3일 후 삭제", "- 후보품질/복기 계열: 최근 7일 보관", "- OPEN/CLOSED/trade_log 핵심 원장은 삭제·활성축소 금지, debug/trace/cache만 순환정리", f"- 삭제 {res['deleted']}개 / 압축 {res.get('compressed',0)}개 / 비움·축소 {res['truncated']}개 / 오류 {res['errors']}개", f"- 확보 추정: {_fmt_bytes(res['freed'])}", f"- 전: 디스크 {before['disk_pct']:.1f}% / 남음 {_fmt_bytes(before['disk_free'])}", f"- 후: 디스크 {after['disk_pct']:.1f}% / 남음 {_fmt_bytes(after['disk_free'])}"]
    if res.get("samples"):
        lines += ["", "처리 예시", *[f"- {x}" for x in res["samples"][:10]]]
    return "\n".join(lines)



V928_STORAGE_FIX_JSON = 'clean_guard_storage_fix_v928.json'
V928_STORAGE_FIX_TEXT = 'clean_guard_storage_fix_v928.txt'


def _v928_install_root_text(target: str, content: str) -> tuple[int, str]:
    tmp = BOT_DIR / ('.v928_' + Path(target).name + '.tmp')
    try:
        tmp.write_text(content, encoding='utf-8')
        return _run_privileged(['install', '-m', '0644', str(tmp), str(target)], timeout=30)
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass


def _v928_system_log_fix(dry_run: bool = False) -> dict:
    before_journal, before_journal_text = _audit_journal_bytes() if '_audit_journal_bytes' in globals() else (0, '')
    before_log = _audit_du_bytes(Path('/var/log'), timeout=15) if '_audit_du_bytes' in globals() else 0
    actions = []
    if dry_run:
        return {
            'dry_run': True, 'before_journal': before_journal, 'before_var_log': before_log,
            'actions': ['journald 256MB persistent cap', 'journal vacuum 256MB', 'google ops logrotate 32MB x2', 'old rotated google ops logs >12h delete', 'system logrotate'],
            'errors': 0,
        }
    errors = 0
    journald_conf = """[Journal]\nSystemMaxUse=256M\nRuntimeMaxUse=64M\nMaxRetentionSec=3day\nCompress=yes\n"""
    rc, out = _v928_install_root_text('/etc/systemd/journald.conf.d/99-coinbot-storage.conf', journald_conf)
    actions.append({'action':'install_journald_cap','rc':rc,'out':tail(out,500)})
    if rc != 0: errors += 1
    else:
        rc2, out2 = _run_privileged(['systemctl','restart','systemd-journald'], timeout=40)
        actions.append({'action':'restart_journald','rc':rc2,'out':tail(out2,500)})
        if rc2 != 0: errors += 1
    rc, out = _run_privileged(['journalctl','--vacuum-size=256M'], timeout=90)
    actions.append({'action':'vacuum_journal','rc':rc,'out':tail(out,700)})
    if rc != 0: errors += 1

    logrotate_conf = """/var/log/google-cloud-ops-agent/subagents/*.log {\n    size 32M\n    rotate 2\n    compress\n    nodelaycompress\n    missingok\n    notifempty\n    copytruncate\n    su root root\n}\n"""
    rc, out = _v928_install_root_text('/etc/logrotate.d/coinbot-google-ops-agent', logrotate_conf)
    actions.append({'action':'install_google_ops_logrotate','rc':rc,'out':tail(out,500)})
    if rc != 0:
        errors += 1
    else:
        rc2, out2 = _run_privileged(['logrotate','-f','/etc/logrotate.d/coinbot-google-ops-agent'], timeout=120)
        actions.append({'action':'rotate_google_ops','rc':rc2,'out':tail(out2,700)})
        if rc2 != 0: errors += 1
    rc, out = _run_privileged(['logrotate','-f','/etc/logrotate.conf'], timeout=120)
    actions.append({'action':'rotate_system_logs','rc':rc,'out':tail(out,700)})
    if rc != 0: errors += 1
    rc, out = _run_privileged(['find','/var/log/google-cloud-ops-agent/subagents','-maxdepth','1','-type','f','-name','*.log.*','-mmin','+720','-delete'], timeout=60)
    actions.append({'action':'delete_old_rotated_google_ops_logs_12h','rc':rc,'out':tail(out,500)})
    if rc != 0: errors += 1
    after_journal, after_journal_text = _audit_journal_bytes() if '_audit_journal_bytes' in globals() else (0, '')
    after_log = _audit_du_bytes(Path('/var/log'), timeout=15) if '_audit_du_bytes' in globals() else 0
    return {
        'dry_run': False, 'before_journal': before_journal, 'after_journal': after_journal,
        'before_var_log': before_log, 'after_var_log': after_log,
        'journal_freed': max(0, before_journal-after_journal),
        'var_log_freed': max(0, before_log-after_log),
        'before_journal_text': before_journal_text, 'after_journal_text': after_journal_text,
        'actions': actions, 'errors': errors,
    }


def gstorage_fix_text(dry_run: bool = False) -> str:
    before = server_resource_snapshot()
    bot = run_retention_cleanup(dry_run=dry_run)
    system = _v928_system_log_fix(dry_run=dry_run)
    after = server_resource_snapshot()
    result = {
        'schema':'guard_storage_fix_v942', 'version':VERSION, 'time':now(), 'ts':time.time(),
        'dry_run':dry_run, 'before':before, 'after':after, 'bot_cleanup':bot, 'system_log_fix':system,
        'protected':'OPEN/CLOSED/trade_log/score/change/issue/good-S2 core ledgers untouched',
    }
    lines = [
        '🧹 불필요파일·로그 정리 /gstorage_fix · v942',
        f"- 모드: {'미리보기' if dry_run else '실행'}",
        f"- 전: 사용 {before.get('disk_pct',0):.1f}% / 남음 {_fmt_bytes(before.get('disk_free',0))}",
        f"- 후: 사용 {after.get('disk_pct',0):.1f}% / 남음 {_fmt_bytes(after.get('disk_free',0))}",
        f"- 봇폴더: 삭제 {bot.get('deleted',0)} / 압축 {bot.get('compressed',0)} / 축소 {bot.get('truncated',0)} / 확보 {_fmt_bytes(bot.get('freed',0))} / 오류 {bot.get('errors',0)}",
        f"- retired live cache 5종: 삭제 {(bot.get('retired_live_cleanup_v942') or {}).get('deleted',0)} / 확보 {_fmt_bytes((bot.get('retired_live_cleanup_v942') or {}).get('freed',0))}",
        f"- 중복 legacy cache archive: 삭제 {(bot.get('legacy_cleanup') or {}).get('deleted',0)} / 확보 {_fmt_bytes((bot.get('legacy_cleanup') or {}).get('freed',0))}",
        f"- journal: {_fmt_bytes(system.get('before_journal',0))} → {_fmt_bytes(system.get('after_journal',system.get('before_journal',0)))} / 확보 {_fmt_bytes(system.get('journal_freed',0))}",
        f"- /var/log: {_fmt_bytes(system.get('before_var_log',0))} → {_fmt_bytes(system.get('after_var_log',system.get('before_var_log',0)))} / 확보 {_fmt_bytes(system.get('var_log_freed',0))}",
        f"- system 작업 오류: {system.get('errors',0)}",
        '', '[보존]', '- OPEN/CLOSED/trade_log/score/change/issue/good-S2 핵심 장부는 정리 대상 아님',
    ]
    if bot.get('samples'):
        lines += ['', '[봇폴더 처리 예시]'] + [f"- {x}" for x in bot.get('samples',[])[:12]]
    acts = system.get('actions') or []
    if acts:
        lines += ['', '[시스템 로그 처리]']
        for a in acts[:8]:
            if isinstance(a, dict): lines.append(f"- {a.get('action')}: rc {a.get('rc')}")
            else: lines.append(f"- {a}")
    text='\n'.join(lines)
    try:
        write_json(BOT_DIR / V928_STORAGE_FIX_JSON, result)
        (BOT_DIR / V928_STORAGE_FIX_TEXT).write_text(text+'\n', encoding='utf-8')
    except Exception:
        pass
    return text


def preupgrade_disk_guard(force: bool = False) -> tuple[bool, str]:
    before = server_resource_snapshot()
    cleanup = run_retention_cleanup(dry_run=False)
    after = server_resource_snapshot()
    ok = bool(after.get("disk_pct", 100.0) < 93.0 and after.get("disk_free", 0.0) > 300 * 1024 * 1024)
    text = "\n".join(["[0/5] 디스크 사전점검·순환정리", f"- 전: 사용 {before['disk_pct']:.1f}% / 남음 {_fmt_bytes(before['disk_free'])}", f"- 정리: 삭제 {cleanup['deleted']}개 / 압축 {cleanup.get('compressed',0)}개 / 비움 {cleanup['truncated']}개 / 확보 {_fmt_bytes(cleanup['freed'])}", f"- 후: 사용 {after['disk_pct']:.1f}% / 남음 {_fmt_bytes(after['disk_free'])}"])
    if ok or force:
        return True, text
    return False, text + "\n❌ 디스크 여유 부족: 업그레이드 중단. /gcleanup 후 큰 파일 확인 필요"



def _offset_path() -> Path:
    return BOT_DIR / GUARD_OFFSET_FILE


def load_guard_offset() -> Optional[int]:
    data = read_json(_offset_path())
    try:
        val = int(data.get("offset", 0) or 0)
        return val if val > 0 else None
    except Exception:
        return None


def save_guard_offset(offset: Optional[int], reason: str = "") -> None:
    if not offset:
        return
    try:
        write_json(_offset_path(), {"offset": int(offset), "updated_at": now(), "reason": reason, "version": VERSION})
    except Exception as e:
        print(f"[{now()}] save_guard_offset failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)


def init_guard_offset_if_empty(offset: Optional[int]) -> Optional[int]:
    """가드봇 재시작 후 같은 /gguard_upgrade를 다시 먹는 루프를 막는다.
    저장된 offset이 없으면 텔레그램 서버에 남은 업데이트를 한 번 비우고 최신 다음부터 받는다.
    """
    if offset is not None:
        return offset
    try:
        data = api("getUpdates", {"timeout": 0}, timeout=5)
        results = data.get("result", []) if isinstance(data, dict) else []
        if results:
            latest = max(int(u.get("update_id", 0) or 0) for u in results)
            offset = latest + 1
            save_guard_offset(offset, "startup_drop_pending")
            return offset
    except Exception as e:
        print(f"[{now()}] init_guard_offset failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    return None


def send_guard_start_notice() -> None:
    """systemd 재시작 루프 때 시작 알림이 연속 도배되지 않게 제한한다."""
    p = BOT_DIR / GUARD_START_NOTICE_FILE
    data = read_json(p)
    try:
        last_ts = float(data.get("ts", 0) or 0)
    except Exception:
        last_ts = 0
    last_version = str(data.get("version", ""))
    # 같은 버전 20초 이내 재시작 알림은 생략. 실제 상태는 /guard에서 확인.
    if last_version == VERSION and time.time() - last_ts < 20:
        return
    try:
        write_json(p, {"ts": time.time(), "time": now(), "version": VERSION})
    except Exception:
        pass
    send(ALLOWED_CHAT_ID, f"🛡 백가 가드봇 시작\n- {VERSION}\n- 관리대상: {MAIN_SERVICE}\n- 업그레이드: /gupgrade_menu 또는 /gupgrade\n- 상태확인: /guard")

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(path: Path) -> str:
    try:
        return sha256_file(path)[:12]
    except Exception:
        return "?"


def extract_bot_version_from_text(text: str) -> str:
    vals = re.findall(r'^\s*BOT_VERSION\s*=\s*[\'\"]([^\'\"]+)', text or "", flags=re.M)
    if vals:
        return vals[-1]
    m = re.search(r"수익형\s*v\d+\.\d+\.\d+", text or "")
    return m.group(0) if m else "?"


def extract_bot_version(path: Path) -> str:
    if not path.exists():
        return "?"
    return extract_bot_version_from_text(path.read_text(encoding="utf-8", errors="ignore"))


def get_bot_versions():
    bot_py = BOT_DIR / "bot.py"
    bot_version = extract_bot_version(bot_py)
    target = read_file(BOT_DIR / "DEPLOY_TARGET.txt")
    deployed = read_file(BOT_DIR / ".deployed_target")
    latest = find_latest_source_file()
    latest_name = latest.name if latest else "?"
    return bot_version, target or "?", deployed or "?", latest_name


def same_version_label(a: str, b: str) -> bool:
    av = BotVersion.parse(a or "")
    bv = BotVersion.parse(b or "")
    return bool(av and bv and av == bv)


def write_deploy_markers(target_name: str) -> None:
    # v2.2: DEPLOY_TARGET은 참고값이지만 오래된 값이 남으면 사람이 헷갈리고
    # 일부 구 배포 경로가 다시 잡을 수 있어 최신 적용 성공 시 같이 맞춘다.
    for name in [".deployed_target", "DEPLOY_TARGET.txt"]:
        try:
            (BOT_DIR / name).write_text(str(target_name).strip() + "\n", encoding="utf-8")
        except Exception:
            pass


def extract_recent_runtime_version(lines: int = 220, since: Optional[str] = None) -> str:
    try:
        if since is None:
            since = service_start_since()
        log = recent_journal(lines=lines, since=since) if since else recent_journal(lines=lines)
        vals = re.findall(r"수익형\s*v\d+\.\d+\.\d+", log or "")
        return vals[-1] if vals else "시작로그 대기"
    except Exception:
        return "시작로그 확인불가"


def version_from_filename(path: Path) -> Optional[BotVersion]:
    return BotVersion.parse(path.name)


def find_latest_source_file() -> Optional[Path]:
    files = []
    # v2.5.54: bundle 내부 메인 파일명은 ASCII 우선, 한글/ASCII 둘 다 정식 메인 후보로 인정한다.
    # 이전 guard는 수익형_v*.py만 찾아서 coinbot_main_v2_13_xxx.py 적용을 막았다.
    for pat in ("수익형_v*.py", "coinbot_main_v*.py"):
        for p in BOT_DIR.glob(pat):
            ver = version_from_filename(p)
            if ver:
                files.append((ver, p))
    if not files:
        return None
    return sorted(files, key=lambda x: x[0])[-1][1]





def find_latest_paper_file() -> Optional[Path]:
    files = []
    for p in BOT_DIR.glob("paper_bot_v*.py"):
        ver = paper_version_from_filename(p)
        if ver:
            files.append((ver, p))
    if not files:
        return None
    return sorted(files, key=lambda x: x[0])[-1][1]


def find_latest_ws_sidecar_file() -> Optional[Path]:
    files = []
    for p in BOT_DIR.glob("ws_sidecar_v*.py"):
        m = re.search(r"ws_sidecar_v(\d+)\.(\d+)", p.name)
        if m:
            files.append(((int(m.group(1)), int(m.group(2)), p.stat().st_mtime), p))
    if not files:
        return None
    return sorted(files, key=lambda x: x[0])[-1][1]


def find_latest_micro_sidecar_file() -> Optional[Path]:
    """bithumb_micro_sidecar_v*.py 중 가장 높은 버전을 찾는다.

    v2.5.28: v2.5.27에서 /gupgrade 자동 이어가기 중 이 함수가 누락되어
    NameError가 났다. micro 파일은 수정하지 않더라도 DEPLOY_TARGETS의 최신 상태
    확인 단계에서 항상 필요하므로 가드봇 본선에 고정한다.
    """
    files = []
    for p in BOT_DIR.glob("bithumb_micro_sidecar_v*.py"):
        m = re.search(r"bithumb_micro_sidecar_v(\d+)\.(\d+)", p.name)
        if m:
            files.append(((int(m.group(1)), int(m.group(2)), p.stat().st_mtime), p))
    if not files:
        return None
    return sorted(files, key=lambda x: x[0])[-1][1]


def find_latest_guard_file() -> Optional[Path]:
    files = []
    for p in BOT_DIR.glob("backga_guard_bot_v*.py"):
        if p.name == GUARD_ACTIVE_FILE:
            continue
        m = re.search(r"v(\d+)[._](\d+)", p.name)
        if m:
            files.append(((int(m.group(1)), int(m.group(2)), p.stat().st_mtime), p))
    if not files:
        return None
    return sorted(files, key=lambda x: x[0])[-1][1]


def extract_assignment(path: Path, key: str) -> str:
    text = read_file(path)
    vals = re.findall(rf"^\s*{re.escape(key)}\s*=\s*[\'\"]([^\'\"]+)", text or "", flags=re.M)
    return vals[-1] if vals else "?"


def extract_paper_version(path: Path) -> str:
    if not path.exists():
        return "?"
    val = extract_assignment(path, "VERSION")
    if val != "?":
        return val
    pv = PaperVersion.parse(path.name)
    return str(pv) if pv else "?"


def extract_guard_version(path: Path) -> str:
    if not path.exists():
        return "?"
    return extract_assignment(path, "VERSION")


def read_deploy_targets() -> dict:
    data = read_json(BOT_DIR / DEPLOY_TARGETS_FILE)
    out = {}
    if isinstance(data, dict):
        main = data.get("main_bot") or data.get("main") or data.get("target")
        paper = data.get("paper_bot") or data.get("paper")
        if main:
            out["main"] = str(main).strip()
        if paper:
            out["paper"] = str(paper).strip()
        ws = data.get("ws_sidecar") or data.get("ws") or data.get("websocket")
        if ws:
            out["ws"] = str(ws).strip()
        micro = data.get("micro_sidecar") or data.get("micro") or data.get("bithumb_micro")
        if micro:
            out["micro"] = str(micro).strip()
    if "main" not in out:
        t = read_file(BOT_DIR / "DEPLOY_TARGET.txt")
        if t:
            out["main"] = t.splitlines()[0].strip()
    return out


def choose_latest_paper_target(explicit: Optional[str] = None) -> Optional[Path]:
    """메인봇처럼 paper_bot도 최신 파일을 자동 선택한다.
    DEPLOY_TARGETS.json의 paper 값이 낡아 있어도 BOT_DIR 안의 paper_bot_v*.py 중
    가장 높은 버전이 있으면 그 파일을 우선한다. explicit이 있으면 explicit만 사용한다.
    """
    if explicit:
        return BOT_DIR / explicit
    latest = find_latest_paper_file()
    targets = read_deploy_targets()
    configured = BOT_DIR / targets["paper"] if targets.get("paper") else None
    if latest and configured and configured.exists():
        latest_v = paper_version_from_filename(latest)
        cfg_v = paper_version_from_filename(configured)
        if latest_v and cfg_v:
            return latest if latest_v >= cfg_v else configured
    return latest or configured


def get_paper_versions() -> dict:
    latest = find_latest_paper_file()
    targets = read_deploy_targets()
    chosen = choose_latest_paper_target()
    configured = targets.get("paper") or "?"
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    return {
        "service": is_service_active(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else "no_service",
        "active_file": PAPER_ACTIVE_FILE,
        "active_version": extract_paper_version(active_path),
        "target": chosen.name if chosen else configured,
        "configured_target": configured,
        "deployed": read_file(BOT_DIR / DEPLOYED_PAPER_FILE) or "?",
        "latest": latest.name if latest else "?",
        "latest_version": extract_paper_version(latest) if latest else "?",
        "active_hash": short_hash(active_path),
        "latest_hash": short_hash(latest) if latest else "?",
    }


def _systemd_unit_names(service: str) -> list[str]:
    raw = str(service or "").strip()
    if not raw:
        return []
    out = [raw]
    if not raw.endswith(".service"):
        out.append(raw + ".service")
    return list(dict.fromkeys(out))


def _systemctl_show_map(service: str, timeout: int = 4) -> dict:
    """systemctl status보다 가벼운 단일 본선 조회.

    v399: /gpaper_state에서 exists=False, /gpaperlog에서 exists=True처럼 갈리는 문제를
    줄이기 위해 show(LoadState/ActiveState/MainPID)만 쓴다.
    """
    last = {}
    for name in _systemd_unit_names(service):
        rc, out = run(["systemctl", "show", name, "--no-pager", "--property=LoadState,ActiveState,SubState,MainPID,FragmentPath"], timeout=timeout)
        d = {"rc": rc, "unit": name}
        for line in str(out or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
        last = d
        if d.get("LoadState") and d.get("LoadState") != "not-found":
            return d
    return last


def service_exists(service: str) -> bool:
    d = _systemctl_show_map(service, timeout=4)
    if d.get("LoadState") and d.get("LoadState") != "not-found":
        return True
    for name in _systemd_unit_names(service):
        if Path(f"/etc/systemd/system/{name}").exists() or Path(f"/lib/systemd/system/{name}").exists():
            return True
    return False


def is_service_active(service: str) -> str:
    d = _systemctl_show_map(service, timeout=4)
    active = str(d.get("ActiveState") or "").strip()
    if active:
        return active
    rc, out = run(["systemctl", "is-active", service], timeout=4)
    return (out or "?").strip()


def _service_main_pid(service: str) -> int:  # type: ignore[override]
    d = _systemctl_show_map(service, timeout=4)
    try:
        return int(str(d.get("MainPID") or "0").strip() or "0")
    except Exception:
        return 0


def validate_paper_target(path: Path) -> tuple[bool, list[str], list[str]]:
    checks, warnings = [], []
    if not path.exists():
        return False, [f"대상 파일 없음: {path.name}"], warnings
    if not paper_version_from_filename(path):
        return False, [f"파일명 형식 오류: {path.name}"], warnings
    ok, out = py_compile(path)
    checks.append("✅ paper py_compile 통과" if ok else "❌ paper py_compile 실패\n" + tail(out, 1600))
    if not ok:
        return False, checks, warnings
    src = read_file(path)
    for required in ["VERSION", "paper_bot_status.json", "/pstatus", "/pbatch"]:
        if required not in src:
            warnings.append(f"paper 필수 문자열 확인 필요: {required}")
    checks.append(f"✅ paper VERSION: {extract_paper_version(path)}")
    return True, checks, warnings


def read_pid(path: Path) -> Optional[int]:
    try:
        if not path.exists():
            return None
        pid = int(path.read_text(encoding="utf-8", errors="ignore").strip())
        return pid if pid > 1 else None
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        return raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
    except Exception:
        return ""


def proc_cgroup(pid: Optional[int]) -> str:
    try:
        if not pid:
            return ""
        return Path(f"/proc/{int(pid)}/cgroup").read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def short_cgroup(text: str) -> str:
    raw = str(text or "").replace("\n", " | ")
    if len(raw) > 180:
        return "..." + raw[-180:]
    return raw or "-"


def sidecar_management(pid: Optional[int], service: str) -> dict:
    cg = proc_cgroup(pid)
    svc_exists = service_exists(service) if service else False
    in_service = bool(pid and svc_exists and service in cg)
    if in_service:
        mode = f"systemd:{service}"
    elif svc_exists:
        mode = "direct-fallback"
    else:
        mode = "direct-fallback"
    warnings = []
    if not svc_exists:
        warnings.append("direct-fallback: systemd 서비스 미등록")
    elif not in_service and pid:
        warnings.append(f"service 등록됨 but 실행 pid가 {service} 밖에 있음")
    if cg and GUARD_SERVICE in cg:
        warnings.append(f"guard cgroup 안에서 실행중: {GUARD_SERVICE} 재시작 연동 종료 의심")
    return {"mode": mode, "service_exists": svc_exists, "in_service": in_service, "cgroup": cg, "cgroup_short": short_cgroup(cg), "warning": " / ".join(warnings) if warnings else "-"}


def service_action(service: str, action: str, timeout: int = 30) -> tuple[int, str]:
    return run(["systemctl", action, service], timeout=timeout)


def _run_privileged(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """systemd unit 설치에 필요한 명령은 root면 바로, 아니면 sudo -n으로만 시도한다."""
    rc, out = run(cmd, timeout=timeout)
    if rc == 0:
        return rc, out
    low = str(out or "").lower()
    if os.geteuid() != 0 and ("permission" in low or "authentication" in low or "access denied" in low or "denied" in low or rc != 0):
        return run(["sudo", "-n"] + cmd, timeout=timeout)
    return rc, out


def _guess_bot_user() -> str:
    try:
        if len(BOT_DIR.parts) >= 3 and BOT_DIR.parts[1] == "home":
            return BOT_DIR.parts[2]
    except Exception:
        pass
    return ENV.get("BOT_USER", "") or ENV.get("USER", "") or os.environ.get("USER", "")


def _sidecar_unit_content(kind: str, python_bin: str, active_file: str, service: str) -> str:
    home = _guess_bot_owner_home() or str(BOT_DIR.parent)
    user = _guess_bot_user()
    pyver = "python3.10" if "3.10" in str(python_bin) else f"python{sys.version_info.major}.{sys.version_info.minor}"
    py_paths = [
        str(Path(home) / ".local" / "lib" / pyver / "site-packages"),
        str(Path(home) / ".local" / "lib" / "python3.10" / "site-packages"),
        str(Path(home) / ".local" / "lib" / "python3" / "site-packages"),
    ]
    py_paths = [x for x in dict.fromkeys(py_paths) if x]
    user_line = f"User={user}\n" if user and user != "root" else ""
    desc = "TradingBot websocket sidecar" if kind == "ws" else "TradingBot Bithumb micro sidecar"
    return f"""[Unit]
Description={desc}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_line}WorkingDirectory={BOT_DIR}
EnvironmentFile=-{ENV_PATH}
Environment=TRADING_BOT_DIR={BOT_DIR}
Environment=HOME={home}
Environment=PYTHONPATH={os.pathsep.join(py_paths)}
ExecStart={python_bin} {BOT_DIR / active_file}
Restart=always
RestartSec=3
KillSignal=SIGTERM
TimeoutStopSec=15

[Install]
WantedBy=multi-user.target
"""


def ensure_sidecar_service_installed(kind: str, *, force: bool = False) -> tuple[bool, list[str]]:
    """WS/micro를 guard direct-fallback이 아니라 systemd 독립 서비스로 관리하게 만든다.

    실패하면 기존 direct-fallback을 막지는 않지만, 상태판에는 계속 구조경고가 남는다.
    """
    if kind == "ws":
        service, active_file = WS_SERVICE, WS_ACTIVE_FILE
        pyinfo = resolve_ws_python(runtime_env=ws_runtime_env())
        python_bin = str(pyinfo.get("python") or "/usr/bin/python3.10") if pyinfo.get("ok") else "/usr/bin/python3.10"
    else:
        service, active_file = MICRO_SERVICE, MICRO_ACTIVE_FILE
        python_bin = MICRO_PYTHON_BIN or "/usr/bin/python3.10"
    notes = []
    unit_path = Path(f"/etc/systemd/system/{service}.service")
    content = _sidecar_unit_content(kind, python_bin, active_file, service)
    existing = ""
    try:
        existing = unit_path.read_text(encoding="utf-8", errors="ignore") if unit_path.exists() else ""
    except Exception:
        existing = ""
    if service_exists(service) and existing.strip() == content.strip() and not force:
        notes.append(f"service already installed: {service}")
        return True, notes
    tmp = BOT_DIR / f".{service}.service.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
    except Exception as exc:
        return False, [f"unit tmp write failed: {exc.__class__.__name__}: {str(exc)[:120]}"]
    try:
        if os.geteuid() == 0:
            unit_path.write_text(content, encoding="utf-8")
            rc_cp, out_cp = 0, ""
        else:
            rc_cp, out_cp = _run_privileged(["cp", str(tmp), str(unit_path)], timeout=10)
        if rc_cp != 0:
            return False, [f"unit install failed: {tail(out_cp, 600)}"]
        _run_privileged(["chmod", "0644", str(unit_path)], timeout=8)
        rc_daemon, out_daemon = _run_privileged(["systemctl", "daemon-reload"], timeout=20)
        rc_enable, out_enable = _run_privileged(["systemctl", "enable", service], timeout=20)
        notes.append(f"service installed: {service} / python={python_bin}")
        if rc_daemon != 0: notes.append("daemon-reload: " + tail(out_daemon, 300))
        if rc_enable != 0: notes.append("enable: " + tail(out_enable, 300))
        return True, notes
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def cleanup_direct_sidecar_pids(kind: str) -> list[str]:
    """systemd 서비스 전환 뒤 guard cgroup/direct 잔류 pid를 정리한다."""
    if kind == "ws":
        service, finder = WS_SERVICE, find_ws_sidecar_pids
    else:
        service, finder = MICRO_SERVICE, find_micro_sidecar_pids
    notes = []
    if not service_exists(service):
        return notes
    for pid in list(finder()):
        if not pid_alive(pid):
            continue
        cg = proc_cgroup(pid)
        if service not in cg:
            stop_pid(pid)
            notes.append(f"direct 잔류 pid 종료: {pid}")
    return notes


def _sidecar_target_sync(kind: str, active_version: str, active_hash: str, target: Optional[Path], deployed_marker: str) -> dict:
    target_name = target.name if target and target.exists() else "?"
    target_version = (ws_target_version(target) if kind == "ws" else micro_target_version(target)) if target and target.exists() else "?"
    target_hash = short_hash(target) if target and target.exists() else "?"
    marker_ok = target_name == "?" or deployed_marker in {target_name, "?", ""}
    hash_ok = target_hash == "?" or active_hash == target_hash
    version_ok = target_version == "?" or active_version == target_version
    return {
        "target_name": target_name,
        "target_version": target_version,
        "target_hash": target_hash,
        "marker_ok": marker_ok,
        "hash_ok": hash_ok,
        "version_ok": version_ok,
        "ok": bool(marker_ok and hash_ok and version_ok),
    }


def service_sidecar_status(service: str) -> str:
    if not service_exists(service):
        return "no_service"
    return is_service_active(service)


def find_paperbot_pids() -> list[int]:
    out = []
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            pid = int(p.name)
            low = proc_cmdline(pid).lower()
            if "paper_bot.py" in low and "python" in low:
                out.append(pid)
    except Exception:
        pass
    return sorted(set(out))


def repair_paper_pid_file(pid: int) -> None:
    try:
        if pid and pid > 1:
            (BOT_DIR / PAPER_PID_FILE).write_text(str(int(pid)), encoding="utf-8")
    except Exception:
        pass


def stop_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except Exception:
        return
    for _ in range(20):
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


# obsolete direct-fallback paper restart/apply definitions removed in guard v2.5.76

def format_paper_apply_result(res: dict) -> str:
    ok = "✅" if res.get("ok") else "❌"
    changed = "교체+재시작" if res.get("changed") else ("변경없음/재시작생략" if res.get("skipped_restart") else "유지")
    lines = [f"{ok} paper_bot {changed}"]
    for k in ["target", "version", "active", "backup", "hash", "error", "warning"]:
        if res.get(k):
            lines.append(f"- {k}: {res.get(k)}")
    if res.get("notes"):
        lines.append("- notes:")
        lines.extend(f"  · {str(x)[:220]}" for x in res.get("notes", [])[:6])
    if res.get("warnings"):
        lines.append("- warnings:")
        lines.extend(f"  · {str(x)[:220]}" for x in res.get("warnings", [])[:6])
    return "\n".join(lines)



def ws_target_version(path: Path) -> str:
    if not path.exists():
        return "?"
    val = extract_assignment(path, "VERSION")
    if val != "?":
        return val
    m = re.search(r"ws_sidecar_v(\d+\.\d+)", path.name)
    return f"ws_sidecar_v{m.group(1)}" if m else path.name


def ws_status_ts(st: dict) -> float:
    try:
        return float(st.get("updated_ts") or st.get("updated_at") or st.get("ts") or 0)
    except Exception:
        return 0.0


def ws_count(st: dict, *keys: str) -> int:
    for k in keys:
        try:
            if k in st and st.get(k) not in (None, ""):
                return int(float(st.get(k) or 0))
        except Exception:
            continue
    return 0


def ws_runtime_file_note(path: Path, label: str, remove_if_not_writable: bool = False) -> str:
    """runtime 파일 권한을 가볍게 점검한다. root 소유 파일이면 unlink 후 재생성 가능하게 만든다."""
    try:
        if not path.exists():
            return f"{label}=missing"
        try:
            with path.open("a", encoding="utf-8"):
                pass
            return f"{label}=writable"
        except PermissionError:
            if remove_if_not_writable:
                try:
                    path.unlink()
                    return f"{label}=permission_fixed_by_unlink"
                except Exception as exc:
                    return f"{label}=permission_blocked:{exc.__class__.__name__}"
            return f"{label}=permission_blocked"
    except Exception as exc:
        return f"{label}=check_failed:{exc.__class__.__name__}"


def write_ws_pid_file(pid: int) -> str:
    pid_path = BOT_DIR / WS_PID_FILE
    try:
        pid_path.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        pid_path.write_text(str(int(pid)), encoding="utf-8")
        return "pid_file_written"
    except Exception as exc:
        return f"pid_file_write_failed:{exc.__class__.__name__}:{str(exc)[:120]}"


def proc_exe(pid: Optional[int]) -> str:
    try:
        if not pid:
            return ""
        return os.path.realpath(f"/proc/{int(pid)}/exe")
    except Exception:
        return ""


def _dedupe_keep_order(items: Iterable[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        s = str(item or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def ws_python_candidates() -> list[str]:
    """ws_sidecar 실행에 쓸 python 후보를 실제 성공 경로 중심으로 잡는다.
    v2.5.18: /usr/bin/python3 하나만 믿지 않는다. 현재 살아 있는 sidecar의
    /proc/<pid>/exe 경로를 우선 보고, 그 다음 설정값과 흔한 python3.10 경로를 본다.
    """
    pids = find_ws_sidecar_pids()
    running = [proc_exe(pid) for pid in pids if pid_alive(pid)]
    return _dedupe_keep_order([
        *(list(reversed(running)) if running else []),
        ENV.get("WS_PYTHON_BIN", ""),
        WS_PYTHON_BIN,
        "/usr/bin/python3.10",
        "/usr/local/bin/python3.10",
        "/usr/bin/python3",
        sys.executable,
        "python3.10",
        "python3",
    ])


def proc_environ(pid: Optional[int]) -> dict:
    """현재 정상 실행 중인 ws_sidecar의 환경을 읽어 업그레이드/재시작에 재사용한다.
    v2.5.19: guard systemd 환경과 수동 실행 환경이 달라 websockets import가 실패하던 원인 보정.
    """
    out = {}
    try:
        if not pid:
            return out
        raw = Path(f"/proc/{int(pid)}/environ").read_bytes()
        for part in raw.split(b"\x00"):
            if not part or b"=" not in part:
                continue
            k, v = part.split(b"=", 1)
            key = k.decode("utf-8", errors="ignore")
            val = v.decode("utf-8", errors="ignore")
            if key:
                out[key] = val
    except Exception:
        pass
    return out


def _guess_bot_owner_home() -> str:
    try:
        # /home/dangdang971216/trading_bot -> /home/dangdang971216
        if len(BOT_DIR.parts) >= 3 and BOT_DIR.parts[1] == "home":
            return str(Path("/") / BOT_DIR.parts[1] / BOT_DIR.parts[2])
    except Exception:
        pass
    return os.environ.get("HOME", "")


def _append_pythonpath(env: dict, paths: list[str]) -> dict:
    cur = [x for x in str(env.get("PYTHONPATH", "")).split(os.pathsep) if x]
    seen = set(cur)
    for path in paths:
        if path and Path(path).exists() and path not in seen:
            cur.append(path)
            seen.add(path)
    if cur:
        env["PYTHONPATH"] = os.pathsep.join(cur)
    return env


def ws_runtime_env() -> dict:
    """ws_sidecar 실행/검사에 쓰는 환경.
    핵심은 guard 자신의 환경이 아니라, 이미 정상수신 중인 sidecar의 HOME/PYTHONPATH/VIRTUAL_ENV를 우선 보존하는 것.
    """
    env = os.environ.copy()
    env.update(ENV)
    # 현재 정상 실행 중인 sidecar 환경을 가능한 한 복원한다.
    running_pids = [pid for pid in find_ws_sidecar_pids() if pid_alive(pid)]
    penv = {}
    if running_pids:
        penv = proc_environ(running_pids[-1])
        for key in ("HOME", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PATH", "LD_LIBRARY_PATH"):
            if penv.get(key):
                env[key] = penv[key]
    # v2.5.24: sidecar가 죽은 상태에서 guard가 root HOME을 쓰면 사용자 site-packages를 못 찾아 import_failed가 난다.
    # 실행 중인 sidecar HOME이 있으면 그것을 쓰고, 없으면 BOT_DIR 기준 소유자 홈(/home/dangdang...)을 우선한다.
    owner_home = _guess_bot_owner_home()
    home = penv.get("HOME") or owner_home or env.get("HOME")
    if home:
        env["HOME"] = home
    # 사용자 계정에 설치된 websockets를 guard systemd 환경에서도 찾게 한다.
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    user_paths = []
    if home:
        user_paths.extend([
            str(Path(home) / ".local" / "lib" / f"python{pyver}" / "site-packages"),
            str(Path(home) / ".local" / "lib" / "python3.10" / "site-packages"),
            str(Path(home) / ".local" / "lib" / "python3" / "site-packages"),
        ])
    env = _append_pythonpath(env, user_paths)
    env["TRADING_BOT_DIR"] = str(BOT_DIR)
    return env


def run_with_env(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 20, env: Optional[dict] = None):
    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env=env,
        )
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        return 124, f"TIMEOUT after {timeout}s\n{out}".strip()
    except Exception as e:
        return 1, f"{type(e).__name__}: {e}"


def ws_import_check_for(python_bin: str, runtime_env: Optional[dict] = None) -> tuple[bool, str]:
    code = (
        "import sys, os; "
        "print(sys.executable); "
        "print('HOME=' + str(os.environ.get('HOME',''))); "
        "print('PYTHONPATH=' + str(os.environ.get('PYTHONPATH',''))[:260]); "
        "import websockets; "
        "print('websockets OK', getattr(websockets, '__version__', '?'), getattr(websockets, '__file__', '?'))"
    )
    env = runtime_env or ws_runtime_env()
    rc, out = run_with_env([python_bin, "-c", code], cwd=BOT_DIR, timeout=10, env=env)
    return rc == 0, (out or "").strip()


def resolve_ws_python(preferred: Optional[str] = None, runtime_env: Optional[dict] = None) -> dict:
    """웹소켓 실행용 python을 결정한다.
    v2.5.24: 기존 sidecar를 멈추기 전의 실행환경을 잡아두고 그 환경으로 import 검사/시작까지 이어간다.
    기존 버그는 preflight 때는 성공했지만, stop 이후 실행환경이 바뀌어 start 단계에서 import_failed가 난 것이었다.
    """
    env = runtime_env or ws_runtime_env()
    candidates = _dedupe_keep_order(([preferred] if preferred else []) + ws_python_candidates())
    notes = []
    for py in candidates:
        ok, out = ws_import_check_for(py, runtime_env=env)
        first = (out.splitlines()[0] if out else "출력없음")
        notes.append(f"try {py}: {'OK' if ok else 'FAIL'} / {first}")
        if ok:
            return {"ok": True, "python": py, "notes": notes, "output": out, "candidates": candidates, "env": env}
    return {"ok": False, "python": preferred or WS_PYTHON_BIN, "notes": notes, "output": "", "candidates": candidates, "env": env}


def ws_import_check() -> tuple[bool, list[str]]:
    info = resolve_ws_python()
    lines = [f"selected={info.get('python')}", "candidates=" + ", ".join(info.get("candidates") or [])]
    lines.extend(info.get("notes") or [])
    if info.get("output"):
        lines.append(tail(str(info.get("output")), 700))
    return bool(info.get("ok")), lines


def init_ws_status_file(reason: str = "guard_restart") -> str:
    try:
        write_json(BOT_DIR / WS_STATUS_FILE, {
            "version": "guard_prestart_v2.5.14",
            "pid": 0,
            "state": "guard_starting",
            "targets": 0,
            "cached": 0,
            "fresh": 0,
            "raw_total": 0,
            "parse_ok": 0,
            "price_ok": 0,
            "amount_ok": 0,
            "match_ok": 0,
            "last_error": "-",
            "last_notice": "-",
            "last_format": "-",
            "python_path": (resolve_ws_python().get("python") if reason not in {"import_failed", "apply_import_failed"} else WS_PYTHON_BIN),
            "guard_reason": reason,
            "updated_ts": time.time(),
        })
        return "status_initialized"
    except Exception as exc:
        return f"status_init_failed:{exc.__class__.__name__}:{str(exc)[:120]}"


def choose_latest_ws_sidecar_target(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        return BOT_DIR / explicit
    targets = read_deploy_targets()
    configured = BOT_DIR / targets["ws"] if targets.get("ws") else None
    latest = find_latest_ws_sidecar_file()
    return latest or configured


def find_ws_sidecar_pids() -> list[int]:
    out = []
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            pid = int(p.name)
            low = proc_cmdline(pid).lower()
            if "python" in low and ("ws_sidecar.py" in low or "ws_sidecar_v" in low):
                out.append(pid)
    except Exception:
        pass
    return sorted(set(out))


def stop_ws_sidecar() -> tuple[bool, list[str]]:
    notes = []
    if service_exists(WS_SERVICE):
        rc, out = service_action(WS_SERVICE, "stop", timeout=25)
        time.sleep(1)
        notes += cleanup_direct_sidecar_pids("ws")
        alive = [p for p in find_ws_sidecar_pids() if pid_alive(p)]
        try:
            (BOT_DIR / WS_PID_FILE).unlink(missing_ok=True)
        except Exception:
            pass
        notes.append(f"관리방식=systemd:{WS_SERVICE}")
        notes.append(f"systemctl stop rc={rc} active={is_service_active(WS_SERVICE)}")
        if out:
            notes.append(tail(out, 500))
        return (not alive), notes + ([f"잔류 pid: {alive}"] if alive else [])
    pid_path = BOT_DIR / WS_PID_FILE
    pid = read_pid(pid_path)
    pids = set(find_ws_sidecar_pids())
    if pid:
        pids.add(pid)
    for p in sorted(pids):
        if p and pid_alive(p):
            stop_pid(p)
            notes.append(f"pid 종료: {p}")
    try:
        pid_path.unlink(missing_ok=True)
        notes.append("pid 파일 제거")
    except Exception as exc:
        notes.append(f"pid 파일 제거 실패: {exc.__class__.__name__}: {str(exc)[:100]}")
    alive = [p for p in find_ws_sidecar_pids() if pid_alive(p)]
    return (not alive), notes + ([f"잔류 pid: {alive}"] if alive else [])


def start_ws_sidecar(python_bin: Optional[str] = None, runtime_env: Optional[dict] = None, skip_import_check: bool = False) -> tuple[str, list[str]]:
    notes = []
    svc_ok, svc_notes = ensure_sidecar_service_installed("ws")
    notes += svc_notes
    if service_exists(WS_SERVICE):
        notes += cleanup_direct_sidecar_pids("ws")
        rc, out = service_action(WS_SERVICE, "start", timeout=25)
        time.sleep(2)
        st = ws_sidecar_state_dict()
        notes.append(f"관리방식=systemd:{WS_SERVICE}")
        notes.append(f"systemctl start rc={rc} active={is_service_active(WS_SERVICE)}")
        notes.append(f"state={st.get('verdict')} / raw={st.get('raw_total')} / match={st.get('match_ok')} / age={st.get('age_text')}")
        if out:
            notes.append(tail(out, 500))
        return ("active" if st.get("alive") else "failed"), notes
    if not svc_ok:
        notes.append("systemd service 설치 실패 → direct-fallback 임시 사용")
    active_path = BOT_DIR / WS_ACTIVE_FILE
    if not active_path.exists():
        return "missing", [f"active file 없음: {WS_ACTIVE_FILE}"]
    ok, out = py_compile(active_path)
    if not ok:
        return "compile_failed", [tail(out, 1200)]

    # 이미 살아 있으면 새로 띄우지 않고 pid만 보정한다.
    pids = find_ws_sidecar_pids()
    if pids:
        used = pids[-1]
        pid_note = write_ws_pid_file(used)
        return "active", [f"이미 실행중 pid={used}", pid_note, f"python_exe={proc_exe(used) or '-'}"]

    selected_env = runtime_env or ws_runtime_env()
    if skip_import_check and python_bin:
        selected_py = str(python_bin)
        notes += ["python auto-detect", f"  · preflight OK 재사용: {selected_py}"]
    else:
        pyinfo = resolve_ws_python(preferred=python_bin, runtime_env=selected_env)
        notes += ["python auto-detect"] + [f"  · {x}" for x in pyinfo.get("notes", [])[:8]]
        selected_env = pyinfo.get("env") or selected_env
        if not pyinfo.get("ok"):
            init_ws_status_file("import_failed")
            return "import_failed", notes + ["websockets import 가능한 python 없음 → sidecar 실행 중단"]
        selected_py = str(pyinfo.get("python") or WS_PYTHON_BIN)

    notes.append(init_ws_status_file("guard_start"))
    notes.append(ws_runtime_file_note(BOT_DIR / WS_PID_FILE, "pid", remove_if_not_writable=True))
    notes.append(ws_runtime_file_note(BOT_DIR / WS_LOG_FILE, "out", remove_if_not_writable=True))
    notes.append(ws_runtime_file_note(BOT_DIR / "clean_ws_sidecar.log", "log", remove_if_not_writable=False))

    try:
        log_f = open(BOT_DIR / WS_LOG_FILE, "a", encoding="utf-8")
    except Exception as exc:
        return "log_open_failed", notes + [f"log open failed: {exc.__class__.__name__}: {str(exc)[:120]}"]
    try:
        env = dict(selected_env)
        env["TRADING_BOT_DIR"] = str(BOT_DIR)
        env["WS_PYTHON_BIN_RESOLVED"] = selected_py
        p = subprocess.Popen([selected_py, str(active_path)], cwd=str(BOT_DIR), env=env, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)
        pid_note = write_ws_pid_file(p.pid)
        time.sleep(3)
        alive = pid_alive(p.pid)
        notes.append(f"direct start pid={p.pid} alive={alive}")
        notes.append(pid_note)
        notes.append(f"python_selected={selected_py}")
        notes.append(f"python_exe={proc_exe(p.pid) or selected_py}")
        state = ws_sidecar_state_dict()
        notes.append(f"state={state.get('verdict')} / raw={state.get('raw_total')} / match={state.get('match_ok')} / age={state.get('age_text')}")
        return "active" if alive else "failed", notes
    finally:
        try:
            log_f.close()
        except Exception:
            pass


def restart_ws_sidecar(python_bin: Optional[str] = None) -> tuple[str, list[str]]:
    svc_ok, notes = ensure_sidecar_service_installed("ws")
    if service_exists(WS_SERVICE):
        notes += cleanup_direct_sidecar_pids("ws")
        rc, out = service_action(WS_SERVICE, "restart", timeout=35)
        time.sleep(3)
        st = ws_sidecar_state_dict()
        notes += [f"관리방식=systemd:{WS_SERVICE}", f"systemctl restart rc={rc} active={is_service_active(WS_SERVICE)}", f"state={st.get('verdict')} / raw={st.get('raw_total')} / match={st.get('match_ok')} / age={st.get('age_text')}"]
        if out:
            notes.append(tail(out, 500))
        return ("active" if st.get("alive") else "failed"), notes
    if not svc_ok:
        notes.append("systemd service 설치 실패 → direct-fallback 임시 재시작")
    # v2.5.24: 기존 sidecar가 살아 있을 때의 PYTHONPATH/HOME을 멈추기 전에 잡아두고,
    # 같은 환경으로 새 sidecar를 시작한다. stop 이후 import 재검사로 실패하던 문제를 막는다.
    pre_env = ws_runtime_env()
    pyinfo = resolve_ws_python(preferred=python_bin, runtime_env=pre_env)
    pre_notes = ["python auto-detect before stop"] + [f"  · {x}" for x in pyinfo.get("notes", [])[:8]]
    if not pyinfo.get("ok"):
        return "import_failed", pre_notes + ["websockets import 가능한 python 없음 → 기존 sidecar는 건드리지 않음"]
    selected_py = str(pyinfo.get("python") or python_bin or WS_PYTHON_BIN)
    selected_env = pyinfo.get("env") or pre_env
    _ok, stop_notes = stop_ws_sidecar()
    active, start_notes = start_ws_sidecar(python_bin=selected_py, runtime_env=selected_env, skip_import_check=True)
    return active, pre_notes + [f"selected_python={selected_py}"] + stop_notes + start_notes


def ws_sidecar_state_dict() -> dict:
    active_path = BOT_DIR / WS_ACTIVE_FILE
    pid_file = read_pid(BOT_DIR / WS_PID_FILE)
    proc_pids = find_ws_sidecar_pids()
    used = pid_file if (pid_file and pid_alive(pid_file)) else (proc_pids[-1] if proc_pids else None)
    pid_repair = "-"
    if used and (not pid_file or pid_file != used):
        pid_repair = write_ws_pid_file(used)
    st = read_json(BOT_DIR / WS_STATUS_FILE)
    if not isinstance(st, dict):
        st = {}
    ts = ws_status_ts(st)
    age = time.time() - ts if ts > 0 else -1
    alive = bool(used and pid_alive(used))
    raw_total = ws_count(st, "raw_total", "raw_count", "raw")
    parse_ok = ws_count(st, "parse_ok", "parse")
    price_ok = ws_count(st, "price_ok", "price")
    amount_ok = ws_count(st, "amount_ok", "amount")
    match_ok = ws_count(st, "match_ok", "match")
    cached = ws_count(st, "cached", "cache")
    fresh = ws_count(st, "fresh")
    last_error = str(st.get("last_error") or "-")
    last_notice = str(st.get("last_notice") or "-")
    # v2.5.25: 구 ws_sidecar가 정상 재구독 문구를 last_error에 남긴 경우도 상태판에서 오류로 보지 않는다.
    if _is_reconnect_notice(last_error):
        if last_notice in {"", "-", "None", "none"}:
            last_notice = last_error
        last_error = "-"
    last_format = str(st.get("last_format") or "-")
    state = str(st.get("state") or "-")
    status_version = str(st.get("version") or "-")
    age_ok = 0 <= age <= 30
    rx_ok = parse_ok > 0 and price_ok > 0 and match_ok > 0
    err_ok = last_error in {"", "-", "None", "none"} or _is_reconnect_notice(last_error)
    mgmt = sidecar_management(used, WS_SERVICE)
    latest_target = choose_latest_ws_sidecar_target()
    deployed_marker = read_file(BOT_DIR / DEPLOYED_WS_FILE) or "?"
    sync = _sidecar_target_sync("ws", ws_target_version(active_path), short_hash(active_path), latest_target, deployed_marker)
    stop_suspect = (not alive) and (state == "종료" or "normal stop" in last_notice.lower() or "signal" in last_error.lower() or "signal" in last_notice.lower())
    if not active_path.exists():
        verdict = "❌ active file 없음"
        ok = False
    elif not sync.get("ok"):
        verdict = f"⚠️ active/latest 불일치: active {ws_target_version(active_path)} / latest {sync.get('target_version')} / deployed {deployed_marker}"
        ok = False
    elif not alive:
        verdict = "❌ 정지"
        ok = False
    elif age_ok and rx_ok and err_ok:
        verdict = "✅ 정상수신"
        ok = True
    elif age_ok and state in {"연결", "수신대기", "대상준비", "guard_starting", "재구독중", "대상변경"} and err_ok:
        verdict = "❔ 실행중 / 수신대기"
        ok = False
    elif age_ok and not err_ok:
        verdict = "⚠️ 실행중 / 수신오류"
        ok = False
    elif alive:
        verdict = "⚠️ 실행중이나 status 오래됨"
        ok = False
    else:
        verdict = "❌ 확인불가"
        ok = False
    return {
        "ok": ok,
        "verdict": verdict,
        "active_file": WS_ACTIVE_FILE,
        "active_exists": active_path.exists(),
        "version": ws_target_version(active_path),
        "status_version": status_version,
        "deployed_marker": deployed_marker,
        "target": sync.get("target_name"),
        "target_version": sync.get("target_version"),
        "target_hash": sync.get("target_hash"),
        "sync_ok": sync.get("ok"),
        "hash": short_hash(active_path),
        "pid_file": pid_file,
        "proc_pids": proc_pids,
        "used_pid": used,
        "alive": alive,
        "python_path": proc_exe(used) or WS_PYTHON_BIN,
        "configured_python": WS_PYTHON_BIN,
        "pid_repair": pid_repair,
        "status": st,
        "age": age,
        "age_text": f"{age:.1f}s" if age >= 0 else "-",
        "raw_total": raw_total,
        "parse_ok": parse_ok,
        "price_ok": price_ok,
        "amount_ok": amount_ok,
        "match_ok": match_ok,
        "cached": cached,
        "fresh": fresh,
        "state": state,
        "last_error": last_error,
        "last_notice": last_notice,
        "last_format": last_format,
        "target_hash": st.get("target_hash", "-"),
        "management": mgmt.get("mode"),
        "service_exists": mgmt.get("service_exists"),
        "in_service": mgmt.get("in_service"),
        "cgroup": mgmt.get("cgroup_short"),
        "management_warning": mgmt.get("warning"),
        "stop_suspect": stop_suspect,
    }


def wait_ws_ready(prev_raw: int = 0, prev_match: int = 0, timeout: float = 22.0) -> tuple[dict, list[str]]:
    """v2.5.20: /gws_upgrade 직후 raw 0이라고 바로 실패로 보지 않고 수신 시작을 기다린다."""
    notes = []
    deadline = time.time() + max(5.0, timeout)
    best = ws_sidecar_state_dict()
    while time.time() < deadline:
        st = ws_sidecar_state_dict()
        best = st
        raw = int(st.get("raw_total") or 0)
        match = int(st.get("match_ok") or 0)
        alive = bool(st.get("alive"))
        err = str(st.get("last_error") or "-")
        err_ok = err in {"", "-", "None", "none"} or _is_reconnect_notice(err)
        if alive and err_ok and (raw > prev_raw or match > prev_match or (raw > 0 and match > 0)):
            notes.append(f"ready raw={raw} match={match} age={st.get('age_text')}")
            return st, notes
        if not alive:
            notes.append("wait: process not alive")
            break
        time.sleep(2)
    notes.append(f"wait_timeout raw={best.get('raw_total',0)} match={best.get('match_ok',0)} verdict={best.get('verdict')}")
    return best, notes


def apply_ws_sidecar_latest(force: bool = False, explicit: Optional[str] = None, restart: bool = True) -> dict:
    """WS sidecar 최신 적용 단일 본선.

    v2.5.29 원칙:
    - hash 동일 + 프로세스 alive + status 최신/오류없음이면 여기서 즉시 return 한다.
    - 이 경우 py_compile/import/old restart path/restart_ws_sidecar()로 절대 내려가지 않는다.
    - 실제 재시작은 hash 변경, force, 프로세스 정지, status 오래됨/오류처럼 이유가 있을 때만 한다.
    """
    target = choose_latest_ws_sidecar_target(explicit=explicit)
    service_notes: list[str] = []
    if target and target.exists():
        _svc_ok, service_notes = ensure_sidecar_service_installed("ws")
    before_state = ws_sidecar_state_dict()
    before_raw = int(before_state.get("raw_total") or 0)
    before_match = int(before_state.get("match_ok") or 0)
    active_path = BOT_DIR / WS_ACTIVE_FILE
    if not target or not target.exists():
        return {"ok": True, "changed": False, "target": explicit or "?", "active": before_state.get("verdict", "유지"), "warning": "ws_sidecar_v*.py 최신 파일 없음 → 기존 상태 유지", "notes": ["대상 파일 없음: 기존 실행은 건드리지 않음"]}

    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    before_age = float(before_state.get("age") or -1)
    alive = bool(before_state.get("alive"))
    err = str(before_state.get("last_error") or "-")
    err_ok = err in {"", "-", "None", "none"} or _is_reconnect_notice(err)
    status_recent = 0 <= before_age <= 90
    state_text = str(before_state.get("state") or "")
    waiting_recent = 0 <= before_age <= 20 and state_text in {"연결", "수신대기", "대상준비", "재구독중", "대상변경"}
    has_rx = int(before_state.get("match_ok") or 0) > 0 or int(before_state.get("raw_total") or 0) > 0 or int(before_state.get("fresh") or 0) > 0 or waiting_recent
    management_ok = str(before_state.get("management") or "").startswith("systemd:") and bool(before_state.get("in_service"))
    ws_healthy_enough = bool(alive and status_recent and err_ok and has_rx and management_ok and before_state.get("sync_ok", True))

    if same_hash and not force and ws_healthy_enough:
        try:
            (BOT_DIR / DEPLOYED_WS_FILE).write_text(target.name + "\n", encoding="utf-8")
        except Exception:
            pass
        return {
            "ok": True, "changed": False, "target": target.name, "version": ws_target_version(target),
            "active": "restart 생략", "backup": "없음", "hash": short_hash(active_path),
            "notes": service_notes + [
                "single-path fast-skip: hash 동일 + alive + status 최신 + 수신흔적 있음 + systemd service pid",
                "py_compile/import/stop/start/restart 경로 완전 우회",
                f"pid 유지: {before_state.get('used_pid') or '-'} / raw {before_state.get('raw_total',0)} / match {before_state.get('match_ok',0)} / age {before_state.get('age_text','-')}",
            ],
            "warning": "변경 없음 → 실행중인 WS 유지 / 재시작 생략",
            "state": before_state.get("verdict"), "raw": before_state.get("raw_total"), "match": before_state.get("match_ok"),
            "selected_python": before_state.get("python_path") or WS_PYTHON_BIN,
            "restart_reason": "-",
        }

    restart_reasons = []
    if force:
        restart_reasons.append("force")
    if not same_hash:
        restart_reasons.append("hash 변경")
    if not alive:
        restart_reasons.append("프로세스 정지")
    if alive and not status_recent:
        restart_reasons.append(f"status 오래됨 {before_state.get('age_text','-')}")
    if alive and not err_ok:
        restart_reasons.append(f"수신오류 {err[:80]}")
    if alive and status_recent and err_ok and not has_rx:
        restart_reasons.append("수신흔적 부족")
    if not management_ok:
        restart_reasons.append("direct-fallback → systemd service 전환")
    if not before_state.get("sync_ok", True):
        restart_reasons.append("active/deployed/latest 불일치")

    okc, comp_out = py_compile(target)
    if not okc:
        return {"ok": False, "changed": False, "target": target.name, "error": "ws_sidecar py_compile 실패", "notes": [tail(comp_out, 1000)], "restart_reason": " / ".join(restart_reasons) or "검수실패"}

    backup = "없음"
    backup_path = None
    changed = False
    previous_deployed_ws = (read_file(BOT_DIR / DEPLOYED_WS_FILE) or "").strip()
    if not same_hash or force:
        if active_path.exists():
            backup_path = BOT_DIR / f"{active_path.name}.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(active_path, backup_path)
            backup = backup_path.name
        tmp = BOT_DIR / f"{active_path.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target, tmp)
        if sha256_file(tmp) != sha256_file(target):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "changed": False, "target": target.name, "error": "ws 임시복사 hash 불일치", "restart_reason": " / ".join(restart_reasons)}
        os.replace(tmp, active_path)
        changed = True
    try:
        (BOT_DIR / DEPLOYED_WS_FILE).write_text(target.name + "\n", encoding="utf-8")
    except Exception:
        pass

    notes = service_notes + ["restart path entered only because: " + (" / ".join(restart_reasons) or "unknown")]
    selected_py = before_state.get("python_path") or WS_PYTHON_BIN
    active = "restart 생략"
    ready_state = before_state
    if restart:
        active, restart_notes = restart_ws_sidecar(python_bin=str(selected_py))
        notes += restart_notes
        ready_state, wait_notes = wait_ws_ready(prev_raw=0 if changed else before_raw, prev_match=0 if changed else before_match, timeout=18.0)
        notes += wait_notes
    else:
        ready_state = ws_sidecar_state_dict()
        notes.append("restart=False → 교체만 하고 재시작 생략")

    ready_err = str(ready_state.get("last_error") or "-")
    ws_ok = bool(ready_state.get("alive")) and (int(ready_state.get("parse_ok") or 0) > 0 or int(ready_state.get("raw_total") or 0) > 0) and (ready_err in {"", "-", "None", "none"} or _is_reconnect_notice(ready_err))
    rollback_note = ""
    if not ws_ok and changed and backup_path and backup_path.exists():
        try:
            shutil.copy2(backup_path, active_path)
            rollback_note = f"rollback={backup_path.name}"
            (BOT_DIR / DEPLOYED_WS_FILE).write_text((previous_deployed_ws or target.name) + "\n", encoding="utf-8")
            active2, rb_notes = restart_ws_sidecar(python_bin=str(selected_py))
            rb_state, rb_wait = wait_ws_ready(timeout=14.0)
            notes += [rollback_note, f"rollback_restart={active2}"] + rb_notes + rb_wait
            ready_state = rb_state
        except Exception as exc:
            notes.append(f"rollback 실패: {exc.__class__.__name__}: {str(exc)[:160]}")
    final_ok = bool(ready_state.get("alive")) and (int(ready_state.get("parse_ok") or 0) > 0 or int(ready_state.get("raw_total") or 0) > 0)
    return {"ok": final_ok, "changed": changed, "target": target.name, "version": ws_target_version(target), "active": active, "backup": backup, "hash": short_hash(active_path), "notes": notes, "warning": rollback_note or ("재시작 수행: " + (" / ".join(restart_reasons) or "unknown")), "state": ready_state.get("verdict"), "raw": ready_state.get("raw_total"), "match": ready_state.get("match_ok"), "selected_python": selected_py, "restart_reason": " / ".join(restart_reasons) or "-"}


def gws_state_text() -> str:
    d = ws_sidecar_state_dict()
    proc = ",".join(map(str, d.get("proc_pids") or [])) or "-"
    return "\n".join([
        "🛰 웹소켓 직원 /gws_state",
        str(d.get("verdict", "?")),
        f"- active_file: {d.get('active_file')} / exists={d.get('active_exists')} / active_version={d.get('version')} / status_version={d.get('status_version','-')} / hash={d.get('hash')}",
        f"- deployed_marker: {d.get('deployed_marker','?')} / target {d.get('target','?')} / target_version {d.get('target_version','?')} / target_hash {d.get('target_hash','-')} / sync={d.get('sync_ok')}",
        f"- pid_file: {d.get('pid_file') or '-'} / proc: {proc} / used: {d.get('used_pid') or '-'} / alive={d.get('alive')}",
        f"- 관리방식: {d.get('management','-')} / 경고: {d.get('management_warning','-')}",
        f"- cgroup: {d.get('cgroup','-')}",
        f"- python: {d.get('python_path') or '-'} / configured: {d.get('configured_python') or '-'}",
        f"- python후보: {', '.join(ws_python_candidates()[:4])}",
        f"- status_age: {d.get('age_text','-')} / state: {d.get('state','-')} / last_error: {d.get('last_error','-')}",
        f"- notice: {d.get('last_notice','-')}",
        f"- raw {d.get('raw_total',0)} / parse {d.get('parse_ok',0)} / price {d.get('price_ok',0)} / amount {d.get('amount_ok',0)} / match {d.get('match_ok',0)} / format {d.get('last_format','-')}",
        f"- cache {d.get('cached',0)} / fresh {d.get('fresh',0)} / pid_note {d.get('pid_repair','-')}",
        f"- files: cache={WS_CACHE_FILE} / status={WS_STATUS_FILE} / log={WS_LOG_FILE}",
        "- 시작: /gws_start / 중지: /gws_stop / 재시작: /gws_restart / 업그레이드: /gws_upgrade",
    ])


def gws_log(lines: int = 80) -> str:
    txt = read_file_tail(BOT_DIR / WS_LOG_FILE, max_bytes=120_000)
    return f"🧾 웹소켓 직원 로그 /gwslog {lines}\n" + (tail(txt, 2600) if txt.strip() else "- 로그 없음")


def gws_start_text() -> str:
    active, notes = start_ws_sidecar()
    return "\n".join(["▶️ 웹소켓 직원 시작 /gws_start", f"- active: {active}", f"- version: {ws_target_version(BOT_DIR / WS_ACTIVE_FILE)}"] + [f"- {x}" for x in notes] + ["", "다음 확인: /gws_state /ws_status"])


def gws_stop_text() -> str:
    ok, notes = stop_ws_sidecar()
    return "\n".join(["⏹ 웹소켓 직원 중지 /gws_stop", f"- ok: {ok}"] + [f"- {x}" for x in notes])


def gws_restart_text() -> str:
    active, notes = restart_ws_sidecar()
    return "\n".join(["🔁 웹소켓 직원 재시작 /gws_restart", f"- active: {active}", f"- version: {ws_target_version(BOT_DIR / WS_ACTIVE_FILE)}"] + [f"- {x}" for x in notes] + ["", "다음 확인: /gws_state /ws_status"])


def format_ws_apply_result(res: dict) -> str:
    ok = "✅" if res.get("ok") else "❌"
    changed = "교체+재시작" if res.get("changed") else ("변경없음/재시작생략" if str(res.get("active")) == "restart 생략" else "변경없음/필요재시작")
    lines = [f"{ok} ws_sidecar {changed}"]
    for k in ["target", "version", "active", "restart_reason", "selected_python", "state", "raw", "match", "backup", "hash", "error", "warning"]:
        if res.get(k) not in (None, ""):
            lines.append(f"- {k}: {res.get(k)}")
    if res.get("notes"):
        lines.append("- notes:")
        lines.extend(f"  · {str(x)[:220]}" for x in res.get("notes", [])[:8])
    return "\n".join(lines)

def gpaper_log(lines: int = 80) -> str:
    """페이퍼봇 로그를 실행방식과 무관하게 확인한다.
    systemd journal이 비어도 paper_bot.log / process.err / process.log / pid 상태를 같이 보여준다.
    """
    lines = max(20, min(300, int(lines or 80)))
    parts = [f"🧾 페이퍼봇 로그 /gpaperlog {lines}", ""]
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    pid_path = BOT_DIR / PAPER_PID_FILE
    pid = read_pid(pid_path)
    alive = bool(pid and pid_alive(pid))
    svc_exists = service_exists(PAPER_SERVICE)
    svc_active = is_service_active(PAPER_SERVICE) if svc_exists else "no_service"
    st = read_json(BOT_DIR / "paper_bot_status.json")
    age = "-"
    try:
        uts = float(st.get("updated_at", 0) or 0)
        age = f"{max(0, time.time()-uts):.0f}s" if uts > 0 else "-"
    except Exception:
        pass
    parts += [
        "상태",
        "- 방식: fast-tail 읽기 / 큰 로그 통째읽기 안 함",
        f"- service: {PAPER_SERVICE} / exists={svc_exists} / active={svc_active}",
        f"- active_file: {PAPER_ACTIVE_FILE} / version={extract_paper_version(active_path)} / hash={short_hash(active_path)}",
        f"- pid: {pid or '-'} / alive={alive} / status_age={age}",
        "",
    ]

    shown = False
    if svc_exists:
        _rc, out = run(["journalctl", "-u", PAPER_SERVICE, "--no-pager", "-n", str(lines)], timeout=20)
        if (out or "").strip():
            parts += ["journalctl", tail(out, 1800), ""]
            shown = True
    for label, path in [
        ("paper_bot_error.log", BOT_DIR / "paper_bot_error.log"),
        ("paper_bot.log", BOT_DIR / "paper_bot.log"),
        (PAPER_PROCESS_ERR, BOT_DIR / PAPER_PROCESS_ERR),
        (PAPER_PROCESS_LOG, BOT_DIR / PAPER_PROCESS_LOG),
    ]:
        txt = read_file_tail(path, max_bytes=120_000)
        if txt.strip():
            parts += [label, tail(txt, 1800), ""]
            shown = True
            if len("\n".join(parts)) > 3300:
                break
    if not shown:
        parts += [
            "❔ 로그 내용이 비어있음",
            "- 흔한 원인: paper_bot이 direct 실행인데 journal만 본 경우, 또는 시작 직후 아직 로그가 적히지 않은 경우",
            "- 다음 확인: /gpaper_restart 후 /gpaperlog 120",
        ]
    return "\n".join(parts).strip()


def gpaper_state_text() -> str:
    snap = paper_runtime_snapshot()
    reasons = list(snap.get('reasons') or [])
    verdict = '✅ 정상: paper runtime 단일선 exact 일치' if snap.get('ok') else '⚠️ CHECK: ' + (' / '.join(reasons[:5]) if reasons else 'runtime 증거 부족')
    started = snap.get('process_started_at')
    try:
        started_text = datetime.fromtimestamp(float(started)).strftime('%Y-%m-%d %H:%M:%S') if started else '-'
    except Exception:
        started_text = '-'
    return '\n'.join([
        '🧪 페이퍼봇 상태 /gpaper_state · v995 one-command release spine',
        verdict,
        f"- service: {snap.get('service')} / active={snap.get('service_active')} / MainPID={snap.get('main_pid') or '-'} / alive={snap.get('main_alive')}",
        f"- active_file: {snap.get('active_file')} / version={snap.get('active_version')} / build={snap.get('active_build')} / hash={snap.get('active_hash')}",
        f"- pid identity: pid_file={snap.get('pid_file') or '-'} / status={snap.get('status_pid') or '-'} / writer={snap.get('writer_pid') or '-'} / proc={','.join(map(str,snap.get('proc_pids') or [])) or '-'}",
        f"- writer: owner={snap.get('writer_owner') or '-'} / expected={snap.get('expected_owner') or '-'} / count={snap.get('writer_count')} / seq={snap.get('write_seq') or '-'}",
        f"- status: version={snap.get('status_version') or '-'} / build={snap.get('status_build') or '-'} / age={float(snap.get('status_age') or 0):.1f}s / running={snap.get('running')} / stop_reason={snap.get('stop_reason') or '-'}",
        f"- process start: {started_text} / instance={snap.get('runtime_instance_id') or '-'}",
        f"- cmdline: {str(snap.get('cmdline') or '-')[:220]}",
        '- 판정: 불일치를 자동 보정하지 않고 CHECK로 공개 · 원장은 건드리지 않음',
        '- 로그 확인: /gpaperlog 120',
        '- 재시작: /gpaper_restart',
    ])


def gpaper_service_text() -> str:
    svc_exists = service_exists(PAPER_SERVICE)
    svc_active = is_service_active(PAPER_SERVICE) if svc_exists else "no_service"
    unit_path = f"/etc/systemd/system/{PAPER_SERVICE}.service"
    lines = [
        "🧩 페이퍼봇 서비스화 /gpaper_service",
        ("✅ systemd 등록됨" if svc_exists else "⚠️ systemd 미등록: paper 실행 금지 / service 설치 필요"),
        f"- service: {PAPER_SERVICE} / exists={svc_exists} / active={svc_active}",
        f"- unit: {unit_path}",
        f"- active_file: {BOT_DIR / PAPER_ACTIVE_FILE}",
        f"- env: {ENV_PATH}",
        "",
        "설치가 필요하면 SSH에서 전달한 install_paper_service.sh를 실행",
        "설치 후 확인",
        f"- systemctl status {PAPER_SERVICE} --no-pager",
        "- /gpaper_state",
        "- /pstatus",
    ]
    return "\n".join(lines)


def gpaper_restart_text() -> str:
    active, notes = restart_paper_bot()
    return "\n".join(["🔁 페이퍼봇 재시작 /gpaper_restart", f"- active: {active}", f"- version: {extract_paper_version(BOT_DIR / PAPER_ACTIVE_FILE)}"] + [f"- {n}" for n in notes])


def guard_self_upgrade(force: bool = False, explicit: Optional[str] = None) -> str:
    ok, steps = git_update()
    if not ok:
        return "❌ 가드봇 자체 업그레이드 실패\n- GitHub 갱신 실패\n\n" + "\n\n".join(steps)
    target = BOT_DIR / explicit if explicit else find_latest_guard_file()
    if not target or not target.exists():
        return "❌ 가드봇 자체 업그레이드 실패\n- backga_guard_bot_v*.py 파일을 못 찾음"
    active = BOT_DIR / GUARD_ACTIVE_FILE
    okc, out = py_compile(target)
    if not okc:
        return f"❌ 가드봇 자체 업그레이드 실패\n- py_compile 실패: {target.name}\n{tail(out, 1800)}"
    same = active.exists() and short_hash(active) == short_hash(target)
    if same and not force:
        return f"✅ 가드봇 자체 업그레이드 불필요\n- target: {target.name}\n- active: {GUARD_ACTIVE_FILE}\n- hash: {short_hash(active)}\n- 필요하면 /gguard_restart"
    backup = None
    if active.exists():
        backup = BOT_DIR / f"{active.name}.backup_guard_self_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(active, backup)
    shutil.copy2(target, active)
    (BOT_DIR / DEPLOYED_GUARD_FILE).write_text(target.name + "\n", encoding="utf-8")
    rc, out = run(["systemctl", "restart", GUARD_SERVICE], timeout=30)
    # 재시작되면 이 응답이 끝까지 못 갈 수 있어도, 대부분은 전송 후 처리된다.
    return f"🛡 가드봇 자체 업그레이드 실행\n- target: {target.name}\n- active: {GUARD_ACTIVE_FILE}\n- backup: {backup.name if backup else '없음'}\n- restart rc: {rc}\n- 다음 확인: /guard /gdeploy"


def progress_notify(text: str) -> None:
    """긴 업그레이드 중 사용자가 멈춘 것으로 오해하지 않게 즉시 단계 메시지를 보낸다."""
    try:
        send(ALLOWED_CHAT_ID, str(text or ""))
    except Exception:
        pass


def guard_post_upgrade_path() -> Path:
    return BOT_DIR / GUARD_POST_UPGRADE_FILE


def _release_inflight_path() -> Path:
    return BOT_DIR / '.guard_post_upgrade_request.inflight.json'


def _release_result_path() -> Path:
    return BOT_DIR / GUARD_RELEASE_RESULT_FILE


def _release_bundle_path(explicit: Optional[str]) -> Optional[Path]:
    name = Path(str(explicit or '')).name
    if not name or not name.startswith('coinbot_update_v') or not name.endswith('.zip'):
        return None
    path = BOT_DIR / name
    return path if path.exists() else None


def _release_bundle_digest(explicit: Optional[str]) -> str:
    path = _release_bundle_path(explicit)
    try:
        return sha256_file(path) if path else ''
    except Exception:
        return ''


def _simple_release_request(data: dict) -> dict:
    """Read both old job files and the v1000 one-shot request.

    Old pending_apply files are accepted only so an older guard can hand a new
    bundle to v1000 across the self restart. Old applying/components_done jobs
    are stale state-machine leftovers and are not resumed.
    """
    if not isinstance(data, dict) or data.get('action') != 'continue_gupgrade':
        return {}
    explicit = Path(str(data.get('explicit') or '')).name
    if not explicit:
        return {}
    stage = str(data.get('stage') or 'pending_apply')
    digest = str(data.get('bundle_digest') or _release_bundle_digest(explicit))
    return {
        'schema': 'guard_release_once_v1000',
        'action': 'continue_gupgrade',
        'request_id': str(data.get('request_id') or data.get('job_id') or f"once-{digest[:12]}-{int(time.time()*1000)}"),
        'explicit': explicit,
        'bundle_digest': digest,
        'force': bool(data.get('force')),
        'stage': stage,
        'created_ts': float(data.get('created_ts') or time.time()),
        'source_schema': str(data.get('schema') or 'legacy'),
    }


def _load_release_job(max_age_sec: float = 7200.0) -> dict:
    for path, stage in ((guard_post_upgrade_path(), 'pending_apply'), (_release_inflight_path(), 'applying')):
        obj = _simple_release_request(read_json(path) or {})
        if not obj:
            continue
        age = time.time() - float(obj.get('created_ts') or 0)
        if -60 <= age <= max_age_sec:
            obj['stage'] = stage if obj.get('source_schema') == 'guard_release_once_v1000' else str(obj.get('stage') or stage)
            obj['_path'] = str(path)
            return obj
    return {}


def write_guard_post_upgrade_request(force: bool = False, explicit: Optional[str] = None) -> dict:
    explicit_name = Path(str(explicit or '')).name
    if not explicit_name:
        raise ValueError('continuation bundle name missing')
    digest = _release_bundle_digest(explicit_name)
    req = {
        'schema': 'guard_release_once_v1000',
        'action': 'continue_gupgrade',
        'request_id': f"once-{digest[:12]}-{int(time.time()*1000)}",
        'explicit': explicit_name,
        'bundle_digest': digest,
        'force': bool(force),
        'stage': 'pending_apply',
        'created_ts': time.time(),
        'created_at': now(),
    }
    # One pending request only. No job queue, retries or component state machine.
    write_json(guard_post_upgrade_path(), req)
    try:
        _release_inflight_path().unlink(missing_ok=True)
    except Exception:
        pass
    return req


def pop_guard_post_upgrade_request(max_age_sec: float = 7200.0) -> dict:
    return _load_release_job(max_age_sec=max_age_sec)


def _write_release_result(req: dict, ok: bool, text: str) -> dict:
    result = {
        'schema': 'guard_release_once_result_v1000',
        'request_id': req.get('request_id'),
        'bundle': req.get('explicit'),
        'bundle_digest': req.get('bundle_digest'),
        'ok': bool(ok),
        'stage': 'done' if ok else 'failed',
        'finished_ts': time.time(),
        'finished_at': now(),
        'text': str(text or ''),
    }
    write_json(_release_result_path(), result)
    return result


def _apply_release_bundle_once(req: dict) -> tuple[bool, str]:
    """Apply the bundle remainder once after the guard self restart."""
    bundle = _release_bundle_path(str(req.get('explicit') or ''))
    if not bundle:
        return False, f"bundle missing: {req.get('explicit') or '-'}"
    actual_digest = sha256_file(bundle)
    expected_digest = str(req.get('bundle_digest') or '')
    if expected_digest and expected_digest != actual_digest:
        return False, f"bundle digest mismatch {expected_digest[:12]}->{actual_digest[:12]}"
    work = BOT_DIR / f".bundle_continue_once_{os.getpid()}_{time.time_ns()}"
    results=[]; notes=[]; manifest={}
    try:
        work.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(bundle, 'r') as z:
            bad = z.testzip()
            if bad:
                return False, f"zip damage: {bad}"
            z.extractall(work)
        manifest_path = work / 'DEPLOY_BUNDLE.json'
        manifest = read_json(manifest_path) if manifest_path.exists() else _bundle_manifest_defaults(work)
        if not isinstance(manifest, dict):
            return False, 'DEPLOY_BUNDLE.json invalid'
        manifest = _normalize_bundle_manifest(manifest, work)
        notes += _copy_bundle_files_to_repo(work, manifest)

        manifest_py=set()
        for key in ('main','paper','guard','ws','micro','ws_sidecar','micro_sidecar'):
            if manifest.get(key): manifest_py.add(str(manifest.get(key)))
        workers = manifest.get('workers') if isinstance(manifest.get('workers'), dict) else {}
        for fname in workers.values():
            if fname: manifest_py.add(str(fname))
        for name in [x for x in os.listdir(work) if x.endswith('.py') and x not in manifest_py]:
            okc,out=py_compile(BOT_DIR/name)
            if not okc:
                return False, f"py_compile failed {name}: {tail(out,600)}"

        for name,fname in workers.items():
            if name not in WORKER_SPECS or not fname:
                continue
            raw=apply_worker_latest(name, force=bool(req.get('force')), explicit=fname, restart=True)
            rendered=format_worker_apply_result(raw)
            results.append(f"worker:{name}\n{tail(rendered,1000)}")
            if isinstance(raw,dict) and not bool(raw.get('ok', True)):
                return False, '\n\n'.join(results)

        if manifest.get('main'):
            rendered=apply_main_latest_auto(force=bool(req.get('force')), explicit=str(manifest.get('main')), skip_git=True)
            results.append(f"main\n{tail(rendered,1200)}")
            if str(rendered).startswith(('❌','🚀 ❌')):
                return False, '\n\n'.join(results)
        if manifest.get('paper'):
            raw=apply_paper_latest(force=bool(req.get('force')), explicit=str(manifest.get('paper')))
            rendered=format_paper_apply_result(raw)
            results.append(f"paper\n{tail(rendered,1200)}")
            if isinstance(raw,dict) and not bool(raw.get('ok',True)):
                return False, '\n\n'.join(results)
        ws_manifest=manifest.get('ws') or manifest.get('ws_sidecar')
        if ws_manifest:
            raw=apply_ws_sidecar_latest(force=bool(req.get('force')), explicit=str(ws_manifest), restart=True)
            rendered=format_ws_apply_result(raw)
            results.append(f"ws\n{tail(rendered,900)}")
            if isinstance(raw,dict) and not bool(raw.get('ok',True)):
                return False, '\n\n'.join(results)
        micro_manifest=manifest.get('micro') or manifest.get('micro_sidecar')
        if micro_manifest:
            raw=apply_micro_sidecar_latest(force=bool(req.get('force')), explicit=str(micro_manifest), restart=True)
            rendered=format_micro_apply_result(raw)
            results.append(f"micro\n{tail(rendered,900)}")
            if isinstance(raw,dict) and not bool(raw.get('ok',True)):
                return False, '\n\n'.join(results)

        # One verification pass only. A failed verification is reported once;
        # it never restarts or reapplies components.
        verify = grelease_verify_text(manifest)
        verify_ok = str(verify).startswith('✅')
        body = '\n'.join([
            f"{'✅' if verify_ok else '⚠️'} 자동 배포 최종결과 {'PASS' if verify_ok else 'CHECK'}",
            f"- bundle: {bundle.name}",
            '- 방식: 가드 재시작 뒤 나머지 1회 적용 · 검수 1회 · 재시도 없음',
            f"- 적용 대상: {', '.join([x for x in ['workers' if workers else '', 'main' if manifest.get('main') else '', 'paper' if manifest.get('paper') else '', 'ws' if ws_manifest else '', 'micro' if micro_manifest else ''] if x]) or 'guard only'}",
            verify,
        ])
        return verify_ok, body + ('\n\n[적용]\n' + '\n\n'.join(results) if results else '')
    except Exception as exc:
        return False, f"⚠️ 자동 이어가기 예외: {exc.__class__.__name__}: {exc}\n{tail(traceback.format_exc(),5000)}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def resume_guard_release_job() -> bool:
    pending = guard_post_upgrade_path()
    inflight = _release_inflight_path()
    raw = read_json(pending) or {}
    req = _simple_release_request(raw)

    # v996-v999 state-machine leftovers are not resumed. They are closed once.
    if req and req.get('source_schema') != 'guard_release_once_v1000' and str(req.get('stage')) in {'applying','components_done','result_ready','delivered','failed'}:
        text = '\n'.join([
            '⚠️ 이전 자동배포 상태 정리 완료',
            f"- bundle: {req.get('explicit') or '-'}",
            f"- 이전 stage: {req.get('stage') or '-'}",
            '- v1000 단순 이어가기에서는 재적용·재검수하지 않음',
        ])
        _write_release_result(req, False, text)
        try: pending.unlink(missing_ok=True)
        except Exception: pass
        try: inflight.unlink(missing_ok=True)
        except Exception: pass
        send(ALLOWED_CHAT_ID, text)
        return True

    if not req:
        # An inflight file means a previous one-shot continuation was interrupted.
        old = _simple_release_request(read_json(inflight) or {})
        if old:
            text = '\n'.join([
                '⚠️ 자동 이어가기 중단 기록',
                f"- bundle: {old.get('explicit') or '-'}",
                '- 이전 1회 적용 process가 완료 결과를 남기지 못함',
                '- 자동 재적용은 하지 않음 · 필요 시 새 bundle 명령으로 다시 적용',
            ])
            _write_release_result(old, False, text)
            try: inflight.unlink(missing_ok=True)
            except Exception: pass
            send(ALLOWED_CHAT_ID, text)
            return True
        return False

    # Atomic claim. Once renamed, no loop iteration can claim it again.
    try:
        os.replace(pending, inflight)
    except FileNotFoundError:
        return False
    except Exception as exc:
        text=f"⚠️ 자동 이어가기 요청 claim 실패: {exc.__class__.__name__}: {exc}"
        _write_release_result(req, False, text)
        send(ALLOWED_CHAT_ID, text)
        return True

    ok, body = _apply_release_bundle_once(req)
    _write_release_result(req, ok, body)
    try: inflight.unlink(missing_ok=True)
    except Exception: pass
    send(ALLOWED_CHAT_ID, body)
    return True


def grelease_last_text() -> str:
    obj=read_json(_release_result_path()) or {}
    if isinstance(obj,dict) and obj:
        return '\n'.join([
            str(obj.get('text') or '📦 최근 자동 배포 결과'),
            '',
            f"- stage: {obj.get('stage') or '-'}",
            f"- request: {obj.get('request_id') or '-'}",
            f"- bundle digest: {str(obj.get('bundle_digest') or '-')[:16]}",
            f"- finished: {obj.get('finished_at') or '-'}",
        ])
    pending=_simple_release_request(read_json(guard_post_upgrade_path()) or {})
    if pending:
        return '\n'.join([
            '📦 자동 이어가기 대기 중',
            f"- bundle: {pending.get('explicit') or '-'}",
            f"- request: {pending.get('request_id') or '-'}",
        ])
    return '📦 최근 자동 배포 결과 없음'

def apply_guard_first_for_smart_upgrade(force: bool = False, continuation_bundle: Optional[str] = None) -> dict:
    """스마트 업그레이드 1단계: 가드봇부터 최신인지 확인한다.

    가드봇이 바뀌었으면 active 파일만 교체하고, post-upgrade 요청 파일을 남긴 뒤
    가드봇을 재시작한다. 새 가드봇이 부팅되면 남은 main/paper/ws/micro 적용을 이어간다.
    이미 최신이면 아무것도 끊지 않는다.
    """
    target = find_latest_guard_file()
    active = BOT_DIR / GUARD_ACTIVE_FILE
    if not target or not target.exists():
        return {"ok": True, "changed": False, "target": "?", "action": "skip", "warning": "backga_guard_bot_v*.py 최신 파일 없음"}
    okc, out = py_compile(target)
    if not okc:
        return {"ok": False, "changed": False, "target": target.name, "action": "error", "error": "guard py_compile 실패", "notes": [tail(out, 1200)]}
    same = active.exists() and short_hash(active) == short_hash(target)
    if same:
        try:
            (BOT_DIR / DEPLOYED_GUARD_FILE).write_text(target.name + "\n", encoding="utf-8")
        except Exception:
            pass
        return {"ok": True, "changed": False, "target": target.name, "action": "skip", "hash": short_hash(active), "warning": "가드봇 hash 동일 → 재시작 생략"}
    backup = None
    if active.exists():
        backup = BOT_DIR / f"{active.name}.backup_guard_smart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(active, backup)
    tmp = BOT_DIR / f"{active.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(target, tmp)
    if sha256_file(tmp) != sha256_file(target):
        tmp.unlink(missing_ok=True)
        return {"ok": False, "changed": False, "target": target.name, "action": "error", "error": "guard 임시복사 hash 불일치"}
    os.replace(tmp, active)
    (BOT_DIR / DEPLOYED_GUARD_FILE).write_text(target.name + "\n", encoding="utf-8")
    # v2.5.78: one-command release. Persist the exact bundle request before self restart;
    # the new guard resumes the remaining component apply from the local verified zip.
    release_job = write_guard_post_upgrade_request(force=force, explicit=continuation_bundle)
    rc, out = run(["systemctl", "restart", GUARD_SERVICE], timeout=30)
    return {"ok": rc == 0, "changed": True, "target": target.name, "action": "restart_then_auto_continue", "backup": backup.name if backup else "없음", "hash": short_hash(active), "restart_rc": rc, "restart_out": tail(out, 500), "job_id": release_job.get("request_id"), "restart_warning": "새 가드봇은 pending bundle을 1회만 이어서 적용하고 요청파일을 삭제함"}

def _local_upgrade_targets_present() -> tuple[bool, str]:
    """v2.5.42: GitHub fetch가 일시 timeout일 때 로컬 repo에 적용 대상이 이미 있는지 확인한다."""
    checks = []
    patterns = [
        "수익형_v*.py",
        "paper_bot_v*.py",
        "backga_guard_bot_v*.py",
        "scanner_worker_v*.py",
        "candle_worker_v*.py",
        "market_regime_worker_v*.py",
        "feature_worker_v*.py",
        "orderflow_worker_v*.py",
        "risk_worker_v*.py",
        "strategy_worker_v*.py",
        "review_worker_v*.py",
        "coinbot_update_v*.zip",
    ]
    ok = False
    for pat in patterns:
        matches = sorted(BOT_DIR.glob(pat))
        if matches:
            ok = True
            checks.append(f"{pat}: {matches[-1].name}")
        else:
            checks.append(f"{pat}: 없음")
    return ok, "\n".join(checks[:30])


def git_update(allow_local_fallback: bool = False, fetch_timeout: int = 60) -> tuple[bool, list[str]]:
    steps = []
    # v2.2: GitHub 최신 파일 자동선택 전에 dubious ownership으로 fetch가 막히지 않게 고정한다.
    rc, out = run(["git", "config", "--global", "--add", "safe.directory", str(BOT_DIR)], cwd=BOT_DIR, timeout=15)
    steps.append(f"git safe.directory: rc={rc}\n{tail(out, 300)}")
    # v2.5.62: 25초 fetch timeout이 반복되어 bundle 적용 전 단계에서 계속 막히던 문제 수정.
    # 기본 fetch 대기시간을 늘리되, 오래된 로컬 zip을 무조건 적용하는 fallback은 하지 않는다.
    rc, out = run(["git", "fetch", "--prune", "origin"], cwd=BOT_DIR, timeout=int(fetch_timeout or 60))
    steps.append(f"git fetch --prune origin: rc={rc} / timeout={int(fetch_timeout or 60)}s\n{tail(out, 900)}")
    if rc != 0:
        if allow_local_fallback:
            present, detail = _local_upgrade_targets_present()
            steps.append("로컬 fallback 확인:\n" + detail)
            if present:
                steps.append("⚠️ GitHub fetch 실패/timeout이지만, 로컬 repo의 기존 파일 기준으로 적용을 계속합니다. 새로 push한 파일이 로컬에 없으면 적용되지 않을 수 있으므로 /gdeploy hash와 target을 반드시 확인하세요.")
                return True, steps
        return False, steps
    rc, out = run(["git", "reset", "--hard", f"origin/{GIT_BRANCH}"], cwd=BOT_DIR, timeout=60)
    steps.append(f"git reset --hard origin/{GIT_BRANCH}: rc={rc} / timeout=60s\n{tail(out, 900)}")
    if rc != 0:
        if allow_local_fallback:
            present, detail = _local_upgrade_targets_present()
            steps.append("로컬 fallback 확인:\n" + detail)
            if present:
                steps.append("⚠️ GitHub reset 실패지만 로컬 파일 기준으로 적용을 계속합니다. /gdeploy로 target/hash를 확인하세요.")
                return True, steps
        return False, steps
    return True, steps


def py_compile(path: Path) -> tuple[bool, str]:
    rc, out = run([PYTHON_BIN, "-m", "py_compile", str(path)], cwd=BOT_DIR, timeout=45)
    return rc == 0, out


def static_alias_check(path: Path) -> list[str]:
    """Catch import-time NameError patterns like: build_x = build_deleted_y."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as e:
        return [f"SyntaxError: {e}"]

    known = set(dir(builtins)) | {"__name__", "__file__", "Path", "datetime", "time", "os", "sys", "json", "re"}
    problems = []

    def add_target(t):
        if isinstance(t, ast.Name):
            known.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for sub in t.elts:
                add_target(sub)

    watch_prefix = ("build_", "format_", "handle_", "cmd_", "summary_", "render_")
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                known.add(alias.asname or alias.name.split(".")[0])
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            known.add(node.name)
            continue
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Name):
                rhs = node.value.id
                if rhs not in known and (rhs.startswith(watch_prefix) or rhs.endswith("_command")):
                    lhs = ",".join(t.id for t in node.targets if isinstance(t, ast.Name)) or "?"
                    problems.append(f"line {node.lineno}: {lhs} = {rhs} / RHS 미정의 가능")
            for t in node.targets:
                add_target(t)
            continue
        if isinstance(node, ast.AnnAssign):
            add_target(node.target)
            continue
    return problems[:30]


def required_command_check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # v2.13.319+ clean worker hub는 /core,/trade,/deploy 중심이 아니라
    # /health,/score,/quality,/strategy_watch,/errorlog 캐시 전용 명령 중심이다.
    modern_required = ["health", "score", "quality", "strategy_watch", "errorlog"]
    if all(c in text for c in modern_required):
        return []
    legacy_required = ["/core", "/trade", "/deploy", "/upgradestatus"]
    missing = [c for c in legacy_required if c not in text]
    if missing:
        return ["필수 명령어 문자열 없음: " + ", ".join(missing)]
    return []


def validate_target(path: Path) -> tuple[bool, list[str], list[str]]:
    checks = []
    warnings = []
    if not path.exists():
        return False, [f"대상 파일 없음: {path.name}"], warnings
    if not version_from_filename(path):
        return False, [f"파일명 형식 오류: {path.name}"], warnings

    # v2.4: 기존 validate 병목은 전체 AST alias 검사였다.
    # 기본은 빠른 검증(py_compile + BOT_VERSION + 필수 문자열)만 하고,
    # 필요할 때만 GUARD_STATIC_ALIAS_CHECK=1로 무거운 검사를 켠다.
    ok, out = py_compile(path)
    checks.append("✅ py_compile 통과" if ok else "❌ py_compile 실패\n" + tail(out, 1600))
    if not ok:
        return False, checks, warnings

    if STATIC_ALIAS_CHECK_ENABLED:
        alias_problems = static_alias_check(path)
        if alias_problems:
            checks.append("❌ 미정의 alias 의심\n" + "\n".join(alias_problems[:10]))
            return False, checks, warnings
        checks.append("✅ 미정의 alias 사전검사 통과")
    else:
        checks.append("✅ alias 정적검사 생략(fast mode)")

    cmd_missing = required_command_check(path)
    if cmd_missing:
        warnings.extend(cmd_missing)
    else:
        checks.append("✅ 필수 명령어 문자열 확인")

    bot_version = extract_bot_version(path)
    if bot_version == "?":
        warnings.append("BOT_VERSION을 못 찾음")
    else:
        checks.append(f"✅ BOT_VERSION: {bot_version}")
    return True, checks, warnings


def is_main_service_active() -> str:
    rc, active = run(["systemctl", "is-active", MAIN_SERVICE], timeout=8)
    return (active or "?").strip()


def is_status_timeout_value(active: str) -> bool:
    s = str(active or "").strip().lower()
    return s.startswith("timeout after") or "timeout after" in s or s in {"timeout", "timed_out"}


def recent_journal(lines: int = 80, since: Optional[str] = None, timeout_sec: int = 4) -> str:
    """메인봇 journal tail을 짧게만 읽는다.
    v2.5.7: journalctl이 막히면 가드봇 polling까지 멈추므로 timeout을 짧게 두고
    status fallback만 반환한다.
    """
    lines = max(20, min(160, int(lines or 80)))
    cmd = ["journalctl", "-u", MAIN_SERVICE, "--no-pager", "-n", str(lines)]
    if since:
        cmd = ["journalctl", "-u", MAIN_SERVICE, "--no-pager", "--since", since, "-n", str(lines)]
    rc, out = run(cmd, timeout=max(2, int(timeout_sec)))
    if rc == 124:
        rc2, st = run(["systemctl", "status", MAIN_SERVICE, "--no-pager", "-l"], timeout=3)
        return (
            f"journalctl timeout after {timeout_sec}s - 가드봇 보호용으로 중단\n"
            f"systemctl status fallback rc={rc2}\n"
            f"{tail(st, 1800)}"
        )
    return out or ""


def has_fatal_log(text: str) -> bool:
    patterns = ["Traceback (most recent call last)", "NameError:", "SyntaxError:", "ImportError:", "ModuleNotFoundError:", "PyCompileError"]
    return any(p in (text or "") for p in patterns)


def restart_service(short: bool = False) -> str:
    rc, out = run(["systemctl", "restart", MAIN_SERVICE], timeout=30)
    time.sleep(3)
    active = is_main_service_active()
    bot_version, target, deployed, latest = get_bot_versions()
    if short:
        return f"🔁 재시작\n- rc: {rc}\n- active: {active}\n- bot.py: {bot_version}\n- deployed: {deployed}"
    log = recent_journal(40)
    return (
        f"🔁 재시작 /grestart\n"
        f"- restart rc: {rc}\n"
        f"- active: {active}\n"
        f"- bot.py: {bot_version}\n"
        f"- 최신파일: {latest}\n"
        f"- .deployed_target: {deployed}\n\n"
        f"[recent log]\n{tail(log, 2600)}"
    )


def rollback_to_file(src: Path, reason: str = "") -> str:
    dst = BOT_DIR / "bot.py"
    if not src.exists():
        return f"↩️ 롤백 실패\n- 백업 없음: {src.name}"
    safety = BOT_DIR / f"bot.py.before_guard_rollback_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if dst.exists():
        shutil.copy2(dst, safety)
    shutil.copy2(src, dst)
    ok, out = py_compile(dst)
    if not ok:
        if safety.exists():
            shutil.copy2(safety, dst)
        return f"↩️ 롤백 실패\n- 대상: {src.name}\n- py_compile 실패, 원복함\n{tail(out, 1800)}"
    (BOT_DIR / ".deployed_target").write_text(src.name + "\n", encoding="utf-8")
    msg = restart_service(short=True)
    data = {"time": now(), "ok": False, "action": "rollback", "target": src.name, "reason": reason, "active": is_main_service_active()}
    write_json(BOT_DIR / UPGRADE_STATUS_FILE, data)
    return f"↩️ 롤백 완료\n- 적용 백업: {src.name}\n- 이유: {reason or '-'}\n\n{msg}"


def list_backups_text() -> str:
    files = collect_backups()
    if not files:
        return "📦 백업 /gbackups\n- 백업 파일 없음"
    lines = ["📦 백업 /gbackups"]
    for i, p in enumerate(files[:10], 1):
        ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{i}. {p.name} / {p.stat().st_size:,} bytes / {ts}")
    return "\n".join(lines)


def collect_backups() -> list[Path]:
    files = []
    for pat in ["bot.py.backup_*", "bot.py.bad_*", "bot.py.bak_*", "bot.py.before_guard_rollback_*"]:
        files.extend(BOT_DIR.glob(pat))
    return sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def rollback_latest() -> str:
    files = collect_backups()
    if not files:
        return "↩️ 롤백 /grollback\n- 백업 파일 없음"
    return rollback_to_file(files[0], reason="수동 /grollback")


def apply_main_latest_auto(force: bool = False, explicit: Optional[str] = None, skip_git: bool = False) -> str:
    started = now()
    total_t0 = perf_now()
    steps = []
    timings = []
    status_path = BOT_DIR / UPGRADE_STATUS_FILE
    target = None
    backup = None

    def mark(stage: str, t0: float):
        timings.append((stage, perf_now() - t0))

    try:
        auto = autodeploy_status()
        if auto.get("problem"):
            write_json(status_path, {"time": now(), "ok": False, "stage": "autodeploy_alive", "detail": auto.get("text", "")})
            return (
                "❌ 업그레이드 중단 /gupgrade\n"
                "- 구 1분 자동배포 timer/service가 살아있거나 표시됨\n"
                "- 이 상태에서 업그레이드하면 v31 회귀 위험이 있음\n\n"
                f"[autodeploy]\n{auto.get('text','?')}\n\n"
                "먼저 tradingbot-autodeploy.timer/service를 stop/disable/mask 또는 격리해야 함"
            )

        if skip_git:
            steps.append("git: smart /gupgrade에서 이미 갱신 완료 → 메인 단계 git 생략")
            timings.append(("git_skip", 0.0))
        else:
            t0 = perf_now()
            ok, git_steps = git_update()
            mark("git", t0)
            steps.extend(git_steps)
            if not ok:
                write_json(status_path, {"time": now(), "ok": False, "stage": "git", "steps": steps, "timings": timings})
                return "❌ 업그레이드 실패 /gupgrade\n- GitHub 갱신 실패\n\n" + "\n\n".join(steps)

        t0 = perf_now()
        target = BOT_DIR / explicit if explicit else find_latest_source_file()
        mark("find_latest", t0)
        if not target:
            write_json(status_path, {"time": now(), "ok": False, "stage": "find_latest", "timings": timings})
            return "❌ 업그레이드 실패 /gupgrade\n- 수익형_v*.py 파일을 못 찾음"
        if not target.exists():
            write_json(status_path, {"time": now(), "ok": False, "stage": "target_missing", "target": str(target), "timings": timings})
            return f"❌ 업그레이드 실패 /gupgrade\n- 대상 파일 없음: {target.name}"

        target_ver = version_from_filename(target)
        current_ver = BotVersion.parse(extract_bot_version(BOT_DIR / "bot.py"))
        if current_ver and target_ver and target_ver < current_ver and not force:
            return f"❌ 업그레이드 중단\n- 최신 파일이 현재보다 낮음: {target.name} < {current_ver}\n- 강제 적용은 /gupgrade force {target.name}"

        t0 = perf_now()
        valid, checks, warnings = validate_target(target)
        mark("validate", t0)
        target_version_text = extract_bot_version(target)
        if not valid:
            write_json(status_path, {"time": now(), "ok": False, "stage": "validate", "target": target.name, "checks": checks, "warnings": warnings, "timings": timings})
            return "❌ 업그레이드 실패 /gupgrade\n- 적용 전 검수 실패\n\n" + "\n".join(checks + warnings)

        dst = BOT_DIR / "bot.py"
        # v2.5.3: 대상 파일과 active bot.py가 같으면 교체/재시작하지 않는다.
        # 메인봇만 안 바뀐 경우 paper_bot 작업만 진행하고, 반대도 마찬가지다.
        if dst.exists() and short_hash(dst) == short_hash(target) and not force:
            active = is_main_service_active()
            runtime_version = extract_recent_runtime_version()
            marker_target = read_file(BOT_DIR / ".deployed_target") or "?"
            write_deploy_markers(target.name)
            write_json(status_path, {
                "time": now(), "ok": True, "target": target.name,
                "target_version": target_version_text, "bot_version": extract_bot_version(dst),
                "runtime_version": runtime_version, "active": active,
                "changed": False, "skipped_restart": True, "timings": timings,
            })
            timing_text = "\n".join(f"- {name}: {fmt_sec(sec)}" for name, sec in timings) or "- skip: 0.0s"
            return (
                "🚀 ✅ 메인봇 변경 없음 /gupgrade\n"
                f"- target: {target.name}\n"
                f"- bot.py: {extract_bot_version(dst)}\n"
                f"- 실행로그: {runtime_version}\n"
                f"- active: {active}\n"
                "- 처리: hash 동일 → 교체/재시작 생략\n"
                f"- 단계별 시간:\n{timing_text}"
            )

        if not dst.exists():
            write_json(status_path, {"time": now(), "ok": False, "stage": "bot_missing", "target": target.name, "timings": timings})
            return "❌ 업그레이드 실패\n- bot.py가 없음"

        t0 = perf_now()
        backup = BOT_DIR / f"bot.py.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(dst, backup)
        tmp = BOT_DIR / f"bot.py.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target, tmp)
        if sha256_file(target) != sha256_file(tmp):
            tmp.unlink(missing_ok=True)
            write_json(status_path, {"time": now(), "ok": False, "stage": "tmp_hash", "target": target.name, "backup": backup.name, "timings": timings})
            return "❌ 업그레이드 실패\n- 임시파일 복사 hash 검증 실패"
        os.replace(tmp, dst)
        if sha256_file(target) != sha256_file(dst):
            shutil.copy2(backup, dst)
            write_json(status_path, {"time": now(), "ok": False, "stage": "replace_hash", "target": target.name, "backup": backup.name, "timings": timings})
            return f"❌ 업그레이드 실패\n- bot.py 교체 후 hash 검증 실패, 백업 복구: {backup.name}"
        mark("copy/hash", t0)

        bot_version_after_copy = extract_bot_version(dst)
        if not same_version_label(bot_version_after_copy, target_version_text):
            shutil.copy2(backup, dst)
            write_json(status_path, {"time": now(), "ok": False, "stage": "bot_version_after_copy", "target": target.name, "target_version": target_version_text, "bot_version": bot_version_after_copy, "backup": backup.name, "timings": timings})
            return (
                "❌ 업그레이드 실패\n"
                "- bot.py 교체 직후 내부버전이 대상과 다름\n"
                f"- target: {target.name} / {target_version_text}\n"
                f"- bot.py: {bot_version_after_copy}\n"
                f"- 백업 복구: {backup.name}"
            )

        t0 = perf_now()
        ok, out = py_compile(dst)
        mark("post_compile", t0)
        if not ok:
            shutil.copy2(backup, dst)
            write_json(status_path, {"time": now(), "ok": False, "stage": "post_compile", "target": target.name, "backup": backup.name, "timings": timings})
            return f"❌ 업그레이드 실패\n- 교체된 bot.py compile 실패, 백업 복구: {backup.name}\n{tail(out, 1800)}"

        write_deploy_markers(target.name)

        t0 = perf_now()
        # v2.5.48: systemctl restart가 stop job 대기 때문에 30초를 꽉 채우던 병목을 줄인다.
        # timeout이 나도 아래 active/hash/version 검증으로 실제 기동 여부를 확인한다.
        rc, out = run(["systemctl", "restart", MAIN_SERVICE], timeout=16)
        mark("restart_cmd", t0)

        active = "unknown"
        fatal = False
        log_since = ""
        waited = 0
        wait_limit = max(8, min(22, int(RESTART_WAIT_SEC)))
        target_hash = short_hash(target)
        bot_hash = short_hash(dst)
        quick_ok = False
        t0 = perf_now()
        while waited < wait_limit:
            time.sleep(2)
            waited += 2
            active = is_main_service_active()
            bot_version_now = extract_bot_version(dst)
            hash_ok = (short_hash(target) == short_hash(dst))
            version_ok = same_version_label(bot_version_now, target_version_text)
            # journalctl은 느리므로 매 2초마다 읽지 않는다. active/hash/version이 맞으면 빠르게 성공 처리한다.
            if active == "active" and hash_ok and version_ok:
                quick_ok = True
                break
            if active in {"failed", "inactive"}:
                log_since = recent_journal(since=started)
                fatal = has_fatal_log(log_since)
                if fatal:
                    break
        if not log_since:
            log_since = recent_journal(since=started)
            fatal = has_fatal_log(log_since)
        mark("verify_wait", t0)

        bot_version_after_restart = extract_bot_version(dst)
        runtime_version = extract_recent_runtime_version(since=started)
        target_hash = short_hash(target)
        bot_hash = short_hash(dst)

        force_recover_notes = []
        if active == "deactivating" and not fatal:
            # v2.5.35: 기존 main이 긴 scan/factory 작업 중이면 systemctl restart가 deactivating에 머문다.
            # 메인봇은 후보생성 brain이고 paper/장부/WS/micro와 분리되어 있으므로, 업그레이드 시 현재 scan 1회만 버리고
            # 새 target을 빠르게 올리기 위해 main service에만 SIGKILL 후 start를 수행한다.
            reason0 = f"main stop hang active=deactivating waited={waited}s"
            krc, kout = run(["systemctl", "kill", "-s", "SIGKILL", MAIN_SERVICE], timeout=10)
            time.sleep(1)
            src2, sout2 = run(["systemctl", "start", MAIN_SERVICE], timeout=35)
            force_recover_notes += [f"deactivating_recover={reason0}", f"systemctl kill rc={krc}", f"systemctl start rc={src2}"]
            t_force = perf_now()
            waited2 = 0
            while waited2 < 25:
                time.sleep(5)
                waited2 += 5
                active = is_main_service_active()
                log_since = recent_journal(since=started)
                fatal = has_fatal_log(log_since)
                bot_version_now = extract_bot_version(dst)
                hash_ok = (short_hash(target) == short_hash(dst))
                version_ok = same_version_label(bot_version_now, target_version_text)
                if active == "active" and not fatal and hash_ok and version_ok:
                    quick_ok = True
                    break
                if fatal:
                    break
            mark("deactivating_recover_wait", t_force)

        if active != "active" or fatal:
            reason = f"재시작 후 active={active}, fatal_log={fatal}, waited={waited}s"
            if force_recover_notes:
                reason += " / " + " / ".join(force_recover_notes)
            bad = BOT_DIR / f"bot.py.bad_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                shutil.copy2(dst, bad)
            except Exception:
                pass
            if AUTO_ROLLBACK and backup and backup.exists():
                rb = rollback_to_file(backup, reason=reason)
                write_json(status_path, {"time": now(), "ok": False, "stage": "post_restart", "target": target.name, "target_version": target_version_text, "backup": backup.name, "rolled_back": True, "reason": reason, "runtime_version": runtime_version, "timings": timings})
                return (
                    f"❌ 업그레이드 실패 후 자동복구\n"
                    f"- target: {target.name} / {target_version_text}\n"
                    f"- reason: {reason}\n"
                    f"- bad backup: {bad.name if bad.exists() else '?'}\n\n"
                    f"{rb}\n\n[최근 로그]\n{tail(log_since, 2200)}"
                )
            write_json(status_path, {"time": now(), "ok": False, "stage": "post_restart", "target": target.name, "target_version": target_version_text, "backup": backup.name if backup else "?", "rolled_back": False, "reason": reason, "runtime_version": runtime_version, "timings": timings})
            return f"❌ 업그레이드 실패\n- target: {target.name} / {target_version_text}\n- reason: {reason}\n- 자동복구 꺼짐\n\n[최근 로그]\n{tail(log_since, 2600)}"

        marker_target = read_file(BOT_DIR / ".deployed_target") or "?"
        total_elapsed = perf_now() - total_t0
        data = {
            "time": now(), "ok": True, "target": target.name, "target_version": target_version_text,
            "backup": backup.name if backup else "?", "target_hash": target_hash, "bot_hash": bot_hash,
            "bot_version": bot_version_after_restart, "runtime_version": runtime_version,
            "active": active, "warnings": warnings, "waited_sec": waited,
            "timings": timings, "total_sec": total_elapsed, "quick_ok": quick_ok,
        }
        write_json(status_path, data)

        warn_lines = list(warnings or [])
        if not same_version_label(bot_version_after_restart, target_version_text):
            warn_lines.append(f"bot.py 내부버전 불일치: {bot_version_after_restart} != {target_version_text}")
        if 'force_recover_notes' in locals() and force_recover_notes:
            warn_lines.append("main deactivating fast-recover 수행: " + " / ".join(force_recover_notes))
        if runtime_version not in {"시작로그 대기", "시작로그 확인불가"} and not same_version_label(runtime_version, target_version_text):
            warn_lines.append(f"실행로그 버전 확인 필요: {runtime_version} != {target_version_text}")
        if marker_target != target.name:
            warn_lines.append(f".deployed_target 갱신 확인 필요: {marker_target}")
        warn_text = "\n".join(f"- ❔ {w}" for w in warn_lines) if warn_lines else "- 없음"
        timing_text = "\n".join(f"- {name}: {fmt_sec(sec)}" for name, sec in timings)

        verdict = "✅ 업그레이드 성공"
        if warn_lines:
            verdict = "❔ 업그레이드 적용, 확인 필요"
        return (
            f"🚀 {verdict} /gupgrade\n"
            f"- target: {target.name}\n"
            f"- target 내부: {target_version_text}\n"
            f"- bot.py 내부: {bot_version_after_restart}\n"
            f"- 실행로그: {runtime_version}\n"
            f"- .deployed_target: {marker_target}\n"
            f"- backup: {backup.name if backup else '?'}\n"
            f"- active: {active}\n"
            f"- wait: {waited}s / limit {wait_limit}s / quick: {'yes' if quick_ok else 'no'}\n"
            f"- total: {fmt_sec(total_elapsed)}\n"
            f"- hash: target {target_hash} / bot.py {bot_hash}\n"
            f"- 단계별 시간:\n{timing_text}\n"
            f"- 경고:\n{warn_text}\n\n"
            f"다음 확인: /gdeploy 또는 메인봇 /health /score /quality /errorlog"
        )
    except Exception as e:
        write_json(status_path, {"time": now(), "ok": False, "stage": "exception", "target": target.name if target else "?", "backup": backup.name if backup else "?", "error": f"{type(e).__name__}: {e}", "timings": timings})
        return f"❌ 업그레이드 예외\n- {type(e).__name__}: {e}"


def micro_health_repair_if_needed() -> dict:
    before = micro_state_dict()
    if not micro_desired_enabled():
        return {"attempted": False, "before": before, "after": before, "notes": ["desired disabled"]}
    age = float(before.get("status_age_num") or -1)
    need = (not before.get("alive")) or (age > 90)
    if not need:
        return {"attempted": False, "before": before, "after": before, "notes": ["healthy"]}
    active, rn = restart_micro_sidecar(python_bin=MICRO_PYTHON_BIN)
    after, wn = wait_micro_ready(timeout=18.0)
    return {"attempted": True, "before": before, "after": after, "notes": [f"repair_restart={active}"] + rn + wn}


def format_guard_apply_result(res: dict) -> str:
    ok = "✅" if res.get("ok") else "❌"
    if res.get("action") in {"restart_guard", "restart_guard_only"}:
        mode = "교체+재시작"
    elif res.get("changed"):
        mode = "교체"
    else:
        mode = "변경없음/재시작생략"
    lines = [f"{ok} guard_bot {mode}"]
    for k in ["target", "backup", "hash", "restart_rc", "error", "warning"]:
        if res.get(k) not in (None, ""):
            lines.append(f"- {k}: {res.get(k)}")
    if res.get("notes"):
        lines.append("- notes:")
        lines.extend(f"  · {str(x)[:220]}" for x in res.get("notes", [])[:8])
    return "\n".join(lines)


def apply_latest_auto(force: bool = False, explicit: Optional[str] = None, skip_guard: bool = False, skip_git: bool = False, allow_local_fallback: bool = False) -> str:
    """스마트 전체 업그레이드.

    v2.5.39: guard → workers → main → paper → WS → micro.
    worker는 systemd 독립 서비스로 관리한다. hash 동일이면 재시작하지 않는다.
    """
    total_t0 = perf_now()
    status_path = BOT_DIR / UPGRADE_STATUS_FILE
    status = read_json(status_path)
    progress_notify("🚀 스마트 업그레이드 시작\n- 순서: 가드봇 → worker → 메인봇 → 페이퍼봇 → WS → micro\n- 원칙: hash 동일 최신 항목은 교체/재시작 생략")

    disk_ok, disk_text = preupgrade_disk_guard(force=force)
    progress_notify(disk_text)
    if not disk_ok:
        write_json(status_path, {"time": now(), "ok": False, "stage": "disk_precheck", "guard_version": VERSION})
        return "❌ 스마트 업그레이드 중단 /gupgrade\n" + disk_text
    if skip_git:
        steps = ["git: /gupgrade nofetch 요청 → GitHub fetch/reset 생략, 현재 서버 로컬 파일 기준으로 진행"]
        progress_notify("[1/6] GitHub 최신 파일 확인 생략\n- /gupgrade nofetch: 서버 로컬 파일 기준으로 적용")
    else:
        progress_notify("[1/6] GitHub 최신 파일 확인 중")
        ok_git, steps = git_update(allow_local_fallback=allow_local_fallback)
        if not ok_git:
            write_json(status_path, {"time": now(), "ok": False, "stage": "git", "steps": steps, "guard_version": VERSION})
            return "❌ 스마트 업그레이드 실패 /gupgrade\n- 단계: GitHub 갱신 실패\n- 조치: 네트워크 지연이면 잠시 후 재시도. 이미 서버에 파일이 받아져 있다면 /gupgrade nofetch 로 로컬 기준 적용 가능.\n\n" + "\n\n".join(steps)

    guard_res = {"ok": True, "changed": False, "action": "skip", "warning": "skip_guard=True"}
    if not skip_guard:
        progress_notify("[2/6] 가드봇 최신 여부 확인 중\n- 바뀐 가드봇이 있으면 먼저 가드봇만 최신화하고, 새 가드가 나머지를 이어감")
        guard_res = apply_guard_first_for_smart_upgrade()
        status["guard_bot"] = guard_res
        status["guard_version"] = VERSION
        write_json(status_path, status)
        if not guard_res.get("ok"):
            return "❌ 스마트 업그레이드 중단 /gupgrade\n- 단계: guard 검수/교체 실패\n\n" + format_guard_apply_result(guard_res)
        if guard_res.get("action") == "restart_guard":
            msg = (
                "🛡 가드봇 먼저 최신화됨 /gupgrade\n"
                "- 이유: 새 가드봇에만 최신 업그레이드 기능이 있을 수 있음\n"
                "- 처리: guard 교체 후 재시작 요청 완료\n"
                "- 다음 처리: 새 가드봇이 부팅되면 worker → 메인봇 → 페이퍼봇 → WS → micro를 자동으로 이어서 확인/적용\n\n"
                + format_guard_apply_result(guard_res)
            )
            progress_notify(msg)
            return msg
    else:
        progress_notify("[2/6] 가드봇 확인 생략\n- 방금 가드봇 자체 최신화 후 자동 이어가기 중")

    progress_notify("[3/6] 리뉴얼 worker 확인 중\n- scanner/candle/market/feature/orderflow/risk/strategy/review systemd 관리 적용")
    workers_res = apply_workers_latest(force=force, restart=True)
    workers_ok = bool(workers_res.get("ok"))

    progress_notify("[4/6] 메인봇 확인 중\n- hash 동일이면 재시작하지 않음")
    main_text = apply_main_latest_auto(force=force, explicit=explicit, skip_git=True)
    main_ok = "❌" not in main_text and "업그레이드 예외" not in main_text and "중단" not in main_text

    progress_notify("[5/6] 페이퍼봇 확인 중\n- hash 동일이면 재시작하지 않음")
    paper_res = apply_paper_latest(force=force, explicit=None)
    paper_ok = bool(paper_res.get("ok"))

    progress_notify("[6/6] 외부직원 확인 중\n- WS/micro hash 동일 + 실행중이면 기존 restart 경로를 타지 않고 즉시 유지")
    ws_res = apply_ws_sidecar_latest(force=force, explicit=None, restart=True)
    ws_ok = bool(ws_res.get("ok"))
    micro_res = apply_micro_sidecar_latest(force=force, explicit=None, restart=True)
    micro_ok = bool(micro_res.get("ok"))

    status = read_json(status_path)
    status.update({
        "guard_bot": guard_res,
        "workers": workers_res.get("results", {}),
        "paper": paper_res,
        "ws_sidecar": ws_res,
        "micro_sidecar": micro_res,
        "guard_version": VERSION,
        "smart_upgrade_total_sec": perf_now() - total_t0,
        "ok": bool(main_ok and paper_ok and ws_ok and micro_ok and workers_ok),
        "order": ["guard", "workers", "main", "paper", "ws", "micro"],
        "changed_only_restart_policy": True,
    })
    write_json(status_path, status)

    all_ok = bool(main_ok and paper_ok and ws_ok and micro_ok and workers_ok)
    title = "🚀 ✅ 전체 최신화 완료 /gupgrade" if all_ok else "🚀 ❔ 전체 최신화 확인 필요 /gupgrade"
    progress_notify("✅ 스마트 업그레이드 단계 완료\n- 최종 결과 정리 전송 중")
    return (
        f"{title}\n"
        f"- guard: {VERSION}\n"
        f"- 순서: 가드봇 → worker → 메인봇 → 페이퍼봇 → WS → micro\n"
        f"- 방식: 파일 hash 비교 후 바뀐 파일만 교체·재시작\n"
        f"- 최신 동일 항목: 유지 / 재시작 생략\n"
        f"- total: {fmt_sec(perf_now() - total_t0)}\n\n"
        f"[가드봇]\n{format_guard_apply_result(guard_res)}\n\n"
        f"[worker]\n" + "\n\n".join(format_worker_apply_result(workers_res.get("results", {}).get(n, {})) for n in WORKER_SPECS) + "\n\n" +
        f"[메인봇]\n{main_text}\n\n"
        f"[페이퍼봇]\n{format_paper_apply_result(paper_res)}\n\n"
        f"[웹소켓직원]\n{format_ws_apply_result(ws_res)}\n\n"
        f"[호가·체결직원]\n{format_micro_apply_result(micro_res)}\n\n"
        f"다음 확인:\n"
        f"- 가드봇: /gdeploy /gworker_state /gexternal_state\n"
        f"- 메인봇: /health /score /quality /errorlog\n"
        f"- 페이퍼봇: /pstatus /perror"
    )

def choose_latest_micro_sidecar_target(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        return BOT_DIR / explicit
    targets = read_deploy_targets()
    configured = BOT_DIR / targets["micro"] if targets.get("micro") else None
    latest = find_latest_micro_sidecar_file()
    return latest or configured


def micro_target_version(path: Path) -> str:
    if not path or not path.exists():
        return "?"
    val = extract_assignment(path, "VERSION")
    if val != "?":
        return val
    m = re.search(r"bithumb_micro_sidecar_v(\d+\.\d+)", path.name)
    return f"bithumb_micro_sidecar_v{m.group(1)}" if m else path.name


def micro_desired_enabled() -> bool:
    p = BOT_DIR / MICRO_DESIRED_FILE
    if not p.exists():
        return False
    raw = read_file(p).strip().lower()
    val = raw.splitlines()[0].strip() if raw else ""
    return val in {"1", "on", "true", "enabled", "yes"}


def set_micro_desired(enabled: bool, reason: str = "manual") -> None:
    try:
        text = ("enabled" if enabled else "disabled") + "\n" + f"reason={reason}\ntime={now()}\n"
        (BOT_DIR / MICRO_DESIRED_FILE).write_text(text, encoding="utf-8")
    except Exception:
        pass


def _micro_write_starting_status(reason: str = "guard_starting") -> None:
    try:
        write_json(BOT_DIR / MICRO_STATUS_FILE, {
            "version": micro_target_version(BOT_DIR / MICRO_ACTIVE_FILE),
            "pid": os.getpid(),
            "state": "시작중",
            "last_error": "-",
            "targets": 0,
            "cached": 0,
            "fresh": 0,
            "orderbook_ok": 0,
            "trade_ok": 0,
            "poll_count": 0,
            "updated_ts": time.time(),
            "reason": reason,
        })
    except Exception:
        pass


def _is_reconnect_notice(err: str) -> bool:
    e = str(err or "").lower()
    return "target file changed" in e or "reconnect" in e or "재구독" in e


def find_micro_sidecar_pids() -> list[int]:
    out = []
    try:
        for p in Path("/proc").iterdir():
            if not p.name.isdigit():
                continue
            pid = int(p.name)
            low = proc_cmdline(pid).lower()
            if "python" in low and ("bithumb_micro_sidecar.py" in low or "bithumb_micro_sidecar_v" in low):
                out.append(pid)
    except Exception:
        pass
    return sorted(set(out))


def micro_state_dict() -> dict:
    active = BOT_DIR / MICRO_ACTIVE_FILE
    pid_file = read_pid(BOT_DIR / MICRO_PID_FILE)
    proc_pids = find_micro_sidecar_pids()
    used = pid_file if pid_file and pid_alive(pid_file) else (proc_pids[-1] if proc_pids else None)
    st = read_json(BOT_DIR / MICRO_STATUS_FILE)
    ts = ws_status_ts(st)
    age = time.time() - ts if ts else -1
    fresh = ws_count(st, "fresh")
    cached = ws_count(st, "cached")
    orderbook_ok = ws_count(st, "orderbook_ok")
    trade_ok = ws_count(st, "trade_ok")
    last_error = str(st.get("last_error") or "-") if isinstance(st, dict) else "-"
    alive = bool(used and pid_alive(used))
    mgmt = sidecar_management(used, MICRO_SERVICE)
    latest_target = choose_latest_micro_sidecar_target()
    deployed_marker = read_file(BOT_DIR / DEPLOYED_MICRO_FILE) or "?"
    sync = _sidecar_target_sync("micro", micro_target_version(active), short_hash(active), latest_target, deployed_marker)
    stop_suspect = (not alive) and (str(st.get("state") or "") == "종료" or "signal" in last_error.lower() or "normal stop" in last_error.lower())
    if active.exists() and not sync.get("ok"):
        verdict = f"⚠️ active/latest 불일치: active {micro_target_version(active)} / latest {sync.get('target_version')} / deployed {deployed_marker}"
        ok = False
    elif alive and age >= 0 and age <= 30 and (fresh > 0 or cached > 0):
        verdict = "✅ 정상수집"
        ok = True
    elif alive and (age < 0 or age > 60):
        verdict = "⚠️ 실행중이나 상태갱신 없음"
        ok = False
    elif alive:
        verdict = "⚠️ 실행중이나 수집대기"
        ok = False
    else:
        verdict = "❌ 정지"
        ok = False
    return {
        "ok": ok,
        "verdict": verdict,
        "active_file": MICRO_ACTIVE_FILE,
        "active_exists": active.exists(),
        "version": micro_target_version(active),
        "hash": short_hash(active),
        "deployed_marker": deployed_marker,
        "target": sync.get("target_name"),
        "target_version": sync.get("target_version"),
        "target_hash": sync.get("target_hash"),
        "sync_ok": sync.get("ok"),
        "pid_file": pid_file,
        "proc_pids": proc_pids,
        "used_pid": used,
        "alive": alive,
        "python_path": proc_exe(used) or MICRO_PYTHON_BIN,
        "configured_python": MICRO_PYTHON_BIN,
        "age_text": f"{age:.1f}s" if age >= 0 else "-",
        "state": str(st.get("state") or "-") if isinstance(st, dict) else "-",
        "last_error": last_error,
        "targets": ws_count(st, "targets"),
        "cached": cached,
        "fresh": fresh,
        "orderbook_ok": orderbook_ok,
        "trade_ok": trade_ok,
        "poll_count": ws_count(st, "poll_count"),
        "target_source": str(st.get("target_source") or "-") if isinstance(st, dict) else "-",
        "reload_count": ws_count(st, "target_reload_count"),
        "desired_enabled": micro_desired_enabled(),
        "status_age_num": age,
        "management": mgmt.get("mode"),
        "service_exists": mgmt.get("service_exists"),
        "in_service": mgmt.get("in_service"),
        "cgroup": mgmt.get("cgroup_short"),
        "management_warning": mgmt.get("warning"),
        "stop_suspect": stop_suspect,
    }


def stop_micro_sidecar() -> tuple[bool, list[str]]:
    set_micro_desired(False, "gmicro_stop")
    notes = []
    if service_exists(MICRO_SERVICE):
        rc, out = service_action(MICRO_SERVICE, "stop", timeout=25)
        time.sleep(1)
        notes += cleanup_direct_sidecar_pids("micro")
        alive = [p for p in find_micro_sidecar_pids() if pid_alive(p)]
        try:
            (BOT_DIR / MICRO_PID_FILE).unlink(missing_ok=True)
        except Exception:
            pass
        notes.append(f"관리방식=systemd:{MICRO_SERVICE}")
        notes.append(f"systemctl stop rc={rc} active={is_service_active(MICRO_SERVICE)}")
        if out:
            notes.append(tail(out, 500))
        return (not alive), notes
    pid_path = BOT_DIR / MICRO_PID_FILE
    pid = read_pid(pid_path)
    pids = set(find_micro_sidecar_pids())
    if pid:
        pids.add(pid)
    for p in sorted(pids):
        if p and pid_alive(p):
            stop_pid(p)
            notes.append(f"pid 종료: {p}")
    try:
        pid_path.unlink(missing_ok=True)
        notes.append("pid 파일 제거")
    except Exception as exc:
        notes.append(f"pid 파일 제거 실패: {exc.__class__.__name__}: {str(exc)[:100]}")
    alive = [p for p in find_micro_sidecar_pids() if pid_alive(p)]
    return (not alive), notes


def start_micro_sidecar(python_bin: Optional[str] = None) -> tuple[str, list[str]]:
    notes = []
    set_micro_desired(True, "gmicro_start")
    svc_ok, svc_notes = ensure_sidecar_service_installed("micro")
    notes += svc_notes
    if service_exists(MICRO_SERVICE):
        notes += cleanup_direct_sidecar_pids("micro")
        rc, out = service_action(MICRO_SERVICE, "start", timeout=25)
        time.sleep(2)
        st = micro_state_dict()
        notes.append(f"관리방식=systemd:{MICRO_SERVICE}")
        notes.append(f"systemctl start rc={rc} active={is_service_active(MICRO_SERVICE)}")
        notes.append(f"state={st.get('verdict')} / fresh={st.get('fresh')} / cached={st.get('cached')} / poll={st.get('poll_count')} / age={st.get('age_text')}")
        if out:
            notes.append(tail(out, 500))
        return ("started" if st.get("alive") else "failed"), notes
    if not svc_ok:
        notes.append("systemd service 설치 실패 → direct-fallback 임시 사용")
    active_path = BOT_DIR / MICRO_ACTIVE_FILE
    if not active_path.exists():
        return "missing", [f"active file 없음: {MICRO_ACTIVE_FILE}"]
    py = python_bin or MICRO_PYTHON_BIN
    okc, out = py_compile(active_path)
    if not okc:
        return "compile_failed", [tail(out, 1000)]
    # v2.5.25: pid_file 하나만 믿지 않고 기존 micro 프로세스를 전부 정리한 뒤 1개만 띄운다.
    old_pids = [p for p in find_micro_sidecar_pids() if pid_alive(p)]
    for p in old_pids:
        stop_pid(p)
        notes.append(f"기존 pid 정리: {p}")
    try:
        (BOT_DIR / MICRO_PID_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    notes.append(ws_runtime_file_note(BOT_DIR / MICRO_PID_FILE, "pid", remove_if_not_writable=True))
    notes.append(ws_runtime_file_note(BOT_DIR / MICRO_LOG_FILE, "log", remove_if_not_writable=True))
    _micro_write_starting_status("guard_start")
    notes.append("status_initialized")
    env = os.environ.copy(); env.update(ENV); env["TRADING_BOT_DIR"] = str(BOT_DIR)
    try:
        log_f = open(BOT_DIR / MICRO_LOG_FILE, "a", encoding="utf-8")
        p = subprocess.Popen([py, str(active_path)], cwd=str(BOT_DIR), stdout=log_f, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            (BOT_DIR / MICRO_PID_FILE).write_text(str(int(p.pid)), encoding="utf-8")
            notes.append("pid_file_written")
        except Exception as exc:
            notes.append(f"pid_file_write_failed:{exc.__class__.__name__}:{str(exc)[:120]}")
        notes.append(f"pid={p.pid}")
    except Exception as exc:
        return "start_failed", [f"{exc.__class__.__name__}: {str(exc)[:160]}"] + notes
    time.sleep(2)
    alive = pid_alive(p.pid)
    dup = [x for x in find_micro_sidecar_pids() if x != p.pid and pid_alive(x)]
    if dup:
        notes.append(f"중복 pid 경고: {dup}")
    return "started" if alive else "exited", notes


def restart_micro_sidecar(python_bin: Optional[str] = None) -> tuple[str, list[str]]:
    set_micro_desired(True, "gmicro_restart")
    ok, notes = stop_micro_sidecar()
    # stop은 desired를 disabled로 남기므로 restart 의도를 다시 enabled로 복구한다.
    set_micro_desired(True, "gmicro_restart")
    active, start_notes = start_micro_sidecar(python_bin=python_bin)
    return active, notes + start_notes


def wait_micro_ready(prev_poll: int = 0, timeout: float = 45.0, target_version: str = "") -> tuple[dict, list[str]]:
    """micro 재시작 후 준비확인.

    v2.5.33: v0.7은 wide target REST 수집 때문에 첫 poll/cache가 늦을 수 있다.
    이전 v2.5.32는 잠깐 started 후 fresh/cache/poll이 0인 준비구간을 실패로 보고
    정상 적용된 v0.7을 v0.5 백업으로 되돌렸다. 이제는 아래를 분리한다.
    - 확정 성공: alive + 수집흔적(fresh/cache/poll/orderbook/trade)
    - 준비중 성공: alive + status 최신 + active/latest sync + last_error 없음
    - 확정 실패: 프로세스 사망 또는 status가 오래되고 오류가 명확함
    """
    notes = []
    deadline = time.time() + max(12.0, timeout)
    best = micro_state_dict()
    target_version = str(target_version or "")
    while time.time() < deadline:
        st = micro_state_dict()
        best = st
        poll = int(st.get("poll_count") or 0)
        fresh = int(st.get("fresh") or 0)
        cached = int(st.get("cached") or 0)
        order_ok = int(st.get("orderbook_ok") or 0)
        trade_ok = int(st.get("trade_ok") or 0)
        age = float(st.get("status_age_num") or -1)
        err = str(st.get("last_error") or "-")
        err_ok = err in {"", "-", "None", "none"}
        synced = bool(st.get("sync_ok", False))
        version_ok = (not target_version) or str(st.get("version") or "") == target_version or str(st.get("target_version") or "") == target_version
        has_collection = fresh > 0 or cached > 0 or poll > prev_poll or order_ok > 0 or trade_ok > 0
        if st.get("alive") and has_collection and synced:
            notes.append(f"ready fresh={fresh} cached={cached} poll={poll} age={st.get('age_text')} sync={synced}")
            return st, notes
        if st.get("alive") and synced and version_ok and 0 <= age <= 30 and err_ok:
            state = str(st.get("state") or "-")
            if state in {"시작", "수집중", "대상준비", "guard_starting", "재시도대기"} or not has_collection:
                notes.append(f"warmup_ok version={st.get('version')} fresh={fresh} cached={cached} poll={poll} age={st.get('age_text')} sync={synced}")
                return st, notes
        if not st.get("alive"):
            notes.append("wait: process not alive")
            break
        time.sleep(3)
    notes.append(f"wait_timeout fresh={best.get('fresh',0)} cached={best.get('cached',0)} poll={best.get('poll_count',0)} sync={best.get('sync_ok')} version={best.get('version')}")
    return best, notes


def apply_micro_sidecar_latest(force: bool = False, explicit: Optional[str] = None, restart: bool = True) -> dict:
    """micro sidecar 최신 적용 단일 본선.

    v2.5.29 원칙:
    - hash 동일 + 프로세스 alive + status 최신/수집흔적 있으면 즉시 return.
    - 이 경우 py_compile/stop/start/restart_micro_sidecar()로 절대 내려가지 않는다.
    - 재시작은 hash 변경, force, 정지, status 오래됨/오류처럼 이유가 있을 때만 한다.
    """
    target = choose_latest_micro_sidecar_target(explicit=explicit)
    service_notes: list[str] = []
    if target and target.exists():
        _svc_ok, service_notes = ensure_sidecar_service_installed("micro")
    before = micro_state_dict()
    prev_poll = int(before.get("poll_count") or 0)
    active_path = BOT_DIR / MICRO_ACTIVE_FILE
    if not target or not target.exists():
        return {"ok": True, "changed": False, "target": explicit or "?", "active": before.get("verdict", "유지"), "warning": "bithumb_micro_sidecar_v*.py 최신 파일 없음 → 기존 상태 유지", "notes": ["대상 파일 없음: 기존 실행은 건드리지 않음"]}

    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    before_age = float(before.get("status_age_num") or -1)
    alive = bool(before.get("alive"))
    err = str(before.get("last_error") or "-")
    err_ok = err in {"", "-", "None", "none"}
    status_recent = 0 <= before_age <= 90
    state_text = str(before.get("state") or "")
    waiting_recent = 0 <= before_age <= 20 and state_text in {"수집중", "시작", "guard_starting"}
    has_collection = int(before.get("fresh") or 0) > 0 or int(before.get("cached") or 0) > 0 or int(before.get("poll_count") or 0) > 0 or int(before.get("orderbook_ok") or 0) > 0 or int(before.get("trade_ok") or 0) > 0 or waiting_recent
    management_ok = str(before.get("management") or "").startswith("systemd:") and bool(before.get("in_service"))
    micro_healthy_enough = bool(alive and status_recent and err_ok and has_collection and management_ok and before.get("sync_ok", True))

    if same_hash and not force and micro_healthy_enough:
        try:
            (BOT_DIR / DEPLOYED_MICRO_FILE).write_text(target.name + "\n", encoding="utf-8")
        except Exception:
            pass
        set_micro_desired(True, "gupgrade_fastskip_keep_running")
        return {
            "ok": True, "changed": False, "target": target.name, "version": micro_target_version(target),
            "active": "restart 생략", "backup": "없음", "hash": short_hash(active_path),
            "notes": service_notes + [
                "single-path fast-skip: hash 동일 + alive + status 최신 + 수집흔적 있음 + systemd service pid",
                "py_compile/stop/start/restart 경로 완전 우회",
                f"pid 유지: {before.get('used_pid') or '-'} / fresh {before.get('fresh',0)} / cached {before.get('cached',0)} / poll {before.get('poll_count',0)} / age {before.get('age_text','-')}",
            ],
            "warning": "변경 없음 → 실행중인 micro 유지 / 재시작 생략",
            "state": before.get("verdict"), "fresh": before.get("fresh"), "cached": before.get("cached"),
            "orderbook_ok": before.get("orderbook_ok"), "trade_ok": before.get("trade_ok"),
            "restart_reason": "-",
        }

    restart_reasons = []
    if force:
        restart_reasons.append("force")
    if not same_hash:
        restart_reasons.append("hash 변경")
    if not alive:
        restart_reasons.append("프로세스 정지")
    if alive and not status_recent:
        restart_reasons.append(f"status 오래됨 {before.get('age_text','-')}")
    if alive and not err_ok:
        restart_reasons.append(f"수집오류 {err[:80]}")
    if alive and status_recent and err_ok and not has_collection:
        restart_reasons.append("수집흔적 부족")
    if not management_ok:
        restart_reasons.append("direct-fallback → systemd service 전환")
    if not before.get("sync_ok", True):
        restart_reasons.append("active/deployed/latest 불일치")

    okc, comp_out = py_compile(target)
    if not okc:
        return {"ok": False, "changed": False, "target": target.name, "error": "micro py_compile 실패", "notes": [tail(comp_out, 1000)], "restart_reason": " / ".join(restart_reasons) or "검수실패"}

    backup = "없음"
    backup_path = None
    changed = False
    if not same_hash or force:
        if active_path.exists():
            backup_path = BOT_DIR / f"{active_path.name}.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(active_path, backup_path)
            backup = backup_path.name
        tmp = BOT_DIR / f"{active_path.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target, tmp)
        if sha256_file(tmp) != sha256_file(target):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "changed": False, "target": target.name, "error": "micro 임시복사 hash 불일치", "restart_reason": " / ".join(restart_reasons)}
        os.replace(tmp, active_path)
        changed = True
    try:
        (BOT_DIR / DEPLOYED_MICRO_FILE).write_text(target.name + "\n", encoding="utf-8")
    except Exception:
        pass

    notes = service_notes + ["restart path entered only because: " + (" / ".join(restart_reasons) or "unknown"), f"python={MICRO_PYTHON_BIN}"]
    active = "restart 생략"
    state = before
    if restart:
        set_micro_desired(True, "gupgrade_restart_needed")
        active, rn = restart_micro_sidecar(python_bin=MICRO_PYTHON_BIN)
        notes += rn
        state, wn = wait_micro_ready(prev_poll=0 if changed else prev_poll, timeout=60.0, target_version=micro_target_version(target))
        notes += wn
    else:
        state = micro_state_dict()
        notes.append("restart=False → 교체만 하고 재시작 생략")
    def _micro_apply_ok(st: dict) -> bool:
        if not st.get("alive"):
            return False
        fresh = int(st.get("fresh") or 0)
        cached = int(st.get("cached") or 0)
        poll = int(st.get("poll_count") or 0)
        order_ok = int(st.get("orderbook_ok") or 0)
        trade_ok = int(st.get("trade_ok") or 0)
        age = float(st.get("status_age_num") or -1)
        err = str(st.get("last_error") or "-")
        err_ok = err in {"", "-", "None", "none"}
        synced = bool(st.get("sync_ok", False))
        has_collection = fresh > 0 or cached > 0 or poll > 0 or order_ok > 0 or trade_ok > 0
        warmup_ok = synced and 0 <= age <= 45 and err_ok
        return bool(synced and (has_collection or warmup_ok))

    ok = _micro_apply_ok(state)
    rollback_note = ""
    # v2.5.33: active/latest가 맞고 프로세스가 살아 있으면 warm-up으로 인정한다.
    # 캐시 0만으로 즉시 rollback하지 않는다. rollback은 sync가 깨지거나 프로세스가 죽은 확정 실패 때만 수행한다.
    if not ok and changed and backup_path and backup_path.exists():
        hard_fail = (not state.get("alive")) or (not state.get("sync_ok", False) and str(state.get("version") or "") != micro_target_version(target))
        if hard_fail:
            try:
                shutil.copy2(backup_path, active_path)
                rollback_note = f"rollback={backup_path.name}"
                active2, rn = restart_micro_sidecar(python_bin=MICRO_PYTHON_BIN)
                state, wn = wait_micro_ready(timeout=20.0, target_version=micro_target_version(backup_path) if backup_path else "")
                notes += [rollback_note, f"rollback_restart={active2}"] + rn + wn
            except Exception as exc:
                notes.append(f"rollback 실패: {exc.__class__.__name__}: {str(exc)[:160]}")
        else:
            notes.append("rollback 생략: active/latest sync + process alive 상태라 micro warm-up으로 판단")
            ok = True
    final_ok = _micro_apply_ok(state) or ok
    return {"ok": final_ok, "changed": changed, "target": target.name, "version": micro_target_version(target), "active": active, "backup": backup, "hash": short_hash(active_path), "notes": notes, "warning": rollback_note or ("재시작 수행: " + (" / ".join(restart_reasons) or "unknown")), "state": state.get("verdict"), "fresh": state.get("fresh"), "cached": state.get("cached"), "orderbook_ok": state.get("orderbook_ok"), "trade_ok": state.get("trade_ok"), "restart_reason": " / ".join(restart_reasons) or "-"}


def format_micro_apply_result(res: dict) -> str:
    ok = "✅" if res.get("ok") else "❌"
    changed = "교체+재시작" if res.get("changed") else ("변경없음/재시작생략" if str(res.get("active")) == "restart 생략" else "변경없음/필요재시작")
    lines = [f"{ok} micro_sidecar {changed}"]
    for k in ["target", "version", "active", "restart_reason", "state", "fresh", "cached", "orderbook_ok", "trade_ok", "backup", "hash", "error", "warning"]:
        if res.get(k) not in (None, ""):
            lines.append(f"- {k}: {res.get(k)}")
    if res.get("notes"):
        lines.append("- notes:")
        lines += ["  · " + str(x) for x in res.get("notes", [])[:12]]
    return "\n".join(lines)


def gmicro_state_text() -> str:
    d = micro_state_dict()
    proc = ",".join(map(str, d.get("proc_pids") or [])) or "-"
    dup_note = "⚠️ 중복 pid 있음" if len(d.get("proc_pids") or []) > 1 else "✅ pid 단일"
    return "\n".join([
        "🔎 빗썸 호가·체결 직원 /gmicro_state",
        str(d.get("verdict", "?")),
        f"- active_file: {d.get('active_file')} / exists={d.get('active_exists')} / version={d.get('version')} / hash={d.get('hash')}",
        f"- latest: {d.get('target','?')} / {d.get('target_version','?')} / target_hash={d.get('target_hash','?')} / deployed={d.get('deployed_marker','?')} / sync={d.get('sync_ok')}",
        f"- pid_file: {d.get('pid_file') or '-'} / proc: {proc} / used: {d.get('used_pid') or '-'} / alive={d.get('alive')}",
        f"- 관리방식: {d.get('management','-')} / 경고: {d.get('management_warning','-')}",
        f"- cgroup: {d.get('cgroup','-')}",
        f"- python: {d.get('python_path') or '-'} / configured: {d.get('configured_python') or '-'}",
        f"- desired: {'enabled' if d.get('desired_enabled') else 'disabled'} / {dup_note}",
        f"- status_age: {d.get('age_text','-')} / state: {d.get('state','-')} / last_error: {d.get('last_error','-')}",
        f"- targets {d.get('targets',0)} / cache {d.get('cached',0)} / fresh {d.get('fresh',0)} / orderbook_ok {d.get('orderbook_ok',0)} / trade_ok {d.get('trade_ok',0)} / poll {d.get('poll_count',0)}",
        f"- target_source: {d.get('target_source','-')} / reload {d.get('reload_count',0)}",
        f"- files: cache={MICRO_CACHE_FILE} / status={MICRO_STATUS_FILE} / log={MICRO_LOG_FILE}",
        "- 시작: /gmicro_start / 중지: /gmicro_stop / 재시작: /gmicro_restart / 업그레이드: /gmicro_upgrade",
    ])


def gmicro_log(lines: int = 80) -> str:
    txt = read_file_tail(BOT_DIR / MICRO_LOG_FILE, max_bytes=120_000)
    return f"🧾 빗썸 호가·체결 직원 로그 /gmicrolog {lines}\n" + (tail(txt, 2600) if txt.strip() else "- 로그 없음")


def gmicro_start_text() -> str:
    active, notes = start_micro_sidecar()
    return "\n".join(["▶️ 빗썸 호가·체결 직원 시작 /gmicro_start", f"- active: {active}", f"- desired: {'enabled' if micro_desired_enabled() else 'disabled'}", f"- version: {micro_target_version(BOT_DIR / MICRO_ACTIVE_FILE)}"] + [f"- {x}" for x in notes] + ["", "다음 확인: /gmicro_state /quality"])


def gmicro_stop_text() -> str:
    ok, notes = stop_micro_sidecar()
    return "\n".join(["⏹ 빗썸 호가·체결 직원 중지 /gmicro_stop", f"- ok: {ok}", f"- desired: {'enabled' if micro_desired_enabled() else 'disabled'}"] + [f"- {x}" for x in notes])


def gmicro_restart_text() -> str:
    active, notes = restart_micro_sidecar()
    return "\n".join(["🔁 빗썸 호가·체결 직원 재시작 /gmicro_restart", f"- active: {active}", f"- desired: {'enabled' if micro_desired_enabled() else 'disabled'}", f"- version: {micro_target_version(BOT_DIR / MICRO_ACTIVE_FILE)}"] + [f"- {x}" for x in notes] + ["", "다음 확인: /gmicro_state /quality"])


def main_brain_state_dict() -> dict:
    """메인봇 내부 status 파일에서 Telegram polling 상태를 읽는다.

    v2.5.38: systemd active/hash만 정상이고 메인봇 명령이 무반응인 상태를
    /gdeploy에서 바로 보이게 한다. journalctl이 지연돼도 clean_brain_status.json을
    fallback source로 사용한다.
    """
    path = BOT_DIR / "clean_brain_status.json"
    obj = read_json(path)
    if not isinstance(obj, dict) or not obj:
        return {"exists": False, "path": path.name, "state": "missing", "verdict": "❔ 상태파일 없음", "age_text": "-", "version": "?", "error": "-"}
    ts = 0.0
    for k in ("telegram_updated_ts", "last_done_scan_ts", "scan_last_ts", "updated_ts", "started_at"):
        try:
            ts = float(obj.get(k) or 0)
        except Exception:
            ts = 0.0
        if ts > 0:
            break
    age = time.time() - ts if ts > 0 else -1.0
    state = str(obj.get("telegram_state") or "unknown")
    error = str(obj.get("telegram_error") or "-")
    version = str(obj.get("version") or obj.get("brain_version") or "?")
    ok_states = {"polling_started", "running", "polling_started_notify_failed"}
    warn_states = {"initializing", "commands_installed", "stopping"}
    if state in ok_states:
        verdict = "✅ 명령수신 준비"
    elif state in warn_states:
        verdict = "⚠️ 시작 중/확인 필요"
    elif state in {"disabled", "error"}:
        verdict = "❌ Telegram 문제"
    elif state in {"missing", "unknown", ""}:
        verdict = "❔ Telegram 상태 미기록"
    else:
        verdict = "⚠️ Telegram 상태 확인"
    return {
        "exists": True,
        "path": path.name,
        "state": state,
        "error": error,
        "version": version,
        "age": age,
        "age_text": f"{age:.1f}s" if age >= 0 else "-",
        "verdict": verdict,
        "command_count": obj.get("command_count", "-"),
        "scan_stage": obj.get("scan_last_stage", "-"),
        "scan_running": bool(obj.get("scan_running")),
        "note": str(obj.get("telegram_note") or "-")[:160],
    }


def main_file_log_focus(max_chars: int = 1600) -> str:
    """journalctl timeout 때 볼 메인봇 내부 파일 로그 요약."""
    parts = []
    for name in ("clean_brain_error.log", "clean_brain_runtime.log"):
        txt = read_file_tail(BOT_DIR / name, max_bytes=80_000)
        if not txt.strip():
            continue
        lines = []
        for line in txt.splitlines():
            if any(x in line for x in ["Traceback", "NameError", "SyntaxError", "ImportError", "ModuleNotFoundError", "telegram_state", "telegram_not_ready", "main_v305_boot", "telegram_send_api", "polling_started"]):
                lines.append(line)
        if lines:
            parts.append(f"[{name}]\n" + "\n".join(lines[-8:]))
    out = "\n".join(parts).strip()
    return tail(out, max_chars) if out else "없음"




# ─────────────────────────────────────────────────────────────
# v2.5.39 clean worker hub 관리
# ─────────────────────────────────────────────────────────────
def worker_marker_path(name: str) -> Path:
    return BOT_DIR / f".deployed_{name}_worker_target"


def worker_service_unit_path(name: str) -> Path:
    spec = WORKER_SPECS[name]
    return Path("/etc/systemd/system") / f"{spec['service']}.service"


def worker_latest_file(name: str, explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = BOT_DIR / explicit
        return p if p.exists() else p
    spec = WORKER_SPECS[name]
    prefix = str(spec["prefix"])
    files = []
    for p in BOT_DIR.glob(f"{prefix}_v*.py"):
        nums = tuple(int(x) for x in re.findall(r"\d+", p.name))
        files.append((nums, p.stat().st_mtime, p))
    if not files:
        # 최신 파일명이 없더라도 active 파일이 있으면 상태 확인은 가능하게 한다.
        active = BOT_DIR / str(spec["active"])
        return active if active.exists() else None
    return sorted(files, key=lambda x: (x[0], x[1]))[-1][2]


def worker_version(path: Optional[Path]) -> str:
    if not path or not path.exists():
        return "?"
    val = extract_assignment(path, "VERSION")
    if val != "?":
        return val
    return path.name


def worker_active_path(name: str) -> Path:
    return BOT_DIR / str(WORKER_SPECS[name]["active"])


def worker_status_path(name: str) -> Path:
    return BOT_DIR / str(WORKER_SPECS[name]["status"])


def worker_status_age(name: str) -> tuple[float, str]:
    p = worker_status_path(name)
    if not p.exists():
        return 999999.0, "없음"
    st = read_json(p)
    ts = st.get("updated_ts") or st.get("ts")
    try:
        age = max(0.0, time.time() - float(ts)) if ts else max(0.0, time.time() - p.stat().st_mtime)
    except Exception:
        age = max(0.0, time.time() - p.stat().st_mtime)
    return age, fmt_sec(age)


def find_worker_pids(name: str) -> list[int]:
    spec = WORKER_SPECS[name]
    needles = {str(spec["active"])}
    latest = worker_latest_file(name)
    if latest:
        needles.add(latest.name)
    out = []
    proc = Path("/proc")
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            cmdline = (p / "cmdline").read_bytes().decode("utf-8", errors="ignore").replace("\x00", " ")
            low = cmdline.lower()
            if "python" not in low:
                continue
            if any(n.lower() in low for n in needles):
                out.append(int(p.name))
        except Exception:
            continue
    return sorted(set(out))


def cleanup_direct_worker_pids(name: str) -> list[str]:
    """systemd 서비스 시작 전 구 direct/subprocess worker를 끊는다.

    v319 메인봇이 worker를 subprocess로 켤 수 있으므로, guard가 systemd 본선으로 전환할 때
    동일 worker가 두 개 도는 문제를 막는다. 단, systemd MainPID는 죽이지 않는다.
    """
    spec = WORKER_SPECS[name]
    svc = str(spec["service"])
    rc, mainpid_txt = run(["systemctl", "show", svc, "-p", "MainPID", "--value"], timeout=5)
    mainpid = 0
    try:
        mainpid = int((mainpid_txt or "0").strip() or "0")
    except Exception:
        mainpid = 0
    notes = []
    for pid in find_worker_pids(name):
        if mainpid and pid == mainpid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            notes.append(f"direct pid {pid} TERM")
        except Exception as e:
            notes.append(f"direct pid {pid} TERM 실패 {type(e).__name__}")
    time.sleep(0.2)
    for pid in find_worker_pids(name):
        if mainpid and pid == mainpid:
            continue
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                notes.append(f"direct pid {pid} KILL")
            except Exception as e:
                notes.append(f"direct pid {pid} KILL 실패 {type(e).__name__}")
    return notes


def ensure_worker_service_installed(name: str) -> tuple[bool, list[str]]:
    spec = WORKER_SPECS[name]
    active = worker_active_path(name)
    svc = str(spec["service"])
    unit_path = worker_service_unit_path(name)
    log_path = BOT_DIR / str(spec.get("log") or f"clean_{name}_worker_service.log")
    err_path = BOT_DIR / f"clean_{name}_worker_service.err"
    unit = f"""[Unit]
Description=TradingBot {spec['label']} worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={BOT_DIR}
EnvironmentFile=-{BOT_DIR / 'guard.env'}
Environment=TRADING_BOT_DIR={BOT_DIR}
ExecStart={WORKER_PYTHON_BIN} {active}
Restart=always
RestartSec=3
StandardOutput=append:{log_path}
StandardError=append:{err_path}

[Install]
WantedBy=multi-user.target
"""
    notes = []
    try:
        old = unit_path.read_text(encoding="utf-8", errors="ignore") if unit_path.exists() else ""
        if old != unit:
            unit_path.write_text(unit, encoding="utf-8")
            notes.append(f"unit write {unit_path.name}")
            rc, out = run(["systemctl", "daemon-reload"], timeout=20)
            notes.append(f"daemon-reload rc={rc}")
        rc, out = run(["systemctl", "enable", svc], timeout=20)
        if rc not in {0, 1}:  # 이미 enabled일 때도 배포판에 따라 0/1 혼재 가능
            notes.append(f"enable rc={rc} {tail(out,180)}")
        return True, notes
    except Exception as e:
        return False, [f"service install failed {type(e).__name__}: {e}"] + notes


def worker_state_dict(name: str) -> dict:
    spec = WORKER_SPECS[name]
    active = worker_active_path(name)
    latest = worker_latest_file(name)
    st = read_json(worker_status_path(name))
    age, age_text = worker_status_age(name)
    rc_active, active_txt = run(["systemctl", "is-active", str(spec["service"])], timeout=5)
    service_active = (active_txt or "").strip()
    rc_enabled, enabled_txt = run(["systemctl", "is-enabled", str(spec["service"])], timeout=5)
    rc_pid, mainpid_txt = run(["systemctl", "show", str(spec["service"]), "-p", "MainPID", "--value"], timeout=5)
    try:
        mainpid = int((mainpid_txt or "0").strip() or "0")
    except Exception:
        mainpid = 0
    state = str(st.get("state") or "missing") if st else "missing"
    active_ver = worker_version(active)
    latest_ver = worker_version(latest)
    fresh_limit = float(spec.get("fresh_limit") or 45)
    alive = bool(mainpid and pid_alive(mainpid))
    sync_ok = bool(latest and active.exists() and short_hash(latest) == short_hash(active))
    ok = bool(service_active == "active" and alive and state == "running" and age <= fresh_limit and sync_ok)
    verdict = "✅ 정상" if ok else "⚠️ 확인 필요"
    if service_active not in {"active", "activating"}:
        verdict = "❌ 꺼짐"
    elif age > fresh_limit:
        verdict = "⚠️ stale"
    elif not sync_ok:
        verdict = "⚠️ 파일 불일치"
    metrics = []
    for k in ["row_count", "pool_count", "evaluated", "s_count", "target_count", "request_count", "near_s_count", "fresh_recovered", "need_ws", "need_micro", "fresh_ws", "fresh_micro", "closed_recent", "big_loss", "good_win", "last_sec", "scanner_age"]:
        if k in st:
            metrics.append(f"{k}={st.get(k)}")
    return {
        "name": name,
        "label": spec["label"],
        "service": spec["service"],
        "active_file": active.name,
        "active_exists": active.exists(),
        "active_version": active_ver,
        "active_hash": short_hash(active),
        "latest": latest.name if latest else "?",
        "latest_version": latest_ver,
        "latest_hash": short_hash(latest) if latest else "?",
        "deployed": read_file(worker_marker_path(name)) or "?",
        "sync_ok": sync_ok,
        "service_active": service_active or "?",
        "enabled": (enabled_txt or "?").strip(),
        "pid": mainpid or (st.get("pid") if isinstance(st, dict) else 0),
        "alive": alive,
        "state": state,
        "age": age,
        "age_text": age_text,
        "status_version": st.get("version", "?") if isinstance(st, dict) else "?",
        "metrics": " / ".join(metrics) if metrics else "-",
        "ok": ok,
        "verdict": verdict,
        "last_error": str(st.get("error") or "-")[:160] if isinstance(st, dict) else "-",
        "raw": st if isinstance(st, dict) else {},
    }


def format_worker_state_line(w: dict) -> str:
    return (
        f"- {w['label']}({w['name']}): {w['verdict']} / svc {w['service_active']} / pid {w.get('pid') or '-'} / age {w['age_text']}\n"
        f"  · active {w['active_file']} {w['active_version']} / latest {w['latest']} {w['latest_version']} / sync {w['sync_ok']}\n"
        f"  · metrics {w['metrics']} / err {w['last_error']}"
    )


def _gworker_val(st: dict, *keys: str):
    # v2.5.52: 일부 상태 dict는 raw가 비어 있고 metrics 문자열만 남아 한눈요약이 '-'로 보였다.
    # raw -> 직접필드 -> metrics 문자열 순서로 읽어 숫자를 복원한다.
    raw = st.get("raw") if isinstance(st.get("raw"), dict) else {}
    for k in keys:
        v = raw.get(k)
        if v not in (None, "", "-"):
            return v
    for k in keys:
        v = st.get(k)
        if v not in (None, "", "-"):
            return v
    metrics = str(st.get("metrics") or "")
    if metrics:
        import re
        for k in keys:
            m = re.search(rf"(?:^|/)\s*{re.escape(k)}=([^/]+)", metrics)
            if m:
                val = m.group(1).strip()
                if val and val != "-":
                    return val
    return "-"


def gworker_state_text(name: Optional[str] = None) -> str:
    names = [name] if name else list(WORKER_SPECS.keys())
    states = {n: worker_state_dict(n) for n in names if n in WORKER_SPECS}
    ok_all = all(s.get("ok") for s in states.values()) if states else False
    title = "🧩 worker 상태 /gworker_state" if not name else f"🧩 {WORKER_SPECS[name]['label']} worker 상태"
    if name:
        return "\n".join([title, "✅ 정상" if ok_all else "⚠️ 확인 필요", "", *[format_worker_state_line(s) for s in states.values()]])
    def st(n): return states.get(n, {})
    lines = [title, "✅ 정상" if ok_all else "⚠️ 확인 필요", "", "[한눈요약]"]
    lines.append(f"- 스캐너: 전체 {_gworker_val(st('scanner'),'row_count','pool_count')}개 / {_gworker_val(st('scanner'),'last_sec')}초")
    lines.append(f"- 특징: {_gworker_val(st('feature'),'row_count')}개 표준값 / {_gworker_val(st('feature'),'last_sec')}초")
    lines.append(f"- 캔들: {_gworker_val(st('candle'),'row_count')}개 보강 / target {_gworker_val(st('candle'),'target_count')} / {_gworker_val(st('candle'),'last_sec')}초")
    lines.append(f"- 호가요약: 전체 {_gworker_val(st('orderflow'),'row_count')}개 / WS {_gworker_val(st('orderflow'),'fresh_ws')} / micro {_gworker_val(st('orderflow'),'fresh_micro')} / {_gworker_val(st('orderflow'),'last_sec')}초")
    lines.append(f"- 정보배차: target {_gworker_val(st('target_router'),'target_count')}개 / S근접 {_gworker_val(st('target_router'),'near_s_count')} / micro필요 {_gworker_val(st('target_router'),'need_micro')} / {_gworker_val(st('target_router'),'last_sec')}초")
    lines.append(f"- 전략판정: 평가 {_gworker_val(st('strategy'),'evaluated')}개 / S {_gworker_val(st('strategy'),'s_count')} / target {_gworker_val(st('strategy'),'target_count')} / {_gworker_val(st('strategy'),'last_sec')}초")
    lines.append(f"- 위험/복기: risk {_gworker_val(st('risk'),'last_sec')}초 / review 최근완료 {_gworker_val(st('review'),'closed_recent')}개, 큰손실 {_gworker_val(st('review'),'big_loss')}, 좋은승리 {_gworker_val(st('review'),'good_win')}")
    lines += ["", "[원본 상세]"]
    lines += [format_worker_state_line(s) for s in states.values()]
    lines += ["", "명령", "- /gscanner_state /gcandle_state /gmarket_state /gfeature_state", "- /gorderflow_state /grisk_state /gstrategy_state /gtarget_state /greview_state"]
    return "\n".join(lines)


def wait_worker_ready(name: str, target_version: str = "", timeout: float = 9.0) -> tuple[dict, list[str]]:
    spec = WORKER_SPECS.get(name, {})
    timeout = float(spec.get("ready_timeout") or timeout)
    t0 = time.time()
    notes = []
    last = {}
    while time.time() - t0 < timeout:
        last = worker_state_dict(name)
        if last.get("service_active") == "active" and last.get("state") in {"running", "ready"} and last.get("age", 9999) < max(20, timeout):
            if not target_version or str(last.get("status_version")) == str(target_version):
                notes.append(f"ready {name} age={last.get('age_text')} version={last.get('status_version')}")
                return last, notes
        time.sleep(0.6)
    notes.append(f"ready_timeout {name} last={last.get('state','?')} age={last.get('age_text','?')} ver={last.get('status_version','?')}")
    return last, notes


def apply_worker_latest(name: str, force: bool = False, explicit: Optional[str] = None, restart: bool = True) -> dict:
    t0 = perf_now()
    if name not in WORKER_SPECS:
        return {"ok": False, "name": name, "error": "unknown worker"}
    spec = WORKER_SPECS[name]
    target = worker_latest_file(name, explicit=explicit)
    if not target or not target.exists():
        return {"ok": False, "name": name, "error": "target missing", "target": str(target or "?")}
    active = worker_active_path(name)
    target_ver = worker_version(target)
    notes = []
    changed = force or (not active.exists()) or (short_hash(active) != short_hash(target))
    before = worker_state_dict(name)
    # v2.5.48: 변경 없는 worker는 py_compile/ready wait까지 생략한다.
    # 변경 파일은 아래에서 py_compile 후 교체한다.
    if not changed and not force and before.get("service_active") == "active" and before.get("alive") and before.get("sync_ok"):
        return {
            "ok": True,
            "name": name,
            "label": spec["label"],
            "target": target.name,
            "target_version": target_ver,
            "active": active.name,
            "changed": False,
            "restarted": False,
            "hash": f"target {short_hash(target)} / active {short_hash(active)}",
            "service": spec["service"],
            "state": before,
            "notes": ["fast-skip: hash 동일 + service active + sync → py_compile/복사/재시작/ready wait 생략"],
            "total_sec": perf_now() - t0,
        }
    rc, out = run([WORKER_PYTHON_BIN, "-m", "py_compile", str(target)], timeout=18)
    if rc != 0:
        return {"ok": False, "name": name, "target": target.name, "error": "py_compile failed", "compile": tail(out, 700)}
    if changed:
        backup = None
        if active.exists():
            backup = BOT_DIR / f"{active.name}.backup_guard_worker_apply_{time.strftime('%Y%m%d_%H%M%S')}"
            try:
                shutil.copy2(active, backup)
                notes.append(f"backup {backup.name}")
            except Exception as e:
                notes.append(f"backup failed {type(e).__name__}: {e}")
        shutil.copy2(target, active)
        worker_marker_path(name).write_text(target.name + "\n", encoding="utf-8")
        notes.append(f"copy {target.name} -> {active.name}")
    ok_unit, unit_notes = ensure_worker_service_installed(name)
    notes += unit_notes
    if not ok_unit:
        return {"ok": False, "name": name, "target": target.name, "changed": changed, "error": "service install failed", "notes": notes}
    need_restart = bool(restart and (changed or force or before.get("service_active") != "active" or not before.get("alive") or not before.get("sync_ok")))
    notes += cleanup_direct_worker_pids(name)
    if need_restart:
        rc, out = run(["systemctl", "restart", str(spec["service"])], timeout=35)
        notes.append(f"restart rc={rc} {tail(out,160)}".strip())
    else:
        rc, out = run(["systemctl", "start", str(spec["service"])], timeout=20)
        if rc not in {0, 1}:
            notes.append(f"start-check rc={rc} {tail(out,160)}")
    after, wait_notes = wait_worker_ready(name, target_version=target_ver, timeout=9)
    notes += wait_notes
    ok = bool(after.get("service_active") == "active" and after.get("state") == "running" and after.get("sync_ok"))
    return {
        "ok": ok,
        "name": name,
        "label": spec["label"],
        "target": target.name,
        "target_version": target_ver,
        "active": active.name,
        "changed": changed,
        "restarted": need_restart,
        "hash": f"target {short_hash(target)} / active {short_hash(active)}",
        "service": spec["service"],
        "state": after,
        "notes": notes[:14],
        "total_sec": perf_now() - t0,
    }


def format_worker_apply_result(res: dict) -> str:
    ok = "✅" if res.get("ok") else "❌"
    mode = "교체+재시작" if res.get("changed") and res.get("restarted") else ("교체" if res.get("changed") else ("재시작" if res.get("restarted") else "변경없음/유지"))
    lines = [f"{ok} {res.get('label', res.get('name','worker'))} worker {mode}"]
    for k in ["target", "target_version", "active", "hash", "service", "error"]:
        if res.get(k) not in (None, ""):
            lines.append(f"- {k}: {res.get(k)}")
    st = res.get("state") or {}
    if st:
        lines.append(f"- state: {st.get('verdict','-')} / svc {st.get('service_active','-')} / age {st.get('age_text','-')} / metrics {st.get('metrics','-')}")
    if res.get("notes"):
        lines.append("- notes:")
        lines.extend(f"  · {str(x)[:220]}" for x in res.get("notes", [])[:10])
    lines.append(f"- total: {fmt_sec(res.get('total_sec',0))}")
    return "\n".join(lines)


def apply_workers_latest(force: bool = False, explicit: Optional[str] = None, restart: bool = True) -> dict:
    t0 = perf_now()
    results = {}
    for name in WORKER_SPECS:
        results[name] = apply_worker_latest(name, force=force, explicit=None, restart=restart)
    ok = all(bool(r.get("ok")) for r in results.values())
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    status["workers"] = results
    status["workers_upgrade_time"] = now()
    status["workers_upgrade_total_sec"] = perf_now() - t0
    write_json(BOT_DIR / UPGRADE_STATUS_FILE, status)
    return {"ok": ok, "results": results, "total_sec": perf_now() - t0}


def gworker_upgrade_text(force: bool = False) -> str:
    res = apply_workers_latest(force=force, restart=True)
    title = "🧩 ✅ worker 최신화 완료 /gworker_upgrade" if res.get("ok") else "🧩 ❔ worker 최신화 확인 필요 /gworker_upgrade"
    parts = [title, f"- guard: {VERSION}", f"- 대상: scanner → candle → market → feature → orderflow → risk → strategy → target_router → review", f"- total: {fmt_sec(res.get('total_sec',0))}"]
    for name in WORKER_SPECS:
        parts.append("\n" + format_worker_apply_result(res["results"].get(name, {})))
    parts.append("\n다음 확인: /gworker_state 또는 메인봇 /health")
    return "\n".join(parts)


def gworker_restart_text(name: Optional[str] = None) -> str:
    names = [name] if name else list(WORKER_SPECS.keys())
    lines = ["🔁 worker 재시작 /gworker_restart"]
    for n in names:
        if n not in WORKER_SPECS:
            continue
        notes = cleanup_direct_worker_pids(n)
        rc, out = run(["systemctl", "restart", str(WORKER_SPECS[n]["service"])], timeout=35)
        st, wn = wait_worker_ready(n, timeout=15)
        icon = "✅" if st.get("service_active") == "active" and st.get("state") == "running" else "⚠️"
        lines.append(f"- {icon} {WORKER_SPECS[n]['label']} rc={rc} / age {st.get('age_text','-')} / state {st.get('state','-')}")
        for x in (notes + wn)[:4]:
            lines.append(f"  · {x}")
    lines.append("\n다음 확인: /gworker_state")
    return "\n".join(lines)


def gworker_log_text(name: str, n: int = 80) -> str:
    if name not in WORKER_SPECS:
        return "알 수 없는 worker야."
    spec = WORKER_SPECS[name]
    log_path = BOT_DIR / str(spec.get("log") or f"clean_{name}_worker_service.log")
    err_path = BOT_DIR / f"clean_{name}_worker_service.err"
    rc, out = run(["journalctl", "-u", str(spec["service"]), "-n", str(max(20, min(300, n))), "--no-pager"], timeout=4)
    if rc == 124 or not out:
        out = read_file_tail(log_path, max_bytes=40_000) + "\n" + read_file_tail(err_path, max_bytes=20_000)
    return f"🧾 {spec['label']} worker 로그\n" + (tail(out, 3500) if out.strip() else "없음")

def deploy_status() -> str:
    bot_version, target, deployed, latest = get_bot_versions()
    pv = get_paper_versions()
    wsv = ws_sidecar_state_dict()
    msv = micro_state_dict()
    workers = {name: worker_state_dict(name) for name in WORKER_SPECS}
    active = is_main_service_active()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    latest_path = BOT_DIR / latest if latest != "?" else None
    latest_ver = extract_bot_version(latest_path) if latest_path else "?"
    runtime_ver = extract_recent_runtime_version(lines=260, since=service_start_since())
    brain = main_brain_state_dict()
    bot_hash = short_hash(BOT_DIR / "bot.py")
    latest_hash = short_hash(latest_path) if latest_path else "?"

    verdict_bits = []
    if active != "active":
        verdict_bits.append("메인봇 inactive")
    if latest_path and not same_version_label(bot_version, latest_ver):
        verdict_bits.append("최신파일 내부버전과 bot.py 버전 다름")
    if latest != "?" and deployed != latest:
        verdict_bits.append(".deployed_target이 최신파일과 다름")
    if pv["latest"] != "?" and pv["deployed"] not in {pv["latest"], "?"}:
        verdict_bits.append(".deployed_paper_target이 최신 paper와 다름")
    if pv["latest_version"] != "?" and pv["active_version"] != "?" and pv["latest_version"] != pv["active_version"]:
        verdict_bits.append("paper_bot 활성버전과 최신버전 다름")
    if pv.get("service") == "no_service":
        verdict_bits.append("paper_bot systemd 서비스 미등록(runtime 금지)")
    if runtime_ver not in {"시작로그 대기", "시작로그 확인불가"} and latest_path and not same_version_label(runtime_ver, bot_version):
        verdict_bits.append("bot.py 파일버전과 실제 실행로그 버전 불일치")
    if active == "active":
        bstate = str(brain.get("state") or "")
        if bstate in {"disabled", "error"}:
            verdict_bits.append("메인봇 Telegram 수신 문제: " + str(brain.get("error") or bstate)[:120])
        elif bstate not in {"polling_started", "running", "polling_started_notify_failed"}:
            verdict_bits.append("메인봇 Telegram 상태 확인 필요: " + (bstate or "미기록"))
    if not wsv.get("ok"):
        verdict_bits.append("웹소켓직원 확인 필요: " + str(wsv.get("verdict", "?")))
    if str(wsv.get("management_warning") or "-") != "-":
        verdict_bits.append("웹소켓직원 구조 확인: " + str(wsv.get("management_warning")))
    if msv.get("active_exists") and not msv.get("ok"):
        verdict_bits.append("호가체결직원 확인 필요: " + str(msv.get("verdict", "?")))
    if str(msv.get("management_warning") or "-") != "-":
        verdict_bits.append("호가체결직원 구조 확인: " + str(msv.get("management_warning")))
    for _wn, _ws in workers.items():
        if not _ws.get("ok"):
            verdict_bits.append(f"worker 확인 필요: {_ws.get('label', _wn)} {_ws.get('verdict','?')}")

    conclusion = "✅ 정상" if not verdict_bits else "❔ 확인 필요"
    last = "없음"
    if status:
        last = f"{status.get('time','?')} / {'성공' if status.get('ok') else '실패/확인필요'} / {status.get('target') or status.get('stage') or '?'}"
        if status.get("paper"):
            last += f" / paper {status.get('paper',{}).get('target','?')}"
    recent = recent_journal(lines=80, since=service_start_since())
    focus = []
    for line in (recent or "").splitlines():
        if any(x in line for x in ["Traceback", "NameError", "SyntaxError", "ImportError", "ModuleNotFoundError", "Failed", "failed", "exit-code"]):
            focus.append(line)
    focus_file = main_file_log_focus()
    if focus:
        focus_text = "\n".join(focus[-5:])
        if focus_file != "없음":
            focus_text += "\n" + focus_file
    else:
        focus_text = focus_file
    verdict_text = "\n".join(f"- ❌ {x}" for x in verdict_bits) if verdict_bits else "- 이상 없음"

    return (
        f"🧭 가드 배포상태 /gdeploy\n"
        f"{conclusion}\n\n"
        f"📊 메인봇\n"
        f"- active: {active}\n"
        f"- 최신 코드파일: {latest}\n"
        f"- 최신 내부 BOT_VERSION: {latest_ver}\n"
        f"- bot.py 내부 BOT_VERSION: {bot_version}\n"
        f"- 실제 실행 로그 버전: {runtime_ver}\n"
        f"- 내부 상태파일 버전: {brain.get('version','?')} / age {brain.get('age_text','-')}\n"
        f"- Telegram 상태: {brain.get('verdict','-')} / {brain.get('state','-')} / err {brain.get('error','-')}\n"
        f"- 명령등록: {brain.get('command_count','-')}개 / scan {brain.get('scan_stage','-')} / running {brain.get('scan_running','-')}\n"
        f"- DEPLOY_TARGET 참고: {target}\n"
        f"- .deployed_target: {deployed}\n"
        f"- latest hash: {latest_hash}\n"
        f"- bot.py hash: {bot_hash}\n\n"
        f"🧪 페이퍼봇\n"
        f"- service: {pv['service']}\n"
        f"- active file: {pv['active_file']}\n"
        f"- active VERSION: {pv['active_version']}\n"
        f"- target: {pv['target']}\n"
        f"- latest: {pv['latest']} / {pv['latest_version']}\n"
        f"- .deployed_paper_target: {pv['deployed']}\n"
        f"- hash: active {pv['active_hash']} / latest {pv['latest_hash']}\n\n"
        f"🛰 웹소켓직원\n"
        f"- 상태: {wsv.get('verdict')}\n"
        f"- active file: {wsv.get('active_file')} / version: {wsv.get('version')} / hash: {wsv.get('hash')}\n"
        f"- latest: {wsv.get('target','?')} / {wsv.get('target_version','?')} / target_hash {wsv.get('target_hash','?')} / sync {wsv.get('sync_ok')}\n"
        f"- 관리방식: {wsv.get('management','-')} / 경고 {wsv.get('management_warning','-')}\n"
        f"- pid: {wsv.get('used_pid') or '-'} / alive: {wsv.get('alive')} / status_age: {wsv.get('age_text','-')}\n"
        f"- python: {wsv.get('python_path') or '-'} / configured: {wsv.get('configured_python') or '-'}\n"
        f"- 수신: raw {wsv.get('raw_total',0)} / parse {wsv.get('parse_ok',0)} / price {wsv.get('price_ok',0)} / amount {wsv.get('amount_ok',0)} / match {wsv.get('match_ok',0)} / format {wsv.get('last_format','-')}\n"
        f"- cache {wsv.get('cached',0)} / fresh {wsv.get('fresh',0)} / last_error {wsv.get('last_error','-')}\n"
        f"- deployed: {read_file(BOT_DIR / DEPLOYED_WS_FILE) or '?'}\n\n"
        f"🔎 호가·체결직원\n"
        f"- 상태: {msv.get('verdict')}\n"
        f"- active file: {msv.get('active_file')} / version: {msv.get('version')} / hash: {msv.get('hash')}\n"
        f"- latest: {msv.get('target','?')} / {msv.get('target_version','?')} / target_hash {msv.get('target_hash','?')} / sync {msv.get('sync_ok')}\n"
        f"- 관리방식: {msv.get('management','-')} / 경고 {msv.get('management_warning','-')}\n"
        f"- pid: {msv.get('used_pid') or '-'} / alive: {msv.get('alive')} / status_age: {msv.get('age_text','-')}\n"
        f"- 수집: targets {msv.get('targets',0)} / cache {msv.get('cached',0)} / fresh {msv.get('fresh',0)} / orderbook_ok {msv.get('orderbook_ok',0)} / trade_ok {msv.get('trade_ok',0)}\n"
        f"- deployed: {read_file(BOT_DIR / DEPLOYED_MICRO_FILE) or '?'}\n\n"
        f"🧩 리뉴얼 worker\n"
        + "\n".join(format_worker_state_line(workers[n]) for n in WORKER_SPECS)
        + "\n\n"
        f"📌 판정\n{verdict_text}\n\n"
        f"📌 최근 업그레이드\n- {last}\n\n"
        f"🧾 최근 오류 의심\n{focus_text}\n\n"
        f"명령\n"
        f"- /gupgrade : 전체 최신화. 변경분만 교체/재시작\n"
        f"- /gpaper_restart : 페이퍼봇 재시작\n"
        f"- /gws_upgrade : 웹소켓직원만 최신 적용\n"
        f"- /gmicro_upgrade : 빗썸 호가·체결직원만 최신 적용\n"
        f"- /gguard_upgrade : 가드봇 자체 최신 적용\n"
        f"- /glog : 메인봇 최근 오류로그"
    )


# v2.5.26: 외부직원 통합상태. /gupgrade는 가드봇→메인봇→페이퍼봇→WS→micro 순서로 변경분만 적용.
def gexternal_state_text() -> str:
    w = ws_sidecar_state_dict()
    m = micro_state_dict()
    w_ok = bool(w.get("ok") or (w.get("alive") and int(w.get("fresh") or 0) > 0 and (str(w.get("last_error") or "-") in {"", "-", "None", "none"} or _is_reconnect_notice(w.get("last_error")))))
    m_ok = bool(m.get("ok"))
    overall = "✅ 정상" if w_ok and m_ok else ("⚠️ 확인 필요" if (w_ok or m_ok) else "❌ 외부직원 문제")
    notes = []
    if w.get("stop_suspect"):
        notes.append("- WS 마지막 종료가 signal/normal stop 계열: 가드 재시작 연동 종료 의심")
    if m.get("stop_suspect"):
        notes.append("- 호가·체결 마지막 종료가 signal/normal stop 계열: 가드 재시작 연동 종료 의심")
    if str(w.get("management_warning") or "-") != "-":
        notes.append("- WS 구조경고: " + str(w.get("management_warning")))
    if str(m.get("management_warning") or "-") != "-":
        notes.append("- 호가·체결 구조경고: " + str(m.get("management_warning")))
    if not w.get("alive"):
        notes.append("- WS 꺼짐: /gws_start 또는 /gws_restart")
    elif not w_ok:
        notes.append("- WS 확인 필요: /gws_state")
    if m.get("desired_enabled") and not m.get("alive"):
        notes.append("- 호가·체결은 켜져 있어야 하는데 꺼짐: /gmicro_start")
    elif not m_ok:
        notes.append("- 호가·체결 확인 필요: /gmicro_state")
    if any("구조경고" in str(x) for x in notes):
        overall = "⚠️ 구조 확인 필요"
    if not notes:
        notes.append("- 이상 없음")
    return "\n".join([
        "🛰 외부직원 통합상태 /gexternal_state",
        overall,
        "",
        "[0/2] 서버자원",
        *resource_status_lines(),
        "",
        "[1/2] 웹소켓 직원",
        f"{'✅' if w_ok else '⚠️'} 상태: {w.get('verdict','-')}",
        f"- pid {w.get('used_pid') or '-'} / alive {w.get('alive')} / age {w.get('age_text','-')}",
        f"- 관리방식 {w.get('management','-')} / 구조경고 {w.get('management_warning','-')}",
        f"- 수신 raw {w.get('raw_total',0)} / match {w.get('match_ok',0)} / cache {w.get('cached',0)} / fresh {w.get('fresh',0)}",
        f"- 상태메모 {('재구독 중' if _is_reconnect_notice(w.get('last_error','-')) else w.get('last_error','-'))}",
        "",
        "[2/2] 호가·체결 직원",
        f"{'✅' if m_ok else '⚠️'} 상태: {m.get('verdict','-')}",
        f"- pid {m.get('used_pid') or '-'} / alive {m.get('alive')} / age {m.get('age_text','-')}",
        f"- 관리방식 {m.get('management','-')} / 구조경고 {m.get('management_warning','-')}",
        f"- desired {'enabled' if m.get('desired_enabled') else 'disabled'}",
        f"- 수집 targets {m.get('targets',0)} / cache {m.get('cached',0)} / fresh {m.get('fresh',0)}",
        f"- 호가OK {m.get('orderbook_ok',0)} / 체결OK {m.get('trade_ok',0)} / 오류 {m.get('last_error','-')}",
        "",
        "판독",
        *notes,
        "",
        "",
        "[worker]",
        *[format_worker_state_line(worker_state_dict(n)) for n in WORKER_SPECS],
        "",
        "명령",
        "- /gws_state: 웹소켓 상세",
        "- /gmicro_state: 호가·체결 상세",
        "- /gworker_state: 리뉴얼 worker 상세",
    ])


def service_status() -> str:
    active = is_main_service_active()
    bot_version, target, deployed, latest = get_bot_versions()
    pv = get_paper_versions()
    wsv = ws_sidecar_state_dict()
    msv = micro_state_dict()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    last_ok = "없음"
    if status:
        last_ok = f"{status.get('time','?')} / {'성공' if status.get('ok') else '실패/확인필요'} / {status.get('target') or status.get('stage') or '?'}"
    issue_bits = []
    if active != "active": issue_bits.append("메인봇 inactive")
    if not wsv.get("ok"): issue_bits.append("WS 확인 필요")
    if str(wsv.get("management_warning") or "-") != "-": issue_bits.append("WS 구조경고")
    if not msv.get("ok"): issue_bits.append("micro 확인 필요")
    if str(msv.get("management_warning") or "-") != "-": issue_bits.append("micro 구조경고")
    head = "✅ 결론" if not issue_bits else "❔ 결론: 확인 필요 - " + ", ".join(issue_bits[:4])
    return (
        f"🛡 가드봇 /guard\n"
        f"{head}\n"
        f"- 메인봇: {active} / {bot_version}\n"
        f"- 페이퍼봇: {pv['service']} / {pv['active_version']}\n"
        f"- 웹소켓직원: {wsv.get('verdict')} / active {wsv.get('version')} / status {wsv.get('status_version','-')}\n"
        f"- 호가·체결직원: {msv.get('verdict')} / {msv.get('version')}\n"
        f"- 업그레이드: /gupgrade 전체 또는 /gupgrade_menu에서 개별 적용\n\n"
        f"📊 상태\n"
        f"- guard: {VERSION}\n"
        f"- main service: {MAIN_SERVICE}\n"
        f"- paper service: {PAPER_SERVICE}\n"
        f"- 최신 수익형 파일: {latest}\n"
        f"- 최신 paper 파일: {pv['latest']}\n"
        f"- 웹소켓 pid: {wsv.get('used_pid') or '-'} / alive: {wsv.get('alive')}\n"
        f"- 호가·체결 pid: {msv.get('used_pid') or '-'} / alive: {msv.get('alive')}\n"
        f"- 최근 적용: {last_ok}\n\n"
        f"🧭 자주 쓰는 명령\n"
        f"- /gdeploy 배포상태\n"
        f"- /gupgrade 전체 최신화(변경분만 교체/재시작)\n"
        f"- /gpaper_restart 페이퍼봇 재시작\n"
        f"- /gpaper_reset 페이퍼 실행연결 재설치\n"
        f"- /gpaper_service 페이퍼봇 서비스화 안내\n"
        f"- /gws_state 웹소켓직원 상태\n"
        f"- /gws_restart 웹소켓직원 재시작\n"
        f"- /gmicro_state 호가·체결직원 상태\n"
        f"- /gmicro_upgrade 호가·체결직원 최신 적용\n"
        f"- /gguard_upgrade 가드봇 자체 업그레이드\n"
        f"- /glog 최근 오류\n"
        f"- /gdeep_audit 작업자 지연·디스크 증가 원인 통합감사\n"
        f"- /gcleanup 디스크 순환정리"
    )

def glog(lines: int = 80) -> str:
    lines = max(20, min(160, int(lines or 80)))
    started = perf_now()
    out = recent_journal(lines=lines, timeout_sec=4)
    took = perf_now() - started
    focus = []
    for line in (out or "").splitlines():
        if any(x in line for x in ["Traceback", "NameError", "SyntaxError", "ImportError", "Exception", "ERROR", "Failed", "failure", "timeout"]):
            focus.append(line)
    if "journalctl timeout" in (out or ""):
        head = "⚠️ journalctl 지연 - 가드봇 보호를 위해 status fallback만 표시"
    elif focus:
        head = "❌ 오류 의심 줄\n" + "\n".join(focus[-16:])
    else:
        head = "✅ 최근 로그에서 뚜렷한 Python 오류 줄 없음"
    return f"🧾 최근 로그 /glog {lines}\n- 방식: 짧은 timeout journal tail / {took:.1f}s\n\n{head}\n\n[tail]\n{tail(out, 2200)}"


def unlock_deploy() -> str:
    removed = []
    for p in [Path("/tmp/tradingbot_auto_deploy.lock"), BOT_DIR / "tradingbot_auto_deploy.lock"]:
        try:
            if p.exists():
                p.unlink()
                removed.append(str(p))
        except Exception as e:
            removed.append(f"{p}: {type(e).__name__}: {e}")
    return "🔓 lock 해제 /gunlock\n- removed: " + (", ".join(removed) if removed else "없음")



# =============================================================================
# guard v2.5.69 / v927: 작업자 지연 + 디스크 증가 통합 원인 감사
# - 읽기 전용. OPEN/CLOSED/trade_log 및 매매 기준을 수정하지 않는다.
# - 한 번의 명령에서 현재 용량/프로세스/worker cycle을 수집하고 짧은 구간 파일 증가량을 재측정한다.
# - journal, 삭제됐지만 열린 파일, 중복 프로세스, runtime reset/backup/trace 증가를 분리한다.
# =============================================================================

def _audit_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return int(default)


def _audit_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _audit_age(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime) if path.exists() else 999999.0
    except Exception:
        return 999999.0


def _audit_rel(path) -> str:
    try:
        pp = Path(path)
        return str(pp.relative_to(BOT_DIR))
    except Exception:
        return str(path)


def _audit_fmt_delta(value) -> str:
    n = _audit_int(value, 0)
    sign = "+" if n >= 0 else "-"
    return sign + _fmt_bytes(abs(n))


def _audit_scan_files(root: Path, max_files: int = 50000) -> dict:
    files: dict[str, int] = {}
    errors = 0
    scanned = 0
    truncated = False
    try:
        root = root.resolve()
    except Exception:
        root = Path(root)
    if not root.exists():
        return {"root": str(root), "files": files, "total": 0, "scanned": 0, "errors": 0, "truncated": False}
    try:
        for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
            # 가상/마운트 하위가 섞이지 않도록 symlink directory는 내려가지 않는다.
            safe_dirs = []
            for name in dirnames:
                dp = Path(dirpath) / name
                try:
                    if not dp.is_symlink():
                        safe_dirs.append(name)
                except Exception:
                    errors += 1
            dirnames[:] = safe_dirs
            for name in filenames:
                if scanned >= max_files:
                    truncated = True
                    break
                fp = Path(dirpath) / name
                scanned += 1
                try:
                    if fp.is_symlink() or not fp.is_file():
                        continue
                    files[str(fp)] = int(fp.stat().st_size)
                except Exception:
                    errors += 1
            if truncated:
                break
    except Exception:
        errors += 1
    return {"root": str(root), "files": files, "total": int(sum(files.values())), "scanned": scanned, "errors": errors, "truncated": truncated}


def _audit_category(path: str) -> str:
    low = str(path).lower()
    name = Path(path).name.lower()
    if any(x in name for x in ("paper_bot_open", "paper_bot_closed", "trade_log", "closed_result", "score_ledger")):
        return "핵심원장"
    if ".paper_runtime_reset_" in low:
        return "paper_runtime_reset"
    if "backup" in name or name.endswith(".bak"):
        return "backup"
    if name.startswith("coinbot_update_v") and name.endswith(".zip"):
        return "update_zip"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith((".log", ".out", ".err")):
        return "log"
    if name.endswith(".jsonl"):
        return "jsonl_trace"
    if name.endswith(".gz"):
        return "gzip_archive"
    if name.endswith(".json"):
        return "json_cache_status"
    if name.endswith(".py"):
        return "python_code"
    return "other"


def _audit_category_totals(files: dict[str, int]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for path, size in files.items():
        cat = _audit_category(path)
        row = out.setdefault(cat, {"bytes": 0, "count": 0})
        row["bytes"] += int(size)
        row["count"] += 1
    return dict(sorted(out.items(), key=lambda kv: kv[1]["bytes"], reverse=True))


def _audit_top_files(files: dict[str, int], limit: int = 15) -> list[dict]:
    return [
        {"path": _audit_rel(path), "bytes": int(size), "category": _audit_category(path)}
        for path, size in sorted(files.items(), key=lambda kv: kv[1], reverse=True)[:max(1, limit)]
    ]


def _audit_du_bytes(path: Path, timeout: int = 12) -> int:
    if not path.exists():
        return 0
    rc, out = run(["du", "-sx", "-B1", str(path)], timeout=timeout)
    if rc == 0 and out:
        try:
            return int(out.split()[0])
        except Exception:
            pass
    return 0


def _audit_journal_bytes() -> tuple[int, str]:
    rc, out = run(["journalctl", "--disk-usage", "--no-pager"], timeout=8)
    text = (out or "").strip()
    if rc != 0:
        return 0, tail(text, 300)
    # 예: Archived and active journals take up 1.2G in the file system.
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT])(?:i?B|B)?", text, re.I)
    if not m:
        return 0, text
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}.get(unit, 1)
    return int(val * mult), text


def _audit_proc_rows() -> list[dict]:
    rc, out = run(["ps", "-eo", "pid=,ppid=,etimes=,pcpu=,pmem=,rss=,nlwp=,args="], timeout=8)
    rows = []
    if rc != 0:
        return rows
    def classify(args: str) -> str:
        tokens = [Path(tok.strip("'\"")).name for tok in args.split() if tok.strip("'\"").endswith(".py")]
        for base in tokens:
            if base == "backga_guard_bot.py" or base.startswith("backga_guard_bot_v"):
                return "guard"
            if base == "paper_bot.py" or base.startswith("paper_bot_v"):
                return "paper"
            if base == "ws_sidecar.py" or base.startswith("ws_sidecar_v"):
                return "ws"
            if base == "bithumb_micro_sidecar.py" or base.startswith("bithumb_micro_sidecar_v"):
                return "micro"
            for n, spec in WORKER_SPECS.items():
                active = str(spec.get("active") or "")
                prefix = str(spec.get("prefix") or "")
                if base == active or (prefix and base.startswith(prefix + "_v")):
                    return n
            if base == "bot.py" or base.startswith("coinbot_main_v"):
                return "main"
        return ""

    for line in (out or "").splitlines():
        parts = line.strip().split(None, 7)
        if len(parts) < 8:
            continue
        pid, ppid, etimes, pcpu, pmem, rss, nlwp, args = parts
        first_exec = Path(args.split()[0]).name.lower() if args.split() else ""
        if "python" not in first_exec:
            continue
        component = classify(args)
        if not component:
            continue
        rows.append({
            "component": component,
            "pid": _audit_int(pid),
            "ppid": _audit_int(ppid),
            "elapsed_sec": _audit_int(etimes),
            "cpu_pct": _audit_float(pcpu),
            "mem_pct": _audit_float(pmem),
            "rss_bytes": _audit_int(rss) * 1024,
            "threads": _audit_int(nlwp),
            "args": args[:500],
        })
    return rows


def _audit_open_fd_snapshot() -> dict:
    owners: dict[str, list[dict]] = {}
    deleted: list[dict] = []
    proc_root = Path("/proc")
    checked = 0
    errors = 0
    try:
        bot_real = str(BOT_DIR.resolve())
    except Exception:
        bot_real = str(BOT_DIR)
    for pd in proc_root.iterdir():
        if not pd.name.isdigit():
            continue
        pid = _audit_int(pd.name)
        if pid <= 0:
            continue
        try:
            cmd_raw = (pd / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore").strip()
            comm = (pd / "comm").read_text(encoding="utf-8", errors="ignore").strip()
        except Exception:
            cmd_raw, comm = "", ""
        fd_dir = pd / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except Exception:
            continue
        for fd in fds:
            checked += 1
            try:
                target = os.readlink(str(fd))
            except Exception:
                errors += 1
                continue
            is_deleted = target.endswith(" (deleted)")
            clean = target[:-10] if is_deleted else target
            try:
                real = os.path.realpath(clean) if clean.startswith("/") else clean
            except Exception:
                real = clean
            if is_deleted:
                try:
                    size = int(os.stat(str(fd)).st_size)
                except Exception:
                    size = 0
                deleted.append({"pid": pid, "fd": fd.name, "bytes": size, "target": clean, "cmd": (cmd_raw or comm)[:300]})
            if real.startswith(bot_real + os.sep) or real == bot_real or real.startswith("/var/log/"):
                owners.setdefault(real, []).append({"pid": pid, "cmd": (cmd_raw or comm)[:240]})
    deleted.sort(key=lambda r: r.get("bytes", 0), reverse=True)
    return {"owners": owners, "deleted": deleted, "deleted_bytes": sum(int(r.get("bytes", 0)) for r in deleted), "checked": checked, "errors": errors}


def _audit_worker_rows(proc_rows: list[dict]) -> list[dict]:
    by_component: dict[str, list[dict]] = {}
    for row in proc_rows:
        by_component.setdefault(str(row.get("component")), []).append(row)
    out = []
    for name in WORKER_SPECS:
        st = worker_state_dict(name)
        raw = st.get("raw") if isinstance(st.get("raw"), dict) else {}
        cycle = None
        for key in ("last_sec", "cycle_sec", "last_cycle_sec", "loop_sec", "elapsed_sec"):
            if raw.get(key) not in (None, "", "-"):
                cycle = _audit_float(raw.get(key), -1.0)
                break
        prows = by_component.get(name, [])
        out.append({
            "name": name,
            "label": st.get("label"),
            "service": st.get("service"),
            "service_active": st.get("service_active"),
            "status_age_sec": _audit_float(st.get("age"), 999999.0),
            "cycle_sec": cycle,
            "row_count": raw.get("row_count", raw.get("evaluated", "-")),
            "phase": raw.get("phase", raw.get("last_phase", "-")),
            "process_count": len(prows),
            "cpu_pct": sum(_audit_float(x.get("cpu_pct")) for x in prows),
            "rss_bytes": sum(_audit_int(x.get("rss_bytes")) for x in prows),
            "threads": sum(_audit_int(x.get("threads")) for x in prows),
            "pids": [x.get("pid") for x in prows],
            "error": st.get("last_error"),
        })
    out.sort(key=lambda r: max(_audit_float(r.get("cycle_sec"), 0.0), _audit_float(r.get("status_age_sec"), 0.0)), reverse=True)
    return out


def _audit_service_rows(proc_rows: list[dict]) -> list[dict]:
    services = {
        "main": MAIN_SERVICE,
        "paper": PAPER_SERVICE,
        "guard": GUARD_SERVICE,
        "ws": WS_SERVICE,
        "micro": MICRO_SERVICE,
    }
    services.update({n: str(spec["service"]) for n, spec in WORKER_SPECS.items()})
    by_comp: dict[str, list[dict]] = {}
    for row in proc_rows:
        by_comp.setdefault(str(row.get("component")), []).append(row)
    out = []
    for comp, svc in services.items():
        rc, txt = run(["systemctl", "show", svc, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID", "-p", "NRestarts", "-p", "MemoryCurrent", "-p", "TasksCurrent", "-p", "CPUUsageNSec"], timeout=6)
        props = {}
        if rc == 0:
            for line in (txt or "").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    props[k.strip()] = v.strip()
        prows = by_comp.get(comp, [])
        out.append({
            "component": comp,
            "service": svc,
            "active": props.get("ActiveState", "?"),
            "sub": props.get("SubState", "?"),
            "main_pid": _audit_int(props.get("MainPID")),
            "restart_count": _audit_int(props.get("NRestarts")),
            "memory_bytes": _audit_int(props.get("MemoryCurrent")),
            "tasks": _audit_int(props.get("TasksCurrent")),
            "cpu_total_sec": _audit_float(props.get("CPUUsageNSec")) / 1_000_000_000.0,
            "process_count": len(prows),
            "pids": [r.get("pid") for r in prows],
            "cpu_pct": sum(_audit_float(r.get("cpu_pct")) for r in prows),
            "rss_bytes": sum(_audit_int(r.get("rss_bytes")) for r in prows),
        })
    return out


def _audit_file_deltas(before: dict[str, int], after: dict[str, int], owners: dict[str, list[dict]], limit: int = 18) -> list[dict]:
    rows = []
    for path in set(before) | set(after):
        delta = int(after.get(path, 0)) - int(before.get(path, 0))
        if delta == 0:
            continue
        owner_rows = owners.get(os.path.realpath(path), [])
        rows.append({
            "path": _audit_rel(path),
            "before": int(before.get(path, 0)),
            "after": int(after.get(path, 0)),
            "delta": delta,
            "category": _audit_category(path),
            "owners": owner_rows[:4],
        })
    rows.sort(key=lambda r: abs(int(r.get("delta", 0))), reverse=True)
    return rows[:max(1, limit)]


def _audit_previous_delta(current_files: dict[str, int], current_disk_used: int) -> dict:
    state = read_json(BOT_DIR / GUARD_DEEP_AUDIT_STATE_FILE)
    prev_files = state.get("files") if isinstance(state.get("files"), dict) else {}
    prev_ts = _audit_float(state.get("ts"), 0.0)
    prev_used = _audit_int(state.get("disk_used"), 0)
    deltas = []
    if prev_files:
        for rel, size in current_files.items():
            old = _audit_int(prev_files.get(rel), 0)
            d = int(size) - old
            if d:
                deltas.append({"path": rel, "delta": d, "before": old, "after": int(size), "category": _audit_category(rel)})
        for rel, old in prev_files.items():
            if rel not in current_files:
                deltas.append({"path": rel, "delta": -_audit_int(old), "before": _audit_int(old), "after": 0, "category": _audit_category(rel)})
        deltas.sort(key=lambda r: abs(int(r.get("delta", 0))), reverse=True)
    return {
        "available": bool(prev_files and prev_ts),
        "age_sec": max(0.0, time.time() - prev_ts) if prev_ts else None,
        "disk_used_delta": int(current_disk_used) - prev_used if prev_used else None,
        "file_deltas": deltas[:20],
    }


def _audit_save_baseline(files: dict[str, int], disk_used: int) -> None:
    # 64KB 이상만 저장해 상태파일 자체가 새 디스크 문제를 만들지 않게 제한한다.
    rows = [(_audit_rel(path), int(size)) for path, size in files.items() if int(size) >= 64 * 1024]
    rows.sort(key=lambda kv: kv[1], reverse=True)
    write_json(BOT_DIR / GUARD_DEEP_AUDIT_STATE_FILE, {
        "schema": "guard_deep_audit_state_v928",
        "ts": time.time(),
        "time": now(),
        "disk_used": int(disk_used),
        "files": dict(rows[:5000]),
    })


def _audit_suspicions(report: dict) -> list[str]:
    out = []
    sample = report.get("sample") or {}
    delta = _audit_int(sample.get("disk_used_delta"), 0)
    if delta > 25 * 1024 * 1024:
        out.append(f"짧은 표본 동안 디스크 사용량이 {_fmt_bytes(delta)} 증가")
    growth = [r for r in sample.get("file_deltas", []) if _audit_int(r.get("delta"), 0) > 5 * 1024 * 1024]
    if growth:
        out.append("빠르게 커진 파일 있음: " + ", ".join(f"{r['path']} +{_fmt_bytes(r['delta'])}" for r in growth[:3]))
    deleted_bytes = _audit_int((report.get("open_fds") or {}).get("deleted_bytes"), 0)
    if deleted_bytes > 20 * 1024 * 1024:
        out.append(f"삭제됐지만 프로세스가 잡고 있는 파일 {_fmt_bytes(deleted_bytes)}")
    journal = _audit_int((report.get("areas") or {}).get("journal_bytes"), 0)
    if journal > 512 * 1024 * 1024:
        out.append(f"systemd journal 사용량 큼: {_fmt_bytes(journal)}")
    dup = [r for r in report.get("services", []) if _audit_int(r.get("process_count"), 0) > 1]
    if dup:
        out.append("중복 프로세스 의심: " + ", ".join(f"{r['component']} {r['process_count']}개" for r in dup[:6]))
    slow = [r for r in report.get("workers", []) if _audit_float(r.get("status_age_sec"), 999999.0) < 900000 and (_audit_float(r.get("cycle_sec"), 0.0) > 30 or _audit_float(r.get("status_age_sec"), 0.0) > 60)]
    if slow:
        out.append("느린 작업자: " + ", ".join(f"{r['name']} cycle {r.get('cycle_sec')}s/age {r.get('status_age_sec'):.1f}s" for r in slow[:5]))
    resets = _audit_int((report.get("categories") or {}).get("paper_runtime_reset", {}).get("bytes"), 0)
    if resets > 100 * 1024 * 1024:
        out.append(f"paper runtime reset 백업 누적 {_fmt_bytes(resets)}")
    if not out:
        out.append("현재 20초 표본에서 단일 급증 원인은 확정되지 않음. 이전 감사 대비 증가량을 다음 호출에서 대조")
    return out


def _audit_format(report: dict) -> str:
    r0 = report.get("resource_before") or {}
    r1 = report.get("resource_after") or {}
    sample = report.get("sample") or {}
    prev = report.get("previous") or {}
    lines = [
        "🔬 작업자·디스크 통합감사 /gdeep_audit · v928",
        f"- 읽기 전용 / 표본 {report.get('sample_sec')}초 / 삭제·정리 0건",
        f"- 디스크: 사용 {_fmt_bytes(r1.get('disk_used',0))} / 남음 {_fmt_bytes(r1.get('disk_free',0))} / {r1.get('disk_pct',0):.1f}%",
        f"- 표본 변화: 사용량 {_audit_fmt_delta(sample.get('disk_used_delta',0))}",
    ]
    if prev.get("available"):
        d = prev.get("disk_used_delta")
        lines.append(f"- 이전 감사 대비: {prev.get('age_sec',0)/60:.1f}분 / 디스크 {_audit_fmt_delta(d or 0)}")
    lines += ["", "[판정]"] + [f"- {x}" for x in report.get("suspicions", [])]

    lines += ["", "[느린 작업자 TOP]"]
    for row in (report.get("workers") or [])[:9]:
        cycle = row.get("cycle_sec")
        cycle_text = "-" if cycle is None or cycle < 0 else f"{cycle:.1f}s"
        lines.append(
            f"- {row['name']}: cycle {cycle_text} / status age {row['status_age_sec']:.1f}s / CPU {row['cpu_pct']:.1f}% / RSS {_fmt_bytes(row['rss_bytes'])} / proc {row['process_count']} / phase {row.get('phase','-')}"
        )

    lines += ["", "[중복·서비스]"]
    duplicate_found = False
    for row in report.get("services", []):
        if row.get("component") in {"main", "paper", "guard", "ws", "micro", *WORKER_SPECS.keys()} and (_audit_int(row.get("process_count"),0) != 1 or _audit_int(row.get("restart_count"),0) > 0):
            duplicate_found = True
            lines.append(f"- {row['component']}: svc {row['active']}/{row['sub']} / proc {row['process_count']} / pid {row.get('pids') or '-'} / restart {row['restart_count']} / mem {_fmt_bytes(row['memory_bytes'])}")
    if not duplicate_found:
        lines.append("- 확인된 관리대상 중복 프로세스 없음")

    lines += ["", f"[파일 증가 TOP · {report.get('sample_sec')}초]"]
    growth_rows = sample.get("file_deltas", [])
    if not growth_rows:
        lines.append("- BOT_DIR 안에서 크기 변화 파일 없음")
    else:
        for row in growth_rows[:12]:
            d = _audit_int(row.get("delta"))
            owner = row.get("owners") or []
            owner_text = ",".join(str(x.get("pid")) for x in owner) if owner else "-"
            lines.append(f"- {row['path']}: {_audit_fmt_delta(d)} / {row['category']} / writer PID {owner_text}")
    log_growth = sample.get("var_log_file_deltas", [])
    if log_growth:
        lines += ["", f"[/var/log 증가 TOP · {report.get('sample_sec')}초]"]
        for row in log_growth[:8]:
            d = _audit_int(row.get("delta"))
            owner = row.get("owners") or []
            owner_text = ",".join(str(x.get("pid")) for x in owner) if owner else "-"
            lines.append(f"- {row['path']}: {_audit_fmt_delta(d)} / writer PID {owner_text}")

    compact = report.get('runtime_compaction_v928') or {}
    st = compact.get('strategy') if isinstance(compact.get('strategy'), dict) else {}
    rt = compact.get('target_router') if isinstance(compact.get('target_router'), dict) else {}
    lines += ['', '[v928 경량화 상태]']
    if not st and not rt:
        lines.append('- v928 worker status 생성 대기')
    else:
        if st:
            fb = st.get('file_bytes') if isinstance(st.get('file_bytes'), dict) else {}
            hold = st.get('hold') if isinstance(st.get('hold'), dict) else {}
            lines.append(f"- strategy cycle {st.get('cycle_sec','-')}s / trace {_fmt_bytes(fb.get('single_pass_trace',0))} / reject {_fmt_bytes(fb.get('reject_summary',0))}")
            lines.append(f"- S1 hold {hold.get('count',0)}개 / 원본추정 {_fmt_bytes(hold.get('full_row_bytes_before_v928',0))} → compact {_fmt_bytes(hold.get('compact_row_bytes_after_v928',0))} / JSONL mirror retired")
        if rt:
            lines.append(f"- target_router source {rt.get('recheck_source','-')} / source rows {rt.get('recheck_source_rows',0)} / queue {_fmt_bytes(rt.get('queue_file_bytes',0))}")
            lines.append(f"- full JSONL 매 cycle 읽기: {rt.get('full_jsonl_read_each_cycle')} / candidate lite {_fmt_bytes(rt.get('candidate_lite_file_bytes',0))}")

    areas = report.get("areas") or {}
    lines += [
        "",
        "[현재 용량 위치]",
        f"- 봇폴더: {_fmt_bytes(areas.get('bot_dir_bytes',0))}",
        f"- 봇 상위 홈: {_fmt_bytes(areas.get('bot_parent_bytes',0))}",
        f"- /var/log: {_fmt_bytes(areas.get('var_log_bytes',0))}",
        f"- systemd journal: {_fmt_bytes(areas.get('journal_bytes',0))}",
        f"- /var/cache: {_fmt_bytes(areas.get('var_cache_bytes',0))} / docker: {_fmt_bytes(areas.get('docker_bytes',0))}",
        f"- /tmp: {_fmt_bytes(areas.get('tmp_bytes',0))} / /var/tmp: {_fmt_bytes(areas.get('var_tmp_bytes',0))}",
    ]
    lines += ["", "[봇폴더 종류별]"]
    for cat, row in list((report.get("categories") or {}).items())[:10]:
        lines.append(f"- {cat}: {_fmt_bytes(row.get('bytes',0))} / {row.get('count',0)}개")

    fd = report.get("open_fds") or {}
    lines += ["", "[삭제됐지만 열린 파일]"]
    if not fd.get("deleted"):
        lines.append("- 없음")
    else:
        lines.append(f"- 합계 {_fmt_bytes(fd.get('deleted_bytes',0))} / {len(fd.get('deleted') or [])}개")
        for row in (fd.get("deleted") or [])[:8]:
            lines.append(f"- PID {row['pid']} / {_fmt_bytes(row['bytes'])} / {row['target']}")

    lines += ["", "[큰 파일 TOP]"]
    for row in (report.get("top_files") or [])[:12]:
        lines.append(f"- {row['path']}: {_fmt_bytes(row['bytes'])} / {row['category']}")
    if report.get("var_log_top_files"):
        lines += ["", "[/var/log 큰 파일 TOP]"]
        for row in (report.get("var_log_top_files") or [])[:8]:
            lines.append(f"- {row['path']}: {_fmt_bytes(row['bytes'])}")
    lines += [
        "",
        "[보존]",
        "- OPEN/CLOSED/trade_log/score 핵심 원장 수정·삭제 없음",
        f"- 전체 JSON: {GUARD_DEEP_AUDIT_JSON}",
        f"- 전체 TXT: {GUARD_DEEP_AUDIT_TEXT}",
        "- 다음 호출은 이전 감사 대비 증가량까지 비교",
    ]
    return "\n".join(lines)


def gdeep_audit_text(sample_sec: int = 20) -> str:
    sample_sec = max(5, min(30, _audit_int(sample_sec, 20)))
    t0 = perf_now()
    before_res = server_resource_snapshot()
    before_scan = _audit_scan_files(BOT_DIR, max_files=60000)
    before_var_log = _audit_scan_files(Path("/var/log"), max_files=25000)
    prev_map = {_audit_rel(k): int(v) for k, v in before_scan.get("files", {}).items() if int(v) >= 64 * 1024}
    previous = _audit_previous_delta(prev_map, _audit_int(before_res.get("disk_used")))
    proc_rows = _audit_proc_rows()
    workers = _audit_worker_rows(proc_rows)
    services = _audit_service_rows(proc_rows)
    journal_bytes, journal_text = _audit_journal_bytes()
    areas = {
        "bot_dir_bytes": _audit_int(before_scan.get("total")),
        "bot_parent_bytes": _audit_du_bytes(BOT_DIR.parent, timeout=14),
        "var_log_bytes": _audit_du_bytes(Path("/var/log"), timeout=12),
        "journal_bytes": journal_bytes,
        "journal_text": journal_text,
        "var_cache_bytes": _audit_du_bytes(Path("/var/cache"), timeout=10),
        "docker_bytes": _audit_du_bytes(Path("/var/lib/docker"), timeout=10),
        "tmp_bytes": _audit_du_bytes(Path("/tmp"), timeout=8),
        "var_tmp_bytes": _audit_du_bytes(Path("/var/tmp"), timeout=8),
    }
    # 실제 증가 파일을 잡기 위한 짧은 동일구간 재측정.
    time.sleep(sample_sec)
    after_res = server_resource_snapshot()
    after_scan = _audit_scan_files(BOT_DIR, max_files=60000)
    after_var_log = _audit_scan_files(Path("/var/log"), max_files=25000)
    fd_snapshot = _audit_open_fd_snapshot()
    file_deltas = _audit_file_deltas(before_scan.get("files", {}), after_scan.get("files", {}), fd_snapshot.get("owners", {}), limit=24)
    var_log_deltas = _audit_file_deltas(before_var_log.get("files", {}), after_var_log.get("files", {}), fd_snapshot.get("owners", {}), limit=16)
    report = {
        "schema": "guard_deep_runtime_disk_audit_v928",
        "time": now(),
        "ts": time.time(),
        "guard_version": VERSION,
        "sample_sec": sample_sec,
        "elapsed_sec": perf_now() - t0,
        "read_only": True,
        "resource_before": before_res,
        "resource_after": after_res,
        "sample": {
            "disk_used_delta": _audit_int(after_res.get("disk_used")) - _audit_int(before_res.get("disk_used")),
            "disk_free_delta": _audit_int(after_res.get("disk_free")) - _audit_int(before_res.get("disk_free")),
            "bot_dir_delta": _audit_int(after_scan.get("total")) - _audit_int(before_scan.get("total")),
            "file_deltas": file_deltas,
            "var_log_file_deltas": var_log_deltas,
            "scan_before": {k: v for k, v in before_scan.items() if k != "files"},
            "scan_after": {k: v for k, v in after_scan.items() if k != "files"},
        },
        "previous": previous,
        "workers": workers,
        "services": services,
        "processes": proc_rows,
        "areas": areas,
        "categories": _audit_category_totals(after_scan.get("files", {})),
        "top_files": _audit_top_files(after_scan.get("files", {}), limit=24),
        "var_log_top_files": _audit_top_files(after_var_log.get("files", {}), limit=16),
        "open_fds": fd_snapshot,
        "runtime_compaction_v928": {
            "strategy": read_json(BOT_DIR / "clean_strategy_storage_status_v928.json"),
            "target_router": read_json(BOT_DIR / "clean_target_router_compact_status_v928.json"),
            "storage_fix": read_json(BOT_DIR / V928_STORAGE_FIX_JSON),
        },
    }
    report["suspicions"] = _audit_suspicions(report)
    text = _audit_format(report)
    try:
        write_json(BOT_DIR / GUARD_DEEP_AUDIT_JSON, report)
        (BOT_DIR / GUARD_DEEP_AUDIT_TEXT).write_text(text + "\n", encoding="utf-8")
        current_map = {_audit_rel(k): int(v) for k, v in after_scan.get("files", {}).items()}
        _audit_save_baseline(current_map, _audit_int(after_res.get("disk_used")))
    except Exception as exc:
        text += f"\n- 감사 결과파일 저장 실패: {exc.__class__.__name__}: {str(exc)[:160]}"
    return text


def help_text() -> str:
    return (
        "🛡 백가 가드봇 메뉴\n"
        "기본\n"
        "/guard - 가드 핵심 상태\n"
        "/gdeploy - 배포·버전 상태\n"
        "/gupgrade_menu - 업그레이드 메뉴\n"
        "/gmenu_sync - 텔레그램 하단 메뉴 강제 갱신\n"
        "/glog - 메인봇 최근 오류 로그\n"
        "/gpaperlog - 페이퍼봇 최근 로그\n\n"
        "업그레이드\n"
        "1) /gupgrade - 전체 최신화: 가드 → 메인 → 페이퍼 → WS → micro\n"
        "2) /gupgrade_main - 메인봇만 최신 적용\n"
        "3) /gupgrade_paper - 페이퍼봇만 최신 적용\n"
        "4) /gupgrade_ws - WS 직원만 최신 적용\n"
        "5) /gupgrade_micro - micro 직원만 최신 적용\n"
        "6) /gupgrade_guard - 가드봇만 최신 적용\n"
        "- 원칙: 같은 적용 본선 사용, hash 동일이면 재시작 생략\n\n"
        "복구/재시작\n"
        "/grestart - 메인봇 재시작\n"
        "/gpaper_restart - 페이퍼봇 재시작\n"
        "/gpaper_reset - 페이퍼 실행연결 재설치\n"
        "/gpaper_service - 페이퍼봇 서비스화 안내\n"
        "/gexternal_state - 외부직원 통합상태\n"
        "/gdeep_audit - 작업자 지연·디스크 증가 원인 통합감사(읽기전용)\n"
        "/gstorage_fix - 승인된 로그상한·archive·trace 정리\n"
        "/gcleanup - 디스크 순환정리\n"
        "/gws_state - 웹소켓 직원 상태\n"
        "/gws_start - 웹소켓 직원 시작\n"
        "/gws_stop - 웹소켓 직원 중지\n"
        "/gws_restart - 웹소켓 직원 재시작\n"
        "/gmicro_state - 빗썸 호가·체결 직원 상태\n"
        "/gmicro_restart - 빗썸 호가·체결 직원 재시작\n"
        "/gbackups - 메인봇 백업 목록\n"
        "/grollback - 메인봇 최신 백업 롤백\n"
        "/gunlock - 배포 lock 삭제\n\n"
        "가드봇 자체\n"
        "/gguard_restart - 가드봇 재시작\n\n"
        "운영법: 전체가 필요하면 /gupgrade, 파일 하나만 바뀌었으면 위 개별 업그레이드 사용.\n"
        "주의: 전략/조건/청산은 건드리지 않고 파일 교체와 재시작만 관리"
    )

def guard_command_menu_items() -> list[dict]:
    """Telegram 하단 메뉴에 등록할 명령 목록.

    v2.5.37: 사용자가 실제로 자주 누르는 업그레이드 명령을 맨 위에 둔다.
    alias 명령(gws_upgrade/gmicro_upgrade 등)은 handler에는 남기되 메뉴에는 중복 노출하지 않는다.
    """
    return [
        {"command": "gupgrade", "description": "1 전체 최신화"},
        {"command": "gupgrade_main", "description": "2 메인봇만 최신 적용"},
        {"command": "gupgrade_paper", "description": "3 페이퍼봇만 최신 적용"},
        {"command": "gupgrade_ws", "description": "4 WS 직원만 최신 적용"},
        {"command": "gupgrade_micro", "description": "5 micro 직원만 최신 적용"},
        {"command": "gupgrade_guard", "description": "6 가드봇만 최신 적용"},
        {"command": "gupgrade_bundle", "description": "한 번으로 전체 적용·재시작·검수"},
        {"command": "grelease_verify", "description": "현재 릴리스 통합검수"},
        {"command": "grelease_last", "description": "최근 자동 배포 최종결과"},
        {"command": "gworker_upgrade", "description": "9 worker 최신 적용"},
        {"command": "gworker_state", "description": "worker 통합상태"},
        {"command": "gdeep_audit", "description": "작업자·디스크 원인 통합감사"},
        {"command": "gstorage_fix", "description": "저장공간·지연 원인수술"},
        {"command": "gupgrade_menu", "description": "업그레이드 메뉴"},
        {"command": "guard", "description": "가드 핵심 상태"},
        {"command": "gdeploy", "description": "배포·버전 상태"},
        {"command": "gpaper_state", "description": "페이퍼봇 상태"},
        {"command": "gexternal_state", "description": "외부직원 통합상태"},
        {"command": "gworker_restart", "description": "worker 재시작"},
        {"command": "glog", "description": "메인봇 로그"},
        {"command": "gpaperlog", "description": "페이퍼봇 로그"},
        {"command": "grestart", "description": "메인봇 재시작"},
        {"command": "gpaper_restart", "description": "페이퍼봇 재시작"},
        {"command": "gpaper_reset", "description": "페이퍼 실행연결 재설치"},
        {"command": "gws_state", "description": "웹소켓 직원 상태"},
        {"command": "gws_restart", "description": "웹소켓 직원 재시작"},
        {"command": "gmicro_state", "description": "호가·체결 직원 상태"},
        {"command": "gmicro_restart", "description": "호가·체결 직원 재시작"},
        {"command": "gcleanup", "description": "디스크 순환정리"},
        {"command": "gbackups", "description": "백업 목록"},
        {"command": "gmenu_sync", "description": "텔레그램 메뉴 갱신"},
        {"command": "gmenu", "description": "가드 메뉴"},
    ]


def set_commands() -> dict:
    commands = guard_command_menu_items()
    # Telegram Bot API는 commands를 JSON 배열 문자열로 받는다.
    # 반환값을 남겨 /gmenu_sync에서 실제 성공/실패를 확인할 수 있게 한다.
    return api("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)}, timeout=15)


def sync_guard_menu_text() -> str:
    try:
        res = set_commands()
        ok = bool(res.get("ok")) if isinstance(res, dict) else False
        if ok:
            return (
                "✅ 텔레그램 메뉴 갱신 완료 /gmenu_sync\n"
                f"- guard: {VERSION}\n"
                "- 메뉴 순서: 전체 → 메인 → 페이퍼 → WS → micro → 가드\n"
                "- aliases는 계속 사용 가능하지만 메뉴에는 중복 노출하지 않음\n"
                "- 앱 하단 메뉴가 바로 안 바뀌면 채팅방을 나갔다가 다시 열거나 텔레그램을 재시작해줘."
            )
        return f"❌ 텔레그램 메뉴 갱신 실패 /gmenu_sync\n- 응답: {res}"
    except Exception as e:
        return f"❌ 텔레그램 메뉴 갱신 실패 /gmenu_sync\n- {type(e).__name__}: {e}"



def _extract_guard_command_lines(text: str) -> list[str]:
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip().startswith("/")]


def _guard_command_name(line: str) -> str:
    first = line.strip().split()[0].lower() if line.strip().split() else ""
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


def parse_upgrade_force_explicit(args: list[str]) -> tuple[bool, Optional[str]]:
    """개별 업그레이드 명령 공통 인자 파서.

    v2.5.62: /gupgrade_bundle nofetch coinbot_update_v###.zip 처럼
    nofetch/local 뒤에 zip 파일명을 적어도 explicit으로 인식한다.
    """
    args = [str(a).strip() for a in (args or []) if str(a).strip()]
    lowered = [a.lower() for a in args]
    force = bool(lowered and lowered[0] == "force")
    explicit = None
    scan = args[1:] if force else args
    for a in scan:
        low = a.lower()
        if low in {"force", "nofetch", "local", "skipgit", "skip_git", "fallback", "localfallback", "local_fallback"}:
            continue
        if low.endswith((".py", ".zip")):
            explicit = a
            break
    return force, explicit


def gupgrade_menu_text() -> str:
    """v2.5.36: 전체/개별 업그레이드 메뉴."""
    return "\n".join([
        "🛠 업그레이드 메뉴 /gupgrade_menu",
        "- 기본은 전체, 급할 때는 바뀐 코드만 개별 적용",
        "- 모든 개별 명령도 기존 단일 적용 함수 사용: hash 동일이면 재시작 생략",
        "",
        "[1] 전체",
        "/gupgrade - 가드 → worker → 메인 → 페이퍼 → WS → micro 전체 확인/적용",
        "",
        "[2] 메인봇",
        "/gupgrade_main - 수익형_v*.py만 적용",
        "",
        "[3] 페이퍼봇",
        "/gupgrade_paper - paper_bot_v*.py만 적용",
        "",
        "[4] WS 직원",
        "/gupgrade_ws - ws_sidecar_v*.py만 적용",
        "",
        "[5] micro 직원",
        "/gupgrade_micro - bithumb_micro_sidecar_v*.py만 적용",
        "",
        "[6] 가드봇",
        "/gupgrade_guard - backga_guard_bot_v*.py만 적용",
        "",
        "예시",
        "- /gupgrade_paper",
        "- /gupgrade_main force 수익형_v2.13.253.py",
        "- /gworker_upgrade",
    ])




def _bundle_version_key(path: Path) -> tuple:
    m = re.search(r"coinbot_update_v(\d+)", path.name)
    return (int(m.group(1)) if m else -1, path.name)

def find_latest_bundle_file(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        p = BOT_DIR / explicit
        return p if p.exists() else None
    files = sorted(BOT_DIR.glob("coinbot_update_v*.zip"), key=_bundle_version_key)
    return files[-1] if files else None

def _bundle_manifest_defaults(bundle_dir: Path) -> dict:
    # DEPLOY_BUNDLE.json이 없을 때도 파일명으로 최대한 복구. 단 정식 bundle은 manifest를 포함한다.
    def latest(pat: str) -> str:
        xs = sorted(bundle_dir.glob(pat), key=lambda p: p.name)
        return xs[-1].name if xs else ""
    def latest_main() -> str:
        xs = []
        for pat in ("수익형_v*.py", "coinbot_main_v*.py"):
            for p in bundle_dir.glob(pat):
                ver = BotVersion.parse(p.name)
                if ver:
                    xs.append((ver, p.name))
        return sorted(xs, key=lambda x: x[0])[-1][1] if xs else ""
    return {
        "main": latest_main(),
        "guard": latest("backga_guard_bot_v*.py"),
        "paper": latest("paper_bot_v*.py"),
        "workers": {
            "scanner": latest("scanner_worker_v*.py"),
            "candle": latest("candle_worker_v*.py"),
            "market": latest("market_regime_worker_v*.py"),
            "feature": latest("feature_worker_v*.py"),
            "orderflow": latest("orderflow_worker_v*.py"),
            "target_router": latest("target_router_worker_v*.py"),
            "risk": latest("risk_worker_v*.py"),
            "strategy": latest("strategy_worker_v*.py"),
            "review": latest("review_worker_v*.py"),
        },
    }

def _normalize_bundle_manifest(manifest: dict, bundle_dir: Path) -> dict:
    """v2.5.63 / v424: bundle manifest schema compatibility.

    과거 bundle은 {"main": "...py"}를 썼고,
    일부 전달용 bundle은 {"target": "...py", "files": [...]}만 들어왔다.
    기존 가드봇은 target/files를 main으로 보지 못해 bundle 내부에 파일이 있어도
    repo 루트에 복사하지 않은 채 py_compile을 시도하다가 FileNotFoundError가 났다.

    여기서는 새 fallback을 실행 경로에 붙이는 것이 아니라, manifest를 단일 형태로
    정규화한 뒤 기존 copy/apply 본선을 그대로 태운다.
    """
    if not isinstance(manifest, dict):
        return manifest
    m = dict(manifest)
    def is_main_py(name: str) -> bool:
        s = str(name or "").strip()
        return s.endswith(".py") and (s.startswith("coinbot_main_v") or s.startswith("수익형_v"))
    if not m.get("main"):
        target = m.get("target") or m.get("deploy_target") or m.get("main_file")
        if isinstance(target, str) and is_main_py(target) and (bundle_dir / target).exists():
            m["main"] = target
        else:
            files = m.get("files")
            if isinstance(files, list):
                cands = [str(x).strip() for x in files if isinstance(x, str) and is_main_py(str(x)) and (bundle_dir / str(x)).exists()]
                if cands:
                    def key(name: str):
                        bv = BotVersion.parse(name)
                        return (bv.major, bv.minor, bv.patch, name) if bv else (-1, -1, -1, name)
                    m["main"] = sorted(cands, key=key)[-1]
    return m

def _copy_bundle_files_to_repo(bundle_dir: Path, manifest: dict) -> list[str]:
    notes=[]
    names=[]
    for key in ["main", "guard", "paper", "ws", "micro", "ws_sidecar", "micro_sidecar"]:
        v = manifest.get(key)
        if isinstance(v, str) and v:
            names.append(v)
    workers = manifest.get("workers") if isinstance(manifest.get("workers"), dict) else {}
    for v in workers.values():
        if isinstance(v, str) and v:
            names.append(v)
    # DEPLOY_TARGET도 bundle 기준으로 갱신
    for name in sorted(set(names)):
        src = bundle_dir / name
        if not src.exists():
            notes.append(f"missing in bundle: {name}")
            continue
        dst = BOT_DIR / name
        if dst.exists() and short_hash(dst) == short_hash(src):
            notes.append(f"same {name}")
        else:
            shutil.copy2(src, dst)
            notes.append(f"copy {name}")
    main = manifest.get("main")
    if main:
        (BOT_DIR / "DEPLOY_TARGET.txt").write_text(str(main).strip()+"\n", encoding="utf-8")
        notes.append(f"DEPLOY_TARGET.txt -> {main}")
    return notes


def _bundle_num_from_name(name: str) -> int:
    m = re.search(r"coinbot_update_v(\d+)", str(name or ""))
    return int(m.group(1)) if m else -1


def _last_success_bundle_name() -> str:
    st = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    if isinstance(st, dict):
        b = st.get("bundle") or st.get("target_bundle") or ""
        if isinstance(b, str):
            return b.strip()
    return ""


def _safe_local_bundle_after_git_failure(explicit: Optional[str] = None) -> tuple[bool, str]:
    """v2.5.62: GitHub fetch timeout 때 오래된 zip을 무조건 적용하지 않기 위한 안전판.

    허용 조건:
    1) 사용자가 zip 파일명을 명시했고 그 zip이 실제 존재함
    2) 또는 로컬 최신 coinbot_update_v*.zip 번호가 마지막 성공 bundle 번호보다 큼

    그 외에는 stale zip 적용 사고를 막기 위해 중단한다.
    """
    bundle = find_latest_bundle_file(explicit)
    if not bundle:
        return False, "로컬 coinbot_update_v*.zip 없음"
    bnum = _bundle_num_from_name(bundle.name)
    last_name = _last_success_bundle_name()
    last_num = _bundle_num_from_name(last_name)
    if explicit:
        return True, f"명시 zip 사용: {bundle.name} / last={last_name or '?'}"
    if bnum > last_num >= 0:
        return True, f"로컬 최신 bundle이 마지막 성공보다 새 버전: {bundle.name} > {last_name}"
    if last_num < 0 and bnum >= 0:
        return False, f"마지막 성공 bundle 기록 없음 → stale 적용 방지: local={bundle.name}"
    return False, f"로컬 bundle이 새 버전으로 확인되지 않음: local={bundle.name} / last={last_name or '?'}"


def _github_repo_from_origin() -> tuple[str, str]:
    """Return (owner/repo, note) for the configured origin."""
    rc, out = run(["git", "config", "--get", "remote.origin.url"], cwd=BOT_DIR, timeout=8)
    raw = (out or "").strip().splitlines()[0].strip() if out else ""
    if not raw:
        raw = "https://github.com/dangdang971216-code/coin-bot.git"
    s = raw
    if s.startswith("git@github.com:"):
        s = "https://github.com/" + s.split(":", 1)[1]
    s = s.replace("git://github.com/", "https://github.com/")
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?/?$", s)
    if not m:
        return "dangdang971216-code/coin-bot", f"origin parse failed: {raw or '-'} → default"
    return f"{m.group(1)}/{m.group(2)}", f"origin={raw}"


def _http_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": "backga-guard-bot"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _download_url_to_path(url: str, dest: Path, timeout: int = 90) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "backga-guard-bot"})
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
            shutil.copyfileobj(resp, f)
        if tmp.stat().st_size <= 0:
            tmp.unlink(missing_ok=True)
            return False, "downloaded file is empty"
        tmp.replace(dest)
        return True, f"downloaded {dest.name} / {dest.stat().st_size} bytes"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def _github_direct_download_latest_bundle(explicit: Optional[str] = None) -> tuple[bool, list[str]]:
    """v2.5.64: /gupgrade_bundle must not depend on full git fetch.

    Old desired flow is kept from the user's side: upload coinbot_update_v###.zip to GitHub,
    then run /gupgrade_bundle. Internally we read the single zip file from GitHub contents API
    instead of fetching/resetting the whole repository.
    """
    notes: list[str] = []
    repo, repo_note = _github_repo_from_origin()
    notes.append(repo_note)
    api = f"https://api.github.com/repos/{repo}/contents?ref={urllib.parse.quote(GIT_BRANCH)}"
    try:
        items = _http_json(api, timeout=25)
    except Exception as exc:
        notes.append(f"github contents api failed: {exc.__class__.__name__}: {exc}")
        return False, notes
    if not isinstance(items, list):
        notes.append("github contents api returned non-list")
        return False, notes
    candidates = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        if not re.fullmatch(r"coinbot_update_v\d+\.zip", name):
            continue
        if explicit and name != explicit:
            continue
        num = _bundle_num_from_name(name)
        candidates.append((num, name, it.get("download_url") or ""))
    if not candidates:
        notes.append(f"github bundle not found: {explicit or 'coinbot_update_v*.zip'}")
        return False, notes
    candidates.sort(reverse=True)
    num, name, download_url = candidates[0]
    if not download_url:
        notes.append(f"download_url missing: {name}")
        return False, notes
    dest = BOT_DIR / name
    ok, msg = _download_url_to_path(download_url, dest, timeout=120)
    notes.append(msg)
    if not ok:
        return False, notes
    return True, [f"github direct bundle={name}"] + notes

def grelease_verify_text(manifest: Optional[dict] = None) -> str:
    """Read-only release verification used automatically after bundle apply and manually by /grelease_verify."""
    manifest = manifest if isinstance(manifest, dict) else {}
    paper_target = BOT_DIR / str(manifest.get('paper') or '') if manifest.get('paper') else choose_latest_paper_target()
    expected_version = _v399_expected_paper_version(paper_target) if paper_target and paper_target.exists() else ''
    expected_build = extract_assignment(paper_target, 'BUILD_LABEL') if paper_target and paper_target.exists() else ''
    expected_build = '' if expected_build == '?' else expected_build
    expected_hash = short_hash(paper_target) if paper_target and paper_target.exists() else ''
    ps = paper_runtime_snapshot(expected_version, expected_build, expected_hash)
    review_status = read_json(BOT_DIR / 'clean_review_worker_status.json') or {}
    review_active = is_service_active(WORKER_SPECS['review']['service']) if 'review' in WORKER_SPECS else 'unknown'
    review_expected = str(((manifest.get('workers') or {}).get('review') if isinstance(manifest.get('workers'), dict) else '') or '')
    review_target = BOT_DIR / review_expected if review_expected else None
    review_version_expected = extract_assignment(review_target, 'VERSION') if review_target and review_target.exists() else ''
    review_version_actual = str(review_status.get('version') or '') if isinstance(review_status, dict) else ''
    review_ok = str(review_active) in {'active','activating'} and (not review_version_expected or review_version_actual == review_version_expected)
    target_status = read_json(BOT_DIR / 'clean_target_router_status.json') or {}
    target_active = is_service_active(WORKER_SPECS['target_router']['service'])
    target_expected = str(((manifest.get('workers') or {}).get('target_router') if isinstance(manifest.get('workers'), dict) else '') or '')
    target_file = BOT_DIR / target_expected if target_expected else None
    target_version_expected = extract_assignment(target_file, 'VERSION') if target_file and target_file.exists() else ''
    target_version_actual = str(target_status.get('version') or '') if isinstance(target_status,dict) else ''
    target_owner = str(target_status.get('active_owner') or '') if isinstance(target_status,dict) else ''
    target_ok = str(target_active) in {'active','activating'} and (not target_version_expected or target_version_actual == target_version_expected) and (not target_owner or target_owner == 'target_router_single_run_once')
    main_expected = str(manifest.get('main') or '')
    main_target = BOT_DIR / main_expected if main_expected else None
    main_expected_version = extract_assignment(main_target, 'BOT_VERSION') if main_target and main_target.exists() else ''
    main_actual_version = extract_assignment(BOT_DIR / MAIN_ACTIVE_FILE, 'BOT_VERSION') if (BOT_DIR / MAIN_ACTIVE_FILE).exists() else ''
    main_ok = not main_expected_version or main_actual_version == main_expected_version
    ok = bool(ps.get('ok')) and review_ok and target_ok and main_ok
    reasons = list(ps.get('reasons') or [])
    if not review_ok: reasons.append(f'review mismatch active={review_active} expected={review_version_expected or "-"} actual={review_version_actual or "-"}')
    if not target_ok: reasons.append(f'target_router mismatch active={target_active} expected={target_version_expected or "-"} actual={target_version_actual or "-"} owner={target_owner or "-"}')
    if not main_ok: reasons.append(f'main mismatch expected={main_expected_version or "-"} actual={main_actual_version or "-"}')
    lines = [
        '✅ 릴리스 통합검수 PASS' if ok else '⚠️ 릴리스 통합검수 CHECK',
        f'- main: expected={main_expected_version or "-"} / actual={main_actual_version or "-"}',
        f'- paper: pid={ps.get("main_pid")} / status={ps.get("status_pid")} / writer={ps.get("writer_pid")} / owner={ps.get("writer_owner")} / count={ps.get("writer_count")}',
        f'- review: active={review_active} / expected={review_version_expected or "-"} / actual={review_version_actual or "-"} / last_sec={review_status.get("last_sec") if isinstance(review_status,dict) else "-"} / rss={review_status.get("rss_mb") if isinstance(review_status,dict) else "-"}MB',
        f'- target_router: active={target_active} / expected={target_version_expected or "-"} / actual={target_version_actual or "-"} / owner={target_owner or "-"} / last_sec={target_status.get("last_sec") if isinstance(target_status,dict) else "-"}',
        f'- errors: {"없음" if not reasons else " / ".join(reasons[:8])}',
        '- 원장·전략수치·자동매수 상태는 변경하지 않는 읽기전용 검수',
    ]
    return '\n'.join(lines)


def gupgrade_bundle_text(force: bool = False, explicit: Optional[str] = None, skip_git: bool = False) -> str:
    active_job = _load_release_job()
    if active_job:
        return "\n".join([
            "📦 ⚠️ 자동 이어가기 1건 처리 중",
            f"- request: {active_job.get('request_id') or '-'}",
            f"- bundle: {active_job.get('explicit') or '-'}",
            "- 중복 실행하지 않음",
            "- 확인: /grelease_last",
        ])
    t0=perf_now()
    status_path = BOT_DIR / UPGRADE_STATUS_FILE
    notes=[]
    progress_notify("📦 /gupgrade_bundle 접수\n- bundle zip 1개 기준으로 적용합니다\n- 단계별 진행을 표시합니다")
    if skip_git:
        progress_notify("[1/5] GitHub 확인 생략\n- 서버 로컬 zip 기준")
    if not skip_git:
        progress_notify("[1/5] GitHub 최신 zip 1개 확인 중\n- repo 전체 fetch/reset 없이 bundle zip만 직접 확인")
        ok_dl, dl_steps = _github_direct_download_latest_bundle(explicit=explicit)
        notes += ["[github bundle]"] + dl_steps[-6:]
        if not ok_dl:
            return "📦 ❌ bundle 업그레이드 실패 /gupgrade_bundle\n- GitHub 최신 zip 직접 확인 실패\n- repo 전체 fetch/stale fallback은 사용하지 않음\n" + "\n".join(notes[-10:])
    bundle = find_latest_bundle_file(explicit)
    if bundle:
        progress_notify(f"[2/5] bundle 발견\n- {bundle.name}\n- 압축 검수/해제 중")
    if not bundle:
        return "📦 ❌ bundle 업그레이드 실패 /gupgrade_bundle\n- coinbot_update_v*.zip 파일을 못 찾음"
    work = BOT_DIR / f".bundle_unpack_{int(time.time())}"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(bundle, 'r') as z:
            bad = z.testzip()
            if bad:
                return f"📦 ❌ bundle 검수 실패\n- zip 손상 의심: {bad}"
            z.extractall(work)
        manifest_path = work / "DEPLOY_BUNDLE.json"
        manifest = read_json(manifest_path) if manifest_path.exists() else _bundle_manifest_defaults(work)
        if not isinstance(manifest, dict):
            return "📦 ❌ bundle 검수 실패\n- DEPLOY_BUNDLE.json 형식 오류"
        manifest = _normalize_bundle_manifest(manifest, work)
        notes.append(f"bundle={bundle.name}")
        if manifest.get("main"):
            notes.append(f"manifest main={manifest.get('main')}")
        notes += _copy_bundle_files_to_repo(work, manifest)
        progress_notify("[3/5] bundle 파일 복사 완료\n- 변경 파일 중심 빠른 검수 중")
        # v2.5.48: component apply 단계에서 main/paper/worker/guard를 다시 검수하므로
        # bundle 단계의 전체 중복 py_compile은 줄인다. manifest 밖의 보조 .py만 여기서 검수한다.
        manifest_py = set()
        for key in ("main", "paper", "guard", "ws", "micro", "ws_sidecar", "micro_sidecar"):
            if manifest.get(key):
                manifest_py.add(str(manifest.get(key)))
        workers_manifest = manifest.get('workers') if isinstance(manifest.get('workers'), dict) else {}
        for fname in workers_manifest.values():
            if fname:
                manifest_py.add(str(fname))
        for n in [x for x in os.listdir(work) if x.endswith('.py') and x not in manifest_py]:
            okc,out=py_compile(BOT_DIR/n)
            if not okc:
                return f"📦 ❌ bundle py_compile 실패\n- file: {n}\n{tail(out,1200)}"
        # v2.5.66: 가드봇 파일이 바뀌는 bundle은 가드봇만 먼저 교체하고 여기서 끝낸다.
        # 같은 명령에서 가드봇 재시작 뒤 worker/main/paper까지 이어가지 않는다.
        if manifest.get('guard'):
            guard_target = BOT_DIR / str(manifest.get('guard'))
            active_guard = BOT_DIR / GUARD_ACTIVE_FILE
            try:
                guard_changed = guard_target.exists() and (not active_guard.exists() or short_hash(guard_target) != short_hash(active_guard))
            except Exception:
                guard_changed = True
            if guard_changed:
                progress_notify("[4/5] 가드봇 우선 적용 중\n- 재시작 뒤 저장된 job 단계부터 직접 이어감")
                guard_res = format_guard_apply_result(apply_guard_first_for_smart_upgrade(force=force, continuation_bundle=bundle.name))
                write_json(status_path, {"time": now(), "ok": True, "stage": "guard_only", "bundle": bundle.name, "guard_version": VERSION, "guard_only": True})
                return "\n".join([
                    "📦 ✅ 가드봇 우선 적용 완료 /gupgrade_bundle",
                    f"- guard: {VERSION}",
                    f"- bundle: {bundle.name}",
                    "- 처리: 가드봇 교체/재시작 후 같은 bundle 나머지를 1회 적용",
                    "- 새 가드봇은 상태머신 없이 나머지 적용 1회·검수 1회·결과 전송 1회만 수행함",
                    "",
                    "[guard]",
                    guard_res,
                    "",
                    "다음 확인: 별도 명령 없음",
                    "새 가드봇이 자동으로 최종 적용 결과를 전송",
                ])

        progress_notify("[4/5] 컴포넌트 적용 중\n- 변경된 worker/main/paper만 재시작")
        results=[]
        # workers first except guard/main/paper. 변경된 것만 fast-skip.
        workers=manifest.get('workers') if isinstance(manifest.get('workers'), dict) else {}
        for name, fname in workers.items():
            if name in WORKER_SPECS and fname:
                results.append(format_worker_apply_result(apply_worker_latest(name, force=force, explicit=fname, restart=True)))
        main_res = ""
        if manifest.get('main'):
            main_res = apply_main_latest_auto(force=force, explicit=str(manifest.get('main')), skip_git=True)
        paper_res = ""
        if manifest.get('paper'):
            paper_res = format_paper_apply_result(apply_paper_latest(force=force, explicit=str(manifest.get('paper'))))
        ws_res = ""
        ws_manifest = manifest.get('ws') or manifest.get('ws_sidecar')
        if ws_manifest:
            ws_res = format_ws_apply_result(apply_ws_sidecar_latest(force=force, explicit=str(ws_manifest), restart=True))
        micro_res = ""
        micro_manifest = manifest.get('micro') or manifest.get('micro_sidecar')
        if micro_manifest:
            micro_res = format_micro_apply_result(apply_micro_sidecar_latest(force=force, explicit=str(micro_manifest), restart=True))
        guard_res = ""
        if manifest.get('guard'):
            # guard는 마지막에 active만 갱신한다. 재시작은 명령 응답 후 service가 이어받게 한다.
            guard_res = format_guard_apply_result(apply_guard_first_for_smart_upgrade(force=force, continuation_bundle=bundle.name))
        write_json(status_path, {"time": now(), "ok": True, "stage": "bundle", "bundle": bundle.name, "manifest": manifest, "guard_version": VERSION})
        progress_notify("[5/5] bundle 적용 완료\n- 최종 결과 정리 전송 중")
        lines=["📦 ✅ bundle 업그레이드 완료 /gupgrade_bundle", f"- guard: {VERSION}", f"- bundle: {bundle.name}", f"- total: {fmt_sec(perf_now()-t0)}", "", "[bundle 복사]"]
        lines += [f"- {x}" for x in notes[:20]]
        if results:
            lines += ["", "[worker]"] + [r[:900] for r in results]
        if main_res:
            lines += ["", "[main]", tail(main_res, 1200)]
        if paper_res:
            lines += ["", "[paper]", paper_res]
        if ws_res:
            lines += ["", "[ws]", ws_res]
        if micro_res:
            lines += ["", "[micro]", micro_res]
        if guard_res:
            lines += ["", "[guard]", guard_res, "- guard가 바뀌었으면 /guard 또는 /gdeploy로 재시작 반영 확인"]
        lines += ["", "[자동 최종검수]", grelease_verify_text(manifest), "", "추가 명령: 문제 있을 때만 /grelease_verify 또는 /gpaperlog 120"]
        return "\n".join(lines)
    except Exception as exc:
        return f"📦 ❌ bundle 업그레이드 예외\n- {exc.__class__.__name__}: {exc}\n{tail(traceback.format_exc(), 1200)}"
    finally:
        shutil.rmtree(work, ignore_errors=True)

def gupgrade_component_text(component: str, force: bool = False, explicit: Optional[str] = None) -> str:
    """v2.5.36: 개별 업그레이드 단일 입구.

    기존 적용 본선을 그대로 사용하고, 대상만 분리한다.
    """
    component = str(component or "").strip().lower()
    labels = {
        "main": ("🚀", "메인봇", "/gupgrade_main"),
        "paper": ("🧪", "페이퍼봇", "/gupgrade_paper"),
        "ws": ("🛰", "WS 직원", "/gupgrade_ws"),
        "micro": ("🔎", "micro 직원", "/gupgrade_micro"),
        "guard": ("🛡", "가드봇", "/gupgrade_guard"),
    }
    if component not in labels:
        return gupgrade_menu_text()
    icon, label, command = labels[component]
    t0 = perf_now()
    status_path = BOT_DIR / UPGRADE_STATUS_FILE

    disk_ok, disk_text = preupgrade_disk_guard(force=force)
    if not disk_ok:
        write_json(status_path, {"time": now(), "ok": False, "stage": f"{component}_disk_precheck", "guard_version": VERSION})
        return f"{icon} ❌ {label} 개별 업그레이드 중단 {command}\n" + disk_text

    progress_notify(f"{icon} {label} 개별 업그레이드 시작\n- 전체가 아니라 {label}만 확인/적용\n- hash 동일이면 재시작 생략")
    progress_notify(f"{icon} [1/2] GitHub 최신 파일 확인 중")
    ok_git, steps = git_update()
    if not ok_git:
        write_json(status_path, {"time": now(), "ok": False, "stage": f"{component}_git", "steps": steps, "guard_version": VERSION})
        return f"{icon} ❌ {label} 개별 업그레이드 실패 {command}\n- 단계: GitHub 갱신 실패\n\n" + "\n\n".join(steps)

    progress_notify(f"{icon} [2/2] {label} 적용 확인 중")
    status = read_json(status_path)
    ok = False
    result_text = ""

    if component == "main":
        result_text = apply_main_latest_auto(force=force, explicit=explicit, skip_git=True)
        ok = "❌" not in result_text and "업그레이드 예외" not in result_text and "중단" not in result_text
        status["main_component_upgrade"] = {"time": now(), "ok": ok, "explicit": explicit or "", "force": force}
    elif component == "paper":
        res = apply_paper_latest(force=force, explicit=explicit)
        ok = bool(res.get("ok"))
        status["paper"] = res
        result_text = format_paper_apply_result(res)
    elif component == "ws":
        res = apply_ws_sidecar_latest(force=force, explicit=explicit, restart=True)
        ok = bool(res.get("ok"))
        status["ws_sidecar"] = res
        result_text = format_ws_apply_result(res)
    elif component == "micro":
        set_micro_desired(True, "gupgrade_micro_component")
        res = apply_micro_sidecar_latest(force=force, explicit=explicit, restart=True)
        ok = bool(res.get("ok"))
        status["micro_sidecar"] = res
        result_text = format_micro_apply_result(res)
    elif component == "guard":
        result_text = guard_self_upgrade(force=force, explicit=explicit)
        ok = "실패" not in result_text and "❌" not in result_text
        status["guard_component_upgrade"] = {"time": now(), "ok": ok, "explicit": explicit or "", "force": force}
    elif component == "workers":
        res = apply_workers_latest(force=force, restart=True)
        ok = bool(res.get("ok"))
        status["workers_component_upgrade"] = {"time": now(), "ok": ok, "force": force}
        result_text = gworker_upgrade_text(force=force)

    status["guard_version"] = VERSION
    status[f"{component}_component_upgrade_time"] = now()
    status[f"{component}_component_upgrade_total_sec"] = perf_now() - t0
    write_json(status_path, status)

    title_icon = "✅" if ok else "❌"
    return (
        f"{icon} {title_icon} {label} 개별 업그레이드 {command}\n"
        f"- guard: {VERSION}\n"
        f"- 대상: {label}만 적용 / 다른 컴포넌트는 건드리지 않음\n"
        f"- 방식: 기존 단일 적용 함수 사용, hash 동일이면 재시작 생략\n"
        f"- total: {fmt_sec(perf_now() - t0)}\n\n"
        f"[적용 결과]\n{result_text}\n\n"
        f"다음 확인:\n"
        f"- 가드봇: /gdeploy\n"
        f"- 메인봇: /health /errorlog\n"
        f"- 페이퍼봇: /pstatus /perror\n"
        f"- 외부직원: /gexternal_state"
    )


def handle_single_command(text: str) -> str:
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    args = parts[1:]

    if cmd in ("/gmenu_sync", "/gsync_menu", "/gcommands", "/gcommand_menu"):
        return sync_guard_menu_text()
    if cmd in ("/start", "/help", "/gmenu", "/menu"):
        return help_text()
    if cmd in ("/guard", "/status", "/check"):
        return service_status()
    if cmd in ("/gdeploy", "/version", "/upgradestatus"):
        return deploy_status()
    if cmd in ("/glog", "/lastlog"):
        n = 80
        if args and args[0].isdigit():
            n = max(20, min(300, int(args[0])))
        return glog(n)
    if cmd in ("/grestart", "/restart"):
        return restart_service()
    if cmd in ("/gpaper_reset", "/gpaperreset", "/gpaper_relink", "/gpaper_reconnect"):
        return gpaper_reset_text()
    if cmd in ("/gpaper_restart", "/gpaperrestart"):
        return gpaper_restart_text()
    if cmd in ("/gpaperlog", "/paperlog"):
        n = 80
        if args and args[0].isdigit():
            n = max(20, min(300, int(args[0])))
        return gpaper_log(n)
    if cmd in ("/gpaper_state", "/gpaperstate"):
        return gpaper_state_text()
    if cmd in ("/gpaper_service", "/gpaperservice"):
        return gpaper_service_text()
    if cmd in ("/gexternal_state", "/gexternal", "/gext_state"):
        return gexternal_state_text()
    if cmd in ("/gws_state", "/gwsstate"):
        return gws_state_text()
    if cmd in ("/gws_start", "/gwsstart"):
        return gws_start_text()
    if cmd in ("/gws_stop", "/gwsstop"):
        return gws_stop_text()
    if cmd in ("/gws_restart", "/gwsrestart"):
        return gws_restart_text()
    if cmd in ("/gupgrade_menu", "/gup_menu", "/upgrade_menu"):
        return gupgrade_menu_text()
    if cmd in ("/gupgrade_main", "/gmain_upgrade", "/gmain_apply"):
        force, explicit = parse_upgrade_force_explicit(args)
        return gupgrade_component_text("main", force=force, explicit=explicit)
    if cmd in ("/gupgrade_paper", "/gpaper_upgrade", "/gpaper_apply"):
        force, explicit = parse_upgrade_force_explicit(args)
        return gupgrade_component_text("paper", force=force, explicit=explicit)
    if cmd in ("/gupgrade_ws", "/gws_upgrade", "/gwsupgrade", "/gws_apply"):
        force, explicit = parse_upgrade_force_explicit(args)
        return gupgrade_component_text("ws", force=force, explicit=explicit)
    if cmd in ("/gwslog", "/gws_log"):
        n = 80
        if args and args[0].isdigit():
            n = max(20, min(300, int(args[0])))
        return gws_log(n)
    if cmd in ("/gws_import", "/gwsimport"):
        ok, notes = ws_import_check()
        icon = "✅" if ok else "❌"
        return "\n".join([f"{icon} 웹소켓 import 검사 /gws_import"] + [f"- {x}" for x in notes])
    if cmd in ("/gmicro_state", "/gmicrostate"):
        return gmicro_state_text()
    if cmd in ("/gmicro_start", "/gmicrostart"):
        return gmicro_start_text()
    if cmd in ("/gmicro_stop", "/gmicrostop"):
        return gmicro_stop_text()
    if cmd in ("/gmicro_restart", "/gmicrorestart"):
        return gmicro_restart_text()
    if cmd in ("/gupgrade_micro", "/gmicro_upgrade", "/gmicroupgrade", "/gmicro_apply"):
        force, explicit = parse_upgrade_force_explicit(args)
        return gupgrade_component_text("micro", force=force, explicit=explicit)
    if cmd in ("/gmicrolog", "/gmicro_log"):
        n = 80
        if args and args[0].isdigit():
            n = max(20, min(300, int(args[0])))
        return gmicro_log(n)
    if cmd in ("/gupgrade_guard", "/gguard_upgrade", "/guard_upgrade"):
        force, explicit = parse_upgrade_force_explicit(args)
        return gupgrade_component_text("guard", force=force, explicit=explicit)
    if cmd in ("/gdeep_audit", "/gdisk_audit", "/gworker_audit", "/gruntime_audit", "/gaudit"):
        sec = 20
        if args and str(args[0]).isdigit():
            sec = max(5, min(30, int(args[0])))
        return gdeep_audit_text(sample_sec=sec)
    if cmd in ("/gstorage_fix", "/gstorage_cleanup", "/gops_cleanup"):
        dry = bool(args and str(args[0]).lower() in {"dry","preview","check"})
        return gstorage_fix_text(dry_run=dry)
    if cmd in ("/gworker_state", "/gworkers", "/gworkerstate"):
        return gworker_state_text()
    if cmd in ("/gscanner_state", "/gscanner"):
        return gworker_state_text("scanner")
    if cmd in ("/gcandle_state", "/gcandle"):
        return gworker_state_text("candle")
    if cmd in ("/gmarket_state", "/gmarket", "/gregime_state"):
        return gworker_state_text("market")
    if cmd in ("/gfeature_state", "/gfeature"):
        return gworker_state_text("feature")
    if cmd in ("/gorderflow_state", "/gorderflow"):
        return gworker_state_text("orderflow")
    if cmd in ("/grisk_state", "/grisk"):
        return gworker_state_text("risk")
    if cmd in ("/gstrategy_state", "/gstrategy"):
        return gworker_state_text("strategy")
    if cmd in ("/gtarget_state", "/gtarget", "/gtarget_router_state"):
        return gworker_state_text("target_router")
    if cmd in ("/greview_state", "/greview"):
        return gworker_state_text("review")
    if cmd in ("/gstrategy_upgrade", "/gupgrade_strategy"):
        force, explicit = parse_upgrade_force_explicit(args)
        return format_worker_apply_result(apply_worker_latest("strategy", force=force, explicit=explicit, restart=True))
    if cmd in ("/gorderflow_upgrade", "/gupgrade_orderflow"):
        force, explicit = parse_upgrade_force_explicit(args)
        return format_worker_apply_result(apply_worker_latest("orderflow", force=force, explicit=explicit, restart=True))
    if cmd in ("/gtarget_upgrade", "/gupgrade_target", "/gtarget_router_upgrade"):
        force, explicit = parse_upgrade_force_explicit(args)
        return format_worker_apply_result(apply_worker_latest("target_router", force=force, explicit=explicit, restart=True))
    if cmd in ("/gworker_upgrade", "/gupgrade_worker", "/gupgrade_workers"):
        force, explicit = parse_upgrade_force_explicit(args)
        return gworker_upgrade_text(force=force)
    if cmd in ("/gworker_restart", "/gworkers_restart"):
        return gworker_restart_text()
    if cmd in ("/gscanner_restart",):
        return gworker_restart_text("scanner")
    if cmd in ("/gcandle_restart",):
        return gworker_restart_text("candle")
    if cmd in ("/gmarket_restart",):
        return gworker_restart_text("market")
    if cmd in ("/gfeature_restart",):
        return gworker_restart_text("feature")
    if cmd in ("/gorderflow_restart",):
        return gworker_restart_text("orderflow")
    if cmd in ("/grisk_restart",):
        return gworker_restart_text("risk")
    if cmd in ("/gstrategy_restart",):
        return gworker_restart_text("strategy")
    if cmd in ("/greview_restart",):
        return gworker_restart_text("review")
    if cmd in ("/gworkerlog", "/gworker_log"):
        name = args[0].lower() if args else "strategy"
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 80
        return gworker_log_text(name, n)
    if cmd in ("/gguard_restart", "/guard_restart"):
        rc, out = run(["systemctl", "restart", GUARD_SERVICE], timeout=30)
        return f"🔁 가드봇 재시작 요청\n- rc: {rc}\n- service: {GUARD_SERVICE}\n- 다음 확인: /guard"
    if cmd in ("/gunlock", "/unlock"):
        return unlock_deploy()
    if cmd in ("/gbackups", "/backups"):
        return list_backups_text()
    if cmd in ("/grollback", "/rollback"):
        return rollback_latest()
    if cmd in ("/gcleanup", "/cleanup", "/gdisk_cleanup"):
        return cleanup_text(dry_run=False)
    if cmd in ("/grelease_verify", "/gverify_release", "/gverify"):
        return grelease_verify_text()
    if cmd in ("/grelease_last", "/glast_release"):
        return grelease_last_text()
    if cmd in ("/gupgrade_bundle", "/gbundle", "/gapply_bundle"):
        force, explicit = parse_upgrade_force_explicit(args)
        lowered_args = {str(a).lower() for a in args}
        skip_git = bool(lowered_args & {"nofetch", "local", "skipgit", "skip_git"})
        return gupgrade_bundle_text(force=force, explicit=explicit, skip_git=skip_git)
    if cmd in ("/gupgrade", "/gupgrade_all", "/apply_latest", "/upgradebot"):
        force, explicit = parse_upgrade_force_explicit(args)
        lowered_args = {str(a).lower() for a in args}
        skip_git = bool(lowered_args & {"nofetch", "local", "skipgit", "skip_git"})
        # v2.5.65: /gupgrade도 /gupgrade_bundle와 같은 단일 bundle 본선만 탄다.
        # repo 전체 git fetch/reset 경로와 stale/local fallback 검문은 기본 적용 경로에서 제거한다.
        return gupgrade_bundle_text(force=force, explicit=explicit, skip_git=skip_git)
    return "알 수 없는 명령어야.\n\n" + help_text()


def handle_command(text: str) -> str:
    lines = _extract_guard_command_lines(text)
    if len(lines) <= 1:
        return handle_single_command(text)
    out = ["📦 자동 묶음 명령 접수", "- /g배치 없이 여러 줄 명령을 감지", f"- 실행 {len(lines)}개"]
    total = len(lines)
    for idx, line in enumerate(lines, start=1):
        name = _guard_command_name(line)
        try:
            if name in {"/gupgrade", "/gupgrade_all", "/apply_latest", "/upgradebot"}:
                body = "안전상 자동묶음에서는 /gupgrade는 따로 실행해줘."
            else:
                body = handle_single_command(line)
        except Exception as exc:
            body = f"오류: {exc.__class__.__name__}: {exc}"
        out.append(f"\n[{idx}/{total}] {name}\n{body}")
    return "\n".join(out)


def main():
    print(f"[{now()}] guard bot started. service={MAIN_SERVICE} dir={BOT_DIR} version={VERSION}", flush=True)
    offset = load_guard_offset()
    offset = init_guard_offset_if_empty(offset)
    last_active = None
    last_notify = 0
    main_active_timeout_count = 0
    last_timeout_notify = 0
    set_commands()
    release_job = _load_release_job()
    if release_job:
        time.sleep(1.5)
        resume_guard_release_job()
    else:
        send_guard_start_notice()

    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            data = api("getUpdates", params, timeout=35)

            for upd in data.get("result", []):
                offset = upd.get("update_id", 0) + 1
                save_guard_offset(offset, "before_handle_update")
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat_id != ALLOWED_CHAT_ID:
                    if text:
                        send(chat_id, "허용되지 않은 사용자입니다.")
                    continue
                if text.startswith("/"):
                    first = text.strip().split()[0].lower() if text.strip().split() else ""
                    if first in ("/gupgrade", "/gupgrade_all", "/apply_latest", "/upgradebot"):
                        send(chat_id, "🚀 /gupgrade 접수\n- 순서: 가드봇 → 메인봇 → 페이퍼봇 → WS → micro\n- 최신 hash 동일 항목은 교체/재시작 생략\n- 진행 단계는 중간 메시지로 계속 알림")
                    elif first in ("/gguard_upgrade", "/guard_upgrade"):
                        send(chat_id, "🛡 /gguard_upgrade 접수\n- 가드봇 자체 최신화 확인 중\n- hash 동일이면 재시작 생략")
                    elif first in ("/gupgrade_ws", "/gws_upgrade", "/gwsupgrade", "/gws_apply"):
                        send(chat_id, "🛰 /gws_upgrade 접수\n- WS sidecar 최신화 확인 중\n- hash 동일 + 실행중이면 재시작 생략")
                    elif first in ("/gupgrade_micro", "/gmicro_upgrade", "/gmicroupgrade", "/gmicro_apply"):
                        send(chat_id, "🔎 /gmicro_upgrade 접수\n- 호가·체결 sidecar 최신화 확인 중\n- hash 동일 + 실행중이면 재시작 생략")
                    elif first in ("/gdeep_audit", "/gdisk_audit", "/gworker_audit", "/gruntime_audit", "/gaudit"):
                        send(chat_id, "🔬 통합감사 접수\n- 작업자 cycle/CPU/메모리/중복 프로세스 확인\n- 디스크 큰 파일/journal/삭제 후 열린 파일 확인\n- 20초 증가량 표본 측정\n- 읽기 전용, 장부·매매코드 변경 없음")
                    reply = handle_command(text)
                    send(chat_id, reply)

            active = is_main_service_active()
            if is_status_timeout_value(active):
                main_active_timeout_count += 1
                # v2.5.67: systemctl timeout은 실제 active 상태가 아니다.
                # TIMEOUT을 last_active로 저장하면 active ↔ TIMEOUT 알림이 반복된다.
                # 마지막 정상 active 값은 유지하고, 연속 timeout 때만 별도 경고를 보낸다.
                if main_active_timeout_count >= 3 and time.time() - last_timeout_notify > 180:
                    last_timeout_notify = time.time()
                    send(ALLOWED_CHAT_ID,
                         "⚠️ 메인봇 상태확인 지연\n"
                         f"- 마지막 정상상태: {last_active or '?'}\n"
                         f"- systemctl timeout: {main_active_timeout_count}회 연속\n"
                         "- 상태변경 알림이 아니라 가드봇 확인 지연입니다.\n"
                         "- 확인: 메인봇 /health 또는 가드봇 /gdeploy")
            else:
                main_active_timeout_count = 0
                if last_active is None:
                    last_active = active
                elif active != last_active:
                    last_active = active
                    if time.time() - last_notify > 20:
                        last_notify = time.time()
                        # v2.5.7: 상태변경 알림에서 /glog를 자동 호출하지 않는다.
                        # journalctl 지연이 가드봇 polling을 막고 재시작처럼 보이는 문제를 차단한다.
                        # v2.5.67: TIMEOUT은 위에서 별도 처리하므로 여기서는 실제 active 값만 알린다.
                        send(ALLOWED_CHAT_ID, f"❔ 메인봇 상태 변경\n- active: {active}\n- 시각: {now()}\n- 로그 확인: /glog 80")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{now()}] loop error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(3)
        time.sleep(POLL_SEC)


# =============================================================================
# v2.5.60 / v398: paper stale fast-skip 제거 + systemd 단일 재시작
# - hash 동일 + active여도 status/trace/version/age/pid가 이상하면 paper만 재시작한다.
# - direct fallback으로 새 paper를 띄우지 않는다. service가 없으면 파일 교체 후 실행 보류한다.
# - OPEN/CLOSED/trade_log 장부는 건드리지 않는다.
# =============================================================================


def _v398_guard_file_age(path: Path) -> float:
    try:
        return max(0.0, time.time() - path.stat().st_mtime) if path.exists() else 999999.0
    except Exception:
        return 999999.0


# =============================================================================
# guard v2.5.61 / v399: paper service hard reset + fast upgrade cleanup
# - paper 코드에 루프/wrapper를 더 붙이지 않는다.
# - stale status/trace/pid와 구 service 연결만 백업 후 제거하고 systemd 본선으로 다시 잇는다.
# - OPEN/CLOSED/trade_log 장부와 전략/청산/worker는 건드리지 않는다.
# =============================================================================

def _v399_expected_paper_version(target: Path) -> str:
    try:
        v = extract_paper_version(target)
        return v if v and v != '?' else str(paper_version_from_filename(target) or '?')
    except Exception:
        return '?'


def paper_runtime_snapshot(expected_version: str = '', expected_build: str = '', expected_hash: str = '') -> dict:
    """One read-only runtime truth: systemd MainPID -> cmdline -> status identity -> alias hash."""
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    status_path = BOT_DIR / 'paper_bot_status.json'
    phase_path = BOT_DIR / 'paper_bot_runtime_phase_v994.json'
    status = read_json(status_path)
    phase = read_json(phase_path)
    phase = phase if isinstance(phase, dict) else {}
    status = status if isinstance(status, dict) else {}
    pid_file = read_pid(BOT_DIR / PAPER_PID_FILE)
    service_main_pid = _service_main_pid(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else None
    proc_pids = find_paperbot_pids()
    try:
        status_pid = int(float(status.get('writer_pid') or status.get('status_writer_pid') or status.get('self_pid') or status.get('pid') or status.get('process_pid') or 0))
    except Exception:
        status_pid = 0
    try:
        writer_count = int(float(status.get('status_writer_count') or 0))
    except Exception:
        writer_count = 0
    active_version = extract_paper_version(active_path)
    active_build = extract_assignment(active_path, 'BUILD_LABEL')
    source_owner = extract_assignment(active_path, '_STATUS_WRITER_OWNER')
    active_hash = short_hash(active_path) if active_path.exists() else '?'
    expected_version = expected_version or (active_version if active_version != '?' else '')
    expected_build = expected_build or (active_build if active_build != '?' else '')
    expected_hash = expected_hash or (active_hash if active_hash != '?' else '')
    expected_owner = source_owner if source_owner != '?' else ''
    age = _v398_guard_file_age(status_path)
    stop_reason = str(status.get('stop_reason') or '').strip()
    running = status.get('running') is True
    service_active = is_service_active(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else 'no_service'
    main_alive = bool(service_main_pid and pid_alive(service_main_pid))
    cmdline = proc_cmdline(service_main_pid) if main_alive else ''
    extras = [pid for pid in proc_pids if pid != service_main_pid and pid_alive(pid)]
    reasons: list[str] = []
    if not service_exists(PAPER_SERVICE): reasons.append('systemd service 없음')
    elif service_active not in {'active','activating'}: reasons.append(f'service={service_active}')
    if not main_alive: reasons.append('systemd MainPID 없음/죽음')
    if main_alive and 'paper_bot.py' not in cmdline: reasons.append('MainPID cmdline이 paper_bot.py 아님')
    if extras: reasons.append('중복 paper process=' + ','.join(map(str, extras)))
    if service_main_pid and pid_file != service_main_pid: reasons.append(f'pid_file 불일치 {pid_file}->{service_main_pid}')
    if service_main_pid and status_pid != service_main_pid: reasons.append(f'status_pid 불일치 {status_pid}->{service_main_pid}')
    if age > 45: reasons.append(f'status age {age:.1f}s')
    if expected_version and str(status.get('version') or status.get('paper_version') or '') != expected_version:
        reasons.append(f"status VERSION 불일치 {status.get('version') or status.get('paper_version') or '-'}->{expected_version}")
    if expected_build and str(status.get('build') or status.get('paper_runtime_build') or '') != expected_build:
        reasons.append(f"status BUILD 불일치 {status.get('build') or status.get('paper_runtime_build') or '-'}->{expected_build}")
    if expected_hash and active_hash != expected_hash: reasons.append(f'active hash 불일치 {active_hash}->{expected_hash}')
    if expected_owner and str(status.get('status_writer_owner') or '') != expected_owner:
        reasons.append(f"writer owner 불일치 {status.get('status_writer_owner') or '-'}->{expected_owner}")
    if writer_count != 1: reasons.append(f'writer_count={writer_count}, expected=1')
    try:
        writer_pid_num = int(float(status.get('writer_pid') or 0))
    except Exception:
        writer_pid_num = 0
    if service_main_pid and writer_pid_num != service_main_pid:
        reasons.append(f"writer_pid 불일치 {writer_pid_num}->{service_main_pid}")
    if not running: reasons.append('status running!=True')
    if stop_reason not in {'','running'}: reasons.append(f'stale stop_reason={stop_reason}')
    if not status.get('process_started_at'): reasons.append('process_started_at 누락')
    return {
        'ok': not reasons,
        'reasons': reasons,
        'service': PAPER_SERVICE,
        'service_active': service_active,
        'main_pid': service_main_pid,
        'main_alive': main_alive,
        'cmdline': cmdline,
        'proc_pids': proc_pids,
        'extra_pids': extras,
        'pid_file': pid_file,
        'status_pid': status_pid,
        'writer_pid': status.get('writer_pid'),
        'writer_owner': status.get('status_writer_owner'),
        'writer_count': writer_count,
        'status_age': age,
        'status_version': status.get('version') or status.get('paper_version'),
        'status_build': status.get('build') or status.get('paper_runtime_build'),
        'running': status.get('running'),
        'stop_reason': stop_reason,
        'process_started_at': status.get('process_started_at'),
        'runtime_instance_id': status.get('runtime_instance_id'),
        'write_seq': status.get('status_write_seq'),
        'active_file': active_path.name,
        'active_version': active_version,
        'active_build': active_build,
        'active_hash': active_hash,
        'expected_owner': expected_owner,
        'runtime_phase': phase.get('phase') or status.get('runtime_phase'),
        'runtime_phase_detail': phase.get('detail') or status.get('runtime_phase_detail'),
        'runtime_phase_age': _v398_guard_file_age(phase_path),
        'exact_commit_active': phase.get('exact_commit_active') if phase else status.get('exact_commit_active'),
    }


def _v399_paper_stale_reasons(target: Path, active_path: Path) -> list[str]:
    expected_version = _v399_expected_paper_version(target)
    expected_build = extract_assignment(target, 'BUILD_LABEL')
    expected_hash = short_hash(target) if target.exists() else ''
    snap = paper_runtime_snapshot(expected_version, expected_build if expected_build != '?' else '', expected_hash)
    reasons = list(snap.get('reasons') or [])
    trace = read_json(BOT_DIR / 'paper_bot_candidate_handoff_trace.json')
    trace_ver = str(trace.get('version') or trace.get('trace_version') or '') if isinstance(trace, dict) else ''
    trace_age = _v398_guard_file_age(BOT_DIR / 'paper_bot_candidate_handoff_trace.json')
    if expected_version != '?' and trace_ver and trace_ver != expected_version:
        reasons.append(f'trace VERSION 불일치 {trace_ver}->{expected_version}')
    if trace_age > 150:
        reasons.append(f'trace age {trace_age:.0f}s')
    return reasons


def _v399_paper_unit_content() -> str:
    home = _guess_bot_owner_home() or str(BOT_DIR.parent)
    user = _guess_bot_user()
    user_line = f"User={user}\n" if user and user != 'root' else ''
    py = PAPER_PYTHON_BIN or '/usr/bin/python3'
    return f"""[Unit]
Description=Coinbot Paper Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{user_line}WorkingDirectory={BOT_DIR}
EnvironmentFile=-{ENV_PATH}
Environment=TRADING_BOT_DIR={BOT_DIR}
Environment=HOME={home}
ExecStart={py} {BOT_DIR / PAPER_ACTIVE_FILE} --bot
Restart=always
RestartSec=3
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=8

[Install]
WantedBy=multi-user.target
"""


def _v399_install_paper_service(force: bool = True) -> tuple[bool, list[str]]:
    notes=[]
    unit_path = Path(f"/etc/systemd/system/{PAPER_SERVICE}.service")
    content = _v399_paper_unit_content()
    existing = ''
    try:
        existing = unit_path.read_text(encoding='utf-8', errors='ignore') if unit_path.exists() else ''
    except Exception:
        existing = ''
    if service_exists(PAPER_SERVICE) and existing.strip() == content.strip() and not force:
        return True, [f'service already installed: {PAPER_SERVICE}']
    tmp = BOT_DIR / f'.{PAPER_SERVICE}.service.tmp'
    try:
        tmp.write_text(content, encoding='utf-8')
    except Exception as exc:
        return False, [f'unit tmp write failed: {exc.__class__.__name__}: {str(exc)[:140]}']
    try:
        if os.geteuid() == 0:
            unit_path.write_text(content, encoding='utf-8')
            rc_cp, out_cp = 0, ''
        else:
            rc_cp, out_cp = _run_privileged(['cp', str(tmp), str(unit_path)], timeout=10)
        if rc_cp != 0:
            return False, ['unit install failed: ' + tail(out_cp, 600)]
        _run_privileged(['chmod', '0644', str(unit_path)], timeout=8)
        rc_daemon, out_daemon = _run_privileged(['systemctl', 'daemon-reload'], timeout=15)
        rc_enable, out_enable = _run_privileged(['systemctl', 'enable', PAPER_SERVICE], timeout=15)
        notes.append(f'service reinstalled: {PAPER_SERVICE}')
        if rc_daemon != 0:
            notes.append('daemon-reload: ' + tail(out_daemon, 300))
        if rc_enable != 0:
            notes.append('enable: ' + tail(out_enable, 300))
        return rc_cp == 0, notes
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass


def _v399_stop_paper_runtime() -> list[str]:
    notes=[]
    if service_exists(PAPER_SERVICE):
        rc, out = run(['systemctl', 'stop', PAPER_SERVICE], timeout=12)
        notes.append(f'systemctl stop {PAPER_SERVICE}: rc={rc} {tail(out,300)}')
        if rc == 124 or is_service_active(PAPER_SERVICE) in {'active','activating','deactivating'}:
            rc2, out2 = run(['systemctl', 'kill', '-s', 'SIGKILL', PAPER_SERVICE], timeout=8)
            notes.append(f'systemctl kill {PAPER_SERVICE}: rc={rc2} {tail(out2,300)}')
    for pid in list(find_paperbot_pids()):
        if pid_alive(pid):
            stop_pid(pid)
            notes.append(f'paper 잔류 pid 종료: {pid}')
    return notes


def _v399_backup_and_remove_paper_runtime_files() -> tuple[str, list[str]]:
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = BOT_DIR / f'.paper_runtime_reset_{stamp}'
    backup_dir.mkdir(exist_ok=True)
    names = [
        PAPER_PID_FILE,
        'paper_bot_status.json',
        'paper_bot_candidate_handoff_trace.json',
        'paper_bot_candidate_handoff_status.json',
        'paper_bot_current_score_anchor.json',
        'paper_bot_score_cache.json',
    ]
    notes=[]
    for name in names:
        p = BOT_DIR / name
        if not p.exists():
            continue
        try:
            shutil.copy2(p, backup_dir / name)
            p.unlink()
            notes.append(f'runtime 캐시 백업 후 제거: {name}')
        except Exception as exc:
            notes.append(f'runtime 캐시 제거 실패: {name} / {exc.__class__.__name__}: {str(exc)[:120]}')
    notes.append('보존: paper_bot_open.json / paper_bot_closed.jsonl / trade_log 계열은 삭제하지 않음')
    return backup_dir.name, notes


def _v399_copy_paper_target_to_active(target: Path, force: bool = True) -> tuple[bool, list[str]]:
    notes=[]
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    valid, checks, warnings = validate_paper_target(target)
    notes += checks[:4] + [f'warning: {w}' for w in warnings[:4]]
    if not valid:
        return False, notes
    if active_path.exists():
        backup_path = BOT_DIR / f"{active_path.name}.backup_v399_reset_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(active_path, backup_path)
        notes.append(f'active backup: {backup_path.name}')
    tmp = BOT_DIR / f"{active_path.name}.v399_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(target, tmp)
    if sha256_file(tmp) != sha256_file(target):
        tmp.unlink(missing_ok=True)
        return False, notes + ['paper active copy hash mismatch']
    os.replace(tmp, active_path)
    try:
        (BOT_DIR / DEPLOYED_PAPER_FILE).write_text(target.name + '\n', encoding='utf-8')
    except Exception:
        pass
    notes.append(f'active paper relinked: {active_path.name} <= {target.name}')
    return True, notes


def _v399_wait_paper_fresh(expected: str, timeout_sec: float = 60.0, expected_build: str = '', expected_hash: str = '', expected_pid: Optional[int] = None) -> tuple[bool, list[str]]:
    notes=[]
    deadline=time.time()+max(1.0, timeout_sec)
    last={}
    while time.time() < deadline:
        last = paper_runtime_snapshot(expected, expected_build, expected_hash)
        if last.get('ok') and (not expected_pid or int(last.get('main_pid') or 0) == int(expected_pid)):
            notes.append(
                f"fresh exact: pid {last.get('main_pid')} / status {last.get('status_version')} / "
                f"owner {last.get('writer_owner')} / count {last.get('writer_count')} / age {float(last.get('status_age') or 0):.1f}s"
            )
            return True, notes
        time.sleep(1)
    notes.append('fresh exact timeout: ' + ' / '.join((last.get('reasons') or ['status 없음'])[:10]))
    return False, notes


def gpaper_reset_text() -> str:
    """paper 실행 연결부만 끊고 다시 잇는다. 장부는 보존한다."""
    t0=perf_now()
    target = choose_latest_paper_target()
    if not target or not target.exists():
        return '🧯 ❌ 페이퍼 재연결 실패 /gpaper_reset\n- paper_bot_v*.py 대상 파일을 못 찾음'
    expected = _v399_expected_paper_version(target)
    notes=[]
    notes.append(f'target: {target.name} / expected {expected}')
    ok_unit, unit_notes = _v399_install_paper_service(force=True)
    notes += unit_notes
    if not ok_unit:
        return '🧯 ❌ 페이퍼 재연결 실패 /gpaper_reset\n- service unit 설치 실패\n' + '\n'.join(f'- {x}' for x in notes[-12:])
    notes += _v399_stop_paper_runtime()
    backup_dir, backup_notes = _v399_backup_and_remove_paper_runtime_files()
    notes += backup_notes
    ok_copy, copy_notes = _v399_copy_paper_target_to_active(target, force=True)
    notes += copy_notes
    if not ok_copy:
        return '🧯 ❌ 페이퍼 재연결 실패 /gpaper_reset\n- active paper 복사 실패\n' + '\n'.join(f'- {x}' for x in notes[-16:])
    rc, out = run(['systemctl', 'start', PAPER_SERVICE], timeout=15)
    notes.append(f'systemctl start {PAPER_SERVICE}: rc={rc} {tail(out,300)}')
    time.sleep(1)
    pid = _service_main_pid(PAPER_SERVICE)
    if pid:
        repair_paper_pid_file(pid)
        notes.append(f'service pid={pid}')
    active = is_service_active(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else 'no_service'
    fresh_ok, fresh_notes = _v399_wait_paper_fresh(expected, timeout_sec=60.0, expected_build=extract_assignment(target, 'BUILD_LABEL'), expected_hash=short_hash(target), expected_pid=_service_main_pid(PAPER_SERVICE))
    notes += fresh_notes
    ok = (rc == 0 and active in {'active','activating'} and fresh_ok)
    title = '🧯 ✅ 페이퍼 재연결 완료 /gpaper_reset' if ok else '🧯 ⚠️ 페이퍼 재연결 확인필요 /gpaper_reset'
    lines=[title, f'- target: {target.name}', f'- expected: {expected}', f'- service: {PAPER_SERVICE} / active={active}', f'- backup_runtime: {backup_dir}', f'- total: {fmt_sec(perf_now()-t0)}', '', '[조치]']
    lines += [f'- {x}' for x in notes[:24]]
    lines += ['', '[보존]', '- paper_bot_open.json / paper_bot_closed.jsonl / trade_log 계열은 건드리지 않음', '', '다음 확인:', '- /gpaper_state', '- /gpaperlog 120', '- 메인봇 /health /pstatus /errorlog']
    return '\n'.join(lines)


def restart_paper_bot(expected_version: str = '', expected_build: str = '', expected_hash: str = '') -> tuple[str, list[str]]:
    """One systemd restart path followed by exact source/status identity verification."""
    notes=[]
    if not service_exists(PAPER_SERVICE):
        ok_unit, unit_notes = _v399_install_paper_service(force=True)
        notes += unit_notes
        if not ok_unit:
            notes.append(f'systemd service 없음({PAPER_SERVICE}); direct fallback 금지')
            return 'no_service', notes
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    expected_version = expected_version or extract_paper_version(active_path)
    build = expected_build or extract_assignment(active_path, 'BUILD_LABEL')
    expected_build = '' if build == '?' else build
    expected_hash = expected_hash or (short_hash(active_path) if active_path.exists() else '')
    rc, out = run(['systemctl', 'restart', PAPER_SERVICE], timeout=25)
    notes.append(f'systemctl restart {PAPER_SERVICE}: rc={rc} {tail(out,500)}')
    if rc != 0:
        return 'restart_failed', notes
    deadline=time.time()+8
    pid=None
    while time.time()<deadline:
        pid=_service_main_pid(PAPER_SERVICE)
        if pid and pid_alive(pid): break
        time.sleep(0.5)
    if pid:
        repair_paper_pid_file(pid)
        notes.append(f'MainPID={pid} / pid_file synchronized')
    extras=[p for p in find_paperbot_pids() if p != pid and pid_alive(p)]
    for extra in extras:
        stop_pid(extra)
        notes.append(f'stale paper pid stopped={extra}')
    permission_ok, permission_notes = repair_paper_canonical_writer_ownership()
    notes += permission_notes
    if not permission_ok:
        run(['systemctl','stop',PAPER_SERVICE], timeout=12)
        notes.append('canonical writer ownership failed -> paper stopped; alternate ledger forbidden')
        return 'permission_failed', notes
    fresh_ok, fresh_notes = _v399_wait_paper_fresh(expected_version, timeout_sec=60.0, expected_build=expected_build, expected_hash=expected_hash, expected_pid=pid)
    notes += fresh_notes
    active=is_service_active(PAPER_SERVICE)
    if not fresh_ok:
        notes.append('service active만으로 성공 처리하지 않음')
        return 'verification_failed', notes
    return active, notes


def _paper_identity_from_pid(pid: int) -> Optional[dict]:
    """Return the actual OS identity of a live paper process."""
    try:
        status = Path(f'/proc/{int(pid)}/status').read_text(encoding='utf-8', errors='ignore')
        uid_line = next((line for line in status.splitlines() if line.startswith('Uid:')), '')
        gid_line = next((line for line in status.splitlines() if line.startswith('Gid:')), '')
        uid = int(uid_line.split()[1]); gid = int(gid_line.split()[1])
        try: user = pwd.getpwuid(uid).pw_name
        except Exception: user = str(uid)
        return {'uid':uid,'gid':gid,'user':user,'pid':int(pid),'source':'paper_main_pid'}
    except Exception:
        return None


def resolve_paper_runtime_identity() -> tuple[Optional[dict], list[str]]:
    """Resolve the exact user that executes paper_bot.py; never infer it from BOT_DIR owner."""
    notes: list[str] = []
    main_pid = _service_main_pid(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else None
    if main_pid and pid_alive(main_pid):
        ident = _paper_identity_from_pid(main_pid)
        if ident:
            notes.append(f"identity pid={main_pid} user={ident['user']} uid={ident['uid']} gid={ident['gid']}")
            return ident, notes
    for pid in reversed(find_paperbot_pids()):
        if pid_alive(pid):
            ident = _paper_identity_from_pid(pid)
            if ident:
                ident['source'] = 'paper_process_scan'
                notes.append(f"identity scan pid={pid} user={ident['user']} uid={ident['uid']} gid={ident['gid']}")
                return ident, notes
    service_user = ''
    if service_exists(PAPER_SERVICE):
        rc, out = run(['systemctl','show',PAPER_SERVICE,'-p','User','--value'], timeout=8)
        if rc == 0:
            service_user = str(out or '').strip()
    user = service_user or _guess_bot_user()
    if user:
        try:
            rec = pwd.getpwnam(user)
            ident = {'uid':int(rec.pw_uid),'gid':int(rec.pw_gid),'user':rec.pw_name,'pid':None,
                     'source':'systemd_user' if service_user else 'bot_path_user'}
            notes.append(f"identity {ident['source']} user={ident['user']} uid={ident['uid']} gid={ident['gid']}")
            return ident, notes
        except Exception as exc:
            notes.append(f"identity user lookup failed {user}: {type(exc).__name__}: {exc}")
    notes.append('paper runtime identity unresolved')
    return None, notes


def repair_paper_canonical_writer_ownership() -> tuple[bool, list[str]]:
    """Make canonical transaction files owned by the actual paper runtime identity."""
    identity, notes = resolve_paper_runtime_identity()
    if not identity:
        return False, notes
    uid = int(identity['uid']); gid = int(identity['gid']); owner = f'{uid}:{gid}'
    names = [
        'paper_bot_closed.jsonl', 'paper_bot_open.json',
        'paper_closed_append_status_v925.json', 'paper_closed_append_events_v925.jsonl',
        'paper_closed_commit_status.json', 'paper_closed_commit_events.jsonl',
        'paper_decision_state_v926.json', 'paper_decision_status_v926.json', 'paper_decision_events_v926.jsonl',
        'paper_decision_reconcile_status.json',
        'paper_exit_monitor_status.json', 'paper_exit_monitor_events.jsonl',
        'paper_writer_permission_status.json',
    ]
    ok = True
    changed = 0
    for name in names:
        path = BOT_DIR / name
        if not path.exists():
            notes.append(f'not_exists {name}')
            continue
        try:
            st = path.stat()
            if int(st.st_uid) != uid or int(st.st_gid) != gid:
                rc, out = _run_privileged(['chown', owner, str(path)], timeout=8)
                notes.append(f'chown {name} -> {identity["user"]}({owner}): rc={rc} {tail(out,160)}')
                ok = ok and rc == 0
                changed += 1 if rc == 0 else 0
            st = path.stat()
            if not bool(st.st_mode & 0o200):
                rc, out = _run_privileged(['chmod','0664',str(path)], timeout=8)
                notes.append(f'chmod {name} 0664: rc={rc} {tail(out,160)}')
                ok = ok and rc == 0
                changed += 1 if rc == 0 else 0
            final = path.stat()
            final_ok = int(final.st_uid) == uid and int(final.st_gid) == gid and bool(final.st_mode & 0o200)
            if not final_ok:
                ok = False
                notes.append(f'verify_fail {name}: uid={final.st_uid} gid={final.st_gid} mode={oct(final.st_mode & 0o777)} expected={owner}')
            else:
                notes.append(f'owner_ok {name}: user={identity["user"]} uid={final.st_uid} gid={final.st_gid} mode={oct(final.st_mode & 0o777)}')
        except Exception as exc:
            ok = False
            notes.append(f'permission_error {name}: {type(exc).__name__}: {exc}')
    notes.insert(1, f"contract actual paper identity only; BOT_DIR owner ignored; changed={changed}")
    return ok, notes

def apply_paper_latest(force: bool = False, explicit: Optional[str] = None) -> dict:
    target = choose_latest_paper_target(explicit=explicit)
    if not target or not target.exists():
        return {'ok': True, 'changed': False, 'target': explicit or '?', 'active': '유지', 'warning': 'paper_bot_v*.py 대상 없음 → 기존 실행 유지'}
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    valid, checks, warnings = validate_paper_target(target)
    if not valid:
        return {'ok': False, 'changed': False, 'target': target.name, 'checks': checks, 'warnings': warnings, 'error': 'paper 검수 실패'}
    permission_ok, permission_notes = repair_paper_canonical_writer_ownership()
    if not permission_ok:
        return {'ok': False, 'changed': False, 'target': target.name, 'checks': checks, 'warnings': warnings, 'error': 'paper canonical ledger 권한 복구 실패', 'notes': permission_notes}
    expected_version = _v399_expected_paper_version(target)
    expected_build = extract_assignment(target, 'BUILD_LABEL')
    expected_build = '' if expected_build == '?' else expected_build
    expected_hash = short_hash(target)
    same_hash = active_path.exists() and short_hash(active_path) == expected_hash
    stale_reasons = _v399_paper_stale_reasons(target, active_path) if same_hash else ['active source hash 변경 필요']
    backup='없음'
    changed=False
    if not same_hash or force:
        if active_path.exists():
            backup_path = BOT_DIR / f"{active_path.name}.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(active_path, backup_path)
            backup=backup_path.name
        tmp = BOT_DIR / f"{active_path.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target, tmp)
        if sha256_file(tmp) != sha256_file(target):
            tmp.unlink(missing_ok=True)
            return {'ok': False, 'changed': False, 'target': target.name, 'error': 'paper 임시복사 hash 불일치'}
        os.replace(tmp, active_path)
        changed=True
        try: (BOT_DIR / DEPLOYED_PAPER_FILE).write_text(target.name + '\n', encoding='utf-8')
        except Exception: pass
    if changed or stale_reasons or force:
        active, notes = restart_paper_bot(expected_version, expected_build, expected_hash)
        ok = active in {'active','activating'}
        return {'ok': ok, 'changed': changed, 'target': target.name, 'version': extract_paper_version(active_path), 'active': active,
                'backup': backup, 'hash': short_hash(active_path), 'notes': permission_notes + notes,
                'warnings': warnings + stale_reasons, 'error': None if ok else 'paper runtime exact verification failed'}
    snap=paper_runtime_snapshot(expected_version, expected_build, expected_hash)
    try: (BOT_DIR / DEPLOYED_PAPER_FILE).write_text(target.name + '\n', encoding='utf-8')
    except Exception: pass
    return {'ok': bool(snap.get('ok')), 'changed': False, 'target': target.name, 'version': extract_paper_version(active_path),
            'active': snap.get('service_active'), 'hash': short_hash(active_path), 'skipped_restart': True,
            'warning': '변경 없음 + exact runtime identity 정상 → 재시작 생략',
            'notes': permission_notes + [f"pid={snap.get('main_pid')} owner={snap.get('writer_owner')} count={snap.get('writer_count')}"],
            'warnings': warnings + list(snap.get('reasons') or [])}


if __name__ == "__main__":
    main()

# guard v2.5.62 / v420: bundle fetch timeout safe local gate + explicit zip parser
