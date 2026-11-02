# P1-B: evaluation patch 이전과 역참조 제거

## 범위와 기준선

2026-11-02 사용자의 다음 단계 실행 요청에 따라 P1-B를 수행했다. 기존 `codex/architecture-p0` 작업 공간에 있던 P1-A·고객사 검색·답변 설명·UI 수정은 그대로 유지했다. 시작 시 Python 소스·테스트·구조 예외·계획 398개 파일을 복사하고 해시와 작업 트리 상태를 `assistant_web/runtime/architecture-p1b/`에 보관했다. 자동 커밋이나 P1-C 작업은 수행하지 않았다.

제품의 프롬프트, 점수, 라우팅, 검색 조건, 타임아웃, 감사 저장 순서를 바꾸지 않았다. 실제 모델 재호출은 수행하지 않았으며, 같은 입력·모델 응답·고정 시각을 사용해 구조 변경 전후 동작을 비교했다.

## 수정 내용

- `evaluation/comparison.py`: 모델 선택/전송 4곳을 해당 모듈이 import한 함수를 직접 호출하도록 이전했다.
- `evaluation/finalization.py`: 후속 질문·검증·A/B 처리 6곳을 해당 모듈의 명시적 의존으로 바꿨다. 두 `_compat_dependency` 구현과 호출 10곳을 제거했다.
- `evaluation/verification.py`: `generate_basic_grounded_baseline`의 core 역호출 wrapper를 삭제했다. 유일한 내부 소비자인 finalization은 comparison의 실제 함수를 가져온다.
- `evaluation/core.py`는 이미 comparison의 실제 baseline 함수를 재내보내고 있어 수정하지 않았다. `reports/evaluation.py`의 한시적 공개 경로도 유지했다.
- `test_query_planning.py`의 문자열 모델 patch 두 경로와 `test_notion_partial_answer.py`의 core 객체 모델 patch 한 곳을 comparison으로 옮겼다. finalization/answer_grounding 테스트에서 동적 조회 함수 자체를 대체하던 7곳은 `patch.multiple`로 소비 모듈의 의존을 직접 대체한다.
- 답변 차단, 숨긴 출처를 이용한 검증·후속 질문, 실제 응답 모델 기록, 감사 로그 1회 저장, A/B 후보의 재검증을 확인한다. 신규 comparison 테스트는 정상 응답·HTTP 실패·timeout·연결 오류의 실제 요청/응답 처리를 검증한다.
- 공개 import 계약에 baseline 함수도 포함했다. `test_quality_insights.py`는 옛 공개 경로의 순수 소비자로서 유지하며 전체 회귀에서 검증한다.

## 전후 동작 비교

같은 스크립트를 변경 전후 각각 실행했다. 경계의 모델 선택/HTTP만 대체하고 실제 근거 구성, 검증, A/B 채점, SQLite 저장과 SSE 생성은 실행했다. 외부 네트워크와 `.env`는 기존 테스트 runner가 차단한다.

고정 사례 10개: 일반 답변, 출처 숨김, 원답 차단, 후보 모델 비교, 기본 검색 비교, 후보 답변 차단, 기본 비교 답변 차단, 모델 HTTP 실패, 검증된 원문 설명의 A/B 생략, 지정 후속 질문 유지.

**10/10 사례에서 최종 답변, 검증 결과, 출처, 후속 질문, 모델 선택·요청 payload·옵션, 감사 DB 행, SSE 이벤트 순서가 동일했다.** 시각·UUID·실행 타이머만 입력 단계에서 고정했으며 결과 필드를 제거하거나 정규화하지 않았다. 각 사례는 감사 행이 정확히 1개인지 검사한다. `before-behavior.json`과 `after-behavior.json`은 바이트 단위로도 동일하다.

추가 AST 대조에서 호환 함수 선택문 제거와 호출 이름 변경을 제외한 기존 함수 본문 10개가 동일함을 확인했다. 따라서 이번 측정으로 실제 모델 정확도나 응답 시간 개선을 주장하지 않는다. 이전 고정 문항의 고객사 검색 결과나 Gemini 연결 상태를 다시 측정한 결과도 아니다.

## 구조 변화

| 항목 | 이전 | 이후 |
|---|---:|---:|
| evaluation 동적 조회 구현 | 2 | 0 |
| evaluation 동적 조회 호출 | 10 | 0 |
| 전체 동적 호환 조회 구현 | 3 | 1 |
| 전체 동적 호환 조회 호출 | 11 | 1 |
| 명시적 런타임 Python 순환 그룹 | 8 | 7 |
| 함수 밖 import 순환 그룹 | 0 | 0 |

