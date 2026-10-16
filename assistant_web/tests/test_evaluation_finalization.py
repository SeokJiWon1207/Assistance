import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from backend.audit import CHAT_STREAM_CONTEXT
from backend.evaluation import finalization
from backend.schemas import ChatRequest


def create_validation_log_table(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_logs (
                id TEXT PRIMARY KEY,
                created_at TEXT,
                question TEXT,
                model_id TEXT,
                mode TEXT,
                score INTEGER,
                status TEXT,
                payload TEXT,
                user_uid TEXT,
                failed_checks_json TEXT
            )
            """
        )


def chat_request(**overrides) -> ChatRequest:
    values = {
        "modelId": "primary-model",
        "messages": [{"role": "user", "content": "질문"}],
        "temperature": 0.2,
        "answerStyle": "normal",
        "responseFormat": "text",
        "abTest": False,
    }
    values.update(overrides)
    return ChatRequest(**values)


class EvaluationFinalizationTests(unittest.TestCase):
    def tearDown(self):
        for attribute in ("emitter", "content_streamed"):
            if hasattr(CHAT_STREAM_CONTEXT, attribute):
                delattr(CHAT_STREAM_CONTEXT, attribute)

    def test_unsupported_answer_is_blocked_before_any_content_event(self):
        original = "확인한 이슈는 #99999 입니다."
        response = {
            "modelId": "primary-model", "content": original,
            "sources": [{"type": "mantis", "issue_id": 12345, "title": "실제 원문"}],
            "retrieval": {"query": "이슈 보여줘", "constraints": {},
                          "query_plan": {"source": "mantis", "operation": "search"}, "stages": []},
            "raw": {"mode": "model"},
        }
        emitted = []
        CHAT_STREAM_CONTEXT.emitter = emitted.append
        followups, baseline = Mock(), Mock()

        def verify(*args):
            self.assertFalse(any(event["type"] == "delta" for event in emitted))
            return finalization.build_verification_report(*args)

        dependencies = {"build_verification_report": verify, "generate_follow_up_questions": followups,
                        "generate_basic_grounded_baseline": baseline}
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "audit.sqlite3"
            with patch.object(finalization, "AUDIT_DB_PATH", database_path), \
                    patch.object(finalization, "init_audit_db", side_effect=lambda: create_validation_log_table(database_path)), \
                    patch.object(finalization, "_compat_dependency", side_effect=lambda name, default: dependencies.get(name, default)):
                result = finalization.finalize_chat_response(chat_request(abTest=True), "이슈 보여줘", response)
            with closing(sqlite3.connect(database_path)) as connection:
                payload = json.loads(connection.execute("SELECT payload FROM validation_logs").fetchone()[0])
        self.assertEqual(result["raw"]["mode"], "verification_blocked")
        self.assertIn("issue_ids", result["raw"]["blockedChecks"])
        self.assertEqual(result["verification"]["delivery"], "blocked")
        self.assertEqual(result["sources"], [])
        self.assertIsNone(result["retrieval"])
        self.assertEqual(result["followUps"], [])
        self.assertIsNone(payload["queryPlan"])
        self.assertEqual(payload["blockedAnswer"], original)
        self.assertNotIn("99999", json.dumps(result, ensure_ascii=False).split('"verification"')[0])
        delivered = "".join(event["content"] for event in emitted if event["type"] == "delta")
        self.assertEqual(delivered, result["content"])
        self.assertNotIn("99999", delivered)
        followups.assert_not_called()
        baseline.assert_not_called()

    def test_delivery_blocks_definite_calculation_and_scope_errors_only(self):
        from backend.evaluation.delivery import decide_delivery
        for check_id in ("count_result", "count_filters", "effort_recalculation", "work_scope", "constraints"):
            with self.subTest(check=check_id):
                decision = decide_delivery({"checks": [{"id": check_id, "status": "fail"}]})
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.blocked_checks, (check_id,))
        decision = decide_delivery({"checks": [
            {"id": "constraints", "status": "warning"},
            {"id": "model_content_review", "status": "review"},
            {"id": "operation_intent", "status": "fail"},
        ]})
        self.assertTrue(decision.allowed)

    def test_write_confirmations_keep_user_supplied_issue_references(self):
        from backend.evaluation.delivery import decide_delivery
        for mode in ("notion_write_pending", "notion_create", "notion_update"):
            with self.subTest(mode=mode):
                report = finalization.build_verification_report(
                    "노션 항목 작성", "제목: #99999 대응 메모", [], None, mode,
                )
                self.assertTrue(decide_delivery(report).allowed)

    def test_finalization_uses_hidden_evidence_and_persists_audit_payload(self):
        evidence = [{"type": "mantis", "issue_id": 31408, "title": "근거 이슈"}]
        retrieval = {
            "query_plan": {"source": "mantis"},
            "filter_audit": {"applied": ["customer"]},
            "stages": [
                {
                    "source": "mantis",
                    "status": "ok",
                    "reason": "matched",
                    "meta": {"displayed_count": 1},
                }
            ],
        }
        response = {
            "content": "검증된 답변 " * 20,
            "sources": [],
            "sourcesForFollowUps": evidence,
            "retrieval": retrieval,
            "raw": {"mode": "model"},
            "modelId": "fallback-model",
        }
        followups = Mock(return_value=["후속 질문"])
        verification = Mock(
            return_value={
                "verifiedAt": "2026-08-24T12:00:00+09:00",
                "score": 80,
                "status": "warning",
                "checks": [
                    {"id": "grounding", "label": "근거", "status": "fail"},
                    {"id": "source", "label": "출처", "status": "pass"},
                ],
            }
        )
        dependencies = {
            "generate_follow_up_questions": followups,
            "build_verification_report": verification,
            "should_attach_ab_test": Mock(return_value=False),
        }
        emitted = []
        CHAT_STREAM_CONTEXT.emitter = emitted.append
        CHAT_STREAM_CONTEXT.content_streamed = False

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "audit.sqlite3"
            with (
                patch.object(finalization, "AUDIT_DB_PATH", database_path),
                patch.object(
                    finalization,
                    "init_audit_db",
                    side_effect=lambda: create_validation_log_table(database_path),
                ),
                patch.object(
                    finalization,
                    "_compat_dependency",
                    side_effect=lambda name, default: dependencies.get(name, default),
                ),
            ):
                finalized = finalization.finalize_chat_response(
                    chat_request(),
                    "질문",
                    response,
                    user_uid="user-1",
                )
            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT model_id, mode, payload, user_uid, failed_checks_json FROM validation_logs"
                ).fetchone()

        self.assertNotIn("sourcesForFollowUps", finalized)
        self.assertEqual(finalized["followUps"], ["후속 질문"])
        self.assertEqual(finalized["verification"]["status"], "warning")
        self.assertEqual(len(finalized["auditId"]), 32)
        followups.assert_called_once_with("질문", evidence, retrieval, "model")
        verification.assert_called_once_with("질문", response["content"], evidence, retrieval, "model")
        payload = json.loads(row[2])
        self.assertEqual(row[0], "fallback-model")
        self.assertEqual(row[1], "model")
        self.assertEqual(row[3], "user-1")
        self.assertEqual(payload["sources"], [])
        self.assertEqual(payload["queryPlan"], {"source": "mantis"})
        self.assertEqual(payload["retrievalStages"][0]["meta"]["displayed_count"], 1)
        self.assertEqual(json.loads(row[4]), [{"id": "grounding", "label": "근거"}])
        self.assertTrue(any(event.get("type") == "delta" for event in emitted))
        self.assertTrue(CHAT_STREAM_CONTEXT.content_streamed)

    def test_ab_comparison_uses_candidate_model_with_the_same_retrieval(self):
        retrieval = {"stages": [], "query_plan": {"source": "notion"}}
        response = {
            "content": "현재 모델 답변",
            "sources": [{"type": "notion", "title": "업무"}],
            "retrieval": retrieval,
            "raw": {"mode": "model"},
        }
        candidate = Mock(return_value={"status": "ok", "content": "후보 답변", "sources": []})
        baseline = Mock(return_value={"status": "ok", "content": "기본 답변", "sources": []})
        comparison = Mock(return_value={"winner": "A"})
        dependencies = {
            "generate_follow_up_questions": Mock(return_value=[]),
            "build_verification_report": Mock(
                return_value={
                    "verifiedAt": "2026-08-24T12:00:00+09:00",
                    "score": 100,
                    "status": "passed",
                    "checks": [],
                }
            ),
            "should_attach_ab_test": Mock(return_value=True),
            "generate_candidate_model_answer": candidate,
            "generate_basic_grounded_baseline": baseline,
            "evaluate_ab_comparison": comparison,
        }
        request = chat_request(abTest=True, abModelId="candidate-model")

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "audit.sqlite3"
            with (
                patch.object(finalization, "AUDIT_DB_PATH", database_path),
                patch.object(
                    finalization,
                    "init_audit_db",
                    side_effect=lambda: create_validation_log_table(database_path),
                ),
                patch.object(
                    finalization,
                    "_compat_dependency",
                    side_effect=lambda name, default: dependencies.get(name, default),
                ),
            ):
                finalized = finalization.finalize_chat_response(request, "질문", response)

        candidate.assert_called_once_with(request, "질문", retrieval, "candidate-model")
        baseline.assert_not_called()
        comparison.assert_called_once()
        self.assertEqual(finalized["abTest"]["variantB"]["content"], "후보 답변")
        self.assertEqual(finalized["abTest"]["evaluation"], {"winner": "A"})

    def test_ab_comparison_uses_grounded_baseline_without_a_distinct_candidate(self):
        retrieval = {"stages": []}
        response = {
            "content": "결정형 답변",
            "sources": [],
            "retrieval": retrieval,
            "raw": {"mode": "deterministic"},
        }
        candidate = Mock()
        baseline = Mock(return_value={"status": "ok", "content": "기본 답변", "sources": []})
        dependencies = {
            "generate_follow_up_questions": Mock(return_value=[]),
            "build_verification_report": Mock(
                return_value={
                    "verifiedAt": "2026-08-24T12:00:00+09:00",
                    "score": 100,
                    "status": "passed",
                    "checks": [],
                }
            ),
            "should_attach_ab_test": Mock(return_value=True),
            "generate_candidate_model_answer": candidate,
            "generate_basic_grounded_baseline": baseline,
            "evaluate_ab_comparison": Mock(return_value={"winner": "B"}),
        }

        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "audit.sqlite3"
            with (
                patch.object(finalization, "AUDIT_DB_PATH", database_path),
                patch.object(
                    finalization,
                    "init_audit_db",
                    side_effect=lambda: create_validation_log_table(database_path),
                ),
                patch.object(
                    finalization,
                    "_compat_dependency",
                    side_effect=lambda name, default: dependencies.get(name, default),
                ),
            ):
                finalized = finalization.finalize_chat_response(chat_request(abTest=True), "질문", response)

        candidate.assert_not_called()
        baseline.assert_called_once()
        self.assertEqual(finalized["abTest"]["variantB"]["label"], "기본 검색 연동")


    def test_validated_source_explanations_never_attach_free_prose_ab_variants(self):
        for completed in (True, False):
            for ab_model_id in ('candidate-model', 'primary-model'):
                with self.subTest(completed=completed, ab_model_id=ab_model_id):
                    response = {
                        'content': '검증한 원문 인용' if completed else '원문 근거가 부족해 설명을 보류합니다.',
                        'sources': [],
                        'retrieval': {'stages': [], 'query_plan': {'source': 'mantis', 'operation': 'explain'}},
                        'raw': {} if completed else {'mode': 'explanation_evidence_incomplete'},
                        'groundingReview': {'method': 'validated_source_quotes', 'completed': completed},
                    }
                    original_content = response['content']
                    candidate = Mock(return_value={'status': 'unavailable', 'content': '검증 밖 후보 답변'})
                    baseline = Mock(return_value={'status': 'unavailable', 'content': '검증 밖 기본 답변'})
                    dependencies = {
                        'generate_follow_up_questions': Mock(return_value=[]),
                        'build_verification_report': Mock(return_value={
                            'verifiedAt': '2026-10-15T12:00:00+09:00',
                            'score': 100, 'status': 'passed', 'checks': [],
                        }),
                        'generate_candidate_model_answer': candidate,
                        'generate_basic_grounded_baseline': baseline,
                        'evaluate_ab_comparison': Mock(return_value={'winner': 'A'}),
                    }
                    with tempfile.TemporaryDirectory() as directory:
                        database_path = Path(directory) / 'audit.sqlite3'
                        with (
                            patch.object(finalization, 'AUDIT_DB_PATH', database_path),
                            patch.object(finalization, 'init_audit_db',
                                         side_effect=lambda path=database_path: create_validation_log_table(path)),
                            patch.object(finalization, '_compat_dependency',
                                         side_effect=lambda name, default, deps=dependencies: deps.get(name, default)),
                        ):
                            result = finalization.finalize_chat_response(
                                chat_request(abTest=True, abModelId=ab_model_id), '이슈 원인을 설명해줘', response)
                        with closing(sqlite3.connect(database_path)) as connection:
                            payload = json.loads(connection.execute('SELECT payload FROM validation_logs').fetchone()[0])
                    self.assertNotIn('abTest', result)
                    self.assertIsNone(payload['abTest'])
                    self.assertEqual(result['content'], original_content)
                    candidate.assert_not_called()
                    baseline.assert_not_called()


if __name__ == "__main__":
    unittest.main()
