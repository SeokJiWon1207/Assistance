import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.evaluation import cases


class EvaluationCaseTests(unittest.TestCase):
    def test_case_loader_uses_defaults_for_missing_or_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            with patch.object(cases, "CONFIG_DIR", config_dir):
                missing = cases.load_evaluation_cases()
                (config_dir / "evaluation_cases.json").write_text("{}", encoding="utf-8")
                invalid_shape = cases.load_evaluation_cases()
                (config_dir / "evaluation_cases.json").write_text("not-json", encoding="utf-8")
                invalid_json = cases.load_evaluation_cases()

        self.assertIs(missing, cases.DEFAULT_EVALUATION_CASES)
        self.assertIs(invalid_shape, cases.DEFAULT_EVALUATION_CASES)
        self.assertIs(invalid_json, cases.DEFAULT_EVALUATION_CASES)

    def test_case_loader_accepts_valid_configuration(self):
        configured = [{"id": "custom", "question": "질문", "expected": {"minResults": 0}}]
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            (config_dir / "evaluation_cases.json").write_text(
                json.dumps(configured, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch.object(cases, "CONFIG_DIR", config_dir):
                loaded = cases.load_evaluation_cases()

        self.assertEqual(loaded, configured)

    def test_run_case_checks_compound_constraints_and_returned_sources(self):
        case = {
            "id": "compound",
            "category": "복합",
            "question": "KB증권 담당자 홍길동의 HTS 화면 5031 이슈",
            "expected": {
                "customer": "KB증권",
                "platform": "HTS",
                "platformExclude": "MTS",
                "screenNumber": "5031",
                "person": "홍길동",
                "issueId": 28508,
                "sourceIds": [28508],
                "source": "notion",
                "titleContains": "장애 보고",
                "databaseLabel": "주요이슈현황",
                "minResults": 2,
            },
        }
        constraints = {
            "customer_key": "KB증권",
            "customer_aliases": ["KB증권"],
            "platform_key": "HTS",
            "excluded_platform_keys": ["MTS"],
            "screen_numbers": ["5031"],
            "people": {"names": ["홍길동"]},
        }
        retrieval_result = {"stages": [{"source": "mantis", "status": "ok", "meta": {}}]}
        sources = [
            {
                "type": "mantis",
                "issue_id": 28508,
                "project": "KB증권",
                "handler": "홍길동",
                "title": "[5031] 주문 장애",
                "detail": {"screen_number": "5031", "media_type": "HTS"},
            },
            {
                "type": "notion",
                "title": "장애 보고 처리 절차",
                "database_label": "주요이슈현황",
            },
        ]
        with (
            patch.object(cases, "extract_query_constraints", return_value=constraints),
            patch.object(cases, "run_retrieval_pipeline", return_value=retrieval_result),
            patch.object(cases, "build_sources", return_value=sources),
            patch.object(cases, "retrieval_trace", return_value=[{"source": "mantis"}]),
        ):
            result = cases.run_evaluation_case(case)

        self.assertTrue(result["passed"])
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual(result["sourceCount"], 2)
        self.assertEqual(result["trace"], [{"source": "mantis"}])

    def test_effort_and_count_cases_use_deterministic_reports(self):
        effort_case = {
            "id": "effort",
            "question": "이번 달 공수",
            "expected": {"effortTotal": 3.5, "issueCount": 2, "minResults": 1},
        }
        effort_report = {
            "retrieval": {"stages": [{"meta": {"total_effort": 3.5}}]},
            "sources": [{"issue_id": 1}, {"issue_id": 2}],
        }
        count_case = {
            "id": "count",
            "question": "이번 달 이슈 수",
            "expected": {"totalCount": 7, "minResults": 0},
        }
        count_report = {
            "retrieval": {"stages": [{"meta": {"total_count": 7}}]},
            "sources": [],
        }
        with (
            patch.object(cases, "extract_query_constraints", return_value={}),
            patch.object(cases, "build_effort_report", return_value=effort_report),
            patch.object(cases, "build_mantis_month_count_report", return_value=count_report),
            patch.object(cases, "retrieval_trace", return_value=[]),
            patch.object(cases, "run_retrieval_pipeline") as pipeline,
        ):
            effort = cases.run_evaluation_case(effort_case)
            count = cases.run_evaluation_case(count_case)

        self.assertTrue(effort["passed"])
        self.assertTrue(count["passed"])
        pipeline.assert_not_called()

    def test_safe_runner_converts_case_exception_to_a_failed_result(self):
        case = {"id": "broken", "question": "실패", "expected": {}}
        with patch.object(cases, "run_evaluation_case", side_effect=RuntimeError("boom")):
            result = cases.run_evaluation_case_safely(case)

        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"][0]["label"], "실행 오류")
        self.assertIn("RuntimeError: boom", result["checks"][0]["actual"])

    def test_summary_and_saved_run_preserve_scores_and_results(self):
        results = [{"id": "a", "passed": True}, {"id": "b", "passed": False}]
        summary = cases.build_evaluation_summary(results, duration_ms=321)
        self.assertEqual(summary["score"], 50)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "audit.sqlite3"

            def initialize() -> None:
                with closing(sqlite3.connect(database_path)) as connection, connection:
                    connection.execute(
                        """
                        CREATE TABLE evaluation_runs (
                            id TEXT, run_at TEXT, total INTEGER, passed INTEGER,
                            failed INTEGER, score INTEGER, duration_ms INTEGER, results TEXT
                        )
                        """
                    )

            with (
                patch.object(cases, "AUDIT_DB_PATH", database_path),
                patch.object(cases, "init_audit_db", side_effect=initialize),
            ):
                run_id = cases.save_evaluation_run(summary, results)
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT total, passed, failed, score, results FROM evaluation_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()

        self.assertEqual(row[:4], (2, 1, 1, 50))
        self.assertEqual(json.loads(row[4]), results)


if __name__ == "__main__":
    unittest.main()
