(function initializeAuthentication(globalScope) {
  "use strict";

  const rawFetch = globalScope.fetch.bind(globalScope);
  let authConfig = { enabled: false, csrfCookie: "ai_csrf" };
  let authenticated = false;

  function readCookie(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    const pair = document.cookie.split(";").map((item) => item.trim()).find((item) => item.startsWith(prefix));
    return pair ? decodeURIComponent(pair.slice(prefix.length)) : "";
  }

  function apiFetch(input, init = {}) {
    const options = { credentials: "same-origin", ...init };
    const method = String(options.method || "GET").toUpperCase();
    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const headers = new Headers(options.headers || {});
      const csrfToken = readCookie(authConfig.csrfCookie || "ai_csrf");
      if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
      options.headers = headers;
    }
    return rawFetch(input, options).then((response) => {
      const path = typeof input === "string" ? input : String(input?.url || "");
      if (authenticated && response.status === 401 && !path.includes("/api/auth/")) {
        globalScope.setTimeout(() => globalScope.location.reload(), 50);
      }
      return response;
    });
  }

  globalScope.AssistantApiFetch = apiFetch;
  // 관리자 센터에서 내 정보를 고치면 사이드바 프로필도 같이 바꾼다.
  globalScope.AssistantApplySignedInUser = (user) => applySignedInUser(user);

  function setLoginError(message, success = false) {
    const element = document.querySelector("#loginError");
    if (!element) return;
    element.textContent = message || "";
    element.hidden = !message;
    element.classList.toggle("is-success", success);
  }

  function firebaseErrorMessage(code) {
    const messages = {
      EMAIL_NOT_FOUND: "이메일 또는 비밀번호를 확인해 주세요.",
      INVALID_PASSWORD: "이메일 또는 비밀번호를 확인해 주세요.",
      INVALID_LOGIN_CREDENTIALS: "이메일 또는 비밀번호를 확인해 주세요.",
      USER_DISABLED: "비활성화된 계정입니다. 관리자에게 문의해 주세요.",
      TOO_MANY_ATTEMPTS_TRY_LATER: "로그인 시도가 많습니다. 잠시 후 다시 시도해 주세요.",
      OPERATION_NOT_ALLOWED: "Firebase에서 이메일 로그인을 활성화해 주세요.",
    };
    return messages[code] || "로그인하지 못했습니다. 입력 내용을 확인해 주세요.";
  }

  async function firebasePasswordLogin(email, password) {
    const response = await rawFetch(
      `https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${encodeURIComponent(authConfig.firebaseApiKey)}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, returnSecureToken: true }),
      },
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.idToken) {
      throw new Error(firebaseErrorMessage(data?.error?.message));
    }
    return data.idToken;
  }

  const ROLE_LABELS = { member: "팀원", manager: "팀장", admin: "관리자" };

  function applySignedInUser(user) {
    const panel = document.querySelector("#signedInUser");
    if (!panel || !user) return;
    const label = user.displayName || user.email || "사용자";
    document.querySelector("#signedInUserName").textContent = label;
    const roleLabel = ROLE_LABELS[user.role] || user.role || "";
    document.querySelector("#signedInUserMeta").textContent = `${roleLabel}${user.team ? ` · ${user.team}` : ""}`;
    document.querySelector("#signedInUserAvatar").textContent = label.trim().slice(0, 1).toUpperCase();
    panel.hidden = false;
  }

  function finishAuthentication(user = null) {
    authenticated = Boolean(user);
    globalScope.AssistantCurrentUser = user;
    applySignedInUser(user);
    document.querySelector("#authGate").hidden = true;
    document.body.classList.remove("auth-pending");
    return { enabled: authConfig.enabled, authenticated, user };
  }

  function showLogin() {
    document.querySelector("#authChecking").hidden = true;
    document.querySelector("#loginForm").hidden = false;
    document.querySelector("#loginEmail")?.focus();
  }

  function setupSessionActions() {
    const logoutButton = document.querySelector("#logoutBtn");
    const modal = document.querySelector("#logoutConfirmModal");
    const confirmButton = document.querySelector("#logoutConfirmBtn");
    const cancelButton = document.querySelector("#logoutCancelBtn");
    const error = document.querySelector("#logoutConfirmError");
    let focusManager;
    const focusReady = import("./modules/dialog_focus.js?v=20260911-6").then((module) => { focusManager = module; });
    const close = () => {
      modal.hidden = true;
      document.body.classList.remove("modal-open");
      error.hidden = true;
      error.textContent = "";
      focusManager?.closeDialogFocus(modal);
      logoutButton?.focus();
    };
    const open = () => {
      modal.hidden = false;
      document.body.classList.add("modal-open");
      error.hidden = true;
      error.textContent = "";
      cancelButton?.focus();
      focusReady.then(() => {
        if (!modal.hidden) focusManager.openDialogFocus(modal, { opener: logoutButton, initialFocus: cancelButton, onClose: close });
      });
    };
    logoutButton?.addEventListener("click", open);
    cancelButton?.addEventListener("click", close);
    document.querySelector("#logoutConfirmClose")?.addEventListener("click", close);
    modal?.addEventListener("click", (event) => { if (event.target === modal) close(); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.hidden) {
        // 확인 창만 닫고, 뒤에 열린 내 계정 메뉴까지 같은 Esc로 닫히지 않게 한다.
        event.stopImmediatePropagation();
        close();
      }
    });
    confirmButton?.addEventListener("click", async () => {
      confirmButton.disabled = true;
      cancelButton.disabled = true;
      confirmButton.textContent = "로그아웃 중…";
      try {
        const response = await apiFetch("/api/auth/logout", { method: "POST" });
        if (!response.ok) throw new Error("로그아웃 요청을 처리하지 못했습니다.");
        globalScope.location.reload();
      } catch (logoutError) {
        error.textContent = logoutError.message;
        error.hidden = false;
        confirmButton.disabled = false;
        cancelButton.disabled = false;
        confirmButton.textContent = "로그아웃";
      }
    });
  }

  function waitForLogin() {
    showLogin();
    return new Promise((resolve) => {
      const form = document.querySelector("#loginForm");
      const submit = document.querySelector("#loginSubmit");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        setLoginError("");
        submit.disabled = true;
        submit.textContent = "로그인 중…";
        try {
          const email = document.querySelector("#loginEmail").value.trim();
          const password = document.querySelector("#loginPassword").value;
          const idToken = await firebasePasswordLogin(email, password);
          const response = await apiFetch("/api/auth/session", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ idToken }),
          });
          const data = await response.json().catch(() => ({}));
          if (!response.ok) throw new Error(data.detail || "로그인 세션을 만들지 못했습니다.");
          resolve(finishAuthentication(data.user));
        } catch (error) {
          setLoginError(error.message);
        } finally {
          submit.disabled = false;
          submit.textContent = "로그인";
        }
      });

      document.querySelector("#passwordResetBtn")?.addEventListener("click", async () => {
        const email = document.querySelector("#loginEmail").value.trim();
        if (!email) {
          setLoginError("비밀번호를 재설정할 이메일을 먼저 입력해 주세요.");
          return;
        }
        try {
          await apiFetch("/api/auth/password-reset", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email }),
          });
          setLoginError("등록된 계정이라면 비밀번호 재설정 메일이 발송됩니다.", true);
        } catch {
          setLoginError("비밀번호 재설정 요청을 처리하지 못했습니다.");
        }
      });
    });
  }

  async function bootstrapAuthentication() {
    setupSessionActions();
    try {
      const configResponse = await rawFetch("/api/auth/config", { credentials: "same-origin" });
      authConfig = await configResponse.json();
      if (!authConfig.enabled) return finishAuthentication(null);
      if (!authConfig.firebaseApiKey) throw new Error("FIREBASE_WEB_API_KEY가 설정되지 않았습니다.");

      const meResponse = await rawFetch("/api/auth/me", { credentials: "same-origin" });
      if (meResponse.ok) {
        const data = await meResponse.json();
        return finishAuthentication(data.user);
      }
      return waitForLogin();
    } catch (error) {
      document.querySelector("#authChecking").hidden = true;
      showLogin();
      setLoginError(`로그인 설정을 확인하지 못했습니다: ${error.message}`);
      return new Promise(() => {});
    }
  }

  globalScope.AssistantAuthReady = bootstrapAuthentication();
})(globalThis);
