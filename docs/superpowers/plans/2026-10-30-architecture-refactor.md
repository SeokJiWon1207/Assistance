# 기존 동작 보존 구조 개선 Implementation Plan

> **For agentic workers:** `superpowers:executing-plans`로 작업별 실행한다. AGENTS.md에 따라 메인 에이전트가 직접 수행하며, 사용자가 해당 작업에서 명시적으로 요청하기 전에는 서브에이전트·위임·자동 리뷰 에이전트를 사용하지 않는다. 체크박스로 작업의 완료 여부를 기록한다.

**Goal:** 답변·검색·화면·인증·저장 데이터를 보존하면서 순환 의존, 역방향 참조, 동적 monkeypatch 호환 장치와 분산된 변경 책임을 제거한다.

**Architecture:** 실제 동작 검사를 유지한 상태에서 테스트 patch를 소비 지점으로 이전하고, 공통 규칙과 데이터 변환을 독립 모듈로 추출한다. 단순 재내보내기는 필요한 동안만 유지하며, 운영 코드·테스트·스크립트 이전 뒤 폐기한다. 백엔드, 저장소, 프런트엔드는 각각 실행 가능한 작은 작업으로 진행한다.

**Tech Stack:** Python 3.12, FastAPI, unittest/coverage, SQLite, 기존 ES modules/Node test runner, 기존 Bklit React 번들 빌드.

**Spec:** `docs/superpowers/specs/2026-10-30-architecture-refactor-design.md`

**P0/P1-A 실행 기록:** P0와 P1-A 코드 이전·결정형 검증·실제 모델 비교를 완료했다. 사용자가 등록한 `OPENAI_API_KEY`로 GPT-6 Luna를 연결하고, 동일 연결을 적용한 변경 전 평가용 코드와 현재 코드에 고정 11문항(13턴)을 실행했다. 전 9/11, 후 10/11이며 새 실패는 없다. 모델 계획 변동과 실시간 API 응답 차이가 있어 점수 증가를 P1-A의 개선 효과로 단정하지 않는다. 상세 결과는 `docs/superpowers/plans/2026-10-30-architecture-p0-results.md`, `docs/superpowers/plans/2026-10-30-architecture-p1a-results.md`에 기록했다.

**최신 상태(2026-11-02):** P4까지 완료했다. Notion 내부 순환은 0개이며 P3의 query/intent/team 경계도 유지했다. 기존 import·캐시·요청 정책을 보존했고 고정 317문항이 바이트 단위로 동일했다. Notion 18사례의 출력·HTTP 호출도 조회 시각을 제외하면 동일했다. 2,751개 테스트·90% 커버리지와 check.sh 전체 단계가 통과했다. 실제 모델은 이번에 재호출하지 않았다. 상세: `docs/superpowers/plans/2026-11-02-architecture-p4-results.md`. 다음 단계 P5-A는 아직 착수하지 않았다.

## Global Constraints

- Python 명령은 `assistant_web/.venv/bin/python`으로 실행한다. 의존성은 기존 `requirements-dev.lock`을 따른다.
- 실행 전 설계 문서와 루트 `AGENTS.md`를 읽는다. 2026-11-02 후속 사용자 요청으로 P4 Notion 내부 순환 해소, 기존 import·동작 보존, 고정 비교와 전체 검증까지 승인 범위를 확장했다. P5 이후에는 착수하지 않는다.
- 사용자 동작·프롬프트·점수·라우팅·타임아웃·TTL·권한·화면 디자인은 유지한다. 발견한 버그 수정은 별도 작업으로 분리한다.
- 비밀값·서비스 계정·실제 이슈 원문·운영 DB·정확도 실행 산출물은 커밋하지 않는다.
- 운영 환경의 CORS, 인증 쿠키, 모델 endpoint 보안 설정을 완화하지 않는다.
- 라우팅·필터 코드를 이동할 때도 경계 사례가 빠져 있으면 `assistant_web/tests/fixtures/routing_cases.jsonl`에 추가한다.
- 커밋 전 `assistant_web/scripts/check.sh` 전체를 실행한다. 커버리지 하한 72%를 낮추지 않는다.
- 검증 명령 변경은 `check.sh`에 모으고 CI는 단계 호출만 유지한다.
- 기존 사용자 변경과 다른 작업의 파일을 수정·커밋하지 않는다. 같은 파일은 변경 구간을 확인한 뒤 필요한 부분만 다룬다.
- 계획상 새 파일명은 제안한 목적지다. 실행 시 기존 동등 책임의 모듈이 발견되면 중복 생성 대신 그곳으로 통합하고 본 문서의 매핑을 함께 갱신한다.

## Review Focus

1. 같은 프로세스에서 설정·명단이 바뀌는 경우: 기존 캐시 키·TTL·갱신 결과 유지. P2/P3 테스트 소유.
2. 취소·timeout·429·부분 원천 장애: 요청 횟수와 폴백·불완전 표시·SSE 순서 유지. P4/P5-C 테스트 소유.
3. 후속 질문의 조건 교체와 사용자 전환: 이전 사용자/질문의 범위가 새 결과로 섞이지 않음. P3/P7 테스트 소유.
4. 빈 DB·이전 버전 DB·동시 쓰기·오래된 클라이언트: 데이터 손실·삭제 복구·이중 migration 없음. P6 테스트 소유.
5. 실제 ES 모듈 초기화·서로 다른 자산 URL·CSS cascade: 중복 listener/상태 인스턴스와 화면 변화 없음. P7/P8 테스트 소유.

---

## 진행 순서와 독립 산출물

| 단계 | 수정 범위 | 단계 완료 조건 | 선행 |
|---|---|---|---|
| P0 | 기준선, 동작/호환/구조 검사 분리 | 보호할 계약·파사드 사용처·8개 Python SCC 목록 고정 | 없음 |
| P1 | Notion/evaluation/intent의 patch 이전 | 동적 조회 구현 4→0, 호출 26→0; 한시적 import 호환 유지 | P0 |
| P2 | common/config·Notion 설정/HTTP 분리 | 순수 helper의 네트워크 의존 제거, 설정·발견 동작 동일 | P1 |
| P3 | query/intent/team 규칙 | intent 내부 순환과 검색 모듈 역참조 제거 | P2 |
| P4 | Notion 검색·변환·저장본 경계 | Notion을 포함한 29개 SCC 해소 | P3 |
| P5 | 근거·리포트, Mantis, 채팅 단계 | 관련 작은 SCC·역방향 참조 제거, 큰 모듈 책임 분리 | P2–P4 |
| P6 | DB별 소유권·연결, workspace/team_brief | 데이터 보존, 잔여 Python SCC 0 | P5 |
| P7 | 프런트 테스트·순환·상태 소유권 | 내부 ES 모듈 SCC 0, 의미별 상태 전이로 이전 | P0; 실행은 P6 뒤 |
| P8 | CSS·자산 URL·ECharts·정적 검사 | 화면/캐시 정책 동일, 불필요 자산 제거 | P7 |
| P9 | 파사드 폐기·최종 회귀 | 제거 대상 참조 0, 전체 검사·실제 원천 비교 증거 확보 | P1–P8 |

각 하위 작업을 하나의 검증 가능한 변경으로 다룬다. 한 단계에 여러 하위 작업이 있으면 순서대로 수행한다. P1에서 공개 경로를 즉시 삭제하지 않으므로 이후 단계의 규모와 분리해서 되돌릴 수 있다.

