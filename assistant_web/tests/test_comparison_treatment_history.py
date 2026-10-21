"""본문 처리 이력을 잃거나 진단 실험을 확정 원인으로 승격하지 않는다."""
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.reports import comparison_facts, comparison_narrative, similar


def history_issue():
    return {'id': 76, 'summary': '메모리릭 제거', 'notes': [], 'description':
            '[0522-담당자] 방문해서 처리 시도했지만 실패했고, 2번이나 소스 보냈는데 처리 안될 것 같음. '
            '메인이 없다보니 상상코딩으로 처리해야해서 공수가 더 많이 들어감. 추가 방문 예정\n'
            '[0524-담당자] 의외로 메모리릭 다 처리된 듯 해서 완료처리'}


class ComparisonTreatmentHistoryTests(TestCase):
    def test_body_history_reaches_every_comparison_case_even_without_notes(self):
        lines = '\n'.join(similar._comparison_lines({'id': 90, 'summary': '메모리 누수'}, [history_issue()]))
        for phrase in ['#76', '실패', '2번', '소스', '보냈', '처리된 듯', '본문']:
            self.assertIn(phrase, lines)
        self.assertEqual(similar._recorded_fix(history_issue()), ('', ''))

    def test_history_is_writer_evidence_not_a_completed_technical_fix(self):
        facts = comparison_narrative.comparison_facts([history_issue()])
        history = [f for f in facts if f['kind'] == 'history']
        self.assertTrue(history)
        for phrase in ['실패', '2번', '소스', '처리된 듯', '본문']:
            self.assertIn(phrase, ' '.join(f['text'] for f in history))
        self.assertFalse(any(f['kind'] in {'cause', 'action'} for f in facts))
        catalog = {f['id']: f for f in facts}
        refs = list(catalog)
        self.assertFalse(comparison_narrative.validate_claim(
            {'text': '#76은 메모리 누수 수정이 완료됐습니다.', 'fact_ids': refs}, catalog))

    def test_source_delivery_request_is_not_a_record_of_delivery(self):
        for description in ['소스를 2번 보내주세요. 방문 처리 예정. 수정된 듯한지 확인 부탁드립니다.',
                            '소스 보냈나요? 전달했는지 확인해주세요.', '방문 처리에 실패하면 연락주세요.']:
            facts = comparison_narrative.comparison_facts([{'id': 76, 'description': description}])
            self.assertFalse(any(f['kind'] == 'history' for f in facts))

    def test_short_summary_can_keep_failure_delivery_count_and_tentative_closure(self):
        facts = comparison_narrative.comparison_facts([history_issue()])
        self.assertTrue(comparison_narrative.validate_claim({
            'text': '#76은 방문 처리에 실패하고 소스를 2번 보냈으며, 처리된 듯해 완료처리한 기록으로 해결 여부는 미확인입니다.',
            'fact_ids': [f['id'] for f in facts]}, {f['id']: f for f in facts}))

    def test_note_history_retains_its_note_reference_and_the_issue_boundary(self):
        issue = {'id': 76, 'notes': [{'id': 45, 'text': '현장 방문해서 처리 시도했지만 실패했습니다. 수정 소스를 두 차례 보냈습니다.'}]}
        text = '\n'.join(similar._comparison_lines({'id': 90}, [issue]))
        self.assertIn('#76', text)
        self.assertIn('노트 45', text)
        self.assertIn('두 차례', text)

    def test_positive_semantic_review_cannot_allow_omitting_selected_case_history(self):
        issues = [{'id': 90, 'summary': '메모리 누수'}, history_issue()]
        facts = comparison_narrative.comparison_facts(issues)
        unknown = {f['issue_id']: f['id'] for f in facts if f['kind'] == 'unknown'}
        draft = {'claims': [
            {'text': '#90은 확정 원인과 실제 조치를 확인하지 못했습니다.', 'fact_ids': [unknown[90]]},
            {'text': '#76은 확정 원인과 실제 조치를 확인하지 못했습니다.', 'fact_ids': [unknown[76]]}]}
        def response(value):
            return Mock(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': json.dumps(value)}}]})
        with patch.object(comparison_narrative, 'post_model_chat', side_effect=[response(draft), response({'supported': [True, True]})]):
            text, meta = comparison_narrative.summarize_comparison('이전 사례의 실패·전달 이력도 비교해줘', issues, {'id': 'company'}, 'key')
        self.assertEqual(text, '')
        self.assertEqual(meta['status'], 'rejected')

    def test_experiment_selected_by_model_is_not_a_cause(self):
        issue = {'id': 76, 'summary': '차트 메모리누수', 'notes': [{'id': 45, 'text':
                 '확인 중 문의드립니다.\nView::OnInitialUpdate()에서 바로 return 처리를 하게 되면 차트 DLL 로딩 없이 Map화면만 띄우게\n'
                 '되는데 이 때에도 메모리 릭이 발생하고 있습니다. 확인 부탁드립니다.'}]}
        comparison_facts._CACHE.clear()
        def response(model, key, payload, **kwargs):
            rows = json.loads(payload['messages'][1]['content'])['passages']
            selected = next(r['id'] for r in rows if 'return' in r['text'])
            proposal = {'issues': [{'issue_id': 76, 'cause': [selected], 'action': []}]}
            return Mock(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': json.dumps(proposal)}}]})
        with patch.object(comparison_facts, 'post_model_chat', side_effect=response):
            meta = comparison_facts.review_comparison_facts([issue], {'id': 'company'}, 'key')
        self.assertEqual(meta['status'], 'reviewed')
        self.assertEqual(similar._recorded_cause(issue), ('', ''))
        self.assertFalse(issue['comparison_facts']['uncertain_cause'])

    def test_real_diagnosis_after_an_experiment_remains_a_cause(self):
        issue = {'id': 76, 'summary': '메모리 누수', 'notes': [{'id': 45, 'text':
                 '함수를 바로 return 하면 DLL 없이 실행되는데 이 때에도 누수가 발생합니다.\n'
                 '원인: 객체 해제 누락 때문에 메모리 누수 발생으로 확인됨.'}]}
        self.assertIn('객체 해제 누락', similar._recorded_cause(issue)[0])

    def test_split_experiment_result_is_not_an_uncertain_cause_either(self):
        issue = {'id': 76, 'summary': '메모리 누수', 'notes': [{'id': 45, 'text':
                 '함수에서 return 하면 DLL 로딩 없이 맵만 표시하게\n되는데 이 때에도 메모리 누수가 발생합니다.\n'
                 '원인: 객체 해제 누락 때문에 메모리 누수 발생으로 확인됨.'}]}
        self.assertTrue(similar._is_consequence('되는데 이 때에도 메모리 누수가 발생합니다', issue))
        self.assertFalse(similar._is_consequence('객체 해제 누락 때문에 메모리 누수 발생으로 확인됨', issue))

    def test_comparison_case_duplicate_closure_keeps_its_own_issue_id(self):
        issue = {'id': 76, 'notes': [{'id': 45, 'text': '중복으로 이슈가 생성되어 종료처리'}]}
        text = '\n'.join(similar._comparison_lines({'id': 90}, [issue]))
        self.assertIn('#76 처리 이력 [노트 45]', text)
        self.assertNotIn('기준 이슈 처리 이력', text)
