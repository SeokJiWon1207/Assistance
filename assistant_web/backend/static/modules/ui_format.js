// innerHTML 텍스트와 속성값 양쪽에 쓰도록 따옴표까지 이스케이프한다.
export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

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

// 화면의 날짜 표기는 두 가지다. 기간·문장은 "9월 1일–30일"(달이 바뀌면 "7월 3일–9월 30일"),
// 차트 축·표처럼 좁은 칸은 "9/1". 연도가 다르면 기간 쪽에만 연도를 붙인다.
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})/;

export function formatPeriodRange(from, to) {
  const start = String(from || "").match(ISO_DAY);
  const end = String(to || "").match(ISO_DAY);
  if (!start || !end) return [from, to].filter(Boolean).join("–");
  const [, startYear, startMonth, startDay] = start.map(Number);
  const [, endYear, endMonth, endDay] = end.map(Number);
  const startText = `${startYear !== endYear ? `${startYear}년 ` : ""}${startMonth}월 ${startDay}일`;
  if (startYear === endYear && startMonth === endMonth) return `${startText}–${endDay}일`;
  return `${startText}–${startYear !== endYear ? `${endYear}년 ` : ""}${endMonth}월 ${endDay}일`;
}

// 서버가 "2026-11-03 ~ 2026-11-09"처럼 보낸 기간 글자도 같은 표기로 바꾼다. 형식이 다르면 그대로 둔다.
export function formatPeriodText(text) {
  const match = String(text || "").match(/^(\d{4}-\d{2}-\d{2})\s*[~–-]\s*(\d{4}-\d{2}-\d{2})$/);
  return match ? formatPeriodRange(match[1], match[2]) : String(text || "");
}

export function formatAxisDate(value) {
  const match = String(value || "").match(ISO_DAY);
  return match ? `${Number(match[2])}/${Number(match[3])}` : String(value || "");
}

// 서버의 customer_directory.json에서 받은 표시 전용 이름표다. 조회 키는 바꾸지 않는다.
let customerExact = new Map();
let customerProjects = [];
let customerSearchPattern = null;
let customerPicker = [];
let customerLogos = new Map();

export function setCustomerNames(directory = {}) {
  const entries = (value) => Object.entries(value || {}).filter(([key, name]) => key && typeof name === "string" && name.trim());
  customerExact = new Map(entries(directory.exact).map(([key, name]) => [key.toLocaleLowerCase(), name]));
  customerProjects = entries(directory.projects).sort(([left], [right]) => right.length - left.length);
  customerPicker = (Array.isArray(directory.picker) ? directory.picker : []).filter((name) => typeof name === "string" && name.trim());
  // 로고는 같은 출처의 고객사 이미지 폴더만 쓴다. 다른 주소는 무시하고 글자 표시로 대신한다.
  customerLogos = new Map(entries(directory.logos).filter(([, url]) => /^\/static\/assets\/customers\/[a-z0-9-]+\.(?:png|webp)$/.test(url)));
  const aliases = [...customerExact.keys()].sort((left, right) => right.length - left.length)
    .map((alias) => alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  customerSearchPattern = aliases.length ? new RegExp(`(^|[^\\p{L}\\p{N}_])(${aliases.join("|")})(?=$|[^A-Za-z0-9_])`, "giu") : null;
}

export function customerLogoUrl(name) {
  return customerLogos.get(String(name || "").trim()) || "";
}

// 고객사 이름 앞에 두는 로고 표시. 로고가 없으면 이름 앞 두 글자를 같은 크기 칸에 보여 준다.
// 이름은 옆에 글자로 따로 보이므로 로고는 꾸밈 이미지(alt="")다.
export function customerMark(name, size = "sm") {
  const label = String(name || "").trim();
  const url = customerLogoUrl(label);
  const sizeClass = size === "md" ? " is-md" : size === "lg" ? " is-lg" : "";
  if (url) return `<span class="customer-mark${sizeClass}" aria-hidden="true"><img src="${url}" alt="" loading="lazy" decoding="async"></span>`;
  const short = (label.replace(/\(.*\)$/, "").replace(/(투자증권|금융투자|증권|선물)$/, "") || label).slice(0, 2);
  const safe = short.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  return `<span class="customer-mark is-text${sizeClass}" aria-hidden="true">${safe || "?"}</span>`;
}

// 고객사 자동완성 목록. 서버 설정(picker_names)이 있으면 그 이름만 설정 순서대로 쓴다.
export function customerSearchNames() {
  if (customerPicker.length) return [...customerPicker];
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
