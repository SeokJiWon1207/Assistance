#!/usr/bin/env python3
"""모델/임베딩 호출 없이 Mantis 원본 전체를 동기화한다."""
import json
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
from backend.environment import load_dotenv  # noqa: E402
load_dotenv(APP_DIR / '.env')
from backend.sources.mantis_store import sync_complete_store  # noqa: E402

if __name__ == '__main__':
    try:
        print(json.dumps(sync_complete_store(), ensure_ascii=False))
    except Exception as exc:
        print(f"Mantis 동기화 실패: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
