# Runtime

실행 중 생성되는 로컬 데이터가 저장됩니다.

- `assistant_audit.sqlite3`: 감사·검증 실행 이력
- `accuracy_reports/`: 정확도 검사 JSON/Markdown 리포트와 로그
- `notion_read_scope.json`: 마지막으로 완전히 확인한 Notion 하위 DB 범위
- `notion_title_index.json`: Notion 제목 검색 기준선
- `notion_database_roles.json`: 제목·스키마로 생성한 Notion DB 역할 카탈로그

이 README를 제외한 런타임 생성 파일은 모두 `.gitignore`에서 제외됩니다.
