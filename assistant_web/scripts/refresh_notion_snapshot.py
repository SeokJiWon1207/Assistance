#!/usr/bin/env python3
"""잠금·용량 점검 후 전수 검색 저장본을 갱신하고 중단한 작업을 재개한다."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import shutil
import sys

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
from backend.environment import load_dotenv
load_dotenv(APP_DIR / '.env')
from backend.config import RUNTIME_DIR, get_notion_settings
from backend.notion.body_index import open_body_index
from backend.notion.complete_snapshot import coverage
from backend.notion.read_scope import resolve_notion_read_scope, refresh_notion_read_scope, save_complete_notion_read_scope
from backend.notion.snapshot_maintenance import refresh_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-seconds', type=float, default=3600)
    parser.add_argument('--max-pages', type=int, default=1000)
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.max_seconds <= 0 or args.max_pages <= 0:
        parser.error('시간과 페이지 수는 양수여야 합니다.')
    with (RUNTIME_DIR / 'notion-snapshot.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'status': 'already_running'}))
            return 0
        settings = get_notion_settings()
        roots = settings['database_ids']
        scope, complete = resolve_notion_read_scope(roots, settings['token'])
        index = open_body_index()
        if index is None:
            raise RuntimeError('body_index_unavailable')
        if not args.status:
            # Mantis의 다음 원자 갱신 공간도 남긴다. 작업마다 임시 복제본을 만들지 않는다.
            mantis = RUNTIME_DIR / 'mantis_complete.sqlite3'
            size = mantis.stat().st_size if mantis.exists() else 0
            required = max(256 * 1024**2, size + size // 4 + 160 * 1024**2)
            if shutil.disk_usage(RUNTIME_DIR).free < required:
                raise RuntimeError('insufficient_disk_space')
            from backend.notion.complete_snapshot import _time
            try:
                saved_scope = json.loads((RUNTIME_DIR / 'notion_read_scope.json').read_text())
                scope_age = datetime.now(timezone.utc).timestamp() - _time(saved_scope.get('generated_at', ''))
            except (OSError, ValueError):
                scope_age = 86400
            if not complete or not 0 <= scope_age < 43200:
                discovered, discovered_complete = refresh_notion_read_scope(roots, settings['token'])
                if discovered_complete:
                    scope, complete = discovered, True
                    save_complete_notion_read_scope(roots, scope, datetime.now(timezone.utc).isoformat())
                else:
                    raise RuntimeError('read_scope_refresh_incomplete')
        proof = coverage(index, list(scope))
        if not args.status:
            # 새 문서·속성 변경은 전체 목록으로만 드러나므로 매 실행 목록부터 갱신한다.
            proof = refresh_snapshot(index, list(scope), max_seconds=args.max_seconds, max_pages=args.max_pages,
                                     refresh_catalog=True)
        proof['complete'] = bool(complete and proof['complete'])
        summary = {key: value for key, value in proof.items() if key != 'pending_page_ids'}
        print(json.dumps(summary, ensure_ascii=False))
        if not args.status:
            target = RUNTIME_DIR / 'notion-snapshot-status.json'
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
            temp.chmod(0o600)
            temp.replace(target)
        return 0 if proof['complete'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error_type': type(exc).__name__,
                          'reason': str(exc) if isinstance(exc, RuntimeError) else 'snapshot_refresh_failed'}))
        raise SystemExit(1)
