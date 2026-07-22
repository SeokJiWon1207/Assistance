"""결정론적 답변 생성 — 이슈/Notion 목록 포맷, 집계 리포트, 후속 질문(Layer 5).

검색 결과를 모델 없이 곧바로 사용자 답변 텍스트로 조립하는 계층.
Mantis 건수 집계 리포트(build_mantis_count_report 등)와 답변 스타일 지침,
검증 화면용 제약조건 표시(display_constraints)도 여기에 둔다.

의존: retrieval + sources + understanding. app.py는 `from backend.answers import *`로 재노출한다.
"""

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.common import compact_text, normalize_chroma_issue_document, normalize_for_match
from backend.config import CHROMA_DB_PATH, STATUS_ALIASES, STATUS_MATCH_ALIASES
from backend.query_planning import build_query_plan, extract_group_by
from backend.query_filters import filter_clause_label, normalize_filter_clauses
from backend.retrieval import (
    build_snapshot_note,
    collect_retrieved_issues,
    merge_combined_sources,
    similar_base_issue_ids_from_retrieval,
)
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    get_mantis_snapshot_freshness,
    collect_mantis_live_ids_in_range,
    extract_snapshot_limit,
    fetch_mantis_recent_issues,
    issue_created_year_month,
    issue_matches_constraints,
    normalize_issue,
)
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.understanding import (
    ISSUE_ANALYSIS_INTENT_KEYWORDS,
    ISSUE_LIST_INTENT_KEYWORDS,
    build_keyword_terms,
    chroma_text_matches_constraints,
    extract_query_constraints,
    extract_requested_limit,
    has_constraint,
    merge_query_plan_constraints,
)


CONVERSATION_TONE_NOTE = (
    "말투는 사내 동료에게 설명하듯 자연스럽고 정중한 한국어 존댓말로 답해라. "
    "이전 대화 내용을 이어서 이해하고, 사용자가 '그거', '그중 첫 번째', '담당자 누구야'처럼 "
    "앞 답변을 가리키면 직전 맥락을 참고해 답해라. "
    "요청이 모호해서 어떤 고객사/기간/조건인지 특정할 수 없으면, 목록을 길게 나열하기 전에 "
    "먼저 한 문장으로 짧게 되물어라. "
    "결과를 나열할 때도 앞뒤에 짧은 한 문장으로 요약이나 다음에 물어볼 만한 제안을 곁들여 "
    "대화가 이어지게 해라. 다만 불필요하게 장황하거나 같은 말을 반복하지는 마라. "
    "가독성을 위해 이모지를 절제해서 사용해도 좋다. 답변 도입부나 섹션 제목에 1~2개 수준이 적당하며, "
    "문장마다 붙이거나 심각한 장애/오류 내용에 장난스러운 이모지를 쓰지는 마라. "
    "강조는 **굵게**, 코드나 화면번호 같은 식별자는 `백틱`을 사용해라."
)


KOREAN_WEEKDAY_NAMES = ("월", "화", "수", "목", "금", "토", "일")


def build_current_date_note() -> str:
    """모델에게 오늘 날짜(요일 포함)를 매 요청마다 알려준다.

    시스템 프롬프트에 날짜를 넣지 않으면 모델은 '오늘', '이번 주', '최근 며칠'
    같은 표현을 실제 날짜와 연결할 방법이 없다. 검색 필터는 이미
    extract_created_at_range/extract_relative_created_at_range가 규칙 기반으로
    독립 계산하므로, 이 노트는 모델이 답변 문장에서 상대 시점을 정확히
    언급하도록 돕는 보조 역할이다."""
    now = datetime.now().astimezone()
    weekday = KOREAN_WEEKDAY_NAMES[now.weekday()]
    note = (
        f"오늘은 {now.strftime('%Y-%m-%d')}({weekday}요일)이다. "
        "사용자가 '오늘', '어제', '이번 주', '최근 며칠'처럼 상대적인 시점을 언급하면 "
        "이 날짜를 기준으로 정확히 계산해서 답해라."
    )
    # 오늘 날짜와 데이터 기준일은 다르다. 이걸 알려주지 않으면 "데이터 기준일"을
    # 물었을 때 모델이 오늘 날짜를 그대로 답한다.
    snapshot_as_of = get_mantis_snapshot_freshness()
    if snapshot_as_of:
        note += (
            f" 단, Mantis 로컬 스냅샷의 데이터 기준일은 {snapshot_as_of}로 오늘과 다르다. "
            "사용자가 데이터 기준일을 물으면 오늘 날짜가 아니라 이 기준일을 답하고, "
            "기준일 이후 등록된 이슈는 빠져 있을 수 있다는 점을 함께 알려라."
        )
    return note


def answer_style_instruction(answer_style: str) -> str:
    if answer_style == "short":
        return "답변은 핵심만 3문장 이내로 짧게 작성해라."
    if answer_style == "detailed":
        return "답변은 배경, 근거, 다음 액션까지 포함해 자세히 작성해라."
    if answer_style == "table":
        return (
            "가능하면 표 형태로 정리하고, 표가 맞지 않는 내용은 짧은 문단으로 보충해라. "
            "표를 쓸 때는 반드시 GitHub Flavored Markdown 표 문법을 지켜라: "
            "헤더 행, 그 다음 줄에 |---|---| 형태의 구분 행, 그 다음 데이터 행들을 각각 "
            "줄바꿈(개행 문자)으로 구분해서 한 줄에 한 행만 써라. "
            "여러 행을 한 줄에 이어 붙이지 마라. 예시:\n"
            "| 항목 | 설명 |\n|---|---|\n| A | 내용1 |\n| B | 내용2 |"
        )
    return "답변은 필요한 만큼만 간결하고 명확하게 작성해라."


