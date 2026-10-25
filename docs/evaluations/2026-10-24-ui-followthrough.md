# UI 후속 구현과 검증 결과

2026-10-23~24. 사용자의 “순서대로해줘” 요청에 따라 이전 PDF UI 적용에서 남은 항목을 처리했다. 서브에이전트, 프레임워크 교체, 운영 사용자 권한 변경, 커밋, 원격 배포는 하지 않았다. 이전 화면별 기록은 [PDF UI 검증 보고서](2026-10-23-pdf-ui-verification.md)에 있다. 그 보고서의 B 후속 질문·알림·#31631 제한은 아래 결과로 갱신된다.

## 순서별 결과

| 순서 | 구현·검증 결과 | 주요 파일 |
|---|---|---|
| 1. #31631 차단 | `확인된 이슈`는 실제 `confirmed` 상태다. 출처 표기 `(#31631 · 필드 summary)`를 값에 포함해 비교한 오류를 수정했다. 현재 상태 질문은 조회 메타데이터 표를 사용하며 근거 없는 다음 조치는 만들지 않는다. | `evaluation/issue_facts.py`, `explanation_metadata.py`, `services/grounded_explanation.py` |
| 2. 계정 | 격리 DB와 실제 인증·CSRF·사용자 관리 코드를 사용해 로그인/로그아웃, member·manager의 관리자 접근 거부, admin 접근, 승인 취소의 즉시 반영을 검증했다. | `tests/test_auth_role_lifecycle.py` |
| 3. B 문맥 | `contextVariant`와 저장 메시지의 `followupVariant`를 추가했다. 서버의 동일 사용자·질문에 묶인 B 본문과 근거만 복원한다. 선택 저장/복원, B 첫 번째 이슈, 중지 후 재질문, 차단·실패·타인 기록·크기 제한을 처리한다. | `schemas.py`, `audit.py`, `services/answer_context.py`, `chat_service.py`, `unified_planning.py`, `intent/context.py`, `frontend_core.js`, `modules/chat*.js` |
| 4. 알림 | 현재 연결·담당 범위의 완전한 동일 세대 저장본을 비교해 새 배정·새 노트를 사용자별로 저장한다. 최초 관찰은 기준선이며 오래된/부분 자료로 이벤트를 만들지 않는다. 읽음 저장과 중복 방지를 연결했다. | `workspace_notifications.py`, `workspace.py`, `modules/workspace.js` |
| 5. 진행·차트 | 서버가 실제 보낸 단계만 완료 표시한다. 비교 단계도 표시하며 근거 이벤트의 실제 건수를 ‘답변에 사용할 원문 N건 확보’로 구분한다. 전체 검색 건수·백분율·주별 과거 잔량은 추정하지 않는다. | `services/chat_service.py`, `modules/chat_controller.js`, `chat_progress.js` |
| 6. 시각·전체 검증 | PDF 12쪽의 비교 카드를 재대조해 답변별 근거·복사·A 피드백을 해당 패널에 배치했다. 모바일 전송/중지 버튼의 클릭 중 위치 이동을 수정했다. | `design_system.css`, `responsive.css`, `modules/chat.js`, `events.js` |

## 실제 원본·회사 모델 확인

실제 서비스 설정과 사용자 정보를 읽어 기존 회사 모델을 호출했다. 결과/감사 기록은 이번 작업 전용 runtime 경로에 저장했다. 이 검증은 브라우저의 신규 Firebase 로그인 검증과는 별개다.

- `#31631 이슈의 현재 상태와 다음 할 일을 간단히 요약해줘`: 실제 상태 `확인된 이슈`, 다음 할 일은 근거 부족으로 명시. 구조 검사 7개 통과, `model_content_review=review`, `delivery=allowed`. 검토 필요 상태를 통과로 바꾸지 않았다.
- 실제 MTS 목록의 B 답변 근거는 #31680·#31668·#31666이었다. 이어서 ‘그중 첫 번째 이슈의 현재 상태’를 요청했을 때 #31680을 참조했고 구조·결정형 검사가 모두 통과했다.
- 원본 결과: `assistant_web/runtime/ui_followthrough_20261023/status-live-result.json`, `ab-live-first.json`, `ab-live-followup.json`. 회사 원문이 들어 있는 이 파일들은 gitignored runtime에만 남긴다.

## 실제 브라우저 확인

Chrome에서 기존 FastAPI 라우트·미들웨어·세션 쿠키·CSRF·SQLite 저장을 사용했다. 외부 Firebase/모델/원본 조회만 격리 테스트 대체를 사용하며 화면 하단에 이 사실을 표시했다. 실제 서비스는 `127.0.0.1:8787`, 격리 서버는 쿠키 호스트가 섞이지 않는 `localhost:8794`로 구분했다.

| 검증 | 결과 |
|---|---|
| B 선택 저장/복원 | B 선택 → 서버 저장 → 새로고침 → B 선택 유지. 원래 메시지 객체를 직접 바꿔 저장 변경이 감지되지 않던 문제를 재현·수정했다. |
| B 후속 질문·복사 | B의 #2002 참조를 서버 문맥으로 확인. 클립보드에도 B 본문과 표가 복사됨. A/B 탭 전환 정상. |
| 생성·중지 | 생성 중 초안 입력 및 Enter 중복 전송 방지. 중지 버튼 한 번 클릭으로 중단되고 초안 유지. 이후 재질문도 B #2002를 참조. |
| 모바일 클릭 | 수정 전 입력→버튼 포커스 이동으로 버튼 y=784→724, 전송되지 않음. 수정 후 y=784→784로 유지되며 한 번 클릭으로 전송됨. |
| 알림 | 새 배정·노트 2개 표시, 실제 확인 시각을 로컬 시간으로 표시. 모두 읽음 후 새로고침·재로그인에도 읽음 상태 유지. |
| 계정 | 로그아웃 확인창 → 실제 격리 로그아웃 API → 로그인 화면 → 재로그인 → 저장 대화 보존. 다크/라이트 전환 확인. |
| 반응형 | 1512×982, 768×1024, 390×844에서 A/B·알림 확인. 문서 가로 크기가 각각 1512/768/390px로 화면 너비와 같음. 모바일 표는 본문 내부, 입력창은 대화 스크롤과 분리됨. |

