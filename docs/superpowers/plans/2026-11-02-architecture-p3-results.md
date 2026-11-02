# P3 query/intent/team 순환 해소 결과

## 범위와 기준선

P3의 질의 규칙·의도 해석·팀 범위 순환을 분리했다. 변경 전 작업 공간의 코드·테스트·fixture·계획 445개 파일과 해시를 `assistant_web/runtime/architecture-p3/`에 보관했다. 변경 전 전체 테스트는 2,734개 통과, 커버리지 90%였다. 선행 P0~P2 및 고객사·제목/본문·모델·UI 변경을 기준선에 포함해 보존했다.

실행·검증 스킬을 적용하고 AGENTS.md에 따라 메인 에이전트가 직접 구현·검토했다. P4의 Notion 내부 순환은 이번 범위에 포함하지 않았다. 커밋, 서버 재시작, 배포는 수행하지 않았다.

## 책임 분리

| 소유자 | 책임 |
|---|---|
| `backend/query/operations.py`, `dates.py` | 결정형 연산·그룹·건수 판단과 날짜 계산 |
| `backend/query/notion_text.py`, `notion_constraints.py` | 제목/본문 구분·본문 근거 병합·담당자/상태/제외/기한 해석 |
| `backend/intent/notion_constraints.py`, `notion_grounding.py` | 명단 조회 및 공통 조건 추출이 필요한 기존 진입점의 조합 |
| `backend/sources/people_catalog.py` | 같은 스냅샷 세대의 담당자·요청자·프로젝트 명단, 파일/WAL 감지와 캐시 |
| `backend/domain/team.py`, `backend/intent/team_scope.py` | 팀 명단·순수 표현 판단과 기본 팀 조건 조합 |
| `backend/intent/keyword_terms.py`, `constraints.py` | 명시적 조건을 받는 검색어 규칙과 선택 인수 호환 진입점 |
| `backend/intent/json_payload.py` | 모델 JSON 추출 공통 규칙 |
| `backend/intent/validation_rules.py`, `execution_contract.py` | 근거·상태·기간 검증 규칙과 확정 계획/실행 조건 일치 검사 |

기존 `text_scope.py`의 순수 제목·본문 파서는 재사용했다. `query_planning`은 계획 구성·검증 조합을 유지하고 하위 소비자는 query의 규칙을 직접 사용한다. `aggregation_spec`, `condition_grounding`, `unified_validation`이 공유 규칙으로 내려가도록 해 검증 계층의 역참조도 해소했다.

계획의 기본 목적지에 더해 `keyword_terms`와 intent의 네 조합 모듈을 추가했다. 기존 선택 인수/한 인수 호출을 보존하면서 하위 규칙이 조건 추출기·명단 저장소를 다시 호출하지 않게 하기 위한 분리다.

## 기존 경로와 동작 보존

- `query_planning`, `team_scope`, `intent.entities`, `intent.search_terms`, `intent.constraints`, `intent.unified_validation`, `notion.search`, `notion.query_text`의 기존 정의 이름과 공개 진입점을 유지했다. `search_terms.__all__`, 관련 내부 상수, 캐시 함수도 재수출한다.
- 캐시·잠금은 people_catalog가 한 번만 소유하며 옛 경로도 같은 객체를 참조한다. 기존 파일 교체·WAL 변경·읽기 중 세대 변경·일시적 DB 오류 후 재시도 동작을 유지했다.
- `parse_notion_constraints(text, *, known_people=...)`는 명단을 인수로 받는다. 기존 `extract_notion_constraints(text)`는 명단을 한 번 읽어 전달한다. 명단별 담당자 matcher 캐시도 유지한다.
- 순수 본문 근거 병합은 이미 추출한 `grounded`를 받는다. 기존 두 인수 진입점은 리터럴을 제외한 구조 텍스트의 공통 조건을 한 번 추출해 전달한다. 원래 질문의 본문 조건과 OR·제외 조건을 보존한다.
- 본문 검색은 처음 계산한 Notion 조건을 재사용한다. 같은 검색 안에서 native 조건 추출을 두 번 호출하던 부분을 한 번으로 줄였다.
- 유사 검색의 하위 판정은 명시적 조건을 받는다. 기존 선택 인수 호출은 조건이 생략됐을 때만 공통 추출기를 한 번 호출하고 같은 판정 함수를 사용한다.
- 테스트 patch는 명단·날짜·검증 함수의 실제 소유자 또는 소비 지점으로 이전했다. 옛 patch 위치를 찾는 동적 호환 장치는 추가하지 않았다. 기존 import 제공과 테스트 patch 경로 보존은 별개다.

