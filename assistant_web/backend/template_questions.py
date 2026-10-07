"""팀 템플릿 질문 문장 만들기. 화면의 frontend_core.js(fillTeamTemplateQuestion)와 같은 규칙이다.

질문 틀에는 '(으)로', '을(를)', '와(과)'처럼 조사를 적어 두고, 채운 값의 마지막 소리에
맞춰 하나로 고른다. 고른 값에 따라 '목록로', '#30220와'처럼 어색해지는 것을 막는다.
"""

import re
from typing import Any, Mapping, Optional, Tuple

PLACEHOLDER = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)(?:([|?])([^{}]*))?\}")
PARTICLE_MARKER = re.compile(r"\(으\)로|(을|를|이|가|은|는|과|와)\((을|를|이|가|은|는|과|와)\)")
PARTICLE_PAIRS = (("으로", "로"), ("을", "를"), ("이", "가"), ("은", "는"), ("과", "와"))
# 숫자·영문은 읽는 소리로 받침을 정한다(0 영, 1 일, 3 삼, 6 육, 7 칠, 8 팔 / L 엘, M 엠, N 엔, R 알).
BATCHIM_DIGITS, RIEUL_DIGITS = set("013678"), set("178")
BATCHIM_LETTERS, RIEUL_LETTERS = set("lmnr"), set("lr")


def final_sound(word: str) -> Optional[Tuple[bool, bool]]:
    """(받침 있음, ㄹ 받침). 닫는 괄호·따옴표는 건너뛴다: '[기준 이슈 ID]' → D."""
    stripped = re.sub(r"[\])}'\"”’\s]+$", "", word)
    if not stripped:
        return None
    char = stripped[-1]
    code = ord(char) - 0xAC00
    if 0 <= code <= 11171:
        jong = code % 28
        return jong != 0, jong == 8
    if char.isdigit():
        return char in BATCHIM_DIGITS, char in RIEUL_DIGITS
    if char.isascii() and char.isalpha():
        return char.lower() in BATCHIM_LETTERS, char.lower() in RIEUL_LETTERS
    return None


def attach_korean_particles(text: str) -> str:
    def replace(match: re.Match) -> str:
        marker, first, second = match.group(0), match.group(1), match.group(2)
        pair = PARTICLE_PAIRS[0] if marker == "(으)로" else next(
            (candidate for candidate in PARTICLE_PAIRS if first in candidate and second in candidate), None)
        if pair is None:
            return marker
        sound = final_sound(match.string[:match.start()])
        if sound is None:
            return pair[1]
        batchim, rieul = sound
        if pair[0] == "으로":
            return "으로" if batchim and not rieul else "로"
        return pair[0] if batchim else pair[1]

    return PARTICLE_MARKER.sub(replace, text)


def fill_template_question(question_template: str, values: Mapping[str, Any]) -> str:
    """값이 이미 정규화돼 있다고 보고 자리표시자를 채운 뒤 조사를 고른다."""

    def replace(match: re.Match) -> str:
        name, operator, operand = match.group(1), match.group(2), match.group(3) or ""
        value = str(values.get(name) or "")
        if operator == "|":
            return value or operand
        if operator == "?":
            return "" if value == operand else value
        return value

    return re.sub(r"\s+", " ", attach_korean_particles(PLACEHOLDER.sub(replace, question_template))).strip()
