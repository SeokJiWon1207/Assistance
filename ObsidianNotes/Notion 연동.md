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

## 관련 코드

`backend/app.py`: `get_notion_settings`, `get_notion_headers`, `notion_post_json`, `notion_is_configured`
