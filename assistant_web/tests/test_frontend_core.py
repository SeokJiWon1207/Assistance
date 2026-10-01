import re
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
            [
                node,
                "--experimental-vm-modules",
                "--test",
                *map(str, sorted((APP_DIR / "tests").glob("frontend*.test.cjs"))),
            ],
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
            "adminPanel",
            "templatePicker",
            "templateForm",
            "templateToggleBtn",
            "feedbackModal",
            "feedbackForm",
            "authGate",
            "loginForm",
            "signedInUser",
            "logoutConfirmModal",
            "logoutConfirmBtn",
            "logoutCancelBtn",
            "viewAdminBtn",
            "adminContent",
            "adminDrawer",
            "adminDrawerForm",
        ):
            self.assertIn(required_id, parser.ids)
        self.assertLess(html.index("/static/auth.js"), html.index("/static/app.js"))
        self.assertLess(html.index("/static/frontend_core.js"), html.index("/static/app.js"))
        self.assertIn('type="module" src="/static/app.js', html)

    def test_stylesheets_are_split_by_responsibility_in_cascade_order(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        stylesheet_names = (
            "styles.css",
            "charts.css",
            "responsive.css",
            "workspace.css",
            "dashboard.css",
            "template_picker.css",
            "conversation_list.css",
            "admin_center.css",
        )
        positions = []
        for name in stylesheet_names:
            path = STATIC_DIR / name
            self.assertTrue(path.is_file(), name)
            self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()), 4500)
            marker = f"/static/{name}?v="
            self.assertIn(marker, html)
            positions.append(html.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_operations_dashboard_is_connected_to_chat_and_responsive_ui(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        dashboard_js = (STATIC_DIR / "modules" / "dashboard.js").read_text(encoding="utf-8")
        dashboard_css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

        for element_id in (
            "viewDashboardBtn",
            "viewChatBtn",
            "dashboardPanel",
            "dashboardPeriod",
            "dashboardBody",
            "dashboardDetail",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertLess(html.index('id="viewChatBtn"'), html.index('id="viewPersonalBtn"'))
        self.assertLess(html.index('id="viewPersonalBtn"'), html.index('id="viewDashboardBtn"'))
        self.assertIn('id="personalPanel" class="personal-panel" role="tabpanel"', html)
        self.assertNotIn('id="workspaceModal"', html)
        self.assertNotIn('id="teamDashboardBtn"', html)
        self.assertNotIn('id="myWorkBtn"', html)
        self.assertIn('dashboard: "/api/workspace/dashboard"', self.application_javascript())
        self.assertIn("CUSTOMER HEALTH", dashboard_js)
        self.assertIn("data-dashboard-question", dashboard_js)
        self.assertIn("showChatView();\n  els.input.value = question", dashboard_js)
        self.assertIn("dashboardState.customer", dashboard_js)
        self.assertIn("@media (max-width: 860px)", dashboard_css)
        self.assertIn("@media (max-width: 560px)", dashboard_css)

    def test_stylesheets_use_tds_type_sizes_and_weights(self):
        # TDS 단계 중 앱에서 쓰기로 한 9단계와 Light~Bold 굵기만 쓴다.
        # 12 캡션 · 13 보조 · 14 목록 · 15 버튼 · 16 본문 · 17 카드 제목 · 20 화면 제목 · 22 섹션 제목 · 26 지표 숫자
        tds_sizes = {12, 13, 14, 15, 16, 17, 20, 22, 26}
        tds_weights = {300, 400, 500, 600, 700}
        for path in sorted(STATIC_DIR.glob("*.css")):
            # @font-face의 굵기 범위는 글꼴 파일이 지원하는 범위라 화면 스타일 검사에서 뺀다.
            text = re.sub(r"@font-face\s*\{[^}]*\}", "", path.read_text(encoding="utf-8"))
            with self.subTest(stylesheet=path.name):
                sizes = {float(value) for value in re.findall(r"font-size:\s*([0-9.]+)px", text)}
                weights = {int(value) for value in re.findall(r"font-weight:\s*([0-9]+)", text)}
                self.assertLessEqual(sizes, tds_sizes)
                self.assertLessEqual(weights, tds_weights)

    def test_icons_come_from_one_sprite_instead_of_text_glyphs(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        modules = {path.name: path.read_text(encoding="utf-8") for path in (STATIC_DIR / "modules").glob("*.js")}
        symbols = set(re.findall(r'<symbol id="i-([a-z-]+)" viewBox="0 0 24 24">', html))

        # HTML의 <use>와 JS의 icon("이름")이 가리키는 아이콘은 모두 스프라이트에 있어야 한다.
        used = set(re.findall(r'href="#i-([a-z-]+)"', html))
        for source in modules.values():
            used |= set(re.findall(r'\bicon\("([a-z-]+)"', source))
        self.assertTrue(used)
        self.assertLessEqual(used, symbols)
        # 버튼 안 문자 기호와 20 격자 아이콘은 남기지 않는다.
        self.assertNotRegex(html, r">[☰＋×↻]</button>")
        self.assertNotIn('viewBox="0 0 24 24" aria-hidden="true"><path', html)
        for name, source in modules.items():
            with self.subTest(module=name):
                self.assertNotIn('viewBox="0 0 20 20"', source)
        for path in STATIC_DIR.glob("*.css"):
            with self.subTest(stylesheet=path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r'content:\s*"[⌄↻×]"')

    def test_dashboard_sections_keep_content_height_in_scroll_grid(self):
        dashboard_css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")
        body_rule = dashboard_css[dashboard_css.index(".dashboard-body {") :]
        body_rule = body_rule[: body_rule.index("}")]

        # overflow: hidden 섹션이 스크롤 grid 안에서 0 높이로 눌리지 않아야 한다.
        self.assertIn("overflow-y: auto", body_rule)
        self.assertIn("grid-auto-rows: max-content", body_rule)

    def test_chat_composer_stays_inside_tabbed_main_view(self):
        dashboard_css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn(".main-view-shell > .chat-panel {", dashboard_css)
        self.assertIn("height: auto", dashboard_css)
        self.assertIn("min-height: 0", dashboard_css)
        self.assertNotIn(".main-view-shell > .chat-panel { height: 100%; }", dashboard_css)

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
        users_js = (STATIC_DIR / "modules" / "admin_users.js").read_text(encoding="utf-8")

        self.assertIn('apiFetch("/api/auth/users")', users_js)
        self.assertIn('method: "PATCH"', users_js)
        self.assertIn("mantisUsername: form.elements.mantisUsername.value.trim()", users_js)
        # 승인 대기 사용자는 편집 패널을 열지 않고 목록에서 바로 승인할 수 있다.
        self.assertIn("patchUser(uid, { approved: true })", users_js)
        self.assertIn('apiFetch("/api/auth/users/invite"', users_js)

    def test_admin_tools_live_in_one_admin_center_view(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        auth_js = (STATIC_DIR / "auth.js").read_text(encoding="utf-8")
        admin_js = (STATIC_DIR / "modules" / "admin.js").read_text(encoding="utf-8")
        dashboard_js = (STATIC_DIR / "modules" / "dashboard.js").read_text(encoding="utf-8")

        # 흩어져 있던 모달 세 개와 문자 기호 버튼 대신 화면 탭 하나로 들어간다.
        for removed_id in ("evaluationModal", "userManagementModal", "inviteModal", "adminUsersBtn", "adminInviteBtn", "evaluationBtn"):
            self.assertNotIn(f'id="{removed_id}"', html)
        self.assertLess(html.index('id="viewDashboardBtn"'), html.index('id="viewAdminBtn"'))
        admin_tab = html[html.index('id="viewAdminBtn"') : html.index("</button>", html.index('id="viewAdminBtn"'))]
        self.assertIn(" hidden>", admin_tab)
        self.assertIn('["admin", els.viewAdminBtn, els.adminPanel]', dashboard_js)
        self.assertIn('if (view === "admin" && !canViewAdminCenter()) view = "chat";', dashboard_js)
        for section in ("overview", "run", "feedback", "logs", "users"):
            self.assertIn(f'data-admin-section="{section}"', html)
        # 권한과 답변 방식은 내부 영문 값 대신 한국어로 보여 준다.
        self.assertIn('ROLE_LABELS = { member: "팀원", manager: "팀장", admin: "관리자" }', auth_js)
        self.assertIn("modeLabel(log.mode)", admin_js)
        self.assertIn('return "자동 집계";', admin_js)
        self.assertNotIn("Firebase", html[html.index('id="adminPanel"') :])

    def test_generation_parameters_are_admin_only(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        bootstrap_js = (STATIC_DIR / "modules" / "bootstrap.js").read_text(encoding="utf-8")

        start = html.index('<details id="adminGenerationSettings"')
        end = html.index("</details>", start)
        admin_section = html[start:end]
        admin_parser = IdCollector()
        admin_parser.feed(admin_section)
        rest_parser = IdCollector()
        rest_parser.feed(html[:start] + html[end:])

        self.assertIn(" hidden>", admin_section.split("\n", 1)[0])
        for element_id in (
            "temperature",
            "topP",
            "topK",
            "minP",
            "maxTokens",
            "presencePenalty",
            "frequencyPenalty",
            "repetitionPenalty",
            "seed",
            "stop",
            "searchLimit",
            "abTestToggle",
        ):
            self.assertIn(element_id, admin_parser.ids)
        for element_id in ("answerStyle", "darkModeToggle", "showSources"):
            self.assertIn(element_id, rest_parser.ids)
        self.assertIn("els.adminGenerationSettings.hidden = !adminToolsVisible", bootstrap_js)

    def test_template_picker_sits_beside_composer_without_nested_forms(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        events_js = (STATIC_DIR / "modules" / "events.js").read_text(encoding="utf-8")

        chat_form = html[html.index('<form id="chatForm"') : html.index("</form>", html.index('<form id="chatForm"'))]
        self.assertIn('id="templateToggleBtn"', chat_form)
        self.assertNotIn("<form", chat_form[1:])
        self.assertNotIn('id="templateModal"', html)
        picker_start = html.index('<div id="templatePicker"')
        self.assertLess(html.index('<form id="chatForm"'), picker_start)
        self.assertLess(picker_start, html.index('<form id="templateForm"'))
        self.assertIn("if (handleTemplatePickerKeydown(event)) return;", events_js)
        self.assertIn("syncTemplatePickerWithPrompt();", events_js)

    def test_chat_rate_limit_uses_retry_after_message(self):
        app_js = self.application_javascript()

        self.assertIn('response.headers.get("Retry-After")', app_js)
        self.assertIn("response.status === 429", app_js)
        self.assertIn("초 후 다시 시도", app_js)

    def test_team_brief_tab_and_snapshot_actions_are_connected(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        app_js = self.application_javascript()

        self.assertNotIn('data-workspace-tab="team-brief"', html)
        self.assertIn('class="team-brief-section"', app_js)
        self.assertIn('teamBrief: "/api/workspace/team-brief"', app_js)
        self.assertIn("data-refresh-team-brief", app_js)
        self.assertIn("markTeamWeeklyBriefRead", app_js)

    def test_personal_workspace_keeps_summary_visible_and_scrolls_issue_list(self):
        workspace_js = (STATIC_DIR / "modules" / "workspace.js").read_text(encoding="utf-8")
        workspace_css = (STATIC_DIR / "workspace.css").read_text(encoding="utf-8")
        dashboard_css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn('class="workspace-list workspace-issue-list"', workspace_js)
        self.assertIn('class="workspace-overview"', workspace_js)
        self.assertIn('role="region" aria-label="내 담당 미종료 이슈 목록" tabindex="0"', workspace_js)
        self.assertIn('classList.toggle("is-overview", isPersonalOverview)', workspace_js)
        self.assertIn("`${issues.length} / ${totalOpen}건 표시`", workspace_js)
        self.assertIn(".personal-panel {", dashboard_css)
        self.assertIn("grid-template-rows: auto auto minmax(0, 1fr)", dashboard_css)
        self.assertIn(".workspace-content.is-overview", workspace_css)
        self.assertIn(".workspace-overview > .workspace-section", workspace_css)
        self.assertIn(".workspace-issue-list {", workspace_css)
        self.assertIn("max-height: none", workspace_css)
        self.assertIn("overflow-y: auto", workspace_css)
        self.assertIn("scrollbar-gutter: stable", workspace_css)
        self.assertIn(".workspace-summary-grid.is-three", workspace_css)
        self.assertIn(
            'els.viewPersonalBtn.addEventListener("click", () => showPersonalView())', self.application_javascript()
        )

    def test_mantis_status_labels_match_source_ui(self):
        expected_labels = {
            "new": "신규 이슈",
            "feedback": "정보 부족",
            "acknowledged": "이슈 검토",
            "confirmed": "확인된 이슈",
            "assigned": "할당된 이슈",
            "developed": "개발완료",
            "resolved": "해결된 이슈",
            "closed": "종료된 이슈",
        }
        workspace_js = (STATIC_DIR / "modules" / "workspace.js").read_text(encoding="utf-8")
        chat_source_detail_js = (STATIC_DIR / "modules" / "chat_source_detail.js").read_text(encoding="utf-8")

        for code, label in expected_labels.items():
            mapping = f'{code}: "{label}"'
            self.assertIn(mapping, workspace_js)
            self.assertIn(mapping, chat_source_detail_js)

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
