import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import answers, retrieval, understanding
from backend.sources import mantis, notion
from backend.answers import (
    build_issue_intro_line,
    count_group_key,
    format_count_group_lines,
)
from backend.schemas import ChatMessage
from backend.query_planning import extract_group_by
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.reports.effort import (
    build_effort_report,
    build_effort_report_from_plan,
    wants_full_effort_roster,
    wants_effort_ranking,
)
from backend.reports.evaluation import evaluate_ab_comparison
from backend.retrieval import merge_combined_sources, source_specific_terms
from backend.sources.notion import (
    deduplicate_notion_pages_by_id,
    extract_notion_constraints,
    format_grouped_notion_answer,
    notion_database_status_filter,
    notion_page_matches_constraints,
    notion_properties_to_text,
    notion_search_max_pages,
    resolve_notion_page_owner,
)
from backend.understanding import (
    extract_query_constraints,
    is_follow_up_question,
    resolve_result_limit,
    rule_based_search_intent,
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
        result = retrieval.run_retrieval_pipeline(
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
            result = notion.execute_notion_search(
                f"노션에서 '{title}' 항목을 찾아서 알려줘",
                limit=5,
                constraints={},
            )

        self.assertEqual(notion_post.call_count, 1)
        self.assertEqual([page["title"] for page in result["notion_pages"]], [title])
        self.assertEqual(result["meta"]["scanned_count"], 1)

    def test_korean_transliterated_customer_is_recognized(self):
        # "케이비 증권 모바일"이 KB증권을 못 잡고 903모바일 프로젝트로
        # 넘어가던 회귀. 음차 표기도 고객사로 인식해야 한다.
        constraints = extract_query_constraints("케이비 증권 모바일 이슈 좀 찾아줘")
        self.assertIn("KB증권", constraints["customer_aliases"])
        self.assertEqual(extract_query_constraints("디비증권 이슈")["customer_key"], "디비증권")

    def test_mobile_is_read_as_mts_platform(self):
        # "케이비 증권 모바일"은 KB증권+MTS로 읽혀야 한다. 예전에는 모바일이
        # 903모바일 프로젝트로 잡혀 고객사가 통째로 바뀌었다.
        constraints = extract_query_constraints("케이비 증권 모바일 이슈 좀 찾아줘")
        self.assertIn("KB증권", constraints["customer_aliases"])
        self.assertEqual(constraints["platform_key"], "mts")
        self.assertEqual(
            extract_query_constraints("신한투자증권 엠티에스 이슈 세 개만")["platform_key"],
            "mts",
        )

    def test_bare_mobile_is_not_a_customer(self):
        constraints = extract_query_constraints("모바일 이슈 보여줘")
        self.assertIsNone(constraints["customer_key"])
        self.assertEqual(constraints["platform_key"], "mts")

    def test_explicit_mobile_project_is_still_a_customer(self):
        self.assertEqual(
            extract_query_constraints("903모바일 프로젝트 이슈")["customer_key"], "903모바일"
        )

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
    @staticmethod
    def effort_issue(issue_id, project, media, created_at, effort_value):
        return {
            "id": issue_id,
            "project": {"name": project},
            "summary": f"공수 테스트 {issue_id}",
            "status": {"name": "closed"},
            "created_at": created_at,
            "custom_fields": [
                {"field": {"name": "매체구분"}, "value": media},
                {"field": {"name": "1문의공수"}, "value": effort_value},
            ],
        }

    def test_ranking_regex_catches_common_phrasings(self):
        self.assertTrue(wants_effort_ranking("작년 기준으로 공수가 제일 많은 사람은 누구야"))
        self.assertTrue(wants_effort_ranking("공수 순위 보여줘"))
        self.assertFalse(wants_effort_ranking("석지원 공수 알려줘"))

    def test_roster_phrasing_is_treated_as_ranking(self):
        # "공수 기입한 사람들 다 보여줘"는 전원 명단 요청 → 랭킹 경로로 처리한다.
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

    def test_customer_platform_effort_query_sums_all_people_without_person_prompt(self):
        matching = self.effort_issue(
            101,
            "120NH(NH증권)",
            "MTS",
            "2025-07-01T09:00:00",
            "석지원(1.5) 정희진(2)",
        )
        wrong_platform = self.effort_issue(
            102,
            "120NH(NH증권)",
            "HTS",
            "2025-07-02T09:00:00",
            "석지원(10)",
        )
        wrong_customer = self.effort_issue(
            103,
            "123Hanhwa(한화증권)",
            "MTS",
            "2025-07-03T09:00:00",
            "석지원(20)",
        )

        with patch(
            "backend.reports.effort.fetch_mantis_issues",
            side_effect=[[matching, wrong_platform, wrong_customer], []],
        ):
            report = build_effort_report("2025년 NH MTS 공수총합 알려줘")

        self.assertNotIn("needsPerson", report)
        self.assertIn("120NH(NH증권) · MTS 공수 집계", report["content"])
        self.assertIn("총 공수: **3.50**", report["content"])
        self.assertEqual(report["effortChart"]["totalEffort"], 3.5)
        self.assertEqual(report["effortChart"]["issueCount"], 1)
        self.assertEqual([source["issue_id"] for source in report["sources"]], [101])
        self.assertEqual(report["raw"]["mode"], "deterministic_filtered_effort_report")

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


class NotionContextInheritanceTests(unittest.TestCase):
    def test_follow_up_inherits_notion_status_and_source(self):
        # "담당자별로 묶어줘"가 상태와 소스를 모두 잃고 0건이 되던 회귀.
        prefix = notion.inherit_notion_context(
            ["담당자별로 묶어줘", "노션에서 결재요청 상태인 항목 보여줘"]
        )
        self.assertTrue(prefix)
        constraints = extract_notion_constraints(f"{prefix} 담당자별로 묶어줘")
        self.assertEqual(constraints["status"], "결재요청")
        self.assertEqual(constraints["group_by"], "owner")

    def test_latest_turn_with_its_own_status_is_not_overridden(self):
        self.assertEqual(
            notion.inherit_notion_context(
                ["노션에서 보관됨 항목 보여줘", "노션에서 결재요청 상태인 항목 보여줘"]
            ),
            "",
        )

    def test_non_notion_conversation_inherits_nothing(self):
        self.assertEqual(notion.inherit_notion_context(["KB증권 이슈 보여줘"]), "")


class LiveStatusRevalidationTests(unittest.TestCase):
    def test_english_live_status_names_count_as_open(self):
        # 스냅샷은 상태를 한글로, 라이브 API는 영문으로 준다. 영문 이름이
        # open에 없어 재검증에서 열린 이슈가 전부 탈락하던 회귀.
        for status in ("confirmed", "assigned", "acknowledged", "new", "resolved"):
            with self.subTest(status=status):
                self.assertTrue(mantis.text_matches_status(status, ["open"]))

    def test_closed_is_never_open(self):
        self.assertFalse(mantis.text_matches_status("closed", ["open"]))
        self.assertFalse(mantis.text_matches_status("종료된 이슈", ["open"]))
        self.assertTrue(mantis.text_matches_status("closed", ["closed"]))

    def test_revalidation_keeps_live_open_issue(self):
        constraints = extract_query_constraints("KB증권 MTS 이슈 중 종료되지 않은 최근 5개 보여줘")
        stage = {
            "source": "mantis_snapshot",
            "status": "ok",
            "issues_list": [{"id": 30977, "status": "확인된 이슈"}],
            "meta": {"displayed_count": 1},
        }
        live_issue = {
            "id": 30977,
            "status": {"name": "confirmed"},
            "project": {"name": "112KBStar(KB증권)"},
            "summary": "[MTS] 차트 매입평균가 표시 기능 추가 요청",
            "handler": {"real_name": "하영훈"},
        }
        with patch("backend.sources.mantis.fetch_mantis_issue_by_id", return_value=live_issue):
            refreshed = mantis.revalidate_mantis_snapshot_stage(stage, constraints)
        self.assertEqual(len(refreshed["issues_list"]), 1)


class TopCustomersTests(unittest.TestCase):
    def test_vague_most_problems_question_is_detected(self):
        for question in (
            "요즘 문제 많은 고객사가 어디야?",
            "이슈가 많은 고객사 알려줘",
            "고객사 중에 문제 많은 곳",
        ):
            with self.subTest(question=question):
                self.assertTrue(answers.TOP_CUSTOMER_QUESTION.search(question))

    def test_normal_questions_are_not_matched(self):
        for question in ("KB증권 이슈 보여줘", "고객사별로 열린 이슈 수를 정리해줘"):
            with self.subTest(question=question):
                self.assertIsNone(answers.build_top_customers_report(question))

    def test_report_states_the_basis_it_used(self):
        # 기준이 빠진 질문에 임의 기준으로 조용히 답하면 안 된다.
        report = answers.build_top_customers_report("요즘 문제 많은 고객사가 어디야?")
        self.assertIsNotNone(report)
        self.assertIn("미종료", report["content"])
        self.assertIn("고객사별 건수", report["content"])


class TableOutputTests(unittest.TestCase):
    def test_table_requests_are_detected(self):
        for question in (
            "최근 이슈 5개를 ID, 고객사, 상태, 담당자 표로 만들어줘",
            "표 형태로 보여줘",
            "테이블로 정리해줘",
        ):
            with self.subTest(question=question):
                self.assertTrue(answers.wants_table_output(question))

    def test_normal_requests_are_not_table_requests(self):
        self.assertFalse(answers.wants_table_output("최근 이슈 5개 보여줘"))
        self.assertFalse(answers.wants_table_output("KB증권 이슈 목록"))


class UnknownCustomerTests(unittest.TestCase):
    def test_unregistered_customer_is_detected(self):
        # 미등록 고객사를 조용히 버리면 조건 없는 전체 검색이 되어 전혀 다른
        # 고객사 이슈가 답으로 나가던 회귀.
        constraints = extract_query_constraints("존재하지않는증권사123 최근 이슈 보여줘")
        self.assertIsNone(constraints["customer_key"])
        self.assertTrue(constraints["unknown_customers"])

    def test_known_customers_are_not_flagged(self):
        for question in ("유진증권 이슈", "한국투자증권 이슈", "하나금융 이슈", "KB증권 이슈"):
            with self.subTest(question=question):
                self.assertEqual(
                    extract_query_constraints(question)["unknown_customers"], []
                )

    def test_generic_words_are_not_unknown_customers(self):
        for question in ("증권사별로 정리해줘", "고객사별 이슈", "최근 이슈 보여줘"):
            with self.subTest(question=question):
                self.assertEqual(
                    extract_query_constraints(question)["unknown_customers"], []
                )

    def test_unknown_customer_report_refuses_to_substitute_results(self):
        report = answers.build_unknown_customer_report("존재하지않는증권사123 최근 이슈 보여줘")
        self.assertIsNotNone(report)
        self.assertEqual(report["sources"], [])
        self.assertIn("찾지 못했어요", report["content"])

    def test_known_customer_has_no_unknown_report(self):
        self.assertIsNone(answers.build_unknown_customer_report("유진증권 최근 이슈 보여줘"))
        self.assertIsNone(answers.build_unknown_customer_report("최근 이슈 보여줘"))


class NotionStatusTypoTests(unittest.TestCase):
    def test_payment_typo_is_corrected_to_approval_status(self):
        # 결재(승인)와 결제(지불)는 발음이 같아 자주 섞인다.
        constraints = extract_notion_constraints("노션에서 결제요청 상태 찾아줘")
        self.assertEqual(constraints["status"], "결재요청")
        self.assertEqual(constraints["status_typo"], ("결제요청", "결재요청"))

    def test_correct_spelling_reports_no_correction(self):
        constraints = extract_notion_constraints("노션에서 결재요청 상태 찾아줘")
        self.assertEqual(constraints["status"], "결재요청")
        self.assertIsNone(constraints["status_typo"])

    def test_quoted_title_is_not_corrected(self):
        # 인용한 제목은 사용자가 지정한 정확한 문자열이다.
        constraints = extract_notion_constraints("노션에서 '결제요청 매뉴얼' 문서 찾아줘")
        self.assertEqual(constraints["exact_title"], "결제요청 매뉴얼")
        self.assertIsNone(constraints["status_typo"])


class NotionPropertyTextTests(unittest.TestCase):
    def test_derived_and_system_columns_are_left_out_of_body(self):
        # 주차 계산·필터 수식·시스템 타임스탬프는 사람이 읽을 값이 아니다.
        properties = {
            "내용": {"type": "title", "title": [{"type": "text", "text": {"content": "버전 처리 NG"}}]},
            "작성주차": {"type": "formula", "formula": {"type": "number", "number": 202626}},
            "종료일 On/Off": {"type": "formula", "formula": {"type": "number", "number": 0}},
            "등록일": {"type": "created_time", "created_time": "2026-06-24T01:11:00.000Z"},
            "모니터링": {"type": "checkbox", "checkbox": False},
            "고객사": {"type": "multi_select", "multi_select": [{"name": "미래에셋"}]},
        }
        text = notion_properties_to_text(properties)
        self.assertEqual(text, "고객사: 미래에셋")

    def test_unnamed_people_are_counted_instead_of_printing_uuids(self):
        # 노션이 이름 없이 사용자 ID만 주는 경우가 있다. UUID를 그대로 찍으면
        # 화면과 모델 근거가 의미 없는 문자열로 덮인다.
        properties = {
            "공유(입력)": {
                "type": "people",
                "people": [
                    {"object": "user", "id": "6e15cf9a-68c2-48cf-b45b-e969a76e4e62"},
                    {"object": "user", "id": "042779f1-cd9f-4421-b0a6-f3554369a7c9", "name": "우동헌"},
                    {"object": "user", "id": "3e265b1b-ab60-4c25-a81b-61a8a3fe6dec"},
                ],
            },
        }
        text = notion_properties_to_text(properties)
        self.assertEqual(text, "공유(입력): 우동헌, 미확인 사용자 2명")
        self.assertNotIn("-", text.split(": ", 1)[1])

    def test_relation_reports_count_rather_than_page_ids(self):
        properties = {
            "일괄처리 업무분장": {
                "type": "relation",
                "relation": [{"id": "3d91082e-bb4c-8390-ad1d-0158ff0fbc6c"}],
            },
            "코스콤 버전관리": {"type": "relation", "relation": []},
        }
        self.assertEqual(notion_properties_to_text(properties), "일괄처리 업무분장: 연결 항목 1건")


class NotionOwnerConstraintTests(unittest.TestCase):
    def test_unassigned_owner_is_not_a_person_named_mijijeong(self):
        # "담당자 미지정"을 이름으로 잡아 0건이 되던 회귀.
        constraints = extract_notion_constraints("담당자 미지정 항목만 다시 보여줘")
        self.assertEqual(constraints["owner"], "")
        self.assertTrue(constraints["unassigned_owner"])

    def test_unassigned_filter_keeps_only_unassigned_pages(self):
        constraints = extract_notion_constraints("담당자 미지정 항목만 보여줘")
        self.assertFalse(
            notion_page_matches_constraints({"owner": "권한별", "status": ""}, constraints)
        )
        self.assertTrue(
            notion_page_matches_constraints({"owner": "담당자 미지정", "status": ""}, constraints)
        )

    def test_owner_particle_is_not_captured_as_name(self):
        # "담당자가"가 담당+자가로 쪼개져 "자가"가 담당자로 잡히던 회귀.
        self.assertEqual(
            extract_notion_constraints("담당자가 권한별인 항목")["owner"], "권한별"
        )
        self.assertEqual(extract_notion_constraints("담당자는 석지원")["owner"], "석지원")


class ContextDateInheritanceTests(unittest.TestCase):
    def context(self, user_turns):
        messages = []
        for text in user_turns[:-1]:
            messages.append(ChatMessage(role="user", content=text))
            messages.append(ChatMessage(role="assistant", content="결과입니다"))
        messages.append(ChatMessage(role="user", content=user_turns[-1]))
        text, _ = understanding.build_contextual_retrieval_text(messages)
        return extract_query_constraints(text)

    def test_month_narrowing_inherits_the_year(self):
        # "그중 4월 것만"이 연도를 잃어 모델이 임의의 연도를 채우던 회귀.
        constraints = self.context(["유진증권 2026년 이슈 보여줘", "그중 4월 것만 남겨줘"])
        self.assertEqual(
            constraints["created_at"]["from"], "2026-04-01"
        )
        self.assertEqual(constraints["created_at"]["to"], "2026-04-30")

    def test_narrowed_month_persists_into_later_turns(self):
        # 좁힌 4월이 이후 턴에서도 유지돼야 "총 몇 건이야?"가 4월 기준으로 센다.
        constraints = self.context(
            ["유진증권 2026년 이슈 보여줘", "그중 4월 것만 남겨줘", "총 몇 건이야?"]
        )
        self.assertEqual(constraints["created_at"]["from"], "2026-04-01")
        self.assertEqual(constraints["created_at"]["to"], "2026-04-30")

    def test_explicit_date_in_latest_turn_wins(self):
        constraints = self.context(["유진증권 2026년 이슈 보여줘", "2025년 이슈만 보여줘"])
        self.assertEqual(constraints["created_at"]["from"], "2025-01-01")


class DemonstrativeReferenceTests(unittest.TestCase):
    def test_demonstrative_issue_questions_are_follow_ups(self):
        # "이 이슈의 처리 이력"이 독립 질문으로 취급돼 대상을 잃던 회귀.
        self.assertTrue(is_follow_up_question("이 이슈의 처리 이력을 시간순으로 정리해줘"))
        self.assertTrue(is_follow_up_question("그 이슈의 담당자와 상태만 다시 알려줘"))
        self.assertFalse(is_follow_up_question("KB증권 HTS 최근 이슈 5개 보여줘"))

    def test_summary_request_without_targets_is_a_follow_up(self):
        # "핵심 해결 내용을 한 문장으로 요약해줘"는 길이 제한을 넘고 지시 표현도
        # 없지만, 그 자체로는 검색할 대상이 없는 후속 질문이다.
        self.assertTrue(is_follow_up_question("핵심 해결 내용을 한 문장으로 요약해줘"))

    def test_summary_request_with_its_own_target_stays_independent(self):
        # 고객사·플랫폼·기간처럼 대상을 특정하는 조건이 있으면 독립 질문이다.
        self.assertFalse(is_follow_up_question("KB증권 이슈를 한 줄씩만 요약해줘"))
        self.assertFalse(is_follow_up_question("신한 MTS 이슈 정리해줘"))
        self.assertFalse(is_follow_up_question("2026년 이슈 요약해줘"))

    @patch("backend.understanding.load_audit_sources")
    def test_demonstrative_reference_resolves_to_focused_issue(self, load_sources):
        # 상세 조회 뒤 후속 질문이 같은 이슈를 계속 가리켜야 한다.
        load_sources.return_value = [{"type": "mantis", "issue_id": 30892}]
        resolved = understanding.resolve_reference_from_context(
            "그 이슈의 담당자와 상태만 다시 알려줘", "audit-1"
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(extract_query_constraints(resolved)["issue_ids"], [30892])

    @patch("backend.understanding.load_audit_sources")
    def test_demonstrative_reference_does_not_guess_among_many(self, load_sources):
        # 여러 건을 보여준 직후라면 어느 것인지 알 수 없으므로 추측하지 않는다.
        load_sources.return_value = [
            {"type": "mantis", "issue_id": 30953},
            {"type": "mantis", "issue_id": 30892},
        ]
        self.assertIsNone(
            understanding.resolve_reference_from_context("그 이슈의 담당자 알려줘", "audit-1")
        )

    @patch("backend.understanding.load_audit_sources")
    def test_ordinal_reference_still_resolves(self, load_sources):
        load_sources.return_value = [
            {"type": "mantis", "issue_id": 30953},
            {"type": "mantis", "issue_id": 30892},
        ]
        resolved = understanding.resolve_reference_from_context(
            "그중 두 번째 이슈를 자세히 설명해줘", "audit-1"
        )
        self.assertEqual(extract_query_constraints(resolved)["issue_ids"], [30892])


class UnassignedHandlerTests(unittest.TestCase):
    def test_unassigned_request_is_detected(self):
        # "담당자가 없는 건만"이 조건으로 잡히지 않아 담당자가 있는 이슈까지
        # 그대로 나오던 회귀를 막는다.
        self.assertTrue(
            extract_query_constraints("신한 HTS 이슈 중 담당자가 없는 건만 보여줘")["unassigned_people"]
        )
        self.assertTrue(extract_query_constraints("담당자 미지정 이슈 보여줘")["unassigned_people"])

    def test_normal_questions_are_unaffected(self):
        self.assertFalse(extract_query_constraints("신한 HTS 이슈 보여줘")["unassigned_people"])
        self.assertFalse(extract_query_constraints("담당자가 석지원인 이슈")["unassigned_people"])

    def test_snapshot_matcher_keeps_only_unassigned_issues(self):
        constraints = extract_query_constraints("담당자가 없는 건만 보여줘")
        self.assertFalse(
            understanding.chroma_text_matches_constraints("이슈 ID: 1\n담당자: 정희진\n", constraints)
        )
        self.assertTrue(
            understanding.chroma_text_matches_constraints(
                "이슈 ID: 2\n담당자: 담당자 미지정\n", constraints
            )
        )

    def test_live_matcher_keeps_only_unassigned_issues(self):
        constraints = extract_query_constraints("담당자가 없는 건만 보여줘")
        assigned = {"id": 1, "handler": {"real_name": "정희진"}, "status": {"name": "closed"}}
        unassigned = {"id": 2, "handler": {}, "status": {"name": "closed"}}
        self.assertFalse(mantis.issue_matches_constraints(assigned, constraints))
        self.assertTrue(mantis.issue_matches_constraints(unassigned, constraints))


class MissingIssueAnswerTests(unittest.TestCase):
    def test_missing_issue_id_is_reported_as_not_found(self):
        # 존재하지 않는 번호인데 "정리했어요"로 답해 찾은 것처럼 보이던 회귀.
        line = build_issue_intro_line({"issue_ids": [0]}, [], 0)
        self.assertIn("찾지 못했어요", line)
        self.assertIn("#0", line)

    def test_existing_issue_id_keeps_summary_intro(self):
        line = build_issue_intro_line({"issue_ids": [30918]}, [{"id": 30918}], 1)
        self.assertIn("정리했어요", line)


class CountGroupingTests(unittest.TestCase):
    def test_group_counts_cover_every_match_not_just_examples(self):
        # 그룹 집계는 표시용 예시(example_limit)가 아니라 매칭 전체를 세야 한다.
        documents = [
            f"이슈 ID: {30000 + index}\n프로젝트: {'A증권' if index % 2 else 'B증권'}\n"
            for index in range(6)
        ]

        def normalize(content):
            match = re.search(r"이슈 ID: (\d+)", content)
            project = re.search(r"프로젝트: (\S+)", content)
            if not match:
                return None
            return {"id": int(match.group(1)), "project": project.group(1) if project else ""}

        with patch("backend.tools.mantis_snapshot.sqlite3.connect") as connect:
            connection = connect.return_value.__enter__.return_value
            connection.execute.return_value = [(content,) for content in documents]
            connect.return_value.execute.return_value = [(content,) for content in documents]
            result = scan_mantis_documents(
                Path(__file__),
                lambda content: True,
                normalize,
                example_limit=2,
                group_key=count_group_key("customer"),
            )

        self.assertEqual(result["matched_count"], 6)
        self.assertEqual(len(result["issues"]), 2)
        self.assertEqual(result["group_counts"], {"A증권": 3, "B증권": 3})

    def test_group_lines_are_sorted_by_count_desc(self):
        lines = format_count_group_lines({"A증권": 2, "B증권": 5, "C증권": 5}, "customer")
        self.assertIn("고객사별 건수", lines[1])
        self.assertEqual(lines[2:], ["- B증권 — 5건", "- C증권 — 5건", "- A증권 — 2건"])

    def test_month_group_is_sorted_chronologically(self):
        # 추이는 많은 순이 아니라 시간순이어야 읽히고, 전월 대비 증감과 요약이 붙는다.
        lines = format_count_group_lines({"2026-03": 9, "2026-01": 4, "2026-02": 7}, "month")
        self.assertIn("시간순", lines[1])
        # 첫 월은 기준점이라 증감이 없고, 이후 월은 전월 대비 증감이 붙는다.
        self.assertEqual(lines[2], "- 2026-01 — 4건")
        self.assertEqual(lines[3], "- 2026-02 — 7건 (▲3, +75.0%)")
        self.assertEqual(lines[4], "- 2026-03 — 9건 (▲2, +28.6%)")
        # 마지막에 추세 요약 한 줄(최다월/월평균/평균 대비)이 붙는다.
        self.assertTrue(any(line.startswith("요약:") for line in lines))

    def test_month_group_key_uses_created_month(self):
        key = count_group_key("month")
        self.assertEqual(key({"created_at": "2026-04-16"}), "2026-04")

    def _run_spike(self, question, group_counts):
        """급증 리포트를 합성 스냅샷으로 실행한다."""
        original_scan = answers.scan_mantis_documents
        original_constraints = answers.extract_query_constraints
        answers.scan_mantis_documents = lambda *a, **k: {
            "available": True,
            "scanned_count": sum(group_counts.values()),
            "group_counts": group_counts,
            "issues": [],
            "matched_count": 0,
        }
        answers.extract_query_constraints = lambda text: {}
        try:
            return answers.build_mantis_spike_report(question)
        finally:
            answers.scan_mantis_documents = original_scan
            answers.extract_query_constraints = original_constraints

    def test_spike_report_flags_recent_surge_over_baseline(self):
        sep = "\x1f"
        counts = {
            f"유진증권{sep}2026-05": 5, f"유진증권{sep}2026-06": 7, f"유진증권{sep}2026-07": 18,
            f"신한투자증권{sep}2026-07": 9,  # 직전 0건 신규 급증
            f"KB증권{sep}2026-05": 10, f"KB증권{sep}2026-06": 11, f"KB증권{sep}2026-07": 12,
        }
        report = self._run_spike("평소보다 이슈 급증한 고객사 알려줘", counts)
        content = report["content"]
        self.assertIn("유진증권", content)
        self.assertIn("3.0배", content)
        self.assertIn("신규 급증", content)  # 신한투자증권
        # 평탄한 KB증권(1.1배)은 급증으로 잡히지 않는다.
        self.assertNotIn("KB증권", content)
        # 신규 급증(비율 무한대)이 배수 급증보다 위로 정렬된다.
        self.assertLess(content.index("신한투자증권"), content.index("유진증권"))

    def test_spike_report_needs_two_months_of_history(self):
        sep = "\x1f"
        counts = {f"유진증권{sep}2026-07": 20}
        report = self._run_spike("급증한 고객사 알려줘", counts)
        self.assertIn("2개 이상의 월", report["content"])

    def test_spike_report_ignores_non_spike_questions(self):
        self.assertIsNone(answers.build_mantis_spike_report("KB증권 HTS 이슈 보여줘"))
        self.assertIsNone(answers.build_mantis_spike_report("이슈 많은 고객사 알려줘"))

    def _run_pivot(self, question, group_counts):
        original_scan = answers.scan_mantis_documents
        original_constraints = answers.extract_query_constraints
        answers.scan_mantis_documents = lambda *a, **k: {
            "available": True,
            "scanned_count": sum(group_counts.values()),
            "group_counts": group_counts,
            "issues": [],
            "matched_count": 0,
        }
        answers.extract_query_constraints = lambda text: {}
        try:
            return answers.build_mantis_pivot_report(question)
        finally:
            answers.scan_mantis_documents = original_scan
            answers.extract_query_constraints = original_constraints

    def test_pivot_report_cross_tabulates_axis_by_month(self):
        sep = "\x1f"
        counts = {
            f"유진증권{sep}2026-05": 5, f"유진증권{sep}2026-06": 7, f"유진증권{sep}2026-07": 18,
            f"KB증권{sep}2026-05": 10, f"KB증권{sep}2026-06": 11, f"KB증권{sep}2026-07": 12,
        }
        report = self._run_pivot("고객사별 월별 이슈 건수 표로 보여줘", counts)
        content = report["content"]
        self.assertIn("| 고객사 | 2026-05 | 2026-06 | 2026-07 | 합계 |", content)
        self.assertIn("| 유진증권 | 5 | 7 | 18 | 30 |", content)
        # 행은 합계가 큰 순으로 정렬된다(KB 33 > 유진 30).
        self.assertLess(content.index("KB증권"), content.index("유진증권"))
        # 열 합계 행이 붙는다.
        self.assertIn("| 합계 | 15 | 18 | 30 | 63 |", content)

    def test_pivot_report_requires_two_axes(self):
        # 축 하나만 있는 요청은 피벗이 아니라 단일 축 집계로 흘려보낸다.
        self.assertIsNone(answers._detect_pivot_axis("월별 이슈 추이"))
        self.assertIsNone(answers._detect_pivot_axis("고객사별 이슈 건수"))
        self.assertEqual(answers._detect_pivot_axis("담당자별 월별 처리 건수")[0], "owner")

    def test_monthly_trend_question_is_grouped_by_month(self):
        self.assertEqual(extract_group_by("2026년 월별 이슈 건수 추이를 알려줘"), "month")
        # "이번 달 이슈 보여줘"는 그룹 요청이 아니다.
        self.assertIsNone(extract_group_by("이번 달 이슈 보여줘"))

    def test_unsupported_group_has_no_key(self):
        self.assertIsNone(count_group_key("type"))
        self.assertIsNone(count_group_key(""))


if __name__ == "__main__":
    unittest.main()
