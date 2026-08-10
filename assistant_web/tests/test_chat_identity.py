import unittest
from unittest.mock import patch

from backend.schemas import ChatRequest
from backend.services import chat_service as chat_service_module


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
        self.assertEqual(constraints["people"], {"names": ["석지원"], "role": "handler"})

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
