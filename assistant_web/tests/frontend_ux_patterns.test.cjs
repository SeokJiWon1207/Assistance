const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const MODULES = path.join(__dirname, '../backend/static/modules');

// 가져오는 모듈이 없는 순수 모듈만 불러온다. chat_progress.js는 icon만 흉내 낸다.
async function load(name) {
  const context = vm.createContext({});
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(MODULES, name), 'utf8'), { context });
  await module.link(() => new vm.SyntheticModule(['icon'], function () { this.setExport('icon', () => '<svg></svg>'); }, { context }));
  await module.evaluate();
  return module.namespace;
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test('answer elapsed time counts whole seconds, switches to minutes and flags slow answers', async () => {
  const progress = await load('chat_progress.js');
  assert.equal(progress.elapsedSeconds(1000, 10_999), 9);
  assert.equal(progress.elapsedSeconds(5000, 1000), 0);
  assert.equal(progress.elapsedSeconds(undefined, 1000), null);
  assert.equal(progress.elapsedLabel(24), '24초째 작업 중');
  assert.equal(progress.elapsedLabel(75), '1분 15초째 작업 중');
  assert.equal(progress.answerDurationLabel(9), '9초 만에 답변');
  assert.equal(progress.answerDurationLabel(undefined), '');

  const fast = progress.renderElapsed({ startedAt: 1000 }, 6000);
  assert.match(fast, /data-elapsed-since="1000"/);
  assert.match(fast, /5초째 작업 중/);
  assert.match(fast, /aria-hidden="true"/);
  assert.match(fast, /data-elapsed-slow-hint hidden/);
  const slow = progress.renderElapsed({ startedAt: 1000 }, 1000 + progress.SLOW_ANSWER_SECONDS * 1000);
  assert.doesNotMatch(slow, /data-elapsed-slow-hint hidden/);
  assert.equal(progress.renderElapsed({}), '');

  // 1초마다 다시 그리지 않고 숫자와 안내만 고친다.
  const label = { textContent: '' };
  const hint = { hidden: true };
  const badge = { dataset: { elapsedSince: '1000' }, querySelector: () => label, parentElement: { querySelector: () => hint } };
  progress.refreshElapsed({ querySelectorAll: () => [badge] }, 31_000);
  assert.equal(label.textContent, '30초째 작업 중');
  assert.equal(hint.hidden, false);
});

test('recent issues keep the newest ten unique views and keep an earlier title', async () => {
  const recent = await load('recent_items.js');
  let list = [];
  for (let id = 1; id <= 12; id += 1) list = recent.rememberIssue(list, { id, title: `이슈 ${id}` }, new Date(Date.UTC(2026, 10, 5, 0, id)));
  assert.equal(list.length, recent.RECENT_ISSUE_LIMIT);
  assert.deepEqual(plain(list.slice(0, 2).map((item) => item.id)), ['12', '11']);
  list = recent.rememberIssue(list, { id: '5', title: '' }, new Date(Date.UTC(2026, 10, 5, 1)));
  assert.deepEqual(plain(list[0]), { id: '5', title: '이슈 5', viewedAt: '2026-11-05T01:00:00.000Z' });
  assert.equal(list.length, recent.RECENT_ISSUE_LIMIT);
  // 이슈 번호가 아닌 값(Notion 페이지 ID 등)은 남기지 않는다.
  assert.equal(recent.rememberIssue(list, { id: 'notion-page' }), list);

  const store = new Map();
  const storage = { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) };
  recent.recordIssueView('user-a', { id: 77, title: '<b>결제</b>' }, storage);
  assert.equal(plain(recent.readRecentIssues('user-a', storage))[0].id, '77');
  assert.deepEqual(plain(recent.readRecentIssues('user-b', storage)), []);
  store.set('team-ai-assistant-recent-issues-v1:user-a', '{broken');
  assert.deepEqual(plain(recent.readRecentIssues('user-a', storage)), []);
  recent.recordIssueView('user-a', { id: 1 }, { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } });
});

