"""원본 라벨 보존과 일반 목록의 기본 팀 범위 회귀."""

import unittest

from backend.common import normalize_chroma_issue_document
from backend.retrieval import collect_retrieved_issues, build_model_retrieval_context
from backend.sources.mantis import normalize_issue, build_sources_from_issue_rows
from backend.sources.mantis_store import render_document
from backend.answers import format_deterministic_issue_answer
from backend.evaluation.issue_facts import check_issue_fields


def raw_issue(handler="석지원"):
    return {"id": 31582, "summary": "[MTS] 조건검색", "handler": {"real_name": handler},
            "status": {"name": "closed", "label": "종료된 이슈"},
            "priority": {"name": "normal", "label": "보통"},
            "severity": {"name": "crash", "label": "추가"},
            "created_at": "2026-10-02", "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]}


class IssueListLabelsScopeTests(unittest.TestCase):


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



if __name__ == "__main__":
    unittest.main()
