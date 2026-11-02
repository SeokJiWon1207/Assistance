# P5-C 채팅 단계와 실행 컨텍스트 분리 결과

## 범위와 기준선

P5-C의 준비·계획·검색·생성 단계와 공유 인증·리포트 규칙을 분리했다. 선행 미커밋 변경을 포함한 598개 파일의 내용·해시와 변경 전 결과를 `assistant_web/runtime/architecture-p5c/`에 보관했다. 변경 전 전체 테스트는 2,763개, 커버리지는 90%였다. `.env`와 운영 DB는 복사하지 않았다.

AGENTS.md에 따라 메인 에이전트가 구현·검토했다. 기존 작업 공간의 선행 변경을 보존했으며 P6는 범위에 포함하지 않았다. 커밋·서버 재시작·배포는 수행하지 않았다.

## 책임과 보존한 계약

| 소유자 | 책임 |
|---|---|
| `services/chat/prepare.py` | 모델 설정, 대화와 감사 기록, 후속 질문에서 `ChatPipelineContext` 준비 |
| `services/chat/planning.py` | 통합·기존 플래너 선택, 상태 해석, 결정형 조기 반환과 최종 질의 계획 |
| `services/chat/retrieve.py` | 검색 여부·개수, 근거 조회, 결정형 목록·빈 근거 처리, 모델 문맥 준비 |
| `services/chat/generate.py` | 후보 모델 생성, SSE/JSON 읽기, 첫 본문 대기와 모델·근거 폴백 |
| `services/chat/scope.py` | 로그인 사용자 확인과 Mantis·Notion 개인 범위 규칙 |
| `services/chat/reports.py` | 인증 범위 안에서 기존 순서로 결정형 리포트 선택 |
| `services/chat_service.py` | admission, 요청 예산·시간 측정, 단계 조합, 응답 메타데이터와 단일 최종 저장 |
| `services/contracts.py` | 기존 문맥·단계 결과·리포트 결과 타입 유지 |

`unified_planning.execute_plan`은 리포트·Notion 쓰기를 실제 소유 모듈에서 가져온다. 공통 결정형 리포트 선택만 `chat/reports.py`를 사용한다. 공유 인증 규칙을 `prepare.py`와 별개로 두어 준비→통합 계획→리포트→준비의 순환이 생기지 않게 했다.

- 기존 함수와 상수는 `chat_service`에서 정적으로 다시 내보낸다. 동일 함수 객체와 인수·반환·예외 계약을 유지한다. `ChatPipelineContext`, `StageResult`, `DeterministicReportResult`를 복제하지 않았다.
- `ChatService` 클래스와 이동한 함수 24개 정의의 AST를 변경 전과 비교했다. 준비·생성 단계의 API 키 조회를 `llm.get_model_api_key`로 명시한 두 지점 외에 본문·데코레이터·시그니처가 같다.
- 요청 예산, admission lease, `request_timing`, 최종화 경계는 기존 오케스트레이터에 한 번만 남아 있다. API 라우터와 contracts는 수정할 필요가 없었다.
- 모델 선택·프롬프트·타임아웃·검색 조건·정렬·SSE 메시지·저장 동작은 변경하지 않았다. Luna→Gemini 폴백 정책도 유지한다.
- 테스트 의존성 patch 168곳을 새 소비 모듈로 이전했다. 준비·생성이 공유하는 API 키는 실제 `backend.llm` 제공자를 patch한다. 오케스트레이터의 단계·저장 경계 patch는 해당 경계에 유지했다. `sys.modules` 조회나 호환 patch 탐색기를 추가하지 않았다.
- 구조 테스트는 같은 파일에 함수가 정의되어야 한다는 검사 대신 단계의 소유자·의존 방향·공유 타입·단일 실행 경계를 검사한다.

## 변경 전후 비교

전체 테스트와 비교 실행은 `.env`·외부 네트워크를 차단하고 임시 DB·고정 저장본을 사용했다.

| 비교 | 결과 |
|---|---|
| 기존 고정 문항 317개 | 조건·계획·출처·답변·검증 결과 317/317 동일 |
| 고정 문항 안의 모델 제안 61개 | 저장된 제안을 같은 검증기에 입력한 결과 동일 |
| 결정형 답변 269개 | 내용·출처·검증 결과 동일 |
| 실행 경계 고정 사례 20개 | 정상/미리 admission한 요청, 신원 확인, 조기 반환·조건 확인·빈 근거, 생성·오류·취소 결과 동일 |
| 실제 단계 실행 14개 × 전후 각 5회 | 응답 전체, 단계별 호출 수, 검색·모델 호출 인수, SSE 상태 순서, 최종 저장 인수 동일 |

