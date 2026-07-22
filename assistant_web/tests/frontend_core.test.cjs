const test = require("node:test");
const assert = require("node:assert/strict");

const {
  storageSafeMessages,
  deriveConversationTitle,
  buildChatMessages,
  parseSseBuffer,
} = require("../backend/static/frontend_core.js");

test("storageSafeMessages removes transient rows and bounds stored source text", () => {
  const messages = [
    { role: "assistant", content: "loading", loading: true },
    {
      role: "assistant",
      content: "done",
      sources: Array.from({ length: 25 }, (_, index) => ({
        issue_id: index,
        content: "c".repeat(5000),
        summary: "s".repeat(1500),
        document: "d".repeat(5000),
      })),
    },
  ];

  const stored = storageSafeMessages(messages);

  assert.equal(stored.length, 1);
  assert.equal(stored[0].sources.length, 20);
  assert.equal(stored[0].sources[0].content.length, 4000);
  assert.equal(stored[0].sources[0].summary.length, 1200);
  assert.equal(stored[0].sources[0].document.length, 4000);
});

test("storageSafeMessages retains only the latest sixty completed messages", () => {
  const messages = Array.from({ length: 70 }, (_, index) => ({
    role: "user",
    content: `question-${index}`,
  }));

  const stored = storageSafeMessages(messages);

  assert.equal(stored.length, 60);
  assert.equal(stored[0].content, "question-10");
  assert.equal(stored.at(-1).content, "question-69");
});

test("deriveConversationTitle uses the first question and truncates long titles", () => {
  assert.equal(deriveConversationTitle([]), "새 대화");
  assert.equal(
    deriveConversationTitle([{ role: "assistant", content: "hello" }, { role: "user", content: " 질문 " }]),
    "질문",
  );
  const longQuestion = "가".repeat(35);
  assert.equal(deriveConversationTitle([{ role: "user", content: longQuestion }]), `${"가".repeat(28)}…`);
});

test("buildChatMessages caps history and includes the current question once", () => {
  const messages = [
    ...Array.from({ length: 25 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", content: `m${index}` })),
    { role: "user", content: "current" },
    { role: "assistant", content: "pending", loading: true },
  ];

  const payload = buildChatMessages(messages, "current", 20);

  assert.equal(payload.length, 21);
  assert.deepEqual(payload[0], { role: "assistant", content: "m5" });
  assert.deepEqual(payload.at(-1), { role: "user", content: "current" });
  assert.equal(payload.filter((message) => message.content === "current").length, 1);
});

test("parseSseBuffer preserves fragmented frames and parses CRLF frames", () => {
  const first = parseSseBuffer('data: {"type":"status","message":"검');
  assert.deepEqual(first.events, []);
  assert.notEqual(first.rest, "");

  const second = parseSseBuffer(`${first.rest}증"}\r\n\r\ndata: {"type":"result","data":{"content":"완료"}}\r\n\r\n`);
  assert.equal(second.rest, "");
  assert.deepEqual(second.events, [
    { type: "status", message: "검증" },
    { type: "result", data: { content: "완료" } },
  ]);
});

test("parseSseBuffer flushes a final frame without a blank terminator", () => {
  const parsed = parseSseBuffer('data: {"type":"done"}', true);
  assert.deepEqual(parsed.events, [{ type: "done" }]);
  assert.equal(parsed.rest, "");
});
