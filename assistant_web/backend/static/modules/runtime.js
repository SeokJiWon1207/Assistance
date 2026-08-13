import { apiFetch } from "./api.js";
import { initialCurrentUser } from "./auth.js";

const {
  storageSafeMessages,
  deriveConversationTitle,
  buildChatMessages,
  buildTeamTemplateQuestion,
  parseSseBuffer,
} = globalThis.AssistantFrontendCore;

export const state = {
  models: [],
  messages: [],
  busy: false,
  sourceMap: new Map(),
  evaluationCases: [],
  evaluationResults: new Map(),
  evaluationHistory: [],
  evaluationRunning: false,
  evaluationRunningCaseId: null,
  evaluationRunProgress: null,
  accuracyReports: [],
  accuracyReport: null,
  accuracyAnalytics: null,
  dataFreshness: null,
  accuracyLive: [],
  accuracyRunning: false,
  accuracyProgress: null,
  accuracyStatusMessage: "",
  validationLogs: [],
  validationSummary: null,
  feedbackSummary: null,
  evaluationTab: "cases",
  conversations: [],
  activeConversationId: null,
  editingMessageIndex: null,
  chatAbortController: null,
  activeTemplateId: null,
  pendingTemplateId: null,
  teamTemplatesError: "",
  feedbackMessageIndex: null,
  currentUser: initialCurrentUser(),
  myWorkspace: null,
  teamDashboard: null,
  workspaceMode: "personal",
  workspaceTab: "overview",
};

export const els = {
  modelSelect: document.querySelector("#modelSelect"),
  modelMeta: document.querySelector("#modelMeta"),
  statusLine: document.querySelector("#statusLine"),
  darkModeToggle: document.querySelector("#darkModeToggle"),
  themeModeLabel: document.querySelector("#themeModeLabel"),
  messages: document.querySelector("#messages"),
  scrollToTopBtn: document.querySelector("#scrollToTopBtn"),
  form: document.querySelector("#chatForm"),
  input: document.querySelector("#promptInput"),
  sendBtn: document.querySelector("#sendBtn"),
  temperature: document.querySelector("#temperature"),
  temperatureValue: document.querySelector("#temperatureValue"),
  answerStyle: document.querySelector("#answerStyle"),
  topP: document.querySelector("#topP"),
  topK: document.querySelector("#topK"),
  minP: document.querySelector("#minP"),
  maxTokens: document.querySelector("#maxTokens"),
  presencePenalty: document.querySelector("#presencePenalty"),
  frequencyPenalty: document.querySelector("#frequencyPenalty"),
  repetitionPenalty: document.querySelector("#repetitionPenalty"),
  seed: document.querySelector("#seed"),
  stop: document.querySelector("#stop"),
  showSources: document.querySelector("#showSources"),
  sourceDisplayLabel: document.querySelector("#sourceDisplayLabel"),
  abTestToggle: document.querySelector("#abTestToggle"),
  abTestLabel: document.querySelector("#abTestLabel"),
  searchLimit: document.querySelector("#searchLimit"),
  searchLimitValue: document.querySelector("#searchLimitValue"),
  resetOptionsBtn: document.querySelector("#resetOptionsBtn"),
  newChatBtn: document.querySelector("#newChatBtn"),
  brandHome: document.querySelector("#brandHome"),
  sidebar: document.querySelector(".sidebar"),
  sidebarMenuToggle: document.querySelector("#sidebarMenuToggle"),
  conversationList: document.querySelector("#conversationList"),
  activeConversationTitle: document.querySelector("#activeConversationTitle"),
  editNotice: document.querySelector("#editNotice"),
  cancelEditBtn: document.querySelector("#cancelEditBtn"),
  sourceModeSelect: document.querySelector("#sourceModeSelect"),
  sourceModeHint: document.querySelector("#sourceModeHint"),
  manualSourceControls: document.querySelector("#manualSourceControls"),
  mantisToggle: document.querySelector("#mantisToggle"),
  vectorToggle: document.querySelector("#vectorToggle"),
  evaluationBtn: document.querySelector("#evaluationBtn"),
  evaluationModal: document.querySelector("#evaluationModal"),
  evaluationModalClose: document.querySelector("#evaluationModalClose"),
  evaluationSummary: document.querySelector("#evaluationSummary"),
  evaluationContent: document.querySelector("#evaluationContent"),
  runEvaluationBtn: document.querySelector("#runEvaluationBtn"),
  chromaModal: document.querySelector("#chromaModal"),
  chromaModalMeta: document.querySelector("#chromaModalMeta"),
  chromaModalTitle: document.querySelector("#chromaModalTitle"),
  chromaModalContent: document.querySelector("#chromaModalContent"),
  chromaModalClose: document.querySelector("#chromaModalClose"),
  templateModal: document.querySelector("#templateModal"),
  templateModalTitle: document.querySelector("#templateModalTitle"),
  templateModalDescription: document.querySelector("#templateModalDescription"),
  templateModalClose: document.querySelector("#templateModalClose"),
  templateForm: document.querySelector("#templateForm"),
  templateFields: document.querySelector("#templateFields"),
  templateQuestionPreview: document.querySelector("#templateQuestionPreview"),
  templateCancelBtn: document.querySelector("#templateCancelBtn"),
  feedbackModal: document.querySelector("#feedbackModal"),
  feedbackModalClose: document.querySelector("#feedbackModalClose"),
  feedbackForm: document.querySelector("#feedbackForm"),
  feedbackQuestion: document.querySelector("#feedbackQuestion"),
  feedbackComment: document.querySelector("#feedbackComment"),
  feedbackCancelBtn: document.querySelector("#feedbackCancelBtn"),
  myWorkBtn: document.querySelector("#myWorkBtn"),
  teamDashboardBtn: document.querySelector("#teamDashboardBtn"),
  workspaceUnreadBadge: document.querySelector("#workspaceUnreadBadge"),
  workspaceModal: document.querySelector("#workspaceModal"),
  workspaceModalClose: document.querySelector("#workspaceModalClose"),
  workspaceModalEyebrow: document.querySelector("#workspaceModalEyebrow"),
  workspaceModalTitle: document.querySelector("#workspaceModalTitle"),
  workspaceRefreshBtn: document.querySelector("#workspaceRefreshBtn"),
  workspaceTabs: document.querySelector("#workspaceTabs"),
  workspaceTabUnread: document.querySelector("#workspaceTabUnread"),
  workspaceContent: document.querySelector("#workspaceContent"),
};

