import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.intent.explanation import explanation_constraints, wants_issue_explanation
from backend.query_planning import infer_operation
from backend.sources.mantis_answer_evidence import build_answer_evidence
from backend.understanding import extract_query_constraints


class ExplanationIntentTests(unittest.TestCase):
    def test_financial_metric_does_not_override_explicit_broker_scope(self):
        from backend.common import text_matches_customer_aliases
        query = ('NH 조건검색에서 유동자산·유동부채가 0억원으로 나온다는 건 왜 그랬어? '
                 '마지막 회신에서 확인된 범위와 우리 쪽 수정이 필요하다고 했는지 정리해줘.')
        self.assertFalse(text_matches_customer_aliases('유동부채', ['동부']))
        self.assertTrue(text_matches_customer_aliases('동부증권 유동부채', ['동부']))
        constraints = extract_query_constraints(query)
        self.assertEqual(constraints['customer_key'], 'NH')
        self.assertTrue(wants_issue_explanation(query, constraints))

    def test_later_conclusion_and_reproduction_conditions_route_to_explanation(self):
        queries = [
            'KB H-able에서 DeMark가 분봉에는 안 보인다는 문의, 뒤에 확인한 결론은 뭐야? 1·2분봉도 되는지와 표시하려면 필요한 데이터 조건을 알려줘.',
            '신한 SOL 차트에서 2000봉 넘게 조회하면 죽던 건, 제한을 없애도 괜찮아진 거야? Android와 iOS 요청 차이, 나중에 다시 재현된 조건과 최종 안내를 알려줘.',
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_narrative_reproduction_does_not_inherit_model_open_filter(self):
        query = ('신한 SOL 차트에서 2000봉 넘게 조회하면 죽던 건, 제한을 없애도 괜찮아진 거야? '
                 'Android와 iOS 요청 차이, 나중에 다시 재현된 조건과 최종 안내를 알려줘.')
        constraints = extract_query_constraints(query)
        constraints['statuses'] = ['open']
        constraints['filter_clauses'].append({'field': 'status', 'operator': 'in', 'value': ['open']})
        constraints['status_resolution'] = {
            'origin': 'llm', 'status_intent': 'open',
            'evidence': 'Android와 iOS 요청 차이, 나중에 다시 재현된 조건과 최종 안내를 알려줘',
        }
        corrected = explanation_constraints(query, constraints)
        self.assertEqual(corrected['statuses'], [])
        self.assertFalse(any(clause['field'] == 'status' for clause in corrected['filter_clauses']))
        self.assertEqual(corrected['customer_key'], '신한')
        self.assertEqual(constraints['statuses'], ['open'])

        # 이슈 상태를 명시하거나 이전 턴에서 상속한 범위는 그대로 적용한다.
        explicit = '신한 SOL에서 미종료 이슈 중 최종 안내를 설명해줘'
        explicit_constraints = extract_query_constraints(explicit)
        self.assertEqual(explanation_constraints(explicit, explicit_constraints), explicit_constraints)
        inherited = {**constraints, 'inherited_from_audit_id': 'confirmed-status'}
        self.assertEqual(explanation_constraints(query, inherited), inherited)

        pending = '신한 SOL에서 아직 손봐야 할 이슈의 최종 안내를 설명해줘'
        pending_constraints = extract_query_constraints(pending)
        pending_constraints['statuses'] = ['open']
        pending_constraints['filter_clauses'].append({'field': 'status', 'operator': 'in', 'value': ['open']})
        pending_constraints['status_resolution'] = {
            'origin': 'llm', 'status_intent': 'open', 'evidence': '아직 손봐야 할 이슈',
        }
        self.assertEqual(explanation_constraints(pending, pending_constraints), pending_constraints)

    @patch('backend.intent.scope_extraction.get_customer_alias_groups',
           return_value=[['우리선물', '143WooriFuture(우리선물)', '우리']])
    def test_single_issue_pronoun_is_not_a_customer_filter(self, _aliases):
        for pronoun in ['우리가', '우리는', '우리도', '우리를']:
            query = f'Mantis #12345 고객의 주장과 {pronoun} 확인한 원인을 구분해서 설명해줘'
            with self.subTest(pronoun=pronoun):
                original = extract_query_constraints(query)
                corrected = explanation_constraints(query, original)
                self.assertIsNone(corrected['customer_key'])
                self.assertEqual(corrected['customer_aliases'], [])
                self.assertEqual(corrected['issue_ids'], [12345])
                self.assertFalse(any(c['field'] == 'customer' for c in corrected['filter_clauses']))
                self.assertEqual(original['customer_key'], '우리')

    @patch('backend.intent.scope_extraction.get_customer_alias_groups',
           return_value=[['우리선물', '143WooriFuture(우리선물)', '우리']])
    def test_explicit_woori_and_inherited_customer_scopes_are_preserved(self, _aliases):
        for query in ['Mantis #12345 우리선물에서 우리가 확인한 원인을 설명해줘',
                      'Mantis #12345 우리 HTS에서 우리는 무엇을 확인했는지 설명해줘']:
            original = extract_query_constraints(query)
            self.assertEqual(explanation_constraints(query, original), original)
        query = 'Mantis #12345 우리가 확인한 원인을 설명해줘'
        inherited = extract_query_constraints(query)
        inherited['inherited_from_audit_id'] = 'confirmed-customer-scope'
        self.assertEqual(explanation_constraints(query, inherited), inherited)
        # 단일 이슈 설명 밖의 고객사·집계 해석은 이번 교정에서 변경하지 않는다.
        query = '우리 HTS 이슈 몇 건이야?'
        self.assertFalse(wants_issue_explanation(query, extract_query_constraints(query)))

    @patch('backend.intent.scope_extraction.get_customer_alias_groups',
           return_value=[['우리선물', '143WooriFuture(우리선물)', '우리']])
    def test_pronoun_correction_passes_plan_and_keeps_real_source_customer_checks(self, _aliases):
        from backend.query_planning import build_query_plan, validate_query_plan
        from backend.sources.mantis import issue_matches_constraints
        query = 'Mantis #31462 고객이 제시한 원인과 우리가 확인한 반례를 나눠서 설명해줘'
        constraints = explanation_constraints(query, extract_query_constraints(query))
        plan = {**build_query_plan(query, {'source': 'mantis'}, constraints),
                'operation': 'explain', 'resolved_constraints': constraints}
        self.assertTrue(validate_query_plan(query, plan)['valid'])
        target = {'id': 31462, 'project': {'name': '147MiraeAsset(미래에셋증권)'},
                  'status': {'name': 'closed'}}
        self.assertTrue(issue_matches_constraints(target, constraints))
        self.assertFalse(issue_matches_constraints({**target, 'id': 31463}, constraints))
        explicit = 'Mantis #31462 우리선물에서 우리가 확인한 원인을 설명해줘'
        explicit_constraints = explanation_constraints(explicit, extract_query_constraints(explicit))
        self.assertFalse(issue_matches_constraints(target, explicit_constraints))

    def test_evidence_judgment_and_delivery_distinction_use_explanation_path(self):
        for query in [
            'Mantis #12345 차트의 1차 전달부터 배포 제외 여부까지 근거로 판단해줘.',
            'IBK 차트 모듈을 다시 보낸 것이 최적화 수정이었어? 별도 개선 일정과 마지막 전달을 구분해줘.',
        ]:
            with self.subTest(query=query):
                self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_narrative_open_and_remaining_items_do_not_filter_target_issue_status(self):
        for query in [
            'Mantis #12345 차트는 개발완료 상태이고 정상 오픈 메일도 있으니 배포까지 끝난 건가? 배포 제외 여부까지 근거로 판단해줘.',
            'Mantis #12345 팝업 수정에서 남은 항목을 플랫폼별로 나누고 마지막 전달만으로 해결됐다고 해도 되는지 알려줘.',
        ]:
            with self.subTest(query=query):
                constraints = explanation_constraints(query, extract_query_constraints(query))
                self.assertEqual(constraints['statuses'], [])
                self.assertFalse(any(c['field'] == 'status' for c in constraints['filter_clauses']))
                self.assertEqual(constraints['issue_ids'], [12345])

    def test_actual_status_filters_and_counts_survive_narrative_status_correction(self):
        for query in [
            '하나 HTS 미종료 이슈 중 남은 항목의 원인을 설명해줘',
            '하나 HTS에서 상태=개발완료인 이슈 중 배포 경위를 설명해줘',
            '종료 이슈 제외하고 마지막 전달의 근거로 판단해줘',
        ]:
            with self.subTest(query=query):
                original = extract_query_constraints(query)
                self.assertEqual(explanation_constraints(query, original), original)
        for query in ['하나 HTS 남은 이슈는 총 몇 건이야?',
                      '하나 HTS 개발완료 이슈의 담당자별 건수와 공수 합계를 알려줘']:
            with self.subTest(query=query):
                self.assertFalse(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_deployment_exclusion_history_passes_real_explanation_plan_validation(self):
        from backend.query_planning import build_query_plan, validate_query_plan
        query = 'Mantis #12345 차트의 1차 전달부터 배포 제외 여부까지 근거로 판단해줘.'
        constraints = explanation_constraints(query, extract_query_constraints(query))
        plan = {**build_query_plan(query, {'source': 'mantis'}, constraints),
                'source': 'mantis', 'operation': 'explain', 'resolved_constraints': constraints}
        checked = validate_query_plan(query, plan)
        self.assertFalse(any(c['id'] == 'negative_intent' and c['status'] == 'fail'
                             for c in checked['checks']), checked)
        self.assertTrue(checked['valid'], checked)
        # 실제 이슈 제외를 지운 잘못된 계획은 같은 검증기가 계속 거부해야 한다.
        negative_query = '종료 이슈 제외하고 마지막 전달의 근거로 판단해줘'
        invalid_plan = {**plan, 'filters': {}, 'filter_clauses': [], 'resolved_constraints': {}}
        rejected = validate_query_plan(negative_query, invalid_plan)
        self.assertTrue(any(c['id'] == 'negative_intent' and c['status'] == 'fail'
                            for c in rejected['checks']))

    def test_comparing_outcomes_and_counting_changes_inside_one_issue(self):
        for query in ['Mantis #30240 두 문제의 1차 결과와 이후 최종 확인·배포 상태를 나눠줘.',
                      'Mantis #31460 수정내역 재작업 건에서 함께 넣기로 한 것은 최종 몇 건이야? 처음 제외한 두 항목의 이유와 이후 바뀐 최종 목록을 알려줘.']:
            self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))
        query = 'Mantis #31460과 비슷한 이슈는 몇 건이야?'
        self.assertFalse(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_narrative_excluded_changes_do_not_disable_actual_exclusion_checks(self):
        from backend.intent.explanation import explanation_filter_text
        query = '처음 제외한 두 항목의 이유를 설명해줘'
        self.assertNotIn('제외', explanation_filter_text(query))
        query = '종료 이슈 제외하고 원인을 설명해줘'
        self.assertEqual(explanation_filter_text(query), query)
        self.assertNotIn('없이', explanation_filter_text('수정 뒤 문제없이 끝났어?'))
        self.assertEqual(explanation_filter_text('담당자 없는 이슈를 설명해줘'), '담당자 없는 이슈를 설명해줘')

    def test_test_scope_and_followup_contact_are_explanations(self):
        for query in ['Mantis #31041 교보 주식위클리옵션 건도 소스를 롤백한 거야? 종목 검색창과 0332 속성 처리, 실제 테스트 범위를 알려줘.',
                      '미래에셋 9580 거래량 음수 문제는 어디에 확인하도록 했어?']:
            self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_technical_day_values_do_not_create_a_registration_date_filter(self):
        query = '교보 차트 환율이 당일 값인지 전일 값인지 최종 확인한 내용은 뭐야?'
        original = extract_query_constraints(query)
        self.assertTrue(wants_issue_explanation(query, original))
        self.assertIsNone(explanation_constraints(query, original)['created_at'])
        query = '교보 전일 등록된 이슈 중 환율의 전일 값을 설명해줘'
        original = extract_query_constraints(query)
        self.assertEqual(explanation_constraints(query, original)['created_at'], original['created_at'])

    def test_requested_output_format_and_history_are_explanations(self):
        query = '미래에셋 차트 표시 단위는 어떤 형식으로 통일하기로 했어? 전달 이력을 알려줘'
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_rollback_evidence_document_hint_preserves_issue_explanation(self):
        query = ('한화에서 롤백했던 작업들을 다시 보내 달라는 건, 바로 재전달할 수 있었어? '
                 '먼저 필요했던 자료와 마지막 QC·전달 기록을 짧게 정리해줘.')
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))
        for document_query in ('한화 운영 매뉴얼 자료를 정리해줘',
                               '한화 노션에서 롤백 자료와 QC 전달 기록을 찾아줘'):
            self.assertFalse(wants_issue_explanation(document_query, extract_query_constraints(document_query)))

    def test_narrative_confirmation_is_not_a_status_filter(self):
        query = ('유진 5032 차트에서 지표 설정이 사라진 문제는 구차트 전환이 원인이었어? '
                 '고객 파일에서 확인된 것과 절전·재접속 가설, 마지막 처리 결론을 근거와 함께 짧게 알려줘.')
        original = extract_query_constraints(query)
        self.assertTrue(wants_issue_explanation(query, original))
        corrected = explanation_constraints(query, original)
        self.assertEqual(corrected['statuses'], [])
        self.assertFalse(any(clause['field'] == 'status' for clause in corrected['filter_clauses']))
        explicit = '유진 확인된 이슈 중 고객 파일에서 확인된 내용을 설명해줘'
        explicit_original = extract_query_constraints(explicit)
        self.assertEqual(explanation_constraints(explicit, explicit_original)['statuses'], ['confirmed'])

    def test_unrequested_default_team_does_not_hide_customer_explanation(self):
        query = ('SK증권 Windows 11 스마트 앱 컨트롤에서 DLL 로드가 막힌 문의는 결국 어떻게 판단했어? '
                 '초기 재현 상황과 오류 코드 근거, 실제 대응 범위를 구분해서 알려줘.')
        original = extract_query_constraints(query)
        self.assertTrue(wants_issue_explanation(query, original))
        self.assertEqual(original['mantis_team_scope'], 'default')
        self.assertNotIn('mantis_team_members', explanation_constraints(query, original))
        team_query = '우리 팀의 SK증권 Windows 11 DLL 로드 오류 원인을 설명해줘'
        team_original = extract_query_constraints(team_query)
        self.assertEqual(explanation_constraints(team_query, team_original)['mantis_team_scope'], 'team')
    def test_broker_alias_does_not_match_stock_name_or_other_broker(self):
        from backend.common import text_matches_customer_aliases
        self.assertIsNone(extract_query_constraints("삼성전자 종목 오류 원인 알려줘")["customer_key"])
        self.assertEqual(extract_query_constraints("삼성 MTS 차트 원인 알려줘")["customer_key"], "삼성")
        self.assertFalse(text_matches_customer_aliases("130SamsungFutures(삼성선물)", ["삼성", "삼성증권", "107Samsung"]))

    def test_explanation_instructions_are_not_mandatory_search_keywords(self):
        from backend.intent.explanation import explanation_search_terms
        query = "하나 차트 오류 원인을 설명해줘"
        self.assertEqual(explanation_search_terms(query, extract_query_constraints(query)), ["차트", "오류"])
        query = "Mantis #31088과 유사한 이슈들의 원인과 조치를 비교해줘"
        self.assertEqual(explanation_search_terms(query, extract_query_constraints(query)), [])
        query = "Mantis #31088과 비슷한 수정주가 이슈의 원인을 설명해줘"
        self.assertTrue(any("수정주" in term for term in explanation_search_terms(query, extract_query_constraints(query))))
    def test_quantity_list_stays_on_existing_list_path(self):
        query = "하나 HTS 이슈 목록 10건을 원인과 함께 정리해줘"
        self.assertFalse(wants_issue_explanation(query, extract_query_constraints(query)))
        for query in ["하나 이슈 진행 상황에서 종료된 이슈가 몇 개야?",
                      "하나 HTS 이슈 진행 상황을 알려줘. 총 몇 개인지 알려줘"]:
            self.assertFalse(wants_issue_explanation(query, extract_query_constraints(query)))
        query = "하나 #11598 출력 필드가 몇 개로 늘었는지 이유를 설명해줘"
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))
        query = "하나 결과필드 크기를 바꾼 이슈의 변경 전후 개수와 배열 크기를 근거로 알려줘"
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_final_technical_fields_and_note_authors_are_explanations(self):
        query = "하나 HTS 최종 FID와 장후 값을 알려주고 발신자와 노트 등록자를 구분해줘"
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_how_to_answered_question_routes_to_issue_explanation(self):
        query = ("NH HTS 멀티종합차트에서 사용자지표를 가격차트와 같은 Y축으로 맞추는 "
                 "방법은 무엇이라고 답했어? 지표관리 탭에서 바로 가능한지와 실제 안내한 "
                 "우회 절차를 구분해 줘.")
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_completion_mentioned_as_question_is_not_status_filter(self):
        for query in (
            '신한 조건검색에서 전략 저장 시 관심그룹 설정도 같이 저장할 수 있냐는 문의에 '
            '최종적으로 뭐라고 답했어? 개발 완료인지도 근거로 구분해 줘.',
            '한국투자 MTS 차트 라이브러리 버전 관리는 현재 어떻게 되고 있고 '
            '다음 전달 때 무엇을 하겠다고 했어? 실제 버전 적용 완료와 계획을 구분해 줘.',
        ):
            with self.subTest(query=query):
                original = extract_query_constraints(query)
                self.assertTrue(wants_issue_explanation(query, original))
                resolved = explanation_constraints(query, original)
                self.assertEqual(resolved['statuses'], [])
                self.assertEqual(resolved['excluded_statuses'], [])

    def test_last_closure_as_history_does_not_filter_explanation_status(self):
        query = ('IBK 7111 종합차트에서 종목명과 차트 데이터가 달랐다는 문의는 재현됐어? '
                 '고객 추가 회신과 마지막 종료·재접수 조건을 근거로 설명해줘.')
        original = extract_query_constraints(query)
        self.assertTrue(wants_issue_explanation(query, original))
        self.assertEqual(explanation_constraints(query, original)['statuses'], [])

    def test_explanation_and_aggregate_fixtures(self):
        path = Path(__file__).parent / "fixtures/routing_cases.jsonl"
        for line in path.read_text().splitlines():
            case = json.loads(line)
            if "issue_explanation" in case:
                with self.subTest(query=case["q"]):
                    constraints = extract_query_constraints(case["q"])
                    self.assertEqual(wants_issue_explanation(case["q"], constraints),
                                     case["issue_explanation"])
                    if "explanation_statuses" in case:
                        self.assertEqual(explanation_constraints(case["q"], constraints)["statuses"],
                                         case["explanation_statuses"])
                    if "operation" in case:
                        self.assertEqual(infer_operation(case["q"]), case["operation"])

    def test_narrative_status_is_not_a_filter_but_explicit_filters_survive(self):
        for query in ("하나 7500 손익그래프 검토 이슈의 계산식을 설명해줘",
                      "Mantis #31088 종료 사유와 실제 반영 상태를 설명해줘",
                      "미래에셋 차트 기능 요청은 개발 완료된 거야? 마지막 진행 상황을 알려줘"):
            self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))
            self.assertEqual(explanation_constraints(query, extract_query_constraints(query))["statuses"], [])
        query = '하나 HTS에서 2025년 등록된 종료된 이슈 중 담당자 김보람의 원인과 조치를 설명해줘'
        original = extract_query_constraints(query)
        self.assertEqual(explanation_constraints(query, original), original)

    def test_completion_as_answer_subject_does_not_filter_explanation(self):
        queries = (
            '코스콤 오래된 유저파일 문의에서 정리 기능 개발 완료로 단정하지 말고 근거를 알려줘.',
            '동부증권 3500 피보나치 문의는 마지막 기록이 QC 완료야, 요청이야? 근거로 알려줘.',
        )
        for query in queries:
            with self.subTest(query=query):
                original = extract_query_constraints(query)
                corrected = explanation_constraints(query, original)
                self.assertTrue(wants_issue_explanation(query, original))
                self.assertEqual(corrected['statuses'], [])
                self.assertFalse(any(item['field'] == 'status' for item in corrected['filter_clauses']))
                self.assertNotIn('mantis_team_scope', corrected)
        explicit = '코스콤에서 상태=개발완료인 이슈 중 오래된 유저파일 문의를 설명해줘'
        original = extract_query_constraints(explicit)
        self.assertEqual(explanation_constraints(explicit, original), original)

    def test_implemented_question_routes_to_explanation(self):
        query = 'SK증권 3201 차트 정규장 종가 옵션은 결국 구현했어? 검토 근거를 알려줘.'
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_qc_completion_history_does_not_filter_issue_status(self):
        query = ('코스콤 일부 회원사에서 정규장 옵션이 안 보인 건 무슨 조건에서 재현됐고, '
                 '수정·QC·고객 전달은 어디까지 기록됐어? 예정과 완료를 구분해 근거로 알려줘.')
        original = extract_query_constraints(query)
        corrected = explanation_constraints(query, original)
        self.assertTrue(wants_issue_explanation(query, original))
        self.assertEqual(corrected['statuses'], [])
        self.assertFalse(any(item['field'] == 'status' for item in corrected['filter_clauses']))
        explicit = '코스콤 종료 상태인 이슈 중 QC 완료와 고객 전달을 구분해서 설명해줘'
        scoped = extract_query_constraints(explicit)
        self.assertEqual(explanation_constraints(explicit, scoped), scoped)

    def test_explicit_review_and_inherited_status_are_not_erased(self):
        query = "하나 검토 이슈 중 원인을 설명해줘"
        original = extract_query_constraints(query)
        self.assertEqual(explanation_constraints(query, original), original)
        inherited = {"statuses": ["closed"], "inherited_from_audit_id": "confirmed",
                     "filter_clauses": [{"field": "status", "operator": "in", "value": ["closed"]}]}
        self.assertEqual(explanation_constraints("그중 종료 사유를 설명해줘", inherited), inherited)

    def test_explanation_plan_is_independently_validated(self):
        from backend.query_planning import validate_query_plan
        query = "하나 HTS가 종료된 이슈의 원인을 설명해줘"
        constraints = explanation_constraints(query, extract_query_constraints(query))
        plan = {"source": "mantis", "operation": "explain", "resolved_constraints": constraints,
                "filter_clauses": constraints["filter_clauses"], "filters": {"statuses": constraints["statuses"]}}
        self.assertTrue(validate_query_plan(query, plan)["valid"])
        self.assertFalse(validate_query_plan("하나 이슈 몇 건이야", plan)["valid"])

    def test_explanation_bypasses_work_report_and_planner(self):
        from backend.services.chat_service import _resolve_chat_plan
        from backend.services.contracts import ChatPipelineContext
        from backend.schemas import ChatRequest
        query = "Mantis #31088 원인과 실제 반영 상태를 설명해줘"
        context = ChatPipelineContext(query, {}, "", [], query, {"mantis": True}, [],
                                      extract_query_constraints(query), 5)
        with patch("backend.reports.work_queries.build_work_query_report") as work:
            self.assertIsNone(_resolve_chat_plan(ChatRequest(modelId="test", messages=[{"role": "user", "content": query}]), context, None))
        work.assert_not_called()
        self.assertEqual(context.intent_plan["operation"], "explain")


