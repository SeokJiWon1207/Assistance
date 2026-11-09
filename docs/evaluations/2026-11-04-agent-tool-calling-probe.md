# 에이전트 모델 tool calling 최소 호환성 검증

2026-11-04. 설정된 회사 모델 `company-qwen3.6-35b-a3b`(`qwen3.6-35b-a3b`, provider `openai-compatible`).
`assistant_web/scripts/agent_tool_probe.py`로 실행했다.

## 범위

- 합성 질문, 가상의 `lookup_fruit_price` 도구, 가짜 도구 결과만 보냈다. 사내 원문, Mantis·Notion
  조회 결과, 대화 이력은 보내지 않았다.
- 검증 기록은 커밋하지 않는 `runtime/agent_tool_verification.json`에 저장했다. API 키와 endpoint
  주소는 기록·출력하지 않았고 지문만 남겼다.
- 실제 Mantis·Notion 데이터에서의 도구 선택·답변 품질·지연은 검증하지 않았다(미검증).

## 결과

| 확인 항목 | 결과 | 관측 |
|---|---|---|
| tools 요청 수락 | 통과 | HTTP 200 |
| tool_calls 응답 형식 | 통과 | `finish_reason=tool_calls`, content 없이 tool_calls 1건 |
| arguments 형식 | 통과 | JSON 문자열, `{"item":"사과"}` |
| role=tool 왕복 | 통과 | 가짜 결과(1234원)를 반영한 최종 본문, `finish_reason=stop` |
| 한 응답의 복수 호출 | 통과 | tool_calls 2건 |
| `tool_choice=required` | 통과 | tool_calls 1건 |
| `tool_choice=none` | **미지원으로 판정** | 구조화 호출 없이 본문에 `<tool_call>` 형식 텍스트를 출력 |
| tools 없는 최종 답변 | 통과 | 순수 본문, `finish_reason=stop` |
| 추론 필드 노출 | 없음 | `reasoning_content` 없음(`think=false`) |
| 토큰 사용량 | 제공됨 | 모든 응답에 `usage` |

서빙 설정(확인 가능한 범위)은 다음과 같다.

- `system_fingerprint`: `vllm-0.24.0-056df9ca`. 서버 헤더는 `uvicorn`이다.
- `/v1/models`는 HTTP 404라 최대 길이·서빙 경로·도구 파서 설정은 확인하지 못했다.
- 구조화 tool_calls가 반환되므로 서버에 자동 도구 선택과 도구 호출 파서가 켜져 있다고 추정한다.
  다만 파서 이름은 미확인이다.

## 합성 데이터 에이전트 루프 확인

실제 오케스트레이터·도구 정의·프롬프트로 회사 모델과 다회 왕복을 확인했다.

- Mantis·Notion·Chroma 조회 함수는 가상 이슈(`#90001 가상지표 XYZ 미표시`)와 가상 문서로 바꿨다.
- 모델 endpoint 외의 네트워크는 차단했다.
- 사내 데이터는 조회하거나 전송하지 않았다.

| 시나리오 | 도구 호출 순서 | 마감 | 모델 호출 | 토큰(입력/출력) | 에이전트 검사 |
|---|---|---|---|---|---|
| Mantis 계획 설명 | `search_mantis_issues` → `get_mantis_issue_detail` → 종료 | 원문 인용 검증 경로(완료) | 6(판단 3 + 인용 검증 3) | 9,778 / 539 | 모두 통과 |
| Notion 계획 설명 | `search_notion_pages` → `get_notion_page_content` → 종료 | 인용 원장 대조(자유 서술) | 3 | 4,187 / 275 | 모두 통과 |

- 모델은 검색어를 질문 원문보다 짧은 핵심어로 다시 썼다(Mantis 시나리오).
- 사용하지 않은 원천은 호출하지 않았다.
- 검색 뒤에는 상세 조회를 거쳐 종료했다.

가상 데이터 2문항의 실행 계약 확인일 뿐이며, 실제 질문에서의 도구 선택률·답변 정확도·지연을 나타내지 않는다.
Notion 시나리오 답변의 '근거 기반 분석·추정' 항목에는 원문에 없는 예시가 추정으로 표시되어 있었다. 실제
품질 평가에서 추정 항목의 허용 범위를 함께 봐야 한다.

## 적용

