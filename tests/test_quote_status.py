import json
import tempfile
import unittest
from pathlib import Path

from app.main import latest_quote_checkpoint


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


if __name__ == "__main__":
    unittest.main()
