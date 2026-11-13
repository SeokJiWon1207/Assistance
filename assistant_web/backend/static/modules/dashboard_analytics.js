// 분석 화면은 동일한 고객사 범위의 집계와 현재 이슈를 사용한다.
import { chartHost } from './bklit_charts.js?v=20261110-3';
import { customerName, customerLabelText, customerMark } from './ui_format.js?v=20260911-6';
const esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');
const num = value => Number(value || 0).toLocaleString('ko-KR', { maximumFractionDigits: 2 });
const stages = [['review','접수·검토'],['progress','진행'],['validation','검증 대기'],['done','해결·종료'],['unknown','상태 미상']];
const count = (n, complete) => `${num(n)}${complete ? '' : '+'}`;
const section = (title, note, body, extra = '') => `<section class="analytics-card ${extra}"><header><div><h3>${title}</h3>${note ? `<p>${note}</p>` : ''}</div></header>${body}</section>`;
const legend = (labels) => `<div class="analytics-legend">${labels.map(([label,color]) => `<span><i style="background:${color}"></i>${label}</span>`).join('')}</div>`;

export function analyticsCustomerOption(data, customer) {
  return data?.analytics?.customerOptions?.find(option => option.id === customer);
}

export function analyticsCustomerLabel(customer) {
  const prefix = 'customer:';
  return customer.startsWith(prefix) ? customer.slice(prefix.length) : customerName(customer, customer);
}

export function analyticsScope(data, customer) {
  if (!customer) return data;
  const option = analyticsCustomerOption(data, customer);
  const analytics = data.analytics?.scopes?.[option?.scope ?? customer];
  const projects = new Set(option?.projects ?? [customer]);
  const filter = rows => (rows || []).filter(row => projects.has(String(row.project || '고객사 미상')));
  const openIssues = filter(data.openIssues || data.priorities);
  // 오래된 API 응답도 선택하지 않은 고객사 값을 보여 주지 않는다.
  const summary = analytics?.summary || {open:openIssues.length, overdue:openIssues.filter(i=>i.overdue).length, aging:openIssues.filter(i=>i.ageDays>=30).length};
  return {...data, summary: {...summary, highRisk:openIssues.filter(i=>i.riskLevel==='high').length}, analytics: analytics || {}, openIssues, priorities:filter(data.priorities),
    intakeTrend:analytics?.intake || [],previousIntakeTrend:analytics?.previousIntake || [],
    members:[], customerHealth:(data.customerHealth || []).filter(row=>projects.has(String(row.project || row.customer || ""))),
    customerSpikes:[],screenSpikes:[],weeklyIssues:null};
}

