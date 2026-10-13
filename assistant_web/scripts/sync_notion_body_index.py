#!/usr/bin/env python3
"""허용된 Notion DB의 전체 본문 색인을 중단·재개 가능한 배치로 갱신한다."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from backend.environment import load_dotenv

load_dotenv(APP_DIR / ".env")

from backend.notion.body_index import BodyIndexError
from backend.notion.body_index_sync import notion_body_index_status, sync_notion_body_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-id", action="append", help="설정된 읽기 범위의 DB ID. 여러 번 지정 가능")
    parser.add_argument("--max-pages", type=int, default=100, help="이번 배치에서 읽을 본문 수. 0은 목록만 갱신")
    parser.add_argument("--max-seconds", type=float, default=300, help="목록과 본문을 포함한 실행 시간 한도")
    parser.add_argument("--resume-catalog", action="store_true", help="24시간 내 완료된 저장 목록에서 본문 배치만 재개")
    parser.add_argument("--refresh-content", action="store_true", help="수정 시각이 같은 페이지도 전체 본문을 다시 수집")
    parser.add_argument("--status", action="store_true", help="원격 요청 없이 현재 저장된 색인 진행률만 조회")
    args = parser.parse_args()
    try:
        stats = (notion_body_index_status(args.database_id) if args.status else
                 sync_notion_body_index(args.database_id, max_pages=args.max_pages, max_seconds=args.max_seconds,
                                        refresh_catalog=not args.resume_catalog, refresh_content=args.refresh_content))
    except ValueError:
        print(json.dumps({"status": "error", "error": "invalid_configuration_or_arguments"}))
        return 2
    except BodyIndexError:
        print(json.dumps({"status": "error", "error": "body_index_storage_failure"}))
        return 1
    print(json.dumps(stats, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
