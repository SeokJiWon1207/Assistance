const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const detailPayload = (overrides = {}) => ({
  meta: { source: 'live', search_complete: true, fetchedAt: '2026-11-03T09:40:00+09:00', periodDays: 30,
    periodFrom: '2026-10-05', periodTo: '2026-11-03', scopeLabel: '컨텐츠서비스팀' },
  customer: { id: '108SK(SK증권)', name: 'SK증권' },
  health: { level: 'high', label: '위험', trend: 'worsening', reasons: ['일정 지연 2건'] },
  summary: { open: 3, overdue: 2, aging: 1, newIssues: 4, previousNewIssues: 1, carriedOpen: 2,
    averageHandlingDays: 6.2, teamAverageHandlingDays: 4.8, totalIssues: 20 },
  monthly: [{ month: '2026-11', label: '11월', newCount: 4, closedCount: 1 }],
  screens: [{ name: '7012', count: 3 }],
  media: [{ name: 'HTS', count: 3 }, { name: '미지정', count: 1 }],
  openIssues: [
    { id: 11, title: '오래된 <이슈>', status: 'confirmed', handler: '석지원', ageDays: 40, dueDate: '미상' },
    { id: 12, title: '마감 이슈', status: 'assigned', handler: '우동헌', ageDays: 5, dueDate: '2026-11-01', overdue: true },
  ],
  openIssuesTotal: 2,
  playbooks: [{ id: 'pb', title: '알림 지연 대응', symptom: '알림이 늦음', matchedIssueIds: ['11'], updatedAt: '2026-10-30' }],
  ...overrides,
});

async function harness(fetcher) {
  const calls = [];
  const element = () => ({
    hidden: true, innerHTML: '', dataset: {}, attributes: {}, handlers: {}, scrollTop: 5,
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return null; },
  });
  const view = element();
  const panel = element();
  const context = vm.createContext({
    URLSearchParams,
    document: { querySelector: (selector) => ({ '#customerDetailView': view, '#dashboardPanel': panel })[selector] || null },
  });
  const dependencies = {
    './runtime.js': { WORKSPACE_ENDPOINTS: { customer: '/customer' },
      apiFetch: async (url) => { calls.push(url); return fetcher(url, calls.length); } },
    './api.js': { responseJson: async (response) => response.payload },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './ui_format.js': { formatPeriodRange: (from, to) => `${from}–${to}`, customerLabelText: (value) => value, customerMark: (name) => `<span class="customer-mark" data-mark="${name}"></span>` },
    './data_badge.js': { dataBadgeState: () => 'fresh', formatBadgeTime: () => '', mantisBadgeDetails: () => ({ source: {}, note: '' }),
      renderDataBadge() {} },
    './workspace.js': { workspaceStatusLabel: (value) => ({ confirmed: '확인된 이슈', assigned: '할당된 이슈' }[value] || value) },
  };
  const focusModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/view_focus.js'), 'utf8'), { context });
  await focusModule.link(() => {});
  await focusModule.evaluate();
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/customer_detail.js'), 'utf8'), { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./view_focus.js')) return focusModule;
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  const click = (selector, dataset = {}) => view.handlers.click({ target: { closest: (query) => (query === selector ? { dataset } : null) } });
  return { api: module.namespace, view, panel, calls, click };
}

test('opening a customer loads the dashboard period and renders escaped detail sections', async () => {
  const h = await harness(() => ({ ok: true, payload: detailPayload() }));
  await h.api.openCustomerDetail('108SK(SK증권)', { period: 30 });
  assert.deepEqual(h.calls, ['/customer?name=108SK%28SK%EC%A6%9D%EA%B6%8C%29&days=30']);
  assert.equal(h.view.hidden, false);
  assert.equal(h.panel.attributes['data-subview'], 'customer');
  assert.equal(h.api.isCustomerDetailOpen(), true);
  const html = h.view.innerHTML;
  assert.match(html, /<h2[^>]*>SK증권<\/h2>/);
  assert.match(html, /위험 · 악화 중/);
  assert.match(html, /오래된 &lt;이슈&gt;/);
  assert.match(html, /11월 1일 지남/);
  assert.match(html, /팀 평균 4\.8일/);
  assert.match(html, /직전 30일 1건/);
  assert.match(html, /data-customer-source="11"/);
  assert.doesNotMatch(html, /건 이상/);
});

test('due-date sorting keeps unknown dates last and partial data shows lower bounds', async () => {
  const h = await harness(() => ({ ok: true, payload: detailPayload({ meta: { ...detailPayload().meta, search_complete: false } }) }));
  await h.api.openCustomerDetail('108SK(SK증권)');
  assert.match(h.view.innerHTML, /건 이상/);
  h.click('[data-customer-sort]', { customerSort: 'due' });
  const html = h.view.innerHTML;
  assert.ok(html.indexOf('#12') < html.indexOf('#11'));
});

