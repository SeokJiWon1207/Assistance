(function exposeFrontendCore(globalScope) {
  "use strict";

  function storageSafeMessages(messages) {
    return messages
      .filter((message) => !message.loading && !message.streaming)
      .slice(-60)
      .map((message) => ({
        role: message.role,
        content: message.content,
        sources: (message.sources || []).slice(0, 20).map((source) => ({
          ...source,
          content: String(source.content || "").slice(0, 4000),
          summary: String(source.summary || "").slice(0, 1200),
          document: String(source.document || "").slice(0, 4000),
        })),
        followUps: message.followUps || [],
        verification: message.verification || null,
        auditId: message.auditId || "",
        templateId: message.templateId || "",
        feedback: message.feedback || null,
        abTest: message.abTest || null,
        createdAt: message.createdAt || "",
        questionAskedAt: message.questionAskedAt || "",
      }));
  }

  function deriveConversationTitle(messages) {
    const firstQuestion = messages.find((message) => message.role === "user")?.content?.trim();
    if (!firstQuestion) return "새 대화";
    return firstQuestion.length > 28 ? `${firstQuestion.slice(0, 28)}…` : firstQuestion;
  }

  function buildChatMessages(messages, currentContent, historyLimit = 20) {
    const safeLimit = Math.max(1, Number(historyLimit) || 20);
    const history = messages
      .filter((message) => !message.loading && !message.streaming)
      .slice(0, -1)
      .map((message) => ({ role: message.role, content: message.content }))
      .slice(-safeLimit);
    return [...history, { role: "user", content: currentContent }];
  }

  function parseSseBuffer(buffer, flush = false) {
    const frames = String(buffer || "").split(/\r?\n\r?\n/);
    const rest = flush ? "" : frames.pop() || "";
    const completeFrames = flush ? frames.filter(Boolean) : frames;
    const events = [];

    completeFrames.forEach((frame) => {
      const payload = frame
        .split(/\r?\n/)
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("");
      if (payload) events.push(JSON.parse(payload));
    });
    return { events, rest };
  }

  function buildTeamTemplateQuestion(templateId, values = {}) {
    const text = (value) => String(value || "").trim();
    const join = (...parts) => parts.map(text).filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
    const period = text(values.period) || "오늘";
    const customer = text(values.customer);
    const platform = text(values.platform);
    const status = text(values.status);

    if (templateId === "daily-operations") {
      return `${join(period, "등록된", status === "전체" ? "" : status, "이슈를 고객사별로")} 요약해줘`;
    }
    if (templateId === "customer-analysis") {
      return `${join(period, customer || "전체 고객사", platform === "전체" ? "" : platform, status === "전체" ? "" : status, "이슈를", text(values.format) || "표")}로 보여줘`;
    }
    if (templateId === "aging-issues") {
      return `${join(customer, platform === "전체" ? "" : platform, `${text(values.days) || "30"}일 넘게 미종료된 이슈를 오래된 순으로`)} 보여줘`;
    }
    if (templateId === "similar-issues") {
      const issueId = text(values.issueId).replace(/^#/, "");
      if (!issueId) return "기준 이슈 ID를 입력해 주세요.";
      return `#${issueId}와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘`;
    }
    if (templateId === "effort-sla") {
      return `${join(period, customer || "고객사별", platform === "전체" ? "" : platform, "공수와 평균 처리시간을 표로")} 보여줘`;
    }
    if (templateId === "weekly-report") {
      return `${join(period, customer, "신규 이슈, 미종료 이슈, 장기 적체, 공수와 처리시간을 주간 보고서 형식으로")} 작성해줘`;
    }
    return "";
  }

  const api = Object.freeze({
    storageSafeMessages,
    deriveConversationTitle,
    buildChatMessages,
    buildTeamTemplateQuestion,
    parseSseBuffer,
  });

  globalScope.AssistantFrontendCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
