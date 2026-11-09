"""기존 원천 조회를 조합한다. 검색 후보와 현재 본문 근거를 구분한다."""
import hashlib
import json
import os
import re
import time
from functools import partial
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit
from fastapi import HTTPException

from backend.config import RUNTIME_DIR, get_mantis_settings
from backend.execution import ExecutionBudget, execution_scope, run_with_deadline
from backend.notion.discovery import resolve_notion_settings
from backend.notion.read_scope import resolve_notion_read_scope, READ_SCOPE_FILENAME
from backend.notion.read_api import fetch_notion_users
from backend.notion.search import execute_notion_search
from backend.notion.semantic_search import read_candidate_page, read_current_page
from backend.notion.retrieval_candidates import hybrid_candidates
from backend.notion.crawl_transport import CrawlBudget, NotionReadFailure
from backend.settings.notion import get_configured_notion_settings
from backend.sources.mantis_client import fetch_mantis_issue_by_id
from backend.sources.mantis_fields import normalize_issue
from backend.storage.workspace_rows import _issue_summary
from backend.reports.team_brief import _priority_content
from backend.workspace import user_mantis_name, _matches_person
from backend.workspace_data import load_complete_workspace
from backend.utils.text import normalize_for_match
from backend.suggestions.contracts import Candidate, EvidenceBatch, Relation, SourceRef
from backend.suggestions.settings import CACHE_SECONDS, POLICY_VERSION


class SourceRevoked(Exception):
    """현재 접근·관련성·원본 버전이 바뀌어 이전 추천을 사용할 수 없다."""


class SourceUnavailable(Exception):
    """일시적 장애는 원천 삭제 또는 권한 철회로 단정하지 않는다."""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def current_scope_key(user: dict) -> str:
    # GET 경로는 discovery/원격 scope 확인을 호출하지 않는다.
    notion = get_configured_notion_settings()
    saved_scope = {}
    try:
        saved = json.loads((RUNTIME_DIR / READ_SCOPE_FILENAME).read_text())
        saved_scope = saved.get('read_scope', {})
    except (OSError, ValueError, TypeError):
        pass
    return digest([user.get('uid'), user.get('displayName'), user.get('mantisUsername'),
                   user.get('team'), user.get('role'),
                   [os.getenv('MANTIS_API_URL', ''), os.getenv('MANTIS_API_TOKEN', '')],
                   notion, saved_scope, os.getenv('NOTION_INCLUDE_CHILD_DATABASES', 'false'),
                   os.getenv('NOTION_FETCH_PAGE_BLOCKS', 'true'), POLICY_VERSION])


def _time(value: str, default: datetime) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return default


def notion_links(text: str) -> list[str]:
    result = []
    for url in re.findall(r'https?://[^\s<>"\)]+', text):
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if not any(host == domain or host.endswith('.' + domain) for domain in ('notion.so', 'notion.site', 'notion.com')):
            continue
        match = re.search(r'([a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$', parsed.path.rstrip('/'))
        if match:
            result.append(match[1].replace('-', '').lower())
    return list(dict.fromkeys(result))


def _owned(issue: dict, user: dict) -> bool:
    return _matches_person(str(issue.get('handler') or ''), user_mantis_name(user))


def _issue_revision(issue: dict) -> str:
    # summary_only 저장본과 상세 API가 공통으로 가진 필드만 사용한다.
    # 본문/노트 변경은 날짜를 자르지 않은 updated_at으로 감지한다.
    return digest([issue.get(k) for k in ('id', 'summary', 'status', 'handler', '_sort_values', 'date_fields',
                                         'priority', 'severity')])


