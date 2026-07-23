# 컨텐츠서비스 AI 비서

회사 LLM을 우선 사용하고 장애 시 Ollama의 로컬 LLM으로 자동 전환하는 컨텐츠서비스 대화형 웹앱입니다.

## Run

```bash
cd assistant_web
cp .env.example .env
ollama pull qwen3.5:9b
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8787 --reload
```

브라우저에서 `http://127.0.0.1:8787`로 접속합니다.
Ollama 모델에는 API 키가 필요하지 않습니다. macOS에서는
`scripts/local/AI비서_서버_재시작.command`를 실행해 앱 서버를 다시 시작할 수도 있습니다.

## Test

```bash
cd assistant_web
python3 -m unittest discover -s tests -p 'test_*.py'
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
│   ├── config/              # 모델·고객사·검증 설정
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
NOTION_TOOL_TIMEOUT_SECONDS=12
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
python3 scripts/accuracy/dynamic_accuracy_check.py --questions 10 --follow-up-scenarios 4
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
python3 scripts/accuracy/run_accuracy_check_and_notify.py
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
0 8 * * * cd /path/to/Assistance/assistant_web && /usr/bin/python3 scripts/accuracy/run_accuracy_check_and_notify.py >> runtime/accuracy_reports/cron.log 2>&1
```
