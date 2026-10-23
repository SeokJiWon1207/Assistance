"""완전한 조회 사이에서 실제 관찰된 배정·노트만 사용자별 알림으로 보존한다."""
import os
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from backend import workspace
from backend import workspace_notifications as notifications


class WorkspaceNotificationTests(TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "workspace.sqlite3"
        self.enterContext(patch.dict(os.environ, {"WORKSPACE_DB_PATH": str(self.path)}))
        workspace.init_workspace_db()

    def observe(self, rows, day, *, user="u1", scope="person:source", complete=True):
        from backend.workspace_notifications import observe_workspace_changes
        return observe_workspace_changes(self.path, user, scope, rows,
                                         f"2026-10-{day:02}T10:00:00+00:00", complete=complete)

    def test_first_view_is_baseline_then_new_assignment_is_persisted_once(self):
        first = {1: {"title": "기존 업무", "notes": {}}}
        self.assertEqual(self.observe(first, 20), [])
        second = {**first, 2: {"title": "새 담당 업무", "notes": {}}}
        events = self.observe(second, 21)
        self.assertEqual([(e["type"], e["issueId"]) for e in events], [("assigned", 2)])
        self.assertEqual([e["id"] for e in self.observe(second, 21)], [e["id"] for e in events])
        workspace.mark_notification_read("u1", events[0]["id"])
        notices = workspace._notifications("u1", [], [], observed_events=events)
        self.assertTrue(notices[0]["read"])

    def test_note_id_change_creates_note_event_without_copying_body(self):
        self.observe({1: {"title": "업무", "notes": {"10": {"createdAt": "2026-10-19"}}}}, 20)
        rows = {1: {"title": "업무", "notes": {"10": {"createdAt": "2026-10-19"},
                    "11": {"createdAt": "2026-10-21", "reporter": "담당자"}}}}
        events = self.observe(rows, 21)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "note")
        self.assertEqual(events[0]["noteId"], 11)
        self.assertIn("담당자", events[0]["description"])
        self.assertEqual(len(self.observe(rows, 22)), 1)

    def test_partial_stale_and_changed_account_never_create_false_assignment(self):
        rows = {1: {"title": "업무", "notes": {}}}
        self.observe(rows, 20)
        self.assertEqual(self.observe({}, 21, complete=False), [])
        self.assertEqual(self.observe(rows, 22), [])
        self.assertEqual(self.observe({**rows, 2: {"title": "옛 자료", "notes": {}}}, 19), [])
        self.assertEqual(self.observe({2: {"title": "다른 연결", "notes": {}}}, 23, scope="other"), [])

    def test_other_user_has_independent_baseline_and_cannot_read_events(self):
        self.observe({}, 20)
        rows = {1: {"title": "업무", "notes": {}}}
        self.assertEqual(len(self.observe(rows, 21)), 1)
        self.assertEqual(self.observe(rows, 21, user="u2"), [])

    def test_preexisting_notes_on_first_assignment_are_not_new_notes(self):
        self.observe({}, 20)
        events = self.observe({1: {"title": "업무", "notes": {"1": {"createdAt": "2025-01-01"}}}}, 21)
        self.assertEqual([event["type"] for event in events], ["assigned"])

    def test_unknown_note_scope_does_not_turn_old_notes_into_new_events(self):
        self.observe({1: {"title": "업무", "notes": None}}, 20)
        events = self.observe({1: {"title": "업무", "notes": {"1": {"createdAt": "2025-01-01"},
                  "2": {"createdAt": "2026-10-21T01:00:00+00:00"}}}}, 21)
        self.assertEqual([event["noteId"] for event in events], [2])

    def test_assignment_removal_hides_previously_saved_notifications(self):
        self.observe({}, 20)
        self.assertEqual(len(self.observe({1: {"title": "이전 업무", "notes": {}}}, 21)), 1)
        self.assertEqual(self.observe({}, 22), [])


class NotificationSourceTests(TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / "source.sqlite3"
        self.stamp = datetime.now(timezone.utc).isoformat()
        self.meta = {"search_complete": True, "snapshot_complete": True, "fetchedAt": self.stamp}
        self.enterContext(patch.object(notifications, "store_path", return_value=self.path))
        self.enterContext(patch.object(notifications, "connection_key", return_value="same-source"))
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE metadata(value TEXT)")
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps({"connection": "same-source", "fetchedAt": self.stamp}),))
            db.execute("CREATE TABLE issues(id INTEGER PRIMARY KEY, payload TEXT)")
            db.execute("INSERT INTO issues VALUES (1, ?)", (json.dumps({"notes": [
                {"id": 12, "text": "저장하면 안 되는 원문", "created_at": self.stamp,
                 "reporter": {"real_name": "작성자"}}, None]}),))

    def read(self):
        return notifications.read_notification_facts([{"id": 1, "summary": "내 업무"}], self.meta)

    def test_source_reads_only_note_identifiers_from_same_complete_generation(self):
        identity, facts = self.read()
        self.assertEqual(identity, "same-source")
        self.assertEqual(facts[1]["notes"], {"12": {"createdAt": self.stamp, "reporter": "작성자"}})
        self.assertNotIn("저장하면 안 되는 원문", json.dumps(facts, ensure_ascii=False))

    def test_partial_different_connection_and_rotated_generation_are_not_compared(self):
        self.meta["search_complete"] = False
        self.assertIsNone(self.read())
        self.meta["search_complete"] = True
        with patch.object(notifications, "connection_key", return_value="other-source"):
            self.assertIsNone(self.read())
        self.meta["fetchedAt"] = "2020-01-01T00:00:00Z"
        self.assertIsNone(self.read())

    def test_stale_source_and_missing_row_do_not_create_empty_baseline(self):
        self.assertIsNone(notifications.read_notification_facts([{"id": 2}], self.meta))
        old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE metadata SET value=?", (json.dumps({"connection": "same-source", "fetchedAt": old}),))
        self.meta["fetchedAt"] = old
        self.assertIsNone(self.read())
