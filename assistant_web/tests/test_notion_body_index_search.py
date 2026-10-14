"""본문 색인 후보가 실제 검색 필터·출처·집계 계약을 지키는지 검증한다."""

import os
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

import test_notion_accuracy_regressions as fixtures

from backend.notion import body_index_search, search


def indexed_candidate(identifier, text="needle", *, kind="paragraph", owner=None,
                      complete=True, title="운영 문서", memo="", status="", edited=None):
    raw_page = fixtures.page(identifier, memo=memo, status=status)
    raw_page["properties"]["제목"]["title"] = [{"plain_text": title}]
    if edited:
        raw_page["last_edited_time"] = edited
    return {
        "raw_page": raw_page,
        "database_id": "db",
        "content": text,
        "segments": [{
            "block_id": f"{identifier}-block", "type": kind, "text": text,
            "page_id": identifier, "owner_page_id": owner or identifier,
        }] if text else [],
        "complete": complete,
    }


def index_result(*candidates, complete=True, validation_complete=True, reasons=()):
    return {
        "candidates": list(candidates),
        "coverage": {"complete": complete, "indexed_pages": len(candidates),
                     "total_pages": len(candidates) if complete else len(candidates) + 100},
        "candidate_count": len(candidates),
        "validation_complete": validation_complete,
        "incomplete_reasons": list(reasons),
    }


