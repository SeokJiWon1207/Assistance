const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const STATIC_DIR = path.join(__dirname, '../backend/static');

async function loadModel() {
  const module = new vm.SourceTextModule(
    fs.readFileSync(path.join(STATIC_DIR, 'modules/command_palette_model.js'), 'utf8'),
    { context: vm.createContext({}) },
  );
  await module.link(() => { throw new Error('명령 팔레트 모델은 다른 모듈을 가져오지 않아야 합니다.'); });
  await module.evaluate();
  return module.namespace;
}

// vm 컨텍스트에서 만든 배열은 프로토타입이 달라 deepEqual 전에 이 영역의 값으로 옮긴다.
const plain = (value) => JSON.parse(JSON.stringify(value));
const keys = (sections) => sections.map((section) => section.key);
const ids = (section) => (section?.items || []).map((item) => item.id);

const views = [
  { view: 'chat', label: '대화 화면으로 이동', icon: 'chat', current: true },
  { view: 'personal', label: '내 업무로 이동', icon: 'clipboard' },
  { view: 'dashboard', label: '팀 대시보드로 이동', icon: 'chart', keywords: '운영 현황' },
  { view: 'admin', label: '관리자 센터로 이동', icon: 'shield', available: false },
];
const conversations = [
  { id: 'active', title: '결제 지연 정리', messages: [{ role: 'user' }] },
  { id: 'empty', title: '새 대화', messages: [] },
  { id: 'server', title: '결제 취소 문의 비교', messages: [], messageCount: 8 },
  { id: 'login', title: '로그인 오류 원인', messages: [{ role: 'user' }] },
];
const templates = [
  { id: 'incident', title: '장애 분류', description: '결제 장애를 원인별로 묶기', category: '장애 대응' },
  { id: 'weekly', title: '주간 보고', description: '이번 주 처리 현황', category: '보고서' },
];
const issues = [
  { id: 1234, title: '결제 승인 응답 지연', status: '할당된 이슈' },
  { id: 88, title: '푸시 중복 발송', status: '신규 이슈' },
];

test('issue numbers are read only when the query is a bare number', async () => {
  const { parseIssueQuery } = await loadModel();
  assert.equal(parseIssueQuery('#1234'), '1234');
  assert.equal(parseIssueQuery(' 0042 '), '42');
  assert.equal(parseIssueQuery('# 77'), '77');
  assert.equal(parseIssueQuery('1234건'), '');
  assert.equal(parseIssueQuery('#12345678'), '');
  assert.equal(parseIssueQuery(''), '');
});

test('an empty query lists recent conversations, a few templates and every visible view', async () => {
  const { commandPaletteSections } = await loadModel();
  const sections = plain(commandPaletteSections({ views, conversations, activeConversationId: 'active', templates, issues }));

  assert.deepEqual(keys(sections), ['conversations', 'templates', 'actions']);
  // 지금 보고 있는 대화와 아직 질문하지 않은 빈 대화는 열 이유가 없다.
  assert.deepEqual(ids(sections[0]), ['conversation-server', 'conversation-login']);
  assert.equal(sections[0].label, '최근 대화');
  assert.deepEqual(ids(sections[2]), ['action-new-chat', 'view-chat', 'view-personal', 'view-dashboard', 'action-theme']);
  assert.equal(sections[2].items.find((item) => item.id === 'view-chat').meta, '지금 화면');
  assert.ok(sections[2].items.every((item) => !('keywords' in item)));
});

test('a bare issue number opens that issue first and still offers to ask about it', async () => {
  const { commandPaletteSections } = await loadModel();
  const known = plain(commandPaletteSections({ query: '#1234', views, conversations, templates, issues }));
  assert.equal(known[0].key, 'issues');
  assert.deepEqual(known[0].items, [{
    id: 'issue-1234', kind: 'issue', issueId: '1234', icon: 'list', label: '#1234 결제 승인 응답 지연', meta: '할당된 이슈',
  }]);
  assert.equal(known.at(-1).key, 'ask');
  assert.equal(known.at(-1).items[0].question, '#1234');

  const unknown = plain(commandPaletteSections({ query: '555', views, issues }));
  assert.equal(unknown[0].items[0].label, '#555 이슈 원문 보기');
  assert.equal(unknown[0].items[0].issueId, '555');
});

