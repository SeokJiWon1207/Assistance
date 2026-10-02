// 앱 틀의 접히는 메뉴 두 가지를 한 곳에서 다룬다.
// - 대화 목록(사이드바): 좁은 화면에서 대화 화면 헤더의 메뉴 버튼으로 여는 왼쪽 드로어.
//   넓은 화면에서는 대화 화면에서 항상 보이므로 body.sidebar-open은 미디어쿼리 안에서만 의미가 있다.
// - 내 계정 메뉴: 주 메뉴(레일·아래 탭바)의 '내 계정' 버튼으로 여는 작은 메뉴.
import { els } from "./runtime.js?v=20260911-6";

let openedFrom = null;

function isMobileSidebarOpen() {
  return document.body.classList.contains("sidebar-open");
}

function openMobileSidebar(trigger = null) {
  document.body.classList.add("sidebar-open");
  els.sidebarMenuToggle?.setAttribute("aria-expanded", "true");
  openedFrom = trigger;
  // 메뉴가 열리면 닫기 버튼으로 옮겨 키보드·스크린리더가 메뉴 안에서 시작하게 한다.
  els.sidebarCloseBtn?.focus();
}

function closeMobileSidebar({ restoreFocus = true } = {}) {
  const wasOpen = isMobileSidebarOpen();
  document.body.classList.remove("sidebar-open");
  els.sidebarMenuToggle?.setAttribute("aria-expanded", "false");
  const trigger = openedFrom;
  openedFrom = null;
  if (wasOpen && restoreFocus && trigger?.isConnected && trigger.offsetParent !== null) trigger.focus();
}

function toggleMobileSidebar(trigger = null) {
  if (isMobileSidebarOpen()) closeMobileSidebar();
  else openMobileSidebar(trigger);
}

function isAccountMenuOpen() {
  return Boolean(els.accountMenu && !els.accountMenu.hidden);
}

function openAccountMenu() {
  if (!els.accountMenu) return;
  els.accountMenu.hidden = false;
  els.accountMenuBtn?.setAttribute("aria-expanded", "true");
}

function closeAccountMenu({ restoreFocus = false } = {}) {
  if (!isAccountMenuOpen()) return;
  els.accountMenu.hidden = true;
  els.accountMenuBtn?.setAttribute("aria-expanded", "false");
  if (restoreFocus) els.accountMenuBtn?.focus();
}

function toggleAccountMenu() {
  if (isAccountMenuOpen()) closeAccountMenu();
  else openAccountMenu();
}

export {
  closeAccountMenu,
  closeMobileSidebar,
  isAccountMenuOpen,
  isMobileSidebarOpen,
  openAccountMenu,
  openMobileSidebar,
  toggleAccountMenu,
  toggleMobileSidebar,
};
