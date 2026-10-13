#!/usr/bin/env python3
"""실제 채팅 경로의 다중 턴 범위를 원문에서 독립 계산한 정답과 비교한다.

PYTHONPATH=. .venv/bin/python scripts/accuracy/verify_followup_scopes.py --model-id 모델ID
회사 모델을 사용하고 감사 기록은 임시 DB에만 저장한다.
"""

import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
from time import perf_counter, sleep


PROJECTS = {"NH": "120NH(NH증권)", "KB증권": "112KBStar(KB증권)", "SK증권": "108SK(SK증권)"}


def scope(customer=None, period=None, owner=None, platform=None):
    return {"customer": customer, "period": period, "owner": owner, "platform": platform}


def conversations():
    return {
        "replace": [
            ("2025년 NH증권 MTS 이영희 담당 이슈 몇 건이야?", scope("NH", "2025", "이영희", "mts")),
            ("그럼 고객사만 KB증권으로 바꿔서 몇 건이야?", scope("KB증권", "2025", "이영희", "mts")),
            ("그럼 담당자는 하영훈으로 바꿔서 몇 건이야?", scope("KB증권", "2025", "하영훈", "mts")),
            ("플랫폼만 HTS로 바꿔서 몇 건이야?", scope("KB증권", "2025", "하영훈", "hts")),
            ("그럼 담당자만 우동헌으로 바꿔서 몇 건이야?", scope("KB증권", "2025", "우동헌", "hts")),
            ("그럼 기간만 2024년으로 바꿔서 몇 건이야?", scope("KB증권", "2024", "우동헌", "hts")),
            ("그럼 고객사만 SK증권으로 바꿔서 몇 건이야?", scope("SK증권", "2024", "우동헌", "hts")),
            ("그 조건으로 최근 3건 보여줘", scope("SK증권", "2024", "우동헌", "hts")),
        ],
        "clear": [
            ("2025년 SK증권 HTS 우동헌 담당 이슈 몇 건이야?", scope("SK증권", "2025", "우동헌", "hts")),
            ("그럼 담당자 조건은 빼고 몇 건이야?", scope("SK증권", "2025", None, "hts")),
            ("그럼 플랫폼 조건도 빼고 몇 건이야?", scope("SK증권", "2025")),
            ("그럼 고객사 제한 없이 몇 건이야?", scope(None, "2025")),
            ("그럼 기간 제한 없이 몇 건이야?", scope()),
        ],
        "period": [
            ("2025년 NH증권 HTS 우동헌 담당 이슈 몇 건이야?", scope("NH", "2025", "우동헌", "hts")),
            ("그럼 6월은 몇 건이야?", scope("NH", "2025-06", "우동헌", "hts")),
            ("그럼 7월은 몇 건이야?", scope("NH", "2025-07", "우동헌", "hts")),
            ("그럼 6월에 등록된 이슈는 몇 건이야?", scope("NH", "2025-06", "우동헌", "hts")),
            ("그럼 2024년은 몇 건이야?", scope("NH", "2024", "우동헌", "hts")),
            ("그럼 작년은 몇 건이야?", scope("NH", str(date.today().year - 1), "우동헌", "hts")),
        ],
        "retain": [
            ("2025년 SK증권 HTS 우동헌 담당 이슈 몇 건이야?", scope("SK증권", "2025", "우동헌", "hts")),
            ("고객사와 기간은 그대로 두고 담당자만 정희진으로 바꿔서 몇 건이야?", scope("SK증권", "2025", "정희진", "hts")),
            ("나머지는 그대로 두고 플랫폼만 MTS로 바꿔서 몇 건이야?", scope("SK증권", "2025", "정희진", "mts")),
            ("그럼 HTS 말고 MTS는 몇 건이야?", scope("SK증권", "2025", "정희진", "mts")),
            ("2024년 NH증권 MTS 이영희 담당 이슈 몇 건이야?", scope("NH", "2024", "이영희", "mts")),
            ("그중 최근 3건 보여줘", scope("NH", "2024", "이영희", "mts")),
        ],
    }


def oracle(rows, wanted):
    result = []
    for row in rows:
        if wanted["customer"] and row.get("project", {}).get("name") != PROJECTS[wanted["customer"]]:
            continue
        if wanted["period"] and not str(row.get("created_at") or "").startswith(wanted["period"]):
            continue
        if wanted["owner"] and row.get("handler", {}).get("real_name") != wanted["owner"]:
            continue
        if wanted["platform"]:
            media = next((str(f.get("value") or "") for f in row.get("custom_fields") or []
                          if f.get("field", {}).get("name") == "매체구분"), "")
            if not media:
                media = " ".join([str(row.get(k) or "") for k in ("summary", "description", "additional_information", "steps_to_reproduce")]
                                 + [str(n.get("text") or "") for n in row.get("notes") or []])
            aliases = {"hts": r"HTS|H-able|Hable", "mts": r"MTS|엠티에스|모바일|mobile"}
            if not re.search(aliases[wanted["platform"]], media, re.I):
                continue
        result.append(int(row["id"]))
    return sorted(result, reverse=True)


