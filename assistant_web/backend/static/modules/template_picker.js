import { formatUiDateTime } from "./ui_format.js?v=20260911-6";
import { state, els, apiFetch, describeTeamTemplateQuestion, scopedStorageKey } from "./runtime.js?v=20260911-6";
import { escapeHtml } from "./chat_rendering.js?v=20260911-6";

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

// team_templates.json의 category를 팝오버의 세 묶음으로 나눈다.
// 여기에 없는 새 분류는 '전체'에서만 보인다.
const TEMPLATE_GROUPS = [
  { key: "all", label: "전체" },
  { key: "analysis", label: "조회·분석", categories: ["운영 현황", "고객사 분석", "장기 적체", "유사 사례", "공수·SLA"] },
  { key: "report", label: "보고서", categories: ["고객사 보고서", "보고서", "회고·예방"] },
  { key: "ops", label: "장애·협업", categories: ["장애 대응", "협업", "고객 커뮤니케이션"] },
];
const RECENT_TEMPLATES_KEY = "team-ai-assistant-recent-templates-v1";
const RECENT_TEMPLATE_LIMIT = 3;
// 선택지가 이 개수 이하인 select는 드롭다운 대신 한 번에 누르는 칩으로 보여준다.
const CHIP_OPTION_LIMIT = 4;

const picker = {
  open: false,
  view: "list",
  group: "all",
  activeIndex: 0,
  fromSlash: false,
};

function templateIconSvg(template) {
  return `<svg viewBox="0 0 24 24">${STARTER_ICONS[template?.icon] || STARTER_ICONS.report}</svg>`;
}

