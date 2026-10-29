const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function load(name, globals = {}) {
  const context = vm.createContext(globals);
  const module = new vm.SourceTextModule(fs.readFileSync(
    path.join(__dirname, `../backend/static/modules/${name}.js`), 'utf8'), { context });
  await module.link(() => {});
  await module.evaluate();
  return module.namespace;
}

test('one condition bar retains sorting and multi-value filters without changing removal payloads', async () => {
  const { answerSearchPresentation } = await load('search_conditions');
  const customer = { id: 'customer', label: '고객사', value: '신한', values: ['신한', '신한투자증권'] };
  const people = { id: 'people', label: '담당자/인물', value: '김민수, 이민수', values: ['김민수', '이민수'] };
  const message = { content: '2건을 찾았습니다.\n\n검색 기준: 고객사=신한투자증권, 담당자=김민수, 이민수, 정렬: 최신 이슈 ID 기준\n\n이슈 목록', verification: { constraints: [customer, people] } };
  const result = answerSearchPresentation(message);
  assert.doesNotMatch(result.content, /검색 기준:/);
  assert.match(result.content, /2건을 찾았습니다/);
  assert.match(result.content, /이슈 목록/);
  assert.equal(result.conditions.length, 3);
  assert.equal(result.conditions[0].value, '신한투자증권');
  assert.equal(result.conditions[0].removeCondition, customer);
  assert.equal(customer.value, '신한');
  assert.equal(result.conditions[1].label, '담당자');
  assert.equal(result.conditions[1].value, '김민수, 이민수');
  assert.equal(result.conditions[2].value, '최신 이슈 ID 기준');
  assert.equal(result.conditions[2].removeCondition, undefined);
});

test('older and streaming answers retain Notion title, keywords and unverified filters as read-only criteria', async () => {
  const { answerSearchPresentation } = await load('search_conditions');
  const result = answerSearchPresentation({ content: '검색 기준: Notion, 정확 제목=배포, 장애 대응, 키워드=오류, 주문, 제외 상태=종료, 보류\n결과입니다.' });
  assert.deepEqual(Array.from(result.conditions, (item) => item.value), ['Notion', '배포, 장애 대응', '오류, 주문', '종료, 보류']);
  assert.ok(result.conditions.every((item) => !item.removeCondition));
  assert.equal(result.content.trim(), '결과입니다.');
  assert.equal(answerSearchPresentation({ content: '일반 설명' }).conditions.length, 0);
  const keywords = answerSearchPresentation({ content: '검색 기준: 상태=new, keyword 포함 주문, 오류, title 미포함 배포, 정렬: 최신 이슈 ID 기준' });
  assert.deepEqual(Array.from(keywords.conditions, (item) => item.label), ['상태', '키워드', '제목', '정렬']);
  assert.equal(keywords.conditions[1].value, '포함 주문, 오류');
});

test('saved aggregation answers with legacy constraint objects keep their content visible', async () => {
  const { answerSearchPresentation } = await load('search_conditions');
  const content = '합계: 등록 건수 68, 공수 72.15\n\n| 고객사 | 월 | 등록 건수 |\n| A | 2026-09 | 68 |';
  const result = answerSearchPresentation({ content, verification: { constraints: { platform_key: 'MTS' } } });
  assert.equal(result.content, content);
  assert.equal(result.conditions.length, 0);
});

test('count scope metadata stays outside removable date and keyword conditions', async () => {
  const { answerSearchPresentation } = await load('search_conditions');
  const date = { id: 'created_at', label: '등록일', value: '2025년', values: ['2025'] };
  const dateOnly = answerSearchPresentation({
    content: '검색 기준: 등록일=2025년, Mantis 전체 저장본 31233건 기준\n총 16건입니다.',
    verification: { constraints: [date] },
  });
  assert.equal(dateOnly.conditions[0].value, '2025년');
  assert.equal(dateOnly.conditions[1].label, '조회 범위');
  for (const scope of ['Mantis 전체 저장본 31233건 기준', 'Mantis 스냅샷 + 실시간 병합 기준', '최신 100건 기준']) {
    const result = answerSearchPresentation({
      content: `검색 기준: 등록일=2025년 1월 1일~2025년 12월 31일, keyword 포함 차트, 오류, ${scope}, 저장본 수집 시각=2026-10-12T00:00:00Z\n총 16건입니다.`,
      verification: { constraints: [date] },
    });
    assert.equal(result.conditions[0].value, '2025년 1월 1일~2025년 12월 31일');
    assert.equal(result.conditions[0].removeCondition, date);
    assert.equal(result.conditions[1].value, '포함 차트, 오류');
    assert.equal(result.conditions[2].label, '조회 범위');
    assert.equal(result.conditions[2].value, scope);
    assert.equal(result.conditions[2].removeCondition, undefined);
  }
});

