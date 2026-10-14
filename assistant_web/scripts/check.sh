#!/usr/bin/env bash
# 로컬·CI·에이전트가 같은 기준으로 실행하는 결정형 검증 묶음.
# 사용법: assistant_web/scripts/check.sh [단계...]  (단계를 생략하면 전체 실행)
set -euo pipefail

ALL_STEPS=(tests compile shell ruff mypy audit)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x assistant_web/.venv/bin/python ]]; then
    PYTHON=assistant_web/.venv/bin/python
  elif [[ "${CI:-}" == "true" ]]; then
    PYTHON=python
  else
    echo "assistant_web/.venv가 없습니다. requirements-dev.lock으로 가상환경을 먼저 만드세요." >&2
    exit 1
  fi
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
  echo "Python 3.12가 필요합니다: $("$PYTHON" --version 2>&1) ($PYTHON)" >&2
  exit 1
fi

run_step() {
  case "$1" in
    tests)
      PYTHONWARNINGS=error::ResourceWarning "$PYTHON" -m coverage run assistant_web/tests/run_test_suite.py
      "$PYTHON" -m coverage report
      ;;
    compile)
      "$PYTHON" -m compileall -q assistant_web/backend assistant_web/scripts assistant_web/tests
      ;;
    shell)
      for script in assistant_web/deploy/aws/*.sh assistant_web/scripts/*.sh assistant_web/scripts/commands/*.command; do
        bash -n "$script"
      done
      ;;
    ruff)
      "$PYTHON" -m ruff check assistant_web/backend assistant_web/tests scripts
      ;;
    mypy)
      "$PYTHON" -m mypy
      ;;
    audit)
      "$PYTHON" assistant_web/scripts/check_dependency_audit.py
      ;;
    *)
      echo "알 수 없는 단계: $1 (가능: ${ALL_STEPS[*]})" >&2
      exit 2
      ;;
  esac
}

if [[ "$#" -eq 0 ]]; then
  set -- "${ALL_STEPS[@]}"
fi
for step in "$@"; do
  echo "==> $step"
  run_step "$step"
done
