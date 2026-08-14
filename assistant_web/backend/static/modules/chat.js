import { state, els, defaultOptions, themeStorageKey, apiFetch, buildChatMessages, buildTeamTemplateQuestion, parseSseBuffer, automaticSourceTools } from "./runtime.js";
import { showToast, persistConversations } from "./workspace.js";
import { isMarkdownTableRow, isMarkdownTableSeparatorRow, splitMarkdownTableRow, renderMarkdownTable, extractInlineMarkdownTable, renderNotionAnswer, formatNotionDetail } from "./notion.js";
import { refreshFeedbackSummary } from "./admin.js";

function selectedModel() {
  return state.models.find((model) => model.id === els.modelSelect.value);
}

function statusLabel(status) {
  if (status === "active") return "사용 가능";
  if (status === "missing_key") return "키 필요";
  if (status === "expired") return "만료됨";
  if (status === "disabled") return "비활성";
  return status;
}

function updateModelMeta() {
  const model = selectedModel();
  if (!model) {
    els.modelMeta.textContent = "";
    els.statusLine.textContent = "사용 가능한 모델이 없습니다.";
    els.statusLine.className = "status-danger";
    return;
  }

  const expires = model.expiresAt ? `만료일 ${model.expiresAt}` : "만료일 미설정";
  els.modelMeta.textContent = `${statusLabel(model.status)} · ${expires}`;
  const modelControl = els.modelSelect.closest(".control-group");
  modelControl?.setAttribute("hidden", "");
  modelControl?.classList.add("model-control-hidden");
  if (model.status !== "active") {
    els.statusLine.textContent = statusLabel(model.status);
    els.statusLine.className = model.status === "missing_key" ? "status-warning" : "status-danger";
  }
}

function renderModels() {
  els.modelSelect.innerHTML = "";
  state.models.forEach((model) => {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = `${model.label} (${statusLabel(model.status)})`;
    option.disabled = model.status === "expired" || model.status === "disabled";
    els.modelSelect.appendChild(option);
  });

  const activeModel = state.models.find((model) => model.status === "active");
  if (activeModel) els.modelSelect.value = activeModel.id;
  globalThis.AssistantCustomSelect?.sync(els.modelSelect);
  updateModelMeta();
}

function applyTheme(theme) {
  const dark = theme === "dark";
  document.body.classList.toggle("theme-dark", dark);
  els.darkModeToggle.setAttribute("aria-pressed", String(dark));
  els.darkModeToggle.setAttribute("aria-label", dark ? "라이트 모드로 변경" : "다크 모드로 변경");
  els.themeModeLabel.textContent = dark ? "Dark" : "Light";
}

function loadTheme() {
  const savedTheme = localStorage.getItem(themeStorageKey);
  if (savedTheme === "dark" || savedTheme === "light") {
    applyTheme(savedTheme);
    return;
  }
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  applyTheme(prefersDark ? "dark" : "light");
}

function saveTheme(dark) {
  const theme = dark ? "dark" : "light";
  localStorage.setItem(themeStorageKey, theme);
  applyTheme(theme);
}

function setShowSources(enabled) {
  els.showSources.setAttribute("aria-pressed", String(enabled));
  els.showSources.setAttribute("aria-label", enabled ? "관련 이슈 숨기기" : "관련 이슈 표시하기");
  els.sourceDisplayLabel.textContent = enabled ? "On" : "Off";
}

function showSourcesEnabled() {
  return els.showSources.getAttribute("aria-pressed") === "true";
}

function setAbComparison(enabled) {
  els.abTestToggle.setAttribute("aria-pressed", String(enabled));
  els.abTestToggle.setAttribute("aria-label", enabled ? "A/B 비교 끄기" : "A/B 비교 켜기");
  els.abTestLabel.textContent = enabled ? "On · A와 B 비교" : "Off · A 답변만";
}

function abComparisonEnabled() {
  return els.abTestToggle?.getAttribute("aria-pressed") === "true";
}

function setToolToggle(element, enabled) {
  element?.setAttribute("aria-pressed", String(enabled));
}

function toolToggleEnabled(element) {
  return element?.getAttribute("aria-pressed") === "true";
}

function setSourceMode(mode) {
  const selectedMode = mode === "manual" ? "manual" : "auto";
  if (els.sourceModeSelect) els.sourceModeSelect.value = selectedMode;
  globalThis.AssistantCustomSelect?.sync(els.sourceModeSelect);
  if (els.manualSourceControls) els.manualSourceControls.hidden = selectedMode !== "manual";
  if (els.sourceModeHint) {
    els.sourceModeHint.textContent = selectedMode === "auto"
      ? "질문에 맞는 Mantis 이슈, Notion 자료와 유사 사례를 자동으로 찾습니다."
      : "사용할 검색 소스를 하나 이상 선택해 주세요.";
  }
  if (selectedMode === "auto") {
    setToolToggle(els.mantisToggle, true);
    setToolToggle(els.vectorToggle, true);
  }
}

function sourceToolsPayload() {
  if (els.sourceModeSelect?.value !== "manual") {
    return automaticSourceTools();
  }
  return {
    mantis: toolToggleEnabled(els.mantisToggle),
    notion: true,
    vectorDb: toolToggleEnabled(els.vectorToggle),
  };
}

const TEAM_TEMPLATES = [];

const STARTER_ICONS = {
  today: '<path d="M5 8h14M8 3v4M16 3v4M6 5h12a2 2 0 0 1 2 2v12H4V7a2 2 0 0 1 2-2Z"/><path d="m8 14 2.2 2.2L16 10.5"/>',
  customer: '<path d="M4 20v-2.2c0-2.1 1.8-3.8 4-3.8s4 1.7 4 3.8V20M8 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7ZM15 5h5M15 9h5M15 13h4"/>',
  profile: '<rect x="4" y="3" width="16" height="18" rx="2"/><circle cx="9" cy="10" r="2.5"/><path d="M6.5 17c.4-2 1.4-3 2.5-3s2.1 1 2.5 3M14 8h3M14 12h3M14 16h3"/>',
  spike: '<path d="M4 18 9 12l3 3 7-9"/><path d="M14 6h5v5"/>',
  aging: '<circle cx="12" cy="12" r="8"/><path d="M12 8v5l3 2"/>',
  similar: '<circle cx="10" cy="10" r="6"/><path d="m14.5 14.5 5 5M7 10h6M10 7v6"/>',
  effort: '<path d="M5 20V10M12 20V4M19 20v-7"/><path d="M3 20h18"/>',
  report: '<path d="M6 3h9l3 3v15H6z"/><path d="M14 3v4h4M9 11h6M9 15h6"/>',
};

function renderTeamTemplates() {
  return TEAM_TEMPLATES.map((item) => `
    <button type="button" class="starter-card" data-template-id="${escapeHtml(item.id)}">
      <span class="starter-card-icon" aria-hidden="true">
        <svg viewBox="0 0 24 24">${STARTER_ICONS[item.icon] || STARTER_ICONS.report}</svg>
      </span>
      <span class="starter-card-copy">
        <small>${escapeHtml(item.category)}</small>
        <strong>${escapeHtml(item.title)}</strong>
        <em>${escapeHtml(item.description)}</em>
      </span>
      <span class="starter-card-arrow" aria-hidden="true">→</span>
    </button>
  `).join("");
}

async function loadTeamTemplates() {
  try {
    const response = await apiFetch("/api/templates");
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail || "팀 템플릿을 불러오지 못했습니다.");
    if (!Array.isArray(payload.templates)) throw new Error("팀 템플릿 응답 형식이 올바르지 않습니다.");
    TEAM_TEMPLATES.splice(0, TEAM_TEMPLATES.length, ...payload.templates);
    state.teamTemplatesError = "";
  } catch (error) {
    TEAM_TEMPLATES.splice(0, TEAM_TEMPLATES.length);
    state.teamTemplatesError = error.message || "팀 템플릿을 불러오지 못했습니다.";
    throw error;
  }
}

function selectedTeamTemplate() {
  return TEAM_TEMPLATES.find((template) => template.id === state.activeTemplateId) || null;
}

function templateFormValues() {
  if (!els.templateForm) return {};
  return Object.fromEntries(new FormData(els.templateForm).entries());
}

function updateTemplatePreview() {
  const template = selectedTeamTemplate();
  if (!template || !els.templateQuestionPreview) return;
  els.templateQuestionPreview.textContent = buildTeamTemplateQuestion(template, templateFormValues());
}

