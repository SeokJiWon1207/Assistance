import { chartHost, mountCharts, releaseCharts } from "./bklit_charts.js?v=20261026-1";
import { renderWeeklyIssues } from "./dashboard_weekly.js?v=20261029-1";
import { formatUiDateTime, formatPeriodRange, customerName, customerLabelText } from "./ui_format.js?v=20260911-6";
import { state, els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { closeAccountMenu, closeMobileSidebar } from "./sidebar.js?v=20260911-6";
import { ensureMyWorkspaceFresh, showTeamBrief, workspaceStatusLabel, workspaceIssueContext } from "./workspace.js?v=20260911-6";
import { openDialogFocus, closeDialogFocus } from "./dialog_focus.js?v=20260911-6";
import { revealOnScroll } from "./motion.js?v=20260911-6";
import { recordIssueView } from "./recent_items.js?v=20261105-1";
import { recentOpenCard, opsKpiTiles, priorityTable } from "./dashboard_overview.js?v=20261105-1";
import { registerDashboardTab, selectDashboardTab, currentDashboardTab } from "./dashboard_tabs.js?v=20261110-1";

import { analyticsCustomerLabel, analyticsCustomerOption, analyticsScope, renderAnalyticsControls, renderOverview, renderComparison, renderFlow } from "./dashboard_analytics.js?v=20261111-1";

const dashboardViewStorageKey = "team-ai-assistant-main-view-v1";
let issueDetailContext = null;
const roleLabels = { member: "팀원", manager: "팀장", admin: "관리자", shared: "공용 계정" };
const metricFilterLabels = { overdue: "일정 지연", aging: "장기 미종료", highRisk: "고위험" };
// 장기 미종료 지표(30일 이상)와 같은 경계로 나눠 차트와 지표 카드의 숫자가 맞게 한다.
const AGING_DAYS = 30;
const AGE_BUCKETS = [
  { key: "0-7", label: "7일 이하", min: 0, max: 7, tone: "fresh" },
  { key: "8-29", label: "8–29일", min: 8, max: 29, tone: "rest" },
  { key: "30-59", label: "30–59일", min: 30, max: 59, tone: "aging" },
  { key: "60+", label: "60일 이상", min: 60, max: Infinity, tone: "overdue" },
];
const BKLIT_SEGMENTS = [{ key: "overdue", label: "지연", color: "var(--danger)" }, { key: "aging", label: "장기(지연 제외)", color: "var(--warning)" }, { key: "rest", label: "그 외", color: "var(--chart-1)" }];
const SEGMENT_LABELS = { overdue: "지연", aging: "장기", rest: "그 외" };
const CHART_CUSTOMER_LIMIT = 6;
const PRIORITY_PREVIEW_LIMIT = 5;

const dashboardState = {
  data: null,
  loading: false,
  error: "",
  period: 30,
  metric: "",
  customer: "",
  age: "",
  prioritiesExpanded: false,
  selectedIssueId: "",
  intakeMedia: "",
  comparisonDimension: "customer",
  initialized: false,
};
let dashboardLoadPromise = null;
let dashboardFreshPromise = null;
let dashboardLoadIsRefresh = false;
let dashboardSyncTimer = null;
let dashboardRefreshAfter = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("ko-KR");
}


function formatDateTime(value) {
  return formatUiDateTime(value);
}

function riskLabel(level) {
  return { high: "위험", attention: "주의", watch: "관찰" }[level] || "관찰";
}

function priorityLabel(value) {
  const raw = String(value || "").trim();
  return { none: "없음", low: "낮음", normal: "보통", high: "높음", urgent: "긴급", immediate: "즉시" }[raw.toLowerCase()] || raw || "미상";
}

function emptyBlock(message, partialMessage = "표시할 항목을 찾지 못했어요. 전체 결과는 미확정이에요.") {
  const complete = dashboardState.data?.meta?.search_complete === true;
  return `<div class="workspace-empty${complete ? "" : " is-warning"}"><span aria-hidden="true">${icon(complete ? "check" : "warning", 20)}</span><p>${escapeHtml(complete ? message : `확인한 범위 기준 · ${partialMessage}`)}</p></div>`;
}

function sectionHeader(title, meta = "") {
  return `<header><div><h3>${escapeHtml(title)}</h3></div>${meta ? `<em>${escapeHtml(meta)}</em>` : ""}</header>`;
}

// 지연을 장기보다 먼저 센다. 지연이면서 30일 넘은 이슈를 두 구간에 겹쳐 세지 않는다.
function workloadSegment(issue) {
  if (issue.overdue) return "overdue";
  return Number(issue.ageDays || 0) >= AGING_DAYS ? "aging" : "rest";
}

function ageBucket(issue) {
  if (issue.ageDays == null || issue.ageDays === "") return "";
  const days = Number(issue.ageDays);
  return AGE_BUCKETS.find((bucket) => days >= bucket.min && days <= bucket.max)?.key || "";
}

function segmentTotal(counts) {
  return counts.overdue + counts.aging + counts.rest;
}

function observedCount(value) {
  return `${formatNumber(value)}건${dashboardState.data?.meta?.search_complete === true ? "" : " 이상"}`;
}

function segmentText(counts) {
  return Object.entries(SEGMENT_LABELS).map(([key, label]) => `${label} ${observedCount(counts[key])}`).join(", ");
}

function stackedTrack(counts, max) {
  const total = segmentTotal(counts);
  const parts = Object.keys(SEGMENT_LABELS).filter((key) => counts[key])
    .map((key) => `<i class="is-${key}" style="width:${Math.round((counts[key] / total) * 1000) / 10}%"></i>`).join("");
  const width = max ? Math.round((total / max) * 1000) / 10 : 0;
  return `<span class="dashboard-track" aria-hidden="true"><span style="width:${width}%">${parts}</span></span>`;
}

function chartLegend() {
  return `<span class="dashboard-chart-note">지연과 장기가 겹치면 지연으로 집계해요.</span>`;
}

function spikeSummary(item, customer = false) {
  return `${customer ? customerName(item.name) : item.name} ${formatNumber(item.count)}건(직전 ${formatNumber(item.previousCount)}건)`;
}

