# 미종료 분포 시안 적용 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. 사용자 지침에 따라 메인 에이전트가 직접 수행한다.

**Goal:** 사용자가 승인한 첫 번째 수정 시안을 실제 미종료 분포에 적용하고, 추가 요청한 신규 추이·현재 상태 카드의 비율과 높이를 맞춘다.

**Architecture:** 기존 dashboard.js 집계와 이벤트 위임을 보존하고 Bklit React island에 고객사 순위 막대, 경과일 도넛, 팀원 상태 히트맵을 추가한다. 원본 Bklit 코드는 변경하지 않고 서비스 전용 레이어와 스타일로 구성한다.

**Tech Stack:** 기존 JavaScript, React/TypeScript, vendored Bklit, esbuild, Node tests.

**Spec:** 이 대화에서 승인한 수정 첫 번째 시안: 고객사 이름/합계/상태별 숫자가 있는 누적 가로 막대, 경과일 도넛과 4개 필터, 팀원 13명 전체 상태별 히트맵. 실제 데이터만 사용하며 예시 숫자는 이식하지 않는다.

## Global Constraints
- 사용자 변경과 관련 없는 파일은 수정하거나 커밋하지 않는다.
- Python 명령은 assistant_web/.venv/bin/python으로 실행한다.
- 집계 기준, 필터 의미, 인증·보안 설정은 유지한다.
- 부분 조회에는 전체 비율을 표시하지 않는다. 등록일 미상 건수는 분모에서 제외하고 안내한다.

## Review Focus
- 빈 결과와 모든 값 0: 유효하지 않은 비율/도형 없이 수치를 제공한다.
- 일부 조회와 등록일 미상: 전체 건수/비율로 오인하지 않는다.
- 긴 고객사명과 악성 문자열: 이름과 필터 원본 키를 보존하고 안전하게 표시한다.
- 팀원 0건 및 13명 이상: 누락 없이 합계와 상태별 수치를 제공한다.
- 번들 실패 및 키보드: 기존 수치와 필터가 계속 사용 가능하다.

## Task 1: 분포 렌더링과 연결
**Files:** frontend/src/distribution-charts.tsx 신규, frontend/src/charts.tsx, backend/static/modules/dashboard.js, backend/static/bklit_charts.css 수정. tests/frontend_navigation.test.cjs 회귀 추가.
- [x] 실제 대시보드 집계·필터·미상 등록일을 검증하는 회귀를 추가하고 실패 확인.
- [x] 전용 React 구성과 dashboard 차트 사양 연결. 원본 필터 속성과 fallback 유지.
- [x] 고객사 상태별 수치, 경과일 30일 이상 비율, 팀원별 상태 히트맵을 실제 데이터로 표시.
- [x] Node 회귀, TypeScript 검사 및 배포 번들 갱신.

## Task 2: 실제 화면 검증
**Files:** backend/static/vendor/bklit 생성 번들, 관련 정적 파일 캐시 키.
- [x] check.sh tests compile ruff 실행.
- [x] 실제 로컬 화면에서 차트, 필터 선택, 작은 화면, 콘솔 오류 확인.
- [x] 변경 diff 자체 리뷰 후 결과 보고. 커밋은 요청 시 수행.

## 추가 요청: 신규 이슈 카드 균형
- [x] 추이·현재 상태 카드의 높이를 맞추고 열 비율을 1.55:1로 조정.
- [x] 오른쪽 도넛을 260px로 확대하고 상태별 건수·비율을 아래 세로 목록으로 정렬.
- [x] 조회·집계 기준 설명을 카드 하단에 배치하고 모바일에서 한 열로 전환.

## 검증 결과
- `assistant_web/scripts/check.sh tests compile ruff`: 2,623개 테스트 통과, 커버리지 89%, TypeScript·번들·ruff 통과.
- 실제 React 서버 렌더링: 정상 비율, 부분 조회, 0건, 등록일 미상 분모, 문자열 이스케이프 확인.
- 실제 Chrome: 고객사·경과일 필터 적용/해제, 팀원 셀 상세, SVG 내부 버튼 접근성, 콘솔 오류 없음 확인.
- 390px 화면에서 가로 넘침 없음. 데스크톱 두 카드 높이 모두 570.99px.
- 변경사항 직접 리뷰 및 `git diff --check` 완료. 커밋하지 않음.
