"""에이전트 경로 결정형 테스트의 가짜 모델·원천·정책 구성. 실제 모델·사내 시스템에 접속하지 않는다."""

import itertools
import json
import os
import re
import tempfile
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch

from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext

COMPANY = {"id": "company-test", "label": "회사 모델", "model": "company-model", "provider": "openai-compatible",
           "endpoint": "https://company.test/v1/chat/completions", "apiKeyEnv": "AGENT_TEST_COMPANY_KEY",
           "think": False, "supportsTools": True, "supportsParallelToolCalls": True, "enabled": True}
EXTERNAL = {"id": "external-test", "label": "외부 모델", "model": "external-model", "provider": "openai",
            "endpoint": "https://api.external.test/v1/chat/completions", "apiKeyEnv": "AGENT_TEST_EXTERNAL_KEY",
            "supportsTools": True, "enabled": True}
LOCAL = {"id": "local-test", "label": "로컬 모델", "model": "local:1b", "provider": "ollama",
         "endpoint": "http://127.0.0.1:11434/v1/chat/completions", "nativeEndpoint": "http://127.0.0.1:11434/api/chat",
         "enabled": True}
SERVING = "vllm-test-fingerprint"


BACKUP = {"id": "backup-test", "label": "대체 모델", "model": "backup-model", "provider": "gemini",
          "endpoint": "https://backup.test/v1beta/openai/chat/completions", "apiKeyEnv": "AGENT_TEST_BACKUP_KEY",
          "supportsTools": True, "enabled": True}


def registry(*, company: Optional[Dict[str, Any]] = None, extra: tuple = ()) -> List[Dict[str, Any]]:
    return [{**model, "status": "active", "hasKey": True, "expired": False, "expiresAt": ""}
            for model in ((company or COMPANY), EXTERNAL, LOCAL, *extra)]


def verification_record(**overrides: Any) -> Dict[str, Any]:
    from backend.services.agent.policy import REQUIRED_CHECKS, config_fingerprint
    model = registry()[0]
    return {"verifiedAt": date.today().isoformat(), "result": "supported", "modelId": model["id"],
            "checks": {name: True for name in REQUIRED_CHECKS}, "configFingerprint": config_fingerprint(model),
            "servingFingerprint": SERVING, **overrides}


def backup_record(**overrides: Any) -> Dict[str, Any]:
    from backend.services.agent.policy import config_fingerprint
    resolved = {**BACKUP, "status": "active", "hasKey": True, "expired": False, "expiresAt": ""}
    return verification_record(modelId=BACKUP["id"], configFingerprint=config_fingerprint(resolved),
                               servingFingerprint=None, **overrides)


class FakeResponse:
    def __init__(self, status: int, data: Optional[Dict[str, Any]] = None):
        self.status_code = status
        self._data = data or {}
        self.headers: Dict[str, str] = {"content-type": "application/json"}
        self.text = json.dumps(self._data, ensure_ascii=False)
        self.closed = False

    def json(self) -> Dict[str, Any]:
        return deepcopy(self._data)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]

    def close(self) -> None:
        self.closed = True


_CALL_IDS = itertools.count(1)


def tool_call(name: str, arguments: Any, call_id: Optional[str] = None) -> Dict[str, Any]:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id or f"call_{next(_CALL_IDS)}", "type": "function",
            "function": {"name": name, "arguments": text}}


def turn(*calls: Dict[str, Any], content: Optional[str] = None, finish: str = "tool_calls") -> Dict[str, Any]:
    return {"message": {"role": "assistant", "content": content, "tool_calls": list(calls)}, "finish_reason": finish}


def final(text: str, finish: str = "stop") -> Dict[str, Any]:
    return {"message": {"role": "assistant", "content": text}, "finish_reason": finish}


