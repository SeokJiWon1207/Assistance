# Assistance

컨텐츠서비스 AI 비서 프로젝트의 최상위 안내입니다.

```text
Assistance/
├── assistant_web/       # 웹 애플리케이션, 설정, 테스트, 배포 도구
├── data/                # 현재 벡터 DB와 백업
├── scripts/             # 프로젝트 단위 데이터 관리 스크립트
└── ObsidianNotes/       # 프로젝트 문서
```

애플리케이션 실행과 설정 방법은 [`assistant_web/README.md`](./assistant_web/README.md)를 참고하세요.

주요 명령:

```bash
cd assistant_web
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8787 --reload

cd ..
python3 scripts/make_vector_db.py --refresh-days 7

cd assistant_web
python3 -m unittest discover -s tests -p 'test_*.py'
```
