"""회귀 평가 케이스의 정의, 실행, 결과 저장을 담당한다."""

import json
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, Dict, List

from backend.answers import build_mantis_month_count_report
from backend.audit import init_audit_db
from backend.common import normalize_for_match, text_matches_customer_aliases
from backend.config import AUDIT_DB_PATH, CONFIG_DIR
from backend.reports.effort import build_effort_report
from backend.retrieval import build_sources, retrieval_trace, run_retrieval_pipeline
from backend.understanding import (
    extract_query_constraints,
    field_matches_screen_number,
    title_has_screen_number,
)


DEFAULT_EVALUATION_CASES = [
    {
        "id": "mantis-kb-hts",
        "category": "고객사·플랫폼",
        "question": "KB증권 HTS 최근 이슈 5개 보여줘",
        "expected": {"customer": "KB증권", "platform": "HTS", "minResults": 1},
    },
    {
        "id": "mantis-eugene-exclude-mts",
        "category": "플랫폼 제외",
        "question": "유진증권 이슈에서 MTS는 빼고 최근 10개 보여줘",
        "expected": {"customer": "유진증권", "platformExclude": "MTS", "minResults": 1},
    },
    {
        "id": "mantis-eugene-screen",
        "category": "화면번호",
        "question": "유진증권 화면번호 5031 관련 이슈 찾아줘",
        "expected": {"customer": "유진증권", "screenNumber": "5031", "minResults": 1},
    },
    {
        "id": "mantis-db-alias",
        "category": "고객사 별칭",
        "question": "DB증권 최근 이슈 보여줘",
        "expected": {"customer": "동부증권", "minResults": 1},
    },
    {
        "id": "mantis-exact-id",
        "category": "정확 ID",
        "question": "0028508 이슈 내용 알려줘",
        "expected": {"issueId": 28508, "minResults": 1},
    },
    {
        "id": "notion-incident",
        "category": "Notion 키워드",
        "question": "노션에서 장애보고 관련 이슈 찾아줘",
        "expected": {"source": "notion", "keyword": "장애보고", "minResults": 1},
    },
    {
        "id": "notion-major-hanwha-order",
        "category": "Notion 복합 키워드",
        "question": "노션에서 한화 서버 자동주문 장애에 관한 건 알려줘",
        "expected": {
            "source": "notion",
            "titleContains": "한화 서버 자동주문 장애",
            "databaseLabel": "주요이슈현황",
            "minResults": 1,
        },
    },
    {
        "id": "mantis-effort-person-month",
        "category": "Mantis 공수 집계",
        "question": "2026년 2월 석지원의 공수를 알고 싶어",
        "expected": {"effortTotal": 5.25, "issueCount": 18, "minResults": 1},
    },
]


