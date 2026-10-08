import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_bridge import (  # noqa: E402
    AgentBridge,
    ClaudeRunner,
    GrokRunner,
    extract_grok_image_paths,
    load_instance_config,
    register_commands,
    send_startup_notification,
)
from bridge import CodexResult, MAX_OUTBOUND_IMAGE_BYTES, StateDB  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def request(self, method, payload=None):
        self.last_request = (method, payload)
        return True

    async def send_typing(self, chat_id):
        return None


class FakeRunner:
    workspace = Path("/home/alex/code/telegram-narrator")

    async def cancel(self):
        return False

    async def run(self, prompt, thread_id, image_path=None):
        return CodexResult(True, "ok", thread_id="session-1")


class StartupNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_registration_includes_peek(self):
        telegram = FakeTelegram()
        await register_commands(telegram, "codex")
        method, payload = telegram.last_request
        self.assertEqual(method, "setMyCommands")
        self.assertIn("peek", {item["command"] for item in payload["commands"]})

    async def test_backend_online_message(self):
        telegram = FakeTelegram()
        await send_startup_notification(telegram, 99, "codex")
        self.assertEqual(telegram.messages, [(99, "Codex bridge is online.")])


class GrokRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.schema = self.root / "schema.json"
        self.schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "reply": {"type": "string"},
                        "confirmation_required": {"type": "boolean"},
                        "confirmation_summary": {"type": ["string", "null"]},
                    },
                    "required": [
                        "reply",
                        "confirmation_required",
                        "confirmation_summary",
                    ],
                    "additionalProperties": False,
                }
            )
        )
        self.runner = GrokRunner(
            "/home/alex/.local/bin/grok",
            Path("/home/alex"),
            self.schema,
            self.root / "prompts",
            timeout_seconds=10,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_argv_uses_approved_workspace_profile_and_resume(self):
        prompt = self.root / "prompt.txt"
        argv = self.runner.build_argv(prompt, "session-123")
        self.assertIn("streaming-json", argv)
        self.assertIn("workspace", argv)
        self.assertIn("--always-approve", argv)
        self.assertIn("--resume", argv)
        self.assertIn("session-123", argv)
        self.assertNotIn("dontAsk", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_environment_excludes_bot_and_provider_secrets(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "telegram-secret",
                "XAI_API_KEY": "provider-secret",
                "HOME": "/home/alex",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            environment = self.runner._environment()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        self.assertNotIn("XAI_API_KEY", environment)

    async def test_streaming_end_event_is_normalized(self):
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps(
                    {
                        "type": "end",
                        "stopReason": "EndTurn",
                        "sessionId": "grok-session",
                        "structuredOutput": {
                            "reply": "hello",
                            "confirmation_required": False,
                            "confirmation_summary": None,
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "hello")
        self.assertEqual(result.thread_id, "grok-session")

    async def test_streaming_reply_harvests_relative_session_image(self):
        session_root = self.root / "sessions"
        image = session_root / "workspace" / "grok-session" / "images" / "1.jpg"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"jpeg")
        reader = asyncio.StreamReader()
        reader.feed_data(
            (
                json.dumps(
                    {
                        "type": "end",
                        "stopReason": "EndTurn",
                        "sessionId": "grok-session",
                        "structuredOutput": {
                            "reply": "Created images/1.jpg",
                            "confirmation_required": False,
                            "confirmation_summary": None,
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        reader.feed_eof()
        self.runner.workspace_key = "workspace"
        with mock.patch("agent_bridge.GROK_SESSION_ROOT", session_root):
            result = await self.runner._read_stdout(reader)
        self.assertEqual(result.generated_images, (image.resolve(),))

    def test_image_extractor_rejects_escape_and_oversize(self):
        session_root = self.root / "sessions"
        images = session_root / "workspace" / "session" / "images"
        images.mkdir(parents=True)
        valid = images / "valid.webp"
        valid.write_bytes(b"image")
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        (images / "escape.jpg").symlink_to(outside)
        oversized = images / "large.png"
        with oversized.open("wb") as output:
            output.truncate(MAX_OUTBOUND_IMAGE_BYTES + 1)
        value = "images/valid.webp images/escape.jpg images/large.png"
        self.assertEqual(
            extract_grok_image_paths(
                value,
                session_id="session",
                session_root=session_root,
                workspace_key="workspace",
            ),
            [valid.resolve()],
        )

    async def test_fake_grok_subprocess_and_prompt_cleanup(self):
        executable = self.root / "fake-grok"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "payload={'reply':'done','confirmation_required':False,'confirmation_summary':None}\n"
            "print(json.dumps({'type':'end','stopReason':'EndTurn','sessionId':'s-1','structuredOutput':payload}))\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        runner = GrokRunner(
            str(executable), Path("/home/alex"), self.schema, self.root / "prompts", 10
        )
        result = await runner.run("secret prompt text", None)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "done")
        self.assertEqual(list((self.root / "prompts").iterdir()), [])


class ClaudeRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.schema = self.root / "schema.json"
        self.schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "reply": {"type": "string"},
                        "confirmation_required": {"type": "boolean"},
                        "confirmation_summary": {"type": ["string", "null"]},
                    },
                    "required": [
                        "reply",
                        "confirmation_required",
                        "confirmation_summary",
                    ],
                    "additionalProperties": False,
                }
            )
        )
        self.runner = ClaudeRunner(
            "/home/alex/.local/bin/claude",
            Path("/home/alex"),
            self.schema,
            self.root / "prompts",
            timeout_seconds=10,
        )

    def tearDown(self):
        self.temp.cleanup()

    def _result_line(self, **overrides):
        event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": "claude-session",
            "structured_output": {
                "reply": "hello",
                "confirmation_required": False,
                "confirmation_summary": None,
            },
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }
        event.update(overrides)
        return (json.dumps(event) + "\n").encode()

    def test_argv_is_non_interactive_and_resumes_session(self):
        argv = self.runner.build_argv(None)
        self.assertIn("-p", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("bypassPermissions", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--max-turns", argv)
        resumed = self.runner.build_argv("claude-session")
        self.assertEqual(resumed[resumed.index("--resume") + 1], "claude-session")

    def test_environment_excludes_bot_and_provider_secrets(self):
        with mock.patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "telegram-secret",
                "ANTHROPIC_API_KEY": "provider-secret",
                "HOME": "/home/alex",
                "PATH": "/usr/bin",
            },
            clear=True,
        ):
            environment = self.runner._environment()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertEqual(environment["HOME"], "/home/alex")

    async def test_result_event_is_normalized(self):
        reader = asyncio.StreamReader()
        reader.feed_data(self._result_line())
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "hello")
        self.assertEqual(result.thread_id, "claude-session")
        self.assertEqual(result.usage, {"input_tokens": 3, "output_tokens": 5})

    async def test_error_result_is_a_failed_turn_and_keeps_session(self):
        reader = asyncio.StreamReader()
        reader.feed_data(self._result_line(is_error=True, subtype="error_max_turns"))
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertFalse(result.success)
        self.assertEqual(result.thread_id, "claude-session")

    async def test_missing_result_and_malformed_reply_fail_closed(self):
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"type": "assistant"}\n')
        reader.feed_eof()
        self.assertFalse((await self.runner._read_stdout(reader)).success)

        reader = asyncio.StreamReader()
        reader.feed_data(self._result_line(structured_output={"reply": 7}))
        reader.feed_eof()
        result = await self.runner._read_stdout(reader)
        self.assertFalse(result.success)
        self.assertIn("invalid structured reply", result.error)

    async def test_fake_claude_receives_prompt_on_stdin_and_cleans_up(self):
        executable = self.root / "fake-claude"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "prompt = sys.stdin.read()\n"
            "payload = {'reply': 'got: ' + prompt, 'confirmation_required': False,"
            " 'confirmation_summary': None}\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
            " 'session_id': 's-1', 'structured_output': payload}))\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        runner = ClaudeRunner(
            str(executable), Path("/home/alex"), self.schema, self.root / "prompts", 10
        )
        result = await runner.run("secret prompt text", None)
        self.assertTrue(result.success)
        self.assertEqual(result.reply, "got: secret prompt text")
        self.assertEqual(list((self.root / "prompts").iterdir()), [])

    async def test_timing_reports_cli_metrics_and_startup(self):
        executable = self.root / "fake-claude-timed"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's-2'}))\n"
            "payload = {'reply': 'ok', 'confirmation_required': False, 'confirmation_summary': None}\n"
            "print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,"
            " 'session_id': 's-2', 'structured_output': payload, 'duration_ms': 123,"
            " 'duration_api_ms': 99, 'ttft_ms': 40, 'first_content_frame_ms': 30,"
            " 'num_turns': 1}))\n"
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        runner = ClaudeRunner(
            str(executable), Path("/home/alex"), self.schema, self.root / "prompts", 10
        )
        result = await runner.run("hi", None)
        self.assertTrue(result.success)
        timing = result.timing
        self.assertEqual(timing["cli_ms"], 123)
        self.assertEqual(timing["api_ms"], 99)
        self.assertEqual(timing["ttft_ms"], 40)
        self.assertEqual(timing["num_turns"], 1)
        self.assertIsNotNone(timing["init_ms"])
        self.assertGreaterEqual(timing["spawn_ms"], 0)
        self.assertIsInstance(timing["cli_overhead_ms"], int)


class AgentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = StateDB(root / "state.sqlite3")
        self.telegram = FakeTelegram()
        self.bridge = AgentBridge(
            self.telegram,
            self.state,
            FakeRunner(),
            123456789,
            root / "uploads",
            backend_name="grok",
        )

    async def asyncTearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_status_and_new_name_the_backend(self):
        await self.bridge.handle_command(99, "/status")
        self.assertIn("Backend: grok", self.telegram.messages[-1][1])
        await self.bridge.handle_command(99, "/new")
        self.assertIn("Grok", self.telegram.messages[-1][1])

    async def test_turn_timing_is_one_json_line_without_content(self):
        class TimedRunner(FakeRunner):
            async def run(self, prompt, thread_id, image_path=None):
                return CodexResult(
                    True, "secret reply", thread_id="s-3", timing={"cli_ms": 5}
                )

        self.bridge.runner = TimedRunner()
        with self.assertLogs(level="INFO") as logs:
            await self.bridge.process_turn(99, {"text": "private question"}, False)
        line = next(entry for entry in logs.output if "turn_timing" in entry)
        payload = json.loads(line.split("turn_timing ", 1)[1])
        self.assertEqual(payload["outcome"], "delivered")
        self.assertEqual(payload["cli_ms"], 5)
        self.assertEqual(payload["backend"], "grok")
        self.assertIn("total_ms", payload)
        self.assertNotIn("private question", line)
        self.assertNotIn("secret reply", line)


