"""이슈 문서 작성 단계: 조사 중 호출 예약·조기 종료와 초안·독립 검토 모델 호출.

문서 구성·검증은 documents.py가 맡고, 이 모듈은 그 과정을 에이전트 요청의 공통 예산과
전송 정책에 연결한다. 모델 호출은 오케스트레이터의 request_turn을 인자로 받아 순환 import를 피한다.
"""

import json
from functools import partial
from typing import Any, Callable

from backend.audit import emit_chat_status
from backend.services.agent.document_format import draft_format
from backend.services.agent.documents import build_document, document_kind, document_reserve_calls, read_progress, ready_for_document
from backend.services.agent.errors import AgentStop
from backend.tools.redaction import redact

RequestTurn = Callable[..., Any]


def reserve_document_calls(run: Any) -> bool:
    """문서 요청이면 목록 크기에 맞는 작성·검토 호출을 남긴다. 전체 호출 상한은 그대로다."""
    if not document_kind(getattr(run, 'plan', {})):
        return False
    if run.ledger.query_results():
        reserve = document_reserve_calls(run.ledger, run.budget.max_model_requests - run.budget.model_requests)
        run.budget.set_reserve(calls=max(run.settings.final_reserve_calls, reserve))
    return True


def document_bodies_ready(run: Any) -> bool:
    """Mantis 문서에 필요한 본문이 원장에 모두 있으면 조사를 끝내고 예약된 작성·검토로 넘긴다."""
    if not (document_kind(run.plan) and run.plan.get('source') == 'mantis' and ready_for_document(run.ledger)):
        return False
    run.trace.append({'step': 'collection_complete', 'reason': 'document_bodies_ready',
                      'listedCount': read_progress(run.ledger)['listed_count']})
    return True


def generate_document(run: Any, request_turn: RequestTurn, system: str, data: dict) -> Any:
    """문서 생성·독립 검토에도 공통 호출 예산과 전송 정책을 적용한다."""
    if run.stop_reason == 'policy_blocked' or not run.budget.can_finalize_call():
        raise ValueError('document budget exhausted')
    outgoing = json.dumps(redact(data), ensure_ascii=False)
    if len(outgoing) + len(system) > run.settings.max_context_chars:
        raise ValueError('document context limit')
    correction = data.get('correction') or {}
    repair_slots = ({slot for row in correction.get('required_slots', []) for slot in row['slots']}
                    if correction and not correction.get('missing_issue_ids') else None)
    payload = {'model': run.model.get('model', run.model['id']), 'temperature': 0, 'stream': False,
               'max_tokens': min(4096, max(run.settings.max_output_tokens, 600 * len(data.get('issues', [])),
                                          180 * len(data.get('claims', [])))),
               'response_format': {'type': 'json_object'} if 'claims' in data else draft_format(data['kind'], slots=repair_slots),
               'messages': [{'role': 'system', 'content': system},
                            {'role': 'user', 'content': outgoing}]}
    step = 'document_review' if 'claims' in data else 'document_draft'
    for attempt in range(2):
        try:
            turn = request_turn(run, payload, run.budget.final_seconds, step if attempt == 0 else 'document_draft_retry', retries=0)
        except AgentStop as exc:
            run.stop_reason = exc.reason
            raise ValueError('document model unavailable') from exc
        error: ValueError
        if turn.kind == 'final':
            try:
                return json.loads((turn.content or '').strip().removeprefix('```json').removeprefix('```')
                                  .removesuffix('```').strip())
            except json.JSONDecodeError as exc:
                error = exc
                reason = '직전 초안의 JSON 형식이 잘못되어 해석하지 못했다. 올바르게 닫힌 하나의 JSON 객체로 다시 작성한다.'
        else:
            error = ValueError(f'incomplete document response: {turn.kind}')
            reason = '직전 초안이 출력 한도에서 잘렸다. 부분 초안은 채택하지 않는다.'
        if (turn.kind not in {'final', 'truncated_final'} or step != 'document_draft' or attempt
                or run.budget.model_requests + 2 > run.budget.max_model_requests or run.budget.remaining() <= 30):
            raise error
        shorter = (system + '\n' + reason + ' '
                   '제공된 모든 이슈를 유지하고 질문에서 명시한 핵심 항목만 항목당 한 문장으로 짧게 다시 작성한다. '
                   '요청하지 않은 원인·조치·후속 작업은 추가하지 않는다. 원문 근거·불확실성·문단 ID와 JSON 형식은 유지한다.')
        if len(outgoing) + len(shorter) > run.settings.max_context_chars:
            raise error
        payload['messages'][0]['content'] = shorter
    raise ValueError('document response unavailable')


def write_document(run: Any, request_turn: RequestTurn) -> tuple[str, str, dict, Any]:
    """원장의 이슈 원문으로 문서를 작성·검토하고 실행 상태에 검토 결과를 기록한다."""
    emit_chat_status('이슈별 원문과 문서 문장을 대조하고 있습니다', 'generation')
    kind = document_kind(run.plan)
    content, review, refs = build_document(run.ledger, kind, run.context.latest_user_text,
                                          partial(generate_document, run, request_turn),
                                          confirmed_ids=tuple(run.constraints.get('issue_ids') or []),
                                          max_context_chars=run.settings.max_context_chars,
                                          can_generate=lambda: run.budget.model_requests + 2 <= run.budget.max_model_requests
                                          and run.budget.remaining() > 5,
                                          can_repair=lambda: run.stop_reason != 'policy_blocked'
                                          and run.budget.model_requests + 2 <= run.budget.max_model_requests
                                          and run.budget.remaining() > 30)
    run.finalization = 'issue_document'
    run.document_review = review
    if review.get('repair'):
        run.repair_review.extend({'path': 'document', **audit} for audit in review['repair']['batches'])
    return kind, content, review, refs
