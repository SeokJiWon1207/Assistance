"""Mantis 저장소·조건 규칙과 검색 실행의 의존 방향을 고정한다."""
import unittest
import json
import subprocess
import sys
from pathlib import Path

from architecture_graph import collect_python_edges, strongly_connected_components


class MantisArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edges = collect_python_edges(Path(__file__).resolve().parents[1] / 'backend')

    def test_mantis_store_and_projection_do_not_import_search_or_tools(self):
        self.assertEqual([
            (edge.importer, edge.imported) for edge in self.edges
            if edge.importer.startswith('backend.sources.mantis')
            and (edge.imported.startswith(('backend.tools.', 'backend.services.'))
                 or (edge.importer != 'backend.sources.mantis' and edge.imported == 'backend.sources.mantis'))
        ], [])

    def test_mantis_modules_are_not_in_a_runtime_cycle(self):
        self.assertEqual([sorted(component) for component in strongly_connected_components(self.edges)
                          if any(name.startswith(('backend.sources.mantis', 'backend.tools.mantis'))
                                 for name in component)], [])

    def test_search_execution_has_a_service_owner(self):
        from backend import retrieval
        self.assertEqual(retrieval.run_retrieval_pipeline.__module__, 'backend.services.retrieval_pipeline')

    def test_fields_and_matching_do_not_load_http_storage_or_orchestration(self):
        lower = {'backend.sources.mantis_fields', 'backend.sources.mantis_matching'}
        self.assertTrue(lower <= {edge.importer for edge in self.edges})
        self.assertEqual([(edge.importer, edge.imported) for edge in self.edges
                          if edge.importer in lower and
                          (edge.imported.startswith(('backend.tools.', 'backend.services.')) or
                           edge.imported in {'backend.sources.mantis', 'backend.sources.mantis_client',
                                             'backend.sources.mantis_store', 'backend.sources.mantis_snapshot_query'})], [])

    def test_old_paths_share_functions_locks_and_caches_with_their_owners(self):
        from backend import retrieval
        from backend.services import retrieval_pipeline
        from backend.sources import mantis, mantis_fields, mantis_matching, mantis_client, mantis_snapshot_query
        from backend.tools import mantis_snapshot, mantis_index
        from backend.sources import mantis_index as index_owner

        pairs = [
            (mantis.normalize_issue, mantis_fields.normalize_issue),
            (mantis.get_issue_custom_field_value, mantis_fields.get_issue_custom_field_value),
            (mantis.issue_matches_constraints, mantis_matching.issue_matches_constraints),
            (mantis.select_mantis_rows, mantis_matching.select_mantis_rows),
            (mantis.fetch_mantis_issue_by_id, mantis_client.fetch_mantis_issue_by_id),
            (mantis.fetch_mantis_issues, mantis_client.fetch_mantis_issues),
            (mantis._mantis_request_slots, mantis_client._mantis_request_slots),
            (mantis_snapshot.scan_mantis_documents, mantis_snapshot_query.scan_mantis_documents),
            (mantis_snapshot._validate_index_signature, mantis_snapshot_query._validate_index_signature),
            (mantis_snapshot.LOGGER, mantis_snapshot_query.LOGGER),
            (mantis_index.MantisIndexError, index_owner.MantisIndexError),
            (mantis_index.build_candidate_query, index_owner.build_candidate_query),
            (retrieval.run_retrieval_pipeline, retrieval_pipeline.run_retrieval_pipeline),
        ]
        for old, owner in pairs:
            self.assertIs(old, owner)

    def test_field_and_matching_imports_are_independent_of_io(self):
        code = '''
import sys
from unittest.mock import patch
from backend import environment
with patch.object(environment, 'load_dotenv'), \\
     patch('sqlite3.connect', side_effect=AssertionError('unexpected database access')), \\
     patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected HTTP')):
    from backend.sources import mantis_fields, mantis_matching
    assert mantis_fields.get_issue_custom_field_value({}, '화면번호') == ''
    assert mantis_matching.issue_matches_platform({}, [])
for name in ('backend.sources.mantis', 'backend.sources.mantis_store', 'backend.sources.mantis_client',
             'backend.sources.mantis_snapshot_query', 'backend.tools.mantis_snapshot',
             'backend.services.retrieval_pipeline'):
    assert name not in sys.modules, name
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_search_transport_and_revalidation_preserve_complete_results_and_calls(self):
        from mantis_contract_cases import run_cases
        expected = json.loads((Path(__file__).parent / 'fixtures/mantis_execution_golden.json').read_text())
        actual = json.loads(json.dumps(run_cases(), ensure_ascii=False, default=str))
        self.assertEqual(len(actual), len(expected))
        for before, after in zip(expected, actual, strict=True):
            with self.subTest(case=before['case']):
                self.assertEqual(after, before)
