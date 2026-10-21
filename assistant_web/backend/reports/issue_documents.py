"""한 이슈의 원문으로 작성하는 읽기 전용 업무 문서. 미확정 항목은 추정하지 않는다."""
import re
from backend.common import compact_text
from backend.display import status_name
from backend.issue_text import encode_issue_field
from backend.evaluation.explanation_direct import _stage_event
from backend.reports.similar import _recorded_cause, _recorded_fix, _clean_note
from backend.sources.mantis import fetch_mantis_issue_by_id, normalize_issue, build_sources_from_issue_rows, issue_matches_constraints


def _progress(notes):
    labels = {'qc':'내부 QC','delivery':'고객사 전달','production':'운영 반영','customer_validation':'고객 검증'}
    states = {'done':'완료 기록 확인','pending':'요청·예정 기록이며 완료 확인 필요',
              'failed':'실패·재확인 기록 있음','unknown':'완료 근거 미확인'}
    lines=[]
    for stage,label in labels.items():
        events=[event for row in notes for event in _stage_event(stage,row)]
        meaningful=[event for event in events if event[0]!='unknown']
        state=meaningful[-1][0] if meaningful else 'unknown'
        lines.append(f'- {label}: {states[state]}')
    return lines


def _section(issue, heading):
    for note in reversed(issue.get('notes') or []):
        text=str(note.get('text') or '').replace('\r\n','\n')
        match=re.search(r'(?m)^\s*[-#*]*\s*(?:'+heading+r')\s*[:：]?\s*\n',text)
        if match:
            block=re.split(r'\n\s*[-#*]+\s*[가-힣A-Za-z]',text[match.end():],maxsplit=1)[0].strip()
            if block:
                return block[:1200]
    return ''


def build_issue_document_report(text: str, constraints: dict, kind: str) -> dict | None:
    ids=constraints.get('issue_ids') or []
    if len(ids)!=1 or kind not in {'handoff','postmortem','notice'}:
        return None
    raw=fetch_mantis_issue_by_id(ids[0])
    if not raw or not issue_matches_constraints(raw,constraints):
        return None
    issue=normalize_issue(raw)
    notes=[{'issue_id':ids[0],'note_id':n.get('id'),'created_at':n.get('created_at',''),
            'text':str(n.get('text') or '')} for n in raw.get('notes') or []]
    notes.sort(key=lambda n:(str(n['created_at']),int(n['note_id'] or 0)))
    progress=_progress(notes)
    cause,_=_recorded_cause(issue);fix,_=_recorded_fix(issue)
    cause=_section(issue,r'현상\s*원인|원인') or cause
    fix=_section(issue,r'작업\s*내역|조치\s*(?:내용|내역)|수정\s*(?:사항|내역)') or fix
    positives=[]
    if cause and fix:
        positives.append('원인과 수정 항목이 함께 기록되어 다음 담당자가 수정 범위를 추적할 수 있습니다.')
    for step in progress:
        if '완료 기록 확인' in step:
            positives.append(step.removeprefix('- ')+' — 단계별 확인 근거가 남아 있습니다.')
    title=encode_issue_field(issue.get('summary') or '제목 미기재')
    cause='\n'.join(encode_issue_field(line) for line in cause.splitlines()) if cause else '확정할 수 있는 원인 기록을 추출하지 못했습니다. 원문 확인이 필요합니다.'
    fix='\n'.join(encode_issue_field(line) for line in fix.splitlines()) if fix else '완료된 조치 내용을 추출하지 못했습니다. 요청·예정을 완료로 보고하지 마세요.'
    if kind=='notice':
        # 외부 전달 초안에는 내부 번호·담당자·경로·기술 원인 추정을 넣지 않는다.
        symptom=re.sub(r'\[[^]]*\]|#?0*'+str(ids[0])+r'(?!\d)', '', str(issue.get('summary') or '')).strip()
        lines=['고객 안내문 초안','', '안녕하세요. 문의하신 사항의 확인 내용을 안내드립니다.','',
               '### 현재 확인된 사실',encode_issue_field(symptom),'',
               '### 고객 영향','문의에 기록된 현상을 기준으로 안내드리며, 영향 범위와 규모는 추가 확인이 필요합니다.','',
               '### 진행 상황',*progress,'',
               '### 다음 안내','다음 안내 시점은 현재 기록에서 확정되지 않았습니다.']
    else:
        heading='인수인계' if kind=='handoff' else '사후 분석서'
        lines=[f'#{ids[0]} {heading}',title,'',f'현재 상태: {status_name(issue.get("status"))}']
        if kind=='handoff':
            lines+=['', '### 확인한 내용',cause,'', '### 시도한 조치',fix,'', '### 진행 단계',*progress,
                    '','### 막힌 점','별도의 막힘·대기 사유는 이 초안에서 확정하지 않았습니다. 최신 담당자 확인이 필요합니다.',
                    '', '### 다음 행동','제안: 완료 근거가 없는 단계의 최신 기록과 다음 담당자의 확인 범위를 점검하세요.',
                    '', '### 주의사항','내부 QC·고객사 전달·운영 반영·고객 검증은 각각의 근거로 구분해 보고하세요.']
        else:
            lines+=['','### 발생 영향',title,'구체적인 영향 규모는 이 기록만으로 확정하지 않습니다.',
                    '', '### 직접 원인',cause,'', '### 근본 원인','직접 원인을 조직·절차상의 근본 원인으로 확대해 확정하지 않습니다.',
                    '', '### 대응 과정',fix,'', '### 진행 단계',*progress,
                    '', '### 잘된 점',*(positives or ['잘된 점을 구체적으로 평가할 완료·작업 근거가 부족합니다.']),
                    '', '### 개선점 및 재발 방지 과제','제안: 재현 조건과 수정 검증 항목을 점검하고, 운영 반영·고객 검증 증빙을 분리해 관리하세요. 확정된 개선 과제로 보고하기 전 담당자 합의가 필요합니다.']
        lines+=['','### 타임라인과 근거']
        for row in notes:
            cleaned=_clean_note(row['text'],issue)
            cleaned=' '.join(line for line in cleaned.splitlines() if not re.match(r'\s*(?:[A-Za-z]:[\\/]|https?://)',line))
            lines.append(f'- {str(row["created_at"])[:10]} · 노트 {row["note_id"]}: {encode_issue_field(compact_text(cleaned,180))}')
        if not notes:
            lines.append('원문 노트 없음. 현재 속성만 확인할 수 있습니다.')
    sources=build_sources_from_issue_rows([issue])
    return {'content':'\n'.join(lines),'sources':sources,'sourcesForFollowUps':sources,
            'raw':{'mode':'deterministic_issue_document'},
            'retrieval':{'query':text,'constraints':constraints,'stages':[{
                'source':'mantis_issue_document','status':'ok','issues_list':[issue],
                'meta':{'issue_ids':ids,'document_kind':kind,'search_complete':True}}]}}
