// 팀 대시보드에서 고객사 한 곳을 열어 보는 상세 화면.
import { apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { formatPeriodRange, customerLabelText, customerMark, escapeHtml } from "./ui_format.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { focusKey, restoreFocus } from "./view_focus.js?v=20261103-1";

const PERIODS = [7, 14, 30, 90];
const OPEN_PREVIEW_LIMIT = 5;
const HEALTH_LABELS = { high: "위험", attention: "주의", stable: "안정", unknown: "판단 보류" };
const TREND_LABELS = { worsening: "악화 중", improving: "개선 중", steady: "변화 없음" };

const detailState = {
  customer: "",
  period: 30,
  data: null,
  loading: false,
  error: "",
  sort: "age",
  expanded: false,
  requestId: 0,
  onAsk: null,
  onClose: null,
};

function formatNumber(value) {
  return Number(value || 0).toLocaleString("ko-KR");
}

function formatDays(value) {
  return value == null ? "—" : `${Number(value).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}`;
}

function formatShortDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${Number(match[2])}월 ${Number(match[3])}일` : "";
}

function viewElement() {
  return document.querySelector("#customerDetailView");
}

function panelElement() {
  return document.querySelector("#dashboardPanel");
}

function isComplete() {
  return detailState.data?.meta?.search_complete === true;
}

// 일부 이슈만 확인했으면 숫자가 하한값이라는 것을 숫자 옆에서 알린다.
function countText(value) {
  return `${formatNumber(value)}${isComplete() ? "" : '<span class="dashboard-lower-bound">건 이상</span>'}`;
}

function metric(label, value, helper, tone = "", unit = "") {
  return `
    <article class="workspace-kpi ${tone}" aria-label="${escapeHtml(label)}">
      <span class="workspace-kpi-label">${escapeHtml(label)}</span>
      <div class="workspace-kpi-value"><strong>${value}${unit ? `<span class="customer-detail-unit">${escapeHtml(unit)}</span>` : ""}</strong></div>
      <small>${escapeHtml(helper)}</small>
    </article>`;
}

function renderMetrics(summary, period) {
  const previous = summary.previousNewIssues;
  const intakeHelper = previous == null ? "직전 기간 비교 미확정" : `직전 ${period}일 ${formatNumber(previous)}건`;
  const handlingHelper = summary.teamAverageHandlingDays == null
    ? "해결·종료 이슈 기준"
    : `팀 평균 ${formatDays(summary.teamAverageHandlingDays)}일`;
  const unit = isComplete() ? "건" : "";
  return `
    <section class="workspace-kpis customer-detail-metrics" aria-label="고객사 핵심 지표">
      ${metric("미종료", countText(summary.open), `이월 ${formatNumber(summary.carriedOpen)}건`, "", unit)}
      ${metric("일정 지연", countText(summary.overdue), "완료 예정일 경과", summary.overdue ? "is-danger" : "", unit)}
      ${metric("장기 미종료", countText(summary.aging), "30일 이상 미종료", summary.aging ? "is-warning" : "", unit)}
      ${metric("평균 처리일", formatDays(summary.averageHandlingDays), handlingHelper, "", summary.averageHandlingDays == null ? "" : "일")}
      ${metric(`최근 ${period}일 신규`, countText(summary.newIssues), intakeHelper, "", unit)}
    </section>`;
}

function renderHealth(health) {
  const level = isComplete() && HEALTH_LABELS[health.level] ? health.level : "unknown";
  const reasons = (health.reasons || []).map((reason) => `<li>${escapeHtml(customerLabelText(reason))}</li>`).join("");
  return `
    <section class="workspace-section customer-detail-health is-${level}" aria-label="고객사 건강도 판단 근거">
      <h3>${level === "unknown" ? "건강도를 확정하지 않았어요" : `${HEALTH_LABELS[level]}으로 본 이유`}</h3>
      ${reasons ? `<ul>${reasons}</ul>` : "<p>표시할 근거가 없어요.</p>"}
      <p class="customer-detail-note">팀 대시보드의 고객사 건강도와 같은 기준이에요.</p>
    </section>`;
}

function sectionHeader(title, meta = "") {
  return `<header><div><h3>${escapeHtml(title)}</h3></div>${meta ? `<em>${escapeHtml(meta)}</em>` : ""}</header>`;
}

function renderMonthly(monthly) {
  if (!monthly.some((row) => row.newCount > 0 || row.closedCount > 0)) {
    return `<section class="workspace-section customer-detail-monthly" aria-label="월별 신규·종료 항목 변경">${sectionHeader("월별 신규 · 종료 항목 변경", "최근 6개월")}
      <div class="customer-detail-section-body"><p class="customer-detail-empty">${isComplete() ? "최근 6개월에 신규 등록·종료 항목 변경이 없어요." : "확인한 범위에는 월별 신규·종료 항목 변경 이슈가 없어요. 전체 결과는 미확정이에요."}</p></div></section>`;
  }
  const max = Math.max(1, ...monthly.flatMap((row) => [row.newCount, row.closedCount]));
  const label = monthly.map((row) => `${row.label} 신규 ${row.newCount}건 종료 항목 변경 ${row.closedCount}건`).join(", ");
  const columns = monthly.map((row) => `
    <div class="customer-monthly-column">
      <div class="customer-monthly-bars" aria-hidden="true">
        <i class="is-new${row.newCount ? "" : " is-zero"}" style="height:${Math.round((row.newCount / max) * 100)}%"></i>
        <i class="is-closed${row.closedCount ? "" : " is-zero"}" style="height:${Math.round((row.closedCount / max) * 100)}%"></i>
      </div>
      <span>${escapeHtml(row.label)}</span>
      <small><b>${formatNumber(row.newCount)}</b> / ${formatNumber(row.closedCount)}</small>
    </div>`).join("");
  return `
    <section class="workspace-section customer-detail-monthly" aria-label="월별 신규·종료 항목 변경">
      ${sectionHeader("월별 신규 · 종료 항목 변경", "최근 6개월")}
      <div class="customer-detail-section-body">
        <div class="customer-monthly-legend" aria-hidden="true"><span><i class="is-new"></i>신규</span><span><i class="is-closed"></i>종료 항목 변경</span></div>
        <div class="customer-monthly-chart" role="img" aria-label="${escapeHtml(label)}">${columns}</div>
        <p class="customer-detail-note">종료 항목 변경은 현재 해결·종료 상태인 이슈의 최종수정월 기준이며 실제 완료 건수와 달라요.</p>
      </div>
    </section>`;
}

function rankedBars(rows, emptyText) {
  if (!rows.length) return `<p class="customer-detail-empty">${escapeHtml(emptyText)}</p>`;
  const max = Math.max(1, ...rows.map((row) => row.count));
  return `<ol class="customer-ranked-list">${rows.map((row) => `
    <li>
      <span class="customer-ranked-name">${escapeHtml(row.name)}</span>
      <span class="customer-ranked-track" aria-hidden="true"><i style="width:${Math.round((row.count / max) * 100)}%"></i></span>
      <span class="customer-ranked-value">${formatNumber(row.count)}건</span>
    </li>`).join("")}</ol>`;
}

function renderMedia(media) {
  if (!media.length) return "";
  const total = media.reduce((sum, row) => sum + row.count, 0);
  if (!total) return "";
  const label = media.map((row) => `${row.name} ${row.count}건`).join(", ");
  return `
    <h4>매체 구분</h4>
    <div class="customer-media-bar" role="img" aria-label="매체 구분: ${escapeHtml(label)}">
      ${media.map((row, index) => `<i class="is-${Math.min(index, 2)}" style="flex:${row.count}" title="${escapeHtml(`${row.name} ${row.count}건`)}"></i>`).join("")}
    </div>
    <p class="customer-media-legend">${media.map((row, index) => `<span><i class="is-${Math.min(index, 2)}" aria-hidden="true"></i>${escapeHtml(row.name)} ${formatNumber(row.count)} (${Math.round((row.count / total) * 100)}%)</span>`).join("")}</p>`;
}

function renderBreakdowns(data, period) {
  return `
    <section class="workspace-section customer-detail-breakdown" aria-label="화면번호와 매체 구분">
      ${sectionHeader("자주 나오는 화면번호", `최근 ${period}일 신규 기준`)}
      <div class="customer-detail-section-body">
        ${rankedBars(data.screens || [], isComplete() ? "이 기간에 화면번호가 기록된 신규 이슈가 없어요." : "확인한 범위에는 화면번호가 기록된 신규 이슈가 없어요.")}
        ${renderMedia(data.media || [])}
      </div>
    </section>`;
}

// 완료예정일이 없거나 '미상'인 이슈는 날짜순 정렬에서 맨 뒤로 보낸다.
function dueKey(issue) {
  const match = String(issue.dueDate || "").match(/^\d{4}-\d{2}-\d{2}/);
  return match ? match[0] : "";
}

function sortedOpenIssues(issues) {
  const rows = [...issues];
  if (detailState.sort === "due") {
    rows.sort((left, right) => (dueKey(left) ? 0 : 1) - (dueKey(right) ? 0 : 1)
      || dueKey(left).localeCompare(dueKey(right)));
  }
  return rows;
}

function renderOpenIssues(data) {
  const issues = sortedOpenIssues(data.openIssues || []);
  const total = Number(data.openIssuesTotal || issues.length);
  const visible = detailState.expanded ? issues : issues.slice(0, OPEN_PREVIEW_LIMIT);
  const sortButton = (key, label) => `<button type="button" class="customer-sort-button" data-customer-sort="${key}" aria-pressed="${detailState.sort === key}">${label}</button>`;
  const rows = visible.map((issue) => {
    const due = formatShortDate(issue.dueDate);
    return `
      <tr>
        <td class="customer-open-main"><div class="customer-open-meta"><span>#${escapeHtml(issue.id)}</span><span class="status-badge">${escapeHtml(workspaceStatusLabel(issue.status))}</span></div>
          <button type="button" class="customer-issue-link customer-open-title" data-customer-source="${escapeHtml(issue.id)}" aria-label="#${escapeHtml(issue.id)} ${escapeHtml(issue.title)} 상세 보기">${escapeHtml(issue.title)}</button></td>
        <td data-label="담당자">${escapeHtml(issue.handler || "미지정")}</td>
        <td data-label="경과" class="is-number">${formatNumber(issue.ageDays)}일</td>
        <td data-label="완료예정" class="${issue.overdue ? "is-overdue" : ""}">${due ? `${escapeHtml(due)}${issue.overdue ? " 지남" : ""}` : "—"}</td>
      </tr>`;
  }).join("");
  const more = issues.length > OPEN_PREVIEW_LIMIT
    ? `<button type="button" class="customer-more-button" data-customer-expand aria-expanded="${detailState.expanded}" aria-controls="customerOpenIssues">${detailState.expanded ? "접기" : `${formatNumber(issues.length)}건 모두 보기`}</button>`
    : "";
  const capped = total > issues.length ? `<p class="customer-detail-note">경과일이 긴 ${formatNumber(issues.length)}건만 표시해요. 전체 ${formatNumber(total)}건은 대화에서 조회할 수 있어요.</p>` : "";
  return `
    <section class="workspace-section customer-detail-issues" aria-label="미종료 이슈">
      <header><div><h3>미종료 이슈 <span class="customer-detail-count">${formatNumber(visible.length)} / ${formatNumber(total)}건${isComplete() ? "" : " 이상"}</span></h3></div>
        ${issues.length > 1 ? `<div class="customer-sort" role="group" aria-label="정렬">${sortButton("age", "경과일순")}${sortButton("due", "완료예정일순")}</div>` : ""}</header>
      <div class="customer-detail-section-body">
        ${issues.length ? `<div class="customer-issue-table-wrap" id="customerOpenIssues"><table class="customer-issue-table customer-open-table">
          <thead><tr><th scope="col">이슈</th><th scope="col">담당자</th><th scope="col" class="is-number">경과</th><th scope="col">완료예정</th></tr></thead>
          <tbody>${rows}</tbody></table></div>${more}${capped}` : `<div class="customer-open-empty"><strong>${isComplete() ? "미종료 이슈가 없어요." : "확인한 범위에는 미종료 이슈가 없어요."}</strong><p>${isComplete() ? "월별 신규·종료 항목 변경 추이와 관련 플레이북을 확인해 보세요." : "일부 데이터만 확인해 전체 결과는 미확정이에요."}</p></div>`}
      </div>
    </section>`;
}

function renderPlaybooks(playbooks) {
  const cards = playbooks.map((playbook) => `
    <li class="customer-playbook">
      <strong>${escapeHtml(playbook.title)}</strong>
      ${playbook.symptom ? `<p>${escapeHtml(playbook.symptom)}</p>` : ""}
      <small>이 고객사 이슈 ${playbook.matchedIssueIds.map((id) => `<button type="button" class="customer-issue-link" data-customer-source="${escapeHtml(id)}">#${escapeHtml(id)}</button>`).join(" ")} 근거${playbook.updatedAt ? ` · ${escapeHtml(formatShortDate(playbook.updatedAt))} 수정` : ""}</small>
    </li>`).join("");
  return `
    <section class="workspace-section customer-detail-playbooks" aria-label="관련 플레이북">
      ${sectionHeader("이 고객사에 쓰인 플레이북", "공개된 플레이북")}
      <div class="customer-detail-section-body">
        ${cards ? `<ul class="customer-playbook-list">${cards}</ul>` : '<p class="customer-detail-empty">이 고객사 이슈를 근거로 한 공개 플레이북이 아직 없어요.</p>'}
      </div>
    </section>`;
}

function renderHeader() {
  const data = detailState.data;
  const name = data?.customer?.name || detailState.customer;
  const health = data?.health;
  const level = isComplete() && HEALTH_LABELS[health?.level] ? health.level : "unknown";
  const trend = TREND_LABELS[health?.trend];
  const meta = data?.meta;
  const subtitle = meta
    ? `${formatPeriodRange(meta.periodFrom, meta.periodTo)} · ${meta.scopeLabel || "팀"} 담당분`
    : "고객사 데이터를 조회하고 있어요.";
  const periods = PERIODS.map((days) => `<button type="button" data-customer-period="${days}" aria-pressed="${detailState.period === days}">${days}일</button>`).join("");
  return `
    <nav class="customer-detail-crumb" aria-label="위치">
      <button type="button" class="customer-detail-back" data-customer-back>${icon("chevron-left", 16)}<span>팀 대시보드</span></button>
    </nav>
    <header class="customer-detail-header">
      <div class="customer-detail-heading">
        <div class="customer-detail-title">
          ${customerMark(name, "lg")}
          <h2 id="customerDetailTitle" tabindex="-1">${escapeHtml(name)}</h2>
          ${health ? `<span class="customer-health-badge is-${level}">${HEALTH_LABELS[level]}${trend && level !== "unknown" ? ` · ${trend}` : ""}</span>` : ""}
        </div>
        <p>${escapeHtml(subtitle)}</p>
      </div>
      <div class="customer-detail-toolbar">
        <span class="data-badge-slot" data-customer-badge></span>
        <fieldset class="dashboard-period"><legend class="sr-only">조회 기간</legend>${periods}</fieldset>
        <button type="button" class="secondary-button workspace-refresh-button" data-customer-refresh aria-label="새로고침">${icon("refresh", 16)}<span>새로고침</span></button>
      </div>
    </header>
    ${data ? `<div class="customer-detail-actions">
      <button type="button" class="secondary-button" data-customer-ask="report">월간 리포트 만들기</button>
      <button type="button" class="customer-primary-button" data-customer-ask="analysis">이 고객사에 대해 묻기</button>
    </div>` : ""}`;
}

function renderBody() {
  const data = detailState.data;
  if (!data) {
    if (detailState.error) {
      return `<div class="workspace-error">${escapeHtml(detailState.error)} <button type="button" class="secondary-button" data-customer-refresh>다시 시도</button></div>`;
    }
    return '<div class="workspace-loading">고객사 현황을 불러오는 중이에요.</div>';
  }
  const period = data.meta?.periodDays || detailState.period;
  return `
    ${detailState.error ? `<p class="customer-detail-error" role="alert">${escapeHtml(detailState.error)}</p>` : ""}
    ${isComplete() ? "" : '<p class="customer-detail-note">일부 데이터만 확인했어요. 건수는 확인한 범위의 최소값이며, 전체 결과는 미확정이에요.</p>'}
    ${renderMetrics(data.summary || {}, period)}
    ${renderHealth(data.health || {})}
    <div class="customer-detail-grid">${renderMonthly(data.monthly || [])}${renderBreakdowns(data, period)}</div>
    ${renderOpenIssues(data)}
    ${renderPlaybooks(data.playbooks || [])}`;
}

function updateBadge(view) {
  const slot = view.querySelector("[data-customer-badge]");
  const meta = detailState.data?.meta;
  if (!slot) return;
  const badgeState = dataBadgeState({
    source: meta?.source || "error",
    complete: meta?.search_complete !== false,
    refreshing: Boolean(meta?.refreshPending),
    loading: detailState.loading,
  });
  const time = formatBadgeTime(meta?.fetchedAt);
  const details = mantisBadgeDetails(badgeState, { time, reason: meta?.sourceReason });
  renderDataBadge(slot, {
    state: badgeState,
    time: ["fresh", "partial", "snapshot"].includes(badgeState) ? time : "",
    sources: [details.source, ...(meta?.scopeLabel ? [{ name: "범위", tone: "muted", text: meta.scopeLabel }] : [])],
    note: details.note,
    onRetry: details.retry ? () => loadCustomerDetail(true) : null,
  });
}

function render() {
  const view = viewElement();
  if (!view) return;
  const focused = focusKey(view);
  view.innerHTML = `<div class="customer-detail-inner">${renderHeader()}<div class="customer-detail-body" aria-busy="${detailState.loading}">${renderBody()}</div></div>`;
  restoreFocus(view, focused);
  updateBadge(view);
}

async function loadCustomerDetail(refresh = false) {
  const requestId = ++detailState.requestId;
  detailState.loading = true;
  render();
  const params = new URLSearchParams({ name: detailState.customer, days: String(detailState.period) });
  if (refresh) params.set("refresh", "true");
  try {
    const response = await apiFetch(`${WORKSPACE_ENDPOINTS.customer}?${params}`);
    const payload = await responseJson(response);
    if (requestId !== detailState.requestId) return;
    if (!response.ok) throw new Error(payload.detail || "고객사 현황을 불러오지 못했어요.");
    detailState.data = payload;
    detailState.error = "";
  } catch (error) {
    if (requestId !== detailState.requestId) return;
    detailState.error = error.message || "고객사 현황을 불러오지 못했어요.";
  }
  detailState.loading = false;
  render();
}

function askQuestion(kind) {
  const name = detailState.data?.customer?.name || detailState.customer;
  const period = detailState.data?.meta?.periodDays || detailState.period;
  const month = Number(String(detailState.data?.meta?.periodTo || "").slice(5, 7));
  const question = kind === "report"
    ? `${name} ${month ? `${month}월 ` : ""}월간 고객사 리포트 작성해줘`
    : `${name} 최근 ${period}일 이슈 현황과 주요 원인을 분석해줘`;
  detailState.onAsk?.(question);
}

function handleClick(event) {
  const target = event.target;
  if (target.closest("[data-customer-back]")) return closeCustomerDetail();
  const period = target.closest("[data-customer-period]");
  if (period) {
    const days = Number(period.dataset.customerPeriod);
    if (days === detailState.period) return;
    detailState.period = days;
    return loadCustomerDetail();
  }
  if (target.closest("[data-customer-refresh]")) return loadCustomerDetail(true);
  const ask = target.closest("[data-customer-ask]");
  if (ask) return askQuestion(ask.dataset.customerAsk);
  const sort = target.closest("[data-customer-sort]");
  if (sort) {
    detailState.sort = sort.dataset.customerSort;
    render();
    return viewElement()?.querySelector(`[data-customer-sort="${detailState.sort}"]`)?.focus();
  }
  if (target.closest("[data-customer-expand]")) {
    detailState.expanded = !detailState.expanded;
    render();
    return viewElement()?.querySelector("[data-customer-expand]")?.focus();
  }
  const source = target.closest("[data-customer-source]");
  if (source) {
    return import("./chat_source_detail.js?v=20260911-6")
      .then(({ openSourceDetail }) => openSourceDetail({ type: "chroma", issue_id: source.dataset.customerSource }));
  }
}

function isCustomerDetailOpen() {
  return Boolean(viewElement() && !viewElement().hidden);
}

function openCustomerDetail(customer, { period = 30, onAsk = null, onClose = null } = {}) {
  const view = viewElement();
  if (!view || !customer) return;
  if (!view.dataset.bound) {
    view.addEventListener("click", handleClick);
    view.dataset.bound = "true";
  }
  const changed = customer !== detailState.customer || !PERIODS.includes(period) || period !== detailState.period;
  Object.assign(detailState, {
    customer,
    period: PERIODS.includes(period) ? period : 30,
    onAsk,
    onClose,
    ...(changed ? { data: null, error: "", sort: "age", expanded: false } : {}),
  });
  panelElement()?.setAttribute("data-subview", "customer");
  view.hidden = false;
  view.scrollTop = 0;
  const loading = loadCustomerDetail();
  view.querySelector("#customerDetailTitle")?.focus();
  return loading;
}

function closeCustomerDetail() {
  const view = viewElement();
  if (!view) return;
  detailState.requestId += 1;
  view.hidden = true;
  panelElement()?.removeAttribute("data-subview");
  detailState.onClose?.();
}

export { openCustomerDetail, closeCustomerDetail, isCustomerDetailOpen };
