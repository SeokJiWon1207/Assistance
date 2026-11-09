const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function load() {
  const context = vm.createContext({});
  const module = new vm.SourceTextModule(
    fs.readFileSync(path.join(__dirname, '../backend/static/modules/answer_details_block.js'), 'utf8'), { context });
  await module.link(() => { throw new Error('접기 블록 모듈은 다른 모듈을 가져오지 않아야 합니다.'); });
  await module.evaluate();
  return module.namespace;
}

const plain = (value) => JSON.parse(JSON.stringify(value));
// reports/quote_rendering.py가 긴 근거를 접어 보내는 실제 형식
const answer = [
  '**핵심 근거**', '', '> 원문 한 줄', '',
  '<details>', '<summary>전체 이력과 원문 근거 보기</summary>', '',
  '**처리 이력**', '- 11/04 접수', '- 11/05 수정', '',
  '</details>', '', '끝맺음 문단',
];

test('server details lines become one folded block with the summary as its label', async () => {
  const { parseDetailsBlock } = await load();
  const block = plain(parseDetailsBlock(answer, 4));
  assert.equal(block.summary, '전체 이력과 원문 근거 보기');
  assert.deepEqual(block.innerLines, ['', '**처리 이력**', '- 11/04 접수', '- 11/05 수정', '']);
  assert.equal(answer[block.nextIndex], '');
  assert.equal(parseDetailsBlock(answer, 0), null);
});

test('only the exact line format is folded; other HTML and unclosed blocks stay as text', async () => {
  const { parseDetailsBlock } = await load();
  assert.equal(parseDetailsBlock(['<details>', '본문', '</details>'], 0), null, '요약 줄이 없으면 접지 않는다');
  assert.equal(parseDetailsBlock(['<details>', '<summary>보기</summary>', '닫는 줄 없음'], 0), null);
  assert.equal(parseDetailsBlock(['<details open>', '<summary>보기</summary>', '</details>'], 0), null);
  assert.equal(parseDetailsBlock(['<details>', '<summary><img src=x onerror=1></summary>', '</details>'], 0), null);
  assert.equal(parseDetailsBlock(['문장 안의 <details> 태그'], 0), null);
  const nested = ['<details>', '<summary>바깥</summary>', '<details>', '<summary>안쪽</summary>', 'a', '</details>', 'b', '</details>'];
  const block = plain(parseDetailsBlock(nested, 0));
  assert.equal(block.summary, '바깥');
  assert.equal(block.nextIndex, nested.length);
  assert.deepEqual(block.innerLines.slice(-1), ['b']);
});

test('copying an answer drops the folding lines and keeps the summary as a bold heading', async () => {
  const { stripDetailsMarkup } = await load();
  const copied = stripDetailsMarkup(answer.join('\n'));
  assert.doesNotMatch(copied, /<\/?details>|<summary>/);
  assert.match(copied, /\*\*전체 이력과 원문 근거 보기\*\*\n\n\*\*처리 이력\*\*/);
  assert.match(copied, /- 11\/05 수정\n\n끝맺음 문단$/);
  assert.equal(stripDetailsMarkup('그냥 문장'), '그냥 문장');
});

test('the answer renderer folds the block with DOM nodes instead of printing the tags', () => {
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/chat_rendering.js'), 'utf8');
  assert.match(source, /const folded = parseDetailsBlock\(lines, index\);/);
  assert.match(source, /foldedDetails\(folded, \(text\) => renderAssistantContent\(text, sources, messageIndex\)\)/);
  const block = fs.readFileSync(path.join(__dirname, '../backend/static/modules/answer_details_block.js'), 'utf8');
  assert.doesNotMatch(block, /innerHTML/);
  const events = fs.readFileSync(path.join(__dirname, '../backend/static/modules/events.js'), 'utf8');
  assert.match(events, /customerAnswerText\(stripDetailsMarkup\(text \|\| ""\)\)/);
});
