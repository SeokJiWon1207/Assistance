import unittest
import unicodedata
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
COMMANDS_DIR = APP_DIR / "scripts" / "commands"


class LocalScriptSafetyTests(unittest.TestCase):
    def test_restart_script_uses_owned_pid_and_graceful_shutdown(self):
        script = (COMMANDS_DIR / "AI비서_서버_재시작.command").read_text(encoding="utf-8")

        self.assertIn('PID_FILE="$RUNTIME_DIR/local-server.pid"', script)
        self.assertIn('kill -TERM "$SERVER_PID"', script)
        self.assertNotIn("xargs kill -9", script)
        self.assertIn("이 스크립트가 관리하지 않는 프로세스", script)
        self.assertIn("-sTCP:LISTEN", script)
        self.assertIn("포트 ${PORT}를", script)

    def test_aws_commands_default_to_key_outside_repository(self):
        for name in ("AWS_시연서버_재배포.command", "AWS_점검모드.command"):
            script = (COMMANDS_DIR / name).read_text(encoding="utf-8")
            self.assertIn("$HOME/.ssh/Assistance_Key.pem", script)
            self.assertNotIn('SSH_KEY="$SCRIPT_DIR/Assistance_Key.pem"', script)

    def test_user_commands_are_managed_in_one_directory(self):
        command_names = sorted(unicodedata.normalize("NFC", path.name) for path in COMMANDS_DIR.glob("*.command"))
        self.assertEqual(
            command_names,
            [
                "AI비서_서버_재시작.command",
                "AWS_시연서버_재배포.command",
                "AWS_점검모드.command",
            ],
        )
        self.assertEqual(list((APP_DIR / "scripts" / "local").glob("*.command")), [])
        deploy_commands = list((APP_DIR / "deploy" / "aws").glob("*.command"))
        self.assertEqual(deploy_commands, [])

    def test_redeploy_command_calls_the_deploy_implementation(self):
        script = (COMMANDS_DIR / "AWS_시연서버_재배포.command").read_text(encoding="utf-8")
        self.assertIn(
            '"$PROJECT_DIR/assistant_web/deploy/aws/deploy_to_ec2.sh"',
            script,
        )
        self.assertIn("RELEASE_KEEP=1", script)


if __name__ == "__main__":
    unittest.main()
