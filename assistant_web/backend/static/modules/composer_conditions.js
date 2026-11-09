// 입력창 '조건' 메뉴. 고른 조건은 숨은 값이 아니라 질문 문장 앞에 그대로 들어가므로
// 보내기 전에 눈으로 확인하고 고칠 수 있고, 질문 해석 방식은 바뀌지 않는다.
const CONDITION_GROUPS = [
  { key: "period", label: "기간", options: [
    { id: "week", phrase: "최근 1주" }, { id: "two-weeks", phrase: "최근 2주" }, { id: "month", phrase: "최근 한 달" },
  ] },
  { key: "owner", label: "담당", options: [{ id: "mine", phrase: "내 담당" }] },
  { key: "status", label: "상태", options: [{ id: "open", phrase: "미해결" }] },
];

const OPTIONS = CONDITION_GROUPS.flatMap((group) => group.options.map((option) => ({ ...option, group: group.key })));

// 템플릿이 넣는 기간도 같은 조회 조건이다. '오늘의 시세', '지난달과 비교',
// '30일 이상'처럼 제목·비교 기준·경과일인 표현은 단독 기간과 구분한다.
const TEMPLATE_PERIOD_PATTERN = /(^|\s)(?:오늘|어제|(?:이번|지난|저번)\s*(?:주|달)|올해|작년|최근\s*\d{1,3}\s*(?:일|주|개월|달)|20\d{2}\s*년\s*\d{1,2}\s*월)(?=\s|$)(?!\s*\d{1,2}\s*일)/g;

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function phrasePattern(phrase) {
  return new RegExp(`(^|\\s)${escapeRegExp(phrase)}(?=\\s|$)`);
}

function activeConditionIds(text) {
  const value = String(text || "");
  return OPTIONS.filter((option) => phrasePattern(option.phrase).test(value)).map((option) => option.id);
}

function removePhrase(text, phrase) {
  return text.replace(phrasePattern(phrase), "$1").replace(/\s{2,}/g, " ").trim();
}

// 같은 조건을 다시 누르면 빼고, 같은 묶음의 다른 조건(예: 기간)은 바꾼다. 새 조건은 문장 맨 앞에 붙인다.
function toggleConditionPhrase(text, optionId) {
  const option = OPTIONS.find((item) => item.id === optionId);
  let value = String(text || "");
  if (!option) return value;
  if (phrasePattern(option.phrase).test(value)) return removePhrase(value, option.phrase);
  if (option.group === "period") value = value.replace(TEMPLATE_PERIOD_PATTERN, "$1");
  for (const sibling of OPTIONS.filter((item) => item.group === option.group)) value = removePhrase(value, sibling.phrase);
  value = value.trim();
  return value ? `${option.phrase} ${value}` : `${option.phrase} `;
}

function menuHtml(active) {
  return CONDITION_GROUPS.map((group) => `
    <div class="composer-conditions-group" role="group" aria-label="${group.label}">
      <span>${group.label}</span>
      ${group.options.map((option) => `<button type="button" class="composer-condition-chip" data-condition-option="${option.id}" aria-pressed="${active.includes(option.id)}">${option.phrase}</button>`).join("")}
    </div>`).join("");
}

function initComposerConditions() {
  const input = document.querySelector("#promptInput");
  const toggle = document.querySelector("#composerConditionsBtn");
  const menu = document.querySelector("#composerConditionsMenu");
  if (!input || !toggle || !menu) return;
  const updateBadge = () => {
    const count = activeConditionIds(input.value).length;
    const badge = toggle.querySelector("[data-condition-count]");
    if (badge) {
      badge.textContent = String(count);
      badge.hidden = !count;
    }
  };
  const render = () => {
    menu.innerHTML = menuHtml(activeConditionIds(input.value));
    updateBadge();
  };
  const setOpen = (open, { focus = false } = {}) => {
    menu.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      render();
      if (focus) menu.querySelector("[data-condition-option]")?.focus();
    } else if (focus) toggle.focus();
  };
  toggle.addEventListener("click", () => setOpen(menu.hidden, { focus: menu.hidden }));
  menu.addEventListener("click", (event) => {
    const option = event.target.closest("[data-condition-option]");
    if (!option) return;
    input.value = toggleConditionPhrase(input.value, option.dataset.conditionOption);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    render();
    menu.querySelector(`[data-condition-option="${option.dataset.conditionOption}"]`)?.focus();
  });
  menu.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    setOpen(false, { focus: true });
  });
  input.addEventListener("input", () => (menu.hidden ? updateBadge() : render()));
  // 전송하면 입력창을 비우므로 다음 틱에 개수 배지를 다시 맞추고 메뉴를 닫는다.
  input.form?.addEventListener("submit", () => globalThis.setTimeout(() => {
    setOpen(false);
    updateBadge();
  }));
  document.addEventListener("pointerdown", (event) => {
    if (!menu.hidden && !menu.contains(event.target) && !toggle.contains(event.target)) setOpen(false);
  });
}

export { CONDITION_GROUPS, activeConditionIds, toggleConditionPhrase, initComposerConditions };
