"""실제 ID에 허위 속성을 붙이는 답변과 근거 없이 생성하는 경로를 차단한다."""

import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from backend.answers import should_return_deterministic_issue_list, format_deterministic_issue_answer
from backend.evaluation.delivery import decide_delivery
from backend.evaluation.verification import build_verification_report
from backend.evaluation import finalization
from backend.audit import CHAT_STREAM_CONTEXT
from backend.retrieval import build_model_retrieval_context, build_sources, collect_retrieved_issues, count_retrieved_issues
from backend.schemas import ChatRequest
from backend.services.chat_service import retrieve_chat_evidence
from backend.services import chat_service
from backend.services.contracts import ChatPipelineContext
from backend.sources.mantis import build_sources_from_issue_rows
from backend.understanding import extract_query_constraints


ISSUE = {
    "id": 31585, "summary": "[MTS] 전략추가·조건찾기·조건설정 화면 UI 개선",
    "project": "132SHGM(신한굿모닝증권)", "handler": "석지원", "status": "confirmed",
    "created_at": "2026-10-06", "updated_at": "2026-10-06", "media_type": "MTS",
    "priority": "normal", "severity": "minor", "description": "화면 UI 개선 요청",
}


def retrieval(rows=None):
    return {"query": "MTS 이슈 조회", "constraints": {},
            "query_plan": {"source": "mantis", "operation": "search"},
            "stages": [{"source": "mantis_page_scan", "status": "ok",
                        "issues_list": rows if rows is not None else [deepcopy(ISSUE)],
                        "meta": {"search_complete": True}}]}


def verification(content, rows=None, mode="model"):
    rows = rows if rows is not None else [deepcopy(ISSUE)]
    return build_verification_report(
        "이슈 조회", content, build_sources_from_issue_rows(rows), retrieval(rows), mode,
    )


