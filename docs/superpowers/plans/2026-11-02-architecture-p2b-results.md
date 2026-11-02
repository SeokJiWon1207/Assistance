# P2-B 설정 로딩과 Notion 통신 분리 결과

## 범위와 기준선

P2-A 다음 단계인 P2-B를 수행했다. 기존 작업 공간의 코드·테스트·fixture·계획 439개 파일과 해시를 `assistant_web/runtime/architecture-p2b/`에 보관한 후 변경했다. README도 수정 전 사본을 별도로 보관했다. 선행 고객사·제목/본문 검색·LLM 설명·보안·UI 변경은 보존했다.

기존 실행·검증 스킬을 적용하고 AGENTS.md에 따라 위임 없이 직접 구현·검토했다. 이번 범위는 명시 초기화 및 앱·CLI·테스트 연결까지다. 커밋, 서버 재시작, 배포, P3 착수는 하지 않았다.

## 책임 분리

| 소유자 | 책임 |
|---|---|
| `backend/settings/paths.py` | 기존 프로젝트·설정·실행 데이터 경로, 실행 디렉터리 준비 |
| `backend/settings/catalogs.py` | 고객사 JSON 로더 3개, 공유 사전, 명시 초기화와 접근 함수 |
| `backend/settings/notion.py` | 환경변수·DB ID 우선순위, API 상수, 헤더, 네트워크 없는 설정 조회 |
| `backend/notion/discovery.py` | 연결 DB 탐색, 토큰별 성공/실패 캐시, 발견이 필요한 설정 해석 |
| `backend/notion/transport.py` | 요청 간격과 공유 잠금, 429 대기, 재시도, 요청 예산 |

순수 환경 설정과 경로를 별도 파일로 둬 settings가 config 호환 경로나 HTTP 구현을 역참조하지 않게 했다. P2-A의 정적 values 모듈은 그대로 재사용한다.

`get_configured_notion_settings()`는 환경에 명시된 설정만 읽는다. `resolve_notion_settings()`는 종전과 같이 토큰이 있고 명시 DB가 없을 때만 탐색한다. 기존 `get_notion_settings()` 소비자는 모두 실제 DB 목록·범위·연결 지문·진단에 발견 결과가 필요하므로 resolver로 이전했다. 명시 ID 우선 확인과 헤더 생성은 기존처럼 순수 경로를 사용한다.

`config.get_notion_settings`와 `client.get_notion_settings`는 같은 resolver 함수를 제공한다. config의 기존 공개 HTTP/발견 함수와 상수도 실제 소유자의 동일 객체를 재내보낸다. 동적 조회나 래퍼 캐시는 추가하지 않았다. config가 HTTP 구현을 소유하지 않지만, 이전 공개 경로를 제공하기 위해 해당 모듈을 import하는 호환 역할은 남아 있다.

## 명시 초기화와 호환성

- 고객사 JSON을 import 시점에 읽지 않는다. `initialize_customer_catalogs()`가 별칭 → 프로젝트 표시명 → 고객사 이름표 순으로 기존 로더를 실행하고, 이름·기존 별칭·프로젝트·추가 별칭의 병합 순서를 유지한다.
- 세 로더가 성공한 후 공유 사전을 채운다. 기존 사전 객체를 교체하지 않아 config/common의 공개 참조와 고객사 캐시가 같은 목록을 사용한다. 초기화는 잠금으로 한 번만 실행하며 반복 호출이 기존 내용을 덮어쓰지 않는다.
- 잘못된 별칭 JSON은 기존 경고와 기본값을 유지한다. 잘못된 필수 고객사 이름표는 초기화 오류로 전파한다. 초기화 전에 접근하면 명확한 오류를 내며, 오류 후에는 올바른 파일로 다시 초기화할 수 있다.
- FastAPI lifespan은 서비스 초기화와 요청 수락 전에 고객사 목록을 준비한다. 관련 CLI와 격리 테스트 runner도 같은 초기화 함수를 호출한다. 별도 Python 호출자에게 필요한 준비 절차는 README에 기록했다.
- 생산 소비자는 `get_customer_aliases()` 등 접근 함수를 사용한다. 상태 근거 판정의 고객사 파생 집합도 사용 시점에 만들어 import 시점의 빈 목록을 고정하지 않는다. 기존 고객사 캐시 크기·명시적 cache_clear 동작은 유지한다.
- 테스트의 config HTTP/캐시 patch와 소비 모듈의 기존 getter patch를 새 소유자/소비 지점으로 이전했다. 공개 경로 동일성 검사는 별도의 legacy 계약 테스트에 남겼다.

