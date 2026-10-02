import { els, apiFetch } from "./runtime.js?v=20260911-6";
import { formatNotionDetail } from "./notion.js?v=20260911-6";

function openChromaModal({ issueId, title, content, meta }) {
  els.chromaModalMeta.textContent = meta || `이슈 상세 · #${issueId}`;
  els.chromaModalTitle.textContent = title || "이슈 상세";
  els.chromaModalContent.replaceChildren(renderDetailContent(content || "내용이 없습니다.", title || ""));
  els.chromaModalContent.scrollTop = 0;
  els.chromaModal.hidden = false;
  document.body.classList.add("modal-open");
  els.chromaModalClose.focus();
}

function parseDetailSections(text) {
  const sections = [];
  let current = { title: "", pairs: [], lines: [] };
  const pushCurrent = () => {
    while (current.lines.length && !current.lines[current.lines.length - 1]) current.lines.pop();
    if (current.title || current.pairs.length || current.lines.length) sections.push(current);
  };
  String(text)
    .split(/\n/)
    .forEach((rawLine) => {
      const line = rawLine.trim();
      const sectionMatch = line.match(/^\[(.+)\]$/);
      if (sectionMatch) {
        pushCurrent();
        current = { title: sectionMatch[1].trim(), pairs: [], lines: [] };
        return;
      }
      const pairMatch = line.match(/^[-•]\s*([^:：]{1,30}?)\s*[:：]\s*(.*)$/);
      if (pairMatch) {
        current.pairs.push([pairMatch[1].trim(), pairMatch[2].trim() || "미상"]);
        return;
      }
      if (line || current.lines.length) current.lines.push(line);
    });
  pushCurrent();
  return sections;
}

function detailBadgeClass(value) {
  if (/종료|해결|완료/.test(value)) return "detail-badge--positive";
  if (/신규|확인|할당|진행/.test(value)) return "detail-badge--info";
  if (/부족|피드백|보류|미상/.test(value)) return "detail-badge--warning";
  return "detail-badge--neutral";
}

function buildDetailValue(key, value) {
  const holder = document.createElement("dd");
  holder.className = "detail-value";
  if (/상태/.test(key)) {
    const badge = document.createElement("span");
    badge.className = `detail-badge ${detailBadgeClass(value)}`;
    badge.textContent = value;
    holder.appendChild(badge);
  } else if (/^https?:\/\//.test(value)) {
    const link = document.createElement("a");
    link.className = "detail-link";
    link.href = value;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = value;
    holder.appendChild(link);
  } else {
    holder.textContent = value;
  }
  return holder;
}

function renderDetailContent(text, headerTitle) {
  const fragment = document.createDocumentFragment();
  const sections = parseDetailSections(text);
  const hasStructure = sections.some((section) => section.title || section.pairs.length);

  if (!hasStructure) {
    const plain = document.createElement("pre");
    plain.className = "detail-plain";
    plain.textContent = String(text);
    fragment.appendChild(plain);
    return fragment;
  }

  const normalizedHeader = String(headerTitle).replace(/\s+/g, " ").trim();
  sections.forEach((section) => {
    const proseText = section.lines.join("\n").trim();
    if (
      section.title === "제목" &&
      !section.pairs.length &&
      proseText.replace(/\s+/g, " ") === normalizedHeader
    ) {
      return;
    }

    const block = document.createElement("section");
    block.className = "detail-section";

    if (section.title) {
      const heading = document.createElement("h4");
      heading.className = "detail-section-title";
      heading.textContent = section.title;
      block.appendChild(heading);
    }

    if (section.pairs.length) {
      const grid = document.createElement("dl");
      grid.className = "detail-grid";
      section.pairs.forEach(([key, value]) => {
        const item = document.createElement("div");
        item.className = "detail-item";
        const label = document.createElement("dt");
        label.className = "detail-key";
        label.textContent = key;
        item.append(label, buildDetailValue(key, value));
        grid.appendChild(item);
      });
      block.appendChild(grid);
    }

    if (proseText) {
      const prose = document.createElement("div");
      prose.className = "detail-prose";
      prose.textContent = proseText;
      block.appendChild(prose);
    }

    fragment.appendChild(block);
  });
  return fragment;
}

