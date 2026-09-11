import { state, els, apiFetch, buildTeamTemplateQuestion, shouldSubmitPromptOnKeydown } from "./runtime.js?v=20260911-6";
import { deleteConversationFromServer, createConversation, showToast, persistConversations, scheduleConversationSync, startNewConversation, openConversation, renderWorkspace, loadMyWorkspace, loadPlaybooks, savePlaybookCandidate, savePlaybookEditor, changePlaybookStatus } from "./workspace.js?v=20260911-6";
import { updateModelMeta, saveTheme, setShowSources, showSourcesEnabled, setAbComparison, abComparisonEnabled, selectedTeamTemplate, templateFormValues, updateTemplatePreview, openTemplateModal, closeTemplateModal, renderMessages, updateComposerShadow, MESSAGE_ACTION_ICONS, openFeedbackModal, closeFeedbackModal, saveMessageFeedback, rerunWithoutCondition, closeChromaModal, openSourceDetail, autoResizeInput, resetOptions, requestAssistant } from "./chat.js?v=20260911-6";
import { renderEvaluationContent, runAccuracyCheck, openEvaluationModal, closeEvaluationModal, runEvaluations } from "./admin.js?v=20260911-6";
import { showChatView } from "./dashboard.js?v=20260911-6";

function cancelMessageEdit() {
  state.editingMessageIndex = null;
  state.pendingTemplateId = null;
  if (els.editNotice) els.editNotice.hidden = true;
}

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
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

els.input.addEventListener("input", autoResizeInput);
els.input.addEventListener("keydown", (event) => {
  if (shouldSubmitPromptOnKeydown(event)) {
    event.preventDefault();
    els.form.requestSubmit();
  }
});

els.messages.addEventListener("click", (event) => {
  const templateCard = event.target.closest("[data-template-id]");
  if (!templateCard || state.busy) return;
  openTemplateModal(templateCard.dataset.templateId);
});

els.templateFields?.addEventListener("input", updateTemplatePreview);
els.templateFields?.addEventListener("change", updateTemplatePreview);
els.templateForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  const template = selectedTeamTemplate();
  if (!template) return;
  const question = buildTeamTemplateQuestion(template, templateFormValues());
  if (!question) return;
  state.pendingTemplateId = template.id;
  els.input.value = question;
  autoResizeInput();
  closeTemplateModal();
  els.form.requestSubmit();
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
  els.brandHome.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      goHome();
    }
  });
}

// 좁은 화면에서는 사이드바를 오버레이로 접어두고 헤더의 메뉴 버튼으로 연다.
// 넓은 화면에서는 항상 펼쳐져 있으므로 이 상태는 CSS 미디어쿼리 조건 안에서만
// 시각적으로 의미를 갖는다.
function closeMobileSidebar() {
  document.body.classList.remove("sidebar-open");
  els.sidebarMenuToggle?.setAttribute("aria-expanded", "false");
  els.personalSidebarMenuToggle?.setAttribute("aria-expanded", "false");
  els.dashboardSidebarMenuToggle?.setAttribute("aria-expanded", "false");
}

els.sidebarMenuToggle?.addEventListener("click", () => {
  const isOpen = document.body.classList.toggle("sidebar-open");
  els.sidebarMenuToggle.setAttribute("aria-expanded", String(isOpen));
});

els.sidebar?.addEventListener("click", (event) => {
  if (event.target.closest("[data-conversation-id]") || event.target.closest("#newChatBtn")) {
    closeMobileSidebar();
  }
});

