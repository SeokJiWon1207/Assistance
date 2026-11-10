// 팀 대시보드 › 고객사 탭. 고객사를 미종료 비교표와 최근 8주 등록 히트맵으로 보여 준다.
import { els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { customerMark } from "./ui_format.js?v=20260911-6";
import { mountCharts, releaseCharts } from "./bklit_charts.js?v=20261026-1";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { focusKey, restoreFocus } from "./view_focus.js?v=20261103-1";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { applyDashboardTab, registerDashboardTab, tabHeaderSlots } from "./dashboard_tabs.js?v=20261110-1";

const FILTERS = [
  ["all", "전체", "customers"],
  ["open", "미종료 있음", "withOpen"],
  ["spike", "지난주 급증", "spiking"],
  ["overdue", "기한 지남", "withOverdue"],
];
const SORTS = [["open", "미종료 많은 순"], ["recent", "최근 등록 순"], ["name", "가나다 순"]];

const overviewState = { filter: "all", sort: "open", query: "", data: null, loading: false, error: "", requestId: 0 };

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function viewElement() {
  return document.querySelector("#customerOverviewView");
}

function isComplete() {
  return overviewState.data?.meta?.search_complete === true;
}

// 마지막 등록일을 "오늘 · 어제 · n일 전 · n주 전"으로 줄인다. 기준은 브라우저 날짜다.
function lastCreatedLabel(value, today = new Date()) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return "등록 기록 없음";
  const day = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const base = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  const days = Math.max(0, Math.round((base - day) / 86400000));
  if (days === 0) return "오늘 등록";
  if (days === 1) return "어제 등록";
  if (days < 14) return `${days}일 전 등록`;
  if (days < 70) return `${Math.floor(days / 7)}주 전 등록`;
  return `${Number(match[2])}월 ${Number(match[3])}일 등록`;
}

function visibleRows() {
  const rows = [...(overviewState.data?.customers || [])];
  const query = overviewState.query.trim().toLocaleLowerCase();
  const filtered = rows.filter((row) => {
    if (query && !row.name.toLocaleLowerCase().includes(query)) return false;
    if (overviewState.filter === "open") return row.open > 0;
    if (overviewState.filter === "spike") return Boolean(row.spike);
    if (overviewState.filter === "overdue") return row.overdue > 0;
    return true;
  });
  if (overviewState.sort === "name") filtered.sort((a, b) => a.name.localeCompare(b.name, "ko"));
  else if (overviewState.sort === "recent") filtered.sort((a, b) => String(b.lastCreatedAt).localeCompare(String(a.lastCreatedAt)) || b.open - a.open);
  else filtered.sort((a, b) => b.open - a.open || b.recent - a.recent);
  return filtered;
}

function customerAnalysis(rows, weeks, complete) {
  const suffix = complete ? "" : "+";
  const maxOpen = Math.max(1, ...rows.map(row => row.open));
  const maxWeekly = Math.max(1, ...rows.flatMap(row => row.weekly || []));
  const name = row => row.project
    ? `<button type="button" class="customer-table-name" data-customer-open="${escapeHtml(row.project)}" aria-label="${escapeHtml(row.name)} 고객사 상세 보기">${customerMark(row.name, "sm")}<strong>${escapeHtml(row.name)}</strong>${icon("chevron-right", 14)}</button>`
    : `<span class="customer-table-name">${customerMark(row.name, "sm")}<strong>${escapeHtml(row.name)}</strong></span>`;
  const table = `<section class="analytics-card"><header><div><h3>고객사 현황 비교</h3><p>현재 미종료와 최근 등록을 함께 확인하세요.</p></div></header><div class="analytics-table-scroll"><table class="analytics-table customer-comparison-table"><thead><tr><th>고객사</th><th>미종료 규모</th><th>미종료</th><th>기한 지남</th><th>30일 이상</th><th>8주 신규</th><th>최근 등록</th></tr></thead><tbody>${rows.map(row => `<tr><th scope="row">${name(row)}</th><td><span class="analytics-bar-track"><i style="width:${row.open/maxOpen*100}%"></i></span></td><td><strong>${row.open}${suffix}</strong></td><td class="${row.overdue ? "is-overdue" : ""}">${row.overdue}${suffix}</td><td>${row.aging}${suffix}</td><td>${row.recent}${suffix}</td><td>${escapeHtml(lastCreatedLabel(row.lastCreatedAt))}${complete && row.spike ? '<span class="customer-card-chip is-spike">지난주 급증</span>' : ''}</td></tr>`).join('')}</tbody></table></div></section>`;
  const heatmap = `<section class="analytics-card"><header><div><h3>고객사별 주간 등록</h3><p>최근 8주 · 진한 칸일수록 신규 등록이 많아요. 마지막 주는 오늘까지예요.</p></div></header><div class="analytics-table-scroll"><table class="analytics-table analytics-heatmap"><thead><tr><th>고객사</th>${weeks.map(week => `<th>${escapeHtml(week.start.slice(5))}</th>`).join('')}</tr></thead><tbody>${rows.map(row => `<tr><th scope="row">${name(row)}</th>${weeks.map((week,i) => { const value=Number(row.weekly?.[i] || 0);return `<td><span class="analytics-heat-cell" data-intensity="${value?Math.min(4,Math.ceil(value/maxWeekly*4)):0}" title="${escapeHtml(week.start)} 주 · 신규 ${value}${suffix}건">${value}${suffix}</span></td>`; }).join('')}</tr>`).join('')}</tbody></table></div><p class="analytics-footnote">${complete?'0건':'확인한 범위'}<span class="analytics-heat-scale"><i data-intensity="0"></i><i data-intensity="1"></i><i data-intensity="2"></i><i data-intensity="3"></i><i data-intensity="4"></i></span>${maxWeekly}${suffix}건</p></section>`;
  return `<div class="customer-analytics">${table}${heatmap}</div>`;
}

