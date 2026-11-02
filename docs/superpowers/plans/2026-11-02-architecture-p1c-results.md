# pyasn1 보안 업데이트와 P1-C 실행 결과

## 범위와 기준선

사용자 요청에 따라 `pyasn1` 보안 업데이트와 검사를 먼저 수행한 뒤 P1-C를 수행했다. 기존 `codex/architecture-p0` 작업 공간에서 시작했으며, 선행 코드·테스트·잠금 파일·계획 401개 파일의 내용과 해시를 `assistant_web/runtime/architecture-p1c/`에 보관했다. 기존 고객사 검색, 제목/본문 검색, 답변 설명, UI 변경은 보존했다.

보안 업데이트와 구조 변경을 구분해 기록하고 자동 커밋하지 않는다. AGENTS.md에 따라 서브에이전트 없이 수행한다. P2 이후는 이번 범위에 포함하지 않는다.

## 먼저 완료한 보안 업데이트

- `requirements.lock`과 `requirements-dev.lock`에서 `pyasn1==0.6.4`만 `0.6.5`로 변경했다. 다른 의존성 핀과 보안 예외는 그대로다.
- 프로젝트 Python 3.12 가상환경에 `pyasn1==0.6.5`를 설치하고 설치 버전을 확인했다.
- `pip check`: 의존성 충돌 없음.
- **P1-C 구현 착수 전 `check.sh audit` 통과.** Python은 기존 예외 5건을 제외한 알려진 취약점 없음, npm은 취약점 0건이다. 앞 단계에서 검출된 pyasn1 취약점 4건은 더 이상 보고되지 않았다.
- 기존 보안 예외가 있다는 사실을 전체 패키지 취약점 0건으로 바꾸어 표현하지 않는다. 보안 예외 정책을 확대하거나 검사를 생략하지 않았다.

## P1-C 수정과 검증

- `test_search_pipeline.py`의 3곳과 `test_team_question_accuracy.py`의 1곳에서 `backend.understanding.load_audit_sources` patch를 `backend.intent.context.load_audit_sources`로 이전했다. 실제 호출 횟수와 `user_uid` 전달도 확인한다.
- patch만 먼저 옮긴 실행에서 4개 동작 검사가 실패했다. 기존 호환 장치가 소비 모듈의 대체 함수를 무시하고 core의 원래 조회 함수를 선택했기 때문이다. 별도로 구조 예외 2개가 해소됐다는 stale 실패도 확인했다.
- `intent/context.py`의 `_compat_dependency` 정의와 마지막 호출을 삭제하고 `load_audit_sources`를 직접 호출한다. 인수, 반환, uid, A/B variant, 예외/폴백 흐름은 유지한다.
- `intent/core.py`, `understanding.py`의 공개 import 경로는 유지했다. P9까지 필요한 모듈 치환과 재내보내기는 이번에 제거하지 않았다.
- 실제 임시 SQLite에서 같은 감사 ID를 owner → other → 빈 uid → owner 순으로 읽으며 A와 B 모두 소유자에게만 해석 결과를 제공하는지 검사했다. 실패·차단·누락된 B 답변을 A의 출처로 대신 연결하지 않는 검사도 보완했다.
- 저장된 순서에 따른 두 번째/마지막 항목 선택, Notion 제목 보존, 범위를 벗어난 서수 처리를 확인했다. 라우팅·필터 제품 규칙은 변경하지 않아 routing fixture의 기대값을 바꾸지 않았다.

## 고정 입력 전후 비교

외부 네트워크와 `.env`를 차단하는 기존 테스트 runner에서 동일한 합성 감사 DB와 질문 17개를 사용했다. 첫째/둘째/마지막/범위 밖 서수, 모호한 지시어, 단일 이슈, B의 Notion 제목, 다른 사용자, 빈 uid, 잘못된/없는 감사 ID, 독립 질문/명시 조건, B 차단/오류/누락을 포함한다.

**17/17 사례에서 해석된 질문, 읽어 온 출처, 감사 DB 행이 변경 전후 동일했다.** JSON 파일은 바이트 단위로도 일치하고 각 조회가 감사 행을 변경하지 않았음을 확인했다. 모델 요청은 수행하지 않았으며 실제 모델 정확도·응답 시간 개선을 주장하지 않는다.

추가 AST 대조에서 동적 선택문 제거와 호출 이름 변경을 제외한 기존 함수 12개의 본문이 동일했다.

## 구조 검사

| 항목 | P1-C 전 | P1-C 후 |
|---|---:|---:|
| 전체 동적 호환 조회 구현 | 1 | 0 |
| 전체 동적 호환 조회 호출 | 1 | 0 |
| 명시적 런타임 Python 순환 그룹 | 7 | 7 |
| 함수 밖 import 순환 그룹 | 0 | 0 |

P0 기준의 호환 조회 구현 4개·호출 26곳이 P1-A/B/C를 거쳐 모두 제거됐다. `sys.modules`를 사용하는 공개 파사드 자체는 아직 남아 있으며 이는 P9 범위다. 나머지 순환 그룹을 이번 작업에서 해소했다고 주장하지 않는다.

구조 fixture의 역사적 기준선은 유지하고 해소된 예외 6개(파사드 참조 레코드 3, 동적 조회 레코드 3)만 삭제했다. 새 순환 간선·금지 의존·구조 예외는 없다.

## 최종 검증

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,718개 통과**, 커버리지 **90%**.
- compile: 프런트 빌드 산출물 검사와 Python compileall 성공.
- shell, ruff 성공. mypy **177개 소스 파일 성공**.
- audit: Python의 기존 예외 5건 외 알려진 취약점 없음, npm 취약점 0건.
- 공개 import 계약, patch 이전 검사, 사용자별 감사 출처·A/B variant 격리, 제목/본문·고객사 검색을 포함한 기존 전체 회귀 통과.
- `git diff --check` 통과. 이번 작업의 기준선과 비교해 제품 코드는 context.py만 변경됐고, 두 잠금 파일은 pyasn1 핀만 변경됐다.

이 결과로 P1-B에서 보류됐던 보안 관문까지 해소됐으며 P1-A/B/C가 모두 완료됐다. 실제 모델 호출은 수행하지 않았다.

상세 증거는 커밋하지 않는 `assistant_web/runtime/architecture-p1c/`에 보관한다: `before-manifest.json`, `install-pyasn1.log`, `pip-check.log`, `security-first.log`, `patch-migration-red.log`, `before-context.json`, `after-context.json`, `behavior-equivalence.json`, `before-graph.json`, `after-graph.json`, `removed-exceptions.json`, `check-final.log`.

## 검토와 다음 단계

기존 실행·검증 스킬을 적용했다. AGENTS.md에 따라 위임 없이 구현자가 diff, 함수 본문, 실제 DB 회귀와 구조 목록을 직접 검토했으며 별도 리뷰 에이전트의 검토를 받은 것은 아니다. 기존 변경을 포함한 현재 작업 공간을 보존했고 커밋하지 않았다.

다음 구조 개선 단계는 P2-A의 공통 함수 책임 분리다. 이번에는 착수하지 않았다.