## 공통 검증 명령과 판정

모든 명령은 저장소 루트에서 실행한다.

```bash
assistant_web/scripts/check.sh tests ruff mypy
```

백엔드 작업 도중의 기본 확인이다. `tests`는 기존 `run_test_suite.py`를 통해 임시 DB·격리 환경·네트워크 차단을 사용한다. 생략 인자를 새로 발명해 선택 실행하지 않는다. 아래 작업의 테스트 파일 목록은 해당 전체 실행에서 반드시 결과를 확인할 대상이다.

```bash
node --experimental-vm-modules --test assistant_web/tests/frontend_workspace.test.cjs assistant_web/tests/frontend_chat_controller.test.cjs assistant_web/tests/frontend_navigation.test.cjs
assistant_web/scripts/check.sh tests compile
```

프런트 작업은 해당 Node 파일을 먼저 실행하고 전체 tests/compile로 확인한다. 새 테스트 파일을 만들면 Node 명령에 실제 파일명을 추가한다. `test_frontend_core.py`가 Node 전체 테스트도 호출하므로 그 결과와 skip 여부를 확인한다.

```bash
assistant_web/scripts/check.sh
```

커밋 전·단계 완료 시 최종 관문이다. tests/compile/shell/ruff/mypy/audit 모두 성공해야 한다. 문서만 작성하는 이번 작업에서는 이 실행 결과를 주장하지 않는다. 실행 중 audit의 외부 서비스 장애 등은 코드 실패와 구분해 기록하되 성공으로 간주하지 않는다.

동작 비교는 고정 시각·fixture·가짜 모델/원천 응답으로 수행한다. 요청/응답에서 생성 UUID·소요시간처럼 비결정적인 필드만 사전 허용 목록으로 정규화한다. 조건·정렬·ID·문구·원천 범위·SSE 순서를 정규화하여 차이를 숨기지 않는다.

## P0. 기준선과 이전 계약

**파일:** 수정 `assistant_web/tests/test_architecture_contracts.py`; 생성 `assistant_web/tests/test_legacy_import_contracts.py`, `assistant_web/tests/test_architecture_dependency_graph.py`, `assistant_web/tests/architecture_graph.py`, `assistant_web/tests/fixtures/architecture_migration.json`; 동작 fixture는 기존 `architecture_contracts.json`, `routing_cases.jsonl`을 유지한다.

**인터페이스:** `collect_python_edges(root: Path) -> list[ImportEdge]`; `ImportEdge`는 importer/imported/line/scope를 가진 테스트용 값. `strongly_connected_components(edges) -> list[set[str]]`. 실제 생산 모듈을 import하지 않고 AST로 읽는다. 스냅샷은 숫자 상한뿐 아니라 모듈·간선·이전 소유 단계·예외 제거 조건을 기록한다.

- [x] 현재 사용자 변경을 포함한 인수 기준선, Python/Node 버전, 설정 플래그, 테스트 결과를 기록한다. HEAD 해시만으로 현재 코드를 식별하지 않는다. 사용자 diff를 되돌리거나 임의로 커밋하지 않는다.
- [x] 기존 전체 check.sh를 실행한다. 기존 실패가 있으면 원인/변경 관련성을 기록하고 동일 영역의 이동 전에 기준 동작을 확인한다.
- [x] AST 그래프 도우미에 함수 내부·중첩 조건·상대 import·패키지 모듈 import·TYPE_CHECKING 구분·단순 DAG/3노드 순환 fixture 검사를 추가한다. `sys.modules`/importlib 등 동적 연결은 별도 목록으로 표시한다.
- [x] 현재 명시적 SCC 8개(29/4/4/4/2/2/2/2), 함수 밖 SCC 0, 호환 조회 구현 4·호출 26을 다시 계산한다. 숫자가 달라지면 작업 트리 변경/집계 기준을 먼저 설명한다.
- [x] PUBLIC_SYMBOLS와 facade 소유 모듈 검사를 한시적 호환 테스트로 옮긴다. `test_query_plan_golden_cases`, Notion 답변 golden, DOM/API 동작 계약은 목표 구조 테스트에 남긴다.
- [x] production/tests/scripts/docs/배포 설정의 old import·patch·문자열 모듈 경로를 조사해 파사드별 사용처/담당 작업을 fixture에 기록한다. 패키지 밖 사용 여부는 별도 확인 항목으로 남긴다.
- [x] 새 순환·금지 간선·새 파사드 참조를 차단한다. 기존 예외는 정확한 간선 목록으로 제한하고, 제거한 예외의 재등장을 실패시킨다. 기존 SCC 안의 신규 간선도 검사한다.
- [x] 파일 이동으로 간선 이름이 바뀌는 작업은 예외를 자동 재생성하지 않는다. 동일 책임의 이전 매핑을 기록하고 해당 작업 끝에 순환/금지 간선이 실제로 줄었는지 검토한다. 신규 기능의 간선을 이전 예외에 끼워 넣지 않는다.
- [x] 공통 명령으로 검증한다. 목표: 현재 동작 검사는 유지되고, 예제의 금지 의존을 넣을 때 구조 검사만 예상대로 실패한다.

**완료/롤백:** 테스트 의미가 이전 전과 같고 기준선이 재현돼야 한다. 검사 파일만 되돌릴 수 있으며 생산 동작 변화가 없어야 한다. 제안 커밋: `test: 구조 개선 기준선과 한시적 호환 계약 분리`.

## P1. patch 이전과 동적 조회 제거

### P1-A. Notion의 15개 호출

**파일:** `backend/notion/{schema,mapping,search,write,compat,core}.py`, `backend/sources/notion.py`; 테스트 `test_search_pipeline.py`, `test_notion_{schema,search_unit,write,client}.py`, P0 사용처 목록. 여기와 이후 `backend/`, `tests/`는 모두 `assistant_web/` 아래다.

**인터페이스:** 함수 인수·반환값은 현재 그대로 둔다. 예: schema의 설정/스키마 조회, mapping의 페이지/관계/사용자 조회, search의 7개 조회 wrapper, write의 설정/보류 명령 조회가 각 소비 지점의 명시적 callable을 사용한다.

- [x] 옛 `backend.sources.notion.*`/`backend.notion.core.*` patch를 실제 호출 모듈로 옮긴다. 예: 스키마 경계는 `backend.notion.schema`가 조회하는 이름, 검색 경계는 `backend.notion.search`가 조회하는 이름을 patch한다.
- [x] 각 fake의 호출 인수·횟수와 결과를 확인하는 테스트를 추가/보완한다. 실제 HTTP 접근은 기존 runner가 차단한다. 이전 전 확인 테스트에서 write 소비 지점의 fake를 파사드가 덮어쓰는 실패를 재현했고, 동적 조회 제거 뒤 통과했다.
- [x] schema 3, mapping 3, search 7, write 2곳을 명시적 의존 호출로 바꾼다. 불필요해진 forwarding wrapper는 그 소비자와 함께 정리한다. write의 미사용 설정 wrapper는 함께 제거했다.
- [x] `compat.py` 사용처가 0임을 AST로 검증한 뒤 삭제한다. `sources/notion.py`와 core의 공개 import 계약은 유지한다.
- [x] 기존 기능 테스트와 구조 검사를 실행한다. AST 규칙으로 `sys.modules`에서 patch된 함수를 찾는 로직의 재도입을 막는다.

