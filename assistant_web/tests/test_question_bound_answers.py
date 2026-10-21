"""질문 항목과 근거 ID를 연결하되 새 문장이나 다른 이슈의 내용을 신뢰하지 않는다."""
import unittest
from backend.services.grounded_explanation import evidence_catalog,resolve_selections,merge_reviewed_selection
from backend.evaluation.explanation_quotes import validate_quote_answer,render_quote_answer


class QuestionBoundAnswerTests(unittest.TestCase):
    def setUp(self):
        self.g={'issues':[{'id':101,'answer_evidence':{'excerpts':[
            {'field':'note:1','note_id':1,'text':'잔고 조회는 ReadBalance 함수를 사용합니다.'},
            {'field':'note:2','note_id':2,'text':'체결 조회는 DEAL001 TR이며 일·분 주기를 지원합니다.'},
            {'field':'note:3','note_id':3,'text':'실시간 처리는 하지 않습니다.'}]}},
            {'id':102,'answer_evidence':{'excerpts':[{'field':'note:99','note_id':99,'text':'실시간 처리가 완료됐습니다.'}]}}]}
        self.shown,self.catalog=evidence_catalog(self.g)
        self.refs={entry['quote']:key for key,entry in self.catalog.items()}

    def payload(self):
        ids=[self.refs[text] for text in ['잔고 조회는 ReadBalance 함수를 사용합니다.','체결 조회는 DEAL001 TR이며 일·분 주기를 지원합니다.','실시간 처리는 하지 않습니다.']]
        return {'selected_issue_ids':[101],'sections':[{'topic':'technical','unanswered':False,'evidence_ids':ids}],
                'answer_items':[{'question_part':part,'unanswered':False,'evidence_ids':[key]}
                                for part,key in zip(['잔고','체결','실시간 처리'],ids,strict=True)]}

    def test_question_parts_and_proofs_appear_in_conclusion(self):
        p=resolve_selections(self.payload(),self.catalog)
        v=validate_quote_answer(p,self.g)
        core=render_quote_answer(v,self.g,question='잔고, 체결, 실시간 처리 방식을 알려줘').split('### 핵심 근거')[0]
        self.assertIn('**잔고**',core)
        self.assertIn('**체결**',core)
        self.assertIn('**실시간 처리**',core)
        self.assertIn('ReadBalance',core)
        self.assertIn('DEAL001',core)
        self.assertIn('하지 않습니다',core)

    def test_other_issue_cannot_be_used_as_a_question_part_answer(self):
        p=self.payload();p['answer_items'][0]['evidence_ids']=[self.refs['실시간 처리가 완료됐습니다.']]
        v=validate_quote_answer(resolve_selections(p,self.catalog),self.g)
        self.assertFalse(v['valid'])

    def test_invented_question_label_is_not_rendered(self):
        p=self.payload();p['answer_items'][0]['question_part']='운영 서버 배포 완료'
        v=validate_quote_answer(resolve_selections(p,self.catalog),self.g)
        core=render_quote_answer(v,self.g,question='잔고, 체결, 실시간 처리 방식을 알려줘').split('### 핵심 근거')[0]
        self.assertNotIn('운영 서버 배포 완료',core)

    def test_arbitrary_summary_is_never_rendered(self):
        p=self.payload();p['answer_items'][0]['summary']='고객 검증과 운영 배포 모두 완료'
        v=validate_quote_answer(resolve_selections(p,self.catalog),self.g)
        text=render_quote_answer(v,self.g,question='잔고, 체결, 실시간 처리 방식을 알려줘')
        self.assertNotIn('모두 완료',text)

    def test_review_preserves_question_bindings_and_old_payload_is_compatible(self):
        v=validate_quote_answer(resolve_selections(self.payload(),self.catalog),self.g)
        merged=merge_reviewed_selection(v,v,grounding=self.g)
        self.assertEqual(len(merged['answer_items']),3)
        old=self.payload();old.pop('answer_items')
        self.assertTrue(validate_quote_answer(resolve_selections(old,self.catalog),self.g)['valid'])

    def test_validated_answer_evidence_is_not_silently_truncated(self):
        rows=[{'field':f'note:{i}','note_id':i,'text':f'기술 항목 {i}의 필수 조건입니다.'} for i in range(1,7)]
        g={'issues':[{'id':101,'answer_evidence':{'excerpts':rows}}]}
        _,catalog=evidence_catalog(g)
        p={'selected_issue_ids':[101],'sections':[{'topic':'technical','unanswered':False,'evidence_ids':list(catalog)}],
           'answer_items':[{'question_part':'필수 조건','unanswered':False,'evidence_ids':list(catalog)}]}
        v=validate_quote_answer(resolve_selections(p,catalog),g)
        core=render_quote_answer(v,g,question='필수 조건을 알려줘').split('### 핵심 근거')[0]
        for i in range(1,7):self.assertIn(f'기술 항목 {i}',core)
