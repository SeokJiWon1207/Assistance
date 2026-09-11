import { state, els, apiFetch, ADMIN_ENDPOINTS } from "./runtime.js?v=20260911-6";
import { statusLabel, TEAM_TEMPLATES, escapeHtml } from "./chat.js?v=20260911-6";

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
  const casesData = await casesResponse.json();
  const logsData = await logsResponse.json();
  const summaryData = await summaryResponse.json();
  state.evaluationCases = casesData.cases || [];
  state.validationLogs = logsData.logs || [];
  state.validationSummary = summaryData;
  // 실행 이력은 부가 정보라 실패해도 모달 자체는 열리게 둔다.
  state.evaluationHistory = historyResponse.ok ? (await historyResponse.json()).runs || [] : [];
  state.feedbackSummary = feedbackResponse.ok ? await feedbackResponse.json() : null;
  await refreshAccuracyReports();
  renderEvaluationContent();
}

async function refreshFeedbackSummary() {
  try {
    const response = await apiFetch("/api/feedback/summary?limit=200");
    if (!response.ok) return;
    state.feedbackSummary = await response.json();
    if (state.evaluationTab === "feedback") renderEvaluationContent();
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
    const response = await apiFetch("/api/evaluation/history?limit=8");
    if (response.ok) state.evaluationHistory = (await response.json()).runs || [];
  } catch {
    // 이력 갱신 실패는 치명적이지 않다.
  }
}

function renderEvaluationContent() {
  document.querySelectorAll(".evaluation-tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.evaluationTab === state.evaluationTab);
  });
  els.runEvaluationBtn.hidden = state.evaluationTab !== "cases";
  els.evaluationContent.innerHTML = "";
  if (state.evaluationTab === "logs") {
    renderValidationLogs();
    return;
  }
  if (state.evaluationTab === "summary") {
    renderValidationSummary();
    return;
  }
  if (state.evaluationTab === "accuracy") {
    renderAccuracyTab();
    return;
  }
  if (state.evaluationTab === "feedback") {
    renderFeedbackTab();
    return;
  }
  renderEvaluationCases();
}

