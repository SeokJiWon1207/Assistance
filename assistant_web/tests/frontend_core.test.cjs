const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  attachKoreanParticles,
  storageSafeMessages,
  deriveConversationTitle,
  conversationContentChanged,
  conversationListSections,
  conversationTimeLabel,
  searchConversations,
  buildChatMessages,
  buildTeamTemplateQuestion,
  describeTeamTemplateQuestion,
  parseSseBuffer,
  shouldSubmitPromptOnKeydown,
  canUseAdminTools,
  messageClockLabel,
  messageDayKey,
  messageDayLabel,
} = require("../backend/static/frontend_core.js");

const configuredTemplates = JSON.parse(
  fs.readFileSync(path.join(__dirname, "../config/team_templates.json"), "utf8"),
).templates;
const template = (id) => configuredTemplates.find((item) => item.id === id);

test("storageSafeMessages removes transient rows and bounds stored source text", () => {
  const messages = [
    { role: "assistant", content: "loading", loading: true },
    {
      role: "assistant",
      content: "done",
      templateId: "customer-analysis",
      feedback: { rating: "helpful" },
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
  assert.equal(stored[0].templateId, "customer-analysis");
  assert.equal(stored[0].feedback.rating, "helpful");
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

test("long list follow-ups send a complete recent suffix within the server character budget", () => {
  const messages = Array.from({ length: 8 }, (_, index) => [
    { role: "user", content: `나머지도 보여줘 ${index}` },
    { role: "assistant", content: "가".repeat(28000), notionActionId: `action-${index}` },
  ]).flat();
  messages.push({ role: "user", content: "더 보여줘" }, { role: "assistant", loading: true });
  const original = JSON.stringify(messages);
  const sent = buildChatMessages(messages, "더 보여줘");
  assert.ok(sent.reduce((size, message) => size + [...message.content].length, 0) <= 120000);
  assert.equal(sent.at(-1).content, "더 보여줘");
  assert.equal(sent.at(-3).content, "나머지도 보여줘 7");
  assert.equal(sent.at(-2).content.length, 28000);
  assert.equal(sent.at(-2).notionActionId, "action-7");
  assert.equal(JSON.stringify(messages), original);
});

test("oversized old answers are omitted from model context without changing the visible answer", () => {
  const messages = [
    { role: "user", content: "기간 조건 없이 보여줘" },
    { role: "assistant", content: "가".repeat(30001) },
    { role: "user", content: "나머지도 보여줘" },
  ];
  const sent = buildChatMessages(messages, "나머지도 보여줘", 100);
  assert.deepEqual(sent.map((message) => message.content), ["기간 조건 없이 보여줘", "나머지도 보여줘"]);
  assert.equal(messages[1].content.length, 30001);
});

test("saved pagination retains all answer tables and audit IDs while bounding duplicate source bytes", () => {
  const messages = Array.from({ length: 10 }, (_, page) => ({
    role: "assistant", content: `페이지 ${page}\n${"가abc".repeat(6750)}`, auditId: `audit-${page}`,
    sources: Array.from({ length: 20 }, (_, index) => ({
      issue_id: page * 200 + index, content: "나".repeat(3000),
      detail: { description: "다".repeat(3000) },
    })),
  }));
  const original = JSON.stringify(messages);
  const stored = storageSafeMessages(messages);
  assert.ok(Buffer.byteLength(JSON.stringify(stored), "utf8") <= 900000);
  assert.deepEqual(stored.map(({ content, auditId }) => ({ content, auditId })),
    messages.map(({ content, auditId }) => ({ content, auditId })));
  assert.equal(stored.at(-1).sources.length, 20);
  assert.equal(JSON.stringify(messages), original);
  assert.deepEqual(storageSafeMessages(stored), stored);
});

test("a large optional chart cannot make a single saved answer exceed the request budget", () => {
  const message = { role: "assistant", content: "원문 답변", auditId: "audit-1",
    effortChart: { points: "가".repeat(340000) } };
  const stored = storageSafeMessages([message]);
  assert.ok(Buffer.byteLength(JSON.stringify(stored), "utf8") <= 900000);
  assert.equal(stored[0].content, message.content);
  assert.equal(stored[0].auditId, message.auditId);
  assert.equal(message.effortChart.points.length, 340000);
});

test("saved size includes the server JSON separator spaces for large arrays", () => {
  const message = { role: "assistant", content: "원문 답변", auditId: "audit-1",
    effortChart: { points: Array(400000).fill(0) } };
  const stored = storageSafeMessages([message]);
  assert.equal(stored[0].effortChart, null);
  assert.equal(stored[0].content, message.content);
  assert.equal(stored[0].auditId, message.auditId);
  assert.equal(message.effortChart.points.length, 400000);
});

const now = new Date(2026, 8, 30, 15, 0);
const daysAgo = (days, hour = 10) => new Date(2026, 8, 30 - days, hour, 0).toISOString();

test("conversationListSections pins favorites and groups the recent limit by date", () => {
  const conversations = [
    { id: "old", title: "오래된 질문", updatedAt: daysAgo(20) },
    { id: "today", title: "오늘 질문", updatedAt: daysAgo(0) },
    { id: "favorite", title: "즐겨찾기", favorite: true, updatedAt: daysAgo(40) },
    { id: "yesterday", title: "어제 질문", updatedAt: daysAgo(1) },
    { id: "week", title: "이번 주 질문", updatedAt: daysAgo(3) },
  ];
  const summary = (sections) => sections.groups.map((group) => [group.label, group.items.map((item) => item.id)]);

  const collapsed = conversationListSections(conversations, { now, limit: 3 });
  assert.deepEqual(summary(collapsed), [
    ["즐겨찾기", ["favorite"]],
    ["오늘", ["today"]],
    ["어제", ["yesterday"]],
    ["지난 7일", ["week"]],
  ]);
  assert.equal(collapsed.hiddenCount, 1);
  assert.equal(collapsed.collapsible, true);

  const expanded = conversationListSections(conversations, { now, limit: 3, expanded: true });
  assert.deepEqual(summary(expanded).at(-1), ["이전", ["old"]]);
  assert.equal(expanded.hiddenCount, 0);
});

test("searchConversations matches titles first and previews matching message text", () => {
  const conversations = [
    { id: "title", title: "주간 보고서 작성", updatedAt: daysAgo(2), messages: [] },
    { id: "content", title: "고객사 추이", updatedAt: daysAgo(0), messages: [
      { role: "assistant", content: "최근 3개월 추이를\n주간 보고서에 넣을 수 있게 표로 정리했습니다." },
    ] },
    { id: "favorite", title: "보고서 모음", favorite: true, updatedAt: daysAgo(30), messages: [] },
    { id: "miss", title: "공수", updatedAt: daysAgo(1), messages: [{ role: "user", content: "공수 알려줘" }] },
  ];

  const results = searchConversations(conversations, " 보고서 ");
  assert.deepEqual(results.map((result) => result.conversation.id), ["favorite", "content", "title"]);
  const byId = Object.fromEntries(results.map((result) => [result.conversation.id, result]));
  assert.deepEqual(byId.title.titleRange, [3, 6]);
  assert.equal(byId.title.snippet, null);
  const { snippet } = byId.content;
  assert.equal(byId.content.titleRange, null);
  assert.equal(snippet.text.slice(snippet.start, snippet.end), "보고서");
  assert.doesNotMatch(snippet.text, /\n/);
  assert.deepEqual(searchConversations(conversations, "   "), []);
});

test("conversationTimeLabel shows time today, then relative day or date", () => {
  assert.match(conversationTimeLabel({ updatedAt: daysAgo(0, 15) }, now), /3:00/);
  assert.equal(conversationTimeLabel({ updatedAt: daysAgo(1) }, now), "어제");
  assert.equal(conversationTimeLabel({ updatedAt: daysAgo(5) }, now), "9/25");
  assert.equal(conversationTimeLabel({ updatedAt: new Date(2025, 11, 3).toISOString() }, now), "2025.12.3");
  assert.equal(conversationTimeLabel({}, now), "");
});

test("message date divider labels today, yesterday, weekday dates and other years", () => {
  assert.equal(messageDayLabel(daysAgo(0, 9), now), "오늘");
  assert.equal(messageDayLabel(daysAgo(1, 23), now), "어제");
  assert.equal(messageDayLabel(new Date(2026, 8, 28, 15, 12).toISOString(), now), "9월 28일 월요일");
  assert.equal(messageDayLabel(new Date(2025, 11, 3, 9).toISOString(), now), "2025년 12월 3일 수요일");
  assert.equal(messageDayLabel("", now), "");
  // 같은 날이면 시각이 달라도 같은 구분선 key를 갖는다.
  assert.equal(messageDayKey(daysAgo(0, 0)), messageDayKey(daysAgo(0, 23)));
  assert.notEqual(messageDayKey(daysAgo(0, 0)), messageDayKey(daysAgo(1, 23)));
  assert.match(messageClockLabel(new Date(2026, 8, 29, 15, 12).toISOString()), /^오후 3:12$/);
  assert.equal(messageClockLabel("not a date"), "");
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

test("shouldSubmitPromptOnKeydown ignores Korean IME composition and repeated Enter", () => {
  assert.equal(shouldSubmitPromptOnKeydown({ key: "Enter" }), true);
  assert.equal(shouldSubmitPromptOnKeydown({ key: "Enter", shiftKey: true }), false);
  assert.equal(shouldSubmitPromptOnKeydown({ key: "Enter", isComposing: true }), false);
  assert.equal(shouldSubmitPromptOnKeydown({ key: "Enter", keyCode: 229 }), false);
  assert.equal(shouldSubmitPromptOnKeydown({ key: "Enter", which: 229 }), false);
  assert.equal(shouldSubmitPromptOnKeydown({ key: "Enter", repeat: true }), false);
  assert.equal(shouldSubmitPromptOnKeydown({ key: "a" }), false);
});

test("canUseAdminTools shows admin tools only to admins when auth is enabled", () => {
  assert.equal(canUseAdminTools(true, { role: "admin" }), true);
  assert.equal(canUseAdminTools(true, { role: "manager" }), false);
  assert.equal(canUseAdminTools(true, { role: "member" }), false);
  assert.equal(canUseAdminTools(true, null), false);
  assert.equal(canUseAdminTools(false, null), true);
});

test("buildTeamTemplateQuestion creates every configured shared template question", () => {
  assert.equal(
    buildTeamTemplateQuestion(template("daily-operations"), { period: "오늘", status: "미종료" }),
    "오늘 등록된 미종료 이슈를 고객사별로 요약해줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("customer-analysis"), {
      period: "이번 달",
      customer: "유진증권",
      platform: "MTS",
      status: "미종료",
      format: "표",
    }),
    "이번 달 유진증권 MTS 미종료 이슈를 표로 모두 보여줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("aging-issues"), { days: "30", platform: "전체" }),
    "30일 넘게 미종료된 이슈를 오래된 순으로 보여줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("similar-issues"), { issueId: "#30225" }),
    "#30225와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("effort-sla"), { period: "지난달", platform: "HTS" }),
    "지난달 고객사별 HTS 공수와 평균 처리시간을 표로 보여줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("weekly-report"), { period: "지난주", customer: "NH증권" }),
    "지난주 NH증권 신규 이슈, 미종료 이슈, 장기 적체, 공수와 처리시간을 주간 보고서 형식으로 작성해줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("customer-profile"), {
      period: "최근 3개월",
      customer: "신한투자증권",
      platform: "전체",
      status: "전체",
    }),
    "최근 3개월 신한투자증권 고객사 프로필을 만들어줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("monthly-customer-report"), {
      month: "2026-08",
      customer: "신한투자증권",
      platform: "전체",
    }),
    "2026년 8월 신한투자증권 월간 고객사 리포트를 만들어줘",
  );
});

