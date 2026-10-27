const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dir = path.join(__dirname, '../backend/static/modules');
const escape = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
function element(tag) {
  return {tag, className:'', children:[], _html:'', dataset:{},
    set textContent(value) {this._html = escape(value);},
    get innerHTML() {return this._html + this.children.map(c => c.outerHTML).join('');},
    set innerHTML(value) {this._html = value; this.children = [];},
    get outerHTML() {return `<${tag} class="${this.className}">${this.innerHTML}</${tag}>`;},
    appendChild(child) {this.children.push(child);return child;},
    querySelectorAll() {return [];}, querySelector() {return null;}, addEventListener() {},
  };
}
async function harness() {
  const context = vm.createContext({document:{createElement:element}, console});
  const cache = new Map();
  async function load(name) {
    name = name.replace('./','').split('?')[0];
    if (cache.has(name)) return cache.get(name);
    if (name === 'motion.js') {
      const mock = {animate(){},stagger(){},prefersReducedMotion:()=>true,onFirstVisible(){},countUp(){}};
      const mod = new vm.SyntheticModule(Object.keys(mock),function(){for(const k in mock)this.setExport(k,mock[k]);},{context});
      await mod.link(()=>{});return mod;
    }
    let source = fs.readFileSync(path.join(dir,name),'utf8');
    if(name === 'dashboard.js') source = 'import {chartHost} from "./bklit_charts.js";\n'+
      source.slice(source.indexOf('function renderIntakeCard('),source.indexOf('\nfunction renderMetrics('))+
      '\nconst formatNumber=n=>String(n??0); export {renderIntakeCard};';
    const mod = new vm.SourceTextModule(source,{context});cache.set(name,mod);
    await mod.link(load);return mod;
  }
  const charts = await load('charts.js');await charts.evaluate();
  const dashboard = await load('dashboard.js');await dashboard.evaluate();
  return {charts:charts.namespace,dashboard:dashboard.namespace};
}
function specs(html) {
  return [...html.matchAll(/data-bklit-spec="([^"]+)"/g)].map(m=>JSON.parse(m[1].replaceAll('&quot;','"').replaceAll('&lt;','<').replaceAll('&gt;','>').replaceAll('&amp;','&')));
}
const sample = {person:'석지원',periodLabel:'2026년 10월',periodFrom:'2026-10-01',periodTo:'2026-10-31',totalEffort:1.25,issueCount:2,
  byType:[{label:'문의',amount:0},{label:'서버',amount:0},{label:'PC',amount:1.25},{label:'QC',amount:0}],
  dailyEffort:[{date:'2026-10-01',amount:1},{date:'2026-10-02',amount:0},{date:'2026-10-03',amount:.25}]};

test('dashboard KPI uses the existing daily registrations and preserves incomplete coverage',async()=>{
  const {dashboard} = await harness();
  const points=[{date:'2026-10-01',count:2},{date:'2026-10-02',count:0}];
  const html=dashboard.renderIntakeCard({newIssues:2,closedNow:1,newOpen:1},false,points);
  const spec=specs(html)[0];
  assert.equal(spec.kind,'sparkline');assert.equal(spec.compact,true);assert.equal(spec.complete,false);
  assert.deepEqual(spec.points,points);assert.match(html,/비교 미확정/);
  assert.match(html,/종료 1/);assert.match(html,/미종료 1/);
});
test('effort answer sends daily decimals to a bar sparkline and retains zeros and all types',async()=>{
  const {charts} = await harness();
  const html=charts.renderEffortChart(sample,{complete:false}).outerHTML;
  const spec=specs(html)[0];
  assert.equal(spec.kind,'sparkline');assert.equal(spec.sparklineVariant,'bar');
  assert.equal(spec.complete,false);assert.equal(spec.decimals,2);assert.equal(spec.unit,' 공수');
  assert.equal(spec.dateBasis,'고객사전달일');
  assert.deepEqual(spec.points,sample.dailyEffort.map(p=>({date:p.date,count:p.amount})));
  assert.match(html,/1\.25/);assert.match(html,/이상/);assert.match(html,/확인한 범위/);
  assert.match(html,/문의/);assert.match(html,/서버/);assert.match(html,/PC/);assert.match(html,/QC/);
  assert.match(html,/2026-10-02/);assert.match(html,/0\.00/); // 번들 실패에도 일별 표를 읽는다.
  assert.doesNotMatch(html,/data-bklit-kind="pie"/);
});
test('saved effort answers without daily data keep totals without fabricated dates',async()=>{
  const {charts} = await harness();
  const {dailyEffort,periodFrom,periodTo,...old} = sample;
  const html=charts.renderEffortChart(old).outerHTML;
  assert.equal(specs(html).length,0);assert.match(html,/1\.25/);
  assert.match(html,/일별 공수 데이터가 없는 저장된 답변/);
  const long=charts.renderEffortChart({...sample,dailyEffort:null}).outerHTML;
  assert.equal(specs(long).length,0);assert.match(long,/1년 이하/);
});
test('effort answer escapes labels and fallback dates and preserves zero-valued series',async()=>{
  const {charts} = await harness();
  const html=charts.renderEffortChart({...sample,person:'<img src=x>',byType:[{label:'<script>',amount:0}],
    totalEffort:0,dailyEffort:[{date:'<img src=x>',amount:0}]}).outerHTML;
  assert.doesNotMatch(html,/<img|<script/);assert.match(html,/&lt;img/);
  assert.equal(specs(html)[0].points[0].count,0);
});

test('effort card retains original answer in a collapsed disclosure without copying or rewriting it',async()=>{
  const {charts} = await harness();
  const original=element('div');original.textContent='전체 답변과 검증 안내';
  const card=charts.renderEffortChart(sample,{originalAnswer:original});
  const disclosure=card.children.find(child=>child.tag==='details');
  assert.ok(disclosure);assert.ok(!disclosure.open);
  assert.equal(disclosure.children.at(-1),original);
  assert.match(disclosure.outerHTML,/답변 원문 보기/);
  assert.equal(original.innerHTML,'전체 답변과 검증 안내');
});

test('chat collapses only completed new effort answers and passes partial coverage to the card',async()=>{
  const source=fs.readFileSync(path.join(dir,'chat.js'),'utf8');
  const original=element('div');const calls=[];
  const context=vm.createContext({renderEffortChart:(_chart,options)=>{calls.push(options);return element('div');},renderVerificationPanel:()=>element('div')});
  const module=new vm.SourceTextModule(source.slice(source.indexOf('function appendAssistantExtras('),source.indexOf('\nfunction conditionId('))+'\nexport {appendAssistantExtras};',{context});
  await module.link(()=>{});await module.evaluate();
  const container={appendChild(){},querySelector:()=>original};
  for(const [chart,streaming,expected] of [[sample,false,original],[sample,true,null],[{...sample,dailyEffort:undefined},false,null]]) {
    module.namespace.appendAssistantExtras(container,{effortChart:chart,streaming,verification:{dataScope:[{coverage:'부분 조회'}]}},0);
    assert.equal(calls.at(-1).originalAnswer,expected);assert.equal(calls.at(-1).complete,false);
  }
});
