import unittest
from datetime import date
from unittest.mock import patch

from backend.query_planning import extract_created_at_range
from backend.reports.effort import build_effort_report, extract_effort_query


class EffortDateRangeTests(unittest.TestCase):
    def test_relative_year_day_range_is_preserved_for_customer_effort(self):
        year = date.today().year
        question = "올해 3월 1일부터 7월 10일까지 미래에셋 공수 알려줘"

        self.assertEqual(
            extract_created_at_range(question),
            {
                "from": f"{year}-03-01",
                "to": f"{year}-07-10",
                "precision": "day_range",
            },
        )
        query = extract_effort_query(question)
        self.assertEqual(query["scope"], "filters")
        self.assertEqual(query["constraints"]["customer_key"], "미래에셋")
        self.assertEqual(
            query["date_range"],
            {
                "from": f"{year}-03-01",
                "to": f"{year}-07-10",
                "precision": "day_range",
            },
        )
        self.assertNotIn("month", query)

    def test_customer_effort_uses_only_issues_inside_exact_date_range(self):
        def issue(issue_id, created_at, effort):
            return {
                "id": issue_id,
                "project": {"name": "147MiraeAsset(미래에셋증권)"},
                "summary": f"공수 테스트 {issue_id}",
                "status": {"name": "assigned"},
                "handler": {"real_name": "석지원"},
                "reporter": {"real_name": "요청자"},
                "created_at": created_at,
                "updated_at": created_at,
                "custom_fields": [
                    {"field": {"name": "1문의공수"}, "value": f"석지원({effort})"}
                ],
            }

        batches = [
            [
                issue(3, "2026-07-11T09:00:00+09:00", 9),
                issue(2, "2026-07-10T09:00:00+09:00", 1.5),
                issue(1, "2026-02-28T09:00:00+09:00", 7),
            ],
            [],
        ]
        with patch(
            "backend.reports.effort.fetch_mantis_issues",
            side_effect=batches,
        ):
            report = build_effort_report(
                "2026년 3월 1일부터 7월 10일까지 미래에셋 공수 알려줘"
            )

        self.assertIn("총 공수: **1.50**", report["content"])
        self.assertIn("산정 기간: 2026-03-01 ~ 2026-07-10", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["period"], "range")
        self.assertEqual(
            report["retrieval"]["constraints"]["created_at"],
            {"from": "2026-03-01", "to": "2026-07-10", "precision": "range"},
        )
        self.assertEqual([source["issue_id"] for source in report["sources"]], [2])

    def test_cross_year_explicit_range_is_supported(self):
        query = extract_effort_query(
            "2025년 12월 31일부터 2026년 1월 2일까지 석지원 공수 알려줘"
        )
        self.assertEqual(
            query["date_range"],
            {
                "from": "2025-12-31",
                "to": "2026-01-02",
                "precision": "day_range",
            },
        )


if __name__ == "__main__":
    unittest.main()
