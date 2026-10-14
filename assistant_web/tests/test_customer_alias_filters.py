import unittest
from unittest.mock import patch

from backend.common import derive_customer_aliases_from_project
from backend.intent import entities
from backend.intent.matching import chroma_text_matches_constraints
from backend.query_filters import matches_source_filters
from backend.sources import mantis
from backend.understanding import extract_query_constraints


class CustomerAliasFilterTests(unittest.TestCase):
    def setUp(self):
        mantis.clear_mantis_recent_cache()
        self.addCleanup(mantis.clear_mantis_recent_cache)
        self.enterContext(patch.object(mantis, "read_complete_store", return_value=None))
        self.enterContext(patch.object(mantis, "get_mantis_settings", return_value={"url": "https://alias.test", "token": "test"}))

    def test_broker_name_precedes_product_alias_but_explicit_project_is_preserved(self):
        cases = [
            ("노션에서 한화 서버 자동주문 장애에 관한 건 알려줘", "한화"),
            ("KB증권 서버자동주문 이슈 보여줘", "KB증권"),
            ("서버자동주문 이슈 보여줘", "서버자동주문"),
            ("프로젝트 서버자동주문에서 한화 관련 이슈", "서버자동주문"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(extract_query_constraints(question)["customer_key"], expected)

    def test_live_and_snapshot_filters_accept_current_and_legacy_names(self):
        issue = {"id": 42, "summary": "차트 오류", "handler": {"real_name": "석지원"}, "project": {"name": "101PopcornRenewal(동부증권)"}}
        document = "이슈 ID: #42\n프로젝트(고객사): 101PopcornRenewal(동부증권)\n담당자: 석지원\n[제목]\n차트 오류\n\n"
        for customer in ("DB증권", "DB금융투자", "동부증권", "디비증권"):
            with self.subTest(customer=customer):
                constraints = extract_query_constraints(f"{customer} 최근 이슈 보여줘")
                self.assertTrue(mantis.issue_matches_constraints(issue, constraints))
                self.assertTrue(chroma_text_matches_constraints(document, constraints))
                with patch.object(mantis, "fetch_mantis_issues", return_value=[issue]):
                    stage = mantis.execute_mantis_page_scan("", constraints=constraints)
                self.assertEqual(stage["meta"]["matched_issue_ids"], [42])

    def test_alias_operators_and_unrelated_projects(self):
        for source in ("mantis", "chroma", "notion"):
            for operator, expected in (("equals", True), ("in", True), ("not_equals", False), ("not_in", False)):
                clause = {"field": "customer", "operator": operator, "value": "DB증권"}
                with self.subTest(source=source, operator=operator):
                    self.assertEqual(matches_source_filters(source, {"customer": "101PopcornRenewal(동부증권)"}, [clause]), expected)
                    self.assertEqual(matches_source_filters(source, {"customer": "112KBStar(KB증권)"}, [clause]), not expected)

    def test_short_alias_does_not_match_arbitrary_word(self):
        self.assertFalse(matches_source_filters("mantis", {"customer": "sandbox"}, [
            {"field": "customer", "operator": "equals", "value": "DB증권"},
        ]))


class RenamedCustomerFilterTests(unittest.TestCase):
    current_project = "163Hi-ib(하이투자증권)"
    legacy_project = "114Solomon(아이엠증권)"

    def setUp(self):
        # 실제 원본의 옛 표시명까지 동적 별칭으로 다시 읽는 경로를 검증한다.
        self.project_groups = tuple(
            tuple(derive_customer_aliases_from_project(project))
            for project in (self.legacy_project, self.current_project)
        )
        self.enterContext(patch.object(entities, "_load_project_alias_groups_cached", return_value=self.project_groups))
        entities._get_customer_alias_groups_cached.cache_clear()
        self.addCleanup(entities._get_customer_alias_groups_cached.cache_clear)

    def test_current_names_only_match_hi_in_live_and_saved_issues(self):
        for name in ("하이투자", "하이투자증권", "아이엠", "아이엠증권", "IM증권", "iM증권", "IM 증권"):
            constraints = extract_query_constraints(f"{name} 이슈 보여줘")
            self.assertIn("163Hi-ib", constraints["customer_aliases"])
            self.assertNotIn("114Solomon", constraints["customer_aliases"])
            for project, expected in ((self.current_project, True), (self.legacy_project, False)):
                with self.subTest(name=name, project=project):
                    issue = {"id": 42, "summary": "차트 오류", "handler": {"real_name": "석지원"}, "project": {"name": project}}
                    document = f"이슈 ID: #42\n프로젝트(고객사): {project}\n담당자: 석지원\n[제목]\n차트 오류\n\n"
                    self.assertEqual(mantis.issue_matches_constraints(issue, constraints), expected)
                    self.assertEqual(chroma_text_matches_constraints(document, constraints), expected)

    def test_legacy_project_remains_separate_regardless_of_snapshot_order(self):
        for groups in (self.project_groups, self.project_groups[::-1]):
            with patch.object(entities, "_load_project_alias_groups_cached", return_value=groups):
                entities._get_customer_alias_groups_cached.cache_clear()
                for name in ("솔로몬", "114Solomon", "IM투자증권", self.legacy_project):
                    with self.subTest(name=name, groups=groups):
                        constraints = extract_query_constraints(f"{name} 이슈 보여줘")
                        self.assertTrue(mantis.issue_matches_constraints(
                            {"handler": {"real_name": "석지원"}, "project": {"name": self.legacy_project}}, constraints,
                        ))
                        self.assertFalse(mantis.issue_matches_constraints(
                            {"handler": {"real_name": "석지원"}, "project": {"name": self.current_project}}, constraints,
                        ))

    def test_current_customer_filter_operators_do_not_include_legacy_project(self):
        for source in ("mantis", "chroma", "notion"):
            for name in ("아이엠증권", "IM증권", "하이투자"):
                for operator, expected in (("equals", True), ("in", True), ("not_equals", False), ("not_in", False)):
                    with self.subTest(source=source, name=name, operator=operator):
                        clause = {"field": "customer", "operator": operator, "value": name}
                        self.assertEqual(matches_source_filters(source, {"customer": self.current_project}, [clause]), expected)
                        self.assertEqual(matches_source_filters(source, {"customer": self.legacy_project}, [clause]), not expected)

    def test_excluding_current_customer_keeps_legacy_project(self):
        constraints = extract_query_constraints("아이엠증권 제외하고 이슈 보여줘")
        self.assertFalse(mantis.issue_matches_constraints({"handler": {"real_name": "석지원"}, "project": {"name": self.current_project}}, constraints))
        self.assertTrue(mantis.issue_matches_constraints({"handler": {"real_name": "석지원"}, "project": {"name": self.legacy_project}}, constraints))
