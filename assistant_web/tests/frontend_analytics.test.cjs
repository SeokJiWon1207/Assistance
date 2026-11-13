const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
async function load() {
  const context = vm.createContext({});
  const modules = new Map();
  async function module(name) {
    name = name.replace(/\?v=.*$/, '').replace('./', '');
    if (modules.has(name)) return modules.get(name);
    const result = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules', name), 'utf8'), { context });
    modules.set(name, result);
    await result.link(module);
    return result;
  }
  const result = await module('dashboard_analytics.js'); await result.evaluate(); return result.namespace;
}
const issue = (id, project) => ({ id, project, ageDays: 34, status: 'assigned', isOpen: true });
test('고객사 선택은 지표, 추이, 목록, 비교 집계에 같은 범위를 적용한다', async () => {
  const { analyticsScope } = await load();
  const scope = {summary:{open:1,newIssues:2},intake:[{date:'2026-11-10',count:2}],comparisons:{customer:[]}};
  const data = {summary:{open:2},openIssues:[issue(1,'LS'),issue(2,'NH')],analytics:{scopes:{LS:scope}},members:[{name:'a'}]};
  const result = analyticsScope(data, 'LS');
  assert.equal(result.summary.open,1); assert.equal(result.intakeTrend[0].count,2);
  assert.deepEqual(Array.from(result.openIssues, i => i.id), [1]);
  assert.equal(result.analytics,scope); assert.equal(data.openIssues.length,2);
});
test('고객사 목록은 등록 순서를 따르고 선택하면 연결된 프로젝트를 함께 집계한다', async () => {
  const { analyticsScope, renderAnalyticsControls } = await load();
  const scope = {summary: {open: 2}};
  const empty = {summary: {open: 0}};
  const issues = [issue(1, '147MiraeAsset'), issue(2, '147MiraeAsset(미래에셋증권)'), issue(3, '125Daewoo')];
  const data = {
    openIssues: issues, priorities: issues,
    analytics: {
      customerOptions: [
        {id: 'customer:삼성증권', name: '삼성증권', projects: [], scope: 'customer:'},
        {id: 'customer:미래에셋증권', name: '미래에셋증권', projects: issues.slice(0, 2).map(row => row.project), scope: 'customer:미래에셋증권'},
      ],
      scopes: {'customer:': empty, 'customer:미래에셋증권': scope, '125Daewoo': {summary: {open: 1}}},
    },
  };
  const html = renderAnalyticsControls(data, 'customer:미래에셋증권');
  assert.deepEqual([...html.matchAll(/<option[^>]*>([^<]+)<\/option>/g)].map(match => match[1]),
    ['모든 고객사', '삼성증권', '미래에셋증권']);
  assert.match(html, /value="customer:미래에셋증권" selected/);
  const result = analyticsScope(data, 'customer:미래에셋증권');
  assert.equal(result.analytics, scope);
  assert.deepEqual(Array.from(result.openIssues, row => row.id), [1, 2]);
  assert.deepEqual(Array.from(result.priorities, row => row.id), [1, 2]);
  assert.equal(analyticsScope(data, 'customer:삼성증권').analytics, empty);
  assert.equal(analyticsScope(data, 'customer:삼성증권').openIssues.length, 0);
});
test('처리 흐름은 등록 주차별 현재 상태를 표시하고 일부 조회에서 비율을 숨긴다', async () => {
  const { renderFlow } = await load();
  const html = renderFlow({meta:{search_complete:false}, analytics:{cohorts:[{weekStart:'2026-11-02',from:'2026-11-02',to:'2026-11-08',total:2,states:{review:1,done:1}}]}, openIssues:[]});
  assert.match(html,/등록 주차별 현재 상태/); assert.match(html,/현재 상태/);
  assert.doesNotMatch(html,/>50%|전환율/); assert.match(html,/2\+/);
});
test('차트 실패 시에도 수치를 읽을 수 있으며 고객사 이름은 HTML로 실행되지 않는다', async () => {
  const { renderOverview } = await load();
  const html = renderOverview({meta:{search_complete:true},summary:{open:1},openIssues:[issue(1,'<img src=x>')],analytics:{intake:[{date:'2026-11-10',count:2}],delivery:{points:[{date:'2026-11-10',count:1}],count:1},effort:{total:0},comparisons:{customer:[]}}});
  assert.match(html,/data-bklit-kind="activity"/); assert.match(html,/수치 보기/);
  assert.doesNotMatch(html,/<img src=x>/); assert.match(html,/고객사 전달/);
});

