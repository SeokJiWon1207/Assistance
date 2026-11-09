// 팀 대시보드 › 공수·처리시간 탭. 운영 현황 본문과 필터 줄 자리를 대신 쓴다.
import { els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { formatPeriodRange, customerMark } from "./ui_format.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { focusKey, restoreFocus } from "./view_focus.js?v=20261103-1";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { applyDashboardTab, registerDashboardTab, tabHeaderSlots } from "./dashboard_tabs.js?v=20261110-1";

const PERIODS = [7, 14, 30, 90];
const AGING_PREVIEW_COUNT = 5;

const boardState = {
  period: 30,
  data: null,
  loading: false,
  error: "",
  requestId: 0,
  agingExpanded: false,
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

function viewElement() {
  return document.querySelector("#effortBoardView");
}

function isComplete() {
  return boardState.data?.meta?.search_complete === true;
}

function countText(value) {
  return `${formatNumber(value)}${isComplete() ? "" : '<span class="dashboard-lower-bound">건 이상</span>'}`;
}

// 지표 카드: 내 업무·주간 보고와 같은 모양(이름 → 숫자+증감 배지 → 설명).
function kpi(label, value, helper, { tone = "", unit = "", delta = null } = {}) {
  const pill = delta ? `<em class="workspace-kpi-delta ${delta.tone}" title="${escapeHtml(delta.text)}">${delta.icon ? icon(delta.icon, 12) : ""}${escapeHtml(delta.label)}<span class="sr-only">, ${escapeHtml(delta.text)}</span></em>` : "";
  return `
    <article class="workspace-kpi ${tone}" aria-label="${escapeHtml(label)}">
      <span class="workspace-kpi-label">${escapeHtml(label)}</span>
      <div class="workspace-kpi-value"><strong>${value}${escapeHtml(unit)}</strong>${pill}</div>
      <small>${escapeHtml(helper)}</small>
    </article>`;
}

// 직전 기간 대비 배지. 해결이 늘거나 처리일이 줄면 좋은 쪽(초록), 반대면 주의(빨강)로 칠한다.
function changeBadge(diff, { unit, betterWhen, digits = 0 }) {
  if (diff == null || !Number.isFinite(diff)) return null;
  const rounded = Math.round(diff * 10 ** digits) / 10 ** digits;
  if (!rounded) return { tone: "is-flat", icon: "", label: "변동 없음", text: "직전 기간과 같음" };
  const good = betterWhen === "more" ? rounded > 0 : rounded < 0;
  const amount = `${formatNumber(Math.abs(rounded), digits)}${unit}`;
  return { tone: good ? "is-good" : "is-bad", icon: rounded > 0 ? "arrow-up" : "arrow-down", label: amount,
    text: `직전 기간보다 ${amount} ${rounded > 0 ? "늘었어요" : "줄었어요"}` };
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
  const complete = isComplete();
  const previousClosed = summary.previousClosedCount == null ? "직전 기간 비교 없음" : `직전 ${period}일 ${formatNumber(summary.previousClosedCount)}건`;
  const closedDelta = complete && summary.previousClosedCount != null
    ? changeBadge(Number(summary.closedCount || 0) - Number(summary.previousClosedCount), { unit: "건", betterWhen: "more" }) : null;
  const medianChange = complete && summary.medianHandlingDays != null && summary.previousMedianHandlingDays != null
    ? changeBadge(summary.medianHandlingDays - summary.previousMedianHandlingDays, { unit: "일", betterWhen: "less", digits: 1 }) : null;
  return `
    <section class="workspace-kpis effort-board-metrics" aria-label="처리시간 핵심 지표">
      ${kpi("해결 · 종료", countText(summary.closedCount), previousClosed, { unit: complete ? "건" : "", delta: closedDelta })}
      ${kpi("처리일 중앙값", formatDays(summary.medianHandlingDays), complete ? medianDelta(summary) : "확인한 해결·종료 이슈 기준", { tone: complete && summary.medianHandlingDays > (summary.previousMedianHandlingDays ?? Infinity) ? "is-warning" : "", unit: summary.medianHandlingDays == null ? "" : "일", delta: medianChange })}
      ${kpi("처리일 상위 10%", formatDays(summary.p90HandlingDays), "오래 걸린 10%는 이 일수 이상", { unit: summary.p90HandlingDays == null ? "" : "일" })}
      ${kpi("장기 미종료", countText(summary.agingOpen), `${data.meta?.agingDays || 30}일 이상 · 7일 안에 ${formatNumber(summary.agingSoon)}건 더`, { tone: summary.agingOpen ? "is-warning" : "", unit: complete ? "건" : "" })}
      ${kpi("기록된 공수", `${formatNumber(effort.total, 2)}${complete ? "" : "+"}`, `공수 기록 이슈 ${formatNumber(effort.issueCount)}건`)}
    </section>`;
}

// 카드 머리: 운영 현황 카드처럼 제목·설명은 왼쪽, 건수 같은 짧은 값은 오른쪽.
function sectionHeader(title, meta = "", subtitle = "") {
  return `<header class="ops-card-head"><div><h3>${escapeHtml(title)}</h3>${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ""}</div>${meta ? `<em class="ops-card-meta">${escapeHtml(meta)}</em>` : ""}</header>`;
}

function columnChart(rows, { label, valueText, tone }) {
  const max = Math.max(1, ...rows.map((row) => Number(row.value) || 0));
  return `
    <div class="effort-columns" role="img" aria-label="${escapeHtml(label)}">
      ${rows.map((row) => `
        <div class="effort-column">
          <b>${escapeHtml(valueText(row))}</b>
          <span class="effort-column-track" aria-hidden="true">${Number(row.value) > 0 ? `<i class="${tone(row)}" style="height:${Math.round((Number(row.value) / max) * 100)}%"></i>` : ""}</span>
          <small>${escapeHtml(row.label)}</small>
        </div>`).join("")}
    </div>`;
}

function renderDistribution(data) {
  const rows = (data.distribution || []).map((row) => ({ ...row, value: row.count }));
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  const label = rows.map((row) => `${row.label} ${row.count}건`).join(", ");
  return `
    <section class="workspace-section ops-card" aria-label="처리일 분포">
      ${sectionHeader("처리일 분포", `해결·종료 ${formatNumber(total)}건`, "등록일부터 마지막 수정일까지를 처리일로 세요.")}
      <div class="customer-detail-section-body">
        ${total ? columnChart(rows, { label: `처리일 분포: ${label}`, valueText: (row) => formatNumber(row.count), tone: (row) => (row.key === "31+" ? "is-late" : "") })
          : `<p class="customer-detail-empty">${isComplete()
            ? "이 기간에 해결·종료된 이슈가 없어요."
            : "일부 데이터만 확인해 처리일을 계산할 종료 이슈를 찾지 못했어요. 전체 결과는 미확정이에요."}</p>`}
      </div>
    </section>`;
}

function renderWeekly(data) {
  const rows = (data.weekly || []).map((row) => ({ ...row, value: row.medianDays }));
  const label = rows.map((row) => `${row.label} 주 ${row.medianDays == null ? "종료 없음" : `${row.medianDays}일`}`).join(", ");
  return `
    <section class="workspace-section ops-card" aria-label="주별 처리일 중앙값">
      ${sectionHeader("주별 처리일 중앙값", "최근 8주", "주는 월요일부터 세요. 이번 주는 오늘까지만 반영해요.")}
      <div class="customer-detail-section-body">
        ${rows.some((row) => row.medianDays != null)
          ? columnChart(rows, { label: `주별 처리일 중앙값: ${label}`, valueText: (row) => (row.medianDays == null ? "—" : `${formatDays(row.medianDays)}일`), tone: (row) => (row.partialWeek ? "is-partial" : "") })
          : `<p class="customer-detail-empty">${isComplete() ? "최근 8주에 처리일을 계산할 해결·종료 이슈가 없어요." : "확인한 범위에는 주별 처리일을 계산할 해결·종료 이슈가 없어요. 전체 결과는 미확정이에요."}</p>`}
      </div>
    </section>`;
}

// marks: 고객사 목록이면 이름 앞에 로고를 둔다.
function rankedList(rows, { name, value, width, action = "", marks = false }) {
  const max = Math.max(1, ...rows.map(width));
  const label = (row) => marks ? `${customerMark(name(row))}<span>${escapeHtml(name(row))}</span>` : escapeHtml(name(row));
  return `<ol class="customer-ranked-list effort-ranked-list${marks ? " has-marks" : ""}">${rows.map((row) => `
    <li>
      ${action ? `<button type="button" class="customer-issue-link effort-ranked-name" ${action(row)}>${label(row)}</button>` : `<span class="customer-ranked-name">${label(row)}</span>`}
      <span class="customer-ranked-track" aria-hidden="true"><i style="width:${Math.round((width(row) / max) * 100)}%"></i></span>
      <span class="customer-ranked-value">${value(row)}</span>
    </li>`).join("")}</ol>`;
}

function renderCustomers(data) {
  const rows = data.customers || [];
  return `
    <section class="workspace-section ops-card" aria-label="고객사별 처리일 중앙값">
      ${sectionHeader("고객사별 처리일 중앙값", "긴 순", "종료 3건 이상 고객사 · 이름을 누르면 고객사 상세")}
      <div class="customer-detail-section-body">
        ${rows.length ? rankedList(rows, {
          name: (row) => row.name,
          value: (row) => `${formatDays(row.medianDays)}일 <small>· ${formatNumber(row.closedCount)}건</small>`,
          width: (row) => Number(row.medianDays) || 0,
          action: (row) => `data-effort-customer="${escapeHtml(row.customer)}" aria-label="${escapeHtml(row.name)} 고객사 상세 보기"`,
          marks: true,
        }) : `<p class="customer-detail-empty">${isComplete() ? "비교할 만큼 종료된 고객사가 없어요." : "확인한 범위에는 비교할 만큼 종료된 고객사가 없어요."}</p>`}
      </div>
    </section>`;
}

function renderEffort(data) {
  const effort = data.effort || { types: [] };
  const rows = effort.types || [];
  return `
    <section class="workspace-section ops-card" aria-label="유형별 공수">
      ${sectionHeader("유형별 공수", "고객사전달일 기준", "Mantis 공수 필드(문의·서버·PC·QC) 합계와 건당 평균")}
      <div class="customer-detail-section-body">
        ${effort.total ? rankedList(rows, {
          name: (row) => row.label,
          value: (row) => `${formatNumber(row.total, 2)} <small>· 건당 ${row.averagePerIssue == null ? "—" : formatNumber(row.averagePerIssue, 2)}</small>`,
          width: (row) => Number(row.total) || 0,
        }) : `<p class="customer-detail-empty">${isComplete() ? "이 기간에 고객사에 전달된 이슈의 공수 기록이 없어요." : "확인한 범위에는 공수 기록이 없어요. 전체 결과는 미확정이에요."}</p>`}
      </div>
    </section>`;
}

function renderAging(data) {
  const issues = data.agingIssues || [];
  const total = Number(data.agingIssuesTotal || issues.length);
  const agingDays = data.meta?.agingDays || 30;
  const visible = boardState.agingExpanded ? issues : issues.slice(0, AGING_PREVIEW_COUNT);
  const rows = visible.map((issue) => `
    <tr>
      <td class="effort-aging-main"><div class="effort-issue-meta"><span>#${escapeHtml(issue.id)}</span><span class="status-badge">${escapeHtml(workspaceStatusLabel(issue.status))}</span></div>
        <button type="button" class="customer-issue-link effort-issue-title" data-effort-source="${escapeHtml(issue.id)}" aria-label="#${escapeHtml(issue.id)} ${escapeHtml(issue.title)} 상세 보기">${escapeHtml(issue.title)}</button></td>
      <td data-label="고객사">${escapeHtml(issue.customerName || "—")}</td>
      <td data-label="담당자">${escapeHtml(issue.handler || "—")}</td>
      <td data-label="경과" class="is-number ${issue.aging ? "is-aging" : "is-soon"}"><strong>${formatNumber(issue.ageDays)}일</strong><small class="effort-age-label">${issue.aging ? "장기 미종료" : "7일 내 장기 전환"}</small></td>
    </tr>`).join("");
  return `
    <section class="workspace-section ops-card effort-aging" aria-label="장기 미종료와 곧 장기가 될 이슈">
      ${sectionHeader("장기 미종료 · 곧 장기가 될 이슈", `${formatNumber(visible.length)} / ${formatNumber(total)}건${isComplete() ? "" : " 이상"}`, `${agingDays}일 이상 또는 7일 내 전환 · 경과일 긴 순`)}
      <div class="customer-detail-section-body">
        ${issues.length ? `<div class="customer-issue-table-wrap" id="effortAgingList"><table class="customer-issue-table effort-aging-table">
          <thead><tr><th scope="col">이슈</th><th scope="col">고객사</th><th scope="col">담당자</th><th scope="col" class="is-number">경과</th></tr></thead>
          <tbody>${rows}</tbody></table></div>` : `<p class="customer-detail-empty">${isComplete() ? "장기 미종료이거나 곧 넘을 이슈가 없어요." : "확인한 범위에는 장기 미종료이거나 곧 넘을 이슈가 없어요. 전체 결과는 미확정이에요."}</p>`}
        ${issues.length > AGING_PREVIEW_COUNT ? `<button type="button" class="customer-more-button" data-effort-expand aria-expanded="${boardState.agingExpanded}" aria-controls="effortAgingList">${boardState.agingExpanded ? "접기" : `조회된 ${formatNumber(issues.length)}건 모두 보기`}</button>` : ""}
        ${total > issues.length ? `<p class="customer-detail-note">나머지 ${formatNumber(total - issues.length)}건은 이슈·분포에서 확인할 수 있어요.</p>` : ""}
      </div>
    </section>`;
}

function toolbarSubtitle() {
  const meta = boardState.data?.meta;
  return meta
    ? `${formatPeriodRange(meta.periodFrom, meta.periodTo)} · 종료일은 마지막 수정일 기준`
    : "처리시간 데이터를 조회하고 있어요.";
}

function renderTools() {
  const periods = PERIODS.map((days) => `<button type="button" data-effort-period="${days}" aria-pressed="${boardState.period === days}">${days}일</button>`).join("");
  return `
    <span class="data-badge-slot" data-effort-badge></span>
    <fieldset class="dashboard-period"><legend class="sr-only">조회 기간</legend>${periods}</fieldset>
    <button type="button" class="secondary-button workspace-refresh-button" data-effort-refresh aria-label="새로고침">${icon("refresh", 16)}<span>새로고침</span></button>`;
}

function renderBody() {
  const data = boardState.data;
  if (!data) {
    if (boardState.error) {
      return `<div class="workspace-error">${escapeHtml(boardState.error)} <button type="button" class="secondary-button" data-effort-refresh>다시 시도</button></div>`;
    }
    return '<div class="workspace-loading">공수·처리시간을 불러오는 중이에요.</div>';
  }
  return `
    ${boardState.error ? `<p class="customer-detail-error" role="alert">${escapeHtml(boardState.error)}</p>` : ""}
    ${isComplete() ? "" : '<p class="customer-detail-note">일부 데이터만 확인했어요. 건수·공수는 확인한 범위의 최소값이며, 처리일은 확인한 해결·종료 이슈 기준이에요.</p>'}
    ${renderMetrics(data)}
    ${renderAging(data)}
    <div class="effort-board-grid">${renderDistribution(data)}${renderWeekly(data)}${renderCustomers(data)}${renderEffort(data)}</div>
    <p class="effort-board-disclaimer">${icon("info", 16)}<span>공수는 Mantis 공수 필드(문의·서버·PC·QC)에 기록된 값만 합산해요. 난이도·휴가를 반영한 실제 업무량이나 개인 성과 평가가 아니므로 담당자별 순위는 보여 주지 않아요.</span></p>`;
}

function updateBadge(container) {
  const slot = container?.querySelector("[data-effort-badge]");
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
  const { subtitle, tools } = tabHeaderSlots("effort");
  const focused = focusKey(view) || focusKey(tools);
  if (subtitle) subtitle.textContent = subtitle.title = toolbarSubtitle();
  if (tools) tools.innerHTML = renderTools();
  view.innerHTML = `<div class="dashboard-tab-inner"><div class="customer-detail-body" aria-busy="${boardState.loading}">${renderBody()}</div></div>`;
  for (const container of [view, tools]) if (container) restoreFocus(container, focused);
  updateBadge(tools);
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
    if (!response.ok) throw new Error(payload.detail || "공수·처리시간을 불러오지 못했어요.");
    boardState.data = payload;
    boardState.error = "";
  } catch (error) {
    if (requestId !== boardState.requestId) return;
    boardState.error = error.message || "공수·처리시간을 불러오지 못했어요.";
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
  if (target.closest("[data-effort-expand]")) {
    boardState.agingExpanded = !boardState.agingExpanded;
    render();
    return viewElement()?.querySelector("[data-effort-expand]")?.focus({ preventScroll: true });
  }
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
  tabHeaderSlots("effort").tools?.addEventListener("click", handleClick);
  registerDashboardTab("effort", {
    view: viewElement,
    onShow: () => { if (!boardState.loading) void loadEffortBoard(); },
  });
}

export { initEffortBoard, loadEffortBoard };
