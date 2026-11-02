"""사례 판정·원문 선택·완료 한계 표시를 분리한 결론 조립."""
import re
from backend.evidence.references import _label
from backend.evidence.stages import _confirmed_qc_done, _confirmation_statement


def technical_scope_findings(all_text, text, source_text):
    findings = []
    if (re.search(r'Y축\s*스케일\s*선택.{0,130}드래그\s*&\s*드랍.{0,80}때에만', text, re.S)
            and re.search(r'지표관리\s*탭.{0,150}드래그\s*&\s*드랍하여\s*나눈\s*후.{0,100}다시', text, re.S)):
        findings.append(('yaxis_drag_workaround',
                         'Y축 스케일 선택은 지표를 드래그해서 합칠 때만 제공됩니다. '
                         '지표관리 탭으로 추가했다면 지표를 분리한 뒤 가격차트에 다시 합치라고 안내했습니다.'))
    if (re.search(r'종목콤보와\s*거래소\s*버튼.{0,90}하나의\s*메인\s*컨트롤\s*\(IJMComboCtl\)', text, re.S)
            and re.search(r'메인에서\s*별도의\s*종목\s*컨트롤\s*및\s*거래소\s*버튼', text)
            and re.search(r'메인.{0,60}지원\s*가능\s*여부.{0,30}우선\s*확인', text, re.S)):
        findings.append(('main_combo_dependency',
                         '종목콤보와 거래소 버튼은 메인 컨트롤 IJMComboCtl 하나입니다. '
                         '분리하려면 메인의 별도 종목 컨트롤·거래소 기능 지원을 먼저 확인해야 하며, '
                         '이 회신은 변경 완료 기록이 아닙니다.'))
    if (re.search(r'테스트\s*서버도\s*운영과\s*동일한\s*현상\s*발생\s*확인', text)
            and re.search(r'로그\s*추가\s*후\s*테스트\s*서버에\s*반영', text)):
        findings.append(('test_server_logging',
                         '운영과 같은 현상을 테스트 서버에서도 확인해 로그를 추가하고 테스트 서버에 반영했습니다. '
                         '원인 확정이나 운영 해결 기록은 아닙니다.'))
    if (re.fullmatch(r'\s*가능합니다\s*[.!]?\s*', text)
            and re.search(r'신규\s*기능.{0,35}추가.{0,25}가능', all_text, re.S)):
        findings.append(('feature_possible_only',
                         '신규 기능 추가가 가능하다는 회신입니다. 구현·배포 완료를 뜻하지는 않습니다.'))
    if re.search(r'현재\s*버전이\s*별도로\s*관리되고\s*있지는\s*않', text):
        findings.append(('version_unmanaged', '당시 차트 라이브러리 버전은 별도로 관리되지 않았습니다.'))
    if (re.search(r'폴더\s*기준.{0,35}\[2\.0\]', text, re.S)
            and re.search(r'버전\s*붙여서\s*전달', text)):
        findings.append(('version_folder_request',
                         '고객은 다음 전달부터 폴더 이름에 [2.0]처럼 버전을 붙여 달라고 요청했습니다.'))
    if (re.search(r'추후\s*라이브러리\s*전달.{0,25}버전\s*넣어서\s*전달', text)
            and re.search(r'겠습니다', text)):
        findings.append(('version_future_promise',
                         '공급사는 추후 라이브러리 전달 때 디렉터리별 버전을 넣겠다고 답했습니다. '
                         '실제 적용본 전달 완료 기록은 아닙니다.'))
    if (re.search(r'해당\s*이슈\s*개선\s*불가능', text)
            and re.search(r'유지보수가\s*아닌\s*개발로.{0,25}추측', text)):
        findings.append(('feasibility_refused',
                         '이 건은 개선 불가능하다고 회신했고, 개선하더라도 유지보수가 아닌 개발로 '
                         '분류될 것으로 추측했습니다. 구현 완료 기록은 아닙니다.'))
    if (re.search(r'제\s*PC에서도\s*발생\s*안|제\s*PC에서도\s*발생안', text)
            and re.search(r'관심탭.{0,70}종목\s*변경.{0,20}그룹이동', text, re.S)):
        findings.append(('stock_mismatch_customer_followup',
                         '고객 담당자 PC에서도 재현되지 않았고, 고객은 2000 화면 관심탭의 '
                         '종목·그룹 변경 뒤 발생했다고 추가 회신했습니다.'))
    if (re.search(r'양사\s*PC에서\s*재현되지\s*않', text)
            and re.search(r'정보가\s*부족.{0,35}우선.{0,20}종료', text, re.S)
            and re.search(r'추\s*후.{0,100}정보.{0,100}재접수', text, re.S)):
        findings.append(('stock_mismatch_conditional_close',
                         '양사 PC에서 재현되지 않고 정보가 부족해 우선 종료했으며, '
                         '추가 정보가 오면 재접수해 다시 확인하기로 했습니다.'))
    if (re.search(r'Crash\s*오류\s*3건\s*수정.{0,40}안드로이드\s*차트\s*라이브러리\s*전달', text, re.I | re.S)
            and re.search(r'JipyoControlSetUI\.java.{0,500}i_cnt\s*!=\s*null', text, re.S)
            and re.search(r'AutoTrendView\.java.{0,300}숫자만.{0,300}InputType\.TYPE_CLASS_NUMBER', text, re.S)
            and re.search(r'NeoChart2\.java.{0,600}blocks\.size\(\)\s*<=\s*m_nMaxIndicatorCount', text, re.S)):
        findings.append(('crash_three_fixes',
                         'Android 차트 라이브러리의 Crash 3건을 수정해 전달했다고 기록했습니다. '
                         'JipyoControlSetUI는 i_cnt null 검사, AutoTrendView는 입력값의 숫자 제한, '
                         'NeoChart2는 블록 경계 비교를 &lt;에서 &lt;=로 바꿨습니다.'))
    if (re.search(r'COMUtil.{0,30}makeZero', text, re.I | re.S)
            and re.search(r'null\s*예외처리\s*추가\s*요청', text, re.I)):
        findings.append(('makezero_request',
                         '그 뒤 고객은 COMUtil.makeZero의 null 예외처리를 추가해 달라고 요청했습니다.'))
    if (re.search(r'3/11\s*메일\s*전달', source_text)
            and re.search(r'소스\s*1826\s*라인.{0,50}널체크\s*추가되어\s*있', text, re.S)
            and re.search(r'언제\s*받은\s*라이브러리.{0,60}지금도\s*계속\s*발생', text, re.S)):
        findings.append(('makezero_reply',
                         '후속 회신 기록에는 보유 소스 1826행의 str.length()에 null 체크가 있다고 답하고, '
                         '사용 중인 라이브러리 시점과 지금도 재현되는지를 되물었습니다.'))
    return findings


