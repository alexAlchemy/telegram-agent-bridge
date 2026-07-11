#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

SOURCE_DIR=/home/alex/codex-telegram-bridge
INSTALL_DIR=/usr/local/lib/codex-telegram-bridge

/usr/bin/install -d -o root -g root -m 0755 "$INSTALL_DIR"
/usr/bin/install -o root -g root -m 0755 "$SOURCE_DIR/bridge.py" "$INSTALL_DIR/bridge.py"
/usr/bin/install -o root -g root -m 0755 "$SOURCE_DIR/agent_bridge.py" "$INSTALL_DIR/agent_bridge.py"
/usr/bin/install -o root -g root -m 0755 "$SOURCE_DIR/bridge_payload_mcp.py" "$INSTALL_DIR/bridge_payload_mcp.py"
/usr/bin/install -o root -g root -m 0755 "$SOURCE_DIR/setup_bot.py" "$INSTALL_DIR/setup_bot.py"
/usr/bin/install -o root -g root -m 0644 "$SOURCE_DIR/response_schema.json" "$INSTALL_DIR/response_schema.json"
/usr/bin/install -d -o root -g root -m 0755 /etc/telegram-agent
/usr/bin/install -d -o root -g root -m 0755 /var/lib/telegram-agent
/usr/bin/install -d -o alex -g alex -m 0700 /var/lib/telegram-agent/codex
/usr/bin/install -d -o alex -g alex -m 0700 /var/lib/telegram-agent/grok
/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_DIR/telegram-agent@.service" \
    /etc/systemd/system/telegram-agent@.service
/usr/bin/systemctl daemon-reload

echo "Instance-capable bridge installed. No services were changed or started."
