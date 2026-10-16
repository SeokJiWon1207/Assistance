const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.className = '';
    this._text = '';
    this.classList = { add: (...names) => { this.className += ` ${names.join(' ')}`; } };
  }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  replaceChildren(...children) { this.children = []; this._text = ''; this.append(...children); }
  setAttribute(name, value) { this[name] = value; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) {
    assert.equal(value, '', 'The quote renderer must not interpret any source as HTML');
    this.children = [];
    this._text = '';
  }
}

function descendants(element, tagName) {
  return element.children.flatMap(child => [
    ...(child.tagName === tagName ? [child] : []), ...descendants(child, tagName),
  ]);
}

async function rendering() {
  const directory = path.join(__dirname, '../backend/static/modules');
  const document = {
    createElement: tagName => new Element(tagName),
    createTextNode: text => { const node = new Element('#text'); node.textContent = text; return node; },
  };
  const context = vm.createContext({ document });
  const modules = new Map();
  const imports = new Map();
  const state = { sourceMap: new Map() };
  for (const filename of ['chat_rendering.js', 'chat_source_quote.js', 'notion.js', 'issue_text.js']) {
    const source = fs.readFileSync(path.join(directory, filename), 'utf8');
    modules.set(filename, new vm.SourceTextModule(source, { context }));
    for (const match of source.matchAll(/import\s*\{([^}]+)\}\s*from\s*["']([^"']+)["']/g)) {
      const filename = match[2].split('?')[0].replace('./', '');
      const names = imports.get(filename) || new Set();
      match[1].split(',').map(value => value.trim()).filter(Boolean).forEach(name => names.add(name));
      imports.set(filename, names);
    }
  }
  imports.get('runtime.js').add('automaticSourceTools');
  const module = modules.get('chat_rendering.js');
  await module.link(specifier => {
    const filename = specifier.split('?')[0].replace('./', '');
    if (!modules.has(filename)) {
      const names = [...imports.get(filename)];
      modules.set(filename, new vm.SyntheticModule(names, function () {
        names.forEach(name => this.setExport(name, name === 'state' ? state : () => ''));
      }, { context }));
    }
    return modules.get(filename);
  });
  await module.evaluate();
  return { api: module.namespace, state };
}

test('quoted fenced source stays literal including HTML, Markdown, issue IDs and whitespace', async () => {
  const { api, state } = await rendering();
  const source = '<img src=x onerror=alert(1)>\n<script>alert(2)</script>\n**완료 아님** `a_b` #90002\n  들여쓰기  \n\n> 중첩 인용';
  const content = `### 원인\n\n**#90001** 실제 대상\n\n> \`\`\`text\n${source.split('\n').map(line => `> ${line}`).join('\n')}\n> \`\`\``;
  const targetSource = { issue_id: 90001, type: 'mantis' };
  const rendered = api.renderAssistantContent(content, [targetSource, { issue_id: 90002, type: 'mantis' }], 3);
  const quotes = descendants(rendered, 'blockquote');
  assert.equal(quotes.length, 1);
  assert.equal(descendants(quotes[0], 'pre').length, 1);
  assert.equal(descendants(quotes[0], 'code')[0].textContent, source);
  assert.equal(descendants(quotes[0], 'button').length, 0);
  assert.equal(descendants(quotes[0], 'strong').length, 0);
  assert.equal(descendants(quotes[0], 'img').length, 0);
  assert.equal(descendants(quotes[0], 'script').length, 0);
  const links = descendants(rendered, 'button');
  assert.equal(links.length, 1);
  assert.equal(links[0].textContent, '#90001');
  assert.equal(state.sourceMap.get(links[0].dataset.sourceKey), targetSource);
});