**실측 보완 게이트 완료:** Gemini HTTP 402 실행은 제외하고, 사용자 요청으로 OpenAI의 같은 모델·같은 설정으로 정상 기준선과 변경 후 측정을 수행했다. 새 실패 0개, 대상 Notion 원문 3개와 13턴의 출처 순서·단계 요약 동일을 확인했다. 기존 본문 인용 검색 실패는 남았으며, 전체 제품의 정확도를 이 표본 점수로 해석하지 않는다.

**완료:** Notion 동적 조회 0, 기존 검색/쓰기/권한 결과 동일. 제안 커밋: `refactor: 노션 테스트 대체 경로와 의존 조회 정리`.

### P1-B. evaluation의 10개 호출과 역참조

**파일:** `backend/evaluation/{comparison,finalization,verification,core}.py`, `backend/reports/evaluation.py`; 테스트 `test_evaluation_finalization.py`, `test_query_planning.py`, `test_quality_insights.py`, P0에서 확인한 추가 사용처.

- [x] comparison의 모델 선택/전송 4곳과 finalization의 후속 질문/검증/A-B 처리 6곳의 patch를 각각 소비 모듈로 옮긴다. 최종 답변의 검증 상태·차단·후속 질문·저장 1회 여부를 확인한다.
- [x] 두 `_compat_dependency` 정의와 10개 호출을 제거한다. 함수 호출 시그니처와 감사 처리 순서는 바꾸지 않는다.
- [x] verification 끝의 `generate_basic_grounded_baseline` 역방향 wrapper 사용자를 comparison의 실제 함수로 이전한다. core가 필요한 심볼은 comparison에서 직접 재내보낸다. verification→core 간선을 제거한다.
- [x] evaluation 4개 SCC가 사라지고 `reports/evaluation`의 한시적 공개 계약이 유지되는지 검증한다.

**완료:** evaluation 동적 조회·core 역참조 0. 2026-11-02 고정 10사례 전후 동일, 2,716개 테스트·compile/shell/ruff/mypy 통과. 당시 audit에서 발견된 pyasn1 0.6.4 취약점 4건은 후속 보안 업데이트(0.6.5)로 해소했고 P1-C 최종 check.sh 전체도 성공했다. 상세: `docs/superpowers/plans/2026-11-02-architecture-p1b-results.md`. 제안 커밋: `refactor: 평가 의존 조회와 호환 역참조 제거`.

### P1-C. intent의 1개 호출

**파일:** `backend/intent/context.py`, `backend/intent/core.py`, `backend/understanding.py`; 테스트 `test_search_pipeline.py`, `test_team_question_accuracy.py` 및 후속 질문 범위 테스트.

- [x] `backend.understanding.load_audit_sources` patch를 `backend.intent.context.load_audit_sources`로 이전한다.
- [x] 같은 이슈 참조라도 사용자 uid가 다르면 이전 감사 근거를 가져오지 않는 검사를 유지한다.
- [x] context의 `_compat_dependency` 정의와 호출을 제거한다. 옛 import 계약은 유지한다.
- [x] 전체 AST 집계가 구현 0·호출 0인지 확인한다. 코어 모듈 치환은 아직 남을 수 있으며 P9에서 별도 처리한다.

**완료:** 4개 구현·26곳 호출 제거. P1-C에서는 patch 4곳과 동적 조회 정의/호출 각 1개를 정리했고, 실제 감사 DB를 사용하는 고정 17사례 전후 결과와 사용자·A/B 격리를 확인했다. 2026-11-02 check.sh 전체 성공(2,718개 테스트, 커버리지 90%). 상세: `docs/superpowers/plans/2026-11-02-architecture-p1c-results.md`. 제안 커밋: `refactor: 후속 질문의 감사 조회 의존 명시`.

## P2. common/config와 기반 책임 분리

### P2-A. 공통 함수의 실제 소유자

**생성:** `backend/utils/{text,numbers,dates}.py`, `backend/domain/people.py`, `backend/sources/chroma_document.py`, 순수 helper에 필요한 정적 상수를 담는 `backend/settings/values.py`. **수정:** `common.py`, `customer_identity.py`, `mantis_status.py`, `effort_fields.py`, `display.py`, `config.py`와 해당 심볼 소비자. 새 패키지의 `__init__.py`는 빈 초기화 파일로 둔다. 고객사 책임은 후속 고객사 검색 수정에서 생긴 기존 `customer_identity.py`로 통합하며, 중복 `domain/customers.py`는 생성하지 않는다.

| 이전 함수군 | 목적지/유지 계약 |
|---|---|
| compact_text, 따옴표 처리, normalize_for_match, 검색 토큰·조회 표현 | utils/text.py; 현재 문자열·공백·대소문자 처리 유지 |
| clamp_int/clamp_float | utils/numbers.py; None/문자열/범위 동작 유지 |
| parse_date, format_mantis_custom_date | utils/dates.py; timezone·잘못된 날짜 처리 유지 |
| 고객사 별칭/프로젝트 소유자 | 기존 customer_identity.py; 동의어 충돌·모호성·표시명 유지 |
| 이름 검증·조사 제거·사람 매칭 | domain/people.py; 제품명/역할명 차단 유지 |
| Chroma 필드/제목/프로젝트/문서 정규화 | sources/chroma_document.py; 기존 포맷의 누락값 처리 유지 |
| display_mantis_status_label | 기존 mantis_status.py; 기존 매핑 보존 |

- [x] 이동 대상의 현재 서명과 반환 fixture를 먼저 고정하고 경계 사례를 추가한다. 이름 문자열·별칭·날짜의 현재 잘못된 동작을 이번 단계에서 고치지 않는다.
- [x] 함수 본문을 책임별로 이동하고, 소비자를 새 경로로 순차 이전한다. 재내보내기를 남기더라도 내부 생산 코드는 사용하지 않게 한다.
- [x] utils가 사용하는 STOPWORDS 등 정적 테이블은 settings/values로 함께 옮겨 config를 경유하지 않게 한다. JSON에서 읽는 고객사 목록은 domain 책임에 남기고 P2-B에서 로딩 경계를 이전한다.
- [x] `extract_issue_id_from_text`, `extract_from_names` 등 경계에 걸친 함수는 실제 포맷 소비자를 기준으로 위 목적지에 배정하고 P0 소유권 목록을 완성한다.
- [x] 기존 common·고객사·사람·상태·Chroma 관련 테스트와 전체 회귀를 실행한다. 새 utils import만으로 HTTP 클라이언트가 로딩되지 않는 격리 검사를 추가한다.

**완료:** 2026-11-02 common의 업무 심볼 44개 소유자와 이전 소비자 목록을 `common_ownership.json`에 기록했다. 구현·정적 객체 동일성, 캐시 초기화, 설정/HTTP 없이 순수 모듈 import, 생산 코드의 common 참조 0을 검사한다. 고정 문항 310개 전후 동일, 2,723개 테스트·90% 커버리지 및 check.sh 전체 성공. 상세: `docs/superpowers/plans/2026-11-02-architecture-p2a-results.md`. 제안 커밋: `refactor: 공통 함수의 도메인별 책임 분리`.

### P2-B. 설정 읽기와 Notion 통신

**생성:** `backend/settings/{catalogs,notion,paths}.py`, `backend/notion/{transport,discovery}.py`. **수정:** `config.py`, `notion/client.py`, config의 HTTP/설정 함수 소비자, `main.py`·CLI·테스트 runner의 초기화 연결. P2-A의 `settings/values.py`는 그대로 재사용한다. 순수 Notion 환경 해석과 경로를 분리해 settings가 config 호환 경로 또는 HTTP를 역참조하지 않게 한다. **테스트:** `test_notion_{discovery,client,rate_cooldown,security,schema_cache}.py`, 새 `test_configuration_boundaries.py`.

