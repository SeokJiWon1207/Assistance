"""명시적인 문서 URL의 읽기 질문은 제목 검색으로 바꾸지 않는다."""
import re
from backend.intent.constraints import extract_query_constraints
from backend.intent.explanation import explanation_constraints
from backend.notion.direct_lookup import notion_page_ids


def explicit_reference_plan(question: str) -> dict | None:
    identifiers = notion_page_ids(question)
    mantis_ids = [int(m[1]) for m in re.finditer(r'\bMantis\s+#([1-9]\d{0,7})(?![\da-zA-Z_])', question, re.I)
                 if not re.match(r'\s*(?:번\s*)?(?:은|는|을|를)?\s*(?:제외|빼고)', question[m.end():])]
    if not (identifiers or mantis_ids) or not re.search(r'조회|읽어|정리|요약|대조|비교|설명|알려|확인', question):
        return None
    if re.search(r'유사|비슷|공수|집계|건수|몇\s*건|업무\s*보고|주간\s*보고|월간\s*보고', question):
        return None
    if re.search(r'(?:삭제|수정|추가|생성|등록|업데이트|저장)(?:해|하|해줘|해\s*줘|해\s*주세요)', question):
        return None
    text = re.sub(r'https?://[^\s<>"\)]+', ' ', question)
    constraints = explanation_constraints(text, extract_query_constraints(text))
    constraints['issue_ids'] = list(dict.fromkeys([*constraints.get('issue_ids', []), *mantis_ids]))
    constraints['notion_page_ids'] = identifiers
    return {'source': ('both' if constraints.get('issue_ids') else 'notion') if identifiers else 'mantis', 'operation': 'explain',
            'planner': 'explicit_references', 'authority': 'authoritative', 'planned': True,
            'resolved_constraints': constraints, 'terms': [], 'limit': max(4, len(constraints.get('issue_ids') or [])),
            'reason': '사용자가 명시한 문서 URL과 이슈 번호의 현재 원문 조회'}
