const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness(ok = true, delayed = false) {
  let release;
  const gate = delayed ? new Promise(resolve => { release = resolve; }) : Promise.resolve();
  function element() { return { hidden: false, textContent: '', innerHTML: '', listeners: {},
    classList: { toggle() {} }, addEventListener(name, fn) { this.listeners[name] = fn; }, focus() {},
  }; }
  const submit = { disabled: false }, error = element(), done = element();
  const form = element();
  form.elements = Object.fromEntries(Object.entries({ mode: 'reset', uid: 'existing',
    temporaryPassword: 'Reset-Fixture-123!', displayName: '팀원', mantisUsername: '', team: '', approved: true })
    .map(([name, value]) => [name, { value, checked: value === true, checkValidity: () => true }]));
  form.querySelector = selector => selector === '[type="submit"]' ? submit : selector === '.admin-form-error' ? error : done;
  const els = Object.fromEntries(['adminDrawer', 'adminContent', 'adminDrawerClose', 'adminDrawerBackdrop', 'adminDrawerTitle', 'adminDrawerSub'].map(name => [name, element()]));
  els.adminDrawerForm = form;
  const calls = [];
  const target = { uid: 'existing', email: 'existing@example.test', role: 'member', approved: true };
  const state = { adminUsers: [target], currentUser: { uid: 'admin' } };
  const context = vm.createContext({ document: { activeElement: null }, globalThis: {} });
  const exports = {
    './dialog_focus.js': { openDialogFocus() {}, closeDialogFocus() {} },
    './runtime.js': { state, els, apiFetch: async (url, options) => {
      calls.push({ url, options });
      await gate;
      return { ok, json: async () => ok ? { user: { ...target, passwordChangeRequired: true } } : { detail: '초기화 실패: 다시 초기화해 주세요.' } };
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
  return { form, els, state, calls, error, submit, release, close: module.namespace.closeAdminDrawer, openEdit(uid='existing') {
    els.adminContent.listeners.click({ target: { closest: selector => selector === '[data-admin-user-edit]' ? { dataset: {adminUserEdit:uid} } : null } });
  }, openReset() {
    form.listeners.click({ target: { closest: selector => selector === '[data-admin-password-reset]' ? { dataset: {adminPasswordReset:'existing'} } : null } });
  }, async send() { submit.disabled = false; await form.listeners.submit({ preventDefault() {} }); } };
}

test('기존 사용자 편집에서 초기화 화면을 열고 전용 API로 전송한다', async () => {
  const h = await harness(); h.openEdit();
  assert.match(h.form.innerHTML, /data-admin-password-reset/);
  h.openReset();
  assert.match(h.form.innerHTML, /기존 로그인 세션/);
  await h.send();
  assert.equal(h.calls[0].url, '/api/auth/users/existing/reset-password');
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { temporaryPassword: 'Reset-Fixture-123!' });
  assert.equal(h.form.elements.temporaryPassword.value, '');
  assert.equal(h.state.adminUsers[0].passwordChangeRequired, true);
  assert.match(h.form.innerHTML, /초기화 완료/);
  assert.doesNotMatch(h.form.innerHTML, /Reset-Fixture-123!/);
});

test('초기화 오류는 성공으로 표시하지 않고 다시 시도할 수 있다', async () => {
  const h = await harness(false); h.openReset(); await h.send();
  assert.match(h.error.textContent, /다시 초기화/);
  assert.equal(h.submit.disabled, false);
  assert.equal(h.state.adminUsers[0].passwordChangeRequired, undefined);
  assert.doesNotMatch(h.form.innerHTML, /초기화 완료/);
});

test('본인 계정과 승인되지 않은 계정에는 초기화 버튼이 없다', async () => {
  const h = await harness();
  h.state.currentUser.uid = 'existing'; h.openEdit();
  assert.doesNotMatch(h.form.innerHTML, /data-admin-password-reset/);
  h.state.currentUser.uid = 'admin'; h.state.adminUsers[0].approved = false; h.openEdit();
  assert.doesNotMatch(h.form.innerHTML, /data-admin-password-reset/);
});


test('요청 도중 닫은 폼에 성공 응답이 와도 사용자 상태를 갱신한다', async () => {
  const h = await harness(true, true); h.openReset();
  const pending = h.send();
  h.close(); h.form.elements = {};
  h.release(); await pending;
  assert.equal(h.els.adminDrawer.hidden, true);
  assert.equal(h.state.adminUsers[0].passwordChangeRequired, true);
  assert.equal(h.form.innerHTML, '');
});

test('이전 초기화의 지연 응답이 새로 연 사용자 편집 화면을 덮어쓰지 않는다', async () => {
  const h = await harness(true, true); h.openReset();
  const pending = h.send(); h.close();
  h.state.adminUsers.push({uid:'another',email:'another@example.test',approved:true,role:'member'});
  h.openEdit('another');
  const nextContents = h.form.innerHTML;
  h.release(); await pending;
  assert.equal(h.form.innerHTML, nextContents);
  assert.equal(h.state.adminUsers[0].passwordChangeRequired, true);
});