function formatEvalDuration(ms) {
  if (!Number.isFinite(ms)) return "";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}초`;
}

function renderEvaluationHistory() {
  if (!state.evaluationHistory.length) return;
  const history = document.createElement("div");
  history.className = "evaluation-history";
  const label = document.createElement("span");
  label.className = "evaluation-history-label";
  label.textContent = "최근 실행";
  history.appendChild(label);
  state.evaluationHistory.forEach((run) => {
    const chip = document.createElement("span");
    chip.className = `evaluation-history-chip ${run.failed ? "history-fail" : "history-pass"}`;
    const when = String(run.run_at || "").replace("T", " ").slice(5, 16);
    chip.title = `${when} · ${run.passed}/${run.total} 통과 · ${formatEvalDuration(run.duration_ms)}`;
    chip.textContent = `${run.score}점 (${run.passed}/${run.total})`;
    history.appendChild(chip);
  });
  els.evaluationContent.appendChild(history);
}

function renderEvaluationCases() {
  if (!state.evaluationCases.length) {
    els.evaluationContent.textContent = "등록된 검증 질문이 없습니다.";
    return;
  }
  const passed = [...state.evaluationResults.values()].filter((result) => result.passed).length;
  const totalRun = state.evaluationResults.size;
  if (state.evaluationRunning) {
    const done = state.evaluationRunProgress?.done ?? 0;
    const total = state.evaluationRunProgress?.total ?? state.evaluationCases.length;
    els.evaluationSummary.innerHTML = `<strong>${done}/${total}</strong> 검사 완료 · 실행 중입니다`;
  } else if (totalRun) {
    els.evaluationSummary.innerHTML = `<strong>${passed}/${totalRun}</strong> 통과 · 최근 실행 결과`;
  } else {
    els.evaluationSummary.innerHTML = `<strong>${state.evaluationCases.length}</strong>개 기준 질문 · 검색 조건과 원문 조회를 검사합니다.`;
  }
  renderEvaluationHistory();
  state.evaluationCases.forEach((item) => {
    const result = state.evaluationResults.get(item.id);
    const running = state.evaluationRunningCaseId === item.id;
    const card = document.createElement("article");
    card.className = `evaluation-case ${running ? "case-running" : result ? (result.passed ? "case-pass" : "case-fail") : ""}`;
    const expected = Object.entries(item.expected || {})
      .map(([key, value]) => `${key}: ${value}`)
      .join(" · ");
    const checks = result?.checks?.map((check) => `
      <li class="${check.passed ? "passed" : "failed"}">
        <strong>${escapeHtml(check.label)}</strong><span>${escapeHtml(check.actual)}</span>
      </li>
    `).join("") || "";
    const statusText = running ? "실행 중" : result ? (result.passed ? "통과" : "실패") : "대기";
    const duration = formatEvalDuration(result?.durationMs);
    card.innerHTML = `
      <div class="evaluation-case-top">
        <span>${escapeHtml(item.category)}</span>
        <span class="evaluation-case-tools">
          ${duration ? `<small class="evaluation-case-duration">${escapeHtml(duration)}</small>` : ""}
          <button type="button" class="evaluation-rerun" data-evaluation-rerun="${escapeHtml(item.id)}" ${state.evaluationRunning ? "disabled" : ""} title="이 질문만 다시 실행">재실행</button>
          <em>${statusText}</em>
        </span>
      </div>
      <h4>${escapeHtml(item.question)}</h4>
      <p>${escapeHtml(expected)}</p>
      ${checks ? `<ul>${checks}</ul>` : ""}
    `;
    els.evaluationContent.appendChild(card);
  });
}

function accuracyValueLabel(value) {
  if (value === null || value === undefined) return "없음";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

function renderAccuracyInsights() {
  const analytics = state.accuracyAnalytics || {};
  const freshness = state.dataFreshness;
  const trend = (analytics.trend || []).slice(-8);
  const failures = (analytics.failureCounts || []).slice(0, 5);
  if (!freshness && !trend.length && !failures.length) return;

  const panel = document.createElement("section");
  panel.className = "accuracy-insights";
  const ageText = freshness?.ageDays === null || freshness?.ageDays === undefined
    ? "기준일 미확인"
    : freshness.ageDays === 0
      ? "오늘 기준"
      : `${freshness.ageDays}일 지연`;
  const freshnessHtml = freshness
    ? `<article class="accuracy-insight-card freshness-${escapeHtml(freshness.status || "unknown")}">
        <span>데이터 신선도</span>
        <strong>${escapeHtml(freshness.label || "확인 필요")}</strong>
        <small>${escapeHtml(freshness.asOf || "기준일 없음")} · ${escapeHtml(ageText)}</small>
      </article>`
    : "";
  const delta = analytics.latestDelta;
  const deltaText = delta === null || delta === undefined
    ? "비교 이력 없음"
    : `직전 대비 ${delta > 0 ? "+" : ""}${delta}점`;
  const trendHtml = trend.length
    ? `<article class="accuracy-insight-card accuracy-trend-card">
        <span>정확도 추세</span>
        <strong>${escapeHtml(deltaText)}</strong>
        <div class="accuracy-trend-bars" aria-label="최근 정확도 추세">
          ${trend.map((item) => {
            const available = item.score !== null && item.score !== undefined;
            const score = available ? Number(item.score) || 0 : 0;
            const label = available ? `${score}점` : "불완전";
            return `<i style="--score:${Math.max(4, score)}%" title="${escapeHtml(String(item.runAt || ""))}: ${escapeHtml(label)}"><b>${available ? score : "N/A"}</b></i>`;
          }).join("")}
        </div>
      </article>`
    : "";
  const failureHtml = `<article class="accuracy-insight-card accuracy-failure-card">
      <span>반복 실패 유형</span>
      ${failures.length
        ? `<ol>${failures.map((item) => `<li><b>${escapeHtml(item.category)}</b><em>${Number(item.count) || 0}회</em></li>`).join("")}</ol>`
        : "<strong>최근 실패 없음</strong><small>조회한 리포트 범위 기준</small>"}
    </article>`;
  panel.innerHTML = `${freshnessHtml}${trendHtml}${failureHtml}`;
  els.evaluationContent.appendChild(panel);
}

function renderAccuracyTab() {
  if (state.accuracyRunning) {
    const done = state.accuracyProgress?.done ?? 0;
    const total = state.accuracyProgress?.total;
    els.evaluationSummary.innerHTML = total
      ? `<strong>${done}/${total}</strong> 질문 검사 완료 · 실행 중입니다`
      : escapeHtml(state.accuracyStatusMessage || "정확도 검증을 준비하고 있습니다");
  } else if (state.accuracyLive.length || state.accuracyReport) {
    const summary = state.accuracyLive.length
      ? {
          passed: state.accuracyLive.filter((result) => result.passed).length,
          total: state.accuracyLive.filter((result) => !result.pending).length,
          base: {
            passed: state.accuracyLive.filter((result) => result.case_group === "base" && result.passed).length,
            total: state.accuracyLive.filter((result) => result.case_group === "base" && !result.pending).length,
          },
          follow_up: {
            passed: state.accuracyLive.filter((result) => result.case_group === "follow_up" && result.passed).length,
            total: state.accuracyLive.filter((result) => result.case_group === "follow_up" && !result.pending).length,
          },
        }
      : state.accuracyReport.summary || {};
    const incomplete = summary.status === "incomplete";
    const groupSummary = summary.base && summary.follow_up
      ? ` · 기본 ${summary.base.passed ?? 0}/${summary.base.total ?? 0} · 후속 ${summary.follow_up.passed ?? 0}/${summary.follow_up.total ?? 0}`
      : "";
    els.evaluationSummary.innerHTML = `<strong>${summary.passed ?? 0}/${summary.total ?? 0}</strong> 통과${groupSummary} · ${incomplete ? "연동 오류로 일부 문항은 정확도에서 제외됨" : "실제 답변까지 채점한 결과"}`;
  } else {
    els.evaluationSummary.innerHTML = "실시간 데이터로 질문을 만들어 실제 답변까지 채점합니다.";
  }

  const toolbar = document.createElement("div");
  toolbar.className = "accuracy-toolbar";
  const desc = document.createElement("p");
  desc.className = "accuracy-desc";
  desc.textContent =
    "실시간 Mantis·Notion 데이터로 기본 질문 10개와 실제 대화 문맥을 잇는 후속 질문 4개를 채점합니다. 수 분이 걸릴 수 있습니다.";
  const runButton = document.createElement("button");
  runButton.type = "button";
  runButton.className = "run-evaluation-button";
  runButton.dataset.accuracyRun = "true";
  runButton.textContent = state.accuracyRunning ? "정확도 검증 실행 중" : "정확도 검증 실행";
  runButton.disabled = state.accuracyRunning;
  toolbar.append(desc, runButton);
  els.evaluationContent.appendChild(toolbar);

  renderAccuracyInsights();

  if (state.accuracyReports.length) {
    const history = document.createElement("div");
    history.className = "evaluation-history";
    const label = document.createElement("span");
    label.className = "evaluation-history-label";
    label.textContent = "최근 리포트";
    history.appendChild(label);
    state.accuracyReports.forEach((report) => {
      const summary = report.summary || {};
      const chip = document.createElement("span");
      const incomplete = summary.status === "incomplete" || (summary.dependency_errors ?? 0) > 0;
      chip.className = `evaluation-history-chip ${summary.failed || incomplete || (report.errors || []).length ? "history-fail" : "history-pass"}`;
      const when = String(report.runAt || "").replace("T", " ").slice(5, 16);
      const scoreLabel = summary.score === null || summary.score === undefined ? "N/A" : `${summary.score}점`;
      chip.title = `${when} · ${summary.passed ?? 0}/${summary.total ?? 0} 통과${incomplete || (report.errors || []).length ? " · 실행 불완전" : ""}`;
      chip.textContent = `${scoreLabel} (${summary.passed ?? 0}/${summary.total ?? 0})`;
      history.appendChild(chip);
    });
    els.evaluationContent.appendChild(history);
  }

  const results = state.accuracyLive.length ? state.accuracyLive : state.accuracyReport?.results || [];
  if (!results.length && !state.accuracyRunning) {
    const empty = document.createElement("p");
    empty.className = "accuracy-empty";
    empty.textContent = "아직 실행된 정확도 검증이 없습니다. 위 버튼으로 첫 검증을 실행해 보세요.";
    els.evaluationContent.appendChild(empty);
    return;
  }
  results.forEach((result) => {
    const card = document.createElement("article");
    card.className = `evaluation-case ${result.pending ? "case-running" : result.passed ? "case-pass" : "case-fail"}`;
    const statusText = result.pending
      ? "실행 중"
      : result.passed
        ? "통과"
        : ["dependency_error", "execution_error"].includes(result.outcome)
          ? "연동 오류"
          : "실패";
    const meta = [result.source, result.category].filter(Boolean).join(" · ");
    const setup = result.setup_question
      ? `<p class="accuracy-setup-question"><strong>1차 질문</strong> ${escapeHtml(result.setup_question)}</p>`
      : "";
    const detail = result.pending
      ? ""
      : result.error
        ? `<li class="failed"><strong>오류</strong><span>${escapeHtml(String(result.error))}</span></li>`
        : `
          <li class="${result.passed ? "passed" : "failed"}">
            <strong>기대 (${escapeHtml(String(result.expected_kind || ""))})</strong>
            <span>${escapeHtml(accuracyValueLabel(result.expected))}</span>
          </li>
          <li class="${result.passed ? "passed" : "failed"}">
            <strong>실제</strong>
            <span>${escapeHtml(accuracyValueLabel(result.actual))}</span>
          </li>
        `;
    card.innerHTML = `
      <div class="evaluation-case-top">
        <span>${escapeHtml(meta)}</span>
        <em>${statusText}</em>
      </div>
      ${setup}
      <h4>${escapeHtml(result.question || "")}</h4>
      ${detail ? `<ul>${detail}</ul>` : ""}
    `;
    els.evaluationContent.appendChild(card);
  });
}

async function runAccuracyCheck() {
  if (state.accuracyRunning) return;
  state.accuracyRunning = true;
  state.accuracyLive = [];
  state.accuracyProgress = { done: 0, total: null };
  state.accuracyStatusMessage = "정확도 검증을 준비하고 있습니다";
  if (state.evaluationTab === "accuracy") renderEvaluationContent();
  try {
    const response = await apiFetch("/api/accuracy/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ questions: 10, followUpScenarios: 4 }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || "정확도 검증 실행에 실패했습니다.");
    }
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
        if (!payload) continue;
        const event = JSON.parse(payload);
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
          throw new Error(event.message || "정확도 검증 중 오류가 발생했습니다.");
        }
        if (state.evaluationTab === "accuracy") renderEvaluationContent();
      }
      if (done) break;
    }
    state.accuracyRunning = false;
    await refreshAccuracyReports();
    if (state.evaluationTab === "accuracy") renderEvaluationContent();
  } catch (error) {
    state.accuracyRunning = false;
    if (state.evaluationTab === "accuracy") renderEvaluationContent();
    els.evaluationSummary.textContent = `오류: ${error.message}`;
  } finally {
    state.accuracyRunning = false;
  }
}

function renderValidationLogs() {
  els.evaluationSummary.innerHTML = `<strong>${state.validationLogs.length}</strong>개 최근 감사 로그`;
  if (!state.validationLogs.length) {
    els.evaluationContent.textContent = "아직 저장된 답변 로그가 없습니다.";
    return;
  }
  state.validationLogs.forEach((log) => {
    const row = document.createElement("article");
    row.className = `validation-log log-${log.status}`;
    row.innerHTML = `
      <div>
        <span>${escapeHtml(log.mode)}</span>
        <time>${escapeHtml(String(log.created_at || "").replace("T", " ").slice(0, 19))}</time>
      </div>
      <h4>${escapeHtml(log.question)}</h4>
      <strong>${escapeHtml(String(log.score))}점</strong>
    `;
    els.evaluationContent.appendChild(row);
  });
}

function teamTemplateLabel(templateId) {
  return TEAM_TEMPLATES.find((template) => template.id === templateId)?.title || templateId || "일반 질문";
}

function renderFeedbackTab() {
  const summary = state.feedbackSummary || {
    total: 0,
    helpfulCount: 0,
    needsImprovementCount: 0,
    helpfulRate: 0,
    reasonCounts: [],
    templateCounts: [],
    recent: [],
  };
  els.evaluationSummary.innerHTML = `<strong>${summary.total || 0}</strong>건 피드백 · 도움됨 <strong>${summary.helpfulRate || 0}%</strong>`;

  const wrap = document.createElement("div");
  wrap.className = "validation-summary feedback-dashboard";
  const statCards = document.createElement("div");
  statCards.className = "summary-stat-grid";
  [
    { label: "도움됨", value: summary.helpfulCount || 0, tone: "pass" },
    { label: "개선 필요", value: summary.needsImprovementCount || 0, tone: "warning" },
    { label: "도움됨 비율", value: `${summary.helpfulRate || 0}%`, tone: "neutral" },
  ].forEach(({ label, value, tone }) => {
    const card = document.createElement("div");
    card.className = `summary-stat-card tone-${tone}`;
    card.innerHTML = `<strong>${escapeHtml(String(value))}</strong><span>${escapeHtml(label)}</span>`;
    statCards.appendChild(card);
  });
  wrap.appendChild(statCards);

  if (!summary.total) {
    const empty = document.createElement("p");
    empty.className = "accuracy-empty";
    empty.textContent = "아직 등록된 답변 피드백이 없습니다. 실제 답변 아래의 ‘도움됨’ 또는 ‘개선 필요’를 선택하면 여기에 집계됩니다.";
    wrap.appendChild(empty);
    els.evaluationContent.appendChild(wrap);
    return;
  }

  const reasons = summary.reasonCounts || [];
  const reasonSection = document.createElement("section");
  reasonSection.className = "summary-section";
  reasonSection.innerHTML = "<h4>개선 필요 사유 6개</h4>";
  const reasonList = document.createElement("div");
  reasonList.className = "summary-bar-list";
  const maxReasonCount = Math.max(1, ...reasons.map((item) => item.count || 0));
  reasons.forEach((item) => {
    const row = document.createElement("div");
    row.className = "summary-bar-row";
    const width = Math.round(((item.count || 0) / maxReasonCount) * 100);
    row.innerHTML = `
      <span class="summary-bar-label">${escapeHtml(item.label)}</span>
      <span class="summary-bar-track"><span class="summary-bar-fill" style="width:${width}%"></span></span>
      <span class="summary-bar-count">${Number(item.count) || 0}</span>
    `;
    reasonList.appendChild(row);
  });
  reasonSection.appendChild(reasonList);
  wrap.appendChild(reasonSection);

  if (summary.templateCounts?.length) {
    const templateSection = document.createElement("section");
    templateSection.className = "summary-section";
    templateSection.innerHTML = "<h4>팀 템플릿별 만족도</h4>";
    const templateList = document.createElement("div");
    templateList.className = "feedback-template-list";
    summary.templateCounts.forEach((item) => {
      const row = document.createElement("div");
      row.className = "feedback-template-row";
      row.innerHTML = `
        <strong>${escapeHtml(teamTemplateLabel(item.templateId))}</strong>
        <span>${Number(item.total) || 0}건</span>
        <em>도움됨 ${Number(item.helpfulRate) || 0}%</em>
      `;
      templateList.appendChild(row);
    });
    templateSection.appendChild(templateList);
    wrap.appendChild(templateSection);
  }

  const recentSection = document.createElement("section");
  recentSection.className = "summary-section";
  recentSection.innerHTML = "<h4>최근 피드백</h4>";
  const recentList = document.createElement("div");
  recentList.className = "feedback-recent-list";
  (summary.recent || []).forEach((item) => {
    const row = document.createElement("article");
    row.className = `feedback-log feedback-${item.rating}`;
    const statusLabel = item.rating === "helpful" ? "도움됨" : "개선 필요";
    const detail = item.comment || item.reasonLabel || "추가 의견 없음";
    row.innerHTML = `
      <div><span>${escapeHtml(statusLabel)}</span><time>${escapeHtml(String(item.updatedAt || "").replace("T", " ").slice(0, 16))}</time></div>
      <h4>${escapeHtml(item.question || "")}</h4>
      <p>${escapeHtml(detail)}</p>
      <small>${escapeHtml(teamTemplateLabel(item.templateId))} · LOG ${escapeHtml(String(item.auditId || "").slice(0, 8))}</small>
    `;
    recentList.appendChild(row);
  });
  recentSection.appendChild(recentList);
  wrap.appendChild(recentSection);
  els.evaluationContent.appendChild(wrap);
}

const MODE_LABELS = {
  model: "모델 답변",
  deterministic_issue_list: "Mantis 목록",
  deterministic_notion_list: "Notion 목록",
  deterministic_effort_report: "공수 리포트",
  deterministic_mantis_month_count: "월별 건수",
  deterministic_combined_search: "통합 검색",
  notion_update_missing_fields: "Notion 수정(필드 누락)",
  notion_update_missing_target: "Notion 수정(대상 못 찾음)",
  notion_update_ambiguous: "Notion 수정(대상 모호)",
  notion_update: "Notion 수정",
  notion_create: "Notion 생성",
  model_timeout_fallback: "모델 타임아웃 폴백",
};

function modeLabel(mode) {
  return MODE_LABELS[mode] || mode || "알 수 없음";
}

function renderValidationSummary() {
  const summary = state.validationSummary;
  if (!summary || !summary.total) {
    els.evaluationSummary.innerHTML = `<strong>0</strong>개 감사 로그`;
    els.evaluationContent.textContent = "아직 집계할 감사 로그가 없습니다.";
    return;
  }

  const { total, averageScore, statusCounts, modeCounts, modelCounts, topFailingChecks, recentIssues } = summary;
  els.evaluationSummary.innerHTML = `<strong>${total}</strong>건 집계 · 평균 <strong>${averageScore}</strong>점`;

  const wrap = document.createElement("div");
  wrap.className = "validation-summary";

  const statCards = document.createElement("div");
  statCards.className = "summary-stat-grid";
  const statEntries = [
    { label: "통과", value: statusCounts.pass || 0, tone: "pass" },
    { label: "확인 필요", value: statusCounts.warning || 0, tone: "warning" },
    { label: "실패", value: statusCounts.fail || 0, tone: "fail" },
  ];
  statEntries.forEach(({ label, value, tone }) => {
    const card = document.createElement("div");
    card.className = `summary-stat-card tone-${tone}`;
    const pct = total ? Math.round((value / total) * 100) : 0;
    card.innerHTML = `<strong>${value}</strong><span>${escapeHtml(label)} · ${pct}%</span>`;
    statCards.appendChild(card);
  });
  wrap.appendChild(statCards);

  const buildBarList = (title, entries) => {
    const section = document.createElement("div");
    section.className = "summary-section";
    const maxCount = Math.max(1, ...entries.map(([, count]) => count));
    section.innerHTML = `<h4>${escapeHtml(title)}</h4>`;
    const list = document.createElement("div");
    list.className = "summary-bar-list";
    entries.forEach(([label, count]) => {
      const row = document.createElement("div");
      row.className = "summary-bar-row";
      const widthPct = Math.round((count / maxCount) * 100);
      row.innerHTML = `
        <span class="summary-bar-label">${escapeHtml(label)}</span>
        <span class="summary-bar-track"><span class="summary-bar-fill" style="width:${widthPct}%"></span></span>
        <span class="summary-bar-count">${count}</span>
      `;
      list.appendChild(row);
    });
    section.appendChild(list);
    return section;
  };

  wrap.appendChild(
    buildBarList(
      "모드별 분포",
      Object.entries(modeCounts).map(([mode, count]) => [modeLabel(mode), count]),
    ),
  );
  wrap.appendChild(buildBarList("모델별 분포", Object.entries(modelCounts)));

  if (topFailingChecks.length) {
    wrap.appendChild(
      buildBarList(
        "자주 실패하는 검증 항목",
        topFailingChecks.map((item) => [item.label, item.count]),
      ),
    );
  }

  if (recentIssues.length) {
    const section = document.createElement("div");
    section.className = "summary-section";
    section.innerHTML = `<h4>최근 확인 필요 / 실패 항목</h4>`;
    recentIssues.forEach((issue) => {
      const row = document.createElement("article");
      row.className = `validation-log log-${issue.status}`;
      row.innerHTML = `
        <div>
          <span>${escapeHtml(modeLabel(issue.mode))}</span>
          <time>${escapeHtml(String(issue.createdAt || "").replace("T", " ").slice(0, 19))}</time>
        </div>
        <h4>${escapeHtml(issue.question)}</h4>
        <strong>${escapeHtml(String(issue.score))}점</strong>
      `;
      section.appendChild(row);
    });
    wrap.appendChild(section);
  }

  els.evaluationContent.appendChild(wrap);
}

async function openEvaluationModal() {
  els.evaluationModal.hidden = false;
  document.body.classList.add("modal-open");
  els.evaluationContent.innerHTML = `<div class="evaluation-loading">검증 데이터를 불러오는 중입니다...</div>`;
  try {
    await loadEvaluationData();
  } catch (error) {
    els.evaluationContent.textContent = `오류: ${error.message}`;
  }
  els.evaluationModalClose.focus();
}

function closeEvaluationModal() {
  els.evaluationModal.hidden = true;
  document.body.classList.remove("modal-open");
}

async function runEvaluations(caseIds = []) {
  if (state.evaluationRunning) return;
  state.evaluationRunning = true;
  state.evaluationRunningCaseId = null;
  state.evaluationRunProgress = { done: 0, total: caseIds.length || state.evaluationCases.length };
  // 전체 실행은 지난 결과를 비우고 시작하고, 케이스 재실행은 기존 결과 위에 덮어쓴다.
  if (!caseIds.length) state.evaluationResults = new Map();
  els.runEvaluationBtn.disabled = true;
  els.runEvaluationBtn.textContent = "테스트 실행 중";
  if (state.evaluationTab === "cases") renderEvaluationContent();
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
        if (!payload) continue;
        const event = JSON.parse(payload);
        if (event.type === "case_start") {
          state.evaluationRunningCaseId = event.caseId;
        } else if (event.type === "case_result") {
          state.evaluationResults.set(event.result.id, event.result);
          state.evaluationRunningCaseId = null;
          state.evaluationRunProgress.done += 1;
        } else if (event.type === "summary") {
          state.evaluationRunningCaseId = null;
        }
        if (state.evaluationTab === "cases") renderEvaluationContent();
      }
      if (done) break;
    }
    state.evaluationRunning = false;
    state.evaluationRunningCaseId = null;
    await refreshEvaluationHistory();
    if (state.evaluationTab === "cases") renderEvaluationContent();
  } catch (error) {
    state.evaluationRunning = false;
    state.evaluationRunningCaseId = null;
    if (state.evaluationTab === "cases") renderEvaluationContent();
    els.evaluationSummary.textContent = `오류: ${error.message}`;
  } finally {
    state.evaluationRunning = false;
    state.evaluationRunningCaseId = null;
    els.runEvaluationBtn.disabled = false;
    els.runEvaluationBtn.textContent = "전체 테스트 실행";
  }
}

export {
  loadEvaluationData,
  refreshFeedbackSummary,
  refreshAccuracyReports,
  refreshEvaluationHistory,
  renderEvaluationContent,
  formatEvalDuration,
  renderEvaluationHistory,
  renderEvaluationCases,
  accuracyValueLabel,
  renderAccuracyInsights,
  renderAccuracyTab,
  runAccuracyCheck,
  renderValidationLogs,
  teamTemplateLabel,
  renderFeedbackTab,
  MODE_LABELS,
  modeLabel,
  renderValidationSummary,
  openEvaluationModal,
  closeEvaluationModal,
  runEvaluations,
};

export { ADMIN_ENDPOINTS } from "./runtime.js?v=20260911-6";
