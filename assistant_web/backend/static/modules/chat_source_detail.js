import { customerName, formatUiDateTime } from "./ui_format.js?v=20260911-6";
import { openDialogFocus, closeDialogFocus } from "./dialog_focus.js?v=20260911-6";
import { state, els, apiFetch } from "./runtime.js?v=20260911-6";
import { recordIssueView } from "./recent_items.js?v=20261105-1";
import { formatNotionDetail } from "./notion.js?v=20260911-6";

let detailRequestId = 0;
const PRIMARY_ISSUE_FIELDS = new Set(["이슈 ID", "프로젝트(고객사)", "고객사", "현재 상태", "담당자", "화면번호", "우선순위", "등록일", "최종수정일"]);

function renderSourceActions({ issueId, title, content, url = "", snapshot = false, loading = false, failed = false }) {
  const footer = document.getElementById("sourceModalActions");
  if (!footer) return;
  footer.replaceChildren();
  footer.hidden = loading;
  if (loading) return;
  if (failed) {
    const retry = document.createElement("button");
    retry.type = "button"; retry.className = "secondary-button"; retry.textContent = "다시 조회";
    retry.addEventListener("click", () => openSourceDetail({ type: "chroma", issue_id: issueId }));
    footer.appendChild(retry);
    return;
  }
  if (/^https?:\/\//i.test(url)) {
    const link = document.createElement("a");
    link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "원본 서비스에서 열기 ↗";
    footer.appendChild(link);
  }
  if (snapshot && /^\d+$/.test(String(issueId))) {
    const full = document.createElement("button");
    full.type = "button"; full.className = "secondary-button"; full.textContent = "전체 원문 조회";
    full.addEventListener("click", () => openSourceDetail({ type: "chroma", issue_id: issueId }));
    footer.appendChild(full);
  }
  const copy = document.createElement("button");
  copy.type = "button"; copy.className = "secondary-button"; copy.textContent = "원문 복사";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(`${title}\n\n${content}`); copy.textContent = "복사됨"; }
    catch { copy.textContent = "복사 실패 · 다시 시도"; }
  });
  footer.appendChild(copy);
  if (/^\d+$/.test(String(issueId))) {
    const ask = document.createElement("button");
    ask.type = "button"; ask.className = "run-evaluation-button"; ask.textContent = "이 이슈로 질문";
    ask.addEventListener("click", () => {
      closeChromaModal();
      document.getElementById("viewChatBtn").click();
      els.input.value = `#${issueId} 이슈의 현재 상태와 다음 할 일을 요약해줘`;
      els.input.dispatchEvent(new Event("input", {bubbles: true}));
      els.input.focus();
    });
    footer.appendChild(ask);
  }
}

