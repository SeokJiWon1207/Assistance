#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
EC2_HOST="43.202.45.113"
SSH_KEY="$SCRIPT_DIR/Assistance_Key.pem"

cd "$PROJECT_DIR"

echo "컨텐츠서비스 AI 비서 AWS 시연 서버 재배포"
echo "프로젝트: $PROJECT_DIR"
echo "EC2: $EC2_HOST"
echo

if [ ! -f "$SSH_KEY" ]; then
  echo "오류: SSH 키를 찾을 수 없습니다: $SSH_KEY"
  echo "아무 키나 누르면 창을 닫습니다."
  read -k 1
  exit 1
fi

chmod 400 "$SSH_KEY"

EC2_HOST="$EC2_HOST" \
SSH_KEY="$SSH_KEY" \
INCLUDE_ENV=1 \
"$SCRIPT_DIR/deploy_to_ec2.sh"

echo
echo "재배포 완료"
echo "접속 주소: http://$EC2_HOST/"
echo
echo "아무 키나 누르면 창을 닫습니다."
read -k 1
