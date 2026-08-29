import asyncio
import contextlib
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import (  # noqa: E402
    Bridge,
    BridgeError,
    CgroupMemory,
    CodexResult,
    CodexRunner,
    MAX_FILE_BYTES,
    StateDB,
    TelegramClient,
    get_attachment,
    parse_command,
    normalize_legacy_codex_reply,
    sanitize_filename,
    split_message,
    read_cgroup_memory,
    iter_subprocess_lines,
)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.typing = 0
        self.files = {}
        self.photos = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id, image_path):
        self.photos.append((chat_id, image_path))

    async def send_typing(self, chat_id):
        self.typing += 1

    async def get_file(self, file_id):
        return {"file_path": self.files[file_id][0]}

    async def download_file(self, file_path, destination):
        for path, content in self.files.values():
            if path == file_path:
                destination.write_bytes(content)
                return
        raise AssertionError("unknown fake file")


class FakeRunner:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = []
        self.cancelled = False

    async def run(self, prompt, thread_id, image_path=None):
        self.calls.append((prompt, thread_id, image_path))
        return self.results.pop(0)

    async def cancel(self):
        self.cancelled = True
        return True


class HelperTests(unittest.TestCase):
    def test_oversized_subprocess_event_is_drained_and_skipped(self):
        async def collect():
            reader = asyncio.StreamReader(limit=16)
            reader.feed_data(b"x" * 40 + b"\n" + b"{\"ok\": true}\n")
            reader.feed_eof()
            return [line async for line in iter_subprocess_lines(reader)]

        self.assertEqual(asyncio.run(collect()), [b"{\"ok\": true}\n"])

    def test_reads_cgroup_v2_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proc = root / "proc-cgroup"
            group = root / "service"
            group.mkdir()
            proc.write_text("0::/service\n")
            (group / "memory.current").write_text("300\n")
            (group / "memory.stat").write_text("anon 20\nfile 250\n")
            self.assertEqual(
                read_cgroup_memory(proc, root), CgroupMemory(300, 20, 250)
            )

    def test_formats_utc_timestamp_with_milliseconds(self):
        from bridge import format_utc_timestamp

        self.assertEqual(format_utc_timestamp(0.125), "1970-01-01T00:00:00.125Z")

    def test_legacy_bridge_json_is_unwrapped_narrowly(self):
        legacy = json.dumps({
            "reply": "hello",
            "confirmation_required": False,
            "confirmation_summary": None,
        })
        self.assertEqual(normalize_legacy_codex_reply(legacy), ("hello", False, None))
        arbitrary = json.dumps({"reply": "data", "extra": True})
        self.assertEqual(normalize_legacy_codex_reply(arbitrary), (arbitrary, False, None))

    def test_sanitize_filename_blocks_traversal(self):
        self.assertEqual(sanitize_filename("../../hello world.txt"), "hello_world.txt")
        self.assertEqual(sanitize_filename("..."), "attachment")

    def test_split_message_respects_limit(self):
        text = "one two three\n" * 1000
        parts = split_message(text, 100)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < len(part) <= 100 for part in parts))
        self.assertEqual("".join(parts).replace("\n", "").replace(" ", ""), text.replace("\n", "").replace(" ", ""))

    def test_parse_command_strips_bot_name(self):
        self.assertEqual(parse_command("/STATUS@my_bot ignored"), "/status")
        self.assertIsNone(parse_command("hello"))

    def test_attachment_selection(self):
        message = {
            "photo": [
                {"file_id": "small", "file_size": 5},
                {"file_id": "large", "file_size": 10},
            ]
        }
        self.assertEqual(get_attachment(message)["file_id"], "large")
        document = get_attachment(
            {
                "document": {
                    "file_id": "doc",
                    "file_name": "x.png",
                    "mime_type": "image/png",
                }
            }
        )
        self.assertTrue(document["is_image"])


class StateTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateDB(Path(directory) / "state.sqlite3")
            self.assertIsNone(state.get_offset())
            state.set_offset(123)
            state.set_thread(7, "thread-1")
            state.set_pending_confirmation(7, "send message")
            self.assertEqual(state.get_offset(), 123)
            self.assertEqual(state.get_thread(7), "thread-1")
            self.assertEqual(state.get_pending_confirmation(7), "send message")
            state.set_thread(7, None)
            self.assertIsNone(state.get_thread(7))
            self.assertIsNone(state.get_pending_confirmation(7))
            state.close()


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.schema = root / "schema.json"
        self.schema.write_text("{}")
        self.runner = CodexRunner("/usr/local/bin/codex", Path("/home/alex"), self.schema)

    def tearDown(self):
        self.temp.cleanup()

    def test_argv_is_fixed_and_resumable(self):
        new = self.runner.build_argv(None)
        resumed = self.runner.build_argv("thread-123", Path("/tmp/image.png"))
        self.assertIn("workspace-write", new)
        self.assertIn("--search", new)
        self.assertIn("--skip-git-repo-check", new)
        self.assertNotIn("resume", new)
        self.assertIn("resume", resumed)
        self.assertIn("thread-123", resumed)
        self.assertIn("/tmp/image.png", resumed)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", new)

    def test_mcp_is_scoped_per_invocation_without_output_schema(self):
        root = Path(self.temp.name)
        runner = CodexRunner(
            "/usr/local/bin/codex", Path("/home/alex"), self.schema, 10,
            root / "payload.json", root / "bridge_payload_mcp.py",
        )
        argv = runner.build_argv(None)
        self.assertNotIn("--output-schema", argv)
        self.assertTrue(any("mcp_servers.telegram_bridge.command" in item for item in argv))
        self.assertTrue(any("bridge_payload_mcp.py" in item for item in argv))
        self.assertTrue(any("mcp_servers.telegram_bridge.env_vars" in item for item in argv))
        environment = runner._environment("turn-123")
        self.assertEqual(environment["TELEGRAM_BRIDGE_TURN_ID"], "turn-123")
        self.assertEqual(environment["TELEGRAM_BRIDGE_PAYLOAD_FILE"], str(root / "payload.json"))

    def test_environment_excludes_telegram_token(self):
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "secret", "PATH": "/usr/bin", "HOME": "/home/alex"},
            clear=True,
        ):
            environment = self.runner._environment()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        self.assertEqual(environment["HOME"], "/home/alex")

    async def test_real_subprocess_jsonl_parsing(self):
        root = Path(self.temp.name)
        executable = root / "fake-codex"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'thread.started','thread_id':'abc'}))\n"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'hello'}}))\n"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':3,'output_tokens':2}}))\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        runner = CodexRunner(str(executable), Path("/home/alex"), self.schema, 10)
        result = await runner.run("test", None)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "hello")
        self.assertEqual(result.thread_id, "abc")
        self.assertEqual(result.usage["output_tokens"], 2)

    async def test_subprocess_consumes_turn_scoped_payload(self):
        root = Path(self.temp.name)
        generated_parent = Path.home() / ".codex" / "generated_images"
        generated_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="runner-payload-", dir=generated_parent) as directory:
            image = Path(directory) / "result.png"
            image.write_bytes(b"png")
            executable = root / "fake-payload-codex"
            executable.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "sys.stdin.read()\n"
                "payload={"
                "'turn_id':os.environ['TELEGRAM_BRIDGE_TURN_ID'],"
                "'attachments':[{'kind':'photo','path':" + repr(str(image)) + "}],"
                "'confirmation':None}\n"
                "pathlib.Path(os.environ['TELEGRAM_BRIDGE_PAYLOAD_FILE']).write_text(json.dumps(payload))\n"
                "print(json.dumps({'type':'thread.started','thread_id':'abc'}))\n"
                "print(json.dumps({'type':'turn.completed','usage':{}}))\n"
            )
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            runner = CodexRunner(str(executable), Path("/home/alex"), self.schema, 10, root / "payload.json", root / "mcp.py")
            result = await runner.run("test", None)
            self.assertTrue(result.success)
            self.assertEqual(result.generated_images, (image.resolve(),))
            self.assertFalse((root / "payload.json").exists())

    async def test_incidental_image_path_is_not_transport_metadata(self):
        reader = asyncio.StreamReader()
        event = {"type": "item.completed", "item": {"type": "tool_call", "output": "saved to /home/alex/.codex/generated_images/example.png"}}
        reader.feed_data((json.dumps(event) + "\n").encode())
        reader.feed_data((json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}) + "\n").encode())
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "done")
        self.assertEqual(result.generated_images, ())

    async def test_no_final_message_can_be_completed_by_payload(self):
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps({"type": "turn.completed", "usage": {}}) + "\n").encode())
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "")

    async def test_plain_final_message_is_accepted(self):
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "nope"}}) + "\n").encode())
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "nope")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateDB(root / "state.sqlite3")
        self.telegram = FakeTelegram()
        self.runner = FakeRunner()
        self.bridge = Bridge(self.telegram, self.state, self.runner, 123456789, root / "uploads")
        self.bridge.upload_root.mkdir()

    async def asyncTearDown(self):
        if self.bridge.active_task and not self.bridge.active_task.done():
            await self.bridge.active_task
        self.state.close()
        self.temp.cleanup()

    def update(self, message, user=123456789, chat_type="private"):
        return {
            "update_id": 1,
            "message": {
                "message_id": 55,
                "from": {"id": user},
                "chat": {"id": 99, "type": chat_type},
                **message,
            },
        }

    async def test_unauthorized_user_is_silent(self):
        await self.bridge.handle_update(self.update({"text": "hello"}, user=123))
        self.assertEqual(self.telegram.messages, [])
        self.assertEqual(self.runner.calls, [])

    async def test_normal_turn_persists_thread(self):
        self.runner.results.append(CodexResult(True, "answer", thread_id="thread-a"))
        await self.bridge.handle_update(self.update({"text": "question"}))
        await self.bridge.active_task
        self.assertEqual(self.state.get_thread(99), "thread-a")
        self.assertEqual(self.telegram.messages[-1], (99, "answer"))

    async def test_message_gets_visible_acknowledgement(self):
        self.runner.results.append(CodexResult(True, "answer", thread_id="thread-a"))
        message = self.update({"text": "question", "date": int(time.time())})
        with self.assertLogs(level="INFO") as captured:
            await self.bridge.handle_update(message)
        self.assertEqual(self.telegram.messages[0], (99, "👍 Received — working."))
        logs = "\n".join(captured.output)
        self.assertIn("telegram message received update_id=1 message_id=55", logs)
        self.assertIn("telegram_sent_at=", logs)
        self.assertIn("received_at=", logs)
        self.assertIn("inbound_lag=", logs)
        self.assertIn("telegram acknowledgement sent message_id=55", logs)
        self.assertIn("request_duration=", logs)
        self.assertIn("since_receive=", logs)
        self.assertIn("telegram_to_ack=", logs)
        await self.bridge.active_task
        self.assertEqual(self.telegram.messages[-1], (99, "answer"))

    async def test_peek_reports_active_and_latest_turn(self):
        self.bridge._start_turn(99, {"message_id": 55, "text": "question"})
        await self.bridge.handle_command(99, "/peek")
        self.assertIn("Turn: working", self.telegram.messages[-1][1])
        self.bridge.active_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.bridge.active_task
        self.bridge._finish_turn("delivered")
        await self.bridge.handle_command(99, "/peek")
        self.assertIn("Last turn: completed", self.telegram.messages[-1][1])
        self.assertIn("Result: delivered", self.telegram.messages[-1][1])

    async def test_completed_turn_requests_recycle_above_file_cache_threshold(self):
        self.bridge.cache_recycle_bytes = 100
        self.bridge.cache_recycle_min_uptime_seconds = 0
        self.bridge.memory_reader = lambda: CgroupMemory(150, 10, 120)
        self.runner.results.append(CodexResult(True, "answer", thread_id="thread-a"))
        await self.bridge.handle_update(self.update({"text": "question"}))
        await self.bridge.active_task
        await asyncio.sleep(0)
        self.assertTrue(self.bridge.recycle_event.is_set())
        self.assertEqual(self.telegram.messages[-1], (99, "answer"))

    async def test_status_reports_cgroup_memory_breakdown(self):
        self.bridge.memory_reader = lambda: CgroupMemory(
            3 * 1024**3, 32 * 1024**2, 2 * 1024**3
        )
        await self.bridge.handle_update(self.update({"text": "/status"}))
        status = self.telegram.messages[-1][1]
        self.assertIn("Bridge memory: 3.0 GiB", status)
        self.assertIn("file cache 2.0 GiB", status)

    async def test_generated_image_is_sent_without_empty_message(self):
        image = Path.home() / ".codex" / "generated_images" / "result.png"
        self.runner.results.append(CodexResult(True, "", thread_id="thread-a", generated_images=(image,)))
        await self.bridge.handle_update(self.update({"text": "make an image"}))
        await self.bridge.active_task
        self.assertEqual(self.telegram.photos, [(99, image)])
        self.assertEqual(self.telegram.messages, [(99, "👍 Received — working.")])

    async def test_confirmation_then_confirm(self):
        self.runner.results.extend(
            [
                CodexResult(
                    True,
                    "Ready to send.",
                    thread_id="thread-a",
                    confirmation_required=True,
                    confirmation_summary="Send email draft 7 to Pat",
                ),
                CodexResult(True, "Sent.", thread_id="thread-a"),
            ]
        )
        await self.bridge.handle_update(self.update({"text": "send the email"}))
        await self.bridge.active_task
        self.assertEqual(
            self.state.get_pending_confirmation(99), "Send email draft 7 to Pat"
        )
        await self.bridge.handle_update(self.update({"text": "/confirm"}))
        await self.bridge.active_task
        self.assertIn("authorized /confirm turn", self.runner.calls[-1][0])
        self.assertIsNone(self.state.get_pending_confirmation(99))

    async def test_new_message_discards_stale_confirmation(self):
        self.state.set_pending_confirmation(99, "old action")
        self.runner.results.append(CodexResult(True, "answer", thread_id="thread-a"))
        await self.bridge.handle_update(self.update({"text": "different question"}))
        await self.bridge.active_task
        self.assertIsNone(self.state.get_pending_confirmation(99))

    async def test_document_download_and_cleanup(self):
        self.telegram.files["file-1"] = ("docs/report.txt", b"hello")
        self.runner.results.append(CodexResult(True, "read", thread_id="thread-a"))
        await self.bridge.handle_update(
            self.update(
                {
                    "caption": "review this",
                    "document": {
                        "file_id": "file-1",
                        "file_name": "../../report.txt",
                        "file_size": 5,
                        "mime_type": "text/plain",
                    },
                }
            )
        )
        await self.bridge.active_task
        self.assertIn("report.txt", self.runner.calls[0][0])
        self.assertEqual(list(self.bridge.upload_root.iterdir()), [])

    async def test_oversized_attachment_is_rejected(self):
        await self.bridge.handle_update(
            self.update(
                {
                    "document": {
                        "file_id": "large",
                        "file_name": "large.bin",
                        "file_size": MAX_FILE_BYTES + 1,
                    }
                }
            )
        )
        await self.bridge.active_task
        self.assertIn("20 MB", self.telegram.messages[-1][1])
        self.assertEqual(self.runner.calls, [])

    async def test_cancel_command(self):
        await self.bridge.handle_command(99, "/cancel")
        self.assertTrue(self.runner.cancelled)
        self.assertEqual(self.telegram.messages[-1], (99, "Cancellation requested."))


class TelegramDownloadTests(unittest.TestCase):
    def test_file_url_encodes_path_without_exposing_in_logs(self):
        client = TelegramClient("token", "https://example.test")
        self.assertEqual(
            client._file_url("dir/a b.txt"),
            "https://example.test/file/bottoken/dir/a%20b.txt",
        )


if __name__ == "__main__":
    unittest.main()
