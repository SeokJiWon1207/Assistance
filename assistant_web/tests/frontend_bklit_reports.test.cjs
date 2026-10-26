const test=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');const vm=require('node:vm');
const moduleDir=path.join(__dirname,'../backend/static/modules');
const escape=text=>String(text??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
async function load(name,extra='') {
  const context=vm.createContext({console,document:{createElement:()=>({className:'',_html:'',set textContent(v){this._html=escape(v)},get innerHTML(){return this._html},set innerHTML(v){this._html=v}})}});
  const cache=new Map();
  async function get(file) {
    file=file.split('?')[0];if(cache.has(file))return cache.get(file);
    const source=fs.readFileSync(path.join(moduleDir,file),'utf8');
    const mod=new vm.SourceTextModule(source+(file===name?extra:''),{context});cache.set(file,mod);
    await mod.link(spec=>get(spec.replace('./','')));return mod;
  }
  const mod=await get(name);await mod.evaluate();return mod.namespace;
}
test('monthly report passes server data unchanged and exposes safe numeric fallback on bundle failure',async()=>{
  const api=await load('trend_chart.js');
  const trend={periods:['202609','202610'],series:[{label:'<img onerror=alert(1)>',counts:[3,0],efforts:[1.25,null]}],scopeLabel:'실제 담당 범위',periodLabel:'9~10월'};
  const html=api.renderEffortTrendChart(trend,{complete:false}).innerHTML;
  assert.match(html,/data-bklit-kind="monthly"/);assert.match(html,/3 \/ 1.25/);assert.match(html,/0 \/ 미확인/);
  assert.match(html,/&quot;complete&quot;:false/);assert.ok(!html.includes('<img'));
  assert.match(html,/실제 담당 범위/);assert.equal(trend.series[0].efforts[1],null);
  assert.match(api.renderEffortTrendChart({periods:[],series:[]}).innerHTML,/표시할 추이 데이터가 없습니다/);
});
test('admin distribution retains zero counts, duplicates and complete labels without accepting HTML',async()=>{
  // 실제 관리자 분포 어댑터를 실행하되 인증/네트워크/다른 관리자 메뉴는 로드하지 않는다.
  const source=fs.readFileSync(path.join(moduleDir,'admin.js'),'utf8');
  const bar=source.slice(source.indexOf('function barList('),source.indexOf('\nfunction chips('));
  const context=vm.createContext({escapeHtml:escape});
  const model=new vm.SourceTextModule(fs.readFileSync(path.join(moduleDir,'chart_models.js'),'utf8'),{context});await model.link(()=>{});await model.evaluate();
  const bridge=new vm.SourceTextModule(fs.readFileSync(path.join(moduleDir,'bklit_charts.js'),'utf8'),{context});await bridge.link(()=>{});await bridge.evaluate();
  const mod=new vm.SourceTextModule('import {metricChartSpec} from "models";import {chartHost} from "bridge";'+bar+'\nexport {barList};',{context});
  await mod.link(s=>s==='models'?model:bridge);await mod.evaluate();
  const html=mod.namespace.barList([['긴 모델명 <img>',7],['같은 이름',0],['같은 이름',2]],'is-danger','검사 실패');
  assert.match(html,/data-bklit-kind="bar"/);assert.match(html,/&quot;amount&quot;:0/);assert.match(html,/&quot;amount&quot;:7/);
  assert.ok(!html.includes('<img>'));assert.match(html,/var\(--danger\)/);
  const spec=JSON.parse(html.match(/data-bklit-spec="([^"]+)"/)[1].replaceAll('&quot;','"').replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&'));
  assert.equal(new Set(spec.rows.map(r=>r.name)).size,3);assert.equal(spec.rows[0].fullLabel,'긴 모델명 <img>');
});
