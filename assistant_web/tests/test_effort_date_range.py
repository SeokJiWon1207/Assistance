import os
import unittest
from datetime import date
from threading import Event
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
                    {"field": {"name": "고객사전달일"}, "value": created_at[:10]},
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
        # 뒤 페이지를 미리 요청하므로 호출 순서가 아니라 페이지 번호로 응답한다.
        with patch(
            "backend.reports.effort.fetch_mantis_issues",
            side_effect=lambda params, **_: (
                batches[params["page"] - 1] if params["page"] <= len(batches) else []
            ),
        ):
            report = build_effort_report(
                "2026년 3월 1일부터 7월 10일까지 미래에셋 공수 알려줘"
            )

        self.assertIn("총 공수: **1.50**", report["content"])
        self.assertIn("산정 기간: 2026-03-01 ~ 2026-07-10", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["period"], "range")
        self.assertEqual(
            report["retrieval"]["constraints"]["delivered_at"],
            {"from": "2026-03-01", "to": "2026-07-10", "precision": "range"},
        )
        self.assertEqual([source["issue_id"] for source in report["sources"]], [2])

    def test_prefetched_pages_after_empty_page_do_not_change_or_break_the_report(self):
        """빈 페이지 뒤의 미리 받은 결과와 오류는 집계에 반영하지 않는다."""
        def issue(issue_id, created_at, effort):
            return {
                "id": issue_id,
                "project": {"name": "147MiraeAsset(미래에셋증권)"},
                "summary": f"공수 테스트 {issue_id}",
                "status": {"name": "assigned"},
                "handler": {"real_name": "석지원"},
                "created_at": created_at,
                "custom_fields": [
                    {"field": {"name": "고객사전달일"}, "value": created_at[:10]},
                    {"field": {"name": "1문의공수"}, "value": f"석지원({effort})"}
                ],
            }

        pages = {
            1: [issue(5, "2026-07-10T09:00:00+09:00", 1.5)],
            2: [],
            3: [issue(3, "2026-01-09T09:00:00+09:00", 7)],
            4: [issue(2, "2026-07-09T09:00:00+09:00", 100)],
        }
        requested = []
        page_four_requested = Event()

        def fetch(params, **_):
            page = params["page"]
            requested.append(page)
            if page == 4:
                page_four_requested.set()
            elif page == 3:
                # 멈추기 전에 4쪽이 이미 선요청됐음을 보장해 검증이 공허하지 않게 한다.
                self.assertTrue(page_four_requested.wait(timeout=3))
            if page >= 5:
                raise RuntimeError("멈춘 뒤의 선요청 오류")
            return pages[page]

        with (
            patch.dict(os.environ, {"MANTIS_LIVE_MAX_WORKERS": "6"}),
            patch("backend.reports.effort.fetch_mantis_issues", side_effect=fetch),
        ):
            report = build_effort_report(
                "2026년 3월 1일부터 7월 10일까지 미래에셋 공수 알려줘"
            )

        self.assertIn("총 공수: **1.50**", report["content"])
        self.assertEqual([source["issue_id"] for source in report["sources"]], [5])
        self.assertIn(4, requested)

    def test_iso_day_range_is_recognized(self):
        """``2026-03-01 ~ 2026-07-10`` 표기가 연 단위로 넓어지면 안 된다."""
        expected = {"from": "2026-03-01", "to": "2026-07-10", "precision": "day_range"}
        for question in (
            "2026-03-01 ~ 2026-07-10 미래에셋증권 공수는 얼마야",
            "2026-03-01~2026-07-10 미래에셋증권 공수는 얼마야",
            "2026-03-01 부터 2026-07-10 까지 미래에셋증권 공수는 얼마야",
            "2026-03-01-2026-07-10 미래에셋증권 공수는 얼마야",
            "2026.3.1 ~ 2026.7.10 미래에셋증권 공수는 얼마야",
            "2026/03/01~2026/07/10 미래에셋증권 공수는 얼마야",
        ):
            with self.subTest(question=question):
                self.assertEqual(extract_created_at_range(question), expected)
                query = extract_effort_query(question)
                self.assertEqual(query["date_range"], expected)
                self.assertNotIn("period", query)

    def test_invalid_or_reversed_iso_range_is_not_guessed(self):
        """잘못된 구간을 그럴듯한 다른 범위로 바꿔치기하면 안 된다."""
        self.assertIsNone(extract_created_at_range("2026-07-10 ~ 2026-03-01 이슈"))
        self.assertIsNone(extract_created_at_range("2026-02-30 ~ 2026-07-10 이슈"))

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
