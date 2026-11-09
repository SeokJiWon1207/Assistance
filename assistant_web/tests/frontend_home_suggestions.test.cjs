const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));
const item = (id = 's1') => ({ id, title: '<추천>', promptPreview: '상세 질문', expiresAt: '2099-01-01T00:00:00Z',
  sources: [{ type: 'mantis', title: '이슈', url: 'https://mantis.test/view.php?id=1' },
    { type: 'notion', title: '문서', url: 'https://www.notion.so/' + 'a'.repeat(32) }] });
const response = (body, status = 200) => ({ ok: status < 400, status, json: async () => body });
async function setup(fetcher, options = {}) {
  const listeners = {}, inputListeners = {}, timers = new Map(), calls = [];
  const host = { innerHTML: '', hidden: true, querySelector: () => null };
  const state = { currentUser: { uid: 'u' }, activeConversationId: null, messages: [], initializing: false, busy: false, editingMessageIndex: null, pendingTemplateId: null };
  const input = { value: '', focus() { this.focused = true; }, addEventListener: (n, cb) => inputListeners[n] = cb,
    dispatchEvent: e => inputListeners[e.type]?.(e) };
  const els = { input, messages: { querySelector: () => host, addEventListener: (n, cb) => listeners['messages:' + n] = cb }, appShell: { dataset: { view: 'chat' } } };
  const context = vm.createContext({ URL, AbortController, Date, Event, console,
    document: { hidden: false, addEventListener: (n, cb) => listeners[n] = cb },
    addEventListener: (n, cb) => listeners[n] = cb,
    setTimeout: (cb, ms) => { const id = Symbol(); timers.set(id, { cb, ms }); return id; }, clearTimeout: id => timers.delete(id),
    MutationObserver: class { observe() {} disconnect() {} },
  });
  const deps = {
    './runtime.js': { state, els, automaticSourceTools: () => options.tools || { mantis: true, notion: true },
      apiFetch: async (url, init) => { calls.push({ url, init }); return fetcher(url, init); } },
    './chat_rendering.js': { escapeHtml: value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])) },
  };
  const mod = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/home_suggestions.js'), 'utf8'), { context });
  await mod.link(spec => { const dep = deps[spec.replace(/\?v=.*/, '')]; return new vm.SyntheticModule(Object.keys(dep), function () { Object.entries(dep).forEach(([k,v]) => this.setExport(k,v)); }, { context }); });
  await mod.evaluate();
  const click = (action, id = 's1') => listeners['messages:click']?.({ target: { closest: selector => selector === '[data-suggestion-action]' ? { dataset: { suggestionAction: action, suggestionId: id } } : null }, preventDefault() {} });
  return { ...mod.namespace, state, input, host, calls, timers, listeners, click, inputListeners, document: context.document };
}

test('renders at most three safe rows with separate source links and accessible preview', async () => {
  const ui = await setup(async () => response({}));
  const html = ui.homeSuggestionsHtml({ state: 'ready', items: [item(), item('s2'), item('s3'), item('s4')] });
  assert.equal((html.match(/data-suggestion-action="prepare"/g) || []).length, 3);
  assert.ok(html.includes('&lt;추천&gt;'));
  assert.ok(html.includes('https://www.notion.so/'));
  assert.ok(html.includes('<summary>'));
  assert.ok(html.includes('noopener noreferrer'));
  const unsafe = item(); unsafe.sources[0].url = 'javascript:alert(1)'; unsafe.sources[1].url = 'https://evil.test/';
  const bad = ui.homeSuggestionsHtml({ state: 'ready', items: [unsafe] });
  assert.ok(!bad.includes('href="javascript:')); assert.ok(!bad.includes('href="https://evil.test/'));
});

test('prepare fills input only and preserves template and editing boundaries', async () => {
  const ui = await setup(async url => response(url.endsWith('/prepare') ? { prompt: '최신 질문' } : { state: 'ready', items: [item()] }));
  ui.initHomeSuggestions(); await flush(); ui.click('prepare'); await flush();
  assert.equal(ui.input.value, '최신 질문'); assert.equal(ui.input.focused, true);
  assert.equal(ui.calls.filter(x => x.url.endsWith('/prepare')).length, 1);
  assert.ok(!ui.calls.some(x => x.url.includes('/chat')));
  ui.state.editingMessageIndex = 0; ui.input.value = ''; ui.click('prepare'); await flush();
  assert.equal(ui.calls.filter(x => x.url.endsWith('/prepare')).length, 1);
});

