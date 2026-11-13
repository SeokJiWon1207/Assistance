// 내 업무 첫 화면의 지표 카드와 차트 카드(참고: beehiiv 대시보드 구성).
// 숫자는 서버가 준 담당 이슈와 등록일만 쓴다. 종료일 기록이 없어 '직전 대비'는 신규 등록에만 붙인다.
import { chartHost } from "./bklit_charts.js?v=20261026-1";
import { icon } from "./icons.js?v=20260911-6";

const AGE_BUCKETS = [
  { id: "0-7", name: "7일 이하", min: 0, max: 7 },
  { id: "8-29", name: "8–29일", min: 8, max: 29 },
  { id: "30-59", name: "30–59일", min: 30, max: 59 },
  { id: "60+", name: "60일 이상", min: 60, max: Infinity },
];

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function countText(value, complete) {
  return `${Number(value || 0).toLocaleString("ko-KR")}건${complete ? "" : " 이상"}`;
}

// 최근 7일과 직전 7일의 차이. 건수가 적어 비율은 오해를 부르므로 건수 차이로만 보여 준다.
function newIssueDelta(recent, previous, period = "직전 7일") {
  const diff = Number(recent || 0) - Number(previous || 0);
  if (diff > 0) return { tone: "is-up", icon: "arrow-up", label: `${diff}건`, text: `${period}보다 ${diff}건 많음` };
  if (diff < 0) return { tone: "is-down", icon: "arrow-down", label: `${-diff}건`, text: `${period}보다 ${-diff}건 적음` };
  return { tone: "is-flat", icon: "", label: "변동 없음", text: `${period}과 같음` };
}

// 서버가 계산한 전주 대비 비율(팀 브리핑). 비교 기준이 없거나 일부만 확인했으면 배지를 만들지 않는다.
function changeRateDelta(rate, complete) {
  if (!complete || rate == null || !Number.isFinite(Number(rate))) return null;
  const value = Number(rate);
  if (value > 0) return { tone: "is-up", icon: "arrow-up", label: `${value}%`, text: `지난주보다 ${value}% 많음` };
  if (value < 0) return { tone: "is-down", icon: "arrow-down", label: `${-value}%`, text: `지난주보다 ${-value}% 적음` };
  return { tone: "is-flat", icon: "", label: "변동 없음", text: "지난주와 같음" };
}

function ageRows(issues) {
  const rows = AGE_BUCKETS.map((bucket) => ({ id: bucket.id, name: bucket.name, minDays: bucket.min, count: 0 }));
  let unknown = 0;
  for (const issue of issues) {
    if (issue.ageDays == null || issue.ageDays === "") {
      unknown += 1;
      continue;
    }
    const days = Number(issue.ageDays);
    const index = AGE_BUCKETS.findIndex((bucket) => days >= bucket.min && days <= bucket.max);
    if (index >= 0) rows[index].count += 1;
  }
  return { rows, unknown };
}

function statusRows(issues, statusLabel) {
  const counts = new Map();
  for (const issue of issues) {
    const label = statusLabel(issue.status) || "상태 미상";
    counts.set(label, (counts.get(label) || 0) + 1);
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1])
    .map(([label, amount], index) => ({ fullLabel: label, amount, color: `var(--chart-${(index % 6) + 1})` }));
}

function kpi({ label, value, tone = "", detail = "", delta = null }) {
  const pill = delta ? `<em class="workspace-kpi-delta ${delta.tone}" title="${escapeText(delta.text)}">${delta.icon ? icon(delta.icon, 12) : ""}${escapeText(delta.label)}<span class="sr-only">, ${escapeText(delta.text)}</span></em>` : "";
  return `<article class="workspace-kpi ${tone}">
    <span class="workspace-kpi-label">${escapeText(label)}</span>
    <div class="workspace-kpi-value"><strong>${escapeText(value)}</strong>${pill}</div>
    ${detail ? `<small>${escapeText(detail)}</small>` : ""}
  </article>`;
}

