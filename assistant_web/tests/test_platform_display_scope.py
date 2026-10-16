"""매체 표시 요청은 조회 매체를 한쪽으로 제한하지 않는다."""

import unittest

from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.query_planning import build_query_plan
from backend.sources.mantis import issue_matches_constraints


class PlatformDisplayScopeTests(unittest.TestCase):
    QUERY = (
        "한화투자증권의 2026년 5월 등록 이슈에서 제목에 정기점검이 들어간 것만 몇 건인지 확인해줘. "
        "우리 팀 범위에서 HTS/MTS 구분이 보이도록 번호와 제목을 알려줘."
    )
    INCLUSIVE_QUERY = (
        "신한투자증권에서 2026년 7월에 등록된 우리 팀 이슈가 총 몇 건인지 번호와 제목으로 정리해줘. "
        "HTS와 MTS 모두 포함해줘."
    )

    def test_both_platforms_included_in_count(self):
        rows = [
            {"id": 31248, "summary": "HTS 회선 대응", "project": {"name": "132SHGM(신한굿모닝증권)"},
             "status": {"name": "closed"}, "handler": {"real_name": "석지원"}, "created_at": "2026-07-06",
             "custom_fields": [{"field": {"name": "매체구분"}, "value": "HTS"}]},
            {"id": 31249, "summary": "MTS 회선 대응", "project": {"name": "132SHGM(신한굿모닝증권)"},
             "status": {"name": "closed"}, "handler": {"real_name": "정희진"}, "created_at": "2026-07-06",
             "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]},
        ]
        for question in (
            self.INCLUSIVE_QUERY,
            self.INCLUSIVE_QUERY.replace("HTS와 MTS 모두", "MTS와 HTS 모두"),
            self.INCLUSIVE_QUERY.replace("HTS와 MTS 모두", "HTS/MTS 둘 다"),
        ):
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                plan = build_query_plan(question, {"source": "mantis"}, constraints)
                merged = merge_query_plan_constraints(constraints, plan)
                self.assertIsNone(constraints["platform_key"])
                self.assertEqual(constraints["excluded_platform_keys"], [])
                self.assertFalse(any(c["field"] == "platform" for c in merged["filter_clauses"]))
                self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)], [31248, 31249])

    def test_dual_platform_display_retains_both_matching_issues(self):
        constraints = extract_query_constraints(self.QUERY)
        plan = build_query_plan(self.QUERY, {"source": "mantis"}, constraints, {
            "planned": True, "source": "mantis", "operation": "count",
            "filter_clauses": [{"field": "title", "operator": "contains", "value": "정기점검"}],
        })
        merged = merge_query_plan_constraints(constraints, plan)
        rows = [
            {"id": 31060, "summary": "한화증권 2026.05 정기점검(MTS)", "project": {"name": "123Hanhwa(한화증권)"},
             "status": {"name": "closed"}, "handler": {"real_name": "석지원"}, "created_at": "2026-05-26",
             "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]},
            {"id": 31061, "summary": "한화증권 2026.05 정기점검(HTS)", "project": {"name": "123Hanhwa(한화증권)"},
             "status": {"name": "closed"}, "handler": {"real_name": "정희진"}, "created_at": "2026-05-26",
             "custom_fields": [{"field": {"name": "매체구분"}, "value": "HTS"}]},
            {"id": 31062, "summary": "한화증권 2026.05 기타 문의", "project": {"name": "123Hanhwa(한화증권)"},
             "status": {"name": "closed"}, "handler": {"real_name": "정희진"}, "created_at": "2026-05-26",
             "custom_fields": [{"field": {"name": "매체구분"}, "value": "HTS"}]},
        ]
        self.assertIsNone(constraints["platform_key"])
        self.assertEqual(constraints["excluded_platform_keys"], [])
        self.assertFalse(any(c["field"] == "platform" for c in merged["filter_clauses"]))
        self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)], [31060, 31061])

    def test_explicit_single_platform_and_exclusion_survive_display_phrase(self):
        for question, platform, excluded in (
            ("한화 HTS 정기점검 이슈", "hts", []),
            ("한화 MTS 정기점검 이슈", "mts", []),
            ("한화 HTS만 보여줘. HTS/MTS 구분하여 표시해줘", "hts", []),
            ("한화 HTS 제외 MTS 이슈를 보여줘", "mts", ["hts"]),
            ("한화 HTS만 보여줘. HTS와 MTS 모두 포함해줘", "hts", []),
            ("한화 MTS만 보여줘. MTS와 HTS 둘 다 포함해줘", "mts", []),
        ):
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                self.assertEqual(constraints["platform_key"], platform)
                self.assertEqual(constraints["excluded_platform_keys"], excluded)


if __name__ == "__main__":
    unittest.main()