def load_evaluation_cases() -> List[Dict[str, Any]]:
    cases_path = CONFIG_DIR / "evaluation_cases.json"
    if not cases_path.exists():
        return DEFAULT_EVALUATION_CASES
    try:
        data = json.loads(cases_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[warn] evaluation_cases.json 로딩 실패, 내장 기본값 사용: {exc}", file=sys.stderr)
        return DEFAULT_EVALUATION_CASES
    if not isinstance(data, list) or not all(
        isinstance(case, dict)
        and isinstance(case.get("id"), str)
        and isinstance(case.get("question"), str)
        and isinstance(case.get("expected"), dict)
        for case in data
    ):
        print(
            "[warn] evaluation_cases.json 형식 오류(id/question 문자열, expected 객체 필수), 내장 기본값 사용",
            file=sys.stderr,
        )
        return DEFAULT_EVALUATION_CASES
    return data


EVALUATION_CASES = load_evaluation_cases()


def run_evaluation_case(case: Dict[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    question = case["question"]
    expected = case["expected"]
    constraints = extract_query_constraints(question)
    effort_report = build_effort_report(question) if expected.get("effortTotal") is not None else None
    count_report = build_mantis_month_count_report(question) if expected.get("totalCount") is not None else None
    retrieval_result = (
        effort_report.get("retrieval")
        if effort_report
        else count_report.get("retrieval")
        if count_report
        else run_retrieval_pipeline(question, 5, {"mantis": True, "vectorDb": True, "notion": True})
    )
    sources = (
        effort_report.get("sources", [])
        if effort_report
        else count_report.get("sources", [])
        if count_report
        else build_sources(retrieval_result, max_sources=20)
    )
    checks: List[Dict[str, Any]] = []

    def add_check(label: str, passed: bool, actual: str) -> None:
        checks.append({"label": label, "passed": passed, "actual": actual})

    if expected.get("customer"):
        aliases = constraints.get("customer_aliases", [])
        passed = any(normalize_for_match(expected["customer"]) in normalize_for_match(alias) or normalize_for_match(alias) in normalize_for_match(expected["customer"]) for alias in aliases)
        add_check("고객사 인식", passed, constraints.get("customer_key") or "미인식")
        mantis_sources = [source for source in sources if source.get("type") in {"mantis", "chroma"}]
        if mantis_sources:
            result_aliases = constraints.get("customer_aliases", []) or [expected["customer"]]
            mismatches = [
                source.get("project", "미상")
                for source in mantis_sources
                if not text_matches_customer_aliases(str(source.get("project", "")), result_aliases)
            ]
            add_check(
                "고객사 결과 일치",
                not mismatches,
                "모두 일치" if not mismatches else ", ".join(dict.fromkeys(mismatches)),
            )
    if expected.get("platform"):
        actual_platform = constraints.get("platform_key") or ""
        add_check(
            "플랫폼 인식",
            normalize_for_match(actual_platform) == normalize_for_match(expected["platform"]),
            actual_platform or "미인식",
        )
    if expected.get("platformExclude"):
        excluded = constraints.get("excluded_platform_keys", [])
        expected_platform = normalize_for_match(expected["platformExclude"])
        add_check(
            "플랫폼 제외 인식",
            expected_platform in {normalize_for_match(value) for value in excluded},
            ", ".join(excluded) or "미인식",
        )
        mismatched_ids = []
        for source in sources:
            if source.get("type") not in {"mantis", "chroma"}:
                continue
            detail = source.get("detail") or {}
            media_type = str(detail.get("media_type") or "")
            if media_type and normalize_for_match(media_type) == expected_platform:
                mismatched_ids.append(str(source.get("issue_id") or "?"))
        add_check(
            "제외 플랫폼 결과 일치",
            not mismatched_ids,
            "모두 제외됨" if not mismatched_ids else f"포함된 ID: {', '.join(mismatched_ids)}",
        )
    if expected.get("screenNumber"):
        actual = constraints.get("screen_numbers", [])
        add_check("화면번호 인식", expected["screenNumber"] in actual, ", ".join(actual) or "미인식")
        mantis_sources = [source for source in sources if source.get("type") in {"mantis", "chroma"}]
        if mantis_sources:
            mismatched_ids = []
            for source in mantis_sources:
                detail = source.get("detail") or {}
                screen_field = str(detail.get("screen_number") or "")
                title = str(source.get("title") or "")
                if not (
                    field_matches_screen_number(screen_field, expected["screenNumber"])
                    or title_has_screen_number(title, expected["screenNumber"])
                ):
                    mismatched_ids.append(str(source.get("issue_id") or "?"))
            add_check(
                "화면번호 결과 정확 일치",
                not mismatched_ids,
                "모두 일치" if not mismatched_ids else f"불일치 ID: {', '.join(mismatched_ids)}",
            )
    if expected.get("person"):
        actual_people = constraints.get("people", {}).get("names", [])
        add_check(
            "담당자 인식",
            expected["person"] in actual_people,
            ", ".join(actual_people) or "미인식",
        )
        mantis_sources = [source for source in sources if source.get("type") in {"mantis", "chroma"}]
        mismatched_ids = [
            str(source.get("issue_id") or "?")
            for source in mantis_sources
            if normalize_for_match(source.get("handler", "")) != normalize_for_match(expected["person"])
        ]
        if mantis_sources:
            add_check(
                "담당자 결과 일치",
                not mismatched_ids,
                "모두 일치" if not mismatched_ids else f"불일치 ID: {', '.join(mismatched_ids)}",
            )
    if expected.get("issueId"):
        actual_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
        add_check("정확 ID 조회", expected["issueId"] in actual_ids, ", ".join(map(str, sorted(actual_ids))) or "조회 없음")
    if expected.get("sourceIds"):
        actual_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
        required_ids = {int(issue_id) for issue_id in expected["sourceIds"]}
        missing_ids = sorted(required_ids - actual_ids)
        add_check(
            "기대 이슈 포함",
            not missing_ids,
            "모두 포함" if not missing_ids else f"누락: {', '.join(map(str, missing_ids))}",
        )
    if expected.get("source"):
        actual_types = {str(source.get("type")) for source in sources if source.get("type")}
        add_check("데이터 소스", expected["source"] in actual_types, ", ".join(sorted(actual_types)) or "조회 없음")
    if expected.get("titleContains"):
        titles = [source.get("title", "") for source in sources]
        matched_title = next(
            (title for title in titles if normalize_for_match(expected["titleContains"]) in normalize_for_match(title)),
            "",
        )
        add_check("제목 일치", bool(matched_title), matched_title or "일치 제목 없음")
    if expected.get("databaseLabel"):
        database_labels = [source.get("database_label", "") for source in sources]
        matched_database = next(
            (
                label
                for label in database_labels
                if normalize_for_match(expected["databaseLabel"]) in normalize_for_match(label)
            ),
            "",
        )
        add_check("Notion DB", bool(matched_database), matched_database or "일치 DB 없음")
    if expected.get("effortTotal") is not None:
        effort_stages = (retrieval_result or {}).get("stages") or []
        effort_stage = effort_stages[0] if effort_stages and isinstance(effort_stages[0], dict) else {}
        effort_meta = effort_stage.get("meta", {})
        actual_total = float(effort_meta.get("total_effort", 0))
        add_check(
            "공수 합계",
            abs(actual_total - float(expected["effortTotal"])) < 0.0001,
            f"{actual_total:.2f}",
        )
    if expected.get("issueCount") is not None:
        add_check(
            "반영 이슈 수",
            len(sources) == int(expected["issueCount"]),
            f"{len(sources)}건",
        )
    if expected.get("totalCount") is not None:
        count_stages = (retrieval_result or {}).get("stages") or []
        count_stage = count_stages[0] if count_stages and isinstance(count_stages[0], dict) else {}
        total_count = int(count_stage.get("meta", {}).get("total_count", -1))
        add_check(
            "전체 건수",
            total_count == int(expected["totalCount"]),
            f"{total_count}건" if total_count >= 0 else "집계 없음",
        )
    add_check("최소 결과 수", len(sources) >= expected.get("minResults", 1), f"{len(sources)}건")
    passed = all(check["passed"] for check in checks)
    return {
        **case,
        "passed": passed,
        "checks": checks,
        "sourceCount": len(sources),
        "trace": retrieval_trace(retrieval_result),
        "durationMs": round((time.perf_counter() - started) * 1000),
    }


def run_evaluation_case_safely(case: Dict[str, Any]) -> Dict[str, Any]:
    # 케이스 하나의 예외가 전체 실행을 500으로 죽이지 않도록 실패 결과로 감싼다.
    started = time.perf_counter()
    try:
        return run_evaluation_case(case)
    except Exception as exc:
        return {
            **case,
            "passed": False,
            "checks": [{"label": "실행 오류", "passed": False, "actual": f"{type(exc).__name__}: {exc}"}],
            "sourceCount": 0,
            "trace": [],
            "durationMs": round((time.perf_counter() - started) * 1000),
        }


def build_evaluation_summary(results: List[Dict[str, Any]], duration_ms: int) -> Dict[str, Any]:
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "total": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "score": round(passed_count / len(results) * 100) if results else 0,
        "runAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "durationMs": duration_ms,
    }


def save_evaluation_run(summary: Dict[str, Any], results: List[Dict[str, Any]]) -> str:
    run_id = uuid.uuid4().hex[:12]
    init_audit_db()
    with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection, connection:
        connection.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                summary["runAt"],
                summary["total"],
                summary["passed"],
                summary["failed"],
                summary["score"],
                summary["durationMs"],
                json.dumps(results, ensure_ascii=False),
            ),
        )
    return run_id


__all__ = [
    "DEFAULT_EVALUATION_CASES",
    "EVALUATION_CASES",
    "build_evaluation_summary",
    "load_evaluation_cases",
    "run_evaluation_case",
    "run_evaluation_case_safely",
    "save_evaluation_run",
]