function renderTemplateFields(template) {
  els.templateFields.innerHTML = template.fields.map((field) => {
    const inputId = `templateField-${field.name}`;
    const required = field.required ? "required" : "";
    const requiredLabel = field.required ? '<small class="required-mark">필수</small>' : '<small>선택</small>';
    const today = new Date();
    const dynamicValue = field.dynamicValue === "current-month"
      ? `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}`
      : "";
    const control = field.type === "select"
      ? `<select id="${inputId}" name="${escapeHtml(field.name)}" ${required}>
          ${field.options.map((option) => `<option value="${escapeHtml(option)}" ${option === field.value ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
        </select>`
      : `<input id="${inputId}" name="${escapeHtml(field.name)}" type="${field.type}" value="${escapeHtml(field.value || dynamicValue)}" placeholder="${escapeHtml(field.placeholder || "")}" ${field.min ? `min="${field.min}"` : ""} ${field.max ? `max="${field.max}"` : ""} ${required} />`;
    return `
      <label class="template-field" for="${inputId}">
        <span>${escapeHtml(field.label)} ${requiredLabel}</span>
        ${control}
      </label>
    `;
  }).join("");
  globalThis.AssistantCustomSelect?.enhanceAll(els.templateFields);
}

function openTemplateModal(templateId) {
  const template = TEAM_TEMPLATES.find((item) => item.id === templateId);
  if (!template || !els.templateModal) return;
  state.activeTemplateId = template.id;
  els.templateModalTitle.textContent = template.title;
  els.templateModalDescription.textContent = template.description;
  renderTemplateFields(template);
  updateTemplatePreview();
  els.templateModal.hidden = false;
  document.body.classList.add("modal-open");
  els.templateFields.querySelector("input, .custom-select-trigger, select")?.focus();
}

function closeTemplateModal() {
  if (!els.templateModal) return;
  els.templateModal.hidden = true;
  document.body.classList.remove("modal-open");
}

function formatQuestionAskedAt(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("ko-KR", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function renderQuestionTimestamp(message, messageIndex) {
  const previousUserMessage = message.role === "assistant"
    ? state.messages.slice(0, messageIndex).reverse().find((item) => item.role === "user")
    : null;
  const timestamp = message.role === "user"
    ? message.createdAt
    : message.questionAskedAt || previousUserMessage?.createdAt;
  const label = formatQuestionAskedAt(timestamp);
  if (!label) return null;

  const time = document.createElement("time");
  time.className = "message-question-time";
  time.dateTime = timestamp;
  time.textContent = `질문한 날짜 · ${label}`;
  time.setAttribute("aria-label", `질문한 날짜 ${label}`);
  return time;
}

function renderMessages() {
  els.messages.innerHTML = "";
  state.sourceMap.clear();

  if (state.messages.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.innerHTML = `
      <div>
        <svg class="empty-state-illustration" viewBox="0 0 96 96" width="72" height="72" fill="none" aria-hidden="true">
          <circle cx="40" cy="40" r="24" stroke="currentColor" stroke-width="4" opacity="0.35"/>
          <path d="M58 58 L78 78" stroke="currentColor" stroke-width="5" stroke-linecap="round"/>
          <path d="M30 40h20M30 48h14" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" opacity="0.6"/>
        </svg>
        <p class="empty-state-eyebrow">팀 공용 템플릿 ${TEAM_TEMPLATES.length}개</p>
        <h3>조건만 고르면 업무 질문이 완성됩니다</h3>
        <p>팀에서 자주 쓰는 업무를 선택하거나 아래 입력창에 직접 질문해 보세요.</p>
        <div class="starter-grid" aria-label="팀 공용 템플릿">
          ${state.teamTemplatesError
            ? `<p class="status-warning">${escapeHtml(state.teamTemplatesError)} 직접 질문은 계속 사용할 수 있습니다.</p>`
            : renderTeamTemplates()}
        </div>
      </div>
    `;
    els.messages.appendChild(empty);
    updateComposerShadow();
    return;
  }

  state.messages.forEach((message, messageIndex) => {
    const row = document.createElement("article");
    row.className = `message ${message.role}${message.loading || message.streaming ? " loading" : ""}`;
    if (message.role === "assistant" && message.abTest?.enabled) row.classList.add("ab-layout");

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    if (message.role === "user") {
      avatar.textContent = "YOU";
    } else {
      avatar.classList.add("assistant-avatar");
      avatar.innerHTML = `<img src="/static/assets/company-logo.png" alt="회사 로고" />`;
    }

    const bubble = document.createElement("div");
    bubble.className = "bubble";
    if (message.loading && !message.content) {
      bubble.innerHTML = `
        <span class="loading-inline">
          <span>${escapeHtml(message.progress || "요청을 준비하고 있습니다")}</span>
          <span class="loading-dots" aria-hidden="true"><i></i><i></i><i></i></span>
        </span>
      `;
      // 자료를 찾거나 정리하는 단계에서는 결과 카드 자리에 뼈대(스켈레톤)를
      // 보여줘 "곧 목록이 채워진다"는 걸 형태로 미리 알린다.
      if (message.stage === "retrieval" || message.stage === "filtering") {
        bubble.appendChild(renderIssueListSkeleton());
      }
    } else if (message.role === "assistant") {
      const assistantContent = renderAssistantContent(message.content, message.sources || [], messageIndex);
      bubble.appendChild(assistantContent);
      if (assistantContent.querySelector(".issue-table-wrap")) {
        row.classList.add("has-issue-table");
        bubble.classList.add("bubble-wide");
      }
    } else {
      bubble.textContent = message.content;
    }

    if (message.role === "user") {
      const userContent = document.createElement("div");
      userContent.className = "message-content user-message-content";
      userContent.appendChild(bubble);
      const questionTimestamp = renderQuestionTimestamp(message, messageIndex);
      if (questionTimestamp) userContent.appendChild(questionTimestamp);
      userContent.appendChild(renderMessageActions(message, messageIndex));
      row.append(userContent, avatar);
    } else {
      const content = document.createElement("div");
      content.className = "message-content";
      if (row.classList.contains("has-issue-table")) content.classList.add("wide-message-content");
      const conditionBar = renderSearchConditionChips(message, messageIndex);
      if (conditionBar) content.appendChild(conditionBar);
      if (message.abTest?.enabled && abComparisonEnabled()) {
        content.appendChild(renderAbComparison(message, bubble, messageIndex));
      } else {
        content.appendChild(bubble);
        const questionTimestamp = renderQuestionTimestamp(message, messageIndex);
        if (questionTimestamp) content.appendChild(questionTimestamp);
        appendAssistantExtras(content, message, messageIndex);
      }
      if (message.abTest?.enabled && abComparisonEnabled()) {
        const questionTimestamp = renderQuestionTimestamp(message, messageIndex);
        if (questionTimestamp) content.appendChild(questionTimestamp);
      }
      if (message.streaming && message.progress) {
        const progress = document.createElement("div");
        progress.className = "stream-progress";
        progress.innerHTML = `<span></span><em>${escapeHtml(message.progress)}</em>`;
        content.appendChild(progress);
      }
      if (!message.loading && !message.streaming) {
        content.appendChild(renderMessageActions(message, messageIndex));
      }
      row.append(avatar, content);
    }
    els.messages.appendChild(row);
  });

  els.messages.scrollTop = els.messages.scrollHeight;
  updateComposerShadow();
}

// 스크롤 위치에 따라 입력창 경계 그림자와 "대화 처음으로" 버튼을 함께 갱신한다.
// 모바일 레이아웃은 페이지 전체가 스크롤되므로 window.scrollY를 기준으로 삼는다.
function updateComposerShadow() {
  const el = els.messages;
  if (!el) return;
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= 4;
  el.classList.toggle("is-scrolled", !atBottom);
  if (els.scrollToTopBtn) {
    const pageScrolls = window.matchMedia?.("(max-width: 860px)").matches;
    const scrollOffset = pageScrolls ? window.scrollY : el.scrollTop;
    const visibleHeight = pageScrolls ? window.innerHeight : el.clientHeight;
    const revealAfter = Math.max(320, visibleHeight * 0.6);
    els.scrollToTopBtn.hidden = scrollOffset < revealAfter;
  }
}

// 다른 생성형 AI 챗 UI처럼 텍스트 버튼 대신 아이콘+라벨 조합을 쓴다.
// 외부 아이콘 폰트/라이브러리 의존성 없이 인라인 SVG(currentColor)로 구현해
// 버튼 색상(hover 등)이 아이콘에도 그대로 적용되게 한다.
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
  renderMessages();
  refreshFeedbackSummary();
  showToast(rating === "helpful" ? "도움이 되었다는 의견을 반영했어요." : "개선 의견을 저장했어요.", "success");
}

const CHART_COLORS = [
  "var(--chart-1)",
  "var(--chart-2)",
  "var(--chart-3)",
  "var(--chart-4)",
  "var(--chart-5)",
  "var(--chart-6)",
];

function truncateChartLabel(label, maxLength = 16) {
  const text = String(label ?? "");
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

// 원형(도넛) 차트: 항목별 비중을 stroke-dasharray로 구간을 나눠 그린다.
// 외부 차트 라이브러리 없이도 실제 원형 차트로 렌더링하기 위한 방식이다.
function renderDonutChart(items, { centerLabel = "", valueFormatter = (v) => v.toFixed(2) } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "donut-chart-row";
  const total = items.reduce((sum, item) => sum + item.amount, 0) || 1;

  const size = 120;
  const radius = 42;
  const strokeWidth = 18;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;
  const segments = items
    .map((item, index) => {
      const segLen = (item.amount / total) * circumference;
      const circle = `<circle cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none" stroke="${CHART_COLORS[index % CHART_COLORS.length]}" stroke-width="${strokeWidth}" stroke-dasharray="${segLen} ${circumference - segLen}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${size / 2} ${size / 2})" />`;
      offset += segLen;
      return circle;
    })
    .join("");

  const svg = document.createElement("div");
  svg.className = "donut-chart-svg";
  svg.innerHTML = `
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}" role="img" aria-label="유형별 공수 비중 도넛 차트">
      ${segments}
      <text x="${size / 2}" y="${size / 2 - 4}" text-anchor="middle" class="donut-chart-total">${escapeHtml(valueFormatter(total))}</text>
      <text x="${size / 2}" y="${size / 2 + 12}" text-anchor="middle" class="donut-chart-total-label">${escapeHtml(centerLabel)}</text>
    </svg>
  `;
  wrap.appendChild(svg);

  const legend = document.createElement("div");
  legend.className = "chart-legend";
  items.forEach((item, index) => {
    const pct = Math.round((item.amount / total) * 100);
    const row = document.createElement("div");
    row.className = "chart-legend-row";
    row.innerHTML = `
      <span class="chart-legend-swatch" style="background:${CHART_COLORS[index % CHART_COLORS.length]}"></span>
      <span>${escapeHtml(item.label)}</span>
      <span class="chart-legend-pct">${pct}%</span>
      <span class="chart-legend-value">${escapeHtml(valueFormatter(item.amount))}</span>
    `;
    legend.appendChild(row);
  });
  wrap.appendChild(legend);
  return wrap;
}

