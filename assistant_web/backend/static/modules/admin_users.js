// 관리자 센터 › 사용자: 목록·검색·필터, 오른쪽 패널에서 편집과 초대.
import { openDialogFocus, closeDialogFocus } from "./dialog_focus.js?v=20260911-6";
import { state, els, apiFetch } from "./runtime.js?v=20260911-6";
import { escapeHtml } from "./chat.js?v=20260911-6";
import { showToast } from "./workspace.js?v=20260911-6";

const ROLE_LABELS = { member: "팀원", manager: "팀장", admin: "관리자" };
const ROLE_HELP = {
  member: "대화와 내 업무를 씁니다.",
  manager: "Notion 수정, 플레이북 편집, 팀 브리핑 다시 만들기를 할 수 있어요.",
  admin: "팀장 권한에 더해 관리자 센터와 모델 설정을 씁니다.",
};
const USER_FILTERS = [
  ["all", "전체"],
  ["pending", "승인 대기"],
  ["staff", "관리자·팀장"],
  ["nomantis", "담당자명 없음"],
];

let notifyChange = () => {};
let drawerTrigger = null;
let drawerOriginal = null;
let drawerVersion = 0;

function usersSectionAvailable() {
  return Boolean(state.authEnabled && state.currentUser?.role === "admin");
}

function pendingAdminUsers() {
  return state.adminUsers.filter((user) => !user.approved);
}

function isCurrentUser(user) {
  return String(user?.uid || "") === String(state.currentUser?.uid || "");
}

async function loadAdminUsers() {
  if (!usersSectionAvailable()) return;
  state.adminUsersError = "";
  try {
    const response = await apiFetch("/api/auth/users");
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "사용자 목록을 불러오지 못했습니다.");
    state.adminUsers = Array.isArray(data.users) ? data.users : [];
  } catch (error) {
    // 사용자 목록이 실패해도 품질 화면은 열려야 하므로 오류는 이 섹션에만 보여 준다.
    state.adminUsersError = error.message;
  }
}

function matchesFilter(user) {
  const filter = state.adminUserFilter;
  if (filter === "pending" && user.approved) return false;
  if (filter === "staff" && user.role === "member") return false;
  if (filter === "nomantis" && user.mantisUsername) return false;
  const query = state.adminUserQuery.trim().toLowerCase();
  if (!query) return true;
  return [user.displayName, user.email, user.mantisUsername, user.team]
    .some((value) => String(value || "").toLowerCase().includes(query));
}

function filterCount(filter) {
  if (filter === "pending") return pendingAdminUsers().length;
  if (filter === "staff") return state.adminUsers.filter((user) => user.role !== "member").length;
  if (filter === "nomantis") return state.adminUsers.filter((user) => !user.mantisUsername).length;
  return state.adminUsers.length;
}

function renderUserRows() {
  const users = state.adminUsers.filter(matchesFilter)
    // 승인 대기인 사람을 맨 위로 모은다.
    .sort((a, b) => Number(Boolean(a.approved)) - Number(Boolean(b.approved)));
  if (!users.length) {
    const query = state.adminUserQuery.trim();
    return `<p class="admin-empty">${query ? `‘${escapeHtml(query)}’에 맞는 사용자가 없어요.` : "이 조건에 맞는 사용자가 없어요."}</p>`;
  }
  return users.map((user) => {
    const name = user.displayName || user.email;
    const uid = escapeHtml(user.uid);
    return `
      <div class="admin-row admin-user-row${user.approved ? "" : " is-pending"}">
        <button type="button" class="admin-user-name" data-admin-user-edit="${uid}" aria-label="${escapeHtml(name)} 편집">
          <span class="admin-avatar" aria-hidden="true">${escapeHtml(name.trim().slice(0, 1).toUpperCase())}</span>
          <span><strong>${escapeHtml(name)}${isCurrentUser(user) ? ' <em class="admin-pill is-grey">내 계정</em>' : ""}</strong><small>${escapeHtml(user.email)}</small></span>
        </button>
        <span class="admin-pill ${user.role === "admin" ? "is-run" : "is-grey"}">${escapeHtml(ROLE_LABELS[user.role] || user.role)}</span>
        <span class="${user.mantisUsername ? "" : "is-warning"}">${escapeHtml(user.mantisUsername || "입력 필요")}</span>
        <span class="admin-muted">${escapeHtml(user.team || "—")}</span>
        <span class="admin-user-login">${user.approved
          ? '<span class="admin-pill is-pass">승인됨</span>'
          : `<button type="button" class="admin-secondary is-accent" data-admin-user-approve="${uid}">승인</button><button type="button" class="admin-secondary" data-admin-user-edit="${uid}">확인</button>`}</span>
      </div>`;
  }).join("");
}