test('home recents escape titles, skip the open and empty conversations and use relative times', async () => {
  const recent = await load('recent_items.js');
  const now = new Date('2026-11-05T12:00:00Z');
  assert.equal(recent.relativeTimeLabel('2026-11-05T11:59:40Z', now), '방금');
  assert.equal(recent.relativeTimeLabel('2026-11-05T11:48:00Z', now), '12분 전');
  assert.equal(recent.relativeTimeLabel('2026-11-05T09:00:00Z', now), '3시간 전');
  assert.equal(recent.relativeTimeLabel('2026-11-04T09:00:00Z', now), '어제');
  assert.equal(recent.relativeTimeLabel('2026-11-01T09:00:00Z', now), '4일 전');

  const conversations = [
    { id: 'open', title: '보는 중', updatedAt: '2026-11-05T11:00:00Z', messages: [{}] },
    { id: 'empty', title: '새 대화', updatedAt: '2026-11-05T11:30:00Z', messages: [] },
    { id: 'old', title: '오래된 대화', updatedAt: '2026-11-01T00:00:00Z', messages: [{}] },
    { id: 'server', title: '서버 <대화>', updatedAt: '2026-11-05T10:00:00Z', messages: [], messageCount: 4 },
  ];
  assert.deepEqual(plain(recent.resumableConversations(conversations, 'open')).map((item) => item.id), ['server', 'old']);
  const html = recent.homeRecentsHtml({ issues: [{ id: '9', title: '<img src=x>', viewedAt: '2026-11-05T11:00:00Z' }], conversations, activeId: 'open', now });
  assert.match(html, /data-home-issue="9"/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /서버 &lt;대화&gt;/);
  assert.match(html, /최근 본 이슈/);
  assert.equal(recent.homeRecentsHtml({ issues: [], conversations: [] }), '');
});

test('trash retention shows days left and marks the last three days', async () => {
  const recent = await load('recent_items.js');
  const now = new Date('2026-11-05T12:00:00Z');
  assert.deepEqual(plain(recent.trashRemainingLabel('2026-12-05T12:00:00Z', now)), { label: '30일 남음', urgent: false });
  assert.deepEqual(plain(recent.trashRemainingLabel('2026-11-08T11:00:00Z', now)), { label: '3일 남음', urgent: true });
  assert.deepEqual(plain(recent.trashRemainingLabel('2026-11-05T11:00:00Z', now)), { label: '곧 영구 삭제', urgent: true });
  assert.deepEqual(plain(recent.trashRemainingLabel('', now)), { label: '', urgent: false });
});

test('saved dashboard views normalize filters, replace by name and stop at the limit', async () => {
  const views = await load('dashboard_views_model.js');
  assert.deepEqual(plain(views.normalizeFilters({ period: '14', metric: 'bogus', customer: '고객사 A', age: '60+' })),
    { period: 14, metric: '', customer: '고객사 A', age: '60+' });
  assert.deepEqual(plain(views.normalizeFilters({ period: 45 })).period, 30);
  assert.ok(views.sameFilters({ period: 30 }, { period: '30', metric: '' }));
  assert.ok(!views.sameFilters({ period: 30 }, { period: 30, customer: 'B' }));

  const now = new Date('2026-11-05T00:00:00Z');
  let result = views.saveView([], { name: '  내 고객사  ', filters: { period: 7, customer: 'A' } }, now);
  assert.equal(result.error, '');
  assert.equal(result.view.name, '내 고객사');
  const firstId = result.view.id;
  result = views.saveView(result.views, { name: '내 고객사', filters: { period: 90 } }, new Date('2026-11-06T00:00:00Z'));
  assert.equal(result.views.length, 1);
  assert.equal(result.views[0].id, firstId);
  assert.equal(result.views[0].filters.period, 90);
  assert.equal(views.saveView(result.views, { name: '   ', filters: {} }).error, '보기 이름을 입력해 주세요.');

  let many = [];
  for (let index = 0; index < views.VIEW_LIMIT; index += 1) {
    many = views.saveView(many, { name: `보기 ${index}`, filters: {}, id: `v${index}` }, now).views;
  }
  assert.match(views.saveView(many, { name: '하나 더', filters: {} }, now).error, /12개까지/);
  assert.equal(views.saveView(many, { name: '보기 3', filters: { period: 7 } }, now).error, '');
  assert.deepEqual(plain(views.removeView(many, 'v0')).length, views.VIEW_LIMIT - 1);
  assert.equal(plain(views.updateViewFilters(many, 'v1', { metric: 'overdue' }))[1].filters.metric, 'overdue');
  assert.equal(views.describeFilters({ period: 30, customer: 'p1', metric: 'aging', age: '30-59' }, { customerName: () => '고객사 가' }),
    '30일 · 고객사 고객사 가 · 장기 미종료 · 경과 30–59일');
  assert.deepEqual(plain(views.sanitizeViews([null, { id: 'x', name: '' }, { id: 1, name: 'n' }, { id: 'ok', name: '보기', filters: {} }])).map((view) => view.id), ['ok']);
});

