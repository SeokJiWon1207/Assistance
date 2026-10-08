"""자연어 업무 질문을 완전한 원본 집계로 답한다. 모델이 수치/담당자를 만들지 않는다.

업무별 날짜 의미와 기본 범위를 명시하며, 기록에 없는 원인·완료 이력은 추정하지
않는다. 이 모듈은 Mantis 전용이며 Notion/일반 대화를 가로채지 않는다.
"""

import re
from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta

from backend.intent.confirmed_context import confirmed_scope_text
from backend.mantis_status import mantis_status_bucket
from backend.query_planning import build_query_plan, extract_created_at_range, extract_relative_created_at_range
from backend.sources.mantis import normalize_issue, select_mantis_rows, build_sources_from_issue_rows
from backend.sources.mantis_store import read_complete_store
from backend.team_scope import TEAM_MEMBERS, TEAM_SCOPE_LABEL
from backend.common import display_mantis_status_label
from backend.intent.search_terms import wants_all_results
from backend.display import customer_name as _customer, korean_day as _day, korean_day_range as _day_range
from backend.reports.issue_relevance import (
    IOS, CRASH, crash_issue, incident_kind, recurrence_candidates,
    rank_similar, narrowing_question,
)


# 이슈를 근거로 문서를 써 달라는 요청. 질문 속 '담당자'·'원인'은 문서에 담을 항목 이름이라
# 담당자·원인 조회로 답하면 요청한 인수인계·사후 분석·안내문이 만들어지지 않는다.
ISSUE_DOCUMENT_REQUEST = re.compile(
    r"인수\s*인계|사후\s*분석|분석서|안내\s*(?:문|메일)|점검\s*공지|회의\s*(?:안건|공유문)|보고서|"
    r"작성해|초안|5줄\s*요약"
)
# 유사 이슈의 원인·해결을 비교해 달라는 요청은 유사 해결 리포트(reports/similar.py)가 맡는다.
SIMILAR_RESOLUTION_REQUEST = re.compile(r"원인|해결|조치|비교")


def work_operation(question: str, constraints: dict) -> str | None:
    """작업 종류만 선택한다. 이름/상태/날짜 값은 공통 조건 해석기를 사용한다."""
    if re.search(r"노션|notion|노선", question, re.I):
        return None
    if constraints.get("unknown_customers"):
        return None
    if constraints.get("issue_ids") and ISSUE_DOCUMENT_REQUEST.search(question):
        return None
    if any(p.get("origin") == "ambiguous_alias" for p in constraints.get("people", {}).get("candidates", [])):
        return "ambiguous_person"
    if re.search(r"남은\s*(?:공수|시간|기간|예산)|잔여\s*(?:공수|시간|기간|예산)", question):
        return None
    if re.search(r"지난주보다|저번주보다|줄었|늘었", question):
        return "comparison"
    if re.search(r"고객.*(?:답|회신).*기다|회신\s*대기", question):
        return "waiting"
    if re.search(r"이번\s*주.*(?:처리했|끝냈|완료했)|지난\s*주.*(?:처리했|끝냈|완료했)", question):
        return "completed"
    if re.search(r"(?:이번\s*주|이번\s*달|오늘).*끝내야|(?:마감|기한).*(?:이번|오늘)|(?:이번|오늘).*(?:마감|기한)", question):
        return "due"
    if re.search(r"누가.*바[쁘빠]|한\s*사람.*몰|담당자별.*(?:남|미종료|업무)", question):
        return "workload"
    if re.search(r"뭐부터|무엇부터|급한\s*(?:것|거|건)|중요한\s*것.*정리", question):
        return "priority"
    if re.search(r"묵혀|묵은", question):
        return "aging"
    if re.search(r"어디.*(?:제일|가장|많이)|문제.*생기는\s*데", question):
        return "customers"
    if re.search(r"담당자.*(?:안\s*정|미정|미지정)", question):
        return "unassigned"
    if constraints.get("issue_ids"):
        if re.search(r"비슷|유사|예전에도", question):
            return None if SIMILAR_RESOLUTION_REQUEST.search(question) else "similar"
        if re.search(r"왜|원인|사유", question):
            return "cause"
        if re.search(r"누구|담당자", question):
            return "owner"
        if re.search(r"어디까지|어떻게\s*됐|진행", question):
            return "detail"
        if re.fullmatch(r"\s*(?:이슈\s*)?#?\d{5,7}(?:번)?[?.!]?\s*", question):
            return "detail"
    if constraints.get("work_lookup"):
        return "candidates"
    if IOS.search(question) and CRASH.search(question):
        return "candidates"
    if re.search(r"(?:차트|VWAP|아이폰).*(?:했|끝|어떻게|잖아)|아이폰.*죽", question, re.I):
        return "candidates"
    if constraints.get("people", {}).get("names") and re.search(r"뭐\s*하고|건만|업무", question):
        return "list"
    if constraints.get("customer_key") and re.search(r"요즘.*어때|현황", question):
        return "health"
    if re.search(r"다\s*끝났|개발\s*끝났|아직.*(?:남|못\s*끝)|끝난\s*건\s*빼", question):
        return "open"
    if constraints.get("customer_key") and re.search(r"남은|남아|미처리", question):
        return "open"
    if constraints.get("customer_key") and re.search(r"만\s*보면|^아니.*말고|^(?:그럼|그러면).*지난달|그중.*팀", question):
        return "list"
    return None