def _issue_candidate(issue: dict, now: datetime, meta: dict) -> Candidate:
    item = _priority_content(_issue_summary(issue), now.astimezone().date())
    identifier = str(issue['id'])
    settings = get_mantis_settings()
    parsed = urlsplit(str(settings.get('url') or ''))
    base = urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path.split('/api/')[0].rstrip('/'), '', ''))
    source = SourceRef(type='mantis', id=identifier, title=str(issue.get('summary') or '이슈')[:200],
        url=f'{base}/view.php?id={identifier}', revision=_issue_revision(issue),
        observedAt=_time(meta.get('fetchedAt', ''), now),
        validUntil=min(now + timedelta(seconds=CACHE_SECONDS), _time(meta.get('cacheUntil', ''), now + timedelta(seconds=CACHE_SECONDS))))
    return Candidate(id='mantis:' + identifier, sources=[source], dedupeKey='mantis:' + identifier,
        revision=source.revision, priority=int(item.get('riskScore', 0)), relevance=2,
        facts={'reason': 'overdue' if item['overdue'] else 'aging' if (item.get('ageDays') or 0) >= 30 else 'progress',
               'notionIds': [], 'topic': str(issue.get('summary') or '')[:200]},
        allowedActions=['review_progress', 'prepare_checklist'])


def _collect_mantis(user: dict, now: datetime, batch: EvidenceBatch) -> None:
    if not user_mantis_name(user):
        batch.sourceStatus['mantis'] = 'unlinked'
        return
    loaded = load_complete_workspace(predicate=lambda row: _owned(row, user))
    if not loaded or not loaded[1].get('search_complete'):
        batch.sourceStatus['mantis'] = 'unavailable'
        return
    rows, meta = loaded
    candidates = [_issue_candidate(row, now, meta) for row in rows if _issue_summary(row)['isOpen']]
    candidates.sort(key=lambda item: (-item.priority, item.id))
    batch.candidates.extend(candidates[:12])
    batch.sourceStatus['mantis'] = 'ok'
    # 기존 관찰 규칙으로 최초 관찰과 동일 세대를 제외한다.
    from backend.workspace_notifications import collect_workspace_events
    from backend.storage.workspace_schema import init_workspace_db, workspace_db_path
    init_workspace_db()
    events, _ = collect_workspace_events(workspace_db_path(), user['uid'], user_mantis_name(user), rows, meta)
    new_ids = {str(event.get('issueId')) for event in events if event.get('type') == 'note'
               and _time(event.get('observedAt', ''), datetime.min.replace(tzinfo=timezone.utc)) >= now - timedelta(days=7)}
    for candidate in candidates[:12]:
        if candidate.sources[0].id in new_ids:
            candidate.facts['reason'] = 'new_note'
            candidate.kind = 'summarize_changes'
            candidate.allowedActions.append('summarize_changes')
            candidate.priority += 2
    for candidate in candidates[:4]:
        try:
            raw = fetch_mantis_issue_by_id(int(candidate.sources[0].id))
            if raw is None:
                batch.candidates.remove(candidate)
                continue
            issue = normalize_issue(raw)
            if not _owned(issue, user) or not _issue_summary(issue)['isOpen']:
                batch.candidates.remove(candidate)
                continue
            refreshed = _issue_candidate(issue, now, meta)
            candidate.sources = refreshed.sources
            candidate.revision = refreshed.revision
            candidate.priority = refreshed.priority + (2 if candidate.kind == 'summarize_changes' else 0)
            if candidate.kind != 'summarize_changes':
                candidate.facts['reason'] = refreshed.facts['reason']
            candidate.facts['topic'] = refreshed.facts['topic']
            texts = [str(raw.get('description') or ''), str(raw.get('additional_information') or ''),
                     *[str(note.get('text') or '') for note in raw.get('notes') or []]]
            candidate.facts['notionIds'] = notion_links('\n'.join(texts))[:12]
        except Exception as exc:
            # 속성 후보는 유효하다. 읽지 못한 본문을 완료 근거로 사용하지 않는다.
            if isinstance(exc, HTTPException) and exc.status_code in {401, 403, 404}:
                batch.candidates.remove(candidate)
            batch.sourceStatus['mantis'] = 'partial'


def notion_owner(user: dict) -> tuple[str, str]:
    name = str(user.get('displayName') or '').strip()
    if not name or '@' in name:
        return '', ''
    matches = [(n, uid) for n, uid in fetch_notion_users() if normalize_for_match(n) == normalize_for_match(name)]
    return matches[0] if len(matches) == 1 else ('', '')


def _page_revision(page: dict, *, include_body: bool = True) -> str:
    body = ([(s.get('block_id'), s.get('text')) for s in page.get('_owned_segments') or []]
            if include_body and page.get('_verified_body') and not page.get('content_incomplete') else [])
    return digest([[page.get(k) for k in ('id', 'last_edited_time_raw', 'title', 'status', 'owner_user_ids', 'due_date')], body])


