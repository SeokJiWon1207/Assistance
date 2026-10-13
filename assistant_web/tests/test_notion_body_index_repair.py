"""후속 작업은 기존 수집과 겹치지 않고 복구 대상만 선택한다."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import repair_notion_body_index as repair


class RepairTests(unittest.TestCase):
    def test_waits_until_all_prior_processes_exit(self):
        with patch.object(repair, "alive", side_effect=[True, False, True, False, False]) as alive, \
                patch.object(repair.time, "sleep") as sleep:
            repair.wait_for_readers([11, 12], repair.time.monotonic() + 60)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(alive.call_args_list[-1].args, (12,))

    def test_wait_timeout_does_not_start_api(self):
        with patch.object(repair, "alive", return_value=True), \
                patch.object(repair.time, "sleep") as sleep:
            with self.assertRaises(TimeoutError):
                repair.wait_for_readers([11], repair.time.monotonic() - 1)
        sleep.assert_not_called()

    def test_second_repair_cannot_acquire_same_lock(self):
        with tempfile.TemporaryDirectory() as folder, repair.repair_lock(Path(folder)):
            with self.assertRaises(BlockingIOError), repair.repair_lock(Path(folder)):
                self.fail("duplicate repair admitted")

    def test_only_recoverable_or_undiagnosed_rows_selected(self):
        def row(reason, current=True):
            return {"last_error": reason, "content_policy_current": current}
        self.assertEqual(repair.repair_priority(row("http_429")), 0)
        self.assertEqual(repair.repair_priority(row("http_503")), 0)
        self.assertEqual(repair.repair_priority(row("foreign_synced_block,unsupported_block")), 1)
        self.assertEqual(repair.repair_priority(row("unsupported_block")), 2)
        self.assertEqual(repair.repair_priority(row("", False)), 3)
        self.assertIsNone(repair.repair_priority(row("http_404")))
        self.assertIsNone(repair.repair_priority(row("")))
        self.assertIsNone(repair.repair_priority(row("block_parent_mismatch")))
