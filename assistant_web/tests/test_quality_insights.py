import random
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from backend import answers, retrieval, understanding
from backend.reports import evaluation
from backend.schemas import ChatRequest
from backend.services.chat_service import chat_service
from backend.sources import mantis
from scripts.accuracy.dynamic_accuracy_check import (
    AccuracyCase,
    FollowUpScenario,
    build_mantis_cases,
    build_notion_cases,
    build_follow_up_scenarios,
    complete_mutable_mantis_ids,
    execute_chat,
    exact_screen_numbers,
    extract_answer_count,
    fetch_mantis_snapshot,
    fetch_notion_snapshot,
    filter_recall_matches,
    grade,
    notion_page_summary,
    notion_property_text,
    observed_issue_ids,
    observed_ordered_issue_ids,
    observed_route_source,
    run_follow_up_scenario,
    select_rotating_cases,
    summarize_boolean_dimension,
    summarize_latency,
    summarize_results,
    write_report,
)


class DataFreshnessNoteTests(unittest.TestCase):
    def test_current_date_note_does_not_present_snapshot_as_live_freshness(self):
        note = answers.build_current_date_note()
        self.assertNotIn("2026-05-07", note)
        self.assertNotIn("데이터 기준일", note)

    def test_snapshot_note_is_hidden_after_successful_live_lookup(self):
        result = {
            "stages": [
                {
                    "source": "mantis_snapshot",
                    "status": "ok",
                    "meta": {"snapshot_as_of": "2026-05-07"},
                },
                {"source": "mantis_page_scan", "status": "ok", "issues_list": []},
            ]
        }
        self.assertIsNone(retrieval.build_snapshot_note(result))

    def test_snapshot_only_result_keeps_freshness_warning(self):
        result = {
            "stages": [
                {
                    "source": "mantis_snapshot",
                    "status": "ok",
                    "meta": {"snapshot_as_of": "2026-05-07"},
                }
            ]
        }
        self.assertIn("2026-05-07", retrieval.build_snapshot_note(result))


