"""주간 팀 브리핑 기준본과 확인 상태 테스트."""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend.reports import team_brief


class TeamBriefTests(unittest.TestCase):
    def test_weekly_brief_compares_previous_week_and_prioritizes_risks(self):
        issues = [
            {"id": 101, "summary": "지난주 신규", "project": "신한", "screen_number": "7600", "status": "confirmed",
             "handler": "담당자 미지정", "created_at": "2026-08-05", "updated_at": "2026-08-06", "date_fields": {}},
            {"id": 102, "summary": "지난주 신규2", "project": "신한", "screen_number": "7600", "status": "closed",
             "handler": "석지원", "created_at": "2026-08-06", "updated_at": "2026-08-07", "date_fields": {}},
            {"id": 90, "summary": "직전 주", "project": "유진", "screen_number": "5000", "status": "closed",
             "handler": "석지원", "created_at": "2026-07-28", "updated_at": "2026-07-29", "date_fields": {}},
            {"id": 80, "summary": "장기 이월", "project": "신한", "screen_number": "7000", "status": "assigned",
             "handler": "석지원", "created_at": "2026-05-01", "updated_at": "2026-08-01", "date_fields": {}},
        ]
        with patch.object(team_brief, "_load_issues", return_value=(issues, {"status": "live", "reason": ""})):
            result = team_brief._generate_team_brief(date(2026, 8, 10))

        self.assertEqual(result["summaryPeriod"], "2026-08-03 ~ 2026-08-09")
        self.assertEqual(result["summary"]["newIssues"], 2)
        self.assertEqual(result["summary"]["previousNewIssues"], 1)
        self.assertEqual(result["summary"]["aging"], 1)
        self.assertEqual(result["summary"]["unassigned"], 1)
        self.assertEqual(result["customerSpikes"][0]["name"], "신한")
        self.assertEqual({item["id"] for item in result["priorities"]}, {80, 101})

    def test_weekly_snapshot_is_reused_and_read_state_is_per_user(self):
        generated = {
            "weekStart": "2026-08-10",
            "title": "브리핑",
            "summaryPeriod": "2026-08-03 ~ 2026-08-09",
            "summary": {},
            "source": {"status": "live", "reason": ""},
            "generatedAt": "2026-08-10T09:00:00+09:00",
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")}
        ), patch.object(team_brief, "_generate_team_brief", return_value=generated) as generate:
            first = team_brief.get_weekly_team_brief("user-a", today=date(2026, 8, 10))
            second = team_brief.get_weekly_team_brief("user-a", today=date(2026, 8, 11))
            team_brief.mark_weekly_team_brief_read("user-a", first["weekStart"])
            read = team_brief.get_weekly_team_brief("user-a", today=date(2026, 8, 12))
            unread = team_brief.get_weekly_team_brief("user-b", today=date(2026, 8, 12))

        self.assertEqual(generate.call_count, 1)
        self.assertEqual(first["generatedAt"], second["generatedAt"])
        self.assertTrue(read["viewed"])
        self.assertFalse(unread["viewed"])
