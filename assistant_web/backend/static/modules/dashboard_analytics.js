// 분석 화면은 동일한 고객사 범위의 집계와 현재 이슈를 사용한다.
import { chartHost } from './bklit_charts.js?v=20261110-3';
import { customerLabelText } from './ui_format.js?v=20260911-6';
const esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const num = value => Number(value || 0).toLocaleString('ko-KR', { maximumFractionDigits: 2 });
const stages = [['review','접수·검토'],['progress','진행'],['validation','검증 대기'],['done','해결·종료'],['unknown','상태 미상']];
const count = (n, complete) => `${num(n)}${complete ? '' : '+'}`;
const section = (title, note, body, extra = '') => `<section class="analytics-card ${extra}"><header><div><h3>${title}</h3>${note ? `<p>${note}</p>` : ''}</div></header>${body}</section>`;
const legend = (labels) => `<div class="analytics-legend">${labels.map(([label,color]) => `<span><i style="background:${color}"></i>${label}</span>`).join('')}</div>`;
const series = [{key:'current',label:'이번 기간',color:'var(--chart-1)'},{key:'previous',label:'직전 기간',color:'var(--analytics-secondary)'}];

export function analyticsScope(data, customer) {
  if (!customer) return data;
  const analytics = data.analytics?.scopes?.[customer];
  const filter = rows => (rows || []).filter(row => String(row.project || '고객사 미상') === customer);
  const openIssues = filter(data.openIssues || data.priorities);
  // 오래된 API 응답도 선택하지 않은 고객사 값을 보여 주지 않는다.
  const summary = analytics?.summary || {open:openIssues.length, overdue:openIssues.filter(i=>i.overdue).length, aging:openIssues.filter(i=>i.ageDays>=30).length};
  return {...data, summary: {...summary, highRisk:openIssues.filter(i=>i.riskLevel==='high').length}, analytics: analytics || {}, openIssues, priorities:filter(data.priorities),
    intakeTrend:analytics?.intake || [],previousIntakeTrend:analytics?.previousIntake || [],
    members:[], customerHealth:(data.customerHealth || []).filter(row=>String(row.project || row.customer || "")===customer),
    customerSpikes:[],screenSpikes:[],weeklyIssues:null};
}

export function renderAnalyticsControls(data, customer) {
  const options = Object.keys(data.analytics?.scopes || {}).map(name => `<option value="${esc(name)}"${customer===name?' selected':''}>${esc(customerLabelText(name))}</option>`).join('');
  return `<div class="analytics-controls"><label>고객사 <select data-analytics-customer aria-label="분석 고객사"><option value="">모든 고객사</option>${options}</select></label><span>기간 내 신규·전달 이력 · 미종료는 현재 기준</span></div>`;
}

function delta(current, previous, complete) {
  if (!complete) return '일부 조회 · 비교 미확정';
  if (previous == null) return '이전 기간 자료 없음';
  if (!previous) return current ? '직전 기간 0건' : '직전 기간과 동일';
  const rate = Math.round((current-previous)/previous*1000)/10;
  return `${rate>0?'↑':rate<0?'↓':'–'} ${Math.abs(rate)}% · 직전 기간 대비`;
}

function metric(title, value, unit, note, points, complete, extra = '') {
  return `<article class="analytics-kpi"><span>${title}</span><div class="analytics-kpi-value">${count(value,complete)}<small>${unit}</small></div><p>${esc(note)}</p>${points?.length ? chartHost({kind:'sparkline',title:`${title} 일별 추이`,compact:true,complete,points,unit,decimals:unit==='M/D'?2:0,periodLabel:'선택 기간',dateBasis:title==='신규 등록'?'등록일':'고객사전달일'}) : extra}</article>`;
}

function dailyTable(a, complete) {
  const delivered = new Map((a.delivery?.points || []).map(p=>[p.date,p.count]));
  return `<div class="analytics-table-scroll"><table class="analytics-table"><thead><tr><th>일자</th><th>신규 등록</th><th>고객사 전달</th></tr></thead><tbody>${(a.intake || []).map(p=>`<tr><th scope="row">${esc(p.date)}</th><td>${count(p.count,complete)}</td><td>${count(delivered.get(p.date),complete)}</td></tr>`).join('')}</tbody></table></div>`;
}

function ranking(rows, complete, label = '미종료') {
  const max = Math.max(1,...rows.map(r=>r.count));
  return rows.length ? `<table class="analytics-table analytics-ranking"><thead><tr><th>고객사</th><th>${label}</th><th>건수</th></tr></thead><tbody>${rows.map(row=>`<tr><th scope="row"><button type="button" data-dashboard-customer="${esc(row.name)}">${esc(customerLabelText(row.name))}</button></th><td><span class="analytics-bar-track"><i style="width:${row.count/max*100}%"></i></span></td><td>${count(row.count,complete)}</td></tr>`).join('')}</tbody></table>` : `<p class="analytics-empty">${complete?'표시할 고객사 이슈가 없어요.':'확인한 범위에는 고객사 이슈가 없어요.'}</p>`;
}

