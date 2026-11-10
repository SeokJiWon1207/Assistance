// 팀 대시보드 운영 현황 상단 카드(참고: Monarch 대시보드의 2열 카드 구성).
// 미종료 구성(예산 진행 막대형) · 신규 등록 이번 vs 직전 기간(비교 영역 차트) · 최근 등록 미종료(거래 목록형).
// 지표 버튼은 기존 data-dashboard-metric 필터를 그대로 쓰고, 이슈 행은 data-dashboard-issue로 상세를 연다.
import { chartHost } from "./bklit_charts.js?v=20261026-1";
import { icon } from "./icons.js?v=20260911-6";
import { customerName, customerMark } from "./ui_format.js?v=20260911-6";

const RECENT_LIMIT = 5;

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function number(value) {
  return Number(value || 0).toLocaleString("ko-KR");
}

function countText(value, complete) {
  return `${number(value)}건${complete ? "" : " 이상"}`;
}

// 미종료 대비 비중. 일부만 확인했으면 분모가 확정되지 않아 비율을 만들지 않는다.
function shareOf(value, total, complete) {
  const whole = Number(total || 0);
  if (!complete || whole <= 0) return null;
  return Math.min(100, Math.round((Number(value || 0) / whole) * 100));
}

function progressRow({ key, label, helper, value, total, tone, complete, metric }) {
  const pressed = metric === key;
  const share = shareOf(value, total, complete);
  const width = share === null ? 0 : Math.max(share, Number(value) > 0 ? 2 : 0);
  return `
    <button class="ops-progress ${tone}${pressed ? " is-active" : ""}" type="button" data-dashboard-metric="${key}" aria-pressed="${pressed}">
      <span class="ops-progress-head"><strong>${escapeText(label)}</strong><span>${escapeText(helper)}</span></span>
      <span class="ops-progress-track" aria-hidden="true"><i style="width:${width}%"></i></span>
      <span class="ops-progress-foot"><b>${escapeText(countText(value, complete))}</b><small>${share === null ? "비율 미확정" : `미종료의 ${share}%`}</small></span>
    </button>`;
}

function opsSummaryCard({ summary = {}, complete = true, metric = "" } = {}) {
  const pressed = !metric;
  const rows = [
    { key: "overdue", label: "일정 지연", helper: "완료 예정일 경과", value: summary.overdue, tone: "is-danger" },
    { key: "aging", label: "장기 미종료", helper: "30일 이상 미종료", value: summary.aging, tone: "is-warning" },
    { key: "highRisk", label: "고위험", helper: `우선순위 높음 ${number(summary.highPriority)}건`, value: summary.highRisk, tone: "is-danger" },
  ];
  return `
    <section class="workspace-section ops-card ops-summary" aria-label="핵심 운영 지표">
      <header class="ops-card-head">
        <div><h3>미종료 구성</h3><p>지표를 누르면 우선 확인 이슈가 걸러져요.</p></div>
        <button class="ops-total${pressed ? " is-active" : ""}" type="button" data-dashboard-metric="open" aria-pressed="${pressed}">
          <span>${complete ? "미종료 이슈" : "확인된 미종료"}</span><strong>${escapeText(countText(summary.open, complete))}</strong>
          <small>${complete ? "전체 보기" : "전체 미확정"} · 이월 ${number(summary.carriedOpen)}건</small>
        </button>
      </header>
      <div class="ops-progress-list">${rows.map((row) => progressRow({ ...row, total: summary.open, complete, metric })).join("")}</div>
    </section>`;
}

