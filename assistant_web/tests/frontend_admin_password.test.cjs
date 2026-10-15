const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('관리자 계정 생성은 입력한 임시 비밀번호를 전송하고 성공 뒤 입력과 결과에서 지운다', async () => {
  function element() { return { hidden: false, textContent: '', innerHTML: '', listeners: {},
    addEventListener(name, fn) { this.listeners[name] = fn; }, focus() {},
  }; }
  const submit = { disabled: false }, error = element(), done = element();
  const form = element();
  form.elements = Object.fromEntries(Object.entries({ mode: 'invite', email: 'member@example.test',
    temporaryPassword: 'Fixture-Temporary-123!', displayName: '팀원', mantisUsername: '', team: '컨텐츠' })
    .map(([name, value]) => [name, { value, checkValidity: () => true }]));
  form.querySelector = selector => selector === '[type="submit"]' ? submit : selector === '.admin-form-error' ? error : done;
  const els = Object.fromEntries(['adminDrawer', 'adminContent', 'adminDrawerClose', 'adminDrawerBackdrop', 'adminDrawerTitle', 'adminDrawerSub'].map(name => [name, element()]));
  els.adminDrawerForm = form;
  const calls = [];
  const state = { adminUsers: [], currentUser: { uid: 'admin' } };
  const context = vm.createContext({ document: { activeElement: null }, globalThis: {} });
  const exports = {
    './dialog_focus.js': { openDialogFocus() {}, closeDialogFocus() {} },
    './runtime.js': { state, els, apiFetch: async (url, options) => {
      calls.push({ url, options });
      return { ok: true, json: async () => ({ user: { uid: 'new', email: 'member@example.test', passwordChangeRequired: true } }) };
    } },
    './chat.js': { escapeHtml: text => text }, './workspace.js': { showToast() {} },
  };
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/admin_users.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link(async specifier => {
    const values = exports[specifier.split('?')[0]];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [name, value] of Object.entries(values)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initAdminUsers();
  els.adminContent.listeners.click({ target: { closest: selector => selector === '[data-admin-invite]' ? {} : null } });
  assert.match(form.innerHTML, /name="temporaryPassword" type="password"/);
  await form.listeners.submit({ preventDefault() {} });
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(calls[0].url, '/api/auth/users/invite');
  assert.equal(payload.temporaryPassword, 'Fixture-Temporary-123!');
  assert.equal(form.elements.temporaryPassword.value, '');
  assert.doesNotMatch(form.innerHTML, /Fixture-Temporary/);
  assert.match(form.innerHTML, /첫 로그인 시 새 비밀번호로 변경/);
  assert.equal(state.adminUsers[0].passwordChangeRequired, true);
});
