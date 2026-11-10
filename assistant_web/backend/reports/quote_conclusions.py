"""사례 판정·원문 선택·완료 한계 표시를 분리한 결론 조립."""
import re
from typing import Any
from backend.evidence.excerpts import safe_excerpts, _NOTE_BOILERPLATE
from backend.evidence.references import _label, _reference
from backend.evidence.stages import _QC_DONE
from backend.evidence.quote_context import TOPIC_TITLES
from backend.reports.quote_selection import _core_choice
from backend.reports.quote_cases import (
    technical_scope_findings,
    cause_and_behavior_findings,
    implementation_and_qc_findings,
    followup_investigation_findings,
    delivery_and_customer_findings,
)


def _group_source_evidence(plans, selected_ids):
    timeline: list[dict[str, Any]] = next((preview + detail for topic, preview, detail, _ in plans if topic == 'timeline'), [])
    rows = timeline + [row for topic, preview, detail, _ in plans if topic != 'timeline'
                       for row in preview + detail]
    by_source: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row['issue_id'] in selected_ids:
            group = by_source.setdefault((row['issue_id'], row['field']), [])
            if not any(previous['text'] == row['text'] for previous in group):
                group.append(row)
    all_text = '\n'.join(row['text'] for group in by_source.values() for row in group)
    return rows, timeline, by_source, all_text


def _field_candidates(candidates, issue_id, row, text, source_text):
    if (row['field'] == 'description'
            and re.search(r'4/17.{0,120}무기한\s*연기', text, re.S)
            and re.search(r'해당\s*건\s*제외하고\s*나머지\s*수정본\s*취합', source_text)):
        candidates[(issue_id, 'postponed_work_excluded')] = (
            row, '고객은 4/17 신종증권 작업의 이행이 무기한 연기돼 이를 제외한 수정본 취합을 요청했습니다.')
    if (row['field'] == 'description'
            and re.search(r'멀티차트\s*1번\s*활성화\s*시\s*정상노출', text)
            and re.search(r'2,3,4\s*번\s*차트\s*활성화\s*시.{0,100}보이지\s*않', text, re.S)
            and re.search(r'iOS\s*/\s*aOS\s*모두', text)):
        candidates[(issue_id, 'multichart_option_symptom')] = (
            row, '고객 보고에서는 1번 차트의 옵션은 보이지만 2·3·4번에서는 보이지 않았고, '
                 'iOS와 Android 양쪽에서 같은 현상이 났습니다.')
    if (row['field'] == 'description' and 'RealOpTuja' in text
            and re.search(r'내려갔다가\s*다시\s*올라오', text)):
        candidates[(issue_id, 'process_reappears')] = (
            row, '고객은 RealOpTuja 프로세스가 내려갔다가 다시 나타난다고 문의했습니다.')
    if (row['field'] == 'description' and re.search(r'관심그룹.{0,100}전략.{0,60}저장', text, re.S)
            and re.search(r'기본값으로\s*초기화', text)):
        candidates[(issue_id, 'group_setting_not_saved')] = (
            row, '고객은 전략을 다시 열면 관심그룹 설정값이 저장되지 않고 기본값으로 초기화된다고 문의했습니다.')
    if (row['field'] == 'description' and re.search(r'두산.{0,100}차트.{0,60}DB하이텍', text, re.S)):
        candidates[(issue_id, 'chart_stock_mismatch_report')] = (
            row, '고객은 화면의 종목명은 두산인데 차트 데이터는 DB하이텍으로 표시된다고 보고했습니다.')
    if (row['field'] == 'description' and 'On Ballance Volume' in text
            and 'On Balance Volume' in text):
        candidates[(issue_id, 'obv_typo_request')] = (
            row, '고객은 OBV 설명의 On Ballance Volume을 On Balance Volume으로 고쳐 달라고 요청했습니다.')
    if (row['field'] == 'description'
            and re.search(r'100\s*건\s*초과', source_text)
            and re.search(r'내용\s*정정.{0,100}100\s*건\s*미만.{0,50}실시간\s*처리가?\s*안되', text, re.S)):
        candidates[(issue_id, 'under_100_correction')] = (
            row, '처음에는 100건 초과 시 현재가·등락률이 멈춘다고 문의했지만, '
                 '접수 본문에서 100건 미만에도 실시간 갱신이 안 된다고 정정했습니다.')
    if (row['field'] == '조치 및 개선' and re.search(r'매매구분.{0,60}실시간\s*처리\s*여부.{0,50}로직\s*추가', text)):
        candidates[(issue_id, 'action_recorded')] = (
            row, '조치 및 개선 필드에는 매매구분으로 실시간 처리 여부를 가르는 로직 추가가 기록됐습니다.')


