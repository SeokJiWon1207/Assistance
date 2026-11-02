# 대시보드 순서·여백 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. 메인 에이전트가 직접 수행한다.

**Goal:** 승인된 운영 요약 → 핵심 지표 → 우선 확인 이슈 → 분석 차트 순서를 적용한다.
**Architecture:** 기존 dashboard.js의 렌더링 순서를 조정하고, 상단에서 중복 추이 차트를 제거해 지표를 압축한다. 모바일 운영 판단은 버튼으로 근거·추천 행동을 펼치며 데스크톱에서는 모두 표시한다.
**Tech Stack:** 기존 JavaScript와 CSS, Node 회귀 테스트.
**Spec:** 이 대화의 UI/UX 검토 후 사용자가 요청한 첫 번째 개선. 분석 차트와 집계·필터 의미는 유지한다.

## 작업 및 확인
- [x] 목록을 지표 바로 아래로 이동하고 로딩 뼈대도 같은 순서로 변경.
- [x] 신규 이슈 지표는 숫자·비교·상태 요약만 유지. 일별 추이는 하단 상세 차트로 제공.
- [x] 모바일 운영 판단의 펼치기·접기와 키보드 접근성 구현.
- [x] 기존 필터·부분 조회와 새 렌더링 순서 회귀 검사, 실제 데스크톱·모바일 배치 확인.
- [x] 직접 diff 리뷰 및 관련 검사. 커밋은 요청 시 수행.

## 검증 결과
- `assistant_web/scripts/check.sh tests compile ruff`: 2,623개 테스트 통과, 커버리지 90%, 컴파일·정적 검사 통과.
- 실제 Chrome: 데스크톱 지표 높이 약 118px, 섹션 간격 16px. 상세 패널을 열어도 지표 가로 넘침 없음.
- 390px 모바일: 근거·추천 행동 기본 접힘, 클릭 펼치기·Enter 접기, 일정 지연 필터 2건 확인.
- `artifacts/dashboard-order-spacing-desktop.jpg`, `artifacts/dashboard-order-spacing-mobile.jpg`에 결과 기록.
