"""질문의 연산과 표시 그룹을 결정하는 순수 규칙."""
from __future__ import annotations

import re
from typing import Optional


COUNT_MARKERS = ("몇개", "몇건", "총몇")
SUM_MARKERS = ("합계", "총합", "합이", "합은", "더하면", "공수합")
SIMILAR_MARKERS = ("비슷", "유사", "같은", "동일", "관련")


GROUP_BY_PATTERNS = {
    "customer": re.compile(r"(?:고객사|프로젝트)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "platform": re.compile(r"(?:플랫폼|매체(?:구분)?)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "owner": re.compile(r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "status": re.compile(r"상태\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "type": re.compile(r"(?:타입|유형|분류)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    # "월별 건수 추이"처럼 기간축으로 묶는 요청. 고객사/담당자 같은 다른 축이
    # 함께 있으면 그쪽이 먼저 잡히도록 마지막에 둔다.
    "month": re.compile(r"(?:월|달)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
}


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def infer_operation(question: str) -> str:
    normalized = normalized_text(question)
    request_text = re.sub(r'''["'“‘][^"'”’]*["'”’]''', " ", question)
    if re.search(r"공수(?:는|가|를|은|이)?\s*어떻게\s*(?:돼|되|됐)", request_text):
        return "aggregate"
    if "공수" in normalized and any(marker in normalized for marker in ("알고", "얼마", "집계", "합", "총")):
        return "aggregate"
    if any(marker in normalized for marker in SUM_MARKERS):
        return "aggregate"
    # '건 수정본', '개 수정'을 붙이면 건수·개수가 생긴다. 띄어 쓴 수량
    # 명사는 '수'와 허용 조사 뒤 경계까지 확인해 '건 수신'과 구분한다.
    if any(marker in normalized for marker in COUNT_MARKERS) or re.search(
        r"건수|개수|(?:건|개)\s+수(?:는|가|를|와|만|도|의|로|까지|부터)?(?=$|[^가-힣])", question
    ):
        return "count"
    if re.search(
        r"(?:이슈|건|게|거|것)(?:이|가)?\s*(?:있는지|있어|있나|있나요)[\s\S]{0,120}"
        r"(?:있으면|있다면)[\s\S]{0,120}(?:없으면|없다면)",
        request_text,
    ):
        return "count"
    if re.search(r"(?:이슈|건)(?:이|가)?\s*있는지(?:만)?\s*(?:알려|말해|답해)", request_text):
        return "count"
    if re.search(r"(?:이슈|건|것|거)(?:을|를|만)?\s*(?:모두|전체|전부)?\s*세(?:고|어|줘|주세요)", request_text):
        return "count"
    if re.search(r"(?:이슈|건|항목)(?:을|를)?\s*(?:모두|전체|전부)?\s*집계(?:해|하여|하)?", request_text):
        return "count"
    if extract_group_by(question) and "집계" in normalized:
        return "count"
    issue_reference = re.search(r"(?<!\d)(?:이슈\s*)?#?0*\d{5,7}(?!\d)(?:\b|(?=[의은는을를과와]))", question, re.IGNORECASE)
    if issue_reference and any(
        marker in normalized for marker in SIMILAR_MARKERS
    ):
        return "search"
    if issue_reference:
        return "get"
    return "search"


def extract_group_by(question: str) -> Optional[str]:
    """Return a deterministic presentation grouping requested by the user.

    Grouping phrases such as ``담당자별로`` describe how to organize results;
    they must never be interpreted as an owner/person filter.
    """
    for field, pattern in GROUP_BY_PATTERNS.items():
        if pattern.search(str(question or "")):
            return field
    return None


def has_count_intent(question: str) -> bool:
    return infer_operation(question) == "count"
