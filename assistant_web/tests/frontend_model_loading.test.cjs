const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const modules = path.join(__dirname, '../backend/static/modules');
const flush = () => new Promise(setImmediate);

async function harness(fetch) {
  const timers = new Map();
  let timerId = 0;
  const state = { models: [] };
  const els = { modelSelect: { value: '', appendChild() {}, closest() { return null; } }, modelMeta: {}, statusLine: {} };
  const context = vm.createContext({
    AbortController, DOMException, console,
    document: { createElement: () => ({}) },
    setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  });
  async function load(name) {
    const source = fs.readFileSync(path.join(modules, name), 'utf8');
    const module = new vm.SourceTextModule(source, { context });
    const imports = new Map();
    for (const match of source.matchAll(/(?:import|export)\s*{([^}]+)}\s*from\s*["']([^"']+)["']/g)) {
      imports.set(match[2], [...new Set([...(imports.get(match[2]) || []), ...match[1].split(',').map((name) => name.trim()).filter(Boolean)])]);
    }
    await module.link(async (specifier) => {
      if (specifier.startsWith('./model_loader.js')) return load('model_loader.js');
      const values = specifier.startsWith('./runtime.js') ? { state, els, apiFetch: fetch } : {};
      const names = imports.get(specifier) || [];
      return new vm.SyntheticModule(names, function () {
        for (const name of names) this.setExport(name, values[name] || (() => {}));
      }, { context });
    });
    return module;
  }
  const module = await load('chat.js');
  await module.evaluate();
  async function settle(promise) {
    let done = false;
    const result = promise.then((value) => { done = true; return value; }, (error) => { done = true; throw error; });
    result.catch(() => {});
    for (let step = 0; step < 30 && !done; step++) {
      await flush();
      if (!done && timers.size) {
        const [id, timer] = [...timers].sort((a, b) => a[1].delay - b[1].delay)[0];
        timers.delete(id);
        timer.fn();
      }
    }
    assert.ok(done, 'model loading must have a bounded wait');
    return result;
  }
  return { state, els, timers, settle, load: module.namespace.loadModels };
}

const ok = () => ({ ok: true, status: 200, json: async () => ({ models: [{ id: 'company', status: 'active', label: '회사 모델' }] }) });

test('temporary 502 and network errors recover and select the active model', async () => {
  let calls = 0;
  const h = await harness(async () => {
    calls++;
    if (calls === 1) return { ok: false, status: 502 };
    if (calls === 2) throw new TypeError('offline');
    return ok();
  });
  await h.settle(h.load());
  assert.equal(calls, 3);
  assert.equal(h.els.modelSelect.value, 'company');
  assert.equal(h.els.statusLine.hidden, true);
});

test('simultaneous initialization and send share one model request', async () => {
  const resolvers = [];
  let calls = 0;
  const h = await harness(() => { calls++; return new Promise((done) => { resolvers.push(done); }); });
  const first = h.load();
  const second = h.load();
  for (const resolve of resolvers) resolve(ok());
  await Promise.all([first, second]);
  assert.equal(calls, 1);
});

