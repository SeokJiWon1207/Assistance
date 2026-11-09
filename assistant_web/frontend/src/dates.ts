// 차트 안의 날짜 표기. 정적 화면의 ui_format.js(formatPeriodRange·formatAxisDate)와 같은 규칙이다.
// 기간·문장은 "11월 3일–9일"(달이 바뀌면 "10월 27일–11월 2일"), 축·표처럼 좁은 칸은 "11/3".
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})/;

const parts = (value: string) => {
  const match = String(value || '').match(ISO_DAY);
  return match ? match.slice(1).map(Number) : null;
};

export const axisDate = (value: string) => {
  const day = parts(value);
  return day ? `${day[1]}/${day[2]}` : String(value || '');
};

export const dayLabel = (value: string) => {
  const day = parts(value);
  return day ? `${day[1]}월 ${day[2]}일` : String(value || '');
};

export const periodRange = (from: string, to: string) => {
  const start = parts(from);
  const end = parts(to);
  if (!start || !end) return [from, to].filter(Boolean).join('–');
  const startText = `${start[0] !== end[0] ? `${start[0]}년 ` : ''}${start[1]}월 ${start[2]}일`;
  if (start[0] === end[0] && start[1] === end[1]) return `${startText}–${end[2]}일`;
  return `${startText}–${start[0] !== end[0] ? `${end[0]}년 ` : ''}${end[1]}월 ${end[2]}일`;
};
