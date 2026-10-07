"""답변 본문의 사용자용 표기(고객사·상태·날짜·시각)."""

import unittest
from datetime import date

from backend import retrieval
from backend.display import customer_name, korean_day, korean_day_range, korean_time, status_name

TODAY = date(2026, 10, 7)


class DisplayNameTests(unittest.TestCase):
    def test_mantis_project_names_read_as_customer_names(self):
        self.assertEqual(customer_name("112KBStar(KB증권)"), "KB증권")
        self.assertEqual(customer_name("117Hantu_Project"), "한국투자증권")
        self.assertEqual(customer_name("138Meritz(메리츠)"), "메리츠")
        # 고객사로 등록되지 않은 내부 프로젝트는 원래 이름을 그대로 둔다.
        self.assertEqual(customer_name("900DRFN(공용)"), "900DRFN(공용)")
        self.assertEqual(customer_name(""), "고객사 미상")

    def test_internal_status_values_read_as_labels(self):
        for raw in ("confirmed", "developed", "closed", "assigned"):
            with self.subTest(raw=raw):
                self.assertNotEqual(status_name(raw), raw)
                self.assertRegex(status_name(raw), r"[가-힣]")

    def test_dates_and_times_read_in_korean(self):
        self.assertEqual(korean_day("2026-10-07", TODAY), "10월 7일")
        self.assertEqual(korean_day("2020-10-22", TODAY), "2020년 10월 22일")
        self.assertEqual(korean_day("미상", TODAY), "미상")
        self.assertEqual(korean_day_range("2026-10-05", "2026-10-11", TODAY), "10월 5일~11일")
        self.assertEqual(korean_day_range("2026-09-28", "2026-10-04", TODAY), "9월 28일~10월 4일")
        # 저장 시각은 UTC로 들어오므로 한국 시간으로 바꿔 보인다.
        self.assertEqual(korean_time("2026-10-07T05:35:39.824841+00:00", TODAY), "10월 7일 오후 2:35")
        self.assertEqual(korean_time("2026-10-06T23:05:00+00:00", TODAY), "10월 7일 오전 8:05")

    def test_stored_data_note_has_no_raw_timestamp(self):
        note = retrieval.build_snapshot_note({"stages": [{
            "source": "mantis_page_scan", "status": "ok",
            "meta": {"status": "snapshot", "fetchedAt": "2026-10-07T05:35:39.824841+00:00"},
        }]})
        self.assertIn("오후 2:35에 저장한 Mantis 원본", note)
        self.assertNotIn("T05:35", note)


if __name__ == "__main__":
    unittest.main()
