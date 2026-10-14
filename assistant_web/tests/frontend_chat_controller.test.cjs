const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const core = require('../backend/static/frontend_core.js');

test('HTTP validation errors display messages without coercing objects or echoing submitted input', async () => {
  for (const [status, detail, expected] of [
    [422, [{ msg: '메시지 전체 길이는 120,000자를 넘을 수 없습니다.', input: 'private issue body' }], '메시지 전체 길이는 120,000자를 넘을 수 없습니다.'],
    [422, { msg: '잘못된 요청입니다.', input: 'private issue body' }, '잘못된 요청입니다.'],
    [429, '요청이 너무 많습니다.', '요청이 너무 많습니다. (5초 후 다시 시도)'],
    [500, { unexpected: 'private issue body' }, '모델 응답 중 오류가 발생했습니다.'],
  ]) {
    const state = { messages: [{ role: 'user', content: '나머지도 보여줘' }, { role: 'assistant', auditId: 'previous-page', content: '원문 목록' }] };
    const context = vm.createContext({ AbortController, TextDecoder, Uint8Array });
    let sent;
    const dependencies = {
      './runtime.js': { state, els: {}, buildChatMessages: core.buildChatMessages, parseSseBuffer: core.parseSseBuffer,
        apiFetch: async (_url, options) => {
          sent = JSON.parse(options.body);
          return { ok: false, status, headers: { get: () => '5' }, json: async () => ({ detail }) };
        } },
      './workspace.js': { persistConversations() {} },
    };
    const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/chat_controller.js'), 'utf8'), { context });
    await module.link((specifier) => {
      const exports = dependencies[specifier.replace(/\?v=.*$/, '')];
      return new vm.SyntheticModule(Object.keys(exports), function () {
        for (const [name, value] of Object.entries(exports)) this.setExport(name, value);
      }, { context });
    });
    await module.evaluate();
    module.namespace.configureChatController({
      ensureModels: async () => {}, selectedModel: () => ({ id: 'test', status: 'active' }),
      generationPayload: () => ({}), abComparisonEnabled: () => false, sourceToolsPayload: () => ({}),
    });
    await assert.rejects(module.namespace.sendChat('나머지도 보여줘', () => {}), (error) => {
      assert.equal(error.message, expected);
      assert.equal(error.status, status);
      assert.ok(!error.message.includes('private issue body'));
      return true;
    });
    assert.equal(sent.contextAuditId, 'previous-page');
  }
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
