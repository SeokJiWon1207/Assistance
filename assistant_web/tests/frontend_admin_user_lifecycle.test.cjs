const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function harness({ ok = true, delayed = false } = {}) {
  let release;
  const gate = delayed ? new Promise(resolve => { release = resolve; }) : Promise.resolve();
  const element = () => ({ hidden: false, textContent: '', innerHTML: '', listeners: {},
    classList: { toggle() {} }, addEventListener(name, fn) { this.listeners[name] = fn; }, focus() {} });
  const submit = { disabled: false }, error = element(), field = element(), form = element();
  form.elements = Object.fromEntries(Object.entries({ mode: 'delete', uid: 'target',
    confirmationEmail: 'target@example.test', displayName: '팀원', mantisUsername: '', team: '', approved: true })
    .map(([name, value]) => [name, { value, checked: value === true, checkValidity: () => true }]));
  let contents = '';
  Object.defineProperty(form, 'innerHTML', { get: () => contents, set(value) {
    contents = value;
    const mode = value.match(/name="mode" value="([^"]+)"/);
    if (mode) form.elements.mode.value = mode[1];
    submit.disabled = /type="submit"[^>]* disabled/.test(value);
  } });
  form.querySelector = selector => selector === '[type="submit"]' ? submit : selector === '.admin-form-error' ? error : field;
  const els = Object.fromEntries(['adminDrawer', 'adminContent', 'adminDrawerClose', 'adminDrawerBackdrop', 'adminDrawerTitle', 'adminDrawerSub'].map(name => [name, element()]));
  els.adminDrawerForm = form;
  const target = { uid: 'target', email: 'target@example.test', displayName: '팀원', role: 'member', approved: true,
    lastLoginAt: '2026-11-05T03:34:56Z' };
  const state = { authEnabled: true, adminUserFilter: 'all', adminUserQuery: '', adminUsers: [target,
    { uid: 'other', email: 'other@example.test', displayName: '다른 사람', approved: true }], currentUser: { uid: 'admin', role: 'admin' } };
  const calls = [], toasts = [];
  const context = vm.createContext({ document: { activeElement: null }, globalThis: {} });
  const dependencies = {
    './dialog_focus.js': { openDialogFocus() {}, closeDialogFocus() {} },
    './runtime.js': { state, els, apiFetch: async (url, options) => {
      calls.push({ url, options }); await gate;
      if (!options) return { ok: true, json: async () => ({ users: state.adminUsers }) };
      return { ok, json: async () => ok ? { deleted: true, uid: 'target' } : { detail: '삭제 실패: 다시 시도해 주세요.' } };
    } },
    './chat.js': { escapeHtml: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;') },
    './workspace.js': { showToast: (...args) => toasts.push(args) },
  };
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/admin_users.js'), 'utf8'), { context });
  await module.link(async specifier => {
    const values = dependencies[specifier.split('?')[0]];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [name, value] of Object.entries(values)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.initAdminUsers();
  const click = (owner, attribute, uid) => owner.listeners.click({ target: { closest: selector =>
    selector === `[${attribute}]` ? { dataset: { [attribute.slice(5).replace(/-([a-z])/g, (_, char) => char.toUpperCase())]: uid } } : null } });
  return { state, els, form, submit, error, calls, toasts, release,
    render() { module.namespace.renderUsersSection(els.adminContent); return els.adminContent.innerHTML; },
    openEdit(uid = 'target') { click(els.adminContent, 'data-admin-user-edit', uid); },
    openDelete() { click(form, 'data-admin-user-delete', 'target'); },
    close: module.namespace.closeAdminDrawer,
    send() { return form.listeners.submit({ preventDefault() {} }); },
  };
}

test('마지막 로그인은 한국 시간으로 표시하고 기록이 없으면 구분한다', async () => {
  const h = await harness();
  const html = h.render();
  assert.match(html, /마지막 로그인/);
  assert.match(html, /2026\. 11\. 05\. 12:34:56/);
  assert.match(html, /로그인 기록 없음/);
  h.state.adminUsers[0].lastLoginAt = '<invalid>';
  assert.doesNotMatch(h.render(), /Invalid Date|<invalid>/);
});

test('삭제 확인 화면에서 이메일을 확인한 후 대상만 목록에서 제거한다', async () => {
  const h = await harness(); h.openEdit();
  assert.match(h.form.innerHTML, /data-admin-user-delete/);
  h.openDelete();
  assert.match(h.form.innerHTML, /복구할 수 없/);
  h.form.elements.confirmationEmail.value = 'wrong@example.test';
  await h.send();
  assert.equal(h.calls.length, 0);
  assert.match(h.error.textContent, /이메일/);
  h.form.elements.confirmationEmail.value = 'target@example.test';
  await h.send();
  assert.equal(h.calls[0].options.method, 'DELETE');
  assert.equal(h.calls[0].url, '/api/auth/users/target');
  assert.deepEqual(JSON.parse(h.calls[0].options.body), { email: 'target@example.test' });
  assert.deepEqual(Array.from(h.state.adminUsers, user => user.uid), ['other']);
  assert.equal(h.els.adminDrawer.hidden, true);
});

test('본인에게 삭제 버튼과 삭제 화면을 제공하지 않는다', async () => {
  const h = await harness(); h.state.currentUser.uid = 'target'; h.openEdit();
  assert.doesNotMatch(h.form.innerHTML, /data-admin-user-delete/);
  const before = h.form.innerHTML; h.openDelete();
  assert.equal(h.form.innerHTML, before);
});

test('삭제 실패는 목록을 유지하고 다시 시도할 수 있게 한다', async () => {
  const h = await harness({ ok: false }); h.openDelete(); await h.send();
  assert.equal(h.state.adminUsers.length, 2);
  assert.match(h.error.textContent, /삭제 실패/);
  assert.equal(h.submit.disabled, false);
  assert.equal(h.els.adminDrawer.hidden, false);
});

test('중복 삭제를 막고 늦은 응답이 다른 사용자 패널을 닫지 않는다', async () => {
  const h = await harness({ delayed: true }); h.openDelete();
  const pending = h.send(); await h.send();
  assert.equal(h.calls.length, 1);
  h.close(); h.openEdit('other'); const content = h.form.innerHTML;
  h.release(); await pending;
  assert.equal(h.form.innerHTML, content);
  assert.equal(h.els.adminDrawer.hidden, false);
  assert.deepEqual(Array.from(h.state.adminUsers, user => user.uid), ['other']);
});
