"""검증 모드를 보존한 응답 부분의 조합. 모델 초안으로 수치 결과를 다시 쓰지 않는다."""
from copy import deepcopy


def unique_sources(sources: list[dict]) -> list[dict]:
    result = []
    seen = set()
    for source in sources:
        key = (source.get('type'), source.get('issue_id'), source.get('page_id'), source.get('url'))
        if key not in seen:
            result.append(source)
            seen.add(key)
    return result


def compose_parts(parts: list[tuple[str, dict]], retrieval: dict, footer: str = '') -> dict:
    """각 부분의 원천·검증 모드를 보존하며 최종 표시와 검증 입력을 함께 만든다."""
    entries = [{'kind': kind, 'content': response['content'], 'mode': response['raw']['mode'],
                'sources': deepcopy(response.get('sourcesForFollowUps') or []),
                'retrieval': deepcopy(response.get('retrieval')), 'checks': deepcopy(response.get('agentChecks') or [])}
               for kind, response in parts]
    ordered = sorted(parts, key=lambda part: 0 if part[0] == 'query' else 2 if part[0] == 'explanation' else 1)
    sources = unique_sources([source for _, response in ordered for source in response.get('sourcesForFollowUps') or []])
    listed = next((response.get('sourcesForFollowUps') or [] for kind, response in parts if kind == 'query'), None)
    return {'content': '\n\n'.join([e['content'].rstrip() for e in entries] + ([footer] if footer else [])),
            'retrieval': {**retrieval, 'answer_parts': entries, 'answer_footer': footer},
            # 목록의 표시 순서는 설명의 인용 순서로 바꾸지 않는다.
            'sources': sources, 'sourcesForFollowUps': listed if listed is not None else sources}