class AccuracyDataParsingTests(unittest.TestCase):
    def test_notion_property_parser_and_page_summary_cover_supported_types(self):
        properties = {
            "제목": {"type": "title", "title": [{"plain_text": "배포 점검"}]},
            "상태": {"type": "status", "status": {"name": "진행 중"}},
            "태그": {
                "type": "multi_select",
                "multi_select": [{"name": "운영"}, {"name": "배포"}],
            },
            "일정": {"type": "date", "date": {"start": "2026-08-24"}},
            "담당": {"type": "people", "people": [{"name": "석지원"}]},
            "점수": {"type": "number", "number": 0},
            "완료": {"type": "checkbox", "checkbox": False},
            "수식": {"type": "formula", "formula": {"string": "제외"}},
        }

        self.assertEqual(notion_property_text(properties["태그"]), "운영, 배포")
        self.assertEqual(notion_property_text(properties["점수"]), "0")
        self.assertEqual(notion_property_text(properties["완료"]), "False")
        self.assertEqual(notion_property_text(properties["수식"]), "")
        summary = notion_page_summary(
            {
                "id": "page-1",
                "last_edited_time": "2026-08-24T10:00:00Z",
                "properties": properties,
            },
            "업무 DB",
        )

        self.assertEqual(summary["title"], "배포 점검")
        self.assertEqual(summary["status"], "진행 중")
        self.assertEqual(summary["database"], "업무 DB")
        self.assertIn("담당: 석지원", summary["search_text"])
        self.assertNotIn("수식", summary["search_text"])

    @patch.dict("os.environ", {"NOTION_API_TOKEN": "token"})
    @patch(
        "scripts.accuracy.dynamic_accuracy_check.parse_notion_database_ids",
        return_value=["database-12345678"],
    )
    @patch("scripts.accuracy.dynamic_accuracy_check.requests.post")
    @patch("scripts.accuracy.dynamic_accuracy_check.requests.get")
    def test_notion_snapshot_follows_pagination_cursor(
        self,
        get,
        post,
        _database_ids,
    ):
        metadata = Mock()
        metadata.json.return_value = {"title": [{"plain_text": "업무 DB"}]}
        get.return_value = metadata
        first = Mock()
        first.json.return_value = {
            "results": [
                {
                    "id": "page-1",
                    "properties": {
                        "제목": {"type": "title", "title": [{"plain_text": "첫 문서"}]}
                    },
                }
            ],
            "has_more": True,
            "next_cursor": "cursor-2",
        }
        second = Mock()
        second.json.return_value = {
            "results": [
                {
                    "id": "page-2",
                    "properties": {
                        "제목": {"type": "title", "title": [{"plain_text": "둘째 문서"}]}
                    },
                }
            ],
            "has_more": False,
        }
        post.side_effect = [first, second]

        pages = fetch_notion_snapshot()

        self.assertEqual([page["title"] for page in pages], ["첫 문서", "둘째 문서"])
        self.assertNotIn("start_cursor", post.call_args_list[0].kwargs["json"])
        self.assertEqual(post.call_args_list[1].kwargs["json"]["start_cursor"], "cursor-2")
        metadata.raise_for_status.assert_called_once_with()
        first.raise_for_status.assert_called_once_with()
        second.raise_for_status.assert_called_once_with()

    def test_observation_and_summary_helpers_preserve_order_and_dimensions(self):
        response = {
            "content": "총 2건입니다. #12346, 추가 이슈입니다.",
            "sources": [{"issue_id": 12345}],
            "retrieval": {"intent_plan": {"source": "mantis"}},
        }
        self.assertEqual(observed_issue_ids(response), {12345})
        self.assertEqual(observed_ordered_issue_ids(response), [12345])
        self.assertEqual(
            observed_ordered_issue_ids({"content": "#00012346, #12345", "sources": []}),
            [],
        )
        self.assertEqual(extract_answer_count(response["content"]), 2)
        self.assertEqual(observed_route_source(response), "mantis")
        self.assertEqual(
            observed_route_source(
                {
                    "retrieval": {
                        "stages": [{"source": "notion"}, {"source": "mantis_page_scan"}]
                    }
                }
            ),
            "both",
        )
        self.assertEqual(
            summarize_boolean_dimension(
                [{"routing_passed": True}, {"routing_passed": False}, {}],
                "routing_passed",
            ),
            {"total": 2, "passed": 1, "score": 50},
        )
        self.assertEqual(
            summarize_latency(
                [{"latency_ms": 100}, {"latency_ms": 300}, {"latency_ms": 200}]
            ),
            {"p50_ms": 200.0, "p95_ms": 300.0},
        )
        self.assertEqual(summarize_latency([]), {"p50_ms": None, "p95_ms": None})

    def test_filter_recall_requires_every_question_constraint(self):
        question = "KB증권 MTS 확인된 이슈 보여줘"
        expected = understanding.extract_query_constraints(question)
        self.assertTrue(filter_recall_matches(question, expected))
        self.assertFalse(filter_recall_matches(question, {**expected, "statuses": []}))

    def test_accuracy_report_writer_persists_json_and_readable_markdown(self):
        report = {
            "run_at": "2026-08-24T16:00:00+09:00",
            "snapshots": {"mantis": 10, "notion": 5},
            "summary": {
                "status": "passed",
                "score": 100,
                "attempted": 1,
                "total": 1,
                "passed": 1,
                "blocked_by_prerequisite": 0,
                "dependency_errors": 0,
                "base": {"score": 100, "passed": 1, "total": 1},
                "follow_up": {"score": None, "passed": 0, "total": 0},
                "routing": {"score": 100, "passed": 1, "total": 1},
                "filter_precision": {"score": 100, "passed": 1, "total": 1},
                "filter_recall": {"score": 100, "passed": 1, "total": 1},
                "latency": {"p50_ms": 120.0, "p95_ms": 120.0},
            },
            "results": [
                {
                    "outcome": "passed",
                    "source": "mantis",
                    "category": "정확 ID",
                    "question": "#31105 알려줘",
                    "expected": [31105],
                    "actual": [31105],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory, patch(
            "scripts.accuracy.dynamic_accuracy_check.REPORT_DIR",
            new=Path(temporary_directory),
        ):
            json_path, markdown_path = write_report(report)

            self.assertIn('"status": "passed"', json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIn("Accuracy: 100%", markdown)
        self.assertIn("| PASS | base | mantis | 정확 ID |", markdown)


class ConstraintGroundingTests(unittest.TestCase):
    def constraints(self, question):
        return understanding.extract_query_constraints(question)

    def test_conditions_from_the_question_are_grounded(self):
        question = "KB증권 MTS 이슈"
        self.assertEqual(
            evaluation.ungrounded_constraint_labels(question, self.constraints(question)), []
        )

    def test_invented_customer_is_flagged(self):
        # 모델이 지어낸 고객사 필터가 조용히 적용돼도 기존 검증은 통과했다.
        constraints = self.constraints("최근 이슈 보여줘")
        constraints["customer_key"] = "유진증권"
        constraints["customer_aliases"] = ["유진증권"]
        self.assertEqual(
            evaluation.ungrounded_constraint_labels("최근 이슈 보여줘", constraints),
            ["고객사=유진증권"],
        )

    def test_invented_owner_is_flagged(self):
        constraints = self.constraints("최근 이슈 보여줘")
        constraints["people"] = {"names": ["석지원"], "role": "handler"}
        self.assertEqual(
            evaluation.ungrounded_constraint_labels("최근 이슈 보여줘", constraints),
            ["담당자=석지원"],
        )

    def test_inherited_follow_up_conditions_are_not_flagged(self):
        # 근거 텍스트는 검색에 실제 쓰인 문장이라 물려받은 조건도 통과해야 한다.
        question = "신한 mts 종료된 건은 빼줘"
        self.assertEqual(
            evaluation.ungrounded_constraint_labels(question, self.constraints(question)), []
        )


class ScreenNumberAccuracyTests(unittest.TestCase):
    def setUp(self):
        mantis.clear_mantis_recent_cache()
        self.addCleanup(mantis.clear_mantis_recent_cache)

    @patch.object(mantis, "fetch_mantis_issues")
    def test_live_count_collection_returns_error_metadata(self, fetch_issues):
        fetch_issues.side_effect = requests.ReadTimeout("slow")

        result = mantis.collect_mantis_live_ids_in_range({}, max_pages=8)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_kind"], "dependency")
        self.assertEqual(result["issues"], {})
        self.assertEqual(result["scanned_pages"], 0)
        self.assertIn("ReadTimeout", result["reason"])

    @patch.object(mantis, "fetch_mantis_issues")
    def test_live_count_collection_reports_completed_pages(self, fetch_issues):
        fetch_issues.side_effect = [[{"id": 2}, {"id": 1}], []]

        result = mantis.collect_mantis_live_ids_in_range({}, max_pages=8, page_size=2)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(set(result["issues"]), {"1", "2"})
        self.assertEqual(result["scanned_pages"], 2)

    @patch.object(mantis, "get_mantis_headers", return_value={})
    @patch.object(mantis, "get_mantis_settings", return_value={"url": "https://mantis.example/api/issues", "token": "test"})
    @patch.object(mantis.requests, "get")
    def test_page_scan_checks_all_recent_pages_before_selecting_latest_results(
        self, get, _settings, _headers
    ):
        def issue(issue_id, project="115다올투자증권(구KTB)"):
            return {
                "id": issue_id,
                "project": {"name": project},
                "summary": f"이슈 {issue_id}",
                "status": {"name": "confirmed"},
                "handler": {"real_name": "담당자"},
                "created_at": "2026-05-01",
            }

        first_page = [issue(300), issue(200), issue(100)] + [
            issue(issue_id, "다른 고객사") for issue_id in range(99, 52, -1)
        ]
        def get_page(*args, **kwargs):
            rows = {1: first_page, 2: [issue(400)]}.get(kwargs["params"]["page"], [])
            return Mock(status_code=200, json=lambda: {"issues": rows})
        get.side_effect = get_page

        result = mantis.execute_mantis_page_scan(
            "115다올투자증권 구KTB",
            limit=3,
            constraints={"customer_exact": "115다올투자증권(구KTB)"},
        )

        self.assertEqual([row["id"] for row in result["issues_list"]], [400, 300, 200])
        pages = [call.kwargs["params"]["page"] for call in get.call_args_list]
        self.assertEqual(pages.count(1), 1)
        self.assertEqual(pages.count(2), 1)
        self.assertEqual(result["meta"]["searched_pages"], 2)

    @patch.object(mantis.time, "sleep")
    @patch.object(mantis, "get_mantis_headers", return_value={})
    @patch.object(mantis, "get_mantis_settings", return_value={"url": "https://mantis.example/api/issues", "token": "test"})
    @patch.object(mantis.requests, "get")
    def test_page_scan_retries_transient_timeout(self, get, _settings, _headers, sleep):
        response = Mock(status_code=200)
        response.json.return_value = {"issues": [{"id": 300, "summary": "테스트"}]}
        first_page_attempts = iter([requests.ReadTimeout("slow"), response])
        def get_page(*args, **kwargs):
            if kwargs["params"]["page"] != 1:
                return Mock(status_code=200, json=lambda: {"issues": []})
            result = next(first_page_attempts)
            if isinstance(result, Exception):
                raise result
            return result
        get.side_effect = get_page

        result = mantis.execute_mantis_page_scan("", limit=1, constraints={})

        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["id"] for row in result["issues_list"]], [300])
        self.assertEqual(sum(call.kwargs["params"]["page"] == 1 for call in get.call_args_list), 2)
        sleep.assert_called_once_with(1)

    @patch.object(mantis.time, "sleep")
    @patch.object(mantis, "get_mantis_headers", return_value={})
    @patch.object(mantis, "get_mantis_settings", return_value={"url": "https://mantis.example/api/issues"})
    @patch.object(mantis.requests, "get")
    def test_exact_issue_lookup_retries_transient_timeout(self, get, _settings, _headers, sleep):
        response = Mock(status_code=200)
        response.json.return_value = {"issues": [{"id": 31351}]}
        get.side_effect = [requests.ReadTimeout("slow"), response]

        self.assertEqual(mantis.fetch_mantis_issue_by_id(31351), {"id": 31351})
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_dynamic_oracle_matches_field_or_exact_title_marker(self):
        field_issue = {
            "summary": "해외주식 차트 수정",
            "custom_fields": [{"field": {"name": "화면번호"}, "value": "9413"}],
        }
        title_issue = {"summary": "[7600] 차트 지표 수정", "custom_fields": []}
        free_text_issue = {"summary": "194130 로그에서 발생", "custom_fields": []}
        self.assertEqual(exact_screen_numbers(field_issue), {"9413"})
        self.assertEqual(exact_screen_numbers(title_issue), {"7600"})
        self.assertEqual(exact_screen_numbers(free_text_issue), set())

    def test_audit_screen_match_requires_numeric_boundary(self):
        self.assertTrue(understanding.text_matches_screen_numbers("화면번호 9413", ["9413"]))
        self.assertFalse(understanding.text_matches_screen_numbers("식별자 194130", ["9413"]))

    @patch("backend.retrieval.execute_mantis_filter_search")
    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_snapshot_search")
    def test_screen_query_uses_exact_snapshot_and_page_scan_only(
        self, snapshot_search, page_scan, filter_search
    ):
        snapshot_search.return_value = {
            "source": "mantis_snapshot",
            "status": "ok",
            "meta": {"snapshot_as_of": "2026-07-01"},
            "issues_list": [],
        }
        page_scan.return_value = {
            "source": "mantis_page_scan",
            "status": "ok",
            "issues_list": [],
        }
        result = retrieval.run_retrieval_pipeline(
            "화면번호 9413 관련 이슈 3개 보여줘",
            3,
            {"mantis": True, "notion": False, "vectorDb": False},
        )
        self.assertEqual(
            [stage["source"] for stage in result["stages"]],
            ["mantis_snapshot", "mantis_page_scan"],
        )
        filter_search.assert_not_called()

    @patch("backend.sources.mantis.fetch_mantis_issue_by_id")
    def test_mutable_snapshot_fields_are_revalidated_against_live_mantis(self, fetch_issue):
        def live_issue(issue_id):
            handler = "다른 담당자" if issue_id == 30072 else "이준옥"
            return {
                "id": issue_id,
                "project": {"name": "테스트"},
                "summary": f"이슈 {issue_id}",
                "status": {"name": "closed"},
                "handler": {"real_name": handler},
                "created_at": "2025-01-01",
            }

        fetch_issue.side_effect = live_issue
        stage = {
            "source": "mantis_snapshot",
            "status": "ok",
            "meta": {"displayed_count": 2},
            "issues_list": [
                {"id": 30072, "handler": "이준옥"},
                {"id": 29730, "handler": "이준옥"},
            ],
        }
        constraints = understanding.extract_query_constraints("담당자 이준옥의 최근 이슈 2개 보여줘")

        result = mantis.revalidate_mantis_snapshot_stage(stage, constraints)

        self.assertEqual([issue["id"] for issue in result["issues_list"]], [29730])
        self.assertEqual(result["meta"]["stale_removed_count"], 1)
        self.assertEqual(result["meta"]["live_revalidated_count"], 2)


class AccuracyInsightTests(unittest.TestCase):
    def test_mantis_cases_add_compound_ordering_exclusion_and_no_result_checks(self):
        issues = []
        for index in range(8):
            issues.append({
                "id": 31008 - index,
                "project": {"name": "001Test(테스트증권)"},
                "summary": f"테스트 이슈 {index}",
                "status": {"name": "closed" if index < 4 else "assigned"},
                "handler": {"real_name": "테스트담당자"},
                "created_at": "2026-08-01",
                "custom_fields": [
                    {"field": {"name": "매체구분"}, "value": "HTS" if index % 2 == 0 else "MTS"},
                    {"field": {"name": "화면번호"}, "value": "2301"},
                ],
            })

        cases = build_mantis_cases(issues, random.Random(42))
        by_id = {case.id: case for case in cases}

        self.assertEqual(by_id["mantis-project-platform"].expected_kind, "ordered_issue_ids")
        self.assertEqual(
            by_id["mantis-project-platform-exclusion"].expected_kind,
            "ordered_issue_ids",
        )
        self.assertEqual(by_id["mantis-screen"].expected_kind, "ordered_issue_ids")
        self.assertEqual(by_id["mantis-screen"].expected, [31008, 31007, 31006])
        self.assertTrue(by_id["mantis-screen"].mutable_oracle)
        self.assertEqual(by_id["mantis-screen-no-result"].expected_kind, "no_results")

    def test_daily_case_selection_keeps_follow_up_prerequisites_and_rotates_other_slots(self):
        cases = [
            AccuracyCase("mantis-exact-id", "mantis", "정확 ID", "q1", "issue_ids", [1]),
            AccuracyCase("mantis-project-latest", "mantis", "고객사", "q2", "issue_ids", [2, 1]),
            AccuracyCase("mantis-project-status", "mantis", "상태", "q3", "issue_ids", [2]),
            *[
                AccuracyCase(f"mantis-extra-{index}", "mantis", "고급", f"mq{index}", "issue_ids", [index])
                for index in range(6)
            ],
            *[
                AccuracyCase(f"notion-title-{index}", "notion", "제목", f"nq{index}", "titles", [str(index)])
                for index in range(1, 7)
            ],
        ]

        first = select_rotating_cases(cases, 10, random.Random("2026-08-21"))
        second = select_rotating_cases(cases, 10, random.Random("2026-08-22"))

        required = {"mantis-exact-id", "mantis-project-latest", "mantis-project-status", "notion-title-1"}
        self.assertTrue(required.issubset({case.id for case in first}))
        self.assertEqual(sum(case.source == "mantis" for case in first), 6)
        self.assertEqual(sum(case.source == "notion" for case in first), 4)
        self.assertNotEqual({case.id for case in first}, {case.id for case in second})

    def test_notion_cases_include_no_result_hallucination_check(self):
        cases = build_notion_cases(
            [{"title": "실제 업무", "status": "진행", "database": "업무", "id": "1"}],
            random.Random(42),
        )

        no_result = next(case for case in cases if case.id == "notion-title-no-result")
        self.assertEqual(no_result.expected_kind, "no_results")
        self.assertIn("존재하지 않는 항목", no_result.question)

    def test_follow_up_scenarios_reuse_four_base_cases(self):
        cases = [
            AccuracyCase("mantis-exact-id", "mantis", "정확 ID", "0030916 이슈", "issue_ids", [30916]),
            AccuracyCase("mantis-project-latest", "mantis", "고객사 최신", "삼성 최근 3개", "issue_ids", [30920, 30919, 30918]),
            AccuracyCase("mantis-project-status", "mantis", "고객사 상태", "삼성 closed 2개", "issue_ids", [30919, 30918]),
            AccuracyCase("notion-title-1", "notion", "제목", "노션 제목 검색", "titles", ["테스트 페이지"]),
        ]
        issues = [{"id": 30916, "project": {"name": "001Samsung(삼성증권)"}}]

        scenarios = build_follow_up_scenarios(cases, issues, 4)

        self.assertEqual(len(scenarios), 4)
        self.assertEqual(
            [scenario.parent_case_id for scenario in scenarios],
            [
                "mantis-exact-id",
                "mantis-project-latest",
                "mantis-project-status",
                "notion-title-1",
            ],
        )
        self.assertEqual(scenarios[1].expected, [30919])
        self.assertEqual(scenarios[3].expected_kind, "text_any")

    def test_notion_title_follow_up_accepts_exact_title_without_repeated_sources(self):
        case = AccuracyCase(
            "followup-notion-title",
            "notion",
            "후속 질문·Notion 문맥",
            "그 항목의 제목을 정확히 다시 알려줘",
            "text_any",
            ["서버 업무파트 회의"],
        )

        result = grade(
            case,
            {
                "content": "위 목록의 첫 번째 항목 제목은 **서버 업무파트 회의**입니다.",
                "sources": [],
            },
        )

        self.assertTrue(result["passed"])
        self.assertEqual(result["actual"], {"matched": "서버 업무파트 회의"})

    @patch("scripts.accuracy.dynamic_accuracy_check.execute_chat")
    def test_follow_up_sends_web_equivalent_history_and_audit_context(self, execute):
        execute.return_value = {
            "content": "#30919 상세 내용",
            "sources": [{"issue_id": 30919}],
            "auditId": "b" * 32,
        }
        parent_case = AccuracyCase(
            "mantis-project-latest", "mantis", "고객사 최신", "삼성 최근 3개", "issue_ids", [30920, 30919]
        )
        parent_result = {
            "passed": True,
            "outcome": "passed",
            "answer": "#30920, #30919",
            "audit_id": "a" * 32,
        }
        scenario = FollowUpScenario(
            "followup-project-second",
            "mantis",
            "후속 질문·순번 참조",
            parent_case.id,
            "그중 두 번째 이슈를 자세히 알려줘",
            "exact_issue_ids",
            [30919],
        )

        result = run_follow_up_scenario(scenario, parent_case, parent_result, "test-model")

        self.assertTrue(result["passed"])
        request = execute.call_args.args[0]
        self.assertEqual(request.contextAuditId, "a" * 32)
        self.assertEqual([message.role for message in request.messages], ["user", "assistant", "user"])
        self.assertEqual(request.messages[-1].content, scenario.question)
        self.assertEqual(request.tools, {"mantis": True, "notion": True, "vectorDb": False})

    @patch("scripts.accuracy.dynamic_accuracy_check.execute_chat")
    def test_follow_up_is_blocked_when_setup_case_fails(self, execute):
        parent_case = AccuracyCase("mantis-exact-id", "mantis", "정확 ID", "질문", "issue_ids", [30916])
        scenario = FollowUpScenario(
            "followup-exact-project", "mantis", "후속", parent_case.id, "이 이슈 고객사?", "text_any", ["삼성증권"]
        )

        result = run_follow_up_scenario(
            scenario,
            parent_case,
            {"passed": False, "outcome": "assertion_failed", "audit_id": "a" * 32},
            "test-model",
        )

        self.assertEqual(result["outcome"], "blocked_by_prerequisite")
        execute.assert_not_called()

    @patch.object(chat_service, "build_response")
    def test_accuracy_chat_uses_service_layer_without_http_request(self, build_response):
        build_response.return_value = {"content": "ok", "sources": []}
        request = ChatRequest(
            modelId="test-model",
            messages=[{"role": "user", "content": "질문"}],
        )

        self.assertEqual(execute_chat(request), build_response.return_value)
        build_response.assert_called_once_with(request, user=None)

    @patch.dict(
        "os.environ",
        {"MANTIS_API_URL": "https://mantis.example/issues", "MANTIS_API_TOKEN": "token"},
    )
    @patch("scripts.accuracy.dynamic_accuracy_check.time.sleep")
    @patch("scripts.accuracy.dynamic_accuracy_check.requests.get")
    def test_mantis_snapshot_retries_transient_timeout(self, get, sleep):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"issues": [{"id": 10}]}
        get.side_effect = [requests.ReadTimeout("slow"), response]

        self.assertEqual(fetch_mantis_snapshot(), [{"id": 10}])
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(get.call_args.kwargs["params"]["page_size"], 50)

    def test_summary_counts_base_failures_blocked_followups_and_errors(self):
        summary = summarize_results([
            {"passed": True, "outcome": "passed"},
            {"passed": False, "outcome": "assertion_failed"},
            {"passed": False, "outcome": "blocked_by_prerequisite"},
            {"passed": False, "outcome": "dependency_error"},
        ])

        self.assertEqual(summary["attempted"], 4)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 2)
        self.assertEqual(summary["blocked_by_prerequisite"], 1)
        self.assertEqual(summary["score"], 33)

    @patch("scripts.accuracy.dynamic_accuracy_check.mantis.revalidate_mantis_snapshot_stage")
    @patch("scripts.accuracy.dynamic_accuracy_check.mantis.execute_mantis_snapshot_search")
    def test_mutable_oracle_combines_live_sample_with_verified_full_snapshot(
        self, snapshot_search, revalidate
    ):
        snapshot_search.return_value = {
            "source": "mantis_snapshot",
            "status": "ok",
            "issues_list": [{"id": 30072}, {"id": 29730}],
            "meta": {},
        }
        revalidate.return_value = {
            "source": "mantis_snapshot",
            "status": "ok",
            "issues_list": [{"id": 30072}, {"id": 29730}],
            "meta": {"live_fallback_count": 0},
        }
        live_sample = [
            {
                "id": 29730,
                "project": {"name": "테스트"},
                "handler": {"real_name": "이준옥"},
                "status": {"name": "closed"},
            },
            {
                "id": 29115,
                "project": {"name": "테스트"},
                "handler": {"real_name": "이준옥"},
                "status": {"name": "closed"},
            },
        ]

        expected = complete_mutable_mantis_ids(
            "담당자 이준옥의 최근 이슈 2개 보여줘",
            live_sample,
            2,
        )

        self.assertEqual(expected, [30072, 29730])

    def test_dependency_timeout_is_not_counted_as_accuracy_failure(self):
        case = AccuracyCase(
            "notion-title-1",
            "notion",
            "제목 검색",
            "노션에서 '테스트' 항목을 찾아서 알려줘",
            "titles",
            ["테스트"],
        )
        result = grade(
            case,
            {
                "content": "Notion connection failed: Read timed out. (read timeout=12)",
                "sources": [],
                "retrievalStages": [
                    {
                        "source": "notion",
                        "status": "error",
                        "reason": "Notion connection failed: Read timed out.",
                    }
                ],
            },
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "dependency_error")

        analytics = evaluation.build_accuracy_analytics(
            [
                {
                    "run_at": "2026-07-21T08:00:00+09:00",
                    "summary": {"score": None, "passed": 0, "total": 0, "status": "incomplete"},
                    "results": [result],
                }
            ]
        )
        self.assertIsNone(analytics["trend"][0]["score"])
        self.assertEqual(analytics["failureCounts"], [])

    def test_no_result_case_rejects_hallucinated_sources(self):
        case = AccuracyCase(
            "mantis-screen-no-result",
            "mantis",
            "결과 없음",
            "화면번호 99999 관련 이슈가 있는지 확인해줘",
            "no_results",
            [],
        )

        empty = grade(case, {"content": "검색 결과가 없습니다.", "sources": []})
        hallucinated = grade(
            case,
            {"content": "#12345가 있습니다.", "sources": [{"issue_id": 12345}]},
        )

        self.assertTrue(empty["passed"])
        self.assertFalse(hallucinated["passed"])

    def test_ordered_issue_case_rejects_wrong_latest_order(self):
        case = AccuracyCase(
            "mantis-project-platform",
            "mantis",
            "고객사·플랫폼·정렬",
            "테스트증권 HTS 최근 이슈 2개",
            "ordered_issue_ids",
            [200, 100],
        )

        correct = grade(
            case,
            {"content": "#200, #100", "sources": [{"issue_id": 200}, {"issue_id": 100}]},
        )
        reversed_result = grade(
            case,
            {"content": "#100, #200", "sources": [{"issue_id": 100}, {"issue_id": 200}]},
        )

        self.assertTrue(correct["passed"])
        self.assertFalse(reversed_result["passed"])

    def test_analytics_reports_delta_trend_and_failure_frequency(self):
        reports = [
            {
                "run_at": "2026-07-10T08:00:00+09:00",
                "summary": {"score": 90, "passed": 9, "total": 10},
                "results": [
                    {"category": "화면번호", "passed": False},
                    {"category": "정확 ID", "passed": True},
                ],
            },
            {
                "run_at": "2026-07-09T08:00:00+09:00",
                "summary": {"score": 70, "passed": 7, "total": 10},
                "results": [
                    {"category": "화면번호", "passed": False},
                    {"category": "담당자", "passed": False},
                ],
            },
        ]
        analytics = evaluation.build_accuracy_analytics(reports)
        self.assertEqual(analytics["latestDelta"], 20)
        self.assertEqual([point["score"] for point in analytics["trend"]], [70, 90])
        self.assertEqual(
            analytics["failureCounts"],
            [{"category": "화면번호", "count": 2}, {"category": "담당자", "count": 1}],
        )

    def test_freshness_marks_old_snapshot_stale(self):
        old_date = (date.today() - timedelta(days=7)).isoformat()
        with patch("backend.sources.mantis.get_mantis_snapshot_freshness", return_value=old_date):
            status = mantis.get_mantis_snapshot_freshness_status()
        self.assertEqual(status["ageDays"], 7)
        self.assertEqual(status["status"], "stale")


if __name__ == "__main__":
    unittest.main()
