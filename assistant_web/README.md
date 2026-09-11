# 컨텐츠서비스 AI 비서

회사 LLM을 우선 사용하고 장애 시 Ollama의 로컬 LLM으로 자동 전환하는 컨텐츠서비스 대화형 웹앱입니다.

## Run

```bash
cd assistant_web
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
cp .env.example .env
ollama pull qwen3.5:9b
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8787 --reload
```

브라우저에서 `http://127.0.0.1:8787`로 접속합니다.
Ollama 모델에는 API 키가 필요하지 않습니다. macOS에서 직접 실행하는 로컬 재시작·AWS
재배포·점검 명령은 [`scripts/commands`](./scripts/commands/README.md)에 모여 있습니다.
로컬 앱 서버는 `scripts/commands/AI비서_서버_재시작.command`로 다시 시작할 수 있습니다.

## Firebase 로그인

로그인은 Firebase Authentication의 **이메일/비밀번호**를 사용하고, 인증 성공 후에는
FastAPI가 발급한 HttpOnly 세션 쿠키로 앱 API를 보호합니다. 공개 회원가입은 제공하지
않으며 로컬 승인 사용자 DB(`runtime/users.sqlite3`)에 등록된 계정만 사용할 수 있습니다.

### 1. Firebase 준비

1. Firebase Console → Authentication → Sign-in method에서 `Email/Password`를 활성화합니다.
2. Project settings에서 Web API Key와 Project ID를 확인합니다.
3. Project settings → Service accounts에서 서버용 서비스 계정 JSON을 발급합니다.
4. Authentication → Users에서 최초 관리자 계정을 하나 생성합니다.

서비스 계정 JSON은 Git에 추가하지 않습니다. 로컬에서는 프로젝트 밖의 안전한 경로에
저장하고 `FIREBASE_SERVICE_ACCOUNT_PATH`로 지정합니다. 배포 환경에서는 비밀 저장소에서
`FIREBASE_SERVICE_ACCOUNT_JSON` 환경변수로 주입할 수도 있습니다.

### 2. 환경변수

```bash
FIREBASE_AUTH_ENABLED=true
FIREBASE_WEB_API_KEY=Firebase_Web_API_Key
FIREBASE_PROJECT_ID=Firebase_Project_ID
FIREBASE_SERVICE_ACCOUNT_PATH=/absolute/secret/firebase-service-account.json
FIREBASE_BOOTSTRAP_ADMIN_EMAILS=admin@company.example
AUTH_SESSION_HOURS=8

# 로컬 HTTP에서만 false
AUTH_COOKIE_SECURE=false
```

서버를 재시작한 뒤 최초 관리자 계정으로 로그인합니다. 사이드바 사용자 영역의 `＋` 버튼에서
팀원을 초대하면 Firebase 계정 생성·앱 승인과 함께 비밀번호 설정 링크가 발급됩니다.
링크는 메신저 등 안전한 채널로 사용자에게 전달합니다. 이미 Firebase에 있는 이메일이면
새 계정을 중복 생성하지 않고 앱 사용 승인과 비밀번호 재설정 링크만 발급합니다.

### 3. 운영 보안 설정

운영에서는 도메인과 HTTPS를 먼저 적용하고 다음 값을 사용해야 합니다.

```bash
AUTH_COOKIE_SECURE=true
```

인증이 켜지면 `/health`, 정적 파일, 로그인 API를 제외한 업무 API는 세션이 필요합니다.
`검증`, 정확도 실행, 감사 로그와 사용자 초대 API는 `admin` 역할만 접근할 수 있습니다.
팀원 정보와 Mantis 담당자 연결은 `runtime/users.sqlite3`에 저장됩니다.

### 로그인 사용자 워크스페이스

상단의 `대화 | 내 업무 | 팀 대시보드` 탭으로 주요 화면을 전환합니다. `내 업무`에서는
로그인 사용자별 Mantis 담당 이슈와 Notion 담당 문서, 일정 지연·장기 미종료 알림,
개인 주간 보고를 확인할 수 있습니다. Mantis 담당자명은
사용자 프로필의 `mantisUsername`을 우선 사용하고, 비어 있으면 표시 이름으로 연결합니다.
사내망에서 Mantis 실시간 API에 연결되지 않으면 화면을 실패시키지 않고 로컬 Chroma
스냅샷으로 전환하며 화면에 데이터 기준을 표시합니다.

