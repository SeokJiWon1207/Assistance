# Mantis·Notion 홈 업무 추천 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:executing-plans`로 작업별 실행한다. 프로젝트 AGENTS.md에 따라 사용자의 명시적 요청 없이 서브에이전트를 생성하거나 리뷰를 위임하지 않는다. 체크박스로 진행을 기록한다.

**Goal:** 기존 Mantis와 Notion API를 모두 활용해 출처가 있는 개인 업무 추천을 홈에 최대 3개 표시하고, 선택한 작업을 기존 채팅으로 이어 간다.

**Architecture:** 원천별 후보 수집과 현재 근거 검증을 추천 서비스로 분리하고, 제한된 백그라운드 작업에서 캐시를 만든다. 규칙으로 후보·출처·작업 범위를 확정하며 선택적 회사 모델은 허용된 후보와 작업 유형만 선택한다. 홈 API는 사용자 캐시를 조회하고 클릭 시 다시 검증한 질문만 입력창에 전달한다.

**Tech Stack:** Python 3.12, 기존 FastAPI/Pydantic/SQLite, 기존 Mantis·Notion 전송 계층, ES modules와 기존 CSS, unittest 및 Node 테스트. 신규 패키지는 기본적으로 필요하지 않다.

**Spec:** [Mantis·Notion 홈 업무 추천 설계](../specs/2026-11-09-home-suggestions-design.md). 실행 전 함께 읽는다. 이후 사용자의 구현 요청에 따라 작업을 실행했다. 실행 결과와 조정 사항은 `.superpowers/sdd/2026-11-09-home-suggestions/progress.md`에 기록한다.

## Global Constraints

- 첫 릴리스부터 Mantis 단독, Notion 단독, Mantis+Notion 연결 추천을 지원한다.
- 최대 3개 표시. Mantis 후보 12개, Notion 후보 12개, 상세 이슈 4개, Notion 본문 4개 이내.
- 생성 예산 60초, 선택적 모델 호출 1회·12초·1,200토큰, 마스킹 입력 최대 12,000자.
- 캐시 최대 10분이며 각 원천 유효기간보다 길지 않다. 생성 워커 프로세스당 1개, 대기 사용자 8명, 사용자 lease 90초.
- `HOME_SUGGESTIONS_ENABLED=false`, `HOME_SUGGESTIONS_MODEL_ID=` 및 전용 모델 ID/endpoint origin 허용 목록을 사용한다.
- `GET`은 캐시 조회만 한다. 추천 선택은 자동 전송·원천 쓰기를 수행하지 않는다.
- 기존 회사 승인 모델만 사용한다. 검증용 Luna/Gemini 승인을 운영 추천으로 확대하지 않는다.
- Python 명령은 `assistant_web/.venv/bin/python`으로 실행한다.
- 기존 CORS·인증·쿠키·endpoint 보안을 완화하지 않는다. 비밀값을 문서·로그·커밋에 넣지 않는다.
- 사용자 변경과 관련 없는 파일은 수정하거나 커밋하지 않는다. 기존 위험 점수·상태 의미는 바꾸지 않는다.
- 커밋 설명은 한국어 Conventional Commit 형식을 사용한다. 사용자 변경이 많은 작업 트리이므로 커밋이 승인된 실행에서만 자기 변경을 분리해 스테이징한다.

## Review Focus

1. 동일 표시 이름·다른 담당자·Notion 매핑 없음: 관련성이 없는 문서를 개인 추천으로 노출하지 않는다. Task 2.
2. 후보 발견 후 삭제·담당 해제·Notion 접근 철회: 캐시와 prepare에서 최신 확인 실패를 반영한다. Task 2, 5.
3. 동시 갱신·lease 만료·계정 삭제 중 작업 완료: 이전 작업이 새 결과나 삭제 상태를 덮어쓰지 않는다. Task 1, 4.
4. 개발 완료·검증 예정·본문 일부만 확인: 단계 차이와 정보 부재를 오류·미검증 확정으로 확대하지 않는다. Task 3.
5. 추천 응답 대기 중 입력·대화 전환·원천 도구 OFF: 초안과 대화를 보존하고 자동 전송·설정 변경을 하지 않는다. Task 6.

## 파일 책임

