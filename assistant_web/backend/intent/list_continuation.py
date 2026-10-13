"""목록 확장 요청은 검색 주제가 아니라 서버 조회 기록에 연결된 표시 동작이다."""
import re

# 독립된 표시 요청만 처리한다. 제목/본문 속 인용문이나 조건 변경을 페이지로 해석하지 않는다.
CONTINUATION = re.compile(
    r"(?:(?:그럼|그러면|이어서)\s*)?"
    r"(?P<kind>나머지(?:도|를|는)?|더|계속|다음)\s*"
    r"(?:(?:이슈|목록|결과)(?:도|를|는)?\s*)?"
    r"(?:(?:모두|전부|전체)|\d+\s*건(?:만|씩)?)?\s*"
    r"(?:보여\s*(?:줘|주세요)|알려\s*(?:줘|주세요))[.!?]*\s*$"
)


def list_continuation_mode(question: str) -> str:
    match = CONTINUATION.fullmatch(question.strip())
    return ("rest" if match["kind"].startswith("나머지") else "more") if match else ""


def remove_list_continuation_request(text: str) -> str:
    match = CONTINUATION.search(text)
    if match and (match.start() == 0 or text[match.start() - 1].isspace()):
        return text[:match.start()]
    return text


def confirmed_list_continuation(question: str, prior_plan: dict, sources: list) -> dict:
    """동일 사용자/직전 질문 검증을 통과한 감사 기록만 호출자가 전달한다."""
    constraints = prior_plan.get("resolved_constraints") or {}
    cursor = constraints.get("list_cursor") or {}
    seen = set(cursor.get("previous_seen_ids") or [])
    seen.update(s.get("issue_id") for s in sources if s.get("type") in {"mantis", "chroma"})
    seen = {int(i) for i in seen if str(i).isdigit() and int(i) > 0}
    return {"mode": list_continuation_mode(question), "seen_ids": sorted(seen),
            "page_size": max(1, min(20, int(cursor.get("page_size") or prior_plan.get("limit") or 5)))}


def continuation_execution_constraints(constraints: dict) -> dict:
    continuation = constraints.get("list_continuation") or {}
    if not continuation or not constraints.get("inherited_from_audit_id"):
        return constraints
    return {**constraints, "exclude_issue_ids": sorted(set(
        [*(constraints.get("exclude_issue_ids") or []), *continuation.get("seen_ids", [])]
    ))}
