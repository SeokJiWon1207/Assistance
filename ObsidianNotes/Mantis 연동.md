---
tags: [integration, mantis]
---

# Mantis 연동

← [[00 Home]] · 관련: [[아키텍처 - 검색 파이프라인]]

Mantis는 회사 이슈 트래커. 4가지 조회 방식은 [[아키텍처 - 검색 파이프라인#Mantis 도구 선택 우선순위]] 참고.

## 로컬 벡터 인덱스 (ChromaDB)

`data/mantis_chroma_db/`에 저장되는 로컬 스냅샷. `scripts/make_vector_db.py`로 구축/갱신한다.

```bash
cd ..                                          # Assistance 루트에서 실행
python3 scripts/make_vector_db.py                      # 증분: 새 이슈만 추가
python3 scripts/make_vector_db.py --refresh-days 14     # + 최근 14일 내 수정 이슈 재임베딩
python3 scripts/make_vector_db.py --full                # 전체 재구축 (data/backups에 자동 백업)
```

- 인증: `assistant_web/.env`의 `MANTIS_API_URL` / `MANTIS_API_TOKEN` (하드코딩 없음)
- 재구축 문서에는 `매체구분`, `등록일` 헤더 포함 → 플랫폼/날짜 필터 정확도 향상
- 매일 새벽 증분 갱신 crontab 예시:

```cron
30 6 * * * cd /path/to/Assistance && /usr/bin/python3 scripts/make_vector_db.py --refresh-days 7 >> data/mantis_chroma_db/rebuild.log 2>&1
```

## 저장 필터 매핑

`assistant_web/config/mantis_filters.json`에서 질문 키워드 ↔ `filter_id` 관리. 신규 매핑은 `filter_id`를 실제 Mantis 필터 ID로 채우고 `enabled: true`로 설정.

## 고객사 별칭

`assistant_web/config/customer_aliases.json`에서 관리. 신규 고객사는 코드 수정 없이 이 파일에 항목만 추가하고 서버 재시작.

```json
{
  "키움": ["키움증권", "Kiwoom", "150Kiwoom"]
}
```

파일이 없거나 형식이 잘못되면 내장 기본값으로 동작.

## 관련 코드

- `backend/tools/mantis_snapshot.py` — 스냅샷 조회 헬퍼
- `backend/app.py` 내 `derive_customer_aliases_from_project`, `get_customer_alias_groups` 등
