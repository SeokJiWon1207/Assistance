"""보고서의 등록 기간·적체·참고 사례가 각자 실제로 조회한 범위로 검증되게 한다."""
from copy import deepcopy
from backend.mantis_status import mantis_status_bucket, source_status


def report_evidence_constraints(mode: str, source: dict, constraints: dict, retrieval: dict) -> dict:
    issue_id=source.get('issue_id')
    if mode=='deterministic_similar_resolution':
        seed_ids={i for stage in retrieval.get('stages',[]) if stage.get('source')=='mantis_similar_resolution'
                  for i in stage.get('meta',{}).get('base_issue_ids',[])}
        if issue_id in seed_ids:
            # 기준 이슈는 비교 대상 조건의 적용 대상이 아닌, 검증된 ID의 참조 원문이다.
            return {'issue_ids':[issue_id]}
    if mode=='deterministic_incident_triage':
        reference_ids={i for s in retrieval.get('stages',[]) if s.get('source')=='chroma_incident_triage'
                       for i in s.get('meta',{}).get('reference_issue_ids',[])}
        if issue_id in reference_ids:
            return {key:deepcopy(constraints[key]) for key in ('platform_key','platform_aliases') if key in constraints}
    stage_name={'deterministic_weekly_report':'mantis_weekly_report',
                'deterministic_monthly_customer_report':'mantis_monthly_customer'}.get(mode)
    backlog_ids={i for s in retrieval.get('stages',[]) if s.get('source')==stage_name and stage_name
                 for i in s.get('meta',{}).get('backlog_ids',[])}
    if issue_id in backlog_ids and mantis_status_bucket(source_status(source))=='open':
        scoped=deepcopy(constraints)
        scoped.pop('created_at',None)
        scoped['filter_clauses']=[c for c in scoped.get('filter_clauses',[]) if c.get('field')!='created_at']
        return scoped
    return constraints
