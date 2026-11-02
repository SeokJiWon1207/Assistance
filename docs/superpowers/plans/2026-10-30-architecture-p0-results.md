# P0 실행 기록

작업 범위: 기준선 확보, 한시적 import 호환 검사 분리, 새 구조 위반 방지 검사. P1 이후 제품 리팩터링은 시작하지 않았다.

## 기준선

- 기준 커밋: `8c9f70109a4aba67e2148f3e3e1fb358814afe47`.
- 작업 브랜치: `codex/architecture-p0`. 기존 미커밋 사용자 변경을 유지한 현재 작업 트리에서 실행했다.
- Python 3.12.14, Node v26.5.0. 인수 시점 소스 파일 653개의 SHA-256을 기록했다.
- 원래 테스트 2,630개 통과, 전체 커버리지 90%. compile/shell/ruff/mypy 통과.
- 첫 audit는 샌드박스의 PyPI DNS 조회 차단으로 실패했다. 권한을 확대한 `check.sh audit` 재실행은 기존 보안 예외를 유지한 상태로 통과했다. 보안 예외나 정책을 변경하지 않았다.
- 파일별 해시·원래 상태·전체 실행 로그는 Git에서 제외된 `assistant_web/runtime/architecture-p0/`에 보관한다. 이 디렉터리가 없어도 커밋 대상 테스트는 fixture만으로 실행된다.
- 기준선 해시의 요약과 환경 정보는 `assistant_web/tests/fixtures/architecture_migration.json`에도 기록했다.

## 구현한 변경

| 파일 | 역할 |
|---|---|
| tests/test_architecture_contracts.py | 기존 query plan/Notion 답변 golden, 채팅 단계, DOM/API 검사를 유지한다. golden 검사는 실제 구현 경로를 참조한다. |
| tests/test_legacy_import_contracts.py | 옛 공개 심볼과 재내보내기 소유 모듈에 관한 기존 검사 5개를 그대로 이전했다. |
| tests/architecture_graph.py | 생산 코드를 실행하지 않는 AST 분석, SCC 계산, 경계 위반·파사드·동적 연결 목록, 이전 예외 차이 검사를 제공한다. |
| tests/test_architecture_dependency_graph.py | 분석기 경계 사례와 실제 저장소의 구조 계약을 검사한다. |
| tests/fixtures/architecture_migration.json | 정확한 이전 예외, 담당 단계, 제거 조건, 과거 기준선 및 저장소 밖 사용 여부 확인 조건을 보관한다. |

이 표의 `tests/`는 `assistant_web/tests/`를 뜻한다. 기존 테스트의 기대 답변과 routing fixture는 수정하지 않았다. 인수한 파일의 해시를 다시 비교해 기존 파일 중 이번에 변경한 것은 `test_architecture_contracts.py`뿐임을 확인했다.

## 자동으로 막는 변경

1. 함수 밖 runtime import에 새 순환이 생기는 경우.
2. 함수 내부 import를 포함한 순환에 새 간선·호출 위치·중복 import가 추가되는 경우. 기존 SCC 안의 추가 간선도 검사한다.
3. intent/reports/sources 및 새 하위 모듈이 금지된 상위 계층을 참조하는 경우.
4. 생산 코드·일반 테스트·실행 스크립트·설정에 옛 파사드 import 또는 문자열 참조가 늘어나는 경우.
5. sys.modules 접근, 모듈 치환, globals 이름 주입, 동적 import, compat 함수 정의/호출이 증가하는 경우.
6. 해소된 예외를 fixture에 계속 남겨 두는 경우. 이전 작업에서 예외를 제거해야 이후 재등장도 검출된다.

줄 번호만 이동하는 것은 허용한다. 식별자는 소스/함수 범위/종류/대상이며 중복 횟수도 비교한다. 제거 조건의 설명은 공유하되 각 예외에 단계와 조건 ID를 연결했다. 자동 baseline 갱신 명령은 추가하지 않았다.

문서 언급은 조사 목록에만 기록한다. 정책 검사 자체의 가상 입력과 fixture 자기 참조는 정확한 파일 경로로 제외한다. runtime·캐시·vendor·다른 worktree·서비스 계정은 소스 조사에 포함하지 않는다. 임의로 계산한 모듈 이름과 모든 런타임 재바인딩까지 증명하는 분석기는 아니므로, 동적 연결 목록의 수동 검토도 유지한다.

## 재확인한 범위

- Python runtime SCC: 8개, 크기 29/4/4/4/2/2/2/2. 함수 밖 명시적 import의 SCC는 0개.
- compat 구현 4개·호출 26곳. sys.modules 접근은 조회 4곳과 모듈 치환 3곳을 함께 기록한다.
- 기존 금지 방향 21개 고유 참조 항목, 순환 import 168개 고유 항목에 담당 단계를 지정했다.
- 파사드 참조 260개 고유 항목을 기록했다. 각 항목의 count가 실제 반복 횟수를 보존한다.
- retrieval 관련 Python 문자열 참조 44곳은 patch 이전 후보를 포함하는 보수적 목록이다. 전부 실제 patch 호출이라고 표현하지 않는다.
- 저장소 밖 소비자는 확인되지 않았으므로 P9의 외부 사용 확인 조건을 유지했다.

## 검증

- 새 분석기 부재로 테스트가 실패하는 것을 확인한 뒤 구현했다.
- 패키지의 `from . import child`를 자기 순환으로 오판하는 사례를 RED→GREEN으로 검증했다.
- TYPE_CHECKING 별칭이 인수/대입으로 가려지는 사례와 다른 worktree·캐시·자격 증명 제외 사례를 각각 RED→GREEN으로 검증했다.
- 구조 검사 21개가 통과했다. 기존 동작 테스트 5개를 호환 파일로 옮겼으므로 기존 검사 수는 줄지 않았다.
- 자체 검토 보완 후 전체 `assistant_web/scripts/check.sh` 결과는 최종 실행 로그에서 확인하고 아래에 기록했다.

최종 전체 검사: `assistant_web/scripts/check.sh` 종료 코드 0. 테스트 2,651개 통과(46.135초), 커버리지 90%, compile/shell/ruff/mypy/audit 모두 통과. Python 감사는 기존 5개 예외를 적용했고 npm 감사는 취약점 0개였다. 전체 로그: `assistant_web/runtime/architecture-p0/final-check-reviewed.log`.

## 다음 작업과 경계

다음 제품 코드 작업은 P1-A의 Notion patch 소비 지점 이전이다. 이번 P0에서는 동적 조회나 파사드 구현 자체를 삭제하지 않았다.

전체 계획의 실제 회사 모델 비교용 `assistant_web/runtime/accuracy_benchmark/cases.jsonl`은 현재 없다. 이번에는 결정형 기준선과 구조 검사까지 실행했다. P1의 제품 코드 변경 전에 고정 문항·원천·모델 기준선을 준비해야 하며, 실제 원천 정확도 검증을 수행한 것으로 표시하지 않는다.

사용자 지침에 따라 서브에이전트 없이 메인 에이전트가 구현과 자체 검토를 수행했다. 기준선에 사용자 미커밋 변경이 있어 clean HEAD worktree로 옮기지 않고 별도 브랜치에서 현재 작업 트리를 보존했다. Git 커밋과 배포는 수행하지 않았다.
