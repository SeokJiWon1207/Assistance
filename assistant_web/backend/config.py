"""경로·상수·연결 설정 모음.

다른 backend 모듈을 참조하지 않는 최하위 계층으로, 경로와 외부 연결 설정을 제공한다.
"""

import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.execution import bounded_timeout, interruptible_sleep, execution_slot


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

# "이슈 하나만"처럼 표시 개수를 한글 수사로 말하는 질문이 흔하다. 숫자 표기만
# 개수로 읽으면 사이드바 기본값이 요청 개수를 덮어쓰고, 수사 자체가 Mantis
# 검색어로 남아 본문에 그 낱말이 들어간 엉뚱한 이슈가 근거로 올라간다.
KOREAN_COUNT_WORDS = {
    "하나": 1,
    "한": 1,
    "둘": 2,
    "두": 2,
    "셋": 3,
    "세": 3,
    "석": 3,
    "넷": 4,
    "네": 4,
    "넉": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
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
    # 삼성증권과 삼성선물 프로젝트가 공통 어간으로 하나의 고객사 그룹에
    # 합쳐지면 양쪽 이슈가 섞인다. 전체 프로젝트명은 유지하고 어간만 막는다.
    "Samsung",
    "삼성",
}

PLATFORM_ALIASES = {
    "hts": ["HTS", "H-able", "Hable"],
    # 현업에서 MTS를 "모바일"로 부르는 경우가 많다. 음차/영문 표기까지 함께 둔다.
    "mts": ["MTS", "엠티에스", "모바일", "mobile"],
}

STATUS_ALIASES = {
    "open": [
        "오픈", "열린", "미종료", "진행중", "진행 중", "처리중", "처리 중",
        "안끝난", "안 끝난", "끝나지 않은", "open", "opened", "new",
    ],
    "closed": ["종료", "종료된", "닫힌", "완료", "closed", "close"],
    "confirmed": ["확인", "확인된", "confirmed", "confirm"],
    "assigned": ["할당", "할당된", "assigned", "assign"],
    "resolved": ["해결", "해결된", "resolved", "resolve"],
    "review": ["검토", "이슈 검토", "acknowledged"],
    "need_info": ["정보 부족", "정보부족", "feedback"],
    "dev_done": ["개발완료", "개발 완료", "developed"],
}

MANTIS_STATUS_NAMES = {
    "new": ("new", "신규", "신규 이슈"),
    "feedback": ("feedback", "need_info", "정보 부족"),
    "acknowledged": ("acknowledged", "review", "이슈 검토", "검토"),
    "confirmed": ("confirmed", "확인", "확인된 이슈"),
    "assigned": ("assigned", "할당", "할당된 이슈"),
    "developed": ("developed", "dev_done", "개발완료", "개발 완료"),
    "resolved": ("resolved", "해결", "해결된 이슈", "해결 및 종료"),
    "closed": ("closed", "close", "종료", "종료된 이슈", "닫힌", "완료"),
}
MANTIS_OPEN_STATUSES = frozenset({"new", "feedback", "acknowledged", "confirmed", "assigned", "developed"})

STATUS_MATCH_ALIASES = {
    # 스냅샷은 상태를 한글로, 라이브 API는 영문으로 준다. 한쪽만 두면
    # 스냅샷 결과를 라이브로 재검증할 때 열린 이슈가 전부 탈락한다.
    "open": [alias for status, aliases in MANTIS_STATUS_NAMES.items() if status in MANTIS_OPEN_STATUSES for alias in aliases],
    "closed": ["종료된 이슈", "종료", "closed"],
    "confirmed": ["확인된 이슈", "확인", "confirmed"],
    "assigned": ["할당된 이슈", "할당", "assigned"],
    "resolved": ["해결된 이슈", "해결", "resolved"],
    "review": ["이슈 검토", "검토", "acknowledged"],
    "need_info": ["정보 부족", "정보부족", "feedback"],
    "dev_done": ["개발완료", "개발 완료", "developed"],
}

