# Codex and Grok Telegram instances

The shared bridge supports independent `codex` and `grok` systemd instances.
Each instance has its own Telegram token, SQLite state, upload directory, CLI
session history, logs, and enabled-at-boot state.

## Install without changing the live bot

```bash
sudo /home/alex/codex-telegram-bridge/install_instances.sh
```

This installs `telegram-agent@.service` and root-owned runtime files. It does not
start, stop, enable, disable, or restart any service.

## Migrate the existing Codex bot

```bash
sudo /home/alex/codex-telegram-bridge/migrate_codex_instance.sh
```

This reuses the existing Codex bot token and SQLite state, replaces the legacy
service with `telegram-agent@codex.service`, and automatically restores the old
service if the template instance fails to start.

## Configure the separate Grok bot

Create a new Telegram bot with BotFather, then enter its token locally:

```bash
sudo /home/alex/codex-telegram-bridge/configure_instance.sh grok
sudo systemctl enable --now telegram-agent@grok.service
```

Never paste a bot token into chat or shell command arguments.

## Operate instances

```bash
systemctl status telegram-agent@codex.service --no-pager -l
systemctl status telegram-agent@grok.service --no-pager -l
journalctl -u telegram-agent@codex.service -n 120 --no-pager
journalctl -u telegram-agent@grok.service -n 120 --no-pager
```

Both bots may run simultaneously because they use distinct tokens. Stopping or
restarting one instance does not affect the other or Hermes.
