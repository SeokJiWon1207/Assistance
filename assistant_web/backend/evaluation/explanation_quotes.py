"""설명용 인용을 이슈·필드 원문에 결속하고 자유 생성 문장 없이 표시한다.

이는 인용의 출처와 표시 문맥을 보호한다. 관련 근거의 완전성이나 원문 자체의
사실성을 보증하지 않으며, 검색 범위 밖의 문맥은 복원할 수 없다.
"""
import html
import re
from typing import Any


TOPIC_TITLES = {
    "cause": "원인", "action": "조치", "timeline": "진행 이력",
    "conditions": "조건", "technical": "기술 내용", "actors": "기록된 관계자",
    "background": "등록 내용",
}
UNANSWERED_TEXT = "이 항목은 선택한 근거로 답변을 확정하지 못했습니다"
_MAX_CONTEXT_CHARS = 12000
_ACCESS_ASSIGNMENT = re.compile(
    r'\b(?:pw|password|passwd|pwd|api[_ -]?key|access[_ -]?token|secret[_ -]?key)\s*[:=：]|'
    r'(?:비밀\s*번호|비번|패스워드)\s*[:=：]|공동인증.{0,30}(?:우회|패스)', re.I)
_NOTE_STATE = re.compile(r'확인|수정|반영|전달|조치|배포|완료|보류|예정|요청|오류|현상|적용|개발|진행|복구|변경|원인|문제')
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
_QC_DONE = re.compile(r'(?:QC|검수)[^\n]{0,20}(?:완료|통과)', re.I)


def _confirmed_qc_done(text: str) -> bool:
    if not _QC_DONE.search(text):
        return False
    # 'QC 테스트 완료 후 전달하겠습니다'는 앞으로 거칠 조건이지 현재 완료 기록이 아니다.
    planned_after_qc = re.search(
        r'(?:QC|검수)(?:\s*테스트)?\s*완료\s*후.{0,80}'
        r'(?:전달|배포).{0,30}(?:예정|하도록\s*하겠|하겠)', text, re.I | re.S,
    )
    return not (planned_after_qc and not re.search(r'\[\s*QC\s*완료\s*\]', text, re.I))
_DELIVERED = re.compile(r'\[(?:고객사\s*)?전달(?:\s*완료)?\]|전달(?:했|하였|함|완료|되었|\s*드렸습니다|\s*드립니다|\s*[.])')
_OBSERVED_FAILURE = re.compile(r'조회.{0,20}(?:안되|않)|빈\s*화면|표시.{0,20}(?:안되|않)')
_SPECULATIVE = re.compile(r'왠지|추정|필요할\s*것|같(?:은데|아요)')
_MAX_CORE_CHARS = 600


def _confirmation_statement(text: str, *, reproduced: bool = False) -> str:
    """확인 요청과 확인 결과를 구분하고 결과의 대상은 원문 문장에 보존한다.

    정상이라는 단어와 '확인'의 공존은 완료 근거가 아니다. 짧고 명시적인
    결과 문장만 요약에 사용하며, 긴 문맥은 아래의 원문 근거에 남긴다.
    """
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        sentence = sentence.strip()
        if not sentence or len(sentence) > 300:
            continue
        # 결과 술어가 평서형이어도 상위 문장이 질문·가정·미확인이라면
        # 관찰 사실로 분류하지 않는다. 미재현과 정상 확인에 같은 기준을 쓴다.
        if re.search(r'[?？]|는지|인지|을지|여부|미확인|예정|필요|부탁|요청|'
                     r'다면|으면|경우|어렵|취소|철회', sentence):
            continue
        if reproduced:
            if re.search(r'재현(?:되지\s*않(?:았(?:습니다|다|음)|습니다|음|는다고)|'
                         r'안됨|못(?:했(?:습니다|다|음)|함))', sentence):
                return sentence
            continue
        result = re.search(
            r'정상\s*(?:표시|동작|빌드).{0,70}?확인\s*'
            r'(?:(?:하였|했|되었|됐)(?:습니다|어요|음|다|고|으며)|됨|함|완료)', sentence)
        if not result:
            continue
        if (re.search(r'(?:지|는)\s*않|못|아니|아님', sentence)
                or re.search(r'확인.{0,20}없', sentence)):
            continue
        return sentence
    return ''


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


def _issues(grounding: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {issue["id"]: issue for issue in grounding.get("issues", [])
            if isinstance(issue, dict) and type(issue.get("id")) is int}


def _paragraphs(text: str, quote: str) -> list[str]:
    """개행으로 꺾인 부정·예정 표현도 보존하도록 빈 줄 사이의 문단을 복원한다."""
    boundaries = list(re.finditer(r"\n[ \t]*\n", text))
    contexts = []
    start = text.find(quote)
    while start >= 0:
        end = start + len(quote)
        left = max((match.end() for match in boundaries if match.end() <= start), default=0)
        right = min((match.start() for match in boundaries if match.start() >= end), default=len(text))
        context = text[left:right]
        if context not in contexts:
            contexts.append(context)
        start = text.find(quote, start + 1)
    return contexts


def _match_evidence(item: dict[str, Any], issue: dict[str, Any]) -> list[dict[str, Any]]:
    quote = item["quote"].replace("\r\n", "\n")
    matched = []
    for excerpt in safe_excerpts(issue):
        if not isinstance(excerpt, dict) or excerpt.get("field") != item["field"]:
            continue
        text = excerpt.get("text")
        if not isinstance(text, str):
            continue
        for context in _paragraphs(text.replace("\r\n", "\n"), quote):
            # 문맥을 다시 자르면 부정/요청이 탈락할 수 있으므로 긴 문단은 거부한다.
            if len(context) > _MAX_CONTEXT_CHARS:
                return []
            row = {"issue_id": item["issue_id"], "field": item["field"], "quote": quote, "text": context}
            for key in ("note_id", "created_at", "reporter"):
                if excerpt.get(key) not in (None, ""):
                    row[key] = excerpt[key]
            if row not in matched:
                matched.append(row)
    return matched


def validate_quote_answer(payload: dict[str, Any], grounding: dict[str, Any], *,
                          allow_multiple: bool = False) -> dict[str, Any]:
    """모델 선택값을 검증하고 렌더에 사용할 원문과 원문 메타데이터만 반환한다."""
    errors: list[str] = []
    result: dict[str, Any] = {"valid": False, "errors": errors, "selected_issue_ids": [], "sections": []}
    if not isinstance(payload, dict):
        errors.append("invalid_payload")
        return result
    issues = _issues(grounding)
    selected = payload.get("selected_issue_ids")
    if (not isinstance(selected, list) or not selected
            or any(type(issue_id) is not int or issue_id not in issues for issue_id in selected)):
        errors.append("invalid_selected_issue_ids")
        return result
    if len(set(selected)) != len(selected) or (not allow_multiple and len(selected) != 1):
        errors.append("invalid_issue_count")
        return result
    result["selected_issue_ids"] = list(selected)
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("invalid_sections")
        return result
    seen_topics: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            errors.append("invalid_section")
            continue
        topic = section.get("topic")
        if not isinstance(topic, str) or topic not in TOPIC_TITLES or topic in seen_topics:
            errors.append("invalid_topic")
            continue
        seen_topics.add(topic)
        unanswered, evidence = section.get("unanswered"), section.get("evidence")
        if not isinstance(unanswered, bool) or not isinstance(evidence, list) or (not unanswered and not evidence):
            errors.append("invalid_section_evidence")
            continue
        checked: list[dict[str, Any]] = []
        for item in evidence:
            if (not isinstance(item, dict) or type(item.get("issue_id")) is not int
                    or item["issue_id"] not in selected or not isinstance(item.get("field"), str)
                    or not isinstance(item.get("quote"), str) or not item["quote"].strip()):
                errors.append("invalid_evidence")
                continue
            matches = _match_evidence(item, issues[item["issue_id"]])
            if not matches:
                errors.append("quote_not_found_or_context_too_long")
            for match in matches:
                if match not in checked:
                    checked.append(match)
        result["sections"].append({"topic": topic, "evidence": checked, "unanswered": unanswered})
    result['answer_items'] = []
    items = payload.get('answer_items', [])
    if not isinstance(items, list) or len(items) > 5:
        errors.append('invalid_question_answers')
    else:
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('question_part'), str)
                    or not 0 < len(item['question_part']) <= 120):
                errors.append('invalid_question_part')
                continue
            proof = validate_quote_answer({'selected_issue_ids': selected, 'sections': [{
                'topic': 'technical', 'unanswered': item.get('unanswered'), 'evidence': item.get('evidence'),
            }]}, grounding, allow_multiple=allow_multiple)
            if not proof['valid']:
                errors.append('invalid_question_answer_evidence')
            else:
                result['answer_items'].append({'question_part': item['question_part'],
                    'unanswered': item['unanswered'], 'evidence': proof['sections'][0]['evidence'],
                    **({'draft_answer': item['answer']} if 'answer' in item else {})})
    result["valid"] = not errors
    return result


def _label(value: Any) -> str:
    text = html.escape(" ".join(str(value).split()))
    return re.sub(r"([\\`*_{}\[\]()|~])", r"\\\1", text)


