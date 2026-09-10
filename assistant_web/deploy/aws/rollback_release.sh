#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
APP_PORT="${APP_PORT:-8787}"
TARGET_RELEASE="${TARGET_RELEASE:-}"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
STATE_DIR="${STATE_DIR:-/var/lib/content-ai-assistant}"
exec 9>"$APP_ROOT/.deployment.lock"
flock -n 9 || { echo "Another deployment or rollback is running." >&2; exit 1; }

if [[ -z "$TARGET_RELEASE" ]]; then
  echo "Usage: TARGET_RELEASE=YYYYMMDDTHHMMSSZ $0" >&2
  echo "Available releases:" >&2
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '  %f\n' | sort -r >&2
  exit 1
fi
if [[ ! "$TARGET_RELEASE" =~ ^[0-9]{8}T[0-9]{6}Z$ && "$TARGET_RELEASE" != "legacy" ]]; then
  echo "Invalid TARGET_RELEASE: $TARGET_RELEASE" >&2
  exit 1
fi

target="$RELEASES_DIR/$TARGET_RELEASE"
if [[ ! -x "$target/assistant_web/.venv/bin/python" || ! -e "$target/data/mantis_chroma_db/chroma.sqlite3" ]]; then
  echo "Target release is incomplete: $target" >&2
  exit 1
fi
# 대상 코드가 현재 DB의 복제본을 읽을 수 없으면 링크를 바꾸지 않는다.
# 오래된 백업으로 자동 복원해 최신 사용자 기록을 잃지 않도록 실패 시 중단한다.
"$CURRENT_LINK/assistant_web/.venv/bin/python" \
  "$CURRENT_LINK/assistant_web/scripts/runtime_databases.py" check \
  --state "$STATE_DIR" --app "$target/assistant_web"
previous="$(readlink -f "$CURRENT_LINK")"
temporary_link="$APP_ROOT/.current-rollback-$$"

restore_previous() {
  local exit_code=$?
  trap - ERR
  set +e
  rm -f -- "$temporary_link"
  ln -s "$previous" "$temporary_link"
  mv -Tf "$temporary_link" "$CURRENT_LINK"
  sudo systemctl restart content-ai-assistant
  exit "$exit_code"
}
trap restore_previous ERR

ln -s "$target" "$temporary_link"
mv -Tf "$temporary_link" "$CURRENT_LINK"
sudo systemctl restart content-ai-assistant

ready=0
for _attempt in $(seq 1 30); do
  if curl --max-time 2 -fsS "http://127.0.0.1:$APP_PORT/health" >/dev/null && \
     curl --max-time 5 -fsS "http://127.0.0.1:$APP_PORT/ready" >/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != "1" ]]; then
  echo "Rollback target did not become ready; restoring $previous" >&2
  false
fi

trap - ERR
echo "Rollback complete: $previous -> $target"