def _clear(constraints: dict, field: str, *keys: str) -> None:
    for key in keys:
        constraints.pop(key, None)
    constraints["filter_clauses"] = [c for c in constraints.get("filter_clauses", []) if c["field"] != field]


def _open(constraints: dict) -> None:
    if not constraints.get("statuses"):
        constraints["statuses"] = ["open"]
        constraints.setdefault("filter_clauses", []).append({"field": "status", "operator": "equals", "value": "open"})


def _due(row: dict) -> str:
    # 완료 실적일은 기한이 아니다.
    fields = row.get("date_fields") or {}
    return next((str(fields[k])[:10] for k in ("고객사전달 목표일", "개발완료 목표일", "QC 완료 목표일", "완료예정일", "수정예정일", "처리예정일", "마감일", "배포예정일")
                 if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fields.get(k) or "")[:10])), "")


def _line(row: dict) -> str:
    title = str(row.get("summary") or "제목 없음").replace("\n", " ")
    return f"- #{row['id']} {title} — {display_mantis_status_label(row['status'])} · {row['handler']} · {_customer(row['project'])}"


def build_work_query_report(question: str, constraints: dict, *, today: date | None = None, limit: int = 10) -> dict | None:
    operation = work_operation(question, constraints)
    if not operation:
        return None
    today = today or date.today()
    # '모두 보여줘'면 표시 개수 기본값(사이드바 5건)에 자르지 않는다.
    show_all = wants_all_results(question)
    if show_all:
        limit = max(limit, 50)
    c = deepcopy(constraints)
    notice = []
    # 날짜 없는 운영 질문의 '오늘'은 등록일 필터가 아니다.
    if operation in {"priority", "due", "completed", "comparison"}:
        _clear(c, "created_at", "created_at")
    if operation in {"priority", "aging", "due", "workload"}:
        if not (c.get("customer_key") or c.get("people", {}).get("names") or c.get("mantis_team_members")):
            c.update(mantis_team_members=list(TEAM_MEMBERS), mantis_team_label=TEAM_SCOPE_LABEL)
            notice.append("범위를 지정하지 않아 팀 대시보드 명단 기준으로 조회했습니다.")
        _open(c)
    if operation in {"open", "unassigned", "list"}:
        if re.search(r"다\s*끝났|끝난\s*건\s*빼", question):
            _clear(c, "status", "statuses", "excluded_statuses", "status_resolution")
        if operation != "list" or (c.get("people", {}).get("names") and not re.search(r"전체\s*상태|모든\s*상태", question)):
            _open(c)
    if operation == "list" and c.get("people", {}).get("names"):
        notice.append("Mantis에 배정된 미종료 업무이며 실시간 작업 중 여부를 뜻하지 않습니다.")
    if operation == "unassigned":
        c["unassigned_people"] = True
        notice.append("담당자 미지정은 담당자 명단만으로 팀 소속을 알 수 없습니다.")
    if operation == "customers" and not c.get("created_at"):
        period = {"from": (today - timedelta(days=29)).isoformat(), "to": today.isoformat()}
        c["created_at"] = period
        notice.append("‘요즘/계속’은 최근 30일 등록 건수로 해석했습니다. 건수만으로 재발 원인을 단정하지 않습니다.")
    if operation == "candidates" and not c.get("work_lookup"):
        # '끝났나'는 검색할 상태가 아니라 후보의 현재 상태에 대한 질문이다.
        _clear(c, "status", "statuses", "excluded_statuses", "status_resolution")
    if operation in {"comparison", "completed", "waiting", "ambiguous_person"}:
        explanations = {
            "comparison": "지난주와 현재의 미종료 건수 비교에는 각 시점의 상태 이력이 필요합니다. 현재 상태만으로 줄었는지 판단할 수 없습니다. 비교할 고객사/팀과 기간을 지정해 주세요.",
            "completed": "처리 완료일은 등록일이나 최종수정일과 다릅니다. 현재 저장된 요약 데이터만으로 이번/지난주 완료 목록을 확정할 수 없습니다. 상태 변경 이력 기준 조회가 필요합니다.",
            "waiting": "고객 회신 대기는 ‘정보부족’ 상태와 같은 뜻이 아닙니다. 현재 데이터에는 확정된 회신 대기 구분이 없어 단정할 수 없습니다. 고객사나 이슈 번호를 알려 주시면 해당 기록을 확인할 수 있습니다.",
            "ambiguous_person": "같은 이름의 담당자 후보가 여러 명입니다. 성까지 포함한 이름을 알려 주세요: " + ", ".join(p["name"] for p in c.get("people", {}).get("candidates", []) if p.get("origin") == "ambiguous_alias"),
        }
        return _response(question, c, operation, explanations[operation], [], None, [], notice)

    stored = read_complete_store(allow_stale=True, summary_only=operation not in {"cause", "candidates", "similar", "customers"})
    if stored is None or not stored.meta.get("snapshot_complete", stored.meta.get("search_complete", False)):
        return _response(question, c, operation, "전체 조회 원본을 사용할 수 없어 건수와 목록을 확정하지 않았습니다. 데이터 갱신 후 다시 조회해 주세요.", [], None, [], notice)
    meta = dict(stored.meta)
    if not meta.get("search_complete"):
        notice.append("갱신이 지연된 저장 데이터입니다. 아래는 저장 시점의 결과이며 현재 건수로 확정할 수 없습니다.")
    selected = select_mantis_rows(stored, c)
    rows = [normalize_issue(row) for row in selected]
    lookup = c.get("work_lookup") or {}
    subject = "아이폰" if IOS.search(question) else "VWAP" if re.search("vwap", question, re.I) else "차트" if "차트" in question else lookup.get("subject", "")
    crash_requested = bool((IOS.search(question) and CRASH.search(question)) or lookup.get("crash"))
    if operation == "candidates":
        c["work_lookup"] = {"subject": subject, "crash": crash_requested}
    if subject:
        if subject == "아이폰":
            rows = [r for r in rows if IOS.search(r["summary"] + "\n" + r["description"])
                    and (not crash_requested or crash_issue(r))]
        else:
            rows = [r for r in rows if subject.casefold() in (r["summary"] + r["description"]).casefold()]
        notice.append(f"검색어: {subject}" + (" · 비정상 종료/크래시. 일반 앱 종료·다운로드는 제외했습니다." if crash_requested else "."))
    # 후보 검색어도 확정 조건으로 보존한다. 문장 조사/구어체는 상속하지 않는다.
    if subject in {"VWAP", "차트"}:
        clause = {"field": "keyword", "operator": "contains", "value": subject}
        clauses = c.setdefault("filter_clauses", [])
        if clause not in clauses:
            clauses.append(clause)
    classification: Counter[str] = Counter()
    if (operation == "customers" and re.search(r"문제|장애|오류|반복|계속", question)) or c.get("work_incident_only"):
        c["work_incident_only"] = True
        classification = Counter(incident_kind(r) for r in rows)
        rows = [r for r in rows if incident_kind(r) == "defect"]
        notice.append(f"제목에 오류·장애 증거가 있는 이슈만 집계했습니다. 기능 추가/개선 요청 {classification['enhancement']}건, 미분류 {classification['unknown']}건은 분리했습니다.")
    if operation == "similar":
        seed = rows[0] if len(rows) == 1 else None
        if not seed:
            return _response(question, c, operation, "비교할 원본 이슈를 하나로 특정하지 못했습니다. 이슈 번호를 알려 주세요.", [], meta, [], notice)
        _clear(c, "issue_id", "issue_ids")
        candidates = [normalize_issue(r) for r in select_mantis_rows(stored, c) if r["id"] != seed["id"]]
        historical = bool(re.search(r"예전|과거|이전", question))
        rows = rank_similar(seed, candidates, historical=historical)
        notice.append(f"기준 이슈 {seed['id']}의 제목에서 핵심 기능·증상 개념이 일치하는 후보를 관련도순으로 정렬했습니다. 동일 원인이나 재발로 확정한 결과가 아닙니다.")
        if historical:
            notice.append(f"‘예전’은 기준 이슈 등록일({seed['created_at'] or '미상'}) 이전 사례로 해석했습니다.")
    if operation == "due":
        period = extract_relative_created_at_range(question, today) or extract_created_at_range(question) or {"from": today.isoformat(), "to": (today + timedelta(days=6-today.weekday())).isoformat()}
        missing = sum(not _due(r) for r in rows)
        rows = [r for r in rows if _due(r) and period["from"] <= _due(r) <= period["to"]]
        c["work_due_range"] = period
        notice.append(f"목표일 {_day_range(period['from'], period['to'], today)} 기준이며 고객사전달 → 개발완료 → QC 완료 목표일 순으로 봤습니다. 목표일이 비어 있는 {missing}건은 포함 여부를 알 수 없습니다.")
    if operation == "aging":
        rows = [r for r in rows if r["created_at"] and r["created_at"] <= (today-timedelta(days=30)).isoformat()]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        notice.append("오래 묵은 업무는 등록 후 30일 이상 지난 미종료로 해석했습니다.")
    elif operation == "priority":
        if re.search(r"급한|중요한", question):
            rows = [r for r in rows if (_due(r) and _due(r) <= today.isoformat())
                    or r["priority"] in {"긴급", "즉시", "urgent", "immediate", "높음", "high"}]
            notice.append("급함/중요함은 높은 우선순위 또는 목표일 도래·경과로 한정했습니다.")
        rows.sort(key=lambda r: (0 if _due(r) and _due(r) <= today.isoformat() else 1,
                    0 if r["priority"] in {"긴급", "즉시", "urgent", "immediate", "높음", "high"} else 1,
                    _due(r) or "9999-12-31", r["created_at"] or "9999-12-31"))
        notice.append("기한 경과/도래 → 높은 우선순위 → 예정일/등록일 순 추천입니다. 실제 작업 중 여부는 상태만으로 알 수 없습니다.")
    elif operation != "similar":
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    shown = rows[:max(1, min(limit, 50 if show_all else 20))]
    if operation == "workload":
        counts = Counter(r["handler"] for r in rows)
        body = f"미종료 {len(rows)}건 · 담당자 {len(counts)}명입니다.\n\n" + "\n".join(f"- {name}: {count}건" for name, count in counts.most_common())
        body += "\n\n미종료 건수 기준입니다. 난이도·공수·휴가를 반영한 실제 바쁨이나 성과 평가는 아닙니다."
    elif operation == "customers":
        counts = Counter(_customer(r["project"]) for r in rows)
        body = f"조건에 맞는 등록 이슈 {len(rows)}건입니다.\n\n" + "\n".join(f"- {name}: {count}건" for name, count in counts.most_common(10))
        if c.get("work_incident_only"):
            repeats = recurrence_candidates(rows)
            body += "\n\n반복 발생 후보(같은 고객사·화면번호·증상에 서로 다른 이슈 2건 이상):\n"
            body += "\n".join(f"- {g['project']} · 화면 {g['screen']} · {g['symptom']}: {g['count']}건" for g in repeats) or "확정 가능한 후보가 없습니다."
            body += "\n동일 원인의 재발인지는 각 이슈의 원인·조치 이력을 대조해야 합니다."
    elif operation == "health":
        opened = [r for r in rows if mantis_status_bucket(r["status"]) == "open"]
        recent = [r for r in rows if (today-timedelta(days=29)).isoformat() <= r["created_at"] <= today.isoformat()]
        overdue = [r for r in opened if _due(r) and _due(r) < today.isoformat()]
        body = f"전체 {len(rows)}건 중 미종료 {len(opened)}건, 최근 30일 등록 {len(recent)}건, 목표일 경과 {len(overdue)}건입니다."
        shown = sorted(opened, key=lambda r: r["id"], reverse=True)[:limit]
    elif operation in {"cause", "owner", "detail"}:
        body = "\n".join(_line(r) for r in shown) or "해당 이슈를 찾지 못했습니다."
        if operation == "cause":
            body += "\n\n" + "\n".join(f"#{r['id']} 기록된 사유 및 원인: {r['resolution_fields'].get('사유 및 원인') or '미입력 — 남아 있는 이유를 추정하지 않았습니다.'}" for r in shown)
        elif operation == "detail":
            body += "\n\n" + "\n".join(f"#{r['id']} 등록일 {r['created_at'] or '미입력'} · 최종수정일 {r['updated_at'] or '미입력'}\n기록된 조치 및 개선: {r['resolution_fields'].get('조치 및 개선') or '미입력 — 현재 상태 이상의 진행 상황은 확인할 수 없습니다.'}" for r in shown)
        shown = []  # 본문에 이미 렌더링했지만 출처는 아래에서 유지한다.
    else:
        body = f"조건에 맞는 이슈 {len(rows)}건입니다."
        if operation == "open" and re.search(r"다\s*끝났", question):
            body = f"미종료 {len(rows)}건입니다. " + ("아직 모두 끝나지 않았습니다." if rows else "미종료로 분류되는 이슈는 없습니다. 상태 미상이나 미기록 작업까지 완료됐다는 뜻은 아닙니다.")
        if operation == "candidates":
            if len(rows) != 1:
                body += " 특정 이슈를 아직 확정하지 못했습니다."
            if re.search(r"끝났|완료", question):
                body += " 전체가 완료됐다고 단정하지 않았습니다."
            if len(rows) != 1:
                body += "\n\n" + narrowing_question(rows, c, subject)
                if len(rows) > 5:
                    body += "\n후보가 많아 임의의 최신 이슈를 정답처럼 제시하지 않았습니다."
                    shown = []
    if shown:
        body += "\n\n" + "\n".join(_line(r) + (f" · 목표일 {_day(_due(r), today)}" if operation in {"due", "priority"} and _due(r) else "") +
                                  (" · 일치 개념: " + ", ".join(r["similarity_terms"]) if r.get("similarity_terms") else "") for r in shown)
        if len(rows) > len(shown):
            body += f"\n\n전체 {len(rows)}건 중 {len(shown)}건을 표시했습니다. 나머지도 보려면 질문 끝에 '모두 보여줘'를 붙여 주세요."
    evidence = rows[:limit] if operation in {"cause", "owner", "detail"} else shown
    return _response(question, c, operation, body, evidence, meta, rows, notice)