function renderCustomerChart(openIssues, health, customerSpikes) {
  const groups = new Map();
  openIssues.forEach((issue) => {
    const name = issue.project || "고객사 미상";
    const counts = groups.get(name) || { overdue: 0, aging: 0, rest: 0 };
    counts[workloadSegment(issue)] += 1;
    groups.set(name, counts);
  });
  const rows = [...groups.entries()]
    .sort(([leftName, left], [rightName, right]) => segmentTotal(right) - segmentTotal(left) || leftName.localeCompare(rightName, "ko"))
    .slice(0, CHART_CUSTOMER_LIMIT);
  const levels = new Map(health.map((item) => [item.customer, item]));
  const spikes = new Map(customerSpikes.map((item) => [item.name, item]));
  const max = rows.length ? segmentTotal(rows[0][1]) : 0;
  const bars = rows.map(([name, counts]) => {
    const customerHealth = levels.get(name);
    const spike = spikes.get(name);
    const spikeText = spike ? (spike.ratio == null ? "신규 급증" : `급증 ×${spike.ratio}`) : "";
    const selected = dashboardState.customer === name;
    const label = [`${customerName(name)} 미종료 ${observedCount(segmentTotal(counts))}`, segmentText(counts), customerHealth?.label,
      spike ? `급증 직전 ${spike.previousCount}건에서 ${spike.count}건` : ""].filter(Boolean).join(", ");
    return `
      <button type="button" class="dashboard-bar dashboard-clickable${selected ? " is-selected" : ""}" data-dashboard-customer="${escapeHtml(name)}" aria-pressed="${selected}" aria-label="${escapeHtml(label)}">
        <span class="dashboard-bar-name"><i class="health-status is-${escapeHtml(customerHealth?.level || "unknown")}" aria-hidden="true"></i><span>${escapeHtml(customerName(name))}</span></span>
        ${stackedTrack(counts, max)}
        <span class="dashboard-bar-value">${observedCount(segmentTotal(counts))}${spikeText ? `<b class="dashboard-spike-badge" title="${escapeHtml(spikeSummary(spike, true))}">${escapeHtml(spikeText)}</b>` : ""}</span>
      </button>`;
  }).join("");
  const hidden = groups.size - rows.length;
  const otherCount = [...groups.values()].reduce((sum, counts) => sum + segmentTotal(counts), 0) - rows.reduce((sum, [, counts]) => sum + segmentTotal(counts), 0);
  return `
    <section class="workspace-section dashboard-chart" aria-label="고객사별 미종료">
      ${sectionHeader("고객사별 미종료", "막대를 누르면 목록이 걸러져요")}
      <div class="dashboard-chart-body">
        ${rows.length ? chartHost({ kind: "customer-ranking", showValues: true, otherCustomers: hidden, otherCount, title: "고객사별 미종료", complete: dashboardState.data?.meta?.search_complete === true, rows: rows.map(([name, counts]) => ({ id: name, name: customerName(name), fullLabel: customerName(name), ...counts })), series: BKLIT_SEGMENTS, action: "data-dashboard-customer", selected: dashboardState.customer }, `<div class="dashboard-bars">${bars}</div>`) : emptyBlock("미종료 이슈가 없어요.")}
        ${hidden > 0 ? `<p class="dashboard-chart-note">그 외 고객사 ${hidden}곳 · ${observedCount(otherCount)}은 우선 확인 이슈 목록에서 볼 수 있어요.</p>` : ""}
      </div>
    </section>`;
}

function renderSpikeSignals(customerSpikes, screenSpikes) {
  if (!customerSpikes.length && !screenSpikes.length) return "";
  const group = (items, title, customer) => items.length ? `<section><h3>${title}</h3><ul aria-label="${title} 급증 신호">${items.map(item => `<li><span class="dashboard-spike-name">${escapeHtml(customer ? customerName(item.name) : item.name)}</span><span class="dashboard-spike-counts"><strong>${formatNumber(item.count)}건</strong><span>직전 ${formatNumber(item.previousCount)}건</span></span></li>`).join("")}</ul></section>` : "";
  return `<details class="workspace-section dashboard-spikes"><summary><strong>급증 신호</strong><span>고객사 ${customerSpikes.length}곳 · 화면 ${screenSpikes.length}개</span><span class="dashboard-spikes-toggle">목록 보기</span></summary><div class="dashboard-spikes-groups">${group(customerSpikes, "고객사", true)}${group(screenSpikes, "화면", false)}</div></details>`;
}

function renderAgeChart(openIssues) {
  const counts = Object.fromEntries(AGE_BUCKETS.map((bucket) => [bucket.key, 0]));
  let unknown = 0;
  openIssues.forEach((issue) => {
    const key = ageBucket(issue);
    if (key) counts[key] += 1;
    else unknown += 1;
  });
  const max = Math.max(0, ...Object.values(counts));
  const aging = AGE_BUCKETS.filter((bucket) => bucket.min >= AGING_DAYS).reduce((sum, bucket) => sum + counts[bucket.key], 0);
  const columns = AGE_BUCKETS.map((bucket) => {
    const count = counts[bucket.key];
    const selected = dashboardState.age === bucket.key;
    const height = max && count ? Math.max(4, Math.round((count / max) * 100)) : 0;
    return `
      <button type="button" class="dashboard-column dashboard-clickable is-${bucket.tone}${selected ? " is-selected" : ""}" data-dashboard-age="${bucket.key}" aria-pressed="${selected}" aria-label="경과 ${bucket.label} 미종료 ${observedCount(count)}">
        <b>${dashboardState.data?.meta?.search_complete === true ? formatNumber(count) : observedCount(count)}</b>
        <span class="dashboard-column-bar" aria-hidden="true"><i style="height:${height}%"></i></span>
        <small>${bucket.label}</small>
      </button>`;
  }).join("");
  return `
    <section class="workspace-section dashboard-chart dashboard-age-chart" aria-label="미종료 경과일">
      ${sectionHeader("미종료 경과일", "등록 후 지난 일수")}
      <div class="dashboard-chart-body">
        ${openIssues.length ? `${chartHost({kind: "age-donut", showValues: true, unknown, title: "미종료 경과일", complete: dashboardState.data?.meta?.search_complete === true, rows: AGE_BUCKETS.map(bucket => ({id: bucket.key, name: bucket.label, minDays: bucket.min, count: counts[bucket.key]})), series: [{key: "count", label: "미종료", color: "var(--chart-1)"}], action: "data-dashboard-age", selected: dashboardState.age}, `<div class="dashboard-columns">${columns}</div>`)}
        <p class="dashboard-chart-note">30일 이상 ${observedCount(aging)}이 장기 미종료예요.${unknown ? ` 등록일을 확인하지 못한 ${formatNumber(unknown)}건은 제외했어요.` : ""}</p>` : emptyBlock("미종료 이슈가 없어요.")}
      </div>
    </section>`;
}

