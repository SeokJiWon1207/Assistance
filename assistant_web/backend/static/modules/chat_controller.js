import { state, els, apiFetch, buildChatMessages, parseSseBuffer } from "./runtime.js";
import { persistConversations } from "./workspace.js";

const controllerHooks = {
  selectedModel: null,
  statusLabel: null,
  generationPayload: null,
  abComparisonEnabled: null,
  sourceToolsPayload: null,
  renderMessages: null,
  setBusy: null,
};

function configureChatController(hooks = {}) {
  Object.assign(controllerHooks, hooks);
}

function controllerHook(name) {
  const hook = controllerHooks[name];
  if (typeof hook !== "function") {
    throw new Error(`채팅 제어기 설정이 누락되었습니다: ${name}`);
  }
  return hook;
}

async function sendChat(content, onEvent = () => {}, signal = undefined) {
  const model = controllerHook("selectedModel")();
  if (!model) throw new Error("모델을 선택해 주세요.");
  if (model.status !== "active") {
    throw new Error(`현재 서비스가 ${controllerHook("statusLabel")(model.status)} 상태입니다.`);
  }

  // 대화가 길어져도 서버로 보내는 히스토리는 최근 N개로 제한한다(서버에서도 한 번 더 제한).
  // 현재 질문은 state.messages에 이미 push되어 있으므로 history에서는 제외하고,
  // 아래에서 현재 질문을 한 번만 명시적으로 붙인다.
  const chatMessages = buildChatMessages(state.messages, content, 20);

  // 직전 assistant 답변의 감사 ID. 서버가 "그중 3번째" 같은 서수 참조를
  // 실제로 보여준 결과 목록을 기준으로 해석하는 데 사용한다.
  const lastAssistantWithAudit = [...state.messages]
    .reverse()
    .find((message) => message.role === "assistant" && message.auditId && !message.loading);

  const response = await apiFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    signal,
    body: JSON.stringify({
      modelId: model.id,
      ...controllerHook("generationPayload")(),
      abTest: controllerHook("abComparisonEnabled")(),
      stream: true,
      contextAuditId: lastAssistantWithAudit?.auditId || null,
      messages: chatMessages,
      tools: controllerHook("sourceToolsPayload")(),
    }),
  });

  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const retryAfter = response.headers.get("Retry-After");
    const suffix = response.status === 429 && retryAfter
      ? ` (${retryAfter}초 후 다시 시도)`
      : "";
    const error = new Error(`${data.detail || "모델 응답 중 오류가 발생했습니다."}${suffix}`);
    error.status = response.status;
    error.retryAfter = retryAfter;
    throw error;
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error("스트리밍 응답을 읽을 수 없습니다.");
  const decoder = new TextDecoder();
  let buffer = "";
  let result = null;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const parsed = parseSseBuffer(buffer, done);
    buffer = parsed.rest;
    for (const event of parsed.events) {
      onEvent(event);
      if (event.type === "result") result = event.data;
      if (event.type === "error") {
        const error = new Error(event.message || "응답 중 오류가 발생했습니다.");
        error.status = event.status;
        throw error;
      }
    }
    if (done) break;
  }
  if (!result) throw new Error("완료된 응답을 받지 못했습니다.");
  return result;
}

async function requestAssistant(content, questionAskedAt = "", templateId = "") {
  state.messages.push({
    role: "assistant",
    content: "",
    questionAskedAt,
    loading: true,
    streaming: false,
    progress: "요청을 준비하고 있습니다",
    templateId,
  });
  const assistantIndex = state.messages.length - 1;
  const renderMessages = controllerHook("renderMessages");
  const setBusy = controllerHook("setBusy");
  renderMessages();
  setBusy(true);
  const abortController = new AbortController();
  state.chatAbortController = abortController;
  state.chatCanAbort = false;

  try {
    const answer = await sendChat(content, (streamEvent) => {
      const assistant = state.messages[assistantIndex];
      if (!assistant) return;
      if (!state.chatCanAbort) {
        state.chatCanAbort = true;
        setBusy(true);
      }
      if (streamEvent.type === "status") {
        assistant.progress = streamEvent.message;
        assistant.stage = streamEvent.stage;
      } else if (streamEvent.type === "delta") {
        assistant.loading = false;
        assistant.streaming = true;
        assistant.content = `${assistant.content || ""}${streamEvent.content || ""}`;
      }
      renderMessages();
    }, abortController.signal);
    state.messages[assistantIndex] = {
      role: "assistant",
      content: answer.content,
      sources: answer.sources || [],
      followUps: answer.followUps || [],
      verification: answer.verification || null,
      auditId: answer.auditId || "",
      notionActionId: answer.notionActionId || "",
      abTest: answer.abTest || null,
      effortChart: answer.effortChart || null,
      effortRanking: answer.effortRanking || null,
      effortTrend: answer.effortTrend || null,
      playbookCandidate: answer.playbookCandidate || null,
      playbookSaved: false,
      questionAskedAt,
      templateId,
    };
  } catch (error) {
    if (error.name === "AbortError") {
      const partial = state.messages[assistantIndex]?.content || "";
      state.messages[assistantIndex] = {
        role: "assistant",
        content: partial
          ? `${partial}\n\n(여기서 답변 생성을 중지했습니다)`
          : "답변 생성을 중지했습니다.",
        stopped: true,
        questionAskedAt,
        templateId,
      };
    } else {
      state.messages[assistantIndex] = {
        role: "assistant",
        content: `오류: ${error.message}`,
        questionAskedAt,
        templateId,
      };
    }
  } finally {
    state.chatAbortController = null;
    state.chatCanAbort = false;
    setBusy(false);
    persistConversations();
    renderMessages();
    els.input.focus();
  }
}

export { configureChatController, sendChat, requestAssistant };
