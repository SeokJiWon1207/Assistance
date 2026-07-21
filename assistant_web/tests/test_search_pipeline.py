import unittest
from unittest.mock import Mock, patch

import backend.app as backend_app
from backend.app import (
    build_effort_report,
    build_effort_report_from_plan,
    evaluate_ab_comparison,
    deduplicate_notion_pages_by_id,
    extract_effort_query,
    extract_notion_constraints,
    extract_query_constraints,
    merge_combined_sources,
    notion_page_matches_constraints,
    notion_search_max_pages,
    notion_database_status_filter,
    format_grouped_notion_answer,
    resolve_notion_page_owner,
    resolve_result_limit,
    rule_based_search_intent,
    source_specific_terms,
    wants_effort_ranking,
)


class SearchIntentTests(unittest.TestCase):
    def route(self, question: str):
        constraints = extract_query_constraints(question)
        return rule_based_search_intent(question, constraints)

    def test_greeting_is_general(self):
        self.assertEqual(self.route("하이")["source"], "general")

    def test_structured_customer_issue_is_mantis(self):
        self.assertEqual(self.route("KB증권 [0600] 확인된 이슈 찾아줘")["source"], "mantis")

    def test_explicit_cross_source_query_is_both(self):
        result = self.route("노션 공지와 Mantis 장애 이력을 함께 비교해줘")
        self.assertEqual(result["source"], "both")
        self.assertGreaterEqual(result["confidence"], 0.9)

    def test_notion_domain_query(self):
        self.assertEqual(self.route("회식 관련 문서 찾아줘")["source"], "notion")

    def test_notion_status_does_not_route_to_mantis(self):
        self.assertEqual(
            self.route("주요이슈현황에서 결재완료(팀장) 항목 보여줘")["source"],
            "notion",
        )


