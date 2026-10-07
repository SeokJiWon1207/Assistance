import { icon } from "./icons.js?v=20260911-6";
import { renderQueryBasis } from "./query_basis.js?v=20261007-1";
import { state } from "./runtime.js?v=20260911-6";
import {
  isMarkdownTableRow,
  isMarkdownTableSeparatorRow,
  splitMarkdownTableRow,
  renderMarkdownTable,
  extractInlineMarkdownTable,
  renderNotionAnswer,
} from "./notion.js?v=20260911-6";
import {
  openChromaModal,
  parseDetailSections,
  detailBadgeClass,
  buildDetailValue,
  renderDetailContent,
  closeChromaModal,
  formatMantisDetail,
  displayIssueStatus,
  openSourceDetail,
} from "./chat_source_detail.js?v=20260911-6";

function hasIssueSummaryList(content) {
  return String(content || "")
    .split("\n")
    .some((line) => parseIssueSummaryLine(line));
}

function shouldHideSourceList(content, sources = []) {
  const text = String(content || "");
  if (sources.length && sources.every((source) => source.type === "notion")) return true;
  if (hasIssueSummaryList(text)) return true;
  return sources.some((source) => {
    if (!source.issue_id) return false;
    const issueIdPattern = new RegExp(`#?0*${String(source.issue_id)}\\b`);
    return issueIdPattern.test(text);
  });
}

// 상태 라벨을 색상 톤 클래스로 매핑한다. 부분 문자열 기준이라 "종료된 이슈",
// "결재완료(팀장)"처럼 변형된 라벨도 자연스럽게 커버된다.
function statusToneClass(label) {
  const text = String(label || "");
  if (/정보 ?부족/.test(text)) return "status-red";
  if (/해결|개발완료|^완료|결재완료/.test(text)) return "status-green";
  if (/할당/.test(text)) return "status-amber";
  if (/확인|신규|진행|등록됨/.test(text)) return "status-blue";
  if (/검토|결재요청/.test(text)) return "status-purple";
  if (/종료|보관/.test(text)) return "status-neutral";
  return "status-neutral";
}

const CALLOUT_ICONS = {
  info: icon("info"),
  warning: icon("warning"),
};

// "참고:"/"데이터 기준:" 같은 안내문을 일반 문단 대신 아이콘 콜아웃으로 그린다.
function calloutToneFor(paragraphText) {
  if (paragraphText.startsWith("참고:")) return "warning";
  if (paragraphText.startsWith("데이터 기준:")) return "info";
  if (paragraphText.startsWith("요청한 ") && paragraphText.includes("건입니다")) return "info";
  return null;
}

