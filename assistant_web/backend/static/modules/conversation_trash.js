// '최근 삭제한 대화' 창. 서버 보관함(30일)에서 복원하거나 영구 삭제한다.
import { state, apiFetch } from "./runtime.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { escapeHtml } from "./chat_rendering.js?v=20260911-6";
import { restoreConversationFromTrash, openConversation, showToast } from "./workspace.js?v=20260911-6";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { openDialogFocus, closeDialogFocus } from "./dialog_focus.js?v=20260911-6";
import { relativeTimeLabel, trashRemainingLabel } from "./recent_items.js?v=20261105-1";

const trash = { items: [], loading: false, failed: false, confirmId: "", confirmAll: false, busyId: "" };

function el(id) {
  return document.querySelector(`#${id}`);
}

function setError(message = "") {
  const error = el("conversationTrashError");
  if (!error) return;
  error.textContent = message;
  error.hidden = !message;
}

function itemHtml(item, now) {
  const remaining = trashRemainingLabel(item.purgeAt, now);
  const confirming = trash.confirmId === item.id;
  const busy = trash.busyId === item.id;
  return `
    <li class="conversation-trash-item">
      ${icon("chat", 18)}
      <div class="conversation-trash-main">
        <strong>${escapeHtml(item.title || "새 대화")}</strong>
        <small>${escapeHtml(relativeTimeLabel(item.deletedAt, now))} 삭제 · 메시지 ${Number(item.messageCount || 0)}개</small>
      </div>
      <span class="conversation-trash-remaining${remaining.urgent ? " is-urgent" : ""}">${escapeHtml(remaining.label)}</span>
      <span class="conversation-trash-actions">
        <button type="button" class="secondary-button" data-trash-restore="${escapeHtml(item.id)}"${busy ? " disabled" : ""}>복원</button>
        <button type="button" class="conversation-trash-purge${confirming ? " is-confirming" : ""}" data-trash-purge="${escapeHtml(item.id)}"${busy ? " disabled" : ""}
          aria-label="${escapeHtml(item.title || "대화")} ${confirming ? "영구 삭제 확인" : "영구 삭제"}">${confirming ? "영구 삭제 확인" : icon("trash", 16)}</button>
      </span>
    </li>`;
}

function renderTrash() {
  const list = el("conversationTrashList");
  const purgeAll = el("conversationTrashPurgeAll");
  if (!list) return;
  // 불러오지 못했으면 '삭제한 대화가 없다'고 잘못 알리지 않도록 목록 자리를 비운다.
  list.hidden = trash.failed && !trash.items.length;
  if (trash.loading) {
    list.innerHTML = '<p class="conversation-trash-empty">보관 중인 대화를 불러오는 중입니다.</p>';
  } else if (!trash.items.length) {
    list.innerHTML = '<p class="conversation-trash-empty">최근 30일 안에 삭제한 대화가 없어요.</p>';
  } else {
    const now = new Date();
    list.innerHTML = `<ul>${trash.items.map((item) => itemHtml(item, now)).join("")}</ul>`;
  }
  if (purgeAll) {
    purgeAll.hidden = !trash.items.length;
    purgeAll.textContent = trash.confirmAll ? `${trash.items.length}개 모두 영구 삭제 확인` : "모두 영구 삭제";
  }
}

async function loadTrash() {
  trash.loading = true;
  trash.failed = false;
  setError("");
  renderTrash();
  try {
    if (!state.currentUser?.uid) throw new Error("로그인하면 최근 삭제한 대화를 볼 수 있습니다.");
    const response = await apiFetch("/api/workspace/conversations/trash");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "보관 중인 대화를 불러오지 못했습니다.");
    trash.items = Array.isArray(data.conversations) ? data.conversations : [];
    const days = Number(data.retentionDays);
    const description = el("conversationTrashDescription");
    if (description && Number.isFinite(days)) {
      description.textContent = `삭제한 대화는 ${days}일 동안 보관돼요. 복원하면 대화 목록으로 돌아오고, 기간이 지나면 자동으로 영구 삭제됩니다.`;
    }
  } catch (error) {
    trash.items = [];
    trash.failed = true;
    setError(error.message);
  } finally {
    trash.loading = false;
    renderTrash();
  }
}

