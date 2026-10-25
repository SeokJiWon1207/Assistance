"""답변의 명시적인 이슈 속성을 같은 ID의 원문과 대조한다."""

import html
import json
import re
from typing import Any

from backend.common import display_mantis_status_label, normalize_for_match, text_matches_customer_aliases, text_matches_any_alias
from backend.config import CUSTOMER_ALIASES
from backend.display import customer_name
from backend.issue_text import decode_issue_field
from backend.query_planning import plan_created_at_matches


FIELD_LABELS = {
    "id": "id", "이슈id": "id", "이슈번호": "id", "이슈": "id", "번호": "id",
    "제목": "title", "프로젝트": "project", "고객사": "project",
    "상태": "status", "현재상태": "status", "담당자": "handler",
    "등록일": "created_at", "접수일": "created_at", "최종수정일": "updated_at",
    "매체구분": "media_type", "플랫폼": "media_type",
    "우선순위": "priority", "중요도": "priority", "심각도": "severity",
}
FIELD_LABELS.update({field: field for field in FIELD_LABELS.values()})
FIELD_LABELS.update({"issue_id": "id", "summary": "title"})
EXPLICIT_ID = re.compile(r"(?:#|(?:이슈\s*(?:번호|ID)?|\bID)\s*[:：]?\s*)0*(\d{1,7})(?!\d|년|월|일|건)", re.I)
LABELLED_FIELD = re.compile(
    r"(?P<label>최종수정일|현재상태|프로젝트|이슈\s*(?:번호|ID)|담당자|매체구분|우선순위|"
    r"플랫폼|등록일|접수일|고객사|심각도|제목|상태|\bID)\s*[:：]\s*"
    r"(?P<value>.*?)(?=\s*\||$)", re.I,
)
UNKNOWN = {"", "-", "미상", "미지정", "담당자미지정", "확인불가", "확인되지않음", "미기재", "없음"}


def plain_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("**", "").replace("`", "").replace(r"\|", "|").strip()


