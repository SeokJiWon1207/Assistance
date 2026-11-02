# P2-A 공통 함수 책임 분리 결과

## 범위와 기준선

사용자 요청에 따라 공통 함수의 구현을 역할별 모듈로 옮기고 기존 import 경로와 동작을 유지했다. 기존 `codex/architecture-p0` 작업 공간에서 시작했으며, 선행 코드·테스트·계획 424개 파일과 해시를 `assistant_web/runtime/architecture-p2a/`에 보관했다. 선행 고객사 검색, 제목/본문 검색, 답변 설명, 보안 업데이트와 UI 변경을 보존했다.

AGENTS.md에 따라 서브에이전트 없이 직접 구현·검토했다. 자동 커밋이나 서버 재시작·배포는 하지 않았다. P2-B의 고객사 JSON 초기화 및 Notion 설정·통신 분리는 이번 범위에 포함하지 않는다.

## 변경 내용

`common.py`의 함수 30개와 관련 정규식·표시명 상수를 다음 소유자로 옮겼다. 함수 본문과 서명은 그대로다.

| 책임 | 실제 소유자 | 함수 수 |
|---|---|---:|
| 문자열·따옴표·검색 토큰·조회 표현 | `backend/utils/text.py` | 9 |
| 숫자 변환과 범위 제한 | `backend/utils/numbers.py` | 2 |
| 날짜 파싱·Mantis 날짜 변환 | `backend/utils/dates.py` | 2 |
| 고객사 별칭·프로젝트 소유자 | 기존 `backend/customer_identity.py` | 5 |
| 이름·담당자 검증·조사·사람 매칭 | `backend/domain/people.py` | 5 |
| Chroma 필드·제목·프로젝트·문서 해석 | `backend/sources/chroma_document.py` | 6 |
| Mantis 상태 표시명 | 기존 `backend/mantis_status.py` | 1 |

고객사 책임은 이미 같은 역할을 맡는 `customer_identity.py`에 통합했다. 계획의 기존 모듈 재사용 원칙에 따라 중복 `domain/customers.py`를 만들지 않고 계획 매핑을 갱신했다.

`config.py`의 정적 테이블·정규식 15개는 `backend/settings/values.py`로 옮겼다. 이 모듈은 환경변수·JSON·HTTP 초기화를 하지 않는다. 새 패키지 초기화 파일은 비워 두었다. 고객사 JSON 로딩과 기존 HTTP 함수는 config에 남아 있다.

생산 코드·스크립트 72개 파일의 관련 import를 실제 소유자로 이전했다. 기존 common 소비 backend 65개 파일을 포함하며, backend와 양쪽 scripts의 common import는 0개다. 기존 tests의 common import는 공개 경로와 동작 보존 검사에 사용할 수 있다.

`common.py`는 업무 심볼 44개를 명시적으로 재내보낸다. `config.py`에서도 옮긴 15개 정적 객체를 재내보낸다. 래퍼나 동적 조회를 만들지 않고 동일 객체를 공유하므로 고객사 캐시를 복제하지 않는다. 기존 별칭 사전의 변경과 명시적 `cache_clear()` 의미, 캐시 용량 2,048/512도 유지한다. 이전 경로로 노출되던 표준 라이브러리·typing 이름도 유지했다.

## 동작 보존 검증

구현을 옮기기 전에 함수 30개의 서명과 반환값·반환 타입·예외 타입/문구·입력 set 변경을 **123개 경계 사례**로 고정했다. 윤년과 잘못된 날짜, KST 날짜 경계, 중첩 따옴표, 짧은 별칭, 사람 이름 차단, Chroma 필드 누락, 상태 표시명, 숫자 변환 오류를 포함한다. 새 소유자·import 경계 검사가 이동 전에 실패하는 것을 확인한 후 구현을 옮겼으며, 통과를 위해 기존 기대값을 바꾸지 않았다.

