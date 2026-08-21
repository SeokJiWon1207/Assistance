# 질의 라우팅 정확도 개선 계획 (rev 3)

- **작성일**: 2026-08-20
- **개정**: rev 3 — 소스 선택, 검색 범위, 실제 필터 적용을 분리하고 실행 소스 우회 경로까지 포함
- **대상 커밋**: `a39aa41` (main)
- **범위**: `assistant_web/backend/intent/`, `backend/retrieval.py`,
  `services/chat_service.py`, `answers.py`, `notion/formatting.py`, 관련 테스트·정확도 스크립트
- **문제**: 문서(Notion) 질문이 이슈(Mantis) 검색으로 라우팅되어 0건으로 끝남
- **방식**: 현재 코드를 실행해 실패를 재현하고, 제안안도 시뮬레이션해 회귀 여부를 확인
- **변경 사항**: 없음(본 문서는 계획서). 조사 과정에서 읽기 전용 스크립트만 실행

---

## 1. 요약

`a39aa41`에서 "신입사원 관련 문서 찾아줘"가 Mantis로 빠지는 문제를 고쳤습니다.
그 커밋은 `_NON_NAME_MODIFIERS`에 15개 단어, `NOTION_INTENT_KEYWORDS`에 13개
단어를 추가하는 방식이었습니다.

**같은 유형의 실패가 현재 HEAD에서 5건 더 재현됩니다.** 그중 4건은 단어를
아무리 등록해도 원리적으로 막을 수 없습니다.

원인은 어휘가 부족한 것이 아니라 **판단 구조**에 있습니다.

1. 조건이 **있다**는 사실을 조건이 **확실하다**로 취급합니다(상수 `0.98`)
2. **검색을 켤 어휘**와 **소스를 정할 어휘**가 구분되어 있지 않습니다
3. 이름 인식이 **열린 집합에 대한 블록리스트**라 영원히 미완성입니다
4. 소스 판단용 후보와 실제 검색에 적용할 필터가 같은 객체에 섞여 있습니다
5. 최종 계획과 별개로 `should_run_notion_search()`가 실행 소스를 다시 늘립니다

### rev 3의 핵심 원칙

질문 해석 결과를 다음 세 층으로 분리합니다.

```text
사용자 질문
  ├─ source evidence   어느 저장소를 검색할지 판단하는 근거
  ├─ query scope       고객사·플랫폼·날짜·상태·인물 등 검색 범위
  └─ applied filters   충분한 근거가 있어 결과에 강제 적용할 조건
```

`query scope`에 값이 있다고 해서 곧바로 `applied filters`가 되는 것은 아닙니다.
특히 직위 문맥에서 추론한 인물은 소스 판단 후보와 진단 정보로만 남기고 담당자
필터에는 적용하지 않습니다. 고객사·플랫폼·담당자·날짜·상태는 원칙적으로 검색
범위이며, 그 자체로 Mantis 소스를 확정하지 않습니다.

### rev 1에서 철회한 것

rev 1은 원인 2의 처방으로 `NOTION_INTENT_KEYWORDS`를 그대로
`NOTION_DOMAIN_MARKERS`로 쓰자고 했습니다. **시뮬레이션 결과 회귀합니다.**
또 임베딩 라우터를 4단계로 넣자고 했는데, **그 근거로 든 유일한 사례가
어휘 분류만으로 해결됩니다.** 근거가 사라진 수정은 뺍니다. 자세한 내용은 2-c와
4절 7단계에서 다룹니다.

---

## 2. 실측

### 2-a. 현재 HEAD에서 재현되는 실패

```bash
cd assistant_web && .venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from backend.understanding import extract_query_constraints, rule_based_search_intent
for q in ['신입사원 관련 문서 찾아줘','김보람 선임 온보딩 문서 어디 있어','협력업체 담당자 인수인계 문서','올해 등록된 규정 문서 정리해줘','담당자 김신입 이슈 보여줘','svn 접속 방법 문서']:
    c = extract_query_constraints(q); r = rule_based_search_intent(q, c)
    print(f'{q!r:34} people={c[\"people\"][\"names\"]!s:14} rule={r[\"source\"]:8} conf={r[\"confidence\"]}')
"
```

| 질문 | 추출된 인물 | 규칙 소스 | 확신도 | 기대 | 판정 |
|---|---|---|---|---|---|
| `신입사원 관련 문서 찾아줘` | `[]` | general | 0.35 | notion | 미해결(LLM 의존) |
| `김보람 선임 온보딩 문서 어디 있어` | `['김보람']` | **mantis** | **0.98** | notion | **실패(복구 불가)** |
| `협력업체 담당자 인수인계 문서` | `['협력업체']` | **mantis** | **0.98** | notion | **실패(복구 불가)** |
| `올해 등록된 규정 문서 정리해줘` | `[]` | **mantis** | **0.98** | notion | **실패(복구 불가)** |
| `담당자 김신입 이슈 보여줘` | `[]` | general | 0.35 | mantis | 인물 조건 유실 |
| `svn 접속 방법 문서` | `['svn']` | **mantis** | **0.98** | notion | **실패(복구 불가)** |

**"복구 불가"인 이유**: `chat_service.py:468`이 규칙 확신도 0.9 이상이면 LLM
플래너의 판단을 덮어씁니다.

