#!/bin/bash
set -euo pipefail

if [[ $# -ne 0 || "$(id -u)" -ne 0 ]]; then
    echo "Run this one-time bootstrap with sudo and no arguments." >&2
    exit 2
fi

repo_dir="$(cd "$(dirname "$0")" && pwd)"
source_dir="$repo_dir/deploy"
bash -n "$source_dir/deploy-telegram-agent-codex"
bash -n "$source_dir/validate-telegram-agent-codex"
node --check <"$source_dir/49-telegram-agent-deploy-codex.rules"
install -o root -g root -m 0755 "$source_dir/deploy-telegram-agent-codex" /usr/local/sbin/deploy-telegram-agent-codex
install -o root -g root -m 0755 "$source_dir/validate-telegram-agent-codex" /usr/local/sbin/validate-telegram-agent-codex
systemd-analyze verify "$source_dir/telegram-agent-deploy-codex.service"
systemd-analyze verify "$source_dir/telegram-agent-validate-codex.service"
install -o root -g root -m 0644 "$source_dir/telegram-agent-deploy-codex.service" /etc/systemd/system/telegram-agent-deploy-codex.service
install -o root -g root -m 0644 "$source_dir/telegram-agent-validate-codex.service" /etc/systemd/system/telegram-agent-validate-codex.service
install -o root -g root -m 0644 "$source_dir/49-telegram-agent-deploy-codex.rules" /etc/polkit-1/rules.d/49-telegram-agent-deploy-codex.rules
install -d -o root -g root -m 0755 /etc/telegram-agent
deploy_config="$(mktemp /etc/telegram-agent/deploy.env.XXXXXX)"
printf 'BRIDGE_SOURCE_DIR=%s\n' "$repo_dir" >"$deploy_config"
chown root:root "$deploy_config"
chmod 0600 "$deploy_config"
mv -f "$deploy_config" /etc/telegram-agent/deploy.env
systemctl daemon-reload
systemctl reset-failed telegram-agent-deploy-codex.service 2>/dev/null || true
echo "Deploy gate installed. Trigger as alex with: systemctl --no-block start telegram-agent-deploy-codex.service"
