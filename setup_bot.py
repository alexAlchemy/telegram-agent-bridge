#!/usr/bin/env python3
"""One-shot validation and command registration for the dedicated Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import os

from bridge import TelegramClient


COMMANDS = [
    {"command": "new", "description": "Start a fresh Codex session"},
    {"command": "status", "description": "Show bridge and session status"},
    {"command": "peek", "description": "Show current or latest turn progress"},
    {"command": "cancel", "description": "Stop the active Codex turn"},
    {"command": "confirm", "description": "Approve the exact pending external action"},
    {"command": "deny", "description": "Discard the pending external action"},
    {"command": "help", "description": "Show usage help"},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--commands-only", action="store_true", help="Update commands without clearing updates"
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    client = TelegramClient(
        token, os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    )
    identity = await client.request("getMe")
    if not isinstance(identity, dict) or not identity.get("is_bot"):
        raise SystemExit("Telegram token did not identify a bot")
    if not args.commands_only:
        await client.request("deleteWebhook", {"drop_pending_updates": True})
    await client.request("setMyCommands", {"commands": COMMANDS})
    username = identity.get("username") or "(no username)"
    action = "commands registered" if args.commands_only else "webhook cleared and commands registered"
    print(f"Validated bot ; {action}.")


if __name__ == "__main__":
    asyncio.run(main())