`내 업무`와 `팀 대시보드`의 기본 조회는 배포 시 검증한 로컬 스냅샷을 함께 사용합니다.
스냅샷은 프로세스에서 재사용하고 파일이 교체되면 자동으로 다시 읽습니다. 사용자가
`새로고침`을 누른 경우에는 실시간 Mantis 조회를 시도한 뒤 실패하면 스냅샷으로 전환합니다.

대화 기록과 즐겨찾기는 Firebase UID별로 `runtime/workspace.sqlite3`에 저장되어 다른
브라우저에서도 이어집니다. 로그인한 모든 팀원은 `팀 대시보드`에서 담당자별
미종료·지연·장기 이슈 현황을 볼 수 있으며, 같은 Mantis 담당자를 사용하는 보조 계정은
중복 집계하지 않습니다.

`팀 대시보드`의 `팀 주간 브리핑` 영역은 매주 월요일 지난주 신규 이슈, 전주 대비 변화, 미종료 이월,
장기·지연·담당자 미지정 이슈와 급증 고객사·화면번호를 기준본으로 생성합니다. 같은 주에는
저장된 기준본을 보여주므로 열 때마다 숫자가 바뀌지 않으며, 매니저·관리자는 필요한 경우
최신 Mantis 데이터로 재생성할 수 있습니다. 사용자별 확인 상태도 함께 저장됩니다.

`플레이북` 탭은 팀에서 검수해 공개한 해결 사례를 증상·원인·해결 방법·검증 방법과
근거 Mantis 이슈로 저장하고 검색합니다. 유사 사례와 조치를 묻는 답변에서는 매니저·관리자에게
`플레이북 후보 저장` 버튼이 표시되며, 저장된 내용은 먼저 검수 대기 초안이 됩니다.
매니저·관리자는 탭에서 내용을 보완한 뒤 공개·초안 전환·보관할 수 있고, 일반 사용자는
공개된 플레이북만 조회할 수 있습니다. 데이터는 `runtime/workspace.sqlite3`에 저장됩니다.

## Test

