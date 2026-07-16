---
tags: [ops, deploy]
---

# 배포 — AWS EC2 (시연 서버)

← [[00 Home]] · 관련: [[설정 및 환경변수]]

팀원들이 로컬과 같은 웹페이지를 보도록 EC2 한 대에 FastAPI 앱을 올리는 **시연용** 구성. 로그인/HTTPS는 아직 없음.

## 1. EC2 준비

- Ubuntu 22.04 또는 24.04 LTS, t3.small 이상 권장
- 보안 그룹: TCP 22(본인 IP), TCP 80(팀원 사내 IP 대역 또는 임시 범위)
- **중요**: EC2에서 회사 모델 API, Mantis API, Notion API 접근 가능해야 로컬과 같은 답변이 나옴

## 2. 배포 (Assistance 루트에서 실행)

```bash
EC2_HOST=EC2_PUBLIC_IP \
SSH_KEY=~/.ssh/your-key.pem \
INCLUDE_ENV=1 \
./assistant_web/deploy/aws/deploy_to_ec2.sh
```

`INCLUDE_ENV=1`은 로컬 `.env`를 서버로 복사 (토큰 포함 → 보안 그룹을 팀원/본인 범위로 제한할 것). 로컬 스냅샷(`data/mantis_chroma_db`)도 함께 업로드되어 로컬과 유사한 검색 결과가 나온다.

도메인을 붙일 경우 `SERVER_NAME=ai-demo.example.com` 추가.

## 3. 접속

```
http://EC2_PUBLIC_IP/
```

## 4. 운영 확인

```bash
ssh -i ~/.ssh/your-key.pem ubuntu@EC2_PUBLIC_IP
sudo systemctl status content-ai-assistant
sudo journalctl -u content-ai-assistant -f
sudo systemctl restart content-ai-assistant   # 재시작만
```

## 관련 스크립트

- `assistant_web/deploy/aws/setup_server.sh`
- `assistant_web/deploy/aws/deploy_to_ec2.sh`
- 로컬 편의 스크립트: `assistant_web/deploy/aws/AWS_시연서버_재배포.command`, `assistant_web/scripts/local/AI비서_서버_재시작.command`

## 주의

- 시연용 구성. 공개 인터넷에 오래 열어둘 경우 HTTPS/인증/IP 제한 추가 필요.
