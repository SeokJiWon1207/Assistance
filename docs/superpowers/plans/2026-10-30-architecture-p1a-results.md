# P1-A 실행 결과 — 2026-10-30

P1-A의 코드 이전·결정형 회귀 검증과 실제 모델 전후 비교를 완료했다. Gemini 결제 오류 이후 사용자 요청으로 **OpenAI GPT-6 Luna**를 연결해 고정 11문항(13턴)을 양쪽 코드에서 실행했다. 변경 전 9/11, 변경 후 10/11이며 새 실패는 없다. 모델 계획과 실시간 API 응답에는 차이가 있어 점수 증가를 구조 변경의 개선 효과로 단정하지 않는다. P1-B 이후에는 착수하지 않았다.

## 코드 변경

| 소비 모듈 | 정리한 호환 조회 | 최종 호출 방식 |
| --- | ---: | --- |
| `backend/notion/schema.py` | 3 | 설정·스키마·제목 함수를 명시적으로 import |
| `backend/notion/mapping.py` | 3 | 본문·관계 제목·사용자 이름 함수를 명시적으로 import |
| `backend/notion/search.py` | 7 | 설정·HTTP·스키마·제목·본문·사용자 조회를 소비 모듈에서 직접 참조 |
| `backend/notion/write.py` | 2 | 보류 명령 직접 호출, 사용처 없는 설정 wrapper 제거 |

`backend/notion/compat.py`를 삭제했다. `backend/sources/notion.py`와 `backend/notion/core.py`는 변경하지 않아 기존 공개 import 경로를 유지한다. 파사드 제거는 후속 단계다.

`test_search_pipeline.py`의 옛 문자열 patch 7곳을 search/mapping으로 이전했다. `test_notion_security.py`의 객체 patch도 schema/write의 실제 소비 지점으로 옮겼다. 검색 URL·스키마 ID·관계/사용자 ID·호출 횟수와 반환 결과를 함께 확인한다.

쓰기 확인 권한 테스트는 이전 전 새 patch 경로에서 실패했다. 기존 호환 함수가 write에 주입한 fake 대신 core의 함수를 선택했기 때문이다. 직접 호출로 변경한 뒤 같은 테스트가 통과한다. 쓰기 차단 검사는 실제 액션 소비 함수인 `consume_action`이 호출되지 않는지도 확인한다.

작업 시작 시 이미 수정되어 있던 `search.py`의 검색 예산 변경은 그대로 보존했다. 보관한 변경 전 코드와 비교하면 92개 구현 함수 중 보류 명령의 의존 호출 한 곳 외에는 함수 본문 AST가 동일하다. 제거한 14개 wrapper는 이 92개에 포함하지 않는다.

## 구조 검사

- Notion 동적 호환 조회: 15 → 0, 전용 구현 1 → 0.
- 전체 동적 호환 조회: 26 → 11, 구현 4 → 3. 남은 대상은 evaluation과 intent다.
- 해소된 예외만 제거: 파사드 참조 9개, 동적 참조 17개. 기준선을 재생성하거나 새 예외를 추가하지 않았다.
- 기존 순환 간선과 금지 의존 간선의 추가 없음. P0의 역사적 수치와 기준선 메타데이터는 그대로 유지했다.

## 고정 문항과 원문 근거

실제 원문과 모델 입력·출력은 Git에서 제외되는 `assistant_web/runtime/`에만 보관한다.

| 항목 | 위치 또는 내용 |
| --- | --- |
| 고정 문항 | `runtime/accuracy_benchmark/cases.jsonl` |
| 문항 구성 | 11개, 13턴. dev 5개 / final 6개 |
| 범위 | Notion 7개(제목·속성·본문·설명·후속 질문), Mantis 3개(상세·후속 질문·기간 집계), 확인 질문 1개 |
| 문항 SHA-256 | `543ad5b61b12f1fd16032e7bc41c1d2178072f768cc6c63c384e996cf4bd466f` |
| 정답 근거 | `runtime/architecture-p1a/case-oracle-manifest.json`, `oracle-1.json` ~ `oracle-3.json`, `mantis-oracle.json` |
| 원천 복사본 | `runtime/architecture-p1a/source-snapshot/`, `source-snapshot-sha256.json` |
| 변경 전 코드·설정 | `runtime/architecture-p1a/baseline-app/` |
| 현재 측정 상태 | `runtime/architecture-p1a/measurement-status.json` |

Notion 3개 문서의 페이지·블록을 독립 REST 조회로 확인했다. 기간 집계는 제품의 매처를 호출하지 않고 복사한 SQLite 원본의 프로젝트명과 날짜를 직접 비교했다. 원본 날짜가 한국 시간대이며 해당 고객사 프로젝트가 하나임을 확인했다. 문항과 기대값은 첫 실행 전에 고정했고, 실행 결과에 맞춰 수정하지 않았다. 본문 문항은 알려진 문서의 발견 여부를 검사하며 전체 검색 재현율을 주장하지 않는다.

