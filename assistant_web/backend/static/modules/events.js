import { customerAnswerText } from "./ui_format.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { state, els, apiFetch, shouldSubmitPromptOnKeydown } from "./runtime.js?v=20260911-6";
import { deleteConversationFromServer, createConversation, showToast, persistConversations, scheduleConversationSync, startNewConversation, openConversation, loadEarlierMessages, setConversationQuery, toggleConversationListExpanded, loadMoreConversations, renderWorkspace, loadMyWorkspace, loadPlaybooks, savePlaybookCandidate, savePlaybookEditor, changePlaybookStatus, showMyBrief, showTeamBrief, markNotificationsRead } from "./workspace.js?v=20260911-6";
import { updateModelMeta, saveTheme, setShowSources, showSourcesEnabled, setAbComparison, abComparisonEnabled, renderMessages, updateComposerShadow, MESSAGE_ACTION_ICONS, openFeedbackModal, closeFeedbackModal, saveMessageFeedback, rerunWithoutCondition, closeChromaModal, openSourceDetail, autoResizeInput, resetOptions, requestAssistant } from "./chat.js?v=20260911-6";
import { showChatView } from "./dashboard.js?v=20260911-6";
import { closeAccountMenu, closeMobileSidebar, isAccountMenuOpen, isMobileSidebarOpen, toggleAccountMenu, toggleMobileSidebar } from "./sidebar.js?v=20260911-6";
import { initTemplatePicker, openTemplatePicker, showTemplateForm, closeTemplatePicker, isTemplatePickerOpen, handleTemplatePickerKeydown, syncTemplatePickerWithPrompt, completedTemplateQuestion, rememberTemplate } from "./template_picker.js?v=20260911-6";

initTemplatePicker();

function cancelMessageEdit() {
  state.editingMessageIndex = null;
  state.pendingTemplateId = null;
  if (els.editNotice) els.editNotice.hidden = true;
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.initializing) {
    showToast("이전 대화를 불러오고 있습니다. 잠시 후 전송해 주세요.", "warning");
    return;
  }
  if (state.busy) {
    if (state.chatCanAbort) state.chatAbortController?.abort();
    return;
  }
  const content = els.input.value.trim();
  if (!content) return;
  const templateId = state.pendingTemplateId || "";
  state.pendingTemplateId = null;

  if (state.editingMessageIndex !== null) {
    state.messages = state.messages.slice(0, state.editingMessageIndex);
    cancelMessageEdit();
  }
  els.input.value = "";
  autoResizeInput();
  const questionAskedAt = new Date().toISOString();
  state.messages.push({ role: "user", content, createdAt: questionAskedAt, templateId });
  persistConversations();
  await requestAssistant(content, questionAskedAt, templateId);
});

els.input.addEventListener("input", () => {
  autoResizeInput();
  // 템플릿 문장을 넣은 뒤 입력창을 비우면 다음 질문에 템플릿 ID가 남지 않게 한다.
  if (!els.input.value.trim()) state.pendingTemplateId = null;
  syncTemplatePickerWithPrompt();
});
els.input.addEventListener("keydown", (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (state.busy && event.key === "Enter" && !event.shiftKey) { event.preventDefault(); return; }
  if (handleTemplatePickerKeydown(event)) return;
  if (shouldSubmitPromptOnKeydown(event)) {
    event.preventDefault();
    els.form.requestSubmit();
  }
});

els.messages.addEventListener("click", (event) => {
  if (event.target.closest("[data-load-earlier-messages]")) {
    void loadEarlierMessages();
    return;
  }
  if (event.target.closest("[data-open-all-templates]")) { openTemplatePicker(); return; }
  const templateCard = event.target.closest("[data-template-id]");
  if (!templateCard || state.busy) return;
  showTemplateForm(templateCard.dataset.templateId);
});

function applyTemplateQuestion() {
  const completed = completedTemplateQuestion();
  if (!completed) return false;
  rememberTemplate(completed.template.id);
  state.pendingTemplateId = completed.template.id;
  els.input.value = completed.question;
  autoResizeInput();
  return true;
}

els.templateForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!applyTemplateQuestion()) return;
  closeTemplatePicker({ clearSlash: false, restoreFocus: false });
  els.form.requestSubmit();
});
els.templateInsertBtn?.addEventListener("click", () => {
  if (!applyTemplateQuestion()) return;
  closeTemplatePicker({ clearSlash: false });
});

