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
    sidebarCloseBtn: button('closeBtn'),
    accountMenuBtn: button('accountBtn'),
    accountMenu: { hidden: true },
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

test('opening the drawer marks the chat menu button and moves focus to the close button', async () => {
  const h = await loadSidebar();
  h.api.openMobileSidebar(h.els.sidebarMenuToggle);
  assert.equal(h.api.isMobileSidebarOpen(), true);
  assert.equal(h.els.sidebarMenuToggle.attributes['aria-expanded'], 'true');
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
  // 화면에서 사라진 메뉴 버튼으로는 포커스를 돌려주지 않는다.
  h.els.sidebarMenuToggle.offsetParent = null;
  h.api.openMobileSidebar(h.els.sidebarMenuToggle);
  h.api.closeMobileSidebar();
  assert.equal(h.focused(), 'closeBtn');
});

test('account menu toggles with aria-expanded and Esc-style close returns focus to its button', async () => {
  const h = await loadSidebar();
  h.api.toggleAccountMenu();
  assert.equal(h.api.isAccountMenuOpen(), true);
  assert.equal(h.els.accountMenuBtn.attributes['aria-expanded'], 'true');
  h.api.toggleAccountMenu();
  assert.equal(h.els.accountMenu.hidden, true);
  assert.equal(h.focused(), '');
  h.api.openAccountMenu();
  h.api.closeAccountMenu({ restoreFocus: true });
  assert.equal(h.els.accountMenuBtn.attributes['aria-expanded'], 'false');
  assert.equal(h.focused(), 'accountBtn');
  // 이미 닫혀 있으면 포커스를 옮기지 않는다.
  h.api.closeAccountMenu({ restoreFocus: true });
  assert.equal(h.api.isAccountMenuOpen(), false);
});