**인터페이스:** `get_configured_notion_settings() -> dict[str, Any]`는 환경의 명시 설정만 읽는다. `resolve_notion_settings() -> dict[str, Any]`는 기존 get_notion_settings와 같은 조건으로 발견을 수행하고 같은 키를 반환한다. `notion_post_json(url: str, payload: dict[str, Any], timeout: int = 45, retries: int = 2) -> requests.Response`와 request slot/retry delay의 현재 정책은 transport로 이동한다.

- [x] 현재 JSON 파일 없음/잘못된 JSON/별칭 충돌, 환경변수 우선순위, 명시 DB 있음/없음, 토큰 없음, 발견 실패·빈 목록·TTL 만료 사례를 고정한다.
- [x] 현재 get_notion_settings 사용자를 순수 설정 읽기와 실제 발견 필요로 분류한다. 발견이 필요하던 호출을 순수 getter로 바꿔 DB가 누락되지 않게 한다.
- [x] transport에 rate-limit 잠금·재시도·deadline 처리를 한 인스턴스로 옮긴다. 복제된 잠금/캐시가 생기지 않게 한다. client는 transport/discovery를 직접 사용한다.
- [x] 기존 get_notion_settings 이름은 이전 기간 resolve 함수의 동일 의미 재내보내기로 유지한다. 모든 소비자·patch 이전 뒤 config에서 네트워크 코드를 제거한다.
- [x] 고객사 JSON 로더를 catalogs로 옮긴다. 최초 추출에서는 현재 로딩/오류/기본값 의미를 그대로 유지한다. 명시 초기화로 바꿀 때는 앱 시작 전에 동일 검증이 끝나고 CLI·테스트 경로에도 동일 초기화가 적용되도록 별도 작은 변경으로 수행한다.
- [x] 명시 초기화 변경까지 P2의 완료 범위에 포함한다. `initialize_customer_catalogs() -> None`가 기존 세 JSON 로더를 같은 우선순위로 실행하고, 소비자는 catalogs의 접근 함수를 사용한다. main의 요청 수락 전과 관련 CLI/격리 테스트 준비에서 초기화한다. 잘못된 필수 설정은 서버가 정상 시작한 뒤 첫 요청에서 발견되지 않게 한다. import 시 네트워크·JSON 읽기 0회, 초기화 시 기존 기본값/오류/동일 캐시 객체의 수명, 앱/CLI 결과 동일성을 확인한다.
- [x] 값 모듈 import/순수 getter에서 HTTP 0회, 명시 DB가 있으면 발견 0회, 없으면 기존 캐시 정책에 따른 발견 횟수, 429 cooldown/취소/오류 동작을 검사한다.

**완료:** 2026-11-02 config의 HTTP 구현과 import 시 고객사 JSON 읽기를 제거했다. 잠금·캐시와 기존 경로의 함수/사전 동일성을 유지했다. 고정 문항 310개·설정/통신 29사례 전후 동일, 2,734개 테스트·90% 커버리지 및 check.sh 전체 성공. 상세: `docs/superpowers/plans/2026-11-02-architecture-p2b-results.md`. 제안 커밋은 `refactor: 노션 설정 해석과 통신 책임 분리`, JSON 초기화 변경은 별도 `refactor` 커밋으로 분리한다. 자동 커밋은 하지 않았다.

## P3. query/intent/team 순환 해소

**생성:** `backend/query/{operations,dates,notion_text,notion_constraints}.py`, `backend/intent/{json_payload,validation_rules,execution_contract,keyword_terms,notion_constraints,notion_grounding,team_scope}.py`, `backend/domain/team.py`, `backend/sources/people_catalog.py`. **수정:** `query_planning.py`, `query_filters.py`, intent의 14개 SCC 구성 모듈, `team_scope.py`, `notion/{search,query_text,formatting}.py`, 해당 규칙의 소비자·회귀 테스트. 기존 `text_scope.py`는 재사용한다.

**인터페이스:** `infer_operation(question: str) -> str`, `extract_group_by(question: str) -> str | None`, `has_count_intent(question: str) -> bool`와 기존 날짜 함수 서명을 유지한다. 순수 Notion 함수는 `parse_notion_constraints(text: str, *, known_people: tuple[tuple[str, ...], tuple[str, ...]]) -> dict[str, Any]`; 명단 조회가 필요한 기존 한 인수 진입점은 intent의 조합 함수로 유지한다. `load_known_people()`의 튜플 반환·파일 변경 감지·캐시 의미는 people_catalog가 보존한다.

- [x] 현재 query_planning의 결정형 연산/기간 함수와 관련 상수를 query로 이동한다. build/validate_query_plan은 조합 책임만 남긴다. 모든 하위 소비자가 query_planning의 조합 모듈로 역참조하지 않게 한다.
- [x] Notion 제목·구조 텍스트·상태/제외/기한 해석은 query의 순수 함수로 이동한다. 사람 명단은 호출자가 기존 people catalog에서 읽어 전달한다. intent와 query_planning에서 notion.search를 import하는 3개 주요 경로를 제거한다.
- [x] notion/query_text의 grounded constraints는 이미 계산된 공통 조건을 인수로 받게 하고, 일반 조건 해석을 다시 호출하던 경로를 조합 계층으로 옮긴다. 같은 질문을 중복 해석하거나 부분 조건을 누락하지 않는 호출 검사를 둔다.
- [x] search_terms가 constraints를 다시 계산하는 similar 판단 경로를 조합 함수로 분리한다. 하위 판정은 명시적 constraints를 받는다. 옛 선택 인수 동작은 사용처 이전까지 상위 진입점에서 보존한다.
- [x] status_semantics가 constraints에서 가져오던 extract_json_object를 json_payload로 이동한다. team_scope가 scope_changes를 다시 참조하지 않도록 팀 명단·순수 표현 판정을 domain/team에, 조건 조합을 intent에 둔다.
- [x] aggregation_spec/condition_grounding/unified_validation이 공유하는 require/unquoted/check_clause/check_query_period를 validation_rules로 이동한다. 해당 helper의 의존 closure도 함께 추출해 validation_rules가 unified_validation으로 되돌아가지 않게 한다.
- [x] `test_query_planning.py`, `test_unified_planner.py`, `test_followup_scope_transitions.py`, `test_all_list_scope_followups.py`, `test_team_question_accuracy.py`, `test_notion_routing.py`와 routing_cases를 검증한다. 현재 운영 플래그와 unified planner on/off 양쪽을 확인한다.
- [x] 고정 날짜의 연말·윤년·기간 역전, 제목 속 날짜/상태, 제외 조건, 미할당자, 명단 교체 후 캐시, 후속 질문의 조건 해제·사용자 범위를 확인한다. 이 영역의 SCC·금지 간선 예외를 제거한다.

**완료 확인(2026-11-02):** intent/query/team 포함 SCC 0, intent/query→notion.search 0. Notion 내부 12개 모듈 SCC는 P4 대상으로 남는다. 고정 315문항 동일, 36경계 사례 보존, 전체 2,745 tests / 90% coverage 및 모든 검사 단계 성공. 상세는 `2026-11-02-architecture-p3-results.md` 참조. 제안 커밋은 기간/연산, Notion 해석, intent 검증/문맥의 세 책임으로 분리한다.

