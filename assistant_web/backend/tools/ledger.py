"""요청 단위 근거 원장. 모델 대화 이력과 분리해 실제 조회 결과와 출처 검증 정보를 보관한다.

컨텍스트를 줄이려고 대화의 도구 결과를 생략해도 원장은 그대로 남는다. 원장은 기존
retrieval_result 형태로 합성되어 근거 검증·출처·감사·후속 질문 복원을 그대로 사용한다.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from backend.evidence.retrieval_rows import collect_retrieved_issues
from backend.notion.matching import collect_retrieved_notion_pages
from backend.query_filters import filter_audit_for_source


GRADES = ("scoped", "reference")


def proves_empty(stage: Dict[str, Any]) -> bool:
    """전체 범위의 후보 수만 0건을 증명한다. 표시 창이나 의미 검색의 빈 결과는 별개다."""
    meta = stage.get("meta") or {}
    count_field = {"notion": "candidate_count", "mantis_snapshot": "found_count",
                   "mantis_page_scan": "found_count"}.get(str(stage.get("source")))
    return bool(count_field and stage.get("status") == "ok" and meta.get("search_complete") is True
                and meta.get("coverage") not in {"top_k", "snapshot_matches", "saved_filter"}
                and meta.get("candidate_count_complete") is not False and not meta.get("errors")
                and not meta.get("incomplete_reasons") and type(meta.get(count_field)) is int
                and meta[count_field] == 0 and not stage.get("issues_list") and not stage.get("notion_pages"))


@dataclass
class LedgerStage:
    family: str
    grade: str
    unapplied: Tuple[str, ...]
    call_id: str
    stage: Dict[str, Any]


@dataclass
class EvidenceLedger:
    stages: List[LedgerStage] = field(default_factory=list)
    notion_refs: Dict[str, str] = field(default_factory=dict)
    mantis_query_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    notion_query_snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    reports: List[Dict[str, Any]] = field(default_factory=list)
    condition_steps: List[Dict[str, Any]] = field(default_factory=list)
    related_pages: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    guide_retries: Dict[int, Dict[str, Any]] = field(default_factory=dict)

    def record_step(self, step: dict, first_stage: int, result: dict, tool: str, call_id: str) -> None:
        """확정된 각 단계와 실제 결과를 묶는다. 추가 조사는 답변 모집단에 합치지 않는다."""
        indices = [i for i in range(first_stage, len(self.stages)) if self.stages[i].call_id == call_id]
        audit = {**deepcopy(step), 'tool': tool, 'call_id': call_id, 'ok': result.get('ok') is True,
                 'search_complete': result.get('search_complete'), 'stage_count': len(indices),
                 'stage_indices': indices}
        self.condition_steps.append(audit)
        for index in indices:
            item = self.stages[index]
            meta = item.stage.setdefault('meta', {})
            meta['condition_step'] = deepcopy(step)
            prefix = 'investigation_' if step['purpose'] == 'investigate' else 'comparison_' if step.get('query_scope_id') else ''
            if prefix:
                for key in ('query_result', 'aggregation_result', 'notion_query_result', 'report_result'):
                    if key in meta:
                        meta[prefix + key] = meta.pop(key)

    def query_results(self, scope_id: str | None = None) -> List[Dict[str, Any]]:
        key = 'comparison_query_result' if scope_id else 'query_result'
        return [item.stage['meta'][key] for item in self.stages
                if item.grade == 'scoped' and item.stage.get('meta', {}).get(key)
                and item.stage['meta'].get('condition_step', {}).get('query_scope_id') == scope_id]

    def aggregation_results(self, scope_id: str | None = None) -> List[Dict[str, Any]]:
        key = 'comparison_aggregation_result' if scope_id else 'aggregation_result'
        return [item.stage['meta'][key] for item in self.stages
                if item.grade == 'scoped' and item.stage.get('meta', {}).get(key)
                and item.stage['meta'].get('condition_step', {}).get('query_scope_id') == scope_id]

    def notion_query_results(self) -> List[Dict[str, Any]]:
        return [item.stage['meta']['notion_query_result'] for item in self.stages
                if item.grade == 'scoped' and item.stage.get('meta', {}).get('notion_query_result')]

    def add(self, family: str, stage: Dict[str, Any], *, grade: str, unapplied: Iterable[str], call_id: str) -> None:
        if grade not in GRADES:
            raise ValueError("unknown evidence grade")
        self.stages.append(LedgerStage(family, grade, tuple(unapplied), call_id, stage))
        for page in stage.get("notion_pages") or []:
            page_id = str(page.get("id") or "")
            if page_id and page_id not in self.notion_refs:
                self.notion_refs[page_id] = f"N{len(self.notion_refs) + 1}"

    def _stages(self, families: Iterable[str], grades: Iterable[str]) -> List[Dict[str, Any]]:
        families, grades = set(families), set(grades)
        return [item.stage for item in self.stages if item.family in families and item.grade in grades]

    def issue_rows(self, grades: Iterable[str] = GRADES) -> List[Dict[str, Any]]:
        stages = self._stages({"mantis"}, grades)
        return collect_retrieved_issues({"stages": stages, "intent_plan": {"operation": "explain"}}, None) if stages else []

    def notion_pages(self, grades: Iterable[str] = GRADES) -> List[Dict[str, Any]]:
        stages = self._stages({"notion"}, grades)
        return collect_retrieved_notion_pages({"stages": stages}, 1000) if stages else []

    def notion_evidence_pages(self, grades: Iterable[str] = GRADES) -> List[Dict[str, Any]]:
        """저장본 검색 힌트는 모델의 후보 선택에만 쓰며 최종 사실 근거에서 제외한다."""
        return [p for p in self.notion_pages(grades) if not p.get('_candidate_only') or p.get('_verified_body') or p.get('_verified_properties')]

    def issue_grade(self, issue_id: int) -> Optional[str]:
        """한 번이라도 모든 조건을 적용한 조회에서 확인됐으면 scoped다."""
        for grade in GRADES:
            if any(int(row.get("id") or 0) == issue_id for row in self.issue_rows((grade,))):
                return grade
        return None

    def page_grade(self, page_id: str) -> Optional[str]:
        for grade in GRADES:
            if any(str(page.get("id")) == page_id for page in self.notion_pages((grade,))):
                return grade
        return None

    def issue_row(self, issue_id: int) -> Optional[Dict[str, Any]]:
        return next((row for row in self.issue_rows() if int(row.get("id") or 0) == issue_id), None)

    def notion_page(self, page_id: str) -> Optional[Dict[str, Any]]:
        return next((page for page in self.notion_pages() if str(page.get("id")) == page_id), None)

    def invalidate_notion_body(self, page_id: str, reason: str) -> None:
        """실패한 현재 본문을 근거에서 내리되 확인된 ref와 실패 사유는 보존한다."""
        for item in self.stages:
            if item.family != 'notion':
                continue
            for page in item.stage.get('notion_pages', []):
                if str(page.get('id')) != page_id:
                    continue
                page.update(content='', content_incomplete=True, _candidate_only=True, _body_read_failure=reason)
                for key in ('_verified_body', 'semantic_excerpt', '_owned_segments', '_verified_page_links'):
                    page.pop(key, None)
                item.stage.setdefault('meta', {}).update(search_complete=False, incomplete_reasons=[reason])
        if page_id in self.related_pages:
            self.related_pages[page_id].update(body_checked=False, guide=False, excerpt='')

    def page_id_for_ref(self, ref: str) -> Optional[str]:
        return next((page_id for page_id, value in self.notion_refs.items() if value == ref), None)

    def query_outcome(self, required: Iterable[str]) -> str:
        if (self.issue_rows(("scoped",)) or self.notion_evidence_pages(("scoped",))
                or any(result.get("total_count") for result in self.query_results())):
            if (any(p.get('_body_read_failure') or p.get('_verified_body') and p.get('content_incomplete')
                    for p in self.notion_pages(('scoped',)))
                    or any(item.grade == 'scoped' and item.stage.get('meta', {}).get('unavailable_link_count')
                           for item in self.stages)):
                return "partial_evidence"
            return "sufficient_evidence"
        scoped = [item for item in self.stages if item.grade == "scoped"
                  and item.stage.get('meta', {}).get('condition_step', {}).get('purpose') != 'investigate']
        if (scoped and set(required).issubset({item.family for item in scoped})
                and all(proves_empty(item.stage) for item in scoped)):
            return "complete_empty"
        return "search_incomplete" if self.stages else "no_evidence"

    def referenced_issue_ids(self) -> set[int]:
        """원장 이슈가 원문 관계나 서버 확인 참조로 연결한 이슈. 상세 조회 출처로 인정한다."""
        identifiers: set[int] = set()
        for row in self.issue_rows():
            detail = row.get("detail") or {}
            for value in [*(detail.get("related_issue_ids") or []), *(detail.get("source_reference_ids") or [])]:
                try:
                    identifiers.add(int(value))
                except (TypeError, ValueError):
                    continue
        return identifiers

    def refs(self, grades: Iterable[str] = GRADES) -> Dict[str, Dict[str, Any]]:
        """ref → 원장 항목. Mantis는 실제 이슈 번호, Notion은 서버가 매긴 번호를 쓴다."""
        catalog: Dict[str, Dict[str, Any]] = {}
        grades = tuple(grades)
        for row in self.issue_rows(grades):
            identifier = int(row.get("id") or 0)
            catalog[f"M#{identifier}"] = {"type": "mantis", "id": identifier, "title": row.get("summary", ""),
                                          "grade": self.issue_grade(identifier)}
        for page in self.notion_evidence_pages(grades):
            page_id = str(page.get("id") or "")
            ref = self.notion_refs.get(page_id)
            if ref:
                catalog[ref] = {"type": "notion", "id": page_id, "title": page.get("title", ""),
                                "grade": self.page_grade(page_id)}
        return catalog

    def unapplied_conditions(self, family: str) -> Tuple[str, ...]:
        labels: Dict[str, None] = {}
        for item in self.stages:
            if item.family == family and item.grade == "reference":
                labels.update(dict.fromkeys(item.unapplied))
        return tuple(labels)

    def as_retrieval_result(self, query: str, constraints: Dict[str, Any], plan: Dict[str, Any], limit: int, *,
                            families: Iterable[str] = ("mantis", "notion"),
                            grades: Iterable[str] = ("scoped",)) -> Dict[str, Any]:
        stages = deepcopy(self._stages(families, grades))
        for stage in stages:
            if 'notion_pages' in stage:
                stage['notion_pages'] = [p for p in stage['notion_pages'] if not p.get('_candidate_only') or p.get('_verified_body') or p.get('_verified_properties')]
        clauses = constraints.get("filter_clauses") or []
        return {
            "query": query, "keyword": "", "constraints": deepcopy(constraints),
            "intent_plan": deepcopy(plan), "query_plan": deepcopy(plan), "limit": limit, "stages": stages,
            "filter_audit": {"planned_filters": deepcopy(clauses),
                             "sources": [filter_audit_for_source(str(stage.get("source") or ""), clauses)
                                         for stage in stages]},
        }
