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
    GrokRunner,
    extract_grok_image_paths,
    load_instance_config,
    send_startup_notification,
)
from bridge import CodexResult, MAX_OUTBOUND_IMAGE_BYTES, StateDB  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    async def send_typing(self, chat_id):
        return None


class FakeRunner:
    async def cancel(self):
        return False

    async def run(self, prompt, thread_id, image_path=None):
        return CodexResult(True, "ok", thread_id="session-1")


class StartupNotificationTests(unittest.IsolatedAsyncioTestCase):
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
        with mock.patch("agent_bridge.GROK_SESSION_ROOT", session_root), mock.patch(
            "agent_bridge.GROK_WORKSPACE_KEY", "workspace"
        ):
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
        self.assertEqual(config["workspace"], Path("/home/alex"))


if __name__ == "__main__":
    unittest.main()
