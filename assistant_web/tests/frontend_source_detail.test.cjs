const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = '') { this.tagName = tag.toUpperCase(); }
  children = [];
  _text = '';
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { children.forEach(child => this.appendChild(child)); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
}

async function detail() {
  const directory = path.join(__dirname, '../backend/static/modules');
  const context = vm.createContext({ document: {
    createElement: (tag) => new Element(tag), createDocumentFragment: () => new Element(),
  } });
  const format = new vm.SourceTextModule(fs.readFileSync(path.join(directory, 'ui_format.js'), 'utf8'), { context });
  await format.link(() => {});
  await format.evaluate();
  const module = new vm.SourceTextModule(fs.readFileSync(path.join(directory, 'chat_source_detail.js'), 'utf8'), { context });
  await module.link(specifier => {
    if (specifier.includes('ui_format')) return format;
    const values = specifier.includes('dialog_focus') ? { openDialogFocus() {}, closeDialogFocus() {} }
      : specifier.includes('runtime') ? { state: {}, els: {}, apiFetch() { throw Error('unexpected request'); } }
      : specifier.includes('recent_items') ? { recordIssueView() {} } : { formatNotionDetail() {} };
    return new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  });
  await module.evaluate();
  return module.namespace;
}

function descendants(node) {
  return node.children.flatMap(child => [child, ...descendants(child)]);
}

function fields(rendered) {
  return Object.fromEntries(descendants(rendered).filter(child => child.className === 'detail-grid')
    .flatMap(grid => grid.children.map(item => item.children.map(child => child.textContent))));
}

test('live and saved detail use source labels while prose keeps original codes and timestamps', async () => {
  const api = await detail();
  const original = '[Mantis 이슈 정보]\n- 현재 상태: developed\n- 이슈 유형: minor\n- 우선순위: normal\n'
    + '- 상태 표시명: 개발완료\n- 이슈 유형 표시명: 개선\n- 우선순위 표시명: 보통\n'
    + '- 등록일: 2026-11-05T13:37:28+09:00\n- 최종수정일: 2026-11-05T05:37:04Z\n\n'
    + '[고객사 본문 요청 내용]\ndeveloped minor normal 2026-11-05T13:37:28+09:00';
  const rendered = api.renderDetailContent(original, '');
  const values = fields(rendered);
  assert.equal(values['현재 상태'], '개발완료');
  assert.equal(values['이슈 유형'], '개선');
  assert.equal(values['우선순위'], '보통');
  assert.equal(values['등록일'], '2026-11-05 오후 1시 37분');
  assert.equal(values['최종수정일'], '2026-11-05 오후 2시 37분');
  assert.match(rendered.children.at(-1).textContent, /developed minor normal 2026-11-05T13:37:28\+09:00$/);
  const custom = fields(api.renderDetailContent('[정보]\n- 현재 상태: developed\n- 상태 표시명: 고객 확인 대기\n- 이슈 유형: crash\n- 이슈 유형 표시명: 추가', ''));
  assert.equal(custom['현재 상태'], '고객 확인 대기');
  assert.equal(custom['이슈 유형'], '추가');
});

test('older detail translates known codes and keeps unknown codes, date-only values and missing dates', async () => {
  const api = await detail();
  assert.equal(api.buildDetailValue('현재 상태', 'Developed').textContent, '개발완료');
  assert.equal(api.buildDetailValue('우선순위', 'high').textContent, '높음');
  assert.equal(api.buildDetailValue('이슈 유형', 'minor').textContent, '개선');
  assert.equal(api.buildDetailValue('현재 상태', 'custom-status').textContent, 'custom-status');
  assert.equal(api.buildDetailValue('고객사요청일', '2026-11-05').textContent, '2026-11-05');
  assert.equal(api.buildDetailValue('등록일', '미상').textContent, '미상');
  assert.equal(api.buildDetailValue('최종수정일', '날짜 확인 필요').textContent, '날짜 확인 필요');
  assert.equal(api.buildDetailValue('등록일', '2026-11-05T00:05:28+09:00').textContent, '2026-11-05 오전 12시 5분');
  assert.equal(api.buildDetailValue('등록일', '2026-11-05T12:00:28').textContent, '2026-11-05 오후 12시 0분');
});

test('Mantis extra metadata and schedules fold without dropping values or interpreting original HTML', async () => {
  const api = await detail();
  const prose = '<img src=x onerror="alert(1)"> 원문 그대로';
  const rendered = api.renderDetailContent('[Mantis 이슈 정보]\n- 이슈 ID: #41\n- 현재 상태: developed\n- 담당자: 담당자 미지정\n- 매체구분: HTS\n- 작업주체: 미상\n\n'
    + '[일정/전달일]\n- 고객사전달일: 미상\n\n[공수]\n- 문의공수: 0.00\n\n[고객사 본문 요청 내용]\n' + prose, '');
  const disclosures = descendants(rendered).filter(node => node.tagName === 'DETAILS');
  assert.equal(disclosures.length, 3);
  assert.ok(disclosures.every(node => !node.open));
  assert.equal(disclosures[0].children[0].textContent, '추가 정보 2개');
  assert.deepEqual(fields(rendered), { '이슈 ID': '#41', '현재 상태': '개발완료', '담당자': '담당자 미지정', '매체구분': 'HTS', '작업주체': '미상', '고객사전달일': '미상', '문의공수': '0.00' });
  assert.equal(descendants(rendered).find(node => node.className === 'detail-prose').textContent, prose);
  assert.equal(descendants(rendered).filter(node => node.tagName === 'IMG').length, 0);
});

test('other source formats and unstructured original text remain fully visible', async () => {
  const api = await detail();
  const rendered = api.renderDetailContent('[정보]\n- 담당자: 담당자\n\n[공수]\n- 문의공수: 0.00', '');
  assert.equal(descendants(rendered).filter(node => node.tagName === 'DETAILS').length, 0);
  assert.equal(fields(rendered)['문의공수'], '0.00');
  const plain = '표식 없는 원문\n<script>alert(1)</script>';
  assert.equal(api.renderDetailContent(plain, '').children[0].textContent, plain);
});
