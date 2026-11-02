"""명시 조건과 원천에 따라 팀 기본 범위를 조합한다."""
import re
from backend.domain.team import TEAM_MEMBERS, TEAM_SCOPE_LABEL


def apply_default_team_scope(text: str, constraints: dict, source: str) -> dict:
    """일반 Mantis 조회에 팀 기본값을 적용하되 명시한 조회 대상은 유지한다."""
    from backend.intent.scope_changes import has_team_scope_edit
    # 인용된 제목/키워드는 검색 대상이며 담당자 범위를 해제하는 지시가 아니다.
    scope_text = re.sub(r'"[^"]*"|\'[^\']*\'|“[^”]*”|‘[^’]*’', " ", text)
    all_teams = bool(re.search(r"전사|전체\s*(?:팀|회사)|팀\s*(?:제한|범위).*해제", scope_text)
                     or has_team_scope_edit(scope_text))
    if all_teams:
        # 원문에 팀 표현이 남아 있어도 명시한 전사 조회/범위 해제가 우선한다.
        constraints.pop("mantis_team_members", None)
        constraints.pop("mantis_team_label", None)
        constraints["mantis_team_scope"] = "all"
    explicit_target = bool(
        constraints.get("people", {}).get("names") or constraints.get("issue_ids")
        or constraints.get("unassigned_people")
        or all_teams
        or re.search(r"(?:내|제)\s*(?:이슈|업무|미종료|할\s*일|담당)|(?:나|저)(?:는|의|에게|한테)\s", text)
    )
    explicit_target |= constraints.get("mantis_team_scope") == "all"
    if constraints.get("mantis_team_scope") == "default" and (source not in {"mantis", "both"} or explicit_target):
        for key in ("mantis_team_members", "mantis_team_label", "mantis_team_scope"):
            constraints.pop(key, None)
    if explicit_target and not constraints.get("mantis_team_members"):
        constraints["mantis_team_scope"] = "all" if all_teams or constraints.get("mantis_team_scope") == "all" else "target"
    if source in {"mantis", "both"} and not explicit_target and not constraints.get("mantis_team_members"):
        constraints.update(mantis_team_members=list(TEAM_MEMBERS), mantis_team_label=TEAM_SCOPE_LABEL,
                           mantis_team_scope="default")
    return constraints
