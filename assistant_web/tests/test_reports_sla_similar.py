"""SLA(처리시간) 리포트와 유사사례 해결책 리포트의 결정형 로직 테스트."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.reports import similar, sla


class SlaReportTest(unittest.TestCase):
    def setUp(self):
        self._originals = {
            "fetch": sla.fetch_mantis_recent_issues,
            "normalize": sla.normalize_issue,
            "matches": sla.issue_matches_constraints,
            "constraints": sla.extract_query_constraints,
            "sources": sla.build_sources_from_issue_rows,
        }
        sla.normalize_issue = lambda issue: issue
        sla.issue_matches_constraints = lambda issue, constraints: True
        sla.extract_query_constraints = lambda text: {}
        sla.build_sources_from_issue_rows = lambda rows: [{"id": r.get("id")} for r in rows]

    def tearDown(self):
        sla.fetch_mantis_recent_issues = self._originals["fetch"]
        sla.normalize_issue = self._originals["normalize"]
        sla.issue_matches_constraints = self._originals["matches"]
        sla.extract_query_constraints = self._originals["constraints"]
        sla.build_sources_from_issue_rows = self._originals["sources"]

    def _set_issues(self, issues):
        sla.fetch_mantis_recent_issues = lambda **kwargs: issues

    def test_mode_detection_distinguishes_duration_and_aging(self):
        self.assertEqual(sla._sla_mode("평균 처리시간 며칠 걸려?"), "duration")
        self.assertEqual(sla._sla_mode("가장 오래된 미종료 이슈"), "aging")
        # 순수 상태어 건수 질문은 SLA가 가로채면 안 된다.
        self.assertIsNone(sla._sla_mode("미종료 이슈 몇 건이야?"))
        self.assertIsNone(sla._sla_mode("KB증권 이슈 보여줘"))

    def test_duration_report_uses_closed_issues_only(self):
        self._set_issues([
            {"id": 101, "summary": "A", "status": "closed", "project": "유진증권",
             "created_at": "2026-06-01", "updated_at": "2026-06-05"},   # 4일
            {"id": 102, "summary": "B", "status": "closed", "project": "KB증권",
             "created_at": "2026-05-01", "updated_at": "2026-05-21"},   # 20일
            {"id": 103, "summary": "C", "status": "assigned", "project": "KB증권",
             "created_at": "2026-07-01", "updated_at": "2026-07-02"},   # 미종료 제외
        ])
        content = sla.build_sla_report("평균 처리시간 며칠 걸려?")["content"]
        self.assertIn("종료 이슈 2건", content)
        self.assertIn("평균 처리기간: **12.0일**", content)  # (4+20)/2
        self.assertIn("#102", content)  # 최장이 먼저

    def test_aging_report_ranks_open_issues_by_age(self):
        self._set_issues([
            {"id": 201, "summary": "묵은건", "status": "assigned", "project": "유진증권",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
            {"id": 202, "summary": "최근건", "status": "assigned", "project": "KB증권",
             "created_at": "2026-07-20", "updated_at": "2026-07-21"},
            {"id": 203, "summary": "종료건", "status": "closed", "project": "KB증권",
             "created_at": "2026-01-01", "updated_at": "2026-01-05"},   # 종료 제외
        ])
        content = sla.build_sla_report("가장 오래 방치된 이슈 알려줘")["content"]
        self.assertIn("미종료 이슈 2건", content)
        # 가장 오래된(2026-01-01) 건이 먼저 나온다.
        self.assertLess(content.index("#201"), content.index("#202"))

    def test_returns_none_for_non_sla_questions(self):
        self.assertIsNone(sla.build_sla_report("KB증권 HTS 이슈 5개"))

    def test_aging_drops_created_at_constraint(self):
        """'이번 주 ... 장기 적체'처럼 등록일 범위가 붙어도 적체 대상이 살아 있어야 한다.

        적체 임계값(30일)과 최근 등록 구간은 정의상 겹칠 수 없어, 등록일 조건을 그대로
        적용하면 데이터와 무관하게 항상 0건이 된다.
        """
        constraints = {
            "customer_aliases": ["159eBest"],
            "created_at": {"from": "2026-08-03", "to": "2026-08-09"},
            "filter_clauses": [
                {"field": "customer", "operator": "equals", "value": "eBest"},
                {"field": "created_at", "operator": "between", "value": ["2026-08-03", "2026-08-09"]},
            ],
        }
        sla.extract_query_constraints = lambda text: constraints
        # 등록일 조건이 살아 있으면 아무것도 통과하지 못하도록 실제 매칭기를 흉내 낸다.
        sla.issue_matches_constraints = lambda issue, cc: "created_at" not in cc
        self._set_issues([
            {"id": 301, "summary": "묵은건", "status": "assigned", "project": "eBest",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
        ])

        report = sla.build_sla_report("이번 주 ebest 장기 적체 알려줘")
        content = report["content"]
        self.assertIn("#301", content)
        self.assertIn("적체 분석에 적용하지 않았습니다", content)
        # 실제로 적용한 조건만 칩·검증 패널에 남아야 한다.
        applied = report["retrieval"]["constraints"]
        self.assertNotIn("created_at", applied)
        self.assertEqual(
            [clause["field"] for clause in applied["filter_clauses"]],
            ["customer"],
        )

    def test_duration_keeps_created_at_constraint(self):
        sla.extract_query_constraints = lambda text: {"created_at": {"from": "2026-08-03"}}
        sla.issue_matches_constraints = lambda issue, cc: "created_at" in cc
        self._set_issues([
            {"id": 401, "summary": "종료건", "status": "closed", "project": "KB증권",
             "created_at": "2026-08-03", "updated_at": "2026-08-05"},
        ])

        report = sla.build_sla_report("평균 처리시간 며칠 걸려?")
        self.assertIn("#401", report["content"])
        self.assertIn("created_at", report["retrieval"]["constraints"])

    def test_empty_result_names_the_constraint_that_emptied_it(self):
        sla.extract_query_constraints = lambda text: {
            "customer_aliases": ["159eBest"],
            "statuses": ["open"],
        }
        # 고객사만 맞고 상태는 맞지 않는 상황을 만든다.
        sla.issue_matches_constraints = lambda issue, cc: "statuses" not in cc
        self._set_issues([
            {"id": 501, "summary": "A", "status": "closed", "project": "eBest",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
        ])

        report = sla.build_sla_report("ebest 장기 적체 알려줘")
        content = report["content"]
        self.assertIn("조건에 맞는 이슈가 없습니다", content)
        self.assertIn("상태 조건을 빼면 1건", content)
        self.assertEqual(report["retrieval"]["stages"][0]["status"], "ok")
        # 결과를 비우지 않은 조건은 힌트에 나오면 안 된다.
        self.assertNotIn("고객사 조건을 빼면", content)


class SimilarResolutionReportTest(unittest.TestCase):
    def setUp(self):
        self._originals = {
            "constraints": similar.extract_query_constraints,
            "wants": similar.wants_similar_issue_search,
            "fetch": similar.fetch_mantis_issue_by_id,
            "normalize": similar.normalize_issue,
            "search_text": similar.build_similar_issue_search_text,
            "terms": similar.build_keyword_terms,
            "sources": similar.build_sources_from_issue_rows,
            "chroma": similar.execute_chroma_search,
        }
        similar.extract_query_constraints = lambda text: {
            "issue_ids": [12345],
            "filter_clauses": [
                {"field": "issue_id", "operator": "in", "value": [12345]}
            ],
        }
        similar.wants_similar_issue_search = lambda text, constraints: True
        similar.fetch_mantis_issue_by_id = lambda issue_id: {"id": issue_id, "summary": "[1200] 세션 만료"}
        similar.normalize_issue = lambda raw: {"id": raw["id"], "summary": raw["summary"], "description": ""}
        similar.build_similar_issue_search_text = lambda issue: "세션 만료"
        similar.build_keyword_terms = lambda text, constraints: ["세션"]
        similar.build_sources_from_issue_rows = lambda rows: [{"id": r.get("id")} for r in rows]

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(
                similar,
                {
                    "constraints": "extract_query_constraints",
                    "wants": "wants_similar_issue_search",
                    "fetch": "fetch_mantis_issue_by_id",
                    "normalize": "normalize_issue",
                    "search_text": "build_similar_issue_search_text",
                    "terms": "build_keyword_terms",
                    "sources": "build_sources_from_issue_rows",
                    "chroma": "execute_chroma_search",
                }[name],
                value,
            )

    def test_wants_resolution_gate(self):
        self.assertTrue(similar._wants_resolution("어떻게 해결했어"))
        self.assertFalse(similar._wants_resolution("비슷한 이슈 목록만"))

    def test_summarize_resolution_pulls_status_and_last_note(self):
        detail = {
            "status": "종료된 이슈",
            "history": [{"created_at": "2026-05-02T11:00:00", "field": "상태",
                         "change": "할당된 이슈 => 종료된 이슈"}],
            "notes": [{"text": "타임아웃을 30분으로 늘려 해결함."}],
        }
        summary = similar._summarize_resolution(detail)
        self.assertIn("종료된 이슈", summary)
        self.assertIn("→", summary)
        self.assertIn("타임아웃", summary)

    def test_report_lists_similar_cases_with_resolution(self):
        captured = {}

        def search(q, limit, cons, semantic=False):
            captured["constraints"] = cons
            return {
                "status": "ok",
                "documents": [
                    {"issue_id": 9001, "latest_issue": {
                        "id": 9001, "summary": "세션 만료 재로그인", "project": "유진증권",
                        "status": "종료된 이슈",
                        "notes": [{"text": "타임아웃 30분으로 조정."}],
                        "history": [{"created_at": "2026-05-02", "field": "상태",
                                     "change": "할당 => 종료"}],
                    }},
                    {"issue_id": 12345, "latest_issue": {"id": 12345, "summary": "자기자신"}},
                ],
            }

        similar.execute_chroma_search = search
        report = similar.build_similar_resolution_report("#12345랑 비슷한 이슈 어떻게 해결했어?")
        content = report["content"]
        self.assertIn("#9001", content)
        self.assertIn("타임아웃", content)
        # 기준 이슈 자기 자신은 유사 목록에서 제외된다.
        self.assertNotIn("자기자신", content)
        candidate = report["playbookCandidate"]
        self.assertIn("세션 만료", candidate["title"])
        self.assertIn("타임아웃", candidate["resolution"])
        self.assertEqual(candidate["sourceIssueIds"], [12345, 9001])
        self.assertEqual(captured["constraints"]["issue_ids"], [])
        self.assertEqual(captured["constraints"]["filter_clauses"], [])

    def test_returns_none_without_resolution_intent(self):
        self.assertIsNone(similar.build_similar_resolution_report("#12345 비슷한 이슈 목록"))


if __name__ == "__main__":
    unittest.main()
