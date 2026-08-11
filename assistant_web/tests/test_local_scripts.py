import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]


class LocalScriptSafetyTests(unittest.TestCase):
    def test_restart_script_uses_owned_pid_and_graceful_shutdown(self):
        script = (APP_DIR / "scripts" / "local" / "AI비서_서버_재시작.command").read_text(encoding="utf-8")

        self.assertIn('PID_FILE="$RUNTIME_DIR/local-server.pid"', script)
        self.assertIn('kill -TERM "$SERVER_PID"', script)
        self.assertNotIn("xargs kill -9", script)
        self.assertIn("이 스크립트가 관리하지 않는 프로세스", script)

    def test_aws_commands_default_to_key_outside_repository(self):
        for name in ("AWS_시연서버_재배포.command", "AWS_점검모드.command"):
            script = (APP_DIR / "deploy" / "aws" / name).read_text(encoding="utf-8")
            self.assertIn("$HOME/.ssh/Assistance_Key.pem", script)
            self.assertNotIn('SSH_KEY="$SCRIPT_DIR/Assistance_Key.pem"', script)


if __name__ == "__main__":
    unittest.main()
