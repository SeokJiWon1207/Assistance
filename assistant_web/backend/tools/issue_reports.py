"""검증된 보고서 목적·조건으로 기존 계산기를 호출하는 읽기 전용 도구."""
from copy import deepcopy
from typing import Literal

from backend.tools.query_conditions import QueryArguments
from backend.tools.registry import ToolContext, ToolSpec, error_result
from backend.tools.report_builders import calculate_report
from backend.tools.report_evidence import evidence_stages


class ReportArgs(QueryArguments):
    kind: Literal['effort', 'weekly', 'monthly', 'profile', 'sla', 'effort_sla', 'similar', 'triage',
                  'priority', 'aging', 'due', 'comparison', 'completed', 'waiting', 'customers', 'pivot', 'spike']
    effort: dict | None = None
    options: dict | None = None


def report_outcome(report: dict, complete: bool) -> str:
    stages = report.get('retrieval', {}).get('stages') or []
    counts = [s.get('meta', {}).get(key) for s in stages
              for key in ('matched_count', 'found_count', 'current_count', 'duration_count')]
    known = [n for n in counts if type(n) is int]
    observed = max(known, default=len(report.get('sources') or []))
    if complete:
        return 'complete_empty' if known and observed == 0 else 'sufficient_evidence'
    return 'partial_evidence' if observed else 'search_incomplete'


def build_report(args: ReportArgs, context: ToolContext) -> dict:
    contract = context.query_contracts.get('mantis')
    expected = contract.report if contract else None
    if (not contract or not expected or args.kind != expected['kind'] or args.effort != expected['effort']
            or args.options != expected.get('options') or contract.population_limit or contract.conditions.grade != 'scoped'):
        return error_result('build_issue_report', 'condition_mismatch', '검증된 report.kind·effort·options를 그대로 사용하세요. 모집단 제한 보고서는 지원하지 않습니다.')
    if args.kind == 'similar' and not context.enabled_sources.get('vectorDb'):
        return error_result('build_issue_report', 'source_disabled', '유사 사례 검색 도구가 꺼져 있습니다.')
    if args.kind == 'similar' and len(contract.conditions.constraints.get('issue_ids') or []) != 1:
        return error_result('build_issue_report', 'condition_mismatch', '유사 비교의 기준 이슈 한 건을 지정하세요.')
    report = calculate_report(args, context, contract)
    if not report:
        return error_result('build_issue_report', 'report_unavailable', '보고서의 기간·대상을 확인하지 못했습니다.')
    stages = report.get('retrieval', {}).get('stages') or []
    complete = bool(stages and all(s.get('status') == 'ok' and s.get('meta', {}).get('search_complete') is True for s in stages))
    if args.kind in {'similar', 'triage'}:
        report['content'] += '\n\n참고 사례는 제한된 후보 검색 결과이며 전체 부재나 동일 원인을 확정하지 않습니다.'
    elif not complete:
        report['content'] = '전체 조회를 마치지 못했습니다. 아래 보고서는 확인된 범위의 결과이며 전체 건수·공수나 부재를 확정하지 않습니다.\n\n' + report['content']
    for stage in report.get('retrieval', {}).get('stages') or []:
        copied = deepcopy(stage)
        copied.setdefault('meta', {})['report_result'] = {'kind': args.kind, 'effort': deepcopy(args.effort),
            'applied_filters': deepcopy(contract.filters), 'scope': contract.scope, 'options': deepcopy(args.options)}
        for evidence, grade, unapplied in evidence_stages(copied, contract):
            context.ledger.add('mantis', evidence, grade=grade, unapplied=unapplied, call_id=context.call_id)
    context.ledger.reports.append({'condition_ref': contract.ref, 'purpose': contract.purpose,
                                   'outcome': report_outcome(report, complete), 'report': deepcopy(report)})
    return {'ok': True, 'tool': 'build_issue_report', 'untrusted_content': True, 'evidence_grade': 'scoped',
            'kind': args.kind, 'search_complete': complete, 'report_contract': deepcopy(args.options or {}),
            'results': [{'content': report['content']}],
            'note': '서버 계산 결과입니다. 최종 응답에서 기존 보고서 검증기로 재검증합니다. 필요한 이슈 상세나 관련 문서는 별도 도구로 읽으세요.'}


SPECS = [ToolSpec('build_issue_report', '보고서·고객사 프로필·처리시간·유사 사례·초동 대응·특수 업무조회를 검증된 report 계약으로 실행한다. kind·effort·options를 그대로 지정한다. 숫자는 서버가 계산한다.',
                  ReportArgs, 'mantis', build_report)]
