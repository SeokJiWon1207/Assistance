"""근거 정리와 리포트가 상위 검증·검색 조정 계층을 호출하지 않는다."""
import unittest
import json
from pathlib import Path

from architecture_graph import collect_python_edges, strongly_connected_components


class EvidenceArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.edges = collect_python_edges(Path(__file__).resolve().parents[1] / 'backend')

    def test_reports_and_sources_do_not_depend_on_evaluation(self):
        self.assertEqual([
            (edge.importer, edge.imported) for edge in self.edges
            if edge.importer.startswith(('backend.reports.', 'backend.sources.'))
            and edge.importer != 'backend.reports.evaluation'
            and edge.imported.startswith('backend.evaluation.')
        ], [])

    def test_result_helpers_do_not_call_orchestration(self):
        self.assertEqual([
            (edge.importer, edge.imported) for edge in self.edges
            if (edge.importer == 'backend.reports.aggregate' and edge.imported == 'backend.retrieval')
            or (edge.importer == 'backend.reports.work_queries' and edge.imported == 'backend.answers')
            or (edge.importer.startswith('backend.evidence.') and
                edge.imported.startswith(('backend.evaluation.', 'backend.reports.', 'backend.services.')))
            or (edge.importer.startswith('backend.evidence.') and edge.imported in {'backend.retrieval', 'backend.answers'})
        ], [])

    def test_evidence_comparison_and_effort_are_acyclic(self):
        owners = {
            'backend.evaluation.explanation_direct', 'backend.evaluation.explanation_quotes',
            'backend.reports.similar', 'backend.reports.comparison_facts',
            'backend.reports.comparison_history', 'backend.reports.comparison_narrative',
            'backend.reports.effort', 'backend.reports.effort_trend',
        }
        self.assertEqual([sorted(component) for component in strongly_connected_components(self.edges)
                          if component & owners or any(name.startswith('backend.evidence.') for name in component)], [])

    def test_legacy_imports_share_the_same_implementations(self):
        from backend import answers, retrieval
        from backend.evaluation import explanation_direct, explanation_quotes
        from backend.evidence import excerpts, stages, retrieval_rows, model_context
        from backend.reports import constraint_display, direct_conclusion, quote_rendering
        from backend.reports import similar, comparison_common, comparison_facts, comparison_history

        pairs = [
            (explanation_quotes.safe_excerpts, excerpts.safe_excerpts),
            (explanation_quotes.render_quote_answer, quote_rendering.render_quote_answer),
            (explanation_direct.direct_conclusion, direct_conclusion.direct_conclusion),
            (explanation_direct._STAGES, stages.STAGES),
            (explanation_direct._stage_event, stages.stage_events),
            (explanation_direct._completed_stage, stages.completed_stage),
            (retrieval.collect_retrieved_issues, retrieval_rows.collect_retrieved_issues),
            (retrieval.build_model_retrieval_context, model_context.build_model_retrieval_context),
            (answers.display_constraints, constraint_display.display_constraints),
            (similar._recorded_cause, comparison_common._recorded_cause),
            (similar._recorded_fix, comparison_common._recorded_fix),
            (similar._history_lines, comparison_history._history_lines),
            (comparison_facts.render_facts, comparison_common.render_facts),
        ]
        for old, owner in pairs:
            self.assertIs(old, owner)

    def test_pre_migration_conclusions_and_quotes_are_unchanged(self):
        from backend.reports.quote_conclusions import _conclusion_lines
        from backend.reports.direct_conclusion import direct_conclusion
        from backend.reports.quote_rendering import render_quote_answer

        functions = {'_conclusion_lines': _conclusion_lines, 'direct_conclusion': direct_conclusion,
                     'render_quote_answer': render_quote_answer}
        path = Path(__file__).parent / 'fixtures/evidence_rendering_golden.json'
        for case in json.loads(path.read_text()):
            with self.subTest(case=case['id'], function=case['function']):
                arguments = case['arguments']
                if 'issues' in arguments:
                    arguments['issues'] = {int(key): value for key, value in arguments['issues'].items()}
                self.assertEqual(functions[case['function']](**arguments), case['expected'])
