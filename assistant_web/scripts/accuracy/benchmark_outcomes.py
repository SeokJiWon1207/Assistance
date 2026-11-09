"""제품 판정기와 독립적인 항목 완수·표시 목록·근거 관련성 평가."""
from collections import Counter
from html import unescape
import re
from scripts.accuracy.benchmark_relevance import LEVELS, assess_relevance, completed_reads as completed_reads, validate_support

CLARIFY_MODES = frozenset({'unified_plan_clarification', 'agent_plan_clarification'})
EXPECTATIONS = frozenset({'displayed_issue_ids', 'followup_issue_ids', 'current_body', 'evidence_relevance', 'groups'})


def response_outcome(response: dict) -> str:
    mode = response.get('mode')
    agent = response.get('agent') or {}
    if agent.get('stopReason') in {'cancelled', 'request_cancelled'}:
        return 'cancelled'
    if (mode == 'verification_blocked' or (response.get('verification') or {}).get('delivery') == 'blocked'
            or agent.get('stopReason') == 'policy_blocked'):
        return 'blocked'
    if mode in CLARIFY_MODES:
        return 'clarify'
    if mode == 'agent_query_shadow_skipped':
        return 'not_run'
    if mode in {'agent_no_evidence', 'explanation_evidence_incomplete'} or not str(response.get('content') or '').strip():
        return 'incomplete'
    if mode == 'agent_evidence_only' or agent.get('retrievalOutcome') in {'partial_evidence', 'search_incomplete'}:
        return 'partial'
    return 'answered'


def displayed_ids(content: str) -> list[int]:
    text = unescape(content)
    numbered = re.findall(r'^\s*\d+[.)]\s+(?:\*\*)?#(\d+)\b', text, re.M)
    if numbered:
        return [int(v) for v in numbered]
    return [int(v) for v in re.findall(r'^\s*(?:[-*]\s+)?(?:\*\*)?#(\d+)\b', text, re.M)]


def validate_expectation(expect: dict) -> None:
    kind = expect['type']
    if kind in {'displayed_issue_ids', 'followup_issue_ids'}:
        if not isinstance(expect.get('ids'), list) or any(type(v) is not int or v <= 0 for v in expect['ids']):
            raise ValueError('ids는 양의 정수 목록이어야 합니다')
    elif kind == 'current_body':
        if 'page_ids' in expect and (not isinstance(expect['page_ids'], list) or
                                    any(not isinstance(v, str) or not v for v in expect['page_ids'])):
            raise ValueError('page_ids는 페이지 ID 목록이어야 합니다')
    elif kind == 'groups':
        if not isinstance(expect.get('values'), dict) or not expect['values'] or any(
                not isinstance(k, str) or not k or type(v) is not int or v < 0 for k, v in expect['values'].items()):
            raise ValueError('values는 그룹별 0 이상의 정수 건수여야 합니다')
    elif kind == 'evidence_relevance':
        if expect.get('level') not in {'related', 'direct'} or not isinstance(expect.get('topic'), str) or not expect['topic']:
            raise ValueError('관련성의 topic과 요구 수준 related/direct가 필요합니다')
        judgments = expect.get('judgments')
        if not isinstance(judgments, list):
            raise ValueError('judgments는 사전에 고정한 원문별 관련성 판정 목록입니다')
        seen = set()
        for item in judgments:
            if (not isinstance(item, dict) or not isinstance(item.get('page_id'), str) or not item['page_id']
                    or item.get('level') not in LEVELS or not isinstance(item.get('reason'), str) or not item['reason']
                    or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('body_sha256') or ''))):
                raise ValueError('관련성 판정에는 page_id·본문 SHA-256·level·reason이 필요합니다')
            key = (item['page_id'].replace('-', ''), item['body_sha256'])
            if key in seen:
                raise ValueError('같은 본문의 관련성 판정을 중복할 수 없습니다')
            seen.add(key)
            validate_support(item)


def check(expect: dict, response: dict) -> str | None:
    kind = expect['type']
    if kind in {'displayed_issue_ids', 'followup_issue_ids'}:
        actual = (displayed_ids(response.get('content') or '') if kind == 'displayed_issue_ids'
                  else response.get('followupIssueIds'))
        return None if actual == expect['ids'] else f'{kind}: 실제 {actual}, 기대 {expect["ids"]}'
    if kind == 'groups':
        if not any(s.get('source') == 'mantis_aggregation' and s.get('status') == 'ok'
                   and (s.get('meta') or {}).get('search_complete') is True for s in response.get('stages') or []):
            return '그룹 집계의 전체 조회 완료 증명이 없습니다'
        groups = (response.get('agent') or {}).get('aggregationResults') or []
        if not any(r.get('groups') == expect['values'] and r.get('total_count') == sum(expect['values'].values()) for r in groups):
            return '그룹 집계가 독립 정답과 다릅니다'
        for name, value in expect['values'].items():
            if not re.search(rf'^\|\s*{re.escape(name)}\s*\|\s*{value:,}\s*\|\s*$', response.get('content') or '', re.M):
                return f'표시한 그룹 건수 불일치: {name}={value}'
        return None
    if kind == 'current_body':
        reads = completed_reads(response)
        if not reads:
            return '현재 본문을 끝까지 읽었다는 증명이 없습니다'
        actual = {r['page_id'].replace('-', '') for r in reads}
        return None if {p.replace('-', '') for p in expect.get('page_ids', [])} <= actual else '필요한 페이지 본문 읽기 누락'
    return assess_relevance(expect, response)['message']


def summarize_requirements(turns: list[dict]) -> dict:
    measured = [t for t in turns if t.get('requirements')]
    items = [item for t in measured for item in t['requirements']]
    counts = Counter(item['status'] for item in items)
    by_id: dict[str, dict] = {}
    for item in items:
        entry = by_id.setdefault(item['id'], {'label': item['label'], 'passed': 0, 'total': 0, 'unassessed': 0})
        entry['total'] += 1
        entry['passed'] += item['status'] == 'pass'
        entry['unassessed'] += item['status'] == 'unassessed'
        for diagnostic in item.get('evidence_diagnostics') or []:
            codes = entry.setdefault('evidence_diagnostics', {})
            codes[diagnostic['code']] = codes.get(diagnostic['code'], 0) + 1
    complete = sum(t['passed'] and all(i['status'] == 'pass' for i in t['requirements']) and t['outcome'] not in
                   {'clarify', 'blocked', 'cancelled', 'error', 'not_run', 'incomplete'} for t in measured)
    return {'passed': counts['pass'], 'total': len(items), 'unassessed': counts['unassessed'],
            'complete_turns': complete, 'evaluated_turns': len(measured),
            'complete_rate': complete / len(measured) if measured else None, 'by_id': by_id}