test('a timed out request retries and clears its timer', async () => {
  let calls = 0;
  const h = await harness((_url, options = {}) => {
    if (++calls > 1) return Promise.resolve(ok());
    return new Promise((_resolve, reject) => options.signal?.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError'))));
  });
  await h.settle(h.load());
  assert.equal(calls, 2);
  assert.equal(h.timers.size, 0);
});

test('persistent failure is bounded and a later send can start recovery again', async () => {
  let healthy = false;
  let calls = 0;
  const h = await harness(async () => { calls++; return healthy ? ok() : { ok: false, status: 503 }; });
  await assert.rejects(h.settle(h.load()));
  assert.ok(calls > 1 && calls <= 8);
  healthy = true;
  await h.settle(h.load());
  assert.equal(h.els.modelSelect.value, 'company');
});

test('authentication failure is not retried', async () => {
  let calls = 0;
  const h = await harness(async () => { calls++; return { ok: false, status: 401 }; });
  await assert.rejects(h.settle(h.load()), /로그인/);
  assert.equal(calls, 1);
});

test('empty model configuration reports a service problem without sending more requests', async () => {
  let calls = 0;
  const h = await harness(async () => { calls++; return { ok: true, json: async () => ({ models: [] }) }; });
  await assert.rejects(h.settle(h.load()), /모델.*설정/);
  assert.equal(calls, 1);
});

test('canceling one waiting question does not cancel shared initialization', async () => {
  let resolve;
  let calls = 0;
  const h = await harness(() => { calls++; return new Promise((done) => { resolve = done; }); });
  const background = h.load();
  const controller = new AbortController();
  const waiting = h.load(controller.signal);
  controller.abort();
  await assert.rejects(waiting, { name: 'AbortError' });
  resolve(ok());
  await background;
  assert.equal(calls, 1);
  assert.equal(h.els.modelSelect.value, 'company');
});

test('model initialization starts while templates are still pending', async () => {
  let requests = 0;
  let finishConversations;
  let finishTemplates;
  let renders = 0;
  const conversations = new Promise((resolve) => { finishConversations = resolve; });
  const state = { initializing: true };
  const context = vm.createContext({
    AssistantAuthReady: Promise.resolve({}),
    fetch: async () => ({ ok: false }), setInterval() {},
  });
  const source = fs.readFileSync(path.join(modules, 'bootstrap.js'), 'utf8');
  const imports = new Map([...source.matchAll(/import\s*{([^}]+)}\s*from\s*["']([^"']+)["']/g)]
    .map((match) => [match[2], match[1].split(',').map((name) => name.trim())]));
  const values = {
    state, els: { adminGenerationSettings: {} }, defaultOptions: {},
    canUseAdminTools: () => false,
    loadConversations: () => conversations,
    loadModels: async () => { requests++; },
    loadTeamTemplates: () => new Promise((resolve) => { finishTemplates = resolve; }),
    renderMessages: () => { renders++; },
  };
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const names = imports.get(specifier) || [];
    return new vm.SyntheticModule(names, function () {
      for (const name of names) this.setExport(name, values[name] || (() => Promise.resolve()));
    }, { context });
  });
  await module.evaluate();
  await flush();
  assert.equal(requests, 1);
  assert.equal(state.initializing, true);
  finishConversations();
  await flush();
  assert.equal(state.initializing, false, 'only conversation restoration gates submission');
  assert.equal(renders, 0, 'initial template cards must wait for their catalog');
  finishTemplates();
  await flush();
  assert.equal(renders, 1);
});

test('submission preserves the draft and history until initial conversation restoration completes', async () => {
  const source = fs.readFileSync(path.join(modules, 'events.js'), 'utf8');
  const start = source.indexOf('els.form.addEventListener("submit"');
  const end = source.indexOf('\nels.input.addEventListener', start);
  let submit, persisted = 0, sent = 0;
  const state = { initializing: true, messages: [], editingMessageIndex: null };
  const els = { form: { addEventListener(_event, handler) { submit = handler; } }, input: { value: '새 질문' } };
  vm.runInNewContext(source.slice(start, end), {
    state, els, showToast() {}, autoResizeInput() {},
    persistConversations() { persisted++; }, requestAssistant() { sent++; },
  });
  await submit({ preventDefault() {} });
  assert.equal(els.input.value, '새 질문');
  assert.equal(state.messages.length, 0);
  assert.equal(persisted, 0);
  assert.equal(sent, 0);
  state.initializing = false;
  state.messages = [{ role: 'assistant', content: '복원된 답변' }];
  await submit({ preventDefault() {} });
  assert.equal(state.messages[0].content, '복원된 답변');
  assert.equal(state.messages[1].content, '새 질문');
  assert.equal(sent, 1);
});
