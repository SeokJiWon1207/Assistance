# AWS 시연 배포

팀원들이 로컬과 같은 웹페이지를 보도록 EC2 한 대에 FastAPI 앱을 올리는 간단한 시연용 구성입니다.

## 1. EC2 준비

권장값:

- Ubuntu 22.04 또는 24.04 LTS
- t3.small 이상 권장
- 보안 그룹 인바운드
  - TCP 22: 본인 IP
  - TCP 80: 팀원 사내 IP 대역 또는 임시로 필요한 범위

중요: EC2에서 회사 모델 API, Mantis API, Notion API에 접근 가능해야 로컬과 같은 답변이 나옵니다.

## 2. 로컬에서 업로드

Assistance 루트에서 실행합니다.

```bash
EC2_HOST=EC2_PUBLIC_IP \
SSH_KEY=~/.ssh/your-key.pem \
INCLUDE_ENV=1 \
./assistant_web/deploy/aws/deploy_to_ec2.sh
```

`INCLUDE_ENV=1`은 로컬 `assistant_web/.env`를 서버로 복사합니다. 시연용으로 같은 데이터 소스를 쓰려면 필요하지만,
토큰이 들어 있으므로 EC2 보안 그룹을 팀원/본인 범위로 제한해 주세요.

## 3. 접속

배포가 끝나면 아래 주소로 접속합니다.

```text
http://EC2_PUBLIC_IP/
```

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

- 이 구성은 시연용입니다. 로그인/HTTPS는 아직 붙이지 않았습니다.
- 공개 인터넷에 오래 열어둘 경우 HTTPS, 인증, IP 제한을 추가해야 합니다.
- 로컬 스냅샷(`data/mantis_chroma_db`)도 함께 업로드하므로 로컬과 유사한 검색 결과가 나옵니다.