## P4. Notion 내부 의존 방향과 큰 검색 모듈

**생성:** `backend/notion/{values,matching,server_filters,crawl_transport,crawl_validation,snapshot_state,cache_maintenance,read_api,page_enrichment,page_ownership}.py`. **수정:** `client.py`, `mapping.py`, `search.py`, `body_index_sync.py`, `body_index_search.py`, `complete_snapshot.py`, `snapshot_maintenance.py`, `property_snapshot.py`, `catalog_search.py`, `recent_catalog.py`, `read_scope.py`, `schema.py`, `schema_cache.py`, `body_index.py`, `main.py`의 warmer 연결과 관련 소비자·테스트.

**인터페이스:** 값 변환의 현재 서명, page matcher/score/sort의 현재 인수·반환을 그대로 이동한다. crawl의 `_Budget`, `_ReadFailure`, `_request`는 `CrawlBudget`, `NotionReadFailure`, `request_json`으로 내부 공개화하되 생성 인수·예외 reason·request 옵션을 보존한다. `coverage`, `_state`, `_time`, `catalog_max_age_seconds`는 snapshot_state가 소유한다.

- [x] client↔mapping의 plain rich text/user/title/block 변환을 values로 추출한다. 관계/사용자 원격 조회를 포함한 변환은 별도 명시적 조합으로 남겨 values가 HTTP를 부르지 않게 한다.
- [x] client의 warmer/refresh_catalog 호출을 cache_maintenance로 이동한다. 시작/중지·TTL·락·schema seed를 보존하며 main이 warmer를 시작한다. read_scope는 schema cache를 직접 사용해 client 역참조를 제거한다.
- [x] body_index_sync↔property_snapshot이 공유하는 요청/예산/예외와 페이지/ancestor 유효성 검사를 crawl 모듈로 옮긴다. snapshot_state는 refresh/search를 import하지 않는다.
- [x] complete_snapshot/recent_catalog/catalog_search가 search에서 가져오는 매칭·정렬·후보 변환을 matching으로 옮긴다. `_indexed_body_matches`가 I/O를 포함하면 조회와 순수 판정을 나누고 조회 소유자는 body_index_search로 둔다.
- [x] `notion_page_matches_and_enrich`처럼 조회를 동반하는 함수는 순수 matching에 넣지 않는다. enrich는 검색 조정 계층이 호출하고 matcher에 필요한 값을 전달한다. 예외/조회 횟수/매칭 순서는 기존과 같아야 한다.
- [x] search의 서버 필터 조합을 server_filters로 옮기고, 기존 검색 조정 진입점·출력 stage/meta 키는 유지한다. filter nesting 한도, top-k, exact-title, 조건 적용 순서와 점수는 바꾸지 않는다.
- [x] `test_notion_search_unit.py`, `test_notion_complete_snapshot.py`, `test_notion_body_index_sync.py`, `test_notion_body_index_candidates.py`, `test_notion_catalog_search.py`, `test_notion_recent_registration.py`, `test_notion_partial_answer.py`, `test_notion_security.py`를 포함한 전체 회귀를 실행한다.
- [x] 429/timeout/취소, 잘린 properties, 반복 cursor, 부분 본문, 이동/삭제/접근권한 변경 페이지, 최신/만료 저장본, warmup 중 요청을 검사한다. HTTP 호출 횟수·완전성 표시·필터 결과가 기준선과 같아야 한다.
- [x] 전체 그래프로 29개 SCC가 재형성되지 않는지 확인하고 해당 예외를 제거한다. 파일 개수·크기 감소만으로 완료하지 않는다.

**완료 확인(2026-11-02):** Notion 내부 SCC 0, 하위 조회·값·상태 모듈의 search/refresh 역참조 0. 기존 공개 경로를 보존하는 read_api와 I/O 분리를 위한 page_enrichment/page_ownership을 추가했다. 고정 317문항 동일, Notion 18사례 동일(조회 시각 제외), 2,751 tests / 90% coverage 및 전체 검사 성공. 상세: `2026-11-02-architecture-p4-results.md`. 변환, crawl/저장본, 검색의 세 하위 변경으로 커밋한다.

## P5. 근거·리포트·Mantis·채팅 책임 분리

### P5-A. 근거와 리포트

**생성:** `backend/evidence/{excerpts,stages,references,retrieval_rows,model_context}.py`, `backend/reports/{constraint_display,comparison_common,quote_rendering}.py`. **수정:** `evaluation/explanation_quotes.py`, `evaluation/explanation_direct.py`, `reports/{aggregate,work_queries,similar,comparison_facts,comparison_history,comparison_narrative,issue_documents,document_context,effort,effort_trend}.py`, `sources/mantis_note_supplement.py`, `retrieval.py`, `answers.py`, `effort_fields.py`.

**인터페이스:** `safe_excerpts(issue: dict[str, Any]) -> list[dict[str, Any]]`, `collect_retrieved_issues(retrieval_result: dict[str, Any], limit: int | None) -> list[dict[str, Any]]`, `display_constraints(constraints: dict[str, Any]) -> list[dict[str, Any]]` 유지. 단계 판정은 `_STAGES→STAGES`, `_stage_event→stage_events`, `_completed_stage→completed_stage`로 공개화하되 현재 반환 구조·반복 순서를 유지한다.

- [x] sources/reports가 사용하는 발췌·출처·단계 판정의 의존 closure를 evidence로 옮긴다. 단순 재내보내기 경유로 evaluation 의존이 남지 않게 한다.
- [x] explanation_quotes의 payload 검증은 evaluation에, 렌더링은 reports/quote_rendering에 둔다. direct↔quotes가 공유하는 label/reference/excerpt를 하위 evidence로 내려 서로 호출하는 고리를 끊는다. 긴 결론 처리 함수는 질문 유형별 동일 결과를 먼저 고정한 뒤 내부 책임을 나눈다.
- [x] aggregate가 retrieval에서 가져오는 rows 정리와 work_queries가 answers에서 가져오는 조건 표시를 새 소유자로 이전한다. 결과 정리 과정에서 원천 조회가 추가되지 않는지 확인한다.
- [x] similar와 comparison_*가 공유하는 필드·근거·표시 helper를 comparison_common으로 옮긴다. effort_trend의 EFFORT_FIELD_LABELS/parse_effort_field_value 참조는 기존 effort_fields로 이전해 effort↔trend 순환을 끊는다.
- [x] `test_explanation_quotes.py`, `test_direct_explanation.py`, `test_explanation_source_references.py`, `test_comparison_evidence_boundaries.py`, `test_comparison_narrative.py`, `test_effort_trend.py`, `test_filtered_effort_verification.py`를 검증한다. 다른 이슈 근거 혼합, 예정/미완료를 완료로 판단하는 차이, 원문 인용 변경은 모두 실패다.

**완료:** 실제 reports 구현/sources→evaluation 0, aggregate→retrieval 0, work_queries→answers 0, 관련 3개 SCC 해소. 한시적 `reports/evaluation.py` 재내보내기는 정확한 경로 예외로 P9까지 유지한다. 근거·비교·공수별 커밋으로 분리한다.

실행 결과: [P5-A 검증 기록](2026-11-02-architecture-p5a-results.md). 기존 경로를 유지하고 대상 순환 3개를 해소했다. 실제 모델 재호출 없이 고정 질문 317개와 근거·문맥 1,992사례가 동일했으며 전체 검증을 통과했다. 커밋은 수행하지 않았다.