def actual_scope(constraints):
    period = constraints.get("created_at") or {}
    return {"customer": constraints.get("customer_key"), "period": period,
            "owner": (constraints.get("people") or {}).get("names") or [],
            "platform": constraints.get("platform_key")}


def scope_matches(actual, wanted):
    from calendar import monthrange
    period = wanted["period"]
    dates = {}
    if period:
        year, *month = [int(part) for part in period.split("-")]
        dates = {"from": f"{period}-01" if month else f"{year}-01-01",
                 "to": f"{period}-{monthrange(year, month[0])[1]}" if month else f"{year}-12-31"}
    customer = {"NH증권": "NH", "NH투자증권": "NH", "kb": "KB증권", "SK": "SK증권"}.get(actual["customer"], actual["customer"])
    return (customer == wanted["customer"] and actual["platform"] == wanted["platform"]
            and actual["owner"] == ([wanted["owner"]] if wanted["owner"] else [])
            and {k: v for k, v in actual["period"].items() if k in {"from", "to"}} == dates)


def run(args):
    from fastapi import HTTPException
    from backend.schemas import ChatMessage, ChatRequest
    from backend.services.chat_service import chat_service
    from backend.sources.mantis_store import read_complete_store, store_request_scope
    destination = Path(args.output)
    with store_request_scope():
        rows = read_complete_store()
        if rows is None:
            raise RuntimeError("최신 전체 원문 저장본이 필요합니다.")
        report = {"modelId": args.model_id, "snapshotAt": rows.meta.get("fetchedAt"), "snapshotCount": len(rows), "results": []}
        for name, cases in conversations().items():
            if args.chain and args.chain != name:
                continue
            messages, audit_id = [], None
            for turn, (question, wanted) in enumerate(cases, 1):
                messages.append(ChatMessage(role="user", content=question))
                expected = oracle(rows, wanted)
                started = perf_counter()
                request = ChatRequest(modelId=args.model_id, messages=messages,
                                      contextAuditId=audit_id, abTest=False, showSources=True, searchLimit=3)
                for attempt in range(3):
                    try:
                        response = chat_service.build_response(request,
                            user={"uid": "isolated-followup-scopes-qa", "displayName": "후속 조건 검증"})
                        break
                    except HTTPException as error:
                        if error.status_code != 429 or attempt == 2:
                            raise
                        sleep(min(60, int((error.headers or {}).get("Retry-After", "2")) + 1))
                retrieval = response.get("retrieval") or {}
                plan = retrieval.get("query_plan") or {}
                constraints = plan.get("resolved_constraints") or retrieval.get("constraints") or {}
                actual = actual_scope(constraints)
                ids = [int(source["issue_id"]) for source in response.get("sources") or [] if source.get("issue_id")]
                count = next((stage.get("meta", {}).get("total_count", stage.get("meta", {}).get("matched_count"))
                              for stage in retrieval.get("stages") or [] if
                              stage.get("meta", {}).get("total_count", stage.get("meta", {}).get("matched_count")) is not None), None)
                listing = "3건" in question
                result_ok = ids == expected[:3] if listing else count == len(expected)
                content = response.get("content") or ""
                answer_ids = [int(value) for value in re.findall(r"\*\*ID\*\*:\s*(\d+)", content)]
                answer_counts = [int(value.replace(",", "")) for value in re.findall(r"총\s*([\d,]+)건", content)]
                answer_ok = answer_ids == expected[:3] if listing else answer_counts == [len(expected)]
                result = {"chain": name, "turn": turn, "question": question, "expectedScope": wanted,
                          "actualScope": actual, "scopePass": scope_matches(actual, wanted), "resultPass": result_ok,
                          "answerPass": answer_ok,
                          "expectedCount": len(expected), "actualCount": count, "expectedIds": expected[:3], "actualIds": ids,
                          "seconds": round(perf_counter() - started, 2), "mode": response.get("raw", {}).get("mode"),
                          "content": response.get("content"), "queryPlan": plan,
                          "checks": (response.get("verification") or {}).get("checks", []),
                          "stageMeta": [{"source": s.get("source"), "meta": {k:v for k,v in s.get("meta", {}).items()
                                         if k not in {"scanned_issue_ids", "matched_issue_ids", "matched_customer_groups"}}}
                                        for s in retrieval.get("stages") or []]}
                result["validationPass"] = all(check["status"] != "fail" for check in result["checks"])
                result["pass"] = result["scopePass"] and result_ok and answer_ok and result["validationPass"]
                report["results"].append(result)
                destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k:v for k,v in result.items() if k not in {"content", "queryPlan", "checks", "stageMeta"}}, ensure_ascii=False), flush=True)
                audit_id = response.get("auditId")
                messages.append(ChatMessage(role="assistant", content=response.get("content") or ""))
        failed = sum(not row["pass"] for row in report["results"])
        print(json.dumps({"total": len(report["results"]), "failed": failed, "output": str(destination)}), flush=True)
        return bool(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--chain", choices=list(conversations()))
    parser.add_argument("--output", default="runtime/followup_scopes_verification.json")
    args = parser.parse_args()
    from backend.environment import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    with tempfile.TemporaryDirectory(prefix="assistant-followup-scopes-") as directory:
        os.environ["AUDIT_DB_PATH"] = str(Path(directory) / "audit.sqlite3")
        return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