function renderMemberChart(members, sharedAccounts) {
  const assignees = [...members, ...sharedAccounts];
  if (!assignees.length) return "";
  const rows = assignees.map((member) => {
    const overdue = Number(member.overdue || 0);
    const aging = Number(member.agingOnly || 0);
    return { ...member, counts: { overdue, aging, rest: Math.max(0, Number(member.open || 0) - overdue - aging) } };
  }).sort((left, right) => segmentTotal(right.counts) - segmentTotal(left.counts));
  const max = Math.max(0, ...rows.map((row) => segmentTotal(row.counts)));
  const bars = rows.map((row) => {
    const role = roleLabels[row.role] || row.role;
    const label = `${row.displayName} ${role} 미종료 ${observedCount(segmentTotal(row.counts))}, ${segmentText(row.counts)}, 고위험 ${observedCount(row.highRisk || 0)}`;
    return `
      <div class="dashboard-bar" role="img" aria-label="${escapeHtml(label)}">
        <span class="dashboard-bar-name"><span>${escapeHtml(row.displayName)}</span></span>
        ${stackedTrack(row.counts, max)}
        <span class="dashboard-bar-value">${observedCount(segmentTotal(row.counts))}${row.highRisk ? `<b class="dashboard-spike-badge">고위험 ${observedCount(row.highRisk)}</b>` : ""}</span>
      </div>`;
  }).join("");
  return `
    <section class="workspace-section dashboard-chart dashboard-members-chart" aria-label="담당자별 업무 부하">
      ${sectionHeader("담당자별 부하", `미종료 기준 · 팀원 ${members.length}명${sharedAccounts.length ? ` · 공용 계정 ${sharedAccounts.length}개` : ""}`)}
      <div class="dashboard-chart-body">${chartHost({kind: "member-heatmap", showValues: true, title: "담당자별 부하", complete: dashboardState.data?.meta?.search_complete === true, rows: rows.map(row => ({id: row.mantisUsername, name: row.displayName, ...row.counts})), series: BKLIT_SEGMENTS}, `<div class="dashboard-bars">${bars}</div>`)}</div>
    </section>`;
}

function filteredPriorities(priorities) {
  return priorities.filter((item) => {
    if (dashboardState.age && ageBucket(item) !== dashboardState.age) return false;
    if (dashboardState.metric === "overdue" && !item.overdue) return false;
    if (dashboardState.metric === "aging" && Number(item.ageDays || 0) < 30) return false;
    if (dashboardState.metric === "highRisk" && item.riskLevel !== "high") return false;
    return true;
  });
}

