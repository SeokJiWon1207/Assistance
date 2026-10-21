"""유사 비교의 검색 주제·원인 확정·기술 조치·필수 이력을 검증한다."""
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests
from backend.reports import similar
from backend.sources import chroma


class SimilarComparisonAccuracyTests(unittest.TestCase):
    def test_temporal_instruction_does_not_filter_out_matching_memory_cases(self):
        rows = {
            100: {'id': 100, 'summary': '전략차트 메모리 부족 케이스 정리'},
            90: {'id': 90, 'summary': '차트 메모리 부족 현상'},
            91: {'id': 91, 'summary': '메모리 부족으로 차트 종료'},
            80: {'id': 80, 'summary': '오래된 차트 메모리 표시 변경'},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chroma.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)')
                for identifier in (90, 91, 80):
                    db.execute('INSERT INTO embedding_metadata VALUES (?,?)', ('chroma:document',
                        f'이슈 ID: #{identifier}\n[제목]\n{rows[identifier]["summary"]}\n\n본문'))
            with patch.object(chroma, 'CHROMA_DB_PATH', path), \
                 patch.dict('os.environ', {'MANTIS_HYBRID_SEARCH_ENABLED': 'false'}), \
                 patch.dict(sys.modules, {'chromadb': types.SimpleNamespace()}), \
                 patch.object(chroma.requests, 'post', side_effect=requests.ConnectionError('offline')), \
                 patch.object(chroma, 'fetch_mantis_issue_by_id', side_effect=rows.get), \
                 patch.object(similar, 'fetch_mantis_issue_by_id', side_effect=rows.get):
                report = similar.build_similar_resolution_report(
                    '#100의 메모리 부족 사례와 더 오래된 비슷한 이슈의 원인·실제 조치를 비교해줘')
        self.assertEqual({source['issue_id'] for source in report['sources']}, {100, 90, 91})

    def test_resource_relevance_uses_description_and_rejects_unrelated_chart(self):
        base = {'id': 100, 'summary': '차트 리소스 문제 검토', 'description': '메모리 증가 및 누수 확인 요청'}
        unrelated = {'issue_id': 90, 'latest_issue': {'id': 90, 'summary': '차트 분배락 표기 개선'}}
        relevant = {'issue_id': 80, 'latest_issue': {'id': 80, 'summary': '차트 리소스 개선',
                                                  'description': '차트 리소스 누수 사항 수정'}}
        with patch.object(similar, 'fetch_mantis_issue_by_id', return_value=base), \
             patch.object(similar, 'execute_chroma_search', side_effect=[
                 {'status': 'ok', 'documents': [unrelated, relevant]}, {'status': 'ok', 'documents': [relevant]}]):
            report = similar.build_similar_resolution_report('#100과 비슷한 과거 사례의 원인과 조치를 비교해줘')
        self.assertEqual({source['issue_id'] for source in report['sources']}, {100, 80})

    def test_possible_and_unclear_causes_are_not_confirmed_causes(self):
        for text in ('순차검색이면서 시세가 많이 들어오면 느려지는 원인이 될 수 있다.',
                     '원인이 명확하지 않아 이번 반영에 즉시 반영은 어려울 것으로 보임.',
                     'CPU 테스트 결과 비교봉수 조건이 원인으로 보입니다.'):
            with self.subTest(text=text):
                self.assertEqual(similar._recorded_cause({'notes': [{'text': text}]}), ('', ''))

    def test_confirmed_description_cause_is_not_lost_when_notes_only_close_ticket(self):
        detail = {'description': '1차 확인결과\n하나의 스크립트 파싱을 위해 db Loading / parsing / compiling 이 매번 이루어짐.\n'
                                 '전체 컴파일이 요청되면 매번 db loading, 공통 함수 parsing 등이 반복되어 발생.',
                  'notes': [{'id': 9, 'text': '완료된 작업 종료처리 합니다.'}]}
        cause, origin = similar._recorded_cause(detail)
        self.assertIn('db Loading / parsing / compiling', cause)
        self.assertEqual(origin, 'description')
        self.assertEqual(similar._recorded_fix(detail), ('', ''))

    def test_technical_changes_survive_later_qc_delivery_and_admin_closure(self):
        detail = {'notes': [
            {'id': 1, 'text': 'index 찾는 방식을 qsort와 bsearch를 통해 하도록 수정.\nsort정보는 신규가 있을 때 갱신되도록 처리함.'},
            {'id': 2, 'text': '[QC요청]\n부분적인 drawing 처리가 되도록 개선함.'},
            {'id': 3, 'text': 'QC 결과 정상적으로 작동하였습니다(OK)'},
            {'id': 4, 'text': '[고객사전달]'},
            {'id': 5, 'text': '완료된 작업 종료처리 합니다.'},
        ]}
        action, _ = similar._recorded_fix(detail)
        for token in ('qsort', 'bsearch', 'drawing', 'QC', '전달'):
            self.assertIn(token, action)
        self.assertNotIn('종료처리', action)

    def test_latest_actual_release_and_ineffective_result_remain_together(self):
        detail = {'notes': [{'id': 1, 'text': 'QC 완료'}, {'id': 2, 'text': (
            'class TimeMeterPDLL 에서 Delete 되지 않는 부분 확인\n'
            '> 처음 로드하는 클래스의 소멸자에서 해제할 수 있도록 처리, 로드 시점과 해제 시점의 주소를 비교하여 정상 해제 확인\n\n'
            '처리 후 측정 시 체감되는 메모리 증가량 변화는 없음')}]}
        action, _ = similar._recorded_fix(detail)
        for token in ('TimeMeterPDLL', '소멸자', '정상 해제'):
            self.assertIn(token, action)
        self.assertIn('이전 QC', action, '수정 전 검수 기록으로 최신 수정을 검수 완료했다고 말하면 안 됩니다')
        self.assertIn('변화는 없음', similar._latest_resolution_caveat(detail))

    def test_implementation_request_does_not_become_actual_action(self):
        for text in ('qsort와 bsearch로 수정할 예정입니다.',
                     '[QC요청]\n부분적인 drawing 처리가 되도록 개선해 주세요.',
                     '소멸자 해제 처리가 되었는지 확인 요청드립니다.'):
            with self.subTest(text=text):
                self.assertEqual(similar._recorded_fix({'notes': [{'text': text}]}), ('', ''))

    def test_possible_cause_is_shown_as_unconfirmed_instead_of_disappearing(self):
        detail = {'id': 8, 'notes': [{'id': 16, 'text': '순차검색으로 인해 느려지는 원인이 될 수 있다.'}]}
        content = '\n'.join(similar._comparison_lines({'id': 9}, [detail]))
        for token in ('미확정', '순차검색', '16'):
            self.assertIn(token, content)

    def test_work_priority_is_not_a_technical_root_cause(self):
        text = '우선순위가 높은건이기 때문에 QC담당자와 개발자가 병행하여 함께 확인 진행하라는 말씀입니다.'
        self.assertEqual(similar._recorded_cause({'notes': [{'text': text}]}), ('', ''))

    def test_suspected_description_cause_and_latest_recheck_remain_uncertain(self):
        detail = {'id': 9, 'description': '24개 차트를 사용하는 것이 1차 원인으로 보이나 확인이 더 필요합니다.',
                  'notes': [{'id': 1, 'text': '[QC 완료]'}, {'id': 2, 'text': (
                      'GDI 개수가 상이한 현상 확인됨.\n지표 등 추가되는 항목들에 대한 확인 필요해 보임')}]}
        self.assertEqual(similar._recorded_cause(detail), ('', ''))
        self.assertIn('확인 필요', similar._latest_resolution_caveat(detail))

    def test_suspected_cause_without_an_explicit_request_is_still_not_confirmed(self):
        detail = {'description': '24개 차트를 사용하는 것이 1차 원인으로 보이나 고객은 이 차트들을 사용해야 한다고 합니다.'}
        self.assertEqual(similar._recorded_cause(detail), ('', ''))

    def test_same_technical_sentence_is_not_repeated_by_overlapping_evidence(self):
        action, _ = similar._recorded_fix({'notes': [{'id': 1, 'text': 'GDI 리소스가 새고 있는 부분 수정함.\nGDI 리소스가 새고 있는 부분 수정함.'}]})
        self.assertEqual(action.count('GDI 리소스가 새고 있는 부분 수정함'), 1)

    def test_memory_leak_aliases_keep_resource_relevance_filter(self):
        for title in ('차트 메모리릭 확인 요청', '메모리 리크 확인', '메모리 누실 원인 파악 요청'):
            with self.subTest(title=title):
                self.assertTrue(similar._resource_symptom(title))

    def test_cause_investigation_completion_is_not_the_identified_cause(self):
        detail = {'notes': [{'text': '해당고객 원격연결하여 원인파악 완료 하였으며, 차트가 열리게끔 조치해놓은상태 입니다.'}]}
        self.assertEqual(similar._recorded_cause(detail), ('', ''))

    def test_confirmed_error_mechanism_is_cause_without_a_cause_heading(self):
        detail = {'notes': [{'text': '신심리도 내부 스크립트에서 0 으로 나눗셈을 하는 오류 발생하여 메모리가 꼬여버리는 현상.'}]}
        self.assertIn('0 으로 나눗셈', similar._recorded_cause(detail)[0])

    def test_reported_leak_fix_is_preserved_with_its_reported_wording(self):
        text = '차트 종목탭 메모리누수 잡았다고함'
        action, _ = similar._recorded_fix({'notes': [{'id': 1, 'text': text}]})
        self.assertIn(text, action)
        self.assertNotIn('운영', action)

    def test_description_request_is_not_a_confirmed_cause(self):
        detail = {'summary': '[0544] 조건검색 CPU 자원 100% 사용',
                  'description': '[0544] 조건검색 CPU 자원 100% 사용에 따른 원인 파악 및 조치'}
        self.assertEqual(similar._recorded_cause(detail), ('', ''))

    def test_same_screen_does_not_replace_cpu_cases(self):
        base = {'id': 100, 'summary': '조건검색 CPU사용율 100% 사용'}
        unrelated = {'issue_id': 90, 'content': '조건검색 조건 문의',
                     'latest_issue': {'id': 90, 'summary': '조건검색 조건 문의'}}
        relevant = {'issue_id': 80, 'content': 'CPU 사용량 증가',
                    'latest_issue': {'id': 80, 'summary': 'CPU 사용량 증가'}}
        def search(query, limit, constraints, semantic=False):
            docs = [unrelated, relevant]
            if constraints.get('semantic_focus_terms') == ['CPU']:
                docs = [relevant]
            return {'status': 'ok', 'documents': docs}
        with patch.object(similar, 'fetch_mantis_issue_by_id', return_value=base), \
             patch.object(similar, 'execute_chroma_search', side_effect=search):
            report = similar.build_similar_resolution_report('#100과 비슷한 과거 CPU 문제 원인과 조치 비교해줘')
        self.assertEqual({source['issue_id'] for source in report['sources']}, {100, 80})

    def test_declared_duplicate_is_not_presented_as_an_independent_case(self):
        base = {'id': 100, 'summary': '메모리 부족'}
        docs = [{'issue_id': 90, 'latest_issue': {'id': 90, 'summary': '[중복제외] 리소스 개선'}},
                {'issue_id': 80, 'latest_issue': {'id': 80, 'summary': '차트 리소스 개선'}}]
        with patch.object(similar, 'fetch_mantis_issue_by_id', return_value=base), \
             patch.object(similar, 'execute_chroma_search', return_value={'status': 'ok', 'documents': docs}):
            report = similar.build_similar_resolution_report('#100과 비슷한 과거 사례 원인과 조치 비교해줘')
        self.assertEqual({source['issue_id'] for source in report['sources']}, {100, 80})

    def test_report_does_not_repeat_the_full_action_in_list_and_table(self):
        base = {'id': 100, 'summary': '로그인 오류'}
        docs = [{'issue_id': 80, 'latest_issue': {'id': 80, 'summary': '인증 오류',
                 'resolution_fields': {'조치 및 개선': '서버 세션 타임아웃 30분으로 조정'}}}]
        with patch.object(similar, 'fetch_mantis_issue_by_id', return_value=base), \
             patch.object(similar, 'execute_chroma_search', return_value={'status': 'ok', 'documents': docs}):
            report = similar.build_similar_resolution_report('#100과 비슷한 과거 사례 원인과 조치 비교해줘')
        self.assertEqual(report['content'].count('서버 세션 타임아웃 30분으로 조정'), 1)

    def test_current_code_behavior_is_not_a_modification(self):
        self.assertEqual(similar._recorded_fix({'notes': [{'text': '오류내용 CLogicST Class에서만 NULL로 처리함.'}]}), ('', ''))

    def test_unnecessary_comparison_removal_and_virtual_method_change_are_actions(self):
        detail = {'notes': [{'id': 1, 'text': '불필요한 비교루틴 사용 수정.'},
                            {'id': 2, 'text': '상위 class인 CLogicItem에 SetPosition을 virtual함수로 추가해서 수정을 함.'}]}
        action, _ = similar._recorded_fix(detail)
        self.assertIn('불필요한 비교루틴', action)
        self.assertIn('SetPosition', action)

    def test_long_file_paths_do_not_truncate_evidence_identifiers(self):
        detail = {'notes': [
            {'id': 70001, 'text': '리비전: 10\nU   '+('VeryLongPath/'*20)+'ChartMng.cpp'},
            {'id': 70002, 'text': 'QC 결과 정상적으로 작동하였습니다(OK)'},
            {'id': 70003, 'text': '[고객사전달]'},
        ]}
        action, _ = similar._recorded_fix(detail)
        for token in ('ChartMng.cpp', '[노트 70001]', '[노트 70002]', '[노트 70003]'):
            self.assertIn(token, action)
        self.assertLessEqual(len(action), 220)

    def test_comparison_evidence_restores_technical_notes_before_latest_twelve(self):
        raw = {'id': 10, 'summary': '차트 메모리 누수', 'notes': [
            {'id': 1, 'text': '타이머 삭제 로직 수정 완료.'},
            *[{'id': index, 'text': '진행 내용 확인'} for index in range(2, 16)],
        ]}
        with patch.object(chroma, 'rank_chroma_documents_semantically', return_value=([
                {'issue_id': 10, 'content': '차트 메모리 누수'}], {})), \
             patch.object(chroma, 'fetch_mantis_issue_by_id', return_value=raw), \
             tempfile.NamedTemporaryFile() as placeholder, \
             patch.object(chroma, 'CHROMA_DB_PATH', Path(placeholder.name)):
            result = chroma.execute_chroma_search('메모리 누수', 3,
                {'resolution_evidence_query': '메모리 누수 원인 실제 수정 조치'}, semantic=True)
        detail = result['documents'][0]['latest_issue']
        self.assertIn('타이머 삭제', similar._recorded_fix(detail)[0])

    def test_duplicate_closure_history_survives_no_candidate_result(self):
        raw = {'id': 100, 'summary': '전략차트 메모리 부족', 'notes': [
            {'id': 22, 'text': '# 110 이슈와 동일한 이슈로써 종료된 이슈로 처리합니다.'}]}
        with patch.object(similar, 'fetch_mantis_issue_by_id', return_value=raw), \
             patch.object(similar, 'execute_chroma_search', return_value={'status': 'ok', 'documents': []}):
            report = similar.build_similar_resolution_report('#100과 비슷한 과거 사례 원인과 조치 비교해줘')
        for token in ('110', '동일한 이슈', '22'):
            self.assertIn(token, report['content'])
        self.assertNotIn('완료된 해결', report['content'])
        from backend.evaluation.verification import build_verification_report
        checked = build_verification_report('#100과 비슷한 과거 사례 원인과 조치 비교해줘',
            report['content'], report['sources'], report['retrieval'], report['raw']['mode'])
        self.assertEqual(next(c['status'] for c in checked['checks'] if c['id'] == 'issue_ids'), 'pass')

    def test_cause_question_heading_and_future_work_are_not_causes(self):
        for text in ('■ or 사용시 전체 조건식 검사하는 부분 개선이 어려운 원인',
                     '1) 원인파악후 임시파일이 남아 있지 않게 처리',
                     '임시저장된 설정파일들이 남아 있는 경우가 있어 원인을 파악중에 있습니다'):
            with self.subTest(text=text):
                self.assertEqual(similar._recorded_cause({'notes': [{'text': text}]}), ('', ''))

    def test_user_data_or_future_reply_is_not_customer_fix_delivery(self):
        issue = {'notes': [{'id': 1, 'text': '고객 설정파일 전달드립니다. 확인 부탁드립니다.'},
                           {'id': 2, 'text': '설정내역 원하시면 전달해드리겠습니다.'}]}
        self.assertEqual(similar._progress_evidence(issue), [])
        issue = {'notes': [{'id': 3, 'text': '수정 모듈 전달'}]}
        self.assertIn('노트 3', ' '.join(similar._progress_evidence(issue)))

    def test_empty_model_action_does_not_hide_explicit_two_completed_fixes(self):
        issue = {'comparison_facts': {'verified': True, 'cause': [], 'action': []},
                 'notes': [{'id': 10, 'text': '1. delete 추가\n함수 호출시마다 new 가 계속 발생함\n함수에서 delete 함.\n(기존 수정부분 최소화 이유로 위와같이 수정함, 호출 부분에서 미리 delete 수정해도 무방)\n'
                            '2. CString 멤버 변수 초기화 추가\n해당 함수에 초기화 부분을 추가함.\n'
                            '위와 같이 2곳 수정하여 메모리 릭 해결하였습니다.'}]}
        actual = similar._recorded_fix(issue)[0]
        self.assertIn('delete', actual)
        self.assertIn('초기화', actual)
        self.assertIn('노트 10', actual)

    def test_validated_followup_topic_is_not_lost_when_seed_title_omits_cpu(self):
        base = {'id': 100, 'summary': '신차트 시세처리 개선 문의'}
        with patch.object(similar, 'extract_query_constraints', return_value={
                'issue_ids': [100], 'unified_plan_validated': True}), \
             patch.object(similar, 'fetch_mantis_issue_by_id', return_value=base), \
             patch.object(similar, 'execute_chroma_search', return_value={'status': 'ok', 'documents': []}) as search:
            similar.build_similar_resolution_report('고객사 조건만 좁혀줘', validated_plan=True, topic_terms=['CPU', '부하'])
        self.assertEqual(search.call_args.args[2]['semantic_focus_terms'], ['CPU'])
        self.assertIn('CPU', search.call_args.args[0])

    def test_schedule_change_is_not_a_technical_fix(self):
        issue = {'notes': [{'id': 1, 'text': '3.23 오픈일정이 4.9로 변경됨'}]}
        self.assertEqual(similar._recorded_fix(issue), ('', ''))

    def test_empty_model_cause_preserves_explicit_description_mechanism(self):
        issue = {'comparison_facts': {'verified': True, 'cause': [], 'action': []},
                 'description': '하나의 스크립트 파싱을 위해 db Loading / parsing / compiling 이 매번 이루어짐.',
                 'notes': [{'id': 1, 'text': '완료된 작업 종료처리 합니다.'}]}
        cause, origin = similar._recorded_cause(issue)
        self.assertIn('db Loading', cause)
        self.assertEqual(origin, 'description')

    def test_direct_symptom_mechanism_wins_over_later_improvement_limitation(self):
        issue = {'id':100, 'summary':'장초반 CPU 사용률 문의', 'notes':[
            {'id':1,'text':'첫 체결과 장 초반 시세가 많이 발생함에 따라 자원을 많이 사용 하는 것입니다.'},
            {'id':2,'text':'현재 프로그램 구조상 전략식의 검사를 할수가 없다.'}]}
        cause,origin=similar._recorded_cause(issue)
        self.assertIn('시세',cause)
        self.assertNotIn('검사를 할수가 없다',cause)
        self.assertEqual(origin,'note')

    def test_completed_operator_guidance_is_action_without_claiming_it_was_executed(self):
        issue={'notes':[{'id':7,'text':'운영자에게 사용자 많은 전략을 확인 하는 방법과 해당전략을 서비스 종료시키는 방법 가이드 해줌.'}]}
        action,_=similar._recorded_fix(issue)
        self.assertIn('가이드 해줌',action)
        self.assertIn('노트 7',action)
        self.assertNotIn('서비스 종료 완료',action)
        for pending in ['운영자에게 서비스 종료 방법 안내 예정', '운영자에게 서비스 종료 방법 가이드 요청']:
            self.assertEqual(similar._recorded_fix({'notes':[{'text':pending}]}),('',''))

    def test_resource_load_answer_is_not_hidden_by_earlier_qa_in_the_same_note(self):
        issue={'notes':[{'id':1,'text':'장시작 체결은 시가 데이터가 수정되어 질수 있기 때문에 첫 체결은 항상 처리하고 있는것입니다.\n'
                                  '첫 체결과 장 초반 시세가 많이 발생함에 따라 자원을 많이 사용 하는 것입니다.'}]}
        self.assertIn('자원',similar._recorded_cause(issue)[0])

    def test_customer_complaint_is_a_consequence_not_the_cpu_issue_cause(self):
        issue={'summary':'실시간 시세 체결 시 CPU 부하 개선',
               'notes':[{'id':1,'text':'결과적으로는 성능 이슈로 인해 점차 고객 불만이 증가를 하고 있습니다.'}]}
        self.assertEqual(similar._recorded_cause(issue),('',''))

    def test_known_defect_fix_does_not_override_unresolved_symptom_cause(self):
        detail = {'id': 99, 'summary': '차트 메모리 증가', 'notes': [
            {'id': 1, 'text': '일부 할당 메모리를 Release 하지 않는 부분이 확인되어 수정하여 전달하려고 합니다.'},
            {'id': 2, 'text': '원인이 명확하지 않아 즉시 반영은 어렵습니다.'},
            {'id': 3, 'text': '누수가 있다고 단정하기 어려우며 힙 재사용 동작일 가능성이 있습니다.'},
            {'id': 4, 'text': 'TimeMeterPDLL Delete 되지 않는 부분 확인. 소멸자에서 해제하도록 처리함. 처리 후 측정 시 메모리 증가량 변화는 없음.'}],
            'comparison_facts': {'verified': True, 'action': [], 'cause': [
                {'id': 'i99p1', 'field': 'note:4', 'text': 'TimeMeterPDLL Delete 되지 않는 부분 확인'}]}}
        self.assertEqual(similar._recorded_cause(detail), ('', ''))
        self.assertIn('명확하지', similar._unconfirmed_cause(detail))
        self.assertIn('소멸자', similar._recorded_fix(detail)[0])
        self.assertIn('변화는 없음', similar._latest_resolution_caveat(detail))

    def test_later_explicit_cause_confirmation_can_resolve_earlier_uncertainty(self):
        detail = {'notes': [{'id': 1, 'text': '원인이 명확하지 않아 확인 중입니다.'},
                            {'id': 2, 'text': '재현 테스트에서 중복 타이머 등록이 원인으로 확인되었습니다.'}]}
        self.assertIn('중복 타이머', similar._recorded_cause(detail)[0])