// 신규 등록 증감 배지(지표 타일·신규 등록 카드 공용). 신규가 는 것은 좋고 나쁨이 아니라 들어온 양이라
// 내 업무처럼 방향만 색으로 나눈다(.workspace-kpi-delta.is-up/is-down).
function intakeBadge(rate) {
  const value = Number(rate);
  if (rate == null || !Number.isFinite(value)) return "";
  const [tone, arrow, label, text] = value > 0
    ? ["is-up", "arrow-up", `${value}%`, `직전 기간보다 ${value}% 많음`]
    : value < 0
      ? ["is-down", "arrow-down", `${-value}%`, `직전 기간보다 ${-value}% 적음`]
      : ["is-flat", "", "변동 없음", "직전 기간과 같음"];
  return `<em class="workspace-kpi-delta ${tone}" title="${escapeText(text)}">${arrow ? icon(arrow, 12) : ""}${escapeText(label)}<span class="sr-only">, ${escapeText(text)}</span></em>`;
}

function intakeDelta(summary, complete) {
  if (!complete) return '<em class="dashboard-delta">확인 범위 · 비교 미확정</em>';
  return intakeBadge(summary.changeRate) || '<em class="dashboard-delta">직전 기간 비교 없음</em>';
}

// closedNow는 기간 중 처리한 전체 건수가 아니라 이번 기간 신규 이슈 중 지금 종료된 건수다.
const INTAKE_MEDIA = [["", "전체"], ["HTS", "HTS"], ["MTS", "MTS"]];

// 신규 등록 카드의 매체 탭. 매체를 고르면 그 매체 이슈만의 이번·직전 기간 누적을 그린다.
function intakeMediaTabs(media, byMedia) {
  if (!byMedia) return "";
  return `<span class="ops-intake-tabs" role="group" aria-label="매체">${INTAKE_MEDIA.map(([key, label]) => `
    <button type="button" data-intake-media="${key}" aria-pressed="${media === key}">${label}</button>`).join("")}</span>`;
}

function mediaIntakeCard({ media, byMedia, complete, periodDays }) {
  const entry = byMedia?.[media] || {};
  const total = Number(entry.total || 0);
  const previous = Number(entry.previousTotal || 0);
  const compare = complete && (entry.previousPoints || []).length > 0;
  const chart = compare
    ? chartHost({ kind: "compare-area", title: `${media} 신규 등록 이번 기간과 직전 기간 비교`, complete, points: entry.points || [], previousPoints: entry.previousPoints || [],
      lineStyle: "snow", series: [{ key: "total", label: "이번 기간", color: "var(--chart-1)" }, { key: "previousTotal", label: "직전 기간", color: "var(--chart-2)" }] },
    `<p class="bklit-empty">${media} 이번 기간 신규 ${escapeText(countText(total, complete))} · 직전 기간 ${number(previous)}건</p>`)
    : `<p class="bklit-note">${complete ? "직전 기간 등록 추이가 없어 비교하지 않아요." : "일부 조회 결과라 직전 기간과 비교하지 않아요."}</p>`;
  return `
    <section class="workspace-section ops-card ops-intake" aria-label="${media} 이번 기간 신규 이슈 ${total}건${complete ? "" : " 이상"}">
      <header class="ops-card-head">
        <div><h3>신규 등록 <strong>${escapeText(countText(total, complete))}</strong></h3><p>${media}만 · ${compare ? `이번 ${periodDays}일 vs 직전 ${periodDays}일 · 누적` : "이번 기간 누적"} · 등록일 기준</p></div>
        ${intakeMediaTabs(media, byMedia)}
      </header>
      ${compare ? `<span class="ops-legend"><span><i class="is-current"></i>이번 기간 <b>${number(total)}</b></span><span><i class="is-previous"></i>직전 기간 <b>${number(previous)}</b></span></span>` : ""}
      ${chart}
      <small class="dashboard-new-change">${media} 표시 이슈만 세요 · 종료·미종료 구분은 전체 탭에서 볼 수 있어요</small>
    </section>`;
}

