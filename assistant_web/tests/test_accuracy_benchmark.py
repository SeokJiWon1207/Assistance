import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.accuracy import benchmark

EXAMPLE = Path(__file__).resolve().parent.parent / "scripts" / "accuracy" / "benchmark_cases.example.jsonl"


def response(**updates):
    base = {"content": "", "mode": "deterministic_mantis_count", "sources": [], "queryPlan": None, "stages": [], "auditId": "a" * 32}
    base.update(updates)
    return base


def count_stage(total):
    return {"source": "mantis_count", "status": "ok", "meta": {"operation": "count", "total_count": total}}


class CaseFormatTests(unittest.TestCase):
    def write(self, *rows):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
        handle.write("\n".join(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False) for row in rows))
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def case(self, **updates):
        base = {"id": "C1", "split": "final", "category": "count",
                "turns": [{"question": "몇 건?", "expect": [{"type": "count", "value": 3}]}]}
        base.update(updates)
        return base

    def test_example_file_documents_every_expectation_type(self):
        cases = benchmark.load_cases(EXAMPLE)
        kinds = {expect["type"] for case in cases for turn in case["turns"] for expect in turn["expect"]}
        self.assertEqual(kinds, {"count", "issue_ids", "notion_titles", "plan", "contains", "not_contains", "clarify", "delivered"})
        # 저장소의 예시는 실제 점수에 섞이지 않도록 모두 dev 문항이다.
        self.assertEqual({case["split"] for case in cases}, {"dev"})

    def test_comments_and_blank_lines_are_ignored(self):
        self.assertEqual(len(benchmark.load_cases(self.write("// 설명", "", self.case()))), 1)

    def test_invalid_cases_are_rejected_with_the_case_location(self):
        for rows, message in (
            ((self.case(), self.case()), "중복"),
            ((self.case(split="test"),), "split"),
            ((self.case(category="misc"),), "category"),
            ((self.case(turns=[]),), "turns"),
            ((self.case(turns=[{"question": "몇 건?", "expect": []}]),), "expect"),
            ((self.case(turns=[{"question": "몇 건?", "expect": [{"type": "count", "value": -1}]}]),), "count.value"),
            ((self.case(turns=[{"question": "몇 건?", "expect": [{"type": "guess"}]}]),), "알 수 없는"),
            ((self.case(turns=[{"question": "몇 건?", "expect": [{"type": "plan", "owner": "석지원"}]}]),), "plan 항목"),
            ((self.case(turns=[{"question": "몇 건?", "expect": [{"type": "plan", "created_at": {"relative": "recent_months"}}]}]),), "양의 정수 n"),
            ((self.case(turns=[{"question": "몇 건?", "expect": [{"type": "plan", "created_at": {"from": "2026-13-01", "to": "2026-12-31"}}]}]),), "날짜 형식"),
            (("{broken",), "JSON"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(benchmark.CaseError, message):
                benchmark.load_cases(self.write(*rows))


class ScoringTests(unittest.TestCase):
    RUN_DATE = date(2026, 10, 21)

    def check(self, expect, actual):
        return benchmark.check_expectation(expect, actual, self.RUN_DATE)

    def test_count_requires_the_structured_total_and_the_displayed_number(self):
        self.assertIsNone(self.check({"type": "count", "value": 1234}, response(stages=[count_stage(1234)], content="총 1,234건입니다.")))
        self.assertIn("집계 결과", self.check({"type": "count", "value": 5}, response(stages=[count_stage(4)], content="총 5건")))
        self.assertIn("표시되지", self.check({"type": "count", "value": 5}, response(stages=[count_stage(5)], content="총 4건")))

    def test_issue_and_notion_sources_support_exact_includes_and_excludes(self):
        actual = response(sources=[{"issue_id": 1}, {"issue_id": "2"}, {"type": "notion", "title": "배포  체크리스트"}])
        self.assertIsNone(self.check({"type": "issue_ids", "ids": [1, 2]}, actual))
        self.assertIn("초과 [2]", self.check({"type": "issue_ids", "ids": [1], "match": "exact"}, actual))
        self.assertIsNone(self.check({"type": "issue_ids", "ids": [1], "match": "includes"}, actual))
        self.assertIn("포함되면 안 되는", self.check({"type": "issue_ids", "ids": [2], "match": "excludes"}, actual))
        self.assertIsNone(self.check({"type": "notion_titles", "titles": ["배포 체크리스트"]}, actual))
        self.assertIn("누락", self.check({"type": "notion_titles", "titles": ["장애 가이드"]}, actual))

    def test_required_facts_accept_synonyms_and_forbidden_claims_fail(self):
        actual = response(content="9/9에는 재현되지 않았고, 9/17 수정 버전을 안내했습니다.")
        self.assertIsNone(self.check({"type": "contains", "all": [["미재현", "재현되지 않"], "수정 버전"]}, actual))
        self.assertIn("고객 검증", self.check({"type": "contains", "all": ["고객 검증"]}, actual))
        self.assertIn("금지된 서술", self.check({"type": "not_contains", "any": ["수정 버전을 안내"]}, actual))

    def test_modes_distinguish_clarification_blocking_and_delivery(self):
        clarify = response(mode="unified_plan_clarification")
        self.assertIsNone(self.check({"type": "clarify"}, clarify))
        self.assertIn("확인 질문", self.check({"type": "delivered"}, clarify))
        self.assertIn("차단", self.check({"type": "delivered"}, response(mode="verification_blocked")))
        self.assertIn("확인 질문 대신", self.check({"type": "clarify"}, response()))
        self.assertIsNone(self.check({"type": "mode", "in": ["deterministic_mantis_count"]}, response()))

    def test_plan_compares_executed_conditions_not_answer_wording(self):
        plan = {"source": "mantis", "operation": "count", "resolved_constraints": {
            "statuses": ["open"], "excluded_statuses": [], "people": {"names": ["석지원"]}, "customer_key": "NH",
            "platform_key": "mts", "issue_ids": [], "created_at": {"from": "2026-08-01", "to": "2026-10-21"}}}
        actual = response(queryPlan=plan)
        self.assertIsNone(self.check({"type": "plan", "operation": "count", "statuses": ["open"], "people": ["석지원"],
                                      "customer": "NH", "platform": "mts", "created_at": {"relative": "recent_months", "n": 3}}, actual))
        failure = self.check({"type": "plan", "people": ["김민수"], "created_at": {"relative": "last_week"}}, actual)
        self.assertIn("people", failure)
        self.assertIn("2026-10-12", failure)
        self.assertEqual(self.check({"type": "plan", "operation": "count"}, response()), "실행 계획이 없습니다")

    def test_relative_periods_are_calculated_independently_of_product_code(self):
        expected = {
            ("today", None): ("2026-10-21", "2026-10-21"), ("yesterday", None): ("2026-10-20", "2026-10-20"),
            ("this_week", None): ("2026-10-19", "2026-10-25"), ("last_week", None): ("2026-10-12", "2026-10-18"),
            ("this_month", None): ("2026-10-01", "2026-10-31"), ("last_month", None): ("2026-09-01", "2026-09-30"),
            ("recent_days", 7): ("2026-10-15", "2026-10-21"), ("recent_months", 3): ("2026-08-01", "2026-10-21"),
            ("recent_months", 12): ("2025-11-01", "2026-10-21"),
        }
        for (kind, n), (start, end) in expected.items():
            with self.subTest(kind=kind, n=n):
                spec = {"relative": kind, **({"n": n} if n else {})}
                self.assertEqual(benchmark.relative_period(spec, self.RUN_DATE), {"from": start, "to": end})
        self.assertEqual(benchmark.relative_period({"relative": "last_month"}, date(2026, 1, 10)),
                         {"from": "2025-12-01", "to": "2025-12-31"})

    def test_case_passes_only_when_every_turn_passes_and_summary_uses_split(self):
        cases = [
            {"id": "F1", "split": "final", "category": "count", "turns": [{"question": "q1", "expect": [{"type": "count", "value": 2}]}]},
            {"id": "F2", "split": "final", "category": "followup", "turns": [
                {"question": "q2", "expect": [{"type": "delivered"}]}, {"question": "q3", "expect": [{"type": "count", "value": 1}]}]},
            {"id": "D1", "split": "dev", "category": "clarify", "turns": [{"question": "q4", "expect": [{"type": "clarify"}]}]},
        ]
        records = [
            {"case_id": "F1", "turn": 0, "latency_ms": 100.0, "response": response(stages=[count_stage(2)], content="총 2건")},
            {"case_id": "F2", "turn": 0, "latency_ms": 300.0, "response": response()},
            {"case_id": "F2", "turn": 1, "latency_ms": 200.0, "error": "Timeout: model"},
            {"case_id": "D1", "turn": 0, "latency_ms": 50.0, "response": response(mode="unified_plan_clarification")},
        ]
        report = benchmark.score_cases(cases, records, self.RUN_DATE)
        final = report["summary"]["final"]
        self.assertEqual((final["passed"], final["total"], final["pass_rate"]), (1, 2, 0.5))
        self.assertEqual(final["categories"], {"count": {"passed": 1, "total": 1}, "followup": {"passed": 0, "total": 1}})
        self.assertEqual(final["latency_ms"], {"mean": 200.0, "median": 200.0, "p95": 300.0})
        self.assertEqual(report["summary"]["dev"]["clarify_rate"], 1.0)
        failures = report["results"][1]["turns"][1]["failures"]
        self.assertEqual(failures, ["실행 오류: Timeout: model"])
        missing = benchmark.score_cases(cases[:1], [], self.RUN_DATE)["results"][0]["turns"][0]["failures"]
        self.assertEqual(missing, ["응답이 없습니다(실행되지 않음)"])

    def test_nearest_rank_percentile(self):
        self.assertIsNone(benchmark.nearest_rank([], 95))
        self.assertEqual(benchmark.nearest_rank([5.0], 95), 5.0)
        self.assertEqual(benchmark.nearest_rank([float(value) for value in range(1, 21)], 95), 19.0)


class RunnerTests(unittest.TestCase):
    def test_followups_carry_history_and_audit_id_and_stop_after_an_error(self):
        cases = [{"id": "C1", "split": "final", "category": "followup", "turns": [
            {"question": "첫 질문", "expect": [{"type": "delivered"}]},
            {"question": "그중 MTS만", "expect": [{"type": "delivered"}]},
            {"question": "세 번째", "expect": [{"type": "delivered"}]}]}]
        requests = []

        def factory(case, model_id, messages, audit_id):
            requests.append((model_id, messages, audit_id))
            return len(requests)

        def chat(request):
            if request == 2:
                raise TimeoutError("model")
            return {"content": "첫 답변", "auditId": "b" * 32, "raw": {"mode": "model"}, "sources": [{"issue_id": 1, "detail": {"big": "원문"}}]}

        records = benchmark.run_cases(cases, chat, "test-model", request_factory=factory)
        self.assertEqual(len(records), 2)
        self.assertEqual(requests[0], ("test-model", [{"role": "user", "content": "첫 질문"}], None))
        self.assertEqual(requests[1][1][1:], [{"role": "assistant", "content": "첫 답변"}, {"role": "user", "content": "그중 MTS만"}])
        self.assertEqual(requests[1][2], "b" * 32)
        self.assertEqual(records[1]["error"], "TimeoutError: model")
        # 원문 상세는 결과 파일에 남기지 않는다.
        self.assertNotIn("detail", records[0]["response"]["sources"][0])

    def test_real_chat_service_count_followup_is_recorded_and_scored(self):
        """실제 서비스·결정형 집계·검증·감사 저장을 거친 응답 구조로 채점한다. 모델과 원본만 대체한다."""
        import backend.reports.aggregate as aggregate
        import backend.services.chat_service as service

        def plan(**updates):
            base = {"source": "mantis", "operation": "count", "scope": "target", "followup": False, "clear_fields": [],
                    "terms": [], "group_by": None, "limit": None,
                    "status": {"mode": "open", "values": [], "evidence": "남아 있는"},
                    "filters": [{"field": "owner", "operator": "equals", "value": "석지원", "evidence": "석지원한테"}]}
            base.update(updates)
            return base

        plans = iter([plan(), plan(followup=True, scope="inherit", status={"mode": "inherit", "values": [], "evidence": ""},
                                   filters=[{"field": "platform", "operator": "equals", "value": "mts", "evidence": "MTS"}])])
        model_inputs = []

        def model(model_config, key, payload, **kwargs):
            model_inputs.append(json.loads(payload["messages"][-1]["content"]))
            body = next(plans)
            return Mock(raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": json.dumps(body)}}]})

        issue = lambda issue_id: {"id": issue_id, "summary": f"이슈 {issue_id}", "project": "NH", "status": "assigned",
                                  "handler": "석지원", "created_at": "2026-10-01"}
        fetched_at = datetime.now(timezone.utc).isoformat()
        scans = iter([{"available": True, "issues": [issue(1), issue(2)], "matched_count": 2, "group_counts": {},
                       "scanned_count": 50, "fetchedAt": fetched_at},
                      {"available": True, "issues": [issue(2)], "matched_count": 1, "group_counts": {},
                       "scanned_count": 50, "fetchedAt": fetched_at}])
        scanned = []

        def scan(*args, **kwargs):
            scanned.append(kwargs.get("constraints"))
            return next(scans)

        cases = [{"id": "I1", "split": "final", "category": "followup", "turns": [
            {"question": "석지원한테 남아 있는 이슈가 모두 몇 개야?", "expect": [
                {"type": "count", "value": 2}, {"type": "issue_ids", "ids": [1, 2]}, {"type": "delivered"},
                {"type": "plan", "operation": "count", "statuses": ["open"], "people": ["석지원"]}]},
            {"question": "그중 MTS만 세어줘", "expect": [
                {"type": "count", "value": 1}, {"type": "plan", "statuses": ["open"], "people": ["석지원"], "platform": "mts"}]}]}]
        user = {"uid": "benchmark-test", "mantisUsername": "석지원", "displayName": "석지원"}
        with (patch.dict("os.environ", {"UNIFIED_QUERY_PLANNER_ENABLED": "true"}),
              patch.object(service, "get_private_model", return_value={"id": "test", "status": "active"}),
              patch.object(service, "get_model_api_key", return_value="key"),
              patch("backend.intent.unified.post_model_chat", side_effect=model),
              patch.object(aggregate, "scan_mantis_documents", side_effect=scan)):
            records = benchmark.run_cases(cases, lambda request: service.chat_service.build_response(request, user=user), "test")

        self.assertEqual([record.get("error") for record in records], [None, None])
        self.assertTrue(all(len(record["response"]["auditId"]) == 32 for record in records))
        # 두 번째 질문은 첫 답변의 감사 기록에서 확정 조건을 복원해 모델과 집계에 넘긴다.
        self.assertEqual(model_inputs[1]["previous_confirmed_plan"]["source"], "mantis")
        self.assertEqual(scanned[1]["platform_key"], "mts")
        report = benchmark.score_cases(cases, records, date.today())
        self.assertEqual(report["results"][0]["turns"][0]["failures"], [])
        self.assertTrue(report["results"][0]["passed"], report["results"][0])


class CommandTests(unittest.TestCase):
    def test_score_command_writes_reports_and_applies_fail_under(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases_path = root / "cases.jsonl"
            cases_path.write_text(json.dumps({"id": "F1", "split": "final", "category": "count", "turns": [
                {"question": "몇 건?", "expect": [{"type": "count", "value": 2}]}]}, ensure_ascii=False) + "\n", encoding="utf-8")
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text(json.dumps({
                "cases_sha256": benchmark.file_sha256(cases_path), "split": "all", "run_date": "2026-10-21",
                "model_id": "test", "git_commit": "abc"}), encoding="utf-8")
            (run_dir / "responses.jsonl").write_text(json.dumps({
                "case_id": "F1", "turn": 0, "latency_ms": 10.0,
                "response": response(stages=[count_stage(3)], content="총 3건")}, ensure_ascii=False) + "\n", encoding="utf-8")
            with patch("builtins.print"):
                self.assertEqual(benchmark.main(["score", str(cases_path), str(run_dir)]), 0)
                self.assertEqual(benchmark.main(["score", str(cases_path), str(run_dir), "--fail-under", "1.0"]), 1)
                self.assertEqual(benchmark.main(["validate", str(cases_path)]), 0)
            report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["final"]["passed"], 0)
            self.assertIn("집계 결과 [3]", (run_dir / "report.md").read_text(encoding="utf-8"))

    def test_invalid_case_file_returns_exit_code_2(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.jsonl"
            path.write_text('{"id": "X"}\n', encoding="utf-8")
            with patch("sys.stderr"):
                self.assertEqual(benchmark.main(["validate", str(path)]), 2)
                self.assertEqual(benchmark.main(["validate", str(Path(directory) / "missing.jsonl")]), 2)
                self.assertEqual(benchmark.main(["score", str(path), str(Path(directory) / "no-run")]), 2)


if __name__ == "__main__":
    unittest.main()
