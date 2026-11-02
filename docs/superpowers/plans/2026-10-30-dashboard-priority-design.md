# 우선 확인 이슈 디자인 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. 사용자 지침에 따라 직접 수행한다.

**Goal:** 대시보드 하단 목록의 읽기 순서와 정보 구분을 개선한다.
**Architecture:** 기존 집계·정렬·필터·이슈 상세 이벤트를 유지하고 `renderPriorities`의 마크업과 전용 CSS를 개선한다.
**Tech Stack:** 기존 JavaScript, CSS, Node 회귀 검사.
**Spec:** 사용자가 실제 화면 디자인 수정을 요청했다. 에이전트 판단으로 데스크톱의 비교 가능한 표를 유지하고 모바일에서 행을 카드 배치로 전환한다. 모든 화면을 카드로 바꾸는 방식은 열 간 비교와 목록 밀도가 떨어지므로 선택하지 않는다.

## Global Constraints
- 기존 변경을 보존하고 새 의존성·커밋·서브에이전트를 추가하지 않는다.
- 데이터와 필터, 정렬, 상세 열기, 더 보기 동작을 유지한다.
- 숫자는 현재 필터 목록 기준. 부분 조회는 확인 범위임을 표시한다.

## Task 1: 목록 디자인 및 확인
**Files:** `assistant_web/backend/static/modules/dashboard.js`, `assistant_web/backend/static/dashboard.css`, `assistant_web/backend/static/index.html`, `assistant_web/backend/static/design_system.css`, `assistant_web/tests/frontend_navigation.test.cjs`.
- [x] 번호·상태, 제목, 판단 근거의 계층을 나누고 제목 최대 두 줄 제공.
- [x] 현재 목록 위험도 요약, 고객사·담당자·경과·위험 열 간격 정돈.
- [x] 작은 화면에서 가로 스크롤 없이 읽히는 행 배치.
- [x] 기존 프런트엔드 회귀 및 스타일 검사, 실제 화면 상세·더 보기·모바일 검증.

## Review Focus
- 길거나 특수 문자가 있는 제목·고객사·담당자 표시와 HTML 이스케이프.
- 위험도 미정, 담당자 미지정, 경과일 미상과 0일.
- 빈 목록·부분 조회와 필터 후 요약 건수의 정확성.
- 표 및 버튼의 접근성, 작은 화면과 선택된 행.

## 검증 결과
- `check.sh tests compile ruff` 통과: Python 검사 2,623개(프런트엔드 Node 회귀 포함), 커버리지 90%, 번들·TypeScript·ruff 정상.
- 필터 기준 위험도별 건수, 부분 조회 하한 표기, 이스케이프 회귀 통과.
- 실제 화면에서 이슈 상세 열기/닫기, 5건→68건→5건 전환 확인.
- 모바일 390px에서 가로 넘침 없음, 데스크톱 크기로 복원. 브라우저 오류·경고 없음.
- 직접 변경사항 리뷰와 `git diff --check` 완료. 커밋하지 않음.

## 후속 요청: 상세 패널과 담당자 표기
- [x] 담당자 옆 한 글자 원형 표시 제거.
- [x] 상세 패널 상단에 목록과 같은 위험도·상태 배지 및 경과일 표시.
- [x] 기본 정보, 항목별 판단 근거, 권장 다음 행동을 구분하고 하단 버튼을 고정.
- [x] 데스크톱 패널 폭 440px, 모바일 2열 메타 및 독립 본문 스크롤 적용.
- [x] 이전·다음 탐색과 닫기, 모바일 대화상자 및 가로 넘침 없음 확인.
- [x] `check.sh tests compile ruff` 재검증 통과: 2,623개 테스트, 커버리지 90%, 번들·TypeScript·ruff 정상.
