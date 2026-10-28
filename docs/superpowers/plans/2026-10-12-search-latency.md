# 검색 응답 시간 개선 계획

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 검색 주제·범위·최신성·메모리 보호를 유지하면서 운영의 동일 질문 45초 응답을 개선하고 재배포 후 실측한다.
**Architecture:** 동일 이슈의 검색 본문을 한 번만 조립하여 조건 검사에 재사용한다. 날짜 표현은 동일한 UTC 날짜를 더 저렴하게 생성한다. 최신 전체 저장본에서 완전한 검색이 끝난 경우에만 중복 Chroma 조회를 생략한다.
**Tech Stack:** Python, SQLite 행 단위 스트림, FastAPI, Chrome, systemd.
**Spec:** 사용자의 현재 요청. 비교 기준 운영 릴리스 20261012T071326Z 및 audit 937990a3d9e343f0828c3cca2827b5d6.

## Global Constraints
- 운영과 일치하는 기존 격리 작업 트리에서 이번 변경만 적용한다.
- 키워드 AND·별칭, 구조화 조건, UTC 날짜, 전체 매칭 건수, stale/error fallback을 보존한다.
- 전체 본문 캐시나 답변 재사용 캐시를 도입하지 않는다. 동시 2개 및 메모리 보호 유지.
- 사용자가 수정·배포·회사 모델 검증을 명시적으로 승인했다. 중간 승인 재요청 없이 진행한다.
- 기존 미커밋 수정은 유지하고 배포 전후 파일 해시로 이번 변경을 구분한다.

## Review Focus
- 플랫폼 필드가 없는 과거 이슈도 본문 판정 유지.
- 날짜 epoch 초/밀리초/한글 표기와 notes-only 키워드 보존.
- snapshot_complete, search_complete 둘 다 확인된 정상 저장본만 중복 검색 생략.
- stale/partial/live/error 데이터와 의미 검색에는 기존 보완 경로 유지.
- 취소·동시 요청·세대 교체 및 메모리 한도 유지.

## Task 1: 본문 조립 반복 제거
**Files:** backend/common.py, backend/sources/mantis.py, tests/test_search_latency.py.
**Interface:** issue_matches_platform/issue_matches_constraints에 keyword-only search_text: Optional[str] = None를 추가하고 호출자가 계산한 문자열을 재사용한다.
- [x] 한 이슈 본문 재조립 비용을 감지하며 실제 결과/notes/날짜를 확인하는 회귀 테스트 RED.
- [x] 한 번 조립한 본문을 조건 검사에 전달하고 UTC date.isoformat으로 변환 비용 축소.
- [x] 회귀 GREEN, 31,233건 실데이터 cProfile 전후 비교 및 bounded-memory 테스트.

## Task 2: 완전한 검색 뒤 중복 조회 제거 및 계측
**Files:** backend/retrieval.py, backend/sources/mantis.py, backend/sources/chroma.py, tests/test_search_latency.py.
- [x] 최신 전체 원본 0건/비어있지 않은 결과가 오래된 Chroma와 섞이지 않는 실제 SQLite 회귀 RED.
- [x] 불완전한 검색에는 Chroma 보완을 유지하며 최신 전체 검색에만 생략 적용.
- [x] mantis_page_scan/chroma_search 단계 시간을 기존 요청 타이밍에 추가.
- [x] 의미검색·정확검색·stale/partial 경로 테스트 및 전체 공식 테스트 GREEN.

## Task 3: 원문 헤더 SQL 반복 계산 제거
**Files:** backend/intent/entities.py, tests/test_search_latency.py.
- [x] 동일 Chroma 원문 명부 72/116/89개 및 빈값·마지막줄·공백·탭 사례의 동등성 검증.
- [x] CASE와 중첩 필터로 인한 반복 부분문자열 계산을 단순화. 캐시 키·원문·범위 변경 없음.

## Task 4: 통합·배포·운영 검증
- [x] 독립 리뷰, Ruff, mypy, 전체 테스트.
- [x] 운영 변경 전 해시 일치 확인 및 디스크 기반 백업/사전 검사 후 새 릴리스 활성화.
- [x] 로그인된 운영 UI에서 동일 질문 단일/동시2건 및 초과 요청 검증. 응답 시간·단계·RSS·가용 메모리·OOM·재시작 측정.

