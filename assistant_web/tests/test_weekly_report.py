"""주간 보고서 결정형 집계 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import weekly_customer


def issue(identifier, created, updated, status, *, effort=None, handler="석지원"):
    return {
        "id": identifier,
        "project": "132SHGM",
        "summary": f"이슈 {identifier}",
        "status": status,
        "handler": handler,
        "created_at": created,
        "updated_at": updated,
        "media_type": "MTS",
        "screen_number": "7600",
        "effort_fields": effort or {},
    }


class ReportDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 14)


class WeeklyReportTests(unittest.TestCase):
    def setUp(self):
        self.date_patch = patch.object(weekly_customer, "date", ReportDate)
        self.date_patch.start()
        self.addCleanup(self.date_patch.stop)
        self.originals = {
            "fetch": weekly_customer.fetch_mantis_recent_issues,
            "normalize": weekly_customer.normalize_issue,
            "matches": weekly_customer.issue_matches_constraints,
            "constraints": weekly_customer.extract_query_constraints,
            "sources": weekly_customer.build_sources_from_issue_rows,
        }
        weekly_customer.normalize_issue = lambda row: row
        weekly_customer.issue_matches_constraints = lambda row, constraints: True
        weekly_customer.extract_query_constraints = lambda text: {
            "customer_key": "신한투자증권",
            "customer_aliases": ["신한투자증권"],
            "statuses": ["open"],
            "created_at": {"from": "2026-09-07", "to": "2026-09-13", "precision": "last_week"},
        }
        weekly_customer.build_sources_from_issue_rows = lambda rows: [{"issue_id": row["id"]} for row in rows]

    def tearDown(self):
        weekly_customer.fetch_mantis_recent_issues = self.originals["fetch"]
        weekly_customer.normalize_issue = self.originals["normalize"]
        weekly_customer.issue_matches_constraints = self.originals["matches"]
        weekly_customer.extract_query_constraints = self.originals["constraints"]
        weekly_customer.build_sources_from_issue_rows = self.originals["sources"]

    def test_unrelated_question_is_not_intercepted(self):
        self.assertIsNone(weekly_customer.build_weekly_report("신한 이슈 보여줘"))
        self.assertIsNone(weekly_customer.build_weekly_report("주간업무 보여줘"))

    def test_period_is_required_and_bounded(self):
        weekly_customer.extract_query_constraints = lambda text: {"customer_aliases": ["신한"]}
        missing = weekly_customer.build_weekly_report("신한 주간 보고서 작성해줘")
        self.assertIn("기간", missing["content"])
        self.assertEqual(missing["retrieval"]["stages"][0]["error_kind"], "validation")

        weekly_customer.extract_query_constraints = lambda text: {
            "customer_aliases": ["신한"],
            "created_at": {"from": "2026-01-01", "to": "2026-06-30", "precision": "range"},
        }
        too_wide = weekly_customer.build_weekly_report("신한 주간 보고서 작성해줘")
        self.assertEqual(too_wide["retrieval"]["stages"][0]["error_kind"], "validation")

    def test_report_counts_new_open_closed_aging_and_effort(self):
        aged = "2026-08-10"
        weekly_customer.fetch_mantis_recent_issues = lambda **kwargs: [
            issue(201, "2026-09-08", "2026-09-11", "closed", effort={"3PC공수": "석지원(2.5)"}),
            issue(202, "2026-09-09", "2026-09-09", "confirmed", effort={"1문의공수": "우동헌(1.0)"}, handler="우동헌"),
            issue(203, "2026-09-02", "2026-09-10", "closed"),
            issue(204, aged, aged, "assigned"),
        ]

        report = weekly_customer.build_weekly_report(
            "지난주 신한 신규 이슈, 미종료 이슈, 장기 적체, 공수와 처리시간을 주간 보고서 형식으로 작성해줘"
        )
        meta = report["retrieval"]["stages"][0]["meta"]
        self.assertEqual(meta["current_count"], 2)
        self.assertEqual(meta["previous_count"], 1)
        # 질문에 '미종료'가 들어 있어도 종료 건을 함께 세어 처리시간을 계산한다.
        self.assertEqual(meta["closed_count"], 2)
        self.assertEqual(meta["open_count"], 2)
        self.assertEqual(meta["aging_count"], 1)
        self.assertEqual(meta["total_effort"], 3.5)

        content = report["content"]
        self.assertIn("주간 보고서", content)
        self.assertIn("신규 등록: **2건**", content)
        self.assertIn("기간 내 해결·종료: **2건**", content)
        self.assertIn("장기 적체: **1건**", content)
        self.assertIn("#204", content)
        self.assertEqual(report["raw"]["mode"], "deterministic_weekly_report")

    def test_every_issue_number_in_the_report_is_in_its_sources(self):
        # 신규에 없는 오래된 미종료 이슈(301·302)가 '미종료 이슈' 목록에만 나와도 근거에 들어가야
        # 응답 검증이 본문의 번호를 근거 없는 번호로 보고 답변 전체를 보류하지 않는다.
        weekly_customer.fetch_mantis_recent_issues = lambda **kwargs: [
            issue(201, "2026-09-08", "2026-09-11", "closed", effort={"3PC공수": "석지원(2.5)"}),
            issue(301, "2026-09-01", "2026-09-01", "assigned"),
            issue(302, "2026-08-31", "2026-08-31", "confirmed"),
            issue(204, "2026-08-10", "2026-08-10", "assigned"),
        ]
        report = weekly_customer.build_weekly_report("지난주 신한 주간 보고서 작성해줘")
        import re
        mentioned = {int(value) for value in re.findall(r"#(\d+)", report["content"])}
        self.assertEqual(mentioned, {201, 301, 302, 204})
        self.assertEqual(mentioned, {source["issue_id"] for source in report["sources"]})
        # 상태는 내부 값 대신 화면 이름으로 보인다.
        self.assertNotRegex(report["content"], r"— (?:assigned|confirmed|closed) ·")

    def test_dependency_failure_is_reported(self):
        def explode(**kwargs):
            raise RuntimeError("timeout")

        weekly_customer.fetch_mantis_recent_issues = explode
        report = weekly_customer.build_weekly_report("지난주 신한 주간 보고서 작성해줘")
        self.assertEqual(report["retrieval"]["stages"][0]["error_kind"], "dependency")


if __name__ == "__main__":
    unittest.main()
