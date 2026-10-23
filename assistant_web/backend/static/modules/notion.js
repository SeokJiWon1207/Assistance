import { state } from "./runtime.js?v=20260911-6";
import { decodeIssueField } from "./issue_text.js?v=20261014-1";
import { statusToneClass, renderCriteriaChips, appendAnswerParagraph, cleanMarkdown, escapeHtml } from "./chat_rendering.js?v=20260911-6";

function isMarkdownTableRow(line) {
  const trimmed = String(line || "").trim();
  return trimmed.startsWith("|") && trimmed.endsWith("|") && trimmed.length > 1;
}

function isMarkdownTableSeparatorRow(line) {
  const trimmed = String(line || "").trim();
  if (!isMarkdownTableRow(trimmed)) return false;
  const cells = splitMarkdownTableRow(trimmed);
  if (!cells.length) return false;
  return cells.every((cell) => /^:?-{1,}:?$/.test(cell.trim()));
}

function splitMarkdownTableRow(line) {
  let trimmed = String(line || "").trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((cell) => decodeIssueField(cleanMarkdown(cell).trim()));
}

function renderMarkdownTable(headerCells, bodyRows) {
  const wrapper = document.createElement("div");
  wrapper.className = "markdown-table-wrap";

  const table = document.createElement("table");
  table.className = "markdown-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  headerCells.forEach((cell) => {
    const th = document.createElement("th");
    th.textContent = cell;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const atomicColumns = headerCells.map(cell => /^(?:ID|번호|이슈\s*(?:ID|번호)|고객사(?:명)?|프로젝트|상태|담당자|등록자|요청자|등록일(?:시)?|수정일(?:시)?|완료일|건수|매체|플랫폼)$/i.test(cell.trim()));
  bodyRows.forEach((cells) => {
    const tr = document.createElement("tr");
    cells.forEach((cell, index) => {
      const td = document.createElement("td");
      td.className = atomicColumns[index] ? "table-atomic" : "table-prose";
      td.textContent = cell;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);

  wrapper.appendChild(table);
  return wrapper;
}

function extractInlineMarkdownTable(content) {
  // 모델이 "표 중심" 답변을 낼 때 줄바꿈 없이 한 줄로 이어붙이는 경우가 있다
  // ("| a | b | |---|---| | c | d |"). 프롬프트를 강화해도 완전히 막긴 어려워서,
  // 구분자 행(|---|---|...)의 컬럼 수를 기준으로 셀을 다시 묶어 표로 복원한다.
  const text = String(content || "");
  const separatorMatch = text.match(/\|(?:\s*:?-{1,}:?\s*\|){1,}/);
  if (!separatorMatch) return null;

  const columnCount = splitMarkdownTableRow(separatorMatch[0]).length;
  if (columnCount < 1) return null;

  const beforeSeparator = text.slice(0, separatorMatch.index).trim();
  if (!beforeSeparator.endsWith("|")) return null;
  const headerCells = splitMarkdownTableRow(beforeSeparator).slice(-columnCount);
  if (headerCells.length !== columnCount) return null;

  let rest = text.slice(separatorMatch.index + separatorMatch[0].length).trim();
  if (rest.startsWith("|")) rest = rest.slice(1);
  // 행 경계마다 "이전 행의 마지막 |"와 "다음 행의 시작 |"가 붙어 있어(예: "값 | | 다음값"),
  // 그대로 나누면 빈 셀이 끼어 컬럼이 밀린다. 빈 셀은 실제 데이터가 아니라 이 경계
  // 아티팩트이므로 제거한다.
  const rawCells = rest.split("|").map((cell) => cell.trim()).filter((cell) => cell !== "");

  const bodyRows = [];
  let i = 0;
  for (; i + columnCount <= rawCells.length; i += columnCount) {
    bodyRows.push(rawCells.slice(i, i + columnCount).map((cell) => cleanMarkdown(cell).trim()));
  }
  if (!bodyRows.length) return null;

  const trailingText = rawCells.slice(i).join(" ").trim();
  return { headerCells, bodyRows, trailingText };
}

function extractNotionProseLines(content) {
  // 결정형 포맷터가 만드는 "검색 기준:" 줄과 "N. **제목**" + 메타 불릿 줄은
  // 카드 목록과 내용이 겹치므로 걸러내고, 그 외 모델이 실제로 작성한
  // 설명 문장(예: 상세 내용 요청에 대한 답변)은 그대로 보존해 화면에 보여준다.
  const rawLines = String(content || "").split("\n");
  const prose = [];
  for (let i = 0; i < rawLines.length; i += 1) {
    const trimmed = rawLines[i].trim();
    if (!trimmed) {
      prose.push("");
      continue;
    }
    if (trimmed.startsWith("검색 기준:")) continue;
    if (/^\d+\.\s+\*\*.+\*\*$/.test(trimmed)) {
      if (i + 1 < rawLines.length && /^\s*-\s/.test(rawLines[i + 1])) {
        i += 1;
      }
      continue;
    }
    prose.push(rawLines[i]);
  }
  return prose.join("\n").trim();
}

function renderNotionAnswer(content, sources = [], messageIndex = 0) {
  const section = document.createElement("section");
  section.className = "notion-answer";

  const criteriaLine = String(content || "")
    .split("\n")
    .map((line) => cleanMarkdown(line).trim())
    .find((line) => line.startsWith("검색 기준:"));

  const proseText = extractNotionProseLines(content);
  if (proseText) {
    proseText.split(/\n{2,}/).forEach((part) => {
      appendAnswerParagraph(section, part, sources, messageIndex);
    });
  }

  if (criteriaLine) {
    section.appendChild(renderCriteriaChips(criteriaLine));
  }

  const list = document.createElement("div");
  list.className = "notion-card-list";

  sources.forEach((source, sourceIndex) => {
    const key = `notion-${messageIndex}-${sourceIndex}`;
    state.sourceMap.set(key, source);

    const card = document.createElement("button");
    card.type = "button";
    card.className = "notion-card";
    card.dataset.sourceKey = key;

    const metaItems = [
      source.database_label || "Notion DB",
      source.status || "상태 미상",
      source.owner && source.owner !== "담당자 미지정" ? `담당 ${source.owner}` : "",
      source.due_date ? `기한 ${source.due_date}` : "",
      source.updated_at ? `수정 ${source.updated_at}` : "",
    ].filter(Boolean);

    card.innerHTML = `
      <div class="notion-card-top">
        <strong>${escapeHtml(source.database_label || "Notion")}</strong>
        <em class="status-badge ${statusToneClass(source.status)}">${escapeHtml(source.status || "상태 미상")}</em>
      </div>
      <h3>${escapeHtml(source.title || "제목 없음")}</h3>
      <div class="notion-card-meta">${escapeHtml(metaItems.join(" · "))}</div>
    `;
    list.appendChild(card);
  });

  section.appendChild(list);
  return section;
}

function formatNotionDetail(source) {
  const detail = source.detail || {};
  const cleanContent = cleanNotionPreview(source.preview || detail.content || "");
  const meta = [
    `- DB: ${source.database_label || detail.database_label || "Notion DB"}`,
    `- 상태: ${source.status || detail.status || "상태 미상"}`,
    `- 담당자: ${source.owner || detail.owner || "담당자 미지정"}`,
    `- 기한: ${source.due_date || detail.due_date || "미상"}`,
    `- 최근 수정: ${source.updated_at || detail.last_edited_time || "미상"}`,
  ];
  if (source.url || detail.url) meta.push(`- URL: ${source.url || detail.url}`);

  return [
    `[Notion 항목]`,
    ...meta,
    ``,
    `[제목]`,
    source.title || "제목 없음",
    ``,
    `[내용]`,
    cleanContent || "상세 내용은 Notion 원문에서 확인해 주세요.",
  ].join("\n");
}

function cleanNotionPreview(value) {
  const noisyPrefixes = /^(F_|PC_|QC|문의_|차주|금주|완료주차|고객사요청일|최종갱신|firstdayofmonth|이베스트VOC|OS|차주개발|차주전달|금주전달|금주차|완료|F_)/i;
  return String(value || "")
    .split(/\n+/)
    .map((line) => line.trim())
    .filter((line) => line && !noisyPrefixes.test(line))
    .slice(0, 8)
    .join("\n");
}

export {
  isMarkdownTableRow,
  isMarkdownTableSeparatorRow,
  splitMarkdownTableRow,
  renderMarkdownTable,
  extractInlineMarkdownTable,
  extractNotionProseLines,
  renderNotionAnswer,
  formatNotionDetail,
  cleanNotionPreview,
};

export { automaticSourceTools } from "./runtime.js?v=20260911-6";
