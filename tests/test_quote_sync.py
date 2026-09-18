import io
import unittest
from urllib.error import URLError
from unittest.mock import patch

from scripts.sync_quotes import next_batch, read_json, state_for_run


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

    @patch("scripts.sync_quotes.time.sleep")
    @patch("scripts.sync_quotes.urlopen")
    def test_http_request_retries_transient_failure(self, urlopen, sleep):
        urlopen.side_effect = [URLError("temporary"), io.BytesIO(b'{"ok": true}')]

        self.assertEqual(read_json("http://127.0.0.1/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
