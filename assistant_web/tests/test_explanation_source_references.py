"""설명 검색에서 원문이 명시한 선행 이슈만 좁게 복원한다."""
import unittest
from unittest.mock import patch

from backend.retrieval import build_model_retrieval_context, run_retrieval_pipeline
from backend.sources.mantis import issue_matches_constraints, normalize_issue
from backend.evaluation.explanation_relations import validate_related_selection
from backend.services.grounded_explanation import related_review_context


QUERY = ('신한 4700의 가격·이동평균간 지지 조건에서 구 HTS와 결과가 달랐던 이유와 '
         '수정 범위를 알려줘. 3월 17일 운영 적용 예정 안내 이후 실제 반영 여부는 어떻게 기록돼 있어?')
PROJECT = '132SHGM(신한굿모닝증권)'
TITLE = "[4700] 조건검색 화면 - '가격/이동평균간 지지' 조건식의 결과값이 신한i HTS (3000)조건검색 화면과 다른 현상"


def issue(issue_id, title, media, handler, note=''):
    return {'id': issue_id, 'summary': title, 'description': title,
            'project': {'name': PROJECT}, 'handler': {'real_name': handler},
            'status': {'name': 'closed'}, 'custom_fields': [
                {'field': {'name': '매체구분'}, 'value': media},
                {'field': {'name': '화면번호'}, 'value': '4700'}],
            'notes': [{'id': 85862, 'text': note}] if note else []}


def constraints():
    return {'customer_aliases': ['신한', '132SHGM'], 'platform_aliases': ['HTS'],
            'platform_key': 'hts', 'mantis_team_scope': 'default',
            'mantis_team_members': ['권한별'], 'screen_numbers': ['4700'],
            'filter_clauses': [{'field': 'customer', 'operator': 'equals', 'value': '신한'},
                               {'field': 'platform', 'operator': 'equals', 'value': 'hts'}]}


def search_result(source, extras=()):
    documents = [{'issue_id': raw['id'], 'latest_issue': normalize_issue(raw, evidence_query=QUERY),
                  'similarity_rank': rank}
                 for rank, raw in enumerate((source, *extras), 1)]
    return {'source': 'chroma_db', 'status': 'ok', 'documents': documents,
            'meta': {'search_complete': False}}