test('source link clicks never prepare a question', async () => {
  const ui = await setup(async () => response({ state: 'ready', items: [item()] }));
  ui.initHomeSuggestions(); await flush();
  ui.listeners['messages:click']({ target: { closest: () => null } }); await flush();
  assert.equal(ui.calls.length, 1);
});

for (const change of ['draft', 'typed-and-cleared', 'conversation', 'account', 'template', 'view']) {
  test('late prepare cannot overwrite after ' + change, async () => {
    let finish;
    const ui = await setup(async url => url.endsWith('/prepare') ? new Promise(resolve => finish = resolve) : response({ state: 'ready', items: [item()] }));
    ui.initHomeSuggestions(); await flush(); ui.click('prepare'); await flush();
    if (change === 'draft') { ui.input.value = '내 초안'; ui.inputListeners.input(); }
    if (change === 'typed-and-cleared') { ui.inputListeners.input(); ui.input.value = ''; }
    if (change === 'conversation') ui.state.activeConversationId = 'other';
    if (change === 'account') ui.state.currentUser.uid = 'other';
    if (change === 'template') ui.state.pendingTemplateId = 'weekly';
    if (change === 'view') ui.stopHomeSuggestions();
    finish(response({ prompt: '늦은 질문' })); await flush();
    assert.equal(ui.input.value, change === 'draft' ? '내 초안' : '');
  });
}

test('existing draft and disabled source are not silently replaced or enabled', async () => {
  const ui = await setup(async () => response({ state: 'ready', items: [item()] }), { tools: { mantis: true, notion: false } });
  ui.initHomeSuggestions(); await flush(); ui.click('prepare'); await flush();
  assert.equal(ui.calls.length, 1); assert.match(ui.host.innerHTML, /Notion/);
  ui.input.value = '초안'; ui.click('prepare'); await flush(); assert.equal(ui.input.value, '초안');
});

test('failed dismiss retains row and successful dismiss removes it', async () => {
  let fail = true;
  const ui = await setup(async url => url.endsWith('/dismiss') ? response({}, fail ? 503 : 200) : response({ state: 'ready', items: [item()] }));
  ui.initHomeSuggestions(); await flush(); ui.click('dismiss'); await flush();
  assert.match(ui.host.innerHTML, /data-suggestion-id="s1"/);
  fail = false; ui.click('dismiss'); await flush(); assert.ok(!ui.host.innerHTML.includes('data-suggestion-id="s1"'));
});

test('pending polls stop after twelve reads and disabled mode never refreshes', async () => {
  const ui = await setup(async () => response({ state: 'pending', items: [], generationId: 'g' }));
  ui.initHomeSuggestions(); await flush();
  for (let i = 0; i < 15; i++) { const entry = [...ui.timers.entries()].find(([,v]) => v.ms === 5000); if (entry) { ui.timers.delete(entry[0]); entry[1].cb(); await flush(); } }
  assert.equal(ui.calls.length, 13); assert.equal(ui.timers.size, 0);
  const disabled = await setup(async () => response({ state: 'disabled', items: [] }));
  disabled.initHomeSuggestions(); await flush(); assert.equal(disabled.calls.length, 1); assert.equal(disabled.host.hidden, true);
});

test('long or malformed fixture expiry cannot create a tight refresh loop', async () => {
  const ui = await setup(async () => response({ state: 'ready', items: [item()] }));
  ui.initHomeSuggestions(); await flush();
  assert.ok([...ui.timers.values()].every(timer => timer.ms > 0 && timer.ms <= 600000));
});

test('switching browser visibility keeps the recommendation DOM stable', async () => {
  const ui = await setup(async () => response({ state: 'ready', items: [item()] }));
  ui.initHomeSuggestions(); await flush();
  const before = ui.host.innerHTML;
  ui.document.hidden = true; ui.listeners.visibilitychange?.(); await flush();
  assert.equal(ui.host.innerHTML, before);
  assert.equal(ui.calls.length, 1);
});

