#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backga_guard_bot_v2.py
- 메인 코인봇(tradingbot)이 죽어도 따로 살아있는 관리 전용 텔레그램 봇
- 표준 라이브러리만 사용
- GUARD_CHAT_ID에서 온 명령만 처리

핵심 개선:
- DEPLOY_TARGET 없이 GitHub 최신 수익형_v*.py 자동 선택
- py_compile + top-level alias NameError 사전 검사
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
VERSION = "guard_v2.0_auto_latest_2026-05-09"
UPGRADE_STATUS_FILE = ".guard_upgrade_status.json"


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
        "GUARD_BOT_TOKEN", "GUARD_CHAT_ID"
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
RESTART_WAIT_SEC = int(ENV.get("GUARD_RESTART_WAIT_SEC", "10") or "10")

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


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}


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


def git_update() -> tuple[bool, list[str]]:
    steps = []
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
    required = ["/core", "/trade", "/deploy", "/deep", "/upgradebot", "/upgradestatus"]
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

    ok, out = py_compile(path)
    checks.append("✅ py_compile 통과" if ok else "🚫 py_compile 실패\n" + tail(out, 1600))
    if not ok:
        return False, checks, warnings

    alias_problems = static_alias_check(path)
    if alias_problems:
        checks.append("🚫 미정의 alias 의심\n" + "\n".join(alias_problems[:10]))
        return False, checks, warnings
    checks.append("✅ 미정의 alias 사전검사 통과")

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


def recent_journal(lines: int = 80, since: Optional[str] = None) -> str:
    cmd = ["journalctl", "-u", MAIN_SERVICE, "--no-pager"]
    if since:
        cmd += ["--since", since]
    else:
        cmd += ["-n", str(lines)]
    rc, out = run(cmd, timeout=20)
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


