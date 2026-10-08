import { formatUiDateTime } from "./ui_format.js?v=20260911-6";

const COVERAGE_LABELS = { "부분 조회": "일부만 확인", "선택 범위 조회 완료": "선택 범위 확인 완료", "범위 미확인": "범위 확인 필요" };

export function answerScopeLabel(row) {
  const parts = [row.source, row.basis, COVERAGE_LABELS[row.coverage] || row.coverage];
  if (row.databaseCount) parts.push(`${row.databaseCount}개 DB`);
  if (row.asOf) {
    parts.push(`${row.asOfLabel || "데이터 기준"} ${formatUiDateTime(row.asOf)}`);
  }
  return parts.filter(Boolean).join(" · ");
}

export function renderAnswerScope(rows) {
  if (!rows?.length) return null;
  const warning = rows.some((row) => row.warning);
  const section = document.createElement(warning ? "section" : "details");
  section.className = warning ? "answer-data-scope is-warning" : "answer-data-scope";
  section.setAttribute("aria-label", "답변 데이터 기준과 조회 범위");
  if (!warning) {
    const summary = document.createElement("summary");
    summary.textContent = `데이터 기준 · ${[...new Set(rows.map((row) => row.source))].join(" · ")}`;
    section.appendChild(summary);
  }
  rows.forEach((row) => {
    const line = document.createElement("div");
    line.className = row.warning ? "answer-data-scope-row is-warning" : "answer-data-scope-row";
    const label = document.createElement("span");
    label.textContent = answerScopeLabel(row);
    line.appendChild(label);
    (row.notes || []).forEach((text) => {
      const note = document.createElement("small");
      note.textContent = text;
      line.appendChild(note);
    });
    section.appendChild(line);
  });
  return section;
}
