"""원본 라벨 보존과 일반 목록의 기본 팀 범위 회귀."""

import unittest
from contextlib import nullcontext
from unittest.mock import patch

from backend.common import normalize_chroma_issue_document
from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.intent.confirmed_context import merge_confirmed_constraints
from backend.intent.matching import chroma_text_matches_constraints
from backend.retrieval import collect_retrieved_issues, build_model_retrieval_context
from backend.sources.mantis import normalize_issue, build_sources_from_issue_rows, issue_matches_constraints
from backend.sources.mantis_store import render_document
from backend.answers import format_deterministic_issue_answer
from backend.evaluation.issue_facts import check_issue_fields
from backend.sources.mantis_collection import IssueBatch
from backend.sources import mantis, mantis_store
from backend.query_planning import build_query_plan
from backend.retrieval import run_retrieval_pipeline
from backend.reports.aggregate import build_mantis_count_report


def raw_issue(handler="석지원"):
    return {"id": 31582, "summary": "[MTS] 조건검색", "handler": {"real_name": handler},
            "status": {"name": "closed", "label": "종료된 이슈"},
            "priority": {"name": "normal", "label": "보통"},
            "severity": {"name": "crash", "label": "추가"},
            "created_at": "2026-10-02", "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]}


class IssueListLabelsScopeTests(unittest.TestCase):

    def test_monthly_all_list_and_count_use_same_team_rows_and_labels(self):
        rows = [dict(raw_issue(name), id=31582 + i) for i, name in enumerate(("석지원", "강민성", "이영희"))]
        stored = IssueBatch(rows, {"search_complete": True, "fetchedAt": "2026-10-13T00:00:00+00:00"})
        question = "2026년 10월 MTS 이슈 전체 목록 보여줘"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        with (patch.object(mantis_store, "read_complete_store", return_value=stored),
              patch.object(mantis, "read_complete_store", return_value=stored),
              patch.object(mantis_store, "stream_complete_store", side_effect=lambda **_: nullcontext(stored), create=True),
              patch.object(mantis, "stream_complete_store", side_effect=lambda **_: nullcontext(stored), create=True),
              patch.object(mantis, "fetch_mantis_issue_by_id", side_effect=lambda n: next(r for r in rows if r["id"] == n))):
            result = run_retrieval_pipeline(question, 50, {"mantis": True}, plan)
            count_question = "2026년 10월 MTS 이슈 몇 건이야"
            count_plan = build_query_plan(count_question, {"source": "mantis"}, extract_query_constraints(count_question))
            report = build_mantis_count_report(count_question, count_plan)
        sources = build_sources_from_issue_rows(collect_retrieved_issues(result, None))
        self.assertEqual({s["issue_id"] for s in sources}, {31582, 31584})
        self.assertEqual({s["priority_label"] for s in sources}, {"보통"})
        self.assertEqual({s["status_label"] for s in sources}, {"종료된 이슈"})
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["total_count"], 2)

    def test_live_and_snapshot_preserve_source_labels_through_response(self):
        raw = raw_issue()
        raw["status"]["label"] = "원본 종료 표시"
        for issue in [normalize_issue(raw), normalize_chroma_issue_document(render_document(raw))]:
            with self.subTest(path="snapshot" if "content" in issue else str(issue)):
                retrieval = {"stages": [{"source": "mantis_snapshot", "status": "ok", "issues_list": [issue]}]}
                source = build_sources_from_issue_rows(collect_retrieved_issues(retrieval, None))[0]
                self.assertEqual(source.get("status_label"), "원본 종료 표시")
                self.assertEqual(source.get("priority_label"), "보통")
                self.assertEqual(source["status"], "closed")
                self.assertEqual(source["priority"], "normal")
                model = build_model_retrieval_context(retrieval, 5)["issues"][0]
                self.assertEqual(model["status"], "원본 종료 표시")
                self.assertEqual(model["priority"], "보통")

    def test_answer_and_fact_check_accept_original_custom_status_label(self):
        raw = raw_issue()
        raw["status"]["label"] = "원본 종료 표시"
        retrieval = {"stages": [{"source": "mantis_snapshot", "status": "ok",
                                "issues_list": [normalize_issue(raw)]}]}
        source = build_sources_from_issue_rows(collect_retrieved_issues(retrieval, None))[0]
        content = format_deterministic_issue_answer("이슈 보여줘", {}, retrieval, 5)
        self.assertIn("원본 종료 표시", content)
        self.assertEqual(check_issue_fields([(31582, "status", "원본 종료 표시")], [source])["status"], "pass")

    def test_general_all_and_count_exclude_external_handler_in_both_sources(self):
        for question in ["이슈 보여줘", "10월 MTS 이슈 전체 목록 보여줘", "전체 미종료 이슈", "MTS 이슈 몇 건이야"]:
            constraints = extract_query_constraints(question)
            for handler, expected in [("석지원", True), ("강민성", False), ("석지원외부", False)]:
                raw = raw_issue(handler)
                raw["status"] = {"name": "assigned", "label": "할당된 이슈"}
                with self.subTest(question=question, handler=handler):
                    self.assertEqual(issue_matches_constraints(raw, constraints), expected)
                    self.assertEqual(chroma_text_matches_constraints(render_document(raw), constraints), expected)

    def test_model_resolved_mantis_scope_defaults_to_team(self):
        constraints = merge_query_plan_constraints(extract_query_constraints("MTS 전체"),
                                                   {"source": "mantis", "grounding_text": "MTS 전체"})
        self.assertFalse(issue_matches_constraints(raw_issue("강민성"), constraints))

    def test_explicit_targets_and_notion_keep_their_scope(self):
        for question in ["내 이슈 보여줘", "담당자 강민성 이슈", "#31582 자세히", "전사 이슈 보여줘", "노션 이슈 문서 보여줘"]:
            with self.subTest(question=question):
                self.assertNotIn("mantis_team_members", extract_query_constraints(question))

    def test_old_snapshot_keeps_priority_without_label(self):
        old = render_document(raw_issue())
        old = "\n".join(line for line in old.splitlines() if "표시명:" not in line)
        issue = normalize_chroma_issue_document(old)
        self.assertEqual(issue.get("priority"), "normal")

    def test_missing_labels_do_not_consume_the_next_metadata_line(self):
        raw = raw_issue()
        for key in ("status", "priority", "severity"):
            raw[key].pop("label")
        issue = normalize_chroma_issue_document(render_document(raw))
        for key in ("status_label", "priority_label", "severity_label"):
            self.assertEqual(issue.get(key), "")

    def test_confirmed_followups_keep_all_scope_or_restore_team_after_owner_clear(self):
        for previous, question, expected in [
            ("전사 이슈 보여줘", "그중 MTS 이슈만 보여줘", True),
            ("담당자 강민성 이슈 보여줘", "담당자 조건 빼줘", False),
            ("10월 MTS 이슈 보여줘", "전체 목록 보여줘", False),
        ]:
            with self.subTest(previous=previous, question=question):
                current = merge_confirmed_constraints(extract_query_constraints(previous),
                                                       extract_query_constraints(question), question)
                current = merge_query_plan_constraints(current, {"source": "mantis", "grounding_text": question})
                self.assertEqual(issue_matches_constraints(raw_issue("강민성"), current), expected)


if __name__ == "__main__":
    unittest.main()
