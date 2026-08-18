import tempfile
import unittest
from datetime import date
from pathlib import Path

from backend.reports.effort_trend import (
    DEFAULT_TREND_MONTHS,
    MAX_TREND_MONTHS,
    TREND_ISSUE_TYPES,
    build_effort_trend_report,
    extract_document_effort,
    parse_trend_months,
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

    def test_scope_label_defaults_to_company_wide(self):
        self.assertEqual(scope_label({}), "전사")
        self.assertEqual(scope_label({"customer_key": "신한", "platform_key": "mts"}), "신한 · MTS")
        self.assertEqual(scope_label({"people": {"names": ["홍길동"]}}), "홍길동")


if __name__ == "__main__":
    unittest.main()
