# UI 일관성과 정보 계층 개선 결과

작업일: 2026-10-24~25. 사용자가 승인한 순서대로 컴포넌트 통일, 글자·여백 조정, 중복 테두리 축소, 주요 행동 강조를 적용했다.

## 적용 내용

1. 공통 컴포넌트: 입력창 16px, 버튼 8px, 배지 6px, 카드 12px 토큰 적용 범위를 로그인·검색·템플릿·피드백·관리자·차트로 확장했다. 실제 업무 정렬 컨트롤을 36px 높이로 맞췄다. A/B 복사 버튼을 같은 32px 아이콘 버튼으로 통일하고 B 답변의 복사 대상과 접근 가능한 이름을 유지했다.
2. 글자와 여백: 업무 제목 16px, 설명 13px, 운영 판단 제목 20px를 기준으로 정리했다. 주요 내부 여백은 데스크톱 24px, 모바일 16px를 사용한다. 긴 업무 제목과 메타 정보는 줄바꿈한다.
3. 경계: 차트·요약 카드 외곽과 섹션 헤더의 중복 선을 줄였다. 표·이슈 목록·폼·선택·포커스·위험 표시는 유지했다.
4. 행동: 대시보드와 팀 브리핑의 첫 권장 행동은 파랑, 보조 행동은 옅은 회색으로 구분했다. 관리자 선택 필터는 공통 파랑 배경을 사용한다. 위험 행동은 공통 보조 버튼 덮어쓰기에서 제외했다.

수정 파일은 `backend/static/design_system.css`, `admin_center.css`, `charts.css`, `template_picker.css`, `modules/chat.js`이다. 기존 스택, API, 권한, 집계 기준, 검색 의미를 변경하지 않았다. 앞선 정확도 수정은 그대로 보존했다. 커밋과 배포는 하지 않았다.

## 검증

- `assistant_web/scripts/check.sh tests compile shell ruff mypy` 통과: 2,401개 Python 테스트(내부 Node 회귀 검사 포함), 커버리지 89%, mypy 159개 파일. 최초 검사에서 모바일 숫자 28px가 기존 크기 규칙에 맞지 않아 26px로 수정하고 재검사했다.
- `git diff --check` 통과.
- 실제 Chrome, 격리 로컬 서버에서 1512×982 / 768×1024 / 390×844 확인. 확인한 화면에서 문서 전체 가로 넘침 없음. 모바일 긴 업무 제목의 컨테이너 넘침 없음. 관리자 상세 패널은 모바일 폭 390px에 맞게 배치됨.
- 대시보드 차트 선택 → 고객사 필터와 이슈 4건 목록 연동 → 이슈 상세 열기/닫기 확인.
- 내 업무 정렬 전환, 주간 보고·팀 브리핑 전환 확인.
- A/B 비교 표시와 B 복사 확인. 복사 후 입력창에 붙여 넣어 B 본문과 표가 복사된 것을 확인했다.
- 템플릿 검색 → 선택 → 조건 30일에서 45일로 변경 → 완성 질문 → 입력창 삽입 → 전송 → 생성 중지 확인.
- 피드백 팝업, 관리자 품질 현황, 사용자 상세 패널, 포커스·선택·비활성 상태 확인.
- 다크 모드 전환, 로그아웃 확인 팝업, 로그아웃, 재로그인 확인. 브라우저 오류 로그 없음.

## 화면 기록

모든 캡처의 수치·이름은 격리 검증 데이터이며 운영 수치가 아니다. 캡처 아래에 검증 환경 배너를 표시했다.

- [내 업무 데스크톱](../../assistant_web/runtime/ui_polish_20261024/workspace-desktop.jpg)
- [대시보드 데스크톱](../../assistant_web/runtime/ui_polish_20261024/dashboard-desktop.jpg)
- [피드백 팝업](../../assistant_web/runtime/ui_polish_20261024/feedback-desktop.jpg)
- [대시보드 태블릿](../../assistant_web/runtime/ui_polish_20261024/dashboard-tablet.jpg)
- [내 업무 모바일](../../assistant_web/runtime/ui_polish_20261024/workspace-mobile.jpg)
- [템플릿 모바일](../../assistant_web/runtime/ui_polish_20261024/template-mobile.jpg)
- [대화 중지 모바일](../../assistant_web/runtime/ui_polish_20261024/chat-mobile.jpg)
- [대시보드 모바일 다크](../../assistant_web/runtime/ui_polish_20261024/dashboard-mobile-dark.jpg)
- [사용자 상세 다크](../../assistant_web/runtime/ui_polish_20261024/admin-dark.jpg)
- [로그인 모바일](../../assistant_web/runtime/ui_polish_20261024/login-mobile.jpg)

검사 로그: `assistant_web/runtime/ui_polish_20261024/check-final.log`.

## 검증 범위와 제약

- 외부 Firebase 인증과 모델 응답은 격리 서버의 테스트 대체를 사용했다. 앱의 세션·권한·대화 저장·화면 전환 코드는 실제 구현을 사용했다. 운영 인증/모델 서버를 대상으로 재검증하거나 운영 데이터를 수정하지 않았다.
- 반응형 검증은 데스크톱 Chrome의 뷰포트 변경으로 진행했다. 실기기 터치·가상 키보드·한글 IME 조합은 이번 시각 검증에서 별도로 재현하지 않았다.
- 토스의 브랜드 색상·폰트·모션을 그대로 복제하는 작업이 아니다. 기존 PDF의 모서리 기준과 업무 서비스의 정보량을 유지하면서 위계와 일관성을 개선했다.
