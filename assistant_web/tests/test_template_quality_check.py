"""템플릿 답변 품질 점검의 질문 생성·저장본 대조·채점 규칙."""

import random
import unittest
from datetime import date

from backend.team_templates import load_team_templates
from scripts.accuracy.template_quality_check import (
    TemplateCase,
    aging_issue_ids,
    build_cases,
    count_issues,
    count_mentioned,
    fill_question,
    grade,
    last_month_range,
    recent_months_range,
    summarize,
    week_range,
)


def issue(issue_id, project, created, status="assigned", handler="홍길동", summary="[MTS] 주문 화면이 열리지 않는 현상"):
    return {
        "id": issue_id,
        "project": {"name": project},
        "created_at": f"{created}T10:00:00+09:00",
        "status": {"name": status},
        "handler": {"name": handler},
        "summary": summary,
    }


TODAY = date(2026, 10, 6)
ISSUES = [
    issue(1001, "147MiraeAsset(미래에셋증권)", "2026-07-01"),
    issue(1002, "147MiraeAsset(미래에셋증권)", "2026-08-15"),
    issue(1003, "147MiraeAsset(미래에셋증권)", "2026-08-15", status="resolved"),
    issue(1004, "147MiraeAsset(미래에셋증권)", "2026-09-06"),
    issue(1009, "147MiraeAsset(미래에셋증권)", "2026-09-07"),
    issue(1005, "147MiraeAsset(미래에셋증권)", "2026-09-29", status="closed"),
    issue(1006, "147MiraeAsset(미래에셋증권)", "2026-10-05"),
    issue(1007, "120NH(NH증권)", "2026-10-06"),
    issue(1008, "900DRFN(공용)", "2026-10-06"),
    issue(1010, "147MiraeAsset(미래에셋증권)", "2026-09-20", status="closed", summary="미래에셋증권 2026.09 서버정기점검"),
]


class TemplateQuestionTests(unittest.TestCase):
    def test_questions_follow_the_same_fill_rules_as_the_composer(self):
        templates = {item["id"]: item for item in load_team_templates()["templates"]}

        self.assertEqual(
            fill_question(templates["aging-issues"], {"days": "30", "customer": "KB증권", "platform": "전체"}),
            "KB증권 30일 넘게 미종료된 이슈를 오래된 순으로 보여줘",
        )
        self.assertEqual(
            fill_question(templates["monthly-customer-report"], {"month": "2026-09", "customer": "KB증권", "platform": "HTS"}),
            "2026년 9월 KB증권 HTS 월간 고객사 리포트를 만들어줘",
        )
        self.assertTrue(
            fill_question(templates["customer-notice"], {"issueId": "#31567", "channel": "고객 안내 메일", "tone": "정중한"})
            .startswith("#31567 이슈를 바탕으로")
        )

    def test_every_template_gets_one_case_with_inputs_from_the_store(self):
        cases = build_cases(load_team_templates()["templates"], ISSUES, today=TODAY, rng=random.Random(1))

        self.assertEqual(len(cases), 12)
        self.assertTrue(all("[" not in case.question and "{" not in case.question for case in cases))
        by_id = {case.template_id: case for case in cases}
        self.assertEqual(by_id["aging-issues"].customer, "미래에셋증권")
        self.assertEqual(by_id["aging-issues"].expected_issue_ids, [1001, 1002, 1004])
        notice = by_id["customer-notice"]
        self.assertIn(f"#{notice.base_issue_id}", notice.forbidden_terms)
        self.assertIn("홍길동", notice.forbidden_terms)
        self.assertEqual(by_id["daily-operations"].expected_count, 3)

    def test_routine_maintenance_tickets_are_not_used_for_incident_templates(self):
        templates = load_team_templates()["templates"]
        for seed in range(20):
            cases = {case.template_id: case for case in build_cases(templates, ISSUES, today=TODAY, rng=random.Random(seed))}
            self.assertIn(cases["incident-review"].base_issue_id, {1003, 1005})