function toolbarSubtitle() {
  const total = overviewState.data?.customers?.length;
  return `팀 담당 이슈 · ${total == null ? "" : `고객사 선택 목록 ${total}곳 · `}최근 8주 주별 신규(등록일 기준)`;
}

function renderTools() {
  return `
    <span class="data-badge-slot" data-customer-badge></span>
    <button type="button" class="secondary-button workspace-refresh-button" data-customer-refresh aria-label="새로고침">${icon("refresh", 16)}<span>새로고침</span></button>`;
}

function renderControls() {
  const summary = overviewState.data?.summary || {};
  const filters = FILTERS.map(([key, label, countKey]) => `
    <button type="button" class="customer-filter-chip" aria-pressed="${overviewState.filter === key}" data-customer-filter="${key}">
      ${label}${overviewState.data ? ` <b>${Number(summary[countKey] || 0)}</b>` : ""}
    </button>`).join("");
  const sorts = SORTS.map(([key, label]) => `<option value="${key}"${overviewState.sort === key ? " selected" : ""}>${label}</option>`).join("");
  return `
    <div class="customer-overview-controls">
      <div class="customer-filter-chips" role="group" aria-label="고객사 거르기">${filters}</div>
      <div class="customer-overview-tools">
        <label class="customer-overview-search">${icon("search", 16)}<span class="sr-only">고객사 검색</span>
          <input type="search" placeholder="고객사 검색" value="${escapeHtml(overviewState.query)}" data-customer-query autocomplete="off">
        </label>
        <label class="screen-filter">정렬 <select data-customer-sort>${sorts}</select></label>
      </div>
    </div>`;
}

function renderBody() {
  const data = overviewState.data;
  if (!data) {
    if (overviewState.error) {
      return `<div class="workspace-error">${escapeHtml(overviewState.error)} <button type="button" class="secondary-button" data-customer-refresh>다시 시도</button></div>`;
    }
    return '<div class="workspace-loading">고객사별 이슈를 불러오는 중이에요.</div>';
  }
  const complete = isComplete();
  const rows = visibleRows();
  const filtered = overviewState.filter !== "all" || Boolean(overviewState.query.trim());
  const reset = '<button type="button" class="customer-reset" data-customer-reset>검색·필터 초기화</button>';
  const result = `<div class="customer-overview-result"><p role="status" aria-live="polite"><strong>${rows.length}곳 표시</strong><span>목록 ${data.customers?.length || 0}곳</span></p>${filtered && rows.length ? reset : ""}</div>`;
  const note = complete ? "" : '<p class="customer-detail-note">일부 데이터만 확인했어요. 건수 뒤 +는 확인한 범위 기준의 최소값이고, 급증 여부는 판단하지 않았어요.</p>';
  const cards = rows.length
    ? customerAnalysis(rows, data.weeks || [], complete)
    : `<section class="customer-overview-empty">${icon("search", 24)}<h3>${filtered ? "조건에 맞는 고객사가 없어요." : "표시할 고객사가 없어요."}</h3><p>${filtered ? "검색어를 바꾸거나 필터를 해제해 보세요." : "고객사 목록을 다시 불러와 주세요."}</p>${filtered ? reset : '<button type="button" class="customer-reset" data-customer-refresh>다시 조회</button>'}</section>`;
  return `${overviewState.error ? `<p class="customer-detail-error" role="alert">${escapeHtml(overviewState.error)}</p>` : ""}${note}${result}${cards}
    ${rows.length ? '<p class="customer-detail-note">고객사 이름을 누르면 이슈가 가장 많은 프로젝트의 상세가 열려요. 주간 등록 표의 날짜는 각 주의 시작일이에요.</p>' : ""}`;
}

