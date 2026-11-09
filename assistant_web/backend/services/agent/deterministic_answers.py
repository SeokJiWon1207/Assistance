"""도구에서 계산·확인한 결과를 기존 최종 검증기에 전달한다."""
from html import escape

from backend.notion.formatting import format_deterministic_notion_answer


PROPERTY_LABELS = {'title': '제목', 'status': '상태', 'owner': '담당자', 'due_date': '기한',
                   'deployment_date': '배포일', 'created_time': '등록일', 'last_edited_time': '수정일',
                   'database_label': 'DB', 'properties_text': '전체 속성'}


def render_notion_results(question: str, retrieval: dict, limit: int) -> str:
    stages = retrieval.get('stages') or []
    queries = [s for s in stages if s.get('meta', {}).get('notion_query_result')]
    parts = []
    if queries:
        stage = queries[-1]
        entry = stage['meta']['notion_query_result']
        if entry['total_count'] is not None:
            parts.append(f"적용 조건에 맞는 Notion 항목은 {entry['total_count']}건입니다.")
        else:
            parts.append(f"전체 조회가 끝나지 않아 총건수와 부재를 확정할 수 없습니다. 확인한 범위는 {entry['observed_count']}건입니다.")
        parts.append(format_deterministic_notion_answer(question, {'stages': [stage]}, limit))
    for stage in stages:
        result = stage.get('meta', {}).get('property_result')
        if not result:
            continue
        lines = [f"{escape(str(result['title']), quote=False)} [{result['page_ref']}] — 현재 속성"]
        lines.extend(f"- {PROPERTY_LABELS[field]}: {escape(str(value), quote=False) if value else '미기재'}"
                     for field, value in result['values'].items())
        parts.append('\n'.join(lines))
    return '\n\n'.join(parts)


def notion_result_check(question: str, content: str, retrieval: dict) -> dict:
    plan = retrieval.get('intent_plan') or retrieval.get('query_plan') or {}
    expected = render_notion_results(question, retrieval, plan.get('limit') or retrieval.get('limit') or 5)
    valid = bool(expected and content == expected)
    from backend.tools.notion_queries import complete_count
    for stage in retrieval.get('stages') or []:
        entry = stage.get('meta', {}).get('notion_query_result')
        if entry:
            complete = complete_count(stage)
            valid = valid and entry['total_count'] == (stage['meta']['candidate_count'] if complete else None)
        prop = stage.get('meta', {}).get('property_result')
        if prop:
            page: dict = next((p for p in stage.get('notion_pages') or [] if p['id'] == prop['page_id']), {})
            valid = valid and bool(page.get('_verified_properties')) and all(
                value == (page.get(name) or '') for name, value in prop['values'].items())
    return {'id': 'agent_notion_result', 'label': 'Notion 건수·목록·속성 대조', 'status': 'pass' if valid else 'fail',
            'detail': '조회 완전성·원장 수치·현재 속성을 서버 생성 답변과 대조'}
