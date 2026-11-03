"""회사 모델과 고정된 실제 저장본으로 질의 해석·조회·답변을 대조한다."""
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack, nullcontext, closing
from copy import deepcopy
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'output' / os.getenv('FOLLOWUP_QA_OUTPUT', 'count_list_followup_qa')
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / 'assistant_web'))
from backend.environment import load_dotenv
from backend.storage.sqlite import open_sqlite
from backend.storage.profiles import MANTIS_STORE_READ
load_dotenv(ROOT / 'assistant_web/.env')

OPEN = {'new', 'feedback', 'acknowledged', 'confirmed', 'assigned', 'developed'}
TEAM = set(json.loads((ROOT / 'assistant_web/config/team_dashboard.json').read_text())['members'])
BASE = {'media': 'MTS', 'states': sorted(OPEN), 'team': True}


def scope(**changes):
    return {**BASE, **changes}


def matches(row, wanted):
    """제품 파서·필터 함수와 독립된 원본 필드 정답 계산."""
    media = next((str(f.get('value') or '') for f in row.get('custom_fields', [])
                  if f.get('field', {}).get('name') == '매체구분'), '').casefold()
    aliases = {'MTS': ('mts', '엠티에스', '모바일', 'mobile'), 'HTS': ('hts', 'h-able', 'hable')}
    if wanted.get('media') and not any(a in media for a in aliases[wanted['media']]):
        return False
    state = row.get('status', {}).get('name')
    if wanted.get('states') is not None and state not in wanted['states']:
        return False
    owner = row.get('handler') or {}
    names = {owner.get('name'), owner.get('real_name')}
    if wanted.get('team') and not names & TEAM:
        return False
    if wanted.get('owner') and wanted['owner'] not in names:
        return False
    project = (row.get('project') or {}).get('name', '')
    if wanted.get('customer') and wanted['customer'] not in project:
        return False
    if wanted.get('exclude_customer') and wanted['exclude_customer'] in project:
        return False
    day = str(row.get('created_at') or '')[:10]
    if wanted.get('after') and day < wanted['after']:
        return False
    if wanted.get('before') and day > wanted['before']:
        return False
    title = str(row.get('summary') or '')
    if wanted.get('title') and wanted['title'] not in title:
        return False
    if wanted.get('exclude_title') and wanted['exclude_title'] in title:
        return False
    return True


with closing(open_sqlite(f'file:{ROOT}/assistant_web/runtime/mantis_complete.sqlite3?mode=ro', profile=MANTIS_STORE_READ)) as db:
    meta = json.loads(db.execute('select value from metadata').fetchone()[0])
    rows = []
    sql = """select id, json_extract(payload,'$.summary'), json_extract(payload,'$.project.name'),
        json_extract(payload,'$.handler.name'), json_extract(payload,'$.handler.real_name'),
        json_extract(payload,'$.status.name'), json_extract(payload,'$.created_at'),
        json_extract(payload,'$.updated_at'),
        (select json_extract(f.value,'$.value') from json_each(payload,'$.custom_fields') f
         where json_extract(f.value,'$.field.name')='매체구분' limit 1)
        from issues order by id desc"""
    for number, title, project, account, owner, state, created, updated, media in db.execute(sql):
        rows.append({'id': number, 'summary': title, 'project': {'name': project or ''},
                     'handler': {'name': account, 'real_name': owner}, 'status': {'name': state},
                     'created_at': created or '', 'updated_at': updated or '',
                     'custom_fields': [{'field': {'name': '매체구분'}, 'value': media or ''}]})

# 실제 저장본에 우연히 없는 조합에서도 잘못된 조건 해제를 검출한다.
probes = []
for n, (media, project, owner, state, day, title) in enumerate(itertools.product(
        ['MTS', 'HTS', 'WEB'], ['120NH(NH증권)', '107Samsung(삼성증권)', '112KBStar(KB증권)'],
        ['석지원', '이영희', '다른팀담당자'], sorted(OPEN | {'resolved', 'closed'}),
        ['2025-09-05', '2026-09-05', '2026-10-05', '2026-11-03'],
        ['차트 오류', '로그인 문의', '차트 점검']), start=1000000):
    probes.append({'id': n, 'summary': title, 'project': {'name': project},
                   'handler': {'name': owner, 'real_name': owner}, 'status': {'name': state},
                   'created_at': day, 'custom_fields': [{'field': {'name': '매체구분'}, 'value': media}]})

cases = []


