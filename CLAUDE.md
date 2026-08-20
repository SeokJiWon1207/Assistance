# Assistance 프로젝트 안내

## 개발 환경

- 기준 런타임은 Python 3.12이며 루트의 `.python-version`을 따른다.
- 가상환경은 `assistant_web/.venv`에 만들고 `requirements-dev.lock`으로 설치한다.
- 로컬 서버는 `assistant_web/scripts/commands/AI비서_서버_재시작.command`로 실행한다.
- 비밀값과 서비스 계정 파일은 커밋하지 않는다. 환경변수 예시는 `assistant_web/.env.example`을 따른다.

## 주요 구조

- `assistant_web/backend/api/`: FastAPI 라우터
- `assistant_web/backend/services/`: 요청 오케스트레이션
- `assistant_web/backend/reports/`: 결정형 집계·리포트 빌더
- `assistant_web/backend/intent/`: 질의 의도와 조건 해석
- `assistant_web/backend/static/`: 브라우저 UI와 기능별 JS/CSS 모듈
- `assistant_web/tests/`: Python·Node 회귀 테스트
- `scripts/make_vector_db.py`: Mantis 스냅샷 구축

## 검증 명령

```bash
assistant_web/.venv/bin/coverage run assistant_web/tests/run_test_suite.py
assistant_web/.venv/bin/coverage report
assistant_web/.venv/bin/python -m compileall -q assistant_web/backend assistant_web/scripts assistant_web/tests
assistant_web/.venv/bin/ruff check assistant_web/backend assistant_web/tests scripts
assistant_web/.venv/bin/mypy
assistant_web/.venv/bin/python assistant_web/scripts/check_dependency_audit.py
```

배포 스크립트를 수정하면 `bash -n assistant_web/deploy/aws/*.sh`와
`bash -n assistant_web/scripts/commands/*.command`도 실행한다.

## 작업 규칙

- 저장소의 최상위 지침은 `AGENTS.md`이며 충돌 시 이를 우선한다.
- 커밋은 Conventional Commit 접두사와 한국어 설명을 사용한다.
- 사용자 변경과 관련 없는 파일은 수정하거나 커밋하지 않는다.
- 라우팅·필터 변경에는 `tests/fixtures/routing_cases.jsonl` 회귀 사례를 추가한다.
- 운영 환경의 CORS, 인증 쿠키, 모델 endpoint 보안 설정을 완화하지 않는다.
