#!/usr/bin/env python3
"""실제 저장본과 채팅 파이프라인을 대조한다. 감사 기록은 임시 DB에만 저장한다.

실행: PYTHONPATH=. .venv/bin/python scripts/accuracy/verify_snapshot_questions.py --model-id 모델ID
설정된 모델/API를 호출하지만 Mantis 원본은 변경하지 않는다.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
from time import perf_counter
from contextlib import ExitStack
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--offline", action="store_true", help="외부 요청을 차단하고 상태 의미 응답을 고정 픽스처로 대체")
    args = parser.parse_args()
    from backend.environment import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    with tempfile.TemporaryDirectory(prefix="assistant-question-qa-") as directory, ExitStack() as stack:
        os.environ["AUDIT_DB_PATH"] = str(Path(directory) / "audit.sqlite3")
        if args.offline:
            from unittest.mock import Mock
            from backend.intent import status_semantics
            from backend.sources.mantis_store import read_complete_store
            from copy import deepcopy
            snapshot = read_complete_store()
            if snapshot is None:
                raise RuntimeError("최신 전체 저장본이 없어 검증을 중단합니다.")
            by_id = {int(row["id"]): row for row in snapshot}
            stack.enter_context(patch("backend.sources.mantis.fetch_mantis_issue_by_id",
                side_effect=lambda issue_id: deepcopy(by_id.get(int(issue_id)))))
            def fixed_status(_model, _key, payload, **kwargs):
                evidence = "아직 손봐야 할 이슈"
                pending = evidence in payload["messages"][-1]["content"]
                fixture = {"status_intent": "open" if pending else None,
                           "evidence": evidence if pending else "", "needs_clarification": False}
                return Mock(json=lambda: {"choices": [{"message": {"content": json.dumps(fixture)}}]})
            stack.enter_context(patch.object(status_semantics, "post_model_chat", side_effect=fixed_status))
            stack.enter_context(patch.dict(os.environ, {"QUERY_PLANNER_ENABLED": "false"}))
            stack.enter_context(patch("requests.sessions.Session.request", side_effect=RuntimeError("외부 요청 차단")))
        from backend.sources.mantis_store import store_request_scope
        with store_request_scope():
            return run(args.model_id)


def run(model_id):
    from backend.schemas import ChatRequest, ChatMessage
    from backend.services.chat_service import chat_service
    from backend.sources.mantis_store import read_complete_store
    from backend.sources.mantis import issue_to_search_text
    from backend.team_scope import TEAM_MEMBERS
    rows = read_complete_store()
    if rows is None:
        raise RuntimeError("최신 전체 저장본이 없어 검증을 중단합니다.")

    # 자연어 추출기/조건 매처를 정답 계산에 쓰지 않고 원본 필드를 직접 비교한다.
    def oracle(scope):
        selected = []
        for row in rows:
            project = (row.get("project") or {}).get("name", "")
            status = (row.get("status") or {}).get("name", "")
            owner = row.get("handler") or {}
            if scope.get("nh") and "120NH(" not in project:
                continue
            if scope.get("exclude_nh") and "120NH(" in project:
                continue
            if scope.get("media"):
                fields = {f.get("field", {}).get("name"): f.get("value") for f in row.get("custom_fields") or []}
                media = str(fields.get("매체구분") or "").strip() or issue_to_search_text(row)
                aliases = {"MTS": ("mts", "엠티에스", "모바일", "mobile"), "HTS": ("hts", "h-able", "hable")}[scope["media"]]
                if not any(value in media.casefold() for value in aliases):
                    continue
            if scope.get("open") and status not in {"new", "feedback", "acknowledged", "confirmed", "assigned", "developed"}:
                continue
            if scope.get("status") and status != scope["status"]:
                continue
            if scope.get("owner") and scope["owner"] not in {owner.get("name"), owner.get("real_name")}:
                continue
            if scope.get("team") and not {owner.get("name"), owner.get("real_name")}.intersection(TEAM_MEMBERS):
                continue
            if scope.get("year") and not str(row.get("created_at") or "").startswith(scope["year"]):
                continue
            if scope.get("title") and scope["title"] not in str(row.get("summary") or ""):
                continue
            selected.append(int(row["id"]))
        return sorted(selected, reverse=True)

    base = {"nh": True, "media": "MTS", "open": True}
    cases = [
        ("NH MTS 미종료 이슈 몇 건이야?", base, "count", False),
        ("지금 NH에서 MTS 이슈 남은거 몇개야", base, "count", False),
        ("NH MTS 남아 있는 건 몇 건이야?", base, "count", False),
        ("NH MTS 아직 처리 안 된 거 몇 건이야?", base, "count", False),
        ("NH MTS 해결되지 않은 이슈 몇 건이야?", base, "count", False),
        ("NH MTS 아직 손봐야 할 이슈 몇 건이야?", base, "count", False),
        ("그중 이영희 담당만 몇 건이야?", {**base, "owner": "이영희"}, "count", True),
        ("그중 개발완료만 몇 건이야?", {**base, "open": False, "owner": "이영희", "status": "developed"}, "count", True),
        ("그중 전체 상태로 몇 건이야?", {"nh": True, "media": "MTS", "owner": "이영희"}, "count", True),
        ("NH MTS 미종료 이슈 최근 2개 보여줘", base, "list", False),
        ("NH MTS 전체 상태 이슈 몇 건이야?", {"nh": True, "media": "MTS"}, "count", False),
        ("NH MTS 종료된 이슈 몇 건이야?", {"nh": True, "media": "MTS", "status": "closed"}, "count", False),
        ("NH MTS 개발완료 이슈 몇 건이야?", {"nh": True, "media": "MTS", "status": "developed"}, "count", False),
        ("NH HTS 미종료 이슈 몇 건이야?", {"nh": True, "media": "HTS", "open": True}, "count", False),
        ("우리 팀 NH 미종료 이슈 몇 건이야?", {"nh": True, "team": True, "open": True}, "count", False),
        ("우리 팀 NH 제외 미종료 이슈 몇 건이야?", {"exclude_nh": True, "team": True, "open": True}, "count", False),
        ("NH 2025년 등록된 이슈 몇 건이야?", {"nh": True, "year": "2025"}, "count", False),
        ('NH 제목에 "차트" 포함된 미종료 이슈 2개 보여줘', {"nh": True, "title": "차트", "open": True}, "list", False),
        ("그중 MTS만 보여줘", {**base, "title": "차트"}, "list", True),
        ("그중 몇 건이야?", {**base, "title": "차트"}, "count", True),
    ]
    messages = []
    audit_id = None
    failures = 0
    for q, scope, kind, followup in cases:
        if not followup:
            messages, audit_id = [], None
        messages.append(ChatMessage(role="user", content=q))
        start = perf_counter()
        response = chat_service.build_response(ChatRequest(modelId=model_id, messages=messages,
            contextAuditId=audit_id, abTest=False, tools={"mantis": True, "vectorDb": False, "notion": False}),
            user={"uid": "isolated-question-qa", "displayName": "검증 사용자"})
        elapsed = round((perf_counter() - start) * 1000)
        expected = oracle(scope)
        if kind == "count":
            count_key = "matched_count" if response.get("raw", {}).get("mode") == "deterministic_work_report" else "total_count"
            actual = next((s.get("meta", {}).get(count_key) for s in response.get("retrieval", {}).get("stages", [])
                           if s.get("meta", {}).get(count_key) is not None), None)
            passed = actual == len(expected)
        else:
            actual = [int(s["issue_id"]) for s in response.get("sources", []) if s.get("issue_id")]
            display_limit = 2 if "2개" in q else 5
            passed = actual == expected[:display_limit]
        passed = passed and response.get("verification", {}).get("status") == "pass"
        failures += not passed
        print(json.dumps({"question": q, "pass": passed, "expectedCount": len(expected), "actual": actual,
            "elapsedMs": elapsed, "mode": response.get("raw", {}).get("mode"),
            "verification": response.get("verification", {}).get("status"), "timings": response.get("timings")}, ensure_ascii=False), flush=True)
        audit_id = response.get("auditId")
        messages.append(ChatMessage(role="assistant", content=response["content"]))
    print(json.dumps({"total": len(cases), "failures": failures, "fetchedAt": rows.meta["fetchedAt"]}), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
