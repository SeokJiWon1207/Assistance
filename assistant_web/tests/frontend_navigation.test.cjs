const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness(role, stored, fetcher, { adminCenter = false } = {}) {
  const calls = [];
  const accountMenuCloses = [];
  const reveals = [];
  const storage = new Map(stored ? [[`team-ai-assistant-main-view-v1:user-${role}`, stored]] : []);
  let focused;
  const element = (name) => ({
    hidden: true, disabled: false, tabIndex: -1, innerHTML: '', textContent: '', attributes: {}, handlers: {},
    classList: { toggle() {}, add() {}, remove() {} },
    getAttribute(key) { return this.attributes[key]; },
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return { textContent: '', addEventListener() {}, focus() {}, scrollIntoView() {} }; },
    focus() { focused = name; },
  });
  const els = Object.fromEntries([
    'viewChatBtn', 'viewPersonalBtn', 'viewDashboardBtn', 'chatPanel', 'personalPanel',
    'dashboardPanel', 'dashboardRefreshBtn', 'dashboardPeriod', 'dashboardFilterbar', 'dashboardBody',
    'dashboardDetail', 'dashboardDetailClose', 'dashboardDetailTitle', 'dashboardDetailBody', 'dashboardDetailContext', 'workspaceContent', 'dashboardSource', 'dashboardSubtitle',
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
  const bridge = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/bklit_charts.js'), 'utf8'), {context});
  await bridge.link(() => {}); await bridge.evaluate();
  const weeklyModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard_weekly.js'), 'utf8'), {context});
  await weeklyModule.link(() => bridge); await weeklyModule.evaluate();
  const dependencies = {
    './runtime.js': { state, els, WORKSPACE_ENDPOINTS: { dashboard: '/dashboard' },
      apiFetch: async (url) => { calls.push(url); return fetcher ? fetcher(url) : { ok: false }; } },
    './api.js': { responseJson: async (response) => response.payload || ({ detail: '연결 오류' }) },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './motion.js': { revealOnScroll: (container, selector) => { reveals.push({ container, selector }); return () => {}; } },
    './sidebar.js': {
      closeMobileSidebar: () => { context.document.body.sidebarOpen = false; },
      closeAccountMenu: () => { accountMenuCloses.push(true); },
    },
    './dialog_focus.js': { openDialogFocus() {}, closeDialogFocus() {} },
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
      workspaceIssueContext: () => ({items:state.myWorkspace?.mantis?.issues || [], label:'내 담당 미종료 이슈'}),
    },
  };
  const formatModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/ui_format.js'), 'utf8'), {context});
  await formatModule.link(() => {}); await formatModule.evaluate();
  formatModule.namespace.setCustomerNames({projects: {'159eBest':'LS증권', '143WooriFuture':'NH선물', '164NHFuture':'NH선물'}, exact:{}});
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard.js'), 'utf8'), { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./bklit_charts.js')) return bridge;
    if (specifier.startsWith('./dashboard_weekly.js')) return weeklyModule;
    if (specifier.startsWith('./ui_format.js')) return formatModule;
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  const loaderCalls = [];
  module.namespace.setMainViewLoader('admin', () => loaderCalls.push('admin'));
  module.namespace.initDashboard();
  return { loaderCalls, badges, reveals, api: module.namespace, els, state, storage, calls, htmlWrites, accountMenuCloses, focus: () => focused };
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

