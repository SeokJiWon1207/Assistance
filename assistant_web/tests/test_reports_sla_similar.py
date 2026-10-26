"""SLA(처리시간) 리포트와 유사사례 해결책 리포트의 결정형 로직 테스트."""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.reports import similar, sla
from backend.sources.mantis_collection import IssueBatch
from unittest.mock import patch


class SlaReportTest(unittest.TestCase):
    def test_combined_effort_excludes_missing_delivery_and_other_people(self):
        constraints = {'delivered_at': {'from': '2026-10-01', 'to': '2026-10-31'},
                       'effort_date_basis': 'delivered_at', 'filter_clauses': [
                           {'field': 'effort_person', 'operator': 'equals', 'value': '석지원'}]}
        rows = [
            {'id': 1, 'summary': '이전 등록', 'status': 'closed', 'project': '고객A',
             'created_at': '2026-09-01', 'updated_at': '2026-10-01',
             'date_fields': {'고객사전달일': '2026-10-01'}, 'effort_fields': {'3PC공수': '석지원(2.5), 홍길동(9)'}},
            {'id': 2, 'summary': '전달 대기', 'status': 'assigned', 'project': '고객A',
             'created_at': '2026-10-01', 'updated_at': '2026-10-02',
             'date_fields': {}, 'effort_fields': {'3PC공수': '석지원(99)'}},
        ]
        result = sla._build_duration_report('공수와 평균 처리시간', constraints, rows, None)
        self.assertIn('| 2.50 |', result['content'])

    def setUp(self):
        store = patch.object(sla, "read_complete_store", return_value=None)
        store.start()
        self.addCleanup(store.stop)
        self._originals = {
            "fetch": sla.fetch_mantis_recent_issues,
            "normalize": sla.normalize_issue,
            "matches": sla.issue_matches_constraints,
            "constraints": sla.extract_query_constraints,
            "sources": sla.build_sources_from_issue_rows,
        }
        sla.normalize_issue = lambda issue: issue
        sla.issue_matches_constraints = lambda issue, constraints: True
        sla.extract_query_constraints = lambda text: {}
        sla.build_sources_from_issue_rows = lambda rows: [{"id": r.get("id")} for r in rows]

    def tearDown(self):
        sla.fetch_mantis_recent_issues = self._originals["fetch"]
        sla.normalize_issue = self._originals["normalize"]
        sla.issue_matches_constraints = self._originals["matches"]
        sla.extract_query_constraints = self._originals["constraints"]
        sla.build_sources_from_issue_rows = self._originals["sources"]

    def _set_issues(self, issues):
        sla.fetch_mantis_recent_issues = lambda **kwargs: IssueBatch(issues, {"search_complete": True})

    def test_mode_detection_distinguishes_duration_and_aging(self):
        self.assertEqual(sla._sla_mode("평균 처리시간 며칠 걸려?"), "duration")
        self.assertEqual(sla._sla_mode("가장 오래된 미종료 이슈"), "aging")
        # 순수 상태어 건수 질문은 SLA가 가로채면 안 된다.
        self.assertIsNone(sla._sla_mode("미종료 이슈 몇 건이야?"))
        self.assertIsNone(sla._sla_mode("KB증권 이슈 보여줘"))

    def test_duration_report_uses_closed_issues_only(self):
        self._set_issues([
            {"id": 101, "summary": "A", "status": "closed", "project": "유진증권",
             "created_at": "2026-06-01", "updated_at": "2026-06-05"},   # 4일
            {"id": 102, "summary": "B", "status": "closed", "project": "KB증권",
             "created_at": "2026-05-01", "updated_at": "2026-05-21"},   # 20일
            {"id": 103, "summary": "C", "status": "assigned", "project": "KB증권",
             "created_at": "2026-07-01", "updated_at": "2026-07-02"},   # 미종료 제외
        ])
        content = sla.build_sla_report("평균 처리시간 며칠 걸려?")["content"]
        self.assertIn("종료 이슈 2건", content)
        self.assertIn("평균 처리기간: **12.0일**", content)  # (4+20)/2
        self.assertIn("#102", content)  # 최장이 먼저

    def test_aging_report_ranks_open_issues_by_age(self):
        self._set_issues([
            {"id": 201, "summary": "묵은건", "status": "assigned", "project": "유진증권",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
            {"id": 202, "summary": "최근건", "status": "assigned", "project": "KB증권",
             "created_at": (date.today() - timedelta(days=10)).isoformat(),
             "updated_at": date.today().isoformat()},
            {"id": 203, "summary": "종료건", "status": "closed", "project": "KB증권",
             "created_at": "2026-01-01", "updated_at": "2026-01-05"},   # 종료 제외
        ])
        content = sla.build_sla_report("가장 오래 방치된 이슈 알려줘")["content"]
        self.assertIn("미종료 이슈 2건", content)
        self.assertIn("#201", content)
        # 기준 미만의 최근 이슈는 전체 미종료 통계에는 포함되지만 상세 목록에서는 빠진다.
        self.assertNotIn("#202", content)

    def test_aging_answers_from_a_stale_complete_store_with_its_time(self):
        rows = [{"id": 201, "summary": "묵은건", "status": "assigned", "project": "유진증권",
                 "created_at": "2026-01-01", "updated_at": "2026-01-02"}]
        stale = IssueBatch(rows, {"status": "snapshot", "fetchedAt": "2026-10-07T05:35:00+00:00",
                                  "search_complete": False, "snapshot_complete": True})
        sla.fetch_mantis_recent_issues = lambda **kwargs: self.fail("저장본이 있으면 최근 N건 실시간 조회로 세지 않는다")
        with patch.object(sla, "read_complete_store", side_effect=lambda allow_stale=False: stale if allow_stale else None):
            content = sla.build_sla_report("가장 오래 방치된 이슈 알려줘")["content"]
        # 갱신이 1시간 넘게 늦어도 답을 거부하지 않고, 몇 시 기준인지 밝힌다.
        self.assertIn("#201", content)
        self.assertIn("저장본 수집 시각: 10월 7일 오후 2:35 (갱신 지연", content)
        self.assertNotIn("확정할 수 없습니다", content)

    def test_aging_still_refuses_when_only_recent_issues_are_available(self):
        sla.fetch_mantis_recent_issues = lambda **kwargs: IssueBatch([], {"search_complete": False})
        content = sla.build_sla_report("가장 오래 방치된 이슈 알려줘")["content"]
        self.assertIn("확정할 수 없습니다", content)

    def test_aging_uses_requested_threshold_without_false_scope_note(self):
        recent = (date.today().replace(day=1)).isoformat()
        old = date(date.today().year - 1, 1, 1).isoformat()
        self._set_issues([
            {"id": 211, "summary": "오래된 건", "status": "assigned", "project": "A",
             "created_at": old, "updated_at": old},
            {"id": 212, "summary": "최근 건", "status": "assigned", "project": "A",
             "created_at": recent, "updated_at": recent},
        ])
        content = sla.build_sla_report("90일 넘게 미종료된 이슈 보여줘")["content"]
        self.assertIn("90일 이상 경과", content)
        self.assertIn("#211", content)
        self.assertNotIn("#212", content)
        self.assertNotIn("적체 분석에 적용하지 않았습니다", content)

    def test_duration_with_effort_returns_combined_customer_table(self):
        self._set_issues([
            {"id": 221, "summary": "A", "status": "closed", "project": "고객A",
             "created_at": "2026-08-01", "updated_at": "2026-08-05",
             "date_fields": {"고객사전달일": "2026-08-05"},
             "effort_fields": {"3PC공수": "김담당(2.5)"}},
            {"id": 222, "summary": "B", "status": "assigned", "project": "고객A",
             "created_at": "2026-08-02", "updated_at": "2026-08-03",
             "date_fields": {"고객사전달일": "2026-08-03"},
             "effort_fields": {"1문의공수": "김담당(1.0)"}},
        ])
        content = sla.build_sla_report("지난달 고객사별 공수와 평균 처리시간을 표로 보여줘")["content"]
        self.assertIn("고객사별 공수·평균 처리시간", content)
        self.assertIn("| 고객A | 3.50 | 4.0일 | 1건 |", content)

    def test_returns_none_for_non_sla_questions(self):
        self.assertIsNone(sla.build_sla_report("KB증권 HTS 이슈 5개"))

    def test_aging_keeps_explicit_registration_scope_even_when_empty(self):
        constraints = {
            "customer_aliases": ["159eBest"],
            "created_at": {"from": "2026-08-03", "to": "2026-08-09"},
            "filter_clauses": [
                {"field": "customer", "operator": "equals", "value": "eBest"},
                {"field": "created_at", "operator": "between", "value": ["2026-08-03", "2026-08-09"]},
            ],
        }
        sla.extract_query_constraints = lambda text: constraints
        sla.issue_matches_constraints = lambda issue, cc: "created_at" not in cc
        self._set_issues([
            {"id": 301, "summary": "범위 밖", "status": "assigned", "project": "eBest",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
        ])
        report = sla.build_sla_report("이번 주 ebest 장기 적체 알려줘")
        self.assertNotIn("#301", report["content"])
        self.assertIn("조건에 맞는 이슈가 없습니다", report["content"])
        self.assertEqual(report["retrieval"]["constraints"], constraints)

    def test_duration_keeps_created_at_constraint(self):
        sla.extract_query_constraints = lambda text: {"created_at": {"from": "2026-08-03"}}
        sla.issue_matches_constraints = lambda issue, cc: "created_at" in cc
        self._set_issues([
            {"id": 401, "summary": "종료건", "status": "closed", "project": "KB증권",
             "created_at": "2026-08-03", "updated_at": "2026-08-05"},
        ])

        report = sla.build_sla_report("평균 처리시간 며칠 걸려?")
        self.assertIn("#401", report["content"])
        self.assertIn("created_at", report["retrieval"]["constraints"])

    def test_empty_result_names_the_constraint_that_emptied_it(self):
        sla.extract_query_constraints = lambda text: {
            "customer_aliases": ["159eBest"],
            "statuses": ["open"],
        }
        # 고객사만 맞고 상태는 맞지 않는 상황을 만든다.
        sla.issue_matches_constraints = lambda issue, cc: "statuses" not in cc
        self._set_issues([
            {"id": 501, "summary": "A", "status": "closed", "project": "eBest",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
        ])

        report = sla.build_sla_report("ebest 장기 적체 알려줘")
        content = report["content"]
        self.assertIn("조건에 맞는 이슈가 없습니다", content)
        self.assertIn("상태 조건을 빼면 1건", content)
        self.assertEqual(report["retrieval"]["stages"][0]["status"], "ok")
        # 결과를 비우지 않은 조건은 힌트에 나오면 안 된다.
        self.assertNotIn("고객사 조건을 빼면", content)


class SimilarResolutionReportTest(unittest.TestCase):
    def setUp(self):
        self._originals = {
            "constraints": similar.extract_query_constraints,
            "wants": similar.wants_similar_issue_search,
            "fetch": similar.fetch_mantis_issue_by_id,
            "normalize": similar.normalize_issue,
            "search_text": similar.build_similar_issue_search_text,
            "sources": similar.build_sources_from_issue_rows,
            "chroma": similar.execute_chroma_search,
        }
        similar.extract_query_constraints = lambda text: {
            "issue_ids": [12345],
            "filter_clauses": [
                {"field": "issue_id", "operator": "in", "value": [12345]}
            ],
        }
        similar.wants_similar_issue_search = lambda text, constraints: True
        similar.fetch_mantis_issue_by_id = lambda issue_id: {"id": issue_id, "summary": "[1200] 세션 만료"}
        similar.normalize_issue = lambda raw, **kwargs: {"id": raw["id"], "summary": raw["summary"], "description": ""}
        similar.build_similar_issue_search_text = lambda issue: "세션 만료"
        similar.build_sources_from_issue_rows = lambda rows: [{"id": r.get("id")} for r in rows]

    def tearDown(self):
        for name, value in self._originals.items():
            setattr(
                similar,
                {
                    "constraints": "extract_query_constraints",
                    "wants": "wants_similar_issue_search",
                    "fetch": "fetch_mantis_issue_by_id",
                    "normalize": "normalize_issue",
                    "search_text": "build_similar_issue_search_text",
                    "sources": "build_sources_from_issue_rows",
                    "chroma": "execute_chroma_search",
                }[name],
                value,
            )

    def test_wants_resolution_gate(self):
        self.assertTrue(similar._wants_resolution("어떻게 해결했어"))
        self.assertFalse(similar._wants_resolution("비슷한 이슈 목록만"))

    def test_requests_hypotheses_and_negations_are_not_recorded_causes(self):
        for text in (
            "원인이 무엇인지 확인 부탁드립니다.", "원인은 인증서 문제가 아닙니다.",
            "원인은 인증서 문제로 추정됩니다.", "원인 분석 진행 중입니다.",
            "원인 미확인", "인증서 오류 때문인지 확인 요청드립니다.",
            "원인이 세션 만료인가요?", "원인은 인증서 문제인 것 같습니다.",
            "원인을 확인할 수 없습니다.", "원인 조사 중입니다.",
        ):
            with self.subTest(text=text):
                detail = {"notes": [{"text": text}]}
                self.assertEqual(similar._recorded_cause(detail), ("", ""))
                table = '\n'.join(similar._comparison_lines({}, [detail])).split('- 원인 기록:')[0]
                self.assertNotIn(text.rstrip("."), table)

    def test_pending_notes_do_not_replace_completed_fixes_in_playbooks(self):
        for text in (
            "서버 설정 수정 후 다시 확인 부탁드립니다.", "패치 배포 예정입니다.",
            "수정이 완료되지 않았습니다.", "아직 해결하지 못했습니다.", "추가 확인 필요",
            "수정 중입니다.", "패치를 적용하겠습니다.", "패치 미적용",
            "서버 설정을 수정해야 합니다.", "패치 배포 바랍니다.",
            "원인과 수정 방법을 공유해주세요.", "추가 문의가 접수되었습니다.",
            "패치를 적용하면 해결됩니다.", "패치 적용 안 됨.",
        ):
            with self.subTest(text=text):
                detail = {"id": 9001, "notes": [{"text": text}]}
                self.assertEqual(similar._recorded_fix(detail), ("", ""))
                self.assertNotIn("마지막 조치:", similar._summarize_resolution(detail))
                self.assertNotIn(f"마지막 조치: {text}", similar._build_playbook_candidate({}, [detail])["resolution"])
                detail["notes"].insert(0, {"text": "서버 설정 수정 완료."})
                self.assertEqual(similar._recorded_fix(detail), ("서버 설정 수정 완료", "note"))

    def test_completed_action_is_retained_when_the_same_note_contains_a_followup_request(self):
        detail = {"notes": [{"text": "서버 설정 수정 완료. 고객 확인 부탁드립니다."}]}
        self.assertEqual(similar._recorded_fix(detail), ("서버 설정 수정 완료", "note"))
        detail["notes"][0]["text"] = "서버 설정 수정 완료\n고객 확인 부탁드립니다."
        self.assertEqual(similar._recorded_fix(detail), ("서버 설정 수정 완료", "note"))

    def test_request_subjects_and_negative_failure_causes_retain_confirmed_records(self):
        detail = {"notes": [{"text": "추가 요청사항 반영 완료."}]}
        self.assertEqual(similar._recorded_fix(detail), ("추가 요청사항 반영 완료", "note"))
        detail["resolution_fields"] = {"조치 및 개선": "추가 요청사항 반영 완료."}
        self.assertEqual(similar._recorded_fix(detail), ("추가 요청사항 반영 완료.", "field"))
        detail["notes"] = [{"text": "예외를 처리하지 않아 발생한 오류가 원인입니다."}]
        self.assertEqual(similar._recorded_cause(detail), ("예외를 처리하지 않아 발생한 오류가 원인입니다", "note"))
        detail["notes"] = [{"text": "원인은 요청 파라미터 누락입니다."}]
        self.assertEqual(similar._recorded_cause(detail), ("원인은 요청 파라미터 누락입니다", "note"))

    def test_summarize_resolution_pulls_status_and_last_note(self):
        detail = {
            "status": "종료된 이슈",
            "history": [{"created_at": "2026-05-02T11:00:00", "field": "상태",
                         "change": "할당된 이슈 => 종료된 이슈"}],
            "notes": [{"text": "타임아웃을 30분으로 늘려 해결함."}],
        }
        summary = similar._summarize_resolution(detail)
        self.assertIn("종료된 이슈", summary)
        self.assertIn("→", summary)
        self.assertIn("타임아웃", summary)

    def test_report_lists_similar_cases_with_resolution(self):
        captured = {}

        def search(q, limit, cons, semantic=False):
            captured["constraints"] = cons
            return {
                "status": "ok",
                "documents": [
                    {"issue_id": 9001, "latest_issue": {
                        "id": 9001, "summary": "세션 만료 재로그인", "project": "유진증권",
                        "status": "종료된 이슈",
                        "notes": [{"text": "타임아웃 30분으로 조정."}],
                        "history": [{"created_at": "2026-05-02", "field": "상태",
                                     "change": "할당 => 종료"}],
                    }},
                    {"issue_id": 12345, "latest_issue": {"id": 12345, "summary": "자기자신"}},
                ],
            }

        similar.execute_chroma_search = search
        report = similar.build_similar_resolution_report("#12345랑 비슷한 이슈 어떻게 해결했어?")
        content = report["content"]
        self.assertIn("#9001", content)
        self.assertIn("타임아웃", content)
        # 기준 이슈 자기 자신은 유사 목록에서 제외된다.
        self.assertNotIn("자기자신", content)
        candidate = report["playbookCandidate"]
        self.assertIn("세션 만료", candidate["title"])
        self.assertIn("타임아웃", candidate["resolution"])
        self.assertEqual(candidate["sourceIssueIds"], [12345, 9001])
        self.assertEqual(captured["constraints"]["issue_ids"], [])
        self.assertEqual(captured["constraints"]["filter_clauses"], [])

    def test_generic_resolution_words_do_not_filter_similarity_candidates(self):
        captured = {}

        def search(q, limit, cons, semantic=False):
            captured["constraints"] = cons
            return {"status": "ok", "documents": []}

        similar.execute_chroma_search = search
        similar.build_similar_resolution_report(
            "#12345와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘"
        )
        self.assertEqual(captured["constraints"]["semantic_focus_terms"], [])

    def test_report_compares_recorded_causes_and_fixes_side_by_side(self):
        similar.normalize_issue = lambda raw, **kwargs: {
            "id": raw["id"], "summary": "[1200] 세션 만료 현상", "description": "",
            "resolution_fields": {"사유 및 원인": "세션 타임아웃 설정 누락"}, "notes": [],
        }

        def search(q, limit, cons, semantic=False):
            return {"status": "ok", "documents": [
                {"issue_id": 9001, "latest_issue": {
                    "id": 9001, "summary": "세션 끊김", "project": "유진증권", "status": "종료된 이슈",
                    "resolution_fields": {"사유 및 원인": "서버 세션 타임아웃 10분 설정", "조치 및 개선": "타임아웃 30분으로 조정"},
                    "notes": [], "history": [],
                }},
                {"issue_id": 9002, "latest_issue": {
                    "id": 9002, "summary": "재로그인 반복", "project": "KB증권", "status": "종료된 이슈",
                    "resolution_fields": {},
                    "notes": [{"text": "원인 확인 요청드립니다."},
                              {"text": "확인 결과 원인은 인증 토큰 갱신 누락입니다. 패치 배포 완료."}],
                    "history": [],
                }},
                {"issue_id": 9003, "latest_issue": {
                    "id": 9003, "summary": "세션 만료 문의", "project": "NH증권", "status": "종료된 이슈",
                    "resolution_fields": {}, "notes": [{"text": "고객 안내 후 종료"}], "history": [],
                }},
                {"issue_id": 9004, "latest_issue": {
                    "id": 9004, "summary": "세션 조회 불가", "project": "교보증권", "status": "종료된 이슈",
                    "resolution_fields": {},
                    "notes": [
                        {"text": "안녕하세요. 교보증권 유호준 대리입니다. 현재 개발기 디스크문제로 조회가 안되는 상황입니다."},
                        {"text": "안녕하세요. 한국금융아이티 이영희부장입니다. 0009004: 세션 조회 불가 서버 설정 수정해서 소스 전달드립니다."},
                        {"text": "[고객사 전달] z:\\01_버전처리\\2024.zip"},
                    ],
                    "history": [],
                }},
            ]}

        similar.execute_chroma_search = search
        report = similar.build_similar_resolution_report("#12345와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘")
        content = report["content"]
        self.assertIn('현상"과 비슷한 과거 사례의 원인과 그때의 조치', content)
        self.assertIn("| 이슈 | 원인 | 해결·조치 |", content)
        self.assertIn("| 기준 #12345 | 세션 타임아웃 설정 누락 | 기록 없음 |", content)
        self.assertIn("| #9001 | 서버 세션 타임아웃 10분 설정 | 타임아웃 30분으로 조정 |", content)
        # 원인을 묻기만 하는 노트는 건너뛰고, 노트에서 뽑은 원인은 (노트)로 표시한다.
        self.assertIn("| #9002 | 확인 결과 원인은 인증 토큰 갱신 누락입니다 (노트) |", content)
        self.assertIn("| #9003 | 기록에서 확인되지 않음 | 기록에서 확인되지 않음 |", content)
        # 인사말·제목 반복과 파일 경로뿐인 전달 기록은 걷어내고 실제 원인·조치를 보여 준다.
        self.assertIn("| #9004 | 현재 개발기 디스크문제로 조회가 안되는 상황입니다 (노트) | 서버 설정 수정해서 소스 전달드립니다", content)
        self.assertIn("원인 기록: 기준 이슈 있음, 과거 사례 3/4건.", content)
        self.assertIn("#9001: 기준 이슈와 겹치는 원인 단서 설정, 세션, 타임아웃", content)
        self.assertIn("같은 원인이라고 단정하지 않았습니다", content)
        self.assertIn("#12345 세션 타임아웃 설정 누락", report["playbookCandidate"]["cause"])
        self.assertIn("#9001 서버 세션 타임아웃 10분 설정", report["playbookCandidate"]["cause"])

    def test_base_issue_in_the_header_is_marked_for_answer_verification(self):
        from backend.retrieval import similar_base_issue_ids_from_retrieval

        def search(q, limit, cons, semantic=False):
            return {"status": "ok", "documents": [{"issue_id": 9001, "latest_issue": {
                "id": 9001, "summary": "세션 끊김", "project": "유진증권", "status": "종료된 이슈", "notes": [], "history": [],
            }}]}

        similar.execute_chroma_search = search
        report = similar.build_similar_resolution_report("#12345와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘")
        self.assertIn("#12345", report["content"])
        self.assertEqual(similar_base_issue_ids_from_retrieval(report["retrieval"]), [12345])

        similar.execute_chroma_search = lambda q, limit, cons, semantic=False: {"status": "ok", "documents": []}
        empty = similar.build_similar_resolution_report("#12345와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘")
        self.assertEqual(similar_base_issue_ids_from_retrieval(empty["retrieval"]), [12345])

    def test_returns_none_without_resolution_intent(self):
        self.assertIsNone(similar.build_similar_resolution_report("#12345 비슷한 이슈 목록"))


if __name__ == "__main__":
    unittest.main()
