"""정확 제목 조회를 짧게 재사용한다. 실패와 쓰기 이후의 결과는 재사용하지 않는다."""

import hashlib
import json
import os
import time
from collections import OrderedDict
from threading import Event, Lock
from typing import Any, Callable

import requests

from backend.config import get_notion_headers


_lock = Lock()
_entries: OrderedDict[str, tuple[float, bytes, dict[str, str]]] = OrderedDict()
_pending: dict[str, Event] = {}
_generation = 0


def clear_notion_query_cache() -> None:
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()


def _ttl() -> float:
    try:
        return max(0, min(float(os.getenv("NOTION_TITLE_QUERY_CACHE_SECONDS", "60")), 120))
    except ValueError:
        return 60


def cached_title_query(
    url: str, payload: dict[str, Any], send: Callable[..., requests.Response], **kwargs: Any,
) -> requests.Response:
    """권한·API 버전·DB·필터·커서별 캐시와 같은 요청의 동시 조회를 합친다."""
    ttl = _ttl()
    if ttl <= 0:
        return send(url, payload, **kwargs)
    key = hashlib.sha256(json.dumps(
        [get_notion_headers(), url, payload], sort_keys=True, ensure_ascii=False,
    ).encode()).hexdigest()
    while True:
        with _lock:
            entry = _entries.get(key)
            if entry and time.monotonic() - entry[0] < ttl:
                _entries.move_to_end(key)
                response = requests.Response()
                response.status_code = 200
                response._content = entry[1]
                response.headers.update(entry[2])
                response.headers["X-Assistance-Cache-Age"] = str(round(time.monotonic() - entry[0], 1))
                response.headers["X-Assistance-Cache-Ttl"] = str(ttl)
                response.encoding = "utf-8"
                response.url = url
                return response
            if key not in _pending:
                event = _pending[key] = Event()
                generation = _generation
                break
            event = _pending[key]
        event.wait()
    try:
        response = send(url, payload, **kwargs)
        if response.status_code == 200:
            # JSON만 저장하므로 요청 객체에 있는 토큰이나 mutable 응답을 공유하지 않는다.
            data = response.json()
            content = json.dumps(data, ensure_ascii=False).encode()
            with _lock:
                if generation == _generation:
                    _entries[key] = (time.monotonic(), content, {"Content-Type": "application/json"})
                    while len(_entries) > 512:
                        _entries.popitem(last=False)
        return response
    finally:
        with _lock:
            _pending.pop(key, None)
            event.set()
