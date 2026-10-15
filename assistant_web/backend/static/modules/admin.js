// 관리자 센터: 품질 현황 · 검증 실행 · 답변 피드백 · 감사 로그 화면과 검증 실행 흐름.
// 사용자 관리는 admin_users.js 가 맡고, 이 모듈은 섹션 전환과 품질 데이터만 다룬다.
import { formatUiDateTime } from "./ui_format.js?v=20260911-6";
import { state, els, apiFetch, ADMIN_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { escapeHtml } from "./chat.js?v=20260911-6";
import { TEAM_TEMPLATES } from "./template_picker.js?v=20260911-6";
import { setMainViewLoader } from "./dashboard.js?v=20260911-6";
import { showToast } from "./workspace.js?v=20260911-6";
import { formatBadgeDate, renderDataBadge } from "./data_badge.js?v=20260911-6";
import {
  initAdminUsers,
  loadAdminUsers,
  pendingAdminUsers,
  renderUsersSection,
  usersSectionAvailable,
} from "./admin_users.js?v=20261015-reset";

const ADMIN_SECTIONS = ["overview", "run", "feedback", "logs", "users"];

const MODE_LABELS = {
  model: "모델 답변",
  deterministic_issue_list: "Mantis 목록",
  deterministic_notion_list: "Notion 목록",
  deterministic_effort_report: "공수 리포트",
  deterministic_mantis_month_count: "월별 건수",
  deterministic_combined_search: "통합 검색",
  deterministic_content: "본문 조회",
  deterministic_customer_profile: "고객사 프로필",
  deterministic_effort_ranking: "공수 순위",
  deterministic_effort_trend: "공수 추이",
  deterministic_filtered_effort_report: "조건별 공수 리포트",
  deterministic_incident_triage: "장애 초동 대응",
  deterministic_mantis_count: "Mantis 건수",
  deterministic_mantis_pivot: "Mantis 교차 집계",
  deterministic_mantis_sla: "SLA 집계",
  deterministic_mantis_spike: "급증 이슈",
  deterministic_monthly_customer_report: "고객사 월간 리포트",
  deterministic_similar_resolution: "유사 사례",
  deterministic_unknown_customer: "고객사 확인 필요",
  deterministic_weekly_report: "주간 보고서",
  status_clarification: "상태 조건 되묻기",
  missing_personal_scope: "담당자 정보 없음",
  notion_update_missing_fields: "Notion 수정(필드 누락)",
  notion_update_missing_target: "Notion 수정(대상 못 찾음)",
  notion_update_ambiguous: "Notion 수정(대상 모호)",
  notion_update: "Notion 수정",
  notion_create: "Notion 생성",
  model_timeout_fallback: "모델 시간 초과 대체 답변",
};

const LOG_STATUS = {
  pass: ["통과", "is-pass"],
  warning: ["확인 필요", "is-wait"],
  fail: ["실패", "is-fail"],
};

// 내부 모드 이름은 화면에 그대로 보이지 않게 한다. 새 모드가 생겨도 영문 식별자 대신 묶음 이름을 쓴다.
function modeLabel(mode) {
  if (MODE_LABELS[mode]) return MODE_LABELS[mode];
  if (String(mode || "").startsWith("deterministic_")) return "자동 집계";
  return mode ? "기타 답변" : "알 수 없음";
}

function teamTemplateLabel(templateId) {
  return TEAM_TEMPLATES.find((template) => template.id === templateId)?.title || templateId || "일반 질문";
}

function formatEvalDuration(ms) {
  if (!Number.isFinite(ms)) return "";
  if (ms < 1000) return `${ms}ms`;
  if (ms >= 60_000) return `약 ${Math.round(ms / 60_000)}분`;
  return `${(ms / 1000).toFixed(1)}초`;
}

// 검증 시각도 다른 화면과 같은 로컬 시간대·분 단위로 표시한다.
function formatRunTime(value) {
  return formatUiDateTime(value, { compact: true });
}

function accuracyValueLabel(value) {
  if (value === null || value === undefined) return "없음";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function statusPill(label, tone) {
  return `<span class="admin-pill ${tone}">${escapeHtml(label)}</span>`;
}

function sectionHeader(title, description, aside = "") {
  return `<header class="admin-section-header"><div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div>${aside}</header>`;
}

function barList(entries, tone = "") {
  if (!entries.length) return `<p class="admin-empty">아직 집계할 항목이 없습니다.</p>`;
  const max = Math.max(1, ...entries.map(([, count]) => Number(count) || 0));
  return `<div class="admin-bars">${entries.map(([label, count]) => `
    <div class="admin-bar-row"><span>${escapeHtml(label)}</span>
      <span class="admin-bar ${tone}"><i style="width:${Math.round(((Number(count) || 0) / max) * 100)}%"></i></span>
      <b>${Number(count) || 0}</b></div>`).join("")}</div>`;
}

function chips(group, options, active) {
  return `<div class="admin-chips" role="group">${options.map(([value, label, count]) => `
    <button type="button" class="admin-chip" data-admin-${group}="${value}" aria-pressed="${value === active}">${escapeHtml(label)}${count === undefined ? "" : ` <b>${count}</b>`}</button>`).join("")}</div>`;
}

async function loadEvaluationData() {
  const [casesResponse, logsResponse, summaryResponse, historyResponse, feedbackResponse] = await Promise.all([
    apiFetch(ADMIN_ENDPOINTS.evaluationCases),
    apiFetch(ADMIN_ENDPOINTS.validationLogs),
    apiFetch(ADMIN_ENDPOINTS.validationSummary),
    apiFetch(ADMIN_ENDPOINTS.evaluationHistory),
    apiFetch(ADMIN_ENDPOINTS.feedbackSummary),
  ]);
  if (!casesResponse.ok || !logsResponse.ok || !summaryResponse.ok) {
    throw new Error("검증 데이터를 불러오지 못했습니다.");
  }
  state.evaluationCases = (await casesResponse.json()).cases || [];
  state.validationLogs = (await logsResponse.json()).logs || [];
  state.validationSummary = await summaryResponse.json();
  // 실행 이력과 피드백은 부가 정보라 실패해도 화면 자체는 열리게 둔다.
  state.evaluationHistory = historyResponse.ok ? (await historyResponse.json()).runs || [] : [];
  state.feedbackSummary = feedbackResponse.ok ? await feedbackResponse.json() : null;
  await refreshAccuracyReports();
}

async function refreshFeedbackSummary() {
  try {
    const response = await apiFetch(ADMIN_ENDPOINTS.feedbackSummary);
    if (!response.ok) return;
    state.feedbackSummary = await response.json();
    if (state.adminSection === "feedback" || state.adminSection === "overview") renderEvaluationContent();
  } catch {
    // 피드백 집계는 답변 사용을 막지 않는 부가 정보다.
  }
}

async function refreshAccuracyReports() {
  try {
    const response = await apiFetch("/api/accuracy/reports?limit=8");
    if (!response.ok) return;
    const data = await response.json();
    state.accuracyReports = data.reports || [];
    state.accuracyAnalytics = data.analytics || null;
    state.dataFreshness = data.freshness?.mantis || null;
    // 실행 직후에는 라이브 결과를 그대로 보여주므로 최신 리포트 상세는
    // 라이브 결과가 없을 때만 불러온다.
    if (!state.accuracyLive.length && state.accuracyReports.length) {
      const latest = state.accuracyReports[0];
      const detail = await apiFetch(`/api/accuracy/reports/${encodeURIComponent(latest.name)}`);
      if (detail.ok) state.accuracyReport = await detail.json();
    }
  } catch {
    // 정확도 리포트는 부가 정보라 실패해도 무시한다.
  }
}

async function refreshEvaluationHistory() {
  try {
    const response = await apiFetch(ADMIN_ENDPOINTS.evaluationHistory);
    if (response.ok) state.evaluationHistory = (await response.json()).runs || [];
  } catch {
    // 이력 갱신 실패는 치명적이지 않다.
  }
}

// ---- 품질 현황 ----------------------------------------------------------

// 정확도 검사가 쓰는 Mantis 저장본의 기준일을 공통 '데이터 기준' 배지로 보여 준다.
function renderFreshnessBadge() {
  const slot = els.adminContent.querySelector("[data-admin-freshness]");
  const freshness = state.dataFreshness;
  if (!slot || !freshness) return;
  const known = Number.isFinite(freshness.ageDays);
  const age = !known ? "기준일 미확인" : freshness.ageDays === 0 ? "오늘 기준" : `${freshness.ageDays}일 지연`;
  const fresh = freshness.status === "fresh";
  renderDataBadge(slot, {
    state: fresh ? "fresh" : "warning",
    time: age,
    detailTime: formatBadgeDate(freshness.asOf) ? `${formatBadgeDate(freshness.asOf)} 기준` : "",
    sources: [{ name: "Mantis", tone: fresh ? "ok" : "warn", text: `저장본 ${freshness.label || "확인 필요"} · ${age}` }],
    note: fresh ? "" : "정확도 검사가 오래된 저장본으로 채점될 수 있어요.",
  });
}

function accuracySummary() {
  if (state.accuracyLive.length && !state.accuracyRunning) {
    const done = state.accuracyLive.filter((result) => !result.pending);
    return { passed: done.filter((result) => result.passed).length, total: done.length, score: null, runAt: "" };
  }
  const latest = state.accuracyReports[0];
  return latest ? { ...(latest.summary || {}), runAt: latest.runAt } : null;
}

function attentionItems() {
  const items = [];
  for (const result of state.evaluationResults.values()) {
    if (result.passed) continue;
    const failed = (result.checks || []).find((check) => !check.passed);
    items.push({ kind: "기준 실패", tone: "is-fail", title: result.question || result.id, why: failed ? `${failed.label}: ${failed.actual}` : "검사 항목 실패", go: "run", kindKey: "base", open: result.id });
  }
  const accuracyResults = state.accuracyLive.length ? state.accuracyLive : state.accuracyReport?.results || [];
  accuracyResults.filter((result) => !result.pending && !result.passed).forEach((result, index) => {
    const dependency = ["dependency_error", "execution_error"].includes(result.outcome);
    items.push({
      kind: dependency ? "연동 오류" : "정확도",
      tone: "is-fail",
      title: result.question || "",
      why: result.error ? String(result.error) : `기대 ${accuracyValueLabel(result.expected)} · 실제 ${accuracyValueLabel(result.actual)}`,
      go: "run",
      kindKey: "accuracy",
      open: `accuracy-${index}`,
    });
  });
  (state.validationSummary?.recentIssues || []).slice(0, 3).forEach((issue) => {
    items.push({ kind: "감사 로그", tone: "is-wait", title: issue.question || "", why: `${modeLabel(issue.mode)} · ${issue.score}점`, go: "logs" });
  });
  const pending = pendingAdminUsers();
  if (pending.length) {
    items.push({ kind: "승인 대기", tone: "is-wait", title: pending.map((user) => user.displayName || user.email).join(", "), why: "로그인을 기다리고 있어요", go: "users", filter: "pending", cta: "확인하기" });
  }
  return items.slice(0, 6);
}

function renderOverview() {
  const latestRun = state.evaluationHistory[0];
  const accuracy = accuracySummary();
  const analytics = state.accuracyAnalytics || {};
  const feedback = state.feedbackSummary;
  const pending = pendingAdminUsers();
  const trend = (analytics.trend || []).slice(-8);
  const delta = analytics.latestDelta;
  const trendBars = trend.map((item, index) => {
    const available = item.score !== null && item.score !== undefined;
    const height = available ? Math.max(3, Math.round(((Number(item.score) || 0) / 100) * 22)) : 3;
    const label = available ? `${item.score}점` : "불완전";
    return `<i class="${index === trend.length - 1 ? "is-latest" : ""}${available ? "" : " is-empty"}" style="height:${height}px" title="${escapeHtml(formatRunTime(item.runAt))} · ${label}"></i>`;
  }).join("");
  const accuracyScore = accuracy?.score === null || accuracy?.score === undefined
    ? (accuracy?.total ? `${accuracy.passed}/${accuracy.total}` : "—")
    : `${accuracy.score}점`;
  const metrics = [
    `<button type="button" class="admin-metric" data-admin-go="run" data-admin-kind="base" data-admin-filter="${latestRun?.failed ? "fail" : "all"}">
      <small>기준 질문 검사</small><strong>${latestRun ? `${latestRun.passed}/${latestRun.total}` : "—"}</strong>
      <span class="${latestRun?.failed ? "is-danger" : ""}">${latestRun ? `${latestRun.failed ? `${latestRun.failed}개 실패` : "모두 통과"} · ${escapeHtml(formatRunTime(latestRun.run_at))}` : "아직 실행 기록이 없어요"}</span></button>`,
    `<button type="button" class="admin-metric" data-admin-go="run" data-admin-kind="accuracy" data-admin-filter="all">
      <small>답변 정확도</small><strong>${escapeHtml(accuracyScore)}</strong>
      <span class="admin-trend">${trendBars}<em class="${delta < 0 ? "is-danger" : "is-good"}">${delta === null || delta === undefined ? (accuracy ? "비교 이력 없음" : "아직 실행 기록이 없어요") : `직전 대비 ${delta > 0 ? "+" : ""}${delta}점`}</em></span></button>`,
    `<button type="button" class="admin-metric" data-admin-go="feedback">
      <small>답변 도움됨</small><strong>${feedback?.total ? `${feedback.helpfulRate || 0}%` : "—"}</strong>
      <span>${feedback?.total ? `피드백 ${feedback.total}건 · 개선 필요 ${feedback.needsImprovementCount || 0}건` : "아직 피드백이 없어요"}</span></button>`,
  ];
  if (usersSectionAvailable()) {
    metrics.push(`<button type="button" class="admin-metric" data-admin-go="users" data-admin-filter="${pending.length ? "pending" : "all"}">
      <small>승인 대기 사용자</small><strong class="${pending.length ? "is-warning" : ""}">${pending.length}명</strong>
      <span>${pending.length ? "눌러서 바로 승인" : "모두 승인됨"}</span></button>`);
  }
  const attention = attentionItems();
  const failures = (analytics.failureCounts || []).slice(0, 5).map((item) => [item.category, item.count]);
  els.adminContent.innerHTML = `
    ${sectionHeader("품질 현황", "검증 실행 결과, 답변 피드백, 자동 채점을 한 화면에서 봅니다.", '<span class="data-badge-slot" data-admin-freshness></span>')}
    <div class="admin-metrics">${metrics.join("")}</div>
    <div class="admin-columns">
      <section class="admin-card admin-list-card" aria-labelledby="adminAttentionTitle">
        <div class="admin-card-head"><h2 id="adminAttentionTitle">먼저 볼 것</h2><span>실패·확인 필요만 모았어요</span></div>
        ${attention.length ? attention.map((item) => `
          <button type="button" class="admin-attention" data-admin-go="${item.go}"${item.kindKey ? ` data-admin-kind="${item.kindKey}" data-admin-filter="fail"` : ""}${item.filter ? ` data-admin-filter="${item.filter}"` : ""}${item.open ? ` data-admin-open="${escapeHtml(item.open)}"` : ""}${item.go === "logs" ? ' data-admin-filter="issue"' : ""}>
            ${statusPill(item.kind, item.tone)}
            <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.why)}</small></span>
            <em>${escapeHtml(item.cta || "결과 보기")} ›</em>
          </button>`).join("") : `<p class="admin-empty">지금 확인할 항목이 없어요.</p>`}
      </section>
      <section class="admin-card" aria-labelledby="adminFailureTitle">
        <div class="admin-card-head"><h2 id="adminFailureTitle">반복 실패 유형</h2><span>최근 정확도 검사 기준</span></div>
        ${failures.length ? barList(failures, "is-danger") : `<p class="admin-empty">최근 정확도 검사에서 반복된 실패가 없어요.</p>`}
      </section>
    </div>`;  renderFreshnessBadge();
}

