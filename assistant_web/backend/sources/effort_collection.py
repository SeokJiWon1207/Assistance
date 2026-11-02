"""전달일 집계용 순회. API 페이지 순서를 전달일 순서로 가정하지 않는다."""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone

from backend.execution import checkpoint
from backend.sources.mantis_collection import collection_metadata, iter_pages_in_order


@contextmanager
def effort_issues(fetch_page, workers):
    from backend.sources.mantis_store import read_store_metadata, stream_complete_store
    with ExitStack() as stack:
        stored = stack.enter_context(stream_complete_store())
        # 갱신 지연과 이슈 누락은 다르다. 전체 저장본을 5,000건 라이브 목록으로
        # 대체하면 오래전에 수정된 이슈의 전달 공수가 합계에서 사라진다.
        if stored is None and read_store_metadata(allow_stale=True):
            stored = stack.enter_context(stream_complete_store(allow_stale=True))
        if stored is not None:
            meta = collection_metadata(stored)
            meta['aggregation_complete'] = bool(meta.get('snapshot_complete') or meta.get('search_complete'))
            if meta.get('snapshot_complete') and not meta.get('search_complete'):
                meta['freshness_warning'] = '저장 시각 기준 전체 집계입니다. 최신 변경은 아직 반영되지 않았을 수 있습니다.'
            yield iter(stored), meta
            return
        meta = {'search_complete': False, 'queriedAt': datetime.now(timezone.utc).isoformat(),
                'incomplete_reasons': ['bounded_live_scan']}
        with closing(iter_pages_in_order(
            lambda page: fetch_page({'page_size': 100, 'page': page}), max_pages=1000, workers=workers,
        )) as pages:
            def rows():
                seen: set[int | str | None] = set()
                duplicate = False
                for _, batch in pages:
                    checkpoint()
                    if not batch:
                        meta.update(search_complete=not duplicate,
                                    incomplete_reasons=['duplicate_live_pages'] if duplicate else [])
                        break
                    previous_count = len(seen)
                    for row in batch:
                        identity = row.get('id')
                        if identity in seen:
                            duplicate = True
                            continue
                        seen.add(identity)
                        yield row
                    if len(seen) == previous_count:
                        meta['incomplete_reasons'] = ['duplicate_live_pages']
                        break
            yield rows(), meta


def effort_chart_coverage(meta):
    """합계의 완전성과 저장본의 최신성을 구분해 화면에 전달한다."""
    return {
        'complete': bool(meta.get('aggregation_complete', meta.get('search_complete', False))),
        'asOf': meta.get('fetchedAt') or meta.get('queriedAt') or '',
        'asOfLabel': '저장 시각' if meta.get('status') == 'snapshot' else '조회 시각',
        'freshnessWarning': meta.get('freshness_warning', ''),
    }


def coverage_note(meta):
    if meta.get('aggregation_complete') and meta.get('freshness_warning'):
        return f"\n\n저장 시각: {meta.get('fetchedAt', '')} · {meta['freshness_warning']}"
    return ('\n\n일부 이슈만 조회되어 공수 합계가 누락될 수 있습니다. 전체 저장본 갱신 후 다시 조회해 주세요.'
            if not meta.get('search_complete') else '')
