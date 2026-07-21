import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import backend.app as backend_app
from backend.answers import (
    build_issue_intro_line,
    count_group_key,
    format_count_group_lines,
)
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.app import (
    build_effort_report,
    build_effort_report_from_plan,
    evaluate_ab_comparison,
    deduplicate_notion_pages_by_id,
    extract_effort_query,
    extract_notion_constraints,
    extract_query_constraints,
    is_follow_up_question,
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


class DemonstrativeReferenceTests(unittest.TestCase):
    def test_demonstrative_issue_questions_are_follow_ups(self):
        # "이 이슈의 처리 이력"이 독립 질문으로 취급돼 대상을 잃던 회귀.
        self.assertTrue(is_follow_up_question("이 이슈의 처리 이력을 시간순으로 정리해줘"))
        self.assertTrue(is_follow_up_question("그 이슈의 담당자와 상태만 다시 알려줘"))
        self.assertFalse(is_follow_up_question("KB증권 HTS 최근 이슈 5개 보여줘"))

    @patch("backend.understanding.load_audit_sources")
    def test_demonstrative_reference_resolves_to_focused_issue(self, load_sources):
        # 상세 조회 뒤 후속 질문이 같은 이슈를 계속 가리켜야 한다.
        load_sources.return_value = [{"type": "mantis", "issue_id": 30892}]
        resolved = backend_app.resolve_reference_from_context(
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
            backend_app.resolve_reference_from_context("그 이슈의 담당자 알려줘", "audit-1")
        )

    @patch("backend.understanding.load_audit_sources")
    def test_ordinal_reference_still_resolves(self, load_sources):
        load_sources.return_value = [
            {"type": "mantis", "issue_id": 30953},
            {"type": "mantis", "issue_id": 30892},
        ]
        resolved = backend_app.resolve_reference_from_context(
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
            backend_app.chroma_text_matches_constraints("이슈 ID: 1\n담당자: 정희진\n", constraints)
        )
        self.assertTrue(
            backend_app.chroma_text_matches_constraints(
                "이슈 ID: 2\n담당자: 담당자 미지정\n", constraints
            )
        )

    def test_live_matcher_keeps_only_unassigned_issues(self):
        constraints = extract_query_constraints("담당자가 없는 건만 보여줘")
        assigned = {"id": 1, "handler": {"real_name": "정희진"}, "status": {"name": "closed"}}
        unassigned = {"id": 2, "handler": {}, "status": {"name": "closed"}}
        self.assertFalse(backend_app.issue_matches_constraints(assigned, constraints))
        self.assertTrue(backend_app.issue_matches_constraints(unassigned, constraints))


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

    def test_unsupported_group_has_no_key(self):
        self.assertIsNone(count_group_key("type"))
        self.assertIsNone(count_group_key(""))


if __name__ == "__main__":
    unittest.main()
