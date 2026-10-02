const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function loadSidebar() {
  const classes = new Set();
  let focused = '';
  const button = (name, visible = true) => ({
    name, attributes: {}, isConnected: true, offsetParent: visible ? {} : null,
    setAttribute(key, value) { this.attributes[key] = value; },
    focus() { focused = name; },
  });
  const els = {
    sidebarMenuToggle: button('chatMenu'),
    personalSidebarMenuToggle: button('personalMenu', false),
    dashboardSidebarMenuToggle: button('dashboardMenu', false),
    adminSidebarMenuToggle: null,
    sidebarCloseBtn: button('closeBtn'),
  };
  const document = { body: { classList: {
    add: (name) => classes.add(name), remove: (name) => classes.delete(name), contains: (name) => classes.has(name),
  } } };
  const context = vm.createContext({ document });
  const module = new vm.SourceTextModule(
    fs.readFileSync(path.join(__dirname, '../backend/static/modules/sidebar.js'), 'utf8'), { context },
  );
  await module.link(() => new vm.SyntheticModule(['els'], function () { this.setExport('els', els); }, { context }));
  await module.evaluate();
  return { api: module.namespace, els, classes, focused: () => focused };
}

test('opening the drawer marks every menu button and moves focus to the close button', async () => {
  const h = await loadSidebar();
  h.api.openMobileSidebar(h.els.sidebarMenuToggle);
  assert.equal(h.api.isMobileSidebarOpen(), true);
  for (const name of ['sidebarMenuToggle', 'personalSidebarMenuToggle', 'dashboardSidebarMenuToggle']) {
    assert.equal(h.els[name].attributes['aria-expanded'], 'true');
  }
  assert.equal(h.focused(), 'closeBtn');
});

test('closing returns focus to the visible menu button that opened it', async () => {
  const h = await loadSidebar();
  h.api.toggleMobileSidebar(h.els.sidebarMenuToggle);
  h.api.toggleMobileSidebar(h.els.sidebarMenuToggle);
  assert.equal(h.api.isMobileSidebarOpen(), false);
  assert.equal(h.els.sidebarMenuToggle.attributes['aria-expanded'], 'false');
  assert.equal(h.focused(), 'chatMenu');
});

test('closing for navigation or when already closed does not steal focus', async () => {
  const h = await loadSidebar();
  h.api.closeMobileSidebar();
  assert.equal(h.focused(), '');
  h.api.openMobileSidebar(h.els.sidebarMenuToggle);
  h.api.closeMobileSidebar({ restoreFocus: false });
  assert.equal(h.focused(), 'closeBtn');
  // 숨겨진 화면의 메뉴 버튼으로는 포커스를 돌려주지 않는다.
  h.api.openMobileSidebar(h.els.personalSidebarMenuToggle);
  h.api.closeMobileSidebar();
  assert.equal(h.focused(), 'closeBtn');
});