test('long quote fences preserve nested backticks and multiple source paragraphs', async () => {
  const { api } = await rendering();
  const content = '> ````text\r\n> ```js\r\n> const value = `원문`;\r\n> ```\r\n> \r\n> **그대로**\r\n> ````\r\n\r\n끝 문장';
  const rendered = api.renderAssistantContent(content);
  assert.equal(descendants(rendered, 'code')[0].textContent, '```js\nconst value = `원문`;\n```\n\n**그대로**');
  assert.equal(descendants(rendered, 'blockquote').length, 1);
  assert.equal(rendered.children.at(-1).textContent, '끝 문장');
});

test('source table and issue-list syntax cannot replace the answer with a parsed table', async () => {
  const { api } = await rendering();
  const source = '| ID | 제목 |\n|---|---|\n| 90002 | 원문 표 |\n1. **ID**: 90002 | **제목**: 원문 목록';
  const content = `원인\n\n> \`\`\`text\n${source.split('\n').map(line => `> ${line}`).join('\n')}\n> \`\`\`\n\n원문 다음 설명`;
  const rendered = api.renderAssistantContent(content, [{ issue_id: 90002 }]);
  assert.equal(descendants(rendered, 'table').length, 0);
  assert.equal(descendants(rendered, 'code')[0].textContent, source);
  assert.match(rendered.textContent, /원문 다음 설명$/);
});

test('unclosed quoted fence keeps quoted content literal without consuming following answer text', async () => {
  const { api } = await rendering();
  const rendered = api.renderAssistantContent('> ```text\n> **원문**\n\n### 다음 항목\n설명');
  assert.equal(descendants(rendered, 'code')[0].textContent, '**원문**');
  assert.equal(descendants(rendered, 'h4')[0].textContent, '다음 항목');
});

test('ordinary aggregate tables and source-less bold references retain their existing behavior', async () => {
  const { api } = await rendering();
  const table = api.renderAssistantContent('| 상태 | 건수 |\n|---|---|\n| 진행 | 3 |');
  assert.equal(descendants(table, 'table').length, 1);
  assert.equal(descendants(table, 'td')[1].textContent, '3');
  const ordinary = api.renderAssistantContent('**#90001** 조회되지 않은 이슈');
  assert.equal(descendants(ordinary, 'button').length, 0);
  assert.equal(descendants(ordinary, 'strong')[0].textContent, '#90001');
});

test('quoted source does not block an ordinary table outside it or a real short issue reference', async () => {
  const { api, state } = await rendering();
  const source = { issue_id: 9 };
  const rendered = api.renderAssistantContent('**#9** 근거 제목\n\n> ```text\n> |원문|\n> ```\n\n| 상태 | 건수 |\n|---|---|\n| 진행 | 3 |', [source]);
  assert.equal(descendants(rendered, 'table').length, 1);
  assert.equal(descendants(rendered, 'code')[0].textContent, '|원문|');
  const refs = descendants(rendered, 'button');
  assert.equal(refs.length, 1);
  assert.equal(refs[0].textContent, '#9');
  assert.equal(state.sourceMap.get(refs[0].dataset.sourceKey), source);
});

test('only the history section of quoted answers starts collapsed and later sections stay visible', async () => {
  const { api } = await rendering();
  const rendered = api.renderAssistantContent('### 원인\n원인 설명\n\n### 진행 이력\n\n**#90001** 제목\n\n> ```text\n> 1차 수정 요청\n> ### 조치\n> 아직 완료 아님\n> ```\n\n### 기술 내용\n\n> ```text\n> FID = 42\n> ```', [{ issue_id: 90001 }]);
  const details = descendants(rendered, 'details');
  assert.equal(details.length, 1);
  assert.ok(!details[0].open);
  assert.equal(details[0].children[0].tagName, 'summary');
  assert.equal(details[0].children[0].textContent, '진행 이력 원문 보기');
  assert.equal(descendants(details[0], 'code')[0].textContent, '1차 수정 요청\n### 조치\n아직 완료 아님');
  assert.equal(descendants(details[0], 'button')[0].textContent, '#90001');
  assert.doesNotMatch(details[0].textContent, /FID = 42/);
  assert.ok(rendered.children.some(child => child.tagName === 'h4' && child.textContent === '원인'));
  assert.ok(rendered.children.some(child => child.tagName === 'h4' && child.textContent === '기술 내용'));
  assert.equal(rendered.children.at(-1).tagName, 'blockquote');
});