직접 AST 검토에서 이동 함수 76개의 구현은 import를 제외하면 동일했다. 명단·grounded 인수 추가와 호환 조합 함수는 별도 검토했다. 기존 파일에 남은 함수 중 import 외 변경은 `execute_notion_search`의 native 조건 재사용과 `validate_plan`의 날짜 소유자 연결이다. 연산 함수의 지연 타입 주석 방식도 종전과 동일하게 유지했다.

## 고정 문항과 회귀 검증

날짜를 2026-10-30으로 고정하고 `.env` 및 외부 네트워크를 차단했다. 원래 310문항에 구현 전에 캡처한 경계 질문 5개를 더했다. 새 행에서 빠졌던 authority/candidate_people 메타데이터는 변경 전 캡처로 보완했으며 기존 질문 기대값은 바꾸지 않았다.

| 비교 | 범위 | 결과 |
|---|---|---|
| 고정 질문 | 315개 계획·조건, 그중 기록된 모델 제안 61개, 결정형 답변·출처·근거 검증 267개 | 315/315 동일, 출력 JSON 바이트 일치 |
| 기존 helper 경계 | 윤년·연말·기간 역전·잘못된 월, 제목 속 날짜/상태, 미할당 담당자, 팀 표현, 유사 검색 등 36사례 | 변경 전 출력·예외 타입/문구 유지 |
| 추가 경계 계약 | 순수 파서의 명시 명단, 명단 교체, 호출 횟수, OR/본문 제외, 입력 불변성, 선택 인수, 기존 경로/캐시 동일성, 독립 import의 I/O 차단 | 모두 통과 |

실제 모델이나 운영 Notion/Mantis API를 재호출한 비교는 아니다. 따라서 이 일치율은 기존 결과 보존을 의미하며 실제 모델 정확도 100% 또는 운영 응답 시간 개선을 의미하지 않는다.

전체 회귀에는 unified planner on/off, 후속 질문의 조건 교체·해제, 제목/본문 리터럴, 고객사 별칭 경계, 사용자 범위, 명단 캐시 교체 및 오류 재시도가 포함된다. 순수 query import를 별도 프로세스에서 확인하고 SQLite 연결·소켓 연결 및 intent/검색/스키마/명단 모듈 유입을 검사한다.

## 구조 결과

- intent/query/team을 포함하는 런타임 순환 그룹: **0개**.
- intent/query/query_planning → `notion.search` 참조: **0개**.
- 함수 밖 import 순환: **0개**.
- 기존 최대 29개 모듈 순환은 **Notion 내부 12개 모듈**로 축소됐다. 이 12개 모듈의 순환은 P4 대상이다.
- 전체 프로젝트의 런타임 순환 그룹 수는 **7개**로 유지된다. Notion 및 나머지 리포트·원천·서비스 순환을 모두 제거했다는 뜻은 아니다.
- 해소된 순환 예외 **71기록/78참조**, 금지 방향 예외 **12기록/12참조**만 제거했다. 기존 예외를 확대하지 않았다.

새 경계 검사는 순환 재발뿐 아니라 query의 의도 해석·원천·HTTP/검색 계층 역참조, team/people_catalog의 의도 조합 역참조, validation_rules의 상위 검증 역참조를 막는다. 새 검사에 들어간 기존 파사드 금지 문자열이 전역 참조 검사에 잡혀 중복 항목을 제거했으며, 전역 파사드 검사는 그대로 유지했다.

## 최종 검증

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,745개 통과**, 커버리지 **90%**. 변경 전 2,734개에서 새 계약 검사 11개를 추가했다.
- compile: 프런트 빌드 산출물 일치 검사와 Python compileall 성공.
- shell, ruff 성공. mypy **205개 소스 파일 성공**.
- audit: 기존 Python 보안 예외 **5건** 외 알려진 취약점 없음, npm 취약점 **0건**. 의존성 핀과 보안 예외를 변경하지 않았다.
- `git diff --check` 통과.

P3 완료. 다음 단계는 P4의 Notion 내부 의존 방향과 검색·변환·저장본 책임 분리다.

증거는 `assistant_web/runtime/architecture-p3/`의 `before-manifest.json`, `before-tests.log`, `before-expanded-questions.json`, `question-comparison.json`, `ast-review.json`, `legacy-defined-symbols.json`, `removed-exceptions.json`, `before-graph.json`, `after-graph.json`, `check-final.log`에 있다. 실행 산출물은 커밋하지 않는다.

고정 helper fixture는 `assistant_web/tests/fixtures/query_boundary_cases.json`, 추가 질문은 `assistant_web/tests/fixtures/routing_cases.jsonl`에 보관했다.