// **굵게**, `코드` 인라인 마크다운을 벗겨내지 않고 실제 요소로 그린다.
// innerHTML 대신 textContent 기반 노드 조립이라 XSS 걱정 없이 안전하다.
function appendInlineMarkdown(target, text, sources = [], messageIndex = 0) {
  const pattern = /(\*\*[^*\n]+\*\*|`[^`\n]+`|#\d{4,7}\b)/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(text))) {
    if (match.index > last) {
      target.appendChild(document.createTextNode(text.slice(last, match.index)));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = token.slice(2, -2);
      target.appendChild(strong);
    } else if (token.startsWith("#")) {
      // 답변에 언급된 이슈ID가 실제 근거 목록(sources)에 있을 때만 클릭 가능한
      // 참조로 만든다. 무관한 숫자를 링크로 오인시키지 않기 위한 안전장치.
      const issueId = Number(token.slice(1));
      const source = sources.find((item) => Number(item.issue_id) === issueId);
      if (source) {
        const key = `inline-${messageIndex}-${issueId}`;
        state.sourceMap.set(key, source);
        const ref = document.createElement("button");
        ref.type = "button";
        ref.className = "inline-issue-ref";
        ref.dataset.sourceKey = key;
        ref.textContent = token;
        target.appendChild(ref);
      } else {
        target.appendChild(document.createTextNode(token));
      }
    } else {
      const code = document.createElement("code");
      code.className = "inline-code";
      code.textContent = token.slice(1, -1);
      target.appendChild(code);
    }
    last = match.index + token.length;
  }
  if (last < text.length) {
    target.appendChild(document.createTextNode(text.slice(last)));
  }
}

// "검색 기준: 고객사=X, 플랫폼=Y, 정렬: ..." 줄을 태그 칩 목록으로 그린다.
// "담당자=김a, 이b"처럼 값 안에 쉼표가 있는 경우, '='도 '정렬'도 없는 조각은
// 직전 칩의 값에 이어 붙인다.
function renderCriteriaChips(criteriaText) {
  const wrap = document.createElement("div");
  wrap.className = "criteria-chips";
  const label = document.createElement("span");
  label.className = "criteria-chips-label";
  label.textContent = "검색 기준";
  wrap.appendChild(label);

  const body = criteriaText.replace(/^검색 기준:\s*/, "");
  const chips = [];
  body.split(/,\s*/).forEach((segment) => {
    const trimmed = segment.trim();
    if (!trimmed) return;
    if (trimmed.includes("=") || trimmed.startsWith("정렬") || !chips.length) {
      chips.push(trimmed);
    } else {
      chips[chips.length - 1] += `, ${trimmed}`;
    }
  });

  chips.forEach((chipText) => {
    const chip = document.createElement("span");
    chip.className = "criteria-chip";
    const eqIndex = chipText.indexOf("=");
    const colonIndex = chipText.indexOf(":");
    const splitIndex = eqIndex > 0 ? eqIndex : colonIndex;
    if (splitIndex > 0) {
      const small = document.createElement("small");
      small.textContent = chipText.slice(0, splitIndex).trim();
      chip.appendChild(small);
      chip.appendChild(document.createTextNode(chipText.slice(splitIndex + 1).trim()));
    } else {
      chip.textContent = chipText;
    }
    wrap.appendChild(chip);
  });
  return wrap;
}

// 답변 텍스트 블록 하나를 콜아웃/검색기준 칩/제목/리스트/문단 중 알맞은
// 형태로 그린다. 마크다운 표기를 벗겨서 버리는 대신 실제 스타일로 살린다.
function appendAnswerParagraph(container, rawBlock, sources = [], messageIndex = 0) {
  const cleaned = cleanMarkdown(rawBlock).trim();
  if (!cleaned) return;

  const tone = calloutToneFor(cleaned);
  if (tone) {
    const callout = document.createElement("div");
    callout.className = `answer-callout callout-${tone}`;
    callout.innerHTML = `${CALLOUT_ICONS[tone]}<span></span>`;
    callout.querySelector("span").textContent = cleaned;
    container.appendChild(callout);
    return;
  }

  if (cleaned.startsWith("검색 기준:") || cleaned.startsWith("조회 기준:")) {
    container.appendChild(cleaned.startsWith("검색") ? renderCriteriaChips(cleaned) : renderQueryBasis(rawBlock));
    return;
  }

  const rawLines = String(rawBlock)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  let listBuffer = [];
  let listTag = "ul";
  let paragraphBuffer = [];

  const flushList = () => {
    if (!listBuffer.length) return;
    const list = document.createElement(listTag);
    list.className = "answer-list";
    listBuffer.forEach((itemText) => {
      const item = document.createElement("li");
      appendInlineMarkdown(item, itemText, sources, messageIndex);
      list.appendChild(item);
    });
    container.appendChild(list);
    listBuffer = [];
  };

  const flushParagraph = () => {
    if (!paragraphBuffer.length) return;
    const paragraph = document.createElement("p");
    paragraphBuffer.forEach((lineText, lineIndex) => {
      if (lineIndex > 0) paragraph.appendChild(document.createElement("br"));
      appendInlineMarkdown(paragraph, lineText, sources, messageIndex);
    });
    container.appendChild(paragraph);
    paragraphBuffer = [];
  };

  rawLines.forEach((line) => {
    const headingMatch = line.match(/^#{1,6}\s+(.+)$/);
    if (headingMatch) {
      flushParagraph();
      flushList();
      const heading = document.createElement("h4");
      heading.className = "answer-heading";
      appendInlineMarkdown(heading, headingMatch[1], sources, messageIndex);
      container.appendChild(heading);
      return;
    }
    const bulletMatch = line.match(/^[-*•]\s+(.+)$/);
    if (bulletMatch) {
      flushParagraph();
      if (listBuffer.length && listTag !== "ul") flushList();
      listTag = "ul";
      listBuffer.push(bulletMatch[1]);
      return;
    }
    const numberedMatch = line.match(/^\d{1,2}\.\s+(.+)$/);
    if (numberedMatch) {
      flushParagraph();
      if (listBuffer.length && listTag !== "ol") flushList();
      listTag = "ol";
      listBuffer.push(numberedMatch[1]);
      return;
    }
    flushList();
    paragraphBuffer.push(line);
  });
  flushParagraph();
  flushList();
}

function renderAssistantContent(content, sources = [], messageIndex = 0) {
  const wrap = document.createElement("div");
  wrap.className = "assistant-answer";
  if (sources.length && sources.every((source) => source.type === "notion")) {
    wrap.appendChild(renderNotionAnswer(content, sources, messageIndex));
    return wrap;
  }

  const issueDetail = parseIssueDetailContent(content);
  if (issueDetail) {
    wrap.appendChild(renderIssueDetailCard(issueDetail, sources, messageIndex));
    return wrap;
  }

  const lines = String(content || "").split("\n");
  let textBuffer = [];
  let issueBuffer = [];

  const flushText = () => {
    const block = textBuffer.join("\n").trim();
    textBuffer = [];
    if (!block) return;

    block.split(/\n{2,}/).forEach((part) => {
      appendAnswerParagraph(wrap, part, sources, messageIndex);
    });
  };

  const flushIssues = () => {
    if (!issueBuffer.length) return;
    wrap.appendChild(renderIssueSummaryList(issueBuffer, sources, messageIndex));
    issueBuffer = [];
  };

  let index = 0;
  while (index < lines.length) {
    const line = lines[index];

    // "답변 스타일: 표 중심"으로 모델이 GFM 마크다운 표를 낼 때, 이걸 그냥
    // 문단으로 흘리면 "| a | b | |---|---|" 같은 파이프 문자가 그대로 노출된다.
    // 헤더 행 다음에 구분선(---) 행이 오는 표준 패턴을 감지해 실제 <table>로 그린다.
    if (
      isMarkdownTableRow(line) &&
      index + 1 < lines.length &&
      isMarkdownTableSeparatorRow(lines[index + 1])
    ) {
      const issue = parseIssueSummaryLine(line);
      if (!issue) {
        flushText();
        flushIssues();
        const headerCells = splitMarkdownTableRow(line);
        const bodyRows = [];
        let cursor = index + 2;
        while (cursor < lines.length && isMarkdownTableRow(lines[cursor])) {
          bodyRows.push(splitMarkdownTableRow(lines[cursor]));
          cursor += 1;
        }
        wrap.appendChild(renderMarkdownTable(headerCells, bodyRows));
        index = cursor;
        continue;
      }
    }

    const issue = parseIssueSummaryLine(line);
    if (issue) {
      flushText();
      issueBuffer.push(issue);
      index += 1;
      continue;
    }
    flushIssues();
    textBuffer.push(line);
    index += 1;
  }

  flushText();
  flushIssues();

  const hasTable = Array.from(wrap.children).some(
    (child) => child.className === "markdown-table-wrap",
  );
  if (!hasTable) {
    const inlineTable = extractInlineMarkdownTable(content);
    if (inlineTable) {
      wrap.innerHTML = "";
      wrap.appendChild(renderMarkdownTable(inlineTable.headerCells, inlineTable.bodyRows));
      if (inlineTable.trailingText) {
        const paragraph = document.createElement("p");
        paragraph.textContent = cleanMarkdown(inlineTable.trailingText).trim();
        wrap.appendChild(paragraph);
      }
      return wrap;
    }
  }

  if (!wrap.children.length) {
    const paragraph = document.createElement("p");
    paragraph.textContent = String(content || "");
    wrap.appendChild(paragraph);
  }
  return wrap;
}

function parseIssueDetailContent(content) {
  const text = String(content || "").trim();
  const titleMatch = text.match(/^\*\*#?(\d+)\s+(.+?)\*\*$/m) || text.match(/^#?(\d+)\s+(.+)$/m);
  const contentMarker = text.match(/^\*\*내용\*\*$/m);
  if (!titleMatch || !contentMarker) return null;

  const beforeTitle = text.slice(0, titleMatch.index).trim();
  const afterTitle = text.slice((titleMatch.index || 0) + titleMatch[0].length, contentMarker.index).trim();
  const body = text.slice((contentMarker.index || 0) + contentMarker[0].length).trim();
  const meta = {};

  afterTitle.split("\n").forEach((line) => {
    const match = line.match(/^-\s*([^:]+):\s*(.*)$/);
    if (!match) return;
    meta[match[1].trim()] = match[2].trim();
  });

  return {
    criteria: cleanMarkdown(beforeTitle),
    id: titleMatch[1],
    title: cleanMarkdown(titleMatch[2]),
    project: meta["프로젝트"] || "",
    status: meta["상태"] || "",
    handler: meta["담당자"] || "",
    createdAt: meta["등록일"] || "",
    screenNumber: meta["화면번호"] || "",
    dateFields: Object.fromEntries(
      Object.entries(meta).filter(([key]) => !["프로젝트", "상태", "담당자", "등록일", "화면번호"].includes(key)),
    ),
    body,
  };
}

function renderIssueDetailCard(issue, sources = [], messageIndex = 0) {
  const source = sources.find((item) => Number(item.issue_id) === Number(issue.id));
  const card = document.createElement("article");
  card.className = "issue-detail-card";

  if (issue.criteria) {
    // 도입 문장과 "검색 기준:" 줄이 섞여 있다. 줄 단위로 블록 렌더러에 태워
    // 도입문은 문단으로, 검색 기준은 칩으로 각각 그린다.
    issue.criteria.split("\n").forEach((line) => {
      appendAnswerParagraph(card, line, sources, messageIndex);
    });
  }

  const sectionLabel = document.createElement("div");
  sectionLabel.className = "issue-section-label";
  sectionLabel.textContent = "이슈 상세 정보";
  card.appendChild(sectionLabel);

  const header = document.createElement("header");
  header.className = "issue-detail-header";
  header.innerHTML = `
    <div>
      <span class="issue-detail-id">#${escapeHtml(issue.id)}</span>
      <h3>${escapeHtml(issue.title)}</h3>
    </div>
    ${issue.status ? `<em class="status-badge ${statusToneClass(issue.status)}">${escapeHtml(issue.status)}</em>` : ""}
  `;
  card.appendChild(header);

  const metaItems = [
    ["프로젝트", issue.project],
    ["담당자", issue.handler],
    ["등록일", issue.createdAt],
    ["화면번호", issue.screenNumber],
    ...Object.entries(issue.dateFields || {}),
  ].filter(([, value]) => value && value !== "미상");

  if (metaItems.length) {
    const metaGrid = document.createElement("dl");
    metaGrid.className = "issue-detail-meta";
    metaItems.forEach(([label, value]) => {
      const item = document.createElement("div");
      item.innerHTML = `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`;
      metaGrid.appendChild(item);
    });
    card.appendChild(metaGrid);
  }

  const body = document.createElement("section");
  body.className = "issue-detail-body";
  const bodyTitle = document.createElement("h4");
  bodyTitle.textContent = "요청 내용";
  body.appendChild(bodyTitle);

  const bodyText = document.createElement("div");
  bodyText.className = "issue-detail-text";
  issue.body.split(/\n{2,}/).forEach((part) => {
    let paragraphText = part.trim();
    if (!paragraphText) return;
    const section = paragraphText.match(/^\*\*(사유 및 원인|조치 및 개선)\*\*\s*\n([\s\S]*)$/);
    if (section) {
      const heading = document.createElement("h4");
      heading.textContent = section[1];
      bodyText.appendChild(heading);
      paragraphText = section[2];
    }
    const paragraph = document.createElement("p");
    paragraph.textContent = paragraphText;
    bodyText.appendChild(paragraph);
  });
  body.appendChild(bodyText);
  card.appendChild(body);

  const notes = buildIssueReferenceNotes(issue, source, sources);
  if (notes.length) {
    const noteSection = document.createElement("section");
    noteSection.className = "issue-reference-notes";
    noteSection.innerHTML = `<h4>참고 사항</h4>`;
    const noteList = document.createElement("ul");
    notes.forEach((note) => {
      const item = document.createElement("li");
      item.textContent = note;
      noteList.appendChild(item);
    });
    noteSection.appendChild(noteList);
    card.appendChild(noteSection);
  }

  if (source) {
    const key = `detail-${messageIndex}-${issue.id}`;
    state.sourceMap.set(key, source);
    const action = document.createElement("button");
    action.className = "issue-detail-action";
    action.type = "button";
    action.dataset.sourceKey = key;
    action.textContent = "Mantis 상세 보기";
    card.appendChild(action);
  }

  return card;
}

function buildIssueReferenceNotes(issue, source, sources = []) {
  const notes = [];
  const status = issue.status || source?.status || "";
  if (status) {
    notes.push(`현재 상태는 ${status}입니다.`);
  }
  if (issue.screenNumber && issue.screenNumber !== "미상") {
    notes.push(`화면번호 ${issue.screenNumber} 기준으로 연관 이슈를 추가 조회할 수 있습니다.`);
  }
  const dateLabels = Object.keys(issue.dateFields || {});
  if (dateLabels.length) {
    notes.push(`${dateLabels.join(", ")} 일정 필드가 함께 확인되었습니다.`);
  }
  const relatedCount = sources.filter((item) => item.issue_id && Number(item.issue_id) !== Number(issue.id)).length;
  if (relatedCount > 0) {
    notes.push(`아래 관련 자료에서 함께 조회된 이슈 ${relatedCount}건을 확인할 수 있습니다.`);
  }
  return notes.slice(0, 3);
}

function renderFollowUps(followUps = []) {
  const section = document.createElement("section");
  section.className = "follow-up-panel";
  section.innerHTML = `<h4>후속 질문</h4>`;
  const list = document.createElement("div");
  list.className = "follow-up-list";

  followUps.forEach((question) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "follow-up-chip";
    button.dataset.followUp = question;
    button.textContent = question;
    list.appendChild(button);
  });

  section.appendChild(list);
  return section;
}

function cleanMarkdown(value) {
  return String(value)
    .replace(/\*\*/g, "")
    .replace(/`/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[*-]\s+/gm, "• ")
    .replace(/[ \t]+\n/g, "\n");
}

