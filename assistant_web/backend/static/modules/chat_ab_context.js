export function selectAnswerContext(messages, index, variant) {
  const message = messages[index];
  if (!message?.auditId || !["A", "B"].includes(variant)
      || (variant === "B" && message.abTest?.variantB?.status !== "ok")) return messages;
  // 보관 목록과 화면은 같은 메시지 객체를 공유할 수 있다. 저장 전 원본 비교를 보존한다.
  return messages.map((item, position) => position === index ? { ...item, followupVariant: variant } : item);
}

export function appendAnswerContextControls(message, panels, { busy, onChoose }) {
  for (const [variant, panel] of [["A", panels[0]], ["B", panels[1]]]) {
    const button = document.createElement("button");
    const selected = (message.followupVariant || "A") === variant;
    button.type = "button"; button.className = "secondary-button";
    button.textContent = selected ? "후속 질문 기준으로 선택됨" : "이 답변으로 계속";
    button.setAttribute("aria-label", `${variant} 답변으로 계속`);
    button.setAttribute("aria-pressed", String(selected));
    button.disabled = busy || (variant === "B" && message.abTest?.variantB?.status !== "ok");
    button.addEventListener("click", () => onChoose(variant));
    panel.appendChild(button);
  }
}
