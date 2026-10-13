const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const core = require('../backend/static/frontend_core.js');

test('category removal on an older answer uses that answer history and audit', async () => {
  let body;
  const selected = [{ role: 'user', content: '10월 NH MTS 분류=조건검색 전체 목록' },
    { role: 'assistant', content: '원문 목록', auditId: 'selected-audit' }];
  const state = { messages: [...selected, { role: 'user', content: '9월 KB HTS 분류=차트 전체 목록' },
    { role: 'assistant', content: '다른 목록', auditId: 'latest-audit' },
    { role: 'user', content: '분류 조건 빼줘' }] };
  const dependencies = {
    './runtime.js': { state, els: { input: { focus() {} } }, buildChatMessages: core.buildChatMessages, parseSseBuffer: core.parseSseBuffer,
      apiFetch: async (_url, options) => {
        body = JSON.parse(options.body);
        const frames = [new TextEncoder().encode('data: {"type":"result","data":{"content":"선택한 범위의 목록"}}\n\n')];
        return { ok: true, body: { getReader: () => ({ read: async () => frames.length
          ? { done: false, value: frames.shift() } : { done: true } }) } };
      } },
    './workspace.js': { persistConversations() {} },
  };
  const context = vm.createContext({ AbortController, TextDecoder, Uint8Array });
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,
    '../backend/static/modules/chat_controller.js'), 'utf8'), { context });
  await module.link((specifier) => {
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [name, value] of Object.entries(values)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.configureChatController({ ensureModels: async () => {},
    selectedModel: () => ({ id: 'fixture', status: 'active' }), statusLabel: String,
    generationPayload: () => ({}), abComparisonEnabled: () => false,
    sourceToolsPayload: () => ({}), renderMessages() {}, setBusy() {} });
  await module.namespace.requestAssistant('분류 조건 빼줘', '', '', selected);
  assert.equal(body.contextAuditId, 'selected-audit');
  assert.deepEqual(body.messages.map(m => m.content), [...selected.map(m => m.content), '분류 조건 빼줘']);
});

test('stream reset removes failed model text before replacement deltas and final save', async () => {
  const state = { messages: [{ role: 'user', content: '질문' }] };
  const snapshots = [];
  const saved = [];
  const frames = [
    { type: 'delta', content: '실패한 모델의 부분 답변' },
    { type: 'reset' },
    { type: 'status', message: '재연결 중', stage: 'generation' },
    { type: 'delta', content: '대체 모델 답변' },
    { type: 'result', data: { content: '대체 모델 답변', auditId: 'new-audit' } },
  ];
  let releaseModels;
  let modelsReady = false;
  const modelPreparation = new Promise((resolve) => { releaseModels = () => { modelsReady = true; resolve(); }; });
  const context = vm.createContext({ AbortController, TextDecoder, Uint8Array });
  const apiFetch = async () => ({ ok: true, body: { getReader: () => ({
    read: async () => frames.length
      ? { done: false, value: new TextEncoder().encode(`data: ${JSON.stringify(frames.shift())}\n\n`) }
      : { done: true },
  }) } });
  const dependencies = {
    './runtime.js': { state, els: { input: { focus() {} } }, apiFetch,
      buildChatMessages: core.buildChatMessages, parseSseBuffer: core.parseSseBuffer },
    './workspace.js': { persistConversations: () => saved.push(JSON.parse(JSON.stringify(state.messages))) },
  };
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/chat_controller.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const exports = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(exports), function () {
      for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
    }, { context });
  });
  await module.evaluate();
  module.namespace.configureChatController({
    ensureModels: () => modelPreparation,
    selectedModel: () => modelsReady ? ({ id: 'test', status: 'active' }) : null, statusLabel: String,
    generationPayload: () => ({}), abComparisonEnabled: () => false, sourceToolsPayload: () => ({}),
    renderMessages: () => snapshots.push({ ...state.messages.at(-1) }),
    setBusy: (busy) => { state.busy = busy; },
  });
  const request = module.namespace.requestAssistant('질문');
  assert.equal(state.messages.at(-1).loading, true);
  releaseModels();
  await request;
  const oldAnswerIndex = snapshots.findIndex((message) => message.content === '실패한 모델의 부분 답변');
  assert.ok(oldAnswerIndex >= 0);
  assert.equal(snapshots[oldAnswerIndex + 1].content, '');
  assert.equal(snapshots[oldAnswerIndex + 1].loading, true);
  assert.equal(snapshots[oldAnswerIndex + 1].streaming, false);
  assert.ok(snapshots.some((message) => message.content === '대체 모델 답변' && message.streaming));
  assert.ok(snapshots.every((message) => !message.content.includes('부분 답변대체')));
  assert.equal(saved[0].at(-1).content, '대체 모델 답변');
  assert.equal(saved[0].at(-1).auditId, 'new-audit');
  assert.equal(state.busy, false);
});
