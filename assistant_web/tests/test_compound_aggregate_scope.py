"""복합 집계 질문의 포함 설명이 조회 범위를 좁히지 않는지 검증한다."""

import unittest
from contextlib import contextmanager
from unittest.mock import patch

from backend.intent.search_terms import build_keyword_terms
from backend.intent.status_semantics import resolve_status_intent
from backend.query_planning import build_query_plan
from backend.sources.mantis import issue_matches_constraints
from backend.understanding import extract_query_constraints, merge_query_plan_constraints
from backend.schemas import ChatMessage, ChatRequest
from backend.services.chat_service import chat_service
from backend.sources.mantis_collection import IssueBatch


def issue(issue_id, project, status, summary, created_at="2026-09-15", *, handler="석지원"):
    return {
        "id": issue_id,
        "project": {"name": project},
        "status": {"name": status},
        "handler": {"real_name": handler},
        "summary": summary,
        "created_at": created_at,
    }


class CompoundAggregateScopeTests(unittest.TestCase):
    H11 = "2026년 9월에 등록된 다올투자증권 Mantis 이슈는 총 몇 건이야? 정기점검도 포함해서 번호와 제목을 함께 보여줘."
    H12 = "2026년 9월에 등록된 SK증권 이슈 중 저장본에서 종료되지 않은 건만 번호·제목·현재 상태로 정리해줘. 개발완료 상태도 종료가 아니면 포함해줘."
    P02 = "2026년 9월 등록 NH증권 이슈 가운데 종료 상태가 아닌 것만 세고 번호·제목·상태를 보여줘. 개발완료도 종료 전이면 포함해줘."
    Q01 = "IBK투자증권의 2026년 5월 등록 건을 정리해줘. 우리 팀 범위에서 서버 정기점검까지 포함한 전체 건수와 이슈 번호·제목이 필요해."
    S01 = "교보증권에서 2026년 7월에 등록된 우리 팀 이슈를 모두 집계해줘. 제목에 적힌 점검 월과 관계없이 등록일 기준으로 세고, 정기점검도 포함해서 번호와 제목을 보여줘."

    def test_title_month_basis_does_not_narrow_registration_count(self):
        constraints = extract_query_constraints(self.S01)
        plan = build_query_plan(self.S01, {"source": "mantis"}, constraints, {
            "planned": True, "source": "mantis", "operation": "search",
            "terms": ["집계해줘.", "적힌", "점검", "월과"],
            "filter_clauses": [{"field": "keyword", "operator": "not_contains", "value": "관계"}],
        })
        merged = merge_query_plan_constraints(constraints, plan)
        rows = [
            issue(31228, "140Kyobo(교보증권)", "closed", "교보증권 2026.06 정기점검(MTS)", "2026-07-01"),
            issue(31229, "140Kyobo(교보증권)", "closed", "교보증권 2026.03 정기점검(HTS)", "2026-07-02"),
            issue(31230, "140Kyobo(교보증권)", "assigned", "일반 차트 문의", "2026-07-03"),
            issue(31231, "140Kyobo(교보증권)", "closed", "교보증권 2026.06 정기점검", "2026-07-04"),
            issue(31233, "140Kyobo(교보증권)", "confirmed", "일반 로그인 문의", "2026-07-05"),
            issue(31262, "140Kyobo(교보증권)", "closed", "교보증권 2026.03 정기점검", "2026-07-20"),
            issue(31289, "140Kyobo(교보증권)", "assigned", "일반 거래 문의", "2026-07-28"),
            issue(31290, "140Kyobo(교보증권)", "assigned", "7월 정기점검", "2026-08-01"),
        ]
        self.assertEqual(constraints["created_at"], {"from": "2026-07-01", "to": "2026-07-31", "precision": "month"})
        self.assertEqual(build_keyword_terms(self.S01, constraints), [])
        self.assertFalse(resolve_status_intent(self.S01, constraints)["needs_clarification"])
        self.assertEqual(plan["operation"], "count")
        self.assertEqual(plan["terms"], [])
        self.assertFalse(any(c["field"] in {"keyword", "title"} for c in merged["filter_clauses"]))
        self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)],
                         [31228, 31229, 31230, 31231, 31233, 31262, 31289])

    def test_title_keyword_condition_remains_explicit(self):
        constraints = extract_query_constraints('2026년 7월 교보증권 제목에 "점검" 포함된 이슈를 집계해줘')
        self.assertIn({"field": "title", "operator": "contains", "value": "점검"}, constraints["filter_clauses"])

    def test_nonclosed_count_with_inclusive_development_complete(self):
        constraints = extract_query_constraints(self.P02)
        plan = build_query_plan(self.P02, {"source": "mantis"}, constraints)
        self.assertEqual(constraints["statuses"], ["open"])
        self.assertEqual(build_keyword_terms(self.P02, constraints), [])
        self.assertEqual(plan["operation"], "count")
        self.assertEqual(plan["terms"], [])
        self.assertFalse(any(clause["field"] == "keyword" for clause in plan["filter_clauses"]))

    def test_including_maintenance_up_to_full_scope_is_not_a_keyword_filter(self):
        constraints = extract_query_constraints(self.Q01)
        plan = build_query_plan(self.Q01, {"source": "mantis"}, constraints)
        merged = merge_query_plan_constraints(constraints, plan)
        self.assertEqual(plan["operation"], "count")
        self.assertEqual(build_keyword_terms(self.Q01, constraints), [])
        self.assertEqual(plan["terms"], [])
        self.assertFalse(any(c["field"] == "keyword" for c in plan["filter_clauses"]))
        rows = [
            issue(30988, "141IBK(IBK증권)", "closed", "차트 문의", "2026-05-08"),
            issue(31065, "141IBK(IBK증권)", "assigned", "서버 정기점검", "2026-05-25"),
            issue(31089, "141IBK(IBK증권)", "closed", "주문 오류", "2026-05-29"),
            issue(31090, "141IBK(IBK증권)", "closed", "6월 문의", "2026-06-01"),
        ]
        self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)],
                         [30988, 31065, 31089])
        model_plan = build_query_plan(self.Q01, {"source": "mantis"}, constraints, {
            "planned": True, "source": "mantis", "operation": "count",
            "terms": ["정기점검까지", "번호"],
            "filter_clauses": [{"field": "keyword", "operator": "contains", "value": "정기점검까지"}],
        })
        self.assertEqual(model_plan["terms"], [])
        self.assertFalse(any(c["field"] == "keyword" for c in model_plan["filter_clauses"]))

    def test_chat_service_uses_full_offline_snapshot_for_compound_questions(self):
        rows = [
            issue(31456, "115다올투자증권", "new", "차트 기능 문의"),
            issue(31480, "115다올투자증권", "assigned", "로그인 장애"),
            issue(31544, "115다올투자증권", "developed", "거래 화면 수정"),
            issue(31574, "115다올투자증권", "closed", "9월 정기점검"),
            issue(31459, "108SK(SK증권)", "new", "신규 차트 요청"),
            issue(31483, "108SK(SK증권)", "assigned", "진행 중 주문 오류"),
            issue(31565, "108SK(SK증권)", "developed", "개발 완료 수정", handler="왕승엽"),
            issue(31566, "108SK(SK증권)", "resolved", "해결된 수정"),
            issue(31537, "120NH(NH증권)", "developed", "차트 기능 개선"),
            issue(31539, "120NH(NH증권)", "confirmed", "시세 확인 요청"),
            issue(31564, "120NH(NH증권)", "developed", "매매 화면 개선"),
            issue(31572, "120NH(NH증권)", "developed", "조건검색 개선"),
            issue(31573, "120NH(NH증권)", "closed", "종료된 문의"),
            issue(31228, "140Kyobo(교보증권)", "closed", "교보증권 2026.06 정기점검(MTS)", "2026-07-01"),
            issue(31229, "140Kyobo(교보증권)", "closed", "교보증권 2026.03 정기점검(HTS)", "2026-07-02"),
            issue(31230, "140Kyobo(교보증권)", "assigned", "일반 차트 문의", "2026-07-03"),
            issue(31231, "140Kyobo(교보증권)", "closed", "교보증권 2026.06 정기점검", "2026-07-04"),
            issue(31233, "140Kyobo(교보증권)", "confirmed", "일반 로그인 문의", "2026-07-05"),
            issue(31262, "140Kyobo(교보증권)", "closed", "교보증권 2026.03 정기점검", "2026-07-20"),
            issue(31289, "140Kyobo(교보증권)", "assigned", "일반 거래 문의", "2026-07-28"),
        ]

        @contextmanager
        def snapshot():
            yield IssueBatch(rows, {"fetchedAt": "2026-10-01T00:00:00+00:00", "search_complete": True})

        by_id = {row["id"]: row for row in rows}
        with (
            patch("backend.services.chat_service.get_private_model", return_value={"id": "offline", "status": "active"}),
            patch("backend.services.chat_service.get_model_api_key", return_value=""),
            patch("backend.intent.status_semantics.get_model_api_key", return_value=""),
            patch("backend.sources.mantis_store.stream_complete_store", side_effect=snapshot),
            patch("backend.sources.mantis.fetch_mantis_issue_by_id", side_effect=lambda issue_id: by_id.get(int(issue_id))),
            patch("backend.reports.aggregate.collect_mantis_live_ids_in_range", return_value={
                "status": "ok", "issues": {}, "scanned_ids": [], "scanned_pages": 0, "complete": True,
            }),
            patch("backend.retrieval.get_mantis_snapshot_freshness", return_value="2026-10-01"),
            patch("backend.sources.mantis.get_mantis_snapshot_freshness", return_value="2026-10-01"),
        ):
            for question, expected, mode in (
                (self.H11, [31456, 31480, 31544, 31574], "deterministic_mantis_count"),
                (self.H12, [31459, 31483], "deterministic_issue_list"),
                (f"전사 {self.H12}", [31459, 31483, 31565], "deterministic_issue_list"),
                (self.P02, [31537, 31539, 31564, 31572], "deterministic_mantis_count"),
                (self.S01, [31228, 31229, 31230, 31231, 31233, 31262, 31289], "deterministic_mantis_count"),
            ):
                with self.subTest(question=question):
                    request = ChatRequest(
                        modelId="offline", abTest=False,
                        messages=[ChatMessage(role="user", content=question)],
                        tools={"mantis": True, "vectorDb": False, "notion": False},
                    )
                    response = chat_service.build_response(request, user={"uid": "compound-offline"})
                    retrieval = response["retrieval"]
                    plan = retrieval["query_plan"]
                    self.assertEqual(response["raw"]["mode"], mode)
                    self.assertEqual(sorted(source["issue_id"] for source in response["sources"]), expected)
                    self.assertFalse(retrieval["constraints"]["status_resolution"]["needs_clarification"])
                    self.assertEqual(plan["terms"], [])
                    self.assertFalse(any(c["field"] == "keyword" for c in plan["filter_clauses"]))
                    self.assertFalse(any(c["field"] == "keyword" for c in retrieval["constraints"]["filter_clauses"]))
                    if question == self.S01:
                        self.assertIn("전체 이슈", response["content"])
                        for issue_id in expected:
                            self.assertIn(f"#{issue_id} ", response["content"])
                    if question in {self.H12, f"전사 {self.H12}"}:
                        self.assertEqual(retrieval["constraints"]["statuses"], ["open"])
                        self.assertEqual(retrieval["keyword"], "")
                        self.assertEqual(
                            retrieval["constraints"]["mantis_team_scope"],
                            "all" if question.startswith("전사 ") else "default",
                        )

    def test_including_regular_maintenance_does_not_require_it(self):
        question = "2026년 9월 등록 다올투자증권 이슈 총 몇건? 정기점검도 포함해서 번호 제목"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        merged = merge_query_plan_constraints(constraints, plan)
        rows = [
            issue(31456, "115다올투자증권", "new", "차트 기능 문의"),
            issue(31480, "115다올투자증권", "assigned", "로그인 장애"),
            issue(31544, "115다올투자증권", "developed", "거래 화면 수정"),
            issue(31574, "115다올투자증권", "closed", "9월 정기점검"),
            issue(31575, "115다올투자증권", "new", "10월 이슈", "2026-10-01"),
            issue(31576, "120NH(NH증권)", "new", "타사 정기점검"),
        ]

        self.assertEqual(plan["operation"], "count")
        self.assertFalse(resolve_status_intent(question, constraints)["needs_clarification"])
        self.assertEqual(plan["filters"]["created_at"]["precision"], "month")
        self.assertFalse(any(clause["field"] in {"title", "keyword"} for clause in plan["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)],
                         [31456, 31480, 31544, 31574])
        model_plan = build_query_plan(question, {"source": "mantis"}, constraints, {
            "planned": True, "source": "mantis", "operation": "count",
            "terms": ["정기점검도", "번호"],
            "filter_clauses": [{"field": "keyword", "operator": "contains", "value": "정기점검도"}],
        })
        self.assertEqual(model_plan["terms"], [])
        self.assertFalse(any(clause["field"] == "keyword" for clause in model_plan["filter_clauses"]))

    def test_development_complete_example_does_not_narrow_open_scope(self):
        question = "2026년 9월 SK증권 저장본 종료되지 않은 건 only, 개발완료 상태도 종료 아니면 포함"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        merged = merge_query_plan_constraints(constraints, plan)
        rows = [
            issue(31459, "108SK(SK증권)", "new", "신규 차트 요청"),
            issue(31483, "108SK(SK증권)", "assigned", "진행 중 주문 오류"),
            issue(31565, "108SK(SK증권)", "developed", "개발 완료 수정", handler="왕승엽"),
            issue(31566, "108SK(SK증권)", "resolved", "해결된 수정"),
            issue(31567, "108SK(SK증권)", "closed", "종료된 수정"),
        ]

        self.assertEqual(constraints["statuses"], ["open"])
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(plan["filters"]["statuses"], ["open"])
        self.assertEqual(merged["mantis_team_scope"], "default")
        self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)],
                         [31459, 31483])
        model_plan = build_query_plan(question, {"source": "mantis"}, constraints, {
            "planned": True, "source": "mantis", "operation": "search",
            "terms": ["저장본", "only", "개발완료"],
            "filter_clauses": [{"field": "keyword", "operator": "contains", "value": "개발완료"}],
        })
        self.assertEqual(model_plan["terms"], [])
        self.assertFalse(any(clause["field"] == "keyword" for clause in model_plan["filter_clauses"]))

    def test_explicit_title_content_still_filters(self):
        constraints = extract_query_constraints('2026년 9월 다올투자증권 제목에 "정기점검" 포함된 이슈')
        self.assertIn({"field": "title", "operator": "contains", "value": "정기점검"},
                      constraints["filter_clauses"])
        unquoted = extract_query_constraints("2026년 9월 다올투자증권 제목 정기점검 포함 이슈")
        self.assertIn({"field": "keyword", "operator": "contains", "value": "정기점검"},
                      unquoted["filter_clauses"])
        excluded = extract_query_constraints("2026년 9월 다올투자증권 제목 로그인 제외 이슈")
        self.assertIn({"field": "keyword", "operator": "not_contains", "value": "로그인"},
                      excluded["filter_clauses"])

    def test_explicit_development_complete_scope_still_filters(self):
        constraints = extract_query_constraints("2026년 9월 SK증권 개발완료 이슈만 보여줘")
        self.assertEqual(constraints["statuses"], ["dev_done"])


if __name__ == "__main__":
    unittest.main()
