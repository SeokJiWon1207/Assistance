import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

from backend import answers, retrieval, understanding
from backend.reports import evaluation
from backend.sources import mantis
from scripts.accuracy.dynamic_accuracy_check import (
    AccuracyCase,
    FollowUpScenario,
    build_follow_up_scenarios,
    complete_mutable_mantis_ids,
    exact_screen_numbers,
    grade,
    run_follow_up_scenario,
    summarize_results,
)


class DataFreshnessNoteTests(unittest.TestCase):
    @patch("backend.answers.get_mantis_snapshot_freshness", return_value="2026-05-07")
    def test_note_tells_model_the_snapshot_base_date(self, _freshness):
        # 기준일을 알려주지 않으면 "데이터 기준일"을 물었을 때 모델이 오늘
        # 날짜를 그대로 답한다.
        note = answers.build_current_date_note()
        self.assertIn("2026-05-07", note)
        self.assertIn("데이터 기준일", note)

    @patch("backend.answers.get_mantis_snapshot_freshness", return_value="")
    def test_note_omits_base_date_when_unknown(self, _freshness):
        self.assertNotIn("데이터 기준일", answers.build_current_date_note())


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
    @patch.object(mantis, "get_mantis_headers", return_value={})
    @patch.object(mantis, "get_mantis_settings", return_value={"url": "https://mantis.example/api/issues"})
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
            issue(issue_id, "다른 고객사") for issue_id in range(99, 2, -1)
        ]
        get.side_effect = [
            Mock(status_code=200, json=lambda: {"issues": first_page}),
            Mock(status_code=200, json=lambda: {"issues": [issue(400)]}),
        ]

        result = mantis.execute_mantis_page_scan(
            "115다올투자증권 구KTB",
            limit=3,
            constraints={"customer_exact": "115다올투자증권(구KTB)"},
        )

        self.assertEqual([row["id"] for row in result["issues_list"]], [400, 300, 200])
        self.assertEqual(get.call_count, 2)

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

    @patch("scripts.accuracy.dynamic_accuracy_check.chat")
    def test_follow_up_sends_web_equivalent_history_and_audit_context(self, chat):
        chat.return_value = {
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
        request = chat.call_args.args[0]
        self.assertEqual(request.contextAuditId, "a" * 32)
        self.assertEqual([message.role for message in request.messages], ["user", "assistant", "user"])
        self.assertEqual(request.messages[-1].content, scenario.question)
        self.assertEqual(request.tools, {"mantis": True, "notion": False, "vectorDb": False})

    @patch("scripts.accuracy.dynamic_accuracy_check.chat")
    def test_follow_up_is_blocked_when_setup_case_fails(self, chat):
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
        chat.assert_not_called()

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
