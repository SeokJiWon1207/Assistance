const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const directory = JSON.parse(fs.readFileSync(path.join(__dirname, '../config/customer_directory.json'))).customers;

async function format() {
  const mod = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/ui_format.js'), 'utf8'), {context: vm.createContext({})});
  await mod.link(() => {}); await mod.evaluate();
  mod.namespace.setCustomerNames({exact: Object.fromEntries(Object.entries(directory).flatMap(([key, value]) => [key, value.name, ...value.aliases, ...value.projects].map(alias => [alias, value.name]))), projects: Object.fromEntries(Object.values(directory).flatMap(value => value.projects.map(project => [project, value.name])))});
  return mod.namespace;
}

test('one directory formats all configured projects and preserves unmapped identities', async () => {
  const api = await format();
  for (const {name, projects} of Object.values(directory)) {
    for (const project of projects) for (const suffix of ['', '_Project', '(구 고객사)', '(구 고객사)_Project']) {
      assert.equal(api.customerName(project + suffix), name);
      assert.equal(api.customerLabelText(`${project}${suffix} 증가 원인 확인`), `${name} 증가 원인 확인`);
    }
  }
  assert.equal(api.customerLabelText("108SK(SK증권)에서 발생"), "SK증권에서 발생");
  assert.equal(api.customerLabelText("147MiraeAsset(미래에셋증권)만 보여줘"), "미래에셋증권만 보여줘");
  assert.equal(api.customerName('147MiraeAssetXYZ'), '147MiraeAssetXYZ');
  assert.equal(api.customerName('900DRFN(공용)'), '900DRFN(공용)');
  assert.equal(api.customerName('125Daewoo'), '미래에셋증권(구 대우증권)');
  assert.equal(api.customerName('136INNI_ETRADE'), '미래에셋증권 인도네시아');
  assert.equal(api.customerName({project:'159eBest',customer_name:'예전 이름'}), 'LS증권');
});

test('saved answers normalize labels while quoted source, inline code and URLs stay literal', async () => {
  const api = await format();
  const content = '고객사별 건수\n- 147MiraeAsset(미래에셋증권) — 4건\n| 159eBest | 2 |\n> ```text\n> 147MiraeAsset 원문\n> ```\n```log\n159eBest literal\n```\n`147MiraeAsset` https://host/159eBest\n끝';
  const formatted = api.customerAnswerText(content);
  assert.match(formatted, /- 미래에셋증권 — 4건/);
  assert.match(formatted, /\| LS증권 \| 2 \|/);
  assert.match(formatted, /> 147MiraeAsset 원문/);
  assert.match(formatted, /159eBest literal/);
  assert.match(formatted, /`147MiraeAsset` https:\/\/host\/159eBest/);
  assert.equal(api.customerAnswerText(formatted), formatted);
});

test('Mantis and saved Chroma detail normalize only the customer metadata field', async () => {
  const naming = await format();
  const context = vm.createContext({document:{createElement:tagName => ({tagName,textContent:'',className:'',children:[],appendChild(child){this.children.push(child);}})}});
  const source = new vm.SourceTextModule(fs.readFileSync(path.join(__dirname, '../backend/static/modules/chat_source_detail.js'), 'utf8'), {context});
  await source.link(specifier => {
    const values = specifier.includes('ui_format') ? naming : specifier.includes('dialog_focus') ? {openDialogFocus(){},closeDialogFocus(){}} : specifier.includes('runtime') ? {els:{},apiFetch(){throw Error('unexpected request');}} : {formatNotionDetail(){}};
    return new vm.SyntheticModule(Object.keys(values), function(){for(const [key,value] of Object.entries(values))this.setExport(key,value);}, {context});
  });
  await source.evaluate();
  const api=source.namespace;
  const original='108SK(SK증권) technical log';
  const issue={issue_id:7,project:'108SK(SK증권)',preview:original,detail:{}};
  const text=api.formatMantisDetail(issue);
  assert.match(text, /프로젝트\(고객사\): SK증권/);
  assert.match(text, /108SK\(SK증권\) technical log/);
  assert.equal(api.buildDetailValue('프로젝트(고객사)','159eBest').textContent, 'LS증권');
  assert.equal(api.buildDetailValue('본문',original).textContent, original);
  assert.equal(issue.project,'108SK(SK증권)');
});

test('copy uses the displayed customer names without changing stored evidence or messages', async () => {
  const naming=await format();
  const source=fs.readFileSync(path.join(__dirname,'../backend/static/modules/events.js'),'utf8');
  const start=source.indexOf('  const actionButton = event.target.closest("[data-message-action]");');
  const end=source.indexOf('    } else if (actionButton.dataset.messageAction === "edit")',start);
  let copied;
  const original='159eBest — 2건\n> 159eBest 원문';
  const button={dataset:{messageIndex:'0',messageAction:'copy'},innerHTML:'복사'};
  const state={messages:[{role:'assistant',content:original}],busy:false};
  vm.runInNewContext(`(() => { ${source.slice(start,end)}\n }\n }\n })()`,{
    event:{target:{closest:()=>button}},state,customerAnswerText:naming.customerAnswerText,
    navigator:{clipboard:{writeText:async text=>{copied=text;}}},
    MESSAGE_ACTION_ICONS:{check:'확인'},setTimeout(){},showToast(){},
  });
  assert.equal(copied,'LS증권 — 2건\n> 159eBest 원문');
  assert.equal(state.messages[0].content,original);
});
