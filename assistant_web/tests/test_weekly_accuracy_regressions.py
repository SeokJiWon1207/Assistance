"""최근 일주일 실데이터 검증에서 발견한 Notion 상태 충돌의 재현."""

import unittest
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints
from backend.intent.context import build_contextual_retrieval_text
from backend.schemas import ChatMessage
from backend.sources import mantis
from backend.sources.mantis_collection import IssueBatch
from backend.notion.mapping import notion_status
from backend.notion.search import (
    extract_notion_constraints, merge_common_notion_constraints,
    notion_page_matches_and_enrich,
)


def check_current_status_precedes_legacy_property_order(reverse):
    properties = {
        "버전상태": {"type": "select", "select": None},
        "구상태": {"type": "select", "select": {"name": "등록됨"}},
        "상태": {"type": "status", "status": {"name": "진행 중"}},
    }
    if reverse:
        properties = dict(reversed(list(properties.items())))
    assert notion_status(properties) == "진행 중"
    properties["상태"]["status"] = None
    assert notion_status(properties) == "상태 미상"


CASES = [
    ("노션에서 상태가 진행 중인 '상반기 특이사항 확인' 항목을 정확히 찾아줘",
     "진행중", [], "진행 중", "등록됨"),
    ("노션에서 '상반기 특이사항 확인' 찾아주세요. 상태가 진행 중인 것만요",
     "진행중", [], "진행 중", "완료"),
    ("노션에서 상태가 결제완료(팀장)인 '오후반차' 항목을 정확히 찾아줘",
     "결제완료(팀장)", [], "결제완료(팀장)", "결재완료(팀장)"),
    ("노션에서 '오후반차' 항목을 찾아줘. 결제완료(팀장) 상태는 빼고",
     "", ["결제완료(팀장)"], "결재요청", "결제완료(팀장)"),
]


def check_native_notion_options_do_not_intersect_mantis_status(question, status, excluded, match, mismatch):
    constraints = merge_common_notion_constraints(extract_notion_constraints(question), extract_query_constraints(question))
    assert constraints["status"] == status
    assert constraints["excluded_statuses"] == excluded
    assert constraints["status_typo"] is None
    assert not any(c["field"] == "status" for c in constraints["common_filter_clauses"])
    for value, expected in ((match, True), (mismatch, False)):
        page = {"title": constraints["exact_title"], "status": value,
                "status_fields": {"상태": value, "구상태": "등록됨"}}
        assert notion_page_matches_and_enrich(page, constraints) is expected


class WeeklyAccuracyRegressionTests(unittest.TestCase):
    def test_explicit_api_window_cannot_be_replaced_with_id_sorted_store(self):
        current = {"id": 31584, "project": {"name": "140Kyobo(교보증권)"}, "handler": {"real_name": "석지원"}, "created_at": "2026-10-02"}
        older = {"id": 29691, "project": {"name": "140Kyobo(교보증권)"}, "handler": {"real_name": "석지원"}, "created_at": "2025-07-17"}
        stored = IssueBatch([current, older], {"fetchedAt": "2026-10-02T11:00:00+00:00"})
        constraints = extract_query_constraints("2025년 7월 교보증권 이슈")
        with patch.object(mantis, "read_complete_store", return_value=stored) as read, \
                patch.object(mantis, "fetch_mantis_issues", return_value=[older, current]) as fetch:
            window = mantis.collect_mantis_live_ids_in_range(constraints, scan_limit=1)
            self.assertEqual(list(window["issues"]), ["29691"])
            self.assertTrue(window["complete"])
            read.assert_not_called()
            fetch.assert_called_once()
            fetch.reset_mock()
            total = mantis.collect_mantis_live_ids_in_range(constraints)
            self.assertEqual(list(total["issues"]), ["29691"])
            read.assert_called_once()
            fetch.assert_not_called()

    def test_native_status_precedence_preserves_non_status_filters(self):
        native = extract_notion_constraints("노션에서 상태가 진행 중인 '상반기 특이사항 확인' 항목")
        owner = {"field": "owner", "operator": "equals", "value": "홍길동"}
        merged = merge_common_notion_constraints(native, {
            "filter_clauses": [owner, {"field": "status", "operator": "in", "value": ["open"]}],
            "excluded_statuses": ["closed"],
        })
        self.assertEqual(merged["common_filter_clauses"], [owner])
        self.assertEqual(merged["owner"], "홍길동")
        self.assertEqual(merged["excluded_statuses"], [])

    def test_follow_up_retains_confirmed_status_without_saved_plan(self):
        for status in ("confirmed", "resolved", "acknowledged", "assigned", "developed", "feedback", "open", "closed"):
            previous = f"138Meritz(메리츠)에서 상태가 {status}인 최근 이슈 2개 보여줘"
            expected = extract_query_constraints(previous)["statuses"]
            with self.subTest(status=status):
                text, rewrite = build_contextual_retrieval_text([
                    ChatMessage(role="user", content=previous),
                    ChatMessage(role="user", content="방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘"),
                ])
                self.assertFalse(rewrite)
                self.assertTrue(expected)
                self.assertEqual(extract_query_constraints(text)["statuses"], expected)

    def test_current_status_precedes_legacy_property_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                check_current_status_precedes_legacy_property_order(reverse)

    def test_native_notion_options_do_not_intersect_mantis_status(self):
        for case in CASES:
            with self.subTest(question=case[0]):
                check_native_notion_options_do_not_intersect_mantis_status(*case)