test('personal issue opens the shared detail without fetching or switching to the dashboard', async () => {
  const h = await harness('member');
  h.api.showPersonalView({load:false});
  h.state.myWorkspace.mantis = {issues:[
    {id:20,title:'개인 업무',status:'developed',handler:'담당자',ageDays:34,riskLevel:'attention',riskReasons:['34일 장기 미종료'],recommendedAction:'다음 단계 확인'},
    {id:21,title:'다음 업무',isOpen:false,status:'closed',ageDays:100},
  ]};
  let restored = 0;
  const opener = {isConnected:true, focus:()=>restored++};
  assert.equal(typeof h.api.openWorkspaceIssueDetail, 'function');
  h.api.openWorkspaceIssueDetail('20', opener);
  assert.equal(h.els.personalPanel.hidden, false);
  assert.equal(h.els.dashboardPanel.hidden, true);
  assert.equal(h.els.dashboardDetail.hidden, false);
  assert.deepEqual(h.calls, []);
  assert.match(h.els.dashboardDetailBody.innerHTML, /dashboard-detail-hero/);
  assert.match(h.els.dashboardDetailBody.innerHTML, /34일 장기 미종료/);
  assert.match(h.els.dashboardDetailBody.innerHTML, /내 담당 미종료 이슈 1 \/ 2/);
  h.els.dashboardDetail.handlers.click({target:{closest:query=>query==='[data-dashboard-detail-step]' ? {dataset:{dashboardDetailStep:'21'},getAttribute:()=> '다음 이슈'} : null}});
  assert.equal(h.els.dashboardDetailTitle.textContent, '#21 이슈 상세');
  assert.doesNotMatch(h.els.dashboardDetailBody.innerHTML, /우선 확인 대상으로 분류|위험 판단 근거|dashboard-risk-badge/);
  h.els.dashboardDetailClose.handlers.click();
  assert.equal(restored, 1);
  assert.equal(h.els.dashboardDetail.hidden, true);
});

test('switching the main view closes a personal detail without returning focus into the hidden view', async () => {
  const h = await harness('member');
  h.api.showPersonalView({load:false});
  h.state.myWorkspace.mantis = {issues:[{id:1,title:'업무'}]};
  let restored = 0;
  assert.equal(typeof h.api.openWorkspaceIssueDetail, 'function');
  h.api.openWorkspaceIssueDetail('1',{isConnected:true,focus:()=>restored++});
  h.api.showChatView({load:false});
  assert.equal(h.els.dashboardDetail.hidden,true);
  assert.equal(restored,0);
  assert.equal(h.els.chatPanel.hidden,false);
});

function distributionSpecs(html) {
  return [...html.matchAll(/data-bklit-spec="([^"]+)"/g)].map(([, value]) => JSON.parse(value.replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&')));
}

test('distribution charts retain real counts, age exclusions and zero-load members', async () => {
  const payload = filterableDashboard();
  payload.openIssues = [...payload.priorities, {id:4, project:'<img src=x>', title:'등록일 미상', ageDays:null}];
  payload.members = [{mantisUsername:'busy',displayName:'담당자',open:5,overdue:1,agingOnly:2}, {mantisUsername:'zero',displayName:'미배정',open:0,overdue:0,agingOnly:0}];
  const h = await harness('member', undefined, async () => ({ok:true,payload}));
  await h.api.loadDashboard();
  const charts = distributionSpecs(h.els.dashboardBody.innerHTML);
  const customer = charts.find(s=>s.title==='고객사별 미종료');
  assert.equal(customer.kind, 'customer-ranking');
  assert.equal(customer.rows[0].overdue, 1);
  assert.equal(customer.action, 'data-dashboard-customer');
  const age = charts.find(s=>s.title==='미종료 경과일');
  assert.equal(age.kind, 'age-donut');
  assert.deepEqual(age.rows.map(r=>r.count), [1,0,2,0]);
  assert.equal(age.unknown, 1);
  assert.equal(age.action, 'data-dashboard-age');
  const members = charts.find(s=>s.title==='팀원별 부하');
  assert.equal(members.kind, 'member-heatmap');
  assert.equal(members.rows.length, 2);
  assert.equal(members.rows[0].rest, 2);
  assert.equal(members.rows[1].rest, 0);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /<img src=x>/);
  h.els.dashboardBody.handlers.click({target:clickTarget('[data-dashboard-age]',{dashboardAge:'30-59'})});
  assert.match(h.els.dashboardBody.innerHTML, /2 \/ 4건/);
  assert.equal(distributionSpecs(h.els.dashboardBody.innerHTML).find(s=>s.kind==='age-donut').selected,'30-59');
});

test('partial distribution retains lower bounds and customer groups outside the top six', async () => {
  const payload = dashboard('snapshot', 8);
  payload.meta.search_complete = false;
  payload.openIssues = Array.from({length:8},(_,i)=>({id:i,project:`고객사 ${i}`,ageDays:60}));
  const h = await harness('member',undefined,async()=>({ok:true,payload}));
  await h.api.loadDashboard();
  const charts = distributionSpecs(h.els.dashboardBody.innerHTML);
  const customer = charts.find(s=>s.title==='고객사별 미종료');
  assert.equal(customer.rows.length,6);
  assert.equal(customer.otherCount,2);
  assert.equal(customer.otherCustomers,2);
  assert.equal(customer.complete,false);
  assert.equal(charts.find(s=>s.title==='미종료 경과일').complete,false);
  assert.match(h.els.dashboardBody.innerHTML,/2건 이상/);
});