export function renderOverview(data, priorities = '') {
  const complete = data.meta?.search_complete === true;
  const a = data.analytics || {intake:data.intakeTrend,previousIntake:data.previousIntakeTrend}, s = a.summary || data.summary || {}, issues = data.openIssues || data.priorities || [];
  const grouped = new Map(), status = new Map();
  for (const issue of issues) {
    const name = issue.project || '고객사 미상'; grouped.set(name,(grouped.get(name)||0)+1);
    const label = {'new':'접수','feedback':'피드백','acknowledged':'인지','confirmed':'확인','assigned':'진행','developed':'개발완료'}[issue.status] || issue.status || '상태 미상';
    status.set(label,(status.get(label)||0)+1);
  }
  const ranked = [...grouped].map(([name,count])=>({name,count})).sort((a,b)=>b.count-a.count).slice(0,8);
  const statusRows = [...status].map(([name,amount])=>({name,amount}));
  const kpis = metric('현재 미종료',s.open,'건',`일정 지연 ${count(s.overdue,complete)} · 30일 이상 ${count(s.aging,complete)}`,null,complete,`<div class="analytics-kpi-foot"><button type="button" data-dashboard-metric="open">미종료 이슈 보기 ↗</button></div>`)
    + metric('신규 등록',s.newIssues,'건',delta(s.newIssues,s.previousNewIssues,complete),a.intake,complete)
    + metric('고객사 전달',a.delivery?.count,'건',delta(a.delivery?.count,a.delivery?.previousCount,complete),a.delivery?.points,complete)
    + metric('전달 건 공수',a.effort?.total,'M/D','고객사전달일 기준 · 입력된 공수 합계',a.effort?.points,complete);
  return `<div class="analytics-kpis">${kpis}</div>
    ${section('업무 흐름','신규 등록과 고객사 전달을 일별로 비교해요. 두 선은 같은 이슈 집합을 뜻하지 않아요.',
      `<div class="analytics-chart-heading"><strong>${count(s.newIssues,complete)}<small>신규 등록</small></strong><strong>${count(a.delivery?.count,complete)}<small>고객사 전달</small></strong>${legend([['신규 등록','var(--chart-1)'],['고객사 전달','var(--analytics-secondary)']])}</div>`+
      chartHost({kind:'activity',title:'일별 신규 등록·고객사 전달',complete,height:320,points:a.intake || data.intakeTrend || [],secondaryPoints:a.delivery?.points || [],showValues:true},dailyTable(a,complete)),'analytics-hero')}
    <div class="analytics-two">${section('고객사별 미종료','현재 업무가 집중된 고객사 · 상위 8곳',ranking(ranked,complete))}
    ${section('현재 상태 분포','현재 미종료 이슈 · 개발완료는 검증 대기로 포함',chartHost({kind:'bar',title:'미종료 상태 분포',complete,rows:statusRows,series:[{key:'amount',label:'이슈',color:'var(--chart-1)'}]},`<ul>${statusRows.map(r=>`<li>${esc(r.name)} ${count(r.amount,complete)}건</li>`).join('')}</ul>`))}</div>
    ${priorities}`;
}

