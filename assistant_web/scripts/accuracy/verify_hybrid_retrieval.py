#!/usr/bin/env python3
"""고정한 실제 원본·질문으로 기존/결합/구간 재순위화의 알려진 정답 검색률을 비교한다.

cases JSON: [{"query": "...", "expected_ids": [123], "constraints": {}, "cohort": "tail"}]
기대 이슈는 검색 결과를 보기 전에 고정한다. 미표기 관련 이슈의 정답 여부는 평가하지
않으므로 결과는 전체 Recall이나 답변 사실 정확도가 아닌 known-target Hit@5/MRR@5다.
회사 모델·Mantis 실시간 API는 호출하지 않고 설정된 로컬 임베딩 서버만 사용한다.
"""
import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
from time import perf_counter

APP_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))


def ranking_metrics(ids: list[int], expected: list[int], k: int = 5) -> dict:
    ranked = list(dict.fromkeys(ids))[:k]
    position = next((i for i, issue_id in enumerate(ranked, 1) if issue_id in expected), None)
    return {"hit_at_5": int(position is not None), "mrr_at_5": 1 / position if position else 0.0,
            "target_rank": position, "top_ids": ranked}


def summarize(rows: list[dict]) -> dict:
    result = {}
    for mode in ("legacy", "fusion", "reranked"):
        values = [row[mode] for row in rows]
        times = sorted(value["seconds"] for value in values)
        result[mode] = {"cases": len(values), "hits": sum(value["hit_at_5"] for value in values),
                        "hit_at_5": statistics.mean(value["hit_at_5"] for value in values),
                        "mrr_at_5": statistics.mean(value["mrr_at_5"] for value in values),
                        "median_seconds": statistics.median(times),
                        "p95_seconds": times[math.ceil(0.95 * len(times)) - 1]}
    return result


def backup(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    from backend.environment import load_dotenv
    load_dotenv(APP_DIR / ".env")
    from backend.sources.chroma import _rank_legacy_documents_semantically
    from backend.sources.mantis_hybrid import INDEX_PATH, search_hybrid
    from backend.sources.mantis_chunk_index import build_index
    from backend.sources.mantis_store import connection_key, store_path
    from backend.config import CHROMA_DB_PATH
    cases_bytes = args.cases.read_bytes()
    cases = json.loads(cases_bytes)
    if not cases or any(not row.get("query") or not row.get("expected_ids") for row in cases):
        raise ValueError("검색 전에 고정한 질문과 기대 이슈가 필요합니다.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix="hybrid-eval-", dir=args.output.parent) as directory:
        source, target = Path(directory) / "source.sqlite3", Path(directory) / "chunks.sqlite3"
        backup(store_path(), source)
        if INDEX_PATH.exists():
            backup(INDEX_PATH, target)
        index_stats = build_index(source, target, connection_key())
        with closing(sqlite3.connect(source)) as raw_db:
            for case in cases:
                for issue_id in case["expected_ids"]:
                    if raw_db.execute("SELECT 1 FROM issues WHERE id=?", [issue_id]).fetchone() is None:
                        raise ValueError(f"기대 이슈 {issue_id}가 고정 원본에 없습니다.")
        for number, case in enumerate(cases, 1):
            query, constraints = case["query"], case.get("constraints", {})
            started = perf_counter()
            legacy, legacy_meta = _rank_legacy_documents_semantically(query, 5, constraints)
            legacy_seconds = perf_counter() - started
            started = perf_counter()
            result = search_hybrid(query, 5, constraints, lambda *_: (legacy, legacy_meta),
                                   source=source, target=target, identity=connection_key())
            elapsed = perf_counter() - started
            if result is None:
                raise RuntimeError("고정 저장본의 구간 인덱스를 사용할 수 없습니다.")
            docs, meta = result
            fusion = sorted(docs, key=lambda doc: doc["fusion_rank"])
            row = {"query": query, "expected_ids": case["expected_ids"], "constraints": constraints,
                   "cohort": case.get("cohort", "unspecified"), "metadata": meta,
                   "legacy_metadata": legacy_meta}
            for name, ranked, seconds in (
                ("legacy", legacy, legacy_seconds),
                ("fusion", fusion, legacy_seconds + max(0, elapsed - meta["reranking_ms"] / 1000)),
                ("reranked", docs, legacy_seconds + elapsed),
            ):
                row[name] = {**ranking_metrics([doc["issue_id"] for doc in ranked], case["expected_ids"]),
                             "seconds": seconds}
            rows.append(row)
            print(json.dumps({"case": number, "total": len(cases), "hits": {
                name: row[name]["hit_at_5"] for name in ("legacy", "fusion", "reranked")},
                "reranking": meta["reranking"]}), flush=True)
        report = {"metric": "known-target Hit@5 / MRR@5 (not exhaustive relevance or answer accuracy)",
                  "case_sha256": hashlib.sha256(cases_bytes).hexdigest(), "index": index_stats,
                  "legacy_index_mtime_ns": CHROMA_DB_PATH.stat().st_mtime_ns,
                  "timing_note": "기존 벡터 결과를 동일하게 재사용하며 해당 소요시간을 결합·재순위화 시간에 더함. fusion 시간은 재순위화 구간을 뺀 추정치.",
                  "data_note": "변경 전 운영 Chroma 인덱스와 고정한 최신 원본의 구간 인덱스 비교. 데이터 포괄성 개선 효과도 포함됨.",
                  "summary": summarize(rows), "cohorts": {
                      cohort: summarize([row for row in rows if row["cohort"] == cohort])
                      for cohort in sorted({row["cohort"] for row in rows})}, "results": rows}
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
