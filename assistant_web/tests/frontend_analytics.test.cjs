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
