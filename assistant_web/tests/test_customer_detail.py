"""고객사 상세 리포트 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import customer_detail


TODAY = date(2026, 8, 27)


def _issue(issue_id, **fields):
    return {
        "id": issue_id,
        "summary": f"이슈 {issue_id}",
        "project": "신한",
        "screen_number": "7600",
        "media_type": "HTS",
        "status": "confirmed",
        "priority": "normal",
        "handler": "석지원",
        "created_at": "2026-08-20",
        "updated_at": "2026-08-20",
        "date_fields": {},
        **fields,
    }


class CustomerDetailTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            _issue(101, created_at="2026-05-01", updated_at="2026-08-01", date_fields={"완료예정일": "2026-08-10"}),
            _issue(102, handler="우동헌", created_at="2026-06-01", media_type="기타/미상"),
            _issue(107, handler="담당자 미지정", created_at="2026-08-26"),
            _issue(103, status="closed", created_at="2026-08-18", updated_at="2026-08-22", screen_number="0150"),
            _issue(104, created_at="2026-08-25", media_type="MTS"),
            _issue(105, project="유진", created_at="2026-08-21"),
            _issue(106, handler="다른팀", created_at="2026-08-21"),
        ]
        self.playbooks = [
            {"id": "pb-old", "title": "오래된 사례", "sourceIssueIds": [101], "updatedAt": "2026-07-01"},
            {"id": "pb-new", "title": "최근 사례", "sourceIssueIds": [103], "updatedAt": "2026-08-01"},
            {"id": "pb-both", "title": "두 건 사례", "sourceIssueIds": [101, 104, 999], "updatedAt": "2026-06-01"},
            {"id": "pb-other", "title": "다른 고객사", "sourceIssueIds": [105], "updatedAt": "2026-08-20"},
        ]

    def _build(self, *, complete=True, customer="신한", days=30):
        source = {"status": "snapshot", "search_complete": complete, "fetchedAt": "2026-08-27T09:00:00+09:00"}
        with patch.object(customer_detail, "_load_issues", return_value=(self.issues, source)):
            return customer_detail.build_customer_detail(
                customer, days=days, today=TODAY, playbook_loader=lambda **_: self.playbooks,
            )

    def test_counts_only_team_issues_of_the_customer(self):
        result = self._build()
        self.assertEqual(result["customer"]["id"], "신한")
        self.assertEqual(result["summary"]["totalIssues"], 4)
        self.assertEqual(result["summary"]["open"], 3)
        self.assertEqual(result["summary"]["overdue"], 1)
        self.assertEqual(result["summary"]["aging"], 2)
        self.assertEqual(result["openIssuesTotal"], 3)
        self.assertEqual([row["id"] for row in result["openIssues"]], [101, 102, 104])

    def test_period_breakdowns_use_new_issues_inside_period(self):
        result = self._build()
        self.assertEqual(result["summary"]["newIssues"], 2)
        self.assertEqual(result["screens"], [{"name": "0150", "count": 1}, {"name": "7600", "count": 1}])
        self.assertEqual(result["media"], [{"name": "HTS", "count": 1}, {"name": "MTS", "count": 1}])
        week = self._build(days=7)
        self.assertEqual(week["summary"]["newIssues"], 1)
        self.assertEqual(week["media"], [{"name": "MTS", "count": 1}])

    def test_monthly_flow_covers_six_months_ending_this_month(self):
        monthly = self._build()["monthly"]
        self.assertEqual([row["month"] for row in monthly], ["2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"])
        self.assertEqual(monthly[-1], {"month": "2026-08", "label": "8월", "newCount": 2, "closedCount": 1})
        self.assertEqual(monthly[2]["newCount"], 1)

    def test_playbooks_match_customer_issues_most_overlap_first(self):
        playbooks = self._build()["playbooks"]
        self.assertEqual([row["id"] for row in playbooks], ["pb-both", "pb-new", "pb-old"])
        self.assertEqual(playbooks[0]["matchedIssueIds"], ["101", "104"])

    def test_partial_source_withholds_health_and_comparisons(self):
        result = self._build(complete=False)
        self.assertEqual(result["health"]["level"], "unknown")
        self.assertIsNone(result["summary"]["previousNewIssues"])
        self.assertIsNone(result["summary"]["teamAverageHandlingDays"])

    def test_unknown_customer_and_invalid_input_are_rejected(self):
        with self.assertRaises(LookupError):
            self._build(customer="없는고객사")
        with self.assertRaises(ValueError):
            self._build(days=5)
        with self.assertRaises(ValueError):
            self._build(customer="  ")


if __name__ == "__main__":
    unittest.main()
