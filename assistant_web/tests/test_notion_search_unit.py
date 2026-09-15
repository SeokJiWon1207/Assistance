import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from backend.notion import search


class NotionSearchParsingTests(unittest.TestCase):
    def test_terms_typo_context_and_constraints_are_normalized(self):
        corrected, typo = search.correct_notion_status_typo("결제요청 문서")
        self.assertEqual(corrected, "결재요청 문서")
        self.assertEqual(typo, ("결제요청", "결재요청"))
        self.assertEqual(search.correct_notion_status_typo("정상 문서"), ("정상 문서", None))

        terms = search.build_notion_terms("노션에서 배포 체크리스트 3건 보여줘 배포")
        self.assertEqual(terms, ["배포", "체크리스트"])
        self.assertEqual(search.extract_notion_database_label_from_text("주요이슈 현황"), "주요이슈")

        with patch.object(search, "load_known_people", return_value=(["권한별"], [])):
            self.assertEqual(search.trim_owner_adnominal("권한별인"), "권한별")

        constraints = search.extract_notion_constraints(
            '주요이슈에서 "배포 계획" 상태는 결재요청 말고 담당자 없는 항목을 담당자별로 보여줘'
        )
        self.assertEqual(constraints["exact_title"], "배포 계획")
        self.assertTrue(constraints["unassigned_owner"])
        self.assertIn("결재요청", constraints["excluded_statuses"])
        self.assertEqual(constraints["group_by"], "owner")

        inherited = search.inherit_notion_context(
            ["담당자별로 묶어줘", "노션에서 결재요청 상태 항목 보여줘"]
        )
        self.assertIn("결재요청", inherited)
        self.assertIn("노션", inherited)
        self.assertEqual(search.inherit_notion_context(["한 문장"]), "")

    def test_common_constraints_and_filter_operators_are_applied(self):
        merged = search.merge_common_notion_constraints(
            {"excluded_statuses": ["보관됨"], "status": "", "owner": ""},
            {
                "filter_clauses": [
                    {"field": "status", "operator": "equals", "value": "진행 중"},
                    {"field": "owner", "operator": "equals", "value": "홍길동"},
                    {"field": "database", "operator": "contains", "value": "업무"},
                    {"field": "title", "operator": "equals", "value": "배포"},
                    {"field": "status", "operator": "not_in", "value": ["완료"]},
                ],
                "excluded_statuses": ["종료"],
            },
        )
        self.assertEqual(merged["status"], "진행 중")
        self.assertEqual(merged["owner"], "홍길동")
        self.assertEqual(merged["database_label"], "업무")
        self.assertEqual(merged["exact_title"], "배포")
        self.assertEqual(merged["excluded_statuses"], ["보관됨", "종료", "완료"])

        page = {
            "customer": "신한투자증권",
            "platform": "MTS",
            "status": "진행 중",
            "owner": "홍길동",
            "created_time": "2026-08-25T10:00:00Z",
            "title": "배포 점검",
            "search_text": "신한투자증권 MTS 배포 점검",
            "database_label": "업무 DB",
        }
        clauses = [
            {"field": "customer", "operator": "equals", "value": "신한투자증권"},
            {"field": "platform", "operator": "contains", "value": "MTS"},
            {"field": "status", "operator": "not_equals", "value": "완료"},
            {"field": "owner", "operator": "exists", "value": True},
            {"field": "created_at", "operator": "between", "value": ["2026-08-01", "2026-08-31"]},
            {"field": "title", "operator": "not_contains", "value": "장애"},
        ]
        self.assertTrue(search.notion_page_matches_common_filters(page, clauses))
        self.assertFalse(
            search.notion_page_matches_common_filters(
                page,
                [{"field": "owner", "operator": "not_exists", "value": True}],
            )
        )

    def test_page_matching_scoring_and_collection(self):
        page = {
            "id": "p1",
            "database_label": "업무 DB",
            "title": "배포 계획",
            "status": "진행 중",
            "owner": "홍길동",
            "search_text": "배포 계획 진행 중 홍길동",
        }
        constraints = {
            "database_label": "업무",
            "exact_title": "배포 계획",
            "status": "진행",
            "owner": "홍길동",
            "excluded_statuses": [],
        }
        self.assertTrue(search.notion_page_matches_constraints(page, constraints))
        self.assertEqual(search.notion_exact_match_score(page, constraints), 135)
        self.assertGreater(search.notion_page_match_score(page, ["배포", "홍길동"]), 0)
        self.assertTrue(search.notion_page_matches_terms(page, ["배포", "진행"] ))
        self.assertEqual(search.notion_minimum_match_score(["a", "b", "c"]), 2)
        self.assertEqual(search.notion_search_max_pages({"status": "진행"}), 30)
        self.assertEqual(search.notion_search_max_pages({}), 3)

        with patch.object(search, "resolve_notion_page_owner") as resolve:
            self.assertTrue(search.notion_page_matches_and_enrich(page, {**constraints, "group_by": "owner"}))
        resolve.assert_called_once_with(page)

        pages = search.deduplicate_notion_pages_by_id([page, page, {"id": ""}, {"id": "p2"}])
        self.assertEqual([item["id"] for item in pages], ["p1", "p2"])
        collected = search.collect_retrieved_notion_pages(
            {"stages": [{"notion_pages": [page, page, {"id": "p2"}]}]},
            2,
        )
        self.assertEqual([item["id"] for item in collected], ["p1", "p2"])


