"""매체 미지정 원문의 본문 언급을 플랫폼 조건으로 확정하지 않는다."""
import unittest

from backend.intent.matching import chroma_text_matches_constraints
from backend.sources.mantis import issue_matches_constraints, issue_matches_platform, select_mantis_rows


class PlatformFieldGroundingTests(unittest.TestCase):
    def raw(self, media=None):
        return {"id": 29242, "summary": "MTS 관련 공용 작업", "description": "HTS와 MTS 작업 대상",
                "project": {"name": "900DRFN(공용)"},
                "custom_fields": [] if media is None else [{"field": {"name": "매체구분"}, "value": media}]}

    def test_unknown_media_does_not_match_title_body_or_supplied_search_text(self):
        for media in (None, "", "기타/미상"):
            with self.subTest(media=media):
                self.assertFalse(issue_matches_platform(self.raw(media), ["MTS"], search_text="MTS"))

    def test_unknown_chroma_media_does_not_match_body_mentions(self):
        for header in ("", "- 매체구분: \n", "- 매체구분: 기타/미상\n"):
            with self.subTest(header=header):
                self.assertFalse(chroma_text_matches_constraints(
                    f"- 이슈 ID: #29242\n{header}[제목]\nMTS 공용 작업\n[본문]\nHTS와 MTS 작업 대상",
                    {"platform_aliases": ["MTS"]}))

    def test_explicit_platform_field_wins_over_other_platform_mentions(self):
        self.assertTrue(issue_matches_platform(self.raw("MTS"), ["MTS"]))
        self.assertFalse(issue_matches_platform(self.raw("HTS"), ["MTS"]))
        self.assertFalse(chroma_text_matches_constraints(
            "- 이슈 ID: #29242\n- 매체구분: HTS\n[제목]\nMTS에서 됩니다",
            {"platform_aliases": ["MTS"]}))

    def test_unfiltered_search_keeps_unspecified_platform_issues(self):
        self.assertTrue(issue_matches_platform(self.raw(), []))
        self.assertTrue(chroma_text_matches_constraints("- 이슈 ID: #29242\n[제목]\nMTS 공용 작업", {}))

    def test_platform_list_counts_only_source_field_matches(self):
        unknown = self.raw()
        specified = {**self.raw("MTS"), "id": 29243}
        self.assertEqual([row["id"] for row in select_mantis_rows(
            [unknown, specified], {"platform_aliases": ["MTS"]})], [29243])

    def test_platform_clause_without_aliases_cannot_infer_an_unspecified_media(self):
        self.assertFalse(issue_matches_constraints(self.raw(), {
            "filter_clauses": [{"field": "platform", "operator": "equals", "value": "mts"}]}))

    def test_excluded_platform_keeps_unspecified_media_consistently(self):
        constraints = {"excluded_platform_aliases": ["MTS"],
                       "filter_clauses": [{"field": "platform", "operator": "not_equals", "value": "mts"}]}
        self.assertTrue(issue_matches_constraints(self.raw(), constraints))
        self.assertTrue(chroma_text_matches_constraints(
            "- 이슈 ID: #29242\n- 매체구분: 기타/미상\n[제목]\nMTS 공용 작업", constraints))