class AnswerEvidenceTests(unittest.TestCase):
    def test_preserves_long_note_tail_and_latest_reversal_with_offsets(self):
        raw = {"id": 9, "description": "재현 순서", "notes": [
            {"id": 10, "created_at": "2025-01-01", "text": "안내\n" * 2000 + "GetEndPrice = (매도가 - 매수가) * 2.5;"},
            {"id": 11, "created_at": "2025-01-02", "text": "수정 완료. 운영 반영은 보류하였으며 아직 SVN 커밋하지 않았습니다."},
        ]}
        result = build_answer_evidence(raw, "GetEndPrice 계산식과 실제 반영 상태", max_chars=4200)
        self.assertLessEqual(result["provided_chars"], 4200)
        self.assertFalse(result["complete"])
        self.assertIn("GetEndPrice = (매도가 - 매수가) * 2.5;", "".join(e["text"] for e in result["excerpts"]))
        self.assertIn(11, [e.get("note_id") for e in result["excerpts"]])
        for item in result["excerpts"]:
            source = next(n["text"] for n in raw["notes"] if n["id"] == item["note_id"]) if item.get("note_id") else raw[item["field"]]
            self.assertEqual(source[item["start"]:item["end"]], item["text"])

    def test_short_notes_complete_and_in_model_grounding(self):
        from backend.sources.mantis import normalize_issue
        from backend.retrieval import build_model_retrieval_context
        raw = {"id": 9, "summary": "오류", "notes": [{"id": 15, "text": "Android 정상, iOS 부호 판독 오류 수정."}]}
        issue = normalize_issue(raw, evidence_query="어느 플랫폼 오류야")
        result = build_model_retrieval_context({"stages": [{"status": "ok", "source": "mantis_id_lookup", "issues_list": [issue]}]}, 5)
        evidence = result["issues"][0]["answer_evidence"]
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["excerpts"][-1]["note_id"], 15)
        self.assertIn("Android 정상", evidence["excerpts"][-1]["text"])


