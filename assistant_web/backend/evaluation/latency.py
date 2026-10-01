"""개별 질문을 노출하지 않는 단계별 지연 통계. 단계는 중첩되어 합산하지 않는다."""
import json
import math
from statistics import median


def latency_summary(rows):
    groups: dict[str, dict[str, list[float]]] = {}
    samples = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
            timings = payload.get("timings") or {}
            values = {key: timings.get(key) for key in ("beforeFinalizationMs", "verificationAndFormattingMs")}
            stages = timings.get("stages") or {}
            if isinstance(stages, dict):
                values.update({"stage:" + str(key): value.get("durationMs") for key, value in stages.items() if isinstance(value, dict)})
            valid = {key: float(value) for key, value in values.items()
                     if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0}
        except (TypeError, ValueError, AttributeError):
            continue
        if not valid:
            continue
        samples += 1
        for group in ("all", "mode:" + str(row["mode"] or "unknown"), "model:" + str(row["model_id"] or "unknown")):
            bucket = groups.setdefault(group, {})
            for key, value in valid.items():
                bucket.setdefault(key, []).append(value)
    return {
        "sampleCount": samples, "percentileMethod": "nearest-rank",
        "note": "단계 시간은 중첩됩니다. 합산하지 않으며 대기열·응답 전송·화면 렌더링은 제외합니다.",
        "groups": {group: {key: {"count": len(values), "medianMs": round(median(values), 2),
            "p95Ms": round(sorted(values)[math.ceil(len(values) * .95) - 1], 2)}
            for key, values in bucket.items()} for group, bucket in groups.items()},
    }