export function renderAnalyticsControls(data, customer) {
  const options = (data.analytics?.customerOptions || []).map(({id, name, projects}) => {
    const selected = customer === id || (projects.length === 1 && customer === projects[0]);
    return `<option value="${esc(id)}"${selected ? ' selected' : ''}>${esc(name)}</option>`;
  }).join('');
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

// previousOrder: 직전에 그린 순위의 고객사 이름 순서. 바뀐 행에만 ▲·▼를 붙인다.
function ranking(rows, complete, label = '미종료', previousOrder = []) {
  const max = Math.max(1,...rows.map(r=>r.count));
  return rows.length ? `<table class="analytics-table analytics-ranking"><thead><tr><th>고객사</th><th>${label}</th><th>건수</th></tr></thead><tbody>${rows.map((row,index)=>`<tr data-layout-key="${esc(row.name)}"><th scope="row"><span class="analytics-rank-cell"><span class="analytics-rank" aria-label="${index+1}위">${index+1}</span>${customerMark(customerName(row.name,row.name))}<button type="button" data-dashboard-customer="${esc(row.name)}">${esc(customerLabelText(row.name))}</button>${rankMove(previousOrder.indexOf(row.name), index)}</span></th><td><span class="analytics-bar-track"><i style="width:${row.count/max*100}%"></i></span></td><td>${count(row.count,complete)}</td></tr>`).join('')}</tbody></table>` : `<p class="analytics-empty">${complete?'표시할 고객사 이슈가 없어요.':'확인한 범위에는 고객사 이슈가 없어요.'}</p>`;
}

// 전체 현황 맨 위의 한눈에 보기 문장. 화면에 이미 있는 확정 수치로만 만든다.
// 일부만 조회했을 때는 수치가 실제보다 적을 수 있어 문장을 만들지 않는다.
export function overviewSummary({ summary = {}, delivery = {}, ranked = [], complete = false } = {}) {
  if (!complete) return [];
  const lines = [], created = Number(summary.newIssues || 0), delivered = Number(delivery.count || 0), open = Number(summary.open || 0);
  const gap = delivered - created;
  lines.push(`신규 등록 ${num(created)}건, 고객사 전달 ${num(delivered)}건이에요.${gap > 0 ? ` 전달이 등록보다 ${num(gap)}건 많았어요.` : gap < 0 ? ` 등록이 전달보다 ${num(-gap)}건 많았어요.` : ''}`);
  const previous = Number(summary.previousNewIssues || 0);
  if (previous) {
    const rate = Math.round((created - previous) / previous * 1000) / 10;
    lines.push(rate ? `신규 등록은 직전 기간보다 ${Math.abs(rate)}% ${rate > 0 ? '늘었어요' : '줄었어요'}.` : '신규 등록은 직전 기간과 같아요.');
  }
  const top = ranked[0];
  if (top && open) lines.push(`미종료 ${num(open)}건 중 ${num(top.count)}건(${Math.round(top.count / open * 100)}%)이 ${customerLabelText(top.name)}에 있어요.`);
  const overdue = Number(summary.overdue || 0), aging = Number(summary.aging || 0);
  if (overdue || aging) lines.push(`일정 지연 ${num(overdue)}건, 30일 이상 미종료 ${num(aging)}건이에요.`);
  return lines;
}

function rankMove(before, now) {
  if (before < 0 || before === now) return '';
  const up = before > now, steps = Math.abs(before - now);
  return `<span class="analytics-rank-move ${up ? 'is-up' : 'is-down'}">${up ? '▲' : '▼'} ${steps}<span class="sr-only">계단 ${up ? '올라감' : '내려감'}</span></span>`;
}

const STATUS_COLORS = {'접수':'var(--muted)','인지':'var(--text-soft)','피드백':'var(--warning)','확인':'var(--blue)','진행':'var(--purple)','개발완료':'var(--success)'};

// 현재 상태 분포: 한 줄 비율 막대와 상태별 건수 목록.
function statusSplit(rows, complete) {
  const total = rows.reduce((sum,row)=>sum+row.amount,0);
  if (!total) return `<p class="analytics-empty">${complete?'미종료 이슈가 없어요.':'확인한 범위에는 미종료 이슈가 없어요.'}</p>`;
  const color = name => STATUS_COLORS[name] || 'var(--line-strong)';
  return `<div class="analytics-status-split" aria-hidden="true">${rows.map(row=>`<i style="flex:${row.amount} 1 0;background:${color(row.name)}"></i>`).join('')}</div>
    <ul class="analytics-status-list">${rows.map(row=>`<li><i style="background:${color(row.name)}" aria-hidden="true"></i><span>${esc(row.name)}</span><strong>${count(row.amount,complete)}건</strong>${complete?`<small>${Math.round(row.amount/total*100)}%</small>`:''}</li>`).join('')}</ul>`;
}

// 이번 주 눈여겨볼 변화: 지난주 신규가 평소보다 많았던 고객사·화면.
function signalList(customerSpikes, screenSpikes, complete) {
  if (!complete) return '<p class="analytics-empty">일부만 확인해 급증 여부는 판단하지 않았어요.</p>';
  const items = [
    ...customerSpikes.map(item=>({kind:'customer', item})),
    ...screenSpikes.map(item=>({kind:'screen', item})),
  ].slice(0,6);
  if (!items.length) return '<p class="analytics-empty">지난주에 평소보다 크게 늘어난 곳이 없어요.</p>';
  return `<ul class="analytics-signals">${items.map(({kind,item})=>`<li>
    ${kind==='customer' ? customerMark(customerName(item.name,item.name)) : '<span class="analytics-signal-screen" aria-hidden="true">화면</span>'}
    <span><strong>${kind==='customer' ? `<button type="button" data-dashboard-customer="${esc(item.name)}">${esc(customerLabelText(item.name))}</button>` : `<button type="button" data-dashboard-goto="screens">${esc(item.name)}</button>`}</strong><small>직전 ${num(item.previousCount)}건 → 지난주 ${num(item.count)}건</small></span>
    <em>+${num(item.count - item.previousCount)}건</em></li>`).join('')}</ul>`;
}

export function renderOverview(data, priorities = '', previousOrder = []) {
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
  const read = overviewSummary({summary:s, delivery:a.delivery, ranked, complete});
  // 요약이 있으면 상태 분포를 요약 카드 아래(지표 2×2 옆)에 두어 빈 칸을 채운다. 요약이 없으면 예전처럼 아래 줄에 둔다.
  const statusCard = section('현재 상태 분포','현재 미종료 이슈 · 개발완료는 검증 대기로 포함',statusSplit(statusRows,complete),'analytics-status-card');
  return `<div class="analytics-top${read.length ? '' : ' is-single'}">${read.length ? `<div class="analytics-top-main"><section class="analytics-card analytics-read" aria-label="한눈에 보기"><header><div><h3>한눈에 보기</h3><p>아래 수치로 만든 요약이에요.</p></div></header><ul>${read.map(line=>`<li>${esc(line)}</li>`).join('')}</ul></section>${statusCard}</div>` : ''}
    <div class="analytics-kpis">${kpis}</div></div>
    ${section('업무 흐름','신규 등록과 고객사 전달을 일별로 비교해요. 두 선은 같은 이슈 집합을 뜻하지 않아요.',
      `<div class="analytics-chart-heading"><strong>${count(s.newIssues,complete)}<small>신규 등록</small></strong><strong>${count(a.delivery?.count,complete)}<small>고객사 전달</small></strong>${legend([['신규 등록','var(--chart-1)'],['고객사 전달','var(--analytics-secondary)']])}</div>`+
      chartHost({kind:'activity',title:'일별 신규 등록·고객사 전달',complete,height:320,points:a.intake || data.intakeTrend || [],secondaryPoints:a.delivery?.points || [],showValues:true},dailyTable(a,complete)),'analytics-hero')}
    <div class="analytics-two">${section('고객사별 미종료','현재 업무가 집중된 고객사 · 상위 8곳 · ▲▼는 직전에 본 순위와 비교',ranking(ranked,complete,'미종료',previousOrder))}
    ${section('이번 주 눈여겨볼 변화','지난주 신규가 앞선 주 평균보다 크게 늘어난 고객사·화면',signalList(data.customerSpikes || [], data.screenSpikes || [], complete))}</div>
    ${read.length ? '' : statusCard}
    ${priorities}`;
}

// 비교 분석 위 한 줄 요약. 일부 조회에서는 증감을 판단하지 않는다.
export function comparisonHeadline(rows, complete, label = name => name) {
  if (!complete || !rows.length) return '';
  // 미상·미배정·0000처럼 값이 기록되지 않은 묶음은 요약 대상에서 뺀다. 표에는 그대로 둔다.
  rows = rows.filter(row => !/미상|미배정|^0+$/.test(String(row.name || '').trim()) && String(row.name || '').trim());
  const up = rows.filter(row => row.delta > 0).sort((a,b) => b.delta-a.delta)[0];
  const down = rows.filter(row => row.delta < 0).sort((a,b) => a.delta-b.delta)[0];
  if (!up && !down) return '직전 기간과 달라진 항목이 없어요.';
  const part = (row, sign) => `${label(row.name)}(${sign}${num(Math.abs(row.delta))}건)`;
  if (up && down) return `가장 많이 늘어난 곳은 ${part(up,'+')}, 가장 많이 줄어든 곳은 ${part(down,'−')}이에요.`;
  return up ? `가장 많이 늘어난 곳은 ${part(up,'+')}이에요. 줄어든 곳은 없어요.` : `가장 많이 줄어든 곳은 ${part(down,'−')}이에요. 늘어난 곳은 없어요.`;
}

// 비교 분석: 기준(고객사·화면번호·담당자)별로 이번·직전 기간 신규를 비교한다.
// 확정 데이터는 차이가 큰 순으로 놓고, 가운데 기준선에서 오른쪽(늘어남)·왼쪽(줄어듦)으로 막대를 그린다.
export function renderComparison(data, dimension = 'customer') {
  const complete = data.meta?.search_complete === true, a = data.analytics || {};
  const label = name => dimension==='customer' ? customerLabelText(name) : name;
  const rows = [...(a.comparisons?.[dimension] || [])];
  if (complete) rows.sort((x,y) => Math.abs(y.delta||0)-Math.abs(x.delta||0) || (y.current||0)-(x.current||0));
  const maxDelta = Math.max(1, ...rows.map(row => Math.abs(row.delta || 0)));
  const name = row => dimension==='customer'
    ? `<span class="analytics-rank-cell">${customerMark(customerName(row.name,row.name))}<button type="button" data-dashboard-customer="${esc(row.name)}">${esc(label(row.name))}</button></span>`
    : esc(label(row.name));
  const bar = row => {
    if (!complete || !row.delta) return '<span class="analytics-diff-track"></span>';
    const width = Math.abs(row.delta)/maxDelta*50;
    return `<span class="analytics-diff-track"><i class="${row.delta>0?'is-up':'is-down'}" style="width:${width}%"></i></span>`;
  };
  const diff = row => complete ? `<strong class="analytics-diff ${row.delta>0?'is-up':row.delta<0?'is-down':''}">${row.delta>0?'+':row.delta<0?'−':''}${num(Math.abs(row.delta||0))}건</strong>${row.changeRate==null?'':`<small>${row.changeRate>0?'+':row.changeRate<0?'−':''}${num(Math.abs(row.changeRate))}%</small>`}` : '—';
  const header = {customer:'고객사',screen:'화면번호',member:'담당자'}[dimension];
  const table = rows.length
    ? `<div class="analytics-table-scroll"><table class="analytics-table analytics-diff-table"><thead><tr><th>${header}</th><th>이번 기간</th><th>직전 기간</th><th><span class="sr-only">차이 막대</span></th><th>증감</th></tr></thead><tbody>${rows.map(row=>`<tr><th scope="row">${name(row)}</th><td>${count(row.current,complete)}</td><td>${count(row.previous,complete)}</td><td aria-hidden="true">${bar(row)}</td><td>${diff(row)}</td></tr>`).join('')}</tbody></table></div>`
    : `<p class="analytics-empty">${complete?'비교할 항목이 없어요.':'확인한 범위에는 비교할 항목이 없어요.'}</p>`;
  const buttons = `<div class="analytics-segments" role="group" aria-label="비교 기준">${[['customer','고객사'],['screen','화면번호'],['member','담당자']].map(([key,text])=>`<button type="button" data-analytics-dimension="${key}" aria-pressed="${key===dimension}">${text}</button>`).join('')}</div>`;
  const headline = comparisonHeadline(rows, complete, label);
  const note = complete ? '신규 등록 기준 · 이번 기간과 같은 길이의 직전 기간 비교 · 차이가 큰 순서' : '일부만 확인해 증감은 판단하지 않았어요. 건수 뒤 +는 최소값이에요.';
  return section('어디에서 달라졌나요?', note,
    buttons + `<div class="analytics-related"><button type="button" data-dashboard-goto="effort">공수·처리시간 보기 ↗</button><button type="button" data-dashboard-goto="screens">화면번호 상세 보기 ↗</button></div>` +
    (headline ? `<p class="analytics-headline">${esc(headline)}</p>` : '') + table);
}

// 처리 흐름 위 한 줄 요약. 일부 조회에서는 비율이 틀릴 수 있어 만들지 않는다.
export function flowHeadline(counts, total, complete) {
  if (!complete || !total) return '';
  const done = counts.done || 0;
  const waiting = stages.filter(([key]) => key !== 'done' && key !== 'unknown' && counts[key]).sort((a,b) => counts[b[0]]-counts[a[0]])[0];
  const head = `이번 기간 신규 ${num(total)}건 중 ${Math.round(done/total*100)}%가 해결·종료됐어요.`;
  return waiting ? `${head} 가장 많이 남아 있는 단계는 ${waiting[1]}(${num(counts[waiting[0]])}건)이에요.` : `${head} 남아 있는 단계는 없어요.`;
}

// 단계를 누르면 아래 등록 주차별 표에서 그 단계 열이 강조된다.
export function renderFlow(data, selectedStage = '') {
  const complete = data.meta?.search_complete === true, a = data.analytics || {};
  const cohorts = a.cohorts || [];
  const counts = Object.fromEntries(stages.map(([key])=>[key,cohorts.reduce((sum,row)=>sum+(row.states?.[key]||0),0)]));
  const total = cohorts.reduce((sum,row)=>sum+row.total,0);
  const shown = stages.filter(([key])=>key!=='unknown'||counts.unknown);
  const stage = shown.some(([key]) => key === selectedStage) ? selectedStage : '';
  const split = total ? `<div class="analytics-stage-split" aria-hidden="true">${shown.filter(([key])=>counts[key]).map(([key])=>`<i class="stage-${key}${stage && stage!==key?' is-dim':''}" style="flex:${counts[key]} 1 0"></i>`).join('')}</div>` : '';
  const phase = `${split}<div class="analytics-stages" role="group" aria-label="처리 단계 · 누르면 아래 표에서 강조">${shown.map(([key,label])=>`<button type="button" class="analytics-stage" data-flow-stage="${key}" aria-pressed="${stage===key}"><span><i class="analytics-stage-dot stage-${key}"></i>${label}</span><strong>${count(counts[key],complete)}<small>건</small></strong><span class="analytics-bar-track"><i class="stage-${key}" style="width:${total?counts[key]/total*100:0}%"></i></span><small>${complete&&total?`${Math.round(counts[key]/total*100)}% · 기간 내 신규 중`:'확인한 건수 기준'}</small></button>`).join('')}</div>`;
  const headline = flowHeadline(counts, total, complete);
  const heatmap = `<div class="analytics-table-scroll"><table class="analytics-table analytics-heatmap"><thead><tr><th>등록 주차</th><th>신규</th>${shown.map(([key,label])=>`<th${stage===key?' class="is-selected"':''}>${label}</th>`).join('')}</tr></thead><tbody>${cohorts.map(row=>`<tr><th scope="row">${esc(row.from?.slice(5)||row.weekStart.slice(5))} – ${esc(row.to?.slice(5)||'')}</th><td>${count(row.total,complete)}</td>${shown.map(([key,label])=>{ const value=row.states?.[key]||0; return `<td${stage===key?' class="is-selected"':''}><span class="analytics-heat-cell" data-intensity="${value?Math.min(4,Math.ceil(value/Math.max(1,row.total)*4)):0}" title="${esc(label)} ${count(value,complete)}건">${count(value,complete)}</span></td>`; }).join('')}</tr>`).join('')}</tbody></table></div>`;
  const buckets = [{name:'7일 이하',amount:0},{name:'8–29일',amount:0},{name:'30–59일',amount:0},{name:'60일 이상',amount:0},{name:'등록일 미상',amount:0}];
  for(const row of data.openIssues || []) { const d=row.ageDays; buckets[d==null?4:d<=7?0:d<30?1:d<60?2:3].amount++; }
  return `${headline ? `<p class="analytics-headline analytics-pane-headline">${esc(headline)}</p>` : ''}
    ${section('처리 단계 현황','선택 기간에 등록된 이슈의 현재 상태예요. 단계를 누르면 아래 표에서 그 단계가 강조돼요.',phase)}
    ${section('등록 주차별 현재 상태',`신규 ${count(total,complete)}건 · 같은 주에 등록된 이슈가 지금 어느 상태인지 확인해요.`,heatmap)}
    ${section('남아 있는 업무의 경과일','등록일부터 현재까지 · 선택 기간 이전의 미종료도 포함',chartHost({kind:'bar',title:'미종료 경과일 분포',complete,rows:buckets,series:[{key:'amount',label:'미종료',color:'var(--chart-1)'}]},`<ul>${buckets.map(r=>`<li>${r.name} ${count(r.amount,complete)}건</li>`).join('')}</ul>`))}
    <p class="analytics-footnote">상태 변경 이력이 없어 과거 시점의 잔존율은 표시하지 않아요.${a.missingCreated?` 등록일 미상 ${count(a.missingCreated,complete)}건은 주차별 집계에서 제외했어요.`:''}</p>`;
}