def cause_and_behavior_findings(text):
    findings = []
    hypothesis = re.search(r'([^\n]{2,60}?)에 문제가 있는 것으로 보입니다', text)
    if hypothesis:
        object_name = hypothesis.group(1).strip(' .,')
        findings.append(('hypothesis', f'초기에는 {_label(object_name)}에 문제가 있는 것으로 추정했습니다.'))
    if (re.search(r'재현(?:되지\s*않았|되지\s*않|안됨|되지\s*않음|못)', text)
            and not re.search(r'return\s+FALSE.{0,40}크래시가\s*재현되지\s*않', text, re.I | re.S)
            and not re.search(r'(?:타사|미래에셋|다른\s*고객|다른\s*환경).{0,25}재현', text)
            and not re.search(r'현상.{0,25}(?:확인됨|재현됨)', text)):
        statement = _confirmation_statement(text, reproduced=True)
        if statement:
            findings.append(('not_reproduced', f'해당 시점의 재현 확인 기록: “{_label(statement)}”'))
    if re.search(r'현상\s*확인되어.{0,25}원인\s*확인\s*중', text, re.S):
        findings.append(('cause_pending', '현상은 확인됐지만 당시 원인 확인 중이었습니다.'))
    if re.search(r'현상\s*재현되는\s*것\s*확인|동일.{0,20}재현.{0,12}확인', text):
        findings.append(('reproduced', '후속 테스트에서는 같은 현상이 재현됐습니다.'))
    if (re.search(r'당일\s*내의\s*분봉들을\s*서로\s*비교', text)
            and re.search(r'8060원.{0,80}9060원.{0,80}5\.104%', text, re.S)):
        findings.append(('minute_bar_condition',
                         '당일 분봉을 전봉과 비교하는 조건입니다. 동양우는 10시 봉에서 '
                         '8060원에서 9060원으로 올라 5.104%를 만족해 검색됐습니다.'))
    if (re.search(r'최근\s*봉의\s*조건\s*만족\s*시간으로\s*변경', text)
            and re.search(r'종목\s*코드를\s*기반으로\s*정렬', text)):
        findings.append(('condition_time_and_sort',
                         '같은 종목이 다시 만족하면 표시된 만족 시각은 최신 봉으로 바뀌지만, '
                         '결과는 종목 코드순이라 맨 위로 이동하지 않습니다.'))
    if ('User\\Chart\\ 폴더가 아닌 User\\ 폴더' in text
            and re.search(r'8000여개의\s*파일', text)):
        findings.append(('legacy_files_location',
                         '확인된 8천여 파일은 현재 설정 폴더인 User\\Chart\\가 아니라 '
                         'User\\ 바로 아래에 있었습니다.'))
    if (re.search(r'과거\s*User.{0,80}User.{0,10}Chart.{0,30}전환', text, re.S)
            and re.search(r'잔여파일이\s*남은\s*상황으로\s*예상', text)):
        findings.append(('legacy_files_hypothesis',
                         '과거 유저파일 전환 뒤 남은 파일로 추정했으며, 현재 유저 설정은 '
                         'User\\Chart\\에 저장된다고 설명했습니다.'))
    if (re.search(r'차트저장하기\s*기능', text)
            and re.search(r'가상페이지\s*저장\s*기능', text)
            and re.search(r'일정기간\s*사용되지\s*않은\s*경우\s*삭제하는\s*기능의\s*추가는\s*가능', text)):
        findings.append(('legacy_cleanup_conditional',
                         '차트 저장·가상페이지 저장 파일을 제외한 미사용 파일은 기간을 정하면 '
                         '삭제 기능 추가가 가능하다고 안내했습니다. 개발 완료 기록은 아닙니다.'))
    if re.search(r'1,\s*2를\s*포함해서\s*지울\s*수\s*있도록\s*요청', text):
        findings.append(('legacy_cleanup_expanded_request',
                         '마지막 고객 요청은 앞서 예외로 둔 1·2번 저장 파일도 삭제 대상에 '
                         '포함해 달라는 것이었습니다.'))
    if (re.search(r'직접\s*고객\s*응대를\s*진행하지는\s*않', text)
            and re.search(r'직접적인\s*유선\s*통화\s*응대는\s*어려', text)):
        findings.append(('direct_customer_call_declined',
                         '공급사는 고객과 직접 전화 응대를 하지는 않는다고 안내했습니다.'))
    if (re.search(r'피보나치\s*조정대\s*사용\s*시', text)
            and re.search(r'수치값\s*그룹을\s*저장', text)
            and re.search(r'다음\s*작도\s*때\s*한\s*번에\s*불러', text)):
        findings.append(('fibonacci_group_requirement',
                         '요청 기능은 피보나치 수치값 그룹을 저장해 다음 작도에 한 번에 '
                         '불러와 적용하는 것입니다.'))
    if re.search(r'넵\s*맞습니다\..{0,30}확인\s*부탁', text, re.S):
        findings.append(('customer_confirms_requirement',
                         '고객은 공급사가 정리한 기능 설명이 맞다고 확인했습니다.'))
    if (re.search(r'정규장\s*옵션\s*제공\s*범위', text)
            and 'KRX/NXT/통합' in text
            and re.search(r'애프터장.{0,50}포함\s*여부', text, re.S)
            and re.search(r'정규장\s*옵션\s*주기\s*제공\s*범위', text)):
        findings.append(('regular_close_scope_decisions',
                         '구현 전 KRX만 또는 KRX·NXT·통합 적용, 주기와 화면 범위, '
                         '애프터 시세 포함 여부를 정해야 했습니다.'))
    if (re.search(r'정규장기준\s*데이터를\s*받을수있는\s*FID', text)
            and re.search(r'정규장\s*전용\s*실시간\s*패킷|기존\s*패킷에\s*정규장용\s*필드', text)):
        findings.append(('regular_close_data_dependency',
                         '정규장 기준 데이터 FID 또는 정규장용 실시간 패킷·필드가 '
                         '추가로 필요했습니다.'))
    if (re.search(r'KRX정규장시세제공.{0,50}당장은\s*어려', text, re.S)
            and re.search(r'개발검토.{0,15}보류', text)):
        findings.append(('regular_close_customer_hold',
                         'SK는 정규장 시세 제공을 당장 준비하기 어려워 10월 1일 '
                         '개발 검토를 보류했습니다. 구현·배포 완료 기록은 아닙니다.'))
    if (re.search(r'비교봉수\s*값의\s*의미', text)
            and re.search(r'좌우로\s*몇\s*개의\s*캔들과\s*가격을\s*비교', text)
            and re.search(r'고점\s*조건.{0,150}5개의\s*봉', text, re.S)
            and re.search(r'저점\s*조건.{0,150}5개의\s*봉', text, re.S)):
        findings.append(('trendline_comparison_bars',
                         '비교봉수는 기준 봉의 좌우 몇 개 봉과 가격을 비교할지 정합니다. '
                         '값이 5면 앞뒤 5봉보다 고가가 높거나 저가가 낮을 때 고점·저점으로 확정합니다.'))
    if (re.search(r'각\s*주기별\s*캔들을\s*기준으로', text)
            and re.search(r'주기별로\s*표시되는\s*추세선은\s*달라질\s*수', text)):
        findings.append(('trendline_period_variation',
                         '각 주기의 캔들을 기준으로 그리므로 주기별 추세선은 달라질 수 있습니다.'))
    if (re.search(r'차트\s*우측\s*차트툴바\s*탭을\s*\[F\]', text)
            and re.search(r'패턴설정\s*툴바.{0,100}자동\s*추세선\s*탭\s*하단의\s*단기/중기/장기', text, re.S)):
        findings.append(('trendline_setting_location',
                         '비교봉수는 차트 우측 [F] 기능툴의 패턴설정 창, '
                         '자동 추세선 탭 아래 단기·중기·장기에서 확인할 수 있습니다.'))
    if re.search(r'ATS를\s*제공하지\s*않는\s*회원사에서\s*발생', text):
        findings.append(('regular_option_ats_absent',
                         '정규장 옵션 미표시는 ATS를 제공하지 않는 회원사에서 발생했습니다.'))
    if (re.search(r'ATS\s*미제공\s*회원사.{0,50}디버깅', text, re.S)
            and re.search(r'KRX로\s*분류하지\s*못', text)
            and re.search(r'수정은\s*오늘\s*진행예정', text)):
        findings.append(('regular_option_misclassified_plan',
                         '디버깅에서는 ATS 미제공 회원사를 KRX로 분류하지 못하는 로직을 확인했습니다. '
                         '9월 30일 수정은 예정 단계였고 반영 일정은 확인 후 안내하기로 했습니다.'))
    if (re.search(r'신호\s*강도\s*숫자별로\s*신호\s*색상이\s*다름', text)
            and re.search(r'단순\s*색상구분.{0,40}아닐까\s*싶음', text, re.S)):
        findings.append(('signal_color_hypothesis',
                         '처음에는 숫자별 색상이 달라 단순 색상 구분 기능일 것으로 추정했습니다.'))
    if (re.search(r'높은\s*숫자일\s*수록\s*매수신호', text)
            and re.search(r'낮은\s*숫자일수록\s*매도\s*신호', text)):
        findings.append(('signal_expert_explanation',
                         '후속 담당자 확인에서는 높은 숫자가 매수신호, 낮은 숫자가 매도신호라고 설명했습니다.'))
    if (re.search(r'매수/매도의\s*강도\s*\(중요도\)', text)
            and re.search(r'높은\s*숫자일수록\s*매수신호', text)
            and re.search(r'강도는\s*변경이\s*가능', text)):
        findings.append(('signal_customer_reply',
                         '최종 고객 회신은 신호의 매수·매도 강도(중요도)를 뜻하며 '
                         '사용자가 강도를 바꿀 수 있다고 안내했습니다.'))
    if (re.search(r'Drdata/Finddata.{0,100}일부\s*고정된\s*파일.{0,40}갱신', text, re.S)
            and re.search(r'종목별/주기별\s*매번\s*저장.{0,50}없는', text, re.S)):
        findings.append(('drdata_fixed_files',
                         'Drdata/Finddata의 일부 고정 파일은 설정이 갱신되지만, '
                         '종목·주기마다 새로 저장되는 구조는 아니라고 확인했습니다.'))
    if (re.search(r'차트를\s*초기화\s*할때\s*기본\s*설정의\s*템플릿', text)
            and '화면번호_시장타입_Chartinfo' in text
            and re.search(r'\[9861\].{0,90}시장타입별', text, re.S)
            and re.search(r'\[9882\].{0,100}시장구분이\s*없는', text, re.S)):
        findings.append(('drdata_market_templates',
                         '해당 ChartInfo는 차트 초기화 템플릿입니다. 9861은 시장별 파일을, '
                         '단일 시장 9882는 시장 구분 없는 파일을 사용합니다.'))
    if (re.search(r'여러군데\s*매체가\s*사용중이라\s*모두\s*확인은\s*불가능', text)
            and re.search(r'실제\s*제공하는\s*매체가\s*없는\s*화면번호라면\s*제거해도', text)):
        findings.append(('drdata_conditional_removal',
                         '모든 매체를 확인한 것은 아니며, 실제 제공 매체가 없는 화면번호의 '
                         '파일이라면 제거할 수 있다는 조건부 안내였습니다.'))
    if (re.search(r'17시\s*이후\s*발생하는\s*이슈', text)
            and re.search(r'아직\s*현상\s*확인\s*중', text)
            and re.search(r'과거.{0,80}비슷한\s*이슈.{0,80}선\s*검토\s*요청', text, re.S)):
        findings.append(('after_hours_initial_pending',
                         '처음에는 17시 이후 현상을 확인 중이었고, '
                         '과거 유사 건을 먼저 검토해 달라고 요청했습니다.'))
    if (re.search(r'kchart/data/st/db/exchange\.json\s*최신\s*파일이\s*반영되지\s*않', text)
            and re.search(r'KRX시간연장_ALL_2026-0903\.zip.{0,30}포함하여\s*전달', text, re.S)):
        findings.append(('exchange_json_missing',
                         '후속 확인에서 최신 exchange.json이 반영되지 않았고, '
                         '이 파일은 9월 3일 전달한 KRX시간연장 취합 zip에 포함돼 있었습니다.'))
    if (re.search(r'당시\s*전달드렸던\s*zip파일을\s*다시\s*첨부', text)
            and re.search(r'확인\s*부탁', text)):
        findings.append(('exchange_zip_resent',
                         '10월 13일 그 zip을 다시 첨부해 고객에게 확인을 요청했습니다. '
                         '고객 운영 정상화가 확인됐다는 기록은 아닙니다.'))
    if re.search(r'상세\s*수식\s*제공\s*시\s*가능', text):
        findings.append(('formula_required',
                         'CMF 추세 조건 추가는 상세 수식을 제공하면 가능하다는 조건부 답변입니다. '
                         '개발·QC·배포 완료 기록은 아닙니다.'))
    if (re.search(r'작업\s*가능하며.{0,180}함께\s*주중에\s*작업\s*진행', text, re.S)
            and re.search(r'테스트하여\s*전달드릴\s*수\s*있도록\s*하겠습니다', text)):
        findings.append(('window_size_work_plan',
                         '공급사는 상단 컨트롤 겹침 건과 함께 주중에 작업·테스트한 뒤 '
                         '전달할 수 있다고 회신했습니다. 당시에는 예정 단계였습니다.'))
    if (re.search(r'OnSelectedChart\s*이벤트', text)
            and re.search(r'chart\.setShowRegular\("YES"\)', text)
            and re.search(r'함수\s*추가\s*후\s*확인\s*부탁', text)):
        findings.append(('multichart_setting_requested',
                         '공급사는 OnSelectedChart 이벤트에 chart.setShowRegular("YES")를 '
                         '추가해 확인해 달라고 안내했습니다. 배포·고객 정상 확인 완료 기록은 아닙니다.'))
    return findings