// ---- 검증 실행 ----------------------------------------------------------

function baseCaseRows() {
  return state.evaluationCases.map((item) => {
    const result = state.evaluationResults.get(item.id);
    const running = state.evaluationRunningCaseId === item.id;
    const status = running ? "running" : result ? (result.passed ? "pass" : "fail") : "wait";
    const expected = Object.entries(item.expected || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
    const details = [
      expected ? { mark: "·", label: "기대 조건", value: expected } : null,
      ...(result?.checks || []).map((check) => ({ mark: check.passed ? "✓" : "✕", label: check.label, value: check.actual })),
    ].filter(Boolean);
    return {
      id: item.id, status, category: item.category, question: item.question,
      duration: formatEvalDuration(result?.durationMs), details, rerun: true,
    };
  });
}

function accuracyCaseRows() {
  const results = state.accuracyLive.length ? state.accuracyLive : state.accuracyReport?.results || [];
  return results.map((result, index) => {
    const dependency = ["dependency_error", "execution_error"].includes(result.outcome);
    const status = result.pending ? "running" : result.passed ? "pass" : dependency ? "error" : "fail";
    const details = [
      result.setup_question ? { mark: "·", label: "1차 질문", value: result.setup_question } : null,
      result.error ? { mark: "✕", label: "오류", value: String(result.error) } : null,
      !result.pending && !result.error ? { mark: result.passed ? "✓" : "✕", label: `기대 (${result.expected_kind || "값"})`, value: accuracyValueLabel(result.expected) } : null,
      !result.pending && !result.error ? { mark: result.passed ? "✓" : "✕", label: "실제", value: accuracyValueLabel(result.actual) } : null,
    ].filter(Boolean);
    return {
      id: `accuracy-${index}`, status, category: [result.source, result.category].filter(Boolean).join(" · "),
      question: result.question || "", duration: "", details, rerun: false,
    };
  });
}

const CASE_STATUS = {
  pass: ["통과", "is-pass"],
  fail: ["실패", "is-fail"],
  error: ["연동 오류", "is-fail"],
  running: ["실행 중", "is-run"],
  wait: ["대기", "is-grey"],
};

function suiteCard(kind) {
  const base = kind === "base";
  const running = base ? state.evaluationRunning : state.accuracyRunning;
  const anyRunning = state.evaluationRunning || state.accuracyRunning;
  let progress = "";
  if (running) {
    const done = (base ? state.evaluationRunProgress?.done : state.accuracyProgress?.done) ?? 0;
    const total = base ? state.evaluationRunProgress?.total : state.accuracyProgress?.total;
    const pct = total ? Math.round((done / total) * 100) : 4;
    const text = total ? `${done}/${total} 검사 중 · 다른 화면으로 옮겨도 계속 실행돼요` : state.accuracyStatusMessage || "검사를 준비하고 있어요";
    progress = `<div class="admin-progress" role="status"><span class="admin-bar"><i style="width:${pct}%"></i></span><em>${escapeHtml(text)}</em></div>`;
  }
  let last = "아직 실행 기록이 없어요";
  if (base && state.evaluationHistory[0]) {
    const run = state.evaluationHistory[0];
    last = `마지막 ${formatRunTime(run.run_at)} · ${run.passed}/${run.total} 통과`;
  } else if (!base && state.accuracyReports[0]) {
    const report = state.accuracyReports[0];
    const summary = report.summary || {};
    const score = summary.score === null || summary.score === undefined ? "점수 없음" : `${summary.score}점`;
    last = `마지막 ${formatRunTime(report.runAt)} · ${score} (${summary.passed ?? 0}/${summary.total ?? 0})${summary.status === "incomplete" ? " · 일부 연동 오류" : ""}`;
  }
  const duration = base ? formatEvalDuration(state.evaluationHistory[0]?.duration_ms) : "";
  const title = base ? "기준 질문 검사" : "답변 정확도 검사";
  const description = base
    ? `미리 정한 ${state.evaluationCases.length}개 질문으로 검색 조건과 원문 조회가 맞는지 봅니다. 답변 문장은 채점하지 않습니다.`
    : "실시간 Mantis·Notion 데이터로 기본 질문 10개와 이전 대화를 잇는 후속 질문 4개를 만들어 실제 답변까지 채점합니다.";
  return `
    <article class="admin-card admin-suite${running ? " is-running" : ""}">
      <div class="admin-suite-head"><div><h2>${title}</h2><p>${escapeHtml(description)}</p></div>
        ${statusPill(base ? (duration ? `지난번 ${duration}` : "빠름") : "수 분 걸림", "is-grey")}</div>
      ${progress}
      <div class="admin-suite-foot"><span>${escapeHtml(last)}</span>
        <button type="button" class="admin-primary" data-admin-run="${kind}"${anyRunning ? " disabled" : ""}>${running ? "실행 중" : "실행"}</button></div>
    </article>`;
}

function renderRunSection() {
  const kind = state.adminResultKind;
  const rows = kind === "base" ? baseCaseRows() : accuracyCaseRows();
  const failed = rows.filter((row) => row.status === "fail" || row.status === "error");
  const passed = rows.filter((row) => row.status === "pass");
  const filter = state.adminResultFilter;
  const order = { fail: 0, error: 0, running: 1, wait: 2, pass: 3 };
  const shown = rows
    .filter((row) => filter === "all" || (filter === "fail" ? failed.includes(row) : passed.includes(row)))
    .sort((a, b) => order[a.status] - order[b.status]);
  const note = kind === "base" && !state.evaluationResults.size && !state.evaluationRunning
    ? `<p class="admin-note">이번 접속에서 아직 실행하지 않았어요. 지난 실행은 점수만 남아 있어 질문별 결과는 ‘실행’ 후에 보입니다.</p>`
    : "";
  const tableRows = shown.map((row) => {
    const [label, tone] = CASE_STATUS[row.status];
    const open = state.adminOpenCase === row.id;
    return `
      <div class="admin-case${open ? " is-open" : ""}">
        <div class="admin-row admin-case-row">
          ${statusPill(label, tone)}
          <span class="admin-muted">${escapeHtml(row.category || "")}</span>
          <button type="button" class="admin-case-toggle" data-admin-case="${escapeHtml(row.id)}" aria-expanded="${open}">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg><span>${escapeHtml(row.question)}</span></button>
          <span class="admin-muted admin-num">${escapeHtml(row.duration)}</span>
          ${row.rerun ? `<button type="button" class="admin-icon-button" data-evaluation-rerun="${escapeHtml(row.id)}"${state.evaluationRunning ? " disabled" : ""} aria-label="이 질문만 다시 실행" title="이 질문만 다시 실행"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/></svg></button>` : "<span></span>"}
        </div>
        ${open ? `<dl class="admin-case-detail">${row.details.length ? row.details.map((detail) => `
          <div><dt><b class="${detail.mark === "✓" ? "is-good" : detail.mark === "✕" ? "is-danger" : ""}" aria-hidden="true">${detail.mark}</b>${escapeHtml(detail.label)}</dt><dd>${escapeHtml(detail.value)}</dd></div>`).join("") : "<div><dd>아직 결과가 없어요.</dd></div>"}</dl>` : ""}
      </div>`;
  }).join("");
  els.adminContent.innerHTML = `
    ${sectionHeader("검증 실행", "두 검사는 확인하는 범위와 걸리는 시간이 다릅니다.")}
    <div class="admin-suites">${suiteCard("base")}${suiteCard("accuracy")}</div>
    <section class="admin-card admin-table" aria-label="검사 결과">
      <div class="admin-table-tools">
        ${chips("kind", [["base", "기준 질문 검사"], ["accuracy", "답변 정확도 검사"]], kind)}
        ${chips("filter", [["all", "전체", rows.length], ["fail", "실패", failed.length], ["pass", "통과", passed.length]], filter)}
      </div>
      ${note}
      <div class="admin-row admin-case-row admin-th" aria-hidden="true"><span>결과</span><span>분류</span><span>질문</span><span class="admin-num">시간</span><span></span></div>
      ${tableRows || `<p class="admin-empty">${rows.length ? "이 조건에 맞는 결과가 없어요." : "아직 실행한 결과가 없어요. 위에서 ‘실행’을 눌러 보세요."}</p>`}
    </section>`;
}

// ---- 답변 피드백 ----------------------------------------------------------

function renderFeedbackSection() {
  const summary = state.feedbackSummary || { total: 0, helpfulCount: 0, needsImprovementCount: 0, helpfulRate: 0, reasonCounts: [], templateCounts: [], recent: [] };
  const header = sectionHeader("답변 피드백", "팀원이 답변 아래에 남긴 ‘도움됨’과 ‘개선 필요’를 모았습니다.");
  if (!summary.total) {
    els.adminContent.innerHTML = `${header}<p class="admin-card admin-empty">아직 등록된 답변 피드백이 없어요. 답변 아래의 ‘도움됨’ 또는 ‘개선 필요’를 누르면 여기에 모입니다.</p>`;
    return;
  }
  const templates = [...(summary.templateCounts || [])].sort((a, b) => (Number(a.helpfulRate) || 0) - (Number(b.helpfulRate) || 0));
  const lowest = templates[0];
  els.adminContent.innerHTML = `
    ${header}
    <div class="admin-metrics">
      <div class="admin-metric"><small>도움됨 비율</small><strong>${summary.helpfulRate || 0}%</strong><span>${summary.total}건 중 ${summary.helpfulCount || 0}건</span></div>
      <div class="admin-metric"><small>개선 필요</small><strong class="${summary.needsImprovementCount ? "is-danger" : ""}">${summary.needsImprovementCount || 0}건</strong><span>사유별로 아래에 나눠 보여 드려요</span></div>
      ${lowest ? `<div class="admin-metric"><small>도움됨이 가장 낮은 템플릿</small><strong class="admin-metric-text">${escapeHtml(teamTemplateLabel(lowest.templateId))}</strong><span>도움됨 ${Number(lowest.helpfulRate) || 0}% · ${Number(lowest.total) || 0}건</span></div>` : ""}
    </div>
    <div class="admin-columns is-even">
      <section class="admin-card"><div class="admin-card-head"><h2>개선 필요 사유</h2></div>
        ${barList((summary.reasonCounts || []).map((item) => [item.label, item.count]))}</section>
      <section class="admin-card admin-list-card"><div class="admin-card-head"><h2>템플릿별 만족도</h2></div>
        ${templates.length ? templates.map((item) => `<div class="admin-row admin-template-row"><span>${escapeHtml(teamTemplateLabel(item.templateId))}</span><span class="admin-muted admin-num">${Number(item.total) || 0}건</span><strong class="admin-num${(Number(item.helpfulRate) || 0) < 70 ? " is-danger" : ""}">${Number(item.helpfulRate) || 0}%</strong></div>`).join("") : `<p class="admin-empty">템플릿으로 한 질문의 피드백이 아직 없어요.</p>`}</section>
    </div>
    <section class="admin-card admin-list-card"><div class="admin-card-head"><h2>최근 피드백</h2></div>
      ${(summary.recent || []).map((item) => `
        <article class="admin-row admin-feedback-row">
          ${item.rating === "helpful" ? statusPill("도움됨", "is-pass") : statusPill("개선 필요", "is-fail")}
          <span><strong>${escapeHtml(item.question || "")}</strong><small>${escapeHtml(item.comment || item.reasonLabel || "추가 의견 없음")} · ${escapeHtml(teamTemplateLabel(item.templateId))}</small></span>
          <time class="admin-muted admin-num">${escapeHtml(formatRunTime(item.updatedAt))}</time>
        </article>`).join("")}
    </section>`;
}

// ---- 감사 로그 ----------------------------------------------------------

function renderLogsSection() {
  const summary = state.validationSummary || {};
  const logs = state.validationLogs;
  const issueLogs = logs.filter((log) => log.status !== "pass");
  const shown = state.adminLogFilter === "issue" ? issueLogs : logs;
  const counts = summary.statusCounts || {};
  const latency = summary.latency || {};
  const latencyLabels = { beforeFinalizationMs: "답변 준비 전체", verificationAndFormattingMs: "검증·서식", "stage:context": "문맥", "stage:planning": "계획 전체", "stage:status_resolution": "상태 해석", "stage:query_planner": "일반 플래너", "stage:reports": "리포트", "stage:count": "집계", "stage:store_read": "저장본 읽기", "stage:filter": "필터", "stage:retrieval": "검색", "stage:generation": "답변 생성" };
  els.adminContent.innerHTML = `
    ${sectionHeader("감사 로그", `답변마다 자동으로 채점한 기록입니다. 최근 ${logs.length}건을 보여 드려요.`, chips("log", [["all", "전체", logs.length], ["issue", "실패·확인 필요", issueLogs.length]], state.adminLogFilter))}
    ${summary.total ? `<div class="admin-metrics">
      <div class="admin-metric"><small>평균 점수</small><strong>${summary.averageScore ?? "—"}점</strong><span>최근 ${summary.total}건 기준</span></div>
      <div class="admin-metric"><small>통과</small><strong>${counts.pass || 0}건</strong><span>${Math.round(((counts.pass || 0) / summary.total) * 100)}%</span></div>
      <div class="admin-metric"><small>확인 필요</small><strong class="${counts.warning ? "is-warning" : ""}">${counts.warning || 0}건</strong><span>${Math.round(((counts.warning || 0) / summary.total) * 100)}%</span></div>
      <div class="admin-metric"><small>실패</small><strong class="${counts.fail ? "is-danger" : ""}">${counts.fail || 0}건</strong><span>${Math.round(((counts.fail || 0) / summary.total) * 100)}%</span></div>
    </div>` : ""}
    <section class="admin-card" aria-label="단계별 응답 시간">
      <div class="admin-card-head"><h2>단계별 응답 시간</h2><span>${Number(latency.sampleCount) || 0}건 측정</span></div>
      ${Object.entries(latency.groups?.all || {}).map(([key, value]) => `<div class="admin-row"><strong>${escapeHtml(latencyLabels[key] || key)}</strong><span>중앙값 ${Number(value.medianMs).toLocaleString()} ms · p95 ${Number(value.p95Ms).toLocaleString()} ms · ${Number(value.count)}건</span></div>`).join("") || '<p class="admin-empty">아직 측정된 응답이 없어요.</p>'}
      <p class="admin-muted">${escapeHtml(latency.note || "단계 시간은 중첩되어 합산하지 않습니다.")}</p>
    </section>
    <section class="admin-card admin-table" aria-label="감사 로그 목록">
      <div class="admin-row admin-log-row admin-th" aria-hidden="true"><span>시각</span><span>답변 방식</span><span>질문</span><span class="admin-num">점수</span><span>상태</span></div>
      ${shown.length ? shown.map((log) => {
        const [label, tone] = LOG_STATUS[log.status] || [log.status, "is-grey"];
        return `<div class="admin-row admin-log-row"><time class="admin-muted admin-num">${escapeHtml(formatRunTime(log.created_at))}</time><span class="admin-muted">${escapeHtml(modeLabel(log.mode))}</span><span class="admin-ellipsis" title="${escapeHtml(log.question)}">${escapeHtml(log.question)}</span><span class="admin-num">${escapeHtml(String(log.score))}점</span>${statusPill(label, tone)}</div>`;
      }).join("") : `<p class="admin-empty">${logs.length ? "실패하거나 확인이 필요한 답변이 없어요." : "아직 저장된 답변 로그가 없어요."}</p>`}
    </section>
    ${summary.total ? `<div class="admin-columns is-three">
      <section class="admin-card"><div class="admin-card-head"><h2>답변 방식별</h2></div>${barList(Object.entries(summary.modeCounts || {}).map(([mode, count]) => [modeLabel(mode), count]))}</section>
      <section class="admin-card"><div class="admin-card-head"><h2>모델별</h2></div>${barList(Object.entries(summary.modelCounts || {}))}</section>
      <section class="admin-card"><div class="admin-card-head"><h2>자주 실패하는 검사 항목</h2></div>${barList((summary.topFailingChecks || []).map((item) => [item.label, item.count]), "is-danger")}</section>
    </div>` : ""}`;
}

// ---- 섹션 전환 ----------------------------------------------------------

function syncAdminBadges() {
  const count = pendingAdminUsers().length;
  for (const badge of [els.adminPendingBadge, els.adminNavPendingBadge]) {
    if (!badge) continue;
    badge.hidden = !count;
    badge.textContent = String(count);
  }
  if (els.adminPendingBadge) els.adminPendingBadge.title = count ? `승인 대기 ${count}명` : "";
}

function renderEvaluationContent() {
  if (!els.adminContent) return;
  if (state.adminSection === "users" && !usersSectionAvailable()) state.adminSection = "overview";
  els.adminPanel.querySelectorAll("[data-admin-section]").forEach((button) => {
    const current = button.dataset.adminSection === state.adminSection;
    button.hidden = button.dataset.adminSection === "users" && !usersSectionAvailable();
    if (current) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  syncAdminBadges();
  if (!state.adminLoaded) {
    els.adminContent.innerHTML = `<p class="admin-empty">관리자 정보를 불러오는 중입니다.</p>`;
    return;
  }
  if (state.adminSection === "run") renderRunSection();
  else if (state.adminSection === "feedback") renderFeedbackSection();
  else if (state.adminSection === "logs") renderLogsSection();
  else if (state.adminSection === "users") renderUsersSection(els.adminContent);
  else renderOverview();
}

function showAdminSection(section, { filter, kind, open } = {}) {
  state.adminSection = ADMIN_SECTIONS.includes(section) ? section : "overview";
  if (state.adminSection === "run") {
    if (kind) state.adminResultKind = kind;
    if (filter) state.adminResultFilter = filter;
    state.adminOpenCase = open || "";
  }
  if (state.adminSection === "logs" && filter) state.adminLogFilter = filter;
  if (state.adminSection === "users" && filter) state.adminUserFilter = filter;
  renderEvaluationContent();
  els.adminContent.scrollTop = 0;
  els.adminContent.focus({ preventScroll: true });
}

// 화면 탭의 승인 대기 배지만 먼저 채운다. 품질 데이터는 관리자 센터를 열 때 불러온다.
function preloadAdminBadges() {
  if (!usersSectionAvailable()) return;
  loadAdminUsers().then(syncAdminBadges);
}

let adminLoadPromise = null;

function loadAdminCenter() {
  if (adminLoadPromise) return adminLoadPromise;
  renderEvaluationContent();
  adminLoadPromise = Promise.all([loadEvaluationData(), loadAdminUsers()])
    .then(() => {
      state.adminLoaded = true;
      renderEvaluationContent();
    })
    .catch((error) => {
      els.adminContent.innerHTML = `<div class="admin-card admin-empty"><strong>관리자 정보를 불러오지 못했어요.</strong><p>${escapeHtml(error.message)}</p><button type="button" class="admin-secondary" data-admin-retry>다시 시도</button></div>`;
    })
    .finally(() => {
      adminLoadPromise = null;
    });
  return adminLoadPromise;
}

// ---- 검증 실행 스트림 ----------------------------------------------------

async function readEventStream(response, onEvent) {
  const reader = response.body?.getReader();
  if (!reader) throw new Error("스트리밍 응답을 읽을 수 없습니다.");
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";
    for (const frame of frames) {
      const payload = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("");
      if (payload) onEvent(JSON.parse(payload));
    }
    if (done) break;
  }
}

function rerenderRunSection() {
  if (state.adminSection === "run" || state.adminSection === "overview") renderEvaluationContent();
}

async function runEvaluations(caseIds = []) {
  if (state.evaluationRunning) return;
  state.evaluationRunning = true;
  state.evaluationRunningCaseId = null;
  state.evaluationRunProgress = { done: 0, total: caseIds.length || state.evaluationCases.length };
  // 전체 실행은 지난 결과를 비우고 시작하고, 케이스 재실행은 기존 결과 위에 덮어쓴다.
  if (!caseIds.length) state.evaluationResults = new Map();
  rerenderRunSection();
  try {
    const response = await apiFetch("/api/evaluation/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ caseIds, stream: true }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || "검증 실행에 실패했습니다.");
    }
    await readEventStream(response, (event) => {
      if (event.type === "case_start") {
        state.evaluationRunningCaseId = event.caseId;
      } else if (event.type === "case_result") {
        state.evaluationResults.set(event.result.id, event.result);
        state.evaluationRunningCaseId = null;
        state.evaluationRunProgress.done += 1;
      } else if (event.type === "summary") {
        state.evaluationRunningCaseId = null;
      }
      rerenderRunSection();
    });
    state.evaluationRunning = false;
    await refreshEvaluationHistory();
    if (!caseIds.length) {
      const failed = [...state.evaluationResults.values()].filter((result) => !result.passed).length;
      state.adminResultKind = "base";
      state.adminResultFilter = failed ? "fail" : "all";
      showToast(failed ? `기준 질문 검사가 끝났어요. 실패 ${failed}개를 모아 보여 드려요.` : "기준 질문 검사가 끝났어요. 모두 통과했어요.", failed ? "warning" : "success");
    }
  } catch (error) {
    showToast(`검증 실행 오류: ${error.message}`, "warning");
  } finally {
    state.evaluationRunning = false;
    state.evaluationRunningCaseId = null;
    rerenderRunSection();
  }
}

async function runAccuracyCheck() {
  if (state.accuracyRunning) return;
  state.accuracyRunning = true;
  state.accuracyLive = [];
  state.accuracyProgress = { done: 0, total: null };
  state.accuracyStatusMessage = "정확도 검사를 준비하고 있어요";
  state.adminResultKind = "accuracy";
  rerenderRunSection();
  try {
    const response = await apiFetch("/api/accuracy/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ questions: 10, followUpScenarios: 4 }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || "정확도 검사 실행에 실패했습니다.");
    }
    await readEventStream(response, (event) => {
      if (event.type === "status") {
        state.accuracyStatusMessage = event.message || "";
      } else if (event.type === "run_start") {
        state.accuracyProgress.total = event.total;
      } else if (event.type === "case_start") {
        state.accuracyLive.push({
          pending: true,
          question: event.question,
          setup_question: event.setupQuestion,
          source: event.source,
          category: event.category,
          case_group: event.caseGroup,
        });
      } else if (event.type === "case_result") {
        const pendingIndex = state.accuracyLive.findIndex((entry) => entry.pending);
        if (pendingIndex >= 0) state.accuracyLive[pendingIndex] = event.result;
        else state.accuracyLive.push(event.result);
        state.accuracyProgress.done += 1;
      } else if (event.type === "error") {
        throw new Error(event.message || "정확도 검사 중 오류가 발생했습니다.");
      }
      rerenderRunSection();
    });
    state.accuracyRunning = false;
    await refreshAccuracyReports();
    const failed = state.accuracyLive.filter((result) => !result.passed).length;
    state.adminResultFilter = failed ? "fail" : "all";
    showToast(failed ? `답변 정확도 검사가 끝났어요. 실패 ${failed}개를 모아 보여 드려요.` : "답변 정확도 검사가 끝났어요. 모두 통과했어요.", failed ? "warning" : "success");
  } catch (error) {
    showToast(`정확도 검사 오류: ${error.message}`, "warning");
  } finally {
    state.accuracyRunning = false;
    rerenderRunSection();
  }
}

function handleAdminClick(event) {
  const section = event.target.closest("[data-admin-section]");
  if (section) return showAdminSection(section.dataset.adminSection);
  const go = event.target.closest("[data-admin-go]");
  if (go) {
    return showAdminSection(go.dataset.adminGo, {
      kind: go.dataset.adminKind, filter: go.dataset.adminFilter, open: go.dataset.adminOpen,
    });
  }
  const run = event.target.closest("[data-admin-run]");
  if (run && !run.disabled) return run.dataset.adminRun === "accuracy" ? runAccuracyCheck() : runEvaluations();
  const rerun = event.target.closest("[data-evaluation-rerun]");
  if (rerun && !rerun.disabled) return runEvaluations([rerun.dataset.evaluationRerun]);
  const kind = event.target.closest("[data-admin-kind]");
  if (kind) {
    state.adminResultKind = kind.dataset.adminKind;
    state.adminOpenCase = "";
    return renderEvaluationContent();
  }
  const filter = event.target.closest("[data-admin-filter]");
  if (filter) {
    state.adminResultFilter = filter.dataset.adminFilter;
    return renderEvaluationContent();
  }
  const log = event.target.closest("[data-admin-log]");
  if (log) {
    state.adminLogFilter = log.dataset.adminLog;
    return renderEvaluationContent();
  }
  const toggle = event.target.closest("[data-admin-case]");
  if (toggle) {
    const id = toggle.dataset.adminCase;
    state.adminOpenCase = state.adminOpenCase === id ? "" : id;
    renderEvaluationContent();
    els.adminContent.querySelector(`[data-admin-case="${CSS.escape(id)}"]`)?.focus();
    return;
  }
  if (event.target.closest("[data-admin-retry]")) loadAdminCenter();
}

function initAdminCenter() {
  if (!els.adminPanel) return;
  els.adminPanel.querySelector(".admin-nav").addEventListener("click", handleAdminClick);
  els.adminContent.addEventListener("click", handleAdminClick);
  initAdminUsers({ onChange: renderEvaluationContent });
  setMainViewLoader("admin", loadAdminCenter);
}

export {
  initAdminCenter,
  loadAdminCenter,
  loadEvaluationData,
  preloadAdminBadges,
  refreshFeedbackSummary,
  renderEvaluationContent,
  runAccuracyCheck,
  runEvaluations,
  showAdminSection,
  modeLabel,
  MODE_LABELS,
};