class NotionSearchExecutionTests(unittest.TestCase):
    def test_status_filter_uses_exact_option_and_fails_open_on_schema_error(self):
        properties = {
            "업무 상태": {
                "type": "status",
                "status": {"options": [{"name": "진행 중"}, {"name": "완료"}]},
            }
        }
        with patch.object(search, "fetch_notion_database_schema", return_value={"properties": properties}):
            supported, api_filter = search.notion_database_status_filter("db", "완료")
            self.assertTrue(supported)
            self.assertEqual(api_filter, {"property": "업무 상태", "status": {"equals": "완료"}})
            self.assertEqual(search.notion_database_status_filter("db", "없는 상태"), (True, None))
        with patch.object(search, "fetch_notion_database_schema", side_effect=requests.Timeout("slow")):
            self.assertEqual(search.notion_database_status_filter("db", "완료"), (False, None))

    def test_search_skips_when_not_configured(self):
        with patch.object(search, "get_notion_settings", return_value={"token": "", "database_ids": []}):
            result = search.execute_notion_search("배포")
        self.assertEqual(result["status"], "skipped")

    def test_exact_title_search_returns_server_filtered_page(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [
                {
                    "id": "p1",
                    "title": "배포 계획",
                    "search_text": "배포 계획",
                    "last_edited_time": "2026-08-25",
                }
            ]
        }
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_title_property", return_value="제목"),
            patch.object(search, "notion_post_json", return_value=response),
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **_kwargs: dict(raw)),
        ):
            result = search.execute_notion_search('노션에서 "배포 계획" 찾아줘', include_blocks=False)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notion_pages"][0]["match_method"], "notion_exact_title_filter")

    def test_exact_title_index_prioritizes_match_without_skipping_other_databases(self):
        indexed_response = Mock(status_code=200)
        indexed_response.json.return_value = {
            "results": [{
                "id": "p1", "title": "배포 계획", "search_text": "배포 계획",
                "last_edited_time": "2026-08-25",
            }], "has_more": False,
        }
        newly_created_response = Mock(status_code=200)
        newly_created_response.json.return_value = {
            "results": [{
                "id": "p2", "title": "배포 계획", "search_text": "배포 계획",
                "last_edited_time": "2026-09-15",
            }], "has_more": False,
        }

        def normalize(raw, **kwargs):
            return {**raw, **kwargs}

        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db1", "db2"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db1": "db1", "db2": "db2"}, True)),
            patch.object(search, "fetch_notion_database_title", side_effect=lambda value: {"db1": "DB 1", "db2": "DB 2"}[value]),
            patch.object(search, "indexed_exact_title_database_ids", return_value=["db2"]),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=[indexed_response, newly_created_response]) as post,
            patch.object(search, "normalize_notion_page", side_effect=normalize),
        ):
            result = search.execute_notion_search('노션에서 "배포 계획" 찾아줘', include_blocks=False)

        self.assertEqual(post.call_count, 2)
        self.assertIn("/databases/db2/query", post.call_args_list[0].args[0])
        self.assertIn("/databases/db1/query", post.call_args_list[1].args[0])
        self.assertEqual({page["id"] for page in result["notion_pages"]}, {"p1", "p2"})
        self.assertEqual(result["notion_pages"][0]["match_method"], "notion_exact_title_filter")
        self.assertTrue(result["meta"]["exact_title_index_hit"])

    def test_general_search_uses_title_then_body_fallback(self):
        targeted = Mock(status_code=200)
        targeted.json.return_value = {"results": []}
        scanned = Mock(status_code=200)
        scanned.json.return_value = {
            "results": [
                {
                    "id": "p1",
                    "title": "운영 문서",
                    "search_text": "운영 문서",
                    "properties_text": "속성",
                    "last_edited_time": "2026-08-25",
                }
            ],
            "has_more": False,
        }
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_title_property", return_value="제목"),
            patch.object(search, "notion_post_json", side_effect=[targeted, scanned]),
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **_kwargs: dict(raw)),
            patch.object(search, "fetch_notion_page_blocks", return_value="배포 체크리스트 본문"),
        ):
            result = search.execute_notion_search(
                "배포 체크리스트",
                planned_terms=["배포", "체크리스트"],
                include_blocks=False,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notion_pages"][0]["match_method"], "notion_body_scan")
        self.assertIn("배포 체크리스트 본문", result["notion_pages"][0]["content"])

    def test_database_query_error_is_reported_without_raising(self):
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={}),
            patch.object(search, "notion_title_property", side_effect=HTTPException(status_code=502)),
            patch.object(search, "notion_post_json", side_effect=requests.Timeout("slow")),
        ):
            result = search.execute_notion_search("", planned_terms=[], include_blocks=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("scan failed", result["reason"])


if __name__ == "__main__":
    unittest.main()