function intakeCompareCard({ summary = {}, complete = true, points = [], previousPoints = [], periodDays = 30, media = "", byMedia = null } = {}) {
  if (media && byMedia?.[media]) return mediaIntakeCard({ media, byMedia, complete, periodDays });
  const total = Number(summary.newIssues || 0);
  const closed = Math.min(Number(summary.closedNow || 0), total);
  const unknown = Number(summary.unknownStatus || 0);
  const open = Number(summary.newOpen ?? Math.max(0, total - closed - unknown));
  // 일부만 조회했으면 직전 기간과 비교할 수 없다. 추이는 아래 '신규 이슈 등록 추이' 차트가 이미 보여 주므로 중복해 그리지 않는다.
  const compare = complete && previousPoints.length > 0;
  const chart = compare
    ? chartHost({ kind: "compare-area", title: "신규 등록 이번 기간과 직전 기간 비교", complete, points, previousPoints,
      lineStyle: "snow", series: [{ key: "total", label: "이번 기간", color: "var(--chart-1)" }, { key: "previousTotal", label: "직전 기간", color: "var(--chart-2)" }] },
    `<p class="bklit-empty">이번 기간 신규 ${escapeText(countText(total, complete))} · 직전 기간 ${number(summary.previousNewIssues)}건</p>`)
    : `<p class="bklit-note">${complete ? "직전 기간 등록 추이가 없어 비교하지 않아요." : "일부 조회 결과라 직전 기간과 비교하지 않아요."} 등록 추이는 아래 '신규 이슈 등록 추이'에서 볼 수 있어요.</p>`;
  const legend = compare
    ? `<span class="ops-legend"><span><i class="is-current"></i>이번 기간 <b>${number(total)}</b></span><span><i class="is-previous"></i>직전 기간 <b>${number(summary.previousNewIssues)}</b></span></span>`
    : "";
  return `
    <section class="workspace-section ops-card ops-intake" aria-label="이번 기간 신규 이슈 ${total}건${complete ? "" : " 이상 · 확인 범위"} 중 종료 ${closed}건, 미종료 ${open}건${unknown ? `, 상태 미상 ${unknown}건` : ""}">
      <header class="ops-card-head">
        <div><h3>신규 등록 <strong>${escapeText(countText(total, complete))}</strong></h3><p>${compare ? `이번 ${periodDays}일 vs 직전 ${periodDays}일 · 누적 · 등록일 기준` : "이번 기간 누적 · 등록일 기준"}</p></div>
        <span class="ops-intake-side">${intakeMediaTabs(media, byMedia)}${intakeDelta(summary, complete)}</span>
      </header>
      ${legend}
      ${chart}
      <small class="dashboard-new-change"><b class="is-closed">종료 ${number(closed)}</b> · <b class="is-open">미종료 ${number(open)}</b>${unknown ? ` · <b>상태 미상 ${number(unknown)}</b>` : ""} · ${complete ? `직전 기간 ${number(summary.previousNewIssues)}건` : "확인된 이슈만 집계"}</small>
    </section>`;
}

function daysAgoLabel(issue) {
  const days = Number(issue.ageDays);
  if (!Number.isFinite(days) || issue.ageDays === null || issue.ageDays === "") return "등록일 미상";
  return days === 0 ? "오늘 등록" : `${days}일 전 등록`;
}

// 최근 등록 순. 등록일을 모르는 이슈는 뒤로 보낸다.
function recentOpenIssues(issues, limit = RECENT_LIMIT) {
  return [...(Array.isArray(issues) ? issues : [])]
    .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")) || Number(a.ageDays ?? 1e9) - Number(b.ageDays ?? 1e9))
    .slice(0, limit);
}

