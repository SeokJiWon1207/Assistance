const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag) { this.tagName=tag.toUpperCase();this.children=[];this.className='';this._text='';this.open=false;this.handlers={};
    this.classList={contains:name=>this.className.split(' ').includes(name),add:name=>this.className += ` ${name}`}; }
  appendChild(child) { if(child.parentElement) child.parentElement.children=child.parentElement.children.filter(c=>c!==child);this.children.push(child);child.parentElement=this;return child; }
  append(...children) { children.forEach(c=>this.appendChild(c)); }
  replaceChildren(...children) { this.children.forEach(c=>c.parentElement=null);this.children=[];this.append(...children); }
  set textContent(value) { this._text=String(value);this.children=[]; }
  get textContent() { return this._text+this.children.map(c=>c.textContent).join(''); }
  addEventListener(name,fn) { this.handlers[name]=fn; }
  setAttribute(name,value) { this[name]=value; }
  querySelector(selector) { return this.children.find(c=>selector.includes(`.${c.className}`) && c.className) || null; }
}
const el=(tag,text='',className='')=>{const e=new Element(tag);e.textContent=text;e.className=className;return e;};
const heading=text=>el('h3',text,'answer-heading');
async function harness() {
  const context=vm.createContext({document:{createElement:tag=>new Element(tag)}});
  const module=new vm.SourceTextModule(fs.readFileSync(path.join(__dirname,'../backend/static/modules/answer_disclosure.js'),'utf8'),{context});
  await module.link(()=>{});await module.evaluate();return module.namespace;
}

test('short answers and single paragraphs remain readable without an empty disclosure',async()=>{
  const api=await harness(), root=el('div');
  root.append(el('p','완료했습니다.'));
  assert.equal(api.collapseAnswerBody(root,{}),null);
  root.children[0].textContent='한 문단 '.repeat(300);
  assert.equal(api.collapseAnswerBody(root,{}),null);
  assert.equal(root.children.length,1);
});

test('explicit conclusion moves before long detail with the original nodes and warning intact',async()=>{
  const api=await harness(), root=el('div'), message={content:'저장된 원문'};
  const background=el('p','긴 분석 근거입니다. '.repeat(90));
  const result=el('p','원인 후보는 재연결 처리입니다.');
  const warning=el('div','일부 이슈만 확인한 결과입니다.','answer-callout callout-warning');
  const table=el('div','실제 결과 표','markdown-table-wrap');
  root.append(heading('배경'),background,heading('핵심 요약'),result,warning,table);
  const detail=api.collapseAnswerBody(root,message);
  assert.equal(root.children[0].textContent,'핵심 요약');
  assert.equal(root.children[1],result);
  assert.ok(root.children.includes(warning));
  assert.ok(root.children.includes(table));
  assert.equal(detail.open,false);
  assert.ok(detail.textContent.includes(background.textContent));
  assert.equal(background.parentElement.className,'answer-disclosure-body');
  assert.equal(message.content,'저장된 원문');
});

test('chart descriptions fold after the lead while incomplete lookup notices stay visible',async()=>{
  const api=await harness(), root=el('div');
  const lead=el('p','월별 추이입니다.'), totals=el('ul','유형별 합계'), caveat=el('p','기한 미등록 3건 제외');
  root.append(lead,totals,caveat);
  const detail=api.collapseAnswerBody(root,{}, {hasChart:true});
  assert.equal(root.children[0],lead);
  assert.ok(root.children.includes(caveat));
  assert.ok(detail.textContent.includes('유형별 합계'));
});

test('a primary result table and an already collapsed quote are never nested in another disclosure',async()=>{
  const api=await harness(), root=el('div');
  const table=el('div','표 내용'.repeat(400),'issue-table-wrap');
  const quote=el('details','원문 내용'.repeat(400),'answer-source-history');
  root.append(el('p','조회된 결과입니다.'),table,quote);
  assert.equal(api.collapseAnswerBody(root,{}),null);
  assert.deepEqual(root.children.slice(1),[table,quote]);
});

test('disclosure state survives rerender and is isolated between answer objects',async()=>{
  const api=await harness(), message={};
  const body=()=>{const root=el('div');root.append(el('p','요약'),el('p','상세 '.repeat(400)));return root;};
  const opened=api.collapseAnswerBody(body(),message);
  opened.open=true;opened.handlers.toggle();
  assert.equal(api.collapseAnswerBody(body(),message).open,true);
  assert.equal(api.collapseAnswerBody(body(),{}).open,false);
});

test('related sources keep their clickable elements and use their own disclosure state',async()=>{
  const api=await harness(), message={}, sources=el('section','','source-list'), link=el('button','#123 원문 보기');
  sources.append(link);
  const result=api.wrapRelatedSources(sources,message,12);
  assert.equal(result.open,false);
  assert.ok(result.textContent.includes('12건'));
  assert.equal(result.children.at(-1),sources);
  assert.equal(sources.children[0],link);
});

test('presentation places explanation after the chart and skips streaming and existing effort disclosures',async()=>{
  const api=await harness();
  const fixture=()=>{
    const container=el('div'),bubble=el('div','','bubble'),body=el('div','','assistant-answer'),chart=el('section','월별 결과');
    body.append(el('p','월별 추이입니다.'),el('p','집계 설명 '.repeat(200)));
    bubble.append(body);container.append(bubble,chart);
    container.querySelector=()=>body;
    return {container,body,chart};
  };
  const completed=fixture();
  api.presentAssistantAnswer(completed.container,{effortTrend:{}});
  assert.equal(completed.body.children.length,1);
  assert.equal(completed.container.children[1],completed.chart);
  assert.equal(completed.container.children[2].tagName,'DETAILS');
  for (const pending of [{loading:true},{streaming:true},{effortChart:{dailyEffort:[]}}]) {
    const sample=fixture();api.presentAssistantAnswer(sample.container,pending);
    assert.equal(sample.body.children.length,2);
    assert.equal(sample.container.children.length,2);
  }
  const sources=el('section');
  assert.equal(api.wrapRelatedSources(sources,{streaming:true},3),sources);
});
