"""정확도 리포트의 추세와 반복 실패를 집계한다."""

from typing import Any, Dict, List


def build_accuracy_analytics(report_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """최신 리포트부터 점수 변화와 반복 실패 유형을 요약한다."""
    if not report_data:
        return {"trend": [], "latestDelta": None, "failureCounts": []}

    def summary_for(data: Dict[str, Any]) -> Dict[str, Any]:
        summary = data.get("summary")
        return summary if isinstance(summary, dict) else {}

    trend: List[Dict[str, Any]] = []
    for data in reversed(report_data):
        summary = summary_for(data)
        score = summary.get("score")
        trend.append(
            {
                "runAt": data.get("run_at"),
                "score": int(score) if score is not None else None,
                "passed": int(summary.get("passed") or 0),
                "total": int(summary.get("total") or 0),
                "status": summary.get("status", "unknown"),
            }
        )
    scored_summaries = [
        summary_for(data)
        for data in report_data
        if summary_for(data).get("score") is not None
    ]
    latest_score = int(scored_summaries[0]["score"]) if scored_summaries else None
    previous_score = (
        int(scored_summaries[1]["score"])
        if len(scored_summaries) > 1
        else None
    )
    failure_counts: Dict[str, int] = {}
    for data in report_data:
        for result in data.get("results") or []:
            if result.get("passed") or result.get("outcome") in {
                "dependency_error",
                "execution_error",
            }:
                continue
            category = str(result.get("category") or "미분류")
            failure_counts[category] = failure_counts.get(category, 0) + 1
    return {
        "trend": trend,
        "latestDelta": (
            latest_score - previous_score
            if latest_score is not None and previous_score is not None
            else None
        ),
        "previousScore": previous_score,
        "failureCounts": [
            {"category": category, "count": count}
            for category, count in sorted(
                failure_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }


__all__ = ["build_accuracy_analytics"]
