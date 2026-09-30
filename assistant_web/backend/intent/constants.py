"""질의 이해 모듈이 공유하는 키워드와 정규식 상수."""

import re

from backend.common import normalize_for_match


SIMPLE_GREETING_TERMS = {
    "hi",
    "hello",
    "하이",
    "헬로",
    "안녕",
    "안녕하세요",
    "반가워",
    "반가워요",
}

RETRIEVAL_INTENT_KEYWORDS = {
    "이슈",
    "건",
    "접수",
    "요청",
    "문의",
    "오류",
    "장애",
    "버그",
    "수정",
    "개선",
    "확인",
    "담당",
    "담당자",
    "고객사",
    "화면",
    "화면번호",
    "차트",
    "mantis",
    "맨티스",
    "멘티스",
    "chroma",
    "chromadb",
    "vector",
    "벡터",
    "notion",
    "노션",
    "주요이슈",
    "주요 이슈",
    "결재요청",
    "결재 요청",
    "팀내 공지",
    "공지",
}

NOTION_INTENT_KEYWORDS = {
    "notion",
    "노션",
    "노선",
    "주요이슈",
    "주요 이슈",
    "결재요청",
    "결재 요청",
    "결재완료",
    "등록됨",
    "상태별",
    "타입별",
    "팀내 공지",
    "공지",
    "휴가",
    "회식",
    "송년회",
    "신년회",
    "팀빌딩",
    "일정",
    "기한",
    # Notion 은 문서 저장소다. "문서/자료/양식" 같은 대표 어휘가 트리거에 없어
    # "신입사원 관련 문서 찾아줘"가 Notion 검색을 아예 타지 못했다.
    # (아래 NOTION_QUERY_STOPWORDS 에도 있지만 그쪽은 '검색어에서 제외'하는
    #  목록이고, 이쪽은 '어느 소스를 볼지' 판단하는 목록이라 역할이 다르다.)
    "문서",
    "자료",
    "양식",
    "템플릿",
    "가이드",
    "매뉴얼",
    "규정",
    "지침",
    "회의록",
    "교육",
    "온보딩",
    "신입사원",
    "인수인계",
}

SOURCE_NAME_ALIASES = {
    normalize_for_match(name)
    for name in ("mantis", "맨티스", "맨티스에서", "notion", "노션", "모바일", "mobile")
}

NOTION_QUERY_STOPWORDS = {
    "그중", "이중",
    "notion",
    "노션",
    "노선",
    "주요이슈",
    "주요 이슈",
    "주요이슈현황",
    "이슈리스트",
    "테스트",
    "상태별",
    "타입별",
    "팀내 공지",
    "공지",
    "항목",
    "문서",
    "자료",
    "기록",
    "페이지",
    "건은",
    "몇개야",
    "몇건이야",
    "된",
    "상태",
    "상태인",
    # 존재/의문 어미가 검색어로 남으면 실제 문서 본문/제목에 거의 등장하지 않아
    # AND 매칭이 항상 실패한다. "메모리 릭 있어?" 같은 자연스러운 질문이
    # 매칭되도록 필러성 어미를 제거한다.
    "있어",
    "있어요",
    "있나요",
    "있나",
    "있는지",
    "없어",
    "없나요",
    "없나",
}

GENERAL_INTENT_KEYWORDS = {
    "모델",
    "누구",
    "너는",
    "넌",
    "사용법",
    "도움말",
    "안녕",
    "뭐할수",
    "뭐 할 수",
    "기능",
}

EXPLICIT_NOTION_MARKERS = ("notion", "노션", "노선")

EXPLICIT_MANTIS_MARKERS = ("mantis", "멘티스", "맨티스")

COMBINED_SOURCE_MARKERS = (
    "둘다",
    "둘 다",
    "두소스",
    "두 소스",
    "양쪽",
    "함께",
    "통합",
    "교차",
    "비교",
)

MANTIS_DOMAIN_MARKERS = (
    "고객사",
    "프로젝트",
    "화면번호",
    "hts",
    "mts",
    "장애",
    "오류",
    "버그",
    "제도개선",
    "거래시간",
    "공수",
    "개발완료",
    "qc완료",
)

NOTION_DOMAIN_MARKERS = (
    "공지",
    "회식",
    "휴가",
    "송년회",
    "신년회",
    "팀빌딩",
    "결재요청",
    "결재완료",
    "주요이슈현황",
    "업무공유",
)

# 소스를 잠글 수 있는 대상 객체 표현이다. 고객사·플랫폼·담당자·날짜·상태는
# 검색 범위이지 소스 근거가 아니므로 이 목록에 넣지 않는다.
NOTION_TARGET_OBJECT_MARKERS = (
    "회의록",
    "온보딩",
    "인수인계",
    "규정",
    "지침",
    "양식",
    "템플릿",
    "매뉴얼",
    "가이드",
    "결재요청",
    "결재완료",
    "팀내 공지",
    "공지",
    "휴가",
    "회식",
    "송년회",
    "신년회",
    "팀빌딩",
    "주요이슈현황",
    "주요 이슈 현황",
    "업무공유",
    "접속 방법",
    "사용 방법",
    "조직도",
    "정기보고서",
    # '주간보고'는 Mantis 집계를 요구하는 '주간 보고서'와 겹쳐 넣지 않는다.
    "주간업무",
    "세미나",
)

