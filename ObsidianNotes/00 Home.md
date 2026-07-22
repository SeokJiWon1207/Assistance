---
tags: [MOC]
---

# 컨텐츠서비스 AI 비서 — 프로젝트 노트

회사 LLM API를 직접 호출하는 컨텐츠서비스 대화형 웹앱. Mantis(이슈트래커)와 Notion을 근거 기반으로 검색해 답변하고, 매일 자동으로 정확도를 검증한다.

## 지도

- [[프로젝트 개요]]
- [[아키텍처 - 검색 파이프라인]]
- [[백엔드 구조]]
- [[API 엔드포인트]]
- [[Notion 연동]]
- [[Mantis 연동]]
- [[정확도 자동 검증]]
- [[배포 - AWS EC2]]
- [[설정 및 환경변수]]

## 한눈에 보기

| 항목 | 내용 |
|---|---|
| 실행 | `python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8787 --reload` |
| 접속 | http://127.0.0.1:8787 |
| 핵심 파일 | `assistant_web/backend/main.py`, `backend/api/`, `backend/services/chat_service.py` |
| 데이터 소스 | Mantis API, Notion API, 로컬 ChromaDB 스냅샷(`data/mantis_chroma_db`) |
| 배포 | AWS EC2 시연 서버 (systemd `content-ai-assistant`) |