test('한눈에 보기는 확정 수치로만 문장을 만들고 일부 조회에서는 만들지 않는다', async () => {
  const { overviewSummary, renderOverview } = await load();
  const input = {summary:{open:10,newIssues:12,previousNewIssues:10,overdue:2,aging:3},delivery:{count:15},ranked:[{name:'LS',count:4}]};
  const lines = Array.from(overviewSummary({...input,complete:true}));
  assert.equal(lines.length,4);
  assert.match(lines[0],/신규 등록 12건, 고객사 전달 15건이에요\. 전달이 등록보다 3건 많았어요\./);
  assert.match(lines[1],/20% 늘었어요/);
  assert.match(lines[2],/미종료 10건 중 4건\(40%\)/);
  assert.match(lines[3],/일정 지연 2건, 30일 이상 미종료 3건/);
  assert.equal(overviewSummary({...input,complete:false}).length,0);
  const html = renderOverview({meta:{search_complete:true},summary:{open:2,newIssues:1},openIssues:[issue(1,'LS'),issue(2,'LS'),issue(3,'NH')],analytics:{delivery:{count:1},effort:{total:0}}});
  assert.match(html,/analytics-read/);
  assert.match(html,/data-layout-key="LS"[^]*aria-label="1위"/);
});

test('비교 분석은 차이가 큰 순으로 놓고 일부 조회에서는 증감을 판단하지 않는다', async () => {
  const { renderComparison, comparisonHeadline } = await load();
  const rows = [{name:'A',current:5,previous:4,delta:1,changeRate:25},{name:'B',current:1,previous:6,delta:-5,changeRate:-83.3},{name:'C',current:9,previous:2,delta:7,changeRate:350}];
  const html = renderComparison({meta:{search_complete:true},analytics:{comparisons:{screen:rows}}}, 'screen');
  assert.ok(html.indexOf('>C<') < html.indexOf('>B<') && html.indexOf('>B<') < html.indexOf('>A<'));
  assert.match(html, /가장 많이 늘어난 곳은 C\(\+7건\), 가장 많이 줄어든 곳은 B\(−5건\)이에요\./);
  assert.match(html, /class="is-up" style="width:50%"/);
  assert.match(html, /class="is-down" style="width:35\.7/);
  assert.doesNotMatch(html, /등록 추이 비교/);
  const partial = renderComparison({meta:{search_complete:false},analytics:{comparisons:{screen:rows}}}, 'screen');
  assert.doesNotMatch(partial, /analytics-headline|is-up|is-down/);
  assert.match(partial, /증감은 판단하지 않았어요/);
  assert.equal(comparisonHeadline([{name:'A',delta:0}], true), '직전 기간과 달라진 항목이 없어요.');
  assert.equal(comparisonHeadline([{name:'미상',delta:9},{name:'0000',delta:-9},{name:'1001',delta:2}], true), '가장 많이 늘어난 곳은 1001(+2건)이에요. 줄어든 곳은 없어요.');
  assert.match(html, /−83\.3%/);
});

test('처리 흐름은 단계 선택을 표의 같은 열에 표시하고 확정 데이터에서만 요약한다', async () => {
  const { renderFlow, flowHeadline } = await load();
  const data = {meta:{search_complete:true}, analytics:{cohorts:[{weekStart:'2026-11-02',from:'2026-11-02',to:'2026-11-08',total:4,states:{review:2,progress:1,done:1}}]}, openIssues:[]};
  const html = renderFlow(data, 'review');
  assert.match(html, /data-flow-stage="review" aria-pressed="true"/);
  assert.match(html, /<th class="is-selected">접수·검토<\/th>/);
  assert.equal((html.match(/<td class="is-selected">/g) || []).length, 1);
  assert.match(html, /이번 기간 신규 4건 중 25%가 해결·종료됐어요\. 가장 많이 남아 있는 단계는 접수·검토\(2건\)이에요\./);
  assert.doesNotMatch(renderFlow(data, 'nope'), /is-selected|aria-pressed="true"/);
  assert.equal(flowHeadline({review:1}, 1, false), '');
});

test('전체 현황은 순위 변화·급증 신호·상태 비율을 보여 주고 일부 조회에서는 급증을 판단하지 않는다', async () => {
  const { renderOverview } = await load();
  const data = {meta:{search_complete:true},summary:{open:3},openIssues:[issue(1,'A'),issue(2,'B'),issue(3,'B')],
    customerSpikes:[{name:'B',count:5,previousCount:1}],screenSpikes:[{name:'0350',count:3,previousCount:1}],analytics:{delivery:{count:0},effort:{total:0}}};
  const html = renderOverview(data, '', ['A','B']);
  assert.match(html, /data-layout-key="B"[^]*▲ 1[^]*data-layout-key="A"[^]*▼ 1/);
  assert.match(html, /직전 1건 → 지난주 5건[^]*\+4건/);
  assert.match(html, /data-dashboard-goto="screens">0350</);
  assert.match(html, /analytics-status-split[^]*진행[^]*3건[^]*100%/);
  assert.doesNotMatch(renderOverview(data, ''), /analytics-rank-move/);
  assert.match(renderOverview({...data, meta:{search_complete:false}}, ''), /급증 여부는 판단하지 않았어요/);
});