SQLite는 읽기 전용 연결의 backup으로 복사했고 실행마다 별도 runtime을 사용한다. API 요청 헤더·키·쿠키는 HTTP 기록에 저장하지 않는다. 실제 Notion 쓰기는 수행하지 않았다.

## 이전 Gemini 측정 이력

최근 실제 검증과 사용자 확인으로 선택한 모델은 `gemini-3.1-flash-lite`다. 변경 전 보관 코드로 13턴을 실행했으나, 모델 요청 11회 모두 **HTTP 402 / RESOURCE_EXHAUSTED / prepayment credits depleted**를 반환했다. 나머지 2턴은 모델 호출 없는 결정형 목록 응답이다. 정상 생성 응답은 0개이며 채점하지 않았다. 결과는 `runtime/architecture-p1a/before-gemini/`에 보관했다.

2026-10-30 13:42:59 KST에 사용자 요청으로 Gemini 준비 상태를 다시 확인했다. 짧은 요청 1회에도 동일한 HTTP 402와 선불 크레딧 소진 메시지가 반환됐다. 따라서 이번 재확인에서는 고정 문항 실행이나 전후 비교를 시작하지 않았다. 증거는 `runtime/architecture-p1a/gemini-readiness-20261030-134259.json`에 보관했다.

초기 Qwen 선택은 실제 사용하는 모델을 잘못 해석한 것이었다. Qwen 연결 실패 기록은 기준선에서 제외했다. 보조 HTTP 재생 시도도 미기록 추가 요청과 백그라운드 스키마 조회로 완료되지 않아 검증 통과 근거에서 제외했다. HTTP 기록만으로 시각·캐시·비동기 동작까지 결정적으로 재현했다고 주장하지 않는다.

실행기는 후속 시도에서 모델 준비 상태를 먼저 확인하고, 정상 응답이 없으면 문항 실행 전에 중단하도록 보완했다. 문항 전체 실행 후에도 정상 모델 응답 수와 HTTP 상태를 확인해야 한다. `manifest.json`의 `real_model_comparison_completed`가 false이면 정상 모델 정확도 기준선으로 사용하지 않는다.

## 완료한 검증

| 검증 | 결과 |
| --- | --- |
| Notion·검색·공개 import 집중 회귀 | 604개 통과 |
| 의존 그래프·이전 계약 검사 | 21개 통과 |
| `assistant_web/scripts/check.sh` | tests / compile / shell / ruff / mypy / audit 전체 통과 |
| 전체 Python 회귀 | 2,655개 통과, 커버리지 90% |
| 프런트엔드·정적 검사 | Node 회귀, TypeScript, Bklit 빌드 확인, Ruff, mypy 173개 소스 통과 |
| 의존성 검사 | 저장소의 기존 예외 정책 적용 후 추가 취약점 없음 |

로그: `runtime/architecture-p1a/consumer-red.log`, `consumer-green.log`, `architecture-green.log`, `full-check.log`. 변경 보존 근거는 `implementation-preservation.json`, 구조 예외 차이는 `inventory-delta.json`에 있다. 전체 검사 시작 이후 Python 소스의 추가 변경이 없음을 확인했다.

## OpenAI 연결과 완료한 전후 비교

