// 화면 머리의 '데이터 기준' 배지. 대화·내 업무·대시보드·관리자 센터가 같은 상태 이름과
// 같은 모양을 쓰도록 배지와 펼침 창을 여기서만 그린다.
import { formatUiDateTime } from "./ui_format.js?v=20260911-6";
import { icon } from "./icons.js?v=20260911-6";

const STATE_META = {
  fresh: { label: "최신", title: "모두 최신이에요" },
  refreshing: { label: "갱신 중", title: "최신 데이터로 바꾸는 중이에요" },
  partial: { label: "일부만 확인", title: "일부 이슈만 확인했어요" },
  snapshot: { label: "저장본", title: "저장해 둔 데이터를 보여 드려요" },
  warning: { label: "확인 필요", title: "확인이 필요한 연결이 있어요" },
  error: { label: "연결 오류", title: "데이터를 불러오지 못했어요" },
  loading: { label: "확인 중", title: "데이터 상태를 확인하고 있어요" },
};

const TONE_ICONS = { ok: "check", warn: "warning", error: "close" };

const retryHandlers = new WeakMap();
let badgeSequence = 0;
let listenersInstalled = false;

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// 오늘이면 "오후 2:02", 아니면 "9/29 오후 2:02"
function formatBadgeTime(value) {
  const date = new Date(value);
  return !value || Number.isNaN(date.getTime()) ? "" : formatUiDateTime(value, { compact: true });
}

// "2026-10-01" → "10월 1일"
function formatBadgeDate(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${Number(match[2])}월 ${Number(match[3])}일` : "";
}

// 서버의 source(live/snapshot/error)·search_complete·refreshPending 값을 배지 상태 하나로 줄인다.
function dataBadgeState({ source, complete = true, refreshing = false, loading = false } = {}) {
  if (loading) return "loading";
  if (source === "error") return "error";
  if (refreshing) return "refreshing";
  if (source === "snapshot") return "snapshot";
  if (complete === false) return "partial";
  return "fresh";
}

// Mantis 조회 결과를 보여 주는 화면(대시보드·내 업무)이 같은 문장을 쓰도록 상태별 줄과 안내를 만든다.
function mantisBadgeDetails(state, { time = "", reason = "" } = {}) {
  const text = {
    fresh: `${time ? `${time}에 ` : ""}직접 조회 · 전체 범위`,
    refreshing: "다시 조회하는 중 · 끝나면 화면이 바뀝니다",
    partial: "전체 범위를 다 받지 못함 · 확인한 범위 기준",
    snapshot: `지금은 연결이 안 돼 ${time || "이전"} 저장본 사용`,
    error: reason || "응답 없음",
    loading: "조회하는 중",
  }[state] || "";
  const tone = { fresh: "ok", refreshing: "pending", partial: "warn", snapshot: "warn", error: "error", loading: "pending" }[state] || "muted";
  const note = {
    partial: "건수는 확인한 범위 기준이라 실제보다 적을 수 있어요.",
    snapshot: `${time || "저장 시각"} 이후 바뀐 내용은 반영되지 않았어요.`,
    error: "잠시 후 다시 조회해 주세요. 계속되면 관리자에게 알려 주세요.",
  }[state] || "";
  return { source: { name: "Mantis", tone, text }, note, retry: ["partial", "snapshot", "error"].includes(state) };
}

function closeAll(except = null) {
  document.querySelectorAll(".data-badge-pop:not([hidden])").forEach((pop) => {
    if (pop === except) return;
    pop.hidden = true;
    pop.closest(".data-badge-slot")?.querySelector("[data-data-badge-toggle]")?.setAttribute("aria-expanded", "false");
  });
}

function installListeners() {
  if (listenersInstalled || typeof document === "undefined") return;
  listenersInstalled = true;
  document.addEventListener("click", (event) => {
    const toggle = event.target.closest?.("[data-data-badge-toggle]");
    if (toggle) {
      const pop = document.getElementById(toggle.getAttribute("aria-controls"));
      if (!pop) return;
      closeAll(pop);
      pop.hidden = !pop.hidden;
      toggle.setAttribute("aria-expanded", String(!pop.hidden));
      return;
    }
    const retry = event.target.closest?.("[data-data-badge-retry]");
    if (retry) {
      const slot = retry.closest(".data-badge-slot");
      closeAll();
      retryHandlers.get(slot)?.();
      return;
    }
    if (!event.target.closest?.(".data-badge-pop")) closeAll();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = document.querySelector(".data-badge-pop:not([hidden])");
    if (!open) return;
    closeAll();
    open.closest(".data-badge-slot")?.querySelector("[data-data-badge-toggle]")?.focus();
  });
}

// sources: [{ name, tone: "ok" | "warn" | "error" | "pending" | "muted", text }]
function renderDataBadge(slot, { state = "fresh", time = "", detailTime = "", sources = [], note = "", onRetry = null, label = "" } = {}) {
  if (!slot) return;
  installListeners();
  const meta = STATE_META[state] || STATE_META.fresh;
  const popId = slot.dataset.badgeId || `data-badge-${++badgeSequence}`;
  slot.dataset.badgeId = popId;
  slot.classList.add("data-badge-slot");
  if (onRetry) retryHandlers.set(slot, onRetry);
  else retryHandlers.delete(slot);
  const text = [label || meta.label, time].filter(Boolean).join(" · ");
  const rows = sources.map((source) => {
    const mark = TONE_ICONS[source.tone] ? icon(TONE_ICONS[source.tone], 14) : `<span aria-hidden="true">${source.tone === "pending" ? "…" : "–"}</span>`;
    return `<li class="is-${escapeHtml(source.tone || "muted")}">${mark}<b>${escapeHtml(source.name)}</b><span>${escapeHtml(source.text)}</span></li>`;
  }).join("");
  slot.innerHTML = `
    <button type="button" class="data-badge is-${state}" data-data-badge-toggle aria-expanded="false" aria-controls="${popId}" aria-haspopup="dialog" title="데이터 기준: ${escapeHtml(text)}">
      <i class="data-badge-dot" aria-hidden="true"></i><span>${escapeHtml(text)}</span>${icon("chevron-down", 14)}
    </button>
    <div class="data-badge-pop" id="${popId}" role="dialog" aria-label="데이터 기준 세부" hidden>
      <div class="data-badge-pop-head"><strong>${escapeHtml(meta.title)}</strong>${detailTime ? `<span>${escapeHtml(detailTime)}</span>` : ""}</div>
      ${rows ? `<ul>${rows}</ul>` : ""}
      ${note ? `<p class="data-badge-note is-${state}">${escapeHtml(note)}</p>` : ""}
      ${onRetry ? `<button type="button" class="data-badge-retry" data-data-badge-retry>${icon("refresh", 16)}다시 조회</button>` : ""}
    </div>`;
}

export { STATE_META, dataBadgeState, formatBadgeDate, formatBadgeTime, mantisBadgeDetails, renderDataBadge };
