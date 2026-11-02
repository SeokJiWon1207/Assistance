# P0–P6 변경 리뷰와 운영 배포 판정

**판정: 현재 작업 공간을 그대로 운영 배포하는 것은 보류한다.** 기능 회귀 검증은 통과했지만, 실제 배포 패키지와 운영 환경에서 아래 세 가지 배포 전 보완 사항을 확인했다. 운영 코드·설정·DB는 변경하지 않았고 배포하지 않았다.

리뷰 근거는 `assistant_web/runtime/release-readiness-20261102-134709/`에 저장했다. AGENTS.md에 따라 서브에이전트 없이 직접 리뷰했다. 미커밋 변경에는 구조 정리, 모델·필터·설명 변경과 선행 프런트 변경이 함께 포함되어 있어 이 작업 공간을 패키징한 결과도 검증했다.

## 배포 전 보완 사항

### 1. [P1] 로컬 .env 전체 업로드가 정상 운영 Gemini 설정을 교체한다

위치: `assistant_web/deploy/aws/deploy_to_ec2.sh:144` 및 `scripts/commands/AWS_시연서버_재배포.command:27`.

표준 실행 래퍼는 INCLUDE_ENV=1로 배포하고, 업로더는 로컬 `.env` 전체를 새 릴리스의 `.env`로 복사한다. 현재 운영에는 OpenAI 키가 없으며, 운영 Gemini 키로 실행한 실제 짧은 요청은 **HTTP 200**이었다. 현재 로컬의 Gemini 요청은 **402**다. 키 값은 출력하지 않고 비교했으며 **엔드포인트는 같고 키는 다름**을 확인했다.

따라서 기존 방식을 그대로 실행하면 신릴리스가 정상 운영 Gemini 키를 물려받지 못하고 로컬의 실패하는 키를 사용한다. Luna 주 모델은 로컬 OpenAI 키를 가져와 동작할 수 있지만, 사용자가 요청한 Gemini 대체 응답이 실패하는 구성으로 전환된다. 릴리스별 이전 `.env`는 남으므로 키를 영구 삭제하는 상황은 아니며, 문제는 신릴리스의 실제 설정이다.

운영 Gemini 등 기존 비밀 설정을 보존한 릴리스 전용 환경 파일에 OpenAI 키를 선택적으로 반영하고, 그 환경으로 Luna·Gemini를 각각 실제 호출해 검증해야 한다. 현재 운영 OpenAI 키 미등록을 해결하려고 로컬 `.env`를 통째로 복사하는 방법은 피해야 한다. 소스에 비밀값을 추가할 필요는 없다.

근거: `production-readonly.json`, `production-gemini-recheck.json`, `model-setting-comparison.json`, 앞선 실측 `weekly-regression-20261102-112814/readiness.json`.

### 2. [P1] 배포 스크립트가 프로젝트 기준과 다른 Python을 선택한다

위치: `assistant_web/deploy/aws/setup_server.sh:69`.

프로젝트 기준은 `.python-version`의 **3.12.14**, check.sh도 **3.12만 허용**한다. 운영의 `/usr/bin/python3`와 활성 venv는 모두 **3.14.4**이며 `python3.12` 실행 파일은 없다. setup_server.sh는 버전 확인 없이 `python3 -m venv .venv`를 실행하므로 새 릴리스도 검증 기준과 다른 런타임을 사용하게 된다.

이 결과가 모든 기능의 즉시 장애를 증명하는 것은 아니다. 그러나 의존성과 SQLite 정책 변경을 검증한 런타임과 운영 런타임이 다르고, 현재 check.sh를 운영 런타임으로 재현할 수도 없다. 운영에 기준 Python 3.12를 준비하고 배포 스크립트가 그 실행 파일을 명시적으로 선택·검증해야 한다. 새 릴리스 venv를 만드는 방식으로 진행하고 현재 활성 릴리스의 venv를 교체할 필요는 없다.

근거: `production-compatibility.json`, `.python-version`, `scripts/check.sh:22`.

### 3. [P2] 배포 패키지에서 필요한 검증 스크립트가 누락된다

위치: `assistant_web/deploy/aws/package_release.sh:10`.

VERIFICATION_FILES에는 scripts/make_vector_db.py만 있고 **scripts/benchmark_mantis_index.py가 없다**. 실제 package_release.sh로 만든 패키지에서 동일 check.sh를 실행하니 2,780개 중 다음 **2개가 실패**했다.

- `test_no_new_facade_imports_or_patch_targets`: 누락된 스크립트 때문에 구조 기준선 사용처가 사라진 것으로 판정.
- `test_standalone_index_comparison_initializes_catalogs`: 패키지 안의 스크립트 실행이 파일 없음으로 실패.