def extract_month_count_query(text: str) -> Optional[Dict[str, int]]:
    normalized_text = normalize_for_match(text)
    if not any(keyword in normalized_text for keyword in ("몇개", "몇건", "건수", "몇개야", "몇건이야")):
        return None
    month_match = re.search(r"(1[0-2]|0?[1-9])\s*월", text)
    if not month_match:
        return None
    year_match = re.search(r"(20\d{2})\s*년", text)
    year = int(year_match.group(1)) if year_match else date.today().year
    month = int(month_match.group(1))
    return {
        "year": year,
        "month": month,
        "snapshot_limit": extract_snapshot_limit(text),
    }


def build_unknown_customer_report(text: str) -> Optional[Dict[str, Any]]:
    """질문이 가리킨 고객사가 등록돼 있지 않으면 그 사실을 그대로 알린다.

    미등록 고객사를 조용히 버리면 조건 없는 전체 검색이 되어, 전혀 다른
    고객사의 최근 이슈가 답으로 나간다. 결과가 없다는 사실 자체가 답이므로
    검색을 돌리지 않고 여기서 끝낸다."""
    constraints = extract_query_constraints(text)
    unknown = constraints.get("unknown_customers") or []
    if not unknown or constraints.get("customer_key"):
        return None
    names = ", ".join(unknown)
    lines = [
        f"🔍 '{names}'는 등록된 고객사에서 찾지 못했어요.",
        "",
        f"검색 기준: 고객사={names} (미등록)",
        "",
        "다른 고객사 결과를 대신 보여드리지 않았습니다. "
        "고객사명을 다시 확인하시거나, 고객사 조건 없이 찾으려면 이름을 빼고 물어봐 주세요.",
    ]
    return {
        "content": "\n".join(lines),
        "sources": [],
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [
                {
                    "source": "mantis",
                    "status": "skipped",
                    "reason": "미등록 고객사",
                    "meta": {"unknown_customers": unknown, "displayed_count": 0},
                }
            ],
        },
        "raw": {"mode": "deterministic_unknown_customer"},
    }


COUNT_GROUP_LABELS = {
    "customer": "고객사",
    "status": "상태",
    "owner": "담당자",
    "month": "월",
}


def count_group_key(group_by: str):
    """건수 집계에 쓸 그룹 값을 이슈에서 뽑는 함수를 돌려준다."""
    if group_by == "customer":
        return lambda issue: str(issue.get("project") or "")
    if group_by == "status":
        return lambda issue: display_status_label(issue.get("status"))
    if group_by == "owner":
        return lambda issue: str(issue.get("handler") or "")
    if group_by == "month":
        # 등록일 YYYY-MM-DD에서 월까지만 남긴다.
        return lambda issue: str(issue.get("created_at") or "")[:7]
    return None


def format_count_group_lines(group_counts: Dict[str, int], group_by: str) -> List[str]:
    """그룹별 건수를 많은 순으로 정리한다."""
    if not group_counts:
        return []
    label = COUNT_GROUP_LABELS.get(group_by, "그룹")
    if group_by == "month":
        # 추이는 많은 순이 아니라 시간순으로 봐야 의미가 있다.
        ordered = sorted(group_counts.items(), key=lambda item: item[0])
        lines = ["", f"{label}별 건수 (시간순, {len(ordered)}개 {label})"]
    else:
        ordered = sorted(group_counts.items(), key=lambda item: (-item[1], item[0]))
        lines = ["", f"{label}별 건수 (많은 순, {len(ordered)}개 {label})"]
    lines.extend(f"- {name} — {count}건" for name, count in ordered)
    return lines


