#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/content-ai-assistant}"
APP_USER="${APP_USER:-ubuntu}"
APP_PORT="${APP_PORT:-8787}"
SERVER_NAME="${SERVER_NAME:-_}"
ACME_WEBROOT="${ACME_WEBROOT:-/var/lib/content-ai-assistant-acme}"
STATE_DIR="${STATE_DIR:-/var/lib/content-ai-assistant}"
RELEASE_DIR="${RELEASE_DIR:-}"
RELEASE_ID="${RELEASE_ID:-}"
RELEASE_KEEP="${RELEASE_KEEP:-2}"
RELEASES_DIR="$APP_ROOT/releases"
CURRENT_LINK="$APP_ROOT/current"
SERVICE_FILE="/etc/systemd/system/content-ai-assistant.service"
NGINX_FILE="/etc/nginx/sites-available/content-ai-assistant"

if ! id "$APP_USER" >/dev/null 2>&1; then
  echo "User '$APP_USER' does not exist. Set APP_USER before running this script." >&2
  exit 1
fi
if [[ -z "$RELEASE_DIR" || -z "$RELEASE_ID" ]]; then
  echo "RELEASE_DIR and RELEASE_ID are required." >&2
  exit 1
fi
if [[ "$RELEASE_DIR" != "$RELEASES_DIR/$RELEASE_ID" || ! "$RELEASE_ID" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
  echo "Release path is outside the managed releases directory: $RELEASE_DIR" >&2
  exit 1
fi
if [[ ! -d "$RELEASE_DIR/assistant_web" || ! -L "$RELEASE_DIR/data" ]]; then
  echo "Release is incomplete: $RELEASE_DIR" >&2
  exit 1
fi

APP_DIR="$RELEASE_DIR/assistant_web"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nginx sqlite3 curl
sudo install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$APP_ROOT" "$RELEASES_DIR"
sudo install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$STATE_DIR"

# 첫 원자 배포에서는 기존 in-place 설치를 legacy 릴리스로 감싸 롤백 대상으로 보존한다.
if [[ ! -L "$CURRENT_LINK" ]]; then
  legacy_release="$RELEASES_DIR/legacy"
  install -d -m 755 "$legacy_release"
  [[ -e "$legacy_release/assistant_web" ]] || ln -s "$APP_ROOT/assistant_web" "$legacy_release/assistant_web"
  [[ -e "$legacy_release/data" ]] || ln -s "$APP_ROOT/data" "$legacy_release/data"
  [[ -e "$legacy_release/scripts" ]] || ln -s "$APP_ROOT/scripts" "$legacy_release/scripts"
  current_temp="$APP_ROOT/.current-legacy-$$"
  ln -s "$legacy_release" "$current_temp"
  mv -Tf "$current_temp" "$CURRENT_LINK"
fi
previous_current="$(readlink -f "$CURRENT_LINK")"

# 릴리스별 가상환경을 완성한 뒤에만 활성 링크를 바꾼다.
cd "$APP_DIR"
python3 -m venv .venv
. .venv/bin/activate
pip install --no-cache-dir --upgrade pip
pip install --no-cache-dir -r requirements.lock

# nginx/systemd를 건드리기 전에 신릴리스의 운영 보안 설정과 DB 마이그레이션을 검증한다.
APP_ENV=production \
PUBLIC_BASE_URL="https://$SERVER_NAME" \
AUTH_COOKIE_SECURE=true \
ASSISTANT_RUNTIME_DIR="$STATE_DIR" \
AUTH_USERS_DB_PATH="$STATE_DIR/users.sqlite3" \
WORKSPACE_DB_PATH="$STATE_DIR/workspace.sqlite3" \
AUDIT_DB_PATH="$STATE_DIR/assistant_audit.sqlite3" \
CHROMA_DB_PATH="$RELEASE_DIR/data/mantis_chroma_db/chroma.sqlite3" \
FIREBASE_SERVICE_ACCOUNT_PATH="$STATE_DIR/firebase-service-account.json" \
  .venv/bin/python - <<'PY'
from pathlib import Path

from backend.environment import load_dotenv

load_dotenv(Path.cwd() / ".env")

from backend.startup_validation import validate_runtime_security
from backend.auth import init_users_db
from backend.audit import init_audit_db
from backend.workspace import init_workspace_db

validate_runtime_security()
init_users_db()
init_workspace_db()
init_audit_db()
print("Release preflight passed")
PY

rollback_dir="$(mktemp -d /tmp/content-ai-assistant-deploy.XXXXXX)"
service_existed=0
nginx_existed=0
if sudo test -f "$SERVICE_FILE"; then
  sudo cp -a "$SERVICE_FILE" "$rollback_dir/service"
  service_existed=1
fi
if sudo test -f "$NGINX_FILE"; then
  sudo cp -a "$NGINX_FILE" "$rollback_dir/nginx"
  nginx_existed=1
fi

atomic_switch_current() {
  local target="$1"
  local temporary_link="$APP_ROOT/.current-$RELEASE_ID-$$"
  rm -f -- "$temporary_link"
  ln -s "$target" "$temporary_link"
  mv -Tf "$temporary_link" "$CURRENT_LINK"
}

rollback_deployment() {
  local exit_code=$?
  trap - ERR
  set +e
  echo "Activation failed; rolling current back to $previous_current" >&2
  atomic_switch_current "$previous_current"
  if [[ "$service_existed" == "1" ]]; then
    sudo cp -a "$rollback_dir/service" "$SERVICE_FILE"
  else
    sudo rm -f -- "$SERVICE_FILE"
  fi
  if [[ "$nginx_existed" == "1" ]]; then
    sudo cp -a "$rollback_dir/nginx" "$NGINX_FILE"
  else
    sudo rm -f -- "$NGINX_FILE"
  fi
  sudo systemctl daemon-reload
  sudo systemctl restart content-ai-assistant
  if sudo nginx -t; then
    sudo systemctl reload nginx
  fi
  rm -rf -- "$rollback_dir"
  exit "$exit_code"
}
trap rollback_deployment ERR

sudo install -d -m 755 "$ACME_WEBROOT/.well-known/acme-challenge"
if ! command -v snap >/dev/null 2>&1; then
  echo "snap is required to install Certbot 5.4+ for IP certificates." >&2
  false
fi
if ! snap list certbot >/dev/null 2>&1; then
  sudo snap install certbot --classic
fi

CERTBOT_ARGS=(
  certonly
  --non-interactive
  --agree-tos
  --webroot
  --webroot-path "$ACME_WEBROOT"
)
if [[ -n "${LETSENCRYPT_EMAIL:-}" ]]; then
  CERTBOT_ARGS+=(--email "$LETSENCRYPT_EMAIL")
else
  CERTBOT_ARGS+=(--register-unsafely-without-email)
fi
if [[ "$SERVER_NAME" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  CERTBOT_ARGS+=(--preferred-profile shortlived --ip-address "$SERVER_NAME")
else
  CERTBOT_ARGS+=(--domain "$SERVER_NAME")
fi

certificate_path="/etc/letsencrypt/live/$SERVER_NAME/fullchain.pem"
if [[ -f "$certificate_path" ]]; then
  # 기존 최종 nginx 구성은 이미 ACME 경로를 제공하므로 active config를 바꾸기 전에 갱신한다.
  sudo /snap/bin/certbot renew --quiet
else
  # 최초 발급만 임시 HTTP bootstrap이 필요하다. 이후 어떤 실패도 trap이 이전 설정으로 복구한다.
  sudo tee "$NGINX_FILE" >/dev/null <<NGINX
server {
    listen 80;
    server_name $SERVER_NAME;

    location ^~ /.well-known/acme-challenge/ {
        root $ACME_WEBROOT;
        default_type text/plain;
    }

    location / {
        return 503;
    }
}
NGINX
  sudo ln -sf "$NGINX_FILE" /etc/nginx/sites-enabled/content-ai-assistant
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t
  sudo systemctl reload nginx
  sudo /snap/bin/certbot "${CERTBOT_ARGS[@]}"
fi

sudo install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-content-ai-nginx >/dev/null <<'HOOK'
#!/usr/bin/env bash
set -e
nginx -t
systemctl reload nginx
HOOK
sudo chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-content-ai-nginx

# 최종 nginx 파일은 먼저 검증하되 새 릴리스가 준비되기 전에는 reload하지 않는다.
sudo tee "$NGINX_FILE" >/dev/null <<NGINX
server {
    listen 80;
    server_name $SERVER_NAME;

    location ^~ /.well-known/acme-challenge/ {
        root $ACME_WEBROOT;
        default_type text/plain;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl;
    server_name $SERVER_NAME;

    ssl_certificate /etc/letsencrypt/live/$SERVER_NAME/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$SERVER_NAME/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_timeout 1d;
    ssl_session_cache shared:ContentAIAssistantTLS:10m;

    client_max_body_size 20m;
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
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 300;
    }

    error_page 503 @maintenance;
    location @maintenance {
        root $CURRENT_LINK/assistant_web/deploy/aws;
        rewrite ^ /maintenance.html break;
        add_header Retry-After 300 always;
        add_header Cache-Control "no-store" always;
    }
}
NGINX
sudo nginx -t

sudo tee "$SERVICE_FILE" >/dev/null <<SERVICE
[Unit]
Description=Content Service AI Assistant
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$CURRENT_LINK/assistant_web
Environment=PYTHONUNBUFFERED=1
Environment=APP_ENV=production
Environment=PUBLIC_BASE_URL=https://$SERVER_NAME
Environment=AUTH_COOKIE_SECURE=true
Environment=ASSISTANT_RUNTIME_DIR=$STATE_DIR
Environment=AUTH_USERS_DB_PATH=$STATE_DIR/users.sqlite3
Environment=WORKSPACE_DB_PATH=$STATE_DIR/workspace.sqlite3
Environment=AUDIT_DB_PATH=$STATE_DIR/assistant_audit.sqlite3
Environment=CHROMA_DB_PATH=$CURRENT_LINK/data/mantis_chroma_db/chroma.sqlite3
Environment=FIREBASE_SERVICE_ACCOUNT_PATH=$STATE_DIR/firebase-service-account.json
UMask=0077
ExecStart=$CURRENT_LINK/assistant_web/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port $APP_PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable content-ai-assistant
atomic_switch_current "$RELEASE_DIR"
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
  echo "New release did not become ready." >&2
  false
fi

sudo systemctl reload nginx
trap - ERR
rm -rf -- "$rollback_dir"

# 성공한 뒤에만 오래된 불변 릴리스와 그 전용 스냅샷을 정리한다.
mapfile -t managed_releases < <(
  find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '20??????T??????Z' -printf '%f\n' | sort -r
)
if (( ${#managed_releases[@]} > RELEASE_KEEP )); then
  for expired_id in "${managed_releases[@]:RELEASE_KEEP}"; do
    expired_release="$RELEASES_DIR/$expired_id"
    if [[ "$(readlink -f "$CURRENT_LINK")" != "$expired_release" ]]; then
      rm -rf -- "$expired_release" "$APP_ROOT/snapshots/$expired_id"
    fi
  done
fi

# 실제 불변 릴리스가 2개 이상이면 legacy 없이도 즉시 롤백할 수 있다. 이때에만
# 외부 상태 디렉터리와 무관한 구 in-place 배포 복사본을 제거해 디스크를 회수한다.
if (( ${#managed_releases[@]} >= 2 )) && [[ -d "$RELEASES_DIR/legacy" ]]; then
  if [[ "$(readlink -f "$CURRENT_LINK")" != "$RELEASES_DIR/legacy" ]]; then
    rm -rf -- \
      "$RELEASES_DIR/legacy" \
      "$APP_ROOT/assistant_web" \
      "$APP_ROOT/data" \
      "$APP_ROOT/scripts"
    echo "Removed legacy in-place deployment after two immutable releases were verified."
  fi
fi

echo "Done. Activated release=$RELEASE_ID previous=$previous_current"