```python
model_plan = plan_search_intent_with_model(retrieval_text, model, api_key)
if rule_plan.get("confidence", 0) >= 0.9 and rule_plan.get("source") != "general":
    model_plan["source"] = rule_plan["source"]
```

위 `0.98` 네 건은 플래너가 `notion`을 정확히 맞춰도 결과가 바뀌지 않습니다.
**모델 성능과 무관한 구조적 실패입니다.**

### 2-b. 개별 원인

- **`김보람`** — 명부에 실재하는 담당자 이름입니다. 온보딩 문서를 묻는 문장인데
  이름 하나로 Mantis가 확정됩니다. 단어 등록으로는 접근조차 안 되는 실패입니다.
- **`협력업체`** — `a39aa41`이 `_NON_NAME_MODIFIERS`에 `협력`을 넣었지만,
  `entities.py:327`의 정규식이 4글자 `협력업체`를 통째로 잡아 그대로 통과합니다.
  **블록리스트가 추가된 당일에 이미 새고 있습니다.**
- **`올해 등록된 규정 문서`** — 인물이 없는데도 `created_at` 하나로
  `has_constraint()`가 참이 되어 0.98이 붙습니다. 기간 표현은 문서 질문에도
  똑같이 붙는데 Mantis 근거로 쓰이고 있습니다.
- **`svn`** — `load_known_people()`이 뽑아온 112명 안에 `DRFN_NEXT`, `DRFN_SM`,
  `signalmaker`, `slackmanager`, `svn` 같은 **시스템 계정이 섞여 있습니다.**
- **`담당자 김신입`** — `entities.py:314`의 명시 담당자 패턴이 콜론 없는 형태에서
  `[A-Za-z]`만 받습니다. 한글 신입 이름은 통째로 버려집니다.

### 2-c. rev 1 어휘 통합안 시뮬레이션 — 회귀 확인

`NOTION_DOMAIN_MARKERS = tuple(NOTION_INTENT_KEYWORDS)`,
`MANTIS_DOMAIN_MARKERS += ("이슈",)`를 적용하고 측정한 결과입니다.

```
'MTS 상태별 이슈 보여줘'   notion_dom=True  mantis_dom=True  -> general 0.35   (기존 mantis 0.98)
'주요 이슈 현황 알려줘'     notion_dom=True  mantis_dom=True  -> general 0.35   (기존 mantis 0.98)
'화면번호 9413 이슈'      notion_dom=False mantis_dom=True  -> mantis  0.98   (유지)
'이번 주 일정 알려줘'       notion_dom=True  mantis_dom=False -> notion  0.92
'김보람 선임 온보딩 문서'    notion_dom=True  mantis_dom=False -> notion  0.92   (해결)
```

`상태별`, `타입별`, `일정`, `기한`, `주요이슈`가 `NOTION_INTENT_KEYWORDS`에 있어
`이슈`를 Mantis 표식에 넣는 순간 양쪽 표식이 동시에 잡히고,
`constraints.py:465`의 분기 구조에서는 어느 쪽도 선택되지 않아 general 0.35로
떨어집니다. **rev 1의 4-a안은 그대로 적용하면 안 됩니다.**

### 2-d. 임베딩 지연 실측

Ollama를 워밍업한 뒤 `bge-m3:latest`로 측정했습니다.

```
112.1 ms  '김보람 선임 온보딩 문서 어디 있어'
 97.3 ms  'MTS 미종료 이슈 보여줘'
100.1 ms  '올해 등록된 규정 문서 정리해줘'
```

warm 기준 약 100ms입니다. `chroma.py:123`의 `CHROMA_EMBEDDING_TIMEOUT_SECONDS`
기본값 12초는 **타임아웃 상한이지 비용이 아닙니다.** 콜드스타트(모델 로드)는
측정하지 않았습니다.

---

## 3. 원인 분석

| # | 원인 | 위치 | 성격 |
|---|---|---|---|
| 1 | 조건의 **존재**를 조건의 **확실함**으로 취급 | `intent/constraints.py:482` | 설계 |
| 2 | **검색 트리거 어휘**와 **소스 확정 어휘**가 미분리 | `intent/constants.py:54`, `:181` | 구조 |
| 3 | 이름 인식이 열린 집합에 대한 블록리스트 | `intent/entities.py:290` | 접근법 |
| 4 | 확신도 임계값을 호출부가 따로 들고 있음 | `services/chat_service.py:468` | 결합 |
| 5 | 명부에 비인물 계정이 섞여 있음 | `intent/entities.py:90` | 데이터 |
| 6 | `people` 객체를 여러 곳에서 각자 생성 | `intent/constraints.py:402` | 구조 |

### 원인 2 상세 — 목록이 갈라진 게 아니라 역할이 없다

rev 1은 이것을 "두 목록이 갈라져 있다"로 진단하고 합치자고 했습니다.
**진단은 맞았지만 처방이 반대였습니다.** 2-c가 보여주듯 합치면 회귀합니다.