class NotionBodyIndexSearchTests(unittest.TestCase):
    search_fixture = fixtures.NotionAccuracyRegressionTests.search_fixture
    paragraph = staticmethod(fixtures.NotionAccuracyRegressionTests.paragraph)

    def setUp(self):
        fixtures.NotionAccuracyRegressionTests.setUp(self)

    def test_query_database_filter_preserves_allowed_synced_source_scope(self):
        index = Mock()
        index.search.return_value = [{"page_id": "copy-page", "database_id": "db", "segments": []}]
        index.status.return_value = {}
        candidate = indexed_candidate("copy-page")
        with patch.object(body_index_search, "open_body_index", return_value=index), patch.object(
            body_index_search, "validate_index_candidate", return_value=candidate,
        ) as validate:
            result = body_index_search.find_indexed_body_candidates(
                ["db"], ["needle"], allowed_database_ids=["db", "source-db"],
            )
        index.search.assert_called_once_with(["db"], ["needle"])
        self.assertEqual(validate.call_args.args[1], ["db", "source-db"])
        self.assertEqual(result["candidates"], [candidate])

    def test_search_passes_read_scope_before_role_narrowing_to_body_validation(self):
        lookup = self.install_index(indexed_candidate("copy-page"))
        with patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db", "source-db": "source-db"}, True)), patch.object(
            search, "select_notion_database_ids", return_value=(["db"], "issue"),
        ):
            result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘', include_blocks=False)
        self.assertEqual(lookup.call_args.args[0], ["db"])
        self.assertEqual(lookup.call_args.kwargs["allowed_database_ids"], ["db", "source-db"])
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["copy-page"])

    def install_index(self, *candidates, rows=None, blocks=None, **metadata):
        self.search_fixture(rows or [], blocks or {})
        mocked = patch.object(search, "find_indexed_body_candidates",
                              return_value=index_result(*candidates, **metadata), create=True)
        lookup = mocked.start()
        self.addCleanup(mocked.stop)
        return lookup

    def test_exact_title_body_target_precedes_recent_unrelated_index_candidates(self):
        self.search_fixture([], {})
        recent = [indexed_candidate(f"recent-{i}", title="다른 문서", edited="2026-10-01T00:00:00Z")
                  for i in range(30)]
        target = indexed_candidate("old-target", title="주간보고", edited="2025-01-01T00:00:00Z")
        index = Mock()
        index.search.return_value = [{**r, "page_id": r["raw_page"]["id"]} for r in [*recent, target]]
        index.status.return_value = {"complete": True}
        with patch.object(body_index_search, "open_body_index", return_value=index), \
                patch.object(body_index_search, "validate_index_candidate", side_effect=lambda row, *_a, **_kw: row):
            result = search.execute_notion_search('노션 "주간보고" 문서에서 본문에 "needle" 포함된 문서 찾아줘',
                                                  include_blocks=False)
        self.assertEqual([p["id"] for p in result["notion_pages"]], ["old-target"])
        self.assertFalse(result["meta"]["body_index"]["validation_complete"])

    def test_exact_title_priority_does_not_exclude_a_renamed_page_from_live_validation(self):
        self.search_fixture([], {})
        old = indexed_candidate("renamed", title="옛 제목")
        index = Mock()
        index.search.return_value = [{**old, "page_id": "renamed"}]
        index.status.return_value = {"complete": True}
        with patch.object(body_index_search, "open_body_index", return_value=index), \
                patch.object(body_index_search, "validate_index_candidate",
                             return_value=indexed_candidate("renamed", title="주간보고")):
            result = search.execute_notion_search('노션 "주간보고" 문서에서 본문에 "needle" 포함된 문서 찾아줘',
                                                  include_blocks=False)
        self.assertEqual([p["id"] for p in result["notion_pages"]], ["renamed"])

    def test_body_table_and_code_find_old_page_outside_latest_scan_window(self):
        rows = [fixtures.page(f"recent-{i}") for i in range(31)]
        for scope, kind in (("본문", "paragraph"), ("표", "table_row"), ("코드", "code")):
            with self.subTest(scope=scope):
                self.install_index(indexed_candidate("old-page", kind=kind), rows=rows)
                result = search.execute_notion_search(f'노션 {scope}에 "needle" 포함된 문서 찾아줘',
                                                      include_blocks=False)
                self.assertEqual([page["id"] for page in result["notion_pages"]], ["old-page"])
                self.assertEqual(result["notion_pages"][0]["match_method"], "notion_body_index")
                self.assertEqual(result["notion_pages"][0]["matched_body_blocks"][0]["type"], kind)
                self.assertLessEqual(result["meta"]["body_scan_attempted_count"], 30)

    def test_typed_queries_exclude_property_paragraph_and_foreign_page_matches(self):
        self.install_index(
            indexed_candidate("property-only", "", memo="needle"),
            indexed_candidate("paragraph-only", kind="paragraph"),
            indexed_candidate("child-page", kind="code", owner="different-page"),
            indexed_candidate("table-only", kind="table_row"),
            indexed_candidate("accepted", kind="code"),
        )
        result = search.execute_notion_search('노션 코드에 "needle" 포함된 문서 찾아줘',
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["accepted"])

    def test_body_query_accepts_own_table_and_code_but_not_property_or_foreign_text(self):
        self.install_index(
            indexed_candidate("property-only", "", memo="needle"),
            indexed_candidate("foreign", owner="different-page"),
            indexed_candidate("paragraph", kind="paragraph"),
            indexed_candidate("table", kind="table_row"),
            indexed_candidate("code", kind="code"),
        )
        result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘',
                                              include_blocks=False)
        self.assertEqual({page["id"] for page in result["notion_pages"]}, {"paragraph", "table", "code"})

    def test_verified_synced_projection_preserves_source_in_typed_search(self):
        for scope, kind in (("본문", "paragraph"), ("표", "table_row"), ("코드", "code")):
            with self.subTest(scope=scope):
                candidate = indexed_candidate("copy-page", kind=kind, owner="source-page")
                candidate["segments"][0].update(display_page_id="copy-page", synced_source_id="source",
                                                 synced_copy_id="copy", source_database_id="db")
                self.install_index(candidate, indexed_candidate("unverified", kind=kind, owner="other"))
                result = search.execute_notion_search(f'노션 {scope}에 "needle" 포함된 문서 찾아줘', include_blocks=False)
                self.assertEqual([row["id"] for row in result["notion_pages"]], ["copy-page"])
                evidence = result["notion_pages"][0]["matched_body_blocks"][0]
                self.assertEqual(evidence["owner_page_id"], "source-page")
                self.assertEqual(evidence["synced_source_id"], "source")

    def test_exact_title_does_not_widen_to_other_indexed_pages(self):
        self.install_index(indexed_candidate("wanted", title="운영 규칙"),
                           indexed_candidate("other", title="다른 규칙"))
        result = search.execute_notion_search('노션 "운영 규칙" 문서에서 본문에 needle 포함된 문서 찾아줘',
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["wanted"])

    def test_index_candidates_still_apply_structured_status_filter(self):
        self.install_index(indexed_candidate("wanted", status="완료"),
                           indexed_candidate("other", status="진행중"))
        result = search.execute_notion_search('노션 완료 상태의 본문에 "needle" 포함된 문서 찾아줘',
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["wanted"])

    def test_negative_conjunction_rejects_present_or_unread_excluded_content(self):
        self.install_index(indexed_candidate("wanted", "needle 정상 절차"),
                           indexed_candidate("excluded", "needle deprecated 절차"),
                           indexed_candidate("unread", "needle 정상 절차", complete=False))
        result = search.execute_notion_search(
            '노션 본문에 "needle" 포함하고 "deprecated" 제외한 문서 찾아줘',
            constraints={"filter_clauses": [
                {"field": "keyword", "operator": "contains", "value": "needle"},
                {"field": "keyword", "operator": "not_contains", "value": "deprecated"},
            ]}, include_blocks=False,
        )
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["wanted"])

    def test_index_and_repeated_candidates_do_not_double_count_pages(self):
        candidate = indexed_candidate("same")
        self.install_index(candidate, candidate, rows=[fixtures.page("same")],
                           blocks={"same": [self.paragraph("live", "needle")]})
        result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서는 몇 개야',
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["same"])
        self.assertEqual(result["meta"]["candidate_count"], 1)
        self.assertEqual(result["notion_pages"][0]["match_method"], "notion_body_index")

    def test_count_includes_matches_beyond_display_limit_and_sorts_latest_first(self):
        self.install_index(*(indexed_candidate(f"page-{i}", edited=f"2026-09-{i + 1:02}T00:00:00Z")
                             for i in range(8)))
        result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서는 몇 개야',
                                              limit=3, include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["page-7", "page-6", "page-5"])
        self.assertEqual(result["meta"]["candidate_count"], 8)
        self.assertEqual(result["meta"]["displayed_count"], 3)
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertFalse(result["meta"]["search_complete"])
        self.assertIn("body_index_snapshot", result["meta"]["incomplete_reasons"])

    def test_partial_coverage_never_reports_a_complete_global_count(self):
        self.install_index(indexed_candidate("known"), complete=False, validation_complete=False,
                           reasons=["body_index_validation_limit"])
        result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서는 몇 개야',
                                              include_blocks=False)
        self.assertEqual(result["meta"]["candidate_count"], 1)
        self.assertFalse(result["meta"]["search_complete"])
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertIn("body_index_incomplete", result["meta"]["incomplete_reasons"])
        self.assertIn("body_index_validation_limit", result["meta"]["incomplete_reasons"])

    def test_validated_index_content_is_reused_without_truncated_refetch(self):
        self.install_index(indexed_candidate("late-code", "needle();", kind="code"))
        with patch.object(search, "fetch_notion_page_blocks", side_effect=AssertionError("unexpected refetch")):
            result = search.execute_notion_search('노션 코드에 "needle" 포함된 문서 찾아줘')
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["late-code"])
        self.assertIn("needle();", result["notion_pages"][0]["content"])

    def test_partial_index_evidence_is_not_replaced_by_legacy_cached_body(self):
        self.install_index(indexed_candidate("large-code", "needle(current_value);", kind="code", complete=False),
                           validation_complete=False, reasons=["body_index_content_incomplete"])
        with patch.object(search, "fetch_notion_page_blocks", side_effect=AssertionError("unexpected cached refetch")):
            result = search.execute_notion_search('노션 코드에 "needle" 포함된 문서 찾아줘')
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["large-code"])
        self.assertIn("needle(current_value);", result["notion_pages"][0]["content"])
        self.assertTrue(result["notion_pages"][0]["content_incomplete"])
        self.assertFalse(result["meta"]["content_complete"])

    def test_index_schema_failure_falls_back_without_aborting_live_search(self):
        self.install_index(indexed_candidate("indexed"), rows=[fixtures.page("fresh")],
                           blocks={"fresh": [self.paragraph("current", "needle 정상 절차")]})
        with patch.object(search, "fetch_notion_database_title", side_effect=[
            HTTPException(status_code=503, detail="database unavailable"), "문서 DB",
        ]):
            result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘',
                                                  include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["fresh"])
        self.assertFalse(result["meta"]["search_complete"])

    def test_one_index_database_schema_failure_keeps_other_database_match(self):
        other = indexed_candidate("wanted")
        other["database_id"] = "db2"
        other["raw_page"]["parent"]["database_id"] = "db2"
        self.install_index(indexed_candidate("unavailable"), other)

        def database_title(database_id):
            if database_id == "db":
                raise HTTPException(status_code=503, detail="database unavailable")
            return "다른 DB"

        with (
            patch.object(search, "get_notion_settings", return_value={
                "token": "test-token", "database_ids": ["db", "db2"],
            }),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db", "db2": "db2"}, True)),
            patch.object(search, "fetch_notion_database_title", side_effect=database_title),
        ):
            result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘',
                                                  include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["wanted"])
        self.assertIn("body_index_metadata_error", result["meta"]["incomplete_reasons"])

    def test_negative_only_query_keeps_live_body_scan(self):
        lookup = self.install_index(indexed_candidate("unrelated", "정상 절차"),
                                    rows=[fixtures.page("wanted"), fixtures.page("excluded")], blocks={
                                        "wanted": [self.paragraph("ok", "정상 절차")],
                                        "excluded": [self.paragraph("bad", "deprecated 절차")],
                                    })
        result = search.execute_notion_search('노션 본문에 "deprecated" 없는 문서는 몇 개야',
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["wanted"])
        lookup.assert_not_called()

    def test_disabled_body_fetch_does_not_expose_indexed_content(self):
        lookup = self.install_index(indexed_candidate("must-not-leak"))
        with patch.dict(os.environ, {"NOTION_FETCH_PAGE_BLOCKS": "false"}):
            result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘',
                                                  include_blocks=False)
        self.assertEqual(result["notion_pages"], [])
        lookup.assert_not_called()

    def test_no_valid_index_matches_falls_back_to_current_live_page(self):
        self.install_index(indexed_candidate("wrong-type", kind="paragraph"),
                           rows=[fixtures.page("fresh-code")], blocks={
                               "fresh-code": [{"id": "fresh", "type": "code", "has_children": False,
                                               "code": {"rich_text": [{"plain_text": "needle();"}]}}],
                           })
        result = search.execute_notion_search('노션 코드에 "needle" 포함된 문서 찾아줘',
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["fresh-code"])

    def test_empty_index_does_not_invalidate_complete_live_negative_check(self):
        self.install_index(rows=[fixtures.page("read-page")], blocks={
            "read-page": [self.paragraph("plain", "정상 절차")],
        }, complete=False)
        result = search.execute_notion_search('노션 본문에 "absent-token" 포함된 문서는 몇 개야',
                                              include_blocks=False)
        self.assertEqual(result["notion_pages"], [])
        self.assertTrue(result["meta"]["search_complete"])
        self.assertFalse(result["meta"]["body_index_used"])

    def test_partial_index_hits_do_not_hide_new_live_results_even_at_display_limit(self):
        fresh = fixtures.page("fresh")
        fresh["last_edited_time"] = "2026-10-12T00:00:00Z"
        self.install_index(indexed_candidate("old"), rows=[fresh], blocks={
            "fresh": [self.paragraph("fresh-block", "needle 신규 문서")],
        }, complete=False)
        result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘',
                                              limit=1, include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["fresh"])
        self.assertEqual(result["meta"]["candidate_count"], 2)
        self.assertFalse(result["meta"]["search_complete"])
        self.assertEqual(result["meta"]["databases"][0]["found_count"], 2)

    def test_live_catalog_stops_after_collecting_the_body_candidate_window(self):
        self.install_index(indexed_candidate("old"))
        rows = [fixtures.page(f"recent-{i}") for i in range(100)]
        with patch.object(search, "notion_post_json", side_effect=lambda _url, payload, **_kw: fixtures.response({
            "results": rows, "has_more": True,
            "next_cursor": str(int(payload.get("start_cursor", "0")) + 1),
        })) as query:
            result = search.execute_notion_search('노션 본문에 "needle" 포함된 문서 찾아줘', include_blocks=False)
        self.assertEqual(query.call_count, 1)
        self.assertFalse(result["meta"]["search_complete"])
        self.assertIn("body_candidate_window", result["meta"]["incomplete_reasons"])

    def test_plain_keyword_query_also_discovers_indexed_body_table_and_code(self):
        self.install_index(*(indexed_candidate(kind, kind=kind) for kind in ("paragraph", "table_row", "code")))
        result = search.execute_notion_search('노션 needle 찾아줘', planned_terms=["needle"], include_blocks=False)
        self.assertEqual({page["id"] for page in result["notion_pages"]}, {"paragraph", "table_row", "code"})
        self.assertTrue(all(page["matched_body_blocks"] for page in result["notion_pages"]))

    def test_plain_index_body_hit_does_not_skip_live_new_body_page(self):
        self.install_index(indexed_candidate("old"), rows=[fixtures.page("fresh")], blocks={
            "fresh": [self.paragraph("fresh-block", "needle")],
        })
        result = search.execute_notion_search('노션 needle 찾아줘', planned_terms=["needle"], include_blocks=False)
        self.assertEqual({page["id"] for page in result["notion_pages"]}, {"old", "fresh"})

    def test_plain_index_keeps_title_match_priority_over_newer_body_only_page(self):
        self.install_index(indexed_candidate("title", title="needle", edited="2025-01-01T00:00:00Z"),
                           indexed_candidate("body", edited="2026-10-12T00:00:00Z"))
        result = search.execute_notion_search('노션 needle 찾아줘', planned_terms=["needle"], limit=1,
                                              include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["title"])
