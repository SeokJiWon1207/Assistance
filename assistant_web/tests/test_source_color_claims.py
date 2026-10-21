"""원문에서 확인한 색상 값만 색상 문맥에서 이슈 참조와 구분한다."""
import unittest
from backend.evaluation.issue_facts import extract_issue_claims


class SourceColorClaimTests(unittest.TestCase):
    source=('"color": "#8f897b", "color": "#385391"',)

    def test_source_bound_colors_do_not_become_issue_ids(self):
        ids,claims=extract_issue_claims('색상은 #8f897b 및 #385391입니다. (#91 · 노트 11)',source_quotes=self.source)
        self.assertEqual(ids,{91})
        self.assertEqual(claims,[])

    def test_unknown_colors_and_explicit_issue_references_still_require_sources(self):
        for text,wanted in [('색상 #385392입니다.',{385392}),('이슈 #385391 상태는 완료',{385391}),
                            ('#385391 수정 내역',{385391}),('색상 #385391, 참고 #92',{92})]:
            with self.subTest(text=text):
                self.assertEqual(extract_issue_claims(text,source_quotes=self.source)[0],wanted)
        self.assertIn(385391,extract_issue_claims('색상 #385391')[0])
