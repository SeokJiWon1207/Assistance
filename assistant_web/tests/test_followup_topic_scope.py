"""후속 질문의 주제 교체와 원문 검색을 실제 서비스 경계에서 검증한다."""

import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

from backend import retrieval
from backend.intent.confirmed_context import confirmed_constraints_scope
from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.sources import chroma, mantis, mantis_store
from backend.sources.mantis_collection import IssueBatch


FIRST = "HTS에서 VB 스크립트를 자바 스크립트로 바꿔달라는 이슈 있었어?"
FOLLOWUP = "그럼 VBScript 관련해서 들어온 곳은?"


def issue(identifier, project, note, media="HTS", **fields):
    return {"id": identifier, "summary": "화면 확인 요청", "description": "일반 설명", "handler": {"real_name": "석지원"},
            "project": {"name": project}, "notes": [{"id": identifier * 10, "text": note}],
            "custom_fields": [{"field": {"name": "매체구분"}, "value": media}], **fields}


class FollowupTopicScopeTests(unittest.TestCase):
    def followup(self, question=FOLLOWUP, plan=None, previous=FIRST):
        plan = plan or {"source": "mantis", "terms": ["VB", "스크립트", "자바"],
                        "resolved_constraints": extract_query_constraints(FIRST)}
        request = ChatRequest(modelId="fixture-model", contextAuditId="a" * 32, messages=[
            ChatMessage(role="user", content=previous),
            ChatMessage(role="assistant", content="전환 요청은 확인되지 않았습니다."),
            ChatMessage(role="user", content=question),
        ])
        with (patch.object(chat, "get_private_model", return_value={"id": "fixture-model", "status": "active"}),
              patch.object(chat, "get_model_api_key", return_value="fixture-key"),
              patch.object(chat, "load_audit_query_plan", return_value=plan),
              patch.object(chat, "resolve_reference_from_context", return_value=None),
              patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("unexpected rewrite"))):
            return chat.prepare_chat_pipeline(request, question, {"uid": "fixture-user"})

    def test_new_related_topic_replaces_old_keywords_and_preserves_platform(self):
        context = self.followup()
        self.assertEqual(context.query_constraints["platform_key"], "hts")
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), ["VBScript"])
        self.assertNotIn("자바", context.retrieval_text)
        self.assertFalse(any("자바" in str(c) for c in context.query_constraints["filter_clauses"]))

    def test_customer_or_status_refinement_preserves_topic(self):
        for question in ("그중 SK증권만 보여줘", "그중 종료된 것만 보여줘"):
            with self.subTest(question=question):
                context = self.followup(question)
                self.assertEqual(context.query_constraints["inherited_terms"], ["VB", "스크립트", "자바"])

    def test_related_phrase_inside_existing_results_keeps_previous_keywords(self):
        context = self.followup("그중 JavaScript 관련 이슈만 보여줘")
        self.assertIn("자바", context.query_constraints["inherited_terms"])
        self.assertIn("JavaScript", build_keyword_terms(context.retrieval_text, context.query_constraints))

    def test_topic_replacement_is_generic_and_preserves_structured_scope(self):
        plan = {"source": "mantis", "terms": ["로그인"],
                "resolved_constraints": extract_query_constraints('2025년 SK증권 HTS 종료 이슈 제목에 "로그인" 포함')}
        context = self.followup("그러면 차트 관련해서 들어온 고객사는?", plan=plan)
        self.assertEqual(context.query_constraints["customer_key"], "SK증권")
        self.assertEqual(context.query_constraints["created_at"]["from"], "2025-01-01")
        self.assertEqual(context.query_constraints["statuses"], ["closed"])
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), ["차트"])
        self.assertNotIn("로그인", str(context.query_constraints["filter_clauses"]))

    def test_conversational_customer_request_words_are_not_search_terms(self):
        self.assertEqual(build_keyword_terms(FOLLOWUP, extract_query_constraints(FOLLOWUP)), ["VBScript"])

    def test_third_turn_inherits_only_replaced_topic(self):
        context = self.followup()
        plan = {"source": "mantis", "terms": build_keyword_terms(context.retrieval_text, context.query_constraints),
                "resolved_constraints": context.query_constraints}
        next_context = self.followup("그중 SK증권만 보여줘", plan=plan, previous=FOLLOWUP)
        self.assertEqual(next_context.query_constraints["inherited_terms"], ["VBScript"])
        self.assertNotIn("자바", next_context.retrieval_text)

    def test_full_followup_retrieval_finds_note_aliases_only_with_matching_platform_fields(self):
        rows = [issue(901, "IBK투자증권", "NXT 맵차트 호출 VB Scripts를 전송드립니다."),
                issue(902, "SK증권", "VBScript event Fire 중 포인터 NULL 확인"),
                issue(903, "하이투자증권", "VBScript에서 GetSelectCode 호출"),
                issue(904, "미래에셋증권", "vbscript.dll 교체 후 화면 속도 확인", "",
                      additional_information="안녕하세요 HTS개발팀입니다."),
                issue(905, "내부자료", "VBScript 참고 자료", ""),
                issue(906, "모바일고객", "VBScript 문의", "MTS"),
                issue(907, "다른고객", "Visual Basic 사용 문의")]
        # 검색어가 오래된 노트의 긴 본문 뒤에 있어도 답변 근거에서 누락하지 않는다.
        rows[1]["notes"][0]["text"] = "앞선 설명 " * 600 + rows[1]["notes"][0]["text"]
        rows[1]["notes"].extend({"id": 10000 + i, "text": "이후 진행 상황"} for i in range(15))
        stored = IssueBatch(rows, {"search_complete": True, "snapshot_complete": True, "status": "snapshot"})
        context = self.followup()
        with (patch.object(mantis, "read_complete_store", return_value=stored),
              patch.object(mantis, "stream_complete_store", return_value=nullcontext(stored), create=True),
              patch.object(retrieval, "execute_mantis_filter_search", return_value={"source": "mantis_filter", "status": "skipped"}),
              confirmed_constraints_scope(context.retrieval_text, context.query_constraints)):
            plan = build_query_plan(context.retrieval_text, {"source": "mantis"}, context.query_constraints)
            result = retrieval.run_retrieval_pipeline(context.retrieval_text, 10, {"mantis": True}, plan)
        self.assertEqual({row["id"] for row in retrieval.collect_retrieved_issues(result, None)}, {901, 902, 903})
        self.assertEqual(retrieval.count_retrieved_issues(result), 3)
        grounding = retrieval.build_model_retrieval_context(result, 10)
        for row in grounding["issues"]:
            self.assertTrue(row.get("matching_evidence"), row["id"])
            self.assertIn("vbscript", str(row["matching_evidence"]).replace(" ", "").casefold())

    def test_vector_refresh_preserves_spaced_note_match_and_model_evidence(self):
        old = issue(901, "IBK투자증권", "VBScript 확인")
        current = issue(901, "IBK투자증권", "NXT 맵차트 호출 VB Scripts를 전송드립니다.")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.sqlite3"
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("CREATE TABLE embedding_metadata (key TEXT, string_value TEXT)")
                db.execute("INSERT INTO embedding_metadata VALUES ('chroma:document', ?)", (mantis_store.render_document(old),))
            with (patch.object(chroma, "CHROMA_DB_PATH", path),
                  patch.object(chroma, "fetch_mantis_issue_by_id", return_value=current)):
                stage = chroma.execute_chroma_search("VBScript", 5, extract_query_constraints("HTS"))
        self.assertEqual([doc["issue_id"] for doc in stage["documents"]], [901])
        grounding = retrieval.build_model_retrieval_context({"stages": [stage]}, 5)
        self.assertIn("VB Scripts", str(grounding["issues"][0]["matching_evidence"]))

    def test_structured_customer_refinement_keeps_old_note_evidence_after_refresh(self):
        raw = issue(901, "SK증권", "VBScript 호출 연결 포인터 확인")
        raw["notes"].extend({"id": 10000 + i, "text": "후속 확인"} for i in range(15))
        constraints = extract_query_constraints('SK증권 HTS 키워드 "VBScript" 포함')
        stage = {"source": "mantis_snapshot", "status": "ok", "issues_list": [mantis.normalize_issue(raw)],
                 "meta": {"matched_issue_ids": [901], "displayed_count": 1, "search_complete": True}}
        with patch.object(mantis, "fetch_mantis_issue_by_id", return_value=raw):
            refreshed = mantis.revalidate_mantis_snapshot_stage(stage, constraints)
        grounding = retrieval.build_model_retrieval_context({"stages": [refreshed]}, 5)
        self.assertIn("VBScript", str(grounding["issues"][0]["matching_evidence"]))


if __name__ == "__main__":
    unittest.main()