function renderUsersSection(container) {
  const header = `
    <header class="admin-section-header">
      <div><h2>사용자</h2><p>권한과 Mantis 담당자명을 맞춰 두면 ‘내 업무’가 정확해집니다.</p></div>
      <button type="button" class="admin-primary" data-admin-invite><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>사용자 초대</button>
    </header>`;
  if (state.adminUsersError) {
    container.innerHTML = `${header}<div class="admin-card admin-empty"><strong>사용자 목록을 불러오지 못했어요.</strong><p>${escapeHtml(state.adminUsersError)}</p><button type="button" class="admin-secondary" data-admin-users-reload>다시 시도</button></div>`;
    return;
  }
  container.innerHTML = `
    ${header}
    <div class="admin-table-tools is-bare">
      <div class="admin-chips" role="group" aria-label="사용자 필터">${USER_FILTERS.map(([value, label]) => `
        <button type="button" class="admin-chip" data-admin-user-filter="${value}" aria-pressed="${state.adminUserFilter === value}">${label} <b>${filterCount(value)}</b></button>`).join("")}</div>
      <label class="admin-search"><span class="sr-only">사용자 검색</span>
        <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></svg>
        <input id="adminUserSearch" type="search" value="${escapeHtml(state.adminUserQuery)}" placeholder="이름·이메일·담당자명" autocomplete="off" />
      </label>
    </div>
    <section class="admin-card admin-table" aria-label="사용자 목록">
      <div class="admin-row admin-user-row admin-th" aria-hidden="true"><span>이름</span><span>권한</span><span>Mantis 담당자명</span><span>팀</span><span>로그인</span></div>
      <div id="adminUsersRows">${renderUserRows()}</div>
    </section>`;
}

// ---- 오른쪽 패널 ----------------------------------------------------------

function roleRadios(current, disabled) {
  return `<div class="admin-seg" role="radiogroup" aria-label="권한">${Object.entries(ROLE_LABELS).map(([value, label]) => `
    <label><input type="radio" name="role" value="${value}"${value === current ? " checked" : ""}${disabled ? " disabled" : ""} /><span>${label}</span></label>`).join("")}</div>`;
}

function openDrawer(title, subtitle, body, trigger) {
  drawerVersion += 1;
  drawerTrigger = trigger || document.activeElement;
  els.adminDrawerTitle.textContent = title;
  els.adminDrawerSub.textContent = subtitle;
  els.adminDrawerForm.innerHTML = body;
  els.adminDrawer.hidden = false;
  els.adminDrawerBackdrop.hidden = false;
  openDialogFocus(els.adminDrawer, {
    opener: drawerTrigger, onClose: closeDrawer,
    initialFocus: els.adminDrawerForm.querySelector("input:not([disabled]):not([type=radio]):not([type=hidden])"),
    fallback: () => els.adminContent.querySelector("[data-admin-invite]") || els.viewAdminBtn,
  });
}

function closeDrawer() {
  if (els.adminDrawer.hidden) return;
  drawerVersion += 1;
  els.adminDrawer.hidden = true;
  els.adminDrawerBackdrop.hidden = true;
  els.adminDrawerForm.innerHTML = "";
  drawerOriginal = null;
  closeDialogFocus(els.adminDrawer);
  drawerTrigger = null;
}

function drawerValues() {
  const form = els.adminDrawerForm;
  return {
    displayName: form.elements.displayName.value.trim(),
    role: form.querySelector('input[name="role"]:checked')?.value || "member",
    mantisUsername: form.elements.mantisUsername.value.trim(),
    team: form.elements.team.value.trim(),
    approved: form.elements.approved.checked,
  };
}

function syncEditDrawer() {
  if (!drawerOriginal) return;
  const values = drawerValues();
  const dirty = Object.keys(values).some((key) => values[key] !== drawerOriginal[key]);
  const status = els.adminDrawerForm.querySelector("[data-admin-dirty]");
  status.textContent = dirty ? "저장하지 않은 변경이 있어요" : "변경 사항 없음";
  status.classList.toggle("is-warning", dirty);
  els.adminDrawerForm.querySelector('[type="submit"]').disabled = !dirty;
  els.adminDrawerForm.querySelector("[data-admin-role-help]").textContent = isCurrentUser(drawerOriginal)
    ? "내 계정의 권한과 로그인 허용은 바꿀 수 없어요."
    : ROLE_HELP[values.role];
  els.adminDrawerForm.querySelector("[data-admin-approved-help]").textContent = values.approved ? "로그인할 수 있어요" : "지금은 로그인할 수 없어요";
}

