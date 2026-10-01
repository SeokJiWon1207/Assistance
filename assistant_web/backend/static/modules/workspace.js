import { state, els, conversationsStorageKeyBase, activeConversationStorageKeyBase, maxStoredConversations, conversationSyncTimers, apiFetch, conversationContentChanged, conversationListSections, conversationTimeLabel, deriveConversationTitle, searchConversations, storageSafeMessages, scopedStorageKey, createConversationRecord, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { renderMessages, escapeHtml, autoResizeInput } from "./chat.js?v=20260911-6";
import { cancelMessageEdit } from "./events.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";

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
const TOAST_DURATION = 2400;
// 실행 취소는 읽고 누를 시간이 필요해 기본 안내보다 길게 띄운다.
const TOAST_ACTION_DURATION = 7000;

function showToast(message, tone = "default", action = null) {
  if (!toastContainer) {
    toastContainer = document.createElement("div");
    toastContainer.className = "toast-stack";
    // 삭제·저장 결과를 스크린리더도 듣도록 상태 영역으로 노출한다.
    toastContainer.setAttribute("role", "status");
    toastContainer.setAttribute("aria-live", "polite");
    document.body.appendChild(toastContainer);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${tone}`;
  const label = document.createElement("span");
  label.textContent = message;
  toast.appendChild(label);
  let hideTimer = null;
  const dismiss = () => {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = null;
    toast.classList.remove("toast-visible");
    setTimeout(() => toast.remove(), 220);
  };
  if (action) {
    const actionButton = document.createElement("button");
    actionButton.type = "button";
    actionButton.className = "toast-action";
    actionButton.textContent = action.label;
    actionButton.addEventListener("click", () => {
      action.onClick();
      dismiss();
    });
    toast.appendChild(actionButton);
  }
  toastContainer.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add("toast-visible"));
  hideTimer = setTimeout(dismiss, action ? TOAST_ACTION_DURATION : TOAST_DURATION);
}

function persistConversations() {
  const active = state.conversations.find((item) => item.id === state.activeConversationId);
  // 대화를 열기만 해도 updatedAt 을 올리면 목록 순서가 바뀌므로 내용 변경 여부를 먼저 판단한다.
  const activeChanged = active ? conversationContentChanged(active.messages, state.messages) : false;
  if (active) {
    active.messages = storageSafeMessages(state.messages);
    active.title = deriveConversationTitle(active.messages);
    if (activeChanged) { active.updatedAt = new Date().toISOString(); active.syncPending = true; }
  }
  state.conversations.sort((a, b) => (
    Number(Boolean(b.favorite)) - Number(Boolean(a.favorite))
    || String(b.updatedAt).localeCompare(String(a.updatedAt))
  ));
  saveLocalConversations();
  if (active && activeChanged) scheduleConversationSync(active);
  renderConversationList();
}

const syncQueues = new Map();

// 목록은 서버에서 받은 대화까지 모두 보여 주고, 기기에는 최근 대화와 동기화 대기 중인 대화만 보관한다.
function saveLocalConversations() {
  const stored = state.conversations.filter((item, index) => item.syncPending || index < maxStoredConversations);
  try {
    localStorage.setItem(conversationsStorageKey(), JSON.stringify(stored));
    localStorage.setItem(activeConversationStorageKey(), state.activeConversationId || "");
  } catch {
    showToast("저장 공간이 부족해 최신 대화를 기기에 보관하지 못했습니다. 창을 닫기 전에 서버 저장을 확인해 주세요.", "warning");
  }
}

function queueConversation(id, operation) {
  const key = `${state.currentUser?.uid}:${id}`;
  const previous = syncQueues.get(key) || Promise.resolve();
  const next = previous.catch(() => {}).then(operation);
  syncQueues.set(key, next);
  void next.finally(() => { if (syncQueues.get(key) === next) syncQueues.delete(key); }).catch(() => {});
  return next;
}

function syncConversation(conversation) {
  const uid = state.currentUser?.uid;
  if (!uid || !conversation?.id) return Promise.resolve();
  return queueConversation(conversation.id, async () => {
    if (state.currentUser?.uid !== uid || !state.conversations.includes(conversation)) return;
    const body = JSON.stringify({
      title: conversation.title || "새 대화",
      messages: storageSafeMessages(conversation.messages || []),
      favorite: Boolean(conversation.favorite),
      createdAt: conversation.createdAt || "",
      expectedUpdatedAt: conversation.serverUpdatedAt || "",
    });
    const response = await apiFetch(`/api/workspace/conversations/${encodeURIComponent(conversation.id)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body,
    });
    if (state.currentUser?.uid !== uid) return;
    if (response.status === 409) {
      // 최신 서버 버전과 충돌한 로컬 변경은 새 대화로 보존한다.
      conversation.id = globalThis.crypto.randomUUID();
      conversation.title = `${conversation.title || "대화"} (복구본)`;
      conversation.serverUpdatedAt = "";
      if (state.activeConversationId && !state.conversations.some((item) => item.id === state.activeConversationId)) {
        state.activeConversationId = conversation.id;
      }
      scheduleConversationSync(conversation);
      showToast("다른 기기의 변경과 겹쳐 현재 기록을 별도 대화로 보존했습니다.", "warning");
      return;
    }
    if (!response.ok) throw new Error("대화 기록을 서버에 저장하지 못했습니다.");
    const saved = (await response.json()).conversation;
    conversation.serverUpdatedAt = saved.updatedAt;
    const sent = JSON.parse(body);
    conversation.syncPending = conversationContentChanged(sent.messages, conversation.messages)
      || sent.title !== conversation.title || sent.favorite !== Boolean(conversation.favorite);
    saveLocalConversations();
  });
}

