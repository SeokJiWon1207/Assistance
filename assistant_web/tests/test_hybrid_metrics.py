import unittest

from scripts.accuracy.verify_hybrid_retrieval import ranking_metrics


class HybridMetricsTests(unittest.TestCase):
    def test_rank_metrics_use_known_targets_and_do_not_count_duplicate_chunks(self):
        self.assertEqual(ranking_metrics([3, 3, 4, 7], [7]),
                         {"hit_at_5": 1, "mrr_at_5": 1 / 3, "target_rank": 3, "top_ids": [3, 4, 7]})
        self.assertEqual(ranking_metrics([1, 2, 3, 4, 5, 6], [6])["hit_at_5"], 0)
        self.assertEqual(ranking_metrics([], [1])["mrr_at_5"], 0)