function updateBadge(container) {
  const slot = container?.querySelector("[data-customer-badge]");
  const meta = overviewState.data?.meta;
  if (!slot) return;
  const badgeState = dataBadgeState({
    source: meta?.source || "error",
    complete: meta?.search_complete !== false,
    refreshing: Boolean(meta?.refreshPending),
    loading: overviewState.loading,
  });
  const time = formatBadgeTime(meta?.fetchedAt);
  const details = mantisBadgeDetails(badgeState, { time, reason: meta?.sourceReason });
  renderDataBadge(slot, {
    state: badgeState,
    time: ["fresh", "partial", "snapshot"].includes(badgeState) ? time : "",
    sources: [details.source, ...(meta?.scopeLabel ? [{ name: "범위", tone: "muted", text: meta.scopeLabel }] : [])],
    note: details.note,
    onRetry: details.retry ? () => loadCustomerOverview(true) : null,
  });
}

function render() {
  const view = viewElement();
  if (!view) return;
  const { subtitle, tools } = tabHeaderSlots("customers");
  const focused = focusKey(view) || focusKey(tools);
  releaseCharts(view);
  if (subtitle) subtitle.textContent = subtitle.title = toolbarSubtitle();
  if (tools) tools.innerHTML = renderTools();
  view.innerHTML = `<div class="dashboard-tab-inner">${renderControls()}<div class="customer-overview-body" aria-busy="${overviewState.loading}">${renderBody()}</div></div>`;
  for (const container of [view, tools]) if (container) restoreFocus(container, focused);
  updateBadge(tools);
  void mountCharts(view);
}

// 검색어 입력 중에는 입력칸을 다시 만들지 않고 분석 영역만 바꾼다.
function renderCards() {
  const body = viewElement()?.querySelector(".customer-overview-body");
  if (!body) return render();
  releaseCharts(body);
  body.innerHTML = renderBody();
  void mountCharts(body);
}

async function loadCustomerOverview(refresh = false) {
  const requestId = ++overviewState.requestId;
  overviewState.loading = true;
  render();
  try {
    const response = await apiFetch(`${WORKSPACE_ENDPOINTS.customerOverview}${refresh ? "?refresh=true" : ""}`);
    const payload = await responseJson(response);
    if (requestId !== overviewState.requestId) return;
    if (!response.ok) throw new Error(payload.detail || "고객사별 이슈를 불러오지 못했어요.");
    overviewState.data = payload;
    overviewState.error = "";
  } catch (error) {
    if (requestId !== overviewState.requestId) return;
    overviewState.error = error.message || "고객사별 이슈를 불러오지 못했어요.";
  }
  overviewState.loading = false;
  render();
}

function askFromOverview(question) {
  if (!question || !els.input) return;
  showChatView();
  els.input.value = question;
  els.input.dispatchEvent(new Event("input", { bubbles: true }));
  els.input.focus();
}

function openCustomer(project) {
  return import("./customer_detail.js?v=20261103-1").then(({ openCustomerDetail }) => openCustomerDetail(project, {
    period: 30,
    onAsk: askFromOverview,
    onClose: () => {
      applyDashboardTab();
      viewElement()?.querySelector(`[data-customer-open="${CSS.escape(project)}"]`)?.focus();
    },
  }));
}

function handleClick(event) {
  const target = event.target;
  if (target.closest("[data-customer-reset]")) {
    overviewState.filter = "all";
    overviewState.query = "";
    render();
    return viewElement()?.querySelector("[data-customer-query]")?.focus();
  }
  const filter = target.closest("[data-customer-filter]");
  if (filter) {
    overviewState.filter = filter.dataset.customerFilter;
    render();
    return viewElement()?.querySelector(`[data-customer-filter="${overviewState.filter}"]`)?.focus();
  }
  if (target.closest("[data-customer-refresh]")) return loadCustomerOverview(true);
  const open = target.closest("[data-customer-open]");
  if (open) return openCustomer(open.dataset.customerOpen);
}

function handleInput(event) {
  if (!event.target.matches?.("[data-customer-query]")) return;
  overviewState.query = event.target.value;
  renderCards();
}

function handleChange(event) {
  if (!event.target.matches?.("[data-customer-sort]")) return;
  overviewState.sort = event.target.value;
  renderCards();
}

function initCustomerOverview() {
  const view = viewElement();
  if (!view) return;
  view.addEventListener("click", handleClick);
  view.addEventListener("input", handleInput);
  view.addEventListener("change", handleChange);
  tabHeaderSlots("customers").tools?.addEventListener("click", handleClick);
  registerDashboardTab("customers", {
    view: viewElement,
    onShow: () => { if (!overviewState.loading) void loadCustomerOverview(); },
  });
}

export { initCustomerOverview, loadCustomerOverview, lastCreatedLabel, visibleRows, overviewState };
