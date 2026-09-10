const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const core = require('../backend/static/frontend_core.js');

async function harness(apiFetch) {
  const state = { currentUser: { uid: 'user-a' }, conversations: [], messages: [], activeConversationId: null };
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
  const runtime = { state, els: {}, conversationsStorageKeyBase: 'chats', activeConversationStorageKeyBase: 'active',
    maxStoredConversations: 20, conversationSyncTimers: new Map(), apiFetch,
    ...core, scopedStorageKey: (base, uid) => `${base}:${uid}`,
    createConversationRecord: () => ({ id: 'new', messages: [] }), WORKSPACE_ENDPOINTS: {} };
  const modules = {
    './runtime.js': runtime,
    './chat.js': { renderMessages() {}, escapeHtml: String, autoResizeInput() {} },
    './events.js': { cancelMessageEdit() {} },
  };
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/workspace.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const exports = modules[specifier];
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  return { state, storage, timers, api: module.namespace };
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
