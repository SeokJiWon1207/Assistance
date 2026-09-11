# AWS 배포

EC2 한 대에 FastAPI 앱을 올리고 nginx와 Let's Encrypt TLS로 공개하는 구성입니다.

## 1. EC2 준비

권장값:

- Ubuntu 22.04 또는 24.04 LTS
- t3.small 이상 권장
- 보안 그룹 인바운드
  - TCP 22: 본인 IP
  - TCP 80: 인증서 발급·갱신 및 HTTPS 리다이렉트용
  - TCP 443: 실제 사용자 접근 범위

중요: EC2에서 회사 모델 API, Mantis API, Notion API에 접근 가능해야 로컬과 같은 답변이 나옵니다.

## 2. 로컬에서 업로드

Assistance 루트에서 실행합니다.

macOS에서는 `assistant_web/scripts/commands/AWS_시연서버_재배포.command`를 더블클릭해
아래 배포 명령을 실행할 수도 있습니다. 사용자가 직접 실행하는 명령 3개는
[`scripts/commands`](../../scripts/commands/README.md)에서 함께 관리합니다.

```bash
EC2_HOST=EC2_PUBLIC_IP \
SSH_KEY=~/.ssh/your-key.pem \
SERVER_NAME=EC2_PUBLIC_IP \
INCLUDE_ENV=1 \
FIREBASE_CREDENTIALS=~/.config/ai-assistant/firebase-service-account.json \
./assistant_web/deploy/aws/deploy_to_ec2.sh
```

`INCLUDE_ENV=1`은 로컬 `assistant_web/.env`를 권한 600으로 복사합니다. `FIREBASE_CREDENTIALS`는
서비스 계정 JSON을 릴리스 디렉터리 밖 `/var/lib/content-ai-assistant`에 권한 600으로 설치합니다.
배포는 파일 동기화 전에 서버 SQLite DB를 `.backup`하고 무결성을 확인하며,
`assistant_web/runtime/`은 동기화 대상에서 제외합니다.

코드와 검색 스냅샷은 운영 경로를 직접 덮어쓰지 않습니다. 각각
`/opt/content-ai-assistant/releases/<release-id>`와 `snapshots/<release-id>`에 완전히
업로드·검증하고, 의존성 설치·보안 preflight·인증서·nginx 검사까지 통과한 뒤
`current` 심볼릭 링크를 원자적으로 전환합니다. 활성화나 readiness가 실패하면 이전
`current`, systemd, nginx 설정을 자동 복구합니다.

공유 DB 사전 검사는 임시 복제본에서 실행합니다. 실제 활성화 직전에는 점검 화면을
켠 뒤 서비스를 중지하고 DB 전체를 백업합니다. 기존 in-place 설치의 DB는 새 상태
디렉터리에 없는 경우에만 SQLite backup 방식으로 이관합니다. 마이그레이션이나
readiness 실패 시 신규 서비스를 중지한 상태에서 활성화 직전 DB와 코드를 복구합니다.
서비스 중지 또는 DB 복구가 실패하면 자동 재기동하지 않고 복구 파일 경로를 남깁니다.

수동 롤백도 대상 릴리스로 현재 DB 복제본을 열어 보고 호환되지 않으면 링크 전환 전에
중단합니다. 오래된 백업을 자동 덮어써 최신 대화를 잃지 않도록 하기 위한 제한입니다.
호환되지 않는 스키마는 먼저 이전 코드 호환 마이그레이션을 준비하거나, 점검 상태에서
복원 시점 이후 데이터 손실 범위를 확인한 별도 DB 복구 절차가 필요합니다. 배포와 수동
롤백은 같은 호스트 잠금을 사용합니다.

운영 적용 전에는 폐기 가능한 서버에서 기존 DB 이관, 신규 마이그레이션 실패,
readiness 실패, 수동 롤백 거절과 복구를 검증해야 합니다. 로컬 테스트는 이관·복구
함수와 스크립트 순서를 검증하며 실제 systemd/nginx 실행을 대신하지 않습니다.

배포 전 로컬 Chroma 스냅샷에 구조화 보조 인덱스가 있어야 합니다. 전체 임베딩을 다시
계산하지 않고 현재 문서에서 인덱스만 만들려면 다음 명령을 실행합니다.

```bash
assistant_web/.venv/bin/python scripts/make_vector_db.py --rebuild-index-only
```

배포 검증은 `chroma.sqlite3`, `mantis_index.sqlite3`, `freshness.json`의 문서 건수와
SQLite 무결성을 함께 확인합니다. 검증을 통과한 운영 서비스는 기본적으로
`MANTIS_INDEX_MODE=primary`를 사용하고 시작 단계에서 업무 화면 데이터를 미리 읽습니다.
보조 인덱스가 누락되거나 손상되면 애플리케이션은 기존 Chroma 순회로 자동 전환합니다.

구조화 인덱스를 기존 결과와 비교해야 하는 점검 배포에서는 `shadow`를 명시하고
결과 차이를 systemd 로그에서 확인합니다.

```bash
sudo journalctl -u content-ai-assistant | grep 'shadow 결과 불일치'
```

```bash
MANTIS_INDEX_MODE=shadow \
EC2_HOST=EC2_PUBLIC_IP \
SSH_KEY=~/.ssh/your-key.pem \
SERVER_NAME=EC2_PUBLIC_IP \
INCLUDE_ENV=1 \
./assistant_web/deploy/aws/deploy_to_ec2.sh
```

### AWS CLI 권한

