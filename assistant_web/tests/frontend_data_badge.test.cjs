const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function loadBadge() {
  const context = vm.createContext({ Intl, Date });
  const module = new vm.SourceTextModule(
    fs.readFileSync(path.join(__dirname, '../backend/static/modules/data_badge.js'), 'utf8'),
    { context },
  );
  await module.link(() => new vm.SyntheticModule(['icon'], function () {
    this.setExport('icon', (name) => `<svg data-icon="${name}"></svg>`);
  }, { context }));
  await module.evaluate();
  return module.namespace;
}

test('server source flags collapse into one badge state with fixed priority', async () => {
  const { dataBadgeState } = await loadBadge();
  assert.equal(dataBadgeState({ source: 'live', complete: true }), 'fresh');
  assert.equal(dataBadgeState({ source: 'live', complete: false }), 'partial');
  assert.equal(dataBadgeState({ source: 'snapshot', complete: false }), 'snapshot');
  assert.equal(dataBadgeState({ source: 'live', refreshing: true, complete: false }), 'refreshing');
  assert.equal(dataBadgeState({ source: 'error', refreshing: true }), 'error');
  assert.equal(dataBadgeState({ source: 'live', loading: true }), 'loading');
});

test('every badge state has one Korean label and no internal store names', async () => {
  const { STATE_META, mantisBadgeDetails } = await loadBadge();
  const labels = Object.values(STATE_META).map((meta) => meta.label);
  assert.deepEqual([...labels], ['최신', '갱신 중', '일부만 확인', '저장본', '확인 필요', '연결 오류', '확인 중']);
  for (const state of Object.keys(STATE_META)) {
    const details = mantisBadgeDetails(state, { time: '오후 2:02' });
    assert.doesNotMatch(JSON.stringify(details), /Vector DB|ChromaDB|스냅샷|미확정/);
  }
  assert.equal(mantisBadgeDetails('partial').retry, true);
  assert.equal(mantisBadgeDetails('fresh').retry, false);
  assert.match(mantisBadgeDetails('snapshot', { time: '오전 9:10' }).note, /오전 9:10 이후/);
});

test('badge time is a short clock today and a dated clock otherwise', async () => {
  const { formatBadgeTime, formatBadgeDate } = await loadBadge();
  const now = new Date();
  assert.doesNotMatch(formatBadgeTime(now.toISOString()), /\//);
  const earlier = new Date(now.getTime() - 3 * 86400000);
  assert.match(formatBadgeTime(earlier.toISOString()), new RegExp(`^${earlier.getMonth() + 1}/${earlier.getDate()} `));
  assert.equal(formatBadgeTime(''), '');
  assert.equal(formatBadgeDate('2026-10-01'), '10월 1일');
});
