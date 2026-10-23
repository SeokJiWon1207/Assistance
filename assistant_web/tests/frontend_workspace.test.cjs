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
    AssistantFrontendCore: core, crypto: { randomUUID: () => 'recovered' }, URLSearchParams,
    document: { createElement: element, body: element() }, requestAnimationFrame: (fn) => fn(),
    localStorage: { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value) },
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: (id) => timers.delete(id),
  });
  const workspaceContent = { innerHTML: '', attributes: {}, classList: { toggle() {} }, setAttribute(key, value) { this.attributes[key] = value; } };
  const htmlWrites = [];
  const badges = [];
  Object.defineProperty(workspaceContent, 'innerHTML', {
    get: () => htmlWrites.at(-1) || '',
    set: (html) => htmlWrites.push(html),
  });
  const workspaceRefreshBtn = { disabled: false, textContent: '새로고침', attributes: {}, setAttribute(key, value) { this.attributes[key] = value; } };
  const els = {
    workspaceContent,
    workspaceRefreshBtn,
    workspaceDataBadge: { innerHTML: '' },
    workspaceTabBriefDot: { hidden: true },
    workspaceModalEyebrow: { textContent: '' },
    workspaceModalTitle: { textContent: '' },
    workspaceTabs: { hidden: false, querySelectorAll: () => [] },
  };
  const runtime = { state, els, conversationsStorageKeyBase: 'chats', activeConversationStorageKeyBase: 'active',
    maxStoredConversations: 20, conversationSyncTimers: new Map(), apiFetch,
    ...core, scopedStorageKey: (base, uid) => `${base}:${uid}`,
    createConversationRecord: () => ({ id: 'new', messages: [] }), WORKSPACE_ENDPOINTS: { personal: '/personal', teamBrief: '/team-brief', teamBriefRead: '/team-brief/read', teamBriefRefresh: '/team-brief/refresh' } };
  const modules = {
    './runtime.js': runtime,
    './chat.js': { renderMessages() {}, escapeHtml: String, autoResizeInput() {} },
    './events.js': { cancelMessageEdit() {} },
    './motion.js': { captureLayout: () => new Map(), animateLayoutChange() {} },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
    './data_badge.js': {
      dataBadgeState: ({ source, complete = true, refreshing = false, loading = false } = {}) => (loading ? 'loading' : source === 'error' ? 'error' : refreshing ? 'refreshing' : source === 'snapshot' ? 'snapshot' : complete === false ? 'partial' : 'fresh'),
      formatBadgeTime: (value) => (value ? '오후 2:02' : ''),
      mantisBadgeDetails: (state) => ({ source: { name: 'Mantis', tone: 'ok', text: state }, note: '', retry: ['partial', 'snapshot', 'error'].includes(state) }),
      renderDataBadge: (slot, options) => { if (slot) { slot.badge = options; badges.push(options); } },
    },
  };
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/workspace.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    if (specifier.startsWith('./ui_format.js')) return new vm.SourceTextModule(
      fs.readFileSync(path.join(__dirname, '../backend/static/modules/ui_format.js'), 'utf8'), { context });
    const exports = modules[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  return { state, storage, timers, els, htmlWrites, badges, api: module.namespace };
}
const message = (content) => ({ role: 'user', content });
const json = (value) => JSON.parse(JSON.stringify(value));

test('playbook refresh failure preserves cached entries and exposes retry', async () => {
  const h = await harness(async () => ({ ok: false, json: async () => ({ detail: '연결 실패' }) }));
  h.state.playbooks = [{ id: 'cached', title: '저장된 절차', status: 'published', tags: [] }];
  await assert.rejects(h.api.loadPlaybooks(true), /연결 실패/);
  assert.equal(h.state.playbooks[0].id, 'cached');
  assert.equal(h.state.playbooksLoading, false);
  assert.match(h.api.renderPlaybooks(), /저장된 절차/);
  assert.match(h.api.renderPlaybooks(), /data-playbooks-retry/);
});

test('partial personal counts identify lower bounds and never claim all results', async () => {
  const h = await harness(async () => ({ ok: true }));
  const html = h.api.renderPersonalOverview({ user: { mantisUsername: 'user-a' }, summary: {open: 1, overdue: 0, aging: 0}, mantis: {search_complete: false, issues: [{id: 1, title: '확인된 업무'}]} });
  assert.match(html, /건 이상/);
  assert.doesNotMatch(html, /건 모두 표시|모두 기한 내|장기 업무 없음/);
});