신규 Python 파일은 `backend/suggestions/`에 모아 한 기능의 정책과 구현이 흩어지지 않게 한다.

| 파일 | 책임 |
|---|---|
| `suggestions/contracts.py`, `settings.py` | DTO, enum, 제한값과 기능 설정 |
| `suggestions/repository.py` | 추천 캐시·숨김·lease의 SQLite 저장과 원자 갱신 |
| `suggestions/sources.py` | 사용자 범위와 Mantis/Notion 후보·현재 근거 어댑터 |
| `suggestions/ranking.py`, `selection.py` | 관계 판정, 중복 제거, 규칙 순위, 제한된 모델 선택 |
| `suggestions/service.py`, `jobs.py` | 생성/prepare 오케스트레이션, 실행 예산·큐·종료 |
| `api/suggestions.py` | 인증 API와 상태 코드 |
| `static/modules/home_suggestions.js`, `static/home_suggestions.css` | 홈 표시·미리보기·클릭·폴링 |

기존 변경: `storage/workspace_schema.py`, `services/user_deletion.py`, `main.py`, `static/modules/{chat_empty_state,bootstrap,runtime,events}.js`, `static/index.html`, `.env.example`, `README.md`. DTO는 새 contracts 모듈에 두고 `ChatRequest`는 유지한다. 기존 모듈은 필요한 연결부만 수정한다. 채팅 URL 조회 지원이 회귀 시험에서 부족한 경우에 한해 관련 Notion 의도·조회 모듈을 최소 변경하고 Task 7에 기록한다.

## Task 1: 계약과 사용자별 저장 수명

**Files**
- Create: `assistant_web/backend/suggestions/{__init__,contracts,settings,repository}.py`
- Modify: `assistant_web/backend/storage/workspace_schema.py`, `assistant_web/backend/services/user_deletion.py`
- Test: `assistant_web/tests/test_suggestion_storage.py`, `assistant_web/tests/test_admin_user_lifecycle.py`, `assistant_web/tests/fixtures/storage_migrations_golden.json`

**Interfaces**
- `SourceRef`: 설계의 `sources[]` 필드. `type`, `grade`는 enum, 날짜는 UTC ISO 문자열.
- `Suggestion`: 설계의 항목 필드 및 내부 `evidenceRefs`. `Candidate`: `id`, `kind`, `sources`, `facts`, `allowedActions`, `dedupeKey`, `revision`, `priority`, `relevance`.
- `EvidenceBatch`: `candidates: list[Candidate]`, `sourceStatus: dict`, `scopeKey: str`.
- `SuggestionBundle`: `items: list[Suggestion]`, `state`, `generationId`, `sourceStatus`, `retryAfterSeconds`.
- `SuggestionRepository(path: Path)`의 `read(user_uid, scope_key, now)`, `dismiss(user_uid, suggestion_id, now)`, `claim(user_uid, scope_key, now) -> Lease | None`, `publish(lease, bundle, now) -> bool`, `release(lease, now)`.
- `Lease`: `token`, `user_uid`, `scope_key`, `expires_at`. 모든 저장 함수에 사용자/범위를 명시한다.

- [ ] 실패 테스트를 작성한다: `test_users_and_connections_are_isolated`, `test_dismiss_survives_identical_source_refresh`, `test_new_revision_can_reappear`, `test_old_lease_cannot_publish`, `test_user_deletion_removes_suggestions`. 다른 사용자 항목 조회·숨김 실패, 의미 없는 수집 시각 변경에 숨김 유지, 사용자 삭제 시 세 테이블 정리를 확인한다.
- [ ] `assistant_web/scripts/check.sh tests`로 새 계약/마이그레이션 실패를 확인한다. 기존 실패가 있으면 별도 기록한다.
- [ ] Pydantic DTO와 설정을 추가한다. 마이그레이션은 기존 목록 끝에 추가하고 캐시/숨김/작업 테이블의 PK에 사용자 범위를 포함한다. 생성 lease는 트랜잭션에서 비교·획득·게시한다. `publish`는 계정 생존·권한과 scope 재확인을 호출자가 통과한 경우에만 허용한다.
- [ ] 테스트를 통과시키고 저장 마이그레이션 fixture 및 계정 삭제 경로를 갱신한다.
- [ ] 커밋을 수행하는 실행에서는 `feat: 사용자별 업무 추천 저장 구조 추가`로 자기 변경만 분리한다.

