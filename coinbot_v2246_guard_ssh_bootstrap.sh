#!/usr/bin/env bash
set -Eeuo pipefail

BOT_DIR="${BOT_DIR:-/home/dangdang971216/trading_bot}"
GUARD_SERVICE="${GUARD_SERVICE:-tradingbot-guard}"
ZIP_PATH="${1:-$BOT_DIR/coinbot_update_v2246.zip}"
EXPECTED_SHA256="384616cd000ac336ec0573d646d08b6c9417a3994cd659ef635831fa152f9d0b"
TARGET_NAME="backga_guard_bot_v2_6_64.py"
ALIAS_NAME="backga_guard_bot.py"

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

fail() {
  echo "❌ $*" >&2
  exit 1
}

[[ -d "$BOT_DIR" ]] || fail "BOT_DIR 없음: $BOT_DIR"
[[ -f "$ZIP_PATH" ]] || fail "적용 ZIP 없음: $ZIP_PATH"

cd "$BOT_DIR"

actual_sha="$(sha256sum "$ZIP_PATH" | awk '{print $1}')"
[[ "$actual_sha" == "$EXPECTED_SHA256" ]] || fail "ZIP SHA256 불일치: $actual_sha"

free_kb="$(df -Pk "$BOT_DIR" | awk 'NR==2 {print $4}')"
[[ "${free_kb:-0}" -ge 524288 ]] || fail "남은 용량 512MB 미만. 디스크 정리를 먼저 끝내세요."

stamp="$(date +%Y%m%d_%H%M%S)"
recovery_dir="$BOT_DIR/runtime/manual_guard_bootstrap_v2246_$stamp"
mkdir -p "$recovery_dir"

echo "1/6 기존 v2244 임시 업그레이드 상태 보존 이동"
for name in \
  .guard_post_upgrade_request.json \
  .guard_post_upgrade_request.inflight.json \
  .guard_release_apply.lock \
  .guard_release_once_v1000.lock \
  .guard_release_final_pending.json
do
  if [[ -e "$BOT_DIR/$name" ]]; then
    mv "$BOT_DIR/$name" "$recovery_dir/"
  fi
done

tmp_dir="$(mktemp -d "$BOT_DIR/.v2246_guard_bootstrap.XXXXXX")"
cleanup() {
  rm -rf "$tmp_dir"
}
trap cleanup EXIT

echo "2/6 v2246 Guard만 안전 추출·문법검사"
python3 - "$ZIP_PATH" "$tmp_dir" "$TARGET_NAME" <<'PY'
import sys, zipfile
zip_path, out_dir, target = sys.argv[1:]
with zipfile.ZipFile(zip_path) as z:
    bad = z.testzip()
    if bad:
        raise SystemExit(f"ZIP 손상: {bad}")
    if target not in z.namelist():
        raise SystemExit(f"Guard 파일 누락: {target}")
    z.extract(target, out_dir)
PY
python3 -m py_compile "$tmp_dir/$TARGET_NAME"

echo "3/6 기존 Guard만 중지"
"${SUDO[@]}" systemctl --no-block stop "$GUARD_SERVICE" || true
sleep 2
"${SUDO[@]}" systemctl kill --kill-whom=all -s SIGKILL "$GUARD_SERVICE" 2>/dev/null || true
sleep 1

guard_pid="$("${SUDO[@]}" systemctl show "$GUARD_SERVICE" -p MainPID --value 2>/dev/null || echo 0)"
[[ "${guard_pid:-0}" == "0" ]] || fail "Guard 프로세스가 아직 남음: PID=$guard_pid"

echo "4/6 기존 alias 보존 후 v2246 Guard 원자 교체"
if [[ -f "$BOT_DIR/$ALIAS_NAME" ]]; then
  cp -a "$BOT_DIR/$ALIAS_NAME" "$recovery_dir/$ALIAS_NAME.before"
fi
if [[ -f "$BOT_DIR/.deployed_guard_target" ]]; then
  cp -a "$BOT_DIR/.deployed_guard_target" "$recovery_dir/.deployed_guard_target.before"
fi

target_tmp="$BOT_DIR/.$TARGET_NAME.tmp.$stamp"
alias_tmp="$BOT_DIR/.$ALIAS_NAME.tmp.$stamp"

cp -a "$tmp_dir/$TARGET_NAME" "$target_tmp"
chmod 0755 "$target_tmp"
mv -f "$target_tmp" "$BOT_DIR/$TARGET_NAME"

cp -a "$BOT_DIR/$TARGET_NAME" "$alias_tmp"
chmod 0755 "$alias_tmp"
mv -f "$alias_tmp" "$BOT_DIR/$ALIAS_NAME"

printf '%s\n' "$TARGET_NAME" > "$BOT_DIR/.deployed_guard_target"

target_sha="$(sha256sum "$BOT_DIR/$TARGET_NAME" | awk '{print $1}')"
alias_sha="$(sha256sum "$BOT_DIR/$ALIAS_NAME" | awk '{print $1}')"
[[ "$target_sha" == "$alias_sha" ]] || fail "Guard target/alias hash 불일치"

echo "5/6 Guard 서비스 시작"
"${SUDO[@]}" systemctl reset-failed "$GUARD_SERVICE" 2>/dev/null || true
"${SUDO[@]}" systemctl start "$GUARD_SERVICE"
sleep 5

active="$("${SUDO[@]}" systemctl is-active "$GUARD_SERVICE" 2>/dev/null || true)"
pid="$("${SUDO[@]}" systemctl show "$GUARD_SERVICE" -p MainPID --value 2>/dev/null || echo 0)"
[[ "$active" == "active" ]] || fail "Guard 서비스 active 아님: $active"
[[ "${pid:-0}" =~ ^[1-9][0-9]*$ ]] || fail "Guard MainPID 없음: $pid"

cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
[[ "$cmdline" == *"backga_guard_bot"* ]] || fail "Guard 실제 cmdline 불일치: $cmdline"

echo "6/6 복구 완료"
echo "✅ Guard SSH 부트스트랩 PASS"
echo "- service: $GUARD_SERVICE"
echo "- active: $active"
echo "- MainPID: $pid"
echo "- target: $TARGET_NAME"
echo "- target/alias SHA256: $target_sha"
echo "- cmdline: $cmdline"
echo "- 기존 임시상태 보존: $recovery_dir"
echo
echo "다음: Telegram 가드봇에서 /status 확인 후 /gupgrade_bundle 1회 실행"
echo "자동매수 OFF"
