// 팀 대시보드 '내 보기' 저장, 필터 결과 CSV 내보내기, 내가 처리할 일 요약.
// 보기는 사용자별로 이 브라우저에만 저장한다(팀 공유 보기는 없음).
import { state, scopedStorageKey } from "./runtime.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { escapeHtml } from "./chat_rendering.js?v=20260911-6";
import { customerName } from "./ui_format.js?v=20260911-6";
import { analyticsCustomerLabel } from "./dashboard_analytics.js?v=20261111-1";
import { workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { dashboardFilterSnapshot, applyDashboardFilters, dashboardPriorityRows, showPersonalView } from "./dashboard.js?v=20260911-6";
import {
  sanitizeViews, saveView, updateViewFilters, removeView, sameFilters, describeFilters, priorityCsv, myWorkSummary,
} from "./dashboard_views_model.js?v=20261105-1";

const VIEWS_KEY = "team-ai-assistant-dashboard-views-v1";
const ACTIVE_VIEW_KEY = "team-ai-assistant-dashboard-active-view-v1";
const NOTICE_KEY = "team-ai-assistant-dashboard-views-notice-v1";
const RISK_LABELS = { high: "위험", attention: "주의", watch: "관찰" };

const ui = { menuOpen: false, error: "", shown: false };

function bar() {
  return document.querySelector("#dashboardViewsBar");
}

function storageKey(base) {
  return scopedStorageKey(base, state.currentUser?.uid);
}

function readJson(key, fallback) {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey(key)) ?? "null");
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key, value) {
  try {
    localStorage.setItem(storageKey(key), JSON.stringify(value));
    return true;
  } catch {
    ui.error = "이 브라우저에 보기를 저장하지 못했어요. 저장 공간 설정을 확인해 주세요.";
    return false;
  }
}

function savedViews() {
  return sanitizeViews(readJson(VIEWS_KEY, []));
}

function activeView(views = savedViews()) {
  const id = readJson(ACTIVE_VIEW_KEY, "");
  return views.find((view) => view.id === id) || null;
}

function setActiveView(id) {
  writeJson(ACTIVE_VIEW_KEY, id || "");
}

function viewRowsHtml(views, active) {
  if (!views.length) return '<p class="dashboard-views-empty">저장한 보기가 없어요. 아래에서 지금 조건을 저장해 보세요.</p>';
  return `<ul class="dashboard-views-list">${views.map((view) => `
    <li>
      <button type="button" class="dashboard-views-item${view.id === active?.id ? " is-active" : ""}" data-apply-view="${escapeHtml(view.id)}"${view.id === active?.id ? ' aria-current="true"' : ""}>
        ${icon("star", 14)}<span><strong>${escapeHtml(view.name)}</strong><small>${escapeHtml(describeFilters(view.filters, { customerName: analyticsCustomerLabel }))}</small></span>
      </button>
      <button type="button" class="dashboard-views-delete" data-delete-view="${escapeHtml(view.id)}" aria-label="${escapeHtml(view.name)} 보기 삭제">${icon("trash", 16)}</button>
    </li>`).join("")}</ul>`;
}

function myWorkHtml() {
  const issues = state.myWorkspace?.mantis?.issues;
  if (!Array.isArray(issues)) return "";
  const summary = myWorkSummary(issues, { statusLabel: workspaceStatusLabel });
  const details = [summary.overdue ? `기한 지남 ${summary.overdue}` : "", summary.aging ? `30일 넘게 미종료 ${summary.aging}` : ""].filter(Boolean).join(" · ");
  return `<button type="button" class="dashboard-mywork" data-open-my-work aria-label="내가 처리할 일 ${summary.open}건${details ? `, ${details}` : ""}, 내 업무로 이동">
    <span class="dashboard-mywork-label">내가 처리할 일</span><strong>${summary.open}건</strong>${details ? `<small>${escapeHtml(details)}</small>` : ""}${icon("chevron-right", 16)}
  </button>`;
}

function noticeHtml() {
  if (readJson(NOTICE_KEY, false)) return "";
  return `<div class="dashboard-views-notice" role="note">
    ${icon("info", 16)}<span>자주 쓰는 필터 조합을 <b>저장된 보기</b>로 남겨 두고 한 번에 다시 열 수 있어요.</span>
    <button type="button" class="dashboard-views-notice-close" data-dismiss-views-notice aria-label="안내 닫기">${icon("close", 16)}</button>
  </div>`;
}