test('priority CSV keeps Korean in Excel, quotes cells and blocks formula injection', async () => {
  const views = await load('dashboard_views_model.js');
  const csv = views.priorityCsv([
    { id: 1, title: '=HYPERLINK("http://x")', project: 'p', status: 'assigned', handler: '', ageDays: 12, riskLevel: 'high', riskReasons: ['기한 지남', '고객 재문의'] },
    { id: 2, title: '줄\n바꿈, 쉼표', project: 'q', status: 'new', handler: '담당 A', ageDays: null, riskLevel: 'other' },
  ], { customerName: (issue) => `고객 ${issue.project}`, statusLabel: (value) => `[${value}]`, riskLabel: (level) => level });
  assert.ok(csv.startsWith('﻿이슈 ID,제목,고객사,상태,담당자,경과일,위험도,위험 사유\r\n'));
  assert.match(csv, /\r\n1,"'=HYPERLINK\(""http:\/\/x""\)",고객 p,\[assigned\],미지정,12,high,기한 지남 · 고객 재문의\r\n/);
  assert.match(csv, /\r\n2,"줄\n바꿈, 쉼표",고객 q,\[new\],담당 A,,watch,\r\n$/);
  assert.equal(views.csvCell('-1'), "'-1");
  assert.equal(views.csvCell('@sum'), "'@sum");
});

test('my work summary counts open issues, overdue ones and long-running ones separately', async () => {
  const views = await load('dashboard_views_model.js');
  const summary = views.myWorkSummary([
    { isOpen: true, overdue: true, ageDays: 40 },
    { isOpen: true, overdue: false, ageDays: 31 },
    { isOpen: true, ageDays: 2 },
    { isOpen: false, overdue: true, ageDays: 90 },
    { status: '할당된 이슈', ageDays: 1 },
    { status: '종료', ageDays: 100 },
  ]);
  assert.deepEqual(plain(summary), { open: 4, overdue: 1, aging: 1 });
});

test('composer conditions are visible phrases that toggle and replace within their group', async () => {
  const conditions = await load('composer_conditions.js');
  let text = conditions.toggleConditionPhrase('', 'two-weeks');
  assert.equal(text, '최근 2주 ');
  text = conditions.toggleConditionPhrase(`${text}결제 이슈 알려줘`, 'mine');
  assert.equal(text, '내 담당 최근 2주 결제 이슈 알려줘');
  text = conditions.toggleConditionPhrase(text, 'month');
  assert.equal(text, '최근 한 달 내 담당 결제 이슈 알려줘');
  assert.deepEqual(plain(conditions.activeConditionIds(text)).sort(), ['mine', 'month']);
  text = conditions.toggleConditionPhrase(text, 'mine');
  assert.equal(text, '최근 한 달 결제 이슈 알려줘');
  // 단어 일부(예: '미해결건')는 조건으로 보지 않는다.
  assert.deepEqual(plain(conditions.activeConditionIds('미해결건 정리')), []);
  assert.equal(conditions.toggleConditionPhrase('질문', 'unknown'), '질문');
});

