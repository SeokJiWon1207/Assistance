#!/usr/bin/env bash
# 검색 요청 밖에서 최신 목록·변경 본문을 갱신한다.
set -euo pipefail
APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
APP_USER="${APP_USER:-ubuntu}"
STATE_DIR="${STATE_DIR:-/var/lib/content-ai-assistant}"
sudo tee /etc/systemd/system/content-ai-notion-snapshot.service >/dev/null <<EOF
[Unit]
Description=Notion 전체 검색 저장본 갱신
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_ROOT/current/assistant_web
Environment=ASSISTANT_RUNTIME_DIR=$STATE_DIR
ExecStart=$APP_ROOT/current/assistant_web/.venv/bin/python scripts/refresh_notion_snapshot.py --max-seconds 3600 --max-pages 1000
TimeoutStartSec=4300
UMask=0077
Nice=10
MemoryMax=384M
EOF
sudo tee /etc/systemd/system/content-ai-notion-snapshot.timer >/dev/null <<'EOF'
[Unit]
Description=Notion 저장본 갱신 및 미완료 수집 재개

[Timer]
OnCalendar=hourly
RandomizedDelaySec=120
Persistent=true
Unit=content-ai-notion-snapshot.service

[Install]
WantedBy=timers.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now content-ai-notion-snapshot.timer