SSH 기반 애플리케이션 배포에는 AWS API 자격 증명이 필요하지 않습니다. 보안 그룹을
조회하거나 HTTPS 인바운드 규칙을 관리할 때만 `assistance-deploy` 프로필을 사용합니다.
이 프로필은 `iam-deploy-policy.json`에 정의된 대로 배포 인스턴스의 보안 그룹 하나만
변경할 수 있어야 하며, 루트 계정으로 일상적인 AWS CLI 작업을 하지 않습니다.

```bash
AWS_PROFILE=assistance-deploy aws ec2 describe-security-groups \
  --group-ids sg-09cd9dc0099cb116c
```

독립 AWS 계정에서 IAM Identity Center의 계정 인스턴스는 AWS 계정 접근용 permission
set을 지원하지 않습니다. 조직 인스턴스를 도입하기 전까지는 콘솔 로그인이 없는
최소 권한 IAM 사용자를 사용하고, 액세스 키를 로컬 AWS 프로필에만 저장·정기 교체합니다.

## 3. 접속

배포가 끝나면 아래 주소로 접속합니다.

```text
https://EC2_PUBLIC_IP/
```

IP 주소를 `SERVER_NAME`으로 사용하면 Certbot 5.4+의 단기 IP 인증서를 발급합니다.
도메인을 사용할 때는 먼저 DNS가 EC2를 가리키게 해야 합니다.

도메인을 붙일 경우:

```bash
EC2_HOST=EC2_PUBLIC_IP \
SSH_KEY=~/.ssh/your-key.pem \
SERVER_NAME=ai-demo.example.com \
INCLUDE_ENV=1 \
./assistant_web/deploy/aws/deploy_to_ec2.sh
```

## 4. 운영 확인

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@EC2_PUBLIC_IP
sudo systemctl status content-ai-assistant
sudo journalctl -u content-ai-assistant -f
```

앱만 재시작:

```bash
sudo systemctl restart content-ai-assistant
```

현재 및 보관 릴리스 확인:

```bash
readlink -f /opt/content-ai-assistant/current
find /opt/content-ai-assistant/releases -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort -r
```

수동 롤백:

```bash
cd /opt/content-ai-assistant/current/assistant_web
TARGET_RELEASE=20260811T010203Z ./deploy/aws/rollback_release.sh
```

수동 롤백도 링크 전환 후 `/health`와 `/ready`를 확인하며, 대상 릴리스가 준비되지 않으면
자동으로 원래 릴리스로 복귀합니다. 기본적으로 최근 성공 릴리스 2개를 보존합니다.
최초 전환용 `legacy`는 실제 불변 릴리스가 2개 검증된 뒤 구 in-place 코드·venv·중복
스냅샷과 함께 제거되며, `/var/lib/content-ai-assistant`의 운영 DB는 영향을 받지 않습니다.

## 5. 점검 모드

배포나 서버 작업 중에 사용자가 오류 화면 대신 "서버 점검중입니다." 안내를 보게 합니다.

`assistant_web/scripts/commands/AWS_점검모드.command`를 더블클릭하면 현재 상태를 보여주고
켜기/끄기를 물어봅니다.
켜고 끄는 데 재배포도, 서버 재시작도 필요하지 않습니다.

터미널에서 직접 할 수도 있습니다.

```bash
# 점검 시작
ssh -i KEY ubuntu@EC2_PUBLIC_IP "echo '점검 시작: $(date)' > /opt/content-ai-assistant/maintenance.on"

# 점검 종료
ssh -i KEY ubuntu@EC2_PUBLIC_IP "rm -f /opt/content-ai-assistant/maintenance.on"
```

동작 방식:

- 판단 주체가 앱이 아니라 nginx입니다. 플래그 파일이 있으면 앱으로 요청을 넘기지 않고
  `deploy/aws/maintenance.html`을 503으로 내려줍니다.
- 따라서 재배포로 uvicorn이 완전히 내려가 있어도 502가 아니라 안내가 보입니다.
  점검 안내가 가장 필요한 순간이 바로 앱이 죽어 있는 때입니다.
- nginx가 요청마다 파일 존재를 확인하므로 `nginx -s reload`도 필요 없습니다.
- 플래그는 `$APP_ROOT/maintenance.on`으로, 배포 rsync 대상(`$APP_ROOT/assistant_web`)
  바깥에 있습니다. 배포해도 점검 상태가 지워지거나 켜지지 않습니다.
- `/health`도 점검 중에는 503이 됩니다. 앱 자체의 생존을 확인하려면 서버에서
  `systemctl status content-ai-assistant` 또는 `curl 127.0.0.1:8787/health`를 사용하세요.

## 6. 주의

- 운영 서비스는 HTTPS와 Firebase 인증을 강제하며 `AUTH_COOKIE_SECURE=true`로 실행됩니다.
- 회사 모델이 아직 HTTP라면 `.env`에 정확한 origin만
  `MODEL_ENDPOINT_INSECURE_ALLOWLIST`로 등록하고 만료일
  `MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT`도 함께 지정해야 합니다. 회사 모델에 TLS가
  적용되면 두 예외 값을 제거합니다.
- 배포 후 `/ready`에서 세 SQLite DB, 스냅샷 DB, `structuredIndexDb`와 데이터 신선도를 확인하세요.
- `/ready`의 `security.modelTransport`에는 HTTP 모델 예외 만료일과 잔여 일수가 포함됩니다.
  기본적으로 D-30부터 `warning` 상태와 기동 경고 로그가 발생합니다.
- 로컬 스냅샷(`data/mantis_chroma_db`)도 함께 업로드하므로 로컬과 유사한 검색 결과가 나옵니다.