## 변경 전후 비교

구현 전 전체 테스트 **2,723개 통과**를 확인하고 고정 출력 두 묶음을 만들었다. 새로운 설정 경계 검사 6개가 기존 import 시 JSON 읽기 및 분리 모듈 부재 때문에 실패하는 것도 확인한 뒤 구현했다. 기존 기대값을 바꿔 통과시키지 않았다.

| 비교 | 범위 | 결과 |
|---|---|---|
| 고정 질문 | 310개 계획·조건, 그중 61개 기록된 모델 제안 검증, 262개 결정형 답변·출처·근거 검증 | 310/310 동일 |
| 설정·통신 | 환경 우선순위·명시/자동 DB·빈 결과·인증 실패·부분 탐색·상한 초과·캐시·HTTP/네트워크 재시도·JSON 로더 | 29/29 동일 |

두 묶음 모두 출력 JSON이 바이트 단위로 일치했다. 설정·통신 비교는 요청 수·요청 인수·timeout·대기 시간·실패 캐시 만료와 예외 타입/문구도 포함한다. 실제 모델이나 운영 원천을 재호출하지 않은 결정형 비교이며, 실제 모델 정확도·응답 시간 개선을 뜻하지 않는다.

추가 검사에서는 성공 TTL 만료와 실패 30초 후 재탐색, 반환 목록 변경이 캐시를 오염시키지 않는지, 동시/중복 초기화, 필수 설정 오류 시 앱 시작 차단, 독립 CLI의 검색 결과 일치를 확인했다. 기존 429 cooldown·취소·보안·스키마 캐시 회귀도 통과했다.

AST 대조에서 이동한 **13개 기존 함수**의 본문·서명은 동일했다. 소비자 **40개 파일**은 import 경로, 고객사 접근 함수, resolver 명칭, 명시 초기화 호출을 제외하면 AST가 동일하다. config 조합과 상태 근거의 파생 집합은 별도로 직접 검토했다. 기존 routing fixture는 변경하지 않았다.

## 구조와 최종 검증

settings→Notion 실행 계층과 transport/discovery→config·검색·상위 조합 계층의 역참조를 금지하는 규칙을 추가했다. 기존 구조 예외는 확대하거나 갱신하지 않았다. 전체 런타임 Python 순환 그룹 **7개**, 함수 밖 import 순환 **0개**, 동적 호환 조회 구현/호출 **0/0**을 유지한다. 나머지 순환 및 P9 파사드는 후속 단계 범위다.

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,734개 통과**, 커버리지 **90%**.
- compile: 프런트 빌드 산출물 검사와 Python compileall 성공.
- shell, ruff 성공. mypy **191개 소스 파일 성공**.
- audit: 기존 Python 예외 **5건** 외 알려진 취약점 없음, npm 취약점 **0건**. 의존성 핀과 보안 예외를 변경하지 않았다.
- `git diff --check` 통과.

증거: `assistant_web/runtime/architecture-p2b/{before-manifest.json,before-tests.log,boundary-red.log,questions-comparison.json,settings-comparison.json,moved-functions-identical.json,consumer-equivalence.json,after-graph.json,check-final.log,completion.json}`. 실행 산출물은 커밋하지 않는다.

P2-B 완료로 P2의 공통 함수·설정·통신 책임 분리를 마쳤다. 다음 단계는 P3의 query/intent/team 순환 해소이며 아직 착수하지 않았다.
