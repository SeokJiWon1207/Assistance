// 내 업무 › 업무 탭의 목표일 기준 정렬·거르기, 최근 활동 피드.
import { icon } from "./icons.js?v=20260911-6";
import { escapeHtml } from "./ui_format.js?v=20260911-6";

function localDay(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])) : null;
}

function startOfDay(today) {
  return new Date(today.getFullYear(), today.getMonth(), today.getDate());
}

// 목표일 묶음: 기한 지남 · 7일 안 마감 · 목표일 없음 · 그 뒤.
export function dueBucket(issue, today = new Date()) {
  if (issue.overdue) return "overdue";
  const due = localDay(issue.dueDate);
  if (!due) return "none";
  const days = Math.round((due - startOfDay(today)) / 86400000);
  return days < 0 ? "overdue" : days <= 6 ? "week" : "later";
}

export const DUE_FILTERS = [["all", "전체"], ["overdue", "기한 지남"], ["week", "7일 안 마감"], ["none", "목표일 없음"]];

export function filterByDue(issues, filter, today = new Date()) {
  if (!filter || filter === "all") return issues;
  return issues.filter((issue) => dueBucket(issue, today) === filter);
}

export function dueFilterCounts(issues, today = new Date()) {
  const counts = { all: issues.length, overdue: 0, week: 0, none: 0, later: 0 };
  for (const issue of issues) counts[dueBucket(issue, today)] += 1;
  return counts;
}

// 목표일 임박순: 목표일이 있는 이슈를 날짜 오름차순(지난 것이 맨 위)으로, 목표일이 없으면 오래된 순으로 뒤에 둔다.
export function sortByDue(issues) {
  return [...issues].sort((a, b) => {
    const left = String(a.dueDate || "");
    const right = String(b.dueDate || "");
    if (left && right) return left.localeCompare(right) || Number(b.ageDays || 0) - Number(a.ageDays || 0);
    if (left || right) return left ? -1 : 1;
    return Number(b.ageDays || 0) - Number(a.ageDays || 0);
  });
}

export function dueFilterChips(issues, active, today = new Date()) {
  const counts = dueFilterCounts(issues, today);
  return `<div class="workspace-due-filters" role="group" aria-label="목표일로 거르기">${DUE_FILTERS.map(([key, label]) => `
    <button type="button" class="workspace-due-chip" data-workspace-due-filter="${key}" aria-pressed="${(active || "all") === key}">${label} <b>${counts[key]}</b></button>`).join("")}</div>`;
}

function relativeTime(value, now = new Date()) {
  const time = new Date(value);
  if (Number.isNaN(time.getTime())) {
    const day = localDay(value);
    return day ? `${day.getMonth() + 1}월 ${day.getDate()}일` : "";
  }
  const minutes = Math.round((now - time) / 60000);
  if (minutes < 1) return "방금";
  if (minutes < 60) return `${minutes}분 전`;
  if (minutes < 60 * 24 && time.getDate() === now.getDate()) return `${Math.round(minutes / 60)}시간 전`;
  const days = Math.round((startOfDay(now) - startOfDay(time)) / 86400000);
  if (days === 1) return "어제";
  if (days < 7) return `${days}일 전`;
  return `${time.getMonth() + 1}월 ${time.getDate()}일`;
}

