// 질문이 없는 대화의 첫 화면: 인사 → 팀 템플릿 → 최근 본 이슈·이어서 볼 대화.
import { state } from "./runtime.js?v=20260911-6";
import { escapeHtml } from "./chat_rendering.js?v=20260911-6";
import { TEAM_TEMPLATES, renderTeamTemplates } from "./template_picker.js?v=20260911-6";
import { homeRecentsHtml, readRecentIssues } from "./recent_items.js?v=20261105-1";

export function emptyStateElement() {
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.innerHTML = `
    <div>
      <p class="empty-state-eyebrow">${escapeHtml(state.currentUser?.displayName || state.currentUser?.name || "사용자")}님, ${new Date().getHours() < 12 ? "좋은 아침이에요" : new Date().getHours() < 18 ? "좋은 오후예요" : "좋은 저녁이에요"}</p>
      <h3>어떤 업무를 도와드릴까요?</h3>
      <p class="empty-state-source-intro">Mantis 이슈와 Notion 문서를 바탕으로 답변해요.</p>
      <section class="home-suggestions" data-home-suggestions aria-label="내 업무 추천" hidden></section>
      <div class="starter-heading"><strong>팀 템플릿</strong><button type="button" data-open-all-templates>전체 ${TEAM_TEMPLATES.length}개 보기</button></div>
      <div class="starter-grid" aria-label="팀 공용 템플릿">
        ${state.teamTemplatesError
          ? `<p class="status-warning">${escapeHtml(state.teamTemplatesError)} 직접 질문은 계속 사용할 수 있습니다.</p>`
          : renderTeamTemplates()}
      </div>
      ${homeRecentsHtml({ issues: readRecentIssues(state.currentUser?.uid), conversations: state.conversations, activeId: state.activeConversationId })}
    </div>
  `;
  return empty;
}
