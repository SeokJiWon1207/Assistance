# PDF 개선 시안 UI 적용·검증 결과

2026-10-23. 첨부 PDF 23쪽을 모두 PNG로 렌더링해 확인했다. 1·5·7쪽은 비교용으로 제외하고 2·6·8쪽을 적용 기준으로 삼았으며, 4쪽의 제안을 공통 컴포넌트에 반영했다. 서브에이전트, 프레임워크 교체, 커밋, 배포는 하지 않았다.

## 적용 범위

| PDF | 적용 내용 |
|---|---|
| 2–4 | 76px 메뉴 레일·272px 대화 사이드바, 760px 기본 대화 폭, AI 표식, 본문 중심 답변, 공통 색·간격·모서리·두 단계 그림자, 아이콘 동작·툴팁·포커스 |
| 3, 9–10 | 실제 사용자 인사, 시안 순서의 시작 템플릿 6개와 전체 목록, 검색·키보드 선택·조건·완성 질문·입력창 삽입·바로 질문 |
| 2, 11–13 | 관련 이슈 통합 목록/표·더 보기, 답변 표 복사/확대, 실제 생성 단계·중지·생성 중 초안 보존, A/B 비교 탭, 관리자 생성 파라미터, 원문·피드백 팝업 |
| 6, 14–17 | 내 업무 목록 정렬·펼침, 주간 보고·팀 브리핑, 플레이북 목록/상세/편집/저장/공개, 알림 필터·읽음 처리, 즐겨찾기 검색·해제·대화 연결 |
| 8, 18 | 운영 판단·실제 지표/차트·필터 의미 유지, 이슈 열 정렬·선택 행, 상세 패널의 원문·유사 질문·AI 질문 연결 |
| 19 | 최초 로딩, 빈 결과, 초기 오류와 재시도, 갱신 실패 시 이전 결과 유지·실패 배지, 부분 조회의 확인 범위와 ‘건 이상’ |
| 20–23 | 기존 품질 현황·사용자 관리의 공통 스타일, 단순한 로그인 화면, 계정 메뉴·다크 모드·로그아웃 확인 |

기존 관리자 세부 메뉴, Notion 관련 기능, 모델·인증·검색 조건·집계·스트리밍·대화 저장 계약은 유지했다. 운영 화면에 PDF의 예시 고객사·이슈·수치를 넣지 않았다.

## 주요 변경 파일

- `assistant_web/backend/static/design_system.css`: 마지막에 읽는 공통 토큰·화면별 스타일. 기존 스타일 파일을 대규모로 덮어쓰지 않고 적용했다.
- `assistant_web/backend/static/modules/ui_details.js`: 아이콘 툴팁, 표 복사·확대, 피드백 글자 수, visualViewport 높이.
- `index.html`, `modules/bootstrap.js`: 스타일·동작 연결과 초기 상태.
- `modules/chat.js`, `chat_rendering.js`, `chat_source_detail.js`, `template_picker.js`, `events.js`: 대화·템플릿·원문·반응형 동작.
- `modules/workspace.js`, `dashboard.js`: 목록·상세·상태 처리.
- `tests/frontend_workspace.test.cjs`, `frontend_navigation.test.cjs`, `frontend_source_quotes.test.cjs`, `test_frontend_core.py`: 회귀 검증.

작업 시작 시 이미 수정되어 있던 파일은 `assistant_web/runtime/pdf_ui_review/baseline`을 기준으로 비교했다. 관련 없는 백엔드·설정·다른 작업의 변경은 수정하지 않았다.

## 실제 브라우저 검증

Chrome의 실제 로그인 세션과 로컬 서비스(127.0.0.1:8787)를 사용했다. 데스크톱 1512×982, 태블릿 768×1024, 모바일 390×844에서 확인했다. 대시보드의 문서 너비는 각 화면 너비와 동일했고, 상세 패널은 태블릿 440px·모바일 전체 폭으로 배치된다. 넓은 표는 표 내부에서만 가로로 스크롤한다.