### P5-B. Mantis와 검색 오케스트레이션

**생성:** `backend/sources/{mantis_fields,mantis_matching,mantis_client,mantis_snapshot_query}.py`, 필요 시 `backend/services/retrieval_pipeline.py`. **수정:** `sources/mantis.py`, `sources/mantis_store.py`, `sources/mantis_search_projection.py`, `tools/mantis_snapshot.py`, `retrieval.py`와 소비자.

- [x] normalize_issue/get_issue_custom_field_value 등 정규화·필드와 필터 판정을 하위 모듈로 이동한다. mantis_store/projection/tools가 mantis의 검색 조정 모듈을 다시 가져오지 않게 한다.
- [x] HTTP fetch 함수와 저장본 조회를 분리한다. existing tools/mantis_snapshot은 읽기 전용 도구 진입점으로 유지하고, sources가 tools 계층을 import하는 간선은 공통 저장본 reader로 이전한다.
- [x] retrieval의 근거 변환은 P5-A 소유자를 사용한다. 검색 실행을 services로 옮길 경우 `run_retrieval_pipeline(query_text, limit, tools, intent_plan=None)` 계약과 도구별 실행 순서를 보존한다.
- [x] `backend.retrieval.*` patch는 이 단계에서 실제 새 소비 지점으로 이전한다. 기존 36회라는 수치를 그대로 목표로 쓰지 않고 P0 조사 결과와 AST/문자열 사용처로 누락을 확인한다.
- [x] `test_search_pipeline.py`, `test_list_continuation.py`, `test_routing_accuracy.py`, `test_explanation_source_references.py`와 Mantis 저장본/필터 테스트를 실행한다. 오래된 저장본·실시간 재검증 실패·중복·빈 결과·정렬·모델 근거 수를 비교한다.

**완료:** Mantis 4개 SCC 해소, 원천 저장소가 검색/도구 조정을 역참조하지 않는다. 정규화/HTTP/검색은 독립 커밋으로 검증한다.

실행 결과: [P5-B 검증 기록](2026-11-02-architecture-p5b-results.md). Mantis 순환과 sources→tools 역참조를 해소하고 검색 실행·patch를 실제 services 소유자로 이전했다. 고정 문항 317개, 순수 변환/필터 14,234사례, HTTP·검색·재검증 37사례가 동일하고 전체 검증을 통과했다. 커밋은 수행하지 않았다.

### P5-C. 채팅 단계와 실행 컨텍스트

**생성:** `backend/services/chat/{prepare,planning,retrieve,generate}.py`. **수정:** `services/chat_service.py`, `services/unified_planning.py`, `services/contracts.py`, API 소비자. `ChatPipelineContext`, `StageResult`, `DeterministicReportResult`는 기존 contracts 정의를 재사용하고 복제하지 않는다.

- [x] unified_planning.execute_plan이 chat_service를 통해 가져오는 report/search 함수를 실제 소유자에서 import하도록 이전한다. 공유 실행 판단은 별도 단계 함수로 이동한다.
- [x] prepare_chat_pipeline/plan_chat_query/retrieve_chat_evidence/generate_model_response를 해당 책임 모듈로 이동한다. 인수·반환·예외 및 호출 순서는 보존한다.
- [x] ChatService는 admission/request_execution/최종화의 단일 요청 경계를 유지한다. 단계 이동 때문에 실행 컨텍스트를 중첩 생성하거나 로그를 두 번 남기지 않는다.
- [x] `test_chat_execution.py`, `test_stream_fallback.py`, `test_unified_planner.py`, `test_unified_model_fallback.py`, `test_evaluation_finalization.py`에서 취소 전/도중, 첫 토큰 timeout, 모델 실패, 결정형 조기 반환, 최종 저장 횟수와 SSE 순서를 검증한다.
- [x] 기존 구조 테스트의 함수 위치 검사를 명시적인 단계 의존 검사로 갱신한다. 같은 파일 안에 함수가 남아 있어야 한다는 조건을 더 이상 요구하지 않는다.

**완료:** chat_service↔unified_planning SCC 해소, 오케스트레이터는 단계 조합에 집중한다. 제안 커밋: `refactor: 채팅 실행 단계와 요청 경계 분리`.

실행 결과: [P5-C 검증 기록](2026-11-02-architecture-p5c-results.md). 채팅 단계와 공유 규칙을 분리하고 통합 플래너의 역참조를 제거했다. 고정 문항 317개, 실행 경계 20개, 실제 단계 실행 14개가 전후 동일하며 전체 검증을 통과했다. 기존 import 경로와 요청 경계를 유지했고 실제 모델 API 호출·커밋·재시작·배포는 수행하지 않았다.

## P6. DB별 소유권과 연결 정책

**생성:** `backend/storage/{sqlite,workspace_schema,workspace_repository,workspace_rows}.py`, `assistant_web/tests/test_storage_contracts.py`. **수정:** `db_migrations.py`, `auth.py`, `audit.py`, `workspace.py`, `workspace_notifications.py`, `conversation_history.py`, `reports/team_brief.py`, `playbooks.py`, 각 SQLite 사용 source/notion/tool 모듈. DB별 다른 schema 파일은 필요한 소유 모듈 옆에 둔다.

**인터페이스:** `open_sqlite(path: Path, *, profile: ConnectionProfile) -> sqlite3.Connection`; ConnectionProfile은 timeout/uri/row_factory/현재 transaction 옵션과 필요한 PRAGMA를 명시한다. helper는 연결을 돌려주며 close/commit/rollback의 호출자 계약을 문서화한다. 첫 이전은 기존 `closing(...)`/`with connection`/`BEGIN IMMEDIATE` 경계를 그대로 사용한다.

- [x] DB별 파일 경로, schema owner, migration 체계, 연결 옵션, read/write 소비자, 버전·잠금 정책을 표로 작성한다. 단순 connect 파일 수와 CREATE TABLE 수는 관찰 지표로만 둔다.
- [x] 빈 DB와 실제 schema를 축약한 구버전 fixture를 만들고, migration 실패 rollback·최신 버전 거절·재시작 재실행·동시 시작을 검사한다. 실제 운영 DB를 테스트에 직접 쓰지 않는다.
- [x] workspace schema/경로/brief 저장 함수를 storage 소유자로 이동한다. team_brief가 쓰던 `_issue_summary`, `_is_open`은 workspace_rows에 두고 `_now`는 공통 시간 책임으로 옮긴다. workspace↔team_brief를 끊는다.
- [x] auth/audit/workspace부터 연결 helper로 이전한다. 이어 Notion actions/body index, Mantis 저장본/인덱스, Chroma reader를 DB별 프로필로 이전한다. 임의 WAL 활성화·timeout 변경·강제 read-only 전환을 섞지 않는다.
- [x] schema 정의는 DB 소유자에 두고 현재 migration 리스트·SQL·user_version을 보존한다. 공용 runner 밖의 DB를 편입하려면 별도 호환 migration 작업으로 분리하고 이 단계에서 자동 재생성하지 않는다.
- [x] `test_db_migrations.py`, `test_auth.py`, `test_conversation_history.py`, `test_conversation_bulk_delete.py`, `test_workspace_notifications.py`, `test_team_brief.py`, `test_notion_actions.py`, `test_notion_body_index.py`를 검증한다.
- [x] 사용자별 데이터 격리, 삭제 후 stale 저장의 부활 차단, optimistic conflict, 전부 삭제/undo, 실패 rollback, 파일 descriptor 정리와 기존 파일 권한을 확인한다.
- [x] 전체 Python 그래프 SCC 0을 요구하고 완료된 간선 예외를 지운다. 연결 생성 허용 모듈을 구조 검사로 제한하되 외부 소유 DB의 schema에 관여하지 않는다.

