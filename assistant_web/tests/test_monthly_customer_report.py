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
            "store": monthly_customer.read_complete_store,
        }
        # 기본은 저장본이 없는 상황(최근 등록분 실시간)으로 고정해 개발 PC의 실제 저장본에 흔들리지 않게 한다.
        monthly_customer.read_complete_store = lambda **kwargs: None
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
        monthly_customer.read_complete_store = self.originals["store"]

    def test_open_and_aging_counts_use_the_complete_store_not_the_latest_1000(self):
        from backend.sources.mantis_collection import IssueBatch

        def row(i, created, status):
            return {"id": i, "project": "132SHGM", "summary": f"이슈 {i}", "status": status, "handler": "석지원",
                    "created_at": created, "updated_at": created, "media_type": "MTS", "screen_number": "7600",
                    "effort_fields": {}}

        # 최근 1,000건 밖의 오래된 미종료 이슈(#5)도 전체 저장본에서는 장기 미종료로 센다.
        stored = IssueBatch([row(2001, "2026-07-02", "confirmed"), *[row(1000 + i, "2026-07-03", "closed") for i in range(1000)],
                             row(5, "2021-01-04", "assigned")],
                            {"status": "snapshot", "fetchedAt": "2026-08-01T00:00:00+00:00", "search_complete": True})
        monthly_customer.read_complete_store = lambda **kwargs: stored
        monthly_customer.fetch_mantis_recent_issues = lambda **kwargs: self.fail("저장본이 있으면 최근 1,000건 실시간 조회를 하지 않는다")
        report = monthly_customer.build_monthly_customer_report("2026년 7월 신한투자증권 월간 고객사 리포트")
        # 장기 미종료는 그 달 등록분이 아니라 보고 종료일까지 열려 있는 전체 이슈를 센다.
        self.assertIn("현재 장기 미종료: **2건**", report["content"])
        self.assertIn("- #5 이슈 5 — 현재", report["content"])
        self.assertIn("Mantis 전체 1,002건 기준", report["content"])
        self.assertNotIn("건 범위", report["content"])

    def test_without_a_store_the_report_says_its_scope_is_partial(self):
        monthly_customer.fetch_mantis_recent_issues = lambda **kwargs: [
            {"id": 101, "project": "132SHGM", "summary": "이슈", "status": "confirmed", "handler": "석지원",
             "created_at": "2026-07-01", "updated_at": "2026-07-01", "media_type": "MTS", "screen_number": "7600",
             "effort_fields": {}},
        ]
        report = monthly_customer.build_monthly_customer_report("2026년 7월 신한투자증권 월간 고객사 리포트")
        self.assertIn("최근 등록 1건만 확인 — 미종료·장기 적체가 실제보다 적을 수 있습니다", report["content"])

    def test_unrelated_question_is_not_intercepted(self):
        self.assertIsNone(monthly_customer.build_monthly_customer_report("신한 이슈 보여줘"))

    def test_customer_and_full_month_are_required(self):
        monthly_customer.extract_query_constraints = lambda text: {}
        missing_customer = monthly_customer.build_monthly_customer_report("월간 고객사 리포트")
        self.assertIn("고객사명을 입력", missing_customer["content"])
        self.assertEqual(
            missing_customer["retrieval"]["stages"][0]["error_kind"],
            "validation",
        )
        monthly_customer.extract_query_constraints = lambda text: {"customer_aliases": ["신한"]}
        missing_month = monthly_customer.build_monthly_customer_report("신한 월간 고객사 리포트")
        self.assertIn("보고할 월", missing_month["content"])
        self.assertEqual(
            missing_month["retrieval"]["stages"][0]["error_kind"],
            "validation",
        )

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
        self.assertEqual(report["retrieval"]["stages"][0]["error_kind"], "dependency")
