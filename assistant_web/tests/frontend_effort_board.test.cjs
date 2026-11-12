const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const boardPayload = (overrides = {}) => ({
  meta: { source: 'live', search_complete: true, fetchedAt: '2026-11-03T09:40:00+09:00', periodDays: 30,
    periodFrom: '2026-10-05', periodTo: '2026-11-03', scopeLabel: '컨텐츠서비스팀', agingDays: 30 },
  summary: { closedCount: 86, previousClosedCount: 79, medianHandlingDays: 3.5, previousMedianHandlingDays: 2.6,
    p90HandlingDays: 14, agingOpen: 17, agingSoon: 4 },
  distribution: [{ key: 'same-day', label: '당일', count: 18 }, { key: '31+', label: '30일 초과', count: 4 }],
  weekly: [{ weekStart: '2026-10-26', label: '10/26', closedCount: 3, medianDays: 4.6, partialWeek: false },
    { weekStart: '2026-11-02', label: '11/2', closedCount: 0, medianDays: null, partialWeek: true }],
  customers: [{ customer: '108SK(SK증권)', name: 'SK증권', closedCount: 5, medianDays: 6.2 }],
  effort: { total: 12.5, issueCount: 3, types: [{ field: '1문의공수', label: '문의', total: 12.5, issueCount: 3, averagePerIssue: 4.17 }] },
  agingIssues: [
    { id: 7, title: '장기 <이슈>', customerName: 'SK증권', handler: '석지원', status: 'confirmed', ageDays: 41, aging: true },
    { id: 8, title: '곧 넘음', customerName: 'NH증권', handler: '우동헌', status: 'assigned', ageDays: 26, aging: false },
  ],
  agingIssuesTotal: 12,
  ...overrides,
});

async function harness(fetcher) {
  const calls = [];
  const element = (dataset = {}) => ({
    hidden: true, innerHTML: '', dataset, attributes: {}, handlers: {}, tabIndex: 0, focused: 0,
    classList: { toggle(name, value) { this[name] = value; } },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key]; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return null; },
    focus() { this.focused += 1; },
  });
  const view = element();
  const subtitle = element();
  const tools = element();
  const panel = element();
  const tabs = element();
  const tabButtons = [element({ dashboardTab: 'overview' }), element({ dashboardTab: 'effort' })];
  const context = vm.createContext({
    URLSearchParams, Event: class { constructor(type) { this.type = type; } },
    CSS: { escape: (value) => String(value) },
    document: {
      querySelector: (selector) => ({ '#effortBoardView': view, '#dashboardPanel': panel, '#dashboardTabs': tabs,
        '[data-tab-subtitle="effort"]': subtitle, '[data-tab-tools="effort"]': tools })[selector] || null,
      querySelectorAll: (selector) => (selector === '[data-dashboard-tab]' ? tabButtons : []),
    },
  });
  const dependencies = {
    './runtime.js': { els: {}, WORKSPACE_ENDPOINTS: { effortBoard: '/effort' },
      apiFetch: async (url) => { calls.push(url); return fetcher(url, calls.length); } },
    './api.js': { responseJson: async (response) => response.payload },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './ui_format.js': { escapeHtml: (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]), formatPeriodRange: (from, to) => `${from}–${to}`, customerMark: (name) => `<span class="customer-mark" data-mark="${name}"></span>` },
    './data_badge.js': { dataBadgeState: () => 'fresh', formatBadgeTime: () => '', mantisBadgeDetails: () => ({ source: {}, note: '' }),
      renderDataBadge() {} },
    './workspace.js': { workspaceStatusLabel: (value) => ({ confirmed: '확인된 이슈', assigned: '할당된 이슈' }[value] || value) },
    './dashboard.js': { showChatView() {} },
  };
  const tabsModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard_tabs.js'), 'utf8'), { context });
  await tabsModule.link(() => {});
  await tabsModule.evaluate();
  const focusModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/view_focus.js'), 'utf8'), { context });
  await focusModule.link(() => {});
  await focusModule.evaluate();
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/effort_board.js'), 'utf8'), { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./view_focus.js')) return focusModule;
    if (specifier.startsWith('./dashboard_tabs.js')) return tabsModule;
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initEffortBoard();
  tabsModule.namespace.initDashboardTabs();
  const target = (selector, dataset = {}) => ({ closest: (query) => (query === selector ? { dataset } : null) });
  const api = { ...module.namespace, selectTab: tabsModule.namespace.selectDashboardTab };
  return { api, view, subtitle, tools, panel, tabs, tabButtons, calls, target };
}

