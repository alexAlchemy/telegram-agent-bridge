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
import uuid
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
    apply_failure_kind,
    bridge_home,
    classify_failure,
    cleanup_upload_root,
    drain_stderr,
    iter_subprocess_lines,
    configure_logging,
    get_attachment,
    with_failure_hint,
)


GROK_SESSION_ROOT = bridge_home() / ".grok" / "sessions"
GROK_ABSOLUTE_IMAGE_PATTERN = re.compile(
    r"(?P<path>" + re.escape(str(GROK_SESSION_ROOT)) + r"/[^\s\]\[()<>]+/"
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
    workspace_key = workspace_key or quote(str(bridge_home() / "code" / "telegram-narrator"), safe="")
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
        environment.setdefault("HOME", str(Path.home()))
        environment.setdefault("USER", "alex")
        environment.setdefault("LOGNAME", "alex")
        environment.setdefault(
            "PATH", f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"
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
            stderr_kind = await stderr_task
            if self.process.returncode != 0:
                return BackendResult(
                    False,
                    "",
                    thread_id=parsed.thread_id,
                    error=with_failure_hint(
                        f"Grok exited with status {self.process.returncode}", stderr_kind
                    ),
                    failure_kind=stderr_kind,
                )
            return apply_failure_kind(parsed, stderr_kind)
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

    async def _drain_stderr(self, stream: asyncio.StreamReader | None) -> str | None:
        return await drain_stderr(stream, "grok")


def parse_structured_reply(structured: Any) -> tuple[str, bool, str | None] | None:
    """Return (reply, confirmation_required, confirmation_summary), or None if malformed."""
    if not isinstance(structured, dict):
        return None
    try:
        reply = structured["reply"]
        confirmation_required = bool(structured["confirmation_required"])
        confirmation_summary = structured.get("confirmation_summary")
    except KeyError:
        return None
    if not isinstance(reply, str):
        return None
    if confirmation_required and not isinstance(confirmation_summary, str):
        return None
    if not confirmation_required:
        confirmation_summary = None
    return reply, confirmation_required, confirmation_summary


# Write-capable MCP tools removed from the Claude instance, so Telegram turns can read
# connected apps but cannot send, edit, delete, share or start external changes.
# Reads stay available. Tool names come from the claude.ai connectors and the cookbook.
CLAUDE_DENIED_TOOLS = (
    # Gmail
    *(f"mcp__claude_ai_Gmail__{name}" for name in (
        "apply_sensitive_message_label", "apply_sensitive_thread_label", "create_draft",
        "create_label", "delete_draft", "delete_label", "forward", "label_message",
        "label_thread", "mark_message_spam", "mark_thread_spam", "reply", "send_message",
        "trash_message", "trash_thread", "unlabel_message", "unlabel_thread",
        "unmark_message_spam", "unmark_thread_spam", "untrash_message", "untrash_thread",
        "update_draft", "update_label", "update_message_labels",
    )),
    # Google Calendar
    *(f"mcp__claude_ai_Google_Calendar__{name}" for name in (
        "create_event", "delete_event", "respond_to_event", "update_event",
    )),
    # Notion
    *(f"mcp__claude_ai_Notion__{name}" for name in (
        "notion-convert-page-to-skill", "notion-create-attachment", "notion-create-comment",
        "notion-create-database", "notion-create-file-upload", "notion-create-folder",
        "notion-create-pages", "notion-create-view", "notion-duplicate-page",
        "notion-move-pages", "notion-restore-pages", "notion-send-message-to-session",
        "notion-spawn-session", "notion-stop-session", "notion-update-data-source",
        "notion-update-folder", "notion-update-page", "notion-update-view",
        "notion-upload-skill",
    )),
    # Linear
    *(f"mcp__claude_ai_Linear__{name}" for name in (
        "create_attachment", "create_attachment_from_upload", "create_initiative_label",
        "create_issue_label", "delete_attachment", "delete_comment", "delete_diff_comment",
        "delete_status_update", "mark_notification", "merge_diff",
        "prepare_attachment_upload", "resolve_diff_thread", "restore_initiative_label",
        "restore_issue_label", "restore_project_label", "retire_initiative_label",
        "retire_issue_label", "retire_project_label", "save_comment", "save_diff_comment",
        "save_document", "save_initiative", "save_initiative_label", "save_issue",
        "save_issue_label", "save_milestone", "save_project", "save_project_label",
        "save_release", "save_release_note", "save_status_update", "share_issue",
        "submit_diff_review", "unshare_issue", "update_diff",
    )),
    # Dropbox
    *(f"mcp__claude_ai_Dropbox__{name}" for name in (
        "copy", "create_file", "create_file_request", "create_folder",
        "create_shared_link", "delete", "move",
    )),
    # Claude Docs
    *(f"mcp__claude_ai_Claude_Docs__{name}" for name in ("batch", "create", "delete", "update")),
    # Cloudflare: execute can run arbitrary API calls
    "mcp__claude_ai_cloudflare__execute",
)


class ClaudeRunner:
    name = "claude"

    def __init__(
        self,
        binary: str,
        workspace: Path,
        schema_path: Path,
        prompt_root: Path,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.binary = binary
        self.workspace = workspace
        self.prompt_root = prompt_root
        self.timeout_seconds = timeout_seconds
        self.process: asyncio.subprocess.Process | None = None
        self.schema_json = json.dumps(
            json.loads(schema_path.read_text()), separators=(",", ":")
        )

    def _environment(self) -> dict[str, str]:
        environment = {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}
        environment.setdefault("HOME", str(Path.home()))
        environment.setdefault("USER", "alex")
        environment.setdefault("LOGNAME", "alex")
        environment.setdefault(
            "PATH", f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin"
        )
        return environment

    def build_argv(self, session_id: str | None) -> list[str]:
        # The prompt arrives on stdin. Only user-level settings load, so the user's
        # connected MCP servers are available. Write-capable tools are denied outright.
        argv = [
            self.binary,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "bypassPermissions",
            "--setting-sources",
            "user",
            "--disallowedTools",
            ",".join(CLAUDE_DENIED_TOOLS),
            "--json-schema",
            self.schema_json,
            "--append-system-prompt",
            DEVELOPER_INSTRUCTIONS,
        ]
        if session_id:
            argv.extend(["--resume", session_id])
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
            return BackendResult(False, "", error="Claude is already running")
        self.prompt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, prompt_name = tempfile.mkstemp(
            prefix="prompt-", suffix=".txt", dir=self.prompt_root
        )
        prompt_file = Path(prompt_name)
        marks: dict[str, Any] = {}
        try:
            if image_path:
                prompt = f"{prompt}\n\nUse this referenced image as input: {image_path}"
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(prompt)
            run_started = time.monotonic()
            with prompt_file.open("rb") as stdin_stream:
                self.process = await asyncio.create_subprocess_exec(
                    *self.build_argv(thread_id),
                    cwd=self.workspace,
                    env=self._environment(),
                    stdin=stdin_stream,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=SUBPROCESS_STREAM_LIMIT_BYTES,
                    start_new_session=True,
                )
            spawned = time.monotonic()
            stdout_task = asyncio.create_task(
                self._read_stdout(self.process.stdout, marks)
            )
            stderr_task = asyncio.create_task(self._drain_stderr(self.process.stderr))
            try:
                await asyncio.wait_for(self.process.wait(), timeout=self.timeout_seconds)
            except TimeoutError:
                await self.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                return BackendResult(False, "", error="Claude turn timed out")
            parsed = await stdout_task
            stderr_kind = await stderr_task
            parsed.timing = self._timing(run_started, spawned, marks)
            if self.process.returncode != 0:
                # Claude also reports the reason in its result event, so prefer that category.
                kind = parsed.failure_kind or stderr_kind
                return BackendResult(
                    False,
                    "",
                    thread_id=parsed.thread_id,
                    error=with_failure_hint(
                        f"Claude exited with status {self.process.returncode}", kind
                    ),
                    failure_kind=kind,
                )
            return apply_failure_kind(parsed, stderr_kind)
        except FileNotFoundError:
            return BackendResult(False, "", error="Claude executable was not found")
        except OSError as exc:
            logging.error("claude subprocess failure: %s", type(exc).__name__)
            return BackendResult(False, "", error="Claude process could not be started")
        finally:
            self.process = None
            with contextlib.suppress(OSError):
                prompt_file.unlink()

    @staticmethod
    def _timing(run_started: float, spawned: float, marks: dict[str, Any]) -> dict[str, Any]:
        """Split one turn's wall time into bridge, CLI-startup, and CLI-reported parts."""
        now = time.monotonic()
        cli = marks.get("cli", {})
        cli_ms = cli.get("duration_ms")
        init_at = marks.get("init")
        wall_ms = (now - run_started) * 1000
        return {
            "spawn_ms": round((spawned - run_started) * 1000),
            # Time from spawn to the CLI's init event: process boot before any model work.
            "init_ms": round((init_at - spawned) * 1000) if init_at else None,
            "cli_ms": cli_ms,
            "api_ms": cli.get("duration_api_ms"),
            "ttft_ms": cli.get("ttft_ms"),
            "first_content_ms": cli.get("first_content_frame_ms"),
            "num_turns": cli.get("num_turns"),
            # Wall time not reported by the CLI: boot before its clock starts, and teardown.
            "cli_overhead_ms": (
                round(wall_ms - cli_ms) if isinstance(cli_ms, (int, float)) else None
            ),
            "wall_ms": round(wall_ms),
        }

    async def _read_stdout(
        self, stream: asyncio.StreamReader | None, marks: dict[str, Any] | None = None
    ) -> BackendResult:
        if stream is None:
            return BackendResult(False, "", error="Claude stdout was unavailable")
        marks = marks if marks is not None else {}
        session_id: str | None = None
        final: dict[str, Any] | None = None
        async for raw_line in iter_subprocess_lines(stream):
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("session_id"), str):
                session_id = event["session_id"]
            if event.get("type") == "system" and event.get("subtype") == "init":
                marks.setdefault("init", time.monotonic())
            if event.get("type") == "result":
                final = event
                marks["cli"] = {
                    key: final.get(key)
                    for key in (
                        "duration_ms",
                        "duration_api_ms",
                        "ttft_ms",
                        "first_content_frame_ms",
                        "num_turns",
                    )
                }
        if final is None:
            return BackendResult(False, "", thread_id=session_id, error="Claude returned no result")
        if isinstance(final.get("session_id"), str):
            session_id = final["session_id"]
        if final.get("is_error") or final.get("subtype") != "success":
            kind = self._failure_kind(final)
            return BackendResult(
                False,
                "",
                thread_id=session_id,
                error=with_failure_hint("Claude reported a failed turn", kind),
                failure_kind=kind,
            )
        fields = parse_structured_reply(final.get("structured_output"))
        if fields is None:
            # The turn itself succeeded, so its work is done. Deliver the plain-text result
            # rather than discarding it; the confirmation flag cannot be recovered here.
            text = final.get("result")
            if isinstance(text, str) and text.strip():
                logging.warning(
                    "claude structured output missing or invalid; using plain-text result"
                )
                usage = final.get("usage") if isinstance(final.get("usage"), dict) else None
                return BackendResult(True, text, thread_id=session_id, usage=usage)
            return BackendResult(
                False,
                "",
                thread_id=session_id,
                error="Claude returned an invalid structured reply",
            )
        reply, confirmation_required, confirmation_summary = fields
        usage = final.get("usage") if isinstance(final.get("usage"), dict) else None
        return BackendResult(
            True,
            reply,
            thread_id=session_id,
            confirmation_required=confirmation_required,
            confirmation_summary=confirmation_summary,
            usage=usage,
        )

    @staticmethod
    def _failure_kind(final: dict[str, Any]) -> str | None:
        """Classify a failed result event from its error strings. Nothing is logged or shown."""
        parts: list[str] = []
        errors = final.get("errors")
        if isinstance(errors, list):
            parts.extend(item for item in errors if isinstance(item, str))
        text = final.get("result")
        if isinstance(text, str):
            parts.append(text[:500])
        return classify_failure(" ".join(parts))

    async def _drain_stderr(self, stream: asyncio.StreamReader | None) -> str | None:
        return await drain_stderr(stream, "claude")


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
        timing: dict[str, Any] = {
            "turn_id": uuid.uuid4().hex[:8],
            "backend": self.backend_name,
            "outcome": "internal error",
        }
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
            timing["attachment"] = attachment is not None
            timing["prep_ms"] = round((time.monotonic() - started) * 1000)
            runner_started = time.monotonic()
            previous_thread = self.state.get_thread(chat_id)
            result = await self.runner.run(prompt, previous_thread, image_path)
            session_reset = False
            if previous_thread and not result.success and result.failure_kind == "session":
                # The CLI no longer knows this session (pruned, or invalidated by an upgrade).
                # It refused before doing any work, so start over once rather than leave the
                # chat stuck failing on the same dead session until /new.
                logging.warning(
                    "%s session could not be resumed; retrying with a fresh session",
                    self.backend_name,
                )
                self.state.set_thread(chat_id, None)
                session_reset = True
                timing["session_reset"] = True
                result = await self.runner.run(prompt, None, image_path)
            timing["runner_ms"] = round((time.monotonic() - runner_started) * 1000)
            timing.update(result.timing or {})
            duration = time.monotonic() - started
            if not result.success:
                logging.warning(
                    "%s turn failed duration=%.1fs kind=%s error=%s",
                    self.backend_name,
                    duration,
                    result.failure_kind or "unknown",
                    result.error,
                )
                if result.failure_kind:
                    timing["failure_kind"] = result.failure_kind
                await self.telegram.send_message(
                    chat_id,
                    f"{self.backend_title} could not complete that turn: {result.error}",
                )
                timing["outcome"] = "backend failed"
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
            if session_reset:
                reply = (
                    f"(The previous {self.backend_title} session could not be resumed, "
                    f"so this started a new one.)\n\n{reply}"
                ).rstrip()
            usage = result.usage or {}
            logging.info(
                "%s turn complete duration=%.1fs input_tokens=%s output_tokens=%s",
                self.backend_name,
                duration,
                usage.get("input_tokens", "unknown"),
                usage.get("output_tokens", "unknown"),
            )
            self._set_turn_stage("sending")
            send_started = time.monotonic()
            for generated_image in result.generated_images:
                await self.telegram.send_photo(chat_id, generated_image)
            if reply:
                await self.telegram.send_message(chat_id, reply)
            elif not result.generated_images:
                await self.telegram.send_message(
                    chat_id, f"{self.backend_title} completed without a response."
                )
            timing["send_ms"] = round((time.monotonic() - send_started) * 1000)
            timing["outcome"] = "delivered"
            self._finish_turn("delivered")
        except BridgeError as exc:
            await self.telegram.send_message(chat_id, str(exc))
            timing["outcome"] = "rejected"
            self._finish_turn("rejected")
        except Exception as exc:
            logging.exception("unexpected turn failure: %s", type(exc).__name__)
            with contextlib.suppress(TelegramError):
                await self.telegram.send_message(
                    chat_id, "The bridge encountered an internal error."
                )
            timing["outcome"] = "internal error"
            self._finish_turn("internal error")
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task
            if turn_dir:
                shutil.rmtree(turn_dir, ignore_errors=True)
            timing["total_ms"] = round((time.monotonic() - started) * 1000)
            # One JSON line per turn, so timings can be aggregated with jq. No content.
            logging.info("turn_timing %s", json.dumps(timing, sort_keys=True))

    def help_text(self) -> str:
        return (
            f"This bot uses the {self.backend_title} backend. Send text, a photo, or a "
            "document up to 20 MB. It can chat, search the web, inspect files, and work "
            "inside the agent home directory.\n\n"
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
    parser.add_argument("--backend", choices=("codex", "grok", "claude"), required=True)
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
        bridge_home() / "code" / "telegram-narrator"
        if backend == "grok"
        else bridge_home()
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
                f"{Path.home()}/.cache/telegram-agent-bridge/{backend}/uploads",
            )
        ).resolve(),
        "timeout_seconds": timeout_seconds,
        "codex_binary": os.environ.get("CODEX_BINARY", "/usr/local/bin/codex"),
        "grok_binary": os.environ.get("GROK_BINARY", str(Path.home() / ".local" / "bin" / "grok")),
        "claude_binary": os.environ.get("CLAUDE_BINARY", str(Path.home() / ".local" / "bin" / "claude")),
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
    elif backend == "claude":
        runner = ClaudeRunner(
            config["claude_binary"],
            config["workspace"],
            config["schema_path"],
            config["upload_root"].parent / "prompts",
            config["timeout_seconds"],
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
