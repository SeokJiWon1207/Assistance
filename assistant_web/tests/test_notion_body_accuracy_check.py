"""The oracle must catch false positives without borrowing production matchers."""
import unittest
from unittest.mock import Mock, patch
from scripts.accuracy import notion_body_accuracy_check as accuracy


def raw_page(page_id, title, kind, text, complete=True, property_text=""):
    block_id = page_id + "-block"
    rich = [{"type": "text", "plain_text": text, "text": {"content": text}}]
    data = {"cells": [rich]} if kind == "table_row" else {"rich_text": rich}
    return {"page_id": page_id, "title": title, "database_id": "db-1", "database_title": "자료",
            "properties": {"태그": {"type": "rich_text", "rich_text": [{"plain_text": property_text}]}},
            "complete": complete, "url": "https://notion.so/" + page_id,
            "segments": [{"block_id": block_id, "type": kind, "text": text, "path": [page_id]}],
            "blocks": [{"id": block_id, "type": kind, kind: data}]}


def evidence(*pages):
    return {"schema_version": 1, "scope": {"complete": False}, "pages": list(pages), "errors": []}


def result(*pages, complete=True):
    return {"stages": [{"source": "notion", "status": "ok", "notion_pages": list(pages),
                        "meta": {"search_complete": complete, "body_scan_attempted_count": 12,
                                 "body_scan_limit": 20}}]}


