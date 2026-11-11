"""에이전트가 제안한 조건을 확정한다. 보정은 초안 전체 재작성 대신 원자적인 부분 변경이다."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.intent.execution_contract import validate_execution_contract
from backend.intent.unified_validation import validate_plan
from backend.tools.redaction import redact
from backend.tools.registry import ToolRegistry, ToolSpec, error_result


@dataclass
class PlanningSession:
    question: str
    user: dict
    previous: dict
    conversation: dict = field(default_factory=dict)
    ordinal_guide_tools: bool = False
    candidate: dict | None = None
    plan: dict | None = None
    attempts: list[dict] = field(default_factory=list)
    error: str = ''

    def submit(self, *, proposal: dict | None = None, changes: dict | None = None) -> dict:
        if self.plan is not None:
            return error_result('prepare_query', 'conditions_locked', '확정한 조건은 이 요청 안에서 변경할 수 없습니다.')
        if (proposal is None) == (changes is None):
            return error_result('prepare_query', 'invalid_arguments', '첫 호출은 proposal, 보정은 changes만 지정하세요.')
        if proposal is not None:
            if self.candidate is not None:
                return error_result('prepare_query', 'patch_required', '전체를 다시 작성하지 말고 changes에 바꿀 필드만 지정하세요.')
            self.candidate = deepcopy(proposal)
        if self.candidate is None:
            return error_result('prepare_query', 'proposal_required', '부분 변경 전에 첫 proposal이 필요합니다.')
        candidate = {**deepcopy(self.candidate), **deepcopy(changes or {})}
        try:
            sections = candidate.get('answer_sections', ['results'])
            if (not isinstance(sections, list) or 'results' not in sections or len(sections) > 2
                    or any(value not in {'results', 'explanation'} for value in sections)
                    or len(set(sections)) != len(sections)):
                raise ValueError('answer_sections에는 results를 포함하고 explanation을 선택하여 중복 없이 표시 순서를 지정하세요.')
            if self.previous.get('query_scopes') and candidate.get('followup'):
                raise ValueError('비교 후속 조회는 고객사·기간과 공통 조건 또는 이슈 번호를 명시해 주세요.')
            if self.candidate.get('comparison') is not None and candidate.get('comparison') is None:
                raise ValueError('제안한 비교 범위를 보정 중 삭제할 수 없습니다. comparison의 잘못된 값만 고치세요.')
            if candidate.get('comparison') is not None:
                from backend.intent.query_comparison import validate_comparison_plan
                plan = validate_comparison_plan(self.question, deepcopy(candidate), self.user)
            else:
                plan = validate_plan(self.question, deepcopy(candidate), self.user, self.previous)
            if self.conversation:
                from backend.intent.conversation_contract import validate_followup_display
                validate_followup_display(self.question, plan, self.previous)
            if self.ordinal_guide_tools:
                from backend.intent.validation_rules import check_compound_issue_list
                check_compound_issue_list(self.question, plan)
            contract = validate_execution_contract(self.question, plan)
            if not contract['valid']:
                raise ValueError(contract['checks'][0]['detail'])
            plan['answer_sections'] = list(sections)
            from backend.tools.conditions import planned_sources
            sources = candidate.get('explanation_sources', list(planned_sources(plan)))
            if (not isinstance(sources, list) or any(source not in planned_sources(plan) for source in sources)
                    or len(set(sources)) != len(sources) or 'explanation' in sections and not sources):
                raise ValueError('explanation_sources는 계획한 원천 mantis/notion 중 실제로 설명할 대상만 중복 없이 지정하세요.')
            plan['explanation_sources'] = list(sources)
        except (ValueError, TypeError, KeyError) as exc:
            self.error = str(exc)[:400]
            self.attempts.append({'status': 'rejected', 'changed_fields': sorted(changes or {}), 'reason': self.error})
            return {**error_result('prepare_query', 'condition_validation', self.error),
                    'candidate': deepcopy(self.candidate),
                    'repair': 'changes에 변경할 최상위 필드만 지정하세요. 생략한 필드는 보존합니다. '
                              '중첩 객체/배열은 해당 필드 전체를 교체합니다. 거부한 변경은 반영하지 않았습니다. '
                              '집계 표현을 바꿀 때도 요청한 그룹·지표를 보존하세요. '
                              '표시 개수 오류는 limit만 보정하고 고객사·상태·범위·정렬을 유지하세요.'}
        self.candidate = candidate
        self.plan = plan
        self.error = ''
        self.attempts.append({'status': 'accepted', 'changed_fields': sorted(changes or {})})
        return {'ok': True, 'tool': 'prepare_query', 'status': 'validated',
                'source': plan['source'], 'operation': plan['operation'],
                'applied_filters': deepcopy(plan['filter_clauses']),
                'scope': plan['resolved_constraints'].get('mantis_team_scope'),
                **({'query_scopes': [{'id': s['id'], 'label': s['label'],
                    'applied_filters': s['plan']['filter_clauses']} for s in plan['query_scopes']]}
                   if plan.get('query_scopes') else {})}


class PrepareQueryArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')
    proposal: dict[str, Any] | None = Field(default=None,
        description='첫 호출의 조회 조건 및 출력 목적 JSON. 필수 키는 시스템의 조건 계약을 따른다.')
    changes: dict[str, Any] | None = Field(default=None,
        description='보정할 최상위 필드만 지정. 생략한 상태·필터·기간 등은 서버가 보존한다. proposal과 함께 쓰지 않는다.')


def planning_registry(session: PlanningSession) -> ToolRegistry:
    def submit(args, context):
        if 'planning' not in context.local_state:
            context.local_state['planning'] = deepcopy(session)
        return context.local_state['planning'].submit(**args.model_dump())

    return ToolRegistry([ToolSpec('prepare_query',
        '질문/서버 대화에 근거한 조회 조건을 검증한다. 성공 전에는 원천 조회를 하지 않는다. '
        '실패 시 원래 proposal을 재작성하지 않고 changes로 잘못된 필드만 고친다. 이 도구는 조회나 쓰기를 실행하지 않는다.',
        PrepareQueryArgs, 'planning', submit)])


def planning_messages(session: PlanningSession) -> list[dict]:
    from backend.intent.unified import PROMPT
    from backend.intent.conversation_contract import CONTEXT_RULES
    from backend.intent.person_roles import person_planning_hints
    from backend.settings.catalogs import get_customer_aliases
    prompt = (PROMPT + '\n' + CONTEXT_RULES + '\n'
        '너는 실행 에이전트다. 위 JSON은 별도 실행 경로를 고르는 결정이 아니라 조건과 출력 목적의 호환 계약이다. '
        'prepare_query의 proposal로 제출한다. 검증 후 건수·목록·집계·본문 도구의 실행 순서를 직접 정한다. '
        'proposal.answer_sections로 응답 구성을 정한다. 건수·목록·보고서·속성은 results, '
        '원문 내용 설명도 요청하면 ["results","explanation"]으로 지정한다. 설명을 먼저 보여주려면 순서를 바꿀 수 있다. '
        'results는 반드시 포함한다. 출력 구성은 조회 필터나 수치 계산을 변경하지 않는다. '
        'explanation_sources에는 실제 설명 대상 원천을 ["mantis"], ["notion"], ["mantis","notion"] 중 지정한다. '
        '이슈 건수·목록과 Notion 가이드 설명을 함께 요청하면 source=both, explanation_sources=["notion"]이다. '
        '이슈 내용 요약도 요청했을 때만 설명 대상에 mantis를 포함한다. '
        '검증 오류가 나면 changes에 잘못된 필드만 넣는다. 미해결 등 나머지 상태·고객사·기간 조건을 다시 생성하거나 삭제하지 않는다. '
        '예: 유효한 aggregation에 그룹이 있는데 최상위 group_by에도 중복 지정했다면 changes={"group_by":null}만 제출한다. '
        '기간 없는 상태별 건수를 aggregation으로 잘못 표현했다면 changes={"aggregation":null,"group_by":"status"}를 함께 제출한다. '
        'aggregation만 지우면 그 안의 그룹·지표도 사라진다. 복합 그룹·공수·기간·비교 요청을 단순 총 건수로 바꾸지 않는다. '
        '질문이 불명확하면 이유를 짧게 답하고 도구 없이 수치/내용을 추측하지 않는다. '
        '해석 모델이 JSON을 직접 답하는 대신 반드시 prepare_query 도구를 호출한다.\n'
        '여러 고객사 또는 등록 기간의 건수·목록·상태별 집계를 비교하면 proposal에 comparison을 추가한다. '
        '형식: {"axis":"customer", "cohorts":[{"value":"한투","evidence":"한투"},{"value":"KB","evidence":"KB"}]}. '
        '기간 비교는 axis=created_at, value=[시작일,종료일], evidence=현재 질문의 정확한 기간 구절(예: 이번 달, 지난달)이다. '
        '공통 filters에는 비교 축을 넣지 않고 고객사·미해결·담당자 등 다른 공통 조건을 보존한다. '
        'source는 mantis, followup=false로 2~4개 범위를 모두 명시한다. '
        'comparison을 쓰는 독립 범위 비교에서는 앞의 aggregation 지침을 적용하지 않는다. aggregation=null로 둔다. '
        '고객사끼리 또는 이번 달과 지난달의 건수만 비교하면 operation=count, group_by=null이다. '
        '각 범위의 목록은 operation=search, 각 범위의 상태별 건수는 operation=aggregate, group_by=status이다. '
        '기간은 cohorts 또는 공통 filters로 지정하며 aggregation.period나 previous_month를 함께 만들지 않는다. '
        '예: 전사 한투 미해결의 이번 달·지난달 등록 건수 비교 → operation=count, aggregation=null, group_by=null, '
        'filters=[{field:customer,operator:equals,value:한투,evidence:한투}], status.mode=open, '
        'comparison.axis=created_at와 두 기간의 cohorts. 월별 그룹 집계가 아니다. '
        '보정에서 필드를 없애려면 changes에 그 필드를 null로 명시한다. 생략한 필드는 이전 초안에 남는다. '
        '각 범위를 독립 조회하며 합집합 한 번의 조회로 대체하지 않는다.\n'
        f'오늘={date.today().isoformat()}\n고객사 사전=' + json.dumps(get_customer_aliases(), ensure_ascii=False))
    if session.ordinal_guide_tools:
        prompt += ('\n이슈 건수·최근 목록·그 순번의 Notion 가이드를 함께 요청할 수 있다. '
                   'source=both, operation=explain으로 고객사·미해결 상태(open)·정렬·limit를 보존한다. '
                   '기간 없는 최근 N건은 sort와 limit만 지정한다. 순번 ID는 실제 목록에서 결정하므로 추측하지 않는다. '
                   '가이드 요청은 이슈 검색 필터가 아니다.')
    previous = {key: session.previous.get(key) for key in (
        'source', 'operation', 'report_kind', 'effort', 'aggregation', 'sort', 'limit',
        'resolved_constraints', 'terms', 'group_by', 'grounding_text', 'query_scopes', 'list_context')} if session.previous else None
    return [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(redact({
        'question': session.question, 'previous_confirmed_plan': previous,
        'conversation_context': session.conversation,
        **person_planning_hints(session.question),
    }), ensure_ascii=False)}]
