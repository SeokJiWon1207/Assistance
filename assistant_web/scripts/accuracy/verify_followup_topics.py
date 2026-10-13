#!/usr/bin/env python3
"""실제 회사 모델과 Mantis 저장본으로 주제 교체 대화를 검증한다.

PYTHONPATH=. .venv/bin/python scripts/accuracy/verify_followup_topics.py --model-id 모델ID
감사 기록은 임시 DB, 결과는 runtime/followup_topic_verification.json에 저장한다.
"""

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
from time import perf_counter


QUESTIONS = ["HTS에서 VB 스크립트를 자바 스크립트로 바꿔달라는 이슈 있었어?",
             "그럼 VBScript 관련해서 들어온 곳은?", "그중 SK증권만 보여줘"]


def run(model_id):
    from backend.schemas import ChatMessage, ChatRequest
    from backend.services.chat_service import chat_service
    from backend.sources.mantis_store import read_complete_store, store_request_scope

    with store_request_scope():
        rows = read_complete_store()
        if rows is None:
            raise RuntimeError("최신 전체 원문 저장본을 확보하지 못했습니다.")
        # 제품의 조건 추출기/매처를 사용하지 않고 원본 필드와 노트를 직접 대조한다.
        expected = []
        for row in rows:
            text = " ".join([str(row.get(key) or "") for key in
                             ("summary", "description", "additional_information", "steps_to_reproduce")]
                            + [str(note.get("text") or "") for note in row.get("notes") or []])
            media = next((str(field.get("value") or "") for field in row.get("custom_fields") or []
                          if field.get("field", {}).get("name") == "매체구분"), "")
            if re.search(r"HTS|H-able|Hable", media or text, re.I) and re.search(r"VB\s*Scripts?", text, re.I):
                expected.append({"id": row["id"], "project": row.get("project", {}).get("name"),
                                 "title": row.get("summary")})
        expected_ids = {row["id"] for row in expected}
        messages, audit_id, results = [], None, []
        for index, question in enumerate(QUESTIONS):
            messages.append(ChatMessage(role="user", content=question))
            start = perf_counter()
            response = chat_service.build_response(ChatRequest(modelId=model_id, messages=messages,
                contextAuditId=audit_id, abTest=False, searchLimit=10, showSources=True),
                user={"uid": "isolated-followup-topic-qa", "displayName": "후속 질문 검증"})
            actual_ids = {source["issue_id"] for source in response.get("sources") or [] if source.get("issue_id")}
            wanted = (set() if index == 0 else expected_ids if index == 1 else
                      {row["id"] for row in expected if "SK" in str(row["project"])})
            content = response.get("content", "")
            mode = response.get("raw", {}).get("mode", "")
            passed = (actual_ids == wanted and all(str(identifier) in content for identifier in wanted)
                      and mode not in {"status_clarification", "search_terms_clarification", "empty_evidence"})
            if index > 0:
                passed = passed and all((source.get("detail") or {}).get("matching_evidence")
                                        for source in response.get("sources") or [])
                passed = passed and all(check["status"] == "pass" for check in
                    (response.get("verification") or {}).get("checks", [])
                    if check["id"] in {"constraints", "issue_ids", "issue_fields", "constraint_grounding"})
            result = {"question": question, "pass": passed, "expectedIds": sorted(wanted),
                      "actualIds": sorted(actual_ids), "elapsedSeconds": round(perf_counter() - start, 2),
                      "content": content, "mode": response.get("raw", {}).get("mode"),
                      "queryPlan": response.get("retrieval", {}).get("query_plan"),
                      "verification": response.get("verification"),
                      "sources": response.get("sources")}
            results.append(result)
            print(json.dumps({key: value for key, value in result.items() if key not in {"queryPlan", "verification", "sources"}}, ensure_ascii=False), flush=True)
            audit_id = response.get("auditId")
            messages.append(ChatMessage(role="assistant", content=content))
        destination = Path(__file__).resolve().parents[2] / "runtime" / "followup_topic_verification.json"
        destination.write_text(json.dumps({"modelId": model_id, "snapshotAt": rows.meta.get("fetchedAt"),
                              "snapshotCount": len(rows), "expected": expected, "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
        print(str(destination), flush=True)
        return 0 if all(result["pass"] for result in results) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()
    from backend.environment import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    with tempfile.TemporaryDirectory(prefix="assistant-followup-qa-") as directory:
        os.environ["AUDIT_DB_PATH"] = str(Path(directory) / "audit.sqlite3")
        return run(args.model_id)


if __name__ == "__main__":
    raise SystemExit(main())
