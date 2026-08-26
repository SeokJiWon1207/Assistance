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

  function applySignedInUser(user) {
    const panel = document.querySelector("#signedInUser");
    if (!panel || !user) return;
    const label = user.displayName || user.email || "사용자";
    document.querySelector("#signedInUserName").textContent = label;
    document.querySelector("#signedInUserMeta").textContent = `${user.role}${user.team ? ` · ${user.team}` : ""}`;
    document.querySelector("#signedInUserAvatar").textContent = label.trim().slice(0, 1).toUpperCase();
    document.querySelector("#adminUsersBtn").hidden = user.role !== "admin";
    document.querySelector("#adminInviteBtn").hidden = user.role !== "admin";
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
    const close = () => {
      modal.hidden = true;
      document.body.classList.remove("modal-open");
      error.hidden = true;
      error.textContent = "";
      logoutButton?.focus();
    };
    const open = () => {
      modal.hidden = false;
      document.body.classList.add("modal-open");
      error.hidden = true;
      error.textContent = "";
      cancelButton?.focus();
    };
    logoutButton?.addEventListener("click", open);
    cancelButton?.addEventListener("click", close);
    document.querySelector("#logoutConfirmClose")?.addEventListener("click", close);
    modal?.addEventListener("click", (event) => { if (event.target === modal) close(); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.hidden) close();
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

  function setupInviteDialog() {
    const modal = document.querySelector("#inviteModal");
    const form = document.querySelector("#inviteForm");
    const close = () => {
      modal.hidden = true;
      document.body.classList.remove("modal-open");
      document.querySelector("#adminInviteBtn")?.focus();
    };
    document.querySelector("#adminInviteBtn")?.addEventListener("click", () => {
      form.reset();
      document.querySelector("#inviteResult").hidden = true;
      document.querySelector("#inviteError").hidden = true;
      // 이전에 복사했더라도 라벨은 초기 상태로 되돌려 연다.
      const copyButton = document.querySelector("#copyInviteUrlBtn");
      if (copyButton) copyButton.textContent = "복사";
      modal.hidden = false;
      document.body.classList.add("modal-open");
      document.querySelector("#inviteEmail")?.focus();
    });
    document.querySelector("#inviteModalClose")?.addEventListener("click", close);
    document.querySelector("#inviteCancelBtn")?.addEventListener("click", close);
    modal?.addEventListener("click", (event) => { if (event.target === modal) close(); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.hidden) close();
    });
    document.querySelector("#copyInviteUrlBtn")?.addEventListener("click", async () => {
      const input = document.querySelector("#inviteSetupUrl");
      try {
        await navigator.clipboard.writeText(input.value);
      } catch {
        input.select();
        document.execCommand("copy");
      }
      document.querySelector("#copyInviteUrlBtn").textContent = "복사됨";
    });
    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = document.querySelector("#inviteSubmitBtn");
      const error = document.querySelector("#inviteError");
      submit.disabled = true;
      error.hidden = true;
      try {
        const values = Object.fromEntries(new FormData(form).entries());
        const response = await apiFetch("/api/auth/users/invite", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(values),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "초대 링크를 만들지 못했습니다.");
        document.querySelector("#inviteSetupUrl").value = data.setupUrl || "";
        document.querySelector("#inviteResult").hidden = false;
        submit.textContent = "링크 다시 만들기";
      } catch (inviteError) {
        error.textContent = inviteError.message;
        error.hidden = false;
      } finally {
        submit.disabled = false;
      }
    });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function setupUserManagementDialog() {
    const modal = document.querySelector("#userManagementModal");
    const list = document.querySelector("#userManagementList");
    const error = document.querySelector("#userManagementError");
    const refreshButton = document.querySelector("#userManagementRefreshBtn");

    const close = () => {
      modal.hidden = true;
      document.body.classList.remove("modal-open");
      document.querySelector("#adminUsersBtn")?.focus();
    };

    const roleOptions = (currentRole) => [
      ["member", "팀원"],
      ["manager", "팀장"],
      ["admin", "관리자"],
    ].map(([value, label]) => (
      `<option value="${value}"${currentRole === value ? " selected" : ""}>${label}</option>`
    )).join("");

    const renderUsers = (users) => {
      if (!users.length) {
        list.innerHTML = '<p class="user-management-empty">등록된 사용자가 없습니다.</p>';
        return;
      }
      const currentUid = String(globalScope.AssistantCurrentUser?.uid || "");
      list.innerHTML = users.map((user) => {
        const isCurrentUser = String(user.uid || "") === currentUid;
        const protectedAttribute = isCurrentUser ? " disabled" : "";
        return `
          <form class="user-management-card" data-user-uid="${escapeHtml(user.uid)}">
            <div class="user-management-card-heading">
              <div>
                <strong>${escapeHtml(user.displayName || user.email)}</strong>
                <small>${escapeHtml(user.email)}</small>
              </div>
              ${isCurrentUser ? '<span class="user-self-badge">내 계정</span>' : ""}
            </div>
            <div class="user-management-fields">
              <label class="template-field"><span>이름</span><input name="displayName" type="text" maxlength="100" value="${escapeHtml(user.displayName)}" /></label>
              <label class="template-field"><span>권한</span><select name="role"${protectedAttribute}>${roleOptions(user.role)}</select></label>
              <label class="template-field"><span>Mantis 담당자명</span><input name="mantisUsername" type="text" maxlength="100" value="${escapeHtml(user.mantisUsername)}" placeholder="예: 석지원" /></label>
              <label class="template-field"><span>팀</span><input name="team" type="text" maxlength="100" value="${escapeHtml(user.team)}" /></label>
            </div>
            <div class="user-management-card-actions">
              <label class="user-approved-toggle">
                <input name="approved" type="checkbox"${user.approved ? " checked" : ""}${protectedAttribute} />
                <span>로그인 승인</span>
              </label>
              <span class="user-save-status" role="status"></span>
              <button class="run-evaluation-button" type="submit">저장</button>
            </div>
          </form>`;
      }).join("");
    };

    const loadUsers = async () => {
      error.hidden = true;
      error.textContent = "";
      refreshButton.disabled = true;
      list.innerHTML = '<p class="user-management-empty">사용자 목록을 불러오는 중…</p>';
      try {
        const response = await apiFetch("/api/auth/users");
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "사용자 목록을 불러오지 못했습니다.");
        renderUsers(Array.isArray(data.users) ? data.users : []);
      } catch (loadError) {
        list.innerHTML = "";
        error.textContent = loadError.message;
        error.hidden = false;
      } finally {
        refreshButton.disabled = false;
      }
    };

    document.querySelector("#adminUsersBtn")?.addEventListener("click", () => {
      modal.hidden = false;
      document.body.classList.add("modal-open");
      loadUsers();
    });
    document.querySelector("#userManagementModalClose")?.addEventListener("click", close);
    refreshButton?.addEventListener("click", loadUsers);
    modal?.addEventListener("click", (event) => { if (event.target === modal) close(); });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && modal && !modal.hidden) close();
    });

    list?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.target.closest(".user-management-card");
      if (!form) return;
      const submit = form.querySelector('button[type="submit"]');
      const status = form.querySelector(".user-save-status");
      const uid = form.dataset.userUid || "";
      const payload = {
        displayName: form.elements.displayName.value.trim(),
        role: form.elements.role.value,
        mantisUsername: form.elements.mantisUsername.value.trim(),
        team: form.elements.team.value.trim(),
        approved: form.elements.approved.checked,
      };
      submit.disabled = true;
      status.textContent = "저장 중…";
      status.className = "user-save-status";
      try {
        const response = await apiFetch(`/api/auth/users/${encodeURIComponent(uid)}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "사용자 정보를 저장하지 못했습니다.");
        if (uid === String(globalScope.AssistantCurrentUser?.uid || "")) {
          globalScope.AssistantCurrentUser = data.user;
          applySignedInUser(data.user);
        }
        status.textContent = "저장됨";
        status.classList.add("is-success");
      } catch (saveError) {
        status.textContent = saveError.message;
        status.classList.add("is-error");
      } finally {
        submit.disabled = false;
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
    setupInviteDialog();
    setupUserManagementDialog();
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
