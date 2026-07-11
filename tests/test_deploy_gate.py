import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


class DeployGateTests(unittest.TestCase):
    def test_helper_is_fixed_no_argument_workflow(self):
        helper = DEPLOY / "deploy-telegram-agent-codex"
        text = helper.read_text()
        self.assertIn("[[ $# -ne 0", text)
        self.assertIn("files=(bridge.py agent_bridge.py bridge_payload_mcp.py setup_bot.py response_schema.json)", text)
        self.assertNotIn("install_instances.sh", text)
        self.assertIn("sha256sum", text)
        self.assertIn("rollback", text)
        self.assertIn("stable_checks", text)
        result = subprocess.run([str(helper), "unexpected"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_policy_allows_only_exact_start_action(self):
        rule = (DEPLOY / "49-telegram-agent-deploy-codex.rules").read_text()
        self.assertIn('subject.user === "alex"', rule)
        self.assertIn('action.id === "org.freedesktop.systemd1.manage-units"', rule)
        self.assertIn('action.lookup("unit") === "telegram-agent-deploy-codex.service"', rule)
        self.assertIn('action.lookup("verb") === "start"', rule)
        self.assertNotIn("isInGroup", rule)

    def test_validator_runs_as_alex_without_uid_switching(self):
        helper = (DEPLOY / "deploy-telegram-agent-codex").read_text()
        validator = (DEPLOY / "telegram-agent-validate-codex.service").read_text()
        self.assertIn("systemctl start telegram-agent-validate-codex.service", helper)
        self.assertNotIn("runuser", helper)
        self.assertNotIn("setpriv", helper)
        self.assertIn("User=alex", validator)
        self.assertIn("NoNewPrivileges=true", validator)

    def test_unit_runs_only_root_owned_fixed_helper(self):
        unit = (DEPLOY / "telegram-agent-deploy-codex.service").read_text()
        self.assertIn("ExecStart=/usr/local/sbin/deploy-telegram-agent-codex", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertNotIn(".codex/generated_images", unit)
        self.assertNotIn("install_instances.sh", unit)


if __name__ == "__main__":
    unittest.main()
