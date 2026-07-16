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
ExecStart=$APP_ROOT/assistant_web/.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port $APP_PORT
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

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_buffering off;
        proxy_read_timeout 300;
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