**완료/롤백:** 기존 파일·schema·버전·행 내용이 같고 단일 소유자 경유로 읽고 쓴다. 코드 변경만 되돌릴 수 있어야 한다. DB 삭제나 migration 되감기는 롤백 절차가 아니다. DB군별 커밋으로 나눈다.

실행 결과: [P6 검증 기록](2026-11-02-architecture-p6-results.md), [DB 소유권·연결 정책](2026-11-02-sqlite-ownership.md). 기존 연결·트랜잭션과 import 경로를 유지하고 workspace 저장 책임을 분리했다. Python SCC 0, 고정 문항 317개와 DB migration 14개 전후 동일, 전체 검증을 통과했다. 커밋·재시작·배포는 수행하지 않았다.

## P7. 프런트엔드 테스트·모듈·상태

### P7-A. 실제 모듈을 실행하는 테스트 기반

**생성:** `tests/helpers/load_frontend_module.cjs`, `tests/frontend_module_graph.test.cjs`, `tests/frontend_bootstrap.test.cjs`. **수정:** 소스를 replace/slice/match로 변형하는 테스트, 특히 `frontend_issue_labels.test.cjs`, `frontend_sparklines.test.cjs`, `frontend_bklit_reports.test.cjs`; `test_frontend_core.py`, `test_architecture_contracts.py`.

- [ ] 이미 SourceTextModule을 사용하는 workspace/chat_controller/navigation 테스트는 유지하고 공통 loader만 필요한 만큼 공유한다.
- [ ] 내부 모듈의 실제 import/export를 연결하고 DOM·fetch·timer 같은 외부 경계만 stub한다. module cache는 전체 URL 기준으로 관리해 실제로 다른 URL을 하나로 합쳐 버그를 숨기지 않는다.
- [ ] 문자열을 잘라 private 함수를 실행하던 검사는 공개 동작을 호출하는 검사로 이전한다. UI 상태·근거·차트 입력 등 기존 assertion 의미를 유지한다.
- [ ] 기존 frontend의 TypeScript parser 의존성을 사용해 정적 import/export와 문자열 dynamic import 그래프를 읽는다. loader가 검사할 수 없는 동적 주소는 목록화한다. 기존 순환은 P0 방식으로 기록하고 P7-B 후 0을 강제한다.
- [ ] 실제 내부 모듈을 연결하는 bootstrap 검사를 추가한다. Node 단위 검사와 별도로 실제 브라우저에서 초기화·모듈 로드 오류·중복 listener를 확인한다.

**완료:** 변형한 소스만 통과하는 테스트가 없어지고, 모듈 연결 오류를 검출한다. 제안 커밋: `test: 프런트 모듈 연결과 동작 검증 정리`.

### P7-B. 순환 제거와 상태 변경 책임

**생성:** `static/modules/{render_utils,markdown_table,chat_editing,conversation_sync,conversation_view,workspace_view,app_state}.js`. **수정:** `runtime.js`, `bootstrap.js`, `events.js`, `chat.js`, `chat_rendering.js`, `notion.js`, `workspace.js`, `admin.js`와 실제 state 변경 소비자.

**인터페이스:** 공유 렌더 함수는 현재 인수/출력을 유지한다. 이벤트 연결은 `initEvents(actions)`로 명시한다. `getState()`는 현재 상태를 읽는 계약이며, 변경은 `selectConversation`, `applyConversationSync`, `rollbackConversationDeletion`, `startChatRequest`, `finishChatRequest`, `setWorkspaceResult`, `setAdminUsers`처럼 의미별 함수가 소유한다. 정확한 인수는 현재 상태 필드/uid/generation 조건을 그대로 옮겨 JSDoc으로 명시한다.

- [ ] chat_rendering↔notion의 HTML/문단/표/상태 표시 공통 코드를 render_utils/markdown_table로 이동한다. 상호 렌더 호출을 한 방향으로 만든다.
- [ ] cancelMessageEdit는 listener가 없는 chat_editing으로 옮긴다. workspace에서 chat rendering을 부르던 연결은 bootstrap이 넘기는 명시적 callback으로 바꾼다. 범용 event bus를 새로 도입하지 않는다.
- [ ] events의 import 시 실행을 initEvents로 옮기고 bootstrap에서 기존 순서에 맞춰 1회 호출한다. 모듈 재로딩/중복 초기화 시 listener 중복이 없는지 검사한다.
- [ ] state 생성 소유자를 app_state 하나로 옮긴다. 이전 기간 runtime은 같은 객체를 재내보내며 복제 상태를 만들지 않는다. 대화→채팅→workspace→관리자 순서로 직접 변경을 의미별 함수로 이전한다.
- [ ] 직접 대입 외 push/splice/Map·Set 변경/Object.assign/alias를 통한 mutation을 조사한다. 상태 전체 임의 patch API로 대체하지 않는다. AbortController·타이머·DOM 참조는 해당 실행 모듈이 정리한다.
- [ ] uid/generation/activeConversationId 검증을 유지하며, 늦은 fetch·삭제 실패·로그아웃·스트리밍 중 대화 이동에서 다른 화면을 덮지 않는 테스트를 추가한다.
- [ ] workspace에서 저장/동기화와 목록·업무화면 렌더를 분리한다. 함수 위치를 요구하는 기존 구조 검사는 새 책임 지도와 동작 검사로 이전한다.
- [ ] Node 전체·compile·브라우저에서 로그인, 대화 생성/전환/편집/삭제/undo, earlier messages, 개인 업무/대시보드 상세, 관리자 권한, 취소·모델 오류·포커스 복원을 확인한다.

**완료:** ES 모듈 SCC 0, 외부 모듈의 임의 상태 변경 0, 초기화 순서·화면 동일. 순환 제거와 상태 도메인 이전을 각각 작은 커밋으로 검증한다.

## P8. CSS·정적 자산·정적 검사

**파일:** `static/styles.css`, `static/index.html`, 관련 기존 CSS, 새 `static/styles/` 연속 구간 파일, `static/modules/*.js`, `static/app.js`, `static/vendor/echarts.esm.min.js`와 전용 LICENSE, `tests/test_frontend_core.py`, 관련 frontend 구조 검사, `pyproject.toml`.

