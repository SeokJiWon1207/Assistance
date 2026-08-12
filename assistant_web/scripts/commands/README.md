# 사용자 실행 명령

macOS Finder에서 직접 실행하는 운영 명령을 이 폴더에서 함께 관리합니다.

| 파일 | 용도 | 대상 |
| --- | --- | --- |
| `AI비서_서버_재시작.command` | 개발용 Uvicorn 서버를 안전하게 재시작 | 로컬 |
| `AWS_시연서버_재배포.command` | 현재 코드·설정·검색 스냅샷을 원자적으로 배포 | AWS 시연 서버 |
| `AWS_점검모드.command` | nginx 점검 안내 화면을 켜거나 끄기 | AWS 시연 서버 |

세 명령 모두 프로젝트 안의 실제 구현 경로를 스스로 계산하므로 프로젝트 폴더 전체를 다른
위치로 옮겨도 사용할 수 있습니다. AWS 명령은 기본적으로
`~/.ssh/Assistance_Key.pem`을 사용하며, 다른 키는 `ASSISTANCE_SSH_KEY` 환경변수로 지정합니다.

터미널에서 실행 권한 오류가 나면 프로젝트 루트에서 다음 명령을 한 번 실행합니다.

```bash
chmod +x assistant_web/scripts/commands/*.command
```