test('dismiss fills the vacant slot from cached reserves without regenerating', async () => {
  let dismissed = false;
  const ui = await setup(async url => {
    if (url.endsWith('/dismiss')) { dismissed = true; return response({}); }
    return response({ state: 'ready', items: dismissed ? [item('s2'), item('s3'), item('s4')] : [item('s1'), item('s2'), item('s3')] });
  });
  ui.initHomeSuggestions(); await flush(); ui.click('dismiss'); await flush();
  assert.equal((ui.host.innerHTML.match(/data-suggestion-action="prepare"/g) || []).length, 3);
  assert.match(ui.host.innerHTML, /data-suggestion-id="s4"/);
  assert.ok(!ui.host.innerHTML.includes('data-suggestion-id="s1"'));
  assert.ok(!ui.calls.some(c => c.url.endsWith('/refresh')));
});

test('failed replacement read keeps remaining rows after successful dismiss', async () => {
  let dismissed = false;
  const ui = await setup(async url => {
    if (url.endsWith('/dismiss')) { dismissed = true; return response({}); }
    return dismissed ? response({}, 503) : response({ state: 'ready', items: [item('s1'), item('s2')] });
  });
  ui.initHomeSuggestions(); await flush(); ui.click('dismiss'); await flush();
  assert.ok(!ui.host.innerHTML.includes('data-suggestion-id="s1"'));
  assert.match(ui.host.innerHTML, /data-suggestion-id="s2"/);
  assert.match(ui.host.innerHTML, /불러오지 못/);
});

test('late replacement read cannot cross conversation boundary', async () => {
  let dismissed = false, finish;
  const ui = await setup(async url => {
    if (url.endsWith('/dismiss')) { dismissed = true; return response({}); }
    return dismissed ? new Promise(resolve => finish = resolve) : response({ state: 'ready', items: [item()] });
  });
  ui.initHomeSuggestions(); await flush(); ui.click('dismiss'); await flush();
  assert.equal(typeof finish, 'function');
  ui.stopHomeSuggestions(); ui.state.activeConversationId = 'other';
  finish(response({ state: 'ready', items: [item('late')] })); await flush();
  assert.ok(!ui.host.innerHTML.includes('data-suggestion-id="late"'));
});

for (const status of [404, 410]) {
  test(`prepare ${status} removes the row and fills it from cached reserves`, async () => {
    let gone = false;
    const ui = await setup(async url => {
      if (url.endsWith('/prepare')) { gone = true; return response({}, status); }
      return response({ state: 'ready', items: gone ? [item('s2'), item('s3'), item('s4')] : [item('s1'), item('s2'), item('s3')] });
    });
    ui.initHomeSuggestions(); await flush(); ui.click('prepare', 's1'); await flush(); await flush();
    assert.ok(!ui.host.innerHTML.includes('data-suggestion-id="s1"'));
    assert.match(ui.host.innerHTML, /data-suggestion-id="s4"/);
    assert.match(ui.host.innerHTML, /목록에서 뺐어요/);
    assert.equal(ui.input.value, '');
    assert.ok(!ui.calls.some(c => c.url.endsWith('/refresh')));
  });
}

test('prepare 503 keeps the row and does not reload reserves', async () => {
  const ui = await setup(async url => url.endsWith('/prepare') ? response({}, 503) : response({ state: 'ready', items: [item()] }));
  ui.initHomeSuggestions(); await flush(); ui.click('prepare'); await flush(); await flush();
  assert.match(ui.host.innerHTML, /data-suggestion-id="s1"/);
  assert.equal(ui.calls.filter(c => !c.url.endsWith('/prepare')).length, 1);
});

test('first source shows only its source name while later sources keep their titles', async () => {
  const ui = await setup(async () => response({}));
  const html = ui.homeSuggestionsHtml({ state: 'ready', items: [item()] });
  assert.match(html, />Mantis ↗<\/a>/);
  assert.match(html, /aria-label="Mantis · 이슈 \(새 창\)"/);
  assert.match(html, />Notion · 문서 ↗<\/a>/);
});
