// 사용자 화면의 시각은 로컬 시간대, 한국어, 분 단위로 표시한다.
export function formatUiDateTime(value, { compact = false, now = new Date() } = {}) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const hour = date.getHours();
  const time = `${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}:${String(date.getMinutes()).padStart(2, "0")}`;
  const sameYear = date.getFullYear() === now.getFullYear();
  if (compact && date.toDateString() === now.toDateString()) return time;
  const day = compact ? `${date.getMonth() + 1}/${date.getDate()}` : `${date.getMonth() + 1}월 ${date.getDate()}일`;
  return `${sameYear ? "" : `${date.getFullYear()}년 `}${day} ${time}`;
}

// 서버의 customer_directory.json에서 받은 표시 전용 이름표다. 조회 키는 바꾸지 않는다.
let customerExact = new Map();
let customerProjects = [];
let customerSearchPattern = null;

export function setCustomerNames(directory = {}) {
  const entries = (value) => Object.entries(value || {}).filter(([key, name]) => key && typeof name === "string" && name.trim());
  customerExact = new Map(entries(directory.exact).map(([key, name]) => [key.toLocaleLowerCase(), name]));
  customerProjects = entries(directory.projects).sort(([left], [right]) => right.length - left.length);
  const aliases = [...customerExact.keys()].sort((left, right) => right.length - left.length)
    .map((alias) => alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  customerSearchPattern = aliases.length ? new RegExp(`(^|[^\\p{L}\\p{N}_])(${aliases.join("|")})(?=$|[^A-Za-z0-9_])`, "giu") : null;
}

export function customerSearchNames() {
  return [...new Set([...customerExact.values(), ...customerProjects.map(([, name]) => name)])]
    .sort((left, right) => left.localeCompare(right, "ko"));
}

// 즐겨찾기 검색용 비교 문자열만 정규화하고 저장된 대화와 인용은 그대로 둔다.
export function customerSearchText(value) {
  const text = customerLabelText(value);
  return (customerSearchPattern ? text.replace(customerSearchPattern, (_, before, alias) => before + customerExact.get(alias.toLocaleLowerCase())) : text)
    .toLocaleLowerCase();
}

export function customerName(value, fallback = "고객사 미상") {
  const raw = String(value?.project ?? value ?? "").trim();
  if (!raw) return fallback;
  const exact = customerExact.get(raw.toLocaleLowerCase());
  if (exact) return exact;
  const entry = customerProjects.find(([project]) => raw.toLocaleLowerCase().startsWith(project.toLocaleLowerCase())
    && (!raw.slice(project.length) || /^[_(\s]/.test(raw.slice(project.length))));
  return entry?.[1] || value?.customer_name || raw;
}

// 권장 질문·요약의 표시만 변환한다. data-*의 원본 질문과 원문 인용에는 적용하지 않는다.
export function customerLabelText(value) {
  let text = String(value || "");
  for (const [project, name] of customerProjects) {
    const escaped = project.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`(^|[^\\p{L}\\p{N}_])${escaped}(?:_Project)?(?:\\([^\\n)]*\\))?(?:_Project)?(?=$|[^A-Za-z0-9_])`, "giu");
    text = text.replace(pattern, (_, before) => before + name);
  }
  return text;
}

export function customerAnswerText(value) {
  let fence = "";
  return String(value || "").split("\n").map((line) => {
    // 근거 인용과 코드·URL은 원문을 그대로 보존한다.
    if (/^\s*>/.test(line)) return line;
    const marker = line.match(/^\s*(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = "";
      return line;
    }
    if (fence) return line;
    return line.split(/(`+[^`]*`+|https?:\/\/\S+)/g).map((part, index) => index % 2 ? part : customerLabelText(part)).join("");
  }).join("\n");
}