function activityIssueId(item) {
  if (item.issueId) return String(item.issueId);
  return String(item.title || "").match(/#(\d+)/)?.[1] || "";
}

// 노트 알림의 설명은 "제목 · 작성자 이름" 형식이다. 작성자는 따로 떼어 보여 준다.
function splitNoteAuthor(description) {
  const text = String(description || "");
  const index = text.lastIndexOf(" · 작성자 ");
  return index < 0 ? { title: text, author: "" } : { title: text.slice(0, index), author: text.slice(index + 7).trim() };
}

export function activityEntry(item, now = new Date()) {
  const id = activityIssueId(item);
  const { title, author } = item.type === "note" ? splitNoteAuthor(item.description) : { title: item.description || "", author: "" };
  const kinds = {
    note: { icon: "note", tone: "is-green", text: author ? `${author}님이 #${id}에 노트를 남겼어요` : `#${id}에 새 노트가 달렸어요` },
    assigned: { icon: "user-plus", tone: "is-blue", text: `#${id} 이슈가 나에게 배정됐어요` },
    overdue: { icon: "clock", tone: "is-red", text: id ? `#${id} 목표일이 지났어요` : String(item.title || "") },
    aging: { icon: "clock", tone: "is-orange", text: `#${id} 30일 넘게 미종료예요` },
  };
  const kind = kinds[item.type] || { icon: "info", tone: "is-gray", text: String(item.title || "") };
  return { ...kind, id, title, when: relativeTime(item.occurredAt || item.date, now), read: Boolean(item.read) };
}

// 최근 활동: 사람이 한 일(배정·노트)만 다섯 개까지. 기한·장기 경고는 위 '알림' 칸이 맡는다.
export function activityFeedHtml(notifications = [], { scopeMessage = "", emptyText = "아직 확인된 활동이 없어요.", now = new Date() } = {}) {
  const items = notifications.filter((item) => ["assigned", "note"].includes(item.type)).slice(0, 5).map((item) => activityEntry(item, now));
  const rows = items.map((entry) => {
    const body = `
      <span class="workspace-activity-icon ${entry.tone}" aria-hidden="true">${icon(entry.icon, 16)}</span>
      <span class="workspace-activity-text">
        <span>${escapeHtml(entry.text)}</span>
        ${entry.title ? `<small class="workspace-activity-title">${escapeHtml(entry.title)}</small>` : ""}
        <small>${escapeHtml(entry.when)}${entry.read ? "" : ' · <b class="workspace-activity-new">새 활동</b>'}</small>
      </span>`;
    return `<li>${entry.id ? `<button type="button" class="workspace-activity-item" data-workspace-issue="${escapeHtml(entry.id)}" aria-label="${escapeHtml(entry.text)} · 이슈 상세 보기">${body}</button>` : `<div class="workspace-activity-item">${body}</div>`}</li>`;
  }).join("");
  return `
    <section class="workspace-side-card workspace-activity">
      <header><h3>최근 활동</h3><button type="button" class="workspace-side-link" data-workspace-go="notifications">모두 보기</button></header>
      ${rows ? `<ol class="workspace-activity-list">${rows}</ol>` : `<p class="workspace-side-empty">${escapeHtml(emptyText)}</p>`}
      ${scopeMessage ? `<p class="workspace-side-empty">${escapeHtml(scopeMessage)}</p>` : ""}
    </section>`;
}


export function kanbanGroups(issues, statusLabel = value => value) {
  const lanes = [
    { key: 'review', title: '접수·검토', statuses: ['new','feedback','acknowledged','confirmed','신규 이슈','정보 부족','이슈 검토','확인된 이슈'] },
    { key: 'progress', title: '진행', statuses: ['assigned','할당된 이슈'] },
    { key: 'validation', title: '검증 대기', statuses: ['developed','개발완료'] },
    { key: 'done', title: '해결·종료', statuses: ['resolved','closed','해결된 이슈','종료된 이슈'] },
    { key: 'unknown', title: '상태 미상', statuses: [] },
  ].map(lane => ({...lane, items:[]}));
  const seen = new Set();
  for (const issue of issues) {
    if (seen.has(String(issue.id))) continue;
    seen.add(String(issue.id));
    const label = statusLabel(issue.status);
    const lane = lanes.find(lane => lane.statuses.includes(label)) || lanes[4];
    lane.items.push(issue);
  }
  return lanes.filter(lane => lane.key !== 'unknown' || lane.items.length);
}

export function kanbanBoard(issues, {renderIssue, statusLabel, complete, closedTotal = 0, showClosed = true}) {
  const lanes = kanbanGroups(issues, statusLabel);
  return `<div class="workspace-kanban" aria-label="상태별 내 업무 보드">${lanes.map(lane => `
    <section class="workspace-kanban-lane" data-kanban-lane="${lane.key}" aria-label="${lane.title}">
      <header><h4><i class="analytics-stage-dot stage-${lane.key}"></i>${lane.title}</h4><span>${lane.items.length}${complete?'':'+'}</span></header>
      ${lane.key==='done'?`<p class="kanban-lane-note">${showClosed ? '최근 30일 수정 · 최대 30건' : '목표일 필터에서는 제외'}</p>`:lane.key==='validation'?'<p class="kanban-lane-note">개발완료 · 현재 미종료</p>':'<p class="kanban-lane-note">현재 담당 이슈</p>'}
      <div class="kanban-lane-items">${lane.items.length?lane.items.map(renderIssue).join(''):`<div class="kanban-empty">${complete?'해당 업무가 없어요.':'확인된 항목이 없어요.'}</div>`}</div>
      ${lane.key==='done'&&showClosed&&closedTotal>lane.items.length?`<p class="kanban-lane-note">${closedTotal}건 중 최근 ${lane.items.length}건 표시</p>`:''}
    </section>`).join('')}</div>`;
}