// due: 목표일 칩과 같은 묶음으로 센 건수(workDueCounts). 일부만 조회했으면 건수 뒤에 '이상'이 붙는다.
function workspaceKpis(data, complete, due = {}) {
  const summary = data.summary || {};
  const activity = data.activity || {};
  const recent = activity.recentNew ?? data.weeklyBrief?.newIssues ?? 0;
  const hasPrevious = Number.isFinite(Number(activity.previousNew)) && activity.previousNew !== undefined;
  const pill = (tone, label, text) => ({ tone, icon: "", label, text });
  return `<section class="workspace-kpis" aria-label="내 업무 지표">
    ${kpi({ label: "미종료", value: countText(summary.open, complete), tone: summary.open ? "is-blue" : "",
      delta: complete && due.overdue ? pill("is-bad", `${due.overdue}건 지연`, `기한이 지난 업무 ${due.overdue}건`) : null,
      detail: complete ? "내 담당 업무" : "확인한 범위 기준" })}
    ${kpi({ label: "이번 주 마감", value: countText(due.week, complete),
      delta: complete && due.today ? pill("is-flat", `오늘 ${due.today}`, `오늘이 목표일인 업무 ${due.today}건`) : null,
      detail: complete ? "오늘부터 7일 안" : "확인한 범위 기준" })}
    ${kpi({ label: "목표일 없음", value: countText(due.none, complete), tone: due.none ? "is-warning" : "",
      detail: !complete ? "확인한 범위 기준" : due.none ? "날짜 지정 필요" : "모두 목표일이 있어요" })}
    ${kpi({ label: "최근 7일 신규", value: countText(recent, complete), delta: hasPrevious && complete ? newIssueDelta(recent, activity.previousNew) : null,
      detail: hasPrevious ? `직전 7일 ${countText(activity.previousNew, complete)} · 등록일 기준` : "등록일 기준" })}
  </section>`;
}

function chartCard({ title, subtitle = "", aside = "", body, wide = false }) {
  return `<section class="workspace-chart-card${wide ? " is-wide" : ""}" aria-label="${escapeText(title)}">
    <header><div><h3>${escapeText(title)}</h3>${subtitle ? `<p>${escapeText(subtitle)}</p>` : ""}</div>${aside}</header>
    ${body}
  </section>`;
}

// 고객사별 담당: 미종료 담당 이슈를 고객사 이름으로 세어 많은 순 6곳. 로고 표시는 호출한 쪽이 넘긴다.
function customerShareCard(openIssues, complete, { customerLabel = (issue) => issue.customer_name || "고객사 미상", customerMark = () => "" } = {}) {
  const counts = new Map();
  for (const issue of openIssues) {
    const name = customerLabel(issue) || "고객사 미상";
    counts.set(name, (counts.get(name) || 0) + 1);
  }
  const rows = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0], "ko")).slice(0, 6);
  const max = Math.max(1, ...rows.map(([, count]) => count));
  return chartCard({
    title: "고객사별 담당",
    subtitle: complete ? "내 담당 미종료" : "확인한 범위 기준",
    body: rows.length
      ? `<ul class="workspace-customer-list">${rows.map(([name, count]) => `
          <li>${customerMark(name)}<span class="workspace-customer-name">${escapeText(name)}</span>
            <span class="workspace-customer-track" aria-hidden="true"><i style="width:${Math.round((count / max) * 100)}%"></i></span>
            <b>${escapeText(countText(count, complete))}</b></li>`).join("")}</ul>`
      : '<p class="bklit-empty">미종료 이슈가 없어요.</p>',
  });
}

