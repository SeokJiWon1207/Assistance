const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const specs = [];
async function load() {
  specs.length = 0;
  const context = vm.createContext({});
  const mocks = {
    './bklit_charts.js': { chartHost: (spec, fallback) => { specs.push(spec); return `<div data-spec="${spec.kind}">${fallback}</div>`; } },
    './icons.js': { icon: (name) => `<svg data-icon="${name}"></svg>` },
  };
  const module = new vm.SourceTextModule(
    fs.readFileSync(path.join(__dirname, '../backend/static/modules/workspace_insights.js'), 'utf8'), { context });
  await module.link((specifier) => {
    const values = mocks[specifier.replace(/\?v=.*$/, '')];
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

const plain = (value) => JSON.parse(JSON.stringify(value));

test('new issue delta compares counts, not percentages, and stays neutral when equal', async () => {
  const { newIssueDelta } = await load();
  assert.deepEqual(plain(newIssueDelta(3, 1)), { tone: 'is-up', icon: 'arrow-up', label: '2건', text: '직전 7일보다 2건 많음' });
  assert.deepEqual(plain(newIssueDelta(1, 2)), { tone: 'is-down', icon: 'arrow-down', label: '1건', text: '직전 7일보다 1건 적음' });
  assert.equal(newIssueDelta(0, 0).label, '변동 없음');
});

test('open issues are bucketed by age with unknown dates kept apart, and statuses sorted by count', async () => {
  const { ageRows, statusRows } = await load();
  const issues = [{ ageDays: 0, status: 'assigned' }, { ageDays: 7, status: 'assigned' }, { ageDays: 8, status: 'new' },
    { ageDays: 45, status: 'assigned' }, { ageDays: 200, status: 'feedback' }, { ageDays: null, status: 'new' }];
  const age = plain(ageRows(issues));
  assert.deepEqual(age.rows.map((row) => row.count), [2, 1, 1, 1]);
  assert.equal(age.unknown, 1);
  assert.deepEqual(age.rows.map((row) => row.minDays), [0, 8, 30, 60]);
  const labels = { assigned: '할당된 이슈', new: '신규 이슈', feedback: '정보 부족' };
  assert.deepEqual(plain(statusRows(issues, (value) => labels[value])), [
    { fullLabel: '할당된 이슈', amount: 3, color: 'var(--chart-1)' },
    { fullLabel: '신규 이슈', amount: 2, color: 'var(--chart-2)' },
    { fullLabel: '정보 부족', amount: 1, color: 'var(--chart-3)' },
  ]);
});

test('KPI strip shows the 7-day comparison only for complete data and keeps lower bounds for partial data', async () => {
  const { workspaceKpis } = await load();
  const data = { summary: { open: 4, overdue: 1, aging: 0 }, activity: { recentNew: 3, previousNew: 1 } };
  const html = workspaceKpis(data, true);
  assert.match(html, /최근 7일 신규/);
  assert.match(html, /workspace-kpi-delta is-up/);
  assert.match(html, /직전 7일보다 2건 많음/);
  assert.match(html, /직전 7일 1건 · 등록일 기준/);
  assert.match(html, /workspace-kpi is-danger/);

  const partial = workspaceKpis(data, false);
  assert.doesNotMatch(partial, /workspace-kpi-delta/);
  assert.match(partial, /4건 이상/);
  assert.match(partial, /확인한 범위 기준/);
  // 서버가 비교 값을 주지 않으면 증감을 지어내지 않는다.
  assert.doesNotMatch(workspaceKpis({ summary: {}, weeklyBrief: { newIssues: 2 } }, true), /workspace-kpi-delta/);
});

test('chart cards pass a cumulative area spec and fall back to plain counts', async () => {
  const { workspaceCharts } = await load();
  const points = [{ date: '2026-11-04', count: 2 }, { date: '2026-11-05', count: 1 }];
  const html = workspaceCharts({ activity: { points, dateBasis: '등록일' }, mantis: { issues: [{ ageDays: 3, status: 'assigned' }] } }, true,
    { statusLabel: () => '할당된 이슈' });
  assert.deepEqual(plain(specs.map((spec) => spec.kind)), ['area', 'age-donut', 'pie']);
  assert.equal(specs[0].cumulative, true);
  assert.deepEqual(plain(specs[0].points), points);
  assert.match(html, /최근 2일 누적 · 등록일 기준 · 종료된 이슈 포함/);
  assert.match(html, /신규 등록<strong>3건<\/strong>/);
  assert.match(html, /최근 2일 신규 등록 3건/);

  const empty = workspaceCharts({ activity: {}, mantis: { issues: [] } }, true);
  assert.equal(specs.filter((spec) => spec.kind !== 'area').length, 2);
  assert.equal(empty, '');
  const history = workspaceCharts({ activity: { points }, mantis: { issues: [] } }, true);
  assert.match(history, /내 담당 신규 등록 추이/);
  assert.doesNotMatch(history, /고객사별 담당|미종료 경과일|상태별 미종료/);
});

test('weekly report compares this week with the 7 days before it from the 30-day activity', async () => {
  const { weekWindows, weeklyKpis, weeklyTrendCard } = await load();
  const points = Array.from({ length: 30 }, (_, index) => ({ date: `2026-10-${String(index + 1).padStart(2, '0')}`, count: index >= 23 ? 1 : index >= 16 ? 2 : 0 }));
  const windows = plain(weekWindows({ points }));
  assert.deepEqual(windows.current.map((point) => point.date), points.slice(-7).map((point) => point.date));
  assert.deepEqual(windows.previous.map((point) => point.date), points.slice(-14, -7).map((point) => point.date));
  assert.deepEqual(plain(weekWindows({ points: points.slice(-10) })).previous, []);

  const data = { weeklyBrief: { newIssues: 7, updatedIssues: 3, openIssues: 5, overdueIssues: 1 }, activity: { points, previousNew: 14 } };
  const kpis = weeklyKpis(data, true);
  assert.match(kpis, /지난주보다 7건 적음/);
  assert.match(kpis, /지난주 14건 · 등록일 기준/);
  assert.match(kpis, /workspace-kpi is-danger/);
  assert.doesNotMatch(weeklyKpis(data, false), /workspace-kpi-delta/);

  const card = weeklyTrendCard(data, true);
  const spec = specs.at(-1);
  assert.equal(spec.kind, 'compare-area');
  assert.equal(spec.points.length, 7);
  assert.equal(spec.previousPoints.length, 7);
  assert.match(card, /이번 주<strong>7건<\/strong>/);
  assert.match(card, /지난주<strong>14건<\/strong>/);
  // 일부만 확인했거나 앞 주 데이터가 없으면 비교하지 않고 이번 주 누적만 그린다.
  weeklyTrendCard(data, false);
  assert.equal(specs.at(-1).kind, 'area');
  weeklyTrendCard({ activity: { points: points.slice(-7) } }, true);
  assert.equal(specs.at(-1).kind, 'area');
});

test('team brief KPI shows the server change rate as a badge only when comparable', async () => {
  const { teamBriefKpis, changeRateDelta } = await load();
  assert.deepEqual(plain(changeRateDelta(12.5, true)), { tone: 'is-up', icon: 'arrow-up', label: '12.5%', text: '지난주보다 12.5% 많음' });
  assert.equal(changeRateDelta(null, true), null);
  assert.equal(changeRateDelta(10, false), null);
  const html = teamBriefKpis({ newIssues: 9, changeRate: -20, carriedOpen: 4, aging: 2, overdue: 1, unassigned: 3 }, true);
  assert.match(html, /지난주보다 20% 적음/);
  assert.match(html, /담당자 미지정 3건/);
  assert.match(teamBriefKpis({ newIssues: 2, changeRate: null }, true), /전주 비교 기준 없음/);
  assert.match(teamBriefKpis({ newIssues: 2, changeRate: 5 }, false), /2건 이상/);
});
