"""확정 조회 조건을 각 원천 도구에 대응시키는 규칙.

계획한 원천에는 서버가 확정한 조건을 그대로 적용한다. 계획하지 않은 원천에는 두 원천에서
의미가 같은 조건(고객사·매체·본문 키워드)만 옮기고, 나머지는 지우지 않고 "적용 불가"로
기록한다. 적용하지 못한 조건이 하나라도 있으면 그 원천의 결과는 일반 참고(reference)
등급이며, 대상 이슈의 실제 원인·조치·완료 여부를 확정하는 근거로 쓰지 않는다.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Tuple

from backend.intent.scope_extraction import empty_query_constraints
from backend.query_filters import clause_supported, normalize_filter_clauses


# 두 원천에서 같은 의미로 확인할 수 있는 조건. 상태·담당자·기간은 필드 이름이 같아도
# 값 체계(Mantis 워크플로 상태, 이슈 담당자, 이슈 등록일)가 Notion 문서와 다르다.
MAPPABLE_FIELDS = frozenset({"customer", "platform", "keyword"})
FIELD_LABELS = {
    "customer": "고객사", "platform": "매체", "status": "상태", "owner": "담당자", "reporter": "요청자",
    "registered_reporter": "등록자", "issue_id": "이슈 번호", "screen_number": "화면번호",
    "created_at": "등록 기간", "delivered_at": "고객사전달일", "effort_person": "공수 작업자",
    "due_at": "목표일", "deployed_at": "배포일", "title": "제목", "keyword": "키워드", "database": "Notion DB",
}
# 필터 절이 아니라 해석 결과 키로만 남는 조건.
_SCOPE_KEYS = (
    ("statuses", "상태"), ("excluded_statuses", "상태"), ("issue_ids", "이슈 번호"),
    ("screen_numbers", "화면번호"), ("created_at", "등록 기간"), ("date_fields", "날짜 조건"),
    ("mantis_team_members", "팀 담당자 범위"), ("unassigned_people", "미지정 담당자"),
)
_CUSTOMER_KEYS = ("customer_key", "customer_aliases", "customer_exact", "excluded_customer_aliases")
_PLATFORM_KEYS = ("platform_key", "platform_aliases", "excluded_platform_aliases", "excluded_platform_keys")


@dataclass(frozen=True)
class SourceConditions:
    source: str
    constraints: Dict[str, Any]
    applied: Tuple[str, ...]
    unapplied: Tuple[str, ...]
    planned: bool

    @property
    def grade(self) -> str:
        return "reference" if self.unapplied else "scoped"


def planned_sources(plan: Dict[str, Any]) -> frozenset[str]:
    source = str(plan.get("source") or "")
    return frozenset({"mantis", "notion"}) if source == "both" else frozenset({source})


def _label(field: str) -> str:
    return FIELD_LABELS.get(field, field)


def project_conditions(plan: Dict[str, Any], constraints: Dict[str, Any], source: str) -> SourceConditions:
    clauses = normalize_filter_clauses(constraints.get("filter_clauses") or [])
    # 통합 플래너는 both의 개인·팀·상태 조건을 Mantis 기준으로 확정한다.
    # Notion을 함께 검색한다고 해서 이슈 담당자가 문서 담당자로 바뀌지는 않는다.
    native = plan.get("source") == source or (plan.get("source") == "both" and source == "mantis")
    shared_fields = MAPPABLE_FIELDS
    if plan.get("source") == "both" and source == "notion":
        shared_fields = shared_fields.union({"database", "due_at", "deployed_at"})
    mapped = [clause for clause in clauses if (native or clause["field"] in shared_fields)
              and clause_supported(source, clause)]
    unapplied = [_label(clause["field"]) for clause in clauses if clause not in mapped]
    applied = tuple(dict.fromkeys(_label(clause["field"]) for clause in mapped))
    if native:
        projected = deepcopy(constraints)
        projected["filter_clauses"] = deepcopy(mapped)
        return SourceConditions(source, projected, applied, tuple(dict.fromkeys(unapplied)), True)
    people = constraints.get("people") or {}
    if people.get("names"):
        unapplied.append("담당자" if people.get("role") in {None, "", "handler"} else _label(str(people["role"])))
    unapplied.extend(label for key, label in _SCOPE_KEYS if constraints.get(key))
    fields = {clause["field"] for clause in mapped}
    if source == "notion":
        projected = {"filter_clauses": deepcopy(mapped)}
        if "customer" in fields:
            projected["customer_aliases"] = list(constraints.get("customer_aliases") or [])
    else:
        projected = {**empty_query_constraints(), "filter_clauses": deepcopy(mapped)}
        for group, field in ((_CUSTOMER_KEYS, "customer"), (_PLATFORM_KEYS, "platform")):
            if field in fields:
                projected.update({key: deepcopy(constraints[key]) for key in group if key in constraints})
    return SourceConditions(source, projected, applied, tuple(dict.fromkeys(unapplied)), False)