class ExplanationSourceReferenceTests(unittest.TestCase):
    def retrieve(self, source, target, *, query=QUERY, scope=None, extras=()):
        resolved = scope or constraints()
        plan = {'operation': 'explain', 'source': 'mantis', 'resolved_constraints': resolved}
        with patch('backend.retrieval.execute_chroma_search', return_value=search_result(source, extras)), patch(
            'backend.retrieval.fetch_mantis_issue_by_id', return_value=target, create=True
        ) as lookup:
            result = run_retrieval_pipeline(query, 5, {'mantis': True, 'vectorDb': True}, intent_plan=plan)
        return result, lookup

    def test_quoted_id_and_exact_title_restore_original_explanation_source(self):
        source = issue(24914, '[4700] 후속 문의', 'HTS', '권한별',
                       "'0023567: " + TITLE + "' 참고하시면 됩니다.\n기간 제한을 제거했습니다.")
        original = issue(23567, TITLE, '', '김호길', '3월 17일 08시 적용 예정. 이후 운영 미반영 확인.')
        self.assertFalse(issue_matches_constraints(original, constraints()))
        result, lookup = self.retrieve(source, original)
        grounding = build_model_retrieval_context(result, 5)
        self.assertEqual({row['id'] for row in grounding['issues']}, {24914, 23567})
        self.assertEqual(grounding['issues'][0]['id'], 23567)
        self.assertEqual(next(row for row in grounding['issues'] if row['id'] == 24914)['source_reference_ids'],
                         [23567])
        self.assertTrue(validate_related_selection(grounding, [23567, 24914]))
        self.assertEqual({row['id'] for row in related_review_context(grounding, [24914])['issues']},
                         {23567, 24914})
        self.assertTrue(any('운영 미반영' in excerpt['text'] for row in grounding['issues']
                            if row['id'] == 23567 for excerpt in row['answer_evidence']['excerpts']))
        lookup.assert_called_once_with(23567)
        self.assertEqual(result['constraints'], constraints())
        self.assertEqual(result['stages'][-1]['meta']['source_reference_edges'], [
            {'source_issue_id': 24914, 'target_issue_id': 23567, 'source_field': 'note:85862',
             'scope_exception': ['default_team', 'unknown_media_context']}])

    def test_quoted_original_keeps_a_slot_when_five_search_results_are_full(self):
        source = issue(24914, '[4700] 후속 문의', 'HTS', '권한별', "'0023567: " + TITLE + "' 참고하세요.")
        target = issue(23567, TITLE, '', '김호길')
        extras = [issue(25000 + i, f'[4700] 다른 문의 {i}', 'HTS', '권한별') for i in range(4)]
        result, _ = self.retrieve(source, target, extras=extras)
        ids = [row['id'] for row in build_model_retrieval_context(result, 5)['issues']]
        self.assertEqual(ids, [23567, 24914, 25000, 25001, 25002])

    def test_bare_id_or_wrong_title_cannot_inject_another_issue(self):
        target = issue(23567, TITLE, '', '김호길')
        for note in ['#23567도 있었습니다.', "'0023567: [4700] 전혀 다른 제목' 참고하세요."]:
            with self.subTest(note=note):
                source = issue(24914, '[4700] 후속 문의', 'HTS', '권한별', note)
                result, _ = self.retrieve(source, target)
                grounding = build_model_retrieval_context(result, 5)
                self.assertEqual([row['id'] for row in grounding['issues']], [24914])

    def test_unrelated_exactly_cited_issue_is_not_promoted(self):
        for unrelated_title in ['[4700] HTS 조건검색 화면 - 거래량 차트 색상 오류',
                                "[4800] HTS 가격/이동평균간 지지 조건식 결과 차이"]:
            with self.subTest(title=unrelated_title):
                source = issue(24914, '[4700] 후속 문의', 'HTS', '권한별',
                               "'0023567: " + unrelated_title + "' 참고하세요.")
                target = issue(23567, unrelated_title, '', '김호길')
                result, _ = self.retrieve(source, target)
                self.assertEqual([row['id'] for row in build_model_retrieval_context(result, 5)['issues']],
                                 [24914])

    def test_explicit_filters_and_project_boundary_still_exclude_reference(self):
        source = issue(24914, '[4700] 후속 문의', 'HTS', '권한별', "'0023567: " + TITLE + "' 참고하세요.")
        target = issue(23567, TITLE, '', '김호길')
        cases = [
            (QUERY, {**constraints(), 'mantis_team_scope': 'team'}, target),
            (QUERY, {**constraints(), 'statuses': ['assigned']}, target),
            (QUERY, {**constraints(), 'exclude_issue_ids': [23567]}, target),
            ('신한 4700 매체구분=HTS 이슈만 설명해줘', constraints(), target),
            (QUERY, constraints(), {**target, 'project': {'name': '다른 고객'}}),
        ]
        for query, scope, raw in cases:
            with self.subTest(query=query, scope=scope):
                result, _ = self.retrieve(source, raw, query=query, scope=scope)
                self.assertEqual([row['id'] for row in build_model_retrieval_context(result, 5)['issues']],
                                 [24914])

    def test_note_supplement_reuses_only_the_verified_reference_scope(self):
        from backend.services.grounded_explanation import supplement_explanation_notes
        source = issue(24914, '[4700] 후속 문의', 'HTS', '권한별', "'0023567: " + TITLE + "' 참고하세요.")
        target = issue(23567, TITLE, '', '김호길', '운영 반영은 아직 보류입니다.')
        result, _ = self.retrieve(source, target)
        grounding = build_model_retrieval_context(result, 5)
        selected = next(row for row in grounding['issues'] if row['id'] == 23567)
        selected['answer_evidence']['excerpts'] = [e for e in selected['answer_evidence']['excerpts']
                                                  if not e['field'].startswith('note:')]
        selected['answer_evidence']['complete'] = False
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=target):
            expanded, meta = supplement_explanation_notes(grounding, [24914, 23567], QUERY, result, constraints())
            _single, single_meta = supplement_explanation_notes(grounding, [23567], QUERY, result, constraints())
        self.assertEqual(meta['failures'], [])
        self.assertEqual(single_meta['failures'], [])
        self.assertIn(23567, meta['supplemented_ids'])
        self.assertEqual(result['constraints'], constraints())
        original = next(row for row in expanded['issues'] if row['id'] == 23567)
        self.assertIn('운영 반영은 아직 보류입니다.', [e['text'] for e in original['answer_evidence']['excerpts']])

    def test_similar_issue_supplement_keeps_the_historical_candidate_scope(self):
        from backend.services.grounded_explanation import supplement_explanation_notes
        query = 'Mantis #24914와 비슷한 과거 이슈의 원인과 조치를 비교해줘'
        base = issue(24914, '차트 화면 오류', 'HTS', '권한별')
        past = issue(23567, '차트 화면 오류 과거 사례', 'HTS', '권한별', 'QC NG로 전달 보류했습니다.')
        scope = {'issue_ids': [24914], 'filter_clauses': [{'field': 'issue_id', 'operator': 'in', 'value': [24914]}]}
        with patch('backend.retrieval.execute_mantis_id_lookup', return_value={
            'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': [normalize_issue(base, evidence_query=query)]
        }), patch('backend.retrieval.execute_chroma_search', return_value=search_result(past)):
            result = run_retrieval_pipeline(query, 5, {'mantis': True, 'vectorDb': True},
                                            {'operation': 'explain', 'resolved_constraints': scope})
        grounding = build_model_retrieval_context(result, 5)
        candidate = next(i for i in grounding['issues'] if i['id'] == 23567)
        candidate['answer_evidence']['complete'] = False
        candidate['answer_evidence']['excerpts'] = [e for e in candidate['answer_evidence']['excerpts']
                                                  if not e['field'].startswith('note:')]
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=past):
            expanded, meta = supplement_explanation_notes(grounding, [24914, 23567], query, result, scope)
        self.assertEqual(meta['failures'], [])
        self.assertEqual(meta['supplemented_ids'], [23567])
        self.assertEqual(result['constraints'], scope)
        restored = next(i for i in expanded['issues'] if i['id'] == 23567)
        self.assertIn('QC NG로 전달 보류했습니다.', [e['text'] for e in restored['answer_evidence']['excerpts']])


if __name__ == '__main__':
    unittest.main()
