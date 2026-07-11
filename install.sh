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
/usr/bin/install -o root -g root -m 0755 "$SOURCE_DIR/setup_bot.py" "$INSTALL_DIR/setup_bot.py"
/usr/bin/install -o root -g root -m 0644 "$SOURCE_DIR/response_schema.json" "$INSTALL_DIR/response_schema.json"
/usr/bin/install -d -o alex -g alex -m 0700 /var/lib/codex-telegram-bridge
/usr/bin/install -o root -g root -m 0644 \
    "$SOURCE_DIR/codex-telegram-bridge.service" \
    /etc/systemd/system/codex-telegram-bridge.service
/usr/bin/systemctl daemon-reload

echo "Bridge files installed. The service was not enabled or started."
