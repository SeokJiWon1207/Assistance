#!/bin/zsh

# 운영 서버(EC2)의 점검 모드를 켜고 끈다.
#
# nginx가 플래그 파일을 보고 판단하므로 배포도, 서버 재시작도 필요 없다.
# 켜는 즉시 반영되고, 앱이 재배포로 내려가 있어도 안내 화면이 유지된다.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EC2_HOST="43.202.45.113"
SSH_KEY="$SCRIPT_DIR/Assistance_Key.pem"
EC2_USER="ubuntu"
APP_ROOT="/opt/content-ai-assistant"
FLAG_PATH="$APP_ROOT/maintenance.on"

echo "컨텐츠서비스 AI 비서 — 운영 서버 점검 모드"
echo "대상 서버: $EC2_HOST"
echo "접속 주소: http://$EC2_HOST/"
echo

if [ ! -f "$SSH_KEY" ]; then
  echo "오류: SSH 키를 찾을 수 없습니다: $SSH_KEY"
  echo
  printf "Enter 를 누르면 창을 닫습니다. "
  read -r _
  exit 1
fi

chmod 400 "$SSH_KEY"

SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 $EC2_USER@$EC2_HOST"

echo "현재 상태를 확인하는 중..."
if ! STATE=$($SSH "test -f '$FLAG_PATH' && echo on || echo off" 2>/dev/null); then
  echo
  echo "오류: 서버에 접속하지 못했습니다."
  echo "네트워크 또는 EC2 상태를 확인해 주세요."
  echo
  printf "Enter 를 누르면 창을 닫습니다. "
  read -r _
  exit 1
fi

echo

if [ "$STATE" = "on" ]; then
  echo "현재 상태: 점검 중"
  SINCE=$($SSH "cat '$FLAG_PATH' 2>/dev/null | head -1" || true)
  if [ -n "$SINCE" ]; then
    echo "  $SINCE"
  fi
  echo
  printf "점검을 종료할까요? (y/N) "
  read -r answer
  if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    $SSH "rm -f '$FLAG_PATH'"
    echo
    echo "점검 모드를 해제했습니다."
    echo "사용자는 새로고침하면 바로 정상 화면을 봅니다."
  else
    echo "그대로 두었습니다."
  fi
else
  echo "현재 상태: 정상 운영 중"
  echo
  echo "점검 모드를 켜면 접속한 사용자는 \"서버 점검중입니다.\" 안내를 보게 됩니다."
  printf "점검을 시작할까요? (y/N) "
  read -r answer
  if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    STAMP="점검 시작: $(date '+%Y-%m-%d %H:%M:%S')"
    $SSH "echo '$STAMP' > '$FLAG_PATH'"
    echo
    echo "점검 모드를 켰습니다. ($STAMP)"
    echo "이제 재배포나 서버 재시작을 해도 안내 화면이 계속 표시됩니다."
    echo "해제하려면 이 커맨드를 다시 실행하세요."
  else
    echo "그대로 두었습니다."
  fi
fi

echo
printf "Enter 를 누르면 창을 닫습니다. "
read -r _