def implementation_and_qc_findings(text):
    findings = []
    if ('TitleName' in text and '이어야 하지만' in text
            and '신고가' in text and '신저가' in text):
        findings.append(('mismatch', '후속 코드 확인에서 TitleName의 기대 표기와 실제 표기가 달랐습니다.'))
    if re.search(r'(?:tag|태그)\s*파일\s*수정', text, re.I):
        findings.append(('tag_changed', '태그 파일 수정 기록이 있습니다.'))
    if re.search(r'작업\s*예정', text) and re.search(r'QC.{0,30}예정', text, re.I):
        findings.append(('work_qc_planned', '마지막 일정 기록은 작업과 QC를 모두 예정 단계로 표시합니다.'))
    if (_confirmed_qc_done(text) and not re.search(
            r'(?:QC|검수).{0,20}(?:완료|통과)\s*(?:는|가|하지|못|않|아님|아니)', text)):
        findings.append(('qc_done', 'QC 완료 기록이 있습니다.'))
        findings.append(('initial_qc_done', '앞선 QC 완료 기록이 있습니다.'))
    if re.search(r'\[QC\s*요청\]', text, re.I):
        findings.append(('qc_requested', 'QC 요청 기록입니다. 이 기록만으로 QC 완료를 뜻하지는 않습니다.'))
    if (re.search(r'특정일\s*선택\s*가능한\s*조건\s*대상', text)
            and re.search(r'1영업일\s*전\s*날짜', text)
            and re.search(r'하루\s*전\s*날짜', text)):
        findings.append(('date_default_scope_changed',
                         '고객은 적용 범위를 특정일 선택 가능 조건 전체로 넓히고, '
                         '기본값은 1영업일 전을 우선 요청했습니다. 불가능하면 하루 전을 원했습니다.'))
    if (re.search(r'특정일\s*선택\s*가능한\s*조건\s*대상', text)
            and re.search(r'기본값을\s*[\'"“‘]?조회\s*시점의\s*1영업일\s*전\s*날짜', text)
            and re.search(r'수정\s*가능', text)):
        findings.append(('date_default_business_day_possible',
                         '공급사는 그 범위의 기본값을 조회 시점 1영업일 전으로 바꿀 수 있다고 회신했습니다.'))
    if (re.search(r'현재\s*개발완료\s*되었고', text)
            and re.search(r'QC\s*테스트\s*중', text)):
        findings.append(('development_done_qc_in_progress',
                         '개발 완료 뒤 QC 테스트 중이라고 안내한 기록입니다. 당시 고객 전달은 아직 예정 단계였습니다.'))
    if re.search(r'\[QC\s*NG\]', text, re.I):
        findings.append(('qc_ng', 'QC에서 오류가 확인되어 재검수가 필요했습니다.'))
    if re.search(r'\[QC\s*재요청\]|\[QC재요청\]', text, re.I):
        if re.search(r'재빌드', text):
            findings.append(('rebuild_qc_requested', '재빌드 뒤 QC를 다시 요청했습니다.'))
        else:
            findings.append(('qc_requested_again', 'QC를 다시 요청했습니다.'))
    if re.search(r'선행스팬\s*구간보다\s*미래영역이\s*더\s*클\s*경우\s*발생', text):
        findings.append(('future_span_mismatch',
                         '일목균형표의 선행스팬보다 미래여백이 클 때 추세선 위치 오차가 발생했습니다.'))
    if re.search(r'타\s*증권사에서도\s*동일하게\s*발생', text):
        findings.append(('other_broker_reproduced', '다른 증권사에서도 같은 현상이 확인됐습니다.'))
    if (re.search(r'구차트\s*컨버트와는\s*관련이\s*없는', text)
            and re.search(r'그래프\s*정보는\s*저장.{0,70}지표\s*\(STUDIES\)\s*정보는\s*저장되어\s*있지', text, re.S)):
        findings.append(('saved_graph_missing_studies',
                         '고객 파일에는 그래프만 저장되고 지표(STUDIES) 정보가 없었습니다. '
                         '구차트 전환 때문은 아니라고 판단했습니다.'))
    if (re.search(r'재접속하는\s*과정에서\s*지표\s*정보가.{0,40}가능성도\s*의심', text, re.S)
            and re.search(r'내부\s*테스트.{0,80}정상적으로\s*저장', text, re.S)):
        findings.append(('sleep_reconnect_unconfirmed',
                         '절전 뒤 재접속 과정의 저장 실패는 가설이었고, 같은 상황의 내부 테스트에서는 '
                         '지표가 정상 저장됐습니다. 원인은 확정되지 않았습니다.'))
    if (re.search(r'절전\s*모드를\s*사용하지\s*않고', text)
            and re.search(r'종료\s*처리하기로', text)
            and re.search(r'동일\s*현상이\s*발생할\s*경우\s*추가로\s*확인', text)):
        findings.append(('monitor_without_sleep_close',
                         '고객은 절전 모드를 쓰지 않고 지켜보기로 했고, 이슈는 종료 처리하되 '
                         '같은 현상이 재발하면 추가 확인하기로 했습니다.'))
    if (re.search(r'ExStatic\.dll.{0,100}오류\s*팝업', text, re.I | re.S)
            and re.search(r'SK\s*HTS에서는\s*다시\s*재현이\s*되지\s*않', text)):
        findings.append(('dll_reproduced_then_not',
                         '초기 공급사 PC에서는 재현되지 않았지만 후속 노트북에서는 ExStatic.dll 로드 오류를 '
                         '확인했습니다. 재부팅 뒤 SK HTS에서는 다시 재현되지 않았습니다.'))
    if (re.search(r'스마트\s*앱\s*컨트롤.{0,100}디지털\s*서명이\s*없는', text, re.S)
            and re.search(r'OS\s*수준의\s*실행\s*거부', text)):
        findings.append(('os_integrity_hypothesis',
                         '최종 회신은 스마트 앱 컨트롤의 시스템 무결성 정책이 서명되지 않은 DLL 로드를 '
                         '거부한 것으로 판단했습니다. 프로그램 로직 결함으로 확정하지 않았습니다.'))
    if (re.search(r'GetLastError\s*Code:\s*4551', text, re.I)
            and re.search(r'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION', text)):
        findings.append(('integrity_error_code',
                         '근거로 든 오류 코드는 GetLastError 4551 '
                         '(ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION)입니다.'))
    if (re.search(r'빌드\s*옵션\s*적용을\s*검토\s*중', text)
            and re.search(r'디지털\s*서명', text)
            and re.search(r'단독으로\s*처리가\s*어려운', text)):
        findings.append(('integrity_response_scope',
                         '빌드 옵션과 디지털 서명을 검토하되 배포·관리 권한 문제로 단독 처리는 어렵다고 '
                         '안내했습니다. 수정·배포 완료 기록은 아닙니다.'))
    if (re.search(r'2개\s*이상의\s*envelope\s*지표', text, re.I)
            and re.search(r'최상단\s*지표의\s*환경설정으로\s*다\s*적용', text)):
        findings.append(('envelope_qc_ng',
                         'QC에서는 Envelope 지표를 둘 이상 넣고 선을 삭제·설정하면 '
                         '다른 지표에도 최상단 지표의 설정이 적용되는 오류가 확인됐습니다.'))
    if (re.search(r'메인\s*헤더.{0,30}라이브러리\s*빠져.{0,15}요청', text, re.S)
            and re.search(r'소스\s*동기화\s*이후.{0,35}롤백한\s*작업\s*다시\s*머지\s*예정', text, re.S)):
        findings.append(('source_sync_before_remerge',
                         '즉시 재전달하지 못했습니다. 빠진 메인 헤더·라이브러리를 받아 소스를 동기화한 뒤 '
                         '롤백 작업을 다시 병합할 예정이라고 기록했습니다.'))
    if re.search(r'헤더\s*파일뿐\s*아니라\s*cpp\s*파일도\s*추가로\s*보내', text, re.I):
        findings.append(('header_cpp_requested', '후속 요청에서는 헤더와 cpp 파일을 함께 보내 달라고 했습니다.'))
    if re.search(r'\[고객사\s*전달\]', text):
        findings.append(('delivered', '고객사 전달 기록이 있습니다.'))
    elif re.search(r'\[전달\]', text):
        findings.append(('file_delivered', '파일 전달 기록이 있습니다.'))
    if re.search(r'\[전달\s*완료\]', text):
        findings.append(('delivery_completed', '고객사 전달 완료 기록이 있습니다.'))
    if (re.search(r'조건검색\s*소스\s*컴파일', text)
            and re.search(r'추가된\s*항목\s*정의가\s*누락.{0,35}컴파일\s*오류', text, re.S)
            and re.search(r'Condition_MngDef\.h.{0,35}누락된\s*것\s*같', text, re.S)):
        findings.append(('missing_definition_complaint',
                         '첫 전달 뒤 고객은 추가 항목 정의 누락으로 컴파일 오류가 났고, '
                         'Condition_MngDef.h 누락을 의심해 파일 재전송을 요청했습니다.'))
    if (re.search(r'개발\s*담당자.{0,35}휴가', text, re.S)
            and re.search(r'내일\s*복귀.{0,100}전달.{0,35}조치', text, re.S)):
        findings.append(('developer_followup_plan',
                         '담당 개발자의 다음 날 복귀 후 요청 내용을 전달·조치하겠다고 안내했습니다.'))
    if re.search(r'\[재전달\].{0,250}?(?:화|금|수|목|월|토|일)B_[^\s\\/]+\.zip', text, re.S):
        findings.append(('b_redelivery', 'B 패키지를 재전달했습니다.'))
    if (re.search(r'변수설정\s*그룹에\s*옵션을\s*추가', text)
            and re.search(r'괜찮을지\s*문의', text)):
        findings.append(('variable_group_proposal', '처음에는 변수설정 그룹에 옵션을 넣는 방안을 제안했습니다.'))
    if (re.search(r'리스트박스\s*크기를\s*조정.{0,30}체크박스\s*추가', text, re.S)
            and re.search(r'(?:(?:위\s*)?방법으로\s*요청|방법이\s*좋아)', text)):
        findings.append(('listbox_checkbox_choice',
                         '고객은 리스트박스 크기를 조정해 체크박스를 추가하는 방안을 요청했습니다.'))
    if (re.search(r'\[QC\s*NG\]', text, re.I)
            and re.search(r'\[\s*esc\s*\].{0,60}HTS\s*비정상\s*종료', text, re.I | re.S)):
        findings.append(('initial_esc_qc_ng', 'QC에서 ESC로 설정 화면을 닫을 때 HTS 비정상 종료를 확인했습니다.'))
        findings.append(('esc_qc_ng', '후속 QC에서도 ESC 종료 시 HTS 비정상 종료가 재확인됐습니다.'))
    if (re.search(r'NG\s*현상은\s*기존\s*오류', text)
            and re.search(r'프로젝트\s*소스에서\s*수정.{0,50}대체거래소때\s*반영\s*예정', text, re.S)
            and re.search(r'기존\s*오류\s*추가\s*수정\s*부분은\s*롤백\s*후\s*전달', text, re.S)):
        findings.append(('esc_existing_fix_rollback',
                         'ESC NG는 기존 오류로 분류했고 프로젝트 소스의 수정은 대체거래소 때 반영 예정이라고 적었습니다. '
                         '이번 추가 수정분은 롤백한 뒤 전달했습니다.'))
    if (re.search(r'고객사\s*롤백\s*요청', text)
            and re.search(r'데이터\s*파일만\s*변경하여\s*전달\s*예정|데이터파일만\s*변경하여\s*전달\s*예정', text)):
        findings.append(('data_file_only_rollback_plan',
                         '고객사 롤백 요청에 따라 데이터 파일만 변경해 전달할 예정이라고 기록했습니다.'))
    if (re.search(r'\[고객사\s*전달\]', text)
            and re.search(r'(?:화|금|수|목|월|토|일)B_[^\s\\/]+\.zip', text)):
        findings.append(('b_package_delivered', '후속 B 패키지의 고객사 전달 기록이 있습니다.'))
    if (re.search(r'4/18.{0,60}4/22.{0,60}4/23.{0,60}4/24.{0,60}4/26.{0,90}소스\s*병합', text, re.S)
            and re.search(r'4/17\s*신종증권.{0,110}메인\s*헤더\s*및\s*라이브러리.{0,70}롤백\s*처리', text, re.S)
            and re.search(r'미래에셋.{0,45}소스에서도\s*이전\s*버전으로\s*롤백\s*처리\s*부탁', text, re.S)):
        findings.append(('merged_source_exclusion',
                         '4/18·22·23·24·26 수정 소스를 병합해 전달했고, 공급사 내부의 4/17 신종증권 메인 헤더·'
                         '라이브러리·작업 소스는 롤백했습니다. 고객사 내부 소스 롤백은 요청 단계였습니다.'))
    if (re.search(r'#206에\s*적용\s*완료', text)
            and re.search(r'수정처리가\s*제대로\s*안', text)
            and re.search(r'kChartSetupDlgAddin\s*에러', text)):
        findings.append(('merged_source_followup_failure',
                         '고객은 취합본을 206번 대상에 적용했지만 4번 수정이 제대로 되지 않았고 '
                         'kChartSetupDlgAddin 오류도 났다고 재문의했습니다.'))
    if (re.search(r'문의주신\s*4번\s*항목', text)
            and re.search(r'5대\s*PC.{0,90}느린\s*현상.{0,25}재현\s*되지\s*않', text, re.S)
            and re.search(r'영상\s*요청', text)
            and re.search(r'PC\s*사양', text)
            and re.search(r'사양\s*요청', text)
            and re.search(r'User\s*파일\s*요청', text, re.I)):
        findings.append(('slow_symptom_unreproduced',
                         '공급사는 5대 PC에서 4번 항목의 느린 현상을 재현하지 못해 영상·PC 사양·User 파일을 요청했습니다.'))
    return findings


