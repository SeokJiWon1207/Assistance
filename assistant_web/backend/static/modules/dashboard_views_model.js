// 대시보드 '내 보기'와 CSV 내보내기의 순수 함수. 저장·DOM은 dashboard_views.js가 맡는다.
const VIEW_LIMIT = 12;
const PERIODS = [7, 14, 30, 90];
const METRIC_LABELS = { overdue: "일정 지연", aging: "장기 미종료", highRisk: "고위험" };
const AGE_LABELS = { "0-7": "7일 이하", "8-29": "8–29일", "30-59": "30–59일", "60+": "60일 이상" };
const OPEN_STATUS_LABELS = ["신규 이슈", "정보 부족", "이슈 검토", "확인된 이슈", "할당된 이슈", "개발완료"];

function normalizeFilters(filters = {}) {
  const period = Number(filters.period);
  return {
    period: PERIODS.includes(period) ? period : 30,
    metric: Object.hasOwn(METRIC_LABELS, filters.metric) ? filters.metric : "",
    customer: String(filters.customer || "").slice(0, 120),
    age: Object.hasOwn(AGE_LABELS, filters.age) ? filters.age : "",
  };
}

function sameFilters(a, b) {
  const left = normalizeFilters(a);
  const right = normalizeFilters(b);
  return Object.keys(left).every((key) => left[key] === right[key]);
}

function sanitizeViews(views) {
  return (Array.isArray(views) ? views : [])
    .filter((view) => view && typeof view.id === "string" && String(view.name || "").trim())
    .map((view) => ({ id: view.id, name: String(view.name).trim().slice(0, 40), filters: normalizeFilters(view.filters), updatedAt: String(view.updatedAt || "") }))
    .slice(0, VIEW_LIMIT);
}

// 같은 이름으로 저장하면 그 보기를 고친다. 개수 제한을 넘으면 저장하지 않고 이유를 돌려준다.
function saveView(views, { name, filters, id = "" }, now = new Date()) {
  const cleanName = String(name || "").trim().slice(0, 40);
  if (!cleanName) return { views: sanitizeViews(views), error: "보기 이름을 입력해 주세요." };
  const list = sanitizeViews(views);
  const existing = list.find((view) => view.name === cleanName);
  if (!existing && list.length >= VIEW_LIMIT) return { views: list, error: `보기는 ${VIEW_LIMIT}개까지 저장할 수 있어요.` };
  const view = { id: existing?.id || id || `view-${now.getTime().toString(36)}`, name: cleanName, filters: normalizeFilters(filters), updatedAt: now.toISOString() };
  return { views: existing ? list.map((item) => (item.id === existing.id ? view : item)) : [...list, view], view, error: "" };
}

function updateViewFilters(views, id, filters, now = new Date()) {
  return sanitizeViews(views).map((view) => (view.id === id ? { ...view, filters: normalizeFilters(filters), updatedAt: now.toISOString() } : view));
}

function removeView(views, id) {
  return sanitizeViews(views).filter((view) => view.id !== id);
}

function describeFilters(filters, { customerName = (value) => value } = {}) {
  const value = normalizeFilters(filters);
  return [
    `${value.period}일`,
    value.customer ? `고객사 ${customerName(value.customer)}` : "",
    value.metric ? METRIC_LABELS[value.metric] : "",
    value.age ? `경과 ${AGE_LABELS[value.age]}` : "",
  ].filter(Boolean).join(" · ");
}

// 엑셀이 =, +, -, @로 시작하는 칸을 수식으로 실행하지 않도록 앞에 작은따옴표를 붙인다.
function csvCell(value) {
  let text = String(value ?? "");
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`;
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function priorityCsv(rows, { customerName = (issue) => issue.project || "", statusLabel = (value) => value || "", riskLabel = (value) => value || "" } = {}) {
  const header = ["이슈 ID", "제목", "고객사", "상태", "담당자", "경과일", "위험도", "위험 사유"];
  const lines = (Array.isArray(rows) ? rows : []).map((issue) => [
    issue.id,
    issue.title,
    customerName(issue),
    statusLabel(issue.status),
    issue.handler || "미지정",
    issue.ageDays ?? "",
    riskLabel(["high", "attention"].includes(issue.riskLevel) ? issue.riskLevel : "watch"),
    (issue.riskReasons || []).join(" · "),
  ].map(csvCell).join(","));
  // 한글이 깨지지 않도록 엑셀용 BOM을 앞에 둔다.
  return `﻿${[header.join(","), ...lines].join("\r\n")}\r\n`;
}

function isOpenIssue(issue, statusLabel = (value) => value) {
  if (typeof issue?.isOpen === "boolean") return issue.isOpen;
  return OPEN_STATUS_LABELS.includes(statusLabel(issue?.status));
}

function myWorkSummary(issues, { statusLabel = (value) => value } = {}) {
  const open = (Array.isArray(issues) ? issues : []).filter((issue) => isOpenIssue(issue, statusLabel));
  return {
    open: open.length,
    overdue: open.filter((issue) => issue.overdue).length,
    aging: open.filter((issue) => !issue.overdue && Number(issue.ageDays || 0) >= 30).length,
  };
}

export {
  VIEW_LIMIT,
  normalizeFilters,
  sameFilters,
  sanitizeViews,
  saveView,
  updateViewFilters,
  removeView,
  describeFilters,
  csvCell,
  priorityCsv,
  myWorkSummary,
};
