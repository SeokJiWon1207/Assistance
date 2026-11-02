"""P3의 하위 규칙 경계와 기존 질문 해석 계약을 검증한다."""
import importlib
import importlib.util
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from architecture_graph import collect_python_edges, strongly_connected_components

APP = Path(__file__).resolve().parents[1]


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 30)


class QueryBoundaryTests(unittest.TestCase):
    def test_no_runtime_cycle_contains_intent_query_or_team_modules(self):
        cycles = strongly_connected_components(collect_python_edges(APP/'backend'))
        affected = [sorted(group) for group in cycles if any(
            name.startswith(('backend.intent.', 'backend.query.')) or name in {'backend.query_planning', 'backend.team_scope'}
            for name in group)]
        self.assertEqual(affected, [])

    def test_query_and_intent_do_not_import_notion_search(self):
        edges = collect_python_edges(APP/'backend')
        self.assertEqual([(e.importer, e.scope) for e in edges if e.imported == 'backend.notion.search'
                          and (e.importer.startswith(('backend.intent.', 'backend.query.'))
                               or e.importer == 'backend.query_planning')], [])

    def test_lower_rules_and_catalog_do_not_import_orchestration(self):
        boundaries = {
            'backend.query.': ('backend.intent.', 'backend.sources.', 'backend.services.', 'backend.llm', 'backend.notion.'),
            'backend.domain.team': ('backend.intent.', 'backend.query_planning', 'backend.services.'),
            'backend.sources.people_catalog': ('backend.intent.', 'backend.query_planning'),
            'backend.intent.validation_rules': ('backend.intent.unified_validation', 'backend.intent.aggregation_spec',
                                               'backend.intent.condition_grounding'),
        }
        violations = []
        for edge in collect_python_edges(APP / 'backend'):
            for owner, forbidden in boundaries.items():
                if (edge.importer.startswith(owner) and edge.imported.startswith(forbidden)
                        and edge.imported != 'backend.notion.constants'):
                    violations.append((edge.importer, edge.scope, edge.imported))
        self.assertEqual(violations, [])

    def test_frozen_legacy_helpers_keep_results(self):
        fixture = json.loads((APP/'tests/fixtures/query_boundary_cases.json').read_text())
        with ExitStack() as stack:
            # Before/after clock owners differ; keep the same frozen input for both.
            for name in ('backend.query_planning', 'backend.intent.unified_validation',
                         'backend.query.dates', 'backend.intent.validation_rules'):
                try:
                    module = importlib.import_module(name)
                except ModuleNotFoundError:
                    continue
                if hasattr(module, 'date'):
                    stack.enter_context(patch.object(module, 'date', FixedDate))
            for case in fixture['cases']:
                with self.subTest(function=case['name'], args=case['args']):
                    fn = getattr(importlib.import_module(case['module']), case['name'])
                    if 'error' in case:
                        with self.assertRaises((ValueError, TypeError)) as raised:
                            fn(*case['args'], **case['kwargs'])
                        self.assertEqual(type(raised.exception).__name__, case['error'])
                        self.assertEqual(str(raised.exception), case['message'])
                    else:
                        self.assertEqual(fn(*case['args'], **case['kwargs']), case['result'])

    def test_notion_parser_uses_supplied_roster_and_preserves_literal_scope(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.query'), '순수 query 계층이 필요합니다')
        from backend.query.notion_constraints import parse_notion_constraints
        first = parse_notion_constraints('노션 김지은이 맡은 문서', known_people=(('김지은',), ()))
        second = parse_notion_constraints('노션 김지은이 맡은 문서', known_people=(('다른사람',), ()))
        self.assertEqual(first['owner'], '김지은')
        self.assertEqual(second['owner'], '')
        literal = parse_notion_constraints("노션 '김지은 대기 2024년 2월 문서' 찾아줘", known_people=(('김지은',), ()))
        self.assertEqual(literal['owner'], '')
        self.assertEqual(literal['status'], '')
        self.assertNotIn('date_filter', literal)
        self.assertEqual(literal['exact_title'], '김지은 대기 2024년 2월 문서')

    def test_notion_entry_point_reads_one_consistent_roster_per_call(self):
        from backend.intent import notion_constraints
        from backend.notion.search import extract_notion_constraints
        with patch.object(notion_constraints, 'load_known_people', side_effect=[
            (('김지은',), ()), (('다른사람',), ()),
        ]) as load:
            self.assertEqual(extract_notion_constraints('노션 김지은이 맡은 문서')['owner'], '김지은')
            self.assertEqual(load.call_count, 1)
            self.assertEqual(extract_notion_constraints('노션 김지은이 맡은 문서')['owner'], '')
            self.assertEqual(load.call_count, 2)

    def test_body_grounding_uses_supplied_constraints_without_reinterpreting(self):
        from backend.intent import notion_grounding
        from backend.query.notion_text import grounded_notion_body_constraints
        question = '노션 담당자 김지은 또는 김보람 문서 중 본문에 "완료" 포함하고 코드에 "deprecated" 없는 문서'
        owner = {'field': 'owner', 'operator': 'in', 'value': ['김지은', '김보람']}
        proposed = {'filter_clauses': [owner, {'field': 'status', 'operator': 'in', 'value': ['완료']}]}
        grounded = {'filter_clauses': [{'field': 'owner', 'operator': 'equals', 'value': '김지은'}]}
        original = deepcopy((proposed, grounded))
        with patch.object(notion_grounding, 'extract_query_constraints', side_effect=AssertionError('재해석 금지')):
            actual = grounded_notion_body_constraints(question, proposed, grounded=grounded)
        self.assertEqual(actual['filter_clauses'], [
            owner,
            {'field': 'keyword', 'operator': 'contains', 'value': '완료'},
            {'field': 'keyword', 'operator': 'not_contains', 'value': 'deprecated'},
        ])
        self.assertEqual((proposed, grounded), original)
        with patch.object(notion_grounding, 'extract_query_constraints', return_value=grounded) as extract:
            self.assertEqual(notion_grounding.grounded_notion_body_constraints(question, proposed), actual)
        extract.assert_called_once()
        self.assertNotIn('deprecated', extract.call_args.args[0])

    def test_legacy_similarity_entry_point_only_extracts_missing_constraints(self):
        from backend.intent import constraints
        from backend.intent.keyword_terms import wants_similar_issue_search as predicate
        from backend.intent.search_terms import wants_similar_issue_search as legacy
        question = '#123456 유사 사례 찾아줘'
        with patch.object(constraints, 'extract_query_constraints', return_value={'issue_ids': [123456]}) as extract:
            self.assertTrue(legacy(question))
            extract.assert_called_once_with(question)
            extract.reset_mock()
            self.assertTrue(legacy(question, {'issue_ids': [123456]}))
            self.assertFalse(legacy(question, {}))
            self.assertTrue(predicate(question, {'issue_ids': [123456]}))
            extract.assert_not_called()

    def test_legacy_imports_reexport_the_same_rules_and_caches(self):
        for old, new, names in (
            ('backend.query_planning', 'backend.query.operations', ('infer_operation', 'extract_group_by')),
            ('backend.query_planning', 'backend.query.dates', ('extract_created_at_range', 'extract_relative_created_at_range')),
            ('backend.intent.constraints', 'backend.intent.json_payload', ('extract_json_object',)),
            ('backend.intent.search_terms', 'backend.intent.keyword_terms', ('__all__', 'build_keyword_terms')),
            ('backend.intent.unified_validation', 'backend.intent.validation_rules', ('require', 'check_clause', 'check_query_period')),
            ('backend.intent.unified_validation', 'backend.intent.execution_contract', ('validate_execution_contract',)),
            ('backend.intent.entities', 'backend.sources.people_catalog', ('load_known_people', '_load_known_people_cached')),
            ('backend.team_scope', 'backend.domain.team', ('TEAM_MEMBERS', 'has_mantis_team_scope')),
            ('backend.team_scope', 'backend.intent.team_scope', ('apply_default_team_scope',)),
            ('backend.notion.search', 'backend.query.notion_constraints', (
                '_known_notion_owner_matcher', '_IMPLICIT_OWNER_NAME', '_STATUS_BOUND_NOUN',
                '_STATUS_SPOKEN_SUFFIX', '_STATUS_WORD_END',
            )),
        ):
            for name in names:
                with self.subTest(module=old, symbol=name):
                    self.assertIs(getattr(importlib.import_module(old), name), getattr(importlib.import_module(new), name))

    def test_pure_query_imports_do_not_load_search_or_open_databases(self):
        code = '''
import importlib, socket, sqlite3, sys
from unittest.mock import patch
def forbidden(*args, **kwargs):
    raise AssertionError("순수 규칙에서 I/O를 호출했습니다")
with patch.object(sqlite3, "connect", forbidden), patch.object(socket.socket, "connect", forbidden):
    for name in ("operations", "dates", "notion_text", "notion_constraints"):
        importlib.import_module("backend.query." + name)
    assert not any(name.startswith(("backend.intent.", "backend.notion.search", "backend.notion.schema",
                                   "backend.notion.client", "backend.sources.people_catalog")) for name in sys.modules)
'''
        # -I excludes user site packages; the only added import path is this application.
        result = subprocess.run(
            [sys.executable, '-I', '-c', f'import sys; sys.path.insert(0, {str(APP)!r})\n' + code],
            cwd=APP, env={key: os.environ[key] for key in ('PATH', 'TMPDIR', 'LANG') if key in os.environ},
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
