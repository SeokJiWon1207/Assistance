# 분석 중심 UI 구현 계획

> **For agentic workers:** `superpowers:executing-plans`로 직접 수행한다. AGENTS.md에 따라 서브에이전트는 사용하지 않는다.

**Goal:** 승인된 차트 중심 구조를 main의 실제 데이터·기존 조작과 연결한다.

**Architecture:** 기존 대시보드 응답에 기간 집계와 고객사별 집계를 추가하고 순수 렌더러를 분리한다. 기존 탭·상세·원문 조회를 재사용하고 신규 분석 CSS로 화면 구조를 통일한다.

**Tech Stack:** Python 3.12, 기존 JS/CSS, React/Bklit 차트, Node/Python 테스트, Ego Lite.

**Spec:** `docs/superpowers/specs/2026-11-10-analytics-ui-design.md`

## Global Constraints
- main에서 작업하며 커밋·배포는 요청하지 않았으므로 수행하지 않는다.
- TDS·추가 의존성 없음. 기존 인증·데이터 접근 범위 유지.
- 원문 데이터와 현재 상태를 임의로 변경하지 않는다. 최종수정일을 완료일로 쓰지 않는다.

## Review Focus
- 현재 미종료는 기간 변경에도 동일 기준, 고객사 선택은 모든 신규 분석 집계에 반영.
- 상태 미상·날짜 누락·부분 조회와 실제 0을 구분.
- 칸반 이슈 중복 방지, 완료 업무가 미종료 지표에 섞이지 않음.
- 차트에서 목록으로 이동 후 상세 닫기·탭 복귀·키보드 동작 유지.
- 390px/1024px/1512px, 라이트·다크에서 가로 넘침과 숨겨진 차트 크기 점검.

### Task 1: 분석 집계
Files: `backend/reports/dashboard_analytics.py`, `backend/reports/dashboard.py`, `backend/workspace.py`, `tests/test_dashboard_analytics.py`.
Interfaces: `build_dashboard_analytics(issues, today, days, complete)` → 전체 및 고객사 scopes, 신규·전달·공수 시계열, 비교 순위, 현재 상태 행렬. 개인 응답 `mantis.recentClosed`는 최근 30일 수정된 종료 업무.
- [x] 신규/전달 날짜 기준, 누락·미상, 기간·고객사 범위 테스트 RED 확인.
- [x] 분석 집계와 개인 종료 업무 응답 구현 후 해당 Python 테스트 통과.

### Task 2: 팀 분석 구조와 차트
Files: `modules/dashboard_analytics.js`, `modules/dashboard.js`, `frontend/src/area-chart.tsx`, `frontend/src/charts.tsx`, `index.html`, `analytics.css`, 관련 Node 테스트.
Interfaces: `analyticsScope(data, customer)`, `renderOverview(data)`, `renderComparison(data, dimension)`, `renderFlow(data)` → 기존 chartHost와 data-dashboard-* 동작을 갖는 HTML.
- [x] 고객사 범위·부분 조회·차트 값 보존 테스트 RED 확인.
- [x] 일별 2계열 차트, 세 분석 탭, 전체 폭 구조·범위 필터·관련 이슈 연결 구현.
- [x] Node 테스트, 차트 번들 빌드/타입 검사.

### Task 3: 칸반과 고객사
Files: `modules/workspace_home.js`, `modules/workspace.js`, `modules/customer_overview.js`, `modules/customer_detail.js`, `analytics.css`, 관련 Node 테스트.
Interfaces: `kanbanGroups(issues)` → 독립 단계별 항목. 기존 상세·질문 버튼 및 정렬/기간 유지.
- [x] 단계 분류·중복·미상·완료 분리, 최근 종료 상세 복귀 회귀 검사.
- [x] 내 업무 보드·고객사 비교 표/행렬·상세 추이 우선 구조 구현.
- [x] 관련 프런트엔드 회귀 검사.

### Task 4: 통합 검증
- [x] `assistant_web/scripts/check.sh tests compile shell ruff mypy audit` 실행.
- [x] Ego Lite로 실제 데이터, 기간/고객사/비교축/칸반/상세 복귀, 라이트·다크·반응형 검증.
- [x] 변경 직접 리뷰, 데이터 한계와 확인한 화면을 기록.

## 기록
- 사용자 시안 승인으로 계획 재승인 없이 실행. 테스트·검증 로그는 `/private/tmp/ai-analytics-ui/`에 기록한다.


## 검증 결과
- 전체 결정형 테스트 3,452개 통과, 커버리지 91% (`check-release.log`). 프런트엔드 Node 335개 통과 (`frontend-final.log`).
- 전체 실행 후 차트의 음수 보간 방지를 위해 일별 선을 직선으로 바꾸고, 탭 전환 시 본문을 처음으로 이동하도록 조정했다. 차트 빌드/타입 검사, 번들 일치 검사 2개, 프런트엔드 공통 검사 33개(전체 Node 포함), 업무·분석 49개와 내비게이션 42개를 다시 통과했다.
- compile / shell / ruff / mypy 통과. audit는 샌드박스 DNS 제한 후 네트워크 허용으로 재실행했고 저장소의 기존 보안 예외 정책 안에서 통과했다 (`audit.log`).
- 실제 저장 데이터에서 전체 64건과 LS증권 15건의 현재 미종료를 확인했다. LS증권을 30일 → 7일로 바꿀 때 미종료 15건은 유지되고 신규 3건 → 2건, 전달 2건 → 1건으로 바뀌며 비교표·이슈 목록 범위도 일치했다.
- 종료 카드 상세 열기·닫기 후 원래 카드 포커스 복귀, 고객사 상세 복귀, 방향키 분석 탭 전환, 390 / 1024 / 1512px의 가로 넘침, 라이트·다크 표시를 확인했다.
- 고객사 상세에서 강제 표시한 탭이 암묵적 2열을 만들던 문제와 전체 현황에서 활성 필터 줄이 숨겨지던 문제를 수정했다.
- 최종 스크린샷: `/private/tmp/ai-analytics-ui/final-*.png` 및 `/Users/seokjiwon/.codex/visualizations/2026/11/09/01a1bf00-4260-7b82-bc13-abb03f88dfa3/implemented-*.png`. 큰 추이 차트는 신규 등록일·고객사전달일 기준이고, 히트맵은 등록 주차별 현재 상태다.
- 작업 도중 발견한 evaluation / intent / notion 도구와 해당 테스트·라우팅 fixture의 별도 변경은 이 작업에서 수정하지 않았다. main 작업 트리에 UI 변경을 남겼다.

- 최종 브라우저 확인에서 활성 고객사 필터 줄 표시(`flex`), 탭 전환 후 본문 스크롤 0, 390px 본문 너비 390px를 재확인했다. Ego Lite에는 전체 현황 결과 탭만 남겼다.
