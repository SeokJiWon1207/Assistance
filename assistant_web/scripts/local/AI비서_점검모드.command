#!/bin/zsh

# 점검 모드를 켜고 끈다. 서버 재시작 없이 즉시 반영된다.
# 켜면 접속한 사용자는 채팅 대신 "서버 점검중입니다." 안내를 보게 된다.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
FLAG_PATH="$APP_DIR/runtime/maintenance.json"

cd "$APP_DIR"

echo "컨텐츠서비스 AI 비서 — 점검 모드"
echo "위치: $APP_DIR"
echo

if [ -f "$FLAG_PATH" ]; then
  echo "현재 상태: 점검 중"
  python3 -c "
import json, sys
try:
    data = json.load(open('$FLAG_PATH', encoding='utf-8'))
except Exception:
    data = {}
print('  안내 문구:', data.get('message', '서버 점검중입니다.'))
if data.get('note'):
    print('  추가 안내:', data['note'])
if data.get('startedAt'):
    print('  시작 시각:', data['startedAt'])
" || true
  echo
  printf "점검을 종료할까요? (y/N) "
  read -r answer
  if [ "$answer" = "y" ] || [ "$answer" = "Y" ]; then
    python3 -c "
import sys; sys.path.insert(0, '$APP_DIR')
from backend.maintenance import stop_maintenance
stop_maintenance()
print('점검 모드를 해제했습니다. 사용자 화면은 15초 안에 자동으로 돌아옵니다.')
"
  else
    echo "그대로 두었습니다."
  fi
else
  echo "현재 상태: 정상 운영 중"
  echo
  echo "표시할 안내 문구를 입력하세요. 그냥 Enter 를 누르면 '서버 점검중입니다.'"
  printf "> "
  read -r message
  echo "추가로 안내할 내용이 있으면 입력하세요(예: 예상 종료 시각). 없으면 Enter"
  printf "> "
  read -r note

  MAINT_MESSAGE="$message" MAINT_NOTE="$note" python3 -c "
import os, sys; sys.path.insert(0, '$APP_DIR')
from backend.maintenance import start_maintenance
state = start_maintenance(os.environ.get('MAINT_MESSAGE', ''), os.environ.get('MAINT_NOTE', ''))
print()
print('점검 모드를 켰습니다.')
print('  안내 문구:', state['message'])
if state['note']:
    print('  추가 안내:', state['note'])
print('  시작 시각:', state['startedAt'])
print()
print('접속 중인 사용자에게는 15초 안에 안내 화면이 표시됩니다.')
print('해제하려면 이 커맨드를 다시 실행하세요.')
"
fi

echo
printf "창을 닫으려면 Enter 를 누르세요. "
read -r _
