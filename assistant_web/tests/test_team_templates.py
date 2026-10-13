"""팀 템플릿 JSON 검증 테스트."""

import json
import tempfile
import unittest
from pathlib import Path

from datetime import date

from backend.team_templates import (
    TeamTemplateConfigError,
    customer_display_for_project,
    customer_template_options,
    customer_template_search_terms,
    load_team_templates,
    order_customer_options,
    recent_customer_issue_counts,
)


class TeamTemplateConfigTests(unittest.TestCase):
    def setUp(self):
        load_team_templates.cache_clear()

    def tearDown(self):
        load_team_templates.cache_clear()

    def test_repository_config_is_valid_and_contains_customer_profile(self):
        payload = load_team_templates()

        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["templates"]), 13)
        template_ids = {item["id"] for item in payload["templates"]}
        self.assertIn("customer-profile", template_ids)
        self.assertIn("monthly-customer-report", template_ids)
        self.assertIn("incident-triage", template_ids)
        self.assertIn("issue-handoff", template_ids)
        self.assertIn("incident-review", template_ids)
        self.assertIn("customer-notice", template_ids)

    def test_all_customer_fields_share_the_complete_searchable_customer_list(self):
        payload = load_team_templates()
        customer_fields = [
            field
            for template in payload["templates"]
            for field in template["fields"]
            if field["name"] == "customer"
        ]
        expected_options = customer_template_options()

        self.assertEqual(len(customer_fields), 8)
        self.assertEqual(len(expected_options), 33)
        self.assertEqual(len(expected_options), len(set(expected_options)))
        for field in customer_fields:
            self.assertEqual(field["type"], "customer")
            self.assertEqual(field["options"], expected_options)
            self.assertIn("미래에셋증권", field["options"])
            self.assertIn("한국투자증권", field["options"])
            self.assertIn("NH투자증권", field["options"])
            self.assertIn("eBest", field["optionSearchTerms"]["이베스트증권"])
            self.assertIn("101PopcornRenewal", field["optionSearchTerms"]["DB증권"])

    def test_mantis_project_names_map_to_customer_display_names(self):
        self.assertEqual(customer_display_for_project("147MiraeAsset(미래에셋증권)"), "미래에셋증권")
        self.assertEqual(customer_display_for_project("117Hantu_Project"), "한국투자증권")
        self.assertEqual(customer_display_for_project("113HMC(HMC증권)"), "현대차증권")
        self.assertEqual(customer_display_for_project("145SignalMakerON"), "시그널메이커")
        # 짧은 영문 별칭(NH)이 다른 회사 이름 안에서 잘못 잡히지 않고, 내부 프로젝트는 고객사가 아니다.
        self.assertIsNone(customer_display_for_project("164NHFuture"))
        self.assertIsNone(customer_display_for_project("900DRFN(공용)"))

    def test_recent_counts_only_include_issues_inside_the_window(self):
        issues = [
            {"project": {"name": "120NH(NH증권)"}, "created_at": "2026-09-30T10:00:00+09:00"},
            {"project": {"name": "120NH(NH증권)"}, "created_at": "2026-07-06T10:00:00+09:00"},
            {"project": {"name": "120NH(NH증권)"}, "created_at": "2026-07-05T10:00:00+09:00"},
            {"project": {"name": "120NH(NH증권)"}, "created_at": "2027-01-01"},
            {"project": {"name": "120NH(NH증권)"}, "created_at": "잘못된 날짜"},
            {"project": {"name": "117Hantu_Project"}, "created_at": "2026-10-01"},
            {"project": {"name": "900DRFN(공용)"}, "created_at": "2026-10-01"},
            {"project": {}, "created_at": "2026-10-01"},
        ]

        counts = recent_customer_issue_counts(issues, today=date(2026, 10, 4), days=90)

        self.assertEqual(counts, {"NH투자증권": 2, "한국투자증권": 1})

    def test_im_securities_option_and_counts_use_hi_project(self):
        self.assertEqual(customer_display_for_project("163Hi-ib(하이투자증권)"), "아이엠증권")
        self.assertEqual(customer_display_for_project("114Solomon(아이엠증권)"), "IM투자증권")
        terms = customer_template_search_terms()["아이엠증권"]
        for alias in ("하이투자", "IM증권", "163Hi-ib"):
            self.assertIn(alias, terms)
        self.assertNotIn("Solomon", terms)
        counts = recent_customer_issue_counts([
            {"project": {"name": "163Hi-ib(하이투자증권)"}, "created_at": "2026-10-01"},
            {"project": {"name": "114Solomon(아이엠증권)"}, "created_at": "2026-10-01"},
        ], today=date(2026, 10, 7))
        self.assertEqual(counts, {"아이엠증권": 1, "IM투자증권": 1})

    def test_customer_options_put_busy_customers_first_and_keep_config_order_for_ties(self):
        payload = load_team_templates()
        original = [field["options"] for template in payload["templates"] for field in template["fields"] if field["type"] == "customer"][0]

        ordered = order_customer_options(payload, {"케이프증권": 8, "NH투자증권": 31, "미래에셋증권": 45})
        options = [field["options"] for template in ordered["templates"] for field in template["fields"] if field["type"] == "customer"]

        for field_options in options:
            self.assertEqual(field_options[:3], ["미래에셋증권", "NH투자증권", "케이프증권"])
            rest = [name for name in original if name not in {"미래에셋증권", "NH투자증권", "케이프증권"}]
            self.assertEqual(field_options[3:], rest)
        # 프로세스 캐시에 든 원본 설정은 바뀌지 않는다.
        self.assertEqual(
            [field["options"] for template in load_team_templates()["templates"] for field in template["fields"] if field["type"] == "customer"][0],
            original,
        )
        self.assertIs(order_customer_options(payload, {}), payload)

    def test_customer_statistics_distinguish_zero_counts_from_unavailable_data(self):
        payload = load_team_templates()
        for available in (True, False):
            result = order_customer_options(payload, {}, statistics={"available": available, "asOf": "2026-10-06T00:00:00Z"})
            field = next(field for template in result["templates"] for field in template["fields"] if field["type"] == "customer")
            self.assertEqual(field["optionDetails"].get("시그널메이커"), "0건" if available else None)
            self.assertIn("최근 90일" if available else "집계 미확인", field["optionsHint"])
            self.assertNotIn("optionsHint", next(field for template in payload["templates"] for field in template["fields"] if field["type"] == "customer"))

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