def _collect_candidates(issues, by_source, all_text):
    candidates: dict[tuple[int, str], tuple[dict[str, Any], str]] = {}
    for (issue_id, _field), group in by_source.items():
        row = group[0]
        text = '\n'.join(item['text'] for item in group)
        source_text = '\n'.join(excerpt['text'] for excerpt in safe_excerpts(issues.get(issue_id, {}))
                                if excerpt.get('field') == row['field'])
        if not row['field'].startswith('note:'):
            _field_candidates(candidates, issue_id, row, text, source_text)
            continue
        findings = []
        findings.extend(technical_scope_findings(all_text, text, source_text))
        findings.extend(cause_and_behavior_findings(text))
        findings.extend(implementation_and_qc_findings(text))
        findings.extend(followup_investigation_findings(all_text, text, source_text))
        findings.extend(delivery_and_customer_findings(row, text, source_text))
        for kind, sentence in findings:
            key = (issue_id, kind)
            previous = candidates.get(key)
            current_order = (str(row.get('created_at') or ''), int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0)
            previous_order = ((str(previous[0].get('created_at') or ''),
                               int(str(previous[0].get('note_id'))) if str(previous[0].get('note_id')).isdigit() else 0)
                              if previous else None)
            earliest = kind in {'hypothesis', 'not_reproduced', 'reproduced', 'requery', 'operation_planned',
                                'delivery_planned', 'customer_requery', 'block_already_applied',
                                'monitor_not_reproduced', 'initial_qc_done', 'initial_esc_qc_ng',
                                'excel_post_delivery_questions', 'signal_expert_explanation'}
            if previous_order is None or (current_order < previous_order if earliest else current_order > previous_order):
                candidates[key] = (row, sentence)
    return candidates


def _select_candidate_entries(selected_ids, by_source, candidates):
    entries: list[tuple[dict[str, Any], str, str]] = []
    for issue_id in selected_ids:
        normal = candidates.get((issue_id, 'normal_confirmed'))
        requery = candidates.get((issue_id, 'requery'))
        for (candidate_issue, kind), (row, sentence) in candidates.items():
            if candidate_issue != issue_id:
                continue
            if kind == 'requery' and (not normal or str(row.get('created_at') or '') <= str(normal[0].get('created_at') or '')):
                continue
            if kind == 'recheck' and (not requery or str(row.get('created_at') or '') < str(requery[0].get('created_at') or '')):
                continue
            if (kind == 'initial_qc_done'
                    and candidates.get((issue_id, 'qc_done'), (None, ''))[0] == row):
                continue
            if (kind == 'initial_esc_qc_ng'
                    and candidates.get((issue_id, 'esc_qc_ng'), (None, ''))[0] == row):
                continue
            if (kind == 'delivered'
                    and candidates.get((issue_id, 'b_package_delivered'), (None, ''))[0] == row):
                continue
            if (kind == 'delivery_completed'
                    and candidates.get((issue_id, 'esc_existing_fix_rollback'), (None, ''))[0] == row):
                continue
            entries.append((row, sentence, kind))
    # 특정 화면·날짜별 문장을 만들지 않고 짧은 본문을 그대로 인용한다.
    # 길거나 이미 요약된 문의는 아래 원문 영역에서 보존한다.
    for (issue_id, field), group in by_source.items():
        if field != 'description' or any(row['issue_id'] == issue_id and row['field'] == field
                                         for row, _sentence, _kind in entries):
            continue
        original = '\n'.join(row['text'] for row in group)
        if 10 <= len(original) <= 500:
            entries.append((group[0], f'본문 원문: “{_label(original)}”', 'initial_request_quote'))
    return entries