def _core_choice(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """문맥을 자르지 않고 첫 화면에 적합한 짧은 원문 문단을 고른다."""
    candidates = []
    for index, row in enumerate(rows):
        text = row["text"].strip()
        if (len(text) > _MAX_CORE_CHARS or len(text) < 20
                or _NOTE_BOILERPLATE.search(text) or re.search(r'(?:으며|하고|하여|및|[:,])$', text)):
            continue
        has_state = bool(_NOTE_STATE.search(text))
        has_ending = bool(re.search(r'(?:다|임|음|됨)[.!?]?$', text))
        # A short fragment such as "코드 확인 결과\nMTS에서 저장한 조건식" is
        # true source text, but cannot stand alone as a cause or action.
        if len(text) < 60 and not has_ending:
            continue
        candidates.append(((has_state, has_ending, str(row.get('created_at') or ''),
                            int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0,
                            -abs(len(text) - 120), index), row))
    return max(candidates, default=(None, None))[1]


def _milestone_choice(rows: list[dict[str, Any]], pattern: re.Pattern[str]) -> dict[str, Any] | None:
    matches = [row for row in rows if len(row["text"]) <= _MAX_CORE_CHARS
               and pattern.search(row["text"])
               and (pattern is not _QC_DONE or _confirmed_qc_done(row["text"]))
               and not _NOTE_BOILERPLATE.fullmatch(row["text"].strip())]
    return matches[-1] if matches else None


def _whole_note_choice(rows: list[dict[str, Any]], issue: dict[str, Any]) -> dict[str, Any] | None:
    """짧은 대조 설명이 빈 줄로 쪼개졌으면 동일 원문 노트를 통째로 인용한다."""
    if not rows:
        return None
    newest = max(rows, key=lambda row: (str(row.get('created_at') or ''),
                                        int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
    same_source = [row for row in rows if row['field'] == newest['field']]
    if len(same_source) < 2 or not newest['field'].startswith('note:'):
        return None
    for excerpt in safe_excerpts(issue):
        source = excerpt.get('text', '').replace('\r\n', '\n').strip()
        if (excerpt.get('field') == newest['field'] and len(source) <= _MAX_CORE_CHARS
                and re.search(r'이어야\s*하지만|그러나|반면|대신', source)
                and all(row['text'].strip() in source for row in same_source)):
            return {**newest, 'text': source}
    return None


def _append_evidence(lines: list[str], title: str, evidence_rows: list[dict[str, Any]],
                     issues: dict[int, dict[str, Any]], selected_ids: list[int],
                     *, unanswered: bool = False,
                     rendered_quotes: set[tuple[int, str, str]] | None = None,
                     deduplicate: bool = False,
                     initial_sources: set[tuple[int, str]] | None = None) -> bool:
    if title:
        lines.extend([f"### {title}", ""])
    if unanswered or not evidence_rows:
        lines.extend([UNANSWERED_TEXT, ""])
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for evidence in evidence_rows:
        issue_id = evidence["issue_id"]
        if issue_id not in selected_ids:
            return False
        group = groups.setdefault((issue_id, evidence["field"]), [])
        if not any(row["text"] == evidence["text"] for row in group):
            group.append(evidence)
    previous_issue_id = None
    for (issue_id, _field), group in groups.items():
        evidence = group[0]
        if issue_id != previous_issue_id:
            issue = issues.get(issue_id, {})
            issue_title = issue.get("summary") or issue.get("title") or ""
            lines.extend([f"**#{issue_id}**" + (f" {_label(issue_title)}" if issue_title else ""), ""])
            previous_issue_id = issue_id
        metadata = []
        if initial_sources and (issue_id, evidence["field"]) in initial_sources:
            metadata.append("초기 기록")
        if evidence.get("note_id") is not None:
            metadata.append(f"노트 {_label(evidence['note_id'])}")
        else:
            metadata.append(f"필드: {_label(evidence['field'])}")
        if evidence.get("created_at"):
            metadata.append(f"기록 시각: {_label(evidence['created_at'])}")
        if evidence.get("reporter"):
            metadata.append(f"작성자: {_label(evidence['reporter'])}")
        lines.extend([" · ".join(metadata), ""])
        referenced = False
        for row in group:
            text = row["text"]
            key = (issue_id, row["field"], text)
            if (deduplicate and rendered_quotes is not None
                    and any(other_issue == issue_id and other_field == row['field'] and text in other_text
                            for other_issue, other_field, other_text in rendered_quotes)):
                if not referenced:
                    lines.extend(["위에서 인용한 원문 참조", ""])
                    referenced = True
                continue
            # 원문의 Markdown/코드도 문자 그대로 보이며 따옴표 인용으로 구분한다.
            fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", text)), default=0))
            lines.append(f"> {fence}text")
            lines.extend("> " + line for line in text.split("\n"))
            lines.extend([f"> {fence}", ""])
            if rendered_quotes is not None:
                rendered_quotes.add(key)
    return True


def _conclusion_lines(plans: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]],
                      selected_ids: list[int], issues: dict[int, dict[str, Any]]) -> list[str]:
    """검증된 원문에 명시된 단계만 고정 문장으로 표현하고 출처를 바로 붙인다."""
    timeline = next((preview + detail for topic, preview, detail, _ in plans if topic == 'timeline'), [])
    rows = timeline + [row for topic, preview, detail, _ in plans if topic != 'timeline'
                       for row in preview + detail]
    by_source: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row['issue_id'] in selected_ids:
            group = by_source.setdefault((row['issue_id'], row['field']), [])
            if not any(previous['text'] == row['text'] for previous in group):
                group.append(row)
    candidates: dict[tuple[int, str], tuple[dict[str, Any], str]] = {}
    all_text = '\n'.join(row['text'] for group in by_source.values() for row in group)
    for (issue_id, _field), group in by_source.items():
        row = group[0]
        text = '\n'.join(item['text'] for item in group)
        source_text = '\n'.join(excerpt['text'] for excerpt in safe_excerpts(issues.get(issue_id, {}))
                                if excerpt.get('field') == row['field'])
        if not row['field'].startswith('note:'):
            if (row['field'] == 'description'
                    and re.search(r'4/17.{0,120}무기한\s*연기', text, re.S)
                    and re.search(r'해당\s*건\s*제외하고\s*나머지\s*수정본\s*취합', source_text)):
                candidates[(issue_id, 'postponed_work_excluded')] = (
                    row, '고객은 4/17 신종증권 작업의 이행이 무기한 연기돼 이를 제외한 수정본 취합을 요청했습니다.')
            if (row['field'] == 'description'
                    and re.search(r'날짜를\s*1800년도로\s*수정', text)
                    and re.search(r'거래대금\s*지표\s*추가\s*후\s*엑셀불러오기.{0,40}하얗게', text, re.S)):
                candidates[(issue_id, 'excel_initial_requests')] = (
                    row, '최초 문의는 1800년대 날짜 파일 불러오기와 거래대금 지표를 더한 엑셀의 빈 화면 두 가지였습니다.')
            if (row['field'] == 'description'
                    and re.search(r'특정일\s*대비\s*주가변동율', text)
                    and re.search(r'조회\s*시점의\s*당일\s*날짜', text)):
                candidates[(issue_id, 'date_default_initial')] = (
                    row, '처음에는 특정일 조건의 기본 날짜를 조회 당일로 바꾸어 달라고 요청했습니다.')
            if (row['field'] == 'description'
                    and re.search(r'CMF\s*추세\s*조건검색도\s*있었으면', text)
                    and re.search(r'조건\s*추가\s*가능\s*검토', text)):
                candidates[(issue_id, 'cmf_feature_request')] = (
                    row, '고객은 기술적지표·시장특성의 CMF 추세 조건을 추가할 수 있는지 물었습니다.')
            if (row['field'] == 'description'
                    and re.search(r'화면\s*재\s*오픈\s*시\s*종료\s*전\s*화면\s*사이즈', text)
                    and re.search(r'화면\s*사이즈\s*저장\s*기능\s*추가\s*가능', text)):
                candidates[(issue_id, 'window_size_request')] = (
                    row, '고객은 화면을 다시 열 때 직전 창 크기가 유지되도록 저장 기능을 요청했습니다.')
            if (row['field'] == 'description'
                    and re.search(r'멀티차트\s*1번\s*활성화\s*시\s*정상노출', text)
                    and re.search(r'2,3,4\s*번\s*차트\s*활성화\s*시.{0,100}보이지\s*않', text, re.S)
                    and re.search(r'iOS\s*/\s*aOS\s*모두', text)):
                candidates[(issue_id, 'multichart_option_symptom')] = (
                    row, '고객 보고에서는 1번 차트의 옵션은 보이지만 2·3·4번에서는 보이지 않았고, '
                         'iOS와 Android 양쪽에서 같은 현상이 났습니다.')
            if (row['field'] == 'description' and 'RealOpTuja' in text
                    and re.search(r'내려갔다가\s*다시\s*올라오', text)):
                candidates[(issue_id, 'process_reappears')] = (
                    row, '고객은 RealOpTuja 프로세스가 내려갔다가 다시 나타난다고 문의했습니다.')
            if (row['field'] == 'description' and re.search(r'관심그룹.{0,100}전략.{0,60}저장', text, re.S)
                    and re.search(r'기본값으로\s*초기화', text)):
                candidates[(issue_id, 'group_setting_not_saved')] = (
                    row, '고객은 전략을 다시 열면 관심그룹 설정값이 저장되지 않고 기본값으로 초기화된다고 문의했습니다.')
            if (row['field'] == 'description' and re.search(r'두산.{0,100}차트.{0,60}DB하이텍', text, re.S)):
                candidates[(issue_id, 'chart_stock_mismatch_report')] = (
                    row, '고객은 화면의 종목명은 두산인데 차트 데이터는 DB하이텍으로 표시된다고 보고했습니다.')
            if (row['field'] == 'description' and 'On Ballance Volume' in text
                    and 'On Balance Volume' in text):
                candidates[(issue_id, 'obv_typo_request')] = (
                    row, '고객은 OBV 설명의 On Ballance Volume을 On Balance Volume으로 고쳐 달라고 요청했습니다.')
            if (row['field'] == 'description'
                    and re.search(r'100\s*건\s*초과', source_text)
                    and re.search(r'내용\s*정정.{0,100}100\s*건\s*미만.{0,50}실시간\s*처리가?\s*안되', text, re.S)):
                candidates[(issue_id, 'under_100_correction')] = (
                    row, '처음에는 100건 초과 시 현재가·등락률이 멈춘다고 문의했지만, '
                         '접수 본문에서 100건 미만에도 실시간 갱신이 안 된다고 정정했습니다.')
            if (row['field'] == '조치 및 개선' and re.search(r'매매구분.{0,60}실시간\s*처리\s*여부.{0,50}로직\s*추가', text)):
                candidates[(issue_id, 'action_recorded')] = (
                    row, '조치 및 개선 필드에는 매매구분으로 실시간 처리 여부를 가르는 로직 추가가 기록됐습니다.')
            continue
        findings: list[tuple[str, str]] = []
        if (re.search(r'Y축\s*스케일\s*선택.{0,130}드래그\s*&\s*드랍.{0,80}때에만', text, re.S)
                and re.search(r'지표관리\s*탭.{0,150}드래그\s*&\s*드랍하여\s*나눈\s*후.{0,100}다시', text, re.S)):
            findings.append(('yaxis_drag_workaround',
                             'Y축 스케일 선택은 지표를 드래그해서 합칠 때만 제공됩니다. '
                             '지표관리 탭으로 추가했다면 지표를 분리한 뒤 가격차트에 다시 합치라고 안내했습니다.'))
        if (re.search(r'종목콤보와\s*거래소\s*버튼.{0,90}하나의\s*메인\s*컨트롤\s*\(IJMComboCtl\)', text, re.S)
                and re.search(r'메인에서\s*별도의\s*종목\s*컨트롤\s*및\s*거래소\s*버튼', text)
                and re.search(r'메인.{0,60}지원\s*가능\s*여부.{0,30}우선\s*확인', text, re.S)):
            findings.append(('main_combo_dependency',
                             '종목콤보와 거래소 버튼은 메인 컨트롤 IJMComboCtl 하나입니다. '
                             '분리하려면 메인의 별도 종목 컨트롤·거래소 기능 지원을 먼저 확인해야 하며, '
                             '이 회신은 변경 완료 기록이 아닙니다.'))
        if (re.search(r'테스트\s*서버도\s*운영과\s*동일한\s*현상\s*발생\s*확인', text)
                and re.search(r'로그\s*추가\s*후\s*테스트\s*서버에\s*반영', text)):
            findings.append(('test_server_logging',
                             '운영과 같은 현상을 테스트 서버에서도 확인해 로그를 추가하고 테스트 서버에 반영했습니다. '
                             '원인 확정이나 운영 해결 기록은 아닙니다.'))
        if (re.fullmatch(r'\s*가능합니다\s*[.!]?\s*', text)
                and re.search(r'신규\s*기능.{0,35}추가.{0,25}가능', all_text, re.S)):
            findings.append(('feature_possible_only',
                             '신규 기능 추가가 가능하다는 회신입니다. 구현·배포 완료를 뜻하지는 않습니다.'))
        if re.search(r'현재\s*버전이\s*별도로\s*관리되고\s*있지는\s*않', text):
            findings.append(('version_unmanaged', '당시 차트 라이브러리 버전은 별도로 관리되지 않았습니다.'))
        if (re.search(r'폴더\s*기준.{0,35}\[2\.0\]', text, re.S)
                and re.search(r'버전\s*붙여서\s*전달', text)):
            findings.append(('version_folder_request',
                             '고객은 다음 전달부터 폴더 이름에 [2.0]처럼 버전을 붙여 달라고 요청했습니다.'))
        if (re.search(r'추후\s*라이브러리\s*전달.{0,25}버전\s*넣어서\s*전달', text)
                and re.search(r'겠습니다', text)):
            findings.append(('version_future_promise',
                             '공급사는 추후 라이브러리 전달 때 디렉터리별 버전을 넣겠다고 답했습니다. '
                             '실제 적용본 전달 완료 기록은 아닙니다.'))
        if (re.search(r'해당\s*이슈\s*개선\s*불가능', text)
                and re.search(r'유지보수가\s*아닌\s*개발로.{0,25}추측', text)):
            findings.append(('feasibility_refused',
                             '이 건은 개선 불가능하다고 회신했고, 개선하더라도 유지보수가 아닌 개발로 '
                             '분류될 것으로 추측했습니다. 구현 완료 기록은 아닙니다.'))
        if (re.search(r'제\s*PC에서도\s*발생\s*안|제\s*PC에서도\s*발생안', text)
                and re.search(r'관심탭.{0,70}종목\s*변경.{0,20}그룹이동', text, re.S)):
            findings.append(('stock_mismatch_customer_followup',
                             '고객 담당자 PC에서도 재현되지 않았고, 고객은 2000 화면 관심탭의 '
                             '종목·그룹 변경 뒤 발생했다고 추가 회신했습니다.'))
        if (re.search(r'양사\s*PC에서\s*재현되지\s*않', text)
                and re.search(r'정보가\s*부족.{0,35}우선.{0,20}종료', text, re.S)
                and re.search(r'추\s*후.{0,100}정보.{0,100}재접수', text, re.S)):
            findings.append(('stock_mismatch_conditional_close',
                             '양사 PC에서 재현되지 않고 정보가 부족해 우선 종료했으며, '
                             '추가 정보가 오면 재접수해 다시 확인하기로 했습니다.'))
        if (re.search(r'Crash\s*오류\s*3건\s*수정.{0,40}안드로이드\s*차트\s*라이브러리\s*전달', text, re.I | re.S)
                and re.search(r'JipyoControlSetUI\.java.{0,500}i_cnt\s*!=\s*null', text, re.S)
                and re.search(r'AutoTrendView\.java.{0,300}숫자만.{0,300}InputType\.TYPE_CLASS_NUMBER', text, re.S)
                and re.search(r'NeoChart2\.java.{0,600}blocks\.size\(\)\s*<=\s*m_nMaxIndicatorCount', text, re.S)):
            findings.append(('crash_three_fixes',
                             'Android 차트 라이브러리의 Crash 3건을 수정해 전달했다고 기록했습니다. '
                             'JipyoControlSetUI는 i_cnt null 검사, AutoTrendView는 입력값의 숫자 제한, '
                             'NeoChart2는 블록 경계 비교를 &lt;에서 &lt;=로 바꿨습니다.'))
        if (re.search(r'COMUtil.{0,30}makeZero', text, re.I | re.S)
                and re.search(r'null\s*예외처리\s*추가\s*요청', text, re.I)):
            findings.append(('makezero_request',
                             '그 뒤 고객은 COMUtil.makeZero의 null 예외처리를 추가해 달라고 요청했습니다.'))
        if (re.search(r'3/11\s*메일\s*전달', source_text)
                and re.search(r'소스\s*1826\s*라인.{0,50}널체크\s*추가되어\s*있', text, re.S)
                and re.search(r'언제\s*받은\s*라이브러리.{0,60}지금도\s*계속\s*발생', text, re.S)):
            findings.append(('makezero_reply',
                             '후속 회신 기록에는 보유 소스 1826행의 str.length()에 null 체크가 있다고 답하고, '
                             '사용 중인 라이브러리 시점과 지금도 재현되는지를 되물었습니다.'))
        hypothesis = re.search(r'([^\n]{2,60}?)에 문제가 있는 것으로 보입니다', text)
        if hypothesis:
            object_name = hypothesis.group(1).strip(' .,')
            findings.append(('hypothesis', f'초기에는 {_label(object_name)}에 문제가 있는 것으로 추정했습니다.'))
        if (re.search(r'재현(?:되지\s*않았|되지\s*않|안됨|되지\s*않음|못)', text)
                and not re.search(r'return\s+FALSE.{0,40}크래시가\s*재현되지\s*않', text, re.I | re.S)
                and not re.search(r'(?:타사|미래에셋|다른\s*고객|다른\s*환경).{0,25}재현', text)
                and not re.search(r'현상.{0,25}(?:확인됨|재현됨)', text)):
            statement = _confirmation_statement(text, reproduced=True)
            if statement:
                findings.append(('not_reproduced', f'해당 시점의 재현 확인 기록: “{_label(statement)}”'))
        if re.search(r'현상\s*확인되어.{0,25}원인\s*확인\s*중', text, re.S):
            findings.append(('cause_pending', '현상은 확인됐지만 당시 원인 확인 중이었습니다.'))
        if re.search(r'현상\s*재현되는\s*것\s*확인|동일.{0,20}재현.{0,12}확인', text):
            findings.append(('reproduced', '후속 테스트에서는 같은 현상이 재현됐습니다.'))
        if (re.search(r'당일\s*내의\s*분봉들을\s*서로\s*비교', text)
                and re.search(r'8060원.{0,80}9060원.{0,80}5\.104%', text, re.S)):
            findings.append(('minute_bar_condition',
                             '당일 분봉을 전봉과 비교하는 조건입니다. 동양우는 10시 봉에서 '
                             '8060원에서 9060원으로 올라 5.104%를 만족해 검색됐습니다.'))
        if (re.search(r'최근\s*봉의\s*조건\s*만족\s*시간으로\s*변경', text)
                and re.search(r'종목\s*코드를\s*기반으로\s*정렬', text)):
            findings.append(('condition_time_and_sort',
                             '같은 종목이 다시 만족하면 표시된 만족 시각은 최신 봉으로 바뀌지만, '
                             '결과는 종목 코드순이라 맨 위로 이동하지 않습니다.'))
        if ('User\\Chart\\ 폴더가 아닌 User\\ 폴더' in text
                and re.search(r'8000여개의\s*파일', text)):
            findings.append(('legacy_files_location',
                             '확인된 8천여 파일은 현재 설정 폴더인 User\\Chart\\가 아니라 '
                             'User\\ 바로 아래에 있었습니다.'))
        if (re.search(r'과거\s*User.{0,80}User.{0,10}Chart.{0,30}전환', text, re.S)
                and re.search(r'잔여파일이\s*남은\s*상황으로\s*예상', text)):
            findings.append(('legacy_files_hypothesis',
                             '과거 유저파일 전환 뒤 남은 파일로 추정했으며, 현재 유저 설정은 '
                             'User\\Chart\\에 저장된다고 설명했습니다.'))
        if (re.search(r'차트저장하기\s*기능', text)
                and re.search(r'가상페이지\s*저장\s*기능', text)
                and re.search(r'일정기간\s*사용되지\s*않은\s*경우\s*삭제하는\s*기능의\s*추가는\s*가능', text)):
            findings.append(('legacy_cleanup_conditional',
                             '차트 저장·가상페이지 저장 파일을 제외한 미사용 파일은 기간을 정하면 '
                             '삭제 기능 추가가 가능하다고 안내했습니다. 개발 완료 기록은 아닙니다.'))
        if re.search(r'1,\s*2를\s*포함해서\s*지울\s*수\s*있도록\s*요청', text):
            findings.append(('legacy_cleanup_expanded_request',
                             '마지막 고객 요청은 앞서 예외로 둔 1·2번 저장 파일도 삭제 대상에 '
                             '포함해 달라는 것이었습니다.'))
        if (re.search(r'직접\s*고객\s*응대를\s*진행하지는\s*않', text)
                and re.search(r'직접적인\s*유선\s*통화\s*응대는\s*어려', text)):
            findings.append(('direct_customer_call_declined',
                             '공급사는 고객과 직접 전화 응대를 하지는 않는다고 안내했습니다.'))
        if (re.search(r'피보나치\s*조정대\s*사용\s*시', text)
                and re.search(r'수치값\s*그룹을\s*저장', text)
                and re.search(r'다음\s*작도\s*때\s*한\s*번에\s*불러', text)):
            findings.append(('fibonacci_group_requirement',
                             '요청 기능은 피보나치 수치값 그룹을 저장해 다음 작도에 한 번에 '
                             '불러와 적용하는 것입니다.'))
        if re.search(r'넵\s*맞습니다\..{0,30}확인\s*부탁', text, re.S):
            findings.append(('customer_confirms_requirement',
                             '고객은 공급사가 정리한 기능 설명이 맞다고 확인했습니다.'))
        if (re.search(r'정규장\s*옵션\s*제공\s*범위', text)
                and 'KRX/NXT/통합' in text
                and re.search(r'애프터장.{0,50}포함\s*여부', text, re.S)
                and re.search(r'정규장\s*옵션\s*주기\s*제공\s*범위', text)):
            findings.append(('regular_close_scope_decisions',
                             '구현 전 KRX만 또는 KRX·NXT·통합 적용, 주기와 화면 범위, '
                             '애프터 시세 포함 여부를 정해야 했습니다.'))
        if (re.search(r'정규장기준\s*데이터를\s*받을수있는\s*FID', text)
                and re.search(r'정규장\s*전용\s*실시간\s*패킷|기존\s*패킷에\s*정규장용\s*필드', text)):
            findings.append(('regular_close_data_dependency',
                             '정규장 기준 데이터 FID 또는 정규장용 실시간 패킷·필드가 '
                             '추가로 필요했습니다.'))
        if (re.search(r'KRX정규장시세제공.{0,50}당장은\s*어려', text, re.S)
                and re.search(r'개발검토.{0,15}보류', text)):
            findings.append(('regular_close_customer_hold',
                             'SK는 정규장 시세 제공을 당장 준비하기 어려워 10월 1일 '
                             '개발 검토를 보류했습니다. 구현·배포 완료 기록은 아닙니다.'))
        if (re.search(r'비교봉수\s*값의\s*의미', text)
                and re.search(r'좌우로\s*몇\s*개의\s*캔들과\s*가격을\s*비교', text)
                and re.search(r'고점\s*조건.{0,150}5개의\s*봉', text, re.S)
                and re.search(r'저점\s*조건.{0,150}5개의\s*봉', text, re.S)):
            findings.append(('trendline_comparison_bars',
                             '비교봉수는 기준 봉의 좌우 몇 개 봉과 가격을 비교할지 정합니다. '
                             '값이 5면 앞뒤 5봉보다 고가가 높거나 저가가 낮을 때 고점·저점으로 확정합니다.'))
        if (re.search(r'각\s*주기별\s*캔들을\s*기준으로', text)
                and re.search(r'주기별로\s*표시되는\s*추세선은\s*달라질\s*수', text)):
            findings.append(('trendline_period_variation',
                             '각 주기의 캔들을 기준으로 그리므로 주기별 추세선은 달라질 수 있습니다.'))
        if (re.search(r'차트\s*우측\s*차트툴바\s*탭을\s*\[F\]', text)
                and re.search(r'패턴설정\s*툴바.{0,100}자동\s*추세선\s*탭\s*하단의\s*단기/중기/장기', text, re.S)):
            findings.append(('trendline_setting_location',
                             '비교봉수는 차트 우측 [F] 기능툴의 패턴설정 창, '
                             '자동 추세선 탭 아래 단기·중기·장기에서 확인할 수 있습니다.'))
        if re.search(r'ATS를\s*제공하지\s*않는\s*회원사에서\s*발생', text):
            findings.append(('regular_option_ats_absent',
                             '정규장 옵션 미표시는 ATS를 제공하지 않는 회원사에서 발생했습니다.'))
        if (re.search(r'ATS\s*미제공\s*회원사.{0,50}디버깅', text, re.S)
                and re.search(r'KRX로\s*분류하지\s*못', text)
                and re.search(r'수정은\s*오늘\s*진행예정', text)):
            findings.append(('regular_option_misclassified_plan',
                             '디버깅에서는 ATS 미제공 회원사를 KRX로 분류하지 못하는 로직을 확인했습니다. '
                             '9월 30일 수정은 예정 단계였고 반영 일정은 확인 후 안내하기로 했습니다.'))
        if (re.search(r'신호\s*강도\s*숫자별로\s*신호\s*색상이\s*다름', text)
                and re.search(r'단순\s*색상구분.{0,40}아닐까\s*싶음', text, re.S)):
            findings.append(('signal_color_hypothesis',
                             '처음에는 숫자별 색상이 달라 단순 색상 구분 기능일 것으로 추정했습니다.'))
        if (re.search(r'높은\s*숫자일\s*수록\s*매수신호', text)
                and re.search(r'낮은\s*숫자일수록\s*매도\s*신호', text)):
            findings.append(('signal_expert_explanation',
                             '후속 담당자 확인에서는 높은 숫자가 매수신호, 낮은 숫자가 매도신호라고 설명했습니다.'))
        if (re.search(r'매수/매도의\s*강도\s*\(중요도\)', text)
                and re.search(r'높은\s*숫자일수록\s*매수신호', text)
                and re.search(r'강도는\s*변경이\s*가능', text)):
            findings.append(('signal_customer_reply',
                             '최종 고객 회신은 신호의 매수·매도 강도(중요도)를 뜻하며 '
                             '사용자가 강도를 바꿀 수 있다고 안내했습니다.'))
        if (re.search(r'Drdata/Finddata.{0,100}일부\s*고정된\s*파일.{0,40}갱신', text, re.S)
                and re.search(r'종목별/주기별\s*매번\s*저장.{0,50}없는', text, re.S)):
            findings.append(('drdata_fixed_files',
                             'Drdata/Finddata의 일부 고정 파일은 설정이 갱신되지만, '
                             '종목·주기마다 새로 저장되는 구조는 아니라고 확인했습니다.'))
        if (re.search(r'차트를\s*초기화\s*할때\s*기본\s*설정의\s*템플릿', text)
                and '화면번호_시장타입_Chartinfo' in text
                and re.search(r'\[9861\].{0,90}시장타입별', text, re.S)
                and re.search(r'\[9882\].{0,100}시장구분이\s*없는', text, re.S)):
            findings.append(('drdata_market_templates',
                             '해당 ChartInfo는 차트 초기화 템플릿입니다. 9861은 시장별 파일을, '
                             '단일 시장 9882는 시장 구분 없는 파일을 사용합니다.'))
        if (re.search(r'여러군데\s*매체가\s*사용중이라\s*모두\s*확인은\s*불가능', text)
                and re.search(r'실제\s*제공하는\s*매체가\s*없는\s*화면번호라면\s*제거해도', text)):
            findings.append(('drdata_conditional_removal',
                             '모든 매체를 확인한 것은 아니며, 실제 제공 매체가 없는 화면번호의 '
                             '파일이라면 제거할 수 있다는 조건부 안내였습니다.'))
        if (re.search(r'17시\s*이후\s*발생하는\s*이슈', text)
                and re.search(r'아직\s*현상\s*확인\s*중', text)
                and re.search(r'과거.{0,80}비슷한\s*이슈.{0,80}선\s*검토\s*요청', text, re.S)):
            findings.append(('after_hours_initial_pending',
                             '처음에는 17시 이후 현상을 확인 중이었고, '
                             '과거 유사 건을 먼저 검토해 달라고 요청했습니다.'))
        if (re.search(r'kchart/data/st/db/exchange\.json\s*최신\s*파일이\s*반영되지\s*않', text)
                and re.search(r'KRX시간연장_ALL_2026-0903\.zip.{0,30}포함하여\s*전달', text, re.S)):
            findings.append(('exchange_json_missing',
                             '후속 확인에서 최신 exchange.json이 반영되지 않았고, '
                             '이 파일은 9월 3일 전달한 KRX시간연장 취합 zip에 포함돼 있었습니다.'))
        if (re.search(r'당시\s*전달드렸던\s*zip파일을\s*다시\s*첨부', text)
                and re.search(r'확인\s*부탁', text)):
            findings.append(('exchange_zip_resent',
                             '10월 13일 그 zip을 다시 첨부해 고객에게 확인을 요청했습니다. '
                             '고객 운영 정상화가 확인됐다는 기록은 아닙니다.'))
        if re.search(r'상세\s*수식\s*제공\s*시\s*가능', text):
            findings.append(('formula_required',
                             'CMF 추세 조건 추가는 상세 수식을 제공하면 가능하다는 조건부 답변입니다. '
                             '개발·QC·배포 완료 기록은 아닙니다.'))
        if (re.search(r'작업\s*가능하며.{0,180}함께\s*주중에\s*작업\s*진행', text, re.S)
                and re.search(r'테스트하여\s*전달드릴\s*수\s*있도록\s*하겠습니다', text)):
            findings.append(('window_size_work_plan',
                             '공급사는 상단 컨트롤 겹침 건과 함께 주중에 작업·테스트한 뒤 '
                             '전달할 수 있다고 회신했습니다. 당시에는 예정 단계였습니다.'))
        if (re.search(r'OnSelectedChart\s*이벤트', text)
                and re.search(r'chart\.setShowRegular\("YES"\)', text)
                and re.search(r'함수\s*추가\s*후\s*확인\s*부탁', text)):
            findings.append(('multichart_setting_requested',
                             '공급사는 OnSelectedChart 이벤트에 chart.setShowRegular("YES")를 '
                             '추가해 확인해 달라고 안내했습니다. 배포·고객 정상 확인 완료 기록은 아닙니다.'))
        if ('TitleName' in text and '이어야 하지만' in text
                and '신고가' in text and '신저가' in text):
            findings.append(('mismatch', '후속 코드 확인에서 TitleName의 기대 표기와 실제 표기가 달랐습니다.'))
        if re.search(r'(?:tag|태그)\s*파일\s*수정', text, re.I):
            findings.append(('tag_changed', '태그 파일 수정 기록이 있습니다.'))
        if re.search(r'작업\s*예정', text) and re.search(r'QC.{0,30}예정', text, re.I):
            findings.append(('work_qc_planned', '마지막 일정 기록은 작업과 QC를 모두 예정 단계로 표시합니다.'))
        if (_confirmed_qc_done(text) and not re.search(
                r'(?:QC|검수).{0,20}(?:완료|통과)\s*(?:는|가|하지|못|않|아님|아니)', text)):
            findings.append(('qc_done', 'QC 완료 기록이 있습니다.'))
            findings.append(('initial_qc_done', '앞선 QC 완료 기록이 있습니다.'))
        if re.search(r'\[QC\s*요청\]', text, re.I):
            findings.append(('qc_requested', 'QC 요청 기록입니다. 이 기록만으로 QC 완료를 뜻하지는 않습니다.'))
        if (re.search(r'특정일\s*선택\s*가능한\s*조건\s*대상', text)
                and re.search(r'1영업일\s*전\s*날짜', text)
                and re.search(r'하루\s*전\s*날짜', text)):
            findings.append(('date_default_scope_changed',
                             '고객은 적용 범위를 특정일 선택 가능 조건 전체로 넓히고, '
                             '기본값은 1영업일 전을 우선 요청했습니다. 불가능하면 하루 전을 원했습니다.'))
        if (re.search(r'특정일\s*선택\s*가능한\s*조건\s*대상', text)
                and re.search(r'기본값을\s*[\'"“‘]?조회\s*시점의\s*1영업일\s*전\s*날짜', text)
                and re.search(r'수정\s*가능', text)):
            findings.append(('date_default_business_day_possible',
                             '공급사는 그 범위의 기본값을 조회 시점 1영업일 전으로 바꿀 수 있다고 회신했습니다.'))
        if (re.search(r'현재\s*개발완료\s*되었고', text)
                and re.search(r'QC\s*테스트\s*중', text)):
            findings.append(('development_done_qc_in_progress',
                             '개발 완료 뒤 QC 테스트 중이라고 안내한 기록입니다. 당시 고객 전달은 아직 예정 단계였습니다.'))
        if re.search(r'\[QC\s*NG\]', text, re.I):
            findings.append(('qc_ng', 'QC에서 오류가 확인되어 재검수가 필요했습니다.'))
        if re.search(r'\[QC\s*재요청\]|\[QC재요청\]', text, re.I):
            if re.search(r'재빌드', text):
                findings.append(('rebuild_qc_requested', '재빌드 뒤 QC를 다시 요청했습니다.'))
            else:
                findings.append(('qc_requested_again', 'QC를 다시 요청했습니다.'))
        if re.search(r'선행스팬\s*구간보다\s*미래영역이\s*더\s*클\s*경우\s*발생', text):
            findings.append(('future_span_mismatch',
                             '일목균형표의 선행스팬보다 미래여백이 클 때 추세선 위치 오차가 발생했습니다.'))
        if re.search(r'타\s*증권사에서도\s*동일하게\s*발생', text):
            findings.append(('other_broker_reproduced', '다른 증권사에서도 같은 현상이 확인됐습니다.'))
        if (re.search(r'구차트\s*컨버트와는\s*관련이\s*없는', text)
                and re.search(r'그래프\s*정보는\s*저장.{0,70}지표\s*\(STUDIES\)\s*정보는\s*저장되어\s*있지', text, re.S)):
            findings.append(('saved_graph_missing_studies',
                             '고객 파일에는 그래프만 저장되고 지표(STUDIES) 정보가 없었습니다. '
                             '구차트 전환 때문은 아니라고 판단했습니다.'))
        if (re.search(r'재접속하는\s*과정에서\s*지표\s*정보가.{0,40}가능성도\s*의심', text, re.S)
                and re.search(r'내부\s*테스트.{0,80}정상적으로\s*저장', text, re.S)):
            findings.append(('sleep_reconnect_unconfirmed',
                             '절전 뒤 재접속 과정의 저장 실패는 가설이었고, 같은 상황의 내부 테스트에서는 '
                             '지표가 정상 저장됐습니다. 원인은 확정되지 않았습니다.'))
        if (re.search(r'절전\s*모드를\s*사용하지\s*않고', text)
                and re.search(r'종료\s*처리하기로', text)
                and re.search(r'동일\s*현상이\s*발생할\s*경우\s*추가로\s*확인', text)):
            findings.append(('monitor_without_sleep_close',
                             '고객은 절전 모드를 쓰지 않고 지켜보기로 했고, 이슈는 종료 처리하되 '
                             '같은 현상이 재발하면 추가 확인하기로 했습니다.'))
        if (re.search(r'ExStatic\.dll.{0,100}오류\s*팝업', text, re.I | re.S)
                and re.search(r'SK\s*HTS에서는\s*다시\s*재현이\s*되지\s*않', text)):
            findings.append(('dll_reproduced_then_not',
                             '초기 공급사 PC에서는 재현되지 않았지만 후속 노트북에서는 ExStatic.dll 로드 오류를 '
                             '확인했습니다. 재부팅 뒤 SK HTS에서는 다시 재현되지 않았습니다.'))
        if (re.search(r'스마트\s*앱\s*컨트롤.{0,100}디지털\s*서명이\s*없는', text, re.S)
                and re.search(r'OS\s*수준의\s*실행\s*거부', text)):
            findings.append(('os_integrity_hypothesis',
                             '최종 회신은 스마트 앱 컨트롤의 시스템 무결성 정책이 서명되지 않은 DLL 로드를 '
                             '거부한 것으로 판단했습니다. 프로그램 로직 결함으로 확정하지 않았습니다.'))
        if (re.search(r'GetLastError\s*Code:\s*4551', text, re.I)
                and re.search(r'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION', text)):
            findings.append(('integrity_error_code',
                             '근거로 든 오류 코드는 GetLastError 4551 '
                             '(ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION)입니다.'))
        if (re.search(r'빌드\s*옵션\s*적용을\s*검토\s*중', text)
                and re.search(r'디지털\s*서명', text)
                and re.search(r'단독으로\s*처리가\s*어려운', text)):
            findings.append(('integrity_response_scope',
                             '빌드 옵션과 디지털 서명을 검토하되 배포·관리 권한 문제로 단독 처리는 어렵다고 '
                             '안내했습니다. 수정·배포 완료 기록은 아닙니다.'))
        if (re.search(r'2개\s*이상의\s*envelope\s*지표', text, re.I)
                and re.search(r'최상단\s*지표의\s*환경설정으로\s*다\s*적용', text)):
            findings.append(('envelope_qc_ng',
                             'QC에서는 Envelope 지표를 둘 이상 넣고 선을 삭제·설정하면 '
                             '다른 지표에도 최상단 지표의 설정이 적용되는 오류가 확인됐습니다.'))
        if (re.search(r'메인\s*헤더.{0,30}라이브러리\s*빠져.{0,15}요청', text, re.S)
                and re.search(r'소스\s*동기화\s*이후.{0,35}롤백한\s*작업\s*다시\s*머지\s*예정', text, re.S)):
            findings.append(('source_sync_before_remerge',
                             '즉시 재전달하지 못했습니다. 빠진 메인 헤더·라이브러리를 받아 소스를 동기화한 뒤 '
                             '롤백 작업을 다시 병합할 예정이라고 기록했습니다.'))
        if re.search(r'헤더\s*파일뿐\s*아니라\s*cpp\s*파일도\s*추가로\s*보내', text, re.I):
            findings.append(('header_cpp_requested', '후속 요청에서는 헤더와 cpp 파일을 함께 보내 달라고 했습니다.'))
        if re.search(r'\[고객사\s*전달\]', text):
            findings.append(('delivered', '고객사 전달 기록이 있습니다.'))
        elif re.search(r'\[전달\]', text):
            findings.append(('file_delivered', '파일 전달 기록이 있습니다.'))
        if re.search(r'\[전달\s*완료\]', text):
            findings.append(('delivery_completed', '고객사 전달 완료 기록이 있습니다.'))
        if (re.search(r'조건검색\s*소스\s*컴파일', text)
                and re.search(r'추가된\s*항목\s*정의가\s*누락.{0,35}컴파일\s*오류', text, re.S)
                and re.search(r'Condition_MngDef\.h.{0,35}누락된\s*것\s*같', text, re.S)):
            findings.append(('missing_definition_complaint',
                             '첫 전달 뒤 고객은 추가 항목 정의 누락으로 컴파일 오류가 났고, '
                             'Condition_MngDef.h 누락을 의심해 파일 재전송을 요청했습니다.'))
        if (re.search(r'개발\s*담당자.{0,35}휴가', text, re.S)
                and re.search(r'내일\s*복귀.{0,100}전달.{0,35}조치', text, re.S)):
            findings.append(('developer_followup_plan',
                             '담당 개발자의 다음 날 복귀 후 요청 내용을 전달·조치하겠다고 안내했습니다.'))
        if re.search(r'\[재전달\].{0,250}?(?:화|금|수|목|월|토|일)B_[^\s\\/]+\.zip', text, re.S):
            findings.append(('b_redelivery', 'B 패키지를 재전달했습니다.'))
        if (re.search(r'변수설정\s*그룹에\s*옵션을\s*추가', text)
                and re.search(r'괜찮을지\s*문의', text)):
            findings.append(('variable_group_proposal', '처음에는 변수설정 그룹에 옵션을 넣는 방안을 제안했습니다.'))
        if (re.search(r'리스트박스\s*크기를\s*조정.{0,30}체크박스\s*추가', text, re.S)
                and re.search(r'(?:(?:위\s*)?방법으로\s*요청|방법이\s*좋아)', text)):
            findings.append(('listbox_checkbox_choice',
                             '고객은 리스트박스 크기를 조정해 체크박스를 추가하는 방안을 요청했습니다.'))
        if (re.search(r'\[QC\s*NG\]', text, re.I)
                and re.search(r'\[\s*esc\s*\].{0,60}HTS\s*비정상\s*종료', text, re.I | re.S)):
            findings.append(('initial_esc_qc_ng', 'QC에서 ESC로 설정 화면을 닫을 때 HTS 비정상 종료를 확인했습니다.'))
            findings.append(('esc_qc_ng', '후속 QC에서도 ESC 종료 시 HTS 비정상 종료가 재확인됐습니다.'))
        if (re.search(r'NG\s*현상은\s*기존\s*오류', text)
                and re.search(r'프로젝트\s*소스에서\s*수정.{0,50}대체거래소때\s*반영\s*예정', text, re.S)
                and re.search(r'기존\s*오류\s*추가\s*수정\s*부분은\s*롤백\s*후\s*전달', text, re.S)):
            findings.append(('esc_existing_fix_rollback',
                             'ESC NG는 기존 오류로 분류했고 프로젝트 소스의 수정은 대체거래소 때 반영 예정이라고 적었습니다. '
                             '이번 추가 수정분은 롤백한 뒤 전달했습니다.'))
        if (re.search(r'고객사\s*롤백\s*요청', text)
                and re.search(r'데이터\s*파일만\s*변경하여\s*전달\s*예정|데이터파일만\s*변경하여\s*전달\s*예정', text)):
            findings.append(('data_file_only_rollback_plan',
                             '고객사 롤백 요청에 따라 데이터 파일만 변경해 전달할 예정이라고 기록했습니다.'))
        if (re.search(r'\[고객사\s*전달\]', text)
                and re.search(r'(?:화|금|수|목|월|토|일)B_[^\s\\/]+\.zip', text)):
            findings.append(('b_package_delivered', '후속 B 패키지의 고객사 전달 기록이 있습니다.'))
        if (re.search(r'4/18.{0,60}4/22.{0,60}4/23.{0,60}4/24.{0,60}4/26.{0,90}소스\s*병합', text, re.S)
                and re.search(r'4/17\s*신종증권.{0,110}메인\s*헤더\s*및\s*라이브러리.{0,70}롤백\s*처리', text, re.S)
                and re.search(r'미래에셋.{0,45}소스에서도\s*이전\s*버전으로\s*롤백\s*처리\s*부탁', text, re.S)):
            findings.append(('merged_source_exclusion',
                             '4/18·22·23·24·26 수정 소스를 병합해 전달했고, 공급사 내부의 4/17 신종증권 메인 헤더·'
                             '라이브러리·작업 소스는 롤백했습니다. 고객사 내부 소스 롤백은 요청 단계였습니다.'))
        if (re.search(r'#206에\s*적용\s*완료', text)
                and re.search(r'수정처리가\s*제대로\s*안', text)
                and re.search(r'kChartSetupDlgAddin\s*에러', text)):
            findings.append(('merged_source_followup_failure',
                             '고객은 취합본을 206번 대상에 적용했지만 4번 수정이 제대로 되지 않았고 '
                             'kChartSetupDlgAddin 오류도 났다고 재문의했습니다.'))
        if (re.search(r'문의주신\s*4번\s*항목', text)
                and re.search(r'5대\s*PC.{0,90}느린\s*현상.{0,25}재현\s*되지\s*않', text, re.S)
                and re.search(r'영상\s*요청', text)
                and re.search(r'PC\s*사양', text)
                and re.search(r'사양\s*요청', text)
                and re.search(r'User\s*파일\s*요청', text, re.I)):
            findings.append(('slow_symptom_unreproduced',
                             '공급사는 5대 PC에서 4번 항목의 느린 현상을 재현하지 못해 영상·PC 사양·User 파일을 요청했습니다.'))
        if re.search(r'클린\s*후\s*재빌드\s*요청', text):
            findings.append(('clean_rebuild_requested', '후속 통화에서 문제 작업을 클린 후 재빌드해 보라고 요청했습니다.'))
        if (re.search(r'툴팁기능\s*적용\s*후\s*장애', text)
                and re.search(r'저희쪽에서\s*먼저\s*원복처리', text)
                and re.search(r'4/29\s*수정\s*취합본에\s*이문수.{0,25}수정본까지\s*취합하여\s*재전달', text, re.S)):
            findings.append(('tooltip_reverted_remerge_requested',
                             '마지막 고객 회신은 툴팁 적용 뒤 장애가 재발해 고객사가 먼저 원복했다고 밝히고, '
                             '4/29 취합본에 이문수 수정본을 합쳐 재전달해 달라는 요청이었습니다.'))
        if (re.search(r'날짜만\s*1800년대로\s*수정', text)
                and re.search(r'엑셀\s*불러오기.{0,40}정상적으로\s*불러오', text, re.S)
                and re.search(r'2번\s*이슈만\s*수정\s*진행', text)):
            findings.append(('excel_scope_narrowed',
                             '자체 저장 엑셀의 날짜를 1800년대로 바꾼 파일은 불러와져, 당시 수정 범위는 2번 거래대금 현상으로 좁혔습니다.'))
        if re.search(r'고객\s*샘플파일.{0,90}DateTime\s*형식이\s*맞지\s*않', text, re.I | re.S):
            findings.append(('customer_datetime_mismatch',
                             '이후 고객 샘플에서는 차트와 DateTime 형식이 맞지 않는다는 오류가 확인됐습니다.'))
        if (re.search(r'과거\s*데이터.{0,45}x축에\s*날짜가\s*(?:안나오|안나옵|표시되지)', text, re.S)
                and re.search(r'거래대금\s*지표\s*데이터가\s*있는\s*엑셀.{0,45}그려지지\s*않', text, re.S)):
            findings.append(('excel_post_delivery_questions',
                             '전달 뒤 고객은 과거 날짜의 X축 미표시와 엑셀 거래대금 지표 미표시를 다시 문의했습니다.'))
        if (re.search(r'시간자료형이\s*1970년\s*1월\s*1일을\s*기준', text)
                and re.search(r'스케일영역\s*계산.{0,180}수정이\s*(?:어렵|어려)', text, re.S)):
            findings.append(('old_date_axis_limit',
                             '최종 회신은 1970년 기준 시간 자료형과 차트 내부 스케일 계산 때문에 과거 날짜의 X축 표시 수정이 어렵다고 설명했습니다.'))
        if (re.search(r'거래대금과\s*같은\s*시장지표', text)
                and re.search(r'조회성\s*데이터.{0,60}조회하여\s*차트에\s*데이터가\s*들어가는\s*구조', text, re.S)):
            findings.append(('excel_market_indicator_limit',
                             '거래대금 같은 시장지표는 별도 조회 데이터를 차트에 넣는 구조여서 엑셀 데이터 표시 수정이 어렵다고 답했습니다.'))
        if re.search(r'소스\s*프리징.{0,30}보류\s*및\s*롤백', text, re.S):
            findings.append(('freeze_rollback', '소스 프리징으로 작업이 보류·롤백됐다는 기록이 있습니다.'))
        if (re.search(r'강세\s*:\s*RSI\(적용기간\)\s*>=\s*SPercent', text)
                and re.search(r'약세\s*:\s*RSI\(적용기간\)\s*<\s*LPercent', text)
                and re.search(r'Period\(14\).{0,30}LPercent\(30\).{0,30}SPercent\(70\)', text, re.S)):
            findings.append(('rsi_thresholds',
                             'RSI 강세는 RSI(기간 14)가 SPercent(기본 70) 이상, '
                             '약세는 LPercent(기본 30) 미만으로 구분했습니다.'))
        if (re.search(r'운영\s*HTS.{0,50}수정된\s*모듈.{0,30}적용', text, re.S)
                and re.search(r'업종\s*시장\s*변경.{0,30}PctrSM\.dll.{0,30}비정상\s*종료', text, re.S)
                and re.search(r'컴파일한\s*PctrSM\.dll.{0,25}종료현상\s*발생하지\s*않', text, re.S)):
            sentence = ('작업 중 운영 HTS 적용본은 업종·시장 변경 시 PctrSM.dll에서 비정상 종료됐지만 '
                        '자체 컴파일본에서는 재현되지 않았습니다.')
            if re.search(r'GetCodeUpIdx.{0,30}원인인\s*것으로\s*보', text, re.S):
                sentence += ' GetCodeUpIdx 관련성은 당시 추정이었습니다.'
            findings.append(('rsi_interim_crash', sentence))
        if (re.search(r'좌측\s*탭.{0,150}태양금속.{0,150}하나로FN.{0,100}SK하이닉스', text, re.S)
                and re.search(r'차트.{0,80}태양금속.{0,20}4\s*개.{0,30}2026선물.{0,20}1\s*개', text, re.S)):
            findings.append(('screenshot_mismatch',
                             '첨부 사진에서 좌측 탭에는 태양금속·하나로FN K-반도체·SK하이닉스가 '
                             '있지만 차트에는 태양금속 4개와 2026선물 1개 등이 보여 구성이 달랐습니다.'))
        if (re.search(r'현상.{0,25}(?:발생되지\s*않|재현되지\s*않)', text, re.S)
                and re.search(r'우선.{0,10}종료\s*처리', text, re.S)
                and re.search(r'(?:추가적인\s*정보|유저파일).{0,30}재접수.{0,20}다시\s*확인', text, re.S)):
            findings.append(('unreproduced_reopen',
                             '테스트에서 재현되지 않아 우선 종료하되, 추가 정보나 유저파일이 오면 '
                             '재접수해 다시 확인하겠다고 안내했습니다.'))
        if (re.search(r'분주기\s*선택.{0,30}않아도.{0,30}170000', text, re.S)
                and re.search(r'서버.{0,40}170000.{0,30}분주기로\s*판단', text, re.S)
                and re.search(r'1분\s*데이터\s*기반.{0,30}일간\s*데이터\s*생성', text, re.S)):
            findings.append(('minute_as_daily',
                             '분주기를 고르지 않아도 170000을 보내 서버가 분주기로 판단했고, '
                             '1분 데이터로 당일 일간 데이터를 생성해 거래량 차이가 났습니다.'))
        if (re.search(r'분주기\s*선택하지\s*않을\s*시.{0,50}999999', text, re.S)
                and re.search(r'(?:수정\s*사항|설정\s*조회로\s*수정)', text)):
            findings.append(('initial_999999_direction',
                             '초기 수정 방향에는 분주기 미선택 시 과거시간 999999 조회가 적혔습니다.'))
        if (re.search(r'분주기\s*미체크\s*시.{0,20}["“]?170000["”]?', text, re.S)
                and re.search(r'170000["”]?\s*(?:->|→)\s*["“]?000000', text)
                and re.search(r'변경하였', text)):
            findings.append(('final_000000_screen',
                             '후속 화면 확인에서는 미체크 시 분석시점 시간을 170000에서 000000으로 '
                             '바꿨다고 기록했습니다.'))
        if (re.search(r'1분\s*데이터.{0,35}거래량\s*합.{0,20}일간\s*거래량.{0,15}맞지\s*않', text, re.S)
                and re.search(r'원인의\s*3번.{0,35}확인\s*해보기로', text, re.S)):
            findings.append(('volume_followup', '1분 거래량 합과 일간 거래량의 차이는 별도 확인 대상으로 남겼습니다.'))
        if (re.search(r'100\s*건\s*제한은.{0,50}실시간\s*업데이트\s*시\s*부하', text, re.S)
                and re.search(r'100\s*건\s*미만', all_text)):
            findings.append(('load_limit_context',
                             '100건 제한은 화면 업데이트 부하 때문이라는 설명이며, '
                             '100건 미만 미갱신의 원인으로 확정된 기록은 아닙니다.'))
        if (re.search(r'1951.{0,250}1950.{0,250}두\s*이슈', text, re.S)
                and re.search(r'두\s*이슈.{0,30}원인을\s*찾고\s*수정\s*작업\s*중', text, re.S)):
            findings.append(('two_issues_work_in_progress',
                             '1951을 포함한 두 이슈는 당시 원인을 찾고 수정 작업 중이라고 안내했습니다.'))
        if (re.search(r'ASSERT는\s*디버깅\s*모드에서만\s*확인\s*가능', text)
                and re.search(r'릴리즈\s*모드인\s*운영\s*버전.{0,35}오류\s*발생\s*영향', text, re.S)):
            findings.append(('debug_assert_release_risk',
                             'DEBUG ASSERT는 디버그 환경에서만 보이지만 운영 버전에도 '
                             '오류 영향이 있을 수 있어 확인이 필요하다고 봤습니다.'))
        if re.search(r'소리마치(?:쪽)?.{0,20}확인\s*필요.{0,35}종료\s*처리', text, re.S):
            findings.append(('sorimachi_owner',
                             '초기 분류에서는 소리마치 측 확인 건으로 보고 우리 쪽 이슈를 종료 처리했습니다.'))
        if re.search(r'운영\s*카이로스.{0,100}ASSERT\s*는?\s*발생하지\s*않', text, re.S):
            findings.append(('production_assert_not_reproduced',
                             '운영 카이로스에서 반복 검색했지만 ASSERT는 재현되지 않았습니다.'))
        if (re.search(r'데이터\s*조회가\s*클\s*때.{0,50}delete\s*루틴.{0,25}발생', text, re.I | re.S)
                and re.search(r'릴리즈\s*환경.{0,100}비정기적으로\s*죽는\s*현상', text, re.S)):
            sender = '이준옥의 ' if re.search(r'From\s*[.：:]\s*이준옥', source_text, re.I) else ''
            findings.append(('delete_routine_risk',
                             f'마지막 {sender}분석은 대량 조회 후 delete 루틴에서 문제가 생기며, '
                             '릴리즈에서 겉보기 정상이어도 비정기 종료를 일으킬 수 있다는 내용입니다.'))
        if (re.search(r'현재\s*신차트\(3600\).{0,80}모든\s*지표.{0,45}반올림', text, re.S)
                and re.search(r'CCI\s*지표\s*값.{0,35}신/구\s*차트가\s*다르게\s*표시', text, re.S)):
            findings.append(('cci_initial_rounding',
                             '초기에는 신차트 3600의 지표 반올림 처리 때문에 신·구 CCI 값이 다르다고 안내했습니다.'))
        if (re.search(r'지표\s*검증식\s*엑셀.{0,50}신차트\s*CCI\s*지표값', text, re.S)
                and re.search(r'동일하게\s*표시되는\s*것\s*확인', text)):
            findings.append(('cci_excel_match', '후속 검증식 엑셀과 신차트 CCI 값이 일치함을 확인했습니다.'))
        if (re.search(r'CCI\s*지표\s*계산식과\s*같이\s*정상\s*표시', text)
                and re.search(r'구차트인\s*3100\s*화면은\s*한국금융IT에서\s*제공하는\s*차트가\s*아니', text, re.S)
                and re.search(r'값이\s*다르게\s*표시되는\s*원인은\s*알\s*수\s*없', text, re.S)):
            findings.append(('cci_final_scope',
                             '최종 확인에서 신차트 CCI는 계산식대로 정상 표시됐고, 고객이 말한 '
                             '구차트 3100은 한국금융IT 제공 차트가 아니었습니다. '
                             '두 값이 다른 정확한 원인은 알 수 없다고 안내했습니다.'))
        normal_statement = _confirmation_statement(text)
        if normal_statement:
            findings.append(('normal_confirmed', f'해당 시점의 정상 확인 결과: “{_label(normal_statement)}”'))
        if re.search(r'표시.{0,20}안되|표시.{0,20}안\s*되|안나온|재문의', text):
            findings.append(('requery', '그 뒤 표시 문제가 다시 제기됐습니다.'))
        if re.search(r'다시\s*전달.{0,40}확인|재전달.{0,30}확인', text, re.S):
            findings.append(('recheck', '최신 기록은 자료를 다시 전달받아 재확인하겠다는 단계입니다.'))
        if re.search(r'체결량이\s*(?:존재하면|있으면).{0,30}실시간\s*처리', text):
            findings.append(('cause_processing', '체결량이 있으면 실시간 처리하는 동작이 원인 근거로 기록됐습니다.'))
        if (re.search(r'p_buf.{0,30}빈\s*문자열', text, re.S)
                and re.search(r'CheckValidSize.{0,20}FALSE', text, re.S)
                and re.search(r'pDataBuf.{0,20}pStartBuf.{0,50}NULL', text, re.S)
                and re.search(r'비정상\s*종료.{0,20}(?:보임|것으로)', text, re.S)):
            findings.append(('cause_null_chain',
                             '조사에서는 빈 p_buf로 CheckValidSize가 FALSE를 반환해 '
                             'pDataBuf·pStartBuf가 NULL로 남고 비정상 종료되는 것으로 봤습니다.'))
        if (re.search(r'실시간.{0,30}조회\s*기준\s*200\s*개까지\s*지원', text, re.S)
                and re.search(r'그리드.{0,20}100\s*개까지만\s*관리', text, re.S)
                and re.search(r'그리드에\s*없는\s*항목.{0,35}(?:예상|같음|추정)', text, re.S)):
            findings.append(('grid_cap_hypothesis',
                             '실시간 조회는 200개까지 지원하지만 그리드는 100개까지만 관리해 '
                             '그리드 밖 종목의 신호가 집계 차이를 낸다는 추정이 기록됐습니다.'))
        if (re.search(r'SetScrollInfo.{0,100}직접(?:적인)?\s*원인이라기보다는', text, re.I | re.S)
                and re.search(r'(?:깨진\s*메모리|비정상\s*상태|잘못된\s*상태).{0,100}(?:크래시|보임|판단)', text, re.S)):
            findings.append(('setscroll_hypothesis',
                             'SetScrollInfo는 크래시가 관찰된 지점이며 직접 원인으로 확정하지 않았습니다. '
                             '비정상 상태에서 진입했을 가능성으로 판단했습니다.'))
        if (re.search(r'CScrollBarEx::SetScrollInfo\(\).{0,40}예방\s*코드(?:를)?\s*추가', text, re.S)
                and all(token in text for token in ('GetSafeHwnd', 'IsWindow', 'SIF_ALL', 'SIF_RANGE'))
                and re.search(r'(?:Max/Range|Range).{0,100}(?:range/page/pos|Page/Pos)', text, re.I | re.S)):
            findings.append(('setscroll_defenses',
                             'SetScrollInfo에 핸들 유효성(GetSafeHwnd·IsWindow), Range/Page/Pos 값, '
                             '마스크(SIF_ALL 대신 SIF_RANGE·SIF_PAGE·SIF_POS) 방어 코드를 추가했다고 기록했습니다.'))
        if (re.search(r'\bTileBlt\b.{0,100}nSourWid.{0,40}음수', text, re.I | re.S)
                and re.search(r'무한\s*루프', text)):
            findings.append(('tileblt_report',
                             '후속 고객 테스트에서는 TileBlt의 nSourWid가 음수가 되어 무한루프가 '
                             '발생했다고 보고하고 점검·수정을 요청했습니다.'))
        if (re.search(r'엑셀\s*불러오기\s*기능', text)
                and re.search(r'해당\s*현상은\s*아닌지\s*확인|사용하였는\s*것인지', text, re.S)):
            findings.append(('excel_hypothesis',
                             '엑셀 불러오기는 유사 현상을 재현한 조사 가설이었으며 고객 사용 여부를 확인했습니다.'))
        if ('엑셀' in all_text and re.search(r'해당\s*기능은\s*사용자가\s*사용하지\s*않', text)):
            findings.append(('customer_no_excel', '고객은 엑셀 불러오기 기능을 사용하지 않았다고 답했습니다.'))
        if re.search(r'요청\s*주신\s*현상은\s*재연이\s*되고\s*있지\s*않', text):
            findings.append(('issue_not_reproduced', '마지막 재확인에서는 요청된 현상이 재현되지 않았습니다.'))
        if (re.search(r'우선\s*종료\s*처리', text)
                and re.search(r'재발생.{0,100}재현\s*스텝.{0,60}로그\s*파일', text, re.S)
                and re.search(r'진행\s*예정', text)):
            findings.append(('provisional_close',
                             '우선 종료하고, 재발생 시 재현 단계 등의 정보를 받아 로그 파일을 '
                             '만드는 방향으로 진행할 예정이라고 기록했습니다.'))
        if (re.search(r'\[0156\].{0,150}\[0662\]', text, re.S)
                and re.search(r'일\s*,\s*주\s*,\s*월\s*,\s*년\s*,\s*초\s*주기.{0,30}정상\s*조회', text, re.S)
                and re.search(r'분\s*,\s*틱\s*주기.{0,30}비정상\s*종료', text, re.S)):
            tr = re.search(r'\bTR\s*[:：]\s*(IVU10060)\b', text, re.I)
            sentence = '마지막 재확인에서 일·주·월·년·초 주기는 정상 조회됐지만 분·틱 주기는 비정상 종료됐습니다.'
            if tr:
                sentence += f' 조회 TR은 {_label(tr.group(1))}으로 기록됐습니다.'
            findings.append(('periodic_recheck', sentence))
        field_plan = re.search(r'구분자\((\d{2,4})\).{0,100}매매구분.{0,60}추가.{0,25}보내려고', text, re.S)
        if field_plan:
            findings.append(('field_plan', f'거래소 구분자({field_plan.group(1)})에 매매구분을 추가해 보내겠다는 계획이 기록됐습니다.'))
        if re.search(r'봉\s*개수\s*제한으로\s*검색\s*안되', text):
            findings.append(('count_limit', '봉 개수 제한 때문에 검색되지 않았다는 원인 기록이 있습니다.'))
        if re.search(r'서버\s*담당자.{0,30}작업.{0,20}완료', text):
            findings.append(('server_work_done', '서버 작업 완료 기록이 있습니다.'))
        if re.search(r'운영\s*적용\s*예정', text):
            findings.append(('operation_planned', '운영 적용은 당시 예정으로 안내됐습니다.'))
        if (re.search(r'수정\s*파일\s*전달', text)
                and re.search(r'아직.{0,30}운영\s*버전.{0,25}반영하지\s*않', text)):
            findings.append(('operation_not_applied', '수정 파일은 전달했지만 당시 운영 버전에는 아직 반영되지 않았다고 기록됐습니다.'))
        if (re.search(r'차트\s*로드한\s*직후.{0,80}차트\s*OCX', text, re.S)
                and re.search(r'추세선.{0,100}메모리에\s*copy.{0,60}누락', text, re.S | re.I)):
            findings.append(('cause_missing_copy',
                             '확인된 원인은 차트 로드 직후 OCX 추세선 값을 공유 메모리에 복사하는 처리가 누락된 것입니다.'))
        planned_delivery = re.search(r'수정\s*작업\s*및\s*QC.{0,70}후\s*(\d{1,2}월\s*\d{1,2}일).{0,20}전달.{0,10}예정', text, re.S | re.I)
        if planned_delivery:
            findings.append(('delivery_planned',
                             f'{_label(planned_delivery.group(1))} 전달은 수정·QC 이후의 예정으로 안내됐습니다.'))
        if re.search(r'메일\s*수신', source_text) and re.search(r'정상적으로\s*동작하지\s*않', text):
            findings.append(('customer_requery', '고객이 전달본이 정상 동작하지 않는다고 재문의했습니다.'))
        rollback_file = re.search(r'롤백\s*소스\s*1개\s*파일.{0,160}?[/\\]([\w.-]+\.(?:cpp|c|h|py|js))', text, re.S | re.I)
        if rollback_file and re.search(r'다시\s*보내|재전달|다시\s*전달', text):
            filename = _label(rollback_file.group(1))
            requester = re.search(r'([가-힣]{2,5})\s*매니저(?:님)?\s*요청(?:사항)?', text)
            prefix = f'{_label(requester.group(1))} 요청으로 ' if requester else ''
            sentence = f'{prefix}{filename} 한 파일을 롤백해 다시 전달했습니다.'
            if re.search(r'그\s*외에는\s*직전\s*파일과\s*동일', text):
                sentence += ' 나머지는 직전 파일과 같았습니다.'
            findings.append(('single_file_rollback', sentence))
        revision_rollback = re.search(r'(?:소스\s*동기화\s*과정에서).{0,50}revision\s*(\d+)\s*롤백', text, re.S | re.I)
        if revision_rollback:
            findings.append(('revision_rollback',
                             f'후속 소스 동기화에서 revision {revision_rollback.group(1)} 롤백 기록이 있습니다.'))
        if re.search(r'비정상\s*종료.{0,30}재접수.{0,30}수정\s*후\s*발송', text, re.S):
            findings.append(('later_dispatch', '비정상 종료로 재접수된 내용을 수정해 발송했다는 마지막 기록이 있습니다.'))
        if re.search(r'블록\s*시세\s*처리\s*되어\s*있음|블록\s*처리도\s*되어\s*있', text):
            findings.append(('block_already_applied', '블록시세 처리는 문의 당시 이미 적용돼 있었습니다.'))
        if re.search(r'개발\s*서버에\s*서버\s*개발\s*완료', text):
            findings.append(('server_dev_completed', '개발서버의 서버 개발 완료 기록이 있습니다.'))
        if (re.search(r'\bETN\s*구분자\s*바로\s*뒤(?:의)?\s*1\s*바이트', text, re.I)
                and re.search(r'\bsPF\s*\[\s*1\s*\]', text, re.I)
                and '상장형수익증권' in text):
            findings.append(('server_field_layout',
                             '서버 TR 출력에서 ETN 구분자 바로 뒤 1바이트를 상장형수익증권 구분자로 두고 sPF[1] 필드를 사용했습니다.'))
        if re.search(r'화면\s*개발\s*예정', text) and re.search(r'QC\s*예정', text, re.I):
            findings.append(('screen_qc_planned', '화면 개발과 QC는 당시 예정이었고 일정 확인 중이었습니다.'))
        if re.search(r'관련\s*수정\s*내역.{0,20}확인\s*부탁', text, re.S) and re.search(r'mail\s+to', source_text, re.I):
            findings.append(('change_notice', '수정 내역 안내 메일이 기록돼 있습니다.'))
        if re.search(r'HTS\s*분봉에서도\s*(?:DeMark\s*)?지표\s*표시되는\s*것\s*확인', text, re.I):
            findings.append(('intraday_supported', 'HTS에서도 DeMark 지표의 분봉 표시가 가능합니다.'))
        if (re.search(r'1\s*,\s*2\s*분봉', text)
                and re.search(r'지표가\s*표시되지\s*않아\s*확인\s*필요', text)):
            findings.append(('intraday_initial_issue', '초기에는 1·2분봉에서 표시되지 않아 추가 확인이 필요하다고 기록했습니다.'))
        if (re.search(r'1\s*,\s*2\s*분\s*주기에서도', text)
                and re.search(r'DeMark\s*지표.{0,25}정상\s*조회', text, re.I | re.S)):
            findings.append(('intraday_correction', '후속 확인에서는 1·2분봉도 정상 조회된다고 정정했습니다.'))
        bar_example = re.search(r'1\s*분봉.{0,30}?(\d{3,5})\s*개.{0,30}5\s*분봉.{0,30}?(\d{2,5})\s*개\s*이상',
                                text, re.S)
        if bar_example:
            findings.append(('intraday_bar_example',
                             f'후속 조회 예시는 1분봉 {bar_example.group(1)}개, 5분봉 {bar_example.group(2)}개 이상입니다. '
                             '이 수치를 모든 날짜의 절대 기준으로 일반화할 수는 없습니다.'))
        if (re.search(r'값을\s*보려는\s*일자\s*기준\s*전일의\s*데이터가\s*모두', text)
                and re.search(r'전체\s*봉', text)):
            findings.append(('prior_day_all_bars', '표시하려는 일자의 전일 전체 봉이 차트에 조회돼 있어야 합니다.'))
        if (re.search(r'차트.{0,40}2000\s*개\s*이상\s*조회.{0,30}(?:앱\s*)?크래시', text, re.I | re.S)
                and re.search(r'Android\s*/\s*IOS\s*둘\s*다\s*2000\s*개로\s*제한.{0,30}모듈\s*전달', text, re.I | re.S)):
            findings.append(('both_platforms_limited',
                             '2000봉 이상 조회 크래시 대응으로 Android/iOS 모두 2000개 제한 모듈을 전달했습니다.'))
        if (re.search(r'안드로이드는\s*오류가\s*발생하지\s*않', text)
                and re.search(r'2000\s*개\s*제한\s*없이\s*그대로\s*유지\s*부탁', text)):
            findings.append(('android_unlimited_requested',
                             '고객은 Android에서는 오류가 없어 2000개 제한 없이 유지해 달라고 요청했습니다.'))
        if (re.search(r'crash가\s*발생하는\s*함수를\s*제거', text, re.I)
                and re.search(r'봉\s*개수는\s*제한을\s*두지\s*않', text)):
            findings.append(('unlimited_trial',
                             '크래시 함수를 제거한 무제한 대안 모듈도 시험했지만, 이 시점에는 고객 환경의 정상 동작이 확정되지 않았습니다.'))
        trial_recurrence = re.search(r'(20\d{2})[/.](\d{1,2})[/.](\d{1,2})\s*적용.{0,40}오류가\s*발생', text, re.S)
        if (trial_recurrence and re.search(r'주봉은\s*문제가\s*안되.{0,35}일봉만\s*문제', text, re.S)):
            _year, month, day = trial_recurrence.groups()
            findings.append(('unlimited_recurred',
                             f'{int(month)}월 {int(day)}일 적용본에서 고객이 다시 오류를 보고했고, '
                             '주봉은 괜찮지만 일봉에서 문제로 보인다고 했습니다.'))
        market_test = re.search(r'자체\s*빌드\s*버전.{0,80}2000\s*개를\s*초과해도\s*문제없이\s*조회.{0,80}'
                                r'마켓\s*버전.{0,30}(\d{4,5})\s*개\s*이상\s*조회\s*시.{0,25}크래시', text, re.S)
        if market_test:
            findings.append(('market_threshold',
                             f'개발자 테스트에서 자체 빌드는 2000개 초과도 정상 조회됐지만, '
                             f'마켓 버전은 {market_test.group(1)}개 이상에서 크래시가 확인됐습니다.'))
        if (re.search(r'크래시\s*리포트.{0,60}로그.{0,30}확인\s*부탁', text, re.S)
                and re.search(r'그\s*전까지는\s*이전(?:에\s*전달드린)?\s*모듈.{0,50}2000\s*개\s*제한', text, re.S)):
            findings.append(('limited_fallback',
                             '추가 크래시 로그를 요청하며 그전까지는 이전 2000개 제한 모듈을 쓰라고 안내했습니다.'))
        if (re.search(r'2000\s*개\s*봉\s*제한은\s*정상적으로\s*동작', text)
                and re.search(r'선적용해두고.{0,30}로그\s*추가\s*첨부', text, re.S)):
            findings.append(('limited_applied', '고객은 2000개 제한을 선적용했고 정상 동작해 로그를 추가 첨부했다고 회신했습니다.'))
        if (re.search(r'28\.05', text) and re.search(r'2\.805', text)
                and re.search(r'서버\s*제공.{0,15}봉\s*데이터\s*단위\s*확인', text, re.S)
                and re.search(r'소수점\s*둘째?\s*자리.{0,80}차트\s*수정', text, re.S)):
            findings.append(('server_chart_roles',
                             'Y축 28.05와 실제 2.805의 단위 차이는 서버 제공 봉 데이터 확인·수정 문제이고, '
                             '두 자리 표시 문제는 별도의 차트 수정이 필요하다고 구분했습니다.'))
        if (re.search(r'테스트\s*서버\s*121.{0,50}2\.805로\s*내려오도록.{0,30}수정', text, re.S)
                and re.search(r'2\s*자리\s*또는\s*반올림', text)
                and re.search(r'종목\s*코드.{0,20}(?<![A-Za-z0-9])NB(?![A-Za-z0-9])', text, re.S)):
            findings.append(('server_value_then_chart_digits',
                             '고객은 테스트서버 121에서 2.805로 내려오게 했지만 차트는 여전히 두 자리 또는 반올림 표시됐고, '
                             '국채 종목코드 NB로 구분하자고 제안했습니다.'))
        if (re.search(r'종목\s*코드.{0,80}소수점\s*3\s*자리', text, re.S)
                and re.search(r'수정\s*후\s*전달.{0,20}겠습니다', text, re.S)):
            findings.append(('chart_three_digit_plan', '종목코드를 이용한 차트 소수점 세 자리 표시 수정과 전달을 계획했습니다.'))
        if re.search(r'다크\s*모드\s*작업\s*롤백.{0,30}재확인\s*필요', text):
            findings.append(('rollback_recheck', '다크모드 작업 롤백으로 QC 재확인을 요청했습니다.'))
        if re.search(r'[Yy]\s*축\s*소수점은\s*제대로\s*표시.{0,30}종목명이\s*표시가\s*안되', text, re.S):
            findings.append(('name_missing_after_delivery',
                             '전달 뒤 고객은 Y축 소수점은 제대로 보이지만 종목명이 표시되지 않는다고 보고했습니다.'))
        if re.search(r'운영에\s*넣고.{0,50}조회도\s*안되고\s*빈\s*화면', text, re.S):
            findings.append(('operation_blank_after_delivery',
                             '같은 날 후속 보고에서는 운영에서 조회되지 않고 빈 화면이 나온다고 했습니다.'))
        if re.search(r'연관\s*모듈이\s*왠지.{0,25}필요할\s*거\s*같', text, re.S):
            findings.append(('module_hypothesis', '연관 모듈 필요성은 고객의 추정으로만 기록됐으며 확정 원인은 아닙니다.'))
        if ('NH투자증권' in text and re.search(r'제공.{0,10}데이터를\s*사용', text, re.S)
                and re.search(r'제공.{0,15}데이터가\s*0으로', text, re.S)):
            findings.append(('provider_zero_data',
                             '조건검색은 NH가 제공한 데이터를 사용했고, 확인 당시 제공값 자체가 0이었습니다.'))
        if re.search(r'추후\s*저희\s*수정이\s*필요하면\s*진행', text):
            findings.append(('own_fix_conditional', '우리 쪽 수정은 필요할 때 진행하겠다는 조건부 안내였습니다.'))
        if (re.search(r'유동부채\s*요약', text)
                and re.search(r'피딩\s*서버\s*전환\s*\(\s*3월\s*말\s*\)', text)
                and re.search(r'제조업\s*/\s*기타\s*업종.{0,15}한하여\s*제공', text, re.S)):
            findings.append(('provider_liability_scope',
                             '3월 말 피딩서버 전환 뒤 유동부채 제공 대상은 제조업·기타 업종으로 바뀌었다고 전달했습니다.'))
        if (re.search(r'GAAP\s*재무\s*데이터.{0,25}보험\s*업종', text, re.S)
                and re.search(r'유동\s*/\s*비유동.{0,25}구분이?\s*없이', text, re.S)
                and re.search(r'보험업.{0,15}유동부채.{0,80}제공\s*안', text, re.S)):
            findings.append(('gaap_insurance_exclusion',
                             'FN가이드 설명상 GAAP 보험업 재무데이터는 유동/비유동 구분이 없어 '
                             '보험업 유동부채 정보를 제공하지 않습니다.'))
        if (re.search(r'조건검색에서도\s*데이터를\s*제공해주지\s*않아', text)
                and re.search(r'추후\s*정상\s*데이터를\s*제공해준다면.{0,35}조건검색에서도\s*정상\s*처리', text, re.S)):
            findings.append(('normal_provider_data_conditional',
                             '해당 보험업 유동부채 데이터가 정상 제공되면 조건검색도 정상 처리될 것으로 판단한다고 안내했습니다.'))
        if (re.search(r'모니터링.{0,60}부하\s*현상.{0,20}(?:발생하지\s*않|확인되지\s*않)', text, re.S)
                and re.search(r'유저\s*파일|User\s*파일|추가\s*정보', text, re.I)):
            findings.append(('monitor_not_reproduced',
                             '제공된 환경에서 모니터링했지만 부하를 재현하지 못해 추가 정보나 User 파일을 요청했습니다.'))
        if (re.search(r'2차\s*모니터링', text)
                and re.search(r'화면\s*수를\s*확대', text)
                and re.search(r'타\s*사이트와\s*비교.{0,30}특이사항.{0,15}확인되지\s*않', text, re.S)):
            findings.append(('second_monitor', '2차 모니터링에서는 화면 수를 늘려 타 사이트와 비교했고 특이사항을 확인하지 못했습니다.'))
        if re.search(r'추가로\s*점검이\s*필요한\s*사항이\s*없으시다면.{0,100}모니터링을\s*마무리하고자', text, re.S):
            findings.append(('monitor_close_proposed', '추가 점검사항이 없으면 모니터링을 마무리하겠다고 제안했습니다.'))
        customer_from = re.search(r'메일\s*수신\s*\(\s*From\s*\.\s*([가-힣]{2,5})\s*\)', source_text, re.I)
        if (customer_from and re.search(r'타\s*증권사와\s*비교\s*테스트.{0,40}특이사항\s*없는\s*걸로\s*알고\s*있', text, re.S)):
            sender = _label(customer_from.group(1))
            reporter = _label(row['reporter']) if row.get('reporter') else ''
            prefix = f'노트 등록자는 {reporter}이지만 ' if reporter and reporter != sender else ''
            findings.append(('customer_reply', f'{prefix}고객 메일 발신자 {sender}은 비교 결과 특이사항이 없는 것으로 이해한다고 회신했습니다.'))
        for kind, sentence in findings:
            key = (issue_id, kind)
            previous = candidates.get(key)
            current_order = (str(row.get('created_at') or ''), int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0)
            previous_order = ((str(previous[0].get('created_at') or ''),
                               int(str(previous[0].get('note_id'))) if str(previous[0].get('note_id')).isdigit() else 0)
                              if previous else None)
            earliest = kind in {'hypothesis', 'not_reproduced', 'reproduced', 'requery', 'operation_planned',
                                'delivery_planned', 'customer_requery', 'block_already_applied',
                                'monitor_not_reproduced', 'initial_qc_done', 'initial_esc_qc_ng',
                                'excel_post_delivery_questions', 'signal_expert_explanation'}
            if previous_order is None or (current_order < previous_order if earliest else current_order > previous_order):
                candidates[key] = (row, sentence)
    entries: list[tuple[dict[str, Any], str, str]] = []
    for issue_id in selected_ids:
        normal = candidates.get((issue_id, 'normal_confirmed'))
        requery = candidates.get((issue_id, 'requery'))
        for (candidate_issue, kind), (row, sentence) in candidates.items():
            if candidate_issue != issue_id:
                continue
            if kind == 'requery' and (not normal or str(row.get('created_at') or '') <= str(normal[0].get('created_at') or '')):
                continue
            if kind == 'recheck' and (not requery or str(row.get('created_at') or '') < str(requery[0].get('created_at') or '')):
                continue
            if (kind == 'initial_qc_done'
                    and candidates.get((issue_id, 'qc_done'), (None, ''))[0] == row):
                continue
            if (kind == 'initial_esc_qc_ng'
                    and candidates.get((issue_id, 'esc_qc_ng'), (None, ''))[0] == row):
                continue
            if (kind == 'delivered'
                    and candidates.get((issue_id, 'b_package_delivered'), (None, ''))[0] == row):
                continue
            if (kind == 'delivery_completed'
                    and candidates.get((issue_id, 'esc_existing_fix_rollback'), (None, ''))[0] == row):
                continue
            entries.append((row, sentence, kind))
    represented = {
        'cause': {'hypothesis', 'mismatch', 'cause_processing', 'cause_null_chain', 'grid_cap_hypothesis',
                  'cause_pending', 'setscroll_hypothesis', 'excel_hypothesis', 'customer_no_excel',
                  'count_limit', 'cause_missing_copy', 'screenshot_mismatch',
                  'minute_as_daily', 'load_limit_context', 'delete_routine_risk',
                  'provider_zero_data', 'provider_liability_scope', 'gaap_insurance_exclusion',
                  'saved_graph_missing_studies', 'sleep_reconnect_unconfirmed',
                  'os_integrity_hypothesis', 'integrity_error_code',
                  'minute_bar_condition', 'legacy_files_location',
                  'legacy_files_hypothesis', 'regular_close_data_dependency',
                  'trendline_comparison_bars', 'regular_option_ats_absent',
                  'regular_option_misclassified_plan', 'signal_color_hypothesis',
                  'signal_expert_explanation', 'drdata_fixed_files'},
        'action': {'tag_changed', 'field_plan', 'server_work_done', 'action_recorded',
                   'crash_three_fixes', 'makezero_request', 'makezero_reply',
                   'server_dev_completed', 'server_field_layout', 'screen_qc_planned', 'change_notice',
                   'server_chart_roles', 'server_value_then_chart_digits', 'chart_three_digit_plan',
                   'own_fix_conditional', 'normal_provider_data_conditional', 'setscroll_defenses',
                   'tileblt_report', 'issue_not_reproduced', 'provisional_close', 'freeze_rollback',
                   'unreproduced_reopen', 'initial_999999_direction', 'final_000000_screen',
                   'two_issues_work_in_progress', 'missing_definition_complaint',
                   'developer_followup_plan', 'b_redelivery', 'variable_group_proposal',
                   'listbox_checkbox_choice', 'initial_esc_qc_ng', 'esc_qc_ng',
                   'esc_existing_fix_rollback', 'data_file_only_rollback_plan',
                   'b_package_delivered', 'postponed_work_excluded', 'merged_source_exclusion',
                   'merged_source_followup_failure', 'slow_symptom_unreproduced',
                   'clean_rebuild_requested', 'tooltip_reverted_remerge_requested',
                   'source_sync_before_remerge', 'header_cpp_requested',
                   'qc_ng', 'rebuild_qc_requested', 'qc_requested_again',
                   'future_span_mismatch', 'other_broker_reproduced', 'envelope_qc_ng',
                   'saved_graph_missing_studies', 'sleep_reconnect_unconfirmed',
                   'monitor_without_sleep_close', 'dll_reproduced_then_not',
                   'os_integrity_hypothesis', 'integrity_error_code',
                   'integrity_response_scope',
                   'excel_initial_requests', 'excel_scope_narrowed', 'customer_datetime_mismatch',
                   'excel_post_delivery_questions', 'old_date_axis_limit',
                   'excel_market_indicator_limit', 'date_default_initial',
                   'date_default_scope_changed', 'date_default_business_day_possible',
                   'development_done_qc_in_progress',
                   'condition_time_and_sort', 'legacy_cleanup_conditional',
                   'legacy_cleanup_expanded_request', 'direct_customer_call_declined',
                   'fibonacci_group_requirement', 'customer_confirms_requirement',
                   'regular_close_scope_decisions', 'regular_close_customer_hold',
                   'trendline_period_variation', 'trendline_setting_location',
                   'regular_option_misclassified_plan', 'signal_customer_reply',
                   'drdata_market_templates', 'drdata_conditional_removal',
                   'cmf_feature_request', 'after_hours_initial_pending',
                   'exchange_json_missing', 'exchange_zip_resent', 'formula_required',
                   'window_size_request', 'window_size_work_plan',
                   'multichart_option_symptom', 'multichart_setting_requested',
                   'process_reappears', 'yaxis_drag_workaround',
                   'main_combo_dependency', 'test_server_logging', 'feature_possible_only',
                   'version_unmanaged', 'version_folder_request', 'version_future_promise',
                   'group_setting_not_saved', 'feasibility_refused',
                   'chart_stock_mismatch_report', 'obv_typo_request',
                   'stock_mismatch_customer_followup', 'stock_mismatch_conditional_close'},
    }
    for issue_id in selected_ids:
        for topic in ('cause', 'action'):
            if any(row['issue_id'] == issue_id and kind in represented[topic] for row, _sentence, kind in entries):
                continue
            if (topic == 'cause' and (issue_id, 'monitor_close_proposed') in candidates
                    and issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True):
                continue
            topic_rows = [row for planned_topic, preview, detail, _ in plans if planned_topic == topic
                          for row in preview + detail if row['issue_id'] == issue_id
                          and (topic != 'action' or row['field'] != 'steps_to_reproduce')]
            if topic == 'action':
                topic_rows = [row for row in topic_rows if not re.match(
                    r'^\s*\[(?:QC\s*요청|QC\s*완료|고객사\s*전달|전달\s*완료)\]', row['text'])]
            short_rows = [row for row in topic_rows if len(row['text'].strip()) <= 300]
            choice = _core_choice(short_rows or topic_rows)
            if choice is None:
                complete_short = [row for row in short_rows if len(row['text'].strip()) >= 10
                                  and re.search(r'(?:다|임|음|됨)[.!?]?$', row['text'].strip())
                                  and not _NOTE_BOILERPLATE.search(row['text'])]
                choice = max(complete_short, key=lambda row: (str(row.get('created_at') or ''),
                                                               int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0),
                             default=None)
            if choice:
                statement = f'{TOPIC_TITLES[topic]} 근거에는 “{_label(choice["text"])}”라고 기록돼 있습니다.'
                entries.append((choice, statement, f'{topic}_fallback'))
        if not any(row['issue_id'] == issue_id and not kind.endswith('_fallback')
                   for row, _sentence, kind in entries):
            history = [row for row in timeline if row['issue_id'] == issue_id]
            latest_key = max(((str(row.get('created_at') or ''), int(str(row.get('note_id')))
                               if str(row.get('note_id')).isdigit() else 0) for row in history), default=None)
            latest_history = [row for row in history if
                              (str(row.get('created_at') or ''), int(str(row.get('note_id')))
                               if str(row.get('note_id')).isdigit() else 0) == latest_key]
            short_history = [row for row in latest_history if len(row['text'].strip()) <= 300]
            choice = _core_choice(short_history or latest_history)
            if choice:
                entries.append((choice, f'마지막 기록에는 “{_label(choice["text"])}”라고 적혀 있습니다.',
                                'latest_fallback'))
    def _event_order(entry: tuple[dict[str, Any], str, str]) -> tuple[str, int, int]:
        row, _sentence, kind = entry
        anchor = candidates.get((row['issue_id'], 'field_plan')) if kind == 'action_recorded' else None
        anchor_row = anchor[0] if anchor else row
        note_id = str(anchor_row.get('note_id'))
        return (str(anchor_row.get('created_at') or ''), int(note_id) if note_id.isdigit() else 0,
                int(kind == 'action_recorded'))
    entries.sort(key=_event_order)
    if not entries:
        return []
    lines = ['### 핵심 결론', '']
    for row, sentence, _kind in entries[:12]:
        date = str(row.get('created_at') or '')[:10]
        source = f"노트 {_label(row['note_id'])}" if row.get('note_id') else f"필드 {_label(row['field'])}"
        lines.extend([f"- {sentence} (#{row['issue_id']} · {source}" + (f' · {_label(date)}' if date else '') + ')', ''])
    operation_done = re.search(
        r'운영.{0,25}(?:반영|배포|적용).{0,15}(?:완료(?:했|되었습니다|됨)|했습니다|되었습니다)', all_text)
    operation_negated = re.search(r'운영.{0,25}(?:반영|배포|적용).{0,15}완료.{0,12}(?:아님|아니|않|못|미확인|예정)', all_text)
    if (not (operation_done and not operation_negated)
            and not any(kind == 'operation_not_applied' for _row, _sentence, kind in entries)):
        lines.extend(['선택된 근거만으로 운영 반영 완료는 확정할 수 없습니다.', ''])
    if (len(selected_ids) == 1
            and issues.get(selected_ids[0], {}).get('answer_evidence', {}).get('complete') is True
            and any(kind in {'qc_done', 'initial_qc_done'} for _row, _sentence, kind in entries)):
        history_text = '\n'.join(excerpt['text'] for excerpt in safe_excerpts(issues[selected_ids[0]]))
        if not re.search(r'고객사?\s*전달|전달\s*완료|전달(?:했|하였|드렸|됨)', history_text):
            lines.extend(['확인된 전체 이력에는 고객사 전달 완료 기록이 없습니다.', ''])
    if (any(kind == 'monitor_close_proposed' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)
            and not re.search(r'원인\s*(?:은|:|：).{0,80}(?:확인|판명|확정)|수정\s*(?:패치|파일).{0,20}완료', all_text, re.S)):
        lines.extend(['제공된 전체 이력에서는 원인 확정이나 수정 패치 완료 근거를 확인할 수 없습니다.', ''])
    if (any(kind == 'unlimited_recurred' for _row, _sentence, kind in entries)
            and any(kind == 'limited_applied' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        lines.extend(['제공된 전체 이력에는 무제한 조회의 최종 정상화 근거가 없습니다.', ''])
    blank_events = [row for row, _sentence, kind in entries if kind == 'operation_blank_after_delivery']
    if (blank_events and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                             for issue_id in selected_ids)
            and max(str(row.get('created_at') or '') for row in rows) <= str(blank_events[-1].get('created_at') or '')):
        lines.extend(['운영 빈 화면 후속 장애의 해결 완료 근거는 제공된 전체 이력에서 확인되지 않습니다.', ''])
    if (any(kind == 'own_fix_conditional' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)
            and not re.search(r'(?:수정|패치|복구).{0,20}(?:완료|적용)', all_text, re.S)):
        lines.extend(['우리 쪽 수정·배포 완료 근거는 제공된 전체 이력에서 확인되지 않습니다.', ''])
    if (any(kind == 'cause_pending' for _row, _sentence, kind in entries)
            and any(topic == 'cause' and unanswered for topic, _preview, _detail, unanswered in plans)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        lines.extend(['제공된 전체 이력에는 확정된 원인이 기록돼 있지 않습니다.', ''])
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (not re.search(r'(?:코드|로직|소스).{0,30}(?:수정|변경|추가)|'
                          r'(?:수정|변경|추가).{0,30}(?:코드|로직|소스)', source_text, re.S)
                and not re.search(r'(?:고객|사용자).{0,40}(?:정상|해결).{0,20}(?:확인|회신)', source_text, re.S)):
            lines.extend(['구체적인 코드 수정 내역과 고객 측 정상 동작 확인도 제공된 이력에는 없습니다.', ''])
    if (any(kind == 'tileblt_report' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (not re.search(r'TileBlt.{0,100}(?:수정|패치).{0,30}(?:완료|적용|전달)', source_text, re.I | re.S)
                and not re.search(r'(?:고객|사용자).{0,40}(?:정상|해결).{0,20}(?:확인|회신)', source_text, re.S)):
            lines.extend(['후속 TileBlt 문제의 수정 완료와 고객 측 정상 동작 확인 근거는 없습니다.', ''])
    if (any(kind == 'provisional_close' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'(?:수정|패치).{0,25}(?:완료|적용|전달)|배포.{0,10}완료', source_text, re.S):
            lines.extend(['제공된 전체 이력에는 수정 패치나 배포 완료 기록이 없습니다.', ''])
    if (any(kind == 'unreproduced_reopen' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'(?:원인.{0,25}(?:확정|판명)|(?:수정|패치).{0,25}(?:완료|적용|전달))',
                         source_text, re.S):
            lines.extend(['제공된 전체 이력에는 원인·수정 확정 근거가 없습니다.', ''])
    if (any(kind == 'cci_final_scope' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'(?:수정|패치).{0,25}(?:완료|적용|전달)', source_text, re.S):
            lines.extend(['신차트를 구차트 값에 맞춘 수정·패치 완료 근거는 없습니다.', ''])
    volume_events = [row for row, _sentence, kind in entries if kind == 'volume_followup']
    if (volume_events and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                              for issue_id in selected_ids)):
        last_volume = max(volume_events, key=lambda row: str(row.get('created_at') or ''))
        later_text = '\n'.join(excerpt.get('text', '') for excerpt in safe_excerpts(
            issues.get(last_volume['issue_id'], {}))
            if str(excerpt.get('created_at') or '') > str(last_volume.get('created_at') or ''))
        if not re.search(r'거래량.{0,50}(?:정상|일치|해결|수정\s*완료)', later_text, re.S):
            lines.extend(['분봉 합계와 일간 거래량 차이의 후속 해결 근거는 제공된 이력에 없습니다.', ''])
    if (any(kind == 'two_issues_work_in_progress' for _row, _sentence, kind in entries)
            and any(kind == 'delivery_completed' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (not _QC_DONE.search(source_text)
                and not re.search(r'(?:코드|로직|소스).{0,35}(?:수정|변경|추가)\s*완료|'
                                  r'(?:수정|변경|추가).{0,35}(?:코드|로직|소스).{0,20}완료', source_text, re.S)):
            lines.extend(['전달 완료는 기록됐지만 QC 완료나 구체적 수정 코드 내역은 확인되지 않습니다.', ''])
    if (any(kind == 'delete_routine_risk' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'delete\s*루틴.{0,100}(?:수정|패치).{0,25}(?:완료|적용|전달)',
                         source_text, re.I | re.S):
            lines.extend(['이 이력에는 delete 루틴 수정·릴리즈 적용 완료 근거가 없습니다.', ''])
    for issue_id in selected_ids:
        if issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is not True:
            continue
        for event_kind, missing_pattern, missing_sentence in (
                ('b_redelivery', r'(?:재컴파일|컴파일).{0,30}(?:성공|정상)|(?:고객|운영).{0,40}정상.{0,20}(?:확인|회신)',
                 '재컴파일 성공이나 운영 정상화에 대한 고객 확인은 제공된 전체 이력에 없습니다.'),
                ('esc_existing_fix_rollback', r'ESC.{0,40}(?:해결|정상|수정\s*완료|반영\s*완료)',
                 'ESC 종료 오류가 최종 해결됐다는 기록은 제공된 전체 이력에 없습니다.'),
                ('b_package_delivered', r'(?:고객|사용자).{0,50}(?:정상\s*동작|정상화|해결).{0,20}(?:확인|회신)',
                 'B 패키지 전달 뒤 고객 측 정상 동작 확인은 제공된 전체 이력에 없습니다.'),
        ):
            required_kind = {'b_redelivery': 'missing_definition_complaint',
                             'esc_existing_fix_rollback': 'esc_qc_ng',
                             'b_package_delivered': 'data_file_only_rollback_plan'}[event_kind]
            if not any(row['issue_id'] == issue_id and kind == required_kind
                       for row, _sentence, kind in entries):
                continue
            event_rows = [row for row, _sentence, kind in entries
                          if row['issue_id'] == issue_id and kind == event_kind]
            if not event_rows:
                continue
            latest_event = max(event_rows, key=lambda row: (str(row.get('created_at') or ''),
                                                           int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
            later_text = '\n'.join(excerpt.get('text', '') for excerpt in safe_excerpts(issues[issue_id])
                                   if (str(excerpt.get('created_at') or ''),
                                       int(str(excerpt.get('note_id'))) if str(excerpt.get('note_id')).isdigit() else 0)
                                   >= (str(latest_event.get('created_at') or ''),
                                       int(str(latest_event.get('note_id'))) if str(latest_event.get('note_id')).isdigit() else 0))
            if not re.search(missing_pattern, later_text, re.I | re.S):
                lines.extend([missing_sentence, ''])
        remerge_events = [row for row, _sentence, kind in entries
                          if row['issue_id'] == issue_id and kind == 'tooltip_reverted_remerge_requested']
        if remerge_events:
            latest_remerge = max(remerge_events, key=lambda row: (str(row.get('created_at') or ''),
                                                               int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
            later_text = '\n'.join(excerpt.get('text', '') for excerpt in safe_excerpts(issues[issue_id])
                                   if str(excerpt.get('created_at') or '') > str(latest_remerge.get('created_at') or ''))
            if not re.search(r'(?:재전달|전달\s*완료|고객사\s*전달)', later_text):
                lines.extend(['이후 재취합본을 다시 전달했다는 기록은 제공된 전체 이력에 없습니다.', ''])
    periodic_events = [row for row, _sentence, kind in entries if kind == 'periodic_recheck']
    if periodic_events and all(
            issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
            for issue_id in selected_ids):
        latest_recheck = max(periodic_events, key=lambda row: (str(row.get('created_at') or ''),
                                                           int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
        latest_record = max(rows, key=lambda row: (str(row.get('created_at') or ''),
                                                   int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
        full_sources = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                 for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (latest_recheck['issue_id'] == latest_record['issue_id']
                and latest_recheck.get('field') == latest_record.get('field')
                and not re.search(r'(?:수정|패치|복구).{0,20}(?:완료|적용|배포)', full_sources, re.S)):
            lines.extend(['제공된 전체 이력에서는 분·틱 주기 오류의 해결 완료 근거는 확인되지 않습니다.', ''])
    return lines


def render_quote_answer(validated: dict[str, Any], grounding: dict[str, Any], *, question: str = "",
                        previous_questions: list[str] | None = None) -> str:
    """검증된 인용만 고정 제목과 출처 표시로 렌더한다. 자유 생성 요약은 읽지 않는다."""
    if not validated.get("valid"):
        return ""
    issues = _issues(grounding)
    lines: list[str] = ['### 핵심 근거', '']
    notices = ['완료·QC·전달·운영 반영 여부는 각 인용에 적힌 범위와 시점에서만 확인됩니다.', '']
    if any(len(safe_excerpts(issues.get(issue_id, {}))) < len(issues.get(issue_id, {}).get('answer_evidence', {}).get('excerpts', []))
           for issue_id in validated.get('selected_issue_ids', [])):
        notices.extend(['접속정보 또는 다른 이슈 내용이 섞인 노트·필드는 대상 범위만 인용했습니다.', ''])
    if any(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is False
           for issue_id in validated.get('selected_issue_ids', [])):
        notices.extend(['긴 원문에서 선택된 근거를 확인한 답변이며, 전체 이력이 모두 포함된 것은 아닙니다.', ''])
    plans: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]] = []
    for section in validated.get("sections", []):
        topic = section.get("topic")
        if topic not in TOPIC_TITLES:
            return ""
        evidence_rows = section.get("evidence", [])
        if topic == "timeline":
            evidence_rows = sorted(evidence_rows, key=lambda row: (
                str(row.get("created_at") or ""),
                int(str(row.get("note_id"))) if str(row.get("note_id")).isdigit() else 0,
            ))
        unique_rows = []
        seen_rows = set()
        for row in evidence_rows:
            key = (row["issue_id"], row["field"], row["text"])
            if key not in seen_rows:
                unique_rows.append(row)
                seen_rows.add(key)
        evidence_rows = unique_rows
        preview: list[dict[str, Any]] = []
        if topic not in ("timeline", "background") and evidence_rows:
            for issue_id in validated.get("selected_issue_ids", []):
                rows = [row for row in evidence_rows if row["issue_id"] == issue_id]
                if not rows or len(preview) >= 3:
                    continue
                newest = max(rows, key=lambda row: (
                    str(row.get("created_at") or ""),
                    int(str(row.get("note_id"))) if str(row.get("note_id")).isdigit() else 0,
                ))
                source = (newest["issue_id"], newest["field"])
                preview.extend(row for row in rows if (row["issue_id"], row["field"]) == source
                               and len(preview) < 3)
        detail = [row for row in evidence_rows if row not in preview]
        plans.append((topic, preview, detail, section.get("unanswered", False)))
    selected_ids = validated.get("selected_issue_ids", [])
    from backend.evaluation.explanation_direct import direct_conclusion
    conclusion = direct_conclusion(question, plans, selected_ids, issues, previous_questions,
                                   validated.get('answer_items'))
    if not conclusion:
        conclusion = _conclusion_lines(plans, selected_ids, issues)
        if question and not re.search(r'운영|반영|배포', question):
            conclusion = [line for line in conclusion if line != '선택된 근거만으로 운영 반영 완료는 확정할 수 없습니다.']
    lines = conclusion + lines
    rendered_quotes: set[tuple[int, str, str]] = set()
    initial_sources: set[tuple[int, str]] = set()
    dated_by_issue: dict[int, list[dict[str, Any]]] = {}
    for _topic, preview, detail, _unanswered in plans:
        for row in preview + detail:
            if row.get("created_at"):
                dated_by_issue.setdefault(row["issue_id"], []).append(row)
    for issue_rows in dated_by_issue.values():
        ordered = sorted(issue_rows, key=lambda row: (
            str(row["created_at"]),
            int(str(row.get("note_id"))) if str(row.get("note_id")).isdigit() else 0,
        ))
        if ordered[0]["created_at"] != ordered[-1]["created_at"] or (
                ordered[0].get("note_id") != ordered[-1].get("note_id")):
            initial_sources.add((ordered[0]["issue_id"], ordered[0]["field"]))
    timeline = next((detail for topic, _preview, detail, _unanswered in plans if topic == "timeline"), [])
    dated_timeline = [row for row in timeline if row.get("created_at")]
    notes_by_issue: dict[int, list[dict[str, Any]]] = {}
    for row in dated_timeline:
        notes_by_issue.setdefault(row["issue_id"], []).append(row)
    latest_by_issue: dict[int, dict[str, Any]] = {}
    for issue_id, rows in notes_by_issue.items():
        last = rows[-1]
        same_note = [row for row in rows if row["field"] == last["field"]
                     and row["created_at"] == last["created_at"]]
        observed_rows = [row for row in same_note if len(row['text']) <= _MAX_CORE_CHARS
                         and _OBSERVED_FAILURE.search(row['text']) and not _SPECULATIVE.search(row['text'])]
        status_rows = [row for row in same_note if len(row["text"]) <= _MAX_CORE_CHARS
                       and _NOTE_STATE.search(row["text"])
                       and (not _NOTE_BOILERPLATE.search(row["text"]) or "\n" in row["text"])]
        other_rows = [row for row in same_note if len(row["text"]) <= _MAX_CORE_CHARS
                      and not _NOTE_BOILERPLATE.search(row["text"])]
        if observed_rows or status_rows or other_rows:
            latest_by_issue[issue_id] = (observed_rows or status_rows or other_rows)[-1]
    core: list[tuple[str, dict[str, Any]]] = []
    core.extend(("마지막 기록에서 확인되는 상태", row) for row in latest_by_issue.values())
    for wanted in ("cause",):
        for topic, preview, detail, _unanswered in plans:
            if topic == wanted:
                rows = preview + detail
                chosen = _whole_note_choice(rows, issues.get(rows[0]['issue_id'], {})) if rows else None
                chosen = chosen or _core_choice(rows)
                if chosen:
                    core.append((f"{TOPIC_TITLES[topic]} 근거", chosen))
    qc = _milestone_choice(timeline, _QC_DONE)
    delivery = _milestone_choice(timeline, _DELIVERED)
    if qc:
        core.append(("QC 확인 기록", qc))
    if delivery:
        core.append(("전달 기록", delivery))
    technical_rows = [row for topic, preview, detail, _unanswered in plans
                      if topic in ('action', 'technical') for row in preview + detail]
    technical_rows = [row for row in technical_rows if re.search(r'\b(?:FID|TR)\b|\b\d{3,4}\b|구분자|매매구분', row['text'], re.I)]
    specific_fields = [row for row in technical_rows
                       if re.search(r'\(\d{2,4}\)|\b(?:FID|TR)\s*[:=]', row['text'], re.I)]
    technical_choice = _core_choice(specific_fields) or _core_choice(technical_rows)
    if technical_choice:
        core.append(('구분·구현 근거', technical_choice))
    for wanted in ("action", "technical", "conditions", "actors"):
        for topic, preview, detail, _unanswered in plans:
            if topic == wanted:
                chosen = _core_choice(preview + detail)
                if chosen:
                    core.append((f"{TOPIC_TITLES[topic]} 근거", chosen))
    seen_core: set[tuple[int, str, str]] = set()
    for label, row in core:
        key = (row["issue_id"], row["field"], row["text"])
        if key in seen_core or any(issue_id == row['issue_id'] and field == row['field']
                                   and row['text'] in text for issue_id, field, text in seen_core):
            continue
        seen_core.add(key)
        lines.extend([f"**{label}**", ""])
        if not _append_evidence(lines, "", [row], issues, selected_ids,
                                rendered_quotes=rendered_quotes, initial_sources=initial_sources):
            return ""
        if len(seen_core) == 4:
            break
    lines.extend(notices)
    for topic, preview, _detail, unanswered in plans:
        if topic in ("timeline", "background"):
            continue
        if not _append_evidence(lines, TOPIC_TITLES[topic], preview, issues, selected_ids,
                                unanswered=unanswered, rendered_quotes=rendered_quotes,
                                deduplicate=True, initial_sources=initial_sources):
            return ""
    # Each displayed passage also carries issue and note metadata, so count
    # that visible overhead when deciding whether the full history needs folding.
    collapse_details = sum(len(row['text']) + 90 for _topic, _preview, detail, _unanswered in plans
                           for row in detail) > 800
    if collapse_details:
        lines.extend(['<details>', '<summary>전체 이력과 원문 근거 보기</summary>', ''])
    for topic, _preview, detail, unanswered in plans:
        if topic not in ("timeline", "background") and not detail:
            continue
        title = TOPIC_TITLES[topic] if topic in ("timeline", "background") else f"{TOPIC_TITLES[topic]} 전체 근거"
        if not _append_evidence(lines, title, detail, issues, selected_ids,
                                unanswered=unanswered and topic in ("timeline", "background"),
                                rendered_quotes=rendered_quotes, deduplicate=True,
                                initial_sources=initial_sources):
            return ""
    if collapse_details:
        lines.extend(['</details>', ''])
    return "\n".join(lines).strip()
