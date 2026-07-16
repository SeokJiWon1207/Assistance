# 팀 배포용 OpenWebUI 도구

이 폴더의 파일은 `assistant_web`, 로컬 ChromaDB, Ollama 및 사용자 PC의 다른 파일을
참조하지 않는 독립형 OpenWebUI Workspace Tool입니다.

- `mantis_tools.py`: 사용자별 인증 정보로 Mantis REST API 직접 조회
- `notion_tools.py`: 사용자별 인증 정보로 Notion API 직접 조회 및 선택적 쓰기

LLM 연결은 도구 코드의 책임이 아닙니다. 각 팀원이 OpenWebUI에 자신이 발급받은
LLM endpoint와 API key를 등록하고, 해당 모델에 이 도구를 연결하면 됩니다.

## 관리자 설정

1. OpenWebUI의 Workspace → Tools에서 각 Python 파일을 별도 도구로 등록합니다.
2. 도구를 사용할 팀원 또는 그룹에 읽기 권한을 부여합니다.
3. 각 모델 설정에서 도구를 연결하고 Function Calling을 Native로 설정합니다.
4. 공용 데이터 인증을 쓸 경우 관리자 Valves에 기본 URL·토큰·ID를 설정합니다.

모델이 Native tool calling을 지원하지 않으면 도구를 안정적으로 자동 호출할 수 없습니다.

## 팀원별 설정(UserValves)

각 팀원은 채팅의 도구 설정에서 자신의 UserValves를 입력합니다.

### Mantis

- `MANTIS_API_URL`: `https://host/mantis/api/rest/issues`
- `MANTIS_API_TOKEN`: 개인 Mantis REST API 토큰

### Notion

- `NOTION_API_TOKEN`: 개인 Notion integration token
- `NOTION_SOURCE_IDS`: 접근할 ID를 쉼표로 구분

Notion 최신 API를 사용할 때는 data source ID와 `NOTION_API_VERSION=2026-03-11`을
사용합니다. 기존 database ID 방식이면 관리자 Valve에서 버전을 `2022-06-28`로
변경합니다.

토큰 입력란은 비밀번호 형태로 표시되지만 OpenWebUI의 기본 저장값은 평문일 수 있습니다.
운영 관리자는 `ENABLE_VALVE_ENCRYPTION=true`와 고정된 `WEBUI_SECRET_KEY`를 설정해
Valve 암호화를 활성화하는 것이 좋습니다.

## 검색 범위 차이

독립형 배포에서는 사용자 PC의 `mantis_chroma_db`를 사용하지 않습니다. 따라서 Mantis
도구는 원본 REST API에서 관리자 `MAX_PAGES` 범위만 검색하며, 기존 로컬 임베딩 기반
유사 이력 검색은 포함하지 않습니다. 모든 팀원이 공통 과거 RAG를 써야 한다면 별도의
공용 벡터 검색 서비스 또는 MCP/OpenAPI 서버가 필요합니다.

Notion 쓰기는 관리자 `ENABLE_WRITES=false`가 기본값입니다. 쓰기를 활성화하더라도
생성·수정 함수는 `confirmed=true`를 요구합니다.
