const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function render(data) {
  const context = vm.createContext({});
  const load = name => new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules', name), 'utf8'), {context});
  const bridge = load('bklit_charts.js');
  await bridge.link(() => {});
  const module = load('dashboard_weekly.js');
  await module.link(() => bridge);
  await module.evaluate();
  return module.namespace.renderWeeklyIssues(data);
}
function sample(complete = true) {
  return {from:'2026-10-24',to:'2026-10-30',complete,
    received:{total:7,missingDate:2,rows:[{name:'HTS',counts:{'개선':5,'오류':1}},{name:'MTS',counts:{'문의':1}}]},
    completed:{total:1,missingDate:0,rows:[{name:'HTS',counts:{'검토/보류':1}}]},
    remaining:{total:51,rows:[{name:'HTS',count:39},{name:'MTS',count:12}]}};
}
function specs(html) {
  return [...html.matchAll(/data-bklit-spec="([^"]+)"/g)].map(([, s]) => JSON.parse(s.replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&')));
}
test('weekly cards show independent Sat–Fri dates, grouped bars, donut and accessible exact values', async () => {
  const html = await render(sample());
  const charts = specs(html);
  assert.equal(charts.length, 3);
  assert.match(html, /10\/24\(토\).*10\/30\(금\)/);
  assert.match(html, /고객사요청일/);
  assert.match(html, /고객사전달일/);
  assert.match(html, /정기점검/);
  assert.equal(charts[0].grouped, true);
  assert.equal(charts[0].rows[0].series0, 5);
  assert.equal(charts[0].rows[1].series0, 0);
  assert.equal(charts[2].kind, 'pie');
  assert.equal(charts[2].rows.reduce((sum, row) => sum + row.amount, 0), 51);
  assert.equal(charts[2].showValues, true);
  assert.match(html, /<table>/);
  assert.match(html, /2건/);
});
test('partial and empty results do not claim a complete zero or a total percentage', async () => {
  const data = sample(false);
  data.received = {total:0,missingDate:12,rows:[]};
  const html = await render(data);
  assert.match(html, /확인한 범위/);
  assert.match(html, /51건 이상/);
  assert.match(html, /0건 이상/);
  assert.ok(specs(html).every(spec => !spec.complete));
  assert.doesNotMatch(html, /%/);
  assert.equal(await render(undefined), '');
});
test('unknown categories are counted and untrusted labels cannot inject markup', async () => {
  const data = sample();
  data.received.rows = [{name:'<img src=x>',counts:{'<script>':3,'추가':2}}];
  data.received.total = 5;
  const html = await render(data);
  assert.doesNotMatch(html, /<img|<script>/);
  const chart = specs(html)[0];
  assert.equal(chart.series.length, 2);
  assert.equal(chart.rows[0].series0 + chart.rows[0].series1, 5);
});