test('period changes refetch and a stale response never replaces the newer one', async () => {
  const pending = [];
  const h = await harness((url) => new Promise((resolve) => pending.push({ url, resolve })));
  const first = h.api.openCustomerDetail('A', { period: 30 });
  const second = h.click('[data-customer-period]', { customerPeriod: '90' });
  assert.deepEqual(pending.map((item) => item.url), ['/customer?name=A&days=30', '/customer?name=A&days=90']);
  pending[1].resolve({ ok: true, payload: detailPayload({ customer: { id: 'A', name: '90일 결과' } }) });
  await second;
  pending[0].resolve({ ok: true, payload: detailPayload({ customer: { id: 'A', name: '30일 결과' } }) });
  await first;
  assert.match(h.view.innerHTML, /90일 결과/);
  assert.doesNotMatch(h.view.innerHTML, /30일 결과/);
});

test('errors are shown with retry, and back closes the subview and restores focus', async () => {
  const h = await harness(() => ({ ok: false, payload: { detail: '팀 담당 이슈 중 해당 고객사를 찾지 못했습니다.' } }));
  let closed = 0;
  await h.api.openCustomerDetail('없음', { onClose: () => closed++ });
  assert.match(h.view.innerHTML, /찾지 못했습니다/);
  assert.match(h.view.innerHTML, /data-customer-refresh/);
  h.click('[data-customer-back]');
  assert.equal(h.view.hidden, true);
  assert.equal(h.panel.attributes['data-subview'], undefined);
  assert.equal(closed, 1);
});

test('ask buttons hand a customer question to the chat', async () => {
  const h = await harness(() => ({ ok: true, payload: detailPayload() }));
  const questions = [];
  await h.api.openCustomerDetail('108SK(SK증권)', { onAsk: (question) => questions.push(question) });
  h.click('[data-customer-ask]', { customerAsk: 'analysis' });
  h.click('[data-customer-ask]', { customerAsk: 'report' });
  assert.deepEqual(questions, ['SK증권 최근 30일 이슈 현황과 주요 원인을 분석해줘', 'SK증권 11월 월간 고객사 리포트 작성해줘']);
});

test('customer issue preview expands without refetching and retains due-date sorting', async () => {
  const openIssues = Array.from({ length: 12 }, (_, index) => ({ ...detailPayload().openIssues[0],
    id: index + 1, dueDate: `2026-11-${String(20 - index).padStart(2, '0')}` }));
  const h = await harness(() => ({ ok: true, payload: detailPayload({ openIssues, openIssuesTotal: 14, playbooks: [] }) }));
  await h.api.openCustomerDetail('SK증권');
  assert.equal((h.view.innerHTML.match(/data-customer-source=/g) || []).length, 5);
  h.click('[data-customer-sort]', { customerSort: 'due' });
  assert.ok(h.view.innerHTML.indexOf('#12') < h.view.innerHTML.indexOf('#11'));
  h.click('[data-customer-expand]');
  assert.equal((h.view.innerHTML.match(/data-customer-source=/g) || []).length, 12);
  assert.match(h.view.innerHTML, /data-customer-sort="due" aria-pressed="true"/);
  h.click('[data-customer-expand]');
  assert.equal((h.view.innerHTML.match(/data-customer-source=/g) || []).length, 5);
  assert.equal(h.calls.length, 1);
});

test('partial empty customer detail does not claim no issues or a confirmed health level', async () => {
  const h = await harness(() => ({ ok: true, payload: detailPayload({
    meta: { ...detailPayload().meta, search_complete: false }, openIssues: [], openIssuesTotal: 0,
    monthly: [], screens: [], media: [],
  }) }));
  await h.api.openCustomerDetail('SK증권');
  assert.match(h.view.innerHTML, /확인한 범위에는 미종료 이슈가 없어요/);
  assert.match(h.view.innerHTML, /전체 결과는 미확정/);
  assert.match(h.view.innerHTML, /건강도를 확정하지 않았어요/);
  assert.doesNotMatch(h.view.innerHTML, /위험 · 악화 중/);
});

test('customer monthly history stays visible after open issues reach zero but empty history has no chart', async () => {
  const h = await harness(() => ({ ok: true, payload: detailPayload({ openIssues: [], openIssuesTotal: 0 }) }));
  await h.api.openCustomerDetail('SK증권');
  assert.match(h.view.innerHTML, /customer-monthly-chart/);
  const empty = await harness(() => ({ ok: true, payload: detailPayload({
    openIssues: [], openIssuesTotal: 0, monthly: [{ label: '11월', newCount: 0, closedCount: 0 }],
  }) }));
  await empty.api.openCustomerDetail('SK증권');
  assert.doesNotMatch(empty.view.innerHTML, /customer-monthly-chart/);
  assert.match(empty.view.innerHTML, /최근 6개월에 신규 등록·종료 항목 변경이 없어요/);
});
