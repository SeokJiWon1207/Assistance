const quotedFence = /^>[ \t]?(`{3,})(?:text)?[ \t]*$/;

export function isQuotedSourceFence(line) {
  return quotedFence.test(line);
}

export function renderQuotedSourceBlock(lines, index) {
  const opening = lines[index].match(quotedFence);
  if (!opening) return null;
  const sourceLines = [];
  let cursor = index + 1;
  while (cursor < lines.length) {
    // 원문의 짧은 코드 펜스가 더 긴 바깥 인용 펜스를 닫지 않도록 한다.
    const closing = lines[cursor].match(/^>[ \t]?(`{3,})[ \t]*$/);
    if (closing && closing[1].length >= opening[1].length) {
      cursor += 1;
      break;
    }
    const quotedLine = lines[cursor].match(/^>[ \t]?([\s\S]*)$/);
    if (!quotedLine) break;
    sourceLines.push(quotedLine[1]);
    cursor += 1;
  }
  const element = document.createElement("blockquote");
  element.className = "answer-source-quote";
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.textContent = sourceLines.join("\n");
  pre.appendChild(code);
  element.appendChild(pre);
  if (code.textContent.length > 800) {
    const details = document.createElement("details");
    details.className = "answer-source-long-quote";
    const summary = document.createElement("summary");
    summary.textContent = "긴 인용 원문 보기";
    details.appendChild(summary);
    details.appendChild(element);
    return { element: details, nextIndex: cursor };
  }
  return { element, nextIndex: cursor };
}

export function collapseQuotedSourceHistory(container) {
  const children = [];
  let history = null;
  for (const child of Array.from(container.children)) {
    if (child.className === "answer-heading") {
      history = null;
      if (["진행 이력", "등록 내용"].includes(child.textContent)
          || child.textContent.endsWith(" 전체 근거")) {
        history = document.createElement("details");
        history.className = "answer-source-history";
        const summary = document.createElement("summary");
        summary.textContent = child.textContent.endsWith(" 전체 근거")
          ? `${child.textContent} 보기` : `${child.textContent} 원문 보기`;
        history.appendChild(summary);
        children.push(history);
        continue;
      }
    }
    if (history) history.appendChild(child);
    else children.push(child);
  }
  container.replaceChildren(...children);
  // 짧은 답변을 먼저 표시하고 서버가 보존한 인용·이력은 한 번에 펼친다.
  const blocks = Array.from(container.children);
  const start = blocks.findIndex(child => child.className === "answer-heading"
    && child.textContent === "원문 근거와 전체 이력");
  if (start >= 0) {
    const details = document.createElement("details");
    details.className = "answer-source-history";
    const summary = document.createElement("summary");
    summary.textContent = "원문 근거와 전체 이력 보기";
    details.appendChild(summary);
    blocks.slice(start + 1).forEach(child => details.appendChild(child));
    container.replaceChildren(...blocks.slice(0, start), details);
  }
}
