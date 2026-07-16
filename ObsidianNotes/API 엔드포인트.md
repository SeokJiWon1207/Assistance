---
tags: [backend, api]
---

# API 엔드포인트

← [[00 Home]] · 관련: [[백엔드 구조]]

`assistant_web/backend/app.py` 하단(6585~7183줄)에 정의된 라우트.

| 메서드 | 경로 | 설명 |
|---|---|---|
| GET | `/api/models` | 사용 가능한 LLM 모델 목록 (공개용, 키/엔드포인트 제외) |
| POST | `/api/chroma/search` | ChromaDB 유사도 검색 |
| GET | `/api/chroma/documents/{issue_id}` | ChromaDB 문서 단건 조회 |
| GET | `/api/notion/status` | Notion 연동 상태 확인 |
| POST | `/api/notion/search` | Notion 검색 |
| GET | `/api/notion/pages/{page_id}` | Notion 페이지 조회 |
| POST | `/api/notion/pages` | Notion 페이지 생성 |
| PATCH | `/api/notion/pages/{page_id}` | Notion 페이지 수정 |
| GET | `/api/validation/logs` | 감사 로그 목록 |
| GET | `/api/validation/summary` | 감사 로그 요약 |
| GET | `/api/validation/logs/{log_id}` | 감사 로그 단건 |
| GET | `/api/evaluation/cases` | 평가 케이스 목록 |
| POST | `/api/evaluation/run` | 평가 실행 |
| POST | `/api/chat` | 채팅 메인 엔드포인트 |
| GET | `/health` | 헬스체크 |
| GET | `/` | 프론트엔드 정적 페이지 |

## 참고

- Notion 토큰/DB ID는 프론트엔드에 노출하지 않고 백엔드 `.env`에서만 사용 → [[설정 및 환경변수]]
- `/api/chat`이 [[아키텍처 - 검색 파이프라인]] 전체(QueryPlan → 검증 → 도구 실행 → 답변 → 감사)를 트리거하는 핵심 엔드포인트
