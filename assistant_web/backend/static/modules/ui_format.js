// 사용자 화면의 시각은 로컬 시간대, 한국어, 분 단위로 표시한다.
export function formatUiDateTime(value, { compact = false, now = new Date() } = {}) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const time = new Intl.DateTimeFormat("ko-KR", { hour: "numeric", minute: "2-digit" }).format(date);
  const sameYear = date.getFullYear() === now.getFullYear();
  if (compact && date.toDateString() === now.toDateString()) return time;
  const day = compact ? `${date.getMonth() + 1}/${date.getDate()}` : `${date.getMonth() + 1}월 ${date.getDate()}일`;
  return `${sameYear ? "" : `${date.getFullYear()}년 `}${day} ${time}`;
}
