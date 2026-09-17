import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from app.zsxq import normalize_response, parse_cli_json, parse_time, response_next_end_time
from scripts.sync_zsxq import backfill_year, fetch_page, payload_shape, topics_in_month


class ZsxqParserTest(unittest.TestCase):
    def test_parse_time_with_compact_offset(self):
        self.assertEqual(parse_time("2026-09-16T10:52:21.712+0800").isoformat(), "2026-09-16T02:52:21.712000+00:00")

    def test_normalize_topic_payload(self):
        payload = {
            "succeeded": True,
            "resp_data": {"topics": [{
                "topic_id": 123,
                "group": {"group_id": 28888114545551, "name": "星辰财经"},
                "title": "订单增长",
                "talk": {"owner": {"name": "作者"}, "text": "订单增长，关注 600519 贵州茅台"},
                "create_time": "2026-09-16T10:52:21.712+0800",
            }]},
        }
        topics, has_more = normalize_response(payload)
        self.assertFalse(has_more)
        self.assertEqual(topics[0]["topic_id"], "123")
        self.assertEqual(topics[0]["author"], "作者")
        self.assertEqual(topics[0]["published_at"], "2026-09-16T02:52:21.712000+00:00")

    def test_cli_output_with_log_prefix(self):
        self.assertEqual(parse_cli_json("info\n{\"succeeded\":true}"), {"succeeded": True})

    def test_normalize_official_cli_envelope(self):
        payload = {
            "ok": True,
            "data": {"topics": [{
                "topic_id": "cli-topic",
                "talk": {"text": "关注订单", "owner": {"name": "作者"}},
                "create_time": "2026-09-16T10:52:21.712+0800",
            }]},
        }
        topics, has_more = normalize_response(payload, page_size=1)
        self.assertEqual(topics[0]["topic_id"], "cli-topic")
        self.assertTrue(has_more)

    def test_normalize_mcp_text_envelope_and_cursor(self):
        payload = {
            "ok": True,
            "result": {"content": [{"type": "text", "text": json.dumps({
                "items": [{
                    "topic_id": "wrapped-topic",
                    "talk": {"text": "订单", "owner": {"name": "作者"}},
                    "create_time": "2026-09-16T10:52:21.712+0800",
                }],
                "has_more": True,
                "next_end_time": "2026-09-16T10:52:21.712+0800",
            })}]},
        }
        topics, has_more = normalize_response(payload)
        self.assertEqual(topics[0]["topic_id"], "wrapped-topic")
        self.assertTrue(has_more)
        self.assertEqual(response_next_end_time(payload), "2026-09-16T10:52:21.712+0800")

    def test_normalize_topics_brief_official_cli_envelope(self):
        payload = {
            "count": 1,
            "has_more": True,
            "next_end_time": "2026-09-16T10:52:21.712+0800",
            "success": True,
            "topics_brief": [{
                "topic_id": "brief-topic",
                "content": "订单增长",
                "owner": {"name": "作者"},
                "create_time": "2026-09-16T10:52:21.712+0800",
            }],
        }
        topics, has_more = normalize_response(payload, page_size=1)
        self.assertEqual(topics[0]["topic_id"], "brief-topic")
        self.assertEqual(topics[0]["content"], "订单增长")
        self.assertEqual(topics[0]["author"], "作者")
        self.assertTrue(has_more)

    @patch("scripts.sync_zsxq.subprocess.run")
    def test_fetch_page_uses_supported_group_topics_command(self, run):
        run.return_value = CompletedProcess(
            args=[], returncode=0, stdout='{"ok": true, "data": {"topics": []}}', stderr=""
        )
        self.assertEqual(fetch_page("zsxq-cli", "123", 20, "2026-09-16T10:00:00+0800")["ok"], True)
        run.assert_called_once_with(
            ["zsxq-cli", "group", "+topics", "--group-id", "123", "--limit", "20", "--json",
             "--end-time", "2026-09-16T10:00:00+0800"],
            capture_output=True, text=True, check=False,
        )

    def test_payload_shape_never_includes_topic_text(self):
        shape = payload_shape({"data": {"topics": [{"topic_id": "1", "talk": {"text": "私密正文"}}]}})
        self.assertEqual(shape["data"]["topics"]["list_length"], 1)
        self.assertNotIn("私密正文", str(shape))

    @patch("scripts.sync_zsxq.post_topics")
    @patch("scripts.sync_zsxq.fetch_page")
    def test_backfill_saves_cursor_after_each_page(self, fetch, post):
        fetch.return_value = {"has_more": True, "next_end_time": "2026-09-15T00:00:00+0800", "topics_brief": [{
            "topic_id": "sep-topic", "content": "正文", "create_time": "2026-09-15T09:00:00+0800", "owner": {"name": "作者"},
        }]}
        post.return_value = {"imported": 1, "skipped": 0}
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "backfill.json"
            args = Namespace(backfill_year=2026, state_file=str(state_file), pages=1, count=30,
                             cli="zsxq-cli", group_id="group", scope="all", app_url="http://app",
                             dry_run=False, debug=False, backfill_month=None)
            result = backfill_year(args)
            state = json.loads(state_file.read_text())
        self.assertEqual(result["imported"], 1)
        self.assertEqual(state["current_month"], "2026-09")
        self.assertEqual(state["end_time"], "2026-09-15T00:00:00+0800")
        self.assertEqual(post.call_args.args[3][0]["topic_id"], "sep-topic")

    def test_topics_in_month_excludes_other_months(self):
        september, october = parse_time("2026-09-01T00:00:00+0800"), parse_time("2026-10-01T00:00:00+0800")
        topics = [{"topic_id": "sep", "published_at": "2026-09-30T23:59:59+0800"},
                  {"topic_id": "aug", "published_at": "2026-08-31T23:59:59+0800"}]
        self.assertEqual([topic["topic_id"] for topic in topics_in_month(topics, september, october)], ["sep"])

    def test_month_keys_can_limit_to_one_month(self):
        from scripts.sync_zsxq import month_keys
        self.assertEqual(month_keys(2026, "2026-09"), ["2026-09"])


if __name__ == "__main__":
    unittest.main()
