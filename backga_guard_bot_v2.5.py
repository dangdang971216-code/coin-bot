#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backga_guard_bot_v2.5.py
- /gupgrade에서 메인봇 + 페이퍼봇을 함께 확인한다.
- DEPLOY_TARGETS.json이 있으면 그 기준을 우선 사용한다.
- 메인봇은 bot.py로 교체 후 MAIN_SERVICE 재시작.
- 페이퍼봇은 paper_bot.py 활성파일로 교체 후 PAPER_SERVICE 재시작, 서비스가 없으면 pid 기반 재시작.
- 전략/조건/청산/BUY_READY는 건드리지 않는다. 배포/실행 관리 전용.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "backga_guard_bot v2.5"


def _load_env_file(path: Path) -> None:
    try:
        if not path.exists():
            return
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
_load_env_file(BASE_DIR / "guard.env")

TOKEN = os.getenv("GUARD_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("GUARD_CHAT_ID", "").strip()
BOT_DIR = Path(os.getenv("BOT_DIR", str(BASE_DIR))).resolve()
MAIN_SERVICE = os.getenv("MAIN_SERVICE", "tradingbot").strip()
PAPER_SERVICE = os.getenv("PAPER_SERVICE", "tradingbot-paper").strip()
GIT_BRANCH = os.getenv("GIT_BRANCH", "main").strip()
PYTHON_BIN = os.getenv("PYTHON_BIN", sys.executable or "python3").strip()

DEPLOY_TARGET_FILE = "DEPLOY_TARGET.txt"
DEPLOY_TARGETS_FILE = "DEPLOY_TARGETS.json"
UPGRADE_STATUS_FILE = ".guard_upgrade_status.json"
DEPLOYED_MAIN_FILE = ".deployed_target"
DEPLOYED_PAPER_FILE = ".deployed_paper_target"
PAPER_ACTIVE_FILE = os.getenv("PAPER_ACTIVE_FILE", "paper_bot.py").strip()
PAPER_PID_FILE = os.getenv("PAPER_PID_FILE", "paper_bot.pid").strip()
PAPER_LOG_FILE = os.getenv("PAPER_LOG_FILE", "paper_bot_process.log").strip()
PAPER_ERR_FILE = os.getenv("PAPER_ERR_FILE", "paper_bot_process.err").strip()


@dataclass(order=True, frozen=True)
class BotVersion:
    major: int
    minor: int
    patch: int
    extra: int = 0

    @classmethod
    def parse(cls, text: str) -> Optional["BotVersion"]:
        m = re.search(r"v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
        if not m:
            return None
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4) or 0))

    def __str__(self) -> str:
        base = f"v{self.major}.{self.minor}.{self.patch}"
        return base if self.extra == 0 else f"{base}.{self.extra}"


@dataclass(order=True, frozen=True)
class PaperVersion:
    major: int
    minor: int

    @classmethod
    def parse(cls, text: str) -> Optional["PaperVersion"]:
        m = re.search(r"paper_bot_v(\d+)\.(\d+)", str(text or ""))
        if not m:
            return None
        return cls(int(m.group(1)), int(m.group(2)))

    def __str__(self) -> str:
        return f"paper_bot_v{self.major}.{self.minor}"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run(cmd: List[str], cwd: Optional[Path] = None, timeout: int = 30) -> Tuple[int, str]:
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


def tail(text: str, n: int = 3500) -> str:
    text = text or ""
    return text if len(text) <= n else "...\n" + text[-n:]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


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


def py_compile(path: Path) -> Tuple[bool, str]:
    returncode, out = run([PYTHON_BIN, "-m", "py_compile", str(path)], cwd=BOT_DIR, timeout=45)
    return returncode == 0, out


def extract_assignment(path: Path, key: str) -> str:
    text = read_text(path)
    vals = re.findall(rf"^\s*{re.escape(key)}\s*=\s*[\'\"]([^\'\"]+)", text, flags=re.M)
    return vals[-1] if vals else "?"


