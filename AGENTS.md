# 프로젝트 에이전트 지침

이 파일은 이 저장소에서 작업하는 모든 코딩 에이전트의 단일 기준이다.
`CLAUDE.md`는 이 파일을 불러오고 Claude Code 전용 내용만 덧붙인다.

## 개발 환경

- 기준 런타임은 Python 3.12이며 루트의 `.python-version`을 따른다.
- 가상환경은 `assistant_web/.venv`에 만들고 `requirements-dev.lock`으로 설치한다.
- Python 명령은 `assistant_web/.venv/bin/python`으로 실행한다. 시스템 `python3`는 버전이 다를 수 있으므로 사용하지 않는다.
- 로컬 서버는 `assistant_web/scripts/commands/AI비서_서버_재시작.command`로 실행한다.
- 비밀값과 서비스 계정 파일은 커밋하지 않는다. 환경변수 예시는 `assistant_web/.env.example`을 따른다.

## 주요 구조

- `assistant_web/backend/api/`: FastAPI 라우터
- `assistant_web/backend/services/`: 요청 오케스트레이션
- `assistant_web/backend/intent/`: 질의 의도와 조건 해석
- `assistant_web/backend/sources/`: Mantis·Chroma·Notion 원천 조회와 저장본
- `assistant_web/backend/notion/`: Notion API·스키마·검색·쓰기
- `assistant_web/backend/reports/`: 결정형 집계·리포트 빌더
- `assistant_web/backend/evaluation/`: 답변 검증·비교·회귀 평가
- `assistant_web/backend/tools/`: 채팅 오케스트레이터가 쓰는 읽기 전용 데이터 도구
- `assistant_web/backend/static/`: 브라우저 UI와 기능별 JS/CSS 모듈
- `assistant_web/tests/`: Python·Node 회귀 테스트
- `assistant_web/scripts/accuracy/`: 실제 저장본·모델을 쓰는 정확도 점검
- `scripts/make_vector_db.py`: Mantis 스냅샷 구축

## 검증

커밋 전에 저장소 루트에서 다음 명령을 실행한다. CI도 같은 스크립트를 단계별로 호출한다.

```bash
assistant_web/scripts/check.sh
```

- 단계는 `tests compile shell ruff mypy audit`이며, 필요한 단계만 인자로 줄 수 있다.
  예: `assistant_web/scripts/check.sh tests ruff`
- `tests`는 외부 네트워크와 `.env`를 차단한 결정형 테스트와 커버리지 기준을 확인한다.
- 검증 명령을 바꿀 때는 `check.sh`만 고치고 CI는 단계 호출만 유지한다.

## 작업 규칙

- 사용자 변경과 관련 없는 파일은 수정하거나 커밋하지 않는다.
- 라우팅·필터 변경에는 `assistant_web/tests/fixtures/routing_cases.jsonl` 회귀 사례를 추가한다.
- 운영 환경의 CORS, 인증 쿠키, 모델 endpoint 보안 설정을 완화하지 않는다.

## 테스트·검증 데이터 전송

프로젝트의 테스트·검증에 필요한 이슈 원문(제목, 본문, 노트, 담당자 등)을 설정된 회사 모델로 전송하는 것은 사용자가 사전 승인했다. 같은 범위의 검증에서는 전송 승인을 반복해서 묻지 않는다.

## Git 커밋 메시지 규칙

앞으로 생성하는 모든 Git 커밋 메시지는 Conventional Commit 접두사를 제외하고 한국어로 작성한다.

- 커밋 제목은 `<type>: <한글 설명>` 형식을 사용한다.
- `type`은 영문 소문자로 작성하며 `feat`, `fix`, `refactor`, `build`, `test`, `docs`, `chore`, `perf`, `ci`, `revert` 중에서 변경 목적에 맞게 선택한다.
- 커밋 제목과 본문의 설명은 모두 한국어로 작성한다.
- 제목은 변경 결과가 드러나는 명령형 문장으로 작성한다.
- 제목은 가능하면 50자 이내로 작성한다.
- 서로 다른 목적의 변경은 작업별로 분리해서 커밋한다.
- `refactor` 커밋에는 점수·타임아웃·라우팅처럼 사용자 동작이 달라지는 변경을 섞지 않고, 각각 별도의 `fix` 커밋으로 분리한다.
- 코드 식별자, 파일명, 명령어, 환경변수처럼 번역하면 안 되는 항목만 영문을 허용한다.
- 사용자가 수정한 관련 없는 파일은 커밋에 포함하지 않는다.
- 커밋 전에 변경 범위에 맞는 테스트와 정적 검사를 실행한다.

예시:

- `feat: 대화 검색 필터 추가`
- `fix: 스트리밍 요청의 속도 제한 응답 수정`
- `refactor: 모델 설정 로딩 책임 분리`
- `build: 의존성 잠금 파일과 품질 검사 추가`
- `test: 만료된 보안 예외 검사 추가`
- `docs: 원자적 배포와 롤백 절차 문서화`
