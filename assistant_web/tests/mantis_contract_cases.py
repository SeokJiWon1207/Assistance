"""Mantis 전후 비교용 고정 입력. 임시 저장본과 가짜 HTTP만 사용한다."""
import copy
from contextlib import ExitStack, nullcontext
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException


def run_cases(*, client=None, pipeline=None):
    from backend import retrieval
    from backend.sources import mantis
    from backend.sources.mantis_collection import IssueBatch
    from backend.intent.constraints import extract_query_constraints
    if client is None:
        from backend.sources import mantis_client as client
    if pipeline is None:
        from backend.services import retrieval_pipeline as pipeline
    rows = [
        {'id': number, 'project': {'name': customer}, 'summary': title,
         'description': '조건검색 서버 점검 본문', 'status': {'name': status},
         'handler': {'real_name': '김담당'}, 'reporter': {'real_name': '이요청'},
         'created_at': f'2026-10-{number-90:02d}T09:00:00', 'updated_at': '2026-10-30T10:00:00',
         'custom_fields': [{'field': {'name': '매체구분'}, 'value': 'HTS'},
                           {'field': {'name': '화면번호'}, 'value': '1001'}]}
        for number, customer, title, status in [
            (103, '한국투자증권', '[1001] 조건검색 서버 오류', 'assigned'),
            (102, '신한투자증권', '[1001] 서버 점검', 'resolved'),
            (101, '한국투자증권', '[1001] 조건검색 서버 점검', 'resolved'),
        ]
    ]
    normalized = [mantis.normalize_issue(row) for row in rows]
    output = []

    def response(status):
        return Mock(status_code=status, text='fixed response', json=lambda: {'issues': copy.deepcopy(rows)})

    def capture(name, run, calls):
        try:
            result = run()
        except HTTPException as exc:
            result = {'error': type(exc).__name__, 'status_code': exc.status_code, 'detail': exc.detail}
        except requests.RequestException as exc:
            result = {'error': type(exc).__name__, 'detail': str(exc)}
        output.append({'case': name, 'result': copy.deepcopy(result), 'calls': copy.deepcopy(calls)})

    for single in (False, True):
        for name, events in [('ok', [200]), ('timeout_retry', ['timeout', 200]), ('rate_limit', [429, 200]),
                             ('server_retry', [500, 500, 200]), ('missing', [404]), ('denied', [403]),
                             ('exhausted', ['timeout', 'timeout', 'timeout'])]:
            calls = []
            pending = iter(events)
            def get(url, pending=pending, calls=calls, **kwargs):
                calls.append({'http': url, **kwargs})
                event = next(pending)
                if event == 'timeout':
                    raise requests.Timeout('fixed timeout')
                return response(event)
            with patch.object(client, 'get_mantis_settings', return_value={'url': 'https://mantis.test/issues', 'token': 'test'}), \
                    patch.object(client, 'get_mantis_headers', return_value={'Authorization': 'test'}), \
                    patch.object(client.requests, 'get', side_effect=get), \
                    patch.object(client, 'interruptible_sleep', side_effect=lambda seconds, calls=calls: calls.append({'sleep': seconds})):
                capture(('detail/' if single else 'page/') + name,
                        lambda single=single: mantis.fetch_mantis_issue_by_id(103) if single else
                        mantis.fetch_mantis_issues({'page': 2, 'page_size': 50}, timeout=12), calls)

    scopes = [
        ('customer', {'customer_aliases': ['한투', '한국투자증권']}, '조건검색'),
        ('status', {'statuses': ['resolved']}, '서버'),
        ('title_body', {'filter_clauses': [{'field': 'title', 'operator': 'contains', 'value': '점검'}]}, '서버'),
        ('empty', {'customer_aliases': ['코스콤']}, '조건검색'),
        ('ascending', {'sort': {'field': 'issue_id', 'direction': 'asc'}}, ''),
    ]
    for stored in (False, True):
        for name, constraints, keyword in scopes:
            batch = IssueBatch(copy.deepcopy(rows), {'search_complete': True, 'fetchedAt': '2026-10-30T10:00:00+09:00'})
            calls = []
            def fetch(*args, calls=calls, batch=batch):
                calls.append({'raw_fetch': args})
                return batch
            with patch.object(mantis, 'stream_complete_store', return_value=nullcontext(batch if stored else None)), \
                    patch.object(mantis, '_fetch_mantis_raw_issues', side_effect=fetch):
                capture(('store/' if stored else 'live/')+name,
                        lambda keyword=keyword, constraints=constraints: mantis.execute_mantis_page_scan(keyword, 2, constraints), calls)

    stage = {'source': 'mantis_snapshot', 'status': 'ok', 'issues_list': normalized,
             'meta': {'matched_issue_ids': [101, 102, 103], 'found_count': 3, 'search_complete': True,
                      'snapshot_as_of': '2026-10-01'}}
    for name in ('changed', 'missing', 'timeout', 'denied'):
        calls = []
        def refresh(issue_id, calls=calls, name=name):
            calls.append({'refresh': issue_id})
            if name == 'missing':
                return None
            if name == 'timeout':
                raise requests.Timeout('fixed timeout')
            if name == 'denied':
                raise HTTPException(status_code=403, detail='denied')
            row = copy.deepcopy(next(row for row in rows if row['id'] == issue_id))
            row['status'] = {'name': 'resolved'}
            return row
        with patch.object(mantis, 'fetch_mantis_issue_by_id', side_effect=refresh):
            capture('revalidate/'+name,
                    lambda: mantis.revalidate_mantis_snapshot_stage(copy.deepcopy(stage), {'statuses': ['assigned']}), calls)

    questions = [
        ('한국투자증권 HTS 미종료 이슈 찾아줘', 'mantis', 'list'),
        ('#103 이슈 보여줘', 'mantis', 'list'),
        ('#103 원인을 설명해줘', 'mantis', 'explain'),
        ('#103과 비슷한 이슈 찾아줘', 'mantis', 'list'),
        ('2025년 한국투자증권 이슈 찾아줘', 'mantis', 'list'),
        ('노션에서 한투 관련 문서 찾아줘', 'notion', 'list'),
        ("노션에서 본문에 '한투'가 들어간 문서 찾아줘", 'notion', 'list'),
        ('멘티스와 노션에서 조건검색 찾아줘', 'both', 'list'),
        ('안녕', 'general', 'list'),
    ]
    for question, source, operation in questions:
        calls = []
        def handler(name, calls=calls):
            def invoke(*args, **kwargs):
                calls.append({'function': name, 'args': copy.deepcopy(args), 'kwargs': copy.deepcopy(kwargs)})
                if name == 'revalidate_mantis_snapshot_stage':
                    return args[0]
                if name == 'get_chroma_document':
                    return None
                if name == 'fetch_mantis_issue_by_id':
                    return copy.deepcopy(rows[0])
                if name == 'execute_notion_search':
                    return {'source': 'notion', 'status': 'ok', 'notion_pages': [], 'meta': {'search_complete': True}}
                if name == 'execute_chroma_search':
                    return {'source': 'chroma', 'status': 'ok', 'documents': [], 'meta': {}}
                if name == 'execute_mantis_filter_search':
                    return {'source': 'mantis_filter', 'status': 'skipped', 'issues_list': [], 'meta': {}}
                return {'source': name.removeprefix('execute_').removesuffix('_search'), 'status': 'ok',
                        'issues_list': copy.deepcopy(normalized), 'meta': {'search_complete': True, 'found_count': 3,
                            'matched_issue_ids': [101, 102, 103], 'displayed_count': 3}}
            return invoke
        with ExitStack() as stack:
            for name in ['execute_mantis_filter_search', 'execute_mantis_id_lookup', 'execute_mantis_page_scan',
                         'execute_mantis_snapshot_search', 'revalidate_mantis_snapshot_stage', 'execute_chroma_search',
                         'execute_notion_search', 'get_chroma_document', 'fetch_mantis_issue_by_id']:
                stack.enter_context(patch.object(pipeline, name, side_effect=handler(name)))
            stack.enter_context(patch.object(pipeline, 'get_mantis_snapshot_freshness', return_value='2026-10-01'))
            capture('pipeline/'+question,
                    lambda question=question, source=source, operation=operation: retrieval.run_retrieval_pipeline(question, 2, {'mantis': True, 'notion': True, 'vectorDb': True},
                                                            {'source': source, 'operation': operation,
                                                             'resolved_constraints': extract_query_constraints(question)}), calls)
    return output
