const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness(role, stored) {
  const calls = [];
  const storage = new Map(stored ? [[`team-ai-assistant-main-view-v1:user-${role}`, stored]] : []);
  let focused;
  const element = (name) => ({
    hidden: true, tabIndex: -1, innerHTML: '', attributes: {}, handlers: {},
    classList: { toggle() {}, remove() {} },
    setAttribute(key, value) { this.attributes[key] = value; },
    addEventListener(type, fn) { this.handlers[type] = fn; },
    querySelector() { return { textContent: '' }; },
    focus() { focused = name; },
  });
  const els = Object.fromEntries([
    'viewChatBtn', 'viewPersonalBtn', 'viewDashboardBtn', 'chatPanel', 'personalPanel',
    'dashboardPanel', 'dashboardRefreshBtn', 'dashboardPeriod', 'dashboardBody',
    'dashboardDetail', 'dashboardDetailClose', 'workspaceContent', 'dashboardSource', 'dashboardGeneratedAt',
  ].map((name) => [name, element(name)]));
  els.viewChatBtn.hidden = els.viewPersonalBtn.hidden = false;
  const state = { currentUser: { uid: `user-${role}`, role }, workspaceTab: 'overview', myWorkspace: { summary: {} } };
  const context = vm.createContext({
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    document: { body: { classList: { remove() {} } }, addEventListener() {} },
  });
  const dependencies = {
    './runtime.js': { state, els, WORKSPACE_ENDPOINTS: { dashboard: '/dashboard' },
      apiFetch: async () => { calls.push('team'); return { ok: false }; } },
    './api.js': { responseJson: async () => ({ detail: '연결 오류' }) },
    './workspace.js': {
      loadMyWorkspace: async () => { calls.push('personal'); }, renderWorkspace() {},
      loadTeamWeeklyBrief: async () => {}, renderTeamWeeklyBrief: () => '', markTeamWeeklyBriefRead: async () => {},
    },
  };
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/dashboard.js'), 'utf8'), { context });
  await module.link((specifier) => {
    const values = dependencies[specifier];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initDashboard();
  return { api: module.namespace, els, state, storage, calls, focus: () => focused };
}

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

test('member cannot load team dashboard even through a saved view or direct call', async () => {
  const h = await harness('member', 'dashboard');
  h.api.showDashboardView();
  await h.api.loadDashboard();
  assert.equal(h.els.viewDashboardBtn.hidden, true);
  assert.equal(h.els.dashboardPanel.hidden, true);
  assert.equal(h.els.personalPanel.hidden, false);
  assert.deepEqual(h.calls, []);
});

test('keyboard navigation follows visible tab order and moves focus', async () => {
  for (const role of ['admin', 'member']) {
    const h = await harness(role);
    const key = (button, value) => h.els[button].handlers.keydown({ key: value, preventDefault() {} });
    key('viewChatBtn', 'ArrowRight');
    assert.equal(h.focus(), 'viewPersonalBtn');
    key('viewPersonalBtn', 'End');
    assert.equal(h.focus(), role === 'admin' ? 'viewDashboardBtn' : 'viewPersonalBtn');
    key(h.focus(), 'Home');
    assert.equal(h.focus(), 'viewChatBtn');
  }
});

test('personal view restores from account-scoped storage and requests missing data', async () => {
  const h = await harness('admin', 'personal');
  assert.equal(h.els.personalPanel.hidden, false);
  h.state.myWorkspace = null;
  h.api.showPersonalView();
  assert.deepEqual(h.calls, ['personal']);
  assert.equal(h.storage.get('team-ai-assistant-main-view-v1:user-admin'), 'personal');
});
