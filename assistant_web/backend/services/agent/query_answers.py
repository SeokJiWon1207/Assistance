"""원장의 조회 결과를 표시한다. 수치·목록·집계는 모델 초안에서 가져오지 않는다."""
from html import escape

from backend.reports.aggregate import COUNT_GROUP_LABELS
from backend.reports.aggregation_rollout import render_report
from backend.tools.report_contracts import REPORT_KINDS


def missing_query_results(plan: dict, ledger, related_ordinal=None, *, scope_id=None) -> list[str]:
    if plan.get('source') == 'notion':
        queries = ledger.notion_query_results()
        if plan.get('operation') == 'count' and not queries:
            return ['Notion 건수(count_notion_pages)']
        if plan.get('operation') in {'search', 'list'} and not any(r['operation'] == 'list' for r in queries):
            return ['Notion 목록(list_notion_pages)']
        return []
    if plan.get('operation') == 'report' and plan.get('report_kind') in REPORT_KINDS:
        return [] if any(r['purpose'] == 'answer' for r in ledger.reports) else ['보고서(build_issue_report)']
    if plan.get('query_scopes'):
        return [f"{s['id']} {s['label']}: {name}" for s in plan['query_scopes']
                for name in missing_query_results(s['plan'], ledger, scope_id=s['id'])]
    required = set()
    if plan.get('aggregation') or plan.get('group_by') and plan.get('operation') in {'count', 'aggregate'}:
        required.add('집계')
    elif plan.get('operation') == 'aggregate':
        required.add('집계')
    if plan.get('operation') in {'search', 'list'} or related_ordinal is not None:
        required.add('목록')
    if plan.get('operation') == 'count' and not required:
        required.add('건수')
    if scope_id:
        required.add('건수')  # 중복 제외 합계는 전체 일치 ID 집합이 필요하다.
    facts = ledger.query_results(scope_id) if scope_id else ledger.query_results()
    if facts:
        required.discard('건수')
    if any(f['operation'] == 'list' for f in facts):
        required.discard('목록')
    if (ledger.aggregation_results(scope_id) if scope_id else ledger.aggregation_results()):
        required.discard('집계')
    return sorted(required)


def render_aggregation(question: str, plan: dict, entry: dict) -> dict:
    if entry['kind'] == 'period':
        return render_report(question, plan, entry['result'])
    group_by = entry['group_by']
    groups = entry['groups']
    keys = sorted(groups) if group_by == 'month' else sorted(groups, key=lambda key: (-groups[key], key))
    lines = [f"적용 조건에 맞는 Mantis 이슈는 {entry['total_count']}건입니다.",
             f"{COUNT_GROUP_LABELS[group_by]}별 건수", '', '| 그룹 | 건수 |', '| --- | --- |']
    lines.extend(f"| {escape(key).replace('|', '&#124;').replace(chr(10), ' ')} | {groups[key]} |" for key in keys)
    return {'content': '\n'.join(lines)}