test('ordinary history headings without source quotes are not collapsed', async () => {
  const { api } = await rendering();
  const rendered = api.renderAssistantContent('### 진행 이력\n일반 설명\n\n| 상태 | 건수 |\n|---|---|\n| 진행 | 3 |');
  assert.equal(descendants(rendered, 'details').length, 0);
  assert.equal(descendants(rendered, 'h4')[0].textContent, '진행 이력');
  assert.equal(descendants(rendered, 'table').length, 1);
});

test('registration and history sources collapse separately while cause remains visible', async () => {
  const { api } = await rendering();
  const rendered = api.renderAssistantContent('### 등록 내용\n\n> ```text\n> 최초 접수 원문\n> ```\n\n### 원인\n\n> ```text\n> 확인된 원인\n> ```\n\n### 진행 이력\n\n> ```text\n> 마지막 보류 기록\n> ```');
  const details = descendants(rendered, 'details');
  assert.equal(details.length, 2);
  assert.ok(details.every(element => !element.open));
  assert.equal(details[0].children[0].textContent, '등록 내용 원문 보기');
  assert.equal(descendants(details[0], 'code')[0].textContent, '최초 접수 원문');
  assert.equal(details[1].children[0].textContent, '진행 이력 원문 보기');
  assert.equal(descendants(details[1], 'code')[0].textContent, '마지막 보류 기록');
  const visibleQuotes = rendered.children.filter(child => child.tagName === 'blockquote');
  assert.equal(visibleQuotes.length, 1);
  assert.equal(descendants(visibleQuotes[0], 'code')[0].textContent, '확인된 원인');
  const ordinary = api.renderAssistantContent('### 등록 내용\n일반 설명');
  assert.equal(descendants(ordinary, 'details').length, 0);
  assert.equal(descendants(ordinary, 'h4')[0].textContent, '등록 내용');
});

test('additional topic evidence starts collapsed while the cited key quote stays visible', async () => {
  const { api } = await rendering();
  const rendered = api.renderAssistantContent('### 핵심 결론과 근거\n\n### 조치\n\n**#90001** 제목\n\n노트 7\n\n> ```text\n> 개발 완료는 아니며 재검토 요청.\n> ```\n\n### 조치 전체 근거\n\n노트 8\n\n> ```text\n> 후속 조치 예정.\n> ```', [{ issue_id: 90001 }]);
  const visibleQuotes = rendered.children.filter(child => child.tagName === 'blockquote');
  assert.equal(visibleQuotes.length, 1);
  assert.equal(descendants(visibleQuotes[0], 'code')[0].textContent, '개발 완료는 아니며 재검토 요청.');
  const detail = descendants(rendered, 'details');
  assert.equal(detail.length, 1);
  assert.equal(detail[0].children[0].textContent, '조치 전체 근거 보기');
  assert.equal(descendants(detail[0], 'code')[0].textContent, '후속 조치 예정.');
});

test('long source context is entirely available behind a disclosure', async () => {
  const { api } = await rendering();
  const source = `승인 조건을 확인해야 합니다. ${'검토 중입니다. '.repeat(100)}아직 승인 완료는 아닙니다.`;
  const content = `### 조치\n\n노트 7\n\n> \`\`\`text\n> ${source}\n> \`\`\``;
  const rendered = api.renderAssistantContent(content);
  assert.equal(rendered.children.filter(child => child.tagName === 'blockquote').length, 0);
  const detail = descendants(rendered, 'details');
  assert.equal(detail.length, 1);
  assert.ok(!detail[0].open);
  assert.equal(detail[0].children[0].textContent, '긴 인용 원문 보기');
  assert.equal(descendants(detail[0], 'code')[0].textContent, source);
});
