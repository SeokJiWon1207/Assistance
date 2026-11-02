"""원문에 명시된 완료·예정·실패를 단계별로 구분한다."""
import re

STAGES = {
    'implementation': ('개발·수정', r'(?:개발|구현|수정)(?!\s*(?:안내|가이드|본))', r'개발|구현|수정|로직'),
    'qc': ('내부 QC', r'QC|검수|내부\s*테스트', r'QC|검수|내부\s*테스트'),
    'delivery': ('고객사 전달', r'전달|발송', r'고객사?\s*전달|전달(?:\s*완료|했|하였|함|드|\s*드|\])|발송'),
    'reply': ('설명·안내 회신', r'회신|답변.{0,15}(?:메일|전달)|메일.{0,15}(?:보냈|발신)', r'회신|발신|발송'),
    'customer_application': ('고객 측 적용', r'고객.{0,18}(?:적용|반영)', r'고객.{0,22}(?:적용|반영)'),
    'customer_validation': ('고객 검증', r'고객.{0,24}(?:검증|테스트|확인)|고객.{0,16}정상',
                            r'고객.{0,24}(?:검증|테스트|확인|정상)'),
    'production': ('운영 반영', r'운영|실서비스', r'(?:운영|실서비스).{0,28}(?:반영|적용|배포)'),
}


_DONE = re.compile(r'완료|통과|마쳤|끝났|끝났음|했(?:습니다|음|다|으며)|하였습니다|되었|됐|드렸|드립니다|전달함|확인함|처리함')


_PENDING = re.compile(r'[?？]|는지|인지|여부|예정|부탁|요청|필요|계획|진행\s*중|확인\s*중|검토\s*중|'
                      r'미확인|결과.{0,8}없|기록.{0,8}없|추정|생각|듯|가정|다면|으면|경우|완료\s*후.{0,50}(?:하겠|예정|요청|부탁)|할\s*수|가능')


_FAILED = re.compile(r'\bNG\b|실패|불합격|재요청|재검수|재발|취소|철회|'
                     r'완료.{0,15}(?:아님|아니|못|않)|(?:검증|확인|테스트).{0,15}(?:못했|못하|않았)', re.I)


def _segments(row):
    # 전체 문장 안의 부정·계획·질문은 보존하고, 메일 인사나 다른 문장의 요청은 분리한다.
    for part in re.split(r'\n|(?<=[.!?])\s+', row['text'].replace('\r\n', '\n')):
        text = part.strip()
        if (text and len(text) <= 500 and not re.match(r'^(?:[A-Za-z]:[\\/]|\\\\|https?://)', text)
                and not re.match(r'^0*\d{5,7}\s*:', text)):
            yield text


def completed_stage(stage, text):
    if stage == 'delivery':
        return bool(re.search(r'\[\s*(?:고객사\s*)?전달(?:\s*완료)?\s*\]|'
                              r'(?:전달|발송)\s*(?:완료|했|하였|함|되었|드렸|드립니다|드림|드렸)', text))
    if stage == 'implementation':
        return bool(re.search(r'(?:개발|구현|수정)(?:\s*(?:작업|처리|내용))?\s*'
                              r'(?:완료|했|하였|됨|되었|됐|처리)|(?:코드|로직).{0,25}(?:추가|수정)\s*(?:완료|했|됨)', text))
    match = re.search(STAGES[stage][2],text,re.I)
    tail = text[match.end():] if match else ''
    tail = re.split(r'고객|운영|실서비스|\bQC\b|검수|개발|전달',tail,maxsplit=1,flags=re.I)[0]
    return bool(_DONE.search(tail) and not re.search(r'미완료|미적용|미반영|못|않|아님|아니',tail))


def stage_events(stage, row):
    if stage == 'reply':
        events = []
        # 원본 발신 표제와 명시적인 발신 완료만 인정한다. 수신·인용·초안은 제외한다.
        for line in row['text'].replace('\r\n','\n').splitlines():
            text = line.strip()
            header = re.match(r'^\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\s+(?:메일|이메일)\s*'
                              r'(?:회신|발신|발송)\s+To[.:\s]', text, re.I)
            explicit = re.search(r'(?:고객|담당자).{0,20}(?:메일|설명|안내).{0,20}'
                                 r'(?:회신|발신|발송)\s*(?:완료|했|하였|함)', text)
            if (header or explicit) and not re.search(r'예정|초안|미발송|미전송|보류|취소|실패|요청|부탁|하겠|인용|원문', text):
                events.append(('done',row,text))
        return events
    _, _, evidence_pattern = STAGES[stage]
    events = []
    for text in _segments(row):
        if not re.search(evidence_pattern, text, re.I):
            continue
        if stage == 'qc' and re.search(r'고객.{0,15}(?:QC|검수)', text, re.I):
            continue
        if _FAILED.search(text):
            state = 'failed'
        elif _PENDING.search(text):
            state = 'pending'
        elif completed_stage(stage, text):
            state = 'done'
        else:
            state = 'unknown'
        events.append((state, row, text))
    return events
_QC_DONE = re.compile(r'(?:QC|검수)[^\n]{0,20}(?:완료|통과)', re.I)


def _confirmed_qc_done(text: str) -> bool:
    if not _QC_DONE.search(text):
        return False
    # 'QC 테스트 완료 후 전달하겠습니다'는 앞으로 거칠 조건이지 현재 완료 기록이 아니다.
    planned_after_qc = re.search(
        r'(?:QC|검수)(?:\s*테스트)?\s*완료\s*후.{0,80}'
        r'(?:전달|배포).{0,30}(?:예정|하도록\s*하겠|하겠)', text, re.I | re.S,
    )
    return not (planned_after_qc and not re.search(r'\[\s*QC\s*완료\s*\]', text, re.I))


def _confirmation_statement(text: str, *, reproduced: bool = False) -> str:
    """확인 요청과 확인 결과를 구분하고 결과의 대상은 원문 문장에 보존한다.

    정상이라는 단어와 '확인'의 공존은 완료 근거가 아니다. 짧고 명시적인
    결과 문장만 요약에 사용하며, 긴 문맥은 아래의 원문 근거에 남긴다.
    """
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        sentence = sentence.strip()
        if not sentence or len(sentence) > 300:
            continue
        # 결과 술어가 평서형이어도 상위 문장이 질문·가정·미확인이라면
        # 관찰 사실로 분류하지 않는다. 미재현과 정상 확인에 같은 기준을 쓴다.
        if re.search(r'[?？]|는지|인지|을지|여부|미확인|예정|필요|부탁|요청|'
                     r'다면|으면|경우|어렵|취소|철회', sentence):
            continue
        if reproduced:
            if re.search(r'재현(?:되지\s*않(?:았(?:습니다|다|음)|습니다|음|는다고)|'
                         r'안됨|못(?:했(?:습니다|다|음)|함))', sentence):
                return sentence
            continue
        result = re.search(
            r'정상\s*(?:표시|동작|빌드).{0,70}?확인\s*'
            r'(?:(?:하였|했|되었|됐)(?:습니다|어요|음|다|고|으며)|됨|함|완료)', sentence)
        if not result:
            continue
        if (re.search(r'(?:지|는)\s*않|못|아니|아님', sentence)
                or re.search(r'확인.{0,20}없', sentence)):
            continue
        return sentence
    return ''


_DELIVERED = re.compile(r'\[(?:고객사\s*)?전달(?:\s*완료)?\]|전달(?:했|하였|함|완료|되었|\s*드렸습니다|\s*드립니다|\s*[.])')
