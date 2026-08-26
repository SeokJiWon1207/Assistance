"""팀 템플릿 JSON 검증 테스트."""

import json
import tempfile
import unittest
from pathlib import Path

from backend.team_templates import TeamTemplateConfigError, load_team_templates


class TeamTemplateConfigTests(unittest.TestCase):
    def setUp(self):
        load_team_templates.cache_clear()

    def tearDown(self):
        load_team_templates.cache_clear()

    def test_repository_config_is_valid_and_contains_customer_profile(self):
        payload = load_team_templates()

        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["templates"]), 12)
        template_ids = {item["id"] for item in payload["templates"]}
        self.assertIn("customer-profile", template_ids)
        self.assertIn("monthly-customer-report", template_ids)
        self.assertIn("incident-triage", template_ids)
        self.assertIn("issue-handoff", template_ids)
        self.assertIn("incident-review", template_ids)
        self.assertIn("customer-notice", template_ids)

    def test_undefined_question_placeholder_is_rejected(self):
        payload = {
            "version": 1,
            "templates": [{
                "id": "valid-id",
                "category": "분류",
                "title": "제목",
                "description": "설명",
                "icon": "report",
                "questionTemplate": "{missing} 알려줘",
                "fields": [{"name": "customer", "label": "고객사", "type": "text"}],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "templates.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(TeamTemplateConfigError, "정의되지 않은 자리표시자"):
                load_team_templates(path)

    def test_duplicate_template_id_is_rejected(self):
        template = {
            "id": "duplicate-id",
            "category": "분류",
            "title": "제목",
            "description": "설명",
            "icon": "report",
            "questionTemplate": "{customer} 알려줘",
            "fields": [{"name": "customer", "label": "고객사", "type": "text"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "templates.json"
            path.write_text(
                json.dumps({"version": 1, "templates": [template, template]}, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TeamTemplateConfigError, "중복 ID"):
                load_team_templates(path)

    def test_validated_config_is_cached_for_the_process_lifetime(self):
        source = Path(__file__).resolve().parents[1] / "config" / "team_templates.json"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "templates.json"
            path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            first = load_team_templates(path)

            path.write_text("invalid json", encoding="utf-8")
            second = load_team_templates(path)

        self.assertIs(first, second)