function renderTeamTemplates() {
  const starterIds = ["daily-operations", "aging-issues", "similar-issues", "incident-triage", "weekly-report", "customer-profile"];
  const preferred = starterIds.map((id) => TEAM_TEMPLATES.find((item) => item.id === id)).filter(Boolean);
  const starters = [...preferred, ...TEAM_TEMPLATES.filter((item) => !starterIds.includes(item.id))].slice(0, 6);
  return starters.map((item) => `
    <button type="button" class="starter-card" data-template-id="${escapeHtml(item.id)}">
      <span class="starter-card-icon" aria-hidden="true">
        ${templateIconSvg(item)}
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

function recentTemplatesKey() {
  return scopedStorageKey(RECENT_TEMPLATES_KEY, state.currentUser?.uid);
}

function recentTemplateIds() {
  try {
    const ids = JSON.parse(localStorage.getItem(recentTemplatesKey()) || "[]");
    return Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : [];
  } catch {
    return [];
  }
}

function rememberTemplate(templateId) {
  if (!templateId) return;
  const ids = [templateId, ...recentTemplateIds().filter((id) => id !== templateId)].slice(0, RECENT_TEMPLATE_LIMIT);
  try {
    localStorage.setItem(recentTemplatesKey(), JSON.stringify(ids));
  } catch {
    // 저장소를 쓸 수 없어도 템플릿 실행은 계속한다.
  }
}

function normalizedSearchText(value) {
  return String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, "");
}

function templateGroupKey(template) {
  return TEMPLATE_GROUPS.find((group) => group.categories?.includes(template.category))?.key || "";
}

function matchesTemplateQuery(template, needle) {
  if (!needle) return true;
  return normalizedSearchText(`${template.title} ${template.description} ${template.category}`).includes(needle);
}

function templatePickerSections(templates, { query = "", group = "all", recentIds = [] } = {}) {
  const needle = normalizedSearchText(query);
  const inGroup = (template, key) => key === "all" || templateGroupKey(template) === key;
  const recent = !needle && group === "all"
    ? recentIds.map((id) => templates.find((template) => template.id === id)).filter(Boolean)
    : [];
  const rest = templates.filter((template) => (
    inGroup(template, group) && matchesTemplateQuery(template, needle) && !recent.includes(template)
  ));
  const groupCounts = Object.fromEntries(TEMPLATE_GROUPS.map((item) => [
    item.key,
    templates.filter((template) => inGroup(template, item.key) && matchesTemplateQuery(template, needle)).length,
  ]));
  return { recent, rest, groupCounts };
}

function slashQuery() {
  const value = els.input.value;
  return value.startsWith("/") ? value.slice(1).trim() : "";
}

function templateOptionHtml(template, index) {
  return `
    <div id="template-option-${escapeHtml(template.id)}" class="template-picker-option" role="option" aria-selected="false" data-template-option="${escapeHtml(template.id)}" data-option-index="${index}">
      <span class="template-picker-option-icon" aria-hidden="true">${templateIconSvg(template)}</span>
      <span class="template-picker-option-copy"><strong>${escapeHtml(template.title)}</strong><span>${escapeHtml(template.description)}</span></span>
      <span class="template-picker-option-meta">${escapeHtml(template.category)}</span>
      <kbd class="template-picker-option-enter" aria-hidden="true">Enter</kbd>
    </div>`;
}

function optionSectionHtml(label, templates, startIndex) {
  if (!templates.length) return "";
  const labelId = `template-section-${startIndex}`;
  return `
    <div class="template-picker-section" role="group" aria-labelledby="${labelId}">
      <div id="${labelId}" class="template-picker-section-label">${escapeHtml(label)}</div>
      ${templates.map((template, index) => templateOptionHtml(template, startIndex + index)).join("")}
    </div>`;
}

function scrollOptionIntoView(row) {
  const list = els.templatePickerList;
  if (row.offsetTop < list.scrollTop) {
    list.scrollTop = row.offsetTop;
  } else if (row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) {
    list.scrollTop = row.offsetTop + row.offsetHeight - list.clientHeight;
  }
}

function setActiveOption(index, { scroll = true } = {}) {
  const rows = [...els.templatePickerList.querySelectorAll("[data-template-option]")];
  picker.activeIndex = rows.length ? (index + rows.length) % rows.length : 0;
  rows.forEach((row, rowIndex) => {
    const active = rowIndex === picker.activeIndex;
    row.classList.toggle("is-active", active);
    row.setAttribute("aria-selected", String(active));
  });
  const activeRow = rows[picker.activeIndex];
  if (!activeRow) {
    els.input.removeAttribute("aria-activedescendant");
    return;
  }
  els.input.setAttribute("aria-activedescendant", activeRow.id);
  if (scroll) scrollOptionIntoView(activeRow);
}

function renderTemplatePickerList() {
  const query = slashQuery();
  const { recent, rest, groupCounts } = templatePickerSections(TEAM_TEMPLATES, {
    query,
    group: picker.group,
    recentIds: recentTemplateIds(),
  });
  const total = recent.length + rest.length;
  const groupLabel = TEMPLATE_GROUPS.find((group) => group.key === picker.group)?.label || "전체";

  els.templatePickerGroups.innerHTML = TEMPLATE_GROUPS.map((group) => `
    <button type="button" class="template-picker-group" data-template-group="${group.key}" aria-pressed="${picker.group === group.key}">
      ${escapeHtml(group.label)} <span>${groupCounts[group.key]}</span>
    </button>`).join("");

  if (!TEAM_TEMPLATES.length) {
    els.templatePickerList.innerHTML = `<p class="template-picker-empty">${escapeHtml(state.teamTemplatesError || "팀 템플릿을 불러오는 중입니다.")}</p>`;
  } else if (!total) {
    els.templatePickerList.innerHTML = `
      <div class="template-picker-empty">
        <strong>${query ? `‘${escapeHtml(query)}’에 맞는 템플릿이 없습니다` : "이 분류에 템플릿이 없습니다"}</strong>
        <span>다른 단어로 찾거나 Esc로 닫아 주세요.</span>
      </div>`;
  } else {
    const restLabel = query ? `‘${query}’ 검색 결과` : groupLabel;
    els.templatePickerList.innerHTML = optionSectionHtml("최근 사용", recent, 0) + optionSectionHtml(restLabel, rest, recent.length);
  }
  els.templatePickerHint.textContent = query ? `/${query} 로 찾는 중` : "이어서 입력하면 바로 걸러집니다";
  els.templatePickerCount.textContent = `${total}개`;
  setActiveOption(Math.min(picker.activeIndex, Math.max(total - 1, 0)));
}

function positionTemplatePicker() {
  els.templatePicker.style.setProperty("--template-picker-bottom", `${Math.max(els.form.offsetHeight - 8, 0)}px`);
}

function showPickerView(view) {
  picker.view = view;
  els.templatePickerListView.hidden = view !== "list";
  els.templateForm.hidden = view !== "form";
}

function setPickerOpen(open) {
  picker.open = open;
  els.templatePicker.hidden = !open;
  els.chatPanel?.classList.toggle("template-picker-open", open);
  els.templateToggleBtn?.setAttribute("aria-expanded", String(open));
  els.templateToggleBtn?.classList.toggle("is-active", open);
  if (open) {
    positionTemplatePicker();
    els.input.setAttribute("aria-controls", "templatePickerList");
  } else {
    els.input.removeAttribute("aria-activedescendant");
    els.input.removeAttribute("aria-controls");
  }
}

function isTemplatePickerOpen() {
  return picker.open;
}

function openTemplatePicker() {
  if (state.busy) return;
  // 버튼으로 열 때 입력창이 비어 있으면 '/'를 넣어 이어지는 입력이 바로 검색어가 되게 한다.
  if (!els.input.value.trim()) els.input.value = "/";
  picker.fromSlash = els.input.value.startsWith("/");
  if (!picker.open || picker.view !== "list") {
    picker.group = "all";
    picker.activeIndex = 0;
  }
  showPickerView("list");
  setPickerOpen(true);
  renderTemplatePickerList();
}

function closeTemplatePicker({ clearSlash = true, restoreFocus = true } = {}) {
  if (!picker.open) return;
  setPickerOpen(false);
  showPickerView("list");
  state.activeTemplateId = null;
  if (clearSlash && els.input.value.startsWith("/")) els.input.value = "";
  if (restoreFocus) els.input.focus();
}

function syncTemplatePickerWithPrompt() {
  if (els.input.value.startsWith("/")) {
    picker.activeIndex = 0;
    if (picker.open && picker.view === "list") renderTemplatePickerList();
    else openTemplatePicker();
    return;
  }
  if (picker.open && picker.view === "list" && picker.fromSlash) {
    closeTemplatePicker({ clearSlash: false, restoreFocus: false });
  }
}

function handleTemplatePickerKeydown(event) {
  if (!picker.open || picker.view !== "list") return false;
  if (event.isComposing || event.keyCode === 229) return false;
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    setActiveOption(picker.activeIndex + (event.key === "ArrowDown" ? 1 : -1));
    return true;
  }
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    const activeRow = els.templatePickerList.querySelectorAll("[data-template-option]")[picker.activeIndex];
    if (activeRow) showTemplateForm(activeRow.dataset.templateOption);
    return true;
  }
  return false;
}

function templateChipGroupHtml(field, inputId, currentValue, requiredLabel) {
  const labelId = `${inputId}-label`;
  return `
    <div class="template-field template-chip-field" role="radiogroup" aria-labelledby="${labelId}">
      <span id="${labelId}">${escapeHtml(field.label)} ${requiredLabel}</span>
      <div class="template-chips">
        ${field.options.map((option, index) => `
          <label class="template-chip">
            <input type="radio" name="${escapeHtml(field.name)}" value="${escapeHtml(option)}" ${option === currentValue ? "checked" : ""} ${field.required && index === 0 ? "required" : ""} />
            <span>${escapeHtml(option)}</span>
          </label>`).join("")}
      </div>
    </div>`;
}

function renderTemplateFields(template) {
  els.templateFields.innerHTML = template.fields.map((field) => {
    const inputId = `templateField-${field.name}`;
    const required = field.required ? "required" : "";
    const requiredLabel = field.required ? '<small class="required-mark">필수</small>' : "";
    const today = new Date();
    const dynamicValue = field.dynamicValue === "current-month"
      ? `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}`
      : "";
    if (field.type === "select" && field.options.length <= CHIP_OPTION_LIMIT) {
      return templateChipGroupHtml(field, inputId, field.value || "", requiredLabel);
    }
    let control;
    if (field.type === "select" || field.type === "customer") {
      const currentValue = field.value || "";
      const emptyLabel = field.placeholder || (field.required ? "선택해 주세요" : "전체");
      const emptyOption = field.type === "customer"
        ? `<option value="" ${field.required ? "disabled" : ""} ${currentValue ? "" : "selected"}>${escapeHtml(emptyLabel)}</option>`
        : "";
      control = `<select id="${inputId}" name="${escapeHtml(field.name)}" data-options-hint="${escapeHtml(field.optionsHint || "")}" ${field.type === "customer" ? 'data-searchable="true"' : ""} ${required}>
          ${emptyOption}
          ${field.options.map((option) => `<option value="${escapeHtml(option)}" data-detail="${escapeHtml(field.optionDetails?.[option] || "")}" data-search-terms="${escapeHtml(field.optionSearchTerms?.[option] || option)}" ${option === currentValue ? "selected" : ""}>${escapeHtml(option)}</option>`).join("")}
        </select>`;
    } else {
      control = `<input id="${inputId}" name="${escapeHtml(field.name)}" type="${field.type}" value="${escapeHtml(field.value || dynamicValue)}" placeholder="${escapeHtml(field.placeholder || "")}" ${field.min ? `min="${field.min}"` : ""} ${field.max ? `max="${field.max}"` : ""} ${required} />`;
    }
    return `
      <label class="template-field" for="${inputId}">
        <span>${escapeHtml(field.label)} ${requiredLabel}</span>
        ${control}
        ${field.optionsHint ? `<small class="template-options-hint">${escapeHtml(field.optionsHint)}${field.optionsAsOf ? `<span>집계 저장본: ${escapeHtml(formatUiDateTime(field.optionsAsOf))}</span>` : ""}</small>` : ""}
      </label>
    `;
  }).join("");
  globalThis.AssistantCustomSelect?.enhanceAll(els.templateFields);
}

function updateTemplatePreview() {
  const template = selectedTeamTemplate();
  if (!template) return;
  const { question, missing } = describeTeamTemplateQuestion(template, templateFormValues());
  els.templateQuestionPreview.textContent = question;
  els.templateMissingHint.textContent = missing.length
    ? `필수 항목: ${missing.join(", ")}`
    : "조건을 바꾸면 완성 질문이 바로 바뀝니다.";
  els.templateMissingHint.classList.toggle("is-missing", missing.length > 0);
  els.templateSubmitBtn.disabled = missing.length > 0;
  els.templateInsertBtn.disabled = missing.length > 0;
}

function completedTemplateQuestion() {
  const template = selectedTeamTemplate();
  if (!template) return null;
  const { question, missing } = describeTeamTemplateQuestion(template, templateFormValues());
  return missing.length || !question ? null : { template, question };
}

function focusFirstTemplateField() {
  // custom select가 숨겨 둔 원래 select 대신 사용자가 조작하는 트리거나 선택된 칩에 포커스를 둔다.
  const field = els.templateFields.querySelector(".template-field");
  const target = field?.querySelector("input[type='radio']:checked")
    || field?.querySelector(".custom-select-trigger, input, select:not(.custom-select-native)");
  target?.focus();
}

function showTemplateForm(templateId) {
  const template = TEAM_TEMPLATES.find((item) => item.id === templateId);
  if (!template || state.busy) return;
  state.activeTemplateId = template.id;
  if (els.input.value.startsWith("/")) els.input.value = "";
  els.templateFormIcon.innerHTML = templateIconSvg(template);
  els.templateFormTitle.textContent = template.title;
  els.templateFormDescription.textContent = template.description;
  renderTemplateFields(template);
  updateTemplatePreview();
  showPickerView("form");
  setPickerOpen(true);
  els.input.removeAttribute("aria-activedescendant");
  focusFirstTemplateField();
}

function backToTemplateList() {
  state.activeTemplateId = null;
  if (!els.input.value.trim()) els.input.value = "/";
  picker.fromSlash = els.input.value.startsWith("/");
  showPickerView("list");
  renderTemplatePickerList();
  els.input.focus();
}

function initTemplatePicker() {
  if (!els.templatePicker) return;
  els.templateToggleBtn?.addEventListener("click", () => {
    if (picker.open) {
      closeTemplatePicker();
      return;
    }
    openTemplatePicker();
    els.input.focus();
  });
  els.templatePickerGroups.addEventListener("click", (event) => {
    const button = event.target.closest("[data-template-group]");
    if (!button) return;
    picker.group = button.dataset.templateGroup;
    picker.activeIndex = 0;
    renderTemplatePickerList();
    els.input.focus();
  });
  els.templatePickerList.addEventListener("click", (event) => {
    const option = event.target.closest("[data-template-option]");
    if (option) showTemplateForm(option.dataset.templateOption);
  });
  els.templatePickerList.addEventListener("mousemove", (event) => {
    const option = event.target.closest("[data-template-option]");
    const index = Number(option?.dataset.optionIndex);
    if (option && index !== picker.activeIndex) setActiveOption(index, { scroll: false });
  });
  els.templateBackBtn.addEventListener("click", backToTemplateList);
  els.templateCloseBtn.addEventListener("click", () => closeTemplatePicker());
  els.templateFields.addEventListener("input", updateTemplatePreview);
  els.templateFields.addEventListener("change", updateTemplatePreview);
  // custom select 트리거는 메뉴가 닫혀 있어도 Escape 전파를 막으므로 캡처 단계에서 먼저 닫는다.
  els.templatePicker.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !picker.open) return;
    if (els.templatePicker.querySelector(".custom-select.is-open")) return;
    event.preventDefault();
    event.stopPropagation();
    closeTemplatePicker();
  }, true);
  // custom select 메뉴는 body에 붙지만 pointerdown 전파를 막으므로 여기서 바깥 클릭으로 잡히지 않는다.
  document.addEventListener("pointerdown", (event) => {
    if (!picker.open) return;
    const target = event.target;
    if (els.templatePicker.contains(target) || els.templateToggleBtn?.contains(target) || els.input.contains(target)) return;
    closeTemplatePicker({ clearSlash: false, restoreFocus: false });
  });
  if ("ResizeObserver" in globalThis) {
    new ResizeObserver(() => {
      if (picker.open) positionTemplatePicker();
    }).observe(els.form);
  }
}

export {
  TEAM_TEMPLATES,
  STARTER_ICONS,
  TEMPLATE_GROUPS,
  renderTeamTemplates,
  loadTeamTemplates,
  selectedTeamTemplate,
  templateFormValues,
  rememberTemplate,
  templatePickerSections,
  renderTemplateFields,
  updateTemplatePreview,
  completedTemplateQuestion,
  isTemplatePickerOpen,
  openTemplatePicker,
  closeTemplatePicker,
  syncTemplatePickerWithPrompt,
  handleTemplatePickerKeydown,
  showTemplateForm,
  initTemplatePicker,
};
