import unittest
from datetime import date

from backend.intent.matching import chroma_text_matches_constraints
from backend.query_planning import extract_relative_created_at_range, plan_created_at_matches
from backend.intent.search_terms import build_keyword_terms
from backend.services.chat_service import (
    apply_authenticated_mantis_scope,
    should_scope_to_authenticated_mantis_user,
)
from backend.understanding import extract_query_constraints

USER = {"mantisUsername": "석지원"}


class PersonalScopeDetectionTest(unittest.TestCase):
    def test_detects_speaker_as_subject(self):
        self.assertTrue(should_scope_to_authenticated_mantis_user("내가 지금 처리해야 하는 이슈만 보여줘"))
        self.assertTrue(should_scope_to_authenticated_mantis_user("제가 맡은 건"))
        self.assertTrue(should_scope_to_authenticated_mantis_user("내 담당 미종료 이슈"))
        self.assertTrue(should_scope_to_authenticated_mantis_user("나 안끝난거 이슈 알려줘"))

    def test_detects_speaker_as_assignee(self):
        self.assertTrue(should_scope_to_authenticated_mantis_user("나한테 배정됐는데 한 달 넘은 건만 볼래"))
        self.assertTrue(should_scope_to_authenticated_mantis_user("저한테 할당된 이슈"))

    def test_named_person_overrides_personal_scope(self):
        # "내 이슈 말고 우동헌 담당" 처럼 다른 사람을 지목하면 개인 범위가 아니다.
        self.assertFalse(should_scope_to_authenticated_mantis_user("내 이슈 말고 우동헌 담당 건 보여줘"))

    def test_group_scope_is_not_personal(self):
        self.assertFalse(should_scope_to_authenticated_mantis_user("우리 팀 이슈"))
        self.assertFalse(should_scope_to_authenticated_mantis_user("신한증권 이슈 보여줘"))


class FirstPersonTokenTest(unittest.TestCase):
    def test_pronouns_are_not_treated_as_handler_names(self):
        # "나한테"가 이름으로 잡히면 없는 담당자로 검색해 0건이 된다.
        self.assertEqual(extract_query_constraints("나한테 배정됐는데 한 달 넘은 건")["people"]["names"], [])
        self.assertEqual(extract_query_constraints("저한테 할당된 이슈")["people"]["names"], [])

    def test_real_names_still_extracted(self):
        self.assertEqual(
            extract_query_constraints("우동헌한테 배정된 이슈")["people"]["names"], ["우동헌"]
        )


class ActionableStatusTest(unittest.TestCase):
    def test_todo_wordings_mean_not_closed(self):
        self.assertEqual(extract_query_constraints("지금 처리해야 하는 이슈")["statuses"], ["open"])
        self.assertEqual(extract_query_constraints("대응해야 할 건")["statuses"], ["open"])
        self.assertEqual(extract_query_constraints("나 안끝난거 이슈 알려줘")["statuses"], ["open"])

    def test_contrastive_assignment_means_not_closed(self):
        # "배정됐는데"는 아직 안 끝난 일을 뜻한다.
        self.assertEqual(
            extract_query_constraints("나한테 배정됐는데 한 달 넘은 건")["statuses"], ["open"]
        )

    def test_plain_assignment_keeps_specific_status(self):
        # 역접이 없으면 기존대로 구체 상태를 유지한다.
        self.assertEqual(extract_query_constraints("할당된 이슈 보여줘")["statuses"], ["assigned"])
        self.assertEqual(extract_query_constraints("종료된 이슈")["statuses"], ["closed"])


