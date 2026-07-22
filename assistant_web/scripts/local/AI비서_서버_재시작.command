#!/bin/zsh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORT="8787"

cd "$APP_DIR"

echo "컨텐츠서비스 AI 비서 서버를 재시작합니다."
echo "위치: $APP_DIR"
echo "포트: $PORT"
echo

PIDS=$(lsof -ti tcp:"$PORT" || true)
if [ -n "$PIDS" ]; then
  echo "기존 서버 종료 중..."
  echo "$PIDS" | xargs kill -9
  sleep 1
else
  echo "실행 중인 기존 서버가 없습니다."
fi

echo
echo "서버 시작 중..."
echo "브라우저 주소: http://127.0.0.1:$PORT"
echo "종료하려면 이 터미널에서 Ctrl + C 를 누르세요."
echo

python3 -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT" --reload
