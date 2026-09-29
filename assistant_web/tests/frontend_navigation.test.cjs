const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness(role, stored, fetcher) {
  const calls = [];
  const storage = new Map(stored ? [[`team-ai-assistant-main-view-v1:user-${role}`, stored]] : []);
  let focused;
  const element = (name) => ({
    hidden: true, disabled: false, tabIndex: -1, innerHTML: '', textContent: '', attributes: {}, handlers: {},
    classList: { toggle() {}, remove() {} },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return { textContent: '', addEventListener() {}, focus() {}, scrollIntoView() {} }; },
    focus() { focused = name; },
  });
  const els = Object.fromEntries([
    'viewChatBtn', 'viewPersonalBtn', 'viewDashboardBtn', 'chatPanel', 'personalPanel',
    'dashboardPanel', 'dashboardRefreshBtn', 'dashboardPeriod', 'dashboardFilterbar', 'dashboardBody',
    'dashboardDetail', 'dashboardDetailClose', 'workspaceContent', 'dashboardSource', 'dashboardGeneratedAt', 'dashboardSubtitle',
  ].map((name) => [name, element(name)]));
  const htmlWrites = [];
  Object.defineProperty(els.dashboardBody, 'innerHTML', {
    get: () => htmlWrites.at(-1) || '',
    set: (html) => htmlWrites.push(html),
  });
  els.viewChatBtn.hidden = els.viewPersonalBtn.hidden = false;
  const state = { currentUser: { uid: `user-${role}`, role }, workspaceTab: 'overview', myWorkspace: { summary: {} } };
  const context = vm.createContext({
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    document: { body: { classList: { remove() {} } }, addEventListener() {} },
    CSS: { escape: (value) => String(value) },
  });
  const dependencies = {
    './runtime.js': { state, els, WORKSPACE_ENDPOINTS: { dashboard: '/dashboard' },
      apiFetch: async (url) => { calls.push(url); return fetcher ? fetcher(url) : { ok: false }; } },
    './api.js': { responseJson: async (response) => response.payload || ({ detail: '연결 오류' }) },
    './workspace.js': {
      ensureMyWorkspaceFresh: async () => { calls.push('personal'); }, renderWorkspace() {},
      loadTeamWeeklyBrief: async () => {}, renderTeamWeeklyBrief: () => '', markTeamWeeklyBriefRead: async () => {},
    },
  };
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard.js'), 'utf8'), { context });
  await module.link((specifier) => {
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initDashboard();
  return { api: module.namespace, els, state, storage, calls, htmlWrites, focus: () => focused };
}

const dashboard = (source, open) => ({
  meta: { source, fetchedAt: new Date().toISOString(), search_complete: true, scope: 'team', periodFrom: '2026-08-13', periodTo: '2026-09-11', periodDays: 30, generatedAt: '2026-09-11T12:00:00' },
  user: { role: 'member' },
  summary: { open, carriedOpen: 0, overdue: 0, aging: 0, highRisk: 0, highPriority: 0, changeRate: 0 },
  decisionSummary: { riskLevel: 'watch', headline: `${source} 업무 요약`, signals: [], recommendedActions: [] },
  todos: [], customerHealth: [], customerHealthSummary: {}, customerSpikes: [], screenSpikes: [], members: [], priorities: [],
});

const filterableDashboard = () => ({
  ...dashboard('live', 3),
  priorities: [
    { id: 1, title: '지연 이슈', project: '고객사 A', overdue: true, ageDays: 40, riskLevel: 'high' },
    { id: 2, title: '장기 이슈', project: '고객사 B', overdue: false, ageDays: 45, riskLevel: 'attention' },
    { id: 3, title: '최근 이슈', project: '고객사 A', overdue: false, ageDays: 3, riskLevel: 'watch' },
  ],
});
const clickTarget = (selector, dataset) => ({ closest: (query) => (query === selector ? { dataset } : null) });

test('manager can switch among three panels without losing personal tab state', async () => {
  const h = await harness('manager');
  assert.equal(h.els.chatPanel.hidden, false);
  assert.equal(h.els.viewDashboardBtn.hidden, false);
  h.state.workspaceTab = 'notifications';
  h.api.showPersonalView();
  assert.equal(h.els.personalPanel.hidden, false);
  assert.equal(h.els.chatPanel.hidden, true);
  h.api.showDashboardView({ load: false });
  assert.equal(h.els.dashboardPanel.hidden, false);
  assert.equal(h.els.personalPanel.hidden, true);
  h.api.showPersonalView();
  assert.equal(h.state.workspaceTab, 'notifications');
  assert.equal(h.els.viewPersonalBtn.attributes['aria-selected'], 'true');
  assert.equal(h.els.viewDashboardBtn.tabIndex, -1);
});

test('member can open and load the team dashboard', async () => {
  const h = await harness('member', 'dashboard');
  await h.api.loadDashboard();
  assert.equal(h.els.viewDashboardBtn.hidden, false);
  assert.equal(h.els.dashboardPanel.hidden, false);
  assert.equal(h.els.personalPanel.hidden, true);
  assert.deepEqual(h.calls, ['/dashboard?days=30&live=true']);
});

test('manual dashboard refresh bypasses the server fast path', async () => {
  const h = await harness('member');
  await h.api.loadDashboard(true);
  assert.deepEqual(h.calls, ['/dashboard?days=30&refresh=true']);
});

test('dashboard shows refresh progress while keeping its controls in sync', async () => {
  let finish;
  const h = await harness('member', undefined, async () => new Promise((resolve) => { finish = resolve; }));
  const loading = h.api.loadDashboard(true);
  await new Promise(setImmediate);
  assert.equal(h.els.dashboardRefreshBtn.disabled, true);
  assert.equal(h.els.dashboardRefreshBtn.textContent, '불러오는 중…');
  assert.equal(h.els.dashboardRefreshBtn.attributes['aria-busy'], 'true');
  finish({ ok: false });
  await loading;
  assert.equal(h.els.dashboardRefreshBtn.disabled, false);
  assert.equal(h.els.dashboardRefreshBtn.textContent, '새로고침');
  assert.equal(h.els.dashboardRefreshBtn.attributes['aria-busy'], 'false');
});

test('dashboard joins one live lookup and hides lists until completion', async () => {
  const payloads = [dashboard('live', 2)];
  let releaseLive;
  const h = await harness('member', undefined, async (url) => {
    await new Promise((resolve) => { releaseLive = resolve; });
    return { ok: true, payload: payloads.shift() };
  });
  const prefetch = h.api.loadDashboard();
  const loading = h.api.ensureDashboardFresh();
  const joined = h.api.ensureDashboardFresh();
  await new Promise(setImmediate);
  assert.equal(h.els.dashboardRefreshBtn.disabled, true);
  assert.equal(h.els.dashboardPeriod.disabled, true);
  assert.equal(h.els.dashboardBody.attributes['aria-busy'], 'true');
  assert.match(h.els.dashboardBody.innerHTML, /dashboard-spinner/);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /dashboard-grid|dashboard-metrics/);
  assert.ok(h.htmlWrites.every((html) => !html.includes('snapshot 업무 요약')));
  releaseLive();
  await Promise.all([prefetch, loading, joined]);
  assert.match(h.els.dashboardBody.innerHTML, /live 업무 요약/);
  assert.ok(h.htmlWrites.every((html) => !html.includes('snapshot 업무 요약')));
  assert.equal(h.els.dashboardPeriod.disabled, false);
  assert.equal(h.els.dashboardBody.attributes['aria-busy'], 'false');
  await h.api.ensureDashboardFresh();
  assert.deepEqual(h.calls, ['/dashboard?days=30&live=true']);
});

