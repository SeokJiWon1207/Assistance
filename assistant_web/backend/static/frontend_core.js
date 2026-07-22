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

  const api = Object.freeze({
    storageSafeMessages,
    deriveConversationTitle,
    buildChatMessages,
    parseSseBuffer,
  });

  globalScope.AssistantFrontendCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