function workspaceCharts(data, complete, { statusLabel = (value) => value, customerLabel, customerMark } = {}) {
  const activity = data.activity || {};
  const points = Array.isArray(activity.points) ? activity.points : [];
  const total = points.reduce((sum, point) => sum + Number(point.count || 0), 0);
  const days = points.length || 30;
  const openIssues = data.mantis?.issues || [];
  const age = ageRows(openIssues);
  const statuses = statusRows(openIssues, statusLabel);
  const legend = `<span class="workspace-chart-legend"><i aria-hidden="true"></i>신규 등록<strong>${escapeText(countText(total, complete))}</strong></span>`;
  const trend = chartCard({
    title: "내 담당 신규 등록 추이",
    subtitle: `최근 ${days}일 누적 · ${activity.dateBasis || "등록일"} 기준 · 종료된 이슈 포함`,
    aside: legend,
    wide: true,
    body: chartHost({ kind: "area", cumulative: true, title: "내 담당 신규 등록 추이", complete, points, series: [{ key: "count", label: "신규 등록", color: "var(--chart-1)" }],
      emptyMessage: "등록 추이를 확인하지 못했어요." }, `<p class="bklit-empty">최근 ${days}일 신규 등록 ${escapeText(countText(total, complete))}</p>`),
  });
  // 미종료가 없으면 빈 분포 카드를 반복하지 않는다. 처리된 업무의 등록 이력은 남긴다.
  if (!openIssues.length) return total > 0 ? `<div class="workspace-chart-row is-single">${trend}</div>` : "";
  const agingCount = age.rows.filter((row) => row.minDays >= 30).reduce((sum, row) => sum + row.count, 0);
  const ageCard = chartCard({
    title: "미종료 경과일",
    subtitle: "등록 후 지난 일수",
    body: openIssues.length
      ? chartHost({ kind: "age-donut", unknown: age.unknown, title: "미종료 경과일", complete, rows: age.rows, series: [{ key: "count", label: "미종료", color: "var(--chart-1)" }] },
        `<p class="bklit-empty">30일 이상 ${escapeText(countText(agingCount, complete))}</p>`)
      : '<p class="bklit-empty">미종료 이슈가 없어요.</p>',
  });
  const statusCard = chartCard({
    title: "상태별 미종료",
    subtitle: "Mantis 현재 상태",
    body: statuses.length
      ? chartHost({ kind: "pie", title: "상태별 미종료", complete, rows: statuses, decimals: 0, unit: "건", centerLabel: "미종료", emptyMessage: "미종료 이슈가 없어요." },
        `<ul class="workspace-chart-values">${statuses.map((row) => `<li><span>${escapeText(row.fullLabel)}</span><strong>${escapeText(countText(row.amount, complete))}</strong></li>`).join("")}</ul>`)
      : '<p class="bklit-empty">미종료 이슈가 없어요.</p>',
  });
  const customers = customerShareCard(openIssues, complete, { customerLabel, customerMark });
  return `<div class="workspace-chart-row is-wide-first">${trend}${customers}</div><div class="workspace-chart-row">${ageCard}${statusCard}</div>`;
}

// 주간 보고(내 보고): 이번 주 7일과 바로 앞 7일. 30일 추이(activity.points)의 마지막 14일을 잘라 쓴다.
function weekWindows(activity = {}) {
  const points = Array.isArray(activity.points) ? activity.points : [];
  if (points.length < 14) return { current: points.slice(-7), previous: [] };
  return { current: points.slice(-7), previous: points.slice(-14, -7) };
}

function weeklyKpis(data, complete) {
  const brief = data.weeklyBrief || {};
  const activity = data.activity || {};
  const hasPrevious = activity.previousNew !== undefined && Number.isFinite(Number(activity.previousNew));
  return `<section class="workspace-kpis" aria-label="이번 주 업무 지표">
    ${kpi({ label: "신규 이슈", value: countText(brief.newIssues, complete), delta: hasPrevious && complete ? newIssueDelta(brief.newIssues, activity.previousNew, "지난주") : null,
      detail: hasPrevious ? `지난주 ${countText(activity.previousNew, complete)} · 등록일 기준` : "이번 주 등록" })}
    ${kpi({ label: "변경 이슈", value: countText(brief.updatedIssues, complete), tone: brief.updatedIssues ? "is-blue" : "", detail: "이번 주 수정된 담당 이슈" })}
    ${kpi({ label: "현재 미종료", value: countText(brief.openIssues, complete), detail: complete ? "내 담당 업무" : "확인한 범위 기준" })}
    ${kpi({ label: "일정 지연", value: countText(brief.overdueIssues, complete), tone: brief.overdueIssues ? "is-danger" : "", detail: !complete ? "확인한 범위 기준" : brief.overdueIssues ? "기한이 지난 업무" : "모두 기한 내 진행 중" })}
  </section>`;
}

