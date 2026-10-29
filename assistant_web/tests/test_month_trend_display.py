"""전월 기준이 없거나 0일 때 거짓 증감률을 표시하지 않는다."""
import unittest

from backend.reports.aggregate import format_month_trend_lines


class MonthTrendDisplayTests(unittest.TestCase):
    def test_zero_baseline_is_new_occurrence(self):
        result = "\n".join(format_month_trend_lines({"2026-08": 0, "2026-09": 10}, "월"))
        self.assertIn("2026-09 — 10건 (▲10, 신규 발생)", result)
        self.assertNotIn("+0.0%", result)

    def test_zero_to_zero_has_no_change(self):
        result = "\n".join(format_month_trend_lines({"2026-08": 0, "2026-09": 0}, "월"))
        self.assertIn("2026-09 — 0건 (― 변동 없음)", result)

    def test_positive_to_zero_is_full_decrease(self):
        result = "\n".join(format_month_trend_lines({"2026-08": 10, "2026-09": 0}, "월"))
        self.assertIn("2026-09 — 0건 (▼10, -100.0%)", result)

    def test_missing_month_is_not_treated_as_previous_month_or_zero(self):
        result = "\n".join(format_month_trend_lines({"2026-07": 5, "2026-09": 10}, "월"))
        line = next(line for line in result.splitlines() if line.startswith("- 2026-09"))
        self.assertIn("전월 자료 없음", line)
        self.assertNotIn("%", line)
        self.assertNotIn("신규 발생", line)
