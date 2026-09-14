"""Notion 검색·쓰기 모듈이 공유하는 상수."""

# 관형 조사 '인'과 달리 끝 글자까지 검색해야 하는 명사만 보존한다.
NOTION_IN_ENDING_NOUNS = frozenset({"로그인", "디자인", "온라인", "승인", "원인"})

NOTION_STATUS_QUERY_TERMS = {
    "시작전",
    "미완료",
    "대기",
    "등록됨",
    "보관됨",
    "결재요청",
    "결재완료",
    "결재완료(팀장)",
    "완료",
    "진행중",
    "종료된이슈",
    "할당된이슈",
}

NOTION_STATUS_TYPOS = {
    "결제요청": "결재요청",
    "결제완료": "결재완료",
    "결제완료(팀장)": "결재완료(팀장)",
}

NOISY_PROPERTY_NAME_KEYWORDS = (
    "휴가표시",
    "vacation",
    "주차",
    "금주",
    "차주",
    "경과",
    "오늘",
    "생성",
    "최종",
    "편집",
    "on/off",
    "id",
    "uuid",
)

NOISY_PROPERTY_TYPES = frozenset(
    {"formula", "title", "created_time", "last_edited_time", "created_by", "last_edited_by"}
)

NOTION_BODY_SCAN_MAX_PER_DB = 30

NOTION_WRITE_CONFIRM_HEADER_CREATE = "📝 다음 내용으로 Notion에 새 항목을 만들까요?"

NOTION_WRITE_CONFIRM_HEADER_UPDATE = "📝 다음 내용으로 기존 Notion 항목을 수정할까요?"

NOTION_WRITE_CONFIRM_PHRASES = (
    "네", "예", "응", "ㅇㅇ", "진행해줘", "진행해", "실행해줘", "실행해",
    "그렇게 해줘", "확인", "맞아", "승인", "좋아", "진행",
)

NOTION_WRITE_CANCEL_PHRASES = (
    "아니", "아니오", "아니요", "취소", "하지마", "하지 마", "그만", "취소해줘", "취소할래",
)

NOTION_WRITE_CONFIRM_MAX_LEN = 12

NOTION_WRITE_FIELD_LABELS = {"status": "상태", "owner": "담당자", "dueDate": "기한", "memo": "메모"}


__all__ = [
    "NOTION_IN_ENDING_NOUNS",
    "NOTION_STATUS_QUERY_TERMS",
    "NOTION_STATUS_TYPOS",
    "NOISY_PROPERTY_NAME_KEYWORDS",
    "NOISY_PROPERTY_TYPES",
    "NOTION_BODY_SCAN_MAX_PER_DB",
    "NOTION_WRITE_CONFIRM_HEADER_CREATE",
    "NOTION_WRITE_CONFIRM_HEADER_UPDATE",
    "NOTION_WRITE_CONFIRM_PHRASES",
    "NOTION_WRITE_CANCEL_PHRASES",
    "NOTION_WRITE_CONFIRM_MAX_LEN",
    "NOTION_WRITE_FIELD_LABELS",
]
