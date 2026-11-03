// innerHTML로 다시 그리는 화면에서 직전에 초점이 있던 컨트롤을 찾아 초점을 되돌린다.
// 꾸민 선택 상자는 원래 select의 data 속성으로 찾고, 감싼 뒤의 버튼에 초점을 준다.
function focusKey(container) {
  const active = document.activeElement;
  if (!active || !container?.contains?.(active)) return "";
  const control = active.closest?.(".custom-select")?.querySelector("select") || active;
  const attribute = [...(control.attributes || [])].find((item) => item.name.startsWith("data-") && item.name !== "data-bound");
  if (attribute) return `[${attribute.name}="${CSS.escape(attribute.value)}"]`;
  return control.id ? `#${CSS.escape(control.id)}` : "";
}

// 꾸민 선택 상자는 MutationObserver가 감싼 뒤에 버튼이 생기므로 한 박자 뒤에 초점을 준다.
function restoreFocus(container, key) {
  if (!key) return;
  Promise.resolve().then(() => {
    const target = container.querySelector(key);
    const visible = target?.closest?.(".custom-select")?.querySelector(".custom-select-trigger") || target;
    visible?.focus?.({ preventScroll: true });
  });
}

export { focusKey, restoreFocus };
