"""질문의 항목마다 검증된 같은 이슈의 근거로 직접 답한다."""
import re
from typing import Any

# 단계는 서로의 완료 증거를 대신하지 않는다.
_STAGES = {
    'implementation': ('개발·수정', r'(?:개발|구현|수정)(?!\s*(?:안내|가이드|본))', r'개발|구현|수정|로직'),
    'qc': ('내부 QC', r'QC|검수|내부\s*테스트', r'QC|검수|내부\s*테스트'),
    'delivery': ('고객사 전달', r'전달|발송', r'고객사?\s*전달|전달(?:\s*완료|했|하였|함|드|\s*드|\])|발송'),
    'customer_application': ('고객 측 적용', r'고객.{0,18}(?:적용|반영)', r'고객.{0,22}(?:적용|반영)'),
    'customer_validation': ('고객 검증', r'고객.{0,24}(?:검증|테스트|확인)|고객.{0,16}정상',
                            r'고객.{0,24}(?:검증|테스트|확인|정상)'),
    'production': ('운영 반영', r'운영|실서비스', r'(?:운영|실서비스).{0,28}(?:반영|적용|배포)'),
}
_DONE = re.compile(r'완료|통과|마쳤|끝났|끝났음|했(?:습니다|음|다|으며)|하였습니다|되었|됐|드렸|드립니다|전달함|확인함|처리함')
_PENDING = re.compile(r'[?？]|는지|인지|여부|예정|부탁|요청|필요|계획|진행\s*중|확인\s*중|검토\s*중|'
                      r'미확인|결과.{0,8}없|기록.{0,8}없|추정|생각|듯|가정|다면|으면|경우|완료\s*후.{0,50}(?:하겠|예정|요청|부탁)|할\s*수|가능')
_FAILED = re.compile(r'\bNG\b|실패|불합격|재요청|재검수|재발|취소|철회|'
                     r'완료.{0,15}(?:아님|아니|못|않)|(?:검증|확인|테스트).{0,15}(?:못했|못하|않았)', re.I)


def _order(row):
    return str(row.get('created_at') or ''), int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0


def _reference(row):
    from backend.evaluation.explanation_quotes import _label
    kind = f"노트 {_label(row['note_id'])}" if row.get('note_id') is not None else f"필드 {_label(row['field'])}"
    date = str(row.get('created_at') or '')[:10]
    return f"(#{row['issue_id']} · {kind}" + (f' · {_label(date)}' if date else '') + ')'


def _segments(row):
    # 전체 문장 안의 부정·계획·질문은 보존하고, 메일 인사나 다른 문장의 요청은 분리한다.
    for part in re.split(r'\n|(?<=[.!?])\s+', row['text'].replace('\r\n', '\n')):
        text = part.strip()
        if (text and len(text) <= 500 and not re.match(r'^(?:[A-Za-z]:[\\/]|\\\\|https?://)', text)
                and not re.match(r'^0*\d{5,7}\s*:', text)):
            yield text


def _completed_stage(stage, text):
    if stage == 'delivery':
        return bool(re.search(r'\[\s*(?:고객사\s*)?전달(?:\s*완료)?\s*\]|'
                              r'(?:전달|발송)\s*(?:완료|했|하였|함|되었|드렸|드립니다|드림|드렸)', text))
    if stage == 'implementation':
        return bool(re.search(r'(?:개발|구현|수정)(?:\s*(?:작업|처리|내용))?\s*'
                              r'(?:완료|했|하였|됨|되었|됐|처리)|(?:코드|로직).{0,25}(?:추가|수정)\s*(?:완료|했|됨)', text))
    match = re.search(_STAGES[stage][2],text,re.I)
    tail = text[match.end():] if match else ''
    tail = re.split(r'고객|운영|실서비스|\bQC\b|검수|개발|전달',tail,maxsplit=1,flags=re.I)[0]
    return bool(_DONE.search(tail) and not re.search(r'미완료|미적용|미반영|못|않|아님|아니',tail))


def _stage_event(stage, row):
    _, _, evidence_pattern = _STAGES[stage]
    events = []
    for text in _segments(row):
        if not re.search(evidence_pattern, text, re.I):
            continue
        if stage == 'qc' and re.search(r'고객.{0,15}(?:QC|검수)', text, re.I):
            continue
        if _FAILED.search(text):
            state = 'failed'
        elif _PENDING.search(text):
            state = 'pending'
        elif _completed_stage(stage, text):
            state = 'done'
        else:
            state = 'unknown'
        events.append((state, row, text))
    return events


def _facts(topic, rows):
    preferred = {'cause':'사유 및 원인','action':'조치 및 개선'}.get(topic)
    candidates = sorted(rows, key=lambda row: (row['field'] != preferred, -len(row['text'])))
    fallback = []
    for row in candidates:
        paragraphs = re.split(r'\n\s*\n', row['text'].replace('\r\n', '\n'))
        for text in paragraphs:
            # 양쪽 필드가 같은 REASON/ACTION 서식을 포함해도 요청한 항목만 고른다.
            if '[REASON]' in text or '[ACTION]' in text:
                marker = 'REASON' if topic == 'cause' else 'ACTION'
                match = re.search(r'\[' + marker + r'\][^\n]*\n(.*?)(?=\[(?:REASON|ACTION)\]|$)', text, re.S)
                text = match[1] if match else ''
            text = re.sub(r'\(여기에 내용을 입력하세요\)|-{4,}', '', text).strip()
            # 같은 이슈의 제목 행만 벗기고 다음 행의 실제 조치와 조건은 보존한다.
            heading = re.match(r'^0*(\d{1,7})\s*:[^\n]*\n', text)
            if heading and int(heading[1]) == row['issue_id']:
                text = text[heading.end():].strip()
            if (0 < len(text) <= 600 and not re.match(r'안녕|감사합니다|수고|\d{4}[.\-/].*(?:메일|회신)', text)
                    and not re.fullmatch(r'\[.*\]', text) and not re.match(r'0*\d{5,7}\s*:', text)):
                if re.fullmatch(r'(?:조치|관련|문의|수정).{0,8}(?:내용|사항).{0,8}(?:안내|답변|회신)(?:함|완료)?', text):
                    fallback.append((row,text))
                else:
                    yield row, text
    yield from fallback