# 상태 별칭은 부분 문자열로 찾기 때문에 "종료 안 된"에서도 먼저 "종료"가
# 발견될 수 있다. 상태 별칭을 적용하기 전에 자주 쓰는 부정 표현을 별도로
# 판별해, closed의 반대 의미가 closed 필터로 뒤집히지 않게 한다.
PENDING_WORK_QUERY_PATTERN = re.compile(
    # '남은 시간/기간/공수'는 상태가 아니다. 잔여 업무를 가리키는 목적어만 받는다.
    r"(?:남(?:은|아\s*있(?:는|던)|았던)|잔여)\s*(?:이슈|티켓|업무|항목|할\s*일|건|거|것)"
    r"|(?:이슈|티켓|업무|항목)(?:가|는|이|은)?\s*(?:아직\s*)?남(?:아\s*있|았)"
    r"|(?<![가-힣])미\s*(?:처리|해결|완료|종료)"
    r"|(?:처리|해결|완료|종료)(?:가|는|이|은)?\s*(?:안|못)\s*(?:되는|된|됐|한|했|됨)"
    r"|(?:처리|해결|완료|종료)(?:되|하)?지\s*(?:않|못)(?:은|한|했|하)"
    r"|(?:안|못)\s*(?:처리|해결|완료|종료)\s*(?:된|한|했|됐)"
    r"|(?:(?:안|못)\s*끝(?:난|낸)|끝(?:나|내)지\s*(?:않|못)은)"
    r"|(?:안\s*닫힌|닫히지\s*않은)"
)