function openEditDrawer(uid, trigger) {
  const user = state.adminUsers.find((item) => String(item.uid) === String(uid));
  if (!user) return;
  const self = isCurrentUser(user);
  drawerOriginal = {
    uid: user.uid,
    displayName: user.displayName || "",
    role: user.role || "member",
    mantisUsername: user.mantisUsername || "",
    team: user.team || "",
    approved: Boolean(user.approved),
  };
  openDrawer(user.displayName || user.email, user.email, `
    <input type="hidden" name="mode" value="edit" />
    <div class="admin-drawer-body">
      ${user.approved ? "" : '<p class="admin-callout">아직 로그인할 수 없는 사용자입니다. 정보를 확인하고 아래 ‘로그인 허용’을 켜 주세요.</p>'}
      <label class="admin-field">이름<input name="displayName" type="text" maxlength="100" value="${escapeHtml(drawerOriginal.displayName)}" /></label>
      <fieldset class="admin-field"><legend>권한</legend>${roleRadios(drawerOriginal.role, self)}<small data-admin-role-help></small></fieldset>
      <label class="admin-field">Mantis 담당자명<input name="mantisUsername" type="text" maxlength="100" value="${escapeHtml(drawerOriginal.mantisUsername)}" placeholder="Mantis에 표시되는 이름" /></label>
      <label class="admin-field">팀<input name="team" type="text" maxlength="100" value="${escapeHtml(drawerOriginal.team)}" /></label>
      <label class="admin-switch-row"><span><strong>로그인 허용</strong><small data-admin-approved-help></small></span>
        <input name="approved" type="checkbox" role="switch" class="admin-switch"${drawerOriginal.approved ? " checked" : ""}${self ? " disabled" : ""} /></label>
      ${!self && user.approved ? `<div class="admin-field"><strong>비밀번호 초기화</strong>
        <p class="admin-muted">${user.passwordResetPending ? "초기화가 완료되지 않아 로그인이 차단되어 있습니다. 다시 초기화해 주세요." : user.passwordChangeRequired ? "다음 로그인 시 비밀번호를 변경해야 합니다." : "비밀번호를 잊은 사용자에게 새 임시 비밀번호를 지정할 수 있습니다."}</p>
        <button type="button" class="admin-secondary" data-admin-password-reset="${escapeHtml(user.uid)}">임시 비밀번호로 초기화</button></div>` : ""}
      <p class="admin-form-error" role="alert" hidden></p>
    </div>
    <footer class="admin-drawer-foot"><span class="admin-muted" data-admin-dirty role="status"></span>
      <span><button type="button" class="admin-secondary" data-admin-drawer-close>취소</button><button type="submit" class="admin-primary" disabled>저장</button></span></footer>`, trigger);
  syncEditDrawer();
}

function openPasswordResetDrawer(uid, trigger) {
  const user = state.adminUsers.find((item) => String(item.uid) === String(uid));
  if (!user || !user.approved || isCurrentUser(user)) return;
  drawerOriginal = null;
  openDrawer("임시 비밀번호로 초기화", user.email, `
    <input type="hidden" name="mode" value="reset" />
    <input type="hidden" name="uid" value="${escapeHtml(user.uid)}" />
    <div class="admin-drawer-body">
      <p class="admin-callout">초기화하면 기존 로그인 세션이 차단됩니다. 사용자에게 새 임시 비밀번호를 전달해 주세요. 사용자는 다시 로그인한 뒤 비밀번호를 변경해야 합니다.</p>
      <label class="admin-field"><span>임시 비밀번호 <small>필수 · 8~128자</small></span><input name="temporaryPassword" type="password" autocomplete="new-password" required minlength="8" maxlength="128" /></label>
      <p class="admin-form-error" role="alert" hidden></p>
    </div>
    <footer class="admin-drawer-foot is-end"><button type="button" class="admin-secondary" data-admin-drawer-close>취소</button>
      <button type="submit" class="admin-primary">비밀번호 초기화</button></footer>`, trigger);
}

function showResetResult(email) {
  els.adminDrawerForm.innerHTML = `
    <div class="admin-drawer-body"><div class="admin-invite-done" role="status">
      <strong>${escapeHtml(email)} 비밀번호 초기화 완료</strong>
      <p>지정한 임시 비밀번호를 사용자에게 전달해 주세요. 다시 로그인한 뒤 새 비밀번호로 변경해야 합니다.</p>
    </div></div>
    <footer class="admin-drawer-foot is-end"><button type="button" class="admin-primary" data-admin-drawer-close>완료</button></footer>`;
  els.adminDrawerForm.querySelector("[data-admin-drawer-close]").focus();
}

