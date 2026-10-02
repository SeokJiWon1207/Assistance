const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness(role, stored, fetcher, { adminCenter = false } = {}) {
  const calls = [];
  const accountMenuCloses = [];
  const storage = new Map(stored ? [[`team-ai-assistant-main-view-v1:user-${role}`, stored]] : []);
  let focused;
  const element = (name) => ({
    hidden: true, disabled: false, tabIndex: -1, innerHTML: '', textContent: '', attributes: {}, handlers: {},
    classList: { toggle() {}, remove() {} },
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return { textContent: '', addEventListener() {}, focus() {}, scrollIntoView() {} }; },
    focus() { focused = name; },
  });
  const els = Object.fromEntries([
    'viewChatBtn', 'viewPersonalBtn', 'viewDashboardBtn', 'chatPanel', 'personalPanel',
    'dashboardPanel', 'dashboardRefreshBtn', 'dashboardPeriod', 'dashboardFilterbar', 'dashboardBody',
    'dashboardDetail', 'dashboardDetailClose', 'dashboardDetailTitle', 'dashboardDetailBody', 'workspaceContent', 'dashboardSource', 'dashboardSubtitle',
    ...(adminCenter ? ['viewAdminBtn', 'adminPanel'] : []),
  ].map((name) => [name, element(name)]));
  els.appShell = { dataset: { view: 'chat' } };
  const htmlWrites = [];
  const badges = [];
  Object.defineProperty(els.dashboardBody, 'innerHTML', {
    get: () => htmlWrites.at(-1) || '',
    set: (html) => htmlWrites.push(html),
  });
  els.viewChatBtn.hidden = els.viewPersonalBtn.hidden = false;
  const state = {
    currentUser: { uid: `user-${role}`, role }, workspaceTab: 'overview', myWorkspace: { summary: {} },
    adminToolsVisible: adminCenter && role === 'admin',
  };
  const context = vm.createContext({
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    document: { body: { classList: { remove() {} } }, addEventListener() {} },
    CSS: { escape: (value) => String(value) },
  });
  const dependencies = {
    './runtime.js': { state, els, WORKSPACE_ENDPOINTS: { dashboard: '/dashboard' },
      apiFetch: async (url) => { calls.push(url); return fetcher ? fetcher(url) : { ok: false }; } },
    './api.js': { responseJson: async (response) => response.payload || ({ detail: '연결 오류' }) },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './sidebar.js': {
      closeMobileSidebar: () => { context.document.body.sidebarOpen = false; },
      closeAccountMenu: () => { accountMenuCloses.push(true); },
    },
    './data_badge.js': {
      dataBadgeState: ({ source, complete = true, refreshing = false, loading = false } = {}) => (loading ? 'loading' : source === 'error' ? 'error' : refreshing ? 'refreshing' : source === 'snapshot' ? 'snapshot' : complete === false ? 'partial' : 'fresh'),
      formatBadgeTime: (value) => (value ? '오후 2:02' : ''),
      mantisBadgeDetails: (state) => ({ source: { name: 'Mantis', tone: 'ok', text: state }, note: '', retry: ['partial', 'snapshot', 'error'].includes(state) }),
      renderDataBadge: (slot, options) => { if (slot) { slot.badge = options; badges.push(options); } },
    },
    './workspace.js': {
      ensureMyWorkspaceFresh: async () => { calls.push('personal'); }, renderWorkspace() {},
      loadTeamWeeklyBrief: async () => {}, renderTeamWeeklyBrief: () => '', markTeamWeeklyBriefRead: async () => {},
      showTeamBrief: async () => { calls.push('team-brief'); },
      workspaceStatusLabel: (value) => ({developed: '개발완료', confirmed: '확인된 이슈'}[value] || value || '상태 미상'),
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
  const loaderCalls = [];
  module.namespace.setMainViewLoader('admin', () => loaderCalls.push('admin'));
  module.namespace.initDashboard();
  return { loaderCalls, badges, api: module.namespace, els, state, storage, calls, htmlWrites, accountMenuCloses, focus: () => focused };
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

test('filters and detail use the entire open population beyond the priority preview', async () => {
  const payload = filterableDashboard();
  payload.openIssues = [...payload.priorities, {
    id: 99, title: '상위 목록 밖', project: '고객사 A', overdue: true, ageDays: 55,
    riskLevel: 'high', status: 'developed', priority: 'normal',
  }];
  const h = await harness('member', undefined, async () => ({ok: true, payload}));
  await h.api.loadDashboard();
  assert.match(h.els.dashboardBody.innerHTML, /4 \/ 4건/);
  for (const [metric, count] of [['overdue', 2], ['aging', 3], ['highRisk', 2]]) {
    h.els.dashboardBody.handlers.click({target: clickTarget('[data-dashboard-metric]', {dashboardMetric: metric})});
    assert.match(h.els.dashboardBody.innerHTML, new RegExp(`${count} / 4건`));
    assert.match(h.els.dashboardBody.innerHTML, /상위 목록 밖/);
  }
  h.els.dashboardBody.handlers.click({target: clickTarget('[data-dashboard-issue]', {dashboardIssue: '99'})});
  assert.equal(h.els.dashboardDetail.hidden, false);
  assert.match(h.els.dashboardDetailBody.innerHTML, /개발완료 · 보통/);
  assert.doesNotMatch(h.els.dashboardDetailBody.innerHTML, /developed|normal/);
});

test('new issue change is separated from open count and partial totals are explicit', async () => {
  const payload = dashboard('live', 69);
  payload.meta.search_complete = false;
  payload.summary.changeRate = 12.8;
  const h = await harness('member', undefined, async () => ({ok: true, payload}));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.match(html, /일부 이슈 기준이며 팀 전체 건수가 아닙니다/);
  assert.match(html, /확인된 미종료/);
  assert.match(html, /신규 증가 12.8%/);
  assert.ok(html.indexOf('dashboard-new-change') > html.indexOf('data-dashboard-metric="highRisk"'));
});

test('detached sidebar controls use their original event path instead of closing the menu', () => {
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/events.js'), 'utf8');
  const start = source.indexOf('document.addEventListener("click", (event) => {');
  const end = source.indexOf('\n});', start) + 4;
  let handler, closed = 0;
  const sidebar = {}, menu = {};
  vm.runInNewContext(source.slice(start, end), {
    document: {body: {classList: {contains: () => true}}, addEventListener: (_, fn) => {handler = fn;}},
    els: {sidebar, sidebarMenuToggle: menu}, closeMobileSidebar: () => {closed++;}, isMobileSidebarOpen: () => true,
  });
  handler({composedPath: () => [{}, sidebar, {}]});
  handler({composedPath: () => [menu, {}]});
  assert.equal(closed, 0);
  handler({composedPath: () => [{}, {}]});
  assert.equal(closed, 1);
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
  assert.equal(h.els.viewPersonalBtn.attributes['aria-current'], 'page');
  assert.equal(h.els.viewDashboardBtn.attributes['aria-current'], undefined);
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
  assert.match(h.els.dashboardRefreshBtn.innerHTML, /<span>불러오는 중…<\/span>/);
  assert.match(h.els.dashboardRefreshBtn.innerHTML, /data-icon="refresh"/);
  assert.equal(h.els.dashboardRefreshBtn.attributes['aria-busy'], 'true');
  finish({ ok: false });
  await loading;
  assert.equal(h.els.dashboardRefreshBtn.disabled, false);
  assert.match(h.els.dashboardRefreshBtn.innerHTML, /<span>새로고침<\/span>/);
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

test('main menu marks the current view, keeps tab order and shows the chat list only on chat', async () => {
  const h = await harness('member');
  assert.equal(h.els.appShell.dataset.view, 'chat');
  assert.equal(h.els.viewChatBtn.attributes['aria-current'], 'page');
  h.api.showDashboardView({ load: false });
  assert.equal(h.els.appShell.dataset.view, 'dashboard');
  assert.equal(h.els.viewDashboardBtn.attributes['aria-current'], 'page');
  assert.equal(h.els.viewChatBtn.attributes['aria-current'], undefined);
  // 탭 목록이 아니라 화면 이동 메뉴라서 방향키 처리와 roving tabindex를 두지 않는다.
  for (const name of ['viewChatBtn', 'viewPersonalBtn', 'viewDashboardBtn']) {
    assert.equal(h.els[name].handlers.keydown, undefined);
    assert.equal(h.els[name].attributes['aria-selected'], undefined);
  }
  // 화면을 옮기면 열려 있던 내 계정 메뉴도 닫는다.
  assert.ok(h.accountMenuCloses.length > 0);
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
  assert.match(h.els.dashboardSource.badge.detailTime, /조회$/);
  assert.equal(h.els.dashboardSource.badge.state, 'fresh');
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
  assert.match(h.els.dashboardFilterbar.innerHTML, /일정 지연<svg data-icon="close">/);
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
  assert.match(h.els.dashboardFilterbar.innerHTML, /고객사: 고객사 A<svg data-icon="close">/);
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

test('admin center is a fourth menu item only for admins and loads when shown', async () => {
  const admin = await harness('admin', 'admin', null, { adminCenter: true });
  assert.equal(admin.els.viewAdminBtn.hidden, false);
  assert.equal(admin.els.adminPanel.hidden, false);
  assert.equal(admin.els.chatPanel.hidden, true);
  assert.deepEqual(admin.loaderCalls, ['admin']);
  assert.equal(admin.els.appShell.dataset.view, 'admin');
  admin.api.showChatView();
  assert.equal(admin.els.adminPanel.hidden, true);
  assert.equal(admin.els.viewAdminBtn.attributes['aria-current'], undefined);
  assert.equal(admin.els.appShell.dataset.view, 'chat');

  // 관리자가 아닌 계정에 남아 있던 저장값은 대화 화면으로 돌린다.
  const member = await harness('member', 'admin', null, { adminCenter: true });
  assert.equal(member.els.viewAdminBtn.hidden, true);
  assert.equal(member.els.adminPanel.hidden, true);
  assert.equal(member.els.chatPanel.hidden, false);
  assert.deepEqual(member.loaderCalls, []);
});

test('header holds scope, period and comparison once and period buttons reload the window', async () => {
  const h = await harness('member', undefined, async () => ({ ok: true, payload: dashboard('live', 3) }));
  await h.api.loadDashboard();
  assert.equal(h.els.dashboardSubtitle.textContent, '팀 전체 · 8월 13일–9월 11일 · 직전 30일과 비교');
  const html = h.els.dashboardBody.innerHTML;
  assert.doesNotMatch(html, /dashboard-welcome|팀 전체 업무 현황|직전 동일 기간과 비교합니다/);
  assert.doesNotMatch(html, /PRIORITY QUEUE|TODAY|CUSTOMER HEALTH|EARLY SIGNAL|WORKLOAD/);
  assert.equal(h.els.dashboardSource.badge.sources[0].name, 'Mantis');

  const pick = (days) => h.els.dashboardPeriod.handlers.click({
    target: { closest: () => ({ dataset: { dashboardPeriod: String(days) }, disabled: false }) },
  });
  pick(30);
  assert.equal(h.calls.length, 1);
  pick(7);
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(h.calls.at(-1), /days=7/);
});

test('dashboard rows keep table semantics and expose real buttons', async () => {
  const payload = filterableDashboard();
  payload.customerHealth = [{ customer: '고객사 A', level: 'high', label: '위험', reasons: ['지연'], openCount: 2, overdueCount: 1, agingCount: 1 }];
  payload.customerSpikes = [{ name: '고객사 B', count: 4, previousCount: 1, ratio: 4 }];
  payload.todos = [{ id: 7, title: '오늘 볼 이슈', project: '고객사 A', riskLevel: 'high', recommendedAction: '확인' }];
  const h = await harness('member', undefined, async () => ({ ok: true, payload }));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  // 표 행·카드에 버튼 역할을 덧씌우지 않고, 누를 곳은 실제 <button>으로 둔다.
  assert.doesNotMatch(html, /role="button"|tabindex="0"/);
  assert.match(html, /<tr class="dashboard-clickable" data-dashboard-row-issue="1">/);
  assert.match(html, /<button type="button" class="dashboard-issue-button" data-dashboard-issue="1">/);
  assert.match(html, /<button type="button" class="workspace-item-title dashboard-card-button" data-dashboard-issue="7">/);
  assert.match(html, /<button type="button" class="health-row dashboard-clickable" aria-pressed="false" data-dashboard-customer="고객사 A">/);
  assert.match(html, /<button class="dashboard-spike-row dashboard-clickable" type="button" data-dashboard-customer="고객사 B" aria-pressed="false">/);
  assert.equal(h.els.dashboardBody.handlers.keydown, undefined);

  // 마우스로 행의 다른 칸을 눌러도 같은 이슈 상세가 열린다.
  h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-row-issue]', { dashboardRowIssue: '1' }) });
  assert.equal(h.els.dashboardDetail.hidden, false);
  assert.match(h.els.dashboardDetailTitle.textContent, /#1 이슈 상세/);
});

test('dashboard points to the single weekly report instead of repeating the team brief', async () => {
  const h = await harness('member', undefined, async () => ({ ok: true, payload: filterableDashboard() }));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.doesNotMatch(html, /team-brief-section|<details/);
  assert.match(html, /data-open-team-brief/);
  await h.els.dashboardBody.handlers.click({ target: clickTarget('[data-open-team-brief]', {}) });
  assert.equal(h.els.personalPanel.hidden, false);
  assert.equal(h.els.dashboardPanel.hidden, true);
  assert.ok(h.calls.includes('team-brief'));
});
