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
        // 차트 payload 도 함께 저장해야 대화를 다시 열었을 때 차트가 남는다.
        effortChart: message.effortChart || null,
        effortRanking: message.effortRanking || null,
        effortTrend: message.effortTrend || null,
        playbookCandidate: message.playbookCandidate || null,
        playbookSaved: Boolean(message.playbookSaved),
        createdAt: message.createdAt || "",
        questionAskedAt: message.questionAskedAt || "",
      }));
  }

  function deriveConversationTitle(messages) {
    const firstQuestion = messages.find((message) => message.role === "user")?.content?.trim();
    if (!firstQuestion) return "새 대화";
    return firstQuestion.length > 28 ? `${firstQuestion.slice(0, 28)}…` : firstQuestion;
  }

  // 목록 정렬 기준인 updatedAt 은 대화 내용이 실제로 바뀔 때만 갱신해야 하므로
  // 저장된 메시지와 현재 메시지를 같은 규칙으로 정규화해 비교한다.
  // 서버는 값이 비어 있는 필드를 지운 채 대화를 돌려주므로 양쪽 모두 정규화한다.
  function conversationContentChanged(storedMessages, currentMessages) {
    const stored = storageSafeMessages(Array.isArray(storedMessages) ? storedMessages : []);
    const current = storageSafeMessages(Array.isArray(currentMessages) ? currentMessages : []);
    return JSON.stringify(stored) !== JSON.stringify(current);
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

  function buildTeamTemplateQuestion(template, values = {}) {
    if (!template || typeof template.questionTemplate !== "string") return "";
    const fields = new Map((template.fields || []).map((field) => [field.name, field]));
    const normalizedValues = Object.fromEntries(Object.entries(values).map(([key, value]) => [
      key,
      String(value ?? "").trim(),
    ]));
    fields.forEach((field, name) => {
      if (field.normalize === "issue-id") {
        normalizedValues[name] = String(normalizedValues[name] || "").replace(/^#/, "");
      } else if (field.normalize === "month-korean") {
        normalizedValues[name] = String(normalizedValues[name] || "").replace(
          /^(20\d{2})-(0?[1-9]|1[0-2])$/,
          (_value, year, month) => `${year}년 ${Number(month)}월`,
        );
      }
    });
    const missingRequired = (template.fields || []).find(
      (field) => field.required && !normalizedValues[field.name],
    );
    if (missingRequired) return `${missingRequired.label} 항목을 입력해 주세요.`;

    return template.questionTemplate.replace(
      /\{([A-Za-z][A-Za-z0-9_]*)(?:([|?])([^{}]*))?\}/g,
      (_token, name, operator, operand = "") => {
        const value = normalizedValues[name] || "";
        if (operator === "|") return value || operand;
        if (operator === "?") return value === operand ? "" : value;
        return value;
      },
    ).replace(/\s+/g, " ").trim();
  }

  const api = Object.freeze({
    storageSafeMessages,
    deriveConversationTitle,
    conversationContentChanged,
    buildChatMessages,
    buildTeamTemplateQuestion,
    parseSseBuffer,
  });

  globalScope.AssistantFrontendCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