`OPENAI_API_KEY`는 사용자 로컬 `.env`에 등록했고 정상 응답 HTTP 200 / `OK`를 확인했다. 비용과 처리량을 고려해 [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)를 사용한다. OpenAI 전용 요청 변환은 `backend/llm.py`에 추가했다. `max_tokens`를 `max_completion_tokens`로 변환하고 2,048토큰 상한을 적용하며, 다른 제공자 전용 옵션과 `seed`를 제외한다. `reasoning_effort: none`으로 온도 0과 기존 JSON 응답 계약을 유지한다. [공식 호환 조건](https://developers.openai.com/api/docs/guides/latest-model#gpt-6-astra-update-api-and-model-parameters)

모델은 설정 파일과 안전한 기본 레지스트리에 모두 추가했다. 키가 등록되어 활성인 OpenAI 모델을 우선 선택하고, 키가 없는 환경은 기존 활성 모델 순서를 유지한다. 공개 모델 목록에 키나 endpoint를 노출하지 않는다. 로컬 서버는 프로젝트 표준 명령으로 재시작했으며 `/health` HTTP 200과 보호된 `/api/models`의 비인증 HTTP 401을 확인했다. 실제 OpenAI 스트리밍 요청도 HTTP 200으로 한글 `연결 정상`을 수신했다. 연결·스트리밍·서버 확인 근거는 `runtime/openai-connection-20261030/{readiness,stream-readiness,server-health}.json`에 있다.

원래 `baseline-app` 보관본은 그대로 두고 `openai-baseline-app`을 만들었다. 이 평가용 복사본에 현재의 `backend/llm.py`와 `config/models.json`만 공통 적용했다. 양쪽 backend의 차이는 정확히 P1-A의 `notion/{compat,schema,mapping,search,write}.py` 5개다. 준비 근거는 `runtime/architecture-p1a/openai-baseline-preparation.json`에 있다.

| 항목 | 변경 전 | 변경 후 |
| --- | ---: | ---: |
| 전체 고정 문항 | 9/11 | 10/11 |
| dev | 4/5 | 4/5 |
| final | 5/6 | 6/6 |
| 실행한 대화 턴 | 13 | 13 |
| 정상 모델 요청 | 21 | 19 |
| 모델 HTTP 오류·실행 예외 | 0 | 0 |
| 입력 토큰 | 104,952 | 99,901 |
| 출력 토큰 | 1,644 | 1,539 |

사전 연결 점검은 위의 본 측정 사용량에서 제외했다. 문항·모델·모델 설정·모든 설정 파일·실행기·실행일·원천 복사본 해시가 일치하고 원래 복사본 파일도 변하지 않았음을 확인했다. 새 실패 0개, 13턴의 출처 순서와 단계 요약 동일, 대상 Notion 원문 페이지 3개 동일이다.

남은 `P1A-N-BODY` 실패는 본문에 인용한 검색어를 제목 equals 필터로 보존하도록 요구해 확인 질문으로 종료하는 기존 동작이다. `P1A-M-FOLLOWUP`의 담당자 질문은 변경 전 `explain`, 변경 후 `get`으로 모델 계획이 달라져 통과했다. 앞선 설명 턴의 `report_kind`도 달랐다. 필터·원문 변경에 따른 통과 증가나 구조 리팩터링의 개선 효과로 해석하지 않는다. 기대값·프롬프트·라우팅·검색 조건을 점수에 맞춰 수정하지 않았다.

실시간 원천 API는 완전히 동일하지 않았다. 공통 요청 형태 419개 중 응답 집합이 다른 형태가 6개였고, 변경 후 Notion HTTP 429가 5회 관측됐다. 조회 요청 수와 모델 문구도 달랐다. 따라서 위 결과는 이 고정 표본에서 새 실패가 관측되지 않았다는 근거이며, 전 동작의 결정적 동일성이나 전체 정확도를 증명하는 수치가 아니다. 원천 HTTP 기록과 모델 응답은 runtime에 보존했다.

원문 산출물: `runtime/architecture-p1a/{before-openai,after-openai}/`의 `manifest.json`, `responses.jsonl`, `http.jsonl`, `report.json`, `report.md`. 통합 비교는 `runtime/architecture-p1a/openai-comparison.json`이다. 생성 감사 ID·실행 시간·조회 시각은 비교에서 제외했고, 계획의 생성된 감사 참조를 제외한 필터와 조건은 유지해 비교했다.

OpenAI 요청·키 처리·JSON·출력 상한·한글 스트리밍 테스트 6개를 추가했다. 관련 집중 검사 61개와 전체 `check.sh`가 통과했다. 최신 전체 결과는 Python 2,661개, 커버리지 90%, mypy 173개 소스 통과이며 로그는 `runtime/openai-connection-20261030/full-check.log`에 있다. 키 값이나 원문 산출물은 커밋하지 않았다.

## 재실행 방법

아래 임시 실행기와 원문 산출물은 해당 작업의 로컬 runtime에 있다. 새 checkout에는 포함되지 않는다. 저장소 루트에서 실행한다. 기존 실패 실행 폴더를 덮어쓰지 않는다.

```bash
assistant_web/.venv/bin/python assistant_web/scripts/accuracy/benchmark.py validate assistant_web/runtime/accuracy_benchmark/cases.jsonl
assistant_web/.venv/bin/python assistant_web/runtime/architecture-p1a/run_comparison.py before --before-app openai-baseline-app --out before-ready --model-id gpt-6-luna
assistant_web/.venv/bin/python assistant_web/runtime/architecture-p1a/run_comparison.py after-live --out after-ready --baseline before-ready --model-id gpt-6-luna
```

`before`는 같은 OpenAI 연결을 적용한 P1-A 이전 평가용 backend/config를, `after-live`는 현재 코드를 사용한다. 문항·모델 설정·원천 복사본 해시와 실행 날짜를 비교한다. 실제 API 원천은 두 실행 사이에 변할 수 있으므로 HTTP 기록과 원문 수정 시각도 확인한다. 입력 변경·조회 범위 차이·모델 장애가 있으면 그 문항의 전후 차이를 코드 회귀로 단정하지 않는다.

두 실행이 정상 완료되고 원천·설정의 비교 가능성을 확인한 뒤 각각 `benchmark.py score`로 채점한다. 기준선의 기존 실패와 새 실패를 구분한다. 본 단계에서 발견된 기존 정확도 문제는 후속 작업으로 다루며 구조 리팩터링에 섞어 수정하지 않는다.
