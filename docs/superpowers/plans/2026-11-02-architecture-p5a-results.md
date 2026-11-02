# P5-A 근거·리포트 책임 분리 결과

## 범위와 기준선

P4 다음 단계인 P5-A를 완료했다. 변경 전 코드·테스트·fixture·계획 531개 파일과 해시를 `assistant_web/runtime/architecture-p5a/`에 보관했다. 선행 미커밋 변경을 포함한 현재 작업 공간을 기준으로 비교했다. 별도 프로세스에서 이전 코드를 재생하기 위해 이번 작업에서 수정하지 않은 설정 JSON 9개도 복사하고 해시를 기록했다. `.env`나 운영 DB는 복사하지 않았다.

변경 전 전체 테스트는 **2,751개 통과**, 커버리지 **90%**였다. AGENTS.md에 따라 메인 에이전트가 직접 구현·검토했다. P5-B/C는 진행하지 않았으며 커밋·서버 재시작·배포는 수행하지 않았다.

## 책임과 의존 방향

| 소유자 | 책임 |
|---|---|
| `evidence/excerpts.py` | 민감한 줄 제거, 동일 이슈·필드 발췌, 공유 패키지와 다른 이슈의 문맥 구분 |
| `evidence/stages.py` | 단계별 완료·예정·실패 판정과 QC·전달 근거 규칙 |
| `evidence/references.py` | 출처 라벨 이스케이프, 노트·필드 참조와 정렬 |
| `evidence/quote_context.py` | 인용 대상 이슈 색인, 원문 문단 복원과 문맥 길이 계약 |
| `evidence/retrieval_rows.py` | 기존 검색 결과의 중복 제거·제외·실시간 우선순위·집계 |
| `evidence/model_context.py` | 조회된 결과에서 모델 입력과 검증 출처 구성 |
| `evaluation/explanation_quotes.py` | 모델이 선택한 payload와 원문 인용 검증 |
| `reports/quote_rendering.py`, `quote_selection.py` | 검증된 인용의 표시와 문단 선택 |
| `reports/quote_conclusions.py`, `quote_cases.py`, `direct_conclusion.py` | 사례 판정, 질문별 근거 선택, 출처와 확인 한계 표시 |
| `reports/comparison_common.py` | 원인·조치·미확정 근거의 공통 규칙과 표시 |
| `reports/comparison_history.py` | 실패한 시도·전달·추정적 완료와 처리 이력 |
| `reports/constraint_display.py` | 적용된 검색 조건 표시 |
| 기존 `effort_fields.py` | 공수 필드 이름과 값 파싱 |

계획의 목적지를 보완해 인용 문맥, 문단 선택, 직접 결론, 사례 판정을 각각 분리했다. 검증 모듈을 다시 참조하지 않고 렌더링 의존도 한 방향으로 유지하기 위한 분리다.

1,081줄이던 `_conclusion_lines`는 출처별 묶기 → 사례 후보 수집 → 후보 선택 → 원인·조치 보완 → 시간순 정렬 → 출력으로 나눴다. 노트 판정은 기술 범위, 원인·동작, 구현·QC, 후속 조사, 전달·고객 회신으로 분리했다. 기존 판정 순서, 가장 이른/늦은 근거 선택, 문장과 확인 한계를 유지했다. 오케스트레이션 함수는 9줄이며 각 사례 판정 함수는 180줄 이하이다.

`similar`와 비교 모듈이 공유하는 규칙은 검색·모델 호출을 하지 않는다. `render_facts`도 공통 소유자로 옮겼고, 처리 이력 표시는 `comparison_history`에 두어 역참조를 막았다. 집계의 행 정리와 모델 문맥 구성은 이미 조회된 결과만 사용한다. 신규 원천 조회를 추가하지 않았다.

## import 호환성과 테스트

- `explanation_quotes`, `explanation_direct`, `retrieval`, `answers`, `similar`, `comparison_facts`의 기존 함수 경로를 명시적 재내보내기로 유지했다. 공개 함수뿐 아니라 옮긴 기존 보조 함수·상수도 보존했다.
- 단계 판정의 새 내부 공개 이름은 `STAGES`, `stage_events`, `completed_stage`다. 기존 `_STAGES`, `_stage_event`, `_completed_stage`와 같은 객체임을 검사한다.
- 실제 reports/sources 소비자는 하위 근거 소유자를 import한다. `sys.modules` 조회나 patch 우회 코드는 추가하지 않았다.
- 이번 분리에서는 기존 모델·원천 조회 patch 위치를 변경할 필요가 없었다. 기존 기대값을 수정하지 않았고, 새 소유자와 옛 진입점의 동일성 검사를 추가했다.
- 변경 전 출력에서 고른 **48개 전체 결론·인용 결과**를 `evidence_rendering_golden.json`에 고정했다. 출처·문구·개행까지 동일한지 CI에서 검사한다.
- 경계 검사 3개는 구현 전에 모두 실패했고, 분리 후 통과했다. 기존 호환성 검사는 유지하면서 새 실제 소유자의 의존 방향도 검사한다.

