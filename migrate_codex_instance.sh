#!/bin/bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Run this migration with sudo." >&2
    exit 1
fi
if [[ ! -f /etc/codex-telegram-bridge.env ]]; then
    echo "Legacy Codex bridge environment file is missing." >&2
    exit 1
fi
if [[ ! -f /etc/systemd/system/telegram-agent@.service ]]; then
    echo "Run install_instances.sh first." >&2
    exit 1
fi

install -d -o root -g root -m 0755 /etc/telegram-agent
temporary_file="$(mktemp /etc/telegram-agent/codex.env.XXXXXX)"
trap 'rm -f "$temporary_file"' EXIT
chmod 0600 "$temporary_file"
cp /etc/codex-telegram-bridge.env "$temporary_file"
{
    printf 'BRIDGE_STATE_PATH=/var/lib/codex-telegram-bridge/state.sqlite3\n'
    printf 'BRIDGE_UPLOAD_ROOT=/home/alex/.cache/codex-telegram-bridge/uploads\n'
} >>"$temporary_file"
chown root:root "$temporary_file"
mv -f "$temporary_file" /etc/telegram-agent/codex.env
trap - EXIT

systemctl stop codex-telegram-bridge.service
systemctl disable codex-telegram-bridge.service
if ! systemctl enable --now telegram-agent@codex.service; then
    echo "Template instance failed; restoring the legacy Codex service." >&2
    systemctl disable --now telegram-agent@codex.service || true
    systemctl enable --now codex-telegram-bridge.service
    exit 1
fi

echo "Codex bot migrated to telegram-agent@codex.service with its session state preserved."
