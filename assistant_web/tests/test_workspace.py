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
        notion = {
            "status": "ok",
            "notion_pages": [
                {
                    "id": "page-1",
                    "title": "검토 업무",
                    "status": "진행중",
                    "owner": "석지원",
                    "due_date": overdue,
                    "last_edited_time": recent,
                    "url": "https://notion.so/page-1",
                    "database_label": "업무",
                }
            ],
        }
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
        ), patch.object(
            workspace, "_my_notion_pages", return_value=notion
        ):
            result = workspace.build_my_workspace(user)

        self.assertEqual(result["summary"]["assigned"], 2)
        self.assertEqual(result["summary"]["open"], 1)
        self.assertEqual(result["summary"]["overdue"], 1)
        self.assertEqual(result["summary"]["aging"], 1)
        self.assertEqual(result["summary"]["notion"], 1)
        self.assertEqual(result["summary"]["unreadNotifications"], 2)
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