class NotionBodyOracleTests(unittest.TestCase):
    def test_oracle_separates_api_leaf_and_child_database_from_supported_content(self):
        blocks = [
            {"id": "button", "type": "unsupported", "has_children": False,
             "unsupported": {"block_type": "button"}},
            {"id": "database", "type": "child_database", "has_children": True},
            raw_page("own", "문서", "paragraph", "owned text")["blocks"][0],
        ]
        with patch("requests.get", side_effect=[Mock(json=lambda: {"properties": {}}),
                   Mock(json=lambda: {"results": blocks})]), patch("backend.config.notion_request_slot"), \
                patch("backend.config.get_notion_headers", return_value={}):
            source = accuracy.fetch_oracle_page("page")
        self.assertFalse(source["complete"])
        self.assertTrue(source["supported_content_complete"])
        self.assertEqual(source["incomplete_reasons"], ["child_database_separate_scope", "unsupported_api_leaf"])
        self.assertEqual(source["child_database_ids"], ["database"])
        self.assertEqual(source["unsupported_elements"], [{"block_id": "button", "block_type": "button"}])

    def test_oracle_keeps_unread_content_and_malformed_pagination_incomplete(self):
        examples = [
            ({"results": [{"id": "unknown", "type": "unsupported", "has_children": False}]}, "unsupported_content_unknown"),
            ({"results": [{"id": "button", "type": "unsupported", "has_children": True,
                           "unsupported": {"block_type": "button"}}]}, "unsupported_content_unknown"),
            ({"results": [{"id": "child", "type": "child_page"}]}, "child_page_not_followed"),
            ({"results": [], "has_more": True}, "invalid_pagination"),
            ({"results": None}, "invalid_response"),
        ]
        for payload, reason in examples:
            with self.subTest(reason=reason), patch("requests.get", side_effect=[
                    Mock(json=lambda: {"properties": {}}), Mock(json=lambda value=payload: value)]), \
                    patch("backend.config.notion_request_slot"), patch("backend.config.get_notion_headers", return_value={}):
                source = accuracy.fetch_oracle_page("page")
                self.assertFalse(source["complete"])
                self.assertFalse(source["supported_content_complete"])
                self.assertIn(reason, source["incomplete_reasons"])

    def test_text_evidence_distinguishes_boundary_formatting_from_content_loss(self):
        cases = [
            ("paragraph", "release marker", "release marker", "exact"),
            ("paragraph", "release marker  ", "release marker", "boundary_whitespace_only"),
            ("code", "\nframe_one()\n  frame_two() \n\n", "frame_one()\n  frame_two()",
             "boundary_whitespace_only"),
            ("code", "\n    indented()\n", "indented()", "mismatch"),
            ("code", "a()\n    b()", "a()\nb()", "mismatch"),
            ("code", "foo bar", "foobar", "mismatch"),
            ("paragraph", "release marker", "release", "mismatch"),
            ("paragraph", "marker", "", "mismatch"),
        ]
        for kind, raw, claim, expected in cases:
            with self.subTest(kind=kind, raw=raw, claim=claim):
                block = raw_page("one", "문서", kind, raw)["blocks"][0]
                self.assertEqual(accuracy.classify_block_text_evidence(block, claim), expected)
                self.assertEqual(accuracy.raw_block_text(block), raw)

    def test_table_evidence_preserves_cell_order_boundaries_and_internal_spaces(self):
        block = {"type": "table_row", "table_row": {"cells": [
            [{"plain_text": " alpha beta  "}], [{"plain_text": "\ngamma\n"}], []]}}
        self.assertEqual(accuracy.classify_block_text_evidence(block, "alpha beta | gamma | "),
                         "boundary_whitespace_only")
        for claim in ("alphabeta | gamma | ", "gamma | alpha beta | ", "alpha beta | gamma",
                      "alpha beta gamma |  | "):
            with self.subTest(claim=claim):
                self.assertEqual(accuracy.classify_block_text_evidence(block, claim), "mismatch")

    def test_korean_spacing_equivalence_is_independent_positive_evidence(self):
        target = raw_page("one", "문서", "paragraph", "주간이슈")
        spaced = raw_page("two", "다른 문서", "paragraph", "주간 이슈 관리", complete=False)
        case = accuracy.BodyCase("one", "노션 본문 주간이슈", ["주간이슈"], ["one"], "paragraph")
        graded = accuracy.grade_retrieval(case, result(
            {"id": "one", "content": "주간이슈"}, {"id": "two", "content": "주간 이슈 관리"}),
            evidence(target, spaced))
        self.assertTrue(graded["passed"])
        self.assertEqual(graded["korean_spacing_equivalent_page_ids"], ["two"])

    def test_spacing_oracle_does_not_join_code_identifiers_or_separate_blocks(self):
        target = raw_page("one", "문서", "code", "foobar")
        wrong = raw_page("two", "다른 문서", "code", "foo bar")
        case = accuracy.BodyCase("one", "노션 코드 foobar", ["foobar"], ["one"], "code")
        graded = accuracy.grade_retrieval(case, result(
            {"id": "one", "content": "foobar"}, {"id": "two", "content": "foo bar"}), evidence(target, wrong))
        self.assertEqual(graded["false_positive_page_ids"], ["two"])
        split = raw_page("two", "다른 문서", "paragraph", "주간")
        tail = raw_page("tail", "문서", "paragraph", "이슈")
        split["blocks"].extend(tail["blocks"])
        split["segments"].extend(tail["segments"])
        target = raw_page("one", "문서", "paragraph", "주간이슈")
        case = accuracy.BodyCase("one", "노션 주간이슈", ["주간이슈"], ["one"], "paragraph")
        graded = accuracy.grade_retrieval(case, result(
            {"id": "one", "content": "주간이슈"}, {"id": "two", "content": "주간\n이슈"}), evidence(target, split))
        self.assertEqual(graded["false_positive_page_ids"], ["two"])

    def test_plain_probe_does_not_select_an_inflected_korean_verb(self):
        source = raw_page("one", "문서", "paragraph", "검색하는 로봇")
        cases = accuracy.build_cases(evidence(source), per_kind=1)
        plain = next(case for case in cases if case.category == "plain_keyword")
        self.assertEqual(plain.terms, ["로봇"])

    def test_plain_probe_avoids_observed_conjunction_and_locative_particles(self):
        for text in ("운영소스와", "단기간에"):
            with self.subTest(text=text):
                self.assertFalse(accuracy.suitable_plain_probe_token(text))
                page = raw_page("one", "문서", "paragraph", text + " zxyq")
                cases = accuracy.build_cases(evidence(page), per_kind=1)
                plain = next(case for case in cases if case.category == "plain_keyword")
                self.assertEqual(plain.terms, ["zxyq"])
        for noun in ("사과", "결과", "과제"):
            self.assertTrue(accuracy.suitable_plain_probe_token(noun))

    def test_plain_probe_does_not_select_a_korean_word_with_attached_particle(self):
        source = raw_page("one", "문서", "code", "인터넷테스트에서 zxyq")
        cases = accuracy.build_cases(evidence(source), per_kind=1)
        plain = next(case for case in cases if case.category == "plain_keyword")
        self.assertEqual(plain.terms, ["zxyq"])

    def test_oracle_resolves_only_synced_original_collected_in_same_page(self):
        original_parent = "page"

        def get(url, **kwargs):
            if "/pages/" in url:
                data = {"id": "page", "object": "page", "properties": {}}
            elif "/blocks/page/" in url:
                data = {"results": [
                    {"id": "copy", "type": "synced_block", "has_children": True,
                     "synced_block": {"synced_from": {"block_id": "original"}}},
                    {"id": "original", "type": "synced_block", "has_children": True,
                     "parent": {"type": "page_id", "page_id": original_parent},
                     "synced_block": {"synced_from": None}},
                ]}
            else:
                self.assertIn("/blocks/original/children", url)
                data = {"results": [{"id": "text", "type": "paragraph",
                        "paragraph": {"rich_text": [{"plain_text": "same_page_evidence"}]}}]}
            response = Mock()
            response.json.return_value = data
            return response

        with patch("requests.get", side_effect=get), patch("backend.config.notion_request_slot"), \
                patch("backend.config.get_notion_headers", return_value={}):
            source = accuracy.fetch_oracle_page("page")
            self.assertTrue(source["complete"])
            self.assertEqual([s["text"] for s in accuracy.verified_segments(source)], ["same_page_evidence"])
            original_parent = "other"
            source = accuracy.fetch_oracle_page("page")
            self.assertFalse(source["complete"])
            self.assertEqual(accuracy.verified_segments(source), [])

    def test_independent_oracle_reads_deep_body_beyond_small_request_window(self):
        def get(url, **kwargs):
            if "/pages/" in url:
                data = {"id": "page", "object": "page", "properties": {}}
            else:
                container = url.split("/blocks/")[1].split("/")[0]
                depth = 0 if container == "page" else int(container)
                kind = "toggle" if depth < 40 else "paragraph"
                data = {"has_more": False, "results": [{"id": str(depth + 1), "type": kind,
                        "has_children": depth < 40,
                        kind: {"rich_text": [{"plain_text": "deep_oracle_evidence" if depth == 40 else ""}]}}]}
            response = Mock()
            response.json.return_value = data
            return response
        with patch("requests.get", side_effect=get), patch("backend.config.notion_request_slot"), \
                patch("backend.config.get_notion_headers", return_value={}):
            source = accuracy.fetch_oracle_page("page")
        self.assertTrue(source["complete"])
        self.assertEqual(source["errors"], [])
        self.assertEqual([s["text"] for s in accuracy.verified_segments(source)], ["deep_oracle_evidence"])

    def test_plain_keyword_probe_has_no_title_or_body_scope_and_accepts_other_body_types(self):
        code = raw_page("one", "문서", "code", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(code), 1) if c.category == "plain_keyword")
        self.assertEqual(case.question, "노션에서 needle_token 찾아줘")
        self.assertEqual(case.exact_title, "")
        table = raw_page("two", "다른 문서", "table_row", "needle_token")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"},
            {"id": "two", "content": "needle_token"}), evidence(code, table))
        self.assertTrue(graded["passed"])
        self.assertFalse(graded["full_recall_verified"])

    def test_plain_keyword_precision_allows_other_page_title_match(self):
        code = raw_page("one", "문서", "code", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(code), 1) if c.category == "plain_keyword")
        title_match = raw_page("two", "needle_token", "paragraph", "관련 안내")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"},
            {"id": "two", "content": "관련 안내"}), evidence(code, title_match))
        self.assertTrue(graded["passed"])
        self.assertFalse(graded["false_positive_page_ids"])

    def test_cases_require_body_only_terms_and_raw_block_provenance(self):
        page = raw_page("one", "공통", "code", "function hidden_token", property_text="function")
        cases = accuracy.build_cases(evidence(page), per_kind=1)
        global_case = next(case for case in cases if case.category == "global_keyword")
        self.assertEqual(global_case.terms, ["hidden_token"])
        self.assertEqual(global_case.required_page_ids, ["one"])
        self.assertEqual(global_case.required_kind, "code")
        self.assertEqual(global_case.source_blocks[0]["block_id"], "one-block")
        page["blocks"] = []
        self.assertEqual(accuracy.build_cases(evidence(page)), [])

    def test_case_prefers_distinctive_later_block_over_common_first_word(self):
        page = raw_page("one", "문서", "paragraph", "common")
        later = raw_page("later", "문서", "paragraph", "specific_identifier")
        page["segments"].extend(later["segments"])
        page["blocks"].extend(later["blocks"])
        other = raw_page("two", "다른 문서", "paragraph", "common")
        case = next(c for c in accuracy.build_cases(evidence(page, other), 1) if c.category == "global_keyword")
        self.assertEqual(case.terms, ["specific_identifier"])
        self.assertEqual(case.source_blocks[0]["block_id"], "later-block")

    def test_model_evidence_check_only_accepts_matching_page_content(self):
        case = accuracy.BodyCase("one", "노션 needle_token", ["needle_token"], ["one"], "code")
        self.assertTrue(accuracy.model_evidence_check(case, result({"id": "one", "content": "needle_token"})))
        self.assertFalse(accuracy.model_evidence_check(case, result(
            {"id": "one", "content": "unrelated"}, {"id": "two", "content": "needle_token"})))

    def test_cases_cover_three_distinct_pages_per_kind_before_reusing_a_page(self):
        pages = [raw_page(f"{kind}-{i}", f"제목{i}", kind, f"unique_{kind}_{i}")
                 for kind in ("paragraph", "table_row", "code") for i in range(4)]
        cases = accuracy.build_cases(evidence(*pages))
        positives = [case for case in cases if case.category == "global_keyword"]
        self.assertEqual(len(positives), 9)
        self.assertEqual({case.required_kind for case in positives}, {"paragraph", "table_row", "code"})
        self.assertEqual(len({tuple(case.required_page_ids) for case in positives}), 9)

    def test_known_target_hit_never_claims_global_recall_from_sample(self):
        page = raw_page("one", "문서", "code", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(page), 1) if c.category == "global_keyword")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"}), evidence(page))
        self.assertTrue(graded["known_target_recalled"])
        self.assertTrue(graded["passed"])
        self.assertFalse(graded["full_recall_verified"])
        self.assertEqual(graded["body_scan_attempted_count"], 12)

    def test_returned_page_without_raw_body_match_is_false_positive(self):
        wanted = raw_page("one", "문서", "code", "needle_token")
        wrong = raw_page("two", "다른 문서", "paragraph", "unrelated_text")
        case = next(c for c in accuracy.build_cases(evidence(wanted), 1) if c.category == "global_keyword")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"},
            {"id": "two", "content": "needle_token"}), evidence(wanted, wrong))
        self.assertEqual(graded["false_positive_page_ids"], ["two"])
        self.assertFalse(graded["passed"])

    def test_source_id_hit_with_missing_returned_body_content_does_not_pass(self):
        page = raw_page("one", "문서", "table_row", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(page), 1) if c.category == "global_keyword")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": ""}), evidence(page))
        self.assertTrue(graded["known_target_recalled"])
        self.assertFalse(graded["returned_matching_content"])
        self.assertFalse(graded["passed"])

    def test_unverified_source_does_not_get_precision_pass(self):
        page = raw_page("one", "문서", "code", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(page), 1) if c.category == "global_keyword")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"},
            {"id": "unknown", "content": "needle_token"}), evidence(page))
        self.assertEqual(graded["unverified_page_ids"], ["unknown"])
        self.assertEqual(graded["outcome"], "incomplete")
        self.assertFalse(graded["passed"])

    def test_negative_scope_checks_only_known_page_not_duplicate_title_global_absence(self):
        page = raw_page("one", "중복 제목", "code", "needle_token")
        cases = accuracy.build_cases(evidence(page), 1)
        negative = next(c for c in cases if c.category == "exact_title_negative")
        other = raw_page("two", "중복 제목", "code", negative.terms[0])
        graded = accuracy.grade_retrieval(negative, result({"id": "two", "content": negative.terms[0]}),
                                          evidence(page, other))
        self.assertTrue(graded["passed"])
        self.assertEqual(graded["oracle_scope"], "known_page_exclusion")
        self.assertFalse(graded["full_recall_verified"])
        page["complete"] = False
        self.assertFalse(any(c.category == "exact_title_negative" for c in accuracy.build_cases(evidence(page))))

    def test_incomplete_empty_negative_is_not_success(self):
        page = raw_page("one", "문서", "code", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(page), 1) if c.category == "exact_title_negative")
        graded = accuracy.grade_retrieval(case, result(complete=False), evidence(page))
        self.assertFalse(graded["passed"])
        self.assertEqual(graded["outcome"], "incomplete")

    def test_negative_without_complete_independent_page_remains_incomplete(self):
        page = raw_page("one", "문서", "code", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(page), 1) if c.category == "exact_title_negative")
        page["complete"] = False
        graded = accuracy.grade_retrieval(case, result(complete=True), evidence(page))
        self.assertFalse(graded["passed"])
        self.assertEqual(graded["outcome"], "incomplete")

    def test_quoted_table_query_rejects_a_paragraph_only_match(self):
        table = raw_page("one", "문서", "table_row", "needle_token")
        prose = raw_page("two", "다른문서", "paragraph", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(table), 1) if c.category == "quoted_body_keyword")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"},
            {"id": "two", "content": "needle_token"}), evidence(table, prose))
        self.assertEqual(graded["false_positive_page_ids"], ["two"])
        self.assertFalse(graded["passed"])

    def test_body_scope_precision_accepts_matching_code_and_table_blocks(self):
        paragraph = raw_page("one", "문서", "paragraph", "needle_token")
        cases = accuracy.build_cases(evidence(paragraph), 1)
        for category in ("quoted_body_keyword", "exact_title_and_body"):
            case = next(c for c in cases if c.category == category)
            for kind in ("code", "table_row"):
                with self.subTest(category=category, kind=kind):
                    other = raw_page("two", "문서", kind, "needle_token")
                    graded = accuracy.grade_retrieval(case, result(
                        {"id": "one", "content": "needle_token"},
                        {"id": "two", "content": "needle_token"}), evidence(paragraph, other))
                    self.assertEqual(graded["false_positive_page_ids"], [])
                    self.assertTrue(graded["required_kind_verified"])
                    self.assertTrue(graded["passed"])

    def test_body_scope_keeps_the_known_target_block_type_requirement(self):
        paragraph = raw_page("one", "문서", "paragraph", "needle_token")
        case = next(c for c in accuracy.build_cases(evidence(paragraph), 1)
                    if c.category == "quoted_body_keyword")
        code = raw_page("one", "문서", "code", "needle_token")
        graded = accuracy.grade_retrieval(case, result({"id": "one", "content": "needle_token"}), evidence(code))
        self.assertFalse(graded["required_kind_verified"])
        self.assertFalse(graded["passed"])
        self.assertIn("target_block_evidence_missing", graded["failed_checks"])

    def test_explicit_case_spec_uses_verified_selected_token_and_source_block(self):
        page = raw_page("one", "문서", "code", "other_long_identifier needle_token")
        spec = {"cases": [{"case_id": "CODE-1", "query_token": "needle_token", "page_id": "one",
                            "block_type": "code", "expected_match_blocks": [{"block_id": "one-block"}]}]}
        cases = accuracy.build_cases_from_spec(evidence(page), spec)
        case = next(c for c in cases if c.category == "global_keyword")
        self.assertEqual(case.id, "CODE-1-global")
        self.assertEqual(case.terms, ["needle_token"])
        self.assertEqual(case.source_blocks[0]["block_id"], "one-block")
        spec["cases"][0]["query_token"] = "fabricated_token"
        with self.assertRaises(ValueError):
            accuracy.build_cases_from_spec(evidence(page), spec)

    def test_live_runner_uses_request_budget_and_restores_it_after_timeout(self):
        from backend.execution import CURRENT_BUDGET, RequestStopped
        case = accuracy.BodyCase("one", "노션 needle_token", ["needle_token"], ["one"], "code")
        observed = []

        def timed_out(*args, **kwargs):
            observed.append(CURRENT_BUDGET.get())
            raise RequestStopped()

        with patch("backend.retrieval.run_retrieval_pipeline", side_effect=timed_out):
            graded = accuracy.run_case(case, evidence())
        self.assertIsNotNone(observed[0])
        self.assertIsNone(CURRENT_BUDGET.get())
        self.assertEqual(graded["outcome"], "incomplete")
        self.assertEqual(graded["error_type"], "RequestStopped")

    def test_live_runner_refetches_partial_oracle_before_declaring_unknown_match(self):
        case = accuracy.BodyCase("one", "노션 needle_token", ["needle_token"], ["one"], "code")
        old = raw_page("one", "문서", "paragraph", "first portion", complete=False)
        fresh = raw_page("one", "문서", "code", "needle_token")
        sources = evidence(old)
        with patch("backend.retrieval.run_retrieval_pipeline", return_value=result({"id": "one", "content": "needle_token"})), \
                patch.object(accuracy, "fetch_oracle_page", return_value=fresh):
            graded = accuracy.run_case(case, sources)
        self.assertTrue(graded["passed"])
        self.assertEqual(len(sources["pages"]), 1)
        self.assertEqual(accuracy.verified_segments(sources["pages"][0])[0]["type"], "code")

    def test_partial_oracle_retry_without_evidence_stays_unverified(self):
        case = accuracy.BodyCase("one", "노션 needle_token", ["needle_token"], ["one"], "code")
        partial = raw_page("one", "문서", "paragraph", "first portion", complete=False)
        with patch("backend.retrieval.run_retrieval_pipeline", return_value=result({"id": "one", "content": "needle_token"})), \
                patch.object(accuracy, "fetch_oracle_page", return_value=partial):
            graded = accuracy.run_case(case, evidence(partial))
        self.assertFalse(graded["passed"])
        self.assertEqual(graded["unverified_page_ids"], ["one"])

    def test_misreported_segment_type_cannot_create_a_table_case(self):
        page = raw_page("one", "문서", "paragraph", "needle_token")
        page["segments"][0]["type"] = "table_row"
        self.assertEqual(accuracy.build_cases(evidence(page)), [])

    def test_reports_keep_natural_planned_and_chat_denominators_separate(self):
        rows = [{"mode": mode, "passed": passed, "outcome": "passed" if passed else "failed"}
                for mode, passed in [("natural_retrieval", False), ("planned_control", True), ("natural_chat", False)]]
        totals = accuracy.summarize_results(rows)
        self.assertEqual(totals["natural_retrieval"]["passed"], 0)
        self.assertEqual(totals["planned_control"]["passed"], 1)
        self.assertEqual(totals["natural_chat"]["total"], 1)

    def chat_grade(self, answer, *, question="노션 코드에 needle_token 포함한 문서 찾아줘",
                   model_evidence=True):
        page = raw_page("one", "문서", "code", "needle_token")
        case = accuracy.BodyCase("one", question, ["needle_token"], ["one"], "code")
        content = "needle_token" if model_evidence else "일반 안내\n" * 100 + "needle_token"
        retrieval = result({"id": "one", "content": content})
        response = {"content": answer, "sources": [{"type": "notion", "page_id": "one"}],
                    "retrieval": retrieval}
        # ChatService can contact model/Notion servers; keep the real independent
        # oracle, model-context builder, and runner grading with a canned response.
        with patch("backend.services.chat_service.chat_service.build_response", return_value=response):
            return accuracy.run_case(case, evidence(page), "natural_chat", "test-model")

    def test_source_attachment_cannot_pass_an_answer_denying_the_known_match(self):
        for answer in (
            "해당 조건에 맞는 문서는 없습니다.", "검색 결과가 없습니다.",
            "🔍 Notion DB에서는 조건에 맞는 항목을 찾지 못했어요. 검색어를 확인해 주세요.",
            "Notion에서 조회한 범위에는 일치하는 항목이 없습니다. 전체 조회가 끝나지 않았습니다.",
        ):
            with self.subTest(answer=answer):
                graded = self.chat_grade(answer)
                self.assertTrue(graded["retrieval_passed"])
                self.assertFalse(graded["passed"])
                self.assertTrue(graded["chat_source_wiring_passed"])
                self.assertEqual(graded["chat_answer_outcome"], "failed")
                self.assertIn("chat_answer_denies_known_match", graded["failed_checks"])

    def test_source_attachment_cannot_pass_an_empty_answer(self):
        graded = self.chat_grade(" ")
        self.assertFalse(graded["passed"])
        self.assertIn("chat_answer_empty", graded["failed_checks"])

    def test_body_answer_without_model_evidence_remains_unverified(self):
        graded = self.chat_grade("needle_token의 동작은 ...", question="노션 needle_token 코드를 설명해줘",
                                 model_evidence=False)
        self.assertTrue(graded["retrieval_passed"])
        self.assertFalse(graded["passed"])
        self.assertEqual(graded["outcome"], "answer_unverified")
        self.assertIn("chat_model_evidence_missing", graded["failed_checks"])

    def test_body_answer_without_requested_keyword_remains_unverified(self):
        graded = self.chat_grade("전혀 다른 코드에 관한 설명입니다.", question="노션 needle_token 코드를 설명해줘")
        self.assertFalse(graded["passed"])
        self.assertEqual(graded.get("chat_answer_outcome"), "answer_unverified")
        self.assertIn("chat_answer_keyword_missing", graded["failed_checks"])

    def test_noncontradictory_answer_only_passes_wiring_and_limited_checks(self):
        graded = self.chat_grade("문서: needle_token 코드를 확인했습니다.", question="노션 needle_token 코드를 설명해줘")
        self.assertTrue(graded["passed"])
        self.assertEqual(graded["chat_answer_outcome"], "answer_unverified")
        self.assertFalse(graded["answer_accuracy_verified"])
        self.assertEqual(graded["measurement"], "source_wiring_and_limited_answer_checks")

    def test_chat_summary_never_reports_heuristics_as_answer_accuracy(self):
        graded = self.chat_grade("문서를 찾았습니다.")
        totals = accuracy.summarize_results([{**graded, "mode": "natural_chat"}])
        self.assertEqual(totals["natural_chat"].get("source_wiring_passed"), 1)
        self.assertFalse(totals["natural_chat"]["answer_accuracy_verified"])


if __name__ == "__main__":
    unittest.main()