class AnswerGroundingTests(unittest.TestCase):
    def test_table_unknown_columns_and_malformed_rows_cannot_hide_fabricated_ids(self):
        for table in (
            f"| ID | 제목 | 설명 |\n|---|---|---|\n|31585|{ISSUE['summary']}|추가 이슈 #99999도 있습니다.|",
            "| ID | 담당자 |\n|---|---|\n|확인 불가|추가 이슈 #99999도 있습니다.|",
            "| ID | 담당자 |\n|---|---|\n|31585|석지원|추가 이슈 #99999도 있습니다.|",
            "| ID | 담당자 |추가 이슈 #99999|\n|---|---|---|\n|31585|석지원|설명|",
            f"| ID | 제목 | 이슈 번호 |\n|---|---|---|\n|31585|{ISSUE['summary']}|#99999|",
            f"| ID | 제목 | ID |\n|---|---|---|\n|31585|{ISSUE['summary']}|99999|",
        ):
            with self.subTest(table=table):
                self.assertIn('issue_ids', decide_delivery(verification(table)).blocked_checks)

    def test_special_title_characters_round_trip_without_false_issue_claims(self):
        titles = [
            '연결 에러 확인\r\n[0314] 테스트 완료',
            '문자 | \x1c 저장 확인 및 ID: 3 참고',
            '**강조** `코드` <문자> C:\\temp "따옴표"',
            '첫 줄\u0085다음 줄\u2028마지막 줄',
        ]
        for title in titles:
            for query in ('MTS 이슈 전체 목록 보여줘', 'MTS 이슈 전체를 표로 보여줘'):
                with self.subTest(title=repr(title), query=query):
                    row = {**ISSUE, 'summary': title}
                    content = format_deterministic_issue_answer(query, {}, retrieval([row]), 5)
                    report = verification(content, [row], 'deterministic_issue_list')
                    self.assertTrue(decide_delivery(report).allowed, report['checks'])

    def test_quoted_title_encoding_does_not_allow_fabricated_fields_or_references(self):
        row = {**ISSUE, 'summary': '원문\r\n문자 | ID: 3'}
        content = format_deterministic_issue_answer('MTS 이슈 전체 보여줘', {}, retrieval([row]), 5)
        for altered in (content.replace('원문', '거짓 제목'),
                        content.replace('**담당자**: 석지원', '**담당자**: 거짓 담당자'),
                        content + '\n추가 이슈 #99999도 있습니다.'):
            with self.subTest(altered=altered):
                self.assertFalse(decide_delivery(verification(altered, [row], 'deterministic_issue_list')).allowed)

    def test_list_and_basic_summary_never_require_model_facts(self):
        for question in ("최근 이슈 보여줘", "10월 MTS 접수 이슈 전체 정리해줘",
                         "10월 MTS 이슈를 표로 요약해줘", "#31585 담당자 누구야"):
            with self.subTest(question=question):
                self.assertTrue(should_return_deterministic_issue_list(question, extract_query_constraints(question)))

    def test_analysis_and_notice_still_use_analysis_path(self):
        for question in ("MTS 이슈 원인 분석해줘", "고객사별 이슈 추이를 요약해줘",
                         "#31585 고객 안내문 작성해줘"):
            with self.subTest(question=question):
                self.assertFalse(should_return_deterministic_issue_list(question, extract_query_constraints(question)))

    def test_same_id_with_fabricated_table_fields_is_blocked(self):
        headers = ["이슈 번호", "프로젝트", "제목", "상태", "담당자", "등록일"]
        correct = ["#31585", "신한투자증권", ISSUE["summary"], "확인된 이슈", "석지원", "2026-10-06"]
        for index, fake in ((1, "KB증권"), (2, "로그인 유지 시간 초과 시 재인증 오류"),
                            (3, "해결됨"), (4, "하영훈"), (5, "2026-10-04")):
            cells = correct.copy()
            cells[index] = fake
            table = "| " + " | ".join(headers) + " |\n|---|---|---|---|---|---|\n| " + " | ".join(cells) + " |"
            with self.subTest(field=headers[index]):
                report = verification(table)
                self.assertFalse(decide_delivery(report).allowed, report)
                self.assertIn("issue_fields", decide_delivery(report).blocked_checks)

    def test_bare_table_id_cannot_bypass_id_validation(self):
        for issue_id in ("99999", "[99999](https://example.test/issue/99999)"):
            report = verification(f"| ID | 담당자 |\n|---|---|\n| {issue_id} | 석지원 |")
            self.assertIn("issue_ids", decide_delivery(report).blocked_checks)

    def test_labeled_fields_are_checked_per_issue_not_global_name_presence(self):
        second = {**ISSUE, "id": 31590, "handler": "하영훈"}
        report = verification("**#31585**\n- 담당자: 하영훈\n\n**#31590**\n- 담당자: 석지원", [ISSUE, second])
        self.assertIn("issue_fields", decide_delivery(report).blocked_checks)

    def test_inline_labeled_fields_and_handler_sentence_are_checked(self):
        for content in ("1. **ID**: 31585 | **담당자**: 하영훈 | **등록일**: 2026-10-06",
                        "#31585의 담당자는 하영훈입니다."):
            with self.subTest(content=content):
                self.assertIn("issue_fields", decide_delivery(verification(content)).blocked_checks)

    def test_correct_native_fields_and_display_aliases_are_allowed(self):
        for project in (ISSUE["project"], "신한투자증권", "신한굿모닝증권"):
            table = ("| ID | 고객사 | 제목 | 상태 | 담당자 | 등록일 |\n|---|---|---|---|---|---|\n"
                     f"| 31585 | {project} | {ISSUE['summary']} | 확인된 이슈 | 석지원 | 2026-10-06 |")
            with self.subTest(project=project):
                self.assertTrue(decide_delivery(verification(table)).allowed)

    def test_unknown_source_field_cannot_be_filled_in(self):
        source = {**ISSUE, "handler": "담당자 미지정"}
        report = verification("| ID | 담당자 |\n|---|---|\n| 31585 | 김민수 |", [source])
        self.assertIn("issue_fields", decide_delivery(report).blocked_checks)

    def test_quoted_source_description_is_not_treated_as_current_handler(self):
        row = {**ISSUE, "description": "#31585 담당자: 하영훈 (과거 요청 내용)"}
        content = "**#31585**\n- 담당자: 석지원\n\n**내용**\n" + row["description"]
        self.assertTrue(decide_delivery(verification(content, [row], "deterministic_issue_list")).allowed)

    def test_context_exposes_actual_evidence_limit_without_requesting_invented_rows(self):
        rows = [{**ISSUE, "id": 31000 + i} for i in range(14)]
        context = build_model_retrieval_context(retrieval(rows), 14)
        self.assertEqual(context["provided_issue_count"], len(context["issues"]))
        self.assertEqual(context["requested_limit"], 14)
        self.assertTrue(context["issues_truncated"])
        self.assertEqual(context["limit"], len(context["issues"]))

    def test_disabled_tools_do_not_fall_through_to_model_for_business_query(self):
        request = ChatRequest(modelId="test", messages=[{"role": "user", "content": "10월 MTS 이슈 보여줘"}])
        context = ChatPipelineContext(
            latest_user_text=request.messages[0].content, retrieval_text=request.messages[0].content,
            model={"id": "test"}, api_key="", messages=[], system_notes=[],
            enabled_tools={"mantis": False, "notion": False, "vectorDb": False},
            query_constraints=extract_query_constraints(request.messages[0].content),
            active_limit=5, intent_plan={"source": "mantis"},
        )
        result = retrieve_chat_evidence(request, context)
        self.assertIsNotNone(result.response)
        self.assertEqual(result.response["raw"]["mode"], "evidence_unavailable")
        self.assertIn("조회", result.response["content"])

    def test_normalized_platform_and_date_mismatch_are_definite_failures(self):
        for row in ({**ISSUE, "media_type": "HTS", "description": "MTS와 비교"},
                    {**ISSUE, "created_at": "2026-09-30"}):
            question = "2026년 10월 MTS 이슈 보여줘"
            result = retrieval([row])
            result["constraints"] = extract_query_constraints(question)
            report = build_verification_report(question, "#31585", build_sources_from_issue_rows([row]), result, "model")
            self.assertIn("constraints", decide_delivery(report).blocked_checks)

    def test_wrong_total_cannot_pass_with_correct_ids(self):
        result = retrieval()
        result["stages"][0]["meta"].update({"matched_issue_ids": [31585], "found_count": 1})
        report = build_verification_report("이슈 조회", "조건에 맞는 이슈는 총 14건입니다. #31585",
                                           build_sources_from_issue_rows([ISSUE]), result, "model")
        self.assertIn("answer_count", decide_delivery(report).blocked_checks)

    def test_model_sources_match_only_rows_the_model_received(self):
        rows = [{**ISSUE, "id": 31000 + i} for i in range(14)]
        result = retrieval(rows)
        model = {"id": "test", "model": "test", "status": "active"}
        request = ChatRequest(modelId="test", temperature=1.2, messages=[{"role": "user", "content": "원인 분석"}])
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": "확인한 원문을 검토했습니다."}}]}
        with patch.object(chat_service, "get_model_registry", return_value=[]), \
             patch.object(chat_service, "get_model_api_key", return_value="test"), \
             patch.object(chat_service, "post_model_chat", return_value=response) as post:
            answer = chat_service.generate_model_response(request, model, [], result, {}, 14)
        self.assertEqual(post.call_args.args[2]["temperature"], 0)
        received_ids = {row["id"] for row in build_model_retrieval_context(result, 14)["issues"]}
        self.assertEqual({s["issue_id"] for s in answer["sourcesForFollowUps"]}, received_ids)

    def test_wrong_known_id_fields_are_blocked_before_stream_and_saved_for_audit(self):
        fabricated = "| ID | 담당자 |\n|---|---|\n| 31585 | 김민수 |"
        response = {"modelId": "test", "content": fabricated, "sources": [],
                    "sourcesForFollowUps": build_sources_from_issue_rows([ISSUE]),
                    "retrieval": retrieval(), "raw": {"mode": "model"}}
        events = []
        request = ChatRequest(modelId="test", showSources=False, messages=[{"role": "user", "content": "조회"}])
        with patch.object(CHAT_STREAM_CONTEXT, "emitter", events.append, create=True), \
             patch.object(CHAT_STREAM_CONTEXT, "content_streamed", False, create=True):
            answer = finalization.finalize_chat_response(request, "조회", response)
        self.assertEqual(answer["raw"]["mode"], "verification_blocked")
        self.assertNotIn("김민수", str(answer))
        self.assertNotIn("김민수", str(events))

    def test_ab_candidate_does_not_bypass_final_validation(self):
        sources = build_sources_from_issue_rows([ISSUE])
        response = {"modelId": "test", "content": "#31585 담당자는 석지원입니다.", "sources": sources,
                    "retrieval": retrieval(), "raw": {"mode": "model"}}
        request = ChatRequest(modelId="test", abTest=True, abModelId="candidate",
                              messages=[{"role": "user", "content": "조회"}])
        candidate = {"status": "ok", "content": "#31585 담당자는 김민수입니다.", "sources": sources}
        dependencies = {"generate_candidate_model_answer": Mock(return_value=candidate),
                        "should_attach_ab_test": Mock(return_value=True)}
        with patch.object(finalization, "_compat_dependency", side_effect=lambda name, default: dependencies.get(name, default)):
            answer = finalization.finalize_chat_response(request, "조회", response)
        self.assertNotIn("김민수", str(answer))
        self.assertEqual(answer["abTest"]["variantB"]["status"], "blocked")

    def test_requested_all_rows_are_rendered_without_model_or_synthetic_fill(self):
        rows = [{**ISSUE, "id": 31000 + i, "handler": f"원문담당{i}"} for i in range(14)]
        query = "2026년 10월 MTS 이슈를 모두 표로 정리해줘"
        request = ChatRequest(modelId="test", messages=[{"role": "user", "content": query}])
        context = ChatPipelineContext(query, {"id": "test"}, "", [], query, {"mantis": True}, [],
                                      extract_query_constraints(query), 5, intent_plan={"source": "mantis"})
        with patch.object(chat_service, "run_retrieval_pipeline", return_value=retrieval(rows)):
            response = retrieve_chat_evidence(request, context).response
        self.assertEqual(response["raw"]["mode"], "deterministic_issue_list")
        self.assertEqual(len(response["sources"]), 14)
        for i in range(14):
            self.assertIn(f"| {31000+i} |", response["content"])
            self.assertIn(f"원문담당{i}", response["content"])
        self.assertNotIn("김민수", response["content"])

    def test_native_enum_labels_are_available_and_validated(self):
        from backend.sources.mantis import normalize_issue
        row = normalize_issue({"id": 31585, "summary": "개선 요청",
                               "priority": {"name": "normal", "label": "보통"},
                               "severity": {"name": "minor", "label": "개선"}})
        context = build_model_retrieval_context(retrieval([row]), 5)
        self.assertEqual(context["issues"][0]["priority"], "보통")
        self.assertEqual(context["issues"][0]["severity"], "개선")
        content = "| ID | 우선순위 | 심각도 |\n|---|---|---|\n|31585|보통|개선|"
        self.assertTrue(decide_delivery(verification(content, [row])).allowed)
        self.assertIn("issue_fields", decide_delivery(verification(content.replace("보통", "긴급"), [row])).blocked_checks)

    def test_issue_header_alias_cannot_hide_fabricated_row(self):
        for identifier in ("31585", "99999"):
            content = f"| 이슈 | 제목 | 담당자 |\n|---|---|---|\n| {identifier} | 가짜 제목 | 김민수 |"
            self.assertFalse(decide_delivery(verification(content)).allowed)

    def test_total_count_word_order_and_emphasis_cannot_bypass_validation(self):
        for content in ("총 14건의 이슈가 확인되었습니다. #31585", "이슈는 **총 14건**입니다. #31585"):
            with self.subTest(content=content):
                self.assertIn("answer_count", decide_delivery(verification(content)).blocked_checks)

    def test_exact_source_quote_is_not_a_current_handler_claim(self):
        row = {**ISSUE, "description": "담당자: 하영훈 (과거 요청 내용)"}
        content = '#31585\n원문 인용: “담당자: 하영훈 (과거 요청 내용)”'
        self.assertTrue(decide_delivery(verification(content, [row])).allowed)
        invented = content.replace("하영훈", "김민수")
        self.assertFalse(decide_delivery(verification(invented, [row])).allowed)

    def test_year_and_count_after_issue_word_are_not_ids(self):
        self.assertTrue(decide_delivery(verification("이슈 2026년 10월 조회 결과입니다. 총 1건의 이슈입니다.")).allowed)

    def test_current_live_row_replaces_snapshot_in_all_answer_views(self):
        result = retrieval()
        result["stages"][0]["source"] = "mantis_snapshot"
        current = {**ISSUE, "handler": "하영훈", "status": "resolved"}
        result["stages"].append({"source": "mantis_page_scan", "status": "ok", "issues_list": [current],
                                 "meta": {"scanned_issue_ids": [31585], "matched_issue_ids": [31585]}})
        for rows in (collect_retrieved_issues(result, 10), build_sources(result),
                     build_model_retrieval_context(result, 10)["issues"]):
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["handler"], "하영훈")

    def test_live_exclusion_cannot_reappear_from_snapshot_or_vector_document(self):
        result = retrieval()
        result["stages"][0]["source"] = "mantis_snapshot"
        result["stages"].extend([
            {"source": "mantis_page_scan", "status": "ok", "issues_list": [],
             "meta": {"scanned_issue_ids": [31585], "matched_issue_ids": []}},
            {"source": "chroma_db", "status": "ok", "documents": [{"issue_id": 31585, "content": "옛 저장본"}]},
        ])
        self.assertEqual(collect_retrieved_issues(result, 10), [])
        self.assertEqual(build_sources(result), [])

    def test_saved_filter_is_not_proof_of_a_complete_total(self):
        result = retrieval()
        result["stages"][0].update({"source": "mantis_filter", "meta": {"search_complete": True, "coverage": "saved_filter"}})
        result["stages"].append({"source": "mantis_page_scan", "status": "error", "error_kind": "dependency"})
        report = build_verification_report("이슈 조회", "이슈는 총 1건입니다. #31585", build_sources(result), result, "model")
        self.assertIn("answer_count", decide_delivery(report).blocked_checks)

    def test_label_inside_exact_title_is_not_a_current_field(self):
        row = {**ISSUE, "summary": "담당자: 김민수 변경 요청"}
        question = "#31585 상세 알려줘"
        content = format_deterministic_issue_answer(question, extract_query_constraints(question), retrieval([row]), 5)
        self.assertTrue(decide_delivery(verification(content, [row], "deterministic_issue_list")).allowed)

    def test_all_request_wins_over_planner_default_limit(self):
        query = "10월 MTS 이슈 모두 표로 정리해줘"
        request = ChatRequest(modelId="test", messages=[{"role": "user", "content": query}])
        context = ChatPipelineContext(query, {"id": "test"}, "", [], query, {"mantis": True}, [],
                                      extract_query_constraints(query), 5, intent_plan={"source": "mantis", "limit": 5})
        self.assertEqual(chat_service.retrieval_limit(request, context)[0], 200)

    def test_empty_ab_evidence_returns_limitation_without_generating_facts(self):
        from backend.evaluation import comparison
        model = {"id": "test", "status": "active"}
        request = ChatRequest(modelId="test", messages=[{"role": "user", "content": "조회"}])
        no_basic_evidence = {"stages": [{"source": "chroma_db", "documents": []}]}
        dependency = {"get_private_model": lambda _: model,
                      "post_model_chat": Mock(side_effect=AssertionError("근거 없는 모델 호출"))}
        with patch.object(comparison, "_compat_dependency", side_effect=lambda n, d: dependency.get(n, d)), \
             patch.object(comparison, "get_model_api_key", return_value="test"):
            answer = comparison.generate_basic_grounded_baseline(request, "이슈 조회", no_basic_evidence)
        self.assertEqual(answer["status"], "unavailable")
        self.assertEqual(answer["sources"], [])

    def test_json_and_borderless_tables_obey_same_fact_checks(self):
        values = [
            '{"issues": [{"id": 31585, "title": "가짜 제목", "handler": "김민수"}]}',
            '```json\n[{"id": 99999, "handler": "김민수"}]\n```',
            '이슈 | 제목 | 담당자\n---|---|---\n31585 | 가짜 제목 | 김민수',
        ]
        for content in values:
            with self.subTest(content=content):
                self.assertFalse(decide_delivery(verification(content)).allowed)

    def test_combined_source_cannot_invent_mantis_count(self):
        result = retrieval()
        result["query_plan"]["source"] = "both"
        report = build_verification_report("멘티스와 노션 조회", "Mantis 이슈는 총 14건입니다. #31585",
                                           build_sources(result), result, "model")
        self.assertIn("answer_count", decide_delivery(report).blocked_checks)

    def test_json_enum_objects_are_fields_not_separate_issue_ids(self):
        content = ('{"issues":[{"id":31585,"handler":{"id":149,"real_name":"석지원"},'
                   '"status":{"id":40,"name":"confirmed"}}]}')
        self.assertTrue(decide_delivery(verification(content)).allowed)
        self.assertFalse(decide_delivery(verification(content.replace("석지원", "김민수"))).allowed)

    def test_refreshed_vector_document_beats_old_snapshot(self):
        result = retrieval()
        result["stages"][0]["source"] = "mantis_snapshot"
        result["stages"].append({"source": "chroma_db", "status": "ok", "documents": [
            {"issue_id": 31585, "latest_issue": {**ISSUE, "handler": "하영훈"}},
        ]})
        self.assertEqual(collect_retrieved_issues(result, 5)[0]["handler"], "하영훈")

    def test_latest_vector_row_replaces_snapshot_page_scan_and_can_restore_a_match(self):
        for matched in ([31585], []):
            result = retrieval([ISSUE] if matched else [])
            result["stages"][0]["meta"].update({"status": "snapshot", "scanned_issue_ids": [31585],
                                                "matched_issue_ids": matched})
            result["stages"].append({"source": "chroma_db", "status": "ok", "documents": [
                {"issue_id": 31585, "latest_issue": {**ISSUE, "handler": "하영훈"}},
            ]})
            with self.subTest(matched=matched):
                rows = collect_retrieved_issues(result, 5)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["handler"], "하영훈")

    def test_json_korean_issue_container_is_validated(self):
        content = '{"이슈":[{"id":31585,"handler":"김민수"},{"id":99999,"title":"가짜"}]}'
        self.assertFalse(decide_delivery(verification(content)).allowed)

    def test_analysis_section_heading_is_not_an_issue_title_claim(self):
        content = '## #31585 원인 분석\n\n담당자: 석지원'
        self.assertTrue(decide_delivery(verification(content)).allowed)

    def test_live_vector_exclusion_removes_the_old_snapshot_row(self):
        result = retrieval()
        result["stages"][0]["meta"]["status"] = "snapshot"
        result["stages"].append({"source": "chroma_db", "status": "ok", "documents": [],
                                 "meta": {"scanned_issue_ids": [31585], "matched_issue_ids": []}})
        self.assertEqual(collect_retrieved_issues(result, 5), [])

    def test_source_body_mentions_do_not_override_structured_customer_or_media(self):
        row = {**ISSUE, "description": "NH와 HTS의 비교 결과를 참고"}
        question = "NH 제외 MTS 이슈 보여줘"
        result = retrieval([row])
        result["query"] = question
        result["constraints"] = extract_query_constraints(question)
        report = build_verification_report(question, "#31585", build_sources_from_issue_rows([row]), result, "model")
        check = next(item for item in report["checks"] if item["id"] == "constraints")
        self.assertEqual(check["status"], "pass", check)

    def test_partial_vector_refresh_cannot_restore_a_live_excluded_issue(self):
        result = retrieval()
        result["stages"][0]["source"] = "mantis_snapshot"
        result["stages"].extend([
            {"source": "mantis_page_scan", "status": "ok", "issues_list": [],
             "meta": {"scanned_issue_ids": [31585], "matched_issue_ids": []}},
            {"source": "chroma_db", "status": "ok", "documents": [
                {"issue_id": 31590, "latest_issue": {**ISSUE, "id": 31590}},
                {"issue_id": 31585, "content": "과거 저장본"},
            ], "meta": {"scanned_issue_ids": [31590], "matched_issue_ids": [31590, 31585]}},
        ])
        self.assertEqual([row["id"] for row in collect_retrieved_issues(result, 5)], [31590])
        self.assertEqual(count_retrieved_issues(result), 1)


if __name__ == "__main__":
    unittest.main()
