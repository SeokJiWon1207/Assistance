import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_DIR / "backend" / "static"


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, _tag, attrs):
        element_id = dict(attrs).get("id")
        if element_id:
            self.ids.append(element_id)


class FrontendCoreTests(unittest.TestCase):
    @staticmethod
    def application_javascript():
        paths = [STATIC_DIR / "app.js", *sorted((STATIC_DIR / "modules").glob("*.js"))]
        return "\n".join(path.read_text(encoding="utf-8") for path in paths)

    def test_frontend_core_node_suite(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js가 없어 프런트엔드 단위 테스트를 건너뜁니다.")
        result = subprocess.run(
            [node, "--test", str(APP_DIR / "tests" / "frontend_core.test.cjs")],
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_frontend_scripts_are_valid_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js가 없어 JavaScript 구문 검사를 건너뜁니다.")
        scripts = [
            STATIC_DIR / "frontend_core.js",
            STATIC_DIR / "auth.js",
            STATIC_DIR / "app.js",
            *sorted((STATIC_DIR / "modules").glob("*.js")),
        ]
        for script in scripts:
            with self.subTest(script=script):
                result = subprocess.run(
                    [node, "--check", str(script)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_frontend_has_unique_ids_and_loads_core_before_app(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        parser = IdCollector()
        parser.feed(html)

        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        for required_id in (
            "chatForm",
            "promptInput",
            "sendBtn",
            "messages",
            "newChatBtn",
            "evaluationModal",
            "sourceModeSelect",
            "manualSourceControls",
            "templateModal",
            "templateForm",
            "feedbackModal",
            "feedbackForm",
            "authGate",
            "loginForm",
            "signedInUser",
            "logoutConfirmModal",
            "logoutConfirmBtn",
            "logoutCancelBtn",
            "inviteModal",
            "inviteForm",
            "adminUsersBtn",
            "userManagementModal",
            "userManagementList",
        ):
            self.assertIn(required_id, parser.ids)
        self.assertLess(html.index("/static/auth.js"), html.index("/static/app.js"))
        self.assertLess(html.index("/static/frontend_core.js"), html.index("/static/app.js"))
        self.assertIn('type="module" src="/static/app.js', html)

    def test_notion_controls_are_hidden_in_mantis_focused_ui(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        app_js = self.application_javascript()

        self.assertNotIn('id="notionToggle"', html)
        self.assertNotIn("Notion 업무", app_js)
        self.assertIn("return { mantis: true, notion: true, vectorDb: true };", app_js)

    def test_logout_requires_confirmation(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        auth_js = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")

        self.assertIn("로그아웃하시겠습니까?", html)
        self.assertIn('logoutButton?.addEventListener("click", open)', auth_js)
        self.assertIn('confirmButton?.addEventListener("click", async () =>', auth_js)
        self.assertIn('apiFetch("/api/auth/logout", { method: "POST" })', auth_js)

    def test_admin_can_manage_existing_users_without_invite(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        auth_js = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")

        self.assertIn("사용자 관리", html)
        self.assertIn('apiFetch("/api/auth/users")', auth_js)
        self.assertIn('method: "PATCH"', auth_js)
        self.assertIn("mantisUsername: form.elements.mantisUsername.value.trim()", auth_js)

    def test_chat_rate_limit_uses_retry_after_message(self):
        app_js = self.application_javascript()

        self.assertIn('response.headers.get("Retry-After")', app_js)
        self.assertIn("response.status === 429", app_js)
        self.assertIn("초 후 다시 시도", app_js)

    def test_team_brief_tab_and_snapshot_actions_are_connected(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        app_js = self.application_javascript()

        self.assertIn('data-workspace-tab="team-brief"', html)
        self.assertIn('teamBrief: "/api/workspace/team-brief"', app_js)
        self.assertIn("data-refresh-team-brief", app_js)
        self.assertIn("markTeamWeeklyBriefRead", app_js)

    def test_playbook_search_and_curator_actions_are_connected(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        app_js = self.application_javascript()

        self.assertIn('data-workspace-tab="playbooks"', html)
        self.assertIn('playbooks: "/api/playbooks"', app_js)
        self.assertIn("data-save-playbook-candidate", app_js)
        self.assertIn("data-playbook-editor", app_js)
        self.assertIn("changePlaybookStatus", app_js)


if __name__ == "__main__":
    unittest.main()
