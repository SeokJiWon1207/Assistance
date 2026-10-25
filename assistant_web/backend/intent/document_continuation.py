"""모델이 표현 변경만 판정한 후속을 기존 서버 계획에 검증·결속한다."""
import re

KINDS = {'postmortem','handoff','notice'}
# 전체 문장을 소비할 수 있을 때만 축약 응답을 허용한다. 새로운 대상·조건은
# 이 문법에 없으므로 기존 전체 계획으로 해석하고 검증한다.
_WORDS = re.compile('|'.join(re.escape(word) for word in sorted({
    '그 분석','그 내용','그 문서','이 문서','앞의 사실','앞의','앞서','이전','같은','동일한','동일',
    '사실','이슈','근거','내용','분석','문서','그대로','유지해서','유지하고','두고',
    '사후 분석','사후분석','회고','인수인계','회의용','회의에서 다룰','회의 안건','회의',
    '안건','형식','간결한','정중한','상세한','말투','개발팀','운영팀','팀장','보고용','보고하는',
    '5줄','5 줄','다섯 줄','요약','작성해줘','바꿔줘','정리해줘','보여줘','으로','에게','해서',
    '해줘','만','은','는','을','를','과','와','로','의','에',
},key=len,reverse=True)))


def eligible(question, previous):
    kind=previous.get('report_kind')
    if (previous.get('source')!='mantis' or previous.get('operation')!='report' or kind not in KINDS
            or len((previous.get('resolved_constraints') or {}).get('issue_ids') or [])!=1):
        return False
    if ('인수인계' in question and kind!='handoff') or (re.search(r'사후\s*분석|회고',question) and kind!='postmortem'):
        return False
    remainder=_WORDS.sub('',question)
    return bool(re.search(r'안건|형식|말투|간결|정중|상세|팀장|개발팀|운영팀|[5다섯]\s*줄',question)
                and not re.sub(r'[\s.!?·,]','',remainder))


def expand(question, previous):
    if not eligible(question,previous):
        raise ValueError('새 조건이나 대상을 포함하는 질문은 전체 계획으로 해석해야 합니다')
    return {'source':'mantis','operation':'report','report_kind':previous['report_kind'],
            'scope':'inherit','followup':True,'filters':[],
            'status':{'mode':'inherit','values':[],'evidence':''},'clear_fields':[],
            'terms':[],'group_by':previous.get('group_by'),'limit':previous.get('limit')}