// 적용 필터는 본문 밖 헤더 아래 줄에 그려 스크롤 위치와 상관없이 보이고 바로 해제할 수 있게 한다.
function renderFilterbar(filteredCount = 0) {
  if (!els.dashboardFilterbar) return;
  const chips = [];
  if (dashboardState.metric) {
    const label = metricFilterLabels[dashboardState.metric];
    chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="metric" aria-label="${escapeHtml(label)} 필터 해제">${escapeHtml(label)}${icon("close", 14)}</button>`);
  }
  if (dashboardState.customer) {
    const option = analyticsCustomerOption(dashboardState.data, dashboardState.customer);
    const label = escapeHtml(analyticsCustomerLabel(dashboardState.customer));
    const project = option ? option.projects[0] : dashboardState.customer;
    chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="customer" aria-label="고객사 ${label} 필터 해제">고객사: ${label}${icon("close", 14)}</button>`);
    if (project) chips.push(`<button class="dashboard-filter-jump dashboard-customer-detail-link" type="button" data-customer-detail="${escapeHtml(project)}">${label} 상세 보기${icon("chevron-right", 14)}</button>`);
  }
  const age = AGE_BUCKETS.find((bucket) => bucket.key === dashboardState.age);
  if (age) {
    chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="age" aria-label="경과 ${age.label} 필터 해제">경과: ${age.label}${icon("close", 14)}</button>`);
  }
  els.dashboardFilterbar.hidden = false;
  els.dashboardFilterbar.innerHTML = chips.length
    ? `<span class="dashboard-filterbar-label">적용 필터</span>${chips.join("")}
      <button class="dashboard-chip dashboard-chip-clear" type="button" data-dashboard-clear="all">전체 해제</button>
      <button class="dashboard-filter-jump" type="button" data-dashboard-jump>우선 확인 이슈 ${formatNumber(filteredCount)}건 보기${icon("arrow-up", 14, "is-bold")}</button>`
    : `<span class="dashboard-filterbar-label">필터 없음</span>
      <span class="dashboard-filterbar-hint">지표나 분포를 눌러 목록을 좁혀 보세요.</span>`;
  // 저장된 보기 줄(dashboard_views.js)이 지금 조건과 내보낼 건수를 다시 맞춘다.
  globalThis.dispatchEvent?.(new Event("dashboard:filters-rendered"));
}

// 저장된 보기가 읽고 되돌리는 필터 조합. 기간이 바뀌면 데이터를 다시 불러온다.
function dashboardFilterSnapshot() {
  const { period, metric, customer, age } = dashboardState;
  return { period, metric, customer, age };
}

function applyDashboardFilters({ period = dashboardState.period, metric = "", customer = "", age = "" } = {}) {
  const nextPeriod = [7, 14, 30, 90].includes(Number(period)) ? Number(period) : dashboardState.period;
  Object.assign(dashboardState, { metric: metric || "", customer: customer || "", age: age || "", prioritiesExpanded: false });
  if (nextPeriod !== dashboardState.period) {
    dashboardState.period = nextPeriod;
    syncPeriodButtons();
    return loadDashboard(true);
  }
  renderDashboard();
  return Promise.resolve(dashboardState.data);
}

function dashboardPriorityRows() {
  if (!dashboardState.data) return [];
  const data = analyticsScope(dashboardState.data, dashboardState.customer);
  return filteredPriorities(data.openIssues || data.priorities || []);
}

function hideFilterbar() {
  if (els.dashboardFilterbar) els.dashboardFilterbar.hidden = true;
}

function renderPriorities(priorities, filtered) {
  const filtering = Boolean(dashboardState.metric || dashboardState.customer || dashboardState.age);
  const shown = dashboardState.prioritiesExpanded ? filtered : filtered.slice(0, PRIORITY_PREVIEW_LIMIT);
  const remaining = filtered.length - shown.length;
  const riskCounts = { high: 0, attention: 0, watch: 0 };
  const levelOf = (issue) => ["high", "attention"].includes(issue.riskLevel) ? issue.riskLevel : "watch";
  filtered.forEach((issue) => { riskCounts[levelOf(issue)] += 1; });
  const riskSummary = Object.entries(riskCounts).map(([level, count]) =>
    `<li class="dashboard-priority-count is-${level}" aria-label="${riskLabel(level)} ${observedCount(count)}"><i aria-hidden="true"></i>${riskLabel(level)}<b>${formatNumber(count)}</b></li>`).join("");
  const toggle = remaining > 0
    ? `<button type="button" class="dashboard-more" data-dashboard-more aria-expanded="false">나머지 ${formatNumber(remaining)}건 보기</button>`
    : (dashboardState.prioritiesExpanded && filtered.length > PRIORITY_PREVIEW_LIMIT
      ? `<button type="button" class="dashboard-more" data-dashboard-more aria-expanded="true">상위 ${PRIORITY_PREVIEW_LIMIT}건만 보기</button>` : "");
  const rows = shown.map((issue) => {
    const level = levelOf(issue);
    const handler = issue.handler || "미지정";
    const reasons = (issue.riskReasons || []).join(" · ");
    return `
    <tr class="dashboard-clickable${String(issue.id) === dashboardState.selectedIssueId ? " is-selected" : ""}" data-dashboard-row-issue="${escapeHtml(issue.id)}" aria-selected="${String(issue.id) === dashboardState.selectedIssueId}">
      <td class="dashboard-priority-main is-${level}">
        <button type="button" class="dashboard-issue-button" data-dashboard-issue="${escapeHtml(issue.id)}">
          <span class="dashboard-issue-eyebrow"><span class="dashboard-issue-id">#${escapeHtml(issue.id)}</span><span class="dashboard-issue-status">${escapeHtml(workspaceStatusLabel(issue.status))}</span></span>
          <strong class="dashboard-issue-title">${escapeHtml(issue.title)}</strong>
        </button>
        ${reasons ? `<small class="dashboard-issue-reason" title="${escapeHtml(reasons)}">${escapeHtml(reasons)}</small>` : ""}
      </td>
      <td class="dashboard-priority-customer" data-label="고객사"><span>${escapeHtml(customerName(issue, "-"))}</span></td>
      <td class="dashboard-priority-handler" data-label="담당자"><span class="dashboard-handler">${escapeHtml(handler)}</span></td>
      <td class="dashboard-priority-age" data-label="경과"><span>${issue.ageDays == null ? "–" : `<b>${formatNumber(issue.ageDays)}</b>일`}</span></td>
      <td class="dashboard-priority-risk" data-label="위험"><span class="dashboard-risk-badge is-${level}">${riskLabel(level)}</span><span class="dashboard-priority-arrow" aria-hidden="true">${icon("chevron-right", 16)}</span></td>
    </tr>`;
  }).join("");
  return `
    <section class="workspace-section dashboard-priority-section${filtering ? " is-filtered" : ""}" tabindex="-1" aria-label="우선 확인 이슈">
      ${sectionHeader("우선 확인 이슈", `${filtered.length} / ${priorities.length}건`)}
      ${filtered.length ? `<div class="dashboard-priority-overview"><div><span class="dashboard-priority-scope">${dashboardState.data?.meta?.search_complete === true ? "현재 목록" : "확인한 범위"}</span><ul aria-label="현재 목록 위험도">${riskSummary}</ul></div><span class="dashboard-priority-preview">${remaining > 0 ? `${shown.length}건 미리보기` : `${formatNumber(shown.length)}건 표시`} · 이슈를 눌러 상세 보기</span></div>
      <div class="workspace-table-wrap"><table class="workspace-table dashboard-priority-table" aria-label="우선 확인 이슈 목록"><thead><tr><th scope="col">이슈</th><th scope="col">고객사</th><th scope="col">담당자</th><th scope="col">경과</th><th scope="col">위험</th></tr></thead><tbody>${rows}</tbody></table></div>${toggle}` : emptyBlock("현재 필터에 해당하는 우선 이슈가 없어요.")}
    </section>`;
}

// 데이터 출처·범위·조회 시각을 공통 '데이터 기준' 배지 하나로 보여 준다.
function updateSourceMeta(meta, { loading = false, liveFailed = false } = {}) {
  const source = liveFailed ? "snapshot" : meta?.source || "error";
  const state = dataBadgeState({
    source,
    complete: meta?.search_complete !== false,
    refreshing: Boolean(meta?.refreshPending),
    loading,
  });
  const time = formatBadgeTime(meta?.fetchedAt);
  const fetchedLabel = meta?.fetchedAt ? `${formatDateTime(meta.fetchedAt)} 조회` : "조회 시각 미확인";
  const details = mantisBadgeDetails(state, { time, reason: meta?.sourceReason });
  renderDataBadge(els.dashboardSource, {
    state,
    time: ["fresh", "partial", "snapshot"].includes(state) ? time : "",
    detailTime: loading ? "" : fetchedLabel,
    sources: [details.source, ...(meta?.scopeLabel ? [{ name: "범위", tone: "muted", text: meta.scopeLabel }] : [])],
    note: details.note,
    onRetry: details.retry ? () => loadDashboard(true) : null,
  });
}

function syncPeriodButtons() {
  els.dashboardPeriod.querySelectorAll?.("[data-dashboard-period]").forEach((button) => {
    button.setAttribute("aria-pressed", String(Number(button.dataset.dashboardPeriod) === dashboardState.period));
  });
}

function setRefreshLabel(label) {
  els.dashboardRefreshBtn.innerHTML = `${icon("refresh", 16)}<span>${escapeHtml(label)}</span>`;
}

// 로딩·오류 화면 다음에 데이터가 처음 보일 때만 구역을 스크롤 등장시킨다.
// 필터 클릭이나 백그라운드 갱신으로 다시 그릴 때는 바로 보여 준다.
const DASHBOARD_REVEAL_SELECTOR = ":scope > :not(.dashboard-grid), :scope > .dashboard-grid > *";
let dashboardContentShown = false;
let stopDashboardReveal = () => {};

function renderDashboard() {
  if (!els.dashboardBody) return;
  releaseCharts(els.dashboardBody);
  stopDashboardReveal();
  stopDashboardReveal = () => {};
  const loading = dashboardState.loading || Boolean(dashboardFreshPromise);
  els.dashboardBody.setAttribute("aria-busy", String(loading));
  els.dashboardRefreshBtn.disabled = loading;
  setRefreshLabel(loading ? "불러오는 중…" : "새로고침");
  els.dashboardRefreshBtn.setAttribute("aria-busy", String(loading));
  els.dashboardPeriod.disabled = loading;
  if (loading && !dashboardState.data) {
    updateSourceMeta(dashboardState.data?.meta, { loading: true });
    els.dashboardSubtitle.textContent = "운영 데이터를 조회하고 있어요.";
    hideFilterbar();
    dashboardContentShown = false;
    // 숫자 대신 실제 화면과 같은 자리·크기의 뼈대를 둬 다 불러왔을 때 화면이 튀지 않게 한다.
    els.dashboardBody.innerHTML = `
      <div class="dashboard-loading" role="status">
        <p class="dashboard-loading-status"><span class="dashboard-spinner" aria-hidden="true"></span><span><strong>운영 데이터를 불러오고 있어요.</strong><small>고객사 건강도와 위험 신호를 같은 기준으로 계산해요.</small></span></p>
        <div class="dashboard-skel" aria-hidden="true"><i class="is-banner"></i><i class="is-strip"></i><span><i></i><i></i><i></i></span><i class="is-table"></i></div>
      </div>`;
    return;
  }
  if (dashboardState.error && !dashboardState.data) {
    hideFilterbar();
    dashboardContentShown = false;
    els.dashboardBody.innerHTML = `<div class="dashboard-state is-error" role="alert"><strong>대시보드를 불러오지 못했어요.</strong><p>${escapeHtml(dashboardState.error)}</p><button class="dashboard-ai-button is-secondary" type="button" data-dashboard-retry>다시 조회</button></div>`;
    updateSourceMeta({ source: "error" });
    return;
  }
  const rootData = dashboardState.data;
  if (!rootData) return;
  const data = analyticsScope(rootData, dashboardState.customer);
  updateSourceMeta(data.meta, { loading, liveFailed: Boolean(dashboardState.error) });
  if (data.meta?.source === "error") {
    hideFilterbar();
    dashboardContentShown = false;
    els.dashboardBody.innerHTML = `<div class="dashboard-state is-error" role="alert"><strong>업무 데이터를 확인하지 못했어요.</strong><p>${escapeHtml(data.meta.sourceReason || "잠시 후 다시 조회해 주세요.")}</p><button class="dashboard-ai-button is-secondary" type="button" data-dashboard-retry>다시 조회</button></div>`;
    return;
  }
  const complete = data.meta.search_complete === true;
  // 범위 · 기간 · 비교 기준을 제목 아래 한 줄에 모은다. 본문에서 같은 말을 반복하지 않는다.
  // '일부만 확인'은 데이터 기준 배지와 본문 안내 한 줄이 알려 주므로 제목 아래에서는 반복하지 않는다.
  const scopeLabel = data.meta.scope === "team" ? "팀 전체" : "내 담당 업무";
  const periodDays = data.meta.periodDays || dashboardState.period;
  els.dashboardSubtitle.textContent = els.dashboardSubtitle.title = `${scopeLabel} · ${formatPeriodRange(data.meta.periodFrom, data.meta.periodTo)} · 직전 ${periodDays}일과 비교`;
  const priorities = data.openIssues || data.priorities || [];
  const filtered = filteredPriorities(priorities);
  renderFilterbar(filtered.length);
  // 전체 현황은 지표 → 큰 추이 → 분포 → 관련 이슈. 고객사 범위는 모든 분석에 적용한다.
  // 지연·장기·그 외 범례는 차트마다 반복하지 않고 분포 제목 옆에 한 번만 둔다.
  const memberChart = dashboardState.customer ? "" : renderMemberChart(data.members || [], data.sharedAccounts || []);
  els.dashboardBody.innerHTML = `
    ${dashboardState.error
        ? `<div class="dashboard-refresh-status is-error" role="status"><strong>${escapeHtml(dashboardState.error)} 저장된 데이터를 계속 표시해요.</strong><span>${escapeHtml(formatDateTime(data.meta.fetchedAt))} 기준</span><button type="button" class="secondary-button" data-dashboard-retry>다시 시도</button></div>`
        : ""}
    ${!complete ? '<div class="dashboard-refresh-status is-warning" role="status"><strong>일부 이슈만 확인했어요.</strong><span>건수는 확인한 범위 기준이라 실제보다 적을 수 있어요.</span></div>' : ""}
    ${renderAnalyticsControls(rootData, dashboardState.customer)}
    <div class="ops-pane analytics-pane" data-ops-pane="summary">${renderOverview(data, priorityTable({ rows: filtered.slice(0, 8), complete }))}</div>
    <div class="ops-pane analytics-pane" data-ops-pane="comparison">${renderComparison(data, dashboardState.comparisonDimension)}</div>
    <div class="ops-pane analytics-pane" data-ops-pane="flow">${renderFlow(data)}</div>
    <div class="ops-pane" data-ops-pane="issues">
    ${opsKpiTiles({ summary: data.summary, complete, metric: dashboardState.metric })}
    ${renderPriorities(priorities, filtered)}
    <div class="dashboard-dist-head"><h3>미종료 분포</h3>${chartLegend()}</div>
    <div class="dashboard-grid${memberChart ? "" : " is-two"}">${renderCustomerChart(priorities, data.customerHealth || [], data.customerSpikes || [])}${renderAgeChart(priorities)}${memberChart}</div>
    ${renderSpikeSignals(data.customerSpikes || [], data.screenSpikes || [])}
    <div class="ops-overview-grid">
      ${recentOpenCard({ issues: filtered, statusLabel: workspaceStatusLabel, selectedId: dashboardState.selectedIssueId, complete })}
      <section class="workspace-section dashboard-chart ops-cohort">${sectionHeader("신규 이슈의 현재 상태", "처리 완료 건수와 구분")}<div class="dashboard-chart-body">${chartHost({kind: "cohort", title: "신규 이슈의 현재 상태", complete, summary: data.summary}, `<p>현재 종료 ${observedCount(data.summary.closedNow)} · 현재 미종료 ${observedCount(data.summary.newOpen)} · 상태 미상 ${observedCount(data.summary.unknownStatus)}</p>`)}</div></section>
    </div>
    ${renderWeeklyIssues(data.weeklyIssues)}
    <div class="bklit-insights-grid is-single">
      <section class="workspace-section dashboard-chart">${sectionHeader("신규 이슈 등록 추이", "선택 기간 · 등록일 기준")}<div class="dashboard-chart-body">${chartHost({kind: "trend", title: "신규 이슈 등록 추이", complete, points: data.intakeTrend || []}, `<p>선택 기간 신규 ${observedCount(data.summary.newIssues)} · 일별 차트를 준비하고 있어요.</p>`)}</div></section>
    </div>
    <div class="dashboard-brief-link"><span>지난주 팀 브리핑은 <b>내 업무 › 주간 보고</b>에서 볼 수 있어요.</span><button type="button" class="secondary-button" data-open-team-brief>팀 브리핑 열기 ›</button></div>
    </div>`;
  const customerSelect = els.dashboardBody.querySelector("[data-analytics-customer]");
  if (dashboardState.customer && customerSelect?.value === "") customerSelect.selectedIndex = -1;
  if (!dashboardContentShown) stopDashboardReveal = revealOnScroll(els.dashboardBody, DASHBOARD_REVEAL_SELECTOR);
  mountCharts(els.dashboardBody);
  dashboardContentShown = true;
}

async function loadDashboard(force = false, revalidate = false, background = false) {
  if (!canViewTeamDashboard()) return;
  if (dashboardLoadPromise) {
    const awaitedRefresh = dashboardLoadIsRefresh;
    await dashboardLoadPromise;
    if (force && !awaitedRefresh) return loadDashboard(true);
    return dashboardState.data;
  }
  if (dashboardState.data && !force && !revalidate) return dashboardState.data;
  dashboardLoadIsRefresh = force;
  dashboardState.loading = !background;
  dashboardState.error = "";
  renderDashboard();
  dashboardLoadPromise = (async () => {
    const refresh = force ? "&refresh=true" : "&live=true";
    try {
      const response = await apiFetch(`${WORKSPACE_ENDPOINTS.dashboard}?days=${dashboardState.period}${refresh}`);
      const payload = await responseJson(response);
      if (!response.ok) throw new Error(payload.detail || "운영 데이터를 불러오지 못했어요.");
      dashboardState.data = payload;
      if (force) dashboardRefreshAfter = payload.meta?.refreshAfter ?? null;
      const pending = Boolean(payload.meta?.refreshPending);
      const failed = payload.meta?.refreshFailed || (!pending && dashboardRefreshAfter !== null
        && dashboardRefreshAfter === payload.meta?.fetchedAt);
      if (failed) dashboardState.error = payload.meta?.sourceReason || "최신 데이터를 갱신하지 못했어요.";
      if (!pending) dashboardRefreshAfter = null;
      if (dashboardSyncTimer) clearTimeout(dashboardSyncTimer);
      dashboardSyncTimer = pending ? setTimeout(() => {
        dashboardSyncTimer = null;
        if (state.currentUser?.uid) void loadDashboard(false, true, true);
      }, 2000) : null;
    } catch (error) {
      dashboardState.error = error.message || "운영 데이터를 불러오지 못했어요.";
    }
    return dashboardState.data;
  })();
  try {
    return await dashboardLoadPromise;
  } finally {
    dashboardLoadPromise = null;
    dashboardLoadIsRefresh = false;
    dashboardState.loading = false;
    renderDashboard();
  }
}

async function ensureDashboardFresh() {
  if (dashboardFreshPromise) return dashboardFreshPromise;
  dashboardFreshPromise = Promise.resolve().then(() => loadDashboard(true));
  renderDashboard();
  try {
    return await dashboardFreshPromise;
  } finally {
    dashboardFreshPromise = null;
    renderDashboard();
  }
}

function canViewTeamDashboard() {
  return Boolean(state.currentUser?.uid);
}

// 다른 모듈이 맡은 화면(관리자 센터)은 여기서 불러오는 함수만 등록해 둔다.
const mainViewLoaders = new Map();

function setMainViewLoader(view, loader) {
  mainViewLoaders.set(view, loader);
}

function canViewAdminCenter() {
  return Boolean(state.adminToolsVisible && els.viewAdminBtn && els.adminPanel);
}

function mainViews() {
  return [
    ["chat", els.viewChatBtn, els.chatPanel],
    ["personal", els.viewPersonalBtn, els.personalPanel],
    ["dashboard", els.viewDashboardBtn, els.dashboardPanel],
    ["admin", els.viewAdminBtn, els.adminPanel],
  ].filter(([, button, panel]) => button && panel);
}

function setActiveView(view, { remember = true, load = true } = {}) {
  if (view === "dashboard" && !canViewTeamDashboard()) view = "personal";
  if (view === "admin" && !canViewAdminCenter()) view = "chat";
  if (!["chat", "personal", "dashboard", "admin"].includes(view)) view = "chat";
  if (issueDetailContext && view !== issueDetailContext.view) closeIssueDetail({ restoreFocus: false });
  els.viewDashboardBtn.hidden = !canViewTeamDashboard();
  if (els.viewAdminBtn) els.viewAdminBtn.hidden = !canViewAdminCenter();
  // 주 메뉴는 탭이 아니라 화면 이동이다. 지금 화면만 aria-current로 표시하고 Tab 순서는 그대로 둔다.
  for (const [name, button, panel] of mainViews()) {
    const selected = name === view;
    panel.hidden = !selected;
    button.classList.toggle("is-active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  // 대화 목록은 대화 화면에서만 보인다(.app-shell[data-view]로 레이아웃 열을 바꾼다).
  if (els.appShell) els.appShell.dataset.view = view;
  if (remember) localStorage.setItem(`${dashboardViewStorageKey}:${state.currentUser?.uid || "guest"}`, view);
  closeMobileSidebar({ restoreFocus: false });
  closeAccountMenu();
  if (view === "dashboard" && load) ensureDashboardFresh();
  if (load) mainViewLoaders.get(view)?.();
  if (view === "personal" && load) {
    // 워크스페이스가 만든 오류·재시도 UI를 유지한다.
    ensureMyWorkspaceFresh().catch(() => {});
  }
}

function showPersonalView(options) {
  setActiveView("personal", options);
}

function showDashboardView(options) {
  setActiveView("dashboard", options);
}

function showChatView(options) {
  setActiveView("chat", options);
}

function showAdminView(options) {
  setActiveView("admin", options);
}

function askFromDashboard(question) {
  if (!question) return;
  showChatView();
  els.input.value = question;
  els.input.dispatchEvent(new Event("input", { bubbles: true }));
  els.input.focus();
}

function issueById(id) {
  const items = [...(dashboardState.data?.openIssues || dashboardState.data?.priorities || []), ...(dashboardState.data?.todos || [])];
  return items.find((item) => String(item.id) === String(id));
}

// 상세 드로어의 이전/다음은 지금 필터가 걸린 우선 확인 목록 순서를 따른다.
function detailNavigation(id, context) {
  const list = context.items();
  const index = list.findIndex((item) => String(item.id) === String(id));
  if (index < 0 || list.length < 2) return "";
  const step = (offset, label, iconName) => `<button type="button" class="dashboard-detail-step" data-dashboard-detail-step="${escapeHtml(String(list[index + offset]?.id ?? ""))}" aria-label="${label}"${list[index + offset] ? "" : " disabled"}>${icon(iconName, 16)}</button>`;
  return `<div class="dashboard-detail-nav"><span>${escapeHtml(context.label)} ${index + 1} / ${list.length}</span>${step(-1, "이전 이슈", "chevron-left")}${step(1, "다음 이슈", "chevron-right")}</div>`;
}

function openWorkspaceIssueDetail(id, opener = document.activeElement) {
  const context = workspaceIssueContext();
  openIssueDetail(id, { context: { view: "personal", label: context.label, items: () => workspaceIssueContext().items, opener } });
}

function syncIssueDetailMode() {
  if (!issueDetailContext || els.dashboardDetail.hidden) return;
  if (globalThis.matchMedia?.("(max-width: 1399px)").matches) {
    els.dashboardDetail.setAttribute("role", "dialog");
    els.dashboardDetail.setAttribute("aria-modal", "true");
    openDialogFocus(els.dashboardDetail, { initialFocus: els.dashboardDetailClose, opener: issueDetailContext.opener, onClose: closeIssueDetail,
      fallback: () => issueDetailContext?.view === "personal" ? els.workspaceContent : els.viewDashboardBtn });
  } else {
    closeDialogFocus(els.dashboardDetail, { restoreFocus: false });
    els.dashboardDetail.removeAttribute("aria-modal");
    els.dashboardDetail.removeAttribute("role");
  }
}

function detailElements(context) {
  const personal = context.view === "personal";
  return { panel: personal ? els.personalPanel : els.dashboardPanel, body: personal ? els.workspaceContent : els.dashboardBody, prefix: personal ? "workspace" : "dashboard" };
}

function openIssueDetail(id, { focusSelector = "", context = null } = {}) {
  context ||= { view: "dashboard", label: "우선 확인 이슈", items: () => filteredPriorities(dashboardState.data?.openIssues || dashboardState.data?.priorities || []), opener: document.activeElement };
  const issue = context.items().find((item) => String(item.id) === String(id)) || (context.view === "dashboard" ? issueById(id) : null);
  if (!issue) return;
  recordIssueView(state.currentUser?.uid, { id: issue.id, title: issue.title });
  if (issueDetailContext && issueDetailContext !== context) closeIssueDetail({ restoreFocus: false });
  issueDetailContext = context;
  const { panel, body, prefix } = detailElements(context);
  const riskLevel = issue.isOpen !== false && ["high", "attention", "watch"].includes(issue.riskLevel) ? issue.riskLevel : "";
  const reasons = riskLevel ? issue.riskReasons || [] : [];
  dashboardState.selectedIssueId = context.view === "dashboard" ? String(id) : "";
  context.selectedId = String(id);
  els.dashboardDetailContext.textContent = context.label;
  els.dashboardDetailTitle.textContent = `#${issue.id} 이슈 상세`;
  els.dashboardDetailBody.innerHTML = `
    <div class="dashboard-detail-content">
    ${detailNavigation(issue.id, context)}
    <div class="dashboard-detail-hero">
      <div class="dashboard-detail-labels">${riskLevel ? `<span class="dashboard-risk-badge is-${riskLevel}">${riskLabel(riskLevel)}</span>` : ""}<span class="dashboard-issue-status">${escapeHtml(workspaceStatusLabel(issue.status))}</span><span class="dashboard-detail-age">${issue.ageDays == null ? "경과일 미상" : `<b>${formatNumber(issue.ageDays)}</b>일 경과`}</span></div>
      <p class="dashboard-detail-summary">${escapeHtml(issue.title)}</p>
    </div>
    <section class="dashboard-detail-next" aria-label="권장 다음 행동"><strong class="dashboard-detail-section-title">권장 다음 행동</strong><p>${escapeHtml(issue.recommendedAction || (issue.isOpen === false ? "해결 내용과 종료 이력을 확인하세요." : "현재 상태와 다음 처리 단계를 확인하세요."))}</p></section>
    <section class="dashboard-detail-info" aria-label="기본 정보">
    <strong class="dashboard-detail-section-title">기본 정보</strong>
    <div class="dashboard-detail-meta">
      <div><span>고객사</span><strong>${escapeHtml(customerName(issue, "-"))}</strong></div>
      <div><span>담당자</span><strong>${escapeHtml(issue.handler || "미지정")}</strong></div>
      <div><span>우선순위</span><strong>${escapeHtml(priorityLabel(issue.priority))}</strong></div>
      <div><span>화면번호</span><strong>${escapeHtml(issue.screenNumber || "-")}</strong></div>
      <div><span>등록일</span><strong>${escapeHtml(issue.createdAt || "-")}</strong></div>
      <div${issue.overdue ? ' class="is-overdue"' : ""}><span>완료 예정일${issue.overdue ? '<em>지연</em>' : ""}</span><strong>${escapeHtml(issue.dueDate || "-")}</strong></div>
    </div>
    </section>
    ${reasons.length ? `<section class="dashboard-detail-reason is-${riskLevel}" aria-label="위험 판단 근거"><strong class="dashboard-detail-section-title">판단 근거</strong><ul>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></section>` : ""}
    </div>
    <div class="dashboard-detail-footer">
    <div class="dashboard-detail-actions"><button type="button" class="secondary-button" data-dashboard-source="${escapeHtml(issue.id)}">이슈 원문 보기</button><button type="button" class="secondary-button" data-dashboard-question="#${escapeHtml(issue.id)} 이슈와 유사한 과거 사례의 원인과 해결 방법을 찾아줘">유사 이슈 찾기</button></div>
    <button class="dashboard-ai-button" type="button" data-dashboard-question="#${escapeHtml(issue.id)} 이슈의 현재 위험, 막힌 점과 다음 행동을 정리해줘">AI에게 이 이슈 질문하기</button>
    </div>`;
  els.dashboardDetail.hidden = false;
  panel.classList.add?.("has-detail");
  body.querySelectorAll?.(`[data-${prefix}-row-issue]`).forEach((row) => {
    const selected = row.getAttribute(`data-${prefix}-row-issue`) === String(id);
    row.classList.toggle("is-selected", selected);
    if (context.view === "dashboard") row.setAttribute("aria-selected", String(selected));
  });
  body.querySelector(`[data-${prefix}-row-issue="${CSS.escape(String(id))}"]`)?.scrollIntoView?.({ block: "nearest" });
  syncIssueDetailMode();
  // 이전/다음으로 넘길 때는 누른 버튼에 포커스를 두고, 끝에 닿아 비활성화되면 닫기 버튼으로 옮긴다.
  const focusTarget = focusSelector ? els.dashboardDetailBody.querySelector(focusSelector) : null;
  if (focusTarget && !focusTarget.disabled) focusTarget.focus();
  else els.dashboardDetailClose.focus();
}

