"""확정 조회 범위 안에서 부족한 원문만 한 번 보강한다."""
import re
import time
from typing import Any, Callable

from backend.execution import CURRENT_BUDGET, ExecutionBudget, RequestStopped, execution_scope


def investigation_plan(question: str, validated: dict[str, Any], grounding: dict[str, Any], requested=None):
    selected=validated['selected_issue_ids']
    partial={i['id'] for i in grounding.get('issues',[]) if i['id'] in selected
             and i.get('answer_evidence',{}).get('complete') is False}
    items=validated.get('answer_items') or []
    legacy=not items or not any('draft_answer' in item for item in items)
    gap=any(s.get('unanswered') for s in validated.get('sections',[])) or any(i.get('unanswered') for i in items)
    history=bool(re.search(r'완료|최종|마지막|이력|경과|QC|검수|검증|전달|반영|종료',question,re.I))
    explicit=isinstance(requested,list) and bool(requested)
    candidates=(requested if explicit else selected) if legacy or gap or history else []
    ids=list(dict.fromkeys(i for i in candidates if type(i) is int and i in partial))[:2]
    return {'issue_ids':ids,'reason':'incomplete_history' if history else 'missing_answer' if gap else 'requested_notes' if explicit else 'legacy_partial' if legacy else 'sufficient',
            'max_rounds':1,'max_issues':2,'timeout_seconds':8,'partial_ids':sorted(partial)}


def bounded_investigation(action: Callable[[], tuple[dict[str, Any],dict[str, Any]]], grounding, plan):
    if not plan['issue_ids']:
        return grounding,{'enabled':True,'attempted_ids':[],'supplemented_ids':[],'added_chars':0,
                          'failures':[],'skipped_ids':[], 'plan':plan}
    parent=CURRENT_BUDGET.get();deadline=time.monotonic()+plan['timeout_seconds']
    if parent is not None:
        parent.remaining();deadline=min(deadline,parent.deadline)
    child=ExecutionBudget(deadline=deadline)
    if parent is not None:
        child.cancelled=parent.cancelled
    try:
        with execution_scope(child):
            result,meta=action()
    except RequestStopped:
        if parent is not None:
            parent.remaining()
        if time.monotonic()<deadline:
            raise
        result=grounding;meta={'enabled':True,'attempted_ids':plan['issue_ids'], 'supplemented_ids':[],
                              'added_chars':0,'skipped_ids':[],
                              'failures':[{'issue_id':i,'reason':'fetch_failed'} for i in plan['issue_ids']],
                              'timed_out':True}
    return result,{**meta,'plan':plan}
