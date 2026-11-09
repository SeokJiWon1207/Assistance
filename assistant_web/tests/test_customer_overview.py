"""팀 대시보드 고객사 탭 집계 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import customer_overview
from backend.settings.catalogs import get_customer_picker


TODAY = date(2026, 8, 27)  # 목요일, 이번 주는 8/24부터


def _issue(issue_id, created_at, project="147MiraeAsset(미래에셋증권)", **fields):
    return {"id": issue_id, "summary": f"이슈 {issue_id}", "project": project, "status": "confirmed",
            "handler": "석지원", "created_at": created_at, **fields}


class CustomerOverviewTests(unittest.TestCase):
    def _build(self, issues, *, complete=True):
        source = {"status": "snapshot", "search_complete": complete, "fetchedAt": "2026-08-27T09:00:00+09:00"}
        with patch.object(customer_overview, "_load_issues", return_value=(issues, source)):
            return customer_overview.build_customer_overview(today=TODAY)

    def test_every_picker_customer_is_listed_in_order_with_weekly_counts(self):
        issues = [
            # 미래에셋: 직전 4주 각 1건, 지난주 4건 → 급증. 하나는 종료, 하나는 기한 지남.
            _issue(1, "2026-07-20"), _issue(2, "2026-07-28"), _issue(3, "2026-08-04"), _issue(4, "2026-08-12"),
            _issue(5, "2026-08-17"), _issue(6, "2026-08-18", status="closed"), _issue(7, "2026-08-19"),
            _issue(8, "2026-08-20", date_fields={"완료예정일": "2026-08-21"}),
            _issue(9, "2026-08-25", project="125Daewoo"),  # 다른 고객사(옛 대우) — 목록 밖
            _issue(10, "2026-05-01", project="112KBStar(KB증권)"),  # 8주 밖이지만 미종료·장기
            _issue(11, "2026-08-20", handler="다른팀"),
        ]
        result = self._build(issues)
        names = [row["name"] for row in result["customers"]]
        self.assertEqual(names, list(get_customer_picker()))
        mirae = next(row for row in result["customers"] if row["name"] == "미래에셋증권")
        self.assertEqual(mirae["weekly"], [0, 0, 1, 1, 1, 1, 4, 0])
        self.assertEqual((mirae["open"], mirae["overdue"], mirae["recent"], mirae["lastCreatedAt"]), (7, 1, 8, "2026-08-20"))
        self.assertEqual(mirae["project"], "147MiraeAsset(미래에셋증권)")
        self.assertEqual(mirae["spike"], {"count": 4, "baselineAverage": 1.0, "ratio": 4.0})
        kb = next(row for row in result["customers"] if row["name"] == "KB증권")
        self.assertEqual((kb["open"], kb["aging"], kb["recent"], kb["weekly"]), (1, 1, 0, [0] * 8))
        samsung = next(row for row in result["customers"] if row["name"] == "삼성증권")
        self.assertEqual((samsung["open"], samsung["project"], samsung["spike"]), (0, "", None))
        self.assertEqual(result["summary"]["withOpen"], 2)
        self.assertEqual(result["summary"]["spiking"], 1)
        self.assertEqual(result["weeks"][-1], {"start": "2026-08-24", "label": "8/24", "partial": True})

    def test_overdue_counts_only_open_issues_and_partial_data_has_no_spike(self):
        issues = [_issue(1, "2026-08-17", date_fields={"완료예정일": "2026-08-20"}), _issue(2, "2026-08-17", date_fields={"완료예정일": "2026-08-20"}, status="closed")]
        for _ in range(3):
            issues.append(_issue(len(issues) + 1, "2026-08-18"))
        complete = next(row for row in self._build(issues)["customers"] if row["name"] == "미래에셋증권")
        self.assertEqual(complete["overdue"], 1)
        partial = self._build(issues, complete=False)
        self.assertIsNone(next(row for row in partial["customers"] if row["name"] == "미래에셋증권")["spike"])
        self.assertEqual(partial["summary"]["spiking"], 0)


if __name__ == "__main__":
    unittest.main()
