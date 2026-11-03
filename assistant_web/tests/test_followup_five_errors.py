"""회사 모델에서 관측한 다섯 후속 질의 오류와 조건 경계 회귀."""
from contextlib import nullcontext
from copy import deepcopy
import unittest
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend.intent.entities import extract_people_constraints
from backend.intent import unified
from backend.evidence.retrieval_rows import collect_retrieved_issues
from backend.intent.unified_validation import validate_plan, validate_execution_contract
from backend.intent.validation_rules import effective_statuses
from backend.sources.mantis import execute_mantis_snapshot_search, revalidate_mantis_snapshot_stage
from backend.sources.mantis_collection import IssueBatch
from backend.sources.mantis_snapshot_query import scan_mantis_documents
from backend.sources.mantis_index import build_mantis_index
from backend.sources.chroma_document import normalize_chroma_issue_document
from test_mantis_index import issue_document, create_chroma_database
from test_count_list_followup import USER, previous_plan
from test_unified_planner import proposal


def followup(**updates):
    return proposal(operation='search', scope='inherit', followup=True, limit=200,
                    status={'mode': 'inherit', 'values': [], 'evidence': ''}, **updates)


class FollowupFiveErrorsTests(unittest.TestCase):
    def test_chroma_shadow_and_structured_index_keep_global_sort(self):
        docs = [issue_document(i, created_at=d) for i, d in
                [(90, '2026-09-02'), (10, '2026-09-03'), (80, '2026-09-01')]]
        with tempfile.TemporaryDirectory() as folder:
            db, index = Path(folder) / 'chroma.sqlite3', Path(folder) / 'index.sqlite3'
            create_chroma_database(db, docs)
            build_mantis_index(index, docs)
            for mode in ['off', 'shadow', 'primary']:
                with self.subTest(mode=mode), patch.dict(os.environ, {'MANTIS_INDEX_MODE': mode}):
                    result = scan_mantis_documents(db, lambda _text: True, normalize_chroma_issue_document,
                        example_limit=2, index_database_path=index,
                        constraints={'sort': {'field': 'created_at', 'direction': 'asc'}})
                    self.assertEqual([r['id'] for r in result['issues']], [80, 90])
                    self.assertEqual(result['matched_count'], 3)

    def test_snapshot_sorts_all_matches_before_limit_and_preserves_refill_order(self):
        rows = [{'id': i, 'summary': '원문', 'created_at': created, 'updated_at': updated,
                 'status': {'name': 'assigned'}} for i, created, updated in [
                    (90, '2026-01-01T09:00:00+09:00', '2026-06-01'),
                    (10, '2026-01-01T11:00:00+09:00', '2026-08-01'),
                    (80, '2026-01-02', '2026-05-01'),
                    (20, '2026-01-03', '2026-07-01')]]
        batch = IssueBatch(rows, {'search_complete': True})
        for field, direction, expected in [('created_at', 'asc', [90, 10]),
                                            ('updated_at', 'asc', [80, 90]),
                                            ('updated_at', 'desc', [10, 20]),
                                            ('issue_id', 'asc', [10, 20])]:
            c = {'sort': {'field': field, 'direction': direction}, 'statuses': ['open']}
            with self.subTest(field=field, direction=direction), \
                    patch('backend.sources.mantis_store.stream_complete_store', return_value=nullcontext(batch)):
                stage = execute_mantis_snapshot_search(2, c)
                self.assertEqual([r['id'] for r in stage['issues_list']], expected)
                self.assertEqual(stage['meta']['found_count'], 4)
                displayed = collect_retrieved_issues({'constraints': c, 'stages': [stage]}, 2)
                self.assertEqual([r['id'] for r in displayed], expected)
                by_id = {r['id']: r for r in rows}
                with patch('backend.sources.mantis.fetch_mantis_issue_by_id',
                           side_effect=lambda i, removed=expected[0], lookup=by_id: None if i == removed else lookup[i]) as fetch:
                    refreshed = revalidate_mantis_snapshot_stage(stage, c)
                next_id = {'created_at': 80, 'updated_at': 20, 'issue_id': 80}[field] if direction == 'asc' else 90
                self.assertEqual(fetch.call_args_list[-1].args[0], next_id)
                self.assertEqual(len(refreshed['issues_list']), 2)

    def test_invalid_clear_repair_explicitly_removes_unsupported_clear(self):
        p = followup(clear_fields=['platform'])
        good = deepcopy(p); good['clear_fields'] = []
        with patch.object(unified, '_propose', side_effect=[p, good]) as model:
            result = unified.plan_question('목록 다 보여줘', {'id': 'fixture'}, 'key', USER, previous_plan())
        self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')
        self.assertIn('clear_fields에서 제거', model.call_args.args[2]['messages'][-1]['content'])
        self.assertIn('scope=inherit로', model.call_args.args[2]['messages'][-1]['content'])

    def test_followup_exclusion_keeps_open_range(self):
        p = followup(); p['status'] = {'mode': 'exclude', 'values': ['developed'], 'evidence': '개발완료는 빼고'}
        result = validate_plan('개발완료는 빼고 목록을 전부 보여줘', p, USER, previous_plan())
        c = result['resolved_constraints']
        self.assertEqual(effective_statuses(c['statuses'], c['excluded_statuses']),
                         {'new', 'feedback', 'acknowledged', 'confirmed', 'assigned'})
        self.assertEqual(c['platform_key'], 'mts')
        self.assertTrue(validate_execution_contract(result['grounding_text'], result)['valid'])

    def test_status_add_replace_remove_and_empty_set(self):
        previous = previous_plan()
        for q, mode, change, values, expected in [
            ('해결도 포함해서 보여줘', 'include', 'add', ['resolved'],
             {'new', 'feedback', 'acknowledged', 'confirmed', 'assigned', 'developed', 'resolved'}),
            ('종료만 보여줘', 'include', 'replace', ['closed'], {'closed'}),
            ('개발완료만 제외해줘', 'exclude', 'remove', ['developed'],
             {'new', 'feedback', 'acknowledged', 'confirmed', 'assigned'}),
        ]:
            p = followup(); p['status'] = {'mode': mode, 'change': change, 'values': values, 'evidence': q}
            with self.subTest(q=q):
                result = validate_plan(q, p, USER, previous)
                c = result['resolved_constraints']
                self.assertEqual(effective_statuses(c['statuses'], c['excluded_statuses']), expected)
        single = followup(); single['status'] = {'mode': 'include', 'values': ['assigned'], 'evidence': '할당만'}
        one = validate_plan('할당만 보여줘', single, USER, previous)
        single['status'] = {'mode': 'exclude', 'change': 'remove', 'values': ['assigned'], 'evidence': '할당도 빼고'}
        c = validate_plan('할당도 빼고 보여줘', single, USER, one)['resolved_constraints']
        self.assertEqual(effective_statuses(c['statuses'], c['excluded_statuses']), set())

    def test_followup_exclude_cannot_replace_and_broaden_the_previous_range(self):
        select = followup(); select['status'] = {'mode': 'include', 'values': ['assigned'], 'evidence': '할당만'}
        previous = validate_plan('할당만 목록 다 보여줘', select, USER, previous_plan())
        bad = followup(); bad['status'] = {'mode': 'exclude', 'change': 'replace', 'values': ['assigned'], 'evidence': '할당된 것도 빼고'}
        good = deepcopy(bad); good['status']['change'] = 'remove'
        q = '할당된 것도 빼고 나머지 목록 보여줘'
        with patch.object(unified, '_propose', side_effect=[bad, good]) as model:
            result = unified.plan_question(q, {'id': 'fixture'}, 'key', USER, previous)
        self.assertEqual(model.call_count, 2)
        c = result['resolved_constraints']
        self.assertEqual(effective_statuses(c['statuses'], c['excluded_statuses']), set())

    def test_grounded_status_clause_overrides_inherit_but_not_an_explicit_conflict(self):
        q = '그중 할당된 이슈만 전부 보여줘'
        p = followup(filters=[{'field': 'status', 'operator': 'equals', 'value': 'assigned', 'evidence': '할당된 이슈만'}])
        original = deepcopy(p)
        result = validate_plan(q, p, USER, previous_plan())
        self.assertEqual(result['resolved_constraints']['statuses'], ['assigned'])
        self.assertEqual(p, original)
        self.assertTrue(validate_execution_contract(q, result)['valid'])
        p['clear_fields'] = ['platform']
        retained = validate_plan(q, p, USER, previous_plan())
        self.assertEqual(retained['resolved_constraints']['platform_key'], 'mts')
        self.assertEqual(retained['resolved_constraints']['statuses'], ['assigned'])
        p['clear_fields'] = []
        for status, evidence in [({'mode': 'inherit', 'values': [], 'evidence': ''}, '보여줘'),
                                  ({'mode': 'include', 'values': ['developed'], 'evidence': '개발완료'}, '할당된')]:
            p['status'] = status; p['filters'][0]['evidence'] = evidence
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                validate_plan(q + ' 개발완료', p, USER, previous_plan())

    def test_customer_only_cannot_clear_platform_even_with_irrelevant_quote(self):
        q = '삼성증권만 몇 건이야?'
        for proof in [None, {'platform': '삼성증권만'}]:
            p = followup(filters=[{'field': 'customer', 'operator': 'equals', 'value': '삼성증권', 'evidence': '삼성증권'}])
            p.update(operation='count', limit=None, clear_fields=['platform'])
            if proof is not None:
                p['clear_evidence'] = proof
            with self.subTest(proof=proof):
                result = validate_plan(q, p, USER, previous_plan())
                self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')
                self.assertEqual(result['resolved_constraints']['customer_key'], '삼성증권')
                self.assertEqual(result['interpretation']['clear_fields'], [])
                self.assertTrue(result['interpretation']['ignored_changes'])
                self.assertTrue(validate_execution_contract(q, result)['valid'])

    def test_replacing_platform_does_not_require_a_separate_clear(self):
        q = 'MTS 대신 HTS로 바꿔서 전체 목록 보여줘'
        p = followup(filters=[{'field': 'platform', 'operator': 'equals', 'value': 'hts', 'evidence': 'HTS로 바꿔서'}],
                     clear_fields=['platform'], clear_evidence={'platform': 'MTS 대신 HTS로 바꿔서'})
        result = validate_plan(q, p, USER, previous_plan())
        self.assertEqual(result['resolved_constraints']['platform_key'], 'hts')
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(result['interpretation']['clear_fields'], [])

    def test_explicit_clear_is_bound_to_execution_contract(self):
        q = '매체 조건을 해제하고 목록 다 보여줘'
        p = followup(clear_fields=['platform'], clear_evidence={'platform': '매체 조건을 해제'})
        result = validate_plan(q, p, USER, previous_plan())
        self.assertIsNone(result['resolved_constraints']['platform_key'])
        self.assertTrue(validate_execution_contract(q, result)['valid'])
        result['interpretation']['clear_evidence']['platform'] = '삼성증권만'
        self.assertFalse(validate_execution_contract(q, result)['valid'])

    def test_execution_cannot_drop_an_inherited_filter_in_both_plan_and_constraints(self):
        q = '목록 다 보여줘'
        result = validate_plan(q, followup(), USER, previous_plan())
        clauses = [c for c in result['filter_clauses'] if c['field'] != 'platform']
        result['filter_clauses'] = clauses
        result['resolved_constraints']['filter_clauses'] = clauses
        self.assertFalse(validate_execution_contract(q, result)['valid'])

    def test_clearing_customer_cannot_expand_team_scope_without_evidence(self):
        previous = validate_plan('NH증권만 목록 다 보여줘', followup(filters=[
            {'field': 'customer', 'operator': 'equals', 'value': 'NH증권', 'evidence': 'NH증권'}]), USER, previous_plan())
        bad = followup(clear_fields=['customer']); bad['scope'] = 'all'
        retained = validate_plan('고객사 제한까지 풀어서 전체 목록 보여줘', bad, USER, previous)
        self.assertEqual(retained['resolved_constraints']['mantis_team_scope'], 'team')
        self.assertTrue(retained['interpretation']['ignored_changes'])
        good = deepcopy(bad); good['scope'] = 'inherit'
        result = validate_plan('고객사 제한까지 풀어서 전체 목록 보여줘', good, USER, previous)
        self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'team')
        result = validate_plan('팀 범위 풀고 전사 기준으로 목록 다 보여줘',
                              {**followup(), 'scope': 'all'}, USER, previous)
        self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'all')
        self.assertTrue(validate_execution_contract(result['grounding_text'], result)['valid'])

    def test_customer_compound_particles_preserve_exact_company_boundaries(self):
        from backend.customer_identity import customer_mentions
        for q in ['고객사는 삼성증권으로만 바꿔서 목록 전부 보여줘',
                  '삼성증권에서만 보여줘', '삼성증권으로도 조회해줘']:
            with self.subTest(q=q):
                self.assertTrue(any(key == '삼성증권' for _, _, key, _ in customer_mentions(q)))
                result = validate_plan(q, followup(filters=[{'field': 'customer', 'operator': 'equals',
                    'value': '삼성증권', 'evidence': q}]), USER, previous_plan())
                self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')
        self.assertFalse(any(key == '삼성증권' for _, _, key, _ in customer_mentions('삼성전자로만 바꿔줘')))

    def test_owner_only_particle_does_not_invent_a_second_person(self):
        for roster, phrase, expected in [
            (('이영희',), '이영희만', '이영희'),
            (('권한별',), '권한별만', '권한별'),
            ((), '홍길동만', '홍길동'),
            (('남궁서인',), '남궁서인만', '남궁서인'),
            (('김지만',), '김지만', '김지만'),
            ((), '김지만', '김지만'),
        ]:
            q = f'담당자는 {phrase} 전체 목록으로 보여줘'
            with self.subTest(phrase=phrase), patch('backend.intent.entities.load_known_people', return_value=(roster, ())):
                self.assertEqual(extract_people_constraints(q)['names'], [expected])
                p = followup(filters=[{'field': 'owner', 'operator': 'equals', 'value': expected, 'evidence': f'담당자는 {phrase}'}])
                self.assertEqual(validate_plan(q, p, USER, previous_plan())['resolved_constraints']['people']['names'], [expected])

    def test_owner_clear_does_not_treat_condition_as_a_person(self):
        q = '담당자 조건만 해제하고 목록 다 보여줘'
        self.assertEqual(extract_people_constraints(q)['names'], [])
        p = followup(filters=[{'field': 'owner', 'operator': 'equals', 'value': '이영희', 'evidence': '이영희'}])
        previous = validate_plan('담당자 이영희 목록 다 보여줘', p, USER, previous_plan())
        result = validate_plan(q, followup(clear_fields=['owner']), USER, previous)
        self.assertEqual(result['resolved_constraints']['people']['names'], [])
        self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')