def apply_latest_auto(force: bool = False, explicit: Optional[str] = None) -> str:
    started = now()
    steps = []
    status_path = BOT_DIR / UPGRADE_STATUS_FILE
    try:
        ok, git_steps = git_update()
        steps.extend(git_steps)
        if not ok:
            write_json(status_path, {"time": now(), "ok": False, "stage": "git", "steps": steps})
            return "🚫 업그레이드 실패 /gupgrade\n- GitHub 갱신 실패\n\n" + "\n\n".join(steps)

        if explicit:
            target = BOT_DIR / explicit
        else:
            target = find_latest_source_file()
        if not target:
            return "🚫 업그레이드 실패 /gupgrade\n- 수익형_v*.py 파일을 못 찾음"

        target_ver = version_from_filename(target)
        current_ver = BotVersion.parse(extract_bot_version(BOT_DIR / "bot.py"))
        if current_ver and target_ver and target_ver < current_ver and not force:
            return f"🚫 업그레이드 중단\n- 최신 파일이 현재보다 낮음: {target.name} < {current_ver}\n- 강제 적용은 /gupgrade force {target.name}"

        valid, checks, warnings = validate_target(target)
        if not valid:
            write_json(status_path, {"time": now(), "ok": False, "stage": "validate", "target": target.name, "checks": checks, "warnings": warnings})
            return "🚫 업그레이드 실패 /gupgrade\n- 적용 전 검수 실패\n\n" + "\n".join(checks + warnings)

        dst = BOT_DIR / "bot.py"
        if not dst.exists():
            return "🚫 업그레이드 실패\n- bot.py가 없음"
        backup = BOT_DIR / f"bot.py.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(dst, backup)

        tmp = BOT_DIR / f"bot.py.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(target, tmp)
        if sha256_file(target) != sha256_file(tmp):
            tmp.unlink(missing_ok=True)
            return "🚫 업그레이드 실패\n- 임시파일 복사 hash 검증 실패"

        os.replace(tmp, dst)
        if sha256_file(target) != sha256_file(dst):
            shutil.copy2(backup, dst)
            return f"🚫 업그레이드 실패\n- bot.py 교체 후 hash 검증 실패, 백업 복구: {backup.name}"

        ok, out = py_compile(dst)
        if not ok:
            shutil.copy2(backup, dst)
            return f"🚫 업그레이드 실패\n- 교체된 bot.py compile 실패, 백업 복구: {backup.name}\n{tail(out, 1800)}"

        (BOT_DIR / ".deployed_target").write_text(target.name + "\n", encoding="utf-8")

        rc, out = run(["systemctl", "restart", MAIN_SERVICE], timeout=30)
        time.sleep(max(3, RESTART_WAIT_SEC))
        active = is_main_service_active()
        log_since = recent_journal(since=started)
        fatal = has_fatal_log(log_since)

        if active != "active" or fatal:
            reason = f"재시작 후 active={active}, fatal_log={fatal}"
            bad = BOT_DIR / f"bot.py.bad_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            try:
                shutil.copy2(dst, bad)
            except Exception:
                pass
            if AUTO_ROLLBACK:
                rb = rollback_to_file(backup, reason=reason)
                write_json(status_path, {"time": now(), "ok": False, "stage": "post_restart", "target": target.name, "backup": backup.name, "rolled_back": True, "reason": reason})
                return f"🚫 업그레이드 실패 후 자동복구\n- target: {target.name}\n- reason: {reason}\n- bad backup: {bad.name if bad.exists() else '?'}\n\n{rb}\n\n[최근 로그]\n{tail(log_since, 1800)}"
            write_json(status_path, {"time": now(), "ok": False, "stage": "post_restart", "target": target.name, "backup": backup.name, "rolled_back": False, "reason": reason})
            return f"🚫 업그레이드 실패\n- target: {target.name}\n- reason: {reason}\n- 자동복구 꺼짐\n\n[최근 로그]\n{tail(log_since, 2200)}"

        data = {
            "time": now(), "ok": True, "target": target.name, "backup": backup.name,
            "target_hash": short_hash(target), "bot_hash": short_hash(dst), "active": active,
            "warnings": warnings,
        }
        write_json(status_path, data)
        bot_version = extract_bot_version(dst)
        warn_text = "\n".join(f"- ⚠️ {w}" for w in warnings) if warnings else "- 없음"
        return (
            f"🚀 업그레이드 성공 /gupgrade\n"
            f"- target: {target.name}\n"
            f"- bot.py: {bot_version}\n"
            f"- backup: {backup.name}\n"
            f"- active: {active}\n"
            f"- hash: {short_hash(dst)}\n"
            f"- 경고:\n{warn_text}\n\n"
            f"다음 확인: /gdeploy 또는 메인봇 /batch /core /trade /deploy"
        )
    except Exception as e:
        write_json(status_path, {"time": now(), "ok": False, "stage": "exception", "error": f"{type(e).__name__}: {e}"})
        return f"🚫 업그레이드 예외\n- {type(e).__name__}: {e}"


def deploy_status() -> str:
    bot_version, target, deployed, latest = get_bot_versions()
    active = is_main_service_active()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    latest_path = BOT_DIR / latest if latest != "?" else None
    latest_ver = extract_bot_version(latest_path) if latest_path else "?"
    last = "없음"
    if status:
        last = f"{status.get('time','?')} / {'성공' if status.get('ok') else '실패'} / {status.get('target') or status.get('stage') or '?'}"
    return (
        f"🧭 가드 배포상태 /gdeploy\n"
        f"✅ 결론\n"
        f"- 메인봇 active: {active}\n"
        f"- 최신 코드파일 자동선택 가능: {latest}\n\n"
        f"📊 버전\n"
        f"- bot.py: {bot_version}\n"
        f"- GitHub/로컬 최신: {latest_ver} ({latest})\n"
        f"- DEPLOY_TARGET(참고만): {target}\n"
        f"- .deployed_target: {deployed}\n\n"
        f"📌 최근 업그레이드\n- {last}\n\n"
        f"명령\n- /gupgrade : 최신 수익형_v*.py 자동 적용\n- /glog : 최근 오류로그\n- /grollback : 최신 백업 복구"
    )


