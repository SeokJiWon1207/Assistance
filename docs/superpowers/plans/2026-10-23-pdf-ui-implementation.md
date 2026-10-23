# PDF 개선 시안 UI 적용 계획

**목표:** 첨부 PDF 23쪽을 모두 이미지로 검토한 결과를 기존 FastAPI·바닐라 JS·CSS 화면에 적용한다.
**기준:** 1/5/7쪽은 비교용, 2/6/8쪽이 목표이며 4쪽 제안이 공통 컴포넌트 기준이다. 예시 데이터는 사용하지 않는다.
**실행:** 사용자가 직접 구현·검증까지 요청했으므로 승인 대기 없이 메인 에이전트가 수행한다. 서브에이전트·커밋·배포는 하지 않는다.
**보존:** 작업 시작 시 변경된 모든 파일을 기준으로 삼는다. UI 기준본은 git-ignored runtime/pdf_ui_review/baseline에 보관한다. 인증·권한·집계·검색 의미는 바꾸지 않는다.

| 단계 | PDF | 파일(assistant_web/backend/static 기준) | 구현 및 확인 |
|---|---|---|---|
| 1 | 2–4 | styles.css, index.html, conversation_list.css, responsive.css | 공통 색·간격·6/8/12/16px 모서리, 두 단계 그림자, 76px 레일/272px 사이드바, 본문·입력 폭, 이름·인사, 툴팁 |
| 2 | 9–13 | modules/chat.js, chat_rendering.js, chat_feedback.js, chat_source_detail.js, events.js, template_picker.css | 관련 이슈 통합 목록/표, 더 보기, 표 복사/확대, 생성 중 초안 입력, 원문 동작, 피드백 글자 수, A/B 접근성 |
| 3 | 6,14–17 | modules/workspace.js, events.js, workspace.css | 실제 사용자명, 부분 조회 하한, 업무 정렬/펼침, 플레이북 선택/편집, 알림 필터·읽음·상세, 즐겨찾기 검색/해제 |
| 4 | 8,18,19 | modules/dashboard.js, dashboard.css | 이슈 우선 열 순서, 선택 행, 상세 원문·유사 질문, 부분 조회 하한, 새로고침 실패 재시도 |
| 5 | 20–23 | admin_center.css, index.html, styles.css | 관리자 토큰/모바일, 계정 순서, 로그인·로그아웃 확인 |
| 6 | 전체 | tests/frontend_*.test.cjs, docs/evaluations | 실제 Chrome 데스크톱/768px/390px, 주요 흐름·팝업, check.sh tests compile shell ruff mypy, 화면 캡처 |

## 기능 차이와 결정
- B 답변 후속 문맥 전환: 서버 contextAuditId가 A 답변을 기준으로 해석하므로 서버 계약 확장 필요. 비교 탭과 A 이어가기만 연결하고 B 전환은 지원하지 않는다고 명시한다.
- 검색 중 비교 건수/진행률: 서버가 보낸 단계만 표시한다. 예시 1,284건은 넣지 않는다.
- 표 옆 주별 접수 차트: 날짜가 포함된 실제 이슈 목록에서만 파생 가능하다. 범위가 불확실한 근거 표에서 전체 추세처럼 생성하지 않는다. 기존 실제 공수/추세 차트를 유지한다.
- Mantis 외부 열기: 실제 source URL이 있을 때만 노출한다. URL을 추정하지 않는다.
- 플레이북 공개/관리자 사용자 변경은 기존 권한·API를 그대로 사용한다. 브라우저에서 실제 사용자의 권한이나 비밀번호는 바꾸지 않는다.

## 검증 집중 사항
- 긴 한글/이슈 ID/원문 줄바꿈, IME Enter, 입력창과 키보드·탭바 겹침.
- 부분 조회의 0/양수, 새로고침 실패 후 기존 데이터 유지, 오류 재시도.
- 동적 렌더링 후 포커스·툴팁·표 도구, 모달 Escape 및 포커스 복구.
- 관리자 비노출 상태, 플레이북 공개 권한, 기존 메뉴 접근.
- 초안 입력 중 생성 완료/중지와 대화 저장 보존.

## 진행 기록
- [x] PDF 23쪽 PNG 렌더링 및 전체 이미지 검토, 코드/API/현재 Chrome 조사.
- [x] 공통 스타일·대화.
- [x] 내 업무.
- [x] 대시보드·관리자·계정.
- [x] 자동·브라우저 검증 및 최종 차이 기록.

## 결과
- 실제 변경은 `design_system.css`와 `modules/ui_details.js`를 추가하고, `index.html`, `bootstrap.js`, `chat.js`, `chat_rendering.js`, `chat_source_detail.js`, `events.js`, `template_picker.js`, `workspace.js`, `dashboard.js`를 수정했다. 기존 CSS와 다른 사용자 변경은 기준본 그대로 유지했다.
- `check.sh tests compile shell ruff mypy` 통과. 후속 확인에서도 2,371개 테스트와 관련 Node 테스트 64개 통과, 커버리지 89%.
- Chrome 데스크톱 1512px, 태블릿 768px, 모바일 390px에서 검증했다. 상세 내용은 `docs/evaluations/2026-10-23-pdf-ui-verification.md`에 기록했다.
- 시안의 노트 추가·신규 배정 알림은 기존 API가 제공하지 않아 만들지 않았다. 현재 제공하는 일정 지연·장기 이슈 알림을 연결했다.