class ExplanationRetrievalTests(unittest.TestCase):
    def test_single_technical_identifier_remains_a_retrieval_focus(self):
        from backend.retrieval import run_retrieval_pipeline
        query = '신한 CMF 추세 조건검색 추가 요청은 개발 완료된 거야? 상세 수식 조건을 알려줘.'
        with patch('backend.retrieval.execute_chroma_search',
                   return_value={'source': 'chroma_db', 'status': 'ok', 'issues_list': []}) as search:
            run_retrieval_pipeline(query, 5, {'mantis': True, 'vectorDb': True},
                                   {'operation': 'explain',
                                    'resolved_constraints': extract_query_constraints(query)})
        self.assertEqual(search.call_args.args[2]['semantic_focus_terms'], ['CMF'])

    def test_generic_similarity_uses_base_symptom_and_preserves_exclusions(self):
        from backend.retrieval import run_retrieval_pipeline
        query = "Mantis #31088과 비슷한 이슈는 어떻게 해결했어?"
        constraints = extract_query_constraints(query)
        constraints["exclude_issue_ids"] = [17]
        constraints["filter_clauses"].append({"field": "issue_id", "operator": "not_in", "value": [17]})
        base = {"source": "mantis_id_lookup", "status": "ok", "issues_list": [{"id": 31088, "summary": "AssetTree NULL 오류"}]}
        with patch("backend.retrieval.execute_mantis_id_lookup", return_value=base), patch(
            "backend.retrieval.execute_chroma_search", return_value={"source": "chroma_db", "status": "ok"}
        ) as search:
            run_retrieval_pipeline(query, 5, {"mantis": True, "vectorDb": True},
                                   {"operation": "explain", "resolved_constraints": constraints})
        self.assertIn("AssetTree", search.call_args.args[0])
        self.assertEqual(search.call_args.args[2]["exclude_issue_ids"], [31088, 17])
        self.assertIn({"field": "issue_id", "operator": "not_in", "value": [17]}, search.call_args.args[2]["filter_clauses"])

    def test_disabling_vector_search_keeps_mantis_read_and_full_evidence_lookup(self):
        from backend.retrieval import run_retrieval_pipeline
        query = "하나 차트 오류 원인을 설명해줘"
        stage = {"source": "mantis_page_scan", "status": "ok", "issues_list": [{"id": 10}]}
        with patch("backend.retrieval.execute_mantis_page_scan", return_value=stage) as scan, patch(
            "backend.retrieval.execute_mantis_id_lookup", return_value=stage
        ) as lookup, patch("backend.retrieval.execute_chroma_search") as vector:
            run_retrieval_pipeline(query, 5, {"mantis": True, "vectorDb": False},
                                   {"operation": "explain", "resolved_constraints": extract_query_constraints(query)})
        scan.assert_called_once()
        self.assertEqual(scan.call_args.args[0], "차트 오류")
        vector.assert_not_called()
        self.assertEqual(lookup.call_args.args[1]["answer_query"], query)

    def test_latest_answer_keeps_compound_setting_in_lexical_candidates(self):
        from backend.retrieval import run_retrieval_pipeline
        query = ('신한 조건검색에서 전략 저장 시 관심그룹 설정도 같이 저장할 수 있냐는 문의에 '
                 '최종적으로 뭐라고 답했어? 개발 완료인지도 근거로 구분해 줘.')
        constraints = explanation_constraints(query, extract_query_constraints(query))
        stage = {'source': 'mantis_page_scan', 'status': 'ok', 'issues_list': [{'id': 31563}]}
        with patch('backend.retrieval.execute_mantis_page_scan', return_value=stage) as scan, patch(
            'backend.retrieval.execute_chroma_search', return_value={'source': 'chroma_db', 'status': 'ok'}
        ):
            result = run_retrieval_pipeline(query, 5, {'mantis': True, 'vectorDb': True},
                                            {'operation': 'explain', 'resolved_constraints': constraints})
        scan.assert_called_once()
        self.assertEqual(scan.call_args.args[0], '관심그룹 저장')
        self.assertEqual(result['stages'][0]['issues_list'][0]['id'], 31563)
        self.assertEqual(result['stages'][0]['source'], 'mantis_explanation_focus')

    def test_explanation_focus_issue_survives_semantic_display_limit(self):
        from backend.retrieval import collect_retrieved_issues
        focus = {'source': 'mantis_explanation_focus', 'status': 'ok', 'issues_list': [
            {'id': 31563, 'summary': '관심그룹 설정값 포함 가능 여부', 'project': {'name': '신한'}},
        ]}
        vector = {'source': 'chroma_explanation', 'status': 'ok', 'documents': [
            {'issue_id': 100 + rank, 'similarity_rank': rank, 'content': '과거 관심그룹 이슈'}
            for rank in range(1, 6)
        ]}
        result = {'intent_plan': {'operation': 'explain'}, 'constraints': {}, 'stages': [focus, vector]}
        ids = [row['id'] for row in collect_retrieved_issues(result, 5)]
        self.assertEqual(ids[0], 31563)

    def test_precise_feasibility_title_excludes_older_similar_issues(self):
        from backend.retrieval import run_retrieval_pipeline
        query = ('신한 조건검색에서 전략 저장 시 관심그룹 설정도 같이 저장할 수 있냐는 문의에 '
                 '최종적으로 뭐라고 답했어? 개발 완료인지도 근거로 구분해 줘.')
        constraints = explanation_constraints(query, extract_query_constraints(query))
        stage = {'source': 'mantis_page_scan', 'status': 'ok', 'issues_list': [
            {'id': 31563, 'summary': "[게시판] 전략저장 시 '관심그룹' 설정값 포함 가능 여부"},
        ]}
        with patch('backend.retrieval.execute_mantis_page_scan', return_value=stage), patch(
            'backend.retrieval.execute_mantis_id_lookup', return_value={
                'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': stage['issues_list'],
            }
        ) as lookup, patch('backend.retrieval.execute_chroma_search') as vector:
            result = run_retrieval_pipeline(query, 5, {'mantis': True, 'vectorDb': True},
                                            {'operation': 'explain', 'resolved_constraints': constraints})
        lookup.assert_called_once()
        self.assertEqual(lookup.call_args.args[0], [31563])
        vector.assert_not_called()
        self.assertEqual({row['id'] for row in result['stages'][-1]['issues_list']}, {31563})

    def test_screen_mismatch_question_prefers_matching_current_title(self):
        from backend.retrieval import run_retrieval_pipeline
        query = ('IBK 7111 종합차트에서 종목명과 차트 데이터가 달랐다는 문의는 재현됐어? '
                 '고객 추가 회신과 마지막 종료·재접수 조건을 근거로 설명해줘.')
        constraints = explanation_constraints(query, extract_query_constraints(query))
        stage = {'source': 'mantis_page_scan', 'status': 'ok', 'issues_list': [
            {'id': 31521, 'summary': '[7111] 종합차트 종목에 맞지 않는 차트가 표시되는 현상'},
        ]}
        with patch('backend.retrieval.execute_mantis_page_scan', return_value=stage) as scan, patch(
            'backend.retrieval.execute_mantis_id_lookup', return_value={
                'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': stage['issues_list'],
            }
        ) as lookup, patch('backend.retrieval.execute_chroma_search') as vector:
            result = run_retrieval_pipeline(query, 5, {'mantis': True, 'vectorDb': True},
                                            {'operation': 'explain', 'resolved_constraints': constraints})
        self.assertEqual(scan.call_args.args[0], '7111 종목 차트')
        self.assertEqual(lookup.call_args.args[0], [31521])
        vector.assert_not_called()
        self.assertEqual({row['id'] for row in result['stages'][-1]['issues_list']}, {31521})


