const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const weeks = Array.from({ length: 8 }, (_, index) => ({ start: `2026-09-${String(index + 7).padStart(2, '0')}`, label: `w${index}`, partial: index === 7 }));
const row = (name, overrides = {}) => ({
  name, project: `${name}_project`, open: 0, overdue: 0, aging: 0, recent: 0, lastCreatedAt: '', weekly: Array(8).fill(0), spike: null, ...overrides,
});
const payload = (overrides = {}) => ({
  meta: { source: 'live', search_complete: true, fetchedAt: '2026-11-06T09:40:00+09:00', scopeLabel: '컨텐츠서비스팀' },
  weeks,
  customers: [
    row('교보증권', { open: 1, recent: 2, lastCreatedAt: '2026-10-01', weekly: [0, 0, 0, 0, 0, 1, 1, 0] }),
    row('미래에셋증권', { open: 7, overdue: 1, aging: 2, recent: 9, lastCreatedAt: '2026-11-05', weekly: [1, 1, 1, 1, 1, 0, 4, 0], spike: { count: 4, baselineAverage: 1, ratio: 4 } }),
    row('삼성증권', { project: '' }),
    row('KB<증권>', { open: 3, overdue: 2, recent: 1, lastCreatedAt: '2026-10-20' }),
  ],
  summary: { customers: 4, withOpen: 3, spiking: 1, withOverdue: 2 },
  ...overrides,
});

async function harness(fetcher) {
  const calls = [];
  const element = (dataset = {}) => ({
    hidden: true, innerHTML: '', dataset, attributes: {}, handlers: {}, tabIndex: 0,
    setAttribute(key, value) { this.attributes[key] = String(value); },
    getAttribute(key) { return this.attributes[key]; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return null; },
    focus() {},
  });
  const view = element();
  const subtitle = element();
  const tools = element();
  const panel = element();
  const tabs = element();
  const tabButtons = ['overview', 'customers'].map((tab) => element({ dashboardTab: tab }));
  const charts = { mounted: 0, released: 0 };
  const context = vm.createContext({
    Event: class { constructor(type) { this.type = type; } },
    CSS: { escape: (value) => String(value) },
    document: {
      activeElement: null,
      querySelector: (selector) => ({ '#customerOverviewView': view, '#dashboardPanel': panel, '#dashboardTabs': tabs,
        '[data-tab-subtitle="customers"]': subtitle, '[data-tab-tools="customers"]': tools })[selector] || null,
      querySelectorAll: (selector) => (selector === '[data-dashboard-tab]' ? tabButtons : []),
    },
  });
  const dependencies = {
    './runtime.js': { els: { input: { value: '', dispatchEvent() {}, focus() {} } }, WORKSPACE_ENDPOINTS: { customerOverview: '/customers' },
      apiFetch: async (url) => { calls.push(url); return fetcher(url, calls.length); } },
    './api.js': { responseJson: async (response) => response.payload },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './ui_format.js': { escapeHtml: (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]), customerMark: (name) => `<span class="customer-mark" data-mark="${name}"></span>` },
    './bklit_charts.js': {
      chartHost: (spec, fallback) => `<div data-chart='${JSON.stringify(spec)}'>${fallback}</div>`,
      mountCharts: async () => { charts.mounted += 1; },
      releaseCharts: () => { charts.released += 1; },
    },
    './data_badge.js': { dataBadgeState: () => 'fresh', formatBadgeTime: () => '', mantisBadgeDetails: () => ({ source: {}, note: '' }), renderDataBadge() {} },
    './dashboard.js': { showChatView() {} },
  };
  const tabsModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard_tabs.js'), 'utf8'), { context });
  await tabsModule.link(() => {});
  await tabsModule.evaluate();
  const focusModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/view_focus.js'), 'utf8'), { context });
  await focusModule.link(() => {});
  await focusModule.evaluate();
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/customer_overview.js'), 'utf8'), { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./view_focus.js')) return focusModule;
    if (specifier.startsWith('./dashboard_tabs.js')) return tabsModule;
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initCustomerOverview();
  tabsModule.namespace.initDashboardTabs();
  const target = (selector, dataset = {}) => ({ closest: (query) => (query === selector ? { dataset } : null), matches: () => false });
  return { api: module.namespace, tabsApi: tabsModule.namespace, view, subtitle, tools, panel, calls, target, charts };
}