class StoreOracleTests(unittest.TestCase):
    def test_periods_match_the_question_parser(self):
        self.assertEqual(week_range(TODAY, last=False), (date(2026, 10, 5), date(2026, 10, 11)))
        self.assertEqual(week_range(TODAY, last=True), (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertEqual(last_month_range(TODAY), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(recent_months_range(TODAY, 3), (date(2026, 8, 1), TODAY))
        self.assertEqual(recent_months_range(date(2026, 2, 3), 3), (date(2025, 12, 1), date(2026, 2, 3)))

    def test_counts_use_customer_status_and_period(self):
        september = last_month_range(TODAY)
        self.assertEqual(count_issues(ISSUES, start=september[0], end=september[1], customer="미래에셋증권"), 4)
        self.assertEqual(count_issues(ISSUES, start=september[0], end=september[1], customer="미래에셋증권", bucket="open"), 2)
        # 고객사가 아닌 내부 프로젝트는 고객사 집계에서 빠진다.
        self.assertEqual(count_issues(ISSUES, start=TODAY, end=TODAY, customer="NH투자증권"), 1)

    def test_aging_lists_open_issues_older_than_the_threshold_oldest_first(self):
        self.assertEqual(aging_issue_ids(ISSUES, customer="미래에셋증권", days=30, today=TODAY), [1001, 1002, 1004])

    def test_count_must_appear_as_a_number_of_issues(self):
        self.assertTrue(count_mentioned("총 12건입니다.", 12))
        self.assertTrue(count_mentioned("미종료 1,204건", 1204))
        self.assertFalse(count_mentioned("112건", 12))
        self.assertTrue(count_mentioned("조건에 맞는 이슈가 없습니다.", 0))


class GradingTests(unittest.TestCase):
    def case(self, **overrides):
        values = {
            "id": "template-aging-issues",
            "template_id": "aging-issues",
            "title": "장기 미종료 이슈",
            "question": "미래에셋증권 30일 넘게 미종료된 이슈를 오래된 순으로 보여줘",
            "values": {},
            "customer": "미래에셋증권",
            "expected_statuses": ["open"],
            "expected_count": 3,
            "expected_issue_ids": [1001, 1002, 1004],
        }
        values.update(overrides)
        return TemplateCase(**values)

    def response(self, content, *, sources=(), constraints=None, stages=()):
        return {
            "content": content,
            "sources": [{"issue_id": value} for value in sources],
            "retrieval": {
                "constraints": constraints if constraints is not None else {
                    "customer_key": "미래에셋", "customer_aliases": ["미래에셋증권", "147MiraeAsset"], "statuses": ["open"],
                },
                "stages": list(stages),
            },
        }

    def test_a_correct_aging_answer_passes_every_check(self):
        result = grade(
            self.case(),
            self.response("오래된 순으로 총 3건입니다. #1001(등록 97일 경과), #1002, #1004", sources=[1001, 1002, 1004]),
            known_issue_ids={1001, 1002, 1004},
        )
        self.assertTrue(result["passed"], result["checks"])

    def test_incomplete_store_answers_and_wrong_filters_fail_with_reasons(self):
        result = grade(
            self.case(),
            self.response(
                "Mantis 처리시간 분석 도구를 사용할 수 없습니다. 전체 조회가 완료되지 않아 장기 이슈의 총건수를 확정할 수 없습니다.",
                constraints={"customer_key": "KB증권", "customer_aliases": ["KB증권"], "statuses": []},
                stages=[{"source": "mantis_sla", "status": "error", "reason": "incomplete"}],
            ),
            known_issue_ids=set(),
        )
        self.assertFalse(result["passed"])
        self.assertFalse(result["checks"]["completed"]["passed"])
        self.assertIn("고객사 미래에셋증권 → KB증권", result["checks"]["filter"]["problems"])
        self.assertIn("상태 ['open'] 누락", result["checks"]["filter"]["problems"])
        self.assertFalse(result["checks"]["oracle"]["passed"])

    def test_made_up_issue_numbers_and_internal_ids_in_notices_fail(self):
        notice = self.case(
            id="template-customer-notice", template_id="customer-notice", customer="", expected_statuses=[],
            expected_count=None, expected_issue_ids=[], base_issue_id=1003, forbidden_terms=["#1003", "1003", "홍길동"],
        )
        result = grade(
            notice,
            self.response("고객 영향과 진행 상황을 안내드립니다. 담당 홍길동이 #1003과 #99999를 확인 중입니다.",
                          sources=[1003], constraints={}),
            known_issue_ids={1003},
        )
        self.assertFalse(result["checks"]["grounding"]["passed"])
        self.assertEqual(result["checks"]["forbidden"]["problems"], ["내부 정보 노출: #1003", "내부 정보 노출: 1003", "내부 정보 노출: 홍길동"])
        self.assertNotIn("filter", result["checks"])

    def test_summary_scores_each_check_separately(self):
        passed = {"passed": True, "checks": {"completed": {"passed": True, "problems": []}}, "latency_ms": 100.0}
        failed = {"passed": False, "checks": {"completed": {"passed": False, "problems": ["x"]}}, "latency_ms": 300.0}
        summary = summarize([passed, failed])
        self.assertEqual((summary["score"], summary["status"]), (50, "failed"))
        self.assertEqual(summary["dimensions"]["completed"], {"passed": 1, "total": 2, "score": 50})
        self.assertEqual(summary["latency"]["max_ms"], 300.0)


if __name__ == "__main__":
    unittest.main()