def case(name, category, question, wanted=None, operation='search', limit=200,
         parent='base', asc=False, followup=True, sort_field='created_at'):
    cases.append({'name': name, 'category': category, 'question': question,
                  'scope': wanted if wanted is not None else deepcopy(BASE), 'operation': operation,
                  'limit': limit, 'parent': parent, 'asc': asc, 'followup': followup, 'sort_field': sort_field})


case('base', '기준 질문', 'MTS 안끝난 이슈 몇개야', operation='count', limit=None, parent=None, followup=False)
base_count = sum(matches(r, BASE) for r in rows)
variants = ['목록 다 보여줘', '리스트 전부 보여줘', '그 이슈들 빠짐없이 나열해줘',
            f'그 {base_count}건을 하나씩 보여줄래?', '한 건도 빼지 말고 어떤 이슈들인지 보여줘',
            '숫자만 말고 어떤 이슈인지 전부 알려줘', '앞에서 센 것들 번호와 제목을 모두 적어줘',
            '기존 조건 유지해서 전체 목록 보여줘', '그 결과를 하나도 누락하지 말고 나열해 주세요',
            '상태는 그대로 두고 목록을 전부 펼쳐줘', '전체 리스트를 표로 부탁해', '다 보여줘']
for i, question in enumerate(variants):
    case(f'variant-{i}', '표현 변화', question)
for i, (question, limit, asc) in enumerate([
        ('그중 최신 3건만 보여줘', 3, False), ('최근 두 개만 알려줘', 2, False),
        ('오래된 순으로 5건만 보여줘', 5, True), ('전체 중 10개만 번호와 제목으로 보여줘', 10, False)]):
    case(f'limit-{i}', '표시 개수·정렬', question, limit=limit, asc=asc)
for i, question in enumerate(['같은 조건으로 다시 세어줘', '그럼 총 몇 개야?', '목록 말고 숫자만 다시 알려줘']):
    case(f'count-{i}', '건수 재질문', question, operation='count', limit=None)
for name, question, wanted, operation in [
        ('no-developed', '개발완료는 빼고 목록을 전부 보여줘', scope(states=sorted(OPEN-{'developed'})), 'search'),
        ('with-resolved', '해결된 것도 포함해서 전부 보여줘', scope(states=sorted(OPEN|{'resolved'})), 'search'),
        ('with-closed', '종료된 것까지 포함해서 목록 전부 보여줘', scope(states=None), 'search'),
        ('assigned', '그중 할당된 이슈만 전부 보여줘', scope(states=['assigned']), 'search'),
        ('developed-count', '그중 개발완료만 몇 건이야?', scope(states=['developed']), 'count'),
        ('all-status-count', '상태 제한을 풀어서 몇 건인지 알려줘', scope(states=None), 'count')]:
    case(name, '상태 변경', question, wanted, operation, None if operation == 'count' else 200)
for name, question, wanted, operation in [
        ('nh', '그중 NH증권 것만 목록 다 보여줘', scope(customer='120NH'), 'search'),
        ('samsung-count', '삼성증권만 몇 건이야?', scope(customer='107Samsung'), 'count'),
        ('owner', '담당자가 이영희인 것만 전부 보여줘', scope(owner='이영희', team=False), 'search'),
        ('self', '내 담당만 목록 다 보여줘', scope(owner='석지원', team=False), 'search'),
        ('hts', 'MTS 대신 HTS로 바꿔서 전체 목록 보여줘', scope(media='HTS'), 'search'),
        ('october', '2026년 10월 등록된 것만 전부 보여줘', scope(after='2026-10-01', before='2026-10-31'), 'search'),
        ('title', '제목에 "차트" 들어간 것만 전부 보여줘', scope(title='차트'), 'search'),
        ('exclude-nh', 'NH증권은 빼고 전체 목록 보여줘', scope(exclude_customer='120NH'), 'search'),
        ('all-team', '팀 범위 풀고 전사 기준으로 목록 다 보여줘', scope(team=False), 'search')]:
    case(name, '조건 추가·교체', question, wanted, operation, None if operation == 'count' else 200)

# 같은 대화에서 실제 서버 감사 ID를 이어 사용하는 연속 후속.
case('chain-nh', '연속 조건 변경', 'NH증권 것만 몇 건이야?', scope(customer='120NH'), 'count', None)
case('chain-hts', '연속 조건 변경', '그럼 HTS는?', scope(customer='120NH', media='HTS'),
     'count', None, parent='chain-nh')
case('chain-list', '연속 조건 변경', '그것들 목록 다 보여줘', scope(customer='120NH', media='HTS'), parent='chain-hts')
case('chain-owner', '연속 조건 변경', '담당자는 이영희만 전체 목록으로 보여줘',
     scope(customer='120NH', media='HTS', owner='이영희', team=False), parent='chain-list')
