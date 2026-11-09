"""단계 조건 참조와 수치를 서버 원장에 대조한다. 기존 최종 답변 검증에 추가한다."""
from collections import Counter

from backend.tools.query_conditions import _same
from backend.tools.step_conditions import step_view


def _same_filters(left: list, right: list) -> bool:
    return (len(left) == len(right) and all(any(_same(c, other) for other in right) for c in left)
            and all(any(_same(c, other) for other in left) for c in right))


def _query_valid(stage: dict, entry: dict, contract) -> bool:
    from backend.intent.aggregation_spec import period_clause
    meta = stage['meta']
    expected = list(contract.filters)
    if contract.aggregation and contract.aggregation.get('period'):
        expected.append(period_clause(contract.aggregation, 'issue_count'))
    if (not _same_filters(entry.get('applied_filters') or [], expected)
            or entry.get('team_scope') != contract.scope or entry.get('sort') != contract.sort
            or entry.get('search_complete') is not meta.get('search_complete')
            or entry.get('population_limit') != contract.population_limit):
        return False
    if not entry.get('search_complete'):
        return entry.get('total_count') is None
    ids = meta.get('matched_issue_ids')
    if (not isinstance(ids, list) or entry.get('total_count') != len(set(ids))
            or meta.get('found_count') != entry['total_count']):
        return False
    return (entry.get('ordered_ids') == [r['id'] for r in stage.get('issues_list') or []]
            == meta.get('ordered_matched_ids') and set(entry['ordered_ids']).issubset(ids))


def _aggregation_valid(entry: dict, contract) -> bool:
    if not _same_filters(entry.get('applied_filters') or [], contract.filters):
        return False
    if entry.get('kind') == 'simple':
        ids, groups = entry.get('matched_issue_ids') or [], entry.get('matched_groups') or {}
        return (entry.get('total_count') == len(set(ids)) and set(map(str, ids)) == set(map(str, groups))
                and dict(Counter(groups.values())) == entry.get('groups'))
    result = entry.get('result') or {}
    return bool(result.get('source', {}).get('search_complete') is True and result.get('totals')
                and all(abs(sum(r['values'][metric] for r in result['rows']) - total) < .0001
                        for metric, total in result['totals'].items()))


def verify_condition_steps(ledger, contracts: dict) -> dict:
    from backend.tools.report_evidence import valid_report_evidence
    valid = all(0 <= index < len(ledger.stages) and ledger.stages[index].stage.get('meta', {}).get(
        'condition_step', {}).get('condition_ref') == audit['condition_ref']
        for audit in ledger.condition_steps for index in audit['stage_indices'])
    for stage in ledger.stages:
        meta = stage.stage.get('meta') or {}
        step = meta.get('condition_step')
        if not step:
            continue  # 이전 비구조화 경로는 기존 검증을 그대로 사용한다.
        contract = contracts.get(step.get('condition_ref'))
        if not contract or step != step_view(contract, contract.conditions) or not valid_report_evidence(stage, contract):
            valid = False
            break
        prefix = 'investigation_' if contract.purpose == 'investigate' else 'comparison_' if contract.scope_id else ''
        if any(meta.get(p + key) for p in ('', 'investigation_', 'comparison_') if p != prefix
               for key in ('query_result', 'aggregation_result', 'notion_query_result', 'report_result')):
            valid = False
            break
        query = meta.get(prefix + 'query_result')
        aggregate = meta.get(prefix + 'aggregation_result')
        if query and not _query_valid(stage.stage, query, contract) or aggregate and not _aggregation_valid(aggregate, contract):
            valid = False
            break
        notion = meta.get(prefix + 'notion_query_result')
        report = meta.get(prefix + 'report_result')
        if notion:
            from backend.tools.notion_queries import complete_count
            complete = complete_count(stage.stage)
            if (not _same_filters(notion.get('applied_filters') or [], contract.filters)
                    or notion.get('search_complete') is not complete or notion.get('sort') != contract.sort
                    or notion.get('terms') != contract.terms
                    or notion.get('total_count') != (meta.get('candidate_count') if complete else None)
                    or notion.get('ordered_page_ids') != [p['id'] for p in stage.stage.get('notion_pages') or []]):
                valid = False
        if report and (not contract.report or report['kind'] != contract.report['kind']
                       or report['effort'] != contract.report['effort'] or report['scope'] != contract.scope
                       or report.get('options') != contract.report.get('options')
                       or not _same_filters(report['applied_filters'], contract.filters)):
            valid = False
    return {'id': 'agent_step_conditions', 'label': '단계별 조건·수치 경계',
            'status': 'pass' if valid else 'fail',
            'detail': f'{len(ledger.condition_steps)}개 단계의 조건 참조·조회 완전성·수치 대조' if valid else '단계 조건 또는 수치 원장 불일치'}
