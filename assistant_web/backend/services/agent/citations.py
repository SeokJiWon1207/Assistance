"""최종 답변의 인용을 근거 원장과 대조한다.

원장에 없는 인용이나 일반 참고 등급 자료를 본문 사실의 근거로 쓴 문장은 기호만 지워
통과시키지 않는다. 호출부가 예산 안에서 한 번 수정하거나, 남은 문장은 주장째 제외한다.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set, Tuple

_REF = r"(?:M#|N)\d+"
CITATION = re.compile(rf"\[({_REF})\]")
# 검증 리포트의 이슈 번호 판정(EXPLICIT_ID)과 같은 표기를 함께 확인한다.
BARE_ISSUE = re.compile(r"(?:#|(?:이슈\s*(?:번호|ID)?|\bID)\s*[:：]?\s*)0*(\d{3,9})(?!\d|년|월|일|건)", re.I)
# 마침표를 생략한 "검토 중입니다 [N1] 운영 반영도 완료했습니다"도 둘로 나눈다.
_SENTENCE = re.compile(rf"(?<=[.!?;；])\s+(?=\S)|(?<=니다)\s*(?=\[{_REF}\])|(?<=\])(?=[,，;；](?!\s*\[))")
_LINK = re.compile(r"(?:\w[\w+.-]*://|//[\w.-]+/|www\.|mailto:|(?:href|src)\s*=|\]\s*[\[(]|^\s*\[[^\]]+\]:\s*\S)", re.I)
_CHECK_HEADINGS = {"추가 확인 필요", "추가 확인 필요 사항"}
_HEADINGS = {"확인된 사실", "근거 기반 분석·추정", *_CHECK_HEADINGS}
# 사건의 부정("배포되지 않았습니다")과 자료의 한계("배포 여부는 확인되지 않았습니다")를 구분한다.
_EVIDENCE_GAP = re.compile(
    r"(?:(?:확인|명시|기재|기록)(?:되지|되어\s*있지|돼\s*있지)\s*않(?:습니다|았습니다|았다|는다|음)|"
    r"(?:(?:확인|판단|단정)할|알)\s*수\s*없(?:습니다|었습니다|다|음)|"
    r"(?:확인|판단|단정)하기\s*어렵(?:습니다|다)|확인(?:하지\s*못(?:했습니다|했다|함)|\s*불가)|"
    r"(?:근거|자료|기록)(?:가|이|는|은)?\s*(?:없(?:습니다|다|음)|부족(?:합니다|하다|함))|미확인(?:입니다)?)$"
)
_CHECK_ITEM = re.compile(
    r"(?:여부|(?:확인|점검|대조|검토|검증|조사|비교|재현)"
    r"(?:(?:이|가|할)?\s*필요(?:합니다|하다|함)?|해야\s*(?:합니다|한다)|해\s*주세요|할\s*것)?|"
    r"(?:인가요|나요|까요)\?)$"
)
_MIXED_CLAIM = re.compile(r"(?:지만|으며|으나|[했됐였었았졌]고|니다)(?=[,\s])|(?:완료|확정|됨|함)\s*[,;；]")


def _heading(text: str) -> str:
    label = text.strip(" \t#*_:：")
    return label if label in _HEADINGS else ""


def _is_verification_note(claim: str, section: str) -> bool:
    text = claim.strip(" \t.*_|")
    if _MIXED_CLAIM.search(text):
        return False
    return bool(_EVIDENCE_GAP.search(text) or (section in _CHECK_HEADINGS and _CHECK_ITEM.search(text)))


@dataclass(frozen=True)
class CitationIssue:
    claim: str
    ref: str
    reason: str
    claim_index: int


@dataclass
class CitationReport:
    issues: List[CitationIssue] = field(default_factory=list)
    cited: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


_LEADING_CITATIONS = re.compile(rf"\s*(?:\[{_REF}\][.!?]?\s*)+")


def claims(content: str) -> List[str]:
    """목록 항목 안에서도 줄과 문장 단위로 주장을 나눈다.

    문장 끝 마침표 뒤에 붙인 인용은 앞 문장의 일부다. 인용만 따로 떼어 내면 기호만
    지우고 근거 없는 문장을 남기게 된다.
    """
    units: List[str] = []
    for line in content.splitlines():
        if not line.strip():
            continue
        spans: List[Tuple[int, int]] = []
        start = 0
        for boundary in [*_SENTENCE.finditer(line), None]:
            end = boundary.start() if boundary else len(line)
            leading = _LEADING_CITATIONS.match(line, start, end)
            # "[N1]에 따르면"처럼 조사와 이어진 ref는 새 문장에 속한다.
            attached = (leading and leading.end() < end and line[leading.end() - 1] == "]"
                        and line[leading.end()].isalpha())
            if spans and leading and not attached:
                # "문장. [M#1] 다음 문장"의 인용은 앞 문장의 근거다. 원래 줄의 구간으로 이어 붙인다.
                spans[-1] = (spans[-1][0], leading.end())
                start = leading.end()
            if line[start:end].strip():
                spans.append((start, end))
            start = boundary.end() if boundary else len(line)
        units.extend(line[begin:end] for begin, end in spans)
    return units


def validate_citations(content: str, catalog: Dict[str, Dict[str, Any]], quoted_ids: Set[int]) -> CitationReport:
    report = CitationReport()
    section = ""
    for index, claim in enumerate(claims(content)):
        heading = _heading(claim)
        if heading:
            section = heading
            continue
        before = len(report.issues)
        refs = list(CITATION.finditer(claim))
        if "](" in claim or _LINK.search(CITATION.sub("", claim)):
            report.issues.append(CitationIssue(claim, "", "model_link", index))
        for match in refs:
            ref = match.group(1)
            item = catalog.get(ref)
            if item is None:
                report.issues.append(CitationIssue(claim, ref, "unknown_ref", index))
            elif item.get("grade") != "scoped":
                report.issues.append(CitationIssue(claim, ref, "reference_grade", index))
            else:
                report.cited.append(ref)
        for match in BARE_ISSUE.finditer(CITATION.sub(" ", claim)):
            identifier = int(match.group(1))
            item = catalog.get(f"M#{identifier}")
            if item is None and identifier not in quoted_ids:
                report.issues.append(CitationIssue(claim, f"#{identifier}", "unknown_issue", index))
            elif item is not None and item.get("grade") != "scoped":
                report.issues.append(CitationIssue(claim, f"#{identifier}", "reference_grade", index))
        if not refs and len(report.issues) == before and not _is_verification_note(claim, section):
            report.issues.append(CitationIssue(claim, "", "missing_citation", index))
    report.cited = list(dict.fromkeys(report.cited))
    return report


def validate_quantities(content: str, evidence_texts: Dict[str, str]) -> List[CitationIssue]:
    """자유 설명의 수량은 인용한 원문에 있어야 한다. 원장의 집계는 별도 결과 부분에서만 표시한다."""
    quantity = re.compile(r'(?<![\d#])\d+(?:,\d{3})*(?:\.\d+)?\s*(?:건|개|명|회|시간|분|초|일|개월|년|%|MD)(?![A-Za-z])', re.I)
    number = re.compile(r'(?<![\dA-Za-z])[-+]?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?')
    def values(text):
        return {prefix + re.sub(r'[\s,]', '', match.group()).casefold()
                for prefix, pattern in (('quantity:', quantity), ('number:', number)) for match in pattern.finditer(text)}
    issues = []
    for index, claim in enumerate(claims(content)):
        quoted = ' '.join(evidence_texts.get(ref, '') for ref in CITATION.findall(claim))
        # 이슈 번호는 validate_citations가 원장과 대조하고 보정한다. 수량으로 중복
        # 차단하면 잘못된 참조를 한 번 고치는 기존 절차까지 건너뛰게 된다.
        statement = BARE_ISSUE.sub('', CITATION.sub('', claim))
        if values(statement) - values(quoted):
            issues.append(CitationIssue(claim, '', 'unsupported_quantity', index))
    return issues


def remove_claims(content: str, report: CitationReport) -> Tuple[str, int]:
    """근거가 없는 주장을 문장째 제외한다. 남은 문장의 의미를 바꾸지 않는다."""
    rejected = {issue.claim_index for issue in report.issues}
    kept = []
    removed = cursor = 0
    for index, claim in enumerate(claims(content)):
        start = content.index(claim, cursor)
        kept.append(content[cursor:start])
        cursor = start + len(claim)
        if index in rejected:
            removed += 1
        else:
            kept.append(claim)
    return prune_empty_headings("".join([*kept, content[cursor:]])), removed


def prune_empty_headings(content: str) -> str:
    kept = []
    has_body = False
    for line in reversed(content.splitlines()):
        if _heading(line):
            if not has_body:
                continue
            has_body = False
        elif line.strip():
            has_body = True
        kept.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(reversed(kept))).strip()


def render_citations(content: str, catalog: Dict[str, Dict[str, Any]]) -> str:
    """검증을 통과한 인용을 사용자 표기로 바꾼다. 링크는 출처 목록에서 서버 데이터로 만든다."""
    def replace(match: re.Match[str]) -> str:
        item = catalog.get(match.group(1)) or {}
        if item.get("type") == "mantis":
            return f"(#{item['id']})"
        return f"(Notion: {item.get('title') or '문서'})"
    return CITATION.sub(replace, content)