const cardOrder = (html) => [...new Set([...html.matchAll(/class="customer-table-name"[^>]*>[\s\S]*?<strong>([^<]+)<\/strong>/g)].map(match=>match[1]))];

test('customers tab loads once and renders a comparison table and weekly heatmap', async () => {
  const h = await harness(() => ({ ok: true, payload: payload() }));
  h.tabsApi.selectDashboardTab('customers');
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual(h.calls, ['/customers']);
  assert.equal(h.panel.attributes['data-subview'], 'customers');
  const html = h.view.innerHTML;
  // 기본 정렬은 미종료 많은 순. 이름은 이스케이프한다.
  assert.deepEqual(cardOrder(html), ['미래에셋증권', 'KB&lt;증권&gt;', '교보증권', '삼성증권']);
  assert.match(html, /data-mark="미래에셋증권"/);
  // 기준 설명·저장본·새로고침은 머리글 경로 줄 칸에, 거르기·검색·정렬은 본문에 둔다.
  assert.match(h.subtitle.textContent, /고객사 선택 목록 4곳/);
  assert.match(h.tools.innerHTML, /data-customer-refresh/);
  assert.match(html, /data-customer-sort/);
  assert.doesNotMatch(html, /data-customer-badge/);
  assert.match(html, /customer-card-chip is-spike">지난주 급증/);
  assert.match(html, /<td class="is-overdue">2<\/td>/);
  assert.match(html, /customer-comparison-table/);
  assert.match(html, /analytics-heatmap/);
  assert.match(html, /2026-09-13 주 · 신규 4건/);
  assert.match(html, /고객사별 주간 등록/);
  // 이슈가 없는 고객사는 열 상세가 없어 버튼이 아니다.
  assert.match(html, /data-customer-open="미래에셋증권_project"/);
  assert.doesNotMatch(html, /data-customer-open="삼성증권_project"/);
  assert.ok(h.charts.mounted >= 1);
});

test('filter chips, search and sort only re-render locally', async () => {
  const h = await harness(() => ({ ok: true, payload: payload() }));
  await h.api.loadCustomerOverview();
  h.view.handlers.click({ target: h.target('[data-customer-filter]', { customerFilter: 'overdue' }) });
  assert.deepEqual(cardOrder(h.view.innerHTML), ['미래에셋증권', 'KB&lt;증권&gt;']);
  h.view.handlers.click({ target: h.target('[data-customer-filter]', { customerFilter: 'spike' }) });
  assert.deepEqual(cardOrder(h.view.innerHTML), ['미래에셋증권']);
  h.view.handlers.click({ target: h.target('[data-customer-filter]', { customerFilter: 'all' }) });
  h.api.overviewState.sort = 'recent';
  assert.deepEqual(Array.from(h.api.visibleRows(), (row) => row.name), ['미래에셋증권', 'KB<증권>', '교보증권', '삼성증권']);
  h.api.overviewState.query = '교보';
  assert.deepEqual(Array.from(h.api.visibleRows(), (row) => row.name), ['교보증권']);
  assert.equal(h.calls.length, 1);
});

test('partial data marks counts as lower bounds and errors keep a retry button', async () => {
  const partial = await harness(() => ({ ok: true, payload: payload({ meta: { source: 'live', search_complete: false } }) }));
  await partial.api.loadCustomerOverview();
  assert.match(partial.view.innerHTML, /7\+<\/strong>/);
  assert.match(partial.view.innerHTML, /급증 여부는 판단하지 않았어요/);
  const failing = await harness(() => ({ ok: false, payload: { detail: '조회 실패' } }));
  await failing.api.loadCustomerOverview();
  assert.match(failing.view.innerHTML, /조회 실패/);
  assert.match(failing.view.innerHTML, /data-customer-refresh>다시 시도/);
});

test('last registration labels shorten recent dates', async () => {
  const h = await harness(() => ({ ok: true, payload: payload() }));
  const today = new Date(2026, 10, 6);
  assert.equal(h.api.lastCreatedLabel('2026-11-06', today), '오늘 등록');
  assert.equal(h.api.lastCreatedLabel('2026-11-05', today), '어제 등록');
  assert.equal(h.api.lastCreatedLabel('2026-10-27', today), '10일 전 등록');
  assert.equal(h.api.lastCreatedLabel('2026-10-01', today), '5주 전 등록');
  assert.equal(h.api.lastCreatedLabel('2026-06-01', today), '6월 1일 등록');
  assert.equal(h.api.lastCreatedLabel('', today), '등록 기록 없음');
});

test('comparison rows show overdue work alongside a spike and keep partial counts uncertain', async () => {
  const h = await harness(() => ({ok:true,payload:payload()}));
  await h.api.loadCustomerOverview();
  assert.match(h.view.innerHTML, /<td class="is-overdue">1<\/td>/);
  assert.match(h.view.innerHTML, /지난주 급증/);
  const partial = await harness(() => ({ok:true,payload:payload({meta:{search_complete:false}})}));
  await partial.api.loadCustomerOverview();
  assert.match(partial.view.innerHTML, /<td class="is-overdue">1\+<\/td>/);
  assert.match(partial.view.innerHTML, /0\+<\/strong>/);
  assert.doesNotMatch(partial.view.innerHTML, /customer-card-chip is-spike/);
});

test('weekly heatmap preserves zero weeks and past registrations for customers without open work', async () => {
  const customers = [row('신규 없는 고객사',{project:''}), row('처리 이력 고객사',{recent:3,weekly:[0,0,0,0,0,1,2,0]})];
  const h = await harness(() => ({ok:true,payload:payload({customers,summary:{customers:2}})}));
  await h.api.loadCustomerOverview();
  assert.match(h.view.innerHTML,/신규 2건/);
  assert.match(h.view.innerHTML,/신규 0건/);
  assert.match(h.view.innerHTML,/data-intensity="0"/);
  assert.match(h.view.innerHTML,/처리 이력 고객사/);
});

test('empty customer search can reset query and filters without fetching or changing sort', async () => {
  const h = await harness(() => ({ ok: true, payload: payload() }));
  await h.api.loadCustomerOverview();
  h.api.overviewState.query = '없는 고객사';
  h.api.overviewState.sort = 'name';
  h.view.handlers.click({ target: h.target('[data-customer-filter]', { customerFilter: 'overdue' }) });
  assert.match(h.view.innerHTML, /0곳 표시/);
  assert.match(h.view.innerHTML, /조건에 맞는 고객사가 없어요/);
  assert.match(h.view.innerHTML, /data-customer-reset/);
  h.view.handlers.click({ target: h.target('[data-customer-reset]') });
  assert.equal(h.api.overviewState.query, '');
  assert.equal(h.api.overviewState.filter, 'all');
  assert.equal(h.api.overviewState.sort, 'name');
  assert.equal(cardOrder(h.view.innerHTML).length, 4);
  assert.equal(h.calls.length, 1);
});

test('고객사 지도는 선택한 기준으로 진하기를 정하고 기준 전환은 다시 조회하지 않는다', async () => {
  const h = await harness(() => ({ ok: true, payload: payload() }));
  await h.api.loadCustomerOverview();
  let html = h.view.innerHTML;
  assert.match(html, /class="customer-overview-headline">4곳 중 3곳에 미종료가 있어요\. 가장 많은 곳은 미래에셋증권\(7건\)이에요\. 지난주 신규가 급증한 곳은 1곳이에요\./);
  assert.match(html, /--tile-strength:70%" data-customer-open="미래에셋증권_project"/);
  assert.match(html, /<div class="customer-tile" style="--tile-strength:0%"/);
  assert.match(html, /KB&lt;증권&gt;/);
  h.view.handlers.click({ target: h.target('[data-customer-metric]', { customerMetric: 'overdue' }) });
  html = h.view.innerHTML;
  assert.match(html, /data-customer-metric="overdue" aria-pressed="true"/);
  assert.match(html, /--tile-strength:70%" data-customer-open="KB&lt;증권&gt;_project"/);
  assert.equal(h.calls.length, 1);
  assert.equal(h.api.customerHeadline([row('A')], true), '1곳 모두 미종료 이슈가 없어요.');
  assert.equal(h.api.customerHeadline([row('A', { open: 1 })], false), '');
});
