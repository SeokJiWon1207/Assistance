"""전달일 집계용 순회. API 페이지 순서를 전달일 순서로 가정하지 않는다."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone

from backend.execution import checkpoint
from backend.sources.mantis_collection import collection_metadata, iter_pages_in_order


@contextmanager
def effort_issues(fetch_page, workers):
    from backend.sources.mantis_store import stream_complete_store
    with stream_complete_store() as stored:
        if stored is not None:
            yield iter(stored), collection_metadata(stored)
            return
        meta = {'search_complete': False, 'queriedAt': datetime.now(timezone.utc).isoformat(),
                'incomplete_reasons': ['bounded_live_scan']}
        with closing(iter_pages_in_order(
            lambda page: fetch_page({'page_size': 100, 'page': page}), max_pages=50, workers=workers,
        )) as pages:
            def rows():
                seen = set()
                duplicate = False
                for _, batch in pages:
                    checkpoint()
                    if not batch:
                        meta.update(search_complete=not duplicate,
                                    incomplete_reasons=['duplicate_live_pages'] if duplicate else [])
                        break
                    for row in batch:
                        identity = row.get('id')
                        if identity in seen:
                            duplicate = True
                            continue
                        seen.add(identity)
                        yield row
            yield rows(), meta


def coverage_note(meta):
    return ('\n\n일부 이슈만 조회되어 공수 합계가 누락될 수 있습니다. 전체 저장본 갱신 후 다시 조회해 주세요.'
            if not meta.get('search_complete') else '')
