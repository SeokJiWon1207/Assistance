"""원문을 자르지 않고 첫 화면용 근거 문단을 선택한다."""
import re
from typing import Any
from backend.evidence.excerpts import _MAX_CORE_CHARS, _NOTE_BOILERPLATE
_NOTE_STATE = re.compile(r'확인|수정|반영|전달|조치|배포|완료|보류|예정|요청|오류|현상|적용|개발|진행|복구|변경|원인|문제')


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
