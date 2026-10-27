"""사내 자료의 조회와 독립적인 지식·작성 질문을 구분한다."""
import re


_INTERNAL_TARGET = re.compile(
    r'#\s*\d{3,}|\[\d{3,}\]|(?:이슈|티켓|화면\s*번호)\s*#?\s*\d{3,}'
    r'|\d{4,}\s*(?:번|이슈)|우리\s*팀|사내|내\s*담당|담당자|공수|미종료|미해결'
    r'|개발\s*완료|등록\s*(?:일|건수)|건수|접수|노트|처리\s*이력|전달\s*이력|재전달|롤백'
    r'|발생했|발생했고|재현|(?:전달|안내|답|적용|수정).{0,8}(?:했|됐|한|된|하겠|예정)'
    r'|(?:그|해당|이)\s*(?:이슈|문서|오류|장애)|같은\s*(?:조건|기준)|그중|직전'
    r'|(?:mantis|맨티스|멘티스|notion|노션)(?:에서|의|에\s*(?:등록|있는))'
    r'|(?:이슈|티켓|버그|문서|회의록|공지|자료|기록|이력|사례).{0,20}'
    r'(?:찾아|조회|검색|목록|보여|몇\s*(?:개|건))'
    r'|(?:찾아|조회|검색|보여).{0,20}(?:이슈|티켓|기록|이력)', re.I,
)
_GENERAL_REQUEST = re.compile(
    r'일반적(?:으로|인)|개념|정의|원리|장단점|차이|종류|뭔지|무엇'
    r'|어떤\s*(?:회사|프로그램)|(?:작성|쓰는|고치는|해결).{0,16}(?:요령|방법)'
    r'|(?:문장|문구).{0,16}(?:다듬|고쳐|바꿔|번역)|번역해'
    r'|생일.{0,20}(?:축하|메일)|(?:책|영화|음악).{0,16}추천'
    r'|날씨|^(?:안녕하세요|안녕)[.!?]*$', re.I,
)


def is_general_question(text: str) -> bool:
    """명시된 사내 조회 대상을 보존하면서 독립적인 일반 질문만 확정한다.

    오류·회의록·고객사명 같은 단어만으로 조회를 강제하지 않는다. 이슈 번호,
    저장된 기록 요청, 업무 범위가 있으면 기존 조회 계획의 판단에 맡긴다.
    """
    if not _GENERAL_REQUEST.search(text) or _INTERNAL_TARGET.search(text):
        return False
    if re.search(r'이슈|장애|오류|버그|차트|HTS|MTS', text, re.I):
        from backend.common import normalize_for_match
        from backend.intent.scope_extraction import extract_customer_scope
        if extract_customer_scope(text, normalize_for_match(text)).get('customer_key'):
            return False
    return True
