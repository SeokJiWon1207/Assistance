const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const row = (screen, weeks, overrides = {}) => ({
  screen, weeks, total: weeks.reduce((sum, value) => sum + value, 0), openCount: 2,
  customers: [{ customer: '108SK(SK증권)', name: 'SK증권', count: 3 }],
  recentIssues: [{ id: 41, title: '주문 <지연>', customerName: 'SK증권', status: 'confirmed', createdAt: '2026-10-28' }],
  playbooks: [],
  ...overrides,
});

const hotspotPayload = (overrides = {}) => ({
  meta: { source: 'live', search_complete: true, fetchedAt: '2026-11-03T09:40:00+09:00', scopeLabel: '컨텐츠서비스팀', media: '', customer: '', customerName: '' },
  weeks: Array.from({ length: 8 }, (_, index) => ({ start: `2026-09-${String(index + 7).padStart(2, '0')}`, label: `w${index}`, partial: index === 7 })),
  rows: [row('7012', [1, 1, 2, 1, 1, 1, 7, 0]), row('0150', [0, 0, 0, 0, 0, 0, 2, 1], { playbooks: [{ id: 'pb', title: '현재가 멈춤', matchedIssueIds: ['9'] }] })],
  spikes: [{ screen: '7012', count: 7, baselineAverage: 1.3, ratio: 5.4 }, { screen: '0150', count: 2, baselineAverage: 0, ratio: null }],
  summary: { issueCount: 20, screenCount: 2, unknownScreenCount: 3 },
  options: { media: ['HTS', 'MTS'], customers: [{ customer: '108SK(SK증권)', name: 'SK증권' }] },
  ...overrides,
});

async function harness(fetcher) {
  const calls = [];
  const focused = [];
  const element = (dataset = {}) => ({
    hidden: true, innerHTML: '', dataset, attributes: {}, handlers: {}, tabIndex: 0,
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key]; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector(selector) { return { focus: () => focused.push(selector) }; },
    focus() {},
  });
  const view = element();
  view.contains = () => true;
  const panel = element();
  const tabs = element();
  const tabButtons = ['overview', 'effort', 'screens'].map((tab) => element({ dashboardTab: tab }));
  const input = { value: '', dispatchEvent() {}, focus() {} };
  let chatShown = 0;
  const context = vm.createContext({
    URLSearchParams, Event: class { constructor(type) { this.type = type; } },
    CSS: { escape: (value) => String(value) },
    document: {
      activeElement: null,
      querySelector: (selector) => ({ '#screenHotspotView': view, '#dashboardPanel': panel, '#dashboardTabs': tabs })[selector] || null,
      querySelectorAll: (selector) => (selector === '[data-dashboard-tab]' ? tabButtons : []),
    },
  });
  const dependencies = {
    './runtime.js': { els: { input }, WORKSPACE_ENDPOINTS: { screenHotspot: '/screens' },
      apiFetch: async (url) => { calls.push(url); return fetcher(url, calls.length); } },
    './api.js': { responseJson: async (response) => response.payload },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './data_badge.js': { dataBadgeState: () => 'fresh', formatBadgeTime: () => '', mantisBadgeDetails: () => ({ source: {}, note: '' }),
      renderDataBadge() {} },
    './workspace.js': { workspaceStatusLabel: (value) => ({ confirmed: '확인된 이슈' }[value] || value) },
    './dashboard.js': { showChatView() { chatShown += 1; } },
  };
  const tabsModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard_tabs.js'), 'utf8'), { context });
  await tabsModule.link(() => {});
  await tabsModule.evaluate();
  const focusModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/view_focus.js'), 'utf8'), { context });
  await focusModule.link(() => {});
  await focusModule.evaluate();
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/screen_hotspot.js'), 'utf8'), { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./view_focus.js')) return focusModule;
    if (specifier.startsWith('./dashboard_tabs.js')) return tabsModule;
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initScreenHotspot();
  tabsModule.namespace.initDashboardTabs();
  const target = (selector, dataset = {}) => ({ closest: (query) => (query === selector ? { dataset } : null) });
  const setActive = (attributes) => { context.document.activeElement = { attributes, closest: () => null }; };
  return { setActive, api: module.namespace, tabsApi: tabsModule.namespace, view, panel, tabs, tabButtons, calls, target, input, focused, chatShown: () => chatShown };
}