test('dashboard manual refresh hides old sections until completion', async () => {
  let finish;
  const h = await harness('member', undefined, async (url) => url.includes('refresh=true')
    ? new Promise((resolve) => { finish = resolve; })
    : { ok: true, payload: dashboard('snapshot', 5) });
  await h.api.loadDashboard();
  assert.match(h.els.dashboardBody.innerHTML, /snapshot 업무 요약/);
  const loading = h.api.loadDashboard(true);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /snapshot 업무 요약|dashboard-grid|dashboard-metrics/);
  finish({ ok: true, payload: dashboard('live', 2) });
  await loading;
  assert.match(h.els.dashboardBody.innerHTML, /live 업무 요약/);
  assert.equal(h.els.dashboardRefreshBtn.disabled, false);
});

test('dashboard reveals server fallback only after the single lookup completes', async () => {
  let finish;
  const h = await harness('member', undefined, async () => new Promise((resolve) => { finish = resolve; }));
  const loading = h.api.ensureDashboardFresh();
  await new Promise(setImmediate);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /snapshot 업무 요약/);
  finish({ ok: true, payload: dashboard('snapshot', 5) });
  await loading;
  assert.match(h.els.dashboardBody.innerHTML, /snapshot 업무 요약/);
  assert.equal(h.els.dashboardBody.attributes['aria-busy'], 'false');
  assert.equal(h.els.dashboardRefreshBtn.disabled, false);
  assert.deepEqual(h.calls, ['/dashboard?days=30&live=true']);
});

test('dashboard joins failed prefetch without retrying or leaving a spinner', async () => {
  let finish;
  const h = await harness('member', undefined, async () => new Promise((resolve) => { finish = resolve; }));
  const prefetch = h.api.loadDashboard();
  const loading = h.api.ensureDashboardFresh();
  await new Promise(setImmediate);
  finish({ ok: false, payload: { detail: '조회 실패' } });
  await Promise.all([prefetch, loading]);
  assert.deepEqual(h.calls, ['/dashboard?days=30&live=true']);
  assert.match(h.els.dashboardBody.innerHTML, /조회 실패/);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /dashboard-spinner|dashboard-grid/);
  assert.equal(h.els.dashboardRefreshBtn.disabled, false);
  assert.equal(h.els.dashboardBody.attributes['aria-busy'], 'false');
});

