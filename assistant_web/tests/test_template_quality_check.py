"""템플릿 답변 품질 점검의 질문 생성·저장본 대조·채점 규칙."""

import random
import unittest
from datetime import date

from backend.intent.search_terms import resolve_result_limit
from backend.team_templates import load_team_templates
from backend.understanding import build_keyword_terms, extract_query_constraints
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

    def test_particles_follow_the_value_chosen_for_every_option(self):
        templates = {item["id"]: item for item in load_team_templates()["templates"]}
        analysis = {"customer": "KB증권", "period": "지난달", "platform": "전체", "status": "전체"}
        self.assertTrue(fill_question(templates["customer-analysis"], {**analysis, "format": "표"}).endswith("이슈를 표로 모두 보여줘"))
        self.assertTrue(fill_question(templates["customer-analysis"], {**analysis, "format": "목록"}).endswith("이슈를 목록으로 모두 보여줘"))
        self.assertTrue(fill_question(templates["customer-analysis"], {**analysis, "format": "요약"}).endswith("이슈를 요약으로 모두 보여줘"))
        # 분석 템플릿은 표시 개수 기본값(5건)에 잘리지 않고 조건에 맞는 이슈를 모두 보여 준다.
        for fmt in ("표", "목록", "요약"):
            question = fill_question(templates["customer-analysis"], {**analysis, "format": fmt})
            self.assertEqual(resolve_result_limit(question, 5), 200, question)
            self.assertEqual(build_keyword_terms(question, extract_query_constraints(question)), [], question)
        similar = templates["similar-issues"]
        self.assertTrue(fill_question(similar, {"issueId": "#30225"}).startswith("#30225와 비슷한"))
        self.assertTrue(fill_question(similar, {"issueId": "#30220"}).startswith("#30220과 비슷한"))
        # 고른 값과 빈 칸 조합 어디에서도 조사가 홀로 남거나 받침과 어긋나지 않는다.
        for template in templates.values():
            selects = [field for field in template["fields"] if field.get("options")]
            for field in selects:
                for option in field["options"]:
                    values = {**{spec["name"]: "" for spec in template["fields"]}, field["name"]: option, "issueId": "#31572", "symptom": "로그인 오류", "month": "2026-09", "days": "30"}
                    question = fill_question(template, values)
                    with self.subTest(template=template["id"], option=option):
                        self.assertNotRegex(question, r"(?:^|\s)(?:에서|으로|로|을|를|와|과)(?:\s|$)")
                        self.assertNotRegex(question, r"\((?:으|을|를|이|가|은|는|과|와)\)")

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

    def test_extended_checks_cover_period_status_and_inherited_customer_filters(self):
        cases = build_cases(load_team_templates()["templates"], ISSUES, today=TODAY,
                            rng=random.Random(1), include_variants=True)
        by_id = {case.id: case for case in cases}
        self.assertEqual(len(cases), 15)
        variant = by_id["template-customer-analysis-open-week"]
        self.assertEqual(variant.expected_count, 1)
        self.assertEqual(variant.expected_range, {"from": "2026-10-05", "to": "2026-10-11"})
        followup = by_id["template-customer-analysis-followup-open"]
        self.assertEqual(followup.question, "그중 미종료만 표로 모두 보여줘")
        self.assertEqual(followup.expected_count, 2)
        self.assertIn("지난달 미래에셋증권", followup.preceding_questions[0])
        self.assertEqual(by_id["template-customer-profile-six-months"].expected_count, 8)


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

    def test_absent_effort_and_subtotals_cannot_satisfy_the_issue_total(self):
        self.assertFalse(count_mentioned("총 12건입니다. 공수 정보는 없습니다.", 0))
        self.assertFalse(count_mentioned("전체 이슈는 총 12건이며 종료된 이슈는 3건입니다.", 3))
        self.assertFalse(count_mentioned("이슈 12건 중 공수 미입력 0건", 0))
        self.assertFalse(count_mentioned("종료된 이슈가 없습니다.", 0, "customer-analysis"))
        self.assertFalse(count_mentioned("신규 등록 이슈: 12건. 미종료 총 3건. 장기 적체 0건. 공수 없음.", 3, "weekly-report"))
        self.assertFalse(count_mentioned("전월 총 3건. 현재 등록 이슈는 미확인입니다. 공수 비교는 생략합니다.", 3, "monthly-customer-report"))
        self.assertFalse(count_mentioned("전체 미종료 총 3건. 30일 이상 이슈 건수는 미확인입니다.", 3, "aging-issues"))


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

    def test_required_filters_cannot_disappear_from_the_evaluation(self):
        result = grade(self.case(), self.response(
            "오래된 순으로 총 3건입니다. #1001, #1002, #1004", constraints={},
        ), {1001, 1002, 1004})
        self.assertFalse(result["passed"])
        self.assertIn("filter", result["checks"])
        self.assertFalse(result["checks"]["filter"]["passed"])

    def test_report_total_is_bound_to_the_requested_metric(self):
        for template_id, text in (
            ("monthly-customer-report", "등록 이슈: 12건 · 전월 3건 대비 증가. 공수 비교"),
            ("weekly-report", "신규 등록: 12건 · 미종료 3건 · 장기 적체 0건 · 공수 0"),
            ("aging-issues", "미종료 12건 중 30일 이상 경과한 이슈: 3건 · 오래된 순"),
        ):
            result = grade(self.case(template_id=template_id, expected_issue_ids=[]), self.response(text), set())
            with self.subTest(template_id=template_id):
                self.assertEqual(result["checks"]["oracle"]["passed"], template_id == "aging-issues")

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

    def test_customer_notice_requires_all_requested_sections(self):
        notice = self.case(
            template_id="customer-notice", customer="", expected_statuses=[],
            expected_count=None, expected_issue_ids=[], base_issue_id=1003,
        )
        text = "확인된 사실: 오류 발생. 고객 영향: 범위 미확정. 진행 상황: 조사 중. 다음 안내 시점: 미확정."
        for missing in ("확인된 사실: 오류 발생. ", "다음 안내 시점: 미확정."):
            result = grade(notice, self.response(text.replace(missing, ""), sources=[1003]), {1003})
            self.assertFalse(result["checks"]["sections"]["passed"])
        self.assertTrue(grade(notice, self.response(text, sources=[1003]), {1003})["checks"]["sections"]["passed"])

    def test_customer_notice_next_update_needs_a_time_condition_or_unknown_schedule(self):
        notice = self.case(template_id="customer-notice", customer="", expected_statuses=[],
                           expected_count=None, expected_issue_ids=[], base_issue_id=1003)
        prefix = "확인된 사실: 오류 접수. 고객 영향: 범위 미확정. 진행 상황: 조사 중.\n"
        for update in (
            "추가 안내를 드리겠습니다.", "다음 안내 시점:",
            "추가 안내를 드리겠습니다.\n고객 영향: 범위 미확정.",
            "추가 안내를 드리겠습니다. 고객 영향: 범위 미확정.",
            "다음 안내 시점:\n고객 영향: 범위 미확정.",
        ):
            with self.subTest(update=update):
                self.assertFalse(grade(notice, self.response(prefix + update, sources=[1003]), {1003})["checks"]["sections"]["passed"])
        for update in ("다음 안내 시점: 미확정", "추가 안내는 내일 오전에 드리겠습니다.",
                       "다음 안내는 원인 확인 후 드리겠습니다.", "다음 안내 시점: 2026-10-08 10:00",
                       "**다음 안내 시점**\n* 미확정"):
            with self.subTest(update=update):
                self.assertTrue(grade(notice, self.response(prefix + update, sources=[1003]), {1003})["checks"]["sections"]["passed"])

    def test_summary_scores_each_check_separately(self):
        passed = {"passed": True, "checks": {"completed": {"passed": True, "problems": []}}, "latency_ms": 100.0}
        failed = {"passed": False, "checks": {"completed": {"passed": False, "problems": ["x"]}}, "latency_ms": 300.0}
        summary = summarize([passed, failed])
        self.assertEqual((summary["score"], summary["status"]), (50, "failed"))
        self.assertEqual(summary["dimensions"]["completed"], {"passed": 1, "total": 2, "score": 50})
        self.assertEqual(summary["latency"]["max_ms"], 300.0)


if __name__ == "__main__":
    unittest.main()
