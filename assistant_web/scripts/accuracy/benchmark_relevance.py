"""독립 판정을 현재 원문 버전과 실제 전달 구간에 결속한다."""

LEVELS = {'irrelevant': 0, 'related': 1, 'direct': 2}


def validate_support(item: dict) -> None:
    scope = item.get('body_scope', 'excerpt')
    if scope not in {'excerpt', 'page'}:
        raise ValueError('관련성 판정 body_scope는 excerpt 또는 page입니다')
    if scope == 'excerpt':
        if 'supporting_ranges' in item or 'body_chars' in item:
            raise ValueError('원문 구간 판정에는 body_scope=page가 필요합니다')
        return
    size, ranges = item.get('body_chars'), item.get('supporting_ranges')
    if (type(size) is not int or size < 0 or not isinstance(ranges, list)
            or item['level'] != 'irrelevant' and not ranges):
        raise ValueError('원문 판정에는 전체 길이와 직접 확인한 근거 구간이 필요합니다')
    for span in ranges:
        if (not isinstance(span, dict) or set(span) != {'start', 'end'}
                or type(span['start']) is not int or type(span['end']) is not int
                or not 0 <= span['start'] < span['end'] <= size):
            raise ValueError('근거 구간은 원문 길이 안의 start/end 정수여야 합니다')


def completed_reads(response: dict) -> list[dict]:
    return [read for read in response.get('evidenceReads') or []
            if read.get('body_checked') is True and read.get('content_complete') is True
            and read.get('body_sha256') and read.get('returned_chars', 1) > 0]


def _covers(reads: list[dict], support: dict, size: int) -> bool:
    cursor = support['start']
    intervals = []
    for read in reads:
        start, length = read.get('content_offset'), read.get('returned_chars')
        if type(start) is int and type(length) is int and 0 <= start < start + length <= size:
            intervals.append((start, start + length))
    for start, end in sorted(intervals):
        if start > cursor:
            break
        cursor = max(cursor, end)
        if cursor >= support['end']:
            return True
    return False


def assess_relevance(expect: dict, response: dict) -> dict:
    """미판정은 부재가 아니다. 알려진 문서도 필요한 구간을 읽어야 통과한다."""
    def result(code, message, **details):
        return {'code': code, 'passed': code == 'supported',
                'unassessed': code == 'unassessed_version', 'message': message, **details}

    reads = completed_reads(response)
    if not reads:
        observed = response.get('evidenceReads') or []
        code = ('body_incomplete' if any(r.get('returned_chars', 0) > 0 for r in observed)
                else 'body_empty' if any(r.get('body_checked') for r in observed) else 'body_not_read')
        return result(code, '현재 본문을 끝까지 읽었다는 증명이 없습니다',
                      reasons=sorted({reason for r in observed for reason in r.get('content_incomplete_reasons') or []}))
    retained = {s['page_id'].replace('-', '') for s in response.get('sources') or [] if s.get('page_id')}
    reads = [r for r in reads if r['page_id'].replace('-', '') in retained]
    if not reads:
        return result('source_not_retained', '현재 읽은 본문이 최종 답변 근거에 없습니다')
    assessed, known, unread = [], set(), []
    for judgment in expect['judgments']:
        matches = [r for r in reads if r['page_id'].replace('-', '') == judgment['page_id'].replace('-', '')]
        page_scope = judgment.get('body_scope') == 'page'
        if page_scope:
            matches = [r for r in matches if r.get('page_body_verified') is True
                       and r.get('page_body_sha256') == judgment['body_sha256']
                       and r.get('page_body_chars') == judgment['body_chars']]
        else:
            matches = [r for r in matches if r['body_sha256'] == judgment['body_sha256']]
        if not matches:
            continue
        known.update(id(r) for r in matches)
        assessed.append(judgment)
        if LEVELS[judgment['level']] < LEVELS[expect['level']]:
            continue
        if page_scope and not all(_covers(matches, span, judgment['body_chars'])
                                  for span in judgment['supporting_ranges']):
            unread.append(judgment['page_id'])
            continue
        return result('supported', None, page_id=judgment['page_id'], level=judgment['level'])
    if unread:
        return result('support_not_read', '근거 구간 미열람: 현재 원문에는 근거가 있지만 필요한 구간을 전달하지 않았습니다', page_ids=unread)
    unknown = sorted({r['page_id'] for r in reads if id(r) not in known})
    if unknown:
        return result('unassessed_version', '관련성 미확인: 읽은 원문 버전에 대한 독립 판정이 없습니다', page_ids=unknown)
    return result('insufficient_relevance', f'요구한 관련성 {expect["level"]} 미충족: ' +
                  '; '.join(j['reason'] for j in assessed), levels=sorted({j['level'] for j in assessed}))
