import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import workspace


class ConversationWorkspaceTests(unittest.TestCase):
    def test_conversations_are_scoped_and_favorites_sort_first(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ):
            workspace.save_conversation(
                "user-a", "chat-1", "일반 대화", [{"role": "user", "content": "질문"}], False
            )
            workspace.save_conversation(
                "user-a", "chat-2", "중요 대화", [{"role": "assistant", "content": "답"}], True
            )
            workspace.save_conversation(
                "user-b", "chat-3", "다른 사용자", [], False
            )

            conversations = workspace.list_conversations("user-a")

            self.assertEqual([item["id"] for item in conversations], ["chat-2", "chat-1"])
            self.assertTrue(conversations[0]["favorite"])
            self.assertNotIn("chat-3", [item["id"] for item in conversations])
            self.assertTrue(workspace.delete_conversation("user-a", "chat-1"))
            self.assertFalse(workspace.delete_conversation("user-b", "chat-1"))

    def test_non_favorite_save_is_returned_after_forty_favorites(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ):
            for index in range(40):
                workspace.save_conversation(
                    "user-a", f"favorite-{index}", f"즐겨찾기 {index}", [], True
                )

            saved = workspace.save_conversation(
                "user-a", "ordinary", "일반 대화", [], False
            )

        self.assertEqual(saved["id"], "ordinary")
        self.assertFalse(saved["favorite"])

    def test_legacy_message_rows_remain_readable_and_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ):
            workspace.init_workspace_db()
            with workspace.closing(workspace.sqlite3.connect(workspace.workspace_db_path())) as connection, connection:
                connection.execute(
                    """
                    INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "user-a",
                        "legacy",
                        "예전 대화",
                        '[{"role":"assistant","content":"답변","unknown":"kept-before",'
                        '"sources":[{"type":"mantis","issue_id":30072,"title":"이슈",'
                        '"detail":{"description":"설명","private_blob":"drop"}}]}]',
                        0,
                        "2026-01-01",
                        "2026-01-01",
                    ),
                )
            loaded = workspace.list_conversations("user-a")

        self.assertEqual(loaded[0]["messages"][0]["content"], "답변")
        self.assertEqual(loaded[0]["messages"][0]["sources"][0]["issue_id"], 30072)
        self.assertNotIn("unknown", loaded[0]["messages"][0])
        self.assertNotIn("private_blob", loaded[0]["messages"][0]["sources"][0]["detail"])

    def test_user_conversation_count_quota_rejects_new_rows_but_allows_updates(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3"),
                "CONVERSATION_MAX_PER_USER": "2",
            },
        ):
            workspace.save_conversation("user-a", "one", "대화 1", [], False)
            workspace.save_conversation("user-a", "two", "대화 2", [], False)
            workspace.save_conversation("user-a", "one", "수정", [], True)
            with self.assertRaises(workspace.ConversationQuotaError):
                workspace.save_conversation("user-a", "three", "대화 3", [], False)


class PersonalWorkspaceTests(unittest.TestCase):
    def test_mantis_workspace_uses_live_data_and_filters_authenticated_handler(self):
        raw_issues = [
            {"id": 1, "handler": "석지원"},
            {"id": 2, "handler": "다른 담당자"},
        ]
        with patch.object(
            workspace,
            "fetch_workspace_recent_issues",
            return_value=raw_issues,
        ), patch.object(workspace, "normalize_issue", side_effect=lambda issue: issue):
            person, issues, meta = workspace._my_mantis_issues(
                {"mantisUsername": "석지원"}
            )

        self.assertEqual(person, "석지원")
        self.assertEqual([issue["id"] for issue in issues], [1])
        self.assertEqual(meta, {"status": "live", "matchedCount": 1})

    def test_default_workspace_uses_snapshot_without_waiting_for_live_data(self):
        snapshot_issues = [
            {"id": 1, "handler": "석지원"},
            {"id": 2, "handler": "다른 담당자"},
        ]
        with patch.object(
            workspace,
            "load_workspace_snapshot",
            return_value=(snapshot_issues, {"status": "snapshot", "matchedCount": 2}),
        ), patch.object(workspace, "fetch_workspace_recent_issues") as fetch_live:
            person, issues, meta = workspace._my_mantis_issues(
                {"mantisUsername": "석지원"}, prefer_snapshot=True
            )

        fetch_live.assert_not_called()
        self.assertEqual(person, "석지원")
        self.assertEqual([issue["id"] for issue in issues], [1])
        self.assertEqual(meta, {"status": "snapshot", "matchedCount": 1})

    def test_mantis_workspace_falls_back_to_snapshot_after_live_failure(self):
        snapshot_issue = {"id": 10, "handler": "석지원"}
        with patch.object(
            workspace,
            "fetch_workspace_recent_issues",
            side_effect=TimeoutError("slow"),
        ), patch.object(
            workspace,
            "scan_mantis_documents",
            return_value={
                "available": True,
                "matched_count": 1,
                "issues": [snapshot_issue],
                "reason": "live timeout",
            },
        ) as scan:
            person, issues, meta = workspace._my_mantis_issues(
                {"displayName": "석지원"}
            )

        self.assertEqual((person, issues), ("석지원", [snapshot_issue]))
        self.assertEqual(
            meta,
            {"status": "snapshot", "matchedCount": 1, "reason": "live timeout"},
        )
        self.assertTrue(scan.call_args.kwargs["matches"]("담당자: 석지원"))

    def test_notion_workspace_reports_missing_identity_and_connection(self):
        self.assertEqual(
            workspace._my_notion_pages({"displayName": "user@example.com"})["status"],
            "skipped",
        )
        with patch.object(
            workspace,
            "get_notion_settings",
            return_value={"token": "", "database_ids": []},
        ):
            result = workspace._my_notion_pages({"displayName": "석지원"})
        self.assertEqual(result["status"], "skipped")
        self.assertIn("연결 정보", result["reason"])

    def test_notion_workspace_collects_pages_and_reports_partial_database_error(self):
        success = Mock(status_code=200)
        success.json.return_value = {
            "results": [
                {"id": "page-1", "last_edited_time": "2026-08-24T10:00:00Z"}
            ]
        }
        failure = Mock(status_code=503)

        with patch.object(
            workspace,
            "get_notion_settings",
            return_value={"token": "token", "database_ids": ["db-1", "db-2"]},
        ), patch.object(
            workspace,
            "fetch_notion_users",
            return_value=[("석지원", "user-1")],
        ), patch.object(
            workspace,
            "fetch_notion_database_schema",
            return_value={"properties": {"담당자": {"type": "people"}}},
        ), patch.object(
            workspace,
            "fetch_notion_database_title",
            side_effect=["업무 DB", "오류 DB"],
        ), patch.object(
            workspace,
            "notion_post_json",
            side_effect=[success, failure],
        ) as post, patch.object(
            workspace,
            "normalize_notion_page",
            return_value={
                "id": "page-1",
                "title": "업무 공유",
                "last_edited_time": "2026-08-24T10:00:00Z",
            },
        ):
            result = workspace._my_notion_pages({"displayName": "석지원"})

        self.assertEqual(result["status"], "partial_error")
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["page-1"])
        self.assertEqual(result["reason"], "오류 DB: 503")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs, {"timeout": 10, "retries": 0})

    def test_open_status_excludes_resolved_and_closed_but_keeps_developed(self):
        for status in ("개발완료", "developed", "확인된 이슈"):
            with self.subTest(status=status):
                self.assertTrue(workspace._is_open(status))
        for status in (
            "해결된 이슈",
            "resolved",
            "MTS-4 해결 및 종료",
            "종료된 이슈",
            "closed",
        ):
            with self.subTest(status=status):
                self.assertFalse(workspace._is_open(status))

    def test_my_workspace_builds_summary_notifications_and_weekly_brief(self):
        today = workspace.date.today()
        old = (today - workspace.timedelta(days=45)).isoformat()
        recent = (today - workspace.timedelta(days=2)).isoformat()
        overdue = (today - workspace.timedelta(days=1)).isoformat()
        issues = [
            {
                "id": 101,
                "summary": "오래된 담당 이슈",
                "project": "고객사",
                "status": "할당된 이슈",
                "priority": "높음",
                "handler": "석지원",
                "created_at": old,
                "updated_at": recent,
                "date_fields": {"완료예정일": overdue},
            },
            {
                "id": 102,
                "summary": "종료 이슈",
                "project": "고객사",
                "status": "종료된 이슈",
                "priority": "보통",
                "handler": "석지원",
                "created_at": recent,
                "updated_at": recent,
                "date_fields": {},
            },
        ]
        user = {
            "uid": "uid-1",
            "email": "user@example.com",
            "displayName": "석지원",
            "mantisUsername": "석지원",
            "team": "컨텐츠서비스팀",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ), patch.object(
            workspace,
            "_my_mantis_issues",
            return_value=("석지원", issues, {"status": "live", "matchedCount": 2}),
        ):
            result = workspace.build_my_workspace(user)

        self.assertEqual(result["summary"]["assigned"], 2)
        self.assertEqual(result["summary"]["open"], 1)
        self.assertEqual(result["summary"]["overdue"], 1)
        self.assertEqual(result["summary"]["aging"], 1)
        self.assertEqual([issue["id"] for issue in result["mantis"]["issues"]], [101])
        self.assertNotIn("notion", result["summary"])
        self.assertEqual(result["notion"]["status"], "hidden")
        self.assertEqual(result["summary"]["unreadNotifications"], 1)
        self.assertEqual(result["weeklyBrief"]["newIssues"], 1)

    def test_personal_issue_list_prioritizes_overdue_then_aging_work(self):
        today = workspace.date.today()
        issues = [
            {
                "id": 201,
                "summary": "일반 업무",
                "project": "고객사",
                "status": "확인된 이슈",
                "handler": "석지원",
                "created_at": (today - workspace.timedelta(days=2)).isoformat(),
                "updated_at": today.isoformat(),
                "date_fields": {"완료예정일": (today + workspace.timedelta(days=1)).isoformat()},
            },
            {
                "id": 202,
                "summary": "장기 업무",
                "project": "고객사",
                "status": "확인된 이슈",
                "handler": "석지원",
                "created_at": (today - workspace.timedelta(days=45)).isoformat(),
                "updated_at": today.isoformat(),
                "date_fields": {"완료예정일": (today + workspace.timedelta(days=2)).isoformat()},
            },
            {
                "id": 203,
                "summary": "지연 업무",
                "project": "고객사",
                "status": "확인된 이슈",
                "handler": "석지원",
                "created_at": (today - workspace.timedelta(days=3)).isoformat(),
                "updated_at": today.isoformat(),
                "date_fields": {"완료예정일": (today - workspace.timedelta(days=1)).isoformat()},
            },
        ]
        user = {
            "uid": "uid-order",
            "email": "user@example.com",
            "displayName": "석지원",
            "mantisUsername": "석지원",
            "team": "컨텐츠서비스팀",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ), patch.object(
            workspace,
            "_my_mantis_issues",
            return_value=("석지원", issues, {"status": "live", "matchedCount": 3}),
        ):
            result = workspace.build_my_workspace(user)

        self.assertEqual(
            [issue["id"] for issue in result["mantis"]["issues"]],
            [203, 202, 201],
        )

    def test_notification_read_state_is_per_user(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ):
            workspace.mark_notification_read("uid-a", "mantis-aging-101")
            issues = [
                {
                    "id": 101,
                    "summary": "장기 이슈",
                    "project": "고객사",
                    "status": "할당된 이슈",
                    "handler": "사용자",
                    "created_at": (workspace.date.today() - workspace.timedelta(days=40)).isoformat(),
                    "updated_at": "",
                    "date_fields": {},
                }
            ]
            read = workspace._notifications("uid-a", issues, [])[0]
            unread = workspace._notifications("uid-b", issues, [])[0]

        self.assertTrue(read["read"])
        self.assertFalse(unread["read"])


class TeamDashboardTests(unittest.TestCase):
    @patch.object(workspace, "fetch_workspace_recent_issues")
    def test_team_dashboard_groups_by_approved_user_mapping(self, fetch_issues):
        fetch_issues.return_value = [
            {
                "id": 11,
                "summary": "담당 이슈",
                "project": {"name": "고객사"},
                "status": {"name": "할당된 이슈"},
                "priority": {"name": "높음"},
                "handler": {"real_name": "석지원"},
                "reporter": {"real_name": "요청자"},
                "created_at": "2026-01-01",
                "updated_at": "2026-07-28",
                "custom_fields": [],
            }
        ]
        users = [
            {
                "uid": "uid-1",
                "displayName": "석지원",
                "email": "user@example.com",
                "mantisUsername": "석지원",
                "approved": True,
                "role": "member",
            },
            {
                "uid": "uid-2",
                "displayName": "비활성",
                "email": "off@example.com",
                "mantisUsername": "비활성",
                "approved": False,
                "role": "member",
            },
        ]

        result = workspace.build_team_dashboard(users)

        self.assertEqual(result["summary"]["members"], 1)
        self.assertEqual(result["members"][0]["open"], 1)
        self.assertEqual(result["members"][0]["displayName"], "석지원")


if __name__ == "__main__":
    unittest.main()
