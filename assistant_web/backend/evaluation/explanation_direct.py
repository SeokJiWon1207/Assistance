"""질문의 항목마다 검증된 같은 이슈의 근거로 직접 답한다."""

# 단계는 서로의 완료 증거를 대신하지 않는다.


from backend.reports.direct_conclusion import (
    _facts as _facts,
    reply_question as reply_question,
    sent_reply_rows as sent_reply_rows,
    direct_conclusion as direct_conclusion,
    question_bound_conclusion as question_bound_conclusion,
)
from backend.evidence.references import (
    _order as _order,
    _reference as _reference,
)
from backend.evidence.stages import (
    STAGES as _STAGES,
    completed_stage as _completed_stage,
    stage_events as _stage_event,
    _DONE as _DONE,
    _PENDING as _PENDING,
    _FAILED as _FAILED,
    _segments as _segments,
)

__all__ = ['_STAGES', '_completed_stage', '_stage_event', '_DONE', '_PENDING', '_FAILED', '_segments',
           '_order', '_reference', '_facts', 'reply_question', 'sent_reply_rows',
           'direct_conclusion', 'question_bound_conclusion']