test("template particles follow the final sound of the chosen value", () => {
  assert.equal(attachKoreanParticles("표(으)로 · 목록(으)로 · 파일(으)로"), "표로 · 목록으로 · 파일로");
  assert.equal(attachKoreanParticles("#30225와(과) · #30220와(과) · #31573와(과)"), "#30225와 · #30220과 · #31573과");
  assert.equal(attachKoreanParticles("HTS을(를) · MTS을(를) · [기준 이슈 ID]와(과)"), "HTS를 · MTS를 · [기준 이슈 ID]와");
  assert.equal(attachKoreanParticles("보고서이(가) · 공지은(는)"), "보고서가 · 공지는");
  const analysis = { period: "지난달", customer: "KB증권", platform: "전체", status: "전체" };
  assert.equal(
    buildTeamTemplateQuestion(template("customer-analysis"), { ...analysis, format: "목록" }),
    "지난달 KB증권 이슈를 목록으로 모두 보여줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("similar-issues"), { issueId: "#30220" }),
    "#30220과 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘",
  );
  // 플랫폼을 '전체'로 두고 고객사만 골라도 '에서'가 고객사에 바로 붙는다.
  assert.match(
    buildTeamTemplateQuestion(template("incident-triage"), { symptom: "로그인 오류", customer: "KB증권", platform: "전체", urgency: "긴급" }),
    /^긴급 우선순위로 KB증권에서 발생한 '로그인 오류' 이슈의/,
  );
  assert.match(
    buildTeamTemplateQuestion(template("incident-triage"), { symptom: "로그인 오류", platform: "전체", urgency: "긴급" }),
    /^긴급 우선순위로 전체 고객사에서 발생한/,
  );
});

