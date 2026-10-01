const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const core = require('../backend/static/frontend_core.js');

async function harness(apiFetch) {
  const state = { currentUser: { uid: 'user-a' }, conversations: [], messages: [], activeConversationId: null, workspaceTab: 'overview' };
  const storage = new Map();
  const timers = new Map();
  let timerId = 0;
  const element = () => ({ appendChild() {}, setAttribute() {}, classList: { add() {}, remove() {} }, remove() {} });
  const context = vm.createContext({
    AssistantFrontendCore: core, crypto: { randomUUID: () => 'recovered' },
    document: { createElement: element, body: element() }, requestAnimationFrame: (fn) => fn(),
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  const workspaceContent = { innerHTML: '', attributes: {}, classList: { toggle() {} }, setAttribute(key, value) { this.attributes[key] = value; } };
  const htmlWrites = [];
  Object.defineProperty(workspaceContent, 'innerHTML', {
    get: () => htmlWrites.at(-1) || '',
    set: (html) => htmlWrites.push(html),
  });
  const workspaceRefreshBtn = { disabled: false, textContent: '새로고침', attributes: {}, setAttribute(key, value) { this.attributes[key] = value; } };
  const els = {
    workspaceContent,
    workspaceRefreshBtn,
    workspaceModalEyebrow: { textContent: '' },
    workspaceModalTitle: { textContent: '' },
    workspaceTabs: { hidden: false, querySelectorAll: () => [] },
  };
  const runtime = { state, els, conversationsStorageKeyBase: 'chats', activeConversationStorageKeyBase: 'active',
    maxStoredConversations: 20, conversationSyncTimers: new Map(), apiFetch,
    ...core, scopedStorageKey: (base, uid) => `${base}:${uid}`,
    createConversationRecord: () => ({ id: 'new', messages: [] }), WORKSPACE_ENDPOINTS: { personal: '/personal' } };
  const modules = {
    './runtime.js': runtime,
    './chat.js': { renderMessages() {}, escapeHtml: String, autoResizeInput() {} },
    './events.js': { cancelMessageEdit() {} },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
  };
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/workspace.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const exports = modules[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  return { state, storage, timers, els, htmlWrites, api: module.namespace };
}
const message = (content) => ({ role: 'user', content });
const json = (value) => JSON.parse(JSON.stringify(value));

test('refresh retains pending local messages and local-only conversations', async () => {
  const local = { id: 'chat', title: '질문', messages: [message('질문'), message('새 질문')], syncPending: true, serverUpdatedAt: 'old' };
  const server = { id: 'chat', title: '질문', messages: [message('질문')], updatedAt: 'old' };
  const h = await harness(async () => ({ ok: true, json: async () => ({ conversations: [server] }) }));
  h.storage.set('chats:user-a', JSON.stringify([local, { id: 'offline', messages: [message('오프라인')] }]));
  await h.api.loadConversations();
  assert.equal(h.state.conversations.find((item) => item.id === 'chat').messages.length, 2);
  assert.ok(h.state.conversations.find((item) => item.id === 'offline'));
  assert.equal(JSON.parse(h.storage.get('chats:user-a'))[0].syncPending, true);
});

test('conflicting remote version preserves both histories', () => {
  const merged = core.mergeConversations(
    [{ id: 'chat', title: 'local', messages: [message('local')], syncPending: true, serverUpdatedAt: 'v1' }],
    [{ id: 'chat', title: 'remote', messages: [message('remote')], updatedAt: 'v2' }], () => 'copy');
  assert.equal(merged.length, 2);
  assert.equal(merged.find((item) => item.id === 'copy').messages[0].content, 'local');
  assert.equal(merged.find((item) => item.id === 'chat').messages[0].content, 'remote');
});

test('failed PUT leaves local pending state for retry', async () => {
  const h = await harness(async () => ({ ok: false, status: 500 }));
  const conversation = { id: 'chat', messages: [message('질문')] };
  h.state.conversations = [conversation];
  h.api.scheduleConversationSync(conversation);
  await assert.rejects(h.api.syncConversation(conversation));
  assert.equal(conversation.syncPending, true);
  assert.equal(JSON.parse(h.storage.get('chats:user-a'))[0].messages[0].content, '질문');
});

test('delete waits for in-flight PUT and cancels delayed saves', async () => {
  const calls = [];
  let finish;
  const h = await harness(async (_url, options) => {
    calls.push(options.method);
    if (options.method === 'PUT') await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => ({ conversation: { updatedAt: 'v2' } }) };
  });
  const conversation = { id: 'chat', title: '질문', messages: [message('질문')] };
  h.state.conversations = [conversation];
  const saving = h.api.syncConversation(conversation);
  await new Promise(setImmediate);
  h.api.scheduleConversationSync(conversation);
  h.state.conversations = [];
  const deleting = h.api.deleteConversationFromServer(conversation);
  finish();
  await Promise.all([saving, deleting]);
  assert.deepEqual(calls, ['PUT', 'DELETE']);
  assert.equal([...h.timers.values()].filter((timer) => timer.ms === 350).length, 0);
});

const serverConversations = (count) => Array.from({ length: count }, (_, index) => ({
  id: `server-${index}`, title: `질문 ${index}`, messages: [message(`질문 ${index}`)],
  updatedAt: new Date(Date.UTC(2026, 8, 30) - index * 60000).toISOString(),
}));

test('conversation list keeps every loaded conversation while the device stores only recent ones', async () => {
  const h = await harness(async () => ({ ok: true, json: async () => ({ conversations: serverConversations(25) }) }));
  await h.api.loadConversations();
  h.api.persistConversations();
  assert.equal(h.state.conversations.length, 25);
  assert.equal(JSON.parse(h.storage.get('chats:user-a')).length, 20);
});

test('older conversations load up to the server maximum only on request', async () => {
  const urls = [];
  const h = await harness(async (url) => {
    urls.push(url);
    return { ok: true, json: async () => ({ conversations: serverConversations(url.includes('limit=100') ? 60 : 40) }) };
  });
  await h.api.loadConversations();
  assert.equal(h.state.conversations.length, 40);
  await h.api.loadMoreConversations();
  assert.equal(h.state.conversations.length, 60);
  assert.deepEqual(urls, ['/api/workspace/conversations?limit=40', '/api/workspace/conversations?limit=100']);
});

test('Notion action id survives storage and is sent with confirmation', () => {
  const history = core.storageSafeMessages([{ role: 'assistant', content: '확인', notionActionId: 'a'.repeat(32) }]);
  const messages = core.buildChatMessages([...history, message('네')], '네');
  assert.equal(messages[0].notionActionId, 'a'.repeat(32));
  assert.equal(json(messages)[1].content, '네');
});

test('late response from previous account cannot replace the next account history', async () => {
  let finish;
  const h = await harness(async () => new Promise((resolve) => { finish = resolve; }));
  const loading = h.api.loadConversations();
  h.state.currentUser = { uid: 'user-b' };
  h.state.conversations = [{ id: 'b-only', messages: [message('B')] }];
  finish({ ok: true, json: async () => ({ conversations: [{ id: 'a-only', messages: [message('A')] }] }) });
  await loading;
  assert.equal(h.state.conversations[0].id, 'b-only');
  assert.equal(h.storage.has('chats:user-b'), false);
});

test('personal workspace joins a single live lookup and hides lists until completion', async () => {
  const calls = [];
  const payloads = [
    { summary: { open: 2 }, mantis: { status: 'live', fetchedAt: new Date().toISOString(), search_complete: true, issues: [{ id: 2, title: '최신 업무' }] }, user: { mantisUsername: '석지원' } },
  ];
  let releaseLive;
  const h = await harness(async (url) => {
    calls.push(url);
    await new Promise((resolve) => { releaseLive = resolve; });
    return { ok: true, json: async () => payloads.shift() };
  });
  const prefetch = h.api.loadMyWorkspace();
  const loading = h.api.ensureMyWorkspaceFresh();
  const joined = h.api.ensureMyWorkspaceFresh();
  await new Promise(setImmediate);
  assert.equal(h.els.workspaceRefreshBtn.disabled, true);
  assert.equal(h.els.workspaceRefreshBtn.textContent, '불러오는 중…');
  assert.match(h.els.workspaceContent.innerHTML, /업무 정보를 불러오는 중입니다/);
  assert.equal(h.els.workspaceContent.attributes['aria-busy'], 'true');
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /workspace-overview|workspace-issue-list/);
  assert.ok(h.htmlWrites.every((html) => !html.includes('이전 업무')));
  releaseLive();
  await Promise.all([prefetch, loading, joined]);
  assert.deepEqual(calls, ['/personal?live=true']);
  assert.equal(h.state.myWorkspace.summary.open, 2);
  assert.equal(h.els.workspaceRefreshBtn.disabled, false);
  assert.equal(h.els.workspaceRefreshBtn.attributes['aria-busy'], 'false');
  assert.equal(h.els.workspaceContent.attributes['aria-busy'], 'false');
  assert.match(h.els.workspaceContent.innerHTML, /최신 업무/);
  assert.ok(h.htmlWrites.every((html) => !html.includes('이전 업무')));
  await h.api.ensureMyWorkspaceFresh();
  assert.equal(calls.length, 1);
});

test('personal refresh hides existing list and restores it only after failure', async () => {
  let finish;
  const h = await harness(async () => new Promise((resolve) => { finish = resolve; }));
  h.state.myWorkspace = { summary: { open: 1 }, mantis: { status: 'snapshot', issues: [{ id: 1, title: '기존 업무' }] } };
  h.api.renderWorkspace();
  assert.match(h.els.workspaceContent.innerHTML, /기존 업무/);
  const loading = h.api.loadMyWorkspace(true);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /기존 업무|workspace-issue-list/);
  finish({ ok: false, json: async () => ({ detail: '조회 실패' }) });
  await assert.rejects(loading, /조회 실패/);
  assert.match(h.els.workspaceContent.innerHTML, /조회 실패/);
  assert.match(h.els.workspaceContent.innerHTML, /기존 업무/);
  assert.equal(h.els.workspaceContent.attributes['aria-busy'], 'false');
  assert.equal(h.els.workspaceRefreshBtn.disabled, false);
});

