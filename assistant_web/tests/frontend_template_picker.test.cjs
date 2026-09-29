const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const templates = JSON.parse(
  fs.readFileSync(path.join(__dirname, '../config/team_templates.json'), 'utf8'),
).templates;

async function loadPicker() {
  const context = vm.createContext({});
  const dependencies = {
    './runtime.js': {
      state: {}, els: {}, apiFetch: async () => ({ ok: false }),
      describeTeamTemplateQuestion: () => ({ question: '', missing: [] }),
      scopedStorageKey: (base, userId) => `${base}:${userId || 'guest'}`,
    },
    './chat_rendering.js': { escapeHtml: (value) => String(value ?? '') },
  };
  const module = new vm.SourceTextModule(
    fs.readFileSync(path.join(__dirname, '../backend/static/modules/template_picker.js'), 'utf8'),
    { context },
  );
  await module.link((specifier) => {
    const values = dependencies[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

// vm 컨텍스트에서 만든 배열은 프로토타입이 달라 deepEqual 전에 이 영역의 배열로 옮긴다.
const ids = (items) => [...items].map((item) => item.id);

test('every configured template category belongs to one picker group', async () => {
  const { TEMPLATE_GROUPS } = await loadPicker();
  const grouped = TEMPLATE_GROUPS.flatMap((group) => group.categories || []);
  for (const template of templates) {
    assert.ok(grouped.includes(template.category), `${template.id}의 분류 ${template.category}가 묶음에 없습니다.`);
  }
});

test('recent templates lead the full list only without a query or group filter', async () => {
  const { templatePickerSections } = await loadPicker();
  const recentIds = ['weekly-report', 'missing-template', 'aging-issues'];

  const all = templatePickerSections(templates, { recentIds });
  assert.deepEqual(ids(all.recent), ['weekly-report', 'aging-issues']);
  assert.equal(all.recent.length + all.rest.length, templates.length);
  assert.ok(!ids(all.rest).includes('weekly-report'));
  assert.equal(all.groupCounts.all, templates.length);

  const searched = templatePickerSections(templates, { query: '보고', recentIds });
  assert.deepEqual(ids(searched.recent), []);
  assert.ok(ids(searched.rest).includes('weekly-report'));

  const grouped = templatePickerSections(templates, { group: 'ops', recentIds });
  assert.deepEqual(ids(grouped.recent), []);
  assert.deepEqual(ids(grouped.rest), ['incident-triage', 'issue-handoff', 'customer-notice']);
});

test('slash query matches title, description, and category ignoring spaces', async () => {
  const { templatePickerSections } = await loadPicker();

  assert.deepEqual(ids(templatePickerSections(templates, { query: '장애대응' }).rest), ['incident-triage']);
  assert.ok(ids(templatePickerSections(templates, { query: 'SLA' }).rest).includes('effort-sla'));
  const counts = templatePickerSections(templates, { query: '보고서' }).groupCounts;
  assert.equal(counts.all, counts.analysis + counts.report + counts.ops);
  assert.deepEqual(ids(templatePickerSections(templates, { query: '없는검색어' }).rest), []);
});
