"""모델의 조회 인자를 서버가 확인한 질문·감사 조건에 결속한다."""
from copy import deepcopy
from dataclasses import dataclass, field
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from backend.customer_identity import canonical_customer
from backend.intent.execution_contract import validate_execution_contract
from backend.intent.validation_rules import same_filter
from backend.query_filters import clause_supported, normalize_filter_clause, values_for_clause
from backend.tools.conditions import SourceConditions, project_conditions
from backend.utils.text import normalize_for_match


class QueryFilter(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    field: Literal['customer', 'platform', 'status', 'owner', 'reporter', 'registered_reporter', 'effort_person',
                   'issue_id', 'screen_number', 'created_at', 'delivered_at', 'due_at', 'deployed_at',
                   'title', 'keyword', 'database']
    operator: Literal['equals', 'not_equals', 'in', 'not_in', 'contains', 'not_contains', 'exists', 'not_exists',
                      'gte', 'lte', 'between']
    value: str | int | list[str | int] | None = None
    evidence: str | None = Field(default=None, max_length=500, description='추가 조사 필터의 현재 질문 원문 구절')
    evidence_ref: str | None = Field(default=None, pattern=r'^(M#[1-9][0-9]{0,7}|Q[0-9a-f]{12})$',
        description='추가 값이 실제 구조화 필드에 있는 이슈 ref 또는 그룹 값이 반환된 집계 condition_ref. 본문 지시는 근거가 아니다.')


class QuerySort(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Literal['created_at', 'updated_at', 'issue_id']
    direction: Literal['asc', 'desc']


class QueryArguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    population_limit: int | None = Field(default=None, ge=1, le=3000, strict=True,
        description='최근/최신 N건 기준의 필터 적용 전 모집단 크기. 표시 limit와 다르다. 요청에 있을 때만 첫 호출에 지정한다.')
    query_scope_id: str | None = Field(default=None, pattern=r'^S[1-4]$',
        description='비교 질문의 독립 범위 ID. 첫 호출에 필수이며 같은 범위의 condition_ref로 재사용할 수 있다.')
    filters: list[QueryFilter] | None = Field(default=None, max_length=20,
        description='이번 단계의 전체 필터. 요청 필수 조건을 유지하고 investigate에서는 근거 있는 추가 조건으로 좁힐 수 있다.')
    scope: Literal['all', 'team', 'target'] | None = Field(default=None, description='Mantis의 확정 조회 범위')
    sort: QuerySort | None = None
    purpose: Literal['answer', 'investigate'] | None = Field(default=None,
        description='answer는 질문 전체의 건수·목록·집계, investigate는 추가 조사. 좁힌 결과를 전체 건수로 사용하지 않는다. 참조 사용 시 생략한다.')
    condition_ref: str | None = Field(default=None, pattern=r'^Q[0-9a-f]{12}$',
        description='같은 원천의 앞선 도구 결과에서 받은 조건 참조. 사용 시 filters/scope/sort는 생략한다.')


def _canonical(clause: dict) -> dict:
    result = normalize_filter_clause(clause)
    if result is None:
        raise ValueError('허용되지 않은 필터입니다.')
    values = values_for_clause(result.get('value'))
    if result['field'] == 'customer':
        values = [canonical_customer(value) for value in values]
    elif result['field'] == 'issue_id':
        values = [str(int(v.lstrip('#'))) if v.lstrip('#').isdigit() else v for v in values]
    if isinstance(result.get('value'), list) or result['operator'] in {'in', 'not_in', 'between'}:
        result['value'] = values
    elif values:
        result['value'] = values[0]
    return result


def _same(left: dict, right: dict) -> bool:
    return bool(same_filter(_canonical(left), _canonical(right)))


@dataclass
class QueryContract:
    conditions: SourceConditions
    filters: list[dict]
    origins: list[dict[str, str]]
    scope: str | None
    sort: dict
    list_limit: int
    aggregation: dict | None = None
    ref: str = field(default_factory=lambda: 'Q' + uuid4().hex[:12])
    accepted: bool = False
    purpose: str = 'answer'
    parent_ref: str | None = None
    scope_id: str | None = None
    population_limit: int | None = None
    report: dict | None = None
    terms: list[str] = field(default_factory=list)

    def model_view(self) -> dict:
        return {'filters': deepcopy(self.filters), 'scope': self.scope, 'sort': deepcopy(self.sort),
                'list_limit': self.list_limit, 'evidence_grade': self.conditions.grade,
                'origins': deepcopy(self.origins),
                **({'population_limit': self.population_limit} if self.population_limit else {}),
                **({'report': deepcopy(self.report)} if self.report else {}),
                **({'terms': list(self.terms)} if self.terms else {}),
                **({'aggregation': self.aggregation_arguments()} if self.aggregation is not None else {})}

    def aggregation_arguments(self) -> dict:
        spec = deepcopy(self.aggregation or {})
        spec.pop('explain', None)
        if spec.get('period'):
            spec['period'].pop('evidence', None)
        return spec

    def bind(self, args: QueryArguments, *, explicit_required: bool, list_limit: int | None = None) -> tuple[SourceConditions, dict]:
        supplied = any(value is not None for value in (args.filters, args.scope, args.sort, args.population_limit))
        if args.condition_ref:
            if supplied or args.condition_ref != self.ref or not self.accepted:
                raise ValueError('같은 요청·원천에서 검증된 조건 참조만 단독으로 사용할 수 있습니다.')
            mode = 'reference'
        else:
            if explicit_required and (args.filters is None or self.conditions.source == 'mantis' and args.scope is None):
                raise ValueError('첫 건수·목록·집계 호출에는 전체 filters와 scope를 명시하세요.')
            if args.population_limit != self.population_limit and (explicit_required or args.population_limit is not None):
                raise ValueError('최근/최신 N건 모집단을 population_limit에 그대로 지정하세요. 표시 limit와 별개입니다.')
            if args.filters is not None:
                proposed = [item.model_dump() for item in args.filters]
                if (any(not clause_supported(self.conditions.source, c) for c in proposed)
                        or len(proposed) != len(self.filters)
                        or not all(any(_same(c, expected) for expected in self.filters) for c in proposed)
                        or not all(any(_same(c, expected) for c in proposed) for expected in self.filters)):
                    raise ValueError('질문·이전 확정 조건과 필터가 다릅니다. 누락·추가·제외 방향을 확인하세요.')
            if args.scope is not None and args.scope != self.scope:
                raise ValueError('확정된 조회 범위를 바꿀 수 없습니다.')
            if args.sort is not None and args.sort.model_dump() != self.sort:
                raise ValueError('확정된 정렬과 도구 인자가 다릅니다.')
            mode = 'explicit' if args.filters is not None else 'confirmed'
        if list_limit is not None and list_limit != self.list_limit:
            raise ValueError(f'확정된 표시 개수 limit={self.list_limit}을 사용하세요.')
        self.accepted = True
        constraints = {**deepcopy(self.conditions.constraints), 'filter_clauses': deepcopy(self.filters)}
        if self.conditions.source == 'mantis':
            constraints['sort'] = deepcopy(self.sort)
        bound = SourceConditions(self.conditions.source, constraints, self.conditions.applied,
                                 self.conditions.unapplied, self.conditions.planned)
        return bound, {'status': 'pass', 'mode': mode, 'origins': deepcopy(self.origins)}


def build_query_contract(question: str, plan: dict, conditions: SourceConditions, default_limit: int,
                         history: dict, *, query_tools: bool = False) -> QueryContract:
    """모델이 보낸 증명 문자열이 아닌 서버 계획의 근거를 검사한다."""
    if not validate_execution_contract(question, plan)['valid']:
        raise ValueError('실행 조건의 근거 검증이 필요합니다.')
    expected = project_conditions(plan, plan.get('resolved_constraints') or {}, conditions.source)
    actual_constraints = deepcopy(conditions.constraints)
    expected_constraints = deepcopy(expected.constraints)
    if conditions.source == 'mantis' or conditions.source == plan.get('source') == 'notion':
        ordering = plan.get('sort') or {'field': 'created_at', 'direction': 'desc'}
        actual_constraints.setdefault('sort', ordering)
        expected_constraints.setdefault('sort', ordering)
    if (actual_constraints != expected_constraints or conditions.applied != expected.applied
            or conditions.unapplied != expected.unapplied or conditions.planned != expected.planned):
        raise ValueError('원천 실행 조건과 검증된 계획의 범위·근거 등급이 다릅니다.')
    proof = plan.get('condition_grounding') or {}
    if proof.get('question') != question:
        raise ValueError('조건 근거의 질문이 다릅니다.')
    audit_id = str(proof.get('audit_id') or '')
    inherited = bool(re.fullmatch(r'[0-9a-f]{32}', audit_id)
                     and (plan.get('resolved_constraints') or {}).get('inherited_from_audit_id') == audit_id)
    filters = deepcopy(conditions.constraints.get('filter_clauses') or [])
    origins = []
    for clause in filters:
        if any(_same(clause, item) for item in proof.get('question_clauses') or []):
            origin = 'question'
        elif inherited and any(_same(clause, item) for item in proof.get('inherited_clauses') or []):
            origin = 'previous'
        elif ((proof.get('authenticated') or {}).get('uid') and clause['field'] in {'owner', 'effort_person'}
              and (plan.get('interpretation') or {}).get('scope') == 'self'
              and values_for_clause(clause.get('value')) == [proof['authenticated']['owner']]):
            origin = 'authenticated'
        elif _default_effort_period(plan, clause):
            origin = 'server_default'
        else:
            raise ValueError('질문·서버 감사·인증 사용자에 근거 없는 조건입니다.')
        origins.append({'field': clause['field'], 'origin': origin})
    # 의미 검색의 주제는 목록 도구에서 리터럴 필터로 쓰이므로 추가로 원문 근거를 확인한다.
    if (query_tools and conditions.source == plan.get('source') == 'mantis'
            and not (plan.get('operation') == 'report' and plan.get('report_kind') in {'similar', 'triage'})):
        prior_terms = [term for turn in history.get('turns') or [] for term in (turn.get('task') or {}).get('terms') or []]
        for term in plan.get('terms') or []:
            clause = {'field': 'keyword', 'operator': 'contains', 'value': term}
            if any(_same(clause, item) for item in filters):
                continue
            if normalize_for_match(term) and normalize_for_match(term) in normalize_for_match(question):
                origin = 'question'
            elif inherited and term in prior_terms:
                origin = 'previous'
            else:
                raise ValueError('목록 검색어의 질문·이전 대화 근거가 없습니다.')
            filters.append(clause)
            origins.append({'field': 'keyword', 'origin': origin})
    aggregation = deepcopy(plan.get('aggregation'))
    if aggregation is None and plan.get('group_by') and plan.get('operation') in {'count', 'aggregate'}:
        aggregation = {'group_by': [plan['group_by']], 'metrics': ['issue_count'],
                       'period': None, 'comparison': None, 'top_n': None}
    document = plan.get('operation') == 'report' and plan.get('report_kind') in {'handoff', 'postmortem', 'notice'}
    from backend.sources.mantis import extract_snapshot_limit
    from backend.tools.report_contracts import report_contract
    from backend.intent.display_contract import requested_display_limit
    report = report_contract(question, plan)
    # count의 limit=null은 전체 집계를 자르지 말라는 뜻이다. 함께 요청한
    # 목록의 표시 개수는 별도 도구 계약이며, 기본 5건으로 덮어쓰지 않는다.
    # 최종 검사(expected_display_limit)는 작업 종류와 무관하게 질문의 개수를 기대한다. '가장 최근
    # 하나만 자세히'를 get으로 계획해도 같은 개수를 써야 기본 5건 목록이 차단되지 않는다.
    display_limit = (requested_display_limit(question)
                     if conditions.source == 'mantis' and plan.get('operation') in {'count', 'search', 'explain', 'get'} else None)
    return QueryContract(conditions, filters, origins,
                         conditions.constraints.get('mantis_team_scope') if conditions.source == 'mantis' else None,
                         deepcopy(plan.get('sort') or {'field': 'created_at', 'direction': 'desc'}),
                         min(20 if document else 200, max(1, display_limit or plan.get('limit') or default_limit)),
                         aggregation=aggregation if conditions.source == 'mantis' else None,
                         population_limit=(extract_snapshot_limit(question, default=0) or None) if conditions.source == 'mantis' else None,
                         report=report if conditions.source == 'mantis' else None,
                         terms=list(plan.get('terms') or []) if conditions.source == plan.get('source') else [])


def _default_effort_period(plan: dict, clause: dict) -> bool:
    """통합 검증기가 기간 생략 시 넣는 올해 전달일만 기본값으로 인정한다."""
    from backend.intent import validation_rules
    if plan.get('operation') != 'report' or plan.get('report_kind') not in {'effort', 'effort_sla'}:
        return False
    from backend.query.dates import (extract_created_at_range, extract_relative_created_at_range,
                                     extract_relative_year_range, extract_yearless_month_range)
    question = str(plan.get('grounding_text') or '')
    for item in (plan.get('interpretation') or {}).get('evidence') or []:
        if item.get('field') != 'delivered_at' and item.get('evidence'):
            question = question.replace(item['evidence'], '')
    if any(parser(question) for parser in (extract_created_at_range, extract_relative_created_at_range,
                                           extract_relative_year_range, extract_yearless_month_range)):
        return False  # 명시 기간을 모델이 누락한 것은 회사 기본값으로 보정하지 않는다.
    year = validation_rules.date.today().year
    expected = {'field': 'delivered_at', 'operator': 'between', 'value': [f'{year}-01-01', f'{year}-12-31']}
    return (_same(clause, expected) and (plan.get('resolved_constraints') or {}).get('effort_date_basis') == 'delivered_at'
            and not any(c.get('field') == 'delivered_at' for c in (plan.get('interpretation') or {}).get('evidence') or []))
