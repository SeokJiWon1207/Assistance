"""팀 범위의 기존 import 경로."""
from backend.domain.team import (
    _config as _config,
    TEAM_SCOPE_LABEL as TEAM_SCOPE_LABEL,
    TEAM_MEMBERS as TEAM_MEMBERS,
    _member_keys as _member_keys,
    _TEAM_PHRASE as _TEAM_PHRASE,
    has_mantis_team_scope as has_mantis_team_scope,
    remove_team_scope_phrase as remove_team_scope_phrase,
    matches_team_constraint as matches_team_constraint,
    filter_team_issues as filter_team_issues,
    team_member_profiles as team_member_profiles,
)
from backend.intent.team_scope import (
    apply_default_team_scope as apply_default_team_scope,
)
