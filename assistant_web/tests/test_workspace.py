import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    @patch.object(workspace, "fetch_mantis_recent_issues")
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