export function renderComparison(data, dimension = 'customer') {
  const complete = data.meta?.search_complete === true, a = data.analytics || {intake:data.intakeTrend,previousIntake:data.previousIntakeTrend}, s = a.summary || data.summary || {};
  const rows = a.comparisons?.[dimension] || [];
  const label = name => dimension==='customer' ? customerLabelText(name) : name;
  const table = `<div class="analytics-table-scroll"><table class="analytics-table"><thead><tr><th>${{customer:'고객사',screen:'화면번호',member:'담당자'}[dimension]}</th><th>이번 기간</th><th>직전 기간</th><th>증감</th><th>증감률</th></tr></thead><tbody>${rows.map(row=>`<tr><th scope="row">${dimension==='customer'?`<button type="button" data-dashboard-customer="${esc(row.name)}">${esc(label(row.name))}</button>`:esc(label(row.name))}</th><td>${count(row.current,complete)}</td><td>${count(row.previous,complete)}</td><td>${complete?`${row.delta>0?'+':''}${num(row.delta)}`:'—'}</td><td>${row.changeRate==null?'—':`${row.changeRate>0?'+':''}${num(row.changeRate)}%`}</td></tr>`).join('')}</tbody></table></div>`;
  const buttons = `<div class="analytics-segments" role="group" aria-label="비교 기준">${[['customer','고객사'],['screen','화면번호'],['member','담당자']].map(([key,label])=>`<button type="button" data-analytics-dimension="${key}" aria-pressed="${key===dimension}">${label}</button>`).join('')}</div>`;
  return `${section('등록 추이 비교','동일한 길이의 이번 기간·직전 기간을 시작일부터 누적으로 비교해요.',
    `<div class="analytics-chart-heading"><strong>${count(s.newIssues,complete)}<small>이번 기간 신규</small></strong><strong>${count(s.previousNewIssues,complete)}<small>직전 기간 신규</small></strong>${legend([['이번 기간','var(--chart-1)'],['직전 기간','var(--muted)']])}</div>` +
    chartHost({kind:'compare-area',title:'기간별 누적 신규 등록',complete,height:320,lineStyle:'snow',points:a.intake || [],previousPoints:a.previousIntake || [],showValues:true},`<p>이번 ${count(s.newIssues,complete)}건 · 직전 ${count(s.previousNewIssues,complete)}건</p>`),'analytics-hero')}
    ${section('어디에서 달라졌나요?','신규 등록 기준 · 막대는 상위 8개, 표는 전체 항목',buttons + `<div class="analytics-related"><button type="button" data-dashboard-goto="effort">공수·처리시간 보기 ↗</button><button type="button" data-dashboard-goto="screens">화면번호 상세 보기 ↗</button></div>` +
      chartHost({kind:'bar',title:'항목별 이번·직전 기간 신규 비교',complete,rows:rows.slice(0,8).map(row=>({...row,name:label(row.name)})),series,grouped:true},table))}`;
}

export function renderFlow(data) {
  const complete = data.meta?.search_complete === true, a = data.analytics || {};
  const cohorts = a.cohorts || [];
  const counts = Object.fromEntries(stages.map(([key])=>[key,cohorts.reduce((sum,row)=>sum+(row.states?.[key]||0),0)]));
  const total = cohorts.reduce((sum,row)=>sum+row.total,0);
  const shown = stages.filter(([key])=>key!=='unknown'||counts.unknown);
  const phase = `<div class="analytics-stages">${shown.map(([key,label],i)=>`<article><span><i class="analytics-stage-dot stage-${key}"></i>${label}</span><strong>${count(counts[key],complete)}<small>건</small></strong><div class="analytics-bar-track"><i class="stage-${key}" style="width:${total?counts[key]/total*100:0}%"></i></div><small>${complete&&total?`${Math.round(counts[key]/total*100)}% · 기간 내 신규 중`:'확인한 건수 기준'}</small></article>`).join('')}</div>`;
  const heatmap = `<div class="analytics-table-scroll"><table class="analytics-table analytics-heatmap"><thead><tr><th>등록 주차</th><th>신규</th>${shown.map(([,label])=>`<th>${label}</th>`).join('')}</tr></thead><tbody>${cohorts.map(row=>`<tr><th scope="row">${esc(row.from?.slice(5)||row.weekStart.slice(5))} – ${esc(row.to?.slice(5)||'')}</th><td>${count(row.total,complete)}</td>${shown.map(([key,label])=>{ const value=row.states?.[key]||0; return `<td><span class="analytics-heat-cell" data-intensity="${value?Math.min(4,Math.ceil(value/Math.max(1,row.total)*4)):0}" title="${esc(label)} ${count(value,complete)}건">${count(value,complete)}</span></td>`; }).join('')}</tr>`).join('')}</tbody></table></div>`;
  const buckets = [{name:'7일 이하',amount:0},{name:'8–29일',amount:0},{name:'30–59일',amount:0},{name:'60일 이상',amount:0},{name:'등록일 미상',amount:0}];
  for(const row of data.openIssues || []) { const d=row.ageDays; buckets[d==null?4:d<=7?0:d<30?1:d<60?2:3].amount++; }
  return `${section('처리 단계 현황','선택 기간에 등록된 이슈의 현재 상태예요. 단계를 이동한 이력은 포함하지 않아요.',phase)}
    ${section('등록 주차별 현재 상태',`신규 ${count(total,complete)}건 · 같은 주에 등록된 이슈가 지금 어느 상태인지 확인해요.`,heatmap)}
    ${section('남아 있는 업무의 경과일','등록일부터 현재까지 · 선택 기간 이전의 미종료도 포함',chartHost({kind:'bar',title:'미종료 경과일 분포',complete,rows:buckets,series:[{key:'amount',label:'미종료',color:'var(--chart-1)'}]},`<ul>${buckets.map(r=>`<li>${r.name} ${count(r.amount,complete)}건</li>`).join('')}</ul>`))}
    <p class="analytics-footnote">상태 변경 이력이 없어 과거 시점의 잔존율은 표시하지 않아요.${a.missingCreated?` 등록일 미상 ${count(a.missingCreated,complete)}건은 주차별 집계에서 제외했어요.`:''}</p>`;
}