MANTIS_TARGET_OBJECT_MARKERS = (
    "공수",
    "고객사 프로필",
    "월간 고객사 리포트",
    "개발완료",
    "개발 완료",
    "qc완료",
    "qc 완료",
)

SHARED_TOPIC_MARKERS = (
    "문서",
    "자료",
    "일정",
    "기한",
    "교육",
    "프로젝트",
    "장애",
    "오류",
    "버그",
    "이슈",
)

TRIAGE_REQUEST_PATTERN = re.compile(r"초동\s*확인.*(?:영향\s*범위|담당자\s*전달)|영향\s*범위.*초동\s*확인")

ISSUE_LIST_INTENT_KEYWORDS = {
    "최근",
    "최신",
    "목록",
    "리스트",
    "보여줘",
    "알려줘",
    "뽑아줘",
    "찾아줘",
    "조회",
    "검색",
    "이슈",
    "건",
    "접수",
    "문의",
    "요청",
}

ISSUE_ANALYSIS_INTENT_KEYWORDS = {
    "분석",
    "흐름",
    "추이",
    "경향",
    "패턴",
    "원인",
    "이유",
    "리스크",
    "영향",
    "정리해줘",
    "요약해줘",
    "비교",
    "분류",
}

CUSTOMER_LIKE_PATTERN = re.compile(r"[가-힣A-Za-z0-9]{2,20}?(?:투자증권|증권|금융투자|금융)")

GENERIC_CUSTOMER_WORDS = {"증권", "증권사", "금융", "금융사", "금융투자", "투자증권", "고객사"}

FOLLOW_UP_STRONG_MARKERS = (
    "그중", "그 중", "이중", "이 중", "그거", "그것", "그건", "그때", "그 이슈", "그 건",
    "해당", "위에", "위 이슈", "아까", "방금",
    # 직전에 보던 대상을 가리키는 지시 표현. 없으면 "이 이슈의 처리 이력"처럼
    # 대상이 분명한 후속 질문이 독립 질문으로 취급돼 대상을 잃는다.
    "이 이슈", "이 건", "이 항목", "본 이슈", "이번 이슈", "이 페이지", "이 문서",
    "첫번째", "첫 번째", "두번째", "두 번째", "세번째", "세 번째", "마지막",
)

FOLLOW_UP_WEAK_MARKERS = (
    "자세히", "자세하게", "추가로", "담당자", "상태",
    "진행", "언제", "왜", "어떻게", "무슨", "몇건", "몇 건",
    "빼줘", "빼고", "제외", "말고", "바꿔", "변경", "개만", "건만", "최근",
)

FOLLOW_UP_WEAK_MAX_LEN = 20

FOLLOW_UP_CONTINUATION_MARKERS = (
    "요약", "한 문장으로", "한줄로", "간단히 정리", "정리해줘", "다시 설명", "풀어서",
    # 자체 검색 대상을 추가하지 않고 직전 결과의 표시 방식만 바꾸는 요청.
    # is_follow_up_question()이 대상 조건 유무를 함께 확인하므로
    # "KB증권 이슈를 최신순으로" 같은 독립 질문은 후속으로 오인하지 않는다.
    "정렬", "오래된 순", "최신순", "최근순", "등록순", "더 보여", "계속 보여",
)

KOREAN_ORDINAL_WORDS = {
    "첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}

DEMONSTRATIVE_ISSUE_PATTERN = re.compile(
    r"(?:이|그|해당|위|본|동)\s*(?:이슈|건|항목|페이지|문서)"
    r"|이번\s*이슈|방금\s*(?:이슈|건|본)|아까\s*(?:이슈|건|본)"
)


__all__ = [
    "SIMPLE_GREETING_TERMS",
    "RETRIEVAL_INTENT_KEYWORDS",
    "NOTION_INTENT_KEYWORDS",
    "SOURCE_NAME_ALIASES",
    "NOTION_QUERY_STOPWORDS",
    "GENERAL_INTENT_KEYWORDS",
    "EXPLICIT_NOTION_MARKERS",
    "EXPLICIT_MANTIS_MARKERS",
    "COMBINED_SOURCE_MARKERS",
    "MANTIS_DOMAIN_MARKERS",
    "NOTION_DOMAIN_MARKERS",
    "NOTION_TARGET_OBJECT_MARKERS",
    "MANTIS_TARGET_OBJECT_MARKERS",
    "SHARED_TOPIC_MARKERS",
    "ISSUE_LIST_INTENT_KEYWORDS",
    "ISSUE_ANALYSIS_INTENT_KEYWORDS",
    "CUSTOMER_LIKE_PATTERN",
    "GENERIC_CUSTOMER_WORDS",
    "FOLLOW_UP_STRONG_MARKERS",
    "FOLLOW_UP_WEAK_MARKERS",
    "FOLLOW_UP_WEAK_MAX_LEN",
    "FOLLOW_UP_CONTINUATION_MARKERS",
    "KOREAN_ORDINAL_WORDS",
    "DEMONSTRATIVE_ISSUE_PATTERN",
]
