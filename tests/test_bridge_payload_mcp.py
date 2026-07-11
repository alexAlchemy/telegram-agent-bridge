import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import BridgeError, consume_bridge_payload
from bridge_payload_mcp import handle, write_payload


class PayloadMcpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.payload_file = Path(self.temp.name) / "payload.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_mcp_lists_tool_and_atomically_writes_payload(self):
        listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(listed["result"]["tools"][0]["name"], "set_payload")
        with mock.patch.dict(os.environ, {
            "TELEGRAM_BRIDGE_TURN_ID": "turn-a",
            "TELEGRAM_BRIDGE_PAYLOAD_FILE": str(self.payload_file),
        }, clear=True):
            write_payload({
                "attachments": [{"kind": "photo", "path": "/tmp/example.png"}],
                "confirmation": {"required": True, "summary": "Send one message"},
            })
        payload = json.loads(self.payload_file.read_text())
        self.assertEqual(payload["turn_id"], "turn-a")
        self.assertEqual(len(payload["attachments"]), 1)
        self.assertEqual(self.payload_file.stat().st_mode & 0o777, 0o600)

    def test_consumer_accepts_multiple_trusted_images_once(self):
        parent = Path.home() / ".codex" / "generated_images"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="payload-test-", dir=parent) as directory:
            first = Path(directory) / "one.png"
            second = Path(directory) / "two.webp"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            self.payload_file.write_text(json.dumps({
                "turn_id": "turn-b",
                "attachments": [
                    {"kind": "photo", "path": str(first)},
                    {"kind": "photo", "path": str(second)},
                ],
                "confirmation": None,
            }))
            images, required, summary = consume_bridge_payload(self.payload_file, "turn-b")
            self.assertEqual(images, (first.resolve(), second.resolve()))
            self.assertFalse(required)
            self.assertIsNone(summary)
            self.assertFalse(self.payload_file.exists())

    def test_consumer_rejects_stale_turn_and_deletes_payload(self):
        self.payload_file.write_text(json.dumps({
            "turn_id": "old-turn", "attachments": [], "confirmation": None
        }))
        with self.assertRaises(BridgeError):
            consume_bridge_payload(self.payload_file, "new-turn")
        self.assertFalse(self.payload_file.exists())

    def test_consumer_rejects_untrusted_image(self):
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"image")
        self.payload_file.write_text(json.dumps({
            "turn_id": "turn-c",
            "attachments": [{"kind": "photo", "path": str(outside)}],
            "confirmation": None,
        }))
        with self.assertRaises(BridgeError):
            consume_bridge_payload(self.payload_file, "turn-c")


if __name__ == "__main__":
    unittest.main()
