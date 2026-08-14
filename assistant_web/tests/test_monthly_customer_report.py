"""월간 고객사 리포트 결정형 집계 테스트."""

import unittest

from backend.reports import monthly_customer


class MonthlyCustomerReportTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            "fetch": monthly_customer.fetch_mantis_recent_issues,
            "normalize": monthly_customer.normalize_issue,
            "matches": monthly_customer.issue_matches_constraints,
            "constraints": monthly_customer.extract_query_constraints,
            "sources": monthly_customer.build_sources_from_issue_rows,
        }
        monthly_customer.normalize_issue = lambda issue: issue
        monthly_customer.issue_matches_constraints = lambda issue, constraints: True
        monthly_customer.extract_query_constraints = lambda text: {
            "customer_key": "신한투자증권",
            "customer_aliases": ["신한투자증권"],
            "created_at": {"from": "2026-07-01", "to": "2026-07-31", "precision": "month"},
        }
        monthly_customer.build_sources_from_issue_rows = lambda rows: [{"issue_id": row["id"]} for row in rows]

    def tearDown(self):
        monthly_customer.fetch_mantis_recent_issues = self.originals["fetch"]
        monthly_customer.normalize_issue = self.originals["normalize"]
        monthly_customer.issue_matches_constraints = self.originals["matches"]
        monthly_customer.extract_query_constraints = self.originals["constraints"]
        monthly_customer.build_sources_from_issue_rows = self.originals["sources"]

    def test_unrelated_question_is_not_intercepted(self):
        self.assertIsNone(monthly_customer.build_monthly_customer_report("신한 이슈 보여줘"))

    def test_customer_and_full_month_are_required(self):
        monthly_customer.extract_query_constraints = lambda text: {}
        self.assertIn("고객사명을 입력", monthly_customer.build_monthly_customer_report("월간 고객사 리포트")["content"])
        monthly_customer.extract_query_constraints = lambda text: {"customer_aliases": ["신한"]}
        self.assertIn("보고할 월", monthly_customer.build_monthly_customer_report("신한 월간 고객사 리포트")["content"])

    def test_report_combines_previous_month_effort_duration_and_aging(self):
        monthly_customer.fetch_mantis_recent_issues = lambda **kwargs: [
            {"id": 101, "project": "132SHGM", "summary": "종료 이슈", "status": "closed", "handler": "석지원",
             "created_at": "2026-07-01", "updated_at": "2026-07-10", "media_type": "MTS", "screen_number": "7600",
             "effort_fields": {"3PC공수": "석지원(2.5)"}},
            {"id": 102, "project": "132SHGM", "summary": "미종료 이슈", "status": "confirmed", "handler": "우동헌",
             "created_at": "2026-07-03", "updated_at": "2026-07-04", "media_type": "HTS", "screen_number": "7600",
             "effort_fields": {"1문의공수": "우동헌(1.0)"}},
            {"id": 90, "project": "132SHGM", "summary": "전월 이슈", "status": "closed", "handler": "석지원",
             "created_at": "2026-06-02", "updated_at": "2026-06-05", "media_type": "MTS", "screen_number": "5000",
             "effort_fields": {}},
            {"id": 80, "project": "132SHGM", "summary": "장기 미종료", "status": "assigned", "handler": "석지원",
             "created_at": "2026-05-01", "updated_at": "2026-05-03", "media_type": "MTS", "screen_number": "5000",
             "effort_fields": {}},
        ]

        report = monthly_customer.build_monthly_customer_report("2026년 7월 신한투자증권 월간 고객사 리포트")
        content = report["content"]

        self.assertIn("2026년 7월 신한투자증권", content)
        self.assertIn("등록 이슈: **2건** · 전월 1건", content)
        self.assertIn("총 공수: **3.50**", content)
        self.assertIn("평균 처리기간: **9.0일**", content)
        self.assertIn("7600 2건", content)
        self.assertIn("#80 장기 미종료", content)
        self.assertIn("과거 월말 상태를 복원한 값이 아닙니다", content)
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["total_effort"], 3.5)

    def test_fetch_failure_is_visible(self):
        def fail(**kwargs):
            raise RuntimeError("연결 실패")

        monthly_customer.fetch_mantis_recent_issues = fail
        report = monthly_customer.build_monthly_customer_report("2026년 7월 신한 월간 고객사 리포트")
        self.assertIn("연결 실패", report["content"])
