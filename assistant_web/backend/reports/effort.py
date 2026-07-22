"""공수(effort) 집계 리포트 — 조건별/인물별/랭킹 공수 산출(상위 계층).

Mantis 이슈의 공수 커스텀필드를 파싱해 기간·고객사·매체·인물 조건으로 집계하고,
조건별 합계, 개인별 리포트와 전체 랭킹 리포트를 생성한다.

의존: common/query_planning + understanding(제약추출) + mantis(이슈 조회/정규화/소스).
"""

import calendar
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from backend.common import (
    is_valid_person_name,
    normalize_for_match,
    text_contains_person_name,
)
from backend.query_planning import build_query_plan
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_issues,
    issue_matches_constraints,
    mantis_issue_effort_date,
    normalize_issue,
)
from backend.understanding import (
    extract_query_constraints,
    load_known_people,
    wants_all_results,
)


EFFORT_FIELD_LABELS = {
    "1문의공수": "문의",
    "2서버공수": "서버",
    "3PC공수": "PC",
    "4QC공수": "QC",
}


def parse_effort_period(text: str) -> tuple[int, Optional[int]]:
    """공수 질문에서 연도/월을 뽑는다. 연도 언급이 없으면 올해로 간주한다."""
    year_match = re.search(r"(20\d{2})\s*년", text)
    month_match = re.search(r"(1[0-2]|0?[1-9])\s*월", text)
    today = date.today()
    if year_match:
        year = int(year_match.group(1))
    elif re.search(r"올해|금년|이번\s*해|이번\s*년도", text):
        year = today.year
    elif re.search(r"작년|전년|지난\s*해|지난\s*년도", text):
        year = today.year - 1
    else:
        year = today.year
    return year, int(month_match.group(1)) if month_match else None


def extract_effort_query(text: str) -> Optional[Dict[str, Any]]:
    if "공수" not in normalize_for_match(text):
        return None
    year, month = parse_effort_period(text)
    person = ""
    person_match = re.search(
        r"([가-힣]{2,4}?)(?:님)?(?:의)?\s*(?:\d{4}\s*년|올해|금년|이번\s*해|이번\s*년도|작년|전년|지난\s*해|지난\s*년도)?\s*(?:\d{1,2}\s*월)?\s*공수",
        text,
    )
    if person_match:
        candidate = person_match.group(1)
        # "작년 기준으로 공수가..." 처럼 "공수" 바로 앞 단어를 무조건 사람 이름으로
        # 오인하지 않도록, 실제 known_people(과거 이슈의 담당자/보고자)에 있는
        # 이름일 때만 인정한다. 없으면 다른 사람 이름이 없다고 보고 아래로 폴백한다.
        handlers, reporters = load_known_people()
        known_people = set(handlers) | set(reporters)
        if candidate in known_people and is_valid_person_name(candidate):
            person = candidate
    constraints = extract_query_constraints(text)
    if not person:
        people = constraints.get("people", {}).get("names") or []
        if people:
            person = str(people[0]).strip()
    if not person:
        # 고객사나 매체가 명시된 공수 질문은 인물별 조회가 아니라 해당 조건에
        # 속한 모든 이슈의 공수 합계를 요청한 것으로 본다.
        if constraints.get("customer_key") or constraints.get("platform_key"):
            query = {
                "scope": "filters",
                "year": year,
                "constraints": constraints,
            }
            if month:
                query["month"] = month
            else:
                query["period"] = "year"
            return query
        return {"error": "공수를 조회할 인물 이름을 함께 입력해 주세요."}
    query = {"person": person, "year": year}
    if month:
        query["month"] = month
    else:
        query["period"] = "year"
    return query


def parse_effort_field_value(value: Any) -> List[tuple[str, float]]:
    results = []
    for name, amount in re.findall(
        r"([가-힣A-Za-z]{2,20})\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)",
        str(value or ""),
    ):
        results.append((name.strip(), float(amount)))
    return results