case('chain-period', '연속 조건 변경', '2026년 10월에 등록된 것만 목록 다 보여줘',
     scope(customer='120NH', media='HTS', owner='이영희', team=False, after='2026-10-01', before='2026-10-31'),
     parent='chain-owner')
case('chain-clear-owner', '조건 해제', '담당자 조건만 해제하고 목록 다 보여줘',
     scope(customer='120NH', media='HTS', team=True, after='2026-10-01', before='2026-10-31'), parent='chain-period')
case('chain-clear-date', '조건 해제', '기간 제한도 빼고 목록 다 보여줘',
     scope(customer='120NH', media='HTS', team=True), parent='chain-clear-owner')
case('chain-clear-customer', '조건 해제', '고객사 제한까지 풀어서 전체 목록 보여줘',
     scope(media='HTS', team=True), parent='chain-clear-date')

case('no-media', '조건 해제', '매체 조건만 해제하고 목록 다 보여줘', scope(media=None))
case('no-customer', '조건 해제', '고객사 조건 해제하고 목록 다 보여줘', BASE, parent='nh')
case('no-period', '조건 해제', '기간 조건 없이 전체 목록 보여줘', BASE, parent='october')
case('restore-team', '범위 복원', '다시 우리 팀만 목록 다 보여줘', BASE, parent='all-team')
case('independent', '독립 질문 전환', '새 질문이야. 삼성증권 HTS 종료 이슈 몇 건이야?',
     scope(media='HTS', customer='107Samsung', states=['closed']), 'count', None, parent='nh', followup=False)
case('independent-year', '독립 질문 전환', '별개로 2025년 HTS 종료 이슈 몇 건이야?',
     scope(media='HTS', states=['closed'], after='2025-01-01', before='2025-12-31'), 'count', None,
     parent='nh', followup=False)

case('resolved-remove-developed', '상태 연속 연산', '여기서 개발완료만 빼고 나머지를 전부 나열해줘',
     scope(states=sorted((OPEN | {'resolved'}) - {'developed'})), parent='with-resolved')
case('resolved-remove-resolved', '상태 연속 연산', '해결 상태는 다시 제외해서 목록 다 보여줘',
     BASE, parent='with-resolved')
case('assigned-remove-all', '빈 상태 집합', '할당된 것도 빼고 나머지 목록 보여줘',
     scope(states=[]), parent='assigned')
case('reselect-developed', '상태 연속 연산', '이번에는 개발완료만 전부 보여줘',
     scope(states=['developed']), parent='assigned')
case('samsung-list', '조건 유지 표현', '고객사는 삼성증권으로만 바꿔서 목록 전부 보여줘',
     scope(customer='107Samsung'))
case('nh-short-count', '조건 유지 표현', 'NH증권은 몇 개?', scope(customer='120NH'), 'count', None)
case('owner-only', '담당자 조사', '담당자 이영희만 빠짐없이 나열해줘', scope(owner='이영희', team=False))
case('owner-unknown', '담당자 조사', '담당자는 홍길동만 전체 목록으로 보여줘', scope(owner='홍길동', team=False))
case('self-customer', '조건 연속 변경', '고객사만 삼성증권으로 변경해서 목록 다 보여줘',
     scope(owner='석지원', team=False, customer='107Samsung'), parent='self')
case('title-count', '검색어 유지', '그럼 이 조건으로 총 몇 개야?', scope(title='차트'), 'count', None, parent='title')
case('title-hts', '검색어 유지', '매체만 HTS로 바꾸고 전부 나열해줘', scope(title='차트', media='HTS'), parent='title')
case('title-closed-word', '상태와 제목 구분', '제목에 "종료"가 들어간 것만 목록 다 보여줘', scope(title='종료'))
case('exclude-developed-count', '상태와 작업 구분', '개발완료는 제외하면 총 몇 건이야?',
     scope(states=sorted(OPEN - {'developed'})), 'count', None)
case('no-platform-relation', '조건 해제 표현', '플랫폼과 관계없이 기존 상태로 목록 전부 보여줘', scope(media=None))
case('no-title', '조건 해제 표현', '제목 조건만 해제하고 전체 목록 보여줘', BASE, parent='title')
case('updated-oldest', '수정일 정렬', '수정일 오래된 순으로 3건만 보여줘', limit=3, asc=True, sort_field='updated_at')
case('updated-newest', '수정일 정렬', '최종 수정일이 최신인 순서대로 3개만 보여줘', limit=3, sort_field='updated_at')
case('id-ascending', '번호 정렬', '이슈 번호가 작은 순으로 4개만 나열해줘', limit=4, asc=True, sort_field='id')
case('id-descending', '번호 정렬', '이슈 번호 큰 순서로 4개만 보여줘', limit=4, sort_field='id')

