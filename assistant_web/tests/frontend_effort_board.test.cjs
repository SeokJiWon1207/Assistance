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
  const panel = element();
  const tabs = element();
  const tabButtons = [element({ dashboardTab: 'overview' }), element({ dashboardTab: 'effort' })];
  const context = vm.createContext({
    URLSearchParams, Event: class { constructor(type) { this.type = type; } },
    CSS: { escape: (value) => String(value) },
    document: {
      querySelector: (selector) => ({ '#effortBoardView': view, '#dashboardPanel': panel, '#dashboardTabs': tabs })[selector] || null,
      querySelectorAll: (selector) => (selector === '[data-dashboard-tab]' ? tabButtons : []),
    },
  });
  const dependencies = {
    './runtime.js': { els: {}, WORKSPACE_ENDPOINTS: { effortBoard: '/effort' },
      apiFetch: async (url) => { calls.push(url); return fetcher(url, calls.length); } },
    './api.js': { responseJson: async (response) => response.payload },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
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
  return { api, view, panel, tabs, tabButtons, calls, target };
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
  assert.match(html, /7일 안에 4건이 더 넘어요/);
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
  const second = h.view.handlers.click({ target: h.target('[data-effort-period]', { effortPeriod: '90' }) });
  assert.deepEqual(pending.map((item) => item.url), ['/effort?days=30', '/effort?days=90']);
  pending[1].resolve({ ok: true, payload: boardPayload({ meta: { ...boardPayload().meta, periodDays: 90 }, summary: { ...boardPayload().summary, closedCount: 900 } }) });
  await second;
  pending[0].resolve({ ok: true, payload: boardPayload() });
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(h.view.innerHTML, />900</);
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
