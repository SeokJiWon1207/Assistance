"""내 업무 첫 화면의 30일 신규 등록 추이와 직전 7일 비교."""
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from backend import workspace


class WorkspaceActivityTests(TestCase):
    def build(self, issues):
        user = {"uid": "activity-user", "displayName": "확인용", "email": "a@example.test", "mantisUsername": "확인용"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")},
        ), patch.object(workspace, "_my_mantis_issues", return_value=("확인용", issues, {"status": "live", "matchedCount": len(issues)})):
            return workspace.build_my_workspace(user)

    def issue(self, issue_id, days_ago, status="assigned"):
        created = (workspace.date.today() - timedelta(days=days_ago)).isoformat()
        return {"id": issue_id, "summary": f"이슈 {issue_id}", "project": "고객사", "status": status,
                "handler": "확인용", "created_at": f"{created} 09:00:00", "updated_at": f"{created} 09:00:00"}

    def test_activity_counts_registrations_including_closed_issues_by_created_date(self):
        result = self.build([
            self.issue(1, 0), self.issue(2, 3, status="closed"), self.issue(3, 6),
            self.issue(4, 7), self.issue(5, 13, status="resolved"), self.issue(6, 14), self.issue(7, 40),
        ])
        activity = result["activity"]
        today = workspace.date.today()
        self.assertEqual(activity["periodTo"], today.isoformat())
        self.assertEqual(activity["periodFrom"], (today - timedelta(days=workspace.ACTIVITY_DAYS - 1)).isoformat())
        self.assertEqual(len(activity["points"]), workspace.ACTIVITY_DAYS)
        self.assertEqual(activity["points"][-1], {"date": today.isoformat(), "count": 1})
        # 30일 밖(40일 전) 등록은 추이에 넣지 않는다. 종료된 이슈도 등록 건수에는 포함한다.
        self.assertEqual(sum(point["count"] for point in activity["points"]), 6)
        # 최근 7일(오늘~6일 전)과 그 앞 7일(7~13일 전)을 같은 길이로 비교한다.
        self.assertEqual((activity["recentNew"], activity["previousNew"]), (3, 2))
        self.assertEqual(activity["recentNew"], result["weeklyBrief"]["newIssues"])
        self.assertEqual(activity["dateBasis"], "등록일")

    def test_activity_skips_issues_without_a_readable_created_date(self):
        broken = self.issue(9, 1)
        broken["created_at"] = "미상"
        activity = self.build([broken])["activity"]
        self.assertEqual(sum(point["count"] for point in activity["points"]), 0)
        self.assertEqual((activity["recentNew"], activity["previousNew"]), (0, 0))

    def test_recent_closed_is_separate_from_open_and_uses_last_updated_date(self):
        old = self.issue(3, 31, status="closed")
        missing = self.issue(4, 1, status="closed")
        missing["updated_at"] = "미상"
        result = self.build([self.issue(1, 1), self.issue(2, 29, status="resolved"), old, missing,
                             self.issue(5, -1, status="closed"), self.issue(6, 0, status="developed")])
        self.assertEqual({row["id"] for row in result["mantis"]["issues"]}, {1, 6})
        self.assertEqual([row["id"] for row in result["mantis"]["recentClosed"]], [2])
        self.assertEqual(result["mantis"]["recentClosedDateBasis"], "최종수정일")
        self.assertEqual(result["summary"]["open"], 2)
