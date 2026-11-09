import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import playbooks, workspace


class ResolutionPlaybookTests(unittest.TestCase):
    def test_draft_can_be_curated_published_searched_and_archived(self):
        user = {"uid": "manager-1", "displayName": "검수자"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ):
            created = playbooks.create_playbook(
                {
                    "title": "세션 만료 대응",
                    "symptom": "1200 화면에서 세션이 조기 만료됨",
                    "cause": "기본 타임아웃 설정",
                    "resolution": "타임아웃을 30분으로 조정",
                    "verification": "30분 동안 세션 유지 확인",
                    "tags": ["MTS", "1200", "MTS"],
                    "sourceIssueIds": [12345, "#12346", 12345],
                    "status": "draft",
                },
                user,
            )

            self.assertEqual(created["status"], "draft")
            self.assertEqual(created["tags"], ["MTS", "1200"])
            self.assertEqual(created["sourceIssueIds"], [12345, 12346])
            self.assertEqual(playbooks.list_playbooks("세션"), [])
            self.assertEqual(
                len(playbooks.list_playbooks("1200", include_unpublished=True)), 1
            )

            published = playbooks.update_playbook(
                created["id"], {"status": "published", "resolution": "설정 변경 후 재기동"}
            )
            self.assertEqual(published["status"], "published")
            self.assertTrue(published["publishedAt"])
            self.assertEqual(playbooks.list_playbooks("12346")[0]["id"], created["id"])

            archived = playbooks.update_playbook(created["id"], {"status": "archived"})
            self.assertEqual(archived["status"], "archived")
            self.assertEqual(playbooks.list_playbooks("세션"), [])

    def test_search_by_suggested_customer_name_finds_legacy_project_tags(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")}
        ):
            payload = {"title": "로그인 대응", "symptom": "세션 만료", "resolution": "설정 확인"}
            saved = playbooks.create_playbook(
                {**payload, "tags": ["132SHGM_Project"], "status": "published"}, {}
            )
            playbooks.create_playbook({**payload, "tags": ["132SHGM"], "status": "draft"}, {})
            playbooks.create_playbook({**payload, "tags": ["098Itas"], "status": "published"}, {})

            playbooks.create_playbook({**payload, "tags": ["KyoboSDK"], "status": "published"}, {})
            self.assertEqual(playbooks.list_playbooks("교보증권"), [])

            result = playbooks.list_playbooks("신한투자증권")
            self.assertEqual([entry["id"] for entry in result], [saved["id"]])
            self.assertEqual(result[0]["tags"], ["132SHGM_Project"])

    def test_workspace_migration_keeps_playbooks_with_notifications_version_five(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ):
            workspace.init_workspace_db()
            with workspace.closing(
                workspace.sqlite3.connect(workspace.workspace_db_path())
            ) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'resolution_playbooks'"
                ).fetchone()

        # 7번째 버전(개인 업무 추천)이 더해져도 플레이북 테이블은 그대로 남는다.
        self.assertEqual(version, 7)
        self.assertEqual(table[0], "resolution_playbooks")


if __name__ == "__main__":
    unittest.main()