test('every date formatter uses Korean local time at minute precision and retains historical years', async () => {
  const { formatUiDateTime } = await load('ui_format');
  const now = new Date(2026, 9, 6, 9, 30);
  const options = { now };
  assert.equal(formatUiDateTime(new Date(2026, 9, 6, 9, 7, 52), options), '10월 6일 오전 9:07');
  assert.equal(formatUiDateTime(new Date(2026, 9, 6, 9, 7, 52), { ...options, compact: true }), '오전 9:07');
  assert.equal(formatUiDateTime(new Date(2025, 9, 6, 9, 7, 52), options), '2025년 10월 6일 오전 9:07');
  assert.equal(formatUiDateTime('시각 미확인', options), '시각 미확인');
  assert.equal(formatUiDateTime('', options), '');
});

function dialogHarness() {
  const document = { activeElement: null, querySelectorAll: () => [], body: { classList: { add() {}, remove() {} } } };
  const element = (name, { disabled = false, visible = true } = {}) => ({
    name, disabled, tabIndex: 0, isConnected: true,
    getClientRects: () => visible ? [{}] : [],
    focus() { document.activeElement = this; },
  });
  const opener = element('opener');
  const first = element('close');
  const last = element('save');
  const hidden = element('hidden', { visible: false });
  const disabled = element('disabled', { disabled: true });
  const handlers = new Map();
  const dialog = {
    querySelectorAll: () => [first, hidden, disabled, last],
    contains: (target) => [first, last, hidden, disabled, dialog].includes(target),
    addEventListener: (type, handler) => handlers.set(type, handler),
    removeEventListener: (type) => handlers.delete(type),
    focus() { document.activeElement = this; },
  };
  opener.focus();
  const press = (key, shiftKey = false) => {
    const event = { key, shiftKey, prevented: false, stopped: false,
      preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
    handlers.get('keydown')?.(event);
    return event;
  };
  return { document, dialog, opener, first, last, press, handlers, element };
}

test('dialog keyboard navigation stays inside and Escape restores its opener once', async () => {
  const h = dialogHarness();
  const api = await load('dialog_focus', { document: h.document });
  api.openDialogFocus(h.dialog, { onClose: () => api.closeDialogFocus(h.dialog) });
  assert.equal(h.document.activeElement, h.first);
  assert.equal(h.press('Tab', true).prevented, true);
  assert.equal(h.document.activeElement, h.last);
  assert.equal(h.press('Tab').prevented, true);
  assert.equal(h.document.activeElement, h.first);
  // 내용을 갱신해도 처음 창을 연 버튼으로 돌아간다.
  api.openDialogFocus(h.dialog);
  // 패널 안의 폼이 교체되어 포커스가 사라지면 새 입력란으로 옮긴다.
  h.document.activeElement = null;
  api.openDialogFocus(h.dialog, { initialFocus: h.last });
  assert.equal(h.document.activeElement, h.last);
  const escape = h.press('Escape');
  assert.equal(escape.stopped, true);
  assert.equal(h.document.activeElement, h.opener);
  assert.equal(h.handlers.size, 0);
});

test('dialog close restores a usable fallback when rendering replaced its opener', async () => {
  const h = dialogHarness();
  const fallback = h.element('replacement feedback button');
  const api = await load('dialog_focus', { document: h.document });
  api.openDialogFocus(h.dialog, { fallback: () => fallback });
  h.opener.isConnected = false;
  api.closeDialogFocus(h.dialog);
  assert.equal(h.document.activeElement, fallback);
});


test('UI clocks stay Korean across ICU day-period differences and midnight/noon', async () => {
  const { formatUiDateTime } = await load('ui_format', { Intl: { DateTimeFormat: class {
    format() { return 'AM 9:07'; }
  } } });
  for (const [hour, expected] of [[0, '오전 12:07'], [9, '오전 9:07'], [12, '오후 12:07'], [23, '오후 11:07']]) {
    const now = new Date(2026, 9, 6, hour, 7);
    assert.equal(formatUiDateTime(now, { now, compact: true }), expected);
  }
});