## Task 2: 기존 두 원천에서 후보와 현재 근거 수집

**Files**
- Create: `assistant_web/backend/suggestions/sources.py`
- Test: `assistant_web/tests/test_suggestion_sources.py`
- Reference: `backend/workspace_data.py`, `workspace_notifications.py`, `notion/{search,semantic_search,page_links,read_scope,property_roles}.py`

**Interfaces**
- `collect_candidates(user: dict, *, now: datetime, budget: ExecutionBudget) -> EvidenceBatch`.
- `current_scope_key(user: dict) -> str`: 사용자와 두 원천 연결/읽기 범위/담당자 매핑/정책 버전의 내부 해시.
- `revalidate_sources(user: dict, sources: list[SourceRef], *, budget: ExecutionBudget) -> list[SourceRef]`: 삭제/범위 상실은 `SourceRevoked`, 일시 장애는 `SourceUnavailable`.
- 후보 수집은 Task 1의 `Candidate`, `EvidenceBatch`를 반환한다. 실패 원천은 `sourceStatus`에 별도 기록한다.

- [ ] fixture 테스트를 작성한다: `test_both_sources_are_collected`, `test_notion_works_without_mantis_identity`, `test_missing_notion_identity_keeps_verified_related_links`, `test_ambiguous_owner_does_not_expand_scope`, `test_no_index_uses_existing_api`, `test_partial_results_do_not_become_total_counts`, `test_revoked_page_is_not_returned`, `test_partial_notion_body_is_not_a_completion_fact`.
- [ ] `assistant_web/scripts/check.sh tests`로 새 테스트 실패를 확인한다.
- [ ] Mantis는 `load_complete_workspace`와 기존 사용자/이슈 요약 규칙을 사용한다. 유효 저장본이 없으면 추천 워커가 전체 수집을 시작하지 않고 원천 unavailable을 반환한다. 새 활동은 기존 동일 세대 관찰 규칙을 사용한다.
- [ ] Notion 개인 문서는 `execute_notion_search`의 명시적 담당자 조건으로 읽는다. 관련 문서는 실제 링크 우선, 이후 구체적 주제 후보 순으로 찾는다. `resolve_notion_read_scope`, `page_record`, `read_current_page`로 현재 소속·본문을 검증한다. 인덱스가 없는 직접 링크는 API에서 읽은 page/database 식별자로 현재 본문 검증 경로에 연결한다.
- [ ] 강한 연결 ID와 단순 유사 후보를 `facts`에서 구분한다. 최신 원본 시각과 개별 노트/페이지 수정 버전을 보존한다. 전송된 본문 안의 명령은 실행하지 않는다.
- [ ] 소스별 예외를 격리하고 `run_with_deadline`의 Mantis 10초·Notion 35초 하위 예산을 적용한다. timeout 후에도 이미 검증한 결과를 보존할 수 있도록 후보 누적기를 원천 단계의 바깥에 둔다. `assistant_web/scripts/check.sh tests ruff mypy`로 검증한다.
- [ ] 커밋 단계: `feat: Mantis와 Notion 추천 근거 수집 추가`.

## Task 3: 연결 추천·순위·문구와 선택적 회사 모델

**Files**
- Create: `assistant_web/backend/suggestions/ranking.py`, `assistant_web/backend/suggestions/selection.py`
- Test: `assistant_web/tests/test_suggestion_ranking.py`, `assistant_web/tests/test_suggestion_model.py`
- Reference: `backend/evidence/stages.py`, `backend/llm.py`, `backend/tools/redaction.py`

**Interfaces**
- `build_suggestions(batch: EvidenceBatch, *, now: datetime) -> list[Suggestion]`.
- `select_candidates(candidates: list[Candidate], *, budget: ExecutionBudget) -> list[tuple[str, str]]`: candidate ID와 allowed action만 반환, 실패 시 규칙 순서.
- `render_prompt(item: Suggestion) -> str`: 확인된 ID/URL과 작업 목적, 최신 근거 재조회 요청을 포함한다.

