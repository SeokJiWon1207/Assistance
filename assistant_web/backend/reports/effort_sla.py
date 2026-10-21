"""동일한 확정 조건의 공수와 처리시간을 기존 계산기로 함께 제공한다."""
from copy import deepcopy
from backend.reports.effort import build_effort_report
from backend.reports.sla import build_sla_report


def build_effort_sla_report(text: str) -> dict | None:
    effort = build_effort_report(text)
    sla = build_sla_report(text)
    if not effort or not sla or effort.get('needsPerson'):
        return None
    sources = {s['issue_id']: s for s in [*effort.get('sources', []), *sla.get('sources', [])]}
    retrieval = deepcopy(effort.get('retrieval') or {})
    retrieval['stages'] = [*retrieval.get('stages', []), *(sla.get('retrieval') or {}).get('stages', [])]
    retrieval['effort_component_mode'] = effort.get('raw', {}).get('mode')
    retrieval['effort_source_ids'] = [s['issue_id'] for s in effort.get('sources', [])]
    return {'content': '## 공수\n\n' + effort['content'] + '\n\n## 처리시간\n\n' + sla['content'],
            'sources': list(sources.values()), 'sourcesForFollowUps': list(sources.values()),
            'retrieval': retrieval, 'raw': {'mode': 'deterministic_effort_sla_report'}}
