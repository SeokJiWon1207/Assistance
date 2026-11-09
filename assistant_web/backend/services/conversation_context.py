"""서버에 저장된 대화의 제한된 요약. 현재 요청의 근거 원장에는 등록하지 않는다."""
import json
from typing import Any

from backend.audit import load_audit_turn
from backend.tools.redaction import redact

MAX_TURNS = 3
MAX_CONTEXT_CHARS = 8000



def _source(source: dict, position: int) -> dict:
    result = {key: source[key] for key in ('type', 'issue_id', 'page_id', 'evidence_grade') if key in source}
    result.update(position=position, title=str(source.get('title') or '')[:160],
                  excerpt=str(source.get('preview') or '')[:240])
    return result


def _retrieval(stages: list) -> list[dict]:
    results = []
    for stage in stages[:8]:
        if not isinstance(stage, dict):
            continue
        meta = stage.get('meta') or {}
        complete = meta.get('search_complete')
        if stage.get('status') not in {'ok', 'empty'}:
            complete = False
        count = meta.get('total_count', meta.get('found_count'))
        results.append({'source': stage.get('source'), 'status': stage.get('status'),
                        'search_complete': complete if isinstance(complete, bool) else None,
                        'total_count': count if complete is True and type(count) is int and count >= 0 else None,
                        'as_of': meta.get('fetchedAt') or meta.get('snapshot_as_of'),
                        'reason': str(stage.get('reason') or meta.get('incomplete_reason') or '')[:160]})
    return results


def _summarize(payload: dict, enabled: dict, *, latest: bool) -> dict:
    # 값별 마스킹 후 자른다. 원문을 먼저 자르면 일부만 남은 인증정보를 놓칠 수 있다.
    plan = payload['queryPlan']
    sources = payload.get('followupSources', payload.get('sources')) or []
    scoped = [s for s in sources if isinstance(s, dict) and s.get('evidence_grade') != 'reference']
    shown = [_source(redact(s), index + 1) for index, s in enumerate(scoped)
             if enabled.get(s.get('type'), True)]
    references = [_source(redact(s), 0) for s in payload.get('sources') or []
                  if isinstance(s, dict) and s.get('evidence_grade') == 'reference' and enabled.get(s.get('type'), True)]
    return redact({'question': redact(str(payload['question']))[:1000], 'recorded_at': payload.get('recorded_at'),
                   'task': {key: plan.get(key) for key in ('source', 'operation', 'report_kind', 'terms', 'sort', 'limit')},
                   'applied_conditions': {key: value for key, value in (plan.get('resolved_constraints') or {}).items()
                       if key in {'filter_clauses', 'mantis_team_scope', 'mantis_team_label', 'statuses', 'excluded_statuses'}},
                   'displayed_sources': shown[:20 if latest else 4],
                   'displayed_source_count': len(scoped), 'sources_truncated': len(shown) > (20 if latest else 4),
                   'reference_sources': references[:3] if latest else [],
                   'retrieval': _retrieval(payload.get('retrievalStages') or [])})


def build_conversation_context(audit_id: str, user_uid: str, expected_question: str, *, variant: str = 'A',
                               enabled_tools: dict | None = None) -> dict[str, Any]:
    """클라이언트 답변/조건을 받지 않고 서버가 연결한 부모 턴만 제한적으로 읽는다."""
    turns: list[dict[str, Any]] = []
    seen = set()
    while audit_id and audit_id not in seen and len(turns) < MAX_TURNS:
        seen.add(audit_id)
        payload = load_audit_turn(audit_id, user_uid, expected_question, variant=variant)
        if not payload:
            break
        turns.append(_summarize(payload, enabled_tools or {}, latest=not turns))
        parent = payload['queryPlan'].get('conversation_parent') or {}
        audit_id = str(parent.get('audit_id') or '')
        expected_question = str(parent.get('question') or '')
        variant = str(parent.get('variant') or 'A')
    if not turns:
        return {}
    context: dict[str, Any] = {'historical_only': True, 'turns': list(reversed(turns))}
    # 항목은 앞 순서부터 남기며 잘림을 명시한다. JSON 중간을 문자열로 자르지 않는다.
    while len(json.dumps(context, ensure_ascii=False)) > MAX_CONTEXT_CHARS:
        removable = next((turn for turn in context['turns'] if turn['reference_sources'] or turn['displayed_sources']), None)
        if removable is not None:
            (removable['reference_sources'] or removable['displayed_sources']).pop()
            removable['sources_truncated'] = True
        elif len(context['turns']) > 1:
            context['turns'].pop(0)
        else:
            return {}
    return context
