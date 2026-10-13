const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('category criterion keeps the verified removal payload without duplicate chips', async () => {
  const module = new vm.SourceTextModule(fs.readFileSync(
    path.join(__dirname, '../backend/static/modules/search_conditions.js'), 'utf8'),
  { context: vm.createContext({}) });
  await module.link(() => { throw new Error('unexpected import'); });
  await module.evaluate();
  const category = { id: 'category', label: '분류', value: '조건검색', values: ['조건검색'] };
  const result = module.namespace.answerSearchPresentation({
    verification: { constraints: [category] },
    content: '검색 기준: 분류=조건검색, 플랫폼=HTS, 등록일=9월 1일~30일\n원문 목록',
  });
  assert.equal(result.conditions.length, 3);
  assert.equal(result.conditions[0].label, '분류');
  assert.equal(result.conditions[0].value, '조건검색');
  assert.equal(result.conditions[0].removeCondition, category);
  assert.equal(result.content.trim(), '원문 목록');
});
