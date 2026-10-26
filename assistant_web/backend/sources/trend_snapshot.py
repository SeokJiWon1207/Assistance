"""월별 집계는 검색 인덱스가 아니라 검증된 전체 원본 저장본을 사용한다."""
import sqlite3
from contextlib import closing, contextmanager

from backend.execution import checkpoint
from backend.sources.mantis import mantis_issue_effort_date, normalize_issue
from backend.sources.mantis_collection import collection_metadata
from backend.sources.mantis_store import render_document


@contextmanager
def trend_rows(index_path, start_date):
    # 명시 경로는 회귀 테스트·오프라인 점검 전용이다. 운영에서 자동 대체하지 않는다.
    if index_path is not None:
        with closing(sqlite3.connect(f'file:{index_path}?mode=ro', uri=True)) as db:
            index_rows = db.execute('''SELECT i.created_at,i.issue_type,i.document FROM issues i
                LEFT JOIN issue_dates d ON d.issue_id=i.issue_id AND d.field_name='고객사전달일'
                WHERE i.created_at>=? OR d.date_value>=?''', (start_date, start_date)).fetchall()
        yield ((created, kind, document, None) for created, kind, document in index_rows), {
            'search_complete': False, 'incomplete_reasons': ['offline_index'], 'status': 'snapshot'}
        return
    from backend.sources.mantis_store import stream_complete_store
    with stream_complete_store() as stored:
        if stored is None:
            raise LookupError('최신 전체 저장본을 확인할 수 없습니다. 동기화 상태를 확인한 뒤 다시 조회해 주세요.')
        meta = collection_metadata(stored)
        if meta.get('search_complete') is not True:
            raise LookupError('최신 전체 저장본의 조회 범위를 확인할 수 없습니다.')

        def rows():
            for raw in stored:
                checkpoint()
                created = str(raw.get('created_at') or '')[:10]
                if created < start_date and mantis_issue_effort_date(raw) < start_date:
                    continue
                normalized = normalize_issue(raw)
                yield normalized['created_at'], normalized['severity_label'] or normalized['severity'], render_document(raw), normalized
        yield rows(), meta