class FakeModel:
    """판단 호출(tools 포함)은 순서대로 정해진 응답을, 인용 검증 경로 호출은 원문 문단 선택 JSON을 돌려준다."""

    def __init__(self, decisions: List[Any], *, finals: Optional[List[Any]] = None, fingerprint: str = SERVING,
                 invalid_selections: int = 0, planner: Optional[Dict[str, Any]] = None,
                 failing: Optional[Dict[str, Any]] = None, failing_tools: Optional[Dict[str, Any]] = None,
                 failing_after: Optional[Dict[str, Any]] = None):
        self.planner = planner
        # 모델 ID별 (성공 호출 수, 이후 실패 응답). 조사 도중 장애를 흉내 낸다.
        self.failing_after = dict(failing_after or {})
        self.model_calls: Dict[str, int] = {}
        # 모델 ID별 고정 실패(HTTP 상태 코드 또는 예외). 대체 모델 전환을 검사한다.
        self.failing = dict(failing or {})
        # 도구 판단 호출에서만 실패하는 모델(예: 다른 모델의 도구 이력 형식을 거부).
        self.failing_tools = dict(failing_tools or {})
        self.decisions = list(decisions)
        self.finals = list(finals or [])
        self.fingerprint = fingerprint
        self.invalid_selections = invalid_selections
        self.calls: List[Dict[str, Any]] = []

    def respond(self, item: Any) -> FakeResponse:
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, int):
            return FakeResponse(item, {"error": "fake"})
        return FakeResponse(200, {"choices": [item], "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                                  "system_fingerprint": self.fingerprint, "model": "company-model"})

    def grounded(self, payload: Dict[str, Any]) -> FakeResponse:
        system = str(payload["messages"][0].get("content") or "")
        if self.planner is not None and system.startswith("사용자 질문 전체를 사내 조회 실행 계획 JSON"):
            return self.respond(final(json.dumps(self.planner, ensure_ascii=False)))
        if "근거에 결속된 설명의 문장 보정" in system:
            return self.respond(final(json.dumps({"answers": ["기간 파라미터 0 처리 오류였고 기본값 14로 수정했습니다."]},
                                                 ensure_ascii=False)))
        if "주장과 근거의 의미를 대조" in system:
            return self.respond(final(json.dumps({"supported": [True]})))
        if "[검색 파이프라인 결과]" in system and "selected_issue_ids" not in system:
            # 기존 일반 생성 경로의 시스템 노트. 원문 선택 형식이 아니다.
            return self.respond(final("기존 경로 답변입니다."))
        if "[검색 파이프라인 결과]" in system:
            if self.invalid_selections:
                self.invalid_selections -= 1
                return self.respond(final("자유 서술 답변입니다."))
            shown = json.loads(system.rsplit("[검색 파이프라인 결과]\n", 1)[1])
            issue = shown["issues"][0]
            passages = [item["passage_id"] for item in issue["answer_evidence"]["excerpts"]
                        if str(item.get("field", "")).startswith("note:")] or [
                item["passage_id"] for item in issue["answer_evidence"]["excerpts"]]
            return self.respond(final(json.dumps({
                "selected_issue_ids": [issue["id"]],
                "sections": [["cause", passages[:1], False], ["action", passages[:1], False]],
                "answer_items": [["원인과 해결 방법", passages[:1], False, "기간 파라미터 0 처리 오류였고 기본값 14로 수정했습니다."]],
                "investigate": []}, ensure_ascii=False)))
        return self.respond(self.finals.pop(0) if self.finals else final("근거 목록만 확인했습니다."))

    def __call__(self, model: Dict[str, Any], api_key: str, payload: Dict[str, Any], timeout: Any,
                 stream: bool, think: Any) -> FakeResponse:
        self.calls.append({"model": model["id"], "payload": deepcopy(payload), "tools": bool(payload.get("tools"))})
        if model["id"] in self.failing:
            return self.respond(self.failing[model["id"]])
        self.model_calls[model["id"]] = self.model_calls.get(model["id"], 0) + 1
        if model["id"] in self.failing_after and self.model_calls[model["id"]] > self.failing_after[model["id"]][0]:
            return self.respond(self.failing_after[model["id"]][1])
        if payload.get("tools") and model["id"] in self.failing_tools:
            return self.respond(self.failing_tools[model["id"]])
        if payload.get("tools"):
            if self.planner is not None and [t['function']['name'] for t in payload['tools']] == ['prepare_query']:
                return self.respond(turn(tool_call('prepare_query', {'proposal': self.planner})))
            if not self.decisions:
                return self.respond(final("확인한 근거로 답했습니다."))
            return self.respond(self.decisions.pop(0))
        return self.grounded(payload)

    def decision_calls(self) -> List[Dict[str, Any]]:
        return [call for call in self.calls if call["tools"]]


def raw_issue(issue_id: int, title: str, note: str, *, project: str = "신한투자증권", status: str = "resolved",
              handler: str = "석지원") -> Dict[str, Any]:
    return {"id": issue_id, "project": {"name": project}, "summary": title,
            "status": {"name": status, "label": {"resolved": "해결", "assigned": "할당"}.get(status, status)},
            "handler": {"real_name": handler}, "reporter": {"real_name": "고객"},
            "created_at": "2026-05-02T10:00:00+09:00", "updated_at": "2026-05-04T10:00:00+09:00",
            "description": f"{title} 현상이 발생합니다.",
            "custom_fields": [{"field": {"name": "매체구분"}, "value": "HTS"}],
            "notes": [{"id": issue_id * 10 + 1, "text": note, "reporter": {"real_name": "개발자"},
                       "created_at": "2026-05-03T10:00:00+09:00"}]}


ADX = raw_issue(31350, "[0389] ADX 지표 미표시", "원인: 기간 파라미터 0 처리 오류. 조치: 기본값 14로 수정 후 전달했습니다.")
RSI = raw_issue(31012, "[0412] RSI 지표 계산 오류", "원인: 평균 구간 누락. 조치: 구간 계산을 보완했습니다.")


def chroma_stage(question: str, *issues: Dict[str, Any], constraints: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from backend.sources.mantis_fields import normalize_issue
    ids = [issue["id"] for issue in issues]
    return {"source": "chroma_db", "status": "ok", "documents": [
        {"issue_id": issue["id"], "latest_issue": normalize_issue(issue, evidence_query=question),
         "latest_source": "mantis", "similarity_rank": rank} for rank, issue in enumerate(issues, 1)],
        "meta": {"scanned_issue_ids": ids, "matched_issue_ids": ids, "search_complete": False, "coverage": "top_k",
                 "constraints": constraints or {}}}


def notion_page(page_id: str, title: str, content: str, *, database: str = "기술 문서") -> Dict[str, Any]:
    return {"id": page_id, "title": title, "database_label": database, "status": "", "owner": "",
            "content": content, "properties_text": "", "url": f"https://www.notion.so/{page_id}",
            "match_method": "notion_body_index", "last_edited_time": "2026-05-05T00:00:00Z"}


def notion_stage(*pages: Dict[str, Any], status: str = "ok") -> Dict[str, Any]:
    return {"source": "notion", "status": status, "notion_pages": list(pages),
            "meta": {"search_complete": True, "displayed_count": len(pages), "constraints": {}}}


class FakeSources:
    """도구가 감싸는 원천 함수를 대신한다. 질의별 결과와 받은 조건을 기록한다."""

    def __init__(self, *, mantis: Optional[Dict[str, Any]] = None, notion: Optional[Dict[str, Any]] = None,
                 lookups: Optional[Dict[int, Dict[str, Any]]] = None, question: str = ""):
        self.mantis = mantis or {}
        self.notion = notion or {}
        self.lookups = lookups or {}
        self.question = question
        self.mantis_calls: List[Dict[str, Any]] = []
        self.notion_calls: List[Dict[str, Any]] = []
        self.lookup_calls: List[Dict[str, Any]] = []
        self.content_calls: List[str] = []

    def search_mantis(self, query: str, queries: List[str], limit: int, constraints: Dict[str, Any], *, search: Any):
        self.mantis_calls.append({"query": query, "limit": limit, "constraints": deepcopy(constraints)})
        result = self.mantis.get(query, ())
        if isinstance(result, BaseException):
            raise result
        return chroma_stage(constraints.get("answer_query") or query, *result, constraints=constraints)

    def search_notion(self, query_text: str, limit: int = 5, constraints: Optional[Dict[str, Any]] = None,
                      planned_terms: Optional[List[str]] = None, include_blocks: bool = True,
                      derive_text_constraints: bool = True):
        self.notion_calls.append({"query_text": query_text, "constraints": deepcopy(constraints or {}),
                                  "planned_terms": planned_terms, "include_blocks": include_blocks,
                                  "derive_text_constraints": derive_text_constraints})
        key = next((name for name in self.notion if name in query_text or name in " ".join(planned_terms or [])), None)
        result = self.notion.get(key, ()) if key else ()
        if isinstance(result, BaseException):
            raise result
        return notion_stage(*deepcopy(list(result)))

    def lookup(self, issue_ids: List[int], constraints: Optional[Dict[str, Any]] = None):
        from backend.sources.mantis_fields import normalize_issue
        self.lookup_calls.append({"issue_ids": list(issue_ids), "constraints": deepcopy(constraints or {})})
        found = [self.lookups[i] for i in issue_ids if i in self.lookups and self.lookups[i] is not None]
        missing = [i for i in issue_ids if i not in self.lookups]
        return {"source": "mantis_id_lookup", "status": "ok" if found else "not_found",
                "meta": {"issue_ids": list(issue_ids), "missing_ids": missing, "search_complete": True},
                "issues_list": [normalize_issue(raw, evidence_query=(constraints or {}).get("answer_query")) for raw in found]}

    def page_blocks(self, page_id: str, max_blocks: int = 30) -> str:
        self.content_calls.append(page_id)
        return f"{page_id} 본문"

    def patches(self) -> List[Any]:
        return [
            patch("backend.tools.mantis_tools.search_explanation_queries", side_effect=self.search_mantis),
            patch("backend.tools.mantis_tools.execute_mantis_id_lookup", side_effect=self.lookup),
            patch("backend.tools.mantis_tools.execute_mantis_page_scan", side_effect=AssertionError("page scan")),
            patch("backend.services.retrieval_pipeline._explanation_referenced_issues", return_value=None),
            patch("backend.tools.notion_tools.execute_notion_search", side_effect=self.search_notion),
            patch("backend.tools.notion_tools.execute_notion_body_search",
                  side_effect=lambda query, limit, constraints, **kwargs: self.search_notion(query, limit, constraints)),
            patch("backend.tools.notion_tools.read_api.fetch_notion_page_blocks", side_effect=self.page_blocks),
            patch("backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id",
                  side_effect=lambda issue_id: deepcopy({ADX["id"]: ADX, RSI["id"]: RSI}.get(issue_id))),
        ]


@contextmanager
def agent_environment(*, enabled: bool = True, record: Optional[Dict[str, Any]] = None, save_record: bool = True,
                      company: Optional[Dict[str, Any]] = None, extra_env: Optional[Dict[str, str]] = None,
                      model: Optional[FakeModel] = None, sources: Optional[FakeSources] = None,
                      extra_models: tuple = (), extra_records: Optional[Dict[str, Dict[str, Any]]] = None,
                      ) -> Iterator[Dict[str, Any]]:
    from backend.services.agent import policy
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        env = {"AGENT_MODE_ENABLED": "true" if enabled else "false", "AGENT_ALLOWED_MODEL_IDS": "company-test",
               "AGENT_ALLOWED_ENDPOINT_ORIGINS": "https://company.test", "AGENT_TEST_COMPANY_KEY": "company-key",
               "AGENT_TEST_EXTERNAL_KEY": "external-key", "AGENT_TEST_BACKUP_KEY": "backup-key",
               "MODEL_CIRCUIT_FAILURE_THRESHOLD": "10", **(extra_env or {})}
        stack.enter_context(patch.dict(os.environ, env))
        models = registry(company=company, extra=extra_models)
        stack.enter_context(patch("backend.llm.get_model_registry", return_value=models))
        stack.enter_context(patch("backend.services.chat.generate.get_model_registry", return_value=models))
        stack.enter_context(patch.object(policy, "verification_path", return_value=Path(directory) / "verification.json"))
        if save_record:
            policy.save_verification("company-test", record or verification_record())
        for model_id, extra_record in (extra_records or {}).items():
            policy.save_verification(model_id, extra_record)
        fake = model or FakeModel([])
        stack.enter_context(patch("backend.llm._send_model_chat", side_effect=fake))
        if sources is not None:
            for item in sources.patches():
                stack.enter_context(item)
        from backend.llm import reset_model_circuits
        reset_model_circuits()
        yield {"model": fake, "sources": sources, "directory": directory}


def explain_context(question: str, *, source: str = "mantis", constraints: Optional[Dict[str, Any]] = None,
                    tools: Optional[Dict[str, bool]] = None, previous: Optional[List[str]] = None) -> ChatPipelineContext:
    from backend import llm
    from backend.intent.scope_extraction import empty_query_constraints
    resolved = {**empty_query_constraints(), "filter_clauses": [], **(constraints or {})}
    plan = {"planner": "unified_llm", "operation": "explain", "source": source, "planned": True,
            "resolved_constraints": deepcopy(resolved), "filter_clauses": deepcopy(resolved["filter_clauses"]),
            "terms": [], "grounding_text": question, "limit": None,
            **({"previous_explanation_questions": previous} if previous else {})}
    model = llm.get_private_model("company-test")
    return ChatPipelineContext(question, model, llm.get_model_api_key(model), [{"role": "user", "content": question}],
                               question, {"mantis": True, "notion": True, "vectorDb": True, **(tools or {})}, [],
                               deepcopy(resolved), 5, intent_plan=plan)


def chat_request(question: str, **overrides: Any) -> ChatRequest:
    return ChatRequest(modelId="company-test", messages=[{"role": "user", "content": question}],
                       **{"abTest": False, **overrides})


def tool_messages(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [message for message in payload["messages"] if message.get("role") == "tool"]


def tool_result(message: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(message["content"])


def citations_in(text: str) -> List[str]:
    return re.findall(r"\[(M#\d+|N\d+)\]", text)
