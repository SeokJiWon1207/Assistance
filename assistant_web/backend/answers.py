"""결정론적 답변 생성 — 이슈/Notion 목록 포맷과 후속 질문(Layer 5).

검색 결과를 모델 없이 곧바로 사용자 답변 텍스트로 조립하는 계층.
집계 리포트는 ``backend.reports.aggregate``에 두고 기존 import 계약만 재노출한다.
답변 스타일 지침과 검증 화면용 제약조건 표시(display_constraints)도 여기에 둔다.

의존: retrieval + sources + understanding.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.display import customer_name
from backend.issue_text import encode_issue_field
from backend.common import compact_text, normalize_for_match
from backend.config import STATUS_ALIASES, STATUS_MATCH_ALIASES
from backend.intent.entities import extract_issue_ids
from backend.query_planning import extract_group_by
from backend.query_filters import filter_clause_label
from backend.reports.aggregate import (
    COUNT_GROUP_LABELS as COUNT_GROUP_LABELS,
    PIVOT_AXIS_PATTERNS as PIVOT_AXIS_PATTERNS,
    PIVOT_MAX_MONTHS as PIVOT_MAX_MONTHS,
    PIVOT_MAX_ROWS as PIVOT_MAX_ROWS,
    PIVOT_MONTH_PATTERN as PIVOT_MONTH_PATTERN,
    SPIKE_BASELINE_MONTHS as SPIKE_BASELINE_MONTHS,
    SPIKE_MIN_RECENT as SPIKE_MIN_RECENT,
    SPIKE_QUESTION as SPIKE_QUESTION,
    SPIKE_RATIO as SPIKE_RATIO,
    SPIKE_TOP_N as SPIKE_TOP_N,
    TOP_CUSTOMER_QUESTION as TOP_CUSTOMER_QUESTION,
    build_mantis_count_report as build_mantis_count_report,
    build_mantis_month_count_report as build_mantis_month_count_report,
    build_mantis_pivot_report as build_mantis_pivot_report,
    build_mantis_spike_report as build_mantis_spike_report,
    build_top_customers_report as build_top_customers_report,
    count_group_key as count_group_key,
    display_status_label,
    extract_month_count_query as extract_month_count_query,
    format_count_group_lines as format_count_group_lines,
    format_issue_list_criteria,
    format_month_trend_lines as format_month_trend_lines,
)
from backend.retrieval import (
    build_snapshot_note,
    collect_retrieved_issues,
    collect_retrieved_customer_groups,
    count_retrieved_issues,
    merge_combined_sources,
    similar_base_issue_ids_from_retrieval,
)
from backend.understanding import (
    ISSUE_ANALYSIS_INTENT_KEYWORDS,
    ISSUE_LIST_INTENT_KEYWORDS,
    build_keyword_terms,
    extract_query_constraints,
    extract_requested_limit,
)


CONVERSATION_TONE_NOTE = (
    "고객사 이름은 근거의 customer_name에 있는 현재 표시명을 사용해라. "
    "project에 있는 Mantis 내부 코드나 구사명은 회사 이름 대신 출력하지 마라. 원문 인용은 그대로 보존해라. "
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


def customer_notice_instruction(question: str) -> str:
    """고객 전달 문안의 필수 항목과 원문에 없는 정보의 표시 방법."""
    normalized = normalize_for_match(question)
    document = re.search(r"고객안내(?:문|메일|메시지|문자)|메신저답변|점검공지|회의공유문", normalized)
    excludes_internal = "내부식별자" in normalized and any(word in normalized for word in ("제외", "빼"))
    if not (
        document
        and ("고객" in normalized or "외부" in normalized or excludes_internal)
        and any(action in normalized for action in ("작성", "만들", "초안", "써줘", "써주세요", "정리"))
    ):
        return ""
    if re.search(
        r"(?:작성|만들|쓰)(?:하지|지)(?:마|말|않)|"
        r"(?:안내문|안내메일|메신저답변|점검공지|회의공유문)(?:은|는|을|를)?말고",
        normalized,
    ):
        return ""
    instruction = (
        "고객에게 전달할 문안 작성 요청이다. 요청한 채널과 말투에 맞춰 바로 사용할 문안만 작성해라. "
        "AI 비서 소개, 작성 과정 설명, 문안 밖의 도입부나 후속 질문은 넣지 마라. "
        "문안 안에 '확인된 사실', '고객 영향', '진행 상황', '다음 안내 시점' 네 항목을 모두 명시해라. "
        "원문에 영향 범위나 다음 안내 일정이 없으면 해당 항목을 생략하지 말고 미확정이라고 표시해라. "
        "확인되지 않은 고객 수, 업무 중단, 원인, 복구 완료, 발송 일시와 다음 안내 시각을 만들어내지 마라. "
        "등록일·수정일·종료일을 고객에게 안내한 날짜로 바꾸지 마라. "
    )
    if excludes_internal:
        instruction += (
            "내부 이슈 번호, 내부 담당자 이름과 이관 내역은 제외하고 고객이 알아야 할 진행 상황으로 표현해라. "
            "화면번호나 주문번호처럼 고객이 알아야 할 업무 식별자는 보존해라."
        )
    return instruction


def sanitize_customer_notice_answer(question: str, content: str) -> str:
    """고객 전달용 문안에서 내부 Mantis 식별자를 제거한다."""
    normalized = normalize_for_match(question)
    if not (
        "내부식별자" in normalized
        and any(marker in normalized for marker in ("고객안내", "메신저답변", "점검공지", "회의공유문"))
    ):
        return content

    issue_ids = set(extract_issue_ids(question))

    def remove_issue_identifier(match: re.Match[str]) -> str:
        # 화면 #9580처럼 다른 업무 식별자가 명시된 경우는 보존한다.
        if re.search(
            r"(?:화면|주문|버전)\s*(?:번호\s*)?$", content[:match.start()],
        ):
            return match.group()
        if match.group().lower().startswith("id"):
            number = re.search(r"\d+", match.group())
            if not number or int(number.group()) not in issue_ids:
                return match.group()
        return ""

    sanitized = re.sub(
        r"(?<![\w])(?:이슈|Mantis|멘티스)\s*(?:ID|번호)?\s*[:#]?\s*\d+(?!\d)"
        r"|(?<![\w])(?:#|ID\s*[:#]?)\s*\d+(?!\d)",
        remove_issue_identifier,
        str(content or ""),
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"\(\s*\)", "", sanitized)
    sanitized = re.sub(r"(?<!\*)\*\*\s*\*\*(?!\*)|(?<!_)__\s*__(?!_)", "", sanitized)
    sanitized = re.sub(r"[ \t]+\n", "\n", sanitized)
    return sanitized.strip()


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
    return (
        "질문에서 표·길이·형식을 요청하면 그 요청을 따르고, "
        "요청이 없으면 필요한 만큼만 간결하고 명확하게 작성해라."
    )




def wants_table_output(text: str) -> bool:
    """질문이 표 형태를 요구하는지 본다."""
    return bool(re.search(r"표\s*(?:로|형태|형식)|테이블\s*(?:로|형태|형식)|표로\s*만들", str(text or "")))


def wants_detailed_output(text: str) -> bool:
    """질문이 이슈별 요약까지 담은 자세한 목록을 요구하는지 본다."""
    return bool(re.search(r"자세히|자세하게|상세히|상세하게|요약\s*(?:도|까지|포함)", str(text or "")))


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




def should_return_deterministic_issue_list(text: str, constraints: Dict[str, Any]) -> bool:
    group_by = extract_group_by(text)
    normalized_text = normalize_for_match(text)
    if constraints.get("issue_ids") and any(
        field in normalized_text for field in ("사유및원인", "조치및개선")
    ):
        return True
    # 그룹별 요약은 전체 후보의 건수를 기준으로 계산해야 한다. 모델에 상위 몇 건만
    # 넘기면 고객사별 건수가 틀릴 수 있으므로 결정형 포맷을 우선한다.
    if any(
        normalize_for_match(keyword) in normalized_text
        for keyword in ISSUE_ANALYSIS_INTENT_KEYWORDS - {"정리해줘", "요약해줘"}
    ):
        return False
    # 관련 사례를 접수한 곳/고객사 조회도 원문 목록으로 답한다.
    if re.search(r"(?:들어온|접수된|등록된)(?:곳|고객사)(?:은|는|이|가)?", normalized_text):
        return True
    if group_by and any(marker in normalized_text for marker in ("요약", "정리")):
        return True
    if re.search(r"안내문|메일|메시지|초안|작성|써줘|써주세요|인수인계", normalized_text):
        return False
    if constraints.get("issue_ids"):
        return True
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
    current_question_key = re.sub(
        r"[\s?!.,'\"“”‘’]+",
        "",
        str(question or ""),
    ).casefold()
    followup_keys: set[str] = set()

    def add(value: str) -> None:
        normalized = re.sub(r"\s+", " ", value).strip()
        normalized_key = re.sub(r"[\s?!.,'\"“”‘’]+", "", normalized).casefold()
        if (
            normalized
            and normalized_key
            and normalized_key != current_question_key
            and normalized_key not in followup_keys
        ):
            followups.append(normalized)
            followup_keys.add(normalized_key)

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
                f"({customer_name(source.get('project', '프로젝트 미상'))} · "
                f"{display_status_label(source.get('status', '상태 미상'))})"
            )
    return "\n".join(lines)




def build_issue_subject_phrase(
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
) -> str:
    """결정형 답변 도입부에 쓸 자연스러운 주어 구를 만든다. (예: '신한투자증권 HTS')"""
    parts: List[str] = []
    if constraints.get("customer_key"):
        project = issues[0].get("project") if issues else ""
        parts.append(customer_name(project) if project else constraints["customer_key"])
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
    order = '오래된 등록순' if constraints.get('sort', {}).get('direction') == 'asc' else '최신 등록순'
    return f"🔍 {prefix}이슈 {count}건을 찾았어요. {order}으로 정리했습니다."


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


def build_unverified_person_note(
    constraints: Dict[str, Any],
    count: int,
) -> Optional[str]:
    """명시했지만 명부에서 확인되지 않은 담당자의 0건 결과를 설명한다."""
    if count:
        return None
    origins = (constraints.get("people") or {}).get("name_origins") or {}
    names = [name for name, origin in origins.items() if origin == "explicit_unverified"]
    if not names:
        return None
    joined = ", ".join(names)
    return (
        f"참고: '{joined}'는 현재 담당자 명부에서 확인하지 못했어요. "
        "이름이 맞다면 아직 이슈 이력이 없거나 명부 갱신 전일 수 있어요."
    )


def build_mantis_dependency_note(retrieval_result: Dict[str, Any]) -> Optional[str]:
    """Mantis 장애를 정상적인 0건 결과와 구분해 알린다.

    로컬 스냅샷이 요청 기간보다 오래된 상태에서 실시간 단계까지 실패하면,
    결과가 비어 있어도 '없음'을 확정할 수 없다. 이 경우 조건 변경을 권하기보다
    외부 조회가 끝나지 않았다는 사실과 재시도 방법을 먼저 안내한다.
    """
    failed = [
        stage
        for stage in retrieval_result.get("stages", [])
        if str(stage.get("source") or "").startswith("mantis_")
        and stage.get("status") in {"error", "partial_error"}
        and stage.get("error_kind") == "dependency"
    ]
    if not failed:
        return None
    return (
        "Mantis 연결 오류로 실시간 조회를 완료하지 못했어요. "
        "현재 조건의 결과가 0건이라고 확정할 수 없습니다. 잠시 후 다시 시도해 주세요."
    )


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
    all_issues = collect_retrieved_issues(retrieval_result, None if group_by else limit)
    issues = select_grouped_issue_rows(all_issues, group_by)[:limit]
    found_count = count_retrieved_issues(retrieval_result)
    continuation = constraints.get("list_continuation") or {}
    customer_group_counts: Dict[str, int] = {}
    if group_by == "customer":
        labels = {_customer_group_key(issue): _issue_group_value(issue, group_by) for issue in issues}
        for project in collect_retrieved_customer_groups(retrieval_result).values():
            key = _customer_group_key({"project": project})
            label = labels.setdefault(key, _issue_group_value({"project": project}, group_by))
            customer_group_counts[label] = customer_group_counts.get(label, 0) + 1
    customer_counts_complete = sum(customer_group_counts.values()) == found_count
    display_constraints = constraints
    similar_base_issue_ids = similar_base_issue_ids_from_retrieval(retrieval_result)
    if similar_base_issue_ids:
        display_constraints = {
            **constraints,
            "issue_ids": [],
            "similar_base_issue_ids": similar_base_issue_ids,
        }
    criteria = format_issue_list_criteria(display_constraints, issues)
    requested_order = (constraints.get('sort') or {})
    sort_label = ('의미 유사도 기준' if similar_base_issue_ids else
                  '등록일 오래된 순' if requested_order.get('direction') == 'asc' else '최신 이슈 ID 기준')
    topic = str(retrieval_result.get("keyword") or "").strip() if not similar_base_issue_ids else ""
    if topic:
        criteria = f"{criteria}, 검색어={topic}"
    if group_by == "customer":
        criteria = f"{criteria}, 고객사별 최신 이슈 1건" if criteria != "요청 조건" else "고객사별 최신 이슈 1건"
    elif group_by == "platform":
        criteria = f"{criteria}, 매체별 구분" if criteria != "요청 조건" else "매체별 구분"
    status_note = build_status_excluded_note(display_constraints, retrieval_result)
    unapplied_note = build_unapplied_filters_note(retrieval_result)
    unverified_person_note = build_unverified_person_note(display_constraints, len(issues))
    snapshot_note = build_snapshot_note(retrieval_result)
    dependency_note = build_mantis_dependency_note(retrieval_result)
    incomplete = any(
        stage.get("meta", {}).get("search_complete") is False
        for stage in retrieval_result.get("stages", [])
        if str(stage.get("source") or "").startswith("mantis_")
    )
    if incomplete and not issues:
        dependency_note = dependency_note or "조회한 범위에 일치하는 이슈가 없습니다. 전체 조회가 완료되지 않아 0건으로 확정할 수 없습니다."
    if incomplete and issues:
        snapshot_note = (snapshot_note + "\n" if snapshot_note else "") + "일부 범위의 결과입니다. 전체 건수와 최신순은 확정할 수 없습니다."
    complete = any(stage.get("status") == "ok" and stage.get("meta", {}).get("search_complete") is True
                   for stage in retrieval_result.get("stages", [])
                   if str(stage.get("source") or "").startswith("mantis_"))
    if not issues and not customer_group_counts:
        intro = ("같은 검색 조건의 이슈는 이미 모두 표시했습니다. 추가로 표시할 이슈가 없습니다."
                 if continuation and complete and not incomplete else
                 dependency_note or build_issue_intro_line(display_constraints, issues, 0))
        if topic and not dependency_note and not (continuation and complete and not incomplete):
            intro = f"🔍 ‘{topic}’ 주제와 요청 조건에 일치하는 이슈를 조회한 범위에서 찾지 못했어요."
        parts = [intro, "", f"검색 기준: {criteria}, 정렬: {sort_label}"]
        if status_note:
            parts.extend(["", status_note])
        if unapplied_note:
            parts.extend(["", unapplied_note])
        if unverified_person_note:
            parts.extend(["", unverified_person_note])
        if snapshot_note:
            parts.extend(["", snapshot_note])
        return "\n".join(parts)

    if group_by == "customer":
        intro = (
            f"🔍 조회된 총 {found_count}건에서 고객사 {len(issues)}곳의 최신 이슈를 "
            "하나씩 정리했어요."
        )
        if not customer_counts_complete:
            intro = f"🔍 조회된 총 {found_count}건 중 고객사를 확인한 범위의 요약입니다."
    elif group_by == "platform":
        platform_count = len({_issue_group_value(issue, group_by) for issue in issues})
        intro = f"🔍 조건에 맞는 이슈 {len(issues)}건을 매체 {platform_count}개로 나눠 정리했어요."
    else:
        intro = build_issue_intro_line(display_constraints, issues, len(issues))

    if display_constraints.get("issue_ids"):
        if (len(issues) == 1 and re.search(r'현재\s*상태만|상태만\s*(?:조회|알려|보여|확인)', query_text)
                and not re.search(r'원인|본문|조치|노트|이력', query_text)):
            issue = issues[0]
            detail = issue.get('detail') or {}
            lines = [f"**#{issue['id']}** 현재 상태는 **{display_status_label(issue.get('status_label') or issue['status'])}**입니다.",
                     f"근거: #{issue['id']}의 현재 상태 필드 · 최종수정일 {detail.get('updated_at') or '미상'}"]
            if snapshot_note:
                lines.extend(['', snapshot_note])
            return '\n'.join(lines)
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
                    f"- 프로젝트: {customer_name(issue['project'])}",
                    f"- 상태: {display_status_label(issue.get('status_label') or issue['status'])}",
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
            if any(field in normalize_for_match(query_text) for field in ("사유및원인", "조치및개선")):
                for field in ("사유 및 원인", "조치 및 개선"):
                    lines.extend(["", f"**{field}**", (detail.get("resolution_fields") or {}).get(field) or "원문에 등록되지 않았습니다."])
        return "\n".join(lines)

    lines = [
        intro,
        "",
        f"검색 기준: {criteria}, 정렬: {sort_label}",
        "",
    ]
    if group_by == "customer":
        lines.extend(["고객사별 건수" if customer_counts_complete else "고객사를 확인한 항목의 건수", "",
                      "| 고객사 | 이슈 수 |", "|---|---:|"])
        for label, count in sorted(customer_group_counts.items(), key=lambda item: (-item[1], item[0])):
            lines.append(f"| {customer_name(label).replace('|', r'\|')} | {count}건 |")
        lines.extend(["", "고객사별 대표 이슈", ""])
    if wants_table_output(query_text):
        # 표를 요청했는데 결정형 답변이 늘 목록만 내보내던 문제. 모델을 거치지
        # 않는 경로라 여기서 직접 표로 조립한다.
        lines.extend(
            [
                "| ID | 고객사 | 제목 | 상태 | 담당자 | 등록일 |",
                "|---|---|---|---|---|---|",
            ]
        )
        for issue in issues:
            title = encode_issue_field(issue["summary"])
            lines.append(
                f"| {issue['id']} | {encode_issue_field(customer_name(issue['project']))} | {title} | "
                f"{encode_issue_field(display_status_label(issue.get('status_label') or issue['status']))} | {encode_issue_field(issue['handler'])} | "
                f"{encode_issue_field(issue['created_at'] or '미상')} |"
            )
    else:
        group_counts: Dict[str, int] = {}
        if group_by in {"customer", "platform"}:
            for issue in issues:
                label = _issue_group_value(issue, group_by)
                group_counts[label] = (
                    customer_group_counts.get(label, 0)
                    if group_by == "customer"
                    else group_counts.get(label, 0) + 1
                )
        current_group = ""
        for index, issue in enumerate(issues, start=1):
            if group_by in {"customer", "platform"}:
                label = _issue_group_value(issue, group_by)
                if label != current_group:
                    current_group = label
                    heading = customer_name(label) if group_by == "customer" else label
                    lines.extend(["", f"**{heading} · {group_counts[label]}건**", ""])
            lines.append(
                f"{index}. **ID**: {issue['id']} | "
                f"**프로젝트**: {encode_issue_field(customer_name(issue['project']))} | "
                f"**제목**: {encode_issue_field(issue['summary'])} | "
                f"**상태**: {encode_issue_field(display_status_label(issue.get('status_label') or issue['status']))} | "
                f"**담당자**: {encode_issue_field(issue['handler'])} | "
                f"**등록일**: {encode_issue_field(issue['created_at'] or '미상')}"
            )
            # 화면의 답변 스타일 설정을 없앴으므로 질문 문구로도 판단한다.
            if answer_style == "detailed" or wants_detailed_output(query_text):
                preview = compact_text(issue.get("preview", ""), 220)
                if preview:
                    lines.append(f"   - 요약: {preview}")

    requested_limit = extract_requested_limit(query_text)
    if group_by == "customer":
        lines.extend([
            "",
            (f"총 {found_count}건을 {len(customer_group_counts)}개 고객사로 나눠 확인했습니다."
             if customer_counts_complete else
             f"조회된 총 {found_count}건 중 {sum(customer_group_counts.values())}건의 고객사별 건수를 확인했습니다."),
        ])
        if len(customer_group_counts) > len(issues):
            lines.append(f"대표 이슈는 {len(issues)}개 고객사까지 표시했습니다.")
    elif group_by == "platform":
        lines.extend(["", f"매체별 이슈를 합해 총 {len(issues)}건을 확인했습니다."])
    elif continuation:
        lines.extend(["", f"이전 검색 조건을 유지하고 이미 표시한 이슈를 제외했습니다. 이번에 {len(issues)}건을 추가로 표시했습니다."])
        if complete and not incomplete:
            remaining = max(0, found_count - len(issues))
            lines.append(f"아직 표시하지 않은 이슈는 {remaining}건입니다." if remaining else "현재 조회 범위의 나머지 이슈를 모두 표시했습니다.")
    elif found_count and found_count > len(issues):
        lines.extend(["", f"조건에 맞는 이슈는 총 {found_count}건이며, 이 중 {len(issues)}건을 표시했습니다. 나머지는 '나머지도 보여줘'라고 질문해 이어서 조회할 수 있습니다."])
    elif requested_limit and found_count and found_count < requested_limit:
        lines.extend(["", f"요청한 {requested_limit}건 중 현재 조회 가능한 결과는 {len(issues)}건입니다."])
    if status_note:
        lines.extend(["", status_note])
    if unapplied_note:
        lines.extend(["", unapplied_note])
    if dependency_note:
        lines.extend(["", dependency_note])
    if snapshot_note:
        lines.extend(["", snapshot_note])
    return "\n".join(lines)


def select_grouped_issue_rows(
    issues: List[Dict[str, Any]],
    group_by: Optional[str],
) -> List[Dict[str, Any]]:
    """For customer grouping, retain the newest issue for each project."""
    if group_by == "platform":
        return sorted(
            issues,
            key=lambda issue: (
                _issue_group_value(issue, group_by).casefold(),
                -int(issue.get("id") or 0),
            ),
        )
    if group_by != "customer":
        return issues

    selected: List[Dict[str, Any]] = []
    seen_projects = set()
    for issue in sorted(issues, key=lambda item: int(item.get("id") or 0), reverse=True):
        project_key = _customer_group_key(issue)
        if not project_key or project_key in seen_projects:
            continue
        seen_projects.add(project_key)
        selected.append(issue)
    return selected


def _customer_group_key(issue: Dict[str, Any]) -> str:
    return re.sub(
        r"_project$",
        "",
        normalize_for_match(str(issue.get("project") or "")),
        flags=re.IGNORECASE,
    )


def _issue_group_value(issue: Dict[str, Any], group_by: str) -> str:
    if group_by == "customer":
        return str(issue.get("project") or "고객사 미상").strip() or "고객사 미상"
    if group_by == "platform":
        detail = issue.get("detail") or {}
        return str(issue.get("media_type") or detail.get("media_type") or "기타/미상").strip() or "기타/미상"
    return "기타/미상"


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
            f"- {issue['project']} / {display_status_label(issue.get('status_label') or issue['status'])} / "
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
