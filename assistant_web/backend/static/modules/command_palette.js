// ⌘K(Windows는 Ctrl+K) 명령 팔레트. 화면 이동·이전 대화·템플릿·이슈 원문을 한 입력창에서 찾는다.
import { state, els } from "./runtime.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";
import { escapeHtml } from "./chat_rendering.js?v=20260911-6";
import { TEAM_TEMPLATES, showTemplateForm, isTemplatePickerOpen, closeTemplatePicker } from "./template_picker.js?v=20260911-6";
import { openConversation, workspaceStatusLabel } from "./workspace.js?v=20260911-6";
import { showChatView, showPersonalView, showDashboardView, showAdminView } from "./dashboard.js?v=20260911-6";
import { openSourceDetail } from "./chat_source_detail.js?v=20260911-6";
import { openDialogFocus, closeDialogFocus } from "./dialog_focus.js?v=20260911-6";
import { commandPaletteSections, flattenCommandItems, nextEnabledIndex } from "./command_palette_model.js?v=20261105-1";

const palette = {
  open: false,
  items: [],
  activeIndex: -1,
};

const VIEW_COMMANDS = [
  { view: "chat", label: "대화 화면으로 이동", icon: "chat", keywords: "채팅 질문", button: () => els.viewChatBtn, show: showChatView },
  { view: "personal", label: "내 업무로 이동", icon: "clipboard", keywords: "워크스페이스 주간보고 플레이북 알림 즐겨찾기", button: () => els.viewPersonalBtn, show: showPersonalView },
  { view: "dashboard", label: "팀 대시보드로 이동", icon: "chart", keywords: "운영 현황 공수 처리시간 화면번호", button: () => els.viewDashboardBtn, show: showDashboardView },
  { view: "admin", label: "관리자 센터로 이동", icon: "shield", keywords: "품질 검증 피드백 감사 로그 사용자", button: () => els.viewAdminBtn, show: showAdminView },
];

function paletteElements() {
  return {
    root: document.querySelector("#commandPalette"),
    dialog: document.querySelector("#commandPaletteDialog"),
    input: document.querySelector("#commandPaletteInput"),
    list: document.querySelector("#commandPaletteList"),
  };
}

function isMacPlatform() {
  const platform = globalThis.navigator?.userAgentData?.platform || globalThis.navigator?.platform || "";
  return /mac|iphone|ipad/i.test(platform);
}

function shortcutLabel() {
  return isMacPlatform() ? "⌘K" : "Ctrl K";
}

function currentView() {
  return els.appShell?.dataset.view || "chat";
}

function availableViews() {
  const view = currentView();
  return VIEW_COMMANDS.map((command) => ({
    view: command.view,
    label: command.label,
    icon: command.icon,
    keywords: command.keywords,
    available: Boolean(command.button()) && !command.button().hidden,
    current: command.view === view,
  }));
}

function knownIssues() {
  return (state.myWorkspace?.mantis?.issues || []).map((issue) => ({
    id: issue.id,
    title: issue.title,
    status: workspaceStatusLabel(issue.status),
  }));
}

function currentSections(query) {
  return commandPaletteSections({
    query,
    views: availableViews(),
    conversations: state.conversations || [],
    activeConversationId: state.activeConversationId,
    templates: TEAM_TEMPLATES,
    issues: knownIssues(),
    busy: Boolean(state.busy),
  });
}

function optionId(index) {
  return `commandPaletteOption-${index}`;
}

function optionHtml(item, index) {
  const meta = item.meta ? `<span class="command-palette-meta">${escapeHtml(item.meta)}</span>` : "";
  return `
    <div id="${optionId(index)}" class="command-palette-option${item.kind === "issue" ? " is-issue" : ""}" role="option"
      aria-selected="false"${item.disabled ? ' aria-disabled="true"' : ""} data-command-index="${index}">
      <span class="command-palette-option-icon">${icon(item.icon || "chevron-right", 18)}</span>
      <span class="command-palette-label">${escapeHtml(item.label)}</span>
      ${meta}
    </div>`;
}

function renderPalette() {
  const { input, list } = paletteElements();
  if (!input || !list) return;
  const sections = currentSections(input.value);
  palette.items = flattenCommandItems(sections);
  let index = 0;
  list.innerHTML = sections.length
    ? sections.map((group) => {
      const headingId = `commandPaletteGroup-${group.key}`;
      const options = group.items.map((item) => optionHtml(item, index++)).join("");
      return `<div class="command-palette-group" role="group" aria-labelledby="${headingId}"><p id="${headingId}" class="command-palette-group-label">${escapeHtml(group.label)}</p>${options}</div>`;
    }).join("")
    : '<p class="command-palette-empty">찾는 항목이 없습니다.</p>';
  setActiveIndex(nextEnabledIndex(palette.items, -1, 1));
}

