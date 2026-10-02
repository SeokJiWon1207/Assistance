// 좁은 화면의 왼쪽 메뉴(사이드바 드로어) 열고 닫기. 대화·내 업무·대시보드·관리자 화면의
// 메뉴 버튼이 모두 이 함수를 써서 aria-expanded와 포커스 이동을 한 곳에서 맞춘다.
// 넓은 화면에서는 사이드바가 항상 보이므로 body.sidebar-open은 미디어쿼리 안에서만 의미가 있다.
import { els } from "./runtime.js?v=20260911-6";

let openedFrom = null;

function menuToggles() {
  return [els.sidebarMenuToggle, els.personalSidebarMenuToggle, els.dashboardSidebarMenuToggle, els.adminSidebarMenuToggle];
}

function isMobileSidebarOpen() {
  return document.body.classList.contains("sidebar-open");
}

function openMobileSidebar(trigger = null) {
  document.body.classList.add("sidebar-open");
  menuToggles().forEach((toggle) => toggle?.setAttribute("aria-expanded", "true"));
  openedFrom = trigger;
  // 메뉴가 열리면 닫기 버튼으로 옮겨 키보드·스크린리더가 메뉴 안에서 시작하게 한다.
  els.sidebarCloseBtn?.focus();
}

function closeMobileSidebar({ restoreFocus = true } = {}) {
  const wasOpen = isMobileSidebarOpen();
  document.body.classList.remove("sidebar-open");
  menuToggles().forEach((toggle) => toggle?.setAttribute("aria-expanded", "false"));
  const trigger = openedFrom;
  openedFrom = null;
  if (wasOpen && restoreFocus && trigger?.isConnected && trigger.offsetParent !== null) trigger.focus();
}

function toggleMobileSidebar(trigger = null) {
  if (isMobileSidebarOpen()) closeMobileSidebar();
  else openMobileSidebar(trigger);
}

export { closeMobileSidebar, isMobileSidebarOpen, openMobileSidebar, toggleMobileSidebar };