els.temperature.addEventListener("input", () => {
  els.temperatureValue.textContent = Number(els.temperature.value).toFixed(1);
});

els.searchLimit.addEventListener("input", () => {
  els.searchLimitValue.textContent = `${els.searchLimit.value}건`;
});

els.darkModeToggle.addEventListener("click", () => {
  saveTheme(!document.body.classList.contains("theme-dark"));
});

els.showSources.addEventListener("click", () => {
  setShowSources(!showSourcesEnabled());
});
els.abTestToggle.addEventListener("click", () => {
  setAbComparison(!abComparisonEnabled());
  renderMessages();
});
els.modelSelect.addEventListener("change", updateModelMeta);
els.resetOptionsBtn.addEventListener("click", () => {
  resetOptions();
  els.input.focus();
});
els.newChatBtn.addEventListener("click", () => {
  if (state.busy) return;
  showChatView();
  startNewConversation();
  els.input.focus();
  closeMobileSidebar();
});

// 새로고침만 하면 localStorage의 기존 활성 대화가 다시 복원된다. 브랜드 영역은
// 새 대화를 실제로 생성한 뒤 빈 초기 화면을 렌더링해야 한다.
if (els.brandHome) {
  const goHome = () => {
    if (state.busy) return;
    showChatView();
    startNewConversation();
    closeMobileSidebar();
    els.input.focus();
  };
  els.brandHome.addEventListener("click", goHome);
}

// 좁은 화면에서는 사이드바를 왼쪽 드로어로 접어두고 헤더의 메뉴 버튼으로 연다(sidebar.js).
els.sidebarMenuToggle?.addEventListener("click", () => toggleMobileSidebar(els.sidebarMenuToggle));
els.sidebarCloseBtn?.addEventListener("click", () => closeMobileSidebar());

els.sidebar?.addEventListener("click", (event) => {
  if (event.target.closest("[data-conversation-id]") || event.target.closest("#newChatBtn")) {
    closeMobileSidebar();
  }
});

// 어두운 배경을 탭하면 닫히도록 한다. 배경은 body::before 가상 요소라 리스너를
// 직접 붙일 수 없어, 문서 클릭이 사이드바 바깥인지로 판단한다.
document.addEventListener("click", (event) => {
  if (!isMobileSidebarOpen()) return;
  // 목록 펼치기는 클릭 도중 DOM을 교체한다. 원래 전파 경로로 판단해야
  // 이미 분리된 버튼을 사이드바 바깥 클릭으로 오인하지 않는다.
  if (event.composedPath().some((node) => node === els.sidebar || node === els.sidebarMenuToggle)) return;
  closeMobileSidebar();
});

