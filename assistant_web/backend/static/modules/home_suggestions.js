// 추천 원문은 데이터로만 표시한다. 선택은 최신 근거 확인 후 입력창에만 반영한다.
import { state, els, apiFetch, automaticSourceTools } from "./runtime.js?v=20260911-6";
import { escapeHtml } from "./chat_rendering.js?v=20260911-6";

let bundle = { state: "disabled", items: [] };
const removedIds = new Set();

function acceptBundle(data) {
  bundle = { ...data, items: (data.items || []).filter(item => !removedIds.has(item.id)) };
}
let notice = "", sessionKey = "", sequence = 0, draftVersion = 0;
let controller = null, timer = null, expiryTimer = null, lastHost = null;
let initialized = false, preparing = false, refreshing = false, polls = 0;
const host = () => els.messages?.querySelector("[data-home-suggestions]");
const homeKey = () => JSON.stringify([state.currentUser?.uid, state.activeConversationId]);
const onHome = () => Boolean(state.currentUser?.uid && !state.initializing && !state.busy && !state.messages.length
  && (!els.appShell?.dataset.view || els.appShell.dataset.view === "chat") && host());

// 첫 출처의 제목은 추천 제목에 이미 들어 있으므로 원천 이름만 보인다. 전체 제목은 보조 기술과 툴팁에 남긴다.
function sourceLink(source, index) {
  try {
    const url = new URL(source.url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return "";
    if (source.type === "notion" && !["notion.so", "notion.site", "notion.com"].some(d => url.hostname === d || url.hostname.endsWith("." + d))) return "";
    if (!["mantis", "notion"].includes(source.type)) return "";
    const role = source.relation === "contributor" ? " (기여)" : source.relation === "related" ? " (관련)" : "";
    const name = `${source.type === "mantis" ? "Mantis" : "Notion"}${role}`;
    const full = `${name} · ${source.title}`;
    const observed = new Date(source.observedAt);
    const tip = Number.isNaN(observed.getTime()) ? full : `${full}\n자료 확인: ${observed.toLocaleString("ko-KR")}`;
    return `<a class="home-suggestion-source" data-source-type="${source.type}" href="${escapeHtml(url.href)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(tip)}" aria-label="${escapeHtml(full)} (새 창)">${escapeHtml(index === 0 ? name : full)} ↗</a>`;
  } catch { return ""; }
}

export function homeSuggestionsHtml(data = bundle, message = notice) {
  const items = (data.items || []).filter(item => Date.parse(item.expiresAt) > Date.now()).slice(0, 3);
  if (data.state === "disabled") return "";
  const status = message || (data.state === "pending" ? "업무 추천을 확인하고 있어요." : data.state === "partial"
    ? "현재 확인할 수 있는 자료를 바탕으로 추천했어요." : !items.length ? "지금 표시할 업무 추천이 없어요." : "");
  return `<div class="home-suggestions-heading"><h4>이어 할 만한 업무</h4><button type="button" data-suggestion-action="refresh" ${refreshing || data.state === "pending" && polls < 12 ? "disabled" : ""}>새로고침</button></div>
    <p class="home-suggestions-status" role="status">${escapeHtml(status)}</p>
    <ul>${items.map(item => `<li>
      <div class="home-suggestion-row"><button class="home-suggestion-title" type="button" data-suggestion-action="prepare" data-suggestion-id="${escapeHtml(item.id)}" ${preparing ? "disabled" : ""}>${escapeHtml(item.title)}</button>
      <button class="home-suggestion-hide" type="button" data-suggestion-action="dismiss" data-suggestion-id="${escapeHtml(item.id)}" aria-label="${escapeHtml(item.title)} 추천 숨기기">숨김</button></div>
      <div class="home-suggestion-meta">${(item.sources || []).map(sourceLink).join("")}
      <details><summary>질문 보기</summary><p>${escapeHtml(item.promptPreview)}</p></details></div>
    </li>`).join("")}</ul>`;
}

function paint() {
  const element = host();
  if (!element) return;
  lastHost = element;
  // 폴링이 키보드 포커스나 열린 미리보기를 잃게 하지 않는다.
  const focused = element.contains?.(document.activeElement) ? document.activeElement : null;
  const action = focused?.dataset?.suggestionAction, id = focused?.dataset?.suggestionId;
  const opened = new Set([...element.querySelectorAll?.("li") || []].filter(row => row.querySelector("details")?.open)
    .map(row => row.querySelector("[data-suggestion-id]")?.dataset.suggestionId));
  element.innerHTML = homeSuggestionsHtml();
  element.hidden = bundle.state === "disabled";
  for (const row of element.querySelectorAll?.("li") || []) {
    if (opened.has(row.querySelector("[data-suggestion-id]")?.dataset.suggestionId)) row.querySelector("details").open = true;
  }
  if (action) [...element.querySelectorAll?.("[data-suggestion-action]") || []]
    .find(button => button.dataset.suggestionAction === action && button.dataset.suggestionId === id)?.focus();
}

function valid(version) { return version === sequence && onHome() && sessionKey === homeKey(); }
async function request(path = "", method = "GET") {
  const response = await apiFetch(`/api/suggestions${path}`, { method, signal: controller?.signal });
  if (!response.ok) { const error = new Error("추천 요청 실패"); error.status = response.status; throw error; }
  return response.json();
}

function schedule(data, version) {
  clearTimeout(timer); clearTimeout(expiryTimer);
  if (data.state === "pending") {
    if (polls < 12) timer = setTimeout(() => { polls++; void load(version, false); }, 5000);
    else { notice = "추천 확인이 지연되고 있어요. 잠시 후 새로고침해 주세요."; paint(); }
  } else if (data.items?.length) {
    const expiry = Math.min(...data.items.map(item => Date.parse(item.expiresAt)));
    if (Number.isFinite(expiry) && expiry > Date.now()) expiryTimer = setTimeout(() => { if (valid(version)) void load(version, true); }, Math.min(600000, expiry - Date.now() + 50));
  }
}

async function load(version, mayRefresh) {
  try {
    const data = await request();
    if (!valid(version)) return;
    acceptBundle(data); notice = ""; paint();
    if (mayRefresh && data.state === "unavailable" && !data.retryAfterSeconds) { await refresh(version); return; }
    schedule(data, version);
  } catch (error) {
    if (!valid(version) || error.name === "AbortError") return;
    bundle = { state: "unavailable", items: [] }; notice = "업무 추천을 불러오지 못했어요. 새로고침해 주세요."; paint();
  }
}

async function refresh(version) {
  if (refreshing || !valid(version)) return;
  refreshing = true; polls = 0; notice = ""; paint();
  try {
    const data = await request("/refresh", "POST");
    if (!valid(version)) return;
    acceptBundle(data);
    if (data.state !== "pending" && data.retryAfterSeconds) notice = "잠시 후 다시 새로고침해 주세요.";
    schedule(data, version);
  } catch (error) {
    if (!valid(version) || error.name === "AbortError") return;
    notice = error.status === 429 ? "추천 요청이 많아요. 잠시 후 다시 시도해 주세요." : "추천을 갱신하지 못했어요. 잠시 후 다시 시도해 주세요.";
  } finally { if (valid(version)) { refreshing = false; paint(); } }
}

async function prepare(id) {
  if (preparing || !onHome() || state.editingMessageIndex !== null) return;
  if (els.input.value.trim() || state.pendingTemplateId) { notice = "작성 중인 질문을 먼저 보내거나 비운 뒤 추천을 선택해 주세요."; paint(); return; }
  const item = bundle.items.find(value => value.id === id);
  if (!item) return;
  const tools = automaticSourceTools();
  if (item.sources.some(source => tools[source.type] === false)) { notice = "이 추천에는 Mantis·Notion 조회가 필요해요. 사용할 원천 설정을 확인해 주세요."; paint(); return; }
  const version = sequence, draft = draftVersion, value = els.input.value, template = state.pendingTemplateId;
  let removed = false;
  preparing = true; notice = "최신 자료를 확인하고 있어요."; paint();
  try {
    const data = await request(`/${encodeURIComponent(id)}/prepare`, "POST");
    if (!valid(version)) return;
    if (draft !== draftVersion || value !== els.input.value || template !== state.pendingTemplateId || state.editingMessageIndex !== null) { notice = "작성 중인 질문을 유지했어요. 입력창을 비운 뒤 다시 선택해 주세요."; return; }
    if (item.sources.some(source => automaticSourceTools()[source.type] === false)) { notice = "사용할 원천 설정을 확인한 뒤 다시 선택해 주세요."; return; }
    if (typeof data.prompt !== "string" || !data.prompt.trim()) throw new Error("빈 질문");
    state.pendingTemplateId = null;
    els.input.value = data.prompt;
    els.input.dispatchEvent(new Event("input", { bubbles: true }));
    els.input.focus();
    notice = "질문을 입력창에 넣었어요. 확인한 뒤 전송해 주세요.";
  } catch (error) {
    if (!valid(version) || error.name === "AbortError") return;
    if ([404, 410].includes(error.status)) {
      removed = true;
      notice = "자료가 바뀌었거나 만료된 추천을 목록에서 뺐어요.";
    } else notice = "최신 자료를 확인하지 못했어요. 잠시 후 다시 선택해 주세요.";
  } finally { if (valid(version)) { preparing = false; paint(); } }
  if (removed && valid(version)) await removeAndFill(id, version, "다음 추천을 불러오지 못했어요. 새로고침해 주세요.");
}

// 서버에서 빠진 항목을 화면에서 지우고, 캐시에 남은 예비 추천으로 빈자리를 채운다. 새 생성은 요청하지 않는다.
async function removeAndFill(id, version, failNotice) {
  removedIds.add(id);
  bundle.items = bundle.items.filter(item => item.id !== id); paint();
  try {
    const data = await request();
    if (!valid(version)) return;
    acceptBundle(data); schedule(bundle, version); paint();
  } catch (error) {
    if (valid(version) && error.name !== "AbortError") { notice = failNotice; paint(); }
  }
}

async function dismiss(id) {
  const version = sequence;
  try {
    await request(`/${encodeURIComponent(id)}/dismiss`, "POST");
    if (!valid(version)) return;
    notice = "추천을 숨겼어요.";
    await removeAndFill(id, version, "추천은 숨겼지만 다음 추천을 불러오지 못했어요. 새로고침해 주세요.");
  } catch (error) {
    if (valid(version) && error.name !== "AbortError") { notice = "추천을 숨기지 못했어요. 다시 시도해 주세요."; paint(); }
  }
}

export function stopHomeSuggestions() {
  sequence++; controller?.abort(); controller = null;
  clearTimeout(timer); clearTimeout(expiryTimer); timer = null; expiryTimer = null;
  sessionKey = ""; lastHost = null; preparing = false; refreshing = false; polls = 0;
  bundle = { state: "disabled", items: [] }; notice = "";
  removedIds.clear();
}

function sync() {
  if (!onHome()) { if (sessionKey) { stopHomeSuggestions(); paint(); } return; }
  if (sessionKey === homeKey()) { if (lastHost !== host()) paint(); return; }
  stopHomeSuggestions(); sessionKey = homeKey(); controller = new AbortController();
  void load(sequence, true);
}

export function initHomeSuggestions() {
  if (!initialized) {
    initialized = true;
    els.input?.addEventListener("input", () => { draftVersion++; });
    els.messages?.addEventListener("click", event => {
      const button = event.target.closest("[data-suggestion-action]");
      if (!button || !onHome()) return;
      const { suggestionAction: action, suggestionId: id } = button.dataset;
      if (action === "prepare") void prepare(id);
      else if (action === "dismiss") void dismiss(id);
      else if (action === "refresh") void refresh(sequence);
    });
    globalThis.addEventListener("conversations:rendered", sync);
    globalThis.addEventListener("pagehide", stopHomeSuggestions);
    const observer = new MutationObserver(sync);
    if (els.messages) observer.observe(els.messages, { childList: true });
    if (els.appShell) observer.observe(els.appShell, { attributes: true, attributeFilter: ["data-view"] });
  }
  sync();
}
