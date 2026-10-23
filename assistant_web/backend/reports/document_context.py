"""인수인계의 최신 고객 기록을 같은 이슈 원문에서 짧게 보존한다."""
import re

from backend.common import compact_text
from backend.evaluation.explanation_quotes import safe_excerpts


def latest_customer_record(issue):
    notes=sorted(issue.get('notes') or [],key=lambda n:(str(n.get('created_at') or ''),int(n.get('id') or 0)))
    if not notes:
        return ''
    note=notes[-1]
    text=str(note.get('text') or '').replace('\r\n','\n')
    if not re.match(r'\s*\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s*'
                    r'(?:메일\s*수신\s*From[.:\s]|유선|통화)',text,re.I):
        return ''
    excerpts=[{'field':f'note:{note.get("id")}','note_id':note.get('id'),
               'text':part,'created_at':note.get('created_at','')}
              for part in re.split(r'\n\s*\n',text) if part.strip()]
    rows=safe_excerpts({'id':issue['id'],'answer_evidence':{'excerpts':excerpts}})
    content=[row['text'].strip() for row in rows
             if not re.match(r'\s*(?:\d{4}[.\-/]|안녕|감사|수고|0*\d{5,7}\s*:)',row['text'])]
    # 최종 결정뿐 아니라 여러 단말의 추가 증상도 보존한다. 마지막 두 문단만
    # 고르면 앞쪽 Android 보고가 뒤쪽 iOS 보고에 가려질 수 있다.
    decisions=[part for part in content if re.search(r'논의|추후|추가\s*확인|재확인|결정|보류|진행.*(?:같|예정)',part)]
    platform=''
    observations=[]
    for part in content:
        for line in part.splitlines():
            line=line.strip()
            if line.startswith('+'):
                platform=''
            if re.fullmatch(r'Android|iOS|AOS|안드로이드|아이폰',line,re.I):
                platform=line
            elif re.search(r'늘어|잘림|안\s*보|안보|빈\s*버튼|보이지|오류|불가|미동작|발생|수정사항',line):
                observations.append((platform+': ' if platform else '')+line)
    selected=list(dict.fromkeys([*observations,*decisions])) or content[-2:]
    if not selected:
        return ''
    budget=max(45,min(160,600//len(selected)))
    return ' / '.join(compact_text(part,budget) for part in selected)+f' [노트 {note.get("id")}] · 노트 기록일 {str(note.get("created_at") or "")[:10]}'