function closeIssueDetail({ restoreFocus = true } = {}) {
  if (!issueDetailContext) return;
  const context = issueDetailContext;
  const { panel, body, prefix } = detailElements(context);
  els.dashboardDetail.hidden = true;
  closeDialogFocus(els.dashboardDetail, { restoreFocus: false });
  els.dashboardDetail.removeAttribute("aria-modal");
  els.dashboardDetail.removeAttribute("role");
  panel.classList.remove("has-detail");
  body.querySelectorAll?.(`[data-${prefix}-row-issue]`).forEach((row) => { row.classList.remove("is-selected"); if (context.view === "dashboard") row.setAttribute("aria-selected", "false"); });
  const trigger = context.opener?.isConnected ? context.opener : body.querySelector(`[data-${prefix}-issue="${CSS.escape(context.selectedId)}"]`) || (context.view === "personal" ? els.workspaceContent : els.viewDashboardBtn);
  dashboardState.selectedIssueId = "";
  issueDetailContext = null;
  if (restoreFocus) trigger?.focus();
}

// 우선 확인 목록은 '이슈·분포' 탭에 있다. 팀 현황에서 지표를 누르면 그 탭으로 옮겨 목록을 보여 준다.
function showPriorityResults() {
  if (currentDashboardTab() !== "issues") selectDashboardTab("issues");
  const section = els.dashboardBody.querySelector(".dashboard-priority-section");
  section?.scrollIntoView({ behavior: "smooth", block: "start" });
  section?.focus({ preventScroll: true });
}