실제 문제는 어휘가 **어떤 역할인지 표시되어 있지 않다**는 것입니다.
`회의록`은 Notion을 확정할 수 있지만 `일정`은 개발/QC 일정이기도 합니다.
`상태별`은 애초에 소스 어휘가 아니라 `extract_group_by()`가 처리할
표현 방식입니다. 세 가지가 한 집합에 섞여 있습니다.

### 원인 3 상세 — 왜 블록리스트는 닫히지 않는가

`_NON_NAME_MODIFIERS`는 "이름이 아닌 말"의 목록이고, 이 집합은 한국어 명사
전체이므로 열거할 수 없습니다. 반면 "이름인 말"의 판별 근거인 **성씨는 닫힌
집합**입니다. 판별 방향을 뒤집으면 목록이 자라지 않습니다.

단, 성씨 게이트만으로는 부족합니다. 실측:

| 성씨 게이트가 거름 | 성씨 형태라 통과 |
|---|---|
| 협력, 경력, 계약, 해당, 담당, 일반, 퇴직, 외주, 파견 | 신입, 정규, 인수, 기존, 전임, 임시, 전직 |

**남는 오탐을 무해하게 만드는 두 번째 층이 반드시 함께 가야 합니다**(4절 5단계).

---

## 4. 수정안 (적용 순서)

### 1단계 — 회귀셋 (선행 조건)

**이것이 없으면 이후 전부가 도박입니다.**

**위치**: `assistant_web/tests/fixtures/routing_cases.jsonl` (신규)

```json
{"q": "김보람 선임 온보딩 문서 어디 있어", "source": "notion", "why": "실명이 있어도 문서 질문"}
{"q": "MTS 상태별 이슈 보여줘",        "source": "mantis", "why": "공용 어휘가 소스를 뒤집으면 안 됨"}
{"q": "종료 안 된 건 보여줘",          "source": "mantis", "why": "상태만 — 5단계에서 abstain 으로 내려감"}
{"q": "MTS 운영 가이드 찾아줘",        "source": "notion", "platform_scope": "mts", "why": "플랫폼은 범위이고 가이드가 대상"}
{"q": "신입 선임 이슈 보여줘",          "source": "mantis", "people": [], "candidate_people": ["신입"], "why": "추론 인물은 필터 미적용"}
{"q": "담당자 김신입 이슈 보여줘",      "source": "mantis", "people": ["김신입"], "origin": "explicit_unverified"}
```

구성 원칙:

- 2-a의 실패 6건 + 2-c에서 회귀한 2건 + 현재 통과 중인 Mantis 질문 8건 이상
- **새로 약해지는 케이스를 의도적으로 포함**합니다. `종료 안 된 건 보여줘`는
  상태 조건뿐이라 5단계에서 `abstain`이 되어 플래너에 위임됩니다.
  이 위임이 허용 가능한지 회귀셋으로 확인해야 합니다
- 네트워크 없이 도는 단위 테스트로 작성. 기존 `tests/test_notion_routing.py`를
  이 픽스처 기반으로 재작성합니다
- **라우팅만으로 부족합니다.** `tests/test_scenario_questions.md`의
  "빠른 실사용 회귀 세트" 12개 중 후속 질문 범위 상속(시나리오 A),
  조건 정정(시나리오 B), 교차 소스 이동(시나리오 E)을 함께 검증합니다.
  이 셋은 `people` 스키마와 소스 결정을 동시에 건드립니다
- `scripts/accuracy/dynamic_accuracy_check.py`에 **소스 라우팅 정확도를 답변
  정확도와 분리해서** 리포트하는 항목을 추가합니다
- 소스 정확도 외에 **필터 정밀도**(요청하지 않은 필터가 붙지 않은 비율),
  **필터 재현율**(요청한 필터가 빠지지 않은 비율), 라우팅·전체 응답 p50/p95를
  별도 지표로 기록합니다

---

### 2단계 — `people` 스키마와 병합 규칙 확립

**위치**: `backend/intent/constraints.py:402`, `backend/intent/entities.py`

rev 1은 "`names` 키가 유지되므로 소비 코드에 영향 없다"고 했으나 **틀렸습니다.**
`merge_query_plan_constraints()`가 모델의 `owner`/`reporter` 필터를 병합할 때
`people` 객체를 통째로 새로 만듭니다.

```python
elif field in {"owner", "reporter"} and operator in {"equals", "in"}:
    merged["people"] = {                       # ← 여기서 name_origins 소멸
        "names": list(dict.fromkeys(str(item) for item in values)),
        "role": "handler" if field == "owner" else "reporter",
    }
```

출처를 추가하기 전에 **생성·병합을 공통 헬퍼로 통일**합니다.

```python
# backend/intent/people.py (신규)

def build_people(pairs: Iterable[tuple[str, str]], role: Optional[str]) -> Dict[str, Any]:
    """(이름, 출처) 쌍에서 people 객체를 만드는 유일한 경로.

    출처를 곳곳에서 각자 붙이면 병합 한 번에 사라진다. 생성과 병합을
    한 함수로 모아 스키마가 갈라지지 않게 한다."""


def merge_people(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """두 people 객체를 합친다. names 와 name_origins 가 항상 함께 움직인다."""


def drop_people(people: Dict[str, Any], names: Iterable[str]) -> Dict[str, Any]:
    """고객사 별칭 등으로 제외할 이름을 names 와 name_origins 에서 함께 뺀다."""


def filterable_people(people: Dict[str, Any]) -> Dict[str, Any]:
    """실제 담당자 필터에 적용할 known·explicit_unverified 이름만 반환한다."""


def grounded_model_people(
    grounding_text: str,
    base: Dict[str, Any],
    model_names: Iterable[str],
) -> Dict[str, Any]:
    """원문·상속 문맥에 근거가 없는 모델 생성 인물은 병합하지 않는다."""
```

