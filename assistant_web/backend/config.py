"""경로·상수·연결 설정 모음.

다른 backend 모듈을 참조하지 않는 최하위 계층으로, 경로와 외부 연결 설정을 제공한다.
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
APP_DIR = BASE_DIR.parent
WORKSPACE_DIR = APP_DIR.parent
CONFIG_DIR = APP_DIR / "config"
RUNTIME_DIR = Path(os.getenv("ASSISTANT_RUNTIME_DIR", str(APP_DIR / "runtime"))).expanduser()
RUNTIME_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
try:
    RUNTIME_DIR.chmod(0o700)
except OSError:
    pass
CHROMA_DB_PATH = Path(
    os.getenv(
        "CHROMA_DB_PATH",
        str(WORKSPACE_DIR / "data" / "mantis_chroma_db" / "chroma.sqlite3"),
    )
).expanduser()
CHROMA_PERSIST_DIR = CHROMA_DB_PATH.parent
MANTIS_INDEX_DB_PATH = Path(
    os.getenv(
        "MANTIS_INDEX_DB_PATH",
        str(CHROMA_PERSIST_DIR / "mantis_index.sqlite3"),
    )
).expanduser()
AUDIT_DB_PATH = Path(
    os.getenv("AUDIT_DB_PATH", str(RUNTIME_DIR / "assistant_audit.sqlite3"))
).expanduser()
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_DEFAULT_VERSION = "2022-06-28"


STOPWORDS = {
    "mantis",
    "맨티스",
    "멘티스",
    "notion",
    "노션",
    "노선",
    "주요",
    "주요이슈",
    "주요 이슈",
    "최근",
    "최신",
    "최신순",
    "최근순",
    "이슈",
    "이슈가",
    "이슈들",
    "이슈리스트",
    "목록",
    "조회",
    "검색",
    "요약",
    "정리",
    "뽑아",
    "뽑아줘",
    "보여줘",
    "알려줘",
    "해줘",
    "달라",
    "비슷한",
    "유사한",
    "과거",
    "사례",
    "찾아",
    "찾아줘",
    "찾아줄래",
    "찾아줘볼래",
    "볼래",
    "줘",
    "그럼",
    "개",
    "건",
    "가장",
    "뭐야",
    "뭐",
    "무엇",
    "어떤",
    "관련",
    "관련된",
    "있는",
    "에서",
    "에게",
    "으로",
    "으로부터",
    "대한",
    "대해",
    "알려",
    "확인",
    "확인해줘",
    "연락",
    "연락온",
    "들어온",
    "들어온거",
    "들어온건",
    "접수",
    "접수된",
    "접수건",
    "요청",
    "요청한",
    "요청한거",
    "요청건",
    "문의",
    "문의한",
    "문의건",
    "담당",
    "담당자",
    "건들",
    "건이",
    "사항",
    "항목",
    "내역",
    "리스트",
    "목록으로",
    "현재",
    "기준",
    "기준으로",
    "몇",
    "몇개",
    "몇건",
    "몇 개",
    "몇 건",
    "다",
    "모두",
    "전부",
    "전체",
    "포함",
    "포함해서",
    "개야",
    "건이야",
    "건수",
    "진행",
    "중인",
    "상태",
    "상태인",
    "만",
    "지난",
    "이번",
    "상세",
    "자세히",
    "내용",
    "화면",
    "화면번호",
    "종료",
    "종료된",
    "확인된",
    "할당된",
    "해결된",
    "검토",
    "오픈",
    "선임",
    "차장",
    "과장",
    "대리",
    "주임",
    "수석",
    "매니저",
    "팀장",
    "부장",
    "사원",
}

# 신규 고객사/별칭은 코드 수정 없이 assistant_web/config/customer_aliases.json 에 추가한다.
# 파일이 없거나 형식이 잘못됐을 때만 아래 내장 기본값을 사용한다(하위 호환).
DEFAULT_CUSTOMER_ALIASES = {
    "kb": ["KB증권", "KBStar", "112KBStar", "KB투자증권", "케이비증권", "케이비"],
    "신한": ["신한투자증권", "신한투자", "신한증권", "신한굿모닝증권", "굿모닝신한증권", "132SHGM", "SHGM"],
    "하나": ["하나증권", "하나금융", "하나금융투자", "134Hanaw", "Hanaw"],
    "db": ["DB", "DB증권", "DB금융투자", "동부증권", "동부", "101PopcornRenewal", "PopcornRenewal", "디비증권", "디비"],
    "유안타": ["유안타증권", "유안타"],
    "메리츠": ["Meritz", "메리츠", "138Meritz"],
    "한투": ["한투증권", "한국투자증권", "117Hantu", "Hantu"],
    "현대차": ["현대차증권", "HMC증권", "113HMC", "HMC", "에이치엠씨"],
    "다올": ["다올투자증권", "KTB", "115다올투자증권", "케이티비"],
}


def load_customer_aliases() -> Dict[str, List[str]]:
    aliases_path = CONFIG_DIR / "customer_aliases.json"
    if not aliases_path.exists():
        return DEFAULT_CUSTOMER_ALIASES
    try:
        data = json.loads(aliases_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[warn] customer_aliases.json 로딩 실패, 내장 기본값 사용: {exc}", file=sys.stderr)
        return DEFAULT_CUSTOMER_ALIASES
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(values, list) and all(isinstance(v, str) for v in values)
        for key, values in data.items()
    ):
        print("[warn] customer_aliases.json 형식 오류(키:문자열, 값:문자열 배열), 내장 기본값 사용", file=sys.stderr)
        return DEFAULT_CUSTOMER_ALIASES
    return data


CUSTOMER_ALIASES = load_customer_aliases()

AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES = {
    "KRX",
}

PLATFORM_ALIASES = {
    "hts": ["HTS", "H-able", "Hable"],
    # 현업에서 MTS를 "모바일"로 부르는 경우가 많다. 음차/영문 표기까지 함께 둔다.
    "mts": ["MTS", "엠티에스", "모바일", "mobile"],
}

STATUS_ALIASES = {
    "open": ["오픈", "열린", "미종료", "진행중", "진행 중", "처리중", "처리 중", "open", "opened", "new"],
    "closed": ["종료", "종료된", "닫힌", "완료", "closed", "close"],
    "confirmed": ["확인", "확인된", "confirmed", "confirm"],
    "assigned": ["할당", "할당된", "assigned", "assign"],
    "resolved": ["해결", "해결된", "resolved", "resolve"],
    "review": ["검토", "이슈 검토"],
    "need_info": ["정보 부족", "정보부족"],
    "dev_done": ["개발완료", "개발 완료", "developed"],
}

STATUS_MATCH_ALIASES = {
    # 스냅샷은 상태를 한글로, 라이브 API는 영문으로 준다. 한쪽만 두면
    # 스냅샷 결과를 라이브로 재검증할 때 열린 이슈가 전부 탈락한다.
    "open": [
        "할당된 이슈", "확인된 이슈", "이슈 검토", "정보 부족", "개발완료", "해결된 이슈",
        "assigned", "confirmed", "acknowledged", "feedback", "new", "resolved", "developed",
    ],
    "closed": ["종료된 이슈", "종료", "closed"],
    "confirmed": ["확인된 이슈", "확인", "confirmed"],
    "assigned": ["할당된 이슈", "할당", "assigned"],
    "resolved": ["해결된 이슈", "해결", "resolved"],
    "review": ["이슈 검토", "검토"],
    "need_info": ["정보 부족", "정보부족"],
    "dev_done": ["개발완료", "개발 완료", "developed"],
}

# 상태 별칭은 부분 문자열로 찾기 때문에 "종료 안 된"에서도 먼저 "종료"가
# 발견될 수 있다. 상태 별칭을 적용하기 전에 자주 쓰는 부정 표현을 별도로
# 판별해, closed의 반대 의미가 closed 필터로 뒤집히지 않게 한다.
NON_CLOSED_STATUS_PATTERNS = (
    re.compile(r"미(?:종료|완료)"),
    re.compile(r"(?:종료|완료|closed)(?:가|는|은|이|를)?안(?:된|됨|됐|되어|돼|되는)"),
    re.compile(r"(?:종료|완료)(?:가|는|은|이)?(?:되|하)?지않"),
    re.compile(r"닫히지않"),
    re.compile(r"(?:종료|완료|closed)(?:가|는|은|이)?(?:된|인)?(?:게|것이|상태가)?(?:아니|아닌|아님)"),
    re.compile(r"(?:종료|closed)(?:를|는|은)?제외"),
)

MANTIS_DATE_CUSTOM_FIELDS = {
    "고객사요청일",
    "고객사전달 목표일",
    "고객사전달일",
    "개발완료 목표일",
    "QC 완료 목표일",
    "버전반영일",
}

MANTIS_DATE_FIELD_ALIASES = {
    "고객사요청일": ["고객사요청일", "고객사 요청일"],
    "고객사전달 목표일": ["고객사전달 목표일", "고객사 전달 목표일"],
    "고객사전달일": ["고객사전달일", "고객사 전달일"],
    "개발완료 목표일": ["개발완료 목표일", "개발 완료 목표일"],
    "QC 완료 목표일": ["QC 완료 목표일", "QC완료 목표일", "qc 완료 목표일"],
    "버전반영일": ["버전반영일", "버전 반영일"],
}

PERSON_ROLE_ALIASES = {
    "handler": ["담당", "담당자", "처리자", "맡은", "배정", "배정된", "할당", "할당된"],
    "reporter": ["from", "수신", "발신", "요청자", "문의자", "고객사", "연락", "들어온", "접수", "요청한", "문의한", "한테", "에게"],
}

PERSON_BLOCKLIST = {
    "담당",
    "담당자",
    # 후속 질문의 서수/위치 표현이 사람 이름으로 오인되는 것을 막는다. (예: "첫 번째")
    "번째",
    "첫째",
    "첫번째",
    "두번째",
    "세번째",
    "마지막",
    # 지시대명사도 "그거 담당자 누구야?" 처럼 담당/할당 표현 앞에 오면 사람 이름으로
    # 오인될 수 있어 차단한다.
    "그거",
    "그것",
    "그건",
    "그중",
    "이거",
    "이것",
    "이중",
    "저거",
    "저것",
    "해당",
    "관련",
    "내용",
    # "파트너매니저"처럼 시스템/화면 이름이 "이름+직함"으로 잘못 쪼개져
    # known_people 캐시에 섞여 들어가는 걸 막는다.
    "파트너",
    "확인",
    "요청",
    "문의",
    "접수",
    # 출력 항목을 가리키는 말이 "제목과 담당자로 정리해줘"처럼 담당 표현 앞에
    # 올 때 사람 이름으로 오인되는 것을 막는다.
    "제목",
    "결과",
    "기한",
    "이슈",
    "고객",
    "고객사",
    "현재",
    "최근",
    "있어",
    "있는지",
    "무슨",
    "모델",
    "사용법",
    "도움말",
    "기능",
    "데이터",
    "데이터베이스",
    "노션",
    "notion",
    "정보",
    "리스트",
    "목록",
    "기준",
    "개발",
    "완료",
}


def get_mantis_settings() -> Dict[str, str]:
    mantis_url = os.getenv("MANTIS_API_URL", "").strip()
    mantis_token = os.getenv("MANTIS_API_TOKEN", "").strip()
    if not mantis_url:
        raise HTTPException(status_code=503, detail="Mantis API 주소가 비어 있습니다. .env의 MANTIS_API_URL을 확인해 주세요.")
    if not mantis_token:
        raise HTTPException(status_code=503, detail="Mantis API 토큰이 비어 있습니다. .env의 MANTIS_API_TOKEN을 입력해 주세요.")
    return {"url": mantis_url, "token": mantis_token}


def get_mantis_headers() -> Dict[str, str]:
    settings = get_mantis_settings()
    return {
        "Authorization": settings["token"],
        "Content-Type": "application/json",
    }


def parse_notion_database_ids() -> List[str]:
    raw_ids = (
        os.getenv("NOTION_DATABASE_IDS")
        or os.getenv("NOTION_DATABASE_ID")
        or os.getenv("NOTION_ISSUE_DB_ID")
        or ""
    )
    database_ids = [
        value.strip().replace("-", "")
        for value in re.split(r"[,\n]", raw_ids)
        if value.strip()
    ]
    return list(dict.fromkeys(database_ids))


def get_notion_settings() -> Dict[str, Any]:
    return {
        "token": os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_TOKEN") or "",
        "database_ids": parse_notion_database_ids(),
        "version": os.getenv("NOTION_API_VERSION") or NOTION_DEFAULT_VERSION,
    }


def get_notion_headers() -> Dict[str, str]:
    settings = get_notion_settings()
    return {
        "Authorization": f"Bearer {settings['token']}",
        "Notion-Version": settings["version"],
        "Content-Type": "application/json",
    }


def notion_post_json(url: str, payload: Dict[str, Any], timeout: int = 45, retries: int = 2) -> requests.Response:
    # A single Notion tool call must not monopolize the whole chat request.
    # Callers may request a larger timeout/retry count, but the orchestration
    # budget caps each network attempt and defaults to one short retry for
    # transient read/connect failures.
    max_timeout = max(3, min(int(os.getenv("NOTION_TOOL_TIMEOUT_SECONDS", "12")), 45))
    max_retries = max(0, min(int(os.getenv("NOTION_TOOL_MAX_RETRIES", "1")), 2))
    timeout = min(timeout, max_timeout)
    retries = min(retries, max_retries)
    last_error: Optional[requests.RequestException] = None
    for attempt in range(max(1, retries + 1)):
        try:
            return requests.post(
                url,
                headers=get_notion_headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(0.25 * (2**attempt), 1.0))
    if last_error:
        raise last_error
    raise requests.RequestException("Notion request failed")


def notion_is_configured() -> bool:
    settings = get_notion_settings()
    return bool(settings["token"] and settings["database_ids"])
