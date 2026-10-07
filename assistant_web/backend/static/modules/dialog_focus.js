const sessions = new WeakMap();
const FOCUSABLE = 'button, a[href], input:not([type="hidden"]), select, textarea, summary, [tabindex]';

function focusableElements(dialog) {
  return [...dialog.querySelectorAll(FOCUSABLE)].filter((element) =>
    !element.disabled && element.tabIndex >= 0 && element.getClientRects().length > 0);
}

export function openDialogFocus(dialog, { initialFocus, opener = document.activeElement, onClose, fallback } = {}) {
  // 비동기 내용 갱신은 창을 새로 연 것으로 취급하지 않는다.
  if (sessions.has(dialog)) {
    if (!dialog.contains(document.activeElement)) (initialFocus || focusableElements(dialog)[0] || dialog).focus();
    return;
  }
  const keydown = (event) => {
    if (event.key === "Escape" && onClose) {
      event.preventDefault();
      event.stopPropagation();
      onClose();
    } else if (event.key === "Tab") {
      const items = focusableElements(dialog);
      const first = items[0];
      const last = items.at(-1);
      if (!first) {
        event.preventDefault();
        dialog.focus();
      } else if (!dialog.contains(document.activeElement) || (event.shiftKey && document.activeElement === first)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
  };
  sessions.set(dialog, { opener, keydown, fallback });
  dialog.tabIndex = -1;
  dialog.addEventListener("keydown", keydown);
  document.body.classList.add("modal-open");
  (initialFocus || focusableElements(dialog)[0] || dialog).focus();
}

export function closeDialogFocus(dialog) {
  const session = sessions.get(dialog);
  if (!session) return;
  dialog.removeEventListener("keydown", session.keydown);
  sessions.delete(dialog);
  const anotherOpen = [...document.querySelectorAll('[aria-modal="true"]')].some((element) => element.getClientRects().length > 0);
  if (!anotherOpen) document.body.classList.remove("modal-open");
  const openerAvailable = session.opener?.isConnected && !session.opener.disabled && session.opener.getClientRects().length > 0;
  const target = openerAvailable ? session.opener : session.fallback?.();
  target?.focus();
}
