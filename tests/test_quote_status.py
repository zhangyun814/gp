import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from app.main import _fetch_quotes_with_retry, latest_quote_checkpoint


class QuoteStatusTest(unittest.TestCase):
    def test_reads_latest_valid_full_market_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "quote-sync-all-2026.json").write_text(
                json.dumps({"completed_codes": ["000001", "000002"], "total": 5565}),
                encoding="utf-8",
            )

            result = latest_quote_checkpoint(data_dir)

            self.assertEqual(result["completed_codes"], ["000001", "000002"])
            self.assertEqual(result["total"], 5565)
            self.assertEqual(result["state_file"], "quote-sync-all-2026.json")

    def test_ignores_invalid_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "quote-sync-all-broken.json").write_text("not-json", encoding="utf-8")

            self.assertIsNone(latest_quote_checkpoint(data_dir))

    @patch("app.main.time.sleep")
    @patch("app.main.fetch_akshare_quotes")
    def test_single_stock_is_retried_before_failure(self, fetch, sleep):
        fetch.side_effect = [RuntimeError("temporary"), [{"date": "2026-09-18"}]]

        rows = _fetch_quotes_with_retry("600519", date(2026, 9, 18), date(2026, 9, 18), "qfq")

        self.assertEqual(rows, [{"date": "2026-09-18"}])
        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
