"""제품의 필터·정렬 구현과 독립적인 고객사 확대 평가 정답/문항."""
from contextlib import closing
import json
from pathlib import Path
from backend.storage.profiles import MANTIS_STORE_READ
from backend.storage.sqlite import open_sqlite


STATUS_LABELS = {'new': '신규 이슈', 'feedback': '정보 부족', 'acknowledged': '이슈 검토',
                 'confirmed': '확인된 이슈', 'assigned': '할당된 이슈', 'developed': '개발완료',
                 'resolved': '해결된 이슈', 'closed': '종료된 이슈'}


def read_oracle(path: Path, project_id: int) -> dict:
    """원본 프로젝트 ID, 상태 열, UTC 생성 시각과 ID로 정답을 계산한다."""
    with closing(open_sqlite(f'{path.resolve().as_uri()}?mode=ro', profile=MANTIS_STORE_READ)) as db:
        rows = db.execute("""SELECT id, payload, CAST(strftime('%s', json_extract(payload, '$.created_at')) AS INTEGER)
            AS created FROM issues WHERE json_extract(payload, '$.project.id')=? ORDER BY created DESC, id DESC""",
            (project_id,)).fetchall()
    selected, groups = [], {}
    for identifier, payload, created in rows:
        raw = json.loads(payload)
        status = (raw.get('status') or {}).get('name')
        if status not in STATUS_LABELS or created is None:
            raise ValueError('독립 정답을 만들 수 없는 상태 또는 생성 시각')
        if status in {'resolved', 'closed'}:
            continue
        selected.append((identifier, raw['summary']))
        label = STATUS_LABELS[status]
        groups[label] = groups.get(label, 0) + 1
    return {'count': len(selected), 'ids': [row[0] for row in selected[:3]],
            'all_ids': [row[0] for row in selected], 'titles': {str(i): title for i, title in selected}, 'groups': groups}


def _plan(customer: str, *, changed: bool = False) -> list[dict]:
    return [{'type': 'delivered'}, {'type': 'plan', 'customer': customer, 'statuses': ['open'],
                                    **({'issue_ids': []} if changed else {})}]


def _list_requirements(profile: dict) -> list[dict]:
    return [
        {'id': 'count', 'label': '미해결 전체 건수', 'expect': [{'type': 'count', 'value': profile['count']}]},
        {'id': 'list', 'label': '표시한 최근 3건과 순서', 'expect': [
            {'type': 'displayed_issue_ids', 'ids': profile['ids']},
            {'type': 'issue_ids', 'ids': profile['ids'], 'ordered': True}]},
        {'id': 'ordinal', 'label': '후속 질문에 저장한 목록 순서', 'expect': [
            {'type': 'followup_issue_ids', 'ids': profile['ids']}]}]


def build_cases(profiles: list[dict]) -> list[dict]:
    if (len(profiles) < 3 or len({p['customer'] for p in profiles}) != len(profiles)
            or len({p['key'] for p in profiles}) != len(profiles)
            or any(len(p['ids']) != 3 or len(set(p['ids'])) != 3 for p in profiles)):
        raise ValueError('서로 다른 고객사 3곳 이상과 고객사별 최근 3건이 필요합니다')
    compound = (
        '전사 범위에서 {c} 미해결 이슈가 몇 건이야? 최근 3건을 보여주고, 두 번째 이슈와 관련된 Notion 가이드도 설명해줘.',
        '전사 {c} 미해결 이슈 전체 건수를 세고 최신 3건을 보여줘. 그중 두 번째 이슈에 관련된 Notion 가이드도 찾아 설명해줘.',
        '전사 기준 {c} 미해결 이슈의 총 건수와 최근 3개 목록을 알려줘. 목록의 두 번째 이슈 관련 Notion 가이드를 읽고 설명도 해줘.')
    followup = ('그중 두 번째 이슈 번호와 제목만 다시 알려줘.',
                '방금 보여준 목록에서 두 번째 항목의 이슈 번호와 정확한 제목을 다시 알려줘.',
                '위 목록 2번의 이슈 번호와 제목만 정확하게 알려줘.')
    cases = []
    for index, profile in enumerate(profiles):
        customer = profile['customer']
        target = profiles[(index + 1) % len(profiles)]
        second = profile['ids'][1]
        requirements = _list_requirements(profile) + [
            {'id': 'body', 'label': '현재 Notion 본문 읽기', 'expect': [{'type': 'current_body'}]}]
        for level, label in (('related', '같은 주제의 관련 참고 근거'), ('direct', '질문한 기능의 직접 가이드')):
            requirements.append({'id': level, 'label': label, 'expect': [{'type': 'evidence_relevance',
                'topic': profile.get(f'{level}_topic', profile['topic']), 'level': level,
                'judgments': profile.get(f'{level}_judgments', profile['judgments'])}]})
        turns = [
            {'question': compound[index % 3].format(c=customer), 'expect': _plan(profile.get('plan_customer', customer)),
             'requirements': requirements, 'customer': customer, 'kind': 'compound'},
            {'question': followup[index % 3], 'expect': [{'type': 'delivered'}], 'customer': customer, 'kind': 'ordinal',
             'requirements': [{'id': 'followup', 'label': '실제 표시 목록 두 번째 ID와 정확한 제목', 'expect': [
                 {'type': 'issue_ids', 'ids': [second]},
                 {'type': 'contains', 'all': [str(second), profile['titles'][str(second)]]}]}]},
            {'question': f"고객사만 {target['customer']}로 바꿔서 전체 건수와 최근 3건을 다시 보여줘.",
             'expect': _plan(target.get('plan_customer', target['customer']), changed=True), 'requirements': _list_requirements(target),
             'customer': target['customer'], 'kind': 'customer_change'}]
        common = {'split': profile.get('split', 'final'), 'customer': customer}
        cases.append({'id': f"{profile['key']}-compound", **common, 'category': 'explain',
                      'tools': {'mantis': True, 'notion': True, 'vectorDb': False}, 'turns': turns})
        cases.append({'id': f"{profile['key']}-groups", **common, 'category': 'aggregate',
                      'tools': {'mantis': True, 'notion': False, 'vectorDb': False}, 'turns': [{
                          'question': f'전사 {customer} 미해결 이슈를 상태별로 몇 건인지 알려줘.',
                          'expect': _plan(profile.get('plan_customer', customer)), 'customer': customer, 'kind': 'aggregate',
                          'requirements': [{'id': 'groups', 'label': '상태별 전체 건수와 표시 표',
                              'expect': [{'type': 'groups', 'values': profile['groups']}]}]}]})
    return cases
