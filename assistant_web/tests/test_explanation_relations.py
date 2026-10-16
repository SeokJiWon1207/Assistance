"""실제 관계로 연결된 같은 프로젝트 이슈만 한 설명에 함께 선택한다."""
import importlib.util
import unittest

from backend.retrieval import build_model_retrieval_context
from backend.sources.mantis import normalize_issue


class ExplanationRelationsTests(unittest.TestCase):
    def validate(self, grounding, selected):
        self.assertIsNotNone(importlib.util.find_spec('backend.evaluation.explanation_relations'))
        from backend.evaluation.explanation_relations import validate_related_selection
        return validate_related_selection(grounding, selected)

    def grounding(self):
        return {'issues': [
            {'id': 23984, 'project': '146케이프증권(CAPE증권)', 'related_issue_ids': [24267]},
            {'id': 24267, 'project': '146케이프증권(CAPE증권)', 'related_issue_ids': [23984]},
            {'id': 30001, 'project': '146케이프증권(CAPE증권)', 'related_issue_ids': []},
        ]}

    def test_related_pair_is_allowed_but_similar_unrelated_candidate_is_rejected(self):
        grounding = self.grounding()
        self.assertTrue(self.validate(grounding, [23984, 24267]))
        self.assertFalse(self.validate(grounding, [23984, 30001]))
        self.assertFalse(self.validate(grounding, [23984, 24267, 30001]))

    def test_single_retrieved_issue_does_not_require_a_relationship(self):
        grounding = self.grounding()
        self.assertTrue(self.validate(grounding, [30001]))
        self.assertFalse(self.validate(grounding, [99999]))

    def test_one_sided_raw_relation_is_an_undirected_connection(self):
        grounding = self.grounding()
        grounding['issues'][1]['related_issue_ids'] = []
        self.assertTrue(self.validate(grounding, [24267, 23984]))

    def test_connected_chain_requires_the_intermediate_issue_to_be_selected(self):
        grounding = self.grounding()
        grounding['issues'][1]['related_issue_ids'].append(30001)
        self.assertTrue(self.validate(grounding, [23984, 24267, 30001]))
        self.assertFalse(self.validate(grounding, [23984, 30001]))

    def test_cross_project_and_unknown_project_do_not_prove_same_case(self):
        for project in ('다른 고객사', '', None, '미상'):
            grounding = self.grounding()
            grounding['issues'][1]['project'] = project
            self.assertFalse(self.validate(grounding, [23984, 24267]))
        grounding = self.grounding()
        for issue in grounding['issues']:
            issue['project'] = '미상'
        self.assertFalse(self.validate(grounding, [23984, 24267]))

    def test_selected_ids_must_be_unique_positive_integers_in_grounding(self):
        for selected in ([], [True], ['23984'], [23984.0], [0], [-1], [23984, 23984], [23984, 99999], None):
            with self.subTest(selected=selected):
                self.assertFalse(self.validate(self.grounding(), selected))

    def test_forged_or_malformed_relation_values_do_not_connect_issues(self):
        for relations in (['24267'], [24267.0], [True], '24267', None, {'24267': True}):
            grounding = self.grounding()
            grounding['issues'][0]['related_issue_ids'] = relations
            grounding['issues'][1]['related_issue_ids'] = []
            with self.subTest(relations=relations):
                self.assertFalse(self.validate(grounding, [23984, 24267]))

    def test_normalization_keeps_only_integer_related_issue_ids_from_raw_relationships(self):
        raw = {'id': 23984, 'summary': 'ADR 검토', 'project': {'name': '146케이프증권(CAPE증권)'},
               'relationships': [
                   {'id': 3203, 'issue': {'id': 24267, 'summary': 'ADR 구현'}, 'type': {'name': 'related-to'}},
                   {'issue': {'id': 24267}, 'type': {'name': 'related-to'}},
                   {'issue': {'id': 30001}, 'type': {'name': 'blocks'}},
                   {'issue': {'id': '30002'}, 'type': {'name': 'related-to'}},
                   {'issue': {'id': True}, 'type': {'name': 'related-to'}},
                   {'issue': {'id': 12.0}, 'type': {'name': 'related-to'}},
                   {'issue': {'id': -2}, 'type': {'name': 'related-to'}},
                   {'issue': {'id': 23984}, 'type': {'name': 'related-to'}},
                   {'issue': None, 'type': {'name': 'related-to'}}, None,
               ]}
        issue = normalize_issue(raw, evidence_query='ADR 기간')
        self.assertEqual(issue.get('related_issue_ids'), [24267])
        grounding = build_model_retrieval_context({'stages': [
            {'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': [issue]}]}, 5)
        self.assertEqual(grounding['issues'][0].get('related_issue_ids'), [24267])

    def test_missing_raw_relationships_stay_empty_without_inference_from_body(self):
        issue = normalize_issue({'id': 23984, 'summary': '#24267 관련 검토',
                                 'description': '#24267과 유사한 증상'})
        self.assertEqual(issue.get('related_issue_ids'), [])


if __name__ == '__main__':
    unittest.main()
