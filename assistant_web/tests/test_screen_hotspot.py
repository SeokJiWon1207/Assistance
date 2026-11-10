"""팀 대시보드 화면번호 탭 집계 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import screen_hotspot


TODAY = date(2026, 8, 27)  # 목요일, 이번 주는 8/24부터


def _issue(issue_id, created_at, screen="7012", **fields):
    return {
        "id": issue_id,
        "summary": f"이슈 {issue_id}",
        "project": "신한",
        "screen_number": screen,
        "media_type": "HTS",
        "status": "confirmed",
        "handler": "석지원",
        "created_at": created_at,
        **fields,
    }


class ScreenHotspotTests(unittest.TestCase):
    def setUp(self):
        # 7012: 직전 4주(7/20~8/16) 각 1건, 지난주(8/17~) 4건, 이번 주 1건
        self.issues = [
            _issue(1, "2026-07-20"), _issue(2, "2026-07-28"), _issue(3, "2026-08-04"), _issue(4, "2026-08-12"),
            _issue(5, "2026-08-17", project="유진"), _issue(6, "2026-08-18", status="closed"),
            _issue(7, "2026-08-19", media_type="MTS"), _issue(8, "2026-08-21"),
            _issue(9, "2026-08-25"),
            _issue(10, "2026-08-18", screen="0150"), _issue(11, "2026-08-19", screen="0150"),
            _issue(12, "2026-08-20", screen="미상"),
            _issue(13, "2026-06-01"),  # 8주 밖
            _issue(14, "2026-08-20", handler="다른팀"),
        ]
        self.playbooks = [{"id": "pb", "title": "주문 지연", "sourceIssueIds": [5, 99], "updatedAt": "2026-08-01"}]

    def _build(self, *, complete=True, **filters):
        source = {"status": "snapshot", "search_complete": complete}
        with patch.object(screen_hotspot, "_load_issues", return_value=(self.issues, source)):
            return screen_hotspot.build_screen_hotspot(
                today=TODAY, playbook_loader=lambda **_: self.playbooks, **filters,
            )

    def test_weekly_grid_ranks_team_screens_inside_eight_weeks(self):
        result = self._build()
        self.assertEqual([week["start"] for week in result["weeks"]][0], "2026-07-06")
        self.assertTrue(result["weeks"][-1]["partial"])
        self.assertEqual([row["screen"] for row in result["rows"]], ["7012", "0150"])
        top = result["rows"][0]
        self.assertEqual(top["weeks"], [0, 0, 1, 1, 1, 1, 4, 1])
        self.assertEqual(top["total"], 9)
        self.assertEqual(top["openCount"], 8)
        self.assertEqual(top["customers"][0], {"customer": "신한", "name": top["customers"][0]["name"], "count": 8})
        self.assertEqual(top["recentIssues"][0]["id"], 9)
        self.assertEqual([playbook["id"] for playbook in top["playbooks"]], ["pb"])
        self.assertEqual(result["summary"], {"issueCount": 12, "screenCount": 2, "unknownScreenCount": 1})

    def test_spike_compares_last_full_week_with_previous_four_weeks(self):
        spikes = self._build()["spikes"]
        self.assertEqual(spikes[0], {"screen": "7012", "count": 4, "baselineAverage": 1.0, "ratio": 4.0})
        self.assertEqual(spikes[1]["screen"], "0150")
        self.assertIsNone(spikes[1]["ratio"])
        self.assertEqual(self._build(complete=False)["spikes"], [])

    def test_shared_accounts_are_counted_under_their_screen(self):
        self.issues = [_issue(1, "2026-08-25", handler="cs이슈"), _issue(2, "2026-08-26", handler="csissue"),
                       _issue(3, "2026-08-27", handler="다른팀")]
        result = self._build(media="HTS", customer="신한")
        self.assertEqual(result["summary"]["issueCount"], 2)
        self.assertEqual(result["rows"][0]["weeks"][-1], 2)
        self.assertEqual([item["id"] for item in result["rows"][0]["recentIssues"]], [2, 1])

    def test_media_and_customer_filters_narrow_rows_but_keep_customer_options(self):
        mts = self._build(media="mts")
        self.assertEqual(mts["meta"]["media"], "MTS")
        self.assertEqual([(row["screen"], row["total"]) for row in mts["rows"]], [("7012", 1)])
        eugene = self._build(customer="유진")
        self.assertEqual([(row["screen"], row["total"]) for row in eugene["rows"]], [("7012", 1)])
        # 고객사 선택 목록은 설정된 이름만, 설정 순서대로 보인다.
        from backend.settings.catalogs import get_customer_picker
        self.assertEqual([option["customer"] for option in eugene["options"]["customers"]], list(get_customer_picker()))
        self.assertEqual(eugene["options"]["customers"][0], {"customer": "교보증권", "name": "교보증권"})
        # 목록 이름으로 고르면 그 고객사의 프로젝트가 모두 걸린다.
        picked = self._build(customer="유진투자증권")
        self.assertEqual([(row["screen"], row["total"]) for row in picked["rows"]], [("7012", 1)])
        self.assertEqual(picked["meta"]["customerName"], "유진투자증권")
        shinhan = self._build(customer="신한투자증권")
        self.assertEqual(shinhan["rows"][0]["total"], 8)
        with self.assertRaises(ValueError):
            self._build(media="WEB")


if __name__ == "__main__":
    unittest.main()
