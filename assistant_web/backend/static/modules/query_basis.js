// 결정형 업무 답변 끝의 '조회 기준: 범위' + '- 안내' 묶음(backend/reports/work_queries.py).
// 답(목록)보다 눈에 덜 띄게 작은 회색 글씨로 묶어, 본문과 같은 무게로 길게 이어지지 않게 한다.
function plainText(value) {
  return String(value || "").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1").trim();
}

export function renderQueryBasis(rawBlock) {
  const lines = String(rawBlock || "").split("\n").map((line) => line.trim()).filter(Boolean);
  const box = document.createElement("div");
  box.className = "answer-basis";
  const title = document.createElement("p");
  title.className = "answer-basis-title";
  const label = document.createElement("strong");
  label.textContent = "조회 기준";
  title.append(label, document.createTextNode(` ${plainText(lines[0]).replace(/^조회 기준:\s*/, "")}`));
  box.appendChild(title);
  const notes = lines.slice(1).map((line) => plainText(line.replace(/^[-*•]\s+/, "")));
  if (notes.length) {
    const list = document.createElement("ul");
    list.className = "answer-basis-notes";
    notes.forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      list.appendChild(item);
    });
    box.appendChild(list);
  }
  return box;
}
