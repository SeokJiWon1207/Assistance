// 화면에서만 접는다. 저장된 답변·복사 원문·출처 식별자는 바꾸지 않는다.
const expandedByAnswer = new WeakMap();
const SUMMARY = /^(핵심 요약|핵심 내용|핵심 결론|요약|결론|한눈에 보기|주요 결과|summary|conclusion)$/i;
const NEXT_ACTION = /^(권장 조치|다음 조치|권장 다음 행동|다음 행동)$/;
const tag = (node) => node.tagName?.toLowerCase();
const isHeading = (node) => /^h[1-6]$/.test(tag(node));
const headingText = (node) => (node.textContent || "").trim().replace(/^\d+[.)]\s*/, "").replace(/[:：]$/, "").trim();

function keepVisible(node) {
  if (tag(node) === "details" || tag(node) === "table") return true;
  if (["answer-title", "answer-metrics", "answer-issue-list", "answer-callout", "criteria-chips", "query-basis", "issue-table-wrap", "markdown-table-wrap", "notion-card-list", "issue-detail-card"]
    .some(name => node.classList.contains(name))) return true;
  // 조회 한계와 조건은 결과를 해석하는 데 필요하므로 본문에 남긴다.
  return /일부|부분 조회|미확정|미확인|확인\s*불가|확인하지 못|확인할 수 없|조회 실패|불완전|주의[:：]|제외|한계/.test(node.textContent || "");
}

function disclosure(label, answer, key) {
  const details = document.createElement("details");
  details.className = "answer-disclosure";
  const summary = document.createElement("summary");
  const title = document.createElement("span");
  title.className = "answer-disclosure-title";
  title.textContent = label;
  const closed = document.createElement("span");
  closed.className = "answer-disclosure-closed";
  closed.textContent = "펼치기";
  const open = document.createElement("span");
  open.className = "answer-disclosure-open";
  open.textContent = "접기";
  summary.append(title, closed, open);
  details.appendChild(summary);
  details.open = expandedByAnswer.get(answer)?.[key] === true;
  details.addEventListener("toggle", () => {
    expandedByAnswer.set(answer, { ...expandedByAnswer.get(answer), [key]: details.open });
  });
  return details;
}

export function collapseAnswerBody(container, answer, { hasChart = false } = {}) {
  const blocks = Array.from(container.children);
  const readable = blocks.filter(node => tag(node) !== "details");
  if (!hasChart && readable.reduce((sum, node) => sum + node.textContent.length, 0) < 700 && readable.length < 8) return null;

  const groups = [];
  let section = null;
  for (const node of blocks) {
    if (isHeading(node)) {
      section = { nodes: [node], heading: headingText(node) };
      groups.push(section);
    } else if (section) section.nodes.push(node);
    else groups.push({ nodes: [node], heading: "" });
  }
  const summaries = groups.filter(group => SUMMARY.test(group.heading));
  const first = groups.find(group => group.nodes.some(node => !isHeading(node) && tag(node) !== "details"));
  const visible = groups.filter(group => summaries.includes(group) || NEXT_ACTION.test(group.heading)
    || group.nodes.some(keepVisible) || (!summaries.length && group === first));
  const hidden = groups.filter(group => !visible.includes(group));
  if (!hidden.some(group => group.nodes.some(node => !isHeading(node)))) return null;

  const details = disclosure(hasChart ? "집계 설명과 상세 수치" : "자세한 설명", answer, "body");
  details.classList.add("answer-body-disclosure");
  const body = document.createElement("div");
  body.className = "answer-disclosure-body";
  hidden.forEach(group => group.nodes.forEach(node => body.appendChild(node)));
  details.appendChild(body);
  // 명시적인 요약만 앞으로 옮긴다. 요약이 없으면 첫 문단을 그대로 보여 준다.
  const titles = visible.filter(group => group.nodes.some(node => node.classList.contains("answer-title")));
  const ordered = [...titles, ...summaries.filter(group => !titles.includes(group)), ...visible.filter(group => !titles.includes(group) && !summaries.includes(group))];
  container.replaceChildren(...ordered.flatMap(group => group.nodes), details);
  return details;
}

export function presentAssistantAnswer(container, answer) {
  if (answer.loading || answer.streaming || answer.effortChart) return;
  const rendered = container.querySelector(":scope > .bubble > .assistant-answer");
  if (!rendered) return;
  const body = rendered.querySelector(":scope > .notion-answer") || rendered.querySelector(".issue-detail-text") || rendered;
  const hasChart = Boolean(answer.effortRanking || answer.effortTrend);
  const details = collapseAnswerBody(body, answer, { hasChart });
  // 차트가 이미 핵심 결과를 보여 주므로 상세 설명은 차트 다음에 둔다.
  if (hasChart && details) container.appendChild(details);
}

export function wrapRelatedSources(sources, answer, count) {
  if (answer.loading || answer.streaming) return sources;
  const details = disclosure(`관련 자료 · ${count}건`, answer, "sources");
  details.classList.add("answer-related-sources");
  details.appendChild(sources);
  return details;
}
