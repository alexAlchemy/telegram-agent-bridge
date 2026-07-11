#!/bin/bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this helper with sudo." >&2
    exit 1
fi

read -r -s -p "Enter the new BotFather token: " token </dev/tty
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

environment_file=/etc/codex-telegram-bridge.env
temporary_file="$(mktemp /etc/codex-telegram-bridge.env.XXXXXX)"
trap 'rm -f "$temporary_file"' EXIT
chmod 0600 "$temporary_file"
{
    printf 'TELEGRAM_BOT_TOKEN=%s\n' "$token"
    printf 'TELEGRAM_ALLOWED_USER_ID=%s\n' "$allowed_user_id"
    printf 'LOG_LEVEL=INFO\n'
} >"$temporary_file"
chown root:root "$temporary_file"
mv -f "$temporary_file" "$environment_file"
trap - EXIT
unset token allowed_user_id

set -a
# shellcheck disable=SC1091
source "$environment_file"
set +a
/usr/bin/python3 /usr/local/lib/codex-telegram-bridge/setup_bot.py

echo "Token configured. The bridge service remains disabled and stopped."