| 흐름 | 확인 결과 |
|---|---|
| 대화 전송·저장·복원 | 실제 템플릿 질문과 표 질문을 전송하고, 저장된 대화를 다시 열어 내용을 확인 |
| 생성 중지 | 생성 중 다음 질문을 입력하고 Enter를 눌러도 중복 전송하지 않음. 중지 후 초안 유지 |
| 템플릿 | 검색·조건 변경·완성 질문·입력창 삽입 확인. ‘오늘의 운영 현황’의 바로 질문으로 실제 답변 완료, 기존 검증 4/4 통과 표시 |
| 표·관련 이슈 | 표 복사 완료, 확대/닫기, 통합 목록↔표 전환, 표에서 실제 원문 팝업 열기 |
| 원문·피드백 | 저장된 전체 원문 조회, 모바일 원문 팝업, 피드백 이유·글자 수·취소. 피드백 전송은 격리 API에서 확인 |
| 내 업무·보고 | 실제 담당 업무, 정렬 UI, 내 보고↔팀 브리핑, 긴 제목 줄바꿈 |
| 대시보드 | 실제 지표/차트, 지연 필터, 선택 행·상세·이전/다음, 원문 연결, Escape와 포커스 복귀 |
| 모바일 | 대화 목록 열기/닫기, 본문·입력창 너비, 하단 메뉴, 상세 패널과 표 스크롤 확인 |
| 상태 | 격리 API로 로딩·빈 결과·503 오류·갱신 실패·부분 조회를 재현. 갱신 실패 시 이전 결과와 재시도 유지 |
| 관리자·저장 | 격리 API로 품질·사용자 상세·A/B·생성 파라미터·플레이북 저장/공개·알림 모두 읽음·즐겨찾기 검색 확인 |
| 계정 | 실제 로그인 세션 유지. 격리 환경에서 로그인 폼·비밀번호 표시 전환·로그인 완료, 다크/라이트·로그아웃 확인/취소 검증 |

격리 서버는 운영 API와 연결하지 않았으며 화면 하단에 테스트 데이터임을 표시했다. 운영 사용자의 권한·비밀번호·플레이북 공개 상태를 변경하지 않았다. 실제 생성 검증에 사용한 질문은 현재 계정의 대화 기록에 저장된다. 실제 모바일 기기의 소프트 키보드와 네이티브 한글 IME는 직접 테스트하지 못했다. Enter 조합 중 전송 방지는 기존 IME 회귀 테스트와 이벤트 가드로 확인했다.

## 자동 검증

```bash
assistant_web/scripts/check.sh tests compile shell ruff mypy
```

- 모두 통과. 결정형 테스트 2,371개, 커버리지 89%.
- `tests` 단계 안에 전체 Node 프런트엔드 테스트와 JavaScript 구문 검사가 포함된다.
- 관련 Node 테스트 64개를 별도로 실행해 모두 통과했다.
- 마지막 템플릿 순서·간격 토큰 변경 후 `check.sh tests`도 다시 통과했다. 이후 템플릿 폼의 여백, 그림자 토큰, 긴 업무 제목의 줄바꿈 스타일을 보정하고 최종 브라우저 화면에서 확인했다.
- 의존성 변경과 커밋이 없어 `audit` 단계는 실행하지 않았다.
- 실행 로그: `assistant_web/runtime/pdf_ui_review/check-all-relevant.log`, `check-tests.log`, `check-node.log`.

## 남은 차이와 제약

1. **B 답변으로 후속 문맥 전환**: 현재 서버는 직전 A 답변의 `contextAuditId`를 사용한다. B는 비교용임을 표시하고, 최신 A 답변에만 ‘이 답변으로 계속’을 연결했다. B 문맥 선택은 서버 계약 확장이 필요하다.
2. **진행률과 추가 차트**: 서버가 실제로 보내는 생성 단계만 표시한다. PDF의 검색 건수·백분율 진행률이나 근거 없는 주별 차트를 만들지 않았다. 기존 실제 공수·추세·대시보드 차트는 유지했다.
3. **알림 종류**: 현재 API가 주는 일정 지연·장기 미종료 알림을 사용한다. PDF의 담당자 노트 추가·신규 배정 알림은 변경 이벤트 수집과 저장이 필요하다.
4. **외부 원문 링크**: 실제 원본 URL이 있을 때만 표시한다. 없는 URL은 추정하지 않고 기존 저장 원문 조회를 제공한다.
5. **실제 답변 검증 차단**: ‘#31631 이슈의 현재 상태와 다음 할 일을 간단히 요약해줘’ 한 건은 기존 검증기가 2/3 영역 불일치로 차단했다. UI가 오류를 숨기지 않는 것을 확인했으며, 모델 답변·검증 정책은 수정하지 않았다.
6. **로그인·관리자 검증 범위**: 실제 Firebase 신규 로그인과 운영 계정 권한 변경은 실행하지 않았다. 기존 로그인 세션과 격리 로그인 흐름으로 확인했다.

