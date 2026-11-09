// ⌘K 명령 팔레트의 검색 결과를 만드는 순수 함수. DOM과 상태 객체를 모르므로 Node 테스트에서 그대로 쓴다.

const SECTION_LIMIT = 5;
const ISSUE_ID_PATTERN = /^#?\s*(\d{1,7})$/;

function normalizedText(value) {
  return String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, "");
}

// "#1234", "1234", "# 1234"를 이슈 번호로 읽는다. 다른 글자가 섞이면 이슈 번호가 아니다.
function parseIssueQuery(query) {
  const match = String(query || "").trim().match(ISSUE_ID_PATTERN);
  return match ? String(Number(match[1])) : "";
}

function matches(needle, ...fields) {
  return !needle || normalizedText(fields.join(" ")).includes(needle);
}

function section(key, label, items) {
  return items.length ? { key, label, items } : null;
}

// views: [{ view, label, keywords, available }] — 보이지 않는 화면(권한 없음)은 available=false로 넘긴다.
function commandPaletteSections({
  query = "",
  views = [],
  conversations = [],
  activeConversationId = "",
  templates = [],
  issues = [],
  busy = false,
} = {}) {
  const trimmed = String(query || "").trim();
  const needle = normalizedText(trimmed);
  const busyReason = "답변을 작성하는 동안에는 열 수 없어요";
  const sections = [];

  const issueId = parseIssueQuery(trimmed);
  const knownIssues = issues.filter((issue) => issue && issue.id != null && String(issue.id) !== "");
  const issueItems = [];
  if (issueId) {
    const known = knownIssues.find((issue) => String(issue.id) === issueId);
    issueItems.push({
      id: `issue-${issueId}`, kind: "issue", issueId, icon: "list",
      label: known?.title ? `#${issueId} ${known.title}` : `#${issueId} 이슈 원문 보기`,
      meta: known?.status || "Mantis",
    });
  }
  if (needle && !issueId) {
    for (const issue of knownIssues) {
      if (issueItems.length >= SECTION_LIMIT) break;
      if (!matches(needle, issue.id, issue.title)) continue;
      issueItems.push({
        id: `issue-${issue.id}`, kind: "issue", issueId: String(issue.id), icon: "list",
        label: `#${issue.id} ${issue.title || "제목 없음"}`, meta: issue.status || "내 업무",
      });
    }
  }
  sections.push(section("issues", "이슈", issueItems));

  const conversationItems = conversations
    // 아직 질문하지 않은 빈 '새 대화'는 열어도 볼 것이 없으므로 뺀다.
    .filter((item) => item?.id && item.id !== activeConversationId && (item.messages?.length || item.messageCount))
    .filter((item) => matches(needle, item.title))
    .slice(0, SECTION_LIMIT)
    .map((item) => ({
      id: `conversation-${item.id}`, kind: "conversation", conversationId: item.id, icon: "chat",
      label: item.title || "새 대화", meta: busy ? busyReason : "이전 대화", disabled: busy,
    }));
  sections.push(section("conversations", needle ? "대화" : "최근 대화", conversationItems));

  const templateItems = templates
    .filter((template) => template?.id && matches(needle, template.title, template.description, template.category))
    .slice(0, needle ? SECTION_LIMIT : 3)
    .map((template) => ({
      id: `template-${template.id}`, kind: "template", templateId: template.id, icon: "template",
      label: template.title, meta: busy ? busyReason : template.category || "팀 템플릿", disabled: busy,
    }));
  sections.push(section("templates", "템플릿으로 질문", templateItems));

  const actionItems = [
    { id: "action-new-chat", kind: "new-chat", icon: "plus", label: "새 대화 시작", keywords: "새대화 new chat 초기화",
      meta: busy ? busyReason : "", disabled: busy },
    ...views.filter((view) => view.available !== false).map((view) => ({
      id: `view-${view.view}`, kind: "view", view: view.view, icon: view.icon, label: view.label,
      keywords: view.keywords || "", meta: view.current ? "지금 화면" : "",
    })),
    { id: "action-theme", kind: "theme", icon: "eye", label: "다크 모드 전환", keywords: "테마 dark light 밝게 어둡게", meta: "" },
  ].filter((item) => matches(needle, item.label, item.keywords));
  sections.push(section("actions", "이동", actionItems.map(({ keywords: _keywords, ...item }) => item)));

  // 무엇을 입력해도 바로 질문으로 넘길 수 있게 마지막에 둔다. 이슈 번호만 입력했을 때는 원문 보기가 우선이다.
  if (trimmed) {
    sections.push(section("ask", "질문", [{
      id: "ask", kind: "ask", icon: "arrow-up", label: `“${trimmed}” 질문하기`, question: trimmed,
      meta: busy ? busyReason : "입력창에 넣기", disabled: busy,
    }]));
  }
  return sections.filter(Boolean);
}

function flattenCommandItems(sections) {
  return sections.flatMap((group) => group.items);
}

// 비활성 항목은 건너뛰고, 끝에서는 반대쪽 끝으로 돌아간다.
function nextEnabledIndex(items, current, step) {
  if (!items.length) return -1;
  const start = current >= 0 ? current : step > 0 ? -1 : items.length;
  for (let offset = 1; offset <= items.length; offset += 1) {
    const index = (((start + step * offset) % items.length) + items.length) % items.length;
    if (!items[index].disabled) return index;
  }
  return -1;
}

export {
  SECTION_LIMIT,
  commandPaletteSections,
  flattenCommandItems,
  nextEnabledIndex,
  normalizedText,
  parseIssueQuery,
};
