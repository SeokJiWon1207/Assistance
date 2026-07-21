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

## 고객사별 최신 이슈

`고객사별 최근 이슈를 하나씩 보여줘` 같은 질문은 단일 고객사 검색이 아니라 `group_by=customer`로 처리한다.

1. `하나씩`의 `하나`를 하나금융 별칭으로 해석하지 않음
2. `고객사별`, `하나씩` 같은 제어 표현을 Mantis 검색어에서 제외
3. 전체 표시 한도를 1건으로 줄이지 않고 최근 이슈를 충분히 조회
4. 이슈 ID 내림차순으로 정렬한 뒤 고객사마다 첫 이슈 1건만 선택
5. `_Project` 접미사만 다른 프로젝트명은 같은 고객사로 통합
6. 답변 본문과 화면의 출처 표에 동일한 고객사별 행을 사용

관련 회귀 테스트는 `assistant_web/tests/test_query_planning.py`의 `test_customer_grouping_*` 케이스에서 검증한다.

## 상태 표준화

질문의 한글·영문 상태를 내부 키로 표준화한 뒤 Mantis 원본 상태와 비교한다.

- `developed`, `개발완료`, `개발 완료` → `dev_done`
- `confirmed`, `확인된` → `confirmed`
- `closed`, `종료된` → `closed`
- `open`, `미종료` → 여러 미종료 상태를 포함하는 `open`

신규 Mantis 상태가 추가되면 `backend/config.py`의 `STATUS_ALIASES`, `STATUS_MATCH_ALIASES`를 함께 수정하고 회귀 테스트를 추가한다.

## 스냅샷과 라이브 재검증

로컬 인덱스는 전체 과거 데이터를 빠르게 찾는 용도지만 담당자와 상태는 구축 이후 바뀔 수 있다.

1. 전체 스냅샷에서 구조화 조건에 맞는 상위 후보를 찾음
2. 담당자·상태 조건이 있으면 표시 후보만 이슈 ID로 라이브 재조회
3. 현재 값이 조건과 다르면 오래된 후보를 제거
4. 라이브 조회가 불가능한 후보만 스냅샷 폴백으로 유지
5. 최신 페이지 스캔 결과와 병합하고 이슈 ID 기준으로 정렬

감사 메타데이터:

- `live_revalidated_count`: 라이브 확인 완료 후보 수
- `stale_removed_count`: 현재 조건과 달라 제거한 후보 수
- `live_fallback_count`: 라이브 확인 실패로 스냅샷을 유지한 후보 수

## 관련 코드

- `backend/tools/mantis_snapshot.py` — 스냅샷 조회 헬퍼
- `backend/sources/mantis.py` — 스냅샷 검색, 라이브 재검증, 페이지 스캔
- `backend/config.py` — 상태 별칭
- `backend/understanding.py` — 고객사·담당자·상태 조건 해석