AST 대조에서 옮긴 **30개 함수와 5개 상수 정의**, config의 **15개 정적 정의**가 전과 동일했다. 새 소유자에 이미 존재하던 함수도 모두 동일하다. 별도 소유자 파일을 제외한 기존 생산 코드·스크립트 **71개 파일은 import문 외 AST가 동일**하다. 나머지 한 소비 파일은 기존 소유자이기도 한 `mantis_status.py`다.

`test_common_boundaries.py`에서 동작 계약과 캐시를 보호하고, `test_legacy_import_contracts.py`에서 이전/새 경로의 동일 객체 재내보내기를 확인한다. 순수 모듈을 정방향·역방향으로 각각 새 프로세스에서 import하면서 config/common/environment/HTTP 라이브러리 import와 JSON·.env 읽기를 차단해도 성공한다.

## 고정 문항 전후 비교

기존 `routing_cases.jsonl`의 **310개 고정 문항**을 같은 시각·합성 Mantis/Notion 자료·기록된 모델 제안으로 실행했다. 기존 runner가 외부 네트워크와 `.env`를 차단한다.

- 310개: 조건 추출, 원천 계획, 질의 계획 비교.
- 그중 61개: 기록된 통합 모델 제안의 검증 결과 또는 정확한 거절 예외 비교. 관련 이전 질문 문맥도 동일하게 제공했다.
- 그중 262개: 선택된 출처, 결정형 답변, 근거 검증 결과까지 비교. 나머지 문항은 원천 분류가 일반/복수 원천이므로 이 비교에서는 계획까지 확인했다.

**310/310 문항의 수집 결과가 변경 전후 동일하고 JSON 파일도 바이트 단위로 일치했다.** 조건·ID·문구·출처 순서를 제거해서 맞추지 않았다. 고객사 포함/제외/변경·해제, 제목/본문 문자 검색 등 기존 문항을 유지했다.

이는 저장된 입력에 대한 결정형 동작 동일성 검증이다. 실제 모델이나 운영 원천을 다시 호출하지 않았으므로 실제 모델 정확도·응답 시간 개선을 주장하지 않는다. 기존 전체 회귀는 모델 대체, 제목/본문·고객사 필터, 권한과 답변 근거 검사도 포함한다.

## 구조와 전체 검증

순수 하위 계층 규칙에 settings를 포함하고 common/config 역참조를 금지했다. 기존 구조 예외와 역사적 기준선을 확대하지 않았다. 전체 런타임 Python 순환 그룹은 **7개 유지**, 함수 밖 import 순환은 **0개 유지**다. 동적 호환 조회 구현/호출은 **0/0 유지**이며, P9 대상의 모듈 치환·이름 주입 파사드는 남아 있다. 남은 순환을 이번 단계에서 해결했다고 주장하지 않는다.

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,723개 통과**, 커버리지 **90%**. 변경 전에는 2,718개 통과였다.
- compile: 프런트 빌드 산출물 검사와 Python compileall 성공.
- shell, ruff 성공. mypy **186개 소스 파일 성공**.
- audit: Python의 기존 예외 **5건** 외 알려진 취약점 없음, npm 취약점 **0건**. 예외 정책·잠금 파일을 이번에 바꾸지 않았다.
- `git diff --check` 통과.

상세 증거는 커밋하지 않는 `assistant_web/runtime/architecture-p2a/`에 있다: `before-manifest.json`, `boundary-red.log`, `before-tests.log`, `before-questions.json`, `after-questions.json`, `question-comparison.json`, `ast-equivalence.json`, `consumer-equivalence.json`, `after-graph.json`, `check-final.log`, `completion.json`. 지속적으로 실행되는 경계 사례와 소유권 목록은 tests/fixtures의 `common_cases.json`, `common_ownership.json`에 보관한다.

## 다음 단계

P2-A는 완료했다. 다음 P2-B에서는 고객사 설정 로딩과 Notion 설정 읽기·DB 탐색·HTTP 전송을 분리한다. 기존 초기화 우선순위, 오류 처리, 캐시·잠금·재시도 정책을 각각 고정한 뒤 이전해야 한다. 이번에는 착수하지 않았다.
