#!/usr/bin/env bash
# 시스템 python3로 폴백하지 않고 프로젝트의 major.minor 버전을 검증한다.
set -euo pipefail
if [[ "$#" != "1" || ! -r "$1" ]]; then
  echo "Usage: select_python.sh PROJECT_PYTHON_VERSION_FILE" >&2
  exit 1
fi
required_version="$(<"$1")"
candidate="${DEPLOY_PYTHON:-python3.12}"
if ! candidate="$(command -v "$candidate")"; then
  echo "Python $required_version 실행 파일을 찾을 수 없습니다. python3.12를 준비하거나 DEPLOY_PYTHON을 지정하세요." >&2
  exit 1
fi
if ! "$candidate" - "$required_version" <<'PY'
import sys
expected = tuple(int(part) for part in sys.argv[1].strip().split('.')[:2])
if len(expected) != 2 or sys.version_info[:2] != expected:
    print(f"Python {sys.argv[1].strip()} 계열이 필요합니다. 현재 {sys.version.split()[0]}", file=sys.stderr)
    raise SystemExit(1)
PY
then
  exit 1
fi
printf '%s\n' "$candidate"
