"""평가 코드·설정·저장본 버전과 최신성을 고정한다. 비밀값은 출력하지 않는다."""
import hashlib
import json
import os
from pathlib import Path


def digest(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def code_version(app: Path) -> dict:
    files = {str(p.relative_to(app)): digest(p) for folder in ('backend', 'config', 'scripts/accuracy')
             for p in (app / folder).rglob('*') if p.is_file() and p.suffix in {'.py', '.json'}
             and '__pycache__' not in p.parts and 'static' not in p.parts}
    return {'sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), 'files': files}


def snapshot_version(runtime: Path) -> dict:
    names = ('mantis_complete.sqlite3', 'notion_body_index.sqlite3', 'notion_semantic.sqlite3',
             'notion_title_index.json', 'notion_read_scope.json', 'notion_database_roles.json', 'agent_tool_verification.json',
             'entity-header-catalog-v1.json')
    return {name: digest(runtime / name) for name in names if (runtime / name).is_file()}


def public_settings() -> dict:
    names = ('AGENT_MODE_ENABLED', 'UNIFIED_QUERY_PLANNER_ENABLED', 'CHAT_REQUEST_TIMEOUT_SECONDS',
             'AGENT_ALLOWED_MODEL_IDS', 'AGENT_ALLOWED_ENDPOINT_ORIGINS', 'AGENT_MAX_MODEL_REQUESTS',
             'AGENT_MAX_TOOL_CALLS', 'AGENT_MAX_CALLS_PER_TURN', 'AGENT_MAX_CONCURRENT_TOOLS', 'AGENT_FINAL_RESERVE_CALLS',
             'AGENT_FINAL_RESERVE_SECONDS', 'AGENT_TOOL_TIMEOUT_SECONDS', 'AGENT_MAX_CONTEXT_CHARS',
             'NOTION_SNAPSHOT_CATALOG_MAX_AGE_SECONDS', 'NOTION_SHARED_RATE_LIMIT_ENABLED', 'NOTION_CACHE_WARM_ENABLED')
    return {name: os.environ.get(name) for name in names}


def require_fresh(sources: set[str]) -> dict:
    from backend.sources.mantis import get_mantis_snapshot_freshness_status
    from backend.notion.complete_snapshot import get_notion_catalog_freshness
    states = {}
    if 'mantis' in sources:
        states['mantis'] = get_mantis_snapshot_freshness_status()
    if 'notion' in sources:
        states['notion'] = get_notion_catalog_freshness()
    stale = [source for source, state in states.items() if state.get('status') != 'fresh']
    if stale:
        raise RuntimeError('측정 중단: 최신 저장본 필요 (' + ', '.join(stale) + ')')
    return states
