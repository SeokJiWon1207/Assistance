"""요청 예산·취소가 실제 호출 경계와 병렬 작업까지 전달되는지 검증한다."""

import asyncio
import time
import unittest
from contextlib import ExitStack
from threading import Event
from unittest.mock import AsyncMock, Mock, patch

import requests
from fastapi import HTTPException

from backend import execution, llm
from backend.admission import ChatAdmissionController
from backend.api import chat as api
from backend.execution import ExecutionBudget, RequestStopped, execution_scope
from backend.ordered_prefetch import iter_in_order
from backend.schemas import ChatRequest
from backend.services import chat_service as chat
from backend.services.contracts import DeterministicReportResult, StageResult, StageState
from backend.services.report_dispatcher import REPORT_ORDER, ReportRequest, dispatch_reports
from backend.sources import mantis


MODEL = {"id": "primary", "model": "primary", "status": "active", "provider": "openai-compatible",
         "endpoint": "https://model.example/chat"}


def request(stream=False):
    return ChatRequest(modelId="primary", messages=[{"role": "user", "content": "KB증권 전체 상태 몇 건"}],
                       stream=stream, abTest=False)


class ExecutionBudgetTests(unittest.TestCase):
    def test_nested_stages_share_a_deadline_without_resetting_it(self):
        with patch.object(execution.time, "monotonic", return_value=100.0) as clock:
            budget = ExecutionBudget(110.0)
            with execution_scope(budget):
                self.assertEqual(execution.bounded_timeout(30), 10)
                clock.return_value = 107.0
                with execution_scope() as nested:
                    self.assertIs(nested, budget)
                    self.assertEqual(execution.bounded_timeout(30), 3)
                clock.return_value = 110.0
                with self.assertRaises(RequestStopped) as raised:
                    execution.checkpoint()
                self.assertEqual(raised.exception.status_code, 504)
        self.assertIsNone(execution.CURRENT_BUDGET.get())

    def test_retries_stop_when_the_first_attempt_consumes_the_budget(self):
        with patch.object(execution.time, "monotonic", return_value=100.0) as clock:
            def failed(*_args, **kwargs):
                self.assertEqual(kwargs["timeout"], 2)
                clock.return_value = 102.0
                return Mock(status_code=503)

            with execution_scope(ExecutionBudget(102.0)), \
                    patch.object(mantis, "get_mantis_settings", return_value={"url": "https://mantis.example"}), \
                    patch.object(mantis, "get_mantis_headers", return_value={}), \
                    patch.object(mantis.requests, "get", side_effect=failed) as get:
                with self.assertRaises(RequestStopped):
                    mantis.fetch_mantis_issues({"page": 1})
                self.assertEqual(get.call_count, 1)

    def test_parallel_pages_inherit_the_same_budget(self):
        budget = ExecutionBudget(time.monotonic() + 5)
        with execution_scope(budget):
            rows = list(iter_in_order(lambda _: execution.CURRENT_BUDGET.get(), range(4), workers=2))
        self.assertTrue(all(value is budget for _, value in rows))
        self.assertIsNone(execution.CURRENT_BUDGET.get())

    def test_cancelled_model_call_does_not_fallback_or_mark_an_outage(self):
        llm.reset_model_circuits()
        self.addCleanup(llm.reset_model_circuits)
        budget = ExecutionBudget(time.monotonic() + 10)

        def cancelled(*_args, **_kwargs):
            budget.cancel()
            raise requests.Timeout("대기 종료")

        with execution_scope(budget), \
                patch.object(chat, "get_model_registry", return_value=[{**MODEL, "id": "secondary"}]), \
                patch.object(chat, "get_model_api_key", return_value="synthetic"), \
                patch.object(llm.requests, "post", side_effect=cancelled) as post:
            with self.assertRaises(RequestStopped) as raised:
                chat.generate_model_response(request(True), MODEL, [], None, {}, 5)
            self.assertEqual(raised.exception.status_code, 499)
            self.assertEqual(post.call_count, 1)
        self.assertEqual(llm.model_circuit_status_for(MODEL)["consecutiveFailures"], 0)

    def test_stream_with_content_still_obeys_total_deadline(self):
        with patch.object(execution.time, "monotonic", return_value=100.0) as clock:
            def chunks(**_kwargs):
                yield 'data: {"choices":[{"delta":{"content":"처음"}}]}'
                clock.return_value = 111.0
                yield 'data: {"choices":[{"delta":{"content":"마지막"}}]}'

            response = Mock(headers={"content-type": "text/event-stream"})
            response.iter_lines.side_effect = chunks
            with execution_scope(ExecutionBudget(110.0)), self.assertRaises(RequestStopped):
                chat._read_model_response(response, MODEL, True)

    def test_deterministic_answer_works_without_a_model_api_key(self):
        answer = {"content": "총 3건", "sources": [], "raw": {"mode": "deterministic_mantis_count"}}
        with patch.object(chat, "get_private_model", return_value=MODEL), \
                patch.object(chat, "get_model_api_key", return_value=""), \
                patch.object(chat, "build_deterministic_report", return_value=DeterministicReportResult(answer)), \
                patch.object(chat, "persist_chat_response", side_effect=lambda _request, _text, response, **_: response), \
                patch.object(chat, "post_model_chat") as post:
            result = chat.chat_service._build_response(request())
        self.assertEqual(result["content"], "총 3건")
        post.assert_not_called()

    def test_stage_contract_rejects_incomplete_terminal_results(self):
        self.assertEqual(StageResult.from_response(None).state, StageState.CONTINUE)
        with self.assertRaises(ValueError):
            StageResult(StageState.ANSWER)
        with self.assertRaises(ValueError):
            StageResult(StageState.CONTINUE, {"content": "잘못된 조합"})
        result = StageResult.from_response({"content": "확인이 필요합니다", "raw": {"mode": "status_clarification"}})
        self.assertEqual(result.state, StageState.CLARIFICATION)

    def test_expired_report_handler_cannot_start_another_handler(self):
        with patch.object(execution.time, "monotonic", return_value=100.0) as clock:
            def expire(_text):
                clock.return_value = 102.0
                return None
            builders = {name: Mock(return_value=None) for name in REPORT_ORDER}
            builders[REPORT_ORDER[0]].side_effect = expire
            effort, notion = Mock(), Mock()
            with execution_scope(ExecutionBudget(101.0)), self.assertRaises(RequestStopped):
                dispatch_reports(ReportRequest("KB증권 이슈", "", {"mantis": True, "vectorDb": True}, [], None),
                                 builders, effort_builder=effort, notion_writer=notion)
            for name in REPORT_ORDER[1:]:
                builders[name].assert_not_called()
            effort.assert_not_called()
            notion.assert_not_called()


class ChatCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_speculative_page_does_not_delay_result_or_release_slot_early(self):
        controller = ChatAdmissionController()
        release, started = Event(), Event()
        budget = ExecutionBudget(time.monotonic() + 5)

        def fetch(page):
            if page == 1:
                started.wait(1)
            else:
                started.set()
                release.wait(2)
            return page

        def build(*_args, **_kwargs):
            pages = iter_in_order(fetch, [1, 2], workers=2)
            try:
                next(pages)
            finally:
                pages.close()
            return {"content": "첫 페이지로 답변 완료"}

        lease = controller.acquire("user")
        with patch.object(api.chat_service, "build_admitted_response", side_effect=build):
            iterator = api.chat_events(request(True), {"uid": "user"}, lease, budget)
            try:
                event = await asyncio.wait_for(anext(iterator), timeout=0.5)
                self.assertEqual(event["type"], "result")
                self.assertFalse(lease._released)
            finally:
                await iterator.aclose()
                release.set()
                for _ in range(100):
                    if lease._released:
                        break
                    await asyncio.sleep(0.01)
            self.assertTrue(lease._released)

    async def test_nonstream_deadline_and_disconnect_cancel_silent_workers(self):
        for disconnected in (False, True):
            with self.subTest(disconnected=disconnected):
                controller = ChatAdmissionController()
                release = Event()
                budget = ExecutionBudget(time.monotonic() + (5 if disconnected else 0.08))
                connection = Mock()
                connection.is_disconnected = AsyncMock(side_effect=[False, True] if disconnected else None,
                                                       return_value=False)
                with patch.object(api, "CHAT_ADMISSION", controller), \
                        patch.object(api, "current_request_user", return_value=None), \
                        patch.object(api.ExecutionBudget, "create", return_value=budget), \
                        patch.object(api.chat_service, "build_admitted_response",
                                     side_effect=lambda *_args, _release=release, **_kwargs: _release.wait(2)):
                    try:
                        with self.assertRaises(HTTPException) as raised:
                            await asyncio.wait_for(api.chat(request(), connection), timeout=0.5)
                        self.assertEqual(raised.exception.status_code, 499 if disconnected else 504)
                        self.assertTrue(budget.cancelled.is_set())
                        self.assertEqual(controller._active_total, 1)
                    finally:
                        release.set()
                        for _ in range(100):
                            if not controller._active_total:
                                break
                            await asyncio.sleep(0.01)
                    self.assertEqual(controller._active_total, 0)

    async def exercise_stream(self, *, deadline=False):
        controller = ChatAdmissionController()
        release = Event()
        finished = Event()
        budget = ExecutionBudget(time.monotonic() + (0.08 if deadline else 5))
        captured = []

        def build(*_args, **_kwargs):
            captured.append(execution.CURRENT_BUDGET.get())
            api.CHAT_STREAM_CONTEXT.emitter({"type": "status", "message": "시작"})
            release.wait(2)
            try:
                execution.checkpoint()
            finally:
                finished.set()
            return {"content": "끝"}

        with ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"CHAT_MAX_CONCURRENT_PER_USER": "1"}))
            stack.enter_context(patch.object(api.chat_service, "build_admitted_response", side_effect=build))
            lease = controller.acquire("user")
            iterator = api.chat_events(request(True), {"uid": "user"}, lease, budget)
            try:
                self.assertEqual((await anext(iterator))["type"], "status")
                if deadline:
                    event = await asyncio.wait_for(anext(iterator), timeout=0.5)
                    self.assertEqual(event["status"], 504)
                await iterator.aclose()
                self.assertTrue(budget.cancelled.is_set())
                self.assertIs(captured[0], budget)
                # 클라이언트는 끝났어도 실제 I/O가 종료되기 전에는 슬롯을 재사용하지 않는다.
                with self.assertRaises(HTTPException) as raised:
                    controller.acquire("user")
                self.assertEqual(raised.exception.status_code, 429)
            finally:
                release.set()
                await iterator.aclose()
                for _ in range(100):
                    if lease._released:
                        break
                    await asyncio.sleep(0.01)
            self.assertTrue(finished.is_set())
            controller.acquire("user").release()

    async def test_disconnect_cancels_worker_and_preserves_admission_until_exit(self):
        await self.exercise_stream()

    async def test_deadline_returns_an_error_while_sync_io_is_still_running(self):
        await self.exercise_stream(deadline=True)
