"""
title: Standalone Mantis Tools
author: Assistance
version: 2.0.0
description: OpenWebUI에서 사용자별 인증 정보로 Mantis REST API를 직접 조회합니다.
requirements: requests,pydantic
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from pydantic import BaseModel, Field


class Tools:
    """로컬 백엔드나 파일에 의존하지 않는 Mantis 읽기 전용 도구입니다."""

    # 관리자가 모든 사용자에게 공통으로 적용할 검색 범위와 기본 접속 정보를 설정합니다.
    # 사용자별 UserValves 값이 있으면 아래 기본 주소와 토큰보다 우선합니다.
    class Valves(BaseModel):
        DEFAULT_MANTIS_API_URL: str = Field(
            default="",
            description="공용 Mantis issues API 주소. 예: https://host/mantis/api/rest/issues",
        )
        DEFAULT_MANTIS_API_TOKEN: str = Field(
            default="",
            description="선택적인 공용 Mantis API 토큰",
            json_schema_extra={"input": {"type": "password"}},
        )
        PAGE_SIZE: int = Field(
            default=100,
            ge=1,
            le=100,
            description="Mantis API 페이지당 조회 건수",
        )
        MAX_PAGES: int = Field(
            default=8,
            ge=1,
            le=100,
            description="검색 한 번에 조회할 최대 페이지 수",
        )
        REQUEST_TIMEOUT_SECONDS: int = Field(
            default=30,
            ge=3,
            le=120,
            description="Mantis API 요청 제한 시간(초)",
        )
        VERIFY_SSL: bool = Field(
            default=True,
            description="HTTPS 인증서 검증 여부",
        )

    # 팀원마다 다른 Mantis 주소나 토큰을 사용할 수 있는 개인 설정입니다.
    # OpenWebUI가 각 도구 호출의 __user__["valves"]에 이 객체를 전달합니다.
    class UserValves(BaseModel):
        MANTIS_API_URL: str = Field(
            default="",
            description="개인 Mantis issues API 주소. 비우면 관리자 기본값 사용",
        )
        MANTIS_API_TOKEN: str = Field(
            default="",
            description="개인 Mantis API 토큰. 비우면 관리자 기본값 사용",
            json_schema_extra={"input": {"type": "password"}},
        )

    def __init__(self):
        # OpenWebUI가 저장한 관리자 Valve 값으로 실행 시 덮어씁니다.
        self.valves = self.Valves()

    # 사용자별 Valve 값을 안전하게 읽고 값이 없으면 관리자 기본값으로 폴백합니다.
    def _user_value(self, __user__: Optional[dict], name: str, fallback: Any) -> Any:
        user_valves = (__user__ or {}).get("valves")
        value = getattr(user_valves, name, None) if user_valves is not None else None
        return value if value not in (None, "") else fallback

    # 현재 사용자의 실제 API 주소와 인증 헤더를 조합합니다.
    def _settings(self, __user__: Optional[dict]) -> Tuple[str, Dict[str, str]]:
        api_url = str(
            self._user_value(
                __user__,
                "MANTIS_API_URL",
                self.valves.DEFAULT_MANTIS_API_URL,
            )
            or ""
        ).strip()
        token = str(
            self._user_value(
                __user__,
                "MANTIS_API_TOKEN",
                self.valves.DEFAULT_MANTIS_API_TOKEN,
            )
            or ""
        ).strip()
        if api_url and not api_url.startswith(("http://", "https://")):
            api_url = f"http://{api_url}"
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = token
        return api_url.rstrip("/"), headers

    # Mantis enum 필드가 dict 또는 문자열 어느 형태로 와도 표시 문자열로 바꿉니다.
    @staticmethod
    def _enum_name(value: Any, default: str = "") -> str:
        if isinstance(value, dict):
            return str(value.get("name") or value.get("label") or default)
        return str(value or default)

    # 커스텀 필드 배열에서 이름이 일치하는 값을 찾아냅니다.
    @staticmethod
    def _custom_field(issue: Dict[str, Any], field_name: str) -> str:
        for custom_field in issue.get("custom_fields") or []:
            if str((custom_field.get("field") or {}).get("name") or "") == field_name:
                return str(custom_field.get("value") or "").strip()
        return ""

    # 검색에 사용할 제목·본문·담당자·커스텀 필드·노트 텍스트를 하나로 합칩니다.
    @classmethod
    def _search_text(cls, issue: Dict[str, Any]) -> str:
        custom_text = " ".join(
            f"{(field.get('field') or {}).get('name', '')} {field.get('value', '')}"
            for field in issue.get("custom_fields") or []
        )
        notes_text = " ".join(
            str(note.get("text") or "") for note in issue.get("notes") or []
        )
        return " ".join(
            [
                str((issue.get("project") or {}).get("name") or ""),
                str(issue.get("summary") or ""),
                str(issue.get("description") or ""),
                str(issue.get("steps_to_reproduce") or ""),
                str((issue.get("reporter") or {}).get("real_name") or ""),
                str((issue.get("handler") or {}).get("real_name") or ""),
                cls._enum_name(issue.get("status")),
                custom_text,
                notes_text,
            ]
        ).casefold()

    # API 원본 이슈에서 모델 답변에 필요한 주요 필드만 일관된 구조로 정리합니다.
    @classmethod
    def _record(cls, issue: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "id": issue.get("id"),
            "project": str((issue.get("project") or {}).get("name") or "프로젝트 미상"),
            "summary": str(issue.get("summary") or "제목 없음"),
            "status": cls._enum_name(issue.get("status"), "상태 미상"),
            "priority": cls._enum_name(issue.get("priority")),
            "severity": cls._enum_name(issue.get("severity")),
            "media_type": cls._custom_field(issue, "매체구분") or "기타/미상",
            "screen_number": cls._custom_field(issue, "화면번호") or "미상",
            "reporter": str((issue.get("reporter") or {}).get("real_name") or "미상"),
            "handler": str((issue.get("handler") or {}).get("real_name") or "담당자 미지정"),
            "created_at": str(issue.get("created_at") or "")[:10],
            "updated_at": str(issue.get("updated_at") or "")[:10],
            "description": str(issue.get("description") or "")[:2000],
        }

    # 구조화된 필터와 다중 키워드 AND 조건을 한 이슈에 적용합니다.
    @classmethod
    def _matches(
        cls,
        issue: Dict[str, Any],
        keywords: List[str],
        project: str,
        status: str,
        handler: str,
        reporter: str,
        media_type: str,
        screen_number: str,
        created_from: str,
        created_to: str,
    ) -> bool:
        search_text = cls._search_text(issue)
        if keywords and not all(keyword.casefold() in search_text for keyword in keywords):
            return False
        project_name = str((issue.get("project") or {}).get("name") or "").casefold()
        if project and project.casefold() not in project_name:
            return False
        issue_status = cls._enum_name(issue.get("status")).casefold()
        if status and status.casefold() not in issue_status:
            return False
        issue_handler = str((issue.get("handler") or {}).get("real_name") or "").casefold()
        if handler and handler.casefold() not in issue_handler:
            return False
        issue_reporter = str((issue.get("reporter") or {}).get("real_name") or "").casefold()
        if reporter and reporter.casefold() not in issue_reporter:
            return False
        issue_media = cls._custom_field(issue, "매체구분").casefold()
        if media_type and media_type.casefold() not in issue_media:
            return False
        issue_screen = cls._custom_field(issue, "화면번호").casefold()
        title = str(issue.get("summary") or "")
        if screen_number and not (
            issue_screen == screen_number.casefold()
            or bool(re.search(rf"\[\s*{re.escape(screen_number)}\s*\]", title))
        ):
            return False
        created_at = str(issue.get("created_at") or "")[:10]
        if created_from and created_at and created_at < created_from:
            return False
        if created_to and created_at and created_at > created_to:
            return False
        return True

    # 제목과 커스텀 필드 일치를 본문 일치보다 우선하도록 간단한 관련도 점수를 계산합니다.
    @classmethod
    def _score(cls, issue: Dict[str, Any], keywords: List[str]) -> int:
        title = str(issue.get("summary") or "").casefold()
        custom_text = " ".join(
            str(field.get("value") or "") for field in issue.get("custom_fields") or []
        ).casefold()
        score = 0
        for keyword in keywords:
            normalized = keyword.casefold()
            if normalized in title:
                score += 30
            elif normalized in custom_text:
                score += 20
            elif normalized in cls._search_text(issue):
                score += 10
        return score

    # Mantis REST API를 페이지 단위로 읽어 현재 사용자가 접근 가능한 이슈를 모읍니다.
    def _fetch_pages(self, __user__: Optional[dict]) -> Tuple[List[Dict[str, Any]], int]:
        api_url, headers = self._settings(__user__)
        if not api_url:
            raise ValueError("MANTIS_API_URL이 설정되지 않았습니다.")
        issues: List[Dict[str, Any]] = []
        searched_pages = 0
        for page in range(1, int(self.valves.MAX_PAGES) + 1):
            response = requests.get(
                api_url,
                headers=headers,
                params={"page_size": int(self.valves.PAGE_SIZE), "page": page},
                timeout=int(self.valves.REQUEST_TIMEOUT_SECONDS),
                verify=bool(self.valves.VERIFY_SSL),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Mantis API 오류: HTTP {response.status_code}")
            batch = response.json().get("issues") or []
            searched_pages = page
            if not batch:
                break
            issues.extend(batch)
            if len(batch) < int(self.valves.PAGE_SIZE):
                break
        return issues, searched_pages

    # 자연어 키워드와 구조화 필터로 Mantis 원본 API를 직접 검색합니다.
    def search_mantis(
        self,
        search_keyword: str = "",
        project: str = "",
        status: str = "",
        handler: str = "",
        reporter: str = "",
        media_type: str = "",
        screen_number: str = "",
        created_from: str = "",
        created_to: str = "",
        limit: int = 20,
        __user__: Optional[dict] = None,
    ) -> str:
        """
        Mantis 이슈를 직접 검색합니다. 모델은 사용자 질문에서 가능한 조건을 각각의
        인자로 분리해야 합니다. 검색어 여러 개는 모두 포함되는 AND 조건입니다.

        :param search_keyword: 제목·본문·노트에서 찾을 공백 구분 키워드
        :param project: 고객사 또는 프로젝트명 일부
        :param status: 상태명 일부
        :param handler: 담당자명 일부
        :param reporter: 등록자명 일부
        :param media_type: 매체구분 값(예: HTS, MTS)
        :param screen_number: 화면번호의 정확한 값
        :param created_from: 등록일 시작 YYYY-MM-DD
        :param created_to: 등록일 종료 YYYY-MM-DD
        :param limit: 반환할 최대 이슈 수(1~50)
        """
        safe_limit = max(1, min(int(limit), 50))
        keywords = [value for value in search_keyword.split() if value]
        try:
            issues, searched_pages = self._fetch_pages(__user__)
            matched = [
                issue
                for issue in issues
                if self._matches(
                    issue,
                    keywords,
                    project,
                    status,
                    handler,
                    reporter,
                    media_type,
                    screen_number,
                    created_from,
                    created_to,
                )
            ]
            matched.sort(
                key=lambda issue: (self._score(issue, keywords), int(issue.get("id") or 0)),
                reverse=True,
            )
            result = {
                "status": "ok",
                "meta": {
                    "scanned_count": len(issues),
                    "found_count": len(matched),
                    "displayed_count": min(len(matched), safe_limit),
                    "searched_pages": searched_pages,
                    "note": "검색 범위는 관리자 MAX_PAGES 설정 안의 Mantis 원본 데이터입니다.",
                },
                "issues_list": [self._record(issue) for issue in matched[:safe_limit]],
            }
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)

    # 숫자 이슈 ID로 Mantis 서버의 최신 단건 상세를 직접 조회합니다.
    def get_mantis_issue(self, issue_id: int, __user__: Optional[dict] = None) -> str:
        """
        Mantis 이슈 ID로 최신 상세를 조회합니다.

        :param issue_id: 조회할 숫자 Mantis 이슈 ID
        """
        try:
            api_url, headers = self._settings(__user__)
            if not api_url:
                raise ValueError("MANTIS_API_URL이 설정되지 않았습니다.")
            response = requests.get(
                f"{api_url}/{int(issue_id)}",
                headers=headers,
                timeout=int(self.valves.REQUEST_TIMEOUT_SECONDS),
                verify=bool(self.valves.VERIFY_SSL),
            )
            if response.status_code == 404:
                result = {"status": "not_found", "issue_id": int(issue_id)}
            elif response.status_code >= 400:
                result = {"status": "error", "reason": f"Mantis API 오류: HTTP {response.status_code}"}
            else:
                issues = response.json().get("issues") or []
                result = {
                    "status": "ok" if issues else "not_found",
                    "issue": self._record(issues[0]) if issues else None,
                }
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "reason": str(exc)}
        return json.dumps(result, ensure_ascii=False, indent=2)
