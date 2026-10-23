"""검색 후보의 증상/기능 근거를 판정한다. 유사 문구를 동일 원인으로 확정하지 않는다."""

import math
import re

from backend.display import customer_name
from collections import Counter, defaultdict


CRASH = re.compile(r"비정상\s*종료|강제\s*종료|크래[시쉬]|\bcrash(?:es|ed|ing)?\b|튕[기김겨]|죽(?:는|음|어|었|는다)|뻗(?:는|음|어)|앱\s*다운|프로그램\s*다운", re.I)
IOS = re.compile(r"아이폰|\biphone\b|\bios\b", re.I)
NEGATED = re.compile(r"(?:오류|장애|크래[시쉬]|crash|비정상\s*종료).{0,8}(?:없(?:음|습니다|다|는)|아님|아닌|않(?:음|습니다))", re.I)
CHART = re.compile(r"차트|차팅|\bchart\b|캔들|\d+\s*(?:분|초|틱)봉", re.I)
DEFECT = re.compile(r"오류|장애|비정상|불일치|누락|실패|먹통|멈춤|안\s*(?:되|됨|나오|보이)|않는\s*현상", re.I)
ENHANCEMENT = re.compile(r"추가|신규\s*개발|개선\s*요청|기능\s*요청|디자인|구현\s*가능|지원\s*가능", re.I)
STOP = frozenset({"mts", "hts", "ios", "iphone", "android", "차트", "이슈", "문의", "요청", "관련", "수정",
                  "가능", "여부", "확인", "구현", "기능", "추가", "개선", "현상", "발생", "에서", "이하", "이상", "대한", "화면"})


def positive_crash(text: str) -> bool:
    """단순 '종료/다운로드'나 명시적인 부정은 충돌 증거가 아니다."""
    return any(CRASH.search(sentence) and not NEGATED.search(sentence)
               for sentence in re.split(r"[\n.!?;]+", str(text or "")))


def crash_issue(row: dict) -> bool:
    title = str(row.get("summary") or "")
    # 제목에 별도 증상이 있고 본문에 과거 크래시가 언급된 경우를 구분한다.
    if positive_crash(title):
        return True
    if DEFECT.search(title) or ENHANCEMENT.search(title) or NEGATED.search(title):
        return False
    return positive_crash(str(row.get("description") or ""))


def incident_kind(row: dict) -> str:
    """제목 우선 분류. 명시되지 않은 사유는 미분류로 남긴다."""
    title = str(row.get("summary") or "")
    if positive_crash(title) or (DEFECT.search(title) and not NEGATED.search(title)):
        return "defect"
    if ENHANCEMENT.search(title):
        return "enhancement"
    # 본문의 인용/과거 사례만으로 장애를 확정하지 않는다.
    return "unknown"


def symptom_key(row: dict) -> str:
    title = str(row.get("summary") or "")
    for name, pattern in (("크래시", CRASH), ("멈춤", re.compile(r"먹통|멈춤")),
                          ("수치 불일치", re.compile(r"불일치")), ("표시 누락", re.compile(r"누락|안\s*보이|표시.*않"))):
        if pattern.search(title):
            return name
    return ""


def recurrence_candidates(rows: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        screen, symptom = row.get("screen_number"), symptom_key(row)
        # 0000/미상/여러 화면을 같은 화면으로 묶지 않는다.
        if screen and screen not in {"미상", "0000"} and symptom:
            groups[(row["project"], screen, symptom)].append(row["id"])
    return [{"project": k[0], "screen": k[1], "symptom": k[2], "count": len(set(ids))}
            for k, ids in sorted(groups.items()) if len(set(ids)) >= 2]


def concepts(text: str) -> set[str]:
    text = re.sub(r"\[.*?\]", " ", str(text or "")).casefold()
    # '1분할'의 분할은 시간 단위가 아니다. 단위 뒤의 실제 결합 표현만 허용한다.
    tokens = re.findall(r"\d+\s*(?:분|초|틱|봉)(?:대)?(?=$|[^가-힣]|봉|차트|차팅|이하|이상|으로|에서|내|마다|간격)|[a-z]{2,}|[가-힣]{2,}", text)
    result = set()
    for token in tokens:
        token = re.sub(r"\s+", "", token)
        if re.fullmatch(r"\d+(?:분|초|틱|봉)대", token):
            token = token[:-1]
        elif not re.match(r"\d", token):
            token = re.sub(r"(?:에서는|에서|으로|관련|여부|하는|되는|의|을|를|은|는)$", "", token)
        if len(token) >= 2 and token not in STOP:
            result.add(token)
    return result


def rank_similar(seed: dict, candidates: list[dict], *, historical: bool = False) -> list[dict]:
    """핵심 개념의 희소성·질의 충족률 순. 최신 ID는 동률일 때만 사용한다."""
    query = concepts(seed.get("summary", ""))
    if not query:
        return []
    candidates = [r for r in candidates if r["id"] != seed["id"] and
                  (not historical or (r.get("created_at") and seed.get("created_at") and
                                      (r["created_at"], r["id"]) < (seed["created_at"], seed["id"])))]
    titles = [concepts(row.get("summary", "")) for row in candidates]
    frequency = Counter(t for tokens in titles for t in tokens & query)
    weights = {t: math.log(1 + len(candidates) / (1 + frequency[t])) for t in query}
    total = sum(weights.values())
    numeric = {t for t in query if re.match(r"\d", t)}
    chart_query = bool(CHART.search(seed.get("summary", "")))
    ranked = []
    for row, tokens in zip(candidates, titles, strict=True):
        overlap = query & tokens
        # 주문 조회 주기나 검색 시각은 같은 '1분'이어도 차트 기능의 근거가 아니다.
        if chart_query and not CHART.search(row.get("summary", "")):
            continue
        # 숫자 단위/증상이 다르면 일반 차트 용어로 보충하여 통과시키지 않는다.
        if not overlap or (numeric and not numeric & overlap) or (positive_crash(seed["summary"]) and not crash_issue(row)):
            continue
        coverage = sum(weights[t] for t in overlap) / total
        if coverage < .45:
            continue
        precision = len(overlap) / max(len(tokens), 1)
        score = coverage * .8 + precision * .2
        ranked.append({**row, "similarity_score": round(score, 6), "similarity_terms": sorted(overlap)})
    return sorted(ranked, key=lambda r: (r["similarity_score"], r["created_at"], r["id"]), reverse=True)


def narrowing_question(rows: list[dict], constraints: dict, subject: str) -> str:
    if not rows:
        return "조건에 맞는 근거를 찾지 못했습니다. 고객사나 화면번호, 기억나는 오류 문구를 알려 주세요."
    if not constraints.get("customer_key") and len({r["project"] for r in rows}) > 1:
        projects = list(dict.fromkeys(r["project"] for r in rows))[:3]
        return "어느 고객사 건인가요? 후보 고객사는 " + ", ".join(customer_name(project) for project in projects) + " 등입니다."
    if not constraints.get("platform_key") and subject != "아이폰":
        return "모바일(MTS) 건인가요, PC(HTS) 건인가요? 기억나는 화면번호나 오류 문구가 있으면 함께 알려 주세요."
    return "화면번호나 발생 시점, 기억나는 오류 문구를 알려 주세요. 지금 조건만으로 특정 이슈의 진행 상태를 단정할 수 없습니다."
