#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

source_dir="$(cd "$(dirname "$0")" && pwd)"
agent_home="$(getent passwd "${SUDO_USER:-alex}" | cut -d: -f6)"
dropin_dir=/etc/systemd/system/telegram-agent@grok.service.d

/usr/bin/test -f "$source_dir/agent_bridge.py"
/usr/bin/test -f "$source_dir/deploy/telegram-agent-grok-lockdown.conf"
/usr/bin/test -d "$agent_home/code/telegram-narrator"
/usr/bin/python3 -m py_compile "$source_dir/agent_bridge.py"

/usr/bin/install -o root -g root -m 0755 \
    "$source_dir/agent_bridge.py" \
    /usr/local/lib/codex-telegram-bridge/agent_bridge.py
/usr/bin/install -d -o root -g root -m 0755 "$dropin_dir"
/usr/bin/install -o root -g root -m 0644 \
    "$source_dir/deploy/telegram-agent-grok-lockdown.conf" \
    "$dropin_dir/lockdown.conf"

/usr/bin/systemctl daemon-reload
/usr/bin/systemctl restart telegram-agent@grok.service
/usr/bin/systemctl --no-pager -l status telegram-agent@grok.service
echo "GROK LOCKDOWN INSTALLED"
