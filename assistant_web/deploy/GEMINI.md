# Gemini 3.1 Flash-Lite 기본 모델 연결

채팅의 기본 모델은 `gemini-3.1-flash-lite`다. Google의 OpenAI 호환 API를 사용하며 별도 SDK는 필요하지 않다.

## 키 준비

Google AI Studio의 API Keys에서 서비스용 키를 만든다. 현재 실행 환경에 유효한 `GEMINI_API_KEY`가 있으면 재발급할 필요가 없다. 키는 앱 서버의 환경변수로만 주입하고 브라우저·로그·Git에는 넣지 않는다. 로컬 `.env` 예시는 `assistant_web/.env.example`을 따른다. 운영은 기존 배포의 비밀 환경변수 관리 절차를 따른다.

사내 원문으로 검증하기 전에 해당 키의 프로젝트와 유료 API 결제 상태, Google로 전송할 자료 범위를 확인한다. 연결 시험에는 공개 테스트 문장만 사용한다.

## 호출 설정

- 모델: `gemini-3.1-flash-lite`
- endpoint: `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`
- 추론: `minimal`
- 호출별 최대 출력: 2,048토큰. 질문 해석처럼 요청이 더 작은 제한을 지정하면 그 제한을 유지한다.
- 회사 모델 전용 생성 옵션은 Gemini 호출에서 제외한다.
- 키가 없는 환경에서는 `missing_key`로 표시한다. 키가 있으면 Gemini를 첫 번째 활성 모델로 선택한다.

서버를 시작한 뒤 화면을 새로고침하면 Gemini 3.1 Flash-Lite가 기본 선택된다. 모델 선택 값은 브라우저에 저장하지 않으므로 기존 Qwen 선택을 이관하는 작업은 필요하지 않다. 일반 대화, JSON 질문 해석, 스트리밍을 먼저 확인하고 월간 고객사 조회·공수·후속 조건 변경의 결과를 기존 회귀 사례와 비교한다. 모델 이름과 실제 해석 모델은 감사 기록에서 함께 확인한다. 질문 해석에 쓰이는 최대 출력 제한은 절대 줄이지 않고 실제 응답을 보고 조정한다.

목록·건수·공수 계산은 기존 서버 코드가 수행한다. 이 설정의 출력 제한은 호출별 제한이며 월 요금 상한은 아니다. 전체 대화와 재시도에 따라 비용이 늘어날 수 있다. 사용량은 Google AI Studio의 Dashboard > Usage에서 확인한다.

## 공식 문서

- https://ai.google.dev/gemini-api/docs/api-key
- https://ai.google.dev/gemini-api/docs/openai
- https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite
- https://ai.google.dev/gemini-api/docs/billing
