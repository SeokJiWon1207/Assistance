// 표시 기능만 담당한다. 질문·집계·권한과 서버 상태는 변경하지 않는다.
export function initUiDetails() {
  const tooltip = document.createElement("div");
  tooltip.className = "ui-tooltip";
  tooltip.id = "uiTooltip";
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = true;
  document.body.appendChild(tooltip);
  let tooltipTarget = null;
  const hideTooltip = () => {
    tooltip.hidden = true;
    if (tooltipTarget?.getAttribute("aria-describedby") === tooltip.id) tooltipTarget.removeAttribute("aria-describedby");
    tooltipTarget = null;
  };
  const showTooltip = (event) => {
    const target = event.target.closest("button[aria-label], button[title]");
    if (!target || target.disabled) return hideTooltip();
    const label = target.getAttribute("title") || target.getAttribute("aria-label");
    if (!label || (!target.querySelector("svg") && target.textContent.trim() === label)) return;
    hideTooltip();
    tooltipTarget = target;
    tooltip.textContent = label;
    tooltip.hidden = false;
    if (!target.hasAttribute("aria-describedby")) target.setAttribute("aria-describedby", tooltip.id);
    const rect = target.getBoundingClientRect();
    const left = Math.min(window.innerWidth - tooltip.offsetWidth - 8, Math.max(8, rect.x + rect.width / 2 - tooltip.offsetWidth / 2));
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${rect.top > tooltip.offsetHeight + 12 ? rect.top - tooltip.offsetHeight - 8 : rect.bottom + 8}px`;
  };
  document.addEventListener("pointerover", showTooltip);
  document.addEventListener("focusin", showTooltip);
  document.addEventListener("pointerout", hideTooltip);
  document.addEventListener("focusout", hideTooltip);
  document.addEventListener("click", hideTooltip);
  document.addEventListener("scroll", hideTooltip, true);

  const dialog = document.createElement("dialog");
  dialog.className = "table-dialog";
  dialog.setAttribute("aria-label", "답변 표 크게 보기");
  dialog.innerHTML = '<header class="table-tools"><strong>답변 표</strong><button type="button" data-close-table aria-label="표 닫기">닫기 ×</button></header><div class="table-dialog-body"></div>';
  document.body.appendChild(dialog);
  dialog.querySelector("[data-close-table]").addEventListener("click", () => dialog.close());
  let tableTrigger = null;
  dialog.addEventListener("close", () => {
    dialog.querySelector(".table-dialog-body").replaceChildren();
    // 표의 이슈에서 원문 모달을 열었다면 새 모달의 포커스를 유지한다.
    if (!document.querySelector(".modal-backdrop:not([hidden])")) tableTrigger?.focus();
  });

  const enhanceTables = () => {
    document.querySelectorAll("#messages .markdown-table-wrap, #messages .issue-table-wrap").forEach((wrap) => {
      if (wrap.dataset.tableTools) return;
      wrap.dataset.tableTools = "true";
      const table = wrap.querySelector("table");
      if (!table) return;
      const toolbar = document.createElement("div");
      toolbar.className = "table-tools";
      const count = table.querySelectorAll("tbody tr").length;
      toolbar.innerHTML = `<strong>조회 결과 · ${count}행</strong><button type="button" data-copy-table>표 복사</button><button type="button" data-expand-table>크게 보기 ↗</button>`;
      toolbar.querySelector("[data-copy-table]").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        const text = [...table.rows].map(row => [...row.cells].map(cell => cell.textContent.trim().replace(/\t|\n/g, " ")).join("\t")).join("\n");
        try { await navigator.clipboard.writeText(text); button.textContent = "복사됨"; }
        catch { button.textContent = "복사 실패 · 다시 시도"; }
      });
      toolbar.querySelector("[data-expand-table]").addEventListener("click", (event) => {
        tableTrigger = event.currentTarget;
        const clone = table.cloneNode(true);
        const originals = [...table.querySelectorAll("button")];
        clone.querySelectorAll("button").forEach((button, index) => button.addEventListener("click", () => {
          dialog.close();
          originals[index]?.click();
        }));
        dialog.querySelector(".table-dialog-body").replaceChildren(clone);
        dialog.showModal();
      });
      wrap.prepend(toolbar);
    });
  };
  new MutationObserver(enhanceTables).observe(document.querySelector("#messages"), {childList: true, subtree: true});
  enhanceTables();
  const comment = document.querySelector("#feedbackComment");
  const counter = document.querySelector("#feedbackCount");
  const updateCount = () => { counter.textContent = `${comment.value.length} / 500`; };
  comment.addEventListener("input", updateCount);
  new MutationObserver(updateCount).observe(document.querySelector("#feedbackModal"), {attributes: true, attributeFilter: ["hidden"]});
  const resize = () => document.documentElement.style.setProperty("--app-height", `${window.visualViewport?.height || window.innerHeight}px`);
  window.visualViewport?.addEventListener("resize", resize);
  window.addEventListener("resize", resize);
  resize();
}
