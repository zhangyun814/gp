import json
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from app.zsxq import normalize_response, parse_cli_json, parse_time, response_next_end_time
from scripts.sync_zsxq import fetch_page, payload_shape


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


if __name__ == "__main__":
    unittest.main()
