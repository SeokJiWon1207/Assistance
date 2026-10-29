"""모델의 계산 명세를 제한된 서버 계약으로 정규화한다. 식/SQL은 받지 않는다."""
from copy import deepcopy
from datetime import date
import re

DIMENSIONS = {'customer', 'platform', 'status', 'owner', 'issue_type', 'month'}
METRIC_DATES = {'issue_count': 'created_at', 'effort': 'delivered_at'}
MAX_MONTHS = 36
MAX_TOP_N = 20


def check_shape(spec):
    from backend.intent.unified_validation import require
    require(isinstance(spec, dict), '집계 명세는 JSON 객체여야 합니다')
    require(set(spec) == {'group_by', 'metrics', 'period', 'comparison', 'top_n', 'explain'}, '지원하지 않는 집계 명세 항목')
    groups, metrics = spec['group_by'], spec['metrics']
    require(isinstance(groups, list) and len(groups) <= 3 and
            all(isinstance(g, str) and g in DIMENSIONS for g in groups) and len(groups) == len(set(groups)), '집계 그룹 오류')
    require(isinstance(metrics, list) and 1 <= len(metrics) <= 2 and
            all(isinstance(m, str) and m in METRIC_DATES for m in metrics) and len(metrics) == len(set(metrics)), '지원하지 않는 집계 지표')
    require(type(spec['top_n']) is int and 1 <= spec['top_n'] <= MAX_TOP_N, '집계 상위 개수 오류')
    require(type(spec['explain']) is bool, '집계 설명 형식 오류')
    require(spec['comparison'] is None or spec['comparison'] == 'previous_month', '지원하지 않는 집계 비교')
    require(spec['comparison'] is None or 'month' in groups, '전월 비교에는 월 그룹이 필요합니다')
    period = spec['period']
    require(isinstance(period, dict) and set(period) == {'from', 'to', 'evidence'}, '집계 기간 형식 오류')
    try:
        start, end = date.fromisoformat(period['from']), date.fromisoformat(period['to'])
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('집계 날짜 형식 오류') from exc
    require(1900 <= start.year <= end.year <= 2100 and start <= end, '집계 날짜 범위 오류')
    require((end.year - start.year) * 12 + end.month - start.month < MAX_MONTHS, '집계 기간은 최대 36개월입니다')
    require(isinstance(period['evidence'], str) and bool(period['evidence'].strip()), '집계 기간 근거 누락')
    return spec


def validate_aggregation(question, specification, previous=None):
    from backend.intent.unified_validation import check_clause, check_query_period, require, unquoted
    require(isinstance(specification, dict), '집계 명세 형식 오류')
    spec = {'comparison': None, 'top_n': 10, 'explain': False, **deepcopy(previous or {}), **deepcopy(specification)}
    check_shape(spec)
    text = unquoted(question)
    require(not re.search(r'(?:수정|종료|해결|배포|QC\s*완료|개발\s*완료)\s*일\s*기준', text, re.I),
            '집계 날짜 기준은 건수의 등록일과 공수의 고객사전달일만 지원합니다')
    created_basis = bool(re.search(r'등록일\s*(?:기준|로|별)', text))
    delivered_basis = bool(re.search(r'전달일\s*(?:기준|로|별)', text))
    require(not ('issue_count' in spec['metrics'] and delivered_basis and not created_basis),
            '건수의 날짜 기준은 등록일입니다. 전달일 기준 건수는 지원하지 않습니다')
    require(not ('effort' in spec['metrics'] and created_basis and not delivered_basis),
            '공수의 날짜 기준은 고객사전달일입니다. 등록일 기준 공수는 지원하지 않습니다')
    if previous:
        removed = set(previous['group_by']) - set(spec['group_by'])
        require(not removed or bool(re.search(r'대신|말고|합쳐|합산|구분.{0,10}(?:없|빼|해제|제거)|그룹.{0,10}(?:해제|제거)', text)),
                '후속 필터·상위 개수 변경에서는 기존 집계 그룹을 유지해야 합니다: ' + ', '.join(sorted(removed)))
    if 'period' in specification and spec['period'] != (previous or {}).get('period'):
        check_clause({'field': 'created_at', 'operator': 'between',
                      'value': [spec['period']['from'], spec['period']['to']],
                      'evidence': spec['period']['evidence']}, question, 'mantis')
    # 전월 대비는 비교 연산이며 조회 기간을 지난달로 교체하라는 요청이 아니다.
    period_question = re.sub(r'전월\s*대비|전월과\s*비교|전월\s*비교', '', question)
    check_query_period(period_question, 'mantis', 'aggregate', [period_clause(spec, 'issue_count')])
    if spec['explain'] and not (previous or {}).get('explain'):
        require(bool(re.search(r'분석|설명|이유|해석|기여|요약', text)), '집계 설명은 분석·설명 요청에만 추가합니다')
    require(not spec['explain'] or not re.search(r'표만|설명\s*없이|설명.{0,6}(?:빼|제외)|분석하지', text),
            '설명 생략 요청에서는 explain=false로 지정해야 합니다')
    return spec


def period_clause(spec, metric):
    return {'field': METRIC_DATES[metric], 'operator': 'between',
            'value': [spec['period']['from'], spec['period']['to']]}