출처 값은 세 가지입니다.

| 출처 | 의미 | 예 |
|---|---|---|
| `known` | 명부(`load_known_people`)에 있는 이름 | `김보람` |
| `explicit_unverified` | 사용자가 `담당자`라고 명시했으나 명부에 없음 | `담당자 김신입` |
| `inferred` | 직위 접미사에서 추론, 명부 미확인 | `신입 선임` |

`people` 객체는 실제 필터와 후보를 구분합니다.

```python
{
    "names": ["김보람"],
    "role": "handler",
    "name_origins": {"김보람": "known"},
    "candidates": [{"name": "신입", "origin": "inferred"}],
}
```

- `known`, `explicit_unverified`만 `names`에 들어가 실제 필터로 적용됩니다
- `inferred`는 `candidates`에만 남고 담당자 필터에는 적용되지 않습니다
- 같은 이름의 출처 우선순위는 `known > explicit_unverified > inferred`입니다
- 모델이 새 담당자 이름을 반환해도 질문 또는 상속 문맥에 같은 이름이 없으면
  병합하지 않으며, 모델 값만으로 출처를 승격하지 않습니다

공통 헬퍼는 최소한 `entities.py`, `constraints.py`의 기본값·모델 병합,
`reports/effort.py`의 개인 공수 조건, 대화 문맥 상속 후 재추출 경로에 적용합니다.
`rg`로 직접 생성 위치를 전수 확인하고, 백엔드 검색 조건의 `people` 생성이 모두
헬퍼를 통과하는 것을 완료 조건으로 둡니다.

**이 단계에서는 출처와 후보를 기록하되 기존 라우팅 결정은 바꾸지 않습니다.**
스키마 변경과 동작 변경을 한 커밋에 섞으면 회귀 원인을 가릴 수 없습니다.

---

### 3단계 — 담당자 정규식과 시스템 계정 정제

#### 3-a. 콜론 없는 한글 담당자 (제한적 허용)

rev 1의 정규식은 오탐을 만듭니다. 실측:

```
'담당 업무 정리해줘'   capture=업무   is_valid_handler_name=True   ← 오탐
'담당 현황 알려줘'     capture=현황   is_valid_handler_name=True   ← 오탐
'담당 부서 어디야'     capture=부서   is_valid_handler_name=True   ← 오탐
'담당자별 건수'        capture=None                                ← 전처리에서 이미 제거됨
```

(`담당자별` 계열은 `person_text` 생성 시 이미 제거되므로 대상이 아닙니다.)

네 가지 조건을 모두 만족할 때만 허용합니다.

```python
# '담당'이 아니라 '담당자'로 시작할 때만. '담당 업무'는 역할+명사이지
# 역할+이름이 아니다. 콜론이 있으면 '담당:'도 허용한다.
_EXPLICIT_HANDLER = re.compile(
    r"(?:담당자\s*|담당\s*[:=]\s*|담당자\s*[:=]\s*)"
    r"([가-힣]{2,4}|[A-Za-z][A-Za-z0-9_.-]{1,31})"
)

# 역할어 뒤에 흔히 오는 일반명사. 성씨 게이트를 통과하는 '현황'을 여기서 막는다.
_ROLE_FOLLOWING_NOUNS = frozenset({"업무", "이슈", "현황", "부서", "목록", "명단", "정보", "내역", "건수"})
```

- 한글 후보에는 **성씨 게이트를 적용**합니다(`업무`, `부서` 차단)
- `_ROLE_FOLLOWING_NOUNS`로 성씨 형태 일반명사를 차단합니다(`현황` 차단)
- 명부에 없으면 `explicit_unverified` 출처를 붙입니다

#### 3-b. 추론 이름에 성씨 게이트

`_NON_NAME_MODIFIERS`(15개)를 삭제하고 성씨 집합으로 대체합니다.

```python
# 한국 성씨는 닫힌 집합이다. "협력업체·해당업무"처럼 이름이 아닌 말을 일일이
# 등록하는 대신, 이름일 수 있는 형태인지를 앞글자로 판별한다.
_COMMON_SURNAMES = frozenset(
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주"
    "우구민진지엄채원천방공현함변염여추도소석선설마길연위표명기반라왕금옥육인맹제모"
)
```

3-절에서 밝혔듯 `신입`, `정규` 등은 여전히 통과합니다. 이 후보들은 2단계의
`candidates`에만 `inferred`로 저장하고 `people.names`와 `filter_clauses`에는 넣지
않습니다. 소스 권한만 낮추고 실제 담당자 필터를 남겨 두면 Mantis로 올바르게
라우팅된 뒤에도 결과가 0건이 되므로, **필터 미적용이 무해화의 핵심**입니다.

#### 3-c. 명부에서 비인물 계정 제외

