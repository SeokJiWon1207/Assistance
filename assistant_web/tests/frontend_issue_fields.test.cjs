const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function loadParser(name) {
  const context = vm.createContext({});
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules', name), 'utf8');
  const exportsByImport = new Map();
  for (const match of source.matchAll(/import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["']/g)) {
    exportsByImport.set(match[2], match[1].split(',').map(value => value.trim()).filter(Boolean));
  }
  const module = new vm.SourceTextModule(source, { context });
  await module.link(async specifier => {
    if (specifier.startsWith('./issue_text.js')) {
      const dependency = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/issue_text.js'), 'utf8'), { context });
      await dependency.link(() => {});
      return dependency;
    }
    const names = [...new Set([...(exportsByImport.get(specifier) || []),
      ...(specifier.startsWith('./runtime.js') ? ['automaticSourceTools'] : [])])];
    return new vm.SyntheticModule(names, function () {
      for (const name of names) this.setExport(name,
        name === 'state' ? { sourceMap: new Map() }
          : name === 'cleanMarkdown' ? value => value.replace(/\*\*|`/g, '') : () => '');
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

const encodedTitle = '"첫 줄\\r\\n문자 \\u007c \\u001c ID: 3 \\u002a\\u002a강조\\u002a\\u002a \\u0060코드\\u0060 C:\\\\temp"';
const title = '첫 줄\r\n문자 | \x1c ID: 3 **강조** `코드` C:\\temp';

test('issue list parser restores the full literal title without source attachments', async () => {
  const parser = await loadParser('chat_rendering.js');
  const issue = parser.parseIssueSummaryLine(`1. **ID**: 90001 | **프로젝트**: 고객 | **제목**: ${encodedTitle} | **상태**: 확인된 이슈 | **담당자**: 검증담당 | **등록일**: 2026-10-01`);
  assert.equal(issue.id, '90001');
  assert.equal(issue.title, title);
  assert.equal(issue.handler, '검증담당');
  assert.equal(issue.createdAt, '2026-10-01');
});

test('Markdown table parser restores quoted source values and keeps the column count', async () => {
  const parser = await loadParser('notion.js');
  const cells = parser.splitMarkdownTableRow(`| 90001 | 고객 | ${encodedTitle} | 확인된 이슈 | 검증담당 | 2026-10-01 |`);
  assert.equal(cells.length, 6);
  assert.equal(cells[2], title);
});