```bash
cd assistant_web
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

이 명령은 검색·답변 단위 테스트와 FastAPI API 통합 테스트를 실행합니다. Node.js가
설치되어 있으면 프런트엔드 핵심 로직과 JavaScript 구문 검사도 함께 실행합니다.

## Project Structure

```text
Assistance/
├── assistant_web/
│   ├── backend/
│   │   ├── main.py          # FastAPI 앱 조립
│   │   ├── api/             # 도메인별 API 라우터
│   │   └── services/        # 채팅 오케스트레이션
│   ├── config/              # 모델·고객사·팀 템플릿·검증 설정
│   ├── scripts/             # 정확도 검사와 로컬 실행 도구
│   ├── tests/               # 자동 테스트와 수동 검증 질문
│   ├── runtime/             # 감사 DB와 정확도 리포트
│   └── deploy/              # AWS 배포 도구
├── data/
│   ├── mantis_chroma_db/    # 현재 Mantis 벡터 스냅샷
│   └── backups/             # 스냅샷 백업
├── scripts/                 # 프로젝트 단위 데이터 관리 도구
└── ObsidianNotes/           # 프로젝트 문서 볼트
```

## Model Config

모델은 `config/models.json`에서 관리합니다. 첫 번째 활성 모델인 회사 `qwen3.6-35b-a3b`를 기본으로 사용하고, 회사 모델의 HTTP 오류·연결 실패·타임아웃 시 로컬 Ollama `qwen3.5:9b`로 자동 전환합니다.

```json
{
  "id": "company-qwen3.6-35b-a3b",
  "label": "회사 Qwen 3.6 35B",
  "model": "qwen3.6-35b-a3b",
  "provider": "openai-compatible",
  "endpointEnv": "COMPANY_MODEL_46_ENDPOINT",
  "apiKeyEnv": "COMPANY_MODEL_46_API_KEY",
  "think": false,
  "enabled": true
}
```

새 모델을 바로 기본값으로 바꾸기 전에는 해당 모델을 `models.json`에 추가한 뒤
`AB_COMPARISON_MODEL_ID`에 후보 모델 ID를 설정할 수 있습니다. A/B 비교가 켜진 일반 분석 답변에서는
현재 모델과 후보 모델에 동일한 검증·검색 근거를 전달하고 근거 연결·조건 일치·환각 여부를 비교합니다.

```bash
AB_COMPARISON_MODEL_ID=qwen3.5-9b-local
AB_CANDIDATE_TIMEOUT_SECONDS=60
```

후보 모델의 정확도 리포트가 기준 모델보다 안정적으로 높을 때 `models.json`의 기본 활성 순서와
프론트엔드 기본 모델을 변경합니다. API 키나 엔드포인트가 없는 모델은 비교 대상으로 실행되지 않습니다.

## Search Pipeline

대화 요청은 LLM이 먼저 구조화된 실행 계획으로 변환하고, 백엔드가 계획을 검증한 뒤 읽기 전용 도구를 실행합니다.

```text
자연어 질문 -> LLM QueryPlan -> 코드 검증/보정 -> Mantis·Notion 도구 -> 근거 기반 답변 -> 감사 검증
```

`QueryPlan` v2는 `operation(search/count/get/aggregate)`, 검색 소스, 정렬, 표시 개수와 함께
데이터 소스 독립적인 `field/operator/value` 필터 배열을 포함합니다.

```json
{
  "filters": [
    {"field": "customer", "operator": "equals", "value": "유진증권"},
    {"field": "platform", "operator": "not_equals", "value": "MTS"}
  ]
}
```

지원 연산자는 `equals`, `not_equals`, `in`, `not_in`, `contains`, `not_contains`,
`exists`, `not_exists`, `gte`, `lte`, `between`입니다. Mantis·Notion 어댑터는 공통 조건을
각 API 필터와 로컬 후처리로 변환합니다. 적용할 수 없는 조건은 조용히 버리지 않고 답변과
감사 로그의 `unapplied_filters`에 남깁니다. 연도와 `몇 개/몇 건`처럼 규칙으로 명확히 검증할 수 있는 조건은
모델 출력과 독립적으로 재검사해 누락되거나 잘못 해석된 계획을 보정합니다.

Mantis 읽기 도구는 다음 원칙으로 선택합니다.

1. Mantis 전체 로컬 인덱스
   - 고객사·플랫폼·화면번호·담당자·등록일처럼 구조화 필터로 완전히 표현되는 목록과 건수 집계에 사용합니다.
   - 건수 집계는 상위 N개가 아니라 전체 인덱스를 스캔하고, 답변에는 대표 이슈만 첨부합니다.

2. Mantis 저장 필터 API
   - `config/mantis_filters.json`에 질문 키워드와 `filter_id`가 매핑되어 있을 때 사용합니다.
   - 예: `신한투자증권` -> `filter_id=123`

3. Mantis 페이지 스캔
   - 자유 키워드 검색처럼 로컬 구조화 필터만으로 처리할 수 없을 때 사용합니다.
   - `page_size=100`으로 최대 8페이지를 가져와 질문의 고객사/매체 조건과 키워드로 필터링합니다.

4. ChromaDB 유사도 검색
   - `../data/mantis_chroma_db/chroma.sqlite3`에서 관련 과거 이슈 문서를 찾습니다.
   - 기준 이슈 기반 검색은 `bge-m3` 임베딩 거리로 정렬하며, 기준 이슈의 고객사는 검색어로 강제하지 않습니다.
   - Ollama 임베딩 서버를 사용할 수 없으면 제목의 한글 문자 유사도 순위로 자동 대체합니다.
   - 정확한 목록 조회보다는 유사 사례/과거 맥락 보조 용도입니다.
   - 질문에 고객사, HTS, MTS 같은 조건이 있으면 해당 조건을 만족하는 문서만 후보로 사용합니다.

감사 로그는 원문 질문, QueryPlan, 소스별 적용·미적용 필터, 조회 단계와 결과 수,
조회 근거, 계산 결과와 최종 답변을 각각 기록·검사합니다.
질문에 연도가 있는데 실행 계획에 등록일 범위가 없거나, 건수 질문이 목록 검색으로 처리되면 통과할 수 없습니다.

선택 설정:

```bash
QUERY_PLANNER_ENABLED=true
NOTION_TOOL_TIMEOUT_SECONDS=8
NOTION_TOOL_MAX_RETRIES=0
```

저장 필터를 사용하려면 `config/mantis_filters.json`에서 `filter_id`를 실제 Mantis 필터 ID로 채우고 `enabled`를 `true`로 바꾸면 됩니다.

### 로컬 스냅샷 갱신과 데이터 기준일

로컬 인덱스(`../data/mantis_chroma_db`)는 `../scripts/make_vector_db.py`로 구축/갱신합니다.

```bash
cd ..                                 # Assistance 루트에서 실행
python3 scripts/make_vector_db.py             # 증분: 새 이슈만 추가
python3 scripts/make_vector_db.py --refresh-days 14   # + 최근 14일 내 수정 이슈 재임베딩(상태 변경 반영)
python3 scripts/make_vector_db.py --full      # 전체 재구축(기존 DB는 data/backups에 자동 백업)
```

- 인증 정보는 `assistant_web/.env`의 `MANTIS_API_URL` / `MANTIS_API_TOKEN`을 사용합니다(하드코딩 없음).
- 재구축된 문서에는 `매체구분`, `등록일` 헤더가 포함되어 플랫폼/날짜 필터 정확도가 올라갑니다.
- **중요**: 등록일 헤더가 없는 옛 포맷 스냅샷에서는 "N월 이슈 몇 건" 같은 날짜 집계가
  0으로 새어 정확도가 떨어집니다. `scripts/make_vector_db.py --full`로 한 번 재구축하면
  모든 문서에 등록일이 채워져 월/연 단위 집계가 정확해집니다.
- 스냅샷 기반 답변에는 "데이터 기준: 로컬 스냅샷 YYYY-MM-DD 수집분" 안내가 붙고,
  질문의 날짜 범위가 스냅샷 기준일 이후를 포함하면 실시간 페이지 스캔을 병행해 최신 구간을 보완합니다.
- 날짜 조건이 있는 건수 집계는 스냅샷에 등록일이 없어도 정확하도록, 스냅샷 매칭
  결과와 실시간 Mantis 조회 결과를 이슈 ID 기준으로 합집합해 집계합니다("최신 N건" 창을
  질문이 지정하면 그 범위, 없으면 기본 최근 800건을 실시간으로 확인).

매일 새벽 증분 갱신 crontab 예시:

```cron
30 6 * * * cd /path/to/Assistance && /usr/bin/python3 scripts/make_vector_db.py --refresh-days 7 >> data/mantis_chroma_db/rebuild.log 2>&1
```

### 고객사 별칭 관리

고객사 별칭은 `config/customer_aliases.json`에서 관리합니다. 신규 고객사는 코드 수정 없이
이 파일에 항목을 추가하고 서버를 재시작하면 됩니다. 파일이 없거나 형식이 잘못되면
내장 기본값으로 동작합니다.

```json
{
  "키움": ["키움증권", "Kiwoom", "150Kiwoom"]
}
```

### 팀 공용 템플릿 관리

첫 화면의 팀 공용 질문 카드는 `config/team_templates.json`에서 관리합니다. 카드 설명,
입력 필드, 선택지와 `questionTemplate`을 수정하면 프런트엔드 코드 변경 없이 반영됩니다.
서버는 중복 ID, 지원하지 않는 필드·아이콘, 잘못된 자리표시자를 검증한 뒤
`GET /api/templates`로 전달합니다. 검증 결과는 프로세스 수명 동안 캐시되므로 설정 변경은
서버 재시작 또는 새 배포 후 반영됩니다.

질문 자리표시자는 `{field}` 외에 빈 값의 기본값을 정하는 `{field|기본값}`과 특정 선택값을
질문에서 생략하는 `{field?생략값}`을 지원합니다. 예를 들어 `{platform?전체}`는 선택값이
`전체`일 때 질문에서 빠집니다. 고객사 프로필 템플릿은 고객사·기간·플랫폼·상태를 받아
이슈 추이, 미종료·장기 이슈, 담당자 분포와 평균 처리기간을 하나의 결정형 리포트로 반환합니다.
월간 고객사 리포트 템플릿은 고객사·월·플랫폼을 받아 전월 대비 등록 건수, 현재 상태,
공수, 처리기간, 빈발 화면과 장기 미종료 이슈를 통합합니다. 진행 중인 달은 전월의 같은
날짜까지와 비교하며, 현재 상태와 처리기간 근사 기준을 답변에 명시합니다.

### 추세·급증 분석

건수 집계에 시간축 해석을 얹은 결정형 리포트입니다. 새 데이터 수집 없이 로컬
스냅샷의 등록일만으로 동작합니다.

- **월별 추이**: "월별 이슈 건수 추이" 같은 `month` 그룹 집계는 단순 나열이 아니라
  전월 대비 증감(`▲8, +26.7%`)과 추세 요약(최다월·월평균·평균 대비)을 함께 냅니다.
- **급증(이상탐지)**: "평소보다 급증한 고객사/화면번호/담당자"처럼 시점 변화를 묻는
  질문은 '많은 순'이 아니라 최근 월 건수를 직전 몇 개월 평균과 비교한 '튄 순'으로
  답합니다. 직전 구간에 없던 신규 급증도 함께 드러냅니다.
- **피벗(교차 표)**: "고객사별 월별", "화면번호별 달마다"처럼 축과 월을 함께 지정하면
  축(고객사/화면번호/담당자/상태) × 월 교차 건수를 표로 냅니다. 가독성을 위해 최근
  `PIVOT_MAX_MONTHS`개월과 합계 상위 `PIVOT_MAX_ROWS`개 행만 보이고, 잘린 경우 안내합니다.
  축이 하나뿐이면 피벗 대신 단일 축 집계(위 월별 추이/그룹 집계)로 처리합니다.

```text
평소 대비 급증한 고객사 (2개, 급증 강도 순)
- 신한투자증권 — 최근 2026-07 9건 (신규 급증, 직전 0건) ▲
- 유진증권 — 최근 2026-07 18건 (직전 평균 6.0건, 3.0배) ▲
```

급증 판정 기준은 `backend/answers.py`의 `SPIKE_MIN_RECENT`(최소 건수),
`SPIKE_RATIO`(급증 배수), `SPIKE_BASELINE_MONTHS`(기준선 개월 수)로 조정합니다.
월 구간이 1개뿐이면 비교가 불가능하므로 스냅샷을 `--full`로 재구축해 등록일을
채우라고 안내합니다.

### 처리시간(SLA) 분석

라이브 Mantis 이슈의 등록일·최종수정일·상태로 처리 성과를 근사하는 결정형
리포트입니다(`backend/reports/sla.py`). 스냅샷에는 최종수정일이 없어 라이브 조회를
기준으로 합니다.

- **완료(리드타임)**: "평균 처리시간 며칠 걸려?"처럼 물으면 **종료** 상태 이슈의
  등록→최종수정 소요일을 평균·중앙값·최장/최단으로 집계하고, 가장 오래 걸린 이슈를
  보여줍니다. "고객사별/담당자별"을 붙이면 축별 평균도 냅니다.
- **적체(경과일)**: "가장 오래 방치된 이슈", "장기 미종료"처럼 물으면 **미종료** 이슈의
  경과일(오늘−등록일)을 랭킹하고 `SLA_AGING_THRESHOLD_DAYS`(기본 30일) 초과 건수를 냅니다.

이 시스템은 '해결'을 아직 열린 상태로 보므로 완료 기준은 '종료'입니다. 정확한 종료
시각 필드가 없어 최종수정일을 완료 시각의 근사로 쓰며, 답변에도 그 사실을 밝힙니다.
'미종료' 같은 순수 상태어는 건수 질문과 겹치므로 SLA가 가로채지 않습니다.

### 유사사례 해결책

"#12345랑 비슷한 이슈 **어떻게 해결했어?**"처럼 기준 이슈 + 유사 + 해결 의도가 모두
있을 때, 비슷한 과거 사례와 **그때의 조치**를 함께 정리합니다(`backend/reports/similar.py`).
ChromaDB 임베딩으로 유사 이슈를 찾은 뒤, 라이브로 갱신된 각 이슈의 상태·상태 변경
이력·마지막 노트에서 처리 내용을 요약합니다. 해결 의도가 없으면 기존 유사 이슈 목록
경로로 흘려보냅니다.

```text
🔍 #12345 "로그인 세션 만료 오류"와 비슷한 과거 사례와 그때의 조치를 정리했어요.
1. #9001 세션 만료 재로그인 필요 (유진증권)
   - 그때 처리: 상태 종료된 이슈 · 상태 변경 할당 → 종료 · 마지막 조치: 타임아웃 30분으로 조정.
