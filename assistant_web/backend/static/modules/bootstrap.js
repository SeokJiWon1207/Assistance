import "./events.js?v=20260911-6";
import { state, els, defaultOptions, canUseAdminTools } from "./runtime.js?v=20260911-6";
import { loadConversations, loadMyWorkspace, prefetchTeamBrief, showToast } from "./workspace.js?v=20260911-6";
import { loadTheme, setAbComparison, renderMessages, loadModels, loadDataStatus, renderChatDataBadge } from "./chat.js?v=20260911-6";
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
  // 템플릿·Notion 상태가 느리거나 실패해도 모델 준비는 바로 시작한다.
  loadModels().catch(() => {});
  // 이전 대화 복원은 새 질문의 메시지 배열을 교체하므로, 완료 전 제출만 막는다.
  await Promise.all([
    loadConversations().then(() => { state.initializing = false; }),
    loadTeamTemplates().catch(() => {}),
    checkAppVersion().catch(() => {}),
  ]);
  const workspaceReady = state.currentUser?.uid
    ? loadMyWorkspace().catch(() => {})
    : Promise.resolve();
  renderChatDataBadge({ loading: true });
  loadDataStatus().catch(() => renderChatDataBadge({ failed: true }));
  loadTheme();
  setAbComparison(defaultOptions.abTest);
  renderMessages();
  initAdminCenter();
  initDashboard();
  // 승인 대기 배지는 관리자 센터를 열기 전에도 보여야 하므로 미리 불러 둔다.
  if (adminToolsVisible) preloadAdminBadges();
  if (state.currentUser?.uid) {
    workspaceReady.finally(() => {
      loadDashboard();
      // 새 팀 브리핑이 있으면 '주간 보고' 탭에 점으로 알린다.
      prefetchTeamBrief();
    });
  }
  globalThis.setInterval(() => checkAppVersion().catch(() => {}), 60_000);
}

Promise.resolve(globalThis.AssistantAuthReady || { enabled: false, authenticated: false, user: null })
  .then(bootstrapApplication);
