#!/usr/bin/env python3
"""현재 Mantis 전체 저장본으로 검색용 구간 인덱스를 원자 구축한다."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

def main() -> None:
    from backend.environment import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    from backend.sources.mantis_chunk_index import build_index
    from backend.sources.mantis_hybrid import INDEX_PATH
    from backend.sources.mantis_store import connection_key, store_path
    result = build_index(store_path(), INDEX_PATH, connection_key())
    if result.get("source_advanced"):
        result = build_index(store_path(), INDEX_PATH, connection_key())
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
