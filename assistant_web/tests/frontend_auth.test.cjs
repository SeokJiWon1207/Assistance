const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const user = { uid: 'fixture', email: 'member@example.test', role: 'member', passwordChangeRequired: true };
const tick = () => new Promise(resolve => setImmediate(resolve));
async function harness(existing = null, { initialAuthStatuses = {}, firebaseApiKey = 'fixture' } = {}) {
  const nodes = new Map();
  function element(id) {
    if (!nodes.has(id)) {
      const classes = new Set(id === 'body' ? ['auth-pending'] : []);
      const listeners = {};
      nodes.set(id, { hidden: true, value: '', textContent: '', disabled: false,
        classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x), toggle() {} },
        focus() {}, reset() {}, addEventListener(name, fn) { listeners[name] = fn; },
        async fire(name) { await listeners[name]?.({ preventDefault() {} }); },
      });
    }
    return nodes.get(id);
  }
  element('#authGate').hidden = false;
  const calls = [];
  const timers = [];
  let sessionUser = user;
  let changeOk = true;
  const context = vm.createContext({ Headers, setTimeout: callback => timers.push(callback),
    document: { cookie: 'ai_csrf=fixture', body: element('body'), querySelector: element, addEventListener() {} },
    location: { reload() {} },
    fetch: async (url, options = {}) => {
      calls.push({ url, options });
      let data, status = 200;
      if (url === '/api/auth/config') data = { enabled: true, firebaseApiKey };
      else if (url === '/api/auth/me') { data = { user: existing }; status = existing ? 200 : 401; }
      else if (url.includes('signInWithPassword')) data = { idToken: 'fixture-token' };
      else if (url === '/api/auth/session') data = { user: sessionUser };
      else if (url === '/api/auth/change-password') { data = changeOk ? { changed: true } : { detail: '현재 비밀번호 오류' }; status = changeOk ? 200 : 400; }
      else data = {};
      status = initialAuthStatuses[url]?.shift() ?? status;
      return { ok: status === 200, status, json: async () => data };
    },
  });
  const focus = new vm.SyntheticModule(['openDialogFocus', 'closeDialogFocus'], function () {
    this.setExport('openDialogFocus', () => {}); this.setExport('closeDialogFocus', () => {});
  }, { context });
  await focus.link(() => {}); await focus.evaluate();
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/auth.js'), 'utf8');
  const script = new vm.Script(source, { importModuleDynamically: async () => focus });
  script.runInContext(context);
  await tick();
  let ready = false;
  context.AssistantAuthReady.then(() => { ready = true; });
  return { element, calls, context, get ready() { return ready; },
    setSession(value) { sessionUser = value; }, failChange() { changeOk = false; },
    async retry() { const next = timers.shift(); assert.ok(next); next(); await tick(); },
    async login() { element('#loginEmail').value = user.email; element('#loginPassword').value = 'Temporary-123!'; await element('#loginForm').fire('submit'); await tick(); },
    async change(confirm = 'Changed-123!') { element('#currentPassword').value = 'Temporary-123!'; element('#newPassword').value = 'Changed-123!'; element('#confirmPassword').value = confirm; await element('#passwordChangeForm').fire('submit'); await tick(); },
  };
}

test('임시 비밀번호 로그인은 앱을 열지 않고 변경 후 새 비밀번호 재로그인을 요구한다', async () => {
  const h = await harness();
  await h.login();
  assert.equal(h.ready, false);
  assert.equal(h.element('#passwordChangeForm').hidden, false);
  assert.equal(h.element('#loginPassword').value, '');
  assert.equal(h.element('body').classList.contains('auth-pending'), true);
  await h.change();
  const call = h.calls.find(call => call.url === '/api/auth/change-password');
  assert.deepEqual(JSON.parse(call.options.body), { currentPassword: 'Temporary-123!', newPassword: 'Changed-123!' });
  assert.equal(call.options.headers.get('X-CSRF-Token'), 'fixture');
  assert.equal(h.ready, false);
  assert.equal(h.element('#loginForm').hidden, false);
  assert.equal(h.element('#newPassword').value, '');
  h.setSession({ ...user, passwordChangeRequired: false });
  await h.login();
  assert.equal(h.ready, true);
  assert.equal(h.element('#authGate').hidden, true);
});

test('새로고침한 최초 로그인 세션도 비밀번호 변경 화면에 머문다', async () => {
  const h = await harness(user);
  assert.equal(h.ready, false);
  assert.equal(h.element('#passwordChangeForm').hidden, false);
  assert.equal(h.element('#loginForm').hidden, true);
});

test('확인 불일치나 서버 변경 실패로는 앱이 열리지 않는다', async () => {
  const h = await harness(user);
  await h.change('different');
  assert.equal(h.calls.some(call => call.url === '/api/auth/change-password'), false);
  assert.equal(h.element('#passwordChangeError').hidden, false);
  h.failChange();
  await h.change();
  assert.equal(h.ready, false);
  assert.equal(h.element('#passwordChangeForm').hidden, false);
  assert.equal(h.element('#passwordChangeError').textContent, '현재 비밀번호 오류');
});

test('기존 일반 계정은 기존 로그인 동작을 유지한다', async () => {
  const h = await harness({ ...user, passwordChangeRequired: false });
  await tick();
  assert.equal(h.ready, true);
  assert.equal(h.element('#authGate').hidden, true);
});

for (const endpoint of ['/api/auth/config', '/api/auth/me']) {
  test(`초기 인증 ${endpoint} 장애가 복구되면 기존 세션으로 앱을 연다`, async () => {
    const h = await harness({ ...user, passwordChangeRequired: false }, { initialAuthStatuses: { [endpoint]: [503, 503] } });
    assert.equal(h.element('#loginForm').hidden, true);
    assert.equal(h.ready, false);
    await h.retry();
    assert.equal(h.element('#loginForm').hidden, true);
    await h.retry();
    assert.equal(h.ready, true);
    assert.equal(h.element('#authGate').hidden, true);
    assert.equal(h.calls.filter(call => call.url === endpoint && call.options.credentials === 'same-origin').length, 3);
    assert.equal(h.calls.some(call => call.url === '/api/auth/session'), false);
  });
}

test('초기 인증 장애가 복구된 뒤 세션이 무효라면 로그인 화면을 연다', async () => {
  const h = await harness(null, { initialAuthStatuses: { '/api/auth/me': [503] } });
  assert.equal(h.element('#loginForm').hidden, true);
  await h.retry();
  assert.equal(h.element('#loginForm').hidden, false);
  assert.equal(h.ready, false);
});

test('신규 로그인용 API 키가 없어도 유효한 기존 세션으로 앱을 연다', async () => {
  const h = await harness({ ...user, passwordChangeRequired: false }, { firebaseApiKey: '' });
  await tick();
  assert.equal(h.ready, true);
  assert.equal(h.element('#authGate').hidden, true);
});