// 다시 그린 본문에서도 방금 누른 컨트롤에 키보드 포커스를 되돌린다.
function refocusDashboard(selector) {
  const pane = currentDashboardTab() === "issues" ? "issues" : "summary";
  els.dashboardBody.querySelector(`[data-ops-pane="${pane}"] ${selector}`)?.focus({ preventScroll: true });
}

function handleDashboardAction(target) {
  const dimension = target.closest("[data-analytics-dimension]");
  if (dimension && ["customer", "screen", "member"].includes(dimension.dataset.analyticsDimension)) {
    dashboardState.comparisonDimension = dimension.dataset.analyticsDimension;
    renderDashboard();
    els.dashboardBody.querySelector(`[data-analytics-dimension="${dashboardState.comparisonDimension}"]`)?.focus();
    return;
  }
  const source = target.closest("[data-dashboard-source]");
  if (source) return import("./chat_source_detail.js?v=20260911-6").then(({ openSourceDetail }) => openSourceDetail({ type: "chroma", issue_id: source.dataset.dashboardSource }));
  // 주간 브리핑은 내 업무 › 주간 보고 한 곳에서만 보여 준다.
  if (target.closest("[data-open-team-brief]")) {
    showPersonalView();
    return showTeamBrief();
  }
  const briefQuestion = target.closest("[data-workspace-question]");
  if (briefQuestion) return askFromDashboard(decodeURIComponent(briefQuestion.dataset.workspaceQuestion));
  const retry = target.closest("[data-dashboard-retry]");
  if (retry) return loadDashboard(true);
  const metric = target.closest("[data-dashboard-metric]");
  if (metric) {
    const key = metric.dataset.dashboardMetric;
    dashboardState.metric = key === "open" || dashboardState.metric === key ? "" : key;
    renderDashboard();
    // '미종료 이슈 전체 보기'는 필터를 풀고 전체 목록으로 넘어간다. 같은 지표를 다시 누르면 필터만 푼다.
    if (dashboardState.metric || key === "open") showPriorityResults();
    else refocusDashboard(`[data-dashboard-metric="${key}"]`);
    return;
  }
  const clear = target.closest("[data-dashboard-clear]");
  if (clear) {
    const kind = clear.dataset.dashboardClear;
    if (kind === "metric" || kind === "all") dashboardState.metric = "";
    if (kind === "customer" || kind === "all") dashboardState.customer = "";
    if (kind === "age" || kind === "all") dashboardState.age = "";
    renderDashboard();
    const remainingChip = els.dashboardFilterbar?.querySelector("[data-dashboard-clear]");
    if (remainingChip) remainingChip.focus();
    else refocusDashboard('[data-dashboard-metric="open"]');
    return;
  }
  const jump = target.closest("[data-dashboard-jump]");
  if (jump) return showPriorityResults();
  const media = target.closest("[data-intake-media]");
  if (media) {
    dashboardState.intakeMedia = media.dataset.intakeMedia;
    renderDashboard();
    refocusDashboard(`[data-intake-media="${media.dataset.intakeMedia}"]`);
    return;
  }
  const goto = target.closest("[data-dashboard-goto]");
  if (goto) return selectDashboardTab(goto.dataset.dashboardGoto, { focus: true });
  const customerDetail = target.closest("[data-customer-detail]");
  if (customerDetail) {
    const project = customerDetail.dataset.customerDetail;
    return import("./customer_detail.js?v=20261103-1").then(({ openCustomerDetail }) => openCustomerDetail(
      project,
      {
        period: dashboardState.period,
        onAsk: askFromDashboard,
        // 팀 현황의 '고객사별 신규'에서 열었으면 그 행으로, 아니면 필터 줄의 고객사 칩으로 돌아간다.
        onClose: () => (els.dashboardBody.querySelector(`[data-customer-detail="${CSS.escape(project)}"]`)
          || els.dashboardFilterbar?.querySelector("[data-customer-detail]"))?.focus(),
      },
    ));
  }
  const customer = target.closest("[data-dashboard-customer]");
  if (customer) {
    const name = customer.dataset.dashboardCustomer;
    dashboardState.customer = dashboardState.customer === name ? "" : name;
    renderDashboard();
    if (dashboardState.customer) showPriorityResults();
    else refocusDashboard(`[data-dashboard-customer="${CSS.escape(name)}"]`);
    return;
  }
  const age = target.closest("[data-dashboard-age]");
  if (age) {
    const key = age.dataset.dashboardAge;
    dashboardState.age = dashboardState.age === key ? "" : key;
    renderDashboard();
    if (dashboardState.age) showPriorityResults();
    else refocusDashboard(`[data-dashboard-age="${key}"]`);
    return;
  }
  if (target.closest("[data-dashboard-more]")) {
    dashboardState.prioritiesExpanded = !dashboardState.prioritiesExpanded;
    renderDashboard();
    refocusDashboard("[data-dashboard-more]");
    return;
  }
  const detailStep = target.closest("[data-dashboard-detail-step]");
  if (detailStep && !detailStep.disabled) {
    const label = detailStep.getAttribute("aria-label");
    return openIssueDetail(detailStep.dataset.dashboardDetailStep, { focusSelector: `[aria-label="${label}"]`, context: issueDetailContext });
  }
  const issue = target.closest("[data-dashboard-issue]");
  if (issue) return openIssueDetail(issue.dataset.dashboardIssue);
  // 표에서는 마우스로 행 어디를 눌러도 이슈 칸의 버튼을 누른 것과 같게 연다.
  const row = target.closest("[data-dashboard-row-issue]");
  if (row) return openIssueDetail(row.dataset.dashboardRowIssue);
  const question = target.closest("[data-dashboard-question]");
  if (question) return askFromDashboard(question.dataset.dashboardQuestion);
}

