"""비교·문서가 공유하는 원인·조치·미확정 근거 규칙. 조회·모델 호출 없음."""
import re
from typing import Any, Dict, List, Tuple
from backend.display import status_name
from backend.utils.text import compact_text
from backend.reports.comparison_passages import focused_excerpt

def render_facts(rows, max_chars=220):
    if not rows:
        return '', ''
    refs = [f" [노트 {r['field'].split(':', 1)[1]}]" if r['field'].startswith('note:') else
            ' (제목)' if r['field'] == 'summary' else ' (본문)' for r in rows]
    remaining = max_chars - sum(map(len, refs)) - 2 * (len(rows) - 1)
    parts = []
    for index, row in enumerate(rows):
        size = min(len(row['text']), max(8, remaining // (len(rows) - index)))
        text = row['text'] if len(row['text']) <= size else row['text'][:max(0, size - 3)].rstrip() + '...'
        parts.append(text + refs[index]); remaining -= len(text)
    return '. '.join(parts), 'note' if any(r['field'].startswith('note:') for r in rows) else rows[0]['field']

NOTE_SNIPPET_CHARS = 220     # 사례별 조치 요약 길이


COMPARE_CELL_CHARS = 220      # 원인·해결 비교표 한 칸 길이


CAUSE_FIELD = "사유 및 원인"


FIX_FIELD = "조치 및 개선"


_BODY_DIAGNOSIS = re.compile(
    r"(?:참조|호출|할당|해제|갱신|초기화).{0,70}(?:되어|해서|하여).{0,25}(?:나타나|발생).{0,25}(?:파악|확인)", re.I
)


_CAUSE_SENTENCE = re.compile(
    _BODY_DIAGNOSIS.pattern + r"|"
    r"(?:증가|발생).{0,12}(?:함에|에)\s*따라.{0,50}(?:자원|CPU|부하|메모리)|원인|때문|(?:으)?로\s*인해|(?:문제|장애|오류|누락|버그)(?:로|가\s*있어)|"
    r"실제로는.{0,80}(?:구현|설정|되어\s*있)|(?:되어야|돼야).*(?:되고|되어\s*있)|"
    r"(?:구조상|기술적인?\s*한계)|(?:데이터|이력).*(?:저장|보관).*(?:않|안\s*하|당일)|"
    r"(?:loading|parsing|compiling).*(?:매번|반복)|(?:오류|버그)\s*발생(?:하여|해서)", re.I
)


_SVN_FILE = re.compile(r"^[AMUD]\s+(\S+\.[A-Za-z0-9]+)\s*$", re.M)


_NOTE_METADATA = re.compile(
    r"^(?:svn\s|리비전\s*[:：]|작성자\s*[:：]|날짜\s*[:：]|메시지\s*[:：]|"
    r"[a-z][a-z0-9+.-]*://|[A-Za-z]:\\)", re.I
)


_QC_SUCCESS = re.compile(r"QC\s*결과.*(?:정상|성공|통과|OK)", re.I)


_GREETING = re.compile(
    r"안녕하세요[.!~]*\s*|[^.\n]{0,25}(?:부장|차장|과장|대리|주임|선임|책임|수석|사원|팀장|매니저|이사|대표)(?:님)?입니다[.!~]*\s*"
)


_UNCONFIRMED = re.compile(
    r"요청\s*(?:드|합|함|하|해|$)|가능하실|했음에도|되었음에도|했지만|부탁|예정|계획|검토\s*중|진행\s*중|분석\s*중|확인\s*중|"
    r"해도\s*무방|추정|추측|의심|유추|파악\s*중|가능성|미상|불명|모름|미확인|(?<!불)필요|무엇인지|때문인지|"
    r"미완료|미해결|"
    r"(?:수정|조치|반영|적용|패치|배포|조사)\s*중|하겠|모르|"
    r"것\s*같|인\s*듯|인가요|입니까|일까요|맞나요|(?:할|알)\s*수\s*없|"
    r"해야|바랍|주세요|해줘|될\s*수\s*있|명확하지|단정하기\s*어려|가능하다고|여부|되었는지"
)


_CAUSE_NEGATION = re.compile(r"(?:원인|때문)[^.\n]*(?:아닙|아니었|아닌|보이나|보이며|보이지만|보임)|원인(?:으)?로\s*보|것으로\s*보")


_FIX_NOT_DONE = re.compile(r"일괄.{0,20}종료|완료된?\s*작업\s*종료|불가|한계|실패|않|못했|못하|미(?:반영|적용|배포|수정|처리)|안\s*(?:됨|됐|되|했|하)|이면|하면|한다면|된다면|되면|했으면")


_COMPLETED_NOTE = re.compile(
    r"복구\s*후|(?:로드|복구|재기동|재시작|재부팅)\s*(?:시킴|했|하였|완료)|"
    r"완료|종료(?:합니다|함|했|\s*$)|(?:delete|초기화)\s*(?:함|했|하였)|"
    r"(?:해결|수정|조치|반영|변경|패치|개선|배포|조정)"
    r"(?:(?:을)?\s*(?:함|했|하였|됨|되었|해두)|해서|하여.*(?:푸시|전달|반영|완료)|\s*$)|"
    r"추가\s*(?:했|하였|함|완료|반영|됨)|적용\s*(?:했|하였|되었)|전달\s*(?:했|하였|드렸|드립니다)|"
    r"(?:패치|파일|고객사).{0,40}전달\s*(?:됨|함|$)|(?:누수|릭).{0,12}잡았(?:다고\s*함|습니다|음)"
)


_PENDING_OUTCOME = re.compile(
    r"(?:배포|반영|적용|수정|해결|조치|전달).*(?:예정|미완료|미해결|않|못했|못하|진행\s*중)|"
    r"미(?:적용|반영|배포|수정|해결)|(?:배포|수정|조치|반영|적용)\s*중"
)


_COMPARE_STOPWORDS = frozenset({"현상", "문제", "오류", "수정", "요청", "확인", "관련", "발생", "처리", "원인", "화면", "경우", "본문", "노트", "제목"})


_NON_CAUSAL_CONTEXT = re.compile(r"문구.{0,20}(?:삭제|변경|수정)|소스\s*프리징|보류\s*(?:및|후)?\s*롤백|"
    r"원인\s*(?:파악\s*)?(?:설명|안내)|"
    r"더\s*중요.{0,15}때문|롤백.{0,15}하지\s*않고|진행하기로|"
    r"그럴\s*경우|이럴\s*때|이런\s*경우|원인으로\s*보고|개선책을\s*찾기|운영에서\s*테스트\s*가능|"
    r"우선순위.{0,50}(?:때문|인해)|(?:QC|검수|담당자).{0,40}병행|원인(?:을)?\s*(?:파악|분석|확인)\s*(?:및|요청|완료|후|중)|롤백.{0,10}하려고|"
    r"(?:오류|장애).{0,20}테스트\s*불가|테스트\s*종료|개선이?\s*어려운\s*원인(?:\s|$)")


_CAUSE_EFFECT = re.compile(r'고객\s*(?:불만|불편).{0,25}(?:증가|발생)|(?:문제|장애|성능|이슈).{0,25}(?:불만|불편)')


_CAUSE_LIMITATION = re.compile(r'개선.{0,25}(?:어렵|불가)|유지\s*보수.{0,35}범위|구조상.{0,45}(?:할\s*수\s*가?\s*없|불가)')


_OPERATIONAL_ACTION = re.compile(r'(?:가이드|안내|설명)\s*(?:해\s*줌|해주었|했|하였|함|드렸)')


_NON_TECHNICAL_CHANGE = re.compile(r'오픈\s*일정|오픈일|납기|마감일|담당자?\s*(?:변경|할당)')


_RESOURCE = re.compile(r"리소스|메모리|(?:사용자|USER|GDI)\s*(?:개체|객체|오브젝트)|타이머|핸들", re.I)


_RESOURCE_SYMPTOM = re.compile(r"증가|누수|누적|부족|해제.{0,6}(?:누락|않|안)|과다|급증|leak|리소스\s*개선|(?:메모리|리소스)\s*(?:릭|리크|누실)", re.I)


def _issue_resource_symptom(detail: dict) -> bool:
    fields = detail.get('resolution_fields') or {}
    return _resource_symptom(' '.join([*(str(detail.get(field) or '') for field in
                                      ('summary', 'description', 'additional_information')),
                                     *(str(value or '') for value in fields.values())]))


def _recorded_section(detail: Dict[str, Any], heading: str) -> str:
    """구조화된 작업 노트의 원인/조치 구역을 완료 통지보다 우선한다."""
    for note in reversed(_resolution_notes(detail)):
        text = _without_access_lines(str(note.get("text") or "")).replace("\r\n", "\n")
        match = re.search(r"(?m)^\s*[-#*]*\s*(?:" + heading + r")\s*[:：]?\s*\n", text)
        if match:
            block = re.split(r"\n\s*[-#*]+\s*[가-힣A-Za-z]", text[match.end():], maxsplit=1)[0].strip()
            if block:
                return block
    return ""


def _resource_symptom(text: str) -> bool:
    return bool(_RESOURCE.search(text) and _RESOURCE_SYMPTOM.search(text))


def _summarize_resolution(detail: Dict[str, Any]) -> str:
    """정규화된 이슈에서 '그때 어떻게 처리했는지' 한두 줄 요약을 만든다."""
    status = status_name(detail.get("status"))

    # 상태 변경 이력 중 마지막으로 상태가 바뀐 사건을 조치 시점으로 본다.
    resolved_event = None
    for item in detail.get("history") or []:
        change = str(item.get("change") or "")
        field = str(item.get("field") or "")
        if "=>" in change and ("상태" in field or "status" in field.casefold()):
            resolved_event = item

    parts = [f"상태 {status}"]
    if resolved_event:
        when = str(resolved_event.get("created_at") or "")[:10]
        change = str(resolved_event.get("change") or "").replace("=>", "→")
        parts.append(f"상태 변경 {change}{f' ({when})' if when else ''}")
    recorded_fix, origin = _recorded_fix(detail, max_chars=NOTE_SNIPPET_CHARS)
    if recorded_fix:
        label = "기록된 조치" if origin == "field" else "마지막 조치"
        parts.append(f"{label}: {recorded_fix}")
    else:
        parts.append("기록에서 완료된 해결·조치를 확인하지 못했습니다.")
    if caveat := _latest_resolution_caveat(detail):
        parts.append(f"후속 상태: {caveat}")
    return " · ".join(parts)


def _without_access_lines(text: str) -> str:
    # 계정 입력 줄은 원인·조치와 무관하며 줄 결합 전에 제거한다.
    return "\n".join(line for line in text.splitlines() if not re.search(
        r"(?:^|\b)(?:ID|PASS|Cerpass|PW|password|passwd|pwd|api[_ -]?key|access[_ -]?token)\s*[:=：]|"
        r"(?:비밀\s*번호|비번|패스워드)\s*[:=：]", line, re.I))


def _clean_note(text: str, detail: Dict[str, Any]) -> str:
    """노트 앞의 인사말·'0029941: [제목]' 반복을 걷어내 실제 내용만 남긴다."""
    cleaned = _GREETING.sub("", str(text or ""))
    cleaned = _without_access_lines(cleaned)
    issue_id = str(detail.get("id") or "")
    summary = str(detail.get("summary") or "").strip()
    if issue_id:
        # 메일 회신에 붙는 '0029941: [제목]' 반복은 노트 어디에 있어도 걷어낸다.
        title = re.escape(summary) if summary else ""
        cleaned = re.sub(rf"0*{re.escape(issue_id)}\s*[:：]\s*{title}", " ", cleaned)
    if summary:
        cleaned = re.sub(r'(?m)^\s*' + re.escape(summary) + r'\s*$', ' ', cleaned)
    return re.sub(r"[^\S\n]+", " ", cleaned).strip(" .-~\n")


def _unconfirmed(text: str, *, cause: bool = False) -> bool:
    # '예외를 처리하지 않아 발생한 오류'는 원인이다. 완료 조치의 부정과 구분한다.
    if cause:
        # '라이브러리 반영 중, 재기동 전 조회하여 CPU 증가됨'의 사건 시점은
        # 원인 파악이 진행 중이라는 뜻이 아니다. 뒤의 불확실성 검사는 유지한다.
        text = re.sub(r'(?:반영|적용|배포)\s*중(?=\s*[,，])', '', text)
    return bool(_UNCONFIRMED.search(text) or (_CAUSE_NEGATION if cause else _FIX_NOT_DONE).search(text))


def _note_sentences(text: str, *, join_wrapped_lines: bool = False) -> List[str]:
    """파일명·메서드·소수점은 유지하고 문장 끝의 구두점만 나눈다."""
    if join_wrapped_lines:
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return [part.strip(" .-~·:") for part in re.split(r"[。!?\n]+|\.(?=\s|$)", text)
            if part.strip(" .-~·:")]


def _cause_sentences(text: str) -> List[str]:
    separate = _note_sentences(text)
    wrapped = _note_sentences(text, join_wrapped_lines=True)
    # 이름: 내용 같은 구역은 다른 주체와 합치지 않는다. 구역이 없는 일반
    # 문장은 줄바꿈 앞의 함수명도 원인과 함께 보존한다.
    labelled = re.search(r'(?m)^\s*[-#*]*\s*[^\n:：]{1,24}[:：]', text)
    # 일반 문단의 예정·의문 문맥은 통째로 보존한다. 요청 문단 사이에 독립적으로
    # 적힌 본문 진단만 구체적인 메커니즘+진단 표현이 있는 경우 분리해 보완한다.
    isolated = separate if labelled else [part for part in separate if _BODY_DIAGNOSIS.search(part)]
    return list(dict.fromkeys([*isolated, *wrapped] if labelled else [*wrapped, *isolated]))


def _is_experiment(text: str) -> bool:
    return bool(re.search(r'(?:return|주석\s*처리|비활성|우회|로딩\s*없이|적재\s*없이).{0,100}'
                          r'(?:하면|되면|경우|때에도|때도)', text, re.I))


def _is_consequence(text: str, detail: dict) -> bool:
    # 특정 기능을 우회·제외한 재현 조건은 증상을 일으킨 메커니즘이 아니다.
    if _is_experiment(text):
        return True
    if re.search(r'이\s*때에도|이\s*때도|그런데도|^되는데', text):
        # 모델 문단이 줄바꿈으로 나뉘어도 지시어가 참조하는 앞 실험을 확인한다.
        needle = ' '.join(text.split())
        for raw in [detail.get('description'), *(note.get('text') for note in _resolution_notes(detail))]:
            source = ' '.join(str(raw or '').split())
            position = source.find(needle)
            if position >= 0 and _is_experiment(source[max(0, position - 250):position + len(needle)]):
                return True
    title = str(detail.get('summary') or '')
    if _CAUSE_EFFECT.search(text) and not re.search(r'불만|불편', title):
        return True
    # 같은 이슈 안의 별도 결함이나 측정 조건도 현재 증상의 원인으로 승격하지 않는다.
    if re.search(r'CPU|버벅|부하', title, re.I):
        secondary = re.search(r'(?:값|수치).{0,25}(?:다르|다른|차이)|(?:Type\s*)?캐스팅\s*차이|회선\s*지연', text, re.I)
        causal_cpu = re.search(r'(?:때문|인해|해서|하여).{0,30}CPU.{0,20}(?:증가|발생|높|올라)|'
                               r'CPU.{0,20}(?:원인|증가|발생|높아|올라)', text, re.I)
        if secondary and not causal_cpu:
            return True
    return False


def _unresolved_cause_note(detail: dict) -> str:
    """확인된 결함의 수정과 신고 증상의 원인 확정을 구분한다."""
    for note in reversed(_resolution_notes(detail)):
        text = _clean_note(str(note.get('text') or ''), detail)
        for part in _note_sentences(text, join_wrapped_lines=True):
            if (re.search(r'원인(?:으)?로\s*확인|원인(?:이|은|:).{0,60}(?:확인|판명)', part)
                    and not _unconfirmed(part, cause=True)):
                return ''
            if re.search(r'원인.{0,12}(?:명확하지|불명확|불명|확인.{0,8}(?:못|않))', part):
                return compact_text(part, 160) + _note_reference(note)
    return ''


def _recorded_cause(detail: Dict[str, Any]) -> Tuple[str, str]:
    """확인 가능한 원인 원문과 출처. 빈 값은 추출 실패이며 기록 부재와 다르다."""
    field = _without_access_lines(str((detail.get("resolution_fields") or {}).get(CAUSE_FIELD) or "")).strip()
    if field and not _unconfirmed(field, cause=True):
        return compact_text(field, COMPARE_CELL_CHARS), "field"
    if _unresolved_cause_note(detail):
        return '', ''
    if (facts := detail.get('comparison_facts') or {}).get('verified') and facts['cause']:
        selected = [row for row in facts['cause'] if not _is_consequence(row['text'], detail)]
        if selected:
            return render_facts(selected)
    section = _recorded_section(detail, r"현상\s*원인|직접\s*원인|원인")
    if section and not _unconfirmed(section, cause=True) and not _is_consequence(section, detail):
        return compact_text(section, COMPARE_CELL_CHARS), "note"
    limitation = None
    for note in reversed(_resolution_notes(detail)):
        cleaned = _clean_note(str(note.get("text") or ""), detail)
        # 원인·조치·서버별 줄 경계를 먼저 지킨다. 줄바꿈만 된 문장은 후순위로 보완한다.
        sentences = _cause_sentences(cleaned)
        # 한 회신의 여러 Q&A 중 증상에 대한 직접 자원 부하 답변을 먼저 본다.
        sentences.sort(key=lambda sentence: not bool(re.search(
            r'(?:증가|발생).{0,12}(?:함에|에)\s*따라.{0,50}(?:자원|CPU|부하|메모리)', sentence, re.I)))
        for sentence in sentences:
            if (len(sentence) >= 8 and _CAUSE_SENTENCE.search(sentence)
                    and not _NON_CAUSAL_CONTEXT.search(sentence) and not _unconfirmed(sentence, cause=True)
                    and not _is_consequence(sentence, detail)):
                if _CAUSE_LIMITATION.search(sentence):
                    limitation = (compact_text(sentence, COMPARE_CELL_CHARS), "note")
                    continue
                return compact_text(focused_excerpt(sentence, _CAUSE_SENTENCE, COMPARE_CELL_CHARS), COMPARE_CELL_CHARS) + _note_reference(note), "note"
    description = _clean_note(str(detail.get('description') or ''), detail)
    sentences = _cause_sentences(description)
    for sentence in sentences:
        if (_CAUSE_SENTENCE.search(sentence) and not _NON_CAUSAL_CONTEXT.search(sentence)
                and not _unconfirmed(sentence, cause=True) and not _is_consequence(sentence, detail)):
            return compact_text(focused_excerpt(sentence, _CAUSE_SENTENCE, COMPARE_CELL_CHARS), COMPARE_CELL_CHARS), 'description'
    return limitation or ("", "")


def _note_reference(note: dict) -> str:
    return f" [노트 {note['id']}]" if note.get('id') else ''


def _progress_evidence(detail: dict, *, after_note_id: int = 0) -> list[str]:
    from backend.evidence.stages import stage_events
    stages = []
    for stage, label in (('qc', 'QC 완료 기록'), ('delivery', '고객사 전달 기록')):
        events = []
        for note in _resolution_notes(detail):
            raw = str(note.get('text') or '')
            for event in stage_events(stage, {'text': raw, 'note_id': note.get('id')}):
                if event[0] == 'unknown':
                    continue
                if stage == 'delivery':
                    # 설정자료 송부·회신은 수정 모듈을 고객에게 전달한 단계가 아니다.
                    artifact = re.search(r'\[\s*(?:고객사\s*)?전달(?:\s*완료)?\s*\]|'
                                         r'(?:수정|패치|배포|업데이트|모듈|QC|git|repository|저장소).{0,40}(?:전달|발송)|고객사.{0,12}전달', event[2], re.I)
                    if not artifact or re.search(r'겠|예정|원하시면|부탁|요청', event[2]):
                        continue
                events.append(event)
            if stage == 'delivery' and any(re.fullmatch(r'\s*(?:수정|패치)\s*모듈\s*전달\s*', line)
                                           for line in raw.splitlines()):
                events.append(('done', {'note_id': note.get('id')}, raw))
        if events and events[-1][0] == 'done':
            _state, row, text = events[-1]
            # QC 원문 결과를 보존하되, QC를 기술 수정이나 운영 배포로 승격하지 않는다.
            value = text if stage == 'qc' and _QC_SUCCESS.search(text) else label
            if stage == 'delivery' and re.search(r'git|repository|저장소', text, re.I):
                value = '저장소 전달 기록'
            if str(row.get('note_id') or '').isdigit() and int(row['note_id']) < after_note_id:
                value = '이전 ' + value
            stages.append(value + (f" [노트 {row['note_id']}]" if row.get('note_id') else ''))
    return stages


def _recorded_fix(detail: Dict[str, Any], *, max_chars: int = COMPARE_CELL_CHARS,
                  include_progress: bool = True) -> Tuple[str, str]:
    """확인된 조치와 QC/전달 단계만 기록하며 전달을 운영 배포로 바꾸지 않는다."""
    field = _without_access_lines(str((detail.get("resolution_fields") or {}).get(FIX_FIELD) or "")).strip()
    if field and not _unconfirmed(field):
        return compact_text(field, max_chars), "field"
    if (facts := detail.get('comparison_facts') or {}).get('verified') and facts['action']:
        action_ids = [int(r['field'].split(':', 1)[1]) for r in facts['action']
                      if re.fullmatch(r'note:\d+', r['field'])]
        stages = _progress_evidence(detail, after_note_id=max(action_ids, default=0)) if include_progress else []
        text, origin = render_facts(facts['action'], max_chars - len('. '.join(stages)) - 2)
        return '. '.join(filter(None, [text, *stages])), origin or ('note' if stages else '')
    section = _recorded_section(detail, r"작업\s*내역|조치\s*(?:내용|내역)|수정\s*(?:사항|내역)")
    stages = _progress_evidence(detail) if include_progress else []
    if section and not _unconfirmed(section):
        action = compact_text(section, max(80, max_chars - len('. '.join(stages)) - 2))
        return compact_text('. '.join([action, *stages]), max_chars), 'note'
    actions = []
    latest_action_id = 0
    source_change = ''
    for note in reversed(_resolution_notes(detail)):
        raw = str(note.get("text") or "").strip()
        if not raw or raw.startswith("이슈노트 추가됨"):
            continue
        cleaned = _clean_note(raw, detail)
        # SVN 변경 링크 자체에는 조치 의미가 없다. 실제 변경 파일만 제한적으로 표시한다.
        changed_files = _SVN_FILE.findall(cleaned)
        if changed_files and re.search(r"svn\s|리비전\s*:", cleaned, re.I):
            if not source_change:
                names = [re.split(r'[/\\]', path)[-1] for path in changed_files]
                source_change = "소스 변경 기록: " + ", ".join(list(dict.fromkeys(names))[:3]) + _note_reference(note)
        text = "\n".join(line for line in cleaned.splitlines()
                         if not _NOTE_METADATA.search(line.strip())
                         and not re.fullmatch(r"\[.*\].*", line.strip())
                         and not _SVN_FILE.fullmatch(line.strip()))
        sentences = _note_sentences(text)
        confirmed = []
        for index, part in enumerate(sentences):
            if not include_progress and re.fullmatch(
                    r'(?:수정하여\s*)?(?:소스|수정본|모듈|패치|라이브러리)\s*전달\s*(?:드립니다|드렸습니다|완료|함)', part):
                continue
            if re.fullmatch(r'(?:현상|원인|조치|조치\s*(?:내용|내역)|작업\s*내역)', part):
                continue
            if (re.search(r'(?:소스|모듈|패치|수정본).{0,60}적용\s*후.{0,80}(?:확인|측정|테스트)', part)
                    and not re.search(r'부탁|요청|주세요|적용\s*후.{0,20}(?:할|하겠)', part)):
                confirmed.append(part)
                continue
            completion = (_COMPLETED_NOTE.search(part) or re.search(
                r'처리(?:함|\s*$)|(?:처리|해제).{0,90}정상\s*해제\s*확인', part) or _OPERATIONAL_ACTION.search(part))
            changed = re.search(r'개발|구현|수정|추가|조정|개선|삭제|제거|변경|해제|패치|적용|반영|해결|갱신|조치|복구|로드|재기동|재시작|재부팅|delete|초기화|(?:누수|릭).{0,12}잡았', part) or _OPERATIONAL_ACTION.search(part)
            if (not completion or not changed or _unconfirmed(part) or _QC_SUCCESS.search(part)
                    or _NON_TECHNICAL_CHANGE.search(part)):
                continue
            if index and re.search(r'\bclass\b|\bDelete\b|해제\s*누락|(?:로드|적재).{0,12}(?:않|누락)', sentences[index - 1], re.I):
                part = sentences[index - 1] + ': ' + part
            confirmed.append(focused_excerpt(part.lstrip('> '), re.compile(
                r'개발|구현|수정|추가|조정|개선|삭제|제거|변경|해제|패치|적용|반영|해결|갱신|조치|복구|로드|재기동|재시작|재부팅|delete|초기화', re.I), max_chars))
        if confirmed:
            actions.append('. '.join(list(dict.fromkeys(confirmed))[:2]) + _note_reference(note))
            if not latest_action_id and str(note.get('id') or '').isdigit():
                latest_action_id = int(note['id'])
    selected = actions[:2] or ([source_change] if source_change else [])
    stages = _progress_evidence(detail, after_note_id=latest_action_id) if include_progress else []
    if not selected and not stages:
        return '', ''
    stage_text = '. '.join(stages)
    action_text = compact_text('. '.join(selected), max(80, max_chars - len(stage_text) - 2)) if selected else ''
    return compact_text('. '.join(filter(None, [action_text, stage_text])), max_chars), 'note'


def _latest_resolution_caveat(detail: Dict[str, Any], *, with_reference: bool = False) -> str:
    """과거 완료 기록을 최신 미해결·배포 예정 상태로 오해하지 않도록 한정한다."""
    for note in reversed(_resolution_notes(detail)):
        sentences = _note_sentences(_clean_note(str(note.get("text") or ""), detail))
        pending = [part for part in sentences if _PENDING_OUTCOME.search(part)]
        pending.extend(part for part in sentences if re.search(
            r'(?:처리|수정|조치)\s*후.*(?:변화.{0,8}없|동일|지속|여전)|'
            r'(?:추가|항목|부분|지표).{0,35}확인\s*필요|'
            r'(?:아직도|여전히).{0,40}(?:메모리|리소스|CPU|증가|오르|발생|지속)|'
            r'추가\s*확인\s*요청\s*수신', part, re.I))
        if pending:
            value = compact_text(". ".join(pending), NOTE_SNIPPET_CHARS)
            if with_reference:
                value += _note_reference(note) + (' · 노트 기록일 ' + str(note['created_at'])[:10] if note.get('created_at') else '')
                # 연락일과 노트 등록일이 다르면 둘 다 원문 범위에서 표시한다.
                contact = re.match(r'\s*(\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})\s*(?:유선|메일|통화)', str(note.get('text') or ''))
                if contact:
                    value += ' · 연락일 ' + contact[1]
            return value
        if (any(_COMPLETED_NOTE.search(part) and not _unconfirmed(part) for part in sentences)
                or re.search(r'\[\s*(?:고객사\s*)?전달(?:\s*완료)?\s*\]', str(note.get('text') or ''))):
            break
    return ""


def _unconfirmed_cause(detail: dict) -> str:
    if unresolved := _unresolved_cause_note(detail):
        return unresolved
    if rows := (detail.get('comparison_facts') or {}).get('uncertain_cause'):
        return render_facts(rows)[0]
    for note in reversed(_resolution_notes(detail)):
        cleaned = _clean_note(str(note.get('text') or ''), detail)
        for part in _cause_sentences(cleaned):
            if (re.search(r'원인|누수.{0,40}(?:단정|판단)|(?:CPU|자원|부하).{0,80}(?:추측|추정|예상)', part, re.I) and _unconfirmed(part, cause=True)
                    and not re.search(r'요청|부탁|주세요|무엇|때문인지', part)):
                return compact_text(part, 160) + _note_reference(note)
    return ''


def _resolution_notes(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 원문 발췌는 표시용 노트의 길이 제한 밖에 있는 근거를 보완한다.
    excerpts = [item for item in [*(detail.get("matching_evidence") or []),
                                  *((detail.get("answer_evidence") or {}).get("excerpts") or [])]
                if str(item.get("field") or "").split(":", 1)[0] == "note"]
    notes: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate([*excerpts, *(detail.get("notes") or [])]):
        note_id = str(item.get("id") or item.get("note_id") or
                      str(item.get("field") or "").partition(":")[2])
        key = note_id or f"position:{index}"
        text = str(item.get("text") or "")
        if key in notes:
            previous = str(notes[key].get("text") or "")
            if text in previous:
                text = previous
            elif previous not in text:
                text = previous + "\n\n" + text
        notes[key] = {**notes.get(key, {}), **item, "id": note_id, "text": text}
    # Mantis ID는 시간순이다. ID 없는 입력은 원래 순서를 유지한다.
    return sorted(notes.values(), key=lambda item: int(str(item.get("id") or "0"))
                  if str(item.get("id") or "0").isdigit() else 0)


def _table_cell(text: str, origin: str, *, has_evidence: bool = False) -> str:
    if not text:
        return "기록에서 확인되지 않음" if has_evidence else "기록 없음"
    cell = re.sub(r"\s+", " ", text).replace("|", "/")
    return f"{cell} (노트)" if origin == "note" else f"{cell} (본문)" if origin == 'description' and '(본문)' not in cell else cell


def _clue_terms(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
        if token not in _COMPARE_STOPWORDS and not token.isdigit()
    }
