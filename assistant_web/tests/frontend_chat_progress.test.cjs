const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function loadProgress() {
  const context = vm.createContext({});
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/chat_progress.js'), 'utf8'), { context });
  await module.link(() => new vm.SyntheticModule(['icon'], function() { this.setExport('icon', () => '<svg></svg>'); }, { context }));
  await module.evaluate();
  return module.namespace;
}

test('stages not reported by the server are never marked completed', async () => {
  const api = await loadProgress();
  const steps = api.progressSteps({ stage: 'verification', stageHistory: ['analysis', 'verification'] });
  assert.equal(steps[0].done, true);
  assert.equal(steps[1].done, false);
  assert.equal(steps[2].current, true);
  assert.equal(api.progressSteps({ stage: 'unknown' }).length, 0);
});

test('comparison is active and evidence counts are labeled as answer sources', async () => {
  const api = await loadProgress();
  const html = api.renderProgressSteps({ stage: 'comparison', stageHistory: ['retrieval', 'comparison'], evidenceCount: 3 });
  assert.match(html, /답변에 사용할 원문 3건 확보/);
  assert.equal((html.match(/is-done/g) || []).length, 1);
  assert.doesNotMatch(html, /100%|전체 3건/);
});