function openInviteDrawer(trigger) {
  drawerOriginal = null;
  openDrawer("사용자 계정 만들기", "이메일과 임시 비밀번호를 전달하면 로그인할 수 있습니다. 첫 로그인 시 비밀번호를 변경해야 합니다.", `
    <input type="hidden" name="mode" value="invite" />
    <div class="admin-drawer-body" data-admin-invite-fields>
      <label class="admin-field"><span>이메일 <small>필수</small></span><input name="email" type="email" required maxlength="320" placeholder="name@회사도메인" /></label>
      <label class="admin-field"><span>임시 비밀번호 <small>필수 · 8~128자</small></span><input name="temporaryPassword" type="password" autocomplete="new-password" required minlength="8" maxlength="128" /></label>
      <label class="admin-field"><span>이름 <small>선택</small></span><input name="displayName" type="text" maxlength="100" /></label>
      <fieldset class="admin-field"><legend>권한</legend>${roleRadios("member", false)}</fieldset>
      <label class="admin-field"><span>Mantis 담당자명 <small>선택 · 나중에 넣어도 돼요</small></span><input name="mantisUsername" type="text" maxlength="100" /></label>
      <label class="admin-field"><span>팀 <small>선택</small></span><input name="team" type="text" maxlength="100" /></label>
      <p class="admin-form-error" role="alert" hidden></p>
    </div>
    <footer class="admin-drawer-foot is-end">
      <button type="button" class="admin-secondary" data-admin-drawer-close>취소</button><button type="submit" class="admin-primary">계정 만들기</button></footer>`, trigger);
}

function showInviteResult(email) {
  els.adminDrawerForm.innerHTML = `
    <input type="hidden" name="mode" value="invited" />
    <div class="admin-drawer-body">
      <div class="admin-invite-done" role="status">
        <strong><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/></svg>${escapeHtml(email)} 계정 생성 완료</strong>
        <p>로그인 주소, 위 이메일과 직접 지정한 임시 비밀번호를 사용자에게 전달해 주세요. 첫 로그인 시 새 비밀번호로 변경해야 서비스를 이용할 수 있습니다.</p>
      </div>
      <p class="admin-muted">초대한 사람은 목록에 ‘승인됨’으로 추가됩니다.</p>
    </div>
    <footer class="admin-drawer-foot is-end">
      <button type="button" class="admin-secondary" data-admin-invite-again>한 명 더 초대</button><button type="button" class="admin-primary" data-admin-drawer-close>완료</button></footer>`;
  els.adminDrawerForm.querySelector("[data-admin-drawer-close]").focus();
}

function replaceUser(user) {
  const index = state.adminUsers.findIndex((item) => String(item.uid) === String(user.uid));
  if (index >= 0) state.adminUsers[index] = user;
  else state.adminUsers.unshift(user);
  if (isCurrentUser(user)) {
    state.currentUser = { ...state.currentUser, ...user };
    globalThis.AssistantCurrentUser = state.currentUser;
    globalThis.AssistantApplySignedInUser?.(state.currentUser);
  }
}