// 내 계정 메뉴(다크 모드 · 로그아웃). 바깥을 누르면 닫되, 로그아웃 확인 창이 떠 있는 동안에는
// 메뉴를 그대로 두어 취소했을 때 포커스가 로그아웃 버튼으로 돌아오게 한다.
els.accountMenuBtn?.addEventListener("click", toggleAccountMenu);
document.addEventListener("click", (event) => {
  if (!isAccountMenuOpen() || document.body.classList.contains("modal-open")) return;
  // 확인 창의 취소 버튼은 클릭 도중 창을 닫으므로 전파 경로로 창 안의 클릭인지 판단한다.
  if (event.composedPath().some((node) => node === els.accountMenu || node === els.accountMenuBtn ||
      node?.classList?.contains("modal-backdrop"))) return;
  closeAccountMenu();
});
els.cancelEditBtn.addEventListener("click", () => {
  cancelMessageEdit();
  els.input.value = "";
  autoResizeInput();
  els.input.focus();
});
els.conversationSearch?.addEventListener("input", () => setConversationQuery(els.conversationSearch.value));
els.conversationSearch?.addEventListener("keydown", (event) => {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === "Escape" && els.conversationSearch.value) {
    // 검색어만 지우고, 좁은 화면의 사이드바를 닫는 문서 Escape 처리까지는 올리지 않는다.
    event.preventDefault();
    event.stopPropagation();
    els.conversationSearch.value = "";
    setConversationQuery("");
  } else if (event.key === "Enter") {
    const firstResult = els.conversationList.querySelector("[data-conversation-id]");
    if (!firstResult) return;
    event.preventDefault();
    firstResult.click();
  }
});
els.conversationList.addEventListener("click", (event) => {
  if (event.target.closest("[data-conversation-more]")) {
    toggleConversationListExpanded();
    return;
  }
  if (event.target.closest("[data-conversation-load-more]")) {
    loadMoreConversations();
    return;
  }
  const favoriteButton = event.target.closest("[data-favorite-conversation-id]");
  if (favoriteButton) {
    if (state.busy) return;
    const conversation = state.conversations.find((item) => item.id === favoriteButton.dataset.favoriteConversationId);
    if (!conversation) return;
    conversation.favorite = !conversation.favorite;
    conversation.updatedAt = new Date().toISOString();
    persistConversations();
    // 즐겨찾기는 메시지 변경이 아니므로 persistConversations 가 동기화하지 않는다.
    scheduleConversationSync(conversation);
    if (state.workspaceTab === "favorites" && !els.personalPanel?.hidden) renderWorkspace();
    showToast(conversation.favorite ? "대화를 즐겨찾기에 추가했어요." : "즐겨찾기에서 해제했어요.");
    // 목록을 다시 그려도 방금 누른 즐겨찾기 버튼에 키보드 포커스를 되돌린다.
    els.conversationList.querySelector(`[data-favorite-conversation-id="${CSS.escape(conversation.id)}"]`)?.focus();
    return;
  }
  const deleteButton = event.target.closest("[data-delete-conversation-id]");
  if (deleteButton) {
    if (state.busy) return;
    const conversationId = deleteButton.dataset.deleteConversationId;
    const removedIndex = state.conversations.findIndex((item) => item.id === conversationId);
    if (removedIndex < 0) return;
    // 실행 취소로 되돌릴 수 있도록 삭제 직전 위치와 활성 대화를 함께 기억한다.
    const removed = state.conversations[removedIndex];
    const deletingUid = state.currentUser?.uid;
    const previousActiveId = state.activeConversationId;
    const previousMessages = [...state.messages];
    state.conversations = state.conversations.filter((item) => item.id !== conversationId);
    const placeholder = state.conversations.length ? null : createConversation();
    if (placeholder) state.conversations.push(placeholder);
    if (conversationId === state.activeConversationId) {
      state.activeConversationId = state.conversations[0].id;
      state.messages = [...(state.conversations[0].messages || [])];
    }
    persistConversations();
    renderMessages();
    if (state.currentUser?.uid) {
      deleteConversationFromServer(removed).catch(() => {
        if (state.currentUser?.uid !== deletingUid) return;
        if (!state.conversations.some((item) => item.id === removed.id)) state.conversations.push(removed);
        persistConversations();
        showToast("서버에서 삭제하지 못해 대화를 복원했습니다.", "warning");
      });
    }
    showToast(`"${removed.title || "대화"}"를 삭제했어요.`, "default", {
      label: "실행 취소",
      onClick: () => {
        if (state.currentUser?.uid !== deletingUid) return;
        // 비어서 채워 넣은 임시 대화를 걷어내고 원래 자리에 되돌린다.
        if (placeholder) {
          state.conversations = state.conversations.filter((item) => item.id !== placeholder.id);
        }
        const restoreIndex = Math.min(removedIndex, state.conversations.length);
        state.conversations.splice(restoreIndex, 0, removed);
        state.activeConversationId = previousActiveId;
        state.messages = previousMessages;
        persistConversations();
        // 서버에서도 같은 ID로 다시 저장해 삭제를 되돌린다.
        scheduleConversationSync(removed);
        renderMessages();
        showToast("삭제를 취소했어요.");
      },
    });
    return;
  }
  const openButton = event.target.closest("[data-conversation-id]");
  if (openButton) {
    showChatView();
    openConversation(openButton.dataset.conversationId);
  }
});
els.feedbackModalClose?.addEventListener("click", closeFeedbackModal);
els.feedbackCancelBtn?.addEventListener("click", closeFeedbackModal);
els.feedbackModal?.addEventListener("click", (event) => {
  if (event.target === els.feedbackModal) closeFeedbackModal();
});
els.feedbackForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const messageIndex = state.feedbackMessageIndex;
  const reason = new FormData(els.feedbackForm).get("feedbackReason");
  if (messageIndex === null || !reason) return;
  const submitButton = els.feedbackForm.querySelector('[type="submit"]');
  submitButton.disabled = true;
  try {
    await saveMessageFeedback(messageIndex, "needs_improvement", String(reason), els.feedbackComment.value.trim());
    closeFeedbackModal();
  } catch (error) {
    showToast(error.message, "warning");
  } finally {
    submitButton.disabled = false;
  }
});
els.workspaceRefreshBtn?.addEventListener("click", async () => {
  try {
    if (state.workspaceTab === "playbooks") {
      els.workspaceRefreshBtn.disabled = true;
      els.workspaceRefreshBtn.innerHTML = `${icon("refresh", 16)}<span>불러오는 중…</span>`;
      els.workspaceRefreshBtn.setAttribute("aria-busy", "true");
      await loadPlaybooks(true);
    } else await loadMyWorkspace(true);
  } catch (error) {
    showToast(error.message, "warning");
  } finally {
    els.workspaceRefreshBtn.disabled = false;
    els.workspaceRefreshBtn.innerHTML = `${icon("refresh", 16)}<span>새로고침</span>`;
    els.workspaceRefreshBtn.setAttribute("aria-busy", "false");
  }
});
function selectWorkspaceTab(name) {
  state.workspaceTab = name;
  // 주간 보고에서 팀 브리핑을 보던 중이면 다시 열 때도 불러오고 읽음으로 저장한다.
  if (name === "brief" && state.workspaceBriefKind === "team") {
    showTeamBrief();
    return;
  }
  renderWorkspace();
  if (state.workspaceTab === "playbooks" && !state.playbooks) {
    loadPlaybooks().catch((error) => showToast(error.message, "warning"));
  }
}

