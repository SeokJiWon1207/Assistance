"""짧은 답변의 문장마다 원문 근거를 유지하며 새 수치·완료 사실을 막는다."""
import unittest
from backend.evaluation.explanation_summary import summary_items, render_summary


class ExplanationSummaryTests(unittest.TestCase):
    def data(self, answer='캐시 덮어쓰기 로직을 수정했습니다.', **updates):
        row={'issue_id':91,'field':'note:11','note_id':11,'created_at':'2026-06-10','text':'캐시 덮어쓰기 로직을 수정했습니다.'}
        item={'question_part':'어떤 조치','unanswered':False,'evidence':[row],'draft_answer':answer}|updates
        v={'valid':True,'selected_issue_ids':[91],'answer_items':[item]}
        g={'issues':[{'id':91,'answer_evidence':{'excerpts':[row]}}]}
        return v,g

    def test_concise_answer_is_bound_to_verified_original_note(self):
        v,g=self.data('캐시를 덮어쓰던 로직을 수정했습니다.')
        items=summary_items(v,g,'어떤 조치를 했어?')
        self.assertEqual(len(items),1)
        text=render_summary(items,'### 핵심 결론\n옛 결론\n### 핵심 근거\n원문입니다.')
        self.assertIn('캐시를 덮어쓰던 로직을 수정했습니다.',text)
        self.assertIn('노트 11',text)
        self.assertIn('### 원문 근거와 전체 이력',text)
        self.assertNotIn('옛 결론',text)

    def test_unsupported_numbers_identifiers_links_and_madeup_labels_are_not_summaries(self):
        for answer,updates in [('100건을 수정했습니다.',{}),('UpdateCacheNew 함수를 고쳤습니다.',{}),
                               ('https://example.com으로 이동하세요.',{}),
                               ('조치를 했습니다.',{'evidence':[]}),('가'*500,{})]:
            with self.subTest(answer=answer):
                v,g=self.data(answer,**updates)
                self.assertEqual(summary_items(v,g,'어떤 조치를 했어?'),[])

    def test_paraphrased_labels_are_hidden_and_uncertainty_can_say_unconfirmed(self):
        v,g=self.data(question_part='운영 배포')
        items=summary_items(v,g,'어떤 조치를 했어?')
        self.assertEqual(len(items),1)
        self.assertNotIn('운영 배포',render_summary(items,'원문'))
        for text in ['고객 검증 완료 여부는 확인 불가함.', '고객 검증 완료를 결론내릴 근거가 부족함.', '고객 검증 통과 여부는 명시되지 않았습니다.', '서버 데이터 제공 여부는 확인 필요하며 확정된 원인은 아님.']:
            v,g=self.data(text,question_part='고객 검증',unanswered=True)
            self.assertEqual(len(summary_items(v,g,'고객 검증 결과는?')),1)

    def test_delivery_request_does_not_prove_customer_validation(self):
        v,g=self.data('고객 검증이 완료됐습니다.',question_part='고객 검증')
        v['answer_items'][0]['evidence'][0]['text']='수정본 전달 드립니다. 적용 후 테스트 부탁드립니다.'
        self.assertEqual(summary_items(v,g,'고객 검증 결과는?'),[])

    def test_uncertainty_is_explicit_and_later_failure_overrides_completion(self):
        v,g=self.data('고객 검증 완료를 확인할 근거가 없습니다.',question_part='고객 검증',unanswered=True)
        self.assertEqual(len(summary_items(v,g,'고객 검증 결과는?')),1)
        v,g=self.data('QC 완료가 확인됩니다.',question_part='QC')
        v['answer_items'][0]['evidence'][0]['text']='[QC 완료]'
        g['issues'][0]['answer_evidence']['excerpts'].append({'field':'note:12','note_id':12,'created_at':'2026-06-11','text':'QC NG로 재요청했습니다.'})
        self.assertEqual(summary_items(v,g,'QC 결과는?'),[])

    def test_mixed_items_fall_back_as_a_whole_instead_of_omitting_required_answer(self):
        v,g=self.data();v['answer_items'].append({'question_part':'검증','unanswered':False,'evidence':[],'draft_answer':'완료'})
        self.assertEqual(summary_items(v,g,'어떤 조치와 검증을 했어?'),[])

    def test_uncertainty_about_another_stage_does_not_hide_false_completion(self):
        v,g=self.data('고객 검증이 완료됐고 운영 반영 근거는 없습니다.',question_part='고객 검증')
        self.assertEqual(summary_items(v,g,'고객 검증과 운영 반영 결과는?'),[])

    def test_disclosure_marker_removes_generated_html_but_preserves_literal_source(self):
        v,g=self.data()
        text=render_summary(summary_items(v,g,'어떤 조치를 했어?'),
                            '### 핵심 근거\n<details>\n<summary>전체 이력과 원문 근거 보기</summary>\n> ```text\n> <details>\n> ```\n</details>')
        self.assertNotIn('\n<details>\n',text)
        self.assertNotIn('<summary>',text)
        self.assertIn('> <details>',text)

    def test_missing_completion_evidence_cannot_become_a_negative_fact(self):
        for text in ['전달되지 않았습니다.', '고객사 보류로 종료했으며 실제 고객 반영은 되지 않음.', '고객 테스트를 하지 않았습니다.']:
            with self.subTest(text=text):
                v,g=self.data(text,question_part='전달')
                self.assertEqual(summary_items(v,g,'전달과 고객 반영, 테스트 결과는?'),[])
        v,g=self.data('전달 완료 여부는 확인할 수 없습니다.',question_part='전달',unanswered=True)
        self.assertEqual(len(summary_items(v,g,'전달과 고객 반영 결과는?')),1)

    def test_unanswered_paraphrased_labels_keep_only_subjects_from_the_question(self):
        v,g=self.data('고객 검증은 확인할 수 없습니다.',question_part='고객 검증 완료 여부',unanswered=True,evidence=[])
        v['answer_items'].append({'question_part':'운영 반영 완료 여부','draft_answer':'운영 반영은 확인할 수 없습니다.','unanswered':True,'evidence':[]})
        items=summary_items(v,g,'고객 검증과 운영 반영까지 끝났다는 근거가 있어?')
        self.assertEqual([i['question_part'] for i in items],['고객 검증','운영 반영'])
        text=render_summary(items,'### 핵심 근거\n원문')
        self.assertIn('**고객 검증**',text)
        self.assertIn('**운영 반영**',text)
