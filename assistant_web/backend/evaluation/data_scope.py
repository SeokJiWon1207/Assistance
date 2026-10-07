"""답변에 사용한 데이터 시점과 검색 범위를 확인된 메타데이터로 표시한다."""

from typing import Any


def build_answer_data_scope(retrieval: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for stage in (retrieval or {}).get("stages", []):
        source = str(stage.get("source") or "")
        if source.startswith("mantis"):
            label = "Mantis"
        elif source == "notion":
            label = "Notion"
        elif source in {"chroma", "vector", "vector_db"}:
            label = "문서 검색"
        else:
            continue
        meta = stage.get("meta") or {}
        as_of = meta.get("fetchedAt") or meta.get("snapshot_as_of") or meta.get("queriedAt") or ""
        snapshot = meta.get("status") == "snapshot" or "snapshot" in source or "snapshot" in str(meta.get("tool") or "")
        coverage = "범위 미확인"
        if stage.get("status") == "error":
            coverage = "조회 실패"
        elif meta.get("search_complete") is False:
            coverage = "부분 조회"
        elif meta.get("search_complete") is True:
            coverage = "선택 범위 조회 완료"
        unapplied = (meta.get("filter_audit") or {}).get("unapplied_filters") or []
        if unapplied:
            coverage = "부분 조회"
        notes = []
        if coverage == "부분 조회":
            notes.append("표시한 건수는 확인된 범위 기준입니다.")
        if meta.get("content_complete") is False:
            notes.append("일부 문서 본문을 확인하지 못했습니다.")
        if unapplied:
            notes.append("일부 검색 조건을 적용하지 못했습니다.")
        if meta.get("query_cache_hit"):
            notes.append(f"동일 제목 조회 재사용 · 최대 {meta.get('query_cache_age_seconds', 0):g}초 전")
        if meta.get("freshness_warning"):
            notes.append(str(meta["freshness_warning"]))
        database_count = len(meta.get("database_ids") or []) if label == "Notion" else 0
        rows.append({
            "source": label,
            "basis": "저장본" if snapshot else "조회 결과",
            "asOf": str(as_of),
            "asOfLabel": "저장 시각" if snapshot else "조회 시각",
            "coverage": coverage,
            "databaseCount": database_count,
            "warning": coverage in {"부분 조회", "조회 실패", "범위 미확인"} or meta.get("content_complete") is False,
            "notes": list(dict.fromkeys(notes)),
        })
    # 동일 출처가 여러 검색 단계를 거쳤으면 각 범위를 유지하되 같은 표시만 합친다.
    return [row for index, row in enumerate(rows) if row not in rows[:index]]