test("buildTeamTemplateQuestion applies defaults, omissions, and field normalizers", () => {
  assert.equal(
    buildTeamTemplateQuestion(template("daily-operations"), { status: "전체" }),
    "오늘 등록된 이슈를 고객사별로 요약해줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("similar-issues"), { issueId: "#30225" }),
    "#30225와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘",
  );
  assert.equal(
    buildTeamTemplateQuestion(template("customer-profile"), { period: "최근 3개월" }),
    "고객사 항목을 입력해 주세요.",
  );
  assert.equal(buildTeamTemplateQuestion(null, {}), "");
});

test("describeTeamTemplateQuestion previews missing required fields as placeholders", () => {
  assert.deepEqual(
    describeTeamTemplateQuestion(template("customer-profile"), { period: "최근 3개월" }),
    { question: "최근 3개월 [고객사] 고객사 프로필을 만들어줘", missing: ["고객사"] },
  );
  assert.deepEqual(
    describeTeamTemplateQuestion(template("similar-issues"), { issueId: "#" }),
    { question: "#[기준 이슈 ID]와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘", missing: ["기준 이슈 ID"] },
  );
  const values = { month: "2026-08", customer: "신한투자증권", platform: "전체" };
  assert.deepEqual(
    describeTeamTemplateQuestion(template("monthly-customer-report"), values),
    { question: buildTeamTemplateQuestion(template("monthly-customer-report"), values), missing: [] },
  );
  assert.deepEqual(describeTeamTemplateQuestion(null, {}), { question: "", missing: [] });
});

