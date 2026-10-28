#!/usr/bin/env bash
# Mac의 Ollama를 EC2 loopback에만 연결하고 로그인 시 터널을 복구한다.
set -euo pipefail

: "${EC2_HOST:?EC2_HOST를 지정하세요}"
: "${SSH_KEY:?EC2 관리자 SSH_KEY를 지정하세요}"
[[ "$(uname -s)" == Darwin ]] || { echo 'macOS에서 실행하세요.' >&2; exit 1; }
[[ "$EC2_HOST" =~ ^[a-zA-Z0-9.-]+$ ]] || exit 1
EC2_USER="${EC2_USER:-ubuntu}"
[[ "$EC2_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 1
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
TUNNEL_KEY="$HOME/.ssh/assistance_model_tunnel_ed25519"
LABEL='kr.assistance.model-tunnel'
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$ROOT_DIR/assistant_web/runtime/model-tunnel"
REMOTE="$EC2_USER@$EC2_HOST"
SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10)

# 기존 서버 보안 설정을 변경하지 않는다. wildcard 바인딩 서버에서는 중단한다.
gateway_ports="$(ssh "${SSH_OPTS[@]}" "$REMOTE" 'sudo sshd -T | sed -n "s/^gatewayports //p"')"
[[ "$gateway_ports" == no ]] || { echo 'GatewayPorts=no인 서버가 필요합니다.' >&2; exit 1; }
curl --fail --silent --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null
umask 077
mkdir -p "$HOME/.ssh" "$HOME/Library/LaunchAgents" "$LOG_DIR"
if [[ ! -f "$TUNNEL_KEY" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C assistance-model-tunnel -f "$TUNNEL_KEY"
fi
public_key="$(ssh-keygen -y -f "$TUNNEL_KEY")"
authorized_key="restrict,port-forwarding,permitlisten=\"127.0.0.1:11434\",permitopen=\"127.0.0.1:11434\",command=\"/bin/false\" $public_key assistance-model-tunnel"
ssh "${SSH_OPTS[@]}" "$REMOTE" '
  set -eu
  umask 077
  mkdir -p "$HOME/.ssh"
  touch "$HOME/.ssh/authorized_keys"
  IFS= read -r key
  if ! grep -qxF "$key" "$HOME/.ssh/authorized_keys"; then
    printf "\n%s\n" "$key" >> "$HOME/.ssh/authorized_keys"
  fi
' <<< "$authorized_key"

"$ROOT_DIR/assistant_web/.venv/bin/python" - "$PLIST" "$LABEL" "$TUNNEL_KEY" "$REMOTE" "$LOG_DIR" <<'PY'
import os
import plistlib
import sys
from pathlib import Path

path, label, key, remote, logs = sys.argv[1:]
arguments = ['/usr/bin/ssh', '-NT', '-i', key,
             '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
             '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
             '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
             '-o', 'ExitOnForwardFailure=yes',
             '-R', '127.0.0.1:11434:127.0.0.1:11434', remote]
config = {'Label': label, 'ProgramArguments': arguments, 'RunAtLoad': True,
          'KeepAlive': True, 'ThrottleInterval': 15, 'ProcessType': 'Background',
          'StandardOutPath': str(Path(logs) / 'stdout.log'),
          'StandardErrorPath': str(Path(logs) / 'stderr.log')}
staged = Path(path + '.tmp')
staged.write_bytes(plistlib.dumps(config))
os.chmod(staged, 0o600)
os.replace(staged, path)
PY
plutil -lint "$PLIST"
domain="gui/$(id -u)"
if launchctl print "$domain/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$domain/$LABEL"
fi
launchctl bootstrap "$domain" "$PLIST"
echo "설치 완료: $LABEL (상태: launchctl print $domain/$LABEL)"
echo "EC2 연결 확인: SSH 접속 후 curl --fail http://127.0.0.1:11434/api/tags"
