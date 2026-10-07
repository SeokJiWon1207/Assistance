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
        notionActionId: message.notionActionId || "",
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

  const CONVERSATION_DATE_GROUPS = ["오늘", "어제", "지난 7일", "이전"];
  const DAY_MS = 24 * 60 * 60 * 1000;

  function conversationTimestamp(conversation) {
    const time = Date.parse(conversation?.updatedAt || conversation?.createdAt || "");
    return Number.isNaN(time) ? 0 : time;
  }

  function conversationDateGroup(conversation, now = new Date()) {
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const time = conversationTimestamp(conversation);
    if (time >= today) return "오늘";
    if (time >= today - DAY_MS) return "어제";
    if (time >= today - 6 * DAY_MS) return "지난 7일";
    return "이전";
  }

  function conversationTimeLabel(conversation, now = new Date()) {
    const time = conversationTimestamp(conversation);
    if (!time) return "";
    const date = new Date(time);
    const group = conversationDateGroup(conversation, now);
    if (group === "오늘") return new Intl.DateTimeFormat("ko-KR", { hour: "numeric", minute: "2-digit" }).format(date);
    if (group === "어제") return "어제";
    if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}/${date.getDate()}`;
    return `${date.getFullYear()}.${date.getMonth() + 1}.${date.getDate()}`;
  }

  // 대화 안의 날짜 구분선. 같은 날 메시지는 같은 key를 갖고, 이름은 오늘·어제·"9월 29일 월요일"
  // (올해가 아니면 연도 포함) 순으로 정한다. 시각은 구분선이 아니라 메시지 옆에 둔다.
  function messageDayKey(timestamp) {
    const date = new Date(timestamp || "");
    if (Number.isNaN(date.getTime())) return "";
    return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
  }

  function messageDayLabel(timestamp, now = new Date()) {
    const date = new Date(timestamp || "");
    if (Number.isNaN(date.getTime())) return "";
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
    if (day === today) return "오늘";
    if (day === today - DAY_MS) return "어제";
    const weekday = new Intl.DateTimeFormat("ko-KR", { weekday: "long" }).format(date);
    const year = date.getFullYear() === now.getFullYear() ? "" : `${date.getFullYear()}년 `;
    return `${year}${date.getMonth() + 1}월 ${date.getDate()}일 ${weekday}`;
  }

  function messageClockLabel(timestamp) {
    const date = new Date(timestamp || "");
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("ko-KR", { hour: "numeric", minute: "2-digit" }).format(date);
  }

  function byFavoriteThenRecent(a, b) {
    return Number(Boolean(b.favorite)) - Number(Boolean(a.favorite))
      || conversationTimestamp(b) - conversationTimestamp(a);
  }

  // 즐겨찾기는 모두 맨 위 묶음에 두고, 나머지는 최근 limit개만 날짜별로 나눈다.
  function conversationListSections(conversations, { now = new Date(), limit = 8, expanded = false } = {}) {
    const sorted = [...conversations].sort(byFavoriteThenRecent);
    const favorites = sorted.filter((item) => item.favorite);
    const others = sorted.filter((item) => !item.favorite);
    const shown = expanded ? others : others.slice(0, limit);
    const groups = favorites.length ? [{ label: "즐겨찾기", items: favorites }] : [];
    CONVERSATION_DATE_GROUPS.forEach((label) => {
      const items = shown.filter((item) => conversationDateGroup(item, now) === label);
      if (items.length) groups.push({ label, items });
    });
    return { groups, hiddenCount: others.length - shown.length, collapsible: others.length > limit };
  }

  // 제목에서 먼저 찾고, 제목에 없으면 질문·답변 내용에서 찾은 문장을 한 줄 미리보기로 돌려준다.
  function searchConversations(conversations, query) {
    const needle = String(query || "").trim().toLocaleLowerCase();
    if (!needle) return [];
    const results = [];
    [...conversations].sort(byFavoriteThenRecent).forEach((conversation) => {
      const title = conversation.title || "새 대화";
      const titleIndex = title.toLocaleLowerCase().indexOf(needle);
      if (titleIndex >= 0) {
        results.push({ conversation, titleRange: [titleIndex, titleIndex + needle.length], snippet: null });
        return;
      }
      for (const message of conversation.messages || []) {
        const text = String(message?.content || "").replace(/\s+/g, " ").trim();
        const index = text.toLocaleLowerCase().indexOf(needle);
        if (index < 0) continue;
        const from = Math.max(0, index - 12);
        const prefix = from > 0 ? "…" : "";
        const start = prefix.length + index - from;
        results.push({
          conversation,
          titleRange: null,
          snippet: { text: `${prefix}${text.slice(from, index + needle.length + 36)}`, start, end: start + needle.length },
        });
        return;
      }
    });
    return results;
  }

  function buildChatMessages(messages, currentContent, historyLimit = 20) {
    const safeLimit = Math.max(1, Number(historyLimit) || 20);
    const history = messages
      .filter((message) => !message.loading && !message.streaming)
      .slice(0, -1)
      .map((message) => ({ role: message.role, content: message.content, ...(message.notionActionId ? { notionActionId: message.notionActionId } : {}) }))
      .slice(-safeLimit);
    return [...history, { role: "user", content: currentContent }];
  }

  function mergeConversations(localItems, serverItems, makeId = () => globalScope.crypto.randomUUID()) {
    const localById = new Map(localItems.map((item) => [item.id, item]));
    const merged = [];
    for (const server of serverItems) {
      const local = localById.get(server.id);
      localById.delete(server.id);
      const changed = local && (conversationContentChanged(local.messages, server.messages)
        || local.title !== server.title || Boolean(local.favorite) !== Boolean(server.favorite));
      const pending = local && (local.syncPending || (!local.serverUpdatedAt && String(local.updatedAt) > String(server.updatedAt)));
      if (changed && pending) {
        if (local.serverUpdatedAt && local.serverUpdatedAt !== server.updatedAt) {
          merged.push({ ...local, id: makeId(), title: `${local.title || "대화"} (복구본)`, serverUpdatedAt: "", syncPending: true });
          merged.push({ ...server, serverUpdatedAt: server.updatedAt, syncPending: false });
        } else {
          merged.push({ ...local, serverUpdatedAt: server.updatedAt, syncPending: true });
        }
      } else {
        merged.push({ ...server, serverUpdatedAt: server.updatedAt, syncPending: false });
      }
    }
    // 서버 목록은 페이지 일부일 수도 있다. 누락을 삭제로 해석하지 않는다.
    merged.push(...localById.values());
    return merged;
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

  function shouldSubmitPromptOnKeydown(event = {}) {
    return event.key === "Enter"
      && !event.shiftKey
      && !event.isComposing
      && event.keyCode !== 229
      && event.which !== 229
      && !event.repeat;
  }

  // 인증을 끈 로컬 개발 환경에는 역할 정보가 없으므로 관리자 도구를 모두 보인다.
  function canUseAdminTools(authEnabled, user) {
    return !authEnabled || user?.role === "admin";
  }

  function normalizeTeamTemplateValues(template, values = {}) {
    const normalizedValues = Object.fromEntries(Object.entries(values).map(([key, value]) => [
      key,
      String(value ?? "").trim(),
    ]));
    (template.fields || []).forEach((field) => {
      const name = field.name;
      if (field.normalize === "issue-id") {
        normalizedValues[name] = String(normalizedValues[name] || "").replace(/^#/, "");
      } else if (field.normalize === "month-korean") {
        normalizedValues[name] = String(normalizedValues[name] || "").replace(
          /^(20\d{2})-(0?[1-9]|1[0-2])$/,
          (_value, year, month) => `${year}년 ${Number(month)}월`,
        );
      }
    });
    return normalizedValues;
  }

  // 숫자·영문은 읽는 소리로 받침을 정한다(0 영, 1 일, 3 삼, 6 육, 7 칠, 8 팔 / L 엘, M 엠, N 엔).
  const BATCHIM_DIGITS = new Set(["0", "1", "3", "6", "7", "8"]);
  const RIEUL_DIGITS = new Set(["1", "7", "8"]);
  const BATCHIM_LETTERS = new Set(["l", "m", "n", "r"]);
  const RIEUL_LETTERS = new Set(["l", "r"]);

  function finalSound(word) {
    // 닫는 괄호·따옴표는 건너뛰고 실제 마지막 글자를 본다: "[기준 이슈 ID]와(과)" → D.
    const char = String(word || "").replace(/[\])}'"”’\s]+$/u, "").slice(-1);
    const code = char.charCodeAt(0) - 0xac00;
    if (code >= 0 && code <= 11171) {
      const jong = code % 28;
      return { batchim: jong !== 0, rieul: jong === 8 };
    }
    const lower = char.toLowerCase();
    if (/[0-9]/.test(char)) return { batchim: BATCHIM_DIGITS.has(char), rieul: RIEUL_DIGITS.has(char) };
    if (/[a-z]/.test(lower)) return { batchim: BATCHIM_LETTERS.has(lower), rieul: RIEUL_LETTERS.has(lower) };
    return null;
  }

  // 질문 틀의 '(으)로', '을(를)', '와(과)'처럼 쓴 조사를 앞말의 받침에 맞춰 하나로 고른다.
  // 고른 값에 따라 '목록로', '#30220와'처럼 어색해지는 것을 막는다.
  const PARTICLE_PAIRS = [["으로", "로"], ["을", "를"], ["이", "가"], ["은", "는"], ["과", "와"]];

  function attachKoreanParticles(text) {
    return String(text || "").replace(
      /\(으\)로|(을|를|이|가|은|는|과|와)\((을|를|이|가|은|는|과|와)\)/g,
      (marker, first, second, offset, whole) => {
        const pair = marker === "(으)로"
          ? PARTICLE_PAIRS[0]
          : PARTICLE_PAIRS.find(([withBatchim, without]) => [withBatchim, without].includes(first) && [withBatchim, without].includes(second));
        if (!pair) return marker;
        const sound = finalSound(whole.slice(0, offset));
        if (!sound) return pair[1];
        if (pair[0] === "으로") return sound.batchim && !sound.rieul ? "으로" : "로";
        return sound.batchim ? pair[0] : pair[1];
      },
    );
  }

  function fillTeamTemplateQuestion(questionTemplate, normalizedValues) {
    return attachKoreanParticles(questionTemplate.replace(
      /\{([A-Za-z][A-Za-z0-9_]*)(?:([|?])([^{}]*))?\}/g,
      (_token, name, operator, operand = "") => {
        const value = normalizedValues[name] || "";
        if (operator === "|") return value || operand;
        if (operator === "?") return value === operand ? "" : value;
        return value;
      },
    )).replace(/\s+/g, " ").trim();
  }

  function buildTeamTemplateQuestion(template, values = {}) {
    if (!template || typeof template.questionTemplate !== "string") return "";
    const normalizedValues = normalizeTeamTemplateValues(template, values);
    const missingRequired = (template.fields || []).find(
      (field) => field.required && !normalizedValues[field.name],
    );
    if (missingRequired) return `${missingRequired.label} 항목을 입력해 주세요.`;
    return fillTeamTemplateQuestion(template.questionTemplate, normalizedValues);
  }

  // 조건 입력 중 미리보기용: 비어 있는 필수 항목을 [항목명]으로 채운 문장과 누락 목록을 함께 준다.
  function describeTeamTemplateQuestion(template, values = {}) {
    if (!template || typeof template.questionTemplate !== "string") return { question: "", missing: [] };
    const normalizedValues = normalizeTeamTemplateValues(template, values);
    const missingFields = (template.fields || []).filter(
      (field) => field.required && !normalizedValues[field.name],
    );
    missingFields.forEach((field) => {
      normalizedValues[field.name] = `[${field.label}]`;
    });
    return {
      question: fillTeamTemplateQuestion(template.questionTemplate, normalizedValues),
      missing: missingFields.map((field) => field.label),
    };
  }

  const api = Object.freeze({
    attachKoreanParticles,
    storageSafeMessages,
    mergeConversations,
    deriveConversationTitle,
    conversationContentChanged,
    conversationTimeLabel,
    conversationListSections,
    messageDayKey,
    messageDayLabel,
    messageClockLabel,
    searchConversations,
    buildChatMessages,
    buildTeamTemplateQuestion,
    describeTeamTemplateQuestion,
    parseSseBuffer,
    shouldSubmitPromptOnKeydown,
    canUseAdminTools,
  });

  globalScope.AssistantFrontendCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(globalThis);