def extract_issue_claims(content: str, *, source_sections: bool = False,
                         source_quotes: tuple[str, ...] = ()) -> tuple[set[int], list[tuple[int, str, str]]]:
    """Markdown 표, 항목별 목록, 명시적인 담당자 문장에서 ID와 속성을 읽는다.

    코드가 인용하는 원문 본문/이력은 현재 속성 주장이 아니므로 별도로 취급한다.
    임의의 숫자(날짜, 건수, 화면번호)는 ID로 간주하지 않는다.
    """
    source_colors = {match[1].casefold() for source in source_quotes
                     for match in re.finditer(r'\bcolor["\']?\s*[:=]\s*["\']?(#[0-9a-f]{6})(?![0-9a-f])', source, re.I)}

    def reference_ids(text: str):
        # 원문에서 확인된 color 값만 색상 문맥에서 구분한다. 명시 이슈 참조는 그대로 검사한다.
        if source_colors and re.search(r'색상|color', text, re.I) and not re.search(r'이슈|issue|\bID\b', text, re.I):
            text = re.sub(r'#[0-9a-f]{6}(?![0-9a-f])',
                          lambda match: '색상코드' if match[0].casefold() in source_colors else match[0], text, flags=re.I)
        return EXPLICIT_ID.findall(text)

    ids: set[int] = set()
    claims: list[tuple[int, str, str]] = []
    headers: list[str] = []
    current_id: int | None = None
    feature_section = False
    feature_identified = False
    in_source = False
    json_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    try:
        structured = json.loads(json_text) if json_text.startswith(("{", "[")) else None
    except (ValueError, RecursionError):
        structured = None
    pending = [structured]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            json_fields = {FIELD_LABELS.get(str(key).casefold(), ""): value for key, value in item.items()}
            identifier = str(json_fields.get("id", ""))
            if re.fullmatch(r"#?\d{1,7}", identifier):
                issue_id = int(identifier.lstrip("#"))
                ids.add(issue_id)
                for key, value in json_fields.items():
                    if not key or key == "id":
                        continue
                    if isinstance(value, dict):
                        value = value.get("label") or value.get("real_name") or value.get("name") or ""
                    if not isinstance(value, list):
                        claims.append((issue_id, key, str(value or "")))
            # 담당자·상태 등의 내부 ID는 이슈 ID가 아니다.
            pending.extend(value for key, value in item.items()
                           if (str(key).casefold() not in FIELD_LABELS or str(key).casefold() == "이슈")
                           and isinstance(value, (dict, list)))
    if structured is not None:
        return ids, claims
    for raw_line in content.splitlines():
        line = plain_text(raw_line)
        quote = re.fullmatch(r'(?:>\s*|원문\s*인용\s*[:：]\s*)[“"‘\']?(.+?)[”"’\']?', line)
        if quote and any(quote[1] in source_text for source_text in source_quotes):
            continue
        if source_sections and re.match(r"^\*\*#\d+\s", raw_line):
            in_source = False
        if source_sections and raw_line.strip() in {"**내용**", "**사유 및 원인**", "**조치 및 개선**"}:
            in_source = True
        if in_source:
            continue
        # 목록 필드 안의 ID 표기는 원문 제목의 일부다. 행의 ID와 속성을 각각 검증한다.
        item = re.match(r"^\s*\d+\.\s*(?:\*\*)?ID(?:\*\*)?\s*:\s*#?(\d{1,7})\s*\|", raw_line, re.I)
        if item:
            feature_section = feature_identified = False
            issue_id = int(item[1])
            ids.add(issue_id)
            current_id = issue_id
            headers = []
            for part in re.split(r"(?<!\\)\|", raw_line)[1:]:
                label, separator, value = part.partition(":")
                field = FIELD_LABELS.get(normalize_for_match(plain_text(label)), "")
                if separator and field and field != "id":
                    claims.append((issue_id, field, decode_issue_field(value.strip())))
                else:
                    ids.update(int(v) for v in reference_ids(plain_text(part)))
            continue
        if "|" in raw_line:
            cells = [plain_text(decode_issue_field(cell.strip())) for cell in re.split(r"(?<!\\)\|", raw_line.strip().strip("|"))]
            fields = [FIELD_LABELS.get(normalize_for_match(cell), "") for cell in cells]
            if "id" in fields and any(field and field != "id" for field in fields):
                feature_section = feature_identified = False
                headers = fields
                current_id = None
                ids.update(int(v) for key, cell in zip(fields, cells, strict=True) if not key
                           for v in reference_ids(cell))
                continue
            if all(re.fullmatch(r"[-:\s]+", cell) for cell in cells):
                continue
            if headers and len(cells) == len(headers):
                id_cell = cells[headers.index("id")]
                match = re.fullmatch(r"#?\s*(\d{1,7})", id_cell)
                if match:
                    issue_id = int(match[1])
                    ids.add(issue_id)
                    claims.extend((issue_id, key, value) for key, value in zip(headers, cells, strict=True)
                                  if key and key != "id")
                    for key, cell in zip(headers, cells, strict=True):
                        if key == "id" and re.fullmatch(r"#?\s*\d{1,7}", cell):
                            ids.add(int(cell.lstrip("#").strip()))
                        elif not key or key == "id":
                            ids.update(int(v) for v in reference_ids(cell))
                    continue
            if headers:
                # 검증 가능한 속성 행이 아닌 경우에도 명시된 이슈 참조는 검사한다.
                ids.update(int(v) for v in reference_ids(line))
                continue
        line_ids = {int(v) for v in reference_ids(line)}
        ids.update(line_ids)
        headers = []
        # 기능의 보류/예정 상태를 Mantis 이슈의 현재 상태로 오인하지 않는다.
        # 독립 소제목과 명시적인 기능 항목이 모두 있는 좁은 범위에만 적용한다.
        section_heading = bool(re.match(r"^\s*#{1,6}\s+", raw_line)
                               or re.fullmatch(r"\s*\*\*[^*]+\*\*\s*", raw_line))
        if section_heading or line_ids:
            feature_section = section_heading and not line_ids and bool(re.search(r"기능(?:\s|$|[(:])", line))
            feature_identified = False
        if feature_section and re.match(r"^\s*(?:[-*]\s*)?기능\s*[:：]", line):
            feature_identified = True
        if len(line_ids) == 1:
            current_id = next(iter(line_ids))
        elif len(line_ids) > 1:
            current_id = None
        if current_id is None:
            continue
        heading = re.match(r"^(?:#{1,6}\s+)?#\d+\s+(.+)$", line)
        if heading and re.match(r"^\s*(?:\*\*#\d+\s+(?!\*\*)|#{1,6}\s+#)", raw_line):
            # 분석 소제목은 이슈 제목을 그대로 인용한다는 주장이 아니다.
            section = re.fullmatch(r"(?:원인|해결|조치|현황|상세|이슈|문제|진행 상황)?\s*(?:분석|요약|정리|검토|상세|현황)", heading[1])
            if not section:
                claims.append((current_id, "title", heading[1]))
            continue
        for match in LABELLED_FIELD.finditer(line):
            field = FIELD_LABELS[normalize_for_match(match["label"])]
            prefix = line[:match.start()].rstrip()
            # '문제 플랫폼', '마지막 진행 상태' 같은 설명 항목의 일부를
            # 구조화된 현재 이슈 속성 라벨로 잘라 읽지 않는다.
            if (re.search(r"[가-힣A-Za-z]$", prefix)
                    and not re.search(r"(?:현재|이슈|Mantis)(?:의)?$", prefix, re.I)):
                continue
            if (field == "status" and feature_identified and not line_ids
                    and not re.search(r"(?:현재|이슈|Mantis)(?:의)?\s*상태\s*[:：]", line, re.I)):
                continue
            if field != "id":
                value = match["value"]
                # 설명 답변의 출처는 속성값의 일부가 아니다. 같은 이슈의 정해진
                # 인용 형식만 벗기며 상태 뒤의 임의 서술·완료 주장은 그대로 검사한다.
                if field == "status":
                    value = re.sub(
                        rf"\s+\(#0*{current_id}\s*·\s*(?:필드\s+[\w가-힣 :.-]+|노트\s+\d+)"
                        r"(?:\s*·\s*\d{4}-\d{2}-\d{2})?\)$", "", value,
                    )
                claims.append((current_id, field, value))
        if len(line_ids) == 1:
            handler = re.search(r"담당자(?:는|가)\s+(.+?)(?:입니다|이에요|예요|이며|이고)(?:[.\s]|$)", line)
            if handler:
                claims.append((current_id, "handler", handler[1]))
    return ids, claims