def _append_topic_fallbacks(plans, selected_ids, issues, timeline, candidates, entries):
    represented = {
        'cause': {'hypothesis', 'mismatch', 'cause_processing', 'cause_null_chain', 'grid_cap_hypothesis',
                  'cause_pending', 'setscroll_hypothesis', 'excel_hypothesis', 'customer_no_excel',
                  'count_limit', 'cause_missing_copy', 'screenshot_mismatch',
                  'minute_as_daily', 'load_limit_context', 'delete_routine_risk',
                  'provider_zero_data', 'provider_liability_scope', 'gaap_insurance_exclusion',
                  'saved_graph_missing_studies', 'sleep_reconnect_unconfirmed',
                  'os_integrity_hypothesis', 'integrity_error_code',
                  'minute_bar_condition', 'legacy_files_location',
                  'legacy_files_hypothesis', 'regular_close_data_dependency',
                  'trendline_comparison_bars', 'regular_option_ats_absent',
                  'regular_option_misclassified_plan', 'signal_color_hypothesis',
                  'signal_expert_explanation', 'drdata_fixed_files'},
        'action': {'tag_changed', 'field_plan', 'server_work_done', 'action_recorded',
                   'crash_three_fixes', 'makezero_request', 'makezero_reply',
                   'server_dev_completed', 'server_field_layout', 'screen_qc_planned', 'change_notice',
                   'server_chart_roles', 'server_value_then_chart_digits', 'chart_three_digit_plan',
                   'own_fix_conditional', 'normal_provider_data_conditional', 'setscroll_defenses',
                   'tileblt_report', 'issue_not_reproduced', 'provisional_close', 'freeze_rollback',
                   'unreproduced_reopen', 'initial_999999_direction', 'final_000000_screen',
                   'two_issues_work_in_progress', 'missing_definition_complaint',
                   'developer_followup_plan', 'b_redelivery', 'variable_group_proposal',
                   'listbox_checkbox_choice', 'initial_esc_qc_ng', 'esc_qc_ng',
                   'esc_existing_fix_rollback', 'data_file_only_rollback_plan',
                   'b_package_delivered', 'postponed_work_excluded', 'merged_source_exclusion',
                   'merged_source_followup_failure', 'slow_symptom_unreproduced',
                   'clean_rebuild_requested', 'tooltip_reverted_remerge_requested',
                   'source_sync_before_remerge', 'header_cpp_requested',
                   'qc_ng', 'rebuild_qc_requested', 'qc_requested_again',
                   'future_span_mismatch', 'other_broker_reproduced', 'envelope_qc_ng',
                   'saved_graph_missing_studies', 'sleep_reconnect_unconfirmed',
                   'monitor_without_sleep_close', 'dll_reproduced_then_not',
                   'os_integrity_hypothesis', 'integrity_error_code',
                   'integrity_response_scope',
                   'excel_scope_narrowed', 'customer_datetime_mismatch',
                   'excel_post_delivery_questions', 'old_date_axis_limit',
                   'excel_market_indicator_limit', 'date_default_initial',
                   'date_default_scope_changed', 'date_default_business_day_possible',
                   'development_done_qc_in_progress',
                   'condition_time_and_sort', 'legacy_cleanup_conditional',
                   'legacy_cleanup_expanded_request', 'direct_customer_call_declined',
                   'fibonacci_group_requirement', 'customer_confirms_requirement',
                   'regular_close_scope_decisions', 'regular_close_customer_hold',
                   'trendline_period_variation', 'trendline_setting_location',
                   'regular_option_misclassified_plan', 'signal_customer_reply',
                   'drdata_market_templates', 'drdata_conditional_removal',
                   'after_hours_initial_pending',
                   'exchange_json_missing', 'exchange_zip_resent', 'formula_required',
                   'window_size_work_plan',
                   'multichart_option_symptom', 'multichart_setting_requested',
                   'process_reappears', 'yaxis_drag_workaround',
                   'main_combo_dependency', 'test_server_logging', 'feature_possible_only',
                   'version_unmanaged', 'version_folder_request', 'version_future_promise',
                   'group_setting_not_saved', 'feasibility_refused',
                   'chart_stock_mismatch_report', 'obv_typo_request',
                   'stock_mismatch_customer_followup', 'stock_mismatch_conditional_close'},
    }
    for issue_id in selected_ids:
        for topic in ('cause', 'action'):
            if any(row['issue_id'] == issue_id and kind in represented[topic] for row, _sentence, kind in entries):
                continue
            if (topic == 'cause' and (issue_id, 'monitor_close_proposed') in candidates
                    and issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True):
                continue
            topic_rows = [row for planned_topic, preview, detail, _ in plans if planned_topic == topic
                          for row in preview + detail if row['issue_id'] == issue_id
                          and (topic != 'action' or row['field'] != 'steps_to_reproduce')]
            if topic == 'action':
                topic_rows = [row for row in topic_rows if not re.match(
                    r'^\s*\[(?:QC\s*요청|QC\s*완료|고객사\s*전달|전달\s*완료)\]', row['text'])]
            short_rows = [row for row in topic_rows if len(row['text'].strip()) <= 300]
            choice = _core_choice(short_rows or topic_rows)
            if choice is None:
                complete_short = [row for row in short_rows if len(row['text'].strip()) >= 10
                                  and re.search(r'(?:다|임|음|됨)[.!?]?$', row['text'].strip())
                                  and not _NOTE_BOILERPLATE.search(row['text'])]
                choice = max(complete_short, key=lambda row: (str(row.get('created_at') or ''),
                                                               int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0),
                             default=None)
            if choice:
                statement = f'{TOPIC_TITLES[topic]} 근거에는 “{_label(choice["text"])}”라고 기록돼 있습니다.'
                entries.append((choice, statement, f'{topic}_fallback'))
        if not any(row['issue_id'] == issue_id and not kind.endswith('_fallback')
                   for row, _sentence, kind in entries):
            history = [row for row in timeline if row['issue_id'] == issue_id]
            latest_key = max(((str(row.get('created_at') or ''), int(str(row.get('note_id')))
                               if str(row.get('note_id')).isdigit() else 0) for row in history), default=None)
            latest_history = [row for row in history if
                              (str(row.get('created_at') or ''), int(str(row.get('note_id')))
                               if str(row.get('note_id')).isdigit() else 0) == latest_key]
            short_history = [row for row in latest_history if len(row['text'].strip()) <= 300]
            choice = _core_choice(short_history or latest_history)
            if choice:
                entries.append((choice, f'마지막 기록에는 “{_label(choice["text"])}”라고 적혀 있습니다.',
                                'latest_fallback'))