test('composer period selection replaces the date already supplied by a team template', async () => {
  const conditions = await load('composer_conditions.js');
  const cases = fs.readFileSync(path.join(__dirname, 'fixtures/routing_cases.jsonl'), 'utf8')
    .trim().split('\n').map(JSON.parse).filter((item) => item.composer_input);
  assert.ok(cases.length > 0);
  for (const item of cases) {
    assert.equal(conditions.toggleConditionPhrase(item.composer_input, item.composer_condition), item.q);
  }
  for (const period of ['이번 주', '이번 달', '지난주', '지난달', '올해', '최근 3개월', '최근 6개월', '2026년 11월']) {
    assert.equal(conditions.toggleConditionPhrase(`${period} 미래에셋증권 이슈를 요약해줘`, 'two-weeks'),
      '최근 2주 미래에셋증권 이슈를 요약해줘', period);
  }
});

test('composer period replacement preserves issue references and non-period wording', async () => {
  const conditions = await load('composer_conditions.js');
  assert.equal(conditions.toggleConditionPhrase('내 담당 이번 주 #31695 화면 0600 미종료 이슈', 'month'),
    '최근 한 달 내 담당 #31695 화면 0600 미종료 이슈');
  assert.equal(conditions.toggleConditionPhrase('오늘 등록된 이슈를 지난달과 비교해줘', 'two-weeks'),
    '최근 2주 등록된 이슈를 지난달과 비교해줘');
  assert.equal(conditions.toggleConditionPhrase('30일 이상 미종료 이슈와 오늘의 시세 오류를 찾아줘', 'week'),
    '최근 1주 30일 이상 미종료 이슈와 오늘의 시세 오류를 찾아줘');
  const replaced = conditions.toggleConditionPhrase('오늘 등록된 이슈', 'two-weeks');
  assert.equal(conditions.toggleConditionPhrase(replaced, 'week'), '최근 1주 등록된 이슈');
  assert.equal(conditions.toggleConditionPhrase(replaced, 'two-weeks'), '등록된 이슈');
});

test('new UI pieces are wired into the page with sprite icons and shared module URLs', () => {
  const html = fs.readFileSync(path.join(__dirname, '../backend/static/index.html'), 'utf8');
  const symbols = new Set([...html.matchAll(/<symbol id="i-([a-z-]+)"/g)].map((match) => match[1]));
  for (const id of ['dashboardViewsBar', 'composerConditionsBtn', 'composerConditionsMenu', 'conversationTrashBtn',
    'conversationTrashModal', 'conversationTrashDialog', 'conversationTrashList', 'conversationTrashPurgeAll']) {
    assert.ok(html.includes(`id="${id}"`), `${id} 요소가 없습니다.`);
  }
  for (const sheet of ['answer_progress', 'home_recents', 'dashboard_views', 'composer_conditions', 'conversation_trash']) {
    assert.match(html, new RegExp(`/static/${sheet}\\.css\\?v=`));
  }
  const bootstrap = fs.readFileSync(path.join(MODULES, 'bootstrap.js'), 'utf8');
  for (const init of ['initHomeRecents', 'initDashboardViews', 'initComposerConditions', 'initConversationTrash']) {
    assert.match(bootstrap, new RegExp(`${init}\\(\\);`));
  }
  for (const name of ['dashboard_views.js', 'conversation_trash.js', 'home_recents.js', 'chat_progress.js']) {
    const source = fs.readFileSync(path.join(MODULES, name), 'utf8');
    for (const match of source.matchAll(/icon\("([a-z-]+)"/g)) assert.ok(symbols.has(match[1]), `${name}: i-${match[1]} 아이콘이 없습니다.`);
    for (const match of source.matchAll(/from "\.\/(runtime|workspace|dashboard|chat_source_detail)\.js\?v=([^"]+)"/g)) {
      assert.equal(match[2], '20260911-6', `${name}가 ${match[1]}.js를 다른 버전 주소로 가져옵니다.`);
    }
  }
});
