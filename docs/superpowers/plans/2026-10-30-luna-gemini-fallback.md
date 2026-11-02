# Luna → Gemini 자동 전환 — 2026-10-30

기본 모델은 OpenAI `gpt-6-luna`, 지정 대체 모델은 `gemini-3.1-flash-lite`다. 이 작업은 P1-A 구조 변경 이후 사용자가 요청한 모델 장애 처리 기능이다. P1-A의 보관 코드와 실측 결과는 수정하지 않는다.

## 변경 범위

- `config/models.json`과 안전한 기본 레지스트리의 Luna 항목에 `fallbackModelIds`를 명시한다.
- `backend/llm.py`의 공통 후보 선택 함수가 지정 순서·활성 상태를 확인하고 자기 자신·중복·없는 ID를 제외한다. 명시 설정이 없는 모델은 기존 호출 단계별 후보 정책을 사용한다.
- 답변 생성은 레지스트리 표시 순서와 무관하게 Luna 다음 Gemini를 사용한다. 두 모델이 실패하면 지정하지 않은 회사·로컬 모델을 추가 호출하지 않는다.
- 질문 해석도 같은 후보를 사용한다. Luna 연결 장애·회로 차단·빈 JSON 응답·HTTP 오류에서 Gemini로 전환하고, API 키가 없는 Luna 요청도 설정된 Gemini를 사용할 수 있다.
- 검색 조건 검증·기존 제한 시간·권한·요청 취소 처리는 유지한다. 잘못된 검색 조건은 검증과 보정 절차를 거치며, 모델 전환을 이유로 무시하지 않는다.

## 검증

새 회귀 테스트는 `tests/test_luna_gemini_fallback.py`, 라우팅·제외 조건 사례는 `tests/fixtures/routing_cases.jsonl`의 `luna-gemini-exclusion`이다. 제목 equals와 상태 제외 조건이 Gemini 재시도 뒤에도 보존되는지 확인한다. 기존 라우팅 fixture 내용은 유지하고 사례 한 줄만 추가했다.

변경 전에는 Gemini가 후보에서 제외되거나 레지스트리의 다른 모델이 먼저 호출되어 새 테스트가 실패했다. 변경 후에는 순서 변경·시간 초과·API 400/401/402/403/429/5xx·빈 응답·없는 키·비활성 Gemini·취소·중복 설정을 검증한다. 집중 검사와 전체 검사 결과는 `runtime/luna-gemini-fallback-20261030/`에 보관한다.

전체 표준 검사 `assistant_web/scripts/check.sh`는 종료 코드 0으로 통과했다. Python 테스트 2,673개, 커버리지 90%이며 compile·shell·ruff·mypy·audit 단계도 통과했다. audit은 저장소에서 이미 승인한 기존 예외 5개를 적용했다. 표준 재시작 명령으로 로컬 서버를 다시 시작했고 `/health` HTTP 200을 확인했다. 앱과 동일하게 `.env`를 로딩한 레지스트리에서도 기본 Luna와 대체 Gemini 순서를 확인했다.

## 현재 Gemini 사용 가능 여부

2026-10-30 14:42:48 KST의 짧은 실제 요청에서 Gemini는 HTTP 402 / RESOURCE_EXHAUSTED를 반환했다. 따라서 자동 전환 설정과 모의 장애 응답을 이용한 성공 전환은 검증했지만, 현재 계정으로 실제 Gemini 대체 답변을 받으려면 선불 결제 상태가 복구되어야 한다. 결제·충전·키 변경은 수행하지 않았다.

실제 점검 근거: `runtime/luna-gemini-fallback-20261030/gemini-readiness.json`.