function weeklyTrendCard(data, complete) {
  const { current, previous } = weekWindows(data.activity);
  const total = current.reduce((sum, point) => sum + Number(point.count || 0), 0);
  const previousTotal = previous.reduce((sum, point) => sum + Number(point.count || 0), 0);
  const compare = complete && previous.length === current.length && current.length > 0;
  const legend = compare
    ? `<span class="workspace-chart-legend is-pair"><span><i aria-hidden="true"></i>이번 주<strong>${escapeText(countText(total, complete))}</strong></span><span><i class="is-previous" aria-hidden="true"></i>지난주<strong>${escapeText(countText(previousTotal, complete))}</strong></span></span>`
    : `<span class="workspace-chart-legend"><i aria-hidden="true"></i>이번 주<strong>${escapeText(countText(total, complete))}</strong></span>`;
  const spec = compare
    // 팀 현황 신규 등록 비교와 같은 선 모양: 이번 주 실선, 지난주 연한 점선.
    ? { kind: "compare-area", title: "이번 주와 지난주 신규 등록 비교", complete, points: current, previousPoints: previous, lineStyle: "snow",
      series: [{ key: "total", label: "이번 주", color: "var(--chart-1)" }, { key: "previousTotal", label: "지난주", color: "var(--chart-2)" }] }
    : { kind: "area", cumulative: true, title: "이번 주 신규 등록", complete, points: current, series: [{ key: "count", label: "신규 등록", color: "var(--chart-1)" }],
      emptyMessage: "등록 추이를 확인하지 못했어요." };
  return chartCard({
    title: "이번 주 신규 등록",
    subtitle: compare ? "이번 주 vs 지난주 · 날짜 순서별 누적 · 등록일 기준" : "이번 주 누적 · 등록일 기준",
    aside: legend,
    wide: true,
    body: chartHost(spec, `<p class="bklit-empty">이번 주 신규 ${escapeText(countText(total, complete))}${compare ? ` · 지난주 ${escapeText(countText(previousTotal, complete))}` : ""}</p>`),
  });
}

// 팀 브리핑: 같은 지표 카드 모양. 전주 대비는 서버의 changeRate를 배지로 쓴다.
function teamBriefKpis(summary = {}, complete = true) {
  const delta = changeRateDelta(summary.changeRate, complete);
  return `<section class="workspace-kpis" aria-label="팀 브리핑 지표">
    ${kpi({ label: "지난주 신규", value: countText(summary.newIssues, complete), delta,
      detail: !complete ? "전주 대비 비교 미확정" : summary.changeRate == null ? "전주 비교 기준 없음" : "전주 대비 등록 증감" })}
    ${kpi({ label: "미종료 이월", value: countText(summary.carriedOpen, complete), tone: summary.carriedOpen ? "is-blue" : "", detail: "지난주 이전 등록 · 아직 미종료" })}
    ${kpi({ label: "장기 미종료", value: countText(summary.aging, complete), tone: summary.aging ? "is-warning" : "", detail: summary.aging ? "30일 이상 미종료" : "30일 이상 미종료 없음" })}
    ${kpi({ label: "일정 지연", value: countText(summary.overdue, complete), tone: summary.overdue ? "is-danger" : "", detail: `담당자 미지정 ${countText(summary.unassigned, complete)}` })}
  </section>`;
}

export { customerShareCard };
export { AGE_BUCKETS, ageRows, statusRows, newIssueDelta, changeRateDelta, weekWindows, workspaceKpis, workspaceCharts, weeklyKpis, weeklyTrendCard, teamBriefKpis };