def page_candidate(page: dict, now: datetime, *, relation: Relation) -> Candidate:
    identifier = str(page['id']).replace('-', '')
    complete = bool(page.get('_verified_body') and not page.get('content_incomplete'))
    source = SourceRef(type='notion', id=identifier, title=str(page.get('title') or 'Notion 문서')[:200],
        url=f'https://www.notion.so/{identifier}', revision=_page_revision(page), observedAt=now,
        validUntil=now + timedelta(seconds=CACHE_SECONDS), grade='body' if complete else 'properties', relation=relation)
    facts = {'reason': 'document', 'ownerRole': page.get('owner_role', '')}
    if complete:
        facts['passages'] = [{'text': str(segment.get('text') or '')[:500], 'blockId': segment.get('block_id', '')}
                             for segment in page.get('_owned_segments') or []][:20]
    due = str(page.get('due_date') or '')[:10]
    status = normalize_for_match(str(page.get('status') or ''))
    # 상태 미상·'미완료'를 '완료'의 부분문자열로 잘못 처리하지 않는다.
    if due and status and status not in {'완료', '종료', '보관', 'done', 'closed', 'complete'} and due < now.astimezone().date().isoformat():
        facts['reason'] = 'due_review'
    return Candidate(id='notion:' + identifier, sources=[source], dedupeKey='notion:' + identifier,
        revision=source.revision, priority=4 if facts['reason'] == 'due_review' else 1,
        relevance=2 if relation in {'owner', 'contributor'} else 1, facts=facts,
        allowedActions=['review_progress', 'prepare_checklist'] if complete else ['review_progress'])


def _current_page(identifier: str, scope: dict, *, body: bool = True) -> dict | None:
    from backend.execution import CURRENT_BUDGET
    active = CURRENT_BUDGET.get()
    deadline = min(time.monotonic() + 15, active.deadline if active else float('inf'))
    page = read_candidate_page({'page_id': identifier}, scope, {}, CrawlBudget(deadline, 20))
    if page is None or not body:
        return page
    try:
        current = read_current_page({'page_id': identifier, 'database_id': page.get('database_id', '')}, scope, {}, deadline=deadline)
        return current or page
    except (NotionReadFailure, RuntimeError):
        return page


def _collect_notion(user: dict, now: datetime, batch: EvidenceBatch) -> None:
    settings = resolve_notion_settings()
    if not settings.get('token') or not settings.get('database_ids'):
        batch.sourceStatus['notion'] = 'unavailable' if settings.get('database_discovery_error') else 'unlinked'
        return
    scope, complete = resolve_notion_read_scope(settings['database_ids'], settings['token'])
    if not complete:
        batch.sourceStatus['notion'] = 'unavailable'
        return
    owner_name, owner_id = notion_owner(user)
    personal: list[dict] = []
    batch.sourceStatus['notion'] = 'ok' if owner_id else 'unlinked'
    if owner_id:
        result = execute_notion_search('', 12, constraints={'owner': owner_name, 'sort': {'field': 'updated_at', 'direction': 'desc'}},
                                      planned_terms=[], include_blocks=False, derive_text_constraints=False)
        personal = result.get('notion_pages') or []
        batch.sourceStatus['notion'] = 'ok' if result.get('status') == 'ok' else 'partial'
    ids = list(dict.fromkeys(identifier for c in batch.candidates for identifier in c.facts.get('notionIds', [])))
    personal_ids = [str(p['id']).replace('-', '') for p in personal]
    ids = list(dict.fromkeys([*ids, *personal_ids]))[:12]
    # 직접 링크가 없을 때만 구체적 제목으로 후보를 찾는다. 현재 본문에서 이슈 번호를 검증하기 전에는 연결하지 않는다.
    for c in batch.candidates[:2]:
        if c.facts.get('notionIds'):
            continue
        for record in hybrid_candidates(c.facts.get('topic', ''), list(scope), semantic=None, limit=2):
            identifier = record['page_id'].replace('-', '')
            if identifier not in ids and len(ids) < 12:
                ids.append(identifier)
    for index, identifier in enumerate(ids):
        try:
            page = _current_page(identifier, scope, body=index < 4)
            if not page:
                continue
            owned = bool(owner_id and owner_id in page.get('owner_user_ids', []))
            linked = any(identifier in c.facts.get('notionIds', []) for c in batch.candidates)
            if page.get('_verified_body') and not page.get('content_incomplete'):
                body = str(page.get('content') or '')
                for c in batch.candidates:
                    if c.sources[0].type == 'mantis' and re.search(r'(?<!\w)#' + re.escape(c.sources[0].id) + r'\b', body):
                        c.facts.setdefault('notionIds', []).append(identifier)
                        linked = True
            if not owned and not linked:
                continue
            relation: Relation = ('contributor' if page.get('owner_role') == '기여자' else 'owner') if owned else 'related'
            batch.candidates.append(page_candidate(page, now, relation=relation))
            if batch.sourceStatus['notion'] == 'unlinked':
                batch.sourceStatus['notion'] = 'ok'
        except Exception:
            batch.sourceStatus['notion'] = 'partial'


