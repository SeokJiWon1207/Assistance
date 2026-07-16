import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import backend.app as app
from scripts.accuracy.dynamic_accuracy_check import exact_screen_numbers


class ScreenNumberAccuracyTests(unittest.TestCase):
    @patch.object(app, "get_mantis_headers", return_value={})
    @patch.object(app, "get_mantis_settings", return_value={"url": "https://mantis.example/api/issues"})
    @patch.object(app.requests, "get")
    def test_page_scan_checks_all_recent_pages_before_selecting_latest_results(
        self, get, _settings, _headers
    ):
        def issue(issue_id, project="115다올투자증권(구KTB)"):
            return {
                "id": issue_id,
                "project": {"name": project},
                "summary": f"이슈 {issue_id}",
                "status": {"name": "confirmed"},
                "handler": {"real_name": "담당자"},
                "created_at": "2026-05-01",
            }

        first_page = [issue(300), issue(200), issue(100)] + [
            issue(issue_id, "다른 고객사") for issue_id in range(99, 2, -1)
        ]
        get.side_effect = [
            Mock(status_code=200, json=lambda: {"issues": first_page}),
            Mock(status_code=200, json=lambda: {"issues": [issue(400)]}),
        ]

        result = app.execute_mantis_page_scan(
            "115다올투자증권 구KTB",
            limit=3,
            constraints={"customer_exact": "115다올투자증권(구KTB)"},
        )

        self.assertEqual([row["id"] for row in result["issues_list"]], [400, 300, 200])
        self.assertEqual(get.call_count, 2)

    def test_dynamic_oracle_matches_field_or_exact_title_marker(self):
        field_issue = {
            "summary": "해외주식 차트 수정",
            "custom_fields": [{"field": {"name": "화면번호"}, "value": "9413"}],
        }
        title_issue = {"summary": "[7600] 차트 지표 수정", "custom_fields": []}
        free_text_issue = {"summary": "194130 로그에서 발생", "custom_fields": []}
        self.assertEqual(exact_screen_numbers(field_issue), {"9413"})
        self.assertEqual(exact_screen_numbers(title_issue), {"7600"})
        self.assertEqual(exact_screen_numbers(free_text_issue), set())

    def test_audit_screen_match_requires_numeric_boundary(self):
        self.assertTrue(app.text_matches_screen_numbers("화면번호 9413", ["9413"]))
        self.assertFalse(app.text_matches_screen_numbers("식별자 194130", ["9413"]))

    @patch("backend.retrieval.execute_mantis_filter_search")
    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_snapshot_search")
    def test_screen_query_uses_exact_snapshot_and_page_scan_only(
        self, snapshot_search, page_scan, filter_search
    ):
        snapshot_search.return_value = {
            "source": "mantis_snapshot",
            "status": "ok",
            "meta": {"snapshot_as_of": "2026-07-01"},
            "issues_list": [],
        }
        page_scan.return_value = {
            "source": "mantis_page_scan",
            "status": "ok",
            "issues_list": [],
        }
        result = app.run_retrieval_pipeline(
            "화면번호 9413 관련 이슈 3개 보여줘",
            3,
            {"mantis": True, "notion": False, "vectorDb": False},
        )
        self.assertEqual(
            [stage["source"] for stage in result["stages"]],
            ["mantis_snapshot", "mantis_page_scan"],
        )
        filter_search.assert_not_called()


class AccuracyInsightTests(unittest.TestCase):
    def test_analytics_reports_delta_trend_and_failure_frequency(self):
        reports = [
            {
                "run_at": "2026-07-10T08:00:00+09:00",
                "summary": {"score": 90, "passed": 9, "total": 10},
                "results": [
                    {"category": "화면번호", "passed": False},
                    {"category": "정확 ID", "passed": True},
                ],
            },
            {
                "run_at": "2026-07-09T08:00:00+09:00",
                "summary": {"score": 70, "passed": 7, "total": 10},
                "results": [
                    {"category": "화면번호", "passed": False},
                    {"category": "담당자", "passed": False},
                ],
            },
        ]
        analytics = app.build_accuracy_analytics(reports)
        self.assertEqual(analytics["latestDelta"], 20)
        self.assertEqual([point["score"] for point in analytics["trend"]], [70, 90])
        self.assertEqual(
            analytics["failureCounts"],
            [{"category": "화면번호", "count": 2}, {"category": "담당자", "count": 1}],
        )

    def test_freshness_marks_old_snapshot_stale(self):
        old_date = (date.today() - timedelta(days=7)).isoformat()
        with patch("backend.sources.mantis.get_mantis_snapshot_freshness", return_value=old_date):
            status = app.get_mantis_snapshot_freshness_status()
        self.assertEqual(status["ageDays"], 7)
        self.assertEqual(status["status"], "stale")


if __name__ == "__main__":
    unittest.main()
