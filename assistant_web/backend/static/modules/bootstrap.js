import "./events.js?v=20260911-6";
import { state, els, defaultOptions, canUseAdminTools } from "./runtime.js?v=20260911-6";
import { loadConversations, loadMyWorkspace, showToast } from "./workspace.js?v=20260911-6";
import { loadTheme, setAbComparison, renderMessages, loadModels, loadDataStatus } from "./chat.js?v=20260911-6";
import { loadTeamTemplates } from "./template_picker.js?v=20260911-6";
import { initDashboard, loadDashboard } from "./dashboard.js?v=20260911-6";
import { initAdminCenter, preloadAdminBadges } from "./admin.js?v=20260911-6";

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
  state.authEnabled = Boolean(authState.enabled);
  const adminToolsVisible = canUseAdminTools(authState.enabled, state.currentUser);
  state.adminToolsVisible = adminToolsVisible;
  els.adminGenerationSettings.hidden = !adminToolsVisible;
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
  initAdminCenter();
  initDashboard();
  // 승인 대기 배지는 관리자 센터를 열기 전에도 보여야 하므로 미리 불러 둔다.
  if (adminToolsVisible) preloadAdminBadges();
  if (state.currentUser?.uid) workspaceReady.finally(() => loadDashboard());
  globalThis.setInterval(() => checkAppVersion().catch(() => {}), 60_000);
}

Promise.resolve(globalThis.AssistantAuthReady || { enabled: false, authenticated: false, user: null })
  .then(bootstrapApplication);
