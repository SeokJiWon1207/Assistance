import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from backend.notion import search
from backend.notion.mapping import normalize_notion_page


class NotionSearchParsingTests(unittest.TestCase):
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

    def test_title_search_checks_unindexed_databases_before_skipping_body_scan(self):
        targeted = Mock(status_code=200)
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
            patch.object(search, "notion_post_json", side_effect=[targeted, self._new_title_response()]) as post,
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
        self.assertIn("/databases/db2/query", post.call_args_list[0].args[0])
        self.assertIn("/databases/db1/query", post.call_args_list[1].args[0])
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