function recentOpenCard({ issues = [], statusLabel = (value) => value, selectedId = "", complete = true } = {}) {
  const rows = recentOpenIssues(issues);
  const body = rows.length ? `<ul class="ops-recent-list">${rows.map((issue) => `
      <li><button type="button" class="ops-recent-row${String(issue.id) === String(selectedId) ? " is-selected" : ""}" data-dashboard-issue="${escapeText(issue.id)}">
        <span class="ops-recent-main"><span class="ops-recent-id">#${escapeText(issue.id)}</span><strong>${escapeText(issue.title || "제목 없음")}</strong></span>
        <span class="ops-recent-chip">${escapeText(customerName(issue, "-"))}</span>
        <span class="ops-recent-meta"><span>${escapeText(statusLabel(issue.status))}</span><small>${escapeText(daysAgoLabel(issue))}</small></span>
        ${icon("chevron-right", 16)}
      </button></li>`).join("")}</ul>`
    // 일부만 조회했으면 '없다'고 단정하지 않는다.
    : `<p class="bklit-empty">${complete ? "미종료 이슈가 없어요." : "확인한 범위에는 미종료 이슈가 없어요. 일부만 확인한 결과예요."}</p>`;
  return `
    <section class="workspace-section ops-card ops-recent" aria-label="최근 등록 미종료 이슈">
      <header class="ops-card-head">
        <div><h3>최근 등록 미종료</h3><p>등록일이 가까운 순 · 눌러서 상세 보기</p></div>
        ${issues.length ? `<button type="button" class="ops-card-link" data-dashboard-jump>우선 확인 이슈 보기${icon("chevron-right", 14)}</button>` : ""}
      </header>
      ${body}
    </section>`;
}

const ROLE_LABELS = { member: "팀원", manager: "팀장", admin: "관리자" };
const AVATAR_TONES = 6;

// 이름에서 늘 같은 아바타 색을 고른다(차트 색 6가지 중 하나).
function avatarTone(name) {
  let hash = 0;
  for (const char of String(name || "")) hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return (hash % AVATAR_TONES) + 1;
}

function initial(name) {
  return String(name || "?").trim().slice(0, 1) || "?";
}

// 지연을 장기보다 먼저 센다(서버 agingOnly). 같은 이슈를 막대에 두 번 그리지 않는다.
function memberSegments(member) {
  const overdue = Number(member.overdue || 0);
  const aging = Number(member.agingOnly ?? Math.max(0, Number(member.aging || 0) - overdue));
  const open = Number(member.open || 0);
  return { overdue, aging, rest: Math.max(0, open - overdue - aging), open };
}

function memberTile(member, { max, complete }) {
  const name = member.displayName || member.mantisUsername || "이름 미등록";
  const seg = memberSegments(member);
  const width = (value) => (max > 0 ? Math.round((value / max) * 100) : 0);
  const chips = [
    seg.overdue ? `<span class="team-chip is-danger">지연 ${number(seg.overdue)}</span>` : "",
    seg.aging ? `<span class="team-chip is-warning">장기 ${number(seg.aging)}</span>` : "",
    Number(member.highRisk) ? `<span class="team-chip is-risk">고위험 ${number(member.highRisk)}</span>` : "",
  ].filter(Boolean).join("") || (seg.open ? '<span class="team-chip">기한 내 진행</span>' : '<span class="team-chip is-calm">미종료 없음</span>');
  const question = `${name} 담당 미종료 이슈를 우선순위 순으로 정리해줘`;
  return `
    <li><button type="button" class="team-member" data-dashboard-question="${escapeText(question)}" aria-label="${escapeText(`${name} 미종료 ${countText(seg.open, complete)}, AI에게 담당 이슈 정리 요청`)}">
      <span class="team-member-head">
        <span class="team-avatar is-tone-${avatarTone(name)}" aria-hidden="true">${escapeText(initial(name))}</span>
        <span class="team-member-name"><strong>${escapeText(name)}</strong><small>${escapeText(ROLE_LABELS[member.role] || member.role || "팀원")}</small></span>
      </span>
      <span class="team-member-count"><b>${number(seg.open)}</b><small>${complete ? "미종료" : "건 이상 미종료"}</small></span>
      <span class="team-member-bar" aria-hidden="true">
        <i class="is-overdue" style="width:${width(seg.overdue)}%"></i><i class="is-aging" style="width:${width(seg.aging)}%"></i><i class="is-rest" style="width:${width(seg.rest)}%"></i>
      </span>
      <span class="team-member-chips">${chips}</span>
    </button></li>`;
}

