#!/usr/bin/env python3
"""Audit live Notion body search against independently collected raw block evidence.

No production matcher, index, planner or body limits are used to derive the
oracle. A positive result means the known target was found, not global recall.
Unknown pages are independently fetched; failed/partial reads remain unknown.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
import uuid

APP_DIR = Path(__file__).resolve().parents[2]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


@dataclass
class BodyCase:
    id: str
    question: str
    terms: list[str]
    required_page_ids: list[str]
    required_kind: str
    category: str = "global_keyword"
    expected_present: bool = True
    exact_title: str = ""
    database_id: str = ""
    source_blocks: list[dict[str, Any]] = field(default_factory=list)


def normalize_id(value: Any) -> str:
    return str(value or "").replace("-", "").casefold()


def contains(text: Any, terms: list[str]) -> bool:
    folded = str(text or "").casefold()
    return all(term.casefold() in folded for term in terms)


def contains_search_phrase(text: Any, terms: list[str]) -> bool:
    """독립 원문의 한글 음절 사이 띄어쓰기만 동등하게 평가한다.

    생산 정규화 함수나 색인을 사용하지 않는다. 호출자는 블록 단위로
    검사해야 하며, 영문 코드 식별자·문장부호의 차이는 그대로 보존한다.
    """
    def folded(value: Any) -> str:
        return re.sub(r"(?<=[가-힣])\s+(?=[가-힣])", "", str(value or "")).casefold()

    return all(folded(term) in folded(text) for term in terms)


def raw_block_text(block: dict[str, Any]) -> str:
    """Read raw Notion rich text/cells, independent of backend mapping code."""
    data = block.get(block.get("type", "")) or {}
    groups = data.get("cells") or [data.get("rich_text") or []]
    return " | ".join("".join(str(item.get("plain_text") or
        (item.get("text") or {}).get("content") or "") for item in group) for group in groups)


def verified_segments(page: dict[str, Any]) -> list[dict[str, Any]]:
    raw = {normalize_id(block.get("id")): block for block in page.get("blocks") or []}
    verified = []
    for segment in page.get("segments") or []:
        block = raw.get(normalize_id(segment.get("block_id")))
        if block and block.get("type") == segment.get("type"):
            text = raw_block_text(block)
            if text.strip():
                verified.append({**segment, "text": text})
    return verified


def case_variants(case: BodyCase, page: dict[str, Any], *, include_probes: bool) -> list[BodyCase]:
    cases = [case]
    prefix = case.id.removesuffix("-global")
    label = {"paragraph": "본문", "table_row": "표", "code": "코드"}[case.required_kind]
    term = case.terms[0]
    title = str(page.get("title") or "")
    title_is_quotable = bool(title) and not any(quote in title for quote in "\"'“”‘’")
    if title_is_quotable:
        cases.append(replace(case, id=f"{prefix}-title", category="exact_title_and_body",
            exact_title=title, question=f"노션 '{title}' 문서에서 {label}에 {term} 포함된 문서 찾아줘"))
    if include_probes:
        cases.append(replace(case, id=f"{prefix}-bare", category="plain_keyword",
            question=f"노션에서 {term} 찾아줘"))
        cases.append(replace(case, id=f"{prefix}-quoted", category="quoted_body_keyword",
            question=f'노션 {label}에 "{term}" 포함된 문서 찾아줘'))
        if page.get("complete") is True and title_is_quotable:
            sentinel = "notionabsent" + hashlib.sha256(str(page["page_id"]).encode()).hexdigest()[:12]
            if sentinel.casefold() not in json.dumps(page, ensure_ascii=False).casefold():
                cases.append(replace(case, id=f"{prefix}-negative", category="exact_title_negative",
                    expected_present=False, terms=[sentinel], exact_title=title,
                    question=f"노션 '{title}' 문서에서 {label}에 {sentinel} 포함된 문서 찾아줘"))
    return cases


def build_cases_from_spec(evidence: dict[str, Any], specification: dict[str, Any]) -> list[BodyCase]:
    """Verify manually selected tokens/IDs against raw evidence before trusting them."""
    pages = {normalize_id(page["page_id"]): page for page in evidence.get("pages") or []}
    cases = []
    seen_kinds: set[str] = set()
    for selected in specification.get("cases") or []:
        page = pages.get(normalize_id(selected.get("page_id")))
        term = str(selected.get("query_token") or "")
        kind = selected.get("block_type")
        if not page or not term or kind not in {"paragraph", "table_row", "code"}:
            raise ValueError("Case specification requires an evidenced page, term and supported block type")
        segments = [segment for segment in verified_segments(page)
                    if segment["type"] == kind and contains(segment["text"], [term])]
        expected = {normalize_id(block["block_id"]) for block in selected.get("expected_match_blocks") or []}
        actual = {normalize_id(segment["block_id"]) for segment in segments}
        if not segments or not expected or not expected.issubset(actual):
            raise ValueError("Selected token/block IDs are not supported by raw evidence")
        for candidate in evidence.get("pages") or []:
            metadata = str(candidate.get("title") or "") + json.dumps(candidate.get("properties") or {}, ensure_ascii=False)
            if contains(metadata, [term]):
                raise ValueError("Selected body-only term occurs in sampled title/properties")
            if any(segment["type"] != kind and contains(segment["text"], [term])
                   for segment in verified_segments(candidate)):
                raise ValueError("Selected typed-body term also occurs in another sampled block type")
        case = BodyCase(id=str(selected["case_id"]) + "-global",
            question=f"노션에서 {term} 내용이 있는 문서 찾아줘", terms=[term],
            required_page_ids=[page["page_id"]], required_kind=kind,
            source_blocks=[{**segment, "page_id": page["page_id"], "page_title": page.get("title", ""),
                            "page_url": page.get("url", ""), "body_only": True} for segment in segments])
        cases.extend(case_variants(case, page, include_probes=kind not in seen_kinds))
        seen_kinds.add(kind)
    return cases


def suitable_plain_probe_token(token: str) -> bool:
    """조사·용언 활용 어절을 원문 그대로의 일반 검색 키워드 정답으로 삼지 않는다.

    생산 검색기의 형태소 정제를 정답 생성에 빌리지 않고 모호한 표본을 제외한다.
    인용문에서의 리터럴 검색은 별도 명시적 사례로 검증할 수 있다.
    """
    return not (re.fullmatch(r"[가-힣]+", token) and
                token.endswith(("에서", "에서는", "으로", "으로는", "에게", "에게는", "까지", "부터", "하는")))


def build_cases(evidence: dict[str, Any], per_kind: int = 3) -> list[BodyCase]:
    cases: list[BodyCase] = []
    word_pattern = r"[A-Za-z_][A-Za-z0-9_]{3,}|[가-힣]{2,}"
    frequency: Counter[str] = Counter()
    for page in evidence.get("pages") or []:
        frequency.update({token.casefold() for segment in verified_segments(page)
                          for token in re.findall(word_pattern, segment["text"])})

    def rank(token: str) -> tuple[Any, ...]:
        return (frequency[token.casefold()], "_" not in token, -len(token), token)

    for kind in ("paragraph", "table_row", "code"):
        candidates = []
        for page in evidence.get("pages") or []:
            metadata = (str(page.get("title") or "") + json.dumps(page.get("properties") or {},
                         ensure_ascii=False)).casefold()
            for segment in verified_segments(page):
                if segment["type"] != kind:
                    continue
                tokens = list(dict.fromkeys(re.findall(word_pattern, segment["text"])))
                tokens = [token for token in tokens if token.casefold() not in metadata
                          and suitable_plain_probe_token(token)
                          and not token.endswith(("합니다", "입니다", "됩니다", "있습니다", "없습니다"))]
                if not tokens:
                    continue
                # Distinctive identifiers first; no fabricated word or expected ID.
                tokens.sort(key=rank)
                candidates.append((page, segment, tokens[0]))
        candidates.sort(key=lambda candidate: rank(candidate[2]))
        # Prefer different pages, then distinct blocks if a kind has few pages.
        selected = []
        seen_pages: set[str] = set()
        for candidate in candidates:
            identifier = normalize_id(candidate[0].get("page_id"))
            if identifier not in seen_pages:
                selected.append(candidate)
                seen_pages.add(identifier)
        selected += [candidate for candidate in candidates if candidate not in selected]
        for index, (page, segment, term) in enumerate(selected[:per_kind], 1):
            case = BodyCase(
                id=f"{kind}-{index}-global", question=f"노션에서 {term} 내용이 있는 문서 찾아줘",
                terms=[term], required_page_ids=[str(page["page_id"])], required_kind=kind,
                source_blocks=[{"page_id": page["page_id"], "block_id": segment["block_id"],
                    "type": kind, "text": segment["text"], "path": segment.get("path") or [],
                    "page_url": page.get("url", ""), "page_title": page.get("title", ""),
                    "body_only": True}],
            )
            cases.extend(case_variants(case, page, include_probes=index == 1))
    return cases


def retrieved_pages(retrieval: dict[str, Any]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for stage in retrieval.get("stages") or []:
        if stage.get("source") == "notion":
            for page in stage.get("notion_pages") or []:
                if page.get("id"):
                    rows[normalize_id(page["id"])] = page
    return list(rows.values())


def grade_retrieval(case: BodyCase, retrieval: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    pages = retrieved_pages(retrieval)
    actual_ids = {normalize_id(page["id"]) for page in pages}
    required_ids = {normalize_id(identifier) for identifier in case.required_page_ids}
    oracle = {normalize_id(page["page_id"]): page for page in evidence.get("pages") or []}
    stages = [stage for stage in retrieval.get("stages") or [] if stage.get("source") == "notion"]
    complete = bool(stages) and all(stage.get("meta", {}).get("search_complete") is True for stage in stages)
    errors = [stage.get("reason") or stage.get("status") for stage in stages
              if stage.get("status") == "error" or (stage.get("meta") or {}).get("errors")]
    false_positives, unverified, matching, spacing_equivalent = [], [], [], []
    for page in pages:
        source = oracle.get(normalize_id(page["id"]))
        if source is None:
            unverified.append(page["id"])
            continue
        title_matches = not case.exact_title or source.get("title") == case.exact_title
        # Paragraph identifies the known target's provenance. A BODY question
        # also legitimately matches code/table blocks in other returned pages;
        # only explicit TABLE/CODE questions restrict candidate block types.
        body_matches = [segment for segment in verified_segments(source)
                        if contains_search_phrase(segment["text"], case.terms) and
                        (case.category in {"global_keyword", "plain_keyword"} or case.required_kind == "paragraph"
                         or segment["type"] == case.required_kind)]
        metadata_matches = case.category == "plain_keyword" and contains_search_phrase(
            str(source.get("title") or "") + "\n" + json.dumps(source.get("properties") or {}, ensure_ascii=False),
            case.terms,
        )
        if title_matches and (body_matches or metadata_matches):
            matching.append({"page_id": page["id"], "blocks": body_matches, "metadata_match": metadata_matches})
            literal_body = any(contains(segment["text"], case.terms) for segment in body_matches)
            literal_metadata = metadata_matches and contains(
                str(source.get("title") or "") + "\n" + json.dumps(source.get("properties") or {}, ensure_ascii=False),
                case.terms,
            )
            if not literal_body and not literal_metadata:
                spacing_equivalent.append(page["id"])
        elif source.get("complete") is True or not title_matches:
            false_positives.append(page["id"])
        else:
            unverified.append(page["id"])
    recalled = required_ids.issubset(actual_ids)
    matching_ids = {normalize_id(row["page_id"]) for row in matching}
    expected_kind_verified = all(any(segment["type"] == case.required_kind and contains_search_phrase(segment["text"], case.terms)
        for segment in verified_segments(oracle.get(identifier, {}))) for identifier in required_ids)
    content_present = all(any(normalize_id(page["id"]) == identifier and contains_search_phrase(page.get("content"), case.terms)
                              for page in pages) for identifier in required_ids)
    failures = []
    if case.expected_present:
        if not recalled:
            failures.append("known_target_missing")
        if not required_ids.issubset(matching_ids) or not expected_kind_verified:
            failures.append("target_block_evidence_missing")
        if not content_present:
            failures.append("returned_matching_content_missing")
    elif actual_ids & required_ids:
        failures.append("known_negative_page_returned")
    if false_positives:
        failures.append("independent_false_positive")
    negative_oracle_complete = all(oracle.get(identifier, {}).get("complete") is True for identifier in required_ids)
    incomplete = bool(errors or unverified or not stages or
                      (not case.expected_present and (not complete or not negative_oracle_complete)))
    passed = not failures and not incomplete
    return {
        "passed": passed, "outcome": "failed" if failures else "incomplete" if incomplete else "passed",
        "failed_checks": failures, "known_target_recalled": recalled if case.expected_present else None,
        "known_negative_excluded": not bool(actual_ids & required_ids) if not case.expected_present else None,
        "retrieved_page_ids": [page["id"] for page in pages], "returned_matching_content": content_present,
        "required_kind_verified": expected_kind_verified if case.expected_present else None,
        "false_positive_page_ids": false_positives, "unverified_page_ids": unverified,
        "korean_spacing_equivalent_page_ids": spacing_equivalent,
        "independently_matching_blocks": matching,
        "search_complete": complete, "full_recall_verified": False,
        "oracle_scope": "known_positive_pages" if case.expected_present else "known_page_exclusion",
        "body_scan_attempted_count": sum((stage.get("meta") or {}).get("body_scan_attempted_count", 0) for stage in stages),
        "body_scan_limits": [(stage.get("meta") or {}).get("body_scan_limit") for stage in stages],
        "dependency_errors": errors, "raw_stage_metadata": [stage.get("meta") or {} for stage in stages],
    }


def fetch_oracle_page(page_id: str) -> dict[str, Any]:
    """Independent bounded REST traversal; skipped/failed children stay incomplete."""
    import requests
    from backend.config import NOTION_API_BASE, get_notion_headers, notion_request_slot
    page: dict[str, Any] = {"page_id": page_id, "segments": [], "blocks": [], "complete": True, "errors": []}
    # 검증용 원문은 대화 검색의 지연 한도와 독립적으로 깊고 큰 문서도 읽는다.
    # 한도에 도달하면 여전히 미완료이며, 이를 확정 음성으로 사용하지 않는다.
    deadline = time.monotonic() + 300
    requests_left = 2000
    originals: set[str] = set()
    references: set[str] = set()

    def read(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal requests_left
        if requests_left <= 0 or time.monotonic() >= deadline:
            raise TimeoutError("oracle_budget_exhausted")
        requests_left -= 1
        notion_request_slot()
        response = requests.get(f"{NOTION_API_BASE}/{path}", headers=get_notion_headers(),
                                params=params, timeout=min(15, max(1, deadline - time.monotonic())))
        response.raise_for_status()
        return response.json()

    def walk(block_id: str, path: list[str]) -> None:
        if len(path) > 96:
            page["complete"] = False
            return
        cursor = None
        cursors: set[str] = set()
        while True:
            params = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = read(f"blocks/{block_id}/children", params)
            for block in data.get("results") or []:
                page["blocks"].append(block)
                parent = block.get("parent") or {}
                parent_kind = parent.get("type")
                if parent_kind and (parent_kind not in {"page_id", "block_id"} or
                                    normalize_id(parent.get(parent_kind)) != normalize_id(block_id)):
                    page["complete"] = False
                    continue
                kind = block.get("type")
                text = raw_block_text(block)
                if text:
                    page["segments"].append({"block_id": block["id"], "type": kind, "text": text, "path": path})
                if kind == "synced_block":
                    reference = (block.get(kind) or {}).get("synced_from")
                    if reference:
                        references.add(normalize_id(reference.get("block_id")))
                        continue
                    originals.add(normalize_id(block["id"]))
                if kind in {"child_page", "child_database", "unsupported"}:
                    page["complete"] = False
                elif block.get("has_children"):
                    walk(block["id"], [*path, block["id"]])
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor or cursor in cursors:
                page["complete"] = False
                return
            cursors.add(cursor)

    try:
        raw = read(f"pages/{page_id}")
        page.update(raw_page=raw, properties=raw.get("properties") or {}, url=raw.get("url", ""))
        page["title"] = "".join(str(item.get("plain_text") or (item.get("text") or {}).get("content") or "")
            for prop in page["properties"].values() if prop.get("type") == "title" for item in prop.get("title") or [])
        walk(page_id, [page_id])
        if references - originals:
            page["complete"] = False
    except (requests.RequestException, ValueError, TimeoutError) as exc:
        page["complete"] = False
        # Never persist request headers or full dependency exception strings.
        page["errors"].append(type(exc).__name__)
    return page


def model_evidence_check(case: BodyCase, retrieval: dict[str, Any]) -> bool | None:
    if not case.expected_present:
        return None
    from backend.retrieval import build_model_retrieval_context
    context = build_model_retrieval_context(retrieval, 5)
    return all(any(normalize_id(page.get("id")) == normalize_id(identifier)
        and contains(page.get("content"), case.terms) for page in context.get("notion_pages") or [])
        for identifier in case.required_page_ids)


def planned_control(case: BodyCase) -> dict[str, Any]:
    return {"source": "notion", "operation": "search", "terms": case.terms,
            "grounding_text": case.question, "filters": {}, "filter_clauses": [
                {"field": "keyword", "operator": "contains", "value": term} for term in case.terms]}


def grade_chat_response(case: BodyCase, response: dict[str, Any],
                        graded: dict[str, Any]) -> dict[str, Any]:
    """Check source attachment and a few contradictions, never semantic accuracy.

    Sources are attached from retrieval independently of the answer text. Even
    passing these checks leaves generated prose unverified and needs review.
    """
    attached = {normalize_id(source.get("page_id") or source.get("id"))
                for source in response.get("sources") or [] if source.get("type") == "notion"}
    required = {normalize_id(identifier) for identifier in case.required_page_ids}
    target_attached = required.issubset(attached) if case.expected_present else None
    wiring_passed = graded["passed"] and (target_attached if case.expected_present else True)
    answer = str(response.get("content") or "").strip()
    # Quoted titles/keywords may themselves contain words such as "설명해줘".
    question = re.sub(r'''"[^"]*"|'[^']*'|“[^”]*”|‘[^’]*’''', " ", case.question)
    body_answer_requested = bool(re.search(r"(?:설명|요약|해설)\s*(?:해|하|부탁|$)", question))
    answer_contains_terms = contains(answer, case.terms)
    failures, unverified = [], []
    if not answer:
        failures.append("chat_answer_empty")
    # Deliberately narrow to explicit no-result sentences. This is a regression
    # guard, not a language-model judge or an exhaustive contradiction detector.
    no_result_claim = re.search(
        r"(?im)^\s*(?:[-*>🔍]\s*)?(?:(?:Notion|노션)(?:\s*DB)?(?:에서는|에서)\s*)?"
        r"(?:조회한\s*범위에는\s*)?(?:(?:해당|요청한|요청하신)\s*)?"
        r"(?:(?:조건에\s*맞는|일치하는)\s*)?(?:문서|페이지|항목|자료|검색\s*결과|결과)"
        r"(?:은|는|이|가|을|를)?\s*(?:없(?:습니다|어요|다)|존재하지\s*않습니다|"
        r"찾지\s*못(?:했|하)|0\s*(?:건|개))",
        answer,
    )
    if case.expected_present and graded.get("known_target_recalled") and no_result_claim:
        failures.append("chat_answer_denies_known_match")
    if body_answer_requested and case.expected_present:
        if graded.get("model_evidence_present") is not True:
            unverified.append("chat_model_evidence_missing")
        if not answer_contains_terms:
            unverified.append("chat_answer_keyword_missing")
    failed_checks = list(graded["failed_checks"])
    if graded["passed"] and not wiring_passed:
        failed_checks.append("chat_target_source_missing")
    failed_checks.extend([*failures, *unverified])
    outcome = graded["outcome"]
    if graded["passed"]:
        outcome = "failed" if not wiring_passed or failures else "answer_unverified" if unverified else "passed"
    return {**graded, "retrieval_passed": graded["passed"],
            "measurement": "source_wiring_and_limited_answer_checks",
            "chat_attaches_known_target_source": target_attached,
            "chat_source_wiring_passed": wiring_passed,
            "body_answer_requested": body_answer_requested,
            "answer_contains_terms": answer_contains_terms,
            "chat_answer_outcome": "failed" if failures else "answer_unverified",
            "chat_answer_check_failures": [*failures, *unverified],
            "answer_accuracy_verified": False,
            "passed": bool(wiring_passed and not failures and not unverified),
            "outcome": outcome, "failed_checks": failed_checks}


def run_case(case: BodyCase, evidence: dict[str, Any], mode: str = "natural_retrieval",
             chat_model_id: str = "", user_uid: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    try:
        from backend.retrieval import run_retrieval_pipeline
        response: dict[str, Any] = {}
        from backend.execution import checkpoint, execution_scope
        with execution_scope() as budget:
            request_budget_seconds = round(budget.remaining(), 3)
            if mode == "natural_chat":
                from backend.schemas import ChatMessage, ChatRequest
                from backend.services.chat_service import chat_service
                response = chat_service.build_response(ChatRequest(
                    modelId=chat_model_id, messages=[ChatMessage(role="user", content=case.question)],
                    temperature=0.0, seed=42, maxTokens=900, searchLimit=5, showSources=True,
                    abTest=False, stream=False, tools={"notion": True, "mantis": False, "vectorDb": False},
                ), user={"uid": user_uid or "notion-body-accuracy-" + uuid.uuid4().hex})
                retrieval = response.get("retrieval") or {}
            else:
                retrieval = run_retrieval_pipeline(case.question, 5,
                    {"notion": True, "mantis": False, "vectorDb": False},
                    intent_plan=planned_control(case) if mode == "planned_control" else None)
            checkpoint()
        latency = round((time.perf_counter() - started) * 1000, 1)
        known = {normalize_id(page["page_id"]): page for page in evidence.get("pages") or []}
        for page in retrieved_pages(retrieval):
            identifier = normalize_id(page["id"])
            source = known.get(identifier)
            if source is None or (source.get("complete") is not True and not any(
                    contains(segment["text"], case.terms) for segment in verified_segments(source))):
                fresh = fetch_oracle_page(page["id"])
                sources = evidence.setdefault("pages", [])
                sources[:] = [item for item in sources if normalize_id(item["page_id"]) != identifier]
                sources.append(fresh)
                known[identifier] = fresh
        graded = grade_retrieval(case, retrieval, evidence)
        graded.update(model_evidence_present=model_evidence_check(case, retrieval))
        if mode == "natural_chat":
            graded = grade_chat_response(case, response, graded)
        return {**asdict(case), **graded, "mode": mode, "latency_ms": latency,
                "request_budget_seconds": request_budget_seconds,
                "oracle_latency_ms": round((time.perf_counter() - started) * 1000 - latency, 1),
                "raw_retrieval": retrieval, "raw_chat_response": response or None}
    except Exception as exc:
        return {**asdict(case), "mode": mode, "passed": False, "outcome": "incomplete" if type(exc).__name__ == "RequestStopped" else "execution_error",
                "error_type": type(exc).__name__, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {mode: {"total": len(rows), "passed": sum(row.get("passed") is True for row in rows),
                   "outcomes": dict(Counter(row.get("outcome", "unknown") for row in rows)),
                   **({"measurement": "source_wiring_and_limited_answer_checks",
                       "retrieval_passed": sum(row.get("retrieval_passed") is True for row in rows),
                       "source_wiring_passed": sum(row.get("chat_source_wiring_passed") is True for row in rows),
                       "answer_outcomes": dict(Counter(row.get("chat_answer_outcome", "not_evaluated") for row in rows)),
                       "answer_accuracy_verified": False} if mode == "natural_chat" else {})}
            for mode in ("natural_retrieval", "planned_control", "natural_chat")
            if (rows := [row for row in results if row.get("mode") == mode])}


def write_report(report: dict[str, Any], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = ["# Notion BODY / TABLE / CODE accuracy", "",
             "Known-target retrieval and independent body precision only; sampled evidence cannot establish global recall.",
             "Natural retrieval, injected planned controls, and natural chat are reported separately.",
             "Natural chat measures source attachment/wiring and limited answer checks, not answer accuracy.",
             "answer_accuracy_verified=false: generated answers require separate review even when these checks pass.",
             "Negative cases exclude one independently read page; duplicate titles may validly return other pages.", "",
             "| Mode | Measurement | Passed / total |", "|---|---|---:|"]
    for mode, summary in report["summary"].items():
        measurement = summary.get("measurement", "known-target retrieval and sampled precision")
        lines.append(f"| {mode} | {measurement} | {summary['passed']} / {summary['total']} |")
    lines += ["", "| Case | Mode | Outcome | Answer status | Known target | Content | Model evidence | Complete | Latency ms |",
              "|---|---|---|---|---|---|---|---|---:|"]
    for row in report["results"]:
        values = [row.get(key) for key in ("id", "mode", "outcome", "chat_answer_outcome", "known_target_recalled", "returned_matching_content",
                                          "model_evidence_present", "search_complete", "latency_ms")]
        lines.append("| " + " | ".join(str(value) for value in values) + " |")
    lines += ["", "Full questions, raw source block provenance, matching/false-positive IDs, budgets, failure checks,",
              "and unmodified retrieval metadata are stored in results.json; oracle reads in oracle.json.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case-spec", type=Path, help="Independent selected_cases.json, reverified against evidence")
    parser.add_argument("--case-ids", default="", help="Comma-separated case IDs")
    parser.add_argument("--chat-model-id", default="")
    parser.add_argument("--include-planned-controls", action="store_true")
    parser.add_argument("--list-cases", action="store_true", help="Save/print cases without network calls")
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    if evidence.get("schema_version") != 1:
        parser.error("Expected evidence schema_version=1")
    try:
        cases = build_cases_from_spec(evidence, json.loads(args.case_spec.read_text(encoding="utf-8"))) if args.case_spec else build_cases(evidence)
    except ValueError as exc:
        parser.error(str(exc))
    if args.case_ids:
        selected = set(args.case_ids.split(","))
        unknown = selected - {case.id for case in cases}
        if unknown:
            parser.error("Unknown case IDs: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case.id in selected]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "cases.json").write_text(json.dumps([asdict(case) for case in cases], ensure_ascii=False, indent=2),
                                               encoding="utf-8")
    if args.list_cases:
        for case in cases:
            print(json.dumps({"id": case.id, "kind": case.required_kind, "question": case.question}, ensure_ascii=False))
        return 0 if cases else 1
    from backend.environment import load_dotenv
    load_dotenv(APP_DIR / ".env")
    modes = ["natural_retrieval"]
    if args.include_planned_controls:
        modes.append("planned_control")
    if args.chat_model_id:
        modes.append("natural_chat")
    report = {"schema_version": 1, "collected_at": datetime.now(timezone.utc).isoformat(),
              "evidence_path": str(args.evidence.resolve()), "oracle_scope": evidence.get("scope"),
              "evidence_errors": evidence.get("errors") or [], "global_recall_verified": False,
              "answer_accuracy_verified": False,
              "search_limit": 5, "chat_model_id": args.chat_model_id or None, "results": [], "summary": {}}
    uid = "notion-body-accuracy-" + uuid.uuid4().hex
    for case in cases:
        for mode in modes:
            print(json.dumps({"event": "start", "id": case.id, "mode": mode}), flush=True)
            row = run_case(case, evidence, mode, args.chat_model_id, uid)
            report["results"].append(row)
            report["summary"] = summarize_results(report["results"])
            write_report(report, args.output_dir)
            (args.output_dir / "oracle.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"event": "result", "id": case.id, "mode": mode,
                              "outcome": row["outcome"], "latency_ms": row["latency_ms"]}), flush=True)
    write_report(report, args.output_dir)
    return 0 if cases and all(row["passed"] for row in report["results"]) else 1


if __name__ == "__main__":
    from scripts.accuracy._runtime import reexec_in_project_venv
    reexec_in_project_venv(APP_DIR)
    raise SystemExit(main())
