import unittest
from unittest.mock import MagicMock, patch
import requests
from datetime import date

from backend.answers import (
    build_mantis_count_report,
    display_constraints,
    format_deterministic_issue_answer,
    generate_follow_up_questions,
    select_grouped_issue_rows,
    should_return_deterministic_issue_list,
)
from backend.config import notion_post_json
from backend.intent.entities import extract_mantis_keyword
from backend.intent.source_router import rule_based_search_intent
from backend.reports.effort import extract_effort_query
from backend.reports.evaluation import build_verification_report
from backend.retrieval import grounded_mantis_terms, run_retrieval_pipeline
from backend.sources.mantis import issue_matches_constraints
from backend.understanding import (
    build_contextual_retrieval_text,
    build_keyword_terms,
    build_similar_issue_search_text,
    chroma_text_matches_constraints,
    extract_query_constraints,
    extract_requested_limit,
    is_follow_up_question,
    resolve_result_limit,
)
from backend.schemas import ChatMessage, ChatRequest
from backend.reports.evaluation import generate_candidate_model_answer
from backend.sources.notion import notion_page_matches_constraints
from backend.query_filters import notion_temporal_field
from backend.query_planning import (
    build_query_plan,
    extract_created_at_range,
    extract_group_by,
    extract_relative_created_at_range,
    infer_operation,
    plan_created_at_matches,
    question_has_temporal_expression,
    validate_query_plan,
)