// 팀원 현황: 팀원마다 아바타·이름·미종료 건수와 지연/장기 구성. 누르면 그 팀원 담당 이슈 정리를 AI에게 요청한다.
function teamMembersCard({ members = [], complete = true, unassigned = 0 } = {}) {
  if (!members.length) return "";
  const max = Math.max(0, ...members.map((member) => Number(member.open || 0)));
  const stack = members.slice(0, 6).map((member) => {
    const name = member.displayName || member.mantisUsername || "?";
    return `<span class="team-avatar is-small is-tone-${avatarTone(name)}" title="${escapeText(name)}">${escapeText(initial(name))}</span>`;
  }).join("");
  const more = members.length > 6 ? `<span class="team-avatar is-small is-more">+${members.length - 6}</span>` : "";
  return `
    <section class="workspace-section ops-card team-members-card" aria-label="팀원 현황">
      <header class="ops-card-head">
        <div><h3>팀원 현황 <strong>${number(members.length)}명</strong></h3><p>담당 미종료 기준 · 이름을 누르면 담당 이슈 정리를 AI에게 요청해요.</p></div>
        <span class="team-avatar-stack" aria-hidden="true">${stack}${more}</span>
      </header>
      <span class="team-legend" aria-hidden="true"><span><i class="is-overdue"></i>지연</span><span><i class="is-aging"></i>장기(지연 제외)</span><span><i class="is-rest"></i>그 외</span>${unassigned ? `<em>담당자 미지정 ${escapeText(countText(unassigned, complete))}</em>` : ""}</span>
      <ul class="team-member-grid">${members.map((member) => memberTile(member, { max, complete })).join("")}</ul>
    </section>`;
}

// 고객사별 신규: 고객사 선택 목록 이름 기준 이번 기간 신규 상위. 로고·막대·직전 기간 대비, 누르면 고객사 상세.
function customerIntakeCard({ rows = [], complete = true, periodDays = 30 } = {}) {
  if (!rows.length) return "";
  const max = Math.max(1, ...rows.map((row) => Number(row.count || 0)));
  const items = rows.map((row) => {
    const diff = Number(row.count || 0) - Number(row.previous || 0);
    const delta = !complete ? "" : diff > 0 ? `<small class="is-up">+${number(diff)}</small>` : diff < 0 ? `<small class="is-down">${number(diff)}</small>` : "<small>±0</small>";
    return `
      <li><button type="button" class="ops-customer-row" data-customer-detail="${escapeText(row.project)}" aria-label="${escapeText(`${row.name} 이번 기간 신규 ${countText(row.count, complete)}, 고객사 상세 보기`)}">
        ${customerMark(row.name)}
        <span class="ops-customer-name">${escapeText(row.name)}</span>
        <span class="ops-customer-track" aria-hidden="true"><i style="width:${Math.round((Number(row.count || 0) / max) * 100)}%"></i></span>
        <span class="ops-customer-value"><b>${number(row.count)}</b>${delta}</span>
      </button></li>`;
  }).join("");
  return `
    <section class="workspace-section ops-card ops-customers" aria-label="고객사별 신규">
      <header class="ops-card-head">
        <div><h3>고객사별 신규</h3><p>이번 ${periodDays}일 등록 상위 · ${complete ? "옆 숫자는 직전 기간 대비" : "확인한 범위 기준"}</p></div>
        <button type="button" class="ops-card-link" data-dashboard-goto="customers">고객사 탭 ›</button>
      </header>
      <ol class="ops-customer-list">${items}</ol>
    </section>`;
}

