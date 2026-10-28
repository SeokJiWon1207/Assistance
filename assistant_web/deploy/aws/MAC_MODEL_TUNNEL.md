# Mac 로컬 모델 연결

운영 서버에 모델을 실행할 자원이 부족하면 Mac의 Ollama를 SSH 역방향 터널로 연결한다.
EC2의 `127.0.0.1:11434`에서만 열리므로 보안 그룹이나 모델 endpoint 허용 범위를 넓히지 않는다.
회사 모델 연결 오류·시간 초과·회로 차단 시 질문 해석도 활성 Ollama 모델로 자동 전환한다.
해석 결과에는 기존 필터 검증을 동일하게 적용한다. 인증 오류는 자동 전환하지 않는다.

Mac에서 Ollama를 실행하고 `qwen3.5:9b`를 설치한 뒤, 기존 서버 호스트 키가 등록된 상태에서 실행한다.

```bash
EC2_HOST=43.202.45.113 SSH_KEY="$HOME/.ssh/Assistance_Key.pem" \
  bash assistant_web/deploy/aws/install_mac_model_tunnel.sh
```

설치 프로그램은 전용 키 `~/.ssh/assistance_model_tunnel_ed25519`를 만들고 서버에
지정 포트 전달만 허용하는 키를 추가한다. 이 키로 쉘 명령은 실행할 수 없다.
서버의 `GatewayPorts=no` 설정을 검사하고 기존 SSH 설정은 변경하지 않는다.
Mac 로그인 시 `kr.assistance.model-tunnel` LaunchAgent가 실행되며 연결 종료 시 재시도한다.
로그는 `assistant_web/runtime/model-tunnel/`에 저장한다. 개인 키와 로그는 커밋하지 않는다.

Mac이 켜져 있고 로그인된 상태에서 Ollama가 실행 중이어야 한다. 절전·네트워크 단절 중에는
대체 모델을 쓸 수 없다. 로그인 시 Ollama도 실행되도록 Ollama 앱의 시작 설정을 유지한다.
해석 요청은 로컬 모델에 16K 컨텍스트와 최대 60초를 배정하여 긴 지침의 잘림을 막는다.

Notion 목록 검색은 원문으로 검증한 색인 결과를 확보하면 추가 실시간 탐색을 최대 30초로
제한하고 답변 처리 시간을 남긴다. 제한에 도달하면 확인한 문서를 부분 검색으로 반환한다.
전체 건수·그룹 집계에는 이 제한을 적용하지 않으며, 사용자 취소와 채팅 전체 마감은 그대로 처리한다.

상태 확인:

```bash
launchctl print "gui/$(id -u)/kr.assistance.model-tunnel"
# EC2에서: 공개 인터페이스가 아닌 loopback 주소만 보여야 한다.
ss -ltn 'sport = :11434'
curl --fail http://127.0.0.1:11434/api/tags
```

중지하려면 `launchctl bootout "gui/$(id -u)/kr.assistance.model-tunnel"`을 실행한다.
영구 해제는 `~/Library/LaunchAgents/kr.assistance.model-tunnel.plist`를 제거하고,
EC2 `~/.ssh/authorized_keys`에서 `assistance-model-tunnel` 전용 키 줄만 제거한다.
기존 관리자 키와 다른 서비스 키는 보존한다.
