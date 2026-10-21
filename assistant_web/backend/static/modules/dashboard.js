import { formatUiDateTime } from "./ui_format.js?v=20260911-6";
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

const dashboardState = {
  data: null,
  loading: false,
  error: "",
  period: 30,
  metric: "",
  customer: "",
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

function emptyBlock(message) {
  const complete = dashboardState.data?.meta?.search_complete === true;
  return `<div class="workspace-empty${complete ? "" : " is-warning"}"><span aria-hidden="true">${icon(complete ? "check" : "warning", 20)}</span><p>${escapeHtml(complete ? message : `확인한 범위 기준 · ${message}`)}</p></div>`;
}

function sectionHeader(title, meta = "") {
  return `<header><div><h3>${escapeHtml(title)}</h3></div>${meta ? `<em>${escapeHtml(meta)}</em>` : ""}</header>`;
}

function metricCard(key, label, value, helper, tone = "", delta = "") {
  // '미종료 이슈'는 걸 조건이 없으므로 지표 필터를 푸는 '전체 보기' 카드로 쓴다.
  const pressed = key === "open" ? !dashboardState.metric : dashboardState.metric === key;
  const active = pressed ? " is-active" : "";
  return `
    <button class="workspace-metric ${tone}${active}" type="button" data-dashboard-metric="${key}" aria-pressed="${pressed}">
      <span class="dashboard-metric-top"><span>${escapeHtml(label)}</span>${delta}</span>
      <strong>${formatNumber(value)}</strong>
      <small>${escapeHtml(helper)}</small>
    </button>`;
}

function renderMetrics(summary, complete = true) {
  const rate = summary.changeRate;
  const delta = rate == null
    ? '<em class="dashboard-delta">신규 증감 비교 기준 없음</em>'
    : `<em class="dashboard-delta ${rate > 0 ? "is-up" : rate < 0 ? "is-down" : ""}">신규 ${rate > 0 ? "증가" : rate < 0 ? "감소" : "변동"} ${Math.abs(rate)}%</em>`;
  return `
    <section class="workspace-summary-grid dashboard-metrics" aria-label="핵심 운영 지표">
      ${metricCard("open", complete ? "미종료 이슈" : "확인된 미종료", summary.open, `${complete ? "전체 보기" : "전체 미확정"} · 이월 ${formatNumber(summary.carriedOpen)}건`, "is-blue")}
      ${metricCard("overdue", "일정 지연", summary.overdue, "완료 예정일 경과", "is-danger")}
      ${metricCard("aging", "장기 미종료", summary.aging, "30일 이상 미종료", "is-warning")}
      ${metricCard("highRisk", "고위험", summary.highRisk, `우선순위 높음 ${formatNumber(summary.highPriority)}건`, "is-danger")}
    </section><p class="dashboard-new-change">${delta} · 직전 동일 기간 대비 신규 이슈 기준</p>`;
}

function renderDecision(decision, periodDays) {
  const signals = (decision.signals || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
  const actions = (decision.recommendedActions || []).map((item) => `
    <button type="button" data-dashboard-question="${escapeHtml(item.question)}">
      <strong>${escapeHtml(item.title)}</strong>
      <span>${escapeHtml(item.reason)}</span>
      <em>AI에게 분석 요청 →</em>
    </button>`).join("");
  return `
    <section class="workspace-decision dashboard-decision ${decision.riskLevel === "high" ? "is-high" : ""}">
      <header><span>${periodDays}일 운영 판단</span><em>${riskLabel(decision.riskLevel)}</em></header>
      <h3>${escapeHtml(decision.headline || "운영 신호를 확인했습니다.")}</h3>
      <ul>${signals}</ul>
      ${actions ? `<div class="workspace-decision-actions">${actions}</div>` : ""}
    </section>`;
}

function renderTodo(issue) {
  const classes = ["workspace-item", "dashboard-clickable", "dashboard-card-link"];
  if (issue.overdue) classes.push("is-overdue");
  else if (Number(issue.ageDays || 0) >= 30) classes.push("is-aging");
  return `
    <article class="${classes.join(" ")}">
      <div class="workspace-item-main">
        <button type="button" class="workspace-item-title dashboard-card-button" data-dashboard-issue="${escapeHtml(issue.id)}"><span>#${escapeHtml(issue.id)}</span><strong>${escapeHtml(issue.title)}</strong><span class="sr-only"> 상세 보기</span></button>
        <div class="workspace-item-meta">
          <span class="workspace-meta-project">${escapeHtml(issue.project)}</span>
          <span>${escapeHtml(issue.handler || "담당자 미지정")}</span>
          ${issue.overdue ? '<span class="workspace-meta-due is-overdue">일정 지연</span>' : ""}
        </div>
        <p class="workspace-item-recommendation"><strong>다음 행동</strong>${escapeHtml(issue.recommendedAction)}</p>
      </div>
      <span class="dashboard-risk-dot is-${escapeHtml(issue.riskLevel)}" aria-label="${riskLabel(issue.riskLevel)}"></span>
    </article>`;
}

function renderTodos(todos) {
  return `
    <section class="workspace-section">
      ${sectionHeader("오늘 확인할 업무", `${todos.length}건`)}
      <div class="dashboard-section-list">${todos.length ? todos.map(renderTodo).join("") : emptyBlock("지금 바로 확인할 업무가 없습니다.")}</div>
    </section>`;
}

function renderHealthRow(item) {
  const reason = (item.reasons || []).join(" · ") || "특별한 위험 신호 없음";
  const selected = dashboardState.customer === item.customer;
  return `
    <button type="button" class="health-row dashboard-clickable${selected ? " is-selected" : ""}" aria-pressed="${selected}" data-dashboard-customer="${escapeHtml(item.customer)}">
      <span class="health-status is-${escapeHtml(item.level)}" aria-hidden="true"></span>
      <span class="health-main">
        <span class="health-title"><strong>${escapeHtml(item.customer)}</strong><span class="health-label is-${escapeHtml(item.level)}">${escapeHtml(item.label)}</span></span>
        <span class="health-reason" title="${escapeHtml(reason)}">${escapeHtml(reason)}</span>
      </span>
      <span class="health-metrics"><strong>미종료 ${formatNumber(item.openCount)}</strong><span>지연 ${formatNumber(item.overdueCount)} · 장기 ${formatNumber(item.agingCount)}</span></span>
    </button>`;
}

function renderCustomerHealth(items, summary) {
  const meta = `위험 ${formatNumber(summary.high)} · 주의 ${formatNumber(summary.attention)}`;
  return `
    <section class="workspace-section">
      ${sectionHeader("고객사 건강도", meta)}
      <div class="dashboard-section-list">${items.length ? items.map(renderHealthRow).join("") : emptyBlock("분석할 고객사 데이터가 없습니다.")}</div>
    </section>`;
}

function renderSpikeRows(items, type) {
  if (!items.length) return `<div class="workspace-empty"><p>${dashboardState.data?.meta?.search_complete === true ? "급증 신호 없음" : "확인한 범위의 급증 신호 없음"}</p></div>`;
  return items.map((item) => {
    const ratio = item.ratio == null ? "이전 0건" : `${item.ratio}배`;
    const selected = type === "customer" && dashboardState.customer === item.name;
    const tag = type === "customer" ? "button" : "article";
    const attribute = type === "customer" ? `type="button" data-dashboard-customer="${escapeHtml(item.name)}" aria-pressed="${selected}"` : "";
    return `<${tag} class="dashboard-spike-row ${type === "customer" ? "dashboard-clickable" : ""}${selected ? " is-selected" : ""}" ${attribute}>
      <strong>${escapeHtml(type === "screen" ? `화면 ${item.name}` : item.name)}</strong>
      <em>${formatNumber(item.count)}건</em>
      <small>직전 ${formatNumber(item.previousCount)}건 · ${escapeHtml(ratio)}</small>
    </${tag}>`;
  }).join("");
}

function renderSpikes(customerSpikes, screenSpikes) {
  return `
    <section class="workspace-section">
      ${sectionHeader("급증 신호", `${customerSpikes.length + screenSpikes.length}개`)}
      <div class="dashboard-spike-group"><span class="dashboard-spike-label">고객사</span>${renderSpikeRows(customerSpikes, "customer")}</div>
      <div class="dashboard-spike-group"><span class="dashboard-spike-label">화면번호</span>${renderSpikeRows(screenSpikes, "screen")}</div>
    </section>`;
}

function renderMembers(members) {
  if (!members.length) return "";
  const rows = members.map((member) => `
    <tr><td><strong>${escapeHtml(member.displayName)}</strong><small>${escapeHtml(roleLabels[member.role] || member.role)}${member.mantisUsername ? ` · ${escapeHtml(member.mantisUsername)}` : ""}</small></td>
    <td>${formatNumber(member.open)}</td><td class="${member.overdue ? "danger-text" : ""}">${formatNumber(member.overdue)}</td><td>${formatNumber(member.aging)}</td><td class="${member.highRisk ? "danger-text" : ""}">${formatNumber(member.highRisk)}</td></tr>`).join("");
  return `
    <section class="workspace-section">
      ${sectionHeader("팀원별 업무 부하", `${members.length}명`)}
      <div class="workspace-table-wrap"><table class="workspace-table dashboard-member-table"><thead><tr><th>팀원</th><th>미종료</th><th>지연</th><th>장기</th><th>고위험</th></tr></thead><tbody>${rows}</tbody></table></div>
    </section>`;
}

function filteredPriorities(priorities) {
  return priorities.filter((item) => {
    if (dashboardState.customer && item.project !== dashboardState.customer) return false;
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
    chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="customer" aria-label="고객사 ${escapeHtml(dashboardState.customer)} 필터 해제">고객사: ${escapeHtml(dashboardState.customer)}${icon("close", 14)}</button>`);
  }
  els.dashboardFilterbar.hidden = false;
  els.dashboardFilterbar.innerHTML = chips.length
    ? `<span class="dashboard-filterbar-label">적용 필터</span>${chips.join("")}
      <button class="dashboard-chip dashboard-chip-clear" type="button" data-dashboard-clear="all">전체 해제</button>
      <button class="dashboard-filter-jump" type="button" data-dashboard-jump>우선 확인 이슈 ${formatNumber(filteredCount)}건 보기${icon("arrow-up", 14, "is-bold")}</button>`
    : `<span class="dashboard-filterbar-label">적용 필터</span>
      <span class="dashboard-filterbar-hint">없음 · 지표 카드나 고객사를 누르면 '우선 확인 이슈'가 바로 걸러집니다.</span>`;
}

function hideFilterbar() {
  if (els.dashboardFilterbar) els.dashboardFilterbar.hidden = true;
}

function renderPriorities(priorities, filtered) {
  const filtering = Boolean(dashboardState.metric || dashboardState.customer);
  const rows = filtered.map((issue) => `
    <tr class="dashboard-clickable" data-dashboard-row-issue="${escapeHtml(issue.id)}">
      <td><span class="dashboard-risk-badge is-${escapeHtml(issue.riskLevel)}">${riskLabel(issue.riskLevel)}</span></td>
      <td><button type="button" class="dashboard-issue-button" data-dashboard-issue="${escapeHtml(issue.id)}"><strong class="dashboard-issue-title">#${escapeHtml(issue.id)} ${escapeHtml(issue.title)}</strong></button><small>${escapeHtml((issue.riskReasons || []).join(" · "))}</small></td>
      <td>${escapeHtml(issue.project || "-")}</td><td>${escapeHtml(issue.handler || "미지정")}</td>
      <td>${issue.ageDays == null ? "-" : `${formatNumber(issue.ageDays)}일`}</td><td>${escapeHtml(workspaceStatusLabel(issue.status))}${issue.overdue ? ' · <span class="danger-text">지연</span>' : ""}</td>
    </tr>`).join("");
  return `
    <section class="workspace-section dashboard-priority-section${filtering ? " is-filtered" : ""}" tabindex="-1" aria-label="우선 확인 이슈">
      ${sectionHeader("우선 확인 이슈", `${filtered.length} / ${priorities.length}건`)}
      ${filtered.length ? `<div class="workspace-table-wrap"><table class="workspace-table dashboard-priority-table"><thead><tr><th>위험도</th><th>이슈</th><th>고객사</th><th>담당자</th><th>경과</th><th>상태</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyBlock("현재 필터에 해당하는 우선 이슈가 없습니다.")}
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
  if (loading) {
    updateSourceMeta(dashboardState.data?.meta, { loading: true });
    els.dashboardSubtitle.textContent = "운영 데이터를 조회하고 있습니다.";
    hideFilterbar();
    dashboardContentShown = false;
    els.dashboardBody.innerHTML = '<div class="dashboard-state" role="status"><span class="dashboard-spinner" aria-hidden="true"></span><strong>운영 데이터를 불러오고 있습니다.</strong><p>고객사 건강도와 위험 신호를 같은 기준으로 계산합니다.</p></div>';
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
  updateSourceMeta(data.meta, { liveFailed: Boolean(dashboardState.error) });
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
  // 지표 카드와 고객사로 거른 결과가 누른 자리 바로 아래에서 바뀌도록 우선 확인 이슈를 지표 다음에 둔다.
  els.dashboardBody.innerHTML = `
    ${dashboardState.error
        ? `<div class="dashboard-refresh-status is-error" role="status"><strong>${escapeHtml(dashboardState.error)} 저장된 데이터를 계속 표시합니다.</strong></div>`
        : ""}
    ${!complete ? '<div class="dashboard-refresh-status is-warning" role="status"><strong>일부 이슈만 확인했어요.</strong><span>건수는 확인한 범위 기준이라 실제보다 적을 수 있어요.</span></div>' : ""}
    ${renderMetrics(data.summary, complete)}
    ${renderPriorities(priorities, filtered)}
    ${renderDecision(data.decisionSummary, data.meta.periodDays)}
    <div class="dashboard-grid">${renderTodos(data.todos || [])}${renderCustomerHealth(data.customerHealth || [], data.customerHealthSummary || {})}${renderSpikes(data.customerSpikes || [], data.screenSpikes || [])}</div>
    ${renderMembers(data.members || [])}
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
    ensureMyWorkspaceFresh().catch((error) => {
      if (!state.myWorkspace) {
        els.workspaceContent.innerHTML = `<div class="workspace-error"><strong>업무 정보를 불러오지 못했습니다.</strong><p>${escapeHtml(error.message)}</p></div>`;
      }
    });
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

function openIssueDetail(id) {
  const issue = issueById(id);
  if (!issue) return;
  dashboardState.selectedIssueId = String(id);
  els.dashboardDetailTitle.textContent = `#${issue.id} 이슈 상세`;
  els.dashboardDetailBody.innerHTML = `
    <p class="dashboard-detail-summary">${escapeHtml(issue.title)}</p>
    <div class="dashboard-detail-meta">
      <div><span>고객사</span><strong>${escapeHtml(issue.project || "-")}</strong></div>
      <div><span>담당자</span><strong>${escapeHtml(issue.handler || "미지정")}</strong></div>
      <div><span>상태 / 우선순위</span><strong>${escapeHtml(workspaceStatusLabel(issue.status))} · ${escapeHtml(priorityLabel(issue.priority))}</strong></div>
      <div><span>화면번호</span><strong>${escapeHtml(issue.screenNumber || "-")}</strong></div>
      <div><span>등록일</span><strong>${escapeHtml(issue.createdAt || "-")}</strong></div>
      <div><span>완료 예정일</span><strong>${escapeHtml(issue.dueDate || "-")}</strong></div>
    </div>
    <div class="dashboard-detail-reason"><strong>${riskLabel(issue.riskLevel)} 판단 근거</strong><p>${escapeHtml((issue.riskReasons || []).join(" · ") || "우선 확인 대상으로 분류")}</p></div>
    <div class="dashboard-detail-reason"><strong>권장 다음 행동</strong><p>${escapeHtml(issue.recommendedAction || "현재 상태와 다음 처리 단계를 확인하세요.")}</p></div>
    <button class="dashboard-ai-button" type="button" data-dashboard-question="#${escapeHtml(issue.id)} 이슈의 현재 위험, 막힌 점과 다음 행동을 정리해줘">AI에게 이 이슈 질문하기</button>`;
  els.dashboardDetail.hidden = false;
  els.dashboardDetailClose.focus();
}

function closeIssueDetail() {
  els.dashboardDetail.hidden = true;
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
    refocusDashboard(`[data-dashboard-metric="${key}"]`);
    return;
  }
  const clear = target.closest("[data-dashboard-clear]");
  if (clear) {
    if (clear.dataset.dashboardClear !== "customer") dashboardState.metric = "";
    if (clear.dataset.dashboardClear !== "metric") dashboardState.customer = "";
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
