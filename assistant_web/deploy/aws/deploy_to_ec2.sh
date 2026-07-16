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
  INCLUDE_ENV=1        # copy assistant_web/.env to the server for a same-data demo
USAGE
  exit 1
fi

EC2_USER="${EC2_USER:-ubuntu}"
APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
SERVER_NAME="${SERVER_NAME:-$EC2_HOST}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=accept-new)
REMOTE="$EC2_USER@$EC2_HOST"

echo "Creating remote app directory: $APP_ROOT"
ssh "${SSH_OPTS[@]}" "$REMOTE" "sudo mkdir -p '$APP_ROOT/data' '$APP_ROOT/scripts' && sudo chown -R '$EC2_USER:$EC2_USER' '$APP_ROOT'"

echo "Uploading application files..."
rsync -az --delete \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  --exclude '.DS_Store' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '*.pem' \
  --exclude 'assistant_web/.venv' \
  --exclude 'assistant_web/.pycache' \
  --exclude 'assistant_web/artifacts' \
  --exclude 'assistant_web/runtime/accuracy_reports' \
  --exclude 'assistant_web/runtime/assistant_audit.sqlite3' \
  --exclude 'assistant_web/.env' \
  "$ROOT_DIR/assistant_web" \
  "$REMOTE:$APP_ROOT/"

echo "Uploading local search snapshot and maintenance script..."
rsync -az --delete \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  "$ROOT_DIR/data/mantis_chroma_db" \
  "$REMOTE:$APP_ROOT/data/"
rsync -az \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
  "$ROOT_DIR/scripts/make_vector_db.py" \
  "$REMOTE:$APP_ROOT/scripts/"

if [[ "${INCLUDE_ENV:-0}" == "1" ]]; then
  echo "Uploading assistant_web/.env because INCLUDE_ENV=1..."
  rsync -az -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    "$ROOT_DIR/assistant_web/.env" \
    "$REMOTE:$APP_ROOT/assistant_web/.env"
else
  echo "Skipped .env upload. Create $APP_ROOT/assistant_web/.env on the server or rerun with INCLUDE_ENV=1."
fi

echo "Running server setup..."
ssh "${SSH_OPTS[@]}" "$REMOTE" "APP_ROOT='$APP_ROOT' APP_USER='$EC2_USER' SERVER_NAME='$SERVER_NAME' bash '$APP_ROOT/assistant_web/deploy/aws/setup_server.sh'"

echo "Deployment finished: http://$SERVER_NAME/"