- [ ] 실패 테스트: `test_explicit_link_can_create_cross_source_task`, `test_similar_title_is_not_identity`, `test_development_done_and_customer_pending_are_distinct`, `test_missing_evidence_is_not_incomplete_work`, `test_same_work_is_one_recommendation`, `test_model_cannot_add_ids_links_or_actions`, `test_company_destination_allowlist`, `test_model_failure_keeps_both_source_rules`.
- [ ] `assistant_web/scripts/check.sh tests`로 새 테스트 실패를 확인한다.
- [ ] `stage_events`의 완료/예정/실패 규칙을 소유 구절 단위로 적용한다. 본문 부분 수집·시점 불명은 비교 제안까지만 허용한다. 설계의 작업 4종과 고정 문구를 작성하고 순위·중복 정책을 적용한다. 기존 `riskScore` 값을 변경하지 않는다.
- [ ] 회사 모델이 명시적으로 허용된 경우에만 `model_transfer_scope` 안에서 `post_model_chat`을 1회 호출한다. 기존 마스킹과 실제 전송 origin 검사를 사용한다. 설정 부재나 실패는 별도 외부 모델 폴백 없이 규칙 결과로 끝낸다.
- [ ] 모델 반환은 strict 구조로 검증하고 후보 ID·action·이유 코드를 허용 집합에 대조한다. 자유서술은 버린다. 제한 시간/호출 횟수/입력 길이와 `limit=3`을 시험한다.
- [ ] `assistant_web/scripts/check.sh tests ruff mypy` 통과 후 커밋 단계: `feat: 두 원천 근거로 다음 업무 추천 생성`.

## Task 4: 갱신 워커와 캐시 게시

**Files**
- Create: `assistant_web/backend/suggestions/service.py`, `assistant_web/backend/suggestions/jobs.py`
- Modify: `assistant_web/backend/main.py`, `assistant_web/backend/services/user_deletion.py`의 잠금/작업 정리 연결부
- Test: `assistant_web/tests/test_suggestion_jobs.py`

**Interfaces**
- `SuggestionService(repository)`의 `read(user, limit, now) -> SuggestionBundle`, `refresh(user, now) -> SuggestionBundle`, `prepare(user, id, now) -> dict`, `dismiss(user, id, now)`.
- `SuggestionJobs.start()`, `enqueue(user_uid: str) -> str | None`, `stop()`; 큐에는 사용자 UID만 보관하고 실행 직전 최신 사용자를 다시 읽는다.
- worker는 Task 2 → Task 3 → Task 1 `publish` 순서로 실행한다. 사용자 데이터 잠금은 기존 계정 삭제 계약과 일치하게 적용한다.

- [ ] 실패 테스트: `test_get_never_calls_network`, `test_duplicate_refresh_shares_generation`, `test_queue_has_eight_user_limit`, `test_expired_lease_cannot_overwrite`, `test_deleted_or_remapped_user_cannot_publish`, `test_shutdown_waits_for_actual_workers`, `test_notion_timeout_preserves_mantis`, `test_mantis_unavailable_preserves_notion`.
- [ ] `assistant_web/scripts/check.sh tests` 실행으로 새 실패를 확인한다.
- [ ] 전용 워커 1개, 대기 사용자 8명, DB lease 90초를 구현한다. 생성에 `ExecutionBudget(monotonic()+60)`을 주고 종료 시 취소 후 실제 하위 작업 종료를 기다린다. 포화 시 refresh를 시작한 척하지 말고 재시도 가능한 상태를 반환한다.
- [ ] 원천별 유효 결과를 합치고 TTL을 원천별 최소 유효 시각으로 자른다. 양쪽을 모두 읽지 못한 경우 기존 유효 항목만 유지하거나 unavailable로 끝낸다. 결과 게시 직전에 계정·scope·lease 토큰을 재검증한다.
- [ ] prepare에는 15초 하위 예산을 적용한다. 현재 소속/원본 변경을 확인하고 변경된 추천은 폐기 후 재생성을 요청한다. 원본 변경으로 의미가 달라진 오래된 prompt를 반환하지 않는다.
- [ ] `assistant_web/scripts/check.sh tests ruff mypy` 통과 후 커밋 단계: `feat: 업무 추천 갱신과 캐시 수명 관리`.

## Task 5: 인증 API와 제품 설정

