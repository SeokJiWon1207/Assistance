"""금주 차트의 업무 날짜·상태·범위를 검증한다."""
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.reports import dashboard
from backend.common import normalize_chroma_issue_document


class WeeklyIssuesTests(unittest.TestCase):
    def issue(self, **overrides):
        return {"id": 1, "summary": "주문 오류", "handler": "석지원", "status": "assigned",
                "media_type": "HTS", "severity_label": "오류", "created_at": "2020-01-01",
                "updated_at": "2026-10-29", "date_fields": {}, **overrides}

    def build(self, issues, today=date(2026, 10, 29), days=30, complete=True):
        with patch.object(dashboard, "_load_issues", return_value=(issues, {"status": "snapshot", "search_complete": complete})):
            result = dashboard.build_operations_dashboard({"role": "member"}, today=today, days=days)
        self.assertIn("weeklyIssues", result)
        return result["weeklyIssues"]

    def test_saturday_friday_bounds_include_future_friday_and_ignore_registration(self):
        issues = [self.issue(id=i, date_fields={"고객사요청일": day}) for i, day in enumerate(
            ["2026-10-23", "2026-10-24", "2026-10-30", "2026-10-31", "", "2026-02-30"])]
        result = self.build(issues)
        self.assertEqual((result["from"], result["to"]), ("2026-10-24", "2026-10-30"))
        self.assertEqual(result["received"]["total"], 2)
        self.assertEqual(result["received"]["missingDate"], 2)
        self.assertEqual(self.build(issues, days=7), result)
        self.assertEqual(self.build(issues, today=date(2026, 10, 30))["received"]["total"], 2)
        self.assertEqual(self.build(issues, today=date(2026, 10, 31))["from"], "2026-10-31")
        self.assertEqual(self.build([], today=date(2022, 1, 1))["from"], "2022-01-01")
        self.assertEqual(self.build([], today=date(2027, 1, 1))["from"], "2026-12-26")

    def test_completion_uses_delivery_date_regardless_of_status_excluding_maintenance(self):
        issues = [
            self.issue(id=1, date_fields={"고객사전달일": "2026-10-24"}, updated_at="2025-01-01"),
            self.issue(id=2, status="closed"),
            self.issue(id=3, status="closed", date_fields={"고객사전달일": "2026-10-23"}),
            self.issue(id=4, summary="[HTS] 정기점검 결과", date_fields={"고객사전달일": "2026-10-29"}),
            self.issue(id=5, status="feedback", media_type="MTS", severity_label="문의",
                       date_fields={"고객사전달일": "2026-10-30"}),
        ]
        result = self.build(issues)
        self.assertEqual(result["completed"]["total"], 2)
        self.assertEqual(result["completed"]["rows"], [
            {"name": "HTS", "counts": {"오류": 1}}, {"name": "MTS", "counts": {"문의": 1}}])

    def test_remaining_excludes_only_four_states_in_both_source_languages(self):
        excluded = ["closed", "feedback", "developed", "resolved", "종료된이슈", "정보부족", "개발완료", "해결된이슈"]
        included = ["new", "acknowledged", "confirmed", "assigned", "알 수 없는 상태", ""]
        result = self.build([self.issue(id=i, status=s) for i, s in enumerate(excluded + included)])
        self.assertEqual(result["remaining"]["total"], 6)
        self.assertEqual(result["remaining"]["rows"], [{"name": "HTS", "count": 6}])

    def test_team_scope_unknown_categories_and_partial_coverage_are_preserved(self):
        issues = [self.issue(id=1, media_type="WEB", severity_label="추가", date_fields={"고객사요청일": "2026-10-29"}),
                  self.issue(id=2, media_type="", severity_label="", date_fields={"고객사요청일": "2026-10-29"}),
                  self.issue(id=3, handler="외부담당", date_fields={"고객사요청일": "2026-10-29"})]
        result = self.build(issues, complete=False)
        self.assertFalse(result["complete"])
        self.assertEqual(result["received"]["total"], 2)
        self.assertEqual(result["remaining"]["total"], 2)
        self.assertEqual(result["received"]["rows"], [
            {"name": "WEB", "counts": {"추가": 1}}, {"name": "기타/미상", "counts": {"미분류": 1}}])

    def test_legacy_snapshot_preserves_actual_business_dates(self):
        row = normalize_chroma_issue_document("[Mantis 이슈 정보]\n- 이슈 ID: #123\n- 고객사요청일: 2026-10-24\n- 고객사전달일: 2026-10-30\n[제목]\n주문 오류")
        self.assertEqual(row["date_fields"].get("고객사요청일"), "2026-10-24")
        self.assertEqual(row["date_fields"].get("고객사전달일"), "2026-10-30")

    def test_saturday_rollover_uses_korean_time_even_when_utc_is_friday(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 30, 15, 1, tzinfo=timezone.utc).astimezone(tz)

        with patch.object(dashboard, "datetime", Clock):
            self.assertEqual(self.build([], today=None)["from"], "2026-10-31")

    def test_weekly_filter_regression_fixture(self):
        cases = [json.loads(line) for line in (Path(__file__).parent / "fixtures/routing_cases.jsonl").read_text().splitlines()]
        cases = [case["dashboard_weekly"] for case in cases if "dashboard_weekly" in case]
        self.assertTrue(cases)
        for case in cases:
            result = self.build([self.issue(**row) for row in case["issues"]])
            self.assertEqual({key: result[key]["total"] for key in case["expected"]}, case["expected"])
