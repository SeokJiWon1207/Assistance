"""관리자가 요청한 계정과 사용자 소유 DB 레코드의 영구 삭제."""
from contextlib import closing

from fastapi import HTTPException

from backend import audit, auth
from backend.auth_passwords import password_operation
from backend.storage.profiles import AUTH
from backend.storage.sqlite import open_sqlite
from backend.storage.workspace_schema import init_workspace_db, workspace_db_path
from backend.user_data_lock import user_data_operation


def _delete_local_data(uid: str) -> None:
    init_workspace_db()
    audit.init_audit_db()
    with closing(open_sqlite(auth.users_db_path(), profile=AUTH)) as db:
        db.execute('ATTACH DATABASE ? AS workspace', (str(workspace_db_path()),))
        db.execute('ATTACH DATABASE ? AS audit', (str(audit.AUDIT_DB_PATH),))
        with db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('conversation_messages', 'conversations',
                          'notification_reads', 'weekly_team_brief_reads',
                          'workspace_observations', 'workspace_events'):
                db.execute(f'DELETE FROM workspace.{table} WHERE user_uid = ?', (uid,))
            # 휴지통 기능이 아직 도입되지 않은 DB에서도 계정을 삭제할 수 있다.
            if db.execute("SELECT 1 FROM workspace.sqlite_master WHERE type = 'table' "
                          "AND name = 'conversation_trash'").fetchone():
                db.execute('DELETE FROM workspace.conversation_trash WHERE user_uid = ?', (uid,))
            db.execute('DELETE FROM workspace.resolution_playbooks WHERE created_by_uid = ?', (uid,))
            db.execute('DELETE FROM audit.answer_feedback WHERE audit_id IN '
                       '(SELECT id FROM audit.validation_logs WHERE user_uid = ?)', (uid,))
            db.execute('DELETE FROM audit.validation_logs WHERE user_uid = ?', (uid,))
            db.execute('DELETE FROM users WHERE firebase_uid = ?', (uid,))


def delete_user_and_data(uid: str, confirmation_email: str) -> None:
    try:
        with user_data_operation(uid, auth.users_db_path(), exclusive=True):
            _delete_idle_user(uid, confirmation_email)
    except BlockingIOError as exc:
        raise HTTPException(status_code=409, detail='사용자의 요청을 처리 중입니다. 잠시 후 삭제를 다시 시도해 주세요.') from exc


def _delete_idle_user(uid: str, confirmation_email: str) -> None:
    # 비밀번호 변경/초기화와 같은 잠금을 사용해 계정 변경이 서로 덮어쓰지 않게 한다.
    with password_operation(uid):
        user = auth.get_user_by_uid(uid)
        if not user:
            raise HTTPException(status_code=404, detail='사용자를 찾지 못했습니다.')
        if confirmation_email.strip().casefold() != user['email']:
            raise HTTPException(status_code=400, detail='삭제할 사용자의 이메일을 정확히 입력해 주세요.')
        try:
            # 이후 단계가 실패해도 계정 행은 남겨 관리자가 삭제를 재시도할 수 있다.
            auth.update_user_access(uid, approved=False)
        except Exception as exc:
            raise HTTPException(status_code=503, detail='계정을 차단하지 못해 삭제를 시작하지 않았습니다. 다시 시도해 주세요.') from exc
        try:
            firebase = auth._firebase_auth_module()
            try:
                firebase.delete_user(uid)
            except firebase.UserNotFoundError:
                # 이전 요청이 Firebase 삭제까지만 끝났거나 외부에서 삭제된 경우.
                pass
        except Exception as exc:
            raise HTTPException(status_code=502, detail='인증 계정을 삭제하지 못했습니다. 로그인은 차단되었으며 데이터는 유지됩니다. 삭제를 다시 시도해 주세요.') from exc
        try:
            _delete_local_data(uid)
        except Exception as exc:
            raise HTTPException(status_code=503, detail='인증 계정은 삭제했지만 DB 정리를 완료하지 못했습니다. 사용자 삭제를 다시 시도해 주세요.') from exc
