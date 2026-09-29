import json
import os
import unittest
from threading import Event
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from backend.notion import search
from backend.understanding import extract_query_constraints
from backend.notion.mapping import normalize_notion_page


class NotionSearchParsingTests(unittest.TestCase):
    def test_custom_status_exclusion_filters_returned_pages(self):
        for status in ("운영반영", "배포승인대기"):
            for ending in ("는 빼고", "를 제외하고", "가 아닌"):
                with self.subTest(status=status, ending=ending):
                    constraints = search.extract_notion_constraints(
                        f"노션에서 '2022-10-20 코스콤 버전처리' 항목을 찾아줘. {status} 상태{ending}"
                    )
                    self.assertEqual(constraints["excluded_statuses"], [status])
                    self.assertEqual(constraints["status"], "")
                    self.assertFalse(search.notion_page_matches_constraints(
                        {"title": "2022-10-20 코스콤 버전처리", "status": status}, constraints,
                    ))
                    self.assertTrue(search.notion_page_matches_constraints(
                        {"title": "2022-10-20 코스콤 버전처리", "status": "진행중"}, constraints,
                    ))

    def test_custom_status_exclusion_inside_title_is_not_a_filter(self):
        constraints = search.extract_notion_constraints("노션에서 '운영반영 상태는 빼고' 문서를 찾아줘")
        self.assertEqual(constraints["excluded_statuses"], [])

    def test_personal_owner_words_are_not_free_text_terms(self):
        constraints = {
            "owner": "석지원",
            "common_filter_clauses": [{
                "field": "owner", "operator": "equals", "value": "석지원",
            }],
        }
        terms = search.build_notion_terms(
            "노션에서 나랑 관련된 문서있어? 담당자: 석지원",
            constraints,
        )
        self.assertEqual(terms, ["석지원"])

    def test_owner_people_filter_uses_matching_notion_user(self):
        search._NOTION_USER_ID_HINTS.clear()
        with patch.object(search, "notion_property_schema", return_value={
            "담당자": {"type": "people"},
            "제목": {"type": "title"},
        }), patch.object(
            search,
            "fetch_notion_users",
            return_value=(("석지원", "user-1"), ("우동헌", "user-2")),
        ):
            decisive, api_filter = search.notion_database_owner_filter("db", "석지원")

        self.assertTrue(decisive)
        self.assertEqual(
            api_filter,
            {"property": "담당자", "people": {"contains": "user-1"}},
        )

    def test_owner_people_filter_learns_guest_id_from_recent_pages(self):
        search._NOTION_USER_ID_HINTS.clear()
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [{
                "properties": {
                    "담당자": {
                        "type": "people",
                        "people": [{"id": "guest-1", "name": "석지원"}],
                    },
                },
            }],
        }
        with patch.object(search, "notion_property_schema", return_value={
            "담당자": {"type": "people"},
            "제목": {"type": "title"},
        }), patch.object(
            search, "fetch_notion_users", return_value=()
        ), patch.object(
            search, "notion_post_json", return_value=response
        ) as post:
            decisive, api_filter = search.notion_database_owner_filter("db", "석지원")
            cached_decisive, cached_filter = search.notion_database_owner_filter(
                "other-db", "석지원"
            )

        self.assertTrue(decisive)
        self.assertEqual(
            api_filter,
            {"property": "담당자", "people": {"contains": "guest-1"}},
        )
        self.assertTrue(cached_decisive)
        self.assertEqual(cached_filter, api_filter)
        post.assert_called_once()

    def test_created_date_terms_are_removed_only_after_date_is_structured(self):
        clause = {
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }
        for question in (
            "노션에서 이번주 등록된 이슈 알려줘",
            "노션에서 지난주 등록된 이슈 알려줘",
            "노션에서 최근 3일 생성된 이슈 알려줘",
            "노션에서 오늘 만든 문서 알려줘",
            "노션에서 9월 작성된 항목 알려줘",
        ):
            self.assertEqual(
                search.build_notion_terms(question, {"filter_clauses": [clause]}),
                [],
                question,
            )
        self.assertIn("등록", search.build_notion_terms("사용자 등록 방법 문서"))
        expected_terms = {
            "이번주 등록된 등록오류 이슈": ["등록오류"],
            "이번주 등록된 작성자 이슈": ["작성자"],
            "이번주 등록된 등록방법 이슈": ["등록방법"],
            "이번주 등록된 운영 이슈": ["운영"],
            "이번주 등록된 반영 이슈": ["반영"],
        }
        for question, expected in expected_terms.items():
            self.assertEqual(
                search.build_notion_terms(question, {"filter_clauses": [clause]}),
                expected,
                question,
            )

    def test_notion_created_time_uses_kst_calendar_date(self):
        item = normalize_notion_page(
            {
                "id": "p1",
                "created_time": "2026-09-13T23:36:00Z",
                "properties": {"제목": {"type": "title", "title": [{"plain_text": "경계 이슈"}]}},
            },
            include_blocks=False,
        )
        self.assertEqual(item["created_time"], "2026-09-14")
        self.assertTrue(search.notion_page_matches_common_filters(item, [{
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }]))

    def test_customer_mapping_does_not_use_customer_request_date(self):
        item = normalize_notion_page(
            {
                "id": "p1",
                "properties": {
                    "제목": {"type": "title", "title": [{"plain_text": "고객 이슈"}]},
                    "고객사요청일": {
                        "type": "date", "date": {"start": "2026-09-10"},
                    },
                    "고객사": {
                        "type": "select", "select": {"name": "하나증권"},
                    },
                },
            },
            include_blocks=False,
        )
        self.assertEqual(item["customer"], "하나증권")

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
        self.assertFalse(search.notion_page_matches_common_filters(
            {
                "registration_date_property": "등록일",
                "registration_date": "",
                "created_time": "2026-08-25",
            },
            [{"field": "created_at", "operator": "between", "value": ["2026-08-01", "2026-08-31"]}],
        ))

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
        self.assertEqual(
            search.notion_search_max_pages(
                {"status": "진행"}, server_filter_applied=True,
            ),
            5,
        )
        self.assertEqual(
            search.notion_search_max_pages(
                {"status": "진행"}, server_filter_applied=True, require_complete=True,
            ),
            30,
        )
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
    def test_schema_failure_keeps_full_scan_page_size_and_does_not_short_circuit(self):
        response = Mock(status_code=200)
        response.json.return_value = {"results": [], "has_more": False}
        constraints = {"filter_clauses": [{
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }]}
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_property_schema", side_effect=HTTPException(status_code=502)),
            patch.object(search, "notion_post_json", return_value=response) as post,
        ):
            result = search.execute_notion_search(
                "노션에서 이번주 등록된 이슈 알려줘",
                constraints=constraints,
                include_blocks=False,
            )
        payload = post.call_args.args[1]
        self.assertEqual(payload["page_size"], 100)
        self.assertNotIn("filter", payload)
        self.assertFalse(result["meta"]["result_window_complete"])

    def test_cross_database_date_sort_uses_uniform_calendar_and_timestamp_keys(self):
        property_page = {
            "registration_date": "2026-09-15",
            "created_time": "2026-09-15",
            "created_time_raw": "2026-09-15T00:02:00Z",
            "match_score": 1,
        }
        timestamp_page = {
            "created_time": "2026-09-15",
            "created_time_raw": "2026-09-15T00:01:00Z",
            "match_score": 1,
        }
        property_key = search.notion_page_sort_key(property_page, created_at_filtered=True)
        timestamp_key = search.notion_page_sort_key(timestamp_page, created_at_filtered=True)
        self.assertIsInstance(property_key[0], str)
        self.assertIsInstance(property_key[1], float)
        self.assertGreater(property_key, timestamp_key)

    def test_created_filter_prefers_registration_property_and_exclusive_end(self):
        clause = {
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }
        with patch.object(search, "notion_property_schema", return_value={"등록일": {"type": "date"}}):
            api_filter, sort, window_safe = search.notion_database_created_filter("db", clause)
        self.assertEqual(api_filter, {"property": "등록일", "date": {
            "on_or_after": "2026-09-14", "before": "2026-09-21",
        }})
        self.assertEqual(sort, {"property": "등록일", "direction": "descending"})
        self.assertTrue(window_safe)

        with patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}):
            api_filter, sort, window_safe = search.notion_database_created_filter("db", clause)
        self.assertEqual(api_filter, {"timestamp": "created_time", "created_time": {
            "on_or_after": "2026-09-14T00:00:00+09:00",
            "before": "2026-09-21T00:00:00+09:00",
        }})
        self.assertEqual(sort, {"timestamp": "created_time", "direction": "descending"})
        self.assertTrue(window_safe)

        with patch.object(search, "notion_property_schema", return_value={
            "등록일": {"type": "date"}, "접수일": {"type": "date"},
        }):
            api_filter, sort, window_safe = search.notion_database_created_filter("db", clause)
        self.assertEqual(api_filter, {"or": [
            {"property": "등록일", "date": {"on_or_after": "2026-09-14", "before": "2026-09-21"}},
            {"property": "접수일", "date": {"on_or_after": "2026-09-14", "before": "2026-09-21"}},
        ]})
        self.assertEqual(sort, {"property": "등록일", "direction": "descending"})
        self.assertFalse(window_safe)

    def test_multiple_registration_fields_are_scanned_and_matched_with_the_same_or_contract(self):
        first = Mock(status_code=200)
        first.json.return_value = {"results": [], "has_more": True, "next_cursor": "next"}
        second = Mock(status_code=200)
        second.json.return_value = {
            "results": [{
                "id": "received-only",
                "created_time": "2026-01-01T00:00:00Z",
                "properties": {
                    "제목": {"type": "title", "title": [{"plain_text": "접수일 항목"}]},
                    "등록일": {"type": "date", "date": None},
                    "접수일": {"type": "date", "date": {"start": "2026-09-15"}},
                },
            }],
            "has_more": False,
        }
        constraints = {"filter_clauses": [{
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }]}
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_property_schema", return_value={
                "제목": {"type": "title"},
                "등록일": {"type": "date"},
                "접수일": {"type": "date"},
            }),
            patch.object(search, "notion_post_json", side_effect=[first, second]) as post,
        ):
            result = search.execute_notion_search(
                "노션에서 이번주 등록된 이슈 알려줘",
                constraints=constraints,
                include_blocks=False,
            )
        self.assertEqual(post.call_count, 2)
        self.assertTrue(all(call.args[1]["page_size"] == 100 for call in post.call_args_list))
        self.assertEqual([item["id"] for item in result["notion_pages"]], ["received-only"])
        self.assertEqual(result["notion_pages"][0]["registration_dates"], ["2026-09-15"])
        self.assertFalse(result["meta"]["result_window_complete"])

    def test_created_filter_is_sent_and_results_are_sorted_by_registration_date(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [
                {"id": "old", "created_time": "2026-09-14T10:00:00Z", "properties": {
                    "제목": {"type": "title", "title": [{"plain_text": "이전"}]},
                    "등록일": {"type": "date", "date": {"start": "2026-09-14"}},
                }},
                {"id": "new", "created_time": "2026-09-15T10:00:00Z", "properties": {
                    "제목": {"type": "title", "title": [{"plain_text": "최신"}]},
                    "등록일": {"type": "date", "date": {"start": "2026-09-15"}},
                }},
            ],
            "has_more": True,
            "next_cursor": "more",
        }
        constraints = {"filter_clauses": [{
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }]}
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_property_schema", return_value={
                "제목": {"type": "title"}, "등록일": {"type": "date"},
            }),
            patch.object(search, "notion_post_json", return_value=response) as post,
        ):
            result = search.execute_notion_search(
                "노션에서 이번주 등록된 이슈 알려줘",
                constraints=constraints,
                include_blocks=False,
            )
        payload = post.call_args.args[1]
        self.assertEqual(payload["filter"], {"property": "등록일", "date": {
            "on_or_after": "2026-09-14", "before": "2026-09-21",
        }})
        self.assertEqual(payload["sorts"], [{"property": "등록일", "direction": "descending"}])
        self.assertEqual(result["meta"]["terms"], [])
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertTrue(result["meta"]["result_window_complete"])
        self.assertEqual([item["id"] for item in result["notion_pages"]], ["new", "old"])

    def test_created_count_query_reads_all_server_filtered_pages(self):
        first = Mock(status_code=200)
        first.json.return_value = {
            "results": [{"id": "p1", "created_time": "2026-09-15T01:00:00Z", "properties": {
                "제목": {"type": "title", "title": [{"plain_text": "첫 항목"}]},
            }}],
            "has_more": True,
            "next_cursor": "next",
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "results": [{"id": "p2", "created_time": "2026-09-14T23:36:00Z", "properties": {
                "제목": {"type": "title", "title": [{"plain_text": "둘째 항목"}]},
            }}],
            "has_more": False,
        }
        constraints = {"filter_clauses": [{
            "field": "created_at", "operator": "between",
            "value": ["2026-09-14", "2026-09-20"],
        }]}
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=[first, second]) as post,
        ):
            result = search.execute_notion_search(
                "노션에서 이번주 등록된 이슈 몇 개야",
                constraints=constraints,
                include_blocks=False,
            )
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["meta"]["candidate_count"], 2)
        self.assertTrue(result["meta"]["candidate_count_complete"])
        self.assertFalse(result["meta"]["result_window_complete"])

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

    def test_customer_filter_uses_matching_database_option(self):
        properties = {
            "고객사": {
                "type": "select",
                "select": {"options": [
                    {"name": "134Hanaw(하나금융)"},
                    {"name": "132SHGM(신한굿모닝증권)"},
                ]},
            },
        }
        clause = {"field": "customer", "operator": "equals", "value": "하나금융"}
        with patch.object(search, "notion_property_schema", return_value=properties):
            api_filter = search.notion_database_customer_filter(
                "db", [clause], ["하나금융", "134Hanaw(하나금융)"],
            )
        self.assertEqual(api_filter, {
            "property": "고객사",
            "select": {"equals": "134Hanaw(하나금융)"},
        })

    def test_server_filtered_customer_list_stops_after_first_result_window(self):
        calls = 0

        def response_for_page(_url, _payload, **_kwargs):
            nonlocal calls
            calls += 1
            response = Mock(status_code=200)
            response.json.return_value = {
                "results": [],
                "has_more": True,
                "next_cursor": f"cursor-{calls}",
            }
            return response

        constraints = {
            "customer_aliases": ["하나금융", "134Hanaw(하나금융)"],
            "filter_clauses": [{
                "field": "customer", "operator": "equals", "value": "하나금융",
            }],
        }
        properties = {
            "제목": {"type": "title"},
            "고객사": {
                "type": "select",
                "select": {"options": [{"name": "134Hanaw(하나금융)"}]},
            },
        }
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_property_schema", return_value=properties),
            patch.object(search, "notion_post_json", side_effect=response_for_page) as post,
        ):
            result = search.execute_notion_search(
                "노션에서 하나금융 고객사 항목 보여줘",
                constraints=constraints,
                include_blocks=False,
            )
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args_list[0].args[1]["filter"], {
            "property": "고객사",
            "select": {"equals": "134Hanaw(하나금융)"},
        })
        self.assertEqual(post.call_args_list[0].args[1]["page_size"], 5)
        self.assertEqual(result["meta"]["scan_limit_per_database"], 500)
        self.assertTrue(result["meta"]["search_complete"])
        self.assertTrue(result["meta"]["result_window_complete"])
        self.assertFalse(result["meta"]["candidate_count_complete"])

    def test_body_scan_budget_is_global_and_shared_across_databases(self):
        def response_for_query(url, payload, **_kwargs):
            response = Mock(status_code=200)
            if "filter" in payload:
                response.json.return_value = {"results": [], "has_more": False}
                return response
            database_id = url.split("/databases/")[1].split("/")[0]
            response.json.return_value = {
                "results": [
                    {
                        "id": f"{database_id}-{index}",
                        "title": f"문서 {index}",
                        "search_text": "관련 없는 속성",
                        "properties_text": "",
                        "last_edited_time": f"2026-09-{(index % 20) + 1:02d}",
                    }
                    for index in range(20)
                ],
                "has_more": False,
            }
            return response

        def normalize(raw, **kwargs):
            return {**raw, **kwargs}

        with (
            patch.object(search, "get_notion_settings", return_value={
                "token": "t", "database_ids": ["db1", "db2"],
            }),
            patch.object(search, "resolve_notion_read_scope", return_value=({
                "db1": "db1", "db2": "db2",
            }, True)),
            patch.object(search, "fetch_notion_database_title", side_effect=lambda value: value),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=response_for_query),
            patch.object(search, "normalize_notion_page", side_effect=normalize),
            patch.object(search, "fetch_notion_page_blocks", return_value="needle 본문") as blocks,
        ):
            result = search.execute_notion_search(
                "needle 문서 찾아줘",
                limit=100,
                planned_terms=["needle"],
                include_blocks=False,
            )
        self.assertEqual(blocks.call_count, 30)
        self.assertEqual(result["meta"]["body_scan_attempted_count"], 30)
        self.assertEqual(result["meta"]["body_scan_limit"], 30)
        self.assertEqual(
            {page["database_id"] for page in result["notion_pages"]},
            {"db1", "db2"},
        )
        self.assertIn("body_scan_limit", result["meta"]["incomplete_reasons"])

    def test_body_scan_stops_at_limit_without_incomplete_warning(self):
        def response_for_query(_url, payload, **_kwargs):
            response = Mock(status_code=200)
            response.json.return_value = {
                "results": [] if "filter" in payload else [
                    {
                        "id": f"page-{index}",
                        "title": f"문서 {index}",
                        "search_text": "관련 없는 속성",
                        "properties_text": "",
                        "last_edited_time": f"2026-09-{(index % 20) + 1:02d}",
                    }
                    for index in range(100)
                ],
                "has_more": False,
            }
            return response

        with (
            patch.object(search, "get_notion_settings", return_value={
                "token": "t", "database_ids": ["db"],
            }),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="기술공유"),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=response_for_query),
            patch.object(
                search, "normalize_notion_page",
                side_effect=lambda raw, **kwargs: {**raw, **kwargs},
            ),
            patch.object(search, "fetch_notion_page_blocks", return_value="needle 본문") as blocks,
        ):
            result = search.execute_notion_search(
                "needle 문서 찾아줘",
                limit=5,
                planned_terms=["needle"],
                include_blocks=False,
            )
        self.assertEqual(blocks.call_count, 5)
        self.assertEqual(result["meta"]["body_scan_attempted_count"], 5)
        self.assertEqual(result["meta"]["displayed_count"], 5)
        self.assertTrue(result["meta"]["search_complete"])
        self.assertTrue(result["meta"]["result_window_complete"])
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertNotIn("body_scan_limit", result["meta"]["incomplete_reasons"])

    def test_result_body_fetch_uses_short_summary_budget(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [{
                "id": "p1",
                "title": "배포 계획",
                "search_text": "배포 계획",
                "last_edited_time": "2026-09-15",
            }],
            "has_more": False,
        }
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", return_value=response),
            patch.object(
                search, "normalize_notion_page",
                side_effect=lambda raw, **kwargs: {**raw, **kwargs},
            ),
            patch.object(search, "fetch_notion_page_blocks", return_value="본문") as blocks,
        ):
            result = search.execute_notion_search(
                '노션에서 "배포 계획" 내용 알려줘',
                include_blocks=True,
            )
        blocks.assert_called_once_with("p1", max_blocks=30)
        self.assertEqual(result["notion_pages"][0]["content"], "본문")

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

        db1_requested = Event()

        def respond(url, payload, **_kwargs):
            # 색인 DB(db2)의 응답이 db1보다 늦게 끝나도 병합은 db2가 먼저여야 한다.
            if "/databases/db1/" in url:
                db1_requested.set()
                return newly_created_response
            self.assertTrue(db1_requested.wait(timeout=3))
            return indexed_response

        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db1", "db2"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db1": "db1", "db2": "db2"}, True)),
            patch.object(search, "fetch_notion_database_title", side_effect=lambda value: {"db1": "DB 1", "db2": "DB 2"}[value]),
            patch.object(search, "indexed_exact_title_database_ids", return_value=["db2"]),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.dict(os.environ, {"NOTION_SEARCH_MAX_WORKERS": "4"}),
            patch.object(search, "notion_post_json", side_effect=respond) as post,
            patch.object(search, "normalize_notion_page", side_effect=normalize),
        ):
            result = search.execute_notion_search('노션에서 "배포 계획" 찾아줘', include_blocks=False)

        # DB는 동시에 조회하므로 요청 순서가 아니라 병합 순서로 색인 우선을 확인한다.
        self.assertEqual(post.call_count, 2)
        self.assertEqual([meta["database_id"] for meta in result["meta"]["databases"]], ["db2", "db1"])
        self.assertEqual({page["id"] for page in result["notion_pages"]}, {"p1", "p2"})
        self.assertEqual(result["notion_pages"][0]["match_method"], "notion_exact_title_filter")
        self.assertTrue(result["meta"]["exact_title_index_hit"])

    def test_full_scan_stops_after_first_database_with_results_even_when_prefetched(self):
        """제목 검색이 비었으면 2차 검색은 결과가 나온 DB까지만 반영한다.

        뒤 DB를 미리 조회했더라도 그 결과가 섞이면 순차 처리와 달라진다.
        """
        queried = []

        def respond(url, payload, **_kwargs):
            database_id = url.split("/databases/")[1].split("/")[0]
            queried.append((database_id, "filter" in payload))
            response = Mock(status_code=200)
            if "filter" in payload:
                response.json.return_value = {"results": [], "has_more": False}
            else:
                response.json.return_value = {"results": [{
                    "id": f"p-{database_id}",
                    "title": f"신입사원 안내 {database_id}",
                    "search_text": f"신입사원 안내 {database_id}",
                    "last_edited_time": "2026-09-10",
                }], "has_more": False}
            return response

        with (
            patch.object(search, "get_notion_settings", return_value={
                "token": "t", "database_ids": ["db1", "db2", "db3"],
            }),
            patch.object(
                search,
                "resolve_notion_read_scope",
                return_value=({"db1": "db1", "db2": "db2", "db3": "db3"}, True),
            ),
            patch.object(search, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(search, "indexed_title_term_database_ids", return_value=[]),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.dict(os.environ, {"NOTION_SEARCH_MAX_WORKERS": "4"}),
            patch.object(search, "notion_post_json", side_effect=respond),
            patch.object(
                search,
                "normalize_notion_page",
                side_effect=lambda raw, **kwargs: {**raw, **kwargs},
            ),
        ):
            result = search.execute_notion_search(
                "노션에서 신입사원 관련 문서 찾아줘",
                planned_terms=["신입사원"],
                include_blocks=False,
            )

        self.assertEqual([page["id"] for page in result["notion_pages"]], ["p-db1"])
        self.assertEqual(
            [(meta["database_id"], meta.get("phase")) for meta in result["meta"]["databases"]],
            [("db1", "title"), ("db2", "title"), ("db3", "title"), ("db1", None)],
        )
        self.assertTrue(result["meta"]["title_search_only"])
        self.assertEqual(sorted(db for db, filtered in queried if filtered), ["db1", "db2", "db3"])

    def test_title_search_checks_unindexed_databases_before_skipping_body_scan(self):
        targeted = Mock(status_code=200)
        new_title_response = self._new_title_response()
        targeted.json.return_value = {
            "results": [
                {
                    "id": f"p{index}",
                    "title": f"신입사원 교육 {index}",
                    "search_text": f"신입사원 교육 {index}",
                    "last_edited_time": f"2026-09-{20 - index:02d}",
                }
                for index in range(3)
            ],
            "has_more": False,
        }
        with (
            patch.object(search, "get_notion_settings", return_value={
                "token": "t", "database_ids": ["db1", "db2"],
            }),
            patch.object(
                search,
                "resolve_notion_read_scope",
                return_value=({"db1": "db1", "db2": "db2"}, True),
            ),
            patch.object(search, "fetch_notion_database_title", return_value="주요이슈현황"),
            patch.object(search, "indexed_title_term_database_ids", return_value=["db2"]),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=lambda url, *_args, **_kwargs: (
                targeted if "/databases/db2/" in url else new_title_response
            )) as post,
            patch.object(
                search,
                "normalize_notion_page",
                side_effect=lambda raw, **kwargs: {**raw, **kwargs},
            ),
        ):
            result = search.execute_notion_search(
                "노션에서 신입사원 관련 문서 찾아줘",
                planned_terms=["신입사원"],
                include_blocks=False,
            )

        self.assertEqual(post.call_count, 2)
        self.assertEqual([meta["database_id"] for meta in result["meta"]["databases"]], ["db2", "db1"])
        self.assertTrue(all("filter" in call.args[1] for call in post.call_args_list))
        self.assertEqual(result["meta"]["displayed_count"], 4)
        self.assertTrue(result["meta"]["search_complete"])
        self.assertNotIn("page_limit", result["meta"]["incomplete_reasons"])
        self.assertTrue(result["meta"]["result_window_complete"])
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertTrue(result["meta"]["title_term_index_hit"])
        self.assertEqual(result["notion_pages"][0]["id"], "new-page")

    @staticmethod
    def _new_title_response():
        response = Mock(status_code=200)
        response.json.return_value = {"results": [{
            "id": "new-page", "title": "신입사원 새 교육", "search_text": "신입사원 새 교육",
            "last_edited_time": "2026-09-22",
        }], "has_more": False}
        return response

    def test_title_matches_do_not_skip_full_scan_for_counts(self):
        targeted = self._new_title_response()
        scanned = Mock(status_code=200)
        scanned.json.return_value = {"results": [{
            "id": "body-page", "title": "교육 안내", "search_text": "신입사원 안내",
        }], "has_more": False}
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="문서"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=[targeted, scanned]) as post,
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **kwargs: {**raw, **kwargs}),
        ):
            result = search.execute_notion_search("신입사원 문서 몇 개야", planned_terms=["신입사원"], include_blocks=False)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result["meta"]["candidate_count"], 2)
        self.assertFalse(result["meta"]["title_search_only"])

    def test_one_database_schema_failure_keeps_other_database_results(self):
        def title(database_id):
            if database_id == "broken":
                raise requests.ReadTimeout("slow schema")
            return "문서"

        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["good", "broken"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"good": "good", "broken": "broken"}, True)),
            patch.object(search, "fetch_notion_database_title", side_effect=title),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", return_value=self._new_title_response()),
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **kwargs: {**raw, **kwargs}),
        ):
            result = search.execute_notion_search("신입사원 문서 찾아줘", planned_terms=["신입사원"], include_blocks=False)
        self.assertEqual(result["status"], "partial_error")
        self.assertEqual(result["notion_pages"][0]["id"], "new-page")
        self.assertFalse(result["meta"]["search_complete"])
        self.assertIn("query_error", result["meta"]["incomplete_reasons"])

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

    def test_schema_timeout_falls_back_to_database_scan(self):
        scanned = Mock(status_code=200)
        scanned.json.return_value = {
            "results": [
                {
                    "id": "p1",
                    "title": "신입사원 안내",
                    "search_text": "신입사원 안내",
                    "properties_text": "온보딩 문서",
                    "last_edited_time": "2026-09-21",
                }
            ],
            "has_more": False,
        }
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db1", "db2"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db1": "db1", "db2": "db2"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="사내규정 Wiki"),
            patch.object(search, "notion_property_schema", side_effect=[
                requests.ReadTimeout("slow schema"),
                {"제목": {"type": "title"}},
            ]) as schema,
            patch.object(search, "notion_post_json", return_value=scanned) as post,
            patch.object(
                search,
                "normalize_notion_page",
                side_effect=lambda raw, **kwargs: {**raw, **kwargs},
            ),
        ):
            result = search.execute_notion_search(
                "노션에서 신입사원 관련 문서 찾아줘",
                planned_terms=["신입사원"],
                include_blocks=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notion_pages"][0]["title"], "신입사원 안내")
        self.assertEqual(schema.call_count, 2)
        self.assertEqual(post.call_count, 3)

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


def compound_levels(api_filter):
    """검사 대상 코드와 독립적으로 and/or 중첩 단계를 센다."""
    if not isinstance(api_filter, dict):
        return 0
    children = api_filter.get("and") or api_filter.get("or")
    if children is None:
        return 0
    return 1 + max((compound_levels(child) for child in children), default=0)


def fake_notion_query(pages):
    """노션처럼 복합 필터가 두 단계를 넘으면 400으로 거부하는 가짜 query API."""
    payloads = []

    def respond(url, payload, **_kwargs):
        payloads.append(payload)
        response = Mock()
        if compound_levels(payload.get("filter")) > 2:
            response.status_code = 400
            response.text = '{"code":"validation_error"}'
            response.raise_for_status.side_effect = requests.HTTPError("400 Client Error: Bad Request")
            return response
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.return_value = {"results": pages, "has_more": False}
        return response

    return respond, payloads


class NotionFilterNestingTests(unittest.TestCase):
    STATUS_ANY = {"or": [
        {"property": "이슈진행", "status": {"equals": "시작전"}},
        {"property": "QC진행 ", "status": {"equals": "시작전"}},
    ]}
    CUSTOMER = {"property": "고객사", "select": {"equals": "케이프투자증권"}}

    def test_and_filter_flattens_nested_and_but_keeps_or(self):
        title = {"property": "이슈내용", "title": {"equals": "정기점검"}}
        combined = search.notion_and_filter(title, {"and": [self.STATUS_ANY, self.CUSTOMER]}, None)
        self.assertEqual(combined, {"and": [title, self.STATUS_ANY, self.CUSTOMER]})
        self.assertEqual(search.notion_filter_levels(combined), 2)
        self.assertEqual(search.notion_and_filter(title), title)
        self.assertIsNone(search.notion_and_filter(None, None))

    def test_title_query_falls_back_to_title_filter_when_conditions_exceed_limit(self):
        title = {"property": "이슈내용", "title": {"contains": "배포"}}
        date_by_property = {"or": [
            {"and": [{"property": name, "date": {"on_or_after": "2026-09-01"}},
                     {"property": name, "date": {"on_or_before": "2026-09-30"}}]}
            for name in ("배포일", "운영배포일")
        ]}
        self.assertEqual(search.notion_title_query_filter(title, date_by_property), title)
        self.assertEqual(
            search.notion_title_query_filter(title, self.STATUS_ANY),
            {"and": [title, self.STATUS_ANY]},
        )

    def test_exact_title_with_two_status_properties_and_customer_is_not_rejected(self):
        """상태 속성이 둘인 DB에서 정확한 제목 + 상태 + 고객사 조회가 400이 나던 회귀."""
        page = {"id": "p1", "title": "케이프증권 2026.09 정기점검",
                "search_text": "케이프증권 2026.09 정기점검", "last_edited_time": "2026-09-20"}
        respond, payloads = fake_notion_query([page])
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_database_status_filter", return_value=(True, self.STATUS_ANY)),
            patch.object(search, "notion_database_customer_filter", return_value=self.CUSTOMER),
            patch.object(search, "notion_property_schema", return_value={"이슈내용": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=respond),
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **kwargs: {**raw, **kwargs}),
            patch.object(search, "notion_page_matches_and_enrich", return_value=True),
        ):
            result = search.execute_notion_search(
                "노션에서 상태가 시작전인 '케이프증권 2026.09 정기점검' 항목을 정확히 찾아줘",
                include_blocks=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["id"] for item in result["notion_pages"]], ["p1"])
        exact_filter = payloads[0]["filter"]
        self.assertLessEqual(compound_levels(exact_filter), 2)
        # 펼치기만 했을 뿐 조건은 하나도 빠지지 않는다.
        self.assertIn(self.STATUS_ANY, exact_filter["and"])
        self.assertIn(self.CUSTOMER, exact_filter["and"])

    def test_date_filter_beyond_nesting_limit_is_checked_locally_instead(self):
        date_by_property = {"or": [
            {"and": [{"property": name, "date": {"on_or_after": "2026-09-01"}},
                     {"property": name, "date": {"on_or_before": "2026-09-30"}}]}
            for name in ("배포일", "운영배포일")
        ]}
        page = {"id": "p1", "title": "배포 항목", "search_text": "배포 항목", "last_edited_time": "2026-09-20"}
        respond, payloads = fake_notion_query([page])
        local_constraints = []

        def matches(candidate, constraints):
            local_constraints.append(constraints)
            return True

        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="배포관리"),
            patch.object(search, "notion_database_status_filter", return_value=(True, self.STATUS_ANY)),
            patch.object(search, "notion_database_date_filter", return_value=date_by_property),
            patch.object(search, "notion_database_customer_filter", return_value=None),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=respond),
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **kwargs: {**raw, **kwargs}),
            patch.object(search, "notion_page_matches_and_enrich", side_effect=matches),
        ):
            result = search.execute_notion_search(
                "노션에서 상태가 시작전인 항목 보여줘",
                constraints={"filter_clauses": [{
                    "field": "deployed_at", "operator": "between",
                    "value": ["2026-09-01", "2026-09-30"],
                }]},
                include_blocks=False,
            )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(payloads)
        self.assertTrue(all(compound_levels(item.get("filter")) <= 2 for item in payloads))
        # 서버에는 상태 조건만 보내고, 배포일 조건은 로컬 검사로 넘어간다.
        self.assertEqual(payloads[-1]["filter"], self.STATUS_ANY)
        self.assertTrue(any(
            clause.get("field") == "deployed_at"
            for constraints in local_constraints
            for clause in constraints.get("common_filter_clauses", [])
        ))


