"""질문에 명시한 Notion URL을 현재 읽기 범위 안에서 직접 조회한다."""
import re
import time
from backend.execution import CURRENT_BUDGET, checkpoint
from backend.notion.page_links import page_links
from backend.notion.discovery import resolve_notion_settings
from backend.notion.read_scope import resolve_notion_read_scope
from backend.notion.semantic_search import read_candidate_page, read_current_page
from backend.notion.crawl_transport import CrawlBudget


def notion_page_ids(text: str) -> list[str]:
    urls = re.findall(r'https?://[^\s<>"\)]+', text)
    refs = page_links({'type': 'paragraph', 'paragraph': {'rich_text': [{'href': url} for url in urls]}})
    return list(dict.fromkeys(item['page_id'] for item in refs))


def read_explicit_pages(identifiers: list[str], constraints: dict) -> dict:
    pages: list[dict] = []
    unavailable = len(identifiers) > 4
    try:
        settings = resolve_notion_settings()
        scope, complete = resolve_notion_read_scope(settings['database_ids'], settings['token'])
        if not complete:
            raise RuntimeError('read_scope_unavailable')
        for identifier in identifiers[:4]:
            checkpoint()
            try:
                budget = CURRENT_BUDGET.get()
                deadline = min(time.monotonic() + 15, budget.deadline if budget else float('inf'))
                page = read_candidate_page({'page_id': identifier}, scope, constraints, CrawlBudget(deadline, 20))
                if page is None:
                    continue
                current = read_current_page({'page_id': identifier, 'database_id': page['database_id']}, scope, constraints, deadline=deadline)
                # 본문을 확인할 수 없으면 현재 속성만 전달하고 부분 조회를 표시한다.
                if current is None:
                    page['content'] = ''
                    page['content_incomplete'] = True
                    unavailable = True
                pages.append(current or page)
            except Exception:
                unavailable = True
    except Exception:
        unavailable = True
    checkpoint()
    return {'source': 'notion', 'status': 'partial' if unavailable and pages else 'error' if unavailable else 'ok',
            'notion_pages': pages, 'meta': {'search_complete': not unavailable,
                'explicit_page_ids': identifiers[:4], 'content_complete': not unavailable and all(not p.get('content_incomplete') for p in pages)}}
