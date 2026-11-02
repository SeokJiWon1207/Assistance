# P5-B Mantis·검색 실행 책임 분리 결과

## 범위와 기준선

P5-B를 완료했다. 선행 미커밋 변경을 포함한 코드·테스트·fixture·설정·계획 **588개 파일**과 해시를 `assistant_web/runtime/architecture-p5b/`에 보관하고 현재 작업 공간에서 진행했다. 변경 전 전체 테스트는 **2,756개**, 커버리지 **90%**였다. `.env`와 운영 DB는 복사하지 않았다.

AGENTS.md에 따라 메인 에이전트가 직접 구현·검토했다. 스킬의 위임·자동 worktree/커밋 절차 대신 앞 단계와 같은 기준선·runtime 기록 방식을 유지했다. P5-C에 착수하지 않았으며 커밋·서버 재시작·배포는 수행하지 않았다.

## 책임 분리

| 소유자 | 책임 |
|---|---|
| `sources/mantis_fields.py` | 원문 필드·날짜·열거형·노트·이력 정규화, 검색문과 출처 행 구성 |
| `sources/mantis_matching.py` | 고객사·매체·상태·날짜·화면·담당자 조건, 키워드 점수와 행 선택 |
| `sources/mantis_client.py` | HTTP 요청, 재시도·대기, 동시 요청 한도와 상세 조회 예산 |
| `sources/mantis_snapshot_query.py` | 전체 저장본·Chroma·구조화 색인 조회와 off/shadow/primary 폴백 |
| `sources/mantis_index.py` | 구조화 색인의 파싱·생성·검증·후보 SQL |
| 기존 `sources/mantis.py` | 필터·ID·페이지 검색, 저장본/실시간 선택, 재검증·신선도와 기존 함수 경로 |
| `services/retrieval_pipeline.py` | Mantis·Notion·Chroma 실행 순서, 설명용 조회, 조건 전달과 검색 단계 조합 |
| 기존 `retrieval.py` | 출처·신선도 안내·실행 흔적 표시와 이전 경로의 재내보내기 |

저장본 query가 도구 계층을 다시 참조하지 않도록 색인 구현도 `sources/mantis_index.py`로 옮겼다. 기존 `tools/mantis_snapshot.py`와 `tools/mantis_index.py`는 명시적 재내보내기로 유지한다. 새로운 저장 방식이나 스키마는 도입하지 않았다.

`mantis_store`는 HTTP를 client에서, 원문 정규화를 fields에서 가져온다. 검색 projection도 fields의 검색문 변환을 사용한다. 원천 저장소가 Mantis 검색 조정 모듈이나 tools를 역참조하지 않는다. 검색 실행을 services로 옮겼으므로 기존 루트 retrieval patch도 실제 소비 지점으로 이전했다.

## 보존한 계약과 테스트 이전

- 기존 Mantis 정의 함수와 도구 함수, `run_retrieval_pipeline(query_text, limit, tools, intent_plan=None)`의 import 경로·인수·반환·예외를 유지했다. P5-A의 행 정리·모델 문맥 소유자도 그대로 사용한다.
- 함수·세마포어·색인 검증 캐시·예외 클래스가 옛 경로와 새 소유자에서 같은 객체임을 검사한다. `sys.modules` 조회나 monkeypatch 우회 장치를 추가하지 않았다.
- HTTP 요청 횟수, 429/5xx/timeout 재시도, 대기 간격, 상세 조회 권한 오류, 비교 요청의 갱신 예산을 유지했다. 기존 스냅샷 로그 채널도 유지했다.
- 노트/필드 정규화, 고객사·제목·본문 필터, 점수·정렬·중복 처리, 오래된 저장본의 폴백과 실시간 재검증 로직을 바꾸지 않았다.
- 기존 원천별 patch는 소비자 위치를 유지하고, 옮긴 HTTP 의존성과 pipeline·snapshot/index 내부 patch만 새 소유자로 이전했다. 정확도 스크립트의 pipeline import도 함께 이전했다.
- 초기 검사에서 옛 HTTP patch, decorator보다 늦게 들어간 테스트 import, workspace 정규화 patch 위치를 수정했다. 검색 결과와 기존 기대값을 수정해 통과시키지 않았다.
- 변경 전 실행에서 캡처한 **37개 전체 결과·호출 기록**을 `tests/fixtures/mantis_execution_golden.json`에 고정했다. 테스트는 실제 함수에 임시 원천과 가짜 HTTP를 연결하며 외부 서비스를 호출하지 않는다.
- 필드·조건 모듈만 새 프로세스에서 import할 때 DB/HTTP 접근이 없고 저장소·검색 실행 모듈이 함께 로드되지 않는 것도 검사한다.