- `config/models.json`에 `supportsTools=true`, `supportsParallelToolCalls=true`를 선언했다.
  실행은 순차로 하며 턴당 최대 3건이다.
- 최종 답변과 인용 수정·합성 호출은 `tool_choice=none`을 쓰지 않고 tools 없이 요청한다.
  본문에 출력된 텍스트형 호출은 `unparsed_tool_call`로 분류해 답변으로 쓰지 않는다.
- 구성 지문이나 `system_fingerprint`가 바뀌면 재검증 전까지 에이전트 후보에서 제외한다.
- 기능 플래그 `AGENT_MODE_ENABLED`는 OFF를 유지한다. 운영 활성화 전에 실제 질문 기준의 도구 선택·
  답변 품질·지연 평가(`benchmark.py run --agent on|off`)가 필요하다.

## 추가 검증: Luna 우선, Gemini 대체(2026-11-04)

사용자 결정으로 에이전트 경로의 선택 모델을 OpenAI Luna(`gpt-6-luna`)로 바꾸고, 장애 시 Gemini
(`gemini-3.1-flash-lite`)를 쓰도록 했다. 같은 합성 입력으로 두 모델을 확인했다.

| 모델 | 결과 | 관측 |
|---|---|---|
| `gpt-6-luna` | 지원 | 도구 호출·인자 JSON·role=tool 왕복·한 응답 2건·`tool_choice` auto/none/required·도구 없는 최종 답변 모두 통과. `system_fingerprint` 미제공 |
| `gemini-3.1-flash-lite` | **미검증** | HTTP 402 `RESOURCE_EXHAUSTED`(선불 크레딧 소진). 도구 호출 지원 여부를 확인하지 못함 |

- `config/models.json`에서 Luna에 `supportsTools=true`, `supportsParallelToolCalls=true`를 선언했다.
- Gemini는 검증 기록이 없어 도구 판단 호출에는 쓰지 않는다. 도구 없는 생성 호출의 대체로만 쓴다.
  크레딧을 복구한 뒤 `agent_tool_probe.py --model-id gemini-3.1-flash-lite`로 재검증해야 도구 판단 대체도 열린다.
- Gemini가 402를 반환하므로 현재는 기존 채팅 경로의 Luna → Gemini 대체도 실제로 동작하지 않는다.
- 합성 데이터 에이전트 루프를 Luna로 실행한 결과는 다음과 같다. 회사 모델과 같은 도구 순서로 끝났고 에이전트 검사는 모두 통과했다.
  - Mantis 시나리오: 모델 호출 6회, 토큰 9,226/421
  - Notion 시나리오: 모델 호출 3회, 토큰 3,352/161
- 실제 사내 데이터에서의 품질·지연은 미검증이다. 이 구성으로 실제 평가를 하면 사내 원문이
  외부 서비스(OpenAI)로 전송된다. AGENTS.md의 사전 승인 범위(회사 모델)와 다르므로 별도 승인이 필요하다.

## Gemini 재검증과 모델 전환 확인(2026-11-04)

크레딧 복구 뒤 같은 합성 입력으로 Gemini를 다시 확인했다.

- 도구 호출·인자 JSON·role=tool 왕복·한 응답 2건·`tool_choice=required`·도구 없는 최종 답변: 통과.
- `tool_choice=none`: 빈 응답(`MALFORMED_FUNCTION_CALL`). 에이전트는 none을 쓰지 않는다.
- `config/models.json`에서 Gemini에 `supportsTools=true`, `supportsParallelToolCalls=true`를 선언했다.

조사 도중 Luna 장애를 흉내 내 Gemini가 이어받는 경우를 합성 데이터로 확인했다.

- 처음에는 Gemini가 HTTP 400을 반환했다. Luna가 만든 이전 도구 호출에 `thought_signature`가 없어서였다.
- 대체 서명 값(`skip_thought_signature_validator`)을 붙이도록 고친 뒤에는 다음 순서로 끝났다.
  Luna 판단 1회 → Luna 장애(재시도 포함 2회) → Gemini가 상세 조회와 종료 판단을 이어서 수행 →
  Gemini로 원문 인용 검증 완료. 모델 호출은 7회였고 에이전트 검사는 모두 통과했다.
- 앞선 판단 단계의 형식 오류 때문에 생성 호출 대체까지 막히던 문제도 고쳤다. 실패를 호출 종류별로 기록한다.