def followup_investigation_findings(all_text, text, source_text):
    findings = []
    if re.search(r'클린\s*후\s*재빌드\s*요청', text):
        findings.append(('clean_rebuild_requested', '후속 통화에서 문제 작업을 클린 후 재빌드해 보라고 요청했습니다.'))
    if (re.search(r'툴팁기능\s*적용\s*후\s*장애', text)
            and re.search(r'저희쪽에서\s*먼저\s*원복처리', text)
            and re.search(r'4/29\s*수정\s*취합본에\s*이문수.{0,25}수정본까지\s*취합하여\s*재전달', text, re.S)):
        findings.append(('tooltip_reverted_remerge_requested',
                         '마지막 고객 회신은 툴팁 적용 뒤 장애가 재발해 고객사가 먼저 원복했다고 밝히고, '
                         '4/29 취합본에 이문수 수정본을 합쳐 재전달해 달라는 요청이었습니다.'))
    if (re.search(r'날짜만\s*1800년대로\s*수정', text)
            and re.search(r'엑셀\s*불러오기.{0,40}정상적으로\s*불러오', text, re.S)
            and re.search(r'2번\s*이슈만\s*수정\s*진행', text)):
        findings.append(('excel_scope_narrowed',
                         '자체 저장 엑셀의 날짜를 1800년대로 바꾼 파일은 불러와져, 당시 수정 범위는 2번 거래대금 현상으로 좁혔습니다.'))
    if re.search(r'고객\s*샘플파일.{0,90}DateTime\s*형식이\s*맞지\s*않', text, re.I | re.S):
        findings.append(('customer_datetime_mismatch',
                         '이후 고객 샘플에서는 차트와 DateTime 형식이 맞지 않는다는 오류가 확인됐습니다.'))
    if (re.search(r'과거\s*데이터.{0,45}x축에\s*날짜가\s*(?:안나오|안나옵|표시되지)', text, re.S)
            and re.search(r'거래대금\s*지표\s*데이터가\s*있는\s*엑셀.{0,45}그려지지\s*않', text, re.S)):
        findings.append(('excel_post_delivery_questions',
                         '전달 뒤 고객은 과거 날짜의 X축 미표시와 엑셀 거래대금 지표 미표시를 다시 문의했습니다.'))
    if (re.search(r'시간자료형이\s*1970년\s*1월\s*1일을\s*기준', text)
            and re.search(r'스케일영역\s*계산.{0,180}수정이\s*(?:어렵|어려)', text, re.S)):
        findings.append(('old_date_axis_limit',
                         '최종 회신은 1970년 기준 시간 자료형과 차트 내부 스케일 계산 때문에 과거 날짜의 X축 표시 수정이 어렵다고 설명했습니다.'))
    if (re.search(r'거래대금과\s*같은\s*시장지표', text)
            and re.search(r'조회성\s*데이터.{0,60}조회하여\s*차트에\s*데이터가\s*들어가는\s*구조', text, re.S)):
        findings.append(('excel_market_indicator_limit',
                         '거래대금 같은 시장지표는 별도 조회 데이터를 차트에 넣는 구조여서 엑셀 데이터 표시 수정이 어렵다고 답했습니다.'))
    if re.search(r'소스\s*프리징.{0,30}보류\s*및\s*롤백', text, re.S):
        findings.append(('freeze_rollback', '소스 프리징으로 작업이 보류·롤백됐다는 기록이 있습니다.'))
    if (re.search(r'강세\s*:\s*RSI\(적용기간\)\s*>=\s*SPercent', text)
            and re.search(r'약세\s*:\s*RSI\(적용기간\)\s*<\s*LPercent', text)
            and re.search(r'Period\(14\).{0,30}LPercent\(30\).{0,30}SPercent\(70\)', text, re.S)):
        findings.append(('rsi_thresholds',
                         'RSI 강세는 RSI(기간 14)가 SPercent(기본 70) 이상, '
                         '약세는 LPercent(기본 30) 미만으로 구분했습니다.'))
    if (re.search(r'운영\s*HTS.{0,50}수정된\s*모듈.{0,30}적용', text, re.S)
            and re.search(r'업종\s*시장\s*변경.{0,30}PctrSM\.dll.{0,30}비정상\s*종료', text, re.S)
            and re.search(r'컴파일한\s*PctrSM\.dll.{0,25}종료현상\s*발생하지\s*않', text, re.S)):
        sentence = ('작업 중 운영 HTS 적용본은 업종·시장 변경 시 PctrSM.dll에서 비정상 종료됐지만 '
                    '자체 컴파일본에서는 재현되지 않았습니다.')
        if re.search(r'GetCodeUpIdx.{0,30}원인인\s*것으로\s*보', text, re.S):
            sentence += ' GetCodeUpIdx 관련성은 당시 추정이었습니다.'
        findings.append(('rsi_interim_crash', sentence))
    if (re.search(r'좌측\s*탭.{0,150}태양금속.{0,150}하나로FN.{0,100}SK하이닉스', text, re.S)
            and re.search(r'차트.{0,80}태양금속.{0,20}4\s*개.{0,30}2026선물.{0,20}1\s*개', text, re.S)):
        findings.append(('screenshot_mismatch',
                         '첨부 사진에서 좌측 탭에는 태양금속·하나로FN K-반도체·SK하이닉스가 '
                         '있지만 차트에는 태양금속 4개와 2026선물 1개 등이 보여 구성이 달랐습니다.'))
    if (re.search(r'현상.{0,25}(?:발생되지\s*않|재현되지\s*않)', text, re.S)
            and re.search(r'우선.{0,10}종료\s*처리', text, re.S)
            and re.search(r'(?:추가적인\s*정보|유저파일).{0,30}재접수.{0,20}다시\s*확인', text, re.S)):
        findings.append(('unreproduced_reopen',
                         '테스트에서 재현되지 않아 우선 종료하되, 추가 정보나 유저파일이 오면 '
                         '재접수해 다시 확인하겠다고 안내했습니다.'))
    if (re.search(r'분주기\s*선택.{0,30}않아도.{0,30}170000', text, re.S)
            and re.search(r'서버.{0,40}170000.{0,30}분주기로\s*판단', text, re.S)
            and re.search(r'1분\s*데이터\s*기반.{0,30}일간\s*데이터\s*생성', text, re.S)):
        findings.append(('minute_as_daily',
                         '분주기를 고르지 않아도 170000을 보내 서버가 분주기로 판단했고, '
                         '1분 데이터로 당일 일간 데이터를 생성해 거래량 차이가 났습니다.'))
    if (re.search(r'분주기\s*선택하지\s*않을\s*시.{0,50}999999', text, re.S)
            and re.search(r'(?:수정\s*사항|설정\s*조회로\s*수정)', text)):
        findings.append(('initial_999999_direction',
                         '초기 수정 방향에는 분주기 미선택 시 과거시간 999999 조회가 적혔습니다.'))
    if (re.search(r'분주기\s*미체크\s*시.{0,20}["“]?170000["”]?', text, re.S)
            and re.search(r'170000["”]?\s*(?:->|→)\s*["“]?000000', text)
            and re.search(r'변경하였', text)):
        findings.append(('final_000000_screen',
                         '후속 화면 확인에서는 미체크 시 분석시점 시간을 170000에서 000000으로 '
                         '바꿨다고 기록했습니다.'))
    if (re.search(r'1분\s*데이터.{0,35}거래량\s*합.{0,20}일간\s*거래량.{0,15}맞지\s*않', text, re.S)
            and re.search(r'원인의\s*3번.{0,35}확인\s*해보기로', text, re.S)):
        findings.append(('volume_followup', '1분 거래량 합과 일간 거래량의 차이는 별도 확인 대상으로 남겼습니다.'))
    if (re.search(r'100\s*건\s*제한은.{0,50}실시간\s*업데이트\s*시\s*부하', text, re.S)
            and re.search(r'100\s*건\s*미만', all_text)):
        findings.append(('load_limit_context',
                         '100건 제한은 화면 업데이트 부하 때문이라는 설명이며, '
                         '100건 미만 미갱신의 원인으로 확정된 기록은 아닙니다.'))
    if (re.search(r'1951.{0,250}1950.{0,250}두\s*이슈', text, re.S)
            and re.search(r'두\s*이슈.{0,30}원인을\s*찾고\s*수정\s*작업\s*중', text, re.S)):
        findings.append(('two_issues_work_in_progress',
                         '1951을 포함한 두 이슈는 당시 원인을 찾고 수정 작업 중이라고 안내했습니다.'))
    if (re.search(r'ASSERT는\s*디버깅\s*모드에서만\s*확인\s*가능', text)
            and re.search(r'릴리즈\s*모드인\s*운영\s*버전.{0,35}오류\s*발생\s*영향', text, re.S)):
        findings.append(('debug_assert_release_risk',
                         'DEBUG ASSERT는 디버그 환경에서만 보이지만 운영 버전에도 '
                         '오류 영향이 있을 수 있어 확인이 필요하다고 봤습니다.'))
    if re.search(r'소리마치(?:쪽)?.{0,20}확인\s*필요.{0,35}종료\s*처리', text, re.S):
        findings.append(('sorimachi_owner',
                         '초기 분류에서는 소리마치 측 확인 건으로 보고 우리 쪽 이슈를 종료 처리했습니다.'))
    if re.search(r'운영\s*카이로스.{0,100}ASSERT\s*는?\s*발생하지\s*않', text, re.S):
        findings.append(('production_assert_not_reproduced',
                         '운영 카이로스에서 반복 검색했지만 ASSERT는 재현되지 않았습니다.'))
    if (re.search(r'데이터\s*조회가\s*클\s*때.{0,50}delete\s*루틴.{0,25}발생', text, re.I | re.S)
            and re.search(r'릴리즈\s*환경.{0,100}비정기적으로\s*죽는\s*현상', text, re.S)):
        sender = '이준옥의 ' if re.search(r'From\s*[.：:]\s*이준옥', source_text, re.I) else ''
        findings.append(('delete_routine_risk',
                         f'마지막 {sender}분석은 대량 조회 후 delete 루틴에서 문제가 생기며, '
                         '릴리즈에서 겉보기 정상이어도 비정기 종료를 일으킬 수 있다는 내용입니다.'))
    if (re.search(r'현재\s*신차트\(3600\).{0,80}모든\s*지표.{0,45}반올림', text, re.S)
            and re.search(r'CCI\s*지표\s*값.{0,35}신/구\s*차트가\s*다르게\s*표시', text, re.S)):
        findings.append(('cci_initial_rounding',
                         '초기에는 신차트 3600의 지표 반올림 처리 때문에 신·구 CCI 값이 다르다고 안내했습니다.'))
    if (re.search(r'지표\s*검증식\s*엑셀.{0,50}신차트\s*CCI\s*지표값', text, re.S)
            and re.search(r'동일하게\s*표시되는\s*것\s*확인', text)):
        findings.append(('cci_excel_match', '후속 검증식 엑셀과 신차트 CCI 값이 일치함을 확인했습니다.'))
    if (re.search(r'CCI\s*지표\s*계산식과\s*같이\s*정상\s*표시', text)
            and re.search(r'구차트인\s*3100\s*화면은\s*한국금융IT에서\s*제공하는\s*차트가\s*아니', text, re.S)
            and re.search(r'값이\s*다르게\s*표시되는\s*원인은\s*알\s*수\s*없', text, re.S)):
        findings.append(('cci_final_scope',
                         '최종 확인에서 신차트 CCI는 계산식대로 정상 표시됐고, 고객이 말한 '
                         '구차트 3100은 한국금융IT 제공 차트가 아니었습니다. '
                         '두 값이 다른 정확한 원인은 알 수 없다고 안내했습니다.'))
    normal_statement = _confirmation_statement(text)
    if normal_statement:
        findings.append(('normal_confirmed', f'해당 시점의 정상 확인 결과: “{_label(normal_statement)}”'))
    if re.search(r'표시.{0,20}안되|표시.{0,20}안\s*되|안나온|재문의', text):
        findings.append(('requery', '그 뒤 표시 문제가 다시 제기됐습니다.'))
    if re.search(r'다시\s*전달.{0,40}확인|재전달.{0,30}확인', text, re.S):
        findings.append(('recheck', '최신 기록은 자료를 다시 전달받아 재확인하겠다는 단계입니다.'))
    if re.search(r'체결량이\s*(?:존재하면|있으면).{0,30}실시간\s*처리', text):
        findings.append(('cause_processing', '체결량이 있으면 실시간 처리하는 동작이 원인 근거로 기록됐습니다.'))
    if (re.search(r'p_buf.{0,30}빈\s*문자열', text, re.S)
            and re.search(r'CheckValidSize.{0,20}FALSE', text, re.S)
            and re.search(r'pDataBuf.{0,20}pStartBuf.{0,50}NULL', text, re.S)
            and re.search(r'비정상\s*종료.{0,20}(?:보임|것으로)', text, re.S)):
        findings.append(('cause_null_chain',
                         '조사에서는 빈 p_buf로 CheckValidSize가 FALSE를 반환해 '
                         'pDataBuf·pStartBuf가 NULL로 남고 비정상 종료되는 것으로 봤습니다.'))
    if (re.search(r'실시간.{0,30}조회\s*기준\s*200\s*개까지\s*지원', text, re.S)
            and re.search(r'그리드.{0,20}100\s*개까지만\s*관리', text, re.S)
            and re.search(r'그리드에\s*없는\s*항목.{0,35}(?:예상|같음|추정)', text, re.S)):
        findings.append(('grid_cap_hypothesis',
                         '실시간 조회는 200개까지 지원하지만 그리드는 100개까지만 관리해 '
                         '그리드 밖 종목의 신호가 집계 차이를 낸다는 추정이 기록됐습니다.'))
    if (re.search(r'SetScrollInfo.{0,100}직접(?:적인)?\s*원인이라기보다는', text, re.I | re.S)
            and re.search(r'(?:깨진\s*메모리|비정상\s*상태|잘못된\s*상태).{0,100}(?:크래시|보임|판단)', text, re.S)):
        findings.append(('setscroll_hypothesis',
                         'SetScrollInfo는 크래시가 관찰된 지점이며 직접 원인으로 확정하지 않았습니다. '
                         '비정상 상태에서 진입했을 가능성으로 판단했습니다.'))
    if (re.search(r'CScrollBarEx::SetScrollInfo\(\).{0,40}예방\s*코드(?:를)?\s*추가', text, re.S)
            and all(token in text for token in ('GetSafeHwnd', 'IsWindow', 'SIF_ALL', 'SIF_RANGE'))
            and re.search(r'(?:Max/Range|Range).{0,100}(?:range/page/pos|Page/Pos)', text, re.I | re.S)):
        findings.append(('setscroll_defenses',
                         'SetScrollInfo에 핸들 유효성(GetSafeHwnd·IsWindow), Range/Page/Pos 값, '
                         '마스크(SIF_ALL 대신 SIF_RANGE·SIF_PAGE·SIF_POS) 방어 코드를 추가했다고 기록했습니다.'))
    if (re.search(r'\bTileBlt\b.{0,100}nSourWid.{0,40}음수', text, re.I | re.S)
            and re.search(r'무한\s*루프', text)):
        findings.append(('tileblt_report',
                         '후속 고객 테스트에서는 TileBlt의 nSourWid가 음수가 되어 무한루프가 '
                         '발생했다고 보고하고 점검·수정을 요청했습니다.'))
    if (re.search(r'엑셀\s*불러오기\s*기능', text)
            and re.search(r'해당\s*현상은\s*아닌지\s*확인|사용하였는\s*것인지', text, re.S)):
        findings.append(('excel_hypothesis',
                         '엑셀 불러오기는 유사 현상을 재현한 조사 가설이었으며 고객 사용 여부를 확인했습니다.'))
    if ('엑셀' in all_text and re.search(r'해당\s*기능은\s*사용자가\s*사용하지\s*않', text)):
        findings.append(('customer_no_excel', '고객은 엑셀 불러오기 기능을 사용하지 않았다고 답했습니다.'))
    if re.search(r'요청\s*주신\s*현상은\s*재연이\s*되고\s*있지\s*않', text):
        findings.append(('issue_not_reproduced', '마지막 재확인에서는 요청된 현상이 재현되지 않았습니다.'))
    if (re.search(r'우선\s*종료\s*처리', text)
            and re.search(r'재발생.{0,100}재현\s*스텝.{0,60}로그\s*파일', text, re.S)
            and re.search(r'진행\s*예정', text)):
        findings.append(('provisional_close',
                         '우선 종료하고, 재발생 시 재현 단계 등의 정보를 받아 로그 파일을 '
                         '만드는 방향으로 진행할 예정이라고 기록했습니다.'))
    if (re.search(r'\[0156\].{0,150}\[0662\]', text, re.S)
            and re.search(r'일\s*,\s*주\s*,\s*월\s*,\s*년\s*,\s*초\s*주기.{0,30}정상\s*조회', text, re.S)
            and re.search(r'분\s*,\s*틱\s*주기.{0,30}비정상\s*종료', text, re.S)):
        tr = re.search(r'\bTR\s*[:：]\s*(IVU10060)\b', text, re.I)
        sentence = '마지막 재확인에서 일·주·월·년·초 주기는 정상 조회됐지만 분·틱 주기는 비정상 종료됐습니다.'
        if tr:
            sentence += f' 조회 TR은 {_label(tr.group(1))}으로 기록됐습니다.'
        findings.append(('periodic_recheck', sentence))
    return findings