## 변경 전후 비교

비교는 `.env`와 외부 네트워크를 차단했다. 이전 코드를 보관한 디렉터리에서 기준선을 실행하고 같은 입력을 변경 후 코드에 재생했다.

| 비교 | 범위 | 결과 |
|---|---|---|
| 고정 질문 | 317개 계획·조건, 기록된 모델 제안 61개, 답변·출처·검증 269개 | **317/317 동일**, JSON 바이트 일치 |
| 필드·필터·정규화 | 기존 회귀 테스트 388개에서 기록한 입력/반환값 14,234개 | **14,234/14,234 동일** |
| HTTP | 페이지/상세의 정상, timeout 재시도, 429, 5xx, 404, 403, 재시도 소진 14사례 | 결과·URL·인수·대기 동일 |
| 저장본·실시간 검색 | 고객사·상태·제목/본문·빈 결과·오름차순 10사례 | 결과·메타데이터·정렬·호출 동일 |
| 상태 재검증 | 상태 변경·삭제/미발견·timeout·접근 거부 4사례 | 반환·제외·실패 표시 동일 |
| 실행 조합 | Mantis 목록/ID/설명/유사/기간, Notion 고객사/본문, 양쪽 검색, 일반 질문 9사례 | 원천별 호출 순서·인수·단계 결과 동일 |

14,234개 로컬 함수 호출을 준비 실행 1회 후 5회 반복했다. 아래 배치 시간은 입력 복사를 포함한 호출 시간 합계의 중앙값이다.

| 측정 | 변경 전 | 변경 후 |
|---|---:|---:|
| 전체 배치 중앙값 | 0.1555초 | 0.1565초 |
| 마지막 반복의 개별 호출 p50 | 0.0051ms | 0.0052ms |
| 마지막 반복의 개별 호출 p95 | 0.0495ms | 0.0496ms |

배치 전체에서 약 1ms 차이다. 이는 로컬 변환·필터 비용이며 **실제 모델/API/원격 검색을 포함한 응답 시간 측정은 아니다**. 실제 Luna/Gemini 호출은 **0회**였다. 이번 일치율은 동작 보존 결과이며 모델의 새로운 정답률을 의미하지 않는다.

## 구조와 전체 검증

- Mantis 4개 모듈의 SCC 해소. 전체 SCC **3 → 2개**, import 시점 SCC **0개 유지**.
- Mantis 저장소/projection → 검색 조정, sources → tools 참조 **0개**.
- 해소된 순환 예외 **9기록/10참조**, 금지 방향 **1기록**, 파사드 소비 **19기록**만 제거했다. 새로운 제품 의존 예외는 추가하지 않았다.
- `backend.retrieval` patch 이전 목록 **44기록/56참조 → 0개**. 최초 제안의 36이라는 수치를 목표로 사용하지 않았다.
- 변경 파일의 기존 함수·클래스 **351개**를 AST로 대조했다. import 소유자 변경을 제외하면 모두 동일하다.
- `sources/mantis.py` **1,107 → 680줄**, `retrieval.py` **803 → 188줄**, `tools/mantis_snapshot.py` **294 → 13줄**.

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,763개 통과**, 커버리지 **90%**.
- compile: Python과 프런트 산출물 일치 검사 통과.
- shell, ruff 통과. mypy **235개 소스 파일 통과**.
- audit: 기존 Python 보안 예외 **5건** 외 알려진 취약점 없음. npm 취약점 **0건**. 의존성 핀과 보안 예외는 변경하지 않았다.
- `git diff --check` 통과.

메인 에이전트가 의존 closure, 이전 진입점·공유 객체, 예외·호출 순서, 테스트 patch, 변경 범위를 직접 검토했다. 구현자가 수행한 검토이며 별도 리뷰 에이전트는 사용하지 않았다.

증거는 `assistant_web/runtime/architecture-p5b/`의 기준선 manifest, `before-tests.log`, `evidence-cases.pickle`, `capture-evidence.json`, `questions-comparison.json`, `before-search.json`, `after-search.json`, `search-comparison.json`, `before-timing-evidence.json`, `after-timing-evidence.json`, `ast-review.json`, `resolved-exceptions.json`, 그래프 전후 파일, `check-final.log`에 있다. runtime 산출물은 커밋하지 않는다.

다음 단계는 **P5-C: 채팅 단계와 실행 컨텍스트 분리**다. 남은 순환은 `chat_service ↔ unified_planning`과 P6의 `workspace ↔ reports.team_brief`다.
