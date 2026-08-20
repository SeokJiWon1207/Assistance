import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