```

## Notion Integration

Notion은 백엔드에서 공식 API를 직접 호출합니다. 토큰과 DB ID는 프론트엔드에 노출하지 않고 `.env`에만 저장합니다.

```bash
NOTION_API_TOKEN=secret_xxx
NOTION_DATABASE_IDS=이슈리스트_테스트_DB_ID,주요이슈현황_테스트_DB_ID
NOTION_API_VERSION=2022-06-28
NOTION_FETCH_PAGE_BLOCKS=true
```

설정 순서:

1. Notion 개발자 포털에서 Internal connection을 생성하고 access token을 발급합니다.
2. `AI_Notion / 이슈리스트_테스트`, `AI_Notion / 주요이슈현황_테스트` 두 데이터베이스 우측 상단 메뉴에서 해당 connection을 각각 추가합니다.
3. 두 데이터베이스 URL의 32자리 ID를 쉼표로 구분해 `NOTION_DATABASE_IDS`에 입력합니다.
4. 서버를 재시작하고 `/api/notion/status` 또는 `/api/notion/search`로 연결 상태를 확인합니다.

예시 질문:

- `노션 주요이슈 보여줘`
- `노션 결재요청 상태인 항목 보여줘`
- `노션에서 이번 주 기한 있는 항목 정리해줘`

## 결과 검증 센터

사이드바의 `검증` 버튼으로 여는 검증 센터는 두 층위의 검사를 제공합니다.

- **검증 질문 탭**: 기준 질문들의 검색 파이프라인(고객사/플랫폼/화면번호 인식,
  정확 ID 조회, Notion 매칭, 공수 집계)을 검사합니다. LLM 답변 생성 단계는
  검사하지 않아 빠릅니다(수 분).
- **답변 정확도 탭**: `scripts/accuracy/dynamic_accuracy_check.py`를 서버가 서브프로세스로 실행해
  실시간 데이터 기반 기본 질문 10개와 후속 질문 시나리오 4개를 실제 챗 응답까지
  채점합니다. 후속 질문은 1차 답변의 대화 이력과 `contextAuditId`를 실제 웹과 동일하게
  전달합니다.
  질문별 진행 상황이 스트리밍되고, 완료된 리포트(`runtime/accuracy_reports/`)의 최근
  이력과 상세 결과를 같은 탭에서 조회합니다
  (`POST /api/accuracy/run`, `GET /api/accuracy/reports[/{name}]`).
  동시에 하나의 실행만 허용됩니다.

- 실행은 케이스 단위로 스트리밍되어 진행 상황(대기 → 실행 중 → 통과/실패)과
  케이스별 소요 시간이 실시간으로 표시됩니다. 카드의 `재실행` 버튼으로 특정
  질문만 다시 검사할 수 있습니다.
- 실행 결과는 `runtime/assistant_audit.sqlite3`의 `evaluation_runs` 테이블에 저장되고,
  검증 질문 탭 상단에 최근 실행 이력이 표시됩니다(`GET /api/evaluation/history`).
- 검증 질문은 `config/evaluation_cases.json`에서 코드 수정 없이 추가/수정합니다.
  파일이 없거나 형식이 잘못되면 내장 기본값으로 동작합니다.

```json
{
  "id": "mantis-kb-hts",
  "category": "고객사·플랫폼",
  "question": "KB증권 HTS 최근 이슈 5개 보여줘",
  "expected": { "customer": "KB증권", "platform": "HTS", "minResults": 1 }
}
```

`expected`에 쓸 수 있는 키: `customer`, `platform`, `screenNumber`, `person`, `issueId`,
`sourceIds`, `source`, `titleContains`, `databaseLabel`, `effortTotal`, `issueCount`,
`totalCount`, `minResults`.

화면번호 검증은 커스텀 필드의 정확한 값과 제목의 `[화면번호]` 표기를 같은 근거로
인정하며, 반환된 모든 이슈가 해당 번호와 정확히 일치하는지도 검사합니다. 월별 건수
케이스는 `totalCount`, 담당자 케이스는 `person`, 필수 포함 이슈는 `sourceIds`로
회귀 기준을 고정할 수 있습니다.

답변 정확도 탭에는 최근 리포트 점수 추세, 직전 실행 대비 증감, 반복 실패 유형과
Mantis 로컬 스냅샷 기준일/지연 일수가 함께 표시됩니다. 신선도는 1일 이내 `최신`,
3일 이내 `갱신 권장`, 그보다 오래되면 `지연`으로 분류합니다
(`GET /api/data/freshness`).

## Scheduled Accuracy Check

`scripts/accuracy/dynamic_accuracy_check.py` builds ten reproducible base questions from live source
data, then runs four follow-up scenarios against the base answers' real conversation
history and audit context. It compares answer IDs, titles, counts, and referenced
entities with an independently fetched Mantis/Notion snapshot.

```bash
cd assistant_web
.venv/bin/python scripts/accuracy/dynamic_accuracy_check.py --questions 10 --follow-up-scenarios 4
```

The daily seed changes the sampled records while keeping a run reproducible.
JSON and Markdown results are written to `runtime/accuracy_reports/`. Accuracy
assertion failures are counted separately from dependency/execution errors. A
run is `incomplete` when an integration is unavailable or fewer than the
requested number of questions can be evaluated; unavailable cases are excluded
from the accuracy denominator. A non-zero exit means the run is `failed` or
`incomplete`. Reports preserve the overall summary and also include separate
`base`, `follow_up`, and Mantis/Notion source summaries. A follow-up whose setup
question fails is recorded as `blocked_by_prerequisite`.

팀 검증용 기본 질문은 [`tests/test_questions.md`](./tests/test_questions.md), 복합 조건과 연속 대화 질문은
[`tests/test_scenario_questions.md`](./tests/test_scenario_questions.md)에 정리되어 있습니다.

### Slack 알림과 함께 자동 실행

`scripts/accuracy/run_accuracy_check_and_notify.py`는 위 검증을 실행하고, 통과하지 못했을 때만(fail 또는
경고) Slack으로 알린다. 그리고 리포트가 생성되면 요약본을 Obsidian 볼트에도
Markdown 노트로 남긴다. 통과 시에는 Slack만 조용히 넘어간다.

```bash
cd assistant_web
.venv/bin/python scripts/accuracy/run_accuracy_check_and_notify.py
```

Slack 알림을 받으려면 `.env`에 Incoming Webhook URL을 추가한다.
Obsidian 노트는 `OBSIDIAN_VAULT_PATH`를 지정하면 그 위치에 저장되고, 비워두면
기본 후보 경로(`../ObsidianNotes`, `~/Documents/Obsidian Vault`)를 자동 탐지한다.

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx/yyy/zzz
# 선택: 하루에 검증할 질문 개수(기본 10)
ACCURACY_CHECK_QUESTIONS=10
OBSIDIAN_VAULT_PATH=/absolute/path/to/your/vault
OBSIDIAN_ACCURACY_FOLDER=Automation Reports/Mantis Notion
```