20개 경계 사례는 `tests/fixtures/chat_pipeline_golden.json`으로 고정하고 상시 테스트에 연결했다. 예산·측정 객체·문맥을 요청 내에서 공유하고 종료 시 복원하는지, 성공은 정확히 한 번 저장하고 취소·모델 오류는 저장하지 않는지 검사한다.

14개 실제 단계 사례는 일반 대화, 스트리밍, 대체 모델, 모델 오류, 조건 확인, 건수·주간 리포트, Notion 쓰기 분기, 비활성 도구, 빈 근거, Notion 목록·근거 생성, 기존 플래너 대화를 실행했다. 모델·검색·리포트의 외부 의존성은 합성 응답으로 고정했고 실제 외부 쓰기는 하지 않았다. 각 사례는 독립 admission controller를 사용해 반복 실행 간 사용량 제한 누적을 막았다.

로컬 시간 비교는 사례별 5회 중앙값의 중앙값이 **0.168→0.161ms**, 가장 느린 사례의 중앙값이 **16.955→17.109ms**였다. 이 정도 차이를 성능 개선·악화로 해석하지 않는다. 실제 모델/API 요청은 **0회**이며, 실서비스 응답 시간이나 모델의 새 답변 정확도를 재측정한 결과는 아니다. 317/317은 정답률이 아니라 변경 전후 결과 일치율이다.

원본 결과·로그·측정치는 `runtime/architecture-p5c/`의 `before/after-questions.json`, `before/after-pipeline.json`, `before/after-chat.json`, `before/after-chat-timing.json`에 있다. 함수 비교는 `extraction-review.json`, patch 이전 내역은 `patch-migration.json`에 기록했다.

## 구조 결과

- 런타임 SCC: **2→1**. `chat_service↔unified_planning` 순환 해소.
- import 시점 SCC: **0 유지**.
- 남은 SCC는 `reports.team_brief↔workspace`이며 P6의 DB 소유권 분리 범위다.
- 해소된 순환 간선 예외 3건, 계층 역참조 1건, 파사드 참조 21건을 제거했다. 새 위반·새 예외·동적 patch 장치를 추가하지 않았다.
- `chat_service.py`는 1,331줄에서 320줄로 줄었다. 이전 공개 심볼을 위한 정적 재노출과 응답 메타데이터 연결은 남겨 두었다.

## 전체 검증

최종 `assistant_web/scripts/check.sh`가 종료 코드 0으로 통과했다.

- 테스트 **2,768개 통과**, 커버리지 **90%**. 변경 전 2,763개에 새 구조·문맥 계약 테스트 5개를 추가했다.
- compile, 프런트 TypeScript·Bklit 빌드 확인, shell, ruff 통과.
- mypy **242개 소스 파일** 통과.
- 의존성 audit 통과: Python은 기존 예외 **5건**을 유지한 상태에서 미해결 신규 항목 없음, npm **0건**. 예외 정책·기한은 수정하지 않았다.
- `test_chat_execution`, `test_stream_fallback`, `test_unified_planner`, `test_unified_model_fallback`, `test_evaluation_finalization`을 포함해 첫 토큰 대기, 중간 취소, 오류 후 대체 모델, 검증 전 본문 버퍼링과 최종 SSE 순서를 확인했다.

최초 전체 실행에서 기존 구조 예외 3종의 stale 항목만 실패해 해소된 예외를 제거했다. 다음 실행의 ruff는 새 테스트 도우미의 반복문 내부 closure를 지적했다. 사례별 함수를 분리해 이를 고친 뒤 전체 검사를 다시 통과했다. 실제 서비스의 동작을 바꿔 실패를 우회하지 않았다. 최종 로그는 `runtime/architecture-p5c/final-check.log`다.

## 다음 범위

P6에서는 DB별 소유자와 SQLite 연결·트랜잭션 정책을 정리하고, 남은 `team_brief↔workspace` 순환을 해소한다. 이번 변경에서는 해당 작업을 시작하지 않았다.
