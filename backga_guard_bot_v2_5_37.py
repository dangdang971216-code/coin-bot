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
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

ENV_PATH = Path(__file__).with_name("guard.env")
VERSION = "guard_v2.5.37_telegram_command_menu_sync_2026-05-19"
UPGRADE_STATUS_FILE = ".guard_upgrade_status.json"
GUARD_OFFSET_FILE = ".guard_update_offset.json"
GUARD_START_NOTICE_FILE = ".guard_start_notice.json"
GUARD_POST_UPGRADE_FILE = ".guard_post_upgrade_request.json"


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
        "MICRO_ACTIVE_FILE", "MICRO_PID_FILE", "MICRO_LOG_FILE", "MICRO_STATUS_FILE", "MICRO_CACHE_FILE", "MICRO_PYTHON_BIN", "WS_SERVICE", "MICRO_SERVICE"
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
RESTART_WAIT_SEC = int(ENV.get("GUARD_RESTART_WAIT_SEC", "45") or "45")
STATIC_ALIAS_CHECK_ENABLED = str(ENV.get("GUARD_STATIC_ALIAS_CHECK", "0")).strip().lower() in {"1", "true", "yes", "on"}
PAPER_SERVICE = ENV.get("PAPER_SERVICE", "tradingbot-paper")
PAPER_ACTIVE_FILE = ENV.get("PAPER_ACTIVE_FILE", "paper_bot.py")
PAPER_PID_FILE = ENV.get("PAPER_PID_FILE", "paper_bot.pid")
PAPER_PROCESS_LOG = ENV.get("PAPER_PROCESS_LOG", "paper_bot_process.log")
PAPER_PROCESS_ERR = ENV.get("PAPER_PROCESS_ERR", "paper_bot_process.err")
# v2.5.15: paper_bot도 direct pid fallback에서는 명시 python을 쓴다.
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
        m = re.search(r"v(\d+)\.(\d+)\.(\d+)", text or "")
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