test('personal initial failure exits loading state without showing an empty work list', async () => {
  const h = await harness(async () => ({ ok: false, json: async () => ({ detail: '조회 실패' }) }));
  await assert.rejects(h.api.ensureMyWorkspaceFresh(), /조회 실패/);
  assert.match(h.els.workspaceContent.innerHTML, /조회 실패/);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /workspace-spinner|workspace-issue-list/);
  assert.equal(h.els.workspaceRefreshBtn.disabled, false);
});

test('unlinked personal account finishes after its initial lookup', async () => {
  let calls = 0;
  const h = await harness(async () => {
    calls++;
    return { ok: true, json: async () => ({ mantis: { status: 'unlinked', issues: [] }, user: {} }) };
  });
  await h.api.ensureMyWorkspaceFresh();
  assert.equal(calls, 1);
  assert.match(h.els.workspaceContent.innerHTML, /담당자명이 연결되지 않았습니다/);
  assert.equal(h.els.workspaceContent.attributes['aria-busy'], 'false');
});


test('personal workspace does not treat old cached data as freshly fetched', async () => {
  let calls = 0;
  const payload = { summary: { open: 0 }, user: { mantisUsername: '석지원' },
    mantis: { status: 'live', search_complete: false, fetchedAt: new Date(Date.now() - 120000).toISOString(), issues: [] } };
  const h = await harness(async () => { calls++; return { ok: true, json: async () => payload }; });
  await h.api.loadMyWorkspace();
  await h.api.ensureMyWorkspaceFresh();
  assert.equal(calls, 2);
  assert.match(h.els.workspaceContent.innerHTML, /전체 결과는 미확정/);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /실시간/);
});

