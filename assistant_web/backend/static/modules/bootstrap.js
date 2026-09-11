import "./events.js";
import { state, els, defaultOptions } from "./runtime.js";
import { loadConversations, loadMyWorkspace } from "./workspace.js";
import { loadTheme, setAbComparison, renderMessages, loadModels, loadDataStatus, loadTeamTemplates } from "./chat.js";
import { initDashboard } from "./dashboard.js";

async function bootstrapApplication(authState = {}) {
  state.currentUser = authState.user || globalThis.AssistantCurrentUser || null;
  if (authState.enabled && state.currentUser?.role !== "admin") {
    els.evaluationBtn.hidden = true;
  }
  await Promise.all([
    loadConversations(),
    loadTeamTemplates().catch(() => {}),
  ]);
  if (state.currentUser?.uid) {
    loadMyWorkspace().catch(() => {});
  }
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
}

Promise.resolve(globalThis.AssistantAuthReady || { enabled: false, authenticated: false, user: null })
  .then(bootstrapApplication);
