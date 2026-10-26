import { chartHost, mountCharts, releaseCharts } from "./bklit_charts.js?v=20261026-1";
import { state, els, conversationsStorageKeyBase, activeConversationStorageKeyBase, maxStoredConversations, conversationSyncTimers, apiFetch, conversationContentChanged, conversationListSections, conversationTimeLabel, deriveConversationTitle, searchConversations, storageSafeMessages, scopedStorageKey, createConversationRecord, WORKSPACE_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { renderMessages, escapeHtml, autoResizeInput } from "./chat.js?v=20260911-6";
import { cancelMessageEdit } from "./events.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { formatUiDateTime, customerName, customerLabelText } from "./ui_format.js?v=20260911-6";
import { dataBadgeState, formatBadgeTime, mantisBadgeDetails, renderDataBadge } from "./data_badge.js?v=20260911-6";
import { captureLayout, animateLayoutChange } from "./motion.js?v=20260911-6";

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
  if (state.conversationDeleting) return;
  const active = state.conversations.find((item) => item.id === state.activeConversationId);
  // 대화를 열기만 해도 updatedAt 을 올리면 목록 순서가 바뀌므로 내용 변경 여부를 먼저 판단한다.
  const activeChanged = active ? conversationContentChanged(active.messages, state.messages) : false;
  if (active) {
    active.messages = storageSafeMessages(state.messages);
    if (!(active.messageOffset > 0)) active.title = deriveConversationTitle(active.messages);
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
const syncedMessageState = new Map();
let conversationCacheWrite = Promise.resolve();
let conversationMutation = 0;

// 목록은 서버에서 받은 대화까지 모두 보여 주고, 기기에는 최근 대화와 동기화 대기 중인 대화만 보관한다.
function saveLocalConversations() {
  const stored = state.conversations.filter((item, index) => item.syncPending || index < maxStoredConversations);
  const key = conversationsStorageKey();
  const durable = conversationCacheWrite = conversationCacheWrite.catch(() => {}).then(() =>
    globalThis.AssistantFrontendCore.conversationCache(key, stored)).catch(() => false);
  try {
    localStorage.setItem(key, JSON.stringify(stored));
    localStorage.setItem(activeConversationStorageKey(), state.activeConversationId || "");
  } catch {
    void durable.then((saved) => {
      if (!saved) showToast("저장 공간이 부족해 최신 대화를 기기에 보관하지 못했습니다. 창을 닫기 전에 서버 저장을 확인해 주세요.", "warning");
    });
  }
  return durable;
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
    const messages = storageSafeMessages(conversation.messages || []);
    const offset = Number(conversation.messageOffset || 0);
    const snapshot = syncedMessageState.get(conversation.id);
    let changed = 0;
    if (snapshot && snapshot.version === conversation.serverUpdatedAt && snapshot.offset === offset) {
      while (changed < Math.min(messages.length, snapshot.messages.length)
        && JSON.stringify(messages[changed]) === JSON.stringify(snapshot.messages[changed])) changed += 1;
    }
    const batches = globalThis.AssistantFrontendCore.conversationMessageBatches(messages.slice(changed));
    let start = offset + changed;
    const title = conversation.title || "새 대화", favorite = Boolean(conversation.favorite);
    for (let index = 0; index < batches.length; index += 1) {
      const response = await apiFetch(`/api/workspace/conversations/${encodeURIComponent(conversation.id)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
          title, messages: batches[index], favorite, createdAt: conversation.createdAt || "",
          expectedUpdatedAt: conversation.serverUpdatedAt || "", startIndex: start, truncate: index === batches.length - 1,
        }),
      });
      if (state.currentUser?.uid !== uid) return;
      if (response.status === 410) {
        // 삭제는 버전 충돌과 다르다. 복구본을 만들거나 재시도해 삭제한 내용을 되살리지 않는다.
        conversationMutation += 1;
        clearTimeout(conversationSyncTimers.get(conversation.id));
        conversationSyncTimers.delete(conversation.id);
        syncedMessageState.delete(conversation.id);
        state.conversations = state.conversations.filter((item) => item.id !== conversation.id);
        if (!state.conversations.length) state.conversations.push(createConversation());
        if (state.activeConversationId === conversation.id) {
          state.activeConversationId = state.conversations[0].id;
          state.messages = [...(state.conversations[0].messages || [])];
          cancelMessageEdit();
          renderMessages();
        }
        await saveLocalConversations();
        renderConversationList();
        showToast("다른 창이나 기기에서 삭제한 대화를 목록에서 제거했습니다.");
        return;
      }
      if (response.status === 409) {
        // 서버 기록은 건드리지 않고 현재 기기의 변경 부분을 복구본으로 보존한다.
        conversation.id = globalThis.crypto.randomUUID();
        conversation.title = `${conversation.title || "대화"} (복구본)`;
        conversation.serverUpdatedAt = "";
        conversation.messageOffset = 0;
        if (state.activeConversationId && !state.conversations.some((item) => item.id === state.activeConversationId)) {
          state.activeConversationId = conversation.id;
        }
        scheduleConversationSync(conversation);
        showToast("다른 기기의 변경과 겹쳐 현재 기기에서 읽은 기록을 복구본으로 보존했습니다. 이전 기록은 원래 대화에 남아 있습니다.", "warning");
        return;
      }
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        throw new Error(payload.detail || "대화 기록을 서버에 저장하지 못했습니다. 기존 기록은 보존됩니다.");
      }
      const saved = (await response.json()).conversation;
      conversation.serverUpdatedAt = saved.updatedAt;
      conversation.messageCount = saved.messageCount ?? offset + messages.length;
      start += batches[index].length;
    }
    syncedMessageState.set(conversation.id, { offset, messages, version: conversation.serverUpdatedAt });
    conversation.syncPending = conversationContentChanged(messages, conversation.messages)
      || title !== conversation.title || favorite !== Boolean(conversation.favorite);
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

async function bulkDeleteConversations({ ids = [], all = false } = {}) {
  if (state.busy || state.conversationDeleting) throw new Error("진행 중인 작업이 끝난 뒤 삭제해 주세요.");
  const uid = state.currentUser?.uid;
  if (!uid || (!all && !ids.length)) throw new Error("삭제할 대화를 선택해 주세요.");
  const selected = new Set(ids);
  const previous = state.conversations;
  const previousActive = state.activeConversationId;
  const previousMessages = state.messages;
  const targets = previous.filter((item) => all || selected.has(item.id));
  state.conversationDeleting = true;
  conversationMutation += 1;
  // 큐에 남은 저장은 목록에서 제외된 항목을 건너뛴다. 이미 시작한 저장은 삭제보다 먼저 끝낸다.
  state.conversations = previous.filter((item) => !all && !selected.has(item.id));
  for (const item of targets) {
    clearTimeout(conversationSyncTimers.get(item.id));
    conversationSyncTimers.delete(item.id);
  }
  try {
    await Promise.allSettled(targets.map((item) => syncQueues.get(`${uid}:${item.id}`)).filter(Boolean));
    if (state.currentUser?.uid !== uid) return null;
    const response = await apiFetch("/api/workspace/conversations/bulk-delete", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: all ? [] : ids, all }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "대화를 삭제하지 못했습니다. 다시 시도해 주세요.");
    if (state.currentUser?.uid !== uid) return null;
    targets.forEach((item) => syncedMessageState.delete(item.id));
    if (!state.conversations.length) state.conversations.push(createConversation());
    if (!state.conversations.some((item) => item.id === previousActive)) {
      state.activeConversationId = state.conversations[0].id;
      state.messages = [...(state.conversations[0].messages || [])];
      cancelMessageEdit();
    }
    state.selectedConversationIds?.clear();
    state.conversationSelectionMode = false;
    if (all) {
      conversationListView.query = "";
      conversationListView.serverHasMore = false;
      conversationListView.expanded = false;
      if (els.conversationSearch) els.conversationSearch.value = "";
    }
    await saveLocalConversations();
    return { ...data, localDeletedCount: targets.length };
  } catch (error) {
    if (state.currentUser?.uid === uid) {
      state.conversations = previous;
      state.activeConversationId = previousActive;
      state.messages = previousMessages;
      await saveLocalConversations();
    }
    throw error;
  } finally {
    if (state.currentUser?.uid === uid) {
      state.conversationDeleting = false;
      renderConversationList();
      renderMessages();
    }
  }
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
  if (state.conversationDeleting) return false;
  const generation = conversationMutation;
  const response = await apiFetch(`/api/workspace/conversations?limit=${limit}`);
  if (!response.ok) throw new Error("대화 목록을 불러오지 못했습니다.");
  const data = await response.json();
  const serverItems = data.conversations || [];
  if (state.currentUser?.uid !== loadingUid || conversationMutation !== generation) return false;
  if (Array.isArray(data.conversationIds)) {
    const existing = new Set(data.conversationIds);
    // 서버에 저장한 적 있는 항목만 정리한다. 미저장 새 대화와 다음 페이지의 기록은 유지한다.
    state.conversations = state.conversations.filter((item) => !item.serverUpdatedAt || existing.has(item.id));
  }
  serverItems.forEach((item) => syncedMessageState.set(item.id, {
    offset: Number(item.messageOffset || 0), messages: storageSafeMessages(item.messages || []), version: item.updatedAt,
  }));
  state.conversations = globalThis.AssistantFrontendCore.mergeConversations(state.conversations, serverItems);
  conversationListView.serverHasMore = serverItems.length >= limit && limit < MAX_CONVERSATION_LIMIT;
  state.conversations.filter((item) => item.syncPending || (!item.serverUpdatedAt && item.messages?.length))
    .forEach(scheduleConversationSync);
  saveLocalConversations();
  return true;
}

async function loadConversations() {
  const loadingUid = state.currentUser?.uid;
  state.conversationDeleting = false;
  state.conversationSelectionMode = false;
  state.selectedConversationIds?.clear();
  try {
    const parsed = JSON.parse(localStorage.getItem(conversationsStorageKey()) || "[]");
    state.conversations = Array.isArray(parsed) ? parsed : [];
  } catch {
    state.conversations = [];
  }
  await conversationCacheWrite;
  const cached = await globalThis.AssistantFrontendCore.conversationCache(conversationsStorageKey()).catch(() => null);
  if (state.currentUser?.uid !== loadingUid) return;
  if (Array.isArray(cached)) {
    const records = new Map(state.conversations.map((item) => [item.id, item]));
    cached.forEach((item) => {
      const local = records.get(item.id);
      if (!local || String(item.updatedAt || "") > String(local.updatedAt || "")
          || (item.updatedAt === local.updatedAt && (item.messages?.length || 0) > (local.messages?.length || 0))) records.set(item.id, item);
    });
    state.conversations = [...records.values()];
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
  row.dataset.layoutKey = conversation.id;
  if (state.conversationSelectionMode) {
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "conversation-selection";
    checkbox.dataset.selectConversationId = conversation.id;
    checkbox.checked = state.selectedConversationIds.has(conversation.id);
    checkbox.disabled = state.conversationDeleting;
    checkbox.setAttribute("aria-label", `${title} 선택`);
    row.appendChild(checkbox);
    row.classList.add("is-selecting");
  }
  const openButton = document.createElement("button");
  openButton.type = "button";
  openButton.className = "conversation-open";
  openButton.dataset.conversationId = conversation.id;
  openButton.title = title;
  openButton.disabled = state.conversationDeleting;
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

const CONVERSATION_LAYOUT_SELECTOR = ".conversation-row[data-layout-key]";

function renderConversationList() {
  updateActiveConversationTitle();
  if (!els.conversationList) return;
  // 정렬·즐겨찾기·삭제·검색으로 순서가 바뀌면 행이 새 자리로 미끄러지게 한다.
  const previousLayout = captureLayout(els.conversationList, CONVERSATION_LAYOUT_SELECTOR);
  els.conversationList.innerHTML = "";
  if (els.conversationCount) els.conversationCount.textContent = `${state.conversations.length}개`;
  const query = conversationListView.query.trim();
  if (!query) {
    if (els.conversationSearchStatus) els.conversationSearchStatus.hidden = true;
    renderConversationGroups();
  } else {
    const count = renderConversationSearch(query);
    if (els.conversationSearchStatus) {
      els.conversationSearchStatus.hidden = false;
      els.conversationSearchStatus.textContent = `‘${query}’ 검색 결과 ${count}개`;
    }
  }
  animateLayoutChange(els.conversationList, CONVERSATION_LAYOUT_SELECTOR, previousLayout);
  globalThis.dispatchEvent?.(new Event("conversations:rendered"));
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
  if (state.conversationDeleting) return;
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
  if (state.busy || state.conversationDeleting || conversationId === state.activeConversationId) return;
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

async function loadEarlierMessages() {
  if (state.busy) return;
  const conversation = state.conversations.find((item) => item.id === state.activeConversationId);
  if (!conversation || !(conversation.messageOffset > 0)) return;
  try {
    const response = await apiFetch(`/api/workspace/conversations/${encodeURIComponent(conversation.id)}?before=${conversation.messageOffset}&limit=60`);
    if (!response.ok) throw new Error("이전 대화 기록을 불러오지 못했습니다.");
    const page = (await response.json()).conversation;
    if (page.updatedAt !== conversation.serverUpdatedAt) throw new Error("다른 기기에서 대화가 바뀌었습니다. 현재 기록을 저장한 뒤 다시 불러와 주세요.");
    if (state.activeConversationId !== conversation.id) return;
    const oldHeight = els.messages?.scrollHeight || 0;
    conversation.messages = [...page.messages, ...storageSafeMessages(state.messages)];
    conversation.messageOffset = page.messageOffset;
    state.messages = [...conversation.messages];
    const snapshot = syncedMessageState.get(conversation.id);
    if (snapshot) syncedMessageState.set(conversation.id, { ...snapshot, offset: page.messageOffset, messages: [...page.messages, ...snapshot.messages] });
    saveLocalConversations();
    renderMessages();
    if (els.messages) els.messages.scrollTop = Math.max(0, els.messages.scrollHeight - oldHeight);
  } catch (error) {
    showToast(error.message, "warning");
  }
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

function workspaceEmpty(message, complete = true) {
  return `<div class="workspace-empty${complete ? "" : " is-warning"}"><span aria-hidden="true">${icon(complete ? "check" : "warning", 20)}</span><p>${escapeHtml(message)}</p></div>`;
}

function workspaceFailure(message) {
  return `<div class="workspace-error" role="alert"><span aria-hidden="true">${icon("warning", 24)}</span><strong>업무 정보를 불러오지 못했어요.</strong><p>${escapeHtml(message)}</p><button type="button" class="workspace-item-action" data-workspace-retry>다시 조회</button></div>`;
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

// 완료 예정일까지 남은(지난) 날을 오른쪽 칸 한 줄로 보여 준다. 예정일이 없으면 등록 경과일을 쓴다.
function workspaceIssueIsOpen(issue) {
  if (typeof issue.isOpen === "boolean") return issue.isOpen;
  // 이전 저장본도 서버에서 쓰는 상태 표시명으로 판별한다. 미상은 진행 중으로 추정하지 않는다.
  return ["신규 이슈", "정보 부족", "이슈 검토", "확인된 이슈", "할당된 이슈", "개발완료"].includes(workspaceStatusLabel(issue.status));
}

function issueDueLabel(issue, ageDays) {
  if (!workspaceIssueIsOpen(issue)) {
    return issue.dueDate ? { main: formatWorkspaceDate(issue.dueDate), sub: "기록된 예정일", tone: "" }
      : ageDays ? { main: `등록 ${ageDays}일`, sub: "예정일 없음", tone: "" } : null;
  }
  const match = String(issue.dueDate || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (match) {
    const due = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((today - due) / 86400000);
    const main = days > 0 ? `${days}일 지남` : days === 0 ? "오늘까지" : `${-days}일 남음`;
    return { main, sub: `${formatWorkspaceDate(issue.dueDate)} 예정`, tone: issue.overdue ? "is-overdue" : "" };
  }
  return ageDays ? { main: `${ageDays}일째`, sub: "예정일 없음", tone: ageDays >= 30 ? "is-aging" : "" } : null;
}

function issueWorkspaceRow(issue) {
  const ageDays = Number(issue.ageDays || 0);
  const aging = workspaceIssueIsOpen(issue) && ageDays >= 30;
  const status = workspaceStatusLabel(issue.status);
  const rowTone = workspaceIssueIsOpen(issue) && issue.overdue ? "is-overdue" : aging ? "is-aging" : "";
  const riskLabel = { high: "높은 위험", attention: "주의", watch: "관찰" }[issue.riskLevel] || "";
  const due = issueDueLabel(issue, ageDays);
  const meta = [customerName(issue), status, issue.dueDate && ageDays ? `등록 ${ageDays}일` : ""].filter(Boolean)
    .map((item) => escapeHtml(item)).join(" · ");
  return `
    <article class="workspace-item workspace-issue-row ${rowTone}">
      <span class="workspace-item-id">#${escapeHtml(String(issue.id || ""))}</span>
      <div class="workspace-item-main">
        <strong class="workspace-item-name">${escapeHtml(issue.title || "제목 없음")}</strong>
        <span class="workspace-item-meta">${meta}${riskLabel ? ` · <b class="workspace-meta-risk is-${escapeHtml(issue.riskLevel)}">${escapeHtml(riskLabel)}</b>` : ""}</span>
        ${issue.recommendedAction ? `<p class="workspace-item-recommendation"><span class="sr-only">권장 조치</span>→ ${escapeHtml(issue.recommendedAction)}</p>` : ""}
      </div>
      <span class="workspace-item-due ${due?.tone || ""}">${due ? `<b>${escapeHtml(due.main)}</b><small>${escapeHtml(due.sub)}</small>` : ""}</span>
      <button type="button" class="workspace-item-action" aria-label="#${escapeHtml(String(issue.id || ""))} 이슈를 AI에게 묻기" data-workspace-question="${encodeURIComponent(`#${issue.id} 이슈의 현재 상태와 다음 할 일을 요약해줘`)}">${icon("chat", 15)}AI 요약</button>
    </article>`;
}

// 내 담당 목록은 기한 지남 → 30일 이상 → 그 외로 묶어 보여 준다. 묶음 안의 순서는 서버 정렬을 그대로 따른다.
function groupedIssueRows(issues) {
  const groups = [
    ["is-overdue", "기한 지남", issues.filter((issue) => issue.overdue)],
    ["is-aging", "30일 이상", issues.filter((issue) => !issue.overdue && Number(issue.ageDays || 0) >= 30)],
    ["", "그 외", issues.filter((issue) => !issue.overdue && Number(issue.ageDays || 0) < 30)],
  ].filter(([, , items]) => items.length);
  // 묶음이 하나뿐이면 제목 없이 목록만 둔다.
  if (groups.length < 2) return issues.map(issueWorkspaceRow).join("");
  return groups.map(([tone, label, items]) => `
    <p class="workspace-group-label ${tone}">${label} · ${items.length}</p>
    ${items.map(issueWorkspaceRow).join("")}`).join("");
}

function renderPersonalOverview(data) {
  if (data.mantis?.status === "error") {
    return workspaceFailure(data.mantis.reason || "잠시 후 다시 조회해 주세요.");
  }
  const summary = data.summary || {};
  const allIssues = [...(data.mantis?.issues || [])];
  if (state.workspaceSort === "oldest") allIssues.sort((a, b) => Number(b.ageDays || 0) - Number(a.ageDays || 0));
  if (state.workspaceSort === "newest") allIssues.sort((a, b) => Number(a.ageDays || 0) - Number(b.ageDays || 0));
  const issues = state.workspaceIssuesExpanded ? allIssues : allIssues.slice(0, 8);
  const totalOpen = Number(summary.open || 0);
  const isConnected = Boolean(data.user?.mantisUsername);
  const complete = data.mantis?.search_complete === true;
  const count = (value) => `${Number(value || 0)}건${complete ? "" : " 이상"}`;
  // 출처·범위·조회 시각은 머리의 '데이터 기준' 배지가 보여 준다. 본문에는 확인이 필요할 때만 한 줄을 둔다.
  const notice = !isConnected
    ? `<div class="workspace-connection is-warning"><span class="workspace-connection-dot" aria-hidden="true"></span><strong>Mantis 담당자명이 연결되지 않았습니다.</strong><span>관리자에게 담당자명 등록을 요청해 주세요.</span></div>`
    : !complete
      ? `<div class="workspace-connection is-warning"><span class="workspace-connection-dot" aria-hidden="true"></span><strong>일부 이슈만 확인했어요.</strong><span>건수는 확인한 범위 기준이라 실제보다 적을 수 있어요.</span></div>`
      : "";
  return `<div class="workspace-overview">
    <section class="workspace-summary-grid is-three">
      ${workspaceMetric("미종료", count(summary.open), summary.open ? "is-blue" : "", complete ? "내 담당 업무" : "확인한 범위 기준")}
      ${workspaceMetric("일정 지연", count(summary.overdue), summary.overdue ? "is-danger" : "", !complete ? "확인한 범위 기준" : summary.overdue ? "기한이 지난 업무" : "모두 기한 내 진행 중")}
      ${workspaceMetric("30일 이상", count(summary.aging), summary.aging ? "is-warning" : "", !complete ? "확인한 범위 기준" : summary.aging ? "우선 확인이 필요한 장기 업무" : "장기 업무 없음")}
    </section>
    ${notice}
    <section class="workspace-section workspace-section-single">
      <header class="workspace-issue-header">
        <div><h3>내 담당 미종료 이슈</h3><p>완료 예정일과 등록 경과일을 확인하고 다음 조치를 준비하세요.</p></div>
        <div class="workspace-list-controls"><em class="workspace-visible-count"><strong>${issues.length}</strong>${!complete ? `건 표시 · ${allIssues.length}건 이상 확인` : issues.length === totalOpen ? "건 모두 표시" : ` / ${totalOpen}건 표시`}</em>
        <select aria-label="업무 정렬" data-workspace-sort><option value="urgent">긴급도순</option><option value="oldest" ${state.workspaceSort === "oldest" ? "selected" : ""}>오래된순</option><option value="newest" ${state.workspaceSort === "newest" ? "selected" : ""}>최근순</option></select></div>
      </header>
      <div class="workspace-list workspace-issue-list" role="region" aria-label="내 담당 미종료 이슈 목록" tabindex="0">${issues.length ? state.workspaceSort && state.workspaceSort !== "urgent" ? issues.map(issueWorkspaceRow).join("") : groupedIssueRows(issues) : workspaceEmpty(complete ? "담당한 미종료 이슈가 없어요." : "확인한 범위에는 미종료 이슈가 없어요. 일부만 확인한 결과예요.", complete)}</div>
    </section>
    ${allIssues.length > 8 ? `<button type="button" class="workspace-show-more" data-workspace-more aria-expanded="${Boolean(state.workspaceIssuesExpanded)}">${state.workspaceIssuesExpanded ? "간략히 보기" : `확인한 업무 ${allIssues.length}건 모두 보기`}</button>` : ""}
    ${renderOverviewSide(data)}
  </div>`;
}

// 넓은 화면의 업무 탭 오른쪽 칸: 다른 탭(주간 보고·알림)을 열지 않아도 이번 주 흐름을 볼 수 있게 요약만 둔다.
function renderOverviewSide(data) {
  const brief = data.weeklyBrief || {};
  const notifications = (data.notifications || []).slice(0, 3);
  const partial = data.mantis?.search_complete !== true;
  const stat = (value, label) => `<div><strong>${escapeHtml(`${value ?? 0}${partial ? "건 이상" : ""}`)}</strong><span>${label}</span></div>`;
  return `
    <aside class="workspace-side" aria-label="이번 주 요약">
      <section class="workspace-side-card">
        <header><h3>이번 주 브리핑</h3>${teamBriefUnread() ? '<span class="workspace-side-badge">새 팀 브리핑</span>' : ""}</header>
        <div class="workspace-side-stats${partial ? " is-partial" : ""}">${stat(brief.newIssues, "신규")}${stat(brief.updatedIssues, "변경")}${stat(brief.overdueIssues, "지연")}</div>
        ${chartHost({kind: "sparkline", title: "최근 7일 신규 추이", complete: !partial, points: brief.intakeTrend || []}, `<p>최근 7일 신규 ${Number(brief.newIssues || 0)}건${partial ? " 이상" : ""}</p>`)}
        <button type="button" class="workspace-primary-action" data-workspace-question="${encodeURIComponent("내 담당 Mantis 이슈를 기준으로 이번 주 업무보고서를 작성해줘")}">AI로 주간 보고서 작성</button>
        <button type="button" class="workspace-side-link" data-workspace-go="brief">주간 보고 열기 ›</button>
      </section>
      <section class="workspace-side-card"><header><h3>앞으로 28일 마감 일정</h3></header>
        ${chartHost({kind: "calendar", title: "완료예정일 달력", complete: !partial, today: brief.periodTo || "", issues: (data.mantis?.issues || []).map(({id, title, dueDate}) => ({id, title, dueDate}))}, '<p>마감 일정은 업무 목록의 완료예정일에서 확인할 수 있습니다.</p>')}
      </section>
      <section class="workspace-side-card">
        <header><h3>최근 알림</h3><button type="button" class="workspace-side-link" data-workspace-go="notifications">모두 보기</button></header>
        ${notifications.length ? `<ul class="workspace-side-notices">${notifications.map((item) => `
          <li class="${item.read ? "is-read" : ""}"><span aria-hidden="true"></span><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(notificationDate(item))}</small></div></li>`).join("")}</ul>`
          : `<p class="workspace-side-empty">${escapeHtml(notificationEmptyText(data))}</p>`}
        ${data.notificationScope?.status === "unavailable" ? `<p class="workspace-side-empty">${escapeHtml(data.notificationScope.message || "배정·노트 변경 수집이 보류되었습니다.")}</p>` : ""}
      </section>
    </aside>`;
}

function renderWeeklyBrief(data) {
  const brief = data.weeklyBrief || {};
  const highlights = brief.highlights || [];
  const complete = data.mantis?.search_complete === true;
  const count = (value) => `${Number(value || 0)}건${complete ? "" : " 이상"}`;
  return `
    <section class="workspace-brief-hero">
      <span>이번 주 업무 브리핑</span>
      <h3>${escapeHtml(data.user?.displayName || "사용자")}님의 업무 요약</h3>
      <p>${escapeHtml(brief.period || "")}</p>
    </section>
    <section class="workspace-summary-grid">
      ${workspaceMetric("신규 이슈", count(brief.newIssues))}
      ${workspaceMetric("변경 이슈", count(brief.updatedIssues), "is-blue")}
      ${workspaceMetric("현재 미종료", count(brief.openIssues))}
      ${workspaceMetric("일정 지연", count(brief.overdueIssues), brief.overdueIssues ? "is-danger" : "")}
    </section>
    <section class="workspace-section workspace-section-single">
      <header><div><h3>이번 주 확인할 항목</h3>${complete ? "" : "<p>일부 조회 결과입니다. 건수는 확인 범위의 최소값입니다.</p>"}</div></header>
      <div class="workspace-list">${highlights.length ? highlights.map(issueWorkspaceRow).join("") : workspaceEmpty(complete ? "이번 주 우선 확인할 이슈가 없습니다." : "확인한 범위에는 우선 확인할 이슈가 없습니다.")}</div>
    </section>
    <button type="button" class="workspace-primary-action" data-workspace-question="${encodeURIComponent("내 담당 Mantis 이슈를 기준으로 이번 주 업무보고서를 작성해줘")}">AI로 주간 보고서 작성</button>`;
}

function briefSpikeRows(items, emptyMessage, customer = false) {
  return items.length ? items.map((item) => {
    const comparison = item.previousCount
      ? `직전 주 ${item.previousCount}건 · ${item.ratio}배`
      : "직전 주 0건 · 새로 증가";
    return `<article class="workspace-insight-row"><strong>${escapeHtml(customer ? customerName(item.name) : item.name)}</strong><span>${item.count}건</span><small>${escapeHtml(comparison)}</small></article>`;
  }).join("") : workspaceEmpty(emptyMessage);
}

function renderTeamWeeklyBrief() {
  const brief = state.teamWeeklyBrief;
  if (!brief) return `<div class="workspace-loading">팀 브리핑을 불러오는 중입니다.</div>`;
  const summary = brief.summary || {};
  const priorities = brief.priorities || [];
  const complete = brief.source?.search_complete === true;
  const count = (value) => `${Number(value || 0)}건${complete ? "" : " 이상"}`;
  const delta = !complete ? "비교 미확정" : summary.changeRate == null
    ? "비교 기준 없음"
    : `${summary.changeRate > 0 ? "+" : ""}${summary.changeRate}%`;
  const canRefresh = ["manager", "admin"].includes(state.currentUser?.role);
  const decision = brief.decisionSummary || {};
  const riskLabel = { high: "높은 위험", attention: "주의 필요", stable: "안정", unknown: "판단 보류" }[decision.riskLevel] || "판단 대기";
  const decisionSignals = (decision.signals || []).map((signal) => `<li>${escapeHtml(customerLabelText(signal))}</li>`).join("");
  const decisionActions = (decision.recommendedActions || []).map((action) => `
    <button type="button" data-workspace-question="${encodeURIComponent(action.question || action.title || "")}">
      <strong>${escapeHtml(customerLabelText(action.title || "권장 조치"))}</strong>
      <span>${escapeHtml(customerLabelText(action.reason || ""))}</span>
      <em>AI로 분석 →</em>
    </button>`).join("");
  return `
    <section class="workspace-brief-hero">
      <span>주간 팀 브리핑</span>
      <h3>${escapeHtml(brief.title || "이번 주 팀 브리핑")}</h3>
      <p>지난주 ${escapeHtml(brief.summaryPeriod || "")} · 생성 ${escapeHtml(formatUiDateTime(brief.generatedAt) || "시각 미확인")}</p>
    </section>
    <section class="workspace-summary-grid">
      ${workspaceMetric("지난주 신규", count(summary.newIssues))}
      ${workspaceMetric("전주 대비", delta, summary.changeRate > 0 ? "is-warning" : "is-blue")}
      ${workspaceMetric("미종료 이월", count(summary.carriedOpen))}
      ${workspaceMetric("30일 이상", count(summary.aging), summary.aging ? "is-warning" : "")}
    </section>
    ${decision.headline ? `<section class="workspace-decision is-${escapeHtml(decision.riskLevel || "stable")}">
      <header><span>이번 주 판단</span><em>${escapeHtml(riskLabel)}</em></header>
      <h3>${escapeHtml(decision.headline)}</h3>
      ${decisionSignals ? `<ul>${decisionSignals}</ul>` : ""}
      ${decisionActions ? `<div class="workspace-decision-actions">${decisionActions}</div>` : ""}
    </section>` : ""}
    <p class="workspace-connection ${brief.source?.status === "live" ? "" : "is-warning"}">${brief.source?.status === "live" ? "최신 데이터로 생성" : "저장본으로 생성"} · 일정 지연 ${count(summary.overdue)} · 담당자 미지정 ${count(summary.unassigned)}${complete ? "" : " · 일부 데이터만 확인한 결과입니다."}</p>
    <div class="workspace-columns">
      <section class="workspace-section">
        <header><div><h3>급증 고객사</h3></div></header>
        <div class="workspace-insight-list">${complete ? briefSpikeRows(brief.customerSpikes || [], "급증한 고객사가 없습니다.", true) : workspaceEmpty("일부 조회 · 고객사 급증 여부는 비교 미확정입니다.", false)}</div>
      </section>
      <section class="workspace-section">
        <header><div><h3>급증 화면번호</h3></div></header>
        <div class="workspace-insight-list">${complete ? briefSpikeRows(brief.screenSpikes || [], "급증한 화면번호가 없습니다.") : workspaceEmpty("일부 조회 · 화면번호 급증 여부는 비교 미확정입니다.", false)}</div>
      </section>
    </div>
    <section class="workspace-section workspace-section-single">
      <header><div><h3>이번 주 우선 확인</h3></div><em>${priorities.length}건 표시</em></header>
      <div class="workspace-list">${priorities.length ? priorities.map(issueWorkspaceRow).join("") : workspaceEmpty(complete ? "우선 확인할 이슈가 없습니다." : "일부만 확인했습니다. 우선 확인할 이슈의 유무는 미확정입니다.", complete)}</div>
    </section>
    ${canRefresh ? '<button type="button" class="workspace-primary-action" data-refresh-team-brief>최신 데이터로 다시 만들기</button>' : ""}`;
}

// 주간 보고 탭: 내 보고와 팀 브리핑을 한 곳에서 고른다. 팀 브리핑은 처음 열 때 불러오고 읽음으로 저장한다.
let teamBriefPromise = null;

function teamBriefUnread() {
  return Boolean(state.teamWeeklyBrief && !state.teamWeeklyBrief.viewed);
}

function renderWeeklyReport(data) {
  const kind = state.workspaceBriefKind === "team" ? "team" : "mine";
  const unread = teamBriefUnread();
  const switcher = `
    <div class="workspace-brief-switch" role="group" aria-label="보고 종류">
      <button type="button" data-brief-kind="mine" aria-pressed="${kind === "mine"}">내 보고</button>
      <button type="button" data-brief-kind="team" aria-pressed="${kind === "team"}">팀 브리핑${unread ? '<i class="workspace-tab-dot" aria-label="새 팀 브리핑"></i>' : ""}</button>
    </div>`;
  if (kind === "mine") return `${switcher}${renderWeeklyBrief(data)}`;
  if (state.teamBriefError && !state.teamWeeklyBrief) {
    return `${switcher}<div class="workspace-error" role="alert"><strong>팀 브리핑을 불러오지 못했습니다.</strong><p>${escapeHtml(state.teamBriefError)}</p><button type="button" class="workspace-item-action" data-brief-kind="team">다시 시도</button></div>`;
  }
  return `${switcher}${renderTeamWeeklyBrief()}`;
}

async function loadTeamBriefOnce(refresh = false) {
  if (teamBriefPromise && !refresh) return teamBriefPromise;
  state.teamBriefError = "";
  teamBriefPromise = loadTeamWeeklyBrief(refresh)
    .catch((error) => { state.teamBriefError = error.message; })
    .finally(() => { teamBriefPromise = null; });
  return teamBriefPromise;
}

// 탭의 빨간 점을 위해 팀 브리핑이 새로 나왔는지만 미리 확인한다(열지 않으면 읽음 처리하지 않음).
function prefetchTeamBrief() {
  if (state.teamWeeklyBrief || !state.currentUser?.uid) return;
  loadTeamBriefOnce().then(() => updateWorkspaceUnread());
}

async function showTeamBrief({ refresh = false } = {}) {
  state.workspaceTab = "brief";
  state.workspaceBriefKind = "team";
  renderWorkspace();
  if (refresh || !state.teamWeeklyBrief) {
    await loadTeamBriefOnce(refresh);
    renderWorkspace();
  }
  if (!state.teamWeeklyBrief) return;
  try {
    await markTeamWeeklyBriefRead();
  } catch {
    // 읽음 저장이 실패해도 브리핑은 계속 보여 준다. 다음에 열 때 다시 저장한다.
  }
  renderWorkspace();
}

function showMyBrief() {
  state.workspaceTab = "brief";
  state.workspaceBriefKind = "mine";
  renderWorkspace();
}

// 일정 알림은 업무 날짜, 배정·노트 알림은 원본을 확인한 로컬 시각으로 표시한다.
const NOTIFICATION_KINDS = {
  overdue: { icon: "warning", tone: "is-overdue", dateLabel: "완료 예정" },
  aging: { icon: "clock", tone: "is-aging", dateLabel: "최근 변경" },
  assigned: { icon: "users", tone: "", dateLabel: "확인 시각" },
  note: { icon: "chat", tone: "", dateLabel: "확인 시각" },
};

function notificationDate(item) {
  return ["assigned", "note"].includes(item.type) ? formatUiDateTime(item.date) : formatWorkspaceDate(item.date);
}

function notificationRow(item) {
  const kind = NOTIFICATION_KINDS[item.type] || { icon: "info", tone: "", dateLabel: "" };
  const issueId = Number.isInteger(item.issueId) && item.issueId > 0 ? item.issueId
    : String(item.id || "").match(/^mantis-(?:overdue|aging)-(\d+)(?:-|$)/)?.[1];
  const dateText = item.date ? `${kind.dateLabel ? `${kind.dateLabel} ` : ""}${notificationDate(item)}` : "";
  return `
        <article class="workspace-notification ${item.read ? "is-read" : ""}">
          <span class="workspace-notification-icon ${kind.tone}" aria-hidden="true">${icon(kind.icon, 18)}</span>
          <div><strong>${escapeHtml(item.title)}${item.read ? "" : '<i class="workspace-notification-dot" aria-label="안 읽음"></i>'}</strong><p>${escapeHtml(item.description)}</p><small>${escapeHtml(dateText)}</small></div>
          <div class="notification-actions">${issueId ? `<button type="button" data-workspace-question="${encodeURIComponent(`#${issueId} 이슈의 현재 상태와 다음 할 일을 요약해줘`)}">AI 요약</button>` : ""}${item.read ? "" : `<button type="button" data-notification-read="${escapeHtml(item.id)}">읽음으로 표시</button>`}</div>
        </article>`;
}

function notificationEmptyText(data, unreadOnly = false) {
  if (data.notificationScope?.status === "unavailable") {
    return unreadOnly ? "확인한 알림 중 안 읽은 항목이 없습니다. 새 알림 수집은 보류 중입니다."
      : "새 업무 알림을 아직 확인하지 못했어요. 수집이 보류된 상태입니다.";
  }
  return unreadOnly ? "읽지 않은 알림이 없습니다." : "새 업무 알림이 없습니다.";
}

function renderNotifications(data) {
  const notifications = data.notifications || [];
  const unread = notifications.filter((item) => !item.read);
  const read = notifications.filter((item) => item.read);
  const group = (label, items) => (items.length ? `<p class="workspace-group-label">${label} · ${items.length}</p>${items.map(notificationRow).join("")}` : "");
  return `
    <section class="workspace-section workspace-section-single">
      <header><div><h3>업무 알림</h3><p>${escapeHtml(data.notificationScope?.message || "확인한 업무에서 발생한 알림입니다.")}</p></div><button type="button" class="secondary-button" data-notifications-read-all ${unread.length ? "" : "disabled"}>표시된 알림 모두 읽음</button></header>
      <div class="workspace-filter-pills" role="group" aria-label="알림 표시"><button type="button" data-notification-filter="all" aria-pressed="${state.notificationFilter !== "unread"}">${data.notificationScope?.status === "unavailable" ? "확인한 알림" : "전체"} ${notifications.length}</button><button type="button" data-notification-filter="unread" aria-pressed="${state.notificationFilter === "unread"}">안 읽음 ${unread.length}</button></div>
      <div class="workspace-list">${notifications.length ? `${group("안 읽음", unread)}${state.notificationFilter === "unread" ? unread.length ? "" : workspaceEmpty(notificationEmptyText(data, true)) : group("읽은 알림", read)}` : workspaceEmpty(notificationEmptyText(data), data.notificationScope?.status !== "unavailable")}</div>
    </section>`;
}

async function markNotificationsRead(ids = null) {
  const workspace = state.myWorkspace;
  const items = (workspace?.notifications || []).filter((item) => !item.read && (!ids || ids.includes(item.id)));
  const results = await Promise.allSettled(items.map(async (item) => {
    const response = await apiFetch(`/api/workspace/notifications/${encodeURIComponent(item.id)}/read`, { method: "POST" });
    if (!response.ok) throw new Error("알림 저장 실패");
    item.read = true;
  }));
  if (workspace?.summary) workspace.summary.unreadNotifications = workspace.notifications.filter((item) => !item.read).length;
  if (state.myWorkspace === workspace) renderWorkspace();
  const failed = results.filter((result) => result.status === "rejected").length;
  if (failed) throw new Error(`${failed}개 알림의 읽음 상태를 저장하지 못했습니다. 다시 시도해 주세요.`);
}

function renderFavoriteConversations() {
  const query = String(state.favoriteQuery || "").trim().toLocaleLowerCase();
  const favorites = state.conversations.filter((item) => item.favorite && (!query || `${item.title || ""} ${(item.messages || []).filter((message) => message.role === "user").map((message) => message.content).join(" ")}`.toLocaleLowerCase().includes(query)));
  return `
    <section class="workspace-section workspace-section-single">
      <header><div><h3>즐겨찾는 대화</h3><p>자주 사용하는 질문과 답변을 다시 확인하세요.</p></div><em>${favorites.length}개</em></header>
      <form class="workspace-search" data-favorite-search><input name="q" type="search" value="${escapeHtml(state.favoriteQuery || "")}" aria-label="즐겨찾기 검색" placeholder="대화 제목 또는 질문 검색"/><button type="submit" class="secondary-button">검색</button></form>
      ${favorites.length ? `<div class="workspace-favorite-grid">${favorites.map((conversation) => {
        // 카드에는 첫 질문을 보여 줘 제목만으로 구분하기 어려운 대화도 알아볼 수 있게 한다.
        const firstQuestion = (conversation.messages || []).find((message) => message.role === "user")?.content || "";
        const count = conversation.messageCount ?? (conversation.messages || []).length;
        return `
        <article class="workspace-favorite-card">
          <header><strong>${escapeHtml(conversation.title || "새 대화")}</strong><button type="button" data-remove-favorite="${escapeHtml(conversation.id)}" aria-label="${escapeHtml(conversation.title || "새 대화")} 즐겨찾기 해제">${icon("star", 18)}</button></header>
          ${firstQuestion ? `<p>“${escapeHtml(firstQuestion)}”</p>` : ""}
          <footer><small>${escapeHtml(formatWorkspaceDate(conversation.updatedAt))} · 메시지 ${count}개</small>
            <button type="button" class="workspace-item-action" data-open-favorite="${escapeHtml(conversation.id)}">대화 열기</button></footer>
        </article>`;
      }).join("")}</div>` : `<div class="workspace-list">${workspaceEmpty(query ? "검색 조건에 맞는 즐겨찾기가 없습니다." : "사이드바의 별표를 눌러 자주 보는 대화를 저장해 보세요.")}</div>`}
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
      <header><div><h3>${editor.id ? "플레이북 편집" : "새 플레이북 작성"}</h3></div></header>
      <label><span>제목 <span class="required-mark">*</span></span><input name="title" maxlength="200" required value="${escapeHtml(editor.title || "")}" /></label>
      <label><span>증상 <span class="required-mark">*</span></span><textarea name="symptom" maxlength="4000" required>${escapeHtml(editor.symptom || "")}</textarea></label>
      <label>원인<textarea name="cause" maxlength="4000" placeholder="확인된 원인이 없으면 비워둘 수 있습니다.">${escapeHtml(editor.cause || "")}</textarea></label>
      <label><span>해결 방법 <span class="required-mark">*</span></span><textarea name="resolution" maxlength="8000" required>${escapeHtml(editor.resolution || "")}</textarea></label>
      <label>검증 방법<textarea name="verification" maxlength="4000" placeholder="조치 후 확인해야 할 내용을 적어주세요.">${escapeHtml(editor.verification || "")}</textarea></label>
      <div class="playbook-editor-row">
        <label>태그<input name="tags" maxlength="500" value="${escapeHtml((editor.tags || []).join(", "))}" placeholder="고객사, 화면번호, 증상" /></label>
        <label>근거 이슈<input name="sourceIssueIds" maxlength="300" value="${escapeHtml((editor.sourceIssueIds || []).map((id) => `#${id}`).join(", "))}" placeholder="#12345, #12346" /></label>
      </div>
      <div class="playbook-editor-actions">
        <button type="button" class="secondary-button" data-cancel-playbook-edit>취소</button>
        <button type="submit" class="secondary-button">${editor.id ? "수정 내용 저장" : "초안 저장"}</button><button type="submit" class="workspace-primary-action" name="publish" value="published">저장 후 공개</button>
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
    <article class="playbook-card${state.playbookEditor?.id && state.playbookEditor.id === playbook.id ? " is-editing" : ""}">
      <header>
        <div><span class="playbook-status is-${escapeHtml(playbook.status)}">${escapeHtml(playbookStatusLabel(playbook.status))}</span><h3>${escapeHtml(playbook.title)}</h3></div>
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
  if (state.playbooksLoading && !state.playbooks) return '<div class="workspace-loading" role="status"><span class="workspace-spinner" aria-hidden="true"></span>플레이북을 불러오는 중입니다.</div>';
  const error = state.playbooksError ? `<div class="workspace-refresh-status is-error" role="alert"><strong>${escapeHtml(state.playbooksError)}</strong><button type="button" class="secondary-button" data-playbooks-retry>다시 시도</button></div>` : "";
  if (error && !state.playbooks) return error;
  const playbooks = state.playbooks || [];
  const canCurate = state.playbooksCanCurate;
  const filter = state.playbookFilter || { q: "", status: "" };
  const editorForm = playbookEditorForm();
  const selected = playbooks.find((item) => item.id === state.selectedPlaybookId) || playbooks[0];
  // 편집 폼이 열리면 넓은 화면에서 왼쪽 목록 · 오른쪽 편집 2단으로 둔다. 닫혀 있으면 목록만 한 칸이다.
  return `
    ${error}
    <section class="playbook-toolbar">
      <form data-playbook-search-form>
        <input name="q" maxlength="200" value="${escapeHtml(filter.q || "")}" placeholder="증상, 고객사, 화면번호, 이슈 번호 검색" aria-label="플레이북 검색" />
        ${canCurate ? `<select name="status" aria-label="플레이북 상태"><option value="">모든 상태</option><option value="draft" ${filter.status === "draft" ? "selected" : ""}>검수 대기</option><option value="published" ${filter.status === "published" ? "selected" : ""}>공개</option><option value="archived" ${filter.status === "archived" ? "selected" : ""}>보관</option></select>` : ""}
        <button type="submit" class="secondary-button">검색</button>
      </form>
      ${canCurate ? '<button type="button" class="workspace-primary-action" data-new-playbook>새 플레이북</button>' : ""}
    </section>
    <p class="workspace-connection">${canCurate ? "검수 대기·공개·보관 플레이북을 모두 관리합니다." : "팀에서 검수해 공개한 해결 플레이북만 표시합니다."} · ${playbooks.length}건</p>
    <div class="playbook-layout has-detail${editorForm ? " has-editor" : ""}">
      <div class="playbook-list" aria-label="플레이북 목록">${playbooks.length ? playbooks.map((item) => `<button type="button" class="playbook-list-item" data-select-playbook="${escapeHtml(item.id)}" aria-pressed="${selected?.id === item.id}"><span class="playbook-status is-${escapeHtml(item.status)}">${escapeHtml(playbookStatusLabel(item.status))}</span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml((item.tags || []).join(" · "))}</small><time>${escapeHtml(String(item.updatedAt || "").slice(0, 10))}</time></button>`).join("") : workspaceEmpty(filter.q ? "검색 조건에 맞는 플레이북이 없습니다." : "아직 공개된 해결 플레이북이 없습니다.")}</div>
      <div class="playbook-detail">${editorForm || (selected ? playbookCard(selected) : "")}</div>
    </div>`;
}

async function loadPlaybooks(force = false) {
  if (state.playbooksLoading) return;
  state.playbooksLoading = true;
  state.playbooksError = "";
  renderWorkspace();
  try {
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
  } catch (error) {
    state.playbooksError = error.message;
    throw error;
  } finally {
    state.playbooksLoading = false;
    renderWorkspace();
  }
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

async function savePlaybookEditor(form, publish = false) {
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
      body: JSON.stringify(publish ? { ...payload, status: "published" } : playbookId ? payload : { ...payload, status: "draft" }),
    },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "플레이북을 저장하지 못했습니다.");
  state.playbookEditor = null;
  state.selectedPlaybookId = data.playbook?.id;
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
  if (els.workspaceTabBriefDot) els.workspaceTabBriefDot.hidden = !teamBriefUnread();
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
  els.workspaceRefreshBtn.innerHTML = `${icon("refresh", 16)}<span>${loading ? "불러오는 중…" : "새로고침"}</span>`;
  els.workspaceRefreshBtn.setAttribute("aria-busy", String(loading));
}

// 내 업무 머리의 '데이터 기준' 배지. 담당자명이 없으면 조회 자체를 할 수 없어 '확인 필요'로 둔다.
function updateWorkspaceDataBadge(loading) {
  if (!els.workspaceDataBadge) return;
  const data = state.myWorkspace;
  const mantis = data?.mantis;
  if (!mantis) {
    if (loading) renderDataBadge(els.workspaceDataBadge, { state: "loading" });
    else if (workspaceLoadingError) {
      renderDataBadge(els.workspaceDataBadge, {
        state: "error",
        sources: [{ name: "Mantis", tone: "error", text: workspaceLoadingError }],
        onRetry: () => loadMyWorkspace(true).catch(() => {}),
      });
    } else els.workspaceDataBadge.innerHTML = "";
    return;
  }
  const owner = data.user?.mantisUsername;
  if (mantis.status === "unlinked" || !owner) {
    renderDataBadge(els.workspaceDataBadge, {
      state: "warning",
      sources: [{ name: "Mantis", tone: "warn", text: "담당자명이 연결되지 않아 조회하지 못했어요" }],
      note: "관리자에게 Mantis 담당자명 등록을 요청해 주세요.",
    });
    return;
  }
  if (workspaceLoadingError && !loading) {
    renderDataBadge(els.workspaceDataBadge, {
      state: "snapshot", label: "갱신 실패", time: formatBadgeTime(mantis.fetchedAt),
      detailTime: mantis.fetchedAt ? `${formatUiDateTime(mantis.fetchedAt)} 마지막 조회` : "마지막 조회 시각 미확인",
      sources: [{ name: "Mantis", tone: "error", text: workspaceLoadingError }],
      note: "마지막으로 확인한 데이터를 표시합니다. 이후 변경은 반영되지 않았어요.",
      onRetry: () => loadMyWorkspace(true).catch(() => {}),
    });
    return;
  }
  const badgeState = dataBadgeState({
    source: mantis.status === "live" ? "live" : mantis.status,
    complete: mantis.search_complete === true,
    refreshing: loading || Boolean(mantis.refreshPending),
  });
  const time = formatBadgeTime(mantis.fetchedAt);
  const details = mantisBadgeDetails(badgeState, { time, reason: mantis.reason });
  renderDataBadge(els.workspaceDataBadge, {
    state: badgeState,
    time: ["fresh", "partial", "snapshot"].includes(badgeState) ? time : "",
    detailTime: mantis.fetchedAt ? `${formatUiDateTime(mantis.fetchedAt)} 조회` : "",
    sources: [details.source, { name: "담당자", tone: "muted", text: owner }],
    note: details.note,
    onRetry: details.retry ? () => loadMyWorkspace(true).catch(() => {}) : null,
  });
}

function renderWorkspace() {
  if (!els.workspaceContent) return;
  releaseCharts(els.workspaceContent);
  const loading = workspaceLoading || Boolean(workspaceFreshPromise);
  updateWorkspaceDataBadge(loading);
  els.workspaceContent.setAttribute("aria-busy", String(loading));
  const isPersonalOverview = state.workspaceTab === "overview";
  els.workspaceContent.classList.toggle("is-overview", isPersonalOverview);
  els.workspaceContent.classList.toggle("has-status", !loading && Boolean(state.myWorkspace) && Boolean(workspaceLoadingError));
  els.workspaceModalEyebrow.textContent = `${state.currentUser?.displayName || state.currentUser?.name || "사용자"} · 내 담당 업무`;
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
  if (loading && !state.myWorkspace) {
    els.workspaceContent.innerHTML = `<div class="workspace-loading" role="status"><span class="workspace-spinner" aria-hidden="true"></span><strong>${escapeHtml(workspaceLoadingMessage || "업무 정보를 불러오는 중입니다.")}</strong></div>`;
    return;
  }
  if (!state.myWorkspace) {
    els.workspaceContent.innerHTML = workspaceLoadingError
      ? workspaceFailure(workspaceLoadingError)
      : '<div class="workspace-loading" role="status">업무 정보를 불러오는 중입니다.</div>';
    return;
  }
  const renderers = {
    overview: () => renderPersonalOverview(state.myWorkspace),
    brief: () => renderWeeklyReport(state.myWorkspace),
    playbooks: renderPlaybooks,
    notifications: () => renderNotifications(state.myWorkspace),
    favorites: renderFavoriteConversations,
  };
  const loadingStatus = workspaceLoadingError
      ? `<div class="workspace-refresh-status is-error" role="status"><strong>${escapeHtml(workspaceLoadingError)}</strong><span>마지막으로 확인한 데이터를 표시합니다.</span><button type="button" class="secondary-button" data-workspace-retry>다시 시도</button></div>`
      : "";
  els.workspaceContent.innerHTML = `${loadingStatus}${(renderers[state.workspaceTab] || renderers.overview)()}`;
  mountCharts(els.workspaceContent);
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
  bulkDeleteConversations,
  scheduleConversationSync,
  loadConversations,
  updateActiveConversationTitle,
  renderConversationList,
  setConversationQuery,
  toggleConversationListExpanded,
  loadMoreConversations,
  startNewConversation,
  openConversation,
  loadEarlierMessages,
  safeExternalUrl,
  workspaceMetric,
  workspaceEmpty,
  workspaceStatusLabel,
  issueWorkspaceRow,
  renderPersonalOverview,
  renderWeeklyBrief,
  renderWeeklyReport,
  prefetchTeamBrief,
  showMyBrief,
  showTeamBrief,
  briefSpikeRows,
  renderTeamWeeklyBrief,
  renderNotifications,
  markNotificationsRead,
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
