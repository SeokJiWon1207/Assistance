"""질문 유형에 맞춘 근거·단계 결론 표시. 모델 호출 없음."""
import re
from typing import Any
from backend.evidence.references import _label, _order, _reference
from backend.evidence.excerpts import safe_excerpts
from backend.evidence.stages import STAGES, stage_events

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


def reply_question(question):
    return bool(re.search(r'고객.{0,16}(?:답변|회신)|어떤\s*(?:답변|회신)|그\s*회신',question)
                and not re.search(r'원인|이유|왜|누가|발신자|수신자',question))


def sent_reply_rows(issues):
    result: list[dict[str,Any]]=[]
    for issue_id,issue in issues.items():
        rows=[{**row,'issue_id':issue_id} for row in safe_excerpts(issue)]
        fields={row['field'] for row in rows if stage_events('reply',row)}
        result.extend(row for row in rows if row['field'] in fields)
    return result


def direct_conclusion(question: str, plans, selected_ids: list[int], issues: dict[int, dict[str, Any]],
                      previous_questions: list[str] | None = None, answer_items: list[dict[str, Any]] | None = None) -> list[str]:
    """결론은 원문 발췌/보수적인 단계 판정이며 새 모델 호출을 추가하지 않는다."""
    if not question:
        return []
    if reply_question(question) and not re.search(r'전달|수정본|적용|검증|완료',question):
        replies=sent_reply_rows({i:issues[i] for i in selected_ids if i in issues})
        choices=[(row,text) for row,text in _facts('actors',replies)
                 if not re.fullmatch(r'.{0,80}(?:부장|차장|과장|대리|사원|팀장)입니다[.]?',text)]
        if choices:
            return ['### 핵심 결론','',*[line for row,text in choices[:4]
                    for line in [f'- **고객 회신**: {_label(text)} {_reference(row)}','']]]
    facts = [topic for topic, pattern in [('cause', r'원인|이유|왜'), ('action', r'조치|수정한\s*(?:내용|부분)|수정\s*내용')]
             if re.search(pattern, question)]
    def requested_stages(text):
        return [stage for stage, (_, pattern, _) in STAGES.items() if any(
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
            title = STAGES[stage][0]
            if stage == 'delivery' and re.search(r'수정본|패치|모듈|라이브러리|소스',question):
                title = '수정본 전달'
            events = [event for row in issue_rows for event in stage_events(stage, row)]
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
                replies = [event for row in issue_rows for event in stage_events('reply',row)] if stage == 'delivery' else []
                if replies:
                    _, reply_row, reply_text = replies[-1]
                    lines.extend([f'- **설명·안내 회신**: 회신 전달 기록이 확인됩니다. {_reference(reply_row)}',
                                  f'  - 근거: “{_label(reply_text)}”', ''])
                    if title == '고객사 전달':
                        title = '수정본 전달'
                line = f'- **{title}**: 현재 제공된 근거로는 {title} 완료를 확인할 수 없습니다.'
                if latest:
                    state, row, text = latest
                    lines.extend([line + ' ' + _reference(row), f'  - 확인된 기록: “{_label(text)}”', ''])
                else:
                    lines.extend([line + f' (#{issue_id} · 제공된 근거 범위)', ''])
    return lines


def question_bound_conclusion(question, items):
    from backend.intent.validation_rules import evidence_is_grounded
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