NON_CLOSED_STATUS_PATTERNS = (
    re.compile(r"(?<!이)미(?:종료|완료)"),  # '이미 완료/종료'는 미완료가 아니다.
    re.compile(r"(?:종료|완료|closed)(?:가|는|은|이|를)?안(?:된|됨|됐|되어|돼|되는)"),
    re.compile(r"(?:종료|완료)(?:가|는|은|이)?(?:되|하)?지않"),
    re.compile(r"닫히지않"),
    re.compile(r"(?:종료|완료|closed)(?:가|는|은|이)?(?:된|인)?(?:게|것이|상태가)?(?:아니|아닌|아님)"),
    re.compile(r"(?:종료|closed)(?:를|는|은)?제외"),
    # "지금 처리해야 하는", "처리할 이슈"처럼 앞으로 할 일을 묻는 표현.
    # 이미 끝난 건을 빼달라는 뜻이므로 미종료로 해석한다.
    re.compile(r"처리(?:해야|할|하아야)"),
    re.compile(r"(?:해결|대응)(?:해야|할)"),
    re.compile(r"남(?:은|아있는|아\s*있는)(?:일|업무|건|이슈)"),
    re.compile(r"잔여\s*(?:일|업무|건|이슈)"),
    # "배정됐는데 한 달 넘은"처럼 역접(-는데/-지만)이 붙으면 아직 안 끝난
    # 일을 뜻한다. 종료된 건까지 돌려주면 밀린 업무를 묻는 의도와 어긋난다.
    re.compile(r"(?:배정|할당|맡)(?:았|받|됐|되었|된|은)[^.?!]{0,10}?(?:는데|지만|음에도|인데)"),
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


NOTION_DISCOVERY_MAX_PAGES = 10
NOTION_DISCOVERY_FAILURE_TTL_SECONDS = 30
_notion_discovery_lock = threading.Lock()
_notion_discovery_cache: Dict[str, Tuple[float, List[str], str, str]] = {}
_notion_request_lock = threading.Lock()
_notion_next_request_at = 0.0
_notion_default_request_interval = os.getenv(
    "NOTION_MIN_REQUEST_INTERVAL_SECONDS", "0.36"
)


def get_notion_token() -> str:
    return os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_TOKEN") or ""


def notion_discovery_ttl_seconds() -> int:
    try:
        return max(30, min(int(os.getenv("NOTION_CACHE_TTL_SECONDS", "600")), 3600))
    except ValueError:
        return 600


def notion_discovery_max_databases() -> int:
    # 질문마다 DB별로 스키마·제목·본문 조회를 하므로 DB 수가 곧 요청 수다.
    # 상위 페이지를 연결해 수백 개 DB가 잡히면 한 질문에 수천 번 호출한다.
    try:
        return max(1, min(int(os.getenv("NOTION_DISCOVERY_MAX_DATABASES", "20")), 200))
    except ValueError:
        return 20


def discover_notion_database_ids(token: str) -> Tuple[List[str], str, str]:
    """API 키의 integration에 연결된 DB를 Notion 검색 API로 찾는다.

    ``NOTION_DATABASE_IDS``를 비워 두면 연결(Connections)된 DB 전체가 검색
    대상이다. 설정 조회가 요청마다 여러 번 일어나므로 결과를 캐시하고,
    실패도 짧게 캐시해 Notion 장애 때 요청마다 재시도하지 않는다.
    반환값은 (DB ID 목록, 오류 메시지, 오류 종류 dependency|configuration)이다.
    """
    now = time.monotonic()
    with _notion_discovery_lock:
        cached = _notion_discovery_cache.get(token)
        if cached and cached[0] > now:
            return list(cached[1]), cached[2], cached[3]

    database_ids: List[str] = []
    error = ""
    error_kind = ""
    has_more = False
    payload: Dict[str, Any] = {
        "filter": {"property": "object", "value": "database"},
        "page_size": 100,
    }
    try:
        for _ in range(NOTION_DISCOVERY_MAX_PAGES):
            response = notion_post_json(f"{NOTION_API_BASE}/search", payload, timeout=20)
            if response.status_code >= 400:
                error = f"Notion DB 목록 조회 실패: {response.status_code} {response.text[:300]}"
                error_kind = "dependency"
                break
            data = response.json()
            database_ids.extend(
                str(item.get("id") or "").replace("-", "")
                for item in data.get("results", [])
                if item.get("object") == "database"
                and item.get("id")
                and not item.get("archived")
                and not item.get("in_trash")
            )
            next_cursor = data.get("next_cursor")
            has_more = bool(data.get("has_more") and next_cursor)
            if not has_more:
                break
            payload["start_cursor"] = next_cursor
    except (requests.RequestException, ValueError) as exc:
        error = f"Notion DB 목록 조회 실패: {type(exc).__name__}"
        error_kind = "dependency"

    database_ids = list(dict.fromkeys(database_ids))
    max_databases = notion_discovery_max_databases()
    if not error and (has_more or len(database_ids) > max_databases):
        count = f"{len(database_ids)}개 이상" if has_more else f"{len(database_ids)}개"
        error = (
            f"API 키에 연결된 Notion DB가 {count}로 자동 검색 한도({max_databases}개)를 넘습니다. "
            "검색할 DB에만 integration을 연결하거나 NOTION_DATABASE_IDS에 DB ID를 지정하세요."
        )
        error_kind = "configuration"
    if error:
        # 일부 목록이나 임의로 자른 목록으로 '전체 연결 DB'를 대신하지 않는다.
        database_ids = []
    ttl = NOTION_DISCOVERY_FAILURE_TTL_SECONDS if error_kind == "dependency" else notion_discovery_ttl_seconds()
    with _notion_discovery_lock:
        _notion_discovery_cache[token] = (time.monotonic() + ttl, database_ids, error, error_kind)
    return list(database_ids), error, error_kind


def get_notion_settings() -> Dict[str, Any]:
    token = get_notion_token()
    database_ids = parse_notion_database_ids()
    database_source = "configured"
    discovery_error = ""
    discovery_error_kind = ""
    if token and not database_ids:
        database_ids, discovery_error, discovery_error_kind = discover_notion_database_ids(token)
        database_source = "discovered"
    return {
        "token": token,
        "database_ids": database_ids,
        "database_source": database_source,
        "database_discovery_error": discovery_error,
        "database_discovery_error_kind": discovery_error_kind,
        "version": os.getenv("NOTION_API_VERSION") or NOTION_DEFAULT_VERSION,
    }


def notion_unavailable_reason(settings: Dict[str, Any]) -> str:
    if not settings.get("token"):
        return "NOTION_API_TOKEN이 설정되지 않았습니다."
    if settings.get("database_discovery_error"):
        return str(settings["database_discovery_error"])
    if settings.get("database_source") == "discovered":
        return "API 키의 integration에 연결된 Notion DB가 없습니다. DB의 연결(Connections)에 integration을 추가하세요."
    return "NOTION_DATABASE_IDS에 조회할 DB가 없습니다."


def get_notion_headers() -> Dict[str, str]:
    # DB 자동 조회 요청도 이 헤더를 쓰므로 여기서 get_notion_settings를 부르지 않는다.
    return {
        "Authorization": f"Bearer {get_notion_token()}",
        "Notion-Version": os.getenv("NOTION_API_VERSION") or NOTION_DEFAULT_VERSION,
        "Content-Type": "application/json",
    }


def notion_request_slot() -> None:
    """Serialize Notion calls at the integration's supported average rate."""
    global _notion_next_request_at
    try:
        interval = max(
            0.0,
            min(
                float(
                    os.getenv(
                        "NOTION_MIN_REQUEST_INTERVAL_SECONDS",
                        _notion_default_request_interval,
                    )
                ),
                2.0,
            ),
        )
    except ValueError:
        interval = 0.36
    if interval <= 0:
        return
    with execution_slot(_notion_request_lock):
        now = time.monotonic()
        wait_seconds = max(0.0, _notion_next_request_at - now)
        if wait_seconds:
            interruptible_sleep(wait_seconds)
            now = time.monotonic()
        _notion_next_request_at = max(now, _notion_next_request_at) + interval


def _notion_retry_delay(response: requests.Response, attempt: int) -> float:
    retry_after = 0.0
    try:
        retry_after = float(response.headers.get("Retry-After", "0"))
    except (AttributeError, TypeError, ValueError):
        pass
    exponential = min(0.5 * (2**attempt), 4.0)
    return min(max(retry_after, exponential) + random.uniform(0.0, 0.15), 10.0)


def notion_post_json(url: str, payload: Dict[str, Any], timeout: int = 45, retries: int = 2) -> requests.Response:
    # A single Notion tool call must not monopolize the whole chat request.
    # Callers may request a larger timeout/retry count, but the orchestration
    # budget caps each network attempt. Chat orchestration can issue more than
    # one Notion query. 정확 제목 조회의 간헐적인 8초 초과를 흡수하되 전체 요청을
    # 장시간 점유하지 않도록 기본 예산은 12초, 네트워크/429/5xx 재시도 1회로 제한한다.
    max_timeout = max(3, min(int(os.getenv("NOTION_TOOL_TIMEOUT_SECONDS", "12")), 45))
    max_retries = max(0, min(int(os.getenv("NOTION_TOOL_MAX_RETRIES", "1")), 2))
    timeout = min(timeout, max_timeout)
    retries = min(retries, max_retries)
    last_error: Optional[requests.RequestException] = None
    for attempt in range(max(1, retries + 1)):
        try:
            notion_request_slot()
            response = requests.post(
                url,
                headers=get_notion_headers(),
                json=payload,
                timeout=bounded_timeout(timeout),
            )
            if (response.status_code == 429 or response.status_code >= 500) and attempt < retries:
                interruptible_sleep(_notion_retry_delay(response, attempt))
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                interruptible_sleep(min(0.5 * (2**attempt) + random.uniform(0.0, 0.15), 4.0))
    if last_error:
        raise last_error
    raise requests.RequestException("Notion request failed")


def notion_is_configured() -> bool:
    settings = get_notion_settings()
    return bool(settings["token"] and settings["database_ids"])