// 어두운 배경을 탭하면 닫히도록 한다. 배경은 body::before 가상 요소라 리스너를
// 직접 붙일 수 없어, 문서 클릭이 사이드바 바깥인지로 판단한다.
document.addEventListener("click", (event) => {
  if (!document.body.classList.contains("sidebar-open")) return;
  if (event.target.closest(".sidebar") || event.target.closest("#sidebarMenuToggle")) return;
  closeMobileSidebar();
});
els.cancelEditBtn.addEventListener("click", () => {
  cancelMessageEdit();
  els.input.value = "";
  autoResizeInput();
  els.input.focus();
});
els.conversationList.addEventListener("click", (event) => {
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
els.evaluationBtn.addEventListener("click", openEvaluationModal);
els.templateModalClose?.addEventListener("click", closeTemplateModal);
els.templateCancelBtn?.addEventListener("click", closeTemplateModal);
els.templateModal?.addEventListener("click", (event) => {
  if (event.target === els.templateModal) closeTemplateModal();
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
els.evaluationModalClose.addEventListener("click", closeEvaluationModal);
els.runEvaluationBtn.addEventListener("click", () => runEvaluations());
els.evaluationContent.addEventListener("click", (event) => {
  const accuracyButton = event.target.closest("[data-accuracy-run]");
  if (accuracyButton && !accuracyButton.disabled) {
    runAccuracyCheck();
    return;
  }
  const rerunButton = event.target.closest("[data-evaluation-rerun]");
  if (!rerunButton || rerunButton.disabled) return;
  runEvaluations([rerunButton.dataset.evaluationRerun]);
});
els.evaluationModal.addEventListener("click", (event) => {
  if (event.target === els.evaluationModal) closeEvaluationModal();
});
els.workspaceRefreshBtn?.addEventListener("click", async () => {
  try {
    if (state.workspaceTab === "playbooks") {
      els.workspaceRefreshBtn.disabled = true;
      els.workspaceRefreshBtn.textContent = "불러오는 중…";
      els.workspaceRefreshBtn.setAttribute("aria-busy", "true");
      await loadPlaybooks(true);
    } else await loadMyWorkspace(true);
  } catch (error) {
    showToast(error.message, "warning");
  } finally {
    els.workspaceRefreshBtn.disabled = false;
    els.workspaceRefreshBtn.textContent = "새로고침";
    els.workspaceRefreshBtn.setAttribute("aria-busy", "false");
  }
});
function selectWorkspaceTab(name) {
  state.workspaceTab = name;
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
  const submitButton = editorForm.querySelector('[type="submit"]');
  submitButton.disabled = true;
  try {
    await savePlaybookEditor(editorForm);
    showToast("플레이북 초안을 저장했어요.", "success");
  } catch (error) {
    showToast(error.message, "warning");
    submitButton.disabled = false;
  }
});
document.querySelectorAll(".evaluation-tab").forEach((button) => {
  button.addEventListener("click", () => {
    state.evaluationTab = button.dataset.evaluationTab;
    renderEvaluationContent();
  });
});

els.messages.addEventListener("scroll", updateComposerShadow, { passive: true });
window.addEventListener("scroll", updateComposerShadow, { passive: true });
window.addEventListener("resize", updateComposerShadow);
els.scrollToTopBtn?.addEventListener("click", () => {
  const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const scrollTarget = window.matchMedia?.("(max-width: 860px)").matches ? window : els.messages;
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
      navigator.clipboard.writeText(message.content || "").then(() => {
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

  const toggle = event.target.closest(".source-list-toggle");
  if (toggle) {
    const panel = document.getElementById(toggle.dataset.sourceToggle);
    if (!panel) return;
    const expanded = toggle.getAttribute("aria-expanded") === "true";
    toggle.setAttribute("aria-expanded", String(!expanded));
    panel.hidden = expanded;
    const label = toggle.querySelector("em");
    if (label) label.textContent = expanded ? "펼치기" : "접기";
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

  const card = event.target.closest(".source-card, .related-issue-item, .notion-card");
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
  if (event.key === "Escape" && !els.templateModal.hidden) {
    closeTemplateModal();
  } else if (event.key === "Escape" && !els.feedbackModal.hidden) {
    closeFeedbackModal();
  } else if (event.key === "Escape" && !els.chromaModal.hidden) {
    closeChromaModal();
  } else if (event.key === "Escape" && !els.evaluationModal.hidden) {
    closeEvaluationModal();
  } else if (event.key === "Escape" && document.body.classList.contains("sidebar-open")) {
    // 모달이 모두 닫혀 있을 때만 좁은 화면의 사이드바를 닫는다.
    closeMobileSidebar();
  }
});

export {
  cancelMessageEdit,
  closeMobileSidebar,
};
