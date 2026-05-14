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
VERSION = "guard_v2.5.13_ws_sidecar_upgrade_restart_2026-05-14"
UPGRADE_STATUS_FILE = ".guard_upgrade_status.json"
GUARD_OFFSET_FILE = ".guard_update_offset.json"
GUARD_START_NOTICE_FILE = ".guard_start_notice.json"


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
        "PAPER_SERVICE", "PAPER_ACTIVE_FILE", "PAPER_PID_FILE", "PAPER_BOT_TOKEN",
        "GUARD_SERVICE", "GUARD_ACTIVE_FILE",
        "WS_ACTIVE_FILE", "WS_PID_FILE", "WS_LOG_FILE", "WS_STATUS_FILE", "WS_CACHE_FILE"
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
GUARD_SERVICE = ENV.get("GUARD_SERVICE", "tradingbot-guard")
GUARD_ACTIVE_FILE = ENV.get("GUARD_ACTIVE_FILE", "backga_guard_bot.py")
DEPLOY_TARGETS_FILE = "DEPLOY_TARGETS.json"
DEPLOYED_PAPER_FILE = ".deployed_paper_target"
DEPLOYED_GUARD_FILE = ".deployed_guard_target"
DEPLOYED_WS_FILE = ".deployed_ws_sidecar_target"
WS_ACTIVE_FILE = ENV.get("WS_ACTIVE_FILE", "ws_sidecar.py")
WS_PID_FILE = ENV.get("WS_PID_FILE", "clean_ws_sidecar.pid")
WS_LOG_FILE = ENV.get("WS_LOG_FILE", "clean_ws_sidecar.out")
WS_STATUS_FILE = ENV.get("WS_STATUS_FILE", "clean_ws_sidecar_status.json")
WS_CACHE_FILE = ENV.get("WS_CACHE_FILE", "clean_ws_live_cache.json")

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
    send(ALLOWED_CHAT_ID, f"🛡 백가 가드봇 시작\n- {VERSION}\n- 관리대상: {MAIN_SERVICE}\n- 업그레이드: /gupgrade\n- 상태확인: /guard")

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
    pid_path = BOT_DIR / PAPER_PID_FILE
    old_pid = read_pid(pid_path)
    if old_pid and pid_alive(old_pid):
        stop_pid(old_pid)
        notes.append(f"기존 paper pid 종료: {old_pid}")
    token = ENV.get("PAPER_BOT_TOKEN", os.environ.get("PAPER_BOT_TOKEN", "")).strip()
    if not token:
        notes.append("PAPER_BOT_TOKEN 없음 → 파일 교체만 완료, 실행 재시작은 보류")
        return "token_missing", notes
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    log_f = open(BOT_DIR / PAPER_PROCESS_LOG, "a", encoding="utf-8")
    err_f = open(BOT_DIR / PAPER_PROCESS_ERR, "a", encoding="utf-8")
    try:
        env = os.environ.copy()
        env.update(ENV)
        p = subprocess.Popen([PYTHON_BIN, str(active_path), "--bot"], cwd=str(BOT_DIR), env=env, stdout=log_f, stderr=err_f, start_new_session=True)
        pid_path.write_text(str(p.pid), encoding="utf-8")
        time.sleep(2)
        alive = pid_alive(p.pid)
        notes.append(f"direct start pid={p.pid} alive={alive}")
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
    valid, checks, warnings = validate_paper_target(target)
    if not valid:
        return {"ok": False, "changed": False, "target": target.name, "checks": checks, "warnings": warnings, "error": "paper 검수 실패"}
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    backup = "없음"
    if same_hash and not force:
        active = is_service_active(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else ("active" if (read_pid(BOT_DIR / PAPER_PID_FILE) and pid_alive(read_pid(BOT_DIR / PAPER_PID_FILE))) else "unknown")
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
    m = re.search(r"ws_sidecar_v(\d+\.\d+)", path.name)
    return f"ws_sidecar_v{m.group(1)}" if m else path.name


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
    except Exception:
        pass
    alive = [p for p in find_ws_sidecar_pids() if pid_alive(p)]
    return (not alive), notes + ([f"잔류 pid: {alive}"] if alive else [])


def start_ws_sidecar() -> tuple[str, list[str]]:
    notes = []
    active_path = BOT_DIR / WS_ACTIVE_FILE
    if not active_path.exists():
        return "missing", [f"active file 없음: {WS_ACTIVE_FILE}"]
    ok, out = py_compile(active_path)
    if not ok:
        return "compile_failed", [tail(out, 1200)]
    pids = find_ws_sidecar_pids()
    if pids:
        (BOT_DIR / WS_PID_FILE).write_text(str(pids[-1]), encoding="utf-8")
        return "active", [f"이미 실행중 pid={pids[-1]}"]
    log_f = open(BOT_DIR / WS_LOG_FILE, "a", encoding="utf-8")
    try:
        env = os.environ.copy()
        env.update(ENV)
        p = subprocess.Popen([PYTHON_BIN, str(active_path)], cwd=str(BOT_DIR), env=env, stdout=log_f, stderr=subprocess.STDOUT, start_new_session=True)
        (BOT_DIR / WS_PID_FILE).write_text(str(p.pid), encoding="utf-8")
        time.sleep(2)
        alive = pid_alive(p.pid)
        notes.append(f"direct start pid={p.pid} alive={alive}")
        return "active" if alive else "failed", notes
    finally:
        try:
            log_f.close()
        except Exception:
            pass


def restart_ws_sidecar() -> tuple[str, list[str]]:
    _ok, stop_notes = stop_ws_sidecar()
    active, start_notes = start_ws_sidecar()
    return active, stop_notes + start_notes


def ws_sidecar_state_dict() -> dict:
    active_path = BOT_DIR / WS_ACTIVE_FILE
    pid_file = read_pid(BOT_DIR / WS_PID_FILE)
    proc_pids = find_ws_sidecar_pids()
    used = pid_file if (pid_file and pid_alive(pid_file)) else (proc_pids[-1] if proc_pids else None)
    if used and (not pid_file or pid_file != used):
        try:
            (BOT_DIR / WS_PID_FILE).write_text(str(used), encoding="utf-8")
        except Exception:
            pass
    st = read_json(BOT_DIR / WS_STATUS_FILE)
    age = -1
    try:
        ts = float(st.get("updated_at", 0) or 0)
        age = time.time() - ts if ts > 0 else -1
    except Exception:
        pass
    alive = bool(used and pid_alive(used))
    if not active_path.exists():
        verdict = "❌ active file 없음"
    elif alive and (0 <= age <= 60):
        verdict = "✅ 정상"
    elif alive:
        verdict = "⚠️ 실행중이나 status 오래됨"
    else:
        verdict = "❌ 정지"
    return {"verdict": verdict, "active_file": WS_ACTIVE_FILE, "active_exists": active_path.exists(), "version": ws_target_version(active_path), "hash": short_hash(active_path), "pid_file": pid_file, "proc_pids": proc_pids, "used_pid": used, "alive": alive, "status": st, "age": age}


def apply_ws_sidecar_latest(force: bool = False, explicit: Optional[str] = None, restart: bool = True) -> dict:
    target = choose_latest_ws_sidecar_target(explicit=explicit)
    if not target or not target.exists():
        return {"ok": True, "changed": False, "target": explicit or "?", "active": ws_sidecar_state_dict().get("verdict", "유지"), "warning": "ws_sidecar_v*.py 최신 파일 없음 → 기존 상태 유지"}
    okc, out = py_compile(target)
    if not okc:
        return {"ok": False, "changed": False, "target": target.name, "error": "ws_sidecar py_compile 실패", "notes": [tail(out, 1000)]}
    active_path = BOT_DIR / WS_ACTIVE_FILE
    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    backup = "없음"
    if not same_hash or force:
        if active_path.exists():
            backup_path = BOT_DIR / f"{active_path.name}.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(active_path, backup_path)
            backup = backup_path.name
        tmp = BOT_DIR / f"{active_path.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target, tmp)
        if sha256_file(tmp) != sha256_file(target):
            tmp.unlink(missing_ok=True)
            return {"ok": False, "changed": False, "target": target.name, "error": "ws 임시복사 hash 불일치"}
        os.replace(tmp, active_path)
        (BOT_DIR / DEPLOYED_WS_FILE).write_text(target.name + "\n", encoding="utf-8")
        changed = True
    else:
        changed = False
        (BOT_DIR / DEPLOYED_WS_FILE).write_text(target.name + "\n", encoding="utf-8")
    if restart:
        active, notes = restart_ws_sidecar()
    else:
        state = ws_sidecar_state_dict()
        active = state.get("verdict", "unknown")
        notes = ["restart 생략"]
    return {"ok": active in {"active", "missing"} or str(active).startswith("✅"), "changed": changed, "target": target.name, "version": ws_target_version(target), "active": active, "backup": backup, "hash": short_hash(active_path), "notes": notes, "warning": "변경 없음 → active 파일 hash 동일" if not changed else ""}


def gws_state_text() -> str:
    d = ws_sidecar_state_dict()
    st = d.get("status") or {}
    age = f"{d['age']:.0f}s" if isinstance(d.get("age"), (int, float)) and d.get("age", -1) >= 0 else "-"
    proc = ",".join(map(str, d.get("proc_pids") or [])) or "-"
    return "\n".join([
        "🛰 웹소켓 직원 /gws_state",
        str(d.get("verdict", "?")),
        f"- active_file: {d.get('active_file')} / exists={d.get('active_exists')} / version={d.get('version')} / hash={d.get('hash')}",
        f"- pid_file: {d.get('pid_file') or '-'} / proc: {proc} / used: {d.get('used_pid') or '-'} / alive={d.get('alive')}",
        f"- status_age: {age} / state: {st.get('state','-')} / last_error: {st.get('last_error','-')}",
        f"- raw {st.get('raw_count', st.get('raw', 0))} / parse {st.get('parse_ok', st.get('parse', 0))} / price {st.get('price_ok', st.get('price', 0))} / amount {st.get('amount_ok', st.get('amount', 0))} / match {st.get('match_ok', st.get('match', 0))}",
        f"- cache: {WS_CACHE_FILE} / status: {WS_STATUS_FILE} / log: {WS_LOG_FILE}",
        "- 시작: /gws_start / 중지: /gws_stop / 재시작: /gws_restart",
    ])


def gws_log(lines: int = 80) -> str:
    txt = read_file_tail(BOT_DIR / WS_LOG_FILE, max_bytes=120_000)
    return f"🧾 웹소켓 직원 로그 /gwslog {lines}\n" + (tail(txt, 2600) if txt.strip() else "- 로그 없음")


def gws_start_text() -> str:
    active, notes = start_ws_sidecar()
    return "\n".join(["▶️ 웹소켓 직원 시작 /gws_start", f"- active: {active}", f"- version: {ws_target_version(BOT_DIR / WS_ACTIVE_FILE)}"] + [f"- {x}" for x in notes])


def gws_stop_text() -> str:
    ok, notes = stop_ws_sidecar()
    return "\n".join(["⏹ 웹소켓 직원 중지 /gws_stop", f"- ok: {ok}"] + [f"- {x}" for x in notes])


def gws_restart_text() -> str:
    active, notes = restart_ws_sidecar()
    return "\n".join(["🔁 웹소켓 직원 재시작 /gws_restart", f"- active: {active}", f"- version: {ws_target_version(BOT_DIR / WS_ACTIVE_FILE)}"] + [f"- {x}" for x in notes])


def format_ws_apply_result(res: dict) -> str:
    ok = "✅" if res.get("ok") else "❌"
    changed = "교체+재시작" if res.get("changed") else "변경없음/재시작"
    lines = [f"{ok} ws_sidecar {changed}"]
    for k in ["target", "version", "active", "backup", "hash", "error", "warning"]:
        if res.get(k):
            lines.append(f"- {k}: {res.get(k)}")
    if res.get("notes"):
        lines.append("- notes:")
        lines.extend(f"  · {str(x)[:220]}" for x in res.get("notes", [])[:6])
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
    else:
        verdict = "✅ 정상: pid alive + status 최신"
    return "\n".join([
        "🧪 페이퍼봇 상태 /gpaper_state",
        verdict,
        f"- service: {PAPER_SERVICE} / exists={svc_exists} / active={svc_active}",
        f"- active_file: {PAPER_ACTIVE_FILE} / version={extract_paper_version(active_path)} / hash={short_hash(active_path)}",
        f"- pid_file: {pid_file or '-'} / status_pid: {status_pid or '-'} / proc: {','.join(map(str, proc_pids[-3:])) if proc_pids else '-'} / used: {used_pid or '-'} / alive={alive}",
        f"- status version: {st.get('version','-')} / running: {st.get('running','-')} / updated age: {age} / stop_reason {st.get('stop_reason','-')}",
        "- 로그 확인: /gpaperlog 80",
        "- 재시작: /gpaper_restart",
    ])


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


def apply_main_latest_auto(force: bool = False, explicit: Optional[str] = None) -> str:
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

        if active != "active" or fatal:
            reason = f"재시작 후 active={active}, fatal_log={fatal}, waited={waited}s"
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


def apply_latest_auto(force: bool = False, explicit: Optional[str] = None) -> str:
    """v2.5: /gupgrade는 메인봇과 페이퍼봇을 함께 확인한다.
    - 메인봇은 기존 v2.4 검수/롤백 루틴 그대로 사용한다.
    - 페이퍼봇은 paper_bot_v*.py를 paper_bot.py로 교체하고 재시작한다.
    """
    total_t0 = perf_now()
    main_text = apply_main_latest_auto(force=force, explicit=explicit)
    main_ok = "❌" not in main_text and "업그레이드 예외" not in main_text
    paper_res = apply_paper_latest(force=force, explicit=None)
    paper_ok = bool(paper_res.get("ok"))
    ws_res = apply_ws_sidecar_latest(force=force, explicit=None, restart=True)
    ws_ok = bool(ws_res.get("ok"))
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    status["paper"] = paper_res
    status["ws_sidecar"] = ws_res
    status["guard_version"] = VERSION
    status["main_paper_ws_total_sec"] = perf_now() - total_t0
    status["ok"] = bool(main_ok and paper_ok and ws_ok)
    write_json(BOT_DIR / UPGRADE_STATUS_FILE, status)
    title = "🚀 ✅ 스마트 업그레이드 완료 /gupgrade" if main_ok and paper_ok and ws_ok else "🚀 ❔ 스마트 업그레이드 확인 필요 /gupgrade"
    return (
        f"{title}\n"
        f"- guard: {VERSION}\n"
        f"- 방식: main/paper/ws 각각 비교 후 바뀐 것만 교체·재시작\n"
        f"- total: {fmt_sec(perf_now() - total_t0)}\n\n"
        f"[메인봇]\n{main_text}\n\n"
        f"[페이퍼봇]\n{format_paper_apply_result(paper_res)}\n\n"
        f"[웹소켓직원]\n{format_ws_apply_result(ws_res)}\n\n"
        f"다음 확인:\n"
        f"- 가드봇: /gdeploy /gws_state\n"
        f"- 메인봇: /batch /ws_status /paper_handoff /deploy\n"
        f"- 페이퍼봇: /pstatus /pbatch /perror"
    )


def deploy_status() -> str:
    bot_version, target, deployed, latest = get_bot_versions()
    pv = get_paper_versions()
    wsv = ws_sidecar_state_dict()
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
    if runtime_ver not in {"시작로그 대기", "시작로그 확인불가"} and latest_path and not same_version_label(runtime_ver, bot_version):
        verdict_bits.append("bot.py 파일버전과 실제 실행로그 버전 불일치")

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
        f"- pid: {wsv.get('used_pid') or '-'} / alive: {wsv.get('alive')} / status_age: {round(wsv.get('age', -1), 1) if isinstance(wsv.get('age'), (int, float)) else '-'}s\n"
        f"- deployed: {read_file(BOT_DIR / DEPLOYED_WS_FILE) or '?'}\n\n"
        f"📌 판정\n{verdict_text}\n\n"
        f"📌 최근 업그레이드\n- {last}\n\n"
        f"🧾 최근 오류 의심\n{focus_text}\n\n"
        f"명령\n"
        f"- /gupgrade : 메인봇 + 페이퍼봇 자동판단 최신 적용\n"
        f"- /gpaper_restart : 페이퍼봇 재시작\n"
        f"- /gguard_upgrade : 가드봇 자체 최신 적용\n"
        f"- /glog : 메인봇 최근 오류로그"
    )

