#!/usr/bin/env python3
"""Benchmark structured Mantis scans against the legacy Chroma metadata scan."""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSISTANT_WEB_DIR = PROJECT_ROOT / "assistant_web"
if str(ASSISTANT_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(ASSISTANT_WEB_DIR))

from backend.common import normalize_chroma_issue_document  # noqa: E402
from backend.tools.mantis_index import INDEX_FILENAME, build_mantis_index  # noqa: E402
from backend.tools.mantis_snapshot import scan_mantis_documents  # noqa: E402
from backend.understanding import chroma_text_matches_constraints  # noqa: E402


def build_document(issue_id: int) -> str:
    year = 2025 if issue_id % 20 == 0 else 2026
    customer = "132SHGM(신한굿모닝증권)" if issue_id % 10 == 0 else "112KBStar(KB증권)"
    return f"""[Mantis 이슈 정보]
- 이슈 ID: #{issue_id}
- 프로젝트(고객사): {customer}
- 매체구분: MTS
- 화면번호: {9000 + issue_id % 100}
- 작업주체: 개발
- 현재 상태: 할당
- 담당자: 홍길동
- 요청자: 김요청
- 등록일: {year}-07-{issue_id % 28 + 1:02d}
- 최종수정일: {year}-07-{issue_id % 28 + 1:02d}

[제목]
구조화 인덱스 성능 측정 이슈 {issue_id}

[고객사 본문 요청 내용]
검색 결과가 기존 스냅샷 순회와 같은지 확인하기 위한 결정형 문서입니다.
"""


def percentile_95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=100, method="inclusive")[94]


def run_scan(chroma_path: Path, constraints: dict, mode: str) -> tuple[dict, float]:
    os.environ["MANTIS_INDEX_MODE"] = mode
    started = time.perf_counter()
    result = scan_mantis_documents(
        chroma_path,
        lambda document: chroma_text_matches_constraints(document, constraints),
        normalize_chroma_issue_document,
        example_limit=10,
        constraints=constraints,
    )
    return result, time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=100_000)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--minimum-speedup", type=float, default=5.0)
    args = parser.parse_args()
    documents = [build_document(issue_id) for issue_id in range(1, args.documents + 1)]
    constraints = {
        "customer_aliases": ["신한"],
        "created_at": {"from": "2025-01-01", "to": "2025-12-31"},
    }

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        chroma_path = root / "chroma.sqlite3"
        with closing(sqlite3.connect(chroma_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT)"
            )
            connection.executemany(
                "INSERT INTO embedding_metadata VALUES (?, 'chroma:document', ?)",
                ((index, document) for index, document in enumerate(documents, start=1)),
            )
        build_mantis_index(root / INDEX_FILENAME, documents)

        run_scan(chroma_path, constraints, "primary")  # validation-cache warm-up
        legacy_samples = []
        index_samples = []
        legacy_result = index_result = {}
        for _ in range(max(3, args.runs)):
            legacy_result, elapsed = run_scan(chroma_path, constraints, "off")
            legacy_samples.append(elapsed)
            index_result, elapsed = run_scan(chroma_path, constraints, "primary")
            index_samples.append(elapsed)

    if legacy_result != index_result:
        raise SystemExit("결과 불일치: 구조화 인덱스를 기본 경로로 전환할 수 없습니다.")
    legacy_p95 = percentile_95(legacy_samples)
    index_p95 = percentile_95(index_samples)
    speedup = legacy_p95 / max(index_p95, 1e-9)
    print(
        f"documents={args.documents} matches={legacy_result['matched_count']} "
        f"legacy_p95={legacy_p95:.4f}s index_p95={index_p95:.4f}s "
        f"speedup={speedup:.1f}x"
    )
    if speedup < args.minimum_speedup:
        raise SystemExit(
            f"성능 기준 미달: {speedup:.1f}x < {args.minimum_speedup:.1f}x"
        )


if __name__ == "__main__":
    main()
