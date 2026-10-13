const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const core = require('../backend/static/frontend_core.js');

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
