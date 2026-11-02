# P4 Notion 내부 순환 해소 결과

## 범위와 기준선

P3 다음 단계인 P4를 완료했다. 변경 전 작업 공간의 코드·테스트·fixture·계획 518개 파일과 해시를 `assistant_web/runtime/architecture-p4/`에 보관했다. 선행 구조 개선과 고객사·제목/본문·모델·UI 변경을 기준선에 포함했다. 변경 전 전체 테스트는 2,745개 통과, 커버리지 90%였다.

실행·검증 스킬을 적용하고 AGENTS.md에 따라 메인 에이전트가 직접 구현·검토했다. P5에는 착수하지 않았으며 커밋·서버 재시작·배포는 수행하지 않았다.

## 책임 분리

| 소유자 | 책임 |
|---|---|
| `notion/values.py` | rich text·사용자·제목·속성·블록·업무일 변환 |
| `notion/page_ownership.py` | 입력 페이지/블록의 소속과 동기화 복제 근거 판정 |
| `notion/read_api.py` | 실제 조회 HTTP, 페이지 본문·사용자·DB 캐시 |
| 기존 `notion/schema_cache.py` | 디스크 스키마와 읽기 범위에서 받은 스키마 seed |
| `notion/cache_maintenance.py` | 앱 시작·주기 실행의 스키마/목록 사전 갱신 |
| `notion/crawl_transport.py` | 공유 요청 예산, 재시도·429 대기 상태와 예외 |
| `notion/crawl_validation.py` | 페이지 상위 경로·동기화 원본의 접근/소속 확인 |
| `notion/snapshot_state.py` | 목록·속성·본문 완전성, 유효 기간과 coverage |
| `notion/matching.py` | 순수 조건·점수·정렬·중복 제거와 본문 후보 판정 |
| `notion/page_enrichment.py` | 저비용 조건 확인 후 필요한 원격 담당자 보강 |
| `notion/server_filters.py` | 스키마별 API 필터·중첩 한도·조회 범위 구성 |
| 기존 `notion/body_index_search.py` | 색인 후보의 메타데이터 조회와 순수 matcher 조합 |

기존 `client` 경로를 유지하면서 warmer의 역참조를 끊기 위해 실제 조회 구현을 `read_api`로 분리했다. 값만 판정하는 matching에 저장소 모듈이 따라오지 않도록 소속 판정도 `page_ownership`으로 옮겼다. 원격 담당자 조회는 `page_enrichment`가 맡는다. 이 세 파일은 계획의 책임 분리를 완성하기 위한 추가 목적지다.

## 보존한 계약과 테스트 이전

- `client`, `mapping`, `search`, `body_index`, `body_index_sync`, `complete_snapshot`의 기존 정의 이름과 진입점을 재수출한다. 동적 `sys.modules` 조회나 patch 우회 장치를 추가하지 않았다.
- 스키마 seed·DB 캐시·사용자 힌트·재시도 잠금은 실제 소유 모듈에 한 번만 존재한다. 이전 경로와 새 경로가 같은 함수/캐시 객체를 참조하는 계약을 검사한다.
- `_Budget`, `_ReadFailure`, `_request`는 새 소유자에서 `CrawlBudget`, `NotionReadFailure`, `request_json`으로 내부 공개화했다. 기존 이름도 유지하며 예산 필드·재시도 횟수·실패 reason·HTTP 옵션은 보존했다.
- `read_scope`는 seed를 `schema_cache`에 직접 전달한다. main은 `cache_maintenance`의 warmer를 시작하고 기존 종료 Event 처리를 유지한다. 사전 갱신 순서, 스레드 시작과 중지, TTL의 절반 이내 반복 주기를 검사했다.
- `_indexed_body_matches`의 메타데이터 조회는 `body_index_search`로, 이미 읽은 페이지/블록의 조건 판정은 matching으로 분리했다. 부분 본문의 부정 조건, 소유 문서 구분, 근거 블록·점수·중복 제거 순서를 유지한다.
- 담당자 보강은 기존처럼 상태·제목·기간 등 저비용 조건을 먼저 통과한 후보에만 실행한다. matcher 자체는 HTTP나 저장소를 읽지 않는다.
- HTTP·스키마 소비자는 실제 읽기 API 모듈을 명시적으로 참조한다. 테스트 patch도 그 소비 경계로 이전했다. 원래 위치의 patch를 찾는 생산 코드는 없다.

