import { state, els, conversationsStorageKeyBase, activeConversationStorageKeyBase, maxStoredConversations, conversationSyncTimers, apiFetch, deriveConversationTitle, storageSafeMessages, scopedStorageKey, createConversationRecord, WORKSPACE_ENDPOINTS } from "./runtime.js";
import { renderMessages, escapeHtml, autoResizeInput } from "./chat.js";
import { cancelMessageEdit } from "./events.js";

function userScopedStorageKey(base) {
  return scopedStorageKey(base, state.currentUser?.uid);
}

function conversationsStorageKey() {
  return userScopedStorageKey(conversationsStorageKeyBase);
}

function activeConversationStorageKey() {
  return userScopedStorageKey(activeConversationStorageKeyBase);
}

function createConversation() {
  return createConversationRecord();
}

let toastContainer = null;

function showToast(message, tone = "default") {
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-stack";
    document.body.appendChild(toastContainer);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${tone}`;
  toast.textContent = message;
  toastContainer.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  setTimeout(() => {
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 220);
  }, 2400);
}

function persistConversations() {
  const active = state.conversations.find((item) => item.id === state.activeConversationId);
  if (active) {
    active.messages = storageSafeMessages(state.messages);
    active.title = deriveConversationTitle(active.messages);
    active.updatedAt = new Date().toISOString();
  }
  state.conversations.sort((a, b) => (
    Number(Boolean(b.favorite)) - Number(Boolean(a.favorite))
    || String(b.updatedAt).localeCompare(String(a.updatedAt))
  ));
  state.conversations = state.conversations.slice(0, maxStoredConversations);
  try {
    localStorage.setItem(conversationsStorageKey(), JSON.stringify(state.conversations));
    localStorage.setItem(activeConversationStorageKey(), state.activeConversationId || "");
  } catch {
    state.conversations = state.conversations.slice(0, 8);
    try {
      localStorage.setItem(conversationsStorageKey(), JSON.stringify(state.conversations));
    } catch {
      localStorage.removeItem(conversationsStorageKey());
      showToast("대화 저장 공간이 가득 차 일부 기록을 정리했어요.", "warning");
    }
  }
  if (active) scheduleConversationSync(active);
  renderConversationList();
}

async function syncConversation(conversation) {
  if (!state.currentUser?.uid || !conversation?.id) return;
  const response = await apiFetch(`/api/workspace/conversations/${encodeURIComponent(conversation.id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: conversation.title || "새 대화",
      messages: storageSafeMessages(conversation.messages || []),
      favorite: Boolean(conversation.favorite),
      createdAt: conversation.createdAt || "",
    }),
  });
  if (!response.ok) throw new Error("대화 기록을 서버에 저장하지 못했습니다.");
}

function scheduleConversationSync(conversation) {
  if (!state.currentUser?.uid || !conversation?.id) return;
  clearTimeout(conversationSyncTimers.get(conversation.id));
  conversationSyncTimers.set(conversation.id, setTimeout(() => {
    syncConversation(conversation).catch(() => {});
    conversationSyncTimers.delete(conversation.id);
  }, 350));
}

async function loadConversations() {
  try {
    const parsed = JSON.parse(localStorage.getItem(conversationsStorageKey()) || "[]");
    state.conversations = Array.isArray(parsed) ? parsed : [];
  } catch {
    state.conversations = [];
  }
  if (state.currentUser?.uid) {
    try {
      const response = await apiFetch("/api/workspace/conversations?limit=40");
      if (response.ok) {
        const serverItems = (await response.json()).conversations || [];
        if (serverItems.length) {
          const localById = new Map(state.conversations.map((item) => [item.id, item]));
          state.conversations = serverItems.map((item) => ({ ...localById.get(item.id), ...item }));
        } else {
          state.conversations.filter((item) => item.messages?.length).forEach(scheduleConversationSync);
        }
      }
    } catch {
      // 오프라인 또는 서버 오류 시 사용자별 로컬 기록으로 계속 동작한다.
    }
  }
  let activeId = localStorage.getItem(activeConversationStorageKey());
  if (!state.conversations.length) state.conversations.push(createConversation());
  if (!state.conversations.some((item) => item.id === activeId)) activeId = state.conversations[0].id;
  state.activeConversationId = activeId;
  state.messages = [...(state.conversations.find((item) => item.id === activeId)?.messages || [])];
  renderConversationList();
}

