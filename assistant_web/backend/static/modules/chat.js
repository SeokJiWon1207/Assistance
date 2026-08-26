import { state, els, defaultOptions, themeStorageKey, apiFetch, buildTeamTemplateQuestion, automaticSourceTools } from "./runtime.js";
import { showToast, persistConversations } from "./workspace.js";
import { configureChatController, sendChat, requestAssistant } from "./chat_controller.js";
import {
  configureChatFeedback,
  MESSAGE_ACTION_ICONS,
  renderMessageActions,
  feedbackQuestionForMessage,
  openFeedbackModal,
  closeFeedbackModal,
  saveMessageFeedback,
} from "./chat_feedback.js";
import {
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
} from "./chat_rendering.js";
import {
  CHART_COLORS,
  renderBarChartSVG,
  renderDonutChart,
  renderEffortChart,
  renderEffortRankingChart,
  truncateChartLabel,
} from "./charts.js";
import { renderEffortTrendChart } from "./trend_chart.js";

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

function sourceToolsPayload() {
  return automaticSourceTools();
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
function appendAssistantExtras(container, message, messageIndex) {
  if (message.effortChart) container.appendChild(renderEffortChart(message.effortChart));
  if (message.effortRanking) container.appendChild(renderEffortRankingChart(message.effortRanking));
  if (message.effortTrend) container.appendChild(renderEffortTrendChart(message.effortTrend));
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
  // 2단 비교 레이아웃(responsive.css의 min-width:1100px)에서 CSS로 다시 펼 수 없다.
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

configureChatController({
  selectedModel,
  statusLabel,
  generationPayload,
  abComparisonEnabled,
  sourceToolsPayload,
  renderMessages,
  setBusy,
});
configureChatFeedback({ renderMessages });

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
