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
    querySelector() { return { textContent: '', addEventListener() {} }; },
    focus() { focused = name; },
  });
  const els = Object.fromEntries([
    'viewChatBtn', 'viewPersonalBtn', 'viewDashboardBtn', 'chatPanel', 'personalPanel',
    'dashboardPanel', 'dashboardRefreshBtn', 'dashboardPeriod', 'dashboardBody',
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
  meta: { source, scope: 'team', periodFrom: '2026-08-13', periodTo: '2026-09-11', periodDays: 30, generatedAt: '2026-09-11T12:00:00' },
  user: { role: 'member' },
  summary: { open, carriedOpen: 0, overdue: 0, aging: 0, highRisk: 0, highPriority: 0, changeRate: 0 },
  decisionSummary: { riskLevel: 'watch', headline: `${source} 업무 요약`, signals: [], recommendedActions: [] },
  todos: [], customerHealth: [], customerHealthSummary: {}, customerSpikes: [], screenSpikes: [], members: [], priorities: [],
});

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