def service_status() -> str:
    active = is_main_service_active()
    bot_version, target, deployed, latest = get_bot_versions()
    pv = get_paper_versions()
    wsv = ws_sidecar_state_dict()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    last_ok = "없음"
    if status:
        last_ok = f"{status.get('time','?')} / {'성공' if status.get('ok') else '실패/확인필요'} / {status.get('target') or status.get('stage') or '?'}"
    return (
        f"🛡 가드봇 /guard\n"
        f"✅ 결론\n"
        f"- 메인봇: {active} / {bot_version}\n"
        f"- 페이퍼봇: {pv['service']} / {pv['active_version']}\n"
        f"- 웹소켓직원: {wsv.get('verdict')} / {wsv.get('version')}\n"
        f"- 업그레이드: /gupgrade 한 번으로 main + paper_bot + ws 확인\n\n"
        f"📊 상태\n"
        f"- guard: {VERSION}\n"
        f"- main service: {MAIN_SERVICE}\n"
        f"- paper service: {PAPER_SERVICE}\n"
        f"- 최신 수익형 파일: {latest}\n"
        f"- 최신 paper 파일: {pv['latest']}\n"
        f"- 웹소켓 pid: {wsv.get('used_pid') or '-'} / alive: {wsv.get('alive')}\n"
        f"- 최근 적용: {last_ok}\n\n"
        f"🧭 자주 쓰는 명령\n"
        f"- /gdeploy 배포상태\n"
        f"- /gupgrade main+paper 변경분만 자동적용\n"
        f"- /gpaper_restart 페이퍼봇 재시작\n"
        f"- /gws_state 웹소켓직원 상태\n"
        f"- /gws_restart 웹소켓직원 재시작\n"
        f"- /gguard_upgrade 가드봇 자체 업그레이드\n"
        f"- /glog 최근 오류"
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
        "/gdeploy - main/paper 배포·버전 상태\n"
        "/gupgrade - GitHub 최신 main + paper_bot 변경분만 자동 적용\n"
        "/glog - 메인봇 최근 오류 로그\n"
        "/gpaperlog - 페이퍼봇 최근 로그\n\n"
        "복구/재시작\n"
        "/grestart - 메인봇 재시작\n"
        "/gpaper_restart - 페이퍼봇 재시작\n"
        "/gws_state - 웹소켓 직원 상태\n"
        "/gws_start - 웹소켓 직원 시작\n"
        "/gws_stop - 웹소켓 직원 중지\n"
        "/gws_restart - 웹소켓 직원 재시작\n"
        "/gbackups - 메인봇 백업 목록\n"
        "/grollback - 메인봇 최신 백업 롤백\n"
        "/gunlock - 배포 lock 삭제\n\n"
        "가드봇 자체\n"
        "/gguard_upgrade - 가드봇 자체 최신 적용\n"
        "/gguard_restart - 가드봇 재시작\n\n"
        "운영법: GitHub에 수익형_v*.py, paper_bot_v*.py, ws_sidecar_v*.py, DEPLOY_TARGETS.json을 올리고 /gupgrade\n"
        "주의: 전략/조건/청산은 건드리지 않고 파일 교체와 재시작만 관리"
    )

