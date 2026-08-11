"""후보 답변 생성과 A/B 비교 진입점."""

from backend.evaluation.core import (
    contains_evasive_phrase,
    evaluate_ab_comparison,
    evaluate_ab_variant,
    generate_candidate_model_answer,
    should_attach_ab_test,
)


__all__ = [
    "contains_evasive_phrase",
    "evaluate_ab_comparison",
    "evaluate_ab_variant",
    "generate_candidate_model_answer",
    "should_attach_ab_test",
]
