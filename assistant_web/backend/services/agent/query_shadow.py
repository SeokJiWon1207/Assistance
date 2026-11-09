"""읽기 전용 건수·목록 비교. 평가 호출의 문맥에만 적용하고 사용자 응답 경로에는 전파하지 않는다."""
from contextlib import contextmanager
from contextvars import ContextVar
from html import escape
from typing import Iterator

from backend.tools.registry import ToolContext
from backend.tools.conditions import FIELD_LABELS
from backend.query_filters import filter_clause_label
from backend.intent.constants import KOREAN_ORDINAL_WORDS
from backend.utils.text import compact_text


QUERY_SHADOW = ContextVar("agent_query_shadow", default=False)


@contextmanager
def query_shadow_scope() -> Iterator[None]:
    token = QUERY_SHADOW.set(True)
    try:
        yield
    finally:
        QUERY_SHADOW.reset(token)


def result_text(tools: ToolContext) -> tuple[str, list[str]]:
    """원장에 남은 조회만 표현한다. 모델 초안의 수량·순번은 재사용하지 않는다."""
    results = tools.ledger.query_results()
    result = results[-1]
    total = result["total_count"]
    lines = ([f"적용 조건에 맞는 Mantis 이슈는 {total}건입니다."] if total is not None else
             [f"전체 조회가 끝나지 않았습니다. 확인한 범위에서 {result['observed_count']}건을 찾았습니다."])
    filters = [filter_clause_label(c).replace(c["field"], FIELD_LABELS.get(c["field"], c["field"]), 1)
               for c in result["applied_filters"]]
    if result.get("team_label") and result.get("team_scope") == "team":
        filters.append("담당자 범위 " + result["team_label"])
    elif result.get("team_scope") == "all":
        filters.append("전사 범위")
    if filters:
        lines.append("적용 조건 · " + " / ".join(filters))
    if result.get('population_limit'):
        lines.append(f"모집단: API 응답 순서의 최근 {result['population_limit']}건을 먼저 선택한 뒤 조건을 적용했습니다.")
    listed = next((item for item in reversed(results) if item["operation"] == "list"), None)
    ids = listed["ordered_ids"] if listed else []
    if listed and ids:
        field = {"created_at": "등록일", "updated_at": "수정일", "issue_id": "이슈 번호"}.get(
            listed["sort"]["field"], listed["sort"]["field"])
        direction = "오름차순" if listed["sort"]["direction"] == "asc" else "내림차순"
        lines.extend(["", f"{field} {direction}으로 확인한 {len(ids)}건입니다."])
        for position, issue_id in enumerate(ids, 1):
            row = tools.ledger.issue_row(issue_id) or {}
            title = escape(compact_text(str(row.get('summary') or '제목 없음'), 500), quote=False)
            lines.append(f"{position}. #{issue_id} {title}")
    if tools.related_ordinal is not None:
        target = tools.ordinal_issue()
        if target is None:
            label = next((word + " 번째" for word, ordinal in KOREAN_ORDINAL_WORDS.items()
                          if ordinal == tools.related_ordinal), "요청한 순번의")
            if listed and total is not None and len(ids) == total:
                lines.append(f"\n목록에 {label} 이슈가 없어 연결할 Notion 가이드가 없습니다.")
            else:
                lines.append(f"\n{label} 이슈를 확인할 수 없어 관련 가이드를 특정하지 못했습니다.")
        else:
            lines.append(f"\n관련 문서를 찾을 대상은 #{target}입니다.")
            if not tools.has_related_guide():
                lines.append("확인한 범위에서 이 이슈와 관련된 가이드 본문을 확보하지 못했습니다.")
    return "\n".join(lines), [f"M#{issue_id}" for issue_id in ids]