**위치**: `backend/intent/entities.py:90` `load_known_people()`

현재 112명 중 `DRFN_NEXT`, `DRFN_SM`, `signalmaker`, `slackmanager`, `svn`은
시스템 계정입니다. 이들이 `known` 출처(가장 강한 근거)를 받고 있습니다.

`한글 2~4자 또는 담당 이슈 수 임계치`는 사용하지 않습니다. 빈번한 시스템 계정은
통과하고 실제 영문 사용자 계정은 탈락할 수 있어 사람 여부의 근거가 되지 못합니다.
명부 원천은 다음 우선순위로 확정합니다.

1. Mantis 사용자 API·메타데이터의 활성 사용자, 실명, 계정 유형
2. 사용할 수 없다면 별도 담당자 명부 설정과 시스템 계정 재계산 결과
3. 둘 다 불가능하면 영문·시스템형 계정은 `known`이 아니라
   `account_unverified`로 낮춰 소스 확정 근거로 사용하지 않음

시스템 계정 제외뿐 아니라 실제 영문 계정 담당자가 유지되는지도 회귀 테스트에
포함합니다. 명부가 Chroma 스냅샷에서 계속 파생된다면 6절에 기록한 증분 갱신 실패
2건을 먼저 고치고, 그렇지 않으면 명부 로딩을 해당 스냅샷 경로에서 분리합니다.

---

### 4단계 — 어휘를 역할로 분류한다

**위치**: `backend/intent/constants.py`

단순 목록 세 개가 아니라 **근거의 역할**을 분류합니다.

```python
class SourceEvidenceKind(str, Enum):
    EXPLICIT_SOURCE = "explicit_source"  # "노션에서", "Mantis에서"
    TARGET_OBJECT = "target_object"      # 회의록, 가이드, 이슈 상세, 공수 집계
    EXACT_IDENTIFIER = "exact_identifier"  # 이슈 ID, 화면번호
    SCOPE = "scope"                      # 고객사, 플랫폼, 담당자, 날짜, 상태
    TOPIC = "topic"                      # 장애, 교육, 프로젝트 등 주제 표현
    PRESENTATION = "presentation"        # 상태별, 타입별, 표로 등 출력 방식
```

| 역할 | 예 | 소스 확정 가능 여부 |
|---|---|---|
| 명시적 소스 | 노션, Notion, Mantis, 맨티스 | 가능 |
| 고정밀 대상 객체 | 회의록, 온보딩 문서, 이슈 상세, 공수 집계 | 가능 |
| 정확 식별자 | 이슈 ID, 화면번호 | Mantis 확정 |
| 검색 범위 | 고객사, MTS, 담당자, 날짜, 상태 | 불가능 |
| 공용 주제 | 장애, 교육, 프로젝트, 일정, 이슈 | 단독 확정 불가 |
| 출력 방식 | 상태별, 타입별, 오래된 순서, 표로 | 불가능 |

고객사·플랫폼은 정확한 필터일 수 있지만 Mantis 소스 근거는 아닙니다. 따라서
`MTS 운영 가이드`, `신한 프로젝트 회의록`에서는 가이드·회의록 대상이 소스를
결정하고 MTS·신한은 범위로만 남습니다.

`상태별`, `타입별`은 `extract_group_by()`가 처리하며 소스 판단에 참여하지 않습니다.
`이슈`, `장애`, `교육`, `프로젝트` 같은 단일 낱말도 구문 없이 소스를 잠그지 않습니다.

분류는 코퍼스 빈도만으로 확정하지 않습니다. 어떤 단어가 저장소에 존재한다는 사실과
사용자가 그 저장소를 요청했다는 사실은 다르며, 문서 길이와 데이터 건수도 빈도를
왜곡합니다. 다음 근거를 함께 사용합니다.

- 라벨이 있는 실제 질문 회귀셋
- Notion/Mantis 코퍼스의 표현 분포
- 기존 오탐·미탐 사례
- `운영 가이드`, `이슈 상세`, `공수 순위` 같은 구문 단위 패턴

계수 스크립트는 보조 자료로 사용하고, 최종 분류 근거와 회귀 질문을 코드 주석 또는
픽스처에 남깁니다.

---

### 5단계 — `locked` 대신 결정 권한을 이름으로 표현한다

**위치**: `backend/intent/constraints.py`, `services/chat_service.py:468`

rev 1의 `locked = confidence >= 0.95`는 근거 없는 임계값을 하나 더 만든 것입니다.
권한을 숫자가 아니라 **이름**으로 표현합니다.

```python
# backend/intent/constraints.py

class SourceAuthority(str, Enum):
    AUTHORITATIVE = "authoritative"  # 모델이 뒤집을 수 없다
    ADVISORY = "advisory"            # 방향만 제시. 모델이 다르면 모델을 따른다
    ABSTAIN = "abstain"              # 규칙이 판단하지 않는다


def source_authority(text: str, constraints: Dict[str, Any]) -> tuple[str, str, str]:
    """규칙이 소스를 정할 권한이 있는지 판단한다. (source, authority, reason)

    조건이 있다는 것과 그 조건이 확실하다는 것은 다르다. 사용자가 직접 말한
    이슈 ID 와, 본문 낱말에서 추론한 이름은 같은 무게일 수 없다."""
```

