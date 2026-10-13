"""13개 대화 템플릿의 선택 조합에 대해 실제 라우팅·리포트 빌더 계약을 검사한다.

외부 I/O만 고정한 테스트다. 모델이 직접 작성하는 문안의 품질 평가는 포함하지 않는다.
"""

import unittest
from contextlib import ExitStack
from datetime import date
from itertools import product
from unittest.mock import patch

from backend.answers import format_deterministic_issue_answer, should_return_deterministic_issue_list
from backend.intent.source_router import rule_based_search_intent
from backend.reports import customer, incident_triage, monthly_customer, similar, sla, weekly_customer
from backend.services.chat_service import build_deterministic_report
from backend.sources.mantis import normalize_issue
from backend.sources.mantis_collection import IssueBatch
from backend.team_templates import load_team_templates
from backend.template_questions import fill_template_question
from backend.understanding import build_keyword_terms, extract_query_constraints


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 9, 18)


def template_variants():
    """프런트엔드에서 제공하는 모든 선택 옵션과 주요 증상·기간 경계를 조합한다."""
    for template in load_team_templates()["templates"]:
        fields = template["fields"]
        choices = []
        for field in fields:
            name = field["name"]
            if field.get("options") and field.get("type") == "select":
                values = field["options"]
            elif name == "customer":
                values = ["미래에셋증권"] + ([] if field.get("required") else [""])
            else:
                values = {"symptom": ["차트 미표시", "로그인 실패", "주문 접수 누락", "파일 다운로드 오류"],
                          "issueId": ["31411"], "month": ["2026-08"], "days": ["7", "30", "90"]}.get(name, [field.get("value", "")])
            choices.append(values)
        for combination in product(*choices):
            values = dict(zip([field["name"] for field in fields], combination, strict=True))
            rendered = dict(values)
            if "month" in rendered:
                rendered["month"] = "2026년 8월"

            yield template["id"], values, fill_template_question(template["questionTemplate"], rendered)


def raw_issues():
    rows = []
    for month, day, platform, status in product((1, 8, 9), (1, 8, 15, 18), ("HTS", "MTS"), ("assigned", "closed")):
        rows.append({"id": 31000 + len(rows), "summary": "차트 표시 점검", "project": {"name": "147MiraeAsset(미래에셋증권)"},
                     "status": {"name": status}, "handler": {"name": "김담당"}, "description": "기록된 증상과 확인 사항",
                     "created_at": f"2026-{month:02d}-{day:02d}", "updated_at": f"2026-{month:02d}-{day + 1:02d}",
                     "custom_fields": [{"field": {"name": "매체구분"}, "value": platform},
                                       {"field": {"name": "3PC공수"}, "value": "김담당(1.5)"}]})
    return rows


class TemplateContractTests(unittest.TestCase):
    def test_all_template_variants_keep_scope_and_answer_mode(self):
        cases = list(template_variants())
        self.assertEqual(len(cases), 351)
        self.assertEqual(len({item[0] for item in cases}), 13)
        raw = raw_issues()
        sample = normalize_issue(raw[0])
        retrieval = {"stages": [{"source": "mantis_page_scan", "status": "ok", "issues_list": [sample],
                                 "meta": {"found_count": 1}}]}
        expected_modes = {
            "customer-profile": "deterministic_customer_profile",
            "monthly-customer-report": "deterministic_monthly_customer_report",
            "aging-issues": "deterministic_mantis_sla", "effort-sla": "deterministic_mantis_sla",
            "weekly-report": "deterministic_weekly_report", "incident-triage": "deterministic_incident_triage",
            "similar-issues": "deterministic_similar_resolution",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(sla, "read_complete_store", return_value=IssueBatch(raw, {"search_complete": True})))
            stack.enter_context(patch("backend.query_planning.date", FixedDate))
            for module in (customer, monthly_customer, sla, weekly_customer):
                stack.enter_context(patch.object(module, "fetch_mantis_recent_issues", return_value=raw))
                stack.enter_context(patch.object(module, "date", FixedDate))
            stack.enter_context(patch.object(similar, "fetch_mantis_issue_by_id", return_value={**raw[0], "id": 31411}))
            for module in (similar, incident_triage):
                stack.enter_context(patch.object(module, "execute_chroma_search", return_value={
                    "status": "ok", "documents": [{"issue_id": sample["id"], "latest_issue": sample}],
                }))
            for template_id, values, question in cases:
                with self.subTest(template_id=template_id, values=values):
                    constraints = extract_query_constraints(question)
                    self.assertEqual(rule_based_search_intent(question, constraints)["source"], "mantis")
                    self.assertEqual(constraints["people"]["names"], [])
                    if values.get("customer"):
                        self.assertEqual(constraints["customer_key"], "미래에셋증권")
                    if values.get("platform") in {"HTS", "MTS"}:
                        self.assertEqual(constraints["platform_key"], values["platform"].lower())
                    if values.get("issueId"):
                        self.assertEqual(constraints["issue_ids"], [31411])
                        self.assertEqual(constraints["statuses"], [])
                    if "status" in values:
                        self.assertEqual(constraints["statuses"], {"미종료": ["open"], "종료": ["closed"], "전체": []}[values["status"]])
                    report = build_deterministic_report(question, question, {"mantis": True, "vectorDb": True}, [], None).response
                    if template_id in expected_modes:
                        self.assertIsNotNone(report)
                        self.assertEqual(report["raw"]["mode"], expected_modes[template_id])
                        self.assertTrue(report["content"])
                        self.assertFalse(any(stage.get("status") == "error" for stage in report["retrieval"]["stages"]))
                        if template_id == "effort-sla":
                            self.assertIn("|", report["content"])
                            self.assertIn("공수", report["content"])
                        if template_id == "incident-triage":
                            self.assertIn(f"우선순위: {values['urgency']}", report["content"])
                    else:
                        self.assertIsNone(report)
                        deterministic = should_return_deterministic_issue_list(question, constraints)
                        self.assertEqual(deterministic, template_id in {"daily-operations", "customer-analysis", "server-condition-search"})
                        if deterministic:
                            self.assertEqual(build_keyword_terms(question, constraints), [])
                            answer = format_deterministic_issue_answer(question, constraints, retrieval, 12)
                            if values.get("format") == "표":
                                self.assertIn("| ID | 고객사 |", answer)
