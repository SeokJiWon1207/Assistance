"""기술 수정과 구분할 실패 시도·전달·불확실한 결과를 원문 출처에 결속한다."""
import re

from backend.common import compact_text

_FAILED = re.compile(r'(?:시도|방문|적용|처리|수정|테스트).{0,65}(?:실패|안\s*됐|되지\s*않았|효과.{0,5}없|변화.{0,5}없)')
_DELIVERED = re.compile(r'(?:소스|수정본|패치|모듈|파일).{0,50}(?:보냈|보내\s*드렸|전달\s*(?:했|하였|완료)|송부\s*(?:했|하였|완료))')
_TENTATIVE = re.compile(r'(?:처리|수정|해결).{0,20}(?:된\s*듯|한\s*듯|된\s*것\s*같|한\s*것\s*같)')
_FUTURE_FAILURE = re.compile(r'실패\s*(?:하면|할|시|가능|예정)|실패하지|실패한\s*것은\s*아니')
_DELIVERY_QUESTION = re.compile(r'(?:보냈|전달했|송부했)(?:는지|나요|습니까|다면|으면)|'
                               r'(?:보냈|전달했|송부했).{0,12}(?:가정|여부)|전달하지|보내지')


def treatment_history(issue: dict) -> list[str]:
    from backend.reports.similar import _clean_note, _note_sentences, _resolution_notes

    sources = [('본문', str(issue.get('description') or ''))]
    sources.extend((f"노트 {note['id']}" if note.get('id') else '노트', str(note.get('text') or ''))
                   for note in _resolution_notes(issue))
    events: dict[str, list[str]] = {}
    for reference, raw in sources:
        for sentence in _note_sentences(_clean_note(raw, issue)):
            # 날짜는 남기되 본문 이력의 작성자 표시는 반복하지 않는다.
            sentence = re.sub(r'\[(\d{4})-[^]\n]+\]', r'[\1]', sentence)
            for clause in re.split(r'[,，]\s*', sentence):
                kinds = []
                if _FAILED.search(clause) and not _FUTURE_FAILURE.search(clause):
                    kinds.append('실패한 시도')
                if _DELIVERED.search(clause) and not _DELIVERY_QUESTION.search(clause):
                    kinds.append('소스 전달')
                if _TENTATIVE.search(clause) and re.search(r'완료\s*처리|종료\s*처리', clause):
                    kinds.append('추정적 완료·행정 처리')
                for kind in kinds:
                    source = f'[{reference}]' if reference.startswith('노트 ') else f'({reference})'
                    value = f'{kind}: {compact_text(clause, 150)} {source}'
                    bucket = events.setdefault(kind, [])
                    if value not in bucket:
                        bucket.append(value)
    return [value for values in events.values() for value in values[-2:]]


def history_covered(text: str, source: str) -> bool:
    """근거 ID만 연결하고 실패·전달·불확실성을 생략한 요약도 거절한다."""
    requirements = [
        ('실패한 시도:', r'실패|성공하지|시도.{0,12}(?:못|않)'),
        ('소스 전달:', r'전달|보냈|보낸|송부'),
        ('추정적 완료·행정 처리:', r'추정|듯|확정.{0,12}(?:못|않|없)|미확인|불확실'),
    ]
    if any(label in source and not re.search(pattern, text) for label, pattern in requirements):
        return False
    if '소스 전달:' in source:
        words = {'한': '1', '두': '2', '세': '3', '네': '4'}
        def counts(value):
            return {words.get(number, number) for number in re.findall(r'(\d+|한|두|세|네)\s*(?:번|회|차례)', value)}
        if not counts(source).issubset(counts(text)):
            return False
    return True