if os.getenv('FOLLOWUP_QA_CASE_NAMES'):
    selected_names = set(os.environ['FOLLOWUP_QA_CASE_NAMES'].split(','))
    cases = [item for item in cases if item['name'] in selected_names]


def main():
    with tempfile.TemporaryDirectory(prefix='expanded-followup-qa-') as runtime, ExitStack() as stack:
        os.environ.update({'ASSISTANT_RUNTIME_DIR': runtime, 'AUDIT_DB_PATH': runtime+'/audit.sqlite3',
                          'WORKSPACE_DB_PATH': runtime+'/workspace.sqlite3', 'AUTH_USERS_DB_PATH': runtime+'/users.sqlite3',
                          'UNIFIED_QUERY_PLANNER_ENABLED': 'true'})
        from backend.settings.catalogs import initialize_customer_catalogs
        initialize_customer_catalogs()
        from backend import llm
        from backend.intent import unified
        from backend.sources import mantis_store, mantis
        from backend.sources.mantis_collection import IssueBatch
        from backend.schemas import ChatMessage, ChatRequest
        from backend.services import unified_planning
        from backend.services.chat import prepare
        from backend.services.chat_service import chat_service
        import requests

        model = {**llm.get_private_model('company-qwen3.6-35b-a3b'), 'fallbackModelIds': []}
        if not llm.get_model_api_key(model):
            raise RuntimeError('회사 모델 API 키가 없습니다.')
        original_request = requests.sessions.Session.request

        def company_only(session, method, url, **kwargs):
            if str(url).rstrip('/') != str(model['endpoint']).rstrip('/'):
                raise RuntimeError('검증에서 회사 모델 이외의 네트워크 요청은 차단합니다.')
            return original_request(session, method, url, **kwargs)

        batch = IssueBatch(rows, {'search_complete': True, 'snapshot_complete': True,
                                 'status': 'snapshot', 'fetchedAt': meta['fetchedAt']})
        stack.enter_context(patch.object(requests.sessions.Session, 'request', company_only))
        stack.enter_context(patch.object(prepare, 'get_private_model', return_value=model))
        stack.enter_context(patch.object(mantis_store, 'stream_complete_store', side_effect=lambda **kw: nullcontext(batch)))
        stack.enter_context(patch.object(mantis, 'stream_complete_store', side_effect=lambda **kw: nullcontext(batch)))
        stack.enter_context(patch.object(mantis, 'read_complete_store', return_value=batch))
        by_id = {r['id']: r for r in rows}
        stack.enter_context(patch.object(mantis, 'fetch_mantis_issue_by_id',
                                         side_effect=lambda issue_id: deepcopy(by_id.get(int(issue_id)))))
        user = {'uid': 'expanded-followup-qa', 'mantisUsername': '석지원', 'displayName': '석지원'}
        outcomes = []
        history = {}
        current_plans = []
        original_plan = unified_planning.plan_question

        def track_plan(*args, **kwargs):
            plan = original_plan(*args, **kwargs)
            current_plans.append(deepcopy(plan))
            return plan

        stack.enter_context(patch.object(unified_planning, 'plan_question', side_effect=track_plan))

        def run_case(item, retry=False):
            name, parent = item['name'], item['parent']
            parent_state = history.get(parent) if parent else None
            if parent and not parent_state:
                return {**item, 'pass': False, 'skipped': True, 'reason': '이전 단계가 실패하여 대화 연결을 검증할 수 없음'}
            messages = list(parent_state['messages']) if parent_state else []
            messages.append(ChatMessage(role='user', content=item['question']))
            request = ChatRequest(modelId=model['id'], messages=messages,
                                  contextAuditId=parent_state['audit'] if parent_state else None,
                                  tools={'mantis': True, 'notion': False, 'vectorDb': False}, abTest=False)
            wanted = [r for r in rows if matches(r, item['scope'])]
            wanted.sort(key=lambda r: (r.get(item['sort_field']) or '', r['id']), reverse=not item['asc'])
            started = perf_counter()
            current_plans.clear()
            proposals = []
            original_propose = unified._propose

            def track_proposal(*args, **kwargs):
                proposal = original_propose(*args, **kwargs)
                proposals.append(deepcopy(proposal))
                return proposal

            try:
                with patch.object(unified, '_propose', side_effect=track_proposal) as model_calls:
                    response = chat_service.build_response(request, user)
                plan = current_plans[-1] if current_plans else {}
                constraints = plan.get('resolved_constraints') or {}
                actual_ids = [int(s['issue_id']) for s in response.get('sources', []) if s.get('issue_id')]
                total = next((s.get('meta', {}).get('total_count') for s in response.get('retrieval', {}).get('stages', [])
                              if s.get('meta', {}).get('total_count') is not None), None)
                expected_probes = {r['id'] for r in probes if matches(r, item['scope'])}
                actual_probes = {r['id'] for r in mantis.select_mantis_rows(probes, constraints)}
                expected_ids = [r['id'] for r in wanted[:item['limit'] or 5]]
                checks = {'operation': plan.get('operation') == item['operation'],
                          'scope': actual_probes == expected_probes,
                          'verification': response.get('verification', {}).get('status') == 'pass',
                          'followup': plan.get('interpretation', {}).get('followup') == item['followup']}
                if item['operation'] == 'count':
                    checks['result'] = total == len(wanted)
                else:
                    checks['result'] = actual_ids == expected_ids
                    checks['limit'] = (plan.get('limit') or 5) == item['limit']
                passed = all(checks.values())
                result = {**item, 'pass': passed, 'checks': checks, 'expectedCount': len(wanted),
                          'actualCount': total if item['operation'] == 'count' else len(actual_ids),
                          'expectedIds': expected_ids if item['operation'] == 'search' else [],
                          'actualIds': actual_ids, 'actualPlan': plan, 'verification': response.get('verification'),
                          'mode': response.get('raw', {}).get('mode'), 'content': response.get('content'),
                          'llmCalls': model_calls.call_count, 'elapsed': round(perf_counter()-started, 2),
                          'modelProposals': proposals,
                          'probeMissing': sorted(expected_probes-actual_probes)[:8],
                          'probeExtra': sorted(actual_probes-expected_probes)[:8]}
                if passed and not retry:
                    history[name] = {'audit': response.get('auditId'),
                                     'messages': [*messages, ChatMessage(role='assistant', content=response['content'])]}
                return result
            except Exception as exc:
                return {**item, 'pass': False, 'exception': type(exc).__name__,
                        'exceptionDetail': str(exc)[:300],
                        'elapsed': round(perf_counter()-started, 2), 'actualPlan': current_plans[-1] if current_plans else {}}

        for i, item in enumerate(cases, start=1):
            result = run_case(item)
            outcomes.append(result)
            print(json.dumps({'index': i, 'total': len(cases), 'question': item['question'],
                              'pass': result['pass'], 'checks': result.get('checks'),
                              'expectedCount': result.get('expectedCount'), 'actualCount': result.get('actualCount'),
                              'elapsed': result.get('elapsed'), 'skipped': result.get('skipped', False),
                              'reason': result.get('reason') or result.get('actualPlan', {}).get('reason')}, ensure_ascii=False), flush=True)
            (OUT / 'results.json').write_text(json.dumps({'model': model['id'], 'snapshot': meta['fetchedAt'],
                                                        'snapshotRows': len(rows), 'probeRows': len(probes),
                                                        'results': outcomes}, ensure_ascii=False, indent=2))
        repeats = []
        for item, result in zip(cases, outcomes):
            if not result['pass'] and not result.get('skipped'):
                repeated = run_case(item, retry=True)
                repeats.append(repeated)
                print(json.dumps({'repeat': item['question'], 'pass': repeated['pass'],
                                  'checks': repeated.get('checks'), 'elapsed': repeated.get('elapsed')}, ensure_ascii=False), flush=True)
        summary = {'total': len(outcomes), 'passed': sum(x['pass'] for x in outcomes),
                   'failed': sum(not x['pass'] and not x.get('skipped') for x in outcomes),
                   'skipped': sum(bool(x.get('skipped')) for x in outcomes),
                   'repeatPassed': sum(x['pass'] for x in repeats), 'repeatTotal': len(repeats),
                   'categories': dict(Counter(x['category'] for x in outcomes))}
        (OUT / 'results.json').write_text(json.dumps({'model': model['id'], 'snapshot': meta['fetchedAt'],
                                                    'snapshotRows': len(rows), 'probeRows': len(probes),
                                                    'summary': summary, 'results': outcomes,
                                                    'repeats': repeats}, ensure_ascii=False, indent=2))
        print(json.dumps({'summary': summary}, ensure_ascii=False), flush=True)
        return 1 if summary['failed'] or summary['skipped'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
