"""표시 개수를 현재 질문에 결속한다. 전체 건수나 조회 모집단을 변경하지 않는다."""
import re

from backend.intent.keyword_terms import extract_requested_limit


def requested_display_limit(question: str, source: str = 'mantis') -> int | None:
    if source not in {'mantis', 'both'}:
        return None
    from backend.intent.validation_rules import unquoted
    text = unquoted(question)
    # 별도 원천의 문서 개수와 API 최근 N건 모집단은 이슈 표시 개수가 아니다.
    text = re.split(r'notion|노션', text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r'(?:최신|최근)\s*\d+\s*건\s*기준', ' ', text)
    # "그 16건을 하나씩"은 앞선 총건수의 참조다. 전체 목록을 16으로 제한하지 않는다.
    text = re.sub(r'(?:그|해당)\s*\d+\s*건(?:을|은)?(?=\s*(?:하나씩|모두|전부|빠짐없이))', ' ', text)
    return extract_requested_limit(text, max_limit=200)


def validate_display_limit(question: str, source: str, operation: str, limit: int | None) -> None:
    if source not in {'mantis', 'both'} or operation not in {'search', 'count', 'explain'}:
        return
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 200):
        raise ValueError('표시 개수 오류')
    if operation == 'count':
        if limit is not None:
            raise ValueError('전체 집계에 표시 개수를 적용할 수 없습니다')
        return
    requested = requested_display_limit(question, source)
    if requested is not None and limit is not None and requested != limit:
        raise ValueError(f'질문에서 요청한 표시 개수 limit={requested}를 사용하세요. 전체 건수는 표시 개수와 별개입니다.')


def expected_display_limit(question: str, plan: dict) -> int | None:
    """최종 검사는 변조된 계획보다 현재 질문의 명시 개수를 우선한다."""
    return requested_display_limit(question, plan.get('source', 'mantis')) or plan.get('limit')
