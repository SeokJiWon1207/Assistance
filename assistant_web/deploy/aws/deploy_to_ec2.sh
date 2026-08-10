#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${EC2_HOST:-}" || -z "${SSH_KEY:-}" ]]; then
  cat >&2 <<'USAGE'
Usage:
  EC2_HOST=ec2-public-ip-or-dns SSH_KEY=~/.ssh/key.pem ./assistant_web/deploy/aws/deploy_to_ec2.sh

Optional:
  EC2_USER=ubuntu
  APP_ROOT=/opt/content-ai-assistant
  SERVER_NAME=demo.example.com
  INCLUDE_ENV=1
  FIREBASE_CREDENTIALS=/secure/path/service-account.json
  RELEASE_KEEP=2
USAGE
  exit 1
fi

EC2_USER="${EC2_USER:-ubuntu}"
APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
SERVER_NAME="${SERVER_NAME:-$EC2_HOST}"
RELEASE_KEEP="${RELEASE_KEEP:-2}"
RELEASE_ID="${RELEASE_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

if [[ ! "$RELEASE_ID" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "RELEASE_ID must use YYYYMMDDTHHMMSSZ format: $RELEASE_ID" >&2
  exit 1
fi
if [[ ! "$RELEASE_KEEP" =~ ^[1-9][0-9]*$ ]]; then
  echo "RELEASE_KEEP must be a positive integer." >&2
  exit 1
fi

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
REMOTE="$EC2_USER@$EC2_HOST"
REMOTE_RELEASE="$APP_ROOT/releases/$RELEASE_ID"
REMOTE_SNAPSHOT="$APP_ROOT/snapshots/$RELEASE_ID"
deployment_complete=0

cleanup_failed_staging() {
  local exit_code=$?
  if [[ "$deployment_complete" == "1" || "$exit_code" == "0" ]]; then
    return
  fi
  echo "Deployment failed; removing only the unactivated staging release $RELEASE_ID..." >&2
  ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "APP_ROOT='$APP_ROOT' RELEASE_DIR='$REMOTE_RELEASE' SNAPSHOT_DIR='$REMOTE_SNAPSHOT' bash -s" <<'REMOTE_CLEANUP' || true
set -u
current_target="$(readlink -f "$APP_ROOT/current" 2>/dev/null || true)"
if [[ "$current_target" != "$RELEASE_DIR" ]]; then
  rm -rf -- "$RELEASE_DIR" "$SNAPSHOT_DIR"
fi
REMOTE_CLEANUP
}
trap cleanup_failed_staging EXIT

echo "Preparing immutable release: $RELEASE_ID"
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "sudo install -d -m 755 -o '$EC2_USER' -g '$EC2_USER' \
    '$APP_ROOT' '$APP_ROOT/releases' '$APP_ROOT/snapshots' && \
   install -d -m 755 '$REMOTE_RELEASE/assistant_web' '$REMOTE_RELEASE/scripts' \
    '$REMOTE_SNAPSHOT/mantis_chroma_db'"

echo "Backing up remote runtime databases outside the release tree..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "sudo APP_ROOT='$APP_ROOT' bash -s" <<'REMOTE_BACKUP'
set -euo pipefail

legacy_runtime_dir="$APP_ROOT/assistant_web/runtime"
state_dir="/var/lib/content-ai-assistant"
backup_dir="/var/backups/content-ai-assistant"
stamp="$(date -u +%Y%m%d-%H%M%S)"
found=0

for name in users workspace assistant_audit; do
  source_db="$state_dir/$name.sqlite3"
  if [[ ! -f "$source_db" ]]; then
    source_db="$legacy_runtime_dir/$name.sqlite3"
  fi
  if [[ ! -f "$source_db" ]]; then
    continue
  fi
  found=1
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "sqlite3 is required to back up existing runtime databases." >&2
    exit 1
  fi
  install -d -m 700 -o root -g root "$backup_dir"
  backup_db="$backup_dir/$name-$stamp.sqlite3"
  sqlite3 "$source_db" ".backup '$backup_db'"
  chmod 600 "$backup_db"
  if [[ "$(sqlite3 "$backup_db" 'PRAGMA integrity_check;' | head -1)" != "ok" ]]; then
    echo "Backup integrity check failed: $backup_db" >&2
    exit 1
  fi
  echo "Backed up $name.sqlite3 to $backup_db"
done

if [[ "$found" == "0" ]]; then
  echo "No remote runtime databases exist yet; backup skipped."
fi
REMOTE_BACKUP

echo "Uploading application into $REMOTE_RELEASE..."
rsync -az --delete \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  --exclude '.DS_Store' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pem' \
  --exclude 'firebase-service-account*.json' \
  --exclude '*service-account*.json' \
  --exclude '.venv' \
  --exclude '.pycache' \
  --exclude 'artifacts' \
  --exclude 'runtime/' \
  --exclude '.env' \
  "$ROOT_DIR/assistant_web/" \
  "$REMOTE:$REMOTE_RELEASE/assistant_web/"

rsync -az \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  "$ROOT_DIR/scripts/make_vector_db.py" \
  "$REMOTE:$REMOTE_RELEASE/scripts/"

echo "Uploading versioned search snapshot..."
rsync -az --delete \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  "$ROOT_DIR/data/mantis_chroma_db/" \
  "$REMOTE:$REMOTE_SNAPSHOT/mantis_chroma_db/"
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "ln -s '$REMOTE_SNAPSHOT' '$REMOTE_RELEASE/data'"

if [[ "${INCLUDE_ENV:-0}" == "1" ]]; then
  echo "Uploading release-scoped assistant_web/.env..."
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$ROOT_DIR/assistant_web/.env" \
    "$REMOTE:$REMOTE_RELEASE/assistant_web/.env"
  ssh "${SSH_OPTS[@]}" "$REMOTE" "chmod 600 '$REMOTE_RELEASE/assistant_web/.env'"
else
  echo "INCLUDE_ENV=1 is required for a new immutable production release." >&2
  exit 1
fi

if [[ -n "${FIREBASE_CREDENTIALS:-}" ]]; then
  if [[ ! -f "$FIREBASE_CREDENTIALS" ]]; then
    echo "FIREBASE_CREDENTIALS does not exist: $FIREBASE_CREDENTIALS" >&2
    exit 1
  fi
  echo "Uploading Firebase credentials to the external state directory..."
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$FIREBASE_CREDENTIALS" \
    "$REMOTE:$REMOTE_RELEASE/firebase-service-account.upload.json"
  ssh "${SSH_OPTS[@]}" "$REMOTE" \
    "chmod 600 '$REMOTE_RELEASE/firebase-service-account.upload.json' && \
      sudo install -d -m 700 -o '$EC2_USER' -g '$EC2_USER' '/var/lib/content-ai-assistant' && \
      sudo install -m 600 -o '$EC2_USER' -g '$EC2_USER' \
        '$REMOTE_RELEASE/firebase-service-account.upload.json' \
        '/var/lib/content-ai-assistant/firebase-service-account.json' && \
      rm -f '$REMOTE_RELEASE/firebase-service-account.upload.json'"
fi

echo "Validating uploaded snapshot before activation..."
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "SNAPSHOT_DIR='$REMOTE_SNAPSHOT' python3 -c \"import json, os, sqlite3; from pathlib import Path; root=Path(os.environ['SNAPSHOT_DIR'])/'mantis_chroma_db'; db=root/'chroma.sqlite3'; con=sqlite3.connect(db); integrity=con.execute('PRAGMA integrity_check').fetchone()[0]; count=con.execute(\\\"SELECT COUNT(*) FROM embedding_metadata WHERE key='chroma:document'\\\").fetchone()[0]; con.close(); metadata=json.loads((root/'freshness.json').read_text()); assert integrity=='ok' and count>0 and metadata.get('documentCount')==count, (integrity,count,metadata); print(f'Snapshot validated: {count} documents')\""

echo "Installing and atomically activating release $RELEASE_ID..."
ssh "${SSH_OPTS[@]}" "$REMOTE" \
  "APP_ROOT='$APP_ROOT' APP_USER='$EC2_USER' SERVER_NAME='$SERVER_NAME' \
   RELEASE_DIR='$REMOTE_RELEASE' RELEASE_ID='$RELEASE_ID' RELEASE_KEEP='$RELEASE_KEEP' \
   LETSENCRYPT_EMAIL='${LETSENCRYPT_EMAIL:-}' \
   bash '$REMOTE_RELEASE/assistant_web/deploy/aws/setup_server.sh'"

deployment_complete=1
trap - EXIT
echo "Deployment finished: release=$RELEASE_ID https://$SERVER_NAME/"