test('favorite search keeps saved conversations and filters title and question', async () => {
  const h = await harness(async () => ({ ok: true }));
  h.state.conversations = [
    {id:'a', favorite:true, title:'주간 보고', messages:[message('결제 문제 확인')]},
    {id:'b', favorite:true, title:'접수 현황', messages:[message('로그인 문제')]},
  ];
  h.state.favoriteQuery = '결제';
  const html = h.api.renderFavoriteConversations();
  assert.match(html, /주간 보고/);
  assert.doesNotMatch(html, /접수 현황/);
  assert.equal(h.state.conversations.length, 2);
});

test('mark all notifications keeps failed items unread and reports partial failure', async () => {
  const h = await harness(async (url) => ({ ok: !url.includes('failed') }));
  h.state.myWorkspace = { summary: {}, notifications: [{id:'ok',read:false},{id:'failed',read:false},{id:'old',read:true}] };
  await assert.rejects(h.api.markNotificationsRead(), /1/);
  assert.equal(h.state.myWorkspace.notifications[0].read, true);
  assert.equal(h.state.myWorkspace.notifications[1].read, false);
  assert.equal(h.state.myWorkspace.summary.unreadNotifications, 1);
});

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
  await new Promise(setImmediate);
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
  assert.match(h.els.workspaceRefreshBtn.innerHTML, /불러오는 중…/);
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

test('personal refresh keeps cached list visible and explains refresh failure', async () => {
  let finish;
  const h = await harness(async () => new Promise((resolve) => { finish = resolve; }));
  h.state.myWorkspace = { user: { mantisUsername: 'user-a' }, summary: { open: 1 }, mantis: { status: 'live', search_complete: true, fetchedAt: '2026-10-23T10:00:00Z', issues: [{ id: 1, title: '기존 업무' }] } };
  h.api.renderWorkspace();
  assert.match(h.els.workspaceContent.innerHTML, /기존 업무/);
  const loading = h.api.loadMyWorkspace(true);
  assert.match(h.els.workspaceContent.innerHTML, /기존 업무/);
  assert.equal(h.els.workspaceContent.attributes['aria-busy'], 'true');
  finish({ ok: false, json: async () => ({ detail: '조회 실패' }) });
  await assert.rejects(loading, /조회 실패/);
  assert.match(h.els.workspaceContent.innerHTML, /조회 실패/);
  assert.match(h.els.workspaceContent.innerHTML, /기존 업무/);
  assert.equal(h.els.workspaceContent.attributes['aria-busy'], 'false');
  assert.equal(h.els.workspaceRefreshBtn.disabled, false);
  assert.equal(h.els.workspaceDataBadge.badge.state, 'snapshot');
  assert.equal(h.els.workspaceDataBadge.badge.label, '갱신 실패');
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
  assert.match(h.els.workspaceContent.innerHTML, /일부 이슈만 확인했어요/);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /실시간|전체 건수 미확정|조회 기준/);
  assert.equal(h.els.workspaceDataBadge.badge.state, 'partial');
});

test('personal sync completion refreshes data without hiding the visible list', async () => {
  let calls = 0;
  const h = await harness(async () => {
    calls++;
    return { ok: true, json: async () => ({ user: { mantisUsername: '석지원' }, summary: { open: 0 },
      mantis: { status: 'snapshot', fetchedAt: new Date().toISOString(), search_complete: true, refreshPending: calls === 1, issues: [] } }) };
  });
  await h.api.loadMyWorkspace(true);
  assert.equal(h.els.workspaceDataBadge.badge.state, 'refreshing');
  const timer = [...h.timers.values()].find((entry) => entry.ms === 10000);
  assert.ok(timer);
  const before = h.htmlWrites.length;
  timer.fn();
  await new Promise(setImmediate);
  assert.equal(calls, 2);
  assert.equal(h.els.workspaceDataBadge.badge.state, 'snapshot');
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
  assert.match(h.els.workspaceContent.innerHTML,/workspace-error.*role="alert"/);
  assert.match(h.els.workspaceContent.innerHTML,/data-workspace-retry/);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML,/workspace-empty/);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML,/workspace-summary-grid/);
});