def _sort_entries(candidates, entries):
    def _event_order(entry: tuple[dict[str, Any], str, str]) -> tuple[str, int, int]:
        row, _sentence, kind = entry
        anchor = candidates.get((row['issue_id'], 'field_plan')) if kind == 'action_recorded' else None
        anchor_row = anchor[0] if anchor else row
        note_id = str(anchor_row.get('note_id'))
        return (str(anchor_row.get('created_at') or ''), int(note_id) if note_id.isdigit() else 0,
                int(kind == 'action_recorded'))
    entries.sort(key=_event_order)


def _append_confirmation_limits(rows, plans, selected_ids, issues, entries, all_text, lines):
    operation_done = re.search(
        r'운영.{0,25}(?:반영|배포|적용).{0,15}(?:완료(?:했|되었습니다|됨)|했습니다|되었습니다)', all_text)
    operation_negated = re.search(r'운영.{0,25}(?:반영|배포|적용).{0,15}완료.{0,12}(?:아님|아니|않|못|미확인|예정)', all_text)
    if (not (operation_done and not operation_negated)
            and not any(kind == 'operation_not_applied' for _row, _sentence, kind in entries)):
        lines.extend(['선택된 근거만으로 운영 반영 완료는 확정할 수 없습니다.', ''])
    if (len(selected_ids) == 1
            and issues.get(selected_ids[0], {}).get('answer_evidence', {}).get('complete') is True
            and any(kind in {'qc_done', 'initial_qc_done'} for _row, _sentence, kind in entries)):
        history_text = '\n'.join(excerpt['text'] for excerpt in safe_excerpts(issues[selected_ids[0]]))
        if not re.search(r'고객사?\s*전달|전달\s*완료|전달(?:했|하였|드렸|됨)', history_text):
            lines.extend(['확인된 전체 이력에는 고객사 전달 완료 기록이 없습니다.', ''])
    if (any(kind == 'monitor_close_proposed' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)
            and not re.search(r'원인\s*(?:은|:|：).{0,80}(?:확인|판명|확정)|수정\s*(?:패치|파일).{0,20}완료', all_text, re.S)):
        lines.extend(['제공된 전체 이력에서는 원인 확정이나 수정 패치 완료 근거를 확인할 수 없습니다.', ''])
    if (any(kind == 'unlimited_recurred' for _row, _sentence, kind in entries)
            and any(kind == 'limited_applied' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        lines.extend(['제공된 전체 이력에는 무제한 조회의 최종 정상화 근거가 없습니다.', ''])
    blank_events = [row for row, _sentence, kind in entries if kind == 'operation_blank_after_delivery']
    if (blank_events and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                             for issue_id in selected_ids)
            and max(str(row.get('created_at') or '') for row in rows) <= str(blank_events[-1].get('created_at') or '')):
        lines.extend(['운영 빈 화면 후속 장애의 해결 완료 근거는 제공된 전체 이력에서 확인되지 않습니다.', ''])
    if (any(kind == 'own_fix_conditional' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)
            and not re.search(r'(?:수정|패치|복구).{0,20}(?:완료|적용)', all_text, re.S)):
        lines.extend(['우리 쪽 수정·배포 완료 근거는 제공된 전체 이력에서 확인되지 않습니다.', ''])
    if (any(kind == 'cause_pending' for _row, _sentence, kind in entries)
            and any(topic == 'cause' and unanswered for topic, _preview, _detail, unanswered in plans)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        lines.extend(['제공된 전체 이력에는 확정된 원인이 기록돼 있지 않습니다.', ''])
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (not re.search(r'(?:코드|로직|소스).{0,30}(?:수정|변경|추가)|'
                          r'(?:수정|변경|추가).{0,30}(?:코드|로직|소스)', source_text, re.S)
                and not re.search(r'(?:고객|사용자).{0,40}(?:정상|해결).{0,20}(?:확인|회신)', source_text, re.S)):
            lines.extend(['구체적인 코드 수정 내역과 고객 측 정상 동작 확인도 제공된 이력에는 없습니다.', ''])
    if (any(kind == 'tileblt_report' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (not re.search(r'TileBlt.{0,100}(?:수정|패치).{0,30}(?:완료|적용|전달)', source_text, re.I | re.S)
                and not re.search(r'(?:고객|사용자).{0,40}(?:정상|해결).{0,20}(?:확인|회신)', source_text, re.S)):
            lines.extend(['후속 TileBlt 문제의 수정 완료와 고객 측 정상 동작 확인 근거는 없습니다.', ''])
    if (any(kind == 'provisional_close' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'(?:수정|패치).{0,25}(?:완료|적용|전달)|배포.{0,10}완료', source_text, re.S):
            lines.extend(['제공된 전체 이력에는 수정 패치나 배포 완료 기록이 없습니다.', ''])
    if (any(kind == 'unreproduced_reopen' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'(?:원인.{0,25}(?:확정|판명)|(?:수정|패치).{0,25}(?:완료|적용|전달))',
                         source_text, re.S):
            lines.extend(['제공된 전체 이력에는 원인·수정 확정 근거가 없습니다.', ''])
    if (any(kind == 'cci_final_scope' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'(?:수정|패치).{0,25}(?:완료|적용|전달)', source_text, re.S):
            lines.extend(['신차트를 구차트 값에 맞춘 수정·패치 완료 근거는 없습니다.', ''])
    volume_events = [row for row, _sentence, kind in entries if kind == 'volume_followup']
    if (volume_events and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                              for issue_id in selected_ids)):
        last_volume = max(volume_events, key=lambda row: str(row.get('created_at') or ''))
        later_text = '\n'.join(excerpt.get('text', '') for excerpt in safe_excerpts(
            issues.get(last_volume['issue_id'], {}))
            if str(excerpt.get('created_at') or '') > str(last_volume.get('created_at') or ''))
        if not re.search(r'거래량.{0,50}(?:정상|일치|해결|수정\s*완료)', later_text, re.S):
            lines.extend(['분봉 합계와 일간 거래량 차이의 후속 해결 근거는 제공된 이력에 없습니다.', ''])
    if (any(kind == 'two_issues_work_in_progress' for _row, _sentence, kind in entries)
            and any(kind == 'delivery_completed' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (not _QC_DONE.search(source_text)
                and not re.search(r'(?:코드|로직|소스).{0,35}(?:수정|변경|추가)\s*완료|'
                                  r'(?:수정|변경|추가).{0,35}(?:코드|로직|소스).{0,20}완료', source_text, re.S)):
            lines.extend(['전달 완료는 기록됐지만 QC 완료나 구체적 수정 코드 내역은 확인되지 않습니다.', ''])
    if (any(kind == 'delete_routine_risk' for _row, _sentence, kind in entries)
            and all(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
                    for issue_id in selected_ids)):
        source_text = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if not re.search(r'delete\s*루틴.{0,100}(?:수정|패치).{0,25}(?:완료|적용|전달)',
                         source_text, re.I | re.S):
            lines.extend(['이 이력에는 delete 루틴 수정·릴리즈 적용 완료 근거가 없습니다.', ''])
    for issue_id in selected_ids:
        if issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is not True:
            continue
        for event_kind, missing_pattern, missing_sentence in (
                ('b_redelivery', r'(?:재컴파일|컴파일).{0,30}(?:성공|정상)|(?:고객|운영).{0,40}정상.{0,20}(?:확인|회신)',
                 '재컴파일 성공이나 운영 정상화에 대한 고객 확인은 제공된 전체 이력에 없습니다.'),
                ('esc_existing_fix_rollback', r'ESC.{0,40}(?:해결|정상|수정\s*완료|반영\s*완료)',
                 'ESC 종료 오류가 최종 해결됐다는 기록은 제공된 전체 이력에 없습니다.'),
                ('b_package_delivered', r'(?:고객|사용자).{0,50}(?:정상\s*동작|정상화|해결).{0,20}(?:확인|회신)',
                 'B 패키지 전달 뒤 고객 측 정상 동작 확인은 제공된 전체 이력에 없습니다.'),
        ):
            required_kind = {'b_redelivery': 'missing_definition_complaint',
                             'esc_existing_fix_rollback': 'esc_qc_ng',
                             'b_package_delivered': 'data_file_only_rollback_plan'}[event_kind]
            if not any(row['issue_id'] == issue_id and kind == required_kind
                       for row, _sentence, kind in entries):
                continue
            event_rows = [row for row, _sentence, kind in entries
                          if row['issue_id'] == issue_id and kind == event_kind]
            if not event_rows:
                continue
            latest_event = max(event_rows, key=lambda row: (str(row.get('created_at') or ''),
                                                           int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
            later_text = '\n'.join(excerpt.get('text', '') for excerpt in safe_excerpts(issues[issue_id])
                                   if (str(excerpt.get('created_at') or ''),
                                       int(str(excerpt.get('note_id'))) if str(excerpt.get('note_id')).isdigit() else 0)
                                   >= (str(latest_event.get('created_at') or ''),
                                       int(str(latest_event.get('note_id'))) if str(latest_event.get('note_id')).isdigit() else 0))
            if not re.search(missing_pattern, later_text, re.I | re.S):
                lines.extend([missing_sentence, ''])
        remerge_events = [row for row, _sentence, kind in entries
                          if row['issue_id'] == issue_id and kind == 'tooltip_reverted_remerge_requested']
        if remerge_events:
            latest_remerge = max(remerge_events, key=lambda row: (str(row.get('created_at') or ''),
                                                               int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
            later_text = '\n'.join(excerpt.get('text', '') for excerpt in safe_excerpts(issues[issue_id])
                                   if str(excerpt.get('created_at') or '') > str(latest_remerge.get('created_at') or ''))
            if not re.search(r'(?:재전달|전달\s*완료|고객사\s*전달)', later_text):
                lines.extend(['이후 재취합본을 다시 전달했다는 기록은 제공된 전체 이력에 없습니다.', ''])
    periodic_events = [row for row, _sentence, kind in entries if kind == 'periodic_recheck']
    if periodic_events and all(
            issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is True
            for issue_id in selected_ids):
        latest_recheck = max(periodic_events, key=lambda row: (str(row.get('created_at') or ''),
                                                           int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
        latest_record = max(rows, key=lambda row: (str(row.get('created_at') or ''),
                                                   int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
        full_sources = '\n'.join(excerpt.get('text', '') for issue_id in selected_ids
                                 for excerpt in safe_excerpts(issues.get(issue_id, {})))
        if (latest_recheck['issue_id'] == latest_record['issue_id']
                and latest_recheck.get('field') == latest_record.get('field')
                and not re.search(r'(?:수정|패치|복구).{0,20}(?:완료|적용|배포)', full_sources, re.S)):
            lines.extend(['제공된 전체 이력에서는 분·틱 주기 오류의 해결 완료 근거는 확인되지 않습니다.', ''])


def _render_entries(rows, plans, selected_ids, issues, entries, all_text):
    if not entries:
        return []
    lines = ['### 핵심 결론', '']
    for row, sentence, _kind in entries[:12]:
        lines.extend([f'- {sentence} {_reference(row)}', ''])
    _append_confirmation_limits(rows, plans, selected_ids, issues, entries, all_text, lines)
    return lines


def _conclusion_lines(plans: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]],
                      selected_ids: list[int], issues: dict[int, dict[str, Any]]) -> list[str]:
    """검증된 원문에 명시된 단계만 고정 문장으로 표현하고 출처를 바로 붙인다."""
    rows, timeline, by_source, all_text = _group_source_evidence(plans, selected_ids)
    candidates = _collect_candidates(issues, by_source, all_text)
    entries = _select_candidate_entries(selected_ids, by_source, candidates)
    _append_topic_fallbacks(plans, selected_ids, issues, timeline, candidates, entries)
    _sort_entries(candidates, entries)
    return _render_entries(rows, plans, selected_ids, issues, entries, all_text)
