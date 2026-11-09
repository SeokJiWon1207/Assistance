"""기존 API 창 수집기를 이용한 필터 적용 전 최근 N건 모집단."""
from collections import Counter

from backend.query.ordering import issue_order_key
from backend.sources.mantis import collect_mantis_live_ids_in_range
from backend.sources.mantis_fields import normalize_issue


def query_population(constraints: dict, limit: int, display_limit: int, group_key=None) -> dict:
    collected = collect_mantis_live_ids_in_range(constraints, max_pages=(limit + 99) // 100, scan_limit=limit)
    rows = [normalize_issue(row) for row in collected.get('issues', {}).values()]
    ordering = constraints['sort']
    rows.sort(key=lambda row: issue_order_key(row, ordering['field']), reverse=ordering['direction'] == 'desc')
    complete = collected.get('status') == 'ok' and collected.get('complete') is True
    groups = {row['id']: str(group_key(row) or '미지정') for row in rows} if group_key else {}
    return {'source': 'mantis_snapshot', 'status': 'ok', 'issues_list': rows[:display_limit],
            'meta': {'search_complete': complete, 'found_count': len(rows),
                     'matched_issue_ids': [row['id'] for row in rows],
                     'ordered_matched_ids': [row['id'] for row in rows],
                     'group_counts': dict(Counter(groups.values())), 'matched_groups': groups,
                     'population_limit': limit, 'population_basis': 'api_response_order',
                     'scanned_count': len(collected.get('scanned_ids') or []),
                     'incomplete_reasons': [] if complete else ['population_scan_incomplete']}}
