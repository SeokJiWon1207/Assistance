"""원문에 결속된 짧은 답변. 의미 일치는 별도 모델 검토를 거쳐야 한다."""
import re
from difflib import SequenceMatcher
from typing import Any

from backend.evaluation.explanation_quotes import safe_excerpts, _label
from backend.intent.unified_validation import evidence_is_grounded

_UNCERTAIN = re.compile(r'확인.{0,12}(?:없|않|못|어렵|불가|필요)|근거.{0,12}(?:없|부족)|미확정|미확인|'
                        r'(?:기록|명시|확정|입증).{0,12}(?:없|않|못|아님|아니)|단정.{0,8}(?:못|않|어렵)')
_STAGE = {
    'implementation': r'개발|구현|수정', 'qc': r'\bQC\b|검수', 'delivery': r'전달|발송',
    'reply': r'회신',
    'customer_validation': r'고객.{0,10}(?:검증|테스트|확인)',
    'customer_application': r'고객.{0,10}(?:적용|반영)', 'production': r'운영|실서비스',
}


def _supported_claim(text, rows, all_rows):
    source = '\n'.join(str(r.get(k) or '') for r in rows for k in ('text','created_at','issue_id','note_id'))
    if re.search(r'https?://|<[^>]+>|```|^\s*#|\[[^]]+\]\(',text):
        return False
    # 근거 부재를 실제 미전달·미반영 등의 부정 사실로 바꾸지 않는다.
    # 명시된 부정 기록도 보수적으로 원문 답변에서 보여 주고 새 의역은 채택하지 않는다.
    if re.search(r'(?:전달|발송|반영|적용|검증|테스트)(?:은|는|이|가|을|를)?\s*(?:(?:아직|실제로)\s*)?'
                 r'(?:(?:되|하|이뤄지|이루어지)지\s*(?:않|못)|'
                 r'(?:안|못)\s*(?:됐|되었|했))|미전달|미반영|미적용',text):
        return False
    numbers = {str(float(n)) for n in re.findall(r'\d+(?:\.\d+)?',source)}
    if not {str(float(n)) for n in re.findall(r'\d+(?:\.\d+)?',text)}.issubset(numbers):
        return False
    identifiers = re.findall(r'[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_0-9]+)*',text)
    if any(word.casefold() not in source.casefold() for word in identifiers):
        return False
    from backend.evaluation.explanation_direct import _stage_event, _order
    for stage, pattern in _STAGE.items():
        for match in re.finditer(r'(?:' + pattern + r').{0,14}?(?:완료|통과|마쳤|끝났)',text,re.I):
            # '완료를 확인할 수 없다'처럼 불확실성을 명시한 문장은 확정 주장이 아니다.
            tail = re.split(r'[,;.]|(?:고|지만|이며|이고|으나)\s+',text[match.end():],maxsplit=1)[0][:30]
            if _UNCERTAIN.search(text[match.start():match.end()]+tail) or re.match(r'.{0,4}(?:아님|아니|않|못)',tail):
                continue
            for issue_id in {r['issue_id'] for r in rows}:
                events = [event for row in sorted(all_rows,key=_order) if row['issue_id']==issue_id
                          for event in _stage_event(stage,row)]
                meaningful = [e for e in events if e[0]!='unknown']
                if not meaningful or meaningful[-1][0]!='done':
                    return False
    return True


def summary_items(validated: dict[str, Any], grounding: dict[str, Any], question: str):
    """출처·형식·수치·식별자·완료 범위 검사이며 문장 전체의 참을 증명하지 않는다."""
    if not validated.get('valid'):
        return []
    items = validated.get('answer_items') or []
    if not items or len(items)>5:
        return []
    all_rows = [{**e,'issue_id':issue['id']} for issue in grounding.get('issues',[])
                if issue['id'] in validated['selected_issue_ids'] for e in safe_excerpts(issue)]
    from backend.evaluation.explanation_direct import reply_question, sent_reply_rows, _facts
    replies=sent_reply_rows({issue['id']:issue for issue in grounding.get('issues',[])
                            if issue['id'] in validated['selected_issue_ids']}) if reply_question(question) else []
    reply_fields={(r['issue_id'],r['field']) for r in replies}
    if reply_fields:
        # 실제 회신 질문에 최초 문의의 가설을 답변으로 섞지 않는다.
        if any((r['issue_id'],r['field']) not in reply_fields for item in items for r in item.get('evidence') or []):
            return []
        drafts=' '.join(str(item.get('draft_answer') or '') for item in items)
        for _row,text in _facts('actors',replies):
            if (not re.search(r'전달|수정본|적용|검증|완료',question)
                    and re.search(r'표기|기입|요청드립니다|부탁드립니다|확인\s*바랍니다',text)):
                # 별도 안내가 요약에서 사라지면 같은 출처의 원문 결론으로 보완한다.
                overlap=SequenceMatcher(None,re.sub(r'\s','',text),re.sub(r'\s','',drafts),autojunk=False).find_longest_match()
                if overlap.size<6:
                    return []
    result=[];total=0
    for item in items:
        answer=item.get('draft_answer');part=item.get('question_part');rows=item.get('evidence') or []
        if not isinstance(answer,str) or not 1<=len(answer.strip())<=180:
            return []
        # 모델이 항목 이름을 의역해도 답변은 검토하되 질문에 없는 표제는 표시하지 않는다.
        if not evidence_is_grounded(part,question):
            match = SequenceMatcher(None,str(part or ''),question,autojunk=False).find_longest_match()
            fragment = question[match.b:match.b+match.size].strip()
            part = fragment if len(re.findall(r'[가-힣A-Za-z0-9]',fragment))>=2 else ''
        answer=' '.join(answer.split());total+=len(answer)
        if reply_fields and re.fullmatch(r'.{0,80}(?:부장|차장|과장|대리|사원|팀장)입니다[.]?',answer):
            continue
        if total>520 or (item.get('unanswered') and not _UNCERTAIN.search(answer)):
            return []
        if not rows:
            if not item.get('unanswered') or not _UNCERTAIN.search(answer):
                return []
            answer='제공된 근거에서는 이 항목을 확인하지 못했습니다.'
        elif not _supported_claim(answer,rows,all_rows):
            return []
        if any(r['issue_id'] not in validated['selected_issue_ids'] for r in rows):
            return []
        result.append({'question_part':part,'answer':answer,'evidence':rows})
    return result


def render_summary(items, original):
    if not items:
        return original
    from backend.evaluation.explanation_direct import _reference
    lines=['### 핵심 답변','']
    for item in items:
        refs=list(dict.fromkeys(_reference(row) for row in item['evidence']))
        label = f'**{_label(item["question_part"])}**: ' if item['question_part'] else ''
        lines.extend([f'- {label}{_label(item["answer"])} '+(' '.join(refs) if refs else '(제공된 근거 범위)'), ''])
    # 원문과 전체 이력은 그대로 보존하고, 이전 규칙 결론만 중복 노출하지 않는다.
    evidence=original.split('### 핵심 근거',1)[-1] if '### 핵심 근거' in original else original
    evidence=re.sub(r'(?m)^</?details>\s*$|^<summary>전체 이력과 원문 근거 보기</summary>\s*$', '', evidence)
    lines.extend(['### 원문 근거와 전체 이력','', '### 핵심 근거'+evidence])
    return '\n'.join(lines).strip()
