// 팀 대시보드 › 공수·처리시간 탭. 운영 현황 본문과 필터 줄 자리를 대신 쓴다.
import { els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { focusKey, restoreFocus } from "./view_focus.js?v=20261103-1";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { applyDashboardTab, registerDashboardTab } from "./dashboard_tabs.js?v=20261103-1";

const PERIODS = [7, 14, 30, 90];

const boardState = {
  period: 30,
  data: null,
  loading: false,
  error: "",
  requestId: 0,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatNumber(value, digits = 0) {
  return Number(value || 0).toLocaleString("ko-KR", { maximumFractionDigits: digits });
}

function formatDays(value) {
  return value == null ? "—" : formatNumber(value, 1);
}

function formatShortDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${Number(match[2])}월 ${Number(match[3])}일` : "";
}

function viewElement() {
  return document.querySelector("#effortBoardView");
}

function isComplete() {
  return boardState.data?.meta?.search_complete === true;
}

function countText(value) {
  return `${formatNumber(value)}${isComplete() ? "" : '<span class="dashboard-lower-bound">건 이상</span>'}`;
}

function metric(label, value, helper, tone = "", unit = "") {
  return `
    <div class="workspace-metric ${tone}" role="group" aria-label="${escapeHtml(label)}">
      <span>${escapeHtml(label)}</span>
      <strong>${value}${unit ? `<span class="customer-detail-unit">${escapeHtml(unit)}</span>` : ""}</strong>
      <small>${escapeHtml(helper)}</small>
    </div>`;
}

function medianDelta(summary) {
  const current = summary.medianHandlingDays;
  const previous = summary.previousMedianHandlingDays;
  if (current == null || previous == null) return "직전 기간 비교 없음";
  const diff = Math.round((current - previous) * 10) / 10;
  if (!diff) return `직전 기간과 같음 (${formatDays(previous)}일)`;
  return `직전 ${formatDays(previous)}일보다 ${formatDays(Math.abs(diff))}일 ${diff > 0 ? "늘었어요" : "줄었어요"}`;
}

function renderMetrics(data) {
  const summary = data.summary || {};
  const period = data.meta?.periodDays || boardState.period;
  const effort = data.effort || {};
  const previousClosed = summary.previousClosedCount == null ? "직전 기간 비교 없음" : `직전 ${period}일 ${formatNumber(summary.previousClosedCount)}건`;
  return `
    <section class="workspace-summary-grid effort-board-metrics" aria-label="처리시간 핵심 지표">
      ${metric("해결 · 종료", countText(summary.closedCount), previousClosed)}
      ${metric("처리일 중앙값", formatDays(summary.medianHandlingDays), medianDelta(summary), summary.medianHandlingDays > (summary.previousMedianHandlingDays ?? Infinity) ? "is-warning" : "", summary.medianHandlingDays == null ? "" : "일")}
      ${metric("처리일 상위 10%", formatDays(summary.p90HandlingDays), "오래 걸린 10%의 경계", "", summary.p90HandlingDays == null ? "" : "일 이상")}
      ${metric(`${data.meta?.agingDays || 30}일 넘은 미종료`, countText(summary.agingOpen), `7일 안에 ${formatNumber(summary.agingSoon)}건이 더 넘어요`, "is-danger")}
      ${metric("기록된 공수", formatNumber(effort.total, 2), `공수 기록 이슈 ${formatNumber(effort.issueCount)}건`)}
    </section>`;
}

function sectionHeader(title, meta = "") {
  return `<header><div><h3>${escapeHtml(title)}</h3></div>${meta ? `<em>${escapeHtml(meta)}</em>` : ""}</header>`;
}

function columnChart(rows, { label, valueText, tone }) {
  const max = Math.max(1, ...rows.map((row) => Number(row.value) || 0));
  return `
    <div class="effort-columns" role="img" aria-label="${escapeHtml(label)}">
      ${rows.map((row) => `
        <div class="effort-column">
          <b>${escapeHtml(valueText(row))}</b>
          <span class="effort-column-track" aria-hidden="true"><i class="${tone(row)}" style="height:${Math.round(((Number(row.value) || 0) / max) * 100)}%"></i></span>
          <small>${escapeHtml(row.label)}</small>
        </div>`).join("")}
    </div>`;
}

function renderDistribution(data) {
  const rows = (data.distribution || []).map((row) => ({ ...row, value: row.count }));
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  const label = rows.map((row) => `${row.label} ${row.count}건`).join(", ");
  return `
    <section class="workspace-section" aria-label="처리일 분포">
      ${sectionHeader("처리일 분포", `해결·종료 ${formatNumber(total)}건`)}
      <div class="customer-detail-section-body">
        ${total ? columnChart(rows, { label: `처리일 분포: ${label}`, valueText: (row) => formatNumber(row.count), tone: (row) => (row.key === "31+" ? "is-late" : "") })
          : `<p class="customer-detail-empty">${isComplete()
            ? "이 기간에 해결·종료된 이슈가 없습니다."
            : "일부 데이터만 확인해 처리일을 계산할 종료 이슈를 찾지 못했습니다. 전체 결과는 미확정입니다."}</p>`}
        <p class="customer-detail-note">등록일부터 마지막 수정일까지를 처리일로 셉니다.</p>
      </div>
    </section>`;
}

function renderWeekly(data) {
  const rows = (data.weekly || []).map((row) => ({ ...row, value: row.medianDays }));
  const label = rows.map((row) => `${row.label} 주 ${row.medianDays == null ? "종료 없음" : `${row.medianDays}일`}`).join(", ");
  return `
    <section class="workspace-section" aria-label="주별 처리일 중앙값">
      ${sectionHeader("주별 처리일 중앙값", "최근 8주")}
      <div class="customer-detail-section-body">
        ${columnChart(rows, { label: `주별 처리일 중앙값: ${label}`, valueText: (row) => (row.medianDays == null ? "—" : `${formatDays(row.medianDays)}일`), tone: (row) => (row.partialWeek ? "is-partial" : "") })}
        <p class="customer-detail-note">주는 월요일부터 셉니다. 이번 주는 오늘까지만 반영합니다.</p>
      </div>
    </section>`;
}

function rankedList(rows, { name, value, width, action = "" }) {
  const max = Math.max(1, ...rows.map(width));
  return `<ol class="customer-ranked-list effort-ranked-list">${rows.map((row) => `
    <li>
      ${action ? `<button type="button" class="customer-issue-link effort-ranked-name" ${action(row)}>${escapeHtml(name(row))}</button>` : `<span class="customer-ranked-name">${escapeHtml(name(row))}</span>`}
      <span class="customer-ranked-track" aria-hidden="true"><i style="width:${Math.round((width(row) / max) * 100)}%"></i></span>
      <span class="customer-ranked-value">${value(row)}</span>
    </li>`).join("")}</ol>`;
}

function renderCustomers(data) {
  const rows = data.customers || [];
  return `
    <section class="workspace-section" aria-label="고객사별 처리일 중앙값">
      ${sectionHeader("고객사별 처리일 중앙값", "긴 순 · 3건 이상 종료")}
      <div class="customer-detail-section-body">
        ${rows.length ? rankedList(rows, {
          name: (row) => row.name,
          value: (row) => `${formatDays(row.medianDays)}일 <small>· ${formatNumber(row.closedCount)}건</small>`,
          width: (row) => Number(row.medianDays) || 0,
          action: (row) => `data-effort-customer="${escapeHtml(row.customer)}" aria-label="${escapeHtml(row.name)} 고객사 상세 보기"`,
        }) : '<p class="customer-detail-empty">비교할 만큼 종료된 고객사가 없습니다.</p>'}
      </div>
    </section>`;
}

function renderEffort(data) {
  const effort = data.effort || { types: [] };
  const rows = effort.types || [];
  return `
    <section class="workspace-section" aria-label="유형별 공수">
      ${sectionHeader("유형별 공수", "고객사전달일 기준")}
      <div class="customer-detail-section-body">
        ${effort.total ? rankedList(rows, {
          name: (row) => row.label,
          value: (row) => `${formatNumber(row.total, 2)} <small>· 건당 ${row.averagePerIssue == null ? "—" : formatNumber(row.averagePerIssue, 2)}</small>`,
          width: (row) => Number(row.total) || 0,
        }) : '<p class="customer-detail-empty">이 기간에 고객사에 전달된 이슈의 공수 기록이 없습니다.</p>'}
      </div>
    </section>`;
}

function renderAging(data) {
  const issues = data.agingIssues || [];
  const total = Number(data.agingIssuesTotal || issues.length);
  const agingDays = data.meta?.agingDays || 30;
  const rows = issues.map((issue) => `
    <tr>
      <td><button type="button" class="customer-issue-link" data-effort-source="${escapeHtml(issue.id)}" aria-label="#${escapeHtml(issue.id)} 상세 보기">#${escapeHtml(issue.id)}</button></td>
      <td class="customer-issue-title">${escapeHtml(issue.title)}</td>
      <td>${escapeHtml(issue.customerName || "")}</td>
      <td>${escapeHtml(issue.handler || "")}</td>
      <td><span class="status-badge">${escapeHtml(workspaceStatusLabel(issue.status))}</span></td>
      <td class="is-number ${issue.aging ? "is-overdue" : "is-soon"}">${formatNumber(issue.ageDays)}일</td>
    </tr>`).join("");
  return `
    <section class="workspace-section" aria-label="장기 미종료와 곧 넘을 이슈">
      ${sectionHeader(`${agingDays}일 넘은 미종료 · 곧 넘을 이슈`, `${formatNumber(total)}건 중 경과일 긴 순`)}
      <div class="customer-detail-section-body">
        ${issues.length ? `<div class="customer-issue-table-wrap"><table class="customer-issue-table">
          <thead><tr><th scope="col">번호</th><th scope="col">제목</th><th scope="col">고객사</th><th scope="col">담당자</th><th scope="col">상태</th><th scope="col" class="is-number">경과</th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : '<p class="customer-detail-empty">장기 미종료이거나 곧 넘을 이슈가 없습니다.</p>'}
        ${total > issues.length ? `<p class="customer-detail-note">나머지 ${formatNumber(total - issues.length)}건은 운영 현황의 장기 미종료 필터에서 볼 수 있습니다.</p>` : ""}
      </div>
    </section>`;
}

function renderToolbar() {
  const meta = boardState.data?.meta;
  const subtitle = meta
    ? `${formatShortDate(meta.periodFrom)} ~ ${formatShortDate(meta.periodTo)} · 종료일은 마지막 수정일 기준`
    : "처리시간 데이터를 조회하고 있습니다.";
  const periods = PERIODS.map((days) => `<button type="button" data-effort-period="${days}" aria-pressed="${boardState.period === days}">${days}일</button>`).join("");
  return `
    <div class="dashboard-tab-toolbar">
      <p>${escapeHtml(subtitle)}</p>
      <div class="customer-detail-toolbar">
        <span class="data-badge-slot" data-effort-badge></span>
        <fieldset class="dashboard-period"><legend class="sr-only">조회 기간</legend>${periods}</fieldset>
        <button type="button" class="secondary-button workspace-refresh-button" data-effort-refresh aria-label="새로고침">${icon("refresh", 16)}<span>새로고침</span></button>
      </div>
    </div>`;
}

function renderBody() {
  const data = boardState.data;
  if (!data) {
    if (boardState.error) {
      return `<div class="workspace-error">${escapeHtml(boardState.error)} <button type="button" class="secondary-button" data-effort-refresh>다시 시도</button></div>`;
    }
    return '<div class="workspace-loading">공수·처리시간을 불러오는 중입니다.</div>';
  }
  return `
    ${boardState.error ? `<p class="customer-detail-error" role="alert">${escapeHtml(boardState.error)}</p>` : ""}
    ${renderMetrics(data)}
    <div class="customer-detail-grid">${renderDistribution(data)}${renderWeekly(data)}</div>
    <div class="effort-board-grid">${renderCustomers(data)}${renderEffort(data)}</div>
    ${renderAging(data)}
    <p class="customer-detail-note">공수는 Mantis 공수 필드(문의·서버·PC·QC)에 기록된 값만 합산합니다. 난이도·휴가를 반영한 실제 업무량이나 개인 성과 평가가 아니므로 담당자별 순위는 보여 주지 않습니다.</p>`;
}

function updateBadge(view) {
  const slot = view.querySelector("[data-effort-badge]");
  const meta = boardState.data?.meta;
  if (!slot) return;
  const badgeState = dataBadgeState({
    source: meta?.source || "error",
    complete: meta?.search_complete !== false,
    refreshing: Boolean(meta?.refreshPending),
    loading: boardState.loading,
  });
  const time = formatBadgeTime(meta?.fetchedAt);
  const details = mantisBadgeDetails(badgeState, { time, reason: meta?.sourceReason });
  renderDataBadge(slot, {
    state: badgeState,
    time: ["fresh", "partial", "snapshot"].includes(badgeState) ? time : "",
    sources: [details.source, ...(meta?.scopeLabel ? [{ name: "범위", tone: "muted", text: meta.scopeLabel }] : [])],
    note: details.note,
    onRetry: details.retry ? () => loadEffortBoard(true) : null,
  });
}

function render() {
  const view = viewElement();
  if (!view) return;
  const focused = focusKey(view);
  view.innerHTML = `<div class="dashboard-tab-inner">${renderToolbar()}<div class="customer-detail-body" aria-busy="${boardState.loading}">${renderBody()}</div></div>`;
  restoreFocus(view, focused);
  updateBadge(view);
}

async function loadEffortBoard(refresh = false) {
  const requestId = ++boardState.requestId;
  boardState.loading = true;
  render();
  const params = new URLSearchParams({ days: String(boardState.period) });
  if (refresh) params.set("refresh", "true");
  try {
    const response = await apiFetch(`${WORKSPACE_ENDPOINTS.effortBoard}?${params}`);
    const payload = await responseJson(response);
    if (requestId !== boardState.requestId) return;
    if (!response.ok) throw new Error(payload.detail || "공수·처리시간을 불러오지 못했습니다.");
    boardState.data = payload;
    boardState.error = "";
  } catch (error) {
    if (requestId !== boardState.requestId) return;
    boardState.error = error.message || "공수·처리시간을 불러오지 못했습니다.";
  }
  boardState.loading = false;
  render();
}

function askFromBoard(question) {
  if (!question || !els.input) return;
  showChatView();
  els.input.value = question;
  els.input.dispatchEvent(new Event("input", { bubbles: true }));
  els.input.focus();
}

function openCustomer(customer) {
  return import("./customer_detail.js?v=20261103-1").then(({ openCustomerDetail }) => openCustomerDetail(customer, {
    period: boardState.period,
    onAsk: askFromBoard,
    // 상세를 닫으면 공수 탭으로 돌아오고 누른 고객사에 초점을 돌려준다.
    onClose: () => {
      applyDashboardTab();
      viewElement()?.querySelector(`[data-effort-customer="${CSS.escape(customer)}"]`)?.focus();
    },
  }));
}

function handleClick(event) {
  const target = event.target;
  const period = target.closest("[data-effort-period]");
  if (period) {
    const days = Number(period.dataset.effortPeriod);
    if (days === boardState.period) return;
    boardState.period = days;
    return loadEffortBoard();
  }
  if (target.closest("[data-effort-refresh]")) return loadEffortBoard(true);
  const customer = target.closest("[data-effort-customer]");
  if (customer) return openCustomer(customer.dataset.effortCustomer);
  const source = target.closest("[data-effort-source]");
  if (source) {
    return import("./chat_source_detail.js?v=20260911-6")
      .then(({ openSourceDetail }) => openSourceDetail({ type: "chroma", issue_id: source.dataset.effortSource }));
  }
}

function initEffortBoard() {
  const view = viewElement();
  if (!view) return;
  view.addEventListener("click", handleClick);
  registerDashboardTab("effort", {
    view: viewElement,
    onShow: () => { if (!boardState.loading) void loadEffortBoard(); },
  });
}

export { initEffortBoard, loadEffortBoard };