def send(chat_id, text: str):
    text = str(text or "(empty)")
    chunks = []
    while len(text) > 3900:
        chunks.append(text[:3900])
        text = text[3900:]
    chunks.append(text)
    for chunk in chunks:
        try:
            api("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}, timeout=15)
        except Exception as e:
            print(f"send failed: {e}", file=sys.stderr)


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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


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


def _service_main_pid(service: str) -> int:
    rc, out = run(["systemctl", "show", service, "-p", "MainPID", "--value"], timeout=5)
    try:
        return int(str(out).strip() or "0")
    except Exception:
        return 0


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

    return {"deleted": deleted, "truncated": truncated, "compressed": compressed, "errors": errors, "freed": freed, "samples": samples, "dry_run": dry_run}

def cleanup_text(dry_run: bool = False) -> str:
    before = server_resource_snapshot()
    res = run_retention_cleanup(dry_run=dry_run)
    after = server_resource_snapshot()
    lines = ["🧹 디스크 순환정리 /gcleanup", "- 내부 로그/이벤트: 날짜 지나면 gzip 압축, gzip은 3일 후 삭제", "- 후보품질/복기 계열: 최근 7일 보관", "- 장부 paper OPEN/CLOSED/trade_log 계열은 삭제하지 않음", f"- 삭제 {res['deleted']}개 / 압축 {res.get('compressed',0)}개 / 비움·축소 {res['truncated']}개 / 오류 {res['errors']}개", f"- 확보 추정: {_fmt_bytes(res['freed'])}", f"- 전: 디스크 {before['disk_pct']:.1f}% / 남음 {_fmt_bytes(before['disk_free'])}", f"- 후: 디스크 {after['disk_pct']:.1f}% / 남음 {_fmt_bytes(after['disk_free'])}"]
    if res.get("samples"):
        lines += ["", "처리 예시", *[f"- {x}" for x in res["samples"][:10]]]
    return "\n".join(lines)


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
    for p in BOT_DIR.glob("수익형_v*.py"):
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


def service_exists(service: str) -> bool:
    rc, out = run(["systemctl", "status", service, "--no-pager"], timeout=8)
    return rc in (0, 3) or "Loaded:" in (out or "") or "Active:" in (out or "")


def is_service_active(service: str) -> str:
    rc, active = run(["systemctl", "is-active", service], timeout=8)
    return (active or "?").strip()


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


def restart_paper_bot() -> tuple[str, list[str]]:
    notes = []
    if service_exists(PAPER_SERVICE):
        rc, out = run(["systemctl", "restart", PAPER_SERVICE], timeout=35)
        time.sleep(3)
        active = is_service_active(PAPER_SERVICE)
        notes.append(f"systemd restart rc={rc} active={active}")
        if out:
            notes.append(tail(out, 700))
        return active, notes

    # v2.5.15: systemd 서비스가 아직 없을 때만 direct pid fallback을 쓴다.
    # 이 방식은 자동복구가 약하므로 /gpaper_service로 service 설치를 우선 안내한다.
    notes.append(f"systemd service 없음({PAPER_SERVICE}) → direct pid fallback")
    pid_path = BOT_DIR / PAPER_PID_FILE
    old_pid = read_pid(pid_path)
    if old_pid and pid_alive(old_pid):
        stop_pid(old_pid)
        notes.append(f"기존 paper pid 종료: {old_pid}")
    token = (
        ENV.get("PAPER_BOT_TOKEN", "").strip()
        or ENV.get("TELEGRAM_TOKEN", "").strip()
        or os.environ.get("PAPER_BOT_TOKEN", "").strip()
        or os.environ.get("TELEGRAM_TOKEN", "").strip()
    )
    if not token:
        notes.append("PAPER_BOT_TOKEN/TELEGRAM_TOKEN 없음 → 파일 교체만 완료, 실행 재시작은 보류")
        return "token_missing", notes
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    log_f = open(BOT_DIR / PAPER_PROCESS_LOG, "a", encoding="utf-8")
    err_f = open(BOT_DIR / PAPER_PROCESS_ERR, "a", encoding="utf-8")
    try:
        env = os.environ.copy()
        env.update(ENV)
        env.setdefault("TRADING_BOT_DIR", str(BOT_DIR))
        p = subprocess.Popen([PAPER_PYTHON_BIN, str(active_path), "--bot"], cwd=str(BOT_DIR), env=env, stdout=log_f, stderr=err_f, start_new_session=True)
        try:
            pid_path.write_text(str(p.pid), encoding="utf-8")
        except Exception as exc:
            notes.append(f"pid 파일 쓰기 실패: {exc.__class__.__name__}: {str(exc)[:80]}")
        time.sleep(2)
        alive = pid_alive(p.pid)
        notes.append(f"direct start pid={p.pid} alive={alive} python={PAPER_PYTHON_BIN}")
        return "active" if alive else "failed", notes
    finally:
        try:
            log_f.close(); err_f.close()
        except Exception:
            pass


def apply_paper_latest(force: bool = False, explicit: Optional[str] = None) -> dict:
    target = choose_latest_paper_target(explicit=explicit)
    if not target or not target.exists():
        return {"ok": True, "changed": False, "target": explicit or "?", "active": "유지", "warning": "paper_bot_v*.py 최신 파일 없음 → 기존 실행 유지"}
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    if same_hash and not force:
        svc_exists = service_exists(PAPER_SERVICE)
        active = is_service_active(PAPER_SERVICE) if svc_exists else ("active" if (read_pid(BOT_DIR / PAPER_PID_FILE) and pid_alive(read_pid(BOT_DIR / PAPER_PID_FILE))) else "unknown")
        if (svc_exists and active in {"active", "activating"}) or ((not svc_exists) and active == "active"):
            try:
                (BOT_DIR / DEPLOYED_PAPER_FILE).write_text(target.name + "\n", encoding="utf-8")
            except Exception:
                pass
            return {
                "ok": True, "changed": False, "target": target.name,
                "version": extract_paper_version(active_path), "active": active,
                "hash": short_hash(active_path), "skipped_restart": True,
                "warning": "fast-skip: hash 동일 + 실행중 → 검수/재시작 생략",
                "notes": ["최신 동일 파일은 끊지 않고 바로 다음 단계로 넘어감"],
                "warnings": [],
            }
    valid, checks, warnings = validate_paper_target(target)
    if not valid:
        return {"ok": False, "changed": False, "target": target.name, "checks": checks, "warnings": warnings, "error": "paper 검수 실패"}
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    backup = "없음"
    if same_hash and not force:
        svc_exists = service_exists(PAPER_SERVICE)
        active = is_service_active(PAPER_SERVICE) if svc_exists else ("active" if (read_pid(BOT_DIR / PAPER_PID_FILE) and pid_alive(read_pid(BOT_DIR / PAPER_PID_FILE))) else "unknown")
        if (svc_exists and active not in {"active", "activating"}) or ((not svc_exists) and active != "active"):
            restarted, notes = restart_paper_bot()
            return {
                "ok": restarted in {"active", "activating", "token_missing"}, "changed": False, "target": target.name,
                "version": extract_paper_version(active_path), "active": restarted,
                "hash": short_hash(active_path),
                "skipped_restart": False,
                "warning": "hash 동일하지만 paper_bot이 죽어 있어 재시작",
                "notes": notes,
                "warnings": warnings,
            }
        return {
            "ok": True, "changed": False, "target": target.name,
            "version": extract_paper_version(active_path), "active": active,
            "hash": short_hash(active_path),
            "skipped_restart": True,
            "warning": "변경 없음 → paper_bot 교체/재시작 생략",
            "notes": ["hash 동일: 안 바뀐 봇은 건드리지 않음"],
            "warnings": warnings,
        }
    if active_path.exists():
        backup_path = BOT_DIR / f"{active_path.name}.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(active_path, backup_path)
        backup = backup_path.name
    tmp = BOT_DIR / f"{active_path.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(target, tmp)
    if sha256_file(tmp) != sha256_file(target):
        tmp.unlink(missing_ok=True)
        return {"ok": False, "changed": False, "target": target.name, "error": "paper 임시복사 hash 불일치"}
    os.replace(tmp, active_path)
    (BOT_DIR / DEPLOYED_PAPER_FILE).write_text(target.name + "\n", encoding="utf-8")
    active, notes = restart_paper_bot()
    return {"ok": active in {"active", "token_missing"}, "changed": True, "target": target.name, "version": extract_paper_version(active_path), "active": active, "backup": backup, "hash": short_hash(active_path), "notes": notes, "warnings": warnings}


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
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    pid_file = read_pid(BOT_DIR / PAPER_PID_FILE)
    file_alive = bool(pid_file and pid_alive(pid_file))
    proc_pids = find_paperbot_pids()
    svc_exists = service_exists(PAPER_SERVICE)
    svc_active = is_service_active(PAPER_SERVICE) if svc_exists else "no_service"
    st = read_json(BOT_DIR / "paper_bot_status.json")
    try:
        status_pid = int(float(st.get("self_pid") or st.get("pid") or st.get("process_pid") or 0))
    except Exception:
        status_pid = 0
    status_alive = bool(status_pid and pid_alive(status_pid))
    proc_alive = bool(proc_pids)
    if status_alive:
        used_pid = status_pid
    elif file_alive:
        used_pid = pid_file
    elif proc_alive:
        used_pid = proc_pids[-1]
        repair_paper_pid_file(used_pid)
    else:
        used_pid = status_pid or pid_file or None
    alive = bool(status_alive or file_alive or proc_alive)
    age = "-"
    stale = True
    try:
        uts = float(st.get("updated_at", 0) or 0)
        sec = max(0, time.time()-uts) if uts > 0 else -1
        age = f"{sec:.0f}s" if sec >= 0 else "-"
        stale = sec > 180 if sec >= 0 else True
    except Exception:
        pass
    mismatch = bool(alive and ((pid_file and used_pid and pid_file != used_pid) or (status_pid and used_pid and status_pid != used_pid)))
    ghost = bool((not alive) and st.get("running"))
    if not alive:
        verdict = "❌ 죽음: 실제 paper_bot.py pid 없음" + (" / status 잔상" if ghost else "")
    elif mismatch:
        verdict = "⚠️ 보정됨: pid 파일/status/proc가 달라 확인 필요"
    elif stale:
        verdict = "⚠️ 확인필요: pid는 살아있지만 status 갱신이 오래됨"
    elif not svc_exists:
        verdict = "⚠️ direct pid 정상: systemd 서비스 미등록"
    elif svc_active not in {"active", "activating"}:
        verdict = f"⚠️ pid는 살아있지만 service={svc_active}"
    else:
        verdict = "✅ 정상: systemd active + pid alive + status 최신"
    return "\n".join([
        "🧪 페이퍼봇 상태 /gpaper_state",
        verdict,
        f"- service: {PAPER_SERVICE} / exists={svc_exists} / active={svc_active}",
        f"- active_file: {PAPER_ACTIVE_FILE} / version={extract_paper_version(active_path)} / hash={short_hash(active_path)}",
        f"- pid_file: {pid_file or '-'} / status_pid: {status_pid or '-'} / proc: {','.join(map(str, proc_pids[-3:])) if proc_pids else '-'} / used: {used_pid or '-'} / alive={alive}",
        f"- status version: {st.get('version','-')} / running: {st.get('running','-')} / updated age: {age} / stop_reason {st.get('stop_reason','-')}",
        f"- direct_python: {PAPER_PYTHON_BIN} / 권장관리: systemd {PAPER_SERVICE}",
        "- 로그 확인: /gpaperlog 80",
        "- 재시작: /gpaper_restart",
        "- 서비스화 안내: /gpaper_service",
    ])


def gpaper_service_text() -> str:
    svc_exists = service_exists(PAPER_SERVICE)
    svc_active = is_service_active(PAPER_SERVICE) if svc_exists else "no_service"
    unit_path = f"/etc/systemd/system/{PAPER_SERVICE}.service"
    lines = [
        "🧩 페이퍼봇 서비스화 /gpaper_service",
        ("✅ systemd 등록됨" if svc_exists else "⚠️ systemd 미등록: direct pid fallback 상태"),
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


def write_guard_post_upgrade_request(force: bool = False, explicit: Optional[str] = None) -> None:
    try:
        write_json(guard_post_upgrade_path(), {
            "action": "continue_gupgrade",
            "created_ts": time.time(),
            "created_at": now(),
            "force": bool(force),
            "explicit": explicit or "",
            "version": VERSION,
        })
    except Exception:
        pass


def pop_guard_post_upgrade_request(max_age_sec: float = 420.0) -> dict:
    path = guard_post_upgrade_path()
    data = read_json(path)
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    if not isinstance(data, dict) or data.get("action") != "continue_gupgrade":
        return {}
    try:
        age = time.time() - float(data.get("created_ts") or 0)
        if age < 0 or age > max_age_sec:
            return {}
    except Exception:
        return {}
    return data


def apply_guard_first_for_smart_upgrade() -> dict:
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
    write_guard_post_upgrade_request()
    rc, out = run(["systemctl", "restart", GUARD_SERVICE], timeout=30)
    return {"ok": rc == 0, "changed": True, "target": target.name, "action": "restart_guard", "backup": backup.name if backup else "없음", "hash": short_hash(active), "restart_rc": rc, "restart_out": tail(out, 500)}

def git_update() -> tuple[bool, list[str]]:
    steps = []
    # v2.2: GitHub 최신 파일 자동선택 전에 dubious ownership으로 fetch가 막히지 않게 고정한다.
    rc, out = run(["git", "config", "--global", "--add", "safe.directory", str(BOT_DIR)], cwd=BOT_DIR, timeout=15)
    steps.append(f"git safe.directory: rc={rc}\n{tail(out, 300)}")
    rc, out = run(["git", "fetch", "origin"], cwd=BOT_DIR, timeout=60)
    steps.append(f"git fetch origin: rc={rc}\n{tail(out, 700)}")
    if rc != 0:
        return False, steps
    rc, out = run(["git", "reset", "--hard", f"origin/{GIT_BRANCH}"], cwd=BOT_DIR, timeout=60)
    steps.append(f"git reset --hard origin/{GIT_BRANCH}: rc={rc}\n{tail(out, 700)}")
    if rc != 0:
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
    required = ["/core", "/trade", "/deploy", "/upgradestatus"]
    missing = [c for c in required if c not in text]
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
        rc, out = run(["systemctl", "restart", MAIN_SERVICE], timeout=30)
        mark("restart_cmd", t0)

        active = "unknown"
        fatal = False
        log_since = ""
        waited = 0
        wait_limit = max(15, min(60, int(RESTART_WAIT_SEC)))
        target_hash = short_hash(target)
        bot_hash = short_hash(dst)
        quick_ok = False
        t0 = perf_now()
        while waited < wait_limit:
            time.sleep(5)
            waited += 5
            active = is_main_service_active()
            log_since = recent_journal(since=started)
            fatal = has_fatal_log(log_since)
            bot_version_now = extract_bot_version(dst)
            hash_ok = (short_hash(target) == short_hash(dst))
            version_ok = same_version_label(bot_version_now, target_version_text)
            # v2.3: 파일/hash/version이 맞고 active면 실행로그 버전 대기를 오래 하지 않는다.
            if active == "active" and not fatal and hash_ok and version_ok:
                quick_ok = True
                break
            if fatal:
                break
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
            f"다음 확인: /gdeploy 또는 메인봇 /batch /core /trade /deploy"
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
    if res.get("action") == "restart_guard":
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


def apply_latest_auto(force: bool = False, explicit: Optional[str] = None, skip_guard: bool = False) -> str:
    """스마트 전체 업그레이드.

    중요도 순서: 가드봇 → 메인봇 → 페이퍼봇 → WS → micro.
    원칙: hash가 같은 최신 상태는 교체/재시작하지 않고 그대로 유지한다.
    가드봇이 바뀌면 먼저 가드봇만 재시작하고, 새 가드가 post flag를 읽어 나머지를 이어간다.
    """
    total_t0 = perf_now()
    status_path = BOT_DIR / UPGRADE_STATUS_FILE
    status = read_json(status_path)
    progress_notify("🚀 스마트 업그레이드 시작\n- 순서: 가드봇 → 메인봇 → 페이퍼봇 → WS → micro\n- 원칙: hash 동일 최신 항목은 교체/재시작 생략")


    disk_ok, disk_text = preupgrade_disk_guard(force=force)
    progress_notify(disk_text)
    if not disk_ok:
        write_json(status_path, {"time": now(), "ok": False, "stage": "disk_precheck", "guard_version": VERSION})
        return "❌ 스마트 업그레이드 중단 /gupgrade\n" + disk_text
    progress_notify("[1/5] GitHub 최신 파일 확인 중")
    ok_git, steps = git_update()
    if not ok_git:
        write_json(status_path, {"time": now(), "ok": False, "stage": "git", "steps": steps, "guard_version": VERSION})
        return "❌ 스마트 업그레이드 실패 /gupgrade\n- 단계: GitHub 갱신 실패\n\n" + "\n\n".join(steps)

    guard_res = {"ok": True, "changed": False, "action": "skip", "warning": "skip_guard=True"}
    if not skip_guard:
        progress_notify("[2/5] 가드봇 최신 여부 확인 중\n- 바뀐 가드봇이 있으면 먼저 가드봇만 최신화하고, 새 가드가 나머지를 이어감")
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
                "- 다음 처리: 새 가드봇이 부팅되면 메인봇 → 페이퍼봇 → WS → micro를 자동으로 이어서 확인/적용\n\n"
                + format_guard_apply_result(guard_res)
            )
            progress_notify(msg)
            return msg
    else:
        progress_notify("[2/5] 가드봇 확인 생략\n- 방금 가드봇 자체 최신화 후 자동 이어가기 중")

    progress_notify("[3/5] 메인봇 확인 중\n- hash 동일이면 재시작하지 않음")
    main_text = apply_main_latest_auto(force=force, explicit=explicit, skip_git=True)
    main_ok = "❌" not in main_text and "업그레이드 예외" not in main_text and "중단" not in main_text

    progress_notify("[4/5] 페이퍼봇 확인 중\n- hash 동일이면 재시작하지 않음")
    paper_res = apply_paper_latest(force=force, explicit=None)
    paper_ok = bool(paper_res.get("ok"))

    progress_notify("[5/5] 외부직원 확인 중\n- WS/micro hash 동일 + 실행중이면 기존 restart 경로를 타지 않고 즉시 유지")
    ws_res = apply_ws_sidecar_latest(force=force, explicit=None, restart=True)
    ws_ok = bool(ws_res.get("ok"))
    micro_res = apply_micro_sidecar_latest(force=force, explicit=None, restart=True)
    micro_ok = bool(micro_res.get("ok"))

    status = read_json(status_path)
    status.update({
        "guard_bot": guard_res,
        "paper": paper_res,
        "ws_sidecar": ws_res,
        "micro_sidecar": micro_res,
        "guard_version": VERSION,
        "smart_upgrade_total_sec": perf_now() - total_t0,
        "ok": bool(main_ok and paper_ok and ws_ok and micro_ok),
        "order": ["guard", "main", "paper", "ws", "micro"],
        "changed_only_restart_policy": True,
    })
    write_json(status_path, status)

    all_ok = bool(main_ok and paper_ok and ws_ok and micro_ok)
    title = "🚀 ✅ 전체 최신화 완료 /gupgrade" if all_ok else "🚀 ❔ 전체 최신화 확인 필요 /gupgrade"
    progress_notify("✅ 스마트 업그레이드 단계 완료\n- 최종 결과 정리 전송 중")
    return (
        f"{title}\n"
        f"- guard: {VERSION}\n"
        f"- 순서: 가드봇 → 메인봇 → 페이퍼봇 → WS → micro\n"
        f"- 방식: 파일 hash 비교 후 바뀐 파일만 교체·재시작\n"
        f"- 최신 동일 항목: 유지 / 재시작 생략\n"
        f"- total: {fmt_sec(perf_now() - total_t0)}\n\n"
        f"[가드봇]\n{format_guard_apply_result(guard_res)}\n\n"
        f"[메인봇]\n{main_text}\n\n"
        f"[페이퍼봇]\n{format_paper_apply_result(paper_res)}\n\n"
        f"[웹소켓직원]\n{format_ws_apply_result(ws_res)}\n\n"
        f"[호가·체결직원]\n{format_micro_apply_result(micro_res)}\n\n"
        f"다음 확인:\n"
        f"- 가드봇: /gdeploy /gexternal_state\n"
        f"- 메인봇: /external_status /quality /errorlog\n"
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


def deploy_status() -> str:
    bot_version, target, deployed, latest = get_bot_versions()
    pv = get_paper_versions()
    wsv = ws_sidecar_state_dict()
    msv = micro_state_dict()
    active = is_main_service_active()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    latest_path = BOT_DIR / latest if latest != "?" else None
    latest_ver = extract_bot_version(latest_path) if latest_path else "?"
    runtime_ver = extract_recent_runtime_version(lines=260, since=service_start_since())
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
        verdict_bits.append("paper_bot systemd 서비스 미등록(direct pid fallback)")
    if runtime_ver not in {"시작로그 대기", "시작로그 확인불가"} and latest_path and not same_version_label(runtime_ver, bot_version):
        verdict_bits.append("bot.py 파일버전과 실제 실행로그 버전 불일치")
    if not wsv.get("ok"):
        verdict_bits.append("웹소켓직원 확인 필요: " + str(wsv.get("verdict", "?")))
    if str(wsv.get("management_warning") or "-") != "-":
        verdict_bits.append("웹소켓직원 구조 확인: " + str(wsv.get("management_warning")))
    if msv.get("active_exists") and not msv.get("ok"):
        verdict_bits.append("호가체결직원 확인 필요: " + str(msv.get("verdict", "?")))
    if str(msv.get("management_warning") or "-") != "-":
        verdict_bits.append("호가체결직원 구조 확인: " + str(msv.get("management_warning")))

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
    focus_text = "\n".join(focus[-5:]) if focus else "없음"
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
        "명령",
        "- /gws_state: 웹소켓 상세",
        "- /gmicro_state: 호가·체결 상세",
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
        f"- /gpaper_service 페이퍼봇 서비스화 안내\n"
        f"- /gws_state 웹소켓직원 상태\n"
        f"- /gws_restart 웹소켓직원 재시작\n"
        f"- /gmicro_state 호가·체결직원 상태\n"
        f"- /gmicro_upgrade 호가·체결직원 최신 적용\n"
        f"- /gguard_upgrade 가드봇 자체 업그레이드\n"
        f"- /glog 최근 오류\n"
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
        "/gpaper_service - 페이퍼봇 서비스화 안내\n"
        "/gexternal_state - 외부직원 통합상태\n"
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
        {"command": "gupgrade_menu", "description": "업그레이드 메뉴"},
        {"command": "guard", "description": "가드 핵심 상태"},
        {"command": "gdeploy", "description": "배포·버전 상태"},
        {"command": "gpaper_state", "description": "페이퍼봇 상태"},
        {"command": "gexternal_state", "description": "외부직원 통합상태"},
        {"command": "glog", "description": "메인봇 로그"},
        {"command": "gpaperlog", "description": "페이퍼봇 로그"},
        {"command": "grestart", "description": "메인봇 재시작"},
        {"command": "gpaper_restart", "description": "페이퍼봇 재시작"},
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
    """개별 업그레이드 명령 공통 인자 파서."""
    force = bool(args and str(args[0]).lower() == "force")
    explicit = None
    if force and len(args) > 1:
        explicit = args[1]
    elif args and str(args[0]).endswith(".py"):
        explicit = args[0]
    return force, explicit


def gupgrade_menu_text() -> str:
    """v2.5.36: 전체/개별 업그레이드 메뉴."""
    return "\n".join([
        "🛠 업그레이드 메뉴 /gupgrade_menu",
        "- 기본은 전체, 급할 때는 바뀐 코드만 개별 적용",
        "- 모든 개별 명령도 기존 단일 적용 함수 사용: hash 동일이면 재시작 생략",
        "",
        "[1] 전체",
        "/gupgrade - 가드 → 메인 → 페이퍼 → WS → micro 전체 확인/적용",
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
    ])


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
    if cmd in ("/gupgrade", "/gupgrade_all", "/apply_latest", "/upgradebot"):
        force, explicit = parse_upgrade_force_explicit(args)
        return apply_latest_auto(force=force, explicit=explicit)
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
    set_commands()
    send_guard_start_notice()

    post_req = pop_guard_post_upgrade_request()
    if post_req:
        try:
            send(ALLOWED_CHAT_ID, "🛡 가드봇 최신화 완료\n- 이어서 메인봇 → 페이퍼봇 → WS → micro 스마트 업그레이드를 자동 진행합니다.")
            reply = apply_latest_auto(
                force=bool(post_req.get("force")),
                explicit=str(post_req.get("explicit") or "") or None,
                skip_guard=True,
            )
            send(ALLOWED_CHAT_ID, reply)
        except Exception as exc:
            send(ALLOWED_CHAT_ID, f"❌ 가드봇 재시작 후 자동 이어가기 실패\n- {exc.__class__.__name__}: {exc}")

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
                    reply = handle_command(text)
                    send(chat_id, reply)

            active = is_main_service_active()
            if last_active is None:
                last_active = active
            elif active != last_active:
                last_active = active
                if time.time() - last_notify > 20:
                    last_notify = time.time()
                    # v2.5.7: 상태변경 알림에서 /glog를 자동 호출하지 않는다.
                    # journalctl 지연이 가드봇 polling을 막고 재시작처럼 보이는 문제를 차단한다.
                    send(ALLOWED_CHAT_ID, f"❔ 메인봇 상태 변경\n- active: {active}\n- 시각: {now()}\n- 로그 확인: /glog 80")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{now()}] loop error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(3)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
