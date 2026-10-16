"""선택된 설명 근거의 누락 노트 보강과 원문 세대/범위 보호."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import requests
from fastapi import HTTPException

from backend.execution import RequestStopped
from backend.evaluation.explanation_quotes import validate_quote_answer
from backend.sources.mantis_note_supplement import supplement_selected_notes


def note(note_id, text):
    return {'id': note_id, 'text': text, 'created_at': '2026-01-01',
            'updated_at': '2026-01-01', 'reporter': {'real_name': '담당자'}}


def fixture(issue_id=71, notes=None):
    raw = {'id': issue_id, 'summary': '화면 오류', 'project': {'name': '가상고객'},
           'notes': notes or [note(1, '원인 확인 중'), note(2, 'QC NG 재검증 요청')]}
    first = raw['notes'][0]
    evidence = {'complete': False, 'excerpts': [
        {'field': 'summary', 'start': 0, 'end': 5, 'text': '화면 오류'},
        {'field': f"note:{first['id']}", 'start': 0, 'end': len(first['text']),
         'text': first['text'], 'note_id': first['id'], 'created_at': first['created_at'],
         'updated_at': first['updated_at'], 'reporter': '담당자', 'passage_id': 'original'}]}
    return raw, {'issues': [{'id': issue_id, 'answer_evidence': evidence}]}


class SelectedNoteSupplementTests(unittest.TestCase):
    def run_supplement(self, raw, grounding, ids=None, constraints=None):
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=raw):
            return supplement_selected_notes(grounding, ids or [71], '원인 조치 QC 전달', constraints or {})

    def test_missing_middle_qc_failure_is_prioritized_and_source_is_immutable(self):
        notes = [note(1, '원인 확인 중')]
        notes += [note(i, '무관한 참고 자료\n' * 160) for i in range(2, 30)]
        notes += [note(30, 'QC NG: 수정한 화면에서 재현되어 전달을 보류합니다.')]
        notes += [note(i, '무관한 참고 자료\n' * 160) for i in range(31, 60)]
        raw, grounding = fixture(notes=notes)
        original = deepcopy(grounding)
        result, meta = self.run_supplement(raw, grounding)
        evidence = result['issues'][0]['answer_evidence']
        self.assertEqual(grounding, original)
        self.assertEqual(evidence['excerpts'][:2], original['issues'][0]['answer_evidence']['excerpts'])
        self.assertIn(30, {e.get('note_id') for e in evidence['excerpts']})
        self.assertFalse(evidence['complete'])
        self.assertLessEqual(meta['added_chars'], 12000)
        self.assertEqual(meta['supplemented_ids'], [71])
        for excerpt in evidence['excerpts'][2:]:
            source = next(n for n in notes if n['id'] == excerpt['note_id'])
            self.assertEqual(excerpt['text'], source['text'][excerpt['start']:excerpt['end']])
            self.assertEqual(excerpt['reporter'], '담당자')
            self.assertEqual(excerpt['created_at'], '2026-01-01')

    def test_complete_coverage_counts_characters_and_notes_exactly(self):
        raw, grounding = fixture()
        result, meta = self.run_supplement(raw, grounding)
        evidence = result['issues'][0]['answer_evidence']
        self.assertTrue(evidence['complete'])
        self.assertEqual(evidence['total_chars'], 24)
        self.assertEqual(evidence['provided_chars'], 24)
        self.assertEqual(evidence['total_notes'], 2)
        self.assertEqual(evidence['provided_notes'], 2)
        self.assertEqual(evidence['total_note_chars'], 19)
        self.assertEqual(evidence['provided_note_chars'], 19)
        self.assertEqual(meta['added_chars'], 12)

    def test_long_qc_note_does_not_displace_other_missing_stages(self):
        raw, grounding = fixture(notes=[note(1, '최초 접수 기록'),
                                      note(2, 'QC NG 재검증 필요\n' * 2000),
                                      note(3, '원인: 입력값 누락으로 발생했습니다.\n' * 40),
                                      note(4, '조치: 누락값 검사를 추가했습니다.\n' * 40),
                                      note(5, '고객 전달 후 운영 반영을 보류했습니다.\n' * 40)])
        result, _ = self.run_supplement(raw, grounding)
        ids = {e.get('note_id') for e in result['issues'][0]['answer_evidence']['excerpts']}
        self.assertTrue({2, 3, 4, 5}.issubset(ids))

    def test_scope_mismatch_cannot_add_notes(self):
        raw, grounding = fixture()
        result, meta = self.run_supplement(raw, grounding, constraints={'customer_exact': '다른고객'})
        self.assertEqual(result['issues'][0]['answer_evidence']['excerpts'],
                         grounding['issues'][0]['answer_evidence']['excerpts'])
        self.assertEqual(meta['failures'], [{'issue_id': 71, 'reason': 'scope_mismatch'}])

    def test_stale_excerpt_or_note_metadata_cannot_mix_generations(self):
        for mutation in ('text', 'created_at', 'reporter', 'missing'):
            with self.subTest(mutation=mutation):
                raw, grounding = fixture()
                if mutation == 'missing':
                    raw['notes'].pop(0)
                elif mutation == 'reporter':
                    raw['notes'][0]['reporter'] = {'real_name': '다른담당자'}
                else:
                    raw['notes'][0][mutation] = '변경됨'
                result, meta = self.run_supplement(raw, grounding)
                self.assertEqual(result['issues'][0]['answer_evidence']['excerpts'],
                                 grounding['issues'][0]['answer_evidence']['excerpts'])
                self.assertEqual(meta['failures'], [{'issue_id': 71, 'reason': 'stale_evidence'}])

    def test_entire_credential_note_is_excluded_even_when_secret_is_in_distant_chunk(self):
        raw, grounding = fixture(notes=[note(1, '원인 확인 중'),
                                      note(2, 'QC NG 수정 실패\n' + '참고 자료\n' * 400 + '\npassword: sample'),
                                      note(3, '조치 완료 전달 예정')])
        result, _ = self.run_supplement(raw, grounding)
        evidence = result['issues'][0]['answer_evidence']
        self.assertNotIn(2, {e.get('note_id') for e in evidence['excerpts']})
        self.assertIn(3, {e.get('note_id') for e in evidence['excerpts']})
        self.assertFalse(evidence['complete'])
        self.assertEqual(evidence['total_notes'], 3)

    def test_other_issue_section_is_excluded_with_exact_source_positions(self):
        text = '00071: 화면 수정\n\nQC NG 재검증 요청\n\n00072: 타 이슈\n\n고객 전달 완료'
        raw, grounding = fixture(notes=[note(1, '원인 확인 중'), note(2, text)])
        result, _ = self.run_supplement(raw, grounding)
        excerpts = result['issues'][0]['answer_evidence']['excerpts'][2:]
        self.assertTrue(any('QC NG' in e['text'] for e in excerpts))
        self.assertFalse(any('고객 전달 완료' in e['text'] for e in excerpts))
        for excerpt in excerpts:
            self.assertEqual(excerpt['text'], text[excerpt['start']:excerpt['end']])

    def test_partial_overlap_preserves_whole_chunk_context_and_unique_coverage(self):
        text = 'x' * 1100 + '아직 전달하지 않았습니다. QC NG 재검증이 필요합니다.' + 'y' * 1000
        raw, grounding = fixture(notes=[note(1, '원인 확인 중'), note(2, text)])
        evidence = grounding['issues'][0]['answer_evidence']
        evidence['excerpts'].append({'field': 'note:2', 'start': 0, 'end': 1120,
                                     'text': text[:1120], 'note_id': 2})
        result, meta = self.run_supplement(raw, grounding)
        output = result['issues'][0]['answer_evidence']
        passages = [e for e in output['excerpts'] if e['field'] == 'note:2']
        self.assertEqual(len(passages), 1)
        self.assertEqual(passages[0]['text'], text)
        self.assertEqual((passages[0]['start'], passages[0]['end']), (0, len(text)))
        self.assertEqual(output['provided_chars'], 12 + len(text))
        self.assertEqual(meta['added_chars'], len(text) - 1120)

    def test_overlapping_chunks_restore_whole_paragraph_for_quote_validation(self):
        warning = '아래는 실제 완료 기록이 아니라 아직 발송하지 않은 회신 초안입니다.'
        text = warning + '\n' + '참고 사항을 확인해야 합니다.\n' * 100 + '운영 반영 완료했습니다.'
        raw, grounding = fixture(notes=[note(1, text)])
        grounding['issues'][0]['answer_evidence']['excerpts'] = grounding['issues'][0]['answer_evidence']['excerpts'][:1]
        result, _ = self.run_supplement(raw, grounding)
        checked = validate_quote_answer({'selected_issue_ids': [71], 'sections': [{
            'topic': 'action', 'unanswered': False, 'evidence': [
                {'issue_id': 71, 'field': 'note:1', 'quote': '운영 반영 완료했습니다.'}]}]}, result)
        self.assertTrue(checked['valid'])
        self.assertEqual(len(checked['sections'][0]['evidence']), 1)
        self.assertEqual(checked['sections'][0]['evidence'][0]['text'], text)
        self.assertTrue(result['issues'][0]['answer_evidence']['complete'])

    def test_source_constraints_apply_only_to_selected_reference_issue(self):
        raw, grounding = fixture()
        grounding['issues'][0]['project'] = '가상고객'
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=raw):
            result, meta = supplement_selected_notes(grounding, [71], 'QC',
                                                     {'issue_ids': [72]}, source_constraints={71: {}})
            self.assertEqual(meta['supplemented_ids'], [71])
            self.assertTrue(result['issues'][0]['answer_evidence']['complete'])
            _, meta = supplement_selected_notes(grounding, [71], 'QC',
                                                {'issue_ids': [72]}, source_constraints={72: {}})
            self.assertEqual(meta['failures'], [{'issue_id': 71, 'reason': 'scope_mismatch'}])

    def test_reference_customer_or_unknown_media_change_is_not_allowed(self):
        raw, grounding = fixture()
        grounding['issues'][0]['project'] = '가상고객'
        cases = [({**raw, 'project': {'name': '다른고객'}}, {}),
                 ({**raw, 'custom_fields': [{'field': {'name': '매체구분'}, 'value': 'HTS'}]},
                  {'_reference_unknown_media': True})]
        for changed, override in cases:
            with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=changed):
                _, meta = supplement_selected_notes(grounding, [71], 'QC', {}, source_constraints={71: override})
                self.assertEqual(meta['failures'], [{'issue_id': 71, 'reason': 'scope_mismatch'}])
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=raw):
            _, meta = supplement_selected_notes(grounding, [71], 'QC', {},
                                                source_constraints={71: {'_reference_unknown_media': True}})
            self.assertEqual(meta['supplemented_ids'], [71])

    def test_unsafe_full_note_invalidates_previously_safe_partial_excerpt(self):
        raw, grounding = fixture()
        raw['notes'][0]['text'] += '\n' + '참고 자료\n' * 300 + 'password: sample'
        result, meta = self.run_supplement(raw, grounding)
        self.assertEqual(meta['failures'], [{'issue_id': 71, 'reason': 'stale_evidence'}])
        self.assertEqual(meta['added_chars'], 0)
        self.assertEqual(result['issues'][0]['answer_evidence']['excerpts'],
                         grounding['issues'][0]['answer_evidence']['excerpts'])

    def test_complete_disabled_and_missing_selection_do_not_fetch(self):
        raw, grounding = fixture()
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id',
                   side_effect=AssertionError('unneeded raw fetch')):
            grounding['issues'][0]['answer_evidence']['complete'] = True
            result, meta = supplement_selected_notes(grounding, [71], '', {})
            self.assertEqual(result, grounding)
            self.assertEqual(meta['attempted_ids'], [])
            grounding['issues'][0]['answer_evidence']['complete'] = False
            result, meta = supplement_selected_notes(grounding, [999], '', {})
            self.assertEqual(result, grounding)
            with patch.dict('os.environ', {'MANTIS_NOTE_SUPPLEMENT_ENABLED': 'false'}):
                result, meta = supplement_selected_notes(grounding, [71], '', {})
                self.assertEqual(result, grounding)
                self.assertFalse(meta['enabled'])

    def test_fetch_failure_is_sanitized_and_cancellation_propagates(self):
        _, grounding = fixture()
        for error, reason in ((requests.Timeout('private raw content'), 'fetch_failed'),
                              (PermissionError('private raw content'), 'access_denied'),
                              (HTTPException(403, 'private raw content'), 'access_denied'),
                              (HTTPException(404, 'private raw content'), 'missing_issue'),
                              (HTTPException(410, 'private raw content'), 'missing_issue')):
            with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', side_effect=error):
                result, meta = supplement_selected_notes(grounding, [71], '', {})
                self.assertEqual(meta['failures'], [{'issue_id': 71, 'reason': reason}])
                self.assertNotIn('private raw content', str(result) + str(meta))
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', side_effect=RequestStopped()):
            with self.assertRaises(RequestStopped):
                supplement_selected_notes(grounding, [71], '', {})

    def test_missing_or_wrong_raw_issue_does_not_add_notes(self):
        raw, grounding = fixture()
        for source in (None, {**raw, 'id': 999}):
            result, meta = self.run_supplement(source, grounding)
            self.assertEqual(result['issues'][0]['answer_evidence']['excerpts'],
                             grounding['issues'][0]['answer_evidence']['excerpts'])
            self.assertEqual(meta['supplemented_ids'], [])
            self.assertTrue(meta['failures'])

    def test_budget_is_shared_and_repeated_ids_are_fetched_once_with_three_issue_limit(self):
        raws = {}
        grounding = {'issues': []}
        for issue_id in (71, 72, 73, 74):
            raw, one = fixture(issue_id, [note(1, '원인 확인 중'), note(2, 'QC NG 수정 요청\n' * 2000)])
            raws[issue_id] = raw
            grounding['issues'].extend(one['issues'])
        fetched = []

        def fetch(issue_id):
            fetched.append(issue_id)
            return raws[issue_id]

        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', side_effect=fetch):
            result, meta = supplement_selected_notes(grounding, [71, 71, 72, 73, 74], 'QC', {})
        self.assertEqual(len(fetched), len(set(fetched)))
        self.assertLessEqual(len(fetched), 3)
        self.assertNotIn(74, fetched)
        self.assertLessEqual(meta['added_chars'], 12000)
        self.assertEqual(meta['added_chars'], sum(len(e['text']) for i in result['issues']
                                                for e in i['answer_evidence']['excerpts'][2:]))
        for issue in result['issues']:
            self.assertFalse(issue['answer_evidence']['complete'])


if __name__ == '__main__':
    unittest.main()
