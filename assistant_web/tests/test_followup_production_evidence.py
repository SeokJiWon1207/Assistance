"""운영 웹에서 재현한 관련 고객사 조회와 오래된 노트 근거 누락 회귀."""
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from backend.answers import should_return_deterministic_issue_list
from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.retrieval import build_model_retrieval_context
from backend.sources import mantis
from backend.sources.mantis_collection import IssueBatch


class FollowupProductionEvidenceTests(unittest.TestCase):
    def test_related_customer_questions_use_source_rows(self):
        constraints = extract_query_constraints('HTS')
        for text in ('그럼 VBScript 관련해서 들어온 곳은?', '그럼 차트 관련해서 접수된 고객사는?',
                     '그럼 VBScript 관련해서 들어온 고객사는 어디야?'):
            with self.subTest(text=text):
                self.assertTrue(should_return_deterministic_issue_list(text, constraints))
        self.assertFalse(should_return_deterministic_issue_list('VBScript 관련해서 들어온 곳의 원인을 분석해줘', constraints))

    def test_spaced_korean_technical_terms_preserve_both_topics(self):
        constraints = extract_query_constraints('HTS')
        spaced = build_keyword_terms('HTS에서 VB 스크립트를 자바 스크립트로 바꿔달라는 이슈 있었어?', constraints)
        compact = build_keyword_terms('HTS에서 VB스크립트를 자바스크립트로 바꿔달라는 이슈 있었어?', constraints)
        self.assertEqual(spaced, compact)
        self.assertEqual(spaced, ['VB스크립트', '자바스크립트'])

    def test_korean_keyword_alias_keeps_matching_english_evidence(self):
        from backend.sources.mantis_evidence import matching_issue_evidence
        evidence = matching_issue_evidence({"notes": [{"id": 7, "text": "VB Script 호출 확인"}]}, ["VB스크립트"])
        self.assertTrue(evidence)
        self.assertIn("VB Script", str(evidence))

    def test_old_long_note_match_reaches_model_context(self):
        row = {'id': 901, 'summary': '화면 확인 요청', 'description': '일반 설명',
               'project': {'name': '108SK(SK증권)'}, 'handler': {'real_name': '우동헌'},
               'custom_fields': [{'field': {'name': '매체구분'}, 'value': 'HTS'}],
               'notes': [{'id': 1, 'text': '앞선 설명 ' * 600 + 'VB Scripts 이벤트 호출 확인'}]
                        + [{'id': i, 'text': '후속 진행 상황'} for i in range(2, 18)]}
        batch = IssueBatch([row], {'search_complete': True, 'snapshot_complete': True, 'status': 'snapshot'})
        with patch.object(mantis, 'read_complete_store', return_value=batch), \
                patch.object(mantis, 'stream_complete_store', return_value=nullcontext(batch), create=True):
            stage = mantis.execute_mantis_page_scan('VBScript', 10, extract_query_constraints('HTS'))
        self.assertEqual([r['id'] for r in stage['issues_list']], [901])
        context = build_model_retrieval_context({'stages': [stage]}, 10)
        evidence = context['issues'][0].get('matching_evidence')
        self.assertTrue(evidence)
        self.assertIn('VB Scripts', str(evidence))
        self.assertIn("'note_id': 1", str(evidence))


if __name__ == '__main__':
    unittest.main()
