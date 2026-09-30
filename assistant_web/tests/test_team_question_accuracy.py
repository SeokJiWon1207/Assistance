"""팀원 질문의 조건 충족, 오답 차단, 평가기의 거짓 통과를 검증한다."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.evaluation.verification import build_verification_report
from backend.notion import search
from backend.notion.formatting import format_deterministic_notion_answer
from backend.query_filters import matches_source_filters
from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.intent.context import build_contextual_retrieval_text, resolve_reference_from_context
from backend.intent.search_terms import build_keyword_terms, extract_requested_limit
from backend.retrieval import grounded_mantis_terms
from backend.services.chat_service import deterministic_retrieval_response
from backend.notion.mapping import notion_status
from backend.notion.write import notion_write_intent
from backend.schemas import ChatMessage, ChatRequest
from scripts.accuracy.dynamic_accuracy_check import AccuracyCase, grade, summarize_external_reliability


class TeamQuestionUnderstandingTests(unittest.TestCase):
    def test_in_particle_is_removed_except_for_literal_nouns(self):
        for noun in ("보류", "긴급", "필수"):
            self.assertEqual(search.build_notion_terms(f"{noun}인 항목 보여줘"), [noun])
        for noun in ("로그인", "디자인", "온라인", "승인", "원인"):
            self.assertEqual(search.build_notion_terms(f"{noun} 문서 찾아줘"), [noun])
            self.assertEqual(search.build_notion_terms(f"{noun}인 항목 보여줘"), [noun])

    def test_owner_matcher_reuses_validation_and_compilation(self):
        search._known_notion_owner_matcher.cache_clear()
        self.addCleanup(search._known_notion_owner_matcher.cache_clear)
        with (
            patch.object(search, "load_known_people", return_value=(("김지은", "남궁지은"), ("김지은", "문서"))),
            patch.object(search, "is_valid_person_name", wraps=search.is_valid_person_name) as validate,
            patch.object(search.re, "compile", wraps=search.re.compile) as compile_pattern,
        ):
            for _ in range(20):
                self.assertEqual(search.known_notion_owner("결제오류 처리 방법 문서"), "")
                self.assertEqual(search.known_notion_owner("김지은의 문서"), "김지은")
                self.assertEqual(search.known_notion_owner("김지은의 문서와 남궁지은의 문서"), "남궁지은")
            self.assertEqual(validate.call_count, 3)
            self.assertEqual(compile_pattern.call_count, 1)

    def test_owner_matcher_tracks_directory_changes_and_empty_directory(self):
        search._known_notion_owner_matcher.cache_clear()
        self.addCleanup(search._known_notion_owner_matcher.cache_clear)
        with patch.object(search, "load_known_people") as directory:
            for people, expected in (((), ""), (("김지은",), "김지은"), (("남궁지은",), ""), ((), "")):
                directory.return_value = (people, ())
                self.assertEqual(search.known_notion_owner("김지은의 문서"), expected)
            directory.return_value = (("Alice",), ())
            self.assertEqual(search.known_notion_owner("ALICE의 문서"), "Alice")

    def test_implicit_owner_requires_actual_people_evidence(self):
        for known in ((), ("김지은", "남궁지은")):
            with patch.object(search, "load_known_people", return_value=(known, ())):
                for phrase in ("결제오류 처리 방법 문서 찾아줘", "로그인 처리 문서", "환불요청 처리 절차",
                               "배포작업 처리 현황", "결제모듈의 이슈", "프로젝트의 문서", "운영팀의 업무"):
                    with self.subTest(phrase=phrase, known=known):
                        self.assertEqual(search.extract_notion_constraints(phrase)["owner"], "")
                for name in ("김지은", "남궁지은"):
                    for suffix in ("이 맡은거", "이 맡고있는", "의 문서", " 담당 항목"):
                        with self.subTest(name=name, suffix=suffix, known=known):
                            self.assertEqual(search.extract_notion_constraints(name + suffix)["owner"],
                                             name if name in known else "")

    def test_explicit_four_character_names_are_preserved_without_directory(self):
        with patch.object(search, "load_known_people", return_value=((), ())):
            for name in ("남궁지은", "남궁지인", "남궁지이"):
                question = f"노션에서 담당자 {name} 항목"
                with self.subTest(name=name):
                    constraints = search.extract_notion_constraints(question)
                    self.assertEqual(constraints["owner"], name)
                    self.assertTrue(search.notion_page_matches_constraints({"owner": name}, constraints))
                    prefix = search.inherit_notion_context(["그중 상태만 완료로 바꿔서 찾아줘", question])
                    self.assertEqual(search.extract_notion_constraints(prefix)["owner"], name)

    def test_status_inflections_do_not_depend_on_suffix_allowlist(self):
        for phrase, status in (
            ("완료건수 알려줘", "완료"), ("완료한 거", "완료"),
            ("완료처리된 거", "완료"), ("결재요청중인 항목", "결재요청"),
            ("진행중이던 거", "진행중"), ("시작전이었던 항목", "시작전"),
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(search.extract_notion_constraints(phrase)["status"], status)
        for phrase in ("대기시간", "대기열", "대기시간 개선 문서", "대기열 처리 방식"):
            self.assertEqual(search.extract_notion_constraints(phrase)["status"], "")

    def test_owner_inflections_keep_owner_in_compound_conditions(self):
        phrases = ("김지은이 맡은거", "김지은이 맡고있는", "김지은의 문서",
                   "담당자는 김지은이고 시작전인 항목")
        with patch.object(search, "load_known_people", return_value=(("김지은",), ())):
            for phrase in phrases:
                with self.subTest(phrase=phrase):
                    constraints = search.extract_notion_constraints(phrase)
                    self.assertEqual(constraints["owner"], "김지은")
                    self.assertFalse(search.notion_page_matches_constraints(
                        {"owner": "김지원", "status": "시작전"}, constraints,
                    ))
                    prefix = search.inherit_notion_context(["그중 상태만 완료로 바꿔서 찾아줘", "노션에서 " + phrase])
                    self.assertEqual(search.extract_notion_constraints(prefix)["owner"], "김지은")

    def test_spoken_status_suffixes_keep_filters_and_exclusions(self):
        cases = (
            ("진행중인거 보여줘", "진행중"),
            ("결재요청건 보여줘", "결재요청"),
            ("완료된거 몇개야", "완료"),
            ("시작전인것만", "시작전"),
            ("상태가 완료인것", "완료"),
            ("상태가 검토대기인것만 보여줘", "검토대기"),
        )
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(search.extract_notion_constraints(question)["status"], expected)
        for suffix in ("인거", "인것만", "건", "된거", "인 것들만", "된 건은"):
            question = f"노션에서 완료{suffix} 보여줘"
            with self.subTest(suffix=suffix):
                self.assertEqual(search.extract_notion_constraints(question)["status"], "완료")
        for phrase in ("완료된거 빼고", "결재요청건 제외", "시작전인것은 제외"):
            with self.subTest(exclusion=phrase):
                result = search.extract_notion_constraints(phrase)
                self.assertEqual(result["status"], "")
                self.assertEqual(len(result["excluded_statuses"]), 1)
        for phrase in ("대기시간", "대기열", "대기시간표"):
            self.assertEqual(search.extract_notion_constraints(f"{phrase} 문서 찾아줘")["status"], "")

    def test_feedback_particle_must_end_before_next_word(self):
        for phrase in ("feedback 로그 누락 이슈", "feedback 인앱 팝업 이슈", "feedback 로직 이슈"):
            with self.subTest(keyword=phrase):
                constraints = extract_query_constraints(phrase)
                self.assertEqual(constraints["statuses"], [])
                self.assertIn("feedback", grounded_mantis_terms(phrase, constraints, []))
        for phrase in ("feedback인 이슈", "feedback 인 이슈", "feedback로 바꿔줘", "feedback으로 바꿔줘",
                       "feedback 상태인 이슈", "상태가 feedback인 이슈"):
            with self.subTest(status=phrase):
                self.assertEqual(extract_query_constraints(phrase)["statuses"], ["need_info"])

    def test_unknown_owner_subject_particle_uses_following_predicate(self):
        with patch.object(search, "load_known_people", return_value=((), ())):
            for name in ("김지은", "김지인", "김지이"):
                for particle in ("이", "가", "은", "는"):
                    question = f"노션에서 담당자 {name}{particle} 맡은 항목"
                    with self.subTest(question=question):
                        constraints = search.extract_notion_constraints(question)
                        self.assertEqual(constraints["owner"], name)
                        self.assertTrue(search.notion_page_matches_constraints({"owner": name}, constraints))
                        prefix = search.inherit_notion_context(["그중 상태만 완료로 바꿔서 찾아줘", question])
                        self.assertEqual(search.extract_notion_constraints(prefix)["owner"], name)
                self.assertEqual(search.extract_notion_constraints(f"담당자 {name} 항목")["owner"], name)
        with patch.object(search, "load_known_people", return_value=(("김지은이",), ())):
            self.assertEqual(search.extract_notion_constraints("담당자 김지은이 맡은 항목")["owner"], "김지은이")

    def test_owner_names_and_followups_preserve_full_name(self):
        with patch.object(search, "load_known_people", return_value=(("김지",), ())):
            for name in ("김지은", "김지인", "김지이"):
                with self.subTest(name=name):
                    prior = f"노션에서 담당자 {name} 항목 찾아줘"
                    self.assertEqual(search.extract_notion_constraints(prior)["owner"], name)
                    prefix = search.inherit_notion_context(["그중 상태만 완료로 바꿔서 찾아줘", prior])
                    self.assertEqual(search.extract_notion_constraints(prefix)["owner"], name)
                    self.assertTrue(search.notion_page_matches_constraints({"owner": f"박보람, {name}"}, {"owner": name}))
                    for wrong in ("김지원", "김지훈", name + "수"):
                        self.assertFalse(search.notion_page_matches_constraints({"owner": wrong}, {"owner": name}))
        self.assertFalse(search.notion_page_matches_constraints({"owner": "김지원"}, {"owner": "김지"}))
        clause = {"field": "owner", "operator": "equals", "value": "김지은"}
        self.assertTrue(search.notion_page_matches_common_filters({"owner": "박보람, 김지은"}, [clause]))
        self.assertFalse(search.notion_page_matches_common_filters({"owner": "김지원"}, [clause]))

    def test_status_options_are_exact_across_all_status_fields(self):
        properties = {
            "상태": {"type": "select", "select": {"options": [{"name": "결재완료"}]}},
            "구상태": {"type": "status", "status": {"options": [{"name": "완료"}]}},
        }
        with patch.object(search, "fetch_notion_database_schema", return_value={"properties": properties}):
            self.assertEqual(search.notion_database_status_filter("db", "완료"),
                             (True, {"property": "구상태", "status": {"equals": "완료"}}))
            properties["진행상태"] = {"type": "select", "select": {"options": [{"name": "완료"}]}}
            supported, api_filter = search.notion_database_status_filter("db", "완료")
            self.assertTrue(supported)
            self.assertEqual({item["property"] for item in api_filter["or"]}, {"구상태", "진행상태"})
            self.assertEqual(search.notion_database_status_filter("db", "미완료"), (True, None))

    def test_keyword_and_adjective_are_not_status_options(self):
        for phrase in ("대기시간 개선 문서", "상태가 변경된 항목", "상태가 오래된 문서", "상태가 변경된 항목 중 담당자 김지인 것"):
            question = f"노션에서 {phrase} 찾아줘"
            self.assertEqual(search.extract_notion_constraints(question)["status"], "")
        self.assertIn("대기시간", search.build_notion_terms("노션에서 대기시간 개선 문서 찾아줘"))
        for question in ("feedback 팝업 관련 이슈", "feedbackloop 팝업 관련 이슈"):
            constraints = extract_query_constraints(question)
            self.assertEqual(constraints["statuses"], [])
            self.assertIn(question.split()[0], grounded_mantis_terms(question, constraints, []))
        for question in ("상태가 feedback인 이슈", "feedback 상태 이슈", "상태만 feedback으로 바꿔줘"):
            self.assertEqual(extract_query_constraints(question)["statuses"], ["need_info"])

    def test_check_request_is_not_a_confirmed_status_filter(self):
        for ending in ("확인해줘", "확인해 주세요", "확인해줄래?"):
            question = f"화면번호 99999 관련 Mantis 이슈가 있는지 {ending}"
            constraints = extract_query_constraints(question)
            self.assertEqual(constraints["issue_ids"], [])
            self.assertEqual(constraints["statuses"], [])
            self.assertEqual(constraints["screen_numbers"], ["99999"])
        self.assertEqual(extract_query_constraints("확인된 이슈가 있는지 확인해줘")["statuses"], ["confirmed"])

    def test_template_instruction_words_are_not_search_constraints(self):
        questions = (
            "#31411 이슈의 현재 상태, 확인한 내용과 다음 행동을 정리해줘",
            "#31411 이슈의 현재 확인된 사실을 고객 안내 메일로 작성해줘",
            "#31411와 비슷한 과거 이슈의 원인과 해결 내용을 비교해줘",
        )
        for question in questions:
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                self.assertEqual(constraints["statuses"], [])
        self.assertEqual(extract_query_constraints("확인된거 보여줘")["statuses"], ["confirmed"])
        self.assertEqual(extract_query_constraints("해결된 건 보여줘")["statuses"], ["resolved"])
        triage = extract_query_constraints(
            "미래에셋증권 MTS 장애의 초동 확인 순서와 담당자 전달 내용을 정리해줘"
        )
        self.assertEqual(triage["customer_key"], "미래에셋증권")
        self.assertEqual(triage["people"]["names"], [])
        self.assertIsNone(triage["created_at"])
        triage_terms = build_keyword_terms(
            "긴급 우선순위로 미래에셋증권 MTS에서 발생한 "
            "'로그인 후 주문 화면이 열리지 않음' 이슈의 영향 범위와 초동 확인 순서를 정리해줘",
            triage,
        )
        self.assertIn("로그인", triage_terms)
        self.assertIn("주문", triage_terms)
        self.assertNotIn("긴급", triage_terms)
        self.assertNotIn("우선순위", triage_terms)

    def test_relative_date_term_is_removed_after_structured_parsing(self):
        question = "오늘 등록된 미종료 이슈를 고객사별로 요약해줘"
        constraints = extract_query_constraints(question)
        self.assertEqual(build_keyword_terms(question, constraints), [])

    def test_document_template_list_alias_targets_form_database(self):
        self.assertEqual(search.extract_notion_database_label_from_text("템플릿 목록 보여줘"), "문서양식")
        self.assertEqual(search.extract_notion_database_label_from_text("양식 리스트 알려줘"), "문서양식")
        self.assertIsNone(search.extract_notion_database_label_from_text("서버 재부팅 템플릿 내용 알려줘"))
        self.assertEqual(
            search.remove_notion_database_label_terms(["템플릿"], "문서양식"),
            [],
        )

    def test_lookup_titles_and_filter_corrections_do_not_request_document_writes(self):
        for question in (
            '노션에서 "차트 수정해주세요" 문서 찾아줄래?',
            '노션에서 "계좌 추가해주세요" 찾아주세요',
            '노션에서 "배포 계획" 상태만 시작전으로 바꿔서 찾아줘',
        ):
            with self.subTest(question=question):
                self.assertIsNone(notion_write_intent(question))
        self.assertEqual(notion_write_intent('노션에서 "배포 계획" 상태를 완료로 바꿔줘'), "update")

    def test_issue_number_suffix_is_understood_in_conversational_requests(self):
        for question in (
            "0031397번 이슈 내용 좀 알려줄래?",
            "이슈 0031397번이 어떤 내용이야?",
            "31397번 티켓 보여주세요",
        ):
            with self.subTest(question=question):
                self.assertEqual(extract_query_constraints(question)["issue_ids"], [31397])
        self.assertEqual(extract_query_constraints("화면번호 9413번 이슈 보여줄래?")["issue_ids"], [])

    def test_polite_request_words_do_not_become_search_keywords(self):
        for ending in ("보여줄래?", "보여주실래요?", "알려줄 수 있나요?", "보여주세요"):
            question = f"신한 최근 이슈 세 개 {ending}"
            with self.subTest(question=question):
                self.assertEqual(grounded_mantis_terms(question, extract_query_constraints(question), [ending]), [])

    def test_project_code_cannot_become_a_model_invented_issue_id(self):
        question = "108SK 상태가 acknowledged인 최근 이슈 하나만 알려줘"
        constraints = merge_query_plan_constraints(extract_query_constraints(question), {
            "grounding_text": question, "filter_clauses": [
                {"field": "issue_id", "operator": "equals", "value": "108SK"},
                {"field": "issue_id", "operator": "equals", "value": "108"},
            ],
        })
        self.assertEqual(constraints["issue_ids"], [])
        self.assertFalse(any(clause["field"] == "issue_id" for clause in constraints["filter_clauses"]))

    def test_native_mantis_status_correction_does_not_inherit_closed(self):
        for status, expected in (("acknowledged", "review"), ("feedback", "need_info")):
            with self.subTest(status=status):
                query, rewrite = build_contextual_retrieval_text([
                    ChatMessage(role="user", content="신한에서 상태가 closed인 최근 이슈 2개 보여줘"),
                    ChatMessage(role="user", content=f"아니, 고객사는 그대로 두고 상태만 {status}로 바꿔줘"),
                ])
                self.assertFalse(rewrite)
                self.assertEqual(extract_query_constraints(query)["statuses"], [expected])
                self.assertTrue(extract_query_constraints(query)["customer_key"])

    def test_notion_single_item_reference_uses_the_actual_previous_source(self):
        with patch("backend.understanding.load_audit_sources", return_value=[{
            "type": "notion", "page_id": "p1", "title": "배포 계획",
        }]):
            query = resolve_reference_from_context("그 항목의 제목을 정확히 다시 알려줘", "audit-1", "user-1")
        self.assertEqual(query, '노션에서 "배포 계획" 그 항목의 제목을 정확히 다시 알려줘')

    def test_query_and_display_use_the_same_status_property(self):
        fields = {
            "진행상태": {"type": "status", "status": {"name": "시작전", "options": [{"name": "시작전"}]}},
            "상태": {"type": "select", "select": {"name": "종료된이슈", "options": [{"name": "종료된이슈"}]}},
        }
        for properties in (fields, dict(reversed(list(fields.items())))):
            with self.subTest(order=list(properties)):
                page = {"status": notion_status(properties), "status_fields": {
                    "진행상태": "시작전", "상태": "종료된이슈",
                }}
                self.assertTrue(search.notion_page_matches_and_enrich(page, {"status": "시작전"}))
                self.assertEqual(page["status"], "시작전")
                self.assertEqual(page["status_property"], "진행상태")
                self.assertFalse(search.notion_page_matches_and_enrich(page, {"excluded_statuses": ["시작전"]}))
                with patch.object(search, "fetch_notion_database_schema", return_value={"properties": properties}):
                    self.assertEqual(search.notion_database_status_filter("db", "시작전")[1]["property"], "진행상태")
                    self.assertEqual(search.notion_database_status_filter("db", "종료된이슈")[1]["property"], "상태")

    def test_team_mantis_phrasings_preserve_platform_count_and_exclusions(self):
        cases = [
            ("신한 MTS 최근 이슈 세 개만 보여줄래?", "MTS", 3, []),
            ("하나금융 HTS 최근 이슈 두 건만 보여주세요", "HTS", 2, []),
            ("SK증권 이슈에서 MTS는 제외하고 세 건만 보여주세요", None, 3, ["MTS"]),
            ("키움 HTS 이슈 다섯 개만 알려줘", "HTS", 5, []),
        ]
        for question, platform, limit, excluded in cases:
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                self.assertEqual(constraints["platform_key"], platform.lower() if platform else None)
                self.assertEqual(constraints["excluded_platform_keys"], [value.lower() for value in excluded])
                self.assertEqual(extract_requested_limit(question), limit)

    def test_zero_padded_issue_ids_match_numeric_ids_without_changing_screen_numbers(self):
        for value in ("0031397", "#31397", 31397):
            with self.subTest(value=value):
                self.assertTrue(matches_source_filters("mantis", {"issue_id": 31397}, [
                    {"field": "issue_id", "operator": "equals", "value": value},
                ]))
        self.assertFalse(matches_source_filters("mantis", {"screen_number": "0090"}, [
            {"field": "screen_number", "operator": "equals", "value": "90"},
        ]))

    def test_natural_status_wordings_keep_all_explicit_conditions(self):
        cases = [
            ("노션에서 시작전인 항목 찾아줘", "시작전", []),
            ("노션에서 시작 전 문서만 보여줘", "시작전", []),
            ("노션에서 진행 중인 업무 알려줘", "진행중", []),
            ("노션에서 상태가 검토대기인 항목 찾아줘", "검토대기", []),
            ("노션에서 상태는 QA검토중인 문서 보여줘", "QA검토중", []),
            ("노션에서 미완료 항목 찾아줘", "미완료", []),
            ("노션에서 결재완료 항목은 빼고 보여줘", "", ["결재완료"]),
            ("노션에서 결재완료는 빼고 보여줘", "", ["결재완료"]),
            ("노션에서 결재완료 상태는 빼고 보여줘", "", ["결재완료"]),
            ("노션에서 상태가 시작전이 아닌 항목 찾아줘", "", ["시작전"]),
            ("노션에서 보관됨 말고 시작전만 보여줘", "시작전", ["보관됨"]),
            ("노션에서 결재완료(팀장) 제외하고 보여줘", "", ["결재완료(팀장)"]),
            ('노션에서 "완료된 MTS 12345 이슈" 찾아줘', "", []),
            ('노션에서 상태가 시작전인 "완료된 MTS 12345 이슈" 찾아줘', "시작전", []),
        ]
        for question, status, excluded in cases:
            with self.subTest(question=question):
                result = search.extract_notion_constraints(question)
                self.assertEqual(result["status"].replace(" ", ""), status)
                self.assertEqual(result["excluded_statuses"], excluded)

    @patch.object(search, "load_known_people", return_value=(("홍길동",), ()))
    def test_status_change_preserves_title_and_owner(self, _known_people):
        latest = "그 항목에서 상태만 시작전으로 바꿔서 찾아줘"
        prior = '노션에서 담당자 홍길동인 "배포 계획" 중 결재요청 상태 찾아줘'
        prefix = search.inherit_notion_context([latest, prior])
        result = search.extract_notion_constraints(f"{prefix} {latest}")
        self.assertEqual(result["exact_title"], "배포 계획")
        self.assertEqual(result["status"], "시작전")
        self.assertEqual(result["owner"], "홍길동")
        self.assertNotIn("결재요청", prefix)

    def test_owner_change_preserves_title_and_status(self):
        latest = "그중 담당자만 김보람으로 바꿔서 찾아줘"
        prior = '노션에서 담당자 홍길동인 "배포 계획" 중 결재요청 상태 찾아줘'
        query = f"{search.inherit_notion_context([latest, prior])} {latest}"
        result = search.extract_notion_constraints(query)
        self.assertEqual(result["owner"], "김보람")
        self.assertEqual(result["status"], "결재요청")
        self.assertEqual(result["exact_title"], "배포 계획")

    def test_new_question_does_not_inherit_old_notion_conditions(self):
        self.assertEqual(search.inherit_notion_context([
            "Mantis 최근 이슈 보여줘", "노션에서 결재요청 상태 찾아줘",
        ]), "")

    def test_complete_is_not_incomplete_and_titles_are_exact(self):
        self.assertFalse(search.notion_page_matches_constraints(
            {"status": "미완료"}, {"status": "완료"},
        ))
        self.assertFalse(search.notion_page_matches_constraints(
            {"title": "배포 계획 과거 사본"}, {"exact_title": "배포 계획"},
        ))

    def test_relation_owner_is_resolved_before_unassigned_filter(self):
        page = {"title": "배포 계획", "owner": "담당자 미지정"}
        with patch.object(search, "resolve_notion_page_owner", side_effect=lambda row: row.update(owner="홍길동")):
            self.assertFalse(search.notion_page_matches_and_enrich(page, {"unassigned_owner": True}))


class TeamQuestionSearchTests(unittest.TestCase):
    def test_adnominal_keywords_find_source_without_particle(self):
        for noun in ("보류", "긴급", "필수"):
            response = Mock(status_code=200)
            response.json.return_value = {"results": [
                {"id": "right", "title": f"{noun} 업무", "status": noun, "search_text": f"{noun} 업무"},
                {"id": "wrong", "title": "일반 업무", "status": "진행중", "search_text": "일반 업무"},
            ], "has_more": False}
            with (
                self.subTest(noun=noun),
                patch.object(search, "load_known_people", return_value=((), ())),
                patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
                patch.object(search, "fetch_notion_database_title", return_value="업무"),
                patch.object(search, "notion_database_status_filter", return_value=(False, None)),
                patch.object(search, "notion_property_schema", return_value={}),
                patch.object(search, "notion_post_json", return_value=response),
                patch.object(search, "fetch_notion_page_blocks", return_value=""),
                patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: dict(page)),
            ):
                result = search.execute_notion_search(f"노션에서 {noun}인 항목 보여줘", include_blocks=False)
                self.assertEqual(result["meta"]["terms"], [noun])
                self.assertEqual([page["id"] for page in result["notion_pages"]], ["right"])

    def test_business_keywords_remain_searchable_without_owner_filter(self):
        for phrase, keyword in (
            ("결제오류 처리 방법 문서 찾아줘", "결제오류"), ("로그인 처리 문서", "로그인"),
            ("환불요청 처리 절차", "환불요청"), ("배포작업 처리 현황", "배포작업"),
            ("결제모듈의 이슈", "결제모듈"), ("프로젝트의 문서", "프로젝트"), ("운영팀의 업무", "운영팀"),
        ):
            response = Mock(status_code=200)
            response.json.return_value = {"results": [
                {"id": "right", "title": phrase, "search_text": phrase, "owner": "김보람"},
            ], "has_more": False}
            if keyword == "로그인":
                response.json.return_value["results"].append({
                    "id": "log-only", "title": "로그 처리 문서", "search_text": "로그 처리 문서", "owner": "김보람",
                })
            with (
                self.subTest(phrase=phrase),
                patch.object(search, "load_known_people", return_value=(("김보람",), ())),
                patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
                patch.object(search, "fetch_notion_database_title", return_value="업무"),
                patch.object(search, "notion_database_status_filter", return_value=(False, None)),
                patch.object(search, "notion_property_schema", return_value={}),
                patch.object(search, "notion_post_json", return_value=response),
                patch.object(search, "fetch_notion_page_blocks", return_value=""),
                patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: dict(page)),
            ):
                result = search.execute_notion_search("노션에서 " + phrase, include_blocks=False)
                self.assertEqual(result["meta"]["constraints"]["owner"], "")
                self.assertIn(keyword, result["meta"]["terms"])
                self.assertEqual([page["id"] for page in result["notion_pages"]], ["right"])

    def test_spoken_filters_reach_search_and_count_without_stray_keywords(self):
        questions = (
            ("진행중인거 보여줘", "진행중", ""),
            ("결재요청건 보여줘", "결재요청", ""),
            ("완료된거 몇개야", "완료", ""),
            ("시작전인것만", "시작전", ""),
            ("상태가 완료인것", "완료", ""),
            ("담당자 김지은이 맡은 항목", "", "김지은"),
            ("담당자 김지은이 처리한 항목", "", "김지은"),
            ("완료건수 알려줘", "완료", ""),
            ("완료한 거", "완료", ""),
            ("완료처리된 거", "완료", ""),
            ("결재요청중인 항목", "결재요청", ""),
            ("진행중이던 거", "진행중", ""),
            ("시작전이었던 항목", "시작전", ""),
            ("김지은이 맡은거", "", "김지은"),
            ("김지은이 맡고있는", "", "김지은"),
            ("김지은의 문서", "", "김지은"),
            ("담당자는 김지은이고 시작전인 항목", "시작전", "김지은"),
        )
        for question, status, owner in questions:
            response = Mock(status_code=200)
            response.json.return_value = {"results": [
                {"id": "right", "title": "배포 계획", "search_text": "배포 계획",
                 "status": status or "완료", "owner": owner or "박보람"},
                {"id": "wrong", "title": "배포 계획 과거", "search_text": "배포 계획 과거",
                 "status": "보관됨", "owner": "김지원"},
            ], "has_more": False}
            if owner:
                response.json.return_value["results"].append({
                    "id": "same-status-other-owner", "title": "다른 사람 배포 계획",
                    "search_text": "배포 계획", "status": status or "완료", "owner": "김지원",
                })
            with (
                self.subTest(question=question),
                patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
                patch.object(search, "load_known_people", return_value=(("김지은",) if not question.startswith("담당자") else (), ())),
                patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
                patch.object(search, "fetch_notion_database_title", return_value="업무"),
                patch.object(search, "notion_database_status_filter", return_value=(False, None)),
                patch.object(search, "notion_property_schema", return_value={}),
                patch.object(search, "notion_post_json", return_value=response),
                patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: dict(page)),
            ):
                result = search.execute_notion_search(question, include_blocks=False)
                self.assertEqual(result["meta"]["terms"], [])
                self.assertEqual([page["id"] for page in result["notion_pages"]], ["right"])
                self.assertEqual(result["meta"]["candidate_count"], 1)
                if "몇개야" in question or "건수" in question:
                    self.assertIn("총 1건", format_deterministic_notion_answer(question, {"stages": [result]}, 5))

    def test_notion_lookup_wordings_always_return_notion_results(self):
        for ending in ("문서 좀 찾아줄래?", "찾아주세요. 상태가 보관됨인 것만요", "보여주실래요?"):
            question = f"노션에서 'DB 증권 추가 기능 가능 여부 문의' {ending}"
            request = ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=question)])
            page = {"id": "p1", "title": "DB 증권 추가 기능 가능 여부 문의", "status": "보관됨"}
            context = SimpleNamespace(
                intent_plan={"source": "notion"}, retrieval_text=question,
                query_constraints=extract_query_constraints(question),
                retrieval_result={"intent_plan": {"source": "notion"}, "stages": [
                    {"source": "notion", "status": "ok", "notion_pages": [page], "meta": {}},
                ]},
            )
            with self.subTest(question=question):
                result = deterministic_retrieval_response(request, context, 5, None)
                self.assertEqual(result["raw"]["mode"], "deterministic_notion_list")
                self.assertEqual(result["sources"][0]["page_id"], "p1")
                self.assertIn(page["title"], result["content"])

    def test_page_limit_marks_search_incomplete(self):
        response = Mock(status_code=200)
        response.json.return_value = {"results": [{"id": "p1", "title": "배포 계획"}],
                                      "has_more": True, "next_cursor": "next"}
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "fetch_notion_database_title", return_value="업무"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_search_max_pages", return_value=1),
            patch.object(search, "notion_post_json", return_value=response),
            patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: dict(page)),
        ):
            result = search.execute_notion_search('노션에서 "배포 계획" 찾아줘', include_blocks=False)
        self.assertFalse(result["meta"]["search_complete"])
        self.assertEqual(result["meta"]["candidate_count"], 1)

    def test_truncated_display_body_does_not_mark_completed_search_incomplete(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [{"id": "p1", "title": "기술공유", "search_text": "기술공유"}],
            "has_more": False,
        }
        with (
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="기술공유"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_title_property", return_value="제목"),
            patch.object(search, "notion_post_json", return_value=response),
            patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: {**page, **kwargs}),
            patch.object(
                search,
                "fetch_notion_page_blocks",
                return_value=f"본문\n{search.NOTION_CONTENT_INCOMPLETE}",
            ),
        ):
            stage = search.execute_notion_search(
                '노션에서 "기술공유" 찾아줘', include_blocks=True
            )

        self.assertTrue(stage["meta"]["search_complete"])
        self.assertEqual(stage["meta"]["incomplete_reasons"], [])
        self.assertFalse(stage["meta"]["content_complete"])
        self.assertEqual(stage["meta"]["content_incomplete_page_ids"], ["p1"])
        content = format_deterministic_notion_answer(
            '노션에서 "기술공유" 찾아줘', {"stages": [stage]}, 5
        )
        self.assertNotIn("일부 항목을 아직", content)

    def test_keyword_page_limit_explains_scope_without_claiming_query_error(self):
        responses = []
        for index in range(3):
            response = Mock(status_code=200)
            response.json.return_value = {
                "results": [{"id": f"p{index}", "title": "대기시간", "search_text": "대기시간"}],
                "has_more": True, "next_cursor": f"cursor{index}",
            }
            responses.append(response)
        with (
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "fetch_notion_database_title", return_value="업무"),
            patch.object(search, "notion_database_status_filter", return_value=(False, None)),
            patch.object(search, "notion_property_schema", return_value={}),
            patch.object(search, "notion_post_json", side_effect=responses),
            patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: dict(page)),
        ):
            stage = search.execute_notion_search("노션에서 대기시간 문서 찾아줘", include_blocks=False)
        self.assertEqual(stage["status"], "ok")
        self.assertFalse(stage["meta"]["search_complete"])
        self.assertEqual(stage["meta"]["incomplete_reasons"], ["page_limit"])
        self.assertIn("대기시간", stage["meta"]["terms"])
        content = format_deterministic_notion_answer("노션에서 대기시간 문서 찾아줘", {"stages": [stage]}, 5)
        self.assertIn("최대 300건", content)
        self.assertNotIn("일부 항목을 아직", content)
        report = build_verification_report("노션에서 대기시간 문서 찾아줘", content, [], {"stages": [stage]}, "test")
        self.assertEqual(next(c for c in report["checks"] if c["id"] == "search_completeness")["status"], "warning")
        result = grade(AccuracyCase("scope", "notion", "범위", "노션에서 대기시간 문서 찾아줘", "titles", ["대기시간"]),
                       {"content": content, "sources": [{"type": "notion", "title": "대기시간"}],
                        "retrieval": {"stages": [stage], "intent_plan": {"source": "notion"}}})
        self.assertTrue(result["passed"])
        self.assertTrue(result["coverage_warning"])
        self.assertNotIn("조회 완전성", result["failed_checks"])

    def test_same_title_wrong_status_is_rejected_and_next_page_is_checked(self):
        responses = []
        for data in (
            {"results": [{"id": "wrong", "title": "배포 계획", "status": "종료된이슈"}],
             "has_more": True, "next_cursor": "page-2"},
            {"results": [{"id": "right", "title": "배포 계획", "status": "시작전"}], "has_more": False},
        ):
            response = Mock(status_code=200)
            response.json.return_value = data
            responses.append(response)
        status_filter = {"property": "상태", "status": {"equals": "시작전"}}
        with (
            patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)),
            patch.object(search, "get_notion_settings", return_value={"token": "t", "database_ids": ["db"]}),
            patch.object(search, "fetch_notion_database_title", return_value="업무"),
            patch.object(search, "notion_database_status_filter", return_value=(True, status_filter)),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "notion_post_json", side_effect=responses) as post,
            patch.object(search, "normalize_notion_page", side_effect=lambda page, **kwargs: dict(page)),
        ):
            result = search.execute_notion_search('노션에서 상태가 시작전인 "배포 계획" 찾아줘', include_blocks=False)
        self.assertEqual([row["id"] for row in result["notion_pages"]], ["right"])
        self.assertEqual(post.call_count, 2)
        self.assertIn(status_filter, post.call_args.args[1]["filter"]["and"])
        self.assertTrue(result["meta"]["search_complete"])

    def test_partial_counts_are_not_presented_as_database_totals(self):
        stage = {"source": "notion", "status": "ok", "meta": {
            "candidate_count": 2, "search_complete": False,
        }, "notion_pages": [{"id": "a", "title": "업무"}]}
        content = format_deterministic_notion_answer("노션 업무 총 몇 건이야?", {"stages": [stage]}, 5)
        self.assertIn("조회한 범위", content)
        self.assertNotIn("총 2건", content)
        self.assertIn("전체 건수와 다를", content)


class StrictAccuracyGradingTests(unittest.TestCase):
    def test_external_reliability_is_reported_separately_from_wrong_answers(self):
        summary = summarize_external_reliability([
            {"outcome": "passed"},
            {"outcome": "assertion_failed"},
            {"outcome": "dependency_error"},
        ])
        self.assertEqual(summary, {"total": 3, "passed": 2, "score": 67})

    def test_no_results_cannot_hide_an_invented_status_filter(self):
        case = AccuracyCase("none", "mantis", "없음", "화면번호 99999 관련 이슈가 있는지 확인해줘", "no_results", [])
        result = grade(case, {"content": "조건에 맞는 이슈가 없습니다.", "sources": [], "retrieval": {
            "intent_plan": {"source": "mantis"},
            "constraints": {"screen_numbers": ["99999"], "statuses": ["confirmed"]},
        }})
        self.assertFalse(result["passed"])
        self.assertFalse(result["filter_precision_passed"])

    def test_repeating_missing_issue_id_does_not_count_as_a_successful_lookup(self):
        for kind in ("issue_ids", "exact_issue_ids", "ordered_issue_ids"):
            with self.subTest(kind=kind):
                case = AccuracyCase("id", "mantis", "번호", "0031397 이슈 알려줘", kind, [31397])
                result = grade(case, {"content": "#31397 이슈를 찾지 못했어요.", "sources": []})
                self.assertFalse(result["passed"])

    def notion_response(self, status="시작전"):
        return {
            "content": f"배포 계획 · 상태 {status}",
            "sources": [{"type": "notion", "title": "배포 계획", "status": status}],
            "retrieval": {"intent_plan": {"source": "notion"}, "stages": [{
                "source": "notion", "status": "ok", "meta": {
                    "constraints": {"exact_title": "배포 계획", "status": "시작전"},
                },
            }]},
        }

    def test_title_match_cannot_hide_wrong_status(self):
        case = AccuracyCase("status", "notion", "상태", '노션에서 상태가 시작전인 "배포 계획" 찾아줘',
                            "titles", ["배포 계획"], expected_constraints={"exact_title": "배포 계획", "status": "시작전"})
        wrong = grade(case, self.notion_response("종료된이슈"))
        self.assertTrue(wrong["answer_passed"])
        self.assertFalse(wrong["source_constraints_passed"])
        self.assertFalse(wrong["passed"])
        self.assertTrue(grade(case, self.notion_response())["passed"])

    def test_empty_answer_is_not_proof_of_no_results(self):
        case = AccuracyCase("empty", "notion", "없음", "노션에서 없는 항목 찾아줘", "no_results", [])
        self.assertFalse(grade(case, {"content": "", "sources": []})["passed"])

    def test_fact_only_in_source_does_not_count_as_answer(self):
        case = AccuracyCase("fact", "mantis", "속성", "어느 고객사야?", "text_any", ["SK증권"])
        result = grade(case, {"content": "이슈를 찾았습니다.", "sources": [{"title": "SK증권 이슈"}]})
        self.assertFalse(result["passed"])

    def test_wrong_routing_cannot_pass_with_matching_issue_id(self):
        case = AccuracyCase("route", "mantis", "라우팅", "0031397 이슈 알려줘", "exact_issue_ids", [31397])
        result = grade(case, {"content": "#31397", "sources": [{"issue_id": 31397}],
                              "retrieval": {"intent_plan": {"source": "notion"}}})
        self.assertFalse(result["passed"])

    def test_runtime_verification_rejects_notion_condition_mismatch(self):
        response = self.notion_response("종료된이슈")
        report = build_verification_report('노션에서 상태가 시작전인 "배포 계획" 찾아줘', response["content"],
                                           response["sources"], response["retrieval"], "deterministic_notion_list")
        checks = report["checks"]
        self.assertTrue(any(check["id"] == "constraints" and check["status"] == "fail" for check in checks))

    def test_mantis_text_only_mismatch_is_warning_not_definite_failure(self):
        report = build_verification_report(
            "할당된 이슈 보여줘", "이슈를 확인했습니다.",
            [{"type": "mantis", "title": "배포 계획"}],
            {"constraints": extract_query_constraints("할당된 이슈 보여줘")}, "test",
        )
        check = next(item for item in report["checks"] if item["id"] == "constraints")
        self.assertEqual(check["status"], "warning")
        self.assertIn("가능성", check["detail"])