els.workspaceTabs?.addEventListener("click", (event) => {
  const button = event.target.closest("[data-workspace-tab]");
  if (!button) return;
  selectWorkspaceTab(button.dataset.workspaceTab);
});

// role="tablist" 는 좌우 화살표·Home·End 이동을 전제로 한다.
els.workspaceTabs?.addEventListener("keydown", (event) => {
  const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
  if (!keys.includes(event.key)) return;
  const tabs = [...els.workspaceTabs.querySelectorAll("[data-workspace-tab]")];
  if (!tabs.length) return;
  const current = tabs.findIndex((tab) => tab.dataset.workspaceTab === state.workspaceTab);
  const from = current < 0 ? 0 : current;
  let next = from;
  if (event.key === "ArrowLeft") next = (from - 1 + tabs.length) % tabs.length;
  else if (event.key === "ArrowRight") next = (from + 1) % tabs.length;
  else if (event.key === "Home") next = 0;
  else next = tabs.length - 1;
  event.preventDefault();
  selectWorkspaceTab(tabs[next].dataset.workspaceTab);
  tabs[next].focus();
});
els.workspaceContent?.addEventListener("click", async (event) => {
  const retry = event.target.closest("[data-workspace-retry]");
  if (retry) {
    retry.disabled = true;
    try {
      await loadMyWorkspace(true);
    } catch (error) {
      showToast(error.message || "업무 정보를 다시 조회하지 못했어요.", "error");
    } finally {
      retry.disabled = false;
    }
    return;
  }
  if (event.target.closest("[data-playbooks-retry]")) { loadPlaybooks(true).catch((error) => showToast(error.message, "warning")); return; }
  const notificationFilter = event.target.closest("[data-notification-filter]");
  if (notificationFilter) { state.notificationFilter = notificationFilter.dataset.notificationFilter; renderWorkspace(); return; }
  const markAll = event.target.closest("[data-notifications-read-all]");
  if (markAll) {
    markAll.disabled = true;
    try { await markNotificationsRead(); showToast("알림을 읽음으로 표시했어요.", "success"); }
    catch (error) { showToast(error.message, "warning"); markAll.disabled = false; }
    return;
  }
  if (event.target.closest("[data-workspace-more]")) { state.workspaceIssuesExpanded = !state.workspaceIssuesExpanded; renderWorkspace(); return; }
  const removeFavorite = event.target.closest("[data-remove-favorite]");
  if (removeFavorite) {
    const conversation = state.conversations.find((item) => item.id === removeFavorite.dataset.removeFavorite);
    if (conversation) { conversation.favorite = false; scheduleConversationSync(conversation); persistConversations(); renderWorkspace(); showToast("즐겨찾기를 해제했어요."); }
    return;
  }
  const selectedPlaybook = event.target.closest("[data-select-playbook]");
  if (selectedPlaybook) {
    if (state.playbookEditor) { showToast("편집 내용을 저장하거나 취소한 후 다른 플레이북을 선택해 주세요.", "warning"); return; }
    state.selectedPlaybookId = selectedPlaybook.dataset.selectPlaybook; renderWorkspace(); return;
  }
  const newPlaybookButton = event.target.closest("[data-new-playbook]");
  if (newPlaybookButton) {
    state.playbookEditor = { title: "", symptom: "", cause: "", resolution: "", verification: "", tags: [], sourceIssueIds: [] };
    renderWorkspace();
    els.workspaceContent.querySelector('[name="title"]')?.focus();
    return;
  }
  const editPlaybookButton = event.target.closest("[data-edit-playbook]");
  if (editPlaybookButton) {
    const playbook = state.playbooks?.find((item) => item.id === editPlaybookButton.dataset.editPlaybook);
    if (!playbook) return;
    state.playbookEditor = { ...playbook, tags: [...(playbook.tags || [])], sourceIssueIds: [...(playbook.sourceIssueIds || [])] };
    renderWorkspace();
    els.workspaceContent.querySelector('[name="title"]')?.focus();
    return;
  }
  if (event.target.closest("[data-cancel-playbook-edit]")) {
    state.playbookEditor = null;
    renderWorkspace();
    return;
  }
  const statusButton = event.target.closest("[data-playbook-status]");
  if (statusButton) {
    statusButton.disabled = true;
    try {
      await changePlaybookStatus(statusButton.dataset.playbookId, statusButton.dataset.playbookStatus);
      showToast("플레이북 상태를 변경했어요.", "success");
    } catch (error) {
      showToast(error.message, "warning");
      statusButton.disabled = false;
    }
    return;
  }
  // 업무 탭 옆 칸의 '주간 보고 열기'·'모두 보기'는 해당 탭으로 옮긴다.
  const goTab = event.target.closest("[data-workspace-go]");
  if (goTab) {
    selectWorkspaceTab(goTab.dataset.workspaceGo);
    return;
  }
  const briefKind = event.target.closest("[data-brief-kind]");
  if (briefKind) {
    if (briefKind.dataset.briefKind === "team") showTeamBrief();
    else showMyBrief();
    return;
  }
  if (event.target.closest("[data-refresh-team-brief]")) {
    showTeamBrief({ refresh: true });
    return;
  }
  const questionButton = event.target.closest("[data-workspace-question]");
  if (questionButton) {
    showChatView();
    els.input.value = decodeURIComponent(questionButton.dataset.workspaceQuestion || "");
    autoResizeInput();
    els.input.focus();
    return;
  }
  const favoriteButton = event.target.closest("[data-open-favorite]");
  if (favoriteButton) {
    showChatView();
    openConversation(favoriteButton.dataset.openFavorite);
    return;
  }
  const notificationButton = event.target.closest("[data-notification-read]");
  if (notificationButton) {
    const notificationId = notificationButton.dataset.notificationRead;
    notificationButton.disabled = true;
    try {
      const response = await apiFetch(`/api/workspace/notifications/${encodeURIComponent(notificationId)}/read`, { method: "POST" });
      if (!response.ok) throw new Error("알림 상태를 저장하지 못했습니다.");
      const notification = state.myWorkspace?.notifications?.find((item) => item.id === notificationId);
      if (notification) notification.read = true;
      if (state.myWorkspace?.summary) {
        state.myWorkspace.summary.unreadNotifications = state.myWorkspace.notifications.filter((item) => !item.read).length;
      }
      renderWorkspace();
    } catch (error) {
      showToast(error.message, "warning");
      notificationButton.disabled = false;
    }
  }
});
els.workspaceContent?.addEventListener("submit", async (event) => {
  const favoriteForm = event.target.closest("[data-favorite-search]");
  if (favoriteForm) { event.preventDefault(); state.favoriteQuery = String(new FormData(favoriteForm).get("q") || ""); renderWorkspace(); els.workspaceContent.querySelector('[data-favorite-search] input')?.focus(); return; }
  const searchForm = event.target.closest("[data-playbook-search-form]");
  if (searchForm) {
    event.preventDefault();
    const formData = new FormData(searchForm);
    state.playbookFilter = {
      q: String(formData.get("q") || "").trim(),
      status: String(formData.get("status") || ""),
    };
    loadPlaybooks(true).catch((error) => showToast(error.message, "warning"));
    return;
  }
  const editorForm = event.target.closest("[data-playbook-editor]");
  if (!editorForm) return;
  event.preventDefault();
  const submitButton = event.submitter || editorForm.querySelector('[type="submit"]');
  const buttons = [...editorForm.querySelectorAll('[type="submit"]')];
  buttons.forEach((button) => { button.disabled = true; });
  try {
    await savePlaybookEditor(editorForm, submitButton.value === "published");
    showToast(submitButton.value === "published" ? "플레이북을 저장하고 공개했어요." : "플레이북을 저장했어요.", "success");
  } catch (error) {
    showToast(error.message, "warning");
    buttons.forEach((button) => { button.disabled = false; });
  }
});

