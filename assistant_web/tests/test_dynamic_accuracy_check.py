import random
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from scripts.accuracy import dynamic_accuracy_check as accuracy

from scripts.accuracy.dynamic_accuracy_check import (
    AccuracyCase,
    dependency_reason_from_stage,
    grade,
    reconcile_mutable_mantis_oracle,
    run_case,
    run_follow_up_scenario,
    FollowUpScenario,
    prepare_mantis_store,
    build_follow_up_scenarios,
    build_mantis_cases,
    complete_mutable_mantis_ids,
)
from backend.intent.constraints import extract_query_constraints


class IndependentAccuracyOracleTests(unittest.TestCase):
    def test_platform_oracle_uses_only_the_original_media_field(self):
        row = {"id": 29242, "summary": "MTS 공용 작업", "description": "HTS와 MTS 작업",
               "project": {"name": "공용"}, "custom_fields": []}
        self.assertFalse(accuracy.mantis_oracle_matches(row, {"platform": "MTS", "scope": "all"}))
        self.assertTrue(accuracy.mantis_oracle_matches(row, {"excluded_platform": "MTS", "scope": "all"}))
        self.assertTrue(accuracy.mantis_oracle_matches(row, {"scope": "all"}))

    def test_independent_oracle_defaults_to_exact_team_handlers(self):
        for handler, expected in (("석지원", True), ("타팀담당", False),
                                  ("석지원_보조", False), ("", False)):
            row = self.issue(1, "고객A", "HTS")
            row["handler"]["real_name"] = handler
            with self.subTest(handler=handler):
                self.assertEqual(accuracy.mantis_oracle_matches(row, {"project": "고객A"}), expected)
                self.assertTrue(accuracy.mantis_oracle_matches(row, {"project": "고객A", "scope": "all"}))
        external = self.issue(2, "고객A", "HTS")
        external["handler"]["real_name"] = "타팀담당"
        self.assertTrue(accuracy.mantis_oracle_matches(external, {"handler": "타팀담당"}))

    def test_mutable_oracle_keeps_default_team_scope_after_live_revalidation(self):
        rows = [self.issue(i, "고객A", "HTS") for i in (4, 3, 2, 1)]
        for row, name in zip(rows, ("타팀담당", "석지원", "석지원", "이영희"), strict=True):
            row["handler"]["real_name"] = name
        latest = {row["id"]: row for row in rows}
        latest[3] = {**rows[1], "handler": {"real_name": "타팀담당"}}
        with (patch.object(accuracy, "read_complete_store", return_value=rows),
              patch.object(accuracy.mantis, "fetch_mantis_issue_by_id", side_effect=latest.get)):
            self.assertEqual(complete_mutable_mantis_ids({"project": "고객A"}, [], 2), [2, 1])
            self.assertEqual(complete_mutable_mantis_ids({"project": "고객A", "scope": "all"}, [], 2), [4, 3])

    def test_generated_structural_cases_explicitly_request_their_company_oracle_scope(self):
        rows = [self.issue(i, "고객A", "HTS" if i % 2 else "MTS") for i in range(8, 0, -1)]
        cases = {case.id: case for case in build_mantis_cases(rows, random.Random(42))}
        for case_id in ("mantis-project-latest", "mantis-project-status", "mantis-project-platform",
                        "mantis-project-platform-exclusion", "mantis-screen", "mantis-month-count",
                        "mantis-screen-no-result"):
            with self.subTest(case_id=case_id):
                case = cases[case_id]
                self.assertEqual(extract_query_constraints(case.question).get("mantis_team_scope"), "all")
                if case.mutable_oracle:
                    self.assertEqual(case.oracle_filters.get("scope"), "all")

    def test_oracle_collection_failure_is_reported_as_incomplete(self):
        with (
            patch.dict(accuracy.os.environ),
            patch.object(accuracy.sys, "argv", ["accuracy", "--all-cases"]),
            patch.object(accuracy, "prepare_mantis_store", return_value={"search_complete": True}),
            patch.object(accuracy, "fetch_mantis_snapshot", return_value=[{"id": 1}]),
            patch.object(accuracy, "fetch_notion_snapshot", return_value=[]),
            patch.object(accuracy, "build_mantis_cases", side_effect=RuntimeError("oracle unavailable")),
            patch.object(accuracy, "build_notion_cases", return_value=[]),
            patch.object(accuracy, "get_model_registry", return_value=[]),
            patch.object(accuracy, "write_report", return_value=(Path("r.json"), Path("r.md"))) as write,
            redirect_stdout(StringIO()),
        ):
            try:
                exit_code = accuracy.main()
            except RuntimeError:
                exit_code = None
        self.assertEqual(exit_code, 1, "원본 수집 실패도 최종 incomplete 리포트가 있어야 한다")
        report = write.call_args.args[0]
        self.assertEqual(report["summary"]["status"], "incomplete")
        self.assertTrue(any("oracle" in error for error in report["errors"]))

    def test_customer_followup_accepts_display_alias_but_rejects_another_customer(self):
        case = AccuracyCase("mantis-exact-id", "mantis", "정확 ID", "#31597", "exact_issue_ids", [31597])
        scenario = build_follow_up_scenarios(
            [case], [{"id": 31597, "project": {"name": "117Hantu(한투증권)"}}], 1,
        )[0]
        followup = AccuracyCase(scenario.id, scenario.source, scenario.category, scenario.question,
                                scenario.expected_kind, scenario.expected)
        self.assertTrue(grade(followup, {"content": "프로젝트: 한국투자증권"})["passed"])
        self.assertFalse(grade(followup, {"content": "프로젝트: 신한투자증권"})["passed"])

    @staticmethod
    def issue(issue_id, project, platform):
        return {"id": issue_id, "project": {"name": project}, "summary": "로그인 오류",
                "status": {"name": "confirmed"}, "handler": {"real_name": "김담당"},
                "created_at": "2025-06-01", "custom_fields": [
                    {"field": {"name": "매체구분"}, "value": platform},
                    {"field": {"name": "화면번호"}, "value": "1234"},
                ]}

    def test_case_expected_ids_do_not_follow_broken_production_filter(self):
        issues = [self.issue(6, "고객A", "HTS"), self.issue(5, "고객B", "HTS"),
                  self.issue(4, "고객A", "MTS"), self.issue(3, "고객B", "MTS"),
                  self.issue(2, "고객A", "HTS"), self.issue(1, "고객B", "MTS")]
        with patch("scripts.accuracy.dynamic_accuracy_check.mantis.issue_matches_constraints", return_value=True):
            cases = {case.id: case for case in build_mantis_cases(issues, random.Random(42))}
        platform = cases["mantis-project-platform"]
        wanted = {"고객A HTS": [6, 2], "고객B MTS": [3, 1]}
        self.assertEqual(platform.expected, next(ids for prefix, ids in wanted.items()
                                               if platform.question.startswith(prefix)))
        exclusion = cases["mantis-project-platform-exclusion"]
        wanted_excluded = {("고객A", "MTS"): [6, 2], ("고객B", "HTS"): [3, 1]}
        self.assertEqual(exclusion.expected, next(ids for (project, excluded), ids in wanted_excluded.items()
                                                if exclusion.question.startswith(project)
                                                and f"{excluded}는 빼고" in exclusion.question))

    def test_mutable_oracle_filters_raw_rows_without_search_helpers(self):
        live = [self.issue(200, "고객B", "HTS"), self.issue(100, "고객A", "HTS")]
        older = self.issue(90, "고객A", "HTS")
        latest = {row["id"]: row for row in [*live, older]}
        with (
            patch("scripts.accuracy.dynamic_accuracy_check.extract_query_constraints", return_value={}),
            patch("scripts.accuracy.dynamic_accuracy_check.mantis.issue_matches_constraints", return_value=True),
            patch("scripts.accuracy.dynamic_accuracy_check.mantis.execute_mantis_snapshot_search",
                  return_value={"status": "error"}),
            patch("scripts.accuracy.dynamic_accuracy_check.mantis.revalidate_mantis_snapshot_stage",
                  return_value={"status": "error"}),
            patch("scripts.accuracy.dynamic_accuracy_check.read_complete_store", create=True, return_value=[older]),
            patch("scripts.accuracy.dynamic_accuracy_check.mantis.fetch_mantis_issue_by_id",
                  side_effect=latest.get),
        ):
            self.assertEqual(complete_mutable_mantis_ids({"project": "고객A", "platform": "HTS", "scope": "all"}, live, 2),
                             [100, 90])