function initDashboard() {
  if (dashboardState.initialized || !els.dashboardPanel) return;
  dashboardState.initialized = true;
  // 분석 세 탭과 이슈 목록은 같은 본문을 공유하며 data-subview로 현재 화면을 고른다.
  for (const tab of ["overview", "comparison", "flow", "issues"]) {
    registerDashboardTab(tab, { view: () => null, onShow: () => { renderDashboard(); els.dashboardBody.scrollTop = 0; } });
  }
  els.viewDashboardBtn.addEventListener("click", () => showDashboardView());
  els.viewPersonalBtn.addEventListener("click", () => showPersonalView());
  els.viewChatBtn.addEventListener("click", () => showChatView());
  els.viewAdminBtn?.addEventListener("click", () => showAdminView());
  els.dashboardPeriod.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-dashboard-period]");
    if (!button || button.disabled) return;
    const period = Number(button.dataset.dashboardPeriod) || 30;
    if (period === dashboardState.period) return;
    dashboardState.period = period;
    syncPeriodButtons();
    loadDashboard(true);
  });
  syncPeriodButtons();
  els.dashboardRefreshBtn.addEventListener("click", () => loadDashboard(true));
  els.dashboardBody.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardBody.addEventListener("change", (event) => {
    if (!event.target.matches?.("[data-analytics-customer]")) return;
    dashboardState.customer = event.target.value;
    renderDashboard();
    els.dashboardBody.querySelector("[data-analytics-customer]")?.focus();
  });
  els.dashboardFilterbar?.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardDetail.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardDetailClose.addEventListener("click", closeIssueDetail);
  globalThis.matchMedia?.("(max-width: 1399px)").addEventListener?.("change", syncIssueDetailMode);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.dashboardDetail.hidden) closeIssueDetail();
  });
  const stored = localStorage.getItem(`${dashboardViewStorageKey}:${state.currentUser?.uid || "guest"}`);
  const initialView = ["chat", "personal", "dashboard", "admin"].includes(stored) ? stored : "chat";
  setActiveView(initialView, { remember: false, load: true });
}

export { initDashboard, loadDashboard, ensureDashboardFresh, renderDashboard, setMainViewLoader, showAdminView, showDashboardView, showPersonalView, showChatView, openWorkspaceIssueDetail, closeIssueDetail, dashboardFilterSnapshot, applyDashboardFilters, dashboardPriorityRows };
