const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  storageSafeMessages,
  deriveConversationTitle,
  buildChatMessages,
  buildTeamTemplateQuestion,
  parseSseBuffer,
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
    "이번 달 유진증권 MTS 미종료 이슈를 표로 보여줘",
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
