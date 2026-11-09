"""보고서 내부 사실 선택·초안·독립 검토를 공통 모델 실행기로 연결한다."""
import json
import requests


def report_chat(run, request_turn, model, api_key, instruction, data, *, max_tokens, timeout):
    from backend.intent.json_payload import extract_json_object
    from backend.services.agent.errors import AgentStop
    payload = {'temperature': 0, 'seed': 42, 'max_tokens': min(max_tokens, run.settings.max_output_tokens),
               'stream': False, 'messages': [{'role': 'system', 'content': instruction},
                {'role': 'user', 'content': json.dumps(data, ensure_ascii=False)}]}
    if sum(len(m['content']) for m in payload['messages']) > run.settings.max_context_chars:
        raise ValueError('보고서 내부 검토 문맥 한도')
    try:
        turn = request_turn(run, payload, lambda limit: run.budget.investigation_seconds(min(limit, timeout)),
                            'report_internal', retries=1, can_retry=run.budget.can_investigate, investigation=True)
    except AgentStop as stop:
        # 정책 거절은 보고서의 '요약 실패'로 우회하지 않는다. 취소·서빙 변경도 위로 전달한다.
        if stop.reason == 'policy_blocked':
            raise
        raise requests.RequestException('보고서 내부 검토를 완료하지 못했습니다.') from stop
    if turn.kind != 'final':
        raise ValueError('보고서 내부 응답이 잘렸거나 형식이 다릅니다.')
    return extract_json_object(turn.content)
