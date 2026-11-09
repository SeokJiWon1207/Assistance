// 서버의 원문 근거 정리(reports/quote_rendering.py)는 긴 근거를 다음 줄들로 접어서 보낸다.
//   <details>
//   <summary>전체 이력과 원문 근거 보기</summary>
//   …본문(Markdown)…
//   </details>
// 답변 렌더러는 HTML을 해석하지 않으므로 이 정확한 줄 형식만 찾아 실제 접기 상자로 바꾼다.
// 다른 HTML 태그나 한 줄에 섞인 태그는 계속 글자로 그린다.
const OPEN = /^\s*<details>\s*$/;
const CLOSE = /^\s*<\/details>\s*$/;
const SUMMARY = /^\s*<summary>([^<>]{1,120})<\/summary>\s*$/;

function parseDetailsBlock(lines, index) {
  if (!OPEN.test(lines[index] ?? "")) return null;
  let cursor = index + 1;
  while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;
  const summary = (lines[cursor] ?? "").match(SUMMARY);
  if (!summary) return null;
  // 같은 깊이의 닫는 줄을 찾는다. 안쪽에 같은 형식이 또 있으면 그만큼 건너뛴다.
  let depth = 1;
  for (let end = cursor + 1; end < lines.length; end += 1) {
    if (OPEN.test(lines[end])) depth += 1;
    else if (CLOSE.test(lines[end])) depth -= 1;
    if (depth === 0) {
      return { summary: summary[1].trim(), innerLines: lines.slice(cursor + 1, end), nextIndex: end + 1 };
    }
  }
  return null;
}

// 안쪽 본문은 답변 렌더러(renderInner)로 다시 그려 원문 인용·표·목록 처리를 그대로 쓴다. 태그는 DOM으로만 만든다.
function foldedDetails(folded, renderInner) {
  const details = document.createElement("details");
  details.className = "answer-evidence-details";
  const summary = document.createElement("summary");
  summary.textContent = folded.summary;
  details.appendChild(summary);
  details.append(...renderInner(folded.innerLines.join("\n")).childNodes);
  return details;
}

export { parseDetailsBlock, foldedDetails };

// 복사할 때는 접기 줄을 없애고 요약 문구만 굵은 제목으로 남긴다(본문은 그대로).
function stripDetailsMarkup(text) {
  return String(text ?? "").split("\n").flatMap((line) => {
    if (OPEN.test(line) || CLOSE.test(line)) return [];
    const summary = line.match(SUMMARY);
    return summary ? [`**${summary[1].trim()}**`] : [line];
  }).join("\n").replace(/\n{3,}/g, "\n\n");
}

export { stripDetailsMarkup };
