import json
import unittest
from unittest.mock import Mock, patch

from openwebui_tools.mantis_tools import Tools as MantisTools
from openwebui_tools.notion_tools import Tools as NotionTools


class StandaloneMantisToolTests(unittest.TestCase):
    @patch("openwebui_tools.mantis_tools.requests.get")
    def test_search_calls_mantis_directly_with_user_credentials(self, get_mock):
        response = Mock(status_code=200)
        response.json.return_value = {
            "issues": [
                {
                    "id": 10,
                    "project": {"name": "KB증권"},
                    "summary": "로그인 오류",
                    "description": "MTS 로그인 실패",
                    "status": {"name": "확인"},
                    "custom_fields": [
                        {"field": {"name": "매체구분"}, "value": "MTS"}
                    ],
                }
            ]
        }
        get_mock.return_value = response
        tools = MantisTools()
        user = {
            "valves": tools.UserValves(
                MANTIS_API_URL="https://mantis.example/api/rest/issues",
                MANTIS_API_TOKEN="user-token",
            )
        }

        result = json.loads(
            tools.search_mantis(
                search_keyword="로그인",
                project="KB",
                media_type="MTS",
                __user__=user,
            )
        )

        self.assertEqual(result["issues_list"][0]["id"], 10)
        self.assertEqual(get_mock.call_args.args[0], "https://mantis.example/api/rest/issues")
        self.assertEqual(get_mock.call_args.kwargs["headers"]["Authorization"], "user-token")

    @patch("openwebui_tools.mantis_tools.requests.get")
    def test_get_issue_uses_admin_defaults_when_user_valves_are_empty(self, get_mock):
        response = Mock(status_code=200)
        response.json.return_value = {"issues": [{"id": 11, "summary": "테스트"}]}
        get_mock.return_value = response
        tools = MantisTools()
        tools.valves.DEFAULT_MANTIS_API_URL = "https://shared.example/issues"
        tools.valves.DEFAULT_MANTIS_API_TOKEN = "shared-token"

        result = json.loads(tools.get_mantis_issue(11))

        self.assertEqual(result["issue"]["id"], 11)
        self.assertEqual(get_mock.call_args.kwargs["headers"]["Authorization"], "shared-token")


class StandaloneNotionToolTests(unittest.TestCase):
    def test_latest_api_uses_data_source_path(self):
        tools = NotionTools()
        tools.valves.NOTION_API_VERSION = "2026-03-11"
        self.assertEqual(tools._source_path("abc", "/query"), "/data_sources/abc/query")
        self.assertEqual(tools._source_parent("abc")["data_source_id"], "abc")

    def test_legacy_api_uses_database_path(self):
        tools = NotionTools()
        tools.valves.NOTION_API_VERSION = "2022-06-28"
        self.assertEqual(tools._source_path("abc", "/query"), "/databases/abc/query")
        self.assertEqual(tools._source_parent("abc")["database_id"], "abc")

    @patch("openwebui_tools.notion_tools.requests.request")
    def test_search_calls_notion_directly_with_user_credentials(self, request_mock):
        response = Mock(status_code=200)
        response.json.return_value = {
            "results": [
                {
                    "object": "page",
                    "id": "page-1",
                    "last_edited_time": "2026-07-14T00:00:00Z",
                    "properties": {
                        "Name": {"type": "title", "title": [{"plain_text": "휴가 결재"}]},
                        "Status": {"type": "status", "status": {"name": "결재요청"}},
                    },
                }
            ],
            "has_more": False,
        }
        request_mock.return_value = response
        tools = NotionTools()
        user = {
            "valves": tools.UserValves(
                NOTION_API_TOKEN="notion-user-token",
                NOTION_SOURCE_IDS="source-1",
            )
        }

        result = json.loads(
            tools.search_notion("휴가", status="결재요청", __user__=user)
        )

        self.assertEqual(result["notion_pages"][0]["id"], "page-1")
        self.assertIn("/data_sources/source1/query", request_mock.call_args.args[1])
        self.assertEqual(request_mock.call_args.kwargs["headers"]["Authorization"], "Bearer notion-user-token")

    @patch("openwebui_tools.notion_tools.requests.request")
    def test_notion_write_is_disabled_by_default(self, request_mock):
        result = json.loads(
            NotionTools().create_notion_page("source", "테스트", confirmed=True)
        )

        self.assertEqual(result["status"], "blocked")
        request_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