async function patchUser(uid, payload) {
  const response = await apiFetch(`/api/auth/users/${encodeURIComponent(uid)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "사용자 정보를 저장하지 못했습니다.");
  replaceUser(data.user);
  return data.user;
}

function showFormError(message) {
  const error = els.adminDrawerForm.querySelector(".admin-form-error");
  if (!error) return;
  error.textContent = message;
  error.hidden = !message;
}

async function submitDrawer(event) {
  event.preventDefault();
  const form = els.adminDrawerForm;
  const mode = form.elements.mode?.value;
  const requestVersion = drawerVersion;
  const submit = form.querySelector('[type="submit"]');
  if (!submit || submit.disabled) return;
  showFormError("");
  submit.disabled = true;
  try {
    if (mode === "reset") {
      if (!form.elements.temporaryPassword.checkValidity()) throw new Error("임시 비밀번호는 8~128자로 입력해 주세요.");
      const uid = form.elements.uid.value;
      const passwordInput = form.elements.temporaryPassword;
      passwordInput.disabled = true;
      let data;
      try {
        const response = await apiFetch(`/api/auth/users/${encodeURIComponent(uid)}/reset-password`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ temporaryPassword: passwordInput.value }),
        });
        data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || "초기화하지 못했습니다. 다시 시도해 주세요.");
        passwordInput.value = "";
      } finally {
        passwordInput.disabled = false;
      }
      replaceUser(data.user);
      notifyChange();
      if (drawerVersion === requestVersion && !els.adminDrawer.hidden) showResetResult(data.user.email);
      else showToast(`${data.user.email} 비밀번호 초기화 완료. 지정한 임시 비밀번호를 전달해 주세요.`, "success");
      return;
    }
    if (mode === "edit") {
      const values = drawerValues();
      const user = await patchUser(drawerOriginal.uid, values);
      closeDrawer();
      notifyChange();
      showToast(`${user.displayName || user.email} 님 정보를 저장했어요.`, "success");
      return;
    }
    const email = form.elements.email.value.trim();
    if (!form.elements.email.checkValidity() || !email) throw new Error("이메일 주소를 확인해 주세요.");
    if (!form.elements.temporaryPassword.checkValidity()) throw new Error("임시 비밀번호는 8~128자로 입력해 주세요.");
    const response = await apiFetch("/api/auth/users/invite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        temporaryPassword: form.elements.temporaryPassword.value,
        displayName: form.elements.displayName.value.trim(),
        role: form.querySelector('input[name="role"]:checked')?.value || "member",
        mantisUsername: form.elements.mantisUsername.value.trim(),
        team: form.elements.team.value.trim(),
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || "계정을 만들지 못했습니다.");
    form.elements.temporaryPassword.value = "";
    if (data.user) replaceUser(data.user);
    notifyChange();
    showInviteResult(email);
  } catch (error) {
    if (mode === "reset" && (drawerVersion !== requestVersion || els.adminDrawer.hidden)) {
      showToast(error.message, "warning");
    } else showFormError(error.message);
    submit.disabled = false;
    if (mode === "edit") syncEditDrawer();
  }
}

async function approveUser(uid, button) {
  button.disabled = true;
  try {
    const user = await patchUser(uid, { approved: true });
    notifyChange();
    showToast(`${user.displayName || user.email} 님이 이제 로그인할 수 있어요.`, "success");
  } catch (error) {
    button.disabled = false;
    showToast(error.message, "warning");
  }
}

function handleUsersClick(event) {
  const filter = event.target.closest("[data-admin-user-filter]");
  if (filter) {
    state.adminUserFilter = filter.dataset.adminUserFilter;
    notifyChange();
    els.adminContent.querySelector(`[data-admin-user-filter="${filter.dataset.adminUserFilter}"]`)?.focus();
    return;
  }
  const approve = event.target.closest("[data-admin-user-approve]");
  if (approve && !approve.disabled) return approveUser(approve.dataset.adminUserApprove, approve);
  const edit = event.target.closest("[data-admin-user-edit]");
  if (edit) return openEditDrawer(edit.dataset.adminUserEdit, edit);
  const invite = event.target.closest("[data-admin-invite]");
  if (invite) return openInviteDrawer(invite);
  if (event.target.closest("[data-admin-users-reload]")) {
    loadAdminUsers().then(notifyChange);
  }
}

function initAdminUsers({ onChange } = {}) {
  if (!els.adminDrawer) return;
  notifyChange = onChange || notifyChange;
  els.adminContent.addEventListener("click", handleUsersClick);
  els.adminContent.addEventListener("input", (event) => {
    if (event.target.id !== "adminUserSearch") return;
    state.adminUserQuery = event.target.value;
    const rows = els.adminContent.querySelector("#adminUsersRows");
    if (rows) rows.innerHTML = renderUserRows();
  });
  els.adminContent.addEventListener("keydown", (event) => {
    if (event.target.id === "adminUserSearch" && event.key === "Escape" && event.target.value) {
      event.stopPropagation();
      event.target.value = "";
      event.target.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });
  els.adminDrawerForm.addEventListener("submit", submitDrawer);
  els.adminDrawerForm.addEventListener("input", syncEditDrawer);
  els.adminDrawerForm.addEventListener("change", syncEditDrawer);
  els.adminDrawerForm.addEventListener("click", (event) => {
    const reset = event.target.closest("[data-admin-password-reset]");
    if (reset) return openPasswordResetDrawer(reset.dataset.adminPasswordReset, reset);
    if (event.target.closest("[data-admin-drawer-close]")) return closeDrawer();
    if (event.target.closest("[data-admin-invite-again]")) openInviteDrawer(drawerTrigger);
  });
  els.adminDrawerClose.addEventListener("click", closeDrawer);
  els.adminDrawerBackdrop.addEventListener("click", closeDrawer);
}

export {
  ROLE_LABELS,
  closeDrawer as closeAdminDrawer,
  initAdminUsers,
  loadAdminUsers,
  pendingAdminUsers,
  renderUsersSection,
  usersSectionAvailable,
};
