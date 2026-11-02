"""동일 이슈·필드의 안전한 발췌와 원문 범위 보존."""
import re
from typing import Any

_ACCESS_ASSIGNMENT = re.compile(
    r'\b(?:pw|password|passwd|pwd|api[_ -]?key|access[_ -]?token|secret[_ -]?key)\s*[:=：]|'
    r'(?:비밀\s*번호|비번|패스워드)\s*[:=：]|공동인증.{0,30}(?:우회|패스)', re.I)


_NOTE_BOILERPLATE = re.compile(
    r'^\s*(?:안녕(?:하십니까|하세요)|감사합니다|수고(?:하십니까|하세요)|'
    r'(?:from|to|subject)\s*:|\d{4}[./-]\d{1,2}[./-]\d{1,2}\s+to\b)', re.I)


_NOTE_ISSUE_HEADER = re.compile(
    r'^\s*(?:(?:[-*]\s*|\d{1,2}\.\s*))?'
    r'(?=(?:\[?0*\d{5,7}\]?|\[0*\d{4}\][:：]|0*\d{4}[:：])\s*[:：]?\s+)'
    r'\[?0*(\d{4,7})\]?\s*[:：]?\s+', re.M)


_SHARED_ISSUES = re.compile(r'(?:두|양쪽)\s*(?:이슈|건)(?:의|에|은|을|에서| 모두)?')


_SHARED_CONTINUATION = re.compile(r'^\s*(?:해당\s*부분|해당부분|다음으로|위\s*두\s*이슈|이에\s*따라)')


_PRIOR_ISSUE_REFERENCE = re.compile(r'아래\s*이슈.{0,100}동일한\s*내용.{0,100}참고하여\s*전달', re.S)


_MAX_CORE_CHARS = 600


def _numbered_issue_heading(text: str) -> bool:
    """일반 변경 목록 뒤의 이슈 footer와 번호가 붙은 별개 이슈 항목을 구분한다."""
    header = _NOTE_ISSUE_HEADER.search(text)
    return bool(header and re.match(r'^\s*\d+\s*[).]\s+', header.group()))


def _shared_package_cautions(group: list[tuple[int, dict[str, Any]]], issue_id: int,
                             ) -> list[dict[str, Any]]:
    """명시적으로 함께 전달한 패키지의 공통 적용 조건만 별도 인용한다."""
    active_target = delivered = shared = False
    package_members: set[int] = set()
    awaiting_member = False
    cautions = []
    for _index, row in group:
        text = row.get('text', '')
        headings = list(_NOTE_ISSUE_HEADER.finditer(text))
        identifiers = {int(heading.group(1)) for heading in headings}
        target_context = active_target
        if identifiers:
            active_target = identifiers == {issue_id}
        if active_target and _DELIVERED.search(text):
            delivered = True
        declaration = re.search(r'(?:아래|다음).{0,60}포함.{0,30}(?:라이브러리|패키지|모듈|수정본)', text)
        # 같은 문단이어도 타 이슈 제목 뒤의 선언은 그 이슈의 안내다.
        if (delivered and target_context and declaration
                and not any(int(heading.group(1)) != issue_id and heading.start() < declaration.start()
                            for heading in headings)
                and re.search(r'(?:같이|함께).{0,20}전달', text)):
            shared = True
            package_members = {issue_id} | identifiers
            awaiting_member = not bool(identifiers - {issue_id})
            continue
        if shared and identifiers:
            if awaiting_member and identifiers - {issue_id}:
                # "아래 내용" 제목이 다음 문단에 오는 형식도 같은 범위다.
                package_members.update(identifiers)
                awaiting_member = False
            elif identifiers - package_members:
                # 명시한 패키지 밖의 새 이슈부터는 공통 조건이 아니다.
                shared = False
        if not shared:
            continue
        # 타 이슈 제목·수정 내역은 복구하지 않고 테스트 후 적용을 요청하는
        # 독립 문장만 보존한다. 완료나 운영 반영 사실로 바꾸지 않는다.
        condition = re.search(
            r'^(?:(?:충분히|반드시|내부에서)\s+)?(?:테스트|검증)'
            r'(?:하시고|하신\s*후|한\s*후|\s*후)\s*(?:적용|반영)'
            r'[^\n.!?]{0,80}(?:부탁드립니다|감사하겠습니다|주세요)[.!]?', text, re.M)
        if condition:
            cautions.append({**row, 'text': condition.group(),
                             'start': int(row.get('start') or 0) + condition.start(),
                             'end': int(row.get('start') or 0) + condition.end()})
    return cautions