def extract_bot_version(path: Path) -> str:
    return extract_assignment(path, "BOT_VERSION")


def extract_paper_version(path: Path) -> str:
    v = extract_assignment(path, "VERSION")
    if v == "?":
        # fallback: filename version
        pv = PaperVersion.parse(path.name)
        return str(pv) if pv else "?"
    return v


def version_from_filename(path: Path) -> Optional[BotVersion]:
    return BotVersion.parse(path.name)


def paper_version_from_filename(path: Path) -> Optional[PaperVersion]:
    return PaperVersion.parse(path.name)


def find_latest_source_file() -> Optional[Path]:
    candidates = []
    for p in BOT_DIR.glob("수익형_v*.py"):
        v = version_from_filename(p)
        if v:
            candidates.append((v, p))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0])[-1][1]


def find_latest_paper_file() -> Optional[Path]:
    candidates = []
    for p in BOT_DIR.glob("paper_bot_v*.py"):
        v = paper_version_from_filename(p)
        if v:
            candidates.append((v, p))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0])[-1][1]


def read_targets() -> Dict[str, str]:
    data = read_json(BOT_DIR / DEPLOY_TARGETS_FILE, {})
    out: Dict[str, str] = {}
    if isinstance(data, dict):
        main = data.get("main_bot") or data.get("main") or data.get("target")
        paper = data.get("paper_bot") or data.get("paper")
        if main:
            out["main"] = str(main)
        if paper:
            out["paper"] = str(paper)
    if "main" not in out:
        main_txt = read_text(BOT_DIR / DEPLOY_TARGET_FILE)
        if main_txt:
            out["main"] = main_txt.splitlines()[0].strip()
    return out


def git_update() -> Tuple[bool, List[str]]:
    steps = []
    if not (BOT_DIR / ".git").exists():
        return True, ["- git: .git 없음 → 로컬 파일 기준"]
    rc, out = run(["git", "fetch", "origin", GIT_BRANCH], cwd=BOT_DIR, timeout=60)
    steps.append(f"- git fetch: rc {rc}\n{tail(out, 900)}")
    if rc != 0:
        return False, steps
    rc, out = run(["git", "pull", "--ff-only", "origin", GIT_BRANCH], cwd=BOT_DIR, timeout=90)
    steps.append(f"- git pull: rc {rc}\n{tail(out, 900)}")
    return rc == 0, steps


def static_alias_check(path: Path) -> List[str]:
    # 가볍게만 확인. 너무 엄격하면 정상 파일을 막을 수 있음.
    text = read_text(path)
    problems = []
    risky = ["CommandHandler(", "BOT_VERSION"]
    for s in risky:
        if s not in text:
            problems.append(f"필수 문자열 확인 필요: {s}")
    return problems


def validate_main_target(path: Path) -> Tuple[bool, List[str], List[str]]:
    checks: List[str] = []
    warnings: List[str] = []
    if not path.exists():
        return False, [f"대상 파일 없음: {path.name}"], warnings
    if not version_from_filename(path):
        return False, [f"파일명 형식 오류: {path.name}"], warnings
    ok, out = py_compile(path)
    checks.append("✅ main py_compile 통과" if ok else "🚫 main py_compile 실패\n" + tail(out, 1600))
    if not ok:
        return False, checks, warnings
    problems = static_alias_check(path)
    if problems:
        warnings.extend(problems)
    ver = extract_bot_version(path)
    checks.append(f"✅ main 내부버전: {ver}") if ver != "?" else warnings.append("main BOT_VERSION 못 찾음")
    return True, checks, warnings