초기 테스트에서 옛 patch가 대기 함수를 대체하지 못해 고정 시계의 수집 테스트가 멈추는 문제가 있었다. 해당 검사 프로세스만 종료하고 공유 crawl transport의 대기·예산 경계로 patch를 이전했다. 이후 남은 실패도 이동한 조회/보강 함수의 patch 및 테스트 import 위치, 실제 소유 모듈 검사로 확인해 수정했다. 질문·검색 결과 기대값을 바꿔 통과시키지 않았다.

## 변경 전후 비교

기존 315문항에 구현 전에 캡처한 제목+본문 조건 및 미할당 담당자+코드 제외 질문 2개를 추가했다. 모든 비교는 `.env`와 외부 네트워크를 차단한 결정형 실행이다.

| 비교 | 범위 | 결과 |
|---|---|---|
| 고정 질문 | 317개 계획·조건, 기록된 모델 제안 61개, 결정형 답변·출처·검증 269개 | **317/317 동일**, JSON 바이트 일치 |
| Notion 경계 | 값·날짜 변환 13사례와 실제 검색 흐름 5사례 | **18/18 동일**, 실행마다 달라지는 `meta.queriedAt`만 비교에서 제외 |
| HTTP 계약 | 위 검색 흐름의 GET/POST 호출 순서·횟수·payload·timeout 등 옵션 | 동일 |

원본 비교 파일에는 조회 시각도 그대로 남겼고, 제외 필드는 비교 기록에 명시했다. 실제 모델이나 운영 Notion/Mantis API를 재호출하지 않았으므로 이 결과는 기존 동작 보존을 뜻하며 실제 모델 정확도나 운영 응답 시간 개선을 뜻하지 않는다.

변경 전 출력으로 만든 값/날짜 13사례는 `tests/fixtures/notion_boundary_cases.json`에 보관했다. 전체 회귀는 429/timeout/취소, 속성 페이지네이션·반복 커서, 부분 본문, 이동/삭제/권한 회수, 최신/만료 저장본, 캐시 갱신 중 읽기, 사용자·고객사·제목/본문 필터를 포함한다. 순수 values/matching의 독립 import에 저장소·조회 모듈이 유입되지 않는 검사도 통과했다.

직접 AST 검토에서 이동 함수·클래스 **81개**는 import 경로, 명시적인 API 모듈 접근, 승인된 crawl 이름 변경을 정규화하면 동일했다. 기존 파일에 남은 함수도 같은 정규화에서 모두 동일했다. 별도로 나눈 본문 후보의 조회/판정 경계는 직접 검토하고 기존 색인·검색 회귀로 확인했다.

## 구조와 전체 검증

- Notion을 포함하는 런타임 순환 그룹: **0개**.
- 함수 밖 import 순환: **0개**. P3에서 해소한 intent/query/team 순환도 재발하지 않았다.
- 전체 런타임 순환 그룹은 **7 → 6개**로 줄었다. 남은 리포트·원천·서비스 순환은 P5/P6 대상이다.
- 해소된 순환 예외 **55기록/55참조**만 제거했다. 구조 예외를 추가하거나 확대하지 않았다.
- 값·매칭·소속·스키마 캐시·crawl·snapshot 상태가 검색/갱신 조정 계층을 역참조하지 못하도록 경계 검사를 추가했다.

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,751개 통과**, 커버리지 **90%**.
- compile: 프런트 빌드 산출물 일치 검사와 Python compileall 통과.
- shell, ruff 통과. mypy **215개 소스 파일 통과**.
- audit: 기존 Python 보안 예외 **5건** 외 알려진 취약점 없음. npm 취약점 **0건**. 의존성 핀과 보안 예외는 변경하지 않았다.
- `git diff --check` 통과.

증거는 `assistant_web/runtime/architecture-p4/`의 `before-manifest.json`, `before-tests.log`, `questions-comparison.json`, `notion-comparison.json`, `ast-review.json`, `legacy-definitions.json`, `removed-exceptions.json`, `before-graph.json`, `after-graph.json`, `check-final.log`에 있다. 실행 산출물은 커밋하지 않는다.

다음 단계는 **P5-A: 근거·리포트 공통 책임 분리**다.