test('filters and detail use the entire open population beyond the priority preview', async () => {
  const payload = filterableDashboard();
  payload.openIssues = [...payload.priorities, {
    id: 99, title: '상위 목록 밖', project: '고객사 A', overdue: true, ageDays: 55,
    riskLevel: 'high', status: 'developed', priority: 'normal',
  }];
  const h = await harness('member', undefined, async () => ({ok: true, payload}));
  await h.api.loadDashboard();
  assert.match(h.els.dashboardBody.innerHTML, /4 \/ 4건/);
  assert.equal(h.reveals.length, 1);
  assert.equal(h.reveals[0].container, h.els.dashboardBody);
  for (const [metric, count] of [['overdue', 2], ['aging', 3], ['highRisk', 2]]) {
    h.els.dashboardBody.handlers.click({target: clickTarget('[data-dashboard-metric]', {dashboardMetric: metric})});
    assert.match(h.els.dashboardBody.innerHTML, new RegExp(`${count} / 4건`));
    assert.match(h.els.dashboardBody.innerHTML, /상위 목록 밖/);
  }
  // 필터로 다시 그릴 때는 등장 효과 없이 바로 보여 준다.
  assert.equal(h.reveals.length, 1);
  await h.api.loadDashboard(true);
  assert.equal(h.reveals.length, 1);
  h.els.dashboardBody.handlers.click({target: clickTarget('[data-dashboard-issue]', {dashboardIssue: '99'})});
  assert.equal(h.els.dashboardDetail.hidden, false);
  assert.match(h.els.dashboardDetailBody.innerHTML, /dashboard-issue-status">개발완료<\/span>/);
  assert.match(h.els.dashboardDetailBody.innerHTML, /우선순위<\/span><strong>보통<\/strong>/);
  assert.doesNotMatch(h.els.dashboardDetailBody.innerHTML, /developed|normal/);
});

test('new issue change is separated from open count and partial totals are explicit', async () => {
  const payload = dashboard('live', 69);
  payload.meta.search_complete = false;
  payload.summary.changeRate = 12.8;
  const h = await harness('member', undefined, async () => ({ok: true, payload}));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.match(html, /dashboard-refresh-status is-warning/);
  assert.match(html, /실제보다 적을 수 있어요/);
  assert.match(html, /workspace-empty is-warning/);
  assert.match(html, /확인한 범위 기준/);
  assert.match(html, /확인된 미종료/);
  assert.match(html, /확인 범위 · 비교 미확정/);
  assert.doesNotMatch(html, /신규 증가 12.8%/);
  assert.equal((html.match(/dashboard-lower-bound/g) || []).length, 5);
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

test('dashboard manual refresh keeps the last successful view until completion', async () => {
  let finish;
  const h = await harness('member', undefined, async (url) => url.includes('refresh=true')
    ? new Promise((resolve) => { finish = resolve; })
    : { ok: true, payload: dashboard('snapshot', 5) });
  await h.api.loadDashboard();
  assert.match(h.els.dashboardBody.innerHTML, /snapshot 업무 요약/);
  const loading = h.api.loadDashboard(true);
  assert.match(h.els.dashboardBody.innerHTML, /snapshot 업무 요약/);
  assert.equal(h.els.dashboardBody.attributes['aria-busy'], 'true');
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
  const payload = filterableDashboard();
  payload.intakeTrend = [{date:'2026-09-10',count:2},{date:'2026-09-11',count:0}];
  const h = await harness('member', undefined, async () => ({ ok: true, payload }));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  // 판단 → 지표 → 우선 확인 목록 → 분석 차트 순서로 읽힌다.
  assert.ok(html.indexOf('dashboard-decision') < html.indexOf('dashboard-metrics'));
  assert.ok(html.indexOf('dashboard-metrics') < html.indexOf('dashboard-grid'));
  // 중복 집계 기준은 한 번 안내하고 각 차트에는 실제 값의 Bklit 범례를 둔다.
  assert.equal(html.split('지연과 장기가 겹치면 지연으로 집계합니다.').length - 1, 1);
  assert.match(html, /data-bklit-kind="customer-ranking"/);
  assert.ok(html.indexOf('dashboard-metrics') < html.indexOf('dashboard-priority-section'));
  assert.ok(html.indexOf('dashboard-priority-section') < html.indexOf('dashboard-grid'));
  assert.doesNotMatch(html.slice(html.indexOf('dashboard-metrics'), html.indexOf('dashboard-priority-section')), /data-bklit-kind="sparkline"/);
  assert.deepEqual(distributionSpecs(html).find(spec => spec.kind === 'trend').points, payload.intakeTrend);
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

test('decision disclosure preserves its state through filters without rerendering charts', async () => {
  const payload = filterableDashboard();
  payload.decisionSummary.signals = ['확인할 근거'];
  const h = await harness('member', undefined, async () => ({ok: true, payload}));
  await h.api.loadDashboard();
  assert.match(h.els.dashboardBody.innerHTML, /data-dashboard-decision-toggle aria-expanded="false" aria-controls="dashboardDecisionSupport"/);
  const writes = h.htmlWrites.length;
  const control = {setAttribute(key, value) {this[key] = value;}};
  const target = {closest: selector => selector === '[data-dashboard-decision-toggle]' ? control : null};
  h.els.dashboardBody.handlers.click({target});
  assert.equal(control['aria-expanded'], 'true');
  assert.equal(h.htmlWrites.length, writes);
  h.els.dashboardBody.handlers.click({target:clickTarget('[data-dashboard-metric]',{dashboardMetric:'overdue'})});
  assert.match(h.els.dashboardBody.innerHTML, /data-dashboard-decision-toggle aria-expanded="true"/);
  h.els.dashboardBody.handlers.click({target});
  assert.equal(control['aria-expanded'], 'false');
});

test('customer rows toggle their filter and preserve controls while reloading', async () => {
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
  assert.equal(h.els.dashboardFilterbar.hidden, false);
  finish({ ok: true, payload: filterableDashboard() });
  await reloading;
  assert.equal(h.els.dashboardFilterbar.hidden, false);
});

test('age columns filter the queue and the age chip clears only that filter', async () => {
  const h = await harness('member', undefined, async () => ({ ok: true, payload: filterableDashboard() }));
  await h.api.loadDashboard();
  const pickAge = (key) => h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-age]', { dashboardAge: key }) });
  pickAge('30-59');
  assert.match(h.els.dashboardFilterbar.innerHTML, /data-dashboard-clear="age"[^>]*>경과: 30–59일<svg data-icon="close">/);
  assert.match(h.els.dashboardBody.innerHTML, /2 \/ 3건/);
  assert.match(h.els.dashboardBody.innerHTML, /data-dashboard-age="30-59" aria-pressed="true"/);

  // 고객사 필터와 겹치면 둘 다 만족하는 이슈만 남고, 경과 칩은 경과 조건만 푼다.
  h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-customer]', { dashboardCustomer: '고객사 A' }) });
  assert.match(h.els.dashboardBody.innerHTML, /1 \/ 3건/);
  h.els.dashboardFilterbar.handlers.click({ target: clickTarget('[data-dashboard-clear]', { dashboardClear: 'age' }) });
  assert.doesNotMatch(h.els.dashboardFilterbar.innerHTML, /경과:/);
  assert.match(h.els.dashboardFilterbar.innerHTML, /고객사: 고객사 A/);
  assert.match(h.els.dashboardBody.innerHTML, /2 \/ 3건/);

  pickAge('0-7');
  h.els.dashboardFilterbar.handlers.click({ target: clickTarget('[data-dashboard-clear]', { dashboardClear: 'all' }) });
  assert.match(h.els.dashboardFilterbar.innerHTML, /없음/);
  assert.match(h.els.dashboardBody.innerHTML, /3 \/ 3건/);
});

test('priority queue previews five rows and toggles the rest', async () => {
  const payload = filterableDashboard();
  payload.openIssues = Array.from({ length: 7 }, (_, index) => ({
    id: index + 1, title: `이슈 ${index + 1}`, project: '고객사 A', overdue: false, ageDays: index, riskLevel: 'watch',
  }));
  const h = await harness('member', undefined, async () => ({ ok: true, payload }));
  await h.api.loadDashboard();
  const rows = () => (h.els.dashboardBody.innerHTML.match(/data-dashboard-row-issue=/g) || []).length;
  assert.equal(rows(), 5);
  assert.match(h.els.dashboardBody.innerHTML, /data-dashboard-more aria-expanded="false">나머지 2건 보기/);
  h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-more]', {}) });
  assert.equal(rows(), 7);
  assert.match(h.els.dashboardBody.innerHTML, /data-dashboard-more aria-expanded="true">상위 5건만 보기/);
  h.els.dashboardBody.handlers.click({ target: clickTarget('[data-dashboard-more]', {}) });
  assert.equal(rows(), 5);
});

test('priority risk summary follows the full filtered list and marks partial data', async () => {
  const payload = filterableDashboard();
  payload.meta.search_complete = false;
  payload.priorities.push(...Array.from({length: 4}, (_, i) => ({id: i + 4, title: '<img src=x>', project: '고객사 B', riskLevel: 'watch'})));
  const h = await harness('member', undefined, async () => ({ok: true, payload}));
  await h.api.loadDashboard();
  const section = () => h.els.dashboardBody.innerHTML.split('aria-label="우선 확인 이슈"')[1];
  assert.match(section(), /aria-label="위험 1건 이상"/);
  assert.match(section(), /aria-label="주의 1건 이상"/);
  assert.match(section(), /aria-label="관찰 5건 이상"/);
  assert.match(section(), /확인한 범위/);
  assert.doesNotMatch(section(), /<img src=x>/);
  h.els.dashboardBody.handlers.click({target: clickTarget('[data-dashboard-customer]', {dashboardCustomer: '고객사 A'})});
  assert.match(section(), /aria-label="위험 1건 이상"/);
  assert.match(section(), /aria-label="주의 0건 이상"/);
  assert.match(section(), /aria-label="관찰 1건 이상"/);
});

test('charts count an overdue aging issue once and intake shows the new issue split', async () => {
  const payload = filterableDashboard();
  Object.assign(payload.summary, { newIssues: 28, closedNow: 19, previousNewIssues: 25, changeRate: 12 });
  payload.members = [
    { displayName: '김팀원', role: 'member', open: 5, overdue: 2, agingOnly: 1, highRisk: 1 },
    { displayName: '이팀원', role: 'member', open: 7, overdue: 0, agingOnly: 0, highRisk: 0 },
  ];
  const h = await harness('member', undefined, async () => ({ ok: true, payload }));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.match(html, /이번 기간 신규 이슈 28건 중 종료 19건, 미종료 9건/);
  assert.match(html, /종료 19<\/b> · <b class="is-open">미종료 9<\/b> · 직전 기간 25건/);
  assert.match(html, /신규 증가 12%/);
  // 고객사 A의 #1은 지연이면서 40일이 지났지만 지연 구간에만 한 번 들어간다.
  assert.match(html, /aria-label="고객사 A 미종료 2건, 지연 1건, 장기 0건, 그 외 1건/);
  assert.match(html, /aria-label="고객사 B 미종료 1건, 지연 0건, 장기 1건, 그 외 0건/);
  assert.match(html, /30일 이상 2건이 장기 미종료입니다\./);
  // 팀원 막대는 미종료 많은 순서이고, 나머지는 미종료에서 지연·장기를 뺀 값이다.
  assert.ok(html.indexOf('이팀원') < html.indexOf('김팀원'));
  assert.match(html, /aria-label="김팀원 [^"]*미종료 5건, 지연 2건, 장기 1건, 그 외 2건, 고위험 1건"/);
  assert.doesNotMatch(html, /dashboard-grid is-two/);
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
  const h = await harness('member', undefined, async () => ({ ok: true, payload }));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  // 표 행·카드에 버튼 역할을 덧씌우지 않고, 누를 곳은 실제 <button>으로 둔다.
  assert.doesNotMatch(html, /role="button"|tabindex="0"/);
  assert.match(html, /<tr class="dashboard-clickable" data-dashboard-row-issue="1" aria-selected="false">/);
  assert.match(html, /<button type="button" class="dashboard-issue-button" data-dashboard-issue="1">/);
  // 차트 막대도 실제 버튼이며, 급증 신호는 해당 고객사 막대의 배지로 붙는다.
  assert.match(html, /<button type="button" class="dashboard-bar dashboard-clickable" data-dashboard-customer="고객사 A" aria-pressed="false" aria-label="고객사 A 미종료 2건, 지연 1건, 장기 0건, 그 외 1건, 위험">/);
  assert.match(html, /<button type="button" class="dashboard-bar dashboard-clickable" data-dashboard-customer="고객사 B"[^>]*>[\s\S]*?<b class="dashboard-spike-badge" title="고객사 B 4건\(직전 1건\)">급증 ×4<\/b>/);
  assert.match(html, /<button type="button" class="dashboard-column dashboard-clickable is-aging" data-dashboard-age="30-59" aria-pressed="false" aria-label="경과 30–59일 미종료 2건">/);
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
  assert.doesNotMatch(html, /team-brief-section/);
  assert.match(html, /data-bklit-values/);
  assert.match(html, /data-open-team-brief/);
  await h.els.dashboardBody.handlers.click({ target: clickTarget('[data-open-team-brief]', {}) });
  assert.equal(h.els.personalPanel.hidden, false);
  assert.equal(h.els.dashboardPanel.hidden, true);
  assert.ok(h.calls.includes('team-brief'));
});


test('customer labels never change dashboard filter identities or merge equal display names', async () => {
  const payload = dashboard('live', 3);
  payload.openIssues = [
    {id:11,title:'이전 프로젝트',project:'143WooriFuture(우리선물)',status:'confirmed'},
    {id:12,title:'현재 프로젝트',project:'164NHFuture',status:'confirmed'},
    {id:13,title:'LS 이슈',project:'159eBest',status:'confirmed'},
  ];
  const h = await harness('member', undefined, async () => ({ok:true,payload}));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.match(html, /data-dashboard-customer="143WooriFuture\(우리선물\)"/);
  assert.match(html, /data-dashboard-customer="164NHFuture"/);
  assert.equal((html.match(/aria-label="NH선물 미종료 1건/g) || []).length, 2);
  assert.match(html, /data-label="고객사"><span>LS증권<\/span><\/td>/);
  h.els.dashboardBody.handlers.click({target:clickTarget('[data-dashboard-customer]', {dashboardCustomer:'164NHFuture'})});
  assert.match(h.els.dashboardBody.innerHTML, /1 \/ 3건/);
  assert.match(h.els.dashboardBody.innerHTML, /현재 프로젝트/);
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /<button[^>]*data-dashboard-issue="11"/);
  assert.match(h.els.dashboardFilterbar.innerHTML, /고객사: NH선물/);
  h.els.dashboardBody.handlers.click({target:clickTarget('[data-dashboard-issue]', {dashboardIssue:'13'})});
  assert.match(h.els.dashboardDetailBody.innerHTML, /<strong>LS증권<\/strong>/);
  assert.equal(payload.openIssues[2].project, '159eBest');
});

test('partial empty charts do not assert absence of open issues', async () => {
  const payload = dashboard('snapshot', 0);
  payload.meta.search_complete = false;
  payload.decisionSummary = {riskLevel:'unknown', headline:'일부 데이터만 확인했습니다.'};
  const h = await harness('member', null, async () => ({ok:true, payload}));
  await h.api.loadDashboard();
  assert.doesNotMatch(h.els.dashboardBody.innerHTML, /미종료 이슈가 없습니다\.|dashboard-decision-badge">안정/);
  assert.match(h.els.dashboardBody.innerHTML, /판단 보류/);
});

test('new cohort unknown status is shown separately from open and closed', async () => {
  const payload = dashboard('live', 0);
  payload.summary = {...payload.summary, newIssues:3, closedNow:1, newOpen:1, unknownStatus:1};
  const h = await harness('member', null, async () => ({ok:true, payload}));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.match(html, /종료 1건, 미종료 1건, 상태 미상 1건/);
  assert.doesNotMatch(html, /미종료 2<\/b>/);
});


test('partial distribution charts identify observed counts as lower bounds', async () => {
  const payload = filterableDashboard();
  payload.meta.search_complete = false;
  payload.members = [{displayName:'검증 팀원', role:'member', open:0, overdue:0, agingOnly:0, highRisk:0}];
  const h = await harness('member', null, async () => ({ok:true, payload}));
  await h.api.loadDashboard();
  const html = h.els.dashboardBody.innerHTML;
  assert.match(html, /aria-label="고객사 A 미종료 2건 이상/);
  assert.match(html, /aria-label="경과 30–59일 미종료 2건 이상/);
  assert.match(html, /aria-label="검증 팀원 팀원 미종료 0건 이상/);
});