class AgingDateRangeTest(unittest.TestCase):
    TODAY = date(2026, 8, 18)

    def test_parses_aging_expressions(self):
        self.assertEqual(
            extract_relative_created_at_range("한 달 넘은 건", self.TODAY),
            {"from": "", "to": "2026-07-18", "precision": "older_than"},
        )
        self.assertEqual(
            extract_relative_created_at_range("30일 지난 이슈", self.TODAY)["to"], "2026-07-19"
        )
        self.assertEqual(
            extract_relative_created_at_range("3개월 이상 된 건", self.TODAY)["to"], "2026-05-18"
        )
        self.assertEqual(
            extract_relative_created_at_range("1년 넘은 이슈", self.TODAY)["to"], "2025-08-18"
        )

    def test_recent_expressions_are_unaffected(self):
        recent = extract_relative_created_at_range("최근 7일 이슈", self.TODAY)
        self.assertEqual(recent["precision"], "recent_days")
        self.assertEqual(recent["from"], "2026-08-12")

    def test_open_ended_range_matches_only_older_issues(self):
        aging = extract_relative_created_at_range("한 달 넘은 건", self.TODAY)
        self.assertFalse(plan_created_at_matches("2026-08-01", aging))
        self.assertTrue(plan_created_at_matches("2026-07-18", aging))
        self.assertTrue(plan_created_at_matches("2011-01-01", aging))

    def test_closed_range_still_bounded_on_both_sides(self):
        window = {"from": "2026-01-01", "to": "2026-01-31"}
        self.assertFalse(plan_created_at_matches("2025-12-31", window))
        self.assertTrue(plan_created_at_matches("2026-01-15", window))
        self.assertFalse(plan_created_at_matches("2026-02-01", window))


class PersonalQuestionEndToEndTest(unittest.TestCase):
    def document(self, *, created_at: str, handler: str, status: str) -> str:
        return f"""[Mantis 이슈 정보]
- 이슈 ID: #1
- 프로젝트(고객사): 132SHGM(신한굿모닝증권)
- 매체구분: MTS
- 화면번호: 9413
- 작업주체: 개발
- 현재 상태: {status}
- 담당자: {handler}
- 요청자: 김요청
- 등록일: {created_at}
- 최종수정일: {created_at}

[제목]
테스트"""

    def constraints_for(self, question: str):
        return extract_query_constraints(apply_authenticated_mantis_scope(question, USER))

    def test_todo_question_keeps_only_my_open_issues(self):
        cons = self.constraints_for("내가 지금 처리해야 하는 이슈만 보여줘")
        self.assertTrue(
            chroma_text_matches_constraints(
                self.document(created_at="2026-08-01", handler="석지원", status="확인된 이슈"), cons
            )
        )
        # 종료된 건과 남의 건은 빠진다.
        self.assertFalse(
            chroma_text_matches_constraints(
                self.document(created_at="2026-08-01", handler="석지원", status="종료된 이슈"), cons
            )
        )
        self.assertFalse(
            chroma_text_matches_constraints(
                self.document(created_at="2026-08-01", handler="우동헌", status="확인된 이슈"), cons
            )
        )

    def test_colloquial_my_unfinished_question_applies_owner_without_keywords(self):
        question = "나 안끝난거 이슈 알려줘"
        scoped = apply_authenticated_mantis_scope(question, USER)
        cons = extract_query_constraints(scoped)

        self.assertEqual(cons["people"]["names"], ["석지원"])
        self.assertEqual(cons["statuses"], ["open"])
        self.assertEqual(build_keyword_terms(scoped, cons), [])

    def test_other_person_question_excludes_me(self):
        cons = self.constraints_for("내 이슈 말고 우동헌 담당 건 보여줘")
        self.assertEqual(cons["people"]["names"], ["우동헌"])
        self.assertTrue(
            chroma_text_matches_constraints(
                self.document(created_at="2026-08-01", handler="우동헌", status="확인된 이슈"), cons
            )
        )
        self.assertFalse(
            chroma_text_matches_constraints(
                self.document(created_at="2026-08-01", handler="석지원", status="확인된 이슈"), cons
            )
        )


if __name__ == "__main__":
    unittest.main()