| 권한 | 근거 | 예 |
|---|---|---|
| `authoritative` | 명시적 소스명, 이슈 ID, 화면번호, 고정밀 대상 객체 | `노션에서 찾아줘`, `#1234567`, `화면번호 9413`, `온보딩 문서` |
| `advisory` | 주제 키워드, `known`/`explicit_unverified` 인물, 공용 어휘 조합 | `김보람 담당 건`, `문서 찾아줘` |
| `abstain` | 날짜·상태 등 공용 조건만 존재, `inferred` 인물만 존재 | `올해 등록된 것`, `종료 안 된 건`, `신입 선임 문서` |

호출부는 권한만 봅니다.

```python
# backend/services/chat_service.py:468
-                if rule_plan.get("confidence", 0) >= 0.9 and rule_plan.get("source") != "general":
+                if rule_plan.get("authority") == "authoritative":
                     model_plan["source"] = rule_plan["source"]
```

**`advisory`일 때 모델이 뒤집을 수 있다는 점을 의식적으로 받아들입니다.**
`김보람 선임 온보딩 문서`는 4단계로 `온보딩`이 `authoritative` Notion 근거가
되므로 인물(`advisory`)보다 우선합니다.

`both`는 사용자가 Notion과 Mantis를 모두 명시하거나 `양쪽`, `둘 다`, `비교`,
`교차`처럼 두 소스 사용을 요청한 경우에만 선택합니다. 서로 다른 소스의 근거가
우연히 함께 잡혔다는 이유로 자동으로 `both`를 선택하지 않습니다. 대상 객체가
명확하면 범위·주제보다 대상 객체를 우선하고, 대상 객체끼리 충돌하면 `abstain`으로
내려 플래너에 맡깁니다.

`inferred` 단독은 `abstain`이라 소스를 정하지 못합니다. 또한 2·3단계에서
`inferred`를 실제 필터에서 제외하므로, 모델이 Mantis를 올바르게 선택한 뒤에도
가짜 담당자 조건 때문에 0건이 되는 일이 없습니다.

#### 실행 소스 단일화

권한 모델을 적용해도 현재 `retrieval.py`의 `should_run_notion_search(query_text)`가
최종 계획과 별도로 Notion 단계를 추가하면 소스 분리는 완성되지 않습니다.

- 일반 검색 여부는 `should_run_retrieval()`이 판단합니다
- Notion 실행은 최종 `plan_source in {"notion", "both"}`일 때만 합니다
- Mantis 실행은 최종 `plan_source in {"mantis", "both"}`일 때만 합니다
- `should_run_notion_search()`는 명시적 Notion 표현 전용으로 축소하거나 제거하고,
  `retrieval.py`, `notion/formatting.py` 등 모든 소비처를 교체합니다
- 라우팅 추적 정보에 규칙·모델·폴백 중 어느 층이 최종 결정했는지 남깁니다

`needs_model`과 `chat_service.py`의 별도 `should_plan`도 권한 모델 기준으로 하나로
정리합니다. `authoritative`는 소스 분류를 위해 모델을 부르지 않되, 필터·검색어
계획에 모델이 필요한 경우는 별도로 호출할 수 있습니다.

---

### 6단계 — 실제 답변까지 포함한 통합 테스트

rev 1의 5-b는 `ungrounded_constraint_labels()`에 라벨을 추가하는 안이었습니다.
**이 함수의 유일한 소비처는 `verification.py:154`의 검증 배지 하나입니다.**

```python
"detail": f"질문에 없는 조건: {', '.join(ungrounded)}"
```

답변 본문이 바뀌지 않을 뿐 아니라, 제안한 라벨(`담당자(명부 미확인)=신입`)이
**"질문에 없는 조건"** 아래 붙어 문구 자체가 거짓이 됩니다. 철회합니다.

**대신 이미 있는 패턴을 그대로 씁니다.** `answers.py`에는 같은 목적의 함수가
둘 있습니다.

- `build_unknown_customer_report()` (`answers.py:121`) — 미등록 고객사면 검색을
  돌리지 않고 사실을 알린다. `chat_service.py:326`에서 호출
- `build_status_excluded_note()` (`answers.py:1060`) — 결과가 왜 적은지 덧붙인다.
  `answers.py:1167`에서 소비

인물은 **후자**를 따라야 합니다. 고객사 별칭은 확정적이라 선차단이 맞지만,
명부는 스냅샷에서 파생되므로 **신입은 실제로 존재하는데 스냅샷에만 없을 수
있습니다.** 검색을 막으면 안 됩니다.

```python
# backend/answers.py

def build_unverified_person_note(
    constraints: Dict[str, Any],
    count: int,
) -> Optional[str]:
    """명부에 없는 이름으로 찾다가 0건이면 그 사실을 밝힌다.

    "결과 없음"과 "그 이름을 담당자로 확인하지 못함"은 사용자에게 전혀 다른
    정보다. 명부는 스냅샷에서 파생되므로 신입이 실재해도 없을 수 있어,
    검색을 막지 않고 결과에 덧붙이기만 한다."""
    if count:
        return None
    origins = (constraints.get("people") or {}).get("name_origins") or {}
    unverified = [n for n, o in origins.items() if o == "explicit_unverified"]
    if not unverified:
        return None
    names = ", ".join(unverified)
    return (
        f"참고: '{names}'는 등록된 담당자 명부에서 확인하지 못했어요. "
        "이름이 맞다면 아직 이슈 이력이 없거나 스냅샷에 반영되지 않았을 수 있어요."
    )
```