## Evidence / Ledger
- 기존 운영: context 6.4–6.7s, planner/status 2.1s, retrieval 35.9–36.0s, 총44.5–44.8s.
- 로컬 31,233건 cProfile: scan14.35s, 본문조립63,192회7.84s, 날짜strftime138,605회5.37s, SQLitestream3.48s. 최대RSS66.5MiB.
- 로컬 Chroma 동일키워드 추가검색0.86s, 0건.
- 준비 단계 독립 조사: cold2.28s 중 헤더 세 조회2.09s, warm20ms. 작은 warm 비용을 위한 추가 캐시는 하지 않는다.

- 구현 검증: 새 회귀 RED 3개(본문중복, 완전한 검색 뒤 오래된 인덱스 혼입 2개) → GREEN 6개. 전체 공식 테스트 1,082개, Ruff/mypy107 green.
- 동일 실데이터 profile 14.35→6.85s, 결과0건/스캔31,233건 유지, 본문조립63,192→31,233회, isolated peakRSS66.5→68.4MiB.
- 준비단계 SQL은 같은 Chroma 원문을 유지한 채 반복계산만 제거. 다른 인덱스로 대체하지 않아 세대 불일치 위험을 추가하지 않는다.

- 원문 헤더 실데이터 재검증: 프로젝트72/담당자116/요청자89개 정확 일치. SQL 각138/144/144ms(기존1381/361/366ms).
- SQLite에서 큰 history/attachments를 먼저 제거하는 경로는 유지했다. Python에서 나중에 제거하면 약1.7초 추가감축 가능했지만 불필요 필드의 순간 메모리 적재를 늘리므로 사용자 메모리 안정성 요구를 우선했다.
- 독립 최종 리뷰 승인, 발견 사항 없음. 원본 최신성 기준은 기존 수집 후1시간 미만 및 UI의 저장시각 표시를 유지한다.

- 운영 배포 완료: 20261012T075418Z, 이전 20261012T071326Z. 배포 대상 6개 SHA256 일치, /health 및 /ready 정상.
- 실제 Chrome 동일 질문 첫 요청: 27.019s(context14.341s, 계획2.005s, 원본검색10.574s, 검증0.086s), audit a3d60a9492c3435f94309867409d6e68.
- 반복 단일: 12.408s(context0.031s, 계획2.021s, 원본검색10.346s), audit 2c334fa1b2c94018902fd87f703fd0ef.
- 동시 2건: 25.407s / 25.508s, 원본검색23.391s / 23.484s. audit155eec54213d4102a1d0d82b0d4f1ff3 및469993c78dd84d2694706b1dfc40531c. 기존 동일 동시 조건44.646s / 44.936s 대비 약43% 단축. 초과3번째 요청429 및 UI 재시도 안내 확인.
- 모든 실제 응답은 VB스크립트/자바스크립트/교체 AND + HTS 유지, 저장 원본 기준 완전 검색0건, Chroma 재검색 없음, UI 검증3/3 통과. 답변 재사용 캐시 미사용.
- 0.25초 간격 운영 메모리 측정: 첫/반복 구간 RSS최대682.56MiB, 동시구간693.45MiB. 동시 서비스 cgroup최대1147.05MiB(회수 가능한 파일캐시 포함), OS가용메모리최소770.92MiB, OOM/oom_kill0, PID382052유지, NRestarts0.
- 남은 지연: 서비스 첫 요청의 초기 준비14.3초와 전체 원본 행 검색(단일10.3초, 동시23.4초). 처음 요청27초가 여전히 반복보다 느린 사실은 최종 보고에서 구분한다. 전체 원문 캐시로 메모리를 늘리거나 검색 조건을 완화하지 않았다.
- 증거: /tmp/assistance-search-latency-final-result.jpg, /tmp/assistance-search-latency-{single,concurrent}-075418.jsonl, /tmp/assistance-search-latency-final-audits.json.
