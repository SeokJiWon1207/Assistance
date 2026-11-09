"""문서 초안 형식만 제한한다. 사실·출처·의미 검증은 별도로 수행한다."""
from backend.services.agent.documents import DRAFT_BATCH_SIZE, NOTICE_SLOTS, SLOTS
from backend.services.agent.policy import endpoint_origin


def _object(properties: dict) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


def draft_format(kind: str, *, slots: set[str] | None = None) -> dict:
    # 종류별 고정 스키마를 재사용한다. 이슈·문단 ID의 결속은 기존 검증기가 대조한다.
    claim = _object({'text': {'type': 'string', 'maxLength': 180},
                     'evidence_ids': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': 6}})
    permitted = set(NOTICE_SLOTS if kind == 'notice' else SLOTS)
    section = _object({'slot': {'type': 'string', 'enum': sorted(permitted if slots is None else permitted & slots)},
                       'claims': {'type': 'array', 'items': claim, 'maxItems': 1 if kind == 'notice' else 2}})
    issue = _object({'issue_id': {'type': 'integer'},
                     'sections': {'type': 'array', 'items': section, 'maxItems': len(SLOTS)}})
    schema = _object({'issues': {'type': 'array', 'items': issue, 'maxItems': DRAFT_BATCH_SIZE}})
    return {'type': 'json_schema', 'json_schema': {'name': 'customer_notice' if kind == 'notice' else 'issue_document',
                                                 'strict': True, 'schema': schema}}


def adapt_document_format(payload: dict, model: dict) -> dict:
    # 실제 전송 모델마다 적용한다. 지원이 확인되지 않은 회사·대체 모델에는
    # 기존 JSON 모드를 사용한다. 전송 허용 여부와 검증 기준은 바꾸지 않는다.
    if (model.get('provider') == 'openai' and endpoint_origin(model.get('endpoint', '')) == 'https://api.openai.com'):
        return payload
    return {**payload, 'response_format': {'type': 'json_object'}}