test('personal sync completion refreshes data without hiding the visible list', async () => {
  let calls = 0;
  const h = await harness(async () => {
    calls++;
    return { ok: true, json: async () => ({ user: { mantisUsername: '석지원' }, summary: { open: 0 },
      mantis: { status: 'snapshot', fetchedAt: new Date().toISOString(), search_complete: true, refreshPending: calls === 1, issues: [] } }) };
  });
  await h.api.loadMyWorkspace(true);
  assert.match(h.els.workspaceContent.innerHTML, /갱신 중/);
  const timer = [...h.timers.values()].find((entry) => entry.ms === 10000);
  assert.ok(timer);
  const before = h.htmlWrites.length;
  timer.fn();
  await new Promise(setImmediate);
  assert.equal(calls, 2);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /갱신 중/);
  assert.ok(h.htmlWrites.slice(before).every((html) => !html.includes('workspace-spinner')));
});


test('personal snapshot keeps fetch time while reusing the valid response window', async () => {
  let calls=0;
  const payload = {user:{mantisUsername:'사용자'}, summary:{open:0}, mantis:{status:'snapshot', search_complete:true,
    fetchedAt:new Date(Date.now()-600000).toISOString(), cacheUntil:new Date(Date.now()+50000).toISOString(), issues:[]}};
  const h=await harness(async()=>{calls++;return {ok:true,json:async()=>payload};});
  await h.api.loadMyWorkspace();
  await h.api.ensureMyWorkspaceFresh();
  assert.equal(calls,1);
  payload.mantis.cacheUntil = new Date(Date.now()-1).toISOString();
  await h.api.ensureMyWorkspaceFresh();
  assert.equal(calls,2);
});

test('personal source error hides unsupported zero counts', async () => {
  const h=await harness(async()=>({ok:true,json:async()=>({user:{mantisUsername:'사용자'}, summary:{open:0},
    mantis:{status:'error',reason:'저장 데이터 만료 및 실시간 조회 실패',issues:[]}})}));
  await h.api.loadMyWorkspace();
  assert.match(h.els.workspaceContent.innerHTML,/저장 데이터 만료/);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML,/workspace-summary-grid/);
});