// 팀 현황 지표 4개. 미종료·지연·장기는 기존 지표 필터(data-dashboard-metric)를 그대로 쓴다.
// 팀 핵심 지표: 내 업무·주간 보고·공수와 같은 지표 타일(이름 → 숫자+증감 배지 → 설명).
function opsKpiTiles({ summary = {}, complete = true, metric = "" } = {}) {
  const tile = (key, label, value, note, delta = "") => {
    const tone = Number(value) > 0 ? ({ overdue: " is-danger", aging: " is-warning" }[key] || "") : "";
    const body = `<span class="workspace-kpi-label">${label}</span><div class="workspace-kpi-value"><strong>${escapeText(countText(value, complete))}</strong>${delta}</div><small>${escapeText(note)}</small>`;
    return key
      ? `<button type="button" class="workspace-kpi snow-kpi${tone}" data-dashboard-metric="${key}" aria-pressed="${key === "open" ? !metric : metric === key}">${body}</button>`
      : `<article class="workspace-kpi snow-kpi">${body}</article>`;
  };
  const share = (value) => {
    const percent = shareOf(value, summary.open, complete);
    return percent == null ? "확인 범위 기준" : `미종료의 ${percent}%`;
  };
  const intakeNote = !complete ? "비교 미확정"
    : summary.previousNewIssues == null ? "등록일 기준" : `직전 기간 ${number(summary.previousNewIssues)}건 · 등록일 기준`;
  return `<section class="workspace-kpis snow-kpis" aria-label="팀 핵심 지표">
    ${tile("open", "미종료", summary.open, `이월 ${number(summary.carriedOpen)}건`)}
    ${tile("overdue", "일정 지연", summary.overdue, share(summary.overdue))}
    ${tile("aging", "장기 미종료", summary.aging, share(summary.aging))}
    ${tile("", "신규 등록", summary.newIssues, intakeNote, complete ? intakeBadge(summary.changeRate) : "")}
  </section>`;
}

// 담당자별 미종료: 지연 · 장기(지연 제외) · 그 외를 쌓은 세로 막대(bklit).
function memberBarsCard({ members = [], sharedAccounts = [], complete = true } = {}) {
  if (!members.length && !sharedAccounts.length) return "";
  const rows = [...members, ...sharedAccounts]
    .map((member) => ({ name: member.displayName || member.mantisUsername || "이름 미등록", ...memberSegments(member) }))
    .sort((a, b) => b.open - a.open);
  // 막대 이름이 겹치지 않게 미종료가 많은 담당자부터 표시한다.
  const shown = rows.slice(0, MEMBER_BAR_LIMIT);
  const fallback = `<ul class="ops-side-list">${rows.map((row) => `<li>${escapeText(row.name)} · 미종료 ${escapeText(countText(row.open, complete))} (지연 ${number(row.overdue)} · 장기 ${number(row.aging)})</li>`).join("")}</ul>`;
  return `
    <section class="workspace-section ops-card ops-members" aria-label="담당자별 미종료">
      <header class="ops-card-head"><div><h3>담당자별 미종료</h3><p>${complete ? "담당 미종료 · 지연과 장기 구성" : "확인한 범위 기준"}${sharedAccounts.length ? " · 공용 계정 포함" : ""}${rows.length > shown.length ? ` · 상위 ${shown.length}개 표시` : ""}</p></div></header>
      ${chartHost({ kind: "bar", title: "담당자별 미종료", complete, orientation: "vertical",
        rows: shown.map((row) => ({ name: row.name, overdue: row.overdue, aging: row.aging, rest: row.rest })),
        series: [
          { key: "overdue", label: "일정 지연", color: "var(--snow-rose, var(--danger))" },
          { key: "aging", label: "장기(지연 제외)", color: "var(--snow-indigo, var(--chart-3))" },
          { key: "rest", label: "그 외", color: "var(--snow-sky, var(--chart-1))" },
        ] }, fallback)}
    </section>`;
}