def _response(question, constraints, operation, body, shown, meta, rows, notice):
    scope = confirmed_scope_text(constraints) or "전체 고객사"
    if constraints.get("statuses") == ["open"]:
        notice.append("미종료는 해결·종료를 빼고 개발완료는 포함합니다.")
    # 저장 시각과 조회 완료 여부는 답변 아래 '데이터 기준' 영역(evaluation/data_scope.py)이 보여 준다.
    # 본문에는 이 답변을 읽는 데 필요한 조회 기준만 '조회 기준:' 묶음으로 남긴다(chat_rendering.js가 작게 그림).
    content = f"{body}\n\n조회 기준: {scope}"
    if notice:
        content += "\n" + "\n".join(f"- {line}" for line in notice)
    plan = build_query_plan(question, {"source": "mantis", "operation": "search"}, constraints)
    plan.update(source="mantis", resolved_constraints=deepcopy(constraints), terms=[], work_operation=operation)
    sources = build_sources_from_issue_rows(shown)
    stage = {"source": "mantis_work", "status": "ok" if meta else "skipped",
             "meta": {**(meta or {}), "operation": "aggregate", "matched_count": len(rows),
                      "matched_ids": [r["id"] for r in rows], "work_operation": operation}}
    return {"content": content, "sources": sources, "raw": {"mode": "deterministic_work_report"},
            "retrieval": {"query": question, "constraints": constraints, "query_plan": plan, "stages": [stage]}}


