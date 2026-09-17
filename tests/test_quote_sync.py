import unittest

from scripts.sync_quotes import next_batch


class QuoteSyncTest(unittest.TestCase):
    def test_next_batch_skips_completed_codes(self):
        self.assertEqual(next_batch(["000001", "000002", "000003"], {"000001"}, 2), ["000002", "000003"])


if __name__ == "__main__":
    unittest.main()