els.workspaceContent?.addEventListener("change", (event) => {
  if (event.target.matches("[data-workspace-sort]")) { state.workspaceSort = event.target.value; renderWorkspace(); }
});

els.messages.addEventListener("scroll", updateComposerShadow, { passive: true });
window.addEventListener("scroll", updateComposerShadow, { passive: true });
window.addEventListener("resize", updateComposerShadow);
els.scrollToTopBtn?.addEventListener("click", () => {
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const scrollTarget = els.messages;
  scrollTarget.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
});

els.messages.addEventListener("click", (event) => {
  const playbookButton = event.target.closest("[data-save-playbook-candidate]");
  if (playbookButton) {
    const messageIndex = Number(playbookButton.dataset.messageIndex);
    const message = state.messages[messageIndex];
    if (!message?.playbookCandidate || message.playbookSaved) return;
    playbookButton.disabled = true;
    savePlaybookCandidate(message.playbookCandidate).then(() => {
      message.playbookSaved = true;
      persistConversations();
      renderMessages();
      showToast("플레이북 검수 대기 초안으로 저장했어요.", "success");
    }).catch((error) => {
      showToast(error.message, "warning");
      playbookButton.disabled = false;
    });
    return;
  }
  const feedbackButton = event.target.closest("[data-feedback-rating]");
  if (feedbackButton) {
    const messageIndex = Number(feedbackButton.dataset.messageIndex);
    if (feedbackButton.dataset.feedbackRating === "helpful") {
      saveMessageFeedback(messageIndex, "helpful").catch((error) => showToast(error.message, "warning"));
    } else {
      openFeedbackModal(messageIndex);
    }
    return;
  }

  const conditionButton = event.target.closest("[data-remove-condition]");
  if (conditionButton) {
    try {
      const condition = JSON.parse(decodeURIComponent(conditionButton.dataset.removeCondition || ""));
      rerunWithoutCondition(Number(conditionButton.dataset.messageIndex), condition);
    } catch {
      return;
    }
    return;
  }

  const actionButton = event.target.closest("[data-message-action]");
  if (actionButton) {
    const messageIndex = Number(actionButton.dataset.messageIndex);
    const message = state.messages[messageIndex];
    if (!message || state.busy) return;
    if (actionButton.dataset.messageAction === "copy") {
      const text = actionButton.dataset.answerVariant === "B" ? message.abTest?.variantB?.content : message.content;
      navigator.clipboard.writeText(customerAnswerText(text || "")).then(() => {
        const original = actionButton.innerHTML;
        actionButton.innerHTML = `${MESSAGE_ACTION_ICONS.check}<span>복사됨</span>`;
        setTimeout(() => { actionButton.innerHTML = original; }, 1200);
      }).catch(() => {
        showToast("클립보드 접근이 차단되어 복사하지 못했어요.", "warning");
      });
    } else if (actionButton.dataset.messageAction === "edit") {
      state.editingMessageIndex = messageIndex;
      state.pendingTemplateId = message.templateId || "";
      els.editNotice.hidden = false;
      els.input.value = message.content || "";
      autoResizeInput();
      els.input.focus();
    } else if (actionButton.dataset.messageAction === "regenerate") {
      let userIndex = messageIndex - 1;
      while (userIndex >= 0 && state.messages[userIndex].role !== "user") userIndex -= 1;
      if (userIndex < 0) return;
      const question = state.messages[userIndex].content;
      const questionAskedAt = state.messages[userIndex].createdAt || "";
      const templateId = state.messages[userIndex].templateId || message.templateId || "";
      state.messages = state.messages.slice(0, messageIndex);
      persistConversations();
      requestAssistant(question, questionAskedAt, templateId);
    }
    return;
  }

  const followUp = event.target.closest(".follow-up-chip");
  if (followUp) {
    els.input.value = followUp.dataset.followUp || followUp.textContent || "";
    autoResizeInput();
    els.input.focus();
    return;
  }

  const sourceMore = event.target.closest("[data-source-more]");
  if (sourceMore) {
    const expanded = sourceMore.getAttribute("aria-expanded") !== "true";
    sourceMore.closest(".source-list").querySelectorAll("[data-related-row]").forEach((row) => { row.hidden = !expanded && Number(row.dataset.relatedRow) >= 3; });
    sourceMore.setAttribute("aria-expanded", String(expanded));
    sourceMore.textContent = expanded ? "접기" : `${sourceMore.dataset.sourceMore}건 더 보기`;
    return;
  }
  const sourceView = event.target.closest("[data-source-view]");
  if (sourceView) {
    const table = sourceView.closest(".source-list").classList.toggle("is-table");
    sourceView.setAttribute("aria-pressed", String(table));
    sourceView.textContent = table ? "목록으로 보기" : "표로 보기";
    return;
  }
  const toggle = event.target.closest(".source-list-toggle");
  if (toggle) {
    const panel = document.getElementById(toggle.dataset.sourceToggle);
    if (!panel) return;
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!expanded));
    panel.hidden = expanded;
    const label = toggle.querySelector("em");
    // 글자만 바꾸고 옆의 펼침 아이콘은 그대로 둔다(회전은 aria-expanded 로 CSS가 처리).
    if (label?.firstChild?.nodeType === Node.TEXT_NODE) label.firstChild.textContent = expanded ? "펼치기" : "접기";
    return;
  }

  const inlineRef = event.target.closest(".inline-issue-ref");
  if (inlineRef) {
    openSourceDetail(state.sourceMap.get(inlineRef.dataset.sourceKey));
    return;
  }

  const issueRow = event.target.closest(".issue-summary-clickable, .issue-table-clickable");
  if (issueRow) {
    openSourceDetail(state.sourceMap.get(issueRow.dataset.sourceKey));
    return;
  }

  const detailAction = event.target.closest(".issue-detail-action");
  if (detailAction) {
    openSourceDetail(state.sourceMap.get(detailAction.dataset.sourceKey));
    return;
  }

  const card = event.target.closest(".source-card, .related-issue-item, .related-issue-table [data-source-key], .notion-card");
  if (!card) return;
  openSourceDetail(state.sourceMap.get(card.dataset.sourceKey));
});

document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  const issueRow = event.target.closest?.(".issue-table-clickable");
  if (!issueRow) return;
  event.preventDefault();
  openSourceDetail(state.sourceMap.get(issueRow.dataset.sourceKey));
});

els.chromaModalClose.addEventListener("click", closeChromaModal);
els.chromaModal.addEventListener("click", (event) => {
  if (event.target === els.chromaModal) closeChromaModal();
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && isTemplatePickerOpen()) {
    closeTemplatePicker();
  } else if (event.key === "Escape" && !els.feedbackModal.hidden) {
    closeFeedbackModal();
  } else if (event.key === "Escape" && !els.chromaModal.hidden) {
    closeChromaModal();
  } else if (event.key === "Escape" && isAccountMenuOpen() && !document.body.classList.contains("modal-open")) {
    closeAccountMenu({ restoreFocus: true });
  } else if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
    // 모달이 모두 닫혀 있을 때만 좁은 화면의 사이드바를 닫는다.
    closeMobileSidebar();
  }
});

export {
  cancelMessageEdit,
  closeMobileSidebar,
};