function renderDashboardViews() {
  const root = bar();
  if (!root || !ui.shown) return;
  const views = savedViews();
  const filters = dashboardFilterSnapshot();
  const active = activeView(views);
  const modified = Boolean(active && !sameFilters(active.filters, filters));
  const rowCount = dashboardPriorityRows().length;
  const triggerLabel = active ? active.name : "저장된 보기";
  root.hidden = false;
  root.innerHTML = `
    ${noticeHtml()}
    <div class="dashboard-views-row">
      <div class="dashboard-views-menu-wrap">
        <button type="button" class="dashboard-views-trigger${active ? " has-view" : ""}" data-views-toggle aria-expanded="${ui.menuOpen}" aria-controls="dashboardViewsMenu">
          ${icon("star", 16)}<span>${escapeHtml(triggerLabel)}</span>${modified ? '<em>변경됨</em>' : ""}${icon("chevron-down", 16)}
        </button>
        <div id="dashboardViewsMenu" class="dashboard-views-menu"${ui.menuOpen ? "" : " hidden"}>
          <p class="dashboard-views-menu-label">내 보기</p>
          ${viewRowsHtml(views, active)}
          <form class="dashboard-views-form" data-save-view-form>
            <label for="dashboardViewName">지금 조건을 새 보기로 저장</label>
            <span class="dashboard-views-form-row">
              <input id="dashboardViewName" name="viewName" type="text" maxlength="40" autocomplete="off" placeholder="예: 내 고객사 30일" />
              <button type="submit" class="secondary-button">저장</button>
            </span>
            <small>${escapeHtml(describeFilters(filters, { customerName: analyticsCustomerLabel }))}</small>
          </form>
          <p class="dashboard-views-error" role="alert"${ui.error ? "" : " hidden"}>${escapeHtml(ui.error)}</p>
        </div>
      </div>
      ${modified ? '<button type="button" class="secondary-button dashboard-views-update" data-update-view>변경 사항을 보기에 저장</button>' : ""}
      <button type="button" class="secondary-button dashboard-views-export" data-export-priorities${rowCount ? "" : " disabled"}>${icon("arrow-down", 16)}<span>필터 결과 내보내기</span><small>${rowCount}건</small></button>
      ${myWorkHtml()}
    </div>`;
}

function setMenuOpen(open, { focus = false } = {}) {
  ui.menuOpen = open;
  if (!open) ui.error = "";
  renderDashboardViews();
  const root = bar();
  if (open && focus) (root?.querySelector("[data-apply-view]") || root?.querySelector("#dashboardViewName"))?.focus();
  if (!open && focus) root?.querySelector("[data-views-toggle]")?.focus();
}

function downloadPriorities() {
  const rows = dashboardPriorityRows();
  if (!rows.length) return;
  const csv = priorityCsv(rows, {
    customerName: (issue) => customerName(issue, ""),
    statusLabel: workspaceStatusLabel,
    riskLabel: (level) => RISK_LABELS[level] || "관찰",
  });
  const stamp = new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" }).replaceAll("-", "");
  const link = document.createElement("a");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  link.href = url;
  link.download = `우선확인이슈_${stamp}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  globalThis.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function handleClick(event) {
  const target = event.target;
  if (target.closest("[data-views-toggle]")) return setMenuOpen(!ui.menuOpen, { focus: !ui.menuOpen });
  if (target.closest("[data-dismiss-views-notice]")) {
    writeJson(NOTICE_KEY, true);
    renderDashboardViews();
    bar()?.querySelector("[data-views-toggle]")?.focus();
    return;
  }
  const apply = target.closest("[data-apply-view]");
  if (apply) {
    const view = savedViews().find((item) => item.id === apply.dataset.applyView);
    if (!view) return;
    setActiveView(view.id);
    ui.menuOpen = false;
    applyDashboardFilters(view.filters);
    bar()?.querySelector("[data-views-toggle]")?.focus();
    return;
  }
  const remove = target.closest("[data-delete-view]");
  if (remove) {
    const id = remove.dataset.deleteView;
    if (activeView()?.id === id) setActiveView("");
    writeJson(VIEWS_KEY, removeView(savedViews(), id));
    setMenuOpen(true, { focus: true });
    return;
  }
  if (target.closest("[data-update-view]")) {
    const active = activeView();
    if (active) writeJson(VIEWS_KEY, updateViewFilters(savedViews(), active.id, dashboardFilterSnapshot()));
    renderDashboardViews();
    bar()?.querySelector("[data-views-toggle]")?.focus();
    return;
  }
  if (target.closest("[data-export-priorities]")) return downloadPriorities();
  if (target.closest("[data-open-my-work]")) showPersonalView();
}

function handleSubmit(event) {
  const form = event.target.closest("[data-save-view-form]");
  if (!form) return;
  event.preventDefault();
  const result = saveView(savedViews(), { name: form.viewName.value, filters: dashboardFilterSnapshot() });
  ui.error = result.error;
  if (!result.error && writeJson(VIEWS_KEY, result.views)) {
    setActiveView(result.view.id);
    ui.menuOpen = false;
  }
  renderDashboardViews();
  bar()?.querySelector(ui.menuOpen ? "#dashboardViewName" : "[data-views-toggle]")?.focus();
}

function initDashboardViews() {
  const root = bar();
  if (!root) return;
  root.addEventListener("click", handleClick);
  root.addEventListener("submit", handleSubmit);
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !ui.menuOpen) return;
    event.preventDefault();
    event.stopPropagation();
    setMenuOpen(false, { focus: true });
  });
  document.addEventListener("pointerdown", (event) => {
    if (ui.menuOpen && !event.target.closest?.(".dashboard-views-menu-wrap")) setMenuOpen(false);
  });
  globalThis.addEventListener("dashboard:filters-rendered", () => {
    ui.shown = true;
    // 백그라운드 갱신이 보기 이름을 입력하는 중인 메뉴를 지우지 않게 한다.
    if (ui.menuOpen && root.contains(document.activeElement)) return;
    renderDashboardViews();
  });
}

export { initDashboardViews, renderDashboardViews };