현재 서버 기동 코드의 즉시 ImportError를 재현한 문제는 아니다. **배포 산출물에서 회귀 검증이 재현되지 않는 문제**다. 원본 패키지 manifest와 실패 로그를 보존한 뒤, 진단용 후보에만 이 파일 하나를 복사하고 동일 check.sh를 다시 실행했다. 테스트 2,780개와 나머지 단계가 모두 통과해 원인을 확인했다. 제품 패키저는 수정하지 않았다.

해당 파일을 릴리스 검증 파일 목록에 포함하고 패키징 테스트에도 이를 고정해야 한다. 예외 목록을 완화하거나 검사를 빼서 해결할 문제가 아니다.

근거: `candidate-check.log`, `candidate-original-manifest.json`, `candidate-diagnostic-check.log`.

## 확인한 정상 항목

- 현재 작업 공간의 새 전체 check.sh: **2,780개 테스트, 커버리지 90%, mypy 249개 파일**, compile·shell·ruff·audit 모두 통과.
- 진단용 패키지에 누락 파일 하나를 추가하면 동일 전체 검증도 통과했다. 원래 패키지의 실패와 구분한다.
- P6의 SQLite 연결·트랜잭션·스키마 소유권, 기존 함수 재노출과 단일 채팅 요청 경계를 검토했다. 전체 Python 의존 그래프의 순환 0개는 구조 테스트로 유지된다.
- 운영 DB user_version은 users **3**, workspace **5**, audit **3**으로 현재 코드와 같다. 새 schema 버전을 올리는 변경이 아니다. 빈/구버전 DB 14개와 rollback·재실행·동시 시작·사용자 격리 테스트는 통과했다.
- 패키지에 storage·chat 단계·query·settings·evidence 등 새 모듈이 들어 있고 backend 해시도 작업 공간과 같다. `.env`·키 파일·서비스 계정·runtime DB는 코드 패키지에 포함되지 않는다.
- 운영 systemd의 실제 설정은 APP_ENV=production, **AUTH_COOKIE_SECURE=true**였다. `.env` 안의 false 값만 보고 쿠키 보안 오류라고 판단하지 않았다.
- 실제 Luna 17문항·19턴, 추가 고객사 해석 3개, 스트리밍은 앞선 현재 코드 실측에서 통과했다. 오늘 등록 미종료 고객사 요약도 1건의 원본 집계와 일치했다.
- 의존성 감사의 기존 예외 5개는 유지된 상태이며 npm 취약점은 0개였다. 예외를 새로 추가하거나 보안 설정을 완화하지 않았다.

## 운영 데이터와 추가 확인 범위

운영 Mantis 전체 저장본은 **2026-10-30 01:32:26 UTC, 31,326건**에 머물러 있다. 로컬 검증은 11/02 수집본 31,328건을 사용했다. 운영 /ready는 200이지만 신선도는 stale이며, /ready 200은 데이터 최신성과 모든 모델의 실제 호출 성공을 보증하지 않는다. 배포 전후 자동 갱신·저장본 수집을 확인하고, 오늘 질문을 운영에서도 원문과 대조해야 한다. 이 지연의 원인이나 P6 도입으로 발생했다는 인과는 이번 읽기 점검으로 확정하지 않았다.

한투 실측은 152초·169초로 180초 요청 예산에 가까웠다. 기능은 통과했지만 지연은 배포 후 관찰 항목이다. 새 배포의 장시간 부하나 전체 수동 브라우저 점검을 이번 리뷰가 대신하지 않는다.

첫 원격 Gemini 점검 스크립트는 context manager가 Response를 반환한다고 잘못 가정해 AttributeError가 발생했다. 이는 제품 오류가 아닌 점검 도우미 오류였다. 응답을 별도로 보관하도록 바로잡아 다시 호출했고 **200 및 본문**을 확인했다. 판정은 후자의 `production-gemini-recheck.json`을 사용했다.

## 재검증과 배포 승인 조건

1. 패키징 누락을 수정하고, 운영 Python 3.12 선택을 고정한다.
2. 운영 비밀 설정을 보존한 후보 환경에 OpenAI 키를 반영한다.
3. 수정된 실제 패키지에서 check.sh를 통과시키고, 운영과 같은 Python/설정으로 기동·DB 사전 점검과 Luna/Gemini 실제 응답을 확인한다.
4. 저장본 신선도와 오늘 질문을 확인한 뒤, 기존 DB 백업·사전 migration 검사·불변 릴리스 전환·health/ready·실제 질문 점검·롤백 절차로 배포한다.

현재 판정은 **배포 보류**다. 이 문서는 리뷰 결과이며 수정·운영 배포 승인을 요청하거나 배포를 실행한 기록이 아니다.

## 후속 수정

코드 수정과 새 실제 패키지의 전체 검증을 완료했다. [수정 결과](2026-11-02-release-fixes.md)에 기록했다. 운영 서버의 Python 3.12 준비·실제 적용은 아직 실행하지 않았다. 위 리뷰 당시의 원본 판정과 증거는 유지한다.
