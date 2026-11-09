"""전용 평가 프로세스의 승인 범위 차단. 제품의 조회 조건을 보정하지 않는다."""
from contextlib import contextmanager
from threading import Lock
from unittest.mock import patch

from backend.tools.ledger import EvidenceLedger
from backend.tools.registry import ToolRegistry


class ApprovalScopeError(RuntimeError):
    """승인 밖의 평가 자료를 모델에 전달하기 전에 해당 턴을 실패 처리한다."""


@contextmanager
def approved_mantis_issues(issue_ids: set[int]):
    if not issue_ids or any(type(i) is not int or i <= 0 for i in issue_ids):
        raise ValueError('평가 전송이 승인된 이슈 ID 목록이 필요합니다')
    allowed = frozenset(issue_ids)
    events: list[dict] = []
    lock = Lock()
    add, execute = EvidenceLedger.add, ToolRegistry._execute

    def record(event):
        with lock:
            events.append(event)
        raise ApprovalScopeError('승인한 평가 이슈 범위를 벗어난 조회를 차단했습니다')

    def guarded_add(ledger, family, stage, *, grade, unapplied, call_id):
        if family == 'mantis':
            ids = {int(row['id']) for row in stage.get('issues_list') or []}
            ids.update(int(i) for i in (stage.get('meta') or {}).get('matched_issue_ids') or [])
            if denied := ids - allowed:
                record({'call_id': call_id, 'denied_issue_ids': sorted(denied)})
        return add(ledger, family, stage, grade=grade, unapplied=unapplied, call_id=call_id)

    def guarded_execute(registry, name, arguments, parse_error, context):
        # 이 문항 세트는 건수·목록·집계·상세·Notion 탐색이다. 보고서는 원장 추가 전
        # 내부 모델을 호출할 수 있으므로 범위 밖 선택 자체를 실패로 남긴다.
        if name == 'build_issue_report':
            record({'tool': name, 'reason': 'outside_evaluation_task'})
        return execute(registry, name, arguments, parse_error, context)

    with patch.object(EvidenceLedger, 'add', guarded_add), patch.object(ToolRegistry, '_execute', guarded_execute):
        yield events