def safe_excerpts(issue: dict[str, Any]) -> list[dict[str, Any]]:
    """접속정보와 여러 이슈가 섞인 노트의 타 이슈 구획을 인용에서 제외한다."""
    excerpts = issue.get('answer_evidence', {}).get('excerpts', [])
    issue_id = issue.get('id')
    blocked = {e.get('field') for e in excerpts if _ACCESS_ASSIGNMENT.search(e.get('text', ''))}
    available: list[dict[str, Any]] = []
    shared_prefixes: list[dict[str, Any]] = []
    shared_cautions: list[dict[str, Any]] = []
    for excerpt in excerpts:
        if excerpt.get('field') in blocked:
            continue
        source = excerpt.get('text') if isinstance(excerpt.get('text'), str) else ''
        source_header_ids = {int(identifier) for identifier in _NOTE_ISSUE_HEADER.findall(source)}
        first_header = _NOTE_ISSUE_HEADER.search(source)
        if (str(excerpt.get('field') or '').startswith('note:') and first_header
                and len(source_header_ids) == 2 and issue_id in source_header_ids):
            # 두 이슈 번호를 함께 적은 회신이라도 번호 앞 문장이 명시적으로
            # 두 작업을 묶어 계획한 경우, 그 공통 문장만 각 이슈의 근거로 남긴다.
            shared = re.search(r'작업\s*가능하며.{0,180}함께.{0,150}테스트하여\s*전달.{0,60}하겠습니다',
                               source[:first_header.start()], re.S)
            if shared:
                shared_prefixes.append({**excerpt, 'text': shared.group().strip(),
                                        'start': int(excerpt.get('start') or 0) + shared.start(),
                                        'end': int(excerpt.get('start') or 0) + shared.end()})
        has_numbered_sibling_before_target = bool(
            first_header and source_header_ids == {issue_id}
            and _numbered_issue_heading(source)
            and re.search(r'^\s*\d+\s*[).]\s+', source[:first_header.start()], re.M)
            and re.search(r'\n\s*\n\s*\S', source[first_header.end():]))
        if isinstance(source, str) and isinstance(excerpt.get('field'), str) and (
                excerpt['field'].startswith('note:')
                and (len(source_header_ids) > 1 or has_numbered_sibling_before_target)):
            # Raw retrieval stores a whole email as one excerpt; split only this
            # shape, so issue boundaries can be checked without losing normal notes.
            for paragraph in re.split(r'\n\s*\n', source.replace('\r\n', '\n')):
                if paragraph.strip():
                    embedded_header = _NOTE_ISSUE_HEADER.search(paragraph)
                    if (embedded_header and embedded_header.start() > 0
                            and _PRIOR_ISSUE_REFERENCE.search(paragraph[:embedded_header.start()])):
                        available.append({**excerpt, 'text': paragraph[:embedded_header.start()].strip()})
                        available.append({**excerpt, 'text': paragraph[embedded_header.start():].strip()})
                    else:
                        available.append({**excerpt, 'text': paragraph})
        else:
            available.append(excerpt)
    by_note: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, excerpt in enumerate(available):
        field = excerpt.get('field')
        if isinstance(field, str) and field.startswith('note:'):
            by_note.setdefault(field, []).append((index, excerpt))
    excluded: set[int] = set()
    for group in by_note.values():
        headers = [[int(match) for match in _NOTE_ISSUE_HEADER.findall(row.get('text', ''))]
                   for _index, row in group]
        mentioned = {identifier for matches in headers for identifier in matches}
        if not mentioned:
            continue
        if issue_id not in mentioned:
            # A reply filed on the target may cite the underlying issue for a
            # numbered item in the customer's immediately preceding follow-up.
            # Keep that reply only when both sides name the same item and the
            # reply itself identifies the attempted reproduction and requests.
            if len(mentioned) == 1 and len(group) == 1:
                reply = group[0][1].get('text', '')
                item = re.search(r'문의주신\s*(\d+)번\s*항목', reply)
                linked = bool(item and re.search(r'재현\s*되지\s*않|재현이\s*되지\s*않', reply)
                              and re.search(r'추가\s*정보\s*요청', reply)
                              and any(other.get('field') != group[0][1].get('field')
                                      and re.search(rf'{item.group(1)}번\s*항목.{{0,100}}수정처리가\s*제대로\s*안',
                                                    other.get('text', ''), re.S)
                                      for other in available))
                if linked:
                    continue
            excluded.update(index for index, _row in group)
            continue
        if len(mentioned) > 1:
            shared_cautions.extend(_shared_package_cautions(group, issue_id))
        if len(mentioned) == 1:
            first_target = next((position for position, matches in enumerate(headers)
                                 if issue_id in matches), None)
            if (first_target is not None and _numbered_issue_heading(group[first_target][1].get('text', ''))
                    and any(re.match(r'^\s*\d+\s*[).]\s+', row.get('text', ''))
                            for _index, row in group[:first_target])
                    and any(row.get('text', '').strip() and not _NOTE_BOILERPLATE.search(row['text'])
                            for _index, row in group[first_target + 1:])):
                # A mail can label an unrelated "2)" discussion before the
                # sole explicit target issue heading. Its earlier prose must
                # not stand in as the target issue's latest conclusion.
                excluded.update(index for index, _row in group[:first_target])
            continue
        if any(len(matches) > 1 for matches in headers):
            # 한 문단에 두 이슈 번호를 나열한 메일은 기본적으로 버린다.
            # 다만 그 뒤에 명시적으로 "두 이슈"를 지칭한 공통 설명과
            # 그 설명에 바로 이어지는 지시어 문단만 같은 근거로 인정한다.
            shared_possible = shared_open = active = False
            for (index, row), matches in zip(group, headers, strict=True):
                if matches:
                    shared_possible = len(set(matches)) == 2 and issue_id in matches
                    shared_open = False
                    active = len(matches) == 1 and matches[0] == issue_id
                    if not active:
                        excluded.add(index)
                    continue
                paragraph = row.get('text', '').strip()
                if shared_possible:
                    if _SHARED_ISSUES.search(paragraph):
                        shared_open = True
                    elif (not shared_open or _NOTE_BOILERPLATE.search(paragraph)
                          or not _SHARED_CONTINUATION.search(paragraph)):
                        shared_open = False
                    if not shared_open:
                        excluded.add(index)
                elif not active:
                    excluded.add(index)
            continue
        active = reference_pending = reference_next = False
        for (index, row), matches in zip(group, headers, strict=True):
            if matches:
                reference_next = reference_pending and matches[0] != issue_id
                active = matches[0] == issue_id
                reference_pending = False
            paragraph = row.get('text', '').strip()
            if active and _PRIOR_ISSUE_REFERENCE.search(paragraph):
                reference_pending = True
            is_reference_explanation = (not matches and reference_next
                                        and len(paragraph) <= _MAX_CORE_CHARS
                                        and re.match(r'^현재\s', paragraph)
                                        and re.search(r'확인(?:됩니다|됨|했습니다)', paragraph)
                                        and not _NOTE_BOILERPLATE.search(paragraph)
                                        and not re.search(r'(?:수정|패치|복구|배포|전달|QC|검수).{0,20}(?:완료|적용)',
                                                          paragraph, re.S | re.I))
            if not matches and reference_next:
                reference_next = False
            if not active and not is_reference_explanation:
                excluded.add(index)
    return [row for index, row in enumerate(available) if index not in excluded] + shared_prefixes + shared_cautions


from backend.evidence.stages import _DELIVERED
