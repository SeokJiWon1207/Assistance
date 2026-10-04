import unittest
from unittest.mock import patch

from scripts.accuracy.dynamic_accuracy_check import (
    AccuracyCase,
    dependency_reason_from_stage,
    grade,
    reconcile_mutable_mantis_oracle,
    run_case,
    run_follow_up_scenario,
    FollowUpScenario,
    prepare_mantis_store,
)


class DynamicAccuracyDependencyTests(unittest.TestCase):
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
        complete_oracle.assert_called_once_with(case.question, fetch_snapshot.return_value, 2)
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
        )
        response = {
            "sources": [{"type": "mantis", "issue_id": 31268}],
            "retrieval": {"constraints": {"customer_key": "NH증권", "statuses": ["confirmed"]}},
        }
        original = {"passed": False, "outcome": "assertion_failed", "answer_passed": False}

        result = reconcile_mutable_mantis_oracle(case, response, original)

        self.assertTrue(result["passed"])
        self.assertEqual(result["expected"], [31268])
        complete_oracle.assert_called_once_with(case.question, [], 2)

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


if __name__ == "__main__":
    unittest.main()
