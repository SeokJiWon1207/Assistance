import tempfile
import unittest
import unittest.mock
from datetime import date
from pathlib import Path

from backend.reports.effort_trend import (
    DEFAULT_TREND_MONTHS,
    MAX_TREND_MONTHS,
    TREND_ISSUE_TYPES,
    build_effort_trend_report,
    build_effort_trend_report_from_plan,
    extract_document_effort,
    parse_trend_months,
    resolve_trend_window,
    run_effort_trend_report,
    scope_label,
    wants_effort_trend,
)
from backend.tools.mantis_index import INDEX_FILENAME, build_mantis_index, parse_index_document


def issue_document(
    issue_id: int,
    *,
    created_at: str,
    issue_type: str,
    effort_line: str = "- 1문의공수: 0.00",
    customer: str = "132SHGM(신한굿모닝증권)",
    platform: str = "MTS",
) -> str:
    return f"""[Mantis 이슈 정보]
- 이슈 ID: #{issue_id}
- 프로젝트(고객사): {customer}
- 매체구분: {platform}
- 화면번호: 9413
- 작업주체: 개발
- 현재 상태: 할당된 이슈
- 이슈 유형: {issue_type}
- 담당자: 홍길동
- 요청자: 김요청
- 등록일: {created_at}
- 최종수정일: {created_at}

[일정/전달일]
- 고객사요청일: {created_at}
- 고객사전달 목표일: 미상
- 고객사전달일: 미상
- 개발완료 목표일: 미상
- QC 완료 목표일: 미상
- 버전반영일: 미상

[공수]
{effort_line}

[제목]
테스트 이슈 {issue_id}

[고객사 본문 요청 내용]
본문

[재현 과정 및 단계]
없음

[내부 개발 진행 및 대화 이력]
없음"""


class EffortTrendTriggerTest(unittest.TestCase):
    def test_detects_monthly_trend_questions(self):
        self.assertTrue(wants_effort_trend("월별 처리 공수 추이 보여줘"))
        self.assertTrue(wants_effort_trend("최근 20개월 월별 처리건수"))
        self.assertTrue(wants_effort_trend("월별 이슈 유형 추세"))

    def test_ignores_single_period_effort_questions(self):
        self.assertFalse(wants_effort_trend("2026년 8월 공수 알려줘"))
        self.assertFalse(wants_effort_trend("홍길동 공수 알려줘"))
        self.assertFalse(wants_effort_trend(""))

    def test_rule_path_stays_high_precision(self):
        # 애매한 표현은 규칙이 잡지 않고 플래너에 맡긴다.
        self.assertFalse(wants_effort_trend("월별 회의 이슈 정리해줘"))
        self.assertFalse(wants_effort_trend("달별로 이슈가 어떻게 변했어?"))
        # 오탐 없는 표현은 LLM 호출 없이 규칙이 바로 처리한다.
        self.assertTrue(wants_effort_trend("월별 등록건수 보여줘"))
        self.assertTrue(wants_effort_trend("월간 공수 추이"))
        self.assertTrue(wants_effort_trend("달별 처리 건수"))

    def test_catches_statistic_wordings(self):
        # '통계/집계/현황'도 월 단위 표현과 함께 오면 추이 질문이다.
        self.assertTrue(wants_effort_trend("월간 통계 보여줘"))
        self.assertTrue(wants_effort_trend("월별 집계 알려줘"))
        self.assertTrue(wants_effort_trend("월별 현황"))

    def test_rule_path_rejects_non_issue_subjects(self):
        # 대상 어휘가 겹쳐도 회의·공지 주제면 이슈 집계가 아니다.
        self.assertFalse(wants_effort_trend("월별 회의 통계"))
        self.assertFalse(wants_effort_trend("월간 공지 현황"))
        self.assertFalse(wants_effort_trend("월별 휴가 집계"))

    def test_defers_questions_it_cannot_scope(self):
        # 1인칭/지시어는 담당자·맥락 해석이 필요하므로 기존 공수 리포트가 맡는다.
        self.assertFalse(wants_effort_trend("내 월별 공수 추이"))
        self.assertFalse(wants_effort_trend("제 월별 처리건수 추이"))
        self.assertFalse(wants_effort_trend("우리 팀 월별 추이"))
        self.assertFalse(wants_effort_trend("그중 월별 추이는?"))
        self.assertTrue(wants_effort_trend("신한증권 월별 처리건수 추이"))

    def test_pins_window_to_explicit_year(self):
        today = date(2026, 8, 15)
        # 지난 해를 지정하면 그 해 1~12월 전체를 본다.
        self.assertEqual(resolve_trend_window("2025년 월별 처리건수 추이", today=today), (12, 2025, 12))
        # 올해는 아직 오지 않은 달까지 그리지 않는다.
        self.assertEqual(resolve_trend_window("2026년 월별 추이", today=today), (8, 2026, 8))
        # 연도와 개월 수를 함께 적으면 그 해 끝에서 거꾸로 센다.
        self.assertEqual(resolve_trend_window("2025년 6개월 월별 추이", today=today), (6, 2025, 12))
        # 연도가 없으면 종전대로 오늘 기준이다.
        self.assertEqual(resolve_trend_window("월별 추이", today=today), (DEFAULT_TREND_MONTHS, None, None))

    def test_parses_month_span_with_bounds(self):
        self.assertEqual(parse_trend_months("최근 20개월 추이"), 20)
        self.assertEqual(parse_trend_months("월별 추이"), DEFAULT_TREND_MONTHS)
        self.assertEqual(parse_trend_months("48개월 월별 추이"), MAX_TREND_MONTHS)


