"""원문의 고객사 포함·제외 집합을 계획과 독립적으로 대조한다."""
import re

from backend.settings.catalogs import get_customer_aliases
from backend.customer_identity import canonical_customer, customer_mentions
from backend.query_filters import values_for_clause


class CustomerScopeError(ValueError):
    """다른 모델도 같은 검증을 거쳐 보정할 수 있는 고객사 해석 오류."""


def explicit_customer_scope(question):
    from backend.text_scope import notion_structural_query
    text = notion_structural_query(question)
    # 인용부호 없는 제목 문자열 검색도 고객사 범위로 승격하지 않는다.
    text = re.sub(r'(?:제목|문서명|이름)(?:에는|에서|에)\s*\S+?\s+(?:포함\S*|들어\s*있는|있는|나오는|없는)', ' ', text)
    text = re.sub(r'(?:이슈|건|항목|결과|문서|페이지|그중|이중)(?:\s*(?:중|가운데))?\s*하나'
                  r'(?=\s|$|(?:만|씩|를|가|는)(?=\s|$))', ' ', text)
    mentions = customer_mentions(text)
    included: set[str] = set()
    excluded: set[str] = set()
    groups: list[list[tuple[int, int, str, str]]] = []
    for mention in mentions:
        if groups and re.fullmatch(r'\s*(?:와|과|랑|이랑|하고|및|또는|,|/|·)\s*', text[groups[-1][-1][1]:mention[0]]):
            groups[-1].append(mention)
        else:
            groups.append([mention])
    for i, group in enumerate(groups):
        tail = text[group[-1][1]:(groups[i+1][0][0] if i+1<len(groups) else len(text))]
        if re.match(r'\s*(?:(?:고객사|조건|필터|제한)(?:은|는|을|를)?\s*)+(?:해제|풀|없)', tail):
            continue
        negative = re.match(r'\s*(?:은|는|을|를|이|가)?\s*(?:(?:고객사|문서|이슈|관련)\s*)?(?:제외|빼고|말고|아닌)', tail)
        (excluded if negative else included).update(item[2] for item in group)
    return included - excluded, excluded


def check_customer_scope(question, clauses, incoming):
    included, excluded = explicit_customer_scope(question)
    positives = [{canonical_customer(v) for v in values_for_clause(c.get('value'))}
                 for c in clauses if c['field']=='customer' and c['operator'] in {'equals','in'}]
    negatives = {canonical_customer(v) for c in clauses if c['field']=='customer'
                 and c['operator'] in {'not_equals','not_in'} for v in values_for_clause(c.get('value'))}
    allowed = set.intersection(*positives) if positives else set()
    if not included <= (allowed-negatives) or not excluded <= negatives:
        raise CustomerScopeError('명시된 고객사 포함·제외 조건이 누락되거나 바뀌었습니다. '
                                 f'포함={sorted(included)}, 제외={sorted(excluded)}를 customer equals/in 또는 not_equals/not_in으로 보존하세요.')
    mentioned = included | excluded
    new_customers = {canonical_customer(v) for c in incoming if c['field']=='customer'
                     for v in values_for_clause(c.get('value'))}
    if any(value in get_customer_aliases() and value not in mentioned for value in new_customers):
        raise CustomerScopeError('현재 질문의 고객사 범위가 아닌 제목·본문 문자열을 고객사 조건으로 바꾸었습니다.')


def consume_customer_terms(terms, clauses):
    customers = {canonical_customer(v) for c in clauses if c['field']=='customer'
                 for v in values_for_clause(c.get('value'))}
    return [term for term in terms if canonical_customer(term) not in customers]