function priorityFlag(row) {
  if (row.overdue) return ["is-overdue", "일정 지연"];
  if (Number(row.ageDays) >= 30) return ["is-aging", "장기 미종료"];
  if (!String(row.handler || "").trim() || ["담당자 미지정", "미지정", "미상"].includes(String(row.handler).trim())) return ["is-unassigned", "담당자 미지정"];
  return ["is-high", (row.reasons || [])[0] || "높은 우선순위"];
}

// 우선 확인 이슈 상위 5개 표. 전체 목록·필터는 이슈 · 분포 탭에 있다.
function priorityTable({ rows = [], complete = true } = {}) {
  const top = rows.slice(0, 5);
  const body = top.length ? `<div class="ops-priority-table-wrap"><table class="ops-priority-table">
      <thead><tr><th scope="col">이슈</th><th scope="col">담당자</th><th scope="col">제목</th><th scope="col">고객사</th><th scope="col">경과</th><th scope="col">판단</th></tr></thead>
      <tbody>${top.map((row) => {
        const owner = String(row.handler || "").trim() || "미지정";
        const [tone, flag] = priorityFlag(row);
        return `<tr>
          <td><button type="button" data-dashboard-issue="${escapeText(row.id)}" aria-label="#${escapeText(row.id)} 상세 보기">#${escapeText(row.id)}</button></td>
          <td><span class="ops-owner"><i aria-hidden="true">${escapeText(initial(owner))}</i>${escapeText(owner)}</span></td>
          <td>${escapeText(row.title || "제목 없음")}</td>
          <td>${escapeText(customerName(row, "-"))}</td>
          <td>${row.ageDays == null ? "-" : `${number(row.ageDays)}일`}</td>
          <td><span class="ops-flag ${tone}">${escapeText(flag)}</span></td>
        </tr>`;
      }).join("")}</tbody></table></div>`
    : `<p class="bklit-empty">${complete ? "우선 확인할 이슈가 없어요." : "확인한 범위에는 우선 확인 이슈가 없어요."}</p>`;
  return `
    <section class="workspace-section ops-card ops-priority-card" aria-label="우선 확인 이슈 상위 5건">
      <header class="ops-card-head"><div><h3>우선 확인 이슈</h3><p>지연 · 장기 · 담당자 미지정 · 높은 우선순위</p></div>
        <button type="button" class="ops-card-link" data-dashboard-jump>이슈 · 분포에서 모두 보기 ›</button></header>
      ${body}
    </section>`;
}

const MEMBER_BAR_LIMIT = 8;
const SIDE_TONES = ["var(--snow-kpi-a)", "var(--snow-kpi-b)", "var(--snow-mint)", "#c6c7f8", "var(--snow-sky)"];

