// 팀 대시보드 › 화면번호 탭. 화면번호 × 최근 8주 신규 이슈와 선택한 화면의 상세를 보여 준다.
import { els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { customerMark, escapeHtml } from "./ui_format.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { focusKey, restoreFocus } from "./view_focus.js?v=20261103-1";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { applyDashboardTab, registerDashboardTab, tabHeaderSlots } from "./dashboard_tabs.js?v=20261110-1";

// 칸 색은 건수 구간마다 한 단계씩 짙어진다. 마지막 값 이상은 가장 짙은 단계.
const HEAT_STEPS = [0, 1, 2, 4, 6];

const hotspotState = {
  media: "",
  customer: "",
  selected: "",
  data: null,
  loading: false,
  error: "",
  requestId: 0,
};

function formatNumber(value, digits = 0) {
  return Number(value || 0).toLocaleString("ko-KR", { maximumFractionDigits: digits });
}

function formatShortDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${Number(match[2])}월 ${Number(match[3])}일` : "";
}

function viewElement() {
  return document.querySelector("#screenHotspotView");
}

function isComplete() {
  return hotspotState.data?.meta?.search_complete === true;
}

function heatLevel(count) {
  let level = 0;
  HEAT_STEPS.forEach((step, index) => { if (count > step) level = index + 1; });
  return level;
}

function selectedRow() {
  const rows = hotspotState.data?.rows || [];
  return rows.find((row) => row.screen === hotspotState.selected) || rows[0] || null;
}

function scopeText() {
  const meta = hotspotState.data?.meta || {};
  return [meta.customerName, meta.media].filter(Boolean).join(" ");
}

function toolbarSubtitle() {
  const summary = hotspotState.data?.summary;
  return summary
    ? `최근 8주 등록 기준 · 화면번호가 있는 이슈 ${countLabel(Math.max(0, summary.issueCount - summary.unknownScreenCount))}${summary.unknownScreenCount ? ` (화면번호 없음 ${countLabel(summary.unknownScreenCount)} 제외)` : ""}`
    : "화면번호별 이슈를 조회하고 있어요.";
}

function renderTools() {
  return `
    <span class="data-badge-slot" data-screen-badge></span>
    <button type="button" class="secondary-button workspace-refresh-button" data-screen-refresh aria-label="새로고침">${icon("refresh", 16)}<span>새로고침</span></button>`;
}

function renderFilters() {
  const data = hotspotState.data;
  const mediaOptions = (data?.options?.media || ["HTS", "MTS"])
    .map((value) => `<option value="${escapeHtml(value)}"${hotspotState.media === value ? " selected" : ""}>${escapeHtml(value)}</option>`).join("");
  const customers = data?.options?.customers || [];
  const selectedMissing = hotspotState.customer && !customers.some((item) => item.customer === hotspotState.customer);
  const customerOptions = [
    ...(selectedMissing ? [{ customer: hotspotState.customer, name: data?.meta?.customerName || hotspotState.customer }] : []),
    ...customers,
  ].map((item) => `<option value="${escapeHtml(item.customer)}"${hotspotState.customer === item.customer ? " selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
  return `<div class="screen-controls" role="group" aria-label="화면번호 조회 조건"><div class="screen-controls-fields">
    <label class="screen-filter">매체
      <select data-screen-filter="media"><option value="">전체</option>${mediaOptions}</select>
    </label>
    <label class="screen-filter">고객사
      <select data-screen-filter="customer"><option value="">전체</option>${customerOptions}</select>
    </label>
    </div><div class="screen-controls-scope"><span>${escapeHtml(scopeText() || "팀 전체")} · 최근 8주</span>${hotspotState.media || hotspotState.customer ? '<button type="button" class="customer-more-button" data-screen-reset>필터 초기화</button>' : ""}</div></div>`;
}

function countLabel(value, unit = "건") {
  return `${formatNumber(value)}${unit}${isComplete() ? "" : " 이상"}`;
}

function renderMetrics(data) {
  const summary = data.summary || {};
  const known = Math.max(0, Number(summary.issueCount || 0) - Number(summary.unknownScreenCount || 0));
  const metrics = [
    ["화면번호 있는 이슈", countLabel(known), "최근 8주 등록 기준"],
    ["집계된 화면", countLabel(summary.screenCount, "개"), data.rows?.length ? `아래에는 신규 많은 상위 ${formatNumber(data.rows.length)}개 화면` : "현재 조회 조건 기준"],
    ["화면번호 없음", countLabel(summary.unknownScreenCount), "화면별 집계에서 제외된 이슈"],
  ];
  return `<section class="workspace-kpis screen-metrics" aria-label="화면번호 집계 범위">${metrics.map(([label, value, helper]) => `
    <article class="workspace-kpi"><span class="workspace-kpi-label">${label}</span><div class="workspace-kpi-value"><strong>${value}</strong></div><small>${helper}</small></article>`).join("")}</section>`;
}

// 화면번호 위 한 줄 요약. 일부 조회에서는 순위·급증을 판단하지 않아 만들지 않는다.
// 0000처럼 화면을 특정하지 않는 번호는 "가장 많은 화면"으로 고르지 않는다.
function screenHeadline(data, complete) {
  if (!complete) return "";
  const summary = data.summary || {};
  const known = Math.max(0, Number(summary.issueCount || 0) - Number(summary.unknownScreenCount || 0));
  if (!known) return "";
  const top = (data.rows || []).filter((row) => !/^0+$/.test(String(row.screen).trim())).sort((a, b) => b.total - a.total)[0];
  const spikes = (data.spikes || []).length;
  return `최근 8주 화면번호가 기록된 이슈는 ${formatNumber(known)}건이에요.${top ? ` 가장 많은 화면은 ${top.screen}(${formatNumber(top.total)}건)이에요.` : ""}${spikes ? ` 지난주 신규가 급증한 화면은 ${formatNumber(spikes)}개예요.` : ""}`;
}

// 선택한 화면의 8주 추이. 값은 히트맵 행과 같고, 수치는 히트맵에서 읽는다.
function weeklySparkline(row, weeks) {
  const values = row.weeks || [];
  if (values.length < 2) return "";
  const max = Math.max(1, ...values);
  const points = values.map((value, index) => `${(index / (values.length - 1) * 296 + 2).toFixed(1)},${(62 - value / max * 54).toFixed(1)}`);
  const label = values.map((value, index) => `${weeks[index]?.label || index + 1} ${formatNumber(value)}건`).join(", ");
  return `<svg class="screen-sparkline" viewBox="0 0 300 66" role="img" aria-label="${escapeHtml(`최근 8주 주별 신규: ${label}`)}"><polygon points="${points.join(" ")} 298,64 2,64"></polygon><polyline points="${points.join(" ")}"></polyline></svg>`;
}

function renderSpikes(data) {
  if (!isComplete()) {
    return '<p class="customer-detail-note">일부 데이터만 확인해 화면번호 급증 여부는 판단하지 않았어요. 건수는 확인한 범위의 최소값이에요.</p>';
  }
  const spikes = data.spikes || [];
  if (!spikes.length) return "";
  const rows = spikes.map((spike) => {
    const change = spike.ratio == null
      ? `앞선 4주에는 없다가 지난주 ${formatNumber(spike.count)}건이 등록됐어요.`
      : `앞선 4주 평균 ${formatNumber(spike.baselineAverage, 1)}건에서 지난주 ${formatNumber(spike.count)}건으로 늘었어요.`;
    return `
      <button type="button" class="screen-spike" data-screen-select="${escapeHtml(spike.screen)}" aria-label="화면 ${escapeHtml(spike.screen)} 지난주 급증, ${escapeHtml(change)} 자세히 보기">
        <span class="screen-spike-icon" aria-hidden="true">${icon("arrow-up", 18)}</span>
        <span class="screen-spike-text"><b>화면 ${escapeHtml(spike.screen)}</b><small>${escapeHtml(change)}</small></span>
        <span class="screen-spike-badge">지난주 ${formatNumber(spike.count)}건</span>
      </button>`;
  }).join("");
  return `<section class="screen-spikes" aria-label="지난주 급증 화면">${rows}</section>`;
}

function renderHeatmap(data) {
  const rows = data.rows || [];
  const selected = selectedRow()?.screen;
  const weeks = data.weeks || [];
  if (!rows.length) {
    return `
      <section class="workspace-section screen-heatmap screen-empty" aria-label="화면번호별 주간 신규 이슈">
        ${icon("search", 24)}<h3>${!isComplete() ? "확인한 범위에는 화면번호가 기록된 이슈가 없어요." : hotspotState.media || hotspotState.customer ? "조건에 맞는 화면번호가 없어요." : "최근 8주에 화면번호가 기록된 이슈가 없어요."}</h3>
        <p>${isComplete() ? "매체·고객사 조건을 바꾸거나 데이터를 새로고침해 보세요." : "전체 결과는 미확정이에요. 새로고침으로 다시 확인해 주세요."}</p>
        <button type="button" class="customer-more-button" data-screen-refresh>새로고침</button>
      </section>`;
  }
  const head = weeks.map((week) => `<th scope="col"${week.partial ? ' class="is-partial" title="이번 주는 오늘까지만 반영"' : ""}>${escapeHtml(week.label)}${week.partial ? "*" : ""}</th>`).join("");
  const body = rows.map((row) => `
    <tr class="${row.screen === selected ? "is-selected" : ""}">
      <th scope="row"><button type="button" class="screen-row-button" data-screen-select="${escapeHtml(row.screen)}" aria-pressed="${row.screen === selected}" aria-controls="screenDetailTitle">${escapeHtml(row.screen)}</button></th>
      ${row.weeks.map((count) => `<td class="screen-heat is-l${heatLevel(count)}">${formatNumber(count)}</td>`).join("")}
      <td class="screen-total">${formatNumber(row.total)}</td>
    </tr>`).join("");
  return `
    <section class="workspace-section screen-heatmap" aria-label="화면번호별 주간 신규 이슈">
      <header><div><h3>화면번호별 신규 이슈</h3><p>화면번호를 선택하면 상세를 볼 수 있어요.</p></div><em>상위 ${formatNumber(rows.length)}개 화면</em></header>
      <div class="customer-detail-section-body">
        <div class="customer-issue-table-wrap">
          <table class="screen-heat-table">
            <thead><tr><th scope="col">화면</th>${head}<th scope="col" class="screen-total">합계</th></tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>
        <div class="screen-heat-legend" aria-label="주별 이슈 수 색상 범례"><span>주별 등록</span>${["0", "1", "2", "3–4", "5–6", "7+"].map((label, index) => `<span><i class="screen-heat is-l${index}" aria-hidden="true"></i>${label}건</span>`).join("")}</div>
        <p class="customer-detail-note">주는 월요일부터 세요. * 이번 주는 오늘까지의 등록 건수예요.</p>
      </div>
    </section>`;
}

function renderDetail() {
  const row = selectedRow();
  if (!row) return "";
  const maxCustomer = Math.max(1, ...row.customers.map((item) => item.count));
  const customers = row.customers.slice(0, 5).map((item) => `
    <li>
      <button type="button" class="customer-issue-link effort-ranked-name" data-screen-customer="${escapeHtml(item.customer)}" aria-label="${escapeHtml(item.name)} 고객사 상세 보기">${customerMark(item.name)}<span>${escapeHtml(item.name)}</span></button>
      <span class="customer-ranked-track" aria-hidden="true"><i style="width:${Math.round((item.count / maxCustomer) * 100)}%"></i></span>
      <span class="customer-ranked-value">${formatNumber(item.count)}건</span>
    </li>`).join("");
  const issues = row.recentIssues.map((issue) => `
    <li>
      <button type="button" class="customer-issue-link" data-screen-source="${escapeHtml(issue.id)}" aria-label="#${escapeHtml(issue.id)} 상세 보기">#${escapeHtml(issue.id)}</button>
      <span class="screen-issue-title">${escapeHtml(issue.title)}</span>
      <small>${escapeHtml([issue.customerName, workspaceStatusLabel(issue.status), formatShortDate(issue.createdAt)].filter(Boolean).join(" · "))}</small>
    </li>`).join("");
  const playbooks = row.playbooks.map((playbook) => `
    <li class="customer-playbook">
      <strong>${escapeHtml(playbook.title)}</strong>
      ${playbook.symptom ? `<p>${escapeHtml(playbook.symptom)}</p>` : ""}
      <small>이 화면 이슈 ${playbook.matchedIssueIds.map((id) => `#${escapeHtml(id)}`).join(" ")} 근거</small>
    </li>`).join("");
  return `
    <aside class="workspace-section screen-detail" aria-labelledby="screenDetailTitle">
      <div class="customer-detail-section-body">
        <div class="screen-detail-heading">
          <span>선택한 화면</span>
          <h3 id="screenDetailTitle" tabindex="-1">${escapeHtml(row.screen)}</h3>
          ${isComplete() && (hotspotState.data?.spikes || []).some((spike) => spike.screen === row.screen) ? '<p><span class="screen-spike-badge">지난주 급증</span></p>' : ""}
        </div>
        ${weeklySparkline(row, hotspotState.data?.weeks || [])}
        <dl class="screen-detail-metrics"><div><dt>8주 신규</dt><dd>${countLabel(row.total)}</dd></div><div><dt>미종료</dt><dd>${countLabel(row.openCount)}</dd></div><div><dt>고객사</dt><dd>${countLabel(row.customers.length, "곳")}</dd></div></dl>
        <section class="screen-detail-section"><h4>최근 등록 이슈</h4>
          ${issues ? `<ol class="screen-issue-list">${issues}</ol>` : '<p class="customer-detail-empty">확인한 최근 등록 이슈가 없어요.</p>'}</section>
        <section class="screen-detail-section"><h4>고객사별</h4>
          ${customers ? `<ol class="customer-ranked-list effort-ranked-list has-marks">${customers}</ol>` : '<p class="customer-detail-empty">고객사가 기록된 이슈가 없어요.</p>'}</section>
        <section class="screen-detail-section"><h4>플레이북</h4>
          ${playbooks ? `<ul class="customer-playbook-list screen-playbooks">${playbooks}</ul>` : '<p class="customer-detail-empty">이 화면 이슈를 근거로 한 공개 플레이북이 아직 없어요.</p>'}</section>
        <div class="screen-detail-actions">
          <button type="button" class="customer-primary-button" data-screen-ask="summary">개발팀 전달용 요약</button>
          <button type="button" class="secondary-button" data-screen-ask="list">이슈 ${formatNumber(row.total)}건 대화에서 보기</button>
        </div>
      </div>
    </aside>`;
}

function renderBody() {
  const data = hotspotState.data;
  if (!data) {
    if (hotspotState.error) {
      return `<div class="workspace-error">${escapeHtml(hotspotState.error)} <button type="button" class="secondary-button" data-screen-refresh>다시 시도</button></div>`;
    }
    return '<div class="workspace-loading">화면번호별 이슈를 불러오는 중이에요.</div>';
  }
  const headline = screenHeadline(data, isComplete());
  return `
    ${hotspotState.error ? `<p class="customer-detail-error" role="alert">${escapeHtml(hotspotState.error)}</p>` : ""}
    ${headline ? `<p class="screen-headline">${escapeHtml(headline)}</p>` : ""}
    ${renderMetrics(data)}
    ${renderSpikes(data)}
    <div class="screen-hotspot-layout">${renderHeatmap(data)}${renderDetail()}</div>`;
}

function updateBadge(container) {
  const slot = container?.querySelector("[data-screen-badge]");
  const meta = hotspotState.data?.meta;
  if (!slot) return;
  const badgeState = dataBadgeState({
    source: meta?.source || "error",
    complete: meta?.search_complete !== false,
    refreshing: Boolean(meta?.refreshPending),
    loading: hotspotState.loading,
  });
  const time = formatBadgeTime(meta?.fetchedAt);
  const details = mantisBadgeDetails(badgeState, { time, reason: meta?.sourceReason });
  renderDataBadge(slot, {
    state: badgeState,
    time: ["fresh", "partial", "snapshot"].includes(badgeState) ? time : "",
    sources: [details.source, ...(meta?.scopeLabel ? [{ name: "범위", tone: "muted", text: meta.scopeLabel }] : [])],
    note: details.note,
    onRetry: details.retry ? () => loadScreenHotspot(true) : null,
  });
}

function render() {
  const view = viewElement();
  if (!view) return;
  const { subtitle, tools } = tabHeaderSlots("screens");
  const focused = focusKey(view) || focusKey(tools);
  if (subtitle) subtitle.textContent = subtitle.title = toolbarSubtitle();
  if (tools) tools.innerHTML = renderTools();
  view.innerHTML = `<div class="dashboard-tab-inner">${renderFilters()}<div class="customer-detail-body" aria-busy="${hotspotState.loading}">${renderBody()}</div></div>`;
  for (const container of [view, tools]) if (container) restoreFocus(container, focused);
  updateBadge(tools);
}

async function loadScreenHotspot(refresh = false) {
  const requestId = ++hotspotState.requestId;
  hotspotState.loading = true;
  render();
  const params = new URLSearchParams();
  if (hotspotState.media) params.set("media", hotspotState.media);
  if (hotspotState.customer) params.set("customer", hotspotState.customer);
  if (refresh) params.set("refresh", "true");
  const query = params.toString();
  try {
    const response = await apiFetch(`${WORKSPACE_ENDPOINTS.screenHotspot}${query ? `?${query}` : ""}`);
    const payload = await responseJson(response);
    if (requestId !== hotspotState.requestId) return;
    if (!response.ok) throw new Error(payload.detail || "화면번호별 이슈를 불러오지 못했어요.");
    hotspotState.data = payload;
    hotspotState.error = "";
    if (!payload.rows?.some((row) => row.screen === hotspotState.selected)) hotspotState.selected = payload.rows?.[0]?.screen || "";
  } catch (error) {
    if (requestId !== hotspotState.requestId) return;
    hotspotState.error = error.message || "화면번호별 이슈를 불러오지 못했어요.";
  }
  hotspotState.loading = false;
  render();
}

function askFromHotspot(question) {
  if (!question || !els.input) return;
  showChatView();
  els.input.value = question;
  els.input.dispatchEvent(new Event("input", { bubbles: true }));
  els.input.focus();
}

function screenQuestion(kind) {
  const row = selectedRow();
  if (!row) return "";
  const scope = scopeText();
  const subject = `${scope ? `${scope} ` : ""}화면번호 ${row.screen} 최근 8주 이슈`;
  return kind === "summary"
    ? `${subject}를 개발팀에 전달할 수 있게 증상·고객사·재현 조건 중심으로 요약해줘`
    : `${subject} 목록 보여줘`;
}

function openCustomer(customer) {
  return import("./customer_detail.js?v=20261103-1").then(({ openCustomerDetail }) => openCustomerDetail(customer, {
    period: 30,
    onAsk: askFromHotspot,
    onClose: () => {
      applyDashboardTab();
      viewElement()?.querySelector(`[data-screen-customer="${CSS.escape(customer)}"]`)?.focus();
    },
  }));
}

function handleClick(event) {
  const target = event.target;
  const select = target.closest("[data-screen-select]");
  if (select) {
    hotspotState.selected = select.dataset.screenSelect;
    render();
    // 렌더러의 기존 포커스 복귀가 끝난 다음 새 선택 위치로 이동한다.
    return Promise.resolve().then(() => {
      if (viewElement()?.clientWidth <= 1100) {
        const heading = viewElement()?.querySelector("#screenDetailTitle");
        heading?.focus({ preventScroll: true });
        return heading?.scrollIntoView({ block: "start" });
      }
      return viewElement()?.querySelector(`.screen-row-button[data-screen-select="${CSS.escape(hotspotState.selected)}"]`)?.focus();
    });
  }
  if (target.closest("[data-screen-reset]")) {
    hotspotState.media = "";
    hotspotState.customer = "";
    return loadScreenHotspot().then(() => restoreFocus(viewElement(), '[data-screen-filter="media"]'));
  }
  if (target.closest("[data-screen-refresh]")) return loadScreenHotspot(true);
  const ask = target.closest("[data-screen-ask]");
  if (ask) return askFromHotspot(screenQuestion(ask.dataset.screenAsk));
  const customer = target.closest("[data-screen-customer]");
  if (customer) return openCustomer(customer.dataset.screenCustomer);
  const source = target.closest("[data-screen-source]");
  if (source) {
    return import("./chat_source_detail.js?v=20260911-6")
      .then(({ openSourceDetail }) => openSourceDetail({ type: "chroma", issue_id: source.dataset.screenSource }));
  }
}

function handleChange(event) {
  const filter = event.target.closest?.("[data-screen-filter]");
  if (!filter) return;
  hotspotState[filter.dataset.screenFilter] = filter.value;
  return loadScreenHotspot();
}

function initScreenHotspot() {
  const view = viewElement();
  if (!view) return;
  view.addEventListener("click", handleClick);
  view.addEventListener("change", handleChange);
  const { tools } = tabHeaderSlots("screens");
  tools?.addEventListener("click", handleClick);
  tools?.addEventListener("change", handleChange);
  registerDashboardTab("screens", {
    view: viewElement,
    onShow: () => { if (!hotspotState.loading) void loadScreenHotspot(); },
  });
}

export { initScreenHotspot, loadScreenHotspot, screenHeadline };