class ExactConstraintTests(unittest.TestCase):
    def test_negated_closed_status_is_open(self):
        for question in (
            "석지원 이슈 중 종료안된 이슈있어?",
            "석지원 이슈 중 종료 안 된 이슈 있어?",
            "석지원 이슈 중 종료되지 않은 이슈 있어?",
            "석지원 이슈 중 종료가 되지 않은 이슈 있어?",
            "석지원 이슈 중 closed가 아닌 이슈 있어?",
            "석지원 미종료 이슈 보여줘",
        ):
            with self.subTest(question=question):
                self.assertEqual(extract_query_constraints(question)["statuses"], ["open"])

    def test_positive_closed_status_remains_closed(self):
        self.assertEqual(
            extract_query_constraints("석지원의 종료된 이슈 보여줘")["statuses"],
            ["closed"],
        )

    def test_specific_open_status_is_not_broadened(self):
        self.assertEqual(
            extract_query_constraints("종료 안 된 할당 이슈 보여줘")["statuses"],
            ["assigned"],
        )

    def test_developed_status_maps_to_dev_done(self):
        self.assertEqual(
            extract_query_constraints("NH증권에서 상태가 developed인 최근 이슈 2개 보여줘")["statuses"],
            ["dev_done"],
        )

    def test_quoted_notion_title_keeps_inner_apostrophes_and_no_inner_filters(self):
        question = "노션에서 '[1892]  '일중 등락폭 비교' 신규조건' 항목을 찾아서 알려줘"
        constraints = extract_notion_constraints(question)
        self.assertEqual(constraints["exact_title"], "[1892]  '일중 등락폭 비교' 신규조건")
        self.assertEqual(constraints["status"], "")
        self.assertEqual(constraints["owner"], "")

    @patch("backend.retrieval.execute_notion_search")
    def test_quoted_notion_title_drops_filters_inferred_from_title(self, notion_search):
        notion_search.return_value = {
            "source": "notion",
            "status": "ok",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }
        question = "노션에서 '[MTS] 김호길 확인요청' 항목을 찾아서 알려줘"
        result = backend_app.run_retrieval_pipeline(
            question,
            5,
            {"mantis": False, "notion": True, "vectorDb": False},
            {"source": "notion", "terms": ["MTS", "김호길", "확인요청"]},
        )

        passed_constraints = notion_search.call_args.args[2]
        self.assertIsNone(passed_constraints["platform_key"])
        self.assertEqual(passed_constraints["statuses"], [])
        self.assertEqual(passed_constraints["people"], {"names": [], "role": None})
        self.assertEqual(result["constraints"], passed_constraints)

    @patch("backend.sources.notion.notion_property_schema")
    @patch("backend.sources.notion.fetch_notion_database_title", return_value="이슈리스트_테스트")
    @patch("backend.sources.notion.get_notion_settings")
    @patch("backend.sources.notion.notion_post_json")
    def test_exact_notion_title_stops_after_server_side_title_query(
        self, notion_post, notion_settings, _database_title, property_schema
    ):
        title = "[MTS] 저장한 차트 적용 및 확인요청"
        notion_settings.return_value = {"token": "token", "database_ids": ["db-id"]}
        property_schema.return_value = {"제목": {"type": "title"}}
        notion_post.return_value = Mock(
            status_code=200,
            text="",
            json=lambda: {
                "results": [
                    {
                        "id": "page-id",
                        "url": "https://notion.so/page-id",
                        "created_time": "2026-07-01T00:00:00Z",
                        "last_edited_time": "2026-07-01T00:00:00Z",
                        "properties": {
                            "제목": {
                                "type": "title",
                                "title": [{"plain_text": title}],
                            }
                        },
                    }
                ]
            },
        )

        with patch.dict("os.environ", {"NOTION_FETCH_PAGE_BLOCKS": "false"}):
            result = backend_app.execute_notion_search(
                f"노션에서 '{title}' 항목을 찾아서 알려줘",
                limit=5,
                constraints={},
            )

        self.assertEqual(notion_post.call_count, 1)
        self.assertEqual([page["title"] for page in result["notion_pages"]], [title])
        self.assertEqual(result["meta"]["scanned_count"], 1)

    def test_krx_domain_term_is_not_legacy_customer(self):
        constraints = extract_query_constraints("KRX 제도개선 관련 이슈 찾아줘")
        self.assertIsNone(constraints["customer_key"])
        self.assertEqual(constraints["customer_aliases"], [])

    def test_explicit_legacy_krx_customer_is_preserved(self):
        constraints = extract_query_constraints("119KRX 고객사 이슈 찾아줘")
        self.assertIsNotNone(constraints["customer_key"])

    def test_full_project_name_uses_exact_customer_constraint(self):
        constraints = extract_query_constraints("900DRFN(공용)에서 closed 이슈 보여줘")
        self.assertEqual(constraints["customer_exact"], "900DRFN(공용)")

    def test_hana_alias_and_all_results_are_structured(self):
        question = "작년 하나증권 멘티스 이슈리스트 다 보여줘 개발 문의건 포함해서"
        constraints = extract_query_constraints(question)
        self.assertIn("하나증권", constraints["customer_aliases"])
        self.assertEqual(constraints["created_at"]["from"], "2025-01-01")
        self.assertEqual(constraints["created_at"]["to"], "2025-12-31")
        self.assertEqual(resolve_result_limit(question, 5), 200)

    def test_explicit_technical_handler_is_not_customer(self):
        constraints = extract_query_constraints("담당자 DRFN_NEXT의 최근 이슈 보여줘")
        self.assertIsNone(constraints["customer_key"])
        self.assertEqual(constraints["people"]["names"], ["DRFN_NEXT"])
        self.assertEqual(constraints["people"]["role"], "handler")

    def test_notion_status_is_structured(self):
        constraints = extract_notion_constraints("주요이슈현황에서 결재완료(팀장) 항목 보여줘")
        self.assertEqual(constraints["status"], "결재완료(팀장)")
        self.assertTrue(
            notion_page_matches_constraints(
                {"database_label": "주요이슈현황_테스트", "status": "결재완료(팀장)", "owner": "", "title": "오후반차"},
                constraints,
            )
        )

    def test_notion_owner_grouping_is_not_a_person_filter(self):
        question = "노션에서 결재요청 상태인 항목을 담당자별로 정리해줘"
        general_constraints = extract_query_constraints(question)
        notion_constraints = extract_notion_constraints(question)

        self.assertEqual(general_constraints["people"], {"names": [], "role": None})
        self.assertEqual(notion_constraints["status"], "결재요청")
        self.assertEqual(notion_constraints["owner"], "")
        self.assertEqual(notion_constraints["group_by"], "owner")
        self.assertEqual(notion_search_max_pages(notion_constraints), 30)
        self.assertEqual(notion_search_max_pages({}), 3)

    @patch("backend.sources.notion.fetch_notion_relation_title", return_value="석지원")
    def test_relation_backed_owner_is_resolved(self, fetch_title):
        page = {
            "owner": "담당자 미지정",
            "owner_relation_ids": ["042779f1-cd9f-4421-b0a6-f3554369a7c9"],
            "search_text": "휴가\n결재요청",
        }
        resolved = resolve_notion_page_owner(page)
        self.assertEqual(resolved["owner"], "석지원")
        self.assertIn("석지원", resolved["search_text"])
        fetch_title.assert_called_once()

    @patch("backend.sources.notion.fetch_notion_user_name", return_value="우동헌")
    def test_people_backed_owner_id_is_resolved(self, fetch_name):
        page = {
            "owner": "담당자 미지정",
            "owner_relation_ids": [],
            "owner_user_ids": ["042779f1-cd9f-4421-b0a6-f3554369a7c9"],
            "owner_user_name_hints": {},
            "search_text": "휴가\n결재요청",
        }
        resolved = resolve_notion_page_owner(page)
        self.assertEqual(resolved["owner"], "우동헌")
        fetch_name.assert_called_once()

    def test_notion_pages_with_same_title_are_kept_by_page_id(self):
        pages = [
            {"id": "page-1", "title": "휴가"},
            {"id": "page-2", "title": "휴가"},
            {"id": "page-1", "title": "휴가"},
        ]
        deduplicated = deduplicate_notion_pages_by_id(pages)
        self.assertEqual([page["id"] for page in deduplicated], ["page-1", "page-2"])

    @patch("backend.sources.notion.fetch_notion_database_schema")
    def test_notion_status_filter_uses_matching_select_property(self, fetch_schema):
        fetch_schema.return_value = {
            "properties": {
                "구상태": {
                    "type": "select",
                    "select": {"options": [{"name": "결재요청"}, {"name": "완료"}]},
                },
                "상태": {
                    "type": "status",
                    "status": {"options": [{"name": "시작 전"}]},
                },
            }
        }
        supported, status_filter = notion_database_status_filter("db-id", "결재요청")
        self.assertTrue(supported)
        self.assertEqual(
            status_filter,
            {"property": "구상태", "select": {"equals": "결재요청"}},
        )

    def test_grouped_notion_answer_organizes_pages_by_owner(self):
        pages = [
            {"id": "page-1", "title": "휴가", "owner": "석지원", "status": "결재요청", "database_label": "주요이슈현황"},
            {"id": "page-2", "title": "휴가", "owner": "우동헌", "status": "결재요청", "database_label": "주요이슈현황"},
        ]
        stage = {"meta": {"candidate_count": 2}}
        answer = format_grouped_notion_answer(pages, stage, "owner")
        self.assertIn("총 2건", answer)
        self.assertIn("석지원 — 1건", answer)
        self.assertIn("우동헌 — 1건", answer)


