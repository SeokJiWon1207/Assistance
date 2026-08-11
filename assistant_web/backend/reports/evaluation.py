"""기존 평가 import 경로를 새 구현으로 연결하는 호환 파사드."""

import sys

from backend.evaluation import core as _core
from backend.evaluation.core import (
    EVALUATION_CASES,
    build_accuracy_analytics,
    build_evaluation_summary,
    build_verification_report,
    evaluate_ab_comparison,
    finalize_chat_response,
    generate_candidate_model_answer,
    run_evaluation_case_safely,
    save_evaluation_run,
)


__all__ = [
    "EVALUATION_CASES",
    "build_accuracy_analytics",
    "build_evaluation_summary",
    "build_verification_report",
    "evaluate_ab_comparison",
    "finalize_chat_response",
    "generate_candidate_model_answer",
    "run_evaluation_case_safely",
    "save_evaluation_run",
]

sys.modules[__name__] = _core