// 가로 막대 차트: SVG로 축 기준선과 막대, 값 레이블을 함께 그린다.
function renderBarChartSVG(items, { valueFormatter = (v) => v.toFixed(2) } = {}) {
  const viewWidth = 760;
  const labelWidth = 260;
  const valueAreaWidth = 62;
  const barAreaWidth = viewWidth - labelWidth - valueAreaWidth;
  const rowHeight = 34;
  const barHeight = 16;
  const topPadding = 8;
  const height = items.length * rowHeight + topPadding * 2;
  const maxAmount = Math.max(1, ...items.map((item) => item.amount));

  const rows = items
    .map((item, index) => {
      const y = topPadding + index * rowHeight + rowHeight / 2;
      const barWidth = Math.max((item.amount / maxAmount) * barAreaWidth, item.amount > 0 ? 2 : 0);
      const label = truncateChartLabel(item.label, 30);
      return `
        <text x="0" y="${y}" dominant-baseline="middle" class="bar-chart-axis-label"><title>${escapeHtml(item.label)}</title>${escapeHtml(label)}</text>
        <rect x="${labelWidth}" y="${y - barHeight / 2}" width="${barWidth}" height="${barHeight}" rx="5" fill="${CHART_COLORS[index % CHART_COLORS.length]}" />
        <text x="${labelWidth + barWidth + 8}" y="${y}" dominant-baseline="middle" class="bar-chart-value-label">${escapeHtml(valueFormatter(item.amount))}</text>
      `;
    })
    .join("");

  const svg = document.createElement("div");
  svg.innerHTML = `
    <svg class="bar-chart-svg" viewBox="0 0 ${viewWidth} ${height}" role="img" aria-label="항목별 공수 막대 차트">
      <line x1="${labelWidth}" y1="0" x2="${labelWidth}" y2="${height}" stroke="var(--line)" stroke-width="1" />
      ${rows}
    </svg>
  `;
  return svg;
}

function renderEffortChart(chart) {
  const wrap = document.createElement("div");
  wrap.className = "effort-chart";

  const statCards = document.createElement("div");
  statCards.className = "summary-stat-grid effort-chart-stats";
  [
    { label: chart.periodLabel || `${chart.year}년 ${chart.month}월`, value: chart.person },
    { label: "총 공수", value: chart.totalEffort.toFixed(2) },
    { label: "반영 이슈", value: `${chart.issueCount}건` },
  ].forEach(({ label, value }, index) => {
    const card = document.createElement("div");
    card.className = `summary-stat-card effort-stat-card effort-stat-${index + 1}`;
    card.innerHTML = `<strong>${escapeHtml(String(value))}</strong><span>${escapeHtml(label)}</span>`;
    statCards.appendChild(card);
  });
  wrap.appendChild(statCards);

  const byType = chart.byType.filter((item) => item.amount > 0);
  if (byType.length) {
    const section = document.createElement("div");
    section.className = "summary-section";
    section.innerHTML = `<h4>유형별 공수</h4>`;
    section.appendChild(renderDonutChart(byType, { centerLabel: "총 공수" }));
    wrap.appendChild(section);
  }

  if (chart.topIssues?.length) {
    const section = document.createElement("div");
    section.className = "summary-section";
    section.innerHTML = `<h4>공수가 큰 이슈</h4>`;
    const items = chart.topIssues.map(({ id, summary, amount }) => ({
      label: id ? `#${id} ${summary}` : summary,
      amount,
    }));
    section.appendChild(renderBarChartSVG(items));
    wrap.appendChild(section);
  }

  return wrap;
}

function renderEffortRankingChart(ranking) {
  const wrap = document.createElement("div");
  wrap.className = "effort-chart";

  const statCards = document.createElement("div");
  statCards.className = "summary-stat-grid effort-chart-stats";
  [
    { label: "기간", value: ranking.periodLabel },
    { label: "공수 기록 이슈", value: `${ranking.issueCount}건` },
    { label: "참여 인원", value: `${ranking.peopleCount}명` },
  ].forEach(({ label, value }) => {
    const card = document.createElement("div");
    card.className = "summary-stat-card";
    card.innerHTML = `<strong>${escapeHtml(String(value))}</strong><span>${escapeHtml(label)}</span>`;
    statCards.appendChild(card);
  });
  wrap.appendChild(statCards);

  if (ranking.topPeople?.length) {
    const section = document.createElement("div");
    section.className = "summary-section";
    section.innerHTML = `<h4>공수 순위</h4>`;
    section.appendChild(renderBarChartSVG(ranking.topPeople));
    wrap.appendChild(section);
  }

  return wrap;
}

function appendAssistantExtras(container, message, messageIndex) {
  if (message.effortChart) container.appendChild(renderEffortChart(message.effortChart));
  if (message.effortRanking) container.appendChild(renderEffortRankingChart(message.effortRanking));
  if (message.sources?.length && !shouldHideSourceList(message.content, message.sources)) {
    container.appendChild(renderSourceList(message.sources, messageIndex));
  }
  if (message.followUps?.length) {
    container.appendChild(renderFollowUps(message.followUps));
  }
  if (message.verification) {
    container.appendChild(renderVerificationPanel(message.verification, message.auditId));
  }
  if (message.playbookCandidate && ["manager", "admin"].includes(state.currentUser?.role)) {
    const action = document.createElement("section");
    action.className = "playbook-candidate-action";
    action.innerHTML = `
      <div><strong>재사용할 해결 사례인가요?</strong><span>검수 전 플레이북 초안으로 저장할 수 있습니다.</span></div>
      <button type="button" data-save-playbook-candidate data-message-index="${messageIndex}" ${message.playbookSaved ? "disabled" : ""}>
        ${message.playbookSaved ? "초안 저장됨" : "플레이북 후보 저장"}
      </button>`;
    container.appendChild(action);
  }
}

function conditionId(condition = {}) {
  if (condition.id) return condition.id;
  return {
    "고객사": "customer",
    "플랫폼": "platform",
    "담당자/인물": "people",
    "상태": "status",
    "이슈 ID": "issue",
    "화면번호": "screen",
  }[condition.label] || "date";
}

