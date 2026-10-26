"""검증된 이슈 사실의 분량·채널·대상만 바꾸는 문서 표현 계층."""
import re


def document_options(text: str, kind: str) -> dict[str, str]:
    form = 'full'
    if kind == 'postmortem':
        form = 'five_lines' if re.search(r'5\s*줄|다섯\s*줄', text) else 'agenda' if re.search(r'회의.{0,20}안건', text) else 'full'
    elif kind == 'notice':
        form = next((key for word, key in [('메신저', 'messenger'), ('점검 공지', 'notice'),
                                         ('회의 공유문', 'meeting')] if word in text), 'email')
    tone = next((word for word in ['간결한', '상세한', '정중한'] if word in text), '정중한')
    audience = next((word for word in ['개발팀', '운영팀', '팀장'] if word in text), '다음 담당자')
    return {'format': form, 'tone': tone, 'audience': audience}


def present_document(lines: list[str], options: dict[str, str], *, kind: str, issue_id: int,
                     title: str, status: str, cause: str, action: str, progress: list[str],
                     note_ids: list[int]) -> list[str]:
    form = options['format']
    one_line = lambda value: ' '.join(value.split())
    evidence = '원문 노트 ' + ', '.join(str(value) for value in note_ids) if note_ids else '원문 노트 없음'
    if kind == 'postmortem' and form in {'five_lines', 'agenda'}:
        facts = [f'현황: #{issue_id} {title} · {status}', f'원인: {one_line(cause)}',
                 f'기술 조치: {one_line(action)}',
                 '진행: ' + ' / '.join(row.removeprefix('- ') for row in progress),
                 f'추가 확인(제안): 완료 근거가 없는 단계의 담당자·검증 기록 확인. 근거: {evidence}']
        if form == 'five_lines':
            return facts
        return [f'#{issue_id} 사후 분석 회의 안건', '', '### 공유할 사실', *facts[:4], '',
                '### 검토·결정할 사항', facts[4], '',
                '원인·조치의 확정 범위와 미확인 단계를 구분해 검토합니다. 새 원인이나 일정은 확정하지 않습니다.']
    if kind == 'handoff':
        focus = {'개발팀': '재현 조건·수정 범위와 후속 검증 기록을 확인하세요.',
                 '운영팀': '운영 반영·복구 절차와 고객 검증의 완료 근거를 확인하세요.',
                 '팀장': '미확인 단계의 담당자·의사결정과 다음 확인 일정을 협의하세요.',
                 '다음 담당자': '미확인 단계와 다음 담당자의 확인 범위를 점검하세요.'}
        return [*lines[:2], f'인수 대상: {options["audience"]}',
                f'대상별 확인(제안): {focus[options["audience"]]}', *lines[2:]]
    if kind == 'notice':
        # 고객용 제목은 기존 작성기에서 내부 번호·화면 코드를 제거한 값을 사용한다.
        symptom = lines[lines.index('### 현재 확인된 사실') + 1]
        state = ' / '.join(row.removeprefix('- ') for row in progress)
        if form in {'messenger', 'notice', 'meeting'}:
            heading = {'messenger': '문의하신 내용 확인 결과', 'notice': '점검 공지 초안',
                       'meeting': '고객 공유용 회의 문안'}[form]
            pending = ('점검 일정·영향 범위·다음 안내 시점은 현재 기록에서 확정되지 않았습니다.'
                       if form == 'notice' else '고객 영향 범위와 다음 안내 시점은 추가 확인이 필요합니다.')
            if options['tone'] == '간결한':
                return [heading, symptom, '진행 상황: ' + state, pending]
            greeting = '안녕하세요. 확인된 내용을 안내드립니다.' if form != 'meeting' else '확인된 내용과 함께 논의할 사항을 공유드립니다.'
            if options['tone'] == '상세한':
                return [heading, '', greeting, '', '확인된 사실: ' + symptom, '',
                        '단계별 진행 상황', *progress, '', '고객 영향 및 다음 안내', pending,
                        '각 단계는 별도로 확인했습니다. 완료 기록이 없는 단계는 완료로 안내하지 않았습니다.']
            return [heading, '', greeting, symptom, '진행 상황: ' + state, pending, '감사합니다.']
        if options['tone'] == '간결한':
            return ['제목: 문의 사항 진행 안내', '', symptom, '진행 상황: ' + state,
                    '고객 영향 범위와 다음 안내 시점은 추가 확인이 필요합니다.']
        result = ['제목: 문의 사항 확인 및 진행 상황 안내', '', *lines]
        if options['tone'] == '상세한':
            result.extend(['', '완료 근거가 확인되지 않은 단계는 완료로 안내하지 않았습니다.'])
        result.extend(['', '감사합니다.'])
        return result
    return lines
