import { state, els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js";
import { responseJson } from "./api.js";
import { loadMyWorkspace, renderWorkspace, loadTeamWeeklyBrief, renderTeamWeeklyBrief, markTeamWeeklyBriefRead } from "./workspace.js";

const dashboardViewStorageKey = "team-ai-assistant-main-view-v1";
const roleLabels = { member: "팀원", manager: "팀장", admin: "관리자" };
const sourceLabels = { live: "실시간 데이터", snapshot: "저장 데이터", error: "연결 오류" };

const dashboardState = {
  data: null,
  loading: false,
  error: "",
  period: 30,
  metric: "",
  customer: "",
  selectedIssueId: "",
  initialized: false,
};

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
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function riskLabel(level) {
  return { high: "위험", attention: "주의", watch: "관찰" }[level] || "관찰";
}

function emptyBlock(message) {
  return `<div class="workspace-empty"><span aria-hidden="true">✓</span><p>${escapeHtml(message)}</p></div>`;
}

function sectionHeader(eyebrow, title, meta = "") {
  return `<header><div><span>${escapeHtml(eyebrow)}</span><h4>${escapeHtml(title)}</h4></div>${meta ? `<em>${escapeHtml(meta)}</em>` : ""}</header>`;
}

function metricCard(key, label, value, helper, tone = "", delta = "") {
  const active = dashboardState.metric === key ? " is-active" : "";
  return `
    <button class="workspace-metric ${tone}${active}" type="button" data-dashboard-metric="${key}" aria-pressed="${dashboardState.metric === key}">
      <span class="dashboard-metric-top"><span>${escapeHtml(label)}</span>${delta}</span>
      <strong>${formatNumber(value)}</strong>
      <small>${escapeHtml(helper)}</small>
    </button>`;
}

function renderMetrics(summary) {
  const rate = summary.changeRate;
  const delta = rate == null
    ? '<em class="dashboard-delta">비교 기준 없음</em>'
    : `<em class="dashboard-delta ${rate > 0 ? "is-up" : rate < 0 ? "is-down" : ""}">${rate > 0 ? "↑" : rate < 0 ? "↓" : "–"} ${Math.abs(rate)}%</em>`;
  return `
    <section class="workspace-summary-grid dashboard-metrics" aria-label="핵심 운영 지표">
      ${metricCard("open", "미종료 이슈", summary.open, `이월 ${formatNumber(summary.carriedOpen)}건`, "is-blue", delta)}
      ${metricCard("overdue", "일정 지연", summary.overdue, "완료 예정일 경과", "is-danger")}
      ${metricCard("aging", "장기 미종료", summary.aging, "30일 이상 미종료", "is-warning")}
      ${metricCard("highRisk", "고위험", summary.highRisk, `우선순위 높음 ${formatNumber(summary.highPriority)}건`, "is-danger")}
    </section>`;
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
      <h4>${escapeHtml(decision.headline || "운영 신호를 확인했습니다.")}</h4>
      <ul>${signals}</ul>
      ${actions ? `<div class="workspace-decision-actions">${actions}</div>` : ""}
    </section>`;
}

function renderTodo(issue) {
  const classes = ["workspace-item", "dashboard-clickable"];
  if (issue.overdue) classes.push("is-overdue");
  else if (Number(issue.ageDays || 0) >= 30) classes.push("is-aging");
  return `
    <article class="${classes.join(" ")}" role="button" tabindex="0" data-dashboard-issue="${escapeHtml(issue.id)}">
      <div class="workspace-item-main">
        <div class="workspace-item-title"><span>#${escapeHtml(issue.id)}</span><strong>${escapeHtml(issue.title)}</strong></div>
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
      ${sectionHeader("TODAY", "오늘 확인할 업무", `${todos.length}건`)}
      <div class="dashboard-section-list">${todos.length ? todos.map(renderTodo).join("") : emptyBlock("지금 바로 확인할 업무가 없습니다.")}</div>
    </section>`;
}

function renderHealthRow(item) {
  const reason = (item.reasons || []).join(" · ") || "특별한 위험 신호 없음";
  return `
    <article class="health-row dashboard-clickable" role="button" tabindex="0" data-dashboard-customer="${escapeHtml(item.customer)}">
      <span class="health-status is-${escapeHtml(item.level)}" aria-hidden="true"></span>
      <div class="health-main">
        <div class="health-title"><strong>${escapeHtml(item.customer)}</strong><span class="health-label is-${escapeHtml(item.level)}">${escapeHtml(item.label)}</span></div>
        <p title="${escapeHtml(reason)}">${escapeHtml(reason)}</p>
      </div>
      <div class="health-metrics"><strong>미종료 ${formatNumber(item.openCount)}</strong><span>지연 ${formatNumber(item.overdueCount)} · 장기 ${formatNumber(item.agingCount)}</span></div>
    </article>`;
}

function renderCustomerHealth(items, summary) {
  const meta = `위험 ${formatNumber(summary.high)} · 주의 ${formatNumber(summary.attention)}`;
  return `
    <section class="workspace-section">
      ${sectionHeader("CUSTOMER HEALTH", "고객사 건강도", meta)}
      <div class="dashboard-section-list">${items.length ? items.map(renderHealthRow).join("") : emptyBlock("분석할 고객사 데이터가 없습니다.")}</div>
    </section>`;
}

function renderSpikeRows(items, type) {
  if (!items.length) return '<div class="workspace-empty"><p>급증 신호 없음</p></div>';
  return items.map((item) => {
    const ratio = item.ratio == null ? "이전 0건" : `${item.ratio}배`;
    const attribute = type === "customer" ? `data-dashboard-customer="${escapeHtml(item.name)}" role="button" tabindex="0"` : "";
    return `<article class="dashboard-spike-row ${type === "customer" ? "dashboard-clickable" : ""}" ${attribute}>
      <strong>${escapeHtml(type === "screen" ? `화면 ${item.name}` : item.name)}</strong>
      <em>${formatNumber(item.count)}건</em>
      <small>직전 ${formatNumber(item.previousCount)}건 · ${escapeHtml(ratio)}</small>
    </article>`;
  }).join("");
}

function renderSpikes(customerSpikes, screenSpikes) {
  return `
    <section class="workspace-section">
      ${sectionHeader("EARLY SIGNAL", "급증 신호", `${customerSpikes.length + screenSpikes.length}개`)}
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
      ${sectionHeader("WORKLOAD", "팀원별 업무 부하", `${members.length}명`)}
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

function renderFilterbar() {
  const chips = [];
  if (dashboardState.metric && dashboardState.metric !== "open") {
    chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="metric">${escapeHtml({ overdue: "일정 지연", aging: "장기 미종료", highRisk: "고위험" }[dashboardState.metric])} ×</button>`);
  }
  if (dashboardState.customer) chips.push(`<button class="dashboard-chip" type="button" data-dashboard-clear="customer">고객사: ${escapeHtml(dashboardState.customer)} ×</button>`);
  if (!chips.length) return "";
  return `<div class="dashboard-filterbar"><span>적용 필터</span>${chips.join("")}<button class="dashboard-chip dashboard-chip-clear" type="button" data-dashboard-clear="all">전체 해제</button></div>`;
}

function renderPriorities(priorities) {
  const filtered = filteredPriorities(priorities);
  const rows = filtered.map((issue) => `
    <tr class="dashboard-clickable" role="button" tabindex="0" data-dashboard-issue="${escapeHtml(issue.id)}">
      <td><span class="dashboard-risk-badge is-${escapeHtml(issue.riskLevel)}">${riskLabel(issue.riskLevel)}</span></td>
      <td><strong class="dashboard-issue-title">#${escapeHtml(issue.id)} ${escapeHtml(issue.title)}</strong><small>${escapeHtml((issue.riskReasons || []).join(" · "))}</small></td>
      <td>${escapeHtml(issue.project || "-")}</td><td>${escapeHtml(issue.handler || "미지정")}</td>
      <td>${issue.ageDays == null ? "-" : `${formatNumber(issue.ageDays)}일`}</td><td>${issue.overdue ? '<span class="danger-text">지연</span>' : escapeHtml(issue.status || "-")}</td>
    </tr>`).join("");
  return `
    ${renderFilterbar()}
    <section class="workspace-section dashboard-priority-section">
      ${sectionHeader("PRIORITY QUEUE", "우선 확인 이슈", `${filtered.length} / ${priorities.length}건`)}
      ${filtered.length ? `<div class="workspace-table-wrap"><table class="workspace-table dashboard-priority-table"><thead><tr><th>위험도</th><th>이슈</th><th>고객사</th><th>담당자</th><th>경과</th><th>상태</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyBlock("현재 필터에 해당하는 우선 이슈가 없습니다.")}
    </section>`;
}

function updateSourceMeta(meta) {
  const source = meta?.source || "error";
  els.dashboardSource.className = `dashboard-source is-${source}`;
  els.dashboardSource.querySelector("em").textContent = sourceLabels[source] || "데이터 상태";
  els.dashboardSource.title = meta?.sourceReason || "";
  els.dashboardGeneratedAt.textContent = meta?.generatedAt ? `${formatDateTime(meta.generatedAt)} 기준` : "";
}

function renderDashboard() {
  if (!els.dashboardBody) return;
  if (dashboardState.loading && !dashboardState.data) {
    els.dashboardBody.innerHTML = '<div class="dashboard-state"><span class="dashboard-spinner" aria-hidden="true"></span><strong>운영 데이터를 불러오고 있습니다.</strong><p>고객사 건강도와 위험 신호를 같은 기준으로 계산합니다.</p></div>';
    return;
  }
  if (dashboardState.error && !dashboardState.data) {
    els.dashboardBody.innerHTML = `<div class="dashboard-state is-error"><strong>대시보드를 불러오지 못했습니다.</strong><p>${escapeHtml(dashboardState.error)}</p><button class="dashboard-ai-button is-secondary" type="button" data-dashboard-retry>다시 시도</button></div>`;
    updateSourceMeta({ source: "error" });
    return;
  }
  const data = dashboardState.data;
  if (!data) return;
  updateSourceMeta(data.meta);
  const scopeLabel = data.meta.scope === "team" ? "팀 전체 기준" : "내 담당 업무 기준";
  els.dashboardSubtitle.textContent = `${scopeLabel} · ${data.meta.periodFrom} ~ ${data.meta.periodTo}`;
  els.dashboardBody.innerHTML = `
    <div class="dashboard-welcome"><div><h3>팀 전체 업무 현황</h3><p>${escapeHtml(data.meta.periodFrom)}부터 ${escapeHtml(data.meta.periodTo)}까지, 직전 동일 기간과 비교합니다.</p></div><span class="dashboard-scope">${escapeHtml(scopeLabel)} · ${escapeHtml(roleLabels[data.user.role] || data.user.role)}</span></div>
    ${renderMetrics(data.summary)}
    ${renderDecision(data.decisionSummary, data.meta.periodDays)}
    <div class="dashboard-grid">${renderTodos(data.todos || [])}${renderCustomerHealth(data.customerHealth || [], data.customerHealthSummary || {})}${renderSpikes(data.customerSpikes || [], data.screenSpikes || [])}</div>
    ${renderMembers(data.members || [])}
    ${renderPriorities(data.priorities || [])}
    <details class="team-brief-section"><summary>팀 주간 브리핑</summary><div class="team-brief-content"></div></details>`;
  const brief = els.dashboardBody.querySelector(".team-brief-section");
  brief.addEventListener("toggle", () => {
    if (brief.open) updateTeamBrief();
  });
}

async function loadDashboard(force = false) {
  if (!canViewTeamDashboard()) return;
  if (dashboardState.loading || (dashboardState.data && !force)) return;
  dashboardState.loading = true;
  dashboardState.error = "";
  els.dashboardRefreshBtn.disabled = true;
  renderDashboard();
  try {
    const response = await apiFetch(`${WORKSPACE_ENDPOINTS.dashboard}?days=${dashboardState.period}`);
    const payload = await responseJson(response);
    if (!response.ok) throw new Error(payload.detail || "운영 데이터를 불러오지 못했습니다.");
    dashboardState.data = payload;
  } catch (error) {
    dashboardState.error = error.message || "운영 데이터를 불러오지 못했습니다.";
  } finally {
    dashboardState.loading = false;
    els.dashboardRefreshBtn.disabled = false;
    renderDashboard();
  }
}

function canViewTeamDashboard() {
  return Boolean(state.currentUser?.uid);
}

function mainViews() {
  return [
    ["chat", els.viewChatBtn, els.chatPanel],
    ["personal", els.viewPersonalBtn, els.personalPanel],
    ["dashboard", els.viewDashboardBtn, els.dashboardPanel],
  ];
}

function setActiveView(view, { remember = true, load = true } = {}) {
  if (view === "dashboard" && !canViewTeamDashboard()) view = "personal";
  if (!["chat", "personal", "dashboard"].includes(view)) view = "chat";
  els.viewDashboardBtn.hidden = !canViewTeamDashboard();
  for (const [name, button, panel] of mainViews()) {
    const selected = name === view;
    panel.hidden = !selected;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  if (remember) localStorage.setItem(`${dashboardViewStorageKey}:${state.currentUser?.uid || "guest"}`, view);
  document.body.classList.remove("sidebar-open");
  for (const button of [els.sidebarMenuToggle, els.personalSidebarMenuToggle, els.dashboardSidebarMenuToggle]) {
    button?.setAttribute("aria-expanded", "false");
  }
  if (view === "dashboard" && load) loadDashboard();
  if (view === "personal" && load) {
    renderWorkspace();
    if (!state.myWorkspace) loadMyWorkspace().catch((error) => {
      els.workspaceContent.innerHTML = `<div class="workspace-error"><strong>업무 정보를 불러오지 못했습니다.</strong><p>${escapeHtml(error.message)}</p></div>`;
    });
  }
}

function showPersonalView(options) {
  setActiveView("personal", options);
}

async function updateTeamBrief(refresh = false) {
  const container = els.dashboardBody.querySelector(".team-brief-content");
  if (!container || !canViewTeamDashboard()) return;
  container.innerHTML = '<p class="workspace-loading">팀 브리핑을 불러오는 중입니다.</p>';
  try {
    await loadTeamWeeklyBrief(refresh);
    container.innerHTML = renderTeamWeeklyBrief();
    await markTeamWeeklyBriefRead();
  } catch (error) {
    container.innerHTML = `<p class="workspace-error">${escapeHtml(error.message)}</p>`;
  }
}

function showDashboardView(options) {
  setActiveView("dashboard", options);
}

function showChatView(options) {
  setActiveView("chat", options);
}

function askFromDashboard(question) {
  if (!question) return;
  showChatView();
  els.input.value = question;
  els.input.dispatchEvent(new Event("input", { bubbles: true }));
  els.input.focus();
}

function issueById(id) {
  const items = [...(dashboardState.data?.priorities || []), ...(dashboardState.data?.todos || [])];
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
      <div><span>상태 / 우선순위</span><strong>${escapeHtml(issue.status || "-")} · ${escapeHtml(issue.priority || "-")}</strong></div>
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

function handleDashboardAction(target) {
  const refreshBrief = target.closest("[data-refresh-team-brief]");
  if (refreshBrief) return updateTeamBrief(true);
  const briefQuestion = target.closest("[data-workspace-question]");
  if (briefQuestion) return askFromDashboard(decodeURIComponent(briefQuestion.dataset.workspaceQuestion));
  const retry = target.closest("[data-dashboard-retry]");
  if (retry) return loadDashboard(true);
  const metric = target.closest("[data-dashboard-metric]");
  if (metric) {
    dashboardState.metric = dashboardState.metric === metric.dataset.dashboardMetric ? "" : metric.dataset.dashboardMetric;
    renderDashboard();
    return;
  }
  const clear = target.closest("[data-dashboard-clear]");
  if (clear) {
    if (clear.dataset.dashboardClear !== "customer") dashboardState.metric = "";
    if (clear.dataset.dashboardClear !== "metric") dashboardState.customer = "";
    renderDashboard();
    return;
  }
  const customer = target.closest("[data-dashboard-customer]");
  if (customer) {
    dashboardState.customer = customer.dataset.dashboardCustomer;
    renderDashboard();
    els.dashboardBody.querySelector(".dashboard-priority-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const issue = target.closest("[data-dashboard-issue]");
  if (issue) return openIssueDetail(issue.dataset.dashboardIssue);
  const question = target.closest("[data-dashboard-question]");
  if (question) return askFromDashboard(question.dataset.dashboardQuestion);
}

function initDashboard() {
  if (dashboardState.initialized || !els.dashboardPanel) return;
  dashboardState.initialized = true;
  els.viewDashboardBtn.addEventListener("click", () => showDashboardView());
  els.viewPersonalBtn.addEventListener("click", () => showPersonalView());
  els.viewChatBtn.addEventListener("click", () => showChatView());
  mainViews().forEach(([, button]) => button.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const visible = mainViews().filter(([, tab]) => !tab.hidden);
    const index = visible.findIndex(([, tab]) => tab === button);
    const next = event.key === "Home" ? 0 : event.key === "End" ? visible.length - 1
      : (index + (event.key === "ArrowLeft" ? -1 : 1) + visible.length) % visible.length;
    setActiveView(visible[next][0]);
    visible[next][1].focus();
  }));
  els.dashboardPeriod.addEventListener("change", () => {
    dashboardState.period = Number(els.dashboardPeriod.value) || 30;
    loadDashboard(true);
  });
  els.dashboardRefreshBtn.addEventListener("click", () => loadDashboard(true));
  els.dashboardBody.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardBody.addEventListener("keydown", (event) => {
    if (!["Enter", " "].includes(event.key)) return;
    if (!event.target.closest("[data-dashboard-issue], [data-dashboard-customer]")) return;
    event.preventDefault();
    handleDashboardAction(event.target);
  });
  els.dashboardDetail.addEventListener("click", (event) => handleDashboardAction(event.target));
  els.dashboardDetailClose.addEventListener("click", closeIssueDetail);
  [els.dashboardSidebarMenuToggle, els.personalSidebarMenuToggle].forEach((button) => button?.addEventListener("click", () => {
    const open = document.body.classList.toggle("sidebar-open");
    els.dashboardSidebarMenuToggle.setAttribute("aria-expanded", String(open));
    els.sidebarMenuToggle?.setAttribute("aria-expanded", String(open));
    els.personalSidebarMenuToggle?.setAttribute("aria-expanded", String(open));
  }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !els.dashboardDetail.hidden) closeIssueDetail();
  });
  const stored = localStorage.getItem(`${dashboardViewStorageKey}:${state.currentUser?.uid || "guest"}`);
  const initialView = ["chat", "personal", "dashboard"].includes(stored) ? stored : "chat";
  setActiveView(initialView, { remember: false, load: true });
}

export { initDashboard, loadDashboard, renderDashboard, showDashboardView, showPersonalView, showChatView };
