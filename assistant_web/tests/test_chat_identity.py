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
            "displayName": "석지원",
            "email": "user@example.com",
            "role": "admin",
            "team": "컨텐츠서비스팀",
            "mantisUsername": "석지원",
        }

        with patch.object(
            chat_service_module,
            "finalize_chat_response",
            side_effect=lambda _request, _question, response: response,
        ), patch.object(chat_service_module, "get_private_model") as get_model:
            response = chat_service_module.chat_service.build_response(request, user=user)

        get_model.assert_not_called()
        self.assertIn("석지원님으로 로그인", response["content"])
        self.assertIn("Mantis 담당자: 석지원", response["content"])
        self.assertEqual(response["raw"]["mode"], "authenticated_identity")

    def test_non_identity_question_does_not_match(self):
        self.assertFalse(chat_service_module.is_identity_question("이 이슈 담당자는 누구야?"))


if __name__ == "__main__":
    unittest.main()
