"""명시적인 원문 관계로 연결된 같은 프로젝트의 복수 이슈 선택을 검증한다."""
from typing import Any


def validate_related_selection(grounding: dict[str, Any], selected: list[int]) -> bool:
    """선택된 이슈 안에서만 원문의 무방향 related-to 그래프 연결성을 확인한다."""
    if (not isinstance(selected, list) or not selected
            or any(type(issue_id) is not int or issue_id <= 0 for issue_id in selected)
            or len(set(selected)) != len(selected)):
        return False
    issues = {issue['id']: issue for issue in grounding.get('issues', [])
              if isinstance(issue, dict) and type(issue.get('id')) is int}
    if any(issue_id not in issues for issue_id in selected):
        return False
    if len(selected) == 1:
        return True
    project = issues[selected[0]].get('project')
    if (not isinstance(project, str) or not project.strip() or project.strip() in {'미상', '기타/미상'}
            or any(issues[issue_id].get('project') != project for issue_id in selected)):
        return False
    neighbors: dict[int, set[int]] = {issue_id: set() for issue_id in selected}
    for issue_id in selected:
        for field in ('related_issue_ids', 'source_reference_ids'):
            related = issues[issue_id].get(field)
            for other_id in related if isinstance(related, list) else []:
                if type(other_id) is int and other_id in neighbors:
                    neighbors[issue_id].add(other_id)
                    neighbors[other_id].add(issue_id)
    reached: set[int] = set()
    pending = [selected[0]]
    while pending:
        issue_id = pending.pop()
        if issue_id not in reached:
            reached.add(issue_id)
            pending.extend(neighbors[issue_id] - reached)
    return reached == set(selected)
