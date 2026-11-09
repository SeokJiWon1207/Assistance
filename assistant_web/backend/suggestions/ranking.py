"""근거가 있는 업무만 합치고 서버가 표시 문구를 작성한다."""
from datetime import datetime
from backend.evidence.stages import STAGES, stage_events
from backend.suggestions.contracts import Action, Candidate, EvidenceBatch, Suggestion
from backend.suggestions.sources import digest

ACTIONS = {
    'review_progress': '현재 상태와 다음 할 일을 정리해 주세요.',
    'summarize_changes': '최근 기록에서 변경된 내용과 추가 확인 사항을 정리해 주세요.',
    'compare_records': '두 자료의 날짜와 업무 단계를 구분하여 진행 상태를 대조해 주세요. 차이를 오류로 단정하지 마세요.',
    'prepare_checklist': '현재 근거에 기반한 확인 목록을 작성해 주세요. 재현 조건, 기대 결과, 확인이 필요한 사항을 구분해 주세요.',
}


def ranked_candidates(batch: EvidenceBatch) -> list[Candidate]:
    unique = {c.dedupeKey: c.model_copy(deep=True) for c in batch.candidates}
    nodes = list(unique.values())
    by_page = {c.sources[0].id: c for c in nodes if c.sources[0].type == 'notion'}
    absorbed = set()
    for item in nodes:
        if item.sources[0].type != 'mantis':
            continue
        for page_id in item.facts.get('notionIds', []):
            page = by_page.get(page_id)
            if page is None or page.id in absorbed or len(item.sources) >= 4:
                continue
            item.sources.extend(page.sources)
            item.facts.setdefault('passages', []).extend(page.facts.get('passages', []))
            item.kind = 'compare_records'
            item.allowedActions = ['compare_records']
            if any(s.grade == 'body' for s in item.sources):
                item.allowedActions.append('prepare_checklist')
            item.priority = max(item.priority, page.priority)
            item.revision = digest([(s.type, s.id, s.revision) for s in item.sources])
            absorbed.add(page.id)
    for item in nodes:
        if any(s.grade == 'body' for s in item.sources):
            pending = any(state in {'pending', 'failed'} for passage in item.facts.get('passages', [])
                          for stage in STAGES for state, _, _ in stage_events(stage, {'text': passage['text']}))
            if pending:
                item.facts['reason'] = 'followup'
                item.priority = max(item.priority, 3)
                if item.kind == 'review_progress' and 'prepare_checklist' in item.allowedActions:
                    item.kind = 'prepare_checklist'
    # 동점에서 원천을 번갈아 고른다. 중요도가 낮은 항목을 원천 quota로 끌어올리지 않는다.
    ordered = sorted((c for c in nodes if c.id not in absorbed),
                     key=lambda c: (-c.priority, -c.relevance, -len(c.sources), c.id))
    result: list[Candidate] = []
    while ordered:
        first = ordered[0]
        same = [c for c in ordered if (c.priority, c.relevance, len(c.sources)) == (first.priority, first.relevance, len(first.sources))]
        last_source = result[-1].sources[0].type if result else ''
        chosen = next((c for c in same if c.sources[0].type != last_source), first)
        result.append(chosen)
        ordered.remove(chosen)
    return result


def render_prompt(item: Suggestion) -> str:
    targets = '\n'.join(f'- Mantis #{s.id}' if s.type == 'mantis' else f'- Notion: {s.url}' for s in item.sources)
    return (f'{targets}\n\n최신 원문을 다시 조회하고 확인한 근거를 표시해 주세요.\n'
            f'{ACTIONS[item.kind]}\n원문에 적힌 사실과 계획, 추가 확인이 필요한 사항을 구분하고 기록이 없다는 이유만으로 업무 상태를 추측하지 마세요.')


def build_suggestions(batch: EvidenceBatch, *, now: datetime, choices: list[tuple[str, Action]] | None = None, limit: int = 3) -> list[Suggestion]:
    candidates = ranked_candidates(batch)
    if choices:
        positions = {key: i for i, (key, _) in enumerate(choices)}
        candidates.sort(key=lambda c: positions.get(c.id, len(positions)))
        actions = dict(choices)
        for c in candidates:
            if actions.get(c.id) in c.allowedActions:
                c.kind = actions[c.id]
    result = []
    for c in candidates:
        if not c.sources or min(s.validUntil for s in c.sources) <= now:
            continue
        reason = str(c.facts.get('reason') or 'progress')
        summary = ' '.join(c.sources[0].title.split())
        summary = summary[:59].rstrip() + '…' if len(summary) > 60 else summary
        label = f'「{summary}」 #{c.sources[0].id} 이슈' if c.sources[0].type == 'mantis' else f'「{summary}」'
        if c.kind == 'compare_records':
            title = f'{label}와 Notion 문서의 진행 상태를 대조해요'
        elif c.kind == 'prepare_checklist':
            title = f'{label}의 확인 목록을 정리해요'
        else:
            suffix = {'overdue': '목표일이 지났어요. 남은 작업을 확인해요', 'new_note': '새 노트가 확인됐어요. 변경 내용을 살펴봐요',
                      'aging': '오래 진행 중이에요. 다음 할 일을 확인해요', 'due_review': '문서의 기한과 진행 상태를 확인해요',
                      'followup': '기록된 후속 확인 항목을 정리해요'}.get(reason, '현재 상태와 다음 할 일을 확인해요')
            title = f'{label} {suffix}'
        item = Suggestion(id='s-' + digest([c.dedupeKey, c.revision])[:32], kind=c.kind, title=title,
            promptPreview='', reasonCode=reason, revision=c.revision, dedupeKey=c.dedupeKey, sources=c.sources,
            generatedAt=now, expiresAt=min(s.validUntil for s in c.sources))
        item.promptPreview = render_prompt(item)
        result.append(item)
        if len(result) == limit:
            break
    return result