def _matches(field: str, claimed: str, actual: Any) -> bool:
    if isinstance(actual, dict):
        actual = actual.get("label") or actual.get("real_name") or actual.get("name") or ""
    expected = plain_text(actual)
    candidate = plain_text(claimed)
    if normalize_for_match(candidate) in UNKNOWN:
        return normalize_for_match(expected) in UNKNOWN
    if field == "project":
        if normalize_for_match(candidate) == normalize_for_match(customer_name(expected)):
            return True
        for canonical, aliases in CUSTOMER_ALIASES.items():
            if (normalize_for_match(candidate) in {normalize_for_match(a) for a in [canonical, *aliases]}
                    and text_matches_customer_aliases(expected, aliases)):
                return True
    if field == "status":
        return display_mantis_status_label(candidate) == display_mantis_status_label(expected)
    if field in {"created_at", "updated_at"}:
        return candidate == expected or candidate == expected[:10]
    if field == "handler":
        # 괄호 속 '(담당자: 이름)'의 닫는 괄호와 이름 뒤 존칭은 속성이 아니다.
        # 원래 값 비교도 유지해 실제 이름이 '님'으로 끝나는 경우를 보존한다.
        presented = re.sub(r"\s*님$", "", candidate.rstrip("). "))
        if normalize_for_match(presented) == normalize_for_match(expected):
            return True
    return normalize_for_match(candidate) == normalize_for_match(expected)


def check_issue_fields(claims: list[tuple[int, str, str]], sources: list[dict[str, Any]]) -> dict[str, str]:
    by_id = {int(source["issue_id"]): source for source in sources if source.get("issue_id")}
    mismatches: list[str] = []
    for issue_id, field, value in claims:
        source = by_id.get(issue_id)
        if source is None:
            continue  # 없는 ID는 issue_ids 검사에서 차단한다.
        detail = source.get("detail") or {}
        actual = source.get(field, detail.get("summary" if field == "title" else field, ""))
        label = (source.get(f"{field}_label") or detail.get(f"{field}_label")) if field in {"status", "priority", "severity"} else None
        if not _matches(field, value, actual) and not (label and _matches(field, value, label)):
            mismatches.append(f"#{issue_id} {field}")
    return {"id": "issue_fields", "label": "이슈 속성 일치",
            "status": "fail" if mismatches else "pass",
            "detail": "원문과 다른 속성: " + ", ".join(dict.fromkeys(mismatches)) if mismatches
            else "명시된 이슈 속성이 원문과 일치" if claims else "명시적인 속성 비교 대상 없음"}


def has_definite_scope_mismatch(source: dict[str, Any], constraints: dict[str, Any]) -> bool:
    """본문의 다른 고객사/매체 언급으로 구조화 필드의 불일치를 덮지 않는다."""
    if source.get("type") != "mantis":
        return False
    detail = source.get("detail") or {}
    project = source.get("project") or detail.get("project") or ""
    media = source.get("media_type") or detail.get("media_type") or ""
    created = source.get("created_at") or detail.get("created_at") or ""
    if project and normalize_for_match(project) not in UNKNOWN:
        if constraints.get("customer_exact") and normalize_for_match(project) != normalize_for_match(constraints["customer_exact"]):
            return True
        if constraints.get("customer_aliases") and not text_matches_customer_aliases(project, constraints["customer_aliases"]):
            return True
        if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(project, constraints["excluded_customer_aliases"]):
            return True
    if media and media != "기타/미상":
        if constraints.get("platform_aliases") and not text_matches_any_alias(media, constraints["platform_aliases"]):
            return True
        if constraints.get("excluded_platform_aliases") and text_matches_any_alias(media, constraints["excluded_platform_aliases"]):
            return True
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", str(created))
                and not plan_created_at_matches(str(created), constraints.get("created_at")))