def validate_paper_target(path: Path) -> Tuple[bool, List[str], List[str]]:
    checks: List[str] = []
    warnings: List[str] = []
    if not path.exists():
        return False, [f"대상 파일 없음: {path.name}"], warnings
    if not paper_version_from_filename(path):
        return False, [f"파일명 형식 오류: {path.name}"], warnings
    ok, out = py_compile(path)
    checks.append("✅ paper py_compile 통과" if ok else "🚫 paper py_compile 실패\n" + tail(out, 1600))
    if not ok:
        return False, checks, warnings
    text = read_text(path)
    for required in ["VERSION", "paper_bot_status.json", "/pbatch", "/pstatus"]:
        if required not in text:
            warnings.append(f"paper 필수 문자열 확인 필요: {required}")
    checks.append(f"✅ paper 내부버전: {extract_paper_version(path)}")
    return True, checks, warnings


def is_service_active(service: str) -> str:
    rc, out = run(["systemctl", "is-active", service], timeout=8)
    return (out or "?").strip()


def service_exists(service: str) -> bool:
    rc, out = run(["systemctl", "status", service, "--no-pager"], timeout=8)
    return rc in (0, 3) or "Loaded:" in out or "Active:" in out


def restart_main_service() -> Tuple[int, str, str]:
    rc, out = run(["systemctl", "restart", MAIN_SERVICE], timeout=35)
    time.sleep(3)
    return rc, out, is_service_active(MAIN_SERVICE)


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


def restart_paper_bot() -> Tuple[str, List[str]]:
    notes: List[str] = []
    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    pid_path = BOT_DIR / PAPER_PID_FILE

    if service_exists(PAPER_SERVICE):
        rc, out = run(["systemctl", "restart", PAPER_SERVICE], timeout=35)
        time.sleep(3)
        active = is_service_active(PAPER_SERVICE)
        notes.append(f"paper service restart rc={rc} active={active}")
        if out:
            notes.append(tail(out, 700))
        return active, notes

    old_pid = read_pid(pid_path)
    if old_pid and pid_alive(old_pid):
        stop_pid(old_pid)
        notes.append(f"기존 paper pid 종료: {old_pid}")

    token = os.getenv("PAPER_BOT_TOKEN", "").strip()
    if not token:
        notes.append("PAPER_BOT_TOKEN 없음 → 파일 교체만 완료, 자동 실행은 보류")
        return "token_missing", notes

    env = os.environ.copy()
    log_f = open(BOT_DIR / PAPER_LOG_FILE, "a", encoding="utf-8")
    err_f = open(BOT_DIR / PAPER_ERR_FILE, "a", encoding="utf-8")
    try:
        p = subprocess.Popen(
            [PYTHON_BIN, str(active_path), "--bot"],
            cwd=str(BOT_DIR),
            env=env,
            stdout=log_f,
            stderr=err_f,
            start_new_session=True,
        )
        (BOT_DIR / PAPER_PID_FILE).write_text(str(p.pid), encoding="utf-8")
        time.sleep(2)
        alive = pid_alive(p.pid)
        notes.append(f"paper direct start pid={p.pid} alive={alive}")
        return "active" if alive else "failed", notes
    finally:
        try:
            log_f.close()
            err_f.close()
        except Exception:
            pass