def build_mantis_count_report(
    text: str,
    query_plan: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Execute a deterministic count plan instead of formatting a top-N list."""
    if query_plan.get("operation") != "count" or query_plan.get("source") not in {"mantis", "both"}:
        return None

    constraints = merge_query_plan_constraints(extract_query_constraints(text), query_plan)
    group_by = str(query_plan.get("group_by") or "")
    # "상태별 이슈 개수"처럼 다른 조건이 없어도 그룹 집계 자체가 요청이다.
    # 예전에는 여기서 빠져나가 Notion 검색으로 흘러갔다.
    if not has_constraint(constraints) and not count_group_key(group_by):
        return None

    # "최신 N건 기준"은 반드시 집계 범위를 제한한다. 등록일 조건이 함께 있어도
    # 전체 로컬 스냅샷을 합치면 N건 창 밖의 과거 이슈가 섞여 결과가 부풀려진다.
    # 라이브 API를 사용할 수 있을 때는 해당 최신 창을 직접 순회한 결과를 기준으로
    # 집계한다. 로컬 스냅샷은 라이브 조회 실패 시의 보조 수단으로만 남긴다.
    date_range = constraints.get("created_at") if isinstance(constraints.get("created_at"), dict) else None
    explicit_snapshot = bool(re.search(r"최신\s*\d+\s*건\s*기준", text))
    snapshot_limit = extract_snapshot_limit(text) if explicit_snapshot else None
    snapshot = scan_mantis_documents(
        CHROMA_DB_PATH,
        lambda content: chroma_text_matches_constraints(content, constraints),
        normalize_chroma_issue_document,
        example_limit=10,
        scan_limit=snapshot_limit,
        group_key=count_group_key(group_by),
    )
    if not snapshot["available"]:
        return {
            "content": f"Mantis 집계 도구를 사용할 수 없습니다.\n\n{snapshot['reason']}",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "intent_plan": query_plan,
                "query_plan": query_plan,
                "stages": [{"source": "mantis_count", "status": "error", "reason": snapshot["reason"], "meta": {"operation": "count"}}],
            },
            "raw": {"mode": "deterministic_mantis_count"},
        }
    normalized_issues = snapshot["issues"]
    total_count = int(snapshot["matched_count"])
    scan_complete = not explicit_snapshot
    merged_live = False
    if date_range:
        # 스냅샷 등록일 누락으로 새는 건수를 라이브 결과로 보완한다. 스냅샷 매칭 ID와
        # 라이브 매칭 ID를 합집합해 중복 없이 정확한 총계를 낸다.
        # 질문에 "최신 N건 기준"이 있으면 그 창(페이지 수)을 그대로 따른다. 없으면
        # 기본 8페이지(=800건, 체커 기준과 동일)로 최근 구간을 커버한다.
        window_pages = 8
        window_match = re.search(r"최신\s*(\d+)\s*건\s*기준", text)
        if window_match:
            window_pages = max(1, min((int(window_match.group(1)) + 99) // 100, 30))
        live_map = collect_mantis_live_ids_in_range(constraints, max_pages=window_pages)
        if live_map is not None:
            if explicit_snapshot:
                # 최신 N건 창을 명시한 요청은 현재 API가 반환한 그 창만 사용한다.
                # 오래된 로컬 인덱스와 합집합하면 요청 범위를 벗어난 이슈가 다시
                # 포함되므로, 라이브 결과가 성공한 경우 이를 완전한 기준으로 본다.
                total_count = len(live_map)
                normalized_issues = sorted(
                    (normalize_issue(issue) for issue in live_map.values()),
                    key=lambda issue: int(issue.get("id") or 0),
                    reverse=True,
                )
            else:
                snapshot_ids = {str(i) for i in snapshot.get("matched_ids") or set()}
                union_ids = snapshot_ids | set(live_map.keys())
                total_count = len(union_ids)
                # 대표 이슈: 라이브에만 있는 건 중 최신 ID를 스냅샷 예시와 함께 노출한다.
                live_only = [live_map[i] for i in live_map if i not in snapshot_ids]
                live_only_norm = [normalize_issue(i) for i in live_only]
                combined = {int(i["id"]): i for i in normalized_issues}
                for i in live_only_norm:
                    combined.setdefault(int(i["id"]), i)
                normalized_issues = sorted(
                    combined.values(), key=lambda i: int(i.get("id") or 0), reverse=True
                )
            scan_complete = True
            merged_live = True
    criteria = format_issue_list_criteria(constraints, normalized_issues)
    if merged_live and explicit_snapshot:
        scope = f"Mantis 실시간 최신 {snapshot_limit}건 기준"
    elif merged_live:
        scope = "Mantis 스냅샷 + 실시간 병합 기준"
    elif explicit_snapshot:
        scope = f"최신 {snapshot_limit}건 기준"
    else:
        scope = f"Mantis 인덱스 전체 {snapshot['scanned_count']}건 기준"
    lines = [
        f"검색 기준: {criteria}, {scope}",
        "",
        f"총 {total_count}건입니다.",
    ]
    group_lines = format_count_group_lines(snapshot.get("group_counts") or {}, group_by)
    if group_lines:
        # 그룹 집계를 요청했으면 총계보다 그룹별 건수가 답의 본문이다.
        lines.extend(group_lines)
    if total_count:
        lines.extend(["", "대표 이슈"])
        for issue in normalized_issues[:5]:
            lines.append(
                f"- #{issue['id']} {issue['summary']} "
                f"({issue['project']} · {display_status_label(issue['status'])} · {issue['created_at'] or '미상'})"
            )
    representative_issues = normalized_issues[:10]
    retrieval = {
        "query": text,
        "keyword": "",
        "constraints": constraints,
        "intent_plan": query_plan,
        "query_plan": query_plan,
        "limit": len(representative_issues),
        "stages": [
            {
                "source": "mantis_count",
                "status": "ok",
                "meta": {
                    "operation": "count",
                    "snapshot_limit": snapshot_limit,
                    "scanned_count": snapshot["scanned_count"],
                    "scan_complete": scan_complete,
                    "total_count": total_count,
                    "displayed_count": len(representative_issues),
                    "applied_filters": constraints,
                    "tool": "mantis_chroma_snapshot",
                },
                "issues_list": representative_issues,
            }
        ],
    }
    sources = build_sources_from_issue_rows(collect_retrieved_issues(retrieval, 10))
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_count"},
    }


def build_mantis_month_count_report(text: str) -> Optional[Dict[str, Any]]:
    month_query = extract_month_count_query(text)
    if not month_query:
        return None

    constraints = extract_query_constraints(text)
    query_plan = build_query_plan(
        text,
        {"source": "mantis", "planner": "rule", "confidence": 1.0},
        constraints,
    )
    target_month = f"{month_query['year']}-{month_query['month']:02d}"
    snapshot_limit = month_query["snapshot_limit"]
    recent_issues = fetch_mantis_recent_issues(snapshot_limit)
    matched_issues = [
        issue
        for issue in recent_issues
        if issue_created_year_month(issue) == target_month
        and issue_matches_constraints(issue, constraints)
    ]
    matched_issues.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    normalized_issues = [normalize_issue(issue) for issue in matched_issues]
    criteria = format_issue_list_criteria(constraints, normalized_issues)
    lines = [
        f"검색 기준: {criteria}, 기간={target_month}, 기준=최신 {snapshot_limit}건",
        "",
        f"총 {len(matched_issues)}건입니다.",
    ]
    if matched_issues:
        lines.extend(["", "대표 이슈"])
        for issue in normalized_issues[:5]:
            lines.append(
                f"- #{issue['id']} {issue['summary']} "
                f"({issue['project']} · {display_status_label(issue['status'])} · {issue['created_at'] or '미상'})"
            )

    retrieval = {
        "query": text,
        "keyword": "",
        "constraints": constraints,
        "intent_plan": query_plan,
        "query_plan": query_plan,
        "limit": min(len(normalized_issues), 10) or 5,
        "stages": [
            {
                "source": "mantis_month_count",
                "status": "ok",
                "meta": {
                    "snapshot_limit": snapshot_limit,
                    "year": month_query["year"],
                    "month": month_query["month"],
                    "total_count": len(matched_issues),
                    "displayed_count": min(len(normalized_issues), 10),
                },
                "issues_list": normalized_issues[:10],
            }
        ],
    }
    return {
        "content": "\n".join(lines),
        "sources": build_sources_from_issue_rows(collect_retrieved_issues(retrieval, 10)),
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_month_count"},
    }


def should_return_deterministic_issue_list(text: str, constraints: Dict[str, Any]) -> bool:
    group_by = extract_group_by(text)
    if not has_constraint(constraints) and not group_by:
        return False
    normalized_text = normalize_for_match(text)
    if any(
        normalize_for_match(keyword) in normalized_text
        for keyword in ISSUE_ANALYSIS_INTENT_KEYWORDS
    ):
        return False
    return any(
        normalize_for_match(keyword) in normalized_text
        for keyword in ISSUE_LIST_INTENT_KEYWORDS
    )


def extract_focus_keywords_for_followups(text: str, max_terms: int = 2) -> List[str]:
    terms = build_keyword_terms(text, {})
    generic_terms = {
        "관련", "이슈", "검토", "문의", "요청", "현상", "문제", "추가", "더", "찾아줘",
        "알려줘", "보여줘", "정리해줘",
    }
    return [
        term
        for term in terms
        if len(term) >= 2 and term not in generic_terms and not term.isdigit()
    ][:max_terms]


def generate_follow_up_questions(
    question: str,
    sources: List[Dict[str, Any]],
    retrieval_result: Optional[Dict[str, Any]],
    mode: str,
) -> List[str]:
    if not sources:
        return []

    constraints = (retrieval_result or {}).get("constraints") or extract_query_constraints(question)
    followups: List[str] = []

    def add(value: str) -> None:
        normalized = re.sub(r"\s+", " ", value).strip()
        if normalized and normalized not in followups:
            followups.append(normalized)

    # "확인된 이슈"처럼 상태 표현이 중의적으로 해석돼 결과가 걸러졌다면,
    # 다른 해석(상태 무관)을 한 번의 클릭으로 실행할 수 있는 칩을 맨 앞에 둔다.
    status_excluded = sum(
        int(stage.get("meta", {}).get("status_excluded_count") or 0)
        for stage in (retrieval_result or {}).get("stages", [])
    )
    if status_excluded and constraints.get("statuses"):
        add("상태 상관없이 다시 보여줘")

    mantis_sources = [source for source in sources if source.get("issue_id")]
    notion_sources = [source for source in sources if source.get("type") == "notion"]

    if mantis_sources:
        first = mantis_sources[0]
        issue_id = first.get("issue_id")
        project = first.get("project") or constraints.get("customer_key") or "해당 고객사"
        title = first.get("title") or ""
        detail = first.get("detail") or {}
        screen_number = detail.get("screen_number") or ""
        keywords = extract_focus_keywords_for_followups(title)

        if issue_id:
            add(f"#{issue_id} 변경 이력이나 참고할 만한 내용을 더 자세히 알려줘")
            add(f"#{issue_id}과 비슷한 과거 이슈 찾아줘")
        if keywords:
            if issue_id:
                add(f"{project}에서 #{issue_id}와 비슷한 과거 {' '.join(keywords)} 이슈 찾아줘")
            else:
                add(f"{project} {' '.join(keywords)} 관련 이슈 더 찾아줘")
        elif screen_number and screen_number != "미상":
            exclusion = f" 중 #{issue_id}를 제외하고" if issue_id else ""
            add(f"{project} 화면번호 {screen_number} 관련 이슈{exclusion} 더 찾아줘")
        if len(mantis_sources) > 1:
            add("이 결과를 담당자별로 묶어서 보여줘")
            add("이 이슈들의 공통 원인을 정리해줘")
        if not constraints.get("statuses"):
            add(f"{project}에서 종료되지 않은 관련 이슈만 보여줘")

    if notion_sources:
        first = notion_sources[0]
        title = first.get("title") or "이 항목"
        database = first.get("database_label") or "Notion"
        add(f"노션에서 '{title}' 상세 내용 정리해줘")
        add(f"{database}에서 같은 상태의 항목 더 보여줘")
        owner = first.get("owner")
        if owner and owner != "담당자 미지정" and not str(owner).startswith("미확인 담당자("):
            add(f"노션에서 {owner} 담당 항목만 보여줘")

    if mode == "deterministic_effort_report":
        add("이 공수를 이슈별로 상세하게 풀어서 보여줘")
        add("같은 기간 다른 담당자 공수와 비교해줘")

    return followups[:3]


def format_deterministic_combined_answer(sources: List[Dict[str, Any]]) -> str:
    merged_sources = merge_combined_sources(sources)
    notion_sources = [source for source in merged_sources if source.get("type") == "notion"]
    issue_sources = [source for source in merged_sources if source.get("type") != "notion"]
    duplicate_count = len(sources) - len(merged_sources)
    lines = [
        f"Notion과 Mantis를 함께 조회해 관련 자료 {len(merged_sources)}건을 찾았습니다.",
    ]
    if duplicate_count:
        lines.append(f"두 소스에서 제목이 같은 자료 {duplicate_count}건은 하나로 묶었습니다.")
    if notion_sources:
        lines.extend(["", "**Notion 자료**"])
        for source in notion_sources:
            source_badge = " · ".join(source.get("matched_sources", ["notion"]))
            lines.append(
                f"- **{source.get('title', '제목 없음')}** `[출처: {source_badge}]` "
                f"({source.get('database_label', 'Notion DB')} · {source.get('status', '상태 미상')})"
            )
    if issue_sources:
        lines.extend(["", "**Mantis 이슈**"])
        for source in issue_sources:
            issue_id = source.get("issue_id")
            issue_label = f"#{issue_id} " if issue_id else ""
            source_badge = " · ".join(source.get("matched_sources", ["mantis"]))
            lines.append(
                f"- **{issue_label}{source.get('title', '제목 없음')}** `[출처: {source_badge}]` "
                f"({source.get('project', '프로젝트 미상')} · "
                f"{display_status_label(source.get('status', '상태 미상'))})"
            )
    return "\n".join(lines)


def format_issue_list_criteria(
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
) -> str:
    criteria = []
    if constraints.get("similar_base_issue_ids"):
        criteria.append(
            "기준 이슈="
            + ", ".join(f"#{issue_id}" for issue_id in constraints["similar_base_issue_ids"])
        )
    if constraints.get("customer_key"):
        project = issues[0].get("project") if issues else ""
        customer = project or constraints["customer_key"]
        criteria.append(f"고객사={customer}")
    if constraints.get("platform_key"):
        criteria.append(f"플랫폼={constraints['platform_key'].upper()}")
    if constraints.get("excluded_platform_keys"):
        criteria.append(
            "플랫폼 제외=" + ", ".join(value.upper() for value in constraints["excluded_platform_keys"])
        )
    if constraints.get("excluded_customer_keys"):
        criteria.append("고객사 제외=" + ", ".join(constraints["excluded_customer_keys"]))
    if constraints.get("people", {}).get("names"):
        role = constraints.get("people", {}).get("role") or "any"
        role_label = "담당자" if role == "handler" else "요청자/본문" if role == "reporter" else "인물"
        criteria.append(f"{role_label}={', '.join(constraints['people']['names'])}")
    elif constraints.get("unassigned_people"):
        criteria.append("담당자=미지정")
    if constraints.get("statuses"):
        criteria.append(f"상태={', '.join(constraints['statuses'])}")
    if constraints.get("excluded_statuses"):
        criteria.append(f"상태 제외={', '.join(constraints['excluded_statuses'])}")
    if constraints.get("screen_numbers"):
        criteria.append(f"화면번호={', '.join(constraints['screen_numbers'])}")
    if constraints.get("issue_ids"):
        criteria.append(f"이슈 ID={', '.join(str(issue_id) for issue_id in constraints['issue_ids'])}")
    for field_name, field_value in constraints.get("date_fields", {}).items():
        criteria.append(f"{field_name}={field_value}")
    created_at = constraints.get("created_at") or {}
    if created_at.get("from") and created_at.get("to"):
        criteria.append(f"등록일={created_at['from']}~{created_at['to']}")
    for clause in normalize_filter_clauses(constraints.get("filter_clauses") or []):
        if clause["field"] in {"title", "keyword"}:
            criteria.append(filter_clause_label(clause))
    return ", ".join(criteria) or "요청 조건"


def display_status_label(status: str) -> str:
    normalized = str(status or "").strip()
    status_labels = {
        "open": "미종료",
        "closed": "종료된 이슈",
        "confirmed": "확인된 이슈",
        "assigned": "할당된 이슈",
        "resolved": "해결된 이슈",
        "feedback": "정보 부족",
        "developed": "개발완료",
        "new": "신규",
    }
    return status_labels.get(normalized.casefold(), normalized or "상태 미상")


def build_issue_subject_phrase(
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
) -> str:
    """결정형 답변 도입부에 쓸 자연스러운 주어 구를 만든다. (예: '신한투자증권 HTS')"""
    parts: List[str] = []
    if constraints.get("customer_key"):
        parts.append((issues[0].get("project") if issues else "") or constraints["customer_key"])
    if constraints.get("platform_key"):
        parts.append(constraints["platform_key"].upper())
    names = constraints.get("people", {}).get("names")
    if names:
        role = constraints.get("people", {}).get("role") or "any"
        prefix = "담당자 " if role == "handler" else ""
        parts.append(f"{prefix}{', '.join(names)}")
    elif constraints.get("unassigned_people"):
        parts.append("담당자 미지정")
    if constraints.get("screen_numbers"):
        parts.append(f"화면번호 {', '.join(constraints['screen_numbers'])}")
    return " ".join(part for part in parts if part).strip()


def build_issue_intro_line(
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
    count: int,
) -> str:
    """검색된 이슈 목록 앞에 붙일 대화형 한 문장을 만든다."""
    if constraints.get("similar_base_issue_ids"):
        base_ids = ", ".join(f"#{issue_id}" for issue_id in constraints["similar_base_issue_ids"])
        if count == 0:
            return f"🔍 {base_ids}과 비슷한 과거 이슈를 지금 조건으로는 찾지 못했어요."
        return f"🔍 {base_ids}과 비슷한 과거 이슈 {count}건을 찾았어요."
    if constraints.get("issue_ids"):
        if count == 0:
            # 존재하지 않는 번호를 조회했을 때 비슷한 ID로 대신 답하지 않고
            # 찾지 못했다는 사실을 그대로 알린다.
            issue_labels = ", ".join(f"#{issue_id}" for issue_id in constraints["issue_ids"])
            return f"🔍 {issue_labels} 이슈를 찾지 못했어요. 이슈 번호를 다시 확인해 주세요."
        return "📄 요청하신 이슈 내용을 정리했어요."
    subject = build_issue_subject_phrase(constraints, issues)
    prefix = f"{subject} 관련 " if subject else ""
    if count == 0:
        tail = f"{prefix}이슈는 지금 조건으로는 찾지 못했어요." if prefix else "조건에 맞는 이슈를 찾지 못했어요."
        return f"🔍 {tail} 기간이나 상태 같은 조건을 바꿔서 다시 찾아볼까요?"
    return f"🔍 {prefix}이슈 {count}건을 찾았어요. 최신 등록순으로 정리했습니다."


def build_status_excluded_note(
    constraints: Dict[str, Any],
    retrieval_result: Dict[str, Any],
) -> Optional[str]:
    """상태 필터 때문에 제외된 이슈가 있으면 사용자에게 복구 경로를 알려주는 문장.

    "확인된 이슈 찾아줘"처럼 상태 표현이 중의적인 질문에서 해석이 사용자 의도와
    어긋났을 때, 결과가 왜 적은지와 어떻게 넓힐 수 있는지를 스스로 알 수 있게 한다."""
    if not constraints.get("statuses"):
        return None
    excluded = sum(
        int(stage.get("meta", {}).get("status_excluded_count") or 0)
        for stage in retrieval_result.get("stages", [])
    )
    if not excluded:
        return None
    status_labels = ", ".join(
        display_status_label(status) for status in constraints["statuses"]
    )
    return (
        f"참고: 상태 조건({status_labels}) 때문에 제외된 이슈가 {excluded}건 있어요. "
        "상태와 무관하게 보려면 '상태 상관없이 다시 보여줘'라고 말해주세요."
    )


def build_unapplied_filters_note(retrieval_result: Dict[str, Any]) -> Optional[str]:
    unapplied: List[Dict[str, Any]] = []
    for stage in retrieval_result.get("stages", []):
        for clause in stage.get("meta", {}).get("filter_audit", {}).get("unapplied_filters", []):
            if clause not in unapplied:
                unapplied.append(clause)
    if not unapplied:
        return None
    labels = ", ".join(filter_clause_label(clause) for clause in unapplied)
    return f"주의: 현재 검색 소스에서 적용하지 못한 조건이 있습니다: {labels}."


def wants_issue_history_detail(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(
        normalize_for_match(marker) in normalized
        for marker in ("변경 이력", "히스토리", "참고할 만한", "처리 과정", "진행 과정")
    )


def format_issue_history_sections(detail: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    history = detail.get("history") or []
    meaningful_history = []
    for item in history:
        change = str(item.get("change") or "").strip()
        message = str(item.get("message") or "").strip()
        if message.startswith("이슈노트 추가됨"):
            continue
        if "=>" in change:
            old_value, new_value = (part.strip() for part in change.split("=>", 1))
            if old_value and old_value == new_value:
                continue
        if change or message:
            meaningful_history.append(item)
    if meaningful_history:
        lines.extend(["", "**변경 이력**"])
        for item in meaningful_history:
            event = item.get("change") or item.get("message") or "변경"
            field = f"{item.get('field')}: " if item.get("field") else ""
            user = f" · {item.get('user')}" if item.get("user") and item.get("user") != "미상" else ""
            timestamp = str(item.get("created_at") or "일시 미상").replace("T", " ")
            lines.append(f"- {timestamp} · {field}{event}{user}")

    notes = detail.get("notes") or []
    if notes:
        lines.extend(["", "**참고 노트**"])
        for note in notes:
            reporter = f" · {note.get('reporter')}" if note.get("reporter") and note.get("reporter") != "미상" else ""
            timestamp = str(note.get("created_at") or "일시 미상").replace("T", " ")
            lines.append(f"- {timestamp}{reporter}")
            lines.append(f"  {compact_text(note.get('text', ''), 900)}")
    return lines


def format_deterministic_issue_answer(
    query_text: str,
    constraints: Dict[str, Any],
    retrieval_result: Dict[str, Any],
    limit: int,
    answer_style: str = "normal",
) -> str:
    group_by = str(
        (retrieval_result.get("intent_plan") or {}).get("group_by")
        or extract_group_by(query_text)
        or ""
    )
    issues = select_grouped_issue_rows(
        collect_retrieved_issues(retrieval_result, limit),
        group_by,
    )
    display_constraints = constraints
    similar_base_issue_ids = similar_base_issue_ids_from_retrieval(retrieval_result)
    if similar_base_issue_ids:
        display_constraints = {
            **constraints,
            "issue_ids": [],
            "similar_base_issue_ids": similar_base_issue_ids,
        }
    criteria = format_issue_list_criteria(display_constraints, issues)
    if group_by == "customer":
        criteria = f"{criteria}, 고객사별 최신 이슈 1건" if criteria != "요청 조건" else "고객사별 최신 이슈 1건"
    status_note = build_status_excluded_note(display_constraints, retrieval_result)
    unapplied_note = build_unapplied_filters_note(retrieval_result)
    snapshot_note = build_snapshot_note(retrieval_result)
    if not issues:
        intro = build_issue_intro_line(display_constraints, issues, 0)
        sort_label = "의미 유사도 기준" if similar_base_issue_ids else "최신 이슈 ID 기준"
        parts = [intro, "", f"검색 기준: {criteria}, 정렬: {sort_label}"]
        if status_note:
            parts.extend(["", status_note])
        if unapplied_note:
            parts.extend(["", unapplied_note])
        if snapshot_note:
            parts.extend(["", snapshot_note])
        return "\n".join(parts)

    intro = (
        f"🔍 고객사 {len(issues)}곳의 최신 이슈를 하나씩 정리했어요."
        if group_by == "customer"
        else build_issue_intro_line(display_constraints, issues, len(issues))
    )

    if display_constraints.get("issue_ids"):
        lines = [intro, "", f"검색 기준: {criteria}, 정렬: 최신 이슈 ID 기준", ""]
        for issue in issues:
            detail = issue.get("detail", {}) or {}
            description = detail.get("description") or issue.get("preview") or "본문 내용이 없습니다."
            date_fields = {
                key: value
                for key, value in (detail.get("date_fields") or {}).items()
                if value
            }
            lines.extend(
                [
                    f"**#{issue['id']} {issue['summary']}**",
                    "",
                    f"- 프로젝트: {issue['project']}",
                    f"- 상태: {display_status_label(issue['status'])}",
                    f"- 담당자: {issue['handler']}",
                    f"- 등록일: {issue['created_at'] or '미상'}",
                    f"- 최종수정일: {detail.get('updated_at') or '미상'}",
                    f"- 화면번호: {detail.get('screen_number') or '미상'}",
                    *[f"- {field}: {value}" for field, value in date_fields.items()],
                    "",
                    "**내용**",
                    description.strip(),
                ]
            )
            if wants_issue_history_detail(query_text):
                lines.extend(format_issue_history_sections(detail))
        return "\n".join(lines)

    lines = [
        intro,
        "",
        f"검색 기준: {criteria}, 정렬: {'의미 유사도 기준' if similar_base_issue_ids else '최신 이슈 ID 기준'}",
        "",
    ]
    for index, issue in enumerate(issues, start=1):
        lines.append(
            f"{index}. **ID**: {issue['id']} | "
            f"**프로젝트**: {issue['project']} | "
            f"**제목**: {issue['summary']} | "
            f"**상태**: {display_status_label(issue['status'])} | "
            f"**담당자**: {issue['handler']} | "
            f"**등록일**: {issue['created_at'] or '미상'}"
        )
        if answer_style == "detailed":
            preview = compact_text(issue.get("preview", ""), 220)
            if preview:
                lines.append(f"   - 요약: {preview}")

    found_count = sum(
        int(stage.get("meta", {}).get("found_count") or stage.get("meta", {}).get("displayed_count") or 0)
        for stage in retrieval_result.get("stages", [])
        if stage.get("status") == "ok" and not stage.get("meta", {}).get("reference_only")
    )
    requested_limit = extract_requested_limit(query_text)
    if group_by == "customer":
        lines.extend(["", f"총 {len(issues)}개 고객사의 최신 이슈를 확인했습니다."])
    elif found_count and found_count > len(issues):
        lines.extend(["", f"조건에 맞는 이슈는 총 {found_count}건이며, 이 중 {len(issues)}건까지 표시했습니다. 더 보려면 표시 개수를 늘리거나 기간을 좁혀 다시 요청해 주세요."])
    elif requested_limit and found_count and found_count < requested_limit:
        lines.extend(["", f"요청한 {requested_limit}건 중 현재 조회 가능한 결과는 {len(issues)}건입니다."])
    if status_note:
        lines.extend(["", status_note])
    if unapplied_note:
        lines.extend(["", unapplied_note])
    if snapshot_note:
        lines.extend(["", snapshot_note])
    return "\n".join(lines)


def select_grouped_issue_rows(
    issues: List[Dict[str, Any]],
    group_by: Optional[str],
) -> List[Dict[str, Any]]:
    """For customer grouping, retain the newest issue for each project."""
    if group_by != "customer":
        return issues

    selected: List[Dict[str, Any]] = []
    seen_projects = set()
    for issue in sorted(issues, key=lambda item: int(item.get("id") or 0), reverse=True):
        project_key = re.sub(
            r"_project$",
            "",
            normalize_for_match(str(issue.get("project") or "")),
            flags=re.IGNORECASE,
        )
        if not project_key or project_key in seen_projects:
            continue
        seen_projects.add(project_key)
        selected.append(issue)
    return selected


def format_model_timeout_fallback(
    constraints: Dict[str, Any],
    retrieval_result: Dict[str, Any],
    limit: int,
) -> str:
    issues = collect_retrieved_issues(retrieval_result, limit)
    criteria = format_issue_list_criteria(constraints, issues)
    if not issues:
        return (
            "모델 분석 응답이 지연되어 검색 결과 기준으로 우선 확인했지만, "
            f"{criteria} 조건에 맞는 이슈를 찾지 못했습니다."
        )

    titles = " ".join(issue.get("summary", "") for issue in issues)
    theme_keywords = [
        ("차트/지표 표시", ["차트", "지표", "표시", "그래프"]),
        ("검색/조회 결과", ["검색", "조회", "조건"]),
        ("데이터/동기화", ["데이터", "갱신", "동기화", "상이"]),
        ("설정/저장", ["설정", "저장", "불러오기"]),
        ("성능/화면 동작", ["팝업", "로딩", "스크롤", "종료", "멈춤"]),
    ]
    themes = [
        label
        for label, keywords in theme_keywords
        if any(keyword in titles for keyword in keywords)
    ]

    lines = [
        "모델 분석 응답이 지연되어, 검색 결과 기준으로 우선 정리합니다.",
        "",
        f"검색 기준: {criteria}, 정렬: 최신 이슈 ID 기준",
    ]
    if themes:
        lines.extend(["", f"반복적으로 보이는 유형: {', '.join(themes[:3])}"])

    lines.extend(["", "관련 이슈"])
    for index, issue in enumerate(issues, start=1):
        lines.append(
            f"{index}. #{issue['id']} {issue['summary']} "
            f"- {issue['project']} / {display_status_label(issue['status'])} / "
            f"담당자 {issue['handler']} / 등록일 {issue['created_at'] or '미상'}"
        )
    lines.extend(
        [
            "",
            "정밀한 원인 분석은 회사 모델 응답이 정상화되면 같은 질문으로 다시 시도해 주세요.",
        ]
    )
    return "\n".join(lines)


def display_constraints(constraints: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if constraints.get("customer_key"):
        items.append(
            {
                "id": "customer",
                "label": "고객사",
                "value": constraints["customer_key"],
                "values": list(dict.fromkeys([constraints["customer_key"], *constraints.get("customer_aliases", [])])),
            }
        )
    if constraints.get("platform_key"):
        items.append(
            {
                "id": "platform",
                "label": "플랫폼",
                "value": constraints["platform_key"],
                "values": list(dict.fromkeys([constraints["platform_key"], *constraints.get("platform_aliases", [])])),
            }
        )
    if constraints.get("excluded_platform_keys"):
        values = [str(value).upper() for value in constraints["excluded_platform_keys"]]
        items.append(
            {
                "id": "platform_exclude",
                "label": "플랫폼 제외",
                "value": ", ".join(values),
                "values": values,
            }
        )
    if constraints.get("excluded_customer_keys"):
        values = list(constraints["excluded_customer_keys"])
        items.append(
            {
                "id": "customer_exclude",
                "label": "고객사 제외",
                "value": ", ".join(values),
                "values": values,
            }
        )
    people = constraints.get("people", {}).get("names", [])
    if people:
        items.append({"id": "people", "label": "담당자/인물", "value": ", ".join(people), "values": people})
    elif constraints.get("unassigned_people"):
        items.append(
            {
                "id": "people_unassigned",
                "label": "담당자",
                "value": "미지정",
                "values": ["담당자 미지정"],
            }
        )
    if constraints.get("statuses"):
        status_values = list(
            dict.fromkeys(
                [
                    value
                    for status in constraints["statuses"]
                    for value in [status, *STATUS_ALIASES.get(status, []), *STATUS_MATCH_ALIASES.get(status, [])]
                ]
            )
        )
        items.append(
            {
                "id": "status",
                "label": "상태",
                "value": ", ".join(constraints["statuses"]),
                "values": status_values,
            }
        )
    if constraints.get("excluded_statuses"):
        values = list(constraints["excluded_statuses"])
        items.append(
            {
                "id": "status_exclude",
                "label": "상태 제외",
                "value": ", ".join(values),
                "values": values,
            }
        )
    if constraints.get("issue_ids"):
        issue_ids = list(map(str, constraints["issue_ids"]))
        items.append({"id": "issue", "label": "이슈 ID", "value": ", ".join(issue_ids), "values": issue_ids})
    if constraints.get("screen_numbers"):
        items.append(
            {
                "id": "screen",
                "label": "화면번호",
                "value": ", ".join(constraints["screen_numbers"]),
                "values": constraints["screen_numbers"],
            }
        )
    for field_name, field_value in constraints.get("date_fields", {}).items():
        items.append(
            {
                "id": "date",
                "label": field_name,
                "value": field_value,
                "values": [field_name, field_value],
            }
        )
    created_at = constraints.get("created_at") or {}
    if created_at.get("from") and created_at.get("to"):
        items.append(
            {
                "id": "created_at",
                "label": "등록일",
                "value": f"{created_at['from']}~{created_at['to']}",
                "values": [created_at["from"], created_at["to"]],
            }
        )
    return items
