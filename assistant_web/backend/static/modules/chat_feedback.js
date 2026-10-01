import { state, els, apiFetch } from "./runtime.js?v=20260911-6";
import { showToast, persistConversations } from "./workspace.js?v=20260911-6";
import { refreshFeedbackSummary } from "./admin.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";

let renderMessagesCallback = () => {};

function configureChatFeedback({ renderMessages } = {}) {
  if (typeof renderMessages === "function") renderMessagesCallback = renderMessages;
}

const MESSAGE_ACTION_ICONS = {
  copy: icon("copy"),
  check: icon("check"),
  regenerate: icon("regenerate"),
  edit: icon("edit"),
  helpful: icon("thumb-up"),
  improve: icon("feedback"),
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
        ${MESSAGE_ACTION_ICONS.helpful}<span>도움됨</span>
      </button>
      <button type="button" class="message-action-btn feedback-action-btn" data-feedback-rating="needs_improvement" data-message-index="${messageIndex}" aria-label="개선 필요" aria-pressed="${improveSelected}">
        ${MESSAGE_ACTION_ICONS.improve}<span>${improveSelected ? "의견 남김" : "개선 필요"}</span>
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