test("storageSafeMessages preserves a bounded playbook candidate", () => {
  const [message] = storageSafeMessages([{
    role: "assistant",
    content: "유사 사례",
    playbookCandidate: {
      title: "세션 만료 대응",
      symptom: "증상",
      cause: "원인",
      resolution: "해결",
      verification: "검증",
      tags: ["MTS"],
      sourceIssueIds: [12345],
    },
    playbookSaved: true,
  }]);
  assert.equal(message.playbookCandidate.title, "세션 만료 대응");
  assert.equal(message.playbookSaved, true);
});

test("conversationContentChanged ignores reopening a conversation without new messages", () => {
  const stored = storageSafeMessages([
    { role: "user", content: "이번 주 잔여 공수", createdAt: "2026-08-20T01:00:00.000Z" },
    { role: "assistant", content: "12시간 남았습니다.", createdAt: "2026-08-20T01:00:03.000Z" },
  ]);
  // 대화를 다시 열면 저장된 메시지를 그대로 state.messages 로 복사한다.
  assert.equal(conversationContentChanged(stored, [...stored]), false);
});

test("conversationContentChanged ignores fields the server drops when empty", () => {
  const stored = storageSafeMessages([{ role: "user", content: "잔여 공수" }]);
  // 서버는 exclude_none 으로 비어 있는 필드를 지운 채 대화를 돌려준다.
  const fromServer = [{ role: "user", content: "잔여 공수", sources: [], followUps: [] }];
  assert.equal(conversationContentChanged(fromServer, stored), false);
});

test("conversationContentChanged detects new and edited messages", () => {
  const stored = storageSafeMessages([{ role: "user", content: "잔여 공수" }]);
  assert.equal(
    conversationContentChanged(stored, [...stored, { role: "assistant", content: "12시간" }]),
    true,
  );
  assert.equal(
    conversationContentChanged(stored, [{ role: "user", content: "누적 공수" }]),
    true,
  );
});

test("conversationContentChanged ignores streaming placeholders", () => {
  const stored = storageSafeMessages([{ role: "user", content: "잔여 공수" }]);
  const streaming = [...stored, { role: "assistant", content: "", loading: true }];
  assert.equal(conversationContentChanged(stored, streaming), false);
});


test("message and sidebar clocks stay Korean when ICU returns English periods", () => {
  const vm = require("node:vm");
  const sandbox = { module: { exports: {} }, Intl: { DateTimeFormat: class {
    format() { return "AM 9:07"; }
  } } };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../backend/static/frontend_core.js"), "utf8"), sandbox);
  for (const [hour, expected] of [[0, "오전 12:07"], [9, "오전 9:07"], [12, "오후 12:07"], [23, "오후 11:07"]]) {
    const date = new Date(2026, 9, 6, hour, 7);
    assert.equal(sandbox.module.exports.messageClockLabel(date.toISOString()), expected);
    assert.equal(sandbox.module.exports.conversationTimeLabel({ updatedAt: date.toISOString() }, date), expected);
  }
});