### 스크린샷

모두 `assistant_web/runtime/ui_followthrough_20261023/`의 **격리 검증 화면**이다. 예시 데이터가 운영 코드에 추가된 것은 아니다.

- [최종 A/B 데스크톱](../../assistant_web/runtime/ui_followthrough_20261023/ab-final-desktop.png)
- [A/B 태블릿](../../assistant_web/runtime/ui_followthrough_20261023/ab-final-tablet.png), [A/B 모바일](../../assistant_web/runtime/ui_followthrough_20261023/ab-final-mobile.png)
- [알림 데스크톱](../../assistant_web/runtime/ui_followthrough_20261023/notifications-desktop.png), [태블릿](../../assistant_web/runtime/ui_followthrough_20261023/notifications-tablet.png), [모바일](../../assistant_web/runtime/ui_followthrough_20261023/notifications-mobile.png)
- [모바일 생성 중](../../assistant_web/runtime/ui_followthrough_20261023/progress-mobile.png), [중지·초안 유지](../../assistant_web/runtime/ui_followthrough_20261023/stopped-mobile.png), [중지 후 B 재질문](../../assistant_web/runtime/ui_followthrough_20261023/ab-after-stop-mobile.png)
- [로그아웃 확인](../../assistant_web/runtime/ui_followthrough_20261023/logout-mobile.png), [로그인 화면](../../assistant_web/runtime/ui_followthrough_20261023/login-mobile.png)

## 최종 자동 검증

`assistant_web/scripts/check.sh` 전체 실행을 완료했다.

- tests: **2,395개 통과**, 커버리지 **89%**. 전체 Node 회귀와 JavaScript 구문 검사 포함.
- compile, shell, ruff: 통과.
- mypy: 159개 소스 파일 통과.
- audit: 저장소에 기존 등록된 예외 5개를 적용한 상태로 통과. 이번 작업에서 예외를 추가하거나 보안 설정을 완화하지 않았다.
- `git diff --check`: 통과.
- 최종 로그: `assistant_web/runtime/ui_followthrough_20261023/check-final-all.log`.
- 샌드박스 DNS 실패가 난 앞선 audit 시도는 실패로 보존하고, 최종 전체 검사는 네트워크 허용 환경에서 다시 실행했다.

## 남은 제약과 배포 준비

1. **외부 인증·실기기:** 실제 Firebase 신규 로그인은 테스트 계정 로그인 응답이 없어 완료하지 못했다. 격리 브라우저 검증은 외부 토큰 검증을 대체했다. 물리 기기의 소프트 키보드·네이티브 한글 IME는 미검증이며 조합 중 Enter 방지는 회귀 테스트로 확인했다.
2. **알림 수집 범위:** 업무 조회/새로고침 때 완전한 Mantis 저장본을 비교한다. 실시간 푸시나 서버 백그라운드 이벤트 구독이 아니므로, 두 관찰 사이에 배정 후 해제된 변경 등은 포착하지 못할 수 있다. 노트 수정/삭제 알림이나 Notion 이벤트는 추가하지 않았다. 이전 범위의 알림은 담당 해제/연결 변경 후 표시하지 않는다.
3. **진행·추세:** 전체 검색 분모가 없으므로 진행 백분율을 표시하지 않는다. 과거 시점별 미종료 잔량 차트에는 별도 이력 데이터가 필요하다. 기존 실제 집계 차트는 유지했다.
4. **피드백:** 기존 품질 피드백은 A 답변 감사 기록에 연결된 의미를 유지하며 A 패널 안에 배치했다. B에는 독립 복사와 후속 문맥 선택을 제공한다. B별 사용자 피드백 집계 정책은 추가하지 않았다.
5. **DB 마이그레이션:** workspace schema를 4→5로 올려 `workspace_observations`, `workspace_events`를 추가한다. 이전 코드의 migration runner는 버전 5를 거부하므로, 운영 배포 전 workspace DB 일관 백업과 롤백 절차가 필요하다. 운영 DB에 수동 마이그레이션하거나 원격 배포하지 않았다.
6. **변경 분리:** 시작 시 이미 여러 작업의 미커밋 변경이 있었다. 이 후속 작업의 코드/테스트 29개 파일만 시작 사본과 비교한 `followthrough-only.patch`, `changed-files.json`을 runtime에 준비했다. 이 패치는 현재 Git HEAD가 아닌 **이번 후속 작업 시작 상태**를 기준으로 하므로 독립 릴리스 패치로 오인하면 안 된다. 관련 없는 파일을 스테이징·커밋하지 않았다.

커밋할 때는 상태 검증 수정, B 문맥 기능, 업무 이벤트 알림, 진행/모바일 수정을 목적별로 나누고 기존 PDF UI 변경과의 의존성을 확인해야 한다. 예시 제목은 `fix: 상태 인용에 따른 잘못된 답변 차단 수정`, `feat: 비교 답변의 후속 질문 문맥 선택 추가`, `feat: 담당 업무의 새 배정과 노트 알림 추가`, `fix: 모바일 전송 버튼과 생성 단계 표시 수정`이다.