class ConfigTests(unittest.TestCase):
    def test_allowed_user_id_is_required(self):
        with mock.patch.dict(
            os.environ, {"TELEGRAM_BOT_TOKEN": "not-logged"}, clear=True
        ):
            with self.assertRaisesRegex(SystemExit, "TELEGRAM_ALLOWED_USER_ID is required"):
                load_instance_config("codex")

    def test_backend_specific_default_paths(self):
        with mock.patch.dict(
            os.environ,
            {"TELEGRAM_BOT_TOKEN": "not-logged", "TELEGRAM_ALLOWED_USER_ID": "123456789", "HOME": "/home/alex"},
            clear=True,
        ):
            config = load_instance_config("grok")
        self.assertEqual(
            config["state_path"], Path("/var/lib/telegram-agent/grok/state.sqlite3")
        )
        self.assertEqual(
            config["workspace"], Path("/home/alex/code/telegram-narrator")
        )

    def test_backend_workspaces_are_fixed(self):
        base = {
            "TELEGRAM_BOT_TOKEN": "not-logged",
            "TELEGRAM_ALLOWED_USER_ID": "123456789",
        }
        with mock.patch.dict(
            os.environ, {**base, "AGENT_WORKSPACE": "/home/alex"}, clear=True
        ):
            with self.assertRaisesRegex(SystemExit, "for grok"):
                load_instance_config("grok")
        with mock.patch.dict(
            os.environ,
            {**base, "AGENT_WORKSPACE": "/home/alex/code/telegram-narrator"},
            clear=True,
        ):
            with self.assertRaisesRegex(SystemExit, "for codex"):
                load_instance_config("codex")


if __name__ == "__main__":
    unittest.main()
