const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
async function models() {
  const source = fs.readFileSync(path.join(__dirname, '../backend/static/modules/chart_models.js'), 'utf8');
  return import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
}
test('due calendar counts real due dates, excludes invalid dates and keeps missing separate', async () => {
  const { dueCalendar } = await models();
  const result = dueCalendar([
    {id:1, dueDate:'2026-10-26'}, {id:2, dueDate:'2026-10-26'},
    {id:3, dueDate:'2026-10-25'}, {id:4, dueDate:''},
    {id:5, dueDate:'2026-02-30'}, {id:6, dueDate:'2026-11-23'},
  ], '2026-10-26');
  assert.equal(result.days.length, 28);
  assert.equal(result.days[0].count, 2);
  assert.deepEqual(result.days[0].ids, [1, 2]);
  assert.equal(result.days.at(-1).date, '2026-11-22');
  assert.equal(result.missing, 2);
  assert.equal(result.outside, 2);
});
test('brush selection clamps to observed range and totals only selected registration days', async () => {
  const { selectTrend } = await models();
  const points = [{date:'2026-10-24',count:3},{date:'2026-10-25',count:0},{date:'2026-10-26',count:2}];
  assert.deepEqual(selectTrend(points, '2026-10-25', '2026-10-30'), {from:'2026-10-25',to:'2026-10-26',total:2});
  assert.equal(selectTrend(points, '2026-10-26', '2026-10-24').total, 5);
  assert.deepEqual(selectTrend([], '', ''), {from:'',to:'',total:0});
});
test('new cohort includes unknown status without double counting or inventing a denominator', async () => {
  const { cohortSlices } = await models();
  const rows = cohortSlices({closedNow:3,newOpen:2,unknownStatus:1});
  assert.equal(rows.reduce((total,row)=>total+row.value,0),6);
  assert.deepEqual(rows.map(row=>row.label),['현재 종료','현재 미종료','상태 미상']);
});

async function bridgeHarness({fail=false, delayed=false}={}) {
  const vm = require('node:vm'); const events=[]; let resolveLoad;
  const context=vm.createContext({console:{warn:()=>events.push('warn')}});
  const bundle=new vm.SyntheticModule(['mountChart'],function(){this.setExport('mountChart',(host,spec)=>{
    events.push(['mount',spec.kind]);return ()=>events.push('unmount');
  });},{context});
  await bundle.link(()=>{}); await bundle.evaluate();
  const loaded=delayed?new Promise(resolve=>{resolveLoad=resolve;}):Promise.resolve(bundle);
  const bridge=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'../backend/static/modules/bklit_charts.js'),'utf8'),{
    context, importModuleDynamically:async()=>{if(fail)throw Error('offline');return loaded;},
  });
  await bridge.link(()=>{});await bridge.evaluate();
  const host={isConnected:true,dataset:{bklitSpec:JSON.stringify({kind:'bar'})}};
  const container={querySelectorAll:()=>[host],contains:child=>child===host};
  return {api:bridge.namespace,events,host,container,resolve:()=>resolveLoad(bundle)};
}
test('chart lifecycle releases existing roots and ignores stale async mounts',async()=>{
  const h=await bridgeHarness();await h.api.mountCharts(h.container);await h.api.mountCharts(h.container);
  assert.deepEqual(h.events,[['mount','bar']]);
  h.api.releaseCharts(h.container);assert.equal(h.events.at(-1),'unmount');
  const delayed=await bridgeHarness({delayed:true});const pending=delayed.api.mountCharts(delayed.container);
  delayed.api.releaseCharts(delayed.container);delayed.resolve();await pending;
  assert.deepEqual(delayed.events,[]);
});
test('bundle failure preserves native fallback and malicious titles cannot escape data attributes',async()=>{
  const h=await bridgeHarness({fail:true});await h.api.mountCharts(h.container);
  assert.equal(h.host.dataset.chartError,'true');assert.deepEqual(h.events,['warn']);
  const html=h.api.chartHost({kind:'bar',title:'"><img src=x onerror=alert(1)>'},'<button>기존 필터</button>');
  assert.ok(html.includes('<button>기존 필터</button>'));
  assert.ok(!html.includes('<img'));assert.ok(html.includes('&lt;img'));
});

test('metric charts preserve decimal effort, long labels and partial coverage', async()=>{
  const { metricChartSpec }=await models();
  assert.equal(typeof metricChartSpec,'function');
  const input=[{label:'긴 제목 '.repeat(20),amount:1.25},{label:'개발',amount:0.05}];
  const spec=metricChartSpec(input,{title:'공수',kind:'pie',decimals:2,unit:'',complete:false});
  assert.equal(spec.rows[0].amount,1.25);assert.equal(spec.rows[1].amount,0.05);
  assert.equal(spec.rows[0].fullLabel,input[0].label);assert.equal(spec.complete,false);
  assert.equal(spec.rows.reduce((sum,row)=>sum+row.amount,0),1.3);
  assert.equal(input[0].id,undefined);
});
test('monthly series preserve alignment, metric precision and missing cells without inventing zeros',async()=>{
  const { monthlyChartData }=await models();assert.equal(typeof monthlyChartData,'function');
  const input={periods:['202601','202602','202603'],series:[{label:'개선',counts:[2,0,3],efforts:[1.25,0,2.05]},{label:'오류',counts:[1,null,2],efforts:[0.5,0.2,0]}]};
  const counts=monthlyChartData(input,'counts');
  assert.deepEqual(counts.rows.map(row=>row.date),['2026-01-01','2026-02-01','2026-03-01']);
  assert.equal(counts.rows[1].series1,null);assert.equal(counts.missing,true);
  assert.equal(counts.rows[1].series0,0);assert.equal(counts.unit,'건');
  const efforts=monthlyChartData(input,'efforts');
  assert.equal(efforts.rows[2].series0,2.05);assert.equal(efforts.decimals,2);assert.equal(efforts.missing,false);
  assert.equal(monthlyChartData({...input,periods:['202613']},'counts').missing,true);
});

test('saved monthly answers keep their original effort date basis',async()=>{
  const { monthlyChartData }=await models();
  const old={periods:['202609'],series:[]};
  assert.equal(monthlyChartData(old,'efforts').effortBasis,'등록일');
  assert.equal(monthlyChartData({...old,effortDateBasis:'delivered_at'},'efforts').effortBasis,'고객사전달일');
});