`answers.py:1167`의 `status_note`, `unapplied_note` 옆에 나란히 붙입니다.

`inferred` 후보에는 안내를 표시하지 않습니다. 잘못 추론한 일반명사를 사용자에게
사람처럼 확인하라고 요구하면 안 됩니다. 결과가 있으면 안내하지 않고, `known`
담당자 0건에도 미확인 경고를 붙이지 않습니다.

**테스트는 라우팅 단위가 아니라 최종 답변 문자열까지 검증합니다.** 이 단계의
회귀 테스트가 없으면 rev 1과 같은 실수(검증 함수만 고치고 답변이 바뀐다고 믿는
것)가 반복됩니다.

---

### 7단계 — 임베딩 라우터는 측정 후, 기능 플래그로만

**rev 1에서 이 수정의 유일한 근거였던 `김보람 선임 온보딩 문서`는 4단계에서
해결됩니다.** 근거가 사라졌으므로 지금 넣지 않습니다.

다음 조건이 **모두** 충족될 때만 검토합니다.

1. 1~6단계 적용 후 회귀셋 정확도를 측정했고
2. 규칙 + 모델 플래너 조합으로 남는 실패가 있으며
3. 그 실패가 어휘로 분류 불가능한 유형임이 확인된 경우

도입하더라도 제약:

- `ROUTER_EMBEDDING_ENABLED` 기능 플래그. 기본값 off
- 실측 warm ~100ms(2-d)를 전제로, `abstain`일 때만 호출
- 라우트별 예시 20~30개 × 3 = 60~90회 임베딩은 **시동 시가 아니라 빌드 시**
  계산해 파일로 영속화. 시동 지연을 만들지 않음
- 임베딩 실패·부재 시 `None`을 반환해 기존 경로로 폴백

---

## 5. 적용 순서 요약

| 순서 | 단계 | 크기 | 위험 | 해결되는 실패 |
|---|---|---|---|---|
| 1 | 회귀셋 + 시나리오 검증 | 중 | 없음 | (판단 근거 확보) |
| 2 | `people` 스키마·병합 헬퍼 | 중 | 낮음 | 출처·후보 보존 |
| 3 | 담당자 추출 + 추론 후보 필터 제외 | 중 | **중** | 협력업체, 담당자 김신입, 가짜 담당자 0건 |
| 4 | 명부 원천 확립 + 어휘 근거 역할 분류 | 중 | **중** | svn, 신입사원 문서, 김보람 온보딩 문서 |
| 5 | 권한 라우팅 + 실행 소스 단일화 | 중 | **높음** | 올해 규정 문서, 계획·실행 소스 불일치 |
| 6 | 미확인 담당자 안내 + 통합 테스트 | 소 | 낮음 | 0건 응답의 오해 |
| 7 | 임베딩 라우터 | — | — | **조건부. 기본 미실행** |

4·5단계가 위험 구간입니다. 지금 0.98로 잠겨 있던 질문들이 플래너 판단에
열리기 때문입니다. 그래서 1단계가 먼저이고, 2단계에서 스키마만 먼저 확정해
동작 변경과 분리합니다.

각 단계는 독립적으로 되돌릴 수 있습니다. 4단계에서 회귀가 나오면 해당 표현을
대상 객체에서 공용 주제·검색 범위로 낮추고, 5단계에서 나오면 근거 역할과 권한을
회귀셋에 맞춰 다시 판단합니다. 단순히 권한 등급만 올려 오탐을 고정하지 않습니다.

커밋은 목적별로 분리합니다.

- `test: 라우팅 및 필터 회귀 사례 추가`
- `refactor: 인물 조건 생성과 병합 경로 통합`
- `fix: 추론 인물을 담당자 필터에서 제외`
- `fix: 최종 계획에 따라 검색 소스 실행`

---

## 6. 검증 계획

1. 각 단계 적용 후 `tests/run_test_suite.py` 전체 통과
   - **현재 370개 중 2개가 이미 실패 중입니다**(`test_snapshot_rebuild`의
     증분 갱신 2건). main에서도 동일하게 실패하며 본 계획과 무관하지만,
     "전체 통과"의 기준선이 370이 아니라 368임을 인지하고 시작해야 합니다
2. `routing_cases.jsonl` 정확도를 단계별로 기록 — 올라가는지, 어떤 케이스가
   바뀌는지 추적
3. `test_scenario_questions.md`의 빠른 회귀 세트 12개 중 시나리오 A·B·E를
   4·5단계 전후로 수동 실행
4. 4·5단계 전후로 `dynamic_accuracy_check.py`를 실데이터로 실행해 **라우팅
   정확도를 답변 정확도와 분리해서** 비교
5. 7단계를 도입한다면 Ollama를 **의도적으로 내린 상태**에서도 4번과 같은
   결과가 나오는지 확인 — 폴백 동작이 이 수정의 전제