function setActiveIndex(index, { scroll = true } = {}) {
  const { input, list } = paletteElements();
  palette.activeIndex = index;
  for (const option of list.querySelectorAll("[data-command-index]")) {
    const active = Number(option.dataset.commandIndex) === index;
    option.classList.toggle("is-active", active);
    option.setAttribute("aria-selected", String(active));
    if (active && scroll) option.scrollIntoView?.({ block: "nearest" });
  }
  if (index >= 0) input.setAttribute("aria-activedescendant", optionId(index));
  else input.removeAttribute("aria-activedescendant");
}

function openCommandPalette() {
  const { root, dialog, input } = paletteElements();
  if (!root || palette.open) return;
  if (isTemplatePickerOpen()) closeTemplatePicker({ clearSlash: false, restoreFocus: false });
  palette.open = true;
  root.hidden = false;
  input.value = "";
  renderPalette();
  openDialogFocus(dialog, { initialFocus: input, onClose: closeCommandPalette, fallback: () => els.input });
}

function closeCommandPalette({ restoreFocus = true } = {}) {
  const { root, dialog } = paletteElements();
  if (!root || !palette.open) return;
  palette.open = false;
  root.hidden = true;
  closeDialogFocus(dialog, { restoreFocus });
}

function isCommandPaletteOpen() {
  return palette.open;
}

function askQuestion(question) {
  showChatView();
  els.input.value = question;
  els.input.dispatchEvent(new Event("input", { bubbles: true }));
  els.input.focus();
}

function runCommand(item) {
  if (!item || item.disabled) return;
  // 실행 결과가 다른 창(템플릿·원문)을 열 수 있으므로 팔레트의 포커스 복원부터 끝낸다.
  closeCommandPalette({ restoreFocus: false });
  if (item.kind === "view") {
    VIEW_COMMANDS.find((command) => command.view === item.view)?.show();
    VIEW_COMMANDS.find((command) => command.view === item.view)?.button()?.focus();
  } else if (item.kind === "new-chat") {
    els.newChatBtn.click();
  } else if (item.kind === "theme") {
    els.darkModeToggle.click();
    els.input.focus();
  } else if (item.kind === "conversation") {
    showChatView();
    openConversation(item.conversationId);
    els.input.focus();
  } else if (item.kind === "template") {
    showChatView();
    showTemplateForm(item.templateId);
  } else if (item.kind === "issue") {
    openSourceDetail({ type: "chroma", issue_id: item.issueId });
  } else if (item.kind === "ask") {
    askQuestion(item.question);
  }
}

function handlePaletteKeydown(event) {
  if (event.isComposing) return;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    setActiveIndex(nextEnabledIndex(palette.items, palette.activeIndex, event.key === "ArrowDown" ? 1 : -1));
  } else if (event.key === "Enter") {
    event.preventDefault();
    runCommand(palette.items[palette.activeIndex]);
  }
}

function isPaletteShortcut(event) {
  return (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && String(event.key).toLowerCase() === "k";
}

function initCommandPalette() {
  const { root, input, list } = paletteElements();
  if (!root || !input || !list) return;
  const label = shortcutLabel();
  for (const element of document.querySelectorAll("[data-command-palette-shortcut]")) element.textContent = label;
  for (const element of document.querySelectorAll("[data-command-palette-open]")) {
    element.setAttribute("aria-keyshortcuts", isMacPlatform() ? "Meta+K" : "Control+K");
    element.title = `빠른 이동 (${label})`;
    element.addEventListener("click", () => openCommandPalette());
  }

  input.addEventListener("input", renderPalette);
  input.addEventListener("keydown", handlePaletteKeydown);
  list.addEventListener("click", (event) => {
    const option = event.target.closest("[data-command-index]");
    if (option) runCommand(palette.items[Number(option.dataset.commandIndex)]);
  });
  list.addEventListener("mousemove", (event) => {
    const option = event.target.closest("[data-command-index]");
    const index = Number(option?.dataset.commandIndex);
    if (option && index !== palette.activeIndex && !palette.items[index]?.disabled) setActiveIndex(index, { scroll: false });
  });
  root.addEventListener("pointerdown", (event) => {
    if (event.target === root) closeCommandPalette();
  });
  document.addEventListener("keydown", (event) => {
    if (!isPaletteShortcut(event) || event.isComposing) return;
    // 로그인 화면이나 다른 확인 창 위에서는 열지 않는다.
    if (document.body.classList.contains("auth-pending") || document.querySelector("#authGate")?.hidden === false) return;
    if (!palette.open && document.body.classList.contains("modal-open")) return;
    event.preventDefault();
    if (palette.open) closeCommandPalette();
    else openCommandPalette();
  });
}

export {
  initCommandPalette,
  openCommandPalette,
  closeCommandPalette,
  isCommandPaletteOpen,
  isPaletteShortcut,
  shortcutLabel,
};
