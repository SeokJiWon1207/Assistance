"""표시된 수량·순번을 모델 출력과 독립적으로 원천 스캔의 집합·정렬 순서에 대조한다."""
import re
from typing import Any


def verify_query_results(content: str, stages: list[dict[str, Any]], requested: int | None) -> dict[str, str]:
    queries = [stage for stage in stages if stage.get("source") == "mantis_snapshot"
               and stage.get("meta", {}).get("query_result")]
    check = {"id": "count", "label": "원천 건수·표시 순서", "status": "fail", "detail": "검증할 조회가 없습니다."}
    if not queries:
        return check
    meta = queries[-1]["meta"]
    total = meta.get("found_count")
    complete = meta.get("search_complete") is True
    match = re.search(r"^적용 조건에 맞는 Mantis 이슈는 (\d+)건입니다\.", content)
    if not complete:
        check.update(status="fail" if match else "warning", detail="전체 조회 미완료 · 총건수 미확정")
        return check
    matched = meta.get("matched_issue_ids")
    if (not match or int(match[1]) != total or not isinstance(matched, list) or len(set(matched)) != total
            or any(stage["meta"]["query_result"]["total_count"] != total for stage in queries)):
        check["detail"] = "답변 건수가 원천의 전체 일치 ID 수와 다릅니다."
        return check
    listed = next((s for s in reversed(queries) if s["meta"]["query_result"]["operation"] == "list"), None)
    if listed:
        result_ids = listed["meta"]["query_result"]["ordered_ids"]
        rows = [r["id"] for r in listed["issues_list"]]
        displayed = [int(x) for x in re.findall(r"^\d+\. #(\d+)\s", content, re.M)]
        if not (displayed == rows == result_ids == listed["meta"].get("ordered_matched_ids")
                and (not requested or len(displayed) == min(requested, total))):
            check["detail"] = "표시한 이슈 수 또는 순서가 원천의 정렬 결과와 다릅니다."
            return check
    check.update(status="pass", detail=f"전체 {total}건 · 표시한 ID와 정렬 순서 대조 완료")
    return check


def verify_agent_aggregation(question: str, content: str, retrieval: dict, mode: str) -> dict:
    from collections import Counter
    from datetime import datetime
    from backend.evaluation.aggregation import verify_aggregation
    from backend.intent.execution_contract import validate_execution_contract
    from backend.services.agent.query_answers import render_aggregation
    stages = retrieval.get('stages') or []
    entries = [s['meta']['aggregation_result'] for s in stages if s.get('meta', {}).get('aggregation_result')]
    plan = retrieval.get('query_plan') or retrieval.get('intent_plan') or {}
    entry = entries[-1] if entries else None
    if entry and entry['kind'] == 'period':
        report = verify_aggregation(question, content, retrieval, mode)
        checks = report['checks']
        source_ok = entry['result'] == retrieval.get('aggregation_result')
    else:
        checks = validate_execution_contract(question, plan)['checks']
        report = {'constraints': retrieval.get('constraints') or {}, 'queryPlan': plan,
                  'trace': stages, 'sourceCount': 0, 'mode': mode, 'verifiedAt': datetime.now().astimezone().isoformat()}
        source_ok = bool(entry and entry['kind'] == 'simple'
            and len(set(entry['matched_issue_ids'])) == entry['total_count']
            and set(map(str, entry['matched_issue_ids'])) == set(map(str, entry['matched_groups']))
            and dict(Counter(entry['matched_groups'].values())) == entry['groups'])
    valid = bool(entry and source_ok and render_aggregation(question, plan, entry)['content'] in content
                 and all(c['status'] != 'fail' for c in checks))
    checks.append({'id': 'agent_aggregation_result', 'label': '집계 원장·표 대조',
                   'status': 'pass' if valid else 'fail', 'detail': '서버 계산 원장·전체 조회·표시 수치 대조'})
    if any(s.get('meta', {}).get('query_result') for s in stages):
        from backend.intent.display_contract import expected_display_limit
        check = verify_query_results(content, stages, expected_display_limit(question, plan))
        checks.append({**check, 'id': 'agent_query_result'})
    status = 'fail' if any(c['status'] == 'fail' for c in checks) else 'warning' if any(c['status'] == 'warning' for c in checks) else 'pass'
    score = round(100 * sum(c['status'] == 'pass' for c in checks) / len(checks))
    return {**report, 'status': status, 'score': score, 'checks': checks, 'completedDimensions': 1, 'applicableDimensions': 1,
            'dimensions': [{'id': 'aggregation', 'label': '집계와 설명', 'status': status, 'score': score, 'checks': checks}]}
