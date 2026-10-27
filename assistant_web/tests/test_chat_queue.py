"""접수 순서·대기 취소·상한·메모리 회복을 실제 컨트롤러로 확인한다."""
import asyncio
import os
import time
import unittest
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from backend.admission import ChatAdmissionController
from backend.api import chat as api
from backend.schemas import ChatRequest


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.controller = ChatAdmissionController()
        self.enterContext(patch.dict(os.environ, {'CHAT_MAX_CONCURRENT_TOTAL':'1',
            'CHAT_MAX_CONCURRENT_PER_USER':'1', 'CHAT_RATE_LIMIT_PER_MINUTE':'20',
            'CHAT_MAX_QUEUED_TOTAL':'32', 'CHAT_MAX_QUEUED_PER_USER':'4', 'CHAT_QUEUE_TIMEOUT_SECONDS':'120'}))
        self.memory = self.enterContext(patch('backend.admission.available_memory_bytes', return_value=1024**3))

    def test_fifo_and_new_arrivals_cannot_overtake_waiting_requests(self):
        active = self.controller.acquire('active')
        first, second = self.controller.enqueue('first'), self.controller.enqueue('second')
        self.assertEqual((first.position, second.position), (1, 2))
        self.assertIsNone(first.try_start())
        active.release()
        with self.assertRaises(HTTPException):
            self.controller.acquire('new-arrival')
        self.assertIsNone(second.try_start())
        lease = first.try_start()
        self.assertIsNotNone(lease)
        self.assertEqual(second.position, 1)
        lease.release()
        second.try_start().release()
        self.assertEqual(self.controller._active_total, 0)

    def test_cancelled_head_does_not_block_next_or_consume_a_slot(self):
        active = self.controller.acquire('active')
        first, second = self.controller.enqueue('first'), self.controller.enqueue('second')
        first.cancel(); first.cancel()
        self.assertEqual(second.position, 1)
        active.release()
        second.try_start().release()
        with self.assertRaises(HTTPException) as caught:
            first.try_start()
        self.assertEqual(caught.exception.status_code, 499)

    def test_low_memory_waits_and_rechecks_before_starting(self):
        self.memory.return_value = 100 * 1024**2
        ticket = self.controller.enqueue('user')
        self.assertIsNone(ticket.try_start())
        self.assertEqual(self.controller._active_total, 0)
        self.memory.return_value = 512 * 1024**2
        ticket.try_start().release()

    def test_expired_ticket_is_removed_and_next_request_can_proceed(self):
        with patch('backend.admission.time.monotonic', return_value=100):
            ticket = self.controller.enqueue('old')
        with patch('backend.admission.time.monotonic', return_value=221):
            with self.assertRaises(HTTPException) as caught:
                ticket.try_start()
            self.assertEqual(caught.exception.status_code, 503)
            self.controller.enqueue('next').try_start().release()

    def test_queue_capacity_and_per_user_limit_are_recovered_after_cancel(self):
        with patch.dict(os.environ, {'CHAT_MAX_QUEUED_TOTAL':'2', 'CHAT_MAX_QUEUED_PER_USER':'1'}):
            first = self.controller.enqueue('one')
            with self.assertRaises(HTTPException):
                self.controller.enqueue('one')
            second = self.controller.enqueue('two')
            with self.assertRaises(HTTPException):
                self.controller.enqueue('three')
            first.cancel()
            third = self.controller.enqueue('three')
            second.cancel(); third.cancel()

    def test_submission_preserves_rate_limit_and_counts_waiting_requests(self):
        with patch.dict(os.environ, {'CHAT_RATE_LIMIT_PER_MINUTE':'1'}):
            ticket = self.controller.enqueue('one')
            with self.assertRaises(HTTPException) as caught:
                self.controller.enqueue('one')
            self.assertEqual(caught.exception.status_code, 429)
            ticket.cancel()

    def test_sync_service_waits_without_rejection_and_releases_the_slot(self):
        active = self.controller.acquire('active')
        queued = Event()
        original = self.controller.enqueue

        def enqueue(key):
            ticket = original(key)
            queued.set()
            return ticket

        def run():
            with self.controller.wait('waiting'):
                return '순서대로 실행'

        with patch.object(self.controller,'enqueue',side_effect=enqueue), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(run)
            self.assertTrue(queued.wait(1))
            self.assertFalse(pending.done())
            active.release()
            self.assertEqual(pending.result(timeout=1),'순서대로 실행')
        self.assertEqual(self.controller._active_total,0)

    def test_per_user_busy_head_keeps_global_fifo_until_it_can_start(self):
        with patch.dict(os.environ,{'CHAT_MAX_CONCURRENT_TOTAL':'2'}):
            active = self.controller.acquire('same')
            head, following = self.controller.enqueue('same'), self.controller.enqueue('other')
            self.assertIsNone(head.try_start())
            self.assertIsNone(following.try_start())
            active.release()
            head_lease = head.try_start()
            other_lease = following.try_start()
            self.assertEqual(self.controller._active_total,2)
            # 응답 종료 후 실행되는 대기 cleanup이 실제 워커 슬롯을 반납하면 안 된다.
            head.cancel()
            self.assertEqual(self.controller._active_total,2)
            head_lease.release(); other_lease.release()


class QueueApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.controller = ChatAdmissionController()
        self.enterContext(patch.object(api, 'CHAT_ADMISSION', self.controller))
        self.enterContext(patch.dict(os.environ, {'CHAT_MAX_CONCURRENT_TOTAL':'1',
            'CHAT_MAX_CONCURRENT_PER_USER':'1','CHAT_RATE_LIMIT_PER_MINUTE':'20'}))
        self.enterContext(patch('backend.admission.available_memory_bytes',return_value=1024**3))

    async def test_waiting_stream_reports_position_without_starting_model_then_runs(self):
        active = self.controller.acquire('active')
        build = self.enterContext(patch.object(api.chat_service,'build_admitted_response',return_value={'content':'완료'}))
        ticket = self.controller.enqueue('user')
        stream = api.queued_chat_events(ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}]),
                                        {'uid':'user'},ticket)
        status = await anext(stream)
        self.assertEqual((status['type'],status['stage'],status['queuePosition']),('status','queue',1))
        build.assert_not_called()
        active.release()
        result = await asyncio.wait_for(anext(stream), 1)
        self.assertEqual(result['data']['content'],'완료')
        await stream.aclose()

    async def test_closing_waiting_stream_removes_ticket_and_never_starts_work(self):
        active = self.controller.acquire('active')
        build = self.enterContext(patch.object(api.chat_service,'build_admitted_response'))
        ticket = self.controller.enqueue('user')
        stream = api.queued_chat_events(ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}]),None,ticket)
        await anext(stream)
        await stream.aclose()
        self.assertEqual(ticket.position,0)
        build.assert_not_called()
        active.release()
        self.controller.enqueue('next').try_start().release()

    async def test_nonstream_waits_and_disconnect_removes_pending_request(self):
        active = self.controller.acquire('active')
        connection = Mock()
        connection.is_disconnected = AsyncMock(side_effect=[False,True])
        self.enterContext(patch.object(api,'current_request_user',return_value={'uid':'user'}))
        build = self.enterContext(patch.object(api.chat_service,'build_admitted_response'))
        with self.assertRaises(HTTPException) as caught:
            await asyncio.wait_for(api.chat(ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}]),connection),1)
        self.assertEqual(caught.exception.status_code,499)
        build.assert_not_called()
        active.release()
        self.controller.enqueue('user').try_start().release()

    async def test_execution_budget_starts_after_queue_wait_and_timeout_does_not_start_work(self):
        active = self.controller.acquire('active')
        ticket = self.controller.enqueue('user')
        create = self.enterContext(patch.object(api.ExecutionBudget,'create',wraps=api.ExecutionBudget.create))
        build = self.enterContext(patch.object(api.chat_service,'build_admitted_response',return_value={'content':'완료'}))
        request = ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}])
        stream = api.queued_chat_events(request,None,ticket)
        await anext(stream)
        create.assert_not_called()
        ticket.expires_at = time.monotonic() - 1
        error = await asyncio.wait_for(anext(stream),1)
        self.assertEqual(error['status'],503)
        build.assert_not_called()
        await stream.aclose()
        active.release()
        followup = api.queued_chat_events(request,None,self.controller.enqueue('next'))
        self.assertEqual((await anext(followup))['type'],'result')
        create.assert_called_once()
        await followup.aclose()

    async def test_stream_background_cleans_up_a_never_consumed_body(self):
        connection = Mock()
        self.enterContext(patch.object(api,'current_request_user',return_value={'uid':'user'}))
        response = await api.chat(ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}],stream=True),connection)
        self.assertEqual(len(self.controller._pending),1)
        await response.body_iterator.aclose()
        await response.background()
        self.assertEqual(len(self.controller._pending),0)
        self.assertEqual(self.controller._active_total,0)

    async def test_failure_during_budget_creation_does_not_leak_newly_granted_slot(self):
        ticket = self.controller.enqueue('user')
        self.enterContext(patch.object(api.ExecutionBudget,'create',side_effect=RuntimeError('기동 실패')))
        stream = api.queued_chat_events(ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}]),None,ticket)
        with self.assertRaisesRegex(RuntimeError,'기동 실패'):
            await anext(stream)
        self.assertEqual(self.controller._active_total,0)

    async def test_nonstream_requests_finish_in_submission_order_without_429(self):
        release, started = Event(), Event()
        order = []

        def build(request, **kwargs):
            text = request.messages[0].content
            order.append(text)
            if text == 'first':
                started.set()
                release.wait(2)
            return {'content':text}

        self.enterContext(patch.object(api.chat_service,'build_admitted_response',side_effect=build))
        self.enterContext(patch.object(api,'current_request_user',return_value={'uid':'same-user'}))
        connection = Mock(is_disconnected=AsyncMock(return_value=False))

        async def submit(text):
            return await api.chat(ChatRequest(modelId='test',messages=[{'role':'user','content':text}]),connection)

        first = asyncio.create_task(submit('first'))
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        second = asyncio.create_task(submit('second'))
        await asyncio.sleep(0)
        third = asyncio.create_task(submit('third'))
        await asyncio.sleep(0)
        self.assertEqual(order,['first'])
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first,second,third),2)
        self.assertEqual(order,['first','second','third'])
        self.assertEqual([r['content'] for r in results],['first','second','third'])
        self.assertEqual(self.controller._active_total,0)

    async def test_cancelling_running_stream_keeps_slot_until_worker_exits(self):
        release = Event()

        def build(*args, **kwargs):
            api.CHAT_STREAM_CONTEXT.emitter({'type':'status','message':'실행 중'})
            release.wait(2)
            return {'content':'완료'}

        self.enterContext(patch.object(api.chat_service,'build_admitted_response',side_effect=build))
        stream = api.queued_chat_events(ChatRequest(modelId='test',messages=[{'role':'user','content':'질문'}]),
                                        None,self.controller.enqueue('user'))
        try:
            self.assertEqual((await anext(stream))['message'],'실행 중')
            await stream.aclose()
            self.assertEqual(self.controller._active_total,1)
            waiting = self.controller.enqueue('next')
            self.assertIsNone(waiting.try_start())
        finally:
            release.set()
            for _ in range(100):
                if not self.controller._active_total:
                    break
                await asyncio.sleep(0.01)
        waiting.try_start().release()
