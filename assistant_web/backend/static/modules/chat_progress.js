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
