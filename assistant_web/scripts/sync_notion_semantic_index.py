#!/usr/bin/env python3
"""저장된 Notion 본문 구간의 로컬 임베딩 색인을 증분 갱신한다."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from backend.environment import load_dotenv
load_dotenv(APP_DIR / '.env')

import requests
from backend.notion.body_index import BodyIndexError
from backend.notion.semantic_index import sync_semantic_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-chunks', type=int, default=256)
    parser.add_argument('--max-seconds', type=float, default=60)
    args = parser.parse_args()
    if args.max_chunks < 0 or args.max_seconds <= 0:
        parser.error('max-chunks는 0 이상, max-seconds는 양수여야 합니다.')
    try:
        result = sync_semantic_index(max_chunks=args.max_chunks, max_seconds=args.max_seconds)
    except (BodyIndexError, sqlite3.Error, OSError, ValueError, KeyError, requests.RequestException):
        print(json.dumps({'status': 'error', 'reason': 'semantic_index_update_failed'}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