매일 자동 실행하려면 아래처럼 `crontab -e`에 직접 등록한다(이 스크립트는 스스로
cron에 등록하지 않는다 - 시스템 스케줄 변경은 사용자가 직접 결정해야 한다).

```cron
0 8 * * * cd /path/to/Assistance/assistant_web && .venv/bin/python scripts/accuracy/run_accuracy_check_and_notify.py >> runtime/accuracy_reports/cron.log 2>&1
```


### 대화 보존과 Notion 확인 작업

대화 저장에는 마지막 서버 수정 시각을 함께 전달합니다. 서버 버전이 달라졌으면
기존 기록을 덮어쓰지 않고 현재 기기의 변경을 별도 복구본으로 보존합니다. 저장 실패한
대화는 로컬에 미동기화 상태로 남으며 재연결·다음 로딩에서 재시도합니다.

채팅의 Notion 쓰기 미리보기는 로그인 UID, 대상 페이지/DB, 실제 전송할 속성을 서버에
10분 동안 고정합니다. 확인 ID는 대화와 함께 저장됩니다. 확인을 다시 보내도 완료 결과를
돌려주며 외부 쓰기를 반복하지 않습니다. 통신 장애로 외부 쓰기 성공 여부가 불확실하면
자동 재실행하지 않으므로 Notion에서 결과를 확인한 뒤 새 작업을 요청해야 합니다.
작업 기록은 runtime의 `notion_actions.sqlite3`에 저장되며 배포 직전 DB 백업에 포함됩니다.

결정형 테스트 실행기는 인증·대화·감사·검색 DB를 임시 디렉터리로 격리하고 실제 `.env`
로딩과 기본 네트워크 연결을 차단합니다. 외부 쓰기·배포 검증은 포함하지 않습니다.
