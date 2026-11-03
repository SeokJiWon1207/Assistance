// 팀 대시보드 › 화면번호 탭. 화면번호 × 최근 8주 신규 이슈와 선택한 화면의 상세를 보여 준다.
import { els, apiFetch, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { responseJson } from "./api.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { focusKey, restoreFocus } from "./view_focus.js?v=20261103-1";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { applyDashboardTab, registerDashboardTab } from "./dashboard_tabs.js?v=20261103-1";

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

function renderToolbar() {
  const data = hotspotState.data;
  const summary = data?.summary;
  const subtitle = summary
    ? `최근 8주 등록 기준 · 화면번호가 있는 이슈 ${formatNumber(summary.issueCount - summary.unknownScreenCount)}건${summary.unknownScreenCount ? ` (화면번호 없음 ${formatNumber(summary.unknownScreenCount)}건 제외)` : ""}`
    : "화면번호별 이슈를 조회하고 있습니다.";
  const mediaOptions = (data?.options?.media || ["HTS", "MTS"])
    .map((value) => `<option value="${escapeHtml(value)}"${hotspotState.media === value ? " selected" : ""}>${escapeHtml(value)}</option>`).join("");
  const customers = data?.options?.customers || [];
  const selectedMissing = hotspotState.customer && !customers.some((item) => item.customer === hotspotState.customer);
  const customerOptions = [
    ...(selectedMissing ? [{ customer: hotspotState.customer, name: data?.meta?.customerName || hotspotState.customer }] : []),
    ...customers,
  ].map((item) => `<option value="${escapeHtml(item.customer)}"${hotspotState.customer === item.customer ? " selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
  return `
    <div class="dashboard-tab-toolbar">
      <p>${escapeHtml(subtitle)}</p>
      <div class="customer-detail-toolbar">
        <span class="data-badge-slot" data-screen-badge></span>
        <label class="screen-filter">매체
          <select data-screen-filter="media"><option value="">전체</option>${mediaOptions}</select>
        </label>
        <label class="screen-filter">고객사
          <select data-screen-filter="customer"><option value="">전체</option>${customerOptions}</select>
        </label>
        <button type="button" class="secondary-button workspace-refresh-button" data-screen-refresh aria-label="새로고침">${icon("refresh", 16)}<span>새로고침</span></button>
      </div>
    </div>`;
}

function renderSpikes(data) {
  if (!isComplete()) {
    return '<p class="customer-detail-note">일부 데이터만 확인해 화면번호 급증 여부는 판단하지 않았습니다.</p>';
  }
  return (data.spikes || []).map((spike) => {
    const change = spike.ratio == null
      ? `앞선 4주에는 없다가 지난주 ${formatNumber(spike.count)}건이 등록됐습니다.`
      : `앞선 4주 평균 ${formatNumber(spike.baselineAverage, 1)}건에서 지난주 ${formatNumber(spike.count)}건으로 늘었습니다.`;
    return `
      <section class="screen-spike" aria-label="지난주 급증 화면">
        <span class="screen-spike-badge">지난주 급증</span>
        <p><b>화면 ${escapeHtml(spike.screen)}</b> · ${escapeHtml(change)}</p>
        <button type="button" class="customer-issue-link" data-screen-select="${escapeHtml(spike.screen)}">${escapeHtml(spike.screen)} 자세히 보기 ›</button>
      </section>`;
  }).join("");
}

function renderHeatmap(data) {
  const rows = data.rows || [];
  const selected = selectedRow()?.screen;
  const weeks = data.weeks || [];
  if (!rows.length) {
    return `
      <section class="workspace-section screen-heatmap" aria-label="화면번호별 주간 신규 이슈">
        <div class="customer-detail-section-body"><p class="customer-detail-empty">최근 8주에 화면번호가 기록된 이슈가 없습니다.</p></div>
      </section>`;
  }
  const head = weeks.map((week) => `<th scope="col"${week.partial ? ' class="is-partial" title="이번 주는 오늘까지만 반영"' : ""}>${escapeHtml(week.label)}${week.partial ? "*" : ""}</th>`).join("");
  const body = rows.map((row) => `
    <tr class="${row.screen === selected ? "is-selected" : ""}">
      <th scope="row"><button type="button" class="screen-row-button" data-screen-select="${escapeHtml(row.screen)}" aria-pressed="${row.screen === selected}">${escapeHtml(row.screen)}</button></th>
      ${row.weeks.map((count) => `<td class="screen-heat is-l${heatLevel(count)}">${formatNumber(count)}</td>`).join("")}
      <td class="screen-total">${formatNumber(row.total)}</td>
    </tr>`).join("");
  return `
    <section class="workspace-section screen-heatmap" aria-label="화면번호별 주간 신규 이슈">
      <header><div><h3>화면번호 × 주별 신규 이슈</h3></div><em>신규 많은 상위 ${formatNumber(rows.length)}개 화면</em></header>
      <div class="customer-detail-section-body">
        <div class="customer-issue-table-wrap">
          <table class="screen-heat-table">
            <thead><tr><th scope="col">화면</th>${head}<th scope="col" class="screen-total">합계</th></tr></thead>
            <tbody>${body}</tbody>
          </table>
        </div>
        <p class="customer-detail-note">주는 월요일부터 셉니다. * 표시한 이번 주는 오늘까지만 반영합니다. 화면을 누르면 오른쪽에 자세히 보입니다.</p>
      </div>
    </section>`;
}

function renderDetail() {
  const row = selectedRow();
  if (!row) return "";
  const maxCustomer = Math.max(1, ...row.customers.map((item) => item.count));
  const customers = row.customers.slice(0, 5).map((item) => `
    <li>
      <button type="button" class="customer-issue-link effort-ranked-name" data-screen-customer="${escapeHtml(item.customer)}" aria-label="${escapeHtml(item.name)} 고객사 상세 보기">${escapeHtml(item.name)}</button>
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
          <h3 id="screenDetailTitle">${escapeHtml(row.screen)}</h3>
          <p>8주 신규 ${formatNumber(row.total)}건 · 미종료 ${formatNumber(row.openCount)}건 · 고객사 ${formatNumber(row.customers.length)}곳</p>
        </div>
        <h4>고객사별</h4>
        ${customers ? `<ol class="customer-ranked-list effort-ranked-list">${customers}</ol>` : '<p class="customer-detail-empty">고객사가 기록된 이슈가 없습니다.</p>'}
        <h4>최근 등록 이슈</h4>
        <ol class="screen-issue-list">${issues}</ol>
        <h4>플레이북</h4>
        ${playbooks ? `<ul class="customer-playbook-list screen-playbooks">${playbooks}</ul>` : '<p class="customer-detail-empty">이 화면 이슈를 근거로 한 공개 플레이북이 아직 없습니다.</p>'}
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
    return '<div class="workspace-loading">화면번호별 이슈를 불러오는 중입니다.</div>';
  }
  return `
    ${hotspotState.error ? `<p class="customer-detail-error" role="alert">${escapeHtml(hotspotState.error)}</p>` : ""}
    ${renderSpikes(data)}
    <div class="screen-hotspot-layout">${renderHeatmap(data)}${renderDetail()}</div>`;
}

function updateBadge(view) {
  const slot = view.querySelector("[data-screen-badge]");
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
  const focused = focusKey(view);
  view.innerHTML = `<div class="dashboard-tab-inner">${renderToolbar()}<div class="customer-detail-body" aria-busy="${hotspotState.loading}">${renderBody()}</div></div>`;
  restoreFocus(view, focused);
  updateBadge(view);
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
    if (!response.ok) throw new Error(payload.detail || "화면번호별 이슈를 불러오지 못했습니다.");
    hotspotState.data = payload;
    hotspotState.error = "";
    if (!payload.rows?.some((row) => row.screen === hotspotState.selected)) hotspotState.selected = payload.rows?.[0]?.screen || "";
  } catch (error) {
    if (requestId !== hotspotState.requestId) return;
    hotspotState.error = error.message || "화면번호별 이슈를 불러오지 못했습니다.";
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
    return viewElement()?.querySelector(`.screen-row-button[data-screen-select="${CSS.escape(hotspotState.selected)}"]`)?.focus();
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
  registerDashboardTab("screens", {
    view: viewElement,
    onShow: () => { if (!hotspotState.loading) void loadScreenHotspot(); },
  });
}

export { initScreenHotspot, loadScreenHotspot };