class DynamicAccuracyDependencyTests(unittest.TestCase):
    def test_clarification_is_never_a_successful_empty_search(self):
        case = AccuracyCase("none", "mantis", "결과 없음", "화면번호 99999 이슈", "no_results", [])
        for response in (
            {"content": "상태와 관계없는 전체 이슈를 뜻하시나요?", "raw": {"mode": "status_clarification"}},
            {"content": "이슈가 없습니다.", "raw": {"mode": "search_terms_clarification"}},
            {"content": "이슈가 없습니다.", "retrieval": {"constraints": {
                "status_resolution": {"needs_clarification": True, "origin": "unavailable",
                                      "reason": "planner_unavailable"}}}},
            {"content": "접근 권한이 없습니다."},
            {"content": "전체가 0건이거나 모두 완료됐다는 뜻은 아닙니다.",
             "raw": {"mode": "empty_evidence"}},
        ):
            with self.subTest(response=response):
                self.assertFalse(grade(case, response)["passed"])

    def test_empty_search_accepts_the_actual_screen_lookup_answer(self):
        from backend.answers import build_issue_intro_line
        case = AccuracyCase("none", "mantis", "결과 없음", "화면번호 99999 이슈", "no_results", [])
        content = build_issue_intro_line({"screen_numbers": ["99999"]}, [], 0)
        self.assertTrue(grade(case, {"content": content, "sources": []})["passed"])

    def test_unavailable_status_planner_is_a_dependency_failure(self):
        case = AccuracyCase("none", "mantis", "결과 없음", "화면번호 99999 이슈", "no_results", [])
        response = {"content": "상태와 관계없는 전체 이슈를 뜻하시나요?",
                    "raw": {"mode": "status_clarification"}, "retrieval": {"constraints": {
                        "status_resolution": {"needs_clarification": True, "origin": "unavailable",
                                              "reason": "planner_unavailable"}}}}
        result = grade(case, response)
        self.assertEqual(result["outcome"], "dependency_error")
        self.assertFalse(result["answer_passed"])

    @patch("scripts.accuracy.dynamic_accuracy_check.sync_complete_store")
    @patch("scripts.accuracy.dynamic_accuracy_check.read_store_metadata")
    def test_stale_search_store_is_refreshed_before_accuracy_queries(self, metadata, sync):
        fresh = {"count": 31000, "search_complete": True}
        metadata.side_effect = [None, fresh]
        self.assertEqual(prepare_mantis_store(), fresh)
        sync.assert_called_once_with()

    @patch("scripts.accuracy.dynamic_accuracy_check.sync_complete_store")
    @patch("scripts.accuracy.dynamic_accuracy_check.read_store_metadata", return_value={"search_complete": True})
    def test_fresh_store_does_not_need_another_full_sync(self, metadata, sync):
        self.assertEqual(prepare_mantis_store(), metadata.return_value)
        sync.assert_not_called()

    @patch("scripts.accuracy.dynamic_accuracy_check.sync_complete_store", return_value={"status": "running"})
    @patch("scripts.accuracy.dynamic_accuracy_check.read_store_metadata", return_value=None)
    @patch("scripts.accuracy.dynamic_accuracy_check.refresh_in_progress", return_value=False)
    def test_unfinished_refresh_is_not_reported_as_ready(self, _running, _metadata, _sync):
        with self.assertRaisesRegex(RuntimeError, "완료되지"):
            prepare_mantis_store()

    @patch("scripts.accuracy.dynamic_accuracy_check.sync_complete_store", return_value={"status": "running"})
    @patch("scripts.accuracy.dynamic_accuracy_check.read_store_metadata")
    @patch("scripts.accuracy.dynamic_accuracy_check.refresh_in_progress", side_effect=[True, False])
    @patch("scripts.accuracy.dynamic_accuracy_check.time.sleep")
    def test_preparation_waits_for_an_existing_server_refresh(self, sleep, _running, metadata, sync):
        fresh = {"search_complete": True, "count": 31000}
        metadata.side_effect = [None, None, fresh]
        self.assertEqual(prepare_mantis_store(), fresh)
        sync.assert_called_once()
        sleep.assert_called_once_with(1)

    @patch("scripts.accuracy.dynamic_accuracy_check.sync_complete_store", side_effect=TimeoutError)
    @patch("scripts.accuracy.dynamic_accuracy_check.read_store_metadata", return_value=None)
    def test_refresh_failure_remains_an_execution_prerequisite_error(self, _metadata, _sync):
        with self.assertRaises(TimeoutError):
            prepare_mantis_store()

    @patch("scripts.accuracy.dynamic_accuracy_check.execute_chat", return_value={"content": "없습니다", "sources": []})
    def test_accuracy_requests_do_not_generate_ungraded_ab_comparisons(self, execute):
        case = AccuracyCase("empty", "notion", "빈 결과", "노션 '없는 문서'", "titles", [])
        run_case(case, "test-model")
        self.assertFalse(execute.call_args.args[0].abTest)
        scenario = FollowUpScenario("follow", "notion", "후속", "empty", "그 문서 찾아줘", "titles", [])
        run_follow_up_scenario(scenario, case, {"passed": True, "audit_id": "synthetic", "answer": "없습니다"}, "test-model")
        self.assertEqual(execute.call_count, 2)
        self.assertFalse(execute.call_args.args[0].abTest)

    def test_nested_retrieval_timeout_is_dependency_error(self):
        case = AccuracyCase(
            "mantis-handler",
            "mantis",
            "담당자",
            "담당자 강민성의 최근 이슈 2개 보여줘",
            "issue_ids",
            [31419, 31383],
        )
        response = {
            "content": "스냅샷 결과입니다.",
            "sources": [
                {"type": "mantis", "issue_id": 31383},
                {"type": "mantis", "issue_id": 31253},
            ],
            "retrieval": {
                "constraints": {},
                "stages": [
                    {
                        "source": "mantis_page_scan",
                        "status": "error",
                        "reason": "Mantis connection failed: ReadTimeout",
                    }
                ],
            },
        }

        result = grade(case, response)

        self.assertFalse(result["passed"])
        self.assertEqual(result["outcome"], "dependency_error")

    def test_dependency_error_wins_over_lucky_answer_match(self):
        case = AccuracyCase("id", "mantis", "정확 ID", "1번", "issue_ids", [1])
        response = {
            "content": "#1",
            "sources": [{"type": "mantis", "issue_id": 1}],
            "retrieval": {
                "constraints": {},
                "stages": [
                    {
                        "source": "mantis_page_scan",
                        "status": "partial_error",
                        "reason": "connection failed after partial scan",
                    }
                ],
            },
        }

        result = grade(case, response)

        self.assertFalse(result["passed"])
        self.assertTrue(result["answer_passed"])
        self.assertEqual(result["outcome"], "dependency_error")

    def test_no_result_stage_is_not_a_dependency_error(self):
        case = AccuracyCase("none", "mantis", "결과 없음", "조건에 맞는 이슈가 있나?", "no_results", [])
        response = {
            "content": "조건에 맞는 이슈가 없습니다.",
            "sources": [],
            "retrieval": {
                "stages": [
                    {
                        "source": "mantis_sla",
                        "status": "error",
                        "reason": "조건에 맞는 이슈가 없습니다.",
                    }
                ]
            },
        }

        result = grade(case, response)

        self.assertTrue(result["passed"])
        self.assertEqual(result["outcome"], "passed")

    def test_structured_dependency_kind_does_not_need_reason_keywords(self):
        reason = dependency_reason_from_stage(
            {
                "status": "partial_error",
                "error_kind": "dependency",
                "reason": "upstream temporarily unavailable",
            }
        )

        self.assertEqual(reason, "upstream temporarily unavailable")

    @patch("scripts.accuracy.dynamic_accuracy_check.complete_mutable_mantis_ids")
    @patch("scripts.accuracy.dynamic_accuracy_check.fetch_mantis_snapshot")
    def test_stale_mutable_oracle_is_reconciled_from_full_live_candidates(
        self,
        fetch_snapshot,
        complete_oracle,
    ):
        fetch_snapshot.return_value = [{"id": 31469}, {"id": 31468}]
        complete_oracle.return_value = [31469, 31468]
        case = AccuracyCase(
            "mantis-handler",
            "mantis",
            "담당자",
            "담당자 정희진의 최근 이슈 2개 보여줘",
            "issue_ids",
            [31465, 31446],
            mutable_oracle=True,
            oracle_filters={"handler": "정희진"},
        )
        response = {
            "sources": [
                {"type": "mantis", "issue_id": 31469},
                {"type": "mantis", "issue_id": 31468},
            ],
            "retrieval": {"constraints": {"people": {"names": ["정희진"], "role": "handler"}}},
        }
        original = {"passed": False, "outcome": "assertion_failed", "answer_passed": False,
                    "failed_checks": ["답변 내용"]}

        result = reconcile_mutable_mantis_oracle(case, response, original)

        self.assertTrue(result["passed"])
        self.assertTrue(result["oracle_reconciled"])
        self.assertEqual(result["failed_checks"], [])
        self.assertEqual(result["expected"], [31469, 31468])
        complete_oracle.assert_called_once_with({"handler": "정희진"}, fetch_snapshot.return_value, 2)
        # 기대값 변경은 원문/실행 오류까지 성공으로 바꿀 근거가 아니다.
        unsafe = {**original, "failed_checks": ["답변 내용", "원문 속성"]}
        self.assertEqual(reconcile_mutable_mantis_oracle(case, response, unsafe), unsafe)

    @patch("scripts.accuracy.dynamic_accuracy_check.complete_mutable_mantis_ids")
    @patch("scripts.accuracy.dynamic_accuracy_check.fetch_mantis_snapshot", return_value=[])
    def test_oracle_reconciliation_keeps_failure_when_original_oracle_is_current(
        self,
        _fetch_snapshot,
        complete_oracle,
    ):
        complete_oracle.return_value = [31419, 31383]
        case = AccuracyCase(
            "mantis-handler",
            "mantis",
            "담당자",
            "담당자 강민성의 최근 이슈 2개 보여줘",
            "issue_ids",
            [31419, 31383],
            mutable_oracle=True,
            oracle_filters={"handler": "강민성"},
        )
        response = {
            "sources": [
                {"type": "mantis", "issue_id": 31383},
                {"type": "mantis", "issue_id": 31253},
            ],
            "retrieval": {"constraints": {"people": {"names": ["강민성"], "role": "handler"}}},
        }
        original = {"passed": False, "outcome": "assertion_failed", "answer_passed": False}

        result = reconcile_mutable_mantis_oracle(case, response, original)

        self.assertFalse(result["passed"])
        self.assertNotIn("oracle_reconciled", result)

    @patch("scripts.accuracy.dynamic_accuracy_check.complete_mutable_mantis_ids")
    @patch("scripts.accuracy.dynamic_accuracy_check.fetch_mantis_snapshot", return_value=[])
    def test_oracle_reconciliation_accepts_fewer_current_matches(
        self,
        _fetch_snapshot,
        complete_oracle,
    ):
        complete_oracle.return_value = [31268]
        case = AccuracyCase(
            "mantis-status",
            "mantis",
            "상태",
            "NH증권에서 상태가 confirmed인 최근 이슈 2개 보여줘",
            "issue_ids",
            [30980, 30979],
            mutable_oracle=True,
            oracle_filters={"project": "120NH(NH증권)", "status": "confirmed"},
        )
        response = {
            "sources": [{"type": "mantis", "issue_id": 31268}],
            "retrieval": {"constraints": {"customer_key": "NH증권", "statuses": ["confirmed"]}},
        }
        original = {"passed": False, "outcome": "assertion_failed", "answer_passed": False}

        result = reconcile_mutable_mantis_oracle(case, response, original)

        self.assertTrue(result["passed"])
        self.assertEqual(result["expected"], [31268])
        complete_oracle.assert_called_once_with({"project": "120NH(NH증권)", "status": "confirmed"}, [], 2)

    @patch("scripts.accuracy.dynamic_accuracy_check.fetch_mantis_snapshot")
    def test_oracle_reconciliation_refuses_wrong_returned_constraints(self, fetch_snapshot):
        case = AccuracyCase(
            "mantis-handler",
            "mantis",
            "담당자",
            "담당자 홍길동의 최근 이슈 2개 보여줘",
            "issue_ids",
            [200, 190],
            mutable_oracle=True,
            oracle_filters={"handler": "홍길동"},
        )
        response = {
            "sources": [
                {"type": "mantis", "issue_id": 300},
                {"type": "mantis", "issue_id": 290},
            ],
            "retrieval": {
                "constraints": {"people": {"names": ["김철수"], "role": "handler"}}
            },
        }
        original = grade(case, response)

        result = reconcile_mutable_mantis_oracle(case, response, original)

        self.assertFalse(result["passed"])
        self.assertFalse(result["filter_recall_passed"])
        fetch_snapshot.assert_not_called()

    @patch("scripts.accuracy.dynamic_accuracy_check.complete_mutable_mantis_ids")
    @patch("scripts.accuracy.dynamic_accuracy_check.fetch_mantis_snapshot", return_value=[])
    def test_oracle_reconciliation_uses_candidates_beyond_expected_and_actual(
        self,
        _fetch_snapshot,
        complete_oracle,
    ):
        complete_oracle.return_value = [400, 300]
        case = AccuracyCase(
            "mantis-handler",
            "mantis",
            "담당자",
            "담당자 홍길동의 최근 이슈 2개 보여줘",
            "issue_ids",
            [200, 190],
            mutable_oracle=True,
            oracle_filters={"handler": "홍길동"},
        )
        response = {
            "sources": [
                {"type": "mantis", "issue_id": 300},
                {"type": "mantis", "issue_id": 290},
            ],
            "retrieval": {
                "constraints": {"people": {"names": ["홍길동"], "role": "handler"}}
            },
        }
        original = grade(case, response)

        result = reconcile_mutable_mantis_oracle(case, response, original)

        self.assertFalse(result["passed"])
        self.assertNotIn("oracle_reconciled", result)


