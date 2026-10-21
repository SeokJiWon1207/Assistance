"""장애 초동 대응 템플릿을 근거 중심의 결정형 문서로 만든다."""

import re
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.display import customer_name, status_name
from backend.common import compact_text, extract_quoted_value
from backend.intent.constants import TRIAGE_REQUEST_PATTERN
from backend.sources.chroma import execute_chroma_search
from backend.sources.mantis import build_sources_from_issue_rows
from backend.understanding import extract_query_constraints


TRIAGE_SIMILAR_LIMIT = 3


def _symptom_checks(symptom: str) -> tuple[List[str], List[str]]:
    """입력 증상에 맞는 확인 항목을 제시하며 원인은 단정하지 않는다."""
    scope = ["전체 사용자/특정 계정, 전체 화면/특정 기능 중 어디까지 발생하는지 구분합니다."]
    checks = [
        "발생 시각, 재현 절차, 화면 경로, 오류 문구와 최근 변경 사항을 확보합니다.",
        "동일 환경과 별도 환경에서 재현하고 버전·OS·네트워크별 차이를 기록합니다.",
        "해당 기능의 클라이언트 오류 로그와 서버/API 응답을 같은 시각대로 대조합니다.",
    ]
    if any(word in symptom for word in ("차트", "시세", "지표")):
        scope.append("종목·조회 기간·주기별 표시 여부와 실시간/과거 데이터 수신 범위를 비교합니다.")
        checks.append("차트·시세 데이터 응답, 갱신 시각과 화면 렌더링 오류를 확인합니다.")
    if "주문" in symptom:
        scope.append("모든 주문/특정 상품·주문유형 중 어디까지 발생하는지 구분합니다.")
        checks.append("주문 전송·접수·체결 기록으로 주문 누락 여부와 중복 주문 가능성을 확인합니다.")
    if any(word in symptom for word in ("로그인", "인증", "세션")):
        scope.append("로그인·인증 성공 여부와 이후 기능 진입 실패를 구분합니다.")
        checks.append("인증 API 응답과 로그인 세션 만료·권한 상태를 확인합니다.")
    checks.append("영향이 계속되면 검증된 우회 경로의 유무를 확인하고 로그와 재현 절차를 담당자에게 전달합니다.")
    return scope, checks


def build_incident_triage_report(text: str, *, search_similar: bool = True) -> Optional[Dict[str, Any]]:
    """증상·영향·초동 확인·인계 항목을 실제 유사 이슈와 함께 정리한다."""
    if not TRIAGE_REQUEST_PATTERN.search(str(text or "")):
        return None

    constraints = extract_query_constraints(text)
    symptom_match = re.search(r"증상\s*[:：]\s*([^\n]+)", text)
    symptom = extract_quoted_value(text) or (symptom_match.group(1).strip() if symptom_match else "입력한 장애 증상")
    urgency_match = re.search(r"(긴급|높음|일반)\s*우선순위|우선순위\s*[:：]?\s*(긴급|높음|일반)", text)
    urgency = next((value for value in urgency_match.groups() if value), "일반") if urgency_match else "일반"
    platform = str(constraints.get("platform_key") or "").upper()
    customer = str(constraints.get("customer_key") or "전체 고객사")

    # 고객사까지 고정하면 과거 사례가 하나도 없는 신규 장애에서 아무 도움도 주지
    # 못한다. 플랫폼 조건만 유지해 다른 고객사의 유사 장애를 참고 사례로 찾는다.
    similar_constraints: Dict[str, Any] = {
        "platform_key": constraints.get("platform_key"),
        "platform_aliases": constraints.get("platform_aliases") or [],
        # platform_key/aliases가 스냅샷과 라이브 양쪽에서 같은 의미로 적용된다.
        # 공통 filter_clauses까지 중복 전달하면 Chroma 문서의 표시값 차이로
        # 후보가 모두 탈락할 수 있다.
        "filter_clauses": [],
        "semantic_focus_terms": [],
    }
    result: Dict[str, Any] = {"status": "skipped", "reason": "유사 사례 검색 도구가 비활성화되어 있습니다."}
    if search_similar:
        try:
            result = execute_chroma_search(symptom, TRIAGE_SIMILAR_LIMIT, similar_constraints, semantic=True)
        except (requests.RequestException, HTTPException) as exc:
            result = {"status": "error", "reason": f"유사 사례 검색에 실패했습니다 ({type(exc).__name__})."}
    documents = (result.get("documents") or []) if result.get("status") == "ok" else []
    examples: List[Dict[str, Any]] = []
    for document in documents:
        issue = document.get("latest_issue") or {}
        if issue and issue.get("id"):
            examples.append(issue)

    scope_label = " · ".join(value for value in (customer, platform) if value)
    scope_checks, initial_checks = _symptom_checks(symptom)
    lines = [
        f"장애 초동 대응안 — {scope_label}",
        f"증상: **{symptom}**",
        "", "아래는 확인할 항목이며, 실제 영향 범위와 원인은 아직 확인되지 않았습니다.",
        "", "영향 범위 확인", "",
        *[f"- {item}" for item in scope_checks],
        "", "초동 확인 순서", "",
        *[f"{index}. {item}" for index, item in enumerate(initial_checks, 1)],
    ]
    lines.extend([
        "",
        "담당자 전달 내용",
        f"- 우선순위: {urgency}",
        f"- 고객사/플랫폼: {scope_label}",
        f"- 증상: {symptom}",
        "- 필수 첨부: 발생 시각, 영향 사용자·기능, 앱 버전, 재현 절차, 화면 캡처, 클라이언트/API 로그",
        "- 함께 전달할 판단: 영향 사용자·기능 범위, 재현 여부, 우회 가능 여부, 다음 확인 담당자와 회신 시각",
    ])

    lines.extend(["", "유사 사례", ""])
    if examples:
        lines.append("- 고객사 제한을 풀고 같은 플랫폼의 과거 사례를 찾았습니다. 원인이 같다고 단정하지 않습니다.")
        for issue in examples:
            lines.append(
                f"- #{issue.get('id')} {compact_text(str(issue.get('summary') or ''), 100)} "
                f"({customer_name(issue.get('project'))} · {status_name(issue.get('status'))})"
            )
    elif result.get("status") != "ok":
        lines.append(f"- {result.get('reason') or '유사 사례 검색을 완료하지 못했습니다.'}")
    else:
        lines.append("- 현재 검색 범위에서는 참고할 만한 과거 사례를 찾지 못했습니다.")



    sources = build_sources_from_issue_rows(examples)
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{
                "source": "chroma_incident_triage",
                "status": result.get("status", "error"),
                "reason": result.get("reason", ""),
                "issues_list": examples,
                "meta": {
                    "displayed_count": len(examples),
                    "reference_issue_ids": [issue['id'] for issue in examples],
                    "similar_scope": "platform" if platform else "all",
                },
            }],
        },
        "raw": {"mode": "deterministic_incident_triage"},
    }
