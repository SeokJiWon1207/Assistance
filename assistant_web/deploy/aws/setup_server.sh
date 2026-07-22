#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
APP_USER="${APP_USER:-ubuntu}"
APP_PORT="${APP_PORT:-8787}"
SERVER_NAME="${SERVER_NAME:-_}"

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "User '$APP_USER' does not exist. Set APP_USER before running this script." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx

sudo mkdir -p "$APP_ROOT"
sudo chown -R "$APP_USER:$APP_USER" "$APP_ROOT"

cd "$APP_ROOT/assistant_web"
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

sudo tee /etc/systemd/system/content-ai-assistant.service >/dev/null <<SERVICE
[Unit]
Description=Content Service AI Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_ROOT/assistant_web
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_ROOT/assistant_web/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port $APP_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

sudo tee /etc/nginx/sites-available/content-ai-assistant >/dev/null <<NGINX
server {
    listen 80;
    server_name $SERVER_NAME;

    client_max_body_size 20m;

    # 점검 모드: $APP_ROOT/maintenance.on 이 있으면 앱으로 넘기지 않고 안내를 내려준다.
    # 앱(uvicorn)이 아니라 nginx가 판단하므로 재배포·재시작으로 앱이 완전히 죽어 있어도
    # 502 대신 안내가 보인다. 요청마다 파일을 확인하므로 nginx 재시작도 필요 없다.
    set \$maintenance_flag "$APP_ROOT/maintenance.on";

    location / {
        if (-f \$maintenance_flag) {
            return 503;
        }
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 300;
    }

    # 점검 안내는 503으로 내려준다. 200으로 주면 모니터링과 크롤러가 정상 응답으로
    # 오해한다. Retry-After 로 잠시 뒤 다시 오라는 뜻을 함께 전한다.
    error_page 503 @maintenance;
    location @maintenance {
        root $APP_ROOT/assistant_web/deploy/aws;
        rewrite ^ /maintenance.html break;
        add_header Retry-After 300 always;
        add_header Cache-Control "no-store" always;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/content-ai-assistant /etc/nginx/sites-enabled/content-ai-assistant
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t

sudo systemctl daemon-reload
sudo systemctl enable content-ai-assistant
sudo systemctl restart content-ai-assistant
sudo systemctl reload nginx

echo "Done. App is proxied on http://$SERVER_NAME/ and service listens on 127.0.0.1:$APP_PORT."
