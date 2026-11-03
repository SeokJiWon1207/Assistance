"""팀 대시보드 공수·처리시간 탭 집계 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import effort_board


TODAY = date(2026, 8, 27)  # 목요일


def _issue(issue_id, **fields):
    return {
        "id": issue_id,
        "summary": f"이슈 {issue_id}",
        "project": "신한",
        "status": "closed",
        "priority": "normal",
        "handler": "석지원",
        "created_at": "2026-08-20",
        "updated_at": "2026-08-20",
        "date_fields": {},
        "effort_fields": {},
        **fields,
    }


class EffortBoardTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            _issue(1, created_at="2026-08-20", updated_at="2026-08-20"),
            _issue(2, created_at="2026-08-18", updated_at="2026-08-20"),
            _issue(3, created_at="2026-08-10", updated_at="2026-08-20"),
            _issue(4, created_at="2026-06-01", updated_at="2026-08-25", project="유진"),
            _issue(5, created_at="2026-07-01", updated_at="2026-07-20"),
            _issue(6, status="confirmed", created_at="2026-07-10", updated_at="2026-08-01"),
            _issue(7, status="assigned", created_at="2026-08-01", updated_at="2026-08-01"),
            _issue(8, status="assigned", created_at="2026-08-25", updated_at="2026-08-25"),
            _issue(9, handler="다른팀", created_at="2026-08-01", updated_at="2026-08-26"),
            _issue(10, created_at="2026-08-01", updated_at="2026-08-21",
                   date_fields={"고객사전달일": "2026-08-21"},
                   effort_fields={"1문의공수": "석지원(1.5)", "2서버공수": "우동헌(2) 석지원(0.5)", "기타공수": "석지원(9)"}),
            _issue(11, created_at="2026-06-01", updated_at="2026-06-02",
                   date_fields={"고객사전달일": "2026-06-02"}, effort_fields={"1문의공수": "석지원(4)"}),
        ]

    def _build(self, *, complete=True, days=30):
        source = {"status": "snapshot", "search_complete": complete}
        with patch.object(effort_board, "_load_issues", return_value=(self.issues, source)):
            return effort_board.build_effort_board(days=days, today=TODAY)

    def test_handling_days_use_team_issues_closed_inside_period(self):
        result = self._build()
        summary = result["summary"]
        # 1:0일, 2:2일, 3:10일, 4:85일, 10:20일 (5는 직전 기간, 9는 다른 팀)
        self.assertEqual(summary["closedCount"], 5)
        self.assertEqual(summary["previousClosedCount"], 1)
        self.assertEqual(summary["medianHandlingDays"], 10.0)
        self.assertEqual(summary["previousMedianHandlingDays"], 19.0)
        self.assertEqual(summary["p90HandlingDays"], 85)
        self.assertEqual([row["count"] for row in result["distribution"]], [1, 1, 0, 1, 1, 1])

    def test_aging_counts_and_watch_list_include_issues_about_to_cross(self):
        result = self._build()
        self.assertEqual(result["summary"]["agingOpen"], 1)
        self.assertEqual(result["summary"]["agingSoon"], 1)
        self.assertEqual([(row["id"], row["aging"]) for row in result["agingIssues"]], [(6, True), (7, False)])
        self.assertEqual(result["agingIssuesTotal"], 2)

    def test_effort_sums_known_types_by_delivery_date(self):
        effort = self._build()["effort"]
        self.assertEqual(effort["total"], 4.0)
        self.assertEqual(effort["issueCount"], 1)
        by_label = {row["label"]: row for row in effort["types"]}
        self.assertEqual(by_label["문의"]["total"], 1.5)
        self.assertEqual(by_label["서버"]["total"], 2.5)
        self.assertIsNone(by_label["QC"]["averagePerIssue"])
        self.assertEqual(self._build(days=90)["effort"]["total"], 8.0)

    def test_weekly_medians_cover_eight_weeks_ending_this_week(self):
        weekly = self._build()["weekly"]
        self.assertEqual(len(weekly), 8)
        self.assertEqual(weekly[-1]["weekStart"], "2026-08-24")
        self.assertTrue(weekly[-1]["partialWeek"])
        self.assertEqual(weekly[-1]["closedCount"], 1)
        self.assertEqual(weekly[-2]["medianDays"], 6.0)

    def test_customers_need_enough_closed_issues(self):
        customers = self._build()["customers"]
        self.assertEqual([(row["customer"], row["closedCount"], row["medianDays"]) for row in customers], [("신한", 4, 6.0)])

    def test_partial_source_withholds_comparisons_and_invalid_period_is_rejected(self):
        summary = self._build(complete=False)["summary"]
        self.assertIsNone(summary["previousClosedCount"])
        self.assertIsNone(summary["previousMedianHandlingDays"])
        with self.assertRaises(ValueError):
            self._build(days=5)


if __name__ == "__main__":
    unittest.main()
