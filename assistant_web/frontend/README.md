# 업무 화면 Bklit 차트

기존 FastAPI와 일반 JavaScript 앱을 유지하고 React는 차트 자리만 소유합니다.
Bklit MIT 원본을 `vendor/bklit/`에 수정 없이 보관했습니다. 출처 커밋과 SHA-256은
`vendor/bklit/manifest.json`에 있습니다. Studio 코드는 사용하지 않습니다.

## 개발·검증

```sh
npm --prefix assistant_web/frontend ci --ignore-scripts
npm --prefix assistant_web/frontend run build
assistant_web/scripts/check.sh tests compile shell ruff mypy audit
```

`check.sh tests`는 원본 해시, 소스/잠금 파일의 빌드 해시, 배포 번들 해시도 오프라인으로 검사합니다.
`compile`은 TypeScript 검사와 실제 재빌드를 비교합니다. `node_modules`가 없으면 잠금 파일로 설치합니다.
번들은 `backend/static/vendor/bklit/`에 저장해 배포하며 운영 서버는 Node나 npm을 실행할 필요가 없습니다.
배포 패키지에서 `node_modules/`를 제외합니다. Tailwind preflight는 없고 유틸리티는 `.bklit-root`에만 적용합니다.
Bklit의 자식 컴포넌트 이름 기반 판별 때문에 esbuild의 `keepNames`를 유지해야 합니다.

공식 원본 갱신은 감사한 체크아웃을 준비한 뒤 `node scripts/vendor-bklit.mjs <체크아웃>`으로 수행합니다.
네트워크에서 자동으로 최신 소스를 받지 않습니다. 원본 수정이 필요하면 별도 래퍼를 사용합니다.

## 데이터와 상호작용

- 분포: 기존 미종료 원본, 지연 우선 → 지연 제외 장기 → 그 외. 고객사/경과 막대 클릭은 기존 필터 이벤트를 사용합니다.
- 신규 추이: 기존 권한·담당 범위 전체에서 등록일 기준 집계. 종료 이슈 포함. 수정일을 등록/종료일로 대신 쓰지 않습니다.
- Brush: 신규 추이 내부 확대에만 적용합니다. 전체 미종료 지표·목록의 의미는 유지하고 화면에 명시합니다. 날짜 입력을 키보드 대안으로 제공합니다.
- 도넛: 기간 신규의 현재 종료/미종료/미상. 불완전한 조회에서는 도넛·비율을 숨기고 최소 건수를 표시합니다.
- 마감 달력: 현재 담당 미종료 이슈의 실제 완료예정일, 오늘부터 28일. 미등록·기간 밖 건수를 분리하고 원문 팝업으로 연결합니다.
- 수치·필터 보기는 기본 HTML을 유지합니다. 번들 로딩·렌더 실패 시 원래 수치를 볼 수 있습니다.
- 화면 갱신 전 React 루트를 해제하고 비동기 로딩의 이전 세대는 마운트하지 않습니다.