def service_status() -> str:
    active = is_main_service_active()
    bot_version, target, deployed, latest = get_bot_versions()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE)
    last_ok = "없음"
    if status:
        last_ok = f"{status.get('time','?')} / {'성공' if status.get('ok') else '실패'} / {status.get('target') or status.get('stage') or '?'}"
    return (
        f"🛡 가드봇 /guard\n"
        f"✅ 결론\n"
        f"- 메인봇: {active}\n"
        f"- 업그레이드는 코드파일 하나만 올리고 /gupgrade\n\n"
        f"📊 상태\n"
        f"- guard: {VERSION}\n"
        f"- service: {MAIN_SERVICE}\n"
        f"- bot.py: {bot_version}\n"
        f"- 최신 수익형 파일: {latest}\n"
        f"- 최근 적용: {last_ok}\n\n"
        f"🧭 자주 쓰는 명령\n"
        f"- /gdeploy 배포상태\n"
        f"- /gupgrade 최신 자동적용\n"
        f"- /glog 최근 오류\n"
        f"- /grestart 재시작\n"
        f"- /grollback 롤백"
    )


def glog(lines: int = 80) -> str:
    out = recent_journal(lines=lines)
    focus = []
    for line in (out or "").splitlines():
        if any(x in line for x in ["Traceback", "NameError", "SyntaxError", "ImportError", "Exception", "ERROR", "Failed", "failure"]):
            focus.append(line)
    if focus:
        head = "🚨 오류 의심 줄\n" + "\n".join(focus[-20:])
    else:
        head = "✅ 최근 로그에서 뚜렷한 Python 오류 줄 없음"
    return f"🧾 최근 로그 /glog {lines}\n\n{head}\n\n[tail]\n{tail(out, 2500)}"


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
        "/gdeploy - 배포/버전/최근 적용상태\n"
        "/gupgrade - GitHub 최신 수익형_v*.py 자동 적용\n"
        "/glog - 최근 오류 로그\n\n"
        "복구\n"
        "/grestart - 메인봇 재시작\n"
        "/gbackups - 백업 목록\n"
        "/grollback - 최신 백업으로 롤백\n"
        "/gunlock - 배포 lock 삭제\n\n"
        "호환 명령\n"
        "/status /check /lastlog /restart /backups /rollback /apply_latest\n\n"
        "운영법: GitHub에 수익형_v2.xx.yy.py 하나만 올리고 /gupgrade"
    )


def set_commands() -> None:
    commands = [
        {"command": "guard", "description": "가드 핵심 상태"},
        {"command": "gdeploy", "description": "배포/버전/최근 적용상태"},
        {"command": "gupgrade", "description": "최신 수익형 파일 자동 적용"},
        {"command": "glog", "description": "최근 오류 로그"},
        {"command": "grestart", "description": "메인봇 재시작"},
        {"command": "grollback", "description": "최신 백업 롤백"},
        {"command": "gbackups", "description": "백업 목록"},
        {"command": "gmenu", "description": "가드 메뉴"},
    ]
    try:
        api("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)}, timeout=15)
    except Exception as e:
        print(f"setMyCommands failed: {e}", file=sys.stderr)


def handle_command(text: str) -> str:
    parts = text.strip().split()
    cmd = parts[0].lower() if parts else ""
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


def main():
    print(f"[{now()}] guard bot started. service={MAIN_SERVICE} dir={BOT_DIR} version={VERSION}", flush=True)
    offset = None
    last_active = None
    last_notify = 0
    set_commands()
    send(ALLOWED_CHAT_ID, f"🛡 백가 가드봇 시작\n- {VERSION}\n- 관리대상: {MAIN_SERVICE}\n- 업그레이드: /gupgrade")

    while True:
        try:
            params = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            data = api("getUpdates", params, timeout=35)

            for upd in data.get("result", []):
                offset = upd.get("update_id", 0) + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat", {})
                chat_id = str(chat.get("id", ""))
                text = (msg.get("text") or "").strip()
                if chat_id != ALLOWED_CHAT_ID:
                    if text:
                        send(chat_id, "허용되지 않은 사용자입니다.")
                    continue
                if text.startswith("/"):
                    reply = handle_command(text)
                    send(chat_id, reply)

            active = is_main_service_active()
            if last_active is None:
                last_active = active
            elif active != last_active:
                last_active = active
                if time.time() - last_notify > 20:
                    last_notify = time.time()
                    send(ALLOWED_CHAT_ID, f"⚠️ 메인봇 상태 변경\n- active: {active}\n- 시각: {now()}\n\n{tail(glog(50), 2500)}")

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[{now()}] loop error: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(3)
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
