export function createConversationRecord(now = new Date()) {
  const timestamp = now.toISOString();
  return {
    id: globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    title: "새 대화",
    createdAt: timestamp,
    updatedAt: timestamp,
    favorite: false,
    messages: [],
  };
}
