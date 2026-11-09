"""격리된 벤치마크 프로세스의 전송 관측. 요청 본문·헤더·원문은 저장하지 않는다."""
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
from threading import Lock
import time
from urllib.parse import urlsplit
from unittest.mock import patch

import requests
from scripts.accuracy.benchmark_pricing import price_event, validate_prices as validate_prices


def _usage(data: dict) -> dict | None:
    raw = data.get('usage') or {}
    prompt = raw.get('prompt_tokens', data.get('prompt_eval_count'))
    completion = raw.get('completion_tokens', data.get('eval_count'))
    details = raw.get('prompt_tokens_details') or {}
    cached = details.get('cached_tokens', 0)
    write = details.get('cache_write_tokens')
    by_lifetime = details.get('cache_write_tokens_details') or {}
    if all(type(v) is int and v >= 0 for v in (prompt, completion, cached)) and cached <= prompt:
        return {'prompt': prompt, 'completion': completion, 'cached': cached, 'cache_write': write,
                'cache_write_30m': by_lifetime.get('ttl_30m_tokens'), 'cache_write_12h': by_lifetime.get('ttl_12h_tokens')}
    return None


class Observation:
    def __init__(self, endpoints: dict, prices: dict):
        self.endpoints = endpoints
        self.prices = validate_prices(prices)
        self.events: list[dict] = []
        self.reads: list[dict] = []
        self.pending_reads: dict[int, tuple[dict, dict]] = {}
        self.serialized_reads: dict[str, list[dict]] = {}
        self.tool_calls = 0
        self.lock = Lock()

    def send(self, send, session, request, **kwargs):
        url = urlsplit(request.url)
        endpoint = f'{url.scheme}://{url.netloc}{url.path}'
        configured = self.endpoints.get(endpoint)
        model = configured
        payload = {}
        if configured:
            try:
                payload = json.loads(request.body or '{}')
                if isinstance(configured, dict):
                    model = configured.get(payload.get('model'), 'unrecognized_model')
            except (ValueError, TypeError, AttributeError):
                model = 'unrecognized_model'
        event = {'kind': 'model' if configured else 'source', 'model': model,
                 'origin': f'{url.scheme}://{url.netloc}', 'method': request.method}
        started = time.perf_counter()
        try:
            response = send(session, request, **kwargs)
            event['status'] = response.status_code
            if configured:
                if 200 <= response.status_code < 300:
                    self.record_model_delivery(payload)
                try:
                    data = response.json()
                    event['usage'] = _usage(data)
                    event['service_tier'] = data.get('service_tier') or payload.get('service_tier')
                    event['cache_ttl'] = (payload.get('prompt_cache_options') or {}).get('ttl', '30m')
                except (ValueError, TypeError, AttributeError):
                    event['usage'] = None
            return response
        except BaseException as exc:
            event['error_type'] = type(exc).__name__
            raise
        finally:
            event['latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
            with self.lock:
                self.events.append(event)

    def record_tool(self, name: str, result: dict, context, *, count: bool = True) -> None:
        with self.lock:
            if count:
                self.tool_calls += 1
            if name != 'get_notion_page_content' or not result.get('ok'):
                return
            for row in result.get('results') or []:
                page_id = context.ledger.page_id_for_ref(row.get('ref', ''))
                if page_id and isinstance(row.get('content'), str):
                    read = {'page_id': page_id, 'body_checked': row.get('body_checked') is True,
                        'content_complete': row.get('content_complete') is True,
                        'body_sha256': hashlib.sha256(row['content'].encode()).hexdigest(),
                        'returned_chars': len(row['content']), 'truncated': bool(result.get('truncated')),
                        'content_offset': row.get('content_offset', 0),
                        'evidence_grade': row.get('evidence_grade', result.get('evidence_grade')),
                        'updated_at': row.get('updated_at')}
                    from backend.tools.redaction import redact_text
                    page = context.ledger.notion_page(page_id) if hasattr(context.ledger, 'notion_page') else None
                    if page and page.get('_verified_body'):
                        body = redact_text(str(page.get('content') or ''))
                        offset = row.get('content_offset', 0)
                        matched = (type(offset) is int and 0 <= offset <= len(body)
                                   and body[offset:offset + len(row['content'])] == row['content'])
                        read.update(page_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                                    page_body_chars=len(body), page_body_verified=matched)
                    if row.get('content_incomplete_reasons'):
                        read['content_incomplete_reasons'] = list(row['content_incomplete_reasons'])
                    # 게시 성공과 모델 전달은 다르다. 원문은 저장하지 않고 최종 직렬화를 기다린다.
                    self.pending_reads[id(row)] = (row, read)

    def record_delivery(self, result: dict, encoded: str) -> None:
        """공통 길이 제한·마스킹을 거친 최종 도구 메시지의 구간을 확인한다."""
        if result.get('tool') != 'get_notion_page_content' or not result.get('ok'):
            return
        sent = json.loads(encoded)
        sent_rows = {r.get('ref'): r for r in sent.get('results') or [] if isinstance(r, dict)}
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.lock:
            for row in result.get('results') or []:
                pending = self.pending_reads.pop(id(row), None)
                if pending is None:
                    continue
                original, read = pending
                delivered = sent_rows.get(row.get('ref')) or {}
                content = delivered.get('content')
                if not isinstance(content, str):
                    continue
                offset = delivered.get('content_offset', 0)
                same_span = offset == original.get('content_offset', 0) and original['content'].startswith(content)
                read.update(body_sha256=hashlib.sha256(content.encode()).hexdigest(), returned_chars=len(content),
                            content_offset=offset, truncated=bool(sent.get('truncated')),
                            page_body_verified=read.get('page_body_verified') is True and same_span)
                self.serialized_reads.setdefault(digest, []).append(read)

    def record_model_delivery(self, payload: dict) -> None:
        """성공한 모델 요청에 실제 포함된 도구 메시지만 증명한다. 원문·메시지는 남기지 않는다."""
        if not isinstance(payload, dict):
            return
        messages = payload.get('messages')
        if not isinstance(messages, list):
            return
        with self.lock:
            for message in messages:
                if not isinstance(message, dict) or message.get('role') != 'tool' or not isinstance(message.get('content'), str):
                    continue
                digest = hashlib.sha256(message['content'].encode()).hexdigest()
                for read in self.serialized_reads.pop(digest, []):
                    self.reads.append({**read, 'delivery_verified': True})

    def report(self) -> dict:
        with self.lock:
            events = list(self.events)
            reads = list(self.reads)
        models = [e for e in events if e['kind'] == 'model']
        usage = [e['usage'] for e in models if e.get('usage') is not None]
        rates = self.prices.get('models') or {}
        known_cost = 0.0
        priced = 0
        unknown = Counter()
        for event in models:
            rate = rates.get(event['model'])
            cost, reason = price_event(event, rate) if rate else (None, 'model_unpriced')
            if cost is not None:
                known_cost += cost
                priced += 1
            else:
                unknown[reason] += 1
        complete = priced == len(models) and bool(self.prices)
        return {'model_attempts': len(models), 'models': dict(Counter(e['model'] for e in models)),
                'source_http_requests': len(events) - len(models), 'logical_tool_attempts': self.tool_calls,
                'prompt_tokens': sum(u['prompt'] for u in usage), 'completion_tokens': sum(u['completion'] for u in usage),
                'cached_tokens': sum(u['cached'] for u in usage), 'reported_usage_calls': len(usage),
                'cache_write_tokens': sum(u['cache_write'] for u in usage if type(u.get('cache_write')) is int and u['cache_write'] >= 0),
                'unreported_cache_write_calls': sum(u.get('cache_write') is None for u in usage),
                'unreported_usage_calls': len(models) - len(usage),
                'unpriced_calls': sum(e['model'] not in rates for e in models),
                'cost': {'currency': self.prices.get('currency'), 'total': round(known_cost, 9) if complete else None,
                         'known_subtotal': round(known_cost, 9) if priced else None, 'complete': complete,
                         'unresolved_calls': dict(unknown),
                         'price_source': self.prices.get('source'), 'price_as_of': self.prices.get('as_of')},
                'events': events, 'evidence_reads': reads}


@contextmanager
def observe_requests(endpoints: dict, prices: dict | None = None):
    """전용 프로세스에서 한 요청을 측정한다. 전송 허용·마스킹은 기존 경로가 담당한다."""
    from backend.tools.registry import ToolRegistry
    from backend.services.agent import orchestrator
    measured = Observation(endpoints, prices or {})
    send, execute = requests.Session.send, ToolRegistry.execute
    invoke, publish = ToolRegistry._execute, ToolRegistry.publish_pending
    fit = orchestrator.fit_result

    def observe_send(session, request, **kwargs):
        return measured.send(send, session, request, **kwargs)

    def observe_tool(registry, name, arguments, parse_error, context):
        result = {}
        try:
            result = execute(registry, name, arguments, parse_error, context)
            return result
        finally:
            measured.record_tool(name, result, context, count=False)

    def observe_invoke(registry, name, arguments, parse_error, context):
        try:
            return invoke(registry, name, arguments, parse_error, context)
        finally:
            # 오류·취소·게시 거절도 논리적 시도다. 현재 본문 근거는 게시 성공 뒤에만 센다.
            measured.record_tool(name, {}, context)

    def observe_publish(registry, calls):
        results = publish(registry, calls)
        for (name, pending, _), result in zip(calls, results, strict=True):
            measured.record_tool(name, result, pending.root, count=False)
        return results

    def observe_format(result, limit):
        encoded = fit(result, limit)
        measured.record_delivery(result, encoded)
        return encoded

    with (patch.object(requests.Session, 'send', observe_send), patch.object(ToolRegistry, 'execute', observe_tool),
          patch.object(ToolRegistry, '_execute', observe_invoke), patch.object(ToolRegistry, 'publish_pending', observe_publish),
          patch.object(orchestrator, 'fit_result', observe_format)):
        yield measured


def summarize_measurements(turns: list[dict]) -> dict:
    observations = [t['observation'] for t in turns if t.get('observation') is not None]
    currencies = {o.get('cost', {}).get('currency') for o in observations}
    complete = len(observations) == len(turns) and len(currencies) == 1 and None not in currencies and all(
        o.get('cost', {}).get('total') is not None for o in observations)
    costs = sorted(o['cost']['total'] for o in observations) if complete else []
    return {'observed_turns': len(observations), 'total_turns': len(turns),
            **{key: sum(o.get(key, 0) for o in observations) for key in ('model_attempts', 'source_http_requests',
                'logical_tool_attempts', 'prompt_tokens', 'completion_tokens', 'cached_tokens',
                'cache_write_tokens', 'unreported_cache_write_calls', 'unreported_usage_calls', 'unpriced_calls')},
            'currency': next(iter(currencies)) if len(currencies) == 1 else None,
            'cost_total': sum(o['cost']['total'] for o in observations) if complete else None,
            'cost_per_request': sum(costs) / len(costs) if costs else None,
            'cost_p95': costs[max(0, (95 * len(costs) + 99)//100 - 1)] if costs else None,
            'cost_unresolved_calls': dict(sum((Counter(o.get('cost', {}).get('unresolved_calls', {})) for o in observations), Counter()))}