def delivery_and_customer_findings(row, text, source_text):
    findings = []
    field_plan = re.search(r'구분자\((\d{2,4})\).{0,100}매매구분.{0,60}추가.{0,25}보내려고', text, re.S)
    if field_plan:
        findings.append(('field_plan', f'거래소 구분자({field_plan.group(1)})에 매매구분을 추가해 보내겠다는 계획이 기록됐습니다.'))
    if re.search(r'봉\s*개수\s*제한으로\s*검색\s*안되', text):
        findings.append(('count_limit', '봉 개수 제한 때문에 검색되지 않았다는 원인 기록이 있습니다.'))
    if re.search(r'서버\s*담당자.{0,30}작업.{0,20}완료', text):
        findings.append(('server_work_done', '서버 작업 완료 기록이 있습니다.'))
    if re.search(r'운영\s*적용\s*예정', text):
        findings.append(('operation_planned', '운영 적용은 당시 예정으로 안내됐습니다.'))
    if (re.search(r'수정\s*파일\s*전달', text)
            and re.search(r'아직.{0,30}운영\s*버전.{0,25}반영하지\s*않', text)):
        findings.append(('operation_not_applied', '수정 파일은 전달했지만 당시 운영 버전에는 아직 반영되지 않았다고 기록됐습니다.'))
    if (re.search(r'차트\s*로드한\s*직후.{0,80}차트\s*OCX', text, re.S)
            and re.search(r'추세선.{0,100}메모리에\s*copy.{0,60}누락', text, re.S | re.I)):
        findings.append(('cause_missing_copy',
                         '확인된 원인은 차트 로드 직후 OCX 추세선 값을 공유 메모리에 복사하는 처리가 누락된 것입니다.'))
    planned_delivery = re.search(r'수정\s*작업\s*및\s*QC.{0,70}후\s*(\d{1,2}월\s*\d{1,2}일).{0,20}전달.{0,10}예정', text, re.S | re.I)
    if planned_delivery:
        findings.append(('delivery_planned',
                         f'{_label(planned_delivery.group(1))} 전달은 수정·QC 이후의 예정으로 안내됐습니다.'))
    if re.search(r'메일\s*수신', source_text) and re.search(r'정상적으로\s*동작하지\s*않', text):
        findings.append(('customer_requery', '고객이 전달본이 정상 동작하지 않는다고 재문의했습니다.'))
    rollback_file = re.search(r'롤백\s*소스\s*1개\s*파일.{0,160}?[/\\]([\w.-]+\.(?:cpp|c|h|py|js))', text, re.S | re.I)
    if rollback_file and re.search(r'다시\s*보내|재전달|다시\s*전달', text):
        filename = _label(rollback_file.group(1))
        requester = re.search(r'([가-힣]{2,5})\s*매니저(?:님)?\s*요청(?:사항)?', text)
        prefix = f'{_label(requester.group(1))} 요청으로 ' if requester else ''
        sentence = f'{prefix}{filename} 한 파일을 롤백해 다시 전달했습니다.'
        if re.search(r'그\s*외에는\s*직전\s*파일과\s*동일', text):
            sentence += ' 나머지는 직전 파일과 같았습니다.'
        findings.append(('single_file_rollback', sentence))
    revision_rollback = re.search(r'(?:소스\s*동기화\s*과정에서).{0,50}revision\s*(\d+)\s*롤백', text, re.S | re.I)
    if revision_rollback:
        findings.append(('revision_rollback',
                         f'후속 소스 동기화에서 revision {revision_rollback.group(1)} 롤백 기록이 있습니다.'))
    if re.search(r'비정상\s*종료.{0,30}재접수.{0,30}수정\s*후\s*발송', text, re.S):
        findings.append(('later_dispatch', '비정상 종료로 재접수된 내용을 수정해 발송했다는 마지막 기록이 있습니다.'))
    if re.search(r'블록\s*시세\s*처리\s*되어\s*있음|블록\s*처리도\s*되어\s*있', text):
        findings.append(('block_already_applied', '블록시세 처리는 문의 당시 이미 적용돼 있었습니다.'))
    if re.search(r'개발\s*서버에\s*서버\s*개발\s*완료', text):
        findings.append(('server_dev_completed', '개발서버의 서버 개발 완료 기록이 있습니다.'))
    if (re.search(r'\bETN\s*구분자\s*바로\s*뒤(?:의)?\s*1\s*바이트', text, re.I)
            and re.search(r'\bsPF\s*\[\s*1\s*\]', text, re.I)
            and '상장형수익증권' in text):
        findings.append(('server_field_layout',
                         '서버 TR 출력에서 ETN 구분자 바로 뒤 1바이트를 상장형수익증권 구분자로 두고 sPF[1] 필드를 사용했습니다.'))
    if re.search(r'화면\s*개발\s*예정', text) and re.search(r'QC\s*예정', text, re.I):
        findings.append(('screen_qc_planned', '화면 개발과 QC는 당시 예정이었고 일정 확인 중이었습니다.'))
    if re.search(r'관련\s*수정\s*내역.{0,20}확인\s*부탁', text, re.S) and re.search(r'mail\s+to', source_text, re.I):
        findings.append(('change_notice', '수정 내역 안내 메일이 기록돼 있습니다.'))
    if re.search(r'HTS\s*분봉에서도\s*(?:DeMark\s*)?지표\s*표시되는\s*것\s*확인', text, re.I):
        findings.append(('intraday_supported', 'HTS에서도 DeMark 지표의 분봉 표시가 가능합니다.'))
    if (re.search(r'1\s*,\s*2\s*분봉', text)
            and re.search(r'지표가\s*표시되지\s*않아\s*확인\s*필요', text)):
        findings.append(('intraday_initial_issue', '초기에는 1·2분봉에서 표시되지 않아 추가 확인이 필요하다고 기록했습니다.'))
    if (re.search(r'1\s*,\s*2\s*분\s*주기에서도', text)
            and re.search(r'DeMark\s*지표.{0,25}정상\s*조회', text, re.I | re.S)):
        findings.append(('intraday_correction', '후속 확인에서는 1·2분봉도 정상 조회된다고 정정했습니다.'))
    bar_example = re.search(r'1\s*분봉.{0,30}?(\d{3,5})\s*개.{0,30}5\s*분봉.{0,30}?(\d{2,5})\s*개\s*이상',
                            text, re.S)
    if bar_example:
        findings.append(('intraday_bar_example',
                         f'후속 조회 예시는 1분봉 {bar_example.group(1)}개, 5분봉 {bar_example.group(2)}개 이상입니다. '
                         '이 수치를 모든 날짜의 절대 기준으로 일반화할 수는 없습니다.'))
    if (re.search(r'값을\s*보려는\s*일자\s*기준\s*전일의\s*데이터가\s*모두', text)
            and re.search(r'전체\s*봉', text)):
        findings.append(('prior_day_all_bars', '표시하려는 일자의 전일 전체 봉이 차트에 조회돼 있어야 합니다.'))
    if (re.search(r'차트.{0,40}2000\s*개\s*이상\s*조회.{0,30}(?:앱\s*)?크래시', text, re.I | re.S)
            and re.search(r'Android\s*/\s*IOS\s*둘\s*다\s*2000\s*개로\s*제한.{0,30}모듈\s*전달', text, re.I | re.S)):
        findings.append(('both_platforms_limited',
                         '2000봉 이상 조회 크래시 대응으로 Android/iOS 모두 2000개 제한 모듈을 전달했습니다.'))
    if (re.search(r'안드로이드는\s*오류가\s*발생하지\s*않', text)
            and re.search(r'2000\s*개\s*제한\s*없이\s*그대로\s*유지\s*부탁', text)):
        findings.append(('android_unlimited_requested',
                         '고객은 Android에서는 오류가 없어 2000개 제한 없이 유지해 달라고 요청했습니다.'))
    if (re.search(r'crash가\s*발생하는\s*함수를\s*제거', text, re.I)
            and re.search(r'봉\s*개수는\s*제한을\s*두지\s*않', text)):
        findings.append(('unlimited_trial',
                         '크래시 함수를 제거한 무제한 대안 모듈도 시험했지만, 이 시점에는 고객 환경의 정상 동작이 확정되지 않았습니다.'))
    trial_recurrence = re.search(r'(20\d{2})[/.](\d{1,2})[/.](\d{1,2})\s*적용.{0,40}오류가\s*발생', text, re.S)
    if (trial_recurrence and re.search(r'주봉은\s*문제가\s*안되.{0,35}일봉만\s*문제', text, re.S)):
        _year, month, day = trial_recurrence.groups()
        findings.append(('unlimited_recurred',
                         f'{int(month)}월 {int(day)}일 적용본에서 고객이 다시 오류를 보고했고, '
                         '주봉은 괜찮지만 일봉에서 문제로 보인다고 했습니다.'))
    market_test = re.search(r'자체\s*빌드\s*버전.{0,80}2000\s*개를\s*초과해도\s*문제없이\s*조회.{0,80}'
                            r'마켓\s*버전.{0,30}(\d{4,5})\s*개\s*이상\s*조회\s*시.{0,25}크래시', text, re.S)
    if market_test:
        findings.append(('market_threshold',
                         f'개발자 테스트에서 자체 빌드는 2000개 초과도 정상 조회됐지만, '
                         f'마켓 버전은 {market_test.group(1)}개 이상에서 크래시가 확인됐습니다.'))
    if (re.search(r'크래시\s*리포트.{0,60}로그.{0,30}확인\s*부탁', text, re.S)
            and re.search(r'그\s*전까지는\s*이전(?:에\s*전달드린)?\s*모듈.{0,50}2000\s*개\s*제한', text, re.S)):
        findings.append(('limited_fallback',
                         '추가 크래시 로그를 요청하며 그전까지는 이전 2000개 제한 모듈을 쓰라고 안내했습니다.'))
    if (re.search(r'2000\s*개\s*봉\s*제한은\s*정상적으로\s*동작', text)
            and re.search(r'선적용해두고.{0,30}로그\s*추가\s*첨부', text, re.S)):
        findings.append(('limited_applied', '고객은 2000개 제한을 선적용했고 정상 동작해 로그를 추가 첨부했다고 회신했습니다.'))
    if (re.search(r'28\.05', text) and re.search(r'2\.805', text)
            and re.search(r'서버\s*제공.{0,15}봉\s*데이터\s*단위\s*확인', text, re.S)
            and re.search(r'소수점\s*둘째?\s*자리.{0,80}차트\s*수정', text, re.S)):
        findings.append(('server_chart_roles',
                         'Y축 28.05와 실제 2.805의 단위 차이는 서버 제공 봉 데이터 확인·수정 문제이고, '
                         '두 자리 표시 문제는 별도의 차트 수정이 필요하다고 구분했습니다.'))
    if (re.search(r'테스트\s*서버\s*121.{0,50}2\.805로\s*내려오도록.{0,30}수정', text, re.S)
            and re.search(r'2\s*자리\s*또는\s*반올림', text)
            and re.search(r'종목\s*코드.{0,20}(?<![A-Za-z0-9])NB(?![A-Za-z0-9])', text, re.S)):
        findings.append(('server_value_then_chart_digits',
                         '고객은 테스트서버 121에서 2.805로 내려오게 했지만 차트는 여전히 두 자리 또는 반올림 표시됐고, '
                         '국채 종목코드 NB로 구분하자고 제안했습니다.'))
    if (re.search(r'종목\s*코드.{0,80}소수점\s*3\s*자리', text, re.S)
            and re.search(r'수정\s*후\s*전달.{0,20}겠습니다', text, re.S)):
        findings.append(('chart_three_digit_plan', '종목코드를 이용한 차트 소수점 세 자리 표시 수정과 전달을 계획했습니다.'))
    if re.search(r'다크\s*모드\s*작업\s*롤백.{0,30}재확인\s*필요', text):
        findings.append(('rollback_recheck', '다크모드 작업 롤백으로 QC 재확인을 요청했습니다.'))
    if re.search(r'[Yy]\s*축\s*소수점은\s*제대로\s*표시.{0,30}종목명이\s*표시가\s*안되', text, re.S):
        findings.append(('name_missing_after_delivery',
                         '전달 뒤 고객은 Y축 소수점은 제대로 보이지만 종목명이 표시되지 않는다고 보고했습니다.'))
    if re.search(r'운영에\s*넣고.{0,50}조회도\s*안되고\s*빈\s*화면', text, re.S):
        findings.append(('operation_blank_after_delivery',
                         '같은 날 후속 보고에서는 운영에서 조회되지 않고 빈 화면이 나온다고 했습니다.'))
    if re.search(r'연관\s*모듈이\s*왠지.{0,25}필요할\s*거\s*같', text, re.S):
        findings.append(('module_hypothesis', '연관 모듈 필요성은 고객의 추정으로만 기록됐으며 확정 원인은 아닙니다.'))
    if ('NH투자증권' in text and re.search(r'제공.{0,10}데이터를\s*사용', text, re.S)
            and re.search(r'제공.{0,15}데이터가\s*0으로', text, re.S)):
        findings.append(('provider_zero_data',
                         '조건검색은 NH가 제공한 데이터를 사용했고, 확인 당시 제공값 자체가 0이었습니다.'))
    if re.search(r'추후\s*저희\s*수정이\s*필요하면\s*진행', text):
        findings.append(('own_fix_conditional', '우리 쪽 수정은 필요할 때 진행하겠다는 조건부 안내였습니다.'))
    if (re.search(r'유동부채\s*요약', text)
            and re.search(r'피딩\s*서버\s*전환\s*\(\s*3월\s*말\s*\)', text)
            and re.search(r'제조업\s*/\s*기타\s*업종.{0,15}한하여\s*제공', text, re.S)):
        findings.append(('provider_liability_scope',
                         '3월 말 피딩서버 전환 뒤 유동부채 제공 대상은 제조업·기타 업종으로 바뀌었다고 전달했습니다.'))
    if (re.search(r'GAAP\s*재무\s*데이터.{0,25}보험\s*업종', text, re.S)
            and re.search(r'유동\s*/\s*비유동.{0,25}구분이?\s*없이', text, re.S)
            and re.search(r'보험업.{0,15}유동부채.{0,80}제공\s*안', text, re.S)):
        findings.append(('gaap_insurance_exclusion',
                         'FN가이드 설명상 GAAP 보험업 재무데이터는 유동/비유동 구분이 없어 '
                         '보험업 유동부채 정보를 제공하지 않습니다.'))
    if (re.search(r'조건검색에서도\s*데이터를\s*제공해주지\s*않아', text)
            and re.search(r'추후\s*정상\s*데이터를\s*제공해준다면.{0,35}조건검색에서도\s*정상\s*처리', text, re.S)):
        findings.append(('normal_provider_data_conditional',
                         '해당 보험업 유동부채 데이터가 정상 제공되면 조건검색도 정상 처리될 것으로 판단한다고 안내했습니다.'))
    if (re.search(r'모니터링.{0,60}부하\s*현상.{0,20}(?:발생하지\s*않|확인되지\s*않)', text, re.S)
            and re.search(r'유저\s*파일|User\s*파일|추가\s*정보', text, re.I)):
        findings.append(('monitor_not_reproduced',
                         '제공된 환경에서 모니터링했지만 부하를 재현하지 못해 추가 정보나 User 파일을 요청했습니다.'))
    if (re.search(r'2차\s*모니터링', text)
            and re.search(r'화면\s*수를\s*확대', text)
            and re.search(r'타\s*사이트와\s*비교.{0,30}특이사항.{0,15}확인되지\s*않', text, re.S)):
        findings.append(('second_monitor', '2차 모니터링에서는 화면 수를 늘려 타 사이트와 비교했고 특이사항을 확인하지 못했습니다.'))
    if re.search(r'추가로\s*점검이\s*필요한\s*사항이\s*없으시다면.{0,100}모니터링을\s*마무리하고자', text, re.S):
        findings.append(('monitor_close_proposed', '추가 점검사항이 없으면 모니터링을 마무리하겠다고 제안했습니다.'))
    customer_from = re.search(r'메일\s*수신\s*\(\s*From\s*\.\s*([가-힣]{2,5})\s*\)', source_text, re.I)
    if (customer_from and re.search(r'타\s*증권사와\s*비교\s*테스트.{0,40}특이사항\s*없는\s*걸로\s*알고\s*있', text, re.S)):
        sender = _label(customer_from.group(1))
        reporter = _label(row['reporter']) if row.get('reporter') else ''
        prefix = f'노트 등록자는 {reporter}이지만 ' if reporter and reporter != sender else ''
        findings.append(('customer_reply', f'{prefix}고객 메일 발신자 {sender}은 비교 결과 특이사항이 없는 것으로 이해한다고 회신했습니다.'))
    return findings