**Files**
- Create: `assistant_web/backend/api/suggestions.py`
- Modify: `assistant_web/backend/main.py`, `assistant_web/.env.example`, `assistant_web/README.md`
- Test: `assistant_web/tests/test_suggestions_api.py`

**Interfaces**
- 설계의 GET 목록, POST refresh/dismiss/prepare 네 경로를 구현한다. 사용자 UID를 요청 본문으로 받지 않고 `current_request_user`에서 얻는다.
- `GET /api/suggestions`는 기능 OFF일 때 disabled 상태의 빈 목록을 반환하고 외부 호출 없음. refresh/prepare는 OFF일 때 409. 미인증은 401, 타인 항목은 404, 철회 항목은 410, 재검증 장애는 503.
- refresh 정상 접수 202, 큐 포화 429와 Retry-After, pending 중복은 같은 generationId. 인증/CSRF는 기존 미들웨어 계약을 따른다.

- [ ] 실패 테스트: `test_anonymous_denied`, `test_cannot_prepare_other_users_item`, `test_limit_is_bounded`, `test_refresh_is_idempotent_while_pending`, `test_disabled_has_no_source_calls`, `test_prepare_revalidates_access_and_revision`, `test_dismiss_is_user_scoped_and_idempotent`.
- [ ] `assistant_web/scripts/check.sh tests`로 새 실패를 확인한다.
- [ ] service를 연결하고 설정을 공개한다. 모델·원천 토큰, 내부 fingerprint, 원문 전체를 응답이나 일반 로그에 내보내지 않는다.
- [ ] README에 기능 활성화, 기존 두 원천 연결 재사용, 모델 없이 동작, 부분 실패, 캐시/숨김, 롤백 방법을 작성한다. 기존 에이전트 설정을 변경하지 않는다.
- [ ] `assistant_web/scripts/check.sh tests ruff mypy` 통과 후 커밋 단계: `feat: 인증된 사용자 업무 추천 API 추가`.

## Task 6: 홈 추천 UI와 입력창 연결

**Files**
- Create: `assistant_web/backend/static/modules/home_suggestions.js`, `assistant_web/backend/static/home_suggestions.css`
- Modify: `assistant_web/backend/static/modules/{chat_empty_state,bootstrap,runtime,events}.js`, `assistant_web/backend/static/index.html`
- Test: `assistant_web/tests/frontend_home_suggestions.test.cjs`. 기존 `test_frontend_core.py:test_frontend_core_node_suite`가 `frontend*.test.cjs`를 자동 검색한다.

**Interfaces**
- `initHomeSuggestions()`, `homeSuggestionsHtml()`, `stopHomeSuggestions()`를 export한다.
- 서버 DTO의 `sources[]`와 날짜만 렌더링하고 내부 facts를 다시 해석하지 않는다. request token에 UID/활성 대화/초안 버전을 저장해 늦은 응답을 거부한다.

- [ ] Node 실패 테스트: 최대 3개, 두 원천 링크, XSS/URL 검증, 링크 클릭 시 prepare 미호출, prepare 후 입력만 채움, 타이핑 중 prepare 응답은 초안 보존, 대화 전환 후 응답 무시, source OFF 안내, 숨김 실패 복구, 12회 폴링 종료.
- [ ] `node --experimental-vm-modules --test assistant_web/tests/frontend_home_suggestions.test.cjs`로 새 테스트 실패를 확인한다.
- [ ] 빈 대화에 추천 영역을 삽입한다. 캐시 조회 후 필요한 경우 refresh 1회 및 5초/12회 폴링을 시작한다. 추천 영역만 갱신하고 전체 메시지 재렌더링을 피한다. 계정/대화 전환에서 AbortController와 타이머를 정리한다.
- [ ] 미리보기·원천 링크·숨김을 각각 접근 가능한 버튼/링크로 구현한다. 입력창 반영은 기존 resize/focus를 재사용하고 pending template/edit 상태를 혼동하지 않게 한다. 준비 응답은 자동 submit하지 않는다.
- [ ] 기존 CSS 토큰을 사용하고 작은 화면/키보드에서 내용·출처를 확인한다. `node --experimental-vm-modules --test assistant_web/tests/frontend_home_suggestions.test.cjs` 및 `assistant_web/scripts/check.sh tests compile`을 통과시킨다.
- [ ] 커밋 단계: `feat: 홈에서 두 원천 업무 추천 표시`.