test('effort tab loads once, renders escaped sections and switches the panel subview', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload() }));
  assert.equal(h.view.hidden, true);
  assert.equal(h.tabButtons[0].attributes['aria-selected'], 'true');
  await h.tabs.handlers.click({ target: h.target('[data-dashboard-tab]', { dashboardTab: 'effort' }) });
  await h.api.loadEffortBoard();
  assert.equal(h.panel.attributes['data-subview'], 'effort');
  assert.equal(h.view.hidden, false);
  assert.equal(h.tabButtons[1].attributes['aria-selected'], 'true');
  assert.equal(h.tabButtons[0].tabIndex, -1);
  const html = h.view.innerHTML;
  assert.match(html, /직전 2\.6일보다 0\.9일 늘었어요/);
  assert.match(html, /장기 미종료/);
  assert.match(html, /30일 이상 · 7일 안에 4건 더/);
  assert.match(html, /장기 &lt;이슈&gt;/);
  assert.match(html, /data-effort-customer="108SK\(SK증권\)"/);
  assert.match(html, /나머지 10건/);
  assert.match(html, /is-soon/);
  assert.doesNotMatch(html, /dashboard-lower-bound/);
  h.tabs.handlers.click({ target: h.target('[data-dashboard-tab]', { dashboardTab: 'overview' }) });
  assert.equal(h.panel.attributes['data-subview'], undefined);
  assert.equal(h.view.hidden, true);
});

test('period changes refetch and stale responses never win', async () => {
  const pending = [];
  const h = await harness((url) => new Promise((resolve) => pending.push({ url, resolve })));
  h.api.selectTab('effort');
  const second = h.tools.handlers.click({ target: h.target('[data-effort-period]', { effortPeriod: '90' }) });
  assert.deepEqual(pending.map((item) => item.url), ['/effort?days=30', '/effort?days=90']);
  pending[1].resolve({ ok: true, payload: boardPayload({ meta: { ...boardPayload().meta, periodDays: 90 }, summary: { ...boardPayload().summary, closedCount: 900 } }) });
  await second;
  pending[0].resolve({ ok: true, payload: boardPayload() });
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(h.view.innerHTML, />900건</);
  assert.match(h.tools.innerHTML, /data-effort-period="90" aria-pressed="true"/);
});

test('tab keys move selection and partial data is marked as lower bounds', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload({ meta: { ...boardPayload().meta, search_complete: false } }) }));
  let prevented = 0;
  h.tabs.handlers.keydown({ key: 'ArrowRight', preventDefault: () => prevented++ });
  await h.api.loadEffortBoard();
  assert.equal(prevented, 1);
  assert.equal(h.tabButtons[1].focused, 1);
  assert.equal(h.panel.attributes['data-subview'], 'effort');
  assert.match(h.view.innerHTML, /dashboard-lower-bound/);
  h.tabs.handlers.keydown({ key: 'Home', preventDefault() {} });
  assert.equal(h.panel.attributes['data-subview'], undefined);
});

test('load errors keep a retry button', async () => {
  const h = await harness(() => ({ ok: false, payload: { detail: '조회 기간은 7일, 14일, 30일, 90일 중 하나여야 합니다.' } }));
  h.api.selectTab('effort');
  await h.api.loadEffortBoard();
  assert.match(h.view.innerHTML, /조회 기간은/);
  assert.match(h.view.innerHTML, /data-effort-refresh/);
});

test('KPI cards mark more closed issues as good and a longer median as a warning', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload() }));
  h.api.selectTab('effort');
  await h.api.loadEffortBoard();
  const html = h.view.innerHTML;
  assert.match(html, /class="workspace-kpis effort-board-metrics"/);
  // 해결 86건(직전 79건) → 7건 늘어 좋은 쪽, 처리일 중앙값 3.5일(직전 2.6일) → 0.9일 늘어 주의.
  assert.match(html, /workspace-kpi-delta is-good"[^>]*>[^<]*<svg data-icon="arrow-up"><\/svg>7건/);
  assert.match(html, /workspace-kpi-delta is-bad"[^>]*>[^<]*<svg data-icon="arrow-up"><\/svg>0\.9일/);
  assert.match(html, /직전 기간보다 7건 늘었어요/);
  // 카드 머리는 운영 현황 카드와 같은 모양이고, 처리일 계산 기준은 머리 설명으로 올렸다.
  assert.match(html, /<header class="ops-card-head"><div><h3>처리일 분포<\/h3><p>등록일부터 마지막 수정일까지를 처리일로 세요\.<\/p><\/div>/);
  assert.match(html, /effort-board-disclaimer/);
});

