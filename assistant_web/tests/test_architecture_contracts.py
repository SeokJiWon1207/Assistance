import ast
import json
import unittest
from pathlib import Path

from backend.intent.constraints import extract_query_constraints
from backend.intent.source_router import rule_based_search_intent
from backend.query_planning import build_query_plan
from backend.notion.formatting import build_sources_from_notion_pages, format_deterministic_notion_answer


APP_DIR = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((APP_DIR / "tests" / "fixtures" / "architecture_contracts.json").read_text(encoding="utf-8"))


class PythonArchitectureContractTests(unittest.TestCase):
    def test_chat_orchestrator_stays_phase_oriented(self):
        path = APP_DIR / "backend" / "services" / "chat_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        chat_service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatService")
        build_response = next(
            node for node in chat_service.body if isinstance(node, ast.FunctionDef) and node.name == "_build_response"
        )
        self.assertLessEqual(build_response.end_lineno - build_response.lineno + 1, 80)
        imported_functions = {
            alias.asname or alias.name: node.module
            for node in tree.body if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        for phase_function, owner in {
            "build_deterministic_report": "reports",
            "prepare_chat_pipeline": "prepare",
            "plan_chat_query": "planning",
            "retrieve_chat_evidence": "retrieve",
            "generate_model_response": "generate",
        }.items():
            with self.subTest(phase_function=phase_function):
                self.assertEqual(imported_functions[phase_function], f"backend.services.chat.{owner}")
        self.assertEqual([ast.unparse(node) for node in build_response.decorator_list],
                         ["request_execution", "request_timing"])

    def test_query_plan_golden_cases(self):
        for case in FIXTURE["queryPlans"]:
            question = case["question"]
            constraints = extract_query_constraints(question)
            source_plan = rule_based_search_intent(question, constraints)
            plan = build_query_plan(question, source_plan, constraints)
            with self.subTest(question=question):
                self.assertEqual(plan["source"], case["source"])
                self.assertEqual(plan["operation"], case["operation"])
                for key, expected in case["filters"].items():
                    self.assertEqual(plan["filters"][key], expected)

    def test_notion_source_and_final_answer_golden_case(self):
        pages = FIXTURE["notionPages"]
        sources = build_sources_from_notion_pages(pages)
        self.assertEqual(sources[0]["page_id"], "page-1")
        self.assertEqual(sources[0]["preview"], "배포 전 체크리스트")
        retrieval_result = {
            "intent_plan": {"source": "notion"},
            "stages": [
                {
                    "source": "notion",
                    "status": "ok",
                    "notion_pages": pages,
                    "meta": {
                        "candidate_count": 1,
                        "terms": ["배포"],
                        "databases": [{"database_label": "업무 DB"}],
                        "constraints": {},
                    },
                }
            ],
        }
        answer = format_deterministic_notion_answer("노션에서 배포 문서 보여줘", retrieval_result, 5)
        self.assertEqual(answer, FIXTURE["notionAnswer"])


class FrontendArchitectureContractTests(unittest.TestCase):
    def test_bootstrap_only_initializes_functional_modules(self):
        modules_dir = APP_DIR / "backend" / "static" / "modules"
        bootstrap = (modules_dir / "bootstrap.js").read_text(encoding="utf-8")
        self.assertLessEqual(len(bootstrap.splitlines()), 300)
        expected_owners = {
            "workspace.js": "function renderWorkspace()",
            "chat.js": "function renderMessages()",
            "chat_controller.js": "async function requestAssistant(",
            "chat_feedback.js": "async function saveMessageFeedback(",
            "chat_rendering.js": "function renderAssistantContent(",
            "chat_source_detail.js": "async function openSourceDetail(",
            "charts.js": "export function renderDonutChart(",
            "notion.js": "function renderNotionAnswer(",
            "admin.js": "function renderEvaluationContent()",
            "admin_users.js": "function renderUsersSection(",
            "events.js": 'els.form.addEventListener("submit"',
            "runtime.js": "export const state =",
        }
        for filename, marker in expected_owners.items():
            with self.subTest(filename=filename):
                content = (modules_dir / filename).read_text(encoding="utf-8")
                self.assertIn(marker, content)
                self.assertNotIn(marker, bootstrap)
        chat = (modules_dir / "chat.js").read_text(encoding="utf-8")
        self.assertNotIn("function renderDonutChart(", chat)
        self.assertNotIn("async function requestAssistant(", chat)
        self.assertNotIn("function renderAssistantContent(", chat)
        for module in ("charts", "chat_controller", "chat_rendering"):
            self.assertRegex(chat, rf'from "\./{module}\.js(?:\?v=[^"\s]+)?"')
        self.assertLessEqual(len(chat.splitlines()), 950)
        rendering = (modules_dir / "chat_rendering.js").read_text(encoding="utf-8")
        self.assertLessEqual(len(rendering.splitlines()), 850)

    def test_dom_and_api_contracts_are_present(self):
        static_dir = APP_DIR / "backend" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        javascript = "\n".join(path.read_text(encoding="utf-8") for path in sorted(static_dir.rglob("*.js")))
        for element_id in (
            "chatForm",
            "promptInput",
            "messages",
            "signedInUser",
            "adminPanel",
            "personalPanel",
            "dashboardPanel",
            "adminDrawer",
        ):
            self.assertIn(f'id="{element_id}"', html)
        for endpoint in (
            "/api/chat",
            "/api/models",
            "/api/workspace/me",
            "/api/workspace/team",
            "/api/playbooks",
            "/api/evaluation/run",
        ):
            self.assertIn(endpoint, javascript)
        for event_contract in (
            'addEventListener("submit"',
            'addEventListener("click"',
            'addEventListener("keydown"',
        ):
            self.assertIn(event_contract, javascript)


if __name__ == "__main__":
    unittest.main()