class FollowUpConstraintGradingTests(unittest.TestCase):
    def run_scenario(self, question, constraints, *, correction=False):
        parent = AccuracyCase(
            "mantis-project-status", "mantis", "고객사·상태",
            "하나금융에서 상태가 closed인 최근 이슈 2개 보여줘", "ordered_issue_ids", [31571, 31554],
        )
        scenario = FollowUpScenario(
            "followup-project-status-correction" if correction else "followup-project-status-latest",
            "mantis", "후속 질문·조건 상속", parent.id, question, "exact_issue_ids", [31571],
        )
        response = {
            "content": "#31571", "sources": [{"type": "mantis", "issue_id": 31571}],
            "retrieval": {"query": (constraints.get("status_resolution") or {}).get("grounding_text", question),
                          "constraints": constraints, "query_plan": {"source": "mantis"}},
        }
        with patch("scripts.accuracy.dynamic_accuracy_check.execute_chat", return_value=response):
            return run_follow_up_scenario(
                scenario, parent, {"passed": True, "audit_id": "a" * 32, "answer": "이전 답변"}, "fixture",
            )

    def test_inherited_customer_is_required_even_when_answer_id_is_correct(self):
        result = self.run_scenario(
            "방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘",
            extract_query_constraints("상태 closed 최근 이슈"),
        )
        self.assertTrue(result["answer_passed"])
        self.assertFalse(result["filter_recall_passed"])
        self.assertFalse(result["passed"])
        self.assertIn("조건 누락", result["failed_checks"])

    def test_missing_constraints_cannot_skip_inherited_filter_grading(self):
        result = self.run_scenario("방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘", {})
        self.assertFalse(result["filter_recall_passed"])
        self.assertFalse(result["passed"])

    def test_status_correction_replaces_status_but_still_requires_customer(self):
        question = "아니, 고객사는 그대로 두고 상태만 confirmed로 바꿔서 가장 최근 이슈 하나를 알려줘"
        constraints = extract_query_constraints("하나금융 상태 confirmed 최근 이슈")
        result = self.run_scenario(question, constraints, correction=True)
        self.assertTrue(result["filter_recall_passed"])
        self.assertTrue(result["passed"])
        missing_customer = self.run_scenario(question, extract_query_constraints("상태 confirmed 이슈"), correction=True)
        self.assertFalse(missing_customer["filter_recall_passed"])


if __name__ == "__main__":
    unittest.main()
