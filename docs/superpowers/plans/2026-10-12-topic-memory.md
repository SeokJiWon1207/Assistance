# 검색 주제 보존과 메모리 보호 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 키워드를 버린 오답을 막고 동시 요청의 메모리를 제한한 뒤 운영에서 검증한다.
**Architecture:** 모든 검색 경로에서 명시한 주제를 유지한다. 전체 본문 검색의 적재량을 줄이고, 채팅 진입 시 가용 메모리와 실행 중인 요청의 예약량으로 수용 여부를 결정한다.
**Tech Stack:** Python, FastAPI, SQLite, JavaScript, Linux procfs/cgroup v2, systemd.
**Spec:** 사용자의 현재 요청 및 운영 감사 로그 3e4aa57728a04e88a80a0df1ed9c9076.

## Global Constraints
- 기존 관련 없는 작업을 보존한다. 운영 코드와 일치하는 기존 격리 작업 폴더를 사용한다.
- 이슈 원문의 설정된 회사 모델 전송은 사용자가 승인했다.
- 새 인증 우회 없이 로그인된 운영 UI에서 검증한다.
- 커밋할 경우 Conventional Commit 접두사 외에는 한국어를 사용한다.

## Review Focus
- 키워드 0건일 때 구조화 조건만 남겨 결과를 채우지 않는다.
- 별칭과 라이브 갱신 후의 실제 본문도 주제에 맞아야 한다.
- 동시 진입은 원자적으로 예약하며 거절된 요청은 슬롯과 빈도 제한을 소비하지 않는다.
- 컨테이너 메모리 한도와 호스트 여유 중 작은 값을 따른다.
- 취소, 오류, 스냅샷 교체 중에도 SQLite 자원을 해제한다.

## Task 1: 검색 주제 보존
- [x] SQLite 및 실제 matching 회귀 테스트에서 키워드 제거 오답을 재현한다.
- [x] Chroma/구조화 검색/첫 키워드 fallback을 제거하고 별칭 및 갱신 후 주제 일치를 검증한다.
- [x] 0건 응답에 질문의 주제를 보존하고 테스트를 통과시킨다.

## Task 2: 메모리 적재 제한
- [x] 전체 본문 검색의 대량 적재를 재현한다.
- [x] SQLite 행 단위 검색을 적용하고 완전성 및 자원 해제를 테스트한다.
- [x] 실제 저장본으로 동시 검색 최대 메모리를 측정한다.

## Task 3: 요청 수용 보호
**Files:** backend/admission.py, backend/memory_pressure.py, tests/test_admission.py, tests/test_memory_pressure.py, .env.example, README.md.
**Interface:** available_memory_bytes() -> int | None; ChatAdmissionController.acquire(user_key) -> ChatAdmissionLease 또는 HTTP 429.
- [x] 남은 메모리 부족, 동시 예약, 예약 해제, cgroup 한도, 계측 불가 테스트를 먼저 실패시킨다.
- [x] 기본 동시 처리 2개, 시스템 여유 192MiB, 진행 요청당 128MiB 예약으로 신규 요청을 제한한다. 환경변수로 조정한다.
- [x] 메모리 부족은 Retry-After와 함께 재시도 안내를 반환한다.

## Task 4: 통합·배포·운영 검증
- [x] 전체 테스트/정적 검사와 독립 리뷰를 통과한다.
- [x] 이번 변경만 새 릴리스로 배포하고 readiness 실패 시 이전 버전으로 복구한다.
- [x] 같은 질문의 답변 및 2개 동시 실행+초과 요청의 결과, RSS/서비스 메모리/OOM/재시작을 0.25초 간격으로 검증한다.

## Decisions
- 사용자 요청에 수정과 배포가 명시되어 있어 중간 승인 질문 없이 진행한다.
- 가용 메모리를 읽지 못하는 플랫폼에서도 고정 동시성 한도를 유지한다.
- 웹 UI의 기존 429 재시도 안내를 재사용한다.

## 운영 검증 결과
- 최종 릴리스: `20261012T071326Z` (2026-10-12 16시대 KST). 수정 파일 33개 SHA-256 일치 확인.
- 공식 테스트 1,076개, Ruff, mypy 107개 소스, 배포 Bash 구문 검사 및 독립 리뷰 통과.
- 처음 질문의 `VB스크립트 / 자바스크립트 / 교체`와 HTS 조건을 유지하며 조회 범위 내 0건으로 응답. 무관한 최신 HTS 5건 반환이 사라짐.
- 단일 실제 질문: RSS 611.0→최대 624.2MiB. 이전 측정 최대 874.0MiB.
- 최종 배포 동시 2요청: 각각 약45초 완료, RSS 611.5→최대 641.8MiB, 서비스 메모리 최대897.7MiB, 호스트 가용 메모리 최소834.7MiB. 0.25초 간격397샘플.
- 같은 시각 세 번째 요청은 HTTP429 및 재시도 안내. 두 답변의 감사 ID: `937990a3d9e343f0828c3cca2827b5d6`, `abf5d1e9a9384626b3137a82543d2e3b`.
- 최종 PID380804 유지, NRestarts=0, oom=0, oom_kill=0, health/ready 정상.
- 추가 발견: 수집 중 임시 SQLite DB의 hot journal로 배포 백업 실패. `mantis-complete-` 미게시 파일을 제외하고 회귀 테스트로 검증.
- 추가 발견: 서버 `/tmp`가 tmpfs여서 이번 작업 복구 사본726MiB가 RAM을 차지함. 디스크로 옮겼고 일반 배포 복구 사본 및 검사 사본도 각각 APP_ROOT/backups, STATE_DIR 아래로 변경.
- 복구 백업은 `/opt/content-ai-assistant/backups/`에 내용 해시를 검증한 `.tar.gz`로 보존. 최종 /tmp 사용964KiB, 디스크900MiB 및 가용 메모리872MiB.
- 검증 범위는 이 질문의 단일/동시2요청과 초과·저메모리 거절이며, 모든 질의의 무제한 동시 실행을 보장하지 않는다.