export const defaultOptions = {
  temperature: "0.3",
  answerStyle: "normal",
  topP: "",
  topK: "",
  minP: "",
  maxTokens: "",
  presencePenalty: "",
  frequencyPenalty: "",
  repetitionPenalty: "",
  seed: "",
  stop: "",
  showSources: true,
  abTest: false,
  searchLimit: "5",
  sourceMode: "auto",
  mantis: true,
  notion: true,
  vectorDb: true,
};

export const themeStorageKey = "team-ai-assistant-theme";
export const conversationsStorageKeyBase = "team-ai-assistant-conversations-v2";
export const activeConversationStorageKeyBase = "team-ai-assistant-active-conversation-v2";
export const maxStoredConversations = 20;
export const conversationSyncTimers = new Map();

export function scopedStorageKey(base, userId) {
  return `${base}:${userId || "guest"}`;
}

export function createConversationRecord(now = new Date()) {
  const timestamp = now.toISOString();
  return {
    id: globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    title: "새 대화",
    createdAt: timestamp,
    updatedAt: timestamp,
    favorite: false,
    messages: [],
  };
}

export function automaticSourceTools() {
  return { mantis: true, notion: true, vectorDb: true };
}

export const WORKSPACE_ENDPOINTS = Object.freeze({
  personal: "/api/workspace/me",
  team: "/api/workspace/team",
});

export const ADMIN_ENDPOINTS = Object.freeze({
  evaluationCases: "/api/evaluation/cases",
  validationLogs: "/api/validation/logs?limit=30",
  validationSummary: "/api/validation/summary?limit=500",
  evaluationHistory: "/api/evaluation/history?limit=8",
  feedbackSummary: "/api/feedback/summary?limit=200",
});

export {
  apiFetch,
  buildChatMessages,
  buildTeamTemplateQuestion,
  deriveConversationTitle,
  parseSseBuffer,
  storageSafeMessages,
};
