// 첫 화면의 '최근 본 이슈'·'이어서 볼 대화'. 최근 본 이슈는 사용자별로 이 브라우저에만 남긴다.
const RECENT_ISSUES_KEY = "team-ai-assistant-recent-issues-v1";
const RECENT_ISSUE_LIMIT = 10;
const HOME_ITEM_LIMIT = 3;

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function recentIssuesKey(userId) {
  return `${RECENT_ISSUES_KEY}:${userId || "guest"}`;
}

// 같은 이슈를 다시 보면 맨 앞으로 옮기고, 새 제목이 없으면 이전 제목을 유지한다.
function rememberIssue(list, { id, title = "" } = {}, now = new Date()) {
  const key = String(id ?? "").trim();
  if (!/^\d{1,9}$/.test(key)) return Array.isArray(list) ? list : [];
  const previous = (Array.isArray(list) ? list : []).filter((item) => item && String(item.id) !== key);
  const old = (Array.isArray(list) ? list : []).find((item) => item && String(item.id) === key);
  const cleanTitle = String(title || "").trim().slice(0, 200) || old?.title || "";
  return [{ id: key, title: cleanTitle, viewedAt: now.toISOString() }, ...previous].slice(0, RECENT_ISSUE_LIMIT);
}

function readRecentIssues(userId, storage = globalThis.localStorage) {
  try {
    const parsed = JSON.parse(storage?.getItem(recentIssuesKey(userId)) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => item && /^\d{1,9}$/.test(String(item.id))) : [];
  } catch {
    return [];
  }
}

function recordIssueView(userId, issue, storage = globalThis.localStorage) {
  try {
    storage?.setItem(recentIssuesKey(userId), JSON.stringify(rememberIssue(readRecentIssues(userId, storage), issue)));
  } catch {
    // 저장 공간이 막힌 브라우저에서도 이슈 상세는 그대로 열려야 한다.
  }
}

function relativeTimeLabel(timestamp, now = new Date()) {
  const time = new Date(timestamp).getTime();
  if (!Number.isFinite(time)) return "";
  const minutes = Math.floor((now.getTime() - time) / 60000);
  if (minutes < 1) return "방금";
  if (minutes < 60) return `${minutes}분 전`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}시간 전`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "어제" : `${days}일 전`;
}

// 보관함의 영구 삭제까지 남은 날(올림). 방금 지운 대화는 보관 일수 그대로, 시각이 지나면 0이다.
function daysUntil(timestamp, now = new Date()) {
  const time = new Date(timestamp).getTime();
  if (!Number.isFinite(time)) return null;
  return Math.max(0, Math.ceil((time - now.getTime()) / 86400000));
}

function trashRemainingLabel(purgeAt, now = new Date()) {
  const days = daysUntil(purgeAt, now);
  if (days === null) return { label: "", urgent: false };
  return { label: days === 0 ? "곧 영구 삭제" : `${days}일 남음`, urgent: days <= 3 };
}

// 지금 대화와 질문이 없는 빈 대화는 빼고 최근에 바뀐 순서로 고른다.
function resumableConversations(conversations, activeId, limit = HOME_ITEM_LIMIT) {
  return (Array.isArray(conversations) ? conversations : [])
    .filter((item) => item?.id && item.id !== activeId && (item.messages?.length || item.messageCount))
    .sort((a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || "")))
    .slice(0, limit);
}

function homeRecentsHtml({ issues = [], conversations = [], activeId = "", now = new Date() } = {}) {
  const issueRows = issues.slice(0, HOME_ITEM_LIMIT).map((issue) => `
    <li><button type="button" class="home-recent-row" data-home-issue="${escapeText(issue.id)}">
      <span class="home-recent-id">#${escapeText(issue.id)}</span>
      <span class="home-recent-title">${escapeText(issue.title || "이슈 원문 보기")}</span>
      <span class="home-recent-time">${escapeText(relativeTimeLabel(issue.viewedAt, now))}</span>
    </button></li>`).join("");
  const conversationRows = resumableConversations(conversations, activeId).map((conversation) => `
    <li><button type="button" class="home-recent-row" data-home-conversation="${escapeText(conversation.id)}">
      <span class="home-recent-title">${escapeText(conversation.title || "새 대화")}</span>
      <span class="home-recent-time">${escapeText(relativeTimeLabel(conversation.updatedAt, now))}</span>
    </button></li>`).join("");
  if (!issueRows && !conversationRows) return "";
  const card = (title, rows) => rows
    ? `<section class="home-recent-card" aria-label="${title}"><h4>${title}</h4><ul>${rows}</ul></section>` : "";
  return `<div class="home-recents">${card("최근 본 이슈", issueRows)}${card("이어서 볼 대화", conversationRows)}</div>`;
}

export {
  RECENT_ISSUE_LIMIT,
  HOME_ITEM_LIMIT,
  rememberIssue,
  readRecentIssues,
  recordIssueView,
  relativeTimeLabel,
  daysUntil,
  trashRemainingLabel,
  resumableConversations,
  homeRecentsHtml,
};