def verify_work_report(question: str, content: str, sources: list, retrieval: dict) -> dict:
    """운영 조회 계약을 검증한다. 미조회/과거 저장본을 성공으로 포장하지 않는다."""
    from backend.sources.mantis import issue_matches_constraints
    from backend.answers import display_constraints
    from backend.intent.scope_extraction import extract_scope_constraints
    from backend.team_scope import has_mantis_team_scope, remove_team_scope_phrase
    c = retrieval.get("constraints") or {}
    plan = retrieval.get("query_plan") or {}
    stages = retrieval.get("stages") or []
    meta = (stages[0].get("meta") or {}) if stages else {}
    ids = set(meta.get("matched_ids") or [])
    source_ids = {s.get("issue_id") for s in sources}
    answer_ids = {int(value) for value in re.findall(r"#(\d+)", content)}
    requested = extract_scope_constraints(remove_team_scope_phrase(question))
    intent_matches = all(not requested.get(key) or requested.get(key) == c.get(key)
                         for key in ("customer_key", "platform_key"))
    names = requested.get("people", {}).get("names") or []
    intent_matches &= not names or set(names) == set(c.get("people", {}).get("names") or [])
    if has_mantis_team_scope(question):
        intent_matches &= set(c.get("mantis_team_members") or []) == set(TEAM_MEMBERS)
    matched = True
    for source in sources:
        row = source.get("detail") or {}
        raw = {"id": source.get("issue_id"), "project": {"name": source.get("project")},
               "summary": source.get("title"), "status": {"name": source.get("status")},
               "handler": {"real_name": source.get("handler")}, "reporter": {"real_name": row.get("reporter")},
               "created_at": source.get("created_at"), "description": row.get("description", ""),
               "custom_fields": [{"field": {"name": "매체구분"}, "value": "" if row.get("media_type") == "기타/미상" else row.get("media_type", "")},
                                 {"field": {"name": "화면번호"}, "value": row.get("screen_number", "")},
                                 *[{"field": {"name": k}, "value": v} for k, v in row.get("date_fields", {}).items()]]}
        matched &= issue_matches_constraints(raw, c)
        if c.get("work_due_range"):
            matched &= c["work_due_range"]["from"] <= _due(row) <= c["work_due_range"]["to"]
    checks = [
        {"id": "work_intent", "label": "명시한 대상 일치", "status": "pass" if intent_matches else "fail", "detail": "질문의 고객사·플랫폼·담당자·팀을 실행 범위와 비교"},
        {"id": "work_scope", "label": "실행 조건·원문 일치", "status": "pass" if matched and plan.get("resolved_constraints") == c else "fail", "detail": "담당자·팀·상태·고객사·날짜 조건 검사"},
        {"id": "work_evidence", "label": "답변·원문 ID 일치", "status": "pass" if answer_ids <= source_ids <= ids else "fail", "detail": "답변과 첨부 원문을 실제 조회 ID와 비교"},
        {"id": "work_count", "label": "집계 원본", "status": "pass" if meta.get("snapshot_complete") and len(ids) == meta.get("matched_count") else "warning", "detail": f"전체 저장본의 고유 결과 {len(ids)}건"},
        {"id": "work_freshness", "label": "데이터 최신성", "status": "pass" if meta.get("search_complete") else "warning", "detail": str(meta.get("fetchedAt") or "조회/확정하지 않은 질문")},
    ]
    total = re.match(r"(?:조건에 맞는 (?:등록 )?이슈|미종료|전체)\s*(\d+)건", content)
    if total:
        checks.append({"id": "work_answer_count", "label": "답변 집계값", "status": "pass" if int(total[1]) == len(ids) else "fail", "detail": "답변의 총 건수를 고유 조회 결과와 비교"})
    if plan.get("work_operation") in {"similar", "candidates"} or c.get("work_incident_only"):
        checks.append({"id": "semantic_review", "label": "의미·유용성 확인", "status": "warning",
                       "detail": "원본/조건 일치와 별개로 후보의 관련성·특정 이슈·동일 원인 재발은 확인이 필요합니다."})
    status = "fail" if any(ch["status"] == "fail" for ch in checks) else "warning" if any(ch["status"] == "warning" for ch in checks) else "pass"
    score = round(sum({"pass": 1, "warning": .5, "fail": 0}[ch["status"]] for ch in checks) / len(checks) * 100)
    return {"score": score, "status": status, "checks": checks,
            "dimensions": [{"id": "work", "label": "업무 원본 검증", "checks": checks, "score": score, "status": status}],
            "completedDimensions": 1, "applicableDimensions": 1, "constraints": display_constraints(c), "queryPlan": plan,
            "trace": [], "sourceCount": len(sources), "mode": "deterministic_work_report",
            "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds")}
