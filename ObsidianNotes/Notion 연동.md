---
tags: [integration, notion]
---

# Notion 연동

← [[00 Home]] · 관련: [[API 엔드포인트]] · [[설정 및 환경변수]]

백엔드가 Notion 공식 API를 직접 호출한다. 토큰과 DB ID는 프론트엔드에 노출하지 않고 `.env`에만 저장.

## 설정

```bash
NOTION_API_TOKEN=secret_xxx
NOTION_DATABASE_IDS=이슈리스트_테스트_DB_ID,주요이슈현황_테스트_DB_ID
NOTION_API_VERSION=2022-06-28
NOTION_FETCH_PAGE_BLOCKS=true
NOTION_TOOL_TIMEOUT_SECONDS=12
NOTION_TOOL_MAX_RETRIES=1
```

## 설정 순서

1. Notion 개발자 포털에서 Internal connection 생성 → access token 발급
2. `AI_Notion / 이슈리스트_테스트`, `AI_Notion / 주요이슈현황_테스트` 두 데이터베이스에서 해당 connection 추가
3. 두 데이터베이스 URL의 32자리 ID를 쉼표로 구분해 `NOTION_DATABASE_IDS`에 입력
4. 서버 재시작 후 `/api/notion/status` 또는 `/api/notion/search`로 연결 상태 확인 → [[API 엔드포인트]]

## 예시 질문

- 노션 주요이슈 보여줘
- 노션 결재요청 상태인 항목 보여줘
- 노션에서 이번 주 기한 있는 항목 정리해줘
- 노션에서 '[MTS] 저장한 차트 적용 및 확인요청' 항목을 찾아서 알려줘

## 정확 제목 검색

따옴표로 감싼 값은 Notion 문서 제목 리터럴로 처리한다.

- 제목 안의 고객사, 플랫폼, 상태 단어, 사람 이름, 화면번호는 별도 필터로 만들지 않음
- 따옴표 바깥에 명시한 조건만 구조화 필터로 추가
- 내부 작은따옴표가 있는 제목은 바깥쪽 첫·마지막 따옴표로 전체 제목 추출
- 서버 측 제목 필터 결과를 사용하고 정확 제목 요청에서는 전체 데이터베이스 스캔을 하지 않음

## 타임아웃과 부분 성공

- 요청당 기본 제한: 12초
- 일시적인 연결·읽기 오류: 기본 1회 짧은 지수 백오프 재시도
- 한 DB가 실패해도 나머지 DB 검색은 계속
- 일부 DB에서 결과를 얻었으면 `partial_error`, 결과 없이 연동 오류만 있으면 `error`
- `NOTION_FETCH_PAGE_BLOCKS=false`이면 제목·속성 검사에 불필요한 본문 호출을 생략

## 관련 코드

- `backend/config.py` — 토큰, 헤더, 타임아웃·재시도
- `backend/sources/notion.py` — 제목 파싱, 필터, 검색, 부분 성공 처리
- `backend/retrieval.py` — 따옴표 제목과 바깥 조건 분리
