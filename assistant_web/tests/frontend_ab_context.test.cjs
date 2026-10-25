const test = require('node:test');
const assert = require('node:assert/strict');
const core = require('../backend/static/frontend_core.js');
const fs = require('node:fs');

test('selecting B changes the conversation without mutating its saved snapshot', async () => {
  const source = fs.readFileSync(require.resolve('../backend/static/modules/chat_ab_context.js'), 'utf8');
  const { selectAnswerContext } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
  const saved = [{ role: 'assistant', content: 'A', auditId: 'owned',
    abTest: { enabled: true, variantB: { status: 'ok', content: 'B' } } }];
  const selected = selectAnswerContext([...saved], 0, 'B');
  assert.equal(selected[0].followupVariant, 'B');
  assert.equal(saved[0].followupVariant, undefined);
  assert.equal(core.conversationContentChanged(saved, selected), true);
  assert.equal(selectAnswerContext(saved, 0, 'C'), saved);
});

test('B selection survives saving and supplies B content to later messages', () => {
  const messages = [{ role: 'user', content: '원래 질문' }, {
    role: 'assistant', content: 'A 답변', auditId: 'owned', followupVariant: 'B',
    abTest: { enabled: true, variantB: { status: 'ok', content: 'B 답변' } },
  }, { role: 'user', content: '그중 첫 번째' }];
  const stored = core.storageSafeMessages(messages);
  assert.equal(stored[1].followupVariant, 'B');
  assert.equal(core.buildChatMessages(stored, '그중 첫 번째')[1].content, 'B 답변');
  assert.equal(stored[1].content, 'A 답변');
  stored[1].followupVariant = 'A';
  assert.equal(core.buildChatMessages(stored, '그중 첫 번째')[1].content, 'A 답변');
});

test('failed B does not become message history when a saved selection is stale', () => {
  const messages = [{ role: 'assistant', content: 'A', followupVariant: 'B',
    abTest: { variantB: { status: 'blocked', content: '차단 응답' } } }, { role: 'user', content: '후속' }];
  assert.equal(core.buildChatMessages(messages, '후속')[0].content, 'A');
});
