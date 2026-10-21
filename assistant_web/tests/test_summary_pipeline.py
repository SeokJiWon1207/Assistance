"""검증·독립 검토 뒤에만 요약을 표시하고 충분한 근거의 추가 조회는 생략한다."""
import json
import unittest
from unittest.mock import patch

from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.services.grounded_explanation import generate_grounded_explanation
from backend.sources.mantis import normalize_issue


class SummaryPipelineTests(unittest.TestCase):
    def run_answer(self, *, partial=False, unsafe=False, missing=False, fetch_failed=False):
        q='조회 함수와 기본값을 알려줘'
        raw={'id':91,'summary':'조회 기능','description':'ReadValue 함수의 기본값은 7입니다.',
             'notes':[{'id':11,'text':'QC NG로 재수정 요청했습니다.','created_at':'2026-01-02'}]}
        issue=normalize_issue(raw,evidence_query=q)
        issue['answer_evidence']['complete']=not partial
        if partial:issue['answer_evidence']['excerpts']=[e for e in issue['answer_evidence']['excerpts'] if e['field']!='note:11']
        req=ChatRequest(modelId='test',messages=[{'role':'user','content':q}])
        ctx=ChatPipelineContext(q,{},'',[],q,{'mantis':True},[],{'issue_ids':[91]},5,
                                intent_plan={'operation':'explain'},retrieval_result={'stages':[
                                    {'source':'mantis_id_lookup','status':'ok','issues_list':[issue]}]})
        calls=[]
        def generate(_req,_model,messages,*_args):
            calls.append(messages)
            shown=json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n',1)[1])
            refs=[p['passage_id'] for p in shown['issues'][0]['answer_evidence']['excerpts'] if p['field']=='description']
            payload={'selected_issue_ids':[91],'sections':[{'topic':'technical','evidence_ids':refs,'unanswered':missing}],
                     'answer_items':[{'question_part':'조회 함수와 기본값','evidence_ids':refs,'unanswered':missing,
                                      'answer':'ReadValue의 기본값은 99입니다.' if unsafe else '제공된 근거로는 확인하지 못했습니다.' if missing else 'ReadValue 함수의 기본값은 7입니다.'}]}
            return {'content':json.dumps(payload if len(calls)==1 else {'unchanged':True}),'sources':[{'issue_id':91}],'raw':{}}
        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id',return_value=raw,
                   side_effect=TimeoutError if fetch_failed else None) as fetch:
            answer=generate_grounded_explanation(req,ctx,generate)
        return answer,calls,fetch.call_count

    def test_verified_draft_approved_by_independent_review_is_rendered_with_two_calls(self):
        r,calls,_=self.run_answer()
        self.assertTrue(r['groundingReview']['summaryAccepted'])
        self.assertEqual(len(calls),2)
        self.assertTrue(r['content'].startswith('### 핵심 답변'))
        self.assertIn('기본값은 7',r['content'].split('### 원문 근거와 전체 이력')[0])
        self.assertIn('QC NG',r['content'])

    def test_sufficient_partial_source_does_not_fetch_but_missing_answer_fetches_once(self):
        r,calls,count=self.run_answer(partial=True)
        self.assertTrue(r['groundingReview']['summaryAccepted'])
        self.assertEqual(count,0)
        self.assertEqual(len(calls),2)
        r,calls,count=self.run_answer(partial=True,missing=True)
        self.assertEqual(count,1)
        self.assertEqual(len(calls),2)
        self.assertIn('QC NG',calls[1][0]['content'])
        self.assertEqual(r['groundingReview']['noteSupplement']['plan']['max_rounds'],1)

    def test_unsupported_draft_does_not_replace_safe_quote_answer_even_when_model_approves(self):
        r,_,_=self.run_answer(unsafe=True)
        self.assertFalse(r['groundingReview']['summaryAccepted'])
        self.assertNotIn('99',r['content'])
        self.assertIn('7',r['content'])

    def test_failed_optional_investigation_notice_is_visible_before_folded_evidence(self):
        r,_,count=self.run_answer(partial=True,missing=True,fetch_failed=True)
        self.assertEqual(count,1)
        self.assertIn('추가 노트를 조회하지 못해',r['content'].split('### 원문 근거와 전체 이력')[0])
