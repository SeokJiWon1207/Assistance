import ast
import json
import unittest
from pathlib import Path

from backend import answers, understanding
from backend.intent import scope_extraction
from backend.query_planning import build_query_plan
from backend.reports import evaluation
from backend.sources import notion


APP_DIR = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((APP_DIR / "tests" / "fixtures" / "architecture_contracts.json").read_text(encoding="utf-8"))


PUBLIC_SYMBOLS = {
    understanding: {
        "COMBINED_SOURCE_MARKERS",
        "EXPLICIT_MANTIS_MARKERS",
        "EXPLICIT_NOTION_MARKERS",
        "ISSUE_ANALYSIS_INTENT_KEYWORDS",
        "ISSUE_LIST_INTENT_KEYWORDS",
        "NOTION_QUERY_STOPWORDS",
        "build_contextual_retrieval_text",
        "build_keyword_terms",
        "build_similar_issue_search_text",
        "chroma_text_matches_constraints",
        "extract_query_constraints",
        "extract_requested_limit",
        "field_matches_screen_number",
        "get_latest_user_text",
        "get_recent_user_texts",
        "has_constraint",
        "has_notion_retrieval_signal",
        "is_follow_up_question",
        "load_known_people",
        "merge_query_plan_constraints",
        "plan_search_intent_with_model",
        "resolve_reference_from_context",
        "resolve_result_limit",
        "rewrite_follow_up_query_with_model",
        "rule_based_search_intent",
        "should_run_notion_search",
        "should_run_retrieval",
        "text_matches_constraints",
        "text_matches_people",
        "text_matches_screen_numbers",
        "text_matches_status",
        "title_has_screen_number",
        "wants_all_results",
        "wants_similar_issue_search",
    },
    notion: {
        "build_notion_terms",
        "build_sources_from_notion_pages",
        "build_sources_from_single_notion_page",
        "collect_retrieved_notion_pages",
        "create_notion_page",
        "deduplicate_notion_pages_by_id",
        "ensure_configured_notion_database",
        "execute_notion_search",
        "extract_notion_constraints",
        "fetch_notion_database_schema",
        "fetch_notion_database_title",
        "fetch_notion_users",
        "format_deterministic_notion_answer",
        "format_grouped_notion_answer",
        "inherit_notion_context",
        "merge_common_notion_constraints",
        "normalize_notion_page",
        "notion_database_status_filter",
        "notion_page_matches_constraints",
        "notion_properties_to_text",
        "notion_search_max_pages",
        "resolve_notion_page_owner",
        "run_notion_write_command",
        "should_return_deterministic_notion_list",
        "update_notion_page",
        "validate_notion_page_id",
    },
    evaluation: {
        "EVALUATION_CASES",
        "build_accuracy_analytics",
        "build_evaluation_summary",
        "build_verification_report",
        "evaluate_ab_comparison",
        "finalize_chat_response",
        "generate_candidate_model_answer",
        "run_evaluation_case_safely",
        "save_evaluation_run",
    },
}


class PythonArchitectureContractTests(unittest.TestCase):
    def test_public_import_contract_is_available(self):
        for module, names in PUBLIC_SYMBOLS.items():
            with self.subTest(module=module.__name__):
                missing = sorted(name for name in names if not hasattr(module, name))
                self.assertEqual(missing, [])

    def test_evaluation_facade_uses_functional_modules(self):
        expected_modules = {
            "build_accuracy_analytics": "backend.evaluation.analytics",
            "build_evaluation_summary": "backend.evaluation.cases",
            "build_verification_report": "backend.evaluation.verification",
            "evaluate_ab_comparison": "backend.evaluation.comparison",
            "finalize_chat_response": "backend.evaluation.finalization",
        }
        for name, expected_module in expected_modules.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(evaluation, name).__module__, expected_module)

    def test_intent_facade_uses_functional_modules(self):
        expected_modules = {
            "extract_status_constraints": "backend.intent.entities",
            "extract_date_field_constraints": "backend.intent.dates",
            "remove_date_field_phrases": "backend.intent.dates",
            "extract_query_constraints": "backend.intent.constraints",
            "rule_based_search_intent": "backend.intent.source_router",
            "has_notion_retrieval_signal": "backend.intent.source_router",
            "should_run_retrieval": "backend.intent.source_router",
            "build_keyword_terms": "backend.intent.search_terms",
            "resolve_result_limit": "backend.intent.search_terms",
            "build_contextual_retrieval_text": "backend.intent.context",
            "chroma_text_matches_constraints": "backend.intent.matching",
            "text_matches_constraints": "backend.intent.matching",
        }
        for name, expected_module in expected_modules.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(understanding, name).__module__, expected_module)
        self.assertEqual(
            scope_extraction.extract_scope_constraints.__module__,
            "backend.intent.scope_extraction",
        )

    def test_notion_facade_uses_functional_modules(self):
        expected_modules = {
            "fetch_notion_database_schema": "backend.notion.client",
            "validate_notion_page_id": "backend.notion.schema",
            "normalize_notion_page": "backend.notion.mapping",
            "execute_notion_search": "backend.notion.search",
            "run_notion_write_command": "backend.notion.write",
            "format_deterministic_notion_answer": "backend.notion.formatting",
        }
        for name, expected_module in expected_modules.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(notion, name).__module__, expected_module)

    def test_aggregate_report_builders_use_dedicated_module(self):
        for name in (
            "build_mantis_count_report",
            "build_mantis_month_count_report",
            "build_mantis_pivot_report",
            "build_mantis_spike_report",
            "build_top_customers_report",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(answers, name).__module__, "backend.reports.aggregate")

    def test_chat_orchestrator_stays_phase_oriented(self):
        path = APP_DIR / "backend" / "services" / "chat_service.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        chat_service = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChatService")
        build_response = next(
            node for node in chat_service.body if isinstance(node, ast.FunctionDef) and node.name == "_build_response"
        )
        self.assertLessEqual(build_response.end_lineno - build_response.lineno + 1, 80)
        function_names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        for phase_function in (
            "build_deterministic_report",
            "prepare_chat_pipeline",
            "plan_chat_query",
            "retrieve_chat_evidence",
            "generate_model_response",
        ):
            with self.subTest(phase_function=phase_function):
                self.assertIn(phase_function, function_names)

    def test_query_plan_golden_cases(self):
        for case in FIXTURE["queryPlans"]:
            question = case["question"]
            constraints = understanding.extract_query_constraints(question)
            source_plan = understanding.rule_based_search_intent(question, constraints)
            plan = build_query_plan(question, source_plan, constraints)
            with self.subTest(question=question):
                self.assertEqual(plan["source"], case["source"])
                self.assertEqual(plan["operation"], case["operation"])
                for key, expected in case["filters"].items():
                    self.assertEqual(plan["filters"][key], expected)

    def test_notion_source_and_final_answer_golden_case(self):
        pages = FIXTURE["notionPages"]
        sources = notion.build_sources_from_notion_pages(pages)
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
        answer = notion.format_deterministic_notion_answer("노션에서 배포 문서 보여줘", retrieval_result, 5)
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
        self.assertIn('from "./charts.js"', chat)
        self.assertIn('from "./chat_controller.js"', chat)
        self.assertIn('from "./chat_rendering.js"', chat)
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
            "evaluationModal",
            "personalPanel",
            "dashboardPanel",
            "inviteModal",
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
