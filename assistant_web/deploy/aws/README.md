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

## 5. 주의

- 이 구성은 시연용입니다. 로그인/HTTPS는 아직 붙이지 않았습니다.
- 공개 인터넷에 오래 열어둘 경우 HTTPS, 인증, IP 제한을 추가해야 합니다.
- 로컬 스냅샷(`data/mantis_chroma_db`)도 함께 업로드하므로 로컬과 유사한 검색 결과가 나옵니다.
