import unittest
from datetime import datetime, timedelta, timezone
from backend.suggestions.contracts import Candidate, EvidenceBatch, SourceRef
from backend.suggestions.ranking import build_suggestions


def candidate(source='mantis', identifier='12', facts=None, revision='v1'):
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    ref = SourceRef(type=source, id=identifier, title='같은 제목', url='https://example.test/' + identifier,
                    revision=revision, observedAt=now, validUntil=now + timedelta(minutes=10))
    return Candidate(id=source + identifier, sources=[ref], dedupeKey=source + ':' + identifier,
                     revision=revision, facts=facts or {}, priority=5, allowedActions=['review_progress', 'prepare_checklist'])


class SuggestionRankingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, tzinfo=timezone.utc)

    def test_explicit_link_creates_one_cross_source_task(self):
        m = candidate(facts={'notionIds': ['abc']})
        n = candidate('notion', 'abc')
        result = build_suggestions(EvidenceBatch(candidates=[m, n]), now=self.now)
        self.assertEqual(len(result), 1)
        self.assertEqual({s.type for s in result[0].sources}, {'mantis', 'notion'})
        self.assertIn('#12', result[0].promptPreview)
        self.assertIn('https://example.test/abc', result[0].promptPreview)
        self.assertIn('최신', result[0].promptPreview)

    def test_similar_title_is_not_identity(self):
        result = build_suggestions(EvidenceBatch(candidates=[candidate(), candidate('notion', 'abc')]), now=self.now)
        self.assertEqual(len(result), 2)

    def test_missing_evidence_does_not_assert_unverified_work(self):
        c = candidate('notion', 'abc', {'passages': [{'text': '개발 완료. 고객 검증 예정'}]})
        item = build_suggestions(EvidenceBatch(candidates=[c]), now=self.now)[0]
        self.assertNotIn('검증하지 않았', item.title)
        self.assertNotIn('검증 완료', item.title)

    def test_limit_and_same_work_dedup(self):
        candidates = [candidate(identifier=str(i)) for i in range(6)]
        result = build_suggestions(EvidenceBatch(candidates=candidates + candidates), now=self.now)
        self.assertEqual(len(result), 3)
        self.assertEqual(len({i.dedupeKey for i in result}), 3)

    def test_verified_followup_can_offer_checklist_without_claiming_failure(self):
        c = candidate('notion', 'a'*32, {'passages': [{'text': '개발 완료. 고객 검증 예정'}]})
        c.sources[0].grade = 'body'
        item = build_suggestions(EvidenceBatch(candidates=[c]), now=self.now)[0]
        self.assertEqual(item.kind, 'prepare_checklist')
        self.assertNotIn('실패', item.title)

    def test_partial_linked_document_only_allows_comparison(self):
        from backend.suggestions.ranking import ranked_candidates
        m = candidate(facts={'notionIds': ['abc']})
        n = candidate('notion', 'abc')
        merged = ranked_candidates(EvidenceBatch(candidates=[m, n]))[0]
        self.assertEqual(merged.allowedActions, ['compare_records'])

    def test_mantis_title_includes_short_single_line_summary_and_stable_particle(self):
        m = candidate(identifier='1231', facts={'notionIds': ['abc']})
        m.sources[0].title = '결제 화면\n 오류 ' + '상세 내용 ' * 30
        item = build_suggestions(EvidenceBatch(candidates=[m, candidate('notion', 'abc')]), now=self.now)[0]
        self.assertIn('결제 화면 오류', item.title)
        self.assertIn('#1231 이슈와', item.title)
        self.assertNotIn('\n', item.title)
        self.assertLess(len(item.title), 120)
        self.assertEqual(item.sources[0].title, m.sources[0].title)
        self.assertIn('Mantis #1231', item.promptPreview)

    def test_notion_title_is_shortened_like_mantis_without_changing_source(self):
        n = candidate('notion', 'abc')
        n.sources[0].title = '운영\n 점검 ' + '긴 문서 제목 ' * 40
        item = build_suggestions(EvidenceBatch(candidates=[n]), now=self.now)[0]
        self.assertIn('「운영 점검', item.title)
        self.assertIn('…」', item.title)
        self.assertNotIn('\n', item.title)
        self.assertLess(len(item.title), 100)
        self.assertEqual(item.sources[0].title, n.sources[0].title)
