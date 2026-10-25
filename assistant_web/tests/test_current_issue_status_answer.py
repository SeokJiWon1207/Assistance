"""현재 상태는 제목·노트의 완료 표현이 아닌 같은 이슈 메타데이터로 답한다."""
import json
from unittest import TestCase
from unittest.mock import patch

from backend.evaluation.issue_facts import check_issue_fields, extract_issue_claims
from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.services.grounded_explanation import generate_grounded_explanation
from backend.sources.mantis import normalize_issue


class CurrentIssueStatusAnswerTests(TestCase):
    def answer(self, status="assigned", question="#91 이슈의 현재 상태와 다음 할 일을 간단히 요약해줘"):
        row = normalize_issue({"id": 91, "summary": "정기점검", "description": "정기점검",
                               "status": {"name": status}, "notes": []}, evidence_query=question)
        request = ChatRequest(modelId="test", messages=[{"role": "user", "content": question}])
        context = ChatPipelineContext(question, {}, "", [], question, {"mantis": True}, [],
                                      {"issue_ids": [91]}, 5, intent_plan={"operation": "explain"},
                                      retrieval_result={"stages": [{"source": "mantis_id_lookup",
                                                                  "status": "ok", "issues_list": [row]}]})
        calls = 0

        def generate(_request, _model, messages, *_args):
            nonlocal calls
            calls += 1
            if calls == 2:
                return {"content": '{"unchanged":true}'}
            shown = json.loads(messages[0]["content"].rsplit("[검색 파이프라인 결과]\n", 1)[1])
            reference = shown["issues"][0]["answer_evidence"]["excerpts"][0]["passage_id"]
            return {"content": json.dumps({"selected_issue_ids": [91],
                "sections": [["technical", [reference], True]],
                "answer_items": [["현재 상태", [reference], False, "확인된 이슈"],
                                 ["다음 할 일", [], True, "확인할 수 없습니다."]], "investigate": []}),
                "sources": [{"type": "mantis", "issue_id": 91, "status": status, "title": "정기점검"}]}

        with patch("backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id", return_value=row):
            return generate_grounded_explanation(request, context, generate)

    def test_current_status_comes_from_metadata_even_when_model_selects_title(self):
        response = self.answer()
        self.assertIn("| #91 | 할당된 이슈 |", response["content"])
        self.assertNotIn("확인된 이슈", response["content"])
        self.assertIn("다음 할 일", response["content"])
        self.assertIn("확인하지 못했습니다", response["content"])
        _ids, claims = extract_issue_claims(response["content"])
        self.assertIn((91, "status", "할당된 이슈"), claims)
        self.assertEqual(check_issue_fields(claims, response["sources"])["status"], "pass")

    def test_closed_status_remains_closed_without_inventing_next_action(self):
        response = self.answer("closed")
        self.assertIn("| #91 | 종료된 이슈 |", response["content"])
        self.assertIn("확인하지 못했습니다", response["content"])

    def test_false_explicit_status_is_still_rejected(self):
        _ids, claims = extract_issue_claims("| 이슈 ID | 상태 |\n|---|---|\n| #91 | 종료된 이슈 |")
        self.assertEqual(check_issue_fields(claims, [{"issue_id": 91, "status": "assigned"}])["status"], "fail")

    def test_matching_status_with_citation_is_not_a_false_mismatch(self):
        text = "- **현재 상태**: 확인된 이슈 (#31631 · 필드 summary)"
        _ids, claims = extract_issue_claims(text)
        self.assertEqual(check_issue_fields(claims, [{"issue_id": 31631, "status": "confirmed"}])["status"], "pass")

    def test_citation_does_not_hide_false_status_or_extra_claim(self):
        for value in ["종료된 이슈", "확인된 이슈이며 운영 반영 완료"]:
            _ids, claims = extract_issue_claims(f"- **현재 상태**: {value} (#31631 · 필드 summary)")
            self.assertEqual(check_issue_fields(claims, [{"issue_id": 31631, "status": "confirmed"}])["status"], "fail")