function renderSearchConditionChips(message, messageIndex) {
  const conditions = message.verification?.constraints || [];
  if (!conditions.length) return null;
  const section = document.createElement("section");
  section.className = "search-condition-bar";
  const label = document.createElement("span");
  label.className = "search-condition-label";
  label.textContent = "검색 조건";
  const list = document.createElement("div");
  list.className = "search-condition-list";
  conditions.forEach((condition) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-condition-chip";
    button.dataset.removeCondition = encodeURIComponent(JSON.stringify(condition));
    button.dataset.messageIndex = String(messageIndex);
    button.setAttribute("aria-label", `${condition.label} ${condition.value} 조건을 제거하고 다시 검색`);
    button.title = "이 조건을 제외하고 다시 검색";
    button.innerHTML = `<span>${escapeHtml(condition.label)}</span><strong>${escapeHtml(condition.value)}</strong><i aria-hidden="true">×</i>`;
    list.appendChild(button);
  });
  section.append(label, list);
  return section;
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function questionWithoutCondition(question, condition) {
  let cleaned = String(question || "");
  const values = [...new Set([...(condition.values || []), condition.value].filter(Boolean))]
    .sort((a, b) => String(b).length - String(a).length);
  values.forEach((value) => {
    cleaned = cleaned.replace(new RegExp(escapeRegExp(value), "gi"), " ");
  });
  const cleanupPatterns = {
    customer: /고객사\s*[:=]?/gi,
    customer_exclude: /(?:고객사\s*)?(?:은|는|을|를)?\s*(?:빼고|제외(?:하고)?|말고|아닌|없이)/gi,
    platform: /플랫폼\s*[:=]?/gi,
    platform_exclude: /(?:플랫폼\s*)?(?:은|는|을|를)?\s*(?:빼고|제외(?:하고)?|말고|아닌|없이)/gi,
    people: /(?:담당자|담당|에게|한테)\s*[:=]?/gi,
    status: /상태(?:가|는|은|이)?\s*[:=]?/gi,
    status_exclude: /(?:상태\s*)?(?:은|는|을|를)?\s*(?:빼고|제외(?:하고)?|말고|아닌|없이)/gi,
    issue: /이슈\s*(?:ID)?\s*[:=#]?/gi,
    screen: /화면(?:번호)?\s*[:=#]?/gi,
    date: /(?:날짜|일자)\s*[:=]?/gi,
  };
  const pattern = cleanupPatterns[conditionId(condition)];
  if (pattern) cleaned = cleaned.replace(pattern, " ");
  cleaned = cleaned
    .replace(/\[\s*\]|\(\s*\)/g, " ")
    .replace(/\s+(에서|에게|한테|의)\b/g, " ")
    .replace(/\s{2,}/g, " ")
    .replace(/^[,·|\s]+|[,·|\s]+$/g, "")
    .trim();
  return cleaned || "관련 자료를 다시 검색해줘";
}

async function rerunWithoutCondition(messageIndex, condition) {
  if (state.busy) return;
  let userIndex = messageIndex - 1;
  while (userIndex >= 0 && state.messages[userIndex].role !== "user") userIndex -= 1;
  if (userIndex < 0) return;
  const nextQuestion = questionWithoutCondition(state.messages[userIndex].content, condition);
  const questionAskedAt = new Date().toISOString();
  state.messages.push({ role: "user", content: nextQuestion, createdAt: questionAskedAt });
  persistConversations();
  await requestAssistant(nextQuestion, questionAskedAt);
}

function renderAbEvaluation(evaluation = {}) {
  const section = document.createElement("section");
  section.className = "ab-evaluation";
  const winner = evaluation.winner;
  const winnerText = winner === "tie" ? "두 답변 유사" : `${winner} 답변 우세`;
  const header = document.createElement("div");
  header.className = "ab-evaluation-header";
  header.innerHTML = `<strong>자동 비교</strong><em>${escapeHtml(winnerText)}</em>`;

  const scores = document.createElement("div");
  scores.className = "ab-score-grid";
  ["A", "B"].forEach((key) => {
    const result = evaluation[`variant${key}`] || {};
    const item = document.createElement("div");
    item.className = `ab-score-item${winner === key ? " is-winner" : ""}`;
    item.innerHTML = `<span>${key}</span><strong>${Number(result.score || 0)}점</strong>`;
    scores.appendChild(item);
  });

  const details = document.createElement("details");
  details.className = "ab-evaluation-details";
  const summary = document.createElement("summary");
  summary.textContent = "평가 기준과 확인 사항";
  const body = document.createElement("div");
  body.className = "ab-evaluation-body";
  ["A", "B"].forEach((key) => {
    const result = evaluation[`variant${key}`] || {};
    const metricLabels = evaluation.metricLabels || {};
    const block = document.createElement("div");
    block.className = "ab-evaluation-variant";
    const metricText = Object.entries(result.metrics || {})
      .map(([metric, score]) => `${metricLabels[metric] || metric} ${score}점`)
      .join(" · ");
    const failures = result.failureReasons || [];
    block.innerHTML = `
      <strong>${key} 답변</strong>
      <p>${escapeHtml(metricText || "평가 데이터 없음")}</p>
      <ul>${(failures.length ? failures : ["자동 검사에서 특이사항이 발견되지 않았습니다."])
        .map((reason) => `<li>${escapeHtml(reason)}</li>`)
        .join("")}</ul>
    `;
    body.appendChild(block);
  });
  details.append(summary, body);
  section.append(header, scores, details);
  return section;
}

function renderAbComparison(message, aBubble, messageIndex) {
  const comparison = document.createElement("section");
  comparison.className = "ab-comparison";

  const tabs = document.createElement("div");
  tabs.className = "ab-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "답변 처리 방식 비교");

  const variantA = message.abTest.variantA || {};
  const variantB = message.abTest.variantB || {};
  const aTab = document.createElement("button");
  const bTab = document.createElement("button");
  aTab.type = "button";
  bTab.type = "button";
  aTab.className = "ab-tab";
  bTab.className = "ab-tab";
  aTab.setAttribute("role", "tab");
  bTab.setAttribute("role", "tab");
  aTab.setAttribute("aria-selected", "true");
  bTab.setAttribute("aria-selected", "false");
  aTab.textContent = `A · ${variantA.label || "전처리 적용"}`;
  bTab.textContent = `B · ${variantB.label || "기본 검색 연동"}`;
  tabs.append(aTab, bTab);

  const aPanel = document.createElement("div");
  const bPanel = document.createElement("div");
  aPanel.className = "ab-panel";
  bPanel.className = "ab-panel";
  aPanel.setAttribute("role", "tabpanel");
  bPanel.setAttribute("role", "tabpanel");
  // hidden 속성은 브라우저 기본 스타일이 display:none !important로 걸어서 넓은 화면의
  // 2단 비교 레이아웃(styles.css의 min-width:1100px)에서 CSS로 다시 펼 수 없다.
  // 탭 전환은 클래스로 제어해 좁은 화면에서만 한쪽을 접는다.
  bPanel.classList.add("is-collapsed");

  const aTitle = document.createElement("h3");
  const bTitle = document.createElement("h3");
  aTitle.className = "ab-panel-title";
  bTitle.className = "ab-panel-title";
  aTitle.textContent = `A · ${variantA.label || "전처리 적용"}`;
  bTitle.textContent = `B · ${variantB.label || "기본 검색 연동"}`;

  const aNote = document.createElement("p");
  aNote.className = "ab-method-note";
  aNote.textContent = variantA.description || "사내 검색과 검증을 적용한 답변입니다.";
  aPanel.append(aTitle, aNote, aBubble);

  const bNote = document.createElement("p");
  bNote.className = `ab-method-note${variantB.status === "error" ? " is-error" : ""}`;
  bNote.textContent = variantB.description || "Mantis·Notion 기본 검색 결과만 모델에 전달한 답변입니다.";
  const baselineSources = variantB.sources || [];
  const bBubble = document.createElement("div");
  bBubble.className = "bubble ab-baseline-bubble";
  bBubble.appendChild(renderAssistantContent(
    variantB.content || "비교 응답이 없습니다.",
    baselineSources,
    `${messageIndex}-baseline`,
  ));
  bPanel.append(bTitle, bNote, bBubble);
  if (baselineSources.length && !baselineSources.every((source) => source.type === "notion")) {
    bPanel.appendChild(renderSourceList(baselineSources, messageIndex, "baseline"));
  }

  const select = (selected) => {
    const showA = selected === "a";
    aTab.setAttribute("aria-selected", String(showA));
    bTab.setAttribute("aria-selected", String(!showA));
    aPanel.classList.toggle("is-collapsed", !showA);
    bPanel.classList.toggle("is-collapsed", showA);
  };
  aTab.addEventListener("click", () => select("a"));
  bTab.addEventListener("click", () => select("b"));

  const panels = document.createElement("div");
  panels.className = "ab-panels";
  panels.append(aPanel, bPanel);
  const sharedExtras = document.createElement("div");
  sharedExtras.className = "ab-shared-extras";
  appendAssistantExtras(sharedExtras, message, messageIndex);

  comparison.append(tabs);
  if (message.abTest.evaluation) {
    comparison.appendChild(renderAbEvaluation(message.abTest.evaluation));
  }
  comparison.append(panels);
  if (sharedExtras.childElementCount) comparison.append(sharedExtras);
  return comparison;
}

function hasIssueSummaryList(content) {
  return String(content || "")
    .split("\n")
    .some((line) => parseIssueSummaryLine(line));
}

function shouldHideSourceList(content, sources = []) {
  const text = String(content || "");
  if (sources.length && sources.every((source) => source.type === "notion")) return true;
  if (hasIssueSummaryList(text)) return true;
  return sources.some((source) => {
    if (!source.issue_id) return false;
    const issueIdPattern = new RegExp(`#?0*${String(source.issue_id)}\\b`);
    return issueIdPattern.test(text);
  });
}

// 상태 라벨을 색상 톤 클래스로 매핑한다. 부분 문자열 기준이라 "종료된 이슈",
// "결재완료(팀장)"처럼 변형된 라벨도 자연스럽게 커버된다.
function statusToneClass(label) {
  const text = String(label || "");
  if (/정보 ?부족/.test(text)) return "status-red";
  if (/해결|개발완료|^완료|결재완료/.test(text)) return "status-green";
  if (/할당/.test(text)) return "status-amber";
  if (/확인|신규|진행|등록됨/.test(text)) return "status-blue";
  if (/검토|결재요청/.test(text)) return "status-purple";
  if (/종료|보관/.test(text)) return "status-neutral";
  return "status-neutral";
}

const CALLOUT_ICONS = {
  info: '<svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="10" cy="10" r="7.2"/><path d="M10 9v4.2M10 6.4v.2"/></svg>',
  warning: '<svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 3.2 17.4 16H2.6z"/><path d="M10 8.4v3.4M10 14.2v.2"/></svg>',
};

// "참고:"/"데이터 기준:" 같은 안내문을 일반 문단 대신 아이콘 콜아웃으로 그린다.
function calloutToneFor(paragraphText) {
  if (paragraphText.startsWith("참고:")) return "warning";
  if (paragraphText.startsWith("데이터 기준:")) return "info";
  if (paragraphText.startsWith("요청한 ") && paragraphText.includes("건입니다")) return "info";
  return null;
}

// **굵게**, `코드` 인라인 마크다운을 벗겨내지 않고 실제 요소로 그린다.
// innerHTML 대신 textContent 기반 노드 조립이라 XSS 걱정 없이 안전하다.
function appendInlineMarkdown(target, text, sources = [], messageIndex = 0) {
  const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`|#\d{4,7}\b)/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) {
      target.appendChild(document.createTextNode(text.slice(last, match.index)));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      target.appendChild(strong);
    } else if (token.startsWith("#")) {
      // 답변에 언급된 이슈ID가 실제 근거 목록(sources)에 있을 때만 클릭 가능한
      // 참조로 만든다. 무관한 숫자를 링크로 오인시키지 않기 위한 안전장치.
      const issueId = Number(token.slice(1));
      const source = sources.find((item) => Number(item.issue_id) === issueId);
      if (source) {
        const key = `inline-${messageIndex}-${issueId}`;
        state.sourceMap.set(key, source);
        const ref = document.createElement("button");
        ref.type = "button";
        ref.className = "inline-issue-ref";
        ref.dataset.sourceKey = key;
        ref.textContent = token;
        target.appendChild(ref);
      } else {
        target.appendChild(document.createTextNode(token));
      }
    } else {
      const code = document.createElement("code");
      code.className = "inline-code";
      code.textContent = token.slice(1, -1);
      target.appendChild(code);
    }
    last = match.index + token.length;
  }
  if (last < text.length) {
    target.appendChild(document.createTextNode(text.slice(last)));
  }
}

// "검색 기준: 고객사=X, 플랫폼=Y, 정렬: ..." 줄을 태그 칩 목록으로 그린다.
// "담당자=김a, 이b"처럼 값 안에 쉼표가 있는 경우, '='도 '정렬'도 없는 조각은
// 직전 칩의 값에 이어 붙인다.
function renderCriteriaChips(criteriaText) {
  const wrap = document.createElement("div");
  wrap.className = "criteria-chips";
  const label = document.createElement("span");
  label.className = "criteria-chips-label";
  label.textContent = "검색 기준";
  wrap.appendChild(label);

  const body = criteriaText.replace(/^검색 기준:\s*/, "");
  const chips = [];
  body.split(/,\s*/).forEach((segment) => {
    const trimmed = segment.trim();
    if (!trimmed) return;
    if (trimmed.includes("=") || trimmed.startsWith("정렬") || !chips.length) {
      chips.push(trimmed);
    } else {
      chips[chips.length - 1] += `, ${trimmed}`;
    }
  });

  chips.forEach((chipText) => {
    const chip = document.createElement("span");
    chip.className = "criteria-chip";
    const eqIndex = chipText.indexOf("=");
    const colonIndex = chipText.indexOf(":");
    const splitIndex = eqIndex > 0 ? eqIndex : colonIndex;
    if (splitIndex > 0) {
      const small = document.createElement("small");
      small.textContent = chipText.slice(0, splitIndex).trim();
      chip.appendChild(small);
      chip.appendChild(document.createTextNode(chipText.slice(splitIndex + 1).trim()));
    } else {
      chip.textContent = chipText;
    }
    wrap.appendChild(chip);
  });
  return wrap;
}

// 답변 텍스트 블록 하나를 콜아웃/검색기준 칩/제목/리스트/문단 중 알맞은
// 형태로 그린다. 마크다운 표기를 벗겨서 버리는 대신 실제 스타일로 살린다.
function appendAnswerParagraph(container, rawBlock, sources = [], messageIndex = 0) {
  const cleaned = cleanMarkdown(rawBlock).trim();
  if (!cleaned) return;

  const tone = calloutToneFor(cleaned);
  if (tone) {
    const callout = document.createElement("div");
    callout.className = `answer-callout callout-${tone}`;
    callout.innerHTML = `${CALLOUT_ICONS[tone]}<span></span>`;
    callout.querySelector("span").textContent = cleaned;
    container.appendChild(callout);
    return;
  }

  if (cleaned.startsWith("검색 기준:")) {
    container.appendChild(renderCriteriaChips(cleaned));
    return;
  }

  const rawLines = String(rawBlock)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  let listBuffer = [];
  let listTag = "ul";
  let paragraphBuffer = [];

  const flushList = () => {
    if (!listBuffer.length) return;
    const list = document.createElement(listTag);
    list.className = "answer-list";
    listBuffer.forEach((itemText) => {
      const item = document.createElement("li");
      appendInlineMarkdown(item, itemText, sources, messageIndex);
      list.appendChild(item);
    });
    container.appendChild(list);
    listBuffer = [];
  };

  const flushParagraph = () => {
    if (!paragraphBuffer.length) return;
    const paragraph = document.createElement("p");
    paragraphBuffer.forEach((lineText, lineIndex) => {
      if (lineIndex > 0) paragraph.appendChild(document.createElement("br"));
      appendInlineMarkdown(paragraph, lineText, sources, messageIndex);
    });
    container.appendChild(paragraph);
    paragraphBuffer = [];
  };

  rawLines.forEach((line) => {
    const headingMatch = line.match(/^#{1,6}\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const heading = document.createElement("h4");
      heading.className = "answer-heading";
      appendInlineMarkdown(heading, headingMatch[1], sources, messageIndex);
      container.appendChild(heading);
      return;
    }
    const bulletMatch = line.match(/^[-*•]\s+(.+)$/);
    if (bulletMatch) {
      flushParagraph();
      if (listBuffer.length && listTag !== "ul") flushList();
      listTag = "ul";
      listBuffer.push(bulletMatch[1]);
      return;
    }
    const numberedMatch = line.match(/^\d{1,2}\.\s+(.+)$/);
    if (numberedMatch) {
      flushParagraph();
      if (listBuffer.length && listTag !== "ol") flushList();
      listTag = "ol";
      listBuffer.push(numberedMatch[1]);
      return;
    }
    flushList();
    paragraphBuffer.push(line);
  });
  flushParagraph();
  flushList();
}

function renderAssistantContent(content, sources = [], messageIndex = 0) {
  const wrap = document.createElement("div");
  wrap.className = "assistant-answer";
  if (sources.length && sources.every((source) => source.type === "notion")) {
    wrap.appendChild(renderNotionAnswer(content, sources, messageIndex));
    return wrap;
  }

  const issueDetail = parseIssueDetailContent(content);
  if (issueDetail) {
    wrap.appendChild(renderIssueDetailCard(issueDetail, sources, messageIndex));
    return wrap;
  }

  const lines = String(content || "").split("\n");
  let textBuffer = [];
  let issueBuffer = [];

  const flushText = () => {
    const block = textBuffer.join("\n").trim();
    textBuffer = [];
    if (!block) return;

    block.split(/\n{2,}/).forEach((part) => {
      appendAnswerParagraph(wrap, part, sources, messageIndex);
    });
  };

  const flushIssues = () => {
    if (!issueBuffer.length) return;
    wrap.appendChild(renderIssueSummaryList(issueBuffer, sources, messageIndex));
    issueBuffer = [];
  };

  let index = 0;
  while (index < lines.length) {
    const line = lines[index];

    // "답변 스타일: 표 중심"으로 모델이 GFM 마크다운 표를 낼 때, 이걸 그냥
    // 문단으로 흘리면 "| a | b | |---|---|" 같은 파이프 문자가 그대로 노출된다.
    // 헤더 행 다음에 구분선(---) 행이 오는 표준 패턴을 감지해 실제 <table>로 그린다.
    if (
      isMarkdownTableRow(line) &&
      index + 1 < lines.length &&
      isMarkdownTableSeparatorRow(lines[index + 1])
    ) {
      const issue = parseIssueSummaryLine(line);
      if (!issue) {
        flushText();
        flushIssues();
        const headerCells = splitMarkdownTableRow(line);
        const bodyRows = [];
        let cursor = index + 2;
        while (cursor < lines.length && isMarkdownTableRow(lines[cursor])) {
          bodyRows.push(splitMarkdownTableRow(lines[cursor]));
          cursor += 1;
        }
        wrap.appendChild(renderMarkdownTable(headerCells, bodyRows));
        index = cursor;
        continue;
      }
    }

    const issue = parseIssueSummaryLine(line);
    if (issue) {
      flushText();
      issueBuffer.push(issue);
      index += 1;
      continue;
    }
    flushIssues();
    textBuffer.push(line);
    index += 1;
  }

  flushText();
  flushIssues();

  const hasTable = Array.from(wrap.children).some(
    (child) => child.className === "markdown-table-wrap",
  );
  if (!hasTable) {
    const inlineTable = extractInlineMarkdownTable(content);
    if (inlineTable) {
      wrap.innerHTML = "";
      wrap.appendChild(renderMarkdownTable(inlineTable.headerCells, inlineTable.bodyRows));
      if (inlineTable.trailingText) {
        const paragraph = document.createElement("p");
        paragraph.textContent = cleanMarkdown(inlineTable.trailingText).trim();
        wrap.appendChild(paragraph);
      }
      return wrap;
    }
  }

  if (!wrap.children.length) {
    const paragraph = document.createElement("p");
    paragraph.textContent = String(content || "");
    wrap.appendChild(paragraph);
  }
  return wrap;
}

function parseIssueDetailContent(content) {
  const text = String(content || "").trim();
  const titleMatch = text.match(/^\*\*#?(\d+)\s+(.+?)\*\*$/m) || text.match(/^#?(\d+)\s+(.+)$/m);
  const contentMarker = text.match(/^\*\*내용\*\*$/m);
  if (!titleMatch || !contentMarker) return null;

  const beforeTitle = text.slice(0, titleMatch.index).trim();
  const afterTitle = text.slice((titleMatch.index || 0) + titleMatch[0].length, contentMarker.index).trim();
  const body = text.slice((contentMarker.index || 0) + contentMarker[0].length).trim();
  const meta = {};

  afterTitle.split("\n").forEach((line) => {
    const match = line.match(/^-\s*([^:]+):\s*(.*)$/);
    if (!match) return;
    meta[match[1].trim()] = match[2].trim();
  });

  return {
    criteria: cleanMarkdown(beforeTitle),
    id: titleMatch[1],
    title: cleanMarkdown(titleMatch[2]),
    project: meta["프로젝트"] || "",
    status: meta["상태"] || "",
    handler: meta["담당자"] || "",
    createdAt: meta["등록일"] || "",
    screenNumber: meta["화면번호"] || "",
    dateFields: Object.fromEntries(
      Object.entries(meta).filter(([key]) => !["프로젝트", "상태", "담당자", "등록일", "화면번호"].includes(key)),
    ),
    body,
  };
}

function renderIssueDetailCard(issue, sources = [], messageIndex = 0) {
  const source = sources.find((item) => Number(item.issue_id) === Number(issue.id));
  const card = document.createElement("article");
  card.className = "issue-detail-card";

  if (issue.criteria) {
    // 도입 문장과 "검색 기준:" 줄이 섞여 있다. 줄 단위로 블록 렌더러에 태워
    // 도입문은 문단으로, 검색 기준은 칩으로 각각 그린다.
    issue.criteria.split("\n").forEach((line) => {
      appendAnswerParagraph(card, line, sources, messageIndex);
    });
  }

  const sectionLabel = document.createElement("div");
  sectionLabel.className = "issue-section-label";
  sectionLabel.textContent = "이슈 상세 정보";
  card.appendChild(sectionLabel);

  const header = document.createElement("header");
  header.className = "issue-detail-header";
  header.innerHTML = `
    <div>
      <span class="issue-detail-id">#${escapeHtml(issue.id)}</span>
      <h3>${escapeHtml(issue.title)}</h3>
    </div>
    ${issue.status ? `<em class="status-badge ${statusToneClass(issue.status)}">${escapeHtml(issue.status)}</em>` : ""}
  `;
  card.appendChild(header);

  const metaItems = [
    ["프로젝트", issue.project],
    ["담당자", issue.handler],
    ["등록일", issue.createdAt],
    ["화면번호", issue.screenNumber],
    ...Object.entries(issue.dateFields || {}),
  ].filter(([, value]) => value && value !== "미상");

  if (metaItems.length) {
    const metaGrid = document.createElement("dl");
    metaGrid.className = "issue-detail-meta";
    metaItems.forEach(([label, value]) => {
      const item = document.createElement("div");
      item.innerHTML = `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`;
      metaGrid.appendChild(item);
    });
    card.appendChild(metaGrid);
  }

  const body = document.createElement("section");
  body.className = "issue-detail-body";
  const bodyTitle = document.createElement("h4");
  bodyTitle.textContent = "요청 내용";
  body.appendChild(bodyTitle);

  const bodyText = document.createElement("div");
  bodyText.className = "issue-detail-text";
  issue.body.split(/\n{2,}/).forEach((part) => {
    const paragraphText = part.trim();
    if (!paragraphText) return;
    const paragraph = document.createElement("p");
    paragraph.textContent = paragraphText;
    bodyText.appendChild(paragraph);
  });
  body.appendChild(bodyText);
  card.appendChild(body);

  const notes = buildIssueReferenceNotes(issue, source, sources);
  if (notes.length) {
    const noteSection = document.createElement("section");
    noteSection.className = "issue-reference-notes";
    noteSection.innerHTML = `<h4>참고 사항</h4>`;
    const noteList = document.createElement("ul");
    notes.forEach((note) => {
      const item = document.createElement("li");
      item.textContent = note;
      noteList.appendChild(item);
    });
    noteSection.appendChild(noteList);
    card.appendChild(noteSection);
  }

  if (source) {
    const key = `detail-${messageIndex}-${issue.id}`;
    state.sourceMap.set(key, source);
    const action = document.createElement("button");
    action.className = "issue-detail-action";
    action.type = "button";
    action.dataset.sourceKey = key;
    action.textContent = "Mantis 상세 보기";
    card.appendChild(action);
  }

  return card;
}

function buildIssueReferenceNotes(issue, source, sources = []) {
  const notes = [];
  const status = issue.status || source?.status || "";
  if (status) {
    notes.push(`현재 상태는 ${status}입니다.`);
  }
  if (issue.screenNumber && issue.screenNumber !== "미상") {
    notes.push(`화면번호 ${issue.screenNumber} 기준으로 연관 이슈를 추가 조회할 수 있습니다.`);
  }
  const dateLabels = Object.keys(issue.dateFields || {});
  if (dateLabels.length) {
    notes.push(`${dateLabels.join(", ")} 일정 필드가 함께 확인되었습니다.`);
  }
  const relatedCount = sources.filter((item) => item.issue_id && Number(item.issue_id) !== Number(issue.id)).length;
  if (relatedCount > 0) {
    notes.push(`아래 관련 자료에서 함께 조회된 이슈 ${relatedCount}건을 확인할 수 있습니다.`);
  }
  return notes.slice(0, 3);
}

function renderFollowUps(followUps = []) {
  const section = document.createElement("section");
  section.className = "follow-up-panel";
  section.innerHTML = `<h4>후속 질문</h4>`;
  const list = document.createElement("div");
  list.className = "follow-up-list";

  followUps.forEach((question) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "follow-up-chip";
    button.dataset.followUp = question;
    button.textContent = question;
    list.appendChild(button);
  });

  section.appendChild(list);
  return section;
}

function cleanMarkdown(value) {
  return String(value)
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[*-]\s+/gm, "• ")
    .replace(/[ \t]+\n/g, "\n");
}

function parseIssueSummaryLine(line) {
  const trimmed = String(line || "").trim();
  if (!/^\d+\.\s/.test(trimmed) || !/\*?\*?ID\*?\*?\s*:/.test(trimmed)) {
    return null;
  }

  const withoutNumber = trimmed.replace(/^\d+\.\s*/, "");
  const parts = cleanMarkdown(withoutNumber).split("|");
  const issue = {};

  parts.forEach((part) => {
    const [rawLabel, ...rawValue] = part.split(":");
    if (!rawLabel || rawValue.length === 0) return;
    const label = rawLabel.trim();
    const value = rawValue.join(":").trim();
    if (label === "ID") issue.id = value.replace(/^#/, "");
    if (label === "프로젝트") issue.project = value;
    if (label === "제목") issue.title = value;
    if (label === "상태") issue.status = value;
    if (label === "담당자") issue.handler = value;
    if (label === "등록일") issue.createdAt = value;
  });

  return issue.id && issue.title ? issue : null;
}

function renderIssueListSkeleton(rows = 3) {
  const list = document.createElement("div");
  list.className = "issue-summary-list skeleton-list";
  for (let i = 0; i < rows; i += 1) {
    const row = document.createElement("div");
    row.className = "issue-summary-row skeleton-row";
    row.innerHTML = `
      <div class="skeleton-line skeleton-line-title"></div>
      <div class="skeleton-line skeleton-line-meta"></div>
    `;
    list.appendChild(row);
  }
  return list;
}

function issueValue(issue, source, key, fallback = "") {
  const sourceValue = source?.[key];
  const localValue = issue?.[key];
  return sourceValue || localValue || fallback;
}

function issuePriorityLabel(issue, source) {
  return (
    issueValue(issue, source, "priority") ||
    issueValue(issue, source, "severity") ||
    issueValue(issue?.detail || {}, source?.detail || {}, "priority") ||
    issueValue(issue?.detail || {}, source?.detail || {}, "severity") ||
    "-"
  );
}

function renderIssueSummaryList(issues, sources = [], messageIndex = 0) {
  const wrapper = document.createElement("div");
  wrapper.className = "issue-table-wrap";

  const table = document.createElement("table");
  table.className = "issue-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["이슈 번호", "프로젝트", "제목", "상태", "중요도", "담당자", "등록일"].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  issues.forEach((issue) => {
    const source = sources.find((item) => Number(item.issue_id) === Number(issue.id));
    const row = document.createElement("tr");
    row.className = "issue-table-row";
    if (source) {
      const key = `answer-${messageIndex}-${issue.id}`;
      state.sourceMap.set(key, source);
      row.classList.add("issue-table-clickable");
      row.dataset.sourceKey = key;
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", `#${issue.id} 이슈 상세 보기`);
    }

    const idCell = document.createElement("td");
    idCell.className = "issue-table-id";
    idCell.textContent = `#${issue.id}`;

    const projectCell = document.createElement("td");
    projectCell.className = "issue-table-project";
    projectCell.textContent = issueValue(issue, source, "project", "미상");

    const titleCell = document.createElement("td");
    titleCell.className = "issue-table-title";
    titleCell.textContent = issueValue(issue, source, "title") || issue.summary || "제목 없음";

    const statusCell = document.createElement("td");
    const statusValue = issueValue(issue, source, "status", "상태 미상");
    if (statusValue) {
      const status = document.createElement("span");
      status.className = `status-badge ${statusToneClass(statusValue)}`;
      status.textContent = statusValue;
      statusCell.appendChild(status);
    }

    const priorityCell = document.createElement("td");
    priorityCell.className = "issue-table-priority";
    priorityCell.textContent = issuePriorityLabel(issue, source);

    const handlerCell = document.createElement("td");
    handlerCell.textContent = issueValue(issue, source, "handler", "담당자 미지정");

    const dateCell = document.createElement("td");
    dateCell.className = "issue-table-date";
    dateCell.textContent = issueValue(issue, source, "created_at") || issue.createdAt || "미상";

    row.append(idCell, projectCell, titleCell, statusCell, priorityCell, handlerCell, dateCell);
    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  wrapper.appendChild(table);
  return wrapper;
}

function renderSourceList(sources, messageIndex, keyPrefix = "source") {
  const wrap = document.createElement("div");
  wrap.className = "source-list";
  const isLongList = sources.length > 5;
  const panelId = `${keyPrefix}-panel-${messageIndex}`;
  const sourceLabel = sources.every((source) => source.type === "notion") ? "관련 Notion" : "관련 자료";
  const toggle = document.createElement("button");
  toggle.className = "source-list-toggle";
  toggle.type = "button";
  toggle.dataset.sourceToggle = panelId;
  toggle.setAttribute("aria-expanded", String(!isLongList));
  toggle.innerHTML = `
    <span>${sourceLabel} <strong>${sources.length}</strong>건</span>
    <em>${isLongList ? "펼치기" : "접기"}</em>
  `;

  const panel = document.createElement("div");
  panel.id = panelId;
  panel.className = "source-list-panel";
  panel.hidden = isLongList;

  wrap.appendChild(toggle);

  sources.forEach((source, sourceIndex) => {
    const key = `${keyPrefix}-${messageIndex}-${sourceIndex}`;
    state.sourceMap.set(key, source);
    const button = document.createElement("button");
    button.className = "related-issue-item";
    button.type = "button";
    button.dataset.sourceKey = key;
    const typeLabel = source.type === "notion" ? "Notion" : source.type === "chroma" ? "ChromaDB" : "Mantis";
    const primaryId = source.type === "notion" ? (source.database_label || "Notion") : `#${source.issue_id || "-"}`;
    const secondary = source.type === "notion"
      ? [source.status, source.owner].filter(Boolean).join(" · ") || "Notion DB"
      : source.project || "프로젝트 미상";
    button.innerHTML = `
      <div class="related-issue-main">
        <div class="related-issue-top">
          <strong>${escapeHtml(primaryId)}</strong>
          <span>${escapeHtml(secondary)}</span>
          <em>${escapeHtml(typeLabel)}</em>
        </div>
        <h4>${escapeHtml(source.title || "제목 없음")}</h4>
      </div>
      <span class="related-issue-action">상세</span>
    `;
    panel.appendChild(button);
  });
  wrap.appendChild(panel);
  return wrap;
}

function verificationStatusLabel(status) {
  if (status === "pass") return "통과";
  if (status === "warning") return "확인 필요";
  if (status === "review") return "검토 필요";
  if (status === "na") return "해당 없음";
  return "불일치";
}

function renderVerificationPanel(report, auditId = "") {
  const details = document.createElement("details");
  details.className = `verification-panel verification-${report.status || "warning"}`;
  const completed = report.completedDimensions ?? 1;
  const applicable = report.applicableDimensions ?? 1;
  const summary = document.createElement("summary");
  summary.innerHTML = `
    <span class="verification-title">
      <strong>검증 결과</strong>
      <em>${escapeHtml(String(completed))}/${escapeHtml(String(applicable))} 영역 판정</em>
    </span>
    <span class="verification-state">${verificationStatusLabel(report.status)}</span>
  `;
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "verification-body";
  if (report.constraints?.length) {
    const chips = document.createElement("div");
    chips.className = "verification-chips";
    report.constraints.forEach((item) => {
      const chip = document.createElement("span");
      chip.innerHTML = `<small>${escapeHtml(item.label)}</small>${escapeHtml(item.value)}`;
      chips.appendChild(chip);
    });
    body.appendChild(chips);
  }

  const dimensions = report.dimensions?.length
    ? report.dimensions
    : [{ id: "retrieval", label: "검색·근거 일치", score: report.score, status: report.status, checks: report.checks || [] }];
  const dimensionGrid = document.createElement("div");
  dimensionGrid.className = "verification-dimensions";
  dimensions.forEach((dimension) => {
    const section = document.createElement("section");
    section.className = `verification-dimension dimension-${dimension.status}`;
    const scoreLabel = dimension.score === null || dimension.score === undefined
      ? verificationStatusLabel(dimension.status)
      : `${dimension.score}점`;
    section.innerHTML = `
      <header>
        <div><strong>${escapeHtml(dimension.label)}</strong><small>${escapeHtml(dimension.description || "")}</small></div>
        <em>${escapeHtml(scoreLabel)}</em>
      </header>
    `;
    if (dimension.checks?.length) {
      const checks = document.createElement("div");
      checks.className = "verification-checks";
      dimension.checks.forEach((check) => {
        const row = document.createElement("div");
        row.className = `verification-check check-${check.status}`;
        row.innerHTML = `
          <span class="check-mark" aria-hidden="true"></span>
          <strong>${escapeHtml(check.label)}</strong>
          <span>${escapeHtml(check.detail)}</span>
        `;
        checks.appendChild(row);
      });
      section.appendChild(checks);
    }
    dimensionGrid.appendChild(section);
  });
  body.appendChild(dimensionGrid);

  if (report.trace?.length) {
    const trace = document.createElement("div");
    trace.className = "verification-trace";
    trace.innerHTML = `<strong>조회 경로</strong>`;
    report.trace.forEach((stage) => {
      const item = document.createElement("span");
      item.className = stage.status === "error" ? "trace-error" : "";
      item.textContent = `${stage.label} ${stage.count}건`;
      trace.appendChild(item);
    });
    body.appendChild(trace);
  }

  const footer = document.createElement("div");
  footer.className = "verification-footer";
  footer.innerHTML = `
    <span>${escapeHtml(report.note || "")}</span>
    <code>${escapeHtml(auditId ? `LOG ${auditId.slice(0, 8)}` : "")}</code>
  `;
  body.appendChild(footer);
  details.appendChild(body);
  return details;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function openChromaModal({ issueId, title, content, meta }) {
  els.chromaModalMeta.textContent = meta || `이슈 상세 · #${issueId}`;
  els.chromaModalTitle.textContent = title || "이슈 상세";
  els.chromaModalContent.replaceChildren(renderDetailContent(content || "내용이 없습니다.", title || ""));
  els.chromaModalContent.scrollTop = 0;
  els.chromaModal.hidden = false;
  document.body.classList.add("modal-open");
  els.chromaModalClose.focus();
}

function parseDetailSections(text) {
  const sections = [];
  let current = { title: "", pairs: [], lines: [] };
  const pushCurrent = () => {
    while (current.lines.length && !current.lines[current.lines.length - 1]) current.lines.pop();
    if (current.title || current.pairs.length || current.lines.length) sections.push(current);
  };
  String(text)
    .split(/\n/)
    .forEach((rawLine) => {
      const line = rawLine.trim();
      const sectionMatch = line.match(/^\[(.+)\]$/);
      if (sectionMatch) {
        pushCurrent();
        current = { title: sectionMatch[1].trim(), pairs: [], lines: [] };
        return;
      }
      const pairMatch = line.match(/^[-•]\s*([^:：]{1,30}?)\s*[:：]\s*(.*)$/);
      if (pairMatch) {
        current.pairs.push([pairMatch[1].trim(), pairMatch[2].trim() || "미상"]);
        return;
      }
      if (line || current.lines.length) current.lines.push(line);
    });
  pushCurrent();
  return sections;
}

function detailBadgeClass(value) {
  if (/종료|해결|완료/.test(value)) return "detail-badge--positive";
  if (/신규|확인|할당|진행/.test(value)) return "detail-badge--info";
  if (/부족|피드백|보류|미상/.test(value)) return "detail-badge--warning";
  return "detail-badge--neutral";
}

function buildDetailValue(key, value) {
  const holder = document.createElement("dd");
  holder.className = "detail-value";
  if (/상태/.test(key)) {
    const badge = document.createElement("span");
    badge.className = `detail-badge ${detailBadgeClass(value)}`;
    badge.textContent = value;
    holder.appendChild(badge);
  } else if (/^https?:\/\//.test(value)) {
    const link = document.createElement("a");
    link.className = "detail-link";
    link.href = value;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = value;
    holder.appendChild(link);
  } else {
    holder.textContent = value;
  }
  return holder;
}

function renderDetailContent(text, headerTitle) {
  const fragment = document.createDocumentFragment();
  const sections = parseDetailSections(text);
  const hasStructure = sections.some((section) => section.title || section.pairs.length);

  if (!hasStructure) {
    const plain = document.createElement("pre");
    plain.className = "detail-plain";
    plain.textContent = String(text);
    fragment.appendChild(plain);
    return fragment;
  }

  const normalizedHeader = String(headerTitle).replace(/\s+/g, " ").trim();
  sections.forEach((section) => {
    const proseText = section.lines.join("\n").trim();
    if (
      section.title === "제목" &&
      !section.pairs.length &&
      proseText.replace(/\s+/g, " ") === normalizedHeader
    ) {
      return;
    }

    const block = document.createElement("section");
    block.className = "detail-section";

    if (section.title) {
      const heading = document.createElement("h4");
      heading.className = "detail-section-title";
      heading.textContent = section.title;
      block.appendChild(heading);
    }

    if (section.pairs.length) {
      const grid = document.createElement("dl");
      grid.className = "detail-grid";
      section.pairs.forEach(([key, value]) => {
        const item = document.createElement("div");
        item.className = "detail-item";
        const label = document.createElement("dt");
        label.className = "detail-key";
        label.textContent = key;
        item.append(label, buildDetailValue(key, value));
        grid.appendChild(item);
      });
      block.appendChild(grid);
    }

    if (proseText) {
      const prose = document.createElement("div");
      prose.className = "detail-prose";
      prose.textContent = proseText;
      block.appendChild(prose);
    }

    fragment.appendChild(block);
  });
  return fragment;
}

function closeChromaModal() {
  els.chromaModal.hidden = true;
  document.body.classList.remove("modal-open");
}

function formatMantisDetail(source) {
  const detail = source.detail || {};
  const lines = [
    `[Mantis 이슈 정보]`,
    `- 이슈 ID: #${source.issue_id || ""}`,
    `- 프로젝트(고객사): ${source.project || "미상"}`,
    `- 현재 상태: ${displayIssueStatus(source.status)}`,
    `- 담당자: ${source.handler || "담당자 미지정"}`,
    `- 등록일: ${source.created_at || "미상"}`,
    `- 화면번호: ${detail.screen_number || "미상"}`,
    `- 작업주체: ${detail.work_subject || "미상"}`,
  ];
  const effortEntries = Object.entries(detail.effort_fields || {}).filter(([, value]) => value);
  if (effortEntries.length) {
    lines.push(``, `[공수]`);
    effortEntries.forEach(([field, value]) => lines.push(`- ${field}: ${value}`));
  }
  const dateEntries = Object.entries(detail.date_fields || {}).filter(([, value]) => value);
  if (dateEntries.length) {
    lines.push(``, `[일정/전달일]`);
    dateEntries.forEach(([field, value]) => lines.push(`- ${field}: ${value}`));
  }
  lines.push(
    ``,
    `[제목]`,
    source.title || "제목 없음",
    ``,
    `[본문 미리보기]`,
    source.preview || detail.description || "내용 없음",
  );
  return lines.join("\n");
}

function displayIssueStatus(status) {
  const statusMap = {
    closed: "종료된 이슈",
    confirmed: "확인된 이슈",
    assigned: "할당된 이슈",
    acknowledged: "이슈 검토",
    resolved: "해결된 이슈",
    feedback: "정보 부족",
    developed: "개발완료",
    new: "신규",
  };
  const normalized = String(status || "").trim();
  return statusMap[normalized.toLowerCase()] || normalized || "미상";
}

async function openSourceDetail(source) {
  if (!source) return;

  if (source.type === "notion") {
    openChromaModal({
      issueId: source.page_id || "",
      title: source.title,
      content: formatNotionDetail(source),
      meta: "Notion DB 항목",
    });
    return;
  }

  if (source.type !== "chroma") {
    openChromaModal({
      issueId: source.issue_id,
      title: source.title,
      content: formatMantisDetail(source),
      meta: `Mantis 이슈 · #${source.issue_id}`,
    });
    return;
  }

  openChromaModal({
    issueId: source.issue_id,
    title: "불러오는 중입니다",
    content: "ChromaDB 원문을 불러오는 중입니다...",
    meta: `ChromaDB 문서 · #${source.issue_id}`,
  });

  try {
    const response = await apiFetch(`/api/chroma/documents/${encodeURIComponent(source.issue_id)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "문서를 불러오지 못했습니다.");
    const titleMatch = data.content.match(/\[제목\]\s*([\s\S]*?)(\n\n|\[고객사 본문 요청 내용\])/);
    openChromaModal({
      issueId: source.issue_id,
      title: titleMatch ? titleMatch[1].trim() : "이슈 상세",
      content: data.content,
      meta: `ChromaDB 문서 · #${source.issue_id}`,
    });
  } catch (error) {
    openChromaModal({
      issueId: source.issue_id,
      title: "문서 조회 오류",
      content: `오류: ${error.message}`,
      meta: `ChromaDB 문서 · #${source.issue_id}`,
    });
  }
}

function setBusy(busy) {
  state.busy = busy;
  els.input.disabled = busy;
  // 생성 중에는 전송 버튼이 중지 버튼으로 바뀐다(비활성화하지 않는다).
  els.sendBtn.disabled = false;
  els.sendBtn.classList.toggle("stop-mode", busy);
  els.sendBtn.setAttribute("aria-label", busy ? "답변 생성 중지" : "전송");
  const label = els.sendBtn.querySelector("span");
  if (label) label.textContent = busy ? "중지" : "전송";
}

function autoResizeInput() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 180)}px`;
}

function resetOptions() {
  els.temperature.value = defaultOptions.temperature;
  els.temperatureValue.textContent = defaultOptions.temperature;
  els.answerStyle.value = defaultOptions.answerStyle;
  els.topP.value = defaultOptions.topP;
  els.topK.value = defaultOptions.topK;
  els.minP.value = defaultOptions.minP;
  els.maxTokens.value = defaultOptions.maxTokens;
  els.presencePenalty.value = defaultOptions.presencePenalty;
  els.frequencyPenalty.value = defaultOptions.frequencyPenalty;
  els.repetitionPenalty.value = defaultOptions.repetitionPenalty;
  els.seed.value = defaultOptions.seed;
  els.stop.value = defaultOptions.stop;
  setShowSources(defaultOptions.showSources);
  setAbComparison(defaultOptions.abTest);
  setSourceMode(defaultOptions.sourceMode);
  setToolToggle(els.mantisToggle, defaultOptions.mantis);
  setToolToggle(els.vectorToggle, defaultOptions.vectorDb);
  els.searchLimit.value = defaultOptions.searchLimit;
  els.searchLimitValue.textContent = `${defaultOptions.searchLimit}건`;
  globalThis.AssistantCustomSelect?.syncAll();
}

function optionalNumber(element) {
  if (!element) return null;
  const value = element.value.trim();
  if (!value) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function stopValues() {
  return els.stop.value
    .split(/[,\n]/)
    .map((value) => value.trim())
    .filter(Boolean);
}

function generationPayload() {
  const payload = {
    temperature: Number(els.temperature.value),
    answerStyle: els.answerStyle.value,
    responseFormat: "text",
    showSources: showSourcesEnabled(),
    searchLimit: Number(els.searchLimit.value),
  };

  const optionalFields = {
    topP: optionalNumber(els.topP),
    topK: optionalNumber(els.topK),
    minP: optionalNumber(els.minP),
    maxTokens: optionalNumber(els.maxTokens),
    presencePenalty: optionalNumber(els.presencePenalty),
    frequencyPenalty: optionalNumber(els.frequencyPenalty),
    repetitionPenalty: optionalNumber(els.repetitionPenalty),
    seed: optionalNumber(els.seed),
  };

  Object.entries(optionalFields).forEach(([key, value]) => {
    if (value !== null) payload[key] = value;
  });

  const stops = stopValues();
  if (stops.length) payload.stop = stops;
  return payload;
}

async function loadModels() {
  const response = await apiFetch("/api/models");
  if (!response.ok) throw new Error("모델 목록을 불러오지 못했습니다.");
  const data = await response.json();
  state.models = data.models || [];
  renderModels();
}

function formatFreshnessDate(value) {
  if (!value) return "기준일 확인 필요";
  const [year, month, day] = String(value).split("-");
  if (!year || !month || !day) return String(value);
  return `${Number(month)}월 ${Number(day)}일 기준`;
}

async function loadDataStatus() {
  const [freshnessResult, notionResult] = await Promise.allSettled([
    apiFetch("/api/data/freshness").then((response) => {
      if (!response.ok) throw new Error("freshness");
      return response.json();
    }),
    apiFetch("/api/notion/status").then((response) => {
      if (!response.ok) throw new Error("notion");
      return response.json();
    }),
  ]);

  const freshness = freshnessResult.status === "fulfilled" ? freshnessResult.value?.mantis : null;
  const notion = notionResult.status === "fulfilled" ? notionResult.value : null;
  const model = selectedModel();
  if (model && model.status !== "active") return;
  const freshnessLabel = formatFreshnessDate(freshness?.asOf);
  const notionLabel = notion?.configured ? "Notion 연결됨" : "Notion 설정 필요";
  els.statusLine.textContent = ["Mantis 실시간", notionLabel, `Vector DB ${freshnessLabel}`].join(" · ");
  els.statusLine.className = freshness?.status === "stale" || !notion?.configured
    ? "status-warning data-status-line"
    : "status-active data-status-line";
  els.statusLine.title = freshness?.updatedAt
    ? `Vector DB 마지막 갱신: ${formatQuestionAskedAt(freshness.updatedAt)}`
    : "";
}

async function sendChat(content, onEvent = () => {}, signal = undefined) {
  const model = selectedModel();
  if (!model) throw new Error("모델을 선택해 주세요.");
  if (model.status !== "active") {
    throw new Error(`현재 서비스가 ${statusLabel(model.status)} 상태입니다.`);
  }

  // 대화가 길어져도 서버로 보내는 히스토리는 최근 N개로 제한한다(서버에서도 한 번 더 제한).
  // 현재 질문은 state.messages에 이미 push되어 있으므로(제출 핸들러 참고) history에서는
  // 제외하고, 아래에서 { role: "user", content }로 한 번만 명시적으로 붙인다.
  const chatMessages = buildChatMessages(state.messages, content, 20);

  // 직전 assistant 답변의 감사 ID. 서버가 "그중 3번째" 같은 서수 참조를
  // 그 답변에서 실제로 보여준 결과 목록으로 정확히 해석하는 데 사용한다.
  const lastAssistantWithAudit = [...state.messages]
    .reverse()
    .find((message) => message.role === "assistant" && message.auditId && !message.loading);

  const response = await apiFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      modelId: model.id,
      ...generationPayload(),
      abTest: abComparisonEnabled(),
      stream: true,
      contextAuditId: lastAssistantWithAudit?.auditId || null,
      messages: chatMessages,
      tools: sourceToolsPayload(),
    }),
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const retryAfter = response.headers.get("Retry-After");
    const suffix = response.status === 429 && retryAfter
      ? ` (${retryAfter}초 후 다시 시도)`
      : "";
    const error = new Error(`${data.detail || "모델 응답 중 오류가 발생했습니다."}${suffix}`);
    error.status = response.status;
    error.retryAfter = retryAfter;
    throw error;
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("스트리밍 응답을 읽을 수 없습니다.");
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const parsed = parseSseBuffer(buffer, done);
    buffer = parsed.rest;
    for (const event of parsed.events) {
      onEvent(event);
      if (event.type === "result") result = event.data;
      if (event.type === "error") {
        const error = new Error(event.message || "응답 중 오류가 발생했습니다.");
        error.status = event.status;
        throw error;
      }
    }
    if (done) break;
  }
  if (!result) throw new Error("완료된 응답을 받지 못했습니다.");
  return result;
}

async function requestAssistant(content, questionAskedAt = "", templateId = "") {
  state.messages.push({
    role: "assistant",
    content: "",
    questionAskedAt,
    loading: true,
    streaming: false,
    progress: "요청을 준비하고 있습니다",
    templateId,
  });
  const assistantIndex = state.messages.length - 1;
  renderMessages();
  setBusy(true);
  const abortController = new AbortController();
  state.chatAbortController = abortController;

  try {
    const answer = await sendChat(content, (streamEvent) => {
      const assistant = state.messages[assistantIndex];
      if (!assistant) return;
      if (streamEvent.type === "status") {
        assistant.progress = streamEvent.message;
        assistant.stage = streamEvent.stage;
      } else if (streamEvent.type === "delta") {
        assistant.loading = false;
        assistant.streaming = true;
        assistant.content = `${assistant.content || ""}${streamEvent.content || ""}`;
      }
      renderMessages();
    }, abortController.signal);
    state.messages[assistantIndex] = {
      role: "assistant",
      content: answer.content,
      sources: answer.sources || [],
      followUps: answer.followUps || [],
      verification: answer.verification || null,
      auditId: answer.auditId || "",
      abTest: answer.abTest || null,
      effortChart: answer.effortChart || null,
      effortRanking: answer.effortRanking || null,
      playbookCandidate: answer.playbookCandidate || null,
      playbookSaved: false,
      questionAskedAt,
      templateId,
    };
  } catch (error) {
    if (error.name === "AbortError") {
      // 사용자가 중지: 지금까지 스트리밍된 내용은 남기고 중지 사실만 표시한다.
      const partial = state.messages[assistantIndex]?.content || "";
      state.messages[assistantIndex] = {
        role: "assistant",
        content: partial
          ? `${partial}\n\n(여기서 답변 생성을 중지했습니다)`
          : "답변 생성을 중지했습니다.",
        stopped: true,
        questionAskedAt,
        templateId,
      };
    } else {
      state.messages[assistantIndex] = {
        role: "assistant",
        content: `오류: ${error.message}`,
        questionAskedAt,
        templateId,
      };
    }
  } finally {
    state.chatAbortController = null;
    setBusy(false);
    persistConversations();
    renderMessages();
    els.input.focus();
  }
}

export {
  selectedModel,
  statusLabel,
  updateModelMeta,
  renderModels,
  applyTheme,
  loadTheme,
  saveTheme,
  setShowSources,
  showSourcesEnabled,
  setAbComparison,
  abComparisonEnabled,
  setToolToggle,
  toolToggleEnabled,
  setSourceMode,
  sourceToolsPayload,
  TEAM_TEMPLATES,
  loadTeamTemplates,
  STARTER_ICONS,
  renderTeamTemplates,
  selectedTeamTemplate,
  templateFormValues,
  updateTemplatePreview,
  renderTemplateFields,
  openTemplateModal,
  closeTemplateModal,
  formatQuestionAskedAt,
  renderQuestionTimestamp,
  renderMessages,
  updateComposerShadow,
  MESSAGE_ACTION_ICONS,
  renderMessageActions,
  feedbackQuestionForMessage,
  openFeedbackModal,
  closeFeedbackModal,
  saveMessageFeedback,
  CHART_COLORS,
  truncateChartLabel,
  renderDonutChart,
  renderBarChartSVG,
  renderEffortChart,
  renderEffortRankingChart,
  appendAssistantExtras,
  conditionId,
  renderSearchConditionChips,
  escapeRegExp,
  questionWithoutCondition,
  rerunWithoutCondition,
  renderAbEvaluation,
  renderAbComparison,
  hasIssueSummaryList,
  shouldHideSourceList,
  statusToneClass,
  CALLOUT_ICONS,
  calloutToneFor,
  appendInlineMarkdown,
  renderCriteriaChips,
  appendAnswerParagraph,
  renderAssistantContent,
  parseIssueDetailContent,
  renderIssueDetailCard,
  buildIssueReferenceNotes,
  renderFollowUps,
  cleanMarkdown,
  parseIssueSummaryLine,
  renderIssueListSkeleton,
  issueValue,
  issuePriorityLabel,
  renderIssueSummaryList,
  renderSourceList,
  verificationStatusLabel,
  renderVerificationPanel,
  escapeHtml,
  openChromaModal,
  parseDetailSections,
  detailBadgeClass,
  buildDetailValue,
  renderDetailContent,
  closeChromaModal,
  formatMantisDetail,
  displayIssueStatus,
  openSourceDetail,
  setBusy,
  autoResizeInput,
  resetOptions,
  optionalNumber,
  stopValues,
  generationPayload,
  loadModels,
  formatFreshnessDate,
  loadDataStatus,
  sendChat,
  requestAssistant,
};

export { createConversationRecord } from "./runtime.js";
