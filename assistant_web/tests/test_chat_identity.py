import unittest
from unittest.mock import Mock, patch

from backend.schemas import ChatRequest
from backend.services import chat_service as chat_service_module
from backend.understanding import build_keyword_terms


class AuthenticatedIdentityTests(unittest.TestCase):
    def test_identity_question_uses_authenticated_user_without_model_call(self):
        request = ChatRequest(
            modelId="test-model",
            messages=[{"role": "user", "content": "내가 누구야?"}],
        )
        user = {
            "uid": "firebase-user-1",
            "displayName": "석지원",
            "email": "user@example.com",
            "role": "admin",
            "team": "컨텐츠서비스팀",
            "mantisUsername": "석지원",
        }

        with patch.object(
            chat_service_module,
            "persist_chat_response",
            side_effect=lambda _request, _question, response, user_uid="": {
                **response,
                "capturedUserUid": user_uid,
            },
        ), patch.object(chat_service_module, "get_private_model") as get_model:
            response = chat_service_module.chat_service.build_response(request, user=user)

        get_model.assert_not_called()
        self.assertIn("석지원님으로 로그인", response["content"])
        self.assertIn("Mantis 담당자: 석지원", response["content"])
        self.assertEqual(response["raw"]["mode"], "authenticated_identity")
        self.assertEqual(response["capturedUserUid"], "firebase-user-1")

    def test_non_identity_question_does_not_match(self):
        self.assertFalse(chat_service_module.is_identity_question("이 이슈 담당자는 누구야?"))

    def test_weekly_report_is_scoped_to_authenticated_mantis_username(self):
        scoped = chat_service_module.apply_authenticated_mantis_scope(
            "저번주 업무보고서 작성해줘",
            {"mantisUsername": "석지원"},
        )
        constraints = chat_service_module.extract_query_constraints(scoped)

        self.assertIn("담당자: 석지원", scoped)
        self.assertEqual(
            constraints["people"],
            {
                "names": ["석지원"],
                "role": "handler",
                "name_origins": {"석지원": "known"},
                "candidates": [],
            },
        )

    def test_customer_weekly_report_is_not_scoped_to_logged_in_owner(self):
        question = (
            "지난주 미래에셋증권 신규 이슈, 미종료 이슈, 장기 적체, "
            "공수와 처리시간을 주간 보고서 형식으로 작성해줘"
        )
        self.assertFalse(chat_service_module.should_scope_to_authenticated_mantis_user(question))
        self.assertEqual(
            chat_service_module.apply_authenticated_mantis_scope(
                question,
                {"mantisUsername": "정희진"},
            ),
            question,
        )

    def test_personal_issue_variants_are_scoped_to_authenticated_user(self):
        questions = (
            "내 최근 이슈 보여줘",
            "내 미종료 이슈 보여줘",
            "내 장기 미종료 이슈 알려줘",
            "나의 최근 고객사 이슈를 보여줘",
            "제가 맡은 이슈 보여주세요",
            "저에게 할당된 이슈 알려줘",
            "내최근이슈 보여줘",
        )
        for question in questions:
            with self.subTest(question=question):
                scoped = chat_service_module.apply_authenticated_mantis_scope(
                    question,
                    {"mantisUsername": "석지원"},
                )
                constraints = chat_service_module.extract_query_constraints(scoped)

                self.assertEqual(scoped, f"{question} 담당자: 석지원")
                self.assertEqual(
                    constraints["people"],
                    {
                        "names": ["석지원"],
                        "role": "handler",
                        "name_origins": {"석지원": "known"},
                        "candidates": [],
                    },
                )
                self.assertTrue(
                    chat_service_module.should_return_deterministic_issue_list(
                        scoped,
                        constraints,
                    )
                )
                self.assertEqual(build_keyword_terms(scoped, constraints), [])

    def test_personal_issue_answer_uses_handler_criteria_and_fixed_format(self):
        question = "내 최근 이슈 보여줘"
        scoped = chat_service_module.apply_authenticated_mantis_scope(
            question,
            {"mantisUsername": "석지원"},
        )
        constraints = chat_service_module.extract_query_constraints(scoped)
        retrieval = {
            "stages": [
                {
                    "source": "mantis_page_scan",
                    "status": "ok",
                    "issues_list": [
                        {
                            "id": 31379,
                            "project": "138Meritz",
                            "summary": "차트 표시 개선",
                            "status": "acknowledged",
                            "handler": "석지원",
                            "created_at": "2026-08-07",
                        }
                    ],
                    "meta": {"found_count": 1},
                }
            ]
        }

        answer = chat_service_module.format_deterministic_issue_answer(
            scoped,
            constraints,
            retrieval,
            5,
        )

        self.assertIn("담당자 석지원 관련 이슈 1건", answer)
        self.assertIn("검색 기준: 담당자=석지원", answer)
        self.assertIn("**상태**: 이슈 검토", answer)
        self.assertIn("**담당자**: 석지원", answer)
        self.assertNotIn("'내'라는 키워드", answer)

    def test_team_or_similar_words_are_not_scoped_as_personal_requests(self):
        questions = (
            "내 팀 최근 이슈 보여줘",
            "우리 팀 이슈 보여줘",
            "사내 최근 이슈 보여줘",
            "내부 시스템 이슈 보여줘",
            "안내 이슈 확인해줘",
            "노션에서 내 최근 이슈 보여줘",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(
                    chat_service_module.should_scope_to_authenticated_mantis_user(question)
                )

    def test_personal_request_without_mantis_mapping_is_not_rewritten(self):
        question = "내 최근 이슈 보여줘"
        self.assertEqual(
            chat_service_module.apply_authenticated_mantis_scope(
                question,
                {"mantisUsername": ""},
            ),
            question,
        )

    def test_personal_notion_document_uses_authenticated_display_name(self):
        question = "노션에서 나랑 관련된 문서있어?"
        user = {"displayName": "석지원", "mantisUsername": "mantis-id"}

        self.assertTrue(
            chat_service_module.should_scope_to_authenticated_notion_user(question)
        )
        scoped = chat_service_module.apply_authenticated_notion_scope(
            question,
            user,
        )
        self.assertEqual(scoped, f"{question} 담당자: 석지원")
        self.assertEqual(
            chat_service_module.extract_query_constraints(scoped)["people"]["names"],
            ["석지원"],
        )

        request = ChatRequest(
            modelId="test-model",
            messages=[{"role": "user", "content": question}],
        )
        with patch.object(
            chat_service_module,
            "get_private_model",
            return_value={"id": "test-model", "status": "active"},
        ), patch.object(
            chat_service_module,
            "get_model_api_key",
            return_value="test-key",
        ):
            context = chat_service_module.prepare_chat_pipeline(request, question, user)

        self.assertEqual(context.authenticated_owner, "석지원")
        self.assertEqual(context.query_constraints["people"]["names"], ["석지원"])
        self.assertIn("담당자: 석지원", context.retrieval_text)

    def test_personal_notion_scope_does_not_override_named_owner(self):
        question = "노션에서 내 문서 말고 우동헌 담당 문서 보여줘"
        self.assertFalse(
            chat_service_module.should_scope_to_authenticated_notion_user(question)
        )

    def test_explicit_other_person_is_not_overridden_by_login(self):
        with patch.object(
            chat_service_module,
            "extract_query_constraints",
            return_value={"people": {"names": ["김담당"], "role": "handler"}},
        ):
            scoped = chat_service_module.apply_authenticated_mantis_scope(
                "김담당 주간 업무보고서 작성해줘",
                {"mantisUsername": "석지원"},
            )

        self.assertEqual(scoped, "김담당 주간 업무보고서 작성해줘")

    def test_personal_scope_survives_a_follow_up_sort_request(self):
        model = {
            "id": "test-model",
            "status": "active",
            "apiKeyEnv": "TEST_MODEL_KEY",
        }
        user = {
            "uid": "firebase-user-1",
            "displayName": "석지원",
            "mantisUsername": "석지원",
        }

        for follow_up in (
            "오래된 순으로 정렬해줘",
            "정렬해줘",
            "최신순으로 보여줘",
            "더 보여줘",
        ):
            with self.subTest(follow_up=follow_up):
                captured = {}

                def capture_report(
                    latest_user_text,
                    retrieval_text,
                    _enabled_tools,
                    _messages,
                    _user,
                    captured_result=captured,
                ):
                    captured_result["latest_user_text"] = latest_user_text
                    captured_result["retrieval_text"] = retrieval_text
                    return chat_service_module.DeterministicReportResult(
                        response={
                            "content": "테스트 응답",
                            "sources": [],
                            "followUps": [],
                            "raw": {"mode": "test"},
                        }
                    )

                request = ChatRequest(
                    modelId="test-model",
                    contextAuditId="a" * 32,
                    messages=[
                        {"role": "user", "content": "내 MTS 미종료 이슈 보여줘"},
                        {"role": "assistant", "content": "담당 이슈를 보여드렸습니다."},
                        {"role": "user", "content": follow_up},
                    ],
                )
                with patch.object(
                    chat_service_module, "load_audit_query_plan",
                    return_value={"source": "mantis", "limit": 5, "resolved_constraints":
                                  chat_service_module.extract_query_constraints("석지원 담당 MTS 미종료 이슈")},
                ), patch.object(chat_service_module, "load_audit_sources", return_value=[]), patch.object(
                    chat_service_module,
                    "get_private_model",
                    return_value=model,
                ), patch.object(
                    chat_service_module,
                    "get_model_api_key",
                    return_value="test-key",
                ), patch.object(
                    chat_service_module,
                    "build_deterministic_report",
                    side_effect=capture_report,
                ), patch.object(
                    chat_service_module,
                    "persist_chat_response",
                    side_effect=lambda _request, _question, response, user_uid="": response,
                ):
                    chat_service_module.chat_service.build_response(request, user=user)

                self.assertEqual(captured["latest_user_text"], follow_up)
                self.assertIn("담당자: 석지원", captured["retrieval_text"])

    def test_deterministic_report_builder_priority_is_stable(self):
        call_order = []
        builder_names = (
            "build_unknown_customer_report",
            "build_similar_resolution_report",
            "build_monthly_customer_report",
            "build_customer_profile_report",
            "build_sla_report",
            "build_mantis_pivot_report",
            "build_mantis_spike_report",
            "build_top_customers_report",
            "build_effort_trend_report",
            "build_effort_ranking_report",
            "build_effort_report",
        )

        def record(name):
            def builder(*_args, **_kwargs):
                call_order.append(name)
                return None

            return builder

        builders = {
            name: Mock(side_effect=record(name))
            for name in builder_names
        }
        with patch.multiple(chat_service_module, **builders):
            result = chat_service_module.build_deterministic_report(
                "고객사 이슈 현황",
                "고객사 이슈 현황",
                {"mantis": True, "vectorDb": True, "notion": False},
                [],
                None,
            )

        self.assertIsNone(result.response)
        self.assertEqual(call_order, list(builder_names))

    def test_follow_up_for_an_explicit_other_person_does_not_inherit_login(self):
        with patch.object(
            chat_service_module,
            "extract_query_constraints",
            return_value={"people": {"names": ["김담당"], "role": "handler"}},
        ):
            scoped = chat_service_module.apply_authenticated_mantis_scope(
                "김담당 오래된 순으로 정렬해줘",
                {"mantisUsername": "석지원"},
                scope_text="김담당 오래된 순으로 정렬해줘 내 이슈 보여줘",
            )
        self.assertEqual(scoped, "김담당 오래된 순으로 정렬해줘")


if __name__ == "__main__":
    unittest.main()
