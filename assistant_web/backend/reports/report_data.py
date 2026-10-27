"""주간·월간 보고서처럼 여러 지표를 한 번에 세는 보고서의 Mantis 원본.

미종료·장기 적체는 몇 년 전에 등록된 이슈까지 세야 한다. 최근 N건으로 자르면
장기 미종료 템플릿(전체 저장본)과 숫자가 어긋나므로 전체 저장본을 우선 쓰고,
저장본이 없을 때만 최근 등록분을 실시간으로 가져오되 범위가 좁다는 것을 밝힌다.
"""

from typing import Any, Callable, Dict, List
from dataclasses import dataclass

from backend.display import korean_time
from backend.sources.mantis_collection import IssueBatch, collection_metadata

LIVE_FALLBACK_LIMIT = 1000


@dataclass
class ReportIssues:
    """행을 복제하지 않고 보고서용 범위 정보만 덧붙이는 재순회 뷰."""
    rows: Any
    meta: Dict[str, Any]

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)


def load_report_issues(
    read_store: Callable[..., Any],
    fetch_recent: Callable[..., List[Dict[str, Any]]],
) -> ReportIssues | IssueBatch:
    """최신 저장본 → 갱신이 지연된 저장본 → 최근 등록분 실시간 순으로 원본을 고른다.

    보고서 모듈의 이름을 넘겨받아 테스트가 각 모듈의 조회 함수를 바꿔 끼울 수 있게 한다.
    """
    stored = read_store(summary_only=True)
    if stored is None:
        stored = read_store(allow_stale=True, summary_only=True)
    if stored is not None:
        return ReportIssues(stored, {**collection_metadata(stored), "coverage": "complete_store"})
    rows = fetch_recent(total_limit=LIVE_FALLBACK_LIMIT)
    meta = {**collection_metadata(rows), "coverage": "recent_live", "search_complete": False}
    return IssueBatch(list(rows), meta)


def report_scope_label(rows: ReportIssues | IssueBatch) -> str:
    """보고서 머리글의 '기준' 뒤에 붙는 데이터 범위 설명."""
    meta = collection_metadata(rows)
    count = f"{len(rows):,}건"
    if meta.get("coverage") == "complete_store":
        if meta.get("search_complete") is False and meta.get("fetchedAt"):
            return f"Mantis 전체 {count}(갱신 지연 · {korean_time(meta['fetchedAt'])} 저장분)"
        return f"Mantis 전체 {count} 기준"
    return f"최근 등록 {count}만 확인 — 미종료·장기 적체가 실제보다 적을 수 있습니다"
