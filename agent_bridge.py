#!/usr/bin/env python3
"""Multi-backend Telegram bridge built on the proven Codex bridge primitives."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from bridge import (
    CodexResult as BackendResult,
    Bridge,
    BridgeError,
    CodexRunner,
    DEFAULT_CACHE_RECYCLE_BYTES,
    DEFAULT_CACHE_RECYCLE_MIN_UPTIME_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DEVELOPER_INSTRUCTIONS,
    MAX_FILE_BYTES,
    MAX_OUTBOUND_IMAGE_BYTES,
    SUBPROCESS_STREAM_LIMIT_BYTES,
    SAFE_ENV_KEYS,
    StateDB,
    TelegramClient,
    TelegramError,
    cleanup_upload_root,
    iter_subprocess_lines,
    configure_logging,
    get_attachment,
)


GROK_SESSION_ROOT = Path.home() / ".grok" / "sessions"
GROK_ABSOLUTE_IMAGE_PATTERN = re.compile(
    r"(?P<path>/home/alex/\.grok/sessions/[^\s\]\[()<>]+/"
    r"[^\s\]\[()<>]+/images/[^\s\]\[()<>]+\.(?:png|jpe?g|webp))",
    re.IGNORECASE,
)
GROK_RELATIVE_IMAGE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._/-])(?P<path>images/[A-Za-z0-9._/-]+\.(?:png|jpe?g|webp))",
    re.IGNORECASE,
)


def extract_grok_image_paths(
    value: Any,
    session_id: str | None = None,
    session_root: Path | None = None,
    workspace_key: str | None = None,
) -> list[Path]:
    """Return only explicitly referenced, size-capped Grok session images."""
    strings: list[str] = []

    def collect(item: Any) -> None:
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, dict):
            for nested in item.values():
                collect(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                collect(nested)

    session_root = session_root or GROK_SESSION_ROOT
    workspace_key = workspace_key or quote("/home/alex/code/telegram-narrator", safe="")
    collect(value)
    candidates: list[Path] = []
    for string in strings:
        candidates.extend(
            Path(match.group("path"))
            for match in GROK_ABSOLUTE_IMAGE_PATTERN.finditer(string)
        )
        if session_id:
            session_images = session_root / workspace_key / session_id
            candidates.extend(
                session_images / match.group("path")
                for match in GROK_RELATIVE_IMAGE_PATTERN.finditer(string)
            )

    trusted_root = session_root.resolve()
    accepted: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(trusted_root)
            stat_result = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            continue
        parts = relative.parts
        if len(parts) < 4 or parts[-2] != "images":
            continue
        if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        if not resolved.is_file() or stat_result.st_size > MAX_OUTBOUND_IMAGE_BYTES:
            continue
        if resolved not in accepted:
            accepted.append(resolved)
    return accepted


class GrokRunner:
    name = "grok"

    def __init__(
        self,
        binary: str,
        workspace: Path,
        schema_path: Path,
        prompt_root: Path,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_turns: int = 100,
    ) -> None:
        self.binary = binary
        self.workspace = workspace
        self.prompt_root = prompt_root
        self.timeout_seconds = timeout_seconds
        self.max_turns = max_turns
        self.workspace_key = quote(str(workspace), safe="")
        self.process: asyncio.subprocess.Process | None = None
        self.schema_json = json.dumps(
            json.loads(schema_path.read_text()), separators=(",", ":")
        )

    def _environment(self) -> dict[str, str]:
        environment = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
        environment.setdefault("HOME", "/home/alex")
        environment.setdefault("USER", "alex")
        environment.setdefault("LOGNAME", "alex")
        environment.setdefault(
            "PATH", "/home/alex/.local/bin:/usr/local/bin:/usr/bin:/bin"
        )
        return environment

    def build_argv(self, prompt_file: Path, session_id: str | None) -> list[str]:
        argv = [
            self.binary,
            "--no-auto-update",
            "--cwd",
            str(self.workspace),
            "--sandbox",
            "workspace",
            "--always-approve",
            "--no-subagents",
            "--max-turns",
            str(self.max_turns),
            "--output-format",
            "streaming-json",
            "--json-schema",
            self.schema_json,
            "--rules",
            DEVELOPER_INSTRUCTIONS,
        ]
        if session_id:
            argv.extend(["--resume", session_id])
        argv.extend(["--prompt-file", str(prompt_file)])
        return argv

    async def cancel(self) -> bool:
        process = self.process
        if process is None or process.returncode is not None:
            return False
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        return True

    async def run(
        self, prompt: str, thread_id: str | None, image_path: Path | None = None
    ) -> BackendResult:
        if self.process is not None and self.process.returncode is None:
            return BackendResult(False, "", error="Grok is already running")
        self.prompt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, prompt_name = tempfile.mkstemp(
            prefix="prompt-", suffix=".txt", dir=self.prompt_root
        )
        prompt_file = Path(prompt_name)
        try:
            if image_path:
                prompt = f"{prompt}\n\nUse this referenced image as input: @{image_path}"
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(prompt)
            self.process = await asyncio.create_subprocess_exec(
                *self.build_argv(prompt_file, thread_id),
                cwd=self.workspace,
                env=self._environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=SUBPROCESS_STREAM_LIMIT_BYTES,
                start_new_session=True,
            )
            stdout_task = asyncio.create_task(self._read_stdout(self.process.stdout))
            stderr_task = asyncio.create_task(self._drain_stderr(self.process.stderr))
            try:
                await asyncio.wait_for(self.process.wait(), timeout=self.timeout_seconds)
            except TimeoutError:
                await self.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                return BackendResult(False, "", error="Grok turn timed out")
            parsed = await stdout_task
            await stderr_task
            if self.process.returncode != 0:
                return BackendResult(
                    False,
                    "",
                    thread_id=parsed.thread_id,
                    error=f"Grok exited with status {self.process.returncode}",
                )
            return parsed
        except FileNotFoundError:
            return BackendResult(False, "", error="Grok executable was not found")
        except OSError as exc:
            logging.error("grok subprocess failure: %s", type(exc).__name__)
            return BackendResult(False, "", error="Grok process could not be started")
        finally:
            self.process = None
            with contextlib.suppress(OSError):
                prompt_file.unlink()

    async def _read_stdout(
        self, stream: asyncio.StreamReader | None
    ) -> BackendResult:
        if stream is None:
            return BackendResult(False, "", error="Grok stdout was unavailable")
        session_id: str | None = None
        structured: dict[str, Any] | None = None
        usage: dict[str, Any] | None = None
        generated_images: list[Path] = []
        failed = False
        async for raw_line in iter_subprocess_lines(stream):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            for image_path in extract_grok_image_paths(
                event, workspace_key=self.workspace_key
            ):
                if image_path not in generated_images:
                    generated_images.append(image_path)
            if event.get("type") == "end":
                if isinstance(event.get("sessionId"), str):
                    session_id = event["sessionId"]
                for image_path in extract_grok_image_paths(
                    event, session_id, workspace_key=self.workspace_key
                ):
                    if image_path not in generated_images:
                        generated_images.append(image_path)
                if isinstance(event.get("structuredOutput"), dict):
                    structured = event["structuredOutput"]
                if isinstance(event.get("usage"), dict):
                    usage = event["usage"]
                if event.get("stopReason") not in {None, "EndTurn"}:
                    failed = True
            elif event.get("type") == "error":
                failed = True
        if failed and structured is None:
            return BackendResult(
                False, "", thread_id=session_id, error="Grok reported a failed turn"
            )
        if structured is None:
            return BackendResult(
                False, "", thread_id=session_id, error="Grok returned no structured reply"
            )
        try:
            reply = structured["reply"]
            confirmation_required = bool(structured["confirmation_required"])
            confirmation_summary = structured.get("confirmation_summary")
            if not isinstance(reply, str):
                raise TypeError
            if confirmation_required and not isinstance(confirmation_summary, str):
                raise TypeError
            if not confirmation_required:
                confirmation_summary = None
        except (KeyError, TypeError):
            return BackendResult(
                False,
                "",
                thread_id=session_id,
                error="Grok returned an invalid structured reply",
            )
        return BackendResult(
            True,
            reply,
            thread_id=session_id,
            confirmation_required=confirmation_required,
            confirmation_summary=confirmation_summary,
            usage=usage,
            generated_images=tuple(generated_images),
        )

    async def _drain_stderr(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        total = 0
        while chunk := await stream.read(64 * 1024):
            total += len(chunk)
        if total:
            logging.debug("grok stderr bytes=%d", total)


class AgentBridge(Bridge):
    def __init__(self, *args: Any, backend_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.backend_name = backend_name
        self.backend_title = backend_name.title()

    async def handle_command(self, chat_id: int, command: str) -> None:
        if command in {"/start", "/help"}:
            await self.telegram.send_message(chat_id, self.help_text())
            return
        if command == "/status":
            active = bool(self.active_task and not self.active_task.done())
            thread_id = self.state.get_thread(chat_id)
            pending = self.state.get_pending_confirmation(chat_id)
            status = [
                f"Status: {'working' if active else 'idle'}",
                f"Backend: {self.backend_name}",
                f"Workspace: {self.runner.workspace}",
                f"Session: {thread_id[:12] + '…' if thread_id else 'new'}",
                f"Pending confirmation: {'yes' if pending else 'no'}",
            ]
            memory_status = self.memory_status()
            if memory_status:
                status.append(memory_status)
            await self.telegram.send_message(chat_id, "\n".join(status))
            return
        if command == "/new":
            if self.active_task and not self.active_task.done():
                await self.telegram.send_message(
                    chat_id, "Cancel the active turn before starting a new session."
                )
                return
            self.state.set_thread(chat_id, None)
            await self.telegram.send_message(
                chat_id, f"Started a fresh {self.backend_title} session."
            )
            return
        await super().handle_command(chat_id, command)

    async def process_turn(
        self, chat_id: int, message: dict[str, Any], confirmation_turn: bool
    ) -> None:
        turn_dir: Path | None = None
        typing_task = asyncio.create_task(self._typing_loop(chat_id))
        started = time.monotonic()
        try:
            self._set_turn_stage("processing")
            prompt = message.get("text") or message.get("caption") or "Analyze the attached file."
            image_path: Path | None = None
            attachment = get_attachment(message)
            if attachment:
                declared_size = attachment.get("file_size")
                if isinstance(declared_size, int) and declared_size > MAX_FILE_BYTES:
                    raise BridgeError("Attachment exceeds the 20 MB limit")
                turn_dir = Path(tempfile.mkdtemp(prefix="turn-", dir=self.upload_root))
                file_record = await self.telegram.get_file(attachment["file_id"])
                from bridge import sanitize_filename

                safe_name = sanitize_filename(
                    attachment.get("file_name") or file_record["file_path"]
                )
                destination = turn_dir / safe_name
                await self.telegram.download_file(file_record["file_path"], destination)
                if attachment.get("is_image"):
                    image_path = destination
                else:
                    prompt = f"{prompt}\n\nThe uploaded document is available at: {destination}"
            result = await self.runner.run(
                prompt, self.state.get_thread(chat_id), image_path
            )
            duration = time.monotonic() - started
            if not result.success:
                logging.warning(
                    "%s turn failed duration=%.1fs error=%s",
                    self.backend_name,
                    duration,
                    result.error,
                )
                await self.telegram.send_message(
                    chat_id,
                    f"{self.backend_title} could not complete that turn: {result.error}",
                )
                self._finish_turn("backend failed")
                return
            if result.thread_id:
                self.state.set_thread(chat_id, result.thread_id)
            if result.confirmation_required:
                self.state.set_pending_confirmation(chat_id, result.confirmation_summary)
                reply = (
                    f"{result.reply}\n\nPending external action:\n"
                    f"{result.confirmation_summary}\n\n"
                    "Reply /confirm to perform exactly this action, or /deny to discard it."
                )
            else:
                if confirmation_turn:
                    self.state.set_pending_confirmation(chat_id, None)
                reply = result.reply
            usage = result.usage or {}
            logging.info(
                "%s turn complete duration=%.1fs input_tokens=%s output_tokens=%s",
                self.backend_name,
                duration,
                usage.get("input_tokens", "unknown"),
                usage.get("output_tokens", "unknown"),
            )
            self._set_turn_stage("sending")
            for generated_image in result.generated_images:
                await self.telegram.send_photo(chat_id, generated_image)
            if reply:
                await self.telegram.send_message(chat_id, reply)
            elif not result.generated_images:
                await self.telegram.send_message(
                    chat_id, f"{self.backend_title} completed without a response."
                )
            self._finish_turn("delivered")
        except BridgeError as exc:
            await self.telegram.send_message(chat_id, str(exc))
            self._finish_turn("rejected")
        except Exception as exc:
            logging.exception("unexpected turn failure: %s", type(exc).__name__)
            with contextlib.suppress(TelegramError):
                await self.telegram.send_message(
                    chat_id, "The bridge encountered an internal error."
                )
            self._finish_turn("internal error")
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task
            if turn_dir:
                shutil.rmtree(turn_dir, ignore_errors=True)

    def help_text(self) -> str:
        return (
            f"This bot uses the {self.backend_title} backend. Send text, a photo, or a "
            "document up to 20 MB. It can chat, search the web, inspect files, and work "
            "inside /home/alex.\n\n"
            f"/new — start a fresh {self.backend_title} session\n"
            "/status — show backend and session status\n"
            "/peek — show progress or the latest turn result\n"
            "/cancel — stop the active turn\n"
            "/confirm — approve the exact pending external action\n"
            "/deny — discard the pending external action\n"
            "/help — show this message"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("codex", "grok"), required=True)
    return parser.parse_args()


def load_instance_config(backend: str) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    allowed_user = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    if not allowed_user:
        raise SystemExit("TELEGRAM_ALLOWED_USER_ID is required")
    try:
        allowed_user_id = int(allowed_user)
        timeout_seconds = int(
            os.environ.get("AGENT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
        )
        grok_max_turns = int(os.environ.get("GROK_MAX_TURNS", "100"))
        cache_recycle_bytes = int(
            os.environ.get("CODEX_CACHE_RECYCLE_BYTES", str(DEFAULT_CACHE_RECYCLE_BYTES))
        )
        cache_recycle_min_uptime_seconds = int(
            os.environ.get(
                "CODEX_CACHE_RECYCLE_MIN_UPTIME_SECONDS",
                str(DEFAULT_CACHE_RECYCLE_MIN_UPTIME_SECONDS),
            )
        )
    except ValueError as exc:
        raise SystemExit("Numeric bridge configuration is invalid") from exc
    base_dir = Path(__file__).resolve().parent
    expected_workspace = (
        Path("/home/alex")
        if backend == "codex"
        else Path("/home/alex/code/telegram-narrator")
    )
    workspace = Path(
        os.environ.get("AGENT_WORKSPACE", str(expected_workspace))
    ).resolve()
    if workspace != expected_workspace:
        raise SystemExit(
            f"AGENT_WORKSPACE for {backend} must be exactly {expected_workspace}"
        )
    return {
        "backend": backend,
        "token": token,
        "allowed_user_id": allowed_user_id,
        "api_base": os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org"),
        "workspace": workspace,
        "schema_path": Path(
            os.environ.get("AGENT_OUTPUT_SCHEMA", str(base_dir / "response_schema.json"))
        ).resolve(),
        "state_path": Path(
            os.environ.get(
                "BRIDGE_STATE_PATH", f"/var/lib/telegram-agent/{backend}/state.sqlite3"
            )
        ).resolve(),
        "upload_root": Path(
            os.environ.get(
                "BRIDGE_UPLOAD_ROOT",
                f"/home/alex/.cache/telegram-agent-bridge/{backend}/uploads",
            )
        ).resolve(),
        "timeout_seconds": timeout_seconds,
        "codex_binary": os.environ.get("CODEX_BINARY", "/usr/local/bin/codex"),
        "grok_binary": os.environ.get("GROK_BINARY", "/home/alex/.local/bin/grok"),
        "grok_max_turns": grok_max_turns,
        "cache_recycle_bytes": cache_recycle_bytes if backend == "codex" else 0,
        "cache_recycle_min_uptime_seconds": cache_recycle_min_uptime_seconds,
    }


BOT_COMMANDS = [
    {"command": "new", "description": "Start a fresh agent session"},
    {"command": "status", "description": "Show bridge and session status"},
    {"command": "peek", "description": "Show current or latest turn progress"},
    {"command": "cancel", "description": "Stop the active agent turn"},
    {"command": "confirm", "description": "Approve the exact pending external action"},
    {"command": "deny", "description": "Discard the pending external action"},
    {"command": "help", "description": "Show usage help"},
]


async def register_commands(telegram: TelegramClient, backend: str) -> None:
    try:
        await telegram.request("setMyCommands", {"commands": BOT_COMMANDS})
    except TelegramError:
        logging.warning("%s command registration failed", backend)


async def send_startup_notification(telegram: TelegramClient, chat_id: int, backend: str) -> None:
    try:
        await telegram.send_message(chat_id, f"{backend.title()} bridge is online.")
    except TelegramError:
        logging.warning("%s startup notification failed", backend)


async def async_main() -> None:
    backend = parse_args().backend
    config = load_instance_config(backend)
    if not config["schema_path"].is_file():
        raise SystemExit("Agent output schema is missing")
    state = StateDB(config["state_path"])
    telegram = TelegramClient(config["token"], config["api_base"])
    if backend == "codex":
        runner: Any = CodexRunner(
            config["codex_binary"],
            config["workspace"],
            config["schema_path"],
            config["timeout_seconds"],
            config["upload_root"].parent / "payload.json",
            Path(__file__).resolve().parent / "bridge_payload_mcp.py",
        )
    else:
        runner = GrokRunner(
            config["grok_binary"],
            config["workspace"],
            config["schema_path"],
            config["upload_root"].parent / "prompts",
            config["timeout_seconds"],
            config["grok_max_turns"],
        )
    bridge = AgentBridge(
        telegram,
        state,
        runner,
        config["allowed_user_id"],
        config["upload_root"],
        backend_name=backend,
        cache_recycle_bytes=config["cache_recycle_bytes"],
        cache_recycle_min_uptime_seconds=config["cache_recycle_min_uptime_seconds"],
    )
    cleanup_upload_root(config["upload_root"])
    await register_commands(telegram, backend)
    await send_startup_notification(telegram, config["allowed_user_id"], backend)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)
    task = asyncio.create_task(bridge.run_forever())
    stop_task = asyncio.create_task(stop_event.wait())
    recycle_task = asyncio.create_task(bridge.recycle_event.wait())
    await asyncio.wait((stop_task, recycle_task), return_when=asyncio.FIRST_COMPLETED)
    await bridge.stop()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    stop_task.cancel()
    recycle_task.cancel()
    state.close()
    if bridge.recycle_event.is_set():
        raise SystemExit(75)


def main() -> None:
    configure_logging()
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
