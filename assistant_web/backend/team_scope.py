"""팀 대시보드의 담당자 범위. 앱 가입과 Mantis 작업주체는 소속 기준이 아니다."""

import json
import re

from backend.common import normalize_for_match
from backend.config import CONFIG_DIR


_config = json.loads((CONFIG_DIR / "team_dashboard.json").read_text(encoding="utf-8"))
TEAM_SCOPE_LABEL = _config["label"]
TEAM_MEMBERS = tuple(_config["members"])
_member_keys = {normalize_for_match(name) for name in TEAM_MEMBERS}
if not TEAM_MEMBERS or "" in _member_keys or len(_member_keys) != len(TEAM_MEMBERS):
    raise ValueError("팀 대시보드 담당자 명단이 비어 있거나 중복되어 있습니다.")


_TEAM_PHRASE = re.compile(
    r"(?<![가-힣A-Za-z0-9])(?:우리\s*팀|저희\s*팀|내\s+팀|"
    r"(?:(?:우리|저희)\s*)?(?:컨텐츠\s*서비스\s*팀|콘텐츠\s*서비스\s*팀|CS\s*팀))"
    r"(?:원)?(?:의|에서|에|만|은|는)?",
    re.IGNORECASE,
)


def has_mantis_team_scope(text: str) -> bool:
    """대화의 팀 범위와 프로젝트·작업주체 필드를 구분한다."""
    if re.search(r"노션|노선|notion", text, re.IGNORECASE) and not re.search(r"멘티스|맨티스|mantis", text, re.IGNORECASE):
        return False
    if re.search(r"(?:작업주체|업무주체|프로젝트)\s*(?:가|는|이|=|:)?\s*(?:CS|컨텐츠서비스|콘텐츠서비스)\s*팀", text, re.IGNORECASE):
        return False
    return bool(_TEAM_PHRASE.search(text))


def remove_team_scope_phrase(text: str) -> str:
    return _TEAM_PHRASE.sub(" ", text)


def matches_team_constraint(handler: str, constraints: dict) -> bool:
    members = constraints.get("mantis_team_members")
    return not members or normalize_for_match(handler) in {normalize_for_match(name) for name in members}


def filter_team_issues(issues):
    """이름을 정확히 비교해 타 팀·공용·미지정 담당자를 제외한다."""
    return [issue for issue in issues if normalize_for_match(str(issue.get("handler") or "")) in _member_keys]


def team_member_profiles(users):
    """가입 계정이 없어도 집계 명단에 포함하며, 보조 계정은 중복하지 않는다."""
    profiles = {}
    for user in users:
        if not user.get("approved"):
            continue
        person = str(user.get("mantisUsername") or user.get("displayName") or "").strip()
        key = normalize_for_match(person)
        if key not in _member_keys:
            continue
        if key not in profiles or normalize_for_match(str(user.get("displayName") or "")) == key:
            profiles[key] = user
    return [
        {**profiles.get(normalize_for_match(name), {}), "displayName": name, "mantisUsername": name,
         "role": profiles.get(normalize_for_match(name), {}).get("role") or "member"}
        for name in TEAM_MEMBERS
    ]