## 화면 증거

모든 이미지와 PDF 렌더링은 `assistant_web/runtime/pdf_ui_review/`에 있다. `fixture`가 붙은 이미지는 격리 테스트 데이터이다.

| 화면 | 이미지 |
|---|---|
| 새 대화 | [데스크톱](../../assistant_web/runtime/pdf_ui_review/new-chat-desktop.jpg) |
| 답변·관련 이슈 | [표 답변](../../assistant_web/runtime/pdf_ui_review/chat-answer-desktop.jpg), [모바일](../../assistant_web/runtime/pdf_ui_review/chat-mobile.jpg) |
| 템플릿 | [검색](../../assistant_web/runtime/pdf_ui_review/template-picker.jpg), [조건](../../assistant_web/runtime/pdf_ui_review/template-form.jpg) |
| 생성·팝업 | [생성/초안](../../assistant_web/runtime/pdf_ui_review/generating-desktop.jpg), [원문](../../assistant_web/runtime/pdf_ui_review/source-mobile.jpg), [표 확대](../../assistant_web/runtime/pdf_ui_review/table-mobile.jpg), [피드백](../../assistant_web/runtime/pdf_ui_review/feedback-mobile.jpg) |
| 내 업무 | [데스크톱](../../assistant_web/runtime/pdf_ui_review/workspace-desktop.jpg), [모바일](../../assistant_web/runtime/pdf_ui_review/workspace-mobile.jpg) |
| 보고 | [주간 보고](../../assistant_web/runtime/pdf_ui_review/weekly-report-desktop.jpg), [팀 브리핑](../../assistant_web/runtime/pdf_ui_review/team-brief-desktop.jpg) |
| 대시보드 | [데스크톱](../../assistant_web/runtime/pdf_ui_review/dashboard-desktop.jpg), [태블릿 상세](../../assistant_web/runtime/pdf_ui_review/detail-tablet.jpg), [모바일 상세](../../assistant_web/runtime/pdf_ui_review/detail-mobile.jpg) |
| 관리자·A/B | [품질](../../assistant_web/runtime/pdf_ui_review/admin-quality-fixture.jpg), [사용자](../../assistant_web/runtime/pdf_ui_review/admin-users-fixture.jpg), [A/B·파라미터](../../assistant_web/runtime/pdf_ui_review/ab-parameters-desktop-fixture.jpg), [모바일 비교](../../assistant_web/runtime/pdf_ui_review/ab-mobile-fixture.jpg) |
| 플레이북 | [모바일 편집](../../assistant_web/runtime/pdf_ui_review/playbook-mobile-fixture.jpg) |
| 상태 | [로딩](../../assistant_web/runtime/pdf_ui_review/loading-mobile-fixture.jpg), [초기 오류](../../assistant_web/runtime/pdf_ui_review/error-mobile-fixture.jpg), [갱신 실패](../../assistant_web/runtime/pdf_ui_review/refresh-failure-mobile-fixture.jpg), [부분 조회](../../assistant_web/runtime/pdf_ui_review/partial-mobile-fixture.jpg) |
| 계정 | [로그인](../../assistant_web/runtime/pdf_ui_review/login-fixture.jpg), [계정 메뉴](../../assistant_web/runtime/pdf_ui_review/account-menu.jpg), [로그아웃 확인](../../assistant_web/runtime/pdf_ui_review/logout-confirm.jpg) |
| 기존 검증기의 차단 상태 | [실제 답변 차단](../../assistant_web/runtime/pdf_ui_review/answer-validation-blocked-desktop.jpg) |
