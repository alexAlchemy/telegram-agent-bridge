#!/bin/bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this helper with sudo." >&2
    exit 1
fi
if [[ $# -ne 1 || ! "$1" =~ ^(codex|grok)$ ]]; then
    echo "Usage: sudo $0 codex|grok" >&2
    exit 1
fi

instance="$1"
read -r -s -p "Enter the BotFather token for the $instance bot: " token </dev/tty
echo >/dev/tty
read -r -p "Enter the allowed Telegram user ID: " allowed_user_id </dev/tty
if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    echo "The token format is invalid; nothing was written." >&2
    exit 1
fi
if [[ ! "$allowed_user_id" =~ ^[0-9]+$ ]]; then
    echo "The Telegram user ID must contain digits only; nothing was written." >&2
    exit 1
fi

install -d -o root -g root -m 0755 /etc/telegram-agent
environment_file="/etc/telegram-agent/$instance.env"
temporary_file="$(mktemp "/etc/telegram-agent/$instance.env.XXXXXX")"
trap 'rm -f "$temporary_file"' EXIT
chmod 0600 "$temporary_file"
{
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
    printf 'TELEGRAM_ALLOWED_USER_ID=%s\n' "$allowed_user_id"
    printf 'AGENT_TIMEOUT_SECONDS=1800\n'
    printf 'LOG_LEVEL=INFO\n'
} >"$temporary_file"
chown root:root "$temporary_file"
mv -f "$temporary_file" "$environment_file"
trap - EXIT
unset token allowed_user_id

set -a
# shellcheck disable=SC1090
source "$environment_file"
set +a
/usr/bin/python3 /usr/local/lib/codex-telegram-bridge/setup_bot.py
echo "$instance bot configured. Its service was not enabled or started."
