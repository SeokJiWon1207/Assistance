"""실제 질문 검증 도구가 응답 종류별 건수 근거를 올바르게 판정하는지 확인한다."""
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from backend.sources.mantis_collection import IssueBatch
from scripts.accuracy import verify_snapshot_questions


class SnapshotQuestionVerificationTests(unittest.TestCase):
    def test_oracle_keeps_explicit_media_and_default_team_contract(self):
        def issue(number, media, owner):
            return {"id": number, "project": {"name": "120NH(NH증권)"},
                    "status": {"name": "assigned"}, "handler": {"name": owner},
                    "description": "MTS 비교 언급", "summary": "차트 오류",
                    "custom_fields": [{"field": {"name": "매체구분"}, "value": media}]}
        rows = IssueBatch([issue(1, "", "이영희"), issue(2, "MTS", "다른팀"),
                           issue(3, "MTS", "이영희")], {"fetchedAt": "2026-10-15T00:00:00+00:00"})
        output = io.StringIO()
        with patch("backend.sources.mantis_store.read_complete_store", return_value=rows), \
             patch("backend.services.chat_service.chat_service.build_response", return_value={"content": "", "sources": []}), \
             redirect_stdout(output):
            verify_snapshot_questions.run("fixture")
        report = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(report[0]["expectedCount"], 1)

    def check_response(self, mode, meta, expected_exit, verification="pass"):
        # 원본에 일치하는 이슈가 없을 때 다른 집계 필드를 0건의 근거로 삼으면 안 된다.
        rows = IssueBatch([], {"fetchedAt": "2026-10-13T00:00:00+00:00"})
        response = {
            "content": "조회 결과", "sources": [], "raw": {"mode": mode},
            "retrieval": {"stages": [{"meta": meta}]},
            "verification": {"status": verification},
        }
        output = io.StringIO()
        with (
            patch("backend.sources.mantis_store.read_complete_store", return_value=rows),
            patch("backend.services.chat_service.chat_service.build_response", return_value=response),
            redirect_stdout(output),
        ):
            self.assertEqual(verify_snapshot_questions.run("fixture"), expected_exit)
        report = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(report[-1]["total"], 20)
        return report

    def test_work_report_uses_filtered_count_instead_of_snapshot_total(self):
        report = self.check_response("deterministic_work_report", {"matched_count": 0, "total_count": 100}, 0)
        self.assertEqual(report[-1]["failures"], 0)

    def test_regular_count_uses_total_count(self):
        self.check_response("deterministic_aggregate", {"matched_count": 100, "total_count": 0}, 0)

    def test_missing_or_wrong_filtered_count_cannot_pass_as_zero(self):
        for meta in ({"total_count": 0}, {"matched_count": 1, "total_count": 0}):
            with self.subTest(meta=meta):
                report = self.check_response("deterministic_work_report", meta, 1)
                self.assertGreater(report[-1]["failures"], 0)

    def test_matching_count_does_not_override_failed_source_verification(self):
        report = self.check_response("deterministic_work_report", {"matched_count": 0}, 1, "warning")
        self.assertEqual(report[-1]["failures"], 20)