comparison/core/finalization/verification의 4개 모듈 순환 그룹이 해소됐다. comparison→verification 등 일부 import 자체는 남지만 순환에 속하지 않으므로 해당 순환 예외를 제거했다. 구조 fixture의 역사적 P0 기준선은 유지했고 해소된 예외 20개(순환 간선 7, 파사드 참조 6, 동적 참조 레코드 7)만 삭제했다. 새 예외나 금지 의존은 추가하지 않았다.

## 검증 기록

- 변경 전 전체 2,714개 검사 중 날짜 테스트 1개 실패: `test_period_open_list_has_one_status_and_registration_date`. 검색 달력만 10월 30일로 고정하고 검증 달력은 실제 날짜를 사용해, 11월 2일부터 서로 다른 주를 계산했다. 이 테스트만 두 소비 모듈에 같은 `FixedDate`를 사용하도록 수정했다. 날짜 해석 제품 코드는 변경하지 않았다.
- patch 경로 이전 후 구현 제거 전 실패를 확인했다. 파사드의 원래 함수가 실제 소비 모듈에 설정한 대체 함수를 덮어써 후보 생성·최종 검증 관련 검사가 실패했다. 실패 로그는 보존한다. 이 실행에는 수정 중인 날짜 mock 오류 1개와 아직 제거 전인 파사드 예외의 stale 실패도 포함되므로 모두 제품 회귀로 세지 않는다.
- 첫 변경 후 전체 검사에서 `test_notion_partial_answer.py`의 `patch.object(core, ...)` 1곳이 남아 기본/후보 A/B 두 분기가 실패했다. 해당 소비 지점도 comparison으로 이전했고 불완전 검색/본문 안내 검사는 그대로 유지했다. 제품 코드를 우회하거나 검증 기대값을 낮추지 않았다.
- 최종 `check.sh`의 tests/compile/shell/ruff/mypy는 통과했다. **2,716개 테스트, 커버리지 90%, mypy 177개 소스 파일 성공**이다. 기존 날짜 테스트와 Notion 부분 검색 안내의 기본/후보 A/B 두 분기도 통과했다.
- audit은 샌드박스 DNS 오류 후 외부 조회가 가능한 환경에서 `check.sh audit`만 재실행했다. **Python 보안 검사는 실패**했다. 이번 작업에서 변경하지 않은 `requirements.lock`과 `requirements-dev.lock`의 `pyasn1==0.6.4`에 CVE-2026-119196, CVE-2026-119194, CVE-2026-119197, CVE-2026-119195 네 건이 보고됐고, 도구가 제시한 수정 버전은 0.6.5다. 기존 예외 정책을 확대하지 않았다.
- Python audit의 조기 종료로 실행되지 않은 npm 검사는 check.sh에 있는 동일 명령(`npm --prefix assistant_web/frontend audit --omit=dev --audit-level=high`)을 별도로 실행해 **취약점 0건**을 확인했다.
- **P1-B 구현·동작·구조 검증은 완료했지만, 전체 단계의 최종 보안 관문은 미통과다.** 이 결과를 전체 check.sh 성공이나 배포 준비 완료로 표시하지 않는다. 의존성 보안 업데이트는 동작 보존 리팩터링과 분리해 처리한다.

원문 없는 합성 입력과 상세 실행 산출물은 `assistant_web/runtime/architecture-p1b/`에 보관한다. 주요 파일: `before-manifest.json`, `before-tests.log`, `patch-migration-red.log`, `before-behavior.json`, `after-behavior.json`, `function-equivalence.json`, `before-graph.json`, `after-graph.json`, `removed-exceptions.json`, `check-final.log`, `check-audit.log`, `check-npm-audit.log`.

## 검토와 다음 단계

AGENTS.md에 따라 서브에이전트 없이 구현자가 계획·전체 diff·구조 목록·실행 결과를 직접 검토했다. 구현 범위에서 추가 수정이 필요한 문제는 발견하지 않았으며, 외부 리뷰를 받은 변경이라고 주장하지 않는다. 현재 작업 공간의 선행 수정이 필요한 기준선이므로 새 worktree로 이동하거나 기존 변경을 되돌리지 않았다.

우선 별도 보안 작업에서 pyasn1을 0.6.5로 갱신하고 잠금 파일·테스트·audit을 다시 확인해야 한다. 구조 개선의 다음 단계는 P1-C다. `intent/context.py`의 감사 출처 patch를 실제 소비 지점으로 옮기고 마지막 `_compat_dependency` 구현 1개와 호출 1곳을 제거한다. 후속 질문의 사용자별 감사 출처 격리를 유지하고 옛 공개 import 경로는 P9까지 보존한다.
