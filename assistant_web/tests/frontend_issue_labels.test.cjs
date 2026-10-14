const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function rendering() {
  const directory = path.join(__dirname, '../backend/static/modules');
  const element = () => ({ children: [], dataset: {}, classList: { add() {} },
    appendChild(child) { this.children.push(child); }, append(...children) { this.children.push(...children); },
    setAttribute() {} });
  const context = vm.createContext({ document: { createElement: element } });
  const detail = fs.readFileSync(path.join(directory, 'chat_source_detail.js'), 'utf8');
  vm.runInContext(detail.match(/function displayIssueStatus\(status\) \{[\s\S]*?\n\}/)[0], context);
  const source = fs.readFileSync(path.join(directory, 'chat_rendering.js'), 'utf8');
  const module = new vm.SourceTextModule(source, { context });
  await module.link((specifier) => {
    const imports = [...source.matchAll(/import\s*\{([^}]+)\}\s*from\s*"([^"]+)"/g)]
      .find((match) => match[2] === specifier)[1].split(',').map((name) => name.trim()).filter(Boolean);
    return new vm.SyntheticModule(imports, function () {
      for (const name of imports) this.setExport(name,
        name === 'state' ? { sourceMap: new Map() } : name === 'displayIssueStatus' ? context.displayIssueStatus : () => '');
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

test('issue table displays original labels even when source codes override answer text', async () => {
  const api = await rendering();
  const table = api.renderIssueSummaryList([{ id: 31582, status: '종료된 이슈' }], [
    { issue_id: 31582, status: 'closed', status_label: '원본 종료 표시', priority: 'normal', priority_label: '보통' },
  ]).children[0];
  const cells = table.children[1].children[0].children;
  assert.equal(cells[3].children[0].textContent, '원본 종료 표시');
  assert.equal(cells[4].textContent, '보통');
});

test('old sources translate known codes and never substitute issue type for priority', async () => {
  const api = await rendering();
  assert.equal(api.issuePriorityLabel({}, { priority: 'high' }), '높음');
  assert.equal(api.issuePriorityLabel({}, { severity: 'crash', severity_label: '추가' }), '-');
  assert.equal(api.issuePriorityLabel({}, { priority: 'normal', detail: { priority_label: '원본 우선순위' } }), '원본 우선순위');
  const table = api.renderIssueSummaryList([{ id: 1 }], [{ issue_id: 1, status: 'confirmed' }]).children[0];
  assert.equal(table.children[1].children[0].children[3].children[0].textContent, '확인된 이슈');
});
