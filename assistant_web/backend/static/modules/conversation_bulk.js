import { state, els, searchConversations } from "./runtime.js?v=20260911-6";
import { renderConversationList, bulkDeleteConversations, showToast } from "./workspace.js?v=20260911-6";
import { openDialogFocus, closeDialogFocus } from "./dialog_focus.js?v=20260911-6";

export function initConversationBulkActions() {
  const find = (id) => document.querySelector(`#${id}`);
  const manage = els.conversationManageBtn;
  if (!manage) return;
  const panel = find("conversationBulkActions");
  const selectLoaded = find("conversationSelectLoaded");
  const selectedCount = find("conversationSelectedCount");
  const deleteSelected = find("conversationDeleteSelected");
  const deleteAll = find("conversationDeleteAll");
  const modal = find("conversationDeleteModal");
  const title = find("conversationDeleteTitle");
  const description = find("conversationDeleteDescription");
  const error = find("conversationDeleteError");
  const confirm = find("conversationDeleteConfirm");
  const cancel = find("conversationDeleteCancel");
  const close = find("conversationDeleteClose");
  let scope = null, submitting = false;
  const matching = () => {
    const query = String(els.conversationSearch?.value || "").trim();
    return query ? searchConversations(state.conversations, query).map((item) => item.conversation) : state.conversations;
  };
  const update = () => {
    const blocked = Boolean(state.busy || state.initializing || state.conversationDeleting || submitting);
    manage.disabled = blocked;
    manage.textContent = state.conversationSelectionMode ? "완료" : "관리";
    manage.setAttribute("aria-expanded", String(Boolean(state.conversationSelectionMode)));
    panel.hidden = !state.conversationSelectionMode;
    const selected = state.selectedConversationIds.size;
    deleteSelected.disabled = blocked || selected === 0;
    deleteAll.disabled = blocked;
    const candidates = matching();
    const count = candidates.filter((item) => state.selectedConversationIds.has(item.id)).length;
    const outsideSearch = selected - count;
    selectedCount.textContent = `${selected}개 선택${outsideSearch > 0 ? ` · 검색 밖 ${outsideSearch}개` : ""}`;
    selectLoaded.checked = candidates.length > 0 && count === candidates.length;
    selectLoaded.indeterminate = count > 0 && count < candidates.length;
    selectLoaded.disabled = blocked || candidates.length === 0;
    for (const checkbox of (els.conversationList?.querySelectorAll(".conversation-selection") || [])) checkbox.disabled = blocked;
    for (const button of [els.newChatBtn, els.brandHome, els.templateToggleBtn]) if (button) button.disabled = blocked;
    if (els.sendBtn && !state.busy) els.sendBtn.disabled = blocked || !els.input?.value.trim();
  };
  const dismiss = () => {
    if (submitting) return;
    modal.hidden = true;
    scope = null;
    closeDialogFocus(modal.querySelector('[role="dialog"]'));
  };
  const ask = (all) => {
    if (state.busy || state.conversationDeleting || submitting) return;
    const ids = [...state.selectedConversationIds];
    if (!all && !ids.length) return;
    scope = { ids: all ? [] : ids, all, uid: state.currentUser?.uid };
    title.textContent = all ? "모든 대화를 삭제할까요?" : `선택한 대화 ${ids.length}개를 삭제할까요?`;
    const names = state.conversations.filter((item) => ids.includes(item.id));
    const preview = names.slice(0, 3).map((item) => String(item.title || "새 대화").slice(0, 60)).join(" · ");
    description.textContent = all
      ? "즐겨찾기와 아직 목록에 불러오지 않은 서버 대화, 이 기기의 미저장 대화를 모두 삭제합니다. 다른 사용자의 대화는 유지됩니다. 삭제한 대화는 복구할 수 없습니다."
      : `${preview}${names.length > 3 ? ` 외 ${names.length - 3}개` : ""}. 선택한 대화의 질문·답변과 즐겨찾기를 서버와 이 기기에서 삭제합니다. 삭제한 대화는 복구할 수 없습니다.`;
    error.hidden = true;
    modal.hidden = false;
    confirm.textContent = all ? "모든 대화 삭제" : `${ids.length}개 삭제`;
    openDialogFocus(modal.querySelector('[role="dialog"]'), { initialFocus: cancel, onClose: dismiss, fallback: () => manage });
  };
  manage.addEventListener("click", () => {
    if (state.busy || state.conversationDeleting) return;
    state.conversationSelectionMode = !state.conversationSelectionMode;
    state.selectedConversationIds.clear();
    renderConversationList();
    update();
  });
  selectLoaded.addEventListener("change", () => {
    if (state.busy || state.conversationDeleting) return;
    for (const item of matching()) {
      if (selectLoaded.checked) state.selectedConversationIds.add(item.id);
      else state.selectedConversationIds.delete(item.id);
    }
    renderConversationList();
    update();
  });
  deleteSelected.addEventListener("click", () => ask(false));
  deleteAll.addEventListener("click", () => ask(true));
  cancel.addEventListener("click", dismiss);
  close.addEventListener("click", dismiss);
  modal.addEventListener("click", (event) => { if (event.target === modal) dismiss(); });
  confirm.addEventListener("click", async () => {
    if (!scope || submitting || scope.uid !== state.currentUser?.uid) return;
    submitting = true;
    confirm.disabled = cancel.disabled = close.disabled = true;
    confirm.textContent = "삭제 중…";
    error.hidden = true;
    const requested = scope;
    const pending = bulkDeleteConversations(requested);
    update();
    try {
      const result = await pending;
      if (result && requested.uid === state.currentUser?.uid) {
        submitting = false;
        dismiss();
        showToast(requested.all ? "모든 대화를 삭제했어요." : "선택한 대화를 삭제했어요.");
      }
    } catch (failure) {
      if (requested.uid === state.currentUser?.uid) {
        error.textContent = failure.message;
        error.hidden = false;
      }
    } finally {
      submitting = false;
      confirm.disabled = cancel.disabled = close.disabled = false;
      confirm.textContent = requested.all ? "모든 대화 삭제" : `${requested.ids.length}개 삭제`;
      if (requested.uid !== state.currentUser?.uid) dismiss();
      update();
    }
  });
  globalThis.addEventListener("conversations:rendered", update);
  update();
}
