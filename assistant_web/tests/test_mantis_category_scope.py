"""실제 분류 필터와 대화 상속을 원문 속성으로 검사한다."""

import unittest

from backend.answers import display_constraints
from backend.evaluation.issue_facts import check_issue_fields, has_definite_scope_mismatch
from backend.intent.confirmed_context import confirmed_scope_text, merge_confirmed_constraints
from backend.intent.constraints import extract_query_constraints
from backend.intent.matching import chroma_text_matches_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan, validate_query_plan
from backend.reports.aggregate import format_issue_list_criteria
from backend.sources.mantis import build_sources_from_issue_rows, normalize_issue, select_mantis_rows
from backend.team_templates import load_team_templates
from backend.template_questions import fill_template_question
import test_all_list_scope_followups as all_lists


BASE = "2026년 10월 MTS Mantis 분류=조건검색 이슈 전체 목록을 표로 보여줘"
CATEGORY = {"field": "category", "operator": "equals", "value": "조건검색"}


def raw(identifier, category="조건검색", title="화면 UI 개선"):
    return {"id": identifier, "summary": title, "category": {"id": identifier, "name": category},
            "project": {"name": "120NH(NH증권)"}, "handler": {"real_name": "우동헌"},
            "created_at": "2026-10-02", "custom_fields": [
                {"field": {"name": "매체구분"}, "value": "MTS"},
                {"field": {"name": "2서버공수"}, "value": "0.00"}]}