### 완료 기준

- 대표 실패와 역회귀 픽스처가 모두 통과함
- 질문에 없는 담당자·고객사·플랫폼 필터가 새로 적용되지 않음
- 사용자가 명시한 담당자·고객사·플랫폼 필터가 누락되지 않음
- 최종 `plan_source`와 실제 실행된 검색 단계가 일치함
- `MTS 운영 가이드`, `신한 프로젝트 회의록`에서 범위 조건이 대상 소스를 이기지 않음
- `신입 선임 이슈`에 담당자 필터가 없고, `담당자 김신입 이슈`에는 필터가 있음
- 개인 담당 범위와 후속 질문 범위가 전역으로 넓어지지 않음

### 중단 기준

다음 중 하나가 발생하면 다음 단계로 넘어가지 않습니다.

- 기존 통과 라우팅 케이스가 실패함
- 질문에 없는 필터가 추가됨
- 최종 계획과 실제 실행 소스가 불일치함
- 모델·Ollama 장애 시 기존보다 더 나쁜 소스로 고정됨
- 개인 담당 범위가 유실됨

---

## 7. 채택하지 않은 대안

| 대안 | 이유 |
|---|---|
| **단어 목록 계속 확장** | `협력` 추가 당일 `협력업체`가 통과했습니다. 열린 집합을 열거로 막을 수 없습니다 |
| **어휘 목록 직접 통합** (rev 1의 4-a) | 2-c에서 회귀 확인. `MTS 상태별 이슈 보여줘`가 0.98 → 0.35 |
| **임베딩 즉시 도입** (rev 1의 4-b) | 유일한 근거 사례가 4단계에서 해결됩니다. 근거 없는 수정입니다 |
| **검증 함수만 고쳐 답변 변경** (rev 1의 5-b) | 소비처가 배지 하나뿐이고, 라벨 문구가 사실과 어긋납니다 |
| **LLM 우선 전면 전환** | 날짜 산술·집계·필터 검증은 결정형 코드가 압도적으로 정확합니다. `query_planning.py`의 환각 방어는 유지할 자산입니다 |
| **`has_constraint` 게이트 제거 후 항상 플래너 호출** | 플래너 타임아웃이 25초입니다. `authoritative` 질문까지 지연시킬 이유가 없습니다 |
| **OpenWebUI 툴 콜링 전환** | 과거 프로토타입 `openwebui_tools/mantis_issue_tools.py`의 소스는 저장소에 없고 무시된 `.pyc`만 남아 있어 실행 의존성으로 사용할 수 없습니다. 당시 구현도 최근 500건·JSON 문자열 부분일치 방식이라 날짜 범위·집계·부정 연산자를 지원하지 않았습니다 |

### 역할 분담 원칙

| | LLM / (조건부) 임베딩 | 결정형 코드 |
|---|---|---|
| 성격 | 열린 집합 | 닫힌 집합 |
| 담당 | `abstain` 구간의 소스 판단, 맥락 해석, 검색어 추출 | 날짜 산술, 집계, 필터 스키마, 별칭 정규화, `authoritative` 소스 |
| 검증 | 사후 검증의 대상 | 검증 기준 자체 |

---

## 8. 범위 밖이지만 추적할 항목

- ~~`should_use_intent_planner()`의 미사용 `needs_model` 플래그~~ — 2026-08-20에
  권한 모델과 실제 `should_plan` 경로만 남기고 제거했습니다
- ~~`signalmaker 관련 자료 찾아줘`가 Mantis로 고정되는 경로~~ — 문서 대상 근거가
  범위 조건보다 우선해 Notion advisory로 분류되며 회귀 픽스처에 포함했습니다
- `test_snapshot_rebuild`의 실패 2건은 라우팅 수정 자체와는 별개입니다. 다만
  **명부(`load_known_people`)가 이 스냅샷에서 파생되므로 3-c의 선행 의존성**입니다.
  3-c 전에 고치거나 명부 로딩을 해당 스냅샷 경로에서 분리합니다
- ~~후속 질문에서 플랫폼 제거가 유지되지 않는 문제~~ — 제외 플랫폼을 우선하는
  문맥 상속 회귀 테스트를 추가했습니다
- ~~후속 정렬 요청이 개인 범위를 잃는 문제~~ — 정렬·추가 표시처럼 대상 없는
  결과 가공 요청을 후속 질문으로 판정하고, `ChatService._build_response`의 실제
  메시지 이력 경로에서 인증 Mantis 담당자가 주입되는 회귀 테스트를 추가했습니다
- ~~Mantis 조회 후 Notion 교차 검색이 장시간 멈추는 문제~~ — Notion 호출의
  기본 예산을 8초·재시도 0회로 제한하고, 실패 시 실행 정확도와 분리해 기록합니다
- ~~검증 배지가 실제 답변 정확도와 어긋나는 문제~~ — 자동 판정하지 못한
  `review` 영역을 0점처럼 평균 분모에 넣던 계산을 수정하고, 점수와 검토 필요
  상태를 분리했습니다

이 항목들은 변경 전후 결과를 기록해 우연히 가려지지 않게 하고, 이번 범위에서 직접
수정하지 않는 항목은 별도 이슈로 분리합니다.
