#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
APP_PORT="${APP_PORT:-8787}"
TARGET_RELEASE="${TARGET_RELEASE:-}"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"

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
