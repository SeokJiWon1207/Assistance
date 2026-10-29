import { chartHost } from "./bklit_charts.js?v=20261026-1";

const escape = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const TYPES = ["개선", "검토/보류", "문의", "오류"];
const COLORS = { "개선": "#5b9bea", "검토/보류": "#9ca3af", "문의": "#e9b956", "오류": "#d978a2" };
const totalText = (value, complete) => `${Number(value || 0).toLocaleString("ko-KR")}건${complete ? "" : " 이상"}`;
const shortDate = day => `${Number(String(day).slice(5, 7))}/${Number(String(day).slice(8, 10))}`;

function groupedSpec(metric, title, complete) {
  const labels = [...new Set(metric.rows.flatMap(row => Object.keys(row.counts)))];
  labels.sort((a, b) => {
    const rank = label => TYPES.includes(label) ? TYPES.indexOf(label) : TYPES.length;
    return rank(a) - rank(b) || a.localeCompare(b, "ko");
  });
  const series = labels.map((label, index) => ({key: `series${index}`, label, color: COLORS[label] || `var(--chart-${index % 6 + 1})`}));
  return {kind: "bar", title, complete, grouped: true, orientation: "horizontal", series,
    rows: metric.rows.map(row => ({name: row.name, ...Object.fromEntries(series.map(s => [s.key, row.counts[s.label] || 0]))}))};
}

function valuesTable(metric, complete, grouped) {
  const rows = metric.rows.flatMap(row => grouped
    ? Object.entries(row.counts).map(([label, value]) => `<tr><th scope="row">${escape(row.name)}</th><td>${escape(label)}</td><td>${totalText(value, complete)}</td></tr>`)
    : [`<tr><th scope="row">${escape(row.name)}</th><td>${totalText(row.count, complete)}</td></tr>`]);
  return `<div class="dashboard-weekly-table"><table><thead><tr><th>매체</th>${grouped ? "<th>유형</th>" : ""}<th>건수</th></tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
}

function card(metric, title, basis, complete, grouped) {
  const spec = grouped ? groupedSpec(metric, title, complete) : {
    kind: "pie", title, centerLabel: "전체 잔여", complete, decimals: 0, unit: "건", animated: true, showValues: true,
    emptyMessage: "집계 기준에 해당하는 잔여 이슈가 없습니다.",
    rows: metric.rows.map((row, index) => ({fullLabel: row.name, amount: row.count,
      color: row.name === "HTS" ? "#5b9bea" : row.name === "MTS" ? "#e9b956" : `var(--chart-${index % 6 + 1})`}))};
  const empty = complete ? "집계 기준에 해당하는 이슈가 없습니다." : "확인한 범위에 해당 이슈가 없습니다. 전체 결과는 미확정입니다.";
  return `<section class="workspace-section dashboard-chart dashboard-weekly-card">
    <header class="dashboard-weekly-card-head"><h4>${title}</h4><p class="dashboard-weekly-total">${totalText(metric.total, complete)}</p><p>${basis}</p></header>
    <div class="dashboard-chart-body">${metric.total ? chartHost(spec, valuesTable(metric, complete, grouped)) : `<p class="bklit-empty">${empty}</p>`}</div>
    ${grouped && metric.missingDate ? `<details class="dashboard-weekly-missing"><summary>날짜 미등록·오류 ${totalText(metric.missingDate, complete)} 제외</summary><p>팀 전체 조회 범위에서 기준 날짜가 없거나 유효하지 않은 건수입니다. 다른 날짜로 대체하지 않습니다.</p></details>` : ""}
  </section>`;
}

export function renderWeeklyIssues(weekly) {
  if (!weekly) return "";
  const complete = weekly.complete === true;
  return `<section class="dashboard-weekly" aria-label="금주 이슈 현황">
    <div class="dashboard-weekly-heading"><div><h3>금주 이슈 현황</h3><p>${escape(shortDate(weekly.from))}(토)–${escape(shortDate(weekly.to))}(금) · 한국 시간 · 선택 기간과 별도 집계</p></div><span>토요일부터 금요일까지</span></div>
    ${!complete ? '<p class="dashboard-chart-note">확인한 범위 기준 · 일부 데이터만 조회되어 전체 건수와 비율은 미확정입니다.</p>' : ""}
    <div class="dashboard-weekly-grid">
      ${card(weekly.received, "금주 접수현황", "고객사요청일 기준", complete, true)}
      ${card(weekly.completed, "금주 완료현황", "고객사전달일 기준 · 제목의 정기점검 제외", complete, true)}
      ${card(weekly.remaining, "잔여현황", "전체 기간 · 매체별 잔여 이슈", complete, false)}
    </div>
    <p class="dashboard-chart-note">잔여현황은 종료된이슈·정보부족·개발완료·해결된이슈를 제외합니다. 금주 완료는 입력한 고객사전달일로 집계합니다.</p>
  </section>`;
}
