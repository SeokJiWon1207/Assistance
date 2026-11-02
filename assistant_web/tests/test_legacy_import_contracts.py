"""이전 기간에만 유지하는 공개 import 계약. 제거 단계는 architecture_migration.json 참조."""

import importlib
import json
import unittest
from pathlib import Path

from backend import answers, common, config, understanding
from backend.intent import scope_extraction
from backend.reports import evaluation
from backend.sources import notion


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
        "generate_basic_grounded_baseline",
        "generate_candidate_model_answer",
        "run_evaluation_case_safely",
        "save_evaluation_run",
    },
}


class LegacyImportContractTests(unittest.TestCase):
    def test_configuration_legacy_paths_share_the_actual_owners(self):
        from backend.settings import catalogs, notion as settings
        from backend.notion import client, discovery, transport

        self.assertIs(config.get_notion_settings, discovery.resolve_notion_settings)
        self.assertIs(client.get_notion_settings, discovery.resolve_notion_settings)
        for owner, names in (
            (catalogs, ('CUSTOMER_ALIASES', 'CUSTOMER_DIRECTORY', 'CUSTOMER_PROJECT_DISPLAY_NAMES',
                        'load_customer_aliases', 'load_customer_directory', 'load_customer_project_display_names')),
            (settings, ('NOTION_API_BASE', 'NOTION_DEFAULT_VERSION', 'get_notion_headers',
                        'get_notion_token', 'parse_notion_database_ids', 'notion_unavailable_reason')),
            (transport, ('notion_post_json', 'notion_request_slot')),
            (discovery, ('discover_notion_database_ids', 'notion_is_configured')),
        ):
            for name in names:
                with self.subTest(symbol=name):
                    self.assertIs(getattr(config, name), getattr(owner, name))

    def test_legacy_imports_share_implementation_and_static_objects(self):
        ownership = json.loads((Path(__file__).parent / 'fixtures/common_ownership.json').read_text())
        for symbol, owner in ownership['common_symbols'].items():
            with self.subTest(symbol=symbol, owner=owner):
                self.assertIs(getattr(common, symbol), getattr(importlib.import_module(owner), symbol))
        values = importlib.import_module('backend.settings.values')
        for symbol in ownership['config_values']:
            with self.subTest(symbol=symbol):
                self.assertIs(getattr(config, symbol), getattr(values, symbol))

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
            "generate_basic_grounded_baseline": "backend.evaluation.comparison",
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
            "build_keyword_terms": "backend.intent.keyword_terms",
            "resolve_result_limit": "backend.intent.keyword_terms",
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
            "fetch_notion_database_schema": "backend.notion.read_api",
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


if __name__ == "__main__":
    unittest.main()
