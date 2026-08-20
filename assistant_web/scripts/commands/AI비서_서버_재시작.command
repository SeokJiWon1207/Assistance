#!/bin/zsh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
PORT="8787"
APP_PYTHON="$APP_DIR/.venv/bin/python"
RUNTIME_DIR="$APP_DIR/runtime"
PID_FILE="$RUNTIME_DIR/local-server.pid"

cd "$APP_DIR"

if [ ! -x "$APP_PYTHON" ]; then
  echo "프로젝트 가상환경을 찾을 수 없습니다: $APP_PYTHON"
  echo "먼저 assistant_web 디렉터리에서 가상환경과 의존성을 설치해 주세요."
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/python -m pip install -r requirements.lock"
  exit 1
fi

echo "컨텐츠서비스 AI 비서 서버를 재시작합니다."
echo "위치: $APP_DIR"
echo "포트: $PORT"
echo

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

if [ -f "$PID_FILE" ]; then
  SERVER_PID="$(tr -dc '0-9' < "$PID_FILE")"
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    SERVER_COMMAND="$(ps -p "$SERVER_PID" -o command= 2>/dev/null || true)"
    if [[ "$SERVER_COMMAND" != *"backend.main:app"* ]]; then
      echo "PID 파일이 다른 프로세스를 가리켜 종료하지 않습니다: $SERVER_PID"
      exit 1
    fi
    echo "기존 서버에 정상 종료 신호를 보냅니다. (PID $SERVER_PID)"
    kill -TERM "$SERVER_PID"
    for _ in {1..20}; do
      kill -0 "$SERVER_PID" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$SERVER_PID" 2>/dev/null; then
      echo "정상 종료 제한시간을 초과해 관리 중인 서버만 강제 종료합니다."
      kill -KILL "$SERVER_PID"
    fi
  fi
  rm -f "$PID_FILE"
fi

PORT_PID="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN | head -1 || true)"
if [ -n "$PORT_PID" ]; then
  echo "포트 ${PORT}를 이 스크립트가 관리하지 않는 프로세스(PID $PORT_PID)가 사용 중입니다."
  echo "프로세스를 직접 확인한 뒤 다시 실행해 주세요."
  exit 1
fi

echo
echo "서버 시작 중..."
echo "브라우저 주소: http://127.0.0.1:$PORT"
echo "종료하려면 이 터미널에서 Ctrl + C 를 누르세요."
echo

"$APP_PYTHON" -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT" --reload &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
chmod 600 "$PID_FILE"

cleanup() {
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill -TERM "$SERVER_PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
}
trap cleanup EXIT INT TERM
wait "$SERVER_PID"
