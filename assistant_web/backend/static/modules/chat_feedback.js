import { state, els, apiFetch } from "./runtime.js";
import { showToast, persistConversations } from "./workspace.js";
import { refreshFeedbackSummary } from "./admin.js";

let renderMessagesCallback = () => {};

function configureChatFeedback({ renderMessages } = {}) {
  if (typeof renderMessages === "function") renderMessagesCallback = renderMessages;
}

const MESSAGE_ACTION_ICONS = {
  copy: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="7.5" y="7.5" width="9" height="9" rx="1.8"/><path d="M4.5 12.5v-7a2 2 0 0 1 2-2h7"/></svg>',
  check: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.5 10.5 8 14l7.5-8"/></svg>',
  regenerate: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 10a6.5 6.5 0 0 1 11.2-4.5M16.5 10a6.5 6.5 0 0 1-11.2 4.5"/><path d="M14.5 3v3h-3M5.5 17v-3h3"/></svg>',
  edit: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4.5 15.5 8 7 16.5H3.5V13z"/></svg>',
  helpful: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6.5 8.5 9.5 3c1.2.2 2 1.3 1.7 2.5L10.8 8h4.1c1.2 0 2.1 1.1 1.8 2.3l-1.2 5A2.2 2.2 0 0 1 13.4 17H6.5z"/><path d="M3.5 8.5h3v8.5h-3z"/></svg>',
  improve: '<svg viewBox="0 0 20 20" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6.5 11.5 3 5.5c1.2-.2 2-1.3 1.7-2.5l-.4-2.5h4.1c1.2 0 2.1-1.1 1.8-2.3l-1.2-5A2.2 2.2 0 0 0 13.4 3H6.5z"/><path d="M3.5 3h3v8.5h-3z"/></svg>',
};

function renderMessageActions(message, messageIndex) {
  const actions = document.createElement("div");
  actions.className = "message-actions";
  if (message.role === "assistant") {
    const helpfulSelected = message.feedback?.rating === "helpful";
    const improveSelected = message.feedback?.rating === "needs_improvement";
    const feedbackActions = message.auditId ? `
      <span class="message-action-divider" aria-hidden="true"></span>
      <span class="message-feedback-label">도움이 되었나요?</span>
      <button type="button" class="message-action-btn feedback-action-btn" data-feedback-rating="helpful" data-message-index="${messageIndex}" aria-label="도움됨" aria-pressed="${helpfulSelected}">
        ${MESSAGE_ACTION_ICONS.helpful}<span>${helpfulSelected ? "도움됨 ✓" : "도움됨"}</span>
      </button>
      <button type="button" class="message-action-btn feedback-action-btn" data-feedback-rating="needs_improvement" data-message-index="${messageIndex}" aria-label="개선 필요" aria-pressed="${improveSelected}">
        ${MESSAGE_ACTION_ICONS.improve}<span>${improveSelected ? "의견 남김 ✓" : "개선 필요"}</span>
      </button>
    ` : "";
    actions.innerHTML = `
      <button type="button" class="message-action-btn" data-message-action="copy" data-message-index="${messageIndex}" aria-label="답변 복사">
        ${MESSAGE_ACTION_ICONS.copy}<span>복사</span>
      </button>
      <button type="button" class="message-action-btn" data-message-action="regenerate" data-message-index="${messageIndex}" aria-label="답변 재생성">
        ${MESSAGE_ACTION_ICONS.regenerate}<span>재생성</span>
      </button>
      ${feedbackActions}
    `;
  } else {
    actions.innerHTML = `
      <button type="button" class="message-action-btn" data-message-action="edit" data-message-index="${messageIndex}" aria-label="수정 후 재전송">
        ${MESSAGE_ACTION_ICONS.edit}<span>수정 후 재전송</span>
      </button>
    `;
  }
  return actions;
}

function feedbackQuestionForMessage(messageIndex) {
  return state.messages.slice(0, messageIndex).reverse().find((message) => message.role === "user")?.content || "";
}

function openFeedbackModal(messageIndex) {
  const message = state.messages[messageIndex];
  if (!message?.auditId || !els.feedbackModal) return;
  state.feedbackMessageIndex = messageIndex;
  els.feedbackForm.reset();
  els.feedbackQuestion.textContent = feedbackQuestionForMessage(messageIndex);
  els.feedbackComment.value = message.feedback?.comment || "";
  if (message.feedback?.reason) {
    const selectedReason = els.feedbackForm.querySelector(`[name="feedbackReason"][value="${message.feedback.reason}"]`);
    if (selectedReason) selectedReason.checked = true;
  }
  els.feedbackModal.hidden = false;
  document.body.classList.add("modal-open");
  els.feedbackForm.querySelector('[name="feedbackReason"]')?.focus();
}

function closeFeedbackModal() {
  if (!els.feedbackModal) return;
  els.feedbackModal.hidden = true;
  state.feedbackMessageIndex = null;
  document.body.classList.remove("modal-open");
}

async function saveMessageFeedback(messageIndex, rating, reason = "", comment = "") {
  const message = state.messages[messageIndex];
  if (!message?.auditId) return;
  const response = await apiFetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      auditId: message.auditId,
      rating,
      reason: reason || null,
      comment,
      templateId: message.templateId || null,
    }),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || "피드백을 저장하지 못했습니다.");
  }
  const data = await response.json();
  message.feedback = data.feedback;
  persistConversations();
  renderMessagesCallback();
  refreshFeedbackSummary();
  showToast(rating === "helpful" ? "도움이 되었다는 의견을 반영했어요." : "개선 의견을 저장했어요.", "success");
}

export {
  configureChatFeedback,
  MESSAGE_ACTION_ICONS,
  renderMessageActions,
  feedbackQuestionForMessage,
  openFeedbackModal,
  closeFeedbackModal,
  saveMessageFeedback,
};