function updateActiveConversationTitle() {
  if (!els.activeConversationTitle) return;
  const active = state.conversations.find((item) => item.id === state.activeConversationId);
  els.activeConversationTitle.textContent = active?.title || "새 대화";
}

function renderConversationList() {
  updateActiveConversationTitle();
  if (!els.conversationList) return;
  els.conversationList.innerHTML = "";
  state.conversations.slice(0, 8).forEach((conversation) => {
    const row = document.createElement("div");
    row.className = `conversation-row${conversation.id === state.activeConversationId ? " active" : ""}`;
    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.className = "conversation-open";
    openButton.dataset.conversationId = conversation.id;
    openButton.textContent = conversation.title || "새 대화";
    openButton.title = conversation.title || "새 대화";
    const favoriteButton = document.createElement("button");
    favoriteButton.type = "button";
    favoriteButton.className = `conversation-favorite${conversation.favorite ? " active" : ""}`;
    favoriteButton.dataset.favoriteConversationId = conversation.id;
    favoriteButton.setAttribute("aria-label", conversation.favorite ? "즐겨찾기 해제" : "즐겨찾기 추가");
    favoriteButton.title = conversation.favorite ? "즐겨찾기 해제" : "즐겨찾기 추가";
    favoriteButton.textContent = conversation.favorite ? "★" : "☆";
    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "conversation-delete";
    deleteButton.dataset.deleteConversationId = conversation.id;
    deleteButton.setAttribute("aria-label", `${conversation.title || "대화"} 삭제`);
    deleteButton.title = "대화 삭제";
    deleteButton.textContent = "×";
    row.append(openButton, favoriteButton, deleteButton);
    els.conversationList.appendChild(row);
  });
}

function startNewConversation() {
  persistConversations();
  const conversation = createConversation();
  state.conversations.unshift(conversation);
  state.activeConversationId = conversation.id;
  state.messages = [];
  cancelMessageEdit();
  els.input.value = "";
  autoResizeInput();
  persistConversations();
  renderMessages();
  els.messages.scrollTop = 0;
}

function openConversation(conversationId) {
  if (state.busy || conversationId === state.activeConversationId) return;
  persistConversations();
  const conversation = state.conversations.find((item) => item.id === conversationId);
  if (!conversation) return;
  state.activeConversationId = conversationId;
  state.messages = [...(conversation.messages || [])];
  cancelMessageEdit();
  localStorage.setItem(activeConversationStorageKey(), conversationId);
  renderConversationList();
  renderMessages();
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value || ""));
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function workspaceMetric(label, value, tone = "") {
  return `<article class="workspace-metric ${tone}"><strong>${escapeHtml(String(value ?? 0))}</strong><span>${escapeHtml(label)}</span></article>`;
}

function workspaceEmpty(message) {
  return `<div class="workspace-empty"><span aria-hidden="true">✓</span><p>${escapeHtml(message)}</p></div>`;
}

