"""고객사 프로필 결정형 리포트 테스트."""

import unittest

from backend.reports import customer


class CustomerProfileReportTests(unittest.TestCase):
    def setUp(self):
        self.originals = {
            "fetch": customer.fetch_mantis_recent_issues,
            "normalize": customer.normalize_issue,
            "matches": customer.issue_matches_constraints,
            "constraints": customer.extract_query_constraints,
            "sources": customer.build_sources_from_issue_rows,
        }
        customer.normalize_issue = lambda issue: issue
        customer.issue_matches_constraints = lambda issue, constraints: True
        customer.extract_query_constraints = lambda text: {
            "customer_key": "신한투자증권",
            "customer_aliases": ["신한투자증권"],
            "created_at": {"from": "2026-06-01", "to": "2026-08-13"},
        }
        customer.build_sources_from_issue_rows = lambda rows: [{"issue_id": row["id"]} for row in rows]

    def tearDown(self):
        customer.fetch_mantis_recent_issues = self.originals["fetch"]
        customer.normalize_issue = self.originals["normalize"]
        customer.issue_matches_constraints = self.originals["matches"]
        customer.extract_query_constraints = self.originals["constraints"]
        customer.build_sources_from_issue_rows = self.originals["sources"]

    def test_unrelated_question_is_not_intercepted(self):
        self.assertIsNone(customer.build_customer_profile_report("신한 이슈 5개 보여줘"))

    def test_customer_is_required(self):
        customer.extract_query_constraints = lambda text: {}

        report = customer.build_customer_profile_report("고객사 프로필 만들어줘")

        self.assertIn("고객사명을 입력", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["status"], "error")
        self.assertEqual(report["retrieval"]["stages"][0]["error_kind"], "validation")

    def test_no_matches_are_a_successful_empty_result(self):
        customer.fetch_mantis_recent_issues = lambda **kwargs: []

        report = customer.build_customer_profile_report("신한투자증권 고객사 프로필")

        self.assertIn("조건에 맞는 이슈가 없습니다", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["status"], "ok")

    def test_profile_combines_trend_backlog_owner_and_duration(self):
        customer.fetch_mantis_recent_issues = lambda **kwargs: [
            {"id": 101, "project": "132SHGM(신한굿모닝증권)", "summary": "오래된 이슈", "status": "confirmed",
             "media_type": "HTS", "handler": "석지원", "created_at": "2026-06-01", "updated_at": "2026-06-03"},
            {"id": 102, "project": "132SHGM(신한굿모닝증권)", "summary": "종료 이슈", "status": "closed",
             "media_type": "MTS", "handler": "우동헌", "created_at": "2026-07-01", "updated_at": "2026-07-11"},
            {"id": 103, "project": "132SHGM(신한굿모닝증권)", "summary": "최근 종료", "status": "closed",
             "media_type": "HTS", "handler": "석지원", "created_at": "2026-08-01", "updated_at": "2026-08-05"},
        ]

        report = customer.build_customer_profile_report("최근 3개월 신한투자증권 고객사 프로필을 만들어줘")
        content = report["content"]

        self.assertIn("신한투자증권 고객사 프로필", content)
        self.assertNotIn("132SHGM(신한굿모닝증권) 고객사 프로필", content)
        self.assertIn("전체: **3건** · 미종료: **1건** · 종료: **2건**", content)
        self.assertIn("평균 처리기간: **7.0일**", content)
        self.assertIn("2026-06: 1건", content)
        self.assertIn("석지원: 2건", content)
        self.assertIn("#101 오래된 이슈", content)
        self.assertIn("운영 건강도: **위험**", content)
        self.assertIn("판단 근거:", content)
        self.assertIn("권장 확인 항목", content)
        self.assertEqual(report["raw"]["mode"], "deterministic_customer_profile")
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["matched_count"], 3)
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["health_level"], "위험")

    def test_fetch_failure_is_visible(self):
        def fail(**kwargs):
            raise RuntimeError("연결 오류")

        customer.fetch_mantis_recent_issues = fail
        report = customer.build_customer_profile_report("신한투자증권 고객사 프로필")

        self.assertIn("연결 오류", report["content"])
        self.assertEqual(report["sources"], [])
        self.assertEqual(report["retrieval"]["stages"][0]["error_kind"], "dependency")
