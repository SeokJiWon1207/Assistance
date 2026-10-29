# 검증된 집계 명세 구현 계획

> **For agentic workers:** Use superpowers:executing-plans. 사용자의 명시 요청 없이 서브에이전트를 사용하지 않는다.

**Goal:** 통합 플래너가 집계 JSON을 만들고 서버가 검증·계산하며 검증된 사실만 모델 설명에 사용한다.

**Architecture:** 기존 단일 group_by와 빌더를 보존한다. aggregation 계약은 정규화된 전체 저장본의 Python 집계기로 실행하고, 기본 shadow 모드에서 기존 응답과 비교한다. 날짜 기준·산술·상한은 서버가 소유한다.

**Tech Stack:** Python 3.12, 기존 unittest/ruff/mypy, 기존 모델 호출 API.

**Spec:** 이 대화에서 승인된 2026-10-29 설계 피드백과 구현 요청.

## Global Constraints

- SQL 생성·실행, 자격증명 변경, 운영 배포는 이 작업에 포함하지 않는다.
- issue_count는 등록일, effort는 고객사전달일 기준. 필터/팀/후속 문맥은 기존 검증 계약을 따른다.
- 전체 저장본의 완전성 확인 후 계산하며 조회 상한으로 모집단을 자르지 않는다.
- 공수 단위는 기존 빌더와 동일하다. 모델은 수치나 날짜 기준을 계산하지 않는다.
- 단순 조회에는 설명 호출을 추가하지 않는다. 설명 실패 시 결정형 표를 반환한다.

## Review Focus

- 0→양수, 0→0, 양수→0, 누락 월과 실제 0을 구분한다.
- 중복 이슈, 집계 전 표시 제한, 기간마다 달라지는 상위 그룹으로 합계가 왜곡되지 않는다.
- 공수 담당자와 이슈 담당자, 지표별 기준 날짜를 혼동하지 않는다.
- 후속 필터 수정·독립 질문에서 집계 명세의 상속/해제가 정확하다.
- 모델의 숫자·부호·기간·고객사 바꿔치기와 미지원 식/SQL을 거부한다.

### Task 1: 전월 0건 표시 수정

Files: backend/reports/aggregate.py, tests/test_month_trend_display.py (assistant_web 기준).
Interface: format_month_trend_lines(group_counts, label) -> list[str].
- [x] 위 네 가지 경계의 실패 테스트 작성 및 실패 확인.
- [x] 신규 발생 표시와 누락 월 비교 방지를 구현한다.
- [x] `assistant_web/scripts/check.sh tests ruff mypy` 통과 후 별도 `fix` 커밋.

### Task 2: 통합 플래너 명세

Files: backend/intent/aggregation_spec.py (신규), unified.py, unified_validation.py, tests/test_aggregation_spec.py, fixtures/routing_cases.jsonl.
Interface: validate_aggregation(question, specification, previous) -> canonical dict; 필터는 기존 validate_plan이 소유한다.
- [x] 복합 그룹, 지표, 기간, 비교, 상한, 설명 요청의 허용 목록/근거와 상속 테스트를 먼저 작성한다.
- [x] 기존 group_by는 보존하고 optional aggregation을 추가한다. 집계 기간을 지표별 고정 날짜 기준으로 적용한다.
- [x] 유효/미지원/후속 명세와 기존 플래너 회귀를 검증한다.

### Task 3: Python 집계기

Files: backend/reports/aggregation.py (신규), tests/test_aggregation.py.
Interface: aggregate_rows(rows, spec, constraints) -> totals/groups/comparisons; execute_aggregation(plan) -> result+source metadata.
- [x] 같은 원본의 다중 그룹·공수·필터·중복·월별 0·상한 보존을 손계산 정답으로 검증한다.
- [x] trend_rows의 정규화 행과 기존 필터/공수 파서를 사용한다. 전체 계산 후 고정 상위 N과 기타를 표시한다.
- [x] 전체 저장본 누락/불완전이면 정확한 결과를 반환하지 않는다.