test('text queries search issue titles, conversations, templates and view keywords together', async () => {
  const { commandPaletteSections } = await loadModel();
  const sections = plain(commandPaletteSections({ query: '결 제', views, conversations, templates, issues }));
  const byKey = Object.fromEntries(sections.map((section) => [section.key, section]));

  assert.deepEqual(ids(byKey.issues), ['issue-1234']);
  assert.deepEqual(ids(byKey.conversations), ['conversation-active', 'conversation-server']);
  assert.equal(byKey.conversations.label, '대화');
  assert.deepEqual(ids(byKey.templates), ['template-incident']);
  assert.equal(byKey.actions, undefined);
  assert.equal(byKey.ask.items[0].label, '“결 제” 질문하기');

  const viewSearch = plain(commandPaletteSections({ query: '운영', views }));
  assert.deepEqual(ids(viewSearch.find((section) => section.key === 'actions')), ['view-dashboard']);
  // 권한이 없어 숨긴 화면은 이름이 맞아도 나오지 않는다.
  assert.equal(plain(commandPaletteSections({ query: '관리자', views })).find((section) => section.key === 'actions'), undefined);
});

test('each section stops at five results', async () => {
  const { commandPaletteSections, SECTION_LIMIT } = await loadModel();
  const many = Array.from({ length: 9 }, (_, index) => ({ id: `c${index}`, title: `결제 대화 ${index}`, messages: [{}] }));
  const sections = plain(commandPaletteSections({ query: '결제', conversations: many }));
  assert.equal(sections.find((section) => section.key === 'conversations').items.length, SECTION_LIMIT);
});

test('while an answer is streaming, commands that replace the conversation are disabled but navigation is not', async () => {
  const { commandPaletteSections, flattenCommandItems, nextEnabledIndex } = await loadModel();
  const items = plain(flattenCommandItems(commandPaletteSections({ query: '결제', views, conversations, templates, issues, busy: true })));
  const disabled = items.filter((item) => item.disabled).map((item) => item.kind);

  assert.deepEqual([...new Set(disabled)].sort(), ['ask', 'conversation', 'template']);
  assert.ok(!items.find((item) => item.kind === 'issue').disabled);

  const list = [{ disabled: true }, {}, { disabled: true }, {}];
  assert.equal(nextEnabledIndex(list, -1, 1), 1);
  assert.equal(nextEnabledIndex(list, 1, 1), 3);
  assert.equal(nextEnabledIndex(list, 3, 1), 1);
  assert.equal(nextEnabledIndex(list, 1, -1), 3);
  assert.equal(nextEnabledIndex(list, -1, -1), 3);
  assert.equal(nextEnabledIndex([{ disabled: true }], -1, 1), -1);
  assert.equal(nextEnabledIndex([], -1, 1), -1);
});

test('palette icons exist in the sprite and the dialog markup is wired to the module', async () => {
  const { commandPaletteSections, flattenCommandItems } = await loadModel();
  const html = fs.readFileSync(path.join(STATIC_DIR, 'index.html'), 'utf8');
  const paletteJs = fs.readFileSync(path.join(STATIC_DIR, 'modules/command_palette.js'), 'utf8');
  const symbols = new Set([...html.matchAll(/<symbol id="i-([a-z-]+)"/g)].map((match) => match[1]));

  const items = flattenCommandItems(commandPaletteSections({ query: '1', views, conversations, templates, issues }))
    .concat(flattenCommandItems(commandPaletteSections({ views, conversations, templates, issues })));
  const viewIcons = [...paletteJs.matchAll(/icon: "([a-z-]+)"/g)].map((match) => match[1]);
  for (const name of new Set([...items.map((item) => item.icon), ...viewIcons])) {
    assert.ok(symbols.has(name), `스프라이트에 i-${name} 아이콘이 없습니다.`);
  }

  for (const id of ['commandPalette', 'commandPaletteDialog', 'commandPaletteInput', 'commandPaletteList', 'commandPaletteBtn']) {
    assert.ok(html.includes(`id="${id}"`), `${id} 요소가 없습니다.`);
    if (id !== 'commandPaletteBtn') assert.ok(paletteJs.includes(`#${id}`), `${id}를 모듈이 찾지 않습니다.`);
  }
  assert.match(html, /id="commandPaletteDialog" class="command-palette" role="dialog" aria-modal="true"/);
  assert.match(html, /\/static\/command_palette\.css\?v=/);
  // 다른 모듈과 같은 주소로 가져와야 상태 객체가 하나로 유지된다.
  for (const match of paletteJs.matchAll(/from "\.\/(runtime|workspace|dashboard|template_picker)\.js\?v=([^"]+)"/g)) {
    assert.equal(match[2], '20260911-6', `${match[1]}.js를 다른 버전 주소로 가져옵니다.`);
  }
});