test('weekly report keeps my report and team brief in one tab and clears the new-brief dot when opened', async () => {
  const calls = [];
  const brief = { title: '이번 주 팀 브리핑', viewed: false, summary: { newIssues: 3 }, priorities: [], customerSpikes: [], screenSpikes: [] };
  const h = await harness(async (url, options = {}) => {
    calls.push(`${options.method || 'GET'} ${url}`);
    if (url.startsWith('/personal')) return { ok: true, json: async () => ({ user: { mantisUsername: 'c' }, summary: {}, weeklyBrief: {}, mantis: { status: 'live', search_complete: true, issues: [] } }) };
    if (url === '/team-brief') return { ok: true, json: async () => ({ ...brief }) };
    if (url === '/team-brief/read') return { ok: true, json: async () => ({ read: true }) };
    return { ok: false, json: async () => ({}) };
  });
  await h.api.loadMyWorkspace();
  h.state.workspaceTab = 'brief';
  h.api.renderWorkspace();
  assert.match(h.els.workspaceContent.innerHTML, /data-brief-kind="mine" aria-pressed="true"/);

  // 새 브리핑을 미리 확인하면 탭에 점이 붙지만 읽음으로 저장하지는 않는다.
  h.api.prefetchTeamBrief();
  await new Promise(setImmediate);
  assert.equal(h.els.workspaceTabBriefDot.hidden, false);
  assert.ok(!calls.includes('POST /team-brief/read'));

  await h.api.showTeamBrief();
  assert.match(h.els.workspaceContent.innerHTML, /data-brief-kind="team" aria-pressed="true"/);
  assert.match(h.els.workspaceContent.innerHTML, /이번 주 팀 브리핑/);
  assert.ok(calls.includes('POST /team-brief/read'));
  assert.equal(h.els.workspaceTabBriefDot.hidden, true);
  assert.doesNotMatch(h.els.workspaceContent.innerHTML, /workspace-tab-dot/);
});

test('long history sync uses bounded batches with contiguous indexes and keeps all local text', async () => {
  const calls = [];
  const h = await harness(async (_url, options) => {
    const body = JSON.parse(options.body); calls.push(body);
    return { ok: true, json: async () => ({conversation: {updatedAt: `v${calls.length}`, messageCount: body.startIndex + body.messages.length}}) };
  });
  const conversation = {id:'long', title:'첫 질문', messages:Array.from({length:184},(_,i)=>message(`원문 ${i}`))};
  h.state.conversations = [conversation];
  await h.api.syncConversation(conversation);
  assert.equal(calls.length, 3);
  assert.deepEqual(calls.map((item)=>item.startIndex), [0,80,160]);
  assert.deepEqual(calls.map((item)=>item.truncate), [false,false,true]);
  assert.equal(calls[1].expectedUpdatedAt,'v1');
  assert.equal(calls.flatMap((item)=>item.messages).length,184);
  assert.equal(conversation.messages[0].content,'원문 0');
  assert.equal(conversation.syncPending,false);
});

test('a paged conversation edits only its loaded tail and preserves the saved prefix', async () => {
  let request;
  const h=await harness(async (_url,options)=>{
    request=JSON.parse(options.body);
    return {ok:true,json:async()=>({conversation:{updatedAt:'v2',messageCount:124}})};
  });
  const conversation={id:'long',title:'첫 질문',messages:Array.from({length:60},(_,i)=>message(`원문 ${i+64}`)),messageOffset:64,serverUpdatedAt:'v1'};
  h.state.conversations=[conversation];
  await h.api.syncConversation(conversation);
  assert.equal(request.startIndex,64);
  assert.equal(request.messages.length,60);
  assert.equal(conversation.messageCount,124);
});

test('loading an earlier page prepends text without dropping recent local messages', async () => {
  const h=await harness(async (url)=>({ok:true,json:async()=>({conversation:{id:'long',updatedAt:'v1',messageOffset:0,messageCount:122,messages:Array.from({length:60},(_,i)=>message(`원문 ${i}`))}})}));
  const conversation={id:'long',title:'첫 질문',messages:Array.from({length:62},(_,i)=>message(`원문 ${i+60}`)),messageOffset:60,serverUpdatedAt:'v1'};
  h.state.conversations=[conversation];h.state.activeConversationId='long';h.state.messages=[...conversation.messages];
  await h.api.loadEarlierMessages();
  assert.equal(conversation.messageOffset,0);
  assert.equal(h.state.messages.length,122);
  assert.equal(h.state.messages[0].content,'원문 0');
  assert.equal(h.state.messages.at(-1).content,'원문 121');
});