function openTrash() {
  const modal = el("conversationTrashModal");
  if (!modal) return;
  Object.assign(trash, { confirmId: "", confirmAll: false, busyId: "" });
  modal.hidden = false;
  openDialogFocus(el("conversationTrashDialog"), { initialFocus: el("conversationTrashClose"), onClose: closeTrash, fallback: () => el("conversationTrashBtn") });
  void loadTrash();
}

function closeTrash() {
  const modal = el("conversationTrashModal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  closeDialogFocus(el("conversationTrashDialog"));
}

async function restore(id) {
  if (state.busy || state.conversationDeleting) {
    setError("답변을 작성하거나 대화를 정리하는 중에는 복원할 수 없어요. 잠시 뒤 다시 시도해 주세요.");
    return;
  }
  trash.busyId = id;
  setError("");
  renderTrash();
  try {
    const saved = await restoreConversationFromTrash(id);
    trash.items = trash.items.filter((item) => item.id !== id);
    showToast(`"${saved.title || "대화"}"를 복원했어요.`, "default", {
      label: "열기",
      onClick: () => {
        closeTrash();
        showChatView();
        openConversation(saved.id);
      },
    });
  } catch (error) {
    setError(error.message);
  } finally {
    trash.busyId = "";
    renderTrash();
    (el("conversationTrashList")?.querySelector("[data-trash-restore]") || el("conversationTrashDone"))?.focus();
  }
}

async function purge({ id = "", all = false }) {
  trash.busyId = id;
  setError("");
  try {
    const response = await apiFetch("/api/workspace/conversations/trash/purge", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(all ? { all: true } : { ids: [id] }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "영구 삭제하지 못했습니다. 다시 시도해 주세요.");
    const purged = new Set(data.purgedIds || []);
    trash.items = trash.items.filter((item) => !purged.has(item.id));
    showToast(all ? "보관 중인 대화를 모두 영구 삭제했어요." : "대화를 영구 삭제했어요.");
  } catch (error) {
    setError(error.message);
  } finally {
    Object.assign(trash, { busyId: "", confirmId: "", confirmAll: false });
    renderTrash();
    (el("conversationTrashList")?.querySelector("[data-trash-restore]") || el("conversationTrashDone"))?.focus();
  }
}

function initConversationTrash() {
  el("conversationTrashBtn")?.addEventListener("click", openTrash);
  el("conversationTrashClose")?.addEventListener("click", closeTrash);
  el("conversationTrashDone")?.addEventListener("click", closeTrash);
  el("conversationTrashModal")?.addEventListener("click", (event) => {
    if (event.target === el("conversationTrashModal")) closeTrash();
  });
  el("conversationTrashPurgeAll")?.addEventListener("click", () => {
    // 되돌릴 수 없으므로 한 번 더 눌러야 실행한다.
    if (!trash.confirmAll) {
      trash.confirmAll = true;
      trash.confirmId = "";
      renderTrash();
      return;
    }
    void purge({ all: true });
  });
  el("conversationTrashList")?.addEventListener("click", (event) => {
    const restoreButton = event.target.closest("[data-trash-restore]");
    if (restoreButton) return void restore(restoreButton.dataset.trashRestore);
    const purgeButton = event.target.closest("[data-trash-purge]");
    if (!purgeButton) return;
    const id = purgeButton.dataset.trashPurge;
    if (trash.confirmId !== id) {
      trash.confirmId = id;
      trash.confirmAll = false;
      renderTrash();
      el("conversationTrashList")?.querySelector(`[data-trash-purge="${CSS.escape(id)}"]`)?.focus();
      return;
    }
    void purge({ id });
  });
}

export { initConversationTrash, openTrash, closeTrash };