- [ ] 기준 화면을 현재 인수한 디자인으로 저장한다. 데스크톱/모바일, 밝은/어두운 테마, 표/차트/모달/빈 상태/오류 상태를 비교 대상으로 삼는다.
- [ ] styles.css의 연속 구간을 기존 cascade 순서 그대로 추출하고 index의 stylesheet 순서를 검증한다. 여러 위치의 같은 selector를 합치거나 specificity를 바꾸는 정리는 분리한다.
- [ ] 앱 코드·템플릿·동적 URL·배포 자산 목록에 ECharts 참조가 없음을 확인하고 파일 및 해당 전용 license만 제거한다. Bklit/다른 라이브러리 license는 유지한다.
- [ ] 앱 내부 정적 URL의 수동 `?v=`를 일관되게 제거한다. 같은 모듈이 bare/version URL로 중복 로딩되지 않게 전체 참조를 확인한다. 실제 module URL 다중 인스턴스 검사를 유지한다.
- [ ] `test_stylesheets_are_split_by_responsibility_in_cascade_order`의 `?v=` 필수 검사를 자산 존재·순서 검사로 바꾼다. JS no-store/기타 no-cache, app-version 새로고침 안내의 동작 검사는 유지한다.
- [ ] import 이동으로 생긴 lint 정책을 검토한다. Ruff I/TID 등은 실제 규칙 ID와 baseline 위반을 확인한 뒤 별도 작은 변경으로 적용한다. import 순서를 바꾸는 autofix는 초기화 회귀 검증과 함께 수행한다. 순환·레이어 정책은 구조 검사에서 강제한다.
- [ ] tests/compile·전체 check.sh와 기준 화면 비교를 수행한다. 접근성·키보드·포커스·스크롤·차트 표시가 같아야 한다. CSS 분리 때문에 HTTP 캐시 정책을 바꾸지 않는다.

**완료:** 화면 변화 없이 자산 관리가 단순해진다. ECharts 제거, URL 정리, CSS 분리, lint 규칙은 목적별 별도 커밋이다. 정적 자산은 묶음으로 이전·롤백하며 오래 열린 탭에는 기존 새로고침 안내를 사용한다.

## P9. 파사드 폐기와 최종 검증

**파일:** `backend/understanding.py`, `backend/sources/notion.py`, `backend/reports/evaluation.py`, `backend/{intent,notion,evaluation}/core.py`; 남은 `common.py/config.py/query_planning.py` 재내보내기; `tests/test_legacy_import_contracts.py`, 구조 fixture, 문서·CLI·배포 스크립트.

- [ ] P0 목록을 다시 전수 확인한다. 생산 코드·일반 테스트·정확도 스크립트·배포 진입점의 옛 import와 patch가 0인지 AST/문자열/실행 검사를 결합한다.
- [ ] 폐기 후보별 외부 소비 여부와 지원 범위를 기록한다. 사용처가 불명확한 경로는 제거 완료로 처리하지 않는다. 유지할 공개 API는 실제 얇은 API로 명시하고 동적 치환을 제거한다.
- [ ] `sys.modules[__name__] = ...` 치환과 `globals().setdefault` 주입을 제거한다. 완전히 이전된 파사드·core를 삭제한다. 이 변경과 동시에 해당 legacy import 검사를 없애고 금지 참조 검사로 바꾼다.
- [ ] common/config/query_planning에 실제 책임이 남는 경우 그 책임만 유지한다. 파일 삭제 자체를 목표로 삼지 않으며, 새로 만든 모듈에 wildcard export나 동적 별칭을 남기지 않는다.
- [ ] Python/ES 내부 SCC 0, 네 동적 조회 구현·26개 호출 0, 폐기 파사드 참조 0, 레이어 금지 간선 0을 확인한다. 허용 목록의 사용 완료 항목을 삭제하고 stale 예외도 실패시키도록 한다.
- [ ] 전체 check.sh를 실행하고 새 프로세스의 주요 진입점 import/앱 초기화와 실제 브라우저 핵심 흐름을 확인한다. 단위 테스트의 import 순서만으로 초기화 안전성을 판단하지 않는다.
- [ ] 고정 원천/문항/모델 설정으로 실행 전후 정확도 보고서를 비교한다. 실제 모델 문장의 일치보다 조건·ID·수치·근거·검증 차단을 확인하고, 이전에 통과한 문항의 퇴행은 사유 없이 허용하지 않는다.
- [ ] 원천·모델 호출 횟수, cache hit, DB 쿼리, 응답 지연을 기준선과 비교한다. 원천 변화·모델 변동은 재현 가능한 구조 회귀와 구분해 보고한다.
- [ ] 완료 보고서에 단계별 변경, 테스트 명령/결과, 최종 그래프, 남은 공개 API, 미검증 외부 환경, 롤백 커밋 경계를 기록한다.

**완료:** 설계의 동작 계약과 구조 목표가 모두 충족되어야 한다. 큰 SCC의 크기 감소나 전체 테스트 일부 통과만으로 전체 개선을 완료 처리하지 않는다.

## 실제 원천 검증 실행 예시

아래 입력 파일과 모델은 P0에서 확보한 동일 자료를 사용한다. 예시 파일을 실데이터 검증의 대체물로 쓰지 않는다. 운영 원천을 쓰는 검증은 기본 결정형 tests와 분리한다. 필요한 회사 모델 전송은 기존 승인 범위이며 메시지 전송 도구/notify 스크립트는 사용하지 않는다.

```bash
assistant_web/.venv/bin/python assistant_web/scripts/accuracy/benchmark.py validate assistant_web/runtime/accuracy_benchmark/cases.jsonl
assistant_web/.venv/bin/python assistant_web/scripts/accuracy/benchmark.py run assistant_web/runtime/accuracy_benchmark/cases.jsonl --model-id '<P0에서 고정한 모델 ID>' --out assistant_web/runtime/accuracy_reports/architecture-after
assistant_web/.venv/bin/python assistant_web/scripts/accuracy/benchmark.py score assistant_web/runtime/accuracy_benchmark/cases.jsonl assistant_web/runtime/accuracy_reports/architecture-after
```

P0에서도 다른 출력 폴더에 같은 명령으로 baseline을 만든다. 파일이 없으면 먼저 검증용 고정 문항과 근거를 준비한다. live benchmark가 source를 다시 읽는 경로에서는 전후 원천 revision이 같은지 별도 확인한다. 동적 문항 생성 스크립트는 보조 점검이며, 고정 문항의 전후 비교를 대신하지 않는다.

## 단계 중단과 되돌리기

- 결정형 입력에서 조건·ID·집계·인용·권한·SSE 순서가 달라지면 해당 작업을 중단하고 원인을 찾는다. 기대값을 바꿔 통과시키지 않는다.
- 구조 검사와 한시적 호환 검사가 충돌하면 소비자 이전 누락인지부터 확인한다. 검사 전체 삭제나 새로운 sys.modules 우회로 해결하지 않는다.
- 데이터 작업은 schema 동일성·migration 계약을 확인하기 전 배포하지 않는다. DB 자체 복원/삭제는 이 계획의 자동 롤백에 포함하지 않는다.
- 이전 작업으로 되돌릴 때는 해당 작업의 코드·소비자·테스트를 함께 되돌린다. 파사드만 되살리거나 새/옛 자산을 섞지 않는다.
- 사용자 변경과 다른 작업이 같은 파일에 있으면 해당 변경은 유지한다. 전체 파일 checkout/reset을 하지 않는다.

## 계획 자체의 검토 결과

- [x] 기존 6개 진단 영역과 추가 3개 호환 구현·11개 호출을 모두 포함했다.
- [x] 큰 29개 SCC 외 7개 SCC를 담당 작업에 배정했다.
- [x] 동적 조회 제거, 소비자 이전, import 호환 종료를 별도 조건으로 정의했다.
- [x] 기존 구조 테스트의 파사드 보존·함수 위치·자산 버전 조건을 이전하는 작업을 포함했다.
- [x] 동작 보존, DB migration/rollback, 프런트 초기화·상태 경합, 실제 모델 비교의 한계를 명시했다.
- [x] 새 외부 프레임워크, DB 재생성, 무조건 import/connect 수 0 같은 불필요한 목표를 넣지 않았다.
- [x] 사용자 변경 보호와 메인 에이전트 직접 실행 규칙을 반영했다.
