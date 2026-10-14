"""세대 캐시·구조화 후속 조건·빠른 조회·운영 지연 통계 회귀."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.sources import mantis_store as store
from backend.intent.constraints import extract_query_constraints
from backend.intent.confirmed_context import confirmed_constraints_scope, merge_confirmed_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.evaluation.latency import latency_summary
from backend.services import chat_service as chat
from backend.schemas import ChatRequest, ChatMessage
from backend.query_planning import build_query_plan


class GenerationCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "snapshot.sqlite3"
        for target, value in (("store_path", self.path), ("connection_key", "synthetic")):
            mock = patch.object(store, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        self.sync(1)

    def sync(self, issue_id):
        store.sync_complete_store(lambda page: [{"id": issue_id, "description": "본문",
            "notes": [{"text": "메모"}], "handler": {"name": "테스트"}}] if page == 1 else [], page_size=2)

    def test_concurrent_requests_decode_generation_once(self):
        with patch.object(store, "_load_complete_store", wraps=store._load_complete_store) as load:
            with ThreadPoolExecutor(max_workers=8) as pool:
                batches = list(pool.map(lambda _: store.read_complete_store(), range(16)))
            self.assertEqual(load.call_count, 1)
        self.assertTrue(all(rows[0] is batches[0][0] for rows in batches))
        self.assertIsNot(batches[0], batches[1])

    def test_nested_mutations_are_rejected_and_copies_are_isolated(self):
        rows = store.read_complete_store()
        with self.assertRaises(TypeError):
            rows[0]["handler"]["name"] = "변조"
        with self.assertRaises(TypeError):
            rows[0]["notes"].append({"text": "변조"})
        copy = deepcopy(rows[0])
        copy["notes"][0]["text"] = "복사본"
        rows.meta["search_complete"] = False
        rows.clear()
        fresh = store.read_complete_store()
        self.assertEqual(fresh[0]["notes"][0]["text"], "메모")
        self.assertTrue(fresh.meta["search_complete"])

    def test_replace_invalidates_and_bounds_generations(self):
        for value in range(1, 5):
            self.sync(value)
            self.assertEqual(store.read_complete_store()[0]["id"], value)
            self.assertNotIn("description", store.read_complete_store(summary_only=True)[0])
            self.assertLessEqual(len(store._generation_cache), 2)

    def test_in_place_change_invalidates(self):
        store.read_complete_store()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE issues SET payload=?", (json.dumps({"id": 1, "summary": "수정"}),))
        self.assertEqual(store.read_complete_store()[0]["summary"], "수정")

    def test_expiry_is_rechecked_on_hit_and_clock_rollback_rejected(self):
        rows = store.read_complete_store()
        timestamp = datetime.fromisoformat(rows.meta["fetchedAt"]).timestamp()
        for now in (timestamp + 3601, timestamp - 1):
            with patch.object(store.time, "time", return_value=now):
                self.assertIsNone(store.read_complete_store())
                self.assertFalse(store.read_complete_store(allow_stale=True).meta["search_complete"])

    def test_deleted_store_and_connection_change_cannot_serve_cached_rows(self):
        store.read_complete_store()
        with patch.object(store, "connection_key", return_value="another-account"):
            self.assertIsNone(store.read_complete_store(allow_stale=True))
        self.path.unlink()
        self.assertIsNone(store.read_complete_store(allow_stale=True))

    def test_failed_load_and_mid_read_replacement_are_not_cached(self):
        original = store._load_complete_store
        with patch.object(store, "_load_complete_store", return_value=None):
            self.assertIsNone(store.read_complete_store())
        def changed(**kwargs):
            rows = original(**kwargs)
            self.sync(2)
            return rows
        with patch.object(store, "_load_complete_store", side_effect=changed):
            self.assertIsNone(store.read_complete_store())
        self.assertEqual(store.read_complete_store()[0]["id"], 2)

    def test_request_pins_generation_until_next_request(self):
        with store.store_request_scope():
            self.assertEqual(store.read_complete_store()[0]["id"], 1)
            self.sync(2)
            self.assertEqual(store.read_complete_store()[0]["id"], 1)
        self.assertEqual(store.read_complete_store()[0]["id"], 2)


class StructuredFollowupTests(unittest.TestCase):
    def test_exclusion_cannot_leak_into_next_status_or_platform(self):
        q = "우리 팀 NH 제외 미종료 MTS 이슈 몇 건이야?"
        result = extract_query_constraints(q)
        self.assertEqual(result["statuses"], ["open"])
        self.assertEqual(result["excluded_statuses"], [])
        self.assertEqual(result["platform_key"], "mts")
        self.assertTrue(result["excluded_customer_keys"])

    def test_quoted_title_filter_survives_followup_and_filters_actual_rows(self):
        from backend.sources.mantis import select_mantis_rows
        prior = extract_query_constraints('NH 제목에 "차트" 포함된 미종료 이슈 2개 보여줘')
        clause = {"field": "title", "operator": "contains", "value": "차트"}
        self.assertIn(clause, prior["filter_clauses"])
        current = merge_confirmed_constraints(prior, extract_query_constraints("그중 MTS만"), "그중 MTS만")
        rows = [{"id": i, "summary": title, "description": "차트", "project": {"name": "120NH(NH증권)"},
                 "handler": {"real_name": "석지원"}, "status": {"name": "assigned"}, "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]}
                for i, title in enumerate(("차트 표시 오류", "로그인 오류"), 1)]
        self.assertEqual([row["id"] for row in select_mantis_rows(rows, current)], [1])
        self.assertEqual(build_keyword_terms('NH 제목에 "차트" 포함된 미종료 이슈 2개 보여줘', prior), [])

    def test_quoted_status_and_customer_are_only_title_literals(self):
        c = extract_query_constraints('NH 제목에 "KB 종료" 포함된 이슈')
        self.assertEqual(c["customer_key"], "NH")
        self.assertEqual(c["statuses"], [])
        self.assertIn({"field": "title", "operator": "contains", "value": "KB 종료"}, c["filter_clauses"])

    def test_preserves_keyword_boolean_clauses_and_identifier_bound(self):
        previous = extract_query_constraints("NH MTS 미종료 이슈")
        clauses = [{"field": "keyword", "operator": "in", "value": ["로그인", "차트"]},
                   {"field": "title", "operator": "not_contains", "value": "테스트"}]
        previous["filter_clauses"] += deepcopy(clauses)
        previous["max_issue_id_exclusive"] = 32000
        previous["inherited_terms"] = ["로그인"]
        result = merge_confirmed_constraints(previous, extract_query_constraints("그중 KB만"), "그중 KB만")
        self.assertEqual(result["customer_key"], "kb")
        self.assertEqual(result["platform_key"], "mts")
        self.assertEqual(result["max_issue_id_exclusive"], 32000)
        self.assertTrue(all(c in result["filter_clauses"] for c in clauses))
        self.assertIn("로그인", build_keyword_terms("그중 KB만", result))
        result["filter_clauses"][-1]["value"] = "변경"
        self.assertNotEqual(previous, result)

    def test_explicit_dimensions_replace_and_all_status_clears(self):
        previous = extract_query_constraints("NH MTS 미종료 담당자 미지정 화면번호 0090 이슈")
        q = "그중 KB HTS 전체 상태 담당자 이영희 화면번호 0080"
        result = merge_confirmed_constraints(previous, extract_query_constraints(q), q)
        self.assertEqual(result["statuses"], [])
        self.assertEqual(result["screen_numbers"], ["0080"])
        self.assertEqual(result["people"]["names"], ["이영희"])
        self.assertFalse(result["unassigned_people"])
        self.assertFalse(any(c["field"] == "status" for c in result["filter_clauses"]))

    def test_confirmed_scope_is_question_bound_nested_and_exception_safe(self):
        q = "그중 KB만"
        with self.assertRaises(RuntimeError), confirmed_constraints_scope(q, {"statuses": ["open"]}):
            self.assertEqual(extract_query_constraints(q)["statuses"], ["open"])
            self.assertEqual(extract_query_constraints("NH 이슈")["statuses"], [])
            with confirmed_constraints_scope(q, {"statuses": ["developed"]}):
                self.assertEqual(extract_query_constraints(q)["statuses"], ["developed"])
            self.assertEqual(extract_query_constraints(q)["statuses"], ["open"])
            raise RuntimeError("test")
        self.assertEqual(extract_query_constraints(q)["statuses"], [])

    def test_actual_prepare_uses_authenticated_plan_without_reparsing_clauses(self):
        prior = extract_query_constraints("NH MTS 미종료 이슈")
        clause = {"field": "title", "operator": "not_contains", "value": "테스트"}
        prior["filter_clauses"].append(clause)
        q = "그중 KB만 보여줘"
        with patch.object(chat, "get_private_model", return_value={"status": "active"}), \
             patch.object(chat, "get_model_api_key", return_value="synthetic"), \
             patch.object(chat, "load_audit_query_plan", return_value={"source": "mantis", "resolved_constraints": prior, "terms": ["로그인"]}), \
             patch.object(chat, "rewrite_follow_up_query_with_model") as rewrite:
            request = ChatRequest(modelId="test", contextAuditId="a" * 32,
                messages=[ChatMessage(role="user", content="NH MTS 미종료 로그인 이슈"), ChatMessage(role="user", content=q)])
            context = chat.prepare_chat_pipeline(request, q, {"uid": "user"})
        rewrite.assert_not_called()
        self.assertIn(clause, context.query_constraints["filter_clauses"])
        self.assertEqual(context.query_constraints["inherited_terms"], ["로그인"])
        with confirmed_constraints_scope(context.retrieval_text, context.query_constraints):
            self.assertIn(clause, extract_query_constraints(context.retrieval_text)["filter_clauses"])

    def test_structured_date_is_not_replaced_by_reparsed_text(self):
        c = extract_query_constraints("NH 2025년 3월 이슈")
        plan = build_query_plan("그중 KB만", {"source": "mantis"}, c)
        self.assertEqual(plan["filters"]["created_at"], c["created_at"])

    def test_unknown_customer_does_not_silently_keep_prior_customer(self):
        previous = extract_query_constraints("NH 미종료 이슈")
        current = extract_query_constraints("그중 가상없는증권만 보여줘")
        result = merge_confirmed_constraints(previous, current, "그중 가상없는증권만 보여줘")
        self.assertTrue(result["unknown_customers"])
        self.assertIsNone(result["customer_key"])

    def test_list_fast_path_retains_ambiguous_searches_for_planner(self):
        for q, expected in (("NH MTS 미종료 이슈 보여줘", True), ("우리 팀 이슈 목록 보여줘", True),
                            ("NH MTS 이슈 최근 5개 보여줘", True), ("NH 로그인 장애 이슈 보여줘", False),
                            ("NH MTS 알려줘", False), ("NH 이슈 담당자별로", False),
                            ("NH 미종료 이슈 원인 분석", False), ("NH 미종료 이슈 오래된 순", False)):
            self.assertEqual(chat.can_skip_general_planner(q, extract_query_constraints(q), {"source": "mantis"}), expected, q)

    def test_actual_list_planning_skips_general_model(self):
        import os
        q = "NH MTS 미종료 이슈 최근 2개 보여줘"
        context = chat.ChatPipelineContext(q, {}, "synthetic", [], q, {"mantis": True}, [], extract_query_constraints(q), 5)
        with patch.dict(os.environ, {"QUERY_PLANNER_ENABLED": "true"}), \
             patch.object(chat, "plan_search_intent_with_model") as planner:
            result = chat.plan_chat_query(ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=q)]), context, None).response
        planner.assert_not_called()
        self.assertIsNone(result)
        self.assertEqual(context.intent_plan["operation"], "search")

    def test_complete_structured_snapshot_skips_only_redundant_scan(self):
        from backend import retrieval
        for tool, complete, scan_needed in (("mantis_complete_snapshot", True, False),
                                            ("mantis_complete_snapshot", False, True),
                                            ("mantis_chroma_snapshot", True, True)):
            stage = {"source": "mantis_snapshot", "status": "ok", "issues_list": [],
                     "meta": {"tool": tool, "search_complete": complete}}
            with patch.object(retrieval, "execute_mantis_snapshot_search", return_value=stage), \
                 patch.object(retrieval, "revalidate_mantis_snapshot_stage", side_effect=lambda stage, _: stage), \
                 patch.object(retrieval, "get_mantis_snapshot_freshness", return_value=None), \
                 patch.object(retrieval, "execute_mantis_filter_search", return_value={"status": "skipped"}), \
                 patch.object(retrieval, "execute_mantis_page_scan", return_value={"status": "ok"}) as scan:
                retrieval.run_retrieval_pipeline("NH MTS 미종료 이슈 보여줘", 5, {"mantis": True}, {"source": "mantis"})
            self.assertEqual(scan.called, scan_needed)


class LatencySummaryTests(unittest.TestCase):
    def test_admin_summary_exposes_latency_without_individual_timing_questions(self):
        from backend.api import quality
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE validation_logs (id, created_at, question, model_id, mode, score, status, failed_checks_json, payload)")
                db.execute("INSERT INTO validation_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("1", "2026-10-01", "검증 질문", "test", "count", 100, "pass", "[]",
                     '{"timings":{"beforeFinalizationMs":123}}'))
            with patch.object(quality, "AUDIT_DB_PATH", path), patch.object(quality, "init_audit_db"):
                result = quality.get_validation_summary()
            self.assertEqual(result["latency"]["sampleCount"], 1)
            self.assertNotIn("검증 질문", json.dumps(result["latency"], ensure_ascii=False))

    def test_percentiles_ignore_invalid_legacy_values_and_separate_models(self):
        rows = [{"payload": json.dumps({"timings": {"beforeFinalizationMs": value,
                 "stages": {"store_read": {"durationMs": value / 2}}}}), "mode": "count", "model_id": "a"}
                for value in range(1, 21)]
        rows += [{"payload": payload, "mode": "legacy", "model_id": "b"}
                 for payload in ('{}', 'bad', '[]', '{"timings": {"beforeFinalizationMs": -1}}',
                                 '{"timings": {"beforeFinalizationMs": true}}', '{"timings": {"beforeFinalizationMs": NaN}}')]
        result = latency_summary(rows)
        self.assertEqual(result["sampleCount"], 20)
        metric = result["groups"]["all"]["beforeFinalizationMs"]
        self.assertEqual(metric, {"count": 20, "medianMs": 10.5, "p95Ms": 19.0})
        self.assertEqual(result["groups"]["all"]["stage:store_read"]["p95Ms"], 9.5)
        self.assertNotIn("model:b", result["groups"])

    def test_empty_and_single_sample(self):
        self.assertEqual(latency_summary([])["groups"], {})
        result = latency_summary([{"payload": '{"timings":{"beforeFinalizationMs":0}}', "mode": "", "model_id": ""}])
        self.assertEqual(result["groups"]["all"]["beforeFinalizationMs"], {"count": 1, "medianMs": 0, "p95Ms": 0})