// 오른쪽 레일: 팀 알림(급증·미지정·주간 브리핑) · 최근 등록 · 팀원별 미종료.
// recentIssues: 지금 걸린 필터를 적용한 미종료 목록(없으면 전체 미종료).
function opsSideRail({ data = {}, complete = true, recentIssues = null } = {}) {
  const summary = data.summary || {};
  const signals = [
    ...(data.customerSpikes || []).slice(0, 2).map((item) => ({ icon: "chart", text: `${customerName(item.name)} 신규 급증`, note: `${number(item.count)}건 · 직전 ${number(item.previousCount)}건` })),
    ...(data.screenSpikes || []).slice(0, 1).map((item) => ({ icon: "grid", text: `화면 ${item.name} 신규 급증`, note: `${number(item.count)}건 · 직전 ${number(item.previousCount)}건` })),
    ...(Number(summary.unassigned) ? [{ icon: "users", text: `담당자 미지정 ${countText(summary.unassigned, complete)}`, note: "배정이 필요한 미종료" }] : []),
  ];
  const notice = `
    <section class="workspace-side-card" aria-labelledby="opsSideNotice"><h3 id="opsSideNotice">알림</h3>
      <ul class="ops-side-list">
        ${signals.map((item) => `<li><div class="ops-side-item"><span class="ops-side-icon" aria-hidden="true">${icon(item.icon, 14)}</span><span class="ops-side-text"><span>${escapeText(item.text)}</span><small>${escapeText(item.note)}</small></span></div></li>`).join("")}
        <li><button type="button" class="ops-side-item" data-open-team-brief><span class="ops-side-icon" aria-hidden="true">${icon("report", 14)}</span><span class="ops-side-text"><span>이번 주 팀 브리핑</span><small>내 업무 › 주간 보고에서 보기</small></span></button></li>
      </ul>
      ${complete ? "" : '<p class="ops-side-empty">일부만 확인해 급증 신호는 판단하지 않았어요.</p>'}
    </section>`;
  const recent = recentOpenIssues(recentIssues || data.openIssues || [], 5);
  const activity = `
    <section class="workspace-side-card" aria-labelledby="opsSideActivity"><h3 id="opsSideActivity">최근 등록</h3>
      ${recent.length ? `<ul class="ops-side-list">${recent.map((issue, index) => {
        const owner = String(issue.handler || "").trim() || "미지정";
        return `<li><button type="button" class="ops-side-item" data-dashboard-issue="${escapeText(issue.id)}" aria-label="#${escapeText(issue.id)} 상세 보기">
          <span class="ops-side-icon is-round" style="background:${SIDE_TONES[index % SIDE_TONES.length]}" aria-hidden="true">${escapeText(initial(owner))}</span>
          <span class="ops-side-text"><span>${escapeText(owner)} · #${escapeText(issue.id)} · ${escapeText(customerName(issue, "-"))}</span><small>${escapeText(daysAgoLabel(issue))} · 미종료</small></span>
        </button></li>`;
      }).join("")}</ul>` : `<p class="ops-side-empty">${complete ? "최근 등록된 미종료가 없어요." : "확인한 범위에는 최근 등록 미종료가 없어요."}</p>`}
    </section>`;
  const members = [...(data.members || [])].sort((a, b) => Number(b.open || 0) - Number(a.open || 0));
  const people = members.length ? `
    <section class="workspace-side-card" aria-labelledby="opsSidePeople"><h3 id="opsSidePeople">팀원 <span class="ops-side-count">${number(members.length)}</span></h3>
      <ul class="ops-side-list">${members.slice(0, 8).map((member, index) => {
        const name = member.displayName || member.mantisUsername || "이름 미등록";
        const open = Number(member.open || 0);
        return `<li><button type="button" class="ops-side-item" data-dashboard-question="${escapeText(`${name} 담당 미종료 이슈를 우선순위 순으로 정리해줘`)}" aria-label="${escapeText(`${name} 미종료 ${countText(open, complete)}, AI에게 담당 이슈 정리 요청`)}">
          <span class="ops-side-icon is-round" style="background:${SIDE_TONES[index % SIDE_TONES.length]}" aria-hidden="true">${escapeText(initial(name))}</span>
          <span class="ops-side-text"><span>${escapeText(name)}</span><small>${escapeText(ROLE_LABELS[member.role] || member.role || "팀원")}</small></span>
          <span class="ops-side-count${open >= 10 ? " is-high" : ""}">${number(open)}</span>
        </button></li>`;
      }).join("")}</ul>
    </section>` : "";
  return `<aside class="ops-side" aria-label="팀 알림과 활동">${notice}${activity}${people}</aside>`;
}

export { opsKpiTiles, memberBarsCard, priorityTable, opsSideRail, priorityFlag };
export { shareOf, customerIntakeCard, recentOpenIssues, opsSummaryCard, intakeCompareCard, recentOpenCard, avatarTone, memberSegments, teamMembersCard };