test('keyboard navigation follows visible tab order and moves focus', async () => {
  for (const role of ['admin', 'member']) {
    const h = await harness(role);
    const key = (button, value) => h.els[button].handlers.keydown({ key: value, preventDefault() {} });
    key('viewChatBtn', 'ArrowRight');
    assert.equal(h.focus(), 'viewPersonalBtn');
    key('viewPersonalBtn', 'End');
    assert.equal(h.focus(), 'viewDashboardBtn');
    key(h.focus(), 'Home');
    assert.equal(h.focus(), 'viewChatBtn');
  }
});

test('personal view restores from account-scoped storage and requests missing data', async () => {
  const h = await harness('admin', 'personal');
  assert.equal(h.els.personalPanel.hidden, false);
  assert.deepEqual(h.calls, ['personal']);
  h.state.myWorkspace = null;
  h.api.showPersonalView();
  assert.deepEqual(h.calls, ['personal', 'personal']);
  assert.equal(h.storage.get('team-ai-assistant-main-view-v1:user-admin'), 'personal');
});


test('dashboard does not renew freshness when receiving an old server cache', async () => {
  const payload = dashboard('live', 2);
  payload.meta.fetchedAt = new Date(Date.now() - 120000).toISOString();
  const h = await harness('member', undefined, async () => ({ ok: true, payload }));
  await h.api.loadDashboard();
  await h.api.ensureDashboardFresh();
  assert.equal(h.calls.length, 2);
  assert.match(h.els.dashboardGeneratedAt.textContent, /조회 기준/);
});


test('a fresh snapshot reuses its validated aggregation window across tab visits', async () => {
  const payload = dashboard('snapshot', 2);
  payload.meta.fetchedAt = new Date(Date.now() - 600000).toISOString();
  payload.meta.cacheUntil = new Date(Date.now() + 50000).toISOString();
  const h = await harness('member', undefined, async () => ({ok:true, payload}));
  await h.api.loadDashboard();
  await h.api.ensureDashboardFresh();
  assert.equal(h.calls.length, 1);
  payload.meta.cacheUntil = new Date(Date.now() - 1).toISOString();
  await h.api.ensureDashboardFresh();
  assert.equal(h.calls.length, 2);
});

test('priority queue follows the metrics and filters stay above the scrolling body', async () => {
  const h = await harness('member', undefined, async () => ({ ok: true, payload: filterableDashboard() }));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.ok(html.indexOf('dashboard-metrics') < html.indexOf('dashboard-priority-section'));
  assert.ok(html.indexOf('dashboard-priority-section') < html.indexOf('dashboard-decision'));
  assert.doesNotMatch(html, /dashboard-filterbar/);
  assert.equal(h.els.dashboardFilterbar.hidden, false);
  assert.match(h.els.dashboardFilterbar.innerHTML, /없음/);
  assert.match(html, /data-dashboard-metric="open" aria-pressed="true"/);

  h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-metric]', { dashboardMetric: 'overdue' }) });
  assert.match(h.els.dashboardFilterbar.innerHTML, /일정 지연 ×/);
  assert.match(h.els.dashboardFilterbar.innerHTML, /우선 확인 이슈 1건 보기/);
  assert.match(h.els.dashboardBody.innerHTML, /1 \/ 3건/);
  assert.match(h.els.dashboardBody.innerHTML, /data-dashboard-metric="open" aria-pressed="false"/);

  // '미종료 이슈' 카드는 지표 필터를 푸는 전체 보기로 동작한다.
  h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-metric]', { dashboardMetric: 'open' }) });
  assert.match(h.els.dashboardFilterbar.innerHTML, /없음/);
  assert.match(h.els.dashboardBody.innerHTML, /3 \/ 3건/);
});

test('customer rows toggle their filter and the filter bar hides while reloading', async () => {
  let finish;
  let requests = 0;
  const h = await harness('member', undefined, async () => (++requests === 1
    ? { ok: true, payload: filterableDashboard() }
    : new Promise((resolve) => { finish = resolve; })));
  await h.api.loadDashboard();
  const pickCustomer = () => h.els.dashboardBody.handlers.click({
    target: clickTarget('[data-dashboard-customer]', { dashboardCustomer: '고객사 A' }),
  });
  pickCustomer();
  assert.match(h.els.dashboardFilterbar.innerHTML, /고객사: 고객사 A ×/);
  assert.match(h.els.dashboardBody.innerHTML, /2 \/ 3건/);
  pickCustomer();
  assert.match(h.els.dashboardFilterbar.innerHTML, /없음/);
  assert.match(h.els.dashboardBody.innerHTML, /3 \/ 3건/);

  const reloading = h.api.loadDashboard(true);
  assert.equal(h.els.dashboardFilterbar.hidden, true);
  finish({ ok: true, payload: filterableDashboard() });
  await reloading;
  assert.equal(h.els.dashboardFilterbar.hidden, false);
});

test('expired snapshot and live error never display zero dashboard metrics', async () => {
  const payload = dashboard('error', 0);
  payload.meta.sourceReason = '저장 데이터 만료 및 실시간 조회 실패';
  const h = await harness('member', undefined, async () => ({ok:true, payload}));
  await h.api.loadDashboard();
  assert.match(h.els.dashboardBody.innerHTML, /저장 데이터 만료/);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /dashboard-metrics/);
});
