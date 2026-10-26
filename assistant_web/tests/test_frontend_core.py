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
            STATIC_DIR / "auth_scene.js",
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
            "authScene",
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
        self.assertIn("/static/design_system.css", html)
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
        self.assertIn('<section id="personalPanel" class="personal-panel" aria-labelledby="viewPersonalBtn"', html)
        self.assertNotIn('id="workspaceModal"', html)
        self.assertNotIn('id="teamDashboardBtn"', html)
        self.assertNotIn('id="myWorkBtn"', html)
        self.assertIn('dashboard: "/api/workspace/dashboard"', self.application_javascript())
        # 섹션 제목은 한국어만 쓰고 영문 대문자 라벨을 위에 따로 달지 않는다.
        for english_label in ("CUSTOMER HEALTH", "PRIORITY QUEUE", "EARLY SIGNAL", "WORKLOAD", '"TODAY"', "ISSUE DETAIL"):
            self.assertNotIn(english_label, dashboard_js + html)
        self.assertIn('<fieldset id="dashboardPeriod" class="dashboard-period">', html)
        self.assertNotIn('id="dashboardGeneratedAt"', html)
        self.assertNotIn("dashboard-welcome", dashboard_js)
        workspace_js = (STATIC_DIR / "modules" / "workspace.js").read_text(encoding="utf-8")
        self.assertNotRegex(workspace_js, r"<header><div><span>[A-Za-z]")
        self.assertNotIn("WEEKLY TEAM BRIEF", workspace_js)
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
                # PDF 개선안의 제목·지표 크기는 최종 디자인 시스템에서 확장한다.
                allowed_sizes = tds_sizes | {18, 24, 30} if path.name == "design_system.css" else tds_sizes
                self.assertLessEqual(sizes, allowed_sizes)
                self.assertLessEqual(weights, tds_weights)
                # 영문 라벨용 대문자 변환은 쓰지 않는다.
                self.assertNotIn("text-transform: uppercase", text)

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

    def test_data_status_uses_one_badge_and_user_facing_names(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        modules = "\n".join(path.read_text(encoding="utf-8") for path in (STATIC_DIR / "modules").glob("*.js"))

        # 대화·내 업무·대시보드 머리에 같은 '데이터 기준' 배지 자리를 두고, 상태 줄은 모델 문제에만 쓴다.
        for slot_id in ("chatDataBadge", "workspaceDataBadge", "dashboardSource"):
            self.assertIn(f'id="{slot_id}" class="data-badge-slot"', html)
        self.assertIn('<p id="statusLine" hidden></p>', html)
        self.assertIn("data-admin-freshness", modules)
        # 내부 저장소 이름과 제각각이던 상태 문구는 화면에 내보내지 않는다.
        for internal in ("Vector DB", "ChromaDB", "로컬 스냅샷", "전체 건수 미확정", "Mantis 조회 데이터"):
            with self.subTest(term=internal):
                self.assertNotIn(internal, html)
                self.assertNotIn(internal, modules)

    def test_messages_show_date_dividers_instead_of_repeated_dates(self):
        chat_js = (STATIC_DIR / "modules" / "chat.js").read_text(encoding="utf-8")
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        # 질문마다 붙던 '질문한 날짜' 줄 대신 날짜가 바뀌는 자리에만 구분선을 둔다.
        self.assertNotIn("질문한 날짜 · ", chat_js)
        self.assertNotIn("renderQuestionTimestamp", chat_js)
        self.assertIn("if (dayKey && dayKey !== lastDayKey)", chat_js)
        self.assertIn('divider.setAttribute("role", "separator")', chat_js)
        # 시각은 동작 줄 끝에 두고, 터치 화면에서는 항상 보인다.
        self.assertIn("@media (hover: none)", styles)
        self.assertIn(".message:focus-within .message-time", styles)

    def test_focus_ring_and_blue_tints_follow_the_theme_accent(self):
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        dark = styles[styles.index("body.theme-dark {") : styles.index("}", styles.index("body.theme-dark {"))]

        # :root에서 계산된 포커스 색은 다크 모드의 --blue를 따르지 않으므로 다크 테마에서 다시 정의한다.
        self.assertIn("--focus-ring-color: color-mix(in srgb, var(--blue) 16%, transparent);", dark)
        self.assertIn("--focus-ring: 0 0 0 3px var(--focus-ring-color);", dark)
        # 포커스 표시와 파랑 틴트는 차트색 고정값 대신 테마 강조색에서 만든다.
        for path in STATIC_DIR.glob("*.css"):
            with self.subTest(path=path.name):
                self.assertNotIn("rgba(49, 130, 246", path.read_text(encoding="utf-8"))

    def test_buttons_and_inputs_share_height_and_weight_tokens(self):
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        self.assertIn("--button-height-lg: 44px;", styles)
        self.assertIn("--control-height: 44px;", styles)
        # 보조 버튼은 한 곳에서만 정의해 앞선 정의가 뒤에서 덮이지 않게 한다.
        self.assertEqual(styles.count("\n.secondary-button {"), 1)
        for selector in (".auth-primary-button {", ".logout-confirm-button {", ".send-button {"):
            with self.subTest(selector=selector):
                rule = styles[styles.index(selector) : styles.index("}", styles.index(selector))]
                self.assertIn("font-weight: var(--button-weight);", rule)

    def test_chips_share_height_and_font_tokens(self):
        sheets = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in (
            "styles.css", "dashboard.css", "admin_center.css", "template_picker.css")}

        self.assertIn("--chip-height: 32px;", sheets["styles.css"])
        self.assertIn("--chip-height-sm: 28px;", sheets["styles.css"])
        for name, selector, height in (
            ("styles.css", ".search-condition-chip {", "var(--chip-height-sm)"),
            ("styles.css", ".criteria-chip {", "var(--chip-height-sm)"),
            ("styles.css", ".follow-up-chip {", "var(--chip-height)"),
            ("dashboard.css", ".dashboard-chip {", "var(--chip-height)"),
            ("admin_center.css", ".admin-chip {", "var(--chip-height)"),
            ("template_picker.css", ".template-chip span {", "var(--chip-height)"),
        ):
            with self.subTest(selector=selector):
                css = sheets[name]
                rule = css[css.index(selector) : css.index("}", css.index(selector))]
                self.assertIn(height, rule)
                self.assertIn("font-size: var(--chip-font-size);", rule)

    def test_same_role_headings_share_size_tokens_across_views(self):
        sheets = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in (
            "styles.css", "dashboard.css", "admin_center.css", "workspace.css", "template_picker.css")}

        for name, selector, token in (
            ("styles.css", ".chat-header h2 {", "--font-title"),
            ("styles.css", ".workspace-modal-header h2 {", "--font-title"),
            ("dashboard.css", ".dashboard-heading h2 { margin", "--font-title"),
            ("dashboard.css", ".dashboard-detail h3 {", "--font-title"),
            ("admin_center.css", ".admin-section-header h2 {", "--font-title"),
            ("admin_center.css", ".admin-drawer h2 {", "--font-title"),
            ("workspace.css", ".workspace-decision h3 {", "--font-headline"),
            ("template_picker.css", ".template-picker-form-heading h3 {", "--font-headline"),
            ("workspace.css", ".workspace-section h3 {", "--font-section"),
            ("admin_center.css", ".admin-card-head h3 {", "--font-section"),
        ):
            with self.subTest(selector=selector):
                css = sheets[name]
                rule = css[css.index(selector) : css.index("}", css.index(selector))]
                self.assertIn(f"font-size: var({token});", rule)

    def test_interactive_labels_are_at_least_13px(self):
        sheets = {name: (STATIC_DIR / name).read_text(encoding="utf-8") for name in ("styles.css", "workspace.css")}

        # 12px은 시간·건수 같은 보조 정보에만 쓰고, 누르는 요소는 13px 이상으로 둔다.
        for name, selector in (
            ("styles.css", "\n.sidebar-action-button {"),
            ("styles.css", "\n.message-actions button {"),
            ("styles.css", "\n.ab-evaluation-details summary {"),
            ("styles.css", "\n.edit-notice button {"),
            ("workspace.css", "\n.workspace-tabs button {"),
        ):
            with self.subTest(selector=selector.strip()):
                css = sheets[name]
                rule = css[css.index(selector) : css.index("}", css.index(selector))]
                self.assertIn("font-size: 13px;", rule)

    def test_corner_radius_uses_the_shared_scale(self):
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        for token in ("--radius-sm: 6px;", "--control-radius: 8px;", "--radius-card: 12px;", "--radius-lg: 16px;"):
            self.assertIn(token, styles)
        # 8px과 눈으로 구분되지 않던 7·9·10·11·14px 모서리는 단계 값으로 합쳤다.
        for path in STATIC_DIR.glob("*.css"):
            with self.subTest(path=path.name):
                css = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"border-radius:\s*(7|9|10|11|14)px", css))

    def test_views_share_one_heading_hierarchy(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        modules = {name: (STATIC_DIR / "modules" / f"{name}.js").read_text(encoding="utf-8")
                   for name in ("admin", "admin_users", "dashboard", "workspace")}

        # 앱 제목 h1 하나 아래에 화면 제목은 h2, 화면 안 구역 제목은 h3로 둔다.
        self.assertEqual(len(re.findall(r"<h1\b", html)), 1)
        for title_id in ("activeConversationTitle", "workspaceModalTitle"):
            with self.subTest(title=title_id):
                self.assertIn(f'<h2 id="{title_id}">', html)
        self.assertIn("<h2>팀 대시보드</h2>", html)
        for name, source in modules.items():
            with self.subTest(module=name):
                self.assertNotRegex(source, r"<h1\b")
                self.assertNotRegex(source, r"<h4\b")
        self.assertIn('<header class="admin-section-header"><div><h2>', modules["admin"])
        self.assertIn('<div class="admin-card-head"><h3', modules["admin"])

        # 답변 본문도 대화 제목(h2) 바로 아래 단계인 h3에서 시작한다. h4는 이슈 상세 카드(h3) 안에서만 쓴다.
        answer = {name: (STATIC_DIR / "modules" / f"{name}.js").read_text(encoding="utf-8")
                  for name in ("chat_rendering", "notion", "charts")}
        self.assertIn('document.createElement("h3");\n      heading.className = "answer-heading";', answer["chat_rendering"])
        self.assertIn("<h3>후속 질문</h3>", answer["chat_rendering"])
        for name in ("notion", "charts"):
            with self.subTest(module=name):
                self.assertNotRegex(answer[name], r"<h4\b")

    def test_dashboard_rows_use_real_buttons_instead_of_button_roles(self):
        dashboard_js = (STATIC_DIR / "modules" / "dashboard.js").read_text(encoding="utf-8")
        dashboard_css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

        self.assertNotIn('role="button"', dashboard_js)
        self.assertNotIn('els.dashboardBody.addEventListener("keydown"', dashboard_js)
        self.assertIn('target.closest("[data-dashboard-row-issue]")', dashboard_js)
        # 차트 막대·경과 열도 버튼 역할이 아닌 실제 버튼이며 기본 버튼 모양만 걷어 낸다.
        self.assertIn('<button type="button" class="dashboard-column dashboard-clickable', dashboard_js)
        self.assertIn("button.dashboard-bar,", dashboard_css)

    def test_mobile_menu_is_a_left_drawer_and_send_sits_inside_the_composer(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        responsive = (STATIC_DIR / "responsive.css").read_text(encoding="utf-8")
        events_js = (STATIC_DIR / "modules" / "events.js").read_text(encoding="utf-8")

        # 버튼 역할만 붙인 요소를 두지 않고, 로고와 메뉴 닫기는 실제 버튼이다.
        self.assertNotIn('role="button"', html)
        self.assertIn('<button id="brandHome" class="rail-brand" type="button"', html)
        self.assertIn('id="sidebarCloseBtn"', html)
        self.assertNotIn('els.brandHome.addEventListener("keydown"', events_js)
        # 좁은 화면 메뉴는 위에서 내려오는 82vh 오버레이가 아니라 왼쪽 전체 높이 드로어다.
        self.assertNotIn("max-height: 82vh", responsive)
        self.assertIn("transform: translateX(-100%)", responsive)
        # 전송은 입력창 안 원형 버튼으로, 입력창 아래 한 줄을 차지하지 않는다.
        self.assertIn('class="ui-icon send-icon"', html)
        self.assertIn('class="ui-icon stop-icon"', html)
        self.assertIn("border-radius: 999px", responsive)
        template_css = (STATIC_DIR / "template_picker.css").read_text(encoding="utf-8")
        self.assertNotIn(".composer .send-button {\n    grid-column: 1 / -1;", template_css)

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

    def test_main_menu_is_one_rail_and_chat_list_only_shows_on_chat(self):
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        responsive = (STATIC_DIR / "responsive.css").read_text(encoding="utf-8")
        dashboard_js = (STATIC_DIR / "modules" / "dashboard.js").read_text(encoding="utf-8")

        # 상단 탭 줄과 화면마다 따로 있던 메뉴 버튼 대신 주 메뉴 하나로 화면을 옮긴다.
        self.assertNotIn("view-switch", html)
        self.assertNotIn('role="tabpanel" aria-labelledby="view', html)
        for removed_id in ("personalSidebarMenuToggle", "dashboardSidebarMenuToggle", "adminSidebarMenuToggle"):
            self.assertNotIn(f'id="{removed_id}"', html)
        self.assertEqual(html.count('class="sidebar-menu-toggle'), 1)
        rail = html[html.index('<nav class="app-rail" aria-label="주 메뉴">') : html.index("</nav>", html.index('class="app-rail"'))]
        for element_id in ("brandHome", "viewChatBtn", "viewPersonalBtn", "viewDashboardBtn", "viewAdminBtn", "accountMenuBtn", "signedInUser", "logoutBtn", "darkModeToggle"):
            self.assertIn(f'id="{element_id}"', rail)
        self.assertIn('aria-current="page"', rail)
        self.assertNotIn('role="tab"', rail)
        # 대화 목록은 대화 화면에서만 보이고, 좁은 화면에서는 주 메뉴가 아래 탭바가 된다.
        self.assertIn('<main class="app-shell" data-view="chat">', html)
        self.assertIn('.app-shell:not([data-view="chat"]) .sidebar', styles)
        self.assertIn("els.appShell.dataset.view = view", dashboard_js)
        self.assertIn("border-top: 1px solid var(--line)", responsive)
        self.assertIn("--rail-h", responsive)

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
        for element_id in ("darkModeToggle", "showSources"):
            self.assertIn(element_id, rest_parser.ids)
        # 답변 형식은 질문 문구로 지정하므로 별도 선택 컨트롤을 두지 않는다.
        self.assertNotIn("answerStyle", rest_parser.ids)
        self.assertIn("els.adminGenerationSettings.hidden = !adminToolsVisible", bootstrap_js)

    def test_query_basis_block_is_drawn_smaller_than_the_answer(self):
        rendering = (STATIC_DIR / "modules" / "chat_rendering.js").read_text(encoding="utf-8")
        basis_js = (STATIC_DIR / "modules" / "query_basis.js").read_text(encoding="utf-8")
        styles = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

        # 결정형 업무 답변의 '조회 기준:' 묶음은 문단이 아니라 작은 회색 영역으로 그린다.
        self.assertIn('cleaned.startsWith("조회 기준:")', rendering)
        self.assertIn("renderQueryBasis(rawBlock)", rendering)
        self.assertIn('box.className = "answer-basis";', basis_js)
        self.assertIn("item.textContent = text;", basis_js)
        basis = styles[styles.index(".answer-basis {") : styles.index("}", styles.index(".answer-basis {"))]
        self.assertIn("color: var(--muted);", basis)
        self.assertIn("font-size: 13px;", basis)

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

        dashboard_js = (STATIC_DIR / "modules" / "dashboard.js").read_text(encoding="utf-8")
        workspace_js = (STATIC_DIR / "modules" / "workspace.js").read_text(encoding="utf-8")

        # 주간 브리핑은 내 업무 › 주간 보고 한 곳에서 '내 보고 / 팀 브리핑'으로 고른다.
        self.assertNotIn('data-workspace-tab="team-brief"', html)
        self.assertIn('data-workspace-tab="brief">주간 보고', html)
        self.assertIn('data-brief-kind="team"', workspace_js)
        self.assertIn('teamBrief: "/api/workspace/team-brief"', app_js)
        self.assertIn("data-refresh-team-brief", app_js)
        self.assertIn("await markTeamWeeklyBriefRead()", workspace_js)
        # 대시보드는 같은 내용을 다시 보여 주지 않고 주간 보고로 안내만 한다.
        self.assertNotIn("team-brief-section", dashboard_js)
        self.assertNotIn("renderTeamWeeklyBrief", dashboard_js)
        self.assertIn("data-open-team-brief", dashboard_js)

    def test_personal_workspace_keeps_summary_visible_and_scrolls_issue_list(self):
        workspace_js = (STATIC_DIR / "modules" / "workspace.js").read_text(encoding="utf-8")
        workspace_css = (STATIC_DIR / "workspace.css").read_text(encoding="utf-8")
        dashboard_css = (STATIC_DIR / "dashboard.css").read_text(encoding="utf-8")

        self.assertIn('class="workspace-list workspace-issue-list"', workspace_js)
        self.assertIn('class="workspace-overview"', workspace_js)
        self.assertIn('role="region" aria-label="내 담당 미종료 이슈 목록" tabindex="0"', workspace_js)
        self.assertIn('classList.toggle("is-overview", isPersonalOverview)', workspace_js)
        # 표시 건수는 목록 머리 한 곳에서만 보여 준다.
        self.assertIn("` / ${totalOpen}건 표시`", workspace_js)
        self.assertEqual(workspace_js.count("totalOpen}건 표시"), 1)
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
