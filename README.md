# Telegram Agent Bridge

A dependency-free Python bridge connecting private Telegram bots to local Codex and Grok CLI sessions. A hardened systemd template runs separate `codex` and `grok` instances, each with its own bot token, session state, uploads, logs, and lifecycle.

This project is intentionally opinionated for a single-user VPS. It uses Telegram long polling, exposes no listener or public port, and fixes the agent workspace at `/home/alex`.

## Features

- Independent Codex and Grok Telegram bots from one codebase.
- Private-chat allowlist configured by the required `TELEGRAM_ALLOWED_USER_ID` environment variable.
- Resumable backend sessions with per-instance SQLite metadata.
- Text, photo, and document input up to 20 MB.
- Generated-image delivery through a turn-scoped stdio MCP payload, with multiple photos supported.
- `/confirm` and `/deny` flow for externally mutating connector actions.
- Process-group cancellation, typing indicators, retry backoff, and sanitized logs.
- Ordinary Codex final messages remain plain text; transport metadata is kept out of the reply. Legacy resumed sessions are narrowly unwrapped from the former bridge schema.
- Each backend sends a best-effort `<Backend> bridge is online.` message after successful startup.
- Python standard library only.

## Architecture

```text
Telegram Bot API                   Telegram Bot API
       |                                  |
telegram-agent@codex.service       telegram-agent@grok.service
       |                                  |
   Codex CLI                           Grok CLI
       |                                  |
SQLite state and uploads           SQLite state and uploads
```

For Codex turns, the bridge launches the bundled `bridge_payload_mcp.py` server over stdio. The tool atomically replaces one turn-tagged `payload.json`; after Codex exits, the bridge validates and consumes it once. Image paths never depend on scraping tool output or final-message JSON.

The instances share root-owned runtime files under `/usr/local/lib/codex-telegram-bridge` but use separate environment files, databases, upload paths, CLI sessions, and Telegram tokens.

## Security model

- Only private messages from the configured Telegram user are accepted.
- Each backend uses a distinct token in a root-owned `0600` environment file.
- Telegram tokens are removed from agent subprocess environments.
- Codex uses workspace-write sandboxing and non-interactive approvals.
- Grok uses workspace confinement, no subagents, and the approved non-interactive profile.
- Attachments are never automatically extracted or executed and are removed after each turn.
- Generated files must resolve beneath backend-specific trusted roots and are capped at 10 MB.
- Prompts, replies, credentials, uploaded contents, and raw backend stderr are not logged.
- No webhook, dashboard, public terminal, or network listener is created.

## Requirements

- Linux with systemd and Python 3.12 or newer.
- A working local Codex CLI login for the Codex instance.
- A working local Grok CLI login for the Grok instance.
- One Telegram bot token per enabled instance.
- Deployment paths and service user matching this VPS-oriented configuration.

## Configuration

Each instance reads `/etc/telegram-agent/<backend>.env`. Start from `instance.env.example`:

```dotenv
TELEGRAM_BOT_TOKEN=replace-me
TELEGRAM_ALLOWED_USER_ID=replace-with-telegram-user-id
LOG_LEVEL=INFO
AGENT_TIMEOUT_SECONDS=1800
```

Keep real environment files out of the repository and set them to `root:root` mode `0600`. Never pass bot tokens in chat or command-line arguments.

The interactive helper validates and writes configuration locally:

```bash
sudo ./configure_instance.sh codex
sudo ./configure_instance.sh grok
```

## Install

Install the runtime and service template without starting or restarting either bot:

```bash
sudo ./install_instances.sh
```

Enable an instance only after its environment and backend login are ready:

```bash
sudo systemctl enable --now telegram-agent@codex.service
sudo systemctl enable --now telegram-agent@grok.service
```

Legacy single-instance scripts remain for migration compatibility. See `INSTANCES.md` for migration and operations.

## Bot commands

- `/new` clears the current bridge session mapping.
- `/status` shows backend, activity, session, and confirmation state.
- `/peek` shows current progress or the latest turn result.
- `/cancel` terminates the active backend process group.
- `/confirm` performs exactly the pending external action.
- `/deny` discards the pending external action.
- `/help` shows command help.

## Controlled self-deployment

A one-time privileged bootstrap installs a root-owned, no-argument deploy helper, a hardened oneshot unit, and a PolicyKit rule allowing only user `alex` to start that exact unit:

```bash
sudo ./install_deploy_gate.sh
```

After bootstrap, an agent running as `alex` can validate, snapshot, deploy, and schedule a delayed Codex-only restart without sudo:

```bash
systemctl --no-block start telegram-agent-deploy-codex.service
```

The helper never executes a repository installer as root and never copies user-editable unit files. It deploys only an explicit application-file allowlist, aborts if source changes during validation or staging, and restores the previous runtime if restart verification fails. Grok and Hermes are outside its scope.

## Operations

```bash
systemctl status telegram-agent@codex.service --no-pager -l
systemctl status telegram-agent@grok.service --no-pager -l
journalctl -u telegram-agent@codex.service -n 120 --no-pager
journalctl -u telegram-agent@grok.service -n 120 --no-pager
```

Restart only the instance whose source, configuration, or authentication changed.

## Development and validation

```bash
python3 -m py_compile bridge.py agent_bridge.py bridge_payload_mcp.py setup_bot.py tests/test_bridge.py tests/test_agent_bridge.py tests/test_bridge_payload_mcp.py tests/test_deploy_gate.py
python3 -m unittest discover -s tests -v
systemd-analyze verify telegram-agent@.service
python3 -m json.tool response_schema.json >/dev/null
```

Validate first, review the diff, install with `install_instances.sh`, and restart only the affected instance.
