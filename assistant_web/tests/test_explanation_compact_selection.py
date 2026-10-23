"""축약 출력도 동일한 원문·미확정·이슈 범위 검사를 통과해야 한다."""
import json
from unittest import TestCase
from unittest.mock import patch

from backend.services.grounded_explanation import generate_grounded_explanation
from backend.evaluation.explanation_selection import expand_selection
from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.sources.mantis import normalize_issue


class CompactSelectionTests(TestCase):
    def test_compact_preserves_unknown_and_exact_refs(self):
        raw = {'selected_issue_ids':[91], 'sections':[['cause',['p1'],False],['action',[],True]],
               'answer_items':[['원인',['p1'],False,'해제 누락입니다.'],['조치',[],True,'확인할 수 없습니다.']],
               'investigate':[91]}
        expanded=expand_selection(raw)
        self.assertEqual(expanded['sections'][1],{'topic':'action','evidence_ids':[],'unanswered':True})
        self.assertEqual(expanded['answer_items'][0]['answer'],'해제 누락입니다.')
        self.assertEqual(expanded['investigate'],[91])
        self.assertIsInstance(raw['sections'][0],list)

    def test_malformed_or_mixed_rows_fail_closed(self):
        for section in [[['cause',['p1']]], [['cause',['p1'],False,'extra']],
                        [['cause',['p1'],False],{'topic':'action','evidence_ids':['p1'],'unanswered':False}]]:
            self.assertEqual(expand_selection({'selected_issue_ids':[91],'sections':section}),{})
        for answer in [[['원인',['p1'],False]], [['원인',['p1'],False,'답','extra']]]:
            self.assertEqual(expand_selection({'sections':[['cause',['p1'],False]],'answer_items':answer}),{})

    def run_answer(self, compact, *, invented=False):
        query='누수 원인과 실제 조치를 알려줘'
        row=normalize_issue({'id':91,'summary':'누수','description':'원인: 타이머 해제 누락 때문에 누수 발생.',
                             'notes':[{'id':12,'text':'타이머 해제 추가 수정함.','created_at':'2026-05-02'}]}, evidence_query=query)
        req=ChatRequest(modelId='test',messages=[{'role':'user','content':query}])
        ctx=ChatPipelineContext(query,{},'',[],query,{'mantis':True},[],{'issue_ids':[91]},5,
                                intent_plan={'operation':'explain'},retrieval_result={
                                    'stages':[{'source':'mantis_id_lookup','status':'ok','issues_list':[row]}]})
        calls=[]
        def generate(_req,_model,messages,*_args):
            calls.append(messages)
            if len(calls)==2 and not invented:return {'content':'{"unchanged":true}'}
            shown=json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n',1)[1])
            refs=[p['passage_id'] for p in shown['issues'][0]['answer_evidence']['excerpts']]
            if invented:refs=['fabricated']
            sections=[['cause',refs,False],['action',refs,False]]
            answers=[['원인과 실제 조치',refs,False,'타이머 해제 누락으로 누수가 발생하여 해제 추가 수정 기록이 있습니다.']]
            payload={'selected_issue_ids':[91],'sections':sections,'answer_items':answers,'investigate':[]}
            if not compact:payload=expand_selection(payload)
            return {'content':json.dumps(payload),'sources':[{'issue_id':91}]}
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id',return_value=row):
            return generate_grounded_explanation(req,ctx,generate),calls

    def test_compact_uses_same_source_and_independent_review(self):
        compact,calls=self.run_answer(True)
        full,_=self.run_answer(False)
        self.assertTrue(compact['groundingReview']['completed'])
        self.assertEqual(len(calls),2)
        self.assertEqual(compact['content'],full['content'])
        self.assertIn('타이머 해제',compact['content'])
        self.assertIn('누수',compact['content'])
        self.assertIn('cause',calls[0][1]['content'])
        self.assertEqual(compact['groundingReview']['modelCalls']['selection'],1)
        self.assertEqual(compact['groundingReview']['modelCalls']['review'],1)

    def test_compact_unknown_id_does_not_skip_source_validation(self):
        result,calls=self.run_answer(True,invented=True)
        self.assertFalse(result['groundingReview']['completed'])
        self.assertEqual(len(calls),2)
        self.assertEqual(result['sources'],[])
