# 대화 목록 일괄 삭제

2026-10-26. 대화 목록의 ‘관리’에서 선택 삭제와 전체 삭제를 제공한다.

## 동작

- 체크박스 또는 행을 눌러 대화를 선택한다. 불러온 대화 모두 선택은 현재 검색 결과에 적용한다.
- 검색으로 숨겨진 선택도 선택 개수에 포함하며 ‘검색 밖 N개’로 표시한다. 확인 창에 선택한 제목을 최대 3개 보여 준다.
- 전체 삭제는 즐겨찾기와 목록에 아직 불러오지 않은 서버 기록, 이 기기의 미저장 기록을 포함한다. 현재 로그인 사용자에게만 적용한다.
- 삭제 확인 창은 취소에 먼저 포커스를 두고 복구 불가를 안내한다. 삭제 중 중복 요청·새 전송·대화 이동을 차단한다.
- 서버는 대화와 메시지를 한 트랜잭션에서 삭제한다. API 인증과 CSRF 검사를 유지한다. 새 DB 스키마와 보관 정책 변경은 없다.
- 삭제할 대화의 대기 저장을 취소하고 진행 중 저장이 끝난 뒤 삭제한다. 캐시 쓰기를 직렬화해 오래된 쓰기가 삭제 뒤에 기록을 되살리지 않게 한다.
- 서버 요청이 실패하면 기기의 대화·활성 대화·메시지를 복원한다. 모두 삭제되면 서버에 저장되지 않은 빈 새 대화로 시작한다.

## 구현

- 서버: `backend/conversation_history.py`, `backend/workspace.py`, `backend/api/workspace.py`
- UI: `static/modules/conversation_bulk.js`, `workspace.js`, `events.js`, `runtime.js`, `bootstrap.js`, `chat.js`, `chat_controller.js`
- 화면: `static/index.html`, `conversation_list.css`, `design_system.css`

`POST /api/workspace/conversations/bulk-delete`에 선택한 `ids` 또는 `all: true`를 전달한다. 빈 범위·잘못된 ID·5,000개 초과·전체/선택 혼합 요청은 422다. 삭제한 ID와 개수를 반환하며 다른 사용자 기록의 존재를 공개하지 않는다.

## 검증

- Python: 사용자별 선택·전체 삭제, 즐겨찾기 포함, 105개 기록 전체 삭제, 메시지 행 정리, 트랜잭션 중단 원복, 잘못된 요청 거절, 인증과 CSRF.
- Node: 실행 중 PUT 완료 후 삭제, 지연 저장 취소, 선택 외 기록 유지, 전체 삭제 후 빈 대화, 실패 원복, 계정 전환 후 늦은 응답 격리, 이전 비동기 캐시 쓰기 완료 후에도 삭제 상태 유지.
- UI 테스트: 확인 전 요청 없음, 취소 시 요청 없음, 중복 확인 요청 방지, 검색 밖 선택 표시와 대상 제목 안내.
- 실제 Chrome: 격리 테스트 대화 13개로 관리 모드·선택·검색·모두 선택·삭제 확인·취소·포커스 확인. 390×844에서 문서 폭 390px, 모달 폭 358px. 모바일 확인 창이 사이드바보다 앞에 표시됨을 확인했다. 브라우저 오류 없음.
- `assistant_web/scripts/check.sh` 전체(tests compile shell ruff mypy audit) 통과: 2,407개 Python 테스트(Node 회귀 포함), 커버리지 89%, mypy 159개 파일. 기존 보안 예외 5개 유지, 새 예외 없음.
- `git diff --check` 통과.

실제 삭제는 격리 SQLite/API와 프런트엔드 자동 테스트로 확인했다. 브라우저 시각 검증에서는 삭제 확인 후 취소했으며 운영 사용자의 대화는 삭제하지 않았다.

## 캡처와 로그

- [데스크톱 관리 모드](../../assistant_web/runtime/bulk_delete_20261026/management-desktop.jpg)
- [모바일 관리 모드](../../assistant_web/runtime/bulk_delete_20261026/management-mobile.jpg)
- [모바일 전체 삭제 확인](../../assistant_web/runtime/bulk_delete_20261026/all-confirm-mobile.jpg)
- 검사 로그: `assistant_web/runtime/bulk_delete_20261026/check-release.log`
