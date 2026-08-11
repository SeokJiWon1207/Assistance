"""대화 문맥·후속 질문·지시어 해석 진입점."""

from backend.intent.core import (
    build_contextual_retrieval_text,
    get_latest_user_text,
    get_recent_user_texts,
    has_demonstrative_issue_reference,
    is_follow_up_question,
    parse_ordinal_reference,
    resolve_recent_created_at,
    resolve_reference_from_context,
    rewrite_follow_up_query_with_model,
)


__all__ = [
    "build_contextual_retrieval_text",
    "get_latest_user_text",
    "get_recent_user_texts",
    "has_demonstrative_issue_reference",
    "is_follow_up_question",
    "parse_ordinal_reference",
    "resolve_recent_created_at",
    "resolve_reference_from_context",
    "rewrite_follow_up_query_with_model",
]