function parseIssueSummaryLine(line) {
  const trimmed = String(line || "").trim();
  if (!/^\d+\.\s/.test(trimmed) || !/\*?\*?ID\*?\*?\s*:/.test(trimmed)) {
    return null;
  }

  const withoutNumber = trimmed.replace(/^\d+\.\s*/, "");
  const parts = cleanMarkdown(withoutNumber).split("|");
  const issue = {};

  parts.forEach((part) => {
    const [rawLabel, ...rawValue] = part.split(":");
    if (!rawLabel || rawValue.length === 0) return;
    const label = rawLabel.trim();
    const value = rawValue.join(":").trim();
    if (label === "ID") issue.id = value.replace(/^#/, "");
    if (label === "프로젝트") issue.project = value;
    if (label === "제목") issue.title = value;
    if (label === "상태") issue.status = value;
    if (label === "담당자") issue.handler = value;
    if (label === "등록일") issue.createdAt = value;
  });

  return issue.id && issue.title ? issue : null;
}

function renderIssueListSkeleton(rows = 3) {
  const list = document.createElement("div");
  list.className = "issue-summary-list skeleton-list";
  for (let i = 0; i < rows; i += 1) {
    const row = document.createElement("div");
    row.className = "issue-summary-row skeleton-row";
    row.innerHTML = `
      <div class="skeleton-line skeleton-line-title"></div>
      <div class="skeleton-line skeleton-line-meta"></div>
    `;
    list.appendChild(row);
  }
  return list;
}

function issueValue(issue, source, key, fallback = "") {
  const sourceValue = source?.[key];
  const localValue = issue?.[key];
  return sourceValue || localValue || fallback;
}

function issuePriorityLabel(issue, source) {
  return (
    issueValue(issue, source, "priority") ||
    issueValue(issue, source, "severity") ||
    issueValue(issue?.detail || {}, source?.detail || {}, "priority") ||
    issueValue(issue?.detail || {}, source?.detail || {}, "severity") ||
    "-"
  );
}

function renderIssueSummaryList(issues, sources = [], messageIndex = 0) {
  const wrapper = document.createElement("div");
  wrapper.className = "issue-table-wrap";

  const table = document.createElement("table");
  table.className = "issue-table";

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["이슈 번호", "프로젝트", "제목", "상태", "중요도", "담당자", "등록일"].forEach((label) => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  issues.forEach((issue) => {
    const source = sources.find((item) => Number(item.issue_id) === Number(issue.id));
    const row = document.createElement("tr");
    row.className = "issue-table-row";
    if (source) {
      const key = `answer-${messageIndex}-${issue.id}`;
      state.sourceMap.set(key, source);
      row.classList.add("issue-table-clickable");
      row.dataset.sourceKey = key;
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-label", `#${issue.id} 이슈 상세 보기`);
    }

    const idCell = document.createElement("td");
    idCell.className = "issue-table-id";
    idCell.textContent = `#${issue.id}`;

    const projectCell = document.createElement("td");
    projectCell.className = "issue-table-project";
    projectCell.textContent = issueValue(issue, source, "project", "미상");

    const titleCell = document.createElement("td");
    titleCell.className = "issue-table-title";
    titleCell.textContent = issueValue(issue, source, "title") || issue.summary || "제목 없음";

    const statusCell = document.createElement("td");
    const statusValue = issueValue(issue, source, "status", "상태 미상");
    if (statusValue) {
      const status = document.createElement("span");
      status.className = `status-badge ${statusToneClass(statusValue)}`;
      status.textContent = statusValue;
      statusCell.appendChild(status);
    }

    const priorityCell = document.createElement("td");
    priorityCell.className = "issue-table-priority";
    priorityCell.textContent = issuePriorityLabel(issue, source);

    const handlerCell = document.createElement("td");
    handlerCell.textContent = issueValue(issue, source, "handler", "담당자 미지정");

    const dateCell = document.createElement("td");
    dateCell.className = "issue-table-date";
    dateCell.textContent = issueValue(issue, source, "created_at") || issue.createdAt || "미상";

    row.append(idCell, projectCell, titleCell, statusCell, priorityCell, handlerCell, dateCell);
    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  wrapper.appendChild(table);
  return wrapper;
}

function renderSourceList(sources, messageIndex, keyPrefix = "source") {
  const wrap = document.createElement("div");
  wrap.className = "source-list";
  const isLongList = sources.length > 5;
  const panelId = `${keyPrefix}-panel-${messageIndex}`;
  const sourceLabel = sources.every((source) => source.type === "notion") ? "관련 Notion" : "관련 자료";
  const toggle = document.createElement("button");
  toggle.className = "source-list-toggle";
  toggle.type = "button";
  toggle.dataset.sourceToggle = panelId;
  toggle.setAttribute("aria-expanded", String(!isLongList));
  toggle.innerHTML = `
    <span>${sourceLabel} <strong>${sources.length}</strong>건</span>
    <em>${isLongList ? "펼치기" : "접기"}${icon("chevron-down")}</em>
  `;

  const panel = document.createElement("div");
  panel.id = panelId;
  panel.className = "source-list-panel";
  panel.hidden = isLongList;

  wrap.appendChild(toggle);

  sources.forEach((source, sourceIndex) => {
    const key = `${keyPrefix}-${messageIndex}-${sourceIndex}`;
    state.sourceMap.set(key, source);
    const button = document.createElement("button");
    button.className = "related-issue-item";
    button.type = "button";
    button.dataset.sourceKey = key;
    const typeLabel = source.type === "notion" ? "Notion" : source.type === "chroma" ? "과거 이슈" : "Mantis";
    const primaryId = source.type === "notion" ? (source.database_label || "Notion") : `#${source.issue_id || "-"}`;
    const secondary = source.type === "notion"
      ? [source.status, source.owner].filter(Boolean).join(" · ") || "Notion DB"
      : source.project || "프로젝트 미상";
    button.innerHTML = `
      <div class="related-issue-main">
        <div class="related-issue-top">
          <strong>${escapeHtml(primaryId)}</strong>
          <span>${escapeHtml(secondary)}</span>
          <em>${escapeHtml(typeLabel)}</em>
        </div>
        <h4>${escapeHtml(source.title || "제목 없음")}</h4>
      </div>
      <span class="related-issue-action">상세</span>
    `;
    panel.appendChild(button);
  });
  wrap.appendChild(panel);
  return wrap;
}

function verificationStatusLabel(status) {
  if (status === "pass") return "통과";
  if (status === "warning") return "확인 필요";
  if (status === "review") return "검토 필요";
  if (status === "na") return "해당 없음";
  return "불일치";
}

function renderVerificationPanel(report, auditId = "") {
  const details = document.createElement("details");
  details.className = `verification-panel verification-${report.status || "warning"}`;
  const completed = report.completedDimensions ?? 1;
  const applicable = report.applicableDimensions ?? 1;
  const summary = document.createElement("summary");
  summary.innerHTML = `
    <span class="verification-title">
      <strong>검증 결과</strong>
      <em>${escapeHtml(String(completed))}/${escapeHtml(String(applicable))} 영역 판정</em>
    </span>
    <span class="verification-state">${verificationStatusLabel(report.status)}</span>
  `;
  details.appendChild(summary);

  const body = document.createElement("div");
  body.className = "verification-body";
  if (report.constraints?.length) {
    const chips = document.createElement("div");
    chips.className = "verification-chips";
    report.constraints.forEach((item) => {
      const chip = document.createElement("span");
      chip.innerHTML = `<small>${escapeHtml(item.label)}</small>${escapeHtml(item.value)}`;
      chips.appendChild(chip);
    });
    body.appendChild(chips);
  }

  const dimensions = report.dimensions?.length
    ? report.dimensions
    : [{ id: "retrieval", label: "검색·근거 일치", score: report.score, status: report.status, checks: report.checks || [] }];
  const dimensionGrid = document.createElement("div");
  dimensionGrid.className = "verification-dimensions";
  dimensions.forEach((dimension) => {
    const section = document.createElement("section");
    section.className = `verification-dimension dimension-${dimension.status}`;
    const scoreLabel = dimension.score === null || dimension.score === undefined
      ? verificationStatusLabel(dimension.status)
      : `${dimension.score}점`;
    section.innerHTML = `
      <header>
        <div><strong>${escapeHtml(dimension.label)}</strong><small>${escapeHtml(dimension.description || "")}</small></div>
        <em>${escapeHtml(scoreLabel)}</em>
      </header>
    `;
    if (dimension.checks?.length) {
      const checks = document.createElement("div");
      checks.className = "verification-checks";
      dimension.checks.forEach((check) => {
        const row = document.createElement("div");
        row.className = `verification-check check-${check.status}`;
        row.innerHTML = `
          <span class="check-mark" aria-hidden="true"></span>
          <strong>${escapeHtml(check.label)}</strong>
          <span>${escapeHtml(check.detail)}</span>
        `;
        checks.appendChild(row);
      });
      section.appendChild(checks);
    }
    dimensionGrid.appendChild(section);
  });
  body.appendChild(dimensionGrid);

  if (report.trace?.length) {
    const trace = document.createElement("div");
    trace.className = "verification-trace";
    trace.innerHTML = `<strong>조회 경로</strong>`;
    report.trace.forEach((stage) => {
      const item = document.createElement("span");
      item.className = stage.status === "error" ? "trace-error" : "";
      item.textContent = `${stage.label} ${stage.count}건`;
      trace.appendChild(item);
    });
    body.appendChild(trace);
  }

  const footer = document.createElement("div");
  footer.className = "verification-footer";
  footer.innerHTML = `
    <span>${escapeHtml(report.note || "")}</span>
    <code>${escapeHtml(auditId ? `LOG ${auditId.slice(0, 8)}` : "")}</code>
  `;
  body.appendChild(footer);
  details.appendChild(body);
  return details;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

export {
  hasIssueSummaryList,
  shouldHideSourceList,
  statusToneClass,
  CALLOUT_ICONS,
  calloutToneFor,
  appendInlineMarkdown,
  renderCriteriaChips,
  appendAnswerParagraph,
  renderAssistantContent,
  parseIssueDetailContent,
  renderIssueDetailCard,
  buildIssueReferenceNotes,
  renderFollowUps,
  cleanMarkdown,
  parseIssueSummaryLine,
  renderIssueListSkeleton,
  issueValue,
  issuePriorityLabel,
  renderIssueSummaryList,
  renderSourceList,
  verificationStatusLabel,
  renderVerificationPanel,
  escapeHtml,
  openChromaModal,
  parseDetailSections,
  detailBadgeClass,
  buildDetailValue,
  renderDetailContent,
  closeChromaModal,
  formatMantisDetail,
  displayIssueStatus,
  openSourceDetail,
};
