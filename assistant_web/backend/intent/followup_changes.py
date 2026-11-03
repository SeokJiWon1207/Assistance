"""LLM이 선언한 조건 해제의 원문 근거 검사. 새 조회 의도를 선택하지 않는다."""
import re

from backend.intent.validation_rules import evidence_is_grounded, require, unquoted
from backend.query_filters import values_for_clause

FIELD_REFERENCES = {
    'platform': ('매체', '플랫폼', 'platform'),
    'customer': ('고객사', '증권사', 'customer'),
    'owner': ('담당자', '담당', 'owner'),
    'reporter': ('요청자', 'reporter'),
    'registered_reporter': ('등록자', '등록한 사람'),
    'effort_person': ('공수 기입자', '공수 담당자'),
    'created_at': ('등록', '생성', '접수', '기간', '날짜', '연도', '이번 달', '지난달', '이번 주', '지난주'),
    'delivered_at': ('전달일', '전달 기간', '기간'),
    'due_at': ('마감', '기한'), 'deployed_at': ('배포일', '배포 기간'),
    'title': ('제목',), 'keyword': ('키워드', '본문', '검색어', '주제'),
    'terms': ('검색어', '키워드', '주제'), 'screen_number': ('화면',),
    'issue_id': ('번호', '이슈 번호'), 'database': ('데이터베이스',),
    'status': ('상태',), 'team': ('팀', '전사', '전체 담당자'),
}
REMOVAL = re.compile(r'해제|풀|빼|제거|제외|없|무관|관계없이|상관없이|말고|상관\s*없|관계\s*없')
ALL_CLEAR = re.compile(r'(?:모든|전체)\s*(?:검색\s*)?조건|조건(?:을|은|도)?\s*(?:전부|모두|다)')


def validate_all_scope_evidence(question, quote):
    text = unquoted(question)
    require(evidence_is_grounded(quote, text) and (
                re.search(r'전사|모든\s*담당자|전체\s*담당자', quote)
                or ('팀' in quote or ALL_CLEAR.search(quote)) and REMOVAL.search(quote)),
            '전사 범위 확대의 원문 근거가 없습니다. 다른 필드만 바꿀 때는 scope=inherit로 팀 범위를 유지하세요')
    return quote


def validate_clear_evidence(question, fields, evidence, previous_clauses, *, comparison_end=False):
    require(isinstance(evidence, dict), '해제 조건 근거는 필드별 인용문이어야 합니다')
    result = {}
    text = unquoted(question)
    for field in fields:
        if comparison_end and field != 'issue_id':
            # 비교를 끝내고 기준 이슈 하나를 조회하면 후보의 필터는 적용되지 않는다.
            result[field] = question
            continue
        quote = evidence.get(field, question)
        references = [*FIELD_REFERENCES.get(field, ()),
                      *(value for clause in previous_clauses if clause['field'] == field
                        for value in values_for_clause(clause.get('value')))]
        grounded = evidence_is_grounded(quote, text)
        target = isinstance(quote, str) and (ALL_CLEAR.search(quote) or
                 any(reference.casefold() in quote.casefold() for reference in references))
        require(grounded and target and REMOVAL.search(quote),
                f'해제 조건의 원문 근거가 없습니다: {FIELD_REFERENCES.get(field, (field,))[0]}({field}). 이 필드를 실제로 해제하라는 인용문을 clear_evidence에 지정하고, 요청하지 않은 조건은 상속하세요')
        result[field] = quote
    return result
