// 첫 화면의 최근 본 이슈·이어서 볼 대화 버튼을 처리한다. 그리는 일은 recent_items.js가 맡는다.
import { state, els } from "./runtime.js?v=20260911-6";
import { openConversation } from "./workspace.js?v=20260911-6";
import { openSourceDetail } from "./chat_source_detail.js?v=20260911-6";

function initHomeRecents() {
  els.messages?.addEventListener("click", (event) => {
    const issue = event.target.closest("[data-home-issue]");
    if (issue) {
      openSourceDetail({ type: "chroma", issue_id: issue.dataset.homeIssue });
      return;
    }
    const conversation = event.target.closest("[data-home-conversation]");
    if (conversation && !state.busy) {
      openConversation(conversation.dataset.homeConversation);
      els.input.focus();
    }
  });
}

export { initHomeRecents };
