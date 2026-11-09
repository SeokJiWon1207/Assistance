"""운영 DB를 사용하지 않는 빈 DB·구버전 마이그레이션 고정 사례."""
import json
import os
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import auth, audit, workspace, conversation_history, workspace_notifications
from backend.db_migrations import run_sqlite_migrations
from backend.storage import workspace_schema


def snapshot_database(path):
    with closing(sqlite3.connect(path)) as db:
        schema = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        tables = [r[1] for r in schema if r[0] == 'table']
        rows = {name: sorted(db.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr) for name in tables}
        return {'version': db.execute('PRAGMA user_version').fetchone()[0], 'schema': schema, 'rows': rows,
                'mode': stat.S_IMODE(path.stat().st_mode)}


def migration_cases():
    plans = {
        'users': [auth._create_users_schema, auth._add_password_change_fields, auth._add_password_reset_state],
        'audit': [audit._create_audit_schema, audit._add_validation_log_user_uid, audit._add_failed_checks_summary],
        'workspace': [workspace._create_workspace_schema, workspace._create_team_brief_schema, workspace._create_playbook_schema,
                      conversation_history.migrate_messages, workspace_notifications.create_notification_schema,
                      workspace_schema.create_conversation_trash_schema, workspace_schema.create_suggestion_schema],
    }
    records = []
    with tempfile.TemporaryDirectory() as directory:
        for owner, migrations in plans.items():
            for version in range(len(migrations) + 1):
                path = Path(directory) / f'{owner}-{version}.sqlite3'
                if version:
                    run_sqlite_migrations(path, migrations[:version], database_name=owner)
                    with closing(sqlite3.connect(path)) as db, db:
                        if owner == 'users':
                            db.execute("INSERT INTO users(firebase_uid,email,created_at,updated_at) VALUES('fixture-user','fixture@example.test','2026-01-01','2026-01-01')")
                        elif owner == 'audit':
                            db.execute("INSERT INTO validation_logs(id,created_at,question,model_id,mode,score,status,payload) VALUES(?,?,?,?,?,?,?,?)",
                                       ('audit-fixture','2026-01-01','고정 질문','fixture','model',100,'pass','{}'))
                        else:
                            messages = [{'role': 'user', 'content': '첫 질문'}, {'role': 'assistant', 'content': '고정 답변'}]
                            for uid in ('user-a', 'user-b'):
                                db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?)",
                                           (uid,'same-id','고정 대화',json.dumps(messages,ensure_ascii=False),1,'2026-01-01','2026-01-01'))
                            db.execute("INSERT INTO notification_reads VALUES('user-a','notification-1','2026-01-01')")
                            if version >= 2:
                                db.execute("INSERT INTO weekly_team_briefs VALUES('2026-01-05','{}','2026-01-01')")
                                db.execute("INSERT INTO weekly_team_brief_reads VALUES('user-a','2026-01-05','2026-01-01')")
                            if version >= 3:
                                db.execute("INSERT INTO resolution_playbooks(id,title,symptom,resolution,created_by_uid,created_at,updated_at) VALUES('fixture','고정 지식','증상','해결','user-a','2026-01-01','2026-01-01')")
                with patch.dict(os.environ, {'AUTH_USERS_DB_PATH': str(path), 'WORKSPACE_DB_PATH': str(path)}), patch.object(audit,'AUDIT_DB_PATH',path):
                    initialize = {'users': auth.init_users_db, 'audit': audit.init_audit_db, 'workspace': workspace.init_workspace_db}[owner]
                    initialize()
                    first = snapshot_database(path)
                    initialize()
                    assert snapshot_database(path) == first
                records.append({'owner': owner, 'from_version': version, 'database': first})
    return json.loads(json.dumps(records, ensure_ascii=False))
