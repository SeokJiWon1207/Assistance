import { icon } from "./icons.js?v=20260911-6";

const STEPS = [
  ["질문 해석", ["analysis", "routing"]],
  ["자료 검색", ["retrieval", "filtering"]],
  ["답변 작성", ["generation", "verification", "comparison"]],
];

export function progressSteps(message) {
  const current = STEPS.findIndex(([, stages]) => stages.includes(message.stage));
  if (current < 0) return [];
  return STEPS.map(([label, stages], index) => ({ label, current: index === current,
    done: index < current && stages.some((stage) => (message.stageHistory || []).includes(stage)) }));
}

export function renderProgressSteps(message) {
  const steps = progressSteps(message);
  if (!steps.length) return "";
  const items = steps.map(({ label, current, done }) => {
    const marker = done ? icon("check", 14, "is-bold") : '<span class="progress-step-dot" aria-hidden="true"></span>';
    return `<li class="progress-step ${done ? "is-done" : current ? "is-current" : ""}"${current ? ' aria-current="step"' : ""}>${marker}${label}</li>`;
  }).join("");
  const count = Number.isInteger(message.evidenceCount) && message.evidenceCount >= 0
    ? `<p class="ab-method-note">답변에 사용할 원문 ${message.evidenceCount}건 확보</p>` : "";
  return `<ol class="progress-steps" aria-label="답변 진행 단계">${items}</ol>${count}`;
}

// 답변 생성 경과 시간. 시작 시각만 메시지에 두고 표시 문구는 매 초 다시 계산한다.
export const SLOW_ANSWER_SECONDS = 20;

export function elapsedSeconds(startedAt, now = Date.now()) {
  const start = Number(startedAt);
  if (!Number.isFinite(start) || start <= 0) return null;
  return Math.max(0, Math.floor((Number(now) - start) / 1000));
}

function durationText(seconds) {
  const value = Math.max(0, Math.round(Number(seconds) || 0));
  return value < 60 ? `${value}초` : `${Math.floor(value / 60)}분 ${value % 60}초`;
}

export function elapsedLabel(seconds) {
  return `${durationText(seconds)}째 작업 중`;
}

export function answerDurationLabel(seconds) {
  const value = Number(seconds);
  return Number.isFinite(value) && value >= 0 ? `${durationText(value)} 만에 답변` : "";
}

const SLOW_HINT = "평소보다 오래 걸리고 있어요. 조회 범위가 넓으면 시간이 더 걸릴 수 있고, 기다리기 어렵다면 중지한 뒤 범위를 좁혀 질문해 보세요.";

// 매 초 바뀌는 숫자는 스크린리더가 계속 읽지 않도록 숨기고, 느려졌다는 안내만 한 번 읽힌다.
export function renderElapsed(message, now = Date.now()) {
  const seconds = elapsedSeconds(message.startedAt, now);
  if (seconds === null) return "";
  const slow = seconds >= SLOW_ANSWER_SECONDS;
  return `<span class="answer-elapsed" data-elapsed-since="${Number(message.startedAt)}" aria-hidden="true">${icon("clock", 14)}<span>${elapsedLabel(seconds)}</span></span>`
    + `<p class="answer-slow-hint" data-elapsed-slow-hint${slow ? "" : " hidden"}>${SLOW_HINT}</p>`;
}

export function refreshElapsed(container, now = Date.now()) {
  for (const badge of container?.querySelectorAll?.("[data-elapsed-since]") || []) {
    const seconds = elapsedSeconds(badge.dataset.elapsedSince, now);
    if (seconds === null) continue;
    const label = badge.querySelector("span");
    if (label) label.textContent = elapsedLabel(seconds);
    const hint = badge.parentElement?.querySelector("[data-elapsed-slow-hint]");
    if (hint && seconds >= SLOW_ANSWER_SECONDS) hint.hidden = false;
  }
}

// 완료된 답변의 동작 줄 끝에 "N초 만에 답변"을 붙인다.
export function appendAnswerDuration(actions, message) {
  const duration = answerDurationLabel(message?.answerSeconds);
  if (!actions || !duration) return;
  const elapsed = document.createElement("span");
  elapsed.className = "message-elapsed";
  elapsed.textContent = duration;
  actions.appendChild(elapsed);
}

// 답변을 만드는 동안만 1초마다 경과 시간 문구를 고친다. 메시지 전체를 다시 그리지 않는다.
let elapsedTimer = null;

export function syncElapsedTicker(busy, container) {
  if (busy && !elapsedTimer) {
    elapsedTimer = globalThis.setInterval?.(() => refreshElapsed(container), 1000) ?? null;
  } else if (!busy && elapsedTimer) {
    globalThis.clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}