function closeChromaModal() {
  els.chromaModal.hidden = true;
  document.body.classList.remove("modal-open");
}

function formatMantisDetail(source) {
  const detail = source.detail || {};
  const lines = [
    `[Mantis 이슈 정보]`,
    `- 이슈 ID: #${source.issue_id || ""}`,
    `- 프로젝트(고객사): ${source.project || "미상"}`,
    `- 현재 상태: ${displayIssueStatus(source.status)}`,
    `- 담당자: ${source.handler || "담당자 미지정"}`,
    `- 등록일: ${source.created_at || "미상"}`,
    `- 화면번호: ${detail.screen_number || "미상"}`,
    `- 작업주체: ${detail.work_subject || "미상"}`,
  ];
  const effortEntries = Object.entries(detail.effort_fields || {}).filter(([, value]) => value);
  if (effortEntries.length) {
    lines.push(``, `[공수]`);
    effortEntries.forEach(([field, value]) => lines.push(`- ${field}: ${value}`));
  }
  const dateEntries = Object.entries(detail.date_fields || {}).filter(([, value]) => value);
  if (dateEntries.length) {
    lines.push(``, `[일정/전달일]`);
    dateEntries.forEach(([field, value]) => lines.push(`- ${field}: ${value}`));
  }
  lines.push(
    ``,
    `[제목]`,
    source.title || "제목 없음",
    ``,
    `[본문 미리보기]`,
    source.preview || detail.description || "내용 없음",
  );
  return lines.join("\n");
}

function displayIssueStatus(status) {
  const statusMap = {
    new: "신규 이슈",
    feedback: "정보 부족",
    acknowledged: "이슈 검토",
    confirmed: "확인된 이슈",
    assigned: "할당된 이슈",
    developed: "개발완료",
    resolved: "해결된 이슈",
    closed: "종료된 이슈",
  };
  const normalized = String(status || "").trim();
  return statusMap[normalized.toLowerCase()] || normalized || "미상";
}

async function openSourceDetail(source) {
  if (!source) return;

  if (source.type === "notion") {
    openChromaModal({
      issueId: source.page_id || "",
      title: source.title,
      content: formatNotionDetail(source),
      meta: "Notion DB 항목",
    });
    return;
  }

  if (source.type !== "chroma") {
    openChromaModal({
      issueId: source.issue_id,
      title: source.title,
      content: formatMantisDetail(source),
      meta: `Mantis 이슈 · #${source.issue_id}`,
    });
    return;
  }

  openChromaModal({
    issueId: source.issue_id,
    title: "불러오는 중입니다",
    content: "과거 이슈 원문을 불러오는 중입니다...",
    meta: `과거 이슈 원문 · #${source.issue_id}`,
  });

  try {
    const response = await apiFetch(`/api/chroma/documents/${encodeURIComponent(source.issue_id)}`);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "문서를 불러오지 못했습니다.");
    const titleMatch = data.content.match(/\[제목\]\s*([\s\S]*?)(\n\n|\[고객사 본문 요청 내용\])/);
    openChromaModal({
      issueId: source.issue_id,
      title: titleMatch ? titleMatch[1].trim() : "이슈 상세",
      content: data.content,
      meta: `과거 이슈 원문 · #${source.issue_id}`,
    });
  } catch (error) {
    openChromaModal({
      issueId: source.issue_id,
      title: "문서 조회 오류",
      content: `오류: ${error.message}`,
      meta: `과거 이슈 원문 · #${source.issue_id}`,
    });
  }
}

export {
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