## 변경 전후 비교

모든 비교는 `.env`와 외부 네트워크를 차단했다. 기존 테스트에서 변경 전 함수 입력·반환값을 기록하고, 보관한 이전 코드와 변경 후 코드에 같은 입력을 재생했다.

| 비교 | 범위 | 결과 |
|---|---|---|
| 고정 질문 | 317개 계획·조건, 기록된 모델 제안 61개, 답변·출처·검증 269개 | **317/317 동일**, JSON 바이트 일치 |
| 근거·비교·문맥 | 1,992개 입력·반환값 | **1,992/1,992 동일** |
| 상세 범위 | 안전 발췌 172, payload 검증 167, 직접 결론 126, 인용 답변 128, 사례 결론 103, 단계 사건 426, 완료 판정 133 | 모두 동일 |
| 모델 문맥·출처 | 모델 입력 36, Notion 본문 정리 14, 모델 출처 3 | 모두 동일 |
| CI 고정 출력 | 결론·인용 전체 출력 48개 | 모두 동일 |

실행 시간은 동일한 1,992개 입력을 별도 프로세스에서 준비 실행 1회 후 5회 재생해 측정했다. 입력 복사를 포함한 함수 실행 시간이며, 5회 배치 총시간의 중앙값이다.

| 측정 | 변경 전 | 변경 후 |
|---|---:|---:|
| 1,992개 전체 배치 중앙값 | 0.2078초 | 0.2159초 |
| 마지막 반복의 개별 호출 p50 | 0.0212ms | 0.0220ms |
| 마지막 반복의 개별 호출 p95 | 0.3802ms | 0.3984ms |

전체 배치는 약 8.1ms 증가했다. 이 측정은 로컬 함수 실행 비용이며 **실제 Luna/Gemini 정확도나 API·검색을 포함한 사용자 응답 시간 측정은 아니다**. 실제 모델 호출은 0회였다. 문항 일치는 동작 보존 결과이며 새로운 정답률 향상을 의미하지 않는다.

## 구조와 전체 검증

- 실제 reports/sources → evaluation 참조 **0개**. 한시적인 `reports/evaluation.py` 재내보내기는 계획대로 P9까지 유지한다.
- aggregate → retrieval, work_queries → answers 참조 **0개**.
- 대상 순환 세 묶음(explanation direct/quotes, similar/comparison, effort/trend) 해소. 함수 내부 import를 포함한 전체 SCC는 **6 → 3개**, import 시점 SCC는 **0개 유지**.
- 순환 예외 21기록, 금지 방향 예외 7기록, 파사드 소비 예외 2기록만 제거했다. 제품 의존 예외를 추가하지 않았다. 새 구조 검사 파일은 분석 대상 문자열을 검사하는 파일이므로 기존 검사 파일과 같이 정확한 경로로 소비자 집계에서 제외했다.
- 함수 **162개**의 AST를 원본과 대조했다. import 소유자와 단계 이름 변경을 정규화하면 `_conclusion_lines` 분할 외 모든 함수 로직이 같다.
- `explanation_quotes.py` **1,743 → 145줄**, `similar.py` **796 → 399줄**, `retrieval.py` **1,210 → 803줄**.

**`assistant_web/scripts/check.sh` 전체 성공(exit 0).**

- tests: **2,756개 통과**, 커버리지 **90%**.
- compile: Python 및 프런트 빌드 산출물 일치 검사 통과.
- shell, ruff 통과. mypy **229개 소스 파일 통과**.
- audit: 기존 Python 보안 예외 **5건** 외 알려진 취약점 없음. npm 취약점 **0건**. 의존성 핀과 보안 예외는 변경하지 않았다.
- `git diff --check` 통과.

증거는 `assistant_web/runtime/architecture-p5a/`의 기준선 manifest, `before-tests.log`, `questions-comparison.json`, `evidence-cases.pickle`, `capture-baseline-evidence.json`, `before-timing-evidence.json`, `after-timing-evidence.json`, `ast-review.json`, `resolved-exceptions.json`, `before-graph.json`, `after-graph.json`, `check-final.log`에 있다. 실행 산출물은 커밋하지 않는다.

다음 단계는 **P5-B: Mantis와 검색 오케스트레이션 책임 분리**다. 남은 SCC는 Mantis 저장본/검색, workspace/team brief, chat service/unified planning 세 묶음이다.
