"""원문과 이슈 경계 검증을 우회하면 실제 답변이 달라지는 회귀 사례."""
import copy
import importlib.util
import unittest


class ExplanationQuoteTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.evaluation.explanation_quotes"),
                             "원문 인용 검증 모듈이 필요합니다")
        from backend.evaluation.explanation_quotes import render_quote_answer, validate_quote_answer
        self.validate = validate_quote_answer
        self.render = render_quote_answer
        self.source = "검토 결과입니다.\n개발 완료는 아니며 반영을 요청했습니다.\n다음 배포 예정입니다."
        self.grounding = {"issues": [
            {"id": 101, "summary": "가격 계산 오류", "answer_evidence": {"excerpts": [
                {"field": "note:7", "text": self.source, "start": 0, "end": len(self.source),
                 "note_id": 7, "created_at": "2026-10-01", "reporter": "김기록"},
                {"field": "description", "text": "GetEndPrice = (매도가 - 매수가) * 2.5;\nFID = 1048;",
                 "start": 0, "end": 57}]}},
            {"id": 102, "summary": "별도 완료 이슈", "answer_evidence": {"excerpts": [
                {"field": "note:7", "text": "반영 완료했습니다.", "start": 0, "end": 10,
                 "note_id": 7, "reporter": "이개발"}]}}
        ]}

    def payload(self, quote="개발 완료", *, topic="action", issue_id=101, field="note:7"):
        return {"selected_issue_ids": [101], "sections": [{"topic": topic,
                "evidence": [{"issue_id": issue_id, "field": field, "quote": quote}], "unanswered": False}]}

    def test_shared_plan_before_two_issue_headers_keeps_only_the_common_statement(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 31525, 'answer_evidence': {'excerpts': [{
            'field': 'note:111384', 'note_id': 111384, 'text':
            '작업 가능하며 이전에 문의 주신 컨트롤이 겹치는 현상과 함께 주중에 작업 진행 후\n'
            '테스트하여 전달드릴 수 있도록 하겠습니다.\n\n'
            '0031502: 다른 화면 개선 요청\n0031525: 화면 크기 저장 요청',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(len(texts), 1)
        self.assertIn('작업 가능하며', texts[0])
        self.assertNotIn('0031502', texts[0])

    def test_access_credentials_are_not_selected_or_rendered_from_another_excerpt_of_the_note(self):
        from backend.services.grounded_explanation import evidence_catalog, timeline_evidence
        excerpts = self.grounding['issues'][0]['answer_evidence']['excerpts']
        excerpts.extend([{'field': 'note:8', 'note_id': 8, 'text': '테스트 환경 접근 안내'},
                         {'field': 'note:8', 'note_id': 8, 'text': 'ID: fictional-user\n\nPW: fixture-only-secret'}])
        shown, catalog = evidence_catalog(self.grounding)
        self.assertNotIn('fixture-only-secret', str(shown))
        self.assertFalse(any(e['field'] == 'note:8' for e in catalog.values()))
        self.assertFalse(any(e['field'] == 'note:8' for e in timeline_evidence(self.grounding, [101])))
        result = self.validate(self.payload('테스트 환경 접근 안내', field='note:8'), self.grounding)
        self.assertFalse(result['valid'])
        result = self.validate(self.payload(), self.grounding)
        self.assertIn('접속정보', self.render(result, self.grounding))

    def test_mixed_mail_note_keeps_only_target_issue_bullet_block(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 29942, 'answer_evidence': {'excerpts': [
            {'field': 'note:104736', 'note_id': 104736, 'text': '2025-10-10 메일 수신'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '- 0029979: ETF 분리 요청'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '    - 서버 개발 완료'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '- 0029948: 파생 차트 요청'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '    - 운영 반영 예정'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '- 0029942: 신고가 조건 저장 오류'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '    - 금주 작업 예정'},
            {'field': 'note:104736', 'note_id': 104736, 'text': '    - QC 10월 3주차 진행 예정'},
        ]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(texts, ['- 0029942: 신고가 조건 저장 오류', '    - 금주 작업 예정',
                                 '    - QC 10월 3주차 진행 예정'])

    def test_mixed_numbered_mail_note_excludes_other_issue_schedules(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 31127, 'answer_evidence': {'excerpts': [
            {'field': 'note:111369', 'note_id': 111369, 'text': '총 VOC 3건 가운데,'},
            {'field': 'note:111369', 'note_id': 111369,
             'text': '1. [31127] 선물예상가 표시 요청\n > 테스트시 계속 표시가 안되는걸 확인했습니다.'},
            {'field': 'note:111369', 'note_id': 111369,
             'text': '2. [31385] 컬럼 변경\n > 10월 1일 운영서버 반영 예정'},
            {'field': 'note:111369', 'note_id': 111369,
             'text': '3. [31405] 차트 더블클릭\n > 10월 1일 운영서버 반영 예정'},
            {'field': 'note:111369', 'note_id': 111369, 'text': '※ 반영시 메일 공유 예정'},
        ]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(texts, [
            '1. [31127] 선물예상가 표시 요청\n > 테스트시 계속 표시가 안되는걸 확인했습니다.',
        ])

    def test_mixed_issue_boundaries_inside_one_raw_note_keep_only_target_block(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 29942, 'answer_evidence': {'excerpts': [{
            'field': 'note:104736', 'note_id': 104736,
            'text': '2025-10-10 메일 수신\r\n\r\n'
                    '- 0029979: 다른 이슈\r\n    - 개발 완료\r\n\r\n'
                    '- 0029942: 대상 이슈\r\n    - 금주 작업 예정\r\n    - QC 예정',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(texts, ['- 0029942: 대상 이슈\n    - 금주 작업 예정\n    - QC 예정'])

    def test_numbered_screen_is_not_an_issue_header_and_explicit_shared_mail_is_scoped(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 28638, 'answer_evidence': {'excerpts': [
            {'field': 'note:1', 'note_id': 1,
             'text': '0028638: 대상 이슈\n0028644: 다른 이슈\n\n'
                     '두 이슈의 원인과 전달 일정을 안내합니다.\n\n'
                     '해당 두 이슈의 현상은 차트 OCX 추세선 공유 메모리 copy 누락입니다.\n\n'
                     '해당 부분 수정 작업 및 QC진행 후 12월 27일에 전달드릴 예정입니다.\n\n'
                     '별도 고객의 배포 완료 기록은 이 건에 속하지 않습니다.'},
            {'field': 'note:2', 'note_id': 2,
             'text': '2025-02-04 메일 수신 From. 고객\n\n'
                     '아래 [9580] 항목이 정상적으로 동작하지 않습니다.\n\n'
                     '2. 0560 주식차트에서 9580 설정을 가져오기'},
        ]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertIn('해당 부분 수정 작업 및 QC진행 후 12월 27일에 전달드릴 예정입니다.', texts)
        self.assertNotIn('별도 고객의 배포 완료 기록은 이 건에 속하지 않습니다.', texts)
        self.assertNotIn('0028638: 대상 이슈\n0028644: 다른 이슈', texts)
        self.assertIn('2. 0560 주식차트에서 9580 설정을 가져오기', texts[-1])

    def test_bracketed_screen_numbers_preserve_last_recheck_in_evidence_catalog(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        from backend.services.grounded_explanation import evidence_catalog
        note = ('2025.11.17 메일 회신 To. 고객\n\n'
                '0029719: [0662] 신호검색 화면 - 신규 상장종목 조회 시 비정상종료\n\n'
                '금일 상장 종목으로 재확인했습니다.\n\n'
                '[0156] 신규상장종목 화면에서 장 시작 전 신규상장 종목을 클릭하여 '
                '[0662] 신호검색 화면에서 일, 주, 월, 년, 초 주기 조회는 정상, '
                '분, 틱 주기 조회는 비정상종료됩니다.\n\n'
                '일, 주, 월, 년, 초 주기는 정상적으로 조회가 완료되었습니다.\n\n'
                '분, 틱 주기는 해당자료가 없습니다.\n\nTR : IVU10060')
        issue = {'id': 29719, 'summary': '신규 상장종목 조회 오류',
                 'answer_evidence': {'excerpts': [
                     {'field': 'note:105676', 'note_id': 105676, 'text': note}]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertIn('분, 틱 주기 조회는 비정상종료됩니다.', '\n'.join(texts))
        self.assertIn('TR : IVU10060', '\n'.join(texts))
        shown, _catalog = evidence_catalog({'issues': [issue]})
        self.assertIn('분, 틱 주기 조회는 비정상종료됩니다.', str(shown))
        self.assertIn('TR : IVU10060', str(shown))

    def test_bracketed_four_digit_issue_with_colon_still_splits_mixed_note(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 1234, 'answer_evidence': {'excerpts': [{
            'field': 'note:3', 'note_id': 3,
            'text': '[1234]: 대상 이슈\n\n대상 이슈의 QC는 예정입니다.\n\n'
                    '[5678]: 다른 이슈\n\n다른 이슈는 배포 완료했습니다.',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(texts, ['[1234]: 대상 이슈', '대상 이슈의 QC는 예정입니다.'])

    def test_explicit_prior_issue_reference_keeps_only_one_explanation_paragraph(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        from backend.services.grounded_explanation import evidence_catalog
        issue = {'id': 28144, 'answer_evidence': {'excerpts': [{
            'field': 'note:97612', 'note_id': 97612,
            'text': '0028144: 대상 CCI 문의\n\n'
                    '아래 이슈와 같이 동일한 내용으로 문의 접수되었던 이력이 있어 '
                    '관련 내용 참고하여 전달 드립니다.\n'
                    '0018988: 과거 CCI 문의\n\n'
                    '현재 신차트에서는 모든 지표를 반올림 처리하여 신·구 차트 값이 '
                    '다르게 표시되는 것으로 확인됩니다.\n\n'
                    '과거 이슈는 패치 완료했습니다.\n\n감사합니다.',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertIn('현재 신차트에서는 모든 지표를 반올림 처리하여 신·구 차트 값이 '
                      '다르게 표시되는 것으로 확인됩니다.', texts)
        self.assertNotIn('0018988: 과거 CCI 문의', texts)
        self.assertNotIn('과거 이슈는 패치 완료했습니다.', texts)
        _shown, catalog = evidence_catalog({'issues': [issue]})
        self.assertTrue(any('반올림 처리하여 신·구 차트 값이' in row['quote']
                            for row in catalog.values()))

    def test_prior_issue_reference_does_not_import_other_issue_status(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 28144, 'answer_evidence': {'excerpts': [{
            'field': 'note:97612', 'note_id': 97612,
            'text': '0028144: 대상 문의\n\n아래 이슈와 동일한 내용이라 참고하여 전달 드립니다.\n'
                    '0018988: 과거 문의\n\n현재 과거 이슈는 QC 완료됐다고 확인됩니다.\n\n'
                    '수정 파일도 배포 완료했습니다.\n\n0029000: 무관한 이슈\n\n'
                    '무관한 이슈의 고객사 전달 완료',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertTrue(any('0028144: 대상 문의' in text for text in texts))
        self.assertFalse(any('QC 완료' in text or '배포 완료' in text or '고객사 전달 완료' in text
                             for text in texts))

    def test_conclusion_summarizes_latest_periodic_recheck_without_claiming_a_fix(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = {'issues': [{'id': 29719, 'summary': '[0662] 신규상장종목 조회 비정상종료',
                                  'answer_evidence': {'complete': True, 'excerpts': [
                                      {'field': 'note:103471', 'note_id': 103471,
                                       'created_at': '2025-08-06',
                                       'text': 'p_buf 값이 빈 문자열이 되어 CheckValidSize 가 FALSE 를 리턴하면 '
                                               'pDataBuf, pStartBuf 는 채워지지 않고 NULL 인 상태로 남게되어 '
                                               '비정상종료가 발생하는 것으로 보임'},
                                      {'field': 'note:105676', 'note_id': 105676,
                                       'created_at': '2025-11-17',
                                       'text': '0029719: [0662] 신호검색 화면 신규상장종목 조회\n\n'
                                               '[0156] 신규상장종목 화면에서 장 시작 전 종목을 클릭하여 '
                                               '[0662] 신호검색 화면에서 일, 주, 월, 년, 초 주기 조회 시 '
                                               '정상 조회되지만 분, 틱 주기 조회 시 비정상종료 됩니다.\n\n'
                                               'TR : IVU10060\n\n전에 보내드린 내용과 더불어 확인 부탁드립니다.'},
                                  ]}}]}
        payload = {'selected_issue_ids': [29719], 'sections': [
            {'topic': 'cause', 'unanswered': False,
             'evidence': [{'issue_id': 29719, 'field': 'note:103471', 'quote': 'p_buf 값이 빈 문자열이 되어'}]},
            {'topic': 'timeline', 'unanswered': False,
             'evidence': timeline_evidence(grounding, [29719])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('빈 p_buf', 'CheckValidSize', 'FALSE', 'pDataBuf', 'pStartBuf',
                         '일·주·월·년·초', '분·틱', 'IVU10060', '노트 105676',
                         '해결 완료 근거는'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('모든 주기에서 비정상', conclusion)
        self.assertNotIn('수정 완료 기록이 있습니다', conclusion)

    def test_conclusion_marks_grid_limit_as_a_hypothesis(self):
        grounding = {'issues': [{'id': 27442, 'summary': '[2054] 실시간 감시 집계 차이',
                                  'answer_evidence': {'complete': True, 'excerpts': [{
                                      'field': 'note:94512', 'note_id': 94512,
                                      'created_at': '2024-03-19',
                                      'text': '실시간은 조회 기준 200개까지 지원하지만 그리드는 100개까지만 '
                                              '관리하기 때문에 그리드에 없는 항목에서 문제가 발생하는게 '
                                              '아닐까 예상됨\n조회가 130 종목이라 가정했을 때 '
                                              '관리하지 않은 30개 종목에서 신호가 발생하면 꼬이는 것 같음',
                                  }]}}]}
        payload = {'selected_issue_ids': [27442], 'sections': [{
            'topic': 'cause', 'unanswered': False,
            'evidence': [{'issue_id': 27442, 'field': 'note:94512', 'quote': '실시간은 조회 기준'}],
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('200개', '100개', '그리드 밖 종목', '추정', '노트 94512'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('확정된 원인', conclusion)

    def test_conclusion_keeps_confirmed_symptom_separate_from_unfound_cause(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = {'issues': [{'id': 28456, 'summary': '가격 & BOX 선 표시 반전',
                                  'answer_evidence': {'complete': True, 'excerpts': [
                                      {'field': 'steps_to_reproduce', 'text': '1. 화면 오픈\n2. 상한선과 중심선이 반대로 표시되는 현상 확인됨'},
                                      {'field': 'note:98558', 'note_id': 98558, 'created_at': '2024-10-18',
                                       'text': '현재 현상 확인되어 원인 확인 중인 것으로 안내함'},
                                      {'field': 'note:98567', 'note_id': 98567, 'created_at': '2024-10-21',
                                       'text': '[QC요청]'},
                                      {'field': 'note:98592', 'note_id': 98592, 'created_at': '2024-10-22',
                                       'text': '[QC 완료]'},
                                      {'field': 'note:98678', 'note_id': 98678, 'created_at': '2024-10-25',
                                       'text': '[고객사 전달]'},
                                  ]}}]}
        payload = {'selected_issue_ids': [28456], 'sections': [
            {'topic': 'cause', 'unanswered': True,
             'evidence': [{'issue_id': 28456, 'field': 'steps_to_reproduce', 'quote': '상한선과 중심선이 반대로'}]},
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 28456, 'field': 'note:98678', 'quote': '[고객사 전달]'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [28456])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('원인 확인 중', '확정된 원인', 'QC 완료', '고객사 전달'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('원인 근거에는 “1.', conclusion)
        self.assertNotIn('조치 근거에는 “\\[고객사 전달', conclusion)

    def test_conclusion_separates_setscroll_defenses_from_later_tileblt_report(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = {'issues': [{'id': 30507, 'summary': 'Win11 차트 비정상 종료',
                                  'answer_evidence': {'complete': True, 'excerpts': [
                                      {'field': 'note:106981', 'note_id': 106981, 'created_at': '2026-01-28',
                                       'text': 'SetScrollInfo 호출 지점에서 크래시가 관찰됨. '
                                               '해당 구간에서 작업 없이 return FALSE로 빠지면 크래시가 재현되지 않음.\n\n'
                                               'SetScrollInfo 자체가 직접적인 원인이라기보다는 이미 깨진 '
                                               '메모리 또는 잘못된 상태로 진입한 양상으로 보임.\n\n'
                                               'CScrollBarEx::SetScrollInfo()에 예방 코드를 추가함.\n'
                                               '1) 핸들 유효성 체크 (GetSafeHwnd, IsWindow)\n'
                                               '2) Max/Range 값 유효성 체크 (range/page/pos 정규화 및 클램프)\n'
                                               '3) 마스크 변경 (SIF_ALL 지양 → SIF_RANGE | SIF_PAGE | SIF_POS로 제한)'},
                                      {'field': 'note:107137', 'note_id': 107137, 'created_at': '2026-02-05',
                                       'text': '소스코드 수정이 필요해보여 검토 부탁드립니다.\n\n'
                                               'CScrollBarEx::TileBlt()의 nSourWid가 음수로 나와 '
                                               '무한루프가 발생하였습니다. 점검 및 수정 부탁드립니다.'},
                                      {'field': 'note:107180', 'note_id': 107180, 'created_at': '2026-02-06',
                                       'text': '[QC 완료]'},
                                      {'field': 'note:107185', 'note_id': 107185, 'created_at': '2026-02-06',
                                       'text': '[고객사 전달]'},
                                  ]}}]}
        payload = {'selected_issue_ids': [30507], 'sections': [
            {'topic': 'cause', 'unanswered': False,
             'evidence': [{'issue_id': 30507, 'field': 'note:106981', 'quote': 'SetScrollInfo 자체가'}]},
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 30507, 'field': 'note:106981', 'quote': '예방 코드를 추가함'},
                          {'issue_id': 30507, 'field': 'note:107137', 'quote': 'nSourWid가 음수로'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [30507])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('직접 원인으로 확정하지', 'GetSafeHwnd', 'IsWindow', 'Range', 'SIF_ALL',
                         'TileBlt', 'nSourWid', '음수', '무한루프', 'QC 완료', '고객사 전달'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('초기 확인에서는 같은 현상이 재현되지', conclusion)
        self.assertNotIn('TileBlt 수정 완료', conclusion)

    def test_conclusion_rejects_excel_hypothesis_and_records_provisional_close(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = {'issues': [{'id': 27777, 'summary': '[7111] 종목 변경 뒤 이전 데이터 표시',
                                  'answer_evidence': {'complete': True, 'excerpts': [
                                      {'field': 'note:95908', 'note_id': 95908, 'created_at': '2024-05-21',
                                       'text': '엑셀 불러오기 기능으로 다른 종목을 불러오기 한 뒤 '
                                               '기존 종목 실시간 데이터가 마지막에 업데이트 되는 현상은 있어 '
                                               '해당 현상은 아닌지 확인이 필요함'},
                                      {'field': 'note:95917', 'note_id': 95917, 'created_at': '2024-05-21',
                                       'text': '해당 기능은 사용자가 사용하지 않는다고 연락을 받았습니다.'},
                                      {'field': 'note:96530', 'note_id': 96530, 'created_at': '2024-06-25',
                                       'text': '요청 주신 현상은 재연이 되고 있지 않습니다. '
                                               '다시 재연될 때 재요청 주시면 다시 확인하겠습니다.'},
                                      {'field': 'note:96552', 'note_id': 96552, 'created_at': '2024-06-26',
                                       'text': '우선 종료 처리하고 다음에 재발생(재요청 시) 될 때 '
                                               '재현 스텝 등 정보를 바탕으로 로그 파일을 만드는 방향으로 진행 예정.'},
                                  ]}}]}
        payload = {'selected_issue_ids': [27777], 'sections': [
            {'topic': 'cause', 'unanswered': False,
             'evidence': [{'issue_id': 27777, 'field': 'note:95908', 'quote': '엑셀 불러오기 기능으로'},
                          {'issue_id': 27777, 'field': 'note:95917', 'quote': '해당 기능은 사용자가'}]},
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 27777, 'field': 'note:96530', 'quote': '재연이 되고 있지'},
                          {'issue_id': 27777, 'field': 'note:96552', 'quote': '우선 종료 처리하고'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [27777])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('엑셀 불러오기', '조사 가설', '사용하지 않았', '재현되지', '우선 종료',
                         '재발생', '재현 단계', '로그 파일', '예정'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('엑셀이 확정 원인', conclusion)
        self.assertNotIn('수정 완료', conclusion)

    def test_conclusion_places_freeze_rollback_between_qc_and_final_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 29957, 'summary': '순간체결량 제외종목 필터',
                 'answer_evidence': {'complete': True, 'excerpts': [
                     {'field': 'steps_to_reproduce', 'text': '1. 제외종목 체크\n2. 검색결과에 제외종목 표시'},
                     {'field': 'note:104456', 'note_id': 104456, 'created_at': '2025-09-23', 'text': '[QC 완료]'},
                     {'field': 'note:104465', 'note_id': 104465, 'created_at': '2025-09-23',
                      'text': '메리츠 증권 소스 프리징으로 인해 보류 및 롤백'},
                     {'field': 'note:104674', 'note_id': 104674, 'created_at': '2025-10-01', 'text': '[QC 요청]'},
                     {'field': 'note:104694', 'note_id': 104694, 'created_at': '2025-10-02', 'text': '[QC 완료]'},
                     {'field': 'note:104723', 'note_id': 104723, 'created_at': '2025-10-02', 'text': '[고객사 전달]'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [29957], 'sections': [
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 29957, 'field': 'steps_to_reproduce', 'quote': '제외종목 체크'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [29957])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('앞선 QC 완료', '소스 프리징', '보류·롤백', 'QC 요청', 'QC 완료', '고객사 전달'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('앞선 QC 완료'), conclusion.index('보류·롤백'))
        self.assertLess(conclusion.index('보류·롤백'), conclusion.index('고객사 전달'))
        self.assertNotIn('조치 근거에는 “1.', conclusion)

    def test_conclusion_shows_rsi_formula_interim_crash_and_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 28119, 'summary': 'RSI 강약 지표', 'answer_evidence': {'complete': True, 'excerpts': [
            {'field': 'note:97472', 'note_id': 97472, 'created_at': '2024-08-12',
             'text': '강세: RSI(적용기간) >= SPercent\n약세: RSI(적용기간) < LPercent\n\n'
                     'Input : Period(14), LPercent(30), SPercent(70);\nvalue = RSI(Period, Close);'},
            {'field': 'note:97609', 'note_id': 97609, 'created_at': '2024-08-19',
             'text': '운영 HTS에 수정된 모듈을 적용했을 때 [0600] 차트 화면에서 업종 시장 변경 시 '
                     'PctrSM.dll에서 비정상 종료가 발생되고 있습니다. 저희가 컴파일한 PctrSM.dll '
                     '사용 시에는 종료현상 발생하지 않으며, GetCodeUpIdx 함수가 원인인 것으로 보입니다.'},
            {'field': 'note:97700', 'note_id': 97700, 'created_at': '2024-08-22',
             'text': 'patnercodemaster, masterdatamanager 인터페이스 수정되어 발생하는 현상인듯합니다'},
            {'field': 'note:97713', 'note_id': 97713, 'created_at': '2024-08-23', 'text': '[QC 완료]'},
            {'field': 'note:97746', 'note_id': 97746, 'created_at': '2024-08-26', 'text': '[전달 완료]'},
        ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [28119], 'sections': [
            {'topic': 'technical', 'unanswered': False,
             'evidence': [{'issue_id': 28119, 'field': 'note:97472', 'quote': '강세: RSI'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [28119])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('기간 14', '기본 70', '기본 30', 'PctrSM.dll', '업종·시장 변경',
                         '자체 컴파일본', 'GetCodeUpIdx', '추정', 'QC 완료', '전달 완료'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('최종 원인은 GetCodeUpIdx', conclusion)

    def test_conclusion_explains_screenshot_mismatch_without_claiming_a_fix(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 30894, 'summary': '비교차트 종목 불일치',
                 'answer_evidence': {'complete': True, 'excerpts': [
                     {'field': 'note:108617', 'note_id': 108617, 'created_at': '2026-04-17',
                      'text': '첨부사진 확인결과\n\n좌측탭에는: 태양금속(K), 하나로FN K-반도체(K), '
                              'SK하이닉스(K) 만 추가되어 있음\n하지만 차트에는 태양금속(K) 종목이 4개, '
                              '2026선물 종목이 1개, 하나로FN K-반도체(K) 1개, '
                              'SK하이닉스(K) 1개 종목이 추가되어 있음;\n\n'
                              '비교차트에서는 종목을 중복으로 추가할 수 없는데 재현케이스 문의 예정'},
                     {'field': 'note:109348', 'note_id': 109348, 'created_at': '2026-05-29',
                      'text': '개발 담당자와 테스트했지만 현상은 발생되지 않았습니다. '
                              '소스상에서도 해당 현상은 발생되기 어렵다는 의견입니다.\n\n'
                              '해당이슈 우선은 종료처리하고자 하며, 추 후 현상이 나타나는 추가적인 정보나 '
                              '유저파일 제공해주신다면 재접수하여 다시 확인을 진행하고자 합니다.'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [30894], 'sections': [
            {'topic': 'cause', 'unanswered': False,
             'evidence': [{'issue_id': 30894, 'field': 'note:108617', 'quote': '좌측탭에는:'}]},
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 30894, 'field': 'note:109348', 'quote': '해당이슈 우선은 종료처리'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [30894])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('좌측 탭', '태양금속 4개', '2026선물', '재현되지', '우선 종료',
                         '유저파일', '재접수', '원인·수정 확정 근거'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('수정 완료', conclusion)

    def test_conclusion_tracks_cci_rounding_then_verified_formula_and_unknown_difference(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 28144, 'summary': '신구 차트 CCI 값 차이',
                 'answer_evidence': {'complete': True, 'excerpts': [
                     {'field': 'note:97612', 'note_id': 97612, 'created_at': '2024-08-19',
                      'text': '0028144: CCI 문의\n\n아래 이슈와 같이 동일한 내용으로 문의 접수되었던 이력이 있어 '
                              '관련 내용 참고하여 전달 드립니다.\n0018988: 과거 문의\n\n'
                              '현재 신차트(3600) 화면에서는 모든 지표에 대하여 반올림 처리를 하도록 '
                              '되어 있어서 CCI 지표 값이 신/구 차트가 다르게 표시되고 있는 것으로 확인됩니다.'},
                     {'field': 'note:97644', 'note_id': 97644, 'created_at': '2024-08-20',
                      'text': '한국금융IT 지표 검증식 엑셀 통하여 신차트 CCI 지표값 확인 결과 동일하게 표시되는 것 확인됨'},
                     {'field': 'note:97667', 'note_id': 97667, 'created_at': '2024-08-21',
                      'text': 'CCI 지표 계산식과 같이 정상 표시되며 고객사에서 이야기 하는 구차트인 3100 화면은 '
                              '한국금융IT에서 제공하는 차트가 아니므로 값이 다를 수 있으며\n'
                              '값이 다르게 표시되는 원인은 알 수 없음을 안내함'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [28144], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [28144])},
            {'topic': 'technical', 'unanswered': False,
             'evidence': [{'issue_id': 28144, 'field': 'note:97612', 'quote': '현재 신차트(3600)'},
                          {'issue_id': 28144, 'field': 'note:97644', 'quote': '지표 검증식 엑셀'},
                          {'issue_id': 28144, 'field': 'note:97667', 'quote': '구차트인 3100'}]},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('초기에는', '반올림', '검증식 엑셀', '정상 표시', '3100',
                         '한국금융IT 제공 차트가 아니', '정확한 원인은 알 수 없', '수정·패치 완료 근거'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('반올림'), conclusion.index('검증식 엑셀'))

    def test_unverified_completion_absence_requires_complete_history(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 27777, 'summary': '재현되지 않은 차트 문의',
                 'answer_evidence': {'complete': False, 'excerpts': [
                     {'field': 'note:1', 'note_id': 1, 'created_at': '2024-06-26',
                      'text': '우선 종료 처리하고 다음에 재발생(재요청 시) 될 때 '
                              '재현 스텝 등 정보를 바탕으로 로그 파일을 만드는 방향으로 진행 예정.'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [27777], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [27777])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertIn('우선 종료', conclusion)
        self.assertNotIn('수정 패치나 배포 완료 기록이 없습니다', conclusion)

    def test_long_detail_is_collapsed_but_every_quote_remains_available(self):
        from backend.services.grounded_explanation import timeline_evidence
        excerpts = [{'field': f'note:{number}', 'note_id': number,
                     'created_at': f'2026-01-{number:02d}',
                     'text': f'{number}번째 단계에서 확인한 동작과 후속 요청입니다. ' + '원문 기록 ' * 18}
                    for number in range(1, 25)]
        grounding = {'issues': [{'id': 101, 'summary': '긴 이력',
                                  'answer_evidence': {'complete': True, 'excerpts': excerpts}}]}
        payload = {'selected_issue_ids': [101], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101])},
        ]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        self.assertIn('<summary>전체 이력과 원문 근거 보기</summary>', rendered)
        self.assertLess(rendered.index('<details>'), rendered.index('### 진행 이력'))
        self.assertLess(rendered.index('### 진행 이력'), rendered.index('</details>'))
        self.assertIn('1번째 단계에서 확인한 동작', rendered)
        self.assertIn('24번째 단계에서 확인한 동작', rendered)

    def test_conclusion_distinguishes_initial_and_final_analysis_time_from_volume_followup(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 29575, 'summary': '분주기 미선택 거래량 차이',
                 'answer_evidence': {'complete': True, 'excerpts': [
                     {'field': 'note:102756', 'note_id': 102756, 'created_at': '2025-07-01',
                      'text': '원인 : 화면에서 분주기 선택을 하지 않아도 과거시점의 시간이 170000으로 서버에 올려줌. '
                              '서버에서는 170000으로 올라왔기 때문에 분주기로 판단. 해당일의 1분데이터 기반으로 '
                              '당일 일간 데이터 생성. 당일 1분데이터의 거래량 합이 일간 거래량과 맞지 않음.\n\n'
                              '수정 사항 : 화면에서 분주기 선택하지 않을 시 과거시간을 999999으로 설정 조회로 수정\n\n'
                              '원인의 3번은 담당자가 확인 해보기로 함'},
                     {'field': 'note:102797', 'note_id': 102797, 'created_at': '2025-07-02',
                      'text': '화면 확인 결과 : 데이터 요청 시 분석시점 시간을 분주기 미체크 시 '
                              '"170000" -> "000000" 으로 변경하였습니다.'},
                     {'field': 'note:102802', 'note_id': 102802, 'created_at': '2025-07-02', 'text': '[QC 요청]'},
                     {'field': 'note:102834', 'note_id': 102834, 'created_at': '2025-07-03', 'text': '[QC 완료]'},
                     {'field': 'note:102838', 'note_id': 102838, 'created_at': '2025-07-03',
                      'text': '[고객사 전달]'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [29575], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [29575])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('170000', '1분 데이터', '999999', '000000', '거래량 합',
                         '후속 해결 근거', 'QC 완료', '고객사 전달'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('999999'), conclusion.index('000000'))
        issue['answer_evidence']['complete'] = False
        incomplete = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('후속 해결 근거는', incomplete)

    def test_conclusion_separates_under_100_correction_from_load_limit_and_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 28221, 'summary': '조건실시간감시 시세 미갱신',
                 'answer_evidence': {'complete': True, 'excerpts': [
                     {'field': 'description',
                      'text': '검색결과 100건 초과 시 현재가와 등락률이 갱신되지 않습니다.\n\n'
                              '내용 정정합니다. 만족 종목수가 100건 미만일 경우에도 실시간 처리가 안되고 있습니다.'},
                     {'field': 'note:97831', 'note_id': 97831, 'created_at': '2024-09-02',
                      'text': '100건 제한은 화면에서 실시간 업데이트 시 부하 때문입니다. 화면에서 검토 할 내용입니다.'},
                     {'field': 'note:97878', 'note_id': 97878, 'created_at': '2024-09-03',
                      'text': '1951 실시간 시세건 관련\n1950 저장전략 삭제 팝업\n'
                              '해당 두 이슈는 현재 원인을 찾고 수정 작업중에 있습니다.'},
                     {'field': 'note:97998', 'note_id': 97998, 'created_at': '2024-09-10', 'text': '[QC요청]'},
                     {'field': 'note:98294', 'note_id': 98294, 'created_at': '2024-10-07', 'text': '[전달 완료]'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [28221], 'sections': [
            {'topic': 'background', 'unanswered': False,
             'evidence': [{'issue_id': 28221, 'field': 'description', 'quote': '내용 정정합니다.'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [28221])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('100건 초과', '100건 미만', '부하', '원인으로 확정',
                         '수정 작업 중', 'QC 요청', '전달 완료', 'QC 완료나 구체적 수정 코드'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('100건 제한이 확정 원인', conclusion)
        issue['answer_evidence']['complete'] = False
        incomplete = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('QC 완료나 구체적 수정 코드 내역은 확인되지', incomplete)
        issue['answer_evidence']['complete'] = True
        issue['answer_evidence']['excerpts'].append(
            {'field': 'note:98300', 'note_id': 98300, 'created_at': '2024-10-08', 'text': '[QC 완료]'})
        with_later_qc = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('QC 완료나 구체적 수정 코드 내역은 확인되지', with_later_qc)

    def test_conclusion_distinguishes_debug_assert_release_risk_and_latest_delete_analysis(self):
        from backend.services.grounded_explanation import timeline_evidence
        issue = {'id': 30699, 'summary': '대량 조회 DEBUG ASSERT',
                 'answer_evidence': {'complete': True, 'excerpts': [
                     {'field': 'note:107654', 'note_id': 107654, 'created_at': '2026-03-05',
                      'text': 'ASSERT는 디버깅 모드에서만 확인 가능. 하지만 ASSERT가 발생하는 경우는 '
                              '릴리즈 모드인 운영 버전에도 오류 발생 영향이 있을 수 있으므로 확인 필요'},
                     {'field': 'note:107658', 'note_id': 107658, 'created_at': '2026-03-05',
                      'text': '소리마치쪽 확인 필요한 내용으로 우리쪽에서 확인할 내용이 아니라 종료처리함'},
                     {'field': 'note:107712', 'note_id': 107712, 'created_at': '2026-03-06',
                      'text': '운영 카이로스에서 몇차례 반복해서 검색 실행하였으나 ASSERT 는 발생하지 않았습니다.'},
                     {'field': 'note:107714', 'note_id': 107714, 'created_at': '2026-03-06',
                      'text': '2026-03-06 메일 수신 From. 이준옥\n\n문제가 되는 부분은 데이터 조회가 클때 '
                              '사용 후 delete루틴을 호출하면서 발생하는 것으로 확인했습니다. '
                              '배포된 릴리즈 환경에서는 문제가 없는 듯 보일 수 있으나 비정상적인 동작을 '
                              '하고 있어서 비정기적으로 죽는 현상을 야기할 수 있는 루틴입니다.'},
                 ]}}
        grounding = {'issues': [issue]}
        payload = {'selected_issue_ids': [30699], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [30699])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('DEBUG ASSERT', '운영 버전', '소리마치', '운영 카이로스',
                         '재현되지', '이준옥', 'delete 루틴', '비정기', '노트 107714'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('문제가 없었다고 확정', conclusion)
        self.assertNotIn('마지막 기록에는 “', conclusion)

    def test_single_target_header_after_numbered_other_topic_drops_earlier_topic(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 30528, 'answer_evidence': {'excerpts': [{
            'field': 'note:9', 'note_id': 9,
            'text': '2026.01.28 회신 To. 고객\n\n'
                    '2) 일봉과 분봉 계산값 차이 문의\n\n'
                    '일봉과 분봉의 종가가 다른 문제입니다.\n\n'
                    '3. 0030528: 분봉 DeMark 표시 문의\n\n'
                    '전일 전체 봉이 차트에 조회되어야 지표가 표시됩니다.',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(texts, ['3. 0030528: 분봉 DeMark 표시 문의',
                                 '전일 전체 봉이 차트에 조회되어야 지표가 표시됩니다.'])

    def test_target_issue_number_as_mail_footer_does_not_drop_numbered_details(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 29193, 'answer_evidence': {'excerpts': [{
            'field': 'note:10', 'note_id': 10,
            'text': '1. Y축 표시값 28.05와 실제 값 2.805의 단위가 다릅니다.\n'
                    '서버 제공 봉 데이터 단위를 확인해야 합니다.\n\n'
                    '2. 소수점 둘째자리까지만 나오는 부분은 차트 수정이 필요합니다.\n\n'
                    '0029193: [3201] 해당 이슈',
        }]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertEqual(len(texts), 1)
        self.assertIn('서버 제공 봉 데이터', texts[0])
        self.assertIn('차트 수정', texts[0])

    def test_issue_footer_with_attachments_keeps_numbered_change_list(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        from backend.services.grounded_explanation import evidence_catalog, timeline_evidence
        paragraphs = [
            '요청사항 반영한 테스트소스 전달드립니다.',
            '1. 변경사항\n- 상품 종류 하위에 일반/소액 항목 추가',
            '2. 추가 변경사항\n- 데이터파일 1건과 소스파일 7건 변경',
            '감사합니다.',
            '0029193: [3201] 상품 종류 추가 요청',
            '[첨부파일]\nrelease/test-source.zip',
        ]
        for texts in (['\n\n'.join(paragraphs)], paragraphs):
            with self.subTest(paragraph_split=len(texts) > 1):
                grounding = {'issues': [{'id': 29193, 'answer_evidence': {'excerpts': [
                    {'field': 'note:10', 'note_id': 10, 'text': text} for text in texts
                ]}}]}
                safe = '\n'.join(row['text'] for row in safe_excerpts(grounding['issues'][0]))
                self.assertIn('요청사항 반영한 테스트소스 전달드립니다.', safe)
                self.assertIn('일반/소액 항목 추가', safe)
                _shown, catalog = evidence_catalog(grounding)
                self.assertTrue(any('일반/소액 항목 추가' in item['quote'] for item in catalog.values()))
                payload = {'selected_issue_ids': [29193], 'sections': [{
                    'topic': 'timeline', 'unanswered': False,
                    'evidence': timeline_evidence(grounding, [29193]),
                }]}
                validated = self.validate(payload, grounding)
                self.assertTrue(validated['valid'])
                self.assertIn('일반/소액 항목 추가', self.render(validated, grounding))

    def test_shared_package_delivery_preserves_only_common_application_condition(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        paragraphs = [
            '0029193: 대상 차트 표시 오류',
            '표시 오류 수정하여 라이브러리 전달드립니다.',
            '아래 내용 포함된 라이브러리라 이전 라이브러리도 같이 전달드립니다.\n'
            '0029194: 다른 화면의 탭 오류',
            '다른 화면 탭 오류는 수정 완료했습니다.',
            '충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.\n감사합니다.',
        ]
        split_reference = paragraphs[:2] + paragraphs[2].split('\n') + paragraphs[3:]
        for texts in (['\n\n'.join(paragraphs)], paragraphs, split_reference):
            with self.subTest(paragraph_count=len(texts)):
                issue = {'id': 29193, 'answer_evidence': {'excerpts': [
                    {'field': 'note:10', 'note_id': 10, 'text': text} for text in texts
                ]}}
                kept = '\n'.join(row['text'] for row in safe_excerpts(issue))
                self.assertIn('표시 오류 수정하여 라이브러리 전달드립니다.', kept)
                self.assertIn('충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.', kept)
                self.assertNotIn('다른 화면 탭 오류는 수정 완료', kept)

    def test_other_issue_application_condition_without_shared_package_stays_excluded(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 29193, 'answer_evidence': {'excerpts': [{
            'field': 'note:10', 'note_id': 10,
            'text': '0029193: 대상 차트 표시 오류\n\n표시 오류 수정했습니다.\n\n'
                    '0029194: 다른 화면의 탭 오류\n\n'
                    '충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.',
        }]}}
        kept = '\n'.join(row['text'] for row in safe_excerpts(issue))
        self.assertNotIn('테스트하시고', kept)

    def test_package_reference_inside_other_issue_does_not_restore_its_condition(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 29193, 'answer_evidence': {'excerpts': [{
            'field': 'note:10', 'note_id': 10,
            'text': '0029193: 대상 차트 표시 오류\n\n수정 파일 전달드립니다.\n\n'
                    '0029194: 다른 화면의 탭 오류\n\n'
                    '아래 내용 포함된 라이브러리라 이전 라이브러리도 같이 전달드립니다.\n\n'
                    '충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.',
        }]}}
        kept = '\n'.join(row['text'] for row in safe_excerpts(issue))
        self.assertNotIn('테스트하시고', kept)

    def test_package_declaration_after_other_issue_header_in_same_paragraph_is_excluded(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        paragraphs = [
            '0029193: 대상 차트 표시 오류',
            '수정 파일 전달드립니다.',
            '0029194: 다른 화면의 탭 오류\n'
            '아래 내용 포함된 라이브러리라 이전 라이브러리도 같이 전달드립니다.',
            '충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.',
        ]
        for texts in (['\n\n'.join(paragraphs)], paragraphs):
            with self.subTest(paragraph_split=len(texts) > 1):
                issue = {'id': 29193, 'answer_evidence': {'excerpts': [
                    {'field': 'note:10', 'note_id': 10, 'text': text} for text in texts
                ]}}
                kept = '\n'.join(row['text'] for row in safe_excerpts(issue))
                self.assertIn('수정 파일 전달드립니다.', kept)
                self.assertNotIn('테스트하시고', kept)

    def test_shared_package_condition_ends_at_an_unrelated_issue_boundary(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        paragraphs = [
            '0029193: 대상 차트 표시 오류',
            '수정 파일 전달드립니다.',
            '아래 내용 포함된 라이브러리라 이전 라이브러리도 같이 전달드립니다.\n'
            '0029194: 같이 포함한 탭 오류',
            '충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.',
            '0029195: 이번 패키지와 무관한 별도 서버 작업',
            '테스트 후 적용은 B서버에만 부탁드립니다.',
        ]
        for texts in (['\n\n'.join(paragraphs)], paragraphs):
            with self.subTest(paragraph_split=len(texts) > 1):
                issue = {'id': 29193, 'answer_evidence': {'excerpts': [
                    {'field': 'note:10', 'note_id': 10, 'text': text} for text in texts
                ]}}
                kept = '\n'.join(row['text'] for row in safe_excerpts(issue))
                self.assertIn('충분히 테스트하시고 적용시켜 주시면 감사하겠습니다.', kept)
                self.assertNotIn('B서버', kept)

    def test_fid_and_general_password_ui_terms_are_not_credential_assignments(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'answer_evidence': {'excerpts': [{'field': 'note:1', 'text': 'FID: 2037\n비밀번호 입력창의 글자가 잘립니다.'}]}}
        self.assertEqual(len(safe_excerpts(issue)), 1)

    def test_partial_quote_restores_negation_request_and_schedule(self):
        result = self.validate(self.payload(), self.grounding)
        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["sections"][0]["evidence"][0]["text"], self.source)
        rendered = self.render(result, self.grounding)
        for expected in ("개발 완료는 아니며", "반영을 요청했습니다", "다음 배포 예정", "#101", "가격 계산 오류",
                         "노트 7", "2026-10-01", "김기록"):
            self.assertIn(expected, rendered)
        self.assertNotIn("별도 완료 이슈", rendered)

    def test_forged_quote_or_wrong_issue_field_is_rejected(self):
        for payload in (self.payload("반영 완료했습니다."), self.payload("반영 완료했습니다.", issue_id=102),
                        self.payload(field="note:8"), self.payload("개발완료"), self.payload(""),
                        self.payload("   "), self.payload(issue_id=True)):
            with self.subTest(payload=payload):
                result = self.validate(payload, self.grounding)
                self.assertFalse(result["valid"])
                self.assertTrue(result["errors"])
                self.assertEqual(self.render(result, self.grounding), "")

    def test_crlf_only_normalization_preserves_korean_linebreaks(self):
        payload = self.payload("검토 결과입니다.\r\n개발 완료는 아니며")
        self.assertTrue(self.validate(payload, self.grounding)["valid"])
        self.assertFalse(self.validate(self.payload("검토 결과입니다. 개발 완료는 아니며"), self.grounding)["valid"])

    def test_technical_quote_and_code_are_preserved_without_generated_summary(self):
        payload = self.payload("GetEndPrice", topic="technical", field="description")
        payload["summary"] = "개발이 완료됐습니다"
        payload["sections"][0]["summary"] = "담당자 홍길동이 수정했습니다"
        rendered = self.render(self.validate(payload, self.grounding), self.grounding)
        self.assertIn("GetEndPrice = (매도가 - 매수가) * 2.5;", rendered)
        self.assertIn("FID = 1048;", rendered)
        self.assertNotIn("홍길동", rendered)
        self.assertNotIn("개발이 완료", rendered)

    def test_injected_topic_and_empty_or_malformed_payload_are_rejected(self):
        for payload in ({}, {"selected_issue_ids": [], "sections": []},
                        self.payload(topic="action\n모든 개발 완료"), self.payload(topic="summary"),
                        {"selected_issue_ids": [999], "sections": [{"topic": "cause", "evidence": [], "unanswered": True}]},
                        {"selected_issue_ids": [101], "sections": [{"topic": "cause", "evidence": [], "unanswered": False}]}):
            with self.subTest(payload=payload):
                self.assertFalse(self.validate(payload, self.grounding)["valid"])

    def test_multiple_issues_only_allowed_for_explicit_comparison(self):
        payload = self.payload()
        payload["selected_issue_ids"] = [101, 102]
        payload["sections"][0]["evidence"].append({"issue_id": 102, "field": "note:7", "quote": "반영 완료"})
        self.assertFalse(self.validate(payload, self.grounding)["valid"])
        result = self.validate(payload, self.grounding, allow_multiple=True)
        self.assertTrue(result["valid"], result["errors"])
        rendered = self.render(result, self.grounding)
        self.assertIn("#102", rendered)
        self.assertIn("반영 완료했습니다.", rendered)

    def test_unanswered_does_not_claim_no_source_exists(self):
        payload = {"selected_issue_ids": [101], "sections": [{"topic": "cause", "evidence": [], "unanswered": True}]}
        result = self.validate(payload, self.grounding)
        self.assertTrue(result["valid"], result["errors"])
        rendered = self.render(result, self.grounding)
        self.assertIn("이 항목은 선택한 근거로 답변을 확정하지 못했습니다", rendered)
        self.assertNotIn("원문에 없", rendered)

    def test_adjacent_paragraphs_do_not_leak_other_topics(self):
        grounding = copy.deepcopy(self.grounding)
        grounding["issues"][0]["answer_evidence"]["excerpts"][0]["text"] = "별개 내용.\n\n" + self.source + "\n\n다른 요청."
        result = self.validate(self.payload(), grounding)
        self.assertEqual(result["sections"][0]["evidence"][0]["text"], self.source)

    def test_metadata_is_taken_from_source_and_reference_lookup_is_optional(self):
        payload = self.payload()
        payload["sections"][0]["evidence"][0].update(note_id=999, reporter="가짜", created_at="2099-01-01")
        result = self.validate(payload, self.grounding)
        rendered = self.render(result, {"issues": []})
        self.assertIn("개발 완료는 아니며", rendered)
        self.assertIn("노트 7", rendered)
        self.assertNotIn("2099-01-01", rendered)
        self.assertNotIn("가짜", rendered)

    def test_repeated_quote_restores_each_matching_paragraph(self):
        grounding = copy.deepcopy(self.grounding)
        grounding["issues"][0]["answer_evidence"]["excerpts"][0]["text"] = (
            "개발 완료 여부 확인 요청.\n\n개발 완료는 아니며 반영 예정입니다.")
        rendered = self.render(self.validate(self.payload(), grounding), grounding)
        self.assertIn("개발 완료 여부 확인 요청", rendered)
        self.assertIn("개발 완료는 아니며 반영 예정", rendered)

    def test_huge_context_is_rejected_instead_of_cutting_away_negation(self):
        grounding = copy.deepcopy(self.grounding)
        grounding["issues"][0]["answer_evidence"]["excerpts"][0]["text"] = (
            "개발 완료" + " 검토" * 5000 + "라는 뜻은 아닙니다.")
        self.assertFalse(self.validate(self.payload(), grounding)["valid"])

    def test_model_markdown_inside_source_remains_literal_quoted_text(self):
        grounding = copy.deepcopy(self.grounding)
        grounding["issues"][0]["answer_evidence"]["excerpts"][0]["text"] = "```\n개발 완료는 아닙니다.\n```"
        rendered = self.render(self.validate(self.payload(), grounding), grounding)
        self.assertIn("> ````text\n> ```\n> 개발 완료는 아닙니다.\n> ```\n> ````", rendered)

    def test_malformed_values_and_duplicate_topics_fail_closed(self):
        duplicate_topic = self.payload()
        duplicate_topic["sections"] *= 2
        for payload in (None, [], {"selected_issue_ids": [True]},
                        {"selected_issue_ids": [101, 101]},
                        {"selected_issue_ids": [101], "sections": [None]},
                        {"selected_issue_ids": [101], "sections": [{"topic": []}]}, duplicate_topic):
            with self.subTest(payload=payload):
                self.assertFalse(self.validate(payload, self.grounding)["valid"])

    def verification_checks(self, grounding, payload):
        from backend.evaluation.verification import build_verification_report
        from backend.sources.mantis import build_sources_from_issue_rows
        row = {**grounding["issues"][0], "description": self.source,
               "notes": [{"id": 67890, "text": self.source}]}
        content = self.render(self.validate(payload, grounding), grounding)
        retrieval = {"query": "가격 계산 오류 원인", "constraints": {},
                     "query_plan": {"source": "mantis", "operation": "search"},
                     "stages": [{"source": "mantis_id_lookup", "status": "ok", "issues_list": [row]}]}
        report = build_verification_report("가격 계산 오류 원인", content,
                                           build_sources_from_issue_rows([row]), retrieval, "model")
        return {check["id"]: check for check in report["checks"]}

    def test_rendered_note_number_is_not_an_unsupported_issue_in_final_verification(self):
        grounding = copy.deepcopy(self.grounding)
        grounding["issues"][0]["answer_evidence"]["excerpts"][0]["note_id"] = 67890
        checks = self.verification_checks(grounding, self.payload())
        self.assertEqual(checks["issue_ids"]["status"], "pass", checks["issue_ids"])

    def test_rendered_title_separator_does_not_become_a_fabricated_title(self):
        checks = self.verification_checks(self.grounding, self.payload())
        self.assertEqual(checks["issue_fields"]["status"], "pass", checks["issue_fields"])

    def test_same_field_groups_metadata_once_and_deduplicates_original_paragraphs(self):
        grounding = copy.deepcopy(self.grounding)
        grounding["issues"][0]["answer_evidence"]["excerpts"][0]["text"] = (
            "개발 완료 여부 확인 요청.\n\n개발 완료는 아니며 반영 예정입니다.")
        payload = self.payload()
        payload["sections"][0]["evidence"].append(
            {"issue_id": 101, "field": "note:7", "quote": "반영 예정"})
        payload["sections"][0]["evidence"].append(
            {"issue_id": 101, "field": "description", "quote": "FID"})
        rendered = self.render(self.validate(payload, grounding), grounding)
        self.assertIn("**#101**", rendered)
        self.assertIn("노트 7", rendered)
        self.assertIn("기록 시각:", rendered)
        self.assertIn("작성자:", rendered)
        self.assertEqual(rendered.count("> 개발 완료는 아니며 반영 예정입니다."), 1)
        self.assertIn("개발 완료 여부 확인 요청.", rendered)
        self.assertIn("FID = 1048;", rendered)

    def test_timeline_orders_source_dates_then_numeric_note_ids(self):
        grounding = copy.deepcopy(self.grounding)
        excerpts = grounding["issues"][0]["answer_evidence"]["excerpts"]
        excerpts[:] = [
            {"field": "note:10", "note_id": 10, "created_at": "2026-10-02", "text": "후속 확인"},
            {"field": "note:9", "note_id": 9, "created_at": "2026-10-02", "text": "수정 전달"},
            {"field": "note:30", "note_id": 30, "created_at": "2026-10-01", "text": "최초 요청"},
        ]
        payload = {"selected_issue_ids": [101], "sections": [{"topic": "timeline", "unanswered": False,
                   "evidence": [{"issue_id": 101, "field": item["field"], "quote": item["text"]}
                                for item in excerpts]}]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        history = rendered.split('### 진행 이력', 1)[1]
        self.assertLess(history.index("최초 요청"), history.index("수정 전달"))
        self.assertLess(history.index("노트 9"), history.index("노트 10"))
        self.assertEqual(history.count("**#101**"), 1)

    def test_timeline_only_answer_previews_latest_qc_and_delivery_with_sources(self):
        grounding = copy.deepcopy(self.grounding)
        excerpts = grounding['issues'][0]['answer_evidence']['excerpts']
        excerpts[:] = [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-06-25',
             'text': '[QC 완료]\n검증본 확인'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-06-26',
             'text': '[고객사 전달]\n수정본 전달'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-09-21',
             'text': '예상가가 다시 나오지 않아 확인을 요청했습니다.'},
        ]
        payload = {"selected_issue_ids": [101], "sections": [
            {"topic": "timeline", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": row["field"], "quote": row["text"]}
                          for row in excerpts]},
        ]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        core = rendered.split('### 진행 이력', 1)[0]
        for expected in ('예상가가 다시 나오지 않아', '[QC 완료]', '[고객사 전달]',
                         '노트 10', '노트 11', '노트 12'):
            self.assertIn(expected, core)
        self.assertLess(core.index('예상가가 다시 나오지 않아'), core.index('각 인용에 적힌 범위'))

    def test_fragmented_contrast_is_previewed_as_one_short_verified_note(self):
        grounding = copy.deepcopy(self.grounding)
        source = ('코드 확인 결과\n\nA는 신고가이어야 하지만\n\n'
                  'A가 신저가로 되어 있습니다.\n\n서버 확인 부탁드립니다.')
        grounding['issues'][0]['answer_evidence']['excerpts'].append({
            'field': 'note:8', 'note_id': 8, 'created_at': '2026-10-02', 'text': source,
        })
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'cause', 'unanswered': False, 'evidence': [
                {'issue_id': 101, 'field': 'note:8', 'quote': '코드 확인 결과'},
                {'issue_id': 101, 'field': 'note:8', 'quote': 'A가 신저가'},
            ],
        }]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        core = rendered.split('### 원인', 1)[0]
        self.assertIn(source.replace('\n', '\n> '), core)
        self.assertEqual(rendered.count('> A가 신저가로 되어 있습니다.'), 1)

    def test_preview_prioritizes_specific_field_plan_with_qc_and_delivery(self):
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:8', 'note_id': 8, 'created_at': '2026-10-02',
             'text': '거래소 구분자(568)에 매매구분을 추가해서 보내려고 합니다.'},
            {'field': 'note:9', 'note_id': 9, 'created_at': '2026-10-03', 'text': '[QC 완료] 검증본 확인'},
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-10-04', 'text': '[전달] 수정본 전달'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'note:8', 'quote': '거래소 구분자(568)'}]},
            {'topic': 'timeline', 'unanswered': False, 'evidence': [
                {'issue_id': 101, 'field': 'note:9', 'quote': '[QC 완료]'},
                {'issue_id': 101, 'field': 'note:10', 'quote': '[전달]'},
            ]},
        ]}
        core = self.render(self.validate(payload, grounding), grounding).split('### 조치', 1)[0]
        for expected in ('구분자(568)', '[QC 완료]', '[전달]'):
            self.assertIn(expected, core)

    def test_conclusion_orders_initial_uncertainty_reproduction_and_planned_qc(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (10, '2025-09-11', '보내주신 xml 파일에 문제가 있는 것으로 보입니다.\n재현되지 않았습니다.'),
                (11, '2025-09-22', '동일하게 현상 재현되는 것 확인됨'),
                (12, '2025-09-24', 'TitleName이 신고가이어야 하지만 신저가로 되어 있습니다.'),
                (13, '2025-09-30', 'tag파일 수정'),
                (14, '2025-10-10', '금주 작업 예정\nQC 10월 3주차 진행 예정'),
            ]
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        conclusion = rendered.split('### 핵심 근거', 1)[0]
        for expected in ('### 핵심 결론', 'xml 파일', '재현되지 않았', '후속 테스트',
                         'TitleName', '태그 파일 수정', '작업과 QC', '노트 14'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('재현되지 않았'), conclusion.index('후속 테스트'))
        self.assertNotIn('운영 반영을 완료했습니다', conclusion)

    def test_minute_bar_condition_and_code_sort_are_summarized(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-09-21',
             'text': '해당 조건은 당일 내의 분봉들을 서로 비교합니다. '
                     '8060원이었던 동양우 종목이 9060원이 되며 상승률 5.104%를 달성했습니다.'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-09-22',
             'text': '최근 봉의 조건 만족 시간으로 변경됩니다. '
                     '해당 조건의 조건 검색 결과 창은 종목 코드를 기반으로 정렬을 하고 있기 때문입니다.'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertIn('분봉을 전봉과 비교', conclusion)
        self.assertIn('5.104%', conclusion)
        self.assertIn('종목 코드순', conclusion)
        self.assertIn('맨 위로 이동하지', conclusion)

    def test_crash_fix_summary_separates_three_changes_from_later_makezero_request(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:9', 'note_id': 9, 'created_at': '2024-02-27',
             'text': '해당 문의는 담당자에게 전달되었습니다. 검토 후 회신 드리겠습니다.'},
            {'field': 'note:10', 'note_id': 10, 'created_at': '2024-03-06', 'text':
             '안녕하세요. 개발 담당자입니다.\nCrash 오류 3건 수정하여 안드로이드 차트 라이브러리 전달 드립니다.\n\n'
             '1. JipyoControlSetUI.java - i_cnt NullPointerException 처리\n'
             'if(i_cnt != null) nContCnt = i_cnt.length;\n\n'
             '2. AutoTrendView.java - 전봉 후봉 입력란에 숫자만 들어가도록 수정하였습니다(NumberFormatException)\n'
             'preNameField.setInputType(InputType.TYPE_CLASS_NUMBER);\n\n'
             '3. NeoChart2.java - block count 경계 오류 수정(ArrayIndexOutOfBoundsException)\n'
             'if (blocks.size() <= m_nMaxIndicatorCount) return;'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2024-03-11', 'text':
             'COMUtil 클래스 내 makeZero 함수에 null 예외처리가 없어 crash가 종료되는 것으로 보입니다.\n'
             'null 예외처리 추가 요청드립니다.'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2024-03-22', 'text':
             '3/11 메일 전달\n1월에 이 부분 수정하여 라이브러리(aar 파일) 전달 드렸을텐데... 혹시 소스로 관리하시요?\n'
             '혹시 언제 받은 라이브러리로 이 크래시 발견되었는지... 지금도 계속 발생하는지 알 수 있을까요?\n'
             '현재 저희가 가진 소스 1826 라인에 str.length() 부분은 널체크 추가되어 있습니다.'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        conclusion = rendered.split('### 핵심 근거', 1)[0]
        for expected in ('Crash 3건', 'JipyoControlSetUI', 'i_cnt', 'AutoTrendView', '숫자',
                         'NeoChart2', '&lt;', '&lt;=', 'makeZero', '1826행', '지금도 재현',
                         '노트 10', '노트 11', '노트 12'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('1월 전달을 완료', conclusion)
        self.assertNotIn('새 패치를 완료', conclusion)
        self.assertLess(len(conclusion), 900)
        self.assertIn('if (blocks.size() <= m_nMaxIndicatorCount)', rendered)
        delivery = rendered.split('**전달 기록**', 1)[1].split('완료·QC·전달', 1)[0]
        self.assertIn('노트 10', delivery)
        self.assertNotIn('노트 9', delivery)

    def test_conclusion_preserves_normal_check_before_later_requery(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (20, '2026-06-25', '[QC 완료]'),
                (21, '2026-06-25', '[고객사 전달]'),
                (22, '2026-07-27', '운영 HTS에 전달드린 모듈 파일 덮어쓰고\n'
                                     'QC 담당자와 선물 예상가 정상 표시되는 것 확인하였습니다.'),
                (23, '2026-09-18', '테스트시 계속 표시가 안되는걸 확인했습니다.'),
                (24, '2026-09-21', '예상가가 안나온다고 함.\n'
                                     '소스 및 모듈 다시 전달해주면 확인해본다고 함.'),
            ]
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        conclusion = rendered.split('### 핵심 근거')[0]
        for expected in ('QC 완료', '고객사 전달', '정상 표시', '다시 제기', '재확인',
                         '노트 22', '노트 23', '노트 24'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('정상 표시'), conclusion.index('다시 제기'))

    def test_conclusion_keeps_planned_delivery_customer_requery_and_single_file_rollback(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (10, '2024-12-18', '차트 로드한 직후 차트 OCX에 그려져 있는 추세선 값들을\n'
                                      '추세선공유 메모리에 copy 해 줘야 하는데 해당부분이 누락되어 있음.'),
                (11, '2024-12-18', '수정 작업 및 QC진행 후 12월 27일에 전달드릴 예정입니다.'),
                (12, '2024-12-23', '[QC 완료]'),
                (13, '2025-01-24', '[QC 완료]'),
                (14, '2025-01-24', '[전달]'),
                (15, '2025-02-04', '메일 수신 From. 김민도\n\n'
                                      '전달본이 정상적으로 동작하지 않습니다.'),
                (16, '2025-02-05', '김현동 매니저님 요청사항으로 롤백 소스 1개 파일 포함하여 다시 보내드립니다.\n'
                                      '- /src/LineShareHelper.cpp\n'
                                      '그 외에는 직전 파일과 동일합니다.'),
                (17, '2025-03-13', '소스 동기화 과정에서 revision 2043 롤백처리'),
                (18, '2025-04-02', '비정상종료건으로 재접수된 내용 수정 후 발송\n[고객사전달]'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('공유 메모리에 복사', '12월 27일 전달은', '예정', '앞선 QC 완료',
                         'QC 완료', '김현동 요청으로',
                         '파일 전달', '정상 동작하지 않는다고 재문의', 'LineShareHelper.cpp 한 파일',
                         '나머지는 직전 파일과 같았', 'revision 2043', '재접수된 내용을 수정해 발송'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('12월 27일 전달은'), conclusion.index('고객이 전달본'))

    def test_conclusion_distinguishes_monitoring_from_patch_and_customer_sender_from_reporter(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'reporter': reporter, 'text': text}
            for note, date, reporter, text in [
                (20, '2025-11-27', '강수현', '블록시세 처리 되어 있음.'),
                (21, '2025-12-03', '김진성', '2000 화면과 1틱 차트로 모니터링했지만\n'
                                              '부하현상은 발생하지 않았습니다.\n'
                                              '추가 정보와 유저 파일을 요청드립니다.'),
                (22, '2025-12-23T13:55:08', '강수현', '2차 모니터링에서는 화면 수를 확대해 점검했고\n'
                                                    '타 사이트와 비교 테스트 결과 특이사항은 확인되지 않았습니다.\n\n'
                                                    '추가로 점검이 필요한 사항이 없으시다면\n'
                                                    '모니터링을 마무리하고자 합니다.'),
                (23, '2025-12-23T14:49:55', '강수현', '2025.12.23 메일수신(From.박성민)\n\n'
                                                    '타 증권사와 비교 테스트 한 결과\n'
                                                    '특이사항 없는걸로 알고 있겠습니다.'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('블록시세 처리는', '부하를 재현하지 못해', '2차 모니터링',
                         '모니터링을 마무리하겠다고 제안', '노트 등록자는 강수현',
                         '메일 발신자 박성민', '원인 확정이나 수정 패치 완료 근거를 확인할 수 없습니다'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('마무리하겠다고 제안'), conclusion.index('고객 메일 발신자'))

    def test_conclusion_separates_server_field_screen_plan_qc_and_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (70, '2025-09-30T08:35:38', '개발서버에 서버 개발 완료\n\n'
                                               'msv001 TR의 아웃풋 ETN 구분자 바로 뒤의 1바이트를 '
                                               '상장형수익증권 구분자로 처리\n\n'
                                               'tobe\nchar sEtn[1];\nchar sPF[1]; /*상장형수익증권 */'),
                (71, '2025-09-30T09:56:19', '서버 개발 완료\n화면 개발 예정/ QC 예정\n'
                                               '화면/QC 일정 확인 중'),
                (72, '2025-10-14', '[QC 요청]'),
                (73, '2025-10-15', '[QC 완료]'),
                (74, '2025-10-16T08:45:42', '[전달 완료]'),
                (75, '2025-10-16T08:48:44', '20251016 mail to 정현선 by 정희진\n\n'
                                               '관련 수정 내역 입니다. 확인 부탁 드립니다.'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'action', 'unanswered': False,
            'evidence': [{'issue_id': 101, 'field': 'note:70', 'quote': 'sPF[1]'},
                         {'issue_id': 101, 'field': 'note:75', 'quote': '관련 수정 내역'}],
        }, {'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101])}]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        conclusion = rendered.split('### 핵심 근거')[0]
        for expected in ('개발서버의 서버 개발 완료', 'ETN 구분자 바로 뒤', '1바이트',
                         'sPF[1]', '화면 개발과 QC는 당시 예정', 'QC 요청', 'QC 완료',
                         '전달 완료', '수정 내역 안내 메일', '운영 반영 완료는 확정할 수 없습니다'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('화면 개발과 QC는 당시 예정'), conclusion.index('QC 완료'))
        self.assertNotIn('조치 근거에는', conclusion)
        core = rendered.split('### 조치', 1)[0]
        self.assertIn('**전달 기록**', core)
        self.assertIn('노트 74', core)

    def test_conclusion_corrects_initial_intraday_non_display_with_prior_day_bars(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['id'] = 30528
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (80, '2026-01-27T13:50:59', 'HTS 분봉에서도 DeMark 지표 표시되는 것 확인됨.'),
                (81, '2026-01-27T13:51:21', '1,2 분봉의 경우는 전일 데이터가 다 조회되어도 '
                                               '지표가 표시되지 않아 확인 필요함'),
                (82, '2026-01-27T16:22:17', '추가 확인 결과 1,2분 주기에서도 DeMark 지표 '
                                               '정상 조회되는 것 확인되었습니다.\n'
                                               '조회개수를 1분봉에서는 1159개, 5분봉에서는 235개 이상 '
                                               '조회 시 지표 표시됩니다.'),
                (83, '2026-01-28T13:26:33', '3. 0030528: 분봉 DeMark 표시 문의\n\n'
                                               '값을 보려는 일자 기준 전일의 데이터가 모두 '
                                               '차트상에서 조회된 상태일 경우만 표시됩니다.\n'
                                               '예. 1월 27일 값을 보려면 1월 26일의 전체 봉이 필요합니다.'),
            ]
        ]}
        payload = {'selected_issue_ids': [30528], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [30528]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('HTS에서도 DeMark', '초기에는 1·2분봉', '후속 확인에서는 1·2분봉도 정상',
                         '전일 전체 봉', '1분봉 1159개', '5분봉 235개', '조회 예시'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('초기에는 1·2분봉'), conclusion.index('후속 확인에서는 1·2분봉'))
        self.assertNotIn('수정 패치', conclusion)

    def test_conclusion_separates_unlimited_trial_recurrence_market_test_and_limited_fallback(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (90, '2025-02-26', '차트 축소 및 봉개수 2000개 이상 조회시 앱 크래시 오류\n'
                                        '-> Android/IOS 둘 다 2000개로 제한하여 모듈 전달드립니다.'),
                (91, '2025-02-27T08:03', '안드로이드는 오류가 발생하지 않는 상황이라 '
                                               '봉 2000개 제한없이 그대로 유지 부탁드립니다.'),
                (92, '2025-02-27T09:33', '일전에 crash가 발생하는 함수를 제거하고 다른 방식으로 수정했습니다.\n'
                                               '제 폰에서는 crash가 발생하지 않아 테스트 부탁드립니다.\n'
                                               '봉 개수는 제한을 두지 않았습니다.'),
                (93, '2025-02-27T16:09', '봉 2000개는 정상적으로 동작하고 있고 '
                                               '2025/02/27 적용을 하면 해당 오류가 발생하고 있습니다.\n'
                                               '주봉은 문제가 안되고 일봉만 문제인것 같습니다.'),
                (94, '2025-02-27T16:16', '자체 빌드 버전에서는 2000개를 초과해도 문제없이 조회되나,\n'
                                               '마켓 버전에서는 2250개 이상 조회 시 크래시가 발생합니다.\n'
                                               '크래시 리포트 관련 로그 확인 부탁드립니다.\n'
                                               '그 전까지는 이전 모듈(봉 조회 개수 2000개 제한) 사용 부탁드립니다.'),
                (95, '2025-02-28', '현재 2000개 봉제한은 정상적으로 동작하고 있어 선적용해두고, '
                                        '로그 추가 첨부합니다.'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('Android/iOS 모두 2000개 제한', 'Android에서는 오류가 없어',
                         '무제한 대안 모듈', '2월 27일 적용본', '주봉은 괜찮',
                         '자체 빌드', '마켓 버전', '2250개 이상', '로그',
                         '이전 2000개 제한', '2000개 제한을 선적용', '무제한 조회의 최종 정상화'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('무제한 대안'), conclusion.index('2월 27일 적용본'))

    def test_conclusion_keeps_server_unit_and_chart_digits_separate_after_delivery_failure(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (100, '2025-03-26T13:50', 'Y축 표시값 28.05의 단위가 실제 값 2.805와 다릅니다.\n'
                                               '차트 조회시 서버 제공 봉 데이터 단위 확인이 필요합니다.\n'
                                               '소수점 둘째자리까지만 나오는 부분은 별도로 차트 수정이 필요합니다.'),
                (101, '2025-03-26T17:08', '테스트서버 121번에는 2.805로 내려오도록 fid전문을 '
                                               '수정했지만 차트는 2자리 또는 반올림 표시됩니다.\n'
                                               '국고채 종목코드 NB로 구분할 수 있을 것 같습니다.'),
                (102, '2025-03-27', '종목코드 등을 이용하여 소수점 3자리 표현되게 '
                                        '수정 후 전달드리도록 하겠습니다.'),
                (103, '2025-03-28', '[QC 완료]'),
                (104, '2025-04-04', '[QC 요청]\n다크모드 작업 롤백으로 인한 재확인 필요'),
                (105, '2025-04-08T13:56', '[QC 완료]'),
                (106, '2025-04-08T14:07', '[전달]'),
                (107, '2025-04-16T12:33', 'Y축 소수점은 제대로 표시되는데 '
                                               '종목명이 표시가 안되는 현상이 있습니다.'),
                (108, '2025-04-16T14:56', '현재 운영에 넣고 실행해보니 '
                                               '아예 조회도 안되고 빈화면으로 나옵니다.\n\n'
                                               '연관 모듈이 왠지 다 필요할거 같은데요?'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        conclusion = rendered.split('### 핵심 근거')[0]
        for expected in ('28.05', '2.805', '서버 제공 봉 데이터', '차트 수정',
                         '테스트서버 121', 'NB', '세 자리 표시', '다크모드 작업 롤백',
                         'QC 완료', '파일 전달', '종목명이 표시되지', '운영에서 조회되지 않고 빈 화면',
                         '추정', '해결 완료 근거는'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('파일 전달'), conclusion.index('종목명이 표시되지'))
        core = rendered.split('### 진행 이력')[0]
        self.assertIn('빈화면', core)
        latest = core.split('**마지막 기록에서 확인되는 상태**', 1)[1].split('**QC 확인 기록**', 1)[0]
        self.assertNotIn('연관 모듈', latest)

    def test_conclusion_limits_provider_zero_explanation_to_insurance_liabilities(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:200', 'note_id': 200, 'created_at': '2026-04-16',
             'text': '해당 데이터는 NH투자증권에서 제공해주는 데이터를 사용하고 있습니다.\n\n'
                     '데이터 확인 결과 제공해주는 데이터가 0으로 되어 있습니다.\n\n'
                     '추후 저희 수정이 필요하면 진행하도록 하겠습니다.'},
            {'field': 'note:201', 'note_id': 201, 'created_at': '2026-04-17',
             'text': '유동부채 요약)\n기존에는 제조업 / 기타 / 보험 업종에 대해 유동부채 정보를 '
                     '보냈으나 이번 피딩서버전환(3월말) 제조업 / 기타 업종에 한하여 제공.\n\n'
                     'GAAP 재무데이터의 보험업종은 유동/비유동 구분이 없이 올라와 '
                     '보험업 유동부채 정보는 제공안한다고 합니다.\n\n'
                     '라는 내용을 재무데이터 제공업체인 FN가이드에서 답변을 받았습니다.\n'
                     '조건검색에서도 데이터를 제공해주지 않아 나온 현상으로 추후 정상 데이터를 '
                     '제공해준다면 조건검색에서도 정상 처리될 것으로 판단 됩니다.'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [
            {'topic': 'cause', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'note:200', 'quote': '데이터가 0으로'},
                          {'issue_id': 101, 'field': 'note:201', 'quote': 'GAAP 재무데이터'}]},
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'note:200', 'quote': '수정이 필요하면'},
                          {'issue_id': 101, 'field': 'note:201', 'quote': '정상 데이터를'}]},
            {'topic': 'timeline', 'unanswered': False,
             'evidence': timeline_evidence(grounding, [101])},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('NH가 제공한 데이터', '제공값 자체가 0', '3월 말 피딩서버 전환',
                         '제조업·기타', '보험업 유동부채', '유동/비유동 구분',
                         'FN가이드', '정상 제공되면', '조건부 안내', '수정·배포 완료 근거'):
            self.assertIn(expected, conclusion)
        self.assertNotIn('유동자산 전체가 보험업', conclusion)
        self.assertNotIn('시스템 수정 완료', conclusion)
        self.assertNotIn('원인 근거에는 “라는 내용을', conclusion)
        self.assertLess(conclusion.index('제공값 자체가 0'), conclusion.index('3월 말 피딩서버 전환'))

    def test_conclusion_marks_568_as_plan_then_qc_and_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (30, '2026-08-03', '체결량이 존재하면 실시간 처리를 하도록 동작하고 있습니다.'),
                (31, '2026-08-06', '거래소 구분자(568)에 매매구분을 추가해서 보내려고 합니다.'),
                (32, '2026-08-10', '[QC 완료]'),
                (33, '2026-08-11', '[전달] 수정 파일'),
            ]
        ]
        grounding['issues'][0]['answer_evidence']['excerpts'].append({
            'field': '조치 및 개선',
            'text': '주식 체결 시 실시간의 매매구분 항목을 이용하여 실시간처리 여부를 결정하는 로직 추가',
        })
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }, {'topic': 'action', 'unanswered': False, 'evidence': [{
            'issue_id': 101, 'field': '조치 및 개선', 'quote': '매매구분 항목을 이용하여',
        }]}]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('체결량', '구분자(568)', '계획', 'QC 완료', '파일 전달',
                         '매매구분으로 실시간 처리 여부를 가르는 로직 추가', '필드 조치 및 개선', '운영 반영 완료는'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('체결량'), conclusion.index('구분자(568)'))
        self.assertLess(conclusion.index('구분자(568)'), conclusion.index('로직 추가'))
        self.assertLess(conclusion.index('구분자(568)'), conclusion.index('QC 완료'))

    def test_other_client_nonreproduction_is_not_this_issues_initial_result(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:30', 'note_id': 30, 'created_at': '2026-08-03',
             'text': '장 전 시간외 종가 거래시간에 체결내역 업데이트 현상 확인됨\n'
                     '*미래에셋에서는 재현되지 않음'},
            {'field': 'note:31', 'note_id': 31, 'created_at': '2026-08-10', 'text': '[QC 완료]'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('같은 현상이 재현되지 않았습니다', conclusion)

    def test_negated_qc_and_normal_display_do_not_become_completed_events(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:30', 'note_id': 30, 'created_at': '2026-08-03',
             'text': 'QC 완료는 아닙니다. 정상 표시되지 않는 것을 확인했습니다.'},
            {'field': 'note:31', 'note_id': 31, 'created_at': '2026-08-04',
             'text': '수정 파일은 다음 주 전달 예정입니다.'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('QC 완료 기록', conclusion)
        self.assertNotIn('정상 표시를 확인', conclusion)

    def test_conclusion_distinguishes_finished_work_from_planned_operation_and_later_nonapplication(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (40, '2022-03-15', '봉개수 제한으로 검색 안되는게 맞음.'),
                (41, '2022-03-16', '금일 서버담당자가 작업 진행하여 완료 했으며\n\n'
                                     '17일 오전 8시 운영 적용 예정입니다.'),
                (42, '2022-04-07', '당사에서 수정 파일 전달하였으나\n'
                                     '귀사 측 담당자가 아직 HTS 운영 버전에 반영하지 않은 건으로 확인되었습니다.'),
            ]
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('봉 개수 제한', '서버 작업 완료', '운영 적용은 당시 예정',
                         '운영 버전에는 아직 반영되지 않았', '노트 42'):
            self.assertIn(expected, conclusion)
        self.assertLess(conclusion.index('서버 작업 완료'), conclusion.index('운영 버전에는 아직 반영되지 않았'))

    def test_general_cause_action_and_latest_fallback_uses_full_verified_paragraphs(self):
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:50', 'note_id': 50, 'created_at': '2026-08-01',
             'text': '원인은 초기화 순서의 충돌입니다.'},
            {'field': 'note:51', 'note_id': 51, 'created_at': '2026-08-02',
             'text': '복원 함수 호출 순서를 바꾸었습니다.'},
            {'field': 'note:52', 'note_id': 52, 'created_at': '2026-08-03',
             'text': '마지막 확인에서 수정 파일을 다시 검토했습니다.'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [
            {'topic': 'cause', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'note:50', 'quote': '원인은'}]},
            {'topic': 'action', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'note:51', 'quote': '호출 순서'}]},
            {'topic': 'timeline', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'note:52', 'quote': '마지막 확인'}]},
        ]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('### 핵심 결론', '원인은 초기화 순서의 충돌입니다',
                         '복원 함수 호출 순서를 바꾸었습니다', '마지막 확인에서 수정 파일을 다시 검토했습니다',
                         '노트 50', '노트 51', '노트 52'):
            self.assertIn(expected, conclusion)

    def test_negated_operational_completion_keeps_scope_limit(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'] = [
            {'field': 'note:60', 'note_id': 60, 'created_at': '2026-08-01',
             'text': '[QC 완료]\n운영 반영 완료 아님'},
        ]
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        conclusion = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertIn('운영 반영 완료는 확정할 수 없습니다', conclusion)

    def test_key_evidence_precedes_collapsed_detail_without_losing_later_facts(self):
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'].extend([
            {'field': 'note:8', 'note_id': 8, 'created_at': '2026-10-02',
             'text': '후속 검토에서 수정 일정은 미정입니다.'},
            {'field': 'note:9', 'note_id': 9, 'created_at': '2026-10-03',
             'text': '최종 확인에서도 운영 반영은 보류 중입니다.'},
        ])
        payload = self.payload()
        payload['sections'][0]['evidence'].extend([
            {'issue_id': 101, 'field': 'note:8', 'quote': '수정 일정'},
            {'issue_id': 101, 'field': 'note:9', 'quote': '운영 반영'},
        ])
        rendered = self.render(self.validate(payload, grounding), grounding)
        self.assertIn('### 핵심 근거', rendered)
        self.assertIn('### 조치', rendered)
        self.assertIn('### 조치 전체 근거', rendered)
        preview = rendered.split('### 조치 전체 근거')[0]
        self.assertIn('운영 반영은 보류', preview)
        self.assertIn('노트 9', preview)
        self.assertNotIn('수정 일정은 미정', preview)
        self.assertNotIn('개발 완료는 아니며 반영을 요청했습니다', preview)
        self.assertIn('개발 완료는 아니며 반영을 요청했습니다', rendered)
        self.assertIn('초기 기록 · 노트 7', rendered)
        self.assertNotIn('초기 기록 · 노트 9', preview)
        self.assertIn('수정 일정은 미정', rendered)
        self.assertIn('운영 반영은 보류', rendered)
        self.assertIn('**#101**', rendered.split('### 조치 전체 근거')[1])

    def test_repeated_note_paragraph_is_cited_once_and_history_keeps_its_place(self):
        payload = {"selected_issue_ids": [101], "sections": [
            {"topic": "timeline", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:7", "quote": "개발 완료"}]},
            {"topic": "action", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:7", "quote": "반영을 요청"}]},
        ]}
        rendered = self.render(self.validate(payload, self.grounding), self.grounding)
        self.assertIn('**마지막 기록에서 확인되는 상태**', rendered)
        self.assertLess(rendered.index('### 조치'), rendered.index('### 진행 이력'))
        self.assertEqual(rendered.count('> 개발 완료는 아니며 반영을 요청했습니다.'), 1)
        self.assertIn('노트 7', rendered[rendered.index('### 진행 이력'):])
        self.assertIn('위에서 인용한 원문', rendered[rendered.index('### 진행 이력'):])

    def test_latest_note_preview_prefers_status_over_greeting_and_mail_header(self):
        grounding = copy.deepcopy(self.grounding)
        paragraphs = [
            '2022.04.07 to 담당자',
            '수정 파일은 전달했으나 HTS 운영 버전에는 아직 반영하지 않았습니다.',
            '안녕하십니까,\n한국금융IT 컨텐츠 서비스팀 담당자입니다.',
        ]
        grounding['issues'][0]['answer_evidence']['excerpts'].append({
            'field': 'note:8', 'note_id': 8, 'created_at': '2026-10-02',
            'text': '\n\n'.join(paragraphs),
        })
        payload = {"selected_issue_ids": [101], "sections": [
            {"topic": "timeline", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:8", "quote": text} for text in paragraphs]},
            {"topic": "action", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:7", "quote": "반영을 요청"}]},
        ]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        latest = rendered.split('**마지막 기록에서 확인되는 상태**', 1)[1].split('### 조치', 1)[0]
        self.assertIn('아직 반영하지 않았습니다', latest)
        self.assertIn('노트 8', latest)
        self.assertNotIn('안녕하십니까', latest)
        self.assertNotIn('2022.04.07 to 담당자', latest)
        self.assertIn('안녕하십니까', rendered)

    def test_core_shows_verified_status_and_action_before_scope_notice(self):
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'].append({
            'field': 'note:8', 'note_id': 8, 'created_at': '2026-10-02',
            'text': '수정 파일은 전달했으나 운영 반영은 아직 안 됐습니다.',
        })
        payload = {"selected_issue_ids": [101], "sections": [
            {"topic": "action", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:7", "quote": "반영을 요청"}]},
            {"topic": "timeline", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:8", "quote": "아직 안 됐습니다"}]},
        ]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        core = rendered.split('### 조치', 1)[0]
        self.assertIn('### 핵심 근거', core)
        self.assertIn('수정 파일은 전달했으나 운영 반영은 아직 안 됐습니다.', core)
        self.assertIn('개발 완료는 아니며 반영을 요청했습니다.', core)
        self.assertIn('노트 8', core)
        self.assertIn('노트 7', core)
        self.assertLess(core.index('수정 파일은 전달'), core.index('각 인용에 적힌 범위'))

    def test_core_skips_headings_and_long_context_without_cutting_the_source(self):
        grounding = copy.deepcopy(self.grounding)
        long_context = '수정 완료' + ' 상세 검토' * 140 + '라는 뜻은 아닙니다.'
        excerpts = grounding['issues'][0]['answer_evidence']['excerpts']
        excerpts.extend([
            {'field': 'note:8', 'note_id': 8, 'created_at': '2026-10-02',
             'text': '2. 충돌 원인\n\n초기화가 끝나기 전에 화면 복원 함수가 호출되어 오류가 발생했습니다.'},
            {'field': 'note:9', 'note_id': 9, 'created_at': '2026-10-03',
             'text': long_context + '\n\n수정 파일을 전달했으나 운영 반영은 확인되지 않았습니다.'},
        ])
        payload = {"selected_issue_ids": [101], "sections": [
            {"topic": "cause", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:8", "quote": "충돌 원인"},
                          {"issue_id": 101, "field": "note:8", "quote": "초기화가 끝나기 전에"}]},
            {"topic": "action", "unanswered": False,
             "evidence": [{"issue_id": 101, "field": "note:9", "quote": "수정 완료"},
                          {"issue_id": 101, "field": "note:9", "quote": "수정 파일을 전달"}]},
        ]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        core = rendered.split('### 원인', 1)[0]
        self.assertIn('초기화가 끝나기 전에 화면 복원 함수가 호출되어 오류가 발생했습니다.', core)
        self.assertIn('수정 파일을 전달했으나 운영 반영은 확인되지 않았습니다.', core)
        self.assertNotIn('2. 충돌 원인', core)
        self.assertNotIn(long_context, core)
        self.assertIn(long_context, rendered)

    def test_long_note_reference_is_quoted_source_not_a_new_issue_claim(self):
        from backend.evaluation.verification import build_verification_report
        text = '후속 작업 #99999에 포함하여 전달할 예정입니다.'
        source = {'type': 'mantis', 'issue_id': 101, 'detail': {'answer_evidence': {
            'excerpts': [{'field': 'note:7', 'text': text}]}}}
        report = build_verification_report('원인 설명', f'**#101**\n> {text}', [source], None, 'model')
        check = next(c for c in report['checks'] if c['id'] == 'issue_ids')
        self.assertEqual(check['status'], 'pass', check)

    def test_compile_redelivery_summary_keeps_complaint_and_unverified_result(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (10, '2025-11-05', '[전달 완료] first.zip'),
                (11, '2025-11-19', '보내주신 조건검색 소스 컴파일시, 추가된 항목 정의가 누락되어서 '
                 '컴파일 오류가 발생합니다. Condition_MngDef.h 파일이 누락된 것 같은데, '
                 '반영되어야 할 파일들을 확인하여 전송 부탁드립니다.'),
                (12, '2025-11-19', '개발 담당자가 금일 휴가 중인 관계로 내일 복귀 예정입니다. '
                 '복귀하는대로 해당 내용 전달하여 조치해놓겠습니다.'),
                (13, '2025-11-20', '[재전달] 한국금융IT_2025_1104화B_신한.zip'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        rendered = self.render(self.validate(payload, grounding), grounding)
        top = rendered.split('### 핵심 근거')[0]
        for expected in ('2025-11-05', 'Condition_MngDef.h', '컴파일 오류', '복귀', 'B 패키지',
                         '재컴파일 성공', '노트 11', '노트 13'):
            self.assertIn(expected, top)
        self.assertLess(top.index('Condition_MngDef.h'), top.index('B 패키지'))
        grounding['issues'][0]['answer_evidence']['complete'] = False
        incomplete = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('재컴파일 성공', incomplete)

    def test_rsi_option_qc_ng_and_rollback_summary_does_not_claim_escape_fix(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (20, '2025-08-11', '구성설정 그룹 영역에 공간이 부족하여 변수설정 그룹에 '
                 '옵션을 추가해도 괜찮을지 문의드립니다.'),
                (21, '2025-08-12', '리스트박스 크기를 조정하여 체크박스 추가 해주시는 방법으로 요청드립니다.'),
                (22, '2025-08-12', '[QC NG] 환경설정 화면 오픈 후 [esc] 입력하여 '
                 '화면 닫기 시 HTS 비정상 종료 현상 확인'),
                (23, '2025-08-18', '[QC NG] RSI 지표값표시 체크 후 [esc] 입력하여 '
                 '화면 닫기 시 HTS 비정상 종료 현상 확인'),
                (24, '2025-08-19', '[QC 완료]'),
                (25, '2025-08-19', '위의 NG 현상은 기존 오류로 프로젝트 소스에서 수정되어있는 '
                 '내용으로 대체거래소때 반영 예정으로 기존 오류 추가 수정 부분은 롤백 후 전달\n'
                 '[전달 완료] final.zip'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('변수설정', '리스트박스', '2025-08-12', '2025-08-18',
                         'ESC', '기존 오류', '대체거래소', '롤백', '최종 해결'):
            self.assertIn(expected, top)
        self.assertNotIn('ESC 종료 오류를 수정 완료', top)

    def test_customer_rollback_plan_and_b_package_do_not_imply_success(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': f'note:{note}', 'note_id': note, 'created_at': date, 'text': text}
            for note, date, text in [
                (30, '2023-11-07', '[QC 요청]'),
                (31, '2023-11-09', '[QC 완료]'),
                (32, '2023-11-10', '[전달 완료] A.zip'),
                (33, '2023-11-22', '고객사 롤백 요청으로 데이터파일만 변경하여 전달 예정'),
                (34, '2023-11-22', '[고객사 전달] 한국금융IT_2023_1110금B_유진.zip'),
            ]
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False,
            'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('QC 요청', 'QC 완료', '2023-11-10', '데이터 파일만', '예정',
                         'B 패키지', '고객', '정상 동작'):
            self.assertIn(expected, top)
        self.assertLess(top.index('QC 요청'), top.index('데이터 파일만'))

    def test_numbered_followup_reply_preserves_linked_reproduction_request_only(self):
        from backend.evaluation.explanation_quotes import safe_excerpts
        issue = {'id': 27706, 'answer_evidence': {'excerpts': [
            {'field': 'note:1', 'note_id': 1,
             'text': '4번 항목부터 살펴봤는데 수정처리가 제대로 안된걸로 확인됩니다.'},
            {'field': 'note:2', 'note_id': 2,
             'text': '문의주신 4번 항목은 5대 PC에서 느린 현상이 재현 되지 않고 있습니다. '
                     '추가 정보 요청 드립니다.\n\n0027535: [0000] 화면로드 속도 이슈\n\n'
                     '1. 영상 요청\n2. PC 사양 요청\n3. User 파일 요청'},
            {'field': 'note:3', 'note_id': 3,
             'text': '0029999: [0000] 다른 이슈\n\n수정 완료 후 전달했습니다.'},
        ]}}
        texts = [row['text'] for row in safe_excerpts(issue)]
        self.assertTrue(any('5대 PC' in row and 'User 파일 요청' in row for row in texts))
        self.assertFalse(any('수정 완료 후 전달했습니다' in row for row in texts))
        issue['answer_evidence']['excerpts'][0]['text'] = '관련 없는 고객 문의입니다.'
        self.assertFalse(any(row.get('note_id') == 2 for row in safe_excerpts(issue)))

    def test_merged_source_answer_keeps_post_delivery_failure_and_latest_request(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'description', 'text': '4/17 신종증권 이행일정이 무기한 연기되었다고 합니다.\n\n'
             '해당 건 제외하고 나머지 수정본 취합하여 전달 요청드립니다.'},
            {'field': 'note:20', 'note_id': 20, 'created_at': '2024-04-30',
             'text': '4/18, 4/22, 4/23, /4/24, 4/26 일자로 전달 드렸던 소스 병합하여 다시 전달드립니다. '
                     '4/17 신종증권 메인 헤더 및 라이브러리, 작업 했던 소스 모두 롤백 처리했습니다. '
                     '미래에셋 내부 소스에서도 이전 버전으로 롤백 처리 부탁드립니다.'},
            {'field': 'note:21', 'note_id': 21, 'created_at': '2024-05-02',
             'text': '수정소스 #206에 적용 완료되었습니다. 4번 항목부터 살펴봤는데 '
                     '수정처리가 제대로 안된걸로 확인됩니다. kChartSetupDlgAddin 에러도 발생했습니다.'},
            {'field': 'note:22', 'note_id': 22, 'created_at': '2024-05-02',
             'text': '문의주신 4번 항목에 대해 5대 PC에서 느린 현상이 재현 되지 않고 있습니다. '
                     '추가 정보 요청 드립니다.\n\n0027535: [0000] 화면로드 속도 이슈\n\n'
                     '1. 영상 요청\n2. PC 사양 요청\n3. User 파일 요청'},
            {'field': 'note:23', 'note_id': 23, 'created_at': '2024-05-02',
             'text': '문제가 되는 작업 클린 후 재빌드 요청'},
            {'field': 'note:24', 'note_id': 24, 'created_at': '2024-05-07',
             'text': '툴팁기능 적용 후 장애발생. 우선 이번건은 저희쪽에서 먼저 원복처리했습니다. '
                     '마지막으로 4/29 수정 취합본에 이문수 수정본까지 취합하여 재전달바랍니다.'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101])},
            {'topic': 'background', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'description', 'quote': '4/17 신종증권'}]},
        ]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('4/17', '4/18·22·23·24·26', '고객사 내부 소스 롤백은 요청',
                         '206번 대상', 'kChartSetupDlgAddin', '영상·PC 사양·User 파일',
                         '클린 후 재빌드', '고객사가 먼저 원복', '이후 재취합본'):
            self.assertIn(expected, top)
        self.assertLess(top.index('206번 대상'), top.index('고객사가 먼저 원복'))
        grounding['issues'][0]['answer_evidence']['complete'] = False
        incomplete = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('이후 재취합본', incomplete)

    def test_rollback_redelivery_summary_states_required_source_before_qc(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2023-12-27',
             'text': '소스 동기화용으로 보내준 소스 중에서 런차트/ 메인 헤더 및 라이브러리 빠져있어서 요청. '
                     '소스 동기화 이후에 롤백한 작업 다시 머지 예정'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2024-01-04',
             'text': '헤더 파일뿐 아니라 cpp 파일도 추가로 보내주시기 바랍니다.'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2024-01-23', 'text': '[QC 완료]'},
            {'field': 'note:13', 'note_id': 13, 'created_at': '2024-01-24', 'text': '[고객사전달]'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('즉시 재전달하지 못했습니다', '메인 헤더·라이브러리', 'cpp 파일',
                         'QC 완료', '고객사 전달'):
            self.assertIn(expected, top)
        self.assertLess(top.index('즉시 재전달하지 못했습니다'), top.index('QC 완료'))

    def test_future_qc_after_fix_is_not_reported_as_already_complete(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-01-09',
             'text': '매개변수 입력 오류로 실시간 표시가 되지 않았습니다. 오류 수정 완료하였고, '
                     'QC 테스트 완료 후 차주 내로 전달드리도록 하겠습니다.'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-01-09', 'text': '[QC 요청]'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-01-12', 'text': '[QC 완료]'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        self.assertNotIn('앞선 QC 완료', top)
        self.assertIn('노트 12', top)

    def test_future_span_cause_and_rebuild_qc_are_visible_in_conclusion(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-01-26',
             'text': '일목균형표의 선행스팬 구간보다 미래영역이 더 클 경우 발생하는 문제'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-01-26', 'text': '[QC 완료]'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-01-30', 'text': '[전달]'},
            {'field': 'note:13', 'note_id': 13, 'created_at': '2026-02-13',
             'text': '[QC재요청] 재빌드로 인하여 QC재요청'},
            {'field': 'note:14', 'note_id': 14, 'created_at': '2026-02-13', 'text': '[QC 완료]'},
            {'field': 'note:15', 'note_id': 15, 'created_at': '2026-02-13', 'text': '[고객사 전달] - 재컴파일'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('선행스팬보다 미래여백', '파일 전달', '재빌드 뒤 QC', '고객사 전달'):
            self.assertIn(expected, top)

    def test_qc_ng_and_recheck_remain_visible_before_final_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-03-26',
             'text': '타 증권사에서도 동일하게 발생하고 있는 오류로 확인됨'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-04-10',
             'text': '[QC NG] 2개 이상의 envelope 지표 추가 후 선 삭제 및 설정 시 '
                     '최상단 지표의 환경설정으로 다 적용되는 오류 현상 확인됨'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-04-10', 'text': '[QC 재요청]'},
            {'field': 'note:13', 'note_id': 13, 'created_at': '2026-04-10', 'text': '[QC 완료]'},
            {'field': 'note:14', 'note_id': 14, 'created_at': '2026-04-10', 'text': '[고객사 전달]'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('다른 증권사', '둘 이상', 'QC에서 오류', 'QC를 다시 요청', 'QC 완료', '고객사 전달'):
            self.assertIn(expected, top)

    def test_changed_default_date_request_is_summarized_before_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'description', 'text': '특정일 대비 주가변동율 범위와 순위 조건의 기본값을 '
             '조회 시점의 당일 날짜로 수정 요청드립니다.'},
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-02-24',
             'text': '특정일 선택 가능한 조건 대상으로 수정 요청. 조회 시점의 1영업일 전 날짜가 가능하면 '
                     '우선이고 불가능하다면 하루 전 날짜로 해주세요.'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-02-26',
             'text': '특정일 선택 가능한 조건 대상의 기본값을 조회 시점의 1영업일 전 날짜로 수정 가능하여 '
                     '개발완료 및 QC 테스트 후 전달드리겠습니다.'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-02-26',
             'text': '현재 개발완료 되었고 QC 테스트 중 입니다.'},
            {'field': 'note:13', 'note_id': 13, 'created_at': '2026-02-27', 'text': '[QC 완료]'},
            {'field': 'note:14', 'note_id': 14, 'created_at': '2026-02-27', 'text': '[고객사 전달]'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }, {'topic': 'conditions', 'unanswered': False, 'evidence': [
            {'issue_id': 101, 'field': 'description', 'quote': '조회 시점의 당일 날짜'},
        ]}]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('조회 당일', '1영업일 전', '하루 전', '개발 완료 뒤 QC', '고객사 전달'):
            self.assertIn(expected, top)
        self.assertLess(top.index('조회 당일'), top.index('1영업일 전'))

    def test_missing_studies_and_sleep_hypothesis_are_separate_conclusions(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-03-16',
             'text': '유저 파일은 구차트 컨버트와는 관련이 없는 것으로 판단됩니다. '
                     '그래프 정보는 저장되어 있으나 지표(STUDIES) 정보는 저장되어 있지 않습니다. '
                     '재접속하는 과정에서 지표 정보가 빠졌을 가능성도 의심되지만, '
                     '내부 테스트에서는 정상적으로 저장됐습니다.'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-03-19',
             'text': '고객이 절전 모드를 사용하지 않고 지켜보겠다고 하여 종료 처리하기로 했음. '
                     '동일 현상이 발생할 경우 추가로 확인합니다.'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('지표(STUDIES)', '구차트 전환 때문은 아니', '가설', '정상 저장', '재발하면 추가 확인'):
            self.assertIn(expected, top)
        self.assertNotIn('절전이 확정 원인', top)

    def test_os_integrity_analysis_does_not_claim_patch_completion(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'note:10', 'note_id': 10, 'created_at': '2026-04-22',
             'text': '공급사 장비에서는 재현되지 않았습니다.'},
            {'field': 'note:11', 'note_id': 11, 'created_at': '2026-04-23',
             'text': 'ExStatic.dll 로드 오류 팝업이 확인됐고 PC 재시작 후 SK HTS에서는 다시 재현이 되지 않음.'},
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-04-23',
             'text': '스마트 앱 컨트롤이 디지털 서명이 없는 DLL을 제한하여 OS 수준의 실행 거부로 판단합니다. '
                     'GetLastError Code: 4551, ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION. '
                     '빌드 옵션 적용을 검토 중이고 디지털 서명도 필요하지만 단독으로 처리가 어려운 상황입니다.'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('ExStatic.dll', '재현되지', '시스템 무결성', '4551', '단독 처리는 어렵',
                         '수정·배포 완료 기록은 아닙니다'):
            self.assertIn(expected, top)

    def test_excel_import_summary_separates_scope_delivery_and_remaining_limits(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = copy.deepcopy(self.grounding)
        grounding['issues'][0]['answer_evidence'] = {'complete': True, 'excerpts': [
            {'field': 'description', 'text': '날짜를 1800년도로 수정하고 불러오기할 수 있는지? '
             '거래대금 지표 추가 후 엑셀불러오기했을때 화면에 하얗게 나옵니다.'},
            {'field': 'note:30', 'note_id': 30, 'created_at': '2023-11-17',
             'text': '차트 저장 엑셀파일의 날짜만 1800년대로 수정 후 엑셀 불러오기 실행 시 '
                     '정상적으로 불러오는 것으로 확인됐습니다. 2번 이슈만 수정 진행합니다.'},
            {'field': 'note:31', 'note_id': 31, 'created_at': '2023-11-22',
             'text': "고객 샘플파일로 엑셀불러오기 시 차트와 DateTime 형식이 맞지 않습니다."},
            {'field': 'note:32', 'note_id': 32, 'created_at': '2023-11-28', 'text': '[QC 완료]'},
            {'field': 'note:33', 'note_id': 33, 'created_at': '2023-11-29', 'text': '[고객사 전달]'},
            {'field': 'note:34', 'note_id': 34, 'created_at': '2023-11-29',
             'text': '과거 데이터로 했을 때 x축에 날짜가 안나옵니다. '
                     '거래대금 지표 데이터가 있는 엑셀을 불러오면 그려지지 않습니다.'},
            {'field': 'note:35', 'note_id': 35, 'created_at': '2023-12-04',
             'text': '시간자료형이 1970년1월1일을 기준으로 사용합니다. '
                     '스케일영역 계산 영향도가 높아 수정이 어렵습니다. '
                     '거래대금과 같은 시장지표들은 조회성 데이터로 구조상 조회하여 '
                     '차트에 데이터가 들어가는 구조로 되어 있어서 수정하기 어렵습니다.'},
        ]}
        payload = {'selected_issue_ids': [101], 'sections': [
            {'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101])},
            {'topic': 'background', 'unanswered': False,
             'evidence': [{'issue_id': 101, 'field': 'description', 'quote': '날짜를 1800년도로'}]},
        ]}
        top = self.render(self.validate(payload, grounding), grounding).split('### 핵심 근거')[0]
        for expected in ('최초 문의', '2번 거래대금', 'DateTime', 'QC 완료', '고객사 전달',
                         '전달 뒤 고객', '1970년', '스케일 계산', '조회 데이터'):
            self.assertIn(expected, top)
        self.assertLess(top.index('DateTime'), top.index('고객사 전달'))

    def test_short_conclusions_cover_workaround_dependency_and_test_only_change(self):
        from backend.evaluation.explanation_quotes import _conclusion_lines

        def summary(issue_id, description, note):
            excerpts = [{'field': 'description', 'text': description},
                        {'field': 'note:1', 'note_id': 1, 'created_at': '2026-10-14', 'text': note}]
            rows = [dict(row, issue_id=issue_id) for row in excerpts]
            issue = {'id': issue_id, 'answer_evidence': {'complete': True, 'excerpts': excerpts}}
            plans = [('timeline', rows, [], False), ('action', rows[1:], [], False)]
            return '\n'.join(_conclusion_lines(plans, [issue_id], {issue_id: issue}))

        yaxis = summary(31618, '사용자지표를 가격차트와 동일한 Y축 스케일로 맞추는 방법 문의',
                        '현재 Y축 스케일 선택할 수 있는 설정 화면은 차트 내 지표를 드래그&드랍 형식으로 '
                        '합쳤을 때에만 팝업되고 있으며 다른 방식으로는 제공되지 않습니다. '
                        '지표관리 탭으로 동일 영역에 추가한 경우 지표명을 드래그&드랍하여 나눈 후 '
                        '다시 가격차트로 합쳐서 Y축 스케일을 합칠 수 있습니다.')
        self.assertIn('드래그', yaxis)
        self.assertIn('지표관리 탭', yaxis)
        self.assertIn('다시', yaxis)

        combo = summary(31598, '거래소 버튼을 종목콤보와 분리해 달라는 요청',
                        '현재 종목콤보와 거래소 버튼은 하나의 메인 컨트롤(IJMComboCtl)로 제공됩니다. '
                        '분리하려면 메인에서 별도의 종목 컨트롤 및 거래소 버튼을 제공해야 합니다. '
                        '작업 가능 여부 검토에 앞서 메인 지원 가능 여부가 우선 확인되어야 합니다.')
        self.assertIn('IJMComboCtl', combo)
        self.assertIn('별도 종목 컨트롤', combo)
        self.assertIn('메인의', combo)

        test_server = summary(31620, 'RealOpTuja 프로세스가 내려갔다 다시 올라온다는 문의',
                              '테스트 서버도 운영과 동일한 현상 발생 확인\n'
                              '로그 추가 후 테스트서버에 반영 해놓았음')
        self.assertIn('테스트 서버', test_server)
        self.assertIn('로그', test_server)
        self.assertIn('원인 확정', test_server)

        possible = summary(31582, 'MTS 거래소 선택 신규 기능 추가 가능 여부 검토 요청', '가능합니다.')
        self.assertIn('가능', possible)
        self.assertIn('구현', possible)

    def test_version_plan_summary_keeps_current_request_and_future_distinct(self):
        from backend.evaluation.explanation_quotes import _conclusion_lines
        excerpts = [
            {'field': 'note:1', 'note_id': 1, 'created_at': '2026-10-01',
             'text': '현재 버전이 별도로 관리되고 있지는 않습니다.'},
            {'field': 'note:2', 'note_id': 2, 'created_at': '2026-10-02',
             'text': '전달주실때 아래 폴더 기준에 "[2.0]" 과 같이 버전 붙여서 전달주시면 인지하겠습니다.'},
            {'field': 'note:3', 'note_id': 3, 'created_at': '2026-10-06',
             'text': '추후 라이브러리 전달 드릴 때 버전 넣어서 전달 드리겠습니다.'},
        ]
        rows = [dict(row, issue_id=31567) for row in excerpts]
        issue = {'id': 31567, 'answer_evidence': {'complete': True, 'excerpts': excerpts}}
        top = '\n'.join(_conclusion_lines([('timeline', rows, [], False)], [31567], {31567: issue}))
        for expected in ('별도로 관리', '[2.0]', '추후', '완료'):
            self.assertIn(expected, top)

    def test_feasibility_refusal_summary_has_request_and_one_decision(self):
        from backend.evaluation.explanation_quotes import _conclusion_lines
        excerpts = [
            {'field': 'description',
             'text': '관심그룹을 지정하여 전략을 저장했으나 다시 오픈하면 관심그룹 지정 내역이 '
                     '저장되지 않고 기본값으로 초기화됩니다. 설정값 함께 저장 가능한지 검토 요청'},
            {'field': 'note:1', 'note_id': 1, 'created_at': '2026-10-01',
             'text': '해당 이슈 개선 불가능합니다. 만일 개선을 하더라도 유지보수가 아닌 개발로 '
                     '들어갈 것으로 추측됩니다.'},
        ]
        rows = [dict(row, issue_id=31563) for row in excerpts]
        issue = {'id': 31563, 'answer_evidence': {'complete': True, 'excerpts': excerpts}}
        top = '\n'.join(_conclusion_lines([('timeline', rows, [], False),
                                           ('action', rows[1:], [], False)], [31563], {31563: issue}))
        for expected in ('기본값', '개선 불가능', '유지보수가 아닌 개발', '추측', '구현 완료'):
            self.assertIn(expected, top)
        self.assertEqual(top.count('개선 불가능'), 1)

    def test_customer_reproduction_followup_and_conditional_closure_are_concise(self):
        from backend.evaluation.explanation_quotes import _conclusion_lines
        excerpts = [
            {'field': 'description', 'text': '7111 화면에서 두산 종목명이 보이는데 차트 데이터는 DB하이텍으로 표시됩니다.'},
            {'field': 'note:1', 'note_id': 1, 'created_at': '2026-09-22',
             'text': '제 PC에서도 발생안하고 있습니다. HTS[2000] 관심탭에서 종목 변경 및 그룹이동 후 '
                     '종목 변경 시 발생한다고 답변받았습니다.'},
            {'field': 'note:2', 'note_id': 2, 'created_at': '2026-09-28',
             'text': '현재 해당 이슈가 양사PC에서 재현되지 않고 구체적인 정보가 부족해 우선 종료합니다. '
                     '추후 추가적인 정보가 들어오면 재접수하여 다시 확인하겠습니다.'},
        ]
        rows = [dict(row, issue_id=31521) for row in excerpts]
        issue = {'id': 31521, 'answer_evidence': {'complete': True, 'excerpts': excerpts}}
        top = '\n'.join(_conclusion_lines([('timeline', rows, [], False)], [31521], {31521: issue}))
        for expected in ('두산', 'DB하이텍', '관심탭', '양사', '우선 종료', '재접수'):
            self.assertIn(expected, top)

    def test_typo_request_is_visible_before_qc_history(self):
        from backend.evaluation.explanation_quotes import _conclusion_lines
        excerpt = {'field': 'description',
                   'text': 'OBV 지표 설명의 On Ballance Volume -> On Balance Volume 오타 수정 요청'}
        issue = {'id': 31510, 'answer_evidence': {'complete': True, 'excerpts': [excerpt]}}
        top = '\n'.join(_conclusion_lines([('timeline', [dict(excerpt, issue_id=31510)], [], False)],
                                          [31510], {31510: issue}))
        self.assertIn('On Ballance Volume', top)
        self.assertIn('On Balance Volume', top)

    def test_complete_qc_history_without_delivery_says_delivery_is_unrecorded(self):
        from backend.evaluation.explanation_quotes import _conclusion_lines
        excerpts = [
            {'field': 'note:1', 'note_id': 1, 'created_at': '2026-09-18', 'text': '[QC요청]'},
            {'field': 'note:2', 'note_id': 2, 'created_at': '2026-09-23', 'text': '[QC 완료]'},
        ]
        issue = {'id': 31509, 'answer_evidence': {'complete': True, 'excerpts': excerpts}}
        rows = [dict(row, issue_id=31509) for row in excerpts]
        top = '\n'.join(_conclusion_lines([('timeline', rows, [], False)], [31509], {31509: issue}))
        self.assertIn('QC 완료', top)
        self.assertIn('고객사 전달 완료 기록이 없습니다', top)


if __name__ == "__main__":
    unittest.main()