def effective_notion_terms(question):
    """실제 검색 경로처럼 구조화된 조건을 넘겨 만든 노션 검색어."""
    constraints = search.merge_common_notion_constraints(
        search.extract_notion_constraints(question), extract_query_constraints(question),
    )
    return search.remove_structured_date_terms(search.build_notion_terms(question, constraints), constraints)


class NotionDateTermTests(unittest.TestCase):
    def test_spaced_or_particle_date_phrases_do_not_leak_into_search_terms(self):
        """띄어 쓴 기간 표현의 조각("달에", "주에", "중에" 등)이 제목 검색어로 남던 회귀."""
        for phrase in (
            "이번 달에", "이번 달엔", "이번 달부터", "이번 달 중에", "이번 달 동안", "이번 달 안에",
            "지난 달에", "저번 달에", "지난달엔", "이번 주에", "이번 주엔", "지난 주에",
            "이번 해에", "올해", "올해에", "작년에", "올해 들어", "오늘자", "9월 중에",
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(effective_notion_terms(f"노션에서 {phrase} 만든 문서 보여줘"), [])

    def test_real_terms_next_to_date_phrases_are_kept(self):
        self.assertEqual(effective_notion_terms("노션에서 이번 달에 만든 장애보고 문서 보여줘"), ["장애보고"])
        self.assertEqual(effective_notion_terms("노션에서 올해 들어 등록된 KRX 연장 문서 찾아줘"), ["KRX", "연장"])
        self.assertEqual(effective_notion_terms("노션에서 9월 중에 만든 서버 점검 문서"), ["서버", "점검"])

    def test_range_words_are_kept_when_no_date_is_structured(self):
        self.assertIn("동안", effective_notion_terms("노션에서 점검 동안 발생한 장애 찾아줘"))

    def test_this_month_document_list_is_queried_by_date_only(self):
        question = "노션에서 이번 달에 만든 문서 보여줘"
        respond, payloads = fake_notion_query([])
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="이슈리스트"),
            patch.object(search, "notion_property_schema", return_value={
                "제목": {"type": "title"}, "등록일": {"type": "date"},
            }),
            patch.object(search, "notion_post_json", side_effect=respond),
        ):
            result = search.execute_notion_search(
                question, constraints=extract_query_constraints(question), include_blocks=False,
            )

        self.assertEqual(result["meta"]["terms"], [])
        self.assertTrue(payloads)
        for payload in payloads:
            # 제목 검색 조건 없이 등록일 조건만 서버에 보낸다.
            self.assertNotIn('"title"', json.dumps(payload.get("filter"), ensure_ascii=False))
            self.assertIn("등록일", json.dumps(payload.get("filter"), ensure_ascii=False))