test('screens tab loads the heatmap, banner and first screen detail', async () => {
  const h = await harness(() => ({ ok: true, payload: hotspotPayload() }));
  h.tabsApi.selectDashboardTab('screens');
  await h.api.loadScreenHotspot();
  assert.deepEqual(h.calls, ['/screens', '/screens']);
  assert.equal(h.panel.attributes['data-subview'], 'screens');
  assert.equal(h.view.hidden, false);
  const html = h.view.innerHTML;
  assert.match(html, /앞선 4주 평균 1\.3건에서 지난주 7건으로/);
  assert.match(html, /앞선 4주에는 없다가 지난주 2건/);
  assert.match(html, /screen-heat is-l5">7</);
  assert.match(html, /screen-heat is-l0">0</);
  assert.match(html, /w7\*/);
  assert.match(html, /화면번호 없음 3건 제외/);
  assert.match(html, /<h3 id="screenDetailTitle">7012<\/h3>/);
  assert.match(html, /주문 &lt;지연&gt;/);
  assert.match(html, /공개 플레이북이 아직 없습니다/);
});

test('selecting a screen switches the detail without refetching', async () => {
  const h = await harness(() => ({ ok: true, payload: hotspotPayload() }));
  await h.api.loadScreenHotspot();
  h.view.handlers.click({ target: h.target('[data-screen-select]', { screenSelect: '0150' }) });
  assert.equal(h.calls.length, 1);
  assert.match(h.view.innerHTML, /<h3 id="screenDetailTitle">0150<\/h3>/);
  assert.match(h.view.innerHTML, /현재가 멈춤/);
  assert.match(h.view.innerHTML, /data-screen-select="0150" aria-pressed="true"/);
});

test('filters refetch with their values and keep focus on the changed select', async () => {
  const h = await harness(() => ({ ok: true, payload: hotspotPayload() }));
  await h.api.loadScreenHotspot();
  h.setActive([{ name: 'data-screen-filter', value: 'customer' }]);
  await h.view.handlers.change({ target: { closest: () => ({ dataset: { screenFilter: 'media' }, value: 'MTS' }) } });
  await h.view.handlers.change({ target: { closest: () => ({ dataset: { screenFilter: 'customer' }, value: '108SK(SK증권)' }) } });
  assert.deepEqual(h.calls.slice(1), ['/screens?media=MTS', '/screens?media=MTS&customer=108SK%28SK%EC%A6%9D%EA%B6%8C%29']);
  await Promise.resolve();
  assert.ok(h.focused.includes('[data-screen-filter="customer"]'));
});

test('ask buttons send the selected screen and filter scope to chat', async () => {
  const h = await harness(() => ({ ok: true, payload: hotspotPayload({ meta: { ...hotspotPayload().meta, media: 'HTS', customerName: 'SK증권' } }) }));
  await h.api.loadScreenHotspot();
  h.view.handlers.click({ target: h.target('[data-screen-ask]', { screenAsk: 'list' }) });
  assert.equal(h.input.value, 'SK증권 HTS 화면번호 7012 최근 8주 이슈 목록 보여줘');
  assert.equal(h.chatShown(), 1);
});

test('partial data skips spike judgement and errors keep retry', async () => {
  const partial = await harness(() => ({ ok: true, payload: hotspotPayload({ meta: { ...hotspotPayload().meta, search_complete: false }, spikes: [] }) }));
  await partial.api.loadScreenHotspot();
  assert.match(partial.view.innerHTML, /급증 여부는 판단하지 않았습니다/);
  const failed = await harness(() => ({ ok: false, payload: { detail: '매체는 HTS 또는 MTS만 선택할 수 있습니다.' } }));
  await failed.api.loadScreenHotspot();
  assert.match(failed.view.innerHTML, /매체는 HTS/);
  assert.match(failed.view.innerHTML, /data-screen-refresh/);
});

test('tab keys cycle only registered tabs in button order', async () => {
  const h = await harness(() => ({ ok: true, payload: hotspotPayload() }));
  // 이 하네스에는 effort 탭이 등록되지 않았으므로 overview ↔ screens만 순환한다.
  h.tabs.handlers.keydown({ key: 'End', preventDefault() {} });
  assert.equal(h.tabsApi.currentDashboardTab(), 'screens');
  assert.equal(h.panel.attributes['data-subview'], 'screens');
  h.tabs.handlers.keydown({ key: 'ArrowRight', preventDefault() {} });
  assert.equal(h.tabsApi.currentDashboardTab(), 'overview');
  assert.equal(h.panel.attributes['data-subview'], undefined);
  h.tabs.handlers.keydown({ key: 'ArrowLeft', preventDefault() {} });
  assert.equal(h.tabsApi.currentDashboardTab(), 'screens');
});