function scheduleConversationSync(conversation) {
  if (!state.currentUser?.uid || !conversation?.id) return;
  conversation.syncPending = true;
  saveLocalConversations();
  clearTimeout(conversationSyncTimers.get(conversation.id));
  conversationSyncTimers.set(conversation.id, setTimeout(() => {
    conversationSyncTimers.delete(conversation.id);
    syncConversation(conversation).catch(() => {
      showToast("서버 저장에 실패했습니다. 대화는 이 기기에 보관되며 다시 연결하면 저장을 재시도합니다.", "warning");
    });
  }, 350));
}

function deleteConversationFromServer(conversation) {
  const uid = state.currentUser?.uid;
  const id = conversation.id;
  clearTimeout(conversationSyncTimers.get(id));
  conversationSyncTimers.delete(id);
  return queueConversation(id, async () => {
    if (!uid || state.currentUser?.uid !== uid) return;
    const response = await apiFetch(`/api/workspace/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!response.ok) throw new Error("대화 삭제 실패");
    conversation.serverUpdatedAt = "";
  });
}

globalThis.addEventListener?.("online", () => {
  state.conversations.filter((item) => item.syncPending).forEach(scheduleConversationSync);
});

// 서버 목록은 메시지 전체를 담아 무거우므로 처음에는 최근 대화만 받고, 더 필요할 때 최대치까지 받는다.
const INITIAL_CONVERSATION_LIMIT = 40;
const MAX_CONVERSATION_LIMIT = 100;
const VISIBLE_RECENT_CONVERSATIONS = 8;
const conversationListView = { query: "", expanded: false, serverHasMore: false, loadingMore: false };

async function mergeServerConversations(limit, loadingUid) {
  const response = await apiFetch(`/api/workspace/conversations?limit=${limit}`);
  if (!response.ok) throw new Error("대화 목록을 불러오지 못했습니다.");
  const serverItems = (await response.json()).conversations || [];
  if (state.currentUser?.uid !== loadingUid) return false;
  state.conversations = globalThis.AssistantFrontendCore.mergeConversations(state.conversations, serverItems);
  conversationListView.serverHasMore = serverItems.length >= limit && limit < MAX_CONVERSATION_LIMIT;
  state.conversations.filter((item) => item.syncPending || (!item.serverUpdatedAt && item.messages?.length))
    .forEach(scheduleConversationSync);
  saveLocalConversations();
  return true;
}

async function loadConversations() {
  const loadingUid = state.currentUser?.uid;
  try {
    const parsed = JSON.parse(localStorage.getItem(conversationsStorageKey()) || "[]");
    state.conversations = Array.isArray(parsed) ? parsed : [];
  } catch {
    state.conversations = [];
  }
  if (state.currentUser?.uid) {
    try {
      await mergeServerConversations(INITIAL_CONVERSATION_LIMIT, loadingUid);
    } catch {
      // 오프라인 또는 서버 오류 시 사용자별 로컬 기록으로 계속 동작한다.
    }
  }
  if (state.currentUser?.uid !== loadingUid) return;
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

const CONVERSATION_ICONS = {
  favorite: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 16.4l-4.8 2.5.9-5.4-3.9-3.8 5.4-.8z"/></svg>',
  delete: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>',
};

function appendHighlighted(element, text, range) {
  if (!range) {
    element.textContent = text;
    return;
  }
  const mark = document.createElement("mark");
  mark.textContent = text.slice(range[0], range[1]);
  element.append(text.slice(0, range[0]), mark, text.slice(range[1]));
}

function conversationRow(conversation, { titleRange = null, snippet = null } = {}) {
  const title = conversation.title || "새 대화";
  const active = conversation.id === state.activeConversationId;
  const row = document.createElement("div");
  row.className = `conversation-row${active ? " active" : ""}`;
  const openButton = document.createElement("button");
  openButton.type = "button";
  openButton.className = "conversation-open";
  openButton.dataset.conversationId = conversation.id;
  openButton.title = title;
  if (active) openButton.setAttribute("aria-current", "true");
  const titleLine = document.createElement("span");
  titleLine.className = "conversation-title";
  appendHighlighted(titleLine, title, titleRange);
  openButton.appendChild(titleLine);
  if (snippet) {
    const snippetLine = document.createElement("span");
    snippetLine.className = "conversation-snippet";
    appendHighlighted(snippetLine, snippet.text, [snippet.start, snippet.end]);
    openButton.appendChild(snippetLine);
  }
  const time = document.createElement("span");
  time.className = "conversation-time";
  time.textContent = conversationTimeLabel(conversation);
  const actions = document.createElement("span");
  actions.className = "conversation-actions";
  const favoriteButton = document.createElement("button");
  favoriteButton.type = "button";
  favoriteButton.className = `conversation-favorite${conversation.favorite ? " active" : ""}`;
  favoriteButton.dataset.favoriteConversationId = conversation.id;
  favoriteButton.setAttribute("aria-label", conversation.favorite ? `${title} 즐겨찾기 해제` : `${title} 즐겨찾기 추가`);
  favoriteButton.setAttribute("aria-pressed", String(Boolean(conversation.favorite)));
  favoriteButton.title = conversation.favorite ? "즐겨찾기 해제" : "즐겨찾기 추가";
  favoriteButton.innerHTML = CONVERSATION_ICONS.favorite;
  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "conversation-delete";
  deleteButton.dataset.deleteConversationId = conversation.id;
  deleteButton.setAttribute("aria-label", `${title} 삭제`);
  deleteButton.title = "대화 삭제";
  deleteButton.innerHTML = CONVERSATION_ICONS.delete;
  actions.append(favoriteButton, deleteButton);
  row.append(openButton, time, actions);
  return row;
}

function conversationListButton(label, dataName, { disabled = false, expanded = null } = {}) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "conversation-more";
  button.dataset[dataName] = "true";
  button.textContent = label;
  button.disabled = disabled;
  if (expanded !== null) button.setAttribute("aria-expanded", String(expanded));
  return button;
}

function loadMoreButton(label) {
  return conversationListButton(conversationListView.loadingMore ? "불러오는 중…" : label, "conversationLoadMore", {
    disabled: conversationListView.loadingMore,
  });
}

function renderConversationGroups() {
  const { groups, hiddenCount, collapsible } = conversationListSections(state.conversations, {
    limit: VISIBLE_RECENT_CONVERSATIONS,
    expanded: conversationListView.expanded,
  });
  groups.forEach((group, index) => {
    const section = document.createElement("div");
    section.className = "conversation-group";
    section.setAttribute("role", "group");
    const label = document.createElement("div");
    label.className = "conversation-group-label";
    label.id = `conversation-group-${index}`;
    label.textContent = group.label;
    section.setAttribute("aria-labelledby", label.id);
    section.appendChild(label);
    group.items.forEach((conversation) => section.appendChild(conversationRow(conversation)));
    els.conversationList.appendChild(section);
  });
  if (collapsible) {
    const label = conversationListView.expanded ? `최근 ${VISIBLE_RECENT_CONVERSATIONS}개만 보기` : `대화 ${hiddenCount}개 더 보기`;
    els.conversationList.appendChild(conversationListButton(label, "conversationMore", { expanded: conversationListView.expanded }));
  }
  if ((conversationListView.expanded || !collapsible) && conversationListView.serverHasMore) {
    els.conversationList.appendChild(loadMoreButton("이전 대화 더 불러오기"));
  }
}

function renderConversationSearch(query) {
  const results = searchConversations(state.conversations, query);
  results.forEach(({ conversation, titleRange, snippet }) => {
    els.conversationList.appendChild(conversationRow(conversation, { titleRange, snippet }));
  });
  if (!results.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "일치하는 대화가 없습니다. 제목과 질문·답변 내용에서 찾았습니다.";
    els.conversationList.appendChild(empty);
  }
  if (conversationListView.serverHasMore) {
    els.conversationList.appendChild(loadMoreButton("이전 대화까지 찾기"));
  }
  return results.length;
}

function renderConversationList() {
  updateActiveConversationTitle();
  if (!els.conversationList) return;
  els.conversationList.innerHTML = "";
  if (els.conversationCount) els.conversationCount.textContent = `${state.conversations.length}개`;
  const query = conversationListView.query.trim();
  if (!query) {
    if (els.conversationSearchStatus) els.conversationSearchStatus.hidden = true;
    renderConversationGroups();
    return;
  }
  const count = renderConversationSearch(query);
  if (els.conversationSearchStatus) {
    els.conversationSearchStatus.hidden = false;
    els.conversationSearchStatus.textContent = `‘${query}’ 검색 결과 ${count}개`;
  }
}

function setConversationQuery(query) {
  conversationListView.query = String(query || "");
  renderConversationList();
}

function toggleConversationListExpanded() {
  conversationListView.expanded = !conversationListView.expanded;
  renderConversationList();
  els.conversationList.querySelector("[data-conversation-more]")?.focus();
}

async function loadMoreConversations() {
  const uid = state.currentUser?.uid;
  if (!uid || conversationListView.loadingMore) return;
  // 병합 전에 열려 있는 대화의 최신 메시지를 목록에 반영해 둔다.
  persistConversations();
  conversationListView.loadingMore = true;
  renderConversationList();
  try {
    await mergeServerConversations(MAX_CONVERSATION_LIMIT, uid);
    conversationListView.expanded = true;
  } catch {
    showToast("이전 대화를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.", "warning");
  } finally {
    conversationListView.loadingMore = false;
    if (state.currentUser?.uid === uid) renderConversationList();
  }
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

function workspaceMetric(label, value, tone = "", detail = "") {
  return `<article class="workspace-metric ${tone}"><strong>${escapeHtml(String(value ?? 0))}</strong><span>${escapeHtml(label)}</span>${detail ? `<small>${escapeHtml(detail)}</small>` : ""}</article>`;
}

function workspaceEmpty(message) {
  return `<div class="workspace-empty"><span aria-hidden="true">${icon("check", 20)}</span><p>${escapeHtml(message)}</p></div>`;
}

function workspaceStatusLabel(value) {
  const status = String(value || "").trim();
  const normalized = status.toLowerCase().replace(/[\s_-]+/g, "");
  const labels = {
    new: "신규 이슈",
    feedback: "정보 부족",
    acknowledged: "이슈 검토",
    confirmed: "확인된 이슈",
    assigned: "할당된 이슈",
    developed: "개발완료",
    resolved: "해결된 이슈",
    closed: "종료된 이슈",
  };
  return labels[normalized] || status || "상태 미상";
}

function formatWorkspaceDate(value) {
  const raw = String(value || "");
  const match = raw.match(/^\d{4}-(\d{2})-(\d{2})/);
  return match ? `${Number(match[1])}.${Number(match[2])}` : raw;
}

function issueWorkspaceRow(issue) {
  const ageDays = Number(issue.ageDays || 0);
  const aging = ageDays >= 30;
  const status = workspaceStatusLabel(issue.status);
  const rowTone = issue.overdue ? "is-overdue" : aging ? "is-aging" : "";
  const riskLabel = { high: "높은 위험", attention: "주의", watch: "관찰" }[issue.riskLevel] || "";
  return `
    <article class="workspace-item ${rowTone}">
      <div class="workspace-item-main">
        <div class="workspace-item-title">
          <span>#${escapeHtml(String(issue.id || ""))}</span>
          <strong>${escapeHtml(issue.title || "제목 없음")}</strong>
        </div>
        <div class="workspace-item-meta">
          ${issue.project ? `<span class="workspace-meta-project">${escapeHtml(issue.project)}</span>` : ""}
          <span class="workspace-meta-status">${escapeHtml(status)}</span>
          ${issue.dueDate ? `<span class="workspace-meta-due ${issue.overdue ? "is-overdue" : ""}">완료 예정 ${escapeHtml(formatWorkspaceDate(issue.dueDate))}</span>` : ""}
          ${ageDays ? `<span class="workspace-meta-age ${aging ? "is-aging" : ""}">등록 ${ageDays}일</span>` : ""}
          ${riskLabel ? `<span class="workspace-meta-risk is-${escapeHtml(issue.riskLevel)}">${escapeHtml(riskLabel)}</span>` : ""}
        </div>
        ${issue.recommendedAction ? `<p class="workspace-item-recommendation"><strong>권장 조치</strong> ${escapeHtml(issue.recommendedAction)}</p>` : ""}
      </div>
      <button type="button" class="workspace-item-action" aria-label="#${escapeHtml(String(issue.id || ""))} 이슈를 AI에게 묻기" data-workspace-question="${encodeURIComponent(`#${issue.id} 이슈의 현재 상태와 다음 할 일을 요약해줘`)}">AI 요약</button>
    </article>`;
}

function renderPersonalOverview(data) {
  if (data.mantis?.status === "error") {
    return workspaceEmpty(data.mantis.reason || "업무 데이터를 확인하지 못했습니다. 잠시 후 다시 조회해 주세요.");
  }
  const summary = data.summary || {};
  const issues = data.mantis?.issues || [];
  const sourceLabel = data.mantis?.status === "snapshot" ? "저장 데이터" : "Mantis 데이터";
  const coverageLabel = data.mantis?.search_complete === true ? "수집 범위 전체" : "일부 범위 · 전체 건수 미확정";
  const fetchedAtLabel = data.mantis?.fetchedAt ? new Date(data.mantis.fetchedAt).toLocaleString("ko-KR") : "미확인";
  const totalOpen = Number(summary.open || 0);
  const visibleCountLabel = issues.length === totalOpen
    ? `${issues.length}건 모두 표시`
    : `${issues.length} / ${totalOpen}건 표시`;
  const isConnected = Boolean(data.user?.mantisUsername);
  return `<div class="workspace-overview">
    <section class="workspace-summary-grid is-three">
      ${workspaceMetric("미종료", summary.open, summary.open ? "is-blue" : "", data.mantis?.search_complete === true ? "조회 기준 시각의 전체 업무" : "확인한 범위의 업무")}
      ${workspaceMetric("일정 지연", summary.overdue, summary.overdue ? "is-danger" : "", summary.overdue ? "기한이 지난 업무" : "모두 기한 내 진행 중")}
      ${workspaceMetric("30일 이상", summary.aging, summary.aging ? "is-warning" : "", summary.aging ? "우선 확인이 필요한 장기 업무" : "장기 업무 없음")}
    </section>
    <div class="workspace-connection ${(["unlinked", "error"].includes(data.mantis?.status) || data.mantis?.search_complete !== true) ? "is-warning" : ""}">
      <span class="workspace-connection-dot" aria-hidden="true"></span>
      ${isConnected
        ? `<strong>${escapeHtml(sourceLabel)} · ${escapeHtml(coverageLabel)}</strong><span>조회 기준 ${escapeHtml(fetchedAtLabel)}${data.mantis?.refreshPending ? " · 갱신 중" : ""}</span><span>담당자 ${escapeHtml(data.user.mantisUsername)}</span><span>${escapeHtml(visibleCountLabel)}</span>`
        : "<strong>Mantis 담당자명이 연결되지 않았습니다.</strong><span>관리자에게 담당자명 등록을 요청해 주세요.</span>"}
    </div>
    <section class="workspace-section workspace-section-single">
      <header class="workspace-issue-header">
        <div><span>Mantis</span><h4>내 담당 미종료 이슈</h4><p>장기 업무와 완료 예정일을 기준으로 먼저 확인할 항목을 정렬했습니다.</p></div>
        <em class="workspace-visible-count"><strong>${issues.length}</strong>${issues.length === totalOpen ? "건 모두 표시" : ` / ${totalOpen}건 표시`}</em>
      </header>
      <div class="workspace-list workspace-issue-list" role="region" aria-label="내 담당 미종료 이슈 목록" tabindex="0">${issues.length ? issues.map(issueWorkspaceRow).join("") : workspaceEmpty(data.mantis?.search_complete === true ? "조회 기준 시각에 담당한 미종료 이슈가 없습니다." : "확인한 범위에 미종료 이슈가 없습니다. 전체 결과는 미확정입니다.")}</div>
    </section>
  </div>`;
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
      <header><div><h4>이번 주 확인할 항목</h4></div></header>
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
  const decision = brief.decisionSummary || {};
  const riskLabel = { high: "높은 위험", attention: "주의 필요", stable: "안정" }[decision.riskLevel] || "판단 대기";
  const decisionSignals = (decision.signals || []).map((signal) => `<li>${escapeHtml(signal)}</li>`).join("");
  const decisionActions = (decision.recommendedActions || []).map((action) => `
    <button type="button" data-workspace-question="${encodeURIComponent(action.question || action.title || "")}">
      <strong>${escapeHtml(action.title || "권장 조치")}</strong>
      <span>${escapeHtml(action.reason || "")}</span>
      <em>AI로 분석 →</em>
    </button>`).join("");
  return `
    <section class="workspace-brief-hero">
      <span>주간 팀 브리핑</span>
      <h4>${escapeHtml(brief.title || "이번 주 팀 브리핑")}</h4>
      <p>지난주 ${escapeHtml(brief.summaryPeriod || "")} · 생성 ${escapeHtml(String(brief.generatedAt || "").slice(0, 16).replace("T", " "))}</p>
    </section>
    <section class="workspace-summary-grid">
      ${workspaceMetric("지난주 신규", summary.newIssues)}
      ${workspaceMetric("전주 대비", delta, summary.changeRate > 0 ? "is-warning" : "is-blue")}
      ${workspaceMetric("미종료 이월", summary.carriedOpen)}
      ${workspaceMetric("30일 이상", summary.aging, summary.aging ? "is-warning" : "")}
    </section>
    ${decision.headline ? `<section class="workspace-decision is-${escapeHtml(decision.riskLevel || "stable")}">
      <header><span>이번 주 판단</span><em>${escapeHtml(riskLabel)}</em></header>
      <h4>${escapeHtml(decision.headline)}</h4>
      ${decisionSignals ? `<ul>${decisionSignals}</ul>` : ""}
      ${decisionActions ? `<div class="workspace-decision-actions">${decisionActions}</div>` : ""}
    </section>` : ""}
    <p class="workspace-connection ${brief.source?.status === "live" ? "" : "is-warning"}">기준본 · ${brief.source?.status === "live" ? "Mantis 조회 데이터로 생성" : "로컬 스냅샷으로 생성"} · 일정 지연 ${summary.overdue || 0}건 · 담당자 미지정 ${summary.unassigned || 0}건</p>
    <div class="workspace-columns">
      <section class="workspace-section">
        <header><div><h4>급증 고객사</h4></div></header>
        <div class="workspace-insight-list">${briefSpikeRows(brief.customerSpikes || [], "급증한 고객사가 없습니다.")}</div>
      </section>
      <section class="workspace-section">
        <header><div><h4>급증 화면번호</h4></div></header>
        <div class="workspace-insight-list">${briefSpikeRows(brief.screenSpikes || [], "급증한 화면번호가 없습니다.")}</div>
      </section>
    </div>
    <section class="workspace-section workspace-section-single">
      <header><div><h4>이번 주 우선 확인</h4></div><em>${priorities.length}건 표시</em></header>
      <div class="workspace-list">${priorities.length ? priorities.map(issueWorkspaceRow).join("") : workspaceEmpty("우선 확인할 이슈가 없습니다.")}</div>
    </section>
    ${canRefresh ? '<button type="button" class="workspace-primary-action" data-refresh-team-brief>최신 정보로 기준본 재생성</button>' : ""}`;
}

function renderNotifications(data) {
  const notifications = data.notifications || [];
  return `
    <section class="workspace-section workspace-section-single">
      <header><div><h4>업무 알림</h4></div><em>${notifications.filter((item) => !item.read).length}개 안 읽음</em></header>
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
      <header><div><h4>즐겨찾는 대화</h4></div><em>${favorites.length}개</em></header>
      <div class="workspace-list">${favorites.length ? favorites.map((conversation) => `
        <article class="workspace-item">
          <div class="workspace-item-main"><strong>${escapeHtml(conversation.title || "새 대화")}</strong><small>${escapeHtml(String(conversation.updatedAt || "").slice(0, 10))} · ${(conversation.messages || []).length}개 메시지</small></div>
          <button type="button" class="workspace-item-action" data-open-favorite="${escapeHtml(conversation.id)}">대화 열기</button>
        </article>`).join("") : workspaceEmpty("사이드바의 별표를 눌러 자주 보는 대화를 저장해 보세요.")}</div>
    </section>`;
}

function playbookStatusLabel(status) {
  return { draft: "검수 대기", published: "공개", archived: "보관" }[status] || status;
}

function playbookEditorForm() {
  const editor = state.playbookEditor;
  if (!editor || !state.playbooksCanCurate) return "";
  return `
    <form class="playbook-editor" data-playbook-editor data-playbook-id="${escapeHtml(editor.id || "")}">
      <header><div><h4>${editor.id ? "플레이북 편집" : "새 플레이북 작성"}</h4></div></header>
      <label>제목<input name="title" maxlength="200" required value="${escapeHtml(editor.title || "")}" /></label>
      <label>증상<textarea name="symptom" maxlength="4000" required>${escapeHtml(editor.symptom || "")}</textarea></label>
      <label>원인<textarea name="cause" maxlength="4000" placeholder="확인된 원인이 없으면 비워둘 수 있습니다.">${escapeHtml(editor.cause || "")}</textarea></label>
      <label>해결 방법<textarea name="resolution" maxlength="8000" required>${escapeHtml(editor.resolution || "")}</textarea></label>
      <label>검증 방법<textarea name="verification" maxlength="4000" placeholder="조치 후 확인해야 할 내용을 적어주세요.">${escapeHtml(editor.verification || "")}</textarea></label>
      <div class="playbook-editor-row">
        <label>태그<input name="tags" maxlength="500" value="${escapeHtml((editor.tags || []).join(", "))}" placeholder="고객사, 화면번호, 증상" /></label>
        <label>근거 이슈<input name="sourceIssueIds" maxlength="300" value="${escapeHtml((editor.sourceIssueIds || []).map((id) => `#${id}`).join(", "))}" placeholder="#12345, #12346" /></label>
      </div>
      <div class="playbook-editor-actions">
        <button type="button" class="secondary-button" data-cancel-playbook-edit>취소</button>
        <button type="submit" class="workspace-primary-action">${editor.id ? "수정 내용 저장" : "초안 저장"}</button>
      </div>
    </form>`;
}

function playbookCard(playbook) {
  const canCurate = state.playbooksCanCurate;
  const tags = (playbook.tags || []).map((tag) => `<span>${escapeHtml(tag)}</span>`).join("");
  const sourceIds = (playbook.sourceIssueIds || []).map((id) => `#${id}`).join(", ");
  const statusActions = canCurate ? `
    <div class="playbook-card-actions">
      <button type="button" data-edit-playbook="${escapeHtml(playbook.id)}">편집</button>
      ${playbook.status !== "published" ? `<button type="button" data-playbook-status="published" data-playbook-id="${escapeHtml(playbook.id)}">공개</button>` : ""}
      ${playbook.status !== "draft" ? `<button type="button" data-playbook-status="draft" data-playbook-id="${escapeHtml(playbook.id)}">초안으로</button>` : ""}
      ${playbook.status !== "archived" ? `<button type="button" data-playbook-status="archived" data-playbook-id="${escapeHtml(playbook.id)}">보관</button>` : ""}
    </div>` : "";
  return `
    <article class="playbook-card">
      <header>
        <div><span class="playbook-status is-${escapeHtml(playbook.status)}">${escapeHtml(playbookStatusLabel(playbook.status))}</span><h4>${escapeHtml(playbook.title)}</h4></div>
        <small>${escapeHtml(String(playbook.updatedAt || "").slice(0, 10))}</small>
      </header>
      ${tags ? `<div class="playbook-tags">${tags}</div>` : ""}
      <section><strong>증상</strong><p>${escapeHtml(playbook.symptom)}</p></section>
      ${playbook.cause ? `<section><strong>원인</strong><p>${escapeHtml(playbook.cause)}</p></section>` : ""}
      <section><strong>해결 방법</strong><p>${escapeHtml(playbook.resolution)}</p></section>
      ${playbook.verification ? `<section><strong>검증 방법</strong><p>${escapeHtml(playbook.verification)}</p></section>` : ""}
      <footer>
        <span>${sourceIds ? `근거 ${escapeHtml(sourceIds)}` : "근거 이슈 미등록"} · 작성 ${escapeHtml(playbook.createdBy?.name || "알 수 없음")}</span>
        ${sourceIds ? `<button type="button" class="workspace-item-action" data-workspace-question="${encodeURIComponent(`${sourceIds} 이슈를 근거로 이 해결 플레이북의 최신 유효성을 검토해줘`)}">AI에게 검토 요청</button>` : ""}
      </footer>
      ${statusActions}
    </article>`;
}

function renderPlaybooks() {
  const playbooks = state.playbooks || [];
  const canCurate = state.playbooksCanCurate;
  const filter = state.playbookFilter || { q: "", status: "" };
  return `
    ${playbookEditorForm()}
    <section class="playbook-toolbar">
      <form data-playbook-search-form>
        <input name="q" maxlength="200" value="${escapeHtml(filter.q || "")}" placeholder="증상, 고객사, 화면번호, 이슈 번호 검색" aria-label="플레이북 검색" />
        ${canCurate ? `<select name="status" aria-label="플레이북 상태"><option value="">모든 상태</option><option value="draft" ${filter.status === "draft" ? "selected" : ""}>검수 대기</option><option value="published" ${filter.status === "published" ? "selected" : ""}>공개</option><option value="archived" ${filter.status === "archived" ? "selected" : ""}>보관</option></select>` : ""}
        <button type="submit" class="secondary-button">검색</button>
      </form>
      ${canCurate ? '<button type="button" class="workspace-primary-action" data-new-playbook>새 플레이북</button>' : ""}
    </section>
    <p class="workspace-connection">${canCurate ? "검수 대기·공개·보관 플레이북을 모두 관리합니다." : "팀에서 검수해 공개한 해결 플레이북만 표시합니다."} · ${playbooks.length}건</p>
    <div class="playbook-list">${playbooks.length ? playbooks.map(playbookCard).join("") : workspaceEmpty(filter.q ? "검색 조건에 맞는 플레이북이 없습니다." : "아직 공개된 해결 플레이북이 없습니다.")}</div>`;
}

async function loadPlaybooks(force = false) {
  if (force) state.playbooks = null;
  renderWorkspace();
  const filter = state.playbookFilter || { q: "", status: "" };
  const parameters = new URLSearchParams();
  if (filter.q) parameters.set("q", filter.q);
  if (filter.status) parameters.set("status", filter.status);
  if (["manager", "admin"].includes(state.currentUser?.role)) parameters.set("includeDrafts", "true");
  const response = await apiFetch(`${WORKSPACE_ENDPOINTS.playbooks}?${parameters}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "플레이북을 불러오지 못했습니다.");
  state.playbooks = data.playbooks || [];
  state.playbooksCanCurate = Boolean(data.canCurate);
  renderWorkspace();
}

async function savePlaybookCandidate(candidate) {
  const response = await apiFetch(WORKSPACE_ENDPOINTS.playbooks, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...candidate, status: "draft" }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "플레이북 후보를 저장하지 못했습니다.");
  state.playbooks = null;
  return data.playbook;
}

async function savePlaybookEditor(form) {
  const formData = new FormData(form);
  const playbookId = form.dataset.playbookId || "";
  const payload = {
    title: String(formData.get("title") || "").trim(),
    symptom: String(formData.get("symptom") || "").trim(),
    cause: String(formData.get("cause") || "").trim(),
    resolution: String(formData.get("resolution") || "").trim(),
    verification: String(formData.get("verification") || "").trim(),
    tags: String(formData.get("tags") || "").split(",").map((tag) => tag.trim()).filter(Boolean),
    sourceIssueIds: [...String(formData.get("sourceIssueIds") || "").matchAll(/\d+/g)].map((match) => Number(match[0])),
  };
  const response = await apiFetch(
    playbookId ? `${WORKSPACE_ENDPOINTS.playbooks}/${encodeURIComponent(playbookId)}` : WORKSPACE_ENDPOINTS.playbooks,
    {
      method: playbookId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(playbookId ? payload : { ...payload, status: "draft" }),
    },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "플레이북을 저장하지 못했습니다.");
  state.playbookEditor = null;
  await loadPlaybooks(true);
  return data.playbook;
}

async function changePlaybookStatus(playbookId, status) {
  const response = await apiFetch(`${WORKSPACE_ENDPOINTS.playbooks}/${encodeURIComponent(playbookId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "플레이북 상태를 변경하지 못했습니다.");
  await loadPlaybooks(true);
  return data.playbook;
}

function updateWorkspaceUnread() {
  const unread = state.myWorkspace?.summary?.unreadNotifications || 0;
  if (els.workspaceUnreadBadge) {
    els.workspaceUnreadBadge.textContent = String(unread);
    els.workspaceUnreadBadge.hidden = unread < 1;
  }
  if (els.workspaceTabUnread) els.workspaceTabUnread.textContent = unread ? String(unread) : "";
}

let workspaceLoadPromise = null;
let workspaceFreshPromise = null;
let workspaceLoadIsRefresh = false;
let workspaceLoading = false;
let workspaceLoadingMessage = "";
let workspaceLoadingError = "";
let workspaceLastLiveAt = 0;
let workspaceSyncTimer = null;
const workspaceLiveTtlMs = 60_000;

function updateWorkspaceLoadingControl() {
  if (!els.workspaceRefreshBtn) return;
  const loading = workspaceLoading || Boolean(workspaceFreshPromise);
  els.workspaceRefreshBtn.disabled = loading;
  els.workspaceRefreshBtn.textContent = loading ? "불러오는 중…" : "새로고침";
  els.workspaceRefreshBtn.setAttribute("aria-busy", String(loading));
}

function renderWorkspace() {
  if (!els.workspaceContent) return;
  const loading = workspaceLoading || Boolean(workspaceFreshPromise);
  els.workspaceContent.setAttribute("aria-busy", String(loading));
  const isPersonalOverview = state.workspaceTab === "overview";
  els.workspaceContent.classList.toggle("is-overview", isPersonalOverview);
  els.workspaceContent.classList.toggle("has-status", !loading && Boolean(state.myWorkspace) && Boolean(workspaceLoadingError));
  els.workspaceModalEyebrow.textContent = "개인 워크스페이스";
  els.workspaceModalTitle.textContent = "내 업무";
  els.workspaceTabs.hidden = false;
  els.workspaceTabs.querySelectorAll("[data-workspace-tab]").forEach((button) => {
    const selected = button.dataset.workspaceTab === state.workspaceTab;
    button.classList.toggle("active", selected);
    // 선택 상태를 클래스뿐 아니라 보조 기술에도 알리고, 탭 목록 안에서는
    // 활성 탭 하나만 Tab 으로 닿도록 roving tabindex 를 유지한다.
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected && button.id) els.workspaceContent.setAttribute("aria-labelledby", button.id);
  });
  if (loading) {
    els.workspaceContent.innerHTML = `<div class="workspace-loading" role="status"><span class="workspace-spinner" aria-hidden="true"></span><strong>${escapeHtml(workspaceLoadingMessage || "업무 정보를 불러오는 중입니다.")}</strong></div>`;
    return;
  }
  if (!state.myWorkspace) {
    els.workspaceContent.innerHTML = workspaceLoadingError
      ? `<div class="workspace-error" role="alert"><strong>업무 정보를 불러오지 못했습니다.</strong><p>${escapeHtml(workspaceLoadingError)}</p></div>`
      : '<div class="workspace-loading" role="status">업무 정보를 불러오는 중입니다.</div>';
    return;
  }
  const renderers = {
    overview: () => renderPersonalOverview(state.myWorkspace),
    brief: () => renderWeeklyBrief(state.myWorkspace),
    playbooks: renderPlaybooks,
    notifications: () => renderNotifications(state.myWorkspace),
    favorites: renderFavoriteConversations,
  };
  const loadingStatus = workspaceLoadingError
      ? `<div class="workspace-refresh-status is-error" role="status"><strong>${escapeHtml(workspaceLoadingError)}</strong></div>`
      : "";
  els.workspaceContent.innerHTML = `${loadingStatus}${(renderers[state.workspaceTab] || renderers.overview)()}`;
  updateWorkspaceUnread();
}

async function loadMyWorkspace(force = false, revalidate = false, background = false) {
  if (workspaceLoadPromise) {
    const awaitedRefresh = workspaceLoadIsRefresh;
    await workspaceLoadPromise;
    if (force && !awaitedRefresh) return loadMyWorkspace(true);
    return state.myWorkspace;
  }
  if (state.myWorkspace && !force && !revalidate) return state.myWorkspace;

  workspaceLoadIsRefresh = force;
  workspaceLoading = !background;
  workspaceLoadingError = "";
  workspaceLoadingMessage = force ? "Mantis 최신 데이터를 확인하고 있습니다." : "업무 정보를 불러오는 중입니다.";
  updateWorkspaceLoadingControl();
  renderWorkspace();
  workspaceLoadPromise = (async () => {
    const endpoint = force ? `${WORKSPACE_ENDPOINTS.personal}?refresh=true` : `${WORKSPACE_ENDPOINTS.personal}?live=true`;
    const response = await apiFetch(endpoint);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "개인 업무를 불러오지 못했습니다.");
    state.myWorkspace = data;
    workspaceLastLiveAt = Date.parse(data.mantis?.fetchedAt || "") || 0;
    if (workspaceSyncTimer) clearTimeout(workspaceSyncTimer);
    workspaceSyncTimer = data.mantis?.refreshPending ? setTimeout(() => {
      workspaceSyncTimer = null;
      if (state.currentUser?.uid) void loadMyWorkspace(false, true, true).catch(() => {});
    }, 10000) : null;
    updateWorkspaceUnread();
    return data;
  })();
  try {
    return await workspaceLoadPromise;
  } catch (error) {
    workspaceLoadingError = error.message || "최신 업무 데이터를 확인하지 못했습니다.";
    throw error;
  } finally {
    workspaceLoadPromise = null;
    workspaceLoadIsRefresh = false;
    workspaceLoading = false;
    workspaceLoadingMessage = "";
    updateWorkspaceLoadingControl();
    renderWorkspace();
  }
}

async function ensureMyWorkspaceFresh() {
  if (workspaceFreshPromise) return workspaceFreshPromise;
  const meta = state.myWorkspace?.mantis;
  const sourceCanBeReused = ["live", "snapshot"].includes(meta?.status);
  const expiresAt = Date.parse(meta?.cacheUntil || "") || workspaceLastLiveAt + workspaceLiveTtlMs;
  const liveDataIsRecent = sourceCanBeReused && workspaceLastLiveAt > 0
    && Date.now() >= workspaceLastLiveAt && Date.now() < expiresAt && !meta?.refreshPending;
  if (!workspaceLoadPromise && state.myWorkspace && (liveDataIsRecent || !state.myWorkspace.user?.mantisUsername)) {
    renderWorkspace();
    return state.myWorkspace;
  }
  // 최초 진입도 실시간 조회 한 번으로 끝내며 원본 캐시는 서버에서 공유한다.
  workspaceFreshPromise = Promise.resolve().then(async () => {
    if (workspaceLoadPromise) await workspaceLoadPromise;
    else await loadMyWorkspace(false, true);
    return state.myWorkspace;
  });
  updateWorkspaceLoadingControl();
  renderWorkspace();
  try {
    return await workspaceFreshPromise;
  } finally {
    workspaceFreshPromise = null;
    updateWorkspaceLoadingControl();
    renderWorkspace();
  }
}

async function loadTeamWeeklyBrief(refresh = false) {
  if (refresh) state.teamWeeklyBrief = null;
  const response = await apiFetch(
    refresh ? WORKSPACE_ENDPOINTS.teamBriefRefresh : WORKSPACE_ENDPOINTS.teamBrief,
    refresh ? { method: "POST" } : undefined,
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "팀 브리핑을 불러오지 못했습니다.");
  state.teamWeeklyBrief = data;
}

async function markTeamWeeklyBriefRead() {
  if (!state.teamWeeklyBrief || state.teamWeeklyBrief.viewed) return;
  const response = await apiFetch(WORKSPACE_ENDPOINTS.teamBriefRead, { method: "POST" });
  if (!response.ok) throw new Error("팀 브리핑 확인 상태를 저장하지 못했습니다.");
  state.teamWeeklyBrief.viewed = true;
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
  deleteConversationFromServer,
  scheduleConversationSync,
  loadConversations,
  updateActiveConversationTitle,
  renderConversationList,
  setConversationQuery,
  toggleConversationListExpanded,
  loadMoreConversations,
  startNewConversation,
  openConversation,
  safeExternalUrl,
  workspaceMetric,
  workspaceEmpty,
  workspaceStatusLabel,
  issueWorkspaceRow,
  renderPersonalOverview,
  renderWeeklyBrief,
  briefSpikeRows,
  renderTeamWeeklyBrief,
  renderNotifications,
  renderFavoriteConversations,
  playbookStatusLabel,
  playbookEditorForm,
  playbookCard,
  renderPlaybooks,
  loadPlaybooks,
  savePlaybookCandidate,
  savePlaybookEditor,
  changePlaybookStatus,
  updateWorkspaceUnread,
  renderWorkspace,
  loadMyWorkspace,
  ensureMyWorkspaceFresh,
  loadTeamWeeklyBrief,
  markTeamWeeklyBriefRead,
};

export { scopedStorageKey, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
