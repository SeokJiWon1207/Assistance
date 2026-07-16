"""
title: Standalone Notion Tools
author: Assistance
version: 2.0.0
description: OpenWebUI에서 사용자별 인증 정보로 Notion API를 직접 조회하고 선택적으로 수정합니다.
requirements: requests,pydantic
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field


NOTION_API_BASE = "https://api.notion.com/v1"


class Tools:
    """로컬 백엔드나 파일에 의존하지 않는 Notion API 도구입니다."""

    # 관리자가 공통 API 버전, 조회 범위, 선택적인 공용 인증 정보를 설정합니다.
    class Valves(BaseModel):
        DEFAULT_NOTION_API_TOKEN: str = Field(
            default="",
            description="선택적인 공용 Notion integration token",
            json_schema_extra={"input": {"type": "password"}},
        )
        DEFAULT_NOTION_SOURCE_IDS: str = Field(
            default="",
            description="쉼표로 구분한 공용 data source ID 또는 구버전 database ID",
        )
        NOTION_API_VERSION: str = Field(
            default="2026-03-11",
            description="Notion-Version 헤더. 구버전 database ID는 2022-06-28 사용",
        )
        MAX_PAGES_PER_SOURCE: int = Field(
            default=5,
            ge=1,
            le=100,
            description="데이터 소스마다 조회할 최대 페이지 수",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=30,
            ge=3,
            le=120,
            description="Notion API 요청 제한 시간(초)",
        )
        VERIFY_SSL: bool = Field(
            default=True,
            description="HTTPS 인증서 검증 여부",
        )
        ENABLE_WRITES: bool = Field(
            default=False,
            description="Notion 페이지 생성·수정 허용 여부",
        )

    # 팀원마다 다른 Notion integration과 데이터 소스를 지정할 수 있는 개인 설정입니다.
    class UserValves(BaseModel):
        NOTION_API_TOKEN: str = Field(
            default="",
            description="개인 Notion integration token. 비우면 관리자 기본값 사용",
            json_schema_extra={"input": {"type": "password"}},
        )
        NOTION_SOURCE_IDS: str = Field(
            default="",
            description="쉼표로 구분한 개인 data source ID 또는 구버전 database ID",
        )

    def __init__(self):
        # OpenWebUI가 저장한 관리자 Valve 값으로 실행 시 덮어씁니다.
        self.valves = self.Valves()

    # 사용자별 Valve 값을 읽고 비어 있으면 관리자 기본값으로 폴백합니다.
    def _user_value(self, __user__: Optional[dict], name: str, fallback: Any) -> Any:
        user_valves = (__user__ or {}).get("valves")
        value = getattr(user_valves, name, None) if user_valves is not None else None
        return value if value not in (None, "") else fallback

    # 현재 사용자의 토큰, 허용된 데이터 소스 ID, 공통 요청 헤더를 준비합니다.
    def _settings(
        self,
        __user__: Optional[dict],
    ) -> Tuple[str, List[str], Dict[str, str]]:
        token = str(
            self._user_value(
                __user__,
                "NOTION_API_TOKEN",
                self.valves.DEFAULT_NOTION_API_TOKEN,
            )
            or ""
        ).strip()
        raw_ids = str(
            self._user_value(
                __user__,
                "NOTION_SOURCE_IDS",
                self.valves.DEFAULT_NOTION_SOURCE_IDS,
            )
            or ""
        )
        source_ids = [
            value.strip().replace("-", "")
            for value in re.split(r"[,\n]", raw_ids)
            if value.strip()
        ]
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": str(self.valves.NOTION_API_VERSION).strip(),
            "Content-Type": "application/json",
        }
        return token, list(dict.fromkeys(source_ids)), headers

    # API 버전에 따라 최신 data_sources 경로와 구버전 databases 경로를 선택합니다.
    def _source_path(self, source_id: str, suffix: str = "") -> str:
        family = "data_sources" if str(self.valves.NOTION_API_VERSION) >= "2025-09-03" else "databases"
        return f"/{family}/{source_id}{suffix}"

    # 페이지 생성 시 API 버전에 맞는 parent 객체를 생성합니다.
    def _source_parent(self, source_id: str) -> Dict[str, str]:
        if str(self.valves.NOTION_API_VERSION) >= "2025-09-03":
            return {"type": "data_source_id", "data_source_id": source_id}
        return {"type": "database_id", "database_id": source_id}

    # Notion API 호출과 오류 메시지 정규화를 한곳에서 처리합니다.
    def _request(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        response = requests.request(
            method,
            f"{NOTION_API_BASE}{path}",
            headers=headers,
            json=payload,
            params=params,
            timeout=int(self.valves.REQUEST_TIMEOUT_SECONDS),
            verify=bool(self.valves.VERIFY_SSL),
        )
        if response.status_code >= 400:
            try:
                message = response.json().get("message") or response.json().get("code")
            except ValueError:
                message = ""
            raise RuntimeError(f"Notion API 오류: HTTP {response.status_code} {message or ''}".strip())
        return response.json()

    # title, rich_text, select, people, date 등 주요 속성 형식을 일반 문자열로 바꿉니다.
    @classmethod
    def _property_text(cls, prop: Dict[str, Any]) -> str:
        prop_type = str(prop.get("type") or "")
        value = prop.get(prop_type)
        if prop_type in {"title", "rich_text"}:
            return "".join(str(item.get("plain_text") or "") for item in value or []).strip()
        if prop_type in {"select", "status"}:
            return str((value or {}).get("name") or "")
        if prop_type == "multi_select":
            return ", ".join(str(item.get("name") or "") for item in value or [])
        if prop_type == "people":
            return ", ".join(
                str(user.get("name") or (user.get("person") or {}).get("email") or user.get("id") or "")
                for user in value or []
            )
        if prop_type == "date":
            if not value:
                return ""
            start = str(value.get("start") or "")
            end = str(value.get("end") or "")
            return f"{start} ~ {end}" if end else start
        if prop_type == "checkbox":
            return "true" if value else "false"
        if prop_type == "number":
            return "" if value is None else str(value)
        if prop_type in {"url", "email", "phone_number", "created_time", "last_edited_time"}:
            return str(value or "")
        if prop_type == "relation":
            return ", ".join(str(item.get("id") or "") for item in value or [])
        if prop_type == "formula":
            formula_type = str((value or {}).get("type") or "")
            return cls._property_text({"type": formula_type, formula_type: (value or {}).get(formula_type)})
        return ""

    # 속성 이름 후보를 이용해 제목·상태·담당자·기한 같은 대표 값을 찾습니다.
    @classmethod
    def _named_property(
        cls,
        properties: Dict[str, Dict[str, Any]],
        keywords: List[str],
        preferred_type: str = "",
    ) -> str:
        if preferred_type:
            for prop in properties.values():
                if prop.get("type") == preferred_type:
                    value = cls._property_text(prop)
                    if value:
                        return value
        for name, prop in properties.items():
            normalized_name = name.casefold().replace(" ", "")
            if any(keyword.casefold().replace(" ", "") in normalized_name for keyword in keywords):
                value = cls._property_text(prop)
                if value:
                    return value
        return ""

    # Notion 페이지 객체를 모델이 답변하기 쉬운 공통 결과 구조로 정리합니다.
    @classmethod
    def _record(cls, page: Dict[str, Any], source_id: str) -> Dict[str, Any]:
        properties = page.get("properties") or {}
        property_values = {
            name: value
            for name, prop in properties.items()
            if (value := cls._property_text(prop))
        }
        return {
            "id": page.get("id"),
            "source_id": source_id,
            "title": cls._named_property(properties, ["제목", "이름", "name"], "title") or "제목 없음",
            "status": cls._named_property(properties, ["상태", "status", "진행"]),
            "owner": cls._named_property(properties, ["담당", "owner", "assignee"]),
            "due_date": cls._named_property(properties, ["기한", "일정", "날짜", "due", "date"]),
            "created_time": str(page.get("created_time") or "")[:19],
            "last_edited_time": str(page.get("last_edited_time") or "")[:19],
            "url": page.get("url"),
            "properties": property_values,
        }

    # 문단·제목·목록 등 주요 블록에서 사람이 읽을 수 있는 텍스트를 추출합니다.
    @staticmethod
    def _block_text(block: Dict[str, Any]) -> str:
        block_type = str(block.get("type") or "")
        value = block.get(block_type) or {}
        if block_type in {
            "paragraph",
            "heading_1",
            "heading_2",
            "heading_3",
            "bulleted_list_item",
            "numbered_list_item",
            "to_do",
            "toggle",
            "quote",
            "callout",
        }:
            return "".join(str(item.get("plain_text") or "") for item in value.get("rich_text") or [])
        if block_type == "child_page":
            return str(value.get("title") or "")
        return ""

    # 데이터 소스 하나를 커서 기반으로 페이지네이션하여 페이지 목록을 가져옵니다.
    def _query_source(
        self,
        source_id: str,
        headers: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        pages: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(int(self.valves.MAX_PAGES_PER_SOURCE)):
            payload: Dict[str, Any] = {
                "page_size": 100,
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
            }
            if cursor:
                payload["start_cursor"] = cursor
            data = self._request(
                "POST",
                self._source_path(source_id, "/query"),
                headers,
                payload=payload,
            )
            pages.extend(item for item in data.get("results") or [] if item.get("object") == "page")
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor:
                break
        return pages

    # 토큰과 데이터 소스 ID가 설정되어 있고 첫 데이터 소스에 접근 가능한지 확인합니다.
    def get_notion_status(self, __user__: Optional[dict] = None) -> str:
        """현재 사용자의 Notion 연결 설정과 첫 데이터 소스 접근 여부를 확인합니다."""
        try:
            token, source_ids, headers = self._settings(__user__)
            if not token or not source_ids:
                raise ValueError("NOTION_API_TOKEN과 NOTION_SOURCE_IDS가 필요합니다.")
            source = self._request("GET", self._source_path(source_ids[0]), headers)
            result = {
                "status": "ok",
                "api_version": self.valves.NOTION_API_VERSION,
                "configured_source_count": len(source_ids),
                "first_source_id": source.get("id") or source_ids[0],
                "first_source_object": source.get("object"),
            }
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    # 설정된 모든 데이터 소스의 속성을 직접 검색하고 관련도·수정일 순으로 정렬합니다.
    def search_notion(
        self,
        search_keyword: str = "",
        status: str = "",
        owner: str = "",
        source_id: str = "",
        limit: int = 10,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Notion data source 또는 구버전 database를 직접 검색합니다. 키워드는 속성값 전체에
        AND 조건으로 적용되며 상태와 담당자는 별도 필터로 사용할 수 있습니다.

        :param search_keyword: 공백으로 구분한 검색 키워드
        :param status: 상태 속성값 일부
        :param owner: 담당자 속성값 일부
        :param source_id: 특정 data source ID. 비우면 사용자에게 설정된 전체 소스
        :param limit: 반환할 최대 페이지 수(1~50)
        """
        safe_limit = max(1, min(int(limit), 50))
        keywords = [value.casefold() for value in search_keyword.split() if value]
        try:
            token, configured_ids, headers = self._settings(__user__)
            if not token or not configured_ids:
                raise ValueError("NOTION_API_TOKEN과 NOTION_SOURCE_IDS가 필요합니다.")
            normalized_source = source_id.strip().replace("-", "")
            if normalized_source and normalized_source not in configured_ids:
                raise ValueError("source_id가 사용자의 NOTION_SOURCE_IDS 허용 목록에 없습니다.")
            target_ids = [normalized_source] if normalized_source else configured_ids
            records: List[Dict[str, Any]] = []
            scanned_count = 0
            for target_id in target_ids:
                pages = self._query_source(target_id, headers)
                scanned_count += len(pages)
                for page in pages:
                    record = self._record(page, target_id)
                    searchable = " ".join(str(value) for value in record.get("properties", {}).values()).casefold()
                    if keywords and not all(keyword in searchable for keyword in keywords):
                        continue
                    if status and status.casefold() not in str(record.get("status") or "").casefold():
                        continue
                    if owner and owner.casefold() not in str(record.get("owner") or "").casefold():
                        continue
                    record["match_score"] = sum(
                        3 if keyword in str(record.get("title") or "").casefold() else 1
                        for keyword in keywords
                        if keyword in searchable
                    )
                    records.append(record)
            records.sort(
                key=lambda item: (item.get("match_score", 0), item.get("last_edited_time", "")),
                reverse=True,
            )
            result = {
                "status": "ok",
                "meta": {
                    "scanned_count": scanned_count,
                    "found_count": len(records),
                    "displayed_count": min(len(records), safe_limit),
                    "source_count": len(target_ids),
                },
                "notion_pages": records[:safe_limit],
            }
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    # page_id로 최신 속성과 본문 블록을 직접 가져옵니다.
    def get_notion_page(self, page_id: str, __user__: Optional[dict] = None) -> str:
        """
        Notion 페이지 ID로 속성과 본문을 조회합니다.

        :param page_id: 조회할 Notion page UUID
        """
        try:
            token, _, headers = self._settings(__user__)
            if not token:
                raise ValueError("NOTION_API_TOKEN이 필요합니다.")
            safe_page_id = page_id.strip()
            if not safe_page_id or "/" in safe_page_id:
                raise ValueError("올바른 page_id가 필요합니다.")
            page = self._request("GET", f"/pages/{safe_page_id}", headers)
            parent = page.get("parent") or {}
            source_id = str(parent.get("data_source_id") or parent.get("database_id") or "")
            blocks_data = self._request(
                "GET",
                f"/blocks/{safe_page_id}/children",
                headers,
                params={"page_size": 100},
            )
            content = "\n".join(
                text
                for text in (self._block_text(block) for block in blocks_data.get("results") or [])
                if text
            )
            result = {
                "status": "ok",
                "page": {**self._record(page, source_id), "content": content},
            }
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    # JSON으로 지정한 속성과 제목을 이용해 새 페이지를 생성합니다.
    # 관리자가 쓰기를 켜고 사용자가 내용을 승인한 경우에만 confirmed=true를 허용합니다.
    def create_notion_page(
        self,
        source_id: str,
        title: str,
        title_property: str = "Name",
        properties_json: str = "{}",
        content: str = "",
        confirmed: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Notion 페이지를 생성합니다. 먼저 사용자에게 source_id, 제목, 속성, 본문을 보여주고
        명시적으로 승인받은 뒤 confirmed=true로 호출해야 합니다.

        :param source_id: 사용자 NOTION_SOURCE_IDS에 등록된 대상 ID
        :param title: 새 페이지 제목
        :param title_property: 대상 스키마의 title 속성명
        :param properties_json: 추가 Notion 속성 payload JSON 객체
        :param content: 선택적인 페이지 본문
        :param confirmed: 사용자가 표시된 변경을 명시적으로 승인했는지 여부
        """
        if not self.valves.ENABLE_WRITES:
            return json.dumps({"status": "blocked", "reason": "Notion 쓰기가 비활성화되어 있습니다."}, ensure_ascii=False)
        if not confirmed:
            return json.dumps(
                {
                    "status": "confirmation_required",
                    "preview": {"source_id": source_id, "title": title, "properties_json": properties_json, "content": content},
                },
                ensure_ascii=False,
                indent=2,
            )
        try:
            token, configured_ids, headers = self._settings(__user__)
            normalized_source = source_id.strip().replace("-", "")
            if not token or normalized_source not in configured_ids:
                raise ValueError("토큰이 없거나 source_id가 사용자 허용 목록에 없습니다.")
            extra_properties = json.loads(properties_json or "{}")
            if not isinstance(extra_properties, dict):
                raise ValueError("properties_json은 JSON 객체여야 합니다.")
            properties = {
                title_property: {"title": [{"type": "text", "text": {"content": title}}]},
                **extra_properties,
            }
            payload: Dict[str, Any] = {
                "parent": self._source_parent(normalized_source),
                "properties": properties,
            }
            if content.strip():
                payload["children"] = [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": content[:2000]}}]},
                    }
                ]
            page = self._request("POST", "/pages", headers, payload=payload)
            result = {"status": "ok", "page": self._record(page, normalized_source)}
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    # 페이지 ID와 정확한 Notion 속성 payload를 받아 기존 페이지를 수정합니다.
    def update_notion_page(
        self,
        page_id: str,
        properties_json: str,
        confirmed: bool = False,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Notion 페이지 속성을 수정합니다. get_notion_page로 대상을 확인하고 변경 JSON을
        사용자에게 보여준 뒤 명시적으로 승인받아 confirmed=true로 호출해야 합니다.

        :param page_id: 수정할 Notion page UUID
        :param properties_json: Notion API 형식의 변경 properties JSON 객체
        :param confirmed: 사용자가 표시된 변경을 명시적으로 승인했는지 여부
        """
        if not self.valves.ENABLE_WRITES:
            return json.dumps({"status": "blocked", "reason": "Notion 쓰기가 비활성화되어 있습니다."}, ensure_ascii=False)
        if not confirmed:
            return json.dumps(
                {"status": "confirmation_required", "preview": {"page_id": page_id, "properties_json": properties_json}},
                ensure_ascii=False,
                indent=2,
            )
        try:
            token, _, headers = self._settings(__user__)
            safe_page_id = page_id.strip()
            if not token or not safe_page_id or "/" in safe_page_id:
                raise ValueError("토큰과 올바른 page_id가 필요합니다.")
            properties = json.loads(properties_json or "{}")
            if not isinstance(properties, dict) or not properties:
                raise ValueError("properties_json에 하나 이상의 변경 속성이 필요합니다.")
            page = self._request("PATCH", f"/pages/{safe_page_id}", headers, payload={"properties": properties})
            parent = page.get("parent") or {}
            source_id = str(parent.get("data_source_id") or parent.get("database_id") or "")
            result = {"status": "ok", "page": self._record(page, source_id)}
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)