def direct_conclusion(question: str, plans, selected_ids: list[int], issues: dict[int, dict[str, Any]],
                      previous_questions: list[str] | None = None, answer_items: list[dict[str, Any]] | None = None) -> list[str]:
    """결론은 원문 발췌/보수적인 단계 판정이며 새 모델 호출을 추가하지 않는다."""
    from backend.evaluation.explanation_quotes import _label
    if not question:
        return []
    facts = [topic for topic, pattern in [('cause', r'원인|이유|왜'), ('action', r'조치|수정한\s*(?:내용|부분)|수정\s*내용')]
             if re.search(pattern, question)]
    def requested_stages(text):
        return [stage for stage, (_, pattern, _) in _STAGES.items() if any(
            re.search(pattern, part, re.I) and (stage != 'implementation' or re.search(r'완료|끝|됐|했|까지|최종', part))
            for part in re.split(r'[?？.!\n]', text))]
    stages = requested_stages(question)
    if not stages and re.search(r'완료|끝|보고|뜻|그럼|그러면', question):
        for previous in reversed(previous_questions or []):
            stages = requested_stages(previous)
            if stages:
                break
    if ('action' not in facts and re.search(r'어떻게|정리|결론', question)
            and any(topic == 'action' for topic, *_rest in plans)):
        facts.append('action')
    if re.search(r'후속\s*(?:답변|회신)|고객.{0,10}(?:답변|회신)|누가|발신자',question) and any(topic == 'actors' for topic, *_rest in plans):
        facts.append('actors')
    if not facts and not stages and any(topic == 'technical' for topic, *_rest in plans):
        facts.append('technical')
    if not stages and (not facts or any(topic in {'technical','actors'} for topic in facts)):
        bound = question_bound_conclusion(question,answer_items or [])
        if bound:
            return bound
    if not facts and not stages:
        return []
    lines = ['### 핵심 결론', '']
    rows = []
    for _topic, preview, detail, _unanswered in plans:
        for row in preview + detail:
            if row['issue_id'] in selected_ids and row not in rows:
                rows.append(row)
    for issue_id in selected_ids:
        issue_rows = sorted([r for r in rows if r['issue_id'] == issue_id], key=_order)
        for topic in facts:
            topic_rows = [r for planned, preview, detail, _unanswered in plans if planned == topic
                          for r in preview + detail if r['issue_id'] == issue_id]
            choices = list(_facts(topic,topic_rows))
            title = {'cause':'원인','action':'조치','technical':'기술 내용','actors':'후속 회신'}[topic]
            if choices:
                for row,text in choices[:4 if topic == 'technical' else 2 if topic == 'actors' else 1]:
                    lines.extend([f'- **{title}**: {_label(text)} {_reference(row)}', ''])
            else:
                lines.extend([f'- **{title}**: 제공된 #{issue_id} 근거로 확정하지 못했습니다.', ''])
        for stage in stages:
            title = _STAGES[stage][0]
            events = [event for row in issue_rows for event in _stage_event(stage, row)]
            # 요청·재검수·취소 등 뒤의 기록도 완료 기록과 같은 기준으로 대조한다.
            meaningful = [event for event in events if event[0] != 'unknown']
            latest = meaningful[-1] if meaningful else events[-1] if events else None
            if latest and latest[0] == 'done':
                state, row, text = latest
                lines.extend([f'- **{title}**: {title} 완료가 확인됩니다. {_reference(row)}',
                              f'  - 근거: “{_label(text)}”', ''])
            elif latest and latest[0] == 'failed':
                state, row, text = latest
                lines.extend([f'- **{title}**: {title}는 실패·재확인 기록이 있습니다. 완료로 단정할 수 없습니다. {_reference(row)}',
                              f'  - 근거: “{_label(text)}”', ''])
            else:
                line = f'- **{title}**: 현재 제공된 근거로는 {title} 완료를 확인할 수 없습니다.'
                if latest:
                    state, row, text = latest
                    lines.extend([line + ' ' + _reference(row), f'  - 확인된 기록: “{_label(text)}”', ''])
                else:
                    lines.extend([line + f' (#{issue_id} · 제공된 근거 범위)', ''])
    return lines


def question_bound_conclusion(question, items):
    from backend.intent.unified_validation import evidence_is_grounded
    from backend.evaluation.explanation_quotes import _label
    lines = []
    for item in items:
        part = item['question_part']
        if not evidence_is_grounded(part,question):
            continue
        rows = item.get('evidence') or []
        if item.get('unanswered') or not rows:
            lines.extend([f'- **{_label(part)}**: 제공된 근거로 확정하지 못했습니다.', ''])
            continue
        for row in rows:
            lines.extend([f'- **{_label(part)}**: {_label(row["text"])} {_reference(row)}', ''])
    return ['### 핵심 결론', '', *lines] if lines else []