def apply_main(target: Path, force: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"name": "main", "target": target.name, "changed": False, "ok": False, "warnings": []}
    dst = BOT_DIR / "bot.py"
    if not dst.exists():
        result.update({"ok": False, "error": "bot.py 없음"})
        return result
    current_ver = BotVersion.parse(extract_bot_version(dst))
    target_ver = version_from_filename(target)
    if current_ver and target_ver and target_ver < current_ver and not force:
        result.update({"ok": False, "error": f"현재보다 낮은 버전: {target.name} < {current_ver}"})
        return result
    valid, checks, warnings = validate_main_target(target)
    result["checks"] = checks
    result["warnings"] = warnings
    if not valid:
        result.update({"ok": False, "error": "main 검수 실패"})
        return result
    same_hash = dst.exists() and short_hash(dst) == short_hash(target)
    result["same_hash"] = same_hash
    if same_hash and not force:
        result.update({"ok": True, "changed": False, "active": is_service_active(MAIN_SERVICE), "hash": short_hash(dst)})
        return result
    backup = BOT_DIR / f"bot.py.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(dst, backup)
    tmp = BOT_DIR / f"bot.py.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(target, tmp)
    if short_hash(tmp) != short_hash(target):
        tmp.unlink(missing_ok=True)
        result.update({"ok": False, "error": "main 임시복사 hash 불일치"})
        return result
    os.replace(tmp, dst)
    if short_hash(dst) != short_hash(target):
        shutil.copy2(backup, dst)
        result.update({"ok": False, "error": "main 교체 후 hash 불일치, 백업복구", "backup": backup.name})
        return result
    (BOT_DIR / DEPLOYED_MAIN_FILE).write_text(target.name, encoding="utf-8")
    rc, out, active = restart_main_service()
    result.update({
        "ok": active == "active",
        "changed": True,
        "backup": backup.name,
        "hash": short_hash(dst),
        "restart_rc": rc,
        "restart_out": tail(out, 900),
        "active": active,
        "bot_version": extract_bot_version(dst),
    })
    return result