class EffortDocumentParsingTest(unittest.TestCase):
    def test_sums_only_known_effort_fields(self):
        document = issue_document(
            1,
            created_at="2026-08-01",
            issue_type="오류",
            effort_line="- 1문의공수: 김성조(0.05), 권한별(0.05)\n- 2서버공수: 강우석(0.30)",
        )
        self.assertAlmostEqual(extract_document_effort(document), 0.4)

    def test_returns_zero_when_no_named_effort(self):
        document = issue_document(2, created_at="2026-08-01", issue_type="개선")
        self.assertEqual(extract_document_effort(document), 0.0)

    def test_index_captures_issue_type(self):
        record = parse_index_document(
            issue_document(3, created_at="2026-08-01", issue_type="검토/보류")
        )
        self.assertEqual(record["issue_type"], "검토/보류")

    def test_index_tolerates_legacy_document_without_issue_type(self):
        legacy = issue_document(4, created_at="2026-08-01", issue_type="오류")
        legacy = legacy.replace("- 이슈 유형: 오류\n", "")
        record = parse_index_document(legacy)
        self.assertEqual(record["issue_type"], "")


class EffortTrendReportTest(unittest.TestCase):
    def build_index(self, directory: Path, documents):
        index_path = directory / INDEX_FILENAME
        build_mantis_index(index_path, documents)
        return index_path

    def test_aggregates_counts_and_effort_by_month_and_type(self):
        documents = [
            issue_document(
                1,
                created_at="2026-08-03",
                issue_type="오류",
                effort_line="- 1문의공수: 홍길동(1.50)",
            ),
            issue_document(
                2,
                created_at="2026-08-20",
                issue_type="오류",
                effort_line="- 2서버공수: 김철수(0.50)",
            ),
            issue_document(
                3,
                created_at="2026-07-11",
                issue_type="개선",
                effort_line="- 3PC공수: 이영희(2.00)",
            ),
            # 차트 시리즈에 없는 유형은 집계에서 빠져야 한다.
            issue_document(
                4,
                created_at="2026-08-05",
                issue_type="기각",
                effort_line="- 1문의공수: 박민수(9.00)",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            report = run_effort_trend_report(
                "최근 2개월 월별 처리 공수 추이",
                months=2,
                index_path=index_path,
                today=date(2026, 8, 15),
            )

        chart = report["effortTrend"]
        self.assertEqual(chart["periods"], ["202607", "202608"])
        self.assertEqual([item["label"] for item in chart["series"]], list(TREND_ISSUE_TYPES))

        by_label = {item["label"]: item for item in chart["series"]}
        self.assertEqual(by_label["오류"]["counts"], [0, 2])
        self.assertEqual(by_label["오류"]["efforts"], [0.0, 2.0])
        self.assertEqual(by_label["개선"]["counts"], [1, 0])
        self.assertEqual(by_label["개선"]["efforts"], [2.0, 0.0])
        self.assertEqual(by_label["문의"]["counts"], [0, 0])
        # 기각 이슈는 제외되므로 총 3건만 잡힌다.
        self.assertEqual(chart["totalIssues"], 3)

    def test_reports_missing_issue_type_without_crashing(self):
        legacy = issue_document(9, created_at="2026-08-01", issue_type="오류")
        legacy = legacy.replace("- 이슈 유형: 오류\n", "")
        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), [legacy])
            report = run_effort_trend_report(
                "월별 처리 공수 추이",
                months=2,
                index_path=index_path,
                today=date(2026, 8, 15),
            )

        self.assertEqual(report["effortTrend"]["totalIssues"], 0)
        self.assertIn("make_vector_db.py", report["content"])

    def test_returns_none_for_unrelated_question(self):
        self.assertIsNone(build_effort_trend_report("홍길동 공수 알려줘"))

    def test_applies_customer_constraint_instead_of_company_wide(self):
        documents = [
            issue_document(
                1,
                created_at="2026-08-03",
                issue_type="오류",
                customer="132SHGM(신한굿모닝증권)",
                effort_line="- 1문의공수: 홍길동(1.00)",
            ),
            issue_document(
                2,
                created_at="2026-08-04",
                issue_type="오류",
                customer="900KIWOOM(키움증권)",
                effort_line="- 1문의공수: 김철수(5.00)",
            ),
        ]
        constraints = {"customer_aliases": ["신한굿모닝증권"], "customer_key": "신한굿모닝증권"}
        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            report = run_effort_trend_report(
                "신한굿모닝증권 월별 처리건수 추이",
                months=1,
                index_path=index_path,
                today=date(2026, 8, 15),
                constraints=constraints,
            )

        chart = report["effortTrend"]
        by_label = {item["label"]: item for item in chart["series"]}
        # 키움증권 이슈(공수 5.00)는 집계에서 빠져야 한다.
        self.assertEqual(by_label["오류"]["counts"], [1])
        self.assertEqual(by_label["오류"]["efforts"], [1.0])
        self.assertEqual(chart["scopeLabel"], "신한굿모닝증권")
        self.assertIn("신한굿모닝증권", report["content"])

    def test_warns_when_many_issues_lack_issue_type(self):
        documents = [issue_document(1, created_at="2026-08-01", issue_type="오류")]
        for issue_id in range(2, 12):
            legacy = issue_document(issue_id, created_at="2026-08-02", issue_type="오류")
            documents.append(legacy.replace("- 이슈 유형: 오류\n", ""))

        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            report = run_effort_trend_report(
                "월별 처리건수 추이",
                months=1,
                index_path=index_path,
                today=date(2026, 8, 15),
            )

        self.assertEqual(report["effortTrend"]["untypedCount"], 10)
        self.assertIn("차트에서 빠졌습니다", report["content"])
        self.assertIn("--full", report["content"])

    def test_explicit_year_window_covers_that_year(self):
        documents = [
            issue_document(1, created_at="2025-02-10", issue_type="오류"),
            issue_document(2, created_at="2025-11-05", issue_type="개선"),
            # 지정한 해 밖의 이슈는 빠져야 한다.
            issue_document(3, created_at="2026-03-01", issue_type="오류"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            months, end_year, end_month = resolve_trend_window(
                "2025년 월별 처리건수 추이", today=date(2026, 8, 15)
            )
            report = run_effort_trend_report(
                "2025년 월별 처리건수 추이",
                months=months,
                index_path=index_path,
                today=date(2026, 8, 15),
                end_year=end_year,
                end_month=end_month,
            )

        chart = report["effortTrend"]
        self.assertEqual(chart["periods"][0], "202501")
        self.assertEqual(chart["periods"][-1], "202512")
        self.assertEqual(chart["totalIssues"], 2)

    def test_empty_result_does_not_blame_the_index(self):
        documents = [issue_document(1, created_at="2026-08-01", issue_type="오류")]
        constraints = {"customer_aliases": ["없는고객사"], "customer_key": "없는고객사"}
        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            report = run_effort_trend_report(
                "없는고객사 월별 처리건수 추이",
                months=1,
                index_path=index_path,
                today=date(2026, 8, 15),
                constraints=constraints,
            )

        self.assertIn("해당하는 이슈가 없습니다", report["content"])
        self.assertNotIn("--full", report["content"])

    def test_summary_states_registration_date_basis(self):
        documents = [issue_document(1, created_at="2026-08-01", issue_type="오류")]
        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            report = run_effort_trend_report(
                "월별 처리건수 추이", months=1, index_path=index_path, today=date(2026, 8, 15)
            )
        self.assertIn("등록일", report["content"])
        self.assertIn("처리·완료 시점 아님", report["content"])

    def test_planner_path_runs_trend_for_unmatched_phrasing(self):
        documents = [
            issue_document(1, created_at="2026-08-01", issue_type="오류"),
            issue_document(2, created_at="2026-08-02", issue_type="개선"),
        ]
        # 규칙은 못 잡지만 플래너가 trend 로 분류한 질문.
        question = "달별로 이슈가 어떻게 변했어?"
        self.assertFalse(wants_effort_trend(question))

        with tempfile.TemporaryDirectory() as directory:
            index_path = self.build_index(Path(directory), documents)
            with unittest.mock.patch(
                "backend.reports.effort_trend.run_effort_trend_report",
                side_effect=lambda *args, **kwargs: run_effort_trend_report(
                    *args, **{**kwargs, "index_path": index_path, "today": date(2026, 8, 15)}
                ),
            ):
                report = build_effort_trend_report_from_plan(
                    question,
                    {"effort": {"type": "trend"}},
                    constraints={},
                )

        self.assertIsNotNone(report)
        self.assertEqual(report["effortTrend"]["totalIssues"], 2)

    def test_planner_path_ignores_other_effort_types(self):
        for effort in ({"type": "person", "person": "홍길동"}, {"type": "ranking"}, {}):
            self.assertIsNone(
                build_effort_trend_report_from_plan("달별 변화", {"effort": effort}, constraints={})
            )
        self.assertIsNone(
            build_effort_trend_report_from_plan("달별 변화", {}, constraints={})
        )

    def test_planner_path_rejects_non_issue_subjects(self):
        # 플래너가 trend 라고 해도 회의록·공지 주제면 이슈 집계가 아니다.
        plan = {"effort": {"type": "trend"}}
        for question in ("월별 회의 이슈 정리해줘", "월간 공지 추이", "월별 휴가 현황 추이"):  # noqa: E501
            self.assertIsNone(
                build_effort_trend_report_from_plan(question, plan, constraints={}),
                msg=question,
            )

    def test_planner_path_defers_unresolved_scope(self):
        # 플래너가 trend 라고 해도 화자 표현이 있으면 넘기지 않는다.
        self.assertIsNone(
            build_effort_trend_report_from_plan(
                "내 공수 어떻게 변했어", {"effort": {"type": "trend"}}, constraints={}
            )
        )

    def test_scope_label_defaults_to_company_wide(self):
        self.assertEqual(scope_label({}), "전사")
        self.assertEqual(scope_label({"customer_key": "신한", "platform_key": "mts"}), "신한 · MTS")
        self.assertEqual(scope_label({"people": {"names": ["홍길동"]}}), "홍길동")


if __name__ == "__main__":
    unittest.main()
