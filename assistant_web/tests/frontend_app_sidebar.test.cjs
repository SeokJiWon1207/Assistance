const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(dataset = {}, attributes = {}) {
  return {
    dataset, attributes: { ...attributes }, textContent: dataset.label || '', clicks: 0,
    getAttribute(key) { return this.attributes[key] ?? null; },
    setAttribute(key, value) { this.attributes[key] = String(value); },
    removeAttribute(key) { delete this.attributes[key]; },
    click() { this.clicks += 1; },
  };
}

async function harness({ view = 'personal', conversations = [], recent = [] } = {}) {
  const workspaceTabs = ['overview', 'brief'].map((tab, index) => element({ workspaceTab: tab }, { 'aria-selected': String(index === 1) }));
  const dashboardTabs = ['overview', 'customers'].map((tab, index) => element({ dashboardTab: tab }, { 'aria-selected': String(index === 0) }));
  const railItems = [
    element({ railTab: 'workspace:overview', label: '업무' }), element({ railTab: 'workspace:brief', label: '주간 보고' }),
    element({ railTab: 'dashboard:overview', label: '팀 현황' }), element({ railTab: 'dashboard:customers', label: '고객사' }),
  ];
  const crumbs = { workspace: element(), dashboard: element() };
  const shell = element({ view });
  const list = { innerHTML: '' };
  const calls = [];
  const query = (selector) => {
    const match = selector.match(/^\[data-(workspace|dashboard)-tab="([^"]+)"\]$/);
    if (match) return (match[1] === 'workspace' ? workspaceTabs : dashboardTabs).find((tab) => (tab.dataset.workspaceTab || tab.dataset.dashboardTab) === match[2]) || null;
    const rail = selector.match(/^\[data-rail-tab="([^"]+)"\]$/);
    if (rail) return railItems.find((item) => item.dataset.railTab === rail[1]) || null;
    const crumb = selector.match(/^\[data-crumb-current="([^"]+)"\]$/);
    if (crumb) return crumbs[crumb[1]];
    return { '.app-shell': shell, '#railShortcutList': list }[selector] || null;
  };
  const queryAll = (selector) => {
    if (selector === '[data-workspace-tab]') return workspaceTabs;
    if (selector === '[data-dashboard-tab]') return dashboardTabs;
    const prefix = selector.match(/^\[data-rail-tab\^="([^"]+)"\]$/);
    if (prefix) return railItems.filter((item) => item.dataset.railTab.startsWith(prefix[1]));
    return [];
  };
  const storage = new Map(recent.length ? [['team-ai-assistant-recent-issues-v1:u1', JSON.stringify(recent)]] : []);
  const context = vm.createContext({
    MutationObserver: class { observe() {} },
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    document: { querySelector: query, querySelectorAll: queryAll, addEventListener() {}, body: { classList: { toggle() {}, contains: () => false } } },
  });
  context.globalThis = context;
  const recentModule = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/recent_items.js'), 'utf8'), { context });
  await recentModule.link(() => {});
  await recentModule.evaluate();
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/app_sidebar.js'), 'utf8'), { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./recent_items.js')) return recentModule;
    return new vm.SyntheticModule(['state'], function () { this.setExport('state', { currentUser: { uid: 'u1' }, conversations }); }, { context });
  });
  await module.evaluate();
  module.namespace.initAppSidebar({
    showPersonalView: () => { calls.push('personal'); shell.dataset.view = 'personal'; },
    showDashboardView: () => { calls.push('dashboard'); shell.dataset.view = 'dashboard'; },
  });
  return { api: module.namespace, railItems, crumbs, workspaceTabs, dashboardTabs, shell, list, calls };
}

test('sidebar sub items mirror the selected tab and the breadcrumb shows its name', async () => {
  const h = await harness();
  assert.equal(h.railItems[1].getAttribute('aria-current'), 'page');
  assert.equal(h.railItems[0].getAttribute('aria-current'), null);
  assert.equal(h.railItems[2].getAttribute('aria-current'), 'page');
  assert.equal(h.crumbs.workspace.textContent, '주간 보고');
  assert.equal(h.crumbs.dashboard.textContent, '팀 현황');
});

test('a sub item for another view switches the view first and then presses the real tab', async () => {
  const h = await harness({ view: 'personal' });
  h.api.openRailTab('dashboard:customers');
  assert.deepEqual(h.calls, ['dashboard']);
  assert.equal(h.dashboardTabs[1].clicks, 1);
  h.api.openRailTab('dashboard:overview');
  assert.deepEqual(h.calls, ['dashboard']);
  assert.equal(h.dashboardTabs[0].clicks, 1);
});

test('shortcuts list favorite conversations and recently viewed issues with escaped titles', async () => {
  const h = await harness({
    conversations: [{ id: 'a', title: '<b>주간</b> 보고', favorite: true }, { id: 'b', title: '일반', favorite: false }],
    recent: [{ id: '31730', title: '해외주식 <검색>' }],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(h.api.shortcutRows())), [{ attr: 'data-rail-conversation="a"', label: '<b>주간</b> 보고' }]);
  h.api.renderShortcuts();
  assert.match(h.list.innerHTML, /&lt;b&gt;주간&lt;\/b&gt; 보고/);
  assert.doesNotMatch(h.list.innerHTML, /일반/);
});
