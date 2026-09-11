import "./events.js?v=20260911-6";
import { state, els, defaultOptions } from "./runtime.js?v=20260911-6";
import { loadConversations, loadMyWorkspace, showToast } from "./workspace.js?v=20260911-6";
import { loadTheme, setAbComparison, renderMessages, loadModels, loadDataStatus, loadTeamTemplates } from "./chat.js?v=20260911-6";
import { initDashboard, loadDashboard } from "./dashboard.js?v=20260911-6";

let loadedAppVersion = "";
let appUpdateNotified = false;

async function checkAppVersion() {
  const response = await fetch("/api/app-version", { credentials: "same-origin", cache: "no-store" });
  if (!response.ok) return;
  const version = String((await response.json()).version || "");
  if (!version) return;
  if (!loadedAppVersion) {
    loadedAppVersion = version;
    return;
  }
  if (version === loadedAppVersion || appUpdateNotified) return;
  appUpdateNotified = true;
  showToast("새 버전이 배포되었습니다. 화면을 새로고침해 주세요.", "warning", {
    label: "새로고침",
    onClick: () => globalThis.location.reload(),
  });
}

async function bootstrapApplication(authState = {}) {
  state.currentUser = authState.user || globalThis.AssistantCurrentUser || null;
  if (authState.enabled && state.currentUser?.role !== "admin") {
    els.evaluationBtn.hidden = true;
  }
  await Promise.all([
    loadConversations(),
    loadTeamTemplates().catch(() => {}),
    checkAppVersion().catch(() => {}),
  ]);
  const workspaceReady = state.currentUser?.uid
    ? loadMyWorkspace().catch(() => {})
    : Promise.resolve();
  loadDataStatus().catch(() => {
    els.statusLine.textContent = "데이터 상태를 확인하지 못했습니다.";
    els.statusLine.className = "status-warning";
  }).finally(() => {
    loadModels().catch((error) => {
      els.statusLine.textContent = error.message;
      els.statusLine.className = "status-danger";
    });
  });
  loadTheme();
  setAbComparison(defaultOptions.abTest);
  renderMessages();
  initDashboard();
  if (state.currentUser?.uid) workspaceReady.finally(() => loadDashboard());
  globalThis.setInterval(() => checkAppVersion().catch(() => {}), 60_000);
}

Promise.resolve(globalThis.AssistantAuthReady || { enabled: false, authenticated: false, user: null })
  .then(bootstrapApplication);