def set_commands() -> None:
    commands = [
        {"command": "guard", "description": "가드 핵심 상태"},
        {"command": "gdeploy", "description": "main+paper 배포상태"},
        {"command": "gupgrade", "description": "main+paper 최신 적용"},
        {"command": "glog", "description": "메인봇 로그"},
        {"command": "gpaperlog", "description": "페이퍼봇 로그"},
        {"command": "gpaper_state", "description": "페이퍼봇 상태"},
        {"command": "grestart", "description": "메인봇 재시작"},
        {"command": "gpaper_restart", "description": "페이퍼봇 재시작"},
        {"command": "gws_state", "description": "웹소켓 직원 상태"},
        {"command": "gws_restart", "description": "웹소켓 직원 재시작"},
        {"command": "gguard_upgrade", "description": "가드봇 자체 업그레이드"},
        {"command": "gbackups", "description": "백업 목록"},
        {"command": "gmenu", "description": "가드 메뉴"},
    ]
    try:
        api("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)}, timeout=15)
    except Exception as e:
        print(f"setMyCommands failed: {e}", file=sys.stderr)


def _extract_guard_command_lines(text: str) -> list[str]:
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip().startswith("/")]


def _guard_command_name(line: str) -> str:
    first = line.strip().split()[0].lower() if line.strip().split() else ""
    if "@" in first:
        first = first.split("@", 1)[0]
    return first


