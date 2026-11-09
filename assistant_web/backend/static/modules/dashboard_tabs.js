// 팀 대시보드 보기 탭. 운영 현황 외의 탭은 패널의 data-subview로 필터 줄·본문 자리를 대신 쓴다.
const tabViews = new Map([["overview", null]]);
let currentTab = "overview";
let bound = false;

function tabButtons() {
  return [...document.querySelectorAll("[data-dashboard-tab]")];
}

function panelElement() {
  return document.querySelector("#dashboardPanel");
}

function tabOrder() {
  return tabButtons().map((button) => button.dataset.dashboardTab).filter((tab) => tabViews.has(tab));
}

function registerDashboardTab(tab, { view, onShow = null }) {
  tabViews.set(tab, { view, onShow });
}

// 고객사 상세처럼 잠시 data-subview를 바꾼 화면이 닫힐 때도 이 함수로 현재 탭을 되살린다.
function applyDashboardTab() {
  tabButtons().forEach((button) => {
    const selected = button.dataset.dashboardTab === currentTab;
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  for (const [tab, entry] of tabViews) {
    const view = entry?.view();
    if (view) view.hidden = tab !== currentTab;
  }
  const panel = panelElement();
  if (currentTab !== "overview") panel?.setAttribute("data-subview", currentTab);
  else if (tabViews.has(panel?.getAttribute("data-subview"))) panel.removeAttribute("data-subview");
}

function selectDashboardTab(tab, { focus = false } = {}) {
  if (!tabViews.has(tab)) return;
  const changed = tab !== currentTab;
  currentTab = tab;
  applyDashboardTab();
  if (focus) tabButtons().find((button) => button.dataset.dashboardTab === tab)?.focus();
  if (changed) tabViews.get(tab)?.onShow?.();
}

// 고객사·공수·화면번호 탭은 기준 설명과 도구를 팀 현황과 같은 머리글 경로 줄 자리에 그린다.
// 칸은 index.html에 탭마다 하나씩 있고 CSS가 현재 탭(data-subview)의 칸만 보여 준다.
function tabHeaderSlots(tab) {
  return {
    subtitle: document.querySelector(`[data-tab-subtitle="${tab}"]`),
    tools: document.querySelector(`[data-tab-tools="${tab}"]`),
  };
}

function currentDashboardTab() {
  return currentTab;
}

function handleTabKey(event) {
  const order = tabOrder();
  const index = order.indexOf(currentTab);
  const keys = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: order.length - 1 };
  if (!(event.key in keys) || !order.length) return;
  event.preventDefault();
  selectDashboardTab(order[(keys[event.key] + order.length) % order.length], { focus: true });
}

function initDashboardTabs() {
  const tabs = document.querySelector("#dashboardTabs");
  if (!tabs) return;
  if (!bound) {
    tabs.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-dashboard-tab]");
      if (tab) selectDashboardTab(tab.dataset.dashboardTab);
    });
    tabs.addEventListener("keydown", handleTabKey);
    bound = true;
  }
  applyDashboardTab();
}

export { registerDashboardTab, applyDashboardTab, selectDashboardTab, currentDashboardTab, initDashboardTabs, tabHeaderSlots };
