"""선택된 이슈의 누락 노트를 제한된 예산 안에서 원문 근거에 추가한다."""
from copy import deepcopy
import os
import re
from typing import Any

from fastapi import HTTPException
import requests

from backend.evaluation.explanation_quotes import safe_excerpts
from backend.execution import RequestStopped, checkpoint
from backend.sources.mantis import fetch_mantis_issue_by_id, get_issue_custom_field_value, issue_matches_constraints
from backend.sources.mantis_chunks import search_tokens, split_issue


_MAX_ADDED_CHARS = 12000
_STAGE = re.compile(r'원인|조치|수정|QC|검수|전달|보류|정정|취소|롤백|제외|실패|재검증|배포', re.I)
_REVERSAL = re.compile(r'QC\s*NG|검수[^\n]{0,20}실패|보류|정정|취소|롤백|제외|재검증', re.I)


def _merged(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    joined: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if joined and start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(end, joined[-1][1]))
        else:
            joined.append((start, end))
    return joined


def _uncovered(start: int, end: int, ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    missing = []
    for left, right in _merged(ranges):
        if right <= start or left >= end:
            continue
        if left > start:
            missing.append((start, left))
        start = max(start, right)
    if start < end:
        missing.append((start, end))
    return missing


def _source_fields(raw: dict[str, Any]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in split_issue(raw):
        field = chunk['field']
        previous = fields.get(field, '')
        fields[field] = previous + chunk['text'][max(0, len(previous) - chunk['start']):]
    return fields


def _note_metadata(note: dict[str, Any]) -> dict[str, Any]:
    return {'note_id': note.get('id'), 'created_at': note.get('created_at', ''),
            'updated_at': note.get('updated_at', ''),
            'reporter': (note.get('reporter') or {}).get('real_name')
            or (note.get('reporter') or {}).get('name', '')}


def _same_source(excerpts: list[dict[str, Any]], fields: dict[str, str],
                 notes: dict[str, dict[str, Any]]) -> bool:
    for excerpt in excerpts:
        field = excerpt.get('field')
        start, end, text = excerpt.get('start'), excerpt.get('end'), excerpt.get('text')
        if (field not in fields or not isinstance(start, int) or not isinstance(end, int)
                or not isinstance(text, str) or not 0 <= start <= end <= len(fields[field])
                or fields[field][start:end] != text):
            return False
        if field in notes:
            metadata = _note_metadata(notes[field])
            if any(excerpt[key] != metadata[key] for key in metadata if key in excerpt):
                return False
    return True


def _safe_note_chunks(raw: dict[str, Any], notes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    # 안전 필터는 노트 전체를 본다. 접속정보나 다른 이슈 제목이 떨어진 구간에
    # 있어도 해당 구간만 독립적으로 통과시키지 않는다.
    full = [{'field': field, 'start': 0, 'end': len(str(note.get('text') or '')),
             'text': str(note.get('text') or ''), **_note_metadata(note)}
            for field, note in notes.items() if note.get('id')]
    safe = safe_excerpts({'id': raw['id'], 'answer_evidence': {'excerpts': full}})
    candidates = []
    for excerpt in safe:
        field, text = excerpt['field'], excerpt['text']
        original = str(notes[field].get('text') or '')
        start = excerpt.get('start', 0)
        if original[start:start + len(text)] != text:
            # 안전 필터가 문단을 분리하면 위치가 그대로일 수 있다. 반복 문단은
            # 위치를 추정하지 않고 제외한다.
            start = original.find(text)
            if start < 0 or original.find(text, start + 1) >= 0:
                continue
        partial = {'id': raw['id'], 'notes': [{'id': notes[field]['id'], 'text': text}]}
        for chunk in split_issue(partial):
            candidates.append({'field': field, 'start': start + chunk['start'],
                               'end': start + chunk['end'], 'text': chunk['text'],
                               **_note_metadata(notes[field])})
    return candidates


def _add_notes(issue: dict[str, Any], raw: dict[str, Any], query: str, budget: int) -> int | None:
    evidence = issue['answer_evidence']
    excerpts = evidence.get('excerpts') or []
    fields = _source_fields(raw)
    notes = {f"note:{note.get('id', index)}": note for index, note in enumerate(raw.get('notes') or [])}
    if len(notes) != len(raw.get('notes') or []) or not _same_source(excerpts, fields, notes):
        return None
    ranges: dict[str, list[tuple[int, int]]] = {}
    for excerpt in excerpts:
        ranges.setdefault(excerpt['field'], []).append((excerpt['start'], excerpt['end']))
    wanted = set(search_tokens(query))
    candidates = _safe_note_chunks(raw, notes)
    safe_ranges: dict[str, list[tuple[int, int]]] = {}
    for candidate in candidates:
        safe_ranges.setdefault(candidate['field'], []).append((candidate['start'], candidate['end']))
    # 새 원문을 전체 검사한 결과 기존 부분 인용까지 안전 범위에서 벗어나면
    # 섞어 쓰지 않는다. 호출자가 기존 근거의 확정 표시도 보류하도록 알린다.
    if any(excerpt['field'] in notes and _uncovered(
            excerpt['start'], excerpt['end'], safe_ranges.get(excerpt['field'], [])) for excerpt in excerpts):
        return None
    # 되돌림/검수 실패와 각 단계의 짧은 근거가 긴 관련 노트 뒤로 밀리지 않는다.
    candidates.sort(key=lambda e: (
        bool(_REVERSAL.search(e['text'])), bool(_STAGE.search(e['text'])),
        len(wanted & set(search_tokens(e['text']))),
    ), reverse=True)
    # 한 긴 노트의 여러 구간보다 다른 누락 노트의 단계 근거를 먼저 보충한다.
    first_fields: set[str] = set()
    anchors, remaining = [], []
    for candidate in candidates:
        if candidate['field'] not in first_fields and (
                _STAGE.search(candidate['text']) or wanted & set(search_tokens(candidate['text']))):
            anchors.append(candidate)
            first_fields.add(candidate['field'])
        else:
            remaining.append(candidate)
    candidates = [*anchors, *remaining]
    added = 0
    for candidate in candidates:
        checkpoint()
        field = candidate['field']
        missing = _uncovered(candidate['start'], candidate['end'], ranges.get(field, []))
        cost = sum(end - start for start, end in missing)
        if not missing or added + cost > budget:
            continue
        # 전체 구간을 포함하고 마지막에 인접·중첩 범위를 합친다. 각 chunk를
        # 별도 문단처럼 검증하면 앞부분의 부정/초안 문맥을 잃을 수 있다.
        ranges.setdefault(field, []).append((candidate['start'], candidate['end']))
        added += cost
    merged_excerpts = []
    for field, rows in ranges.items():
        for start, end in _merged(rows):
            exact = next((e for e in excerpts if e['field'] == field
                          and e['start'] == start and e['end'] == end), None)
            if exact is not None:
                merged_excerpts.append(exact)
            else:
                merged_excerpts.append({'field': field, 'start': start, 'end': end,
                                        'text': fields[field][start:end],
                                        **(_note_metadata(notes[field]) if field in notes else {})})
    evidence['excerpts'] = merged_excerpts
    covered = {field: sum(end - start for start, end in _merged(rows)) for field, rows in ranges.items()}
    total_chars = sum(map(len, fields.values()))
    provided_chars = sum(covered.values())
    evidence.update(total_chars=total_chars, provided_chars=provided_chars,
                    total_notes=len(notes), provided_notes=len({e['field'] for e in evidence['excerpts']
                                                               if e['field'] in notes}),
                    total_note_chars=sum(len(text) for field, text in fields.items() if field in notes),
                    provided_note_chars=sum(count for field, count in covered.items() if field in notes),
                    complete=provided_chars == total_chars)
    return added


def supplement_selected_notes(grounding: dict[str, Any], selected_ids: list[int], query: str,
                              constraints: dict[str, Any], *,
                              source_constraints: dict[int, dict[str, Any]] | None = None,
                              ) -> tuple[dict[str, Any], dict[str, Any]]:
    """기존 근거를 보존하고 최대 세 이슈에 총 12,000자의 누락 노트를 추가한다."""
    result = deepcopy(grounding)
    enabled = os.getenv('MANTIS_NOTE_SUPPLEMENT_ENABLED', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}
    meta: dict[str, Any] = {'enabled': enabled, 'attempted_ids': [], 'supplemented_ids': [],
                            'added_chars': 0, 'failures': [], 'skipped_ids': []}
    if not enabled:
        return result, meta
    issues = {issue.get('id'): issue for issue in result.get('issues', [])}
    for issue_id in dict.fromkeys(selected_ids):
        issue = issues.get(issue_id)
        if (not issue or (issue.get('answer_evidence') or {}).get('complete') is not False
                or len(meta['attempted_ids']) >= 3 or meta['added_chars'] >= _MAX_ADDED_CHARS):
            meta['skipped_ids'].append(issue_id)
            continue
        checkpoint()
        meta['attempted_ids'].append(issue_id)
        reason = ''
        try:
            raw = fetch_mantis_issue_by_id(issue_id)
            checkpoint()
            effective = (source_constraints or {}).get(issue_id, constraints)
            project = issue.get('project')
            known_project = str(project.get('name') or '') if isinstance(project, dict) else str(project or '')
            if known_project in {'미상', '기타/미상'}:
                known_project = ''
            if not raw or raw.get('id') != issue_id:
                reason = 'missing_issue'
            elif (known_project and known_project != str((raw.get('project') or {}).get('name') or '')
                  or effective.get('_reference_unknown_media') is True
                  and get_issue_custom_field_value(raw, '매체구분').strip()
                  or not issue_matches_constraints(raw, effective)):
                reason = 'scope_mismatch'
            else:
                added = _add_notes(issue, raw, query, _MAX_ADDED_CHARS - meta['added_chars'])
                if added is None:
                    reason = 'stale_evidence'
                elif added:
                    meta['added_chars'] += added
                    meta['supplemented_ids'].append(issue_id)
        except RequestStopped:
            raise
        except PermissionError:
            reason = 'access_denied'
        except HTTPException as exc:
            reason = ('access_denied' if exc.status_code in {401, 403} else
                      'missing_issue' if exc.status_code in {404, 410} else 'fetch_failed')
        except (requests.RequestException, TimeoutError):
            # 외부 오류 메시지에는 원문/접속정보가 들어갈 수 있다.
            reason = 'fetch_failed'
        except (ValueError, TypeError, KeyError, AttributeError):
            reason = 'stale_evidence'
        if reason:
            meta['failures'].append({'issue_id': issue_id, 'reason': reason})
            issue['answer_evidence']['supplement_failed'] = reason
            issue['answer_evidence']['complete'] = False
    return result, meta