class EffortIntentTests(unittest.TestCase):
    def test_ranking_regex_catches_common_phrasings(self):
        self.assertTrue(wants_effort_ranking("작년 기준으로 공수가 제일 많은 사람은 누구야"))
        self.assertTrue(wants_effort_ranking("공수 순위 보여줘"))
        self.assertFalse(wants_effort_ranking("석지원 공수 알려줘"))

    def test_roster_phrasing_is_treated_as_ranking(self):
        # "공수 기입한 사람들 다 보여줘"는 전원 명단 요청 → 랭킹 경로로 처리한다.
        from backend.app import wants_full_effort_roster
        self.assertTrue(wants_effort_ranking("작년에 공수 기입한 사람들 다 보여줘"))
        self.assertTrue(wants_effort_ranking("공수 낸 사람들 알려줘"))
        self.assertTrue(wants_full_effort_roster("작년에 공수 기입한 사람들 다 보여줘"))
        self.assertFalse(wants_full_effort_roster("공수 순위 보여줘"))
        # 공수 무관하거나 특정 인물 조회는 랭킹이 아니다.
        self.assertFalse(wants_effort_ranking("신한 이슈 보여줘"))

    def test_unresolved_person_defers_to_planner(self):
        # 규칙이 인물을 못 찾으면 즉시 에러가 아니라 needsPerson 표식을 남겨
        # 파이프라인이 LLM 플래너 폴백을 시도할 수 있게 한다.
        report = build_effort_report("작년 공수 현황 궁금해")
        self.assertIsNotNone(report)
        self.assertTrue(report.get("needsPerson"))

    def test_non_effort_question_returns_none(self):
        self.assertIsNone(build_effort_report("신한 MTS 최근 이슈 보여줘"))

    def test_planner_ranking_intent_runs_deterministic_core(self):
        with patch(
            "backend.reports.effort.run_effort_ranking_report", return_value={"content": "RANKED"}
        ) as mock_rank:
            result = build_effort_report_from_plan(
                "작년 공수 젤 많이 낸 사람 궁금해",
                {"effort": {"type": "ranking"}, "planned": True},
            )
        self.assertEqual(result, {"content": "RANKED"})
        self.assertEqual(mock_rank.call_args[0][1], 2025)

    def test_planner_hallucinated_person_is_blocked(self):
        with patch("backend.reports.effort.load_known_people", return_value=(("석지원",), ())):
            result = build_effort_report_from_plan(
                "홍길동 공수 알려줘",
                {"effort": {"type": "person", "person": "홍길동"}},
            )
        self.assertIsNone(result)

    def test_planner_known_person_runs_deterministic_core(self):
        with patch("backend.reports.effort.load_known_people", return_value=(("석지원",), ())), patch(
            "backend.reports.effort.run_person_effort_report", return_value={"content": "PERSON"}
        ) as mock_person:
            result = build_effort_report_from_plan(
                "석지원 작년 공수",
                {"effort": {"type": "person", "person": "석지원"}},
            )
        self.assertEqual(result, {"content": "PERSON"})
        self.assertEqual(mock_person.call_args[0][1], "석지원")

    def test_plan_without_effort_field_is_ignored(self):
        self.assertIsNone(build_effort_report_from_plan("이슈 보여줘", {"planned": True}))