def apply_paper(target: Path, force: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {"name": "paper", "target": target.name, "changed": False, "ok": False, "warnings": []}
    valid, checks, warnings = validate_paper_target(target)
    result["checks"] = checks
    result["warnings"] = warnings
    if not valid:
        result.update({"ok": False, "error": "paper 검수 실패"})
        return result

    active_path = BOT_DIR / PAPER_ACTIVE_FILE
    same_hash = active_path.exists() and short_hash(active_path) == short_hash(target)
    result["same_hash"] = same_hash
    if same_hash and not force:
        active, notes = restart_paper_bot()
        # hash가 같아도 실행 프로세스가 구버전일 수 있어서 restart는 수행한다.
        result.update({"ok": active in {"active", "token_missing"}, "changed": False, "active": active, "notes": notes, "hash": short_hash(active_path), "paper_version": extract_paper_version(active_path)})
        return result

    backup = None
    if active_path.exists():
        backup = BOT_DIR / f"{active_path.name}.backup_guard_apply_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(active_path, backup)
    tmp = BOT_DIR / f"{active_path.name}.guard_tmp_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(target, tmp)
    if short_hash(tmp) != short_hash(target):
        tmp.unlink(missing_ok=True)
        result.update({"ok": False, "error": "paper 임시복사 hash 불일치"})
        return result
    os.replace(tmp, active_path)
    if short_hash(active_path) != short_hash(target):
        if backup:
            shutil.copy2(backup, active_path)
        result.update({"ok": False, "error": "paper 교체 후 hash 불일치, 백업복구"})
        return result
    (BOT_DIR / DEPLOYED_PAPER_FILE).write_text(target.name, encoding="utf-8")
    active, notes = restart_paper_bot()
    result.update({
        "ok": active in {"active", "token_missing"},
        "changed": True,
        "backup": backup.name if backup else "없음",
        "hash": short_hash(active_path),
        "active": active,
        "notes": notes,
        "paper_version": extract_paper_version(active_path),
        "active_file": active_path.name,
    })
    return result


def get_versions() -> Dict[str, Any]:
    latest_main = find_latest_source_file()
    latest_paper = find_latest_paper_file()
    targets = read_targets()
    main_target = BOT_DIR / targets["main"] if targets.get("main") else latest_main
    paper_target = BOT_DIR / targets["paper"] if targets.get("paper") else latest_paper
    return {
        "main_service": is_service_active(MAIN_SERVICE),
        "paper_service": is_service_active(PAPER_SERVICE) if service_exists(PAPER_SERVICE) else "no_service",
        "bot_py_version": extract_bot_version(BOT_DIR / "bot.py"),
        "paper_active_version": extract_paper_version(BOT_DIR / PAPER_ACTIVE_FILE),
        "latest_main": latest_main.name if latest_main else "?",
        "latest_main_version": extract_bot_version(latest_main) if latest_main else "?",
        "latest_paper": latest_paper.name if latest_paper else "?",
        "latest_paper_version": extract_paper_version(latest_paper) if latest_paper else "?",
        "target_main": main_target.name if main_target else "?",
        "target_paper": paper_target.name if paper_target else "?",
        "deployed_main": read_text(BOT_DIR / DEPLOYED_MAIN_FILE) or "?",
        "deployed_paper": read_text(BOT_DIR / DEPLOYED_PAPER_FILE) or "?",
        "paper_active_file": PAPER_ACTIVE_FILE,
    }


def format_result(res: Dict[str, Any]) -> str:
    ok = "✅" if res.get("ok") else "🚫"
    changed = "교체" if res.get("changed") else "유지/재시작"
    lines = [f"{ok} {res.get('name')} {changed}"]
    for k in ["target", "bot_version", "paper_version", "active_file", "active", "backup", "hash", "error"]:
        if k in res and res.get(k) not in (None, ""):
            lines.append(f"- {k}: {res.get(k)}")
    notes = res.get("notes") or []
    if notes:
        lines.append("- notes:")
        lines.extend([f"  · {str(n)[:220]}" for n in notes[:5]])
    warnings = res.get("warnings") or []
    if warnings:
        lines.append("- warnings:")
        lines.extend([f"  · {w}" for w in warnings[:8]])
    return "\n".join(lines)


def apply_latest_auto(force: bool = False, explicit: Optional[str] = None) -> str:
    started = time.time()
    status_path = BOT_DIR / UPGRADE_STATUS_FILE
    steps: List[str] = []
    try:
        ok, git_steps = git_update()
        steps.extend(git_steps)
        if not ok:
            write_json(status_path, {"time": now(), "ok": False, "stage": "git", "steps": steps})
            return "🚫 업그레이드 실패 /gupgrade\n- GitHub 갱신 실패\n\n" + "\n\n".join(steps)

        targets = read_targets()
        if explicit:
            targets["main"] = explicit
        main_target = BOT_DIR / targets["main"] if targets.get("main") else find_latest_source_file()
        paper_target = BOT_DIR / targets["paper"] if targets.get("paper") else find_latest_paper_file()
        if not main_target or not main_target.exists():
            return "🚫 업그레이드 실패 /gupgrade\n- 메인봇 대상 파일을 못 찾음"
        if not paper_target or not paper_target.exists():
            # 페이퍼봇이 아직 GitHub에 없으면 메인은 진행하되 paper는 유지한다.
            paper_missing = True
        else:
            paper_missing = False

        main_res = apply_main(main_target, force=force)
        paper_res: Dict[str, Any]
        if paper_missing:
            paper_res = {"name": "paper", "ok": True, "changed": False, "target": targets.get("paper", "?"), "active": "유지", "warnings": ["paper_bot 대상 파일 없음 → 기존 실행 유지"]}
        else:
            paper_res = apply_paper(paper_target, force=force)

        all_ok = bool(main_res.get("ok")) and bool(paper_res.get("ok"))
        data = {
            "time": now(),
            "ok": all_ok,
            "main": main_res,
            "paper": paper_res,
            "steps": steps,
            "elapsed_sec": round(time.time() - started, 1),
            "version": VERSION,
        }
        write_json(status_path, data)
        title = "🚀 ✅ 업그레이드 성공 /gupgrade" if all_ok else "🚀 ⚠️ 업그레이드 일부 확인 필요 /gupgrade"
        return "\n".join([
            title,
            f"- guard: {VERSION}",
            f"- total: {round(time.time() - started, 1)}s",
            "",
            "[메인봇]",
            format_result(main_res),
            "",
            "[페이퍼봇]",
            format_result(paper_res),
            "",
            "다음 확인:",
            "- 가드봇: /gdeploy",
            "- 메인봇: /batch /paper_handoff /deploy",
            "- 페이퍼봇: /pstatus /pbatch /perror",
        ])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        write_json(status_path, {"time": now(), "ok": False, "stage": "exception", "error": err, "traceback": traceback.format_exc()})
        return f"🚫 업그레이드 예외\n- {err}"


def deploy_status() -> str:
    v = get_versions()
    status = read_json(BOT_DIR / UPGRADE_STATUS_FILE, {})
    last = "없음"
    if isinstance(status, dict) and status:
        last = f"{status.get('time','?')} / {'성공' if status.get('ok') else '확인필요'}"
    return "\n".join([
        "🧭 가드 배포상태 /gdeploy",
        "✅ 결론",
        f"- 메인봇: {v['main_service']} / bot.py {v['bot_py_version']}",
        f"- 페이퍼봇: {v['paper_service']} / {v['paper_active_file']} {v['paper_active_version']}",
        "",
        "📊 GitHub/로컬 대상",
        f"- main target: {v['target_main']} / latest {v['latest_main_version']} ({v['latest_main']})",
        f"- paper target: {v['target_paper']} / latest {v['latest_paper_version']} ({v['latest_paper']})",
        f"- .deployed_target: {v['deployed_main']}",
        f"- .deployed_paper_target: {v['deployed_paper']}",
        "",
        f"📌 최근 업그레이드: {last}",
        "",
        "명령",
        "- /gupgrade : main + paper_bot 동시 확인/교체",
        "- /grestart : 메인봇 재시작",
        "- /gpaper_restart : 페이퍼봇 재시작",
        "- /glog : 메인봇 최근 로그",
    ])


def service_status() -> str:
    v = get_versions()
    return "\n".join([
        "🛡 가드봇 /guard",
        f"- guard: {VERSION}",
        f"- 메인봇: {v['main_service']} / {v['bot_py_version']}",
        f"- 페이퍼봇: {v['paper_service']} / {v['paper_active_version']}",
        "- 업그레이드: /gupgrade 한 번으로 main + paper_bot 확인",
    ])


def recent_journal(service: str, lines: int = 80) -> str:
    rc, out = run(["journalctl", "-u", service, "--no-pager", "-n", str(lines)], timeout=20)
    return out or ""


def glog(lines: int = 80) -> str:
    out = recent_journal(MAIN_SERVICE, lines=lines)
    return f"🧾 메인봇 최근 로그 /glog {lines}\n\n{tail(out, 3500)}"


def gpaper_log(lines: int = 80) -> str:
    if service_exists(PAPER_SERVICE):
        out = recent_journal(PAPER_SERVICE, lines=lines)
        return f"🧾 페이퍼봇 최근 로그 /gpaperlog {lines}\n\n{tail(out, 3500)}"
    out = read_text(BOT_DIR / PAPER_ERR_FILE) or read_text(BOT_DIR / PAPER_LOG_FILE)
    return f"🧾 페이퍼봇 최근 로그 /gpaperlog\n\n{tail(out, 3500)}"


def restart_service() -> str:
    rc, out, active = restart_main_service()
    return f"🔁 메인봇 재시작 /grestart\n- rc: {rc}\n- active: {active}\n- bot.py: {extract_bot_version(BOT_DIR / 'bot.py')}\n\n{tail(out, 1600)}"


def restart_paper_command() -> str:
    active, notes = restart_paper_bot()
    return "\n".join(["🔁 페이퍼봇 재시작 /gpaper_restart", f"- active: {active}", f"- version: {extract_paper_version(BOT_DIR / PAPER_ACTIVE_FILE)}", *[f"- {n}" for n in notes]])


def collect_backups() -> List[Path]:
    files: List[Path] = []
    for pat in ["bot.py.backup_*", "bot.py.bad_*", "bot.py.bak_*", "bot.py.before_guard_rollback_*"]:
        files.extend(BOT_DIR.glob(pat))
    return sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def list_backups_text() -> str:
    files = collect_backups()[:12]
    if not files:
        return "📦 백업 목록\n- 없음"
    return "📦 백업 목록\n" + "\n".join([f"- {p.name}" for p in files])


def help_text() -> str:
    return "\n".join([
        "🛡 가드봇 명령어",
        "/guard - 핵심 상태",
        "/gdeploy - main + paper 배포 상태",
        "/gupgrade - main + paper 동시 최신 확인/교체",
        "/grestart - 메인봇 재시작",
        "/gpaper_restart - 페이퍼봇 재시작",
        "/glog - 메인봇 로그",
        "/gpaperlog - 페이퍼봇 로그",
        "/gbackups - 메인봇 백업 목록",
    ])


def api(method: str, params: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def send(chat_id: Any, text: str) -> None:
    if not text:
        text = "(empty)"
    s = str(text)
    chunks = []
    while len(s) > 3900:
        chunks.append(s[:3900])
        s = s[3900:]
    chunks.append(s)
    for chunk in chunks:
        try:
            api("sendMessage", {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}, timeout=15)
        except Exception as e:
            print(f"send failed: {e}", file=sys.stderr)


def set_commands() -> None:
    commands = [
        {"command": "guard", "description": "가드 핵심 상태"},
        {"command": "gdeploy", "description": "main+paper 배포상태"},
        {"command": "gupgrade", "description": "main+paper 최신 적용"},
        {"command": "glog", "description": "메인봇 로그"},
        {"command": "gpaperlog", "description": "페이퍼봇 로그"},
        {"command": "grestart", "description": "메인봇 재시작"},
        {"command": "gpaper_restart", "description": "페이퍼봇 재시작"},
        {"command": "gbackups", "description": "백업 목록"},
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
    if cmd in ("/gupgrade", "/apply_latest", "/upgradebot"):
        force = bool(args and args[0].lower() == "force")
        explicit = None
        if force and len(args) > 1:
            explicit = args[1]
        elif args and args[0].endswith(".py"):
            explicit = args[0]
        return apply_latest_auto(force=force, explicit=explicit)
    if cmd in ("/grestart", "/restart"):
        return restart_service()
    if cmd in ("/gpaper_restart", "/gpaperrestart", "/prestart_guard"):
        return restart_paper_command()
    if cmd in ("/glog", "/lastlog"):
        n = int(args[0]) if args and args[0].isdigit() else 80
        return glog(max(20, min(300, n)))
    if cmd in ("/gpaperlog", "/paperlog"):
        n = int(args[0]) if args and args[0].isdigit() else 80
        return gpaper_log(max(20, min(300, n)))
    if cmd in ("/gbackups", "/backups"):
        return list_backups_text()
    return "알 수 없는 명령어야.\n\n" + help_text()


def main() -> int:
    if not TOKEN:
        print("GUARD_BOT_TOKEN missing. Put it in guard.env", file=sys.stderr)
        return 2
    if not ALLOWED_CHAT_ID:
        print("GUARD_CHAT_ID missing. Put it in guard.env", file=sys.stderr)
        return 2
    print(f"[{now()}] guard bot started. dir={BOT_DIR} version={VERSION}", flush=True)
    offset = None
    set_commands()
    send(ALLOWED_CHAT_ID, f"🛡 백가 가드봇 시작\n- {VERSION}\n- /gupgrade: main + paper_bot 동시 확인")
    while True:
        try:
            params: Dict[str, Any] = {"timeout": 25}
            if offset is not None:
                params["offset"] = offset
            data = api("getUpdates", params, timeout=35)
            for upd in data.get("result", []):
                offset = int(upd.get("update_id", 0)) + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                if not text or not chat_id:
                    continue
                if str(chat_id) != str(ALLOWED_CHAT_ID):
                    send(chat_id, "허용된 채팅방이 아니야.")
                    continue
                reply = handle_command(text)
                send(chat_id, reply)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"loop error: {type(e).__name__}: {e}", file=sys.stderr)
            time.sleep(3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