class QueryPlanningTests(unittest.TestCase):
    def test_weekly_media_grouping_routes_to_mantis_without_keyword_pollution(self):
        question = "금주 접수된 건 보여줘 매체별로"
        constraints = extract_query_constraints(question)
        source_plan = rule_based_search_intent(question, constraints)
        plan = build_query_plan(question, source_plan, constraints)

        self.assertEqual(extract_group_by(question), "platform")
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(source_plan["source"], "mantis")
        self.assertEqual(source_plan["authority"], "authoritative")
        self.assertEqual(plan["group_by"], "platform")
        self.assertEqual(plan["filters"]["created_at"]["precision"], "this_week")

    def test_media_grouped_issue_list_keeps_all_rows_under_media_headings(self):
        def issue(issue_id, media):
            return {
                "id": issue_id,
                "project": "고객사",
                "summary": f"테스트 이슈 {issue_id}",
                "status": "confirmed",
                "handler": "담당자",
                "created_at": "2026-09-10",
                "media_type": media,
            }

        question = "금주 접수된 건 보여줘 매체별로"
        constraints = extract_query_constraints(question)
        retrieval = {
            "intent_plan": {"source": "mantis", "group_by": "platform"},
            "stages": [{
                "source": "mantis_page_scan",
                "status": "ok",
                "issues_list": [issue(3, "HTS"), issue(2, "MTS"), issue(1, "HTS")],
                "meta": {"found_count": 3},
            }],
        }

        answer = format_deterministic_issue_answer(question, constraints, retrieval, 200)

        self.assertIn("매체 2개로 나눠", answer)
        self.assertIn("**HTS · 2건**", answer)
        self.assertIn("**MTS · 1건**", answer)
        self.assertEqual(answer.count("**ID**:"), 3)
        self.assertIn("총 3건을 확인했습니다", answer)

    def test_weekly_report_control_words_do_not_become_mantis_keywords(self):
        question = "저번주 업무보고서 작성해줘 담당자: 석지원"
        constraints = extract_query_constraints(question)
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(
            grounded_mantis_terms(
                question,
                constraints,
                ["업무보고서", "저번주"],
            ),
            [],
        )

    def test_customer_grouping_is_not_hana_customer_filter(self):
        question = "고객사별 최근 이슈를 하나씩 보여줘"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(
            question,
            {"source": "mantis"},
            constraints,
            {
                "planned": True,
                "source": "mantis",
                "limit": 1,
                "filters": [
                    {"field": "customer", "operator": "equals", "value": "하나"}
                ],
            },
        )

        self.assertIsNone(constraints["customer_key"])
        self.assertEqual(constraints["customer_aliases"], [])
        self.assertEqual(constraints["filter_clauses"], [])
        self.assertEqual(plan["group_by"], "customer")
        self.assertFalse(any(clause["field"] == "customer" for clause in plan["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertTrue(should_return_deterministic_issue_list(question, constraints))

    def test_customer_grouping_keeps_latest_issue_per_project(self):
        issues = [
            {"id": 100, "project": "고객사A"},
            {"id": 300, "project": "고객사A"},
            {"id": 275, "project": "고객사A_Project"},
            {"id": 250, "project": "고객사B"},
            {"id": 200, "project": "고객사B"},
            {"id": 150, "project": "고객사C"},
        ]

        grouped = select_grouped_issue_rows(issues, "customer")

        self.assertEqual([issue["id"] for issue in grouped], [300, 250, 150])

    def test_follow_up_quantity_hana_inherits_the_previous_customer(self):
        messages = [
            ChatMessage(role="user", content="159eBest에서 상태가 closed인 최근 이슈 2개 보여줘"),
            ChatMessage(role="assistant", content="31350, 31347입니다"),
            ChatMessage(role="user", content="방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘"),
        ]

        latest = extract_query_constraints(messages[-1].content)
        retrieval_text, needs_rewrite = build_contextual_retrieval_text(messages)
        inherited = extract_query_constraints(retrieval_text)

        self.assertIsNone(latest["customer_key"])
        self.assertFalse(needs_rewrite)
        self.assertEqual(inherited["customer_key"], "159eBest")
        self.assertEqual(inherited["statuses"], ["closed"])
        self.assertEqual(build_keyword_terms(retrieval_text, inherited), [])

    def test_follow_up_condition_correction_keeps_customer_and_replaces_status(self):
        messages = [
            ChatMessage(role="user", content="HMC증권에서 상태가 closed인 최근 이슈 2개 보여줘"),
            ChatMessage(role="assistant", content="결과입니다"),
            ChatMessage(
                role="user",
                content="아니, 고객사는 그대로 두고 상태만 confirmed로 바꿔서 가장 최근 이슈 하나를 알려줘",
            ),
        ]

        retrieval_text, needs_rewrite = build_contextual_retrieval_text(messages)
        inherited = extract_query_constraints(retrieval_text)

        self.assertTrue(is_follow_up_question(messages[-1].content))
        self.assertFalse(needs_rewrite)
        self.assertEqual(inherited["customer_key"], "현대차")
        self.assertEqual(inherited["statuses"], ["confirmed"])
        self.assertEqual(build_keyword_terms(retrieval_text, inherited), [])

    def test_handler_possessive_particle_is_not_part_of_name(self):
        constraints = extract_query_constraints("담당자 강민성의 최근 이슈 2개 보여줘")
        self.assertEqual(constraints["people"]["names"], ["강민성"])

    def test_handler_subject_particle_is_not_part_of_name(self):
        constraints = extract_query_constraints("담당자 홍길동이 맡은 최근 이슈를 보여줘")
        self.assertEqual(constraints["people"]["names"], ["홍길동"])

    def test_handler_name_accepts_sentence_ending_punctuation(self):
        question = extract_query_constraints("담당자 홍길동?")
        exclamation = extract_query_constraints("담당자 홍길동!")
        self.assertEqual(question["people"]["names"], ["홍길동"])
        self.assertEqual(exclamation["people"]["names"], ["홍길동"])

    def test_handler_name_ending_in_particle_syllable_is_preserved(self):
        constraints = extract_query_constraints("담당자 이지은 최근 이슈를 보여줘")
        self.assertEqual(constraints["people"]["names"], ["이지은"])

    def test_explicit_hana_customer_is_not_removed(self):
        self.assertEqual(extract_query_constraints("하나 이슈 보여줘")["customer_key"], "하나")
        self.assertEqual(
            extract_query_constraints("하나금융 최근 이슈 하나만 보여줘")["customer_key"],
            "하나금융",
        )

    def test_korean_count_words_are_read_as_a_display_limit(self):
        for question, expected in (
            ("가장 최근 이슈 하나만", 1),
            ("가장 최근 이슈 하나", 1),
            ("최근 이슈 한 건만 보여줘", 1),
            ("최근 이슈 두 개만 보여줘", 2),
            ("최근 이슈 세건 보여줘", 3),
            ("최근 이슈 열 개 보여줘", 10),
        ):
            self.assertEqual(resolve_result_limit(question, 5), expected, msg=question)

    def test_period_and_grouping_expressions_are_not_display_limits(self):
        # "3개월"의 3개, "하나씩"의 묶음 단위, 고객사 앞글자는 표시 개수가 아니다.
        for question in (
            "최근 3개월 이슈 보여줘",
            "고객사별 최근 이슈를 하나씩 보여줘",
            "하나증권 이슈 보여줘",
            "한국투자증권 이슈 보여줘",
        ):
            self.assertIsNone(extract_requested_limit(question), msg=question)
            self.assertEqual(resolve_result_limit(question, 5), 5, msg=question)

    def test_count_expressions_do_not_become_mantis_keywords(self):
        for question in (
            "가장 최근 이슈 하나만",
            "최근 이슈 1개만 보여줘",
            "최근 이슈 1건만 보여줘",
            "최근 이슈 한 건만 보여줘",
            "최근 이슈 두 개만 보여줘",
        ):
            constraints = extract_query_constraints(question)
            self.assertEqual(build_keyword_terms(question, constraints), [], msg=question)

    def test_period_expressions_do_not_leave_an_orphan_keyword(self):
        # "3개월"에서 "3개"만 떼면 "월"이 남아 AND 키워드가 되고, 검색 텍스트의
        # "월"은 날짜 커스텀 필드가 채워진 이슈에만 있어 결과가 조용히 줄어든다.
        for question in (
            "최근 3개월 이슈 보여줘",
            "KB증권 최근 3개월간 미종료 이슈",
            "2주 이내 등록된 이슈",
            "최근 2년 이슈 보여줘",
        ):
            constraints = extract_query_constraints(question)
            self.assertNotIn("월", build_keyword_terms(question, constraints), msg=question)
            self.assertNotIn("월", extract_mantis_keyword(question).split(), msg=question)

    def test_customer_grouping_answer_lists_one_issue_per_customer(self):
        def issue(issue_id, project):
            return {
                "id": issue_id,
                "project": project,
                "summary": f"테스트 이슈 {issue_id}",
                "status": "open",
                "handler": "담당자",
                "created_at": "2026-07-21",
            }

        retrieval = {
            "intent_plan": {"source": "mantis", "group_by": "customer"},
            "stages": [
                {
                    "source": "mantis_page_scan",
                    "status": "ok",
                    "issues_list": [
                        issue(300, "고객사A"),
                        issue(200, "고객사A"),
                        issue(250, "고객사B"),
                    ],
                    "meta": {"found_count": 3},
                }
            ],
        }
        question = "고객사별 최근 이슈를 하나씩 보여줘"

        answer = format_deterministic_issue_answer(
            question,
            extract_query_constraints(question),
            retrieval,
            800,
        )

        self.assertIn("고객사 2곳의 최신 이슈", answer)
        self.assertIn("고객사별 최신 이슈 1건", answer)
        self.assertIn("**ID**: 300", answer)
        self.assertIn("**ID**: 250", answer)
        self.assertNotIn("**ID**: 200", answer)

    @patch("backend.reports.evaluation.post_model_chat")
    @patch("backend.reports.evaluation.get_private_model")
    def test_candidate_model_ab_uses_same_grounded_retrieval(self, get_model, post_chat):
        get_model.return_value = {
            "id": "candidate-model",
            "model": "candidate-model",
            "provider": "ollama",
        }
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "choices": [{"message": {"content": "동일 근거 기반 후보 답변"}}]
        }
        post_chat.return_value = response
        request = ChatRequest(
            modelId="current-model",
            messages=[ChatMessage(role="user", content="질문")],
        )
        retrieval = {
            "query": "질문",
            "constraints": {"filter_clauses": []},
            "stages": [],
        }

        result = generate_candidate_model_answer(
            request,
            "질문",
            retrieval,
            "candidate-model",
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["modelId"], "candidate-model")
        payload = post_chat.call_args.args[2]
        self.assertIn("검증된 검색 결과", payload["messages"][0]["content"])

    def test_platform_exclusion_is_not_reversed_to_inclusion(self):
        question = "유진증권 이슈에서 MTS는 빼고 최근 10개 보여줘"
        constraints = extract_query_constraints(question)

        self.assertIsNone(constraints["platform_key"])
        self.assertEqual(constraints["excluded_platform_keys"], ["mts"])
        self.assertIn(
            {"field": "platform", "operator": "not_equals", "value": "mts"},
            constraints["filter_clauses"],
        )

        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        self.assertTrue(validate_query_plan(question, plan)["valid"])

    def test_model_filter_clauses_are_merged_into_plan(self):
        question = "유진증권 이슈에서 MTS는 빼고 최근 10개 보여줘"
        plan = build_query_plan(
            question,
            {"source": "mantis"},
            extract_query_constraints(question),
            {
                "planned": True,
                "source": "mantis",
                "filters": [
                    {"field": "platform", "operator": "not_equals", "value": "MTS"},
                    {"field": "customer", "operator": "equals", "value": "유진증권"},
                ],
            },
        )

        self.assertEqual(plan["version"], 2)
        self.assertTrue(any(clause["operator"] == "not_equals" for clause in plan["filter_clauses"]))

    def test_mantis_adapter_applies_platform_and_keyword_exclusions(self):
        issue = {
            "id": 26206,
            "project": {"name": "104EuGene(유진증권)"},
            "summary": "로그인 비밀번호 오류",
            "description": "로그인 시 비밀번호 검증 오류",
            "status": {"name": "closed"},
            "handler": {"real_name": "이영희"},
            "reporter": {"real_name": "홍길동"},
            "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}],
        }
        constraints = extract_query_constraints(
            "유진증권 이슈 중 MTS는 빼고 제목에 로그인은 있지만 비밀번호는 없는 건"
        )
        self.assertFalse(issue_matches_constraints(issue, constraints))

        issue["custom_fields"][0]["value"] = "HTS"
        self.assertFalse(issue_matches_constraints(issue, constraints))
        issue["summary"] = "로그인 오류"
        issue["description"] = "로그인 실패"
        self.assertTrue(issue_matches_constraints(issue, constraints))

    def test_notion_adapter_applies_negative_status_filter(self):
        page = {
            "title": "주요 이슈",
            "status": "보관됨",
            "owner": "석지원",
            "database_label": "주요이슈현황",
            "created_time": "2026-07-01",
            "search_text": "상태: 보관됨",
            "content": "",
        }
        constraints = {
            "excluded_statuses": ["보관됨"],
            "common_filter_clauses": [
                {"field": "status", "operator": "not_equals", "value": "보관됨"}
            ],
        }
        self.assertFalse(notion_page_matches_constraints(page, constraints))
        page["status"] = "진행중"
        page["search_text"] = "상태: 진행중"
        self.assertTrue(notion_page_matches_constraints(page, constraints))

    def test_follow_up_inherits_excluded_platform_as_structured_intent(self):
        messages = [
            ChatMessage(role="user", content="유진증권 이슈 보여줘"),
            ChatMessage(role="assistant", content="결과입니다"),
            ChatMessage(role="user", content="MTS는 빼줘"),
            ChatMessage(role="assistant", content="제외했습니다"),
            ChatMessage(role="user", content="최근 5개만"),
        ]
        retrieval_text, needs_rewrite = build_contextual_retrieval_text(messages)
        inherited = extract_query_constraints(retrieval_text)

        self.assertFalse(needs_rewrite)
        self.assertIn("유진증권", inherited["customer_aliases"])
        self.assertEqual(inherited["excluded_platform_keys"], ["mts"])

    def test_excluded_constraints_are_visible_in_verification_chips(self):
        constraints = extract_query_constraints("유진증권 이슈에서 MTS는 빼고 보여줘")
        chips = display_constraints(constraints)
        platform_chip = next(chip for chip in chips if chip["id"] == "platform_exclude")
        self.assertEqual(platform_chip["value"], "MTS")

    def test_source_names_are_not_treated_as_customer(self):
        # 프로젝트 목록의 999Mantis 때문에 "Mantis에서 찾아줘"의 소스 이름이
        # 고객사로 잡히고, 별칭이 길어 진짜 고객사까지 밀어내던 회귀를 막는다.
        self.assertIsNone(
            extract_query_constraints("Mantis와 Notion에서 로그인 장애 관련 내용을 함께 찾아줘")["customer_key"]
        )
        self.assertEqual(
            extract_query_constraints("KB증권 관련 내용을 Mantis와 Notion에서 각각 3개씩 보여줘")["customer_key"],
            "KB증권",
        )

    def test_explicit_mantis_project_is_still_a_customer(self):
        self.assertEqual(
            extract_query_constraints("999Mantis 프로젝트 이슈 보여줘")["customer_key"],
            "999Mantis",
        )

    def test_particle_suffixed_nouns_are_not_treated_as_people(self):
        # 후속 질문의 "그 이슈의 담당자", "제목과 담당자로"에서 앞 명사가
        # 담당자 이름으로 잡혀 검색이 0건이 되던 회귀를 막는다.
        self.assertEqual(
            extract_query_constraints("그 이슈의 담당자와 상태만 다시 알려줘")["people"]["names"],
            [],
        )
        self.assertEqual(
            extract_query_constraints("최근 3개만 제목과 담당자로 정리해줘")["people"]["names"],
            [],
        )

    def test_real_handler_names_are_still_extracted(self):
        self.assertEqual(
            extract_query_constraints("담당자가 DRFN_NEXT 인 이슈 보여줘")["people"]["names"],
            ["DRFN_NEXT"],
        )
        self.assertIn(
            "권한별",
            extract_query_constraints("권한별 담당 이슈 보여줘")["people"]["names"],
        )

    def test_status_chips_are_built_without_positive_status_filter(self):
        # 제외 상태만 있는 질문("종료된 건은 빼줘")에서 상태 칩을 만들다
        # status_values가 정의되지 않아 500이 나던 회귀를 막는다.
        chips = display_constraints({"excluded_statuses": ["closed"]})
        exclude_chip = next(chip for chip in chips if chip["id"] == "status_exclude")
        self.assertEqual(exclude_chip["value"], "closed")
        self.assertEqual([chip for chip in chips if chip["id"] == "status"], [])

    def test_status_chip_is_built_without_exclusion(self):
        # 긍정 상태만 있을 때도 상태 칩이 노출돼야 한다(기존에는 누락됐다).
        chips = display_constraints({"statuses": ["open"]})
        status_chip = next(chip for chip in chips if chip["id"] == "status")
        self.assertEqual(status_chip["value"], "open")

    def test_model_created_at_is_dropped_without_temporal_expression(self):
        # "최근"만 있는 질문에서 플래너가 지어낸 날짜 필터는 버려야 한다.
        # (2026-07-09 정확도 점검: "담당자 OO의 최근 이슈 2개"가 최근 7일로 오해석돼 빈 결과)
        question = "담당자 권한별의 최근 이슈 2개 보여줘"
        self.assertFalse(question_has_temporal_expression(question))
        plan = build_query_plan(
            question,
            {"source": "mantis", "confidence": 0.98},
            {},
            {
                "planned": True,
                "source": "mantis",
                "operation": "search",
                "filters": {"created_at": {"from": "2026-07-02", "to": "2026-07-09"}},
            },
        )
        self.assertIsNone(plan["filters"]["created_at"])

    def test_model_created_at_is_kept_with_temporal_expression(self):
        # 시점 표현이 실제로 있으면 규칙이 못 잡아도 모델 날짜를 수용한다.
        question = "최근 30일 신한 이슈 보여줘"
        self.assertTrue(question_has_temporal_expression(question))
        self.assertTrue(question_has_temporal_expression("작년 하나증권 이슈"))
        self.assertTrue(question_has_temporal_expression("2025년 12월 이슈 몇 개야"))
        self.assertFalse(question_has_temporal_expression("최근 이슈 보여줘"))
        self.assertFalse(question_has_temporal_expression("최신 이슈 5개"))
    def test_year_and_count_are_structured(self):
        question = "2025년 신한 이슈 몇개야?"
        plan = build_query_plan(
            question,
            {"source": "mantis", "confidence": 0.98},
            {"customer_key": "신한"},
        )
        self.assertEqual(plan["operation"], "count")
        self.assertEqual(
            plan["filters"]["created_at"],
            {"from": "2025-01-01", "to": "2025-12-31", "precision": "year"},
        )
        self.assertTrue(validate_query_plan(question, plan)["valid"])

    def test_month_range_uses_last_calendar_day(self):
        self.assertEqual(
            extract_created_at_range("2024년 2월 신한 이슈"),
            {"from": "2024-02-01", "to": "2024-02-29", "precision": "month"},
        )

    def test_day_level_range_spans_year_boundary(self):
        # 일 단위 구간이 월 규칙에 걸려 12월 한 달로 뭉개지던 회귀를 막는다.
        self.assertEqual(
            extract_created_at_range("2025년 12월 31일부터 2026년 1월 2일까지 등록된 이슈 보여줘"),
            {"from": "2025-12-31", "to": "2026-01-02", "precision": "day_range"},
        )

    def test_day_level_range_reuses_start_month_when_omitted(self):
        self.assertEqual(
            extract_created_at_range("2026년 2월 1일부터 10일까지 이슈"),
            {"from": "2026-02-01", "to": "2026-02-10", "precision": "day_range"},
        )

    def test_invalid_month_is_not_silently_widened_to_the_year(self):
        # "2026년 13월"이 조용히 2026년 전체로 바뀌면 사용자가 결과를 오해한다.
        self.assertIsNone(extract_created_at_range("2026년 13월 이슈 보여줘"))
        self.assertEqual(
            extract_created_at_range("2026년 12월 이슈"),
            {"from": "2026-12-01", "to": "2026-12-31", "precision": "month"},
        )

    def test_registration_month_without_year_uses_current_year(self):
        year = date.today().year
        self.assertEqual(
            extract_created_at_range("9월 등록된 항목"),
            {"from": f"{year}-09-01", "to": f"{year}-09-30", "precision": "month"},
        )
        self.assertIsNone(extract_created_at_range("9월 사용자 등록 방법 문서"))
        self.assertIsNone(extract_created_at_range("9월 마감인 작업 알려줘"))
        self.assertIsNone(extract_created_at_range("9월 운영 반영 항목 알려줘"))

        notion_due = build_query_plan("노션에서 9월 마감인 작업 알려줘", {"source": "notion"})
        self.assertIn(
            {"field": "due_at", "operator": "between", "value": [f"{year}-09-01", f"{year}-09-30"]},
            notion_due["filter_clauses"],
        )
        mantis_due = build_query_plan("9월 마감인 이슈 알려줘", {"source": "mantis"})
        self.assertFalse(any(clause["field"] == "created_at" for clause in mantis_due["filter_clauses"]))

        for question in (
            "노션 마감 임박 작업 중 3월 프로젝트 찾아줘",
            "노션에서 마감 지난 3월호 자료 정리해줘",
            "노션 운영 반영 목록에서 2월 신규기능 문서 찾아줘",
        ):
            plan = build_query_plan(question, {"source": "notion"})
            self.assertFalse(
                any(clause["field"] in {"due_at", "deployed_at"} for clause in plan["filter_clauses"]),
                question,
            )
            model_plan = build_query_plan(
                question,
                {"source": "notion"},
                model_plan={"filters": {"created_at": {
                    "from": f"{year}-03-01", "to": f"{year}-03-31",
                }}},
            )
            self.assertFalse(
                any(clause["field"] in {"due_at", "deployed_at"} for clause in model_plan["filter_clauses"]),
                question,
            )

        for question, field in (
            ("노션에서 3월 마감 작업 알려줘", "due_at"),
            ("노션에서 마감 3월까지인 작업 알려줘", "due_at"),
            ("노션에서 2월 운영 반영 문서 알려줘", "deployed_at"),
        ):
            plan = build_query_plan(question, {"source": "notion"})
            self.assertTrue(any(clause["field"] == field for clause in plan["filter_clauses"]), question)

    def test_notion_temporal_field_requires_a_complete_field_name(self):
        for question in (
            "노션에서 어제 올라온 배포일정 문서 찾아줘",
            "노션에서 어제 작성된 마감일정 문서 찾아줘",
            "노션 기한 없는 항목 정리해줘",
            "노션 기한이 없는 항목 정리해줘",
            "노션 배포일 미지정 항목 보여줘",
        ):
            self.assertEqual(notion_temporal_field(question), "created_at", question)

        plan = build_query_plan(
            "노션에서 어제 올라온 배포일정 문서 찾아줘",
            {"source": "notion"},
        )
        self.assertIsNotNone(plan["filters"]["created_at"])
        self.assertFalse(
            any(clause["field"] == "deployed_at" for clause in plan["filter_clauses"]),
        )
        date_check = next(
            check for check in validate_query_plan(
                "노션에서 어제 올라온 배포일정 문서 찾아줘", plan,
            )["checks"]
            if check["id"] == "date_intent"
        )
        self.assertEqual(date_check["label"], "등록일 기간 해석")
        self.assertEqual(date_check["status"], "pass")

    def test_notion_temporal_field_keeps_particles_and_predicate_endings(self):
        for question in (
            "노션에서 9월 마감인 작업 알려줘",
            "노션에서 어제 마감된 항목 알려줘",
            "노션에서 기한이 오늘인 항목 알려줘",
        ):
            self.assertEqual(notion_temporal_field(question), "due_at", question)
        for question in (
            "노션에서 2월 운영 반영된 문서 알려줘",
            "노션에서 배포일이 어제인 문서 알려줘",
        ):
            self.assertEqual(notion_temporal_field(question), "deployed_at", question)

    def test_relative_month_ranges(self):
        base = date(2026, 7, 21)
        self.assertEqual(
            extract_relative_created_at_range("이번 달 유진증권 이슈를 등록일 순으로 보여줘", base),
            {"from": "2026-07-01", "to": "2026-07-31", "precision": "this_month"},
        )
        self.assertEqual(
            extract_relative_created_at_range("지난달 KB증권 이슈는 몇 건이었어?", base),
            {"from": "2026-06-01", "to": "2026-06-30", "precision": "last_month"},
        )

    def test_last_month_crosses_year_boundary(self):
        # 연초에도 이전 달을 전년 12월로 계산해야 한다.
        self.assertEqual(
            extract_relative_created_at_range("지난달 이슈", date(2026, 1, 15)),
            {"from": "2025-12-01", "to": "2025-12-31", "precision": "last_month"},
        )

    def test_recent_months_range(self):
        self.assertEqual(
            extract_relative_created_at_range("최근 3개월 이슈", date(2026, 1, 15)),
            {"from": "2025-11-01", "to": "2026-01-15", "precision": "recent_months"},
        )

    def test_invalid_month_also_rejects_model_supplied_range(self):
        # "2026년 13월"은 연도 때문에 시점 표현으로 잡혀서, 규칙이 해석을
        # 거부해도 모델이 지어낸 범위가 그대로 통과하던 구멍을 막는다.
        plan = build_query_plan(
            "2026년 13월 이슈 보여줘",
            {"source": "mantis", "confidence": 0.9},
            {},
            {
                "planned": True,
                "source": "mantis",
                "operation": "search",
                "filters": {"created_at": {"from": "2025-12-01", "to": "2027-01-31"}},
            },
        )
        self.assertIsNone(plan["filters"]["created_at"])

    def test_model_status_filter_is_dropped_without_status_mention(self):
        # "최근 이슈 5개" 질문에 모델이 종료 제외를 붙여 5개가 아닌 2건만
        # 나가던 회귀.
        plan = build_query_plan(
            "최근 이슈 5개를 ID, 고객사, 상태, 담당자 표로 만들어줘",
            {"source": "mantis", "confidence": 0.9},
            extract_query_constraints("최근 이슈 5개를 ID, 고객사, 상태, 담당자 표로 만들어줘"),
            {
                "planned": True,
                "source": "mantis",
                "operation": "search",
                "filter_clauses": [{"field": "status", "operator": "not_equals", "value": "closed"}],
            },
        )
        self.assertEqual([c for c in plan["filter_clauses"] if c["field"] == "status"], [])

    def test_status_filter_is_kept_when_the_question_mentions_status(self):
        question = "종료되지 않은 최근 이슈 보여줘"
        plan = build_query_plan(
            question,
            {"source": "mantis", "confidence": 0.9},
            extract_query_constraints(question),
            {
                "planned": True,
                "source": "mantis",
                "operation": "search",
                "filter_clauses": [{"field": "status", "operator": "not_equals", "value": "closed"}],
            },
        )
        self.assertTrue([c for c in plan["filter_clauses"] if c["field"] == "status"])

    def test_model_clause_cannot_exclude_the_requested_platform(self):
        # 플랫폼=MTS인데 모델이 플랫폼 제외=MTS를 덧붙이면 결과가 항상 0건이 된다.
        constraints = extract_query_constraints("신한 mts 종료된 건은 빼줘")
        plan = build_query_plan(
            "신한 mts 종료된 건은 빼줘",
            {"source": "mantis", "confidence": 0.9},
            constraints,
            {
                "planned": True,
                "source": "mantis",
                "operation": "search",
                "filter_clauses": [
                    {"field": "platform", "operator": "not_equals", "value": "mts"},
                    {"field": "status", "operator": "not_equals", "value": "closed"},
                ],
            },
        )
        self.assertEqual(plan["filters"]["platform"], "mts")
        self.assertNotIn(
            {"field": "platform", "operator": "not_equals", "value": "mts"},
            plan["filter_clauses"],
        )
        # 사용자가 실제로 요청한 상태 제외는 그대로 남아야 한다.
        self.assertIn(
            {"field": "status", "operator": "not_equals", "value": "closed"},
            plan["filter_clauses"],
        )

    def test_count_and_requested_list_are_distinct(self):
        self.assertEqual(infer_operation("신한 이슈 몇 개야?"), "count")
        self.assertEqual(infer_operation("신한 이슈 5개만 보여줘"), "search")
        self.assertEqual(infer_operation("2026년 2월 석지원의 공수를 알고 싶어"), "aggregate")
        self.assertEqual(infer_operation("#30967과 비슷한 과거 이슈 찾아줘"), "search")
        self.assertEqual(infer_operation("#30967 이슈 내용을 알려줘"), "get")

    def test_grouping_is_preserved_in_query_plan(self):
        question = "노션에서 결재요청 상태인 항목을 담당자별로 정리해줘"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "notion"}, constraints)
        self.assertEqual(plan["group_by"], "owner")
        self.assertEqual(
            plan["filters"]["people"],
            {"names": [], "role": None, "name_origins": {}, "candidates": []},
        )

    def test_effort_query_uses_detected_person_when_date_separates_words(self):
        self.assertEqual(
            extract_effort_query("석지원의 2025년 7월 공수 합 알려줘"),
            {"person": "석지원", "year": 2025, "month": 7},
        )

    def test_effort_query_accepts_year_without_month(self):
        self.assertEqual(
            extract_effort_query("석지원의 올해 공수 합 알려줘"),
            {"person": "석지원", "year": date.today().year, "period": "year"},
        )

    def test_effort_query_accepts_customer_and_platform_without_person(self):
        query = extract_effort_query("작년 NH MTS 공수총합 알려줘")

        self.assertEqual(query["scope"], "filters")
        self.assertEqual(query["year"], date.today().year - 1)
        self.assertEqual(query["period"], "year")
        self.assertEqual(query["constraints"]["customer_key"], "NH")
        self.assertEqual(query["constraints"]["platform_key"], "mts")

    def test_invalid_model_operation_and_year_are_repaired(self):
        plan = build_query_plan(
            "2025년 신한 이슈 5개 보여줘",
            {"source": "mantis"},
            {"customer_key": "신한"},
            {
                "planned": True,
                "source": "mantis",
                "operation": "count",
                "filters": {"created_at": {"from": "2026-01-01", "to": "2026-12-31"}},
            },
        )
        self.assertEqual(plan["operation"], "search")
        self.assertEqual(plan["filters"]["created_at"]["from"], "2025-01-01")

    def test_validation_rejects_missing_year(self):
        plan = build_query_plan("신한 이슈 보여줘", {"source": "mantis"}, {"customer_key": "신한"})
        result = validate_query_plan("2025년 신한 이슈 보여줘", plan)
        self.assertFalse(result["valid"])
        self.assertEqual(result["checks"][0]["id"], "date_intent")

    def test_created_at_range_matching(self):
        date_range = {"from": "2025-01-01", "to": "2025-12-31"}
        self.assertTrue(plan_created_at_matches("2025-10-23", date_range))
        self.assertFalse(plan_created_at_matches("2026-01-21", date_range))

    def test_issue_constraint_applies_registration_year(self):
        constraints = extract_query_constraints("2025년에 등록된 신한 이슈")
        self.assertTrue(issue_matches_constraints(self.issue(1, "2025-10-23"), constraints))
        self.assertFalse(issue_matches_constraints(self.issue(2, "2026-01-21"), constraints))

    @patch(
        "backend.reports.aggregate.collect_mantis_live_ids_in_range",
        return_value={"status": "ok", "issues": {}, "reason": "", "scanned_pages": 8, "complete": True},
    )
    @patch("backend.reports.aggregate.scan_mantis_documents")
    def test_count_report_counts_all_matches_not_top_five(self, scan_documents, _live):
        # 날짜 조건이 있으면 스냅샷 matched_ids와 라이브 결과를 합집합한다.
        # 라이브는 빈 결과로 목킹해 스냅샷 2건이 그대로 유지되는지 검증한다.
        scan_documents.return_value = {
            "available": True,
            "scanned_count": 4,
            "matched_count": 2,
            "matched_ids": {3, 2},
            "issues": [
                self.normalized_issue(3, "2025-12-22"),
                self.normalized_issue(2, "2025-10-23"),
            ],
            "reason": "",
        }
        question = "2025년 신한 이슈 몇개야?"
        plan = build_query_plan(
            question,
            {"source": "mantis", "confidence": 0.98},
            extract_query_constraints(question),
        )
        report = build_mantis_count_report(question, plan)
        self.assertIsNotNone(report)
        self.assertIn("총 2건입니다.", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["total_count"], 2)
        self.assertEqual([source["issue_id"] for source in report["sources"]], [3, 2])

        verification = build_verification_report(
            question,
            report["content"],
            report["sources"],
            report["retrieval"],
            "deterministic_mantis_count",
        )
        self.assertEqual(verification["score"], 100)
        self.assertEqual(verification["status"], "pass")

    @patch("backend.reports.aggregate.scan_mantis_documents")
    def test_month_count_merges_live_when_snapshot_misses_dates(self, scan_documents):
        # 옛 포맷 스냅샷은 등록일이 없어 월 필터에서 0으로 샌다(matched_ids 비어 있음).
        # 라이브에서 실제 등록일로 매칭된 2건이 합집합돼 총 2건으로 보완돼야 한다.
        scan_documents.return_value = {
            "available": True,
            "scanned_count": 30000,
            "matched_count": 0,
            "matched_ids": set(),
            "issues": [],
            "reason": "",
        }
        live = {
            "30411": {"id": 30411, "summary": "CAPE 2025.12 서버정기점검",
                      "project": {"name": "146케이프증권(CAPE증권)"},
                      "status": {"name": "종료된 이슈"}, "created_at": "2025-12-29"},
            "30392": {"id": 30392, "summary": "CAPE 2025.12 정기점검",
                      "project": {"name": "146케이프증권(CAPE증권)"},
                      "status": {"name": "종료된 이슈"}, "created_at": "2025-12-22"},
        }
        question = "최신 800건 기준으로 2025년 12월에 CAPE증권에서 들어온 이슈는 몇 개야?"
        plan = build_query_plan(
            question,
            {"source": "mantis", "planner": "rule", "confidence": 1.0},
            extract_query_constraints(question),
            {"planned": True, "source": "mantis", "operation": "count", "reason": "월별"},
        )
        with patch(
            "backend.reports.aggregate.collect_mantis_live_ids_in_range",
            return_value={"status": "ok", "issues": live, "reason": "", "scanned_pages": 8, "complete": True},
        ):
            report = build_mantis_count_report(question, plan)
        self.assertIsNotNone(report)
        self.assertIn("총 2건입니다.", report["content"])
        self.assertIn("실시간 최신 800건 기준", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["total_count"], 2)

    @patch("backend.reports.aggregate.scan_mantis_documents")
    def test_month_count_uses_only_live_window_when_latest_snapshot_is_explicit(self, scan_documents):
        # "최신 800건 기준"에서는 로컬 스냅샷의 과거 이슈를 합치면 안 된다.
        # 스냅샷 {3,2}, 라이브 최신 창 {2,9}라면 답은 라이브 창의 2건이다.
        scan_documents.return_value = {
            "available": True,
            "scanned_count": 30000,
            "matched_count": 2,
            "matched_ids": {3, 2},
            "issues": [self.normalized_issue(3, "2025-12-22"), self.normalized_issue(2, "2025-12-10")],
            "reason": "",
        }
        live = {
            "2": {"id": 2, "summary": "dup", "project": {"name": "132SHGM(신한)"},
                  "status": {"name": "종료된 이슈"}, "created_at": "2025-12-10"},
            "9": {"id": 9, "summary": "live-only", "project": {"name": "132SHGM(신한)"},
                  "status": {"name": "종료된 이슈"}, "created_at": "2025-12-28"},
        }
        question = "최신 800건 기준으로 2025년 12월 신한 이슈 몇개야?"
        plan = build_query_plan(
            question,
            {"source": "mantis", "planner": "rule", "confidence": 1.0},
            extract_query_constraints(question),
            {"planned": True, "source": "mantis", "operation": "count", "reason": "월별"},
        )
        with patch(
            "backend.reports.aggregate.collect_mantis_live_ids_in_range",
            return_value={"status": "ok", "issues": live, "reason": "", "scanned_pages": 8, "complete": True},
        ):
            report = build_mantis_count_report(question, plan)
        self.assertIn("총 2건입니다.", report["content"])
        self.assertIn("실시간 최신 800건 기준", report["content"])

    @patch("backend.reports.aggregate.scan_mantis_documents")
    def test_explicit_month_count_reports_partial_error_when_live_query_fails(self, scan_documents):
        scan_documents.return_value = {
            "available": True,
            "scanned_count": 800,
            "matched_count": 5,
            "matched_ids": {1, 2, 3, 4, 5},
            "issues": [self.normalized_issue(5, "2026-08-10")],
            "reason": "",
        }
        question = "최신 800건 기준으로 2026년 8월 신한 이슈 몇개야?"
        plan = build_query_plan(
            question,
            {"source": "mantis", "planner": "rule", "confidence": 1.0},
            extract_query_constraints(question),
            {"planned": True, "source": "mantis", "operation": "count", "reason": "월별"},
        )
        with patch(
            "backend.reports.aggregate.collect_mantis_live_ids_in_range",
            return_value={
                "status": "error",
                "issues": {},
                "reason": "Mantis live count query failed: ReadTimeout",
                "scanned_pages": 0,
            },
        ):
            report = build_mantis_count_report(question, plan)

        stage = report["retrieval"]["stages"][0]
        self.assertEqual(stage["status"], "partial_error")
        self.assertEqual(stage["error_kind"], "dependency")
        self.assertFalse(stage["meta"]["scan_complete"])
        self.assertIn("정확한 건수를 확정할 수 없습니다", report["content"])
        self.assertNotIn("총 5건입니다", report["content"])

    def test_audit_rejects_legacy_plan_that_omits_year_and_count(self):
        question = "2025년 신한 이슈 몇개야?"
        source = {
            "type": "mantis",
            "issue_id": 30943,
            "title": "테스트",
            "project": "132SHGM(신한굿모닝증권)",
            "created_at": "2026-04-27",
        }
        retrieval = {
            "constraints": extract_query_constraints("신한 이슈"),
            "intent_plan": {"source": "mantis", "terms": []},
            "stages": [{"source": "chroma_db", "status": "ok"}],
        }
        verification = build_verification_report(
            question,
            "신한 관련 최신 이슈 5건입니다. #30943",
            [source],
            retrieval,
            "deterministic_issue_list",
        )
        self.assertLess(verification["score"], 100)
        self.assertEqual(verification["status"], "fail")
        intent = next(item for item in verification["dimensions"] if item["id"] == "intent")
        self.assertEqual(intent["status"], "fail")

    @patch("backend.retrieval.execute_chroma_search")
    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_snapshot_search")
    def test_retrieval_pipeline_executes_structured_date_filter(self, snapshot_search, page_scan, chroma_search):
        snapshot_search.return_value = {"source": "mantis_snapshot", "status": "ok", "issues_list": []}
        question = "2025년에 등록된 신한 MTS 이슈 5개 보여줘"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        result = run_retrieval_pipeline(
            question,
            5,
            {"mantis": True, "notion": False, "vectorDb": True},
            intent_plan=plan,
        )
        self.assertEqual(result["constraints"]["created_at"]["from"], "2025-01-01")
        self.assertEqual(result["query_plan"]["operation"], "search")
        passed_constraints = snapshot_search.call_args.args[1]
        self.assertEqual(passed_constraints["created_at"]["to"], "2025-12-31")
        page_scan.assert_not_called()
        chroma_search.assert_not_called()

    @patch("backend.retrieval.execute_mantis_filter_search")
    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_snapshot_search")
    def test_structured_customer_query_uses_snapshot_tool(
        self, snapshot_search, page_scan, filter_search
    ):
        snapshot_search.return_value = {"source": "mantis_snapshot", "status": "ok", "issues_list": []}
        page_scan.return_value = {"source": "mantis_page_scan", "status": "ok", "issues_list": []}
        filter_search.return_value = {"source": "mantis_filter", "status": "ok", "issues_list": []}
        question = "신한 MTS 최근 이슈 5개 보여줘"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        run_retrieval_pipeline(
            question,
            5,
            {"mantis": True, "notion": False, "vectorDb": False},
            intent_plan=plan,
        )
        snapshot_search.assert_called_once()

    @patch("backend.retrieval.execute_mantis_filter_search")
    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_snapshot_search")
    @patch("backend.retrieval.execute_chroma_search")
    @patch("backend.retrieval.get_chroma_document")
    @patch("backend.sources.mantis.fetch_mantis_issue_by_id")
    def test_similar_issue_query_uses_reference_issue_for_chroma(
        self,
        fetch_issue,
        get_document,
        chroma_search,
        snapshot_search,
        page_scan,
        filter_search,
    ):
        get_document.return_value = {
            "issue_id": 30967,
            "content": """[Mantis 이슈 정보]
- 이슈 ID: #30967
- 프로젝트(고객사): 146케이프증권(CAPE증권)
- 현재 상태: assigned
- 담당자: 석지원
- 등록일: 2026-04-01
- 매체구분: HTS
- 화면번호: 7600

[제목]
[7600] 차트 지표 실시간 미갱신 현상

[고객사 본문 요청 내용]
차트 지표가 실시간으로 갱신되지 않는 현상""",
        }
        chroma_search.return_value = {
            "source": "chroma_db",
            "status": "ok",
            "documents": [{"issue_id": 30812, "content": "이슈 ID: #30812\n[제목] 차트 지표 미갱신"}],
            "meta": {"displayed_count": 1},
        }

        question = "#30967과 비슷한 과거 이슈 찾아줘"
        plan = build_query_plan(question, {"source": "mantis"}, extract_query_constraints(question))
        result = run_retrieval_pipeline(
            question,
            5,
            {"mantis": True, "notion": False, "vectorDb": True},
            intent_plan=plan,
        )

        self.assertEqual([stage["source"] for stage in result["stages"]], ["mantis_id_lookup_reference", "chroma_similar"])
        self.assertEqual(result["stages"][0]["issues_list"], [])
        self.assertEqual(result["stages"][0]["meta"]["local_reference_count"], 1)
        fetch_issue.assert_not_called()
        similar_constraints = chroma_search.call_args.args[2]
        self.assertEqual(similar_constraints["issue_ids"], [])
        self.assertEqual(similar_constraints["exclude_issue_ids"], [30967])
        self.assertEqual(similar_constraints["max_issue_id_exclusive"], 30967)
        self.assertFalse(
            any(
                clause.get("field") == "issue_id"
                for clause in similar_constraints["filter_clauses"]
            )
        )
        self.assertIn("차트 지표", chroma_search.call_args.args[0])
        self.assertTrue(chroma_search.call_args.kwargs["semantic"])
        snapshot_search.assert_not_called()
        page_scan.assert_not_called()
        filter_search.assert_not_called()

    def test_similar_issue_search_text_prioritizes_reusable_terms(self):
        issue = {
            "id": 30288,
            "project": "120NH(NH증권)",
            "summary": "NH조건검색 서버 자원사용률이 100%임박 상황",
            "media_type": "HTS",
            "screen_number": "미상",
            "description": "NH조건검색 서버 자원사용률이 100%임박 상황",
        }

        search_text = build_similar_issue_search_text(issue)

        self.assertEqual(search_text, "NH조건검색 서버 자원사용률이 100%임박 상황")
        self.assertNotIn("120NH", search_text)
        self.assertNotIn("NH증권", search_text)

    def test_follow_up_questions_exclude_current_issue_from_more_search(self):
        source = {
            "type": "mantis",
            "issue_id": 30964,
            "project": "117Hantu(한투증권)",
            "title": "[0301] 메모리 관련 검토 및 개선",
            "detail": {"screen_number": "0301"},
        }
        followups = generate_follow_up_questions("한투 최근 이슈", [source], None, "deterministic_issue_list")

        self.assertEqual(followups[0], "#30964 변경 이력이나 참고할 만한 내용을 더 자세히 알려줘")
        self.assertEqual(followups[1], "#30964과 비슷한 과거 이슈 찾아줘")
        self.assertEqual(
            followups[2],
            "117Hantu(한투증권)에서 #30964와 비슷한 과거 메모리 개선 이슈 찾아줘",
        )
        constraints = extract_query_constraints(followups[2])
        self.assertEqual(constraints["issue_ids"], [30964])
        self.assertEqual(constraints["customer_exact"], "117Hantu(한투증권)")

    def test_notion_follow_up_does_not_repeat_current_detail_question(self):
        title = (
            "[VOC][9580] 주식차트 화면 - 지표탭 내 차트유형 카테고리에 "
            "매물차트 유형 추가 요청(520)"
        )
        question = f"노션에서 '{title}' 상세 내용 정리해줘"
        source = {
            "type": "notion",
            "page_id": "page-1",
            "title": title,
            "database_label": "이슈리스트_테스트",
            "status": "종료된이슈",
            "owner": "담당자 미지정",
        }

        followups = generate_follow_up_questions(
            question,
            [source],
            None,
            "model",
        )

        self.assertNotIn(question, followups)
        self.assertEqual(
            followups,
            ["이슈리스트_테스트에서 같은 상태의 항목 더 보여줘"],
        )

    def test_issue_history_follow_up_includes_history_and_notes(self):
        issue = self.normalized_issue(30964, "2026-04-30")
        issue.update(
            {
                "summary": "[0301] 메모리 관련 검토 및 개선",
                "updated_at": "2026-05-06",
                "history": [
                    {
                        "created_at": "2026-05-06T14:34:41",
                        "user": "우동헌",
                        "field": "상태",
                        "message": "상태",
                        "change": "확인된 이슈 => 개발완료",
                    }
                ],
                "notes": [
                    {
                        "created_at": "2026-04-30T09:43:55",
                        "reporter": "우동헌",
                        "text": "이전 이슈 #30883 참고. TimeMeterPDLL 해제 처리 확인",
                    }
                ],
            }
        )
        retrieval = {
            "stages": [{"source": "mantis_id_lookup", "status": "ok", "issues_list": [issue], "meta": {}}]
        }
        question = "#30964 변경 이력이나 참고할 만한 내용을 더 자세히 알려줘"
        answer = format_deterministic_issue_answer(
            question,
            extract_query_constraints(question),
            retrieval,
            5,
        )

        self.assertIn("**변경 이력**", answer)
        self.assertIn("확인된 이슈 => 개발완료", answer)
        self.assertIn("**참고 노트**", answer)
        self.assertIn("#30883", answer)

    def test_similar_issue_answer_uses_base_issue_as_criteria_not_filter(self):
        retrieval = {
            "stages": [
                {
                    "source": "mantis_id_lookup_reference",
                    "status": "ok",
                    "issues_list": [],
                    "meta": {"reference_only": True, "reference_issues": [{"id": 30288}]},
                },
                {
                    "source": "chroma_similar",
                    "status": "ok",
                    "documents": [
                        {
                            "issue_id": 30848,
                            "content": (
                                "[Mantis 이슈 정보]\n"
                                "- 이슈 ID: #30848\n"
                                "- 프로젝트(고객사): 120NH(NH증권)\n"
                                "- 현재 상태: 종료된 이슈\n"
                                "- 담당자: 정희진\n\n"
                                "[제목]\n조건검색 서버 관련 확인 요청\n\n"
                                "[고객사 본문 요청 내용]\n확인 요청"
                            ),
                        }
                    ],
                    "meta": {"base_issue_ids": [30288]},
                },
            ]
        }

        answer = format_deterministic_issue_answer(
            "#30288과 비슷한 과거 이슈 찾아줘",
            extract_query_constraints("#30288과 비슷한 과거 이슈 찾아줘"),
            retrieval,
            5,
        )

        self.assertIn("기준 이슈=#30288", answer)
        self.assertNotIn("이슈 ID=30288", answer)
        self.assertIn("**ID**: 30848", answer)

        report = build_verification_report(
            "#30288과 비슷한 과거 이슈 찾아줘",
            answer,
            [{"type": "chroma", "issue_id": 30848, "title": "조건검색 서버 관련 확인 요청"}],
            retrieval,
            "deterministic_issue_list",
        )
        retrieval_dimension = next(item for item in report["dimensions"] if item["id"] == "retrieval")
        issue_id_check = next(item for item in retrieval_dimension["checks"] if item["id"] == "issue_ids")
        constraints_check = next(item for item in retrieval_dimension["checks"] if item["id"] == "constraints")
        self.assertEqual(issue_id_check["status"], "pass")
        self.assertEqual(constraints_check["status"], "pass")

    def test_similar_issue_answer_preserves_similarity_order(self):
        def document(issue_id, title):
            return {
                "issue_id": issue_id,
                "content": (
                    f"[Mantis 이슈 정보]\n- 이슈 ID: #{issue_id}\n"
                    "- 프로젝트(고객사): 테스트\n- 현재 상태: 종료된 이슈\n\n"
                    f"[제목]\n{title}\n\n[고객사 본문 요청 내용]\n내용"
                ),
            }

        retrieval = {
            "stages": [
                {
                    "source": "mantis_id_lookup_reference",
                    "status": "ok",
                    "issues_list": [],
                    "meta": {"reference_only": True, "displayed_count": 1},
                },
                {
                    "source": "chroma_similar",
                    "status": "ok",
                    "documents": [document(100, "가장 유사"), document(300, "두 번째 유사")],
                    "meta": {"base_issue_ids": [400], "displayed_count": 2},
                }
            ]
        }
        answer = format_deterministic_issue_answer(
            "#400과 비슷한 과거 이슈 찾아줘",
            extract_query_constraints("#400과 비슷한 과거 이슈 찾아줘"),
            retrieval,
            5,
        )

        self.assertLess(answer.index("**ID**: 100"), answer.index("**ID**: 300"))
        self.assertIn("정렬: 의미 유사도 기준", answer)
        self.assertNotIn("총 3건", answer)

    @patch("backend.config.requests.post")
    def test_notion_tool_timeout_budget_caps_retries(self, post):
        post.side_effect = requests.Timeout("timeout")
        with patch.dict("os.environ", {"NOTION_TOOL_TIMEOUT_SECONDS": "7", "NOTION_TOOL_MAX_RETRIES": "0"}):
            with self.assertRaises(requests.Timeout):
                notion_post_json("https://example.invalid", {}, timeout=45, retries=2)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["timeout"], 7)

    @patch("backend.config.requests.post")
    def test_notion_tool_default_budget_retries_timeout_once(self, post):
        success = MagicMock(status_code=200)
        post.side_effect = [requests.Timeout("timeout"), success]
        with patch.dict("os.environ", {}, clear=True):
            response = notion_post_json("https://example.invalid", {}, timeout=45, retries=2)
        self.assertIs(response, success)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["timeout"], 12)

    def test_unscored_model_content_does_not_depress_verified_dimension_score(self):
        report = build_verification_report(
            "안녕하세요",
            "안녕하세요. 무엇을 도와드릴까요?",
            [],
            None,
            "model_generated",
        )
        self.assertEqual(report["score"], 100)
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["completedDimensions"], 1)
        self.assertEqual(report["applicableDimensions"], 2)

    def test_chroma_status_uses_status_field_not_body_words(self):
        document = """[Mantis 이슈 정보]
- 이슈 ID: #30985
- 프로젝트(고객사): 113HMC(HMC증권)
- 현재 상태: 정보 부족

[제목]
확인 요청

[고객사 본문 요청 내용]
원인을 확인해 주세요.
"""
        self.assertFalse(chroma_text_matches_constraints(document, {"statuses": ["confirmed"]}))

    @staticmethod
    def issue(issue_id, created_at, project="132SHGM(신한굿모닝증권)"):
        return {
            "id": issue_id,
            "project": {"name": project},
            "summary": f"테스트 이슈 {issue_id}",
            "status": {"name": "closed"},
            "handler": {"real_name": "석지원"},
            "reporter": {"real_name": "테스터"},
            "created_at": created_at,
            "updated_at": created_at,
            "description": "테스트",
            "custom_fields": [],
        }

    @staticmethod
    def normalized_issue(issue_id, created_at, project="132SHGM(신한굿모닝증권)"):
        return {
            "id": issue_id,
            "project": project,
            "summary": f"테스트 이슈 {issue_id}",
            "status": "closed",
            "handler": "석지원",
            "created_at": created_at,
            "description": "테스트",
            "date_fields": {},
            "effort_fields": {},
        }


if __name__ == "__main__":
    unittest.main()
