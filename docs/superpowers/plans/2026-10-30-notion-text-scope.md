# Notion 제목·본문 검색 구분 수정

## 문제와 수정 범위

고정 개발 문항 `P1A-N-BODY`의 `본문에 '안정성'이 나오는 문서`에서, 공통 파서가 `나오는`을 본문 조건으로 소비하지 못했다. 남은 따옴표가 제목으로 분류되어 통합 플래너 검증이 `title equals`를 요구했다. 실제 Luna의 첫 응답은 이미 올바른 `keyword contains` 계획이었다.

`backend/notion/query_text.py`의 공통 연산자 문법을 수정했다. `나오는·나온·언급된·적힌`은 포함, `나오지 않는·언급되지 않은·포함되지 않은`은 제외 조건이다. 기존 본문·코드·표 범위와 따옴표 내부 보호를 함께 사용하므로, 계획 검증과 실제 조회가 같은 구분을 적용한다. 제목 그대로 조회하는 계약과 기존 제외 조건은 유지한다.

제품 코드 수정은 이 파일 하나다. 모델·프롬프트·검색 한도·권한·타임아웃은 변경하지 않았다. 이전 P1-A 구조 변경과 Luna→Gemini 전환은 작업 시작 시 상태 그대로 포함하며, 이전 평가 결과를 덮어쓰지 않는다.

## 결정형 검증

- 수정 전 신규 사례에서 제목 오류와 본문 검색 실패를 재현했다. 집중 테스트 46개 실행에서 하위 사례 포함 실패 22개를 기록했다.
- 신규 테스트 6개는 여러 포함·제외 표현, 네 종류의 따옴표, 제목 내부의 본문 표현, 제목과 본문 동시 조건, 코드 범위, 제목·속성에만 일치하는 문서의 배제를 검증한다.
- 기존 `routing_cases.jsonl`을 보존하고 포함·제외·제목 병행 사례 세 줄을 추가했다.
- 수정 후 관련 111개 테스트와 전체 `check.sh`가 통과했다. 전체 Python 테스트 2,679개, 커버리지 90%, mypy 173개 소스 통과. compile·shell·ruff·audit도 통과했으며 audit의 기존 예외 5개는 그대로 적용됐다.
- 이번 실측에서 기록한 동일한 Luna 계획을 수정 전후 검증기에 재입력했다. 수정 전은 제목 조건 오류로 거부, 수정 후는 DB 조건과 본문 `keyword contains`를 그대로 보존해 통과했다. 모델의 응답 차이 없이 원인과 수정 효과를 확인한 검증이다.

## 실제 모델 비교 조건

실행 폴더는 `assistant_web/runtime/notion-text-scope-20261030/`이다. `baseline-app`에 작업 시작 시 backend/config를 복사했으며, 제품 파일 해시 차이가 `backend/notion/query_text.py` 하나인지 확인한다.

기존 11문항·13턴과 정답을 그대로 사용한다. 문항 SHA-256은 `543ad5b61b12f1fd16032e7bc41c1d2178072f768cc6c63c384e996cf4bd466f`다. 두 실행은 같은 Luna 설정과 기존 원천 복사본에서 시작하며, 모든 원천 요청은 읽기 전용으로 제한한다. 실제 원천 API와 독립 모델 생성은 변동할 수 있어 요청·응답 기록과 원문 비교를 별도로 남긴다.

검증 로그는 `red.log`, `green.log`, `full-check.log`, 동일 모델 계획 대조는 `recorded-plan-{before,after}.json`이다. 실측은 각 `before/`, `after/` 아래의 `manifest.json`, `responses.jsonl`, `http.jsonl`, `report.json`에 보존한다. 비밀값·원문 산출물은 Git에 추가하지 않는다.

## 전후 비교 결과

| 항목 | 수정 전 | 수정 후 |
| --- | --- | --- |
| 고정 문항 통과 | 9/11 | 10/11 |
| 본문 검색 `P1A-N-BODY` | 제목 조건 확인 질문으로 종료 | 기대한 문서 조회 성공 |
| 기존 Mantis 담당자 후속 질문 | 실패 | 동일 실패 |
| 모델 요청 HTTP 200 | 22회 | 20회 |
| 모델 입력 토큰 | 111,394 | 98,500 |
| 모델 출력 토큰 | 1,754 | 1,431 |

새 실패는 0개다. 본문 검색을 제외한 12턴은 검색 조건·출처 순서·조회 단계 요약이 동일했다. 답변 문구는 독립 모델 생성이므로 달라질 수 있다. 비교 자료는 `runtime/notion-text-scope-20261030/comparison.json`에 있다.

변경 전후의 문항·모델 설정·모든 설정 파일·실행기·실행일·원천 복사본 해시가 일치했다. 두 실행의 backend 차이는 공통 파서 한 파일이며 원래 원천 복사본은 변경되지 않았다. 모델 요청은 모두 Luna HTTP 200이고 Gemini 전환은 없었다. 토큰 수는 사전 짧은 연결 확인을 제외한 본 측정 합계다.

실시간 원천 요청은 완전히 같지 않았다. 공통 요청 형태 348개 중 응답 집합이 달라진 형태가 9개였고, 변경 전 Notion HTTP 429가 7회, 변경 후 HTTP 500이 1회 관측됐다. 대상 Notion 3개 페이지의 원시 페이지 응답은 같았으나, 이 비교를 모든 원천 입력의 결정적 동일성으로 해석하지 않는다. 같은 실제 모델 계획을 두 검증기에 넣은 별도 재생으로 본문 구분 수정 효과를 확인했다.

본문 검색은 3.5초 만에 확인 질문으로 끝나던 동작에서 실제 원문을 조회하는 동작으로 바뀌었다. 수정 후 실측은 약 92.8초였고 기대 문서를 찾았지만 `search_complete=false`로 부분 조회 안내를 표시했다. 기존 조회 한도와 속도는 이번 수정 범위에 포함하지 않았다. 검색 성공을 전체 DB의 완전 조회로 표현하지 않는다.

이 11문항은 제한된 회귀 표본이다. 전체 제품 정확도를 뜻하지 않으며, 남은 Mantis 담당자 후속 질문과 본문 조회의 속도·완전성은 별도 개선 과제다.

재실행 명령은 저장소 루트 기준이다. 실행 폴더 이름은 새 이름을 사용한다.

```bash
assistant_web/.venv/bin/python assistant_web/runtime/notion-text-scope-20261030/run_comparison.py before --before-app baseline-app --out before-new --model-id gpt-6-luna
assistant_web/.venv/bin/python assistant_web/runtime/notion-text-scope-20261030/run_comparison.py after-live --out after-new --baseline before-new --model-id gpt-6-luna
assistant_web/.venv/bin/python assistant_web/scripts/accuracy/benchmark.py score assistant_web/runtime/accuracy_benchmark/cases.jsonl assistant_web/runtime/notion-text-scope-20261030/before-new
assistant_web/.venv/bin/python assistant_web/scripts/accuracy/benchmark.py score assistant_web/runtime/accuracy_benchmark/cases.jsonl assistant_web/runtime/notion-text-scope-20261030/after-new
```