function openChromaModal({ issueId, title, content, meta, url = "", snapshot = false, loading = false, failed = false }) {
  els.chromaModalMeta.textContent = meta || `이슈 상세 · #${issueId}`;
  els.chromaModalTitle.textContent = title || "이슈 상세";
  els.chromaModalContent.replaceChildren(renderDetailContent(content || "내용이 없습니다.", title || ""));
  els.chromaModalContent.scrollTop = 0;
  renderSourceActions({ issueId, title, content, url, snapshot, loading, failed });
  els.chromaModal.hidden = false;
  openDialogFocus(els.chromaModal, { initialFocus: els.chromaModalClose, onClose: closeChromaModal, fallback: () => els.input });
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

function formatDetailDateTime(value) {
  const text = String(value || "").trim();
  // 날짜만 있는 일정에는 시간을 덧붙이지 않는다. 시간대가 없는 Mantis 시각은 한국 시간이다.
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text)) return value;
  const zoned = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}+09:00`;
  const date = new Date(zoned.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date).map(({ type, value }) => [type, value]));
  const hour = Number(parts.hour);
  return `${parts.year}-${parts.month}-${parts.day} ${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}시 ${Number(parts.minute)}분`;
}

function buildDetailValue(key, value, fields = {}) {
  if (/^(?:프로젝트(?:\(고객사\))?|고객사)$/.test(key)) value = customerName(value, "미상");
  const labelKey = { "현재 상태": "상태 표시명", "우선순위": "우선순위 표시명", "이슈 유형": "이슈 유형 표시명" }[key];
  const label = labelKey && fields[labelKey];
  if (label && !["미상", "-"].includes(label)) value = label;
  else if (key === "현재 상태" || key === "상태 표시명") value = displayIssueStatus(value);
  else if (key === "우선순위" || key === "우선순위 표시명") {
    const labels = { none: "없음", low: "낮음", normal: "보통", high: "높음", urgent: "긴급", immediate: "즉시" };
    const code = String(value).trim().toLowerCase();
    if (Object.hasOwn(labels, code)) value = labels[code];
  } else if ((key === "이슈 유형" || key === "이슈 유형 표시명") && String(value).trim().toLowerCase() === "minor") value = "개선";
  if (/(?:일|일시)$/.test(key)) value = formatDetailDateTime(value);
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
  const mantis = sections.some((section) => section.title === "Mantis 이슈 정보");
  const primaryTitles = new Set(["Mantis 이슈 정보", "고객사 본문 요청 내용", "조회된 본문", "본문 미리보기"]);
  const readingOrder = mantis ? [...sections.filter((section) => primaryTitles.has(section.title)), ...sections.filter((section) => !primaryTitles.has(section.title))] : sections;
  readingOrder.forEach((section) => {
    const proseText = section.lines.join("\n").trim();
    if (
      section.title === "제목" &&
      !section.pairs.length &&
      proseText.replace(/\s+/g, " ") === normalizedHeader
    ) {
      return;
    }

    const folded = mantis && ["일정/전달일", "공수"].includes(section.title);
    const block = document.createElement(folded ? "details" : "section");
    block.className = folded ? "detail-section detail-disclosure" : "detail-section";

    if (section.title) {
      const heading = document.createElement(folded ? "summary" : "h4");
      heading.className = "detail-section-title";
      heading.textContent = section.title;
      block.appendChild(heading);
    }

    if (section.pairs.length) {
      const fields = Object.fromEntries(section.pairs);
      const primary = document.createElement("dl");
      primary.className = "detail-grid";
      const extra = document.createElement("dl");
      extra.className = "detail-grid";
      const split = section.title === "Mantis 이슈 정보";
      let extraCount = 0;
      section.pairs.forEach(([key, value]) => {
        const item = document.createElement("div");
        item.className = "detail-item";
        const label = document.createElement("dt");
        label.className = "detail-key";
        label.textContent = key;
        item.append(label, buildDetailValue(key, value, fields));
        if (split && !PRIMARY_ISSUE_FIELDS.has(key)) {
          extra.appendChild(item);
          extraCount += 1;
        } else primary.appendChild(item);
      });
      if (primary.children.length) block.appendChild(primary);
      if (extraCount) {
        const disclosure = document.createElement("details");
        disclosure.className = "detail-extra-fields";
        const summary = document.createElement("summary");
        summary.textContent = `추가 정보 ${extraCount}개`;
        disclosure.append(summary, extra);
        block.appendChild(disclosure);
      }
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
  detailRequestId += 1;
  els.chromaModal.hidden = true;
  closeDialogFocus(els.chromaModal);
}

function formatMantisDetail(source) {
  const detail = source.detail || {};
  const lines = [
    `[Mantis 이슈 정보]`,
    `- 이슈 ID: #${source.issue_id || ""}`,
    `- 프로젝트(고객사): ${customerName(source, "미상")}`,
    `- 현재 상태: ${source.status_label || source.detail?.status_label || displayIssueStatus(source.status)}`,
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
    detail.description ? `[조회된 본문]` : `[본문 미리보기]`,
    detail.description || source.preview || "내용 없음",
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
  const requestId = ++detailRequestId;

  if (source.type === "notion") {
    openChromaModal({
      issueId: source.page_id || "",
      title: source.title,
      content: formatNotionDetail(source),
      meta: "Notion DB 항목",
      url: source.url || "",
    });
    return;
  }

  if (source.type !== "chroma") {
    recordIssueView(state.currentUser?.uid, { id: source.issue_id, title: source.title });
    openChromaModal({
      issueId: source.issue_id,
      title: source.title,
      content: formatMantisDetail(source),
      meta: `Mantis 이슈 · #${source.issue_id} · 조회된 원문 범위`,
      snapshot: true,
      url: source.url || "",
    });
    return;
  }

  openChromaModal({
    issueId: source.issue_id,
    title: "불러오는 중입니다",
    content: "이슈 원문을 불러오는 중입니다...",
    loading: true,
    meta: `이슈 원문 · #${source.issue_id}`,
  });

  try {
    const response = await apiFetch(`/api/chroma/documents/${encodeURIComponent(source.issue_id)}`);
    const data = await response.json().catch(() => ({}));
    if (requestId !== detailRequestId || els.chromaModal.hidden) return;
    if (!response.ok) throw new Error(data.detail || "문서를 불러오지 못했습니다.");
    const titleMatch = data.content.match(/\[제목\]\s*([\s\S]*?)(\n\n|\[고객사 본문 요청 내용\])/);
    recordIssueView(state.currentUser?.uid, { id: source.issue_id, title: titleMatch ? titleMatch[1].trim() : source.title });
    openChromaModal({
      issueId: source.issue_id,
      title: titleMatch ? titleMatch[1].trim() : "이슈 상세",
      content: data.content,
      meta: data.source === "mantis"
        ? `Mantis 현재 원문 · #${source.issue_id} · ${formatUiDateTime(data.fetchedAt)} 조회 · 과거 저장본 없음`
        : `과거 이슈 원문 · #${source.issue_id} · 검색 저장본 기준`,
    });
  } catch (error) {
    if (requestId !== detailRequestId || els.chromaModal.hidden) return;
    openChromaModal({
      issueId: source.issue_id,
      title: "문서 조회 오류",
      failed: true,
      content: `오류: ${error.message}`,
      meta: `이슈 원문 · #${source.issue_id}`,
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
