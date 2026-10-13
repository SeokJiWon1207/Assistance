# Chroma 보안 예외 검토

검토일: 2026-10-13 · 대상: `chromadb==1.5.9` · 재검토 기한: 2026-11-12

## 공식 패치 상태

[PyPI](https://pypi.org/project/chromadb/)와 [공식 릴리스](https://github.com/chroma-core/chroma/releases)의 최신 안정 버전은 `1.5.9`다. 아래 보안 공지와 실제 의존성 감사에서 패치 버전이 제공되지 않는 것을 확인했다. 개발 버전은 수정 완료를 입증하는 안정 패치로 취급하지 않는다.

| 예외 | 문제 | 근거 |
| --- | --- | --- |
| `PYSEC-2026-311` | 인증 전 컬렉션 생성 시 원격 모델 코드 실행 | [공식 공지](https://github.com/advisories/GHSA-f4j7-r4q5-qw2c) |
| `PYSEC-2026-3814` | 컬렉션 설정 변경 시 원격 모델 코드 실행 | [공식 공지](https://github.com/advisories/GHSA-36p7-vc44-83pf) |
| `PYSEC-2026-3815` | SimpleRBAC의 자원별 권한 검사 누락 | [공식 공지](https://github.com/advisories/GHSA-xph7-9rjv-w5fr) |
| `PYSEC-2026-3813` | 다른 테넌트 컬렉션의 읽기·변경 허용 | [공식 공지](https://github.com/advisories/GHSA-2wm9-hf6c-p5cr) |

## 프로젝트의 적용 범위와 완화 조건

- `backend/sources/chroma.py`와 `scripts/make_vector_db.py`는 로컬 `PersistentClient`를 사용한다. 배포 서비스는 `uvicorn backend.main:app`을 실행하며 Chroma Python HTTP 서버, `HttpClient`, SimpleRBAC, 외부 테넌트 API를 실행하거나 노출하지 않는다.
- 의미 검색은 설정된 Ollama에서 벡터를 받아 `query_embeddings`로 전달한다. 컬렉션 설정에서 임베딩 함수를 자동 선택하는 `query_texts` 경로를 사용하지 않는다.
- 저장본 구축은 코드에 정의된 `OllamaEmbeddingFunction`을 명시한다. 기존 임베딩 재사용은 저장된 벡터를 읽어 직접 전달한다. 사용자 요청으로 원격 모델 저장소나 `trust_remote_code` 설정을 받지 않는다.
- 로컬 저장본과 서비스 환경 설정은 신뢰한 관리자가 관리해야 한다. 이 검토는 임의로 업로드한 Chroma DB나 공격자가 변경할 수 있는 컬렉션 설정의 안전성을 보장하지 않는다. SDK의 자동 임베딩 위험은 [공식 저장소의 보고](https://github.com/chroma-core/chroma/issues/6717)도 함께 검토했다.
- Chroma를 원격 서버로 전환하거나 `CHROMA_API_IMPL`을 변경하는 구성은 이 예외의 적용 범위 밖이다. 애플리케이션의 인증·조회 범위 검사는 계속 필요하다.

## 감사와 재검토

`pip-audit-ignores.json`의 네 예외는 `chromadb==1.5.9`에 한정되며, 패키지 버전이 달라지거나 고정 버전이 없어지면 감사 도구가 실패한다. 만료 시 실패하는 기존 검사는 유지한다. CI는 매주 감사를 실행한다.

기한까지 안정 패치 출시 여부와 위 사용 조건을 다시 확인한다. 패치가 나오면 호환성과 저장본 검색·구축을 검증한 뒤 업그레이드하고 예외를 삭제한다. HTTP 서버, 자동 임베딩, 외부 컬렉션 설정을 도입할 때에는 기한 전이라도 예외를 재검토해야 한다.

이번 감사에서 패치가 존재하는 `multidict`, `oauthlib`, `PyJWT`, `urllib3`는 각각 `6.9.1`, `4.0.0`, `2.15.0`, `2.8.0`으로 올리고 운영·개발 잠금 파일에 반영한다. Chroma 예외를 적용한 감사 통과는 취약한 라이브러리 자체의 패치 완료를 뜻하지 않는다.
