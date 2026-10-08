const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function loadScope() {
  const document = {
    createElement: (tagName) => ({ tagName, children: [], setAttribute() {}, appendChild(child) { this.children.push(child); } }),
  };
  const context = vm.createContext({ document });
  const module = new vm.SourceTextModule(fs.readFileSync(
    path.join(__dirname, '../backend/static/modules/answer_scope.js'), 'utf8',
  ), { context });
  await module.link(() => new vm.SourceTextModule(fs.readFileSync(
    path.join(__dirname, '../backend/static/modules/ui_format.js'), 'utf8',
  ), { context }));
  await module.evaluate();
  return module.namespace;
}

test('scope is omitted for old answers and retains partial lookup and data date', async () => {
  const scope = await loadScope();
  assert.equal(scope.renderAnswerScope(undefined), null);
  const row = { source: 'Mantis', basis: '저장본', coverage: '부분 조회', asOf: '기준일 미확인', warning: true };
  assert.match(scope.answerScopeLabel(row), /Mantis · 저장본 · 일부만 확인 · 데이터 기준 기준일 미확인/);
  assert.equal(scope.renderAnswerScope([row]).children[0].className, 'answer-data-scope-row is-warning');
});

test('external metadata and warnings are rendered as text', async () => {
  const scope = await loadScope();
  const section = scope.renderAnswerScope([{ source: '<img onerror=alert(1)>', coverage: '선택 범위 조회 완료', databaseCount: 33, notes: ['<script>본문 미확인</script>'], warning: true }]);
  assert.equal(section.children[0].children[0].textContent, '<img onerror=alert(1)> · 선택 범위 확인 완료 · 33개 DB');
  assert.equal(section.children[0].children[1].textContent, '<script>본문 미확인</script>');
});

test('successful scope details start collapsed while incomplete lookup warnings remain visible', async () => {
  const scope = await loadScope();
  const normal = scope.renderAnswerScope([{ source: 'Mantis', basis: '저장본', coverage: '선택 범위 조회 완료', asOf: '2026-10-07T06:00:00Z' }]);
  assert.equal(normal.tagName, 'details');
  assert.ok(!normal.open);
  assert.match(normal.children[0].textContent, /데이터 기준.*Mantis/);
  assert.match(normal.children[1].children[0].textContent, /저장본.*확인 완료/);
  const warning = scope.renderAnswerScope([{ source: 'Notion', coverage: '부분 조회', warning: true, notes: ['본문 일부 미확인'] }]);
  assert.equal(warning.tagName, 'section');
  assert.match(warning.children[0].children[1].textContent, /본문 일부 미확인/);
});