def handle_single_command(text: str) -> str:
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    args = parts[1:]

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
    if cmd in ("/gws_state", "/gwsstate"):
        return gws_state_text()
    if cmd in ("/gws_start", "/gwsstart"):
        return gws_start_text()
    if cmd in ("/gws_stop", "/gwsstop"):
        return gws_stop_text()
    if cmd in ("/gws_restart", "/gwsrestart"):
        return gws_restart_text()
    if cmd in ("/gwslog", "/gws_log"):
        n = 80
        if args and args[0].isdigit():
            n = max(20, min(300, int(args[0])))
        return gws_log(n)
    if cmd in ("/gguard_upgrade", "/guard_upgrade"):
        force = bool(args and args[0].lower() == "force")
        explicit = None
        if force and len(args) > 1:
            explicit = args[1]
        elif args and args[0].endswith(".py"):
            explicit = args[0]
        return guard_self_upgrade(force=force, explicit=explicit)
    if cmd in ("/gguard_restart", "/guard_restart"):
        rc, out = run(["systemctl", "restart", GUARD_SERVICE], timeout=30)
        return f"🔁 가드봇 재시작 요청\n- rc: {rc}\n- service: {GUARD_SERVICE}\n- 다음 확인: /guard"
    if cmd in ("/gunlock", "/unlock"):
        return unlock_deploy()
    if cmd in ("/gbackups", "/backups"):
        return list_backups_text()
    if cmd in ("/grollback", "/rollback"):
        return rollback_latest()
    if cmd in ("/gupgrade", "/apply_latest", "/upgradebot"):
        force = bool(args and args[0].lower() == "force")
        explicit = None
        if force and len(args) > 1:
            explicit = args[1]
        elif args and args[0].endswith(".py"):
            explicit = args[0]
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
            if name in {"/gupgrade", "/apply_latest", "/upgradebot"}:
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
                    if first in ("/gupgrade", "/apply_latest", "/upgradebot"):
                        send(chat_id, "🚀 /gupgrade 접수\n- GitHub 갱신 → 검수 → 교체 → 재시작 확인 중\n- v2.4부터 validate fast mode. hash/version 일치 시 빠르게 성공 처리함")
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