class ExplanationVerificationTests(unittest.TestCase):

    def test_feature_status_is_distinct_from_explicit_issue_status(self):
        from backend.evaluation.issue_facts import extract_issue_claims
        prefix = "#29729\n**2. 보류된 기능**\n* **기능:** 그룹 순서 변경\n"
        self.assertEqual(extract_issue_claims(prefix + "* **상태:** 보류")[1], [])
        for text in ["#29729\n상태: 보류", prefix + "현재 상태: 보류",
                     prefix + "#29729 이슈 상태: 보류", prefix + "**3. 이슈 정보**\n상태: 보류",
                     prefix + "#29730\n상태: 보류", prefix + "1. ID: #29730 | 제목: 정상 제목\n상태: 보류",
                     "| ID | 상태 |\n|---|---|\n|29729|보류|",
                     '{"id":29729,"상태":"보류"}']:
            with self.subTest(text=text):
                self.assertTrue(any(field == "status" for _, field, _ in extract_issue_claims(text)[1]))

    def test_topic_labels_are_not_issue_metadata_fields(self):
        from backend.evaluation.issue_facts import extract_issue_claims
        text = "#9\n* 문제 플랫폼: 아이폰이 잘못되었습니다.\n* 마지막 진행 상태:\n  * 롤백 후 재반영 예정"
        self.assertEqual(extract_issue_claims(text)[1], [])
        for label in ["플랫폼", "상태", "현재 상태", "이슈 상태", "Mantis 상태", "현재 담당자",
                      "이슈 제목", "현재 플랫폼", "이슈 등록일", "이슈의 담당자", "이슈의 제목", "Mantis의 상태"]:
            self.assertTrue(extract_issue_claims(f"#9\n{label}: 잘못된 값")[1])

    def test_bold_id_followed_by_prose_is_not_an_issue_title_claim(self):
        from backend.evaluation.issue_facts import extract_issue_claims
        ids, claims = extract_issue_claims("**#29968** 이슈에서 Android가 정상인 이유는 다음과 같습니다.")
        self.assertEqual(ids, {29968})
        self.assertEqual(claims, [])
        self.assertIn((29968, "title", "잘못된 제목"), extract_issue_claims("**#29968 잘못된 제목**")[1])

    def test_handler_honorific_and_closing_parenthesis_are_presentation(self):
        from backend.evaluation.issue_facts import check_issue_fields, extract_issue_claims
        source = [{"issue_id": 9, "handler": "김진성"}]
        for text in ("#9 담당자는 김진성 님입니다.", "#9 기존 기능으로 안내했습니다. (담당자: 김진성)"):
            self.assertEqual(check_issue_fields(extract_issue_claims(text)[1], source)["status"], "pass")
        self.assertEqual(check_issue_fields(extract_issue_claims("#9 담당자는 다른사람 님입니다.")[1], source)["status"], "fail")


if __name__ == "__main__":
    unittest.main()
