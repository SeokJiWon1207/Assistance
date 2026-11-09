// 넓은 화면의 내 업무·팀 대시보드에서 왼쪽 막대를 글자 사이드바로 펼친다(참고: Snow 대시보드).
// 하위 메뉴는 화면 안의 기존 탭을 그대로 누르는 바로가기라 탭 로직·키보드 처리는 각 화면 모듈이 맡는다.
import { state } from "./runtime.js?v=20260911-6";
import { readRecentIssues } from "./recent_items.js?v=20261105-1";

const COLLAPSE_KEY = "team-ai-assistant-rail-collapsed-v1";
const SIDE_RAIL_KEY = "team-ai-assistant-side-rail-hidden-v1";
const SHORTCUT_LIMIT = 4;
const TAB_SELECTORS = {
  workspace: (tab) => `[data-workspace-tab="${tab}"]`,
  dashboard: (tab) => `[data-dashboard-tab="${tab}"]`,
};
const VIEW_OF = { workspace: "personal", dashboard: "dashboard" };
let shortcutKind = "favorites";
let handlers = {};

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function readFlag(key) {
  try { return globalThis.localStorage?.getItem(key) === "1"; } catch { return false; }
}

function writeFlag(key, value) {
  try { globalThis.localStorage?.setItem(key, value ? "1" : "0"); } catch { /* 저장이 막혀도 이번 화면에는 적용된다. */ }
}

function shell() {
  return document.querySelector(".app-shell");
}

// 하위 메뉴의 현재 항목과 경로 줄의 현재 보기 이름을 화면 탭의 선택 상태에 맞춘다.
function syncSidebar() {
  for (const [group, selector] of Object.entries(TAB_SELECTORS)) {
    const selected = [...document.querySelectorAll(selector("").replace('=""', ""))].find((tab) => tab.getAttribute("aria-selected") === "true");
    const key = selected?.dataset.workspaceTab || selected?.dataset.dashboardTab || "";
    for (const item of document.querySelectorAll(`[data-rail-tab^="${group}:"]`)) {
      const current = item.dataset.railTab === `${group}:${key}`;
      if (current) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }
    const crumb = document.querySelector(`[data-crumb-current="${group}"]`);
    const label = document.querySelector(`[data-rail-tab="${group}:${key}"]`)?.textContent.trim() || "";
    if (crumb) crumb.textContent = label;
  }
}

function shortcutRows() {
  if (shortcutKind === "recent") {
    return readRecentIssues(state.currentUser?.uid).slice(0, SHORTCUT_LIMIT).map((issue) => ({
      attr: `data-rail-issue="${escapeText(issue.id)}"`, label: `#${issue.id} ${issue.title || ""}`.trim(),
    }));
  }
  return (state.conversations || []).filter((conversation) => conversation.favorite).slice(0, SHORTCUT_LIMIT).map((conversation) => ({
    attr: `data-rail-conversation="${escapeText(conversation.id)}"`, label: conversation.title || "새 대화",
  }));
}

function renderShortcuts() {
  const list = document.querySelector("#railShortcutList");
  if (!list) return;
  for (const button of document.querySelectorAll("[data-rail-shortcuts]")) {
    button.setAttribute("aria-pressed", String(button.dataset.railShortcuts === shortcutKind));
  }
  const rows = shortcutRows();
  list.innerHTML = rows.length
    ? rows.map((row) => `<li><button type="button" class="rail-shortcut" ${row.attr}><i aria-hidden="true"></i><span>${escapeText(row.label)}</span></button></li>`).join("")
    : `<li class="rail-shortcut-empty">${shortcutKind === "recent" ? "최근 본 이슈가 없어요." : "즐겨찾기한 대화가 없어요."}</li>`;
}

function applyFlags() {
  const collapsed = readFlag(COLLAPSE_KEY);
  const sideHidden = readFlag(SIDE_RAIL_KEY);
  document.body.classList.toggle("rail-collapsed", collapsed);
  document.body.classList.toggle("side-rail-hidden", sideHidden);
  for (const button of document.querySelectorAll("[data-rail-collapse]")) {
    button.setAttribute("aria-pressed", String(collapsed));
    button.setAttribute("aria-label", collapsed ? "왼쪽 메뉴 펼치기" : "왼쪽 메뉴 접기");
  }
  for (const button of document.querySelectorAll("[data-side-rail-toggle]")) {
    button.setAttribute("aria-pressed", String(!sideHidden));
    button.setAttribute("aria-label", sideHidden ? "오른쪽 패널 펼치기" : "오른쪽 패널 접기");
  }
}

function openRailTab(value) {
  const [group, tab] = value.split(":");
  const view = VIEW_OF[group];
  if (shell()?.dataset.view !== view) {
    if (view === "personal") handlers.showPersonalView?.();
    else handlers.showDashboardView?.();
  }
  document.querySelector(TAB_SELECTORS[group](tab))?.click();
  syncSidebar();
}

function handleClick(event) {
  const target = event.target;
  const railTab = target.closest?.("[data-rail-tab]");
  if (railTab) return openRailTab(railTab.dataset.railTab);
  const kind = target.closest?.("[data-rail-shortcuts]");
  if (kind) {
    shortcutKind = kind.dataset.railShortcuts;
    return renderShortcuts();
  }
  const conversation = target.closest?.("[data-rail-conversation]");
  if (conversation) {
    handlers.showChatView?.();
    return handlers.openConversation?.(conversation.dataset.railConversation);
  }
  const issue = target.closest?.("[data-rail-issue]");
  if (issue) return handlers.openIssue?.(issue.dataset.railIssue);
  if (target.closest?.("[data-rail-collapse]")) {
    writeFlag(COLLAPSE_KEY, !document.body.classList.contains("rail-collapsed"));
    return applyFlags();
  }
  if (target.closest?.("[data-side-rail-toggle]")) {
    writeFlag(SIDE_RAIL_KEY, !document.body.classList.contains("side-rail-hidden"));
    return applyFlags();
  }
}

// handlers: 화면 전환·대화 열기·이슈 열기 함수. 순환 import를 피하려고 bootstrap에서 넘겨받는다.
function initAppSidebar(options = {}) {
  handlers = options;
  document.addEventListener("click", handleClick);
  applyFlags();
  renderShortcuts();
  syncSidebar();
  // 탭 선택(aria-selected)과 화면 전환(data-view)이 바뀔 때마다 하위 메뉴·경로·바로가기를 맞춘다.
  const observer = new MutationObserver(() => {
    syncSidebar();
    renderShortcuts();
  });
  for (const tab of document.querySelectorAll("[data-workspace-tab], [data-dashboard-tab]")) {
    observer.observe(tab, { attributes: true, attributeFilter: ["aria-selected"] });
  }
  if (shell()) observer.observe(shell(), { attributes: true, attributeFilter: ["data-view"] });
}

export { initAppSidebar, syncSidebar, renderShortcuts, openRailTab, shortcutRows };