### Task 4: shadow 비교와 실행 연결

Files: backend/reports/aggregation_rollout.py (신규), services/unified_planning.py, tests/test_aggregation_rollout.py, .env.example.
Interface: run_aggregation_report(question, plan, model, api_key) -> report.
- [x] off/shadow/primary, 일치/불일치/비교불가/실패 테스트를 먼저 작성한다.
- [x] 같은 저장본 요청 범위에서 기존 답변을 보존하고 비교 상태를 감사 메타데이터에 기록한다.
- [x] 복합 집계는 shadow에서 지원하지 않는 기존 숫자로 대체하지 않는다. primary에서만 새 결과를 응답한다.

### Task 5: 기여도와 사실 ID 설명

Files: backend/reports/aggregation_narrative.py (신규), tests/test_aggregation_narrative.py.
Interface: build_facts(result) -> facts; summarize_aggregation(question, result, model, api_key) -> text+audit.
- [x] 서버 산술·기여도·부호·기간·그룹 연결, 알 수 없는 사실 ID, 모델 실패의 회귀 테스트를 먼저 작성한다.
- [x] 모델에는 제한된 계산 사실만 전달한다. 설명은 사실 ID와 구조화된 주장으로 검증하고 실패하면 표만 반환한다.
- [x] 단순 건수에는 호출하지 않으며 설명에 원문 이슈/개인정보를 불필요하게 전송하지 않는다.
- [x] 전체 check.sh, 동일 저장본 비교와 실행 성능 확인 후 결과를 문서화하고 기능 커밋한다.

## Execution notes

- Ruling: 승인된 설계·구현 요청에 따라 추가 계획 승인 대기 없이 메인 에이전트가 현재 체크아웃의 codex/validated-aggregation-spec 브랜치에서 수행한다.
- 시작 상태: main, 사용자 소유의 미추적 output/만 존재. 그 디렉터리는 변경·커밋하지 않는다.
- Task 1 red: 2581 tests, 기대한 2 failures (0→양수, 누락 월). 기존 2577 tests 통과.

- Task 1 complete: 5a768bc, 2,581 tests + ruff/mypy 통과.
- Task 2–5 complete: 집계 전용 32개 테스트, 별도 복사본 전체 2,613 tests, 커버리지 89%, compile/shell/ruff/mypy/audit 통과.
- Ruling: 날짜 기준과 복수 지표의 기간을 분리하기 위해 집계 기간은 aggregation.period에 두고, 추가 제한은 기존 필터 계약에 둔다. 복수 지표는 같은 기간을 각 지표 고정 날짜에 적용한다.
- Ruling: 기존 빌더가 같은 형태를 표현할 수 없는 복합 집계는 shadow에서 비교 불가로 기록하고 primary에서만 응답한다. 기존 숫자의 조용한 축소 응답을 방지한다.
- Ruling: 실제 Gemini에서 월 축 누락을 재현해 후속 그룹 삭제 근거를 검증했다. 생략한 설명 문맥은 사실 ID로 보완하고, 명시한 잘못된 기간/숫자는 거부한다. 의미 검토 제한은 실제 3초 시간 초과를 근거로 6초로 조정했다.
- 실제 데이터: 31,324건 저장본에서 shadow 4/4 일치, Gemini 계획 3/3 및 최종 설명 채택. 첫 실패와 최종 통과 모두 별도 로컬 보고서에 보존.
- 자동 승인 검토가 무료 Gemini로의 집계 전송을 차단했으나 사용자가 2026-10-29 현재 대화에서 집계 데이터 전송을 명시 허용한 후 재개했다. 본문/노트는 전송하지 않았다.
- 최종 리뷰: 주 작업자가 변경 범위·계약·누락/0·날짜·기여도·후속 상속·상한·실패 대체 경로를 직접 검토했다. AGENTS.md에 따라 별도 에이전트는 사용하지 않았다.