test('partial data never shows comparison badges', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload({ meta: { ...boardPayload().meta, search_complete: false } }) }));
  h.api.selectTab('effort');
  await h.api.loadEffortBoard();
  assert.doesNotMatch(h.view.innerHTML, /workspace-kpi-delta/);
});

test('aging issues start with five rows and expand or collapse without refetching', async () => {
  const agingIssues = Array.from({ length: 8 }, (_, index) => ({ ...boardPayload().agingIssues[0], id: 100 + index }));
  const h = await harness(() => ({ ok: true, payload: boardPayload({ agingIssues, agingIssuesTotal: 12 }) }));
  await h.api.loadEffortBoard();
  assert.equal((h.view.innerHTML.match(/data-effort-source=/g) || []).length, 5);
  assert.match(h.view.innerHTML, /data-effort-expand[^>]*aria-expanded="false"/);
  await h.view.handlers.click({ target: h.target('[data-effort-expand]') });
  assert.equal((h.view.innerHTML.match(/data-effort-source=/g) || []).length, 8);
  assert.match(h.view.innerHTML, /data-effort-expand[^>]*aria-expanded="true"/);
  assert.match(h.view.innerHTML, /나머지 4건/);
  await h.view.handlers.click({ target: h.target('[data-effort-expand]') });
  assert.equal((h.view.innerHTML.match(/data-effort-source=/g) || []).length, 5);
  assert.equal(h.calls.length, 1);
});

test('weekly handling time distinguishes missing data from valid same-day handling', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload({ weekly: [{ label: '11/2', medianDays: null, closedCount: 0 }] }) }));
  await h.api.loadEffortBoard();
  assert.match(h.view.innerHTML, /최근 8주에 처리일을 계산할 해결·종료 이슈가 없어요/);
  assert.doesNotMatch(h.view.innerHTML, /role="img" aria-label="주별 처리일 중앙값:/);
  const zero = await harness(() => ({ ok: true, payload: boardPayload({ weekly: [{ label: '11/2', medianDays: 0, closedCount: 1 }] }) }));
  await zero.api.loadEffortBoard();
  assert.match(zero.view.innerHTML, /role="img" aria-label="주별 처리일 중앙값: 11\/2 주 0일"/);
});

test('partial empty aging and effort results retain uncertainty', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload({
    meta: { ...boardPayload().meta, search_complete: false }, agingIssues: [], agingIssuesTotal: 0,
    effort: { total: 0, issueCount: 0, types: [] }, weekly: [], distribution: [],
  }) }));
  await h.api.loadEffortBoard();
  assert.match(h.view.innerHTML, /확인한 범위에는 장기 미종료이거나 곧 넘을 이슈가 없어요/);
  assert.match(h.view.innerHTML, /확인한 범위에는 공수 기록이 없어요/);
  assert.match(h.view.innerHTML, /전체 결과는 미확정/);
});

test('공수 요약은 확정 데이터에서만 만들고 고객사 처리일에 팀 중앙값 기준선을 긋는다', async () => {
  const h = await harness(() => ({ ok: true, payload: boardPayload() }));
  await h.api.loadEffortBoard();
  const html = h.view.innerHTML;
  assert.match(html, /class="effort-headline">처리일 중앙값은 3\.5일이에요\. 직전 기간보다 0\.9일 늘었어요\. 30일 넘게 걸린 이슈는 4건이에요\./);
  assert.match(html, /세로선은 팀 중앙값 3\.5일/);
  assert.match(html, /<i class="is-over" style="width:100%"><\/i><b class="effort-reference" style="left:56%"><\/b>/);
  assert.equal(h.api.effortHeadline(boardPayload(), false), '');
  const long = await harness(() => ({ ok: true, payload: boardPayload({ customers: [{ customer: 'a', name: 'A', closedCount: 4, medianDays: 202 }, { customer: 'b', name: 'B', closedCount: 4, medianDays: 7 }] }) }));
  await long.api.loadEffortBoard();
  assert.match(long.view.innerHTML, /<i class="is-over is-clipped" style="width:100%"><\/i><b class="effort-reference" style="left:33%"><\/b>/);
  assert.match(long.view.innerHTML, /<i class="is-over" style="width:67%"><\/i>/);
});
