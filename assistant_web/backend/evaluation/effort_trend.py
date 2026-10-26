"""월별 표시값을 원문 날짜·기입 공수와 독립적으로 대조한다."""
import re
from decimal import Decimal
from typing import Any

from backend.query_filters import matches_source_filters
from backend.reports.effort import EFFORT_FIELD_LABELS, parse_effort_field_value


def trend_checks(content, retrieval):
    stage: dict[str, Any] = next((s for s in (retrieval or {}).get('stages', []) if s.get('source') == 'mantis_effort_trend'), {})
    meta = stage.get('meta') or {}
    chart, facts = meta.get('trend') or {}, meta.get('facts')
    if stage.get('status') != 'ok' or not chart or facts is None:
        return [{'id': 'trend_evidence', 'label': '월별 집계 근거', 'status': 'warning',
                 'detail': '검증할 전체 저장본과 집계 근거를 확인하지 못했습니다.'}]
    periods = chart.get('periods', [])
    expected: dict[str, dict[str, list[Any]]] = {s['label']: {p: [0, Decimal(0)] for p in periods} for s in chart['series']}
    clauses = [f for f in (retrieval.get('constraints') or {}).get('filter_clauses', []) if f['field'] == 'effort_person']
    for fact in facts:
        values = expected.get(fact['issue_type'], {})
        created = fact['created_at'][:7].replace('-', '')
        delivered = fact['delivered_at'][:7].replace('-', '')
        if fact['counted'] and created in values:
            values[created][0] += 1
        if fact['effort_included'] and delivered in values:
            for field, raw in fact['effort_fields'].items():
                if field in EFFORT_FIELD_LABELS:
                    for person, amount in parse_effort_field_value(raw):
                        if matches_source_filters('mantis', {'effort_person': person}, clauses):
                            values[delivered][1] += Decimal(str(amount))
    cells_ok = all(len(s[key]) == len(periods) for s in chart['series'] for key in ('counts', 'efforts'))
    for series in chart['series']:
        for i, period in enumerate(periods):
            count, amount = expected[series['label']][period]
            if i >= len(series['counts']) or i >= len(series['efforts']):
                cells_ok = False
                continue
            cells_ok &= series['counts'][i] == count and abs(series['efforts'][i] - float(round(amount, 2))) < 0.00001
    display_ok = True
    if facts:
        for series in chart['series']:
            match = re.search(r'- ' + re.escape(series['label']) + r': 등록 \*\*(\d+)건\*\* · 공수 \*\*([0-9.]+)\*\*', content)
            display_ok &= bool(match and int(match[1]) == sum(series['counts']) and abs(float(match[2]) - round(sum(series['efforts']), 2)) < .00001)
    else:
        display_ok = '해당하는 이슈가 없습니다' in content or '모두 이슈 유형' in content
    unique = len({f['issue_id'] for f in facts}) == len(facts)
    return [
        {'id': 'trend_cells', 'label': '월별 원문 재합산', 'status': 'pass' if cells_ok else 'fail',
         'detail': f'{len(periods) * len(chart["series"]) * 2}개 월별 건수·공수 대조'},
        {'id': 'trend_display', 'label': '유형별 표시값', 'status': 'pass' if display_ok else 'fail',
         'detail': '본문 유형별 합계와 월별 합계 대조'},
        {'id': 'trend_issue_count', 'label': '집계 이슈 수', 'status': 'pass' if unique and len(facts) == chart['totalIssues'] else 'fail',
         'detail': f'중복 제거 원문 {len(facts)}건 · 집계 {chart["totalIssues"]}건'},
        {'id': 'trend_types', 'label': '이슈 유형 확인', 'status': 'warning' if chart.get('untypedCount') else 'pass',
         'detail': f'유형을 확인하지 못해 제외한 원문 {chart.get("untypedCount", 0)}건'},
    ]
