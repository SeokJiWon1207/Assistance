"""기존 평가 공개 경로를 기능별 구현 모듈에 연결하는 호환 파사드."""

from backend.answers import generate_follow_up_questions as generate_follow_up_questions
from backend.evaluation.analytics import build_accuracy_analytics as build_accuracy_analytics
from backend.evaluation.cases import (
    DEFAULT_EVALUATION_CASES as DEFAULT_EVALUATION_CASES,
    EVALUATION_CASES as EVALUATION_CASES,
    build_evaluation_summary as build_evaluation_summary,
    load_evaluation_cases as load_evaluation_cases,
    run_evaluation_case as run_evaluation_case,
    run_evaluation_case_safely as run_evaluation_case_safely,
    save_evaluation_run as save_evaluation_run,
)
from backend.evaluation.comparison import (
    contains_evasive_phrase as contains_evasive_phrase,
    evaluate_ab_comparison as evaluate_ab_comparison,
    evaluate_ab_variant as evaluate_ab_variant,
    generate_basic_grounded_baseline as generate_basic_grounded_baseline,
    generate_candidate_model_answer as generate_candidate_model_answer,
    should_attach_ab_test as should_attach_ab_test,
)
from backend.evaluation.finalization import finalize_chat_response as finalize_chat_response
from backend.evaluation.verification import (
    build_basic_grounding as build_basic_grounding,
    build_verification_report as build_verification_report,
    ungrounded_constraint_labels as ungrounded_constraint_labels,
)
from backend.llm import (
    get_private_model as get_private_model,
    post_model_chat as post_model_chat,
)


__all__ = [
    "DEFAULT_EVALUATION_CASES",
    "EVALUATION_CASES",
    "build_accuracy_analytics",
    "build_basic_grounding",
    "build_evaluation_summary",
    "build_verification_report",
    "contains_evasive_phrase",
    "evaluate_ab_comparison",
    "evaluate_ab_variant",
    "finalize_chat_response",
    "generate_basic_grounded_baseline",
    "generate_candidate_model_answer",
    "load_evaluation_cases",
    "run_evaluation_case",
    "run_evaluation_case_safely",
    "save_evaluation_run",
    "should_attach_ab_test",
    "ungrounded_constraint_labels",
]
