// 본문의 조회 기준과 검증된 제거 가능한 필터를 한 줄로 모은다.
const LABEL_KEYS = {
  "담당자/인물": "people", "담당자": "people", "인물": "people", "요청자/본문": "people",
  "제외 상태": "상태 제외",
};
const keyFor = (label) => LABEL_KEYS[label] || label;

function parseCriteria(text) {
  // 값 안의 쉼표(여러 담당자·상태)는 유지하고 다음 '라벨=값' 앞에서만 나눈다.
  return text.split(/,\s*(?=[^,=:]{1,30}\s*[=:]|(?:title|keyword)\s+(?:포함|미포함|일치|제외)|Mantis\s+(?:전체 저장본|인덱스 전체|실시간|스냅샷)|최신\s*\d+건\s*기준)/).map((part) => {
    // 집계한 데이터 범위는 등록일·검색어 값에 붙이지 않는다.
    if (/^(?:Mantis\s+(?:전체 저장본|인덱스 전체|실시간|스냅샷)|최신\s*\d+건\s*기준)/.test(part.trim())) {
      return { label: "조회 범위", value: part.trim() };
    }
    const pair = part.trim().match(/^([^=:]+?)\s*[=:]\s*(.+)$/);
    if (pair) return { label: pair[1].trim(), value: pair[2].trim() };
    const clause = part.trim().match(/^(title|keyword)\s+(.+)$/);
    return clause ? { label: clause[1] === "title" ? "제목" : "키워드", value: clause[2] }
      : { label: "조회 대상", value: part.trim() };
  }).filter((condition) => condition.value && condition.value !== "요청 조건");
}

export function answerSearchPresentation(message) {
  const verified = message.verification?.constraints;
  // 이전 집계 답변에 저장된 원시 조건 객체는 제거 버튼용 목록이 아니다.
  const conditions = (Array.isArray(verified) ? verified : []).map((condition) => ({
    ...condition, removeCondition: condition,
  }));
  const content = String(message.content || "").replace(/^검색 기준:\s*(.+)$/gm, (line, criteria) => {
    parseCriteria(criteria).forEach((condition) => {
      const existing = conditions.find((item) => keyFor(item.label) === keyFor(condition.label)
        && (item.removeCondition || item.value === condition.value));
      if (existing) {
        // 본문은 실제 조회에 쓰인 고객사 이름·담당자 역할을 표시한다.
        existing.label = condition.label;
        existing.value = condition.value;
      } else conditions.push(condition);
    });
    return "";
  });
  return { content, conditions };
}
