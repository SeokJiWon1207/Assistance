const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function api() {
  const context = vm.createContext({});
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/answer_summary.js'), 'utf8'), {context});
  await module.link(() => {});
  await module.evaluate();
  return module.namespace;
}

test('summary metrics keep zero, thousands, units and the scope note from the answer', async () => {
  const {parseSummaryMetric} = await api();
  const cases = [
    ['미종료: 0건', {label:'미종료', value:'0', unit:'건', note:''}],
    ['- 신규 등록: **1,234건** · 직전 7일 12건 대비 +2건', {label:'신규 등록', value:'1,234', unit:'건', note:'· 직전 7일 12건 대비 +2건'}],
    ['현재 미종료: 8건 이상 (일부만 조회)', {label:'현재 미종료', value:'8', unit:'건 이상', note:'(일부만 조회)'}],
    ['평균 처리기간: 2.5일 (해결 4건 기준)', {label:'평균 처리기간', value:'2.5', unit:'일', note:'(해결 4건 기준)'}],
    ['기간 공수 합계: 2.50', {label:'기간 공수 합계', value:'2.50', unit:'', note:''}],
    ['등록 이슈: 1건 · 전월 동기 1건 대비 +0건', {label:'등록 이슈', value:'1', unit:'건', note:'· 전월 동기 1건 대비 +0건'}],
    ['총 공수: 2.00', {label:'총 공수', value:'2.00', unit:'', note:''}],
    ['현재 장기 미종료: 0건 (30일 이상)', {label:'현재 장기 미종료', value:'0', unit:'건', note:'(30일 이상)'}],
  ];
  for (const [text, expected] of cases) assert.deepEqual(JSON.parse(JSON.stringify(parseSummaryMetric(text))), expected);
});

test('dates, IDs, uncertainty and malformed counts do not become summary metrics', async () => {
  const {parseSummaryMetric} = await api();
  for (const text of ['등록일: 2026-11-09', '이슈 번호: 10321', '화면번호: 0000',
    '신규 등록: 12,34건', '미종료: 확인하지 못했습니다', '미종료: 약 8건',
    '조회 기준: 2026년 11월', '현재 미종료: 8건 #10321 참조',
    '등록자: 123', '<img src=x>: 8건']) {
    assert.equal(parseSummaryMetric(text), null, text);
  }
});

test('metric scope notes remain literal text instead of becoming markup', async () => {
  const {parseSummaryMetric} = await api();
  const result = parseSummaryMetric('미종료: 8건 (<img src=x onerror=alert(1)>)');
  assert.equal(result.note, '(<img src=x onerror=alert(1)>)');
});

test('only complete independent section labels receive heading semantics', async () => {
  const {isSummarySectionHeading} = await api();
  for (const text of ['요약', '핵심 결론', '신규 이슈', '미종료 이슈', '장기 적체', '공수·처리시간', '최근 월별 추이', '11월 주요 등록 이슈']) {
    assert.equal(isSummarySectionHeading(text), true, text);
  }
  for (const text of ['요약하면 8건입니다.', '신규 이슈가 없습니다.', '고객이 요청한 요약', '', '<h1>요약</h1>']) {
    assert.equal(isSummarySectionHeading(text), false, text);
  }
});