class MantisCategoryScopeTests(unittest.TestCase):
    def test_explicit_category_is_a_filter_and_never_a_keyword(self):
        for question in (BASE, "분류가 조건검색인 Mantis 이슈 전부 보여줘",
                         "Mantis 카테고리='03 서버' 이슈 전체 목록 보여줘"):
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                self.assertEqual(build_keyword_terms(question, constraints), [])
                self.assertTrue(any(c["field"] == "category" for c in constraints["filter_clauses"]))

    def test_exact_field_excludes_title_mentions_and_missing_categories(self):
        constraints = extract_query_constraints(BASE)
        rows = [raw(1), raw(2, "차트", "조건검색 오류"), raw(3, "조건검색 서버"), raw(4)]
        rows[-1].pop("category")
        self.assertEqual([r["id"] for r in select_mantis_rows(rows, constraints)], [1])
        # 서버공수가 0이어도 선택한 분류에 포함되어야 한다.
        self.assertEqual(normalize_issue(rows[0])["category"], "조건검색")

    def test_quoted_title_is_not_a_category_filter(self):
        constraints = extract_query_constraints('Mantis 제목에 "분류=조건검색" 포함된 이슈 보여줘')
        self.assertFalse(any(c["field"] == "category" for c in constraints["filter_clauses"]))
        constraints = extract_query_constraints("Mantis 이슈를 분류별로 요약해줘")
        self.assertFalse(any(c["field"] == "category" for c in constraints["filter_clauses"]))

    def test_category_change_replaces_only_category(self):
        previous = extract_query_constraints(BASE)
        question = "분류는 차트로 바꿔줘"
        merged = merge_confirmed_constraints(previous, extract_query_constraints(question), question)
        self.assertEqual([c for c in merged["filter_clauses"] if c["field"] == "category"],
                         [{**CATEGORY, "value": "차트"}])
        self.assertEqual(merged["platform_key"], "mts")
        self.assertEqual(merged["created_at"], previous["created_at"])
        self.assertEqual(build_keyword_terms(confirmed_scope_text(merged), merged), [])

    def test_category_clear_does_not_return_in_next_turn(self):
        previous = extract_query_constraints(BASE)
        question = "분류 조건 빼줘"
        merged = merge_confirmed_constraints(previous, extract_query_constraints(question), question)
        next_merged = merge_confirmed_constraints(merged, extract_query_constraints("더 보여줘"), "더 보여줘")
        self.assertFalse(any(c["field"] == "category" for c in next_merged["filter_clauses"]))
        self.assertEqual(next_merged["platform_key"], "mts")

    def test_category_exclusion_and_space_separated_name_survive_followup(self):
        for question in ("Mantis 분류='03 서버' 이슈 보여줘", "Mantis 분류=조건검색 제외하고 이슈 보여줘"):
            constraints = extract_query_constraints(question)
            restored = extract_query_constraints(confirmed_scope_text(constraints))
            self.assertEqual(restored["filter_clauses"], constraints["filter_clauses"])

    def test_category_clear_and_negated_clear(self):
        previous = extract_query_constraints(BASE)
        for question, expected in (("분류 조건 해제하지 말고 HTS로 바꿔줘", True),
                                   ("분류는 전체로 바꿔줘", False)):
            merged = merge_confirmed_constraints(previous, extract_query_constraints(question), question)
            self.assertEqual(any(c["field"] == "category" for c in merged["filter_clauses"]), expected)

    def test_category_lookup_and_keep_phrases_do_not_invent_values(self):
        for question in ("#31620 분류 알려줘", "#31620 분류가 뭐야?"):
            constraints = extract_query_constraints(question)
            self.assertFalse(any(c["field"] == "category" for c in constraints["filter_clauses"]))
            self.assertEqual(constraints["issue_ids"], [31620])
        helper = all_lists.AllListScopeFollowupTests()
        plan = {"source": "mantis", "grounding_text": BASE, "terms": [],
                "resolved_constraints": extract_query_constraints(BASE)}
        for question in ("분류 조건은 해제하지 말고 HTS로 바꿔줘", "분류는 전체로 바꾸지 말고 HTS로 바꿔줘"):
            _, context = helper.prepare(BASE, plan, question)
            self.assertIn(CATEGORY, context.query_constraints["filter_clauses"])
            self.assertEqual(context.query_constraints["platform_key"], "hts")
            self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), [])

    def test_other_scope_changes_and_continuations_preserve_category(self):
        helper = all_lists.AllListScopeFollowupTests()
        previous = BASE
        constraints = extract_query_constraints(BASE)
        for question in ("HTS로 바꿔줘", "9월로 바꿔줘", "NH증권만 보여줘",
                         "담당자는 우동헌으로 바꿔줘", "나머지도 보여줘", "더 보여줘", "전부 보여줘"):
            plan = {"source": "mantis", "grounding_text": previous, "terms": [],
                    "resolved_constraints": constraints}
            with self.subTest(question=question):
                _, context = helper.prepare(previous, plan, question)
                constraints = context.query_constraints
                self.assertIn(CATEGORY, constraints["filter_clauses"])
                self.assertIn("분류=조건검색", context.retrieval_text)
                self.assertEqual(build_keyword_terms(context.retrieval_text, constraints), [])
            previous = question

    def test_category_changes_are_scope_edits_with_clean_keywords(self):
        helper = all_lists.AllListScopeFollowupTests()
        plan = {"source": "mantis", "grounding_text": BASE, "terms": [],
                "resolved_constraints": extract_query_constraints(BASE)}
        for question in ("분류는 차트로 바꿔줘", "분류 조건 빼줘", "분류는 전체로 바꿔줘"):
            with self.subTest(question=question):
                _, context = helper.prepare(BASE, plan, question)
                self.assertTrue(context.query_constraints["list_scope_edit"])
                self.assertTrue(context.query_constraints["list_show_all"])
                self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), [])

    def test_category_is_displayed_in_chip_and_answer(self):
        constraints = extract_query_constraints(BASE)
        self.assertIn({"id": "category", "label": "분류", "value": "조건검색", "values": ["조건검색"]},
                      display_constraints(constraints))
        self.assertIn("분류=조건검색", format_issue_list_criteria(constraints, []))

    def test_sources_carry_category_and_verification_rejects_mismatch(self):
        sources = build_sources_from_issue_rows([normalize_issue(raw(1))])
        self.assertEqual(sources[0]["category"], "조건검색")
        self.assertEqual(check_issue_fields([(1, "category", "차트")], sources)["status"], "fail")
        sources[0]["category"] = "차트"
        sources[0]["detail"]["category"] = "차트"
        self.assertTrue(has_definite_scope_mismatch(sources[0], extract_query_constraints(BASE)))

    def test_legacy_vector_document_without_category_cannot_match(self):
        text = "이슈 ID: 1\n프로젝트: 120NH(NH증권)\n현재 상태: assigned\n담당자: 우동헌\n등록일: 2026-10-02\n매체구분: MTS\n제목: 조건검색 UI 개선\n"
        constraints = extract_query_constraints(BASE)
        self.assertFalse(chroma_text_matches_constraints(text, constraints))
        self.assertTrue(chroma_text_matches_constraints("분류: 조건검색\n" + text, constraints))
        self.assertFalse(chroma_text_matches_constraints("분류: 차트\n" + text, constraints))
        self.assertFalse(chroma_text_matches_constraints(text + "분류: 조건검색\n", constraints))

    def test_model_cannot_invent_or_override_category(self):
        for question, constraints in ((BASE, extract_query_constraints(BASE)),
                                      ("Mantis 이번 달 이슈 보여줘", extract_query_constraints("Mantis 이번 달 이슈 보여줘"))):
            plan = build_query_plan(question, {"source": "mantis"}, constraints,
                                    {"filter_clauses": [{**CATEGORY, "value": "차트"}]})
            self.assertEqual([c for c in plan["filter_clauses"] if c["field"] == "category"],
                             [CATEGORY] if question == BASE else [])

    def test_validation_detects_missing_and_inherited_category(self):
        constraints = extract_query_constraints(BASE)
        plan = build_query_plan(BASE, {"source": "mantis"}, constraints)
        plan["resolved_constraints"] = {**constraints, "inherited_from_audit_id": "a" * 32}
        self.assertTrue(validate_query_plan("HTS로 바꿔줘", plan)["valid"])
        plan["filter_clauses"] = [c for c in plan["filter_clauses"] if c["field"] != "category"]
        self.assertFalse(validate_query_plan(BASE, plan)["valid"])
        self.assertFalse(validate_query_plan("HTS로 바꿔줘", plan)["valid"])

    def test_server_template_generates_exact_category_all_list(self):
        template = next(t for t in load_team_templates()["templates"] if t["id"] == "server-condition-search")
        question = fill_template_question(template["questionTemplate"], {"period": "2026년 10월", "platform": "MTS", "customer": "", "status": "전체"})
        constraints = extract_query_constraints(question)
        self.assertIn(CATEGORY, constraints["filter_clauses"])
        self.assertEqual(build_keyword_terms(question, constraints), [])
