"""채팅 단계의 소유권과 요청 단위 실행 경계를 고정한다."""
import importlib
import json
from pathlib import Path
import unittest

from architecture_graph import collect_python_edges, strongly_connected_components


class ChatPipelineArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edges = collect_python_edges(Path(__file__).resolve().parents[1] / 'backend')

    def test_stages_and_unified_planning_do_not_import_the_orchestrator(self):
        self.assertEqual([(edge.importer, edge.imported) for edge in self.edges
                          if (edge.importer.startswith('backend.services.chat.') or
                              edge.importer == 'backend.services.unified_planning') and
                          edge.imported == 'backend.services.chat_service'], [])

    def test_chat_modules_are_not_in_a_runtime_cycle(self):
        self.assertEqual([sorted(component) for component in strongly_connected_components(self.edges)
                          if any(name.startswith('backend.services.chat') or
                                 name == 'backend.services.unified_planning' for name in component)], [])

    def test_legacy_paths_share_the_stage_functions_and_contract_types(self):
        from backend.services import chat_service, contracts
        for name, phase in {'prepare_chat_pipeline': 'prepare', 'plan_chat_query': 'planning',
                            'retrieve_chat_evidence': 'retrieve', 'generate_model_response': 'generate',
                            '_read_model_response': 'generate', 'build_deterministic_report': 'reports',
                            'apply_authenticated_mantis_scope': 'scope',
                            'apply_authenticated_notion_scope': 'scope'}.items():
            with self.subTest(name=name):
                owner = importlib.import_module('backend.services.chat.' + phase)
                self.assertIs(getattr(chat_service, name), getattr(owner, name))
                self.assertEqual(getattr(owner, name).__module__, owner.__name__)
        for name in ('ChatPipelineContext', 'StageResult', 'DeterministicReportResult'):
            self.assertIs(getattr(chat_service, name), getattr(contracts, name))

    def test_stages_do_not_own_admission_execution_or_final_storage(self):
        for edge in self.edges:
            if edge.importer.startswith('backend.services.chat.'):
                with self.subTest(module=edge.importer, target=edge.imported):
                    self.assertNotIn(edge.imported, {'backend.admission', 'backend.evaluation.finalization'})
        import ast
        root = Path(__file__).resolve().parents[1] / 'backend/services/chat'
        for path in root.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse({alias.name for alias in node.names} &
                                     {'request_execution', 'execution_scope', 'request_timing'}, path.name)
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, 'attr', '')
                    self.assertNotIn(name, {'request_execution', 'execution_scope', 'request_timing',
                                            'persist_chat_response', 'finalize_chat_response'})

    def test_lifecycle_matches_pre_extraction_golden(self):
        from chat_pipeline_contract_cases import capture_pipeline_cases
        expected = json.loads((Path(__file__).parent / 'fixtures/chat_pipeline_golden.json').read_text())
        actual = capture_pipeline_cases()
        for before, after in zip(expected, actual, strict=True):
            with self.subTest(case=before['scenario'], admitted=before['admitted']):
                self.assertEqual(after, before)
                for key in ('shared_budget', 'shared_timing', 'shared_context', 'budget_restored', 'timing_restored'):
                    self.assertTrue(after[key], key)
                expected_saves = 0 if after['error'] else 1
                self.assertEqual(len(after['stored']), expected_saves)
