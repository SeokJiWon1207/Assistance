import { formatUiDateTime, customerName, customerLabelText } from "./ui_format.js?v=20260911-6";
import { state, els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { closeAccountMenu, closeMobileSidebar } from "./sidebar.js?v=20260911-6";
import { ensureMyWorkspaceFresh, showTeamBrief, workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { revealOnScroll } from "./motion.js?v=20260911-6";

const dashboardViewStorageKey = "team-ai-assistant-main-view-v1";
const roleLabels = { member: "팀원", manager: "팀장", admin: "관리자" };
const metricFilterLabels = { overdue: "일정 지연", aging: "장기 미종료", highRisk: "고위험" };
const decisionLabels = { high: "위험", attention: "주의", stable: "안정", unknown: "판단 보류" };
// 장기 미종료 지표(30일 이상)와 같은 경계로 나눠 차트와 지표 카드의 숫자가 맞게 한다.
const AGING_DAYS = 30;
const AGE_BUCKETS = [
  { key: "0-7", label: "7일 이하", min: 0, max: 7, tone: "fresh" },
  { key: "8-29", label: "8–29일", min: 8, max: 29, tone: "rest" },
  { key: "30-59", label: "30–59일", min: 30, max: 59, tone: "aging" },
  { key: "60+", label: "60일 이상", min: 60, max: Infinity, tone: "overdue" },
];
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
  initialized: false,
  lastLiveAt: 0,
};
const dashboardLiveTtlMs = 60_000;
let dashboardLoadPromise = null;
let dashboardFreshPromise = null;
let dashboardLoadIsRefresh = false;
let dashboardSyncTimer = null;

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


// 2026-09-01 ~ 2026-09-30 → "9월 1일–30일", 달이 바뀌면 "7월 3일–9월 30일"
function formatPeriodRange(from, to) {
  const parse = (value) => String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  const start = parse(from);
  const end = parse(to);
  if (!start || !end) return [from, to].filter(Boolean).join(" ~ ");
  const [, startYear, startMonth, startDay] = start.map(Number);
  const [, endYear, endMonth, endDay] = end.map(Number);
  const startText = `${startYear !== endYear ? `${startYear}년 ` : ""}${startMonth}월 ${startDay}일`;
  if (startYear === endYear && startMonth === endMonth) return `${startText}–${endDay}일`;
  return `${startText}–${startYear !== endYear ? `${endYear}년 ` : ""}${endMonth}월 ${endDay}일`;
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

function emptyBlock(message, partialMessage = "표시할 항목을 찾지 못했습니다. 전체 결과는 미확정입니다.") {
  const complete = dashboardState.data?.meta?.search_complete === true;
  return `<div class="workspace-empty${complete ? "" : " is-warning"}"><span aria-hidden="true">${icon(complete ? "check" : "warning", 20)}</span><p>${escapeHtml(complete ? message : `확인한 범위 기준 · ${partialMessage}`)}</p></div>`;
}

function sectionHeader(title, meta = "") {
  return `<header><div><h3>${escapeHtml(title)}</h3></div>${meta ? `<em>${escapeHtml(meta)}</em>` : ""}</header>`;
}

// 일부 이슈만 확인한 경우 숫자 옆에 '건 이상'을 붙여 하한값이라는 것을 지표 자체에서 알린다.
function metricCard(key, label, value, helper, tone = "", delta = "", lowerBound = false) {
  // '미종료 이슈'는 걸 조건이 없으므로 지표 필터를 푸는 '전체 보기' 카드로 쓴다.
  const pressed = key === "open" ? !dashboardState.metric : dashboardState.metric === key;
  const active = pressed ? " is-active" : "";
  return `
    <button class="workspace-metric ${tone}${active}" type="button" data-dashboard-metric="${key}" aria-pressed="${pressed}">
      <span class="dashboard-metric-top"><span>${escapeHtml(label)}</span>${delta}</span>
      <strong>${formatNumber(value)}${lowerBound ? '<span class="dashboard-lower-bound">건 이상</span>' : ""}</strong>
      <small>${escapeHtml(helper)}</small>
    </button>`;
}

// closedNow는 기간 중 처리한 전체 건수가 아니라 이번 기간 신규 이슈 중 지금 종료된 건수다.
function renderIntakeCard(summary, complete = true) {
  const total = Number(summary.newIssues || 0);
  const closed = Math.min(Number(summary.closedNow || 0), total);
  const unknown = Number(summary.unknownStatus || 0);
  const open = Number(summary.newOpen ?? Math.max(0, total - closed - unknown));
  const closedShare = total ? Math.round((closed / total) * 1000) / 10 : 0;
  const openShare = total ? Math.round((open / total) * 1000) / 10 : 0;
  const unknownShare = total ? Math.round((unknown / total) * 1000) / 10 : 0;
  const rate = summary.changeRate;
  const delta = !complete ? '<em class="dashboard-delta">확인 범위 · 비교 미확정</em>' : rate == null
    ? '<em class="dashboard-delta">직전 기간 비교 없음</em>'
    : `<em class="dashboard-delta ${rate > 0 ? "is-up" : rate < 0 ? "is-down" : ""}">신규 ${rate > 0 ? "증가" : rate < 0 ? "감소" : "변동"} ${Math.abs(rate)}%</em>`;
  return `
    <div class="workspace-metric dashboard-intake" role="group" aria-label="이번 기간 신규 이슈 ${total}건${complete ? "" : " 이상 · 확인 범위"} 중 종료 ${closed}건, 미종료 ${open}건${unknown ? `, 상태 미상 ${unknown}건` : ""}">
      <span class="dashboard-metric-top"><span>이번 기간 신규 이슈</span>${delta}</span>
      <strong>${formatNumber(total)}${complete ? "" : '<span class="dashboard-lower-bound">건 이상</span>'}</strong>
      <span class="dashboard-intake-bar" aria-hidden="true"><i class="is-closed" style="width:${closedShare}%"></i><i class="is-open" style="width:${openShare}%"></i>${unknown ? `<i class="is-unknown" style="width:${unknownShare}%"></i>` : ""}</span>
      <small class="dashboard-new-change"><b class="is-closed">종료 ${formatNumber(closed)}</b> · <b class="is-open">미종료 ${formatNumber(open)}</b>${unknown ? ` · <b>상태 미상 ${formatNumber(unknown)}</b>` : ""} · ${complete ? `직전 기간 ${formatNumber(summary.previousNewIssues)}건` : "확인된 이슈만 집계"}</small>
    </div>`;
}

function renderMetrics(summary, complete = true) {
  return `
    <section class="workspace-summary-grid dashboard-metrics" aria-label="핵심 운영 지표">
      ${metricCard("open", complete ? "미종료 이슈" : "확인된 미종료", summary.open, `${complete ? "전체 보기" : "전체 미확정"} · 이월 ${formatNumber(summary.carriedOpen)}건`, "is-blue", "", !complete)}
      ${metricCard("overdue", "일정 지연", summary.overdue, "완료 예정일 경과", "is-danger", "", !complete)}
      ${metricCard("aging", "장기 미종료", summary.aging, "30일 이상 미종료", "is-warning", "", !complete)}
      ${metricCard("highRisk", "고위험", summary.highRisk, `우선순위 높음 ${formatNumber(summary.highPriority)}건`, "is-danger", "", !complete)}
      ${renderIntakeCard(summary, complete)}
    </section>`;
}

// 결론을 지표 바로 아래 한 줄로 둔다. 근거는 칩, 다음 행동은 AI 분석 버튼으로 줄인다.
function renderDecision(decision, periodDays) {
  const level = decisionLabels[decision.riskLevel] ? decision.riskLevel : "unknown";
  const signals = (decision.signals || []).map((item) => `<li>${escapeHtml(customerLabelText(item))}</li>`).join("");
  const actions = (decision.recommendedActions || []).map((item) => `
    <button type="button" class="dashboard-decision-action" data-dashboard-question="${escapeHtml(item.question)}" title="${escapeHtml(customerLabelText(item.reason))}">${escapeHtml(customerLabelText(item.title))} →<span class="sr-only"> AI에게 분석 요청 · ${escapeHtml(customerLabelText(item.reason))}</span></button>`).join("");
  return `
    <section class="dashboard-decision is-${level}" aria-label="${periodDays}일 운영 판단">
      <span class="dashboard-decision-badge">${decisionLabels[level]}</span>
      <div class="dashboard-decision-main">
        <span class="dashboard-decision-period">${periodDays}일 운영 판단</span>
        <h3>${escapeHtml(decision.headline || "운영 신호를 확인했습니다.")}</h3>
        ${signals ? `<ul>${signals}</ul>` : ""}
      </div>
      ${actions ? `<div class="dashboard-decision-actions">${actions}</div>` : ""}
    </section>`;
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
  return `<div class="dashboard-chart-legend" aria-hidden="true">${Object.entries(SEGMENT_LABELS)
    .map(([key, label]) => `<span><i class="is-${key}"></i>${label}</span>`).join("")}</div>`;
}

function spikeSummary(item, customer = false) {
  return `${customer ? customerName(item.name) : item.name} ${formatNumber(item.count)}건(직전 ${formatNumber(item.previousCount)}건)`;
}

function renderCustomerChart(openIssues, health, customerSpikes, screenSpikes) {
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
  // 막대에 배지로 붙지 못한 급증 신호도 사라지지 않게 한 줄로 남긴다.
  const shown = new Set(rows.map(([name]) => name));
  const otherSpikes = [
    ...customerSpikes.filter((item) => !shown.has(item.name)).map((item) => `고객사 ${spikeSummary(item, true)}`),
    ...screenSpikes.map((item) => `화면 ${spikeSummary(item)}`),
  ];
  const hidden = groups.size - rows.length;
  return `
    <section class="workspace-section dashboard-chart" aria-label="고객사별 미종료">
      ${sectionHeader("고객사별 미종료", "막대를 누르면 목록이 걸러집니다")}
      <div class="dashboard-chart-body">
        ${rows.length ? `<div class="dashboard-bars">${bars}</div>` : emptyBlock("미종료 이슈가 없습니다.")}
        ${hidden > 0 ? `<p class="dashboard-chart-note">그 외 고객사 ${hidden}곳은 우선 확인 이슈 목록에서 볼 수 있습니다.</p>` : ""}
        ${otherSpikes.length ? `<p class="dashboard-chart-note"><b>급증 신호</b> ${escapeHtml(otherSpikes.join(" · "))}</p>` : ""}
      </div>
    </section>`;
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
    <section class="workspace-section dashboard-chart" aria-label="미종료 경과일">
      ${sectionHeader("미종료 경과일", "등록 후 지난 일수")}
      <div class="dashboard-chart-body">
        ${openIssues.length ? `<div class="dashboard-columns">${columns}</div>
        <p class="dashboard-chart-note">30일 이상 ${observedCount(aging)}이 장기 미종료입니다.${unknown ? ` 등록일을 확인하지 못한 ${formatNumber(unknown)}건은 제외했습니다.` : ""}</p>` : emptyBlock("미종료 이슈가 없습니다.")}
      </div>
    </section>`;
}

function renderMemberChart(members) {
  if (!members.length) return "";
  const rows = members.map((member) => {
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
    <section class="workspace-section dashboard-chart" aria-label="팀원별 업무 부하">
      ${sectionHeader("팀원별 부하", `미종료 기준 · ${members.length}명`)}
      <div class="dashboard-chart-body"><div class="dashboard-bars">${bars}</div></div>
    </section>`;
}

function filteredPriorities(priorities) {
  return priorities.filter((item) => {
    if (dashboardState.customer && item.project !== dashboardState.customer) return false;
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
    chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="customer" aria-label="고객사 ${escapeHtml(customerName(dashboardState.customer))} 필터 해제">고객사: ${escapeHtml(customerName(dashboardState.customer))}${icon("close", 14)}</button>`);
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
    : `<span class="dashboard-filterbar-label">적용 필터</span>
      <span class="dashboard-filterbar-hint">없음 · 지표 카드나 차트 막대를 누르면 '우선 확인 이슈'가 바로 걸러집니다.</span>`;
}

function hideFilterbar() {
  if (els.dashboardFilterbar) els.dashboardFilterbar.hidden = true;
}

function renderPriorities(priorities, filtered) {
  const filtering = Boolean(dashboardState.metric || dashboardState.customer || dashboardState.age);
  const shown = dashboardState.prioritiesExpanded ? filtered : filtered.slice(0, PRIORITY_PREVIEW_LIMIT);
  const remaining = filtered.length - shown.length;
  const toggle = remaining > 0
    ? `<button type="button" class="dashboard-more" data-dashboard-more aria-expanded="false">나머지 ${formatNumber(remaining)}건 보기</button>`
    : (dashboardState.prioritiesExpanded && filtered.length > PRIORITY_PREVIEW_LIMIT
      ? `<button type="button" class="dashboard-more" data-dashboard-more aria-expanded="true">상위 ${PRIORITY_PREVIEW_LIMIT}건만 보기</button>` : "");
  const rows = shown.map((issue) => `
    <tr class="dashboard-clickable${String(issue.id) === dashboardState.selectedIssueId ? " is-selected" : ""}" data-dashboard-row-issue="${escapeHtml(issue.id)}" aria-selected="${String(issue.id) === dashboardState.selectedIssueId}">
      <td><button type="button" class="dashboard-issue-button" data-dashboard-issue="${escapeHtml(issue.id)}"><strong class="dashboard-issue-title">#${escapeHtml(issue.id)} ${escapeHtml(issue.title)}</strong></button><small>${escapeHtml(workspaceStatusLabel(issue.status))} · ${escapeHtml((issue.riskReasons || []).join(" · "))}</small></td>
      <td>${escapeHtml(customerName(issue, "-"))}</td><td>${escapeHtml(issue.handler || "미지정")}</td>
      <td>${issue.ageDays == null ? "-" : `${formatNumber(issue.ageDays)}일`}</td><td><span class="dashboard-risk-badge is-${escapeHtml(issue.riskLevel)}">${riskLabel(issue.riskLevel)}</span></td>
    </tr>`).join("");
  return `
    <section class="workspace-section dashboard-priority-section${filtering ? " is-filtered" : ""}" tabindex="-1" aria-label="우선 확인 이슈">
      ${sectionHeader("우선 확인 이슈", `${filtered.length} / ${priorities.length}건`)}
      ${filtered.length ? `<div class="workspace-table-wrap"><table class="workspace-table dashboard-priority-table"><thead><tr><th>이슈</th><th>고객사</th><th>담당자</th><th>경과</th><th>위험</th></tr></thead><tbody>${rows}</tbody></table></div>${toggle}` : emptyBlock("현재 필터에 해당하는 우선 이슈가 없습니다.")}
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
    els.dashboardSubtitle.textContent = "운영 데이터를 조회하고 있습니다.";
    hideFilterbar();
    dashboardContentShown = false;
    // 숫자 대신 실제 화면과 같은 자리·크기의 뼈대를 둬 다 불러왔을 때 화면이 튀지 않게 한다.
    els.dashboardBody.innerHTML = `
      <div class="dashboard-loading" role="status">
        <p class="dashboard-loading-status"><span class="dashboard-spinner" aria-hidden="true"></span><span><strong>운영 데이터를 불러오고 있습니다.</strong><small>고객사 건강도와 위험 신호를 같은 기준으로 계산합니다.</small></span></p>
        <div class="dashboard-skel" aria-hidden="true"><i class="is-banner"></i><i class="is-strip"></i><span><i></i><i></i><i></i></span><i class="is-table"></i></div>
      </div>`;
    return;
  }
  if (dashboardState.error && !dashboardState.data) {
    hideFilterbar();
    dashboardContentShown = false;
    els.dashboardBody.innerHTML = `<div class="dashboard-state is-error" role="alert"><strong>대시보드를 불러오지 못했습니다.</strong><p>${escapeHtml(dashboardState.error)}</p><button class="dashboard-ai-button is-secondary" type="button" data-dashboard-retry>다시 조회</button></div>`;
    updateSourceMeta({ source: "error" });
    return;
  }
  const data = dashboardState.data;
  if (!data) return;
  updateSourceMeta(data.meta, { loading, liveFailed: Boolean(dashboardState.error) });
  if (data.meta?.source === "error") {
    hideFilterbar();
    dashboardContentShown = false;
    els.dashboardBody.innerHTML = `<div class="dashboard-state is-error" role="alert"><strong>업무 데이터를 확인하지 못했습니다.</strong><p>${escapeHtml(data.meta.sourceReason || "잠시 후 다시 조회해 주세요.")}</p><button class="dashboard-ai-button is-secondary" type="button" data-dashboard-retry>다시 조회</button></div>`;
    return;
  }
  const complete = data.meta.search_complete === true;
  // 범위 · 기간 · 비교 기준을 제목 아래 한 줄에 모은다. 본문에서 같은 말을 반복하지 않는다.
  // '일부만 확인'은 데이터 기준 배지와 본문 안내 한 줄이 알려 주므로 제목 아래에서는 반복하지 않는다.
  const scopeLabel = data.meta.scope === "team" ? "팀 전체" : "내 담당 업무";
  const periodDays = data.meta.periodDays || dashboardState.period;
  els.dashboardSubtitle.textContent = `${scopeLabel} · ${formatPeriodRange(data.meta.periodFrom, data.meta.periodTo)} · 직전 ${periodDays}일과 비교`;
  const priorities = data.openIssues || data.priorities || [];
  const filtered = filteredPriorities(priorities);
  renderFilterbar(filtered.length);
  // 결론(판단) → 지표 → 분포(차트) → 개별 이슈(목록) 순으로 둔다. 차트·지표를 누르면 목록으로 이동해 걸러진 결과를 보여 준다.
  // 지연·장기·그 외 범례는 차트마다 반복하지 않고 분포 제목 옆에 한 번만 둔다.
  const memberChart = renderMemberChart(data.members || []);
  els.dashboardBody.innerHTML = `
    ${dashboardState.error
        ? `<div class="dashboard-refresh-status is-error" role="status"><strong>${escapeHtml(dashboardState.error)} 저장된 데이터를 계속 표시합니다.</strong><span>${escapeHtml(formatDateTime(data.meta.fetchedAt))} 기준</span><button type="button" class="secondary-button" data-dashboard-retry>다시 시도</button></div>`
        : ""}
    ${!complete ? '<div class="dashboard-refresh-status is-warning" role="status"><strong>일부 이슈만 확인했어요.</strong><span>건수는 확인한 범위 기준이라 실제보다 적을 수 있어요.</span></div>' : ""}
    ${renderDecision(data.decisionSummary || {}, data.meta.periodDays)}
    ${renderMetrics(data.summary, complete)}
    <div class="dashboard-dist-head"><h3>미종료 분포</h3>${chartLegend()}</div>
    <div class="dashboard-grid${memberChart ? "" : " is-two"}">${renderCustomerChart(priorities, data.customerHealth || [], data.customerSpikes || [], data.screenSpikes || [])}${renderAgeChart(priorities)}${memberChart}</div>
    ${renderPriorities(priorities, filtered)}
    <div class="dashboard-brief-link"><span>지난주 팀 브리핑은 <b>내 업무 › 주간 보고</b>에서 볼 수 있어요.</span><button type="button" class="secondary-button" data-open-team-brief>팀 브리핑 열기 ›</button></div>`;
  if (!dashboardContentShown) stopDashboardReveal = revealOnScroll(els.dashboardBody, DASHBOARD_REVEAL_SELECTOR);
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
      if (!response.ok) throw new Error(payload.detail || "운영 데이터를 불러오지 못했습니다.");
      dashboardState.data = payload;
      dashboardState.lastLiveAt = Date.parse(payload.meta?.fetchedAt || "") || 0;
      if (dashboardSyncTimer) clearTimeout(dashboardSyncTimer);
      dashboardSyncTimer = payload.meta?.refreshPending ? setTimeout(() => {
        dashboardSyncTimer = null;
        if (state.currentUser?.uid) void loadDashboard(false, true, true);
      }, 10000) : null;
    } catch (error) {
      dashboardState.error = error.message || "운영 데이터를 불러오지 못했습니다.";
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
  const meta = dashboardState.data?.meta;
  const sourceCanBeReused = ["live", "snapshot"].includes(meta?.source);
  const expiresAt = Date.parse(meta?.cacheUntil || "") || dashboardState.lastLiveAt + dashboardLiveTtlMs;
  const liveDataIsRecent = sourceCanBeReused && dashboardState.lastLiveAt > 0
    && Date.now() >= dashboardState.lastLiveAt && Date.now() < expiresAt && !meta?.refreshPending;
  if (!dashboardLoadPromise && liveDataIsRecent) {
    renderDashboard();
    return dashboardState.data;
  }
  dashboardFreshPromise = Promise.resolve().then(async () => {
    if (dashboardLoadPromise) await dashboardLoadPromise;
    else await loadDashboard(false, true);
    return dashboardState.data;
  });
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
function detailNavigation(id) {
  const list = filteredPriorities(dashboardState.data?.openIssues || dashboardState.data?.priorities || []);
  const index = list.findIndex((item) => String(item.id) === String(id));
  if (index < 0 || list.length < 2) return "";
  const step = (offset, label, iconName) => `<button type="button" class="dashboard-detail-step" data-dashboard-detail-step="${escapeHtml(String(list[index + offset]?.id ?? ""))}" aria-label="${label}"${list[index + offset] ? "" : " disabled"}>${icon(iconName, 16)}</button>`;
  return `<div class="dashboard-detail-nav"><span>우선 확인 이슈 ${index + 1} / ${list.length}</span>${step(-1, "이전 이슈", "chevron-left")}${step(1, "다음 이슈", "chevron-right")}</div>`;
}

function openIssueDetail(id, { focusSelector = "" } = {}) {
  const issue = issueById(id);
  if (!issue) return;
  dashboardState.selectedIssueId = String(id);
  els.dashboardDetailTitle.textContent = `#${issue.id} 이슈 상세`;
  els.dashboardDetailBody.innerHTML = `
    ${detailNavigation(issue.id)}
    <p class="dashboard-detail-summary">${escapeHtml(issue.title)}</p>
    <div class="dashboard-detail-meta">
      <div><span>고객사</span><strong>${escapeHtml(customerName(issue, "-"))}</strong></div>
      <div><span>담당자</span><strong>${escapeHtml(issue.handler || "미지정")}</strong></div>
      <div><span>상태 / 우선순위</span><strong>${escapeHtml(workspaceStatusLabel(issue.status))} · ${escapeHtml(priorityLabel(issue.priority))}</strong></div>
      <div><span>화면번호</span><strong>${escapeHtml(issue.screenNumber || "-")}</strong></div>
      <div><span>등록일</span><strong>${escapeHtml(issue.createdAt || "-")}</strong></div>
      <div><span>완료 예정일</span><strong>${escapeHtml(issue.dueDate || "-")}</strong></div>
    </div>
    <div class="dashboard-detail-reason"><strong>${riskLabel(issue.riskLevel)} 판단 근거</strong><p>${escapeHtml((issue.riskReasons || []).join(" · ") || "우선 확인 대상으로 분류")}</p></div>
    <div class="dashboard-detail-reason"><strong>권장 다음 행동</strong><p>${escapeHtml(issue.recommendedAction || "현재 상태와 다음 처리 단계를 확인하세요.")}</p></div>
    <div class="dashboard-detail-actions"><button type="button" class="secondary-button" data-dashboard-source="${escapeHtml(issue.id)}">저장된 원문 보기</button><button type="button" class="secondary-button" data-dashboard-question="#${escapeHtml(issue.id)} 이슈와 유사한 과거 사례의 원인과 해결 방법을 찾아줘">유사 이슈 찾기</button></div>
    <button class="dashboard-ai-button" type="button" data-dashboard-question="#${escapeHtml(issue.id)} 이슈의 현재 위험, 막힌 점과 다음 행동을 정리해줘">AI에게 이 이슈 질문하기</button>`;
  els.dashboardDetail.hidden = false;
  els.dashboardPanel.classList.add?.("has-detail");
  els.dashboardBody.querySelectorAll?.("[data-dashboard-row-issue]").forEach((row) => {
    const selected = row.dataset.dashboardRowIssue === String(id);
    row.classList.toggle("is-selected", selected);
    row.setAttribute("aria-selected", String(selected));
  });
  els.dashboardBody.querySelector(`[data-dashboard-row-issue="${CSS.escape(String(id))}"]`)?.scrollIntoView?.({ block: "nearest" });
  if (globalThis.matchMedia?.("(max-width: 1399px)").matches) {
    import("./dialog_focus.js?v=20260911-6").then(({ openDialogFocus }) => {
      if (els.dashboardDetail.hidden) return;
      els.dashboardDetail.setAttribute("role", "dialog");
      els.dashboardDetail.setAttribute("aria-modal", "true");
      openDialogFocus(els.dashboardDetail, { initialFocus: els.dashboardDetailClose, onClose: closeIssueDetail });
    });
  }
  // 이전/다음으로 넘길 때는 누른 버튼에 포커스를 두고, 끝에 닿아 비활성화되면 닫기 버튼으로 옮긴다.
  const focusTarget = focusSelector ? els.dashboardDetailBody.querySelector(focusSelector) : null;
  if (focusTarget && !focusTarget.disabled) focusTarget.focus();
  else els.dashboardDetailClose.focus();
}

function closeIssueDetail() {
  els.dashboardDetail.hidden = true;
  if (els.dashboardDetail.getAttribute?.("aria-modal") === "true") {
    import("./dialog_focus.js?v=20260911-6").then(({ closeDialogFocus }) => closeDialogFocus(els.dashboardDetail));
    els.dashboardDetail.removeAttribute("aria-modal");
    els.dashboardDetail.removeAttribute("role");
  }
  els.dashboardPanel.classList.remove("has-detail");
  els.dashboardBody.querySelectorAll?.("[data-dashboard-row-issue]").forEach((row) => { row.classList.remove("is-selected"); row.setAttribute("aria-selected", "false"); });
  const trigger = els.dashboardBody.querySelector(`[data-dashboard-issue="${CSS.escape(dashboardState.selectedIssueId)}"]`);
  dashboardState.selectedIssueId = "";
  trigger?.focus();
}

function showPriorityResults() {
  const section = els.dashboardBody.querySelector(".dashboard-priority-section");
  section?.scrollIntoView({ behavior: "smooth", block: "start" });
  section?.focus({ preventScroll: true });
}

// 다시 그린 본문에서도 방금 누른 컨트롤에 키보드 포커스를 되돌린다.
function refocusDashboard(selector) {
  els.dashboardBody.querySelector(selector)?.focus({ preventScroll: true });
}

function handleDashboardAction(target) {
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
    if (dashboardState.metric) showPriorityResults();
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
    return openIssueDetail(detailStep.dataset.dashboardDetailStep, { focusSelector: `[aria-label="${label}"]` });
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
    loadDashboard(false, true);
  });
  syncPeriodButtons();
  els.dashboardRefreshBtn.addEventListener("click", () => loadDashboard(true));
  els.dashboardBody.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardFilterbar?.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardDetail.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardDetailClose.addEventListener("click", closeIssueDetail);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.dashboardDetail.hidden) closeIssueDetail();
  });
  const stored = localStorage.getItem(`${dashboardViewStorageKey}:${state.currentUser?.uid || "guest"}`);
  const initialView = ["chat", "personal", "dashboard", "admin"].includes(stored) ? stored : "chat";
  setActiveView(initialView, { remember: false, load: true });
}

export { initDashboard, loadDashboard, ensureDashboardFresh, renderDashboard, setMainViewLoader, showAdminView, showDashboardView, showPersonalView, showChatView };
