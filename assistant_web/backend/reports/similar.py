"""유사사례 해결책 리포트 — 비슷한 과거 이슈 + 그때의 조치 요약(상위 계층).

기준 이슈와 의미가 비슷한 과거 이슈를 ChromaDB 임베딩으로 찾은 뒤, 각 사례가 실제로
어떻게 처리됐는지(상태·마지막 노트·상태 변경 이력)를 요약해 함께 보여준다. 단순히
'비슷한 이슈 목록'을 넘어 축적된 해결 지식을 재사용하도록 돕는다.

execute_chroma_search(semantic=True)는 후보 문서를 라이브 Mantis로 refresh해
latest_issue(notes/history 포함)를 채워주므로, 여기서는 추가 조회 없이 그 결과에서
해결 맥락을 뽑아낸다.

의존: chroma(유사도 검색) + mantis(기준 이슈 조회/정규화) + understanding(제약/유사질의).
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from backend.display import customer_name, status_name
from backend.common import compact_text, normalize_for_match
from backend.sources.chroma import execute_chroma_search
from backend.sources.chroma_lexical import lexical_ranking_scope
from backend.sources.mantis_hybrid import hybrid_filter_scope
from backend.sources.mantis_refresh import short_refresh_scope
from backend.template_questions import attach_korean_particles
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_issue_by_id,
    normalize_issue,
)
from backend.understanding import (
    build_similar_issue_search_text,
    extract_query_constraints,
    wants_similar_issue_search,
)


# '어떻게 해결했는지'를 함께 원하는 신호. 유사검색 의도에 이게 더해질 때만 동작한다.
RESOLUTION_MARKERS = (
    "해결", "어떻게", "조치", "처리방법", "처리 방법", "대응", "원인",
    "고쳤", "고친", "수정방법", "픽스", "fix",
)

SIMILAR_TOP_N = 3            # 조치까지 정리해 보여줄 유사 사례 수
NOTE_SNIPPET_CHARS = 220     # 사례별 조치 요약 길이
COMPARE_CELL_CHARS = 220      # 원인·해결 비교표 한 칸 길이
CAUSE_FIELD = "사유 및 원인"
FIX_FIELD = "조치 및 개선"
# '원인'이라는 표제가 없어도 원문에 명시된 구현 불일치·제약은 보존한다.
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
# 인사말과 '○○ 과장입니다' 같은 자기소개. 직급이 붙은 경우만 지워 '원인은 …입니다' 같은 내용은 남긴다.
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


def _search_similar_candidates(query: str, constraints: dict, resource: bool) -> dict:
    symptom_query='리소스 증가 메모리 누수 사용자 개체 타이머 해제 누락'
    with (lexical_ranking_scope([query,symptom_query] if resource else [query]),
          hybrid_filter_scope(), short_refresh_scope() as refresh):
        result=execute_chroma_search(query,8 if resource else SIMILAR_TOP_N,constraints,semantic=True)
        if result.get('status')!='ok':
            return result
        searches=[result.get('meta') or {}]
        documents=list(result.get('documents') or [])
        if resource:
            extra=execute_chroma_search(symptom_query,8,constraints,semantic=True)
            searches.append(extra.get('meta') or {})
            pool=[*documents,*(extra.get('documents') or [])] if extra.get('status')=='ok' else documents
            seen=set(constraints.get('exclude_issue_ids') or [])
            documents=[]
            for document in pool:
                issue_id=int(document.get('issue_id') or 0)
                detail=document.get('latest_issue') or {}
                if (issue_id in seen or '[중복제외]' in str(detail.get('summary') or '')
                        or not _issue_resource_symptom(detail)):
                    continue
                seen.add(issue_id)
                documents.append(document)
            documents=documents[:SIMILAR_TOP_N]
        else:
            documents = [doc for doc in documents
                         if '[중복제외]' not in str((doc.get('latest_issue') or {}).get('summary') or '')]
        return {**result,'documents':documents,'searches':searches,
                'refresh_budget': {'seconds': refresh.total_seconds, 'spent_seconds': round(refresh.spent_seconds, 3),
                                   'cache_hits': refresh.cache_hits}}


def _wants_resolution(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(normalize_for_match(marker) in normalized for marker in RESOLUTION_MARKERS)


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


def _is_consequence(text: str, detail: dict) -> bool:
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
        from backend.reports.comparison_facts import render_facts
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
                return compact_text(sentence, COMPARE_CELL_CHARS) + _note_reference(note), "note"
    description = _clean_note(str(detail.get('description') or ''), detail)
    sentences = _cause_sentences(description)
    for sentence in sentences:
        if (_CAUSE_SENTENCE.search(sentence) and not _NON_CAUSAL_CONTEXT.search(sentence)
                and not _unconfirmed(sentence, cause=True) and not _is_consequence(sentence, detail)):
            return compact_text(sentence, COMPARE_CELL_CHARS), 'description'
    return limitation or ("", "")


def _note_reference(note: dict) -> str:
    return f" [노트 {note['id']}]" if note.get('id') else ''


def _progress_evidence(detail: dict, *, after_note_id: int = 0) -> list[str]:
    from backend.evaluation.explanation_direct import _stage_event
    stages = []
    for stage, label in (('qc', 'QC 완료 기록'), ('delivery', '고객사 전달 기록')):
        events = []
        for note in _resolution_notes(detail):
            raw = str(note.get('text') or '')
            for event in _stage_event(stage, {'text': raw, 'note_id': note.get('id')}):
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


def _recorded_fix(detail: Dict[str, Any], *, max_chars: int = COMPARE_CELL_CHARS) -> Tuple[str, str]:
    """확인된 조치와 QC/전달 단계만 기록하며 전달을 운영 배포로 바꾸지 않는다."""
    field = _without_access_lines(str((detail.get("resolution_fields") or {}).get(FIX_FIELD) or "")).strip()
    if field and not _unconfirmed(field):
        return compact_text(field, max_chars), "field"
    if (facts := detail.get('comparison_facts') or {}).get('verified') and facts['action']:
        from backend.reports.comparison_facts import render_facts
        action_ids = [int(r['field'].split(':', 1)[1]) for r in facts['action']
                      if re.fullmatch(r'note:\d+', r['field'])]
        stages = _progress_evidence(detail, after_note_id=max(action_ids, default=0))
        text, origin = render_facts(facts['action'], max_chars - len('. '.join(stages)) - 2)
        return '. '.join(filter(None, [text, *stages])), origin or ('note' if stages else '')
    section = _recorded_section(detail, r"작업\s*내역|조치\s*(?:내용|내역)|수정\s*(?:사항|내역)")
    stages = _progress_evidence(detail)
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
            confirmed.append(part.lstrip('> '))
        if confirmed:
            actions.append('. '.join(list(dict.fromkeys(confirmed))[:2]) + _note_reference(note))
            if not latest_action_id and str(note.get('id') or '').isdigit():
                latest_action_id = int(note['id'])
    selected = actions[:2] or ([source_change] if source_change else [])
    stages = _progress_evidence(detail, after_note_id=latest_action_id)
    if not selected and not stages:
        return '', ''
    stage_text = '. '.join(stages)
    action_text = compact_text('. '.join(selected), max(80, max_chars - len(stage_text) - 2)) if selected else ''
    return compact_text('. '.join(filter(None, [action_text, stage_text])), max_chars), 'note'


def _latest_resolution_caveat(detail: Dict[str, Any]) -> str:
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
            return compact_text(". ".join(pending), NOTE_SNIPPET_CHARS)
        if (any(_COMPLETED_NOTE.search(part) and not _unconfirmed(part) for part in sentences)
                or re.search(r'\[\s*(?:고객사\s*)?전달(?:\s*완료)?\s*\]', str(note.get('text') or ''))):
            break
    return ""


def _history_lines(detail: dict) -> list[str]:
    """중복 종료는 해결 조치로 승격하지 않고 기준 이슈의 처리 이력으로 남긴다."""
    lines = []
    for note in _resolution_notes(detail):
        text = _clean_note(str(note.get('text') or ''), detail)
        for sentence in _note_sentences(text):
            if re.search(r'(?:동일|중복).{0,35}(?:종료|처리)', sentence):
                lines.extend([f"- 기준 이슈 처리 이력{_note_reference(note)}:", f'> {sentence}'])
            elif re.search(r'롤백|이슈.{0,20}(?:부활|재오픈)|재확인.{0,30}부활', sentence):
                label = '조치 계획·결정 기록' if re.search(r'하려고|하기로|예정|하겠|시키도록', sentence) else '조치 이력'
                lines.extend([f"- #{detail.get('id')} {label}{_note_reference(note)}:",
                              '> ' + compact_text(sentence, 220)])
    return lines[-8:]


def _unconfirmed_cause(detail: dict) -> str:
    if unresolved := _unresolved_cause_note(detail):
        return unresolved
    if rows := (detail.get('comparison_facts') or {}).get('uncertain_cause'):
        from backend.reports.comparison_facts import render_facts
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


def _comparison_lines(base_issue: Dict[str, Any], examples: List[Dict[str, Any]]) -> List[str]:
    """기준 이슈와 사례의 원인·해결을 표로 나란히 놓고, 기록 범위와 겹치는 단서만 짚는다."""
    base_cause, base_cause_origin = _recorded_cause(base_issue)
    base_fix, base_fix_origin = _recorded_fix(base_issue)
    rows = [f"| 기준 #{base_issue.get('id')} | {_table_cell(base_cause, base_cause_origin, has_evidence=bool(_resolution_notes(base_issue)))} | "
            f"{_table_cell(base_fix, base_fix_origin, has_evidence=bool(_resolution_notes(base_issue)))} |"]
    case_causes = []
    for item in examples:
        cause, cause_origin = _recorded_cause(item)
        fix, fix_origin = _recorded_fix(item)
        case_causes.append((item, cause))
        rows.append(f"| #{item.get('id')} | {_table_cell(cause, cause_origin, has_evidence=bool(_resolution_notes(item)))} | {_table_cell(fix, fix_origin, has_evidence=bool(_resolution_notes(item)))} |")

    recorded = sum(1 for _, cause in case_causes if cause)
    lines = [
        "**원인·해결 비교**",
        "",
        "| 이슈 | 원인 | 해결·조치 |",
        "|---|---|---|",
        *rows,
        "",
        f"- 원인 기록: 기준 이슈 {'있음' if base_cause else ('미확인' if _resolution_notes(base_issue) else '없음')}, 과거 사례 {recorded}/{len(examples)}건.",
    ]
    for item in [base_issue, *examples]:
        cause_text = _recorded_cause(item)[0]
        uncertain = _unconfirmed_cause(item)
        # 같은 노트의 서로 다른 원인 범위는 함께 남긴다. 이후 확정 필드가
        # 있는 경우 예전 다른 노트의 추측을 현재 미확정 원인으로 되살리지 않는다.
        shared_note = set(re.findall(r'\[노트 (\d+)\]', cause_text)) & set(re.findall(r'\[노트 (\d+)\]', uncertain))
        if uncertain and (not cause_text or shared_note) and normalize_for_match(uncertain) != normalize_for_match(cause_text):
            lines.append(f"- #{item.get('id')}: 미확정 원인 — {uncertain}")
        if caveat := _latest_resolution_caveat(item):
            lines.append(f"- #{item.get('id')}: 후속 상태 — {caveat} (노트).")
    lines.extend(_history_lines(base_issue))
    base_terms = _clue_terms(f"{base_cause} {base_issue.get('summary') or ''}")
    for item, cause in case_causes:
        shared = sorted(base_terms & _clue_terms(cause)) if cause else []
        if shared:
            lines.append(f"- #{item.get('id')}: 기준 이슈와 겹치는 원인 단서 {', '.join(shared[:4])}. 같은 원인인지는 재현으로 확인하세요.")
    lines.append(
        "- 원인을 기록에서 확인하지 못한 사례는 해결·조치 내용으로만 비교했고, 같은 원인이라고 단정하지 않았습니다."
        if recorded < len(examples) else
        "- 기록된 원인끼리 비교했으며, 같은 원인인지는 재현으로 확인해야 합니다."
    )
    return lines


def _build_playbook_candidate(
    base_issue: Dict[str, Any], examples: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """현재 답변을 검수 가능한 플레이북 초안으로 넘길 구조를 만든다."""
    base_id = int(base_issue.get("id") or 0)
    base_summary = compact_text(base_issue.get("summary") or "이슈 대응", 160)
    description = compact_text(base_issue.get("description") or "", 1200)
    symptom = f"#{base_id} {base_summary}" if base_id else base_summary
    if description:
        symptom += f"\n\n{description}"
    resolution_lines = [
        f"- #{int(item.get('id') or 0)} {_summarize_resolution(item)}"
        for item in examples
        if int(item.get("id") or 0) > 0
    ]
    tags: List[str] = []
    for value in (
        base_issue.get("project"),
        base_issue.get("screen_number"),
        base_issue.get("platform"),
    ):
        tag = str(value or "").strip()
        if tag and tag not in tags:
            tags.append(tag)
    cause_lines = [
        f"- #{int(item.get('id') or 0)} {cause}"
        for item in [base_issue, *examples]
        if int(item.get("id") or 0) > 0 and (cause := _recorded_cause(item)[0])
    ]
    return {
        "title": f"{base_summary} 대응",
        "symptom": symptom,
        "cause": "\n".join(cause_lines),
        "resolution": "\n".join(resolution_lines),
        "verification": "",
        "tags": tags,
        "sourceIssueIds": [
            issue_id
            for issue_id in [base_id, *[int(item.get("id") or 0) for item in examples]]
            if issue_id > 0
        ],
    }


def build_similar_resolution_report(text: str, *, validated_plan: bool = False, model=None, api_key=None, topic_terms=None) -> Optional[Dict[str, Any]]:
    """'#12345랑 비슷한 이슈 어떻게 해결했어?'류 질문에 사례+조치로 답한다."""
    constraints = extract_query_constraints(text)
    confirmed=validated_plan and constraints.get('unified_plan_validated') is True
    if not confirmed and not wants_similar_issue_search(text, constraints):
        return None
    if not confirmed and not _wants_resolution(text):
        return None
    base_ids = list(constraints.get("issue_ids") or [])
    if not base_ids:
        return None

    base_id = int(base_ids[0])
    raw_base = fetch_mantis_issue_by_id(base_id)
    if not raw_base:
        return {
            "content": f"기준 이슈 #{base_id}를 찾지 못했어요. 이슈 번호를 다시 확인해 주세요.",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "base_not_found",
                            "meta": {"base_id": base_id, "base_issue_ids": [base_id]}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    base_issue = normalize_issue(raw_base, evidence_query=text)
    similar_query = build_similar_issue_search_text(base_issue) or text
    topic_text = ' '.join(str(term) for term in (topic_terms or [])) if confirmed else text
    cpu_focus = bool(re.search(r'(?<![A-Za-z])CPU(?![A-Za-z])', similar_query + ' ' + topic_text, re.I))
    if cpu_focus and 'cpu' not in similar_query.lower():
        similar_query = 'CPU ' + similar_query
    similar_constraints = {
        **constraints,
        "issue_ids": [],
        "exclude_issue_ids": base_ids,
        "max_issue_id_exclusive": base_id,
        # 비교 주제는 검증된 기준 원문에서 만든다. 원시 질문의 '더 오래된' 같은
        # 지시어를 다시 필수 검색어로 해석하지 않는다. 명시 조건은 아래 절을 유지한다.
        "semantic_focus_terms": ['CPU'] if cpu_focus else [],
        "require_semantic_focus": True,
        "resolution_evidence_query": similar_query + ' 원인 실제 수정 조치 완료 미해결',
        "filter_clauses": [
            clause
            for clause in constraints.get("filter_clauses", [])
            if clause.get("field") != "issue_id"
        ],
    }
    result = _search_similar_candidates(similar_query, similar_constraints,
                                        _issue_resource_symptom(base_issue))
    if result.get("status") not in {"ok"}:
        reason = result.get("reason") or "유사도 검색을 사용할 수 없습니다."
        return {
            "content": (
                attach_korean_particles(f"#{base_id}와(과) 비슷한 과거 사례를 찾는 중 문제가 있었어요.\n\n{reason}")
            ),
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "search_failed",
                            "reason": reason, "meta": {"base_id": base_id, "base_issue_ids": [base_id]}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    documents = [
        doc for doc in (result.get("documents") or [])
        if int(doc.get("issue_id") or 0) != base_id
    ]
    searches = result.get('searches') or []

    header = attach_korean_particles(
        f"🔍 #{base_id} \"{compact_text(base_issue.get('summary', ''), 60)}\"와(과) 비슷한 "
        f"과거 사례의 원인과 그때의 조치를 정리했어요."
    )
    if not documents:
        return {
            "content": '\n'.join([header, '', '지금 조건으로는 비슷한 과거 사례를 찾지 못했어요.',
                                    *_history_lines(base_issue)]),
            "sources": build_sources_from_issue_rows([base_issue]),
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "no_results", "issues_list":[base_issue],
                            "meta": {"base_id": base_id, "base_issue_ids": [base_id], "displayed_count":0,
                                     "derived_query": similar_query[:300],"searches":searches,
                                     "refresh_budget": result.get('refresh_budget')}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    lines = [header, ""]
    snapshot_fallback = sum(doc.get('latest_source') in {'mantis_snapshot', 'chroma_snapshot'} for doc in documents)
    if snapshot_fallback:
        saved_at = next((str(search.get('fetchedAt')) for search in searches if search.get('fetchedAt')), '수집 시각 미확인')
        lines.extend([f'일부 사례는 원문 갱신이 지연되어 저장본 근거로 비교했습니다. 저장본 수집 시각: {saved_at}', ''])
    example_rows: List[Dict[str, Any]] = []
    for index, doc in enumerate(documents, start=1):
        latest = doc.get("latest_issue")
        if latest:
            detail = latest
        else:
            # 라이브 refresh가 안 된 경우(스냅샷 폴백) 최소 정보라도 보여준다.
            detail = {
                "id": doc.get("issue_id"),
                "summary": "제목 미상",
                "project": "미상",
                "status": "상태 미상(스냅샷)",
                "notes": [],
                "history": [],
            }
        example_rows.append(detail)
        lines.extend([
            f"{index}. **#{detail.get('id')} {detail.get('summary', '제목 없음')}** "
            f"({customer_name(detail.get('project'))} · {status_name(detail.get('status'))})",
        ])

    from backend.reports.comparison_facts import review_comparison_facts
    comparison_review = review_comparison_facts([base_issue, *example_rows], model, api_key)
    from backend.reports.comparison_narrative import summarize_comparison
    narrative, summary_meta = summarize_comparison(text, [base_issue, *example_rows], model, api_key)
    if narrative:
        lines[2:2] = [narrative, '']
    elif summary_meta['status'] in {'unavailable', 'rejected'}:
        lines[2:2] = ['요약을 확정하지 못해 확인된 사실과 원문 근거 비교를 표시했습니다.', '']
    lines.extend(["", *_comparison_lines(base_issue, example_rows)])
    if comparison_review['status'] in {'unavailable', 'rejected'}:
        lines.append('- 일부 원문 근거의 추가 분류를 확정하지 못해 확인 가능한 기록만 표시했습니다.')
    lines.extend([
        "",
        "※ 유사도는 제목·증상 기준입니다. 원인·해결은 Mantis의 '사유 및 원인'·'조치 및 개선'을 먼저 쓰고, "
        "비어 있으면 본문·노트에서 확인한 내용을 출처와 함께 표시했습니다. QC·전달은 실제 기술 조치와 구분합니다.",
    ])

    sources = build_sources_from_issue_rows([base_issue, *example_rows])
    follow_up_sources = build_sources_from_issue_rows(example_rows)
    retrieval = {
        "query": text,
        "constraints": constraints,
        "stages": [{
            "source": "mantis_similar_resolution",
            "status": "ok",
            "issues_list": [base_issue, *example_rows],
            "meta": {
                "base_id": base_id,
                # 응답 검증이 머리글의 기준 이슈 번호를 근거 없는 번호로 보지 않게 한다.
                "base_issue_ids": [base_id],
                "derived_query": similar_query[:300],
                "displayed_count": len(example_rows),
                "searches": searches,
                "refresh_budget": result.get('refresh_budget'),
                "snapshot_fallback_count": snapshot_fallback,
                "comparison_review": comparison_review,
                "comparison_summary": summary_meta,
            },
        }],
    }
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": follow_up_sources,
        "playbookCandidate": _build_playbook_candidate(base_issue, example_rows),
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_similar_resolution"},
    }