function issueWorkspaceRow(issue) {
  const metadata = [issue.project, issue.status, issue.dueDate ? `예정 ${issue.dueDate}` : "", issue.ageDays ? `${issue.ageDays}일 경과` : ""]
    .filter(Boolean).join(" · ");
  return `
    <article class="workspace-item ${issue.overdue ? "is-overdue" : ""}">
      <div class="workspace-item-main">
        <strong><span>#${escapeHtml(String(issue.id || ""))}</span> ${escapeHtml(issue.title || "제목 없음")}</strong>
        <small>${escapeHtml(metadata)}</small>
      </div>
      <button type="button" class="workspace-item-action" data-workspace-question="${encodeURIComponent(`#${issue.id} 이슈의 현재 상태와 다음 할 일을 요약해줘`)}">AI에게 묻기</button>
    </article>`;
}

function renderPersonalOverview(data) {
  const summary = data.summary || {};
  const issues = data.mantis?.issues || [];
  const sourceLabel = data.mantis?.status === "snapshot" ? "로컬 스냅샷" : "실시간";
  const mapping = data.user?.mantisUsername
    ? `Mantis 담당자 '${data.user.mantisUsername}'(으)로 연결됨 · ${sourceLabel}`
    : "Mantis 담당자명이 연결되지 않았습니다. 관리자에게 담당자명 등록을 요청해 주세요.";
  return `
    ${renderTeamBriefBanner()}
    <section class="workspace-summary-grid">
      ${workspaceMetric("미종료", summary.open, summary.open ? "is-blue" : "")}
      ${workspaceMetric("일정 지연", summary.overdue, summary.overdue ? "is-danger" : "")}
      ${workspaceMetric("30일 이상", summary.aging, summary.aging ? "is-warning" : "")}
    </section>
    <p class="workspace-connection ${["unlinked", "error", "snapshot"].includes(data.mantis?.status) ? "is-warning" : ""}">${escapeHtml(mapping)}</p>
    <section class="workspace-section workspace-section-single">
      <header><div><span>Mantis</span><h4>내 담당 이슈</h4></div><em>${issues.length}건</em></header>
      <div class="workspace-list">${issues.length ? issues.map(issueWorkspaceRow).join("") : workspaceEmpty("최근 담당 이슈가 없습니다.")}</div>
    </section>`;
}

function renderTeamBriefBanner() {
  const brief = state.teamWeeklyBrief;
  if (!brief || brief.viewed || new Date().getDay() !== 1) return "";
  return `<button type="button" class="workspace-brief-banner" data-open-team-brief>
    <span>월요일 팀 브리핑이 도착했습니다</span>
    <strong>${escapeHtml(brief.title || "이번 주 팀 현황을 확인하세요")}</strong>
    <em>열어보기 →</em>
  </button>`;
}

function renderWeeklyBrief(data) {
  const brief = data.weeklyBrief || {};
  const highlights = brief.highlights || [];
  return `
    <section class="workspace-brief-hero">
      <span>이번 주 업무 브리핑</span>
      <h4>${escapeHtml(data.user?.displayName || "사용자")}님의 업무 요약</h4>
      <p>${escapeHtml(brief.period || "")}</p>
    </section>
    <section class="workspace-summary-grid">
      ${workspaceMetric("신규 이슈", brief.newIssues)}
      ${workspaceMetric("변경 이슈", brief.updatedIssues, "is-blue")}
      ${workspaceMetric("현재 미종료", brief.openIssues)}
      ${workspaceMetric("일정 지연", brief.overdueIssues, brief.overdueIssues ? "is-danger" : "")}
    </section>
    <section class="workspace-section workspace-section-single">
      <header><div><span>Priority</span><h4>이번 주 확인할 항목</h4></div></header>
      <div class="workspace-list">${highlights.length ? highlights.map(issueWorkspaceRow).join("") : workspaceEmpty("이번 주 우선 확인할 이슈가 없습니다.")}</div>
    </section>
    <button type="button" class="workspace-primary-action" data-workspace-question="${encodeURIComponent("내 담당 Mantis 이슈를 기준으로 이번 주 업무보고서를 작성해줘")}">AI로 주간 보고서 작성</button>`;
}

function briefSpikeRows(items, emptyMessage) {
  return items.length ? items.map((item) => {
    const comparison = item.previousCount
      ? `직전 주 ${item.previousCount}건 · ${item.ratio}배`
      : "직전 주 0건 · 새로 증가";
    return `<article class="workspace-insight-row"><strong>${escapeHtml(item.name)}</strong><span>${item.count}건</span><small>${escapeHtml(comparison)}</small></article>`;
  }).join("") : workspaceEmpty(emptyMessage);
}

function renderTeamWeeklyBrief() {
  const brief = state.teamWeeklyBrief;
  if (!brief) return `<div class="workspace-loading">팀 브리핑을 불러오는 중입니다.</div>`;
  const summary = brief.summary || {};
  const priorities = brief.priorities || [];
  const delta = summary.changeRate == null
    ? "비교 기준 없음"
    : `${summary.changeRate > 0 ? "+" : ""}${summary.changeRate}%`;
  const canRefresh = ["manager", "admin"].includes(state.currentUser?.role);
  return `
    <section class="workspace-brief-hero">
      <span>WEEKLY TEAM BRIEF</span>
      <h4>${escapeHtml(brief.title || "이번 주 팀 브리핑")}</h4>
      <p>지난주 ${escapeHtml(brief.summaryPeriod || "")} · 생성 ${escapeHtml(String(brief.generatedAt || "").slice(0, 16).replace("T", " "))}</p>
    </section>
    <section class="workspace-summary-grid">
      ${workspaceMetric("지난주 신규", summary.newIssues)}
      ${workspaceMetric("전주 대비", delta, summary.changeRate > 0 ? "is-warning" : "is-blue")}
      ${workspaceMetric("미종료 이월", summary.carriedOpen)}
      ${workspaceMetric("30일 이상", summary.aging, summary.aging ? "is-warning" : "")}
    </section>
    <p class="workspace-connection ${brief.source?.status === "live" ? "" : "is-warning"}">기준본 · ${brief.source?.status === "live" ? "Mantis 실시간 데이터로 생성" : "로컬 스냅샷으로 생성"} · 일정 지연 ${summary.overdue || 0}건 · 담당자 미지정 ${summary.unassigned || 0}건</p>
    <div class="workspace-columns">
      <section class="workspace-section">
        <header><div><span>Customer change</span><h4>급증 고객사</h4></div></header>
        <div class="workspace-insight-list">${briefSpikeRows(brief.customerSpikes || [], "급증한 고객사가 없습니다.")}</div>
      </section>
      <section class="workspace-section">
        <header><div><span>Screen change</span><h4>급증 화면번호</h4></div></header>
        <div class="workspace-insight-list">${briefSpikeRows(brief.screenSpikes || [], "급증한 화면번호가 없습니다.")}</div>
      </section>
    </div>
    <section class="workspace-section workspace-section-single">
      <header><div><span>Priority</span><h4>이번 주 우선 확인</h4></div><em>${priorities.length}건 표시</em></header>
      <div class="workspace-list">${priorities.length ? priorities.map(issueWorkspaceRow).join("") : workspaceEmpty("우선 확인할 이슈가 없습니다.")}</div>
    </section>
    ${canRefresh ? '<button type="button" class="workspace-primary-action" data-refresh-team-brief>최신 정보로 기준본 재생성</button>' : ""}`;
}

function renderNotifications(data) {
  const notifications = data.notifications || [];
  return `
    <section class="workspace-section workspace-section-single">
      <header><div><span>Notifications</span><h4>업무 알림</h4></div><em>${notifications.filter((item) => !item.read).length}개 안 읽음</em></header>
      <div class="workspace-list">${notifications.length ? notifications.map((item) => `
        <article class="workspace-notification ${item.read ? "is-read" : ""}">
          <span class="workspace-notification-dot" aria-hidden="true"></span>
          <div><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.description)}</p><small>${escapeHtml(item.date || "")}</small></div>
          ${item.read ? "" : `<button type="button" data-notification-read="${escapeHtml(item.id)}">읽음</button>`}
        </article>`).join("") : workspaceEmpty("새 업무 알림이 없습니다.")}</div>
    </section>`;
}

function renderFavoriteConversations() {
  const favorites = state.conversations.filter((item) => item.favorite);
  return `
    <section class="workspace-section workspace-section-single">
      <header><div><span>Saved</span><h4>즐겨찾는 대화</h4></div><em>${favorites.length}개</em></header>
      <div class="workspace-list">${favorites.length ? favorites.map((conversation) => `
        <article class="workspace-item">
          <div class="workspace-item-main"><strong>${escapeHtml(conversation.title || "새 대화")}</strong><small>${escapeHtml(String(conversation.updatedAt || "").slice(0, 10))} · ${(conversation.messages || []).length}개 메시지</small></div>
          <button type="button" class="workspace-item-action" data-open-favorite="${escapeHtml(conversation.id)}">대화 열기</button>
        </article>`).join("") : workspaceEmpty("사이드바의 별표를 눌러 자주 보는 대화를 저장해 보세요.")}</div>
    </section>`;
}

function renderTeamDashboard(data) {
  const summary = data.summary || {};
  const members = data.members || [];
  const aging = data.agingIssues || [];
  return `
    <section class="workspace-summary-grid">
      ${workspaceMetric("승인 사용자", summary.members)}
      ${workspaceMetric("미종료 이슈", summary.open, "is-blue")}
      ${workspaceMetric("일정 지연", summary.overdue, summary.overdue ? "is-danger" : "")}
      ${workspaceMetric("30일 이상", summary.aging, summary.aging ? "is-warning" : "")}
    </section>
    <p class="workspace-connection ${data.source?.status === "live" ? "" : "is-warning"}">Mantis 데이터: ${data.source?.status === "live" ? "실시간" : data.source?.status === "snapshot" ? "로컬 스냅샷" : "연결 오류"}</p>
    <div class="workspace-columns">
      <section class="workspace-section">
        <header><div><span>Workload</span><h4>담당자별 현황</h4></div></header>
        <div class="workspace-table-wrap"><table class="workspace-table"><thead><tr><th>담당자</th><th>미종료</th><th>지연</th><th>장기</th></tr></thead><tbody>
          ${members.map((member) => `<tr><td><strong>${escapeHtml(member.displayName || "")}</strong><small>${escapeHtml(member.mantisUsername || "미연결")}</small></td><td>${member.open}</td><td class="${member.overdue ? "danger-text" : ""}">${member.overdue}</td><td>${member.aging}</td></tr>`).join("")}
        </tbody></table></div>
      </section>
      <section class="workspace-section">
        <header><div><span>Aging</span><h4>장기 미종료 이슈</h4></div><em>${aging.length}건 표시</em></header>
        <div class="workspace-list">${aging.length ? aging.map(issueWorkspaceRow).join("") : workspaceEmpty("30일 이상 미종료 이슈가 없습니다.")}</div>
      </section>
    </div>`;
}

function updateWorkspaceUnread() {
  const unread = state.myWorkspace?.summary?.unreadNotifications || 0;
  if (els.workspaceUnreadBadge) {
    els.workspaceUnreadBadge.textContent = String(unread);
    els.workspaceUnreadBadge.hidden = unread < 1;
  }
  if (els.workspaceTabUnread) els.workspaceTabUnread.textContent = unread ? String(unread) : "";
}

function renderWorkspace() {
  if (!els.workspaceContent) return;
  if (state.workspaceMode === "team") {
    els.workspaceModalEyebrow.textContent = "관리자 워크스페이스";
    els.workspaceModalTitle.textContent = "팀 대시보드";
    els.workspaceTabs.hidden = true;
    els.workspaceContent.innerHTML = state.teamDashboard
      ? renderTeamDashboard(state.teamDashboard)
      : `<div class="workspace-loading">팀 현황을 불러오는 중입니다.</div>`;
    return;
  }
  els.workspaceModalEyebrow.textContent = "개인 워크스페이스";
  els.workspaceModalTitle.textContent = "내 업무";
  els.workspaceTabs.hidden = false;
  els.workspaceTabs.querySelectorAll("[data-workspace-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.workspaceTab === state.workspaceTab);
  });
  if (!state.myWorkspace) {
    els.workspaceContent.innerHTML = `<div class="workspace-loading">업무 정보를 불러오는 중입니다.</div>`;
    return;
  }
  const renderers = {
    overview: () => renderPersonalOverview(state.myWorkspace),
    brief: () => renderWeeklyBrief(state.myWorkspace),
    "team-brief": renderTeamWeeklyBrief,
    notifications: () => renderNotifications(state.myWorkspace),
    favorites: renderFavoriteConversations,
  };
  els.workspaceContent.innerHTML = (renderers[state.workspaceTab] || renderers.overview)();
  updateWorkspaceUnread();
}

async function loadMyWorkspace(force = false) {
  if (force) state.myWorkspace = null;
  renderWorkspace();
  const response = await apiFetch(WORKSPACE_ENDPOINTS.personal);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "개인 업무를 불러오지 못했습니다.");
  state.myWorkspace = data;
  updateWorkspaceUnread();
  renderWorkspace();
  loadTeamWeeklyBrief().catch(() => {});
}

async function loadTeamWeeklyBrief(refresh = false) {
  if (refresh) state.teamWeeklyBrief = null;
  renderWorkspace();
  const response = await apiFetch(
    refresh ? WORKSPACE_ENDPOINTS.teamBriefRefresh : WORKSPACE_ENDPOINTS.teamBrief,
    refresh ? { method: "POST" } : undefined,
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "팀 브리핑을 불러오지 못했습니다.");
  state.teamWeeklyBrief = data;
  renderWorkspace();
}

async function markTeamWeeklyBriefRead() {
  if (!state.teamWeeklyBrief || state.teamWeeklyBrief.viewed) return;
  const response = await apiFetch(WORKSPACE_ENDPOINTS.teamBriefRead, { method: "POST" });
  if (!response.ok) throw new Error("팀 브리핑 확인 상태를 저장하지 못했습니다.");
  state.teamWeeklyBrief.viewed = true;
  renderWorkspace();
}

async function loadTeamDashboard(force = false) {
  if (force) state.teamDashboard = null;
  renderWorkspace();
  const response = await apiFetch(WORKSPACE_ENDPOINTS.team);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "팀 현황을 불러오지 못했습니다.");
  state.teamDashboard = data;
  renderWorkspace();
}

function closeWorkspaceModal() {
  els.workspaceModal.hidden = true;
  document.body.classList.remove("modal-open");
}

async function openWorkspaceModal(mode = "personal") {
  state.workspaceMode = mode;
  if (mode === "personal" && !["overview", "brief", "team-brief", "notifications", "favorites"].includes(state.workspaceTab)) {
    state.workspaceTab = "overview";
  }
  els.workspaceModal.hidden = false;
  document.body.classList.add("modal-open");
  renderWorkspace();
  try {
    if (mode === "team") await loadTeamDashboard();
    else if (!state.myWorkspace) await loadMyWorkspace();
  } catch (error) {
    els.workspaceContent.innerHTML = `<div class="workspace-error"><strong>업무 정보를 불러오지 못했습니다.</strong><p>${escapeHtml(error.message)}</p></div>`;
  }
}

export {
  userScopedStorageKey,
  conversationsStorageKey,
  activeConversationStorageKey,
  createConversation,
  toastContainer,
  showToast,
  persistConversations,
  syncConversation,
  scheduleConversationSync,
  loadConversations,
  updateActiveConversationTitle,
  renderConversationList,
  startNewConversation,
  openConversation,
  safeExternalUrl,
  workspaceMetric,
  workspaceEmpty,
  issueWorkspaceRow,
  renderPersonalOverview,
  renderWeeklyBrief,
  renderTeamBriefBanner,
  briefSpikeRows,
  renderTeamWeeklyBrief,
  renderNotifications,
  renderFavoriteConversations,
  renderTeamDashboard,
  updateWorkspaceUnread,
  renderWorkspace,
  loadMyWorkspace,
  loadTeamWeeklyBrief,
  markTeamWeeklyBriefRead,
  loadTeamDashboard,
  closeWorkspaceModal,
  openWorkspaceModal,
};

export { scopedStorageKey, WORKSPACE_ENDPOINTS } from "./runtime.js";
