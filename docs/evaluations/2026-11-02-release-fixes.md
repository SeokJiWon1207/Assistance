# 배포 리뷰 3건 수정과 검증

[배포 리뷰](2026-11-02-release-readiness-review.md)의 환경 파일·Python 선택·패키징 문제를 수정했다. **코드와 실제 배포 패키지 검증은 완료했으며, 운영 배포는 하지 않았다. 운영 서버에는 아직 Python 3.12 준비가 필요하다.**

## 변경 내용

| 항목 | 수정 |
|---|---|
| 환경 파일 | `scripts/release_environment.py`가 기존 운영 값을 보존하고 비어 있는 OPENAI_API_KEY만 로컬에서 채운다. 기존 OpenAI·Gemini·Mantis·Notion 키와 보안 설정을 덮어쓰지 않는다. |
| 전송 경계 | 일반 재배포는 OpenAI 키만 든 0600 overlay를 전송하고 서버에서 운영 환경과 합친다. 전체 로컬 .env 업로드를 제거했다. 환경 생성 후 임시 파일을 지운다. |
| 최초 설치 | 기존 환경이 전혀 없을 때만 ENV_BOOTSTRAP_FILE로 검토한 전체 파일을 명시한다. 기존 운영 환경이 있으면 bootstrap을 거절한다. |
| Python | `deploy/aws/select_python.sh`가 python3.12 또는 명시한 DEPLOY_PYTHON을 .python-version의 major.minor와 대조한다. 기본 python3로 폴백하지 않는다. |
| 실패 위치 | 환경 파일 생성 전과 setup_server의 apt/systemd/venv 작업 전에 Python을 확인한다. 3.12가 없거나 잘못된 버전이면 활성 서비스·DB를 전환하지 않는다. |
| 패키징 | benchmark_mantis_index.py를 패키징 목록에 포함하고, 서버 업로드도 패키지의 scripts 디렉터리 전체를 전송하도록 고쳤다. 로컬 포함과 서버 전송 양쪽을 검증한다. |
| 공용 검사 | check.sh의 ruff·mypy 대상에 새 환경 도우미를 추가했다. CI의 단계 호출은 변경하지 않았다. |

환경 도우미는 실제 앱 dotenv의 첫 정의 우선 규칙을 따른다. 기존 줄과 JSON·따옴표·이스케이프를 다시 직렬화하지 않는다. 빈 OpenAI 키의 첫 정의만 대체하고, 다른 키는 기존 원문 그대로 유지한다. 정상 운영 키가 이미 있으면 같은 OpenAI 키라도 로컬 값으로 바꾸지 않는다. 입력과 같은 경로·기존 출력 파일에 대한 덮어쓰기도 거절한다. 값은 로그에 출력하지 않으며 파일 권한은 0600이다.

운영 키 회전이나 다른 설정 변경은 별도로 검토한 운영 환경에 먼저 반영해야 한다. 새 기본 동작이 일반적인 환경 전체 덮어쓰기 기능을 제공하지 않도록 했다.

## 검증 결과

- 변경 전 새 테스트가 실패하는 것을 확인한 뒤 구현했다. ENV helper와 Python 선택기 부재, benchmark 파일 누락을 재현했다.
- 환경·Python 테스트 **13개**를 추가했다. 운영 비밀·보안 설정 보존, 누락 OpenAI 추가, 기존 OpenAI 보존, JSON 원문·중복·빈 값 처리, 최초 설치와 잘못된 bootstrap 거절, 기존 파일 보존, 값 없는 로그, 올바른/틀린/없는 Python과 setup 실패 위치를 확인했다.
- SSH·rsync만 가짜 전송 경계로 둔 배포 테스트에서 실제 env 도우미와 Python 선택기를 실행했다. 운영 Gemini/Mantis 값이 유지되고 OpenAI만 추가되며 임시 overlay는 삭제되는 것을 확인했다.
- 로컬 `assistant_web/scripts/check.sh`: **2,793개 테스트 통과, 커버리지 90%, mypy 250개 소스 파일**.
- 수정한 실제 package_release.sh로 **새 후보를 생성**하고 동일 check.sh를 실행했다. **2,793개 테스트와 compile/TypeScript·Bklit/shell/ruff/mypy/audit 전체 통과**. 앞선 리뷰의 수동 파일 보완은 사용하지 않았다.
- Python audit는 기존 예외 5건을 유지한 상태에서 통과, npm 취약점 0건. 새 보안 예외나 설정 완화는 없다.
- 패키지에 benchmark·env helper·Python 선택기가 모두 포함되고 원본과 해시가 같다. .env·키·서비스 계정·운영 runtime은 코드 패키지에서 제외된다. 검사 실행 중 설치된 node_modules는 원본 코드 패키지에 포함되지 않는다.
- 채팅·검색·집계 제품 코드는 이번 수정에서 바꾸지 않았다. 전체 회귀에서 기존 동작을 계속 검증했다.

원본 기록은 `assistant_web/runtime/release-fixes-20261102-140647/`에 있다. `red.log`, `green.log`, `full-check.log`, `candidate-check.log`, `package-inspection.json`을 보관했다.

## 운영에 남은 실행 단계

현재 운영 서버에서 새 Python 선택기만 stdin으로 실행하는 읽기 전용 확인을 했다. **python3.12가 없다는 메시지와 종료 코드 1**로 끝났고 시스템 패키지·venv·서비스·DB는 변경하지 않았다. `production-runtime-gate.log`에 기록했다.

서버에 Python 3.12와 해당 venv 모듈을 준비한 뒤, 기본 `python3.12` 또는 명시적인 DEPLOY_PYTHON 경로를 사용해야 한다. 현재 3.14.4 서비스의 Python·venv를 덮어쓸 필요는 없다. 버전 검증을 우회하는 옵션은 추가하지 않았다.

그다음 실제 배포에서는 기존 운영 Gemini 키가 보존된 신릴리스 환경으로 Luna·Gemini 응답, 저장본 신선도와 오늘 질문, health/ready 및 롤백을 확인해야 한다. 이번 작업에서 그 운영 적용을 실행하거나 전체 운영 준비가 끝났다고 판정하지 않았다.

커밋·서버 설치·재시작·배포는 수행하지 않았다. 사용자의 선행 변경은 유지했다.