class CombinedSearchTests(unittest.TestCase):
    def test_combined_query_uses_source_specific_terms(self):
        question = "노션 공지와 Mantis 장애 이력을 함께 비교해줘"
        notion_terms = source_specific_terms(question, "notion", {})
        mantis_terms = source_specific_terms(question, "mantis", {})
        self.assertIn("공지", notion_terms)
        self.assertNotIn("장애", notion_terms)
        self.assertIn("장애", mantis_terms)
        self.assertNotIn("공지", mantis_terms)

    def test_duplicate_titles_are_merged_across_sources(self):
        sources = [
            {"type": "notion", "title": "[0600] 차트 추세선 오류"},
            {"type": "mantis", "title": "[0600] 차트 추세선 오류", "issue_id": 30891},
            {"type": "mantis", "title": "다른 장애", "issue_id": 30892},
        ]
        merged = merge_combined_sources(sources)
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["matched_sources"], ["notion", "mantis"])


class AbEvaluationTests(unittest.TestCase):
    def test_ab_evaluation_records_winner_and_failures(self):
        source = {
            "type": "mantis",
            "issue_id": 30891,
            "title": "[0600] 차트 추세선 다르게 표시되는 현상",
            "project": "112KBStar(KB증권)",
            "status": "confirmed",
            "handler": "이상신",
        }
        result = evaluate_ab_comparison(
            "KB증권 [0600] 확인된 이슈 1건 보여줘",
            {"status": "ok", "content": "#30891 [0600] 차트 추세선 다르게 표시되는 현상", "sources": [source]},
            {"status": "ok", "content": "#99999 다른 이슈", "sources": []},
        )
        self.assertEqual(result["winner"], "A")
        self.assertGreater(result["variantA"]["score"], result["variantB"]["score"])
        self.assertTrue(result["variantB"]["failureReasons"])


if __name__ == "__main__":
    unittest.main()
