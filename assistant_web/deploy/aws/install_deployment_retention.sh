#!/usr/bin/env bash
# 현재·직전 두 릴리스 보관 정책을 배포 후와 매일 실행한다.
set -euo pipefail
APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
STATE_DIR="${STATE_DIR:-/var/lib/content-ai-assistant}"
APP_USER="${APP_USER:-ubuntu}"
APP_PORT="${APP_PORT:-8787}"
RELEASE_KEEP="${RELEASE_KEEP:-3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBEXEC_DIR=/usr/local/libexec/content-ai-assistant
if [[ ! "$RELEASE_KEEP" =~ ^[0-9]+$ ]] || (( RELEASE_KEEP < 3 )); then
  echo "RELEASE_KEEP must retain the current release and at least two previous releases." >&2
  exit 1
fi
if [[ ! "$APP_PORT" =~ ^[0-9]+$ ]] || (( APP_PORT < 1 || APP_PORT > 65535 )); then
  echo "APP_PORT must be a valid TCP port." >&2
  exit 1
fi
for managed_path in "$APP_ROOT" "$STATE_DIR"; do
  if [[ ! "$managed_path" =~ ^/[a-zA-Z0-9_./-]+$ || "$managed_path" == *..* ]]; then
    echo "Unsupported managed path: $managed_path" >&2
    exit 1
  fi
done
if [[ ! -e "$APP_ROOT/.deployment.lock" ]]; then
  sudo install -o "$APP_USER" -g "$APP_USER" -m 600 /dev/null "$APP_ROOT/.deployment.lock"
fi
sudo install -d -m 755 "$LIBEXEC_DIR"
sudo install -m 644 "$SCRIPT_DIR/../../scripts/prune_deployments.py" "$LIBEXEC_DIR/prune_deployments.py.new"
sudo mv -f "$LIBEXEC_DIR/prune_deployments.py.new" "$LIBEXEC_DIR/prune_deployments.py"
sudo tee /etc/systemd/system/content-ai-deployment-retention.service >/dev/null <<EOF
[Unit]
Description=AI assistant deployment and backup retention
After=content-ai-assistant.service
ConditionPathIsSymbolicLink=$APP_ROOT/current

[Service]
Type=oneshot
User=root
ExecStart=$APP_ROOT/current/assistant_web/.venv/bin/python -B $LIBEXEC_DIR/prune_deployments.py --app-root $APP_ROOT --state $STATE_DIR --port $APP_PORT --keep-releases $RELEASE_KEEP --apply
TimeoutStartSec=600
UMask=0077
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$APP_ROOT/releases $APP_ROOT/backups $APP_ROOT/.deployment.lock $STATE_DIR -/var/backups/content-ai-assistant
EOF
sudo tee /etc/systemd/system/content-ai-deployment-retention.timer >/dev/null <<'EOF'
[Unit]
Description=Daily deployment and backup retention

[Timer]
OnCalendar=daily
RandomizedDelaySec=300
Persistent=true
Unit=content-ai-deployment-retention.service

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now content-ai-deployment-retention.timer
sudo systemctl start content-ai-deployment-retention.service
