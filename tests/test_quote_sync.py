import unittest

from scripts.sync_quotes import next_batch, state_for_run


class QuoteSyncTest(unittest.TestCase):
    def test_next_batch_skips_completed_codes(self):
        self.assertEqual(next_batch(["000001", "000002", "000003"], {"000001"}, 2), ["000002", "000003"])

    def test_next_batch_skips_deferred_codes_for_current_run(self):
        self.assertEqual(next_batch(["000001", "000002", "000003"], set(), 2, {"000001"}),
                         ["000002", "000003"])

    def test_changed_date_or_mode_starts_a_new_checkpoint(self):
        state = {"completed_codes": ["000001"], "start_date": "2026-01-01",
                 "end_date": "2026-09-16", "all_stocks": False}
        self.assertEqual(state_for_run(state, "2026-01-01", "2026-09-17", True),
                         {"completed_codes": []})


if __name__ == "__main__":
    unittest.main()