def build_effort_report(text: str) -> Optional[Dict[str, Any]]:
    query = extract_effort_query(text)
    if not query:
        return None
    if query.get("error"):
        # 인물을 규칙으로 못 찾은 경우다. 파이프라인이 이 응답을 바로 반환하지 않고
        # LLM 플래너에게 한 번 더 분류를 맡길 수 있도록 표식을 남긴다.
        return {
            "content": query["error"],
            "sources": [],
            "needsPerson": True,
            "raw": {"mode": "deterministic_effort_report"},
        }
    if query.get("scope") == "filters":
        return run_filtered_effort_report(
            text,
            query["year"],
            query.get("month"),
            query["constraints"],
        )
    return run_person_effort_report(text, query["person"], query["year"], query.get("month"))


def run_filtered_effort_report(
    text: str,
    year: int,
    month: Optional[int],
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """고객사·매체 조건에 맞는 이슈의 모든 인물 공수를 합산한다."""
    is_year_period = not month
    if is_year_period:
        start_date = f"{year:04d}-01-01"
        end_date = f"{year:04d}-12-31"
        period_label = f"{year}년"
    else:
        last_day = calendar.monthrange(year, month)[1]
        start_date = f"{year:04d}-{month:02d}-01"
        end_date = f"{year:04d}-{month:02d}-{last_day:02d}"
        period_label = f"{year}년 {month}월"

    effort_constraints = {
        **constraints,
        "created_at": {"from": start_date, "to": end_date, "precision": "range"},
    }
    effort_plan = build_query_plan(
        text,
        {"source": "mantis", "planner": "rule", "confidence": 1.0},
        effort_constraints,
        {
            "planned": True,
            "source": "mantis",
            "operation": "aggregate",
            "reason": "고객사·매체 조건별 공수 합계",
        },
    )
    totals: Dict[str, float] = {field: 0.0 for field in EFFORT_FIELD_LABELS}
    matched_rows: List[Dict[str, Any]] = []
    consecutive_old_pages = 0

    for page_number in range(1, 51):
        issues = fetch_mantis_issues({"page_size": 100, "page": page_number})
        if not issues:
            break
        page_dates = [mantis_issue_effort_date(issue) for issue in issues]
        page_has_target_or_newer = any(date_value >= start_date for date_value in page_dates if date_value)
        consecutive_old_pages = 0 if page_has_target_or_newer else consecutive_old_pages + 1

        for issue in issues:
            issue_date = mantis_issue_effort_date(issue)
            if not issue_date or issue_date < start_date or issue_date > end_date:
                continue
            if not issue_matches_constraints(issue, effort_constraints):
                continue
            effort_items: List[Dict[str, Any]] = []
            for custom_field in issue.get("custom_fields", []):
                field_name = str(custom_field.get("field", {}).get("name", ""))
                if field_name not in EFFORT_FIELD_LABELS:
                    continue
                for effort_person, amount in parse_effort_field_value(custom_field.get("value", "")):
                    totals[field_name] += amount
                    effort_items.append(
                        {
                            "field": field_name,
                            "label": EFFORT_FIELD_LABELS[field_name],
                            "person": effort_person,
                            "amount": amount,
                        }
                    )
            if effort_items:
                normalized_issue = normalize_issue(issue)
                normalized_issue["effort_items"] = effort_items
                normalized_issue["effort_total"] = round(
                    sum(item["amount"] for item in effort_items), 4
                )
                matched_rows.append(normalized_issue)
        if consecutive_old_pages >= 2:
            break

    matched_rows.sort(
        key=lambda issue: (issue.get("effort_total", 0), issue.get("id", 0)),
        reverse=True,
    )
    total_effort = round(sum(totals.values()), 4)
    customer_label = (
        matched_rows[0].get("project")
        if matched_rows
        else constraints.get("customer_key")
    )
    platform_label = str(constraints.get("platform_key") or "").upper()
    scope_parts = [str(value) for value in (customer_label, platform_label) if value]
    scope_label = " · ".join(scope_parts) or "요청 조건"

    lines = [
        f"{period_label} {scope_label} 공수 집계입니다.",
        "",
        f"- 총 공수: **{total_effort:.2f}**",
        f"- 반영 이슈: **{len(matched_rows)}건**",
        f"- 산정 기간: {start_date} ~ {end_date}",
        "- 기준: Mantis 이슈 등록일 · 고객사/매체 조건 · 전체 공수 커스텀 필드",
        "",
        "유형별 공수",
    ]
    for field_name, label in EFFORT_FIELD_LABELS.items():
        lines.append(f"- {label}: **{totals[field_name]:.2f}**")
    if matched_rows:
        lines.extend(["", "공수가 큰 이슈"])
        for issue in matched_rows[:5]:
            lines.append(
                f"- #{issue.get('id')} {issue.get('summary', '제목 없음')} · "
                f"{issue.get('effort_total', 0):.2f}"
            )
    else:
        lines.extend(["", "해당 조건과 기간에 기록된 공수를 찾지 못했습니다."])

    effort_chart = {
        "person": f"{scope_label} 전체",
        "year": year,
        "periodLabel": period_label,
        "month": month,
        "totalEffort": total_effort,
        "issueCount": len(matched_rows),
        "byType": [
            {"label": label, "amount": round(totals[field_name], 2)}
            for field_name, label in EFFORT_FIELD_LABELS.items()
        ],
        "topIssues": [
            {
                "id": issue.get("id"),
                "summary": issue.get("summary", "제목 없음"),
                "amount": round(issue.get("effort_total", 0), 2),
            }
            for issue in matched_rows[:5]
        ],
    }
    retrieval_result = {
        "query": text,
        "keyword": " ".join(scope_parts),
        "constraints": effort_constraints,
        "intent_plan": effort_plan,
        "query_plan": effort_plan,
        "limit": len(matched_rows),
        "stages": [
            {
                "source": "mantis_effort_filtered",
                "status": "ok",
                "issues_list": matched_rows,
                "meta": {
                    "displayed_count": len(matched_rows),
                    "year": year,
                    "month": month,
                    "period": "year" if is_year_period else "month",
                    "totals": {key: round(value, 4) for key, value in totals.items()},
                    "total_effort": total_effort,
                    "date_basis": "created_at",
                },
            }
        ],
    }
    return {
        "content": "\n".join(lines),
        "sources": build_sources_from_issue_rows(matched_rows),
        "retrieval": retrieval_result,
        "effortChart": effort_chart if matched_rows else None,
        "raw": {"mode": "deterministic_filtered_effort_report"},
    }


def run_person_effort_report(
    text: str,
    person: str,
    year: int,
    month: Optional[int] = None,
) -> Dict[str, Any]:
    """특정 인물의 기간 공수를 Mantis 라이브 API에서 합산한다(결정형 계산 코어)."""
    is_year_period = not month
    if is_year_period:
        start_date = f"{year:04d}-01-01"
        end_date = f"{year:04d}-12-31"
        period_label = f"{year}년"
        period_reason = "연간 개인 공수 집계"
    else:
        last_day = calendar.monthrange(year, month)[1]
        start_date = f"{year:04d}-{month:02d}-01"
        end_date = f"{year:04d}-{month:02d}-{last_day:02d}"
        period_label = f"{year}년 {month}월"
        period_reason = "월별 개인 공수 집계"
    effort_constraints = extract_query_constraints(text)
    effort_constraints["people"] = {"names": [person], "role": "any"}
    effort_plan = build_query_plan(
        text,
        {"source": "mantis", "planner": "rule", "confidence": 1.0},
        effort_constraints,
        {"planned": True, "source": "mantis", "operation": "aggregate", "reason": period_reason},
    )
    totals: Dict[str, float] = {field: 0.0 for field in EFFORT_FIELD_LABELS}
    matched_rows: List[Dict[str, Any]] = []
    consecutive_old_pages = 0

    for page_number in range(1, 51):
        issues = fetch_mantis_issues({"page_size": 100, "page": page_number})
        if not issues:
            break
        page_dates = [mantis_issue_effort_date(issue) for issue in issues]
        page_has_target_or_newer = any(date_value >= start_date for date_value in page_dates if date_value)
        consecutive_old_pages = 0 if page_has_target_or_newer else consecutive_old_pages + 1

        for issue in issues:
            issue_date = mantis_issue_effort_date(issue)
            if not issue_date or issue_date < start_date or issue_date > end_date:
                continue
            effort_items = []
            for custom_field in issue.get("custom_fields", []):
                field_name = str(custom_field.get("field", {}).get("name", ""))
                if field_name not in EFFORT_FIELD_LABELS:
                    continue
                for effort_person, amount in parse_effort_field_value(custom_field.get("value", "")):
                    if normalize_for_match(effort_person) != normalize_for_match(person):
                        continue
                    totals[field_name] += amount
                    effort_items.append(
                        {
                            "field": field_name,
                            "label": EFFORT_FIELD_LABELS[field_name],
                            "amount": amount,
                        }
                    )
            if effort_items:
                normalized_issue = normalize_issue(issue)
                normalized_issue["effort_items"] = effort_items
                normalized_issue["effort_total"] = round(
                    sum(item["amount"] for item in effort_items), 4
                )
                matched_rows.append(normalized_issue)
        if consecutive_old_pages >= 2:
            break

    matched_rows.sort(
        key=lambda issue: (issue.get("effort_total", 0), issue.get("id", 0)),
        reverse=True,
    )
    total_effort = round(sum(totals.values()), 4)
    lines = [
        f"{period_label} {person} 공수 집계입니다.",
        "",
        f"- 총 공수: **{total_effort:.2f}**",
        f"- 반영 이슈: **{len(matched_rows)}건**",
        f"- 산정 기간: {start_date} ~ {end_date}",
        "- 기준: Mantis 이슈 등록일 · 공수 커스텀 필드",
        "",
        "유형별 공수",
    ]
    for field_name, label in EFFORT_FIELD_LABELS.items():
        lines.append(f"- {label}: **{totals[field_name]:.2f}**")
    if matched_rows:
        lines.extend(["", "공수가 큰 이슈"])
        for issue in matched_rows[:5]:
            lines.append(
                f"- {issue.get('summary', '제목 없음')} · {issue.get('effort_total', 0):.2f}"
            )
    else:
        lines.extend(["", "해당 기간에 기록된 공수를 찾지 못했습니다."])

    effort_chart = {
        "person": person,
        "year": year,
        "periodLabel": period_label,
        "month": month,
        "totalEffort": total_effort,
        "issueCount": len(matched_rows),
        "byType": [
            {"label": label, "amount": round(totals[field_name], 2)}
            for field_name, label in EFFORT_FIELD_LABELS.items()
        ],
        "topIssues": [
            {
                "id": issue.get("id"),
                "summary": issue.get("summary", "제목 없음"),
                "amount": round(issue.get("effort_total", 0), 2),
            }
            for issue in matched_rows[:5]
        ],
    }

    retrieval_result = {
        "query": text,
        "keyword": person,
        "constraints": effort_constraints,
        "intent_plan": effort_plan,
        "query_plan": effort_plan,
        "limit": len(matched_rows),
        "stages": [
            {
                "source": "mantis_effort",
                "status": "ok",
                "issues_list": matched_rows,
                "meta": {
                    "displayed_count": len(matched_rows),
                    "person": person,
                    "year": year,
                    "month": month,
                    "period": "year" if is_year_period else "month",
                    "totals": {key: round(value, 4) for key, value in totals.items()},
                    "total_effort": total_effort,
                    "date_basis": "created_at",
                },
            }
        ],
    }
    return {
        "content": "\n".join(lines),
        "sources": build_sources_from_issue_rows(matched_rows),
        "retrieval": retrieval_result,
        "effortChart": effort_chart if matched_rows else None,
        "raw": {"mode": "deterministic_effort_report"},
    }


def wants_effort_ranking(text: str) -> bool:
    """특정 인물을 지정하지 않고 공수 순위/명단을 묻는 질문인지 판별한다.

    두 갈래를 잡는다.
    1) 순위형: "제일 많은 사람", "공수 순위" 등 명시적 랭킹 표현.
    2) 명단형: "공수 기입한 사람들 다 보여줘"처럼 공수를 낸 인원 전체를 묻는 표현.
       (인원 집계 결과는 순위 리포트와 동일하므로 같은 경로로 처리한다.)"""
    normalized = normalize_for_match(text)
    if "공수" not in normalized:
        return False
    ranking_markers = (
        "제일많은사람",
        "가장많은사람",
        "제일많이한사람",
        "가장많이한사람",
        "제일많이쓴사람",
        "가장많이쓴사람",
        "최다공수",
        "공수1위",
        "공수순위",
        "공수랭킹",
        "누가제일",
        "누가가장",
        "누구가제일",
        "누구가가장",
    )
    if any(marker in normalized for marker in ranking_markers):
        return True
    # 명단형: "사람/인원/담당자" + 목록 요청 동사가 함께 오고, 특정 인물 이름이
    # 없을 때 전원 집계로 본다. "석지원 공수 알려줘"(특정 인물)는 아래에서 걸러진다.
    roster_subject = any(marker in normalized for marker in ("사람", "인원", "담당자", "명단", "누구"))
    roster_verb = any(marker in normalized for marker in ("보여", "알려", "정리", "누구", "리스트", "목록"))
    if roster_subject and roster_verb:
        handlers, reporters = load_known_people()
        known = set(handlers) | set(reporters)
        # 질문에 known_people 이름이 직접 등장하면 개인 조회이므로 랭킹이 아니다.
        if not any(text_contains_person_name(text, name) for name in known):
            return True
    return False


def wants_full_effort_roster(text: str) -> bool:
    """'다/전부/전체/모두 보여줘'처럼 상위 N명이 아니라 전원을 원하는지 판별한다."""
    return wants_all_results(text) or bool(
        re.search(r"(다|전부|전체|모두)\s*(보여|알려|정리|나열)", text)
    )


def build_effort_ranking_report(text: str) -> Optional[Dict[str, Any]]:
    """특정 인물을 지정하지 않고 '공수가 제일 많은 사람'을 묻는 질문에 답한다.

    build_effort_report는 이미 알고 있는 한 사람의 공수를 합산하는 반면, 이 함수는
    기간 내 전체 이슈를 스캔해 인물별로 공수를 집계·정렬해 순위를 낸다."""
    if not wants_effort_ranking(text):
        return None
    year, month = parse_effort_period(text)
    return run_effort_ranking_report(text, year, month)


def run_effort_ranking_report(
    text: str,
    year: int,
    month: Optional[int] = None,
) -> Dict[str, Any]:
    """기간 내 전체 이슈를 스캔해 인물별 공수를 집계·정렬한다(결정형 계산 코어)."""
    is_year_period = not month
    if is_year_period:
        start_date = f"{year:04d}-01-01"
        end_date = f"{year:04d}-12-31"
        period_label = f"{year}년"
    else:
        last_day = calendar.monthrange(year, month)[1]
        start_date = f"{year:04d}-{month:02d}-01"
        end_date = f"{year:04d}-{month:02d}-{last_day:02d}"
        period_label = f"{year}년 {month}월"

    per_person: Dict[str, Dict[str, float]] = {}
    consecutive_old_pages = 0
    scanned_issue_count = 0

    for page_number in range(1, 51):
        issues = fetch_mantis_issues({"page_size": 100, "page": page_number})
        if not issues:
            break
        page_dates = [mantis_issue_effort_date(issue) for issue in issues]
        page_has_target_or_newer = any(date_value >= start_date for date_value in page_dates if date_value)
        consecutive_old_pages = 0 if page_has_target_or_newer else consecutive_old_pages + 1

        for issue in issues:
            issue_date = mantis_issue_effort_date(issue)
            if not issue_date or issue_date < start_date or issue_date > end_date:
                continue
            issue_has_effort = False
            for custom_field in issue.get("custom_fields", []):
                field_name = str(custom_field.get("field", {}).get("name", ""))
                if field_name not in EFFORT_FIELD_LABELS:
                    continue
                for effort_person, amount in parse_effort_field_value(custom_field.get("value", "")):
                    normalized_name = effort_person.strip()
                    if not normalized_name:
                        continue
                    bucket = per_person.get(normalized_name)
                    if bucket is None:
                        bucket = {field: 0.0 for field in EFFORT_FIELD_LABELS}
                        bucket["_total"] = 0.0
                        per_person[normalized_name] = bucket
                    bucket[field_name] += amount
                    bucket["_total"] += amount
                    issue_has_effort = True
            if issue_has_effort:
                scanned_issue_count += 1
        if consecutive_old_pages >= 2:
            break

    if not per_person:
        return {
            "content": f"{period_label} 동안 공수가 기록된 이슈를 찾지 못했습니다.",
            "sources": [],
            "raw": {"mode": "deterministic_effort_ranking"},
        }

    ranked = sorted(per_person.items(), key=lambda item: item[1]["_total"], reverse=True)
    # "다/전원 보여줘"면 전체를, 아니면 상위 10명만 노출한다.
    show_all = wants_full_effort_roster(text)
    top_n = ranked if show_all else ranked[:10]
    top_person_name, top_person_totals = top_n[0]

    heading = f"{period_label} 공수 기입자 전체 명단입니다." if show_all else f"{period_label} 공수 순위입니다."
    list_label = "공수 기입자 (많은 순)" if show_all else "공수 순위"
    lines = [
        heading,
        "",
        f"- 산정 기간: {start_date} ~ {end_date}",
        f"- 공수가 기록된 이슈: **{scanned_issue_count}건** · 인원: **{len(per_person)}명**",
        "",
        list_label,
    ]
    for index, (name, totals) in enumerate(top_n, start=1):
        lines.append(f"{index}. {name} — **{totals['_total']:.2f}**")

    lines.extend(["", f"1위는 **{top_person_name}**이며 유형별 내역은 다음과 같습니다."])
    for field_name, label in EFFORT_FIELD_LABELS.items():
        lines.append(f"- {label}: **{top_person_totals[field_name]:.2f}**")

    effort_ranking_chart = {
        "periodLabel": period_label,
        "issueCount": scanned_issue_count,
        "peopleCount": len(per_person),
        "topPeople": [
            {"label": name, "amount": round(totals["_total"], 2)}
            for name, totals in top_n
        ],
    }

    retrieval_result = {
        "query": text,
        "keyword": "공수 랭킹",
        "constraints": {},
        "limit": len(top_n),
        "stages": [
            {
                "source": "mantis_effort_ranking",
                "status": "ok",
                "issues_list": [],
                "meta": {
                    "displayed_count": len(top_n),
                    "found_count": len(per_person),
                    "period": "year" if is_year_period else "month",
                    "year": year,
                    "month": month,
                    "date_basis": "created_at",
                },
            }
        ],
    }

    return {
        "content": "\n".join(lines),
        "sources": [],
        "retrieval": retrieval_result,
        "effortRanking": effort_ranking_chart,
        "raw": {"mode": "deterministic_effort_ranking"},
    }


def build_effort_report_from_plan(
    text: str,
    model_plan: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """규칙 경로가 못 잡은 공수 질문을 LLM 플래너의 effort 분류로 실행한다.

    플래너는 의도(person/ranking)와 인물 이름만 제공하고, 기간 해석과 실제 합산은
    전부 기존 결정형 코어(run_*)가 수행한다. 플래너가 지어낸 이름이 실제 담당자
    명단에 없으면 실행하지 않는다(환각 차단)."""
    effort = model_plan.get("effort") if isinstance(model_plan.get("effort"), dict) else None
    if not effort:
        return None
    year, month = parse_effort_period(text)
    effort_type = str(effort.get("type", "")).strip().lower()
    if effort_type == "ranking":
        return run_effort_ranking_report(text, year, month)
    if effort_type == "person":
        person = str(effort.get("person") or "").strip()
        if not person or not is_valid_person_name(person):
            return None
        handlers, reporters = load_known_people()
        if person not in set(handlers) | set(reporters):
            return None
        return run_person_effort_report(text, person, year, month)
    return None