## Task 7: 생성부터 실제 질문까지 회귀 검증과 도입

**Files**
- Create: `assistant_web/tests/test_suggestion_flow.py`, `assistant_web/tests/fixtures/home_suggestions.json`
- Modify: `assistant_web/tests/fixtures/routing_cases.jsonl`, `assistant_web/README.md`
- 필요할 때만 Modify: Notion URL 기반 조회를 소유한 기존 의도/검색 모듈과 해당 회귀 테스트. 이미 통과하면 변경하지 않는다.

- [ ] fixture 6종을 만든다: Mantis 지연, Notion 담당 문서 기한, 명시 링크가 있는 두 원천 단계 비교, 비슷한 제목이지만 다른 고객사, Notion 429, 계정 범위 변경. 각 사례에 후보 포함/제외와 근거·질문을 명시한다.
- [ ] 생성된 prompt를 기존 질문 처리의 입력으로 전달해 원천·ID·기간·작업 목적 보존을 확인한다. Notion URL 단독과 복합 prompt가 정확한 페이지를 읽는지 검증한다. 부족할 때만 라우팅을 최소 수정하고 회귀 사례를 추가한다.
- [ ] prepare와 실제 전송 사이 원문이 바뀐 사례에서 실행 답변이 최신 원문을 다시 읽는지 확인한다. 추천 제목은 답변의 확정 근거로 사용하지 않는다.
- [ ] 신규 Node 테스트가 기존 `test_frontend_core.py`의 자동 검색을 통해 CI의 `check.sh tests` 경로에서 실제 실행되는지 확인한다. 별도의 테스트 실행기를 추가하지 않는다. 검증 명령 자체를 바꿔야 하면 `check.sh`만 변경하고 CI는 단계 호출을 유지한다.
- [ ] 루트에서 `assistant_web/scripts/check.sh`를 실행한다. 계획 작성 단계에서는 아직 실행하지 않는다. 기존 실패와 신규 실패를 분리해 기록하며 실패를 숨기기 위해 기준을 낮추지 않는다.
- [ ] 승인된 구현 검증 단계에서 기존 로컬 서버 실행 명령으로 실제 UI를 점검한다. 두 원천 성공/한쪽 실패/새 대화 전환/초안 보존/숨김·재표시를 확인한다. 원천 수정·외부 발송 없이 읽기와 입력 미리보기까지만 검증한다.
- [ ] 기능 OFF → 소수 계정 검증 → 기본 사용 순으로 도입한다. 초기에는 규칙만으로 두 원천 추천을 검증하고, 승인된 회사 모델을 설정한 뒤 결과를 비교한다. 홈 응답·작업시간·원천별 후보/채택 수·중복·실패를 계측하되 원문·토큰은 로그에 남기지 않는다.
- [ ] 최종 자체 리뷰에서 양쪽 원천 지원, 근거 검증, 계정 삭제, 실패 격리, UI 동작을 대조한다. 기능 OFF를 즉시 롤백 수단으로 문서화한다.
- [ ] 커밋 단계: `test: 두 원천 업무 추천 흐름 회귀 검증`.

## 실행 전 주의

현재 작업 트리에 관련 모듈의 사용자 변경이 많다. 구현 시작 시 다시 읽고 변경 충돌을 확인하며 일괄 `git add`·원복·청소를 하지 않는다. 이 계획의 커밋 단계는 기존 변경을 포함할 권한이 아니다. 새 구현 방식·파일 책임을 조정해야 하면 근거와 변경 범위를 계획에 먼저 반영한다.

## 계획 자체 점검

- 두 원천을 첫 릴리스에 포함: Task 2, 3, 7.
- 후보/본문·관련성/권한 구분: Task 2, 3, 5.
- 캐시/숨김/계정 삭제/동시 작업: Task 1, 4.
- 인증·모델 목적지·마스킹: Task 3, 5.
- 자동 전송 없음·초안 보존·키보드: Task 6.
- 생성한 질문의 실제 실행 가능성: Task 7.
- 제품 구현과 테스트를 수행했다. 커밋·배포·운영 활성화는 수행하지 않았으며 기본 OFF를 유지한다.
