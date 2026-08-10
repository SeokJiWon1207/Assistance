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

`AWS_점검모드.command`를 더블클릭하면 현재 상태를 보여주고 켜기/끄기를 물어봅니다.
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
- 배포 후 `/ready`에서 세 SQLite DB, 스냅샷 DB와 데이터 신선도를 확인하세요.
- `/ready`의 `security.modelTransport`에는 HTTP 모델 예외 만료일과 잔여 일수가 포함됩니다.
  기본적으로 D-30부터 `warning` 상태와 기동 경고 로그가 발생합니다.
- 로컬 스냅샷(`data/mantis_chroma_db`)도 함께 업로드하므로 로컬과 유사한 검색 결과가 나옵니다.