def collect_candidates(user: dict, *, now: datetime, budget: ExecutionBudget) -> EvidenceBatch:
    batch = EvidenceBatch(scopeKey=current_scope_key(user), sourceStatus={'mantis': 'unavailable', 'notion': 'unavailable'})
    with execution_scope(budget):
        for source, seconds, collector in [('mantis', 10, _collect_mantis), ('notion', 35, _collect_notion)]:
            try:
                run_with_deadline(seconds, partial(collector, user, now, batch))
            except Exception:
                batch.sourceStatus[source] = 'partial' if any(s.type == source for c in batch.candidates for s in c.sources) else 'unavailable'
    return batch


def revalidate_sources(user: dict, sources: list[SourceRef], *, budget: ExecutionBudget) -> list[SourceRef]:
    """prepare는 캐시 내용이 아닌 현재 원본을 확인한다."""
    with execution_scope(budget):
        try:
            linked_ids: set[str] = set()
            issue_ids = []
            for source in sources:
                if source.type != 'mantis':
                    continue
                raw = fetch_mantis_issue_by_id(int(source.id))
                if raw is None:
                    raise SourceRevoked()
                issue = normalize_issue(raw)
                if not _owned(issue, user) or not _issue_summary(issue)['isOpen'] or _issue_revision(issue) != source.revision:
                    raise SourceRevoked()
                linked_ids.update(notion_links(json.dumps(raw, ensure_ascii=False)))
                issue_ids.append(source.id)
            if any(source.type == 'notion' for source in sources):
                settings = resolve_notion_settings()
                scope, complete = resolve_notion_read_scope(settings['database_ids'], settings['token'])
                if not complete:
                    raise SourceUnavailable()
                _, owner_id = notion_owner(user)
                for source in sources:
                    if source.type != 'notion':
                        continue
                    page = _current_page(source.id, scope, body=source.grade == 'body' or source.relation == 'related')
                    if page is None:
                        raise SourceRevoked()
                    if source.relation in {'owner', 'contributor'} and (not owner_id or owner_id not in page.get('owner_user_ids', [])):
                        raise SourceRevoked()
                    if source.grade == 'body' and (not page.get('_verified_body') or page.get('content_incomplete')):
                        raise SourceUnavailable()
                    if _page_revision(page, include_body=source.grade == 'body') != source.revision:
                        raise SourceRevoked()
                    if source.relation == 'related' and source.id not in linked_ids:
                        body = str(page.get('content') or '') if page.get('_verified_body') and not page.get('content_incomplete') else ''
                        if not any(re.search(r'(?<!\w)#' + re.escape(i) + r'\b', body) for i in issue_ids):
                            raise SourceRevoked()
            return sources
        except SourceRevoked:
            raise
        except Exception as exc:
            if isinstance(exc, HTTPException) and exc.status_code in {401, 403, 404}:
                raise SourceRevoked() from exc
            if isinstance(exc, NotionReadFailure) and str(exc) in {'http_403', 'http_404'}:
                raise SourceRevoked() from exc
            raise SourceUnavailable() from exc
